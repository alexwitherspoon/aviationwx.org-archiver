# AviationWX.org Archiver

A Docker-based utility that periodically fetches and archives all webcam images from [AviationWX.org](https://aviationwx.org), organising them on disk by date and airport. Includes a local web GUI for configuration, monitoring, and browsing the archived content.

Part of the [AviationWX.org](https://github.com/alexwitherspoon/aviationwx) project.

## Features

- **Automated archiving** — fetches images from all or selected airports on a configurable schedule
- **History API mode** — uses AviationWX history API to download all available frames, only missing ones; run every 15 min captures ~15 images per webcam (60s refresh)
- **Organised storage** — files saved as `archive/YYYY/MM/DD/AIRPORT_CODE/filename`
- **Single config file** — one YAML file drives the entire system (stored in named volume)
- **Environment variable overrides** — configure via `ARCHIVER_*` env vars
- **Web GUI** — local dashboard for monitoring, configuration, and browsing the archive
- **Retention policy** — optional automatic cleanup of files older than N days
- **Minimal dependencies** — Python + Flask + Requests + PyYAML + APScheduler
- **Docker-first** — simple `docker compose up` to get started

## Quick Start

### Option A: Pull pre-built image (recommended)

```bash
# Bleeding-edge (latest main) — use until first release
docker pull ghcr.io/alexwitherspoon/aviationwx.org-archiver:edge

# Latest stable — available after first tagged release (e.g. v1.0.0)
docker pull ghcr.io/alexwitherspoon/aviationwx.org-archiver:latest
```

Then run with volumes (see Docker section below).

### Option B: Build from source

```bash
# 1. Clone the repository
git clone https://github.com/alexwitherspoon/aviationwx.org-archiver.git
cd aviationwx.org-archiver

# 2. Start the container (archive stored in ./archive, config in named volume)
docker compose up -d

# 3. Open the web GUI to configure airports and schedule
open http://localhost:8080
```

On first run, the app uses defaults. Configure via the web GUI (saved to the config volume) or via environment variables (see below).

Or use Make:

```bash
make up        # starts the container
make logs      # tail container logs
make down      # stop the container
```

## Configuration

Config is stored in the `config_data` named volume. On first run, the app uses defaults. Configure via the web GUI (saved to the volume) or via `ARCHIVER_*` environment variables.

Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `archive.output_dir` | `/archive` | Where images are saved inside the container |
| `archive.retention_days` | `0` | Days to keep files (0 = unlimited) |
| `schedule.interval_minutes` | `15` | How often to fetch new images |
| `schedule.fetch_on_start` | `true` | Run an immediate fetch on container start |
| `schedule.job_timeout_minutes` | `30` | Max minutes per run; next run resumes from where it stopped |
| `airports.archive_all` | `false` | Archive every airport on AviationWX.org |
| `airports.selected` | `[KSPB, KAWO]` | Specific airport codes when archive_all is false |
| `source.use_history_api` | `true` | Use history API to fetch all frames, download only missing; `false` = current image only per run |
| `source.api_key` | `""` | Partner API key (optional). Enables 500 req/min vs 100/min anonymous. |
| `source.request_delay_seconds` | `1.2` | Delay before each API request (half of 100/min anonymous; set 0 for Partner keys) |
| `web.enabled` | `true` | Set to `false` in secure environments to run scheduler only (no web UI) |
| `web.port` | `8080` | Web GUI port |
| `logging.level` | `INFO` | Log verbosity: DEBUG, INFO, WARNING, ERROR |

### Environment variable overrides

Any config setting can be overridden via `ARCHIVER_*` environment variables. Env vars take precedence over the config file.

| Env var | Maps to | Example |
|---------|---------|---------|
| `ARCHIVER_ARCHIVE_OUTPUT_DIR` | `archive.output_dir` | `/archive` |
| `ARCHIVER_ARCHIVE_RETENTION_DAYS` | `archive.retention_days` | `30` |
| `ARCHIVER_SCHEDULE_INTERVAL_MINUTES` | `schedule.interval_minutes` | `15` |
| `ARCHIVER_SCHEDULE_FETCH_ON_START` | `schedule.fetch_on_start` | `true` |
| `ARCHIVER_SOURCE_API_KEY` | `source.api_key` | `your-key` |
| `ARCHIVER_AIRPORTS_ARCHIVE_ALL` | `airports.archive_all` | `false` |
| `ARCHIVER_AIRPORTS_SELECTED` | `airports.selected` | `KSPB,KAWO` |
| `ARCHIVER_WEB_ENABLED` | `web.enabled` | `true` |
| `ARCHIVER_LOGGING_LEVEL` | `logging.level` | `INFO` |

Booleans: `true`, `false`, `1`, `0`, `yes`, `no`. Lists: comma- or newline-separated (e.g. `KSPB,KAWO`).

**Example — configure entirely via env vars:**

```bash
mkdir -p archive
docker run -d \
  -p 8080:8080 \
  -v $(pwd)/archive:/archive \
  -v config_data:/config \
  -e ARCHIVER_AIRPORTS_SELECTED=KSPB,KAWO \
  -e ARCHIVER_SCHEDULE_INTERVAL_MINUTES=15 \
  -e ARCHIVER_SOURCE_API_KEY=your-partner-key \
  ghcr.io/alexwitherspoon/aviationwx.org-archiver:latest
```

## Archive Layout

```
archive/
└── 2024/
    └── 06/
        └── 15/
            ├── KSPB/
            │   ├── 20240615_143000_webcam.jpg
            │   ├── 20240615_150000_webcam.jpg
            │   └── 1718456780_0.jpg   # history mode: {timestamp}_{cam}.jpg
            └── KAWO/
                └── 20240615_143001_snapshot.webp
```

## Web GUI

The local web interface (default `http://localhost:8080`) provides:

- **Dashboard** — archive statistics, last/next run times, live log stream, manual trigger
- **Browse** — explore archived images by year → month → day → airport
- **Config** — edit all settings through a form (no file editing needed)
- **API** — `GET /api/status` returns JSON status for health checks and monitoring

## Development

```bash
# Install dependencies (Python 3.12+)
pip install -r requirements.txt -r requirements-dev.txt

# Run tests
make test

# Run locally without Docker
make dev
```

## Docker

Pre-built images are published to GitHub Container Registry:

```bash
# Bleeding-edge (latest main) — use this until first release
docker pull ghcr.io/alexwitherspoon/aviationwx.org-archiver:edge

# Latest stable — available after first tagged release (e.g. v1.0.0)
docker pull ghcr.io/alexwitherspoon/aviationwx.org-archiver:latest
```

Or build from source:

```bash
# Build the image
make build

# Or build and run with Docker Compose
make up
```

The container runs as a non-root user. Archive images are stored on the host via a bind mount; config uses a named volume.

| Mount | Purpose |
|-------|---------|
| `./archive:/archive` | Archived images (host bind mount) |
| `config_data:/config` | Config file (named volume; web GUI saves changes here) |

**Example with pre-built image:**

```bash
# Create archive directory on host
mkdir -p archive

docker run -d \
  --name aviationwx-archiver \
  -p 8080:8080 \
  -v $(pwd)/archive:/archive \
  -v config_data:/config \
  -e ARCHIVER_AIRPORTS_SELECTED=KSPB,KAWO \
  ghcr.io/alexwitherspoon/aviationwx.org-archiver:latest
```

### Unraid

1. **Docker** → **Add Container**
2. **Name:** `aviationwx-archiver`
3. **Repository:** `ghcr.io/alexwitherspoon/aviationwx.org-archiver:latest` (or `:edge` for latest main)
4. **Network Type:** Bridge
5. **Port:** Host `8080` → Container `8080`
6. **Volume mappings:**
   - Host: `/mnt/user/appdata/aviationwx/archive` → Container: `/archive`
   - Add a path for config (optional): Host: `/mnt/user/appdata/aviationwx/config` → Container: `/config`
7. **Environment variables** (optional; or configure via web GUI after first start):
   - `ARCHIVER_AIRPORTS_SELECTED` = `KSPB,KAWO` (comma-separated airport codes)
   - `ARCHIVER_SCHEDULE_INTERVAL_MINUTES` = `15`
8. **Apply** and start the container.
9. Open `http://your-unraid-ip:8080` to configure airports and schedule.

Create the host paths first (e.g. `mkdir -p /mnt/user/appdata/aviationwx/archive` via Unraid terminal or a share).

## Requirements

- Docker and Docker Compose (for containerised use), **or**
- Python 3.12+ (for local development)

## Project Structure

```
aviationwx.org-archiver/
├── app/
│   ├── archiver.py        # image fetching and archival logic
│   ├── config.py          # YAML config loader/saver
│   ├── scheduler.py       # APScheduler background job
│   ├── web.py             # Flask web GUI
│   └── templates/         # HTML templates
│       ├── base.html
│       ├── dashboard.html
│       ├── config.html
│       └── browse.html
├── config/
│   └── config.yaml.example
├── tests/
│   └── test_archiver.py
├── Dockerfile
├── docker-compose.yml
├── Makefile
├── main.py                # application entry point
└── requirements.txt
```

## License

MIT License — see [LICENSE](LICENSE)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines. Please read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) before participating.

## Related Projects

- [AviationWX.org](https://github.com/alexwitherspoon/aviationwx) — the main platform this tool archives

---

**Made for pilots, by pilots** ✈️
