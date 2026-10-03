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
    FiledFileRef,
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

    def add(
        self,
        name: str,
        parent: str,
        mime: str = "application/pdf",
        app_properties: dict[str, str] | None = None,
    ) -> str:
        self._next_id += 1
        file_id = f"id-{self._next_id}"
        entry: dict[str, Any] = {"id": file_id, "name": name, "mimeType": mime, "parents": [parent]}
        if app_properties is not None:
            entry["appProperties"] = dict(app_properties)
        self.files.append(entry)
        return file_id

    def _list(self, **kwargs: Any) -> MagicMock:
        if self.list_error is not None:
            return _request(error=self.list_error)
        q = kwargs["q"]
        if "appProperties has" in q:
            return self._list_by_app_properties(q, kwargs)
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

    def _list_by_app_properties(self, q: str, kwargs: dict[str, Any]) -> MagicMock:
        assert "trashed = false" in q
        assert "parents" in kwargs["fields"]
        clauses = re.findall(
            r"appProperties has \{ key='(\w+)' and value='((?:[^'\\]|\\.)*)' \}", q
        )
        assert clauses, q
        wanted = {key: re.sub(r"\\(.)", r"\1", value) for key, value in clauses}
        matches = [
            f
            for f in self.files
            if f["mimeType"] != FOLDER_MIME_TYPE
            and all(f.get("appProperties", {}).get(k) == v for k, v in wanted.items())
        ]
        page = [
            {"id": f["id"], "name": f["name"], "mimeType": f["mimeType"], "parents": f["parents"]}
            | (
                {"appProperties": f.get("appProperties", {})}
                if "appProperties" in kwargs["fields"]
                else {}
            )
            for f in matches
        ]
        return _request({"files": page})

    def _create(self, **kwargs: Any) -> MagicMock:
        if self.create_error is not None:
            return _request(error=self.create_error)
        body = kwargs["body"]
        file_id = self.add(
            body["name"], body["parents"][0], body.get("mimeType", "x"), body.get("appProperties")
        )
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


# --- source link (filed file <-> Gmail message) ------------------------------------------

MSG = "18f2c3a4b5d6e7f8"
KEY = "sha256:" + "a" * 64


def test_save_file_without_source_sends_no_app_properties() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)

    fake.client().save_file(folder, "2405001ACMECORP.pdf", b"x")

    assert "appProperties" not in fake.files_resource.create.call_args.kwargs["body"]


def test_save_file_attaches_source_message_and_attachment_as_app_properties() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)

    fake.client().save_file(
        folder,
        "2405001ACMECORP.pdf",
        b"x",
        "application/pdf",
        source_message_id=MSG,
        source_attachment_key=KEY,
    )

    body = fake.files_resource.create.call_args.kwargs["body"]
    assert body == {
        "name": "2405001ACMECORP.pdf",
        "parents": [folder],
        "appProperties": {"kibitSourceMessageId": MSG, "kibitSourceAttachment": KEY},
    }
    assert drive_client.SOURCE_MESSAGE_ID_PROPERTY == "kibitSourceMessageId"
    assert drive_client.SOURCE_ATTACHMENT_PROPERTY == "kibitSourceAttachment"


def test_save_file_with_message_id_only_sets_only_the_message_property() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)

    fake.client().save_file(folder, "a.pdf", b"x", source_message_id=MSG)

    body = fake.files_resource.create.call_args.kwargs["body"]
    assert body["appProperties"] == {"kibitSourceMessageId": MSG}


def test_save_file_existing_name_with_source_is_still_a_no_op() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)
    existing = fake.add("a.pdf", folder)

    saved = fake.client().save_file(folder, "a.pdf", b"x", source_message_id=MSG)

    assert saved == SavedFile(file_id=existing, filename="a.pdf", created=False)
    fake.files_resource.create.assert_not_called()


@pytest.mark.parametrize(
    ("message_id", "attachment_key"),
    [
        ("", None),
        ("   ", None),
        ("bad\nid", None),
        (MSG, ""),
        (MSG, "tab\tkey"),
        (MSG, "x" * 200),  # Drive limits key + value to 124 bytes
        (None, KEY),  # an attachment key without its message is meaningless
    ],
)
def test_save_file_rejects_invalid_source_before_any_api_call(
    message_id: str | None, attachment_key: str | None
) -> None:
    fake = FakeDrive()

    with pytest.raises(ValueError):
        fake.client().save_file(
            "folder",
            "a.pdf",
            b"x",
            source_message_id=message_id,
            source_attachment_key=attachment_key,
        )

    fake.files_resource.list.assert_not_called()
    fake.files_resource.create.assert_not_called()


def test_find_filed_by_source_query_shape_and_shared_drive_flags() -> None:
    fake = FakeDrive()

    assert fake.client().find_filed_by_source(MSG, KEY) == []

    kwargs = fake.files_resource.list.call_args.kwargs
    assert kwargs["q"] == (
        f"appProperties has {{ key='kibitSourceMessageId' and value='{MSG}' }} "
        f"and appProperties has {{ key='kibitSourceAttachment' and value='{KEY}' }} "
        f"and mimeType != '{FOLDER_MIME_TYPE}' and trashed = false"
    )
    assert "parents" in kwargs["fields"]
    assert kwargs["supportsAllDrives"] is True
    assert kwargs["includeItemsFromAllDrives"] is True
    assert kwargs["corpora"] == "allDrives"


def test_find_filed_by_source_without_attachment_key_queries_the_message_only() -> None:
    fake = FakeDrive()

    fake.client().find_filed_by_source(MSG)

    q = fake.files_resource.list.call_args.kwargs["q"]
    assert "kibitSourceAttachment" not in q
    assert f"key='kibitSourceMessageId' and value='{MSG}'" in q


def test_find_filed_by_source_escapes_quotes_and_backslashes() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)
    odd_msg, odd_key = "m'1\\x", "k'ey"
    fake.add(
        "2405001A.pdf",
        folder,
        app_properties={"kibitSourceMessageId": odd_msg, "kibitSourceAttachment": odd_key},
    )

    found = fake.client().find_filed_by_source(odd_msg, odd_key)

    q = fake.files_resource.list.call_args.kwargs["q"]
    assert "value='m\\'1\\\\x'" in q
    assert "value='k\\'ey'" in q
    assert [f.name for f in found] == ["2405001A.pdf"]


def test_find_filed_by_source_returns_zero_one_or_many_refs() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)
    client = fake.client()
    props = {"kibitSourceMessageId": MSG, "kibitSourceAttachment": KEY}

    assert client.find_filed_by_source(MSG, KEY) == []

    first = fake.add("2405001ACMECORP.pdf", folder, app_properties=props)
    fake.add("unrelated.pdf", folder, app_properties={"kibitSourceMessageId": "other"})
    fake.add("2405003ACMECORP.pdf", folder, app_properties={"kibitSourceMessageId": MSG})
    assert client.find_filed_by_source(MSG, KEY) == [
        FiledFileRef(file_id=first, name="2405001ACMECORP.pdf", folder_id=folder)
    ]

    second = fake.add("2405002ACMECORP.pdf", folder, app_properties=props)
    assert [r.file_id for r in client.find_filed_by_source(MSG, KEY)] == [first, second]
    assert len(client.find_filed_by_source(MSG)) == 3


def test_find_filed_by_source_after_save_file_finds_the_saved_file() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)
    client = fake.client()

    saved = client.save_file(
        folder, "2405001ACMECORP.pdf", b"x", source_message_id=MSG, source_attachment_key=KEY
    )

    assert client.find_filed_by_source(MSG, KEY) == [
        FiledFileRef(file_id=saved.file_id, name="2405001ACMECORP.pdf", folder_id=folder)
    ]


def test_find_filed_by_source_without_parents_reports_no_folder() -> None:
    fake = FakeDrive()
    fake.files_resource.list.side_effect = None
    fake.files_resource.list.return_value = _request(
        {"files": [{"id": "f1", "name": "2405001A.pdf"}]}
    )

    assert fake.client().find_filed_by_source(MSG) == [
        FiledFileRef(file_id="f1", name="2405001A.pdf", folder_id=None)
    ]


def test_find_filed_by_source_propagates_api_errors() -> None:
    fake = FakeDrive()
    fake.list_error = _http_error(503)

    with pytest.raises(HttpError):
        fake.client().find_filed_by_source(MSG, KEY)


def test_find_filed_by_source_rejects_malformed_entries() -> None:
    fake = FakeDrive()
    fake.files_resource.list.side_effect = None
    fake.files_resource.list.return_value = _request({"files": [{"name": "no-id.pdf"}]})

    with pytest.raises(DriveResponseError):
        fake.client().find_filed_by_source(MSG)


@pytest.mark.parametrize(("message_id", "attachment_key"), [("", None), (" ", None), (MSG, "")])
def test_find_filed_by_source_rejects_blank_ids_before_any_api_call(
    message_id: str, attachment_key: str | None
) -> None:
    fake = FakeDrive()

    with pytest.raises(ValueError):
        fake.client().find_filed_by_source(message_id, attachment_key)

    fake.files_resource.list.assert_not_called()


def test_module_exports() -> None:
    assert "DriveClient" in drive_client.__all__
    assert "FiledFileRef" in drive_client.__all__


# --- find_filed_by_attachment (duplicate PDFs) ------------------------------------------


def test_find_filed_by_attachment_matches_the_fingerprint_across_messages() -> None:
    fake = FakeDrive()
    folder = fake.add("2405", ROOT, FOLDER_MIME_TYPE)
    fake.add(
        "2405_001_A.pdf",
        folder,
        app_properties={"kibitSourceMessageId": "m1", "kibitSourceAttachment": KEY},
    )
    fake.add(
        "2405_002_B.pdf",
        folder,
        app_properties={"kibitSourceMessageId": "m2", "kibitSourceAttachment": "sha256:other"},
    )

    found = fake.client().find_filed_by_attachment(KEY)

    q = fake.files_resource.list.call_args.kwargs["q"]
    assert "kibitSourceMessageId" not in q
    assert f"key='kibitSourceAttachment' and value='{KEY}'" in q
    assert [(f.name, f.source_message_id) for f in found] == [("2405_001_A.pdf", "m1")]


def test_find_filed_by_attachment_rejects_a_blank_key() -> None:
    with pytest.raises(ValueError):
        FakeDrive().client().find_filed_by_attachment("  ")
