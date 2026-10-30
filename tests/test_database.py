"""Database constraints and CSRF protection."""

import os
import tempfile

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.extensions import db
from app.models import EventCategory
from config import TestingConfig


def test_categories_and_foreign_keys(app):
    with app.app_context():
        assert EventCategory.query.count() >= 10
        assert db.session.execute(text("PRAGMA foreign_keys")).scalar() == 1
        with pytest.raises(IntegrityError):
            db.session.execute(
                text(
                    """
                    INSERT INTO planner_profiles (
                        user_id, business_name, biography, location, experience,
                        specialities, availability, approval_status, terms_accepted,
                        created_at, updated_at
                    ) VALUES (
                        99999, 'Missing User Events', 'Biography text', 'Lusaka',
                        'Experience text', 'Weddings', '', 'pending', 1,
                        '2026-01-01 00:00:00', '2026-01-01 00:00:00'
                    )
                    """
                )
            )
            db.session.commit()
        db.session.rollback()


def test_state_change_without_csrf_token_is_rejected():
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)

    class Protected(TestingConfig):
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + path.replace("\\", "/")
        WTF_CSRF_ENABLED = True

    application = create_app(Protected)
    try:
        response = application.test_client().post(
            "/auth/login",
            data={"email": "person@example.com", "password": "Secret123"},
        )
        assert response.status_code == 400
    finally:
        with application.app_context():
            db.session.remove()
            db.engine.dispose()
        os.unlink(path)
