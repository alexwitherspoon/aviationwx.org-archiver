"""
Tests for app.scheduler — background job scheduling and trigger.
"""

import copy
import logging
import time
from unittest.mock import MagicMock, patch

from app.config import DEFAULT_CONFIG
from app.scheduler import get_state, trigger_run


def _mock_process_to_run_synchronously():
    """Patch Process so start() runs the target in-process (for tests)."""

    def create_process(target=None, args=None, **kwargs):
        m = MagicMock()

        def start():
            if target and args is not None:
                target(*args)

        m.start = start
        m.join = MagicMock()
        m.is_alive = MagicMock(return_value=False)  # After sync run, target done
        return m

    return patch("app.scheduler.multiprocessing.Process", side_effect=create_process)


def test_get_state_returns_dict_with_expected_keys():
    """get_state returns dict with expected scheduler state keys."""
    state = get_state()
    assert isinstance(state, dict)
    assert "last_run" in state
    assert "next_run" in state
    assert "running" in state
    assert "run_count" in state
    assert "log_entries" in state
    assert isinstance(state["log_entries"], list)


def test_get_state_excludes_internal_keys():
    """get_state does not expose internal scheduler keys."""
    state = get_state()
    assert "_stats_cache_dirty" not in state
    assert not any(k.startswith("_") for k in state)


def test_trigger_run_returns_false_when_config_invalid():
    """trigger_run returns False when config validation fails."""
    config = {
        "archive": {"output_dir": "/archive"},
        "airports": {"archive_all": False, "selected": []},
    }
    result = trigger_run(config)
    assert result is False


def test_trigger_run_returns_false_when_already_running():
    """trigger_run returns False when an archive run is already in progress."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]

    with patch("app.scheduler._state", {"running": True, "log_entries": []}):
        with patch("app.scheduler._state_lock"):
            result = trigger_run(config)

    assert result is False


def test_trigger_run_starts_job_when_config_valid():
    """trigger_run returns True and starts background job when config is valid."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]

    with patch("app.scheduler._state_lock"):
        with patch("app.scheduler._state", {"running": False, "log_entries": []}):
            with patch("app.scheduler._archive_job"):
                with patch("app.scheduler.threading.Thread") as mock_thread:
                    result = trigger_run(config)

    assert result is True
    mock_thread.return_value.start.assert_called_once()


def test_archive_job_skips_when_config_has_errors():
    """_archive_job logs errors and returns without running when config invalid."""
    from app.scheduler import _archive_job

    config = {
        "archive": {"output_dir": ""},
        "airports": {"archive_all": False, "selected": []},
    }

    with patch("app.scheduler.logger"):
        with patch("app.scheduler._append_log") as mock_append:
            _archive_job(config)

    mock_append.assert_called()
    calls = mock_append.call_args_list
    assert any("error" in str(c).lower() or "config" in str(c).lower() for c in calls)


def test_archive_job_skips_when_already_running():
    """_archive_job skips when a run is already in progress."""
    from app.scheduler import _archive_job

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]

    with patch("app.scheduler._state_lock"):
        with patch("app.scheduler._state", {"running": True}):
            with patch("app.worker.run_archive") as mock_run:
                _archive_job(config)

    mock_run.assert_not_called()


def test_archive_job_forwards_worker_logs_to_append_log():
    """Worker log messages are received and passed to _append_log."""
    from app.scheduler import _archive_job

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]
    stats = {
        "airports_processed": 1,
        "images_fetched": 0,
        "images_saved": 0,
        "errors": 0,
    }

    def fake_run(*a, **kw):
        logging.getLogger("app.archiver").info("test log from worker")
        return stats

    with _mock_process_to_run_synchronously():
        with patch("app.scheduler._state_lock"):
            with patch("app.scheduler._state", {"running": False, "run_count": 0}):
                with patch("app.scheduler._append_log") as mock_append:
                    with patch("app.worker.run_archive", side_effect=fake_run):
                        _archive_job(config)

    log_calls = [c for c in mock_append.call_args_list if "test log" in str(c)]
    assert len(log_calls) >= 1


def test_archive_job_calls_run_archive_when_config_valid():
    """_archive_job runs worker and updates state on success."""
    from app.scheduler import _archive_job

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]

    mock_stats = {
        "airports_processed": 1,
        "images_fetched": 2,
        "images_saved": 2,
        "errors": 0,
    }

    state_ref = {
        "last_run": None,
        "last_stats": None,
        "next_run": None,
        "running": False,
        "run_count": 0,
        "log_entries": [],
    }

    with _mock_process_to_run_synchronously():
        with patch("app.scheduler._state_lock"):
            with patch("app.scheduler._state", state_ref):
                with patch(
                    "app.worker.run_archive", return_value=mock_stats
                ) as mock_run:
                    with patch("app.scheduler._append_log"):
                        _archive_job(config)

    assert state_ref["last_run"] is not None
    assert state_ref["last_stats"] == mock_stats
    assert state_ref["run_count"] == 1
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args[1]
    assert "deadline" in call_kwargs
    deadline = call_kwargs["deadline"]
    assert deadline > time.time()


def test_archive_job_passes_deadline_at_90_percent_of_interval():
    """_archive_job passes deadline = now + 90% of interval_minutes."""
    from app.scheduler import _archive_job

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]
    config["schedule"]["interval_minutes"] = 15

    state_ref = {
        "last_run": None,
        "last_stats": None,
        "next_run": None,
        "running": False,
        "run_count": 0,
        "log_entries": [],
    }

    with _mock_process_to_run_synchronously():
        with patch("app.scheduler._state_lock"):
            with patch("app.scheduler._state", state_ref):
                with patch("app.worker.run_archive") as mock_run:
                    with patch("app.scheduler._append_log"):
                        before = time.time()
                        _archive_job(config)

    mock_run.assert_called_once()
    deadline = mock_run.call_args[1]["deadline"]
    # 90% of 15 min = 13.5 min = 810 seconds
    run_limit = 15 * 0.9 * 60  # 810
    assert run_limit - 2 <= (deadline - before) <= run_limit + 2


def test_start_scheduler_adds_job_and_runs_fetch_on_start():
    """start_scheduler creates job and runs initial pass when fetch_on_start is True."""
    from app.scheduler import start_scheduler

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]
    config["schedule"]["fetch_on_start"] = True

    def config_getter():
        return config

    with _mock_process_to_run_synchronously():
        with patch("app.worker.run_archive"):
            with patch("app.scheduler.threading.Thread") as mock_thread:
                scheduler = start_scheduler(config_getter)

    assert scheduler is not None
    job = scheduler.get_job("archive")
    assert job is not None
    mock_thread.return_value.start.assert_called_once()


def test_start_scheduler_skips_initial_run_when_fetch_on_start_false():
    """start_scheduler does not run initial pass when fetch_on_start is False."""
    from app.scheduler import start_scheduler

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]
    config["schedule"]["fetch_on_start"] = False

    def config_getter():
        return config

    with _mock_process_to_run_synchronously():
        with patch("app.worker.run_archive"):
            with patch("app.scheduler.threading.Thread") as mock_thread:
                start_scheduler(config_getter)

    mock_thread.return_value.start.assert_not_called()


def test_archive_job_clears_stale_lock_and_proceeds():
    """_archive_job clears running lock when stuck longer than 2x interval."""
    from app.scheduler import _archive_job

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]
    config["schedule"]["interval_minutes"] = 15  # 2x = 30 min stale threshold

    state_ref = {
        "last_run": None,
        "last_stats": None,
        "next_run": None,
        "running": True,  # Stuck from previous run
        "_running_since": time.time() - (45 * 60),  # 45 min ago — past 30 min threshold
        "_running_job": "archive",  # Stale override only applies to archive
        "run_count": 0,
        "log_entries": [],
    }

    with _mock_process_to_run_synchronously():
        with patch("app.scheduler._state_lock"):
            with patch("app.scheduler._state", state_ref):
                with patch("app.worker.run_archive") as mock_run:
                    with patch("app.scheduler._append_log"):
                        _archive_job(config)

    mock_run.assert_called_once()
    assert state_ref["running"] is False
    assert state_ref["_running_since"] is None


def test_archive_job_sets_running_false_on_exception():
    """_archive_job sets running=False when worker returns error."""
    from app.scheduler import _archive_job

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]

    state_ref = {
        "last_run": None,
        "last_stats": None,
        "next_run": None,
        "running": False,
        "run_count": 0,
        "log_entries": [],
    }

    with _mock_process_to_run_synchronously():
        with patch("app.scheduler._state_lock"):
            with patch("app.scheduler._state", state_ref):
                with patch(
                    "app.worker.run_archive",
                    side_effect=RuntimeError("test error"),
                ):
                    with patch("app.scheduler._append_log"):
                        with patch("app.scheduler.logger"):
                            _archive_job(config)

    assert state_ref["running"] is False


def test_archive_job_skips_when_retention_running():
    """_archive_job skips when retention run is in progress (no stale override)."""
    from app.scheduler import _archive_job

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]
    config["schedule"]["interval_minutes"] = 15

    # Retention running for 45 min — archive should skip (retention can run for hours)
    state_ref = {
        "last_run": None,
        "running": True,
        "_running_since": time.time() - (45 * 60),
        "_running_job": "retention",
        "run_count": 0,
        "log_entries": [],
    }

    with patch("app.scheduler._state_lock"):
        with patch("app.scheduler._state", state_ref):
            with patch("app.worker.run_archive") as mock_run:
                with patch("app.scheduler._append_log"):
                    _archive_job(config)

    mock_run.assert_not_called()


def test_retention_job_skips_when_archive_running():
    """_retention_job skips when archive run is in progress."""
    from app.scheduler import _retention_job

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["archive"]["output_dir"] = "/tmp/archive"
    config["archive"]["retention_days"] = 7

    with patch("app.scheduler._state_lock"):
        with patch("app.scheduler._state", {"running": True}):
            with patch("app.worker.run_retention_worker") as mock_run:
                with patch("app.scheduler._append_log"):
                    _retention_job(config)

    mock_run.assert_not_called()


def test_archive_job_updates_next_run_after_completion():
    """_archive_job calls _update_next_run so dashboard shows fresh next run."""
    from datetime import datetime, timezone

    from app.scheduler import _archive_job

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]

    next_run_dt = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
    mock_job = MagicMock()
    mock_job.next_run_time = next_run_dt
    mock_scheduler = MagicMock()
    mock_scheduler.get_job.return_value = mock_job

    state_ref = {
        "last_run": None,
        "last_stats": None,
        "next_run": None,
        "running": False,
        "run_count": 0,
        "log_entries": [],
    }

    with _mock_process_to_run_synchronously():
        with patch("app.scheduler._state_lock"):
            with patch("app.scheduler._state", state_ref):
                with patch("app.scheduler._scheduler_instance", mock_scheduler):
                    with patch("app.worker.run_archive") as mock_run:
                        mock_run.return_value = {
                            "airports_processed": 1,
                            "images_fetched": 0,
                            "images_saved": 0,
                            "errors": 0,
                        }
                        with patch("app.scheduler._append_log"):
                            _archive_job(config)

    mock_scheduler.get_job.assert_called_with("archive")
    assert state_ref["next_run"] == next_run_dt


def test_archive_job_rebuilds_index_when_worker_exits_without_result():
    """_archive_job triggers index rebuild when worker dies before completing."""
    from app.scheduler import _archive_job

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]
    config["archive"]["output_dir"] = "/tmp/archive"

    # Process that exits immediately without putting result on queue
    def crash_immediately(target=None, args=None, **kwargs):
        m = MagicMock()
        m.start = MagicMock()
        m.join = MagicMock()
        m.is_alive = MagicMock(return_value=False)
        return m

    state_ref = {
        "last_run": None,
        "running": False,
        "run_count": 0,
        "log_entries": [],
    }

    with patch("app.scheduler.multiprocessing.Process", side_effect=crash_immediately):
        with patch("app.scheduler._state_lock"):
            with patch("app.scheduler._state", state_ref):
                with patch("app.scheduler._append_log"):
                    with patch(
                        "app.scheduler._rebuild_index_on_worker_crash"
                    ) as mock_rebuild:
                        with patch("app.scheduler.logger"):
                            _archive_job(config)

    mock_rebuild.assert_called_once_with(config)


def test_retention_job_rebuilds_index_when_worker_exits_without_result():
    """_retention_job triggers index rebuild when worker dies before completing."""
    import multiprocessing

    from app.scheduler import MSG_COMPLETE, _retention_job

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["archive"]["output_dir"] = "/tmp/archive"
    config["archive"]["retention_days"] = 7
    config["airports"]["selected"] = ["KSPB"]  # Pass validate_config

    # Pre-fill queue so first get() returns crash result
    result_queue = multiprocessing.Queue()
    result_queue.put(
        {
            "type": MSG_COMPLETE,
            "stats": None,
            "error": "Retention worker exited without result",
        }
    )

    def fake_process(target=None, args=None, **kwargs):
        m = MagicMock()
        m.start = MagicMock()
        m.join = MagicMock()
        m.is_alive = MagicMock(return_value=False)
        return m

    # Patch _rebuild_index_on_worker_crash to verify it's invoked (avoids import
    # timing; scheduler does local import when crash path runs)
    with patch("app.scheduler.multiprocessing.Process", side_effect=fake_process):
        with patch("app.scheduler.multiprocessing.Queue", return_value=result_queue):
            with patch("app.scheduler._state_lock"):
                state = {"running": False, "log_entries": []}
                with patch("app.scheduler._state", state):
                    with patch("app.scheduler._append_log"):
                        with patch(
                            "app.scheduler._rebuild_index_on_worker_crash",
                            wraps=lambda cfg: None,
                        ) as mock_rebuild:
                            with patch("app.scheduler.logger"):
                                _retention_job(config)
                            mock_rebuild.assert_called_once_with(config)


def test_append_log_respects_deque_maxlen():
    """_append_log keeps a bounded deque of recent entries."""
    from collections import deque

    from app.scheduler import _append_log, _state, _state_lock

    with _state_lock:
        _state["log_entries"] = deque(maxlen=5)

    for i in range(50):
        _append_log(f"msg-{i}", "INFO")

    with _state_lock:
        assert len(_state["log_entries"]) == 5
        assert _state["log_entries"][-1]["message"] == "msg-49"
