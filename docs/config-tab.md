# Ledger spreadsheet: `Config` tab layout

The service reads its configuration once per run from the `Config` tab of the ledger
spreadsheet (`intake/clients/sheets_client.py`, `SheetsClient.load_config()`), then
validates it with `intake.config.Config.from_mapping`. Nothing here is hardcoded in the
code: edit the tab, and the next run picks the change up.

## Layout

- Columns **A** and **B** only. Anything in column C onwards is ignored.
- **Row 1 is a header**: `key` in A1, `value` in B1 (case-insensitive).
- **One row per key** from row 2 on. Row order does not matter, blank rows are skipped,
  and unknown keys are ignored. A key listed twice stops the run with an error.
- **Lists** are one cell of comma-separated values. Spaces around each item are trimmed.
- **`currency_map`** is one cell of comma-separated `symbol=ISO` pairs. The ISO code must
  be a 3-letter ISO 4217 code (it's upper-cased automatically).
- **Numbers** can be entered as numbers or as text.
- All 13 keys are required. If any is missing or blank, the run stops with one error that
  names every missing key, and no email is touched.

## Example

| key | value |
|---|---|
| `invoice_keywords` | `számla, szamla, invoice, Rechnung, díjbekérő` |
| `attachment_mime_allowlist` | `application/pdf, image/jpeg, image/png` |
| `contractor_identifiers` | `dev@contractor-one.hu, contractor-two.example` |
| `tig_subject_indicators` | `TIG, teljesítésigazolás, teljesitesigazolas` |
| `currency_map` | `Ft=HUF, HUF=HUF, €=EUR, EUR=EUR, $=USD, USD=USD` |
| `label_processed` | `Kibit/Processed` |
| `label_pending` | `Kibit/Pending` |
| `label_needs_review` | `Kibit/NeedsReview` |
| `label_awaiting_tig` | `Kibit/AwaitingTIG` |
| `sequence_start` | `1` |
| `sequence_width` | `3` |
| `rounding_tolerance` | `0.01` |
| `drive_root_folder_id` | `1AbCdEfGhIjKlMnOpQrStUvWxYz012345` |

## What each key means

| Key | Type | Meaning |
|---|---|---|
| `invoice_keywords` | list | Subject keywords that make an unread email an invoice candidate (USR-001-01). |
| `attachment_mime_allowlist` | list | Attachment MIME types that qualify an email as a candidate, and the only types sent to Gemini or Drive. |
| `contractor_identifiers` | list | Contractor senders, routed to TIG reconciliation. Use a full address (`dev@x.hu`) or a bare domain (`x.hu` / `@x.hu`) (USR-001-02). |
| `tig_subject_indicators` | list | Subject terms that route an email from an otherwise unrecognised sender to TIG. |
| `currency_map` | `symbol=ISO` pairs | Currency symbols and abbreviations mapped to ISO 4217 codes (USR-002-05). |
| `label_processed` | text | Gmail label for a filed and booked invoice with no open issue. |
| `label_pending` | text | Gmail label for a filed and booked invoice with an open TIG mismatch. |
| `label_needs_review` | text | Gmail label for an ambiguous or incomplete invoice. The service never looks at it again. |
| `label_awaiting_tig` | text | Gmail label for a contractor invoice with no TIG yet. It's re-checked every run. |
| `sequence_start` | integer ≥ 0 | First sequence number in a new month folder (USR-003-02). |
| `sequence_width` | integer ≥ 1 | Zero-padded width of the sequence part of the registry number. `sequence_start` must fit in it. |
| `rounding_tolerance` | decimal ≥ 0 | Largest invoice-vs-TIG total-net difference that still counts as a match (USR-005-01). |
| `drive_root_folder_id` | text | ID of the Drive "Invoices" root folder that holds the `YYMM` month folders. It's the last part of the folder's URL, and it can be in a Shared Drive. |

## Gmail labels must already exist

The service applies labels by name but never creates them. Before the first run, create
all four labels named in the `label_*` rows in the shared mailbox. Otherwise the first
labelling step fails with `GmailLabelNotFoundError`, and the email is left unread and
unlabelled for the next run. The candidate search excludes `label_processed`,
`label_pending` and `label_needs_review`, and deliberately not `label_awaiting_tig`.
Label names that contain spaces are quoted in the search automatically.

## The `Ledger` tab

Row 1 of the `Ledger` tab must be exactly these columns, in this order and with the same
case:

| A | B | C | D | E | F | G | H |
|---|---|---|---|---|---|---|---|
| `INV_ID_int` | `Provider` | `INV_ID_ext` | `INV_type` | `Currency` | `Net` | `Gross` | `Due date` |

The service checks this header before every dedupe read and every append. If the header
was reordered, renamed or deleted, it stops instead of writing to the wrong columns.
Columns after H are not checked.

Rows are written with `valueInputOption=RAW`. Text such as external invoice IDs keeps any
leading zeros. Net and Gross are written as numbers, and the due date as the text
`YYYY.MM.DD`.
