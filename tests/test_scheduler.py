"""
Tests for app.scheduler — background job scheduling and trigger.
"""

import copy
from unittest.mock import patch

from app.config import DEFAULT_CONFIG
from app.scheduler import get_state, trigger_run


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
            with patch("app.scheduler.run_archive") as mock_run:
                _archive_job(config)

    mock_run.assert_not_called()


def test_archive_job_calls_run_archive_when_config_valid():
    """_archive_job calls run_archive and updates state on success."""
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

    with patch("app.scheduler._state_lock"):
        with patch("app.scheduler._state", state_ref):
            with patch("app.scheduler.run_archive", return_value=mock_stats):
                with patch("app.scheduler._append_log"):
                    _archive_job(config)

    assert state_ref["last_run"] is not None
    assert state_ref["last_stats"] == mock_stats
    assert state_ref["run_count"] == 1


def test_start_scheduler_adds_job_and_runs_fetch_on_start():
    """start_scheduler creates job and runs initial pass when fetch_on_start is True."""
    from app.scheduler import start_scheduler

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]
    config["schedule"]["fetch_on_start"] = True

    def config_getter():
        return config

    with patch("app.scheduler.run_archive"):
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

    with patch("app.scheduler.run_archive"):
        with patch("app.scheduler.threading.Thread") as mock_thread:
            start_scheduler(config_getter)

    mock_thread.return_value.start.assert_not_called()


def test_archive_job_sets_running_false_on_exception():
    """_archive_job sets running=False when run_archive raises."""
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

    with patch("app.scheduler._state_lock"):
        with patch("app.scheduler._state", state_ref):
            with patch(
                "app.scheduler.run_archive",
                side_effect=RuntimeError("test error"),
            ):
                with patch("app.scheduler._append_log"):
                    with patch("app.scheduler.logger"):
                        _archive_job(config)

    assert state_ref["running"] is False
