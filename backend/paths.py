"""Filesystem helpers shared by the paths that move or write files.

Nothing here touches the database or the network; these are the small decisions
about names on disk that more than one caller has to make the same way.
"""

from __future__ import annotations

from pathlib import Path


def free_name(target: Path) -> tuple[Path, bool]:
    """A free name at ``target``, never overwriting anything.

    Returns the path to use and whether a collision was worked around. A
    collision is a finding, not an error: two files landing on the same name are
    usually the same picture, twice on disk. Later ones get ``-1``, ``-2`` and
    both are kept.
    """
    if not target.exists():
        return target, False
    index = 1
    while True:
        candidate = target.with_name(f"{target.stem}-{index}{target.suffix}")
        if not candidate.exists():
            return candidate, True
        index += 1


def sidecars_for(image: Path) -> list[Path]:
    """Files that belong to ``image`` and should travel with it.

    Everything in the same folder whose name starts with the image's stem plus a
    dot, and that is not itself a scanned image. That covers the prompt ``.txt``
    a generator writes, a ``.json`` next to it, and the ``bild.png.json`` form
    where the tool appends rather than replaces the extension.

    Deliberately not ``Path.with_suffix()``: that only ever produces one
    candidate and cannot express a doubled extension.
    """
    from . import config

    prefix = f"{image.stem}."
    found = []
    try:
        entries = sorted(image.parent.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry == image or not entry.is_file():
            continue
        if not entry.name.startswith(prefix):
            continue
        if entry.suffix.lower() in config.SUPPORTED_SUFFIXES:
            continue
        found.append(entry)
    return found


def restem(name: str, old_stem: str, new_stem: str) -> str:
    """``name`` with its leading ``old_stem`` swapped for ``new_stem``.

    The tail is whatever follows, however many dots it contains:
    ``bild.png.json`` under the new stem ``bild_42`` becomes
    ``bild_42.png.json``. Neither ``Path.stem`` nor ``Path.with_suffix()`` can
    express that - both see only the last extension, so ``bild.png.json`` would
    lose its ``.png`` and collide with a plain ``bild.json`` next to it.
    """
    if not name.startswith(old_stem):
        return name
    return f"{new_stem}{name[len(old_stem):]}"
