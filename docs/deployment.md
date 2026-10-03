# Deployment runbook

This runbook deploys the intake service to Google Cloud. It is this repo's version of the
factory's `deployment-runbook.md`, matched to the Terraform in `infra/` and the workflow in
`.github/workflows/deploy.yml`. Run the steps in order. Each block can be copied as is,
once the variables in step 0 are set.

Everything lives in **one GCP project** (`kibit-invoice-intake`, region `europe-west1`),
with two environments separated by resource name:

| | staging | production |
|---|---|---|
| Workspace account | sandbox account | shared production mailbox |
| Cloud Run service | `kibit-intake-staging` | `kibit-intake-production` |
| Scheduler job (every minute, Europe/Budapest) | `kibit-intake-poll-staging` | `kibit-intake-poll-production` |
| Runtime SA | `kibit-intake-staging@…` | `kibit-intake-production@…` |
| Scheduler SA (the only caller `/run` accepts) | `kibit-scheduler-staging@…` | `kibit-scheduler-production@…` |
| OIDC audience (`OIDC_AUDIENCE`) | `kibit-intake-staging` | `kibit-intake-production` |
| Deployed by CI | automatically, on green `main` | after the release gate and a reviewer's approval |

Log-based metrics and alert policies come from `infra/monitoring*.tf` (Task 23) and are
applied by the same `terraform apply`.

## 0. Prerequisites

- Tools: `terraform >= 1.7`, `gcloud`, `docker`, Python 3.12.
- A GCP account with **Owner** on the project. The first apply creates service accounts,
  IAM bindings and Workload Identity Federation. Creating the budget also needs Billing
  Account Administrator (or Costs Manager) on the billing account.
- Both Workspace accounts prepared as described in
  [google-workspace-setup.md](google-workspace-setup.md), steps 0–3 and Path A1–A2. Each
  needs its labels, its ledger sheet (`Ledger` + `Config`) and its "Invoices" folder.
  `secrets/client_secret.json` must be the Desktop OAuth client.
- One AI Compass API key per environment.

Set these in your shell first:

```bash
export PROJECT_ID=kibit-invoice-intake
export REGION=europe-west1
export BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX          # gcloud billing accounts list
export GITHUB_REPO=<owner>/<repo>
export REPO_PATH="${REGION}-docker.pkg.dev/${PROJECT_ID}/kibit-intake/intake-service"
gcloud config set project "$PROJECT_ID"
gcloud auth login
gcloud auth application-default login                 # credentials Terraform uses
```

## 1. Bootstrap (once)

Link billing, then create the Terraform state bucket. A backend can't create its own
bucket, so this one is made by hand.

```bash
gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT"
gcloud services enable storage.googleapis.com cloudresourcemanager.googleapis.com serviceusage.googleapis.com

gcloud storage buckets create gs://kibit-invoice-intake-tfstate \
  --location="$REGION" --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update gs://kibit-invoice-intake-tfstate --versioning
```

Then create `infra/terraform.tfvars`. It is gitignored, and no secret values go in it.

```bash
cp infra/terraform.tfvars.example infra/terraform.tfvars
# edit: github_repository, billing_account_id (or "" to skip the budget),
#       ledger_spreadsheet_ids and drive_root_folder_ids for staging + production.
# Task 23's monitoring*.tf may add more variables (e.g. alert_email); set them too.
```

## 2. Infrastructure, in two passes

A Cloud Run service can only be created from an image that already exists. So the first
pass creates the APIs and the registry, then you push a `bootstrap` image, then the second
pass creates everything else.

```bash
cd infra
terraform init
terraform apply \
  -target=google_project_service.apis \
  -target=google_artifact_registry_repository.intake_images
cd ..

gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker build --platform=linux/amd64 -t "${REPO_PATH}:bootstrap" .
docker push "${REPO_PATH}:bootstrap"

cd infra
terraform plan -out=tfplan
terraform apply tfplan
terraform output
cd ..
```

The second pass creates, for both environments:

- the runtime, Scheduler and CI service accounts and their IAM bindings;
- the secret containers, empty for now;
- the Cloud Run services;
- the Scheduler jobs;
- the Workload Identity pool and provider;
- the monitoring resources;
- the budget, if `billing_account_id` is set.

Until step 3 is done, the scheduled runs fail with `SecretAccessError`. Nothing is
processed in the meantime, so this is harmless. To stop the noise, pause the jobs until
then:

```bash
gcloud scheduler jobs pause kibit-intake-poll-staging    --location="$REGION"
gcloud scheduler jobs pause kibit-intake-poll-production --location="$REGION"
```

## 3. Populate the secrets (once per environment)

The service reads every credential from Secret Manager at request time. Nothing is
injected as an env var. Each environment needs five secrets, and only that environment's
runtime SA can read them:

| Secret | Read by | Value |
|---|---|---|
| `kibit-oauth-refresh-token-<env>` | `intake/clients/auth.py` | refresh token of that environment's mailbox |
| `kibit-oauth-client-id-<env>` | `intake/clients/auth.py` | `installed.client_id` from `client_secret.json` |
| `kibit-oauth-client-secret-<env>` | `intake/clients/auth.py` | `installed.client_secret` from `client_secret.json` |
| `kibit-ai-compass-api-key-<env>` | `intake/extraction/claude_extractor.py` | AI Compass API key |
| `kibit-gemini-api-key-<env>` | `intake/extraction/gemini_extractor.py` | optional; only for `EXTRACTOR_BACKEND=gemini` |

First get one refresh token per mailbox. When the browser opens, sign in as **that
mailbox**, not as yourself.

```bash
pip install google-auth-oauthlib                     # dev dependency, needed for --authorize
python -m intake --authorize                         # sign in as the SANDBOX account   -> secrets/token.json
mkdir -p secrets/production && cp secrets/client_secret.json secrets/production/
python -m intake --authorize --secrets-dir secrets/production   # sign in as the PRODUCTION mailbox
```

Then store the values. `printf '%s'` adds no trailing newline. The app strips whitespace
anyway.

```bash
# field FILE KEY [KEY...]: print a nested JSON value without a trailing newline
field() { python3 -c 'import json,sys,functools; print(functools.reduce(lambda d,k: d[k], sys.argv[2:], json.load(open(sys.argv[1]))), end="")' "$@"; }

for ENV in staging production; do
  printf '%s' "$(field secrets/client_secret.json installed client_id)" \
    | gcloud secrets versions add "kibit-oauth-client-id-${ENV}" --data-file=-
  printf '%s' "$(field secrets/client_secret.json installed client_secret)" \
    | gcloud secrets versions add "kibit-oauth-client-secret-${ENV}" --data-file=-
done

printf '%s' "$(field secrets/token.json refresh_token)" \
  | gcloud secrets versions add kibit-oauth-refresh-token-staging --data-file=-
printf '%s' "$(field secrets/production/token.json refresh_token)" \
  | gcloud secrets versions add kibit-oauth-refresh-token-production --data-file=-

read -rs AI_COMPASS_KEY && printf '%s' "$AI_COMPASS_KEY" \
  | gcloud secrets versions add kibit-ai-compass-api-key-staging --data-file=-
read -rs AI_COMPASS_KEY && printf '%s' "$AI_COMPASS_KEY" \
  | gcloud secrets versions add kibit-ai-compass-api-key-production --data-file=-
unset AI_COMPASS_KEY

# Optional, only if a service is switched to EXTRACTOR_BACKEND=gemini:
# read -rs GEMINI_KEY && printf '%s' "$GEMINI_KEY" | gcloud secrets versions add kibit-gemini-api-key-staging --data-file=-
```

Check that every required secret has an enabled version:

```bash
for ENV in staging production; do for S in oauth-refresh-token oauth-client-id oauth-client-secret ai-compass-api-key; do
  printf '%-45s ' "kibit-${S}-${ENV}"; gcloud secrets versions list "kibit-${S}-${ENV}" --filter=state=ENABLED --format='value(name)' --limit=1 | grep -q . && echo ok || echo MISSING
done; done
```

`gcloud secrets versions add` always creates a new version, and the service reads
`latest`. To rotate a credential, add a new version, then disable the old one.

## 4. GitHub configuration (once)

`deploy.yml` authenticates through Workload Identity Federation, so there are no JSON
keys. Copy the values from `terraform -chdir=infra output`.

1. Repository **variables** (Settings → Secrets and variables → Actions → Variables):
   - `GCP_PROJECT_ID` = `kibit-invoice-intake`
   - `WIF_PROVIDER` = output `workload_identity_provider`
   - `CI_DEPLOYER_SA_EMAIL` = output `ci_deployer_service_account_email`
   - `CI_E2E_SA_EMAIL` = output `ci_e2e_service_account_email`
2. **Environments** (Settings → Environments):
   - `staging`: no protection rules. Add the variables `LEDGER_SPREADSHEET_ID` and
     `DRIVE_ROOT_FOLDER_ID` with the sandbox IDs, for the release gate.
   - `production`: **required reviewers**. This is the pipeline's only manual gate. Limit
     deployment branches to `main`.
   - The production deploy is **switched off** until the repository variable
     `ENABLE_PRODUCTION_DEPLOY` is `true`. Until then a push on `main` deploys to staging
     and runs the release gate with no approval step.
3. Branch protection on `main`: require the `CI` workflow's `lint` and `unit-tests` jobs.

The WIF provider only accepts tokens from `$GITHUB_REPO` workflows running on
`refs/heads/main`.

## 5. Authenticated smoke test

Both services are IAM-protected: only the Scheduler SA and the CI deployer hold
`roles/run.invoker`. An unauthenticated `curl …/health` therefore gets **403**, which is
expected. Send an identity token instead. A project Owner may invoke any service, and a
user's identity token comes from plain `print-identity-token`. `--audiences` only works
for service accounts.

```bash
for ENV in staging production; do
  URL="$(gcloud run services describe "kibit-intake-${ENV}" --region="$REGION" --format='value(status.url)')"
  echo "== ${ENV}: ${URL}"
  curl -s -w ' %{http_code}\n' -H "Authorization: Bearer $(gcloud auth print-identity-token)" "${URL}/health"    # {"status":"ok"} 200
  curl -s -o /dev/null -w 'no token: %{http_code}\n' "${URL}/health"                                            # 403 (IAM)
  curl -s -w ' %{http_code}\n' -X POST -H "Authorization: Bearer $(gcloud auth print-identity-token)" "${URL}/run" # {"error":"unauthorized"} 401
done
```

The last call proves the app's own check. Even a caller that Cloud Run IAM lets through
is refused unless its token was minted for the Scheduler SA with the configured audience.

To test as the CI identity instead (needs `roles/iam.serviceAccountTokenCreator` on it):
`gcloud auth print-identity-token --impersonate-service-account="$(terraform -chdir=infra output -raw ci_deployer_service_account_email)" --audiences="$URL" --include-email`.

## 6. Trigger one scheduled run (staging first)

```bash
gcloud scheduler jobs resume kibit-intake-poll-staging --location="$REGION"   # if paused in step 2
gcloud scheduler jobs run    kibit-intake-poll-staging --location="$REGION"

# The job's last attempt (status code 0 = OK):
gcloud scheduler jobs describe kibit-intake-poll-staging --location="$REGION" --format='value(lastAttemptTime,status)'

# The run's logs and summary ("POST /run completed" carries the per-outcome counts):
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="kibit-intake-staging"' \
  --freshness=15m --limit=50 --format='value(timestamp,severity,textPayload,jsonPayload.message)'
```

Then check the sandbox account:

- a booked candidate is marked read and labelled `Kibit/Processed` (or `Pending`,
  `NeedsReview` or `AwaitingTIG`);
- its PDF is in `Invoices/<YYMM>/` under its registry number;
- the `Ledger` tab has one new row.

When staging looks right, run the full release gate. Run it locally or let CI run it in
step 7:

```bash
gcloud scheduler jobs pause kibit-intake-poll-staging --location="$REGION"
pytest -m e2e tests/e2e/fixture_set -v
python scripts/verify_fixture_answer_key.py --strict --require-run --results tests/e2e/results/<run-id>.json
gcloud scheduler jobs resume kibit-intake-poll-staging --location="$REGION"
```

Resume production only after that:
`gcloud scheduler jobs resume kibit-intake-poll-production --location="$REGION"`.

## 7. Continuous deployment

`.github/workflows/deploy.yml` starts when `CI` succeeds for a push to `main`, or by hand
with *Run workflow*:

1. **build-and-push**: builds the image once, tagged with the commit SHA, and deploys it
   by digest.
2. **deploy-staging**:
   - `gcloud run services update kibit-intake-staging --image <digest>`, then
     `update-traffic --to-latest`;
   - an authenticated smoke test: `/health` with a token returns 200, without a token
     401/403, and `POST /run` as CI returns 401.
3. **release-gate**, as `kibit-ci-e2e`, fail-closed:
   - pauses `kibit-intake-poll-staging` (custom role `kibitSchedulerPauser`), so the
     staging service doesn't book the seeded emails into the demo ledger;
   - `pytest -m e2e tests/e2e/fixture_set` with `KIBIT_E2E_REQUIRED=1` and
     `KIBIT_E2E_PAIRS=smoke`: 7 of the 32 pairs (one per outcome x currency x month),
     run twice against a fresh ledger and folder. The sandbox OAuth credentials and the
     AI Compass key come from the staging secrets, which that SA can read. Anything that
     would skip the suite fails it;
   - `scripts/verify_fixture_answer_key.py --strict --require-run` on the run's results;
   - resumes the staging job if it was enabled before.

   Each release costs a few minutes and ~14 AI Compass calls. The run's
   "Kibit E2E ledger <run-id>" sheet and "Invoices E2E <run-id>" folder are moved to the
   sandbox Drive trash when it passes, and kept when it fails (`KIBIT_E2E_KEEP=1` keeps
   them always).
4. **deploy-production**: only when the repository variable `ENABLE_PRODUCTION_DEPLOY`
   is `true` (off for now; the job shows as skipped). Waits for approval on the
   `production` Environment, then promotes **the same digest** and runs the same smoke
   test.

CI changes only the image. Everything else, including env vars, scaling, IAM, the
Scheduler and secrets, changes through `terraform apply` from an operator's machine.
Terraform ignores the image, so it never reverts a CI deploy.

## 8. Rollback

The usual case is a bad code deploy. Redeploy the previous good image. That keeps traffic
on "latest", which matches Terraform.

```bash
ENV=production
gcloud run revisions list --service="kibit-intake-${ENV}" --region="$REGION" \
  --format='table(metadata.name,metadata.creationTimestamp,spec.containers[0].image)'
gcloud run services update "kibit-intake-${ENV}" --region="$REGION" \
  --image="${REPO_PATH}@sha256:<previous-good-digest>"
```

In an emergency, you can instead re-point traffic to an earlier revision. This is
instant, with no new revision:

```bash
gcloud run services update-traffic "kibit-intake-${ENV}" --region="$REGION" \
  --to-revisions=<previous-good-revision>=100
```

A traffic pin is temporary. The next CI deploy, or a `terraform apply`, which declares
100% to latest, sends traffic back to the newest revision. Fix forward on `main` before
then.

To stop processing altogether, for example while investigating, pause the job:
`gcloud scheduler jobs pause kibit-intake-poll-${ENV} --location="$REGION"`.

**Bad Config-tab edit.** No infra rollback is needed. Restore the tab from the Sheet's
version history (File → Version history), because Config is deliberately outside the
deploy pipeline (ADR 6). Drive and Sheets version history are also the restore path for
filed invoices and ledger rows (no DR region, MVP decision).

## Runtime model and known risks

**Concurrency.** The pipeline derives sequence numbers and the duplicate check from live
Drive/Sheets contents, so it assumes **one run at a time per mailbox** (ADR 5). The
infrastructure enforces this as far as Cloud Run allows:

- `max_instance_count = 1`, `max_instance_request_concurrency = 1`, and gunicorn has 1
  worker and 1 thread. A second `POST /run` during a run is queued briefly and then
  rejected (429). It is not run in parallel.
- The Scheduler has `retry_count = 0`. A failed or timed-out attempt is never retried while
  the original may still be running. The next 10-minute tick picks up whatever was left.
  Nothing is labelled until it succeeds, so that is safe.
- A manual `gcloud scheduler jobs run` during a run gets the same 429.

Remaining overlap risk, not closed by infrastructure:

- **During a deploy**, the old revision can still be finishing a run while the new one
  receives the next tick. Cloud Run may briefly exceed `max_instance_count` across
  revisions. Avoid deploying while a run is in progress, or pause the job around a
  production deploy.
- **Local or E2E runs against the same mailbox.** `python -m intake` or the release-gate
  suite against the sandbox account can overlap a scheduled staging run. Pause
  `kibit-intake-poll-staging` while running them by hand. The CI release gate pauses
  and resumes it itself.
- Closing these properly is the deferred scaling step: a per-month lock, e.g. Firestore
  with Cloud Tasks (ADR 5).

**Timeouts.**

- Cloud Run's request timeout is `request_timeout_seconds` = 3600 s, the Cloud Run
  maximum.
- A live run measured about 50 s for 4 invoices, roughly 8–12 s per invoice including TIG
  extraction, so about 300 invoices fit in one run.
- gunicorn's worker timeout is set from the same variable (`GUNICORN_TIMEOUT`).
- Cloud Scheduler waits at most 1800 s, its maximum for HTTP targets. A longer run keeps
  going, but the job records that attempt as `DEADLINE_EXCEEDED`.
- No liveness probe is configured. The single busy worker couldn't answer it, and Cloud
  Run would kill the instance mid-run. A startup probe on `/healthz` gates new revisions.

**Egress.** The default is direct internet egress, with no VPC connector. The service needs
outbound HTTPS to:

- Google APIs: `gmail`, `drive`, `sheets`, `secretmanager`, and `oauth2.googleapis.com`
  for token refresh;
- the AI Compass gateway at `https://ai-compass.kibit.cloud`.

If an egress restriction is added later, both must stay allowed.

**Scale to zero.** `min_instance_count = 0`. An instance runs only while a request is being
served. CPU is allocated per request (`cpu_idle = true`).

## Deviations from the factory Terraform and pipeline

| Factory (`iac-main.tf`, `cicd-pipeline.yml`) | Here | Why |
|---|---|---|
| `OAUTH_REFRESH_TOKEN` and `GEMINI_API_KEY` injected as `secret_key_ref` env vars | No secret env vars. The app reads Secret Manager at runtime | `intake/clients/auth.py` and the extractors read the secrets by name. Credentials never appear in the revision config |
| Secrets: refresh token + Gemini key | + OAuth client ID, client secret, AI Compass key; Gemini kept, optional | Task 3 auth design; ADR 4 revised (AI Compass) |
| `roles/aiplatform.user` always granted | Behind `enable_vertex_ai_user`, default `false` | Extraction uses AI Compass, and the Gemini backend uses an API key, not Vertex AI. Least privilege |
| Scheduler audience = service `.uri` | Fixed `kibit-intake-<env>`, used as the Cloud Run `custom_audiences` entry and as `OIDC_AUDIENCE` | The service can't put its own URI in its env (cycle). The app verifies the audience exactly |
| `OIDC_AUDIENCE` / `SCHEDULER_SERVICE_ACCOUNT_EMAIL` not set | Set | `intake/app.py` fails closed (401) without them |
| `max_instances = 2`, default concurrency (80), timeout 540 s | 1 instance, concurrency 1, 3600 s | Single-run assumption (ADR 5). Sized for ≥ 100 invoices |
| Scheduler `retry_count = 1`, default attempt deadline | `retry_count = 0`, `attempt_deadline = 1800s` | A retry could overlap the still-running first attempt |
| `AWAITING_TIG_ALERT_DAYS` env var | Not set | The app doesn't read it. Staleness alerting belongs to Task 23 |
| Env vars for extractor: none | `EXTRACTOR_BACKEND=ai_compass`, `AI_COMPASS_BASE_URL`, `AI_COMPASS_MODEL`, `AI_COMPASS_EFFORT` | ADR 4 revised |
| `/healthz` smoke test with unauthenticated `curl` | Identity-token smoke test on `/health` (Cloud Run's front end 404s public paths ending in `z`). CI deployer holds `run.invoker` | The service is IAM-protected, so anonymous requests get 403 |
| CI deployer: AR writer + `run.developer` | + `iam.serviceAccountUser` on each runtime SA, + `run.invoker` | Deploying a revision that runs as the runtime SA needs `actAs`. `run.invoker` is for the smoke test |
| WIF condition: repository only | Repository **and** `refs/heads/main` | Feature-branch workflows can't deploy |
| CI deploy: `terraform apply -target=…intake["<env>"]` with `image_tags` | `gcloud run services update --image <digest>`. Terraform `ignore_changes` on the image | CI needs no state-bucket access, no tfvars and no `-target`. The factory step also read a nonexistent `production_image_tag` output, and `-target` still requires every variable |
| E2E gate before build, with the CI deployer reading secrets | Gate after the staging deploy, before production. Separate `kibit-ci-e2e` SA with staging-only secret access | Task 24 validates the *deployed* pipeline against the sandbox, which is staging. The security checklist forbids secret access for the deployer |
| Canary `/run` in production (`scripts/canary_run.py`) | Dropped | The script doesn't exist, and only the Scheduler SA may call `/run` |
| `monitoring_notification_channel` and alert policies in main | Not here | Owned by Task 23 (`infra/monitoring*.tf`) |
| Budget: `projects/<id>`, notification channel, required `billing_account_id` | `projects/<number>`, default IAM recipients, created only if `billing_account_id` is set | The Budgets API keys on project number. The channel is Task 23's. Lets the apply run without billing-admin rights |
| APIs | + `gmail`, `drive`, `sheets`, `iam`. `aiplatform` only with Vertex | The OAuth client lives in this project |
| — | Startup probe on `/healthz`, no liveness probe, gen2, `cpu_idle` | See "Timeouts" |

## Open items needing a human

- Create or confirm the GCP project `kibit-invoice-intake`, link billing, and run steps 1–2
  as an Owner. Decide whether to set `billing_account_id`, which needs billing-account
  rights.
- Steps 3–4: the OAuth consent for both mailboxes, the AI Compass keys, the GitHub
  variables, the `staging` and `production` Environments with reviewers, and branch
  protection.
- The release gate runs this commit's pipeline on the runner against the sandbox, not
  the deployed staging revision (`/run` accepts only the Scheduler SA). Both are built
  from the same commit.
- Security checklist follow-ups, not done here:
  - Artifact Registry vulnerability scanning and a cleanup policy (FinOps);
  - `pip-audit` in CI;
  - Dependabot;
  - a 6-monthly credential rotation reminder.
- Verify the first CI run end to end. Nothing here was applied to a real project: Terraform
  passed `validate` only, and the workflow passed `actionlint` only.
