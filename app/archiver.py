"""
AviationWX.org Archiver - Core image fetching and archival logic.

Fetches webcam images from AviationWX.org and organises them on disk as:
    <output_dir>/<AIRPORT_CODE>/<YYYY>/<MM>/<DD>/<camera_name>/<filename>
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests

from app.constants import (
    BYTES_PER_GIB,
    DEFAULT_REQUEST_DELAY_SECONDS,
    MD5_READ_CHUNK_SIZE,
    SECONDS_PER_DAY,
    SECONDS_PER_MINUTE,
)

logger = logging.getLogger(__name__)


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


def _rate_limit(config: dict) -> None:
    """
    Sleep before API requests to respect rate limits.

    AviationWX anonymous: 100 req/min; Partner: 500 req/min. Default uses half
    of anonymous limit (50 req/min). Set to 0 for Partner API keys.
    """
    delay = config.get("source", {}).get(
        "request_delay_seconds", DEFAULT_REQUEST_DELAY_SECONDS
    )
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
            resp = requests.get(url, timeout=timeout, headers=_api_headers(config))
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
        resp = requests.get(webcam_api, timeout=timeout, headers=_api_headers(config))
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
        resp = requests.get(page_url, timeout=timeout, headers=_api_headers(config))
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
        resp = requests.get(webcam_api, timeout=timeout, headers=_api_headers(config))
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
        for webcam in webcams:
            cam_name = webcam.get("name")
            cam_safe = _sanitize_camera_name(
                cam_name or "", fallback=f"cam_{webcam.get('index', 0)}"
            )
            date_path = os.path.join(
                airport_root,
                run_ts.strftime("%Y"),
                run_ts.strftime("%m"),
                run_ts.strftime("%d"),
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
        resp = requests.get(full_url, timeout=timeout, headers=_api_headers(config))
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
    """
    existing: set[tuple[int, int]] = set()
    airport_upper = airport_code.upper()
    if not os.path.isdir(output_dir):
        logger.debug(
            "Output dir %s missing; no existing frames for %s",
            output_dir,
            airport_code,
        )
        return existing

    for root, _dirs, files in os.walk(output_dir):
        rel = os.path.relpath(root, output_dir)
        parts = rel.split(os.sep)
        if len(parts) < 5:
            continue
        if parts[0].upper() != airport_upper:
            continue
        for fname in files:
            base, ext = os.path.splitext(fname)
            if ext.lower() not in (".jpg", ".jpeg", ".webp"):
                continue
            underscore = base.rfind("_")
            if underscore == -1:
                continue
            try:
                ts = int(base[:underscore])
                cam = int(base[underscore + 1 :])
                existing.add((ts, cam))
            except ValueError:
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
            resp = requests.get(
                url, timeout=timeout, stream=True, headers=_api_headers(config)
            )
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


def save_history_image(
    image_data: bytes,
    airport_code: str,
    cam_index: int,
    frame_ts: int,
    config: dict,
    camera_name: str | None = None,
) -> str | None:
    """
    Save a history API frame to the archive.

    Filename: {ts}_{cam}.jpg for uniqueness.
    Directory: output_dir/AIRPORT/YYYY/MM/DD/camera_name/
    Files are created with mode 0o644 (owner rw, group/others r).
    """
    output_dir = config["archive"]["output_dir"]
    cam_safe = _sanitize_camera_name(camera_name or "", fallback=f"cam_{cam_index}")
    dt = datetime.fromtimestamp(frame_ts, tz=timezone.utc)
    date_path = os.path.join(
        output_dir,
        airport_code.upper(),
        dt.strftime("%Y"),
        dt.strftime("%m"),
        dt.strftime("%d"),
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
            return filepath

    try:
        with open(filepath, "wb") as fh:
            fh.write(image_data)
        os.chmod(filepath, 0o644)
        os.utime(filepath, (frame_ts, frame_ts))
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


def save_image(
    image_data: bytes,
    url: str,
    airport_code: str,
    config: dict,
    timestamp: datetime | None = None,
    camera_name: str | None = None,
) -> str | None:
    """
    Save image bytes to the archive directory.

    Directory structure: output_dir/AIRPORT/YYYY/MM/DD/camera_name/filename

    The filename is derived from the URL basename; a timestamp prefix is added
    to avoid collisions when the same URL is fetched repeatedly.
    Files are created with mode 0o644 (owner rw, group/others r).

    Returns the saved file path on success, or None on failure.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    output_dir = config["archive"]["output_dir"]
    cam_safe = _sanitize_camera_name(camera_name or "", fallback="current")
    date_path = os.path.join(
        output_dir,
        airport_code.upper(),
        timestamp.strftime("%Y"),
        timestamp.strftime("%m"),
        timestamp.strftime("%d"),
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
            return filepath

    try:
        with open(filepath, "wb") as fh:
            fh.write(image_data)
        os.chmod(filepath, 0o644)
        ts = timestamp.timestamp()
        os.utime(filepath, (ts, ts))
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


def _collect_archive_files(output_dir: str) -> list[tuple[str, float, int]]:
    """
    Walk archive directory and return list of (path, mtime, size) for all files.

    Used for retention by size (oldest-first deletion).
    Excludes metadata.json (updated each run, not versioned).
    """
    result: list[tuple[str, float, int]] = []
    for root, _dirs, files in os.walk(output_dir):
        for fname in files:
            if fname == "metadata.json":
                continue
            fpath = os.path.join(root, fname)
            try:
                stat = os.stat(fpath)
                result.append((fpath, stat.st_mtime, stat.st_size))
            except OSError as exc:
                logger.debug("Retention: could not stat %s: %s", fpath, exc)
    return result


def apply_retention(config: dict) -> int:
    """
    Delete archived files based on retention rules.

    Supports:
    - ``archive.retention_days``: Delete files older than N days (0 = disabled).
    - ``archive.retention_max_gb``: Keep total size under N GB by deleting
      oldest files first (0 = disabled).

    Both can be used together; each rule is applied independently.

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

    if retention_days <= 0 and retention_max_bytes <= 0:
        logger.debug(
            "Retention disabled (retention_days=%s, retention_max_gb=%s)",
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

    # Phase 1: Delete by age
    if retention_days > 0:
        cutoff = datetime.now(timezone.utc).timestamp() - (
            retention_days * SECONDS_PER_DAY
        )
        logger.debug(
            "Retention: scanning by age (cutoff %d days, before %s)",
            retention_days,
            datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(),
        )
        for root, _dirs, files in os.walk(output_dir):
            for fname in files:
                if fname == "metadata.json":
                    continue
                fpath = os.path.join(root, fname)
                try:
                    if os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
                        deleted += 1
                except OSError as exc:
                    logger.warning("Retention: failed to remove %s: %s", fpath, exc)

    # Phase 2: Delete by size (oldest first) until under limit
    if retention_max_bytes > 0:
        files_sorted = _collect_archive_files(output_dir)
        files_sorted.sort(key=lambda x: x[1])  # mtime ascending (oldest first)
        total_bytes = sum(s for _, _, s in files_sorted)
        to_remove = total_bytes - retention_max_bytes

        if to_remove > 0:
            logger.debug(
                "Retention: total %.1f GB exceeds max %.1f GB; removing oldest",
                total_bytes / BYTES_PER_GIB,
                retention_max_bytes / BYTES_PER_GIB,
            )
            removed_bytes = 0
            for fpath, _mtime, size in files_sorted:
                if removed_bytes >= to_remove:
                    break
                try:
                    os.remove(fpath)
                    deleted += 1
                    removed_bytes += size
                except OSError as exc:
                    logger.warning("Retention: failed to remove %s: %s", fpath, exc)

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

            if frame is not None:
                stats["images_fetched"] += 1
                image_data = download_image(frame["url"], config)
                if image_data is None:
                    continue
                saved = save_history_image(
                    image_data,
                    code,
                    cam_index,
                    frame["timestamp"],
                    config,
                    camera_name=cam_name,
                )
                if saved:
                    stats["images_saved"] += 1
            else:
                url = _webcam_to_image_url(webcam, config)
                if url:
                    stats["images_fetched"] += 1
                    image_data = download_image(url, config)
                    if image_data is not None:
                        saved = save_image(
                            image_data,
                            url,
                            code,
                            config,
                            timestamp=run_ts,
                            camera_name=cam_name,
                        )
                        if saved:
                            stats["images_saved"] += 1
            progress = True
        if not progress:
            break


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
                image_data = download_image(frame["url"], config)
                if image_data is None:
                    continue
                cam_name = webcam.get("name")
                saved = save_history_image(
                    image_data, code, cam_index, ts, config, camera_name=cam_name
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
                image_data = download_image(url, config)
                if image_data is not None:
                    cam_name = webcam.get("name")
                    saved = save_image(
                        image_data,
                        url,
                        code,
                        config,
                        timestamp=run_ts,
                        camera_name=cam_name,
                    )
                    if saved:
                        stats["images_saved"] += 1
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
    if webcams is None:
        webcams = _fetch_webcams_list(airport, config)
    if webcams:
        for webcam in webcams:
            url = _webcam_to_image_url(webcam, config)
            if not url:
                continue
            stats["images_fetched"] += 1
            image_data = download_image(url, config)
            if image_data is None:
                continue
            cam_name = webcam.get("name")
            saved = save_image(
                image_data, url, code, config, timestamp=run_ts, camera_name=cam_name
            )
            if saved:
                stats["images_saved"] += 1
    else:
        image_urls = fetch_image_urls(airport, config)
        if not image_urls:
            logger.debug("No image URLs for %s (current-only mode)", code)
        for url in image_urls:
            stats["images_fetched"] += 1
            image_data = download_image(url, config)
            if image_data is None:
                continue
            saved = save_image(image_data, url, code, config, timestamp=run_ts)
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
                logger.info(
                    "Job stopped after %d min; next run will resume.",
                    timeout_min,
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
