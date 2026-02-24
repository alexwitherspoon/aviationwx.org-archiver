"""
AviationWX.org Archiver - Archive worker process.

Runs archive jobs in a separate process to avoid GIL contention with the web UI.
Sends log messages and final result to the main process via a queue.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import time

from app.archiver import apply_retention, run_archive
from app.config import validate_config
from app.constants import DEFAULT_INTERVAL_MINUTES

# Message types for worker -> main process communication
MSG_LOG = "log"
MSG_COMPLETE = "complete"


class _QueueLogHandler(logging.Handler):
    """Forwards log records to a multiprocessing queue for the main process."""

    def __init__(self, queue: multiprocessing.Queue) -> None:
        super().__init__()
        self._queue = queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._queue.put(
                {"type": MSG_LOG, "message": msg, "level": record.levelname}
            )
        except Exception:
            pass


def run_archive_worker(config: dict, result_queue: multiprocessing.Queue) -> None:
    """
    Entry point for the archive worker process.

    Sends log messages and final result to result_queue.
    Messages: {"type": "log", "message": str, "level": str}
              {"type": "complete", "stats": dict|None, "error": str|None}
    """
    # Worker logging: stdout for Docker + queue for main process web UI
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        force=True,
    )
    logger = logging.getLogger(__name__)

    # Forward archiver logs to main process for web UI
    queue_handler = _QueueLogHandler(result_queue)
    queue_handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("app.archiver").addHandler(queue_handler)

    # Lower worker CPU priority on Unix (higher nice = lower priority)
    worker_nice = config.get("schedule", {}).get("worker_nice", 0)
    if worker_nice > 0:
        try:
            os.nice(worker_nice)
            logger.debug("Worker nice set to +%d", worker_nice)
        except (AttributeError, OSError):
            pass  # Unsupported (e.g. Windows) or permission denied

    try:
        errors = validate_config(config)
        if errors:
            result_queue.put(
                {"type": MSG_COMPLETE, "stats": None, "error": "; ".join(errors)}
            )
            return

        interval_minutes = max(
            1,
            config["schedule"].get("interval_minutes", DEFAULT_INTERVAL_MINUTES),
        )
        run_limit_seconds = interval_minutes * 0.9 * 60
        deadline = time.time() + run_limit_seconds

        stats = run_archive(config, deadline=deadline)
        result_queue.put({"type": MSG_COMPLETE, "stats": stats, "error": None})
    except Exception as exc:
        logger.exception("Archive worker failed")
        result_queue.put({"type": MSG_COMPLETE, "stats": None, "error": str(exc)})


def run_retention_worker(config: dict, result_queue: multiprocessing.Queue) -> None:
    """
    Entry point for the retention worker process.

    Runs apply_retention in a separate process. Sends log messages and
    final result (deleted count) to result_queue.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        force=True,
    )
    logger = logging.getLogger(__name__)

    queue_handler = _QueueLogHandler(result_queue)
    queue_handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("app.archiver").addHandler(queue_handler)

    worker_nice = config.get("schedule", {}).get("worker_nice", 0)
    if worker_nice > 0:
        try:
            os.nice(worker_nice)
            logger.debug("Retention worker nice set to +%d", worker_nice)
        except (AttributeError, OSError):
            pass

    try:
        deleted = apply_retention(config)
        result_queue.put(
            {"type": MSG_COMPLETE, "stats": {"deleted": deleted}, "error": None}
        )
    except Exception as exc:
        logger.exception("Retention worker failed")
        result_queue.put({"type": MSG_COMPLETE, "stats": None, "error": str(exc)})
