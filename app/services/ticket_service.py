"""Ticket inventory, issuance, and redemption."""

import secrets

from flask import current_app
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Order, Ticket, TicketRedemption, TicketType
from app.models.database import utcnow
from app.services.notification_service import notify
from app.utils.helpers import generate_ticket_code
from app.utils.validators import utcnow_naive


def sales_are_open(event, ticket_type, now=None):
    now = now or utcnow_naive()
    if event is None or ticket_type is None or ticket_type.event_id != event.id:
        return False
    if event.approval_status != "approved" or event.publication_status != "published":
        return False
    if now < event.sales_start or now > event.sales_end or now > event.end_datetime:
        return False
    return True


def sales_label(event, ticket_type, now=None):
    now = now or utcnow_naive()
    if event.publication_status == "cancelled":
        return "cancelled"
    if event.publication_status in {"suspended", "unpublished"} or event.approval_status != "approved":
        return "unavailable"
    if now > event.end_datetime:
        return "ended"
    if now < event.sales_start:
        return "not_open"
    if now > event.sales_end:
        return "closed"
    if ticket_type.remaining <= 0:
        return "sold_out"
    return "available"


def reserve_stock(ticket_type_id, quantity):
    result = db.session.execute(
        update(TicketType)
        .where(TicketType.id == ticket_type_id)
        .where(
            TicketType.quantity_total - TicketType.quantity_sold - TicketType.quantity_reserved
            >= quantity
        )
        .values(quantity_reserved=TicketType.quantity_reserved + quantity)
    )
    return result.rowcount == 1


def release_stock(ticket_type_id, quantity):
    result = db.session.execute(
        update(TicketType)
        .where(TicketType.id == ticket_type_id)
        .where(TicketType.quantity_reserved >= quantity)
        .values(quantity_reserved=TicketType.quantity_reserved - quantity)
    )
    return result.rowcount == 1


def consume_reservation(ticket_type_id, quantity):
    result = db.session.execute(
        update(TicketType)
        .where(TicketType.id == ticket_type_id)
        .where(TicketType.quantity_reserved >= quantity)
        .values(
            quantity_reserved=TicketType.quantity_reserved - quantity,
            quantity_sold=TicketType.quantity_sold + quantity,
        )
    )
    return result.rowcount == 1


def release_order_reservation(order):
    for item in order.items:
        if not release_stock(item.ticket_type_id, item.quantity):
            return False
    return True


def release_expired_reservations():
    now = utcnow_naive()
    orders = Order.query.filter(Order.order_status == "pending", Order.expires_at < now).all()
    for order in orders:
        if not release_order_reservation(order):
            db.session.rollback()
            current_app.logger.warning("Could not release reservation for order %s", order.id)
            continue
        order.order_status = "cancelled"
        for payment in order.payments:
            if payment.status == "pending":
                payment.status = "failed"
                payment.provider_status = "expired"
        notify(
            order.client_id,
            "Ticket reservation expired",
            f"Order {order.order_reference} was not paid in time. The reserved tickets have been released.",
            "/client/payments",
        )
        db.session.commit()


def issue_tickets(order):
    created = []
    for item in order.items:
        if not consume_reservation(item.ticket_type_id, item.quantity):
            return None
        for _ in range(item.quantity):
            ticket = _new_ticket(order, item)
            db.session.add(ticket)
            created.append(ticket)
    db.session.flush()
    return created


def _new_ticket(order, item):
    for _ in range(5):
        ticket = Ticket(
            ticket_code=generate_ticket_code(),
            qr_token=secrets.token_urlsafe(32),
            order_item_id=item.id,
            client_id=order.client_id,
            event_id=order.event_id,
            ticket_type_id=item.ticket_type_id,
            status="valid",
            issued_at=utcnow(),
        )
        db.session.add(ticket)
        try:
            db.session.flush()
            return ticket
        except IntegrityError:
            db.session.expunge(ticket)
    raise RuntimeError("Could not generate a unique ticket code.")


def event_is_locked(event):
    if any(ticket.quantity_sold or ticket.quantity_reserved for ticket in event.ticket_types):
        return True
    return (
        Order.query.filter(
            Order.event_id == event.id,
            Order.order_status.in_(["pending", "paid", "refunded"]),
        ).count()
        > 0
    )


def staff_can_verify(user, event):
    if user is None or user.account_status != "active":
        return False
    if user.role == "admin":
        return True
    return user.role == "planner" and user.id == event.organiser_id and user.is_approved_planner


def record_redemption(ticket, actor, result):
    db.session.add(TicketRedemption(ticket_id=ticket.id, actor_id=actor.id, result=result))


def redeem_ticket(ticket, actor):
    if ticket.status == "redeemed":
        record_redemption(ticket, actor, "already_redeemed")
        db.session.commit()
        return "already_redeemed"
    if ticket.status != "valid":
        record_redemption(ticket, actor, "not_valid")
        db.session.commit()
        return "not_valid"
    result = db.session.execute(
        update(Ticket)
        .where(Ticket.id == ticket.id)
        .where(Ticket.status == "valid")
        .values(status="redeemed", redeemed_at=utcnow(), redeemed_by=actor.id)
    )
    if result.rowcount != 1:
        record_redemption(ticket, actor, "already_redeemed")
        db.session.commit()
        return "already_redeemed"
    record_redemption(ticket, actor, "accepted")
    db.session.commit()
    return "accepted"
