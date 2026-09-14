# evcc Charging Cost Report – Documentation

[🇬🇧 English](DOCS.md) | [🇩🇪 Deutsch](DOCS.de.md)

## What does this add-on do?

It retrieves a month's charging sessions from your evcc instance's REST API
(`/api/sessions`), filters them down to your configured vehicles, and
generates a PDF containing:

- date, start/end time per charging session
- meter reading start / end (kWh)
- energy charged
- calculated reimbursement amount (flat electricity rate or actual tariff)
- plausibility notes for deviations between the meter difference and the
  reported energy
- a signature field

Reports can be generated manually via the web interface (accessible in the
Home Assistant sidebar under "Charging Costs" via ingress), or automatically
on the 2nd of each month for the previous month (see the `auto_generate`
option).

In the list of existing reports, each report can be marked "Submitted" via
checkbox (e.g. once it has been handed in to the employer) and removed again
with the "Delete" button. Charging sessions that run past midnight (e.g.
5:28 PM to 5:00 AM the next day) are marked with "+1" in the End column
(or "+2", "+3", ... for a gap of more than one day).

For the "Actual cost" method, a **tariff history** is maintained directly on
the add-on page (see below).

All PDFs are also placed under `/share/evcc_ladekosten/`, so they're
reachable via the File Explorer / Samba add-on as well.

The interface follows the language selected in the `language` option
(English or German, see [Configuration](#configuration) below) and
automatically adapts to your browser's/operating system's light/dark setting
(`prefers-color-scheme`). Direct access to the theme selected in Home
Assistant is technically not possible from within an ingress iframe; the
system/browser preference is the best possible approximation and matches the
Home Assistant setting in most setups.

## Background: why meter readings matter

As of the BMF letter dated 2025-11-11 (effective 2026-01-01), the old
monthly flat rates for charging a company car at home no longer apply. A
tax-free reimbursement now requires proof of the actually charged kWh. Two
methods are permitted, but must be applied consistently per calendar year:

- **Flat electricity rate** (2026: €0.34/kWh) – a simple meter, not
  necessarily calibrated, is sufficient.
- **Actual cost** (your own household tariff) – for this, the BMF requires a
  calibration-law-compliant (MID-certified) meter.

This add-on does not replace tax advice. Please align the chosen method with
HR/payroll.

## Tariff history (method "Actual cost")

The add-on page has a "Tariff history" card where you can store any number
of periods with a start date and price (€/kWh). For each charging session,
the entry whose start date is closest to (but not after) the charging date
is automatically applied – a tariff change in the middle of the month
therefore correctly affects only the charging sessions after it.

Example: entries "2020-01-01 → €0.2614/kWh" and "2026-08-15 → €0.31/kWh"
mean that all charging sessions up to 2026-08-14 are calculated at
€0.2614/kWh, and from 2026-08-15 onward at €0.31/kWh.

The rate actually applied is shown for traceability as its own "Rate
(€/kWh)" column in the PDF table. If a charging session predates the oldest
stored period, that oldest rate is used as a fallback and noted in the PDF
as a plausibility note, instead of aborting report creation.

If multiple tariffs were applied within the report period, the PDF
additionally prints a small "Tariffs used in this period" table with exactly
the relevant entries (start date + price) – not the entire tariff history,
just what was actually used for this report. This works correctly even when
an older and a newer tariff entry happen to share the same price (e.g. an
unchanged price after switching providers): the entry actually applied is
shown, not every entry with a matching price.

**Receipts as attachments:** an optional PDF receipt can be uploaded per
tariff period (e.g. an energy contract or price adjustment letter). Once a
period with a receipt is relevant to a report, the receipt is automatically
attached to the generated PDF – regardless of whether a tariff change
occurred within the period or only a single tariff applied, and even with
several simultaneously relevant receipts (one per period, each with its own
divider page). A corrupted or invalid PDF is not attached, but noted as a
plausibility note in the report instead.

On the very first start, the add-on automatically creates a seed value
(2020-01-01, €0.2614/kWh) so nothing breaks before you've maintained your
own entries.

## Configuration

| Option | Description |
|---|---|
| `evcc_url` | Base URL of your evcc instance, e.g. `http://homeassistant.local:7070` |
| `vehicles` | List of vehicle titles (as named in evcc under `vehicles -> title`) whose charging sessions are included in the report |
| `method` | `pauschale` (flat rate) or `actual` |
| `rate_ct_per_kwh` | Cents/kWh for `method: pauschale` |
| `employee` | Default name for the report header |
| `vehicle` | Display text in the report header (e.g. "Seat, WI-XX 1234") – independent of the `vehicles` filter above |
| `language` | UI and PDF language: `en` (English, default) or `de` (German) |
| `auto_generate` | Automatic creation on the 2nd of each month for the previous month |
| `notify_on_generate` | Persistent notification in Home Assistant after each report |
| `footnote_pauschale` | Custom disclaimer text below the table for method `pauschale`. Leave empty for the default text (BMF reference). |
| `footnote_actual` | Custom disclaimer text below the table for method `actual`. Leave empty for the default text (BMF reference). |
| `include_chart` | Whether the trend chart is included in automatically generated reports. Default: off. Manually created reports have their own checkbox in the web interface (also off by default). |

Via the web interface you can also override month/year, method, employee and
vehicle for a single report without changing the configuration.

**Preconfigured defaults for this setup:** `evcc_url: http://evcc.local:7070`,
`vehicles: ["Seat"]` (the title of your vehicle in the evcc config). Adjust
this if your evcc config changes (e.g. a second vehicle or a vehicle
change).

Why vehicle instead of charge point? This way, charging sessions of the same
vehicle at different (home) charge points are also correctly captured, and
other vehicles that happen to charge at the same charge point are reliably
excluded.

## Known limitations

- The evcc API must be reachable from the add-on via HTTP (same home
  network or same host).
- For method `actual`, responsibility for a calibration-law-compliant meter
  and correct tariff maintenance lies with the user.
- The tariff history is stored in `/data/tariffs.json` in the add-on's
  persistent data storage (not in `/share`) and survives restarts and
  updates, but is not directly visible via the File Explorer – it is
  maintained exclusively via the web interface.
- Uploaded tariff receipts are correspondingly stored in
  `/data/tariff_docs/`, likewise persistent and not visible via `/share`.
