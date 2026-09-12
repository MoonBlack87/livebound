"""Move images from archived posts out of the source folders into the archive.

Explicitly **manual**. This moves real files, irreversibly - it never runs by
itself, never as a side effect of a push, only when it is asked for.

Target layout::

    <archive>/2026-08-24__30497397/00000-972684115_140252440.png
    <archive>/2026-08-24__30497397/00000-972684115_140252440.txt

An adopted post whose publication date is not known uses its CivitAI post id
alone. Existing ``post_id_<post>`` folders are deliberately reused and never
renamed by an archive run.

The filename stays and the CivitAI image id is appended. That puts duplicates
visibly next to each other in one folder: two files carrying the same ``_999`` are
the same picture, twice on disk. Those cases are counted and named as well,
because with different filenames they would otherwise go unnoticed.

If two files really do collide by name - the same name from different source
folders - the later ones get ``-1``, ``-2``. Nothing is ever discarded.

Every file next to the image that shares its stem moves along and is renamed to
match: the prompt ``.txt``, a ``.json``, and the ``bild.png.json`` form where a
tool appended rather than replaced the extension. The whole extension chain is
kept.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import config, db, hashing, jobs, relocate
from .posts import lifecycle
from .store import images as image_store
from .store import sources as source_store
from .store import usage


def status() -> dict[str, Any]:
    """Is an archive configured, and how many images would be due?"""
    archive = source_store.archive_root()
    if archive is None:
        return {"configured": False, "reason": "No archive folder configured."}

    problem = _nesting_problem(archive)
    if problem:
        return {"configured": True, "archive": archive, "blocked": problem, "candidates": 0}

    candidates = plan()
    return {
        "configured": True,
        "archive": archive,
        "blocked": None,
        "candidates": len(candidates),
        "posts": len({item["remote_post_id"] for item in candidates}),
        "unresolved": sum(1 for item in candidates if not item["remote_image_id"]),
        "duplicates": duplicate_groups(candidates),
    }


def duplicate_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Files pointing at the same picture on CivitAI.

    Only the appended image id makes this visible - with different filenames they
    do not collide in the target folder and would otherwise stay unnoticed.
    """
    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for item in items:
        if not item.get("remote_image_id"):
            continue
        key = (item["remote_post_id"], item["remote_image_id"])
        groups.setdefault(key, []).append(item)

    return [
        {
            "remote_post_id": post_id,
            "remote_image_id": image_id,
            "paths": [entry["source_path"] for entry in entries],
        }
        for (post_id, image_id), entries in sorted(groups.items())
        if len(entries) > 1
    ]


def plan(limit: int | None = None) -> list[dict[str, Any]]:
    """What would be moved. Changes nothing.

    Only an identical non-empty file or pixel hash authorises a move. A close
    perceptual hash remains a comparison hint even when generation data matches:
    a series of related images would otherwise be dragged along with it, and the
    move cannot be undone.

    The order of the checks is a performance decision, not a matter of taste.
    The database answers "has this been published?" in microseconds; the
    filesystem answers "is this file still there?" in milliseconds, and on a
    Windows mount under WSL in several. Asking every image in the library the
    expensive question first took 42 seconds over 3,800 images - and the panel
    asks for the plan twice. Cheap questions first, the filesystem only for the
    rows that are actually due.
    """
    archive = source_store.archive_root()
    if archive is None:
        return []

    archive_path = Path(archive["path"]).resolve()
    archive_root_id = archive["id"]
    rows, _ = image_store.search(limit=100000)
    items: list[dict[str, Any]] = []
    publication_dates: dict[int, str | None] = {}

    # Whatever the archive root itself holds stays where it is, and the row
    # already says which root it came from - no filesystem question needed.
    candidates = [row for row in rows if row.get("source_root_id") != archive_root_id]
    # One bulk lookup instead of one history scan per image, and without the
    # perceptual stage: only identical bytes or pixels authorise a move.
    history = usage.check_many(candidates, exact_only=True)
    # The history also contains scheduled posts and backfill rows. One indexed
    # lookup keeps the state gate out of the per-image loop while requiring the
    # usage row to point at a post the maintainer explicitly archived.
    archivable_post_ids = {
        row["id"]
        for row in db.get_connection().execute(
            "SELECT id FROM posts WHERE state=?",
            (lifecycle.ARCHIVED,),
        )
    }

    for row in candidates:
        result = history.get(row.get("sha256") or "")
        if not result:
            continue
        matches = [*result["exact"], *result["confirmed"]]
        match = next(
            (
                candidate
                for candidate in matches
                if candidate.get("match_reason")
                in {"identical bytes", "identical pixels"}
                and candidate.get("post_id") in archivable_post_ids
            ),
            None,
        )
        if match is None:
            continue
        remote_post_id = match.get("remote_post_id")
        if not remote_post_id:
            # Without a post id there is no target folder - such an entry comes
            # from a local plan, not from a published post.
            continue

        # Only now the filesystem, and only for a row that is actually due.
        path = Path(row["absolute_path"])
        try:
            resolved = path.resolve()
        except OSError:
            continue
        # A folder inside a source root can still lie inside the archive.
        if resolved == archive_path or archive_path in resolved.parents:
            continue
        if not path.exists():
            continue

        if remote_post_id not in publication_dates:
            publication_dates[remote_post_id] = publication_date(remote_post_id)
        published_on = publication_dates[remote_post_id]

        items.append(
            {
                "image_id": row["id"],
                "source_path": str(path),
                "name": path.name,
                "remote_post_id": remote_post_id,
                "remote_image_id": match.get("remote_image_id"),
                "published_on": published_on,
                "how": match["match_reason"],
                "sha256": row.get("sha256"),
                "target": str(
                    _target_for(
                        archive_path,
                        path,
                        remote_post_id,
                        match.get("remote_image_id"),
                        published_on,
                    )
                ),
            }
        )
        if limit and len(items) >= limit:
            break

    return items


def run(job: jobs.Job, image_ids: list[int] | None = None) -> dict[str, Any]:
    """Move. One file after another, each recorded immediately."""
    archive = source_store.archive_root()
    if archive is None:
        # Raised, not assigned: assigning left the job status at "done", so the
        # bar reported a cheerful "0 moved" and the reason was never shown.
        raise RuntimeError("No archive folder configured. Choose one in the settings.")

    archive_path = Path(archive["path"]).resolve()
    problem = _nesting_problem(archive)
    if problem:
        raise RuntimeError(problem)

    items = plan()
    if image_ids is not None:
        wanted = set(image_ids)
        items = [item for item in items if item["image_id"] in wanted]

    job.total = len(items)
    duplicates = duplicate_groups(items)
    totals = {
        "moved": 0,
        "sidecars": 0,
        "collisions": 0,
        "duplicates": len(duplicates),
        "skipped": 0,
        "failed": 0,
    }
    for group in duplicates:
        job.log(
            status="duplicate",
            remote_image_id=group["remote_image_id"],
            message=(
                f"{len(group['paths'])} files point at the same picture "
                f"{group['remote_image_id']}"
            ),
            paths=group["paths"],
        )

    for index, item in enumerate(items, start=1):
        if job.should_stop():
            break
        job.processed = index
        source = Path(item["source_path"])
        job.stage = f"{index}/{len(items)}: {source.name}"

        if not source.exists():
            totals["skipped"] += 1
            continue

        # The file must still be the one that was checked. It can have been
        # replaced between preview and run, and the move is irreversible.
        if item.get("sha256") and hashing.file_sha256(source) != item["sha256"]:
            totals["skipped"] += 1
            job.log(path=str(source), status="skipped", message="file has changed")
            continue

        try:
            moved = _move_one(archive_path, item)
        except Exception as exc:
            totals["failed"] += 1
            job.failed += 1
            job.log(path=str(source), status="error", message=f"{type(exc).__name__}: {exc}")
            continue

        totals["moved"] += 1
        totals["sidecars"] += moved["sidecars"]
        if moved["collision"]:
            totals["collisions"] += 1
        job.succeeded += 1
        job.log(
            path=str(source),
            status="ok",
            target=moved["target"],
            collision=moved["collision"],
        )

    # What the run *planned* to drain: an item can be skipped, fail, or be cut
    # short by a stop. Folder cleanup below re-checks the actual outcome.
    planned_posts = {item["remote_post_id"] for item in items}
    totals["folders_removed"] = _remove_emptied_adopted_folders(job, planned_posts)

    job.result = {**totals, "duplicate_groups": duplicates}
    job.stage = "Done"
    return totals


#: The folder name ``fetch_images`` creates for an adopted post, and the only
#: shape this cleanup will ever remove.
_ADOPTED_FOLDER = re.compile(r"^post_id_\d+$")


def _remove_emptied_adopted_folders(job: jobs.Job, remote_post_ids: set[int]) -> int:
    """Remove the adopted folders this run has just emptied.

    Removing a directory is a deletion, so it happens only where it provably
    discards nothing (`PRI-03`). All three have to hold: the directory lies
    directly inside the configured adopted folder, its name is the
    ``post_id_<digits>`` this application created itself
    (`posts/fetch_images.py`), and it is empty. Anything else is left alone -
    a source root is the user's own folder structure and is never tidied.

    Scoped to the posts this run planned to drain rather than to the whole
    adopted folder. That is a narrowing, not a guarantee: emptiness is what
    decides, and a fetch for the same post can recreate the directory - it uses
    `mkdir(exist_ok=True)`, so nothing is lost either way.

    This is tidying, not the point of the run: a directory that refuses to go
    is logged and the run carries on.
    """
    adopted = config.adopted_images_dir()
    if adopted is None:
        return 0
    try:
        root = adopted.resolve()
    except OSError:
        return 0

    removed = 0
    for remote_post_id in sorted(remote_post_ids):
        folder = root / f"post_id_{remote_post_id}"
        if not _ADOPTED_FOLDER.match(folder.name):
            continue
        try:
            if folder.is_symlink() or not folder.is_dir():
                continue
            # Belt to is_symlink's braces: if the name ever stops being a
            # literal, this keeps the deletion inside the adopted folder.
            if folder.resolve().parent != root:
                continue
            if next(folder.iterdir(), None) is not None:
                continue
            folder.rmdir()
        except OSError as exc:
            # One folder that will not go must not fail a run whose files have
            # already moved.
            job.log(path=str(folder), status="error", message=f"folder kept: {exc}")
            continue
        removed += 1
        job.log(path=str(folder), status="ok", message="empty adopted folder removed")
    return removed


def _move_one(archive_path: Path, item: dict[str, Any]) -> dict[str, Any]:
    source = Path(item["source_path"])
    target = _target_for(
        archive_path,
        source,
        item["remote_post_id"],
        item.get("remote_image_id"),
        item.get("published_on"),
    )
    archive = source_store.archive_root()
    if archive is None:
        raise RuntimeError("The archive folder is no longer configured; configure it and retry.")
    return relocate.move_image(
        item["image_id"],
        source,
        target,
        destination_root=archive,
        update_usage_path=True,
    )


def _target_for(
    archive_path: Path,
    source: Path,
    remote_post_id: int,
    remote_image_id: int | None,
    published_on: str | None,
) -> Path:
    """``<archive>/<date>__<post>/<name>_<imageid><suffix>``.

    Without an image id the name stays as it is - better no suffix than an
    invented one.
    """
    stem = source.stem
    if remote_image_id:
        stem = f"{stem}_{remote_image_id}"
    return folder_for(archive_path, remote_post_id, published_on) / f"{stem}{source.suffix}"


def publication_date(remote_post_id: int) -> str | None:
    """The post's confirmed UTC publication date, if it is still knowable.

    The post row is the precise source. The first ``published`` event is the
    durable fallback after a local post was deleted or for older records that
    do not carry ``remote_published_at``.
    """
    from .posts import schedule

    conn = db.get_connection()
    row = conn.execute(
        "SELECT remote_published_at FROM posts WHERE remote_post_id=?",
        (remote_post_id,),
    ).fetchone()
    candidates = [row["remote_published_at"]] if row else []
    event = conn.execute(
        "SELECT at FROM post_events WHERE remote_post_id=? AND event='published'"
        " ORDER BY at, id LIMIT 1",
        (remote_post_id,),
    ).fetchone()
    if event:
        candidates.append(event["at"])

    for value in candidates:
        if when := schedule.parse_iso(value):
            return when.date().isoformat()
    return None


def folder_for(archive_path: Path, remote_post_id: int, published_on: str | None) -> Path:
    """Choose one folder without splitting or silently renaming an old post.

    Legacy and id-only folders win when they already exist. A dated folder is
    used for a new post only when the date is known.
    """
    legacy = archive_path / f"post_id_{remote_post_id}"
    if legacy.is_dir():
        return legacy

    undated = archive_path / str(remote_post_id)
    if undated.is_dir():
        return undated

    if published_on:
        dated = archive_path / f"{published_on}__{remote_post_id}"
        if dated.is_dir():
            return dated

    suffix = f"__{remote_post_id}"
    dated_folders = sorted(
        child
        for child in archive_path.glob(f"????-??-??{suffix}")
        if child.is_dir() and child.name.endswith(suffix)
    )
    if len(dated_folders) == 1:
        return dated_folders[0]
    name = f"{published_on}__{remote_post_id}" if published_on else str(remote_post_id)
    return archive_path / name


def undated_folder_rename(remote_post_id: int) -> dict[str, str] | None:
    """Preview the explicit id-only folder rename, when it is available."""
    archive = source_store.archive_root()
    published_on = publication_date(remote_post_id)
    if archive is None or published_on is None:
        return None

    archive_path = Path(archive["path"]).resolve()
    source = archive_path / str(remote_post_id)
    if not source.is_dir():
        return None
    return {
        "from": str(source),
        "to": str(archive_path / f"{published_on}__{remote_post_id}"),
    }


def rename_undated_folder(remote_post_id: int) -> dict[str, Any]:
    """Explicitly rename an id-only folder once its date is known.

    Nothing calls this as part of a scan, sync or archive run. Renaming moves
    every file in the folder, so the caller must present it as a destructive
    action before invoking it.
    """
    archive = source_store.archive_root()
    if archive is None:
        raise ValueError("Configure an archive folder before renaming a post folder.")
    problem = _nesting_problem(archive)
    if problem:
        raise ValueError(problem)

    published_on = publication_date(remote_post_id)
    if published_on is None:
        raise ValueError(
            f"Post {remote_post_id} has no known publication date; sync it before renaming."
        )

    archive_path = Path(archive["path"]).resolve()
    source = archive_path / str(remote_post_id)
    target = archive_path / f"{published_on}__{remote_post_id}"
    if not source.is_dir():
        if target.is_dir():
            return {"renamed": False, "from": str(source), "to": str(target)}
        raise FileNotFoundError(
            f"The id-only archive folder {source} was not found; refresh the archive and retry."
        )
    result = relocate.rename_folder(source, target, root=archive)
    return {"renamed": True, "from": str(source), "to": str(target), **result}


def _nesting_problem(archive: dict[str, Any]) -> str | None:
    """The archive must not lie inside a source folder, or the other way round.

    Otherwise the run moves files within the tree it is walking - and the next
    scan finds them in the archive as supposedly new images.
    """
    archive_path = Path(archive["path"]).resolve()
    for root in source_store.list_roots():
        if root["id"] == archive["id"] or root.get("is_archive"):
            continue
        other = Path(root["path"]).resolve()
        if archive_path == other:
            return f"Archive and source folder are the same folder: {other}"
        if other in archive_path.parents:
            return (
                f"The archive lies inside the source folder {other}. Choose a folder "
                "outside the folders being scanned."
            )
        if archive_path in other.parents:
            return f"The source folder {other} lies inside the archive."
    return None
