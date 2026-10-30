"""Application configuration loaded from environment variables."""

import os
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    PAYMENT_MODE = os.environ.get("PAYMENT_MODE", "simulation").strip().lower()
    SIMULATION_WEBHOOK_SECRET = os.environ.get("SIMULATION_WEBHOOK_SECRET", "")
    PAYMENT_WEBHOOK_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "")
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024
    UPLOAD_MAX_BYTES = 2 * 1024 * 1024
    DOCUMENT_MAX_BYTES = 5 * 1024 * 1024
    PORTFOLIO_MAX_IMAGES = 6
    PRIVATE_UPLOAD_FOLDER = ""
    TEMPLATES_AUTO_RELOAD = True
    ORDER_HOLD_MINUTES = 30
    PLATFORM_COMMISSION_PERCENT = 15
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_DATABASE_URI = ""
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 30}}
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 60 * 60 * 8
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
    JSON_AS_ASCII = False


class TestingConfig(Config):
    TESTING = True
    SECRET_KEY = "test-secret-key-not-for-production"
    PAYMENT_MODE = "simulation"
    SIMULATION_WEBHOOK_SECRET = "test-webhook-secret-value"
    WTF_CSRF_ENABLED = False
    SESSION_COOKIE_SECURE = False
