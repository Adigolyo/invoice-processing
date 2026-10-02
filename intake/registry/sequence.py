"""Sequence number derivation from the monthly Drive folder (USR-003-02).

There is no stored counter: the next ``[seq]`` for a month is derived from the files that
are already filed in that month's ``YYMM`` folder, read live at derivation time.

Filename pattern
    Filed invoices are named ``{registry_number}.{ext}`` where the registry number is
    ``[YYMM][seq][SUPPLIER]``: the 4-digit month, ``seq`` zero-padded to exactly
    ``sequence_width`` ASCII digits, and the supplier ID (1-8 characters of ``[A-Z0-9]``,
    see ``supplier_id.py``). Because ``seq`` has a fixed width it is always the
    ``width`` digits straight after ``YYMM``; a supplier ID that starts with digits
    (``3M``, ``7ELEVEN``) can therefore never be mistaken for part of the sequence.
    Only names whose prefix is the folder's own ``YYMM`` count. The extension is
    optional (a bare registry number still marks its sequence as taken), but there may
    be at most one extension segment. Anything else (other documents, Drive copies like
    ``"... (1).pdf"``, lower-case or over-long supplier IDs) is ignored (AC5).

Algorithm
    ``next_sequence`` returns ``max(found) + 1``, or the configured start value when no
    filename matches (AC1, AC2, AC4). Gaps left by files removed outside the pipeline are
    not back-filled. A result that no longer fits ``width`` digits raises
    ``SequenceExhaustedError`` rather than producing an over-long registry number.

In-run allocation (AC3)
    ``SequenceAllocator`` re-scans the folder on every ``allocate`` call and also treats
    every number it has already handed out in this run as taken, so several invoices for
    the same month in one run never collide even before they are filed. A scan failure
    propagates unchanged and nothing is allocated (AC6): no number is ever guessed.

    The allocation set lives in one process only. Two overlapping runs (or a future
    concurrent worker model) could still derive the same number from the same folder
    state; the design avoids that by running a single sequential worker (ADR 5).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from typing import Protocol

from intake.config import Config
from intake.registry.folder_naming import validate_yymm
from intake.registry.supplier_id import SUPPLIER_ID_LENGTH


class SequenceExhaustedError(ValueError):
    """The next sequence number does not fit the configured ``sequence_width``."""


class MonthFolderLister(Protocol):
    """The slice of ``DriveClient`` sequence derivation needs."""

    def list_month_folder(self, yymm: str) -> list[str]:
        """Filenames in the ``YYMM`` folder; ``[]`` if missing; raises if the scan fails."""
        ...


def _validate_width(width: int) -> None:
    if isinstance(width, bool) or not isinstance(width, int) or width < 1:
        raise ValueError(f"sequence width must be a positive integer, got {width!r}")


def _validate_start(start: int, width: int) -> None:
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise ValueError(f"sequence start must be a non-negative integer, got {start!r}")
    if start >= 10**width:
        raise ValueError(f"sequence start {start} does not fit sequence width {width}")


@lru_cache(maxsize=64)
def registry_filename_pattern(yymm: str, width: int) -> re.Pattern[str]:
    """Compiled pattern for registry filenames in the ``yymm`` folder (use ``fullmatch``).

    Named groups: ``seq`` (exactly ``width`` digits), ``supplier`` and optional ``ext``.
    """
    validate_yymm(yymm)
    _validate_width(width)
    return re.compile(
        rf"{yymm}"
        rf"(?P<seq>[0-9]{{{width}}})"
        rf"(?P<supplier>[A-Z0-9]{{1,{SUPPLIER_ID_LENGTH}}})"
        r"(?:\.(?P<ext>[^./\\\s]+))?"
    )


def existing_sequences(filenames: Iterable[str], *, yymm: str, width: int) -> set[int]:
    """The ``[seq]`` values of every filename that matches the registry pattern."""
    pattern = registry_filename_pattern(yymm, width)
    found: set[int] = set()
    for name in filenames:
        match = pattern.fullmatch(name)
        if match is not None:
            found.add(int(match.group("seq")))
    return found


def next_sequence(
    existing_filenames: Iterable[str],
    *,
    yymm: str,
    width: int,
    start: int,
    allocated: Iterable[int] = (),
) -> int:
    """The next free sequence number for month ``yymm``.

    ``allocated`` holds numbers already handed out in this run but possibly not yet filed;
    they count as taken exactly like matching filenames.

    Raises:
        ValueError: on an invalid ``yymm``, ``width`` or ``start``.
        SequenceExhaustedError: if the next number needs more than ``width`` digits.
    """
    _validate_width(width)
    _validate_start(start, width)
    taken = existing_sequences(existing_filenames, yymm=yymm, width=width)
    taken.update(allocated)
    candidate = max(taken) + 1 if taken else start
    if candidate >= 10**width:
        raise SequenceExhaustedError(
            f"month {yymm}: sequence {candidate} does not fit sequence width {width}"
        )
    return candidate


class SequenceAllocator:
    """Hands out sequence numbers for one pipeline run, never the same one twice.

    Create one per run. Each ``allocate`` reads the month folder live, so numbers filed
    meanwhile (by this run or anyone else) are always respected.
    """

    def __init__(self, drive: MonthFolderLister, *, start: int, width: int) -> None:
        _validate_width(width)
        _validate_start(start, width)
        self._drive = drive
        self.start = start
        self.width = width
        self._allocated: dict[str, set[int]] = {}

    @classmethod
    def from_config(cls, drive: MonthFolderLister, config: Config) -> SequenceAllocator:
        return cls(drive, start=config.sequence_start, width=config.sequence_width)

    def allocate(self, yymm: str) -> int:
        """Derive and reserve the next sequence number for month ``yymm``.

        Raises:
            ValueError: if ``yymm`` is not a valid ``YYMM`` (checked before any scan).
            SequenceExhaustedError: if the month has run out of ``width``-digit numbers.
            Exception: whatever the folder scan raised (e.g. ``HttpError``), unchanged.
                Nothing is reserved in any failure case.
        """
        validate_yymm(yymm)
        filenames = self._drive.list_month_folder(yymm)
        reserved = self._allocated.setdefault(yymm, set())
        seq = next_sequence(
            filenames, yymm=yymm, width=self.width, start=self.start, allocated=reserved
        )
        reserved.add(seq)
        return seq

    def release(self, yymm: str, seq: int) -> None:
        """Return a number whose invoice was not filed, so the run can reuse it.

        Safe even if the file was in fact filed: the live folder scan still counts it.
        Releasing a number that was never allocated is a no-op.
        """
        self._allocated.get(yymm, set()).discard(seq)

    def allocated(self, yymm: str) -> frozenset[int]:
        """Numbers currently reserved in this run for month ``yymm``."""
        return frozenset(self._allocated.get(yymm, ()))


__all__ = [
    "MonthFolderLister",
    "SequenceAllocator",
    "SequenceExhaustedError",
    "existing_sequences",
    "next_sequence",
    "registry_filename_pattern",
]
