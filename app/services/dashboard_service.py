"""Role dashboards built only from database records."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import (
    AuditLog,
    ContactMessage,
    Event,
    EventCategory,
    Notification,
    Order,
    Payment,
    PlannerBooking,
    PlannerProfile,
    Refund,
    Ticket,
    TicketType,
    User,
)
from app.models.database import utcnow
from app.services.ticket_service import sales_label
from app.utils.helpers import LUSAKA, to_local

ISSUED_TICKET_STATUSES = ("valid", "redeemed")
CONFIRMED_BOOKING_STATUSES = ("accepted", "in_progress", "completed")
OPEN_BOOKING_STATUSES = ("pending", "accepted", "in_progress")
AVAILABILITY_ORDER = ("available", "not_open", "closed", "sold_out", "ended", "cancelled", "unavailable")


def _recent_months(count=6):
    """Month keys and labels for the last `count` Lusaka calendar months, plus the UTC start of the first."""
    local_now = to_local(utcnow())
    year, month = local_now.year, local_now.month
    months = []
    for _ in range(count):
        months.append((year, month))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    months.reverse()
    first_year, first_month = months[0]
    start_local = datetime(first_year, first_month, 1, tzinfo=LUSAKA)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    keys = [f"{y:04d}-{m:02d}" for y, m in months]
    labels = [datetime(y, m, 1).strftime("%b %Y") for y, m in months]
    return keys, labels, start_utc


def _bucket(values, keys):
    counts = dict.fromkeys(keys, 0)
    for value in values:
        local = to_local(value)
        if local is None:
            continue
        key = local.strftime("%Y-%m")
        if key in counts:
            counts[key] += 1
    return [counts[key] for key in keys]


def event_availability(event, now=None):
    now = now or utcnow()
    labels = {sales_label(event, ticket_type, now) for ticket_type in event.ticket_types}
    if not labels:
        return "unavailable"
    for label in AVAILABILITY_ORDER:
        if label in labels:
            return label
    return "unavailable"


def _discover_events(now, limit=6):
    events = (
        Event.query.options(joinedload(Event.ticket_types), joinedload(Event.category))
        .filter(
            Event.approval_status == "approved",
            Event.publication_status == "published",
            Event.end_datetime >= now,
        )
        .order_by(Event.start_datetime.asc())
        .limit(limit)
        .all()
    )
    cards = []
    for event in events:
        prices = [ticket_type.price for ticket_type in event.ticket_types]
        cards.append(
            {
                "event": event,
                "availability": event_availability(event, now),
                "min_price": min(prices) if prices else None,
                "max_price": max(prices) if prices else None,
                "ticket_names": [ticket_type.name for ticket_type in event.ticket_types],
            }
        )
    return cards


def client_overview(user):
    now = utcnow()
    upcoming_filter = (
        Ticket.client_id == user.id,
        Ticket.status == "valid",
        Event.end_datetime >= now,
        Event.publication_status != "cancelled",
    )
    upcoming_tickets = (
        Ticket.query.options(joinedload(Ticket.event), joinedload(Ticket.ticket_type))
        .join(Event, Ticket.event_id == Event.id)
        .filter(*upcoming_filter)
        .order_by(Event.start_datetime.asc(), Ticket.id.asc())
        .limit(6)
        .all()
    )
    upcoming_events = (
        db.session.query(func.count(func.distinct(Ticket.event_id)))
        .join(Event, Ticket.event_id == Event.id)
        .filter(*upcoming_filter)
        .scalar()
        or 0
    )
    booking_counts = dict(
        db.session.query(PlannerBooking.status, func.count(PlannerBooking.id))
        .filter(PlannerBooking.client_id == user.id)
        .group_by(PlannerBooking.status)
        .all()
    )
    orders = (
        Order.query.options(joinedload(Order.event))
        .filter_by(client_id=user.id)
        .order_by(Order.created_at.desc())
        .limit(5)
        .all()
    )
    bookings = (
        PlannerBooking.query.options(joinedload(PlannerBooking.planner), joinedload(PlannerBooking.quotations))
        .filter_by(client_id=user.id)
        .order_by(PlannerBooking.created_at.desc())
        .limit(5)
        .all()
    )
    notifications = (
        Notification.query.filter_by(user_id=user.id).order_by(Notification.created_at.desc()).limit(8).all()
    )
    activity = [{"kind": "order", "when": order.created_at, "item": order} for order in orders]
    activity += [{"kind": "booking", "when": booking.created_at, "item": booking} for booking in bookings]
    activity += [{"kind": "notification", "when": item.created_at, "item": item} for item in notifications]
    activity.sort(key=lambda row: row["when"], reverse=True)

    stats = {
        "tickets": Ticket.query.filter_by(client_id=user.id).count(),
        "valid_tickets": Ticket.query.filter_by(client_id=user.id, status="valid").count(),
        "upcoming_events": int(upcoming_events),
        "active_bookings": sum(booking_counts.get(status, 0) for status in OPEN_BOOKING_STATUSES),
        "pending_bookings": booking_counts.get("pending", 0),
        "unread": Notification.query.filter_by(user_id=user.id, is_read=False).count(),
        "pending_orders": Order.query.filter_by(client_id=user.id, order_status="pending").count(),
    }
    return {
        "stats": stats,
        "upcoming_tickets": upcoming_tickets,
        "discover": _discover_events(now),
        "orders": orders,
        "bookings": bookings,
        "activity": activity[:8],
    }


def planner_overview(user, profile):
    now = utcnow()
    today = to_local(now).date()
    counts = dict(
        db.session.query(PlannerBooking.status, func.count(PlannerBooking.id))
        .filter(PlannerBooking.planner_id == profile.id)
        .group_by(PlannerBooking.status)
        .all()
    )
    pending_requests = (
        PlannerBooking.query.options(joinedload(PlannerBooking.client), joinedload(PlannerBooking.quotations))
        .filter(PlannerBooking.planner_id == profile.id, PlannerBooking.status == "pending")
        .order_by(PlannerBooking.created_at.asc())
        .limit(8)
        .all()
    )
    booking_filter = (
        PlannerBooking.planner_id == profile.id,
        PlannerBooking.status.in_(("accepted", "in_progress")),
        PlannerBooking.event_date >= today,
    )
    event_filter = (
        Event.organiser_id == user.id,
        Event.approval_status == "approved",
        Event.publication_status == "published",
        Event.end_datetime >= now,
    )
    upcoming_bookings = (
        PlannerBooking.query.options(joinedload(PlannerBooking.client))
        .filter(*booking_filter)
        .order_by(PlannerBooking.event_date, PlannerBooking.start_time)
        .limit(6)
        .all()
    )
    upcoming_events = Event.query.filter(*event_filter).order_by(Event.start_datetime).limit(6).all()
    schedule = [
        {"kind": "booking", "when": datetime.combine(item.event_date, item.start_time), "item": item}
        for item in upcoming_bookings
    ]
    schedule += [
        {"kind": "event", "when": to_local(item.start_datetime).replace(tzinfo=None), "item": item}
        for item in upcoming_events
    ]
    schedule.sort(key=lambda row: row["when"])

    keys, labels, start = _recent_months()
    recent = (
        db.session.query(PlannerBooking.created_at, PlannerBooking.status)
        .filter(PlannerBooking.planner_id == profile.id, PlannerBooking.created_at >= start)
        .all()
    )
    requests_series = _bucket([row[0] for row in recent], keys)
    confirmed_series = _bucket([row[0] for row in recent if row[1] in CONFIRMED_BOOKING_STATUSES], keys)

    revenue = dict(
        db.session.query(Payment.is_simulated, func.coalesce(func.sum(Payment.amount), 0))
        .join(Order, Payment.order_id == Order.id)
        .join(Event, Order.event_id == Event.id)
        .filter(Event.organiser_id == user.id, Payment.status == "successful")
        .group_by(Payment.is_simulated)
        .all()
    )
    tickets_sold = (
        Ticket.query.join(Event, Ticket.event_id == Event.id)
        .filter(Event.organiser_id == user.id, Ticket.status.in_(ISSUED_TICKET_STATUSES))
        .count()
    )
    stats = {
        "total": sum(counts.values()),
        "pending": counts.get("pending", 0),
        "confirmed": sum(counts.get(status, 0) for status in CONFIRMED_BOOKING_STATUSES),
        "completed": counts.get("completed", 0),
        "upcoming_bookings": PlannerBooking.query.filter(*booking_filter).count(),
        "upcoming_events": Event.query.filter(*event_filter).count(),
        "events": Event.query.filter_by(organiser_id=user.id).count(),
        "services": len(profile.services),
        "tickets_sold": tickets_sold,
        "revenue_live": int(revenue.get(False, 0) or 0),
        "revenue_simulated": int(revenue.get(True, 0) or 0),
    }
    stats["upcoming"] = stats["upcoming_bookings"] + stats["upcoming_events"]
    chart = {
        "id": "planner-bookings-chart",
        "type": "bar",
        "labels": labels,
        "datasets": [
            {"label": "Requests received", "data": requests_series, "color": "green"},
            {"label": "Confirmed", "data": confirmed_series, "color": "copper"},
        ],
    }
    return {
        "stats": stats,
        "pending_requests": pending_requests,
        "schedule": schedule[:6],
        "charts": [chart],
        "missing": profile.missing_profile_items,
    }


def _performance_tag(row, now):
    capacity, sold, event = row["capacity"], row["sold"], row["event"]
    if capacity <= 0:
        return None
    if sold >= capacity:
        return "sold_out"
    rate = sold / capacity
    if rate >= 0.75:
        return "selling_fast"
    if now - event.sales_start >= timedelta(days=7) and now <= event.sales_end and rate < 0.10:
        return "low_sales"
    return None


def admin_overview():
    now = utcnow()
    planner_status = dict(
        db.session.query(PlannerProfile.approval_status, func.count(PlannerProfile.id))
        .group_by(PlannerProfile.approval_status)
        .all()
    )
    payment_status = dict(
        db.session.query(Payment.status, func.count(Payment.id)).group_by(Payment.status).all()
    )
    successful = dict(
        db.session.query(Payment.is_simulated, func.coalesce(func.sum(Payment.amount), 0))
        .filter(Payment.status == "successful")
        .group_by(Payment.is_simulated)
        .all()
    )
    upcoming_filter = (
        Event.approval_status == "approved",
        Event.publication_status == "published",
        Event.end_datetime >= now,
    )
    stats = {
        "clients": User.query.filter_by(role="client").count(),
        "planners": User.query.filter_by(role="planner").count(),
        "planners_verified": planner_status.get("approved", 0),
        "planners_pending": planner_status.get("pending", 0),
        "planners_other": planner_status.get("rejected", 0) + planner_status.get("suspended", 0),
        "events_pending": Event.query.filter_by(approval_status="pending").count(),
        "events_upcoming": Event.query.filter(*upcoming_filter).count(),
        "tickets_sold": Ticket.query.filter(Ticket.status.in_(ISSUED_TICKET_STATUSES)).count(),
        "orders": Order.query.count(),
        "orders_paid": Order.query.filter_by(order_status="paid").count(),
        "payments_successful": payment_status.get("successful", 0),
        "payments_pending": payment_status.get("pending", 0),
        "payments_failed": payment_status.get("failed", 0),
        "payments_refunded": payment_status.get("refunded", 0),
        "revenue_live": int(successful.get(False, 0) or 0),
        "revenue_simulated": int(successful.get(True, 0) or 0),
        "refunds_requested": Refund.query.filter_by(status="requested").count(),
        "new_messages": ContactMessage.query.filter_by(status="new").count(),
        "bookings": PlannerBooking.query.count(),
    }
    stats["tasks"] = (
        stats["planners_pending"] + stats["events_pending"] + stats["refunds_requested"] + stats["new_messages"]
    )

    pending_planners = (
        PlannerProfile.query.options(joinedload(PlannerProfile.user))
        .filter_by(approval_status="pending")
        .order_by(PlannerProfile.created_at.asc())
        .limit(6)
        .all()
    )
    pending_events = (
        Event.query.options(joinedload(Event.organiser), joinedload(Event.category))
        .filter_by(approval_status="pending")
        .order_by(Event.updated_at.asc())
        .limit(6)
        .all()
    )

    performance_events = Event.query.filter(*upcoming_filter).order_by(Event.start_datetime.asc()).limit(8).all()
    totals = {}
    if performance_events:
        totals = {
            event_id: (int(capacity or 0), int(sold or 0))
            for event_id, capacity, sold in db.session.query(
                TicketType.event_id,
                func.sum(TicketType.quantity_total),
                func.sum(TicketType.quantity_sold),
            )
            .filter(TicketType.event_id.in_([event.id for event in performance_events]))
            .group_by(TicketType.event_id)
            .all()
        }
    performance = []
    for event in performance_events:
        capacity, sold = totals.get(event.id, (0, 0))
        row = {
            "event": event,
            "capacity": capacity,
            "sold": sold,
            "percent": int(round(100 * sold / capacity)) if capacity else 0,
            "availability": event_availability(event, now),
        }
        row["tag"] = _performance_tag(row, now)
        performance.append(row)

    keys, labels, start = _recent_months()
    issued = [
        row[0]
        for row in db.session.query(Ticket.issued_at)
        .filter(Ticket.status.in_(ISSUED_TICKET_STATUSES), Ticket.issued_at >= start)
        .all()
    ]
    by_category = (
        db.session.query(EventCategory.name, func.count(Ticket.id))
        .join(Event, Event.category_id == EventCategory.id)
        .join(Ticket, Ticket.event_id == Event.id)
        .filter(Ticket.status.in_(ISSUED_TICKET_STATUSES))
        .group_by(EventCategory.id, EventCategory.name)
        .order_by(func.count(Ticket.id).desc())
        .limit(8)
        .all()
    )
    by_event = (
        db.session.query(Event.title, func.count(Ticket.id))
        .join(Ticket, Ticket.event_id == Event.id)
        .filter(Ticket.status.in_(ISSUED_TICKET_STATUSES))
        .group_by(Event.id, Event.title)
        .order_by(func.count(Ticket.id).desc())
        .limit(6)
        .all()
    )
    charts = [
        {
            "id": "admin-monthly-chart",
            "type": "bar",
            "labels": labels,
            "datasets": [{"label": "Tickets issued", "data": _bucket(issued, keys), "color": "green"}],
        },
        {
            "id": "admin-category-chart",
            "type": "bar",
            "horizontal": True,
            "labels": [name for name, _ in by_category],
            "datasets": [{"label": "Tickets issued", "data": [count for _, count in by_category], "color": "copper"}],
        },
        {
            "id": "admin-event-chart",
            "type": "bar",
            "horizontal": True,
            "labels": [title for title, _ in by_event],
            "datasets": [{"label": "Tickets issued", "data": [count for _, count in by_event], "color": "green"}],
        },
        {
            "id": "admin-payment-chart",
            "type": "doughnut",
            "labels": ["Successful", "Pending", "Failed", "Refunded"],
            "datasets": [
                {
                    "label": "Payments",
                    "data": [
                        stats["payments_successful"],
                        stats["payments_pending"],
                        stats["payments_failed"],
                        stats["payments_refunded"],
                    ],
                    "colors": ["green", "gold", "red", "slate"],
                }
            ],
        },
    ]

    logs = AuditLog.query.options(joinedload(AuditLog.actor)).order_by(AuditLog.created_at.desc()).limit(10).all()
    users = User.query.filter(User.role.in_(("client", "planner"))).order_by(User.created_at.desc()).limit(10).all()
    activity = [{"kind": "audit", "when": log.created_at, "item": log} for log in logs]
    activity += [{"kind": "registration", "when": user.created_at, "item": user} for user in users]
    activity.sort(key=lambda row: row["when"], reverse=True)

    return {
        "stats": stats,
        "pending_planners": pending_planners,
        "pending_events": pending_events,
        "performance": performance,
        "charts": charts,
        "activity": activity[:10],
        "today": to_local(now),
    }
