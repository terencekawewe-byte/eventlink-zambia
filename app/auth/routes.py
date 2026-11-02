"""Login, registration, logout, and password changes."""

from datetime import timedelta

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError

from app.auth.forms import ChangePasswordForm, ClientRegisterForm, LoginForm, PlannerRegisterForm
from app.extensions import db
from app.models import LoginAttempt, PlannerPortfolio, PlannerProfile, PlannerService, User
from app.models.database import utcnow
from app.utils.decorators import login_required
from app.utils.helpers import (
    audit,
    client_ip,
    dashboard_endpoint,
    delete_document,
    delete_upload,
    safe_next,
    save_document,
    save_image,
)
from app.utils.validators import (
    normalize_phone,
    utcnow_naive,
    validate_business_name,
    validate_email_address,
    validate_pacra_number,
    validate_pacra_reference,
    validate_person_name,
)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def _limited(email, ip_address):
    cutoff = utcnow_naive() - timedelta(minutes=15)
    email_failures = LoginAttempt.query.filter(
        LoginAttempt.email == email,
        LoginAttempt.success.is_(False),
        LoginAttempt.created_at >= cutoff,
    ).count()
    ip_failures = LoginAttempt.query.filter(
        LoginAttempt.ip_address == ip_address,
        LoginAttempt.success.is_(False),
        LoginAttempt.created_at >= cutoff,
    ).count()
    return email_failures >= 5 or ip_failures >= 20


def _record_attempt(email, success):
    db.session.add(LoginAttempt(email=email[:120], ip_address=client_ip(), success=success, created_at=utcnow()))
    db.session.commit()


def _unique_account(email, phone):
    if User.query.filter_by(email=email).first():
        return "An account with this email address already exists."
    if User.query.filter_by(phone=phone).first():
        return "An account with this phone number already exists."
    return None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for(dashboard_endpoint(g.user)))
    form = LoginForm()
    if form.validate_on_submit():
        email, email_error = validate_email_address(form.email.data)
        ip_address = client_ip()
        lookup = email or (form.email.data or "").strip().lower()[:120]
        if _limited(lookup, ip_address):
            flash("Too many login attempts. Please wait 15 minutes and try again.", "danger")
            return render_template("auth/login.html", form=form)
        user = User.query.filter_by(email=email).first() if email and not email_error else None
        if user is None or not user.check_password(form.password.data):
            _record_attempt(lookup, False)
            flash("Invalid email or password.", "danger")
            return render_template("auth/login.html", form=form)
        if user.account_status != "active":
            _record_attempt(lookup, False)
            flash("This account has been suspended. Contact EventLink support.", "danger")
            return render_template("auth/login.html", form=form)
        if user.role == "attendant" and not user.is_active_attendant:
            _record_attempt(lookup, False)
            flash("This gate attendant account is not active. Contact the event planner.", "danger")
            return render_template("auth/login.html", form=form)
        _record_attempt(email, True)
        session.clear()
        session["user_id"] = user.id
        session["role"] = user.role
        session.permanent = True
        flash("Welcome back.", "success")
        return redirect(safe_next(request.args.get("next")) or url_for(dashboard_endpoint(user)))
    return render_template("auth/login.html", form=form)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if g.user is not None:
        return redirect(url_for(dashboard_endpoint(g.user)))
    form = ClientRegisterForm()
    if form.validate_on_submit():
        name, _name_error = validate_person_name(form.full_name.data)
        email, _email_error = validate_email_address(form.email.data)
        phone, _phone_error = normalize_phone(form.phone.data)
        duplicate = _unique_account(email, phone)
        if duplicate:
            flash(duplicate, "danger")
            return render_template("auth/register.html", form=form)
        user = User(full_name=name, email=email, phone=phone, role="client", account_status="active")
        user.set_password(form.password.data)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("An account with this email or phone number already exists.", "danger")
            return render_template("auth/register.html", form=form)
        flash("Your client account has been created. You can log in now.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/register.html", form=form)


@auth_bp.route("/register/planner", methods=["GET", "POST"])
def planner_register():
    if g.user is not None:
        return redirect(url_for(dashboard_endpoint(g.user)))
    form = PlannerRegisterForm()
    if request.method == "POST":
        if not form.validate_on_submit():
            return _planner_form_again(form)
        business, _business_error = validate_business_name(form.business_name.data)
        name, _name_error = validate_person_name(form.full_name.data)
        email, _email_error = validate_email_address(form.email.data)
        phone, _phone_error = normalize_phone(form.phone.data)
        pacra_number, _pacra_error = validate_pacra_number(form.pacra_number.data)
        pacra_reference, _reference_error = validate_pacra_reference(form.pacra_reference.data)
        duplicate = _unique_account(email, phone)
        service_lines = [line.strip() for line in (form.services.data or "").splitlines() if line.strip()]
        photo = request.files.get("profile_photo")
        portfolio_files = [item for item in request.files.getlist("portfolio") if item and item.filename]
        document = request.files.get("pacra_document")
        limit = current_app.config["PORTFOLIO_MAX_IMAGES"]
        saved_paths = []
        saved_document = None
        if duplicate:
            flash(duplicate, "danger")
        elif PlannerProfile.query.filter_by(pacra_number=pacra_number).first():
            flash("A planner application with this PACRA registration number already exists.", "danger")
        elif not service_lines or len(service_lines) > 12:
            flash("Enter between 1 and 12 services, one on each line.", "danger")
        elif any(not 3 <= len(line) <= 120 for line in service_lines):
            flash("Each service must be between 3 and 120 characters.", "danger")
        elif len(portfolio_files) > limit:
            flash(f"You can upload up to {limit} portfolio images.", "danger")
        else:
            try:
                if photo and photo.filename:
                    saved_paths.append(save_image(photo, "planner_profiles"))
                for portfolio in portfolio_files:
                    saved_paths.append(save_image(portfolio, "portfolios"))
                if document and document.filename:
                    saved_document = save_document(document)
                user = User(full_name=name, email=email, phone=phone, role="planner", account_status="active")
                user.set_password(form.password.data)
                db.session.add(user)
                db.session.flush()
                profile = PlannerProfile(
                    user_id=user.id,
                    business_name=business,
                    biography=form.biography.data.strip(),
                    location=form.location.data.strip(),
                    experience=form.experience.data.strip(),
                    specialities=form.specialities.data.strip(),
                    business_category=form.business_category.data,
                    business_address=" ".join(form.business_address.data.split()),
                    pacra_number=pacra_number,
                    pacra_reference=pacra_reference,
                    pacra_document=saved_document,
                    profile_photo=next((path for path in saved_paths if path.startswith("uploads/planner_profiles/")), None),
                    approval_status="pending",
                    terms_accepted=True,
                )
                db.session.add(profile)
                db.session.flush()
                for line in service_lines:
                    db.session.add(
                        PlannerService(
                            planner_id=profile.id,
                            service_name=line,
                            description=line,
                            pricing_type="quotation",
                        )
                    )
                for path in saved_paths:
                    if path.startswith("uploads/portfolios/"):
                        db.session.add(PlannerPortfolio(planner_id=profile.id, file_path=path))
                audit(user.id, "planner_application_submitted", "planner_profile", profile.id)
                db.session.commit()
            except ValueError as exc:
                db.session.rollback()
                _discard_uploads(saved_paths, saved_document)
                flash(str(exc), "danger")
                return _planner_form_again(form)
            except IntegrityError:
                db.session.rollback()
                _discard_uploads(saved_paths, saved_document)
                flash("An account with this email or phone number already exists.", "danger")
                return _planner_form_again(form)
            flash("Your planner application has been received and is pending administrator approval.", "success")
            return redirect(url_for("auth.login"))
        return _planner_form_again(form)
    return render_template("auth/planner_register.html", form=form, files_cleared=False)


def _discard_uploads(paths, document):
    for path in paths:
        delete_upload(path)
    if document:
        delete_document(document)


def _planner_form_again(form):
    """Browsers never re-fill file inputs, so tell the applicant to choose their images again."""
    had_files = any(item and item.filename for name in ("profile_photo", "portfolio", "pacra_document") for item in request.files.getlist(name))
    return render_template("auth/planner_register.html", form=form, files_cleared=had_files)


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("pages.home"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not g.user.check_password(form.current_password.data):
            flash("The current password is not correct.", "danger")
        elif g.user.check_password(form.password.data):
            flash("Choose a new password that is different from the current one.", "danger")
        else:
            g.user.set_password(form.password.data)
            if g.user.gate_profile is not None and g.user.gate_profile.must_change_password:
                g.user.gate_profile.must_change_password = False
                audit(g.user.id, "attendant_password_set", "gate_attendant", g.user.gate_profile.id)
            db.session.commit()
            flash("Your password has been updated.", "success")
            return redirect(url_for(dashboard_endpoint(g.user)))
    return render_template("auth/change_password.html", form=form)
