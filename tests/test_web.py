"""
Tests for app.web — Flask routes, helpers, and form handling.
"""

import copy
import json
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
    _format_size_in_unit,
    _parse_timestamp_from_filename,
    _pick_display_unit,
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


def test_parse_timestamp_from_filename_unix_format():
    """_parse_timestamp_from_filename parses Unix timestamp format."""
    result = _parse_timestamp_from_filename("1718456780_0.jpg")
    assert result is not None
    assert "2024-06" in result
    assert " UTC" in result


def test_parse_timestamp_from_filename_date_time_format():
    """_parse_timestamp_from_filename parses YYYYMMDD_HHMMSS format."""
    result = _parse_timestamp_from_filename("20240615_143000_webcam.jpg")
    assert result == "2024-06-15 14:30:00 UTC"


def test_parse_timestamp_from_filename_returns_none_for_unparseable():
    """_parse_timestamp_from_filename returns None for non-matching filenames."""
    assert _parse_timestamp_from_filename("random_file.jpg") is None
    assert _parse_timestamp_from_filename("single_part") is None


def test_archive_tree_returns_empty_when_output_dir_missing():
    """_archive_tree returns empty dict when output_dir does not exist."""
    result = _archive_tree("/nonexistent/path/12345")
    assert result == {}


def test_archive_tree_respects_browse_airport_limit():
    """_archive_tree limits top-level airports when browse_airport_limit is set."""
    with tempfile.TemporaryDirectory() as tmpdir:
        for code in ("KAAA", "KBBB", "KCCC"):
            path = os.path.join(tmpdir, code, "2024", "06", "15", "cam")
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "a.jpg"), "wb") as fh:
                fh.write(b"x")
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["web"]["browse_airport_limit"] = 1
        tree = _archive_tree(tmpdir, cfg)

    assert len(tree) == 1
    assert "KAAA" in tree


def test_archive_tree_builds_nested_structure_from_directory():
    """_archive_tree builds airport/year/month/day/camera structure from archive."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "KSPB", "2024", "06", "15", "north_runway")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "image.jpg"), "wb") as fh:
            fh.write(b"data")

        tree = _archive_tree(tmpdir)

    assert "KSPB" in tree
    assert "2024" in tree["KSPB"]
    assert "06" in tree["KSPB"]["2024"]
    assert "15" in tree["KSPB"]["2024"]["06"]
    assert "north_runway" in tree["KSPB"]["2024"]["06"]["15"]
    assert "image.jpg" in tree["KSPB"]["2024"]["06"]["15"]["north_runway"]


def test_archive_tree_uses_index_when_present():
    """_archive_tree uses index when valid for fast browse (no scandir walk)."""
    from app.archiver import _save_archive_index

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "KSPB", "2024", "06", "15", "north_runway")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "image.jpg"), "wb") as fh:
            fh.write(b"data")
        rel = os.path.relpath(os.path.join(path, "image.jpg"), tmpdir).replace(
            "\\", "/"
        )
        index_data = {
            "version": 1,
            "files": {rel: {"mtime": 1234567890.0, "size": 4}},
        }
        _save_archive_index(tmpdir, index_data)

        tree = _archive_tree(tmpdir)

    assert "KSPB" in tree
    assert tree["KSPB"]["2024"]["06"]["15"]["north_runway"] == ["image.jpg"]


def test_archive_tree_ignores_non_digit_directories():
    """_archive_tree skips year/month/day directories that are not digits."""
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(
            os.path.join(tmpdir, "KSPB", "2024", "xx", "15", "cam"),
            exist_ok=True,
        )
        os.makedirs(
            os.path.join(tmpdir, "KAWO", "abcd", "06", "15", "cam"),
            exist_ok=True,
        )

        tree = _archive_tree(tmpdir)

    assert "KSPB" in tree
    assert "KAWO" in tree
    assert tree["KSPB"]["2024"] == {}
    assert "abcd" not in tree["KAWO"]


def test_disk_usage_returns_dict_when_path_valid():
    """_disk_usage returns raw and formatted disk usage when path exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _disk_usage(tmpdir)

    assert result is not None
    assert "used_gb" in result
    assert "total_gb" in result
    assert "free_gb" in result
    assert "percent_used" in result
    assert "used_fmt" in result
    assert "free_fmt" in result
    assert "total_fmt" in result
    assert "unit" in result
    assert result["unit"] in ("GB", "TB", "PB")
    assert isinstance(result["used_gb"], (int, float))
    assert isinstance(result["total_gb"], (int, float))


def test_disk_usage_returns_none_on_oserror():
    """_disk_usage returns None when disk_usage raises OSError."""
    with patch("app.web.shutil.disk_usage", side_effect=OSError(2, "No such file")):
        result = _disk_usage("/nonexistent")
    assert result is None


def test_pick_display_unit_returns_gb_for_small_disks():
    """_pick_display_unit returns GB when total < 1 TiB."""
    assert _pick_display_unit(500 * (1024**3)) == "GB"
    assert _pick_display_unit((1024**4) - 1) == "GB"


def test_pick_display_unit_returns_tb_for_disks_over_1tib():
    """_pick_display_unit returns TB when total >= 1 TiB."""
    assert _pick_display_unit(1024**4) == "TB"
    assert _pick_display_unit(2 * (1024**4)) == "TB"
    assert _pick_display_unit((1024**5) - 1) == "TB"


def test_pick_display_unit_returns_pb_for_disks_over_1pib():
    """_pick_display_unit returns PB when total >= 1 PiB."""
    assert _pick_display_unit(1024**5) == "PB"
    assert _pick_display_unit(2 * (1024**5)) == "PB"


def test_format_size_in_unit_converts_correctly():
    """_format_size_in_unit converts bytes to GB, TB, PB."""
    one_gib = 1024**3
    one_tib = 1024**4
    one_pib = 1024**5
    assert _format_size_in_unit(one_gib, "GB") == 1.0
    assert _format_size_in_unit(one_tib, "TB") == 1.0
    assert _format_size_in_unit(one_pib, "PB") == 1.0
    assert _format_size_in_unit(2 * one_gib, "GB") == 2.0


def test_disk_usage_uses_gb_for_small_disks():
    """_disk_usage uses GB unit when total < 1 TiB."""
    usage = type("Usage", (), {})()
    usage.total = 500 * (1024**3)  # 500 GiB
    usage.used = 100 * (1024**3)
    usage.free = 400 * (1024**3)

    with patch("app.web.shutil.disk_usage", return_value=usage):
        result = _disk_usage("/some/path")

    assert result is not None
    assert result["unit"] == "GB"


def test_disk_usage_uses_tb_for_large_disks():
    """_disk_usage uses TB unit when total >= 1 TiB."""
    usage = type("Usage", (), {})()
    usage.total = 2 * (1024**4)
    usage.used = 1 * (1024**4)
    usage.free = 1 * (1024**4)

    with patch("app.web.shutil.disk_usage", return_value=usage):
        result = _disk_usage("/some/path")

    assert result is not None
    assert result["unit"] == "TB"
    assert result["total_fmt"] == "2.00"
    assert result["used_fmt"] == "1.00"
    assert result["free_fmt"] == "1.00"


def test_disk_usage_uses_pb_for_petabyte_disks():
    """_disk_usage uses PB unit when total >= 1 PiB."""
    usage = type("Usage", (), {})()
    usage.total = 2 * (1024**5)
    usage.used = 1 * (1024**5)
    usage.free = 1 * (1024**5)

    with patch("app.web.shutil.disk_usage", return_value=usage):
        result = _disk_usage("/some/path")

    assert result is not None
    assert result["unit"] == "PB"
    assert result["total_fmt"] == "2.00"


def test_disk_usage_formatted_values_include_thousand_separators():
    """_disk_usage formats values >= 1000 with thousand separators."""
    usage = type("Usage", (), {})()
    # 1500 TiB: >= 1 TiB, and 1500 >= 1024 so >= 1 PiB -> unit is PB
    # Use 1500 PiB to get comma in PB unit
    usage.total = 1500 * (1024**5)
    usage.used = 900 * (1024**5)
    usage.free = 600 * (1024**5)

    with patch("app.web.shutil.disk_usage", return_value=usage):
        result = _disk_usage("/some/path")

    assert result is not None
    assert result["unit"] == "PB"
    assert "1,500.00" in result["total_fmt"]


def test_archive_stats_returns_empty_when_output_dir_missing():
    """_archive_stats returns zeros when output_dir does not exist."""
    result = _archive_stats("/nonexistent/path")
    assert result["total_files"] == 0
    assert result["total_size_gb"] == 0.0
    assert result["airports"] == []
    assert "disk_usage" in result


def test_archive_stats_counts_files_and_airports():
    """_archive_stats counts files, size, airports (uses index or scandir fallback)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path1 = os.path.join(tmpdir, "KSPB", "2024", "06", "15", "cam_a")
        path2 = os.path.join(tmpdir, "KAWO", "2024", "06", "15", "cam_b")
        os.makedirs(path1, exist_ok=True)
        os.makedirs(path2, exist_ok=True)
        with open(os.path.join(path1, "a.jpg"), "wb") as fh:
            fh.write(b"x" * (1024 * 1024))
        with open(os.path.join(path2, "b.jpg"), "wb") as fh:
            fh.write(b"y" * (512 * 1024))

        stats = _archive_stats(tmpdir)

    assert stats["total_files"] == 2
    assert stats["total_size_gb"] >= 0.001  # 1.5 MB = ~0.0015 GB
    assert set(stats["airports"]) == {"KSPB", "KAWO"}


def test_archive_stats_uses_index_when_present():
    """_archive_stats uses index when available and spot-check passes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path1 = os.path.join(tmpdir, "KSPB", "2024", "06", "15", "a.jpg")
        path2 = os.path.join(tmpdir, "KAWO", "2024", "06", "15", "b.jpg")
        os.makedirs(os.path.dirname(path1), exist_ok=True)
        os.makedirs(os.path.dirname(path2), exist_ok=True)
        with open(path1, "wb") as fh:
            fh.write(b"x" * (1024 * 1024))
        with open(path2, "wb") as fh:
            fh.write(b"y" * (512 * 1024))

        idx_path = os.path.join(tmpdir, ".archive_index.json")
        rel1 = os.path.relpath(path1, tmpdir)
        rel2 = os.path.relpath(path2, tmpdir)
        index_data = {
            "version": 1,
            "files": {
                rel1: {"mtime": 1718456780.0, "size": 1024 * 1024},
                rel2: {"mtime": 1718456780.0, "size": 512 * 1024},
            },
        }
        with open(idx_path, "w") as fh:
            json.dump(index_data, fh)

        stats = _archive_stats(tmpdir)

    assert stats["total_files"] == 2
    assert stats["total_size_gb"] >= 0.001  # 1.5 MB = ~0.0015 GB
    assert set(stats["airports"]) == {"KSPB", "KAWO"}


def test_archive_stats_returns_zero_when_scan_yields_no_files():
    """_archive_stats returns zeros when scandir fallback yields no files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "KSPB", "2024", "06", "15", "north_runway")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "a.jpg"), "wb") as fh:
            fh.write(b"data")

        def empty_scandir(*args, **kwargs):
            yield from ()

        with (
            patch(
                "app.archiver._load_archive_index",
                return_value=None,
            ),
            patch(
                "app.archiver._scandir_walk_files",
                side_effect=empty_scandir,
            ),
        ):
            stats = _archive_stats(tmpdir)

    assert stats["total_files"] == 0
    assert stats["total_size_gb"] == 0.0


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
        "retention_max_gb": "0",
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
        "retention_max_gb": "0",
        "selected_airports": "KSPB",
        "log_level": "INFO",
    }.get(k, d)
    form.__contains__ = lambda self, k: k in ["fetch_on_start"]

    with pytest.raises(ValueError, match="output_dir"):
        _form_to_config(form, DEFAULT_CONFIG)


def test_form_to_config_raises_when_retention_max_gb_negative():
    """_form_to_config raises ValueError when retention_max_gb < 0."""
    form = MagicMock()
    form.get.side_effect = lambda k, d=None: {
        "interval_minutes": "15",
        "output_dir": "/archive",
        "retention_days": "0",
        "retention_max_gb": "-1",
        "selected_airports": "KSPB",
        "log_level": "INFO",
    }.get(k, d)
    form.__contains__ = lambda self, k: k in ["fetch_on_start"]

    with pytest.raises(ValueError, match="retention_max_gb"):
        _form_to_config(form, DEFAULT_CONFIG)


def test_form_to_config_raises_when_retention_negative():
    """_form_to_config raises ValueError when retention_days < 0."""
    form = MagicMock()
    form.get.side_effect = lambda k, d=None: {
        "interval_minutes": "15",
        "output_dir": "/archive",
        "retention_days": "-1",
        "retention_max_gb": "0",
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
        "retention_max_gb": "0",
        "selected_airports": "KSPB",
        "log_level": "INVALID",
    }.get(k, d)
    form.__contains__ = lambda self, k: k in ["fetch_on_start"]

    with pytest.raises(ValueError, match="Invalid log level"):
        _form_to_config(form, DEFAULT_CONFIG)


def test_form_to_config_sets_retention_max_gb():
    """_form_to_config sets archive.retention_max_gb from form."""
    form = MagicMock()
    form.get.side_effect = lambda k, d=None: {
        "interval_minutes": "15",
        "output_dir": "/archive",
        "retention_days": "30",
        "retention_max_gb": "100",
        "selected_airports": "KSPB",
        "log_level": "INFO",
    }.get(k, d)
    form.__contains__ = lambda self, k: k in ["fetch_on_start"]

    config = _form_to_config(form, DEFAULT_CONFIG)
    assert config["archive"]["retention_max_gb"] == 100.0


def test_form_to_config_retention_max_gb_empty_becomes_zero():
    """_form_to_config treats empty retention_max_gb as 0."""
    form = MagicMock()
    form.get.side_effect = lambda k, d=None: {
        "interval_minutes": "15",
        "output_dir": "/archive",
        "retention_days": "0",
        "retention_max_gb": "",
        "selected_airports": "KSPB",
        "log_level": "INFO",
    }.get(k, d)
    form.__contains__ = lambda self, k: k in ["fetch_on_start"]

    config = _form_to_config(form, DEFAULT_CONFIG)
    assert config["archive"]["retention_max_gb"] == 0.0


def test_form_to_config_sets_api_key_when_provided():
    """_form_to_config sets source.api_key when form provides non-empty value."""
    form = MagicMock()
    form.get.side_effect = lambda k, d=None: {
        "interval_minutes": "15",
        "output_dir": "/archive",
        "retention_days": "0",
        "retention_max_gb": "0",
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
                "retention_max_gb": "0",
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
            "retention_max_gb": "0",
            "selected_airports": "KSPB",
            "log_level": "INFO",
        },
    )

    assert resp.status_code == 200
    assert b"Invalid" in resp.data or b"error" in resp.data.lower()


def test_configuration_post_rejects_root_output_dir(flask_client):
    """POST to /config rejects output_dir that is root or contains path traversal."""
    for bad_dir in ("/", "\\", "/archive/../etc"):
        resp = flask_client.post(
            "/config",
            data={
                "interval_minutes": "15",
                "output_dir": bad_dir,
                "retention_days": "0",
                "retention_max_gb": "0",
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
    """GET /browse returns 200 with browse shell (lazy-loaded tree)."""
    resp = flask_client.get("/browse")
    assert resp.status_code == 200
    assert b"Browse" in resp.data or b"browse" in resp.data.lower()


def test_browse_includes_preview_and_clickable_files(flask_client):
    """Browse shell has preview panel; file list comes from browse files API."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    orig_dir = config["archive"]["output_dir"]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "KSPB", "2024", "06", "15", "north_runway")
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "image.jpg"), "wb") as fh:
                fh.write(b"x")
            config["archive"]["output_dir"] = tmpdir
            flask_app.config["ARCHIVER_CONFIG"] = config

            resp = flask_client.get("/browse")
            api = flask_client.get(
                "/api/browse/files?path=KSPB/2024/06/15/north_runway"
            )

        assert resp.status_code == 200
        assert b"preview-panel" in resp.data
        assert b"browse-root" in resp.data
        assert b"preview-download-btn" in resp.data
        assert b"prev-btn" in resp.data
        assert b"next-btn" in resp.data
        assert api.status_code == 200
        rows = api.get_json()["files"]
        assert any(r["name"] == "image.jpg" for r in rows)
    finally:
        config["archive"]["output_dir"] = orig_dir


def test_browse_files_api_preview_images_metadata(flask_client):
    """Browse files API returns preview_images entries with path and index."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    orig_dir = config["archive"]["output_dir"]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "KSPB", "2024", "06", "15", "north_runway")
            os.makedirs(path, exist_ok=True)
            for i in range(3):
                with open(os.path.join(path, f"171845678{i}_0.jpg"), "wb") as fh:
                    fh.write(b"x")
            config["archive"]["output_dir"] = tmpdir
            flask_app.config["ARCHIVER_CONFIG"] = config

            api = flask_client.get(
                "/api/browse/files?path=KSPB/2024/06/15/north_runway"
            )

        assert api.status_code == 200
        data = api.get_json()
        prev = data["preview_images"]
        assert len(prev) == 3
        assert all("path" in s and "filename" in s and "index" in s for s in prev)
    finally:
        config["archive"]["output_dir"] = orig_dir


def test_browse_includes_time_utc_column(flask_client):
    """Browse page script builds tables with Time (UTC) column; API returns times."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    orig_dir = config["archive"]["output_dir"]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "KSPB", "2024", "06", "15", "north_runway")
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "20240615_143000_webcam.jpg"), "wb") as fh:
                fh.write(b"x")
            config["archive"]["output_dir"] = tmpdir
            flask_app.config["ARCHIVER_CONFIG"] = config

            resp = flask_client.get("/browse")
            api = flask_client.get(
                "/api/browse/files?path=KSPB/2024/06/15/north_runway"
            )

        assert resp.status_code == 200
        assert b"Time (UTC)" in resp.data
        assert api.status_code == 200
        rows = api.get_json()["files"]
        assert rows and "UTC" in rows[0].get("time_utc", "")
    finally:
        config["archive"]["output_dir"] = orig_dir


def test_serve_archive_file_returns_image(flask_client):
    """GET /archive/<path> serves file with correct Content-Type and body."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    orig_dir = config["archive"]["output_dir"]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "KSPB", "2024", "06", "15", "north_runway")
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "test.jpg"), "wb") as fh:
                fh.write(b"\xff\xd8\xfffake-jpeg")
            config["archive"]["output_dir"] = tmpdir
            flask_app.config["ARCHIVER_CONFIG"] = config

            resp = flask_client.get("/archive/KSPB/2024/06/15/north_runway/test.jpg")

        assert resp.status_code == 200
        assert resp.content_type and "image" in resp.content_type
        assert resp.data == b"\xff\xd8\xfffake-jpeg"
    finally:
        config["archive"]["output_dir"] = orig_dir


def test_serve_archive_file_via_symlinked_output_dir(flask_client):
    """Regression when output_dir is a symlink: serve via resolved canonical path."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    orig_dir = config["archive"]["output_dir"]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            backing = os.path.join(tmpdir, "backing")
            path = os.path.join(backing, "KSPB", "2024", "06", "15", "north_runway")
            os.makedirs(path, exist_ok=True)
            content = b"symlink-archive-bytes\n"
            with open(os.path.join(path, "via_link.jpg"), "wb") as fh:
                fh.write(content)
            link_root = os.path.join(tmpdir, "archive_link")
            try:
                os.symlink(backing, link_root, target_is_directory=True)
            except OSError:
                pytest.skip("Filesystem does not support symlinked directories")
            config["archive"]["output_dir"] = link_root
            flask_app.config["ARCHIVER_CONFIG"] = config

            resp = flask_client.get(
                "/archive/KSPB/2024/06/15/north_runway/via_link.jpg"
            )

        assert resp.status_code == 200
        assert resp.data == content
    finally:
        config["archive"]["output_dir"] = orig_dir


def test_serve_archive_file_404_symlink_inside_archive_escapes_root(flask_client):
    """Symlink under output_dir resolving outside canonical root yields 404."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    orig_dir = config["archive"]["output_dir"]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            outer = os.path.join(tmpdir, "outside_archive")
            os.makedirs(outer, exist_ok=True)
            rogue = os.path.join(outer, "rogue.bin")
            with open(rogue, "wb") as fh:
                fh.write(b"should-not-leak")

            archive_root = os.path.join(tmpdir, "archive")
            os.makedirs(archive_root, exist_ok=True)
            leap = os.path.join(archive_root, "escape")
            parent = os.path.dirname(archive_root)
            target = os.path.join(parent, "outside_archive")
            try:
                os.symlink(target, leap)
            except OSError:
                pytest.skip("Filesystem does not support symlinks")

            config["archive"]["output_dir"] = archive_root
            flask_app.config["ARCHIVER_CONFIG"] = config

            resp = flask_client.get("/archive/escape/rogue.bin")

        assert resp.status_code == 404
    finally:
        config["archive"]["output_dir"] = orig_dir


def test_serve_archive_file_404_when_file_missing(flask_client):
    """GET /archive/<path> returns 404 when file does not exist."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    orig_dir = config["archive"]["output_dir"]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(
                os.path.join(tmpdir, "KSPB", "2024", "06", "15", "north_runway"),
                exist_ok=True,
            )
            config["archive"]["output_dir"] = tmpdir
            flask_app.config["ARCHIVER_CONFIG"] = config

            resp = flask_client.get(
                "/archive/KSPB/2024/06/15/north_runway/nonexistent.jpg"
            )

        assert resp.status_code == 404
    finally:
        config["archive"]["output_dir"] = orig_dir


def test_serve_archive_file_404_for_path_traversal(flask_client):
    """GET /archive with path traversal returns 404."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    orig_dir = config["archive"]["output_dir"]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            config["archive"]["output_dir"] = tmpdir
            flask_app.config["ARCHIVER_CONFIG"] = config

            resp = flask_client.get("/archive/../../../etc/passwd")

        assert resp.status_code == 404
    finally:
        config["archive"]["output_dir"] = orig_dir


def test_serve_archive_file_404_when_output_dir_is_root(flask_client):
    """GET /archive returns 404 when output_dir is / (prevents serving system files)."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    orig_dir = config["archive"]["output_dir"]
    try:
        config["archive"]["output_dir"] = "/"
        flask_app.config["ARCHIVER_CONFIG"] = config

        resp = flask_client.get("/archive/etc/passwd")

        assert resp.status_code == 404
    finally:
        config["archive"]["output_dir"] = orig_dir


def test_serve_archive_file_404_for_invalid_subpath(flask_client):
    """GET /archive rejects invalid subpaths (empty segments, dot segments)."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    orig_dir = config["archive"]["output_dir"]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            config["archive"]["output_dir"] = tmpdir
            flask_app.config["ARCHIVER_CONFIG"] = config

            bad_paths = ["KSPB//2024/image.jpg", "KSPB/./2024/image.jpg"]
            for bad_path in bad_paths:
                resp = flask_client.get(f"/archive/{bad_path}")
                assert resp.status_code == 404, f"Expected 404 for {bad_path!r}"
    finally:
        config["archive"]["output_dir"] = orig_dir


def test_api_status_includes_version_and_git_sha(flask_client):
    """GET /api/status includes version and git_sha in response."""
    resp = flask_client.get("/api/status")
    data = resp.get_json()
    assert "version" in data
    assert data["version"]  # Non-empty version string
    assert "git_sha" in data


def test_api_health_returns_minimal_json(flask_client):
    """GET /api/health returns ok without heavy work."""
    resp = flask_client.get("/api/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {"status": "ok"}


def test_api_status_light_skips_archive_stats(flask_client):
    """GET /api/status?light=1 does not call _archive_stats."""
    with patch("app.web._archive_stats") as mock_stats:
        resp = flask_client.get("/api/status?light=1")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["archive"].get("light") is True
    mock_stats.assert_not_called()


def test_dashboard_accepts_string_slow_request_threshold(flask_client):
    """Quoted YAML web.slow_request_log_seconds must not raise during requests."""
    cfg = flask_app.config["ARCHIVER_CONFIG"]
    prev = cfg["web"].get("slow_request_log_seconds")
    try:
        cfg["web"]["slow_request_log_seconds"] = "2.5"
        flask_app.config["ARCHIVER_CONFIG"] = cfg
        resp = flask_client.get("/")
    finally:
        if prev is not None:
            cfg["web"]["slow_request_log_seconds"] = prev
        else:
            cfg["web"].pop("slow_request_log_seconds", None)
        flask_app.config["ARCHIVER_CONFIG"] = cfg

    assert resp.status_code == 200


def test_dashboard_footer_shows_version(flask_client):
    """Dashboard page includes version in footer."""
    resp = flask_client.get("/")
    assert resp.status_code == 200
    # Version is either semantic (v0.x.x) or fallback when undetectable
    assert b"v0." in resp.data or b"0." in resp.data or b"Unknown Version" in resp.data


def test_dashboard_footer_shows_single_version_no_duplicate(flask_client):
    """Footer shows version once; no duplicate version or git sha in footer."""
    resp = flask_client.get("/")
    assert resp.status_code == 200
    text = resp.data.decode("utf-8")
    # Footer format: AviationWX.org Archiver vX.X.X — Part of... MIT License
    assert "AviationWX.org Archiver" in text
    assert "Part of the" in text
    assert "AviationWX.org" in text
    assert "MIT License" in text
    # Version should appear exactly once in footer (not v0.2.0 v0.2.0)
    footer_start = text.find("<footer>")
    footer_end = text.find("</footer>")
    assert footer_start >= 0 and footer_end >= 0
    footer = text[footer_start:footer_end]
    version_count = footer.count(VERSION)
    assert version_count == 1, f"Expected version once in footer, found {version_count}"


def test_dashboard_disk_usage_shows_unit_when_present(flask_client):
    """Dashboard Disk Usage section shows formatted values with unit (GB/TB/PB)."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    with tempfile.TemporaryDirectory() as tmpdir:
        config["archive"]["output_dir"] = tmpdir
        flask_app.config["ARCHIVER_CONFIG"] = config

        resp = flask_client.get("/")

    assert resp.status_code == 200
    text = resp.data.decode("utf-8")
    assert "Disk Usage" in text
    # Should show unit (GB for small tmp dir)
    assert " GB</div>" in text or " TB</div>" in text or " PB</div>" in text


def test_api_status_includes_disk_usage_when_available(flask_client):
    """GET /api/status includes disk_usage with raw and formatted values."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    with tempfile.TemporaryDirectory() as tmpdir:
        config["archive"]["output_dir"] = tmpdir
        flask_app.config["ARCHIVER_CONFIG"] = config

        resp = flask_client.get("/api/status")

    data = resp.get_json()
    assert data["status"] == "ok"
    assert "disk_usage" in data
    assert data["disk_usage"] is not None
    du = data["disk_usage"]
    assert "used_gb" in du
    assert "total_gb" in du
    assert "used_fmt" in du
    assert "total_fmt" in du
    assert "unit" in du
    assert du["unit"] in ("GB", "TB", "PB")


# ---------------------------------------------------------------------------
# Browse API (lazy tree)
# ---------------------------------------------------------------------------


def test_api_browse_children_lists_airports(flask_client):
    """GET /api/browse/children returns airports from archive layout."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    orig = config["archive"]["output_dir"]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            cam = os.path.join(tmpdir, "KSEA", "2024", "01", "01", "north")
            os.makedirs(cam, exist_ok=True)
            with open(os.path.join(cam, "a.jpg"), "wb") as fh:
                fh.write(b"x")
            config["archive"]["output_dir"] = tmpdir
            flask_app.config["ARCHIVER_CONFIG"] = config

            resp = flask_client.get("/api/browse/children")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["level"] == "airports"
        assert data.get("archive_unavailable") is False
        assert any(x["name"] == "KSEA" for x in data["items"])
        assert (
            data["items"][0]["file_count"] is None
            or data["items"][0]["file_count"] >= 1
        )
    finally:
        config["archive"]["output_dir"] = orig
        flask_app.config["ARCHIVER_CONFIG"] = config


def test_api_browse_files_paginates(flask_client):
    """GET /api/browse/files returns paginated rows and preview metadata."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    orig = config["archive"]["output_dir"]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            cam = os.path.join(tmpdir, "KSEA", "2024", "01", "01", "north")
            os.makedirs(cam, exist_ok=True)
            for i in range(5):
                with open(os.path.join(cam, f"{i}.jpg"), "wb") as fh:
                    fh.write(b"x")
            config["archive"]["output_dir"] = tmpdir
            config["web"]["browse_page_size"] = 2
            flask_app.config["ARCHIVER_CONFIG"] = config

            r0 = flask_client.get("/api/browse/files?path=" + "KSEA/2024/01/01/north")
            r1 = flask_client.get(
                "/api/browse/files?path=" + "KSEA/2024/01/01/north&offset=2"
            )
        assert r0.status_code == 200
        d0 = r0.get_json()
        assert d0["total"] == 5
        assert len(d0["files"]) == 2
        assert d0["offset"] == 0
        d1 = r1.get_json()
        assert len(d1["files"]) == 2
        assert d1["offset"] == 2
        assert len(d1["preview_images"]) >= 1
        assert d0.get("archive_unavailable") is False
        assert d1.get("archive_unavailable") is False
    finally:
        config["archive"]["output_dir"] = orig
        config["web"].pop("browse_page_size", None)
        flask_app.config["ARCHIVER_CONFIG"] = config


def test_api_browse_files_rejects_short_path(flask_client):
    """GET /api/browse/files returns 400 when path is not five segments."""
    resp = flask_client.get("/api/browse/files?path=KSEA/2024")
    assert resp.status_code == 400


def test_api_browse_children_rejects_bad_path(flask_client):
    """GET /api/browse/children returns 400 for traversal-like path."""
    resp = flask_client.get("/api/browse/children?path=x/../y")
    assert resp.status_code == 400


def test_api_browse_children_rejects_five_segment_path(flask_client):
    """GET /api/browse/children returns 400 at camera depth (use /api/browse/files)."""
    resp = flask_client.get("/api/browse/children?path=KSEA/2024/01/01/north")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_api_browse_rejects_path_too_deep(flask_client):
    """More than five path segments: 400 with a stable error message (not str(exc))."""
    resp = flask_client.get("/api/browse/children?path=a/b/c/d/e/extra")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "error" in data
    assert "too many segments" in data["error"].lower()


def test_api_browse_archive_unavailable_when_output_dir_missing(flask_client):
    """Browse APIs set archive_unavailable when configured output_dir is missing."""
    config = flask_app.config["ARCHIVER_CONFIG"]
    orig = config["archive"]["output_dir"]
    try:
        config["archive"]["output_dir"] = "/nonexistent/archive/path/for/browse/test"
        flask_app.config["ARCHIVER_CONFIG"] = config
        r_children = flask_client.get("/api/browse/children")
        r_files = flask_client.get("/api/browse/files?path=KSEA/2024/01/01/north")
    finally:
        config["archive"]["output_dir"] = orig
        flask_app.config["ARCHIVER_CONFIG"] = config

    assert r_children.status_code == 200
    d1 = r_children.get_json()
    assert d1["archive_unavailable"] is True
    assert d1["items"] == []
    assert d1["path"] == ""

    assert r_files.status_code == 200
    d2 = r_files.get_json()
    assert d2["archive_unavailable"] is True
    assert d2["total"] == 0
    assert d2["files"] == []
    assert d2["path"] == "KSEA/2024/01/01/north"
    assert d2["preview_images"] == []
    assert d2["preview_truncated"] is False
