"""
Tests for NTP time verification.
"""

from unittest.mock import MagicMock, patch


def test_check_ntp_time_returns_true_when_offset_acceptable():
    """check_ntp_time returns True when system time is within threshold."""
    from app.ntp import check_ntp_time

    mock_response = MagicMock()
    mock_response.offset = 0.5
    mock_response.tx_time = 1700000000.0

    with patch("app.ntp.ntplib.NTPClient") as mock_ntp_client:
        mock_client = MagicMock()
        mock_client.request.return_value = mock_response
        mock_ntp_client.return_value = mock_client

        result = check_ntp_time(threshold_sec=60)

    assert result is True


def test_check_ntp_time_returns_false_when_offset_exceeds_threshold():
    """check_ntp_time returns False and logs error when offset exceeds threshold."""
    from app.ntp import check_ntp_time

    mock_response = MagicMock()
    mock_response.offset = 120.0
    mock_response.tx_time = 1700000000.0

    with patch("app.ntp.ntplib.NTPClient") as mock_ntp_client:
        mock_client = MagicMock()
        mock_client.request.return_value = mock_response
        mock_ntp_client.return_value = mock_client

        with patch("app.ntp.logger") as mock_logger:
            result = check_ntp_time(threshold_sec=60)

    assert result is False
    mock_logger.error.assert_called_once()
    assert "incorrect" in mock_logger.error.call_args[0][0].lower()


def test_check_ntp_time_returns_true_when_ntp_unreachable():
    """check_ntp_time returns True (proceed) when NTP request fails."""
    from app.ntp import check_ntp_time

    with patch("app.ntp.ntplib.NTPClient") as mock_ntp_client:
        mock_client = MagicMock()
        mock_client.request.side_effect = OSError("Connection refused")
        mock_ntp_client.return_value = mock_client

        with patch("app.ntp.logger") as mock_logger:
            result = check_ntp_time()

    assert result is True
    mock_logger.warning.assert_called_once()
    assert "Could not verify" in mock_logger.warning.call_args[0][0]


def test_check_ntp_time_returns_true_when_ntplib_missing():
    """check_ntp_time returns True when ntplib is not installed."""
    from app import ntp

    orig_ntplib = ntp.ntplib
    try:
        ntp.ntplib = None
        from app.ntp import check_ntp_time

        with patch("app.ntp.logger") as mock_logger:
            result = check_ntp_time()

        assert result is True
        mock_logger.warning.assert_called_once()
        assert "not installed" in mock_logger.warning.call_args[0][0]
    finally:
        ntp.ntplib = orig_ntplib
