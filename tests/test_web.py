"""
Tests for app.web — Flask routes, helpers, and form handling.
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from app.config import DEFAULT_CONFIG
from app.version import GIT_SHA, VERSION
from app.web import (
    _archive_stats,
    _archive_tree,
    _disk_usage,
    _form_to_config,
)
from app.web import (
    app as flask_app,
)

# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


def test_version_module_provides_version_and_sha():
    """VERSION is a non-empty string; GIT_SHA is a string (may be empty)."""
    assert isinstance(VERSION, str)
    assert len(VERSION) > 0
    assert isinstance(GIT_SHA, str)


def test_archive_tree_returns_empty_when_output_dir_missing():
    """_archive_tree returns empty dict when output_dir does not exist."""
    result = _archive_tree("/nonexistent/path/12345")
    assert result == {}


def test_archive_tree_builds_nested_structure_from_directory():
    """_archive_tree builds year/month/day/airport structure from archive layout."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path_2024_06_15_kspb = os.path.join(tmpdir, "2024", "06", "15", "KSPB")
        os.makedirs(path_2024_06_15_kspb, exist_ok=True)
        with open(os.path.join(path_2024_06_15_kspb, "image.jpg"), "wb") as fh:
            fh.write(b"data")

        tree = _archive_tree(tmpdir)

    assert "2024" in tree
    assert "06" in tree["2024"]
    assert "15" in tree["2024"]["06"]
    assert "KSPB" in tree["2024"]["06"]["15"]
    assert "image.jpg" in tree["2024"]["06"]["15"]["KSPB"]


def test_archive_tree_ignores_non_digit_directories():
    """_archive_tree skips month/day directories that are not digits."""
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "2024", "xx", "15", "KSPB"), exist_ok=True)
        os.makedirs(os.path.join(tmpdir, "abcd", "06", "15", "KSPB"), exist_ok=True)

        tree = _archive_tree(tmpdir)

    assert "abcd" not in tree
    assert "2024" in tree
    assert tree["2024"] == {}


def test_disk_usage_returns_dict_when_path_valid():
    """_disk_usage returns used_gb, total_gb, free_gb, percent_used when path exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _disk_usage(tmpdir)

    assert result is not None
    assert "used_gb" in result
    assert "total_gb" in result
    assert "free_gb" in result
    assert "percent_used" in result
    assert isinstance(result["used_gb"], (int, float))
    assert isinstance(result["total_gb"], (int, float))


def test_disk_usage_returns_none_on_oserror():
    """_disk_usage returns None when disk_usage raises OSError."""
    with patch("app.web.shutil.disk_usage", side_effect=OSError(2, "No such file")):
        result = _disk_usage("/nonexistent")
    assert result is None


def test_archive_stats_returns_empty_when_output_dir_missing():
    """_archive_stats returns zeros when output_dir does not exist."""
    result = _archive_stats("/nonexistent/path")
    assert result["total_files"] == 0
    assert result["total_size_mb"] == 0.0
    assert result["airports"] == []
    assert "disk_usage" in result


def test_archive_stats_counts_files_and_airports():
    """_archive_stats counts total files, size, and unique airports."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path1 = os.path.join(tmpdir, "2024", "06", "15", "KSPB")
        path2 = os.path.join(tmpdir, "2024", "06", "15", "KAWO")
        os.makedirs(path1, exist_ok=True)
        os.makedirs(path2, exist_ok=True)
        with open(os.path.join(path1, "a.jpg"), "wb") as fh:
            fh.write(b"x" * (1024 * 1024))
        with open(os.path.join(path2, "b.jpg"), "wb") as fh:
            fh.write(b"y" * (512 * 1024))

        stats = _archive_stats(tmpdir)

    assert stats["total_files"] == 2
    assert stats["total_size_mb"] >= 1.0
    assert set(stats["airports"]) == {"KSPB", "KAWO"}


def test_archive_stats_handles_getsize_oserror():
    """_archive_stats continues when os.path.getsize raises OSError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "2024", "06", "15", "KSPB")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "a.jpg"), "wb") as fh:
            fh.write(b"data")

        with patch(
            "app.web.os.path.getsize",
            side_effect=OSError(13, "Permission denied"),
        ):
            stats = _archive_stats(tmpdir)

    assert stats["total_files"] == 1
    assert stats["total_size_mb"] == 0.0


# ---------------------------------------------------------------------------
# Form helper tests
# ---------------------------------------------------------------------------


def test_form_to_config_raises_when_interval_less_than_one():
    """_form_to_config raises ValueError when interval_minutes < 1."""
    form = MagicMock()
    form.get.side_effect = lambda k, d=None: {
        "interval_minutes": "0",
        "output_dir": "/archive",
        "retention_days": "0",
        "selected_airports": "KSPB",
        "log_level": "INFO",
    }.get(k, d)

    with pytest.raises(ValueError, match="interval_minutes"):
        _form_to_config(form, DEFAULT_CONFIG)


def test_form_to_config_raises_when_output_dir_empty():
    """_form_to_config raises ValueError when output_dir is empty."""
    form = MagicMock()
    form.get.side_effect = lambda k, d=None: {
        "interval_minutes": "15",
        "output_dir": "",
        "retention_days": "0",
        "selected_airports": "KSPB",
        "log_level": "INFO",
    }.get(k, d)
    form.__contains__ = lambda self, k: k in ["fetch_on_start"]

    with pytest.raises(ValueError, match="output_dir"):
        _form_to_config(form, DEFAULT_CONFIG)


def test_form_to_config_raises_when_retention_negative():
    """_form_to_config raises ValueError when retention_days < 0."""
    form = MagicMock()
    form.get.side_effect = lambda k, d=None: {
        "interval_minutes": "15",
        "output_dir": "/archive",
        "retention_days": "-1",
        "selected_airports": "KSPB",
        "log_level": "INFO",
    }.get(k, d)
    form.__contains__ = lambda self, k: k in ["fetch_on_start"]

    with pytest.raises(ValueError, match="retention_days"):
        _form_to_config(form, DEFAULT_CONFIG)


def test_form_to_config_raises_when_log_level_invalid():
    """_form_to_config raises ValueError when log_level is invalid."""
    form = MagicMock()
    form.get.side_effect = lambda k, d=None: {
        "interval_minutes": "15",
        "output_dir": "/archive",
        "retention_days": "0",
        "selected_airports": "KSPB",
        "log_level": "INVALID",
    }.get(k, d)
    form.__contains__ = lambda self, k: k in ["fetch_on_start"]

    with pytest.raises(ValueError, match="Invalid log level"):
        _form_to_config(form, DEFAULT_CONFIG)


def test_form_to_config_sets_api_key_when_provided():
    """_form_to_config sets source.api_key when form provides non-empty value."""
    form = MagicMock()
    form.get.side_effect = lambda k, d=None: {
        "interval_minutes": "15",
        "output_dir": "/archive",
        "retention_days": "0",
        "selected_airports": "KSPB",
        "log_level": "INFO",
        "base_url": "https://aviationwx.org",
        "api_key": "test-key-123",
    }.get(k, d)
    form.__contains__ = lambda self, k: k in ["fetch_on_start"]

    config = _form_to_config(form, DEFAULT_CONFIG)
    assert config["source"]["api_key"] == "test-key-123"


# ---------------------------------------------------------------------------
# Route tests (require Flask app context)
# ---------------------------------------------------------------------------


@pytest.fixture
def flask_client():
    """Create a Flask test client with a minimal valid config."""
    import copy

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]
    flask_app.config["ARCHIVER_CONFIG"] = config
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as client:
        yield client


def test_configuration_post_saves_valid_config(flask_client):
    """POST to /config with valid data saves and redirects with success message."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = os.path.join(tmpdir, "config.yaml")
        with patch.dict("os.environ", {"ARCHIVER_CONFIG": config_path}):
            resp = flask_client.post(
                "/config",
                data={
                    "interval_minutes": "20",
                    "output_dir": "/archive",
                    "retention_days": "7",
                    "selected_airports": "KSPB\nKAWO",
                    "log_level": "INFO",
                },
                follow_redirects=False,
            )

    assert resp.status_code == 200
    assert b"Configuration saved" in resp.data or b"success" in resp.data.lower()


def test_configuration_post_shows_error_on_save_failure(flask_client):
    """POST to /config shows error when save_config returns False."""
    with patch("app.web.save_config", return_value=False):
        resp = flask_client.post(
            "/config",
            data={
                "interval_minutes": "15",
                "output_dir": "/archive",
                "retention_days": "0",
                "selected_airports": "KSPB",
                "log_level": "INFO",
            },
        )

    assert resp.status_code == 200
    assert b"Failed to save" in resp.data or b"error" in resp.data.lower()


def test_configuration_post_shows_error_on_validation_failure(flask_client):
    """POST to /config shows error when form validation raises ValueError."""
    resp = flask_client.post(
        "/config",
        data={
            "interval_minutes": "0",
            "output_dir": "/archive",
            "retention_days": "0",
            "selected_airports": "KSPB",
            "log_level": "INFO",
        },
    )

    assert resp.status_code == 200
    assert b"Invalid" in resp.data or b"error" in resp.data.lower()


def test_trigger_archive_redirects_to_config_when_invalid(flask_client):
    """POST to /run redirects to config page when config is invalid."""
    config = {
        "archive": {"output_dir": "/archive"},
        "airports": {"archive_all": False, "selected": []},
    }
    flask_app.config["ARCHIVER_CONFIG"] = config

    resp = flask_client.post("/run", follow_redirects=False)

    assert resp.status_code == 302
    assert "config" in resp.headers.get("Location", "")


def test_browse_includes_tree_and_output_dir(flask_client):
    """GET /browse returns 200 with tree and output_dir in template context."""
    resp = flask_client.get("/browse")
    assert resp.status_code == 200
    assert b"Browse" in resp.data or b"browse" in resp.data.lower()


def test_api_status_includes_version_and_git_sha(flask_client):
    """GET /api/status includes version and git_sha in response."""
    resp = flask_client.get("/api/status")
    data = resp.get_json()
    assert "version" in data
    assert data["version"]  # Non-empty version string
    assert "git_sha" in data


def test_dashboard_footer_shows_version(flask_client):
    """Dashboard page includes version in footer."""
    resp = flask_client.get("/")
    assert resp.status_code == 200
    assert b"v0." in resp.data or b"0.2" in resp.data


def test_api_status_includes_disk_usage_when_available(flask_client):
    """GET /api/status includes disk_usage when output_dir exists."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    with tempfile.TemporaryDirectory() as tmpdir:
        config["archive"]["output_dir"] = tmpdir
        flask_app.config["ARCHIVER_CONFIG"] = config

        resp = flask_client.get("/api/status")

    data = resp.get_json()
    assert data["status"] == "ok"
    assert "disk_usage" in data
    assert data["disk_usage"] is not None
