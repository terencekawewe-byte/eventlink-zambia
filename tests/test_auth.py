"""Client registration, login, and account protection."""

from app.extensions import db
from app.models import AuditLog, User
from tests.factories import login, make_user


def test_client_can_register_and_login(client, app):
    response = client.post(
        "/auth/register",
        data={
            "full_name": "Chipo Mulenga",
            "email": "chipo@example.com",
            "phone": "0971234567",
            "password": "Secret123",
            "confirm_password": "Secret123",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"created" in response.data
    with app.app_context():
        user = User.query.filter_by(email="chipo@example.com").one()
        assert user.role == "client"
        assert user.phone == "+260971234567"
        assert user.password_hash != "Secret123"
        assert user.check_password("Secret123")
    logged_in = login(client, "chipo@example.com")
    assert b"Welcome" in logged_in.data
    assert b"Chipo Mulenga" in logged_in.data


def test_duplicate_email_and_phone_are_rejected(client, app):
    make_user(app, email="taken@example.com", phone="+260971111111")
    email_response = client.post(
        "/auth/register",
        data={
            "full_name": "New Person",
            "email": "taken@example.com",
            "phone": "0971222222",
            "password": "Secret123",
            "confirm_password": "Secret123",
        },
    )
    assert b"email" in email_response.data.lower()
    phone_response = client.post(
        "/auth/register",
        data={
            "full_name": "New Person",
            "email": "other@example.com",
            "phone": "+260971111111",
            "password": "Secret123",
            "confirm_password": "Secret123",
        },
    )
    assert b"phone" in phone_response.data.lower()


def test_invalid_registration_is_rejected(client):
    response = client.post(
        "/auth/register",
        data={
            "full_name": "1234",
            "email": "not-an-email",
            "phone": "12345",
            "password": "secret",
            "confirm_password": "different",
            "role": "admin",
        },
    )
    assert response.status_code == 200
    assert b"email" in response.data.lower()
    assert b"Passwords do not match" in response.data or b"Password must" in response.data


def test_public_registration_cannot_create_admin(client, app):
    client.post(
        "/auth/register",
        data={
            "full_name": "Hope Phiri",
            "email": "hope@example.com",
            "phone": "+260971333333",
            "password": "Secret123",
            "confirm_password": "Secret123",
            "role": "admin",
        },
    )
    with app.app_context():
        user = User.query.filter_by(email="hope@example.com").one()
        assert user.role == "client"


def test_login_rejects_unknown_user(client):
    response = login(client, "missing@example.com")
    assert b"Invalid email or password" in response.data


def test_every_password_field_has_a_show_toggle(client):
    for path in ("/auth/login", "/auth/register", "/auth/register/planner"):
        html = client.get(path).get_data(as_text=True)
        assert html.count('type="password"') == html.count("data-password-toggle") > 0, path


def test_reset_password_command_sets_new_password(client, app):
    make_user(app, role="admin", email="admin@example.com", phone="+260971111119", name="Site Admin")
    result = app.test_cli_runner().invoke(
        args=["reset-password", "--email", "admin@example.com"],
        input="NewSecret456\nNewSecret456\n",
    )
    assert "Password updated for admin@example.com (admin)." in result.output
    assert "NewSecret456" not in result.output
    with app.app_context():
        user = User.query.filter_by(email="admin@example.com").one()
        assert user.check_password("NewSecret456")
        assert not user.check_password("Secret123")
        assert AuditLog.query.filter_by(action="password_reset_cli", entity_id=user.id).count() == 1
    assert login(client, "admin@example.com", "NewSecret456").request.path == "/admin/dashboard"


def test_reset_password_command_rejects_unknown_email_and_weak_password(app):
    make_user(app)
    runner = app.test_cli_runner()
    missing = runner.invoke(args=["reset-password", "--email", "nobody@example.com", "--password", "NewSecret456"])
    assert "No account uses that email." in missing.output
    weak = runner.invoke(args=["reset-password", "--email", "client@example.com", "--password", "short"])
    assert weak.exit_code != 0
    with app.app_context():
        assert User.query.filter_by(email="client@example.com").one().check_password("Secret123")


def test_client_cannot_open_admin(client, app):
    make_user(app)
    login(client, "client@example.com")
    response = client.get("/admin/dashboard")
    assert response.status_code == 403
