"""Planner and platform performance reports built only from database records.

Earned revenue comes only from ticket payments a provider verified as successful,
including ones that were later refunded; a completed refund is subtracted in the
period it was completed. Commission is split per transaction so totals agree
however they are grouped (by event, by month, or by planner).
"""

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from flask import current_app
from sqlalchemy import func
from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db
from app.models import (
    Event,
    Order,
    OrderItem,
    Payment,
    PlannerBooking,
    PlannerPayout,
    PlannerProfile,
    Refund,
    Ticket,
    TicketType,
    User,
)
from app.models.database import utcnow
from app.utils.helpers import LUSAKA, format_zmw, to_local

PERIODS = {
    "today": "Today",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "3m": "Last 3 months",
    "6m": "Last 6 months",
    "year": "This year",
    "all": "All time",
    "custom": "Custom range",
}
TRANSACTION_TYPES = {"all": "All transactions", "ticket_sale": "Ticket sales", "refund": "Refunds", "payout": "Payouts"}
EARNED_PAYMENT_STATUSES = ("successful", "refunded")
ISSUED_TICKET_STATUSES = ("valid", "redeemed")
BOOKING_STATUSES = ("pending", "accepted", "rejected", "in_progress", "completed", "cancelled")
ACCEPTED_BOOKING_STATUSES = ("accepted", "in_progress", "completed")
EVENT_STATUS_LABELS = {
    "draft": "Draft",
    "pending": "Pending approval",
    "published": "Published",
    "rejected": "Rejected",
    "completed": "Completed",
    "cancelled": "Cancelled",
    "sold_out": "Sold out",
    "suspended": "Suspended",
    "unpublished": "Not published",
}
CAPACITY_WARNING = 0.8
LOW_SALES_RATE = 0.10
LOW_SALES_AFTER = timedelta(days=7)
MAX_MONTH_BUCKETS = 60


def _utc(local_day):
    return datetime.combine(local_day, time.min, tzinfo=LUSAKA).astimezone(timezone.utc).replace(tzinfo=None)


def _months_back(day, months):
    index = day.year * 12 + day.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


@dataclass
class Period:
    key: str
    label: str
    start: datetime | None
    end: datetime | None
    date_from: str = ""
    date_to: str = ""

    @property
    def bounded(self):
        return self.start is not None and self.end is not None

    @property
    def previous(self):
        if not self.bounded:
            return None
        span = self.end - self.start
        return self.start - span, self.start

    @property
    def bucket(self):
        return "day" if self.bounded and self.end - self.start <= timedelta(days=62) else "month"

    @property
    def first_day(self):
        return to_local(self.start).date() if self.start else None

    @property
    def last_day(self):
        return (to_local(self.end) - timedelta(days=1)).date() if self.end else None

    @property
    def range_text(self):
        if self.start is None and self.end is None:
            return "All recorded activity"
        first = self.first_day.strftime("%d %b %Y") if self.start else "the first record"
        last = self.last_day.strftime("%d %b %Y") if self.end else "today"
        return first if first == last else f"{first} to {last}"


def _all_time():
    return Period("all", PERIODS["all"], None, None)


def resolve_period(args):
    """Turn the period filter in the query string into UTC bounds. Returns (period, error)."""
    key = args.get("period") or "all"
    if key not in PERIODS:
        return _all_time(), "That time period is not available, so all recorded activity is shown."
    today = to_local(utcnow()).date()
    tomorrow = today + timedelta(days=1)
    starts = {
        "today": today,
        "7d": today - timedelta(days=6),
        "30d": today - timedelta(days=29),
        "3m": _months_back(today, 3),
        "6m": _months_back(today, 6),
        "year": date(today.year, 1, 1),
    }
    if key in starts:
        first = starts[key]
        return Period(key, PERIODS[key], _utc(first), _utc(tomorrow), first.isoformat(), today.isoformat()), None
    if key == "all":
        return _all_time(), None

    raw_from = (args.get("date_from") or "").strip()
    raw_to = (args.get("date_to") or "").strip()
    try:
        first = date.fromisoformat(raw_from) if raw_from else None
        last = date.fromisoformat(raw_to) if raw_to else None
    except ValueError:
        return _all_time(), "Enter the custom dates as a valid day, month and year."
    if first is None and last is None:
        return _all_time(), "Choose a start date, an end date, or both for a custom range."
    if first and last and first > last:
        return _all_time(), "The start date must be on or before the end date."
    if first and last is None:
        last = max(today, first)
    period = Period(
        "custom",
        PERIODS["custom"],
        _utc(first) if first else None,
        _utc(last + timedelta(days=1)),
        first.isoformat() if first else "",
        last.isoformat(),
    )
    return period, None


def resolve_transaction_type(args):
    value = args.get("type") or "all"
    return value if value in TRANSACTION_TYPES else "all"


def commission_rate():
    return int(current_app.config.get("PLATFORM_COMMISSION_PERCENT", 15))


def split_amount(amount, rate=None):
    """Platform commission and planner share for one transaction, rounded half up to the ngwee."""
    rate = commission_rate() if rate is None else rate
    commission = (amount * rate + 50) // 100
    return commission, amount - commission


def kwacha(ngwee):
    return round((ngwee or 0) / 100, 2)


@dataclass
class Scope:
    organiser_id: int | None = None
    planner_id: int | None = None
    event_id: int | None = None


def _within(query, column, start, end):
    if start is not None:
        query = query.filter(column >= start)
    if end is not None:
        query = query.filter(column < end)
    return query


def _event_scope(query, scope):
    if scope.organiser_id is not None:
        query = query.filter(Event.organiser_id == scope.organiser_id)
    if scope.event_id is not None:
        query = query.filter(Event.id == scope.event_id)
    return query


def _sales(scope, start, end):
    query = (
        db.session.query(
            Payment.payment_reference.label("reference"),
            Payment.amount,
            Payment.status,
            Payment.is_simulated,
            Payment.payment_method,
            Payment.verified_at,
            Order.order_reference,
            Event.id.label("event_id"),
            Event.title.label("event_title"),
            Event.organiser_id,
            User.full_name.label("client_name"),
            PlannerProfile.business_name.label("planner_name"),
        )
        .join(Order, Payment.order_id == Order.id)
        .join(Event, Order.event_id == Event.id)
        .join(User, Order.client_id == User.id)
        .outerjoin(PlannerProfile, PlannerProfile.user_id == Event.organiser_id)
        .filter(Payment.status.in_(EARNED_PAYMENT_STATUSES), Payment.verified_at.isnot(None))
    )
    rows = _within(_event_scope(query, scope), Payment.verified_at, start, end).order_by(Payment.verified_at).all()
    rate = commission_rate()
    sales = []
    for row in rows:
        commission, share = split_amount(row.amount, rate)
        sales.append(
            {
                "kind": "ticket_sale",
                "when": row.verified_at,
                "reference": row.reference,
                "detail": f"Order {row.order_reference} · {row.event_title}",
                "event_id": row.event_id,
                "event_title": row.event_title,
                "organiser_id": row.organiser_id,
                "planner_name": row.planner_name or "",
                "client_name": row.client_name,
                "gross": row.amount,
                "commission": commission,
                "net": share,
                "status": row.status,
                "simulated": bool(row.is_simulated),
                "method": row.payment_method,
            }
        )
    return sales


def _refunds(scope, start, end):
    query = (
        db.session.query(
            Refund.amount,
            Refund.updated_at.label("when"),
            Refund.order_id,
            Payment.payment_reference.label("reference"),
            Payment.is_simulated,
            Order.order_reference,
            Event.id.label("event_id"),
            Event.title.label("event_title"),
            Event.organiser_id,
            User.full_name.label("client_name"),
            PlannerProfile.business_name.label("planner_name"),
        )
        .join(Payment, Refund.payment_id == Payment.id)
        .join(Order, Refund.order_id == Order.id)
        .join(Event, Order.event_id == Event.id)
        .join(User, Order.client_id == User.id)
        .outerjoin(PlannerProfile, PlannerProfile.user_id == Event.organiser_id)
        .filter(Refund.status == "completed")
    )
    rows = _within(_event_scope(query, scope), Refund.updated_at, start, end).order_by(Refund.updated_at).all()
    rate = commission_rate()
    refunds = []
    for row in rows:
        commission, share = split_amount(row.amount, rate)
        refunds.append(
            {
                "kind": "refund",
                "when": row.when,
                "reference": row.reference,
                "detail": f"Refund of order {row.order_reference} · {row.event_title}",
                "order_id": row.order_id,
                "event_id": row.event_id,
                "event_title": row.event_title,
                "organiser_id": row.organiser_id,
                "planner_name": row.planner_name or "",
                "client_name": row.client_name,
                "gross": -row.amount,
                "commission": -commission,
                "net": -share,
                "status": "completed",
                "simulated": bool(row.is_simulated),
                "method": None,
            }
        )
    return refunds


def _payouts(planner_id, start, end):
    query = PlannerPayout.query.options(joinedload(PlannerPayout.planner))
    if planner_id is not None:
        query = query.filter(PlannerPayout.planner_id == planner_id)
    rows = _within(query, PlannerPayout.created_at, start, end).order_by(PlannerPayout.created_at).all()
    return [
        {
            "kind": "payout",
            "when": payout.created_at,
            "reference": payout.payout_reference,
            "detail": f"{payout.method_label} · ref {payout.external_reference}",
            "event_id": None,
            "event_title": None,
            "organiser_id": payout.planner.user_id if payout.planner else None,
            "planner_id": payout.planner_id,
            "planner_name": payout.planner.business_name if payout.planner else "",
            "client_name": None,
            "gross": None,
            "commission": None,
            "net": None,
            "amount": payout.amount,
            "status": "completed",
            "simulated": False,
            "method": payout.method,
            "method_label": payout.method_label,
            "external_reference": payout.external_reference,
            "note": payout.note,
        }
        for payout in rows
    ]


def _issued_tickets(scope, start, end):
    query = (
        db.session.query(
            Ticket.issued_at,
            Ticket.event_id,
            Event.organiser_id,
            TicketType.id.label("ticket_type_id"),
            TicketType.name.label("category"),
            OrderItem.unit_price,
        )
        .join(Event, Ticket.event_id == Event.id)
        .join(TicketType, Ticket.ticket_type_id == TicketType.id)
        .join(OrderItem, Ticket.order_item_id == OrderItem.id)
        .filter(Ticket.status.in_(ISSUED_TICKET_STATUSES))
    )
    return _within(_event_scope(query, scope), Ticket.issued_at, start, end).all()


def _refunded_ticket_count(refunds):
    order_ids = {refund["order_id"] for refund in refunds}
    if not order_ids:
        return 0
    return (
        Ticket.query.join(OrderItem, Ticket.order_item_id == OrderItem.id)
        .filter(OrderItem.order_id.in_(order_ids), Ticket.status == "refunded")
        .count()
    )


def _unearned_payments(scope, start, end):
    query = (
        db.session.query(Payment.status, func.count(Payment.id), func.coalesce(func.sum(Payment.amount), 0))
        .join(Order, Payment.order_id == Order.id)
        .join(Event, Order.event_id == Event.id)
        .filter(Payment.status.in_(("pending", "failed")))
    )
    rows = _within(_event_scope(query, scope), Payment.created_at, start, end).group_by(Payment.status).all()
    found = {status: (int(count), int(total or 0)) for status, count, total in rows}
    return {status: found.get(status, (0, 0)) for status in ("pending", "failed")}


def _bookings(scope, start, end):
    if scope.event_id is not None:
        return []
    query = PlannerBooking.query.options(
        joinedload(PlannerBooking.client),
        joinedload(PlannerBooking.planner),
        selectinload(PlannerBooking.quotations),
    )
    if scope.planner_id is not None:
        query = query.filter(PlannerBooking.planner_id == scope.planner_id)
    return _within(query, PlannerBooking.created_at, start, end).order_by(PlannerBooking.created_at.desc()).all()


def _money(sales, refunds):
    gross = sum(row["gross"] for row in sales)
    refunded = -sum(row["gross"] for row in refunds)
    return {
        "gross": gross,
        "refunds": refunded,
        "net_revenue": gross - refunded,
        "commission": sum(row["commission"] for row in sales) + sum(row["commission"] for row in refunds),
        "earnings": sum(row["net"] for row in sales) + sum(row["net"] for row in refunds),
    }


def event_status(event, capacity, sold, now):
    if event.approval_status in ("draft", "pending", "rejected"):
        return event.approval_status
    if event.publication_status == "cancelled":
        return "cancelled"
    if event.publication_status == "completed" or event.end_datetime < now:
        return "completed"
    if event.publication_status == "suspended":
        return "suspended"
    if event.publication_status != "published":
        return "unpublished"
    if capacity and sold >= capacity:
        return "sold_out"
    return "published"


def _event_rows(scope, sales, refunds, issued, now):
    events = (
        _event_scope(Event.query.options(joinedload(Event.category), selectinload(Event.ticket_types)), scope)
        .order_by(Event.start_datetime.desc())
        .all()
    )
    sold_period = {}
    for ticket in issued:
        sold_period[ticket.event_id] = sold_period.get(ticket.event_id, 0) + 1
    rows = []
    for event in events:
        capacity = sum(item.quantity_total for item in event.ticket_types)
        sold_all = sum(item.quantity_sold for item in event.ticket_types)
        reserved = sum(item.quantity_reserved for item in event.ticket_types)
        money = _money(
            [row for row in sales if row["event_id"] == event.id],
            [row for row in refunds if row["event_id"] == event.id],
        )
        status = event_status(event, capacity, sold_all, now)
        rows.append(
            {
                "event": event,
                "status": status,
                "status_label": EVENT_STATUS_LABELS[status],
                "capacity": capacity,
                "sold_all": sold_all,
                "reserved": reserved,
                "remaining": max(capacity - sold_all - reserved, 0),
                "sold": sold_period.get(event.id, 0),
                "sell_through": round(100 * sold_all / capacity) if capacity else 0,
                **money,
            }
        )
    return rows


def _event_detail(row, issued):
    event = row["event"]
    by_type = {}
    for ticket in issued:
        if ticket.event_id == event.id:
            count, revenue = by_type.get(ticket.ticket_type_id, (0, 0))
            by_type[ticket.ticket_type_id] = (count + 1, revenue + ticket.unit_price)
    types = []
    for item in event.ticket_types:
        count, revenue = by_type.get(item.id, (0, 0))
        types.append(
            {
                "name": item.name,
                "price": item.price,
                "total": item.quantity_total,
                "sold_all": item.quantity_sold,
                "reserved": item.quantity_reserved,
                "remaining": item.remaining,
                "sold": count,
                "revenue": revenue,
                "sell_through": round(100 * item.quantity_sold / item.quantity_total) if item.quantity_total else 0,
            }
        )
    return {**row, "types": types}


def _bucket_key(moment, bucket):
    local = to_local(moment)
    return local.strftime("%Y-%m-%d") if bucket == "day" else local.strftime("%Y-%m")


def _series_axis(period, moments):
    """Bucket keys and labels covering the period, by day for short ranges and by month otherwise."""
    today = to_local(utcnow()).date()
    if period.bucket == "day":
        first, last = period.first_day, period.last_day
        days = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
        return [day.isoformat() for day in days], [day.strftime("%d %b") for day in days]
    local_days = [to_local(moment).date() for moment in moments if moment is not None]
    first = period.first_day or (min(local_days) if local_days else today)
    last = period.last_day or max([today, *local_days])
    keys, labels = [], []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        keys.append(f"{year:04d}-{month:02d}")
        labels.append(date(year, month, 1).strftime("%b %Y"))
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return keys[-MAX_MONTH_BUCKETS:], labels[-MAX_MONTH_BUCKETS:]


def _sum_by_bucket(rows, keys, bucket, value):
    totals = dict.fromkeys(keys, 0)
    for row in rows:
        key = _bucket_key(row["when"], bucket)
        if key in totals:
            totals[key] += value(row)
    return [totals[key] for key in keys]


def _count_by_bucket(moments, keys, bucket):
    totals = dict.fromkeys(keys, 0)
    for moment in moments:
        key = _bucket_key(moment, bucket)
        if key in totals:
            totals[key] += 1
    return [totals[key] for key in keys]


def _best_month(sales, refunds):
    months = {}
    for row in sales + refunds:
        key = to_local(row["when"]).strftime("%Y-%m")
        months[key] = months.get(key, 0) + row["net"]
    if not months:
        return None
    key, amount = max(months.items(), key=lambda item: (item[1], item[0]))
    if amount <= 0:
        return None
    year, month = (int(part) for part in key.split("-"))
    return {"label": date(year, month, 1).strftime("%B %Y"), "amount": amount}


def _growth(scope, period, current_net):
    previous = period.previous
    if previous is None:
        return None
    before = _money(_sales(scope, *previous), _refunds(scope, *previous))["net_revenue"]
    percent = round(100 * (current_net - before) / before, 1) if before > 0 else None
    return {"current": current_net, "previous": before, "percent": percent}


def _booking_rows(bookings):
    rows = []
    for booking in bookings:
        quotes = booking.quotations
        accepted = next((quote for quote in reversed(quotes) if quote.status == "accepted"), None)
        latest = quotes[-1] if quotes else None
        if accepted:
            payment_status, payment_label = "outside", "Agreed, not paid through EventLink"
        elif latest is not None and latest.status == "pending":
            payment_status, payment_label = "quoted", "Quotation awaiting client"
        elif latest is not None:
            payment_status, payment_label = "declined", "Quotation declined"
        else:
            payment_status, payment_label = "none", "No quotation yet"
        rows.append(
            {
                "booking": booking,
                "reference": f"BKG-{booking.id:05d}",
                "client_name": booking.client.full_name if booking.client else "",
                "planner_name": booking.planner.business_name if booking.planner else "",
                "service": booking.event_type,
                "event_date": booking.event_date,
                "requested": booking.created_at,
                "agreed_amount": accepted.amount if accepted else (latest.amount if latest else None),
                "payment_status": payment_status,
                "payment_label": payment_label,
                "status": booking.status,
            }
        )
    return rows


def _booking_summary(booking_rows):
    counts = dict.fromkeys(BOOKING_STATUSES, 0)
    for row in booking_rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    accepted = sum(counts[status] for status in ACCEPTED_BOOKING_STATUSES)
    decided = accepted + counts["rejected"]
    agreed = sum(
        row["agreed_amount"] for row in booking_rows
        if row["payment_status"] == "outside" and row["status"] in ACCEPTED_BOOKING_STATUSES
    )
    # Booking payments are agreed between planner and client and are never paid through EventLink,
    # so there are no verified booking payments to earn commission on.
    revenue = 0
    commission, earnings = split_amount(revenue)
    return {
        "total": len(booking_rows),
        "counts": counts,
        "accepted": accepted,
        "completed": counts["completed"],
        "cancelled": counts["cancelled"],
        "rejected": counts["rejected"],
        "pending": counts["pending"],
        "agreed_value": agreed,
        "revenue": revenue,
        "commission": commission,
        "earnings": earnings,
        "acceptance_rate": round(100 * accepted / decided) if decided else None,
        "completion_rate": round(100 * counts["completed"] / accepted) if accepted else None,
    }


def _rate_text(value):
    return "n/a" if value is None else f"{value}%"


def _insights(event_rows, sales, refunds, growth, bookings, now):
    insights = []

    by_tickets = max((row for row in event_rows if row["sold"] > 0), key=lambda row: row["sold"], default=None)
    insights.append(
        {
            "icon": "ticket-perforated",
            "label": "Highest ticket sales",
            "value": by_tickets["event"].title if by_tickets else "No ticket sales",
            "detail": f"{by_tickets['sold']} ticket{'s' if by_tickets['sold'] != 1 else ''} sold in this period"
            if by_tickets else "No tickets were issued in this period.",
        }
    )
    by_revenue = max((row for row in event_rows if row["net_revenue"] > 0), key=lambda row: row["net_revenue"], default=None)
    insights.append(
        {
            "icon": "cash-stack",
            "label": "Most revenue",
            "value": by_revenue["event"].title if by_revenue else "No revenue yet",
            "detail": f"{format_zmw(by_revenue['net_revenue'])} net revenue in this period"
            if by_revenue else "No verified payments in this period.",
        }
    )
    best = _best_month(sales, refunds)
    insights.append(
        {
            "icon": "calendar-check",
            "label": "Best month for earnings",
            "value": best["label"] if best else "No earnings yet",
            "detail": f"{format_zmw(best['amount'])} in planner earnings" if best else "Earnings appear once payments are verified.",
        }
    )
    if growth is None:
        growth_value, growth_detail = "Choose a time period", "Growth compares the selected period with the one just before it."
    elif growth["percent"] is None:
        growth_value = "New revenue" if growth["current"] > 0 else "No change"
        growth_detail = "There was no net revenue in the previous period to compare with."
    else:
        sign = "+" if growth["percent"] > 0 else ""
        growth_value = f"{sign}{growth['percent']}%"
        growth_detail = f"{format_zmw(growth['current'])} net revenue, compared with {format_zmw(growth['previous'])} in the previous period of the same length"
    insights.append({"icon": "graph-up-arrow", "label": "Revenue growth", "value": growth_value, "detail": growth_detail})

    near_full = [
        row for row in event_rows
        if row["status"] == "published" and row["capacity"] and CAPACITY_WARNING * row["capacity"] <= row["sold_all"] < row["capacity"]
    ]
    insights.append(
        {
            "icon": "speedometer",
            "label": "Approaching capacity",
            "value": f"{len(near_full)} event{'s' if len(near_full) != 1 else ''}",
            "detail": ", ".join(f"{row['event'].title} ({row['sell_through']}% sold)" for row in near_full[:3])
            or f"No published event has passed {int(CAPACITY_WARNING * 100)}% of its tickets.",
        }
    )
    low = low_sales_events(event_rows, now)
    insights.append(
        {
            "icon": "graph-down-arrow",
            "label": "Low ticket sales",
            "value": f"{len(low)} event{'s' if len(low) != 1 else ''}",
            "detail": ", ".join(f"{row['event'].title} ({row['sell_through']}% sold)" for row in low[:3])
            or f"No event on sale for a week is below {int(LOW_SALES_RATE * 100)}% sold.",
        }
    )
    if bookings is not None:
        insights.append(
            {
                "icon": "hand-thumbs-up",
                "label": "Booking acceptance rate",
                "value": _rate_text(bookings["acceptance_rate"]),
                "detail": f"{bookings['accepted']} accepted of {bookings['accepted'] + bookings['rejected']} answered requests"
                if bookings["acceptance_rate"] is not None else "No booking requests have been accepted or rejected in this period.",
            }
        )
        insights.append(
            {
                "icon": "check2-all",
                "label": "Booking completion rate",
                "value": _rate_text(bookings["completion_rate"]),
                "detail": f"{bookings['completed']} completed of {bookings['accepted']} accepted bookings"
                if bookings["completion_rate"] is not None else "No accepted bookings in this period yet.",
            }
        )
    return insights


def low_sales_events(event_rows, now):
    return [
        row for row in event_rows
        if row["status"] == "published"
        and row["capacity"]
        and now - row["event"].sales_start >= LOW_SALES_AFTER
        and now <= row["event"].sales_end
        and row["sold_all"] < LOW_SALES_RATE * row["capacity"]
    ]


def _transactions(sales, refunds, payouts, transaction_type):
    rows = {"ticket_sale": sales, "refund": refunds, "payout": payouts}
    chosen = [row for kind, items in rows.items() if transaction_type in ("all", kind) for row in items]
    return sorted(chosen, key=lambda row: row["when"], reverse=True)


def _category_sales(issued):
    categories = {}
    for ticket in issued:
        count, revenue = categories.get(ticket.category, (0, 0))
        categories[ticket.category] = (count + 1, revenue + ticket.unit_price)
    return [
        {"name": name, "tickets": count, "revenue": revenue}
        for name, (count, revenue) in sorted(categories.items(), key=lambda item: (-item[1][0], item[0]))
    ]


def _events_created(event_rows, period):
    created = [row for row in event_rows if (period.start is None or row["event"].created_at >= period.start) and (period.end is None or row["event"].created_at < period.end)]
    counts = dict.fromkeys(EVENT_STATUS_LABELS, 0)
    for row in created:
        counts[row["status"]] += 1
    return {"total": len(created), **counts}


def payout_balance(profile):
    scope = Scope(organiser_id=profile.user_id, planner_id=profile.id)
    earned = _money(_sales(scope, None, None), _refunds(scope, None, None))["earnings"]
    paid = int(
        db.session.query(func.coalesce(func.sum(PlannerPayout.amount), 0))
        .filter(PlannerPayout.planner_id == profile.id)
        .scalar()
        or 0
    )
    return {"earned": earned, "paid": paid, "balance": earned - paid}


def _money_chart(chart_id, chart_type, labels, datasets, horizontal=False):
    return {"id": chart_id, "type": chart_type, "money": True, "horizontal": horizontal, "labels": labels, "datasets": datasets}


def planner_report(profile, period, event=None, transaction_type="all"):
    """Everything on one planner's report. Only that planner's events, bookings and payouts are read."""
    now = utcnow()
    rate = commission_rate()
    scope = Scope(organiser_id=profile.user_id, planner_id=profile.id, event_id=event.id if event else None)
    sales = _sales(scope, period.start, period.end)
    refunds = _refunds(scope, period.start, period.end)
    issued = _issued_tickets(scope, period.start, period.end)
    payouts = [] if event else _payouts(profile.id, period.start, period.end)
    booking_rows = _booking_rows(_bookings(scope, period.start, period.end))
    bookings = _booking_summary(booking_rows)
    unearned = _unearned_payments(scope, period.start, period.end)

    all_event_rows = _event_rows(Scope(organiser_id=profile.user_id), sales, refunds, issued, now)
    event_rows = [row for row in all_event_rows if event is None or row["event"].id == event.id]
    event_rows.sort(key=lambda row: (row["net_revenue"], row["sold"], row["event"].start_datetime), reverse=True)
    money = _money(sales, refunds)
    balance = payout_balance(profile)
    finance = {
        **money,
        "ticket_gross": money["gross"],
        "ticket_net": money["net_revenue"],
        "booking_revenue": bookings["revenue"],
        "booking_commission": bookings["commission"],
        "booking_earnings": bookings["earnings"],
        "total_gross": money["gross"] + bookings["revenue"],
        "total_commission": money["commission"] + bookings["commission"],
        "total_earnings": money["earnings"] + bookings["earnings"],
        "payouts_period": sum(row["amount"] for row in payouts),
        "balance": balance["balance"],
        "earned_all_time": balance["earned"],
        "paid_all_time": balance["paid"],
        "pending_count": unearned["pending"][0],
        "pending_amount": unearned["pending"][1],
        "failed_count": unearned["failed"][0],
        "failed_amount": unearned["failed"][1],
        "simulated_amount": sum(row["gross"] for row in sales if row["simulated"]),
    }

    keys, labels = _series_axis(
        period, [row["when"] for row in sales + refunds] + [ticket.issued_at for ticket in issued] + [row["requested"] for row in booking_rows]
    )
    bucket = period.bucket
    categories = _category_sales(issued)
    top_events = [row for row in event_rows if row["net_revenue"] > 0][:8]
    charts = [
        _money_chart("report-revenue-chart", "bar", labels, [
            {"label": "Gross revenue", "data": [kwacha(v) for v in _sum_by_bucket(sales, keys, bucket, lambda r: r["gross"])], "color": "green"},
            {"label": "Refunds", "data": [kwacha(-v) for v in _sum_by_bucket(refunds, keys, bucket, lambda r: r["gross"])], "color": "red"},
        ]),
        _money_chart("report-earnings-chart", "line", labels, [
            {"label": f"Planner earnings ({100 - rate}%)", "data": [kwacha(v) for v in _sum_by_bucket(sales + refunds, keys, bucket, lambda r: r["net"])], "color": "green"},
            {"label": f"Platform commission ({rate}%)", "data": [kwacha(v) for v in _sum_by_bucket(sales + refunds, keys, bucket, lambda r: r["commission"])], "color": "copper"},
        ]),
        {"id": "report-tickets-chart", "type": "bar", "labels": labels, "datasets": [
            {"label": "Tickets sold", "data": _count_by_bucket([ticket.issued_at for ticket in issued], keys, bucket), "color": "copper"},
        ]},
        {"id": "report-bookings-chart", "type": "bar", "labels": labels, "datasets": [
            {"label": "Requests received", "data": _count_by_bucket([row["requested"] for row in booking_rows], keys, bucket), "color": "green"},
            {"label": "Completed", "data": _count_by_bucket([row["requested"] for row in booking_rows if row["status"] == "completed"], keys, bucket), "color": "gold"},
        ]},
        {"id": "report-category-chart", "type": "doughnut", "labels": [row["name"] for row in categories], "datasets": [
            {"label": "Tickets sold", "data": [row["tickets"] for row in categories], "colors": ["green", "copper", "gold", "slate", "red"]},
        ]},
        _money_chart("report-events-chart", "bar", [row["event"].title for row in top_events], [
            {"label": "Net revenue", "data": [kwacha(row["net_revenue"]) for row in top_events], "color": "green"},
            {"label": f"Planner earnings ({100 - rate}%)", "data": [kwacha(row["earnings"]) for row in top_events], "color": "copper"},
        ], horizontal=True),
    ]

    return {
        "period": period,
        "rate": rate,
        "event": event,
        "event_detail": _event_detail(event_rows[0], issued) if event and event_rows else None,
        "overview": {
            "events": _events_created(all_event_rows if event is None else event_rows, period),
            "tickets_sold": len(issued),
            "ticket_revenue": money["net_revenue"],
            "bookings_total": bookings["total"],
            "bookings_completed": bookings["completed"],
            "bookings_cancelled": bookings["cancelled"],
            "gross": finance["total_gross"],
            "commission": finance["total_commission"],
            "earnings": finance["total_earnings"],
        },
        "events": event_rows,
        "tickets": {
            "sold": len(issued),
            "refunded": _refunded_ticket_count(refunds),
            "refunded_amount": money["refunds"],
            "successful_transactions": len(sales),
            "categories": categories,
            "best": top_events[:5],
            "low": low_sales_events(event_rows, now),
        },
        "bookings": bookings,
        "booking_rows": booking_rows,
        "finance": finance,
        "payouts": sorted(payouts, key=lambda row: row["when"], reverse=True),
        "insights": _insights(event_rows, sales, refunds, _growth(scope, period, money["net_revenue"]), None if event else bookings, now),
        "transaction_type": transaction_type,
        "transactions": _transactions(sales, refunds, payouts, transaction_type),
        "charts": charts,
    }


def planner_comparison(period, profile_ids=None):
    """One row per planner with the same calculations as their own report, for admin comparison."""
    profiles_query = PlannerProfile.query.options(joinedload(PlannerProfile.user)).order_by(PlannerProfile.business_name)
    if profile_ids:
        profiles_query = profiles_query.filter(PlannerProfile.id.in_(profile_ids))
    profiles = profiles_query.all()
    platform = Scope()
    sales = _sales(platform, period.start, period.end)
    refunds = _refunds(platform, period.start, period.end)
    issued = _issued_tickets(platform, period.start, period.end)
    payouts = _payouts(None, period.start, period.end)
    all_sales = _sales(platform, None, None) if period.start or period.end else sales
    all_refunds = _refunds(platform, None, None) if period.start or period.end else refunds
    paid_all = dict(
        db.session.query(PlannerPayout.planner_id, func.coalesce(func.sum(PlannerPayout.amount), 0))
        .group_by(PlannerPayout.planner_id)
        .all()
    )
    events_created = dict(
        _within(db.session.query(Event.organiser_id, func.count(Event.id)), Event.created_at, period.start, period.end)
        .group_by(Event.organiser_id)
        .all()
    )
    booking_rows = _booking_rows(_bookings(platform, period.start, period.end))

    rows = []
    for profile in profiles:
        user_id = profile.user_id
        mine = [row for row in sales if row["organiser_id"] == user_id]
        mine_refunds = [row for row in refunds if row["organiser_id"] == user_id]
        money = _money(mine, mine_refunds)
        earned_all = _money(
            [row for row in all_sales if row["organiser_id"] == user_id],
            [row for row in all_refunds if row["organiser_id"] == user_id],
        )["earnings"]
        bookings = _booking_summary([row for row in booking_rows if row["booking"].planner_id == profile.id])
        rows.append(
            {
                "profile": profile,
                "events": int(events_created.get(user_id, 0)),
                "tickets": sum(1 for ticket in issued if ticket.organiser_id == user_id),
                **money,
                "payouts": sum(row["amount"] for row in payouts if row["planner_id"] == profile.id),
                "balance": earned_all - int(paid_all.get(profile.id, 0) or 0),
                "bookings": bookings["total"],
                "bookings_completed": bookings["completed"],
                "acceptance_rate": bookings["acceptance_rate"],
                "completion_rate": bookings["completion_rate"],
            }
        )
    rows.sort(key=lambda row: (row["net_revenue"], row["tickets"], row["profile"].business_name), reverse=True)
    return rows


def comparison_chart(rows, rate=None):
    rate = commission_rate() if rate is None else rate
    labels = [row["profile"].business_name for row in rows]
    return _money_chart("admin-compare-chart", "bar", labels, [
        {"label": "Net revenue", "data": [kwacha(row["net_revenue"]) for row in rows], "color": "green"},
        {"label": f"Commission ({rate}%)", "data": [kwacha(row["commission"]) for row in rows], "color": "copper"},
        {"label": f"Planner earnings ({100 - rate}%)", "data": [kwacha(row["earnings"]) for row in rows], "color": "gold"},
    ])


def platform_report(period, transaction_type="all"):
    """Platform-wide totals across every planner, for administrators only."""
    now = utcnow()
    rate = commission_rate()
    scope = Scope()
    sales = _sales(scope, period.start, period.end)
    refunds = _refunds(scope, period.start, period.end)
    issued = _issued_tickets(scope, period.start, period.end)
    payouts = _payouts(None, period.start, period.end)
    booking_rows = _booking_rows(_bookings(scope, period.start, period.end))
    bookings = _booking_summary(booking_rows)
    unearned = _unearned_payments(scope, period.start, period.end)
    event_rows = _event_rows(scope, sales, refunds, issued, now)
    event_rows.sort(key=lambda row: (row["net_revenue"], row["sold"]), reverse=True)
    comparison = planner_comparison(period)
    money = _money(sales, refunds)

    planner_users = db.session.query(func.count(User.id)).filter(User.role == "planner").scalar() or 0
    approved = PlannerProfile.query.filter_by(approval_status="approved").count()
    new_planners = _within(User.query.filter(User.role == "planner"), User.created_at, period.start, period.end).count()
    registrations = _within(
        db.session.query(User.created_at, User.role).filter(User.role.in_(("client", "planner"))), User.created_at, period.start, period.end
    ).all()

    finance = {
        **money,
        "booking_revenue": bookings["revenue"],
        "total_gross": money["gross"] + bookings["revenue"],
        "total_commission": money["commission"] + bookings["commission"],
        "total_earnings": money["earnings"] + bookings["earnings"],
        "payouts_period": sum(row["amount"] for row in payouts),
        "balance": sum(row["balance"] for row in comparison),
        "pending_count": unearned["pending"][0],
        "pending_amount": unearned["pending"][1],
        "failed_count": unearned["failed"][0],
        "failed_amount": unearned["failed"][1],
        "simulated_amount": sum(row["gross"] for row in sales if row["simulated"]),
        "live_amount": sum(row["gross"] for row in sales if not row["simulated"]),
    }

    keys, labels = _series_axis(
        period,
        [row["when"] for row in sales + refunds] + [ticket.issued_at for ticket in issued] + [row["requested"] for row in booking_rows] + [moment for moment, _ in registrations],
    )
    bucket = period.bucket
    top_planners = [row for row in comparison if row["net_revenue"] > 0][:8]
    best_events = [row for row in event_rows if row["net_revenue"] > 0][:8]
    charts = [
        _money_chart("admin-revenue-chart", "bar", labels, [
            {"label": "Gross revenue", "data": [kwacha(v) for v in _sum_by_bucket(sales, keys, bucket, lambda r: r["gross"])], "color": "green"},
            {"label": f"Platform commission ({rate}%)", "data": [kwacha(v) for v in _sum_by_bucket(sales + refunds, keys, bucket, lambda r: r["commission"])], "color": "copper"},
        ]),
        {"id": "admin-tickets-chart", "type": "bar", "labels": labels, "datasets": [
            {"label": "Tickets sold", "data": _count_by_bucket([ticket.issued_at for ticket in issued], keys, bucket), "color": "copper"},
        ]},
        _money_chart("admin-planners-chart", "bar", [row["profile"].business_name for row in top_planners], [
            {"label": "Net revenue", "data": [kwacha(row["net_revenue"]) for row in top_planners], "color": "green"},
            {"label": f"Commission ({rate}%)", "data": [kwacha(row["commission"]) for row in top_planners], "color": "copper"},
        ], horizontal=True),
        _money_chart("admin-events-chart", "bar", [row["event"].title for row in best_events], [
            {"label": "Net revenue", "data": [kwacha(row["net_revenue"]) for row in best_events], "color": "green"},
        ], horizontal=True),
        {"id": "admin-bookings-chart", "type": "bar", "labels": labels, "datasets": [
            {"label": "Requests received", "data": _count_by_bucket([row["requested"] for row in booking_rows], keys, bucket), "color": "green"},
            {"label": "Completed", "data": _count_by_bucket([row["requested"] for row in booking_rows if row["status"] == "completed"], keys, bucket), "color": "gold"},
        ]},
        {"id": "admin-registrations-chart", "type": "line", "labels": labels, "datasets": [
            {"label": "Clients", "data": _count_by_bucket([moment for moment, role in registrations if role == "client"], keys, bucket), "color": "green"},
            {"label": "Planners", "data": _count_by_bucket([moment for moment, role in registrations if role == "planner"], keys, bucket), "color": "copper"},
        ]},
    ]

    insights = _insights(event_rows, sales, refunds, _growth(scope, period, money["net_revenue"]), bookings, now)
    leader = top_planners[0] if top_planners else None
    insights.insert(
        0,
        {
            "icon": "trophy",
            "label": "Top-performing planner",
            "value": leader["profile"].business_name if leader else "No planner revenue yet",
            "detail": f"{format_zmw(leader['net_revenue'])} net revenue in this period" if leader else "No verified payments for planner events in this period.",
        },
    )

    return {
        "period": period,
        "rate": rate,
        "overview": {
            "planners": int(planner_users),
            "planners_approved": approved,
            "planners_new": new_planners,
            "events": _events_created(event_rows, period),
            "tickets_sold": len(issued),
            "ticket_revenue": money["net_revenue"],
            "successful_transactions": len(sales),
            "refunded_transactions": len(refunds),
            "refunded_tickets": _refunded_ticket_count(refunds),
        },
        "finance": finance,
        "bookings": bookings,
        "events": event_rows,
        "best_events": best_events,
        "comparison": comparison,
        "insights": insights,
        "transaction_type": transaction_type,
        "transactions": _transactions(sales, refunds, payouts, transaction_type),
        "charts": charts,
    }
