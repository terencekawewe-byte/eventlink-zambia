"""Client dashboard, tickets, bookings, and profile."""

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import (
    EventCategory,
    Order,
    PlannerBooking,
    PlannerProfile,
    Quotation,
    Refund,
    Ticket,
    User,
)
from app.models.database import utcnow
from app.planner.forms import BookingRequestForm
from app.services.booking_service import client_can_cancel
from app.services.dashboard_service import client_overview
from app.services.notification_service import list_for_user, mark_all_read, mark_read, notify
from app.utils.decorators import client_required
from app.utils.validators import (
    normalize_phone,
    parse_future_booking_slot,
    parse_money_to_ngwee,
    validate_person_name,
)

client_bp = Blueprint("client", __name__, url_prefix="/client")


def _own_booking(booking_id):
    return PlannerBooking.query.filter_by(id=booking_id, client_id=g.user.id).first_or_404()


@client_bp.route("/dashboard")
@client_required
def dashboard():
    return render_template("client/dashboard.html", **client_overview(g.user))


@client_bp.route("/profile", methods=["GET", "POST"])
@client_required
def profile():
    if request.method == "POST":
        name, name_error = validate_person_name(request.form.get("full_name"))
        phone, phone_error = normalize_phone(request.form.get("phone"))
        if name_error or phone_error:
            flash(name_error or phone_error, "danger")
        elif User.query.filter(User.phone == phone, User.id != g.user.id).first():
            flash("That phone number is already used by another account.", "danger")
        else:
            g.user.full_name = name
            g.user.phone = phone
            db.session.commit()
            flash("Your profile has been updated.", "success")
            return redirect(url_for("client.profile"))
    return render_template("client/profile.html")


@client_bp.route("/tickets")
@client_required
def tickets():
    items = (
        Ticket.query.options(joinedload(Ticket.event), joinedload(Ticket.ticket_type))
        .filter_by(client_id=g.user.id)
        .order_by(Ticket.issued_at.desc())
        .all()
    )
    return render_template("client/my_tickets.html", tickets=items)


@client_bp.route("/tickets/<int:ticket_id>")
@client_required
def ticket_detail(ticket_id):
    ticket = (
        Ticket.query.options(joinedload(Ticket.event), joinedload(Ticket.ticket_type), joinedload(Ticket.order_item))
        .filter_by(id=ticket_id, client_id=g.user.id)
        .first_or_404()
    )
    return render_template("client/ticket_details.html", ticket=ticket)


@client_bp.route("/bookings")
@client_required
def bookings():
    items = (
        PlannerBooking.query.options(joinedload(PlannerBooking.planner))
        .filter_by(client_id=g.user.id)
        .order_by(PlannerBooking.created_at.desc())
        .all()
    )
    return render_template("client/my_bookings.html", bookings=items)


@client_bp.route("/bookings/new/<int:planner_id>", methods=["GET", "POST"])
@client_required
def booking_new(planner_id):
    profile = PlannerProfile.query.get_or_404(planner_id)
    if profile.approval_status != "approved" or profile.user.account_status != "active":
        flash("This planner is not available for bookings.", "warning")
        return redirect(url_for("events.planners"))
    form = BookingRequestForm()
    form.event_type.choices = [(category.name, category.name) for category in EventCategory.query.filter_by(is_active=True).order_by(EventCategory.name)]
    if form.validate_on_submit():
        try:
            event_date, start_time, minutes = parse_future_booking_slot(
                form.event_date.data, form.start_time.data, form.duration.data
            )
            budget = parse_money_to_ngwee(form.budget.data, "Budget")
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("client/booking_form.html", form=form, profile=profile)
        booking = PlannerBooking(
            client_id=g.user.id,
            planner_id=profile.id,
            event_type=form.event_type.data,
            event_date=event_date,
            start_time=start_time,
            duration=minutes,
            venue=form.venue.data.strip(),
            guest_count=int(form.guest_count.data),
            budget=budget,
            requirements=form.requirements.data.strip(),
            contact_method=form.contact_method.data,
            status="pending",
        )
        db.session.add(booking)
        db.session.flush()
        notify(
            profile.user_id,
            "New booking request",
            f"{g.user.full_name} requested planning for a {booking.event_type.lower()} on {booking.event_date.isoformat()}.",
            f"/planner/bookings/{booking.id}",
        )
        db.session.commit()
        flash("Your booking request has been sent to the planner.", "success")
        return redirect(url_for("client.booking_detail", booking_id=booking.id))
    return render_template("client/booking_form.html", form=form, profile=profile)


@client_bp.route("/bookings/<int:booking_id>")
@client_required
def booking_detail(booking_id):
    booking = _own_booking(booking_id)
    return render_template(
        "client/booking_details.html",
        booking=booking,
        can_cancel=client_can_cancel(booking),
    )


@client_bp.route("/bookings/<int:booking_id>/cancel", methods=["POST"])
@client_required
def booking_cancel(booking_id):
    booking = _own_booking(booking_id)
    reason = (request.form.get("reason") or "").strip()
    if not client_can_cancel(booking):
        flash("This booking can no longer be cancelled online. Accepted bookings can be cancelled up to 48 hours before the start time.", "warning")
    elif not 5 <= len(reason) <= 500:
        flash("Provide a cancellation reason between 5 and 500 characters.", "danger")
    else:
        booking.status = "cancelled"
        booking.cancellation_reason = reason
        booking.cancelled_by = g.user.id
        notify(
            booking.planner.user_id,
            "Booking cancelled",
            f"{g.user.full_name} cancelled the {booking.event_type.lower()} request.",
            f"/planner/bookings/{booking.id}",
        )
        db.session.commit()
        flash("The booking has been cancelled.", "success")
    return redirect(url_for("client.booking_detail", booking_id=booking.id))


@client_bp.route("/quotations/<int:quotation_id>", methods=["POST"])
@client_required
def quotation_respond(quotation_id):
    quotation = Quotation.query.get_or_404(quotation_id)
    if quotation.booking.client_id != g.user.id:
        return render_template("errors/404.html"), 404
    if quotation.status != "pending":
        flash("This quotation has already been answered.", "info")
        return redirect(url_for("client.booking_detail", booking_id=quotation.booking_id))
    decision = request.form.get("decision")
    if decision not in {"accepted", "declined"}:
        flash("Choose whether to accept or decline the quotation.", "danger")
        return redirect(url_for("client.booking_detail", booking_id=quotation.booking_id))
    quotation.status = decision
    quotation.responded_at = utcnow()
    notify(
        quotation.booking.planner.user_id,
        "Quotation response",
        f"{g.user.full_name} {decision} the quotation for booking #{quotation.booking_id}.",
        f"/planner/bookings/{quotation.booking_id}",
    )
    db.session.commit()
    flash(f"You {decision} the quotation.", "success")
    return redirect(url_for("client.booking_detail", booking_id=quotation.booking_id))


@client_bp.route("/payments")
@client_required
def payments():
    orders = (
        Order.query.options(joinedload(Order.event), joinedload(Order.payments), joinedload(Order.items))
        .filter_by(client_id=g.user.id)
        .order_by(Order.created_at.desc())
        .all()
    )
    return render_template("client/payment_history.html", orders=orders)


@client_bp.route("/orders/<order_reference>/refund", methods=["POST"])
@client_required
def refund_request(order_reference):
    order = Order.query.filter_by(order_reference=order_reference, client_id=g.user.id).first_or_404()
    payment = next((item for item in order.payments if item.status == "successful"), None)
    reason = (request.form.get("reason") or "").strip()
    if order.event.publication_status != "cancelled":
        flash("A refund request can be opened when the event has been cancelled.", "warning")
    elif payment is None:
        flash("There is no verified payment on this order.", "warning")
    elif Refund.query.filter(
        Refund.payment_id == payment.id, Refund.status.in_(["requested", "approved", "completed"])
    ).first():
        flash("A refund request already exists for this payment.", "info")
    elif not 5 <= len(reason) <= 500:
        flash("Provide a refund reason between 5 and 500 characters.", "danger")
    else:
        refund = Refund(
            payment_id=payment.id,
            order_id=order.id,
            amount=payment.amount,
            reason=reason,
            status="requested",
            requested_by=g.user.id,
        )
        db.session.add(refund)
        from app.services.notification_service import notify_admins

        notify_admins(
            "Refund requested",
            f"{g.user.full_name} requested a refund for order {order.order_reference}.",
            "/admin/payments",
        )
        db.session.commit()
        flash("Your refund request has been recorded. Completion depends on confirmation outside EventLink.", "success")
    return redirect(url_for("client.payments"))


@client_bp.route("/notifications")
@client_required
def notifications():
    return render_template("client/notifications.html", notifications=list_for_user(g.user))


@client_bp.route("/notifications/<int:notification_id>/read", methods=["POST"])
@client_required
def notification_read(notification_id):
    if not mark_read(g.user, notification_id):
        return render_template("errors/404.html"), 404
    return redirect(request.referrer or url_for("client.notifications"))


@client_bp.route("/notifications/read-all", methods=["POST"])
@client_required
def notifications_read_all():
    mark_all_read(g.user)
    return redirect(url_for("client.notifications"))
