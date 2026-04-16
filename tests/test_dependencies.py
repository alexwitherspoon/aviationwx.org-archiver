"""
Guards for declared runtime dependencies (supply-chain / CVE remediation).
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REQUIREMENTS = _REPO_ROOT / "requirements.txt"


def _pinned_requests_version() -> tuple[int, int, int]:
    text = _REQUIREMENTS.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        m = re.match(r"^requests==(\d+)\.(\d+)\.(\d+)", line)
        if m:
            return tuple(int(x) for x in m.groups())
    raise AssertionError("No requests== pin found in requirements.txt")


def test_requirements_requests_meets_cve_2026_25645_minimum():
    """Pin must stay >= 2.33.0 for CVE-2026-25645 (GHSA-gc5v-m9x4-r6x2)."""
    assert _pinned_requests_version() >= (2, 33, 0)
