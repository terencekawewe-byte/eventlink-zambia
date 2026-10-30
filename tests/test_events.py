"""Event submission and publication rules."""

from io import BytesIO

from PIL import Image

from app.extensions import db
from app.models import Event
from tests.factories import login, make_planner, make_user


def _png():
    buffer = BytesIO()
    Image.new("RGB", (20, 20), (14, 107, 79)).save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def test_planner_submission_requires_admin_approval(client, app):
    make_planner(app, approved=True)
    make_user(app, role="admin", email="admin@example.com", phone="+260971111199", name="Admin User")
    login(client, "planner@example.com")
    response = client.post(
        "/planner/events/create",
        data={
            "action": "submit",
            "title": "Lusaka Jazz Night",
            "category_id": "1",
            "description": "An evening of jazz, food, and a managed guest list at a Lusaka venue.",
            "venue": "Lusaka Playhouse",
            "location": "Lusaka",
            "start_datetime": "2027-07-10T18:00",
            "end_datetime": "2027-07-10T22:00",
            "sales_start": "2027-06-01T09:00",
            "sales_end": "2027-07-10T17:00",
            "contact_info": "+260971111112",
            "ticket_name": "Ordinary",
            "ticket_price": "100.00",
            "ticket_qty": "50",
            "ticket_max": "6",
            "ticket_desc": "Standard entry",
            "poster": (_png(), "poster.png"),
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    with app.app_context():
        event = Event.query.filter_by(title="Lusaka Jazz Night").one()
        assert event.approval_status == "pending"
        assert event.publication_status == "unpublished"
        assert event.is_public is False
        event_id = event.id
    client.post("/auth/logout")
    hidden = client.get(f"/events/{event_id}")
    assert hidden.status_code == 404
    client.post("/auth/logout")
    login(client, "admin@example.com")
    approved = client.post(f"/admin/events/{event_id}/approve", follow_redirects=True)
    assert approved.status_code == 200
    with app.app_context():
        event = db.session.get(Event, event_id)
        assert event.approval_status == "approved"
        assert event.publication_status == "published"
    public = client.get(f"/events/{event_id}")
    assert public.status_code == 200
    assert b"Lusaka Jazz Night" in public.data


def test_text_file_is_rejected_as_poster(client, app):
    make_planner(app, approved=True)
    login(client, "planner@example.com")
    response = client.post(
        "/planner/events/create",
        data={
            "action": "submit",
            "title": "Broken Upload Night",
            "category_id": "1",
            "description": "This submission uses a file that is not a real image.",
            "venue": "Hall",
            "location": "Ndola",
            "start_datetime": "2027-08-10T18:00",
            "end_datetime": "2027-08-10T22:00",
            "sales_start": "2027-07-01T09:00",
            "sales_end": "2027-08-10T17:00",
            "contact_info": "+260971111112",
            "ticket_name": "VIP",
            "ticket_price": "250",
            "ticket_qty": "10",
            "ticket_max": "2",
            "ticket_desc": "Front row",
            "poster": (BytesIO(b"not an image"), "poster.png"),
        },
        follow_redirects=True,
    )
    assert b"image" in response.data.lower()
    with app.app_context():
        assert Event.query.filter_by(title="Broken Upload Night").count() == 0


def test_public_information_pages_render(client):
    about = client.get("/about")
    assert about.status_code == 200
    assert b"A home for Zambian events" in about.data
    assert b"Prices in Kwacha" in about.data
    guide = client.get("/how-it-works")
    assert guide.status_code == 200
    assert b"Booking a planner" in guide.data
