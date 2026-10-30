"""Planner and admin reports: data separation, commission, filters, exports and payouts."""

import csv
import io
from datetime import date, time, timedelta

import pytest
from openpyxl import load_workbook

from app.extensions import db
from app.models import (
    AuditLog,
    Event,
    Notification,
    Payment,
    PlannerBooking,
    PlannerPayout,
    PlannerProfile,
    Refund,
    Ticket,
    TicketType,
    User,
)
from app.models.database import utcnow
from app.services.payment_service import dispatch_simulation, start_checkout
from app.services.report_service import planner_report, platform_report, resolve_period, split_amount
from app.utils.helpers import to_local
from tests.factories import login, make_event, make_planner, make_user


def _planner(app, email, phone, business):
    user_id, profile_id = make_planner(app, email=email, phone=phone, approved=True)
    with app.app_context():
        db.session.get(PlannerProfile, profile_id).business_name = business
        db.session.commit()
    return user_id, profile_id


def _buy(app, client_id, event_id, ticket_id, quantity, outcome="successful"):
    with app.app_context():
        order, error = start_checkout(
            db.session.get(User, client_id), db.session.get(Event, event_id), db.session.get(TicketType, ticket_id), quantity
        )
        assert error is None
        if outcome != "pending":
            assert dispatch_simulation(order.payments[0], outcome, "mtn")["status"] == outcome
        return order.order_reference


def _booking(app, client_id, profile_id, status):
    with app.app_context():
        db.session.add(
            PlannerBooking(
                client_id=client_id,
                planner_id=profile_id,
                event_type="Wedding reception",
                event_date=date(2027, 3, 20),
                start_time=time(14, 0),
                duration=5,
                venue="Taj Pamodzi",
                guest_count=120,
                budget=5000000,
                requirements="Decor, catering, and music.",
                contact_method="phone",
                status=status,
            )
        )
        db.session.commit()


@pytest.fixture()
def world(app):
    """Two planners with their own events and sales, one client and one administrator."""
    a_user, a_profile = _planner(app, "amina@example.com", "+260971111121", "Amina Events")
    b_user, b_profile = _planner(app, "bwalya@example.com", "+260971111122", "Bwalya Celebrations")
    a_event, a_ticket = make_event(app, a_user, quantity=10, price=20000, title="Copperbelt Night")
    b_event, b_ticket = make_event(app, b_user, quantity=10, price=15000, title="Lusaka Jazz Evening")
    client_id = make_user(app)
    make_user(app, role="admin", email="admin@example.com", phone="+260971111199", name="Admin User")

    paid_order = _buy(app, client_id, a_event, a_ticket, 2)
    _buy(app, client_id, a_event, a_ticket, 1, "failed")
    _buy(app, client_id, a_event, a_ticket, 1, "pending")
    _buy(app, client_id, b_event, b_ticket, 3)

    _booking(app, client_id, a_profile, "accepted")
    _booking(app, client_id, a_profile, "rejected")
    _booking(app, client_id, b_profile, "pending")
    return {
        "a_user": a_user, "a_profile": a_profile, "a_event": a_event, "a_order": paid_order,
        "b_user": b_user, "b_profile": b_profile, "b_event": b_event,
        "client": client_id,
    }


def _all_time():
    period, error = resolve_period({})
    assert error is None
    return period


def test_commission_is_split_per_payment_and_rounded_half_up(app):
    with app.app_context():
        assert split_amount(20000) == (3000, 17000)
        assert split_amount(10) == (2, 8)
        assert split_amount(1) == (0, 1)


def test_planner_report_counts_only_verified_payments(app, world):
    with app.app_context():
        report = planner_report(db.session.get(PlannerProfile, world["a_profile"]), _all_time())
    finance = report["finance"]
    assert finance["gross"] == 40000
    assert finance["commission"] == 6000
    assert finance["earnings"] == 34000
    assert finance["pending_count"] == 1 and finance["pending_amount"] == 20000
    assert finance["failed_count"] == 1 and finance["failed_amount"] == 20000
    assert report["overview"]["tickets_sold"] == 2
    assert report["tickets"]["successful_transactions"] == 1
    assert [row["event"].title for row in report["events"]] == ["Copperbelt Night"]
    assert report["bookings"]["total"] == 2
    assert report["bookings"]["acceptance_rate"] == 50
    assert all(row["kind"] == "ticket_sale" and row["gross"] == 40000 for row in report["transactions"])


def test_each_planner_sees_only_their_own_report(client, app, world):
    login(client, "amina@example.com")
    page = client.get("/planner/reports")
    assert page.status_code == 200
    assert b"Copperbelt Night" in page.data
    assert b"K400.00" in page.data and b"K60.00" in page.data and b"K340.00" in page.data
    assert b"Lusaka Jazz Evening" not in page.data
    assert b"K450.00" not in page.data

    tampered = client.get(f"/planner/reports?planner_id={world['b_profile']}&organiser_id={world['b_user']}")
    assert b"Lusaka Jazz Evening" not in tampered.data
    assert b"Copperbelt Night" in tampered.data

    client.post("/auth/logout")
    login(client, "bwalya@example.com")
    page = client.get("/planner/reports")
    assert b"Lusaka Jazz Evening" in page.data
    assert b"K450.00" in page.data
    assert b"Copperbelt Night" not in page.data


def test_planners_cannot_reach_other_reports(client, app, world):
    login(client, "amina@example.com")
    assert client.get(f"/planner/reports?event={world['b_event']}").status_code == 404
    assert client.get(f"/planner/reports/export.csv?event={world['b_event']}").status_code == 404
    assert client.get("/planner/reports?event=abc").status_code == 404
    assert client.get("/admin/reports").status_code == 403
    assert client.get("/admin/reports/export.csv").status_code == 403
    assert client.get(f"/admin/reports/planners/{world['b_profile']}").status_code == 403
    assert client.get(f"/admin/reports/planners/{world['b_profile']}/export.pdf").status_code == 403
    assert client.post(
        f"/admin/reports/planners/{world['a_profile']}/payouts",
        data={"amount": "10.00", "method": "bank_transfer", "external_reference": "SELF-PAY", "confirm": "yes"},
    ).status_code == 403
    client.post("/auth/logout")
    login(client, "client@example.com")
    assert client.get("/planner/reports").status_code == 403


def test_event_filter_shows_one_event(client, app, world):
    other_event, other_ticket = make_event(app, world["a_user"], quantity=5, price=5000, title="Kafue River Fest")
    _buy(app, world["client"], other_event, other_ticket, 1)
    login(client, "amina@example.com")
    everything = client.get("/planner/reports")
    assert b"Kafue River Fest" in everything.data and b"K450.00" in everything.data
    single = client.get(f"/planner/reports?event={other_event}")
    assert single.status_code == 200
    assert b"Showing <strong>Kafue River Fest</strong> only" in single.data
    assert b"K50.00" in single.data
    assert b"Booking history" not in single.data


def test_period_filters_change_the_totals(client, app, world):
    with app.app_context():
        payment = Payment.query.join(Payment.order).filter_by(order_reference=world["a_order"]).one()
        old = utcnow() - timedelta(days=40)
        payment.verified_at = old
        for ticket in Ticket.query.filter_by(event_id=world["a_event"]).all():
            ticket.issued_at = old
        db.session.commit()
        profile = db.session.get(PlannerProfile, world["a_profile"])
        recent = planner_report(profile, resolve_period({"period": "30d"})[0])
        wider = planner_report(profile, resolve_period({"period": "3m"})[0])
        day = to_local(old).date()
        custom = planner_report(profile, resolve_period({"period": "custom", "date_from": day.isoformat(), "date_to": day.isoformat()})[0])
        before = (day - timedelta(days=1)).isoformat()
        missed = planner_report(profile, resolve_period({"period": "custom", "date_from": before, "date_to": before})[0])
    assert recent["finance"]["gross"] == 0 and recent["overview"]["tickets_sold"] == 0
    assert wider["finance"]["gross"] == 40000 and wider["overview"]["tickets_sold"] == 2
    assert custom["finance"]["gross"] == 40000
    assert missed["finance"]["gross"] == 0

    login(client, "amina@example.com")
    page = client.get("/planner/reports?period=custom&date_from=2026-05-10&date_to=2026-05-01")
    assert page.status_code == 200
    assert b"The start date must be on or before the end date." in page.data
    assert b"Last 30 days" in client.get("/planner/reports?period=30d").data


def test_completed_refund_is_taken_off_earnings(client, app, world):
    with app.app_context():
        db.session.get(Event, world["a_event"]).publication_status = "cancelled"
        db.session.commit()
    login(client, "client@example.com")
    client.post(f"/client/orders/{world['a_order']}/refund", data={"reason": "The show was cancelled."})
    client.post("/auth/logout")
    login(client, "admin@example.com")
    with app.app_context():
        refund_id = Refund.query.one().id
    client.post(
        f"/admin/refunds/{refund_id}",
        data={"decision": "completed", "note": "Returned to the customer's wallet.", "confirm_external": "yes"},
    )
    with app.app_context():
        assert db.session.get(Refund, refund_id).status == "completed"
        report = planner_report(db.session.get(PlannerProfile, world["a_profile"]), _all_time())
        platform = platform_report(_all_time())
    finance = report["finance"]
    assert finance["gross"] == 40000
    assert finance["refunds"] == 40000
    assert finance["net_revenue"] == 0
    assert finance["commission"] == 0
    assert finance["earnings"] == 0
    assert report["tickets"]["refunded"] == 2
    assert {row["kind"] for row in report["transactions"]} == {"ticket_sale", "refund"}
    assert platform["overview"]["refunded_transactions"] == 1
    assert platform["finance"]["net_revenue"] == 45000


def test_admin_sees_the_whole_platform_and_each_planner(client, app, world):
    login(client, "admin@example.com")
    page = client.get("/admin/reports")
    assert page.status_code == 200
    assert b"Amina Events" in page.data and b"Bwalya Celebrations" in page.data
    assert b"K850.00" in page.data
    assert b"K127.50" in page.data
    with app.app_context():
        report = platform_report(_all_time())
    assert report["finance"]["total_gross"] == 85000
    assert report["finance"]["total_commission"] == 6000 + 6750
    assert report["overview"]["tickets_sold"] == 5
    assert report["finance"]["pending_count"] == 1
    assert report["finance"]["failed_count"] == 1
    assert [row["profile"].business_name for row in report["comparison"]] == ["Bwalya Celebrations", "Amina Events"]

    drill = client.get(f"/admin/reports/planners/{world['a_profile']}")
    assert drill.status_code == 200
    assert b"Copperbelt Night" in drill.data and b"Lusaka Jazz Evening" not in drill.data
    assert b"Record a payout" in drill.data
    assert client.get(f"/admin/reports/planners/{world['a_profile']}?event={world['b_event']}").status_code == 404
    assert client.get("/admin/reports/planners/9999").status_code == 404

    compared = client.get(f"/admin/reports?compare={world['a_profile']}&compare={world['b_profile']}")
    assert b"Side-by-side comparison" in compared.data
    assert b"admin-compare-chart" in compared.data
    single = client.get(f"/admin/reports?compare={world['a_profile']}")
    assert b"Choose at least two planners to compare." in single.data


def test_planner_exports_contain_only_their_figures(client, app, world):
    login(client, "amina@example.com")
    response = client.get("/planner/reports/export.csv")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "eventlink-amina-events-all-" in response.headers["Content-Disposition"]
    rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
    flat = [cell for row in rows for cell in row]
    assert "Amina Events" in flat and "Copperbelt Night" in flat
    assert "Lusaka Jazz Evening" not in flat and "Bwalya Celebrations" not in flat
    assert ["Total gross revenue", "400.00"] in rows
    assert ["Platform commission (15%)", "60.00"] in rows
    assert ["Planner earnings (85%)", "340.00"] in rows

    workbook = load_workbook(io.BytesIO(client.get("/planner/reports/export.xlsx").data))
    values = [cell for sheet in workbook.worksheets for row in sheet.iter_rows(values_only=True) for cell in row if cell is not None]
    assert "Copperbelt Night" in values and 400.0 in values and 340.0 in values
    assert "Lusaka Jazz Evening" not in values

    pdf = client.get("/planner/reports/export.pdf")
    assert pdf.status_code == 200
    assert pdf.mimetype == "application/pdf"
    assert pdf.data.startswith(b"%PDF")

    assert client.get("/planner/reports/export.doc").status_code == 404


def test_admin_exports_cover_all_planners(client, app, world):
    login(client, "admin@example.com")
    response = client.get("/admin/reports/export.csv?period=year")
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
    flat = [cell for row in rows for cell in row]
    assert "Amina Events" in flat and "Bwalya Celebrations" in flat
    assert ["Total gross revenue", "850.00"] in rows
    assert client.get("/admin/reports/export.xlsx").status_code == 200
    assert client.get("/admin/reports/export.pdf").data.startswith(b"%PDF")
    planner_csv = client.get(f"/admin/reports/planners/{world['b_profile']}/export.csv").data.decode("utf-8-sig")
    assert "Lusaka Jazz Evening" in planner_csv and "Copperbelt Night" not in planner_csv


def test_exports_neutralise_formula_text(client, app, world):
    with app.app_context():
        db.session.get(Event, world["a_event"]).title = "=HYPERLINK(\"http://example.com\")"
        db.session.commit()
    login(client, "amina@example.com")
    text = client.get("/planner/reports/export.csv").data.decode("utf-8-sig")
    assert "'=HYPERLINK" in text
    workbook = load_workbook(io.BytesIO(client.get("/planner/reports/export.xlsx").data))
    values = [cell for sheet in workbook.worksheets for row in sheet.iter_rows(values_only=True) for cell in row if isinstance(cell, str)]
    assert not any(value.startswith("=") for value in values)


def test_admin_records_payouts_against_the_balance(client, app, world):
    login(client, "admin@example.com")
    url = f"/admin/reports/planners/{world['a_profile']}/payouts"
    good = {"amount": "100.00", "method": "bank_transfer", "external_reference": "ZANACO-778812", "confirm": "yes"}

    client.post(url, data={**good, "confirm": ""})
    client.post(url, data={**good, "amount": "340.01"})
    client.post(url, data={**good, "method": "cash"})
    with app.app_context():
        assert PlannerPayout.query.count() == 0

    response = client.post(url, data=good, follow_redirects=True)
    assert b"Payout of K100.00 recorded for Amina Events." in response.data
    duplicate = client.post(url, data=good, follow_redirects=True)
    assert b"already been recorded" in duplicate.data
    with app.app_context():
        payout = PlannerPayout.query.one()
        assert payout.amount == 10000 and payout.planner_id == world["a_profile"]
        assert AuditLog.query.filter_by(action="payout_recorded", entity_id=payout.id).count() == 1
        assert Notification.query.filter_by(user_id=world["a_user"], title="Payout recorded").count() == 1
        report = planner_report(db.session.get(PlannerProfile, world["a_profile"]), _all_time())
        other = planner_report(db.session.get(PlannerProfile, world["b_profile"]), _all_time())
    assert report["finance"]["balance"] == 24000
    assert report["finance"]["paid_all_time"] == 10000
    assert [row["kind"] for row in report["transactions"]].count("payout") == 1
    assert other["finance"]["paid_all_time"] == 0 and other["payouts"] == []

    client.post("/auth/logout")
    login(client, "amina@example.com")
    page = client.get("/planner/reports")
    assert b"ZANACO-778812" in page.data
    assert b"K240.00" in page.data
