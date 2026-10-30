"""Validation and persistence rules for public events."""

import re

from app.extensions import db
from app.models import EventArtist, EventCategory, TicketType
from app.models.database import ARTIST_CATEGORIES
from app.utils.helpers import save_image
from app.utils.validators import (
    parse_local_datetime,
    parse_non_negative_money,
    parse_positive_int,
    utcnow_naive,
    validate_required_text,
)


def _blank_ticket_row(name, price, qty, maximum, description):
    return not any((part or "").strip() for part in (name, price, qty, maximum, description))


def parse_ticket_rows(form, required):
    names = form.getlist("ticket_name")
    prices = form.getlist("ticket_price")
    quantities = form.getlist("ticket_qty")
    maximums = form.getlist("ticket_max")
    descriptions = form.getlist("ticket_desc")
    identifiers = form.getlist("ticket_id")
    count = len(names)
    if not (len(prices) == len(quantities) == len(maximums) == len(descriptions) == count):
        return None, ["Ticket categories were not submitted correctly."]
    if count > 10:
        return None, ["An event can have at most 10 ticket categories."]
    rows = []
    errors = []
    seen = set()
    for index in range(count):
        name = " ".join((names[index] or "").split())
        price = prices[index] if index < len(prices) else ""
        quantity = quantities[index] if index < len(quantities) else ""
        maximum = maximums[index] if index < len(maximums) else ""
        description = descriptions[index] if index < len(descriptions) else ""
        if _blank_ticket_row(name, price, quantity, maximum, description):
            continue
        if not 2 <= len(name) <= 40:
            errors.append(f"Ticket category {index + 1} needs a name between 2 and 40 characters.")
            continue
        if name.lower() in seen:
            errors.append(f"The ticket category '{name}' is listed more than once.")
        seen.add(name.lower())
        try:
            price_ngwee = parse_non_negative_money(price, f"{name} price")
            quantity_value = parse_positive_int(quantity, f"{name} quantity", 100000)
            maximum_value = parse_positive_int(maximum, f"{name} maximum per customer", 100)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if maximum_value > quantity_value:
            errors.append(f"{name}: the maximum per customer cannot exceed the total quantity.")
        description = (description or "").strip()
        if len(description) > 300:
            errors.append(f"{name} description must be 300 characters or fewer.")
        ticket_id = identifiers[index].strip() if index < len(identifiers) else ""
        rows.append(
            {
                "id": int(ticket_id) if ticket_id.isdigit() else None,
                "name": name,
                "price": price_ngwee,
                "quantity": quantity_value,
                "max_per_order": maximum_value,
                "description": description,
            }
        )
    if required and not rows and not errors:
        errors.append("Add at least one ticket category before submitting the event.")
    if errors:
        return None, errors
    return rows, []


def validate_event_form(form, submitting, has_poster, existing=None):
    errors = []
    title = " ".join((form.get("title") or "").split())
    if not 5 <= len(title) <= 150:
        errors.append("The event title must be between 5 and 150 characters.")

    category = None
    raw_category = (form.get("category_id") or "").strip()
    if raw_category.isdigit():
        category = EventCategory.query.filter_by(id=int(raw_category), is_active=True).first()
    if category is None:
        errors.append("Choose an event category.")

    description = (form.get("description") or "").strip()
    if len(description) > 5000:
        errors.append("The description must be 5,000 characters or fewer.")
    if submitting and len(description) < 30:
        errors.append("The description must be at least 30 characters before the event can be submitted.")

    venue, venue_error = validate_required_text(form.get("venue"), "Venue", 2, 200)
    location, location_error = validate_required_text(form.get("location"), "Town or city", 2, 120)
    contact, contact_error = validate_required_text(form.get("contact_info"), "Contact information", 5, 200)
    for error in (venue_error, location_error, contact_error):
        if error:
            errors.append(error)

    start = end = sales_start = sales_end = None
    try:
        start = parse_local_datetime(form.get("start_datetime"), "start date and time")
        end = parse_local_datetime(form.get("end_datetime"), "end date and time")
        sales_start = parse_local_datetime(form.get("sales_start"), "ticket sales opening")
        sales_end = parse_local_datetime(form.get("sales_end"), "ticket sales closing")
    except ValueError as exc:
        errors.append(str(exc))

    now = utcnow_naive()
    locked = False
    if existing is not None:
        from app.services.ticket_service import event_is_locked

        locked = event_is_locked(existing)
    if start and end and end <= start:
        errors.append("The end time must be later than the start time.")
    if start and not locked and start <= now:
        errors.append("The event start must be in the future.")
    if start and sales_start and sales_end:
        if not (sales_start < sales_end <= start):
            errors.append(
                "Ticket sales must open before they close, and sales must close at or before the event starts."
            )

    rows, row_errors = parse_ticket_rows(form, required=submitting and not locked)
    errors.extend(row_errors)
    if submitting and not has_poster:
        errors.append("Upload an event poster before submitting the event for approval.")

    if errors:
        return None, errors
    return {
        "title": title,
        "category": category,
        "description": description,
        "venue": venue,
        "location": location,
        "contact_info": contact,
        "start": start,
        "end": end,
        "sales_start": sales_start,
        "sales_end": sales_end,
        "rows": rows or [],
        "locked": locked,
    }, []


ARTIST_LIMIT = 20
ARTIST_KEY = re.compile(r"[a-z0-9]{1,16}")


def parse_artist_rows(form, files, event, photos_required):
    """Read the Artists & Performers cards. Each card's photo input is named artist_photo_<key>."""
    keys = form.getlist("artist_key")
    ids = form.getlist("artist_id")
    names = form.getlist("artist_name")
    categories = form.getlist("artist_category")
    descriptions = form.getlist("artist_description")
    if not (len(keys) == len(ids) == len(names) == len(categories) == len(descriptions)):
        return None, ["Artists were not submitted correctly. Reload the page and try again."]
    existing = {artist.id: artist for artist in event.artists} if event else {}
    rows = []
    errors = []
    seen_ids = set()
    seen_keys = set()
    for index, key in enumerate(keys):
        key = key.strip()
        if not ARTIST_KEY.fullmatch(key) or key in seen_keys:
            errors.append("Artists were not submitted correctly. Reload the page and try again.")
            continue
        seen_keys.add(key)
        artist = None
        raw_id = ids[index].strip()
        if raw_id:
            if not raw_id.isdigit() or int(raw_id) not in existing or int(raw_id) in seen_ids:
                errors.append("An artist on this form does not belong to this event.")
                continue
            artist = existing[int(raw_id)]
            seen_ids.add(artist.id)
        name = " ".join((names[index] or "").split())
        category = (categories[index] or "").strip()
        description = (descriptions[index] or "").strip()
        photo = files.get(f"artist_photo_{key}")
        has_upload = bool(photo and photo.filename)
        if artist is None and not name and not description and not has_upload:
            continue
        label = name or f"artist {index + 1}"
        if not 2 <= len(name) <= 120:
            errors.append(f"Enter the full name of artist {index + 1} (2 to 120 characters).")
        if category not in ARTIST_CATEGORIES:
            errors.append(f"Choose a performer category for {label}.")
        if len(description) > 500:
            errors.append(f"The performance description for {label} must be 500 characters or fewer.")
        if photos_required and not has_upload and not (artist and artist.artist_image):
            errors.append(f"Add a profile photo for {label}. Customers see artist photos on the event page.")
        rows.append(
            {
                "artist": artist,
                "name": name,
                "category": category,
                "description": description,
                "photo": photo if has_upload else None,
            }
        )
    if len(rows) > ARTIST_LIMIT:
        errors.append(f"An event can list at most {ARTIST_LIMIT} artists.")
    if errors:
        return None, errors
    return rows, []


def sync_artists(event, rows, saved_paths):
    """Apply the artist cards to the event.

    New photo paths are appended to saved_paths as they are written, so the caller
    can delete them if the transaction fails. Returns the photo paths that are no
    longer used and should be deleted after a successful commit.
    """
    before = [artist for artist in event.artists if artist.id is not None]
    kept = set()
    unused = []
    for row in rows:
        path = None
        if row["photo"] is not None:
            path = save_image(row["photo"], "event_artists")
            saved_paths.append(path)
        artist = row["artist"]
        if artist is None:
            artist = EventArtist(event=event, artist_name=row["name"])
            db.session.add(artist)
        else:
            kept.add(artist.id)
        artist.artist_name = row["name"]
        artist.artist_category = row["category"]
        artist.performance_description = row["description"]
        if path:
            if artist.artist_image:
                unused.append(artist.artist_image)
            artist.artist_image = path
    for artist in before:
        if artist.id not in kept:
            if artist.artist_image:
                unused.append(artist.artist_image)
            event.artists.remove(artist)
    return unused


def sync_ticket_types(event, rows):
    existing = {ticket.id: ticket for ticket in list(event.ticket_types)}
    kept = set()
    for row in rows:
        ticket = existing.get(row["id"]) if row["id"] else None
        if ticket is None:
            db.session.add(
                TicketType(
                    event=event,
                    name=row["name"],
                    description=row["description"],
                    price=row["price"],
                    quantity_total=row["quantity"],
                    quantity_sold=0,
                    quantity_reserved=0,
                    max_per_order=row["max_per_order"],
                )
            )
            continue
        committed = ticket.quantity_sold + ticket.quantity_reserved
        if committed and row["quantity"] < committed:
            raise ValueError(
                f"{ticket.name} already has {committed} sold or reserved tickets, so the quantity cannot be lower."
            )
        if committed and (row["price"] != ticket.price or row["name"] != ticket.name):
            raise ValueError("Ticket categories that already have sales cannot change name or price.")
        ticket.name = row["name"]
        ticket.description = row["description"]
        ticket.price = row["price"]
        ticket.quantity_total = row["quantity"]
        ticket.max_per_order = min(row["max_per_order"], row["quantity"])
        kept.add(ticket.id)
    for ticket_id, ticket in existing.items():
        if ticket_id in kept:
            continue
        if ticket.quantity_sold or ticket.quantity_reserved:
            raise ValueError("A ticket category with sales or reservations cannot be removed.")
        db.session.delete(ticket)
