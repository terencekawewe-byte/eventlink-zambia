"""Event planner profiles, bookings, services, and event submissions."""

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db
from app.models import Event, EventCategory, PlannerBooking, PlannerPortfolio, PlannerService, Quotation, TicketType
from app.planner.forms import PlannerProfileForm, PlannerServiceForm, QuotationForm
from app.services.booking_service import has_conflict
from app.services.dashboard_service import planner_overview
from app.models.database import ARTIST_CATEGORIES
from app.services.event_service import (
    ARTIST_KEY,
    parse_artist_rows,
    sync_artists,
    sync_ticket_types,
    validate_event_form,
)
from app.services.notification_service import list_for_user, mark_all_read, mark_read, notify, notify_admins
from app.services.report_export import EXPORT_FORMATS, export_response, planner_document
from app.services.report_service import (
    PERIODS,
    TRANSACTION_TYPES,
    planner_report,
    resolve_period,
    resolve_transaction_type,
)
from app.services.ticket_service import event_is_locked
from app.utils.decorators import approved_planner_required, planner_account_required
from app.utils.helpers import audit, delete_upload, save_image
from app.utils.validators import parse_money_to_ngwee, to_local_input, validate_business_name

planner_bp = Blueprint("planner", __name__, url_prefix="/planner")


def _profile():
    return g.user.planner_profile


def _own_booking(booking_id):
    profile = _profile()
    return PlannerBooking.query.filter_by(id=booking_id, planner_id=profile.id).first_or_404()


def _own_event(event_id):
    return Event.query.filter_by(id=event_id, organiser_id=g.user.id).first_or_404()


def _raw_ticket_rows(form):
    names = form.getlist("ticket_name")
    if not names:
        return [{"id": "", "name": "", "price": "", "quantity": "", "max_per_order": "10", "description": ""}]
    rows = []
    for index, name in enumerate(names):
        rows.append(
            {
                "id": (form.getlist("ticket_id")[index] if index < len(form.getlist("ticket_id")) else ""),
                "name": name,
                "price": form.getlist("ticket_price")[index] if index < len(form.getlist("ticket_price")) else "",
                "quantity": form.getlist("ticket_qty")[index] if index < len(form.getlist("ticket_qty")) else "",
                "max_per_order": form.getlist("ticket_max")[index] if index < len(form.getlist("ticket_max")) else "",
                "description": form.getlist("ticket_desc")[index] if index < len(form.getlist("ticket_desc")) else "",
            }
        )
    return rows


def _artist_rows(event):
    return [
        {
            "key": f"a{artist.id}",
            "id": artist.id,
            "name": artist.artist_name,
            "category": artist.artist_category,
            "description": artist.performance_description,
            "image": artist.artist_image,
        }
        for artist in (event.artists if event else [])
    ]


def _raw_artist_rows(form, event):
    """Rebuild the artist cards from a rejected submission, keeping saved photos of existing artists."""
    existing = {str(artist.id): artist for artist in (event.artists if event else [])}
    keys = form.getlist("artist_key")
    fields = {name: form.getlist(name) for name in ("artist_id", "artist_name", "artist_category", "artist_description")}
    rows = []
    for index, key in enumerate(keys):
        value = {name: (items[index] if index < len(items) else "") for name, items in fields.items()}
        artist = existing.get(value["artist_id"].strip())
        rows.append(
            {
                "key": key if ARTIST_KEY.fullmatch(key) else f"r{index}",
                "id": artist.id if artist else "",
                "name": value["artist_name"],
                "category": value["artist_category"],
                "description": value["artist_description"],
                "image": artist.artist_image if artist else None,
            }
        )
    return rows


def _new_artist_photos_chosen():
    return any(
        name.startswith("artist_photo_") and item and item.filename
        for name, items in request.files.lists()
        for item in items
    )


def _event_form(event, errors, ticket_rows, artist_rows, locked, photos_cleared=False):
    return render_template(
        "planner/create_event.html",
        event=event,
        categories=EventCategory.query.filter_by(is_active=True).order_by(EventCategory.name).all(),
        artist_categories=ARTIST_CATEGORIES,
        errors=errors,
        ticket_rows=ticket_rows,
        artist_rows=artist_rows,
        locked=locked,
        photos_cleared=photos_cleared,
        local_input=to_local_input,
    )


@planner_bp.route("/pending")
@planner_account_required
def pending():
    if g.user.is_approved_planner:
        return redirect(url_for("planner.dashboard"))
    return render_template("planner/pending.html", profile=_profile())


@planner_bp.route("/dashboard")
@approved_planner_required
def dashboard():
    profile = _profile()
    return render_template(
        "planner/dashboard.html",
        profile=profile,
        notifications=list_for_user(g.user)[:5],
        **planner_overview(g.user, profile),
    )


@planner_bp.route("/profile")
@approved_planner_required
def profile():
    return render_template("planner/profile.html", profile=_profile())


@planner_bp.route("/profile/edit", methods=["GET", "POST"])
@approved_planner_required
def edit_profile():
    profile = _profile()
    form = PlannerProfileForm(obj=profile)
    if form.validate_on_submit():
        business, error = validate_business_name(form.business_name.data)
        if error:
            flash(error, "danger")
        else:
            saved = None
            portfolios = []
            try:
                photo = request.files.get("profile_photo")
                if photo and photo.filename:
                    saved = save_image(photo, "planner_profiles")
                for upload in request.files.getlist("portfolio"):
                    if upload and upload.filename:
                        portfolios.append(save_image(upload, "portfolios"))
                if len(profile.portfolio_images) + len(portfolios) > 6:
                    for path in portfolios:
                        delete_upload(path)
                    if saved:
                        delete_upload(saved)
                    flash("You can keep up to 6 portfolio images.", "danger")
                    return render_template("planner/edit_profile.html", form=form, profile=profile)
                profile.business_name = business
                profile.biography = form.biography.data.strip()
                profile.location = form.location.data.strip()
                profile.experience = form.experience.data.strip()
                profile.specialities = form.specialities.data.strip()
                profile.availability = (form.availability.data or "").strip()
                profile.business_category = form.business_category.data or None
                profile.business_address = " ".join((form.business_address.data or "").split()) or None
                previous_photo = profile.profile_photo
                if saved:
                    profile.profile_photo = saved
                for path in portfolios:
                    db.session.add(PlannerPortfolio(planner_id=profile.id, file_path=path))
                db.session.commit()
                if saved and previous_photo:
                    delete_upload(previous_photo)
            except ValueError as exc:
                db.session.rollback()
                for path in [saved, *portfolios]:
                    if path:
                        delete_upload(path)
                flash(str(exc), "danger")
                return render_template("planner/edit_profile.html", form=form, profile=profile)
            flash("Your planner profile has been updated.", "success")
            return redirect(url_for("planner.profile"))
    return render_template("planner/edit_profile.html", form=form, profile=profile)


@planner_bp.route("/portfolio/<int:image_id>/delete", methods=["POST"])
@approved_planner_required
def delete_portfolio(image_id):
    image = PlannerPortfolio.query.filter_by(id=image_id, planner_id=_profile().id).first_or_404()
    delete_upload(image.file_path)
    db.session.delete(image)
    db.session.commit()
    flash("Portfolio image removed.", "success")
    return redirect(url_for("planner.edit_profile"))


@planner_bp.route("/services", methods=["GET", "POST"])
@approved_planner_required
def services():
    profile = _profile()
    form = PlannerServiceForm()
    if form.validate_on_submit():
        price = None
        try:
            if form.pricing_type.data in {"fixed", "starting_from"}:
                price = parse_money_to_ngwee(form.starting_price.data, "Starting price")
            elif (form.starting_price.data or "").strip():
                raise ValueError("Leave the price empty when pricing is by quotation.")
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("planner/services.html", form=form, profile=profile)
        db.session.add(
            PlannerService(
                planner_id=profile.id,
                service_name=form.service_name.data.strip(),
                description=form.description.data.strip(),
                pricing_type=form.pricing_type.data,
                starting_price=price,
            )
        )
        db.session.commit()
        flash("Service added.", "success")
        return redirect(url_for("planner.services"))
    return render_template("planner/services.html", form=form, profile=profile)


@planner_bp.route("/services/<int:service_id>/delete", methods=["POST"])
@approved_planner_required
def delete_service(service_id):
    service = PlannerService.query.filter_by(id=service_id, planner_id=_profile().id).first_or_404()
    db.session.delete(service)
    db.session.commit()
    flash("Service removed.", "success")
    return redirect(url_for("planner.services"))


@planner_bp.route("/bookings")
@approved_planner_required
def bookings():
    status = request.args.get("status") or ""
    query = PlannerBooking.query.filter_by(planner_id=_profile().id)
    if status in {"pending", "accepted", "rejected", "in_progress", "completed", "cancelled"}:
        query = query.filter_by(status=status)
    items = query.order_by(PlannerBooking.event_date.desc(), PlannerBooking.created_at.desc()).all()
    return render_template("planner/bookings.html", bookings=items, status=status)


@planner_bp.route("/bookings/<int:booking_id>")
@approved_planner_required
def booking_detail(booking_id):
    booking = _own_booking(booking_id)
    return render_template("planner/booking_details.html", booking=booking, quote_form=QuotationForm())


def _set_booking_status(booking, status, reason=None):
    booking.status = status
    if status == "rejected":
        booking.rejection_reason = reason
    if status == "cancelled":
        booking.cancellation_reason = reason
        booking.cancelled_by = g.user.id
    notify(
        booking.client_id,
        "Booking update",
        f"Your {booking.event_type.lower()} booking with {booking.planner.business_name} is now {status.replace('_', ' ')}.",
        f"/client/bookings/{booking.id}",
    )
    db.session.commit()


@planner_bp.route("/bookings/<int:booking_id>/accept", methods=["POST"])
@approved_planner_required
def booking_accept(booking_id):
    booking = _own_booking(booking_id)
    if booking.status != "pending":
        flash("Only pending requests can be accepted.", "warning")
    elif has_conflict(booking.planner_id, booking.event_date, booking.start_time, booking.duration, booking.id):
        flash("You already have an accepted booking that overlaps this date and time.", "danger")
    else:
        _set_booking_status(booking, "accepted")
        flash("Booking accepted.", "success")
        return redirect(url_for("planner.booking_detail", booking_id=booking.id))
    return redirect(url_for("planner.booking_detail", booking_id=booking.id))


@planner_bp.route("/bookings/<int:booking_id>/reject", methods=["POST"])
@approved_planner_required
def booking_reject(booking_id):
    booking = _own_booking(booking_id)
    reason = (request.form.get("reason") or "").strip()
    if booking.status != "pending":
        flash("Only pending requests can be rejected.", "warning")
    elif not 5 <= len(reason) <= 500:
        flash("Provide a rejection reason between 5 and 500 characters.", "danger")
    else:
        _set_booking_status(booking, "rejected", reason)
        flash("Booking rejected.", "success")
    return redirect(url_for("planner.booking_detail", booking_id=booking.id))


@planner_bp.route("/bookings/<int:booking_id>/progress", methods=["POST"])
@approved_planner_required
def booking_progress(booking_id):
    booking = _own_booking(booking_id)
    if booking.status != "accepted":
        flash("Only accepted bookings can be marked in progress.", "warning")
    else:
        _set_booking_status(booking, "in_progress")
        flash("Booking marked as in progress.", "success")
    return redirect(url_for("planner.booking_detail", booking_id=booking.id))


@planner_bp.route("/bookings/<int:booking_id>/complete", methods=["POST"])
@approved_planner_required
def booking_complete(booking_id):
    booking = _own_booking(booking_id)
    if booking.status != "in_progress":
        flash("Only bookings that are in progress can be completed.", "warning")
    else:
        _set_booking_status(booking, "completed")
        flash("Booking marked as completed.", "success")
    return redirect(url_for("planner.booking_detail", booking_id=booking.id))


@planner_bp.route("/bookings/<int:booking_id>/cancel", methods=["POST"])
@approved_planner_required
def booking_cancel(booking_id):
    booking = _own_booking(booking_id)
    reason = (request.form.get("reason") or "").strip()
    if booking.status not in {"pending", "accepted"}:
        flash("This booking cannot be cancelled at its current stage.", "warning")
    elif not 5 <= len(reason) <= 500:
        flash("Provide a cancellation reason between 5 and 500 characters.", "danger")
    else:
        _set_booking_status(booking, "cancelled", reason)
        flash("Booking cancelled.", "success")
    return redirect(url_for("planner.booking_detail", booking_id=booking.id))


@planner_bp.route("/bookings/<int:booking_id>/quotation", methods=["POST"])
@approved_planner_required
def quotation_create(booking_id):
    booking = _own_booking(booking_id)
    form = QuotationForm()
    if booking.status not in {"pending", "accepted", "in_progress"}:
        flash("Quotations can only be sent for open bookings.", "warning")
    elif any(item.status == "pending" for item in booking.quotations):
        flash("There is already a quotation waiting for the client.", "warning")
    elif form.validate_on_submit():
        try:
            amount = parse_money_to_ngwee(form.amount.data, "Quotation amount")
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("planner.booking_detail", booking_id=booking.id))
        quotation = Quotation(
            booking_id=booking.id,
            amount=amount,
            description=form.description.data.strip(),
            status="pending",
        )
        db.session.add(quotation)
        notify(
            booking.client_id,
            "Quotation received",
            f"{booking.planner.business_name} sent a quotation for your {booking.event_type.lower()} booking.",
            f"/client/bookings/{booking.id}",
        )
        db.session.commit()
        flash("Quotation sent to the client.", "success")
    else:
        flash("Check the quotation amount and description.", "danger")
    return redirect(url_for("planner.booking_detail", booking_id=booking.id))


@planner_bp.route("/quotations")
@approved_planner_required
def quotations():
    items = (
        Quotation.query.join(PlannerBooking)
        .filter(PlannerBooking.planner_id == _profile().id)
        .order_by(Quotation.created_at.desc())
        .all()
    )
    return render_template("planner/quotations.html", quotations=items)


@planner_bp.route("/events")
@approved_planner_required
def my_events():
    items = (
        Event.query.options(selectinload(Event.artists))
        .filter_by(organiser_id=g.user.id)
        .order_by(Event.start_datetime.desc())
        .all()
    )
    return render_template("planner/my_events.html", events=items)


@planner_bp.route("/events/create", methods=["GET", "POST"])
@approved_planner_required
def create_event():
    return _save_event(None)


@planner_bp.route("/events/<int:event_id>/edit", methods=["GET", "POST"])
@approved_planner_required
def edit_event(event_id):
    return _save_event(_own_event(event_id))


def _save_event(event):
    if event and event.publication_status == "cancelled":
        flash("Cancelled events cannot be edited.", "warning")
        return redirect(url_for("planner.my_events"))
    if request.method == "POST":
        submitting = request.form.get("action") == "submit"
        poster_file = request.files.get("poster")
        has_poster = bool(event and event.poster_path) or bool(poster_file and poster_file.filename)
        payload, errors = validate_event_form(request.form, submitting, has_poster, event)
        stays_visible = bool(
            event
            and (event_is_locked(event) or event.publication_status == "suspended" or event.approval_status == "pending")
        )
        artist_rows, artist_errors = parse_artist_rows(request.form, request.files, event, submitting or stays_visible)
        errors = (errors or []) + artist_errors
        if errors:
            return _event_form(
                event,
                errors,
                _raw_ticket_rows(request.form),
                _raw_artist_rows(request.form, event),
                event_is_locked(event) if event else False,
                photos_cleared=_new_artist_photos_chosen() or bool(poster_file and poster_file.filename),
            )
        saved_poster = None
        saved_artist_photos = []
        unused_artist_photos = []
        try:
            if poster_file and poster_file.filename:
                saved_poster = save_image(poster_file, "event_posters")
            if event is None:
                event = Event(
                    title=payload["title"],
                    category_id=payload["category"].id,
                    description=payload["description"],
                    venue=payload["venue"],
                    location=payload["location"],
                    start_datetime=payload["start"],
                    end_datetime=payload["end"],
                    sales_start=payload["sales_start"],
                    sales_end=payload["sales_end"],
                    organiser_id=g.user.id,
                    contact_info=payload["contact_info"],
                    approval_status="draft",
                    publication_status="unpublished",
                )
                db.session.add(event)
                db.session.flush()
            locked = event_is_locked(event) or event.publication_status == "suspended"
            if locked:
                event.description = payload["description"]
                event.contact_info = payload["contact_info"]
            else:
                event.title = payload["title"]
                event.category_id = payload["category"].id
                event.description = payload["description"]
                event.venue = payload["venue"]
                event.location = payload["location"]
                event.start_datetime = payload["start"]
                event.end_datetime = payload["end"]
                event.sales_start = payload["sales_start"]
                event.sales_end = payload["sales_end"]
                event.contact_info = payload["contact_info"]
                sync_ticket_types(event, payload["rows"])
            unused_artist_photos = sync_artists(event, artist_rows, saved_artist_photos)
            if saved_poster:
                delete_upload(event.poster_path)
                event.poster_path = saved_poster
            if event.publication_status == "cancelled":
                raise ValueError("Cancelled events cannot be edited.")
            if event.publication_status != "suspended" and not locked:
                if event.approval_status == "approved" and event.publication_status == "published":
                    event.approval_status = "pending" if submitting else "draft"
                    event.publication_status = "unpublished"
                elif submitting:
                    event.approval_status = "pending"
                    event.publication_status = "unpublished"
                    event.rejection_reason = None
                elif event.approval_status != "pending":
                    event.approval_status = "draft"
                    event.publication_status = "unpublished"
            if submitting and event.approval_status == "pending":
                notify_admins(
                    "Event submitted",
                    f"{event.title} is waiting for approval.",
                    f"/admin/events/{event.id}",
                )
            db.session.commit()
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            if saved_poster:
                delete_upload(saved_poster)
            for path in saved_artist_photos:
                delete_upload(path)
            message = str(exc) if isinstance(exc, ValueError) else "The event could not be saved. Check the ticket details and try again."
            flash(message, "danger")
            current = event if event and event.id and db.session.get(Event, event.id) else None
            return _event_form(
                current,
                [message],
                _raw_ticket_rows(request.form),
                _raw_artist_rows(request.form, current),
                False,
                photos_cleared=True,
            )
        for path in unused_artist_photos:
            delete_upload(path)
        flash("Event submitted for approval." if submitting else "Event saved.", "success")
        return redirect(url_for("planner.my_events"))
    rows = []
    if event:
        for ticket in event.ticket_types:
            rows.append(
                {
                    "id": ticket.id,
                    "name": ticket.name,
                    "price": f"{ticket.price / 100:.2f}",
                    "quantity": ticket.quantity_total,
                    "max_per_order": ticket.max_per_order,
                    "description": ticket.description,
                }
            )
    if not rows:
        rows = [{"id": "", "name": "", "price": "", "quantity": "", "max_per_order": "10", "description": ""}]
    return _event_form(event, [], rows, _artist_rows(event), event_is_locked(event) if event else False)


@planner_bp.route("/events/<int:event_id>/delete", methods=["POST"])
@approved_planner_required
def delete_event(event_id):
    event = _own_event(event_id)
    if event.approval_status not in {"draft", "rejected", "pending"} or event_is_locked(event):
        flash("This event cannot be deleted because it is published or already has ticket activity.", "danger")
        return redirect(url_for("planner.my_events"))
    photos = [event.poster_path] + [artist.artist_image for artist in event.artists]
    db.session.delete(event)
    db.session.commit()
    for path in photos:
        delete_upload(path)
    flash("Event deleted.", "success")
    return redirect(url_for("planner.my_events"))


def _report_filters():
    """Read report filters. The planner always comes from the session; an event must be one of theirs."""
    period, error = resolve_period(request.args)
    raw_event = (request.args.get("event") or "").strip()
    event = None
    if raw_event:
        if not raw_event.isdigit():
            abort(404)
        event = _own_event(int(raw_event))
    return period, error, event, resolve_transaction_type(request.args)


@planner_bp.route("/reports")
@approved_planner_required
def reports():
    profile = _profile()
    period, error, event, transaction_type = _report_filters()
    event_options = (
        Event.query.with_entities(Event.id, Event.title)
        .filter_by(organiser_id=g.user.id)
        .order_by(Event.start_datetime.desc())
        .all()
    )
    return render_template(
        "planner/reports.html",
        profile=profile,
        report=planner_report(profile, period, event, transaction_type),
        filter_error=error,
        event_options=event_options,
        periods=PERIODS,
        transaction_types=TRANSACTION_TYPES,
        export_endpoint="planner.reports_export",
        admin_view=False,
    )


@planner_bp.route("/reports/export.<fmt>")
@approved_planner_required
def reports_export(fmt):
    if fmt not in EXPORT_FORMATS:
        abort(404)
    profile = _profile()
    period, error, event, transaction_type = _report_filters()
    if error:
        flash(error, "danger")
        return redirect(url_for("planner.reports"))
    report = planner_report(profile, period, event, transaction_type)
    return export_response(planner_document(profile, report), fmt, profile.business_name, period.key)


@planner_bp.route("/notifications")
@approved_planner_required
def notifications():
    return render_template("planner/notifications.html", notifications=list_for_user(g.user))


@planner_bp.route("/notifications/<int:notification_id>/read", methods=["POST"])
@planner_account_required
def notification_read(notification_id):
    if not mark_read(g.user, notification_id):
        return render_template("errors/404.html"), 404
    return redirect(request.referrer or url_for("planner.notifications"))


@planner_bp.route("/notifications/read-all", methods=["POST"])
@planner_account_required
def notifications_read_all():
    mark_all_read(g.user)
    return redirect(url_for("planner.notifications"))


from app.planner import attendants as _attendant_routes  # noqa: E402,F401  registers the gate attendant pages
