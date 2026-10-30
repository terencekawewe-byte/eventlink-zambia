"""CSV, Excel and PDF downloads of planner and platform reports.

Each report is first turned into one document (details plus titled tables) so the
three formats always carry the same figures.
"""

import csv
import io
import re
from dataclasses import dataclass

from flask import Response
from fpdf import FPDF
from fpdf.fonts import FontFace
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.models.database import utcnow
from app.services.report_service import BOOKING_STATUSES, TRANSACTION_TYPES
from app.utils.helpers import format_local, format_zmw

EXPORT_FORMATS = ("csv", "xlsx", "pdf")
TRANSACTION_LABELS = {"ticket_sale": "Ticket sale", "refund": "Refund", "payout": "Payout"}
MAX_EXPORT_TRANSACTIONS = 5000
GREEN = "0E6B4F"


@dataclass(frozen=True)
class Money:
    ngwee: int


def _money(value):
    return "" if value is None else Money(int(value))


def _label(value):
    return (value or "").replace("_", " ").capitalize()


def _percent(value):
    return "n/a" if value is None else f"{value}%"


def _date(value):
    return value.strftime("%d %b %Y") if value else ""


def _details(title, rows):
    return {"title": title, "columns": ["Item", "Value"], "rows": [[label, value] for label, value in rows]}


def _finance_section(finance, rate, planner=True):
    earnings_label = f"Planner earnings ({100 - rate}%)"
    rows = [
        ["Ticket sales (verified payments)", _money(finance["gross"])],
        ["Planner booking revenue (verified payments)", _money(finance["booking_revenue"])],
        ["Total gross revenue", _money(finance["total_gross"])],
        ["Refunded amounts", _money(finance["refunds"])],
        ["Net revenue after refunds", _money(finance["net_revenue"] + finance["booking_revenue"])],
        [f"Platform commission ({rate}%)", _money(finance["total_commission"])],
        [earnings_label, _money(finance["total_earnings"])],
        ["Payouts completed in this period", _money(finance["payouts_period"])],
        ["Pending payout balance (all time, as of now)", _money(finance["balance"])],
        [f"Pending payments not counted ({finance['pending_count']})", _money(finance["pending_amount"])],
        [f"Failed payments not counted ({finance['failed_count']})", _money(finance["failed_amount"])],
    ]
    if planner:
        rows.insert(9, ["Planner earnings to date (all time)", _money(finance["earned_all_time"])])
        rows.insert(10, ["Payouts received to date (all time)", _money(finance["paid_all_time"])])
    return {"title": "Financial summary", "columns": ["Measure", "Amount (ZMW)"], "rows": rows}


def _event_section(rows):
    return {
        "title": "Event performance",
        "columns": ["Event", "Category", "Event date", "Status", "Tickets available", "Sold in period", "Remaining",
                    "Gross revenue", "Refunds", "Net revenue", "Commission", "Planner earnings"],
        "rows": [
            [
                row["event"].title,
                row["event"].category.name if row["event"].category else "",
                format_local(row["event"].start_datetime, "%d %b %Y"),
                row["status_label"],
                row["capacity"],
                row["sold"],
                row["remaining"],
                _money(row["gross"]),
                _money(row["refunds"]),
                _money(row["net_revenue"]),
                _money(row["commission"]),
                _money(row["earnings"]),
            ]
            for row in rows
        ],
    }


def _transaction_section(transactions, with_planner=False):
    columns = ["Date", "Type", "Reference", "Details", "Client", "Gross", "Commission", "Planner share", "Payout amount", "Status"]
    if with_planner:
        columns.insert(4, "Planner")
    rows = []
    for row in transactions[:MAX_EXPORT_TRANSACTIONS]:
        values = [
            format_local(row["when"]),
            TRANSACTION_LABELS[row["kind"]],
            row["reference"],
            row["detail"],
            row.get("client_name") or "",
            _money(row["gross"]),
            _money(row["commission"]),
            _money(row["net"]),
            _money(row.get("amount")),
            _label(row["status"]),
        ]
        if with_planner:
            values.insert(4, row.get("planner_name") or "")
        rows.append(values)
    return {"title": "Transaction records", "columns": columns, "rows": rows}


def _insight_section(insights):
    return {
        "title": "Performance insights",
        "columns": ["Insight", "Result", "Details"],
        "rows": [[item["label"], item["value"], item["detail"]] for item in insights],
    }


def _period_rows(report, transaction_type):
    period = report["period"]
    return [
        ["Reporting period", f"{period.label} ({period.range_text})"],
        ["Transactions included", TRANSACTION_TYPES[transaction_type]],
        ["Platform commission rate", f"{report['rate']}%"],
        ["Generated", format_local(utcnow()) + " (Lusaka time)"],
    ]


def planner_document(profile, report):
    rate = report["rate"]
    overview = report["overview"]
    events = overview["events"]
    bookings = report["bookings"]
    info = [
        ["Planner", profile.business_name],
        ["Contact person", profile.user.full_name],
        ["Email", profile.user.email],
        ["Phone", profile.user.phone],
        ["Location", profile.location],
        *_period_rows(report, report["transaction_type"]),
    ]
    if report["event"]:
        info.insert(5, ["Event", report["event"].title])
    overview_rows = [
        ["Events created", events["total"]],
        ["Published events", events["published"] + events["sold_out"]],
        ["Pending event approvals", events["pending"]],
        ["Rejected events", events["rejected"]],
        ["Completed events", events["completed"]],
        ["Tickets sold", overview["tickets_sold"]],
        ["Tickets refunded", report["tickets"]["refunded"]],
        ["Successful ticket transactions", report["tickets"]["successful_transactions"]],
        ["Total booking requests", bookings["total"]],
        *[[f"{_label(status)} bookings", bookings["counts"][status]] for status in BOOKING_STATUSES],
        ["Agreed booking value (accepted quotations, not paid through EventLink)", _money(bookings["agreed_value"])],
        ["Booking acceptance rate", _percent(bookings["acceptance_rate"])],
        ["Booking completion rate", _percent(bookings["completion_rate"])],
    ]
    sections = [
        _details("Report details", info),
        _finance_section(report["finance"], rate),
        {"title": "Overview", "columns": ["Measure", "Value"], "rows": overview_rows},
        _insight_section(report["insights"]),
        _event_section(report["events"]),
        {
            "title": "Sales by ticket category",
            "columns": ["Ticket category", "Tickets sold", "Revenue"],
            "rows": [[row["name"], row["tickets"], _money(row["revenue"])] for row in report["tickets"]["categories"]],
        },
        {
            "title": "Booking history",
            "columns": ["Reference", "Client", "Service", "Event date", "Requested", "Agreed amount", "Payment status", "Booking status"],
            "rows": [
                [row["reference"], row["client_name"], row["service"], _date(row["event_date"]), format_local(row["requested"], "%d %b %Y"),
                 _money(row["agreed_amount"]), row["payment_label"], _label(row["status"])]
                for row in report["booking_rows"]
            ],
        },
        _transaction_section(report["transactions"]),
        {
            "title": "Payout history",
            "columns": ["Date", "Reference", "Method and external reference", "Amount", "Note"],
            "rows": [[format_local(row["when"]), row["reference"], row["detail"], _money(row["amount"]), row["note"]] for row in report["payouts"]],
        },
    ]
    return {"title": f"{profile.business_name} performance report", "sections": sections}


def platform_document(report):
    rate = report["rate"]
    overview = report["overview"]
    events = overview["events"]
    info = _period_rows(report, report["transaction_type"])
    overview_rows = [
        ["Registered planners", overview["planners"]],
        ["Approved planners", overview["planners_approved"]],
        ["New planners in period", overview["planners_new"]],
        ["Events created", events["total"]],
        ["Published events", events["published"] + events["sold_out"]],
        ["Pending events", events["pending"]],
        ["Rejected events", events["rejected"]],
        ["Tickets sold", overview["tickets_sold"]],
        ["Successful transactions", overview["successful_transactions"]],
        ["Refunded transactions", overview["refunded_transactions"]],
        ["Pending payments", report["finance"]["pending_count"]],
        ["Planner booking requests", report["bookings"]["total"]],
    ]
    comparison = {
        "title": "Planner performance comparison",
        "columns": ["Planner", "Status", "Events created", "Tickets sold", "Gross revenue", "Refunds", "Net revenue",
                    f"Commission ({rate}%)", f"Planner earnings ({100 - rate}%)", "Payouts in period", "Pending payout balance",
                    "Booking requests", "Completed bookings", "Acceptance rate"],
        "rows": [
            [row["profile"].business_name, _label(row["profile"].approval_status), row["events"], row["tickets"], _money(row["gross"]),
             _money(row["refunds"]), _money(row["net_revenue"]), _money(row["commission"]), _money(row["earnings"]), _money(row["payouts"]),
             _money(row["balance"]), row["bookings"], row["bookings_completed"], _percent(row["acceptance_rate"])]
            for row in report["comparison"]
        ],
    }
    best = _event_section(report["best_events"])
    best["title"] = "Best-performing events"
    sections = [
        _details("Report details", info),
        _finance_section(report["finance"], rate, planner=False),
        {"title": "Platform overview", "columns": ["Measure", "Value"], "rows": overview_rows},
        _insight_section(report["insights"]),
        comparison,
        best,
        _transaction_section(report["transactions"], with_planner=True),
    ]
    return {"title": "EventLink Zambia platform report", "sections": sections}


FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_text(value):
    """Stop spreadsheet apps treating names or titles typed by users as formulas."""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def _csv(document):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([_safe_text(document["title"])])
    writer.writerow(["Amounts are in Zambian Kwacha (ZMW)."])
    for section in document["sections"]:
        writer.writerow([])
        writer.writerow([section["title"]])
        writer.writerow(section["columns"])
        if not section["rows"]:
            writer.writerow(["No records for this period."])
        for row in section["rows"]:
            writer.writerow([f"{value.ngwee / 100:.2f}" if isinstance(value, Money) else _safe_text(value) for value in row])
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def _sheet_name(title, used):
    name = re.sub(r"[\[\]:*?/\\]", "", title)[:31] or "Sheet"
    base, number = name, 2
    while name in used:
        suffix = f" {number}"
        name = base[: 31 - len(suffix)] + suffix
        number += 1
    used.add(name)
    return name


def _write_table(sheet, start_row, section):
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor=GREEN)
    sheet.cell(row=start_row, column=1, value=section["title"]).font = Font(bold=True, size=12, color=GREEN)
    header_row = start_row + 1
    for index, column in enumerate(section["columns"], start=1):
        cell = sheet.cell(row=header_row, column=index, value=column)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    row_number = header_row
    if not section["rows"]:
        row_number += 1
        sheet.cell(row=row_number, column=1, value="No records for this period.").font = Font(italic=True, color="666666")
    for values in section["rows"]:
        row_number += 1
        for index, value in enumerate(values, start=1):
            if isinstance(value, Money):
                cell = sheet.cell(row=row_number, column=index, value=value.ngwee / 100)
                cell.number_format = '"K"#,##0.00;-"K"#,##0.00'
            else:
                sheet.cell(row=row_number, column=index, value=_safe_text(value))
    return row_number


def _fit_columns(sheet):
    widths = {}
    for row in sheet.iter_rows():
        for cell in row:
            if cell.value is not None:
                length = len(str(cell.value)) if not isinstance(cell.value, float) else 12
                widths[cell.column] = max(widths.get(cell.column, 8), min(length + 2, 60))
    for column, width in widths.items():
        sheet.column_dimensions[get_column_letter(column)].width = width


def _xlsx(document):
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Summary"
    used = {"Summary"}
    summary.cell(row=1, column=1, value=_safe_text(document["title"])).font = Font(bold=True, size=14, color=GREEN)
    summary.cell(row=2, column=1, value="Amounts are in Zambian Kwacha (ZMW).").font = Font(italic=True, color="666666")
    row = 3
    for section in document["sections"]:
        if len(section["columns"]) <= 3:
            row = _write_table(summary, row + 1, section) + 1
            continue
        sheet = workbook.create_sheet(_sheet_name(section["title"], used))
        _write_table(sheet, 1, section)
        sheet.freeze_panes = "A3"
        _fit_columns(sheet)
    _fit_columns(summary)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


PDF_REPLACEMENTS = {"\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u2192": "->"}


def _pdf_text(value):
    if isinstance(value, Money):
        text = format_zmw(value.ngwee)
    else:
        text = "" if value is None else str(value)
    for original, replacement in PDF_REPLACEMENTS.items():
        text = text.replace(original, replacement)
    return text.encode("latin-1", "replace").decode("latin-1")


class _ReportPDF(FPDF):
    report_title = ""

    def header(self):
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(14, 107, 79)
        self.cell(0, 6, "EventLink Zambia", new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(222, 214, 198)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, _pdf_text(f"{self.report_title} - page {self.page_no()} of {{nb}}"), align="C")


def _pdf(document):
    pdf = _ReportPDF(orientation="L", unit="mm", format="A4")
    pdf.report_title = document["title"]
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(28, 36, 32)
    pdf.multi_cell(0, 9, _pdf_text(document["title"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(110, 110, 110)
    pdf.cell(0, 6, "Amounts are in Zambian Kwacha (ZMW).", new_x="LMARGIN", new_y="NEXT")
    heading = FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=(14, 107, 79))
    for section in document["sections"]:
        pdf.ln(4)
        if pdf.get_y() > pdf.h - 40:
            pdf.add_page()
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(14, 107, 79)
        pdf.cell(0, 7, _pdf_text(section["title"]), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(28, 36, 32)
        pdf.set_font("Helvetica", "", 7.5 if len(section["columns"]) > 8 else 9)
        if not section["rows"]:
            pdf.set_font("Helvetica", "I", 9)
            pdf.cell(0, 6, "No records for this period.", new_x="LMARGIN", new_y="NEXT")
            continue
        columns = len(section["columns"])
        widths = (3, 2) if columns == 2 else tuple(3 if index == 0 or section["columns"][index] in ("Details", "Event", "Planner") else 2 for index in range(columns))
        with pdf.table(col_widths=widths, headings_style=heading, line_height=4.6, padding=1.2, first_row_as_headings=True) as table:
            header = table.row()
            for column in section["columns"]:
                header.cell(_pdf_text(column))
            for values in section["rows"]:
                row = table.row()
                for value in values:
                    row.cell(_pdf_text(value), align="R" if isinstance(value, (Money, int)) else "L")
    return bytes(pdf.output())


MIMETYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}
RENDERERS = {"csv": _csv, "xlsx": _xlsx, "pdf": _pdf}


def export_response(document, fmt, name, period_key):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "report"
    stamp = utcnow().strftime("%Y%m%d")
    filename = f"eventlink-{slug}-{period_key}-{stamp}.{fmt}"
    return Response(
        RENDERERS[fmt](document),
        mimetype=MIMETYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
