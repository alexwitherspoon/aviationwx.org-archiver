"""
AviationWX.org Archiver - Flask web GUI.

Provides a local web interface for:
  - Dashboard: status, stats, and recent log entries
  - Configuration: view and edit config.yaml via a form
  - Browse: explore archived images by date and airport
"""

import logging
import os
from datetime import datetime, timezone

from flask import Flask, jsonify, redirect, render_template, request, url_for

from app.config import save_config, validate_config
from app.scheduler import get_state, trigger_run

logger = logging.getLogger(__name__)

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Context / helpers
# ---------------------------------------------------------------------------


def _archive_tree(output_dir: str) -> dict:
    """
    Build a nested dict representing the archive directory tree.

    Structure: {year: {month: {day: {airport: [filenames]}}}}
    """
    tree = {}
    if not os.path.isdir(output_dir):
        return tree

    for year in sorted(os.listdir(output_dir)):
        year_path = os.path.join(output_dir, year)
        if not os.path.isdir(year_path) or not year.isdigit():
            continue
        tree[year] = {}
        for month in sorted(os.listdir(year_path)):
            month_path = os.path.join(year_path, month)
            if not os.path.isdir(month_path) or not month.isdigit():
                continue
            tree[year][month] = {}
            for day in sorted(os.listdir(month_path)):
                day_path = os.path.join(month_path, day)
                if not os.path.isdir(day_path) or not day.isdigit():
                    continue
                tree[year][month][day] = {}
                for airport in sorted(os.listdir(day_path)):
                    airport_path = os.path.join(day_path, airport)
                    if not os.path.isdir(airport_path):
                        continue
                    files = sorted(os.listdir(airport_path))
                    tree[year][month][day][airport] = files

    return tree


def _archive_stats(output_dir: str) -> dict:
    """Return basic stats about the archive directory."""
    total_files = 0
    total_size = 0
    airports: set = set()

    if not os.path.isdir(output_dir):
        return {"total_files": 0, "total_size_mb": 0.0, "airports": []}

    for root, _dirs, files in os.walk(output_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                total_files += 1
                total_size += os.path.getsize(fpath)
                # archive: output_dir/YYYY/MM/DD/AIRPORT/file — airport is parent
                parts = fpath.replace(output_dir, "").strip(os.sep).split(os.sep)
                if len(parts) >= 2:
                    airports.add(parts[-2])
            except OSError:
                pass

    return {
        "total_files": total_files,
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "airports": sorted(airports),
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
    log_count = config["web"].get("log_display_count", 100)
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

    config_errors = validate_config(config)
    return render_template(
        "config.html",
        config=config,
        message=message,
        error=error,
        config_errors=config_errors,
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
    return jsonify(
        {
            "status": "ok",
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
    )


# ---------------------------------------------------------------------------
# Form helpers
# ---------------------------------------------------------------------------


def _form_to_config(form, existing_config: dict) -> dict:
    """Convert web form POST data into a config dict."""
    import copy

    config = copy.deepcopy(existing_config)

    # Schedule
    interval = int(form.get("interval_minutes", 15))
    if interval < 1:
        raise ValueError("interval_minutes must be >= 1")
    config["schedule"]["interval_minutes"] = interval
    config["schedule"]["fetch_on_start"] = "fetch_on_start" in form

    # Archive
    output_dir = form.get("output_dir", "").strip()
    if not output_dir:
        raise ValueError("output_dir must not be empty")
    config["archive"]["output_dir"] = output_dir

    retention = int(form.get("retention_days", 0))
    if retention < 0:
        raise ValueError("retention_days must be >= 0")
    config["archive"]["retention_days"] = retention

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

    # Logging
    log_level = form.get("log_level", "INFO").strip().upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ValueError("Invalid log level")
    config["logging"]["level"] = log_level

    return config
