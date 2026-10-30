"""Payment initiation and verified webhook processing.

A checkout button never marks a payment as successful. Status changes happen
only after a signed provider payload is verified. Simulated payments are stored
separately from live provider payments.
"""

import hashlib
import hmac
import json
import secrets
from datetime import timedelta

from flask import current_app
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Order, OrderItem, Payment, PaymentCallback, TicketType
from app.models.database import utcnow
from app.services.notification_service import notify
from app.services.ticket_service import (
    issue_tickets,
    release_expired_reservations,
    release_order_reservation,
    reserve_stock,
    sales_are_open,
)
from app.utils.helpers import audit, generate_reference
from app.utils.validators import PERSON_NAME, normalize_phone, utcnow_naive

SIMULATED_METHODS = {
    "mtn": "simulation:mtn",
    "airtel": "simulation:airtel",
    "zamtel": "simulation:zamtel",
    "card": "simulation:card",
}
LIVE_METHODS = {
    "mtn": "live:mtn",
    "airtel": "live:airtel",
    "zamtel": "live:zamtel",
    "card": "live:card",
}


MOBILE_NETWORKS = {
    "mtn": ("MTN", ("96", "76")),
    "airtel": ("Airtel", ("97", "77")),
    "zamtel": ("Zamtel", ("95", "75")),
}


def validate_payer_details(method_key, form):
    """Check the payer's mobile money number or name on card. Nothing here is stored."""
    if method_key in MOBILE_NETWORKS:
        network, prefixes = MOBILE_NETWORKS[method_key]
        phone, error = normalize_phone(form.get("payer_phone"))
        if error:
            return None, f"Enter the {network} mobile money number to pay from, such as 0{prefixes[0]}1234567."
        if phone[4:6] not in prefixes:
            starts = " or ".join(f"0{prefix}" for prefix in prefixes)
            return None, f"That number is not on {network}. {network} numbers start with {starts}."
        local = "0" + phone[4:]
        return {"kind": "mobile", "label": f"{local[:3]} *** {local[-4:]}"}, None
    if method_key == "card":
        name = " ".join((form.get("card_name") or "").split())
        if not PERSON_NAME.fullmatch(name):
            return None, "Enter the name exactly as it appears on the card."
        return {"kind": "card", "label": name}, None
    return None, "Choose a payment method."


def simulation_mode():
    return current_app.config.get("PAYMENT_MODE") == "simulation"


def encode_payload(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_payload(raw_body, secret):
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


HOLDING_ORDER_STATUSES = ("pending", "paid")


def tickets_held(client_id, ticket_type_id):
    """Tickets a client already holds or has reserved in one category."""
    total = (
        db.session.query(db.func.coalesce(db.func.sum(OrderItem.quantity), 0))
        .join(Order, Order.id == OrderItem.order_id)
        .filter(
            Order.client_id == client_id,
            Order.order_status.in_(HOLDING_ORDER_STATUSES),
            OrderItem.ticket_type_id == ticket_type_id,
        )
        .scalar()
    )
    return int(total or 0)


def purchase_allowance(client_id, ticket_type):
    return max(0, ticket_type.max_per_order - tickets_held(client_id, ticket_type.id))


def start_checkout(client, event, ticket_type, quantity):
    release_expired_reservations()
    if client is None or client.role != "client" or client.account_status != "active":
        return None, "Only a signed-in client can purchase tickets."
    db.session.refresh(event)
    ticket_type = db.session.get(TicketType, ticket_type.id)
    if ticket_type is None or ticket_type.event_id != event.id:
        return None, "That ticket category does not belong to this event."
    if event.publication_status == "cancelled":
        return None, "This event has been cancelled."
    if not sales_are_open(event, ticket_type):
        return None, "Ticket sales are not open for this event."
    if not isinstance(quantity, int) or quantity < 1:
        return None, "Ticket quantity must be a positive whole number."
    limit = ticket_type.max_per_order
    held = tickets_held(client.id, ticket_type.id)
    if held >= limit:
        return None, f"You already have the maximum of {limit} {ticket_type.name} tickets for this event."
    if held + quantity > limit:
        if held:
            return None, (
                f"Each customer can buy at most {limit} {ticket_type.name} tickets. "
                f"You already have {held}, so you can buy up to {limit - held} more."
            )
        return None, f"Each customer can buy at most {limit} {ticket_type.name} tickets."
    if ticket_type.price <= 0:
        return None, "This ticket price is not valid."

    if not reserve_stock(ticket_type.id, quantity):
        db.session.rollback()
        return None, "Not enough tickets remain for that quantity."

    db.session.refresh(ticket_type)
    unit_price = ticket_type.price
    if unit_price <= 0:
        db.session.rollback()
        return None, "This ticket price is not valid."
    subtotal = unit_price * quantity
    hold = int(current_app.config.get("ORDER_HOLD_MINUTES", 30))
    order = Order(
        order_reference=generate_reference("ELZ"),
        client_id=client.id,
        event_id=event.id,
        total_amount=subtotal,
        order_status="pending",
        expires_at=utcnow_naive() + timedelta(minutes=hold),
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            order_id=order.id,
            ticket_type_id=ticket_type.id,
            quantity=quantity,
            unit_price=unit_price,
            subtotal=subtotal,
        )
    )
    payment = Payment(
        order_id=order.id,
        payment_reference=generate_reference("PAY"),
        payment_method="unassigned",
        amount=subtotal,
        status="pending",
        is_simulated=simulation_mode(),
    )
    db.session.add(payment)
    if order.total_amount != subtotal or payment.amount != order.total_amount:
        db.session.rollback()
        return None, "The order total could not be confirmed."
    db.session.commit()
    return order, None


def build_provider_payload(payment, status):
    if status not in {"successful", "failed"}:
        raise ValueError("Unsupported provider status.")
    prefix = "SIM" if payment.is_simulated else "PROV"
    return {
        "idempotency_key": secrets.token_hex(16),
        "payment_reference": payment.payment_reference,
        "provider_reference": f"{prefix}-{secrets.token_hex(6).upper()}",
        "amount": payment.amount,
        "currency": "ZMW",
        "status": status,
    }


def process_webhook(raw_body, signature, simulated):
    secret = (
        current_app.config.get("SIMULATION_WEBHOOK_SECRET")
        if simulated
        else current_app.config.get("PAYMENT_WEBHOOK_SECRET")
    )
    if not secret or not hmac.compare_digest(sign_payload(raw_body, secret), signature or ""):
        return {"ok": False, "error": "The payment notification could not be verified."}

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "error": "The payment notification was not valid JSON."}

    required = ["idempotency_key", "payment_reference", "provider_reference", "amount", "currency", "status"]
    if not isinstance(payload, dict) or any(key not in payload for key in required):
        return {"ok": False, "error": "The payment notification is missing required fields."}
    if payload["currency"] != "ZMW" or payload["status"] not in {"successful", "failed"}:
        return {"ok": False, "error": "The payment notification status is not supported."}
    if not isinstance(payload["amount"], int) or isinstance(payload["amount"], bool):
        return {"ok": False, "error": "The payment amount is not valid."}
    key = str(payload["idempotency_key"])
    if not 8 <= len(key) <= 80:
        return {"ok": False, "error": "The payment notification key is not valid."}

    payment = Payment.query.filter_by(payment_reference=str(payload["payment_reference"])).first()
    if payment is None:
        return {"ok": False, "error": "The payment reference was not found."}
    if bool(payment.is_simulated) != bool(simulated):
        return {"ok": False, "error": "Simulated and live payments cannot be mixed."}
    if int(payload["amount"]) != int(payment.amount):
        return {"ok": False, "error": "The verified amount does not match the order."}

    payload_hash = hashlib.sha256(raw_body).hexdigest()
    existing = PaymentCallback.query.filter_by(idempotency_key=key).first()
    if existing:
        return {"ok": True, "duplicate": True, "status": payment.status}

    try:
        db.session.add(
            PaymentCallback(
                idempotency_key=key,
                payment_id=payment.id,
                payload_hash=payload_hash,
            )
        )
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return {"ok": True, "duplicate": True, "status": "pending"}

    order = db.session.get(Order, payment.order_id)
    if payload["status"] == "successful":
        return _mark_successful(payment, order, payload)
    return _mark_failed(payment, order, payload)


def _mark_successful(payment, order, payload):
    if payment.status != "pending" or order.order_status != "pending":
        db.session.commit()
        return {"ok": True, "duplicate": True, "status": payment.status}

    paid = db.session.execute(
        update(Payment)
        .where(Payment.id == payment.id)
        .where(Payment.status == "pending")
        .values(
            status="successful",
            provider_reference=str(payload["provider_reference"])[:80],
            provider_status=payload["status"],
            verified_at=utcnow(),
        )
    )
    ordered = db.session.execute(
        update(Order)
        .where(Order.id == order.id)
        .where(Order.order_status == "pending")
        .values(order_status="paid", updated_at=utcnow())
    )
    if paid.rowcount != 1 or ordered.rowcount != 1:
        db.session.rollback()
        return {"ok": True, "duplicate": True, "status": "successful"}

    db.session.refresh(order)
    tickets = issue_tickets(order)
    if not tickets:
        db.session.rollback()
        return {"ok": False, "error": "Tickets could not be issued for this payment."}

    notify(
        order.client_id,
        "Ticket purchase confirmed",
        f"Payment for order {order.order_reference} was verified. Your tickets are ready.",
        "/client/tickets",
    )
    audit(
        None,
        "payment_verified",
        "payment",
        payment.id,
        "Verified provider notification accepted. Tickets issued.",
    )
    db.session.commit()
    return {"ok": True, "duplicate": False, "status": "successful"}


def _mark_failed(payment, order, payload):
    if payment.status != "pending" or order.order_status != "pending":
        db.session.commit()
        return {"ok": True, "duplicate": True, "status": payment.status}
    if not release_order_reservation(order):
        db.session.rollback()
        return {"ok": False, "error": "The reserved tickets could not be released."}
    payment.status = "failed"
    payment.provider_reference = str(payload["provider_reference"])[:80]
    payment.provider_status = payload["status"]
    payment.verified_at = utcnow()
    order.order_status = "failed"
    notify(
        order.client_id,
        "Payment was not completed",
        f"Payment for order {order.order_reference} was not successful. No tickets were issued.",
        "/client/payments",
    )
    audit(None, "payment_failed", "payment", payment.id, "Verified provider notification reported failure.")
    db.session.commit()
    return {"ok": True, "duplicate": False, "status": "failed"}


def dispatch_simulation(payment, outcome, method_key):
    """Ask the simulated provider to emit a signed notification.

    This function does not change the payment status itself.
    """
    if not simulation_mode() or not payment.is_simulated:
        return {"ok": False, "error": "This payment cannot be completed here."}
    if payment.status != "pending":
        return {"ok": True, "duplicate": True, "status": payment.status}
    if method_key not in SIMULATED_METHODS or outcome not in {"successful", "failed"}:
        return {"ok": False, "error": "Choose a payment method."}
    payment.payment_method = SIMULATED_METHODS[method_key]
    db.session.commit()
    payload = build_provider_payload(payment, outcome)
    raw = encode_payload(payload)
    signature = sign_payload(raw, current_app.config["SIMULATION_WEBHOOK_SECRET"])
    return process_webhook(raw, signature, simulated=True)
