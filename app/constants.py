"""
AviationWX.org Archiver - Application constants.

Centralizes magic numbers for clarity and maintainability.
"""

# Time
SECONDS_PER_MINUTE = 60
SECONDS_PER_DAY = 86400

# Bytes / units
BYTES_PER_MIB = 1024 * 1024
BYTES_PER_GIB = 1024**3

# Percent scale (0-100)
PERCENT_SCALE = 100

# File hashing
MD5_READ_CHUNK_SIZE = 65536

# Config defaults (used as fallbacks in .get() when key missing)
DEFAULT_INTERVAL_MINUTES = 15
DEFAULT_LOG_DISPLAY_COUNT = 100

# API rate limiting (aviationwx.org - https://api.aviationwx.org/)
# Anonymous: 100/min, 1,000/hr, 10,000/day. Partner: 500/min, 5,000/hr, 50,000/day.
# Default uses half of anonymous limit (50 req/min = 1.2s between requests).
API_LIMIT_ANONYMOUS_REQ_PER_MIN = 100
API_LIMIT_PARTNER_REQ_PER_MIN = 500
DEFAULT_REQUEST_DELAY_SECONDS = 1.2  # 50 req/min = half of anonymous limit
