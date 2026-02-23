# AviationWX.org Archiver
# Docker image: Python-based archiver + web GUI
#
# Build:  docker build -t aviationwx-archiver .
# Run:    docker compose up

# ---------------------------------------------------------------------------
# Stage 1 — dependency install
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS deps

WORKDIR /build

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 — final image
# ---------------------------------------------------------------------------
FROM python:3.14-slim

LABEL org.opencontainers.image.title="AviationWX.org Archiver" \
      org.opencontainers.image.description="Archives webcam images from AviationWX.org" \
      org.opencontainers.image.source="https://github.com/alexwitherspoon/aviationwx.org-archiver" \
      org.opencontainers.image.licenses="MIT"

# Create a non-root user for security
RUN groupadd -r archiver && useradd -r -g archiver -d /app -s /sbin/nologin archiver

WORKDIR /app

# Copy installed packages from the deps stage
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Copy application source
COPY app/ ./app/
COPY main.py .

# Create default directories; actual data should be mounted as volumes
RUN mkdir -p /archive /config \
 && chown -R archiver:archiver /archive /config /app

USER archiver

# Web GUI port
EXPOSE 8080

# Persistent storage — mount host directories to these paths
VOLUME ["/archive", "/config"]

# Health check — poll the JSON status endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/status')" || exit 1

CMD ["python", "main.py"]
