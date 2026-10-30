"""Ticket inventory, totals, and redemption."""

from app.extensions import db
from app.models import Order, Ticket, TicketType, User
from app.services.payment_service import dispatch_simulation, start_checkout
from app.services.ticket_service import redeem_ticket
from tests.factories import login, make_event, make_planner, make_user


def _buy(app, client_id, event_id, ticket_type_id, quantity):
    with app.app_context():
        buyer = db.session.get(User, client_id)
        from app.models import Event

        event = db.session.get(Event, event_id)
        ticket_type = db.session.get(TicketType, ticket_type_id)
        order, error = start_checkout(buyer, event, ticket_type, quantity)
        return order.id if order else None, error


def test_purchase_total_comes_from_the_database(app):
    planner_id, _profile = make_planner(app, approved=True)
    event_id, ticket_id = make_event(app, planner_id, quantity=5, price=25000)
    client_id = make_user(app)
    order_id, error = _buy(app, client_id, event_id, ticket_id, 3)
    assert error is None
    with app.app_context():
        order = db.session.get(Order, order_id)
        assert order.total_amount == 75000
        assert order.items[0].unit_price == 25000
        assert Ticket.query.count() == 0


def test_insufficient_inventory_is_rejected(app):
    planner_id, _profile = make_planner(app, approved=True)
    event_id, ticket_id = make_event(app, planner_id, quantity=1, price=10000)
    client_id = make_user(app)
    _order_id, error = _buy(app, client_id, event_id, ticket_id, 2)
    assert error is not None
    assert "enough" in error.lower()


def test_second_purchase_cannot_oversell(app):
    planner_id, _profile = make_planner(app, approved=True)
    event_id, ticket_id = make_event(app, planner_id, quantity=1, price=10000)
    first = make_user(app, email="one@example.com", phone="+260971111141")
    second = make_user(app, email="two@example.com", phone="+260971111142", name="Second Client")
    order_id, error = _buy(app, first, event_id, ticket_id, 1)
    assert error is None
    with app.app_context():
        order = db.session.get(Order, order_id)
        result = dispatch_simulation(order.payments[0], "successful", "airtel")
        assert result["status"] == "successful"
    _other, second_error = _buy(app, second, event_id, ticket_id, 1)
    assert second_error is not None


def test_ticket_can_be_redeemed_only_once(app):
    planner_id, _profile = make_planner(app, approved=True)
    event_id, ticket_id = make_event(app, planner_id, quantity=1, price=10000)
    client_id = make_user(app)
    order_id, _error = _buy(app, client_id, event_id, ticket_id, 1)
    with app.app_context():
        order = db.session.get(Order, order_id)
        dispatch_simulation(order.payments[0], "successful", "card")
        ticket = Ticket.query.one()
        organiser = db.session.get(User, planner_id)
        assert redeem_ticket(ticket, organiser) == "accepted"
        assert redeem_ticket(ticket, organiser) == "already_redeemed"
        assert Ticket.query.filter_by(status="redeemed").count() == 1
