"""In-app notifications stored in SQLite."""

from app.extensions import db
from app.models import Notification
from app.utils.helpers import safe_link


def notify_admins(title, message, link=None):
    from app.models import User

    admins = User.query.filter_by(role="admin", account_status="active").all()
    for admin in admins:
        notify(admin.id, title, message, link)


def notify(user_id, title, message, link=None):
    db.session.add(
        Notification(
            user_id=user_id,
            title=title[:120],
            message=message[:2000],
            link=safe_link(link),
            is_read=False,
        )
    )


def unread_count(user_id):
    return Notification.query.filter_by(user_id=user_id, is_read=False).count()


def list_for_user(user):
    return (
        Notification.query.filter_by(user_id=user.id)
        .order_by(Notification.created_at.desc())
        .limit(100)
        .all()
    )


def mark_read(user, notification_id):
    item = Notification.query.filter_by(id=notification_id, user_id=user.id).first()
    if item is None:
        return False
    item.is_read = True
    db.session.commit()
    return True


def mark_all_read(user):
    Notification.query.filter_by(user_id=user.id, is_read=False).update({"is_read": True})
    db.session.commit()
