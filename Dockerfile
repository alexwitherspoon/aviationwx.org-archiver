# AviationWX.org Archiver
# Docker image: Python-based archiver + web GUI
#
# Build:  docker build -t aviationwx-archiver .
# Run:    docker compose up

# ---------------------------------------------------------------------------
# Stage 1 — dependency install
# ---------------------------------------------------------------------------
# Single source of truth for the pinned base (multi-arch index digest).
# Update when intentionally pulling upstream base-image updates (Python patch bumps, rebuilds, or security fixes).
ARG PYTHON_BASE=python:3.14-slim@sha256:a7185a8e40af01bf891414a4df16ef10fc6000cee460a404a13da9029fe41604

FROM ${PYTHON_BASE} AS deps

WORKDIR /build

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 — final image
# ---------------------------------------------------------------------------
FROM ${PYTHON_BASE}

ARG GIT_SHA=
ENV GIT_SHA=${GIT_SHA}

LABEL org.opencontainers.image.title="AviationWX.org Archiver" \
      org.opencontainers.image.description="Archives webcam images from AviationWX.org" \
      org.opencontainers.image.source="https://github.com/alexwitherspoon/aviationwx.org-archiver" \
      org.opencontainers.image.licenses="MIT"

# Create a non-root user for security
RUN groupadd -r archiver && useradd -r -g archiver -d /app -s /sbin/nologin archiver

# Install gosu for clean privilege drop in entrypoint
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from the deps stage
COPY --from=deps /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Copy application source (pyproject.toml needed for version display in web UI)
COPY pyproject.toml .
COPY app/ ./app/
COPY main.py .

# Create default directories; actual data should be mounted as volumes
RUN mkdir -p /archive /config \
 && chown -R archiver:archiver /archive /config /app

# Entrypoint fixes volume permissions (chown) before dropping to archiver user
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# Web GUI port
EXPOSE 8080

# Persistent storage — mount host directories to these paths
VOLUME ["/archive", "/config"]

# Health check — minimal endpoint (no archive scan)
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')" || exit 1

CMD ["python", "main.py"]
