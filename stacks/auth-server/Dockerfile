# syntax=docker/dockerfile:1
#
# VPS Auth- & Licensing-Server für Sigma Gamma Labs
#
# Läuft im Tailnet unter https://vps.tail5c3478.ts.net:8100
# Daten liegen persistent unter /app/data (gemountet von /srv/apps/auth-server/data)

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Berlin

# Zeitzonen-Datenbank installieren
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Abhängigkeiten zuerst cachen
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Anwendungs-Code kopieren
COPY app/ ./app/

# Unprivilegierter App-Benutzer (UID 10001 - Standard der VPS-Plattform)
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data \
    && chown -R app:app /app

USER app

EXPOSE 8100

# Früher Integritäts-Check beim Image-Bau
RUN python -c "import app.main"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100", "--proxy-headers", "--forwarded-allow-ips", "*"]
