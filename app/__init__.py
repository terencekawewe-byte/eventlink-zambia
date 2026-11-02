"""EventLink Zambia application factory."""

import os
from datetime import datetime

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from flask_wtf.csrf import CSRFError
from markupsafe import Markup, escape
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import RequestEntityTooLarge

from app.cli import register_commands
from app.extensions import csrf, db
from config import Config


def _validate_runtime_config(app):
    if app.config.get("TESTING"):
        return
    secret = app.config.get("SECRET_KEY") or ""
    if len(secret) < 16 or secret.lower() in {"secret", "changeme", "password", "dev", "eventlink"}:
        raise RuntimeError("Set a unique SECRET_KEY of at least 16 characters in the environment.")
    mode = app.config.get("PAYMENT_MODE")
    if mode not in {"simulation", "live"}:
        raise RuntimeError("PAYMENT_MODE must be either simulation or live.")
    if mode == "simulation" and len(app.config.get("SIMULATION_WEBHOOK_SECRET") or "") < 16:
        raise RuntimeError("Set SIMULATION_WEBHOOK_SECRET to at least 16 characters.")
    if mode == "live" and len(app.config.get("PAYMENT_WEBHOOK_SECRET") or "") < 16:
        raise RuntimeError("Set PAYMENT_WEBHOOK_SECRET before using live payments.")


def create_app(config_class=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class)
    os.makedirs(app.instance_path, exist_ok=True)
    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        if app.config.get("TESTING"):
            raise RuntimeError("The test configuration must set SQLALCHEMY_DATABASE_URI.")
        database_path = os.path.join(app.instance_path, "eventlink.db").replace("\\", "/")
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + database_path
    _validate_runtime_config(app)

    for folder in ("event_posters", "event_artists", "planner_profiles", "portfolios"):
        os.makedirs(os.path.join(app.static_folder, "uploads", folder), exist_ok=True)
    if not app.config.get("PRIVATE_UPLOAD_FOLDER"):
        app.config["PRIVATE_UPLOAD_FOLDER"] = os.path.join(app.instance_path, "private_uploads")
    os.makedirs(os.path.join(app.config["PRIVATE_UPLOAD_FOLDER"], "pacra"), exist_ok=True)

    db.init_app(app)
    csrf.init_app(app)
    register_commands(app)

    from app import models  # noqa: F401
    from app.admin.routes import admin_bp
    from app.auth.routes import auth_bp
    from app.client.routes import client_bp
    from app.events.routes import events_bp
    from app.gate.routes import gate_bp
    from app.pages.routes import pages_bp
    from app.payments.routes import payments_bp
    from app.planner.routes import planner_bp
    from app.tickets.routes import tickets_bp

    app.register_blueprint(pages_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(events_bp)
    app.register_blueprint(client_bp)
    app.register_blueprint(planner_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(tickets_bp)
    app.register_blueprint(gate_bp)
    app.register_blueprint(payments_bp)

    with app.app_context():
        from app.models.schema import upgrade_schema

        db.create_all()
        upgrade_schema()
        models.seed_reference_data()

    _register_hooks(app)
    return app


def _register_hooks(app):
    @app.before_request
    def load_user():
        g.user = None
        user_id = session.get("user_id")
        if not user_id:
            return None
        from app.models import User

        user = db.session.get(User, user_id)
        if user is None or user.account_status != "active":
            session.clear()
            return None
        if user.role == "attendant" and not user.is_active_attendant:
            session.clear()
            return None
        g.user = user
        return None

    @app.context_processor
    def inject_template_globals():
        from app.services.notification_service import unread_count
        from app.utils.helpers import format_duration, format_local, format_zmw, safe_link, safe_upload_path

        user = getattr(g, "user", None)
        unread = 0
        new_messages = 0
        if user is not None:
            try:
                unread = unread_count(user.id)
                if user.role == "admin":
                    from app.models import ContactMessage

                    new_messages = ContactMessage.query.filter_by(status="new").count()
            except SQLAlchemyError:
                unread = 0
        return {
            "current_user": user,
            "unread_notifications": unread,
            "admin_new_messages": new_messages,
            "format_zmw": format_zmw,
            "format_local": format_local,
            "format_duration": format_duration,
            "safe_link": safe_link,
            "safe_upload_path": safe_upload_path,
            "payment_mode": app.config.get("PAYMENT_MODE"),
            "current_year": datetime.now().year,
        }

    @app.template_filter("zmw")
    def zmw_filter(value):
        from app.utils.helpers import format_zmw

        return format_zmw(value)

    @app.template_filter("localdt")
    def localdt_filter(value, pattern="%d %b %Y, %H:%M"):
        from app.utils.helpers import format_local

        return format_local(value, pattern)

    @app.template_filter("localdate")
    def localdate_filter(value):
        from app.utils.helpers import format_local

        return format_local(value, "%d %b %Y")

    @app.template_filter("method_label")
    def method_label_filter(value):
        from app.utils.helpers import payment_method_label

        return payment_method_label(value)

    @app.template_filter("provider_label")
    def provider_label_filter(value):
        from app.utils.helpers import payment_method_label

        return payment_method_label(value, show_mode=False)

    @app.template_filter("nl2br")
    def nl2br_filter(value):
        if not value:
            return ""
        return Markup("<br>".join(escape(value).splitlines()))

    @app.template_filter("upload")
    def upload_filter(value):
        from app.utils.helpers import safe_upload_path

        return safe_upload_path(value)

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if getattr(g, "user", None) is not None and request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def missing(_error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(_error):
        db.session.rollback()
        return render_template("errors/500.html"), 500

    @app.errorhandler(CSRFError)
    def csrf_error(_error):
        return render_template("errors/403.html", detail="The form could not be verified. Go back and try again."), 400

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        flash("The upload is too large. Each image must be 2 MB or smaller and all files together under 20 MB.", "danger")
        return redirect(request.referrer or url_for("pages.home"))
