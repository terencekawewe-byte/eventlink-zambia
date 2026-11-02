"""Ticket display, QR codes, and staff verification."""

from io import BytesIO

import qrcode
from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for

from app.extensions import db
from app.models import Event, Ticket
from app.services.gate_service import assignment_for
from app.services.ticket_service import record_redemption, redeem_ticket, staff_can_verify
from app.utils.decorators import login_required

tickets_bp = Blueprint("tickets", __name__, url_prefix="/tickets")


def _selected_event(value):
    event = db.session.get(Event, int(value)) if value.isdigit() else None
    return event if event is not None and staff_can_verify(g.user, event) else None


def _authorised_ticket(ticket):
    if g.user.id == ticket.client_id or staff_can_verify(g.user, ticket.event):
        return True
    return False


@tickets_bp.route("/code/<ticket_code>")
@login_required
def show(ticket_code):
    ticket = Ticket.query.filter_by(ticket_code=ticket_code.upper()).first_or_404()
    if not _authorised_ticket(ticket):
        abort(404)
    return render_template("tickets/ticket.html", ticket=ticket)


@tickets_bp.route("/<int:ticket_id>/qr")
@login_required
def qr_image(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    if not _authorised_ticket(ticket):
        abort(404)
    target = url_for("tickets.verify_token", token=ticket.qr_token, _external=True)
    image = qrcode.make(target)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return send_file(buffer, mimetype="image/png")


@tickets_bp.route("/verify", methods=["GET", "POST"])
@login_required
def verify_lookup():
    if g.user.role not in {"admin", "planner"}:
        abort(403)
    events = []
    if g.user.role == "admin":
        events = Event.query.order_by(Event.start_datetime.desc()).limit(100).all()
    elif g.user.is_approved_planner:
        events = Event.query.filter_by(organiser_id=g.user.id).order_by(Event.start_datetime.desc()).all()
    if request.method == "POST":
        code = (request.form.get("ticket_code") or "").strip().upper()
        ticket = Ticket.query.filter_by(ticket_code=code).first()
        if ticket is None or not staff_can_verify(g.user, ticket.event):
            flash("No ticket with that code was found for an event you can verify.", "danger")
            return redirect(url_for("tickets.verify_lookup"))
        selected = (request.form.get("event_id") or "").strip()
        if selected and selected != str(ticket.event_id):
            record_redemption(ticket, g.user, "invalid", "wrong_event", _selected_event(selected), "manual", code)
            db.session.commit()
            flash("This ticket does not belong to the selected event.", "danger")
            return redirect(url_for("tickets.verify_lookup"))
        return redirect(url_for("tickets.verify_token", token=ticket.qr_token))
    return render_template("tickets/verify_ticket.html", events=events, ticket=None)


@tickets_bp.route("/verify/<token>")
@login_required
def verify_token(token):
    if g.user.role == "attendant":
        ticket = Ticket.query.filter_by(qr_token=token).first()
        if ticket is not None and assignment_for(g.user, ticket.event_id) is not None:
            return redirect(url_for("gate.scan", event_id=ticket.event_id, code=token))
        flash("This ticket could not be verified for an event you are assigned to. Open the event's scan page and scan it there.", "danger")
        return redirect(url_for("gate.dashboard"))
    if g.user.role not in {"admin", "planner"}:
        abort(403)
    ticket = Ticket.query.filter_by(qr_token=token).first()
    if ticket is None or not staff_can_verify(g.user, ticket.event):
        flash("This ticket could not be verified.", "danger")
        return render_template("tickets/verify_ticket.html", events=[], ticket=None), 404
    return render_template("tickets/verify_ticket.html", events=[], ticket=ticket)


@tickets_bp.route("/<int:ticket_id>/redeem", methods=["POST"])
@login_required
def redeem(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    if not staff_can_verify(g.user, ticket.event):
        abort(403)
    selected = (request.form.get("event_id") or "").strip()
    if selected and selected != str(ticket.event_id):
        record_redemption(ticket, g.user, "invalid", "wrong_event", _selected_event(selected))
        db.session.commit()
        flash("This ticket does not belong to the selected event.", "danger")
        return redirect(url_for("tickets.verify_lookup"))
    result = redeem_ticket(ticket, g.user)
    messages = {
        "accepted": "Ticket redeemed.",
        "already_redeemed": "This ticket was already redeemed.",
        "not_valid": "This ticket is not valid for entry.",
    }
    category = "success" if result == "accepted" else "warning"
    flash(messages.get(result, "The ticket could not be redeemed."), category)
    return redirect(url_for("tickets.verify_token", token=ticket.qr_token))
