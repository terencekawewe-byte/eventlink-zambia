"""Planner booking rules and schedule conflicts."""

from datetime import datetime, timedelta

from app.models import PlannerBooking
from app.utils.validators import LUSAKA


ACTIVE_STATUSES = ("accepted", "in_progress")


def _interval(booking):
    start = datetime.combine(booking.event_date, booking.start_time)
    end = start + timedelta(minutes=int(booking.duration))
    return start, end


def has_conflict(planner_id, event_date, start_time, duration_minutes, exclude_id=None):
    proposed_start = datetime.combine(event_date, start_time)
    proposed_end = proposed_start + timedelta(minutes=int(duration_minutes))
    dates = {event_date, (proposed_start - timedelta(days=1)).date(), proposed_end.date()}
    query = PlannerBooking.query.filter(
        PlannerBooking.planner_id == planner_id,
        PlannerBooking.status.in_(ACTIVE_STATUSES),
        PlannerBooking.event_date.in_(dates),
    )
    if exclude_id:
        query = query.filter(PlannerBooking.id != exclude_id)
    for booking in query.all():
        start, end = _interval(booking)
        if proposed_start < end and start < proposed_end:
            return True
    return False


def client_can_cancel(booking):
    if booking.status == "pending":
        return True
    if booking.status != "accepted":
        return False
    start = datetime.combine(booking.event_date, booking.start_time, tzinfo=LUSAKA)
    return start - datetime.now(LUSAKA) >= timedelta(hours=48)
