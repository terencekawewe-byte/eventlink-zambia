"""Planner registration uploads, PACRA documents, and planner images on public and admin pages."""

import os
import sqlite3
import tempfile
from io import BytesIO

from PIL import Image
from werkzeug.datastructures import FileStorage

from app import create_app
from app.extensions import db
from app.models import AuditLog, PlannerPortfolio, PlannerProfile, User
from app.utils.helpers import save_document, save_image
from config import TestingConfig
from tests.factories import login, make_planner, make_user

PDF_BYTES = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


def _png(color=(20, 110, 80)):
    buffer = BytesIO()
    Image.new("RGB", (64, 40), color).save(buffer, "PNG")
    buffer.seek(0)
    return buffer


def _application(**overrides):
    data = {
        "business_name": "Kafue Celebrations",
        "pacra_number": "1202 1001 2345",
        "pacra_reference": "ref-88412",
        "business_category": "Wedding planning",
        "location": "Lusaka",
        "biography": "We plan weddings and family celebrations around Lusaka.",
        "experience": "Six years of weddings, kitchen parties and graduations.",
        "specialities": "Weddings, kitchen parties",
        "services": "Full wedding planning\nDecor and styling",
        "full_name": "Mutale Phiri",
        "email": "mutale@example.com",
        "phone": "+260971234560",
        "business_address": "Plot 12, Cairo Road, Lusaka",
        "password": "Secret123",
        "confirm_password": "Secret123",
        "accept_terms": "y",
    }
    data.update(overrides)
    return data


def _register(client, portfolio_count=2, document=True, **overrides):
    data = _application(**overrides)
    data["profile_photo"] = (_png(), "logo.png")
    data["portfolio"] = [(_png((index * 30, 90, 60)), f"event-{index}.png") for index in range(portfolio_count)]
    if document:
        data["pacra_document"] = (BytesIO(PDF_BYTES), "certificate.pdf")
    return client.post("/auth/register/planner", data=data, content_type="multipart/form-data", follow_redirects=True)


def _static_file(app, relative):
    return os.path.join(app.static_folder, *relative.split("/"))


def test_planner_registration_saves_images_and_private_pacra_document(app, client):
    response = _register(client, portfolio_count=3)
    assert response.request.path == "/auth/login"
    assert b"pending administrator approval" in response.data
    with app.app_context():
        profile = PlannerProfile.query.one()
        assert profile.approval_status == "pending"
        assert profile.pacra_number == "120210012345"
        assert profile.pacra_reference == "REF-88412"
        assert profile.business_category == "Wedding planning"
        assert profile.business_address == "Plot 12, Cairo Road, Lusaka"
        assert profile.profile_photo.startswith("uploads/planner_profiles/")
        assert os.path.isfile(_static_file(app, profile.profile_photo))
        assert len(profile.portfolio_images) == 3
        for image in profile.portfolio_images:
            assert image.file_path.startswith("uploads/portfolios/")
            assert os.path.isfile(_static_file(app, image.file_path))
        assert profile.pacra_document.startswith("pacra/") and profile.pacra_document.endswith(".pdf")
        stored = os.path.join(app.config["PRIVATE_UPLOAD_FOLDER"], *profile.pacra_document.split("/"))
        assert os.path.isfile(stored)
        assert not os.path.realpath(stored).startswith(os.path.realpath(app.static_folder))
        assert AuditLog.query.filter_by(action="planner_application_submitted", entity_id=profile.id).count() == 1


def test_planner_registration_without_optional_document(app, client):
    response = _register(client, document=False, pacra_reference="")
    assert response.request.path == "/auth/login"
    with app.app_context():
        profile = PlannerProfile.query.one()
        assert profile.pacra_document is None
        assert profile.pacra_reference is None
        assert profile.approval_status == "pending"


def test_planner_registration_rejects_too_many_portfolio_images(app, client):
    response = _register(client, portfolio_count=7)
    assert b"You can upload up to 6 portfolio images." in response.data
    assert b"choose your logo, portfolio images and PACRA document again" in response.data
    with app.app_context():
        assert PlannerProfile.query.count() == 0
        assert User.query.filter_by(email="mutale@example.com").count() == 0
        assert PlannerPortfolio.query.count() == 0
        assert os.listdir(os.path.join(app.config["PRIVATE_UPLOAD_FOLDER"], "pacra")) == []


def test_planner_registration_rejects_duplicate_pacra_number(app, client):
    _register(client)
    client.get("/")
    response = _register(client, email="second@example.com", phone="+260971234561", business_name="Second Events", pacra_number="120210012345")
    assert b"A planner application with this PACRA registration number already exists." in response.data
    with app.app_context():
        assert PlannerProfile.query.count() == 1


def test_planner_registration_rejects_fake_image_and_keeps_nothing(app, client):
    profiles = os.path.join(app.static_folder, "uploads", "planner_profiles")
    before = set(os.listdir(profiles))
    data = _application()
    data["profile_photo"] = (BytesIO(b"<?php echo 'not an image'; ?>"), "logo.png")
    data["portfolio"] = [(_png(), "event.png")]
    response = client.post("/auth/register/planner", data=data, content_type="multipart/form-data")
    assert b"Upload a valid JPEG, PNG, or WebP image." in response.data
    assert set(os.listdir(profiles)) == before
    with app.app_context():
        assert PlannerProfile.query.count() == 0


def test_planner_registration_rejects_invalid_pacra_number(client):
    response = client.post("/auth/register/planner", data=_application(pacra_number="12"))
    assert response.status_code == 200
    with client.application.app_context():
        assert PlannerProfile.query.count() == 0


def _approved_planner_with_images(app):
    _user_id, profile_id = make_planner(app, approved=True)
    with app.test_request_context():
        profile = db.session.get(PlannerProfile, profile_id)
        profile.profile_photo = save_image(FileStorage(_png(), "logo.png"), "planner_profiles")
        for index in range(2):
            db.session.add(PlannerPortfolio(planner_id=profile.id, file_path=save_image(FileStorage(_png((index * 60, 40, 90)), "p.png"), "portfolios")))
        profile.pacra_number = "120210012345"
        profile.pacra_document = save_document(FileStorage(BytesIO(PDF_BYTES), "certificate.pdf"))
        db.session.commit()
        return profile_id, profile.profile_photo, [image.file_path for image in profile.portfolio_images]


def test_uploaded_planner_images_appear_on_homepage_list_and_profile(app, client):
    profile_id, photo, portfolio = _approved_planner_with_images(app)
    home = client.get("/").get_data(as_text=True)
    assert f'src="/static/{photo}"' in home
    assert "planner-card-media" in home
    assert "View profile" in home
    listing = client.get("/planners").get_data(as_text=True)
    assert f'src="/static/{photo}"' in listing
    public = client.get(f"/planners/{profile_id}").get_data(as_text=True)
    assert f'src="/static/{photo}"' in public
    for path in portfolio:
        assert f'href="/static/{path}" data-lightbox="planner-portfolio"' in public or f'href="/static/{path}"' in public
    assert 'id="imageLightbox"' in public
    assert "data-fallback" in public
    assert "120210012345" not in public
    assert "pacra-document" not in public


def test_planner_without_images_shows_initials_and_empty_portfolio(app, client):
    _user_id, profile_id = make_planner(app, approved=True)
    public = client.get(f"/planners/{profile_id}").get_data(as_text=True)
    assert 'class="planner-initials"' in public and ">AE</span>" in public
    assert "<img" not in public.split('class="portfolio-gallery"')[1].split("</section>")[0]
    assert 'data-gallery-empty>' in public


def test_pending_planners_are_not_listed_publicly(app, client):
    make_planner(app, approved=False)
    assert b"Amina Events" not in client.get("/").data
    assert b"Amina Events" not in client.get("/planners").data


def test_admin_sees_pacra_details_portfolio_and_can_open_document(app, client):
    profile_id, photo, portfolio = _approved_planner_with_images(app)
    make_user(app, role="admin", email="admin@example.com", phone="+260971111119", name="Admin User")
    login(client, "admin@example.com")
    page = client.get(f"/admin/planners/{profile_id}").get_data(as_text=True)
    assert "Registration and PACRA information" in page
    assert "120210012345" in page
    assert f"/admin/planners/{profile_id}/pacra-document" in page
    assert f"/admin/planners/{profile_id}/pacra-document?download=1" in page
    assert f'src="/static/{photo}"' in page
    for path in portfolio:
        assert f'href="/static/{path}" data-lightbox="admin-portfolio"' in page
    assert 'id="imageLightbox"' in page

    viewed = client.get(f"/admin/planners/{profile_id}/pacra-document")
    assert viewed.status_code == 200
    assert viewed.mimetype == "application/pdf"
    assert viewed.data == PDF_BYTES
    assert "inline" in viewed.headers.get("Content-Disposition", "inline")
    assert "no-store" in viewed.headers["Cache-Control"]
    downloaded = client.get(f"/admin/planners/{profile_id}/pacra-document?download=1")
    assert downloaded.headers["Content-Disposition"].startswith("attachment;")


def test_pacra_document_is_admin_only(app, client):
    profile_id, _photo, _portfolio = _approved_planner_with_images(app)
    url = f"/admin/planners/{profile_id}/pacra-document"
    anonymous = client.get(url)
    assert anonymous.status_code == 302 and "/auth/login" in anonymous.headers["Location"]
    login(client, "planner@example.com")
    assert client.get(url).status_code == 403
    client.post("/auth/logout")
    make_user(app, email="client@example.com")
    login(client, "client@example.com")
    assert client.get(url).status_code == 403


def test_missing_pacra_document_returns_not_found(app, client):
    _user_id, profile_id = make_planner(app)
    make_user(app, role="admin", email="admin@example.com", phone="+260971111119", name="Admin User")
    login(client, "admin@example.com")
    assert client.get(f"/admin/planners/{profile_id}/pacra-document").status_code == 404
    page = client.get(f"/admin/planners/{profile_id}").get_data(as_text=True)
    assert "No PACRA certificate was uploaded" in page
    assert "This application has no PACRA details" in page
    assert "No portfolio images" in page


def test_schema_upgrade_adds_columns_without_losing_planners():
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    private_folder = tempfile.mkdtemp(prefix="eventlink-private-")
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE planner_profiles (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, business_name VARCHAR(120) NOT NULL, "
        "profile_photo VARCHAR(255), approval_status VARCHAR(20) NOT NULL)"
    )
    connection.execute("INSERT INTO planner_profiles VALUES (1, 7, 'Existing Planner', 'uploads/planner_profiles/old.png', 'approved')")
    connection.commit()
    connection.close()

    class OldDatabase(TestingConfig):
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + path.replace("\\", "/")
        PRIVATE_UPLOAD_FOLDER = private_folder

    application = create_app(OldDatabase)
    with application.app_context():
        db.engine.dispose()
    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(planner_profiles)")}
    row = connection.execute("SELECT business_name, profile_photo, approval_status, pacra_number FROM planner_profiles WHERE id = 1").fetchone()
    connection.close()
    os.unlink(path)
    os.rmdir(os.path.join(private_folder, "pacra"))
    os.rmdir(private_folder)
    assert {"business_category", "business_address", "pacra_number", "pacra_reference", "pacra_document"} <= columns
    assert row == ("Existing Planner", "uploads/planner_profiles/old.png", "approved", None)
