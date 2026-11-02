"""Gate attendants, QR ticket verification, and the scan audit trail."""

import os
import re
import sqlite3
import tempfile
import threading

from app import create_app
from app.extensions import db
from app.models import AttendantAssignment, Event, GateAttendant, Order, Ticket, TicketRedemption, TicketType, User
from app.models.migrations import rebuild_tables
from app.services.gate_service import parse_scan
from app.services.payment_service import dispatch_simulation, start_checkout
from config import TestingConfig
from tests.factories import login, make_event, make_planner, make_user

JSON = {"Accept": "application/json", "X-Requested-With": "fetch"}
TEMP_PASSWORD = re.compile(r'class="temp-password">([^<]+)<')
NEW_PASSWORD = "GateKeeper42"


def _issue_ticket(app, client_id, event_id, ticket_type_id):
    with app.app_context():
        order, error = start_checkout(
            db.session.get(User, client_id), db.session.get(Event, event_id), db.session.get(TicketType, ticket_type_id), 1
        )
        assert error is None
        assert dispatch_simulation(order.payments[0], "successful", "mtn")["status"] == "successful"
        ticket = Ticket.query.filter_by(order_item_id=order.items[0].id).one()
        return {"id": ticket.id, "code": ticket.ticket_code, "token": ticket.qr_token}


def _create_attendant(client, event_id=None, email="gate1@example.com", phone="0977000001", name="Mutale Phiri", **extra):
    data = {"full_name": name, "email": email, "phone": phone, "event_id": str(event_id or ""), **extra}
    response = client.post("/planner/attendants", data=data)
    match = TEMP_PASSWORD.search(response.get_data(as_text=True))
    return response, (match.group(1) if match else None)


def _attendant_login(app, email, temporary):
    gate = app.test_client()
    page = login(gate, email, temporary)
    assert "Choose a new password" in page.get_data(as_text=True)
    changed = gate.post(
        "/auth/change-password",
        data={"current_password": temporary, "password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD},
        follow_redirects=True,
    )
    assert changed.status_code == 200
    return gate


def _scan(gate, event_id, code, method="camera"):
    response = gate.post(f"/gate/events/{event_id}/verify", data={"code": code, "method": method}, headers=JSON)
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    return response.get_json()


def _setup(app):
    planner_id, _ = make_planner(app, approved=True)
    event_id, type_id = make_event(app, planner_id, quantity=10, title="Copperbelt Night")
    client_id = make_user(app, name="Chanda Mwila")
    return planner_id, event_id, type_id, client_id


def test_parse_scan_accepts_only_ticket_formats():
    token = "A" * 43
    assert parse_scan(f"https://192.168.1.20:5000/tickets/verify/{token}") == ("token", token)
    assert parse_scan(token) == ("token", token)
    assert parse_scan(" elz-0a1b2c3d-9f9f ") == ("code", "ELZ-0A1B2C3D-9F9F")
    assert parse_scan("https://example.com/") == (None, None)
    assert parse_scan("ELZ-123") == (None, None)
    assert parse_scan("") == (None, None)


def test_full_gate_workflow(app):
    planner_id, event_id, type_id, client_id = _setup(app)
    ticket = _issue_ticket(app, client_id, event_id, type_id)

    planner = app.test_client()
    login(planner, "planner@example.com")
    response, temporary = _create_attendant(planner, event_id)
    assert response.status_code == 200 and temporary
    with app.app_context():
        user = User.query.filter_by(email="gate1@example.com").one()
        assert user.role == "attendant"
        assert user.gate_profile.created_by_id == planner_id
        assert user.gate_profile.must_change_password is True
        assert [a.event_id for a in user.gate_profile.active_assignments] == [event_id]

    gate = _attendant_login(app, "gate1@example.com", temporary)
    dashboard = gate.get("/gate/").get_data(as_text=True)
    assert "Copperbelt Night" in dashboard and "Scan Ticket" in dashboard
    scan_page = gate.get(f"/gate/events/{event_id}/scan").get_data(as_text=True)
    assert "html5-qrcode.min.js" in scan_page and "Type the ticket code" in scan_page

    first = _scan(gate, event_id, f"http://127.0.0.1:5000/tickets/verify/{ticket['token']}")
    assert first["outcome"]["result"] == "valid"
    assert first["outcome"]["message"] == "Ticket verified successfully. Allow entry."
    assert first["outcome"]["ticket"]["holder"] == "Chanda Mwila"
    assert first["outcome"]["ticket"]["category"] == "Ordinary"
    assert first["outcome"]["ticket"]["code"] == ticket["code"]
    assert first["stats"] == {"expected": 1, "scanned": 1, "remaining": 0, "rejected": 0}

    second = _scan(gate, event_id, ticket["code"].lower(), method="manual")
    assert second["outcome"]["result"] == "already_used"
    assert second["outcome"]["message"] == "This ticket has already been used. Do not allow entry again."
    assert second["outcome"]["ticket"]["redeemed_at"]

    fake = _scan(gate, event_id, "ELZ-00000000-0000", method="manual")
    assert fake["outcome"]["result"] == "invalid"
    assert fake["outcome"]["reason"] == "No ticket matches this code."
    junk = _scan(gate, event_id, "https://example.com/promo")
    assert junk["outcome"]["result"] == "invalid"
    assert junk["stats"]["rejected"] == 3

    with app.app_context():
        assert db.session.get(Ticket, ticket["id"]).status == "redeemed"
        rows = TicketRedemption.query.order_by(TicketRedemption.id).all()
        assert [row.result for row in rows] == ["valid", "already_used", "invalid", "invalid"]
        assert {row.event_id for row in rows} == {event_id}
        assert rows[0].method == "camera" and rows[1].method == "manual"

    history = gate.get("/gate/history").get_data(as_text=True)
    assert ticket["code"] in history and "Already used" in history

    activity = planner.get("/planner/scan-activity").get_data(as_text=True)
    assert ticket["code"] in activity and "Mutale Phiri" in activity

    make_user(app, role="admin", email="admin@example.com", phone="+260971111199", name="Admin One")
    admin = app.test_client()
    login(admin, "admin@example.com")
    scans = admin.get("/admin/ticket-scans").get_data(as_text=True)
    assert ticket["code"] in scans and "Mutale Phiri" in scans
    assert "Repeated attempts on used tickets" in scans
    attendants = admin.get("/admin/attendants").get_data(as_text=True)
    assert "Mutale Phiri" in attendants and "Amina Banda" in attendants

    with app.app_context():
        attendant_id = GateAttendant.query.one().id
    planner.post(f"/planner/attendants/{attendant_id}/status", data={"status": "inactive"})
    assert gate.get("/gate/").status_code == 302
    relogin = login(app.test_client(), "gate1@example.com", NEW_PASSWORD).get_data(as_text=True)
    assert "not active" in relogin


def test_ineligible_tickets_are_invalid(app):
    planner_id, event_id, type_id, client_id = _setup(app)
    cancelled = _issue_ticket(app, client_id, event_id, type_id)
    unpaid = _issue_ticket(app, client_id, event_id, type_id)
    late = _issue_ticket(app, client_id, event_id, type_id)
    other_planner, _ = make_planner(app, email="other@example.com", phone="+260971111120", approved=True)
    other_event, other_type = make_event(app, other_planner, title="Lusaka Jazz Evening")
    foreign = _issue_ticket(app, client_id, other_event, other_type)
    with app.app_context():
        db.session.get(Ticket, cancelled["id"]).status = "cancelled"
        db.session.get(Ticket, unpaid["id"]).order_item.order.order_status = "pending"
        db.session.commit()

    planner = app.test_client()
    login(planner, "planner@example.com")
    _response, temporary = _create_attendant(planner, event_id)
    gate = _attendant_login(app, "gate1@example.com", temporary)

    result = _scan(gate, event_id, cancelled["code"])["outcome"]
    assert result["result"] == "invalid" and result["reason"] == "This ticket has been cancelled."
    assert result["message"] == "Ticket could not be verified. Do not allow entry."
    result = _scan(gate, event_id, unpaid["code"])["outcome"]
    assert result["result"] == "invalid" and result["reason"] == "Payment for this ticket has not been verified."
    response = gate.post(f"/gate/events/{event_id}/verify", data={"code": foreign["token"], "method": "camera"}, headers=JSON)
    body = response.get_data(as_text=True)
    assert response.get_json()["outcome"]["reason"] == "This ticket is for a different event."
    assert response.get_json()["outcome"]["ticket"] is None
    assert "Lusaka Jazz Evening" not in body and "Chanda Mwila" not in body
    with app.app_context():
        assert Ticket.query.filter_by(status="redeemed").count() == 0
        db.session.get(Event, event_id).publication_status = "cancelled"
        db.session.commit()
    result = _scan(gate, event_id, late["code"])["outcome"]
    assert result["result"] == "invalid" and result["reason"] == "This event has been cancelled."
    with app.app_context():
        assert db.session.get(Ticket, late["id"]).status == "valid"


def test_simultaneous_scans_admit_only_once(app):
    _planner_id, event_id, type_id, client_id = _setup(app)
    ticket = _issue_ticket(app, client_id, event_id, type_id)
    planner = app.test_client()
    login(planner, "planner@example.com")
    _response, temporary = _create_attendant(planner, event_id)
    first_gate = _attendant_login(app, "gate1@example.com", temporary)
    second_gate = app.test_client()
    login(second_gate, "gate1@example.com", NEW_PASSWORD)

    barrier = threading.Barrier(2)
    results, errors = [], []

    def scan(gate):
        try:
            barrier.wait(timeout=10)
            results.append(_scan(gate, event_id, ticket["token"])["outcome"]["result"])
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))

    threads = [threading.Thread(target=scan, args=(gate,)) for gate in (first_gate, second_gate)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert sorted(results) == ["already_used", "valid"]
    with app.app_context():
        assert db.session.get(Ticket, ticket["id"]).status == "redeemed"
        assert TicketRedemption.query.filter_by(result="valid").count() == 1


def test_attendant_permissions(app):
    planner_id, event_id, type_id, client_id = _setup(app)
    second_event, _ = make_event(app, planner_id, title="Second Show")
    other_planner, _ = make_planner(app, email="other@example.com", phone="+260971111120", approved=True)
    other_event, other_type = make_event(app, other_planner, title="Other Planner Event")
    make_user(app, role="admin", email="admin@example.com", phone="+260971111199", name="Admin One")

    planner = app.test_client()
    login(planner, "planner@example.com")
    _response, temporary = _create_attendant(planner, event_id, role="admin")
    gate = _attendant_login(app, "gate1@example.com", temporary)
    with app.app_context():
        assert User.query.filter_by(email="gate1@example.com").one().role == "attendant"
        attendant_id = GateAttendant.query.one().id

    for path in ("/admin/dashboard", "/admin/users", "/admin/ticket-scans", "/planner/dashboard", "/planner/attendants",
                 "/planner/scan-activity", "/client/dashboard", "/client/tickets", "/tickets/verify"):
        assert gate.get(path).status_code == 403, path
    assert gate.get(f"/gate/events/{second_event}/scan").status_code == 404
    assert gate.post(f"/gate/events/{second_event}/verify", data={"code": "x"}, headers=JSON).status_code == 404
    assert gate.get(f"/gate/events/{other_event}/scan").status_code == 404

    other = app.test_client()
    login(other, "other@example.com")
    assert other.post(f"/planner/attendants/{attendant_id}/status", data={"status": "inactive"}).status_code == 404
    assert other.post(f"/planner/attendants/{attendant_id}/assign", data={"event_id": other_event}).status_code == 404
    assert other.post(f"/planner/attendants/{attendant_id}/reset-password").status_code == 404
    planner.post(f"/planner/attendants/{attendant_id}/assign", data={"event_id": other_event})
    with app.app_context():
        assert AttendantAssignment.query.filter_by(event_id=other_event).count() == 0

    foreign = _issue_ticket(app, client_id, other_event, other_type)
    with app.app_context():
        from app.services.ticket_service import redeem_ticket

        redeem_ticket(db.session.get(Ticket, foreign["id"]), db.session.get(User, other_planner))
    assert foreign["code"] not in planner.get("/planner/scan-activity").get_data(as_text=True)
    assert foreign["code"] not in gate.get("/gate/history").get_data(as_text=True)

    customer = app.test_client()
    login(customer, "client@example.com")
    admin = app.test_client()
    login(admin, "admin@example.com")
    for session in (customer, planner, admin):
        assert session.get("/gate/").status_code == 403
    assert customer.get("/planner/attendants").status_code == 403
    assert customer.get("/admin/attendants").status_code == 403
    assert planner.get("/admin/ticket-scans").status_code == 403

    with app.app_context():
        assignment = AttendantAssignment.query.filter_by(event_id=event_id).one()
    planner.post(f"/planner/attendants/{attendant_id}/assignments/{assignment.id}/revoke")
    assert gate.get(f"/gate/events/{event_id}/scan").status_code == 404

    duplicate, password = _create_attendant(planner, email="client@example.com", phone="0977000009")
    assert duplicate.status_code == 400 and password is None


def test_gate_verify_requires_csrf_token():
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)

    class Protected(TestingConfig):
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + path.replace("\\", "/")
        WTF_CSRF_ENABLED = True

    application = create_app(Protected)
    try:
        response = application.test_client().post("/gate/events/1/verify", data={"code": "ELZ-00000000-0000"}, headers=JSON)
        assert response.status_code == 400
    finally:
        with application.app_context():
            db.session.remove()
            db.engine.dispose()
        os.unlink(path)


OLD_REDEMPTIONS = """
CREATE TABLE ticket_redemptions (
    id INTEGER NOT NULL PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets (id),
    actor_id INTEGER NOT NULL REFERENCES users (id),
    result VARCHAR(40) NOT NULL,
    created_at DATETIME NOT NULL
)
"""


def test_migration_upgrades_old_tables_without_losing_rows(app):
    planner_id, event_id, type_id, client_id = _setup(app)
    ticket = _issue_ticket(app, client_id, event_id, type_id)
    path = app.config["SQLALCHEMY_DATABASE_URI"][len("sqlite:///"):]
    with app.app_context():
        db.session.remove()
        db.engine.dispose()

    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys = OFF")
    users_sql = raw.execute("SELECT sql FROM sqlite_master WHERE name = 'users'").fetchone()[0]
    assert "'attendant'" in users_sql
    old_users_sql = users_sql.replace(", 'attendant'", "").replace("CREATE TABLE users", "CREATE TABLE users_old", 1)
    assert "users_old" in old_users_sql
    users_before = raw.execute("SELECT * FROM users ORDER BY id").fetchall()
    raw.executescript(
        old_users_sql + ";"
        + "INSERT INTO users_old SELECT * FROM users; DROP TABLE users; ALTER TABLE users_old RENAME TO users;"
        + "DROP TABLE ticket_redemptions;" + OLD_REDEMPTIONS + ";"
    )
    assert "'attendant'" not in raw.execute("SELECT sql FROM sqlite_master WHERE name = 'users'").fetchone()[0]
    legacy = [("accepted",), ("already_redeemed",), ("wrong_event",), ("not_valid",)]
    raw.executemany(
        f"INSERT INTO ticket_redemptions (ticket_id, actor_id, result, created_at) VALUES ({ticket['id']}, {planner_id}, ?, '2026-05-01 10:00:00')",
        legacy,
    )
    raw.commit()
    raw.close()

    backups = tempfile.mkdtemp(prefix="eventlink-backups-")
    with app.app_context():
        done = rebuild_tables(db.engine, db.metadata, backups)
        assert done[0] == "rebuilt users (2 rows kept)"
        assert done[1] == "rebuilt ticket_redemptions (4 rows kept)"
        assert rebuild_tables(db.engine, db.metadata, backups) == []
        rows = TicketRedemption.query.order_by(TicketRedemption.id).all()
        assert [(row.result, row.reason) for row in rows] == [
            ("valid", None), ("already_used", None), ("invalid", "wrong_event"), ("invalid", "not_eligible"),
        ]
        assert {row.event_id for row in rows} == {event_id}
        db.session.add(User(full_name="Gate Person", email="g@example.com", phone="+260977777777", role="attendant", password_hash="x"))
        db.session.commit()
        db.session.remove()
        db.engine.dispose()
    check = sqlite3.connect(path)
    assert check.execute("SELECT * FROM users WHERE email != 'g@example.com' ORDER BY id").fetchall() == users_before
    assert check.execute("PRAGMA foreign_key_check").fetchall() == []
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    check.close()
    assert len(os.listdir(backups)) == 1
