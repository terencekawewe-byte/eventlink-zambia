"""Client ownership and booking requests."""

from datetime import timedelta

from app.extensions import db
from app.models import PlannerBooking
from app.models.database import utcnow
from tests.factories import login, make_event, make_planner, make_user


def test_client_cannot_view_another_clients_ticket(client, app):
    owner = make_user(app, email="owner@example.com", phone="+260971111121")
    other = make_user(app, email="other@example.com", phone="+260971111122", name="Other Client")
    planner_id, _profile = make_planner(app, approved=True)
    event_id, ticket_id = make_event(app, planner_id, quantity=2, price=15000)
    with app.app_context():
        from app.models import User
        from app.services.payment_service import dispatch_simulation, start_checkout

        buyer = db.session.get(User, owner)
        from app.models import Event, TicketType

        event = db.session.get(Event, event_id)
        ticket_type = db.session.get(TicketType, ticket_id)
        order, error = start_checkout(buyer, event, ticket_type, 1)
        assert error is None
        payment = order.payments[0]
        result = dispatch_simulation(payment, "successful", "mtn")
        assert result["status"] == "successful"
        from app.models import Ticket

        ticket = Ticket.query.filter_by(client_id=owner).one()
        ticket_pk = ticket.id
    login(client, "other@example.com")
    response = client.get(f"/client/tickets/{ticket_pk}")
    assert response.status_code == 404


def test_client_booking_requires_future_date(client, app):
    _user_id, profile_id = make_planner(app, approved=True)
    make_user(app)
    login(client, "client@example.com")
    yesterday = (utcnow() - timedelta(days=1)).date().isoformat()
    response = client.post(
        f"/client/bookings/new/{profile_id}",
        data={
            "event_type": "Weddings",
            "event_date": yesterday,
            "start_time": "18:00",
            "duration": "4",
            "venue": "Civic Centre",
            "guest_count": "80",
            "budget": "5000",
            "requirements": "Need decor, sound, and a coordinator on site.",
            "contact_method": "phone",
        },
        follow_redirects=True,
    )
    assert b"future" in response.data.lower()
    with app.app_context():
        assert PlannerBooking.query.count() == 0
