"""Server-side validation helpers."""

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from email_validator import EmailNotValidError, validate_email

LUSAKA = ZoneInfo("Africa/Lusaka")
PHONE_PATTERN = re.compile(r"^\+260[79]\d{8}$")
PERSON_NAME = re.compile(r"^[A-Za-z][A-Za-z' .\-]{1,119}$")
BUSINESS_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '&.,\-]{1,119}$")
MONEY_PATTERN = re.compile(r"^\d{1,8}(\.\d{1,2})?$")
PACRA_NUMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/\-]{4,39}$")
PACRA_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/\-]{2,59}$")
PLANNER_CATEGORIES = [
    "Full event planning",
    "Wedding planning",
    "Corporate events and conferences",
    "Parties and celebrations",
    "Decor and styling",
    "Catering",
    "Sound, lighting and entertainment",
    "Photography and videography",
    "Venue and logistics",
    "Other",
]


def validate_pacra_number(value):
    number = re.sub(r"\s+", "", value or "").upper()
    if not PACRA_NUMBER.fullmatch(number):
        return None, "Enter the PACRA registration number using letters, numbers, slashes, or hyphens (5–40 characters)."
    return number, None


def validate_pacra_reference(value):
    reference = re.sub(r"\s+", "", value or "").upper()
    if not reference:
        return None, None
    if not PACRA_REFERENCE.fullmatch(reference):
        return None, "Enter the PACRA verification or reference code using letters, numbers, slashes, or hyphens (3–60 characters)."
    return reference, None


def clean_text(value, limit):
    text = " ".join((value or "").split())
    return text[:limit]


def validate_person_name(value):
    name = clean_text(value, 120)
    if not PERSON_NAME.fullmatch(name):
        return None, "Enter a valid name using letters, spaces, hyphens, or apostrophes (2–120 characters)."
    return name, None


def validate_business_name(value):
    name = clean_text(value, 120)
    if not BUSINESS_NAME.fullmatch(name):
        return None, "Enter a valid business name (2–120 characters)."
    return name, None


def validate_email_address(value):
    raw = (value or "").strip().lower()
    if len(raw) > 120:
        return None, "Email addresses must be 120 characters or fewer."
    try:
        result = validate_email(raw, check_deliverability=False)
    except EmailNotValidError:
        return None, "Enter a valid email address."
    return result.normalized.lower(), None


def normalize_phone(value):
    raw = re.sub(r"[\s\-()]", "", (value or "").strip())
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    if raw.startswith("0") and len(raw) == 10:
        raw = "+260" + raw[1:]
    elif raw.startswith("260") and len(raw) == 12:
        raw = "+" + raw
    if not PHONE_PATTERN.fullmatch(raw):
        return None, "Enter a valid Zambian phone number, such as +260971234567 or 0971234567."
    return raw, None


def validate_password(password):
    password = password or ""
    if not 8 <= len(password) <= 128:
        return "Password must be 8 to 128 characters."
    if not re.search(r"[A-Z]", password):
        return "Password must include an uppercase letter."
    if not re.search(r"[a-z]", password):
        return "Password must include a lowercase letter."
    if not re.search(r"\d", password):
        return "Password must include a number."
    return None


def validate_required_text(value, label, minimum, maximum):
    text = (value or "").strip()
    if not minimum <= len(text) <= maximum:
        return None, f"{label} must be between {minimum} and {maximum} characters."
    return text, None


def parse_money_to_ngwee(value, label="Amount"):
    raw = (value or "").strip().replace(",", "")
    if raw.startswith("K"):
        raw = raw[1:].strip()
    if not MONEY_PATTERN.fullmatch(raw):
        raise ValueError(f"{label} must be a positive amount in ZMW with up to 2 decimal places.")
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{label} is not a valid amount.") from exc
    if amount <= 0:
        raise ValueError(f"{label} must be greater than zero.")
    return int(amount * 100)


def parse_non_negative_money(value, label="Price"):
    raw = (value or "").strip().replace(",", "")
    if raw.startswith("K"):
        raw = raw[1:].strip()
    if not MONEY_PATTERN.fullmatch(raw):
        raise ValueError(f"{label} must be a ZMW amount with up to 2 decimal places.")
    amount = Decimal(raw)
    if amount <= 0:
        raise ValueError(f"{label} must be greater than zero for a paid ticket.")
    return int(amount * 100)


def parse_positive_int(value, label, maximum):
    raw = (value or "").strip()
    if not re.fullmatch(r"\d+", raw):
        raise ValueError(f"{label} must be a whole number.")
    number = int(raw)
    if number < 1 or number > maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}.")
    return number


def parse_local_datetime(value, label):
    raw = (value or "").strip()
    try:
        local = datetime.strptime(raw, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ValueError(f"Enter a valid {label}.") from exc
    aware = local.replace(tzinfo=LUSAKA)
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local_input(dt):
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LUSAKA).strftime("%Y-%m-%dT%H:%M")


def parse_future_booking_slot(date_value, time_value, duration_hours):
    try:
        event_date = datetime.strptime((date_value or "").strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Enter a valid event date.") from exc
    try:
        start = datetime.strptime((time_value or "").strip(), "%H:%M").time()
    except ValueError as exc:
        raise ValueError("Enter a valid start time.") from exc
    raw_duration = (duration_hours or "").strip()
    if not re.fullmatch(r"\d{1,2}(\.\d)?", raw_duration):
        raise ValueError("Duration must be a number of hours, such as 4 or 2.5.")
    hours = Decimal(raw_duration)
    if hours <= 0 or hours > 18:
        raise ValueError("Duration must be greater than 0 and at most 18 hours.")
    minutes = int(hours * 60)
    local_start = datetime.combine(event_date, start, tzinfo=LUSAKA)
    if local_start <= datetime.now(LUSAKA):
        raise ValueError("The event date and start time must be in the future.")
    return event_date, start, minutes


def cancellation_allowed(booking, actor_role):
    if booking.status in {"completed", "rejected", "cancelled"}:
        return False
    if actor_role == "planner" and booking.status in {"pending", "accepted"}:
        return True
    if actor_role == "client" and booking.status == "pending":
        return True
    if actor_role == "client" and booking.status == "accepted":
        start = datetime.combine(booking.event_date, booking.start_time, tzinfo=LUSAKA)
        return start - datetime.now(LUSAKA) >= timedelta(hours=48)
    return False
