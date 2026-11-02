"""Gate attendants and ticket verification at event entrances.

The server decides every scan result. The attendant's browser only submits the
text read from a QR code (or typed from a ticket) and displays the decision.
"""

import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.extensions import db
from app.models import AttendantAssignment, Event, GateAttendant, Ticket, TicketRedemption
from app.models.database import SCAN_RESULTS
from app.services.ticket_service import record_redemption, redeem_for_entry
from app.utils.helpers import LUSAKA
from app.utils.validators import utcnow_naive

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
CODE_PATTERN = re.compile(r"^ELZ-[0-9A-F]{8}-[0-9A-F]{4}$")
VERIFY_URL_PATTERN = re.compile(r"/tickets/verify/([A-Za-z0-9_-]{43})(?:[/?#]|$)")
RESULT_MESSAGES = {
    "valid": "Ticket verified successfully. Allow entry.",
    "already_used": "This ticket has already been used. Do not allow entry again.",
    "invalid": "Ticket could not be verified. Do not allow entry.",
}
ASSIGNABLE_STATUSES = ("published", "completed")


def parse_scan(raw):
    """Return ("token", value), ("code", value), or (None, None) for unreadable input."""
    text = (raw or "").strip()
    if not text or len(text) > 512:
        return None, None
    match = VERIFY_URL_PATTERN.search(text)
    if match:
        return "token", match.group(1)
    if TOKEN_PATTERN.match(text):
        return "token", text
    code = text.upper()
    if CODE_PATTERN.match(code):
        return "code", code
    return None, None


def find_ticket(kind, value):
    if kind == "token":
        return Ticket.query.filter_by(qr_token=value).first()
    if kind == "code":
        return Ticket.query.filter_by(ticket_code=value).first()
    return None


def assignment_for(user, event_id):
    """The user's active assignment to an event, or None if they may not scan for it."""
    if user is None or not user.is_active_attendant:
        return None
    assignment = AttendantAssignment.query.filter_by(
        attendant_id=user.gate_profile.id, event_id=event_id, status="active"
    ).first()
    if assignment is None or assignment.event.organiser_id != assignment.attendant.created_by_id:
        return None
    return assignment


def assigned_events(user):
    if user is None or not user.is_active_attendant:
        return []
    return (
        Event.query.join(AttendantAssignment, AttendantAssignment.event_id == Event.id)
        .filter(
            AttendantAssignment.attendant_id == user.gate_profile.id,
            AttendantAssignment.status == "active",
            Event.organiser_id == user.gate_profile.created_by_id,
        )
        .order_by(Event.start_datetime.asc())
        .all()
    )


def verify_scan(user, event, raw, method):
    """Verify one scan for an event the user is assigned to. Returns a result dictionary."""
    method = method if method in {"camera", "manual"} else "manual"
    kind, value = parse_scan(raw)
    attempted = (raw or "").strip()[:40]
    ticket = find_ticket(kind, value)
    if ticket is None:
        reason = "unreadable" if kind is None else "not_found"
        entry = record_redemption(None, user, "invalid", reason, event, method, attempted)
        db.session.commit()
        return describe(entry)
    _, _, entry = redeem_for_entry(ticket, event, user, method, value)
    return describe(entry)


def describe(entry):
    """The scan result as shown to the attendant. Other events' ticket details stay hidden."""
    data = {
        "result": entry.result,
        "label": entry.result_label,
        "message": RESULT_MESSAGES[entry.result],
        "reason": entry.reason_text,
        "checked_at": entry.created_at,
        "ticket": None,
    }
    if entry.shows_ticket:
        ticket = entry.ticket
        data["ticket"] = {
            "code": ticket.ticket_code,
            "event": ticket.event.title,
            "category": ticket.ticket_type.name,
            "holder": ticket.client.full_name,
            "redeemed_at": ticket.redeemed_at,
        }
    return data


def event_stats(event):
    counts = dict(
        db.session.query(Ticket.status, func.count(Ticket.id))
        .filter(Ticket.event_id == event.id)
        .group_by(Ticket.status)
        .all()
    )
    rejected = TicketRedemption.query.filter(
        TicketRedemption.event_id == event.id,
        TicketRedemption.result.in_(("invalid", "already_used")),
    ).count()
    scanned = counts.get("redeemed", 0)
    remaining = counts.get("valid", 0)
    return {"expected": scanned + remaining, "scanned": scanned, "remaining": remaining, "rejected": rejected}


def recent_scans(event_ids, actor_id=None, limit=10):
    if not event_ids:
        return []
    query = TicketRedemption.query.filter(TicketRedemption.event_id.in_(event_ids))
    if actor_id is not None:
        query = query.filter(TicketRedemption.actor_id == actor_id)
    return query.order_by(TicketRedemption.created_at.desc(), TicketRedemption.id.desc()).limit(limit).all()


def temporary_password():
    """A readable one-time password that satisfies the account password rules."""
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower = "abcdefghijkmnopqrstuvwxyz"
    letters = secrets.choice(upper) + "".join(secrets.choice(lower) for _ in range(7))
    return f"{letters}{secrets.randbelow(9000) + 1000}"


def assignable_events(planner_id):
    return (
        Event.query.filter(
            Event.organiser_id == planner_id,
            Event.approval_status == "approved",
            Event.publication_status.in_(ASSIGNABLE_STATUSES),
        )
        .order_by(Event.start_datetime.desc())
        .all()
    )


def attendants_for(planner_id):
    return (
        GateAttendant.query.filter_by(created_by_id=planner_id)
        .order_by(GateAttendant.created_at.desc())
        .all()
    )


def filter_scans(query, args, event_ids, actor_ids=None):
    """Apply the shared scan filters (event, result, checker, local dates). Returns (query, filters, errors)."""
    filters = {
        "event_id": args.get("event_id", type=int),
        "result": args.get("result", ""),
        "actor_id": args.get("actor_id", type=int),
        "date_from": (args.get("date_from") or "").strip(),
        "date_to": (args.get("date_to") or "").strip(),
    }
    errors = []
    if filters["event_id"] in event_ids:
        query = query.filter(TicketRedemption.event_id == filters["event_id"])
    else:
        filters["event_id"] = None
    if filters["result"] in SCAN_RESULTS:
        query = query.filter(TicketRedemption.result == filters["result"])
    else:
        filters["result"] = ""
    if filters["actor_id"] and (actor_ids is None or filters["actor_id"] in actor_ids):
        query = query.filter(TicketRedemption.actor_id == filters["actor_id"])
    else:
        filters["actor_id"] = None
    bounds = {}
    for key, shift in (("date_from", 0), ("date_to", 1)):
        if not filters[key]:
            continue
        try:
            day = datetime.strptime(filters[key], "%Y-%m-%d") + timedelta(days=shift)
        except ValueError:
            errors.append("Enter dates as YYYY-MM-DD.")
            filters[key] = ""
            continue
        bounds[key] = day.replace(tzinfo=LUSAKA).astimezone(timezone.utc).replace(tzinfo=None)
    if "date_from" in bounds and "date_to" in bounds and bounds["date_from"] >= bounds["date_to"]:
        errors.append("The start date must be before the end date.")
    else:
        if "date_from" in bounds:
            query = query.filter(TicketRedemption.created_at >= bounds["date_from"])
        if "date_to" in bounds:
            query = query.filter(TicketRedemption.created_at < bounds["date_to"])
    return query, filters, errors


def scan_counts(query):
    rows = dict(query.with_entities(TicketRedemption.result, func.count(TicketRedemption.id)).group_by(TicketRedemption.result).all())
    return {key: rows.get(key, 0) for key in SCAN_RESULTS}


def repeated_scans(query, minimum=2):
    """Tickets rejected as already used at least `minimum` times, newest first."""
    since = utcnow_naive() - timedelta(days=30)
    rows = (
        query.filter(
            TicketRedemption.result == "already_used",
            TicketRedemption.ticket_id.isnot(None),
            TicketRedemption.created_at >= since,
        )
        .with_entities(
            TicketRedemption.ticket_id,
            func.count(TicketRedemption.id).label("attempts"),
            func.count(func.distinct(TicketRedemption.actor_id)).label("attendants"),
            func.max(TicketRedemption.created_at).label("last_attempt"),
        )
        .group_by(TicketRedemption.ticket_id)
        .having(func.count(TicketRedemption.id) >= minimum)
        .order_by(func.max(TicketRedemption.created_at).desc())
        .limit(20)
        .all()
    )
    tickets = {ticket.id: ticket for ticket in Ticket.query.filter(Ticket.id.in_([row.ticket_id for row in rows])).all()}
    return [
        {"ticket": tickets[row.ticket_id], "attempts": row.attempts, "attendants": row.attendants, "last_attempt": row.last_attempt}
        for row in rows
        if row.ticket_id in tickets
    ]
