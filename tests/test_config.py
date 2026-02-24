"""
Tests for app.config — configuration loading, validation, and host resource checks.
"""

import os
import tempfile
from unittest.mock import patch

from app.config import check_host_resources, load_config

# ---------------------------------------------------------------------------
# Environment variable config override tests
# ---------------------------------------------------------------------------


def test_load_config_env_var_overrides_output_dir():
    """ARCHIVER_ARCHIVE_OUTPUT_DIR overrides config file and defaults."""
    with patch.dict(
        os.environ,
        {"ARCHIVER_ARCHIVE_OUTPUT_DIR": "/custom/archive"},
        clear=False,
    ):
        config = load_config("/nonexistent/config.yaml")
    assert config["archive"]["output_dir"] == "/custom/archive"


def test_load_config_env_var_overrides_interval_minutes():
    """ARCHIVER_SCHEDULE_INTERVAL_MINUTES overrides defaults."""
    with patch.dict(
        os.environ,
        {"ARCHIVER_SCHEDULE_INTERVAL_MINUTES": "30"},
        clear=False,
    ):
        config = load_config("/nonexistent/config.yaml")
    assert config["schedule"]["interval_minutes"] == 30


def test_load_config_env_var_overrides_airports_selected():
    """ARCHIVER_AIRPORTS_SELECTED accepts comma-separated list."""
    with patch.dict(
        os.environ,
        {"ARCHIVER_AIRPORTS_SELECTED": "KSPB,KAWO,KPWT"},
        clear=False,
    ):
        config = load_config("/nonexistent/config.yaml")
    assert config["airports"]["selected"] == ["KSPB", "KAWO", "KPWT"]


def test_load_config_env_var_overrides_archive_all():
    """ARCHIVER_AIRPORTS_ARCHIVE_ALL accepts true/false/1/0."""
    with patch.dict(
        os.environ,
        {"ARCHIVER_AIRPORTS_ARCHIVE_ALL": "true"},
        clear=False,
    ):
        config = load_config("/nonexistent/config.yaml")
    assert config["airports"]["archive_all"] is True


def test_load_config_env_var_overrides_config_file():
    """Env vars override values from config file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
        tmp.write("schedule:\n  interval_minutes: 15\n")
        tmp_path = tmp.name
    try:
        with patch.dict(
            os.environ,
            {"ARCHIVER_SCHEDULE_INTERVAL_MINUTES": "45"},
            clear=False,
        ):
            config = load_config(tmp_path)
        assert config["schedule"]["interval_minutes"] == 45
    finally:
        os.unlink(tmp_path)


def test_load_config_env_var_api_key():
    """ARCHIVER_SOURCE_API_KEY sets Partner API key."""
    with patch.dict(
        os.environ,
        {"ARCHIVER_SOURCE_API_KEY": "secret-key-123"},
        clear=False,
    ):
        config = load_config("/nonexistent/config.yaml")
    assert config["source"]["api_key"] == "secret-key-123"


def test_load_config_env_var_ignores_unknown_prefix():
    """Env vars without ARCHIVER_ prefix are ignored."""
    with patch.dict(
        os.environ,
        {"OTHER_VAR": "value", "ARCHIVER_SCHEDULE_INTERVAL_MINUTES": "20"},
        clear=False,
    ):
        config = load_config("/nonexistent/config.yaml")
    assert config["schedule"]["interval_minutes"] == 20


def test_load_config_env_var_overrides_retention_days():
    """ARCHIVER_ARCHIVE_RETENTION_DAYS overrides defaults."""
    with patch.dict(
        os.environ,
        {"ARCHIVER_ARCHIVE_RETENTION_DAYS": "14"},
        clear=False,
    ):
        config = load_config("/nonexistent/config.yaml")
    assert config["archive"]["retention_days"] == 14


def test_load_config_env_var_overrides_log_level():
    """ARCHIVER_LOGGING_LEVEL overrides defaults."""
    with patch.dict(
        os.environ,
        {"ARCHIVER_LOGGING_LEVEL": "DEBUG"},
        clear=False,
    ):
        config = load_config("/nonexistent/config.yaml")
    assert config["logging"]["level"] == "DEBUG"


def test_load_config_env_var_overrides_web_enabled():
    """ARCHIVER_WEB_ENABLED=false disables web UI."""
    with patch.dict(
        os.environ,
        {"ARCHIVER_WEB_ENABLED": "false"},
        clear=False,
    ):
        config = load_config("/nonexistent/config.yaml")
    assert config["web"]["enabled"] is False


def test_check_host_resources_logs_debug_when_output_dir_write_test_unlink_fails():
    """check_host_resources logs debug when write-test file cannot be removed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = {"archive": {"output_dir": tmpdir}}
        config_path = os.path.join(tmpdir, "config.yaml")

        with patch(
            "app.config.os.unlink", side_effect=OSError(13, "Permission denied")
        ):
            with patch("app.config.logger") as mock_logger:
                check_host_resources(config, config_path=config_path)

        mock_logger.debug.assert_called()
        assert "write-test" in str(mock_logger.debug.call_args).lower()


def test_check_host_resources_logs_warning_when_config_file_unreadable():
    """check_host_resources logs warning when config file exists but cannot be read."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = os.path.join(tmpdir, "config.yaml")
        with open(config_path, "w") as fh:
            fh.write("archive: {}")

        config = {"archive": {"output_dir": tmpdir}}

        with patch(
            "builtins.open",
            side_effect=OSError(13, "Permission denied"),
        ):
            with patch("app.config.os.path.isfile", return_value=True):
                with patch("app.config.logger") as mock_logger:
                    check_host_resources(config, config_path=config_path)

        mock_logger.warning.assert_called()
        assert any(
            "cannot be read" in str(c).lower() or "permission" in str(c).lower()
            for c in mock_logger.warning.call_args_list
        )


def test_check_host_resources_logs_debug_when_config_dir_write_test_unlink_fails():
    """check_host_resources logs debug when config write-test file cannot be removed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = os.path.join(tmpdir, "config.yaml")
        config = {"archive": {"output_dir": ""}}

        with patch("app.config.os.path.isfile", return_value=False):
            with patch("app.config.logger") as mock_logger:
                with patch(
                    "app.config.os.unlink",
                    side_effect=OSError(13, "Permission denied"),
                ):
                    check_host_resources(config, config_path=config_path)

        mock_logger.debug.assert_called()
        assert any(
            "config" in str(c).lower() or "write" in str(c).lower()
            for c in mock_logger.debug.call_args_list
        )


def test_check_host_resources_logs_warning_when_config_dir_not_writable():
    """check_host_resources logs warning when config directory cannot be written to."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = os.path.join(tmpdir, "subdir", "config.yaml")
        config = {"archive": {"output_dir": tmpdir}}

        with patch("app.config.os.path.isfile", return_value=False):
            with patch("app.config.os.path.dirname", return_value=tmpdir):
                with patch("app.config.os.path.isdir", return_value=True):
                    with patch(
                        "builtins.open",
                        side_effect=OSError(13, "Permission denied"),
                    ):
                        with patch("app.config.logger") as mock_logger:
                            check_host_resources(config, config_path=config_path)

        mock_logger.warning.assert_called()
        assert any(
            "not writable" in str(c).lower() for c in mock_logger.warning.call_args_list
        )
