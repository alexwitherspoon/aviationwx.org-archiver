# GitHub Copilot Instructions — AviationWX.org Archiver

Repository custom instructions for GitHub Copilot coding agent and code review. Focus on **repo health** — tests, config validation, structure. Not for biasing code generation.

---

## Project Overview

**AviationWX.org Archiver** is a Docker utility that periodically fetches and archives webcam images from AviationWX.org. It provides a local web GUI for configuration, monitoring, and browsing archived content.

### Technology Stack

- **Backend**: Python 3.12, Flask
- **Scheduler**: APScheduler
- **Testing**: pytest, ruff (lint + format)
- **Infrastructure**: Docker, multi-arch (amd64, arm64, arm/v7)

---

## Build & Validation

**Always run `make test-ci` before committing.** This matches CI.

### Commands

| Purpose | Command |
|---------|---------|
| **Full validation** | `make test-ci` |
| **Lint only** | `make lint` |
| **Format code** | `make format` |
| **Tests only** | `make test` |
| **Start with Docker** | `make up` |
| **Run locally** | `make dev` |

---

## Project Layout

```
aviationwx.org-archiver/
├── app/
│   ├── archiver.py    # Core fetch/save logic
│   ├── config.py      # YAML config loader
│   ├── scheduler.py   # APScheduler background job
│   └── web.py         # Flask GUI
├── config/config.yaml.example
├── main.py            # Entry point
├── tests/
└── Dockerfile
```

---

## Code Conventions

- **Follow [CODE_STYLE.md](CODE_STYLE.md)** — Human-first, no AI bias.
- **Pre-commit**: Run `make test-ci` before every commit.
- **Cleanup**: Delete AI-generated temp files (research, analysis, plans) before committing.

---

## Code Review Checklist

- [ ] Tests pass
- [ ] Lint and format check pass
- [ ] No sensitive data (API keys, credentials)
- [ ] Follows CODE_STYLE.md
- [ ] No AI temp files in commit
