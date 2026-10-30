"""Shared fixtures for the EventLink Zambia test suite."""

import os
import shutil
import tempfile

import pytest

from app import create_app
from app.extensions import db
from config import TestingConfig


@pytest.fixture
def app():
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    private_folder = tempfile.mkdtemp(prefix="eventlink-private-")

    class Configured(TestingConfig):
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + path.replace("\\", "/")
        PRIVATE_UPLOAD_FOLDER = private_folder

    application = create_app(Configured)
    yield application
    with application.app_context():
        db.session.remove()
        db.engine.dispose()
    os.unlink(path)
    shutil.rmtree(private_folder, ignore_errors=True)


@pytest.fixture(autouse=True)
def _remove_test_uploads():
    """Tests save real files into app/static/uploads; delete only the ones a test adds."""
    root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "static", "uploads")
    folders = [os.path.join(root, name) for name in ("event_posters", "event_artists", "planner_profiles", "portfolios")]
    before = {folder: set(os.listdir(folder)) if os.path.isdir(folder) else set() for folder in folders}
    yield
    for folder in folders:
        if os.path.isdir(folder):
            for name in set(os.listdir(folder)) - before[folder]:
                os.remove(os.path.join(folder, name))


@pytest.fixture
def client(app):
    return app.test_client()
