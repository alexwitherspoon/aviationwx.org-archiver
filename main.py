"""
AviationWX.org Archiver — application entry point.

Starts the background scheduler and the Flask web GUI.
"""

from __future__ import annotations

import logging
import os
import sys

from app.config import load_config
from app.scheduler import start_scheduler
from app.web import app


def setup_logging(config: dict) -> None:
    level_str = config.get("logging", {}).get("level", "INFO").upper()
    level = getattr(logging, level_str, logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_file = config.get("logging", {}).get("file", "")
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.append(logging.FileHandler(log_file))

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

    # Scheduler uses getter so web UI config changes take effect on next run
    app.config["ARCHIVER_CONFIG"] = config
    scheduler = start_scheduler(lambda: app.config["ARCHIVER_CONFIG"])

    host = config["web"]["host"]
    port = int(config["web"]["port"])

    host_display = host if host != "0.0.0.0" else "localhost"
    logger.info("Web GUI available at http://%s:%d", host_display, port)

    try:
        app.run(host=host, port=port, debug=False, use_reloader=False)
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Archiver shut down.")


if __name__ == "__main__":
    main()
