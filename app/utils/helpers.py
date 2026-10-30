"""Shared helpers for money, uploads, audit records, and safe links."""

import os
import secrets
import uuid
from datetime import datetime, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

from flask import current_app, request
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import AuditLog

LUSAKA = ZoneInfo("Africa/Lusaka")
ALLOWED_FOLDERS = {"event_posters", "event_artists", "planner_profiles", "portfolios"}
IMAGE_EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}


def format_zmw(ngwee):
    value = int(ngwee or 0)
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}K{value // 100:,}.{value % 100:02d}"


PAYMENT_PROVIDERS = {"mtn": "MTN MoMo", "airtel": "Airtel Money", "zamtel": "Zamtel Kwacha", "card": "Card"}


def payment_method_label(value, show_mode=True):
    if not value or value == "unassigned":
        return "Not selected"
    mode, _, provider = value.partition(":")
    name = PAYMENT_PROVIDERS.get(provider, provider.replace("_", " ").title() or value)
    return f"{name} (simulation)" if show_mode and mode == "simulation" else name


def format_duration(minutes):
    minutes = int(minutes or 0)
    hours, mins = divmod(minutes, 60)
    if mins:
        return f"{hours}h {mins}m"
    return f"{hours}h"


def to_local(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LUSAKA)


def format_local(dt, pattern="%d %b %Y, %H:%M"):
    local = to_local(dt)
    if local is None:
        return ""
    return local.strftime(pattern)


def dashboard_endpoint(user):
    if user.role == "admin":
        return "admin.dashboard"
    if user.role == "planner":
        return "planner.dashboard" if user.is_approved_planner else "planner.pending"
    return "client.dashboard"


def client_ip():
    address = request.remote_addr or ""
    return address[:64]


def safe_next(target):
    if not target or not isinstance(target, str):
        return None
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    return target


def safe_link(link):
    return safe_next(link)


def safe_upload_path(path):
    if not path or not isinstance(path, str):
        return None
    normalised = path.replace("\\", "/")
    if normalised.startswith("/") or ".." in normalised.split("/"):
        return None
    parts = normalised.split("/")
    if len(parts) != 3 or parts[0] != "uploads" or parts[1] not in ALLOWED_FOLDERS:
        return None
    filename = secure_filename(parts[2])
    if filename != parts[2]:
        return None
    return normalised


def save_image(file_storage, subfolder):
    if subfolder not in ALLOWED_FOLDERS:
        raise ValueError("Invalid upload folder.")
    if file_storage is None or not file_storage.filename:
        raise ValueError("Choose an image to upload.")
    data = file_storage.read()
    file_storage.seek(0)
    if not data:
        raise ValueError("The uploaded file is empty.")
    if len(data) > current_app.config["UPLOAD_MAX_BYTES"]:
        raise ValueError("Images must be 2 MB or smaller.")
    try:
        probed = Image.open(BytesIO(data))
        probed.verify()
        image = Image.open(BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("Upload a valid JPEG, PNG, or WebP image.") from exc
    if image.format not in IMAGE_EXTENSIONS:
        raise ValueError("Upload a valid JPEG, PNG, or WebP image.")
    filename = f"{uuid.uuid4().hex}.{IMAGE_EXTENSIONS[image.format]}"
    relative = f"uploads/{subfolder}/{filename}"
    absolute = os.path.join(current_app.static_folder, "uploads", subfolder, filename)
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    with open(absolute, "wb") as handle:
        handle.write(data)
    return relative


def delete_upload(relative):
    safe = safe_upload_path(relative)
    if not safe:
        return
    absolute = os.path.join(current_app.static_folder, *safe.split("/"))
    root = os.path.realpath(os.path.join(current_app.static_folder, "uploads"))
    target = os.path.realpath(absolute)
    if target != root and not target.startswith(root + os.sep):
        return
    if os.path.isfile(target):
        os.remove(target)


DOCUMENT_TYPES = {"pdf": "application/pdf", "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def _private_root():
    return os.path.realpath(current_app.config["PRIVATE_UPLOAD_FOLDER"])


def save_document(file_storage):
    """Store a PACRA certificate outside the static folder so only admins can open it."""
    if file_storage is None or not file_storage.filename:
        raise ValueError("Choose a document to upload.")
    data = file_storage.read()
    file_storage.seek(0)
    if not data:
        raise ValueError("The PACRA document is empty.")
    if len(data) > current_app.config["DOCUMENT_MAX_BYTES"]:
        raise ValueError("The PACRA document must be 5 MB or smaller.")
    if data.startswith(b"%PDF-"):
        extension = "pdf"
    else:
        try:
            probed = Image.open(BytesIO(data))
            probed.verify()
            image_format = Image.open(BytesIO(data)).format
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ValueError("Upload the PACRA document as a PDF, JPEG, PNG, or WebP file.") from exc
        if image_format not in IMAGE_EXTENSIONS:
            raise ValueError("Upload the PACRA document as a PDF, JPEG, PNG, or WebP file.")
        extension = IMAGE_EXTENSIONS[image_format]
    filename = f"{uuid.uuid4().hex}.{extension}"
    folder = os.path.join(_private_root(), "pacra")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, filename), "wb") as handle:
        handle.write(data)
    return f"pacra/{filename}"


def private_document_path(relative):
    if not relative or not isinstance(relative, str):
        return None
    parts = relative.replace("\\", "/").split("/")
    if len(parts) != 2 or parts[0] != "pacra" or secure_filename(parts[1]) != parts[1]:
        return None
    extension = parts[1].rsplit(".", 1)[-1].lower()
    if extension not in DOCUMENT_TYPES:
        return None
    root = _private_root()
    target = os.path.realpath(os.path.join(root, *parts))
    if not target.startswith(root + os.sep) or not os.path.isfile(target):
        return None
    return target


def document_mimetype(relative):
    return DOCUMENT_TYPES.get((relative or "").rsplit(".", 1)[-1].lower(), "application/octet-stream")


def delete_document(relative):
    target = private_document_path(relative)
    if target:
        os.remove(target)


def audit(actor_id, action, entity_type, entity_id=None, reason=None):
    db.session.add(
        AuditLog(
            actor_id=actor_id,
            action=action[:80],
            entity_type=entity_type[:40],
            entity_id=entity_id,
            reason=(reason or None) and str(reason)[:500],
        )
    )


def generate_reference(prefix):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{prefix}-{stamp}-{secrets.token_hex(4).upper()}"


def generate_ticket_code():
    return f"ELZ-{secrets.token_hex(4).upper()}-{secrets.token_hex(2).upper()}"


def page_arg(default=1):
    try:
        return max(1, int(request.args.get("page", default)))
    except (TypeError, ValueError):
        return 1


def paginate(query, page, per_page):
    page = max(int(page or 1), 1)
    total = query.count()
    pages = max((total + per_page - 1) // per_page, 1)
    if page > pages:
        page = pages
    items = query.limit(per_page).offset((page - 1) * per_page).all()
    return {
        "items": items,
        "page": page,
        "pages": pages,
        "total": total,
        "has_prev": page > 1,
        "has_next": page < pages,
        "per_page": per_page,
    }


def escape_like(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
