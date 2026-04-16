"""
Tests for main.py — startup helpers.
"""

from app.constants import DEFAULT_WAITRESS_THREADS
from main import _waitress_threads_runtime


def test_waitress_threads_runtime_accepts_valid_int():
    """Configured thread count in range is returned as-is."""
    assert _waitress_threads_runtime({"web": {"waitress_threads": 8}}) == 8


def test_waitress_threads_runtime_accepts_numeric_string():
    """Quoted YAML-style string integers coerce like validate_config."""
    assert _waitress_threads_runtime({"web": {"waitress_threads": "12"}}) == 12


def test_waitress_threads_runtime_invalid_falls_back_to_default():
    """Non-numeric values fall back to DEFAULT_WAITRESS_THREADS (clamped)."""
    expected = max(1, min(128, DEFAULT_WAITRESS_THREADS))
    assert _waitress_threads_runtime({"web": {"waitress_threads": "nope"}}) == expected


def test_waitress_threads_runtime_clamps_above_range():
    """Values above 128 clamp to 128."""
    assert _waitress_threads_runtime({"web": {"waitress_threads": 500}}) == 128


def test_waitress_threads_runtime_clamps_below_range():
    """Values below 1 clamp to 1."""
    assert _waitress_threads_runtime({"web": {"waitress_threads": 0}}) == 1
