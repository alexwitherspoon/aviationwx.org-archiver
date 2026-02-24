"""
NTP time check — verify system clock against UTC.

Logs an error if the server time differs from NTP by more than the threshold.
The app uses UTC as the default timezone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

try:
    import ntplib
except ImportError:
    ntplib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Max allowed offset (seconds) between system UTC and NTP before logging error
_NTP_OFFSET_THRESHOLD_SEC = 60

# NTP server (pool.ntp.org resolves to multiple servers, uses UTC)
_NTP_SERVER = "pool.ntp.org"


def check_ntp_time(threshold_sec: float = _NTP_OFFSET_THRESHOLD_SEC) -> bool:
    """
    Check system UTC time against NTP. Log error if offset exceeds threshold.

    Returns True if time is acceptable (within threshold or NTP unreachable),
    False if offset exceeds threshold (caller may choose to warn/abort).
    """
    if ntplib is None:
        logger.warning("ntplib not installed; skipping NTP time verification.")
        return True
    try:
        client = ntplib.NTPClient()
        response = client.request(_NTP_SERVER, version=3)
        # offset: positive = local clock is ahead of NTP
        offset_sec = response.offset
        local_utc = datetime.now(timezone.utc)
        ntp_utc = datetime.fromtimestamp(response.tx_time, tz=timezone.utc)

        if abs(offset_sec) > threshold_sec:
            logger.error(
                "System time is incorrect: UTC offset from NTP (%s) is %.1f s "
                "(threshold %.1f s). Local UTC: %s, NTP UTC: %s. "
                "File timestamps may be wrong. Fix system time (NTP sync).",
                _NTP_SERVER,
                offset_sec,
                threshold_sec,
                local_utc.isoformat(),
                ntp_utc.isoformat(),
            )
            return False
        logger.debug(
            "NTP check OK: UTC offset %.2f s from %s",
            offset_sec,
            _NTP_SERVER,
        )
        return True
    except Exception as exc:
        logger.warning(
            "Could not verify system time via NTP (%s): %s. "
            "Proceeding without time verification.",
            _NTP_SERVER,
            exc,
        )
        return True  # Don't block on NTP failure
