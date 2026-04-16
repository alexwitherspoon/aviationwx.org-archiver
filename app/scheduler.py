"""
AviationWX.org Archiver - Background scheduler.

Runs archive passes on a configurable interval using APScheduler.
Archive jobs run in a separate process to avoid GIL contention with the web UI.
"""

import logging
import multiprocessing
import threading
import time
from collections import deque
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import validate_config
from app.constants import DEFAULT_INTERVAL_MINUTES, DEFAULT_MAX_LOG_ENTRIES
from app.worker import MSG_COMPLETE, MSG_LOG, run_archive_worker, run_retention_worker

logger = logging.getLogger(__name__)

# Shared in-memory state (written by scheduler thread, read by web thread)
_state_lock = threading.Lock()
_scheduler_instance = None  # Set in start_scheduler; used to refresh next_run
_state = {
    "last_run": None,  # datetime | None
    "last_stats": None,  # dict | None
    "next_run": None,  # datetime | None
    "running": False,  # bool — True while a run is in progress
    "_running_since": None,  # float | None — time.time() when run started
    "_running_job": None,  # "archive" | "retention" | None — which job holds the lock
    "run_count": 0,  # int — total number of completed runs
    "log_entries": deque(maxlen=DEFAULT_MAX_LOG_ENTRIES),  # recent logs for web GUI
    "_stats_cache_dirty": False,  # web stats cache invalidation
    "_tree_cache_dirty": False,  # browse tree cache invalidation
}


def get_state() -> dict:
    """Return a copy of the current scheduler state."""
    with _state_lock:
        out = {k: v for k, v in _state.items() if not k.startswith("_")}
        if "log_entries" in out:
            out["log_entries"] = list(out["log_entries"])
        return out


def consume_archive_cache_dirty_flags() -> tuple[bool, bool]:
    """
    Read and clear stats/tree cache dirty flags.

    Returns:
        (stats_dirty, tree_dirty) — True if that cache should be invalidated.

    Web calls this before serving stats/tree. Avoids scheduler importing app.web.
    """
    with _state_lock:
        stats = _state.pop("_stats_cache_dirty", False)
        tree = _state.pop("_tree_cache_dirty", False)
        return (stats, tree)


def _append_log(message: str, level: str = "INFO") -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
    }
    with _state_lock:
        _state["log_entries"].append(entry)


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


def _update_next_run() -> None:
    """Refresh next_run from the archive job's next scheduled time."""
    global _scheduler_instance
    sched = _scheduler_instance
    if sched is None:
        return
    job = sched.get_job("archive")
    if job and job.next_run_time:
        with _state_lock:
            _state["next_run"] = job.next_run_time


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
        manual_trigger = _state.pop("_manual_trigger", False)
        if _state["running"] and not manual_trigger:
            running_job = _state.get("_running_job")
            running_since = _state.get("_running_since")
            elapsed = time.time() - running_since if running_since else 0
            # Only override if stuck job was archive; retention can run for hours
            if (
                running_job == "archive"
                and running_since is not None
                and elapsed > stale_threshold_seconds
            ):
                logger.warning(
                    "Previous archive run appears stuck (%.0f min) — clearing lock.",
                    elapsed / 60,
                )
                _append_log(
                    "Previous run appears stuck — clearing lock and starting new run.",
                    "WARNING",
                )
                _state["running"] = False
                _state["_running_since"] = None
                _state["_running_job"] = None
            else:
                logger.warning("Archive run skipped — previous run still in progress.")
                return
        if not manual_trigger:
            _state["running"] = True
            _state["_running_since"] = time.time()
            _state["_running_job"] = "archive"

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
            if "exited without result" in str(result["error"]):
                _rebuild_index_on_worker_crash(config)
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
            _state["_running_job"] = None
            _state["_stats_cache_dirty"] = True
            _state["_tree_cache_dirty"] = True
        _update_next_run()


def _rebuild_index_on_worker_crash(config: dict) -> None:
    """Rebuild archive index when worker dies before flushing its batch."""
    from app.archiver import _rebuild_archive_index

    output_dir = (config.get("archive") or {}).get("output_dir")
    if output_dir:
        try:
            _rebuild_archive_index(output_dir, config)
            logger.info("Rebuilt archive index after worker crash.")
        except Exception as exc:
            logger.warning("Failed to rebuild index after worker crash: %s", exc)


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

    with _state_lock:
        if _state["running"]:
            logger.debug("Retention job skipped — another run in progress.")
            _append_log("Retention skipped — another run in progress.", "INFO")
            return
        _state["running"] = True
        _state["_running_since"] = time.time()
        _state["_running_job"] = "retention"

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

    try:
        if result["error"]:
            logger.error("Retention cleanup failed: %s", result["error"])
            _append_log(f"Retention cleanup failed: {result['error']}", "ERROR")
            if "exited without result" in str(result["error"]):
                _rebuild_index_on_worker_crash(config)
            with _state_lock:
                _state["_stats_cache_dirty"] = True
                _state["_tree_cache_dirty"] = True
        else:
            deleted = result.get("stats", {}).get("deleted", 0)
            _append_log(
                f"Retention cleanup complete — deleted {deleted} file(s).", "INFO"
            )
            with _state_lock:
                _state["_stats_cache_dirty"] = True
                if deleted > 0:
                    _state["_tree_cache_dirty"] = True
    finally:
        with _state_lock:
            _state["running"] = False
            _state["_running_since"] = None
            _state["_running_job"] = None
    _update_next_run()


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

    global _scheduler_instance
    scheduler = BackgroundScheduler(daemon=True)
    _scheduler_instance = scheduler
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
        delay = max(0, int(config["schedule"].get("fetch_on_start_delay_seconds", 0)))

        def _delayed_start() -> None:
            if delay:
                time.sleep(delay)
            _job_wrapper()

        threading.Thread(target=_delayed_start, daemon=True).start()

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
        _state["running"] = True
        _state["_running_since"] = time.time()
        _state["_running_job"] = "archive"
        _state["_manual_trigger"] = True
    logger.debug("Manual archive run triggered.")
    threading.Thread(target=_archive_job, args=[config], daemon=True).start()
    return True
