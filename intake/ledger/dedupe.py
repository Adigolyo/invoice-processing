"""Duplicate invoice entry prevention: the gate consulted right before a ledger append.

USR-004-03. An invoice is "already booked" when the ledger holds a row whose
``(INV_ID_ext, Provider)`` pair (columns C and B) matches the invoice's pair. Both values
must match together: the same external ID from a different provider is not a duplicate.

Rules:

- **Live read, never cached.** Every check calls ``load_existing_keys()`` on the ledger
  source (``SheetsClient`` in production), so the answer reflects the ledger at check
  time. This makes the check correct across runs and restarts with no extra state.
- **In-run guard.** ``DuplicateGate`` additionally remembers keys the orchestrator
  reports as booked *in this run* (``record_booked``). The live read already covers a
  repeated candidate, but if a read straight after an append did not yet show the new
  row, the guard still blocks the second append. It only ever adds keys to the live set,
  so it can make a check more conservative, never less. Create one gate per run.
- **Fail closed.** If the ledger cannot be read, or answers with something that is not a
  collection of ``(ext_id, provider)`` string pairs, ``DuplicateCheckError`` is raised.
  The caller must not book the invoice (and must not label it), leaving it for retry.
- **Key comparison.** Both values are compared after Unicode NFKC normalisation,
  whitespace trimming and collapsing, and case folding. Cosmetic variants of a booked
  key (stray spaces, a non-breaking space, different case, full-width characters) are
  therefore treated as the same invoice; any other difference (punctuation, digits,
  accents) is significant.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Iterable
from typing import Protocol

logger = logging.getLogger(__name__)

DedupeKey = tuple[str, str]
"""A normalised ``(ext_id, provider)`` pair."""


class LedgerKeySource(Protocol):
    """Anything that can read the ledger's ``{(INV_ID_ext, Provider)}`` live."""

    def load_existing_keys(self) -> set[tuple[str, str]]: ...


class DuplicateCheckError(RuntimeError):
    """The duplicate check could not complete; the invoice must not be booked."""


def _normalise(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def dedupe_key(*, ext_id: str, provider: str) -> DedupeKey:
    """The normalised ``(ext_id, provider)`` key used for comparison.

    Raises:
        ValueError: if either value is blank; an invoice without both cannot be checked.
    """
    key = (_normalise(ext_id), _normalise(provider))
    if not key[0] or not key[1]:
        raise ValueError("duplicate check needs a non-blank external invoice ID and provider")
    return key


def _read_keys(source: LedgerKeySource) -> set[DedupeKey]:
    try:
        raw: object = source.load_existing_keys()
    except Exception as exc:
        raise DuplicateCheckError(
            f"could not read existing ledger keys ({type(exc).__name__}); "
            "refusing to book without a completed duplicate check"
        ) from exc
    return _normalise_keys(raw)


def _normalise_keys(raw: object) -> set[DedupeKey]:
    if isinstance(raw, str | bytes) or not isinstance(raw, Iterable):
        raise DuplicateCheckError(
            f"ledger key source returned {type(raw).__name__}, not a collection of keys"
        )
    keys: set[DedupeKey] = set()
    for item in raw:
        if not (
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], str)
        ):
            raise DuplicateCheckError(
                f"ledger key source returned a malformed key of type {type(item).__name__}"
            )
        ext_id, provider = _normalise(item[0]), _normalise(item[1])
        if ext_id and provider:
            keys.add((ext_id, provider))
    return keys


def is_duplicate(source: LedgerKeySource, *, ext_id: str, provider: str) -> bool:
    """Whether the ledger already holds this ``(ext_id, provider)``, read live now.

    Raises:
        ValueError: if ``ext_id`` or ``provider`` is blank (checked before any read).
        DuplicateCheckError: if the ledger could not be read or answered malformed data.
    """
    key = dedupe_key(ext_id=ext_id, provider=provider)
    found = key in _read_keys(source)
    if found:
        logger.info("duplicate ledger entry detected")
    return found


class DuplicateGate:
    """Run-scoped duplicate gate: a live ledger read plus this run's booked keys.

    The orchestrator creates one per run, calls ``is_duplicate`` immediately before each
    append, and ``record_booked`` after each confirmed append.
    """

    def __init__(self, source: LedgerKeySource) -> None:
        self._source = source
        self._booked_this_run: set[DedupeKey] = set()

    def is_duplicate(self, *, ext_id: str, provider: str) -> bool:
        """Whether the invoice is already booked (in the live ledger or earlier this run).

        Always reads the ledger, even for a key booked earlier in this run, so a
        failing read blocks booking in every case.

        Raises:
            ValueError: if ``ext_id`` or ``provider`` is blank (checked before any read).
            DuplicateCheckError: if the ledger could not be read or answered malformed data.
        """
        key = dedupe_key(ext_id=ext_id, provider=provider)
        live = _read_keys(self._source)
        found = key in live or key in self._booked_this_run
        if found:
            logger.info("duplicate ledger entry detected")
        return found

    def record_booked(self, *, ext_id: str, provider: str) -> None:
        """Remember a key whose ledger row was appended successfully in this run."""
        self._booked_this_run.add(dedupe_key(ext_id=ext_id, provider=provider))


__all__ = [
    "DedupeKey",
    "DuplicateCheckError",
    "DuplicateGate",
    "LedgerKeySource",
    "dedupe_key",
    "is_duplicate",
]
