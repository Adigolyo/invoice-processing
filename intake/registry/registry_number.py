"""Registry number assembly: ``[YYMM][seq][SUPPLIER8]`` (USR-003-01).

``assemble`` is pure string assembly of three components that other stories derive:

- ``YYMM``: the performance date's two-digit year and month (USR-002-04), formatted by the
  shared ``yymm_folder_name`` so the number's prefix always equals its Drive folder name.
- ``seq``: the monthly sequence number (USR-003-02), zero-padded to exactly ``width``
  digits (``Config.sequence_width``).
- ``SUPPLIER8``: the supplier identifier from ``normalize_supplier`` (USR-003-03), used
  verbatim. It is 1-8 characters of ``[A-Z0-9]``: shorter names are kept in full and are
  **not** padded (USR-003-03 AC6), so the total length is ``4 + width + len(supplier8)``,
  i.e. ``4 + width + 8`` for every supplier ID of full length.

Nothing is ever guessed or repaired: a missing (``None``) or invalid component raises
``RegistryNumberError`` and no partial number is produced (AC2). The orchestrator turns the
error into an incomplete ``StageResult`` (``to_stage_result``) and flags the invoice. As a
final guard the result must parse back through ``registry_filename_pattern``, so every
assembled number is one sequence derivation will recognise once it is filed.

Assigning a number at most once per invoice (AC4) and attaching it to the invoice record
(AC5) are the orchestrator's job; this module is deterministic and side-effect free.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Final

from intake.models import FlagReason, StageResult
from intake.registry.folder_naming import validate_yymm, yymm_folder_name
from intake.registry.sequence import registry_filename_pattern
from intake.registry.supplier_id import SUPPLIER_ID_LENGTH

YYMM_LENGTH: Final = 4

_SUPPLIER_ID = re.compile(rf"[A-Z0-9]{{1,{SUPPLIER_ID_LENGTH}}}")


class RegistryNumberError(ValueError):
    """A registry number could not be assembled; the invoice must be flagged, not filed.

    ``component`` names the offending input (``yymm``, ``seq``, ``supplier8``, ``width``
    or ``performance_date``).
    """

    reason: Final = FlagReason.INCOMPLETE_DATA

    def __init__(self, component: str, detail: str) -> None:
        super().__init__(f"registry number not assigned: {component}: {detail}")
        self.component = component

    def to_stage_result[T](self) -> StageResult[T]:
        return StageResult.incomplete(self.reason, str(self))


def _check_width(width: object) -> int:
    if isinstance(width, bool) or not isinstance(width, int) or width < 1:
        raise RegistryNumberError("width", f"must be a positive integer, got {width!r}")
    return width


def _check_yymm(yymm: object) -> str:
    if yymm is None:
        raise RegistryNumberError("yymm", "missing")
    if not isinstance(yymm, str):
        raise RegistryNumberError("yymm", f"must be a YYMM string, got {yymm!r}")
    try:
        return validate_yymm(yymm)
    except ValueError as exc:
        raise RegistryNumberError("yymm", str(exc)) from exc


def _check_seq(seq: object, width: int) -> int:
    if seq is None:
        raise RegistryNumberError("seq", "missing")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise RegistryNumberError("seq", f"must be a non-negative integer, got {seq!r}")
    if seq >= 10**width:
        raise RegistryNumberError("seq", f"{seq} does not fit sequence width {width}")
    return seq


def _check_supplier(supplier8: object) -> str:
    if supplier8 is None:
        raise RegistryNumberError("supplier8", "missing")
    if not isinstance(supplier8, str) or not _SUPPLIER_ID.fullmatch(supplier8):
        raise RegistryNumberError(
            "supplier8",
            f"must be a normalised supplier ID of 1-{SUPPLIER_ID_LENGTH} characters "
            f"[A-Z0-9], got {supplier8!r}",
        )
    return supplier8


def assemble(yymm: str | None, seq: int | None, supplier8: str | None, width: int) -> str:
    """Return the registry number ``[YYMM][seq zero-padded to width][SUPPLIER8]``.

    Raises:
        RegistryNumberError: if any component is missing (``None``) or invalid: a
            non-``YYMM`` month, a negative/non-integer sequence or one needing more than
            ``width`` digits, a supplier ID that is not 1-8 characters of ``[A-Z0-9]``,
            or a non-positive ``width``. No partial number is ever returned.
    """
    checked_width = _check_width(width)
    checked_yymm = _check_yymm(yymm)
    checked_seq = _check_seq(seq, checked_width)
    checked_supplier = _check_supplier(supplier8)

    number = f"{checked_yymm}{checked_seq:0{checked_width}d}{checked_supplier}"

    # Defensive invariants (AC3 and round-trip with sequence derivation); unreachable
    # given the checks above, so a failure here is a programming error.
    expected_length = YYMM_LENGTH + checked_width + len(checked_supplier)
    match = registry_filename_pattern(checked_yymm, checked_width).fullmatch(number)
    if (
        len(number) != expected_length
        or match is None
        or int(match.group("seq")) != checked_seq
        or match.group("supplier") != checked_supplier
    ):  # pragma: no cover
        raise AssertionError(f"assembled registry number {number!r} is malformed")
    return number


def assemble_for_date(
    performance_date: date | None, seq: int | None, supplier8: str | None, width: int
) -> str:
    """``assemble`` with ``YYMM`` taken from the selected performance date (USR-002-04).

    Uses the shared ``yymm_folder_name`` so the prefix matches the Drive month folder.

    Raises:
        RegistryNumberError: if the performance date is missing or not a ``date``, or for
            any reason ``assemble`` raises.
    """
    if performance_date is None:
        raise RegistryNumberError("performance_date", "missing")
    if not isinstance(performance_date, date):
        raise RegistryNumberError(
            "performance_date", f"expected a date, got {type(performance_date).__name__}"
        )
    return assemble(yymm_folder_name(performance_date), seq, supplier8, width)


__all__ = ["YYMM_LENGTH", "RegistryNumberError", "assemble", "assemble_for_date"]
