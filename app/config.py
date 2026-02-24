"""
AviationWX.org Archiver - Configuration loader.

Loads and validates configuration from a YAML file.
"""

from __future__ import annotations

import logging
import os

import yaml

from app.constants import (
    DEFAULT_INTERVAL_MINUTES,
    DEFAULT_LOG_DISPLAY_COUNT,
    DEFAULT_REQUEST_DELAY_SECONDS,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "archive": {
        "output_dir": "/archive",
        "retention_days": 0,
        "retention_max_gb": 0,
    },
    "schedule": {
        "interval_minutes": DEFAULT_INTERVAL_MINUTES,
        "fetch_on_start": True,
        "job_timeout_minutes": 30,
    },
    "source": {
        "base_url": "https://aviationwx.org",
        "airports_api_url": "https://api.aviationwx.org/v1/airports",
        "api_key": "",
        "request_timeout": 30,
        "max_retries": 3,
        "retry_delay": 5,
        "use_history_api": True,
        "request_delay_seconds": DEFAULT_REQUEST_DELAY_SECONDS,
    },
    "airports": {
        "archive_all": False,
        "selected": [],
    },
    "web": {
        "enabled": True,
        "port": 8080,
        "host": "0.0.0.0",
        "log_display_count": DEFAULT_LOG_DISPLAY_COUNT,
        # When > 0 and web enabled: archive worker yields this many seconds at
        # strategic points so the web UI gets CPU time. 0 = disabled.
        "priority_yield_seconds": 0.02,
    },
    "logging": {
        "level": "INFO",
        "file": "",
    },
}

_CONFIG_PATH_ENV = "ARCHIVER_CONFIG"
_DEFAULT_CONFIG_PATH = "/config/config.yaml"

# Map ARCHIVER_* env vars to config paths. Type: str, int, float, bool, or "list"
_ENV_TO_CONFIG: list[tuple[str, tuple[str, ...], str | type]] = [
    ("ARCHIVER_ARCHIVE_OUTPUT_DIR", ("archive", "output_dir"), str),
    ("ARCHIVER_ARCHIVE_RETENTION_DAYS", ("archive", "retention_days"), int),
    ("ARCHIVER_ARCHIVE_RETENTION_MAX_GB", ("archive", "retention_max_gb"), "float"),
    ("ARCHIVER_SCHEDULE_INTERVAL_MINUTES", ("schedule", "interval_minutes"), int),
    ("ARCHIVER_SCHEDULE_FETCH_ON_START", ("schedule", "fetch_on_start"), bool),
    ("ARCHIVER_SCHEDULE_JOB_TIMEOUT_MINUTES", ("schedule", "job_timeout_minutes"), int),
    ("ARCHIVER_SOURCE_BASE_URL", ("source", "base_url"), str),
    ("ARCHIVER_SOURCE_AIRPORTS_API_URL", ("source", "airports_api_url"), str),
    ("ARCHIVER_SOURCE_API_KEY", ("source", "api_key"), str),
    ("ARCHIVER_SOURCE_REQUEST_TIMEOUT", ("source", "request_timeout"), int),
    ("ARCHIVER_SOURCE_MAX_RETRIES", ("source", "max_retries"), int),
    ("ARCHIVER_SOURCE_RETRY_DELAY", ("source", "retry_delay"), int),
    ("ARCHIVER_SOURCE_USE_HISTORY_API", ("source", "use_history_api"), bool),
    (
        "ARCHIVER_SOURCE_REQUEST_DELAY_SECONDS",
        ("source", "request_delay_seconds"),
        "float",
    ),
    ("ARCHIVER_AIRPORTS_ARCHIVE_ALL", ("airports", "archive_all"), bool),
    ("ARCHIVER_AIRPORTS_SELECTED", ("airports", "selected"), "list"),
    ("ARCHIVER_WEB_ENABLED", ("web", "enabled"), bool),
    ("ARCHIVER_WEB_PORT", ("web", "port"), int),
    ("ARCHIVER_WEB_HOST", ("web", "host"), str),
    ("ARCHIVER_WEB_LOG_DISPLAY_COUNT", ("web", "log_display_count"), int),
    ("ARCHIVER_WEB_PRIORITY_YIELD_SECONDS", ("web", "priority_yield_seconds"), "float"),
    ("ARCHIVER_LOGGING_LEVEL", ("logging", "level"), str),
    ("ARCHIVER_LOGGING_FILE", ("logging", "file"), str),
]


def _parse_env_bool(val: str) -> bool:
    """Parse string to bool. Accepts true/false, 1/0, yes/no (case-insensitive)."""
    v = val.strip().lower()
    return v in ("true", "1", "yes", "on")


def _parse_env_list(val: str) -> list[str]:
    """Parse comma/newline-separated string to list of stripped, non-empty strings."""
    items = []
    for part in val.replace(",", "\n").splitlines():
        s = part.strip().upper()
        if s:
            items.append(s)
    return items


def _env_overrides() -> dict:
    """Build config override dict from ARCHIVER_* environment variables."""
    overrides: dict = {}
    for env_key, path, typ in _ENV_TO_CONFIG:
        val = os.environ.get(env_key, "").strip()
        if not val:
            continue
        try:
            if typ is str:
                parsed = val
            elif typ is int:
                parsed = int(val)
            elif typ == "float":
                parsed = float(val) if "." in str(val) else int(val)
            elif typ is bool:
                parsed = _parse_env_bool(val)
            elif typ == "list":
                parsed = _parse_env_list(val)
            else:
                continue
        except (ValueError, TypeError):
            logger.warning("Invalid env %s=%r; ignoring.", env_key, val)
            continue
        target = overrides
        for key in path[:-1]:
            target = target.setdefault(key, {})
        target[path[-1]] = parsed
    return overrides


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str | None = None) -> dict:
    """
    Load configuration from a YAML file, falling back to defaults.

    The config file path is resolved in this order:
    1. Explicit ``config_path`` argument
    2. ``ARCHIVER_CONFIG`` environment variable
    3. Default path ``/config/config.yaml``

    Missing keys fall back to DEFAULT_CONFIG values.
    """
    path = config_path or os.environ.get(_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH)

    config = dict(DEFAULT_CONFIG)

    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                user_config = yaml.safe_load(fh) or {}
            config = _deep_merge(DEFAULT_CONFIG, user_config)
            logger.debug("Merged config from %s (%d top-level keys)", path, len(config))
            logger.info("Configuration loaded from %s", path)
        except yaml.YAMLError as exc:
            logger.error("Failed to parse config file %s: %s", path, exc)
        except OSError as exc:
            logger.error("Failed to read config file %s: %s", path, exc)
    else:
        logger.warning(
            "Config file not found at %s; using defaults. "
            "Use web GUI or ARCHIVER_* env vars to configure.",
            path,
        )

    env_overrides = _env_overrides()
    if env_overrides:
        config = _deep_merge(config, env_overrides)
        logger.debug("Applied config overrides from ARCHIVER_* environment variables")

    return config


def validate_config(config: dict) -> list[str]:
    """
    Validate configuration for minimal operation.

    Args:
        config: Configuration dict (from load_config or similar).

    Returns:
        List of error messages. Empty list means config is valid.
    """
    errors: list[str] = []

    archive_all = config.get("airports", {}).get("archive_all", False)
    selected = config.get("airports", {}).get("selected", [])
    if not archive_all and not selected:
        errors.append(
            "Select at least one airport: enable 'Archive all airports' or add "
            "airport codes (e.g. KSPB, KAWO) to the selected list."
        )

    output_dir = (config.get("archive", {}).get("output_dir") or "").strip()
    if not output_dir:
        errors.append("Archive output directory must not be empty.")
    elif ".." in output_dir or output_dir in ("/", "\\"):
        errors.append(
            "Archive output directory must not be root or contain path traversal (..)."
        )

    source = config.get("source", {})
    if not (source.get("airports_api_url") or "").strip():
        errors.append(
            "Source API URL (source.airports_api_url) must not be empty. "
            "Check configuration."
        )

    interval_minutes = config.get("schedule", {}).get(
        "interval_minutes", DEFAULT_INTERVAL_MINUTES
    )
    if interval_minutes < 1:
        errors.append(
            "Schedule interval (schedule.interval_minutes) must be at least 1 minute."
        )

    if errors:
        logger.debug("Config validation failed: %s", "; ".join(errors))
    else:
        logger.debug("Config validation passed.")
    return errors


def check_host_resources(config: dict, config_path: str | None = None) -> None:
    """
    Log warnings when Docker host resources (output_dir, config file) are
    missing or inaccessible. Helps diagnose volume mount and permission issues.

    Args:
        config: Configuration dict with archive.output_dir.
        config_path: Optional config file path. Defaults to ARCHIVER_CONFIG env
            or /config/config.yaml.
    """
    output_dir = (config.get("archive", {}).get("output_dir") or "").strip()
    if output_dir:
        if not os.path.isdir(output_dir):
            logger.warning(
                "Archive output_dir %s does not exist or is not a directory. "
                "Ensure the host volume is mounted (e.g. -v ./archive:/archive).",
                output_dir,
            )
        else:
            try:
                test_path = os.path.join(output_dir, ".archiver_write_test")
                with open(test_path, "wb") as fh:
                    fh.write(b"")
            except OSError as exc:
                logger.warning(
                    "Archive output_dir %s is not writable: %s. "
                    "Check volume mount permissions (container runs as non-root).",
                    output_dir,
                    exc,
                )
            else:
                try:
                    os.unlink(test_path)
                except OSError as exc:
                    logger.debug(
                        "Could not remove write-test file %s: %s", test_path, exc
                    )

    path = config_path or os.environ.get(_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                fh.read(1)
        except OSError as exc:
            logger.warning(
                "Config file %s exists but cannot be read: %s. "
                "Check volume mount permissions.",
                path,
                exc,
            )
    dir_path = os.path.dirname(path)
    if dir_path and os.path.isdir(dir_path):
        test_file = os.path.join(dir_path, ".archiver_config_write_test")
        try:
            with open(test_file, "w", encoding="utf-8") as fh:
                fh.write("")
        except OSError as exc:
            logger.warning(
                "Config directory %s is not writable (web GUI save may fail): %s.",
                dir_path,
                exc,
            )
        else:
            try:
                os.unlink(test_file)
            except OSError as exc:
                logger.debug(
                    "Could not remove config write-test file %s: %s",
                    test_file,
                    exc,
                )


def save_config(config: dict, config_path: str | None = None) -> bool:
    """
    Save configuration to the YAML file.

    Returns True on success, False on failure.
    """
    path = config_path or os.environ.get(_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH)

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(config, fh, default_flow_style=False, sort_keys=False)
        logger.debug("Wrote config to %s (%d top-level keys)", path, len(config))
        logger.info("Configuration saved to %s", path)
        return True
    except OSError as exc:
        logger.error("Failed to write config file %s: %s", path, exc)
        return False
