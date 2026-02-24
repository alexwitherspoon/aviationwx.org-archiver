# Code Style Guide — AviationWX.org Archiver

Coding standards and development practices for the AviationWX.org Archiver. All contributors must follow these guidelines.

**Human-first**: Code should read as human-written — clear, idiomatic, no AI-isms. Do not bias toward any single AI agent.

---

## Table of Contents

- [Pre-commit and Cleanup](#pre-commit-and-cleanup)
- [Formatting and Linting](#formatting-and-linting)
- [Naming Conventions](#naming-conventions)
- [Error Handling](#error-handling)
- [Testing](#testing)
- [Docstrings](#docstrings)
- [Comments](#comments)
- [Dependencies](#dependencies)
- [Configuration](#configuration)

---

## Pre-commit and Cleanup

### Before Every Commit

1. **Run `make test-ci`** — Lint, format check, and tests must pass.
2. **Delete AI-generated temp files** — Research, analysis, plans, checklists, diagnostics. Never commit these.

### Permitted During Work

Temporary files for research, analysis, or planning are fine while working. Remove them before committing.

---

## Formatting and Linting

- **Ruff**: Use `ruff check` and `ruff format`. Line length 88 (Black-compatible).
- **Target**: Python 3.12.
- **Commands**:
  - `make lint` — Check only
  - `make format` — Format code
  - `make format-check` — Verify formatting
  - `make test-ci` — Lint + format check + tests

---

## Naming Conventions

| Element | Style | Example |
|---------|-------|---------|
| Functions, variables | `snake_case` | `fetch_airport_list`, `output_dir` |
| Classes | `PascalCase` | `ConfigLoader` |
| Constants | `UPPER_SNAKE_CASE` | `MAX_RETRIES`, `DEFAULT_PORT` |
| Modules | `snake_case` | `archiver.py`, `config.py` |

---

## Error Handling

- **Never silently fail.** Handle errors explicitly.
- **Log appropriately** — Use `logging` with appropriate levels (debug, info, warning, error).
- **Per-airport degradation** — One airport's failure must not affect others.
- **Retries** — Use configurable retries with backoff for transient failures.

---

## Testing

- **Framework**: pytest.
- **Naming**: `test_function_name_scenario_expected_behavior`.
- **Mocks**: Mock I/O (HTTP, filesystem) — tests must not require network.
- **Coverage**: Bug fixes must include a test that would have caught the bug.
- **TDD**: Prefer writing tests first when adding new behavior.

---

## Docstrings

- **Public APIs**: Docstrings required. Use Google or NumPy style.
- **Content**: Purpose, args, returns, raises. Focus on behavior, not implementation.

```python
def fetch_airport_list(config: dict) -> list[dict]:
    """
    Return the list of airports from the AviationWX.org public API.

    Args:
        config: Configuration dict with source.airports_api_url, etc.

    Returns:
        List of airport dicts with at least {"code": "..."}. Empty on failure.
    """
```

---

## Comments

- **Focus on "why"** — Not "what". Code should be self-documenting.
- **DO** comment: Complex logic, non-obvious behavior, safety-critical paths.
- **DON'T** comment: Self-explanatory code, obvious operations.

---

## Dependencies

- **Minimal** — Add dependencies only when justified.
- **Pin versions** — Use `>=` with minimum in requirements.txt.
- **Separate** — `requirements.txt` (runtime), `requirements-dev.txt` (test, lint).

---

## Configuration

- **Single source** — `config/config.yaml` driven by `ARCHIVER_CONFIG` env var.
- **Defaults** — Deep-merge user config over defaults. `ARCHIVER_*` env vars override file values.
- **Never commit** — `config/config.yaml` is in `.gitignore`. Use `config.yaml.example` as template.
