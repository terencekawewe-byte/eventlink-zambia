"""Dashboard sidebar layout, page rendering, and access after logout."""

from datetime import date, time

from app.extensions import db
from app.models import Event, PlannerBooking, Quotation, Ticket, TicketType, User
from app.services.payment_service import build_provider_payload, encode_payload, process_webhook, sign_payload, start_checkout
from tests.factories import login, make_event, make_planner, make_user

CLIENT_PAGES = [
    "/client/dashboard",
    "/client/tickets",
    "/client/bookings",
    "/client/payments",
    "/client/notifications",
    "/client/profile",
    "/auth/change-password",
]
PLANNER_PAGES = [
    "/planner/dashboard",
    "/planner/profile",
    "/planner/profile/edit",
    "/planner/services",
    "/planner/bookings",
    "/planner/quotations",
    "/planner/events",
    "/planner/events/create",
    "/planner/reports",
    "/planner/notifications",
]
ADMIN_PAGES = [
    "/admin/dashboard",
    "/admin/users",
    "/admin/planners",
    "/admin/events",
    "/admin/ticket-sales",
    "/admin/orders",
    "/admin/payments",
    "/admin/bookings",
    "/admin/reports",
    "/admin/messages",
    "/admin/notifications",
    "/admin/audit-logs",
    "/admin/settings",
]


def _assert_sidebar_pages(client, pages):
    for path in pages:
        response = client.get(path)
        assert response.status_code == 200, path
        html = response.get_data(as_text=True)
        assert 'id="dashSidebar"' in html, path
        assert 'class="dash-link active"' in html, path
        assert "dash-nav" not in html, path
        assert response.headers.get("Cache-Control") == "no-store", path


def test_client_sidebar_pages_render(app, client):
    make_user(app)
    login(client, "client@example.com")
    _assert_sidebar_pages(client, CLIENT_PAGES)
    html = client.get("/client/tickets").get_data(as_text=True)
    assert 'class="dash-link active" href="/client/tickets" aria-current="page"' in html
    assert "/planner/dashboard" not in html and "/admin/dashboard" not in html


def test_client_dashboard_shows_real_counts(app, client):
    make_user(app)
    login(client, "client@example.com")
    html = client.get("/client/dashboard").get_data(as_text=True)
    assert "Welcome back, Client One" in html
    assert "No upcoming tickets" in html
    assert "No upcoming public events" in html
    assert "Unread notifications" in html and "Active planner bookings" in html


def test_client_pages_show_verified_purchase(app, client):
    planner_id, _profile = make_planner(app, approved=True)
    event_id, ticket_type_id = make_event(app, planner_id, quantity=4, price=10000)
    client_id = make_user(app)
    with app.app_context():
        order, error = start_checkout(
            db.session.get(User, client_id), db.session.get(Event, event_id), db.session.get(TicketType, ticket_type_id), 2
        )
        assert error is None
        raw = encode_payload(build_provider_payload(order.payments[0], "successful"))
        result = process_webhook(raw, sign_payload(raw, app.config["SIMULATION_WEBHOOK_SECRET"]), True)
        assert result["status"] == "successful"
        code = Ticket.query.first().ticket_code
    login(client, "client@example.com")

    dashboard = client.get("/client/dashboard").get_data(as_text=True)
    assert "K200.00" in dashboard
    assert "Copperbelt Night" in dashboard and "Heroes Stadium" in dashboard

    tickets = client.get("/client/tickets").get_data(as_text=True)
    assert code in tickets and "Heroes Stadium" in tickets and "Lusaka" in tickets

    payments = client.get("/client/payments").get_data(as_text=True)
    assert "K200.00" in payments and "Verified" in payments and "Successful" in payments


def test_client_bookings_show_planner_and_latest_quotation(app, client):
    _planner_id, profile_id = make_planner(app, approved=True)
    client_id = make_user(app)
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
            status="accepted",
        )
        db.session.add(booking)
        db.session.flush()
        db.session.add(Quotation(booking_id=booking.id, amount=4500000, description="Full package", status="pending"))
        db.session.commit()
    login(client, "client@example.com")
    html = client.get("/client/bookings").get_data(as_text=True)
    assert "Wedding reception" in html and "Amina Events" in html
    assert "20 Mar 2027" in html and "K45,000.00" in html
    dashboard = client.get("/client/dashboard").get_data(as_text=True)
    assert "0 awaiting a planner reply" in dashboard
    assert "Wedding reception" in dashboard and "K45,000.00 (pending)" in dashboard


def test_planner_sidebar_pages_render(app, client):
    make_planner(app, approved=True)
    login(client, "planner@example.com")
    _assert_sidebar_pages(client, PLANNER_PAGES)


def test_pending_planner_sidebar_is_limited(app, client):
    make_planner(app)
    login(client, "planner@example.com")
    html = client.get("/planner/pending").get_data(as_text=True)
    assert "Application Status" in html
    assert "/planner/services" not in html


def test_admin_sidebar_pages_render(app, client):
    admin_id = make_user(app, role="admin", email="admin@example.com", phone="+260971111119", name="Admin One")
    make_event(app, admin_id)
    login(client, "admin@example.com")
    _assert_sidebar_pages(client, ADMIN_PAGES)


def test_dashboard_pages_require_login_after_logout(app, client):
    make_user(app)
    login(client, "client@example.com")
    assert client.get("/client/tickets").status_code == 200
    client.post("/auth/logout")
    for path in CLIENT_PAGES:
        response = client.get(path)
        assert response.status_code == 302, path
        assert "/auth/login" in response.headers["Location"], path
