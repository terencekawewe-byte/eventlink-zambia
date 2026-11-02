"""Administrator management, monitoring, and reports."""

from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import (
    AttendantAssignment,
    AuditLog,
    ContactMessage,
    Event,
    EventCategory,
    GateAttendant,
    Order,
    Payment,
    PlannerBooking,
    PlannerPayout,
    PlannerProfile,
    Refund,
    Ticket,
    TicketRedemption,
    TicketType,
    User,
)
from app.models.database import PAYOUT_METHODS, SCAN_RESULTS, USER_ROLES, utcnow
from app.services.dashboard_service import admin_overview
from app.services.event_service import validate_event_form
from app.services.gate_service import filter_scans, repeated_scans, scan_counts
from app.services.notification_service import list_for_user, mark_all_read, mark_read, notify
from app.services.report_export import EXPORT_FORMATS, export_response, planner_document, platform_document
from app.services.report_service import (
    PERIODS,
    TRANSACTION_TYPES,
    comparison_chart,
    payout_balance,
    platform_report,
    resolve_period,
    resolve_transaction_type,
)
from app.services.report_service import planner_report as build_planner_report
from app.utils.decorators import admin_required
from app.utils.helpers import (
    audit,
    document_mimetype,
    escape_like,
    format_zmw,
    generate_reference,
    page_arg,
    paginate,
    private_document_path,
)
from app.utils.validators import (
    LUSAKA,
    normalize_phone,
    parse_money_to_ngwee,
    validate_person_name,
    validate_required_text,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _reason(minimum=5):
    reason = (request.form.get("reason") or "").strip()
    if not minimum <= len(reason) <= 500:
        return None
    return reason


def _active_admin_count():
    return User.query.filter_by(role="admin", account_status="active").count()


@admin_bp.route("/dashboard")
@admin_required
def dashboard():
    return render_template("admin/dashboard.html", **admin_overview())


@admin_bp.route("/users")
@admin_required
def users():
    query = User.query
    term = (request.args.get("q") or "").strip()[:100]
    role = request.args.get("role") or ""
    if term:
        like = f"%{escape_like(term)}%"
        query = query.filter(
            or_(
                User.full_name.ilike(like, escape="\\"),
                User.email.ilike(like, escape="\\"),
                User.phone.ilike(like, escape="\\"),
            )
        )
    if role in USER_ROLES:
        query = query.filter_by(role=role)
    else:
        role = ""
    status = request.args.get("status") or ""
    if status in {"active", "suspended"}:
        query = query.filter_by(account_status=status)
    else:
        status = ""
    counts = dict(db.session.query(User.account_status, func.count(User.id)).group_by(User.account_status).all())
    result = paginate(query.order_by(User.created_at.desc()), page_arg(), 15)
    return render_template("admin/users.html", result=result, term=term, role=role, roles=USER_ROLES, status=status, counts=counts)


@admin_bp.route("/users/<int:user_id>")
@admin_required
def user_detail(user_id):
    user = User.query.options(joinedload(User.planner_profile)).get_or_404(user_id)
    profile = user.planner_profile
    orders = (
        Order.query.options(joinedload(Order.event))
        .filter_by(client_id=user.id)
        .order_by(Order.created_at.desc())
        .limit(8)
        .all()
    )
    ticket_counts = dict(
        db.session.query(Ticket.status, func.count(Ticket.id))
        .filter(Ticket.client_id == user.id)
        .group_by(Ticket.status)
        .all()
    )
    spend = dict(
        db.session.query(Payment.is_simulated, func.coalesce(func.sum(Payment.amount), 0))
        .join(Order, Payment.order_id == Order.id)
        .filter(Order.client_id == user.id, Payment.status == "successful")
        .group_by(Payment.is_simulated)
        .all()
    )
    bookings = (
        PlannerBooking.query.options(joinedload(PlannerBooking.planner))
        .filter_by(client_id=user.id)
        .order_by(PlannerBooking.created_at.desc())
        .limit(5)
        .all()
    )
    events = (
        Event.query.filter_by(organiser_id=user.id).order_by(Event.created_at.desc()).limit(5).all()
        if user.role == "planner"
        else []
    )
    record_filter = (AuditLog.entity_type == "user") & (AuditLog.entity_id == user.id)
    if profile is not None:
        record_filter = record_filter | ((AuditLog.entity_type == "planner_profile") & (AuditLog.entity_id == profile.id))
    history = (
        AuditLog.query.options(joinedload(AuditLog.actor))
        .filter(record_filter)
        .order_by(AuditLog.created_at.desc())
        .limit(10)
        .all()
    )
    summary = {
        "orders": Order.query.filter_by(client_id=user.id).count(),
        "tickets": sum(ticket_counts.values()),
        "valid_tickets": ticket_counts.get("valid", 0),
        "bookings": PlannerBooking.query.filter_by(client_id=user.id).count(),
        "spend_live": int(spend.get(False, 0) or 0),
        "spend_simulated": int(spend.get(True, 0) or 0),
        "events": Event.query.filter_by(organiser_id=user.id).count() if user.role == "planner" else 0,
        "received": PlannerBooking.query.filter_by(planner_id=profile.id).count() if profile else 0,
    }
    return render_template(
        "admin/user_detail.html",
        user=user,
        profile=profile,
        orders=orders,
        bookings=bookings,
        events=events,
        history=history,
        summary=summary,
    )


def _back_to_user(user):
    if request.form.get("return_to") == "detail":
        return redirect(url_for("admin.user_detail", user_id=user.id))
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/suspend", methods=["POST"])
@admin_required
def user_suspend(user_id):
    user = User.query.get_or_404(user_id)
    reason = _reason()
    if user.id == g.user.id:
        flash("You cannot suspend your own account.", "danger")
    elif not reason:
        flash("A suspension reason between 5 and 500 characters is required.", "danger")
    elif user.role == "admin" and user.account_status == "active" and _active_admin_count() <= 1:
        flash("The last active administrator cannot be suspended.", "danger")
    elif user.account_status == "suspended":
        flash("This account is already suspended.", "info")
    else:
        user.account_status = "suspended"
        audit(g.user.id, "account_suspended", "user", user.id, reason)
        notify(user.id, "Account suspended", reason, "/support")
        db.session.commit()
        flash("Account suspended.", "success")
    return _back_to_user(user)


@admin_bp.route("/users/<int:user_id>/reactivate", methods=["POST"])
@admin_required
def user_reactivate(user_id):
    user = User.query.get_or_404(user_id)
    reason = _reason()
    if not reason:
        flash("A reason between 5 and 500 characters is required.", "danger")
    elif user.account_status == "active":
        flash("This account is already active.", "info")
    else:
        user.account_status = "active"
        audit(g.user.id, "account_reactivated", "user", user.id, reason)
        notify(user.id, "Account reactivated", "Your EventLink account is active again.", "/auth/login")
        db.session.commit()
        flash("Account reactivated.", "success")
    return _back_to_user(user)


@admin_bp.route("/attendants")
@admin_required
def attendants():
    query = GateAttendant.query.join(User, GateAttendant.user_id == User.id)
    term = (request.args.get("q") or "").strip()[:100]
    if term:
        like = f"%{escape_like(term)}%"
        query = query.filter(
            or_(User.full_name.ilike(like, escape="\\"), User.email.ilike(like, escape="\\"), User.phone.ilike(like, escape="\\"))
        )
    planner_ids = [row[0] for row in db.session.query(GateAttendant.created_by_id).distinct()]
    planners = User.query.filter(User.id.in_(planner_ids or [0])).order_by(User.full_name).all()
    planner_id = request.args.get("planner_id", type=int)
    if planner_id in planner_ids:
        query = query.filter(GateAttendant.created_by_id == planner_id)
    else:
        planner_id = None
    status = request.args.get("status") or ""
    if status in {"active", "inactive"}:
        query = query.filter(GateAttendant.status == status)
    else:
        status = ""
    result = paginate(query.order_by(GateAttendant.created_at.desc()), page_arg(), 20)
    scan_totals = dict(
        db.session.query(TicketRedemption.actor_id, func.count(TicketRedemption.id))
        .filter(TicketRedemption.actor_id.in_([item.user_id for item in result["items"]] or [0]))
        .group_by(TicketRedemption.actor_id)
        .all()
    )
    return render_template(
        "admin/attendants.html",
        result=result,
        term=term,
        planners=planners,
        planner_id=planner_id,
        status=status,
        scan_totals=scan_totals,
    )


@admin_bp.route("/attendants/<int:attendant_id>/status", methods=["POST"])
@admin_required
def attendant_status(attendant_id):
    attendant = db.session.get(GateAttendant, attendant_id)
    if attendant is None:
        abort(404)
    target = request.form.get("status")
    reason = _reason()
    if target not in {"active", "inactive"}:
        abort(400)
    if not reason:
        flash("A reason between 5 and 500 characters is required.", "danger")
    elif attendant.status == target:
        flash("No change was needed.", "info")
    else:
        attendant.status = target
        action = "attendant_activated" if target == "active" else "attendant_deactivated"
        audit(g.user.id, action, "gate_attendant", attendant.id, reason)
        verb = "reactivated" if target == "active" else "deactivated"
        notify(
            attendant.created_by_id,
            f"Gate attendant {verb}",
            f"EventLink {verb} the gate attendant account for {attendant.user.full_name}. Reason: {reason}",
            "/planner/attendants",
        )
        db.session.commit()
        flash(f"Gate attendant {attendant.user.full_name} {verb}.", "success")
    return redirect(request.referrer or url_for("admin.attendants"))


@admin_bp.route("/ticket-scans")
@admin_required
def ticket_scans():
    events = Event.query.filter(Event.approval_status == "approved").order_by(Event.start_datetime.desc()).all()
    actor_ids = [row[0] for row in db.session.query(TicketRedemption.actor_id).distinct()]
    actors = User.query.filter(User.id.in_(actor_ids or [0])).order_by(User.full_name).all()
    query = TicketRedemption.query
    query, filters, errors = filter_scans(query, request.args, [event.id for event in events], set(actor_ids))
    for error in errors:
        flash(error, "danger")
    scans = paginate(query.order_by(TicketRedemption.created_at.desc(), TicketRedemption.id.desc()), page_arg(), 30)
    return render_template(
        "admin/ticket_scans.html",
        scans=scans,
        events=events,
        actors=actors,
        counts=scan_counts(query),
        suspicious=repeated_scans(query),
        filters=filters,
        results=SCAN_RESULTS,
    )


@admin_bp.route("/planners")
@admin_required
def planners():
    status = request.args.get("status") or ""
    query = PlannerProfile.query.join(User, PlannerProfile.user_id == User.id).order_by(
        PlannerProfile.created_at.desc()
    )
    if status in {"pending", "approved", "rejected", "suspended"}:
        query = query.filter(PlannerProfile.approval_status == status)
    return render_template("admin/planners.html", profiles=query.all(), status=status)


@admin_bp.route("/planners/<int:profile_id>")
@admin_required
def planner_detail(profile_id):
    profile = PlannerProfile.query.get_or_404(profile_id)
    return render_template(
        "admin/planner_details.html",
        profile=profile,
        pacra_document_available=bool(private_document_path(profile.pacra_document)),
    )


@admin_bp.route("/planners/<int:profile_id>/pacra-document")
@admin_required
def planner_pacra_document(profile_id):
    profile = PlannerProfile.query.get_or_404(profile_id)
    target = private_document_path(profile.pacra_document)
    if not target:
        abort(404)
    extension = profile.pacra_document.rsplit(".", 1)[-1].lower()
    response = send_file(
        target,
        mimetype=document_mimetype(profile.pacra_document),
        as_attachment=request.args.get("download") == "1",
        download_name=f"pacra-{profile.id}.{extension}",
        max_age=0,
    )
    if extension != "pdf":
        response.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    return response


def _review_planner(profile, status, reason):
    profile.approval_status = status
    profile.rejection_reason = reason if status in {"rejected", "suspended"} else None
    profile.approved_by = g.user.id
    profile.approved_at = utcnow()
    audit(g.user.id, f"planner_{status}", "planner_profile", profile.id, reason)
    messages = {
        "approved": "Your planner profile has been approved. You can now use planner tools.",
        "rejected": f"Your planner application was rejected. Reason: {reason}",
        "suspended": f"Your planner access has been suspended. Reason: {reason}",
    }
    notify(profile.user_id, "Planner account update", messages[status], "/planner/pending")
    db.session.commit()


@admin_bp.route("/planners/<int:profile_id>/approve", methods=["POST"])
@admin_required
def planner_approve(profile_id):
    profile = PlannerProfile.query.get_or_404(profile_id)
    if profile.user.account_status != "active":
        flash("Reactivate the user account before approving the planner.", "warning")
    else:
        _review_planner(profile, "approved", None)
        flash("Planner approved.", "success")
    return redirect(url_for("admin.planner_detail", profile_id=profile.id))


@admin_bp.route("/planners/<int:profile_id>/reject", methods=["POST"])
@admin_required
def planner_reject(profile_id):
    profile = PlannerProfile.query.get_or_404(profile_id)
    reason = _reason()
    if profile.approval_status != "pending":
        flash("Only pending applications can be rejected.", "warning")
    elif not reason:
        flash("A rejection reason between 5 and 500 characters is required.", "danger")
    else:
        _review_planner(profile, "rejected", reason)
        flash("Planner application rejected.", "success")
    return redirect(url_for("admin.planner_detail", profile_id=profile.id))


@admin_bp.route("/planners/<int:profile_id>/suspend", methods=["POST"])
@admin_required
def planner_suspend(profile_id):
    profile = PlannerProfile.query.get_or_404(profile_id)
    reason = _reason()
    if profile.approval_status != "approved":
        flash("Only approved planners can be suspended this way.", "warning")
    elif not reason:
        flash("A suspension reason is required.", "danger")
    else:
        _review_planner(profile, "suspended", reason)
        flash("Planner access suspended.", "success")
    return redirect(url_for("admin.planner_detail", profile_id=profile.id))


@admin_bp.route("/events")
@admin_required
def events():
    status = request.args.get("status") or ""
    term = (request.args.get("q") or "").strip()[:100]
    category = request.args.get("category", type=int)
    query = Event.query.join(User, Event.organiser_id == User.id).options(
        joinedload(Event.organiser), joinedload(Event.category)
    )
    if status in {"draft", "pending", "approved", "rejected"}:
        query = query.filter(Event.approval_status == status)
    else:
        status = ""
    if term:
        like = f"%{escape_like(term)}%"
        query = query.filter(
            or_(
                Event.title.ilike(like, escape="\\"),
                Event.venue.ilike(like, escape="\\"),
                Event.location.ilike(like, escape="\\"),
                User.full_name.ilike(like, escape="\\"),
            )
        )
    if category:
        query = query.filter(Event.category_id == category)
    result = paginate(query.order_by(Event.created_at.desc()), page_arg(), 15)
    categories = EventCategory.query.order_by(EventCategory.name).all()
    return render_template(
        "admin/events.html", result=result, status=status, term=term, category=category, categories=categories
    )


@admin_bp.route("/events/<int:event_id>")
@admin_required
def event_detail(event_id):
    event = Event.query.options(joinedload(Event.ticket_types), joinedload(Event.organiser)).get_or_404(event_id)
    sold = (
        db.session.query(func.coalesce(func.sum(TicketType.quantity_sold), 0))
        .filter(TicketType.event_id == event.id)
        .scalar()
    )
    return render_template("admin/event_details.html", event=event, sold=int(sold or 0))


def _event_ready(event):
    probe = {
        "title": event.title,
        "category_id": str(event.category_id),
        "description": event.description,
        "venue": event.venue,
        "location": event.location,
        "contact_info": event.contact_info,
        "start_datetime": "",
        "end_datetime": "",
        "sales_start": "",
        "sales_end": "",
    }
    if len(event.description or "") < 30 or not event.poster_path or not event.ticket_types:
        return False
    if not (event.sales_start < event.sales_end <= event.start_datetime < event.end_datetime):
        return False
    return all(ticket.price > 0 and ticket.quantity_total > 0 for ticket in event.ticket_types) and bool(probe)


@admin_bp.route("/events/<int:event_id>/approve", methods=["POST"])
@admin_required
def event_approve(event_id):
    event = Event.query.get_or_404(event_id)
    if event.approval_status not in {"pending", "rejected"}:
        flash("This event is not waiting for approval.", "warning")
    elif not _event_ready(event):
        flash("The event is missing a poster, description, schedule, or ticket category.", "danger")
    else:
        event.approval_status = "approved"
        event.publication_status = "published"
        event.rejection_reason = None
        event.reviewed_by = g.user.id
        event.reviewed_at = utcnow()
        audit(g.user.id, "event_approved", "event", event.id, None)
        notify(event.organiser_id, "Event approved", f"{event.title} is now published.", f"/events/{event.id}")
        db.session.commit()
        flash("Event approved and published.", "success")
    return redirect(url_for("admin.event_detail", event_id=event.id))


@admin_bp.route("/events/<int:event_id>/reject", methods=["POST"])
@admin_required
def event_reject(event_id):
    event = Event.query.get_or_404(event_id)
    reason = _reason()
    if not reason:
        flash("A rejection reason is required.", "danger")
    else:
        event.approval_status = "rejected"
        event.publication_status = "unpublished"
        event.rejection_reason = reason
        event.reviewed_by = g.user.id
        event.reviewed_at = utcnow()
        audit(g.user.id, "event_rejected", "event", event.id, reason)
        notify(event.organiser_id, "Event not approved", reason, f"/planner/events/{event.id}/edit")
        db.session.commit()
        flash("Event rejected.", "success")
    return redirect(url_for("admin.event_detail", event_id=event.id))


@admin_bp.route("/events/<int:event_id>/suspend", methods=["POST"])
@admin_required
def event_suspend(event_id):
    event = Event.query.get_or_404(event_id)
    reason = _reason()
    if event.publication_status != "published":
        flash("Only a published event can be suspended.", "warning")
    elif not reason:
        flash("A suspension reason is required.", "danger")
    else:
        event.publication_status = "suspended"
        event.reviewed_by = g.user.id
        event.reviewed_at = utcnow()
        audit(g.user.id, "event_suspended", "event", event.id, reason)
        notify(event.organiser_id, "Event suspended", reason, f"/planner/events/{event.id}/edit")
        db.session.commit()
        flash("Event suspended.", "success")
    return redirect(url_for("admin.event_detail", event_id=event.id))


@admin_bp.route("/events/<int:event_id>/cancel", methods=["POST"])
@admin_required
def event_cancel(event_id):
    event = Event.query.get_or_404(event_id)
    reason = _reason()
    if event.publication_status == "cancelled":
        flash("This event is already cancelled.", "info")
    elif not reason:
        flash("A cancellation reason is required.", "danger")
    else:
        event.publication_status = "cancelled"
        event.cancellation_reason = reason
        event.reviewed_by = g.user.id
        event.reviewed_at = utcnow()
        holders = (
            User.query.join(Ticket, Ticket.client_id == User.id)
            .filter(Ticket.event_id == event.id, Ticket.status == "valid")
            .distinct()
            .all()
        )
        for holder in holders:
            notify(
                holder.id,
                "Event cancelled",
                f"{event.title} was cancelled. You can request a refund from your payment history. A refund is not completed until it is confirmed.",
                "/client/payments",
            )
        notify(event.organiser_id, "Event cancelled", reason, f"/planner/events")
        audit(g.user.id, "event_cancelled", "event", event.id, reason)
        db.session.commit()
        flash("Event cancelled. Existing tickets and payments were kept.", "success")
    return redirect(url_for("admin.event_detail", event_id=event.id))


@admin_bp.route("/ticket-sales")
@admin_required
def ticket_sales():
    view = "issued" if request.args.get("view") == "issued" else "categories"
    term = (request.args.get("q") or "").strip()[:100]
    like = f"%{escape_like(term)}%"
    categories = EventCategory.query.order_by(EventCategory.name).all()
    context = {"view": view, "term": term, "categories": categories}
    if view == "issued":
        status = request.args.get("status") or ""
        start, end, start_raw, end_raw, valid = _date_bounds()
        query = (
            Ticket.query.join(Event, Ticket.event_id == Event.id)
            .join(User, Ticket.client_id == User.id)
            .options(joinedload(Ticket.event), joinedload(Ticket.client), joinedload(Ticket.ticket_type))
        )
        if term:
            query = query.filter(
                or_(
                    Ticket.ticket_code.ilike(like, escape="\\"),
                    Event.title.ilike(like, escape="\\"),
                    User.full_name.ilike(like, escape="\\"),
                )
            )
        if status in {"valid", "redeemed", "cancelled", "refunded"}:
            query = query.filter(Ticket.status == status)
        else:
            status = ""
        if valid and start is not None:
            query = query.filter(Ticket.issued_at >= start)
        if valid and end is not None:
            query = query.filter(Ticket.issued_at < end)
        context.update(
            result=paginate(query.order_by(Ticket.issued_at.desc()), page_arg(), 20),
            status=status,
            date_from=start_raw,
            date_to=end_raw,
        )
        return render_template("admin/ticket_sales.html", **context)

    timing = request.args.get("timing") or ""
    category = request.args.get("category", type=int)
    query = TicketType.query.join(Event, TicketType.event_id == Event.id)
    if term:
        query = query.filter(Event.title.ilike(like, escape="\\"))
    if category:
        query = query.filter(Event.category_id == category)
    now = utcnow()
    if timing == "upcoming":
        query = query.filter(Event.end_datetime >= now)
    elif timing == "past":
        query = query.filter(Event.end_datetime < now)
    else:
        timing = ""
    sold, reserved, capacity = query.with_entities(
        func.coalesce(func.sum(TicketType.quantity_sold), 0),
        func.coalesce(func.sum(TicketType.quantity_reserved), 0),
        func.coalesce(func.sum(TicketType.quantity_total), 0),
    ).one()
    query = query.options(joinedload(TicketType.event)).order_by(Event.start_datetime.desc(), TicketType.price)
    context.update(
        result=paginate(query, page_arg(), 20),
        timing=timing,
        category=category,
        totals={"sold": int(sold), "reserved": int(reserved), "capacity": int(capacity)},
    )
    return render_template("admin/ticket_sales.html", **context)


@admin_bp.route("/orders")
@admin_required
def orders():
    term = (request.args.get("q") or "").strip()[:100]
    query = (
        Order.query.join(User, Order.client_id == User.id)
        .join(Event, Order.event_id == Event.id)
        .options(joinedload(Order.client), joinedload(Order.event))
    )
    if term:
        like = f"%{escape_like(term)}%"
        query = query.filter(
            or_(
                Order.order_reference.ilike(like, escape="\\"),
                User.full_name.ilike(like, escape="\\"),
                Event.title.ilike(like, escape="\\"),
            )
        )
    date_value = (request.args.get("date") or "").strip()
    if date_value:
        try:
            day = datetime.strptime(date_value, "%Y-%m-%d")
            start = day.replace(tzinfo=LUSAKA).astimezone(timezone.utc).replace(tzinfo=None)
            query = query.filter(Order.created_at >= start, Order.created_at < start + timedelta(days=1))
        except ValueError:
            flash("Enter the order date as YYYY-MM-DD.", "danger")
    result = paginate(query.order_by(Order.created_at.desc()), page_arg(), 15)
    return render_template("admin/orders.html", result=result, term=term, date_value=date_value)


@admin_bp.route("/orders/<order_reference>")
@admin_required
def order_detail(order_reference):
    order = Order.query.filter_by(order_reference=order_reference).first_or_404()
    return render_template("admin/order_details.html", order=order)


@admin_bp.route("/payments")
@admin_required
def payments():
    status = request.args.get("status") or ""
    term = (request.args.get("q") or "").strip()[:100]
    method = request.args.get("method") or ""
    start, end, start_raw, end_raw, valid = _date_bounds()
    methods = [value for (value,) in db.session.query(Payment.payment_method).distinct().order_by(Payment.payment_method)]
    query = Payment.query.join(Order, Payment.order_id == Order.id)
    if term:
        like = f"%{escape_like(term)}%"
        query = query.filter(
            or_(
                Payment.payment_reference.ilike(like, escape="\\"),
                Payment.provider_reference.ilike(like, escape="\\"),
                Order.order_reference.ilike(like, escape="\\"),
            )
        )
    if method in methods:
        query = query.filter(Payment.payment_method == method)
    else:
        method = ""
    if valid and start is not None:
        query = query.filter(Payment.created_at >= start)
    if valid and end is not None:
        query = query.filter(Payment.created_at < end)
    totals = dict(
        query.with_entities(Payment.status, func.coalesce(func.sum(Payment.amount), 0))
        .group_by(Payment.status)
        .all()
    )
    if status in {"pending", "successful", "failed", "refunded"}:
        query = query.filter(Payment.status == status)
    else:
        status = ""
    query = query.options(joinedload(Payment.order)).order_by(Payment.created_at.desc())
    filters = {key: value for key, value in {"q": term, "method": method, "date_from": start_raw, "date_to": end_raw}.items() if value}
    refunds = Refund.query.order_by(Refund.created_at.desc()).limit(50).all()
    return render_template(
        "admin/payments.html",
        result=paginate(query, page_arg(), 20),
        status=status,
        term=term,
        method=method,
        methods=methods,
        date_from=start_raw,
        date_to=end_raw,
        filters=filters,
        totals={key: int(value) for key, value in totals.items()},
        refunds=refunds,
    )


@admin_bp.route("/refunds/<int:refund_id>", methods=["POST"])
@admin_required
def refund_update(refund_id):
    refund = Refund.query.get_or_404(refund_id)
    decision = request.form.get("decision")
    note = (request.form.get("note") or "").strip()
    if decision not in {"approved", "rejected", "completed"} or not 5 <= len(note) <= 500:
        flash("Choose a decision and enter a note between 5 and 500 characters.", "danger")
    elif refund.status in {"rejected", "completed"}:
        flash("This refund request is already closed.", "info")
    elif decision == "completed" and request.form.get("confirm_external") != "yes":
        flash("Confirm that the refund was completed outside EventLink before recording it.", "danger")
    elif decision == "completed":
        prefix = "Simulation record only. No real money was transferred. " if refund.payment.is_simulated else ""
        refund.status = "completed"
        refund.confirmation_note = (prefix + note)[:500]
        refund.processed_by = g.user.id
        refund.payment.status = "refunded"
        refund.order.order_status = "refunded"
        for item in refund.order.items:
            for ticket in item.tickets:
                if ticket.status == "valid":
                    ticket.status = "refunded"
        audit(g.user.id, "refund_completed", "refund", refund.id, refund.confirmation_note)
        notify(refund.requested_by, "Refund recorded", refund.confirmation_note, "/client/payments")
        db.session.commit()
        flash("Refund recorded. EventLink did not move money by itself.", "success")
    else:
        refund.status = decision
        refund.confirmation_note = note
        refund.processed_by = g.user.id
        audit(g.user.id, f"refund_{decision}", "refund", refund.id, note)
        notify(refund.requested_by, "Refund update", note, "/client/payments")
        db.session.commit()
        flash("Refund request updated.", "success")
    return redirect(url_for("admin.payments", _anchor="refunds"))


@admin_bp.route("/bookings")
@admin_required
def bookings():
    items = PlannerBooking.query.order_by(PlannerBooking.created_at.desc()).limit(100).all()
    return render_template("admin/bookings.html", bookings=items)


def _date_bounds():
    start = end = None
    start_raw = (request.args.get("date_from") or "").strip()
    end_raw = (request.args.get("date_to") or "").strip()
    try:
        if start_raw:
            start = datetime.strptime(start_raw, "%Y-%m-%d").replace(tzinfo=LUSAKA).astimezone(timezone.utc).replace(tzinfo=None)
        if end_raw:
            end_day = datetime.strptime(end_raw, "%Y-%m-%d") + timedelta(days=1)
            end = end_day.replace(tzinfo=LUSAKA).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        flash("Enter dates as YYYY-MM-DD.", "danger")
        return None, None, start_raw, end_raw, False
    if start and end and start >= end:
        flash("The start date must be before the end date.", "danger")
        return None, None, start_raw, end_raw, False
    return start, end, start_raw, end_raw, True


MAX_COMPARED_PLANNERS = 6


def _compare_ids():
    ids = []
    for raw in request.args.getlist("compare"):
        raw = raw.strip()
        if raw.isdigit() and int(raw) not in ids:
            ids.append(int(raw))
    return ids[:MAX_COMPARED_PLANNERS]


@admin_bp.route("/reports")
@admin_required
def reports():
    period, error = resolve_period(request.args)
    report = platform_report(period, resolve_transaction_type(request.args))
    compare_ids = _compare_ids()
    compared = [row for row in report["comparison"] if row["profile"].id in compare_ids]
    compare_error = None
    if compare_ids and len(compared) < 2:
        compare_error = "Choose at least two planners to compare."
    elif len(compared) >= 2:
        report["charts"].append(comparison_chart(compared, report["rate"]))
    return render_template(
        "admin/reports.html",
        report=report,
        filter_error=error,
        periods=PERIODS,
        transaction_types=TRANSACTION_TYPES,
        export_endpoint="admin.reports_export",
        compare_ids=compare_ids,
        compared=compared if len(compared) >= 2 else [],
        compare_error=compare_error,
        max_compared=MAX_COMPARED_PLANNERS,
    )


@admin_bp.route("/reports/export.<fmt>")
@admin_required
def reports_export(fmt):
    if fmt not in EXPORT_FORMATS:
        abort(404)
    period, error = resolve_period(request.args)
    if error:
        flash(error, "danger")
        return redirect(url_for("admin.reports"))
    report = platform_report(period, resolve_transaction_type(request.args))
    return export_response(platform_document(report), fmt, "platform", period.key)


def _planner_report_filters(profile):
    period, error = resolve_period(request.args)
    raw_event = (request.args.get("event") or "").strip()
    event = None
    if raw_event:
        if not raw_event.isdigit():
            abort(404)
        event = Event.query.filter_by(id=int(raw_event), organiser_id=profile.user_id).first_or_404()
    return period, error, event, resolve_transaction_type(request.args)


@admin_bp.route("/reports/planners/<int:profile_id>")
@admin_required
def planner_report(profile_id):
    profile = db.get_or_404(PlannerProfile, profile_id)
    period, error, event, transaction_type = _planner_report_filters(profile)
    event_options = (
        Event.query.with_entities(Event.id, Event.title)
        .filter_by(organiser_id=profile.user_id)
        .order_by(Event.start_datetime.desc())
        .all()
    )
    return render_template(
        "admin/planner_report.html",
        profile=profile,
        report=build_planner_report(profile, period, event, transaction_type),
        filter_error=error,
        event_options=event_options,
        periods=PERIODS,
        transaction_types=TRANSACTION_TYPES,
        export_endpoint="admin.planner_report_export",
        admin_view=True,
        payout_methods=PAYOUT_METHODS,
        balance=payout_balance(profile),
    )


@admin_bp.route("/reports/planners/<int:profile_id>/export.<fmt>")
@admin_required
def planner_report_export(profile_id, fmt):
    if fmt not in EXPORT_FORMATS:
        abort(404)
    profile = db.get_or_404(PlannerProfile, profile_id)
    period, error, event, transaction_type = _planner_report_filters(profile)
    if error:
        flash(error, "danger")
        return redirect(url_for("admin.planner_report", profile_id=profile.id))
    report = build_planner_report(profile, period, event, transaction_type)
    return export_response(planner_document(profile, report), fmt, profile.business_name, period.key)


@admin_bp.route("/reports/planners/<int:profile_id>/payouts", methods=["POST"])
@admin_required
def record_payout(profile_id):
    profile = db.get_or_404(PlannerProfile, profile_id)
    back = url_for("admin.planner_report", profile_id=profile.id, _anchor="record-payout")
    errors = []
    amount = None
    try:
        amount = parse_money_to_ngwee(request.form.get("amount"), "Payout amount")
    except ValueError as exc:
        errors.append(str(exc))
    method = request.form.get("method") or ""
    if method not in PAYOUT_METHODS:
        errors.append("Choose how the money was sent.")
    external_reference = (request.form.get("external_reference") or "").strip()
    if not 3 <= len(external_reference) <= 80:
        errors.append("Enter the bank or mobile money transfer reference (3 to 80 characters).")
    note = (request.form.get("note") or "").strip()
    if len(note) > 500:
        errors.append("Keep the note under 500 characters.")
    if request.form.get("confirm") != "yes":
        errors.append("Confirm that the money has already been sent to the planner.")
    balance = payout_balance(profile)["balance"]
    if amount is not None and amount > balance:
        errors.append(f"The payout cannot be more than the pending balance of {format_zmw(max(balance, 0))}.")
    if external_reference and PlannerPayout.query.filter_by(planner_id=profile.id, external_reference=external_reference).first():
        errors.append("That transfer reference has already been recorded for this planner.")
    if errors:
        for message in errors:
            flash(message, "danger")
        return redirect(back)

    payout = PlannerPayout(
        payout_reference=generate_reference("PO"),
        planner_id=profile.id,
        amount=amount,
        method=method,
        external_reference=external_reference,
        note=note,
        recorded_by=g.user.id,
    )
    db.session.add(payout)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        flash("That transfer reference has already been recorded for this planner.", "danger")
        return redirect(back)
    audit(
        g.user.id,
        "payout_recorded",
        "planner_payout",
        payout.id,
        f"{format_zmw(amount)} to {profile.business_name} by {payout.method_label}, transfer ref {external_reference}",
    )
    notify(
        profile.user_id,
        "Payout recorded",
        f"EventLink recorded a payout of {format_zmw(amount)} to you by {payout.method_label.lower()} (reference {external_reference}).",
        url_for("planner.reports"),
    )
    db.session.commit()
    flash(f"Payout of {format_zmw(amount)} recorded for {profile.business_name}.", "success")
    return redirect(back)


@admin_bp.route("/audit-logs")
@admin_required
def audit_logs():
    term = (request.args.get("q") or "").strip()[:80]
    entity = request.args.get("entity") or ""
    start, end, start_raw, end_raw, valid = _date_bounds()
    entities = [value for (value,) in db.session.query(AuditLog.entity_type).distinct().order_by(AuditLog.entity_type)]
    query = AuditLog.query.options(joinedload(AuditLog.actor))
    if term:
        like = f"%{escape_like(term)}%"
        query = query.filter(or_(AuditLog.action.ilike(like, escape="\\"), AuditLog.reason.ilike(like, escape="\\")))
    if entity in entities:
        query = query.filter(AuditLog.entity_type == entity)
    else:
        entity = ""
    if valid and start is not None:
        query = query.filter(AuditLog.created_at >= start)
    if valid and end is not None:
        query = query.filter(AuditLog.created_at < end)
    return render_template(
        "admin/audit_logs.html",
        result=paginate(query.order_by(AuditLog.created_at.desc()), page_arg(), 20),
        term=term,
        entity=entity,
        entities=entities,
        date_from=start_raw,
        date_to=end_raw,
    )


@admin_bp.route("/profile", methods=["GET", "POST"])
@admin_required
def profile():
    if request.method == "POST":
        name, name_error = validate_person_name(request.form.get("full_name"))
        phone, phone_error = normalize_phone(request.form.get("phone"))
        if name_error or phone_error:
            flash(name_error or phone_error, "danger")
        elif User.query.filter(User.phone == phone, User.id != g.user.id).first():
            flash("That phone number is already used by another account.", "danger")
        else:
            g.user.full_name = name
            g.user.phone = phone
            audit(g.user.id, "admin_profile_updated", "user", g.user.id, None)
            db.session.commit()
            flash("Your profile has been updated.", "success")
            return redirect(url_for("admin.profile"))
    recent = (
        AuditLog.query.filter_by(actor_id=g.user.id).order_by(AuditLog.created_at.desc()).limit(8).all()
    )
    return render_template("admin/profile.html", recent=recent)


@admin_bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        name, name_error = validate_required_text(request.form.get("name"), "Category name", 3, 80)
        description, description_error = validate_required_text(request.form.get("description"), "Description", 3, 255)
        if name_error or description_error:
            flash(name_error or description_error, "danger")
        elif EventCategory.query.filter(func.lower(EventCategory.name) == name.lower()).first():
            flash("That category already exists.", "danger")
        else:
            db.session.add(EventCategory(name=name, description=description, is_active=True))
            audit(g.user.id, "category_created", "event_category", None, name)
            db.session.commit()
            flash("Category added.", "success")
            return redirect(url_for("admin.settings"))
    categories = EventCategory.query.order_by(EventCategory.name).all()
    return render_template("admin/settings.html", categories=categories)


@admin_bp.route("/categories/<int:category_id>/toggle", methods=["POST"])
@admin_required
def category_toggle(category_id):
    category = EventCategory.query.get_or_404(category_id)
    category.is_active = not category.is_active
    audit(g.user.id, "category_toggled", "event_category", category.id, category.name)
    db.session.commit()
    flash("Category updated.", "success")
    return redirect(url_for("admin.settings"))


@admin_bp.route("/messages")
@admin_required
def messages():
    status = request.args.get("status") or ""
    term = (request.args.get("q") or "").strip()[:100]
    query = ContactMessage.query
    if status in {"new", "reviewed"}:
        query = query.filter_by(status=status)
    if term:
        like = f"%{escape_like(term)}%"
        query = query.filter(
            or_(
                ContactMessage.name.ilike(like, escape="\\"),
                ContactMessage.email.ilike(like, escape="\\"),
                ContactMessage.subject.ilike(like, escape="\\"),
                ContactMessage.message.ilike(like, escape="\\"),
            )
        )
    counts = {
        "all": ContactMessage.query.count(),
        "new": ContactMessage.query.filter_by(status="new").count(),
        "reviewed": ContactMessage.query.filter_by(status="reviewed").count(),
    }
    result = paginate(query.order_by(ContactMessage.created_at.desc()), page_arg(), 10)
    return render_template("admin/messages.html", result=result, status=status, term=term, counts=counts)


@admin_bp.route("/messages/<int:message_id>/status", methods=["POST"])
@admin_required
def contact_status(message_id):
    message = ContactMessage.query.get_or_404(message_id)
    status = request.form.get("status")
    if status not in {"new", "reviewed"}:
        flash("Choose a valid message status.", "danger")
    else:
        message.status = status
        audit(g.user.id, f"contact_message_{status}", "contact_message", message.id, message.subject)
        db.session.commit()
        flash("Message marked as reviewed." if status == "reviewed" else "Message marked as new.", "success")
    return redirect(request.referrer or url_for("admin.messages"))


@admin_bp.route("/notifications")
@admin_required
def notifications():
    return render_template("admin/notifications.html", notifications=list_for_user(g.user))


@admin_bp.route("/notifications/<int:notification_id>/read", methods=["POST"])
@admin_required
def notification_read(notification_id):
    if not mark_read(g.user, notification_id):
        return render_template("errors/404.html"), 404
    return redirect(request.referrer or url_for("admin.notifications"))


@admin_bp.route("/notifications/read-all", methods=["POST"])
@admin_required
def notifications_read_all():
    mark_all_read(g.user)
    return redirect(url_for("admin.notifications"))
