"""Artists & Performers on events: planner form, admin review and the public page."""

import os
from io import BytesIO

from PIL import Image

from app.extensions import db
from app.models import Event, EventArtist
from tests.factories import login, make_event, make_planner, make_user


def _png(colour=(14, 107, 79)):
    buffer = BytesIO()
    Image.new("RGB", (24, 24), colour).save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def _event_fields(title="Lusaka Live Showcase", action="submit"):
    return {
        "action": action,
        "title": title,
        "category_id": "1",
        "description": "A night of Zambian music with a full stage, sound crew and food stalls on site.",
        "venue": "Lusaka Playhouse",
        "location": "Lusaka",
        "start_datetime": "2027-07-10T18:00",
        "end_datetime": "2027-07-10T23:00",
        "sales_start": "2027-06-01T09:00",
        "sales_end": "2027-07-10T17:00",
        "contact_info": "+260971111112",
        "ticket_name": "Ordinary",
        "ticket_price": "150.00",
        "ticket_qty": "100",
        "ticket_max": "6",
        "ticket_desc": "Standard entry",
    }


def _artist_fields(cards):
    """cards: list of (key, id, name, category, description, photo_or_None)."""
    data = {
        "artist_key": [card[0] for card in cards],
        "artist_id": [card[1] for card in cards],
        "artist_name": [card[2] for card in cards],
        "artist_category": [card[3] for card in cards],
        "artist_description": [card[4] for card in cards],
    }
    for card in cards:
        if card[5] is not None:
            data[f"artist_photo_{card[0]}"] = (card[5], f"{card[0]}.png")
    return data


def _disk(app, relative):
    return os.path.join(app.static_folder, *relative.split("/"))


def _create_with_artists(client, title="Lusaka Live Showcase"):
    data = _event_fields(title)
    data["poster"] = (_png(), "poster.png")
    data.update(
        _artist_fields(
            [
                ("n1", "", "Mampi", "Musician/Singer", "Headline set with a live band.", _png((200, 40, 40))),
                ("n2", "", "DJ Mzenga Man", "DJ", "Opening and closing sets.", _png((40, 40, 200))),
                ("n3", "", "Kalindula Sounds", "Band", "Traditional kalindula performance.", _png((200, 160, 20))),
            ]
        )
    )
    return client.post("/planner/events/create", data=data, follow_redirects=True)


def test_artists_flow_from_planner_to_public_page(client, app):
    make_planner(app, approved=True)
    make_user(app, role="admin", email="admin@example.com", phone="+260971111199", name="Admin User")
    login(client, "planner@example.com")
    form = client.get("/planner/events/create")
    assert b"Artists &amp; Performers" in form.data
    assert form.data.index(b'id="category_id"') < form.data.index(b"artists-section-heading") < form.data.index(b'id="description"')

    response = _create_with_artists(client)
    assert response.status_code == 200
    with app.app_context():
        event = Event.query.filter_by(title="Lusaka Live Showcase").one()
        assert event.approval_status == "pending"
        artists = event.artists
        assert [artist.artist_name for artist in artists] == ["Mampi", "DJ Mzenga Man", "Kalindula Sounds"]
        assert [artist.artist_category for artist in artists] == ["Musician/Singer", "DJ", "Band"]
        images = [artist.artist_image for artist in artists]
        for path in images:
            assert path.startswith("uploads/event_artists/")
            assert os.path.isfile(_disk(app, path))
        event_id = event.id

    my_events = client.get("/planner/events")
    assert b"3 artists" in my_events.data
    client.post("/auth/logout")
    assert client.get(f"/events/{event_id}").status_code == 404

    login(client, "admin@example.com")
    review = client.get(f"/admin/events/{event_id}")
    assert b"Kalindula Sounds" in review.data
    for path in images:
        assert path.encode() in review.data
    client.post(f"/admin/events/{event_id}/approve", follow_redirects=True)
    client.post("/auth/logout")

    public = client.get(f"/events/{event_id}")
    assert public.status_code == 200
    assert b"Artists Performing at This Event" in public.data
    for name in (b"Mampi", b"DJ Mzenga Man", b"Kalindula Sounds", b"Headline set with a live band."):
        assert name in public.data
    for path in images:
        assert path.encode() in public.data
        assert client.get(f"/static/{path}").status_code == 200


def test_event_without_artists_hides_section(client, app):
    planner_id, _ = make_planner(app, approved=True)
    event_id, _ = make_event(app, planner_id)
    public = client.get(f"/events/{event_id}")
    assert public.status_code == 200
    assert b"Artists Performing at This Event" not in public.data


def test_planner_edits_artists_and_old_photos_are_removed(client, app):
    make_planner(app, approved=True)
    login(client, "planner@example.com")
    _create_with_artists(client)
    with app.app_context():
        event = Event.query.filter_by(title="Lusaka Live Showcase").one()
        first, second, third = event.artists
        event_id = event.id
        ids = (first.id, second.id, third.id)
        old_first, old_third = first.artist_image, third.artist_image
        kept_second = second.artist_image

    data = _event_fields(action="submit")
    data.update(
        _artist_fields(
            [
                (f"a{ids[0]}", str(ids[0]), "Mampi Live", "Musician/Singer", "Full band set.", _png((10, 10, 10))),
                (f"a{ids[1]}", str(ids[1]), "DJ Mzenga Man", "DJ", "Closing set only.", None),
                ("nx9", "", "Chanda Comedy", "Comedian", "", _png((90, 20, 120))),
            ]
        )
    )
    response = client.post(f"/planner/events/{event_id}/edit", data=data, follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        event = db.session.get(Event, event_id)
        names = [artist.artist_name for artist in event.artists]
        assert names == ["Mampi Live", "DJ Mzenga Man", "Chanda Comedy"]
        assert db.session.get(EventArtist, ids[2]) is None
        assert event.artists[0].artist_image != old_first
        assert event.artists[1].artist_image == kept_second
        assert event.artists[1].performance_description == "Closing set only."
        new_paths = [artist.artist_image for artist in event.artists]
    assert not os.path.exists(_disk(app, old_first))
    assert not os.path.exists(_disk(app, old_third))
    for path in new_paths:
        assert os.path.isfile(_disk(app, path))


def test_submit_requires_artist_photo_and_valid_category(client, app):
    make_planner(app, approved=True)
    login(client, "planner@example.com")
    data = _event_fields("Photo Check Night")
    data["poster"] = (_png(), "poster.png")
    data.update(_artist_fields([("n1", "", "Mampi", "Rapper", "", None)]))
    response = client.post("/planner/events/create", data=data)
    assert b"Add a profile photo for Mampi" in response.data
    assert b"Choose a performer category for Mampi" in response.data
    assert b'value="Mampi"' in response.data
    with app.app_context():
        assert Event.query.filter_by(title="Photo Check Night").count() == 0


def test_text_file_rejected_as_artist_photo(client, app):
    make_planner(app, approved=True)
    login(client, "planner@example.com")
    folder = os.path.join(app.static_folder, "uploads", "event_artists")
    before = set(os.listdir(folder))
    data = _event_fields("Fake Photo Night")
    data["poster"] = (_png(), "poster.png")
    data.update(_artist_fields([("n1", "", "Mampi", "Musician/Singer", "", BytesIO(b"not an image"))]))
    response = client.post("/planner/events/create", data=data)
    assert b"image" in response.data.lower()
    with app.app_context():
        assert Event.query.filter_by(title="Fake Photo Night").count() == 0
        assert EventArtist.query.count() == 0
    assert set(os.listdir(folder)) == before


def test_artist_from_another_event_cannot_be_edited(client, app):
    make_planner(app, approved=True)
    other_id, _ = make_planner(app, email="other@example.com", phone="+260971111113", approved=True)
    event_id, _ = make_event(app, other_id, title="Other Planner Night")
    with app.app_context():
        foreign = EventArtist(event_id=event_id, artist_name="Foreign Act", artist_category="DJ", performance_description="")
        db.session.add(foreign)
        db.session.commit()
        foreign_id = foreign.id
    login(client, "planner@example.com")
    data = _event_fields("Hijack Attempt Night", action="draft")
    data.update(_artist_fields([(f"a{foreign_id}", str(foreign_id), "Renamed", "DJ", "", None)]))
    response = client.post("/planner/events/create", data=data)
    assert b"does not belong to this event" in response.data
    with app.app_context():
        assert db.session.get(EventArtist, foreign_id).artist_name == "Foreign Act"


def test_deleting_event_removes_artist_photos(client, app):
    make_planner(app, approved=True)
    login(client, "planner@example.com")
    _create_with_artists(client)
    with app.app_context():
        event = Event.query.filter_by(title="Lusaka Live Showcase").one()
        event_id = event.id
        paths = [artist.artist_image for artist in event.artists]
    client.post(f"/planner/events/{event_id}/delete", follow_redirects=True)
    with app.app_context():
        assert db.session.get(Event, event_id) is None
        assert EventArtist.query.filter_by(event_id=event_id).count() == 0
    for path in paths:
        assert not os.path.exists(_disk(app, path))
