"""
Kernlogik für den evcc Ladekosten-Report.

Enthaelt keine Add-on-spezifische Logik (kein Flask, kein /data/options.json) -
so bleibt das Modul auch ausserhalb des Containers testbar.
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

PLAUSIBILITY_TOLERANCE_KWH = 0.3

# ---------------------------------------------------------------------------
# Design-System: Farben, Typografie, Maße
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
    """Registriert DejaVu Sans für ein moderneres Schriftbild, falls verfügbar.
    Fällt sauber auf die eingebauten Helvetica-Fonts zurück (z. B. wenn das
    Paket ttf-dejavu im Container nicht installiert ist)."""
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
# evcc-Datenzugriff
# ---------------------------------------------------------------------------

def fetch_sessions(evcc_url: str, month: int, year: int) -> list[dict]:
    url = f"{evcc_url.rstrip('/')}/api/sessions"
    params = {"format": "json", "month": month, "year": year, "lang": "de"}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    payload = resp.json()
    sessions = payload.get("result", payload) if isinstance(payload, dict) else payload
    if not isinstance(sessions, list):
        raise ValueError("Unerwartetes Antwortformat der evcc-API (keine Liste von Sessions).")
    return sessions


def filter_by_vehicle(sessions: list[dict], vehicles: list[str]) -> list[dict]:
    """Filtert Sessions auf die konfigurierten Fahrzeuge (evcc-Feld 'vehicle').

    Relevant ist das Fahrzeug, nicht der Ladepunkt: so werden auch
    Ladevorgänge desselben Fahrzeugs an unterschiedlichen (Home-)Ladepunkten
    korrekt erfasst, und andere Fahrzeuge am selben Ladepunkt bleiben außen vor.
    """
    if not vehicles:
        return sessions
    return [s for s in sessions if s.get("vehicle") in vehicles]


def check_plausibility(session: dict) -> str | None:
    start = session.get("meterStart")
    stop = session.get("meterStop")
    energy = session.get("chargedEnergy")
    if start is None or stop is None or energy is None:
        return "Zählerstand fehlt"
    diff = round(stop - start, 2)
    if abs(diff - energy) > PLAUSIBILITY_TOLERANCE_KWH:
        return f"Abweichung Zähler ({diff} kWh) vs. Energie ({energy} kWh)"
    return None


def compute_amount(energy_kwh: float, rate_eur_per_kwh: float) -> float:
    return round(energy_kwh * rate_eur_per_kwh, 2)


def resolve_tariff(session_date, tariff_periods: list[dict]) -> tuple[float, dict | None, str | None]:
    """Ermittelt den zum Ladedatum gültigen Tarif aus der Tarifhistorie.

    ``tariff_periods``: Liste von {"start_date": date, "price": float}, beliebige
    Reihenfolge. Es gilt der Eintrag mit dem jüngsten Startdatum <= session_date.
    Liegt session_date vor dem frühesten Eintrag, wird dieser als Fallback
    genutzt und eine Warnung zurückgegeben (statt die Erstellung abzubrechen).
    Ohne jegliche Einträge wird 0.0 mit Warnung zurückgegeben.

    Gibt (Preis, verwendete Periode, Warnung) zurück. Die zurückgegebene
    Periode ist wichtig, um später genau die tatsächlich herangezogenen
    Zeiträume zu identifizieren - rein über den Preis zu matchen würde bei
    zwei Perioden mit zufällig demselben Preis (z. B. unveränderter Tarif,
    aber neuer Vertrag/Beleg zum Jahreswechsel) fälschlich beide als
    "verwendet" markieren.
    """
    if not tariff_periods:
        return 0.0, None, "Kein Tarif in der Tarifhistorie hinterlegt"

    sorted_periods = sorted(tariff_periods, key=lambda p: p["start_date"])
    applicable = None
    for p in sorted_periods:
        if p["start_date"] <= session_date:
            applicable = p
        else:
            break

    if applicable is None:
        earliest = sorted_periods[0]
        price_label = f"{earliest['price']:.4f}".replace(".", ",")
        return earliest["price"], earliest, (
            f"Kein Tarif vor {earliest['start_date'].strftime('%d.%m.%Y')} hinterlegt – "
            f"{price_label} €/kWh angenommen"
        )
    return applicable["price"], applicable, None


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# PDF-Aufbau
# ---------------------------------------------------------------------------

MONTH_NAMES_DE = [
    "", "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


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


def _build_attachment_cover(period: dict, period_label: str, original_filename: str | None) -> bytes:
    """Erzeugt eine einzelne Trennseite vor einem angehängten Tarifnachweis-PDF."""
    buf = BytesIO()
    styles = _styles()
    cnv = pdfcanvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4

    cnv.setFillColor(NAVY)
    cnv.rect(0, page_h - HEADER_HEIGHT, page_w, HEADER_HEIGHT, stroke=0, fill=1)
    cnv.setFillColor(ACCENT)
    cnv.rect(0, page_h - HEADER_HEIGHT - 1.2 * mm, page_w, 1.2 * mm, stroke=0, fill=1)
    cnv.setFont(FONT_BOLD, 16)
    cnv.setFillColor(WHITE)
    cnv.drawString(PAGE_MARGIN, page_h - 15 * mm, "Ladekosten-Nachweis")
    cnv.setFont(FONT_REGULAR, 9.5)
    cnv.setFillColor(colors.HexColor("#C7D2FE"))
    cnv.drawString(PAGE_MARGIN, page_h - 21.5 * mm, "Laden des Dienstwagens am privaten Anschluss")
    cnv.setFont(FONT_BOLD, 12)
    cnv.setFillColor(WHITE)
    cnv.drawRightString(page_w - PAGE_MARGIN, page_h - 15 * mm, period_label)
    cnv.setFont(FONT_REGULAR, 8.5)
    cnv.setFillColor(colors.HexColor("#C7D2FE"))
    cnv.drawRightString(page_w - PAGE_MARGIN, page_h - 21.5 * mm, "Zeitraum")

    center_y = page_h / 2
    cnv.setFillColor(ACCENT)
    cnv.setFont(FONT_BOLD, 11)
    cnv.drawCentredString(page_w / 2, center_y + 22 * mm, "ANLAGE")
    cnv.setFillColor(NAVY)
    cnv.setFont(FONT_BOLD, 18)
    cnv.drawCentredString(page_w / 2, center_y + 10 * mm, "Tarifnachweis")

    cnv.setFont(FONT_REGULAR, 11)
    cnv.setFillColor(NAVY_SOFT)
    price_label = f"{period['price']:.4f}".replace(".", ",")
    cnv.drawCentredString(
        page_w / 2, center_y - 2 * mm,
        f"Gültig ab {period['start_date'].strftime('%d.%m.%Y')} · {price_label} €/kWh",
    )
    if original_filename:
        cnv.setFont(FONT_REGULAR, 9)
        cnv.setFillColor(GREY_TEXT)
        cnv.drawCentredString(page_w / 2, center_y - 9 * mm, f"Hochgeladene Datei: {original_filename}")

    cnv.setFont(FONT_REGULAR, 7.5)
    cnv.setFillColor(GREY_TEXT)
    cnv.drawCentredString(
        page_w / 2, 20 * mm,
        "Vom Nutzer hochgeladener Beleg, unverändert angehängt.",
    )
    cnv.save()
    return buf.getvalue()


def _make_chart(sessions_sorted: list[dict]) -> Drawing:
    labels = []
    values = []
    for s in sessions_sorted:
        try:
            dt = parse_iso(s["created"])
        except (KeyError, ValueError):
            continue
        labels.append(dt.strftime("%d.%m."))
        values.append(round(s.get("chargedEnergy", 0.0) or 0.0, 1))

    # Bei vielen Ladevorgängen nur jedes n-te Datum beschriften, sonst
    # überlappen sich die Achsenbeschriftungen unleserlich.
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
) -> dict:
    """Baut das PDF und gibt eine kleine Zusammenfassung (Summen, Warnungen) zurück.

    ``tariff_periods`` ist die Tarifhistorie für Methode "actual": eine Liste von
    {"start_date": date, "price": float}. Pro Ladevorgang wird der zum jeweiligen
    Ladedatum gültige Satz angewandt (siehe ``resolve_tariff``) und als eigene
    Spalte in der Tabelle ausgewiesen. Für Methode "pauschale" wird sie ignoriert.

    ``footnote`` überschreibt den Standardtext unterhalb der Tabelle (z. B. um
    eine unternehmensspezifische Formulierung zu hinterlegen). Wird nichts
    übergeben oder ein leerer String, greift der automatisch aus der
    Berechnungsmethode abgeleitete Standardtext.

    ``include_chart`` steuert, ob das Verlaufsdiagramm mit ausgegeben wird
    (Standard: nein).
    """
    styles = _styles()
    period_label = f"{MONTH_NAMES_DE[month]} {year}"

    method_footnote = (
        "Berechnungsgrundlage: BMF-Schreiben vom 11.11.2025 (Strompreispauschale)"
        if method == "pauschale"
        else "Berechnungsgrundlage: nachgewiesener Haushaltstarif (BMF-Schreiben vom 11.11.2025)"
    )
    default_footnote_text = (
        f"{method_footnote}. Angaben auf Basis der Zählerdaten der Wallbox (evcc). "
        "Keine steuerliche Beratung."
    )

    # ---- Header/Footer, auf jeder Seite identisch -------------------------
    def draw_header_footer(cnv: pdfcanvas.Canvas, doc) -> None:
        page_w, page_h = A4
        cnv.saveState()

        # Kopfband
        cnv.setFillColor(NAVY)
        cnv.rect(0, page_h - HEADER_HEIGHT, page_w, HEADER_HEIGHT, stroke=0, fill=1)
        cnv.setFillColor(ACCENT)
        cnv.rect(0, page_h - HEADER_HEIGHT - 1.2 * mm, page_w, 1.2 * mm, stroke=0, fill=1)

        cnv.setFont(FONT_BOLD, 16)
        cnv.setFillColor(WHITE)
        cnv.drawString(PAGE_MARGIN, page_h - 15 * mm, "Ladekosten-Nachweis")

        cnv.setFont(FONT_REGULAR, 9.5)
        cnv.setFillColor(colors.HexColor("#C7D2FE"))
        cnv.drawString(PAGE_MARGIN, page_h - 21.5 * mm, "Laden des Dienstwagens am privaten Anschluss")

        cnv.setFont(FONT_BOLD, 12)
        cnv.setFillColor(WHITE)
        cnv.drawRightString(page_w - PAGE_MARGIN, page_h - 15 * mm, period_label)
        cnv.setFont(FONT_REGULAR, 8.5)
        cnv.setFillColor(colors.HexColor("#C7D2FE"))
        cnv.drawRightString(page_w - PAGE_MARGIN, page_h - 21.5 * mm, "Zeitraum")

        # Fußzeile
        cnv.setStrokeColor(GREY_LINE)
        cnv.setLineWidth(0.5)
        cnv.line(PAGE_MARGIN, FOOTER_HEIGHT, page_w - PAGE_MARGIN, FOOTER_HEIGHT)
        cnv.setFont(FONT_REGULAR, 7)
        cnv.setFillColor(GREY_TEXT)
        cnv.drawString(PAGE_MARGIN, FOOTER_HEIGHT - 5 * mm,
                        f"Erstellt am {datetime.now().strftime('%d.%m.%Y %H:%M')} · Datenquelle: evcc")
        cnv.drawRightString(page_w - PAGE_MARGIN, FOOTER_HEIGHT - 5 * mm,
                             f"Seite {doc.page}")
        cnv.restoreState()

    main_buffer = BytesIO()
    doc = SimpleDocTemplate(
        main_buffer,
        pagesize=A4,
        topMargin=HEADER_HEIGHT + 6 * mm,
        bottomMargin=FOOTER_HEIGHT + 4 * mm,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
        title=f"Ladekosten-Nachweis {month:02d}/{year}",
        author="evcc Ladekosten-Report",
    )

    story = []

    # ---- Sessions verarbeiten (vor dem Meta-Header, da Methode-Label und
    #      KPI-Kacheln von den Ergebnissen abhängen) -------------------------
    sessions_sorted = sorted(sessions, key=lambda s: s.get("created", ""))
    rows = []
    total_kwh = 0.0
    total_amount = 0.0
    warnings = []
    used_rates: set[float] = set()
    used_period_keys: set = set()  # Startdaten der tatsächlich herangezogenen Tarifperioden

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
            rate, used_period, tariff_warning = resolve_tariff(start_dt.date(), tariff_periods or [])
            if used_period is not None:
                used_period_keys.add(used_period["start_date"])
            if tariff_warning:
                warnings.append(f"{start_dt.strftime('%d.%m.%Y')}: {tariff_warning}")
        amount = compute_amount(energy, rate)
        used_rates.add(round(rate, 6))

        total_kwh += energy
        total_amount += amount

        issue = check_plausibility(s)
        if issue:
            warnings.append(f"{start_dt.strftime('%d.%m.%Y')}: {issue}")

        rows.append((start_dt, end_dt, meter_start, meter_stop, energy, amount, rate))

    # ---- Relevante Tarifperioden + deren Nachweis-PDFs (nur Methode "actual") --
    # Wichtig: Zuordnung über die tatsächlich verwendete Periode (Startdatum),
    # NICHT über den Preis - sonst würde eine zufällig preisgleiche, aber nie
    # herangezogene Periode fälschlich mit aufgeführt (z. B. ein alter
    # Sammel-Eintrag mit demselben Cent-Betrag wie der wirklich gültige).
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
                PdfReader(doc_path)  # nur zur Validierung, dass die Datei lesbar ist
            except Exception:
                warnings.append(
                    f"Tarifnachweis ab {p['start_date'].strftime('%d.%m.%Y')} konnte nicht gelesen "
                    "werden (Datei beschädigt oder kein gültiges PDF) und wurde nicht angehängt."
                )
                continue
            periods_with_docs.append(p)

    if method == "pauschale":
        method_label = f"Strompreispauschale · {rate_ct_per_kwh:.0f} ct/kWh"
    elif len(used_rates) == 1:
        rate_label = f"{next(iter(used_rates)):.4f}".replace(".", ",")
        method_label = f"Tatsächliche Kosten · {rate_label} €/kWh"
    elif len(used_rates) > 1:
        method_label = "Tatsächliche Kosten · mehrere Tarife im Zeitraum (siehe Tabelle)"
    else:
        method_label = "Tatsächliche Kosten"

    # ---- Meta-Zeile: Mitarbeiter / Fahrzeug / Methode ----------------------
    meta_table = Table(
        [[
            Paragraph("MITARBEITER/IN", styles["label"]),
            Paragraph("FAHRZEUG", styles["label"]),
            Paragraph("BERECHNUNGSMETHODE", styles["label"]),
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

    # ---- KPI-Kacheln --------------------------------------------------------
    kpi_row = Table(
        [[
            _kpi_card(str(len(rows)), "LADEVORGÄNGE", styles),
            _kpi_card(f"{total_kwh:.2f} kWh".replace(".", ","), "GELADENE ENERGIE", styles),
            _kpi_card(f"{total_amount:.2f} €".replace(".", ","), "ERSTATTUNGSBETRAG", styles, accent=True),
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

    # ---- Detailtabelle -------------------------------------------------------
    story.append(Paragraph("LADEVORGÄNGE IM ZEITRAUM", styles["section"]))
    story.append(Spacer(1, 3 * mm))

    header = [
        Paragraph("Datum", styles["cell_head"]),
        Paragraph("Beginn", styles["cell_head"]),
        Paragraph("Ende", styles["cell_head"]),
        Paragraph("Zähler Start (kWh)", styles["cell_head_num"]),
        Paragraph("Zähler Ende (kWh)", styles["cell_head_num"]),
        Paragraph("Geladen (kWh)", styles["cell_head_num"]),
    ]
    show_rate_column = method == "actual"
    if show_rate_column:
        header.append(Paragraph("Satz (€/kWh)", styles["cell_head_num"]))
    header.append(Paragraph("Betrag (EUR)", styles["cell_head_num"]))

    table_data = [header]
    overnight_present = False

    for start_dt, end_dt, meter_start, meter_stop, energy, amount, rate in rows:
        day_diff = (end_dt.date() - start_dt.date()).days
        overnight_present = overnight_present or day_diff > 0
        end_label = end_dt.strftime("%H:%M") + (f" +{day_diff}" if day_diff > 0 else "")
        row_cells = [
            Paragraph(start_dt.strftime("%d.%m.%Y"), styles["cell"]),
            Paragraph(start_dt.strftime("%H:%M"), styles["cell"]),
            Paragraph(end_label, styles["cell"]),
            Paragraph(f"{meter_start:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                      if meter_start is not None else "–", styles["cell_num"]),
            Paragraph(f"{meter_stop:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                      if meter_stop is not None else "–", styles["cell_num"]),
            Paragraph(f"{energy:.2f}".replace(".", ","), styles["cell_num"]),
        ]
        if show_rate_column:
            row_cells.append(Paragraph(f"{rate:.4f}".replace(".", ","), styles["cell_num"]))
        row_cells.append(Paragraph(f"{amount:.2f}".replace(".", ","), styles["cell_num"]))
        table_data.append(row_cells)

    if len(table_data) == 1:
        story.append(Paragraph(
            "Keine Ladevorgänge im gewählten Zeitraum an den konfigurierten Home-Ladepunkten gefunden.",
            styles["cell"],
        ))
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

        # Summenzeile als eigene, optisch abgesetzte Tabelle
        if show_rate_column:
            sum_table = Table(
                [[
                    Paragraph("Summe", styles["sum_label"]),
                    Paragraph(f"{total_kwh:.2f} kWh".replace(".", ","), styles["sum_value"]),
                    "",
                    Paragraph(f"{total_amount:.2f} €".replace(".", ","), styles["sum_value"]),
                ]],
                colWidths=[23 * mm + 16 * mm + 22 * mm + 21 * mm + 21 * mm, 30 * mm, 17 * mm, 24 * mm],
            )
        else:
            sum_table = Table(
                [[
                    Paragraph("Summe", styles["sum_label"]),
                    Paragraph(f"{total_kwh:.2f} kWh".replace(".", ","), styles["sum_value"]),
                    Paragraph(f"{total_amount:.2f} €".replace(".", ","), styles["sum_value"]),
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
            tariff_block = [Paragraph("IM ZEITRAUM VERWENDETE TARIFE", styles["section"]),
                             Spacer(1, 3 * mm)]

            tariff_header = [
                Paragraph("Gültig ab", styles["cell_head"]),
                Paragraph("Preis (€/kWh)", styles["cell_head_num"]),
                Paragraph("Beleg", styles["cell_head"]),
            ]
            tariff_rows = [tariff_header]
            for p in relevant_periods:
                has_doc = p in periods_with_docs
                tariff_rows.append([
                    Paragraph(p["start_date"].strftime("%d.%m.%Y"), styles["cell"]),
                    Paragraph(f"{p['price']:.4f}".replace(".", ","), styles["cell_num"]),
                    Paragraph("siehe Anlage" if has_doc else "–", styles["cell"]),
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
            story.append(Paragraph(
                "Im Zeitraum wurden mehrere Tarife angewandt (siehe Spalte \"Satz\").",
                styles["small"],
            ))

        if overnight_present:
            story.append(Spacer(1, 2 * mm))
            story.append(Paragraph(
                "+N = Ladevorgang endet N Tag(e) nach dem Beginndatum (z. B. +1 = am Folgetag, +2 = zwei Tage später)",
                styles["small"],
            ))

    # ---- Verlaufsdiagramm (optional, Standard: aus) ---------------------------
    if include_chart and len(rows) >= 2:
        story.append(Spacer(1, 5 * mm))
        story.append(KeepTogether([
            Paragraph("VERLAUF GELADENE ENERGIE JE LADEVORGANG", styles["section"]),
            Spacer(1, 3 * mm),
            _make_chart(sessions_sorted),
        ]))

    # ---- Hinweise / Plausibilität ---------------------------------------------
    if warnings:
        story.append(Spacer(1, 5 * mm))
        warn_lines = [Paragraph("Zu prüfende Abweichungen", styles["warn_head"])]
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

    # ---- Footnote & Unterschrift -----------------------------------------------
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph(
        footnote.strip() if footnote and footnote.strip() else default_footnote_text,
        styles["small"],
    ))
    if periods_with_docs:
        story.append(Spacer(1, 1.5 * mm))
        anzahl = len(periods_with_docs)
        text = (
            "Tarifnachweis als Anlage beigefügt (siehe Ende dieses Dokuments)."
            if anzahl == 1
            else f"{anzahl} Tarifnachweise als Anlagen beigefügt (siehe Ende dieses Dokuments)."
        )
        story.append(Paragraph(text, styles["small"]))
    story.append(Spacer(1, 9 * mm))

    sig_table = Table(
        [
            ["", ""],
            [HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#9CA3AF")),
             HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#9CA3AF"))],
            [Paragraph("Ort, Datum", styles["sig_label"]),
             Paragraph("Unterschrift Mitarbeiter/in", styles["sig_label"])],
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
            cover_bytes = _build_attachment_cover(p, period_label, p.get("document_original_name"))
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
