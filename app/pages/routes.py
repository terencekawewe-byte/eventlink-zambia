"""Public pages that are not tied to a single actor."""

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import ContactMessage, Event, EventCategory, PlannerBooking, PlannerProfile, User
from app.pages.forms import ContactForm
from app.services.notification_service import notify_admins
from app.utils.validators import utcnow_naive, validate_email_address, validate_person_name

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/")
def home():
    now = utcnow_naive()
    featured = (
        Event.query.filter(
            Event.approval_status == "approved",
            Event.publication_status == "published",
            Event.end_datetime >= now,
        )
        .order_by(Event.start_datetime.asc())
        .limit(6)
        .all()
    )
    categories = EventCategory.query.filter_by(is_active=True).order_by(EventCategory.name).all()
    planners = (
        db.session.query(PlannerProfile, func.count(PlannerBooking.id).label("completed"))
        .join(User, PlannerProfile.user_id == User.id)
        .outerjoin(
            PlannerBooking,
            (PlannerBooking.planner_id == PlannerProfile.id) & (PlannerBooking.status == "completed"),
        )
        .filter(PlannerProfile.approval_status == "approved", User.account_status == "active")
        .group_by(PlannerProfile.id)
        .order_by(func.count(PlannerBooking.id).desc(), PlannerProfile.business_name)
        .limit(4)
        .all()
    )
    return render_template("index.html", featured=featured, categories=categories, planners=planners)


@pages_bp.route("/about")
def about():
    published = Event.query.filter(
        Event.approval_status == "approved",
        Event.publication_status == "published",
    )
    stats = {
        "events": published.count(),
        "planners": PlannerProfile.query.join(User, PlannerProfile.user_id == User.id)
        .filter(PlannerProfile.approval_status == "approved", User.account_status == "active")
        .count(),
        "categories": EventCategory.query.filter_by(is_active=True).count(),
        "towns": published.with_entities(func.count(func.distinct(Event.location))).scalar() or 0,
    }
    return render_template("about.html", stats=stats)


@pages_bp.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")


@pages_bp.route("/contact", methods=["GET", "POST"])
def contact():
    form = ContactForm()
    user = getattr(g, "user", None)
    if request.method == "GET" and user is not None:
        form.name.data = user.full_name
        form.email.data = user.email
    if form.validate_on_submit():
        name, name_error = validate_person_name(form.name.data)
        email, email_error = validate_email_address(form.email.data)
        if name_error or email_error:
            flash(name_error or email_error, "danger")
        else:
            message = ContactMessage(
                name=name,
                email=email,
                subject=form.subject.data.strip(),
                message=form.message.data.strip(),
                status="new",
            )
            db.session.add(message)
            db.session.flush()
            notify_admins(
                "New contact message",
                f"{name} ({email}) wrote: {message.subject}",
                f"/admin/messages#message-{message.id}",
            )
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("Your message could not be saved. Please try again.", "danger")
                return render_template("contact.html", form=form)
            flash("Thank you. Your message has been sent to the EventLink team.", "success")
            return redirect(url_for("pages.contact"))
    return render_template("contact.html", form=form)


@pages_bp.route("/terms")
def terms():
    return render_template("terms.html")


@pages_bp.route("/privacy")
def privacy():
    return render_template("privacy.html")


@pages_bp.route("/support")
def support():
    return render_template("support.html")
