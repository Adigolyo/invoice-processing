FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY intake/ intake/

# One worker, one thread: the pipeline is strictly sequential (ADR 5). No gunicorn timeout;
# Cloud Run's request timeout bounds a run. Task 22 finalises this image.
CMD exec gunicorn --bind ":${PORT}" --workers 1 --threads 1 --timeout 0 intake.app:app
