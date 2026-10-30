"""Public event catalogue, planner directory, and ticket checkout start."""

import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db
from app.models import Event, EventCategory, PlannerBooking, PlannerProfile, TicketType, User
from app.services.payment_service import start_checkout
from app.services.ticket_service import release_expired_reservations, sales_label
from app.utils.decorators import client_required
from app.utils.helpers import escape_like, page_arg, paginate
from app.utils.validators import LUSAKA, parse_money_to_ngwee, parse_positive_int, utcnow_naive

events_bp = Blueprint("events", __name__)

ZAMBIAN_TOWNS = [
    "Lusaka",
    "Kitwe",
    "Ndola",
    "Kabwe",
    "Chingola",
    "Mufulira",
    "Livingstone",
    "Luanshya",
    "Chipata",
    "Kasama",
    "Solwezi",
    "Mazabuka",
]


def _public_query():
    now = utcnow_naive()
    return Event.query.filter(
        Event.approval_status == "approved",
        Event.publication_status == "published",
        Event.end_datetime >= now,
    )


def _local_bound(value, end=False):
    day = datetime.strptime(value, "%Y-%m-%d")
    if end:
        day = day + timedelta(days=1)
    return day.replace(tzinfo=LUSAKA).astimezone(timezone.utc).replace(tzinfo=None)


@events_bp.route("/events")
def listing():
    categories = EventCategory.query.filter_by(is_active=True).order_by(EventCategory.name).all()
    filters = {
        "q": (request.args.get("q") or "").strip()[:100],
        "category": (request.args.get("category") or "").strip(),
        "location": (request.args.get("location") or "").strip()[:120],
        "date_from": (request.args.get("date_from") or "").strip(),
        "date_to": (request.args.get("date_to") or "").strip(),
        "price_min": (request.args.get("price_min") or "").strip(),
        "price_max": (request.args.get("price_max") or "").strip(),
        "sort": request.args.get("sort") or "date_asc",
    }
    errors = []
    query = _public_query().options(joinedload(Event.category), joinedload(Event.ticket_types))
    if filters["q"]:
        like = f"%{escape_like(filters['q'])}%"
        query = query.filter(or_(Event.title.ilike(like, escape="\\"), Event.venue.ilike(like, escape="\\")))
    if filters["category"].isdigit():
        query = query.filter(Event.category_id == int(filters["category"]))
    if filters["location"]:
        query = query.filter(Event.location.ilike(f"%{escape_like(filters['location'])}%", escape="\\"))
    try:
        if filters["date_from"]:
            query = query.filter(Event.start_datetime >= _local_bound(filters["date_from"]))
        if filters["date_to"]:
            query = query.filter(Event.start_datetime < _local_bound(filters["date_to"], end=True))
    except ValueError:
        errors.append("Enter dates as YYYY-MM-DD.")
    min_price = max_price = None
    try:
        if filters["price_min"]:
            min_price = parse_money_to_ngwee(filters["price_min"], "Minimum price")
        if filters["price_max"]:
            max_price = parse_money_to_ngwee(filters["price_max"], "Maximum price")
    except ValueError as exc:
        errors.append(str(exc))
    if min_price is not None and max_price is not None and min_price > max_price:
        errors.append("The minimum price cannot be higher than the maximum price.")
    if min_price is not None or max_price is not None:
        match = db.session.query(TicketType.event_id)
        if min_price is not None:
            match = match.filter(TicketType.price >= min_price)
        if max_price is not None:
            match = match.filter(TicketType.price <= max_price)
        query = query.filter(Event.id.in_(match))
    min_ticket_price = (
        db.session.query(func.min(TicketType.price))
        .filter(TicketType.event_id == Event.id)
        .correlate(Event)
        .scalar_subquery()
    )
    sort = filters["sort"]
    if sort == "date_desc":
        query = query.order_by(Event.start_datetime.desc())
    elif sort == "price_asc":
        query = query.order_by(min_ticket_price.asc(), Event.start_datetime.asc())
    elif sort == "price_desc":
        query = query.order_by(min_ticket_price.desc(), Event.start_datetime.asc())
    else:
        sort = "date_asc"
        query = query.order_by(Event.start_datetime.asc())
    filters["sort"] = sort
    searching = any(filters[key] for key in ("q", "category", "location", "date_from", "date_to", "price_min", "price_max"))
    template = "events/search_results.html" if searching else "events/event_list.html"
    return render_template(
        template,
        result=paginate(query, page_arg(), 9),
        categories=categories,
        filters=filters,
        errors=errors,
        towns=ZAMBIAN_TOWNS,
    )


@events_bp.route("/events/<int:event_id>")
def detail(event_id):
    release_expired_reservations()
    event = Event.query.options(
        joinedload(Event.ticket_types),
        joinedload(Event.category),
        joinedload(Event.organiser),
        selectinload(Event.artists),
    ).get_or_404(event_id)
    can_preview = g.user and (g.user.role == "admin" or g.user.id == event.organiser_id)
    holds_ticket = False
    if g.user and g.user.role == "client":
        from app.models import Ticket

        holds_ticket = Ticket.query.filter_by(client_id=g.user.id, event_id=event.id).count() > 0
    if not event.is_public and not can_preview and not holds_ticket:
        return render_template("errors/404.html"), 404
    token = secrets.token_urlsafe(24)
    session["checkout_token"] = token
    states = {ticket.id: sales_label(event, ticket) for ticket in event.ticket_types}
    allowances = {}
    if g.user and g.user.role == "client":
        from app.services.payment_service import purchase_allowance

        allowances = {ticket.id: purchase_allowance(g.user.id, ticket) for ticket in event.ticket_types}
    return render_template(
        "events/event_details.html", event=event, states=states, allowances=allowances, checkout_token=token
    )


@events_bp.route("/events/<int:event_id>/purchase", methods=["POST"])
@client_required
def purchase(event_id):
    event = Event.query.get_or_404(event_id)
    supplied = request.form.get("checkout_token") or ""
    expected = session.get("checkout_token") or ""
    session.pop("checkout_token", None)
    if not expected or not supplied or len(supplied) != len(expected) or not secrets.compare_digest(supplied, expected):
        flash("The checkout form expired. Choose your tickets again.", "warning")
        return redirect(url_for("events.detail", event_id=event.id))
    raw_type = request.form.get("ticket_type_id", "")
    if not str(raw_type).isdigit():
        flash("Choose a ticket category.", "danger")
        return redirect(url_for("events.detail", event_id=event.id))
    ticket_type = TicketType.query.filter_by(id=int(raw_type), event_id=event.id).first()
    if ticket_type is None:
        flash("That ticket category does not belong to this event.", "danger")
        return redirect(url_for("events.detail", event_id=event.id))
    try:
        quantity = parse_positive_int(request.form.get("quantity"), "Quantity", 100)
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("events.detail", event_id=event.id))
    order, error = start_checkout(g.user, event, ticket_type, quantity)
    if error:
        flash(error, "danger")
        return redirect(url_for("events.detail", event_id=event.id))
    return redirect(url_for("payments.show", order_reference=order.order_reference))


@events_bp.route("/planners")
def planners():
    location = (request.args.get("location") or "").strip()[:120]
    query = (
        PlannerProfile.query.join(User, PlannerProfile.user_id == User.id)
        .filter(PlannerProfile.approval_status == "approved", User.account_status == "active")
        .order_by(PlannerProfile.business_name)
    )
    if location:
        query = query.filter(PlannerProfile.location.ilike(f"%{escape_like(location)}%", escape="\\"))
    result = paginate(query, page_arg(), 9)
    return render_template(
        "events/planner_list.html",
        result=result,
        completed=_completed_bookings([profile.id for profile in result["items"]]),
        location=location,
        towns=ZAMBIAN_TOWNS,
    )


def _completed_bookings(profile_ids):
    if not profile_ids:
        return {}
    rows = (
        db.session.query(PlannerBooking.planner_id, func.count(PlannerBooking.id))
        .filter(PlannerBooking.planner_id.in_(profile_ids), PlannerBooking.status == "completed")
        .group_by(PlannerBooking.planner_id)
        .all()
    )
    return dict(rows)


@events_bp.route("/planners/<int:planner_id>")
def planner_detail(planner_id):
    profile = PlannerProfile.query.get_or_404(planner_id)
    if profile.approval_status != "approved" or profile.user.account_status != "active":
        if not (g.user and (g.user.role == "admin" or g.user.id == profile.user_id)):
            return render_template("errors/404.html"), 404
    completed = _completed_bookings([profile.id]).get(profile.id, 0)
    return render_template("events/planner_public.html", profile=profile, completed=completed)
