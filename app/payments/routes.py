"""Checkout pages and provider webhook verification."""

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from app.extensions import csrf, db
from app.models import Order
from app.services.payment_service import (
    LIVE_METHODS,
    dispatch_simulation,
    process_webhook,
    simulation_mode,
    validate_payer_details,
)
from app.services.ticket_service import release_expired_reservations
from app.utils.decorators import client_required
from app.utils.validators import utcnow_naive

payments_bp = Blueprint("payments", __name__, url_prefix="/payments")


def _own_order(order_reference):
    return Order.query.filter_by(order_reference=order_reference, client_id=g.user.id).first_or_404()


def _local_phone(phone):
    return "0" + phone[4:] if phone and phone.startswith("+260") else (phone or "")


def _render_checkout(order, selected="mtn", payer_phone=None, card_name=None, detail_error=None):
    payment = order.payments[0] if order.payments else None
    return render_template(
        "client/checkout.html",
        order=order,
        payment=payment,
        simulation=simulation_mode() and payment is not None and payment.is_simulated,
        selected=selected if selected in LIVE_METHODS else "mtn",
        payer_phone=_local_phone(g.user.phone) if payer_phone is None else payer_phone,
        card_name=g.user.full_name if card_name is None else card_name,
        detail_error=detail_error,
    )


def _details_or_error(order):
    """Validate step 2. Returns the checked details, or a rendered page with the problem shown."""
    method = request.form.get("method")
    details, error = validate_payer_details(method, request.form)
    if error:
        page = _render_checkout(
            order,
            selected=method,
            payer_phone=request.form.get("payer_phone", ""),
            card_name=request.form.get("card_name", ""),
            detail_error=error,
        )
        return None, page
    return details, None


@payments_bp.route("/<order_reference>")
@client_required
def show(order_reference):
    release_expired_reservations()
    return _render_checkout(_own_order(order_reference))


@payments_bp.route("/<order_reference>/pay", methods=["POST"])
@client_required
def simulate(order_reference):
    if not simulation_mode():
        abort(404)
    order = _own_order(order_reference)
    payment = next((item for item in order.payments if item.status == "pending"), None)
    if order.order_status != "pending" or payment is None:
        flash("This order is no longer awaiting payment.", "info")
        return redirect(url_for("payments.show", order_reference=order.order_reference))
    if order.expires_at <= utcnow_naive():
        release_expired_reservations()
        flash("The reservation expired before payment was verified.", "warning")
        return redirect(url_for("events.detail", event_id=order.event_id))
    outcome = request.form.get("outcome")
    details = None
    if outcome == "successful":
        details, page = _details_or_error(order)
        if page is not None:
            return page, 400
    result = dispatch_simulation(payment, outcome, request.form.get("method"))
    if not result.get("ok"):
        flash(result.get("error") or "The payment could not be verified. Please try again.", "danger")
    elif result.get("status") == "successful":
        paid_from = f" Paid from {details['label']}." if details and details["kind"] == "mobile" else ""
        flash(f"Payment confirmed.{paid_from} Your tickets are ready.", "success")
        return redirect(url_for("client.tickets"))
    elif result.get("status") == "failed":
        flash("The payment was cancelled. No tickets were issued.", "warning")
    else:
        flash("The payment notification was already processed.", "info")
    return redirect(url_for("payments.show", order_reference=order.order_reference))


@payments_bp.route("/<order_reference>/method", methods=["POST"])
@client_required
def live_method(order_reference):
    if simulation_mode():
        abort(404)
    order = _own_order(order_reference)
    payment = next((item for item in order.payments if item.status == "pending"), None)
    method = LIVE_METHODS.get(request.form.get("method"))
    if payment is None or not method:
        flash("Choose a payment method while the order is still pending.", "danger")
    else:
        _details, page = _details_or_error(order)
        if page is not None:
            return page, 400
        payment.payment_method = method
        db.session.commit()
        flash(
            "Your payment method was saved. EventLink will update this order only after the provider verifies it. Do not enter a PIN or card security code here.",
            "info",
        )
    return redirect(url_for("payments.show", order_reference=order.order_reference))


def _webhook(simulated):
    secret_ready = (
        current_app.config.get("SIMULATION_WEBHOOK_SECRET")
        if simulated
        else current_app.config.get("PAYMENT_WEBHOOK_SECRET")
    )
    if not secret_ready:
        return jsonify({"ok": False}), 400
    result = process_webhook(request.get_data(), request.headers.get("X-EventLink-Signature", ""), simulated)
    status = 200 if result.get("ok") else 400
    return jsonify({"ok": bool(result.get("ok")), "duplicate": bool(result.get("duplicate"))}), status


@payments_bp.route("/webhook/simulation", methods=["POST"])
@csrf.exempt
def simulation_webhook():
    return _webhook(True)


@payments_bp.route("/webhook/live", methods=["POST"])
@csrf.exempt
def live_webhook():
    return _webhook(False)
