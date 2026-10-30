"""Helpers for creating accounts and events in tests."""

from datetime import datetime, timedelta

from app.extensions import db
from app.models import Event, EventCategory, PlannerProfile, PlannerService, TicketType, User


def make_user(app, role="client", email="client@example.com", phone="+260971111111", name="Client One", password="Secret123"):
    with app.app_context():
        user = User(full_name=name, email=email, phone=phone, role=role, account_status="active")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user.id


def make_planner(app, email="planner@example.com", phone="+260971111112", approved=False):
    user_id = make_user(app, role="planner", email=email, phone=phone, name="Amina Banda")
    with app.app_context():
        profile = PlannerProfile(
            user_id=user_id,
            business_name="Amina Events",
            biography="We plan celebrations across Lusaka and the Copperbelt.",
            location="Lusaka",
            experience="Ten years of weddings, graduations, and corporate events.",
            specialities="Weddings, graduations",
            approval_status="approved" if approved else "pending",
            terms_accepted=True,
        )
        db.session.add(profile)
        db.session.flush()
        db.session.add(
            PlannerService(
                planner_id=profile.id,
                service_name="Full planning",
                description="Planning from the first brief to the event day.",
                pricing_type="quotation",
            )
        )
        db.session.commit()
        return user_id, profile.id


def make_event(app, organiser_id, quantity=5, price=10000, title="Copperbelt Night"):
    with app.app_context():
        category = EventCategory.query.first()
        start = datetime(2027, 6, 1, 16, 0)
        event = Event(
            title=title,
            category_id=category.id,
            description="A live music night with local artists and a full production crew.",
            venue="Heroes Stadium",
            location="Lusaka",
            start_datetime=start,
            end_datetime=start + timedelta(hours=4),
            sales_start=datetime(2026, 1, 1, 8, 0),
            sales_end=start,
            organiser_id=organiser_id,
            contact_info="+260971111112",
            approval_status="approved",
            publication_status="published",
        )
        db.session.add(event)
        db.session.flush()
        ticket = TicketType(
            event_id=event.id,
            name="Ordinary",
            description="Standard admission",
            price=price,
            quantity_total=quantity,
            quantity_sold=0,
            quantity_reserved=0,
            max_per_order=10,
        )
        db.session.add(ticket)
        db.session.commit()
        return event.id, ticket.id


def login(client, email, password="Secret123"):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)
