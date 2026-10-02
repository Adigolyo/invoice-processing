"""Date normalisation to the ledger's YYYY.MM.DD format (USR-002-02).

Supported inputs (whitespace and case are ignored, a trailing ``.`` is allowed):

- **Year-first numeric**, any origin: ``2024.03.05.``, ``2024. 03. 05.``, ``2024-03-05``,
  ``2024/3/5``. This is the Hungarian native format and ISO 8601; it has one reading.
- **Month names** (English or Hungarian, full or abbreviated, accents optional), any
  origin: ``2024. március 5.``, ``2024. márc. 5.``, ``March 14, 2024``,
  ``14th March 2024``. The month is spelled out, so there is one reading.
- **Day/month-order numeric** ``NN/NN/YYYY`` (``/``, ``.`` or ``-``): the rule comes from
  the invoice's origin: US reads MM/DD/YYYY, other-foreign reads DD/MM/YYYY. Domestic
  invoices use year-first dates, so this pattern on a domestic invoice is flagged, and
  with an unknown origin it is always flagged, even when only one reading is valid.

A trailing time of day (``2026.09.23 08:03:15``, ``2026-09-23T08:03``) is dropped
before parsing: receipts print the moment of sale, and only the day matters here.

Everything else (two-digit years, impossible calendar days, invalid times, mixed
separators, free text) returns a ``NormalizationFailure``.

``normalize_dates`` applies one fallback: a due date or performance date that was not
printed at all takes the issue date. Receipts paid on the spot print neither (project
owner decision, 2026-10-03). A printed but unparseable date is still flagged, never
replaced.
"""

from __future__ import annotations

import dataclasses
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Final

from intake.models import InvoiceExtraction
from intake.normalization.country_of_origin import Origin
from intake.normalization.result import NormalizationFailure

DATE_FIELDS: Final = ("issue_date", "due_date", "supply_date", "performance_date")

_MIN_YEAR: Final = 1900
_MAX_YEAR: Final = 2099

_SEP = r"\s*([./-])\s*"
_YEAR_FIRST = re.compile(rf"^(\d{{4}}){_SEP}(\d{{1,2}})\s*\2\s*(\d{{1,2}})\.?$")
_DAY_MONTH_ORDER = re.compile(rf"^(\d{{1,2}}){_SEP}(\d{{1,2}})\s*\2\s*(\d{{4}})\.?$")
_WORD = r"([^\W\d_]+)"
_ORDINAL = r"(?:st|nd|rd|th)?"
_TEXT_YEAR_FIRST = re.compile(rf"^(\d{{4}})\.?\s*{_WORD}\.?\s*(\d{{1,2}})\.?$")
_TEXT_MONTH_FIRST = re.compile(rf"^{_WORD}\.?\s*(\d{{1,2}}){_ORDINAL},?\s+(\d{{4}})\.?$")
_TEXT_DAY_FIRST = re.compile(rf"^(\d{{1,2}}){_ORDINAL}\.?\s*{_WORD}\.?,?\s+(\d{{4}})\.?$")
_TIME_SUFFIX = re.compile(r"(?:\s+|T)(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$")

# Not printed on receipts paid on the spot; the issue date stands in for them.
ISSUE_DATE_FALLBACK_FIELDS: Final = ("due_date", "performance_date")

# Keys are accent-stripped and case-folded (see ``_fold``).
_MONTHS: Final[dict[str, int]] = {
    # English
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
    # Hungarian
    "januar": 1,
    "februar": 2,
    "febr": 2,
    "marcius": 3,
    "marc": 3,
    "aprilis": 4,
    "majus": 5,
    "maj": 5,
    "junius": 6,
    "julius": 7,
    "augusztus": 8,
    "szeptember": 9,
    "szept": 9,
    "oktober": 10,
    "okt": 10,
}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _build(raw: str, year: int, month: int, day: int) -> date | NormalizationFailure:
    if not _MIN_YEAR <= year <= _MAX_YEAR:
        return NormalizationFailure(f"implausible year {year} in date {raw!r}")
    try:
        return date(year, month, day)
    except ValueError:
        return NormalizationFailure(f"{raw!r} is not a valid calendar date")


def _from_month_name(raw: str, year: str, month_name: str, day: str) -> date | NormalizationFailure:
    month = _MONTHS.get(_fold(month_name))
    if month is None:
        return NormalizationFailure(f"unknown month name {month_name!r} in date {raw!r}")
    return _build(raw, int(year), month, int(day))


def _from_day_month_order(
    raw: str, first: int, second: int, year: int, origin: Origin
) -> date | NormalizationFailure:
    match origin:
        case Origin.US:
            return _build(raw, year, first, second)
        case Origin.OTHER_FOREIGN:
            return _build(raw, year, second, first)
        case Origin.DOMESTIC:
            return NormalizationFailure(
                f"{raw!r} is not a domestic (year-first) date; day/month order is unknown"
            )
        case _:
            return NormalizationFailure(
                f"day/month order of {raw!r} depends on the invoice's origin, "
                "which could not be determined"
            )


def normalize_date(raw: str | None, origin: Origin) -> date | NormalizationFailure:
    """Parse one extracted date under the rule for ``origin``; flag, never guess."""
    if raw is None or not raw.strip():
        return NormalizationFailure("date is missing")
    text = _TIME_SUFFIX.sub("", re.sub(r"\s+", " ", raw).strip())

    if match := _YEAR_FIRST.match(text):
        year, _sep, month, day = match.groups()
        return _build(raw, int(year), int(month), int(day))
    if match := _DAY_MONTH_ORDER.match(text):
        first, _sep, second, year = match.groups()
        return _from_day_month_order(raw, int(first), int(second), int(year), origin)
    if match := _TEXT_YEAR_FIRST.match(text):
        year, month_name, day = match.groups()
        return _from_month_name(raw, year, month_name, day)
    if match := _TEXT_MONTH_FIRST.match(text):
        month_name, day, year = match.groups()
        return _from_month_name(raw, year, month_name, day)
    if match := _TEXT_DAY_FIRST.match(text):
        day, month_name, year = match.groups()
        return _from_month_name(raw, year, month_name, day)
    return NormalizationFailure(f"unrecognised date format {raw!r}")


def format_ledger_date(value: date) -> str:
    """Render a date in the ledger/registry format ``YYYY.MM.DD``."""
    return f"{value.year:04d}.{value.month:02d}.{value.day:02d}"


type DateValue = date | NormalizationFailure | None


@dataclass(frozen=True, slots=True)
class NormalizedDates:
    """Every date field of one invoice after normalisation (USR-002-02 AC6).

    Each field is a ``date``, a ``NormalizationFailure`` (printed but unusable), or
    ``None`` (not extracted at all; whether that matters is the consumer's decision,
    e.g. USR-002-04 tolerates a missing supply date on a foreign invoice).
    """

    issue_date: DateValue = None
    due_date: DateValue = None
    supply_date: DateValue = None
    performance_date: DateValue = None

    @property
    def failures(self) -> tuple[NormalizationFailure, ...]:
        values = (getattr(self, name) for name in DATE_FIELDS)
        return tuple(value for value in values if isinstance(value, NormalizationFailure))

    def formatted(self) -> dict[str, str | None]:
        """``YYYY.MM.DD`` per field; ``None`` where there is no usable date."""
        result: dict[str, str | None] = {}
        for name in DATE_FIELDS:
            value = getattr(self, name)
            result[name] = format_ledger_date(value) if isinstance(value, date) else None
        return result


def normalize_dates(extraction: InvoiceExtraction, origin: Origin) -> NormalizedDates:
    """Normalise every date field independently with the same rule set.

    A due or performance date that was not extracted takes the issue date, when that
    is usable (see the module docstring).
    """
    values: dict[str, DateValue] = {}
    for name in DATE_FIELDS:
        raw: str | None = getattr(extraction, name)
        if raw is None:
            values[name] = None
            continue
        result = normalize_date(raw, origin)
        if isinstance(result, NormalizationFailure):
            result = dataclasses.replace(result, field=name)
        values[name] = result
    issue = values["issue_date"]
    if isinstance(issue, date):
        for name in ISSUE_DATE_FALLBACK_FIELDS:
            if values[name] is None:
                values[name] = issue
    return NormalizedDates(**values)


__all__ = [
    "DATE_FIELDS",
    "ISSUE_DATE_FALLBACK_FIELDS",
    "NormalizedDates",
    "format_ledger_date",
    "normalize_date",
    "normalize_dates",
]
