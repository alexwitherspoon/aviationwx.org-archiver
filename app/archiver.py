"""
AviationWX.org Archiver - Core image fetching and archival logic.

Fetches webcam images from AviationWX.org and organises them on disk as:
    <output_dir>/<YYYY>/<MM>/<DD>/<AIRPORT_CODE>/<filename>
"""

import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)


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

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            # API returns {"airports": [...]} or a bare list
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("airports", data.get("data", []))
        except requests.RequestException as exc:
            logger.warning(
                "Attempt %d/%d: failed to fetch airport list from %s: %s",
                attempt, retries, url, exc,
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
        return all_airports

    selected_codes = {c.upper() for c in config["airports"].get("selected", [])}
    if not selected_codes:
        logger.warning("No airports selected and archive_all is false; nothing to archive.")
        return []

    filtered = [a for a in all_airports if _airport_code(a).upper() in selected_codes]
    found_codes = {_airport_code(a).upper() for a in filtered}
    missing = selected_codes - found_codes
    if missing:
        logger.warning("Selected airports not found in API response: %s", ", ".join(sorted(missing)))

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
    webcam_api = api_url.rstrip("/").rsplit("/airports", 1)[0] + f"/v1/airports/{code}/webcams"
    try:
        resp = requests.get(webcam_api, timeout=timeout)
        if resp.ok:
            data = resp.json()
            urls = _extract_urls_from_api(data, base_url)
            if urls:
                logger.debug("Found %d image(s) for %s via API", len(urls), code)
                return urls
    except requests.RequestException:
        pass

    # 2. Fall back: fetch the airport page and look for image tags
    page_url = f"{base_url.rstrip('/')}/?airport={code.lower()}"
    try:
        resp = requests.get(page_url, timeout=timeout)
        if resp.ok:
            urls = _scrape_image_urls(resp.text, base_url)
            logger.debug("Found %d image(s) for %s via page scrape", len(urls), code)
            return urls
    except requests.RequestException as exc:
        logger.warning("Failed to fetch airport page for %s: %s", code, exc)

    return []


def _extract_urls_from_api(data: dict | list, base_url: str) -> list[str]:
    """Extract image URLs from a webcam API response."""
    urls = []
    items = data if isinstance(data, list) else data.get("webcams", data.get("data", []))
    for item in items:
        if isinstance(item, dict):
            for key in ("image_url", "url", "src", "snapshot_url"):
                if key in item and isinstance(item[key], str) and item[key]:
                    urls.append(_absolute_url(item[key], base_url))
                    break
    return urls


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
        tag = html[start:end + 1]
        src = _extract_attr(tag, "src")
        if src and _looks_like_webcam(src):
            urls.append(_absolute_url(src, base_url))
        pos = end + 1
    return urls


def _extract_attr(tag: str, attr: str) -> str:
    """Extract an attribute value from an HTML tag string."""
    lower = tag.lower()
    search = f'{attr}='
    idx = lower.find(search)
    if idx == -1:
        return ""
    idx += len(search)
    if idx >= len(tag):
        return ""
    quote = tag[idx]
    if quote in ('"', "'"):
        end = tag.find(quote, idx + 1)
        return tag[idx + 1:end] if end != -1 else ""
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
    has_image_ext = any(lower.endswith(ext) or (ext + "?") in lower for ext in image_exts)
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

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                logger.debug("Skipping non-image URL %s (content-type: %s)", url, content_type)
                return None
            return resp.content
        except requests.RequestException as exc:
            logger.warning(
                "Attempt %d/%d: failed to download %s: %s",
                attempt, retries, url, exc,
            )
            if attempt < retries:
                time.sleep(delay)

    logger.error("All %d attempts to download %s failed.", retries, url)
    return None


def save_image(image_data: bytes, url: str, airport_code: str, config: dict, timestamp: datetime | None = None) -> str | None:
    """
    Save image bytes to the archive directory.

    Directory structure: <output_dir>/<YYYY>/<MM>/<DD>/<AIRPORT_CODE>/<filename>

    The filename is derived from the URL basename; a timestamp prefix is added
    to avoid collisions when the same URL is fetched repeatedly.

    Returns the saved file path on success, or None on failure.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    output_dir = config["archive"]["output_dir"]
    date_path = os.path.join(
        output_dir,
        timestamp.strftime("%Y"),
        timestamp.strftime("%m"),
        timestamp.strftime("%d"),
        airport_code.upper(),
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
        logger.info("Archived %s -> %s", url, filepath)
        return filepath
    except OSError as exc:
        logger.error("Failed to write image to %s: %s", filepath, exc)
        return None


def _file_md5(path: str) -> str:
    """Return the MD5 hex digest of a file."""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Retention / cleanup
# ---------------------------------------------------------------------------

def apply_retention(config: dict) -> int:
    """
    Delete archived files older than ``archive.retention_days``.

    Returns the number of files deleted.
    """
    retention_days = config["archive"].get("retention_days", 0)
    if retention_days <= 0:
        return 0

    output_dir = config["archive"]["output_dir"]
    cutoff = datetime.now(timezone.utc).timestamp() - (retention_days * 86400)
    deleted = 0

    for root, _dirs, files in os.walk(output_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                if os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    deleted += 1
            except OSError:
                pass

    if deleted:
        logger.info("Retention cleanup: deleted %d file(s) older than %d days.", deleted, retention_days)

    return deleted


# ---------------------------------------------------------------------------
# High-level archive run
# ---------------------------------------------------------------------------

def run_archive(config: dict, stats: dict | None = None) -> dict:
    """
    Perform a single full archive pass.

    Fetches the airport list, selects configured airports, fetches and saves
    images for each, and applies retention policy.

    Returns a stats dict with keys: airports_processed, images_fetched, images_saved, errors.
    """
    if stats is None:
        stats = {"airports_processed": 0, "images_fetched": 0, "images_saved": 0, "errors": 0}

    logger.info("Starting archive run...")
    run_ts = datetime.now(timezone.utc)

    all_airports = fetch_airport_list(config)
    if not all_airports:
        logger.warning("No airports returned from API; skipping run.")
        stats["errors"] += 1
        return stats

    airports = select_airports(all_airports, config)
    logger.info("Archiving %d airport(s).", len(airports))

    for airport in airports:
        code = _airport_code(airport)
        if not code:
            continue

        stats["airports_processed"] += 1

        try:
            image_urls = fetch_image_urls(airport, config)
        except Exception as exc:
            logger.error("Error fetching image URLs for %s: %s", code, exc)
            stats["errors"] += 1
            continue

        for url in image_urls:
            stats["images_fetched"] += 1
            try:
                image_data = download_image(url, config)
                if image_data is None:
                    continue
                saved = save_image(image_data, url, code, config, timestamp=run_ts)
                if saved:
                    stats["images_saved"] += 1
            except Exception as exc:
                logger.error("Error archiving image %s for %s: %s", url, code, exc)
                stats["errors"] += 1

    apply_retention(config)
    logger.info(
        "Archive run complete: %d airport(s), %d image(s) fetched, %d saved, %d error(s).",
        stats["airports_processed"],
        stats["images_fetched"],
        stats["images_saved"],
        stats["errors"],
    )
    return stats
