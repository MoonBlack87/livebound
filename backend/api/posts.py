"""Post CRUD, images, tags and the live checklist."""

from __future__ import annotations

import contextlib
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

from .. import archive as image_archive
from .. import watchers
from ..models import (
    AdoptRequest,
    ArchivePreview,
    ArchiveSelection,
    ImageList,
    ImageOrder,
    Ok,
    PostCreate,
    PostPatch,
    RescheduleRequest,
    TagList,
)
from ..posts import (
    fetch_images,
    lifecycle,
    matching,
    operations,
    reconcile,
    schedule,
    sync,
    validation,
)
from ..resources import cache as resource_cache
from ..store import events
from ..store import posts as post_store
from ..store import sources as source_store
from .common import api_error, guard_post_mutation, guarded, start_job

router = APIRouter(prefix="/api", tags=["posts"])


def _check_image_limit(image_ids: list[int]) -> None:
    try:
        post_store.check_image_limit(image_ids)
    except post_store.PostImageLimitExceeded as exc:
        raise api_error(
            "post_image_limit",
            str(exc),
            400,
            limit=exc.limit,
            count=exc.count,
            excess=exc.excess,
        ) from exc


def _set_images_and_resolve(post_id: int, image_ids: list[int]) -> dict[str, int]:
    current = post_store.images(post_id)
    unmatched_ids = [
        int(image["id"]) for image in current if image.get("image_id") is None
    ]
    _check_image_limit([*image_ids, *unmatched_ids])
    existing = {
        int(image["image_id"])
        for image in current
        if image.get("image_id") is not None
    }
    post = post_store.get(post_id)
    if post and post.get("remote_post_id"):
        guarded(operations.reorder_images, post_id, image_ids)
    else:
        post_store.set_images(post_id, image_ids)
    added = list(dict.fromkeys(image_id for image_id in image_ids if image_id not in existing))
    return resource_cache.resolve_for_images(added)


@router.get("/posts")
def list_posts(state: str | None = None) -> dict[str, Any]:
    states = [s.strip() for s in state.split(",")] if state else None
    items = post_store.list_posts(states=states)
    for item in items:
        _decorate(item)
    return {"items": items, "states": _state_meta()}


@router.post("/posts")
def create_post(payload: PostCreate) -> dict[str, Any]:
    _check_image_limit(payload.image_ids)
    post_id = post_store.create(**payload.model_dump(exclude={"image_ids", "tags"}))
    resolution = resource_cache.resolve_for_images([])
    if payload.image_ids:
        resolution = _set_images_and_resolve(post_id, payload.image_ids)
    if payload.tags:
        post_store.set_tags(post_id, payload.tags)
    if payload.schedule_offset_minutes is not None or payload.scheduled_at:
        _maybe_ready(post_id)
    post = get_post(post_id)
    post["resource_resolution"] = resolution
    return post


# Registered before /posts/{post_id}: FastAPI matches in declaration order, and
# "archive" would otherwise be read as a post id.
@router.get("/posts/archive")
def list_archive(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """Filed-away posts, newest first.

    Deliberately its own listing rather than a filter on the board: the board
    answers "what is going on", this answers "what did I publish", and the two
    grow at very different rates.
    """
    from_date, to_date = _validated_archive_range(from_date, to_date)
    rows, total = post_store.list_archived_posts(
        limit=min(limit, 200), offset=offset, from_date=from_date, to_date=to_date
    )
    # Resolved once for the page, not once per row: this is a listing of up to
    # 200 posts, and `resolve()` on a Windows drive is an order dearer than on
    # a Linux one.
    archive_root = source_store.archive_root()
    archive_base = Path(archive_root["path"]).resolve() if archive_root else None
    for row in rows:
        _decorate_archive(row, archive_base)
    return {"items": rows, "total": total, "offset": offset}


@router.post("/posts/archive-published")
def archive_published(days: int = Query(30, ge=0, le=3650)) -> dict[str, Any]:
    """File away everything published longer ago than ``days``.

    The board shows the current situation; a post from three months ago is not
    part of it. Nothing is deleted and nothing on CivitAI is touched - the post
    simply moves to the archive view, where its link still works.
    """
    cutoff = schedule.iso_z(schedule.utcnow() - timedelta(days=max(0, days)))
    published = []
    offset = 0
    page_size = 500
    while True:
        page = post_store.list_posts(
            states=[lifecycle.PUBLISHED], limit=page_size, offset=offset
        )
        published.extend(page)
        if len(page) < page_size:
            break
        offset += len(page)

    moved = []
    for post in published:
        when = post.get("remote_published_at") or post.get("scheduled_at")
        if not when or when > cutoff:
            continue
        try:
            lifecycle.transition(post["id"], lifecycle.ARCHIVED, reason="older than the cutoff")
            moved.append(post["id"])
        except lifecycle.TransitionError:
            continue
    return {"archived": len(moved), "post_ids": moved, "cutoff": cutoff}


@router.post("/posts/archive-selected")
def archive_selected(payload: ArchiveSelection) -> dict[str, Any]:
    """Move exactly these posts to ``archived``.

    A state change, not a file move: the images stay where they are until an
    archive run is started explicitly (`ARC-04`). The two are separate actions
    in this application and the wording must keep them apart.

    Every write goes through ``lifecycle.transition`` like any other, and a post
    that may not make the move is **reported** rather than dropped in silence -
    a selection of twenty must not archive nineteen without a word.
    """
    archived: list[int] = []
    refused: list[dict[str, Any]] = []
    for post_id in dict.fromkeys(payload.post_ids):
        try:
            lifecycle.transition(post_id, lifecycle.ARCHIVED, reason="archived from the board")
            archived.append(post_id)
        except lifecycle.TransitionError as exc:
            refused.append(
                {
                    "post_id": post_id,
                    "code": exc.code,
                    "message": str(exc),
                    "params": exc.params,
                }
            )
    return {"archived": len(archived), "post_ids": archived, "refused": refused}


@router.get("/posts/{post_id}")
def get_post(post_id: int) -> dict[str, Any]:
    post = post_store.detail(post_id)
    if post is None:
        raise api_error("post_not_found", "Post not found.", 404)
    _decorate(post)
    post["checklist"] = validation.summary(
        validation.validate(post, post["images"], post["tags"])
    )
    post["suggested_bindings"] = resource_cache.suggest_bindings(
        [image["image_id"] for image in post["images"] if image.get("image_id")]
    )
    post["events"] = events.for_post(post_id, limit=30)
    return post


@router.patch("/posts/{post_id}")
def patch_post(post_id: int, payload: PostPatch) -> dict[str, Any]:
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    post = post_store.get(post_id)
    if post is None:
        raise api_error("post_not_found", "Post not found.", 404)

    fields = {
        key: value
        for key, value in payload.model_dump(exclude={"clear_binding"}).items()
        if value is not None
    }
    if payload.clear_binding:
        fields["model_version_id"] = None
        fields["model_name"] = None
        fields["version_name"] = None

    if "model_version_id" in fields or payload.clear_binding:
        guarded(lifecycle.guard_binding_change, post, fields.get("model_version_id"))

    if "scheduled_at" in fields and "schedule_offset_minutes" in fields:
        raise api_error(
            "schedule_time_and_offset",
            "Choose either a publish time or an offset, not both.",
            400,
        )

    if post.get("remote_post_id") and (
        "scheduled_at" in fields or "schedule_offset_minutes" in fields
    ):
        when = fields.pop("scheduled_at", None)
        offset_minutes = fields.pop("schedule_offset_minutes", None)
        guarded(
            operations.reschedule,
            post_id,
            when=when,
            offset_minutes=offset_minutes,
        )

    post_store.update(post_id, **fields)
    _maybe_ready(post_id)
    return get_post(post_id)


@router.delete("/posts/{post_id}", response_model=Ok)
def delete_post(post_id: int, remote: bool = False) -> Ok:
    """Delete locally, optionally on CivitAI too.

    Deleting only locally leaves the remote post standing on purpose - it is the
    user's published work, and this app must not remove it as a side effect of
    tidying up a local plan.
    """
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    post = post_store.get(post_id)
    if post is None:
        raise api_error("post_not_found", "Post not found.", 404)
    if remote and post.get("remote_post_id"):
        guarded(operations.delete_remote, post_id)
    post_store.delete(post_id)
    return Ok()


@router.put("/posts/{post_id}/images")
def set_images(post_id: int, payload: ImageList) -> dict[str, Any]:
    """Set the library images in a post; ``image-order`` orders or removes tiles."""
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    resolution = _set_images_and_resolve(post_id, payload.image_ids)
    post = get_post(post_id)
    post["resource_resolution"] = resolution
    return post


@router.put("/posts/{post_id}/image-order")
def set_image_order(post_id: int, payload: ImageOrder) -> dict[str, Any]:
    """Order or remove existing post-image tiles without changing their identity."""
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    _check_image_limit(payload.post_image_ids)
    post_store.set_image_order(post_id, payload.post_image_ids)
    return get_post(post_id)


@router.put("/posts/{post_id}/tags")
def set_tags(post_id: int, payload: TagList) -> dict[str, Any]:
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    post_store.set_tags(post_id, payload.tags)
    return get_post(post_id)


@router.post("/posts/{post_id}/images/{post_image_id}/dedup-ack", response_model=Ok)
def ack_duplicate(post_id: int, post_image_id: int, value: bool = True) -> Ok:
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    post_store.set_image_flag(post_image_id, "dedup_ack", value)
    return Ok()


@router.post("/posts/{post_id}/images/{post_image_id}/hide-meta")
def hide_meta(post_id: int, post_image_id: int, value: bool = True) -> dict[str, Any]:
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    return guarded(operations.set_hide_meta, post_image_id, value)


@router.post("/posts/{post_id}/ready")
def mark_ready(post_id: int) -> dict[str, Any]:
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    guarded(lifecycle.transition, post_id, lifecycle.READY)
    return get_post(post_id)


@router.post("/posts/{post_id}/unready")
def mark_draft(post_id: int) -> dict[str, Any]:
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    guarded(lifecycle.transition, post_id, lifecycle.DRAFT)
    return get_post(post_id)


@router.post("/posts/{post_id}/archive")
def archive(post_id: int) -> dict[str, Any]:
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    guarded(lifecycle.transition, post_id, lifecycle.ARCHIVED)
    return get_post(post_id)


@router.post("/posts/{post_id}/archive-folder/rename")
def rename_archive_folder(post_id: int) -> dict[str, Any]:
    """Explicitly give an id-only archive folder its dated name."""
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    post = post_store.get(post_id)
    if post is None:
        raise api_error("post_not_found", "Post not found.", 404)
    remote_post_id = post.get("remote_post_id")
    preview = (
        image_archive.undated_folder_rename(int(remote_post_id))
        if post["state"] == lifecycle.ARCHIVED and remote_post_id
        else None
    )
    if preview is None:
        raise api_error(
            "archive_folder_rename_unavailable",
            "This archive folder cannot be renamed now. Refresh the archive and try again.",
            409,
        )
    try:
        return image_archive.rename_undated_folder(int(remote_post_id))
    except FileExistsError as exc:
        raise api_error(
            "archive_folder_target_exists",
            f"The target folder already exists: {preview['to']}. "
            "Merge the folders manually before retrying.",
            409,
            path=preview["to"],
        ) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise api_error(
            "archive_folder_rename_unavailable",
            f"{exc} Refresh the archive and try again.",
            409,
        ) from exc


@router.post("/posts/{post_id}/reschedule")
def reschedule(post_id: int, payload: RescheduleRequest) -> dict[str, Any]:
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    guarded(
        operations.reschedule,
        post_id,
        when=payload.scheduled_at,
        offset_minutes=payload.offset_minutes,
    )
    return get_post(post_id)


@router.post("/posts/{post_id}/push-text")
def push_text(post_id: int) -> dict[str, Any]:
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    guarded(operations.push_text, post_id)
    return get_post(post_id)


@router.post("/posts/{post_id}/remote-delete")
def remote_delete(post_id: int) -> dict[str, Any]:
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    guarded(operations.delete_remote, post_id)
    return get_post(post_id)


@router.post("/posts/{post_id}/rebuild")
def rebuild(post_id: int) -> dict[str, Any]:
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    guarded(operations.rebuild, post_id)
    return get_post(post_id)


@router.post("/posts/{post_id}/adopt-remote-snapshot")
def adopt_snapshot(post_id: int) -> dict[str, Any]:
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    guarded(operations.adopt_remote_snapshot, post_id)
    return get_post(post_id)


@router.get("/posts/{post_id}/reconcile")
def reconcile_candidates(post_id: int) -> dict[str, Any]:
    return guarded(reconcile.candidates, post_id)


@router.post("/posts/{post_id}/reconcile")
def reconcile_adopt(post_id: int, payload: AdoptRequest) -> dict[str, Any]:
    """Bind this post to an existing CivitAI post, after verifying the id."""
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    guarded(reconcile.adopt, post_id, payload.remote_post_id)
    return get_post(post_id)


@router.post("/posts/match-local")
def match_local_all() -> dict[str, Any]:
    """Search for local files for every post that still misses some.

    Registered before the per-post route: `{post_id}` is an int, so this path
    could never be mistaken for one, but the order makes that independent of
    FastAPI's matching rules.
    """
    return start_job(
        "match", lambda job: matching.resolve_all(job), code="match_running"
    )


@router.post("/posts/{post_id}/match-local")
def match_local(post_id: int, download: bool = True) -> dict[str, Any]:
    """Match images without a local file against the library.

    With ``download`` every unresolved image is fetched once and hashed - more
    accurate, but slower. Without it, only the history is consulted.
    """
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    guarded(matching.resolve, post_id, download=download)
    return get_post(post_id)


@router.get("/posts/{post_id}/images/{post_image_id}/match-candidates")
def match_candidates(
    post_id: int,
    post_image_id: int,
    limit: int = Query(24, ge=1, le=100),
) -> dict[str, Any]:
    """Rank non-exact evidence for the manual local-image picker."""
    try:
        return matching.suggestions(post_id, post_image_id, limit=limit)
    except matching.ManualMatchError as exc:
        raise api_error(exc.code, str(exc), exc.status) from exc


@router.post("/posts/{post_id}/images/{post_image_id}/match-local/{image_id}")
def match_local_manually(post_id: int, post_image_id: int, image_id: int) -> dict[str, Any]:
    """Link the already-indexed file explicitly chosen by the user."""
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    try:
        matching.link_manual(post_id, post_image_id, image_id)
    except matching.ManualMatchError as exc:
        raise api_error(exc.code, str(exc), exc.status) from exc
    return get_post(post_id)


@router.post("/posts/{post_id}/fetch-images")
def start_fetch_images(post_id: int) -> dict[str, Any]:
    """Fetch missing images from CivitAI.

    They land in the configured adopted folder and reach the archive only
    through the explicit archive move.
    """
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    return start_job(
        "fetch", lambda job: fetch_images.run(job, post_id), code="fetch_running"
    )


@router.post("/posts/{post_id}/sync")
def sync_post(post_id: int) -> dict[str, Any]:
    guard_post_mutation(post_id, {lifecycle.PUSHING})
    guarded(sync.sync_one, post_id)
    watchers.reset_post(post_id)
    return get_post(post_id)


# --- helpers ----------------------------------------------------------------


def _decorate(
    post: dict[str, Any], *, summary_images: list[dict[str, Any]] | None = None
) -> None:
    """Add the derived values the UI needs but the table does not store."""
    info = lifecycle.INFO.get(post["state"])
    post["can_schedule"] = info.can_schedule if info else False
    post["can_edit_remote"] = info.can_edit_remote if info else False
    post["has_remote"] = bool(post.get("remote_post_id"))

    when = schedule.effective_publish_at(post)
    post["resolved_publish_at"] = schedule.iso_z(when) if when else None
    post["publish_at_is_pinned"] = schedule.is_pinned(post)
    post["offset_label"] = (
        "" if post["publish_at_is_pinned"]
        else schedule.describe_offset(post.get("schedule_offset_minutes"))
    )
    for image in post.get("images", []):
        image["is_missing"] = bool(image.get("is_missing"))
    if "images" not in post:
        if summary_images is None:
            summary_images = post_store.images(post["id"])
        post["image_count"] = len(summary_images)
        post["thumbnails"] = [
            {
                "image_id": image["image_id"],
                "thumbnail_path": image.get("thumbnail_path"),
            }
            for image in summary_images[:4]
            if image.get("image_id")
        ]
        post["tags"] = [row["name"] for row in post_store.tags(post["id"])]


def _decorate_archive(post: dict[str, Any], archive_base: Path | None = None) -> None:
    images = post_store.images(post["id"])
    _decorate(post, summary_images=images)
    post["previews"] = [
        ArchivePreview(
            position=int(image["position"]),
            image_id=int(image["image_id"]) if image.get("image_id") is not None else None,
            thumbnail_path=(
                str(image["thumbnail_path"]) if image.get("thumbnail_path") else None
            ),
            remote_url=str(image["remote_url"]) if image.get("remote_url") else None,
        ).model_dump()
        for image in images
        if not fetch_images.is_video(image)
    ][:5]
    remote_post_id = post.get("remote_post_id")
    archive_folder = (
        image_archive.folder_for(
            archive_base, int(remote_post_id), post.get("archive_date")
        )
        if archive_base and remote_post_id
        else None
    )
    post["archive_folder"] = (
        str(archive_folder) if archive_folder and archive_folder.is_dir() else None
    )
    post["archive_folder_rename"] = (
        image_archive.undated_folder_rename(int(remote_post_id)) if remote_post_id else None
    )


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _validated_archive_range(
    from_date: str | None, to_date: str | None
) -> tuple[str | None, str | None]:
    values = (from_date, to_date)
    parsed: list[date | None] = []
    for value in values:
        if value is None:
            parsed.append(None)
            continue
        try:
            valid = (
                bool(_ISO_DATE.fullmatch(value))
                and date.fromisoformat(value).isoformat() == value
            )
        except ValueError:
            valid = False
        if not valid:
            raise api_error(
                "invalid_archive_date_range",
                "Use valid archive dates in YYYY-MM-DD format, with From no later than To.",
            )
        parsed.append(date.fromisoformat(value))
    if parsed[0] is not None and parsed[1] is not None and parsed[0] > parsed[1]:
        raise api_error(
            "invalid_archive_date_range",
            "Use valid archive dates in YYYY-MM-DD format, with From no later than To.",
        )
    return from_date, to_date


def _maybe_ready(post_id: int) -> None:
    """A draft that has gained a publish time is ready; keep that automatic."""
    post = post_store.get(post_id)
    if post is None or post["state"] != lifecycle.DRAFT:
        return
    has_time = post.get("schedule_offset_minutes") is not None or post.get("scheduled_at")
    if has_time or post.get("publish_mode") in ("now", "draft_only"):
        with contextlib.suppress(lifecycle.TransitionError):
            lifecycle.transition(post_id, lifecycle.READY)


def _state_meta() -> list[dict[str, Any]]:
    return [
        {
            "state": info.state,
            "label": info.label,
            "can_schedule": info.can_schedule,
            "has_remote": info.has_remote,
        }
        for info in lifecycle.INFO.values()
    ]
