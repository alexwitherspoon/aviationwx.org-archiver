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
from datetime import datetime, timezone

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from app.config import save_config, validate_config
from app.constants import (
    BYTES_PER_GIB,
    BYTES_PER_MIB,
    BYTES_PER_PIB,
    BYTES_PER_TIB,
    DEFAULT_INTERVAL_MINUTES,
    DEFAULT_LOG_DISPLAY_COUNT,
    PERCENT_SCALE,
)
from app.scheduler import get_state, trigger_run
from app.version import GIT_SHA, VERSION

logger = logging.getLogger(__name__)

app = Flask(__name__)


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
        except (ValueError, OSError):
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


def _archive_tree(output_dir: str) -> dict:
    """
    Build a nested dict representing the archive directory tree.

    Structure: {airport: {year: {month: {day: {camera: [filenames]}}}}}
    Layout: output_dir/AIRPORT/YYYY/MM/DD/camera_name/
    """
    tree = {}
    if not os.path.isdir(output_dir):
        return tree

    for airport in sorted(os.listdir(output_dir)):
        airport_path = os.path.join(output_dir, airport)
        if not os.path.isdir(airport_path):
            continue
        tree[airport] = {}
        for year in sorted(os.listdir(airport_path)):
            year_path = os.path.join(airport_path, year)
            if not os.path.isdir(year_path) or not year.isdigit():
                continue
            tree[airport][year] = {}
            for month in sorted(os.listdir(year_path)):
                month_path = os.path.join(year_path, month)
                if not os.path.isdir(month_path) or not month.isdigit():
                    continue
                tree[airport][year][month] = {}
                for day in sorted(os.listdir(month_path)):
                    day_path = os.path.join(month_path, day)
                    if not os.path.isdir(day_path) or not day.isdigit():
                        continue
                    tree[airport][year][month][day] = {}
                    for camera in sorted(os.listdir(day_path)):
                        camera_path = os.path.join(day_path, camera)
                        if not os.path.isdir(camera_path):
                            continue
                        files = sorted(os.listdir(camera_path))
                        tree[airport][year][month][day][camera] = files

    return tree


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


def _archive_stats(output_dir: str) -> dict:
    """Return basic stats about the archive directory."""
    total_files = 0
    total_size = 0
    airports: set = set()

    if not os.path.isdir(output_dir):
        return {
            "total_files": 0,
            "total_size_mb": 0.0,
            "airports": [],
            "disk_usage": _disk_usage(os.path.dirname(output_dir) or "/"),
        }

    for root, _dirs, files in os.walk(output_dir):
        for fname in files:
            if fname == "metadata.json":
                continue
            fpath = os.path.join(root, fname)
            try:
                total_files += 1
                total_size += os.path.getsize(fpath)
                # archive: output_dir/AIRPORT/YYYY/MM/DD/file — airport is first
                parts = fpath.replace(output_dir, "").strip(os.sep).split(os.sep)
                if len(parts) >= 1:
                    airports.add(parts[0])
            except OSError as exc:
                logger.debug("Could not stat %s: %s", fpath, exc)

    return {
        "total_files": total_files,
        "total_size_mb": round(total_size / BYTES_PER_MIB, 2),
        "airports": sorted(airports),
        "disk_usage": _disk_usage(output_dir),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def dashboard():
    config = app.config["ARCHIVER_CONFIG"]
    config_errors = validate_config(config)
    state = get_state()
    output_dir = config["archive"]["output_dir"]
    archive_stats = _archive_stats(output_dir)
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
                message = "Configuration saved successfully."
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


@app.route("/browse")
def browse():
    config = app.config["ARCHIVER_CONFIG"]
    output_dir = config["archive"]["output_dir"]
    tree = _archive_tree(output_dir)
    return render_template("browse.html", tree=tree, output_dir=output_dir)


@app.route("/api/status")
def api_status():
    """JSON status endpoint for health checks and monitoring."""
    state = get_state()
    config = app.config["ARCHIVER_CONFIG"]
    output_dir = config["archive"]["output_dir"]
    archive_stats = _archive_stats(output_dir)
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
