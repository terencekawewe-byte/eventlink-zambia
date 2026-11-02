"""SQLite models for EventLink Zambia.

Money is stored as integer ngwee (1 ZMW = 100 ngwee). Timestamps are naive UTC.
"""

from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


DEFAULT_CATEGORIES = [
    ("Concerts", "Live music concerts and artist showcases."),
    ("Music festivals", "Multi-artist festivals and outdoor music events."),
    ("Bashes and parties", "Club nights, bashes, and social parties."),
    ("Weddings", "Wedding ceremonies, receptions, and related celebrations."),
    ("Birthdays", "Birthday parties and milestone celebrations."),
    ("Graduations", "Graduation ceremonies and celebration events."),
    ("Corporate events", "Company functions, launches, and staff events."),
    ("Conferences", "Conferences, summits, and professional gatherings."),
    ("Religious events", "Faith gatherings, crusades, and worship events."),
    ("Other events", "Occasions that do not fit the categories above."),
]
USER_ROLES = {"client": "Client", "planner": "Planner", "admin": "Admin", "attendant": "Gate attendant"}


class User(db.Model):
    __tablename__ = "users"
    __table_args__ = (
        db.CheckConstraint("role IN ('client', 'planner', 'admin', 'attendant')", name="ck_users_role"),
        db.CheckConstraint("account_status IN ('active', 'suspended')", name="ck_users_status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(20), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, index=True)
    account_status = db.Column(db.String(20), nullable=False, default="active", index=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    planner_profile = db.relationship(
        "PlannerProfile",
        back_populates="user",
        uselist=False,
        foreign_keys="PlannerProfile.user_id",
    )
    notifications = db.relationship("Notification", back_populates="user")
    gate_profile = db.relationship(
        "GateAttendant",
        back_populates="user",
        uselist=False,
        foreign_keys="GateAttendant.user_id",
    )

    def set_password(self, password):
        from werkzeug.security import generate_password_hash

        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        from werkzeug.security import check_password_hash

        return check_password_hash(self.password_hash, password)

    @property
    def initials(self):
        parts = [part for part in (self.full_name or "").split() if part]
        if not parts:
            return "EL"
        return "".join(part[0] for part in parts[:2]).upper()

    @property
    def is_approved_planner(self):
        profile = self.planner_profile
        return (
            self.role == "planner"
            and self.account_status == "active"
            and profile is not None
            and profile.approval_status == "approved"
        )

    @property
    def role_label(self):
        return USER_ROLES.get(self.role, self.role.title())

    @property
    def is_active_attendant(self):
        profile = self.gate_profile
        return (
            self.role == "attendant"
            and self.account_status == "active"
            and profile is not None
            and profile.status == "active"
        )


class PlannerProfile(db.Model):
    __tablename__ = "planner_profiles"
    __table_args__ = (
        db.CheckConstraint(
            "approval_status IN ('pending', 'approved', 'rejected', 'suspended')",
            name="ck_planner_approval",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    business_name = db.Column(db.String(120), nullable=False)
    biography = db.Column(db.Text, nullable=False, default="")
    location = db.Column(db.String(120), nullable=False)
    experience = db.Column(db.Text, nullable=False, default="")
    specialities = db.Column(db.String(300), nullable=False, default="")
    availability = db.Column(db.Text, nullable=False, default="")
    profile_photo = db.Column(db.String(255))
    business_category = db.Column(db.String(80))
    business_address = db.Column(db.String(255))
    pacra_number = db.Column(db.String(40))
    pacra_reference = db.Column(db.String(60))
    pacra_document = db.Column(db.String(255))
    approval_status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    rejection_reason = db.Column(db.String(500))
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    terms_accepted = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id], back_populates="planner_profile")
    approver = db.relationship("User", foreign_keys=[approved_by])
    services = db.relationship(
        "PlannerService",
        back_populates="planner",
        cascade="all, delete-orphan",
    )
    portfolio_images = db.relationship(
        "PlannerPortfolio",
        back_populates="planner",
        cascade="all, delete-orphan",
    )
    bookings = db.relationship("PlannerBooking", back_populates="planner")

    @property
    def profile_checklist(self):
        return [
            ("Business name", bool(self.business_name)),
            ("Biography of at least 20 characters", bool(self.biography and len(self.biography) >= 20)),
            ("Location", bool(self.location)),
            ("Experience of at least 20 characters", bool(self.experience and len(self.experience) >= 20)),
            ("Specialities", bool(self.specialities)),
            ("At least one service", bool(self.services)),
            ("Profile photo", bool(self.profile_photo)),
            ("Portfolio images", bool(self.portfolio_images)),
            ("Availability", bool(self.availability)),
        ]

    @property
    def initials(self):
        parts = [part for part in (self.business_name or "").split() if part[:1].isalnum()]
        return "".join(part[0] for part in parts[:2]).upper() or "EL"

    @property
    def speciality_list(self):
        return [item.strip() for item in (self.specialities or "").split(",") if item.strip()]

    @property
    def category_label(self):
        if self.business_category:
            return self.business_category
        specialities = self.speciality_list
        return specialities[0] if specialities else None

    @property
    def missing_profile_items(self):
        return [label for label, done in self.profile_checklist if not done]

    @property
    def completion_percent(self):
        checks = self.profile_checklist
        return int(round(100 * sum(1 for _, done in checks if done) / len(checks)))


class PlannerService(db.Model):
    __tablename__ = "planner_services"
    __table_args__ = (
        db.CheckConstraint(
            "pricing_type IN ('fixed', 'starting_from', 'quotation')",
            name="ck_service_pricing",
        ),
        db.CheckConstraint(
            "starting_price IS NULL OR starting_price >= 0",
            name="ck_service_price",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    planner_id = db.Column(db.Integer, db.ForeignKey("planner_profiles.id"), nullable=False, index=True)
    service_name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    starting_price = db.Column(db.Integer)
    pricing_type = db.Column(db.String(20), nullable=False, default="quotation")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    planner = db.relationship("PlannerProfile", back_populates="services")


class PlannerPortfolio(db.Model):
    __tablename__ = "planner_portfolio"

    id = db.Column(db.Integer, primary_key=True)
    planner_id = db.Column(db.Integer, db.ForeignKey("planner_profiles.id"), nullable=False, index=True)
    file_path = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    planner = db.relationship("PlannerProfile", back_populates="portfolio_images")


class EventCategory(db.Model):
    __tablename__ = "event_categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    description = db.Column(db.String(255), nullable=False, default="")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    events = db.relationship("Event", back_populates="category")


class Event(db.Model):
    __tablename__ = "events"
    __table_args__ = (
        db.CheckConstraint(
            "approval_status IN ('draft', 'pending', 'approved', 'rejected')",
            name="ck_event_approval",
        ),
        db.CheckConstraint(
            "publication_status IN ('unpublished', 'published', 'suspended', 'cancelled', 'completed')",
            name="ck_event_publication",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey("event_categories.id"), nullable=False, index=True)
    description = db.Column(db.Text, nullable=False, default="")
    venue = db.Column(db.String(200), nullable=False, default="")
    location = db.Column(db.String(120), nullable=False, default="", index=True)
    start_datetime = db.Column(db.DateTime, nullable=False, index=True)
    end_datetime = db.Column(db.DateTime, nullable=False)
    sales_start = db.Column(db.DateTime, nullable=False)
    sales_end = db.Column(db.DateTime, nullable=False)
    poster_path = db.Column(db.String(255))
    organiser_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    contact_info = db.Column(db.String(200), nullable=False, default="")
    approval_status = db.Column(db.String(20), nullable=False, default="draft", index=True)
    publication_status = db.Column(db.String(20), nullable=False, default="unpublished", index=True)
    rejection_reason = db.Column(db.String(500))
    cancellation_reason = db.Column(db.String(500))
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    category = db.relationship("EventCategory", back_populates="events")
    organiser = db.relationship("User", foreign_keys=[organiser_id])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    ticket_types = db.relationship(
        "TicketType",
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="TicketType.price",
    )
    orders = db.relationship("Order", back_populates="event")
    artists = db.relationship(
        "EventArtist",
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="EventArtist.id",
    )

    @property
    def is_public(self):
        return self.approval_status == "approved" and self.publication_status == "published"


ARTIST_CATEGORIES = ["Musician/Singer", "DJ", "Band", "Comedian", "Dancer", "Other Performer"]


class EventArtist(db.Model):
    __tablename__ = "event_artists"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    artist_name = db.Column(db.String(120), nullable=False)
    artist_category = db.Column(db.String(40), nullable=False)
    artist_image = db.Column(db.String(255))
    performance_description = db.Column(db.String(500), nullable=False, default="")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    event = db.relationship("Event", back_populates="artists")

    @property
    def initials(self):
        words = [word for word in self.artist_name.split() if word[:1].isalnum()]
        return "".join(word[0] for word in words[:2]).upper() or "A"


class TicketType(db.Model):
    __tablename__ = "ticket_types"
    __table_args__ = (
        db.CheckConstraint("price >= 0", name="ck_ticket_price"),
        db.CheckConstraint("quantity_total > 0", name="ck_ticket_qty_total"),
        db.CheckConstraint("quantity_sold >= 0", name="ck_ticket_qty_sold"),
        db.CheckConstraint("quantity_reserved >= 0", name="ck_ticket_qty_reserved"),
        db.CheckConstraint(
            "quantity_sold + quantity_reserved <= quantity_total",
            name="ck_ticket_qty_bounds",
        ),
        db.CheckConstraint("max_per_order > 0", name="ck_ticket_max"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), nullable=False, index=True)
    name = db.Column(db.String(40), nullable=False)
    description = db.Column(db.String(300), nullable=False, default="")
    price = db.Column(db.Integer, nullable=False)
    quantity_total = db.Column(db.Integer, nullable=False)
    quantity_sold = db.Column(db.Integer, nullable=False, default=0)
    quantity_reserved = db.Column(db.Integer, nullable=False, default=0)
    max_per_order = db.Column(db.Integer, nullable=False, default=10)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    event = db.relationship("Event", back_populates="ticket_types")

    @property
    def remaining(self):
        return self.quantity_total - self.quantity_sold - self.quantity_reserved


class Order(db.Model):
    __tablename__ = "orders"
    __table_args__ = (
        db.CheckConstraint("total_amount >= 0", name="ck_order_total"),
        db.CheckConstraint(
            "order_status IN ('pending', 'paid', 'failed', 'cancelled', 'refunded')",
            name="ck_order_status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_reference = db.Column(db.String(40), unique=True, nullable=False, index=True)
    client_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), nullable=False, index=True)
    total_amount = db.Column(db.Integer, nullable=False)
    order_status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    client = db.relationship("User", foreign_keys=[client_id])
    event = db.relationship("Event", back_populates="orders")
    items = db.relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    payments = db.relationship("Payment", back_populates="order")


class OrderItem(db.Model):
    __tablename__ = "order_items"
    __table_args__ = (
        db.CheckConstraint("quantity > 0", name="ck_item_qty"),
        db.CheckConstraint("unit_price >= 0", name="ck_item_price"),
        db.CheckConstraint("subtotal = quantity * unit_price", name="ck_item_subtotal"),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False, index=True)
    ticket_type_id = db.Column(db.Integer, db.ForeignKey("ticket_types.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Integer, nullable=False)
    subtotal = db.Column(db.Integer, nullable=False)

    order = db.relationship("Order", back_populates="items")
    ticket_type = db.relationship("TicketType")
    tickets = db.relationship("Ticket", back_populates="order_item")


class Payment(db.Model):
    __tablename__ = "payments"
    __table_args__ = (
        db.CheckConstraint("amount > 0", name="ck_payment_amount"),
        db.CheckConstraint(
            "status IN ('pending', 'successful', 'failed', 'refunded')",
            name="ck_payment_status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False, index=True)
    payment_reference = db.Column(db.String(40), unique=True, nullable=False, index=True)
    provider_reference = db.Column(db.String(80))
    payment_method = db.Column(db.String(40), nullable=False, default="unassigned")
    amount = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    is_simulated = db.Column(db.Boolean, nullable=False, default=True, index=True)
    provider_status = db.Column(db.String(40))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    verified_at = db.Column(db.DateTime)

    order = db.relationship("Order", back_populates="payments")
    refunds = db.relationship("Refund", back_populates="payment")


class PaymentCallback(db.Model):
    __tablename__ = "payment_callbacks"

    id = db.Column(db.Integer, primary_key=True)
    idempotency_key = db.Column(db.String(80), unique=True, nullable=False)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id"), nullable=False, index=True)
    payload_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    payment = db.relationship("Payment")


class Refund(db.Model):
    __tablename__ = "refunds"
    __table_args__ = (
        db.CheckConstraint("amount > 0", name="ck_refund_amount"),
        db.CheckConstraint(
            "status IN ('requested', 'approved', 'rejected', 'completed')",
            name="ck_refund_status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id"), nullable=False, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False, index=True)
    amount = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="requested", index=True)
    requested_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    processed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    confirmation_note = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    payment = db.relationship("Payment", back_populates="refunds")
    order = db.relationship("Order")
    requester = db.relationship("User", foreign_keys=[requested_by])
    processor = db.relationship("User", foreign_keys=[processed_by])


class Ticket(db.Model):
    __tablename__ = "tickets"
    __table_args__ = (
        db.CheckConstraint(
            "status IN ('valid', 'redeemed', 'cancelled', 'refunded')",
            name="ck_ticket_status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    ticket_code = db.Column(db.String(40), unique=True, nullable=False, index=True)
    qr_token = db.Column(db.String(80), unique=True, nullable=False, index=True)
    order_item_id = db.Column(db.Integer, db.ForeignKey("order_items.id"), nullable=False, index=True)
    client_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), nullable=False, index=True)
    ticket_type_id = db.Column(db.Integer, db.ForeignKey("ticket_types.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="valid", index=True)
    issued_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    redeemed_at = db.Column(db.DateTime)
    redeemed_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    order_item = db.relationship("OrderItem", back_populates="tickets")
    client = db.relationship("User", foreign_keys=[client_id])
    event = db.relationship("Event")
    ticket_type = db.relationship("TicketType")
    redeemer = db.relationship("User", foreign_keys=[redeemed_by])


SCAN_RESULTS = {"valid": "Valid ticket", "already_used": "Already used", "invalid": "Invalid ticket"}
SCAN_REASONS = {
    "unreadable": "The code is not an EventLink ticket code.",
    "not_found": "No ticket matches this code.",
    "wrong_event": "This ticket is for a different event.",
    "cancelled": "This ticket has been cancelled.",
    "refunded": "This ticket has been refunded.",
    "not_paid": "Payment for this ticket has not been verified.",
    "event_cancelled": "This event has been cancelled.",
    "not_eligible": "This ticket is not eligible for entry.",
}
SCAN_METHODS = {"camera": "Camera", "manual": "Typed code", "staff": "Staff check"}


class TicketRedemption(db.Model):
    """One verification attempt at the gate, whatever its result."""

    __tablename__ = "ticket_redemptions"
    __table_args__ = (
        db.CheckConstraint("result IN ('valid', 'already_used', 'invalid')", name="ck_redemption_result"),
        db.CheckConstraint("method IS NULL OR method IN ('camera', 'manual', 'staff')", name="ck_redemption_method"),
    )

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("tickets.id"), index=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    result = db.Column(db.String(20), nullable=False, index=True)
    reason = db.Column(db.String(40))
    method = db.Column(db.String(10))
    attempted_code = db.Column(db.String(40))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    ticket = db.relationship("Ticket")
    event = db.relationship("Event")
    actor = db.relationship("User")

    @property
    def result_label(self):
        return SCAN_RESULTS.get(self.result, self.result)

    @property
    def reason_text(self):
        return SCAN_REASONS.get(self.reason, "") if self.reason else ""

    @property
    def method_label(self):
        return SCAN_METHODS.get(self.method, "")

    @property
    def shows_ticket(self):
        """Tickets from another event are not identified to the people running this one."""
        return self.ticket is not None and self.ticket.event_id == self.event_id


class GateAttendant(db.Model):
    """Gate staff account created by a planner to scan tickets at their events."""

    __tablename__ = "gate_attendants"
    __table_args__ = (db.CheckConstraint("status IN ('active', 'inactive')", name="ck_gate_attendant_status"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="active", index=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id], back_populates="gate_profile")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    assignments = db.relationship(
        "AttendantAssignment",
        back_populates="attendant",
        order_by="AttendantAssignment.created_at.desc()",
    )

    @property
    def active_assignments(self):
        return [assignment for assignment in self.assignments if assignment.status == "active"]


class AttendantAssignment(db.Model):
    __tablename__ = "attendant_assignments"
    __table_args__ = (
        db.UniqueConstraint("attendant_id", "event_id", name="uq_attendant_event"),
        db.CheckConstraint("status IN ('active', 'revoked')", name="ck_assignment_status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    attendant_id = db.Column(db.Integer, db.ForeignKey("gate_attendants.id"), nullable=False, index=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), nullable=False, index=True)
    assigned_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="active", index=True)
    revoked_at = db.Column(db.DateTime)
    revoked_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    attendant = db.relationship("GateAttendant", back_populates="assignments")
    event = db.relationship("Event")
    assigned_by = db.relationship("User", foreign_keys=[assigned_by_id])
    revoked_by = db.relationship("User", foreign_keys=[revoked_by_id])


class PlannerBooking(db.Model):
    __tablename__ = "planner_bookings"
    __table_args__ = (
        db.CheckConstraint("guest_count > 0", name="ck_booking_guests"),
        db.CheckConstraint("budget > 0", name="ck_booking_budget"),
        db.CheckConstraint("duration > 0", name="ck_booking_duration"),
        db.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'in_progress', 'completed', 'cancelled')",
            name="ck_booking_status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    planner_id = db.Column(db.Integer, db.ForeignKey("planner_profiles.id"), nullable=False, index=True)
    event_type = db.Column(db.String(80), nullable=False)
    event_date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.Time, nullable=False)
    duration = db.Column(db.Integer, nullable=False)
    venue = db.Column(db.String(200), nullable=False)
    guest_count = db.Column(db.Integer, nullable=False)
    budget = db.Column(db.Integer, nullable=False)
    requirements = db.Column(db.Text, nullable=False)
    contact_method = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    rejection_reason = db.Column(db.String(500))
    cancellation_reason = db.Column(db.String(500))
    cancelled_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    client = db.relationship("User", foreign_keys=[client_id])
    planner = db.relationship("PlannerProfile", back_populates="bookings")
    canceller = db.relationship("User", foreign_keys=[cancelled_by])
    quotations = db.relationship("Quotation", back_populates="booking", order_by="Quotation.created_at")


class Quotation(db.Model):
    __tablename__ = "quotations"
    __table_args__ = (
        db.CheckConstraint("amount > 0", name="ck_quote_amount"),
        db.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined')",
            name="ck_quote_status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("planner_bookings.id"), nullable=False, index=True)
    amount = db.Column(db.Integer, nullable=False)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    responded_at = db.Column(db.DateTime)

    booking = db.relationship("PlannerBooking", back_populates="quotations")


PAYOUT_METHODS = {"bank_transfer": "Bank transfer", "mobile_money": "Mobile money"}


class PlannerPayout(db.Model):
    """Money an administrator confirms was sent to a planner outside EventLink."""

    __tablename__ = "planner_payouts"
    __table_args__ = (
        db.CheckConstraint("amount > 0", name="ck_payout_amount"),
        db.CheckConstraint("method IN ('bank_transfer', 'mobile_money')", name="ck_payout_method"),
        db.UniqueConstraint("planner_id", "external_reference", name="uq_payout_external_reference"),
    )

    id = db.Column(db.Integer, primary_key=True)
    payout_reference = db.Column(db.String(40), unique=True, nullable=False, index=True)
    planner_id = db.Column(db.Integer, db.ForeignKey("planner_profiles.id"), nullable=False, index=True)
    amount = db.Column(db.Integer, nullable=False)
    method = db.Column(db.String(20), nullable=False)
    external_reference = db.Column(db.String(80), nullable=False)
    note = db.Column(db.String(500), nullable=False, default="")
    recorded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    planner = db.relationship("PlannerProfile")
    recorder = db.relationship("User")

    @property
    def method_label(self):
        return PAYOUT_METHODS.get(self.method, self.method)


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    message = db.Column(db.String(2000), nullable=False)
    link = db.Column(db.String(255))
    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship("User", back_populates="notifications")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.String(80), nullable=False, index=True)
    entity_type = db.Column(db.String(40), nullable=False)
    entity_id = db.Column(db.Integer)
    reason = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    actor = db.relationship("User")


class ContactMessage(db.Model):
    __tablename__ = "contact_messages"
    __table_args__ = (
        db.CheckConstraint("status IN ('new', 'reviewed')", name="ck_contact_status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    subject = db.Column(db.String(150), nullable=False)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="new", index=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)


class LoginAttempt(db.Model):
    __tablename__ = "login_attempts"
    __table_args__ = (db.Index("ix_login_attempts_email_created", "email", "created_at"),)

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False)
    ip_address = db.Column(db.String(64), nullable=False, default="")
    success = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)


def seed_reference_data():
    if EventCategory.query.count() == 0:
        for name, description in DEFAULT_CATEGORIES:
            db.session.add(EventCategory(name=name, description=description, is_active=True))
        db.session.commit()
