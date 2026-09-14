"""
Minimal i18n helper for the app.

English (`locales/en.yaml`) is the source of truth: every key must exist
there. Other locales (currently just `de.yaml`) only need to override the
keys they translate - missing keys fall back to English automatically, so a
partial translation never breaks rendering.
"""

import os

import yaml

DEFAULT_LOCALE = "en"
SUPPORTED_LOCALES = ("en", "de")

_LOCALES_DIR = os.path.join(os.path.dirname(__file__), "locales")
_cache: dict[str, dict] = {}


def _load(locale: str) -> dict:
    if locale not in _cache:
        path = os.path.join(_LOCALES_DIR, f"{locale}.yaml")
        with open(path, "r", encoding="utf-8") as f:
            _cache[locale] = yaml.safe_load(f) or {}
    return _cache[locale]


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_locale(locale: str | None) -> str:
    return locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE


def strings(locale: str | None) -> dict:
    """Full nested translation dict for `locale`, with English as the fallback
    for any key the translation doesn't override."""
    locale = normalize_locale(locale)
    base = _load(DEFAULT_LOCALE)
    if locale == DEFAULT_LOCALE:
        return base
    return _deep_merge(base, _load(locale))


def _lookup(data: dict, dotted_key: str):
    node = data
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def t(locale: str | None, key: str, **kwargs) -> str:
    """Look up a dotted translation key (e.g. 'messages.report_created') and
    format it with `kwargs`. Falls back to English, then to the key itself."""
    value = _lookup(strings(locale), key)
    if value is None:
        return key
    return value.format(**kwargs) if kwargs else value


# ---------------------------------------------------------------------------
# Locale-aware number/date formatting
# ---------------------------------------------------------------------------

def fmt_number(value: float, decimals: int, locale: str | None) -> str:
    locale = normalize_locale(locale)
    text = f"{value:,.{decimals}f}"
    if locale == "de":
        text = text.replace(",", "X").replace(".", ",").replace("X", ".")
    return text


def fmt_currency(value: float, locale: str | None) -> str:
    return f"{fmt_number(value, 2, locale)} €"


def fmt_date(d, locale: str | None) -> str:
    locale = normalize_locale(locale)
    return d.strftime("%d.%m.%Y") if locale == "de" else d.strftime("%m/%d/%Y")


def fmt_datetime(d, locale: str | None) -> str:
    locale = normalize_locale(locale)
    return d.strftime("%d.%m.%Y %H:%M") if locale == "de" else d.strftime("%m/%d/%Y %H:%M")


def fmt_time(d) -> str:
    return d.strftime("%H:%M")
