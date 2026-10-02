"""The shared "flag, don't guess" result of every normaliser (EPIC-002).

``normalize_date``, ``normalize_amount``, ``to_iso_code`` and ``select_performance_date``
return either their value or a ``NormalizationFailure``; the orchestrator turns any
failure into an incomplete ``StageResult`` (USR-001-04).
"""

from __future__ import annotations

from dataclasses import dataclass

from intake.models import FlagReason, StageResult


@dataclass(frozen=True, slots=True)
class NormalizationFailure:
    """A value that could not be normalised reliably.

    ``field`` names the invoice field when known (e.g. ``"due_date"``).
    """

    detail: str
    field: str | None = None
    reason: FlagReason = FlagReason.INCOMPLETE_DATA

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("a NormalizationFailure needs a detail message")

    @property
    def message(self) -> str:
        return f"{self.field}: {self.detail}" if self.field else self.detail

    def to_stage_result[T](self) -> StageResult[T]:
        return StageResult.incomplete(self.reason, self.message)


__all__ = ["NormalizationFailure"]
