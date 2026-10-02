"""Ledger booking and duplicate-entry prevention (EPIC-004)."""

from intake.ledger.dedupe import (
    DedupeKey,
    DuplicateCheckError,
    DuplicateGate,
    LedgerKeySource,
    dedupe_key,
    is_duplicate,
)

__all__ = [
    "DedupeKey",
    "DuplicateCheckError",
    "DuplicateGate",
    "LedgerKeySource",
    "dedupe_key",
    "is_duplicate",
]
