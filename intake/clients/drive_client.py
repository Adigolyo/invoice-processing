"""Thin wrapper over the Drive v3 API for the monthly invoice folders.

Layout (technical design, "Data Layer"): one root folder (``Config.drive_root_folder_id``)
holds one subfolder per month named ``YYMM``; filed invoices are named
``<registry number>.<ext>``.

The ``googleapiclient`` Drive resource is injected through the constructor. Every call is
Shared-Drive-safe (``supportsAllDrives``; list calls also ``includeItemsFromAllDrives`` and
``corpora="allDrives"``), so the root folder may live in a My Drive or a Shared Drive.

Error semantics: Drive API failures propagate unchanged as
``googleapiclient.errors.HttpError``. Malformed responses raise ``DriveResponseError``;
two month folders with the same name under the root raise ``DriveConflictError`` (which
one to use cannot be decided safely). Invalid arguments raise ``ValueError`` before any
API call.
"""

from __future__ import annotations

import io
import mimetypes
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from googleapiclient.http import MediaIoBaseUpload

DriveResource = Any
"""The untyped ``googleapiclient`` Drive v3 resource."""

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DEFAULT_MIME_TYPE = "application/octet-stream"

_YYMM = re.compile(r"^\d{2}(0[1-9]|1[0-2])$")
_LIST_FIELDS = "nextPageToken, files(id, name, mimeType)"
_PAGE_SIZE = 1000


class DriveResponseError(RuntimeError):
    """The Drive API answered, but not with the shape this client relies on."""


class DriveConflictError(RuntimeError):
    """More than one month folder with the same name exists under the root folder."""


@dataclass(frozen=True, slots=True)
class SavedFile:
    """Result of ``save_file``: ``created`` is False when the name already existed."""

    file_id: str
    filename: str
    created: bool


def _validate_yymm(yymm: str) -> None:
    if not _YYMM.fullmatch(yymm):
        raise ValueError(f"month folder name must be YYMM (e.g. '2405'), got {yymm!r}")


def validate_filename(filename: str) -> None:
    """Reject names that could escape the folder or corrupt the Drive name."""
    if not filename.strip() or filename in {".", ".."}:
        raise ValueError(f"invalid filename: {filename!r}")
    if "/" in filename or "\\" in filename:
        raise ValueError(f"filename must not contain path separators: {filename!r}")
    if any(unicodedata.category(ch) == "Cc" for ch in filename):
        raise ValueError(f"filename must not contain control characters: {filename!r}")


def _escape(value: str) -> str:
    """Escape a literal for a Drive ``q`` string (backslash and single quote)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _execute(request: Any) -> dict[str, Any]:
    result = request.execute()
    if not isinstance(result, dict):
        raise DriveResponseError(f"unexpected Drive API response type: {type(result).__name__}")
    return result


def _file_id(entry: Any) -> str:
    value = entry.get("id") if isinstance(entry, dict) else None
    if not isinstance(value, str) or not value:
        raise DriveResponseError("Drive response is missing a file 'id'")
    return value


class DriveClient:
    """Drive operations used by sequence derivation (USR-003-02) and filing (USR-003-04)."""

    def __init__(self, service: DriveResource, root_folder_id: str) -> None:
        if not root_folder_id.strip():
            raise ValueError("root_folder_id is required")
        self._service = service
        self._root = root_folder_id.strip()

    def find_month_folder(self, yymm: str) -> str | None:
        """ID of the ``YYMM`` folder under the root, or None if it does not exist."""
        _validate_yymm(yymm)
        matches = self._list(
            f"'{_escape(self._root)}' in parents and name = '{yymm}' "
            f"and mimeType = '{FOLDER_MIME_TYPE}' and trashed = false"
        )
        if len(matches) > 1:
            raise DriveConflictError(
                f"{len(matches)} folders named {yymm!r} exist under the invoice root folder"
            )
        return _file_id(matches[0]) if matches else None

    def list_month_folder(self, yymm: str) -> list[str]:
        """Names of the (non-folder) files in the ``YYMM`` folder; ``[]`` if it is missing.

        Raises on any API error, so a failed scan is never mistaken for an empty month.
        """
        folder_id = self.find_month_folder(yymm)
        if folder_id is None:
            return []
        files = self._list(
            f"'{_escape(folder_id)}' in parents and mimeType != '{FOLDER_MIME_TYPE}' "
            "and trashed = false"
        )
        return [str(f.get("name", "")) for f in files]

    def create_month_folder(self, yymm: str) -> str:
        """Create the ``YYMM`` folder under the root and return its ID.

        Idempotent: if the folder already exists its ID is returned and nothing is created.
        """
        existing = self.find_month_folder(yymm)
        if existing is not None:
            return existing
        response = _execute(
            self._service.files().create(
                body={"name": yymm, "mimeType": FOLDER_MIME_TYPE, "parents": [self._root]},
                fields="id",
                supportsAllDrives=True,
            )
        )
        return _file_id(response)

    def save_file(
        self, folder_id: str, filename: str, content: bytes, mime_type: str | None = None
    ) -> SavedFile:
        """Upload ``content`` as ``filename`` into ``folder_id``, unless that name exists.

        Idempotent by filename: if a file with exactly this name is already in the folder,
        nothing is uploaded and the existing file's ID is returned with ``created=False``.
        The MIME type defaults to one guessed from the extension.
        """
        if not folder_id.strip():
            raise ValueError("folder_id is required")
        validate_filename(filename)
        existing = self._list(
            f"'{_escape(folder_id)}' in parents and name = '{_escape(filename)}' "
            "and trashed = false"
        )
        if existing:
            return SavedFile(file_id=_file_id(existing[0]), filename=filename, created=False)

        resolved_mime = mime_type or mimetypes.guess_type(filename)[0] or DEFAULT_MIME_TYPE
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=resolved_mime, resumable=False)
        response = _execute(
            self._service.files().create(
                body={"name": filename, "parents": [folder_id]},
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            )
        )
        return SavedFile(file_id=_file_id(response), filename=filename, created=True)

    def _list(self, query: str) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "q": query,
                "fields": _LIST_FIELDS,
                "pageSize": _PAGE_SIZE,
                "corpora": "allDrives",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            response = _execute(self._service.files().list(**kwargs))
            page = response.get("files", [])
            if not isinstance(page, list) or not all(isinstance(f, dict) for f in page):
                raise DriveResponseError("Drive files.list response has a malformed 'files'")
            files.extend(page)
            page_token = response.get("nextPageToken")
            if not page_token:
                return files


__all__ = [
    "FOLDER_MIME_TYPE",
    "DriveClient",
    "DriveConflictError",
    "DriveResponseError",
    "SavedFile",
    "validate_filename",
]
