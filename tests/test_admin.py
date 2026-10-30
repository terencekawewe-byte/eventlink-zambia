"""Administrator protection and empty reporting states."""

from app.extensions import db
from app.models import ContactMessage, Notification
from app.services.report_service import platform_report, resolve_period
from tests.factories import login, make_user


def test_admin_routes_require_an_admin(client, app):
    make_user(app)
    login(client, "client@example.com")
    assert client.get("/admin/users").status_code == 403
    assert client.get("/admin/reports").status_code == 403
    assert client.get("/admin/reports/export.csv").status_code == 403
    assert client.post("/admin/payments/1/mark-successful").status_code == 404


def test_empty_dashboard_and_report_are_zero(client, app):
    make_user(app, role="admin", email="admin@example.com", phone="+260971111199", name="Admin User")
    login(client, "admin@example.com")
    page = client.get("/admin/dashboard")
    assert page.status_code == 200
    assert b"<strong>0</strong>\n    <span>Registered clients" in page.data
    assert b"<span>Pending planner verifications</span><strong>0</strong>" in page.data
    assert b"Nothing is waiting for review right now." in page.data
    assert b'id="admin-monthly-chart-empty"' in page.data
    with app.app_context():
        period, error = resolve_period({})
        report = platform_report(period)
    assert error is None
    assert report["finance"]["total_gross"] == 0
    assert report["finance"]["simulated_amount"] == 0
    assert report["finance"]["pending_amount"] == 0
    assert report["overview"]["tickets_sold"] == 0
    assert report["best_events"] == []
    assert report["transactions"] == []
    page = client.get("/admin/reports")
    assert page.status_code == 200
    assert b"No event revenue" in page.data


def test_last_admin_cannot_be_suspended(client, app):
    admin_id = make_user(app, role="admin", email="admin@example.com", phone="+260971111199", name="Admin User")
    login(client, "admin@example.com")
    response = client.post(
        f"/admin/users/{admin_id}/suspend",
        data={"reason": "Trying to remove the only administrator"},
        follow_redirects=True,
    )
    assert b"cannot" in response.data.lower()


def test_contact_message_reaches_admin_inbox(client, app):
    admin_id = make_user(app, role="admin", email="admin@example.com", phone="+260971111199", name="Admin User")
    response = client.post(
        "/contact",
        data={
            "name": "Mwila Banda",
            "email": "mwila@example.com",
            "subject": "Refund for cancelled show",
            "message": "My event was cancelled and I would like to know about my refund.",
        },
        follow_redirects=True,
    )
    assert b"sent to the EventLink team" in response.data
    with app.app_context():
        message = ContactMessage.query.one()
        assert message.status == "new"
        alert = Notification.query.filter_by(user_id=admin_id).one()
        assert "Refund for cancelled show" in alert.message
        assert alert.link == f"/admin/messages#message-{message.id}"
        message_id = message.id

    assert client.get("/admin/messages").status_code in (302, 403)
    login(client, "admin@example.com")
    assert b"New contact message" in client.get("/admin/notifications").data
    inbox = client.get("/admin/messages?status=new")
    assert inbox.status_code == 200
    assert b"Refund for cancelled show" in inbox.data
    assert b"mwila@example.com" in inbox.data
    client.post(f"/admin/messages/{message_id}/status", data={"status": "reviewed"})
    with app.app_context():
        assert db.session.get(ContactMessage, message_id).status == "reviewed"
    assert b"Refund for cancelled show" not in client.get("/admin/messages?status=new").data
