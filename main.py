"""
AviationWX.org Archiver — application entry point.

Starts the background scheduler and optionally the Flask web GUI.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import sys
import time

from app.config import check_host_resources, load_config
from app.ntp import check_ntp_time
from app.scheduler import start_scheduler
from app.web import app


def setup_logging(config: dict) -> None:
    level_str = config.get("logging", {}).get("level", "INFO").upper()
    level = getattr(logging, level_str, logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_file = config.get("logging", {}).get("file", "")
    if log_file:
        try:
            log_dir = os.path.dirname(log_file)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            handlers.append(logging.FileHandler(log_file))
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Could not open log file %s: %s. Logging to stdout only.",
                log_file,
                exc,
            )

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        handlers=handlers,
    )


def main() -> None:
    config = load_config()
    setup_logging(config)

    logger = logging.getLogger(__name__)
    logger.info("AviationWX.org Archiver starting up.")

    check_host_resources(config)
    check_ntp_time()

    # Scheduler uses getter so web UI config changes take effect on next run
    app.config["ARCHIVER_CONFIG"] = config
    scheduler = start_scheduler(lambda: app.config["ARCHIVER_CONFIG"])

    web_enabled = config["web"].get("enabled", True)

    try:
        if web_enabled:
            host = config["web"]["host"]
            port = int(config["web"]["port"])
            host_display = host if host != "0.0.0.0" else "localhost"
            logger.info("Web GUI available at http://%s:%d", host_display, port)
            app.run(host=host, port=port, debug=False, use_reloader=False)
        else:
            logger.info("Web UI disabled; running scheduler only.")
            while True:
                time.sleep(3600)
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Archiver shut down.")


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn", force=True)
    main()
