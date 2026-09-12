"""Walking the configured source folders."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from .. import config


def iter_images(
    root: Path,
    *,
    excluded: set[str] | None = None,
    trash_folder: Path | None = None,
) -> Iterator[Path]:
    """Yield every supported image below ``root``.

    Excluded folders are pruned in place so ``os.walk`` never descends into them
    at all - relevant when a source folder contains a large "Posted" archive.
    The resolved configured ``trash_folder`` and everything below it are also
    pruned. Hidden directories are always skipped.
    """
    excluded_lower = {name.lower() for name in (excluded or set())}
    resolved_trash = trash_folder.resolve() if trash_folder is not None else None
    if resolved_trash is not None and _inside(root, resolved_trash):
        return

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda _: None):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name.lower() not in excluded_lower
            and not name.startswith(".")
            and not (
                resolved_trash is not None
                and _inside(Path(dirpath) / name, resolved_trash)
            )
        )
        for name in sorted(filenames):
            if Path(name).suffix.lower() in config.SUPPORTED_SUFFIXES:
                yield Path(dirpath) / name


def _inside(path: Path, folder: Path) -> bool:
    resolved = path.resolve()
    return resolved == folder or folder in resolved.parents
