"""Settings and the CivitAI account."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import backup, config, db, duplicates, jobs, relocate, usage_ping, watchers
from ..civitai import account, oauth, ratelimit, scopes
from ..civitai.errors import CivitaiError
from ..models import MoveDataDir, Ok, SettingsPatch
from ..store import images as image_store
from ..store import model_roots as model_store
from ..store import sources as source_store
from .common import api_error, edit_target_image_ids, guarded, start_job

router = APIRouter(prefix="/api", tags=["settings"])


def _auth_mode() -> str:
    """Whether the next authenticated call has an OAuth connection to carry."""
    return "oauth" if oauth.connected() else "none"


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    source_roots = source_store.list_roots()
    return {
        "civitai_auth_mode": _auth_mode(),
        "civitai_username": db.get_setting("civitai_username"),
        "site_base": config.site_base(),
        "site_base_default": config.DEFAULT_SITE_BASE,
        "phash_threshold": int(
            db.get_setting("phash_threshold") or config.PHASH_DISTANCE_THRESHOLD
        ),
        "require_title": db.get_setting("require_title") == "1",
        # Off unless it says "1": a fresh installation checks nothing by itself.
        "watch_posts_enabled": db.get_setting(watchers.SETTING_WATCH_POSTS) == "1",
        # Displayed by the frontend help text, so the promised ladder comes from
        # the same constant as the implementation rather than drifting in prose.
        "watch_post_retry_minutes": [
            int(seconds // 60) for seconds in watchers.RETRY_DELAYS_SECONDS
        ],
        "watch_post_rating_refresh_minutes": int(
            watchers.RATING_REFRESH_SECONDS // 60
        ),
        # Drives the one background poll in the frontend: a run nobody pressed
        # is only worth looking for when something is actually watched.
        "watchers_active": watchers.anything_watched(),
        # Derived, never typed: an About section that states a version of its
        # own would be wrong the first time somebody forgets to change it.
        "app_version": config.APP_VERSION,
        "app_licence": config.APP_LICENCE,
        "app_rights_holder": config.APP_RIGHTS_HOLDER,
        "debug_logging": db.get_setting("debug_logging") == "1",
        "usage_ping_enabled": usage_ping.enabled(),
        "ui_language": db.get_setting("ui_language") or "en",
        "ui_grid_size": db.int_setting("ui_grid_size", 0),
        "ui_page_size": db.int_setting("ui_page_size", 200),
        "ui_discover_limit": db.int_setting(
            "ui_discover_limit", config.DISCOVER_DEFAULT_LIMIT
        ),
        "ui_discover_days": db.int_setting("ui_discover_days", 0),
        "rate_limit_posts_per_day": db.get_setting("rate_limit_posts_per_day"),
        "llm_python": db.get_setting("llm_python") or "",
        "llm_model_dir": db.get_setting("llm_model_dir") or "",
        "llm_device": db.get_setting("llm_device") or "auto",
        "llm_load_in_4bit": db.get_setting("llm_load_in_4bit") != "0",
        "llm_trust_remote_code": db.get_setting("llm_trust_remote_code") == "1",
        "llm_endpoint": db.get_setting("llm_endpoint") or "",
        "llm_endpoint_model": db.get_setting("llm_endpoint_model") or "",
        "llm_variants": db.int_setting("llm_variants", 1),
        "metadata_excluded_fields": db.get_json_setting("metadata_excluded_fields", []),
        "prompt_exclusions": db.get_json_setting("prompt_exclusions", []),
        "trash_folder": db.get_setting("trash_folder") or "",
        "adopted_folder": db.get_setting("adopted_folder") or "",
        "setup": {
            "source_folder": any(not root.get("is_archive") for root in source_roots),
            "archive_folder": any(root.get("is_archive") for root in source_roots),
            "adopted_folder": bool(db.get_setting("adopted_folder")),
            "model_root": bool(model_store.list_roots()),
        },
        "generation_time_patterns": db.get_json_setting(
            "generation_time_patterns", list(config.DEFAULT_GENERATION_TIME_PATTERNS)
        ),
        "limits": {
            "min_schedule_minutes": config.POST_MINIMUM_SCHEDULE_MINUTES,
            "max_schedule_months": config.POST_MAXIMUM_SCHEDULE_MONTHS,
            "max_upload_bytes": config.MCP_UPLOAD_MAX_BYTES,
        },
    }


@router.get("/settings/metadata-fields")
def get_metadata_fields(
    post_id: int | None = None, image_ids: str | None = None
) -> dict[str, Any]:
    """The recorded metadata fields, library-wide or scoped to an edit.

    Scoped through the same rule the edit itself uses
    (``edit_target_image_ids``), so a completion list offers exactly the fields
    of the images that would be changed.
    """
    if post_id is None and image_ids is None:
        return {"items": image_store.metadata_field_inventory()}
    wanted = [int(part) for part in (image_ids or "").split(",") if part.strip().isdigit()]
    return {
        "items": image_store.metadata_field_inventory(
            edit_target_image_ids(wanted, post_id)
        )
    }


@router.put("/settings")
def put_settings(patch: SettingsPatch) -> dict[str, Any]:
    prompt_exclusions = None
    if patch.prompt_exclusions is not None:
        prompt_exclusions = list(
            dict.fromkeys(value.strip() for value in patch.prompt_exclusions if value.strip())
        )
        for expression in prompt_exclusions:
            try:
                re.compile(expression, re.IGNORECASE)
            except re.error as exc:
                raise api_error(
                    "invalid_prompt_exclusion",
                    f"Invalid prompt exclusion {expression!r}: {exc}.",
                    400,
                    expression=expression,
                    reason=str(exc),
                ) from exc

    generation_time_patterns = None
    if patch.generation_time_patterns is not None:
        generation_time_patterns = list(
            dict.fromkeys(
                value.strip() for value in patch.generation_time_patterns if value.strip()
            )
        )
        for pattern in generation_time_patterns:
            if duplicates.compile_generation_time_pattern(pattern) is None:
                raise api_error(
                    "invalid_generation_time_pattern",
                    f"Invalid generation-time filename pattern: {pattern!r}. "
                    "Use yyyy, mm, dd, HH, MM and SS exactly once.",
                    400,
                    pattern=pattern,
                )

    trash_folder_update = patch.trash_folder is not None
    trash_folder: Path | None = None
    if trash_folder_update and patch.trash_folder:
        raw = patch.trash_folder.strip()
        if raw:
            folder = Path(raw).expanduser()
            if not folder.is_absolute():
                raise api_error(
                    "bad_trash_folder",
                    "Give an absolute path for the trash folder.",
                    400,
                )
            try:
                folder.mkdir(parents=True, exist_ok=True)
                folder = folder.resolve(strict=True)
            except OSError as exc:
                raise api_error(
                    "bad_trash_folder",
                    f"Cannot create the trash folder {folder}: {exc}",
                    400,
                ) from exc
            if not folder.is_dir():
                raise api_error(
                    "bad_trash_folder", f"The trash path is not a folder: {folder}", 400
                )
            for source in source_store.list_roots():
                root = Path(source["path"]).resolve()
                if folder == root or folder in root.parents:
                    raise api_error(
                        "bad_trash_folder",
                        f"The trash folder contains source folder {root}. "
                        "Choose a folder inside a source or separate from it.",
                        400,
                    )
            trash_folder = folder

    adopted_folder_update = patch.adopted_folder is not None
    adopted_folder: Path | None = None
    if adopted_folder_update and patch.adopted_folder:
        adopted_folder = _validate_adopted_folder(patch.adopted_folder)

    if patch.site_base is not None:
        value = patch.site_base.strip().rstrip("/")
        if value and not config.site_host_allowed(value):
            raise api_error(
                "bad_site",
                "The CivitAI credential is only sent to civitai.red or civitai.com, "
                "over https. "
                "Set LIVEBOUND_ALLOWED_SITES to point somewhere else.",
                400,
            )
        db.set_setting("site_base", value or None)
        # The account hangs off the domain; the answer from there could differ,
        # so do not carry the old one forward.
        db.set_setting("account_json", None)

    if patch.phash_threshold is not None:
        db.set_setting("phash_threshold", str(patch.phash_threshold))
    if patch.require_title is not None:
        db.set_setting("require_title", "1" if patch.require_title else "0")
    if patch.watch_posts_enabled is not None:
        enabled = db.get_setting(watchers.SETTING_WATCH_POSTS) == "1"
        if enabled != patch.watch_posts_enabled:
            # Off and on is the existing, visible gesture for trying a post
            # again after the bounded ladder gave up.
            watchers.reset_state()
        db.set_setting(
            watchers.SETTING_WATCH_POSTS, "1" if patch.watch_posts_enabled else "0"
        )
    if patch.debug_logging is not None:
        db.set_setting("debug_logging", "1" if patch.debug_logging else "0")
    if patch.usage_ping_enabled is not None:
        usage_ping.set_enabled(patch.usage_ping_enabled)
    if patch.ui_language is not None:
        db.set_setting("ui_language", patch.ui_language or None)
    if patch.ui_grid_size is not None:
        db.set_setting("ui_grid_size", str(patch.ui_grid_size))
    if patch.ui_page_size is not None:
        db.set_setting("ui_page_size", str(patch.ui_page_size))
    if patch.ui_discover_limit is not None:
        db.set_setting("ui_discover_limit", str(patch.ui_discover_limit))
    if patch.ui_discover_days is not None:
        db.set_setting("ui_discover_days", str(patch.ui_discover_days))
    if patch.rate_limit_posts_per_day is not None:
        db.set_setting("rate_limit_posts_per_day", str(patch.rate_limit_posts_per_day))
    for key in ("llm_python", "llm_model_dir", "llm_device", "llm_endpoint",
                "llm_endpoint_model"):
        value = getattr(patch, key)
        if value is not None:
            db.set_setting(key, value or None)
    if patch.llm_load_in_4bit is not None:
        db.set_setting("llm_load_in_4bit", "1" if patch.llm_load_in_4bit else "0")
    if patch.llm_trust_remote_code is not None:
        db.set_setting("llm_trust_remote_code", "1" if patch.llm_trust_remote_code else "0")
    if patch.llm_variants is not None:
        db.set_setting("llm_variants", str(patch.llm_variants))
    if patch.metadata_excluded_fields is not None:
        fields = list(
            dict.fromkeys(
                value.strip() for value in patch.metadata_excluded_fields if value.strip()
            )
        )
        db.set_json_setting("metadata_excluded_fields", fields)
    if prompt_exclusions is not None:
        db.set_json_setting("prompt_exclusions", prompt_exclusions)
    if trash_folder_update:
        db.set_setting("trash_folder", str(trash_folder) if trash_folder else None)
    if adopted_folder_update:
        db.set_setting("adopted_folder", str(adopted_folder) if adopted_folder else None)
    if generation_time_patterns is not None:
        db.set_json_setting("generation_time_patterns", generation_time_patterns)

    return get_settings()


def _validate_adopted_folder(raw: str) -> Path:
    """Create and validate the folder before it becomes a fetch destination."""
    folder = Path(raw.strip()).expanduser()
    if not folder.is_absolute():
        raise api_error(
            "bad_adopted_folder",
            "Give an absolute path for the adopted folder.",
            400,
        )
    try:
        folder.mkdir(parents=True, exist_ok=True)
        folder = folder.resolve(strict=True)
    except OSError as exc:
        raise api_error(
            "bad_adopted_folder",
            f"Cannot create the adopted folder {folder}: {exc}",
            400,
        ) from exc
    if not folder.is_dir() or not os.access(folder, os.W_OK | os.X_OK):
        raise api_error(
            "bad_adopted_folder",
            f"The adopted folder is not a writable folder: {folder}. "
            "Choose a writable folder outside image and archive folders.",
            400,
        )

    archive = source_store.archive_root()
    if archive is not None:
        archive_path = Path(archive["path"]).resolve()
        if (
            folder == archive_path
            or archive_path in folder.parents
            or folder in archive_path.parents
        ):
            raise api_error(
                "bad_adopted_folder",
                f"The adopted folder overlaps the archive folder {archive_path}. "
                "Choose a separate folder outside image and archive folders.",
                400,
            )

    for source in source_store.list_roots():
        if source.get("is_archive"):
            continue
        root = Path(source["path"]).resolve()
        # Equality is safe: the fetched file is indexed under that one root,
        # rather than creating a nested root which would scan it twice.
        if folder != root and (root in folder.parents or folder in root.parents):
            raise api_error(
                "bad_adopted_folder",
                f"The adopted folder overlaps image folder {root}. "
                "Choose a separate folder outside image and archive folders.",
                400,
            )
    return folder


@router.get("/account")
def get_account() -> dict[str, Any]:
    cached = account.cached()
    if cached is None and oauth.connected():
        try:
            cached = account.refresh()
        except CivitaiError:
            # Resolving profile details is useful but secondary: a temporary
            # read failure must not turn a completed OAuth grant into a failed
            # connection. The next account read tries again while uncached.
            cached = None
    return {
        "account": cached,
        "username": account.username(),
        "budget": ratelimit.budget() if cached else None,
        "auth_mode": _auth_mode(),
        "scopes": scopes.describe(cached),
    }


@router.get("/settings/data-dir")
def data_dir_status() -> dict[str, Any]:
    status = relocate.status()
    status["source"] = config.data_dir_source()
    return status


@router.post("/settings/data-dir")
def move_data_dir(payload: MoveDataDir) -> dict[str, Any]:
    """Move the whole data directory somewhere else.

    Refused while anything else is running: the move closes the database, and a
    job mid-transaction would be looking at a file that is about to be replaced.
    """
    try:
        relocate.check_target(payload.path)
    except ValueError as exc:
        raise api_error("bad_data_dir", str(exc), 400) from exc
    return start_job(
        "move-data",
        lambda job: relocate.run(job, payload.path),
        code="job_running",
        exclusive="*",
    )


@router.get("/browse-dirs")
def browse_dirs(path: str = "") -> dict[str, Any]:
    """Server-side folder picker: the browser cannot read the filesystem itself.

    Only directories are listed and only their names are returned, so this cannot
    be used to read file contents.
    """
    base = Path(path).expanduser() if path else Path.home()
    try:
        base = base.resolve(strict=True)
    except OSError:
        raise api_error("folder_not_found", f"Folder not found: {path}", 404, path=path) from None
    if not base.is_dir():
        raise api_error("not_a_folder", f"Not a folder: {base}", 400, path=str(base))

    try:
        entries = sorted(
            (
                entry
                for entry in base.iterdir()
                if entry.is_dir() and not entry.name.startswith(".")
            ),
            key=lambda entry: entry.name.lower(),
        )
    except PermissionError:
        raise api_error("folder_forbidden", f"No access to {base}", 403, path=str(base)) from None

    return {
        "path": str(base),
        "parent": str(base.parent) if base.parent != base else None,
        "entries": [{"name": entry.name, "path": str(entry)} for entry in entries[:500]],
    }


@router.get("/backups")
def list_backups() -> dict[str, Any]:
    return {"items": backup.list_backups(), "folder": str(backup.backup_dir())}


@router.post("/backups")
def create_backup(reason: str = "manual") -> dict[str, Any]:
    """Create a backup without the CivitAI API token."""
    return guarded(backup.create, reason)


@router.post("/backups/{name}/restore")
def restore_backup(name: str) -> dict[str, Any]:
    """Restore without the API token. The current state is backed up first.

    Open connections stay valid - the restore goes through the connection, not by
    overwriting the file.
    """
    # The same guard the data-directory move has, for the same reason: this
    # replaces the whole database, and a push thread mid-run would carry on
    # uploading against a record that no longer exists. A real slot, not a
    # question: the restore runs in the request, so nothing else may start
    # while it does.
    try:
        with jobs.reserve("restore"):
            return backup.restore(name)
    except jobs.AlreadyRunning as exc:
        raise api_error(
            "job_running", f"Wait for job {exc.job.id} to finish first.", 409, job=exc.job.id
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/backups/{name}", response_model=Ok)
def delete_backup(name: str) -> Ok:
    try:
        deleted = backup.delete(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not deleted:
        raise api_error("backup_not_found", "This backup no longer exists.", 404)
    return Ok()


@router.get("/backups/archive/status")
def archive_backup_status() -> dict[str, Any]:
    return backup.archive_status()


@router.post("/backups/archive")
def create_archive_backup(split_mb: int | None = None) -> dict[str, Any]:
    """Back the archive up as a zip, optionally split into parts.

    The database backup protects the state, not the images - and the archive
    increasingly holds pictures that exist nowhere else.
    """
    return start_job(
        "archive-backup",
        lambda job: backup.create_archive_zip(job, split_mb=split_mb),
        code="archive_backup_running",
    )
