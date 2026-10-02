"""Ledger booking: map a fully normalised invoice to ledger columns A-H (USR-004-01).

``build_ledger_row`` turns the values the earlier stages produced into one
``LedgerRow`` (``INV_ID_int, Provider, INV_ID_ext, INV_type, Currency, Net, Gross,
Due date``). It is pure: it never calls Sheets. The orchestrator, the only module allowed
to call ``append_row`` (technical design, "Core Logic"), appends the returned row with
``SheetsClient.append_row`` immediately after the ``DuplicateGate`` check (USR-004-03).
That ordering is the orchestrator's job ("the orchestrator enforces this ordering"), so
booking neither takes nor consults a gate.

Column sources
    - ``INV_ID_int``: the registry number (USR-003-01). It must parse as
      ``[YYMM][seq of exactly sequence_width digits][1-8 chars A-Z0-9]``.
    - ``Provider``: the supplier name as extracted (USR-002-01), tidied only cosmetically
      by ``clean_ledger_text`` (Unicode NFC, whitespace trimmed and collapsed). It is
      **not** the 8-character supplier ID, which USR-003-03 defines for filenames and
      which is already part of ``INV_ID_int``. The orchestrator must pass this same
      ``row.provider`` (and ``row.inv_id_ext``) to ``DuplicateGate.is_duplicate``, so
      the dedupe key is exactly what column B/C hold.
    - ``INV_ID_ext``: the external invoice number as extracted, tidied the same way.
    - ``INV_type``: the route (USR-001-02), written as ``Route.value`` (``direct`` or
      ``tig``) per USR-004-01's assumption. ``Route.AMBIGUOUS`` is never booked.
    - ``Currency``: a resolved ISO 4217 code (USR-002-05), never a symbol or raw text.
    - ``Net`` / ``Gross``: ``Decimal`` values already rounded per USR-004-02. Booking
      does not round (the story says it writes the values "without re-rounding them");
      it checks that ``round_for_ledger`` would leave each amount unchanged and refuses an
      unrounded one (e.g. HUF ``125000.40``) rather than silently fixing it.
    - ``Due date``: a calendar ``date`` (USR-002-02); ``LedgerRow`` writes it as
      ``YYYY.MM.DD``.

Refusal (AC3)
    Every value is checked before the row is built. Anything missing (``None`` or blank),
    a ``NormalizationFailure``, a raw/unnormalised value or an unrounded amount raises
    ``BookingError`` naming every offending column, so no row with a blank or guessed
    cell can reach Sheets. The orchestrator turns it into an incomplete ``StageResult``
    (``to_stage_result``) and the invoice is flagged for manual review (USR-001-04).

Append (AC4/AC5)
    ``SheetsClient.append_row`` writes exactly one row (it verifies ``updatedRows == 1``)
    and lets Sheets errors propagate unchanged, so the orchestrator aborts before
    ``mark_read``/labelling and the invoice is retried next run.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from intake.clients.sheets_client import LEDGER_HEADER, LedgerRow
from intake.models import FlagReason, Route, StageResult
from intake.normalization.amounts import round_for_ledger
from intake.normalization.result import NormalizationFailure
from intake.registry.folder_naming import validate_yymm
from intake.registry.sequence import registry_filename_pattern

INV_TYPE_BY_ROUTE: Final[Mapping[Route, str]] = MappingProxyType(
    {Route.DIRECT: Route.DIRECT.value, Route.TIG: Route.TIG.value}
)
"""Column D value per bookable route. ``Route.AMBIGUOUS`` is deliberately absent."""

_INV_ID_INT, _PROVIDER, _INV_ID_EXT, _INV_TYPE, _CURRENCY, _NET, _GROSS, _DUE_DATE = LEDGER_HEADER

type _Problem = tuple[str, str]


class BookingError(ValueError):
    """The invoice cannot be booked; it must be flagged for manual review, not retried.

    ``problems`` holds ``(column, detail)`` pairs in ledger column order; ``columns``
    names just the offending columns.
    """

    reason: Final = FlagReason.INCOMPLETE_DATA

    def __init__(self, problems: Sequence[_Problem]) -> None:
        if not problems:
            raise ValueError("a BookingError needs at least one problem")
        self.problems: tuple[_Problem, ...] = tuple(problems)
        details = "; ".join(f"{column}: {detail}" for column, detail in self.problems)
        super().__init__(f"ledger row not booked: {details}")

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(column for column, _ in self.problems)

    def to_stage_result[T](self) -> StageResult[T]:
        return StageResult.incomplete(self.reason, str(self))


def clean_ledger_text(value: str) -> str:
    """Cosmetic tidy-up for text columns: Unicode NFC, whitespace trimmed and collapsed.

    Nothing else changes (case, punctuation and accents are kept), so the cell shows the
    value as extracted. Returns ``""`` for whitespace-only input.
    """
    return " ".join(unicodedata.normalize("NFC", value).split())


def _text(column: str, value: object, problems: list[_Problem]) -> str:
    if value is None:
        problems.append((column, "missing"))
        return ""
    if not isinstance(value, str):
        problems.append((column, f"must be text, got {type(value).__name__}"))
        return ""
    if any(unicodedata.category(ch) == "Cc" and not ch.isspace() for ch in value):
        problems.append((column, f"control characters in {value!r}"))
        return ""
    cleaned = clean_ledger_text(value)
    if not cleaned:
        problems.append((column, "missing (blank)"))
    return cleaned


def _registry_number(value: object, width: object, problems: list[_Problem]) -> str:
    if isinstance(width, bool) or not isinstance(width, int) or width < 1:
        problems.append((_INV_ID_INT, f"sequence width must be a positive integer, got {width!r}"))
        return ""
    if value is None or (isinstance(value, str) and not value.strip()):
        problems.append((_INV_ID_INT, "missing"))
        return ""
    if not isinstance(value, str):
        problems.append((_INV_ID_INT, f"must be text, got {type(value).__name__}"))
        return ""
    try:
        validate_yymm(value[:4])
    except ValueError:
        match = None
    else:
        match = registry_filename_pattern(value[:4], width).fullmatch(value)
    if match is None or match.group("ext") is not None:
        problems.append(
            (_INV_ID_INT, f"{value!r} is not [YYMM][{width}-digit seq][1-8 chars A-Z0-9]")
        )
        return ""
    return value


def _route(value: object, problems: list[_Problem]) -> str:
    if value is None:
        problems.append((_INV_TYPE, "missing"))
        return ""
    if not isinstance(value, Route):
        problems.append((_INV_TYPE, f"must be a Route, got {value!r}"))
        return ""
    inv_type = INV_TYPE_BY_ROUTE.get(value)
    if inv_type is None:
        problems.append((_INV_TYPE, f"route {value.value!r} cannot be booked"))
        return ""
    return inv_type


def _currency(value: object, problems: list[_Problem]) -> str | None:
    if isinstance(value, NormalizationFailure):
        problems.append((_CURRENCY, value.message))
        return None
    if value is None or (isinstance(value, str) and not value.strip()):
        problems.append((_CURRENCY, "missing"))
        return None
    if not isinstance(value, str):
        problems.append((_CURRENCY, f"must be text, got {type(value).__name__}"))
        return None
    # round_for_ledger is the single definition of "a resolved ISO 4217 code".
    if isinstance(round_for_ledger(Decimal(0), value), NormalizationFailure):
        problems.append((_CURRENCY, f"{value!r} is not a resolved ISO 4217 code"))
        return None
    return value


def _amount(column: str, value: object, currency: str | None, problems: list[_Problem]) -> Decimal:
    if isinstance(value, NormalizationFailure):
        problems.append((column, value.message))
        return Decimal(0)
    if value is None:
        problems.append((column, "missing"))
        return Decimal(0)
    if not isinstance(value, Decimal):
        problems.append((column, f"must be a normalised Decimal, got {type(value).__name__}"))
        return Decimal(0)
    if not value.is_finite():
        problems.append((column, f"must be finite, got {value}"))
        return Decimal(0)
    if currency is not None:
        rounded = round_for_ledger(value, currency)
        if isinstance(rounded, NormalizationFailure) or rounded != value:
            problems.append(
                (column, f"{value} is not rounded for the ledger in {currency} (USR-004-02)")
            )
    return value


def _due_date(value: object, problems: list[_Problem]) -> date:
    if isinstance(value, NormalizationFailure):
        problems.append((_DUE_DATE, value.message))
    elif value is None:
        problems.append((_DUE_DATE, "missing"))
    elif isinstance(value, datetime) or not isinstance(value, date):
        problems.append((_DUE_DATE, f"must be a normalised date, got {type(value).__name__}"))
    else:
        return value
    return date.min


def build_ledger_row(
    *,
    registry_number: str | None,
    provider: str | None,
    external_invoice_id: str | None,
    route: Route | None,
    currency: str | NormalizationFailure | None,
    net: Decimal | NormalizationFailure | None,
    gross: Decimal | NormalizationFailure | None,
    due_date: date | NormalizationFailure | None,
    sequence_width: int,
) -> LedgerRow:
    """The ledger row (columns A-H) for one fully normalised invoice.

    ``net``/``gross``/``due_date``/``currency`` accept the normalisers' results directly
    (``NormalizedAmounts.net`` after ``round_amounts_for_ledger``, ``NormalizedDates
    .due_date``, ``to_iso_code(...)``); a ``NormalizationFailure`` or ``None`` blocks
    the row. ``sequence_width`` is ``Config.sequence_width``.

    Raises:
        BookingError: listing every missing, unnormalised or unrounded value. No row is
            returned in that case, so nothing can be appended.
    """
    problems: list[_Problem] = []
    inv_id_int = _registry_number(registry_number, sequence_width, problems)
    provider_cell = _text(_PROVIDER, provider, problems)
    inv_id_ext = _text(_INV_ID_EXT, external_invoice_id, problems)
    inv_type = _route(route, problems)
    iso = _currency(currency, problems)
    net_cell = _amount(_NET, net, iso, problems)
    gross_cell = _amount(_GROSS, gross, iso, problems)
    due = _due_date(due_date, problems)
    if problems:
        raise BookingError(problems)
    assert iso is not None  # guaranteed: a None currency always records a problem
    return LedgerRow(
        inv_id_int=inv_id_int,
        provider=provider_cell,
        inv_id_ext=inv_id_ext,
        inv_type=inv_type,
        currency=iso,
        net=net_cell,
        gross=gross_cell,
        due_date=due,
    )


__all__ = ["INV_TYPE_BY_ROUTE", "BookingError", "build_ledger_row", "clean_ledger_text"]
