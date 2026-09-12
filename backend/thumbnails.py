"""WEBP thumbnail cache for the library grid.

Two sizes: the small one is produced during the scan, the large one on first
request. Generating both up front would double the cost of every scan for a size
most images are never shown at.

The cache key includes the source content hash and the edge length, so even a
file restored with its old mtime and size re-keys when its bytes change.

Writes go to a ``.part`` file first and are then renamed, because a half-written
thumbnail served to the browser is worse than none.

These files exist only for display. Nothing here is ever uploaded: the push path
uses a full-resolution metadata-only copy, never a thumbnail.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from PIL import Image

from . import config


def cache_key(content_sha256: str, edge: int) -> str:
    """The edge length belongs in the key: two sizes of one file coexist."""
    return hashlib.sha256(f"{content_sha256}|{edge}".encode()).hexdigest()


def path_for(name: str) -> Path:
    return config.thumbnail_dir() / name


def ensure(
    source: Path, content_sha256: str, edge: int = config.THUMBNAIL_MAX_EDGE
) -> str | None:
    """Return the cached thumbnail's *file name*, creating it if needed.

    A name, not a path: the data directory can be moved, and a stored absolute
    path would point nowhere afterwards. ``None`` on failure - a missing
    thumbnail must not break ingestion, so every failure is swallowed and the
    grid falls back to the full image.
    """
    if not content_sha256:
        return None
    name = f"{cache_key(content_sha256, edge)}.webp"
    target = path_for(name)
    if target.exists():
        return name

    tmp = target.with_suffix(".part")
    try:
        with Image.open(source) as image:
            # draft() is a fast JPEG-only downscale hint; a no-op for PNG
            image.draft("RGB", (edge, edge))
            image = image.convert("RGB")
            image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            image.save(tmp, "WEBP", quality=82, method=4)
        tmp.replace(target)
        return name
    except Exception:
        tmp.unlink(missing_ok=True)
        return None


def prune(content_hashes: Iterable[str], stored_names: Iterable[str] = ()) -> int:
    """Remove cache entries that no current image content can use.

    Both supported sizes are retained. This runs only after a completed scan,
    so an interruption cannot discard the thumbnails of rows not reached yet.
    """
    keep = set(stored_names) | {
        f"{cache_key(content_sha256, edge)}.webp"
        for content_sha256 in content_hashes
        for edge in (config.THUMBNAIL_MAX_EDGE, config.THUMBNAIL_LARGE_EDGE)
    }
    removed = 0
    for cached in config.thumbnail_dir().glob("*.webp"):
        if cached.name not in keep:
            try:
                cached.unlink()
            except OSError:
                # One stale cache entry must not abort the scan batch whose
                # image rows and current thumbnails are already complete.
                continue
            removed += 1
    return removed
