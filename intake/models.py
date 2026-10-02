"""Shared domain models consumed by every pipeline stage.

All models are frozen (immutable, hashable) dataclasses. Money and quantities use
``Decimal``; floats are rejected so binary rounding errors can never reach the ledger.
Values that could not be extracted are ``None``, never fabricated.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Route(StrEnum):
    """Processing route for an invoice email (USR-001-02)."""

    DIRECT = "direct"
    TIG = "tig"
    AMBIGUOUS = "ambiguous"


class FlagReason(StrEnum):
    """Why an invoice was not processed automatically."""

    AMBIGUOUS_ROUTE = "ambiguous_route"
    INCOMPLETE_DATA = "incomplete_data"
    MISSING_TIG = "missing_tig"


class StageStatus(StrEnum):
    """Outcome of a single pipeline stage."""

    OK = "ok"
    INCOMPLETE = "incomplete"
    ERROR = "error"


class ComparisonOutcome(StrEnum):
    """Result of reconciling a contractor invoice against its TIG (USR-005-01)."""

    MATCH = "match"
    MISMATCH = "mismatch"


class DiscrepancyField(StrEnum):
    """Which compared value disagreed between invoice and TIG."""

    QUANTITY = "quantity"
    UNIT_PRICE = "unit_price"
    TOTAL_NET = "total_net"


def _require_decimal_or_none(obj: object, *names: str) -> None:
    for name in names:
        value = getattr(obj, name)
        if value is not None and not isinstance(value, Decimal):
            raise TypeError(
                f"{type(obj).__name__}.{name} must be a Decimal or None, got {type(value).__name__}"
            )


def _freeze_tuple(obj: object, name: str) -> None:
    value: Iterable[Any] = getattr(obj, name)
    if not isinstance(value, tuple):
        object.__setattr__(obj, name, tuple(value))


@dataclass(frozen=True, slots=True)
class Attachment:
    """Metadata of one email attachment; content is fetched separately."""

    filename: str
    mime_type: str
    attachment_id: str | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    """An unread inbox email that may contain an invoice (USR-001-01)."""

    message_id: str
    thread_id: str
    sender: str
    subject: str
    attachments: tuple[Attachment, ...] = ()
    label_names: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _freeze_tuple(self, "attachments")
        if not isinstance(self.label_names, frozenset):
            object.__setattr__(self, "label_names", frozenset(self.label_names))


@dataclass(frozen=True, slots=True)
class LineItem:
    """One invoice (or TIG) line item."""

    description: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    net: Decimal | None

    def __post_init__(self) -> None:
        _require_decimal_or_none(self, "quantity", "unit_price", "net")


@dataclass(frozen=True, slots=True)
class InvoiceExtraction:
    """Fields extracted from an invoice document (USR-002-01).

    Header currency, amounts and dates are kept exactly as printed; the normalisation
    stage (EPIC-002) turns them into ISO codes, Decimals and dates.
    """

    supplier: str | None = None
    invoice_number: str | None = None
    currency: str | None = None
    net: str | None = None
    gross: str | None = None
    due_date: str | None = None
    issue_date: str | None = None
    supply_date: str | None = None
    performance_date: str | None = None
    line_items: tuple[LineItem, ...] = ()
    # The supplier's country as printed (name or ISO 3166 code), the signal
    # ``determine_origin`` uses (USR-002-02/04/05). ``None`` when not stated.
    supplier_country: str | None = None

    def __post_init__(self) -> None:
        _freeze_tuple(self, "line_items")


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """The classifier's route plus which signal decided it."""

    route: Route
    matched_on: str | None = None

    @property
    def is_ambiguous(self) -> bool:
        return self.route is Route.AMBIGUOUS

    @property
    def flag_reason(self) -> FlagReason | None:
        return FlagReason.AMBIGUOUS_ROUTE if self.is_ambiguous else None


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """One invoice-vs-TIG disagreement.

    ``line_index`` is the zero-based line item position; ``None`` for total-level checks.
    """

    field: DiscrepancyField
    invoice_value: Decimal | None
    tig_value: Decimal | None
    line_index: int | None = None
    line_description: str | None = None

    def __post_init__(self) -> None:
        _require_decimal_or_none(self, "invoice_value", "tig_value")


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """MATCH with no discrepancies, or MISMATCH with at least one."""

    outcome: ComparisonOutcome
    discrepancies: tuple[Discrepancy, ...] = ()

    def __post_init__(self) -> None:
        _freeze_tuple(self, "discrepancies")
        if self.outcome is ComparisonOutcome.MISMATCH and not self.discrepancies:
            raise ValueError("a MISMATCH result needs at least one discrepancy")
        if self.outcome is ComparisonOutcome.MATCH and self.discrepancies:
            raise ValueError("a MATCH result must not carry discrepancies")

    @classmethod
    def match(cls) -> ComparisonResult:
        return cls(ComparisonOutcome.MATCH)

    @classmethod
    def mismatch(cls, discrepancies: Iterable[Discrepancy]) -> ComparisonResult:
        return cls(ComparisonOutcome.MISMATCH, tuple(discrepancies))

    @property
    def is_mismatch(self) -> bool:
        return self.outcome is ComparisonOutcome.MISMATCH


@dataclass(frozen=True, slots=True)
class StageResult[T]:
    """What a pipeline stage hands back to the orchestrator.

    - ``ok``: ``value`` holds the stage output.
    - ``incomplete``: data is missing/ambiguous; ``flag_reason`` says why (flag, never guess).
    - ``error``: the stage failed; ``error`` holds a message. The candidate is left for retry.
    """

    status: StageStatus
    value: T | None = None
    flag_reason: FlagReason | None = None
    detail: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status is StageStatus.OK and (
            self.flag_reason is not None or self.error is not None
        ):
            raise ValueError("an ok StageResult must not carry a flag_reason or error")
        if self.status is StageStatus.INCOMPLETE and self.flag_reason is None:
            raise ValueError("an incomplete StageResult needs a flag_reason")
        if self.status is StageStatus.ERROR and not self.error:
            raise ValueError("an error StageResult needs an error message")

    @classmethod
    def ok(cls, value: T) -> StageResult[T]:
        return cls(StageStatus.OK, value=value)

    @classmethod
    def incomplete(cls, reason: FlagReason, detail: str | None = None) -> StageResult[T]:
        return cls(StageStatus.INCOMPLETE, flag_reason=reason, detail=detail)

    @classmethod
    def failed(cls, error: str) -> StageResult[T]:
        return cls(StageStatus.ERROR, error=error)

    @property
    def is_ok(self) -> bool:
        return self.status is StageStatus.OK


__all__ = [
    "Attachment",
    "Candidate",
    "ComparisonOutcome",
    "ComparisonResult",
    "Discrepancy",
    "DiscrepancyField",
    "FlagReason",
    "InvoiceExtraction",
    "LineItem",
    "Route",
    "RouteDecision",
    "StageResult",
    "StageStatus",
]
