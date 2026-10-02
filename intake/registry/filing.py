"""Renamed invoice filing into the monthly ``YYMM`` Drive folder (USR-003-04).

``file_invoice`` saves the original invoice bytes, unchanged, as
``{registry_number}.{ext}`` into the month folder named by the registry number's
``YYMM`` prefix, creating that folder first if it does not exist yet (AC1-AC3).

Month folder
    The folder name is taken from the registry number itself (its first four
    characters), never derived separately, so the number and its folder cannot disagree.
    It uses the same ``YYMM`` convention (``folder_naming``) that sequence derivation
    (USR-003-02) scans. When the caller also passes the performance date, the prefix must
    equal ``yymm_folder_name(performance_date)``.

Validation (AC4)
    Everything is checked before the first Drive call: a missing registry number, one
    that does not parse with ``registry_filename_pattern`` for the configured sequence
    width (so sequence derivation would not count the filed file), a missing or unsafe
    extension (the last ``.``-segment must be 1-10 ASCII letters/digits) or empty
    content raise ``FilingError``. The orchestrator turns it into an incomplete
    ``StageResult`` and the invoice stays flagged for manual review. Nothing is ever
    filed under a placeholder name.

Extension
    The original extension is kept verbatim, including its case (``scan.PNG`` becomes
    ``<number>.PNG``): the specification asks to preserve it and is silent on
    normalisation.

Failures (AC5)
    Drive errors (``HttpError``, ``DriveConflictError``, timeouts, ...) propagate
    unchanged; filing never swallows them, so the orchestrator aborts before booking or
    labelling and the invoice is retried next run.

Idempotency (AC6)
    ``DriveClient.save_file`` is idempotent by filename. When the name already exists,
    nothing is uploaded or overwritten and the result's status is
    ``FilingStatus.ALREADY_PRESENT``. Filing cannot tell a re-run of the same invoice
    from a different invoice that was given the same number (e.g. overlapping runs):
    ``DriveClient`` does not expose size/checksum. The status is therefore explicit, and
    a caller that has just allocated a fresh number can pass ``require_new=True`` to get
    ``FilingConflictError`` instead.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final, Protocol

from intake.clients.drive_client import SavedFile, validate_filename
from intake.clients.gmail_client import AttachmentContent
from intake.models import FlagReason, StageResult
from intake.registry.folder_naming import validate_yymm, yymm_folder_name
from intake.registry.sequence import registry_filename_pattern

MAX_EXTENSION_LENGTH: Final = 10

_EXTENSION = re.compile(rf"[A-Za-z0-9]{{1,{MAX_EXTENSION_LENGTH}}}")


class FilingError(ValueError):
    """The invoice cannot be filed; it must be flagged for manual review, not retried.

    ``component`` names the offending input (``registry_number``, ``extension``,
    ``content``, ``sequence_width`` or ``performance_date``).
    """

    reason: Final = FlagReason.INCOMPLETE_DATA

    def __init__(self, component: str, detail: str) -> None:
        super().__init__(f"invoice not filed: {component}: {detail}")
        self.component = component

    def to_stage_result[T](self) -> StageResult[T]:
        return StageResult.incomplete(self.reason, str(self))


class FilingConflictError(RuntimeError):
    """``require_new`` was set but a file with the target name already exists."""

    def __init__(self, folder_id: str, filename: str, file_id: str) -> None:
        super().__init__(f"a file named {filename!r} already exists in month folder {folder_id!r}")
        self.folder_id = folder_id
        self.filename = filename
        self.file_id = file_id


class FilingStatus(StrEnum):
    CREATED = "created"
    """The file was uploaded by this call."""
    ALREADY_PRESENT = "already_present"
    """A file with this name already existed; nothing was uploaded or overwritten."""


@dataclass(frozen=True, slots=True)
class FiledInvoice:
    """Where an invoice was filed and whether this call uploaded it."""

    file_id: str
    filename: str
    folder_id: str
    yymm: str
    status: FilingStatus
    folder_created: bool

    @property
    def created(self) -> bool:
        return self.status is FilingStatus.CREATED


class MonthFolderDrive(Protocol):
    """The slice of ``DriveClient`` filing needs."""

    def find_month_folder(self, yymm: str) -> str | None: ...

    def create_month_folder(self, yymm: str) -> str: ...

    def save_file(
        self, folder_id: str, filename: str, content: bytes, mime_type: str | None = None
    ) -> SavedFile: ...


def _check_width(width: object) -> int:
    if isinstance(width, bool) or not isinstance(width, int) or width < 1:
        raise FilingError("sequence_width", f"must be a positive integer, got {width!r}")
    return width


def _check_registry_number(registry_number: object, width: int) -> str:
    if registry_number is None or (
        isinstance(registry_number, str) and not registry_number.strip()
    ):
        raise FilingError("registry_number", "missing")
    if not isinstance(registry_number, str):
        raise FilingError("registry_number", f"must be a string, got {registry_number!r}")
    yymm = registry_number[:4]
    try:
        validate_yymm(yymm)
    except ValueError as exc:
        raise FilingError("registry_number", f"{registry_number!r}: {exc}") from exc
    match = registry_filename_pattern(yymm, width).fullmatch(registry_number)
    if match is None or match.group("ext") is not None:
        raise FilingError(
            "registry_number",
            f"{registry_number!r} is not [YYMM][{width}-digit seq][1-8 chars A-Z0-9]",
        )
    return registry_number


def _check_performance_date(performance_date: object, yymm: str) -> None:
    if performance_date is None:
        return
    if not isinstance(performance_date, date):
        raise FilingError(
            "performance_date", f"expected a date, got {type(performance_date).__name__}"
        )
    expected = yymm_folder_name(performance_date)
    if expected != yymm:
        raise FilingError(
            "registry_number",
            f"month prefix {yymm!r} disagrees with the performance date's month {expected!r}",
        )


def _extension(original_filename: object) -> str:
    if not isinstance(original_filename, str):
        raise FilingError(
            "extension", f"original filename must be a string, got {original_filename!r}"
        )
    if any(unicodedata.category(ch) == "Cc" for ch in original_filename):
        raise FilingError("extension", f"control characters in {original_filename!r}")
    _, dot, ext = original_filename.strip().rpartition(".")
    if not dot or not _EXTENSION.fullmatch(ext):
        raise FilingError(
            "extension",
            f"{original_filename!r} has no usable extension "
            f"(1-{MAX_EXTENSION_LENGTH} ASCII letters/digits)",
        )
    return ext


def filed_filename(
    registry_number: str | None, original_filename: str, *, sequence_width: int
) -> str:
    """The Drive filename ``{registry_number}.{ext}`` for an invoice.

    Raises:
        FilingError: on a missing/malformed registry number, a missing/unsafe extension
            or an invalid ``sequence_width``.
    """
    width = _check_width(sequence_width)
    number = _check_registry_number(registry_number, width)
    filename = f"{number}.{_extension(original_filename)}"
    validate_filename(filename)  # defence in depth; cannot fail after the checks above
    return filename


def file_invoice(
    drive: MonthFolderDrive,
    registry_number: str | None,
    original_filename: str,
    content: bytes,
    *,
    sequence_width: int,
    mime_type: str | None = None,
    performance_date: date | None = None,
    require_new: bool = False,
) -> FiledInvoice:
    """File ``content`` as ``{registry_number}.{ext}`` in its ``YYMM`` month folder.

    Args:
        drive: the ``DriveClient`` (or anything with the same three methods).
        registry_number: the assigned number; ``None``/blank means filing is refused.
        original_filename: the source document's name; only its extension is used.
        content: the document bytes, uploaded unchanged.
        sequence_width: ``Config.sequence_width``, used to validate the number.
        mime_type: passed to Drive; ``None`` lets Drive guess from the extension.
        performance_date: if given, must fall in the registry number's month.
        require_new: raise ``FilingConflictError`` instead of returning
            ``ALREADY_PRESENT`` when the name is already taken.

    Raises:
        FilingError: invalid input, raised before any Drive call (flag the invoice).
        TypeError: if ``content`` is not bytes.
        FilingConflictError: only with ``require_new`` and an existing name.
        Exception: any Drive failure, unchanged (retry next run).
    """
    filename = filed_filename(registry_number, original_filename, sequence_width=sequence_width)
    yymm = filename[:4]
    _check_performance_date(performance_date, yymm)
    if not isinstance(content, bytes | bytearray):
        raise TypeError(f"content must be bytes, got {type(content).__name__}")
    if not content:
        raise FilingError("content", "the invoice document is empty")

    folder_id = drive.find_month_folder(yymm)
    folder_created = folder_id is None
    if folder_id is None:
        folder_id = drive.create_month_folder(yymm)

    saved = drive.save_file(folder_id, filename, bytes(content), mime_type)
    if not saved.created and require_new:
        raise FilingConflictError(folder_id, filename, saved.file_id)
    return FiledInvoice(
        file_id=saved.file_id,
        filename=filename,
        folder_id=folder_id,
        yymm=yymm,
        status=FilingStatus.CREATED if saved.created else FilingStatus.ALREADY_PRESENT,
        folder_created=folder_created,
    )


def file_attachment(
    drive: MonthFolderDrive,
    registry_number: str | None,
    attachment: AttachmentContent,
    *,
    sequence_width: int,
    performance_date: date | None = None,
    require_new: bool = False,
) -> FiledInvoice:
    """``file_invoice`` for a downloaded Gmail attachment (name, MIME type and bytes)."""
    return file_invoice(
        drive,
        registry_number,
        attachment.attachment.filename,
        attachment.content,
        sequence_width=sequence_width,
        mime_type=attachment.attachment.mime_type or None,
        performance_date=performance_date,
        require_new=require_new,
    )


__all__ = [
    "MAX_EXTENSION_LENGTH",
    "FiledInvoice",
    "FilingConflictError",
    "FilingError",
    "FilingStatus",
    "MonthFolderDrive",
    "file_attachment",
    "file_invoice",
    "filed_filename",
]
