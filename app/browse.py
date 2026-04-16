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
        if not isinstance(rel, str):
            continue
        norm = os.path.normpath(rel)
        parts = norm.split(os.sep)
        if len(parts) != 6:
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

    Uses the same normpath + os.sep split as index_child_file_counts so index
    keys work with mixed or platform-specific separators.
    """
    if len(prefix_parts) != 5:
        return []
    want = list(prefix_parts)
    out: list[str] = []
    for rel in files:
        if not isinstance(rel, str):
            continue
        norm = os.path.normpath(rel)
        parts = norm.split(os.sep)
        if len(parts) != 6:
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
