"""
Tests for the AviationWX.org Archiver.
"""

import hashlib
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

def test_load_config_defaults_when_file_missing():
    """Loading config from a non-existent path returns DEFAULT_CONFIG values."""
    from app.config import load_config, DEFAULT_CONFIG

    config = load_config("/nonexistent/path/config.yaml")

    assert config["archive"]["output_dir"] == DEFAULT_CONFIG["archive"]["output_dir"]
    assert config["schedule"]["interval_minutes"] == DEFAULT_CONFIG["schedule"]["interval_minutes"]
    assert config["web"]["port"] == DEFAULT_CONFIG["web"]["port"]


def test_load_config_reads_yaml_file():
    """Config values from a YAML file override defaults."""
    from app.config import load_config

    data = {
        "schedule": {"interval_minutes": 30},
        "airports": {"archive_all": True, "selected": ["KSPB"]},
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
        yaml.safe_dump(data, tmp)
        tmp_path = tmp.name

    try:
        config = load_config(tmp_path)
        assert config["schedule"]["interval_minutes"] == 30
        assert config["airports"]["archive_all"] is True
        assert "KSPB" in config["airports"]["selected"]
        # Unset keys should fall back to defaults
        assert config["web"]["port"] == 8080
    finally:
        os.unlink(tmp_path)


def test_save_config_roundtrip():
    """Saving and re-loading a config produces identical values."""
    from app.config import load_config, save_config, DEFAULT_CONFIG
    import copy

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["schedule"]["interval_minutes"] = 42

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "config.yaml")
        assert save_config(config, path) is True
        loaded = load_config(path)
        assert loaded["schedule"]["interval_minutes"] == 42


def test_load_config_invalid_yaml():
    """A malformed YAML file falls back to defaults without raising."""
    from app.config import load_config, DEFAULT_CONFIG

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
        tmp.write(": : invalid yaml :::")
        tmp_path = tmp.name

    try:
        config = load_config(tmp_path)
        # Should return defaults without crashing
        assert config["web"]["port"] == DEFAULT_CONFIG["web"]["port"]
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Airport selection tests
# ---------------------------------------------------------------------------

def test_select_airports_archive_all():
    """When archive_all is True, all airports are returned."""
    from app.archiver import select_airports

    all_airports = [{"code": "KSPB"}, {"code": "KAWO"}, {"code": "KPWT"}]
    config = {"airports": {"archive_all": True, "selected": []}}

    result = select_airports(all_airports, config)
    assert result == all_airports


def test_select_airports_selected_subset():
    """Only the configured airports are returned when archive_all is False."""
    from app.archiver import select_airports

    all_airports = [{"code": "KSPB"}, {"code": "KAWO"}, {"code": "KPWT"}]
    config = {"airports": {"archive_all": False, "selected": ["KSPB", "KPWT"]}}

    result = select_airports(all_airports, config)
    codes = [a["code"] for a in result]
    assert "KSPB" in codes
    assert "KPWT" in codes
    assert "KAWO" not in codes


def test_select_airports_case_insensitive():
    """Airport code matching is case-insensitive."""
    from app.archiver import select_airports

    all_airports = [{"code": "KSPB"}]
    config = {"airports": {"archive_all": False, "selected": ["kspb"]}}

    result = select_airports(all_airports, config)
    assert len(result) == 1


def test_select_airports_empty_selected():
    """Empty selected list returns no airports when archive_all is False."""
    from app.archiver import select_airports

    all_airports = [{"code": "KSPB"}]
    config = {"airports": {"archive_all": False, "selected": []}}

    result = select_airports(all_airports, config)
    assert result == []


# ---------------------------------------------------------------------------
# Image URL helpers
# ---------------------------------------------------------------------------

def test_absolute_url_already_absolute():
    from app.archiver import _absolute_url

    url = "https://example.com/img/cam.jpg"
    assert _absolute_url(url, "https://aviationwx.org") == url


def test_absolute_url_relative():
    from app.archiver import _absolute_url

    url = "/webcam/kspb/cam.jpg"
    result = _absolute_url(url, "https://aviationwx.org")
    assert result == "https://aviationwx.org/webcam/kspb/cam.jpg"


def test_looks_like_webcam_true():
    from app.archiver import _looks_like_webcam

    assert _looks_like_webcam("/cams/kspb/webcam.jpg") is True
    assert _looks_like_webcam("/snapshot/camera.webp") is True


def test_looks_like_webcam_false():
    from app.archiver import _looks_like_webcam

    assert _looks_like_webcam("/logo.png") is False
    assert _looks_like_webcam("/styles.css") is False
    assert _looks_like_webcam("/api/data.json") is False


def test_scrape_image_urls_finds_webcam():
    from app.archiver import _scrape_image_urls

    html = '''<html>
    <img src="/cams/kspb/webcam.jpg" alt="webcam">
    <img src="/logo.png" alt="logo">
    <img src="/snapshot/camera.webp" alt="camera">
    </html>'''

    urls = _scrape_image_urls(html, "https://aviationwx.org")
    assert any("webcam.jpg" in u for u in urls)
    assert any("camera.webp" in u for u in urls)
    # logo.png should be excluded
    assert not any("logo.png" in u for u in urls)


def test_extract_attr_double_quoted():
    from app.archiver import _extract_attr

    tag = '<img src="/path/to/img.jpg" alt="test">'
    assert _extract_attr(tag, "src") == "/path/to/img.jpg"


def test_extract_attr_single_quoted():
    from app.archiver import _extract_attr

    tag = "<img src='/path/img.jpg'>"
    assert _extract_attr(tag, "src") == "/path/img.jpg"


def test_extract_attr_missing():
    from app.archiver import _extract_attr

    tag = '<img alt="test">'
    assert _extract_attr(tag, "src") == ""


# ---------------------------------------------------------------------------
# save_image tests
# ---------------------------------------------------------------------------

def test_save_image_creates_directory_structure():
    """save_image creates year/month/day/airport subdirectories."""
    from app.archiver import save_image

    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "archive": {"output_dir": tmpdir},
            "source": {},
        }
        ts = datetime(2024, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        data = b"\xff\xd8\xff" + b"\x00" * 100  # fake JPEG bytes

        path = save_image(data, "http://example.com/webcam.jpg", "KSPB", config, timestamp=ts)

        assert path is not None
        assert os.path.isfile(path)
        assert "2024" in path
        assert "06" in path
        assert "15" in path
        assert "KSPB" in path


def test_save_image_deduplication():
    """Saving identical content a second time returns the existing path without rewriting."""
    from app.archiver import save_image

    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "archive": {"output_dir": tmpdir},
            "source": {},
        }
        ts = datetime(2024, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        data = b"\xff\xd8\xff" + b"\x00" * 100

        path1 = save_image(data, "http://example.com/webcam.jpg", "KSPB", config, timestamp=ts)
        mtime1 = os.path.getmtime(path1)

        path2 = save_image(data, "http://example.com/webcam.jpg", "KSPB", config, timestamp=ts)

        assert path1 == path2
        assert os.path.getmtime(path1) == mtime1  # file was not rewritten


# ---------------------------------------------------------------------------
# fetch_airport_list tests (mocked HTTP)
# ---------------------------------------------------------------------------

def test_fetch_airport_list_success():
    """fetch_airport_list returns airports from a successful API response."""
    from app.archiver import fetch_airport_list

    config = {
        "source": {
            "airports_api_url": "https://api.aviationwx.org/v1/airports",
            "request_timeout": 5,
            "max_retries": 1,
            "retry_delay": 0,
        }
    }

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"airports": [{"code": "KSPB"}, {"code": "KAWO"}]}
    mock_resp.raise_for_status.return_value = None

    with patch("app.archiver.requests.get", return_value=mock_resp):
        airports = fetch_airport_list(config)

    assert len(airports) == 2
    assert airports[0]["code"] == "KSPB"


def test_fetch_airport_list_retries_on_failure():
    """fetch_airport_list retries on RequestException and returns empty list when all fail."""
    import requests as req_lib
    from app.archiver import fetch_airport_list

    config = {
        "source": {
            "airports_api_url": "https://api.aviationwx.org/v1/airports",
            "request_timeout": 5,
            "max_retries": 2,
            "retry_delay": 0,
        }
    }

    with patch("app.archiver.requests.get", side_effect=req_lib.RequestException("network error")):
        airports = fetch_airport_list(config)

    assert airports == []


# ---------------------------------------------------------------------------
# apply_retention tests
# ---------------------------------------------------------------------------

def test_apply_retention_zero_means_no_deletion():
    """retention_days=0 should not delete any files."""
    from app.archiver import apply_retention

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a file
        fpath = os.path.join(tmpdir, "test.jpg")
        with open(fpath, "wb") as fh:
            fh.write(b"data")

        config = {"archive": {"output_dir": tmpdir, "retention_days": 0}}
        deleted = apply_retention(config)

    assert deleted == 0


def test_apply_retention_removes_old_files():
    """apply_retention removes files older than retention_days."""
    import time
    from app.archiver import apply_retention

    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "old.jpg")
        with open(fpath, "wb") as fh:
            fh.write(b"data")

        # Force modification time far in the past (2 days ago)
        old_mtime = time.time() - (2 * 86400 + 1)
        os.utime(fpath, (old_mtime, old_mtime))

        config = {"archive": {"output_dir": tmpdir, "retention_days": 1}}
        deleted = apply_retention(config)

    assert deleted == 1


# ---------------------------------------------------------------------------
# Web GUI tests
# ---------------------------------------------------------------------------

@pytest.fixture
def flask_client():
    """Create a Flask test client with a minimal config."""
    from app.config import DEFAULT_CONFIG
    from app.web import app as flask_app
    import copy

    config = copy.deepcopy(DEFAULT_CONFIG)
    flask_app.config["ARCHIVER_CONFIG"] = config
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as client:
        yield client


def test_dashboard_returns_200(flask_client):
    resp = flask_client.get("/")
    assert resp.status_code == 200
    assert b"AviationWX" in resp.data


def test_api_status_returns_json(flask_client):
    resp = flask_client.get("/api/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert "run_count" in data
    assert "archive" in data


def test_browse_returns_200(flask_client):
    resp = flask_client.get("/browse")
    assert resp.status_code == 200


def test_config_page_returns_200(flask_client):
    resp = flask_client.get("/config")
    assert resp.status_code == 200
    assert b"Configuration" in resp.data


def test_trigger_archive_redirects(flask_client):
    resp = flask_client.post("/run")
    assert resp.status_code == 302  # redirect to dashboard
