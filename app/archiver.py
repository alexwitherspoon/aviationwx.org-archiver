"""
AviationWX.org Archiver - Core image fetching and archival logic.

Fetches webcam images from AviationWX.org and organises them on disk as:
    <output_dir>/<AIRPORT_CODE>/<YYYY>/<MM>/<DD>/<camera_name>/<filename>
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

from app.constants import (
    API_LIMIT_ANONYMOUS_REQ_PER_MIN,
    BYTES_PER_GIB,
    DEFAULT_REQUEST_DELAY_SECONDS,
    MD5_READ_CHUNK_SIZE,
    MIN_IMAGE_SIZE,
    SECONDS_PER_MINUTE,
)

logger = logging.getLogger(__name__)

# Exclude metadata.json from retention/stats scans (updated each run, not versioned)
_METADATA_JSON = "metadata.json"
_ARCHIVE_INDEX_FILENAME = ".archive_index.json"
_ARCHIVE_INDEX_VERSION = 1


def _archive_index_path(output_dir: str) -> str:
    """Path to the file-based archive index."""
    return os.path.join(output_dir, _ARCHIVE_INDEX_FILENAME)


def _load_archive_index(output_dir: str) -> dict | None:
    """
    Load the archive index. Returns None if missing or invalid.
    Index format: {"version": N, "files": {rel_path: {"mtime": float, "size": int}}}
    """
    path = _archive_index_path(output_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("version") != _ARCHIVE_INDEX_VERSION:
            return None
        files = data.get("files")
        if not isinstance(files, dict):
            return None
        return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Archive index load failed (%s): %s", path, exc)
        return None


def _save_archive_index(output_dir: str, data: dict) -> bool:
    """Save the archive index. Returns True on success."""
    path = _archive_index_path(output_dir)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        return True
    except OSError as exc:
        logger.debug("Archive index save failed (%s): %s", path, exc)
        return False


def _add_to_archive_index(
    output_dir: str, full_path: str, mtime: float, size: int
) -> None:
    """Add or update a file entry in the archive index."""
    try:
        rel = os.path.relpath(full_path, output_dir)
        if rel.startswith("..") or os.path.isabs(rel):
            return
    except ValueError:
        return
    data = _load_archive_index(output_dir) or {
        "version": _ARCHIVE_INDEX_VERSION,
        "files": {},
    }
    data["files"][rel] = {"mtime": mtime, "size": size}
    _save_archive_index(output_dir, data)


def _remove_from_archive_index(output_dir: str, full_path: str) -> None:
    """Remove a file entry from the archive index."""
    try:
        rel = os.path.relpath(full_path, output_dir)
    except ValueError:
        return
    data = _load_archive_index(output_dir)
    if not data or rel not in data.get("files", {}):
        return
    del data["files"][rel]
    _save_archive_index(output_dir, data)


def _rel_path_safe(output_dir: str, rel: str) -> bool:
    """Return True if rel stays under output_dir (no path traversal)."""
    if rel.startswith("..") or os.path.isabs(rel):
        return False
    try:
        full = os.path.normpath(os.path.join(output_dir, rel))
        base = os.path.normpath(output_dir)
        return full == base or full.startswith(base + os.sep)
    except (ValueError, OSError):
        return False


def _index_entries_valid(output_dir: str, data: dict, sample_size: int = 10) -> bool:
    """
    Spot-check a sample of index entries. Returns False if any are missing on disk.

    Used to detect index staleness (e.g. manual file deletion) and trigger rebuild.
    """
    files = data.get("files", {})
    if not files:
        return True
    keys = list(files.keys())
    sample = random.sample(keys, min(sample_size, len(keys)))
    for rel in sample:
        if not _rel_path_safe(output_dir, rel):
            logger.debug("Index: unsafe path %s in spot-check; triggering rebuild", rel)
            return False
        full = os.path.join(output_dir, rel)
        if not os.path.isfile(full):
            logger.debug("Index staleness: %s missing on disk; triggering rebuild", rel)
            return False
    return True


def _rebuild_archive_index(
    output_dir: str,
    config: dict | None = None,
    pre_collected: list[tuple[str, float, int]] | None = None,
) -> dict | None:
    """
    Rebuild the archive index from a full scandir walk or pre-collected results.

    Returns the new index data or None on failure.
    pre_collected: optional list of (full_path, mtime, size) to avoid double scan.
    """
    files: dict[str, dict] = {}
    if pre_collected is not None:
        for fpath, mtime, size in pre_collected:
            try:
                rel = os.path.relpath(fpath, output_dir)
                if rel.startswith("..") or os.path.isabs(rel):
                    continue
                files[rel] = {"mtime": mtime, "size": size}
            except ValueError:
                continue
    else:
        for fpath, st in _scandir_walk_files(output_dir, config=config):
            try:
                rel = os.path.relpath(fpath, output_dir)
                if rel.startswith("..") or os.path.isabs(rel):
                    continue
                files[rel] = {"mtime": st.st_mtime, "size": st.st_size}
            except ValueError:
                continue
    data = {"version": _ARCHIVE_INDEX_VERSION, "files": files}
    if _save_archive_index(output_dir, data):
        logger.debug("Rebuilt archive index (%d files)", len(files))
        return data
    return None


def _scandir_walk_files(
    top: str,
    exclude_names: frozenset[str] | None = None,
    config: dict | None = None,
):
    """
    Walk archive using os.scandir. Yields (filepath, stat) for each file.

    Uses scandir for better performance on large directories (avoids extra
    stat syscalls on systems that cache DirEntry info).
    exclude_names: filenames to skip (default: metadata.json).
    config: if set, call _yield_for_web(config) after each directory.
    """
    exclude = exclude_names or frozenset({_METADATA_JSON})
    try:
        with os.scandir(top) as it:
            entries = list(it)
    except OSError:
        return
    dirs = []
    files = []
    for entry in entries:
        try:
            if entry.name in exclude or entry.name.startswith("."):
                continue
            if entry.is_dir(follow_symlinks=False):
                dirs.append(entry)
            else:
                files.append(entry)
        except OSError:
            continue
    for entry in files:
        try:
            st = entry.stat(follow_symlinks=False)
            yield (entry.path, st)
        except OSError:
            pass
    if config:
        _yield_for_web(config)
    for d in dirs:
        yield from _scandir_walk_files(d.path, exclude, config)


def _http_get(config: dict, url: str, **kwargs) -> requests.Response:
    """GET with session reuse when available (avoids per-request connection setup)."""
    session = config.get("source", {}).get("_session")
    if session is not None:
        return session.get(url, **kwargs)
    return requests.get(url, **kwargs)


def _yield_for_web(config: dict) -> None:
    """
    Yield CPU to other threads when web UI is enabled.

    When web.enabled and web.priority_yield_seconds > 0, sleeps briefly so the
    web thread gets scheduled. Helps keep the admin panel responsive during
    archive runs.
    """
    web = config.get("web") or {}
    if not web.get("enabled", True):
        return
    sec = web.get("priority_yield_seconds", 0)
    if sec > 0:
        time.sleep(sec)


def _sanitize_camera_name(name: str, fallback: str = "unknown") -> str:
    """
    Make camera name safe for Linux filesystem: lowercase, no spaces.

    Replaces spaces with underscores and strips other unsafe chars.
    Returns fallback if result would be empty.
    """
    if not name or not isinstance(name, str):
        return fallback
    # Lowercase, replace spaces with underscores, keep alphanumeric and underscore
    safe = name.lower().replace(" ", "_")
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in safe)
    # Collapse multiple underscores
    while "__" in safe:
        safe = safe.replace("__", "_")
    safe = safe.strip("_")
    return safe if safe else fallback


def _airport_timezone(airport: dict | None) -> str:
    """Return IANA timezone (e.g. America/Los_Angeles) or UTC if unknown."""
    if not airport:
        return "UTC"
    tz = (airport.get("timezone") or "").strip()
    return tz if tz else "UTC"


def _utc_ts_to_local_ymd(utc_ts: int | float, tz_name: str) -> tuple[str, str, str]:
    """
    Convert UTC Unix timestamp to (YYYY, MM, DD) in airport local time for folder path.

    Uses airport local date so folders match when the image was captured at the airport,
    not UTC (which can show tomorrow's date in evening US time).
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:
        logger.debug("Timezone %r invalid or unavailable, using UTC: %s", tz_name, exc)
        tz = timezone.utc
    dt_utc = datetime.fromtimestamp(utc_ts, tz=timezone.utc)
    dt_local = dt_utc.astimezone(tz)
    return (
        dt_local.strftime("%Y"),
        dt_local.strftime("%m"),
        dt_local.strftime("%d"),
    )


def _utc_dt_to_local_ymd(dt: datetime, tz_name: str) -> tuple[str, str, str]:
    """
    Convert UTC datetime to (YYYY, MM, DD) in airport local time for folder path.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:
        logger.debug("Timezone %r invalid or unavailable, using UTC: %s", tz_name, exc)
        tz = timezone.utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_local = dt.astimezone(tz)
    return (
        dt_local.strftime("%Y"),
        dt_local.strftime("%m"),
        dt_local.strftime("%d"),
    )


# Browser-like User-Agent to avoid Cloudflare blocks.
# Python requests default is often blocked by WAF.
_USER_AGENT = (
    "Mozilla/5.0 (compatible; AviationWX-Archiver/1.0; "
    "+https://github.com/alexwitherspoon/aviationwx.org-archiver)"
)


def _api_headers(config: dict) -> dict:
    """Return headers for API requests, including X-API-Key if configured."""
    headers = {"User-Agent": _USER_AGENT}
    api_key = (config.get("source", {}).get("api_key") or "").strip()
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _status_url(config: dict) -> str:
    """Derive /v1/status URL from airports_api_url."""
    url = config.get("source", {}).get("airports_api_url", "")
    if "/airports" in url:
        return url.replace("/airports", "/status")
    return url.rstrip("/").rsplit("/", 1)[0] + "/status" if url else ""


def _detect_and_set_request_delay(config: dict) -> None:
    """
    Probe the API for X-RateLimit-Limit and set request delay to 50% of limit.

    Working API key: uses 50% of partner limit from response.
    No key or invalid key: uses 50% of anonymous limit (100/min).
    """
    source = config.setdefault("source", {})
    url = _status_url(config)
    if not url:
        source["_request_delay_seconds"] = DEFAULT_REQUEST_DELAY_SECONDS
        logger.debug(
            "Could not derive status URL; using default delay %.2fs",
            DEFAULT_REQUEST_DELAY_SECONDS,
        )
        return

    timeout = source.get("request_timeout", 30)
    try:
        resp = _http_get(config, url, timeout=timeout, headers=_api_headers(config))
        limit_header = resp.headers.get("X-RateLimit-Limit") or resp.headers.get(
            "x-ratelimit-limit"
        )
        if resp.ok and limit_header:
            try:
                limit = int(limit_header)
                if limit > 0:
                    # 50% of limit: delay = 60 / (limit/2) = 120/limit seconds
                    delay = 120.0 / limit
                    source["_request_delay_seconds"] = delay
                    logger.info(
                        "Detected API limit %d req/min; using %.2fs delay (50%%)",
                        limit,
                        delay,
                    )
                    return
            except ValueError:
                pass

        # 401 (invalid key) or missing headers: use 50% of anonymous
        limit = API_LIMIT_ANONYMOUS_REQ_PER_MIN
        delay = 120.0 / limit
        source["_request_delay_seconds"] = delay
        if resp.status_code == 401:
            logger.info(
                "API key invalid or rejected; using anonymous limit %.2fs delay",
                delay,
            )
        else:
            logger.debug(
                "No X-RateLimit-Limit in response; using anonymous %.2fs delay",
                delay,
            )
    except requests.RequestException as exc:
        source["_request_delay_seconds"] = DEFAULT_REQUEST_DELAY_SECONDS
        logger.warning(
            "Rate limit probe failed (%s); using default %.2fs delay",
            exc,
            DEFAULT_REQUEST_DELAY_SECONDS,
        )


def _rate_limit(config: dict) -> None:
    """
    Sleep before API requests to respect rate limits.

    Uses dynamically detected delay (_request_delay_seconds) when set by
    _detect_and_set_request_delay, otherwise request_delay_seconds from config.
    Skips sleep when _skip_next_rate_limit is set (e.g. after 404) so the next
    download starts immediately.
    """
    source = config.get("source", {})
    if source.pop("_skip_next_rate_limit", False):
        return
    delay = source.get("_request_delay_seconds")
    if delay is None:
        delay = source.get("request_delay_seconds", DEFAULT_REQUEST_DELAY_SECONDS)
    if delay > 0:
        time.sleep(delay)


# ---------------------------------------------------------------------------
# Airport discovery
# ---------------------------------------------------------------------------


def fetch_airport_list(config: dict) -> list[dict]:
    """
    Return the list of airports from the AviationWX.org public API.

    Each item is a dict with at least ``{"code": "KSPB", ...}``.
    Returns an empty list on failure.
    """
    url = config["source"]["airports_api_url"]
    timeout = config["source"]["request_timeout"]
    retries = config["source"]["max_retries"]
    delay = config["source"]["retry_delay"]
    logger.debug("Fetching airport list from %s", url)

    for attempt in range(1, retries + 1):
        _rate_limit(config)
        try:
            resp = _http_get(config, url, timeout=timeout, headers=_api_headers(config))
            resp.raise_for_status()
            data = resp.json()
            # API returns {"airports": [...]} or a bare list
            if isinstance(data, list):
                logger.debug("Fetched %d airports from API (bare list)", len(data))
                return data
            if isinstance(data, dict):
                airports = data.get("airports", data.get("data", []))
                if isinstance(airports, list):
                    logger.debug(
                        "Fetched %d airports from API (dict response)", len(airports)
                    )
                    return airports
                logger.debug(
                    "Airports API returned non-list (got %s)",
                    type(airports).__name__,
                )
                return []
        except requests.RequestException as exc:
            logger.warning(
                "Attempt %d/%d: failed to fetch airport list from %s: %s",
                attempt,
                retries,
                url,
                exc,
            )
        except json.JSONDecodeError as exc:
            logger.warning(
                "Attempt %d/%d: invalid JSON from %s: %s",
                attempt,
                retries,
                url,
                exc,
            )
        if attempt < retries:
            time.sleep(delay)

    logger.error("All %d attempts to fetch airport list failed.", retries)
    return []


def select_airports(all_airports: list[dict], config: dict) -> list[dict]:
    """
    Filter the full airport list according to configuration.

    When ``airports.archive_all`` is True every airport is returned.
    Otherwise only airports whose code appears in ``airports.selected`` are kept.
    """
    if config["airports"].get("archive_all", False):
        logger.debug(
            "Selected all %d airports (archive_all=true)",
            len(all_airports),
        )
        return all_airports

    selected_codes = {c.upper() for c in config["airports"].get("selected", [])}
    if not selected_codes:
        logger.warning(
            "No airports selected and archive_all is false; nothing to archive."
        )
        return []

    filtered = [a for a in all_airports if _airport_code(a).upper() in selected_codes]
    found_codes = {_airport_code(a).upper() for a in filtered}
    missing = selected_codes - found_codes
    if missing:
        logger.warning(
            "Selected airports not found in API: %s",
            ", ".join(sorted(missing)),
        )

    logger.debug(
        "Selected %d of %d airports: %s",
        len(filtered),
        len(all_airports),
        ", ".join(sorted(found_codes)) if found_codes else "(none)",
    )
    return filtered


def _airport_code(airport: dict) -> str:
    """Extract the airport code from an airport dict."""
    return airport.get("code") or airport.get("id") or airport.get("icao") or ""


# ---------------------------------------------------------------------------
# Image URL discovery
# ---------------------------------------------------------------------------


def fetch_image_urls(airport: dict, config: dict) -> list[str]:
    """
    Return a list of image URLs for a given airport.

    Tries the public API endpoint first; falls back to scraping the airport
    page for ``<img>`` tags that look like webcam images.
    """
    code = _airport_code(airport)
    base_url = config["source"]["base_url"]
    timeout = config["source"]["request_timeout"]

    # 1. Try the API endpoint for webcam images
    api_url = config["source"]["airports_api_url"]
    api_base = api_url.rstrip("/").rsplit("/airports", 1)[0]
    webcam_api = f"{api_base}/airports/{code}/webcams"
    try:
        _rate_limit(config)
        resp = _http_get(
            config, webcam_api, timeout=timeout, headers=_api_headers(config)
        )
        if resp.ok:
            try:
                data = resp.json()
            except json.JSONDecodeError as exc:
                logger.warning("Webcam API returned invalid JSON for %s: %s", code, exc)
            else:
                urls = _extract_urls_from_api(data, api_base)
                if urls:
                    logger.debug("Found %d image(s) for %s via API", len(urls), code)
                    return urls
                logger.debug(
                    "Webcam API returned data but no image URLs for %s",
                    code,
                )
        else:
            logger.debug(
                "Webcam API %s returned %s for %s",
                webcam_api,
                resp.status_code,
                code,
            )
    except requests.RequestException as exc:
        logger.debug("Webcam API request failed for %s: %s", code, exc)

    # 2. Fall back: fetch the airport page and look for image tags
    page_url = f"{base_url.rstrip('/')}/?airport={code.lower()}"
    logger.debug("Fetching airport page %s (fallback)", page_url)
    try:
        _rate_limit(config)
        resp = _http_get(
            config, page_url, timeout=timeout, headers=_api_headers(config)
        )
        if resp.ok:
            urls = _scrape_image_urls(resp.text, base_url)
            if urls:
                logger.debug(
                    "Found %d image(s) for %s via page scrape", len(urls), code
                )
                return urls
            logger.debug("Page scrape found 0 image URLs for %s", code)
        else:
            logger.debug(
                "Airport page %s returned %s for %s",
                page_url,
                resp.status_code,
                code,
            )
    except requests.RequestException as exc:
        logger.warning("Failed to fetch airport page for %s: %s", code, exc)

    logger.warning(
        "No images found for %s — API (%s) and page scrape returned none. "
        "Check source.base_url and source.airports_api_url in config.",
        code,
        webcam_api,
    )
    return []


def _extract_urls_from_api(data: dict | list, base_url: str) -> list[str]:
    """Extract image URLs from a webcam API response."""
    urls = []
    items = (
        data if isinstance(data, list) else data.get("webcams", data.get("data", []))
    )
    for item in items:
        if isinstance(item, dict):
            for key in ("image_url", "url", "src", "snapshot_url"):
                if key in item and isinstance(item[key], str) and item[key]:
                    urls.append(_absolute_url(item[key], base_url))
                    break
    return urls


def _webcam_to_image_url(webcam: dict, config: dict) -> str | None:
    """Convert webcam dict to full current image URL."""
    api_url = config["source"]["airports_api_url"]
    api_base = api_url.rstrip("/").rsplit("/airports", 1)[0]
    for key in ("image_url", "url", "src", "snapshot_url"):
        val = webcam.get(key)
        if val and isinstance(val, str):
            base = api_base + "/"
            return val if val.startswith("http") else urljoin(base, val)
    return None


def _fetch_webcams_api_response(airport: dict, config: dict) -> dict | None:
    """
    Fetch full webcams API response for an airport.

    Returns the raw API response dict (meta, webcams, etc.) or None on failure.
    Used by setup to write metadata.json and identify cameras.
    """
    code = _airport_code(airport)
    api_url = config["source"]["airports_api_url"]
    api_base = api_url.rstrip("/").rsplit("/airports", 1)[0]
    webcam_api = f"{api_base}/airports/{code}/webcams"
    timeout = config["source"]["request_timeout"]
    logger.debug("Fetching webcams from %s", webcam_api)

    try:
        _rate_limit(config)
        resp = _http_get(
            config, webcam_api, timeout=timeout, headers=_api_headers(config)
        )
        if not resp.ok:
            logger.debug(
                "Webcams API %s returned %s for %s",
                webcam_api,
                resp.status_code,
                code,
            )
            return None
        data = resp.json()
        webcams = data.get("webcams", data.get("data", []))
        if not isinstance(webcams, list):
            logger.debug(
                "Webcams API returned non-list for %s (got %s)",
                code,
                type(webcams).__name__,
            )
            return None
        logger.debug("Webcams API returned %d webcam(s) for %s", len(webcams), code)
        return data
    except requests.RequestException as exc:
        logger.debug("Webcams API request failed for %s: %s", code, exc)
        return None
    except json.JSONDecodeError as exc:
        logger.debug("Webcams API invalid JSON for %s: %s", code, exc)
        return None


def _fetch_webcams_list(airport: dict, config: dict) -> list[dict]:
    """
    Fetch webcams list from API for an airport.

    Returns list of webcam dicts with index, history_url, history_enabled, etc.
    """
    data = _fetch_webcams_api_response(airport, config)
    if data is None:
        return []
    webcams = data.get("webcams", data.get("data", []))
    return webcams if isinstance(webcams, list) else []


def setup_airport_archive(airport: dict, config: dict) -> list[dict] | None:
    """
    Setup API call: fetch webcams, write metadata.json, create directory structure.

    Creates output_dir/AIRPORT/metadata.json with full API response.
    Creates output_dir/AIRPORT/YYYY/MM/DD/camera_name/ for today and each camera.
    Returns list of webcams for use by archive run, or None on API failure.
    """
    code = _airport_code(airport)
    output_dir = config["archive"]["output_dir"]
    airport_root = os.path.join(output_dir, code.upper())

    api_response = _fetch_webcams_api_response(airport, config)
    webcams = []
    if api_response is not None:
        webcams = api_response.get("webcams", api_response.get("data", []))
        if not isinstance(webcams, list):
            webcams = []

    metadata = {
        "airport": airport,
        "api_response": api_response if api_response else {},
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    metadata_path = os.path.join(airport_root, "metadata.json")
    try:
        os.makedirs(airport_root, exist_ok=True)
        with open(metadata_path, "w") as fh:
            json.dump(metadata, fh, indent=2)
        logger.debug("Wrote %s", metadata_path)
    except OSError as exc:
        logger.warning("Failed to write metadata for %s: %s", code, exc)

    if webcams:
        run_ts = datetime.now(timezone.utc)
        airport_tz = _airport_timezone(airport)
        y, m, d = _utc_dt_to_local_ymd(run_ts, airport_tz)
        for webcam in webcams:
            cam_name = webcam.get("name")
            cam_safe = _sanitize_camera_name(
                cam_name or "", fallback=f"cam_{webcam.get('index', 0)}"
            )
            date_path = os.path.join(
                airport_root,
                y,
                m,
                d,
                cam_safe,
            )
            try:
                os.makedirs(date_path, exist_ok=True)
            except OSError as exc:
                logger.warning("Failed to create camera dir %s: %s", date_path, exc)

    return webcams if webcams else None


def fetch_history_frames(airport_code: str, webcam: dict, config: dict) -> list[dict]:
    """
    Fetch list of historical frames from the webcam history API.

    Returns list of dicts: {url, timestamp, timestamp_iso, cam_index}, sorted
    oldest-first. Many airports have ~24h retention; processing oldest first
    captures frames about to expire before they are purged.
    """
    cam_index = webcam.get("index", 0)
    history_url = webcam.get("history_url") if webcam.get("history_enabled") else None
    if not history_url:
        logger.debug(
            "Skipping %s cam %s: no history_url (history_enabled=%s)",
            airport_code,
            cam_index,
            webcam.get("history_enabled"),
        )
        return []

    api_url = config["source"]["airports_api_url"]
    api_base = api_url.rstrip("/").rsplit("/airports", 1)[0]
    if not history_url.startswith("http"):
        full_url = urljoin(api_base + "/", history_url)
    else:
        full_url = history_url
    logger.debug("Fetching history from %s", full_url)

    timeout = config["source"]["request_timeout"]
    try:
        _rate_limit(config)
        resp = _http_get(
            config, full_url, timeout=timeout, headers=_api_headers(config)
        )
        if not resp.ok:
            logger.debug(
                "History API returned %s for %s cam %s",
                resp.status_code,
                airport_code,
                cam_index,
            )
            return []
        data = resp.json()
        frames = data.get("frames", [])
        if not isinstance(frames, list):
            logger.debug(
                "History API returned non-list frames for %s cam %s",
                airport_code,
                cam_index,
            )
            return []
        if not frames:
            logger.debug(
                "History API returned empty frames for %s cam %s",
                airport_code,
                cam_index,
            )
            return []

        logger.debug(
            "History API returned %d frame(s) for %s cam %s",
            len(frames),
            airport_code,
            cam_index,
        )
        result = []
        for f in frames:
            ts = f.get("timestamp")
            url = f.get("url")
            if ts is None or not url:
                continue
            if not url.startswith("http"):
                url = urljoin(api_base + "/", url)
            result.append(
                {
                    "url": url,
                    "timestamp": ts,
                    "timestamp_iso": f.get("timestamp_iso", ""),
                    "cam_index": cam_index,
                }
            )
        result.sort(key=lambda x: x["timestamp"])
        return result
    except (requests.RequestException, json.JSONDecodeError) as exc:
        logger.debug(
            "History API failed for %s cam %s: %s", airport_code, cam_index, exc
        )
        return []


def _get_existing_frames(output_dir: str, airport_code: str) -> set[tuple[int, int]]:
    """
    Scan archive for existing frames and return set of (timestamp, cam_index).

    History filenames follow: {ts}_{cam}.jpg (or .webp).
    Layout: output_dir/AIRPORT/YYYY/MM/DD/camera_name/
    Walks only output_dir/AIRPORT to avoid scanning other airports.
    """
    existing: set[tuple[int, int]] = set()
    airport_upper = airport_code.upper()
    airport_root = os.path.join(output_dir, airport_upper)
    if not os.path.isdir(airport_root):
        logger.debug(
            "Airport dir %s missing; no existing frames for %s",
            airport_root,
            airport_code,
        )
        return existing

    # Walk only this airport's tree; need YYYY/MM/DD/camera (5 parts) for file paths
    for fpath, st in _scandir_walk_files(airport_root):
        rel = os.path.relpath(fpath, airport_root)
        parts = rel.split(os.sep)
        if len(parts) < 5:  # YYYY/MM/DD/camera/file -> 5 parts for file path
            continue
        base, ext = os.path.splitext(os.path.basename(fpath))
        if ext.lower() not in (".jpg", ".jpeg", ".webp"):
            continue
        underscore = base.rfind("_")
        if underscore == -1:
            continue
        try:
            ts = int(base[:underscore])
            cam = int(base[underscore + 1 :])
            if st.st_size < MIN_IMAGE_SIZE:
                _delete_partial_file(fpath)
                continue
            existing.add((ts, cam))
        except (ValueError, OSError):
            continue
    logger.debug("Found %d existing frames for %s", len(existing), airport_code)
    return existing


def _scrape_image_urls(html: str, base_url: str) -> list[str]:
    """
    Very lightweight scraper — no external dependency, uses plain string search.

    Finds src attributes on <img> tags that look like webcam snapshots.
    """
    urls = []
    lower = html.lower()
    pos = 0
    while True:
        start = lower.find("<img", pos)
        if start == -1:
            break
        end = lower.find(">", start)
        tag = html[start : end + 1]
        src = _extract_attr(tag, "src")
        if src and _looks_like_webcam(src):
            urls.append(_absolute_url(src, base_url))
        pos = end + 1
    return urls


def _extract_attr(tag: str, attr: str) -> str:
    """Extract an attribute value from an HTML tag string."""
    lower = tag.lower()
    search = f"{attr}="
    idx = lower.find(search)
    if idx == -1:
        return ""
    idx += len(search)
    if idx >= len(tag):
        return ""
    quote = tag[idx]
    if quote in ('"', "'"):
        end = tag.find(quote, idx + 1)
        return tag[idx + 1 : end] if end != -1 else ""
    # unquoted attribute
    end = len(tag)
    for ch in (" ", ">", "\t", "\n"):
        pos = tag.find(ch, idx)
        if pos != -1:
            end = min(end, pos)
    return tag[idx:end]


def _looks_like_webcam(src: str) -> bool:
    """Return True if the URL looks like a webcam image."""
    lower = src.lower()
    image_exts = (".jpg", ".jpeg", ".webp", ".png", ".gif")
    webcam_keywords = ("webcam", "camera", "cam", "snapshot", "image", "photo")
    has_image_ext = any(
        lower.endswith(ext) or (ext + "?") in lower for ext in image_exts
    )
    has_keyword = any(kw in lower for kw in webcam_keywords)
    return has_image_ext and has_keyword


def _absolute_url(url: str, base_url: str) -> str:
    """Convert a relative URL to absolute using the base URL."""
    if url.startswith(("http://", "https://")):
        return url
    return urljoin(base_url, url)


# ---------------------------------------------------------------------------
# Download and save
# ---------------------------------------------------------------------------


def _delete_partial_file(filepath: str) -> None:
    """Remove partial file so another run can retry. Best-effort."""
    try:
        if os.path.isfile(filepath):
            os.unlink(filepath)
            logger.debug("Removed partial file %s for retry", filepath)
    except OSError as exc:
        logger.warning("Could not remove partial file %s: %s", filepath, exc)


def _parse_content_digest(
    headers: requests.structures.CaseInsensitiveDict,
) -> tuple[str, bytes] | None:
    """
    Parse Content-Digest (RFC 9530) header.

    Returns (algorithm, digest_bytes) for first supported algo, or None.
    Prefers sha-256 > sha-512 > md5.
    """
    raw = headers.get("Content-Digest") or headers.get("content-digest")
    if not raw:
        return None
    # Format: sha-256=:base64:, sha-512=:base64:, md5=:base64:
    for algo in ("sha-256", "sha-512", "md5"):
        match = re.search(rf"{re.escape(algo)}\s*=\s*:([A-Za-z0-9+/=]+):", raw)
        if match:
            try:
                digest = base64.b64decode(match.group(1))
                if digest:
                    return (algo, digest)
            except (ValueError, TypeError):
                continue
    return None


def _parse_content_md5(
    headers: requests.structures.CaseInsensitiveDict,
) -> bytes | None:
    """Parse Content-MD5 header (RFC 1864). Returns digest bytes or None."""
    raw = headers.get("Content-MD5") or headers.get("content-md5")
    if not raw:
        return None
    try:
        digest = base64.b64decode(raw.strip())
        return digest if len(digest) == 16 else None
    except (ValueError, TypeError):
        return None


def _parse_etag_as_md5(
    headers: requests.structures.CaseInsensitiveDict,
) -> bytes | None:
    """
    Parse ETag header as MD5 hex if it looks like 32 hex chars.

    Some servers (e.g. S3) use MD5 hex as ETag. Returns digest bytes or None.
    """
    raw = headers.get("ETag") or headers.get("etag")
    if not raw:
        return None
    etag = raw.strip().strip('"')
    if len(etag) == 32 and re.match(r"^[0-9a-fA-F]{32}$", etag):
        try:
            return bytes.fromhex(etag)
        except ValueError:
            pass
    return None


def _parse_content_range_total(
    headers: requests.structures.CaseInsensitiveDict,
) -> int | None:
    """
    Parse Content-Range header for total size (206 responses).

    Format: bytes start-end/total. Returns total or None if absent/unknown.
    """
    raw = headers.get("Content-Range") or headers.get("content-range")
    if not raw:
        return None
    match = re.search(r"bytes\s+\d+-\d+/(\d+)", raw, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    return None


def _verify_file_integrity(
    filepath: str,
    expected_algo: str,
    expected_digest: bytes,
) -> bool:
    """Compute file hash and compare with expected digest. Returns True if match."""
    try:
        h = hashlib.new(expected_algo)
        with open(filepath, "rb") as fh:
            for chunk in iter(lambda: fh.read(MD5_READ_CHUNK_SIZE), b""):
                h.update(chunk)
        return h.digest() == expected_digest
    except (OSError, ValueError):
        return False


def _get_integrity_check(resp: requests.Response) -> tuple[str, bytes] | None:
    """
    Extract integrity check from response headers.

    Prefers Content-Digest (RFC 9530), then Content-MD5, then ETag (if MD5-shaped).
    Returns (algo, digest) or None when no integrity headers present (fallback: skip).
    """
    headers = resp.headers
    parsed = _parse_content_digest(headers)
    if parsed:
        return parsed
    md5_digest = _parse_content_md5(headers) or _parse_etag_as_md5(headers)
    if md5_digest:
        return ("md5", md5_digest)
    return None


def download_image_to_file(url: str, filepath: str, config: dict) -> bool:
    """
    Download an image to filepath with resume support.

    If a partial file exists, tries Range request to resume. If resume fails
    or download is interrupted, deletes the partial file so another run can retry.
    Returns True on success, False on failure.
    """
    timeout = config["source"]["request_timeout"]
    retries = config["source"]["max_retries"]
    delay = config["source"]["retry_delay"]
    headers = _api_headers(config)

    existing_size = 0
    if os.path.isfile(filepath):
        existing_size = os.path.getsize(filepath)
        if existing_size < MIN_IMAGE_SIZE:
            _delete_partial_file(filepath)
            existing_size = 0

    for attempt in range(1, retries + 1):
        _rate_limit(config)
        try:
            range_headers = {}
            if existing_size > 0:
                range_headers["Range"] = f"bytes={existing_size}-"

            resp = _http_get(
                config,
                url,
                timeout=timeout,
                stream=True,
                headers={**headers, **range_headers},
            )

            # 404/410: image aged out or removed; fail fast, no retry
            if resp.status_code in (404, 410):
                logger.debug(
                    "Image no longer available (%s): %s",
                    resp.status_code,
                    url,
                )
                _delete_partial_file(filepath)
                config.setdefault("source", {})["_skip_next_rate_limit"] = True
                return False

            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                logger.debug(
                    "Skipping non-image URL %s (content-type: %s)",
                    url,
                    content_type,
                )
                return False

            mode = "ab" if existing_size > 0 and resp.status_code == 206 else "wb"
            if mode == "wb" and existing_size > 0:
                _delete_partial_file(filepath)
                existing_size = 0
                mode = "wb"

            with open(filepath, mode) as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)

            size = os.path.getsize(filepath)

            # Verify completeness using server-provided integrity info
            if resp.status_code == 206:
                total = _parse_content_range_total(resp.headers)
                if total is None:
                    logger.debug(
                        "No Content-Range total for resume %s; skipping size check",
                        url,
                    )
                elif size != total:
                    logger.warning(
                        "Incomplete resume: %s has %d bytes, expected %d",
                        filepath,
                        size,
                        total,
                    )
                    _delete_partial_file(filepath)
                    existing_size = 0
                    raise requests.RequestException(
                        f"Incomplete resume: got {size} bytes, expected {total}"
                    )
            else:
                integrity = _get_integrity_check(resp)
                if integrity:
                    algo, expected = integrity
                    if not _verify_file_integrity(filepath, algo, expected):
                        logger.warning(
                            "Integrity check failed for %s (%s mismatch)",
                            filepath,
                            algo,
                        )
                        _delete_partial_file(filepath)
                        existing_size = 0
                        raise requests.RequestException(
                            f"Integrity check failed ({algo} mismatch)"
                        )
                else:
                    logger.debug(
                        "No integrity headers for %s; skipping verification",
                        url,
                    )

            logger.debug("Downloaded to %s (%d bytes)", filepath, size)
            return True

        except requests.RequestException as exc:
            logger.warning(
                "Attempt %d/%d: failed to download %s: %s",
                attempt,
                retries,
                url,
                exc,
            )
            _delete_partial_file(filepath)
            existing_size = 0
            if attempt < retries:
                time.sleep(delay)

    logger.error("All %d attempts to download %s failed.", retries, url)
    _delete_partial_file(filepath)
    return False


def download_image(url: str, config: dict) -> bytes | None:
    """
    Download an image from ``url``.

    Returns the raw bytes on success, or None on failure.
    """
    timeout = config["source"]["request_timeout"]
    retries = config["source"]["max_retries"]
    delay = config["source"]["retry_delay"]

    logger.debug("Downloading image from %s", url)
    for attempt in range(1, retries + 1):
        _rate_limit(config)
        try:
            resp = _http_get(
                config,
                url,
                timeout=timeout,
                stream=True,
                headers=_api_headers(config),
            )

            # 404/410: image aged out or removed; fail fast, no retry
            if resp.status_code in (404, 410):
                logger.debug(
                    "Image no longer available (%s): %s",
                    resp.status_code,
                    url,
                )
                config.setdefault("source", {})["_skip_next_rate_limit"] = True
                return None

            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                logger.debug(
                    "Skipping non-image URL %s (content-type: %s)",
                    url,
                    content_type,
                )
                return None
            logger.debug("Downloaded %d bytes from %s", len(resp.content), url)
            return resp.content
        except requests.RequestException as exc:
            logger.warning(
                "Attempt %d/%d: failed to download %s: %s",
                attempt,
                retries,
                url,
                exc,
            )
            if attempt < retries:
                time.sleep(delay)

    logger.error("All %d attempts to download %s failed.", retries, url)
    return None


def save_history_image_from_url(
    url: str,
    airport_code: str,
    cam_index: int,
    frame_ts: int,
    config: dict,
    camera_name: str | None = None,
    airport_tz: str = "UTC",
) -> str | None:
    """
    Download and save a history API frame via URL with resume support.

    Uses download_image_to_file for resume on interrupt; deletes partial on failure.
    Folder date uses airport local timezone so folders match when image was captured.
    """
    output_dir = config["archive"]["output_dir"]
    cam_safe = _sanitize_camera_name(camera_name or "", fallback=f"cam_{cam_index}")
    y, m, d = _utc_ts_to_local_ymd(frame_ts, airport_tz)
    date_path = os.path.join(
        output_dir,
        airport_code.upper(),
        y,
        m,
        d,
        cam_safe,
    )

    try:
        os.makedirs(date_path, exist_ok=True)
    except OSError as exc:
        logger.error("Failed to create directory %s: %s", date_path, exc)
        return None

    filename = f"{frame_ts}_{cam_index}.jpg"
    filepath = os.path.join(date_path, filename)

    if not download_image_to_file(url, filepath, config):
        return None

    try:
        os.chmod(filepath, 0o644)
        os.utime(filepath, (frame_ts, frame_ts))
        st = os.stat(filepath)
        _add_to_archive_index(output_dir, filepath, float(frame_ts), st.st_size)
        logger.info(
            "Archived history frame %s cam %s @ %s -> %s",
            airport_code,
            cam_index,
            frame_ts,
            filepath,
        )
        return filepath
    except OSError as exc:
        logger.error("Failed to set permissions on %s: %s", filepath, exc)
        _delete_partial_file(filepath)
        return None


def save_history_image(
    image_data: bytes,
    airport_code: str,
    cam_index: int,
    frame_ts: int,
    config: dict,
    camera_name: str | None = None,
    airport_tz: str = "UTC",
) -> str | None:
    """
    Save a history API frame to the archive.

    Filename: {ts}_{cam}.jpg for uniqueness.
    Directory: output_dir/AIRPORT/YYYY/MM/DD/camera_name/ (date in airport local time).
    Files are created with mode 0o644 (owner rw, group/others r).
    """
    output_dir = config["archive"]["output_dir"]
    cam_safe = _sanitize_camera_name(camera_name or "", fallback=f"cam_{cam_index}")
    y, m, d = _utc_ts_to_local_ymd(frame_ts, airport_tz)
    date_path = os.path.join(
        output_dir,
        airport_code.upper(),
        y,
        m,
        d,
        cam_safe,
    )

    try:
        os.makedirs(date_path, exist_ok=True)
    except OSError as exc:
        logger.error("Failed to create directory %s: %s", date_path, exc)
        return None

    filename = f"{frame_ts}_{cam_index}.jpg"
    filepath = os.path.join(date_path, filename)

    if os.path.isfile(filepath):
        existing_hash = _file_md5(filepath)
        new_hash = hashlib.md5(image_data).hexdigest()
        if existing_hash == new_hash:
            logger.debug("Skipping duplicate history frame %s", filepath)
            try:
                st = os.stat(filepath)
                _add_to_archive_index(output_dir, filepath, st.st_mtime, st.st_size)
            except OSError:
                pass
            return filepath

    try:
        with open(filepath, "wb") as fh:
            fh.write(image_data)
        os.chmod(filepath, 0o644)
        os.utime(filepath, (frame_ts, frame_ts))
        _add_to_archive_index(output_dir, filepath, float(frame_ts), len(image_data))
        logger.info(
            "Archived history frame %s cam %s @ %s -> %s",
            airport_code,
            cam_index,
            frame_ts,
            filepath,
        )
        return filepath
    except OSError as exc:
        logger.error("Failed to write image to %s: %s", filepath, exc)
        return None


def save_image_from_url(
    url: str,
    airport_code: str,
    config: dict,
    timestamp: datetime | None = None,
    camera_name: str | None = None,
    airport_tz: str = "UTC",
) -> str | None:
    """
    Download and save an image via URL with resume support.

    Uses download_image_to_file for resume on interrupt; deletes partial on failure.
    Folder date uses airport local timezone so folders match when image was captured.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    output_dir = config["archive"]["output_dir"]
    cam_safe = _sanitize_camera_name(camera_name or "", fallback="current")
    y, m, d = _utc_dt_to_local_ymd(timestamp, airport_tz)
    date_path = os.path.join(
        output_dir,
        airport_code.upper(),
        y,
        m,
        d,
        cam_safe,
    )

    try:
        os.makedirs(date_path, exist_ok=True)
    except OSError as exc:
        logger.error("Failed to create directory %s: %s", date_path, exc)
        return None

    url_basename = os.path.basename(urlparse(url).path) or "image"
    ts_prefix = timestamp.strftime("%Y%m%d_%H%M%S")
    filename = f"{ts_prefix}_{url_basename}"
    filepath = os.path.join(date_path, filename)

    if not download_image_to_file(url, filepath, config):
        return None

    try:
        os.chmod(filepath, 0o644)
        ts = timestamp.timestamp()
        os.utime(filepath, (ts, ts))
        st = os.stat(filepath)
        _add_to_archive_index(output_dir, filepath, ts, st.st_size)
        logger.info("Archived %s -> %s", url, filepath)
        return filepath
    except OSError as exc:
        logger.error("Failed to set permissions on %s: %s", filepath, exc)
        _delete_partial_file(filepath)
        return None


def save_image(
    image_data: bytes,
    url: str,
    airport_code: str,
    config: dict,
    timestamp: datetime | None = None,
    camera_name: str | None = None,
    airport_tz: str = "UTC",
) -> str | None:
    """
    Save image bytes to the archive directory.

    Directory structure: output_dir/AIRPORT/YYYY/MM/DD/camera_name/filename
    Date uses airport local timezone so folders match when image was captured.

    The filename is derived from the URL basename; a timestamp prefix is added
    to avoid collisions when the same URL is fetched repeatedly.
    Files are created with mode 0o644 (owner rw, group/others r).

    Returns the saved file path on success, or None on failure.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    output_dir = config["archive"]["output_dir"]
    cam_safe = _sanitize_camera_name(camera_name or "", fallback="current")
    y, m, d = _utc_dt_to_local_ymd(timestamp, airport_tz)
    date_path = os.path.join(
        output_dir,
        airport_code.upper(),
        y,
        m,
        d,
        cam_safe,
    )

    try:
        os.makedirs(date_path, exist_ok=True)
    except OSError as exc:
        logger.error("Failed to create directory %s: %s", date_path, exc)
        return None

    # Build a stable filename: timestamp + original basename
    url_basename = os.path.basename(urlparse(url).path) or "image"
    ts_prefix = timestamp.strftime("%Y%m%d_%H%M%S")
    filename = f"{ts_prefix}_{url_basename}"
    filepath = os.path.join(date_path, filename)

    # Skip if identical content already archived (deduplication by hash)
    if os.path.isfile(filepath):
        existing_hash = _file_md5(filepath)
        new_hash = hashlib.md5(image_data).hexdigest()
        if existing_hash == new_hash:
            logger.debug("Skipping duplicate image %s", filepath)
            try:
                st = os.stat(filepath)
                _add_to_archive_index(output_dir, filepath, st.st_mtime, st.st_size)
            except OSError:
                pass
            return filepath

    try:
        with open(filepath, "wb") as fh:
            fh.write(image_data)
        os.chmod(filepath, 0o644)
        ts = timestamp.timestamp()
        os.utime(filepath, (ts, ts))
        _add_to_archive_index(output_dir, filepath, ts, len(image_data))
        logger.info("Archived %s -> %s", url, filepath)
        return filepath
    except OSError as exc:
        logger.error("Failed to write image to %s: %s", filepath, exc)
        return None


def _file_md5(path: str) -> str:
    """Return the MD5 hex digest of a file."""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(MD5_READ_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Retention / cleanup
# ---------------------------------------------------------------------------


def _collect_archive_files(
    output_dir: str, config: dict | None = None
) -> list[tuple[str, float, int]]:
    """
    Return list of (full_path, mtime, size) for all archive files.

    Uses file-based index when available and spot-check validates it; otherwise
    falls back to full scandir walk and rebuilds the index.
    """
    data = _load_archive_index(output_dir)
    if data and data.get("files") and _index_entries_valid(output_dir, data):
        result: list[tuple[str, float, int]] = []
        for rel, meta in data["files"].items():
            if not _rel_path_safe(output_dir, rel):
                logger.debug("Index: rejecting unsafe path %s; triggering rebuild", rel)
                break
            if not isinstance(meta, dict):
                logger.debug("Index: invalid entry for %s; triggering rebuild", rel)
                break
            mtime = meta.get("mtime")
            size = meta.get("size")
            if not isinstance(mtime, (int, float)) or not isinstance(size, int):
                logger.debug("Index: invalid mtime/size for %s; rebuild", rel)
                break
            full = os.path.join(output_dir, rel)
            result.append((full, float(mtime), size))
        else:
            return result
    # Fallback: full scan and rebuild index (single walk)
    result = []
    for fpath, st in _scandir_walk_files(output_dir, config=config):
        result.append((fpath, st.st_mtime, st.st_size))
    _rebuild_archive_index(output_dir, config, pre_collected=result)
    return result


def _oldest_directory_date(output_dir: str) -> datetime | None:
    """
    Find the oldest date directory (YYYY/MM/DD) in the archive.

    Walks only the directory structure, no file stats. Used to skip full
    retention scans when the oldest content is within the retention window.
    Returns None if empty or no valid date dirs.
    """
    oldest: datetime | None = None
    try:
        for airport in os.listdir(output_dir):
            apath = os.path.join(output_dir, airport)
            if not os.path.isdir(apath) or airport.startswith("."):
                continue
            for year in os.listdir(apath):
                if len(year) != 4 or not year.isdigit():
                    continue
                ypath = os.path.join(apath, year)
                if not os.path.isdir(ypath):
                    continue
                for month in os.listdir(ypath):
                    if len(month) != 2 or not month.isdigit():
                        continue
                    mpath = os.path.join(ypath, month)
                    if not os.path.isdir(mpath):
                        continue
                    for day in os.listdir(mpath):
                        if len(day) != 2 or not day.isdigit():
                            continue
                        try:
                            dt = datetime(
                                int(year),
                                int(month),
                                int(day),
                                tzinfo=timezone.utc,
                            )
                            if oldest is None or dt < oldest:
                                oldest = dt
                        except ValueError:
                            continue
    except OSError as exc:
        logger.debug("Retention quick check: %s", exc)
        return None
    return oldest


def apply_retention(config: dict) -> int:
    """
    Delete archived files based on retention rules.

    Supports:
    - ``archive.retention_days``: Delete files older than N days (0 = disabled).
    - ``archive.retention_max_gb``: Keep total size under N GB by deleting
      oldest files first (0 = disabled).

    Both can be used together; each rule is applied independently.
    Returns 0 immediately when both are 0 or unlimited, without scanning.
    Uses a single directory walk when both rules are enabled.

    Returns the number of files deleted.
    """
    retention_days = config["archive"].get("retention_days", 0)
    retention_max_gb = config["archive"].get("retention_max_gb", 0)
    if isinstance(retention_max_gb, str):
        from app.constants import parse_storage_gb

        retention_max_gb = parse_storage_gb(retention_max_gb)
    retention_max_bytes = (
        int(retention_max_gb * BYTES_PER_GIB) if retention_max_gb > 0 else 0
    )

    # Quick exit: 0 or unlimited = disabled; no scan needed
    if retention_days <= 0 and retention_max_bytes <= 0:
        logger.debug(
            "Retention disabled (retention_days=%s, retention_max_gb=%s); skipping.",
            retention_days,
            retention_max_gb,
        )
        return 0

    output_dir = config["archive"]["output_dir"]
    if not os.path.isdir(output_dir):
        logger.warning(
            "Retention: output_dir %s does not exist; nothing to clean. "
            "Check volume mount (e.g. -v ./archive:/archive).",
            output_dir,
        )
        return 0

    deleted = 0
    cutoff_dt = (
        datetime.now(timezone.utc) - timedelta(days=retention_days)
        if retention_days > 0
        else None
    )
    cutoff_ts = cutoff_dt.timestamp() if cutoff_dt else 0

    # When both rules enabled: single walk, apply age then size
    if retention_days > 0 and retention_max_bytes > 0:
        files_list = _collect_archive_files(output_dir, config)
        remaining: list[tuple[str, float, int]] = []
        for fpath, mtime, size in files_list:
            if mtime < cutoff_ts:
                try:
                    os.remove(fpath)
                    _remove_from_archive_index(output_dir, fpath)
                    deleted += 1
                except OSError as exc:
                    if exc.errno == errno.ENOENT:
                        _remove_from_archive_index(output_dir, fpath)
                    logger.warning("Retention: failed to remove %s: %s", fpath, exc)
            else:
                remaining.append((fpath, mtime, size))
        total_bytes = sum(s for _, _, s in remaining)
        if total_bytes > retention_max_bytes:
            remaining.sort(key=lambda x: x[1])
            to_remove = total_bytes - retention_max_bytes
            removed_bytes = 0
            for i, (fpath, _mtime, size) in enumerate(remaining):
                if removed_bytes >= to_remove:
                    break
                try:
                    os.remove(fpath)
                    _remove_from_archive_index(output_dir, fpath)
                    deleted += 1
                    removed_bytes += size
                except OSError as exc:
                    if exc.errno == errno.ENOENT:
                        _remove_from_archive_index(output_dir, fpath)
                    logger.warning("Retention: failed to remove %s: %s", fpath, exc)
                if i > 0 and i % 50 == 0:
                    _yield_for_web(config)
    else:
        # Single rule: use quick checks where possible
        if retention_days > 0:
            # Do not use _oldest_directory_date quick-check: folder dates are now
            # airport-local (not UTC), so we cannot reliably interpret YYYY/MM/DD
            # as a single timezone. Always scan by file mtime for correctness.
            logger.debug(
                "Retention: scanning by age (cutoff %d days, before %s)",
                retention_days,
                cutoff_dt.isoformat(),
            )
            for fpath, mtime, _size in _collect_archive_files(output_dir, config):
                if mtime < cutoff_ts:
                    try:
                        os.remove(fpath)
                        _remove_from_archive_index(output_dir, fpath)
                        deleted += 1
                    except OSError as exc:
                        if exc.errno == errno.ENOENT:
                            _remove_from_archive_index(output_dir, fpath)
                        logger.warning("Retention: failed to remove %s: %s", fpath, exc)

        if retention_max_bytes > 0:
            files_sorted = _collect_archive_files(output_dir, config)
            total_bytes = sum(s for _, _, s in files_sorted)
            if total_bytes <= retention_max_bytes:
                logger.debug(
                    "Retention: total %.1f GB under %.1f GB limit; no deletion needed.",
                    total_bytes / BYTES_PER_GIB,
                    retention_max_bytes / BYTES_PER_GIB,
                )
            else:
                files_sorted.sort(key=lambda x: x[1])
                to_remove = total_bytes - retention_max_bytes
                logger.debug(
                    "Retention: total %.1f GB exceeds max %.1f GB; removing oldest",
                    total_bytes / BYTES_PER_GIB,
                    retention_max_bytes / BYTES_PER_GIB,
                )
                removed_bytes = 0
                for i, (fpath, _mtime, size) in enumerate(files_sorted):
                    if removed_bytes >= to_remove:
                        break
                    try:
                        os.remove(fpath)
                        _remove_from_archive_index(output_dir, fpath)
                        deleted += 1
                        removed_bytes += size
                    except OSError as exc:
                        if exc.errno == errno.ENOENT:
                            _remove_from_archive_index(output_dir, fpath)
                        logger.warning("Retention: failed to remove %s: %s", fpath, exc)
                    if i > 0 and i % 50 == 0:
                        _yield_for_web(config)

    if deleted:
        reasons = []
        if retention_days > 0:
            reasons.append(f"older than {retention_days} days")
        if retention_max_bytes > 0:
            reasons.append(f"over {retention_max_gb} GB limit")
        suffix = " (" + "; ".join(reasons) + ")" if reasons else ""
        logger.info("Retention cleanup: deleted %d file(s)%s.", deleted, suffix)

    return deleted


# ---------------------------------------------------------------------------
# High-level archive run
# ---------------------------------------------------------------------------


def _collect_pending_per_airport(
    airports: list[dict],
    config: dict,
) -> tuple[dict[str, list[tuple[dict, dict | None]]], dict[str, dict]]:
    """
    Collect pending (webcam, frame) per airport, sorted oldest-first.

    Returns (queues, airport_by_code). queues[code] = [(webcam, frame), ...]
    with frame=None meaning "current image" for webcams without history.
    """
    output_dir = config["archive"]["output_dir"]
    queues: dict[str, list[tuple[dict, dict | None]]] = {}
    airport_by_code: dict[str, dict] = {}

    for airport in airports:
        code = _airport_code(airport)
        if not code:
            continue
        webcams = setup_airport_archive(airport, config)
        if not webcams:
            continue
        airport_by_code[code] = airport
        existing = _get_existing_frames(output_dir, code)
        pending: list[tuple[dict, dict | None]] = []

        for webcam in webcams:
            if webcam.get("history_enabled") and webcam.get("history_url"):
                frames = fetch_history_frames(code, webcam, config)
                cam_index = webcam.get("index", 0)
                for frame in frames:
                    ts = frame["timestamp"]
                    if (ts, cam_index) not in existing:
                        pending.append((webcam, frame))
            else:
                pending.append((webcam, None))

        pending.sort(key=lambda x: x[1]["timestamp"] if x[1] is not None else 2**63)
        if pending:
            queues[code] = pending

    return queues, airport_by_code


def _run_archive_round_robin(
    queues: dict[str, list[tuple[dict, dict | None]]],
    airport_by_code: dict[str, dict],
    config: dict,
    stats: dict,
    deadline: float | None,
) -> None:
    """
    Process pending frames in round-robin across airports, oldest first per airport.

    Each round visits each airport once, downloading its oldest pending frame.
    """
    run_ts = datetime.now(timezone.utc)
    codes = list(queues.keys())
    if not codes:
        return

    while codes:
        if deadline is not None and time.time() >= deadline:
            return
        progress = False
        for code in list(codes):
            if not queues[code]:
                codes.remove(code)
                continue
            webcam, frame = queues[code].pop(0)
            cam_index = webcam.get("index", 0)
            cam_name = webcam.get("name")
            airport = airport_by_code.get(code, {})
            airport_tz = _airport_timezone(airport)

            if frame is not None:
                stats["images_fetched"] += 1
                saved = save_history_image_from_url(
                    frame["url"],
                    code,
                    cam_index,
                    frame["timestamp"],
                    config,
                    camera_name=cam_name,
                    airport_tz=airport_tz,
                )
                if saved:
                    stats["images_saved"] += 1
            else:
                url = _webcam_to_image_url(webcam, config)
                if url:
                    stats["images_fetched"] += 1
                    saved = save_image_from_url(
                        url,
                        code,
                        config,
                        timestamp=run_ts,
                        camera_name=cam_name,
                        airport_tz=airport_tz,
                    )
                    if saved:
                        stats["images_saved"] += 1
            progress = True
        if not progress:
            break
        _yield_for_web(config)


def _run_archive_history(
    airport: dict,
    code: str,
    config: dict,
    stats: dict,
    deadline: float | None = None,
    webcams: list[dict] | None = None,
) -> bool:
    """
    Archive using the history API: fetch all available frames, download only
    missing ones. When run every 15 min with 60s refresh, captures ~15 new
    images per webcam. Webcams without history fall back to current image.

    If webcams is provided (from setup), uses it; otherwise fetches from API.
    Returns True if stopped due to deadline (next run will resume from here).
    """
    if webcams is None:
        webcams = _fetch_webcams_list(airport, config)
    if not webcams:
        logger.debug("No webcams from API for %s; falling back to current-only", code)
        _run_archive_current_only(
            airport, code, config, stats, datetime.now(timezone.utc)
        )
        return False

    output_dir = config["archive"]["output_dir"]
    existing = _get_existing_frames(output_dir, code)
    airport_tz = _airport_timezone(airport)
    logger.debug(
        "Processing %s: %d webcams, %d existing frames",
        code,
        len(webcams),
        len(existing),
    )
    run_ts = datetime.now(timezone.utc)

    for webcam in webcams:
        if deadline is not None and time.time() >= deadline:
            return True
        if webcam.get("history_enabled") and webcam.get("history_url"):
            frames = fetch_history_frames(code, webcam, config)
            cam_index = webcam.get("index", 0)

            for frame in frames:
                if deadline is not None and time.time() >= deadline:
                    return True
                ts = frame["timestamp"]
                if (ts, cam_index) in existing:
                    continue

                stats["images_fetched"] += 1
                cam_name = webcam.get("name")
                saved = save_history_image_from_url(
                    frame["url"],
                    code,
                    cam_index,
                    ts,
                    config,
                    camera_name=cam_name,
                    airport_tz=airport_tz,
                )
                if saved:
                    stats["images_saved"] += 1
                    existing.add((ts, cam_index))
        else:
            url = _webcam_to_image_url(webcam, config)
            if not url:
                logger.debug(
                    "Skipping %s cam %s: no image_url/url/src in webcam data",
                    code,
                    webcam.get("index", 0),
                )
            elif url:
                stats["images_fetched"] += 1
                cam_name = webcam.get("name")
                saved = save_image_from_url(
                    url,
                    code,
                    config,
                    timestamp=run_ts,
                    camera_name=cam_name,
                    airport_tz=airport_tz,
                )
                if saved:
                    stats["images_saved"] += 1
        _yield_for_web(config)
    return False


def _run_archive_current_only(
    airport: dict,
    code: str,
    config: dict,
    stats: dict,
    run_ts: datetime,
    webcams: list[dict] | None = None,
) -> None:
    """Archive using current image only (legacy behavior)."""
    airport_tz = _airport_timezone(airport)
    if webcams is None:
        webcams = _fetch_webcams_list(airport, config)
    if webcams:
        for webcam in webcams:
            url = _webcam_to_image_url(webcam, config)
            if not url:
                continue
            stats["images_fetched"] += 1
            cam_name = webcam.get("name")
            saved = save_image_from_url(
                url,
                code,
                config,
                timestamp=run_ts,
                camera_name=cam_name,
                airport_tz=airport_tz,
            )
            if saved:
                stats["images_saved"] += 1
            _yield_for_web(config)
    else:
        image_urls = fetch_image_urls(airport, config)
        if not image_urls:
            logger.debug("No image URLs for %s (current-only mode)", code)
        for url in image_urls:
            stats["images_fetched"] += 1
            saved = save_image_from_url(
                url, code, config, timestamp=run_ts, airport_tz=airport_tz
            )
            if saved:
                stats["images_saved"] += 1


def run_archive(
    config: dict, stats: dict | None = None, deadline: float | None = None
) -> dict:
    """
    Perform a single full archive pass.

    Fetches the airport list, selects configured airports, fetches and saves
    images for each, and applies retention policy. Stops early if deadline
    is reached; next run resumes from where this left off (skips existing).

    Returns a stats dict with keys: airports_processed, images_fetched,
    images_saved, errors.
    """
    if stats is None:
        stats = {
            "airports_processed": 0,
            "images_fetched": 0,
            "images_saved": 0,
            "errors": 0,
        }

    timeout_min = config.get("schedule", {}).get("job_timeout_minutes", 0)
    if deadline is None and timeout_min > 0:
        deadline = time.time() + (timeout_min * SECONDS_PER_MINUTE)

    logger.info("Starting archive run...")
    run_ts = datetime.now(timezone.utc)

    # Defensive: ensure required config structure exists (avoids KeyError on
    # malformed config)
    try:
        _ = config["source"]["airports_api_url"]
        _ = config["archive"]["output_dir"]
    except (KeyError, TypeError) as exc:
        logger.error("Invalid config structure (missing source or archive): %s", exc)
        stats["errors"] += 1
        return stats

    # Reuse HTTP connections across requests (avoids per-request setup overhead)
    config["source"]["_session"] = requests.Session()
    try:
        return _run_archive_impl(config, stats, deadline, run_ts)
    finally:
        try:
            config["source"]["_session"].close()
        except Exception:
            pass
        config["source"].pop("_session", None)


def _run_archive_impl(
    config: dict, stats: dict, deadline: float | None, run_ts: datetime
) -> dict:
    """Inner archive logic; assumes _session is already set in config."""
    # Probe API for X-RateLimit-Limit; set delay to 50% of limit (partner or
    # anonymous depending on key validity)
    _detect_and_set_request_delay(config)

    all_airports = fetch_airport_list(config)
    if not all_airports:
        logger.warning("No airports returned from API; skipping run.")
        stats["errors"] += 1
        return stats

    airports = select_airports(all_airports, config)
    logger.info("Archiving %d airport(s).", len(airports))

    if not airports:
        logger.warning(
            "No airports to archive — check airports.archive_all or "
            "airports.selected in config."
        )
        return stats

    use_history = config.get("source", {}).get("use_history_api", True)
    logger.debug(
        "Archive run: use_history=%s, deadline=%s",
        use_history,
        "yes" if deadline else "no",
    )

    if deadline is not None and time.time() >= deadline:
        logger.info("Archive run skipped: already past deadline.")
        stats["timed_out"] = True
        if config.get("schedule", {}).get("retention_on_archive_run", False):
            apply_retention(config)
        return stats

    if use_history:
        try:
            queues, airport_by_code = _collect_pending_per_airport(airports, config)
            stats["airports_processed"] = len(airport_by_code)
            total_pending = sum(len(q) for q in queues.values())
            logger.info(
                "Round-robin: %d airport(s), %d pending frame(s) (oldest first)",
                len(queues),
                total_pending,
            )
            _run_archive_round_robin(queues, airport_by_code, config, stats, deadline)
            if deadline is not None and any(queues.values()):
                stats["timed_out"] = True
            airports_without_webcams = [
                a
                for a in airports
                if _airport_code(a) and _airport_code(a) not in airport_by_code
            ]
            stats["airports_processed"] += len(airports_without_webcams)
            for airport in airports_without_webcams:
                code = _airport_code(airport)
                try:
                    _run_archive_current_only(airport, code, config, stats, run_ts)
                except Exception as exc:
                    logger.error(
                        "Error archiving current-only for %s: %s",
                        code,
                        exc,
                    )
                    stats["errors"] += 1
        except Exception as exc:
            logger.error("Error during round-robin archive: %s", exc)
            stats["errors"] += 1
    else:
        total_airports = len(airports)
        for idx, airport in enumerate(airports, start=1):
            if deadline is not None and time.time() >= deadline:
                elapsed_min = (time.time() - run_ts.timestamp()) / 60
                logger.info(
                    "Job stopped after %.1f min; next run will resume.",
                    elapsed_min,
                )
                stats["timed_out"] = True
                break

            code = _airport_code(airport)
            if not code:
                logger.debug("Skipping airport with no code/id/icao: %s", airport)
                continue

            stats["airports_processed"] += 1
            logger.info(
                "Archiving %s (airport %d/%d)...",
                code,
                idx,
                total_airports,
            )

            try:
                webcams = setup_airport_archive(airport, config)
                _run_archive_current_only(
                    airport, code, config, stats, run_ts, webcams=webcams
                )
            except Exception as exc:
                logger.error("Error archiving images for %s: %s", code, exc)
                stats["errors"] += 1
            _yield_for_web(config)

    if config.get("schedule", {}).get("retention_on_archive_run", False):
        apply_retention(config)
    logger.info(
        "Archive run complete: %d airport(s), %d image(s) fetched, "
        "%d saved, %d error(s).",
        stats["airports_processed"],
        stats["images_fetched"],
        stats["images_saved"],
        stats["errors"],
    )
    if stats["airports_processed"] > 0 and stats["images_fetched"] == 0:
        logger.warning(
            "No images were fetched for any airport. Check source.base_url "
            "and source.airports_api_url; set logging.level=DEBUG for details."
        )
    elif stats["images_fetched"] > 0 and stats["images_saved"] == 0:
        logger.warning(
            "Images were fetched but none saved. Check archive.output_dir "
            "permissions and disk space."
        )
    return stats
