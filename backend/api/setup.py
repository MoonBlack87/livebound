"""First-start setup before a data directory or database exists."""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from .. import backup as backup_service
from .. import config, db, usage_ping
from ..civitai import oauth
from ..store import sources as source_store
from .common import api_error

router = APIRouter(prefix="/api/setup", tags=["setup"])


@router.get("")
def status(request: Request) -> dict[str, Any]:
    database_ready = not request.app.state.setup_required
    wizard_pending = database_ready and db.get_setting("setup_completed") == "0"
    return {
        "setup_required": bool(request.app.state.setup_required or wizard_pending),
        "data_directory_ready": database_ready,
        "default_path": str(config.default_data_dir()),
    }


@router.post("")
async def complete(
    request: Request,
    path: Annotated[str, Form(min_length=1, max_length=4096)],
    backup: Annotated[UploadFile | None, File()] = None,
) -> dict[str, Any]:
    """Choose the data directory and optionally import a multipart backup."""
    staged = await _stage_backup(backup)
    try:
        if staged is not None:
            _validate_backup(staged)
        return await run_in_threadpool(_complete, request, path, staged)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


async def _stage_backup(upload: UploadFile | None) -> Path | None:
    if upload is None:
        return None
    path: Path | None = None
    staged = False
    try:
        with tempfile.NamedTemporaryFile(
            prefix="livebound-setup-", suffix=".db", delete=False
        ) as out:
            path = Path(out.name)
            copied = 0
            while chunk := await upload.read(1024**2):
                copied += len(chunk)
                if copied > config.SETUP_BACKUP_MAX_BYTES:
                    limit = _byte_limit_label(config.SETUP_BACKUP_MAX_BYTES)
                    raise api_error(
                        "setup_backup_too_large",
                        f"The backup exceeds the {limit} import limit. "
                        "Choose a smaller Livebound backup or start empty.",
                        413,
                        limit=limit,
                    )
                out.write(chunk)
        staged = True
    finally:
        closed = False
        try:
            await upload.close()
            closed = True
        finally:
            if (not staged or not closed) and path is not None:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
    return path


def _byte_limit_label(size: int) -> str:
    if size % 1024**2 == 0:
        return f"{size // 1024**2} MiB"
    return f"{size} bytes"


@router.post("/finish")
def finish(request: Request) -> dict[str, bool]:
    """Close the wizard only after its three database-backed minimums exist."""
    with request.app.state.setup_lock:
        if request.app.state.setup_required:
            raise api_error(
                "setup_required",
                "Choose the data directory before completing first-start setup.",
                428,
            )
        with db.transaction() as connection:
            completed = connection.execute(
                "SELECT value FROM settings WHERE key='setup_completed'"
            ).fetchone()
            if completed is None or completed["value"] != "0":
                raise api_error(
                    "setup_already_completed",
                    "Setup has already been completed.",
                    409,
                )
            roots = source_store.list_roots()
            missing = []
            if not oauth.connected():
                missing.append("CivitAI connection")
            if not any(not root["is_archive"] for root in roots):
                missing.append("image folder")
            if not any(root["is_archive"] for root in roots):
                missing.append("archive folder")
            if missing:
                raise api_error(
                    "setup_incomplete",
                    f"Complete first-start setup: {', '.join(missing)}.",
                    409,
                )
            connection.execute(
                "UPDATE settings SET value='1' WHERE key='setup_completed'"
            )
        request.app.state.finish_setup()
    usage_ping.send_at_start()
    return {"setup_required": False}


def _validate_backup(path: Path) -> None:
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            checked = connection.execute("PRAGMA quick_check").fetchone()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise api_error(
            "bad_setup_backup",
            f"The supplied backup is not a readable SQLite database: {exc}",
            400,
        ) from exc
    required_tables = set(backup_service.BACKUP_REQUIRED_TABLES)
    if (
        checked is None
        or checked[0] != "ok"
        or not required_tables.issubset(tables)
        or not 1 <= schema_version <= db.SCHEMA_VERSION
    ):
        raise api_error(
            "bad_setup_backup",
            "The supplied file is not an intact Livebound backup. Choose another backup.",
            400,
        )


def _complete(request: Request, raw_path: str, staged: Path | None) -> dict[str, Any]:
    with request.app.state.setup_lock:
        if not request.app.state.setup_required:
            raise api_error(
                "setup_already_completed",
                "Setup has already been completed. Use Settings to move the data directory.",
                409,
            )

        target, target_existed = _target(raw_path)
        try:
            target.mkdir(parents=True, exist_ok=True)
            with config.provisional_data_dir(target):
                db.relocated()
                if staged is not None:
                    shutil.copyfile(staged, config.db_path(create_parent=False))
                request.app.state.initialise_database()
                # Missing means an installation predates this wizard and is
                # already complete. Only this decision creates the pending row.
                db.set_setting("setup_completed", "0")
                # A restored database can carry an earlier choice. First-start
                # consent is asked afresh and stays off until it is chosen here.
                usage_ping.set_enabled(False)
                config.set_data_dir(target)
        except (OSError, sqlite3.Error, db.MigrationBackupError) as exc:
            _rollback(target, keep_directory=target_existed)
            raise api_error(
                "setup_failed",
                f"Could not initialise the data directory {target}: {exc}. Choose it again.",
                500,
                path=str(target),
            ) from exc

        request.app.state.finish_setup()
        return {
            "setup_required": True,
            "data_directory_ready": True,
            "data_dir": str(target),
            "restored": staged is not None,
        }


def _target(raw: str) -> tuple[Path, bool]:
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise api_error(
            "bad_setup_data_dir",
            "Give an absolute path for the data directory.",
            400,
            path=raw,
        )
    try:
        target = target.resolve()
        existed = target.exists()
        if existed and (not target.is_dir() or any(target.iterdir())):
            raise api_error(
                "bad_setup_data_dir",
                f"The data directory must be empty: {target}. Choose an empty directory.",
                400,
                path=str(target),
            )
    except OSError as exc:
        raise api_error(
            "bad_setup_data_dir",
            f"Cannot use the data directory {target}: {exc}. Choose another directory.",
            400,
            path=str(target),
        ) from exc
    return target, existed


def _rollback(target: Path, *, keep_directory: bool) -> None:
    db.close_connection()
    db.relocated()
    if target.is_dir():
        for child in target.iterdir():
            with contextlib.suppress(OSError):
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        if not keep_directory:
            with contextlib.suppress(OSError):
                target.rmdir()
