import json
import logging
import os
from datetime import datetime, date, timedelta

import requests
from flask import (
    Flask, request, render_template, send_from_directory,
    redirect, flash, get_flashed_messages,
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import report

logging.basicConfig(level=logging.INFO, format="[evcc-ladekosten] %(message)s")
log = logging.getLogger(__name__)

OPTIONS_PATH = "/data/options.json"
SHARE_DIR = "/share/evcc_ladekosten"
META_PATH = os.path.join(SHARE_DIR, ".reports_meta.json")
TARIFFS_PATH = "/data/tariffs.json"
TARIFF_DOCS_DIR = "/data/tariff_docs"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")

# Einmaliger Startwert, falls noch keine Tarifhistorie existiert (Migration
# vom frueheren statischen Konfigurationswert tarif_eur_per_kwh).
DEFAULT_TARIFF_SEED = [{"start_date": "2020-01-01", "price": 0.2614}]

DEFAULT_OPTIONS = {
    "evcc_url": "http://evcc.local:7070",
    "vehicles": ["Seat"],
    "method": "pauschale",
    "rate_ct_per_kwh": 34.0,
    "employee": "Jan",
    "vehicle": "Seat",
    "auto_generate": True,
    "notify_on_generate": True,
    "footnote_pauschale": "",
    "footnote_actual": (
        "Berechnungsgrundlage: individueller Haushaltstarif. Die geladene Energiemenge wurde "
        "mit dem fest an der Wallbox verbauten, MID-zertifizierten Zähler SDM630 (Modbus, MID V2) erfasst."
    ),
    "include_chart": False,
}


def load_options() -> dict:
    if os.path.exists(OPTIONS_PATH):
        try:
            with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
                opts = json.load(f)
            merged = {**DEFAULT_OPTIONS, **opts}
            return merged
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Konnte %s nicht lesen (%s), nutze Defaults.", OPTIONS_PATH, exc)
    return dict(DEFAULT_OPTIONS)


os.makedirs(SHARE_DIR, exist_ok=True)
os.makedirs(TARIFF_DOCS_DIR, exist_ok=True)
app = Flask(__name__)
# Nur fuer Flash-Messages zwischen POST (Redirect) und dem folgenden GET
# benoetigt - keine sicherheitskritischen Daten, daher reicht ein
# zufaelliger Key pro Prozessstart.
app.secret_key = os.urandom(24)


# ---------------------------------------------------------------------------
# Redirect-nach-POST (Post/Redirect/Get), ingress-sicher
# ---------------------------------------------------------------------------
# Home Assistant Ingress proxied ueber einen dynamischen, dem Add-on
# unbekannten Prefix. Absolute Redirects ("/") wuerden daher auf die
# HA-Wurzel statt auf die Add-on-Seite zeigen. Stattdessen wird relativ zur
# aktuellen Tiefe zurueck zur Wurzel navigiert (".." je verschachteltem
# Pfad-Segment) - das macht z.B. "/tariffs/add" (Tiefe 2) zu "../" und
# "/generate" (Tiefe 1) zu ".". So bleibt die Seite nach jeder Aktion
# konsistent auf der Wurzel, ohne dass sich relative Formular-Ziele bei
# mehrfachem Absenden aufaddieren koennen (das war die Ursache des
# "Not Found"-Fehlers beim zweiten Hinzufuegen ohne Neuladen).

def _redirect_to_root():
    depth = len([seg for seg in request.path.split("/") if seg])
    target = ("../" * (depth - 1)) if depth > 1 else "."
    # 303 See Other statt des Flask-Standards 302: erzwingt bei JEDEM Client
    # zuverlässig eine GET-Anfrage auf das Ziel (bei 302 behandeln manche
    # Clients - z. B. curl per Default - das Redirect-Ziel weiterhin als POST,
    # was hier zu einem 405 auf "/" führen würde).
    return redirect(target, code=303)


def _flash_and_redirect(message: str | None = None, error: str | None = None):
    if message:
        flash(message, "ok")
    if error:
        flash(error, "err")
    return _redirect_to_root()


# ---------------------------------------------------------------------------
# Report-Metadaten ("Eingereicht"-Status)
# ---------------------------------------------------------------------------

def load_meta() -> dict:
    """Lädt den 'Eingereicht'-Status je Report-Datei (kleines JSON-Sidecar-File)."""
    if os.path.exists(META_PATH):
        try:
            with open(META_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Konnte %s nicht lesen (%s), starte mit leeren Metadaten.", META_PATH, exc)
    return {}


def save_meta(meta: dict) -> None:
    try:
        with open(META_PATH, "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except OSError as exc:
        log.warning("Konnte %s nicht schreiben (%s).", META_PATH, exc)


def _safe_report_path(filename: str) -> str | None:
    """Verhindert Path-Traversal: nur echte, existierende PDF-Dateien direkt in SHARE_DIR."""
    if not filename or "/" in filename or "\\" in filename or not filename.endswith(".pdf"):
        return None
    full = os.path.join(SHARE_DIR, filename)
    if os.path.commonpath([os.path.abspath(full), os.path.abspath(SHARE_DIR)]) != os.path.abspath(SHARE_DIR):
        return None
    return full if os.path.isfile(full) else None


def list_reports() -> list[dict]:
    meta = load_meta()
    files = []
    if os.path.isdir(SHARE_DIR):
        for name in sorted(os.listdir(SHARE_DIR), reverse=True):
            if name.endswith(".pdf"):
                full = os.path.join(SHARE_DIR, name)
                files.append({
                    "name": name,
                    "size_kb": round(os.path.getsize(full) / 1024, 1),
                    "modified": datetime.fromtimestamp(os.path.getmtime(full)).strftime("%d.%m.%Y %H:%M"),
                    "submitted": bool(meta.get(name, {}).get("submitted", False)),
                })
    return files


# ---------------------------------------------------------------------------
# Tarifhistorie (Methode "Tatsächliche Kosten") + optionale Belege (PDF)
# ---------------------------------------------------------------------------

def _valid_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False


def load_tariffs_raw() -> list[dict]:
    """Liest die Tarifhistorie als rohe JSON-Liste
    ({"start_date": "YYYY-MM-DD", "price": float, "document_name": str|None}),
    sortiert nach Startdatum aufsteigend. Legt beim allerersten Start einen Seed-Eintrag an."""
    if not os.path.exists(TARIFFS_PATH):
        save_tariffs_raw(DEFAULT_TARIFF_SEED)
        return list(DEFAULT_TARIFF_SEED)
    try:
        with open(TARIFFS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("Tarifdatei enthält keine Liste")
        return sorted(data, key=lambda t: t["start_date"])
    except (json.JSONDecodeError, OSError, ValueError, KeyError) as exc:
        log.warning("Konnte %s nicht lesen (%s), nutze Seed-Werte.", TARIFFS_PATH, exc)
        return list(DEFAULT_TARIFF_SEED)


def save_tariffs_raw(tariffs: list[dict]) -> None:
    try:
        with open(TARIFFS_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(tariffs, key=lambda t: t["start_date"]), f)
    except OSError as exc:
        log.warning("Konnte %s nicht schreiben (%s).", TARIFFS_PATH, exc)


def tariff_doc_path(start_date_iso: str) -> str | None:
    """Pfad zum hinterlegten Beleg-PDF fuer ein Startdatum, oder None wenn ungueltig.
    Der Dateiname basiert ausschliesslich auf dem validierten ISO-Datum (YYYY-MM-DD),
    daher ist kein Path-Traversal ueber diesen Wert moeglich."""
    if not _valid_iso_date(start_date_iso):
        return None
    return os.path.join(TARIFF_DOCS_DIR, f"{start_date_iso}.pdf")


def save_tariff_doc(start_date_iso: str, file_storage) -> None:
    path = tariff_doc_path(start_date_iso)
    if path:
        file_storage.save(path)


def delete_tariff_doc(start_date_iso: str) -> None:
    path = tariff_doc_path(start_date_iso)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as exc:
            log.warning("Konnte Tarifbeleg %s nicht löschen (%s).", path, exc)


def tariffs_for_display() -> list[dict]:
    """Tarifhistorie fürs Template: Anzeige-Strings vorformatiert + Beleg-Status."""
    result = []
    for t in load_tariffs_raw():
        try:
            d = date.fromisoformat(t["start_date"])
        except ValueError:
            continue
        doc_path = tariff_doc_path(t["start_date"])
        result.append({
            "start_date_iso": t["start_date"],
            "start_date_display": d.strftime("%d.%m.%Y"),
            "price": t["price"],
            "price_display": f"{float(t['price']):.4f}".replace(".", ","),
            "has_document": bool(doc_path and os.path.exists(doc_path)),
            "document_name": t.get("document_name") or "Beleg.pdf",
        })
    return result


def tariffs_for_calculation() -> list[dict]:
    """Tarifhistorie für report.build_pdf: start_date als date-Objekt + Beleg-Pfad."""
    result = []
    for t in load_tariffs_raw():
        try:
            start = date.fromisoformat(t["start_date"])
        except (ValueError, TypeError):
            continue
        doc_path = tariff_doc_path(t["start_date"])
        has_doc = bool(doc_path and os.path.exists(doc_path))
        result.append({
            "start_date": start,
            "price": float(t["price"]),
            "document_path": doc_path if has_doc else None,
            "document_original_name": t.get("document_name") if has_doc else None,
        })
    return result


def notify_home_assistant(title: str, message: str) -> None:
    if not SUPERVISOR_TOKEN:
        return
    try:
        requests.post(
            "http://supervisor/core/api/services/persistent_notification/create",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            json={"title": title, "message": message, "notification_id": "evcc_ladekosten"},
            timeout=5,
        )
    except requests.RequestException as exc:
        log.warning("Benachrichtigung an Home Assistant fehlgeschlagen: %s", exc)


def generate_report(month: int, year: int, overrides: dict | None = None) -> dict:
    opts = load_options()
    if overrides:
        opts.update({k: v for k, v in overrides.items() if v not in (None, "")})

    sessions = report.fetch_sessions(opts["evcc_url"], month, year)
    sessions = report.filter_by_vehicle(sessions, opts.get("vehicles") or [])

    filename = f"ladekosten_{year}_{month:02d}.pdf"
    out_path = os.path.join(SHARE_DIR, filename)

    footnote = opts.get("footnote_pauschale") if opts["method"] == "pauschale" else opts.get("footnote_actual")

    summary = report.build_pdf(
        sessions=sessions,
        out_path=out_path,
        month=month,
        year=year,
        method=opts["method"],
        rate_ct_per_kwh=float(opts["rate_ct_per_kwh"]),
        employee=opts.get("employee", ""),
        vehicle=opts.get("vehicle", ""),
        tariff_periods=tariffs_for_calculation(),
        footnote=footnote,
        include_chart=bool(opts.get("include_chart", False)),
    )
    summary["filename"] = filename
    log.info("Report erstellt: %s (%s Ladevorgänge, %.2f kWh, %.2f EUR, %s Anlage(n))",
              filename, summary["sessions"], summary["total_kwh"], summary["total_amount"],
              summary.get("attached_documents", 0))

    if opts.get("notify_on_generate"):
        notify_home_assistant(
            "evcc Ladekosten-Report erstellt",
            f"{filename}: {summary['sessions']} Ladevorgänge, "
            f"{summary['total_kwh']} kWh, {summary['total_amount']} EUR. "
            "Abrufbar über die Add-on-Oberfläche oder /share/evcc_ladekosten.",
        )
    return summary


def scheduled_job() -> None:
    """Erzeugt automatisch den Report für den abgelaufenen Vormonat."""
    opts = load_options()
    if not opts.get("auto_generate", True):
        return
    today = datetime.now()
    first_of_this_month = today.replace(day=1)
    last_month_end = first_of_this_month - timedelta(days=1)
    try:
        generate_report(last_month_end.month, last_month_end.year)
    except Exception as exc:  # noqa: BLE001 - Job soll den Scheduler nicht crashen
        log.error("Automatische Report-Erstellung fehlgeschlagen: %s", exc)
        notify_home_assistant(
            "evcc Ladekosten-Report: Fehler",
            f"Automatische Erstellung für {last_month_end.month:02d}/{last_month_end.year} "
            f"fehlgeschlagen: {exc}",
        )


def _render_index():
    now = datetime.now()
    prev_month = (now.replace(day=1) - timedelta(days=1))
    flashed = get_flashed_messages(with_categories=True)
    message = next((m for cat, m in flashed if cat == "ok"), None)
    error = next((m for cat, m in flashed if cat == "err"), None)
    return render_template(
        "index.html",
        reports=list_reports(),
        tariffs=tariffs_for_display(),
        default_month=prev_month.month,
        default_year=prev_month.year,
        opts=load_options(),
        message=message,
        error=error,
    )


@app.route("/", methods=["GET"])
def index():
    return _render_index()


@app.route("/generate", methods=["POST"])
def generate():
    opts = load_options()
    message = None
    error = None
    try:
        now = datetime.now()
        prev_month = (now.replace(day=1) - timedelta(days=1))
        month = int(request.form.get("month", prev_month.month))
        year = int(request.form.get("year", prev_month.year))
        overrides = {
            "employee": request.form.get("employee") or opts.get("employee", ""),
            "vehicle": request.form.get("vehicle") or opts.get("vehicle", ""),
            "method": request.form.get("method") or opts.get("method"),
            "include_chart": request.form.get("include_chart") == "on",
        }
        summary = generate_report(month, year, overrides)
        if summary["sessions"] == 0:
            message = f"Keine Ladevorgänge für {month:02d}/{year} für die konfigurierten Fahrzeuge gefunden."
        else:
            message = (
                f"Report für {month:02d}/{year} erstellt: {summary['sessions']} Ladevorgänge, "
                f"{summary['total_kwh']} kWh, {summary['total_amount']} EUR."
            )
            if summary.get("attached_documents"):
                message += f" {summary['attached_documents']} Tarifnachweis(e) angehängt."
    except requests.RequestException as exc:
        error = f"evcc-API nicht erreichbar unter {opts['evcc_url']}: {exc}"
    except Exception as exc:  # noqa: BLE001
        error = f"Fehler bei der Report-Erstellung: {exc}"

    return _flash_and_redirect(message=message, error=error)


@app.route("/download/<path:filename>", methods=["GET"])
def download(filename):
    return send_from_directory(SHARE_DIR, filename, as_attachment=True)


@app.route("/delete", methods=["POST"])
def delete():
    message = None
    error = None
    filename = request.form.get("filename", "")
    full = _safe_report_path(filename)
    if not full:
        error = f"Datei nicht gefunden oder ungültig: {filename}"
    else:
        try:
            os.remove(full)
            meta = load_meta()
            meta.pop(filename, None)
            save_meta(meta)
            message = f"Report {filename} gelöscht."
        except OSError as exc:
            error = f"Löschen fehlgeschlagen: {exc}"
    return _flash_and_redirect(message=message, error=error)


@app.route("/toggle_submitted", methods=["POST"])
def toggle_submitted():
    filename = request.form.get("filename", "")
    full = _safe_report_path(filename)
    if full:
        meta = load_meta()
        current = bool(meta.get(filename, {}).get("submitted", False))
        meta[filename] = {"submitted": not current}
        save_meta(meta)
    return _flash_and_redirect()


@app.route("/tariffs/add", methods=["POST"])
def tariffs_add():
    message = None
    error = None
    raw_date = request.form.get("start_date", "").strip()
    raw_price = request.form.get("price", "").strip().replace(",", ".")
    uploaded = request.files.get("document")

    try:
        parsed_date = date.fromisoformat(raw_date)
        parsed_price = float(raw_price)
        if parsed_price < 0:
            raise ValueError("Preis darf nicht negativ sein")

        if uploaded and uploaded.filename:
            if not uploaded.filename.lower().endswith(".pdf"):
                raise ValueError("Beleg muss eine PDF-Datei sein")

        tariffs = load_tariffs_raw()
        existing = next((t for t in tariffs if t["start_date"] == raw_date), None)
        entry = {"start_date": raw_date, "price": parsed_price}
        # Bestehenden Dateinamen nur beibehalten, wenn kein neuer Beleg hochgeladen wurde
        if existing and existing.get("document_name") and not (uploaded and uploaded.filename):
            entry["document_name"] = existing["document_name"]

        tariffs = [t for t in tariffs if t["start_date"] != raw_date]
        tariffs.append(entry)
        save_tariffs_raw(tariffs)

        if uploaded and uploaded.filename:
            save_tariff_doc(raw_date, uploaded)
            tariffs = load_tariffs_raw()
            for t in tariffs:
                if t["start_date"] == raw_date:
                    t["document_name"] = uploaded.filename
            save_tariffs_raw(tariffs)

        price_label = f"{parsed_price:.4f}".replace(".", ",")
        message = f"Tarif ab {parsed_date.strftime('%d.%m.%Y')} gespeichert: {price_label} €/kWh."
        if uploaded and uploaded.filename:
            message += " Beleg hochgeladen."
    except ValueError as exc:
        error = f"Ungültige Eingabe (Datum: '{raw_date}', Preis: '{raw_price}'): {exc}"

    return _flash_and_redirect(message=message, error=error)


@app.route("/tariffs/delete", methods=["POST"])
def tariffs_delete():
    start_date = request.form.get("start_date", "")
    tariffs = load_tariffs_raw()
    remaining = [t for t in tariffs if t["start_date"] != start_date]
    if len(remaining) == len(tariffs):
        return _flash_and_redirect(error=f"Tarifeintrag ab {start_date} nicht gefunden.")
    save_tariffs_raw(remaining)
    delete_tariff_doc(start_date)
    return _flash_and_redirect(message=f"Tarifeintrag ab {start_date} gelöscht.")


@app.route("/tariffs/delete_doc", methods=["POST"])
def tariffs_delete_doc():
    start_date = request.form.get("start_date", "")
    delete_tariff_doc(start_date)
    tariffs = load_tariffs_raw()
    for t in tariffs:
        if t["start_date"] == start_date:
            t.pop("document_name", None)
    save_tariffs_raw(tariffs)
    return _flash_and_redirect(message=f"Beleg für Tarif ab {start_date} entfernt.")


@app.route("/tariffs/doc/<start_date>", methods=["GET"])
def tariffs_doc(start_date):
    path = tariff_doc_path(start_date)
    if not path or not os.path.exists(path):
        return ("Beleg nicht gefunden", 404)
    return send_from_directory(TARIFF_DOCS_DIR, os.path.basename(path))


if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    # Am 2. jeden Monats um 06:00 Uhr den Vormonat automatisch erzeugen
    scheduler.add_job(scheduled_job, CronTrigger(day=2, hour=6, minute=0))
    scheduler.start()
    log.info("evcc Ladekosten-Report Add-on gestartet (Ingress-Port 8099)")
    app.run(host="0.0.0.0", port=8099)
