"""
Core logic for the evcc charging cost report.

Contains no add-on-specific logic (no Flask, no /data/options.json) so the
module stays testable outside the container too.
"""

import os
from datetime import datetime
from io import BytesIO

import requests
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_RIGHT, TA_LEFT, TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    HRFlowable,
    KeepTogether,
)
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.charts.barcharts import VerticalBarChart

import i18n

PLAUSIBILITY_TOLERANCE_KWH = 0.3

# ---------------------------------------------------------------------------
# Design system: colors, typography, measurements
# ---------------------------------------------------------------------------

NAVY = colors.HexColor("#111827")
NAVY_SOFT = colors.HexColor("#1F2937")
ACCENT = colors.HexColor("#2563EB")
ACCENT_SOFT = colors.HexColor("#EFF4FF")
GREY_TEXT = colors.HexColor("#6B7280")
GREY_LINE = colors.HexColor("#E5E7EB")
STRIPE = colors.HexColor("#F8F9FB")
WARN_BG = colors.HexColor("#FFFBEB")
WARN_BORDER = colors.HexColor("#F59E0B")
WARN_TEXT = colors.HexColor("#92400E")
WHITE = colors.white

PAGE_MARGIN = 18 * mm
HEADER_HEIGHT = 32 * mm
FOOTER_HEIGHT = 14 * mm


def _register_fonts() -> tuple[str, str]:
    """Registers DejaVu Sans for a more modern look, if available.
    Falls back cleanly to the built-in Helvetica fonts (e.g. when the
    ttf-dejavu package isn't installed in the container)."""
    candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for regular_path, bold_path in candidates:
        if os.path.exists(regular_path) and os.path.exists(bold_path):
            try:
                pdfmetrics.registerFont(TTFont("Report-Regular", regular_path))
                pdfmetrics.registerFont(TTFont("Report-Bold", bold_path))
                pdfmetrics.registerFontFamily(
                    "Report", normal="Report-Regular", bold="Report-Bold"
                )
                return "Report-Regular", "Report-Bold"
            except Exception:
                break
    return "Helvetica", "Helvetica-Bold"


FONT_REGULAR, FONT_BOLD = _register_fonts()


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", fontName=FONT_BOLD, fontSize=17, leading=20, textColor=WHITE,
        ),
        "title_sub": ParagraphStyle(
            "title_sub", fontName=FONT_REGULAR, fontSize=9.5, leading=12,
            textColor=colors.HexColor("#C7D2FE"),
        ),
        "label": ParagraphStyle(
            "label", fontName=FONT_REGULAR, fontSize=7.5, leading=10,
            textColor=GREY_TEXT, letterSpacing=0.6,
        ),
        "value": ParagraphStyle(
            "value", fontName=FONT_BOLD, fontSize=10.5, leading=13,
            textColor=NAVY,
        ),
        "section": ParagraphStyle(
            "section", fontName=FONT_BOLD, fontSize=9.5, leading=12,
            textColor=NAVY, spaceBefore=0, spaceAfter=0,
        ),
        "kpi_value": ParagraphStyle(
            "kpi_value", fontName=FONT_BOLD, fontSize=15, leading=18,
            textColor=NAVY, alignment=TA_LEFT,
        ),
        "kpi_label": ParagraphStyle(
            "kpi_label", fontName=FONT_REGULAR, fontSize=7.5, leading=10,
            textColor=GREY_TEXT, alignment=TA_LEFT,
        ),
        "cell": ParagraphStyle(
            "cell", fontName=FONT_REGULAR, fontSize=8.7, leading=11, textColor=NAVY_SOFT,
        ),
        "cell_num": ParagraphStyle(
            "cell_num", fontName=FONT_REGULAR, fontSize=8.7, leading=11,
            textColor=NAVY_SOFT, alignment=TA_RIGHT,
        ),
        "cell_head": ParagraphStyle(
            "cell_head", fontName=FONT_BOLD, fontSize=7.6, leading=10, textColor=WHITE,
        ),
        "cell_head_num": ParagraphStyle(
            "cell_head_num", fontName=FONT_BOLD, fontSize=7.6, leading=10,
            textColor=WHITE, alignment=TA_RIGHT,
        ),
        "sum_label": ParagraphStyle(
            "sum_label", fontName=FONT_BOLD, fontSize=9, leading=12, textColor=NAVY,
            alignment=TA_RIGHT,
        ),
        "sum_value": ParagraphStyle(
            "sum_value", fontName=FONT_BOLD, fontSize=9.5, leading=12, textColor=ACCENT,
            alignment=TA_RIGHT,
        ),
        "small": ParagraphStyle(
            "small", fontName=FONT_REGULAR, fontSize=7.6, leading=10.5, textColor=GREY_TEXT,
        ),
        "warn": ParagraphStyle(
            "warn", fontName=FONT_REGULAR, fontSize=8, leading=11.5, textColor=WARN_TEXT,
        ),
        "warn_head": ParagraphStyle(
            "warn_head", fontName=FONT_BOLD, fontSize=8.2, leading=11, textColor=WARN_TEXT,
        ),
        "sig_label": ParagraphStyle(
            "sig_label", fontName=FONT_REGULAR, fontSize=7.8, leading=10, textColor=GREY_TEXT,
        ),
    }


# ---------------------------------------------------------------------------
# evcc data access
# ---------------------------------------------------------------------------

def fetch_sessions(evcc_url: str, month: int, year: int, locale: str = "en") -> list[dict]:
    url = f"{evcc_url.rstrip('/')}/api/sessions"
    params = {"format": "json", "month": month, "year": year, "lang": locale}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    payload = resp.json()
    sessions = payload.get("result", payload) if isinstance(payload, dict) else payload
    if not isinstance(sessions, list):
        raise ValueError("Unexpected evcc API response format (not a list of sessions).")
    return sessions


def filter_by_vehicle(sessions: list[dict], vehicles: list[str]) -> list[dict]:
    """Filters sessions down to the configured vehicles (evcc field 'vehicle').

    What matters is the vehicle, not the charge point: this way charging
    sessions of the same vehicle at different (home) charge points are
    correctly included, while other vehicles at the same charge point are
    reliably excluded.
    """
    if not vehicles:
        return sessions
    return [s for s in sessions if s.get("vehicle") in vehicles]


def check_plausibility(session: dict, locale: str = "en") -> str | None:
    start = session.get("meterStart")
    stop = session.get("meterStop")
    energy = session.get("chargedEnergy")
    if start is None or stop is None or energy is None:
        return i18n.t(locale, "report.meter_missing")
    diff = round(stop - start, 2)
    if abs(diff - energy) > PLAUSIBILITY_TOLERANCE_KWH:
        return i18n.t(locale, "report.meter_deviation", diff=diff, energy=energy)
    return None


def compute_amount(energy_kwh: float, rate_eur_per_kwh: float) -> float:
    return round(energy_kwh * rate_eur_per_kwh, 2)


def resolve_tariff(
    session_date, tariff_periods: list[dict], locale: str = "en"
) -> tuple[float, dict | None, str | None]:
    """Determines the tariff in effect for a charging date from the tariff history.

    ``tariff_periods``: list of {"start_date": date, "price": float}, any
    order. The entry with the most recent start date <= session_date applies.
    If session_date is before the earliest entry, that entry is used as a
    fallback and a warning is returned (instead of aborting report creation).
    With no entries at all, 0.0 is returned with a warning.

    Returns (price, period used, warning). The returned period matters for
    later identifying exactly which periods were actually used - matching by
    price alone would incorrectly flag both periods as "used" when two
    periods happen to share the same price (e.g. an unchanged tariff but a
    new contract/receipt at the turn of the year).
    """
    if not tariff_periods:
        return 0.0, None, i18n.t(locale, "report.no_tariff_history")

    sorted_periods = sorted(tariff_periods, key=lambda p: p["start_date"])
    applicable = None
    for p in sorted_periods:
        if p["start_date"] <= session_date:
            applicable = p
        else:
            break

    if applicable is None:
        earliest = sorted_periods[0]
        price_label = i18n.fmt_number(earliest["price"], 4, locale)
        warning = i18n.t(
            locale, "report.no_tariff_before",
            date=i18n.fmt_date(earliest["start_date"], locale), price=price_label,
        )
        return earliest["price"], earliest, warning
    return applicable["price"], applicable, None


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# PDF assembly
# ---------------------------------------------------------------------------

def _month_name(month: int, locale: str) -> str:
    return i18n.strings(locale)["report"]["months"][month]


def _kpi_card(value: str, label: str, styles: dict, accent: bool = False) -> Table:
    t = Table(
        [[Paragraph(value, styles["kpi_value"])], [Paragraph(label, styles["kpi_label"])]],
        colWidths=[52 * mm],
    )
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT_SOFT if accent else STRIPE),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING", (0, 1), (-1, 1), 0),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("LINEBELOW", (0, 0), (-1, 0), 2.2, ACCENT if accent else colors.HexColor("#CBD5E1")),
    ]))
    return t


def _build_attachment_cover(
    period: dict, period_label: str, original_filename: str | None, locale: str = "en"
) -> bytes:
    """Renders a single divider page placed before an attached tariff receipt PDF."""
    buf = BytesIO()
    styles = _styles()
    cnv = pdfcanvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4
    r = i18n.strings(locale)["report"]

    cnv.setFillColor(NAVY)
    cnv.rect(0, page_h - HEADER_HEIGHT, page_w, HEADER_HEIGHT, stroke=0, fill=1)
    cnv.setFillColor(ACCENT)
    cnv.rect(0, page_h - HEADER_HEIGHT - 1.2 * mm, page_w, 1.2 * mm, stroke=0, fill=1)
    cnv.setFont(FONT_BOLD, 16)
    cnv.setFillColor(WHITE)
    cnv.drawString(PAGE_MARGIN, page_h - 15 * mm, r["attachment_cover_title"])
    cnv.setFont(FONT_REGULAR, 9.5)
    cnv.setFillColor(colors.HexColor("#C7D2FE"))
    cnv.drawString(PAGE_MARGIN, page_h - 21.5 * mm, r["attachment_cover_subtitle"])
    cnv.setFont(FONT_BOLD, 12)
    cnv.setFillColor(WHITE)
    cnv.drawRightString(page_w - PAGE_MARGIN, page_h - 15 * mm, period_label)
    cnv.setFont(FONT_REGULAR, 8.5)
    cnv.setFillColor(colors.HexColor("#C7D2FE"))
    cnv.drawRightString(page_w - PAGE_MARGIN, page_h - 21.5 * mm, r["attachment_period"])

    center_y = page_h / 2
    cnv.setFillColor(ACCENT)
    cnv.setFont(FONT_BOLD, 11)
    cnv.drawCentredString(page_w / 2, center_y + 22 * mm, r["attachment_label"])
    cnv.setFillColor(NAVY)
    cnv.setFont(FONT_BOLD, 18)
    cnv.drawCentredString(page_w / 2, center_y + 10 * mm, r["attachment_tariff_receipt"])

    cnv.setFont(FONT_REGULAR, 11)
    cnv.setFillColor(NAVY_SOFT)
    price_label = i18n.fmt_number(period["price"], 4, locale)
    cnv.drawCentredString(
        page_w / 2, center_y - 2 * mm,
        i18n.t(
            locale, "report.attachment_valid_from",
            date=i18n.fmt_date(period["start_date"], locale), price=price_label,
        ),
    )
    if original_filename:
        cnv.setFont(FONT_REGULAR, 9)
        cnv.setFillColor(GREY_TEXT)
        cnv.drawCentredString(
            page_w / 2, center_y - 9 * mm,
            i18n.t(locale, "report.attachment_uploaded_file", name=original_filename),
        )

    cnv.setFont(FONT_REGULAR, 7.5)
    cnv.setFillColor(GREY_TEXT)
    cnv.drawCentredString(page_w / 2, 20 * mm, r["attachment_footer"])
    cnv.save()
    return buf.getvalue()


def _make_chart(sessions_sorted: list[dict], locale: str = "en") -> Drawing:
    labels = []
    values = []
    date_fmt = "%d.%m." if locale == "de" else "%m/%d"
    for s in sessions_sorted:
        try:
            dt = parse_iso(s["created"])
        except (KeyError, ValueError):
            continue
        labels.append(dt.strftime(date_fmt))
        values.append(round(s.get("chargedEnergy", 0.0) or 0.0, 1))

    # With many charging sessions, label only every nth date - otherwise the
    # axis labels overlap and become unreadable.
    step = max(1, -(-len(labels) // 12))  # ceil(len/12)
    labels = [lab if i % step == 0 else "" for i, lab in enumerate(labels)]

    drawing = Drawing(174 * mm, 28 * mm)
    chart = VerticalBarChart()
    chart.x = 5
    chart.y = 5
    chart.height = 21 * mm
    chart.width = 168 * mm
    chart.data = [values]
    chart.categoryAxis.categoryNames = labels
    chart.categoryAxis.labels.fontName = FONT_REGULAR
    chart.categoryAxis.labels.fontSize = 6
    chart.categoryAxis.labels.fillColor = GREY_TEXT
    chart.valueAxis.labels.fontName = FONT_REGULAR
    chart.valueAxis.labels.fontSize = 6
    chart.valueAxis.labels.fillColor = GREY_TEXT
    chart.valueAxis.valueMin = 0
    chart.bars[0].fillColor = ACCENT
    chart.barWidth = 6
    chart.groupSpacing = 4
    chart.valueAxis.gridStrokeColor = GREY_LINE
    chart.valueAxis.visibleGrid = True
    chart.valueAxis.gridStrokeWidth = 0.4
    chart.categoryAxis.strokeColor = GREY_LINE
    chart.valueAxis.strokeColor = GREY_LINE
    drawing.add(chart)
    return drawing


def build_pdf(
    sessions: list[dict],
    out_path: str,
    month: int,
    year: int,
    method: str,
    rate_ct_per_kwh: float,
    employee: str,
    vehicle: str,
    tariff_periods: list[dict] | None = None,
    footnote: str | None = None,
    include_chart: bool = False,
    locale: str = "en",
) -> dict:
    """Builds the PDF and returns a small summary (totals, warnings).

    ``tariff_periods`` is the tariff history for method "actual": a list of
    {"start_date": date, "price": float}. For each charging session, the rate
    in effect on that date is applied (see ``resolve_tariff``) and shown as
    its own column in the table. It is ignored for method "pauschale".

    ``footnote`` overrides the default text shown below the table (e.g. to
    use company-specific wording). If nothing is passed, or an empty string,
    the default text derived from the calculation method applies.

    ``include_chart`` controls whether the trend chart is included
    (default: no).

    ``locale`` selects the language used for every label in the PDF
    (see app/locales/*.yaml); defaults to English.
    """
    styles = _styles()
    r = i18n.strings(locale)["report"]
    period_label = f"{_month_name(month, locale)} {year}"

    method_footnote = (
        r["footnote_flat"] if method == "pauschale" else r["footnote_actual"]
    )
    default_footnote_text = f"{method_footnote}. {r['default_footnote_suffix']}"

    # ---- Header/footer, identical on every page ---------------------------
    def draw_header_footer(cnv: pdfcanvas.Canvas, doc) -> None:
        page_w, page_h = A4
        cnv.saveState()

        # Header band
        cnv.setFillColor(NAVY)
        cnv.rect(0, page_h - HEADER_HEIGHT, page_w, HEADER_HEIGHT, stroke=0, fill=1)
        cnv.setFillColor(ACCENT)
        cnv.rect(0, page_h - HEADER_HEIGHT - 1.2 * mm, page_w, 1.2 * mm, stroke=0, fill=1)

        cnv.setFont(FONT_BOLD, 16)
        cnv.setFillColor(WHITE)
        cnv.drawString(PAGE_MARGIN, page_h - 15 * mm, r["header_title"])

        cnv.setFont(FONT_REGULAR, 9.5)
        cnv.setFillColor(colors.HexColor("#C7D2FE"))
        cnv.drawString(PAGE_MARGIN, page_h - 21.5 * mm, r["header_subtitle"])

        cnv.setFont(FONT_BOLD, 12)
        cnv.setFillColor(WHITE)
        cnv.drawRightString(page_w - PAGE_MARGIN, page_h - 15 * mm, period_label)
        cnv.setFont(FONT_REGULAR, 8.5)
        cnv.setFillColor(colors.HexColor("#C7D2FE"))
        cnv.drawRightString(page_w - PAGE_MARGIN, page_h - 21.5 * mm, r["period_label"])

        # Footer
        cnv.setStrokeColor(GREY_LINE)
        cnv.setLineWidth(0.5)
        cnv.line(PAGE_MARGIN, FOOTER_HEIGHT, page_w - PAGE_MARGIN, FOOTER_HEIGHT)
        cnv.setFont(FONT_REGULAR, 7)
        cnv.setFillColor(GREY_TEXT)
        cnv.drawString(
            PAGE_MARGIN, FOOTER_HEIGHT - 5 * mm,
            i18n.t(locale, "report.footer_created", date=i18n.fmt_datetime(datetime.now(), locale)),
        )
        cnv.drawRightString(
            page_w - PAGE_MARGIN, FOOTER_HEIGHT - 5 * mm,
            i18n.t(locale, "report.footer_page", page=doc.page),
        )
        cnv.restoreState()

    main_buffer = BytesIO()
    doc = SimpleDocTemplate(
        main_buffer,
        pagesize=A4,
        topMargin=HEADER_HEIGHT + 6 * mm,
        bottomMargin=FOOTER_HEIGHT + 4 * mm,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
        title=i18n.t(locale, "report.pdf_title", month=month, year=year),
        author=r["pdf_author"],
    )

    story = []

    # ---- Process sessions (before the meta header, since the method label
    #      and KPI tiles depend on the results) -----------------------------
    sessions_sorted = sorted(sessions, key=lambda s: s.get("created", ""))
    rows = []
    total_kwh = 0.0
    total_amount = 0.0
    warnings = []
    used_rates: set[float] = set()
    used_period_keys: set = set()  # start dates of the tariff periods actually used

    for s in sessions_sorted:
        try:
            start_dt = parse_iso(s["created"])
            end_dt = parse_iso(s["finished"]) if s.get("finished") else start_dt
        except (KeyError, ValueError):
            continue

        energy = s.get("chargedEnergy", 0.0) or 0.0
        meter_start = s.get("meterStart")
        meter_stop = s.get("meterStop")

        if method == "pauschale":
            rate = rate_ct_per_kwh / 100.0
        else:
            rate, used_period, tariff_warning = resolve_tariff(
                start_dt.date(), tariff_periods or [], locale
            )
            if used_period is not None:
                used_period_keys.add(used_period["start_date"])
            if tariff_warning:
                warnings.append(f"{i18n.fmt_date(start_dt, locale)}: {tariff_warning}")
        amount = compute_amount(energy, rate)
        used_rates.add(round(rate, 6))

        total_kwh += energy
        total_amount += amount

        issue = check_plausibility(s, locale)
        if issue:
            warnings.append(f"{i18n.fmt_date(start_dt, locale)}: {issue}")

        rows.append((start_dt, end_dt, meter_start, meter_stop, energy, amount, rate))

    # ---- Relevant tariff periods + their receipt PDFs (method "actual" only) --
    # Important: match via the period actually used (start date), NOT via the
    # price - otherwise a period that happens to share the same price but was
    # never applied would be incorrectly listed too (e.g. an old bulk entry
    # with the same cent amount as the one that's actually in effect).
    relevant_periods = []
    periods_with_docs = []
    if method == "actual":
        relevant_periods = sorted(
            (p for p in (tariff_periods or []) if p["start_date"] in used_period_keys),
            key=lambda p: p["start_date"],
        )
        for p in relevant_periods:
            doc_path = p.get("document_path")
            if not doc_path:
                continue
            try:
                PdfReader(doc_path)  # only to validate that the file is readable
            except Exception:
                warnings.append(
                    i18n.t(
                        locale, "report.tariff_receipt_unreadable",
                        date=i18n.fmt_date(p["start_date"], locale),
                    )
                )
                continue
            periods_with_docs.append(p)

    if method == "pauschale":
        method_label = i18n.t(locale, "report.method_flat_label", rate=rate_ct_per_kwh)
    elif len(used_rates) == 1:
        rate_label = i18n.fmt_number(next(iter(used_rates)), 4, locale)
        method_label = i18n.t(locale, "report.method_actual_label_single", rate=rate_label)
    elif len(used_rates) > 1:
        method_label = r["method_actual_label_multi"]
    else:
        method_label = r["method_actual_label_none"]

    # ---- Meta row: employee / vehicle / method -----------------------------
    meta_table = Table(
        [[
            Paragraph(r["meta_employee"], styles["label"]),
            Paragraph(r["meta_vehicle"], styles["label"]),
            Paragraph(r["meta_method"], styles["label"]),
        ], [
            Paragraph(employee or "—", styles["value"]),
            Paragraph(vehicle or "—", styles["value"]),
            Paragraph(method_label, styles["value"]),
        ]],
        colWidths=[58 * mm, 58 * mm, 58 * mm],
    )
    meta_table.setStyle(TableStyle([
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 3 * mm))
    story.append(HRFlowable(width="100%", thickness=0.6, color=GREY_LINE))
    story.append(Spacer(1, 4 * mm))

    # ---- KPI tiles ------------------------------------------------------------
    kpi_row = Table(
        [[
            _kpi_card(str(len(rows)), r["kpi_sessions"], styles),
            _kpi_card(f"{i18n.fmt_number(total_kwh, 2, locale)} kWh", r["kpi_energy"], styles),
            _kpi_card(i18n.fmt_currency(total_amount, locale), r["kpi_amount"], styles, accent=True),
        ]],
        colWidths=[58 * mm, 58 * mm, 58 * mm],
        spaceBefore=0,
    )
    kpi_row.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 3),
        ("RIGHTPADDING", (1, 0), (1, 0), 3),
        ("LEFTPADDING", (1, 0), (1, 0), 3),
        ("LEFTPADDING", (2, 0), (2, 0), 3),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(kpi_row)
    story.append(Spacer(1, 5 * mm))

    # ---- Detail table -----------------------------------------------------------
    story.append(Paragraph(r["section_sessions"], styles["section"]))
    story.append(Spacer(1, 3 * mm))

    header = [
        Paragraph(r["col_date"], styles["cell_head"]),
        Paragraph(r["col_start"], styles["cell_head"]),
        Paragraph(r["col_end"], styles["cell_head"]),
        Paragraph(r["col_meter_start"], styles["cell_head_num"]),
        Paragraph(r["col_meter_end"], styles["cell_head_num"]),
        Paragraph(r["col_charged"], styles["cell_head_num"]),
    ]
    show_rate_column = method == "actual"
    if show_rate_column:
        header.append(Paragraph(r["col_rate"], styles["cell_head_num"]))
    header.append(Paragraph(r["col_amount"], styles["cell_head_num"]))

    table_data = [header]
    overnight_present = False

    for start_dt, end_dt, meter_start, meter_stop, energy, amount, rate in rows:
        day_diff = (end_dt.date() - start_dt.date()).days
        overnight_present = overnight_present or day_diff > 0
        end_label = i18n.fmt_time(end_dt) + (f" +{day_diff}" if day_diff > 0 else "")
        row_cells = [
            Paragraph(i18n.fmt_date(start_dt, locale), styles["cell"]),
            Paragraph(i18n.fmt_time(start_dt), styles["cell"]),
            Paragraph(end_label, styles["cell"]),
            Paragraph(
                i18n.fmt_number(meter_start, 2, locale) if meter_start is not None else "–",
                styles["cell_num"],
            ),
            Paragraph(
                i18n.fmt_number(meter_stop, 2, locale) if meter_stop is not None else "–",
                styles["cell_num"],
            ),
            Paragraph(i18n.fmt_number(energy, 2, locale), styles["cell_num"]),
        ]
        if show_rate_column:
            row_cells.append(Paragraph(i18n.fmt_number(rate, 4, locale), styles["cell_num"]))
        row_cells.append(Paragraph(i18n.fmt_number(amount, 2, locale), styles["cell_num"]))
        table_data.append(row_cells)

    if len(table_data) == 1:
        story.append(Paragraph(r["no_sessions"], styles["cell"]))
    else:
        if show_rate_column:
            col_widths = [23 * mm, 16 * mm, 22 * mm, 21 * mm, 21 * mm, 30 * mm, 17 * mm, 24 * mm]
        else:
            col_widths = [27 * mm, 15 * mm, 23 * mm, 27 * mm, 27 * mm, 27 * mm, 28 * mm]
        t = Table(table_data, colWidths=col_widths, repeatRows=1)
        row_styles = [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("TOPPADDING", (0, 1), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("LINEBELOW", (0, 0), (-1, 0), 0, WHITE),
            ("LINEBELOW", (0, 1), (-1, -2), 0.5, GREY_LINE),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, STRIPE]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]
        t.setStyle(TableStyle(row_styles))
        story.append(t)

        # Total row as its own, visually separated table
        total_kwh_label = f"{i18n.fmt_number(total_kwh, 2, locale)} kWh"
        total_amount_label = i18n.fmt_currency(total_amount, locale)
        if show_rate_column:
            sum_table = Table(
                [[
                    Paragraph(r["sum_label"], styles["sum_label"]),
                    Paragraph(total_kwh_label, styles["sum_value"]),
                    "",
                    Paragraph(total_amount_label, styles["sum_value"]),
                ]],
                colWidths=[23 * mm + 16 * mm + 22 * mm + 21 * mm + 21 * mm, 30 * mm, 17 * mm, 24 * mm],
            )
        else:
            sum_table = Table(
                [[
                    Paragraph(r["sum_label"], styles["sum_label"]),
                    Paragraph(total_kwh_label, styles["sum_value"]),
                    Paragraph(total_amount_label, styles["sum_value"]),
                ]],
                colWidths=[119 * mm, 27 * mm, 28 * mm],
            )
        sum_table.setStyle(TableStyle([
            ("LINEABOVE", (0, 0), (-1, 0), 1.1, NAVY),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(sum_table)

        if show_rate_column and len(used_rates) > 1 and relevant_periods:
            story.append(Spacer(1, 6 * mm))
            tariff_block = [Paragraph(r["section_tariffs_used"], styles["section"]),
                             Spacer(1, 3 * mm)]

            tariff_header = [
                Paragraph(r["col_valid_from"], styles["cell_head"]),
                Paragraph(r["col_price"], styles["cell_head_num"]),
                Paragraph(r["col_receipt"], styles["cell_head"]),
            ]
            tariff_rows = [tariff_header]
            for p in relevant_periods:
                has_doc = p in periods_with_docs
                tariff_rows.append([
                    Paragraph(i18n.fmt_date(p["start_date"], locale), styles["cell"]),
                    Paragraph(i18n.fmt_number(p["price"], 4, locale), styles["cell_num"]),
                    Paragraph(r["receipt_see_attachment"] if has_doc else "–", styles["cell"]),
                ])
            tariff_table = Table(tariff_rows, colWidths=[32 * mm, 32 * mm, 32 * mm])
            tariff_table.hAlign = "LEFT"
            tariff_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("TOPPADDING", (0, 0), (-1, 0), 5),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
                ("TOPPADDING", (0, 1), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("LINEBELOW", (0, 1), (-1, -2), 0.5, GREY_LINE),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, STRIPE]),
            ]))
            tariff_block.append(tariff_table)
            story.append(KeepTogether(tariff_block))
        elif show_rate_column and len(used_rates) > 1:
            story.append(Spacer(1, 2 * mm))
            story.append(Paragraph(r["multi_tariff_note"], styles["small"]))

        if overnight_present:
            story.append(Spacer(1, 2 * mm))
            story.append(Paragraph(r["overnight_note"], styles["small"]))

    # ---- Trend chart (optional, default: off) ---------------------------------
    if include_chart and len(rows) >= 2:
        story.append(Spacer(1, 5 * mm))
        story.append(KeepTogether([
            Paragraph(r["chart_section"], styles["section"]),
            Spacer(1, 3 * mm),
            _make_chart(sessions_sorted, locale),
        ]))

    # ---- Warnings / plausibility ------------------------------------------------
    if warnings:
        story.append(Spacer(1, 5 * mm))
        warn_lines = [Paragraph(r["warnings_title"], styles["warn_head"])]
        for w in warnings:
            warn_lines.append(Paragraph(f"• {w}", styles["warn"]))
        warn_box = Table([[warn_lines]], colWidths=[174 * mm])
        warn_box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), WARN_BG),
            ("BOX", (0, 0), (-1, -1), 0, WARN_BG),
            ("LINEBEFORE", (0, 0), (0, 0), 2.5, WARN_BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ]))
        story.append(warn_box)

    # ---- Footnote & signature -----------------------------------------------
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph(
        footnote.strip() if footnote and footnote.strip() else default_footnote_text,
        styles["small"],
    ))
    if periods_with_docs:
        story.append(Spacer(1, 1.5 * mm))
        count = len(periods_with_docs)
        text = (
            r["attachment_single"] if count == 1
            else i18n.t(locale, "report.attachment_multi", count=count)
        )
        story.append(Paragraph(text, styles["small"]))
    story.append(Spacer(1, 9 * mm))

    sig_table = Table(
        [
            ["", ""],
            [HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#9CA3AF")),
             HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#9CA3AF"))],
            [Paragraph(r["sig_place_date"], styles["sig_label"]),
             Paragraph(r["sig_employee"], styles["sig_label"])],
        ],
        colWidths=[80 * mm, 80 * mm],
    )
    sig_table.setStyle(TableStyle([
        ("TOPPADDING", (0, 0), (-1, 0), 0),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
        ("TOPPADDING", (0, 2), (-1, 2), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, -1), 14),
    ]))
    story.append(KeepTogether(sig_table))

    doc.build(story, onFirstPage=draw_header_footer, onLaterPages=draw_header_footer)

    if periods_with_docs:
        writer = PdfWriter()
        for page in PdfReader(main_buffer).pages:
            writer.add_page(page)
        for p in periods_with_docs:
            cover_bytes = _build_attachment_cover(
                p, period_label, p.get("document_original_name"), locale
            )
            for page in PdfReader(BytesIO(cover_bytes)).pages:
                writer.add_page(page)
            for page in PdfReader(p["document_path"]).pages:
                writer.add_page(page)
        with open(out_path, "wb") as f:
            writer.write(f)
    else:
        with open(out_path, "wb") as f:
            f.write(main_buffer.getvalue())

    return {
        "sessions": len(rows),
        "total_kwh": round(total_kwh, 2),
        "total_amount": round(total_amount, 2),
        "warnings": warnings,
        "attached_documents": len(periods_with_docs),
    }
