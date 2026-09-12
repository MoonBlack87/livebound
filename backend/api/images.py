"""The image library: listing, thumbnails, metadata, duplicate checks."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from .. import config, duplicates, jobs, security, thumbnails
from ..metadata import editing, resource_edits, upload_document
from ..metadata import resources as metadata_resources
from ..models import (
    BulkEditRequest,
    DeleteImages,
    DuplicateDismissal,
    ImageEditPut,
    ImageSelection,
    RestoreImages,
    TrashImages,
    UsageState,
)
from ..posts import lifecycle
from ..scanner import service as scanner
from ..store import edits as edit_store
from ..store import images as image_store
from ..store import posts as post_store
from ..store import sources as source_store
from ..store import usage
from .common import api_error, edit_target_image_ids, guard_image_mutation, start_job

router = APIRouter(prefix="/api", tags=["images"])


@router.get("/images")
def list_images(
    source_id: int | None = None,
    folder: str | None = None,
    q: str | None = None,
    usage_state: UsageState = "unused",
    include_missing: bool = False,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    candidates = image_store.search_candidates(
        root_id=source_id,
        folder=folder,
        query=q,
        include_missing=include_missing,
    )
    #: One classification for the complete candidate set drives both filtering
    #: and page badges, so the two answers cannot drift apart again.
    #: Hashes only: `check_many` classifies on those, and building a generation
    #: identity for every row meant parsing every infotext for nothing.
    marks = usage.check_many(
        [
            {
                "sha256": row.get("sha256"),
                "pixel_sha256": row.get("pixel_sha256"),
                "phash": row.get("phash"),
            }
            for row in candidates
        ]
    )
    planned_ids = image_store.planned_image_ids([row["id"] for row in candidates])

    def wanted(row: dict[str, Any]) -> bool:
        used = bool(marks.get(row.get("sha256") or "", {}).get("has_certain"))
        planned = row["id"] in planned_ids
        if usage_state == "all":
            return True
        if usage_state == "used":
            return used
        if usage_state == "planned":
            return planned and not used
        # "unused" means available. An image already placed in a draft is not,
        # and leaving it here is what let the same picture be planned twice.
        # A missing file stays visible either way: `IMG-16`'s "source not found"
        # placeholder is the only way the maintainer learns about it, and hiding
        # it behind a filter would be the one thing worse than showing it twice.
        return not used and (not planned or bool(row.get("is_missing")))

    candidate_ids = [row["id"] for row in candidates if wanted(row)]
    group_candidate_ids = (
        candidate_ids
        if usage_state != "all" or (q is not None and q.strip())
        else None
    )
    duplicate_groups = duplicates.library_groups(group_candidate_ids)
    rows, total = image_store.search(
        root_id=source_id,
        folder=folder,
        query=q,
        include_missing=include_missing,
        candidate_ids=set(candidate_ids),
        duplicate_groups=duplicate_groups,
        limit=limit,
        offset=offset,
    )
    visible_groups = image_store.duplicate_groups_for(
        rows,
        duplicate_groups,
        root_id=source_id,
        folder=folder,
        query=q,
        include_missing=include_missing,
    )
    items = []
    edited = edit_store.edited_ids([row["id"] for row in rows])
    for row in rows:
        mark = marks.get(row.get("sha256") or "", {})
        duplicate_group = visible_groups.get(row["id"])
        duplicate_members = duplicate_group["members"] if duplicate_group else []
        items.append(
            {
                **{k: v for k, v in row.items() if k != "parsed"},
                "duplicate_count": len(duplicate_members) or 1,
                "duplicate_confidence": (
                    duplicate_group["confidence"] if duplicate_group else None
                ),
                "duplicate_members": duplicate_members,
                "used": mark.get("has_certain", False),
                "planned": row["id"] in planned_ids,
                "similar": mark.get("has_similar", False),
                "edited": row["id"] in edited,
                "quick": _quick_facts(row),
            }
        )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "usage_state": usage_state,
    }


@router.get("/images/{image_id}")
def get_image(image_id: int) -> dict[str, Any]:
    image = image_store.get(image_id)
    if image is None:
        raise api_error("image_not_found", "Image not found.", 404)
    return {
        **image,
        "edited": image_id in edit_store.edited_ids([image_id]),
        # The folder the user knows this file by. Cheaper here than a second
        # request from the drawer, and the label is what they recognise.
        "source_label": _source_label(image.get("source_root_id")),
        "metadata": scanner.image_view(image),
        "duplicates": usage.check(
            image.get("sha256"),
            image.get("phash"),
            pixel_sha256=image.get("pixel_sha256"),
        ),
    }


@router.get("/images/{image_id}/edit")
def get_image_edit(image_id: int) -> dict[str, Any]:
    image = image_store.get(image_id)
    if image is None:
        raise api_error("image_not_found", "Image not found.", 404)
    result = editing.view(image)
    return {
        **result,
        "resources": image_store.resources_for(image_id),
        "original_resources": metadata_resources.extract(result["original"]),
    }


@router.put("/images/{image_id}/edit")
def put_image_edit(image_id: int, payload: ImageEditPut) -> dict[str, Any]:
    guard_image_mutation([image_id], {lifecycle.PUSHING})
    image = image_store.get(image_id)
    if image is None:
        raise api_error("image_not_found", "Image not found.", 404)
    draft_fields = payload.draft.get("fields")
    requested = set(payload.touched) | set(payload.deleted)
    if isinstance(draft_fields, dict):
        requested.update(draft_fields)
    derived = sorted(
        requested.intersection({*resource_edits.RESOURCE_FIELDS, upload_document.LIVEBOUND_KEY})
    )
    if derived:
        raise api_error(
            "derived_metadata_field",
            f"{derived[0]} is derived from the image resources and cannot be edited.",
            field=derived[0],
        )
    editing.save(image, payload.model_dump())
    result = editing.view(image_store.get(image_id) or image)
    return {
        **result,
        "resources": image_store.resources_for(image_id),
        "original_resources": metadata_resources.extract(result["original"]),
    }


@router.post("/images/edit/bulk")
def bulk_edit(payload: BulkEditRequest) -> dict[str, Any]:
    target_ids = edit_target_image_ids(payload.image_ids, payload.post_id)
    guard_image_mutation(target_ids, {lifecycle.PUSHING})
    images = image_store.get_many(target_ids)
    if not images:
        raise api_error("no_images_selected", "No images selected.", 400)
    try:
        plan = editing.bulk_plan(images, payload.operations.model_dump())
    except re.error as exc:
        # The one user-input error the pure planner can raise. Anything else is
        # ours and has no business being a 400.
        raise api_error("invalid_regex", f"Invalid regular expression: {exc}.", 400) from exc
    changed = editing.apply_plan(plan) if payload.apply else 0
    # A delete-field that no affected image carries changes nothing, and the
    # only feedback was an empty preview the user had to read as a misspelling.
    present = {name for item in plan for name in item.get("fields_present") or []}
    unmatched = [
        name for name in (payload.operations.delete_fields or []) if name not in present
    ]
    public_plan = [
        {key: value for key, value in item.items() if key not in ("edit", "fields_present")}
        for item in plan
    ]
    return {
        "applied": payload.apply,
        "changed": changed,
        "plan": public_plan,
        "unmatched_delete_fields": unmatched,
    }


@router.get("/images/{image_id}/thumbnail")
def get_thumbnail(image_id: int, size: str = "small", v: str | None = None) -> FileResponse:
    """The cached thumbnail. ``size=large`` is produced on first request.

    Generating the large variant during the scan would double its cost for a
    size most images are never shown at, so it is made here instead.
    """
    image = _locally_available_image(image_id)

    if size == "large":
        # Bounded like every other read of a stored path. Without it this branch
        # rendered any file Pillow could decode, anywhere on the machine - the
        # cache path below passes serveable, so the check succeeded on the wrong
        # file and the picture was served regardless.
        name = thumbnails.ensure(
            security.serveable(Path(image["absolute_path"])),
            image.get("sha256") or "",
            edge=config.THUMBNAIL_LARGE_EDGE,
        )
    else:
        name = image.get("thumbnail_path")

    if name:
        cached = thumbnails.path_for(name)
        if cached.exists():
            current_version = (
                f'{thumbnails.cache_key(image["sha256"], config.THUMBNAIL_MAX_EDGE)}.webp'
                if image.get("sha256")
                else None
            )
            immutable = (
                bool(v)
                and v == current_version
                and (size == "large" or name == current_version)
            )
            headers = (
                {"Cache-Control": "public, max-age=31536000, immutable"}
                if immutable
                else None
            )
            return FileResponse(
                security.serveable(cached), media_type="image/webp", headers=headers
            )
    # A missing thumbnail must not leave a hole in the grid.
    return get_preview(image_id)


@router.get("/images/{image_id}/preview")
def get_preview(image_id: int) -> FileResponse:
    image = _locally_available_image(image_id)

    # Bounded to the configured folders: the path comes from the scanner, but a
    # stale row must not turn this into a general file reader.
    path = security.serveable(Path(image["absolute_path"]))
    return FileResponse(path, media_type=image.get("content_type") or "image/png")


def _locally_available_image(image_id: int) -> dict[str, Any]:
    image = image_store.get(image_id)
    if image is None:
        raise api_error("image_not_found", "Image not found.", 404)
    if image.get("is_missing"):
        raise api_error(
            "source_file_not_found",
            "The source file is no longer available locally.",
            404,
        )
    return image


@router.get("/duplicates")
def get_duplicates() -> dict[str, Any]:
    """The newest finished search, filtered to the current library.

    The result rides on the job rather than a table: it is a snapshot of a
    moment, and the job registry already keeps the last runs. Rows deleted since
    that moment are removed before the snapshot reaches the page.
    """
    running = jobs.active("duplicates")
    latest = max(
        (
            job
            for job in jobs.all_jobs()
            if job.kind == "duplicates" and job.status == "done"
        ),
        key=lambda job: job.id,
        default=None,
    )
    groups = duplicates.current_groups((latest.result or {}).get("groups", [])) if latest else []
    return {
        "running": running.to_dict() if running else None,
        "groups": groups,
        "checked_at": latest.finished_at if latest else None,
    }


@router.post("/duplicates/scan")
def scan_duplicates() -> dict[str, Any]:
    return start_job("duplicates", duplicates.run, code="duplicates_running")


@router.post("/duplicates/dismiss")
def dismiss_duplicates(payload: DuplicateDismissal) -> dict[str, Any]:
    try:
        dismissed = duplicates.dismiss(payload.image_ids)
    except LookupError:
        raise api_error(
            "image_not_found",
            "One or more selected images no longer exist. Refresh the duplicates list.",
            404,
        ) from None
    return {"dismissed": dismissed}


@router.post("/images/delete")
def delete_images(payload: DeleteImages) -> dict[str, Any]:
    """Delete files for good.

    The only place in this application that removes a source file. Bounded to the
    configured folders, and never reached without a typed confirmation in the UI.
    """
    # A file an active push is still uploading must not disappear from under it,
    # and this is the one door in the application that cannot be walked back.
    guard_image_mutation(payload.image_ids, {lifecycle.PUSHING})
    _refuse_archive_duplicates(payload.image_ids)
    return duplicates.delete_files(payload.image_ids, with_sidecars=payload.with_sidecars)


@router.post("/images/delete-plan")
def plan_image_deletion(payload: DeleteImages) -> dict[str, Any]:
    """List every image and sidecar that permanent deletion would touch."""
    # The preview of a refused deletion is refused too: learning that the plan
    # was fine and then being turned away at the delete is the worse order.
    guard_image_mutation(payload.image_ids, {lifecycle.PUSHING})
    _refuse_archive_duplicates(payload.image_ids)
    return {
        "paths": duplicates.deletion_plan(
            payload.image_ids, with_sidecars=payload.with_sidecars
        )
    }


@router.post("/images/trash-plan")
def plan_image_trash(payload: TrashImages) -> dict[str, Any]:
    """Report selected images a post holds before a trash move starts."""
    post_bound = duplicates.post_bound_image_ids(payload.image_ids)
    return {
        "post_bound_image_ids": sorted(post_bound),
        "items": [
            {
                "id": image["id"],
                "absolute_path": image["absolute_path"],
                "post_bound": image["id"] in post_bound,
            }
            for image in image_store.get_many(payload.image_ids)
        ],
    }


@router.post("/images/post-holders")
def image_post_holders(payload: ImageSelection) -> dict[str, Any]:
    """Which posts already hold each image of a selection.

    A new post from a selection that still contains planned images is allowed.
    The caller asks first and names the posts that already hold selected images,
    so the choice is made with that information available.
    """
    holders = post_store.posts_holding_images(payload.image_ids)
    return {
        "items": [
            {"image_id": image_id, "posts": posts}
            for image_id, posts in sorted(holders.items())
        ]
    }


@router.get("/trash")
def get_trash() -> dict[str, Any]:
    return {"items": image_store.trashed()}


@router.post("/images/trash")
def trash_images(payload: TrashImages) -> dict[str, Any]:
    _refuse_archive_duplicates(payload.image_ids)
    try:
        duplicates.require_trash_folder()
    except ValueError as exc:
        raise api_error("trash_unavailable", str(exc), 400) from exc
    return start_job(
        "trash",
        lambda job: duplicates.move_to_trash(
            payload.image_ids,
            with_sidecars=payload.with_sidecars,
            job=job,
        ),
        code="trash_running",
    )


@router.post("/images/restore")
def restore_images(payload: RestoreImages) -> dict[str, Any]:
    return duplicates.restore_from_trash(payload.image_ids)


def _refuse_archive_duplicates(image_ids: list[int]) -> None:
    if duplicates.archive_duplicate_image_ids(image_ids):
        raise api_error(
            "archive_duplicate_exempt",
            "Archive duplicates are protected. Remove them manually outside the application "
            "if that is really intended.",
            409,
        )


def _source_label(root_id: int | None) -> str:
    if root_id is None:
        return ""
    root = next((r for r in source_store.list_roots() if r["id"] == root_id), None)
    if root is None:
        return ""
    return root["label"] or Path(root["path"]).name


def _quick_facts(row: dict[str, Any]) -> dict[str, Any]:
    """The handful of values worth showing on a grid tile."""
    parsed = row.get("parsed") or {}
    fields = parsed.get("fields") or {}
    return {
        "model": fields.get("Model"),
        "sampler": fields.get("Sampler"),
        "steps": fields.get("Steps"),
        "cfg": fields.get("CFG scale"),
        "seed": fields.get("Seed"),
        "has_metadata": bool(parsed.get("prompt") or fields),
    }
