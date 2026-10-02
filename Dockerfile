# Kibit invoice intake service: production image for Cloud Run (Task 22).
#
# Base image pinned by digest (multi-arch index of python:3.12-slim); bump it deliberately.
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    GUNICORN_TIMEOUT=3600

WORKDIR /app

# Runtime dependencies only (pinned in requirements.txt); dev tools stay out of the image.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY intake/ intake/

# Unprivileged runtime user; the app writes nothing to disk.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin intake
USER 10001

EXPOSE 8080

# One worker, one thread: the pipeline is strictly sequential (ADR 5) and Cloud Run sends
# at most one request at a time (max_instance_request_concurrency = 1). The sync worker
# is busy for the whole run, so its timeout must cover a full run: GUNICORN_TIMEOUT
# matches the Cloud Run request timeout (Terraform sets both from request_timeout_seconds).
# PORT is injected by Cloud Run. No control socket: the user has no home directory.
CMD exec gunicorn --bind ":${PORT}" --workers 1 --threads 1 --timeout "${GUNICORN_TIMEOUT}" --graceful-timeout 10 --no-control-socket --access-logfile - intake.app:app
