"""
Lazy browse archive listing for the web UI.

Builds one tree level at a time from the archive index when valid, otherwise
uses scoped os.scandir. File lists under a camera are paginated.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

# TTL cache for index_child_file_counts (large archives: avoid full index rescans).
_CHILD_COUNTS_TTL_SEC = 60.0
_CHILD_COUNTS_MAX = 128
_child_counts_cache: dict[
    tuple[str, tuple[str, ...], int], tuple[float, dict[str, int]]
] = {}
_child_counts_lock = threading.Lock()

# Image extensions for preview carousel (lowercase, no dot)
IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"})


def _browse_segment_invalid_chars(segment: str) -> bool:
    """True if segment contains NUL or other C0 control characters."""
    return any(ord(c) < 32 for c in segment)


def parse_browse_path(path: str | None) -> tuple[str, ...]:
    """Parse URL path query into 0–5 path segments. Raises ValueError if invalid."""
    if not path or not str(path).strip():
        return ()
    parts: list[str] = []
    for p in str(path).replace("\\", "/").strip().strip("/").split("/"):
        if not p or p in (".", ".."):
            raise ValueError("invalid path segment")
        if _browse_segment_invalid_chars(p):
            raise ValueError("invalid path segment")
        parts.append(p)
    if len(parts) > 5:
        raise ValueError("path too deep")
    return tuple(parts)


def safe_browse_segments(parts: tuple[str, ...]) -> bool:
    """Reject empty or traversal-like segment values."""
    for p in parts:
        if not p or p.strip() != p:
            return False
        if ".." in p:
            return False
        if _browse_segment_invalid_chars(p):
            return False
    return True


def _is_image_filename(name: str) -> bool:
    if "." not in name:
        return False
    ext = name.rsplit(".", 1)[-1].lower()
    return ext in IMAGE_EXTENSIONS


def _index_rel_six_parts(rel: str) -> list[str] | None:
    """
    Split an index archive key into six segments (airport/year/month/day/camera/file).

    Normalizes ``\\\\`` to ``/`` before ``normpath`` so index keys written on Windows
    still parse on POSIX (``normpath`` alone does not treat ``\\\\`` as a separator
    on POSIX).
    """
    if not isinstance(rel, str) or not rel.strip():
        return None
    s = rel.replace("\\", "/").strip()
    norm = os.path.normpath(s)
    slash = norm.replace("\\", "/")
    parts = [p for p in slash.split("/") if p and p not in (".", "..")]
    if len(parts) != 6:
        return None
    return parts


def _browse_archive_path_parts_valid(parts: list[str]) -> bool:
    """True if airport/year/month/day/camera match scandir_child_names layout rules."""
    if len(parts) != 6:
        return False
    airport, year, month, day, camera, _fname = parts
    if airport.startswith(".") or camera.startswith("."):
        return False
    if not (year.isdigit() and len(year) == 4):
        return False
    if not (month.isdigit() and len(month) == 2):
        return False
    if not (day.isdigit() and len(day) == 2):
        return False
    return True


def index_child_file_counts(
    files: dict[str, Any], prefix_parts: tuple[str, ...]
) -> dict[str, int]:
    """Count archive files under each immediate child name at the next level."""
    k = len(prefix_parts)
    if k > 4:
        return {}
    counts: dict[str, int] = {}
    want = list(prefix_parts)
    for rel in files:
        parts = _index_rel_six_parts(rel)
        if parts is None or not _browse_archive_path_parts_valid(parts):
            continue
        if k > 0 and parts[:k] != want:
            continue
        child = parts[k]
        counts[child] = counts.get(child, 0) + 1
    return counts


def clear_child_file_counts_cache() -> None:
    """Clear TTL cache for child file counts (tests or after a full index rebuild)."""
    with _child_counts_lock:
        _child_counts_cache.clear()


def index_child_file_counts_cached(
    output_dir: str,
    files: dict[str, Any],
    prefix_parts: tuple[str, ...],
) -> dict[str, int]:
    """
    Same as ``index_child_file_counts``, cached briefly per
    ``(output_dir, prefix_parts, len(files))`` so repeated tree expands do not
    rescan the whole index on every request.
    """
    key = (output_dir, prefix_parts, len(files))
    now = time.monotonic()
    with _child_counts_lock:
        ent = _child_counts_cache.get(key)
        if ent is not None and now - ent[0] < _CHILD_COUNTS_TTL_SEC:
            return dict(ent[1])
        counts = index_child_file_counts(files, prefix_parts)
        if len(_child_counts_cache) >= _CHILD_COUNTS_MAX:
            _child_counts_cache.clear()
        _child_counts_cache[key] = (now, counts)
        return dict(counts)


def index_list_all_filenames(
    files: dict[str, Any], prefix_parts: tuple[str, ...]
) -> list[str]:
    """
    Sorted filenames in a camera directory (prefix_parts length 5).

    Uses the same splitting rules as ``index_child_file_counts`` (including
    cross-platform ``\\\\`` normalization and date-segment validation).
    """
    if len(prefix_parts) != 5:
        return []
    want = list(prefix_parts)
    out: list[str] = []
    for rel in files:
        parts = _index_rel_six_parts(rel)
        if parts is None or not _browse_archive_path_parts_valid(parts):
            continue
        if parts[:5] != want:
            continue
        out.append(parts[5])
    out.sort()
    return out


def scandir_child_names(output_dir: str, prefix_parts: tuple[str, ...]) -> list[str]:
    """Immediate subdirectories for non-index or partial walk (depth 0–4)."""
    if len(prefix_parts) > 4:
        return []
    base = os.path.join(output_dir, *prefix_parts)
    if not os.path.isdir(base):
        return []
    depth = len(prefix_parts)
    names: list[str] = []
    try:
        with os.scandir(base) as it:
            for e in it:
                try:
                    if not e.is_dir(follow_symlinks=False) or e.name.startswith("."):
                        continue
                except OSError:
                    continue
                if depth == 0:
                    names.append(e.name)
                elif depth == 1:
                    if e.name.isdigit() and len(e.name) == 4:
                        names.append(e.name)
                elif depth in (2, 3):
                    if e.name.isdigit() and len(e.name) == 2:
                        names.append(e.name)
                elif depth == 4:
                    names.append(e.name)
    except OSError:
        return []
    return sorted(names)


def scandir_list_filenames(output_dir: str, prefix_parts: tuple[str, ...]) -> list[str]:
    """Sorted filenames under a camera directory (5 path parts)."""
    if len(prefix_parts) != 5:
        return []
    base = os.path.join(output_dir, *prefix_parts)
    if not os.path.isdir(base):
        return []
    names: list[str] = []
    try:
        with os.scandir(base) as it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False):
                        names.append(e.name)
                except OSError:
                    pass
    except OSError:
        return []
    names.sort()
    return names


def paginate_list(items: list[str], offset: int, limit: int) -> tuple[int, list[str]]:
    """Return (total, page slice). Negative offset → 0; past end → empty slice."""
    total = len(items)
    if offset < 0:
        offset = 0
    if limit < 1:
        limit = 1
    page = items[offset : offset + limit]
    return total, page


def build_preview_images(
    all_filenames: list[str],
    prefix_parts: tuple[str, ...],
    preview_limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Capped preview list for carousel; returns (preview_entries, truncated).

    ``all_filenames`` must already be sorted (e.g. from ``index_list_all_filenames``
    or ``scandir_list_filenames``). Single pass: collect up to ``preview_limit``
    images; if another image appears later in the list, set ``truncated`` and stop
    (no full list of image names).
    """
    base = "/".join(prefix_parts)
    preview: list[dict[str, Any]] = []
    truncated = False
    img_index = 0
    for fname in all_filenames:
        if not _is_image_filename(fname):
            continue
        if len(preview) < preview_limit:
            preview.append(
                {
                    "path": f"{base}/{fname}",
                    "filename": fname,
                    "index": img_index,
                }
            )
            img_index += 1
        else:
            truncated = True
            break
    return preview, truncated
