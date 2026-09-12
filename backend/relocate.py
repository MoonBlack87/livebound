"""Moving application data and library image files.

The database, the thumbnail cache and the backups travel together: splitting
them would leave the biggest part (the thumbnails) behind, which is usually the
reason for moving in the first place.

Copy, verify, repoint, and only then remove the source. A move that fails
halfway must leave the old location intact and usable - it is the only copy of
the state there is.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import backup, config, db, jobs, paths

#: What lives in the data directory and has to travel with it. The diagnostic
#: log is here because the settings text promises everything is copied, and a
#: user who deletes the old directory afterwards should not be leaving a file
#: behind that this application put there.
CONTENTS = ("publisher.db", "thumbnails", "backups", "diagnostics.log")


def move_image(
    image_id: int,
    source: Path,
    target: Path,
    *,
    destination_root: dict[str, Any] | None = None,
    with_sidecars: bool = True,
    exact_target: bool = False,
    mark_trashed: bool | None = None,
    update_usage_path: bool = False,
) -> dict[str, Any]:
    """Move an image, its sidecars and its database row as one operation.

    ``destination_root`` changes the row's source-root-relative location for an
    archive move. Trash and restore keep that original library location, while
    ``mark_trashed`` records whether the current absolute path is hidden from
    the library. A failed row update moves every file back before surfacing the
    error, so disk and database cannot quietly disagree.
    """
    row = db.get_connection().execute(
        "SELECT absolute_path FROM images WHERE id=?", (image_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Image {image_id} no longer exists in the library.")
    if Path(row["absolute_path"]) != source:
        raise ValueError(f"Image {image_id} moved before this operation; refresh and try again.")
    if not source.is_file():
        raise FileNotFoundError(f"Source file not found: {source}")

    target.parent.mkdir(parents=True, exist_ok=True)
    if exact_target:
        if target.exists():
            raise FileExistsError(f"Restore target already exists: {target}")
        final, collision = target, False
    else:
        final, collision = paths.free_name(target)

    companions: list[tuple[Path, Path]] = []
    if with_sidecars:
        for sidecar in paths.sidecars_for(source):
            proposed = final.with_name(paths.restem(sidecar.name, source.stem, final.stem))
            if exact_target:
                if proposed.exists():
                    raise FileExistsError(f"Restore target already exists: {proposed}")
                companion = proposed
            else:
                companion, _ = paths.free_name(proposed)
            companions.append((sidecar, companion))

    destination: tuple[int, str, str] | None = None
    if destination_root is not None:
        root_path = Path(destination_root["path"]).resolve()
        relative = final.resolve().relative_to(root_path)
        folder = str(relative.parent) if str(relative.parent) != "." else ""
        destination = (int(destination_root["id"]), str(relative), folder)

    moved: list[tuple[Path, Path]] = []
    try:
        shutil.move(str(source), str(final))
        moved.append((source, final))
        for companion_source, companion_target in companions:
            shutil.move(str(companion_source), str(companion_target))
            moved.append((companion_source, companion_target))
    except OSError as exc:
        _put_back(moved, exc)
        raise

    try:
        with db.transaction() as conn:
            assignments = ["absolute_path=?", "is_missing=0"]
            values: list[Any] = [str(final)]
            if destination is not None:
                assignments.extend(("source_root_id=?", "relative_path=?", "folder=?"))
                values.extend(destination)
            if mark_trashed is True:
                assignments.extend(
                    ("is_trashed=1", "trash_original_path=?", "trashed_at=?")
                )
                values.extend((str(source), db.now_iso()))
            elif mark_trashed is False or destination is not None:
                assignments.extend(
                    ("is_trashed=0", "trash_original_path=NULL", "trashed_at=NULL")
                )
            values.append(image_id)
            cursor = conn.execute(
                f"UPDATE images SET {', '.join(assignments)} WHERE id=?",
                values,
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"Image {image_id} disappeared during the move; the files were put back."
                )
            if update_usage_path:
                conn.execute(
                    "UPDATE image_usage SET source_path=? WHERE source_path=?",
                    (str(final), str(source)),
                )
    except (sqlite3.Error, ValueError) as exc:
        _put_back(moved, exc)
        raise

    return {
        "target": str(final),
        "collision": collision,
        "sidecars": len(companions),
    }


def rename_folder(source: Path, target: Path, *, root: dict[str, Any]) -> dict[str, int]:
    """Rename one folder and repoint every library and usage row inside it.

    The filesystem rename happens first and is put back if the database update
    fails. Source and target must be direct children of the same configured
    root; this operation is for changing a folder name, not relocating a tree.
    """
    root_path = Path(root["path"]).resolve()
    source_path = source.resolve()
    target_path = target.resolve()
    if source_path.parent != root_path or target_path.parent != root_path:
        raise ValueError("Both folder names must be directly inside the configured root.")
    if not source.is_dir():
        raise FileNotFoundError(f"Folder not found: {source}")
    if target.exists():
        raise FileExistsError(
            f"The target folder already exists: {target}. Merge the folders manually first."
        )

    conn = db.get_connection()
    image_changes: list[tuple[int, str, str, str]] = []
    for row in conn.execute(
        "SELECT id, absolute_path FROM images WHERE source_root_id=?", (int(root["id"]),)
    ):
        replacement = _inside_replacement(row["absolute_path"], source_path, target_path)
        if replacement is None:
            continue
        relative = replacement.relative_to(root_path)
        folder = str(relative.parent) if str(relative.parent) != "." else ""
        image_changes.append((int(row["id"]), str(replacement), str(relative), folder))

    usage_changes: list[tuple[int, str]] = []
    for row in conn.execute(
        "SELECT id, source_path FROM image_usage WHERE source_path IS NOT NULL"
    ):
        replacement = _inside_replacement(row["source_path"], source_path, target_path)
        if replacement is not None:
            usage_changes.append((int(row["id"]), str(replacement)))

    source.rename(target)
    try:
        with db.transaction() as tx:
            for image_id, absolute_path, relative_path, folder in image_changes:
                cursor = tx.execute(
                    "UPDATE images SET absolute_path=?, relative_path=?, folder=? WHERE id=?",
                    (absolute_path, relative_path, folder, image_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        f"Image {image_id} disappeared during the rename; the folder was put back."
                    )
            for usage_id, source_path in usage_changes:
                tx.execute(
                    "UPDATE image_usage SET source_path=? WHERE id=?",
                    (source_path, usage_id),
                )
    except (sqlite3.Error, ValueError) as exc:
        _put_folder_back(source, target, exc)
        raise

    return {"images": len(image_changes), "usage_rows": len(usage_changes)}


def _inside_replacement(raw: str, source: Path, target: Path) -> Path | None:
    try:
        relative = Path(raw).resolve().relative_to(source)
    except (OSError, ValueError):
        return None
    return target / relative


def _put_folder_back(source: Path, target: Path, cause: BaseException) -> None:
    try:
        if target.exists():
            target.rename(source)
    except OSError as rollback_error:
        raise RuntimeError(
            f"The folder rename failed ({cause}) and rollback also failed ({rollback_error}). "
            "Put the folder back manually before retrying."
        ) from rollback_error


def _put_back(moved: list[tuple[Path, Path]], cause: BaseException) -> None:
    """Reverse the completed part of a failed image relocation."""
    try:
        for source, destination in reversed(moved):
            if destination.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), str(source))
    except OSError as rollback_error:
        raise RuntimeError(
            f"The move failed ({cause}) and rollback also failed ({rollback_error}). "
            "Put the files back manually before retrying."
        ) from rollback_error


def status() -> dict[str, Any]:
    """Where the data lives now, how much of it there is, and how it got there."""
    current = config.data_dir()
    return {
        "path": str(current),
        "default_path": str(config.default_data_dir()),
        "pointer_file": str(config.config_file()),
        "is_default": current.resolve() == config.default_data_dir().resolve(),
        "bytes": _size(current),
        "free_bytes": shutil.disk_usage(current).free,
    }


def check_target(raw: str) -> Path:
    """Refuse a target that cannot work, before anything is copied."""
    target = Path(raw).expanduser()
    current = config.data_dir()

    if not raw.strip():
        raise ValueError("Give a folder.")
    if not target.is_absolute():
        raise ValueError("Give an absolute path.")
    if target.resolve() == current.resolve():
        raise ValueError("The data already lives there.")
    if current.resolve() in target.resolve().parents:
        raise ValueError("The target lies inside the current data directory.")

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"Cannot create {target}: {exc}") from exc
    if any(target.iterdir()):
        raise ValueError(f"{target} is not empty.")

    needed = _size(current)
    free = shutil.disk_usage(target).free
    if free < needed * 1.1:
        raise ValueError(
            f"Not enough room: {needed / 1024**3:.1f} GB to move, "
            f"{free / 1024**3:.1f} GB free."
        )
    return target


def run(job: jobs.Job, raw_target: str) -> dict[str, Any]:
    """Copy everything across, repoint, then remove the source."""
    source = config.data_dir()
    target = check_target(raw_target)

    job.stage = "Backing the database up first"
    backup.create(reason="before-move", automatic=True)

    job.stage = "Closing the database"
    # Everything from here to the repoint happens with the database shut: a
    # write that lands in the old file after it has been copied would be thrown
    # away by the switch, and it would be thrown away silently.
    with db.frozen():
        # WAL data lives in a side file; folding it back in means the copy is
        # one self-contained file rather than three that have to stay in step.
        row = db.get_connection().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is not None and row[0]:
            # busy: a reader still holds the WAL, so it does not all fit in the
            # main file yet and the copy would be missing the newest writes.
            raise RuntimeError("The database is still in use; try the move again in a moment.")
        db.relocated()

        copied = []
        try:
            for name in CONTENTS:
                item = source / name
                if not item.exists():
                    continue
                job.stage = f"Copying {name}"
                if item.is_dir():
                    shutil.copytree(item, target / name)
                else:
                    shutil.copy2(item, target / name)
                copied.append(name)

            job.stage = "Checking the copy"
            _verify(source, target, copied)
        except Exception:
            # Nothing has been repointed yet, so the old location is still the
            # live one. Clear the half-copy so a retry starts from an empty
            # folder.
            shutil.rmtree(target, ignore_errors=True)
            db.relocated()
            raise

        config.set_data_dir(target)
        db.relocated()
        db.init_db()

    job.stage = "Removing the old copy"
    for name in copied:
        item = source / name
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)
    # WAL side files of the database that was just closed.
    for leftover in ("publisher.db-wal", "publisher.db-shm"):
        (source / leftover).unlink(missing_ok=True)

    job.stage = "Done"
    result = {"from": str(source), "to": str(target), "moved": copied}
    job.result = result
    return result


def _verify(source: Path, target: Path, names: list[str]) -> None:
    for name in names:
        origin, copy = source / name, target / name
        if not copy.exists():
            raise RuntimeError(f"{name} did not arrive at the target.")
        if origin.is_file() and origin.stat().st_size != copy.stat().st_size:
            raise RuntimeError(f"{name} arrived at a different size.")
        if origin.is_dir() and _count(origin) != _count(copy):
            raise RuntimeError(f"{name} arrived with a different number of files.")


def _count(folder: Path) -> int:
    return sum(1 for item in folder.rglob("*") if item.is_file())


def _size(folder: Path) -> int:
    try:
        return sum(item.stat().st_size for item in folder.rglob("*") if item.is_file())
    except OSError:
        return 0
