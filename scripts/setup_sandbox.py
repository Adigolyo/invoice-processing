"""One-off setup of the SANDBOX Google Workspace account for the end-to-end demo.

Idempotent: everything it creates is looked up first and reused if present.

    .venv/bin/python scripts/setup_sandbox.py              # labels, folder, ledger, .env
    .venv/bin/python scripts/setup_sandbox.py --seed-demo  # ...plus the demo emails

Needs ``secrets/token.json`` (``python -m intake --authorize``, signed in as the sandbox
mailbox). Demo emails are *inserted* into the sandbox inbox with ``users.messages.insert``
(Gmail's import API): nothing is sent, and every sender is a fake ``*.example`` address.
Attachments come from the committed synthetic fixtures in ``tests/fixtures/tig_pairs/``.
Prints IDs and counts only, never secrets or document content.
"""

from __future__ import annotations

import argparse
import base64
import sys
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intake.__main__ import local_credentials  # noqa: E402
from intake.clients.sheets_client import LEDGER_HEADER  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "tig_pairs"
ENV_FILE = ROOT / ".env"
SECRETS_DIR = ROOT / "secrets"

LABELS = ("Kibit/Processed", "Kibit/Pending", "Kibit/NeedsReview", "Kibit/AwaitingTIG")
DRIVE_FOLDER_NAME = "Invoices (sandbox)"
SPREADSHEET_TITLE = "Kibit invoice ledger (sandbox)"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"

CONTRACTOR_DOMAINS = ("b-kft.example", "c-kft.example", "d-kft.example")
PM_ADDRESS = "projekt@kibitfinance.com"
MAILBOX = "invoice@kibitfinance.com"


def config_rows(drive_root_folder_id: str) -> list[list[str]]:
    return [
        ["key", "value"],
        ["invoice_keywords", "számla, szamla, invoice, Rechnung, díjbekérő"],
        ["attachment_mime_allowlist", "application/pdf, image/jpeg, image/png"],
        ["contractor_identifiers", ", ".join(CONTRACTOR_DOMAINS)],
        ["tig_subject_indicators", "TIG, teljesítésigazolás, teljesitesigazolas"],
        ["currency_map", "Ft=HUF, HUF=HUF, €=EUR, EUR=EUR, $=USD, USD=USD"],
        ["label_processed", LABELS[0]],
        ["label_pending", LABELS[1]],
        ["label_needs_review", LABELS[2]],
        ["label_awaiting_tig", LABELS[3]],
        ["sequence_start", "1"],
        ["sequence_width", "3"],
        ["rounding_tolerance", "0.01"],
        ["drive_root_folder_id", drive_root_folder_id],
    ]


# (scenario, pair folder, sender of the invoice, has TIG thread)
DEMO_SCENARIOS: tuple[tuple[str, str, str, bool], ...] = (
    ("direct", "TIG-2026-10-KEK-A__INV_A-2026-03", "szamlazas@a-kft.example", False),
    ("tig-match", "TIG-2026-10-ZOLD-B__INV-B_2026_02", "szamlazas@b-kft.example", True),
    ("tig-mismatch", "TIG-2026-09-KEK-C__INV-C-2026-02", "szamlazas@c-kft.example", True),
    ("missing-tig", "TIG-2026-10-PIROS-D__INV_D_2026_03", "szamlazas@d-kft.example", False),
)


def ensure_labels(gmail: Any) -> dict[str, str]:
    labels = gmail.users().labels().list(userId="me").execute()["labels"]
    existing = {lab["name"]: lab["id"] for lab in labels}
    for name in LABELS:
        if name not in existing:
            created = (
                gmail.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
            existing[name] = created["id"]
            print(f"label created: {name}")
        else:
            print(f"label exists:  {name}")
    return existing


def find_file(drive: Any, name: str, mime: str) -> str | None:
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    files = (
        drive.files()
        .list(
            q=f"name = '{escaped}' and mimeType = '{mime}' and trashed = false",
            fields="files(id)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()["files"]
    )
    if len(files) > 1:
        raise SystemExit(f"more than one '{name}' exists; remove the extras first")
    return files[0]["id"] if files else None


def ensure_drive_folder(drive: Any) -> str:
    folder_id = find_file(drive, DRIVE_FOLDER_NAME, FOLDER_MIME)
    if folder_id:
        print(f"drive folder exists: {folder_id}")
        return folder_id
    folder = (
        drive.files()
        .create(body={"name": DRIVE_FOLDER_NAME, "mimeType": FOLDER_MIME}, fields="id")
        .execute()
    )
    print(f"drive folder created: {folder['id']}")
    return str(folder["id"])


def ensure_spreadsheet(drive: Any, sheets: Any, drive_root_folder_id: str) -> str:
    spreadsheet_id = find_file(drive, SPREADSHEET_TITLE, SHEET_MIME)
    if spreadsheet_id is None:
        created = (
            sheets.spreadsheets()
            .create(
                body={
                    "properties": {"title": SPREADSHEET_TITLE, "locale": "hu_HU"},
                    "sheets": [
                        {"properties": {"title": "Ledger"}},
                        {"properties": {"title": "Config"}},
                    ],
                },
                fields="spreadsheetId",
            )
            .execute()
        )
        spreadsheet_id = str(created["spreadsheetId"])
        print(f"spreadsheet created: {spreadsheet_id}")
    else:
        print(f"spreadsheet exists: {spreadsheet_id}")

    values = sheets.spreadsheets().values()
    header = values.get(spreadsheetId=spreadsheet_id, range="'Ledger'!A1:H1").execute()
    if not header.get("values"):
        values.update(
            spreadsheetId=spreadsheet_id,
            range="'Ledger'!A1:H1",
            valueInputOption="RAW",
            body={"values": [list(LEDGER_HEADER)]},
        ).execute()
        print("ledger header written")
    values.update(
        spreadsheetId=spreadsheet_id,
        range="'Config'!A1:B14",
        valueInputOption="RAW",
        body={"values": config_rows(drive_root_folder_id)},
    ).execute()
    print("config tab written (13 keys)")
    return spreadsheet_id


def update_env(updates: dict[str, str]) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    keys_done: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            keys_done.add(key)
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in updates.items() if k not in keys_done)
    ENV_FILE.write_text("\n".join(out) + "\n")
    print(f".env updated: {', '.join(sorted(updates))}")


def _pdf(path: Path) -> tuple[str, bytes]:
    return path.name, path.read_bytes()


def _message(
    *,
    sender: str,
    to: str,
    subject: str,
    body: str,
    message_id: str,
    sent: datetime,
    attachment: tuple[str, bytes],
    in_reply_to: str | None = None,
) -> str:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = format_datetime(sent)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    name, data = attachment
    msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _exists(gmail: Any, message_id: str) -> str | None:
    found = (
        gmail.users()
        .messages()
        .list(userId="me", q=f"rfc822msgid:{message_id.strip('<>')}", includeSpamTrash=False)
        .execute()
        .get("messages", [])
    )
    return found[0]["threadId"] if found else None


def _insert(gmail: Any, raw: str, thread_id: str | None = None) -> str:
    body: dict[str, Any] = {"raw": raw, "labelIds": ["INBOX", "UNREAD"]}
    if thread_id:
        body["threadId"] = thread_id
    inserted = (
        gmail.users()
        .messages()
        .insert(userId="me", body=body, internalDateSource="dateHeader")
        .execute()
    )
    return str(inserted["threadId"])


def seed_demo_emails(gmail: Any) -> None:
    base = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
    for index, (scenario, pair, sender, has_tig) in enumerate(DEMO_SCENARIOS):
        folder = FIXTURES / pair
        tig_pdf = next(folder.glob("TIG-*.pdf"))
        invoice_pdf = next(p for p in folder.glob("*.pdf") if not p.name.startswith("TIG-"))
        tig_number = tig_pdf.stem
        invoice_mid = f"<demo-{scenario}-invoice@kibit-demo.example>"
        if _exists(gmail, invoice_mid):
            print(f"demo email exists: {scenario}")
            continue

        thread_id: str | None = None
        reply_to: str | None = None
        subject = f"Számla {invoice_pdf.stem}"
        if has_tig:
            tig_mid = f"<demo-{scenario}-tig@kibit-demo.example>"
            thread_id = _exists(gmail, tig_mid)
            if thread_id is None:
                thread_id = _insert(
                    gmail,
                    _message(
                        sender=f"Projektmenedzser <{PM_ADDRESS}>",
                        to=sender,
                        subject=f"Teljesítésigazolás {tig_number}",
                        body=(
                            "Csatolva küldöm a teljesítésigazolást. "
                            "Kérjük, ez alapján számlázzanak."
                        ),
                        message_id=tig_mid,
                        sent=base.replace(hour=8 + index),
                        attachment=_pdf(tig_pdf),
                    ),
                )
            reply_to = tig_mid
            subject = f"Re: Teljesítésigazolás {tig_number}"

        _insert(
            gmail,
            _message(
                sender=f"{sender.split('@')[1].split('.')[0].upper()} <{sender}>",
                to=MAILBOX,
                subject=subject,
                body="Mellékelten küldjük a számlát.",
                message_id=invoice_mid,
                sent=base.replace(hour=8 + index, minute=30),
                attachment=_pdf(invoice_pdf),
                in_reply_to=reply_to,
            ),
            thread_id,
        )
        print(f"demo email inserted: {scenario} ({invoice_pdf.name})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed-demo", action="store_true", help="also insert the demo emails")
    args = parser.parse_args()

    creds = local_credentials(SECRETS_DIR)
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

    address = gmail.users().getProfile(userId="me").execute()["emailAddress"]
    print(f"mailbox: {address}")
    if address != MAILBOX:
        raise SystemExit(f"token belongs to {address}, expected the sandbox mailbox {MAILBOX}")

    ensure_labels(gmail)
    folder_id = ensure_drive_folder(drive)
    spreadsheet_id = ensure_spreadsheet(drive, sheets, folder_id)
    update_env({"LEDGER_SPREADSHEET_ID": spreadsheet_id, "DRIVE_ROOT_FOLDER_ID": folder_id})
    if args.seed_demo:
        seed_demo_emails(gmail)
    print(f"ledger: https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit")
    print(f"drive:  https://drive.google.com/drive/folders/{folder_id}")


if __name__ == "__main__":
    main()
