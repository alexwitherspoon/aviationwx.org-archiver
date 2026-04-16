"""
AviationWX.org Archiver - Flask web GUI.

Provides a local web interface for:
  - Dashboard: status, stats, and recent log entries
  - Configuration: view and edit config.yaml via a form
  - Browse: explore archived images by airport and date
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from app.browse import (
    IMAGE_EXTENSIONS,
    build_preview_images,
    index_child_file_counts,
    index_list_all_filenames,
    paginate_list,
    parse_browse_path,
    safe_browse_segments,
    scandir_child_names,
    scandir_list_filenames,
)
from app.config import (
    DEFAULT_SLOW_REQUEST_LOG_SECONDS,
    _coerce_float_for_validation,
    save_config,
    validate_config,
)
from app.constants import (
    BYTES_PER_GIB,
    BYTES_PER_PIB,
    BYTES_PER_TIB,
    DEFAULT_BROWSE_PAGE_SIZE,
    DEFAULT_BROWSE_PREVIEW_IMAGE_LIMIT,
    DEFAULT_INTERVAL_MINUTES,
    DEFAULT_LOG_DISPLAY_COUNT,
    PERCENT_SCALE,
)
from app.scheduler import consume_archive_cache_dirty_flags, get_state, trigger_run
from app.version import GIT_SHA, VERSION

logger = logging.getLogger(__name__)

app = Flask(__name__)

# TTL cache for archive tree/stats (avoids full scans on every request)
# Keys: (output_dir, "stats") or (output_dir, "tree", browse_limit_int).
_archive_cache: dict[tuple, tuple[float, dict]] = {}
_archive_cache_lock = threading.Lock()
_ARCHIVE_CACHE_TTL_SEC = 60


def invalidate_archive_cache() -> None:
    """Clear cached archive tree/stats. Call after archive or retention runs."""
    with _archive_cache_lock:
        _archive_cache.clear()


def _maybe_invalidate_archive_cache() -> None:
    """Invalidate stats/tree cache entries when scheduler signals changes."""
    stats_d, tree_d = consume_archive_cache_dirty_flags()
    with _archive_cache_lock:
        if stats_d and tree_d:
            _archive_cache.clear()
            return
        if stats_d:
            for k in list(_archive_cache.keys()):
                if len(k) >= 2 and k[1] == "stats":
                    del _archive_cache[k]
        if tree_d:
            for k in list(_archive_cache.keys()):
                if len(k) >= 2 and k[1] == "tree":
                    del _archive_cache[k]


@app.before_request
def _start_request_timer():
    g._request_start = time.perf_counter()


@app.after_request
def _log_slow_request(response):
    cfg = app.config.get("ARCHIVER_CONFIG") or {}
    web_cfg = cfg.get("web") or {}
    raw = web_cfg.get("slow_request_log_seconds")
    if raw is None:
        raw = DEFAULT_SLOW_REQUEST_LOG_SECONDS
    threshold, thr_err = _coerce_float_for_validation(
        raw, "web.slow_request_log_seconds"
    )
    if thr_err or threshold is None or threshold <= 0:
        return response
    start = getattr(g, "_request_start", None)
    if start is not None:
        elapsed = time.perf_counter() - start
        if elapsed >= threshold:
            logger.warning(
                "Slow request %.2fs %s %s",
                elapsed,
                request.method,
                request.path,
            )
    return response


@app.context_processor
def _inject_version():
    """Make version and git_sha available in all templates."""
    return {"app_version": VERSION, "app_git_sha": GIT_SHA}


# ---------------------------------------------------------------------------
# Context / helpers
# ---------------------------------------------------------------------------


def _parse_timestamp_from_filename(filename: str) -> str | None:
    """
    Parse a timestamp from an archive filename. Returns formatted UTC string or None.

    Supports:
    - Unix timestamp: 1718456780_0.jpg
    - Date+time: 20240615_143000_webcam.jpg (YYYYMMDD_HHMMSS)
    """
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    parts = base.split("_")
    if len(parts) < 2:
        return None
    first = parts[0]
    if re.match(r"^\d{10,}$", first):
        try:
            ts = int(first)
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        except (ValueError, OSError):  # fmt: skip
            return None
    date_ok = re.match(r"^\d{8}$", first)
    time_ok = len(parts) >= 2 and re.match(r"^\d{6}$", parts[1])
    if date_ok and time_ok:
        y, m, d = first[:4], first[4:6], first[6:8]
        h, mi, s = parts[1][:2], parts[1][2:4], parts[1][4:6]
        return f"{y}-{m}-{d} {h}:{mi}:{s} UTC"
    return None


@app.template_filter("timestamp_from_filename")
def timestamp_from_filename_filter(filename: str) -> str:
    """Parse UTC timestamp from filename, or return '—' if unparseable."""
    result = _parse_timestamp_from_filename(filename)
    return result if result else "—"


def _effective_browse_airport_limit(config: dict | None) -> int:
    """Normalized browse_airport_limit for cache keys (must match uncached logic)."""
    if not config:
        return 0
    try:
        v = int((config.get("web") or {}).get("browse_airport_limit", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, v)


_BROWSE_LEVEL_NAMES = ("airports", "years", "months", "days", "cameras")


def _effective_browse_page_size(config: dict | None) -> int:
    """Rows per /api/browse/files page (clamped)."""
    if not config:
        return DEFAULT_BROWSE_PAGE_SIZE
    try:
        raw = (config.get("web") or {}).get("browse_page_size")
        if raw is None:
            v = DEFAULT_BROWSE_PAGE_SIZE
        else:
            v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_BROWSE_PAGE_SIZE
    return max(1, min(v, 10_000))


def _effective_browse_preview_limit(config: dict | None) -> int:
    """Max preview carousel entries (clamped)."""
    if not config:
        return DEFAULT_BROWSE_PREVIEW_IMAGE_LIMIT
    try:
        raw = (config.get("web") or {}).get("browse_preview_image_limit")
        if raw is None:
            v = DEFAULT_BROWSE_PREVIEW_IMAGE_LIMIT
        else:
            v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_BROWSE_PREVIEW_IMAGE_LIMIT
    return max(1, min(v, 50_000))


def _fname_is_image(fname: str) -> bool:
    if "." not in fname:
        return False
    return fname.rsplit(".", 1)[-1].lower() in IMAGE_EXTENSIONS


def _archive_tree_uncached(output_dir: str, config: dict | None = None) -> dict:
    """
    Build a nested dict representing the archive directory tree.

    Structure: {airport: {year: {month: {day: {camera: [filenames]}}}}}
    Layout: output_dir/AIRPORT/YYYY/MM/DD/camera_name/
    Uses index when valid (fast); falls back to scandir for large archives.
    """
    if not os.path.isdir(output_dir):
        return {}

    limit = _effective_browse_airport_limit(config)

    from app.archiver import (
        _archive_tree_from_index,
        _index_entries_valid,
        _load_archive_index,
    )

    data = _load_archive_index(output_dir)
    if data and "files" in data and _index_entries_valid(output_dir, data):
        tree = _archive_tree_from_index(data, output_dir)
        if tree is not None:
            if limit > 0:
                keys = sorted(tree.keys())[:limit]
                tree = {k: tree[k] for k in keys}
            return tree

    tree = {}
    try:
        with os.scandir(output_dir) as it:
            dirs = []
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                        dirs.append(e)
                except OSError:
                    pass
            airports = sorted(dirs, key=lambda e: e.name)
    except OSError:
        return tree

    if limit > 0:
        airports = airports[:limit]

    for airport_entry in airports:
        airport = airport_entry.name
        tree[airport] = {}
        try:
            with os.scandir(airport_entry.path) as it:
                dirs = []
                for e in it:
                    try:
                        if (
                            e.is_dir(follow_symlinks=False)
                            and e.name.isdigit()
                            and len(e.name) == 4
                        ):
                            dirs.append(e)
                    except OSError:
                        pass
                years = sorted(dirs, key=lambda e: e.name)
        except OSError:
            continue
        for year_entry in years:
            year = year_entry.name
            tree[airport][year] = {}
            try:
                with os.scandir(year_entry.path) as it:
                    dirs = []
                    for e in it:
                        try:
                            if (
                                e.is_dir(follow_symlinks=False)
                                and e.name.isdigit()
                                and len(e.name) == 2
                            ):
                                dirs.append(e)
                        except OSError:
                            pass
                    months = sorted(dirs, key=lambda e: e.name)
            except OSError:
                continue
            for month_entry in months:
                month = month_entry.name
                tree[airport][year][month] = {}
                try:
                    with os.scandir(month_entry.path) as it:
                        dirs = []
                        for e in it:
                            try:
                                if (
                                    e.is_dir(follow_symlinks=False)
                                    and e.name.isdigit()
                                    and len(e.name) == 2
                                ):
                                    dirs.append(e)
                            except OSError:
                                pass
                        days = sorted(dirs, key=lambda e: e.name)
                except OSError:
                    continue
                for day_entry in days:
                    day = day_entry.name
                    tree[airport][year][month][day] = {}
                    try:
                        with os.scandir(day_entry.path) as it:
                            dirs = []
                            for e in it:
                                try:
                                    if e.is_dir(follow_symlinks=False):
                                        dirs.append(e)
                                except OSError:
                                    pass
                            cameras = sorted(dirs, key=lambda e: e.name)
                    except OSError:
                        continue
                    for camera_entry in cameras:
                        camera = camera_entry.name
                        try:
                            with os.scandir(camera_entry.path) as it:
                                names = []
                                for e in it:
                                    try:
                                        if e.is_file(follow_symlinks=False):
                                            names.append(e.name)
                                    except OSError:
                                        pass
                                files = sorted(names)
                        except OSError:
                            files = []
                        tree[airport][year][month][day][camera] = files

    return tree


def _archive_tree(output_dir: str, config: dict | None = None) -> dict:
    """Cached wrapper for _archive_tree_uncached."""
    _maybe_invalidate_archive_cache()
    limit = _effective_browse_airport_limit(config)
    key = (output_dir, "tree", limit)
    now = time.time()
    with _archive_cache_lock:
        if key in _archive_cache:
            ts, data = _archive_cache[key]
            if now - ts < _ARCHIVE_CACHE_TTL_SEC:
                return data
    data = _archive_tree_uncached(output_dir, config)
    with _archive_cache_lock:
        _archive_cache[key] = (now, data)
    return data


def _format_size_in_unit(bytes_val: int, unit: str) -> float:
    """
    Convert bytes to the given unit (GB/TB/PB).

    Args:
        bytes_val: Size in bytes.
        unit: Target unit: "GB", "TB", or "PB".

    Returns:
        Size in the target unit, rounded to 2 decimal places.
    """
    if unit == "PB":
        return round(bytes_val / BYTES_PER_PIB, 2)
    if unit == "TB":
        return round(bytes_val / BYTES_PER_TIB, 2)
    return round(bytes_val / BYTES_PER_GIB, 2)


def _pick_display_unit(total_bytes: int) -> str:
    """
    Pick GB, TB, or PB based on total size.

    Args:
        total_bytes: Total disk size in bytes.

    Returns:
        "PB" if >= 1 PiB, "TB" if >= 1 TiB, else "GB".
    """
    if total_bytes >= BYTES_PER_PIB:
        return "PB"
    if total_bytes >= BYTES_PER_TIB:
        return "TB"
    return "GB"


def _disk_usage(path: str) -> dict | None:
    """
    Return disk usage for the filesystem containing path.

    Returns dict with used_gb, total_gb, free_gb, percent_used (raw),
    and used_fmt, free_fmt, total_fmt, unit (human-readable), or None on error.
    """
    try:
        usage = shutil.disk_usage(path)
        total_gb = usage.total / BYTES_PER_GIB
        used_gb = usage.used / BYTES_PER_GIB
        free_gb = usage.free / BYTES_PER_GIB
        percent = (usage.used / usage.total * PERCENT_SCALE) if usage.total else 0

        unit = _pick_display_unit(usage.total)
        used_val = _format_size_in_unit(usage.used, unit)
        free_val = _format_size_in_unit(usage.free, unit)
        total_val = _format_size_in_unit(usage.total, unit)

        return {
            "used_gb": round(used_gb, 2),
            "total_gb": round(total_gb, 2),
            "free_gb": round(free_gb, 2),
            "percent_used": round(percent, 1),
            "used_fmt": f"{used_val:,.2f}",
            "free_fmt": f"{free_val:,.2f}",
            "total_fmt": f"{total_val:,.2f}",
            "unit": unit,
        }
    except OSError as exc:
        logger.debug("Disk usage failed for %s: %s", path, exc)
        return None


def _archive_stats_uncached(output_dir: str, config: dict | None = None) -> dict:
    """
    Return basic stats about the archive directory. Uses index when available.

    Reads index without holding the lock (best-effort). Spot-check validates;
    on failure we rebuild under lock. Worst case: slightly stale stats or
    redundant rebuild, not corruption.
    """
    from app.archiver import (
        _index_entries_valid,
        _load_archive_index,
        _rebuild_archive_index,
        _scandir_walk_files,
    )

    total_files = 0
    total_size = 0
    airports: set = set()

    if not os.path.isdir(output_dir):
        return {
            "total_files": 0,
            "total_size_gb": 0.0,
            "airports": [],
            "disk_usage": _disk_usage(os.path.dirname(output_dir) or "/"),
        }

    data = _load_archive_index(output_dir)
    use_index = False
    if data and "files" in data and _index_entries_valid(output_dir, data):
        from app.archiver import _rel_path_safe

        files = data.get("files", {})
        for rel_path, entry in files.items():
            if not _rel_path_safe(output_dir, rel_path):
                break
            if not isinstance(entry, dict):
                break
            size = entry.get("size")
            if not isinstance(size, int):
                break
            total_files += 1
            total_size += size
            parts = os.path.normpath(rel_path).split(os.sep)
            if len(parts) >= 1:
                airports.add(parts[0])
        else:
            use_index = True
    if not use_index:
        total_files = 0
        total_size = 0
        airports.clear()
        collected: list[tuple[str, float, int]] = []
        for fpath, st in _scandir_walk_files(output_dir, config=config):
            total_files += 1
            total_size += st.st_size
            collected.append((fpath, st.st_mtime, st.st_size))
            parts = os.path.relpath(fpath, output_dir).split(os.sep)
            if len(parts) >= 1:
                airports.add(parts[0])
        # Non-blocking: skip index persist if archive worker holds lock
        _rebuild_archive_index(
            output_dir, config=None, pre_collected=collected, lock_timeout=2
        )

    return {
        "total_files": total_files,
        "total_size_gb": round(total_size / BYTES_PER_GIB, 3),
        "airports": sorted(airports),
        "disk_usage": _disk_usage(output_dir),
    }


def _archive_stats(output_dir: str, config: dict | None = None) -> dict:
    """Cached wrapper for _archive_stats_uncached."""
    _maybe_invalidate_archive_cache()
    key = (output_dir, "stats")
    now = time.time()
    with _archive_cache_lock:
        if key in _archive_cache:
            ts, data = _archive_cache[key]
            if now - ts < _ARCHIVE_CACHE_TTL_SEC:
                return data
    data = _archive_stats_uncached(output_dir, config)
    with _archive_cache_lock:
        _archive_cache[key] = (now, data)
    return data


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def dashboard():
    config = app.config["ARCHIVER_CONFIG"]
    config_errors = validate_config(config)
    state = get_state()
    output_dir = config["archive"]["output_dir"]
    archive_stats = _archive_stats(output_dir, config)
    log_count = config["web"].get("log_display_count", DEFAULT_LOG_DISPLAY_COUNT)
    recent_logs = list(reversed(state.get("log_entries", [])))[:log_count]
    return render_template(
        "dashboard.html",
        state=state,
        archive_stats=archive_stats,
        recent_logs=recent_logs,
        config=config,
        config_errors=config_errors,
    )


@app.route("/run", methods=["POST"])
def trigger_archive():
    config = app.config["ARCHIVER_CONFIG"]
    if validate_config(config):
        return redirect(url_for("configuration"))
    started = trigger_run(config)
    if started:
        logger.info("Manual archive run triggered via web GUI.")
    return redirect(url_for("dashboard"))


@app.route("/config", methods=["GET", "POST"])
def configuration():
    config = app.config["ARCHIVER_CONFIG"]
    message = None
    error = None

    if request.method == "POST":
        try:
            new_config = _form_to_config(request.form, config)
            if save_config(new_config):
                app.config["ARCHIVER_CONFIG"] = new_config
                config = new_config
                message = (
                    "Configuration saved successfully. Note: changes to scheduling "
                    "settings (such as interval minutes) will not take effect until "
                    "the application is restarted."
                )
            else:
                error = "Failed to save configuration. Check server logs."
        except ValueError as exc:
            error = f"Invalid configuration: {exc}"
        except (KeyError, TypeError) as exc:
            logger.warning("Configuration form error: %s", exc, exc_info=True)
            error = "Invalid form data. Check that all required fields are present."

    config_errors = validate_config(config)
    return render_template(
        "config.html",
        config=config,
        message=message,
        error=error,
        config_errors=config_errors,
    )


def _is_safe_archive_subpath(subpath: str) -> bool:
    """
    Validate subpath to prevent path injection. Reject traversal and absolute paths.
    """
    if not subpath or ".." in subpath:
        return False
    if subpath.startswith("/") or subpath.startswith("\\"):
        return False
    # Reject path components that could escape (e.g. drive letters on Windows)
    parts = subpath.replace("\\", "/").split("/")
    for part in parts:
        if not part or part in (".", ".."):
            return False
    return True


@app.route("/archive/<path:subpath>")
def serve_archive_file(subpath: str):
    """Serve a file from the archive directory. Safe against path traversal."""
    if not _is_safe_archive_subpath(subpath):
        abort(404)
    config = app.config["ARCHIVER_CONFIG"]
    output_dir = config["archive"]["output_dir"]
    full_path = os.path.normpath(os.path.join(output_dir, subpath))
    resolved_output = os.path.realpath(output_dir)
    resolved_path = os.path.realpath(full_path)
    # Require path to be strictly under output_dir (prevents root "/" bypass)
    if not resolved_path.startswith(resolved_output + os.sep):
        abort(404)
    if not os.path.isfile(full_path):
        abort(404)
    return send_file(
        full_path,
        mimetype=None,
        as_attachment=False,
        download_name=os.path.basename(full_path),
    )


def _browse_path_bad_request(exc: ValueError):
    """Map parse_browse_path errors to fixed API strings (do not echo str(exc))."""
    if exc.args and exc.args[0] == "path too deep":
        return jsonify({"error": "path has too many segments (maximum five)"}), 400
    return jsonify({"error": "invalid path"}), 400


@app.route("/api/browse/children")
def api_browse_children():
    """JSON: one tree level under path (lazy browse). Uses index when valid."""
    raw_path = request.args.get("path", "") or ""
    try:
        parts = parse_browse_path(raw_path)
    except ValueError as exc:
        return _browse_path_bad_request(exc)
    if not safe_browse_segments(parts):
        return jsonify({"error": "invalid path"}), 400
    # Deeper than cameras: no folder children; list files via /api/browse/files.
    if len(parts) >= len(_BROWSE_LEVEL_NAMES):
        return jsonify(
            {
                "error": (
                    "path too deep for folder listing; "
                    "use /api/browse/files under a camera folder"
                )
            }
        ), 400

    config = app.config["ARCHIVER_CONFIG"]
    output_dir = config["archive"]["output_dir"]
    if not os.path.isdir(output_dir):
        return jsonify(
            {
                "level": _BROWSE_LEVEL_NAMES[len(parts)],
                "items": [],
                "path": "/".join(parts),
            }
        )

    from app.archiver import _index_entries_valid, _load_archive_index

    data = _load_archive_index(output_dir)
    use_index = bool(
        data and "files" in data and _index_entries_valid(output_dir, data)
    )
    files = data.get("files", {}) if use_index and data else {}

    if use_index:
        counts = index_child_file_counts(files, parts)
        items = [
            {"name": name, "file_count": counts[name]} for name in sorted(counts.keys())
        ]
    else:
        names = scandir_child_names(output_dir, parts)
        items = [{"name": name, "file_count": None} for name in names]

    if len(parts) == 0:
        lim = _effective_browse_airport_limit(config)
        if lim > 0:
            items = items[:lim]

    return jsonify(
        {
            "level": _BROWSE_LEVEL_NAMES[len(parts)],
            "items": items,
            "path": "/".join(parts),
        }
    )


@app.route("/api/browse/files")
def api_browse_files():
    """JSON: paginated filenames under a camera path (five segments)."""
    raw_path = request.args.get("path", "") or ""
    try:
        parts = parse_browse_path(raw_path)
    except ValueError as exc:
        return _browse_path_bad_request(exc)
    if not safe_browse_segments(parts):
        return jsonify({"error": "invalid path"}), 400
    if len(parts) != 5:
        return jsonify(
            {"error": "path must be airport/year/month/day/camera (five segments)"}
        ), 400

    config = app.config["ARCHIVER_CONFIG"]
    output_dir = config["archive"]["output_dir"]
    offset = request.args.get("offset", 0, type=int) or 0
    if offset < 0:
        offset = 0

    page_size = _effective_browse_page_size(config)
    preview_limit = _effective_browse_preview_limit(config)

    from app.archiver import _index_entries_valid, _load_archive_index

    data = _load_archive_index(output_dir)
    use_index = bool(
        data and "files" in data and _index_entries_valid(output_dir, data)
    )
    files = data.get("files", {}) if use_index and data else {}

    if use_index:
        all_names = index_list_all_filenames(files, parts)
    else:
        all_names = scandir_list_filenames(output_dir, parts)

    total, page = paginate_list(all_names, offset, page_size)
    preview_images, preview_truncated = build_preview_images(
        all_names, parts, preview_limit
    )
    preview_lookup = {e["filename"]: i for i, e in enumerate(preview_images)}

    rows = []
    for i, fname in enumerate(page):
        ts = _parse_timestamp_from_filename(fname)
        rows.append(
            {
                "name": fname,
                "time_utc": ts if ts else "—",
                "row": offset + i + 1,
                "preview_slot": preview_lookup.get(fname, -1),
                "is_image": _fname_is_image(fname),
            }
        )

    return jsonify(
        {
            "path": "/".join(parts),
            "total": total,
            "offset": offset,
            "limit": page_size,
            "files": rows,
            "preview_images": preview_images,
            "preview_truncated": preview_truncated,
        }
    )


@app.route("/browse")
def browse():
    config = app.config["ARCHIVER_CONFIG"]
    output_dir = config["archive"]["output_dir"]
    return render_template(
        "browse.html",
        output_dir=output_dir,
        browse_preview_limit=_effective_browse_preview_limit(config),
        serve_archive_base=url_for("serve_archive_file", subpath=""),
    )


@app.route("/api/health")
def api_health():
    """Minimal liveness check (no archive scan). Use for Docker HEALTHCHECK."""
    return jsonify({"status": "ok"})


@app.route("/api/status")
def api_status():
    """JSON status endpoint for monitoring. Use light=1 to skip heavy archive stats."""
    state = get_state()
    config = app.config["ARCHIVER_CONFIG"]
    output_dir = config["archive"]["output_dir"]
    light = request.args.get("light", "").lower() in ("1", "true", "yes")
    if light:
        archive_stats = {
            "light": True,
            "disk_usage": _disk_usage(output_dir),
        }
    else:
        archive_stats = _archive_stats(output_dir, config)
    response = {
        "status": "ok",
        "version": VERSION,
        "git_sha": GIT_SHA or None,
        "running": state.get("running", False),
        "last_run": (
            state.get("last_run").isoformat() if state.get("last_run") else None
        ),
        "next_run": (
            state.get("next_run").isoformat() if state.get("next_run") else None
        ),
        "run_count": state.get("run_count", 0),
        "last_stats": state.get("last_stats"),
        "archive": archive_stats,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if archive_stats.get("disk_usage"):
        response["disk_usage"] = archive_stats["disk_usage"]
    return jsonify(response)


# ---------------------------------------------------------------------------
# Form helpers
# ---------------------------------------------------------------------------


def _form_to_config(form, existing_config: dict) -> dict:
    """Convert web form POST data into a config dict."""
    import copy

    config = copy.deepcopy(existing_config)

    # Schedule
    interval = int(form.get("interval_minutes", DEFAULT_INTERVAL_MINUTES))
    if interval < 1:
        raise ValueError("interval_minutes must be >= 1")
    config["schedule"]["interval_minutes"] = interval
    config["schedule"]["fetch_on_start"] = "fetch_on_start" in form

    # Archive
    output_dir = form.get("output_dir", "").strip()
    if not output_dir:
        raise ValueError("output_dir must not be empty")
    if ".." in output_dir or output_dir in ("/", "\\"):
        raise ValueError("output_dir must not be root or contain path traversal")
    config["archive"]["output_dir"] = output_dir

    retention = int(form.get("retention_days", 0))
    if retention < 0:
        raise ValueError("retention_days must be >= 0")
    config["archive"]["retention_days"] = retention

    retention_max = form.get("retention_max_gb", "0").strip()
    try:
        retention_max_gb = float(retention_max) if retention_max else 0.0
    except ValueError:
        retention_max_gb = 0.0
    if retention_max_gb < 0:
        raise ValueError("retention_max_gb must be >= 0")
    config["archive"]["retention_max_gb"] = retention_max_gb

    # Retention schedule
    config["schedule"]["retention_on_archive_run"] = "retention_on_archive_run" in form
    retention_hour = int(form.get("retention_hour", 3))
    if not 0 <= retention_hour <= 23:
        raise ValueError("retention_hour must be 0–23")
    config["schedule"]["retention_hour"] = retention_hour
    retention_minute = int(form.get("retention_minute", 0))
    if not 0 <= retention_minute <= 59:
        raise ValueError("retention_minute must be 0–59")
    config["schedule"]["retention_minute"] = retention_minute

    # Airports
    config["airports"]["archive_all"] = "archive_all" in form
    selected_raw = form.get("selected_airports", "")
    selected = [
        c.strip().upper()
        for c in selected_raw.replace(",", "\n").splitlines()
        if c.strip()
    ]
    config["airports"]["selected"] = selected

    # Source
    base_url = form.get("base_url", "").strip()
    if base_url:
        config["source"]["base_url"] = base_url

    api_key = form.get("api_key", "").strip()
    if api_key:
        config["source"]["api_key"] = api_key

    # Logging
    log_level = form.get("log_level", "INFO").strip().upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ValueError("Invalid log level")
    config["logging"]["level"] = log_level

    return config
