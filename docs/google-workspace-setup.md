# Google Workspace integration setup

This guide sets up the Google credentials the pipeline needs to:

- **Gmail:** read the inbox, mark messages read, apply labels (`processed`, `pending`) and
  create draft replies (TIG mismatches).
- **Drive:** list the monthly folders, download attachments and upload renamed invoice PDFs.
- **Sheets:** read the ledger (duplicate check) and append rows (columns A–H).

> **The technical design has chosen Path A.** It's a Python 3.12 service on Cloud Run
> using an OAuth "installed app" refresh token stored in Secret Manager, with Gemini for
> extraction. Do Steps 0–3, then Path A, then Step 4. Paths B and C are kept only for
> reference.

**Do everything twice: once for the sandbox account, once for production.** The design
uses a **sandbox** Google account (staging and all tests) and the **production** shared
mailbox. Each needs its own labels, ledger sheet, Drive folder and refresh token. Tests
never touch the real mailbox.

| Path | Use it when | Keys to manage |
|---|---|---|
| **A. OAuth client + refresh token** (chosen) | The service runs as the shared mailbox user. No Workspace admin is needed. | `client_secret.json`, plus a refresh token per account |
| B. Service account + domain-wide delegation | Not used. | One service-account key (JSON) |
| C. Google Apps Script | Not used (ADR 1 picked Python on Cloud Run). | None |

---

## Step 0: Decide and collect

Write these down. You'll need them throughout.

- [ ] **Mailbox:** the Google account whose inbox receives invoices, e.g.
      `invoices@<your-domain>`. Use a dedicated shared mailbox, not a personal one.
- [ ] **Workspace admin:** who your Google Workspace super admin is. Path B needs them.
      Path A with an "Internal" app also needs the mailbox to be in your Workspace domain.
- [ ] **Drive location:** where filed invoices live. A **Shared Drive** is strongly
      recommended (see the gotchas).
- [ ] **Ledger:** the Google Sheet used as the ledger.

---

## Step 1: Create a Google Cloud project

1. Open <https://console.cloud.google.com/>, signed in with your Workspace account.
2. In the project picker at the top, choose **New project**.
   - Name: `invoice-processing`
   - Organization: your Workspace organisation, so the app can be "Internal".
3. Write down the **Project ID**.

With the gcloud CLI instead:

```bash
gcloud projects create invoice-processing-kibit --name="invoice-processing"
```

## Step 2: Enable the APIs

In **APIs & Services → Library**, enable all three:

- **Gmail API**
- **Google Drive API**
- **Google Sheets API**

Or:

```bash
gcloud services enable gmail.googleapis.com drive.googleapis.com sheets.googleapis.com --project=invoice-processing-kibit
```

(Your AI extraction model needs its own API key from its provider. That's outside this
guide.)

## Step 3: Prepare the Workspace resources

1. **Gmail labels.** In the mailbox, create the four labels from the design (ADR 3).
   Their names are also listed in the `Config` tab.
   - `Kibit/Processed`
   - `Kibit/Pending`
   - `Kibit/NeedsReview`
   - `Kibit/AwaitingTIG`
2. **Drive folder.** Create an "Invoices" root folder. The service creates the monthly
   `YYMM` subfolders under it. Copy its **folder ID** from the URL:
   `https://drive.google.com/drive/folders/<FOLDER_ID>`.
3. **Ledger sheet.** Create one spreadsheet with two tabs. The service validates this
   structure but doesn't create it.
   - `Ledger`: header row A–H exactly
     `INV_ID_int | Provider | INV_ID_ext | INV_type | Currency | Net | Gross | Due date`.
     A reordered header makes the service fail at startup.
   - `Config`: invoice keyword list, attachment MIME allowlist, contractor sender
     identifiers, TIG subject indicators, currency symbol→ISO map, sequence start and
     zero-pad width, rounding tolerance, label names, and the Drive root folder ID. The
     exact row format is defined when Task 2 (`intake/config.py`) is built.

   Copy its **spreadsheet ID** from the URL:
   `https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit`.
4. **Gemini API key.** Create one key per environment in
   [Google AI Studio](https://aistudio.google.com/apikey) or under the GCP project's
   Generative Language API. It's stored in Secret Manager (Step 4).

The Terraform setup needs these IDs as `ledger_spreadsheet_ids` and
`drive_root_folder_ids`, each with `staging` and `production` values.

### Scopes the pipeline needs

| Scope | Why |
|---|---|
| `https://www.googleapis.com/auth/gmail.modify` | Read messages and attachments, mark read, add labels, create drafts. Doesn't allow permanent deletion. |
| `https://www.googleapis.com/auth/drive` | List existing monthly folders (created by people too), download, upload. `drive.file` is **not** enough: it only sees files the app itself created. |
| `https://www.googleapis.com/auth/spreadsheets` | Read the ledger and append rows. |

Use exactly these three in every path below.

---

## Path A: OAuth client + refresh token

### A1. Configure the consent screen

In the Cloud Console, open **Google Auth Platform**. In older consoles this is
**APIs & Services → OAuth consent screen**.

1. **Branding:** app name `Invoice processing`, plus the support and developer contact emails.
2. **Audience:** user type **Internal**.
   - Internal means only users in your Workspace domain can authorise, Google doesn't need
     to verify the app, and refresh tokens don't expire on a 7-day timer.
   - If the mailbox is a consumer `@gmail.com` account, you must choose **External**. Then
     either publish the app ("In production") or keep it in "Testing" with the mailbox as
     a test user. **Refresh tokens issued in Testing expire after 7 days.**
3. **Data Access:** add the three scopes from Step 3.

### A2. Create the OAuth client

1. **Google Auth Platform → Clients → Create client.**
2. Application type: **Desktop app**. Name: `invoice-processing-local`.
3. Download the JSON and save it as `secrets/client_secret.json`. Don't commit it (see
   Step 4).

### A3. Get a refresh token (one time)

Run this once on your machine. When the browser opens, **sign in as the shared mailbox
account** (not your own account) and approve.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install google-auth google-auth-oauthlib google-api-python-client
```

```python
# scripts/authorize.py: one-time OAuth consent and refresh-token capture
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

flow = InstalledAppFlow.from_client_secrets_file("secrets/client_secret.json", SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
with open("secrets/token.json", "w") as f:
    f.write(creds.to_json())
print("Saved secrets/token.json")
```

`secrets/token.json` now holds the refresh token. The service uses it to get new access
tokens without a browser. Node's `googleapis` library has the same flow
(`OAuth2Client` + `getToken`).

Go to **Step 4**.

---

## Path B: Service account + domain-wide delegation

A service account can't read a Gmail inbox on its own. It has to **impersonate** the
mailbox user, and that takes a Workspace super admin.

### B1. Create the service account

1. **IAM & Admin → Service Accounts → Create service account.**
   Name: `invoice-pipeline`. It needs no IAM project roles.
2. Open it and copy its **Unique ID** (numeric client ID). You'll need it in B3.

### B2. Create a key

1. **Keys → Add key → Create new key → JSON.** Save it as `secrets/service-account.json`.
2. If you get *"Service account key creation is disabled"*, the organisation policy
   `iam.disableServiceAccountKeyCreation` is on. New organisations have it on by default.
   Your options:
   - Ask an org admin to make an exception for this project.
   - Run on Google Cloud (Cloud Run or Cloud Functions) with the service account attached,
     so no key file is needed.
   - Use Path A instead.

### B3. Grant domain-wide delegation (super admin)

1. Open <https://admin.google.com> → **Security → Access and data control → API controls →
   Manage Domain Wide Delegation → Add new**.
2. Client ID: the numeric Unique ID from B1.
3. OAuth scopes, comma-separated:
   ```
   https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/spreadsheets
   ```
4. Save. It can take a few minutes, occasionally up to 24 hours, to take effect.

### B4. Use it in code

```python
from google.oauth2 import service_account

creds = service_account.Credentials.from_service_account_file(
    "secrets/service-account.json", scopes=SCOPES
).with_subject("invoices@<your-domain>")  # the mailbox to impersonate
```

Because it impersonates the mailbox user, the files it uploads belong to that user, which
avoids the service-account storage problem described in the gotchas.

Go to **Step 4**.

---

## Path C: Google Apps Script

No keys or client secrets are needed. The script runs as the account that authorises it.

1. Sign in as the shared mailbox account and open the ledger sheet →
   **Extensions → Apps Script**. This makes a script bound to the sheet.
2. **Project Settings:** turn on *Show "appsscript.json" manifest file*.
3. In `appsscript.json`, declare the scopes explicitly:
   ```json
   {
     "timeZone": "Europe/Budapest",
     "oauthScopes": [
       "https://www.googleapis.com/auth/gmail.modify",
       "https://www.googleapis.com/auth/drive",
       "https://www.googleapis.com/auth/spreadsheets",
       "https://www.googleapis.com/auth/script.external_request",
       "https://www.googleapis.com/auth/script.scriptapp"
     ],
     "runtimeVersion": "V8"
   }
   ```
   `script.external_request` lets the script call the AI model's API with `UrlFetchApp`.
   `script.scriptapp` lets it create time-driven triggers.
4. **Services (+):** add **Gmail API** and **Drive API** as advanced services, needed for
   things like draft replies on a thread and Shared Drive support.
5. Run any function once and approve the consent prompt.
6. **Triggers → Add trigger:** time-driven, e.g. every 5 or 10 minutes, for the polling
   function.
7. Store the AI API key and the folder and sheet IDs in **Project Settings → Script
   Properties**, not in code.

If you want the code in this repo, use [`clasp`](https://github.com/google/clasp)
(`npm i -g @google/clasp`, `clasp login`, `clasp clone <scriptId>`).

---

## Step 4: Store secrets and configuration

### Deployed service: Secret Manager

The design's Terraform creates empty secret containers. The deployment runbook then has
you add the values, per environment:

```bash
# Refresh token from Path A3, captured while signed in as the SANDBOX account
gcloud secrets versions add kibit-oauth-refresh-token-staging --data-file=- <<< "$REFRESH_TOKEN"
# ...and again, signed in as the PRODUCTION shared mailbox
gcloud secrets versions add kibit-oauth-refresh-token-production --data-file=- <<< "$REFRESH_TOKEN"

gcloud secrets versions add kibit-gemini-api-key-staging    --data-file=- <<< "$GEMINI_API_KEY_STAGING"
gcloud secrets versions add kibit-gemini-api-key-production --data-file=- <<< "$GEMINI_API_KEY_PRODUCTION"
```

To get the bare refresh token out of `token.json`:
`python -c "import json;print(json.load(open('secrets/token.json'))['refresh_token'])"`.
The runbook mentions a `scripts/oauth_consent_flow.py` that will print it directly; that
script doesn't exist yet.

### Local development

Keep local secrets out of git:

```bash
mkdir -p secrets
printf 'secrets/\n.env\n' >> .gitignore
```

Suggested `.env` (Paths A and B):

```dotenv
GOOGLE_AUTH_MODE=oauth                     # oauth | service_account
GOOGLE_CLIENT_SECRET_FILE=secrets/client_secret.json
GOOGLE_TOKEN_FILE=secrets/token.json
GOOGLE_SERVICE_ACCOUNT_FILE=secrets/service-account.json
GOOGLE_IMPERSONATE_USER=invoices@<your-domain>
GEMINI_API_KEY=<sandbox key>
DRIVE_ROOT_FOLDER_ID=<FOLDER_ID>
LEDGER_SPREADSHEET_ID=<SPREADSHEET_ID>
LEDGER_SHEET_NAME=Ledger
```

To share credentials with teammates, use a password manager or a secret manager such as
Google Secret Manager. Never use email, chat or the repo. This is financial data, and
`gmail.modify` plus `drive` together are powerful.

## Step 5: Smoke test

This confirms all three APIs work with your credentials. Path A is shown; for Path B,
swap in the credentials from B4.

```python
# scripts/smoke_test.py
import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
creds = Credentials.from_authorized_user_file("secrets/token.json", SCOPES)

gmail = build("gmail", "v1", credentials=creds)
unread = gmail.users().messages().list(userId="me", q="is:unread in:inbox", maxResults=5).execute()
print("Unread messages:", unread.get("resultSizeEstimate", 0))
labels = gmail.users().labels().list(userId="me").execute()["labels"]
print("Labels:", [l["name"] for l in labels if l["type"] == "user"])

drive = build("drive", "v3", credentials=creds)
files = drive.files().list(
    q=f"'{os.environ['DRIVE_ROOT_FOLDER_ID']}' in parents and trashed=false",
    fields="files(id,name,mimeType)",
    supportsAllDrives=True, includeItemsFromAllDrives=True,
).execute()["files"]
print("Drive root contents:", [f["name"] for f in files])

sheets = build("sheets", "v4", credentials=creds)
header = sheets.spreadsheets().values().get(
    spreadsheetId=os.environ["LEDGER_SPREADSHEET_ID"],
    range=f"{os.environ.get('LEDGER_SHEET_NAME', 'Ledger')}!A1:H1",
).execute().get("values")
print("Ledger header:", header)
```

```bash
set -a && source .env && set +a && python scripts/smoke_test.py
```

You're done when all four prints succeed with no `403` or `404`.

---

## Gotchas

- **Service accounts have no Drive storage.** A service account that isn't impersonating
  anyone can't upload to a normal "My Drive" folder, even one shared with it. The upload
  fails with `storageQuotaExceeded`. Use a **Shared Drive** (add the service account as a
  Content manager) or impersonate a user (Path B).
- **Shared Drives need flags.** Every Drive call needs `supportsAllDrives=True`, and list
  calls also need `includeItemsFromAllDrives=True`. Without them you get empty results or
  `404`s.
- **`drive.file` is too narrow.** The pipeline scans monthly folders people created by
  hand to derive sequence numbers (USR-003-02), so it needs the full `drive` scope.
- **7-day tokens.** An External OAuth app in "Testing" status issues refresh tokens that
  expire after 7 days. Use Internal, or publish the app.
- **Scope changes need new consent.** If you add a scope later, run `authorize.py` again
  (Path A) or update the delegation entry (Path B).
- **Wrong account during consent.** Path A's token belongs to whoever signed in. If you
  approved as yourself, the pipeline reads *your* inbox. Run the script again signed in as
  the mailbox account.
- **Quotas.** The Gmail API allows 250 quota units per user per second, more than enough
  for a polling job. Sheets allows 60 write requests per minute per user, so batch your
  appends.
