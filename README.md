# AviationWX.org Archiver

A Docker-based utility that periodically fetches and archives all webcam images from [AviationWX.org](https://aviationwx.org), organising them on disk by date and airport. Includes a local web GUI for configuration, monitoring, and browsing the archived content.

Part of the [AviationWX.org](https://github.com/alexwitherspoon/aviationwx) project.

## Features

- **Automated archiving** — fetches images from all or selected airports on a configurable schedule
- **Organised storage** — files saved as `archive/YYYY/MM/DD/AIRPORT_CODE/filename`
- **Single config file** — one YAML file drives the entire system; mount it into the container
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

Then run with your config and archive volume (see docker-compose.yml).

### Option B: Build from source

```bash
# 1. Clone the repository
git clone https://github.com/alexwitherspoon/aviationwx.org-archiver.git
cd aviationwx.org-archiver

# 2. Create your config file from the example
cp config/config.yaml.example config/config.yaml
# Edit config/config.yaml to set airports and schedule

# 3. Start the container
docker compose up -d

# 4. Open the web GUI
open http://localhost:8080
```

Or use Make:

```bash
make up        # copies example config if needed and starts the container
make logs      # tail container logs
make down      # stop the container
```

## Configuration

All settings live in a single YAML file. Copy the annotated example:

```bash
cp config/config.yaml.example config/config.yaml
```

Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `archive.output_dir` | `/archive` | Where images are saved inside the container |
| `archive.retention_days` | `0` | Days to keep files (0 = unlimited) |
| `schedule.interval_minutes` | `15` | How often to fetch new images |
| `schedule.fetch_on_start` | `true` | Run an immediate fetch on container start |
| `airports.archive_all` | `false` | Archive every airport on AviationWX.org |
| `airports.selected` | `[KSPB, KAWO]` | Specific airport codes when archive_all is false |
| `web.port` | `8080` | Web GUI port |
| `logging.level` | `INFO` | Log verbosity: DEBUG, INFO, WARNING, ERROR |

The config file can be passed into Docker via a bind mount (see `docker-compose.yml`).

## Archive Layout

```
archive/
└── 2024/
    └── 06/
        └── 15/
            ├── KSPB/
            │   ├── 20240615_143000_webcam.jpg
            │   └── 20240615_150000_webcam.jpg
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

The container runs as a non-root user. Persist data by mounting host directories:

| Host path (your choice) | Container path | Purpose |
|-------------------------|----------------|---------|
| `./archive` or `/mnt/user/aviationwx/archive` | `/archive` | Archived images (required for persistence) |
| `./config/config.yaml` | `/config/config.yaml` | Config file (read/write — web GUI saves changes) |

**Example with pre-built image:**

```bash
# 1. Create config (clone repo or download config.yaml.example)
mkdir -p config archive
cp config/config.yaml.example config/config.yaml
# Edit config/config.yaml to set airports and schedule

# 2. Run with volume mounts — replace /path/on/host with your paths
docker run -d \
  --name aviationwx-archiver \
  -p 8080:8080 \
  -v /path/on/host/archive:/archive \
  -v /path/on/host/config/config.yaml:/config/config.yaml \
  -e ARCHIVER_CONFIG=/config/config.yaml \
  ghcr.io/alexwitherspoon/aviationwx.org-archiver:latest
```

On Unraid or similar, use your data path (e.g. `/mnt/user/appdata/aviationwx/archive`).

**Docker Compose** (when using the repo):

```yaml
volumes:
  - ./archive:/archive
  - ./config/config.yaml:/config/config.yaml  # read/write so web GUI can save
```

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
