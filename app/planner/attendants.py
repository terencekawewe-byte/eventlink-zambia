"""Planner management of gate attendants and ticket scan activity for their events."""

from flask import abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import AttendantAssignment, Event, GateAttendant, TicketRedemption, User
from app.models.database import SCAN_RESULTS, utcnow
from app.planner.routes import planner_bp
from app.services.gate_service import (
    assignable_events,
    attendants_for,
    filter_scans,
    repeated_scans,
    scan_counts,
    temporary_password,
)
from app.utils.decorators import approved_planner_required
from app.utils.helpers import audit, page_arg, paginate
from app.utils.validators import normalize_phone, validate_email_address, validate_person_name


def _own_attendant(attendant_id):
    attendant = db.session.get(GateAttendant, attendant_id)
    if attendant is None or attendant.created_by_id != g.user.id:
        abort(404)
    return attendant


def _render_attendants(form=None, credentials=None, status=200):
    events = assignable_events(g.user.id)
    return (
        render_template(
            "planner/attendants.html",
            attendants=attendants_for(g.user.id),
            events=events,
            form=form or {},
            credentials=credentials,
        ),
        status,
    )


def _validate_new_attendant(form):
    name, name_error = validate_person_name(form.get("full_name"))
    email, email_error = validate_email_address(form.get("email"))
    phone, phone_error = normalize_phone(form.get("phone"))
    error = name_error or email_error or phone_error
    if not error and User.query.filter_by(email=email).first():
        error = "An account with this email address already exists. Use a different email for the gate attendant."
    if not error and User.query.filter_by(phone=phone).first():
        error = "An account with this phone number already exists. Use a different phone number for the gate attendant."
    return name, email, phone, error


def _assign(attendant, event):
    assignment = AttendantAssignment.query.filter_by(attendant_id=attendant.id, event_id=event.id).first()
    if assignment is None:
        assignment = AttendantAssignment(attendant_id=attendant.id, event_id=event.id, assigned_by_id=g.user.id, status="active")
        db.session.add(assignment)
    elif assignment.status == "active":
        return False
    else:
        assignment.status = "active"
        assignment.assigned_by_id = g.user.id
        assignment.revoked_at = None
        assignment.revoked_by_id = None
    db.session.flush()
    audit(g.user.id, "attendant_assigned", "gate_attendant", attendant.id, f"Assigned to event #{event.id} {event.title}")
    return True


def _assignable_event(value):
    try:
        event_id = int(value)
    except (TypeError, ValueError):
        return None
    return next((event for event in assignable_events(g.user.id) if event.id == event_id), None)


@planner_bp.route("/attendants", methods=["GET", "POST"])
@approved_planner_required
def attendants():
    if request.method == "GET":
        return _render_attendants()
    form = request.form
    name, email, phone, error = _validate_new_attendant(form)
    event = None
    if not error and form.get("event_id"):
        event = _assignable_event(form.get("event_id"))
        if event is None:
            error = "Choose one of your approved, published events."
    if error:
        flash(error, "danger")
        return _render_attendants(form, status=400)
    password = temporary_password()
    try:
        user = User(full_name=name, email=email, phone=phone, role="attendant", account_status="active")
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        attendant = GateAttendant(user_id=user.id, created_by_id=g.user.id, status="active", must_change_password=True)
        db.session.add(attendant)
        db.session.flush()
        audit(g.user.id, "attendant_created", "gate_attendant", attendant.id, f"Gate attendant account for {name}")
        if event is not None:
            _assign(attendant, event)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("An account with this email address or phone number already exists.", "danger")
        return _render_attendants(form, status=400)
    return _render_attendants(credentials={"name": name, "email": email, "password": password, "new": True})


@planner_bp.route("/attendants/<int:attendant_id>/assign", methods=["POST"])
@approved_planner_required
def attendant_assign(attendant_id):
    attendant = _own_attendant(attendant_id)
    event = _assignable_event(request.form.get("event_id"))
    if event is None:
        flash("Choose one of your approved, published events.", "danger")
    elif _assign(attendant, event):
        db.session.commit()
        flash(f"{attendant.user.full_name} can now scan tickets for {event.title}.", "success")
    else:
        flash(f"{attendant.user.full_name} is already assigned to {event.title}.", "info")
    return redirect(url_for("planner.attendants") + f"#attendant-{attendant.id}")


@planner_bp.route("/attendants/<int:attendant_id>/assignments/<int:assignment_id>/revoke", methods=["POST"])
@approved_planner_required
def attendant_revoke(attendant_id, assignment_id):
    attendant = _own_attendant(attendant_id)
    assignment = AttendantAssignment.query.filter_by(id=assignment_id, attendant_id=attendant.id).first_or_404()
    if assignment.status == "active":
        assignment.status = "revoked"
        assignment.revoked_at = utcnow()
        assignment.revoked_by_id = g.user.id
        audit(g.user.id, "attendant_access_removed", "gate_attendant", attendant.id, f"Removed from event #{assignment.event_id} {assignment.event.title}")
        db.session.commit()
        flash(f"{attendant.user.full_name} can no longer scan tickets for {assignment.event.title}.", "success")
    return redirect(url_for("planner.attendants") + f"#attendant-{attendant.id}")


@planner_bp.route("/attendants/<int:attendant_id>/status", methods=["POST"])
@approved_planner_required
def attendant_status(attendant_id):
    attendant = _own_attendant(attendant_id)
    target = request.form.get("status")
    if target not in {"active", "inactive"} or target == attendant.status:
        return redirect(url_for("planner.attendants") + f"#attendant-{attendant.id}")
    attendant.status = target
    audit(g.user.id, "attendant_activated" if target == "active" else "attendant_deactivated", "gate_attendant", attendant.id)
    db.session.commit()
    if target == "active":
        flash(f"{attendant.user.full_name} is active again.", "success")
    else:
        flash(f"{attendant.user.full_name} has been deactivated and can no longer log in or scan tickets.", "success")
    return redirect(url_for("planner.attendants") + f"#attendant-{attendant.id}")


@planner_bp.route("/attendants/<int:attendant_id>/reset-password", methods=["POST"])
@approved_planner_required
def attendant_reset_password(attendant_id):
    attendant = _own_attendant(attendant_id)
    password = temporary_password()
    attendant.user.set_password(password)
    attendant.must_change_password = True
    audit(g.user.id, "attendant_password_reset", "gate_attendant", attendant.id)
    db.session.commit()
    return _render_attendants(credentials={"name": attendant.user.full_name, "email": attendant.user.email, "password": password, "new": False})


@planner_bp.route("/scan-activity")
@approved_planner_required
def scan_activity():
    events = Event.query.filter_by(organiser_id=g.user.id).order_by(Event.start_datetime.desc()).all()
    event_ids = [event.id for event in events]
    staff = attendants_for(g.user.id)
    actor_ids = {attendant.user_id for attendant in staff} | {g.user.id}
    query = TicketRedemption.query.filter(TicketRedemption.event_id.in_(event_ids or [0]))
    query, filters, errors = filter_scans(query, request.args, event_ids, actor_ids)
    for error in errors:
        flash(error, "danger")
    scans = paginate(query.order_by(TicketRedemption.created_at.desc(), TicketRedemption.id.desc()), page_arg(), 30)
    return render_template(
        "planner/scan_activity.html",
        scans=scans,
        events=events,
        staff=staff,
        counts=scan_counts(query),
        suspicious=repeated_scans(query),
        filters=filters,
        results=SCAN_RESULTS,
    )
