"""Database backups.

The database *is* this application's state: posts, plans, schedules, the
duplicate history and the settings. Images live in the source folders and are not
part of a backup - they are never modified, except when archiving, and there they
are moved rather than written.

Backups go through SQLite's own backup interface, not by copying the file. In WAL
mode part of the data sits in a side file, so a plain file copy would be
incomplete depending on timing. The interface yields a self-consistent copy even
while work is going on.

The CivitAI OAuth connection is removed from every backup. Restoring therefore
leaves the application disconnected and the maintainer connects it again.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config, db
from .civitai import oauth

#: How many automatic backups are kept. Manual ones are never pruned.
KEEP_AUTOMATIC = 10

PREFIX = "publisher"
REGULAR_REASON = "regular"
REGULAR_INTERVAL = timedelta(days=1)

# Archive parts may be several gigabytes; only this much is held while copying.
_SPLIT_BUFFER_BYTES = 1024**2

# Every database backup created here contains these application tables. The
# setup importer uses the same signature instead of accepting any SQLite file
# that happens to have a settings table.
BACKUP_REQUIRED_TABLES = (
    "settings",
    "posts",
    "images",
    "image_usage",
    "source_roots",
)


def backup_dir() -> Path:
    path = config.data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def create(reason: str = "manual", *, automatic: bool = False) -> dict[str, Any]:
    """Create a backup and report what came out of it."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in reason)[:40] or "backup"
    kind = "auto" if automatic else "manual"
    source = db.get_connection()
    target = _reserve_path(backup_dir() / f"{PREFIX}-{stamp}-{kind}-{slug}.db")
    destination = None
    ready = False
    try:
        destination = sqlite3.connect(target)
        # SQLite's own backup: consistent even while a write is in flight.
        source.backup(destination)
        _strip_token(destination)
        _strip_rebuildable_data(destination)
        ready = True
    finally:
        try:
            if destination is not None:
                destination.close()
        finally:
            if not ready:
                # Any unfinished copy may still carry a credential in its
                # database or WAL, so none of its files may be left behind.
                _remove_database_and_sidecars(target)

    if automatic:
        prune()
    return describe(target)


def _reserve_path(target: Path) -> Path:
    """Claim a backup filename, adding a counter only after a collision."""
    candidate = target
    counter = 2
    while True:
        try:
            candidate.touch(exist_ok=False)
            return candidate
        except FileExistsError:
            candidate = target.with_name(f"{target.stem}-{counter}{target.suffix}")
            counter += 1


def _strip_token(conn: sqlite3.Connection) -> None:
    """Remove every credential from a database copy before it leaves the app.

    The rows that make up an OAuth connection are covered (`SET-05`). A refresh
    token is a long-lived credential - thirty days - so it must not leave with a
    backup. The registered client id goes too: it is not secret, but a restored
    copy pointing at a client the account no longer owns only confuses. The
    legacy API key row is still stripped from databases created by older builds.
    """
    settings_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
    ).fetchone()
    if settings_table is not None:
        keys = ("civitai_api_token", *oauth.BACKUP_STRIPPED_KEYS)
        placeholders = ",".join("?" for _ in keys)
        conn.execute(f"DELETE FROM settings WHERE key IN ({placeholders})", keys)
        conn.commit()


def _strip_rebuildable_data(conn: sqlite3.Connection) -> None:
    """Remove the search index from a copy and release the pages it occupied."""
    conn.execute("DROP TABLE IF EXISTS image_search")
    conn.commit()
    conn.execute("VACUUM")


def _sidecars(path: Path) -> tuple[Path, Path]:
    return Path(f"{path}-wal"), Path(f"{path}-shm")


def _remove_database_and_sidecars(path: Path) -> bool:
    """Remove one database copy and, only then, the sidecars that belong to it."""
    try:
        path.unlink()
    except FileNotFoundError:
        removed = False
    except OSError:
        return False
    else:
        removed = True

    for sidecar in _sidecars(path):
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            continue
    return removed


def _remove_unused_sidecars() -> None:
    """Remove orphaned sidecars and empty WAL state from closed backups."""
    folder = backup_dir()
    for wal in folder.glob(f"{PREFIX}-*.db-wal"):
        database = Path(str(wal)[: -len("-wal")])
        try:
            has_uncheckpointed_data = database.exists() and wal.stat().st_size > 0
        except OSError:
            continue
        if has_uncheckpointed_data:
            continue
        for sidecar in _sidecars(database):
            try:
                sidecar.unlink()
            except OSError:
                continue

    for shm in folder.glob(f"{PREFIX}-*.db-shm"):
        database = Path(str(shm)[: -len("-shm")])
        wal = Path(f"{database}-wal")
        if database.exists() and wal.exists():
            continue
        try:
            shm.unlink()
        except OSError:
            continue


def create_regular_if_due(*, now: datetime | None = None) -> dict[str, Any] | None:
    """Create the daily backup when none was taken in the last interval."""
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - REGULAR_INTERVAL.total_seconds()
    recent = any(
        path.stat().st_mtime > cutoff
        for path in backup_dir().glob(f"{PREFIX}-*-auto-{REGULAR_REASON}.db")
    )
    if recent:
        prune()
        return None
    return create(reason=REGULAR_REASON, automatic=True)


def list_backups() -> list[dict[str, Any]]:
    return sorted(
        (describe(path) for path in backup_dir().glob(f"{PREFIX}-*.db")),
        key=lambda item: item["created_at"],
        reverse=True,
    )


def describe(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "automatic": "-auto-" in path.name,
        "contents": _peek(path),
    }


def _peek(path: Path) -> dict[str, Any]:
    """Roughly what is inside, so a restore shows what you are landing on."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return {}
    try:
        counts: dict[str, Any] = {}
        for table in BACKUP_REQUIRED_TABLES[1:]:
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                counts[table] = None
        counts["schema_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        return counts
    finally:
        conn.close()


def prune(keep: int | None = None) -> int:
    """Remove old automatic backups. Manual ones are left alone."""
    keep = KEEP_AUTOMATIC if keep is None else keep
    automatic = sorted(
        (path for path in backup_dir().glob(f"{PREFIX}-*-auto-*.db")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for path in automatic[keep:]:
        removed += int(_remove_database_and_sidecars(path))

    _remove_unused_sidecars()
    return removed


def restore(name: str) -> dict[str, Any]:
    """Restore a backup.

    The current state is backed up first - a restore must not destroy the only
    copy that was left.

    The restore goes *through* the connection rather than overwriting the file.
    Other threads hold open connections to it; swap the file out from under them
    and they answer with an I/O error. Through SQLite's backup interface they
    simply see the new contents.

    The migration then runs again, so an older backup also ends up at the schema
    of the running version.
    """
    source = backup_dir() / Path(name).name
    if source.parent.resolve() != backup_dir().resolve():
        raise ValueError("Only backups from the backup folder")
    if not source.exists():
        raise FileNotFoundError(f"Backup not found: {name}")

    safety = create(reason="before-restore", automatic=True)

    destination = db.get_connection()
    origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        origin.backup(destination)
        # Old backups may predate the no-token rule. A restore still always
        # leaves the application ready for a freshly entered credential.
        _strip_token(destination)
    finally:
        origin.close()
        _remove_unused_sidecars()

    # An older backup can carry an older schema.
    db.init_db()

    return {
        "restored": source.name,
        "safety_copy": safety["name"],
        "schema_version": db.get_connection().execute("PRAGMA user_version").fetchone()[0],
        "token_required": True,
    }


def delete(name: str) -> bool:
    """Delete a backup, returning whether its file was present."""
    path = backup_dir() / Path(name).name
    if path.parent.resolve() != backup_dir().resolve():
        raise ValueError("Only backups from the backup folder")
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


# --- backing up the image archive -------------------------------------------
#
# The database backup above protects the state, not the images. For an archive
# that increasingly holds pictures existing nowhere else - the ones generated
# straight on CivitAI, for instance - that is not enough.


def archive_status() -> dict[str, Any]:
    """How large the archive is, without touching it."""
    from .store import sources as source_store

    archive = source_store.archive_root()
    if archive is None:
        return {"configured": False, "reason": "No archive folder configured."}

    root = Path(archive["path"])
    if not root.is_dir():
        return {"configured": True, "exists": False, "path": str(root)}

    files = 0
    total = 0
    for path in _archive_files(root):
        files += 1
        total += path.stat().st_size
    return {
        "configured": True,
        "exists": True,
        "path": str(root),
        "files": files,
        "bytes": total,
    }


def create_archive_zip(job, *, split_mb: int | None = None) -> dict[str, Any]:
    """Pack the archive into a zip, optionally split into parts.

    No compression: images are compressed already, and a second pass costs only
    time. The zip bundles here, it does not shrink.

    ``split_mb`` cuts the finished file into fixed-size pieces - for media that
    dislike large files. Reassemble with ``cat part.* > whole.zip``; a real
    multi-part archive would not be readable without an extra tool.
    """
    import zipfile

    from .store import sources as source_store

    archive = source_store.archive_root()
    if archive is None:
        job.error = "No archive folder configured."
        return {}
    root = Path(archive["path"])
    if not root.is_dir():
        job.error = f"Archive folder not found: {root}"
        return {}

    files = sorted(_archive_files(root))
    job.total = len(files)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = backup_dir() / f"archive-{stamp}.zip"

    job.stage = "Packing the archive"
    written = 0
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
        for index, path in enumerate(files, start=1):
            if job.should_stop():
                break
            job.processed = index
            try:
                bundle.write(path, path.relative_to(root))
                written += 1
            except OSError as exc:
                job.failed += 1
                job.log(status="error", path=str(path), message=str(exc))

    result: dict[str, Any] = {
        "path": str(target),
        "files": written,
        "bytes": target.stat().st_size,
        "parts": [],
    }

    if split_mb:
        job.stage = "Splitting the file"
        result["parts"] = _split(target, split_mb)
        target.unlink(missing_ok=True)
        result["path"] = None

    job.result = result
    job.stage = "Done"
    return result


def _archive_files(root: Path) -> Iterator[Path]:
    """Yield ordinary files from directories that stay inside the archive."""
    boundary = root.resolve()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            resolved = boundary if directory == root else directory.resolve()
        except OSError:
            # One unreadable archive directory must not abort the whole backup.
            continue
        if not resolved.is_relative_to(boundary):
            continue

        try:
            with os.scandir(directory) as scan:
                entries = list(scan)
        except OSError:
            # One unreadable archive directory must not abort the whole backup.
            continue

        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                path = Path(entry.path)
                if entry.is_file(follow_symlinks=False):
                    yield path
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(path)
            except OSError:
                # One unreadable archive item must not abort the whole backup.
                continue


def _split(path: Path, size_mb: int) -> list[dict[str, Any]]:
    """Cut a file into numbered pieces.

    Back together: ``cat name.zip.* > name.zip`` - the order is right because the
    suffixes are three digits and ascending.
    """
    part_size = max(1, size_mb) * 1024**2
    parts: list[dict[str, Any]] = []
    with path.open("rb") as source:
        index = 1
        while True:
            data = source.read(min(_SPLIT_BUFFER_BYTES, part_size))
            if not data:
                break
            part = path.with_name(f"{path.name}.{index:03d}")
            written = 0
            with part.open("wb") as destination:
                while data:
                    destination.write(data)
                    written += len(data)
                    remaining = part_size - written
                    if remaining == 0:
                        break
                    data = source.read(min(_SPLIT_BUFFER_BYTES, remaining))
            parts.append({"name": part.name, "bytes": written})
            index += 1
    return parts
