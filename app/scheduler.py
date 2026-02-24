"""
AviationWX.org Archiver - Background scheduler.

Runs archive passes on a configurable interval using APScheduler.
Archive jobs run in a separate process to avoid GIL contention with the web UI.
"""

import json
import logging
import multiprocessing
import threading
import time
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import validate_config
from app.constants import DEFAULT_INTERVAL_MINUTES
from app.worker import MSG_COMPLETE, MSG_LOG, run_archive_worker, run_retention_worker

logger = logging.getLogger(__name__)

# Shared in-memory state (written by scheduler thread, read by web thread)
_state_lock = threading.Lock()
_state = {
    "last_run": None,  # datetime | None
    "last_stats": None,  # dict | None
    "next_run": None,  # datetime | None
    "running": False,  # bool — True while a run is in progress
    "_running_since": None,  # float | None — time.time() when run started
    "run_count": 0,  # int — total number of completed runs
    "log_entries": [],  # list[dict] — recent log entries for the web GUI
    "_log_bytes": 0,  # internal: approximate byte size of log_entries
    "_archive_cache_dirty": False,  # True after archive/retention; web clears cache
}

_MAX_LOG_BYTES = 500 * 1024  # 500 KB


def get_state() -> dict:
    """Return a copy of the current scheduler state."""
    with _state_lock:
        return {k: v for k, v in _state.items() if not k.startswith("_")}


def clear_archive_cache_dirty() -> bool:
    """
    Clear the archive-cache-dirty flag. Returns True if it was set.

    Web calls this before serving stats/tree; if True, web should invalidate
    its cache. Avoids scheduler importing app.web (circular import / deadlock).
    """
    with _state_lock:
        was_dirty = _state.pop("_archive_cache_dirty", False)
        return was_dirty


def _append_log(message: str, level: str = "INFO") -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
    }
    with _state_lock:
        entry_bytes = len(json.dumps(entry))
        _state["log_entries"].append(entry)
        _state["_log_bytes"] = _state.get("_log_bytes", 0) + entry_bytes

        while _state["_log_bytes"] > _MAX_LOG_BYTES and len(_state["log_entries"]) > 1:
            removed = _state["log_entries"].pop(0)
            _state["_log_bytes"] -= len(json.dumps(removed))


class _SchedulerLogHandler(logging.Handler):
    """Captures log records from the archiver and stores them in _state."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _append_log(self.format(record), record.levelname)
        except Exception as exc:
            # Log to root logger to avoid losing the failure (don't use _append_log
            # to avoid recursion if that is also failing)
            logging.getLogger().warning(
                "Scheduler log handler failed to store entry: %s", exc
            )


def _apply_log_level(config: dict) -> None:
    """Apply logging level from config (so web UI changes take effect on next run)."""
    level_str = config.get("logging", {}).get("level", "INFO").upper()
    level = getattr(logging, level_str, logging.INFO)
    logging.getLogger().setLevel(level)


def _archive_job(config: dict) -> None:
    """Scheduled job: run one archive pass and update shared state."""
    _apply_log_level(config)
    errors = validate_config(config)
    if errors:
        for err in errors:
            logger.warning("Config validation: %s", err)
            _append_log(f"Config error: {err}", "WARNING")
        return

    interval_minutes = max(
        1,
        config["schedule"].get("interval_minutes", DEFAULT_INTERVAL_MINUTES),
    )
    # Stale threshold: 2x interval — if running longer, assume previous run died
    stale_threshold_seconds = interval_minutes * 2 * 60

    with _state_lock:
        if _state["running"]:
            running_since = _state.get("_running_since")
            elapsed = time.time() - running_since if running_since else 0
            if running_since is not None and elapsed > stale_threshold_seconds:
                logger.warning(
                    "Previous run appears stuck (%.0f min) — clearing lock.",
                    elapsed / 60,
                )
                _append_log(
                    "Previous run appears stuck — clearing lock and starting new run.",
                    "WARNING",
                )
                _state["running"] = False
                _state["_running_since"] = None
            else:
                logger.warning("Archive run skipped — previous run still in progress.")
                return
        _state["running"] = True
        _state["_running_since"] = time.time()

    logger.debug("Starting archive job.")
    _append_log("Archive run started.", "INFO")

    result_queue: multiprocessing.Queue = multiprocessing.Queue()
    process = multiprocessing.Process(
        target=run_archive_worker,
        args=(config, result_queue),
        name="archive-worker",
    )
    process.start()

    # Consume queue: log messages -> _append_log; complete -> result
    result = None
    while result is None:
        try:
            msg = result_queue.get(timeout=0.5)
            if msg.get("type") == MSG_LOG:
                _append_log(msg["message"], msg.get("level", "INFO"))
            elif msg.get("type") == MSG_COMPLETE:
                result = msg
        except Exception:
            if not process.is_alive():
                try:
                    result = result_queue.get(timeout=1)
                except Exception:
                    result = {"stats": None, "error": "Worker exited without result"}
                break

    process.join()

    try:
        if result["error"]:
            logger.error("Archive run failed: %s", result["error"])
            _append_log(f"Archive run failed: {result['error']}", "ERROR")
        else:
            stats = result["stats"]
            with _state_lock:
                _state["last_run"] = datetime.now(timezone.utc)
                _state["last_stats"] = stats
                _state["run_count"] += 1
            suffix = (
                " (stopped at timeout, will resume next run)"
                if stats.get("timed_out")
                else ""
            )
            msg = (
                f"Archive run complete{suffix} — airports: "
                f"{stats['airports_processed']}, images fetched: "
                f"{stats['images_fetched']}, saved: {stats['images_saved']}, "
                f"errors: {stats['errors']}."
            )
            _append_log(msg, "INFO")
    finally:
        with _state_lock:
            _state["running"] = False
            _state["_running_since"] = None
            _state["_archive_cache_dirty"] = True


def _retention_job(config: dict) -> None:
    """Scheduled job: run retention cleanup in a separate process."""
    _apply_log_level(config)
    errors = validate_config(config)
    if errors:
        for err in errors:
            logger.warning("Config validation: %s", err)
            _append_log(f"Config error: {err}", "WARNING")
        return

    retention_days = config.get("archive", {}).get("retention_days", 0)
    retention_max_gb = config.get("archive", {}).get("retention_max_gb", 0)
    if isinstance(retention_max_gb, str):
        from app.constants import parse_storage_gb

        retention_max_gb = parse_storage_gb(retention_max_gb)
    if retention_days <= 0 and (not retention_max_gb or retention_max_gb <= 0):
        logger.debug(
            "Retention job skipped: retention_days and retention_max_gb disabled."
        )
        return

    logger.info("Starting retention cleanup job.")
    _append_log("Retention cleanup started.", "INFO")

    result_queue: multiprocessing.Queue = multiprocessing.Queue()
    process = multiprocessing.Process(
        target=run_retention_worker,
        args=(config, result_queue),
        name="retention-worker",
    )
    process.start()

    result = None
    while result is None:
        try:
            msg = result_queue.get(timeout=0.5)
            if msg.get("type") == MSG_LOG:
                _append_log(msg["message"], msg.get("level", "INFO"))
            elif msg.get("type") == MSG_COMPLETE:
                result = msg
        except Exception:
            if not process.is_alive():
                try:
                    result = result_queue.get(timeout=1)
                except Exception:
                    result = {
                        "stats": None,
                        "error": "Retention worker exited without result",
                    }
                break

    process.join()

    if result["error"]:
        logger.error("Retention cleanup failed: %s", result["error"])
        _append_log(f"Retention cleanup failed: {result['error']}", "ERROR")
    else:
        deleted = result.get("stats", {}).get("deleted", 0)
        _append_log(f"Retention cleanup complete — deleted {deleted} file(s).", "INFO")
    with _state_lock:
        _state["_archive_cache_dirty"] = True


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
    interval_minutes = max(
        1,
        config["schedule"].get("interval_minutes", DEFAULT_INTERVAL_MINUTES),
    )

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        _job_wrapper,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="archive",
        name="AviationWX Archiver",
        replace_existing=True,
    )

    # Add daily retention job when retention is configured
    retention_days = config.get("archive", {}).get("retention_days", 0)
    retention_max_gb = config.get("archive", {}).get("retention_max_gb", 0)
    if isinstance(retention_max_gb, str):
        from app.constants import parse_storage_gb

        retention_max_gb = parse_storage_gb(retention_max_gb)
    if retention_days > 0 or (retention_max_gb and retention_max_gb > 0):
        retention_hour = config.get("schedule", {}).get("retention_hour", 3)
        retention_minute = config.get("schedule", {}).get("retention_minute", 0)
        scheduler.add_job(
            lambda: _retention_job(config_getter()),
            trigger=CronTrigger(hour=retention_hour, minute=retention_minute),
            id="retention",
            name="Retention Cleanup",
            replace_existing=True,
        )
        logger.info(
            "Retention job scheduled daily at %02d:%02d UTC.",
            retention_hour,
            retention_minute,
        )
        t = f"{retention_hour:02d}:{retention_minute:02d} UTC"
        _append_log(f"Retention cleanup daily at {t}.", "INFO")

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
        logger.debug("Trigger skipped: config validation failed.")
        return False
    with _state_lock:
        if _state["running"]:
            logger.debug("Trigger skipped: archive run already in progress.")
            return False
    logger.debug("Manual archive run triggered.")
    threading.Thread(target=_archive_job, args=[config], daemon=True).start()
    return True
