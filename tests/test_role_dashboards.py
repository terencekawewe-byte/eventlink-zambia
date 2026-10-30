"""Role dashboards show real records and use the existing workflows."""

import json
import re
from datetime import date, time

from app.extensions import db
from app.models import Event, PlannerBooking, Ticket, TicketType, User
from app.services.payment_service import build_provider_payload, encode_payload, process_webhook, sign_payload, start_checkout
from tests.factories import login, make_event, make_planner, make_user


def _buy(app, client_id, event_id, ticket_type_id, quantity=2):
    with app.app_context():
        order, error = start_checkout(
            db.session.get(User, client_id),
            db.session.get(Event, event_id),
            db.session.get(TicketType, ticket_type_id),
            quantity,
        )
        assert error is None
        raw = encode_payload(build_provider_payload(order.payments[0], "successful"))
        result = process_webhook(raw, sign_payload(raw, app.config["SIMULATION_WEBHOOK_SECRET"]), True)
        assert result["status"] == "successful"
        return order.order_reference


def _charts(html):
    match = re.search(r'<script id="dash-charts" type="application/json">(.*?)</script>', html, re.S)
    assert match, "chart data missing"
    return {chart["id"]: chart for chart in json.loads(match.group(1))}


def _stat(html, label):
    match = re.search(r"<strong>([^<]+)</strong>\s*<span>" + re.escape(label) + "</span>", html)
    assert match, label
    return match.group(1)


def _booking(app, client_id, profile_id, status="pending"):
    with app.app_context():
        booking = PlannerBooking(
            client_id=client_id,
            planner_id=profile_id,
            event_type="Wedding reception",
            event_date=date(2027, 3, 20),
            start_time=time(14, 0),
            duration=5,
            venue="Taj Pamodzi",
            guest_count=120,
            budget=5000000,
            requirements="Decor, catering, and music.",
            contact_method="phone",
            status=status,
        )
        db.session.add(booking)
        db.session.commit()
        return booking.id


def test_client_dashboard_discovers_only_public_events(app, client):
    planner_id, _ = make_planner(app, approved=True)
    event_id, ticket_type_id = make_event(app, planner_id, quantity=4, price=10000)
    hidden_id, _ = make_event(app, planner_id, title="Hidden Gala")
    full_id, full_type = make_event(app, planner_id, quantity=1, title="Full House")
    with app.app_context():
        db.session.get(Event, hidden_id).approval_status = "pending"
        db.session.get(Event, hidden_id).publication_status = "unpublished"
        db.session.get(TicketType, full_type).quantity_sold = 1
        db.session.commit()
    client_id = make_user(app)
    reference = _buy(app, client_id, event_id, ticket_type_id)
    login(client, "client@example.com")

    html = client.get("/client/dashboard").get_data(as_text=True)
    assert "Copperbelt Night" in html and "Full House" in html
    assert "Hidden Gala" not in html
    assert "Sold out" in html and "Get tickets" in html and "K100.00" in html
    assert "View ticket" in html
    assert f"Order {reference} placed" in html
    assert "Ticket purchase confirmed" in html
    assert _stat(html, "My tickets") == "2"
    assert _stat(html, "Upcoming events") == "1"
    assert _stat(html, "Unread notifications") == "1"
    assert 'class="status status-valid"><i class="bi bi-check-circle-fill"' in html


def test_planner_dashboard_lists_pending_requests_and_chart(app, client):
    _planner_id, profile_id = make_planner(app, approved=True)
    client_id = make_user(app)
    booking_id = _booking(app, client_id, profile_id)
    login(client, "planner@example.com")

    html = client.get("/planner/dashboard").get_data(as_text=True)
    assert "Booking requests awaiting your reply" in html
    assert "Client One" in html and "Wedding reception" in html and "Taj Pamodzi" in html
    assert f'action="/planner/bookings/{booking_id}/accept"' in html
    assert f'href="/planner/bookings/{booking_id}#reject-reason"' in html
    assert "Still missing" in html and "Profile photo" in html
    assert _stat(html, "Pending requests") == "1"
    assert _stat(html, "Confirmed bookings") == "0"
    chart = _charts(html)["planner-bookings-chart"]
    assert len(chart["labels"]) == 6
    assert chart["datasets"][0]["data"][-1] == 1
    assert chart["datasets"][1]["data"][-1] == 0

    response = client.post(f"/planner/bookings/{booking_id}/accept", follow_redirects=True)
    assert response.status_code == 200
    html = client.get("/planner/dashboard").get_data(as_text=True)
    assert "No pending requests" in html
    assert _stat(html, "Confirmed bookings") == "1"
    assert _charts(html)["planner-bookings-chart"]["datasets"][1]["data"][-1] == 1
    assert 'href="/planner/bookings/%d">Wedding reception</a>' % booking_id in html


def test_planner_dashboard_counts_only_verified_ticket_payments(app, client):
    planner_id, _ = make_planner(app, approved=True)
    event_id, ticket_type_id = make_event(app, planner_id, quantity=4, price=10000)
    client_id = make_user(app)
    _buy(app, client_id, event_id, ticket_type_id)
    with app.app_context():
        start_checkout(
            db.session.get(User, client_id), db.session.get(Event, event_id), db.session.get(TicketType, ticket_type_id), 1
        )
    login(client, "planner@example.com")
    html = client.get("/planner/dashboard").get_data(as_text=True)
    assert "Verified payments (simulation, not real money)" in html
    assert "K200.00" in html and "K300.00" not in html


def test_admin_dashboard_uses_real_records(app, client):
    make_user(app, role="admin", email="admin@example.com", phone="+260971111119", name="Admin One")
    _pending_user, pending_profile = make_planner(app)
    approved_user, _ = make_planner(app, email="second@example.com", phone="+260971111113", approved=True)
    event_id, ticket_type_id = make_event(app, approved_user, quantity=4)
    make_event(app, approved_user, quantity=100, title="Quiet Evening")
    waiting_id, _ = make_event(app, approved_user, title="Awaiting Show")
    with app.app_context():
        db.session.get(Event, waiting_id).approval_status = "pending"
        db.session.get(Event, waiting_id).publication_status = "unpublished"
        db.session.commit()
    client_id = make_user(app)
    _buy(app, client_id, event_id, ticket_type_id)
    with app.app_context():
        ticket = Ticket.query.first()
        ticket.status = "cancelled"
        db.session.commit()
    login(client, "admin@example.com")

    html = client.get("/admin/dashboard").get_data(as_text=True)
    assert _stat(html, "Registered clients") == "1"
    assert _stat(html, "Event planners") == "2"
    assert _stat(html, "Events awaiting approval") == "1"
    assert _stat(html, "Tickets sold") == "1"
    assert "1 verified · 1 pending" in html
    assert f'action="/admin/planners/{pending_profile}/approve"' in html
    assert "Awaiting Show" in html and f'href="/admin/events/{waiting_id}"' in html
    assert "Quiet Evening" in html and "Low sales" in html
    assert "Payment verified" in html and "New client registered" in html
    charts = _charts(html)
    assert charts["admin-payment-chart"]["datasets"][0]["data"] == [1, 0, 0, 0]
    assert charts["admin-monthly-chart"]["datasets"][0]["data"][-1] == 1
    assert charts["admin-event-chart"]["labels"] == ["Copperbelt Night"]


def test_header_profile_menu_and_breadcrumb(app, client):
    make_user(app)
    login(client, "client@example.com")
    html = client.get("/client/tickets").get_data(as_text=True)
    assert "Account menu" in html and 'href="/client/profile"' in html
    assert 'href="/auth/change-password"' in html and "Log out" in html
    assert 'aria-label="Breadcrumb"' in html


def test_pending_planner_header_links_to_application(app, client):
    make_planner(app)
    login(client, "planner@example.com")
    html = client.get("/planner/pending").get_data(as_text=True)
    assert "/planner/notifications" not in html
    assert "Your application is being reviewed" in html
    assert "Profile photo" in html
