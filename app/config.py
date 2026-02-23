"""
AviationWX.org Archiver - Configuration loader.

Loads and validates configuration from a YAML file.
"""

from __future__ import annotations

import logging
import os

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "archive": {
        "output_dir": "/archive",
        "retention_days": 0,
    },
    "schedule": {
        "interval_minutes": 15,
        "fetch_on_start": True,
    },
    "source": {
        "base_url": "https://aviationwx.org",
        "airports_api_url": "https://api.aviationwx.org/v1/airports",
        "request_timeout": 30,
        "max_retries": 3,
        "retry_delay": 5,
    },
    "airports": {
        "archive_all": False,
        "selected": [],
    },
    "web": {
        "port": 8080,
        "host": "0.0.0.0",
        "log_display_count": 100,
    },
    "logging": {
        "level": "INFO",
        "file": "",
    },
}

_CONFIG_PATH_ENV = "ARCHIVER_CONFIG"
_DEFAULT_CONFIG_PATH = "/config/config.yaml"


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
            logger.info("Configuration loaded from %s", path)
        except yaml.YAMLError as exc:
            logger.error("Failed to parse config file %s: %s", path, exc)
        except OSError as exc:
            logger.error("Failed to read config file %s: %s", path, exc)
    else:
        logger.warning(
            "Config file not found at %s; using defaults. "
            "Copy config/config.yaml.example to %s to customise.",
            path,
            path,
        )

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

    return errors


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
        logger.info("Configuration saved to %s", path)
        return True
    except OSError as exc:
        logger.error("Failed to write config file %s: %s", path, exc)
        return False
