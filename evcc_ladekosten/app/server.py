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

import i18n
import report

logging.basicConfig(level=logging.INFO, format="[evcc-ladekosten] %(message)s")
log = logging.getLogger(__name__)

OPTIONS_PATH = "/data/options.json"
SHARE_DIR = "/share/evcc_ladekosten"
META_PATH = os.path.join(SHARE_DIR, ".reports_meta.json")
TARIFFS_PATH = "/data/tariffs.json"
TARIFF_DOCS_DIR = "/data/tariff_docs"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")

# One-time seed value if no tariff history exists yet (migration from the
# former static configuration value tarif_eur_per_kwh).
DEFAULT_TARIFF_SEED = [{"start_date": "2020-01-01", "price": 0.2614}]

DEFAULT_OPTIONS = {
    "evcc_url": "http://homeassistant.local:7070",
    "vehicles": [],
    "method": "pauschale",
    "rate_ct_per_kwh": 0.0,
    "employee": "",
    "vehicle": "",
    "language": "en",
    "auto_generate": True,
    "notify_on_generate": True,
    "footnote_pauschale": "",
    "footnote_actual": "Basis of calculation: individual household tariff.",
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
            log.warning("Could not read %s (%s), using defaults.", OPTIONS_PATH, exc)
    return dict(DEFAULT_OPTIONS)


def get_locale(opts: dict | None = None) -> str:
    return i18n.normalize_locale((opts or load_options()).get("language"))


os.makedirs(SHARE_DIR, exist_ok=True)
os.makedirs(TARIFF_DOCS_DIR, exist_ok=True)
app = Flask(__name__)
# Only needed for flash messages between a POST (redirect) and the following
# GET - no security-sensitive data, so a random key per process start suffices.
app.secret_key = os.urandom(24)


# ---------------------------------------------------------------------------
# Redirect-after-POST (Post/Redirect/Get), ingress-safe
# ---------------------------------------------------------------------------
# Home Assistant Ingress proxies through a dynamic prefix unknown to the
# add-on. Absolute redirects ("/") would therefore point at the HA root
# instead of the add-on page. Instead we navigate back to the root relative
# to the current depth (".." per nested path segment) - this turns e.g.
# "/tariffs/add" (depth 2) into "../" and "/generate" (depth 1) into ".".
# This keeps the page consistently on the root after every action, without
# relative form targets stacking up on repeated submits (that was the cause
# of the "Not Found" error on the second add without a reload).

def _redirect_to_root():
    depth = len([seg for seg in request.path.split("/") if seg])
    target = ("../" * (depth - 1)) if depth > 1 else "."
    # 303 See Other instead of Flask's default 302: reliably forces a GET
    # request on the target for EVERY client (with 302 some clients - e.g.
    # curl by default - still treat the redirect target as a POST, which
    # would result in a 405 on "/" here).
    return redirect(target, code=303)


def _flash_and_redirect(message: str | None = None, error: str | None = None):
    if message:
        flash(message, "ok")
    if error:
        flash(error, "err")
    return _redirect_to_root()


# ---------------------------------------------------------------------------
# Report metadata ("submitted" status)
# ---------------------------------------------------------------------------

def load_meta() -> dict:
    """Loads the "submitted" status per report file (a small JSON sidecar file)."""
    if os.path.exists(META_PATH):
        try:
            with open(META_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read %s (%s), starting with empty metadata.", META_PATH, exc)
    return {}


def save_meta(meta: dict) -> None:
    try:
        with open(META_PATH, "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except OSError as exc:
        log.warning("Could not write %s (%s).", META_PATH, exc)


def _safe_report_path(filename: str) -> str | None:
    """Prevents path traversal: only real, existing PDF files directly in SHARE_DIR."""
    if not filename or "/" in filename or "\\" in filename or not filename.endswith(".pdf"):
        return None
    full = os.path.join(SHARE_DIR, filename)
    if os.path.commonpath([os.path.abspath(full), os.path.abspath(SHARE_DIR)]) != os.path.abspath(SHARE_DIR):
        return None
    return full if os.path.isfile(full) else None


def list_reports(locale: str) -> list[dict]:
    meta = load_meta()
    files = []
    if os.path.isdir(SHARE_DIR):
        for name in sorted(os.listdir(SHARE_DIR), reverse=True):
            if name.endswith(".pdf"):
                full = os.path.join(SHARE_DIR, name)
                files.append({
                    "name": name,
                    "size_kb": round(os.path.getsize(full) / 1024, 1),
                    "modified": i18n.fmt_datetime(datetime.fromtimestamp(os.path.getmtime(full)), locale),
                    "submitted": bool(meta.get(name, {}).get("submitted", False)),
                })
    return files


# ---------------------------------------------------------------------------
# Tariff history (method "actual cost") + optional receipts (PDF)
# ---------------------------------------------------------------------------

def _valid_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False


def load_tariffs_raw() -> list[dict]:
    """Reads the tariff history as a raw JSON list
    ({"start_date": "YYYY-MM-DD", "price": float, "document_name": str|None}),
    sorted by start date ascending. Creates a seed entry on the very first start."""
    if not os.path.exists(TARIFFS_PATH):
        save_tariffs_raw(DEFAULT_TARIFF_SEED)
        return list(DEFAULT_TARIFF_SEED)
    try:
        with open(TARIFFS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("Tariff file does not contain a list")
        return sorted(data, key=lambda t: t["start_date"])
    except (json.JSONDecodeError, OSError, ValueError, KeyError) as exc:
        log.warning("Could not read %s (%s), using seed values.", TARIFFS_PATH, exc)
        return list(DEFAULT_TARIFF_SEED)


def save_tariffs_raw(tariffs: list[dict]) -> None:
    try:
        with open(TARIFFS_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(tariffs, key=lambda t: t["start_date"]), f)
    except OSError as exc:
        log.warning("Could not write %s (%s).", TARIFFS_PATH, exc)


def tariff_doc_path(start_date_iso: str) -> str | None:
    """Path to the stored receipt PDF for a start date, or None if invalid.
    The filename is based solely on the validated ISO date (YYYY-MM-DD),
    so path traversal via this value is not possible."""
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
            log.warning("Could not delete tariff receipt %s (%s).", path, exc)


def tariffs_for_display(locale: str) -> list[dict]:
    """Tariff history for the template: pre-formatted display strings + receipt status."""
    result = []
    for t in load_tariffs_raw():
        try:
            d = date.fromisoformat(t["start_date"])
        except ValueError:
            continue
        doc_path = tariff_doc_path(t["start_date"])
        result.append({
            "start_date_iso": t["start_date"],
            "start_date_display": i18n.fmt_date(d, locale),
            "price": t["price"],
            "price_display": i18n.fmt_number(float(t["price"]), 4, locale),
            "has_document": bool(doc_path and os.path.exists(doc_path)),
            "document_name": t.get("document_name") or i18n.t(locale, "messages.default_document_name"),
        })
    return result


def tariffs_for_calculation() -> list[dict]:
    """Tariff history for report.build_pdf: start_date as a date object + receipt path."""
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
        log.warning("Notification to Home Assistant failed: %s", exc)


def _resolve_report_filename(month: int, year: int) -> str:
    """Picks the filename to write the report to.

    The regular filename is reused as long as it doesn't exist yet, or
    exists but isn't marked as submitted (in which case it's fine to
    overwrite it, same as before). If it exists and is marked submitted, a
    submitted report must never be overwritten - a "_X" suffix is appended
    instead, starting at 2 and counting up until a free/non-submitted name
    is found.
    """
    meta = load_meta()
    base = f"ladekosten_{year}_{month:02d}"
    candidate = f"{base}.pdf"
    suffix = 2
    while (
        os.path.exists(os.path.join(SHARE_DIR, candidate))
        and meta.get(candidate, {}).get("submitted")
    ):
        candidate = f"{base}_{suffix}.pdf"
        suffix += 1
    return candidate


def generate_report(month: int, year: int, overrides: dict | None = None) -> dict:
    opts = load_options()
    if overrides:
        opts.update({k: v for k, v in overrides.items() if v not in (None, "")})
    locale = get_locale(opts)

    sessions = report.fetch_sessions(opts["evcc_url"], month, year, locale)
    sessions = report.filter_by_vehicle(sessions, opts.get("vehicles") or [])

    filename = _resolve_report_filename(month, year)
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
        locale=locale,
    )
    summary["filename"] = filename
    log.info("Report created: %s (%s sessions, %.2f kWh, %.2f EUR, %s attachment(s))",
              filename, summary["sessions"], summary["total_kwh"], summary["total_amount"],
              summary.get("attached_documents", 0))

    if opts.get("notify_on_generate"):
        notify_home_assistant(
            i18n.t(locale, "notify.created_title"),
            i18n.t(
                locale, "notify.created_body",
                filename=filename, sessions=summary["sessions"],
                kwh=summary["total_kwh"], amount=summary["total_amount"],
            ),
        )
    return summary


def scheduled_job() -> None:
    """Automatically creates the report for the month that just ended."""
    opts = load_options()
    if not opts.get("auto_generate", True):
        return
    locale = get_locale(opts)
    today = datetime.now()
    first_of_this_month = today.replace(day=1)
    last_month_end = first_of_this_month - timedelta(days=1)
    try:
        generate_report(last_month_end.month, last_month_end.year)
    except Exception as exc:  # noqa: BLE001 - job must not crash the scheduler
        log.error("Automatic report creation failed: %s", exc)
        notify_home_assistant(
            i18n.t(locale, "notify.error_title"),
            i18n.t(locale, "notify.error_body", month=last_month_end.month, year=last_month_end.year, error=exc),
        )


def _render_index():
    opts = load_options()
    locale = get_locale(opts)
    now = datetime.now()
    prev_month = (now.replace(day=1) - timedelta(days=1))
    flashed = get_flashed_messages(with_categories=True)
    message = next((m for cat, m in flashed if cat == "ok"), None)
    error = next((m for cat, m in flashed if cat == "err"), None)
    return render_template(
        "index.html",
        reports=list_reports(locale),
        tariffs=tariffs_for_display(locale),
        default_month=prev_month.month,
        default_year=prev_month.year,
        opts=opts,
        message=message,
        error=error,
        t=i18n.strings(locale),
        locale=locale,
    )


@app.route("/", methods=["GET"])
def index():
    return _render_index()


@app.route("/generate", methods=["POST"])
def generate():
    opts = load_options()
    locale = get_locale(opts)
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
            message = i18n.t(locale, "messages.no_sessions", month=month, year=year)
        else:
            message = i18n.t(
                locale, "messages.report_created",
                month=month, year=year, sessions=summary["sessions"],
                kwh=summary["total_kwh"], amount=summary["total_amount"],
            )
            if summary.get("attached_documents"):
                message += i18n.t(locale, "messages.attached_docs", count=summary["attached_documents"])
    except requests.RequestException as exc:
        error = i18n.t(locale, "messages.api_unreachable", url=opts["evcc_url"], error=exc)
    except Exception as exc:  # noqa: BLE001
        error = i18n.t(locale, "messages.generation_failed", error=exc)

    return _flash_and_redirect(message=message, error=error)


@app.route("/download/<path:filename>", methods=["GET"])
def download(filename):
    return send_from_directory(SHARE_DIR, filename, as_attachment=True)


@app.route("/delete", methods=["POST"])
def delete():
    locale = get_locale()
    message = None
    error = None
    filename = request.form.get("filename", "")
    full = _safe_report_path(filename)
    if not full:
        error = i18n.t(locale, "messages.file_not_found", filename=filename)
    else:
        meta = load_meta()
        if meta.get(filename, {}).get("submitted"):
            error = i18n.t(locale, "messages.delete_blocked_submitted", filename=filename)
        else:
            try:
                os.remove(full)
                meta.pop(filename, None)
                save_meta(meta)
                message = i18n.t(locale, "messages.report_deleted", filename=filename)
            except OSError as exc:
                error = i18n.t(locale, "messages.delete_failed", error=exc)
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
    locale = get_locale()
    message = None
    error = None
    raw_date = request.form.get("start_date", "").strip()
    raw_price = request.form.get("price", "").strip().replace(",", ".")
    uploaded = request.files.get("document")

    try:
        parsed_date = date.fromisoformat(raw_date)
        parsed_price = float(raw_price)
        if parsed_price < 0:
            raise ValueError(i18n.t(locale, "messages.price_negative"))

        if uploaded and uploaded.filename:
            if not uploaded.filename.lower().endswith(".pdf"):
                raise ValueError(i18n.t(locale, "messages.receipt_must_be_pdf"))

        tariffs = load_tariffs_raw()
        existing = next((t for t in tariffs if t["start_date"] == raw_date), None)
        entry = {"start_date": raw_date, "price": parsed_price}
        # Keep the existing filename only if no new receipt was uploaded
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

        price_label = i18n.fmt_number(parsed_price, 4, locale)
        message = i18n.t(
            locale, "messages.tariff_saved",
            date=i18n.fmt_date(parsed_date, locale), price=price_label,
        )
        if uploaded and uploaded.filename:
            message += i18n.t(locale, "messages.tariff_doc_uploaded")
    except ValueError as exc:
        error = i18n.t(locale, "messages.invalid_input", date=raw_date, price=raw_price, error=exc)

    return _flash_and_redirect(message=message, error=error)


@app.route("/tariffs/delete", methods=["POST"])
def tariffs_delete():
    locale = get_locale()
    start_date = request.form.get("start_date", "")
    tariffs = load_tariffs_raw()
    remaining = [t for t in tariffs if t["start_date"] != start_date]
    if len(remaining) == len(tariffs):
        return _flash_and_redirect(error=i18n.t(locale, "messages.tariff_not_found", date=start_date))
    save_tariffs_raw(remaining)
    delete_tariff_doc(start_date)
    return _flash_and_redirect(message=i18n.t(locale, "messages.tariff_deleted", date=start_date))


@app.route("/tariffs/delete_doc", methods=["POST"])
def tariffs_delete_doc():
    locale = get_locale()
    start_date = request.form.get("start_date", "")
    delete_tariff_doc(start_date)
    tariffs = load_tariffs_raw()
    for t in tariffs:
        if t["start_date"] == start_date:
            t.pop("document_name", None)
    save_tariffs_raw(tariffs)
    return _flash_and_redirect(message=i18n.t(locale, "messages.tariff_doc_removed", date=start_date))


@app.route("/tariffs/doc/<start_date>", methods=["GET"])
def tariffs_doc(start_date):
    path = tariff_doc_path(start_date)
    if not path or not os.path.exists(path):
        return (i18n.t(get_locale(), "messages.tariff_doc_not_found"), 404)
    return send_from_directory(TARIFF_DOCS_DIR, os.path.basename(path))


if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    # On the 2nd of each month at 06:00, automatically generate last month's report
    scheduler.add_job(scheduled_job, CronTrigger(day=2, hour=6, minute=0))
    scheduler.start()
    log.info("evcc Charging Cost Report add-on started (ingress port 8099)")
    app.run(host="0.0.0.0", port=8099)
