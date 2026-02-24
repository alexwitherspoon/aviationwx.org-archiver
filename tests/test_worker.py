"""
Tests for app.worker — archive worker process.
"""

import copy
import multiprocessing
from unittest.mock import patch

from app.config import DEFAULT_CONFIG
from app.worker import run_archive_worker, run_retention_worker


def test_run_archive_worker_puts_stats_on_queue():
    """Worker runs run_archive and puts stats on result queue."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]
    queue = multiprocessing.Queue()

    with patch("app.worker.run_archive") as mock_run:
        mock_run.return_value = {
            "airports_processed": 1,
            "images_fetched": 2,
            "images_saved": 2,
            "errors": 0,
        }
        run_archive_worker(config, queue)

    result = queue.get(timeout=1)
    assert result["error"] is None
    assert result["stats"]["airports_processed"] == 1
    assert result["stats"]["images_saved"] == 2


def test_run_archive_worker_puts_error_on_queue_when_validation_fails():
    """Worker puts error on queue when config validation fails."""
    config = {
        "archive": {"output_dir": ""},
        "airports": {"archive_all": False, "selected": []},
    }
    queue = multiprocessing.Queue()

    run_archive_worker(config, queue)

    result = queue.get(timeout=1)
    assert result["stats"] is None
    assert "output" in result["error"].lower() or "directory" in result["error"].lower()


def test_run_archive_worker_sets_nice_when_configured():
    """Worker calls os.nice when schedule.worker_nice > 0."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]
    config["schedule"]["worker_nice"] = 10
    queue = multiprocessing.Queue()

    with patch("app.worker.run_archive") as mock_run:
        mock_run.return_value = {
            "airports_processed": 1,
            "images_fetched": 0,
            "images_saved": 0,
            "errors": 0,
        }
        with patch("app.worker.os.nice") as mock_nice:
            run_archive_worker(config, queue)

    mock_nice.assert_called_once_with(10)


def test_run_archive_worker_skips_nice_when_zero():
    """Worker does not call os.nice when worker_nice is 0."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]
    config["schedule"]["worker_nice"] = 0
    queue = multiprocessing.Queue()

    with patch("app.worker.run_archive") as mock_run:
        mock_run.return_value = {
            "airports_processed": 1,
            "images_fetched": 0,
            "images_saved": 0,
            "errors": 0,
        }
        with patch("app.worker.os.nice") as mock_nice:
            run_archive_worker(config, queue)

    mock_nice.assert_not_called()


def test_run_archive_worker_puts_error_on_queue_when_run_raises():
    """Worker puts error on queue when run_archive raises."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]
    queue = multiprocessing.Queue()

    with patch("app.worker.run_archive", side_effect=RuntimeError("test failure")):
        run_archive_worker(config, queue)

    result = queue.get(timeout=1)
    assert result["stats"] is None
    assert "test failure" in result["error"]


def test_run_retention_worker_puts_deleted_count_on_queue():
    """Retention worker runs apply_retention and puts deleted count on queue."""
    import tempfile

    config = copy.deepcopy(DEFAULT_CONFIG)
    with tempfile.TemporaryDirectory() as tmpdir:
        config["archive"]["output_dir"] = tmpdir
        config["archive"]["retention_days"] = 7
        queue = multiprocessing.Queue()

        with patch("app.worker.apply_retention", return_value=42):
            run_retention_worker(config, queue)

        result = queue.get(timeout=1)
        assert result["error"] is None
        assert result["stats"]["deleted"] == 42


def test_run_retention_worker_puts_error_on_queue_when_raises():
    """Retention worker puts error on queue when apply_retention raises."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["archive"]["output_dir"] = "/tmp"
    config["archive"]["retention_days"] = 7
    queue = multiprocessing.Queue()

    with patch(
        "app.worker.apply_retention", side_effect=RuntimeError("retention failed")
    ):
        run_retention_worker(config, queue)

    result = queue.get(timeout=1)
    assert result["stats"] is None
    assert "retention failed" in result["error"]
