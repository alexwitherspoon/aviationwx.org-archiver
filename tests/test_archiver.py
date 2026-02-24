"""
Tests for the AviationWX.org Archiver.
"""

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
    from app.config import DEFAULT_CONFIG, load_config

    config = load_config("/nonexistent/path/config.yaml")

    assert config["archive"]["output_dir"] == DEFAULT_CONFIG["archive"]["output_dir"]
    assert (
        config["schedule"]["interval_minutes"]
        == DEFAULT_CONFIG["schedule"]["interval_minutes"]
    )
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
    import copy

    from app.config import DEFAULT_CONFIG, load_config, save_config

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["schedule"]["interval_minutes"] = 42

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "config.yaml")
        assert save_config(config, path) is True
        loaded = load_config(path)
        assert loaded["schedule"]["interval_minutes"] == 42


def test_save_config_returns_false_on_oserror():
    """save_config returns False and logs when write fails."""
    from app.config import DEFAULT_CONFIG, save_config

    config = dict(DEFAULT_CONFIG)
    with patch("builtins.open", side_effect=OSError(13, "Permission denied")):
        result = save_config(config, "/readonly/config.yaml")
    assert result is False


def test_load_config_logs_error_on_oserror():
    """load_config logs error and returns defaults when config file cannot be read."""
    from app.config import DEFAULT_CONFIG, load_config

    with patch("builtins.open", side_effect=OSError(13, "Permission denied")):
        with patch("app.config.os.path.isfile", return_value=True):
            with patch("app.config.logger") as mock_logger:
                config = load_config("/config/config.yaml")

    assert config["archive"]["output_dir"] == DEFAULT_CONFIG["archive"]["output_dir"]
    mock_logger.error.assert_called()
    assert any(
        "Failed to read" in str(c) or "Permission" in str(c)
        for c in mock_logger.error.call_args_list
    )


def test_load_config_invalid_yaml():
    """A malformed YAML file falls back to defaults without raising."""
    from app.config import DEFAULT_CONFIG, load_config

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
        tmp.write(": : invalid yaml :::")
        tmp_path = tmp.name

    try:
        config = load_config(tmp_path)
        # Should return defaults without crashing
        assert config["web"]["port"] == DEFAULT_CONFIG["web"]["port"]
    finally:
        os.unlink(tmp_path)


def test_validate_config_requires_airports():
    """Config is invalid when no airports are selected."""
    from app.config import validate_config

    config = {
        "archive": {"output_dir": "/archive", "retention_days": 0},
        "airports": {"archive_all": False, "selected": []},
    }
    errors = validate_config(config)
    assert len(errors) >= 1
    assert "airport" in errors[0].lower()


def test_validate_config_valid_with_archive_all():
    """Config is valid when archive_all is True."""
    from app.config import validate_config

    config = {
        "archive": {"output_dir": "/archive", "retention_days": 0},
        "airports": {"archive_all": True, "selected": []},
        "source": {"airports_api_url": "https://api.example.com/airports"},
    }
    assert validate_config(config) == []


def test_validate_config_valid_with_selected():
    """Config is valid when at least one airport is selected."""
    from app.config import validate_config

    config = {
        "archive": {"output_dir": "/archive", "retention_days": 0},
        "airports": {"archive_all": False, "selected": ["KSPB"]},
        "source": {"airports_api_url": "https://api.example.com/airports"},
    }
    assert validate_config(config) == []


def test_check_host_resources_warns_when_output_dir_missing():
    """check_host_resources logs warning when output_dir does not exist."""
    from app.config import check_host_resources

    config = {"archive": {"output_dir": "/nonexistent/mount/path"}}
    with patch("app.config.logger") as mock_logger:
        check_host_resources(config)

    mock_logger.warning.assert_called()
    calls = [str(c) for c in mock_logger.warning.call_args_list]
    assert any("output_dir" in c or "does not exist" in c for c in calls)
    assert any("volume" in c.lower() or "mount" in c.lower() for c in calls)


def test_check_host_resources_warns_when_output_dir_not_writable():
    """check_host_resources logs warning when output_dir exists but is not writable."""
    from app.config import check_host_resources

    with tempfile.TemporaryDirectory() as tmpdir:
        config = {"archive": {"output_dir": tmpdir}}
        config_path = "/nonexistent/config.yaml"
        with patch("app.config.open", side_effect=OSError(13, "Permission denied")):
            with patch("app.config.logger") as mock_logger:
                check_host_resources(config, config_path=config_path)

        mock_logger.warning.assert_called()
        assert any(
            "not writable" in str(c).lower() for c in mock_logger.warning.call_args_list
        )


def test_check_host_resources_no_warning_when_output_dir_ok():
    """check_host_resources does not warn when output_dir exists and is writable."""
    from app.config import check_host_resources

    with tempfile.TemporaryDirectory() as tmpdir:
        config = {"archive": {"output_dir": tmpdir}}
        config_path = os.path.join(tmpdir, "config.yaml")
        with patch("app.config.logger") as mock_logger:
            check_host_resources(config, config_path=config_path)

        warn_strs = [str(c) for c in mock_logger.warning.call_args_list]
        assert not any(
            "output_dir" in s or "Archive" in s or "writable" in s.lower()
            for s in warn_strs
        )


def test_validate_config_requires_source_api_url():
    """Config is invalid when source.airports_api_url is missing or empty."""
    from app.config import validate_config

    config = {
        "archive": {"output_dir": "/archive"},
        "airports": {"archive_all": True, "selected": []},
        "source": {"airports_api_url": ""},
    }
    errors = validate_config(config)
    assert len(errors) >= 1
    assert "airports_api_url" in errors[0].lower() or "source" in errors[0].lower()


def test_validate_config_requires_output_dir():
    """Config is invalid when output_dir is empty."""
    from app.config import validate_config

    config = {
        "archive": {"output_dir": "", "retention_days": 0},
        "airports": {"archive_all": True, "selected": []},
    }
    errors = validate_config(config)
    assert any("output" in e.lower() or "directory" in e.lower() for e in errors)


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


def test_select_airports_uses_id_when_no_code():
    """Airports with id (no code) are matched correctly."""
    from app.archiver import select_airports

    all_airports = [{"id": "kspb", "name": "Scappoose"}]
    config = {"airports": {"archive_all": False, "selected": ["KSPB"]}}

    result = select_airports(all_airports, config)
    assert len(result) == 1
    assert result[0]["id"] == "kspb"


def test_select_airports_uses_icao_when_no_code_or_id():
    """Airports with icao (no code/id) are matched correctly."""
    from app.archiver import select_airports

    all_airports = [{"icao": "KBOI", "name": "Boise"}]
    config = {"airports": {"archive_all": False, "selected": ["kboi"]}}

    result = select_airports(all_airports, config)
    assert len(result) == 1


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


def test_looks_like_webcam_url_with_query_string():
    """URLs with query params (e.g. image.jpg?t=123) are accepted."""
    from app.archiver import _looks_like_webcam

    assert _looks_like_webcam("/cams/webcam.jpg?token=abc") is True


def test_scrape_image_urls_finds_webcam():
    from app.archiver import _scrape_image_urls

    html = """<html>
    <img src="/cams/kspb/webcam.jpg" alt="webcam">
    <img src="/logo.png" alt="logo">
    <img src="/snapshot/camera.webp" alt="camera">
    </html>"""

    urls = _scrape_image_urls(html, "https://aviationwx.org")
    assert any("webcam.jpg" in u for u in urls)
    assert any("camera.webp" in u for u in urls)
    # logo.png should be excluded
    assert not any("logo.png" in u for u in urls)


def test_scrape_image_urls_empty_html_returns_empty():
    from app.archiver import _scrape_image_urls

    assert _scrape_image_urls("", "https://example.com") == []
    html_no_imgs = "<html><body>No images</body></html>"
    assert _scrape_image_urls(html_no_imgs, "https://x.com") == []


def test_scrape_image_urls_ignores_img_without_matching_src():
    """Img tags without webcam-like src are skipped."""
    from app.archiver import _scrape_image_urls

    html = '<html><img src="/logo.png"><img src="/banner.gif"></html>'
    urls = _scrape_image_urls(html, "https://example.com")
    assert urls == []


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


def test_extract_attr_unquoted_value():
    """Unquoted attribute values are extracted."""
    from app.archiver import _extract_attr

    tag = "<img src=/path/cam.jpg alt=test>"
    assert _extract_attr(tag, "src") == "/path/cam.jpg"


def test_extract_urls_from_api_uses_api_base_for_relative_paths():
    """API-returned relative image URLs must be resolved against API host."""
    from app.archiver import _extract_urls_from_api

    data = {
        "webcams": [
            {"image_url": "/v1/airports/kspb/webcams/0/image"},
        ],
    }
    urls = _extract_urls_from_api(data, "https://api.aviationwx.org/v1")
    assert len(urls) == 1
    assert urls[0] == "https://api.aviationwx.org/v1/airports/kspb/webcams/0/image"


def test_extract_urls_from_api_prefers_image_url_over_other_keys():
    """First matching key in (image_url, url, src, snapshot_url) is used."""
    from app.archiver import _extract_urls_from_api

    data = {
        "webcams": [
            {
                "image_url": "/img1.jpg",
                "url": "/img2.jpg",
                "src": "/img3.jpg",
            },
        ],
    }
    urls = _extract_urls_from_api(data, "https://api.example.com")
    assert len(urls) == 1
    assert "img1.jpg" in urls[0]


def test_extract_urls_from_api_handles_list_format():
    """API can return a bare list of webcam items."""
    from app.archiver import _extract_urls_from_api

    data = [{"image_url": "/cam/0/image"}, {"url": "https://example.com/cam.jpg"}]
    urls = _extract_urls_from_api(data, "https://api.example.com")
    assert len(urls) == 2
    assert "api.example.com" in urls[0]
    assert urls[1] == "https://example.com/cam.jpg"


def test_extract_urls_from_api_handles_empty_response():
    """Empty or missing webcams returns empty list."""
    from app.archiver import _extract_urls_from_api

    assert _extract_urls_from_api({}, "https://api.example.com") == []
    assert _extract_urls_from_api({"webcams": []}, "https://api.example.com") == []


def test_fetch_image_urls_webcam_api_url_no_double_v1():
    """Webcam API URL must not have duplicate /v1 path segment."""
    from app.archiver import fetch_image_urls

    config = {
        "source": {
            "base_url": "https://aviationwx.org",
            "airports_api_url": "https://api.aviationwx.org/v1/airports",
            "request_timeout": 30,
        },
    }
    airport = {"id": "kspb", "icao": "KSPB"}

    with patch("app.archiver.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "webcams": [{"image_url": "/v1/airports/kspb/webcams/0/image"}],
        }
        mock_get.return_value = mock_resp

        urls = fetch_image_urls(airport, config)

        call_url = mock_get.call_args[0][0]
        assert "/v1/v1/" not in call_url
        assert call_url == "https://api.aviationwx.org/v1/airports/kspb/webcams"
        assert len(urls) == 1
        assert "api.aviationwx.org" in urls[0]


def test_fetch_image_urls_falls_back_to_page_scrape_when_api_empty():
    """When API returns no webcams, page scrape is tried and can succeed."""
    from app.archiver import fetch_image_urls

    config = {
        "source": {
            "base_url": "https://aviationwx.org",
            "airports_api_url": "https://api.aviationwx.org/v1/airports",
            "request_timeout": 30,
        },
    }
    airport = {"id": "kspb"}

    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.ok = True
        if "webcams" in url:
            resp.json.return_value = {"webcams": []}
        else:
            resp.text = '<html><img src="/cams/kspb/webcam_snapshot.jpg"></html>'
        return resp

    with patch("app.archiver.requests.get", side_effect=mock_get):
        urls = fetch_image_urls(airport, config)

    assert len(urls) == 1
    assert "webcam_snapshot" in urls[0]
    assert "aviationwx.org" in urls[0]


def test_fetch_image_urls_logs_warning_on_invalid_webcam_api_json():
    """fetch_image_urls logs warning when webcam API returns invalid JSON."""
    import json as json_lib

    from app.archiver import fetch_image_urls

    config = {
        "source": {
            "base_url": "https://aviationwx.org",
            "airports_api_url": "https://api.aviationwx.org/v1/airports",
            "request_timeout": 30,
        },
    }
    airport = {"id": "kspb"}

    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.ok = True
        if "webcams" in url:
            resp.json.side_effect = json_lib.JSONDecodeError("Expecting value", "", 0)
        else:
            resp.text = "<html></html>"
        return resp

    with patch("app.archiver.requests.get", side_effect=mock_get):
        with patch("app.archiver.logger") as mock_logger:
            urls = fetch_image_urls(airport, config)

    assert urls == []
    mock_logger.warning.assert_called()
    assert any("invalid JSON" in str(c) for c in mock_logger.warning.call_args_list)


def test_fetch_image_urls_tries_page_scrape_when_webcam_api_returns_non_ok():
    """When webcam API returns non-OK status, page scrape fallback is tried."""
    from app.archiver import fetch_image_urls

    config = {
        "source": {
            "base_url": "https://aviationwx.org",
            "airports_api_url": "https://api.aviationwx.org/v1/airports",
            "request_timeout": 30,
        },
    }
    airport = {"id": "kspb"}

    def mock_get(url, **kwargs):
        resp = MagicMock()
        if "webcams" in url:
            resp.ok = False
            resp.status_code = 404
        else:
            resp.ok = True
            resp.text = '<html><img src="/cams/kspb/webcam_snapshot.jpg"></html>'
        return resp

    with patch("app.archiver.requests.get", side_effect=mock_get):
        urls = fetch_image_urls(airport, config)

    assert len(urls) == 1
    assert "webcam_snapshot" in urls[0]


def test_fetch_image_urls_page_non_ok_falls_through_to_warning():
    """When airport page returns non-OK, we log debug and eventually warn."""
    from app.archiver import fetch_image_urls

    config = {
        "source": {
            "base_url": "https://aviationwx.org",
            "airports_api_url": "https://api.aviationwx.org/v1/airports",
            "request_timeout": 30,
        },
    }
    airport = {"id": "kspb", "icao": "KSPB"}

    def mock_get(url, **kwargs):
        resp = MagicMock()
        if "webcams" in url:
            resp.ok = True
            resp.json.return_value = {"webcams": []}
        else:
            resp.ok = False
            resp.status_code = 404
        resp.text = "<html></html>"
        return resp

    with patch("app.archiver.requests.get", side_effect=mock_get):
        urls = fetch_image_urls(airport, config)

    assert urls == []


def test_fetch_image_urls_logs_warning_when_no_images_found():
    """When no images found, a warning is logged with diagnostic info."""
    from app.archiver import fetch_image_urls

    config = {
        "source": {
            "base_url": "https://aviationwx.org",
            "airports_api_url": "https://api.aviationwx.org/v1/airports",
            "request_timeout": 30,
        },
    }
    airport = {"id": "kspb", "icao": "KSPB"}

    with patch("app.archiver.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"webcams": []}
        mock_resp.text = '<html><img src="/logo.png"></html>'
        mock_get.return_value = mock_resp

        with patch("app.archiver.logger") as mock_logger:
            urls = fetch_image_urls(airport, config)

        assert urls == []
        mock_logger.warning.assert_called_once()
        msg = str(mock_logger.warning.call_args)
        assert "No images" in msg
        assert "api.aviationwx.org" in msg
        assert "base_url" in msg or "config" in msg


# ---------------------------------------------------------------------------
# download_image tests
# ---------------------------------------------------------------------------


def test_download_image_success_returns_bytes():
    """download_image returns image bytes on success."""
    from app.archiver import download_image

    config = {
        "source": {
            "request_timeout": 5,
            "max_retries": 1,
            "retry_delay": 0,
        },
    }
    image_data = b"\xff\xd8\xff\xe0\x00\x10JFIF"  # JPEG header

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.headers = {"content-type": "image/jpeg"}
    mock_resp.content = image_data

    with patch("app.archiver.requests.get", return_value=mock_resp):
        result = download_image("https://example.com/cam.jpg", config)

    assert result == image_data


def test_download_image_returns_none_for_non_image_content_type():
    """download_image returns None when content-type is not image/*."""
    from app.archiver import download_image

    config = {
        "source": {
            "request_timeout": 5,
            "max_retries": 1,
            "retry_delay": 0,
        },
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.headers = {"content-type": "text/html"}

    with patch("app.archiver.requests.get", return_value=mock_resp):
        result = download_image("https://example.com/page", config)

    assert result is None


def test_download_image_returns_none_after_all_retries_fail():
    """download_image returns None when all retries fail."""
    import requests as req_lib

    from app.archiver import download_image

    config = {
        "source": {
            "request_timeout": 5,
            "max_retries": 2,
            "retry_delay": 0,
        },
    }

    with patch(
        "app.archiver.requests.get",
        side_effect=req_lib.RequestException("connection refused"),
    ):
        result = download_image("https://example.com/cam.jpg", config)

    assert result is None


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

        path = save_image(
            data, "http://example.com/webcam.jpg", "KSPB", config, timestamp=ts
        )

        assert path is not None
        assert os.path.isfile(path)
        assert "2024" in path
        assert "06" in path
        assert "15" in path
        assert "KSPB" in path


def test_save_image_deduplication():
    """Saving identical content a second time returns existing path, no rewrite."""
    from app.archiver import save_image

    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "archive": {"output_dir": tmpdir},
            "source": {},
        }
        ts = datetime(2024, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        data = b"\xff\xd8\xff" + b"\x00" * 100

        path1 = save_image(
            data, "http://example.com/webcam.jpg", "KSPB", config, timestamp=ts
        )
        mtime1 = os.path.getmtime(path1)

        path2 = save_image(
            data, "http://example.com/webcam.jpg", "KSPB", config, timestamp=ts
        )

        assert path1 == path2
        assert os.path.getmtime(path1) == mtime1  # file was not rewritten


def test_save_image_returns_none_on_directory_error():
    """save_image returns None when directory cannot be created."""
    from app.archiver import save_image

    with tempfile.TemporaryDirectory() as tmpdir:
        config = {"archive": {"output_dir": tmpdir}, "source": {}}
        data = b"\xff\xd8\xff"
        ts = datetime(2024, 6, 15, 14, 30, 0, tzinfo=timezone.utc)

        with patch(
            "app.archiver.os.makedirs", side_effect=OSError(13, "Permission denied")
        ):
            result = save_image(
                data, "http://example.com/cam.jpg", "KSPB", config, timestamp=ts
            )

    assert result is None


def test_save_image_handles_url_without_path_basename():
    """URL with no path uses 'image' as basename fallback."""
    from app.archiver import save_image

    with tempfile.TemporaryDirectory() as tmpdir:
        config = {"archive": {"output_dir": tmpdir}, "source": {}}
        data = b"\xff\xd8\xff"
        ts = datetime(2024, 6, 15, 14, 30, 0, tzinfo=timezone.utc)

        path = save_image(data, "https://example.com/", "KSPB", config, timestamp=ts)
        assert path is not None
        assert "image" in os.path.basename(path)


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


def test_fetch_airport_list_sends_api_key_when_configured():
    """fetch_airport_list includes X-API-Key header when api_key is set."""
    from app.archiver import fetch_airport_list

    config = {
        "source": {
            "airports_api_url": "https://api.aviationwx.org/v1/airports",
            "api_key": "test-partner-key",
            "request_timeout": 5,
            "max_retries": 1,
            "retry_delay": 0,
        }
    }

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"airports": [{"code": "KSPB"}]}
    mock_resp.raise_for_status.return_value = None

    with patch("app.archiver.requests.get", return_value=mock_resp) as mock_get:
        fetch_airport_list(config)

    mock_get.assert_called_once()
    assert mock_get.call_args.kwargs.get("headers") == {"X-API-Key": "test-partner-key"}


def test_fetch_airport_list_retries_on_failure():
    """fetch_airport_list retries on RequestException, returns [] when all fail."""
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

    with patch(
        "app.archiver.requests.get",
        side_effect=req_lib.RequestException("network error"),
    ):
        airports = fetch_airport_list(config)

    assert airports == []


def test_fetch_airport_list_bare_list_response():
    """fetch_airport_list handles API returning a bare list."""
    from app.archiver import fetch_airport_list

    config = {
        "source": {
            "airports_api_url": "https://api.example.com/airports",
            "request_timeout": 5,
            "max_retries": 1,
            "retry_delay": 0,
        },
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = [{"id": "kspb"}, {"id": "kboi"}]

    with patch("app.archiver.requests.get", return_value=mock_resp):
        airports = fetch_airport_list(config)

    assert len(airports) == 2
    assert airports[0]["id"] == "kspb"


def test_fetch_airport_list_data_key_fallback():
    """fetch_airport_list uses data key when airports key missing."""
    from app.archiver import fetch_airport_list

    config = {
        "source": {
            "airports_api_url": "https://api.example.com/airports",
            "request_timeout": 5,
            "max_retries": 1,
            "retry_delay": 0,
        },
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"data": [{"code": "KSPB"}]}

    with patch("app.archiver.requests.get", return_value=mock_resp):
        airports = fetch_airport_list(config)

    assert len(airports) == 1
    assert airports[0]["code"] == "KSPB"


def test_fetch_airport_list_returns_empty_on_invalid_json():
    """fetch_airport_list logs warning and returns [] when API returns invalid JSON."""
    import json as json_lib

    from app.archiver import fetch_airport_list

    config = {
        "source": {
            "airports_api_url": "https://api.example.com/airports",
            "request_timeout": 5,
            "max_retries": 1,
            "retry_delay": 0,
        },
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.side_effect = json_lib.JSONDecodeError("Expecting value", "", 0)

    with patch("app.archiver.requests.get", return_value=mock_resp):
        airports = fetch_airport_list(config)

    assert airports == []


def test_fetch_airport_list_returns_empty_when_airports_non_list():
    """fetch_airport_list returns [] when API returns non-list (e.g. airports: 123)."""
    from app.archiver import fetch_airport_list

    config = {
        "source": {
            "airports_api_url": "https://api.example.com/airports",
            "request_timeout": 5,
            "max_retries": 1,
            "retry_delay": 0,
        },
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"airports": "invalid"}

    with patch("app.archiver.requests.get", return_value=mock_resp):
        airports = fetch_airport_list(config)

    assert airports == []


# ---------------------------------------------------------------------------
# parse_storage_gb tests (constants)
# ---------------------------------------------------------------------------


def test_parse_storage_gb_accepts_numeric_strings():
    """parse_storage_gb parses '10', '10.5', '0.002' as GB."""
    from app.constants import parse_storage_gb

    assert parse_storage_gb("10") == 10.0
    assert parse_storage_gb("10.5") == 10.5
    assert parse_storage_gb("0.002") == 0.002
    assert parse_storage_gb(100) == 100.0


def test_parse_storage_gb_accepts_tb_suffix():
    """parse_storage_gb converts '1TB', '1 TB' to GB (1024)."""
    from app.constants import parse_storage_gb

    assert parse_storage_gb("1TB") == 1024.0
    assert parse_storage_gb("1 TB") == 1024.0
    assert parse_storage_gb("0.5TB") == 512.0


def test_parse_storage_gb_returns_zero_for_empty_or_invalid():
    """parse_storage_gb returns 0 for None, '', or invalid input."""
    from app.constants import parse_storage_gb

    assert parse_storage_gb(None) == 0.0
    assert parse_storage_gb("") == 0.0
    assert parse_storage_gb("  ") == 0.0
    assert parse_storage_gb("invalid") == 0.0


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
# run_archive tests
# ---------------------------------------------------------------------------


def test_run_archive_full_flow_with_mocked_http():
    """run_archive fetches airports, images, and saves them."""
    from app.archiver import run_archive

    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "source": {
                "base_url": "https://aviationwx.org",
                "airports_api_url": "https://api.aviationwx.org/v1/airports",
                "request_timeout": 30,
                "max_retries": 1,
                "retry_delay": 0,
            },
            "archive": {"output_dir": tmpdir, "retention_days": 0},
            "airports": {"archive_all": False, "selected": ["KSPB"]},
        }

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.ok = True
            if "airports" in url and "webcams" not in url:
                resp.json.return_value = {
                    "airports": [{"id": "kspb", "icao": "KSPB"}],
                }
            elif "/image" in url or url.endswith("image"):
                resp.headers = {"content-type": "image/jpeg"}
                resp.content = b"\xff\xd8\xff\xe0"
            elif "webcams" in url:
                resp.json.return_value = {
                    "webcams": [
                        {"image_url": "/v1/airports/kspb/webcams/0/image"},
                    ],
                }
            else:
                resp.headers = {"content-type": "image/jpeg"}
                resp.content = b"\xff\xd8\xff\xe0"
            return resp

        with patch("app.archiver.requests.get", side_effect=mock_get):
            stats = run_archive(config)

        assert stats["airports_processed"] == 1
        assert stats["images_fetched"] == 1
        assert stats["images_saved"] == 1
        assert stats["errors"] == 0
        assert len(os.listdir(tmpdir)) > 0


def test_run_archive_returns_early_when_no_airports_from_api():
    """run_archive increments errors and returns when API returns no airports."""
    from app.archiver import run_archive

    config = {
        "source": {
            "airports_api_url": "https://api.example.com/airports",
            "request_timeout": 5,
            "max_retries": 1,
            "retry_delay": 0,
        },
        "archive": {"output_dir": "/tmp", "retention_days": 0},
        "airports": {"archive_all": True, "selected": []},
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"airports": []}

    with patch("app.archiver.requests.get", return_value=mock_resp):
        stats = run_archive(config)

    assert stats["airports_processed"] == 0
    assert stats["images_fetched"] == 0
    assert stats["errors"] == 1


def test_run_archive_warns_when_no_airports_selected():
    """run_archive logs warning and returns early when select_airports returns []."""
    from app.archiver import run_archive

    config = {
        "source": {
            "airports_api_url": "https://api.example.com/airports",
            "request_timeout": 5,
            "max_retries": 1,
            "retry_delay": 0,
        },
        "archive": {"output_dir": "/tmp", "retention_days": 0},
        "airports": {"archive_all": False, "selected": ["NONEXISTENT"]},
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"airports": [{"id": "kspb"}]}

    with patch("app.archiver.requests.get", return_value=mock_resp):
        with patch("app.archiver.logger") as mock_logger:
            stats = run_archive(config)

    assert stats["airports_processed"] == 0
    assert stats["images_fetched"] == 0
    mock_logger.warning.assert_called()
    assert any("No airports" in str(c) for c in mock_logger.warning.call_args_list)


def test_run_archive_logs_warning_when_zero_images_fetched():
    """run_archive logs warning when airports processed but 0 images fetched."""
    from app.archiver import run_archive

    config = {
        "source": {
            "base_url": "https://aviationwx.org",
            "airports_api_url": "https://api.aviationwx.org/v1/airports",
            "request_timeout": 30,
            "max_retries": 1,
            "retry_delay": 0,
        },
        "archive": {"output_dir": "/tmp", "retention_days": 0},
        "airports": {"archive_all": False, "selected": ["KSPB"]},
    }

    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.ok = True
        if "webcams" not in url:
            resp.json.return_value = {"airports": [{"id": "kspb"}]}
        else:
            resp.json.return_value = {"webcams": []}
            resp.text = "<html></html>"
        return resp

    with patch("app.archiver.requests.get", side_effect=mock_get):
        with patch("app.archiver.logger") as mock_logger:
            stats = run_archive(config)

    assert stats["airports_processed"] == 1
    assert stats["images_fetched"] == 0
    mock_logger.warning.assert_called()
    warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
    assert any("No images" in c for c in warning_calls)


def test_fetch_history_frames_returns_empty_when_no_history_url():
    """fetch_history_frames returns [] when webcam has no history_url."""
    from app.archiver import fetch_history_frames

    webcam = {"index": 0, "history_enabled": True, "history_url": None}
    config = {
        "source": {"airports_api_url": "https://api.example.com/v1/airports", "request_timeout": 5},
    }
    assert fetch_history_frames("KSPB", webcam, config) == []


def test_fetch_history_frames_returns_frames():
    """fetch_history_frames returns list of frame dicts from history API."""
    from app.archiver import fetch_history_frames

    webcam = {
        "index": 0,
        "history_enabled": True,
        "history_url": "/v1/airports/kspb/webcams/0/history",
    }
    config = {
        "source": {
            "airports_api_url": "https://api.aviationwx.org/v1/airports",
            "request_timeout": 5,
        },
    }

    mock_resp = MagicMock()
    mock_resp.ok = True
    base = "/v1/airports/kspb/webcams/0/history?ts="
    mock_resp.json.return_value = {
        "frames": [
            {"timestamp": 1700000060, "url": base + "1700000060"},
            {"timestamp": 1700000000, "url": base + "1700000000"},
        ],
    }

    with patch("app.archiver.requests.get", return_value=mock_resp):
        frames = fetch_history_frames("KSPB", webcam, config)

    assert len(frames) == 2
    assert frames[0]["timestamp"] == 1700000000
    assert frames[1]["timestamp"] == 1700000060
    assert frames[0]["cam_index"] == 0
    assert "1700000000" in frames[0]["url"]
    assert frames[0]["timestamp"] < frames[1]["timestamp"]


def test_get_existing_frames_returns_empty_when_output_dir_missing():
    """_get_existing_frames returns empty set when output_dir does not exist."""
    from app.archiver import _get_existing_frames

    existing = _get_existing_frames("/nonexistent/path/12345", "KSPB")
    assert existing == set()


def test_get_existing_frames_finds_history_files():
    """_get_existing_frames returns set of (timestamp, cam_index) from archive."""
    from app.archiver import _get_existing_frames

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "2024", "01", "15", "KSPB")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "1700000000_0.jpg"), "wb") as f:
            f.write(b"x")
        with open(os.path.join(path, "1700000060_0.jpg"), "wb") as f:
            f.write(b"x")
        with open(os.path.join(path, "1700000120_1.jpg"), "wb") as f:
            f.write(b"x")

        existing = _get_existing_frames(tmpdir, "KSPB")
        assert (1700000000, 0) in existing
        assert (1700000060, 0) in existing
        assert (1700000120, 1) in existing
        assert len(existing) == 3


def test_save_history_image_creates_correct_structure():
    """save_history_image creates output_dir/YYYY/MM/DD/AIRPORT/{ts}_{cam}.jpg."""
    from app.archiver import save_history_image

    with tempfile.TemporaryDirectory() as tmpdir:
        config = {"archive": {"output_dir": tmpdir}}
        ts = 1700000000
        data = b"\xff\xd8\xff" + b"\x00" * 100

        path = save_history_image(data, "KSPB", 0, ts, config)

        assert path is not None
        assert os.path.isfile(path)
        assert "KSPB" in path
        assert path.endswith("1700000000_0.jpg")
        assert "2023" in path and "11" in path


def test_save_history_image_returns_none_on_oserror():
    """save_history_image returns None when makedirs or write fails."""
    from app.archiver import save_history_image

    config = {"archive": {"output_dir": "/tmp"}}
    data = b"\xff\xd8\xff"

    with patch("app.archiver.os.makedirs", side_effect=OSError(13, "Permission denied")):
        result = save_history_image(data, "KSPB", 0, 1700000000, config)

    assert result is None


def test_webcam_to_image_url_returns_absolute_url():
    """_webcam_to_image_url converts relative URL using api_base."""
    from app.archiver import _webcam_to_image_url

    webcam = {"index": 0, "image_url": "/v1/airports/kspb/webcams/0/image"}
    config = {
        "source": {"airports_api_url": "https://api.example.com/v1/airports"},
    }
    url = _webcam_to_image_url(webcam, config)
    assert url is not None
    assert url.startswith("http")
    assert "webcams/0/image" in url


def test_webcam_to_image_url_returns_none_when_no_url_keys():
    """_webcam_to_image_url returns None when webcam has no image URL keys."""
    from app.archiver import _webcam_to_image_url

    webcam = {"index": 0, "name": "Main Cam"}
    config = {"source": {"airports_api_url": "https://api.example.com/v1/airports"}}
    assert _webcam_to_image_url(webcam, config) is None


def test_run_archive_skips_airport_with_no_code():
    """run_archive skips airports that have no code/id/icao."""
    from app.archiver import run_archive

    config = {
        "source": {
            "base_url": "https://aviationwx.org",
            "airports_api_url": "https://api.example.com/v1/airports",
            "request_timeout": 5,
            "max_retries": 1,
            "retry_delay": 0,
        },
        "archive": {"output_dir": "/tmp", "retention_days": 0},
        "airports": {"archive_all": True, "selected": []},
    }

    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.raise_for_status.return_value = None
        if "airports" in url and "webcams" not in url:
            resp.json.return_value = {
                "airports": [
                    {"code": "KSPB"},
                    {"name": "No Code Airport"},  # No code, id, or icao
                ],
            }
        elif "webcams" in url:
            resp.json.return_value = {"webcams": []}
            resp.text = '<html><img src="/cams/kspb/webcam_snapshot.jpg"></html>'
        elif "airport=" in url:
            resp.text = '<html><img src="/cams/kspb/webcam_snapshot.jpg"></html>'
        else:
            resp.headers = {"content-type": "image/jpeg"}
            resp.content = b"\xff\xd8\xff"
        return resp

    with patch("app.archiver.requests.get", side_effect=mock_get):
        stats = run_archive(config)

    assert stats["airports_processed"] == 1
    assert stats["errors"] == 0


def test_run_archive_use_history_false_uses_current_only():
    """run_archive with use_history_api=False uses current image only (no history API)."""
    from app.archiver import run_archive

    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "source": {
                "base_url": "https://aviationwx.org",
                "airports_api_url": "https://api.aviationwx.org/v1/airports",
                "request_timeout": 30,
                "max_retries": 1,
                "retry_delay": 0,
                "use_history_api": False,
            },
            "archive": {"output_dir": tmpdir, "retention_days": 0},
            "airports": {"archive_all": False, "selected": ["KSPB"]},
        }

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.ok = True
            resp.raise_for_status.return_value = None
            # Image download: URL ends with /image or has /image in path
            if "/image" in url or url.endswith("image"):
                resp.headers = {"content-type": "image/jpeg"}
                resp.content = b"\xff\xd8\xff\xe0"
            elif "airports" in url and "webcams" not in url:
                resp.json.return_value = {
                    "airports": [{"id": "kspb", "icao": "KSPB"}],
                }
            elif "webcams" in url:
                resp.json.return_value = {
                    "webcams": [{"image_url": "/v1/airports/kspb/webcams/0/image"}],
                }
            else:
                resp.headers = {"content-type": "image/jpeg"}
                resp.content = b"\xff\xd8\xff\xe0"
            return resp

        with patch("app.archiver.requests.get", side_effect=mock_get):
            stats = run_archive(config)

        assert stats["airports_processed"] == 1
        assert stats["images_fetched"] >= 1
        assert stats["images_saved"] >= 1
        assert stats["errors"] == 0


def test_run_archive_history_mode_downloads_missing_frames():
    """History mode: run_archive fetches history API and downloads missing frames."""
    from app.archiver import run_archive

    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "source": {
                "base_url": "https://aviationwx.org",
                "airports_api_url": "https://api.aviationwx.org/v1/airports",
                "request_timeout": 30,
                "max_retries": 1,
                "retry_delay": 0,
                "use_history_api": True,
            },
            "archive": {"output_dir": tmpdir, "retention_days": 0},
            "airports": {"archive_all": False, "selected": ["KSPB"]},
        }

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.ok = True
            if "airports" in url and "webcams" not in url:
                resp.json.return_value = {
                    "airports": [{"id": "kspb", "icao": "KSPB"}],
                }
            elif "history" in url and "ts=" not in url:
                resp.json.return_value = {
                    "success": True,
                    "frames": [
                        {
                            "timestamp": 1700000000,
                            "timestamp_iso": "2023-11-15T02:13:20+00:00",
                            "url": "/v1/airports/kspb/webcams/0/history?ts=1700000000",
                        },
                    ],
                }
            elif "history" in url or "/image" in url:
                resp.headers = {"content-type": "image/jpeg"}
                resp.content = b"\xff\xd8\xff\xe0"
            elif "webcams" in url:
                resp.json.return_value = {
                    "webcams": [
                        {
                            "index": 0,
                            "image_url": "/v1/airports/kspb/webcams/0/image",
                            "history_enabled": True,
                            "history_url": "/v1/airports/kspb/webcams/0/history",
                        },
                    ],
                }
            else:
                resp.headers = {"content-type": "image/jpeg"}
                resp.content = b"\xff\xd8\xff\xe0"
            return resp

        with patch("app.archiver.requests.get", side_effect=mock_get):
            stats = run_archive(config)

        assert stats["airports_processed"] == 1
        assert stats["images_fetched"] >= 1
        assert stats["images_saved"] >= 1
        assert stats["errors"] == 0
        assert any("2023" in d for d in os.listdir(tmpdir))


def test_run_archive_stops_at_deadline():
    """When deadline is reached, run stops early with timed_out=True."""
    from app.archiver import run_archive

    config = {
        "schedule": {"job_timeout_minutes": 30},
        "source": {
            "airports_api_url": "https://api.example.com/airports",
            "request_timeout": 5,
            "max_retries": 1,
            "retry_delay": 0,
        },
        "archive": {"output_dir": "/tmp", "retention_days": 0},
        "airports": {"archive_all": False, "selected": ["KSPB"]},
    }

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"airports": [{"id": "kspb", "icao": "KSPB"}]}

    with patch("app.archiver.requests.get", return_value=mock_resp):
        stats = run_archive(config, deadline=0)

    assert stats.get("timed_out") is True


def test_run_archive_warns_when_fetched_but_none_saved():
    """run_archive logs warning when images fetched but all save attempts fail."""
    from app.archiver import run_archive

    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "source": {
                "base_url": "https://aviationwx.org",
                "airports_api_url": "https://api.aviationwx.org/v1/airports",
                "request_timeout": 30,
                "max_retries": 1,
                "retry_delay": 0,
            },
            "archive": {"output_dir": tmpdir, "retention_days": 0},
            "airports": {"archive_all": False, "selected": ["KSPB"]},
        }

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.ok = True
            if "airports" in url and "webcams" not in url:
                resp.json.return_value = {"airports": [{"id": "kspb"}]}
            elif "/image" in url or url.endswith("image"):
                resp.headers = {"content-type": "image/jpeg"}
                resp.content = b"\xff\xd8\xff"
            elif "webcams" in url:
                resp.json.return_value = {
                    "webcams": [{"image_url": "/v1/airports/kspb/webcams/0/image"}],
                }
            return resp

        with patch("app.archiver.requests.get", side_effect=mock_get):
            with patch("app.archiver.save_image", return_value=None):
                with patch("app.archiver.logger") as mock_logger:
                    stats = run_archive(config)

        assert stats["images_fetched"] == 1
        assert stats["images_saved"] == 0
        mock_logger.warning.assert_called()
        assert any(
            "fetched but none saved" in str(c).lower() or "output_dir" in str(c).lower()
            for c in mock_logger.warning.call_args_list
        )


# ---------------------------------------------------------------------------
# apply_retention error handling tests
# ---------------------------------------------------------------------------


def test_apply_retention_returns_zero_when_output_dir_missing():
    """apply_retention returns 0 and logs warning when output_dir does not exist."""
    from app.archiver import apply_retention

    config = {
        "archive": {
            "output_dir": "/nonexistent/path/12345",
            "retention_days": 7,
        },
    }
    with patch("app.archiver.logger") as mock_logger:
        deleted = apply_retention(config)
    assert deleted == 0
    mock_logger.warning.assert_called()
    assert "output_dir" in str(mock_logger.warning.call_args)
    assert (
        "volume" in str(mock_logger.warning.call_args).lower()
        or "mount" in str(mock_logger.warning.call_args).lower()
    )


def test_apply_retention_by_size_no_deletion_when_under_limit():
    """apply_retention with retention_max_gb does nothing when total size is under limit."""
    from app.archiver import apply_retention

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1MB total, limit 10MB
        fpath = os.path.join(tmpdir, "small.jpg")
        with open(fpath, "wb") as fh:
            fh.write(b"x" * (1024 * 1024))

        config = {
            "archive": {
                "output_dir": tmpdir,
                "retention_days": 0,
                "retention_max_gb": 10,  # 10 GB - plenty of room
            },
        }
        deleted = apply_retention(config)

        assert deleted == 0
        assert os.path.exists(fpath)


def test_apply_retention_by_size_removes_oldest_first():
    """apply_retention with retention_max_gb removes oldest files until under limit."""
    import time

    from app.archiver import apply_retention

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create 4 files of 1MB each = 4MB total, with distinct mtimes (oldest first)
        for i in range(4):
            fpath = os.path.join(tmpdir, f"file_{i}.jpg")
            with open(fpath, "wb") as fh:
                fh.write(b"x" * (1024 * 1024))
            if i < 3:
                time.sleep(0.01)  # Ensure distinct mtimes

        # Limit to 2MB = ~0.002 GB; should remove 2 oldest files (file_0, file_1)
        config = {
            "archive": {
                "output_dir": tmpdir,
                "retention_days": 0,
                "retention_max_gb": 0.002,  # ~2 MB
            },
        }
        deleted = apply_retention(config)

        assert deleted >= 2
        remaining = sum(
            os.path.getsize(os.path.join(tmpdir, f))
            for f in os.listdir(tmpdir)
        )
        assert remaining <= 2.5 * 1024 * 1024  # Allow small tolerance
        # Oldest (file_0) must be gone; newest (file_3) must remain
        assert not os.path.exists(os.path.join(tmpdir, "file_0.jpg"))
        assert os.path.exists(os.path.join(tmpdir, "file_3.jpg"))


def test_apply_retention_by_size_works_with_nested_archive_structure():
    """apply_retention by size works with YYYY/MM/DD/AIRPORT/ directory structure."""
    import time

    from app.archiver import apply_retention

    with tempfile.TemporaryDirectory() as tmpdir:
        # Mimic archive layout: 2024/01/15/KSPB/file.jpg
        archive_path = os.path.join(tmpdir, "2024", "01", "15", "KSPB")
        os.makedirs(archive_path, exist_ok=True)
        for i in range(3):
            fpath = os.path.join(archive_path, f"frame_{i}.jpg")
            with open(fpath, "wb") as fh:
                fh.write(b"x" * (1024 * 1024))  # 1MB each
            if i < 2:
                time.sleep(0.01)

        config = {
            "archive": {
                "output_dir": tmpdir,
                "retention_days": 0,
                "retention_max_gb": 0.001,  # ~1 MB - keep only 1 file
            },
        }
        deleted = apply_retention(config)

        assert deleted >= 2
        remaining_files = [
            f for f in os.listdir(archive_path) if f.endswith(".jpg")
        ]
        assert len(remaining_files) <= 1
        # Newest (frame_2) should remain
        assert os.path.exists(os.path.join(archive_path, "frame_2.jpg"))


def test_apply_retention_max_gb_zero_disabled():
    """retention_max_gb=0 with retention_days=0 means no retention (no deletion)."""
    from app.archiver import apply_retention

    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "test.jpg")
        with open(fpath, "wb") as fh:
            fh.write(b"x" * (10 * 1024 * 1024))  # 10MB

        config = {
            "archive": {
                "output_dir": tmpdir,
                "retention_days": 0,
                "retention_max_gb": 0,
            },
        }
        deleted = apply_retention(config)

        assert deleted == 0
        assert os.path.exists(fpath)


def test_apply_retention_max_gb_string_parsed():
    """apply_retention accepts retention_max_gb as string (e.g. from YAML)."""
    import time

    from app.archiver import apply_retention

    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(2):
            fpath = os.path.join(tmpdir, f"file_{i}.jpg")
            with open(fpath, "wb") as fh:
                fh.write(b"x" * (1024 * 1024))
            if i < 1:
                time.sleep(0.01)

        config = {
            "archive": {
                "output_dir": tmpdir,
                "retention_days": 0,
                "retention_max_gb": "0.0005",  # String ~0.5 MB
            },
        }
        deleted = apply_retention(config)

        assert deleted >= 1
        assert not os.path.exists(os.path.join(tmpdir, "file_0.jpg"))


def test_apply_retention_by_days_and_size_both_applied():
    """apply_retention applies both retention_days and retention_max_gb when set."""
    import time

    from app.archiver import apply_retention

    with tempfile.TemporaryDirectory() as tmpdir:
        # One old file, two recent
        old_path = os.path.join(tmpdir, "old.jpg")
        with open(old_path, "wb") as fh:
            fh.write(b"x" * 1000)
        old_mtime = time.time() - (3 * 86400)
        os.utime(old_path, (old_mtime, old_mtime))

        for i in range(2):
            fpath = os.path.join(tmpdir, f"new_{i}.jpg")
            with open(fpath, "wb") as fh:
                fh.write(b"x" * 1000)

        config = {
            "archive": {
                "output_dir": tmpdir,
                "retention_days": 2,
                "retention_max_gb": 0.000001,  # ~1KB - will remove all
            },
        }
        deleted = apply_retention(config)

        assert deleted >= 1
        assert not os.path.exists(old_path)


def test_run_archive_applies_retention_including_max_gb():
    """run_archive calls apply_retention with config including retention_max_gb."""
    from app.archiver import apply_retention, run_archive

    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "source": {
                "base_url": "https://aviationwx.org",
                "airports_api_url": "https://api.example.com/v1/airports",
                "request_timeout": 5,
                "max_retries": 1,
                "retry_delay": 0,
            },
            "archive": {
                "output_dir": tmpdir,
                "retention_days": 7,
                "retention_max_gb": 50,
            },
            "airports": {"archive_all": False, "selected": ["KSPB"]},
        }

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.ok = True
            resp.raise_for_status.return_value = None
            if "airports" in url and "webcams" not in url:
                resp.json.return_value = {"airports": [{"code": "KSPB"}]}
            elif "webcams" in url:
                resp.json.return_value = {"webcams": []}
                resp.text = "<html></html>"
            else:
                resp.headers = {"content-type": "image/jpeg"}
                resp.content = b"\xff\xd8\xff"
            return resp

        with patch("app.archiver.requests.get", side_effect=mock_get):
            with patch("app.archiver.apply_retention") as mock_retention:
                mock_retention.return_value = 0
                run_archive(config)

        mock_retention.assert_called_once()
        call_config = mock_retention.call_args[0][0]
        assert call_config["archive"]["retention_max_gb"] == 50
        assert call_config["archive"]["retention_days"] == 7


def test_apply_retention_logs_warning_on_remove_failure():
    """apply_retention logs warning when file removal fails."""
    from app.archiver import apply_retention

    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "old.jpg")
        with open(fpath, "wb") as fh:
            fh.write(b"data")

        config = {"archive": {"output_dir": tmpdir, "retention_days": 365}}

        with patch(
            "app.archiver.os.remove",
            side_effect=OSError(13, "Permission denied"),
        ):
            with patch("app.archiver.os.path.getmtime", return_value=0):
                with patch("app.archiver.logger") as mock_logger:
                    deleted = apply_retention(config)

        assert deleted == 0
        mock_logger.warning.assert_called()
        assert "Retention" in str(mock_logger.warning.call_args)
        assert "failed" in str(mock_logger.warning.call_args).lower()


# ---------------------------------------------------------------------------
# Web GUI tests
# ---------------------------------------------------------------------------


@pytest.fixture
def flask_client():
    """Create a Flask test client with a minimal valid config."""
    import copy

    from app.config import DEFAULT_CONFIG
    from app.web import app as flask_app

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["airports"]["selected"] = ["KSPB"]  # Valid: at least one airport
    flask_app.config["ARCHIVER_CONFIG"] = config
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as client:
        yield client


def test_disk_usage_included_when_output_dir_exists():
    """archive_stats includes disk_usage when output_dir exists and is accessible."""
    from app.web import _archive_stats

    with tempfile.TemporaryDirectory() as tmpdir:
        stats = _archive_stats(tmpdir)
    assert "disk_usage" in stats
    assert stats["disk_usage"] is not None
    du = stats["disk_usage"]
    assert "used_gb" in du
    assert "total_gb" in du
    assert "free_gb" in du
    assert "percent_used" in du
    assert "used_fmt" in du
    assert "total_fmt" in du
    assert "unit" in du


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
    if data.get("disk_usage"):
        du = data["disk_usage"]
        assert "used_gb" in du
        assert "total_gb" in du
        assert "free_gb" in du
        assert "used_fmt" in du
        assert "unit" in du


def test_browse_returns_200(flask_client):
    resp = flask_client.get("/browse")
    assert resp.status_code == 200


def test_config_page_returns_200(flask_client):
    resp = flask_client.get("/config")
    assert resp.status_code == 200
    assert b"Configuration" in resp.data


def test_trigger_archive_redirects(flask_client):
    """Valid config: trigger redirects to dashboard."""
    resp = flask_client.post("/run")
    assert resp.status_code == 302
    loc = resp.headers.get("Location", "")
    assert loc.endswith("/") or "dashboard" in loc


def test_trigger_archive_invalid_config_redirects_to_config():
    """Invalid config: trigger redirects to config page."""
    from app.config import DEFAULT_CONFIG
    from app.web import app as flask_app

    config = dict(DEFAULT_CONFIG)
    config["airports"] = {"archive_all": False, "selected": []}  # Invalid
    flask_app.config["ARCHIVER_CONFIG"] = config
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as client:
        resp = client.post("/run")
    assert resp.status_code == 302
    assert "config" in resp.headers.get("Location", "")
