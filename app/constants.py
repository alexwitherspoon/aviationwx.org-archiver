"""
AviationWX.org Archiver - Application constants.

Centralizes magic numbers for clarity and maintainability.
"""

# Time
SECONDS_PER_MINUTE = 60
SECONDS_PER_DAY = 86400

# Bytes / units (binary: MiB, GiB, TiB, PiB)
BYTES_PER_MIB = 1024 * 1024
BYTES_PER_GIB = 1024**3
BYTES_PER_TIB = 1024**4
BYTES_PER_PIB = 1024**5


def parse_storage_gb(value):  # str | int | float -> float
    """
    Parse storage limit from user input.

    Accepts: "10", "10.5", "100" (GB), "1TB" or "1 TB" (converted to GB).
    Returns value in GB. 0 or invalid = disabled.
    """
    if value is None or value == "":
        return 0.0
    s = str(value).strip().upper()
    if not s:
        return 0.0
    # Handle "1TB", "1 TB", "500GB" etc.
    if "TB" in s:
        try:
            num = float(s.replace("TB", "").replace(" ", "").strip())
            return max(0.0, num * 1024)  # TB -> GB
        except ValueError:
            return 0.0
    if "GB" in s:
        s = s.replace("GB", "").replace(" ", "").strip()
    try:
        return max(0.0, float(s))
    except ValueError:
        return 0.0


# Percent scale (0-100)
PERCENT_SCALE = 100

# File hashing
MD5_READ_CHUNK_SIZE = 65536

# Partial file detection: images smaller than this are considered incomplete
MIN_IMAGE_SIZE = 256

# Config defaults (used as fallbacks in .get() when key missing)
DEFAULT_INTERVAL_MINUTES = 15
DEFAULT_LOG_DISPLAY_COUNT = 100

# API rate limiting (aviationwx.org - https://api.aviationwx.org/)
# Anonymous: 100/min, 1,000/hr, 10,000/day. Partner: 500/min, 5,000/hr, 50,000/day.
# Default uses half of anonymous limit (50 req/min = 1.2s between requests).
API_LIMIT_ANONYMOUS_REQ_PER_MIN = 100
API_LIMIT_PARTNER_REQ_PER_MIN = 500
DEFAULT_REQUEST_DELAY_SECONDS = 1.2  # 50 req/min = half of anonymous limit
