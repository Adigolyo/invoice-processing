FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY intake/ intake/

# No HTTP entrypoint yet; Task 3 adds intake/app.py and Task 22 finalises this image.
CMD ["python", "-c", "import intake; print(f'intake {intake.__version__}')"]
