"""
AviationWX.org Archiver - Version and build info.

Version is read from package metadata when installed, else pyproject.toml.
Git SHA comes from GIT_SHA env (set at Docker build) or from git rev-parse.
"""

from __future__ import annotations

import os
import subprocess


def _get_version() -> str:
    """Return package version from metadata, pyproject, or fallback."""
    try:
        from importlib.metadata import version

        return version("aviationwx-archiver")
    except Exception:
        pass
    try:
        from pathlib import Path

        try:
            import tomllib
        except ImportError:
            tomllib = None

        if tomllib:
            for path in (Path(__file__).resolve().parent.parent, Path.cwd()):
                pyproject = path / "pyproject.toml"
                if pyproject.exists():
                    with open(pyproject, "rb") as fh:
                        data = tomllib.load(fh)
                    return data.get("project", {}).get("version", "0.0.0")
    except Exception:
        pass
    return "0.3.0"


def _get_git_sha() -> str:
    """Return short git SHA from env or git, or empty string."""
    sha = os.environ.get("GIT_SHA", "").strip()
    if sha:
        return sha[:12] if len(sha) > 12 else sha
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


VERSION = _get_version()
GIT_SHA = _get_git_sha()
