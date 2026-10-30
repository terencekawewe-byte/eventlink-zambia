"""Payment verification, duplicates, and ticket issuance."""

import re

from app.extensions import db
from app.models import Order, Payment, Ticket, TicketType, User
from app.services.payment_service import (
    build_provider_payload,
    dispatch_simulation,
    encode_payload,
    process_webhook,
    sign_payload,
    start_checkout,
)
from tests.factories import login, make_event, make_planner, make_user


def _reserved_order(app):
    planner_id, _profile = make_planner(app, approved=True)
    event_id, ticket_id = make_event(app, planner_id, quantity=4, price=10000)
    client_id = make_user(app)
    with app.app_context():
        buyer = db.session.get(User, client_id)
        from app.models import Event

        order, error = start_checkout(buyer, db.session.get(Event, event_id), db.session.get(TicketType, ticket_id), 2)
        assert error is None
        return order.id


def test_unverified_order_has_no_tickets(app):
    order_id = _reserved_order(app)
    with app.app_context():
        order = db.session.get(Order, order_id)
        assert order.order_status == "pending"
        assert order.total_amount == 20000
        assert Ticket.query.count() == 0
        assert order.payments[0].is_simulated is True


def test_bad_signature_and_amount_do_not_issue_tickets(app):
    order_id = _reserved_order(app)
    with app.app_context():
        payment = Payment.query.filter_by(order_id=order_id).one()
        payload = build_provider_payload(payment, "successful")
        raw = encode_payload(payload)
        rejected = process_webhook(raw, "not-a-signature", True)
        assert rejected["ok"] is False
        payload["amount"] = 1
        tampered = encode_payload(payload)
        signature = sign_payload(tampered, app.config["SIMULATION_WEBHOOK_SECRET"])
        mismatch = process_webhook(tampered, signature, True)
        assert mismatch["ok"] is False
        assert Ticket.query.count() == 0
        assert db.session.get(Payment, payment.id).status == "pending"


def test_verified_webhook_issues_tickets_once(app):
    order_id = _reserved_order(app)
    with app.app_context():
        payment = Payment.query.filter_by(order_id=order_id).one()
        payload = build_provider_payload(payment, "successful")
        raw = encode_payload(payload)
        signature = sign_payload(raw, app.config["SIMULATION_WEBHOOK_SECRET"])
        first = process_webhook(raw, signature, True)
        second = process_webhook(raw, signature, True)
        assert first["status"] == "successful"
        assert second["duplicate"] is True
        assert Ticket.query.count() == 2
        assert db.session.get(Order, order_id).order_status == "paid"


def test_failed_verification_releases_stock(app):
    order_id = _reserved_order(app)
    with app.app_context():
        payment = Payment.query.filter_by(order_id=order_id).one()
        result = dispatch_simulation(payment, "failed", "zamtel")
        assert result["status"] == "failed"
        assert Ticket.query.count() == 0
        ticket_type = TicketType.query.one()
        assert ticket_type.quantity_reserved == 0
        assert ticket_type.quantity_sold == 0


def _limited_event(app, limit=5):
    planner_id, _profile = make_planner(app, approved=True)
    event_id, ticket_id = make_event(app, planner_id, quantity=50, price=10000)
    with app.app_context():
        db.session.get(TicketType, ticket_id).max_per_order = limit
        db.session.commit()
    client_id = make_user(app)
    return event_id, ticket_id, client_id


def _checkout(app, client_id, event_id, ticket_id, quantity):
    from app.models import Event

    with app.app_context():
        return start_checkout(
            db.session.get(User, client_id), db.session.get(Event, event_id), db.session.get(TicketType, ticket_id), quantity
        )


def test_max_per_customer_applies_across_orders(app):
    event_id, ticket_id, client_id = _limited_event(app, limit=5)
    order, error = _checkout(app, client_id, event_id, ticket_id, 6)
    assert order is None and "at most 5" in error
    first, error = _checkout(app, client_id, event_id, ticket_id, 3)
    assert error is None
    second, error = _checkout(app, client_id, event_id, ticket_id, 3)
    assert second is None
    assert "You already have 3, so you can buy up to 2 more." in error
    third, error = _checkout(app, client_id, event_id, ticket_id, 2)
    assert error is None
    fourth, error = _checkout(app, client_id, event_id, ticket_id, 1)
    assert fourth is None and "already have the maximum of 5" in error
    with app.app_context():
        assert db.session.get(TicketType, ticket_id).quantity_reserved == 5


def test_failed_or_cancelled_orders_do_not_count_towards_limit(app):
    event_id, ticket_id, client_id = _limited_event(app, limit=5)
    _order, error = _checkout(app, client_id, event_id, ticket_id, 5)
    assert error is None
    with app.app_context():
        payment = Payment.query.join(Order).filter(Order.client_id == client_id).one()
        assert dispatch_simulation(payment, "failed", "mtn")["status"] == "failed"
    again, error = _checkout(app, client_id, event_id, ticket_id, 5)
    assert error is None and again is not None


def test_limit_is_per_customer_not_shared(app):
    event_id, ticket_id, client_id = _limited_event(app, limit=2)
    other_id = make_user(app, email="other@example.com", phone="+260971111188", name="Other Client")
    assert _checkout(app, client_id, event_id, ticket_id, 2)[1] is None
    assert _checkout(app, other_id, event_id, ticket_id, 2)[1] is None


def test_event_page_shows_remaining_allowance(client, app):
    event_id, ticket_id, client_id = _limited_event(app, limit=5)
    login(client, "client@example.com")
    page = client.get(f"/events/{event_id}").get_data(as_text=True)
    assert "max 5 per customer" in page
    assert 'data-max="5"' in page and 'max="5"' in page
    _checkout(app, client_id, event_id, ticket_id, 3)
    page = client.get(f"/events/{event_id}").get_data(as_text=True)
    assert "You already have 3, so you can buy 2 more." in page
    assert 'data-max="2"' in page
    _checkout(app, client_id, event_id, ticket_id, 2)
    page = client.get(f"/events/{event_id}").get_data(as_text=True)
    assert "You have reached the limit of 5 for this ticket." in page
    assert "Continue to checkout" not in page
    token = re.search(r'name="checkout_token" value="([^"]+)"', page).group(1)
    blocked = client.post(
        f"/events/{event_id}/purchase",
        data={"ticket_type_id": str(ticket_id), "quantity": "1", "checkout_token": token},
        follow_redirects=True,
    )
    assert "already have the maximum of 5" in blocked.get_data(as_text=True)
    with app.app_context():
        assert Order.query.filter_by(client_id=client_id).count() == 2


def test_checkout_page_offers_methods_without_simulation_wording(client, app):
    order_id = _reserved_order(app)
    with app.app_context():
        reference = db.session.get(Order, order_id).order_reference
    login(client, "client@example.com")
    page = client.get(f"/payments/{reference}").get_data(as_text=True)
    for value, label in (("mtn", "MTN Mobile Money"), ("airtel", "Airtel Money"), ("zamtel", "Zamtel Money"), ("card", "Card")):
        assert f'name="method" value="{value}"' in page
        assert label in page
    assert "Pay now · K200.00" in page
    assert "never asks for your mobile money PIN" in page
    assert 'name="payer_phone"' in page and 'value="0971111111"' in page
    assert 'name="card_name"' in page
    assert "card_number" not in page and "cvv" not in page.lower()
    assert "simulat" not in page.lower()
    assert "no real money" not in page.lower()


def test_checkout_pay_and_cancel_buttons(client, app):
    order_id = _reserved_order(app)
    with app.app_context():
        reference = db.session.get(Order, order_id).order_reference
    login(client, "client@example.com")
    paid = client.post(
        f"/payments/{reference}/pay",
        data={"method": "airtel", "payer_phone": "077 123 4567", "outcome": "successful"},
        follow_redirects=True,
    )
    assert "Payment confirmed. Paid from 077 *** 4567. Your tickets are ready." in paid.get_data(as_text=True)
    with app.app_context():
        order = db.session.get(Order, order_id)
        assert order.order_status == "paid"
        assert order.payments[0].payment_method == "simulation:airtel"
        assert Ticket.query.count() == 2
    done = client.get(f"/payments/{reference}").get_data(as_text=True)
    assert "Your tickets are ready" in done and "Airtel Money" in done
    assert "simulat" not in done.lower()
    history = client.get("/client/payments").get_data(as_text=True)
    assert "Airtel Money" in history and "simulat" not in history.lower()


def test_checkout_cancel_releases_reservation(client, app):
    order_id = _reserved_order(app)
    with app.app_context():
        reference = db.session.get(Order, order_id).order_reference
    login(client, "client@example.com")
    cancelled = client.post(f"/payments/{reference}/pay", data={"method": "mtn", "payer_phone": "", "outcome": "failed"}, follow_redirects=True)
    html = cancelled.get_data(as_text=True)
    assert "The payment was cancelled. No tickets were issued." in html
    assert "Payment not completed" in html
    with app.app_context():
        assert db.session.get(Order, order_id).order_status == "failed"
        assert TicketType.query.one().quantity_reserved == 0
        assert Ticket.query.count() == 0


def test_mobile_money_number_must_match_chosen_network(client, app):
    order_id = _reserved_order(app)
    with app.app_context():
        reference = db.session.get(Order, order_id).order_reference
    login(client, "client@example.com")
    for method, number, message in (
        ("mtn", "", "Enter the MTN mobile money number to pay from"),
        ("mtn", "12345", "Enter the MTN mobile money number to pay from"),
        ("mtn", "0971234567", "That number is not on MTN. MTN numbers start with 096 or 076."),
        ("airtel", "+260961234567", "That number is not on Airtel. Airtel numbers start with 097 or 077."),
        ("zamtel", "0761234567", "That number is not on Zamtel. Zamtel numbers start with 095 or 075."),
    ):
        response = client.post(
            f"/payments/{reference}/pay", data={"method": method, "payer_phone": number, "outcome": "successful"}
        )
        html = response.get_data(as_text=True)
        assert response.status_code == 400
        assert message in html
        assert re.search(rf'name="method" value="{method}"[^>]*checked>', html)
        assert len(re.findall(r'name="method"[^>]*checked>', html)) == 1
        assert f'value="{number}"' in html
    with app.app_context():
        order = db.session.get(Order, order_id)
        assert order.order_status == "pending"
        assert order.payments[0].status == "pending"
        assert Ticket.query.count() == 0


def test_valid_numbers_for_each_network_are_accepted(app):
    from app.services.payment_service import validate_payer_details

    with app.app_context():
        for method, number in (
            ("mtn", "0961234567"),
            ("mtn", "260761234567"),
            ("airtel", "+260 97 123 4567"),
            ("zamtel", "0751234567"),
        ):
            details, error = validate_payer_details(method, {"payer_phone": number})
            assert error is None, (method, number, error)
            assert details["label"].endswith("4567") and "***" in details["label"]
        assert validate_payer_details("card", {"card_name": "  Client   One "})[0]["label"] == "Client One"
        assert validate_payer_details("card", {"card_name": "4111111111111111"})[1]
        assert validate_payer_details("cash", {})[1] == "Choose a payment method."


def test_card_payment_needs_only_name_on_card(client, app):
    order_id = _reserved_order(app)
    with app.app_context():
        reference = db.session.get(Order, order_id).order_reference
    login(client, "client@example.com")
    missing = client.post(f"/payments/{reference}/pay", data={"method": "card", "card_name": "", "outcome": "successful"})
    assert missing.status_code == 400
    assert "Enter the name exactly as it appears on the card." in missing.get_data(as_text=True)
    paid = client.post(
        f"/payments/{reference}/pay",
        data={"method": "card", "card_name": "Client One", "outcome": "successful"},
        follow_redirects=True,
    )
    assert "Payment confirmed. Your tickets are ready." in paid.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Order, order_id).payments[0].payment_method == "simulation:card"
        assert Ticket.query.count() == 2
