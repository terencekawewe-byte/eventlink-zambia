"""Administrator list filters, user details, and the admin profile."""

from datetime import timedelta

from app.extensions import db
from app.models import AuditLog, Event, EventCategory, Payment, Ticket, User
from app.models.database import utcnow
from app.utils.helpers import to_local
from tests.factories import login, make_event, make_planner, make_user
from tests.test_role_dashboards import _buy


def _admin(app):
    return make_user(app, role="admin", email="admin@example.com", phone="+260971111199", name="Admin User")


def _sale(app):
    planner_id, _ = make_planner(app, approved=True)
    event_id, ticket_type_id = make_event(app, planner_id, quantity=4, price=10000)
    client_id = make_user(app)
    reference = _buy(app, client_id, event_id, ticket_type_id)
    return planner_id, event_id, client_id, reference


def _today():
    return to_local(utcnow()).date()


def test_user_status_filter_and_detail_page(app, client):
    admin_id = _admin(app)
    planner_id, _event_id, client_id, reference = _sale(app)
    login(client, "admin@example.com")

    assert b"No users match that search." in client.get("/admin/users?status=suspended").data
    listing = client.get("/admin/users?status=active&role=client").get_data(as_text=True)
    assert f'href="/admin/users/{client_id}"' in listing and "Amina Banda" not in listing
    assert '<option value="client" selected>' in listing and '<option value="admin" selected>' not in listing
    assert '<option value="active" selected>' in listing
    own_row = client.get("/admin/users?role=admin").get_data(as_text=True)
    assert "This is your account." in own_row and f"/admin/users/{admin_id}/suspend" not in own_row

    detail = client.get(f"/admin/users/{client_id}").get_data(as_text=True)
    assert "client@example.com" in detail and "+260971111111" in detail
    assert reference in detail and "Copperbelt Night" in detail
    assert "<strong>2</strong><span>Tickets issued</span>" in detail
    assert "K200.00 in simulation payments" in detail
    assert "No administrator actions have been recorded" in detail

    response = client.post(
        f"/admin/users/{client_id}/suspend",
        data={"reason": "Repeated chargeback attempts", "return_to": "detail"},
    )
    assert response.status_code == 302 and response.headers["Location"].endswith(f"/admin/users/{client_id}")
    detail = client.get(f"/admin/users/{client_id}").get_data(as_text=True)
    assert "Account suspended" in detail and "Repeated chargeback attempts" in detail
    assert f"/admin/users/{client_id}/reactivate" in detail
    assert b"Client One" in client.get("/admin/users?status=suspended").data

    planner_detail = client.get(f"/admin/users/{planner_id}").get_data(as_text=True)
    assert "Amina Events" in planner_detail and "Events created" in planner_detail

    self_detail = client.get(f"/admin/users/{admin_id}").get_data(as_text=True)
    assert "You cannot suspend your own account." in self_detail
    response = client.post(f"/admin/users/{admin_id}/suspend", data={"reason": "Testing self suspension"}, follow_redirects=True)
    assert b"You cannot suspend your own account." in response.data
    with app.app_context():
        assert db.session.get(User, admin_id).account_status == "active"
    assert client.get("/admin/users/9999").status_code == 404


def test_event_search_category_and_status_filters(app, client):
    _admin(app)
    planner_id, _ = make_planner(app, approved=True)
    night_id, _ = make_event(app, planner_id)
    jazz_id, _ = make_event(app, planner_id, title="Lusaka Jazz Brunch")
    with app.app_context():
        categories = EventCategory.query.order_by(EventCategory.id).all()
        assert len(categories) >= 2
        db.session.get(Event, jazz_id).category_id = categories[1].id
        db.session.get(Event, night_id).approval_status = "pending"
        db.session.commit()
        jazz_category = categories[1].id
    login(client, "admin@example.com")

    html = client.get("/admin/events?q=jazz").get_data(as_text=True)
    assert "Lusaka Jazz Brunch" in html and "Copperbelt Night" not in html
    html = client.get("/admin/events?q=Amina").get_data(as_text=True)
    assert "Lusaka Jazz Brunch" in html and "Copperbelt Night" in html
    html = client.get(f"/admin/events?category={jazz_category}").get_data(as_text=True)
    assert "Lusaka Jazz Brunch" in html and "Copperbelt Night" not in html
    html = client.get("/admin/events?status=pending&q=Heroes").get_data(as_text=True)
    assert "Copperbelt Night" in html and "Lusaka Jazz Brunch" not in html
    assert 'href="/admin/events?q=Heroes"' in html
    assert b"No events in this list." in client.get("/admin/events?q=100%25").data


def test_ticket_sales_summary_and_issued_ticket_filters(app, client):
    _admin(app)
    _sale(app)
    login(client, "admin@example.com")

    summary = client.get("/admin/ticket-sales").get_data(as_text=True)
    assert "<strong>2</strong> sold" in summary and "remaining of 4" in summary
    assert "<small>50%</small>" in summary
    assert "<strong>0</strong> sold" in client.get("/admin/ticket-sales?timing=past").get_data(as_text=True)

    with app.app_context():
        ticket = Ticket.query.first()
        ticket.status = "redeemed"
        db.session.commit()
        code = ticket.ticket_code
    issued = client.get("/admin/ticket-sales?view=issued").get_data(as_text=True)
    assert code in issued and "Client One" in issued and "2 tickets found" in issued
    redeemed = client.get("/admin/ticket-sales?view=issued&status=redeemed").get_data(as_text=True)
    assert "1 ticket found" in redeemed and code in redeemed
    today = _today()
    assert "2 tickets found" in client.get(f"/admin/ticket-sales?view=issued&date_from={today}&date_to={today}").get_data(as_text=True)
    tomorrow = today + timedelta(days=1)
    assert "0 tickets found" in client.get(f"/admin/ticket-sales?view=issued&date_from={tomorrow}").get_data(as_text=True)
    bad = client.get("/admin/ticket-sales?view=issued&date_from=2026-13-40").get_data(as_text=True)
    assert "Enter dates as YYYY-MM-DD." in bad and "2 tickets found" in bad


def test_payment_filters_by_reference_method_status_and_date(app, client):
    _admin(app)
    _planner_id, _event_id, _client_id, reference = _sale(app)
    with app.app_context():
        payment = Payment.query.one()
        payment.payment_method = "simulation:mtn"
        db.session.commit()
        payment_reference = payment.payment_reference
    login(client, "admin@example.com")

    html = client.get("/admin/payments").get_data(as_text=True)
    assert payment_reference in html and "MTN MoMo (simulation)" in html
    assert '<option value="simulation:mtn"' in html
    assert "Successful <span>K200.00</span>" in html
    assert payment_reference in client.get(f"/admin/payments?q={reference}").get_data(as_text=True)
    assert payment_reference in client.get("/admin/payments?method=simulation:mtn&status=successful").get_data(as_text=True)
    assert payment_reference not in client.get("/admin/payments?status=pending").get_data(as_text=True)
    assert payment_reference not in client.get("/admin/payments?q=NOPE-REF").get_data(as_text=True)
    yesterday = _today() - timedelta(days=1)
    old = client.get(f"/admin/payments?date_to={yesterday}").get_data(as_text=True)
    assert payment_reference not in old and "0 payments found" in old
    tabs = client.get("/admin/payments?method=simulation:mtn").get_data(as_text=True)
    assert 'href="/admin/payments?status=failed&amp;method=simulation:mtn"' in tabs


def test_audit_log_filters_and_record_links(app, client):
    _admin(app)
    _planner_id, event_id, client_id, _reference = _sale(app)
    login(client, "admin@example.com")
    client.post(f"/admin/users/{client_id}/suspend", data={"reason": "Fraud review in progress"})
    client.post(f"/admin/events/{event_id}/suspend", data={"reason": "Venue licence expired"})

    html = client.get("/admin/audit-logs?entity=user").get_data(as_text=True)
    assert "account_suspended" in html and "event_suspended" not in html
    assert f'href="/admin/users/{client_id}"' in html
    assert "Venue licence expired" in client.get("/admin/audit-logs?q=licence").get_data(as_text=True)
    future = _today() + timedelta(days=1)
    assert "0 records found" in client.get(f"/admin/audit-logs?date_from={future}").get_data(as_text=True)


def test_admin_profile_updates_details_and_is_linked(app, client):
    admin_id = _admin(app)
    make_user(app, email="other@example.com", phone="+260971111155", name="Other Client")
    login(client, "admin@example.com")

    page = client.get("/admin/profile").get_data(as_text=True)
    assert 'href="/admin/profile"' in page and "Administrator" in page
    assert "You have not taken any recorded actions yet." in page

    response = client.post("/admin/profile", data={"full_name": "Admin Mwale", "phone": "+260971111155"}, follow_redirects=True)
    assert b"already used by another account" in response.data
    response = client.post("/admin/profile", data={"full_name": "Admin Mwale", "phone": "0977000111"}, follow_redirects=True)
    assert b"Your profile has been updated." in response.data
    with app.app_context():
        admin = db.session.get(User, admin_id)
        assert admin.full_name == "Admin Mwale" and admin.phone == "+260977000111"
        assert AuditLog.query.filter_by(action="admin_profile_updated", entity_id=admin_id).count() == 1
    assert "Admin profile updated" in client.get("/admin/profile").get_data(as_text=True)


def test_new_admin_pages_require_admin(app, client):
    make_user(app)
    login(client, "client@example.com")
    for path in ("/admin/users/1", "/admin/profile", "/admin/ticket-sales?view=issued"):
        assert client.get(path).status_code == 403
    assert client.post("/admin/profile", data={"full_name": "Hacker", "phone": "0977000111"}).status_code in (400, 403)
