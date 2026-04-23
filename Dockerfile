FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/static/avatars

EXPOSE 8001

# Run via the uvicorn CLI so the app is imported as module "watch_app" (not "__main__").
# Routes do `from watch_app import X` — if the app were run as __main__, Python would
# import a second copy of watch_app.py for those imports and their module-level
# attributes (eview_mqtt_service, db_manager, ...) would be separate from the ones
# set by the lifespan.
CMD ["uvicorn", "watch_app:app", "--host", "0.0.0.0", "--port", "8001", "--log-level", "info"]
