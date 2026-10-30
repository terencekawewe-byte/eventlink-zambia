"""Planner approval, access control, and booking conflicts."""

from datetime import date, time, timedelta

from app.extensions import db
from app.models import PlannerBooking, PlannerProfile
from app.models.database import utcnow
from app.services.booking_service import has_conflict
from tests.factories import login, make_planner, make_user


def test_unapproved_planner_cannot_open_planner_tools(client, app):
    make_planner(app, approved=False)
    login(client, "planner@example.com")
    response = client.get("/planner/dashboard", follow_redirects=True)
    assert b"not approved" in response.data.lower() or b"waiting" in response.data.lower()
    create = client.get("/planner/events/create")
    assert create.status_code in {302, 403} or b"not approved" in create.data.lower()


def test_admin_can_approve_planner(client, app):
    _user_id, profile_id = make_planner(app, approved=False)
    make_user(app, role="admin", email="admin@example.com", phone="+260971111199", name="Admin User")
    login(client, "admin@example.com")
    listing = client.get("/admin/planners")
    assert listing.status_code == 200
    assert b"Amina Events" in listing.data
    response = client.post(f"/admin/planners/{profile_id}/approve", follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        profile = db.session.get(PlannerProfile, profile_id)
        assert profile.approval_status == "approved"
    client.post("/auth/logout")
    login(client, "planner@example.com")
    dashboard = client.get("/planner/dashboard")
    assert dashboard.status_code == 200
    assert b"Amina Events" in dashboard.data


def test_admin_planner_details_page_shows_review_actions(client, app):
    _user_id, profile_id = make_planner(app, approved=False)
    make_user(app, role="admin", email="admin@example.com", phone="+260971111199", name="Admin User")
    login(client, "admin@example.com")
    url = f"/admin/planners/{profile_id}"

    html = client.get(url).get_data(as_text=True)
    assert "Planner Details" in html and "Back to Planners" in html
    assert 'href="/admin/planners">Planner Verification</a>' in html
    for text in ("Amina Events", "Amina Banda", "planner@example.com", "+260971111112", "Lusaka", "Full planning", "Pending review"):
        assert text in html
    assert html.count('<li>Weddings</li>') == 1 and '<li>graduations</li>' in html
    assert f'action="/admin/planners/{profile_id}/approve"' in html
    assert f'action="/admin/planners/{profile_id}/reject"' in html
    assert 'name="reason"' in html and 'minlength="5"' in html
    assert html.count("data-confirm=") == 2
    assert f'action="/admin/planners/{profile_id}/suspend"' not in html

    client.post(f"{url}/reject", data={"reason": "Please add more service details."})
    html = client.get(url).get_data(as_text=True)
    assert "Please add more service details." in html and "by Admin User" in html
    assert f'action="/admin/planners/{profile_id}/approve"' in html
    assert f'action="/admin/planners/{profile_id}/reject"' not in html

    client.post(f"{url}/approve")
    html = client.get(url).get_data(as_text=True)
    assert f'action="/admin/planners/{profile_id}/suspend"' in html
    assert f'action="/admin/planners/{profile_id}/approve"' not in html
    with app.app_context():
        assert db.session.get(PlannerProfile, profile_id).approval_status == "approved"


def test_planner_cannot_open_another_planners_booking(client, app):
    _first, profile_id = make_planner(app, approved=True)
    make_planner(app, email="second@example.com", phone="+260971111155", approved=True)
    client_id = make_user(app)
    with app.app_context():
        booking = PlannerBooking(
            client_id=client_id,
            planner_id=profile_id,
            event_type="Weddings",
            event_date=date(2027, 7, 1),
            start_time=time(10, 0),
            duration=180,
            venue="Hall",
            guest_count=40,
            budget=200000,
            requirements="A complete wedding plan with vendors.",
            contact_method="email",
            status="pending",
        )
        db.session.add(booking)
        db.session.commit()
        booking_id = booking.id
    login(client, "second@example.com")
    response = client.get(f"/planner/bookings/{booking_id}")
    assert response.status_code == 404


def test_overlapping_acceptance_is_rejected(app):
    _user_id, profile_id = make_planner(app, approved=True)
    client_id = make_user(app)
    with app.app_context():
        first = PlannerBooking(
            client_id=client_id,
            planner_id=profile_id,
            event_type="Weddings",
            event_date=date(2027, 8, 1),
            start_time=time(9, 0),
            duration=180,
            venue="Hall A",
            guest_count=30,
            budget=150000,
            requirements="Morning ceremony and photos.",
            contact_method="phone",
            status="accepted",
        )
        db.session.add(first)
        db.session.commit()
        assert has_conflict(profile_id, date(2027, 8, 1), time(11, 0), 120) is True
        assert has_conflict(profile_id, date(2027, 8, 2), time(11, 0), 120) is False
        assert utcnow() < utcnow() + timedelta(seconds=1)
