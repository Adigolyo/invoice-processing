"""Performance date selection: the domestic/foreign rule (USR-002-04).

Runs on dates already normalised by ``normalize_dates`` (USR-002-02), with the origin
from the shared ``determine_origin``, so both steps agree on domestic vs. foreign.

- **Domestic:** the invoice's stated performance date. If it is absent or unparseable
  the invoice is flagged; the foreign rule is never used as a fallback (AC4).
- **Foreign** (US and other-foreign): the earliest of issue, due and supply date. A
  candidate that was not extracted (``None``) is skipped (AC3); if none is left the
  invoice is flagged (AC5). A candidate that was printed but could not be parsed
  (``NormalizationFailure``) flags the invoice even when other candidates are usable:
  the unreadable date might be the earliest, so ignoring it could pick a later date.
- **Unknown origin:** the rule itself cannot be chosen, so the invoice is flagged.

Every flag is a ``NormalizationFailure`` for ``performance_date`` with
``FlagReason.INCOMPLETE_DATA`` (USR-001-04).
"""

from __future__ import annotations

from datetime import date
from typing import Final

from intake.normalization.country_of_origin import Origin
from intake.normalization.dates import NormalizedDates, format_ledger_date
from intake.normalization.result import NormalizationFailure

FIELD: Final = "performance_date"
FOREIGN_CANDIDATES: Final = ("issue_date", "due_date", "supply_date")


def _flag(detail: str) -> NormalizationFailure:
    return NormalizationFailure(detail, field=FIELD)


def _domestic(dates: NormalizedDates) -> date | NormalizationFailure:
    stated = dates.performance_date
    if isinstance(stated, date):
        return stated
    if isinstance(stated, NormalizationFailure):
        return _flag(f"stated performance date of a domestic invoice is unusable: {stated.message}")
    return _flag("domestic invoice states no performance date")


def _foreign(dates: NormalizedDates) -> date | NormalizationFailure:
    usable: list[date] = []
    unparseable: list[str] = []
    for name in FOREIGN_CANDIDATES:
        value = getattr(dates, name)
        if isinstance(value, NormalizationFailure):
            unparseable.append(name)
        elif isinstance(value, date):
            usable.append(value)
    if unparseable:
        return _flag(
            "foreign invoice has unparseable candidate date(s) "
            f"{', '.join(unparseable)}; the earliest date cannot be determined"
        )
    if not usable:
        return _flag("foreign invoice has no issue, due or supply date")
    return min(usable)


def select_performance_date(
    normalized_dates: NormalizedDates, origin: Origin
) -> date | NormalizationFailure:
    """Select the invoice's performance date under the rule for ``origin``; flag, never guess."""
    if origin is Origin.DOMESTIC:
        return _domestic(normalized_dates)
    if origin.is_foreign:
        return _foreign(normalized_dates)
    return _flag("invoice origin could not be determined, so the selection rule is unknown")


def ledger_performance_date(
    normalized_dates: NormalizedDates, origin: Origin
) -> str | NormalizationFailure:
    """The selected performance date as ``YYYY.MM.DD`` (AC6), or the flag."""
    selected = select_performance_date(normalized_dates, origin)
    if isinstance(selected, NormalizationFailure):
        return selected
    return format_ledger_date(selected)


__all__ = ["ledger_performance_date", "select_performance_date"]
