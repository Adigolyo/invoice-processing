"""Task 4: Drive client wrapper, against a fake ``googleapiclient`` Drive service."""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from intake.clients import drive_client
from intake.clients.drive_client import (
    FOLDER_MIME_TYPE,
    DriveClient,
    DriveConflictError,
    DriveResponseError,
    SavedFile,
)

ROOT = "root-folder-id"


def _http_error(status: int = 503) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b'{"error": "boom"}')


def _request(result: Any = None, error: Exception | None = None) -> MagicMock:
    req = MagicMock(name="request")
    if error is not None:
        req.execute.side_effect = error
    else:
        req.execute.return_value = result
    return req


class FakeDrive:
    """An in-memory Drive answering the narrow ``files().list/create`` queries we issue."""

    def __init__(self) -> None:
        self.service = MagicMock(name="drive")
        self.files_resource = self.service.files.return_value
        self.files: list[dict[str, Any]] = []
        self.files_resource.list.side_effect = self._list
        self.files_resource.create.side_effect = self._create
        self.page_size = 1000
        self.list_error: Exception | None = None
        self.create_error: Exception | None = None
        self._next_id = 0

    def add(self, name: str, parent: str, mime: str = "application/pdf") -> str:
        self._next_id += 1
        file_id = f"id-{self._next_id}"
        self.files.append({"id": file_id, "name": name, "mimeType": mime, "parents": [parent]})
        return file_id

    def _list(self, **kwargs: Any) -> MagicMock:
        if self.list_error is not None:
            return _request(error=self.list_error)
        q = kwargs["q"]
        parent = re.search(r"'((?:[^'\\]|\\.)*)' in parents", q)
        name = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
        assert parent is not None
        assert "trashed = false" in q
        matches = [f for f in self.files if parent.group(1) in f["parents"]]
        if name is not None:
            wanted = name.group(1).replace("\\'", "'").replace("\\\\", "\\")
            matches = [f for f in matches if f["name"] == wanted]
        if f"mimeType = '{FOLDER_MIME_TYPE}'" in q:
            matches = [f for f in matches if f["mimeType"] == FOLDER_MIME_TYPE]
        if f"mimeType != '{FOLDER_MIME_TYPE}'" in q:
            matches = [f for f in matches if f["mimeType"] != FOLDER_MIME_TYPE]
        start = int(kwargs.get("pageToken") or 0)
        page = matches[start : start + self.page_size]
        result: dict[str, Any] = {
            "files": [{"id": f["id"], "name": f["name"], "mimeType": f["mimeType"]} for f in page]
        }
        if start + self.page_size < len(matches):
            result["nextPageToken"] = str(start + self.page_size)
        return _request(result)

    def _create(self, **kwargs: Any) -> MagicMock:
        if self.create_error is not None:
            return _request(error=self.create_error)
        body = kwargs["body"]
        file_id = self.add(body["name"], body["parents"][0], body.get("mimeType", "x"))
        return _request({"id": file_id, "name": body["name"]})

    def client(self) -> DriveClient:
        return DriveClient(self.service, ROOT)


# --- month folders -----------------------------------------------------------------------


def test_list_month_folder_returns_filenames_of_existing_folder() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)
    fake.add("2405001ACMECORP.pdf", folder)
    fake.add("2405002KOVARIKF.jpg", folder)
    fake.add("sub", folder, FOLDER_MIME_TYPE)
    fake.add("2406001OTHER.pdf", ROOT)

    names = fake.client().list_month_folder("2405")

    assert sorted(names) == ["2405001ACMECORP.pdf", "2405002KOVARIKF.jpg"]


def test_list_month_folder_returns_empty_list_when_folder_missing() -> None:
    fake = FakeDrive()
    assert fake.client().list_month_folder("2405") == []


def test_list_month_folder_follows_pagination() -> None:
    fake = FakeDrive()
    fake.page_size = 2
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)
    for seq in range(5):
        fake.add(f"240500{seq}X.pdf", folder)

    assert len(fake.client().list_month_folder("2405")) == 5


def test_list_calls_are_shared_drive_safe() -> None:
    fake = FakeDrive()
    fake.add("2405", ROOT, FOLDER_MIME_TYPE)

    fake.client().list_month_folder("2405")

    for call in fake.files_resource.list.call_args_list:
        assert call.kwargs["supportsAllDrives"] is True
        assert call.kwargs["includeItemsFromAllDrives"] is True
        assert call.kwargs["corpora"] == "allDrives"


def test_list_month_folder_raises_on_api_error() -> None:
    fake = FakeDrive()
    fake.list_error = _http_error(503)

    with pytest.raises(HttpError):
        fake.client().list_month_folder("2405")


def test_list_month_folder_raises_on_duplicate_month_folders() -> None:
    fake = FakeDrive()
    fake.add("2405", ROOT, FOLDER_MIME_TYPE)
    fake.add("2405", ROOT, FOLDER_MIME_TYPE)

    with pytest.raises(DriveConflictError, match="2405"):
        fake.client().list_month_folder("2405")


@pytest.mark.parametrize("bad", ["", "245", "24051", "2413", "2400", "ab05", "24/5", " 2405"])
def test_month_folder_methods_reject_malformed_yymm(bad: str) -> None:
    fake = FakeDrive()
    client = fake.client()
    with pytest.raises(ValueError):
        client.list_month_folder(bad)
    with pytest.raises(ValueError):
        client.create_month_folder(bad)
    fake.files_resource.list.assert_not_called()


def test_find_month_folder_returns_id_or_none() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)
    client = fake.client()

    assert client.find_month_folder("2405") == folder
    assert client.find_month_folder("2406") is None


def test_create_month_folder_creates_named_folder_under_root() -> None:
    fake = FakeDrive()

    folder_id = fake.client().create_month_folder("2406")

    kwargs = fake.files_resource.create.call_args.kwargs
    assert kwargs["body"] == {"name": "2406", "mimeType": FOLDER_MIME_TYPE, "parents": [ROOT]}
    assert kwargs["supportsAllDrives"] is True
    assert fake.client().find_month_folder("2406") == folder_id


def test_create_month_folder_is_idempotent_when_folder_exists() -> None:
    fake = FakeDrive()
    existing = fake.add("2406", ROOT, FOLDER_MIME_TYPE)

    assert fake.client().create_month_folder("2406") == existing
    fake.files_resource.create.assert_not_called()


def test_create_month_folder_raises_on_api_error() -> None:
    fake = FakeDrive()
    fake.create_error = _http_error(500)

    with pytest.raises(HttpError):
        fake.client().create_month_folder("2406")


def test_create_month_folder_raises_when_response_has_no_id() -> None:
    fake = FakeDrive()
    fake.files_resource.create.side_effect = None
    fake.files_resource.create.return_value = _request({})

    with pytest.raises(DriveResponseError):
        fake.client().create_month_folder("2406")


# --- save_file ---------------------------------------------------------------------------


def test_save_file_uploads_content_with_name_parent_and_mime() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)
    content = b"%PDF-1.7 original bytes"

    saved = fake.client().save_file(folder, "2405001ACMECORP.pdf", content)

    assert saved == SavedFile(file_id=saved.file_id, filename="2405001ACMECORP.pdf", created=True)
    kwargs = fake.files_resource.create.call_args.kwargs
    assert kwargs["body"] == {"name": "2405001ACMECORP.pdf", "parents": [folder]}
    assert kwargs["supportsAllDrives"] is True
    media = kwargs["media_body"]
    assert media.mimetype() == "application/pdf"
    assert media.getbytes(0, media.size()) == content


def test_save_file_uses_explicit_mime_type_and_falls_back_to_octet_stream() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)
    client = fake.client()

    client.save_file(folder, "a.bin", b"x", mime_type="image/jpeg")
    assert fake.files_resource.create.call_args.kwargs["media_body"].mimetype() == "image/jpeg"

    client.save_file(folder, "noext", b"x")
    media = fake.files_resource.create.call_args.kwargs["media_body"]
    assert media.mimetype() == "application/octet-stream"


def test_save_file_is_a_no_op_when_filename_already_exists() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)
    existing = fake.add("2405001ACMECORP.pdf", folder)

    saved = fake.client().save_file(folder, "2405001ACMECORP.pdf", b"bytes")

    assert saved == SavedFile(file_id=existing, filename="2405001ACMECORP.pdf", created=False)
    fake.files_resource.create.assert_not_called()


def test_save_file_twice_creates_exactly_one_file() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)
    client = fake.client()

    first = client.save_file(folder, "2405001ACMECORP.pdf", b"bytes")
    second = client.save_file(folder, "2405001ACMECORP.pdf", b"bytes")

    assert first.created and not second.created
    assert first.file_id == second.file_id
    assert fake.files_resource.create.call_count == 1


def test_save_file_escapes_quotes_in_the_existence_query() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)
    fake.add("O'Brien.pdf", folder)

    saved = fake.client().save_file(folder, "O'Brien.pdf", b"x")

    assert saved.created is False
    assert "name = 'O\\'Brien.pdf'" in fake.files_resource.list.call_args.kwargs["q"]


@pytest.mark.parametrize(
    "bad",
    ["", "   ", ".", "..", "../evil.pdf", "a/b.pdf", "a\\b.pdf", "nul\x00.pdf", "tab\t.pdf"],
)
def test_save_file_rejects_unsafe_filenames_before_any_api_call(bad: str) -> None:
    fake = FakeDrive()

    with pytest.raises(ValueError):
        fake.client().save_file("folder", bad, b"x")

    fake.files_resource.list.assert_not_called()
    fake.files_resource.create.assert_not_called()


def test_save_file_rejects_blank_folder_id() -> None:
    fake = FakeDrive()
    with pytest.raises(ValueError):
        fake.client().save_file("", "a.pdf", b"x")


def test_save_file_raises_when_existence_check_fails() -> None:
    fake = FakeDrive()
    fake.list_error = _http_error(503)

    with pytest.raises(HttpError):
        fake.client().save_file("folder", "a.pdf", b"x")

    fake.files_resource.create.assert_not_called()


def test_save_file_raises_on_upload_error() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)
    fake.create_error = _http_error(500)

    with pytest.raises(HttpError):
        fake.client().save_file(folder, "a.pdf", b"x")


def test_constructor_requires_root_folder_id() -> None:
    with pytest.raises(ValueError):
        DriveClient(MagicMock(), "  ")


def test_list_raises_on_malformed_response() -> None:
    fake = FakeDrive()
    fake.files_resource.list.side_effect = None
    fake.files_resource.list.return_value = _request({"files": "nope"})

    with pytest.raises(DriveResponseError):
        fake.client().list_month_folder("2405")


def test_module_exports() -> None:
    assert "DriveClient" in drive_client.__all__
