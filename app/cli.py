"""Flask CLI commands for database setup and the first administrator."""

import click
from flask import current_app

from app.extensions import db
from app.models import User, seed_reference_data
from app.utils.helpers import audit
from app.utils.validators import normalize_phone, validate_email_address, validate_password, validate_person_name


def register_commands(app):
    @app.cli.command("init-db")
    def init_db():
        """Create tables and seed event categories."""
        db.create_all()
        seed_reference_data()
        click.echo("Database initialised.")

    @app.cli.command("create-admin")
    @click.option("--full-name", prompt=True)
    @click.option("--email", prompt=True)
    @click.option("--phone", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_admin(full_name, email, phone, password):
        """Create an administrator. There is no public admin registration page."""
        name, name_error = validate_person_name(full_name)
        email_value, email_error = validate_email_address(email)
        phone_value, phone_error = normalize_phone(phone)
        password_error = validate_password(password)
        errors = [item for item in (name_error, email_error, phone_error, password_error) if item]
        if errors:
            for error in errors:
                click.echo(error)
            raise SystemExit(1)
        if User.query.filter_by(email=email_value).first() or User.query.filter_by(phone=phone_value).first():
            click.echo("An account with that email or phone already exists.")
            raise SystemExit(1)
        admin = User(
            full_name=name,
            email=email_value,
            phone=phone_value,
            role="admin",
            account_status="active",
        )
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        click.echo(f"Administrator {email_value} created for {current_app.name}.")

    @app.cli.command("reset-password")
    @click.option("--email", prompt=True)
    @click.option("--password", prompt="New password", hide_input=True, confirmation_prompt=True)
    def reset_password(email, password):
        """Set a new password for an existing account, for example a forgotten admin password."""
        email_value, email_error = validate_email_address(email)
        if email_error:
            click.echo(email_error)
            raise SystemExit(1)
        user = User.query.filter_by(email=email_value).first()
        if not user:
            click.echo("No account uses that email.")
            raise SystemExit(1)
        password_error = validate_password(password)
        if password_error:
            click.echo(password_error)
            raise SystemExit(1)
        user.set_password(password)
        audit(None, "password_reset_cli", "user", user.id, f"Password reset from the command line for {user.role} account.")
        db.session.commit()
        click.echo(f"Password updated for {user.email} ({user.role}).")
