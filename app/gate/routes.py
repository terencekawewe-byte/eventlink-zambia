"""Gate attendant dashboard, ticket scanning, and scan history."""

from flask import Blueprint, abort, g, jsonify, render_template, request

from app.models import TicketRedemption
from app.models.database import SCAN_RESULTS
from app.services.gate_service import assigned_events, assignment_for, event_stats, recent_scans, verify_scan
from app.utils.decorators import attendant_required
from app.utils.helpers import format_local, page_arg, paginate

gate_bp = Blueprint("gate", __name__, url_prefix="/gate")


def _assigned_event_or_404(event_id):
    assignment = assignment_for(g.user, event_id)
    if assignment is None:
        abort(404)
    return assignment.event


def _wants_json():
    return request.accept_mimetypes.best == "application/json" or request.headers.get("X-Requested-With") == "fetch"


def _serialise(outcome):
    data = dict(outcome)
    data["checked_at"] = format_local(outcome["checked_at"], "%d %b %Y, %H:%M:%S")
    if outcome["ticket"]:
        ticket = dict(outcome["ticket"])
        ticket["redeemed_at"] = format_local(ticket["redeemed_at"], "%d %b %Y, %H:%M:%S") if ticket["redeemed_at"] else None
        data["ticket"] = ticket
    return data


@gate_bp.route("/")
@attendant_required
def dashboard():
    events = assigned_events(g.user)
    cards = [{"event": event, "stats": event_stats(event)} for event in events]
    history = recent_scans([event.id for event in events], actor_id=g.user.id, limit=10)
    return render_template("gate/dashboard.html", cards=cards, history=history)


@gate_bp.route("/events/<int:event_id>/scan")
@attendant_required
def scan(event_id):
    event = _assigned_event_or_404(event_id)
    return render_template(
        "gate/scan.html",
        event=event,
        stats=event_stats(event),
        history=recent_scans([event.id], actor_id=g.user.id, limit=5),
        outcome=None,
        prefill=(request.args.get("code") or "").strip()[:64],
    )


@gate_bp.route("/events/<int:event_id>/verify", methods=["POST"])
@attendant_required
def verify(event_id):
    event = _assigned_event_or_404(event_id)
    method = request.form.get("method", "manual")
    outcome = verify_scan(g.user, event, request.form.get("code", ""), method)
    stats = event_stats(event)
    if _wants_json():
        return jsonify({"ok": True, "outcome": _serialise(outcome), "stats": stats})
    return render_template(
        "gate/scan.html",
        event=event,
        stats=stats,
        history=recent_scans([event.id], actor_id=g.user.id, limit=5),
        outcome=_serialise(outcome),
    )


@gate_bp.route("/history")
@attendant_required
def history():
    events = assigned_events(g.user)
    event_ids = [event.id for event in events]
    query = TicketRedemption.query.filter(TicketRedemption.event_id.in_(event_ids or [0]))
    selected_event = request.args.get("event_id", type=int)
    if selected_event in event_ids:
        query = query.filter(TicketRedemption.event_id == selected_event)
    result = request.args.get("result", "")
    if result in SCAN_RESULTS:
        query = query.filter(TicketRedemption.result == result)
    if request.args.get("mine") == "1":
        query = query.filter(TicketRedemption.actor_id == g.user.id)
    scans = paginate(query.order_by(TicketRedemption.created_at.desc(), TicketRedemption.id.desc()), page_arg(), 25)
    return render_template(
        "gate/history.html",
        scans=scans,
        events=events,
        selected_event=selected_event,
        result=result,
        results=SCAN_RESULTS,
    )
