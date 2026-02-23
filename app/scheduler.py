"""
AviationWX.org Archiver - Background scheduler.

Runs archive passes on a configurable interval using APScheduler.
"""

import logging
import threading
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.archiver import run_archive
from app.config import validate_config

logger = logging.getLogger(__name__)

# Shared in-memory state (written by scheduler thread, read by web thread)
_state_lock = threading.Lock()
_state = {
    "last_run": None,  # datetime | None
    "last_stats": None,  # dict | None
    "next_run": None,  # datetime | None
    "running": False,  # bool — True while a run is in progress
    "run_count": 0,  # int — total number of completed runs
    "log_entries": [],  # list[dict] — recent log entries for the web GUI
}

_MAX_LOG_ENTRIES = 200


def get_state() -> dict:
    """Return a copy of the current scheduler state."""
    with _state_lock:
        return dict(_state)


def _append_log(message: str, level: str = "INFO") -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
    }
    with _state_lock:
        _state["log_entries"].append(entry)
        if len(_state["log_entries"]) > _MAX_LOG_ENTRIES:
            _state["log_entries"] = _state["log_entries"][-_MAX_LOG_ENTRIES:]


class _SchedulerLogHandler(logging.Handler):
    """Captures log records from the archiver and stores them in _state."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _append_log(self.format(record), record.levelname)
        except Exception:
            pass


def _archive_job(config: dict) -> None:
    """Scheduled job: run one archive pass and update shared state."""
    errors = validate_config(config)
    if errors:
        for err in errors:
            logger.warning("Config validation: %s", err)
            _append_log(f"Config error: {err}", "WARNING")
        return

    with _state_lock:
        if _state["running"]:
            logger.warning("Archive run skipped — previous run still in progress.")
            return
        _state["running"] = True

    _append_log("Archive run started.", "INFO")
    try:
        stats = run_archive(config)
        with _state_lock:
            _state["last_run"] = datetime.now(timezone.utc)
            _state["last_stats"] = stats
            _state["run_count"] += 1
            _state["running"] = False
        _append_log(
            f"Archive run complete — airports: {stats['airports_processed']}, "
            f"images fetched: {stats['images_fetched']}, "
            f"saved: {stats['images_saved']}, "
            f"errors: {stats['errors']}.",
            "INFO",
        )
    except Exception as exc:
        logger.error("Unhandled error in archive job: %s", exc)
        _append_log(f"Archive run failed: {exc}", "ERROR")
        with _state_lock:
            _state["running"] = False


def start_scheduler(config_getter) -> BackgroundScheduler:
    """
    Create, configure, and start the background scheduler.

    Args:
        config_getter: Callable returning the current config dict. Used so
            config changes via the web UI are picked up on each scheduled run.

    Returns:
        The scheduler instance for graceful shutdown.
    """
    # Attach handler so archiver logs appear in the web GUI
    handler = _SchedulerLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("app.archiver").addHandler(handler)
    logging.getLogger("app.scheduler").addHandler(handler)

    def _job_wrapper() -> None:
        config = config_getter()
        _archive_job(config)

    config = config_getter()
    interval_minutes = config["schedule"].get("interval_minutes", 15)

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        _job_wrapper,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="archive",
        name="AviationWX Archiver",
        replace_existing=True,
    )
    scheduler.start()

    job = scheduler.get_job("archive")
    if job and job.next_run_time:
        with _state_lock:
            _state["next_run"] = job.next_run_time

    logger.info("Scheduler started — interval: %d minute(s).", interval_minutes)
    _append_log(f"Scheduler started — interval: {interval_minutes} minute(s).", "INFO")

    if config["schedule"].get("fetch_on_start", True):
        logger.info("fetch_on_start is enabled — running initial archive pass.")
        _append_log("Running initial archive pass (fetch_on_start).", "INFO")
        threading.Thread(target=_job_wrapper, daemon=True).start()

    return scheduler


def trigger_run(config: dict) -> bool:
    """
    Trigger an immediate archive run in a background thread.

    Args:
        config: Configuration dict for the archive run.

    Returns:
        True if the run was started, False if already running or config invalid.
    """
    if validate_config(config):
        return False
    with _state_lock:
        if _state["running"]:
            return False
    threading.Thread(target=_archive_job, args=[config], daemon=True).start()
    return True
