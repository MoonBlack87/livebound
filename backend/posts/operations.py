"""Actions on a post that already exists on CivitAI.

Everything here is post-publication rework: change the schedule, fix the title,
adjust tags, take it down, or rebuild it because the one thing that cannot be
changed - the model binding - is wrong.
"""

from __future__ import annotations

import contextlib
from typing import Any

from .. import db
from ..civitai import mcp, scopes, trpc
from ..civitai.errors import CivitaiError, NotFound
from ..store import events, usage
from ..store import posts as post_store
from . import lifecycle, schedule, sync


class TextUpdateNotConfirmed(CivitaiError):
    """CivitAI answered the update but kept different post text."""

    code = "post_text_update_not_confirmed"

    def __init__(self) -> None:
        super().__init__(
            "CivitAI cannot clear a title or description. Leave at least one "
            "character, or use Rebuild to delete and recreate the post if it must "
            "be empty.",
            status=409,
        )


class TextUpdateConfirmationFailed(CivitaiError):
    """The text write was sent, but the read-back supplied no usable answer."""

    code = "post_text_update_confirmation_failed"

    def __init__(self) -> None:
        super().__init__(
            "The text update was sent but could not be confirmed. Sync the post "
            "before deciding whether to try again.",
            status=502,
        )


class ImageReorderFailed(CivitaiError):
    """The remote order was refused or could not be confirmed as accepted."""

    code = "post_image_reorder_failed"

    def __init__(self, *, status: int | None = None) -> None:
        super().__init__(
            "CivitAI did not accept the new image order. Retry the reorder or "
            "sync the post before trying again.",
            status=status or 502,
        )


def reorder_images(post_id: int, image_ids: list[int]) -> None:
    """Reorder a remote post before committing the matching local sequence.

    A changed membership remains on the existing local path.
    ``post.reorderImages`` only assigns indexes; it cannot make an incomplete
    remote image list match a changed membership.
    """
    post = post_store.get(post_id)
    if post is None:
        raise ValueError("Post not found.")
    remote_id = post.get("remote_post_id")
    if not isinstance(remote_id, int):
        post_store.set_images(post_id, image_ids)
        return

    current = post_store.images(post_id)
    current_ids = [
        int(image["image_id"])
        for image in current
        if image.get("image_id") is not None
    ]
    remote_by_local = {
        int(image["image_id"]): image.get("remote_image_id")
        for image in current
        if image.get("image_id") is not None
    }
    pure_reorder = (
        len(image_ids) == len(set(image_ids))
        and len(image_ids) == len(current_ids)
        and set(image_ids) == set(current_ids)
    )
    remote_image_ids = [remote_by_local.get(image_id) for image_id in image_ids]
    complete_remote_order = pure_reorder and all(
        isinstance(image_id, int) for image_id in remote_image_ids
    )

    if complete_remote_order:
        confirmed_remote_ids = [
            int(image_id)
            for image_id in remote_image_ids
            if isinstance(image_id, int)
        ]
        try:
            trpc.post_reorder_images(remote_id, confirmed_remote_ids)
        except CivitaiError as exc:
            raise ImageReorderFailed(status=exc.status) from exc

    post_store.set_images(post_id, image_ids)


def reschedule(post_id: int, *, when: str | None = None, offset_minutes: int | None = None) -> dict:
    """Move a scheduled post to a new time.

    Refused once the post is live: CivitAI freezes ``publishedAt`` the moment it
    passes, and answers an attempt with "You cannot reschedule a post that is
    already published".
    """
    post = post_store.get(post_id)
    if post is None:
        raise ValueError("Post not found.")
    lifecycle.guard_reschedule(post)

    # Resolve and check before writing anything: a refused time must leave the
    # row holding the one the maintainer can correct, not the one thrown out
    # (`LIF-19`).
    if offset_minutes is not None:
        target = schedule.resolve(mode=schedule.RELATIVE, offset_minutes=offset_minutes,
                                  scheduled_at=None)
    else:
        target = schedule.parse_iso(when)

    if target is None:
        raise ValueError("Not a valid publish time.")

    check = schedule.validate(target)
    if not check.ok:
        raise ValueError(check.message)

    if offset_minutes is not None:
        post_store.update(post_id, schedule_mode=schedule.RELATIVE,
                          schedule_offset_minutes=offset_minutes)
    else:
        post_store.update(post_id, schedule_mode=schedule.ABSOLUTE,
                          scheduled_at=schedule.iso_z(target))

    remote_id = post.get("remote_post_id")
    if not remote_id:
        # Purely local so far - nothing to send, the push will use the new time.
        return {"post_id": post_id, "published_at": schedule.iso_z(target), "sent": False}

    stamp = schedule.iso_z(target)
    trpc.post_update(remote_id, published_at=stamp)
    post_store.set_fields(post_id, scheduled_at=stamp)
    events.record("scheduled", post_id=post_id, remote_post_id=remote_id,
                  detail={"published_at": stamp})
    sync.sync_one(post_id)
    return {"post_id": post_id, "published_at": stamp, "sent": True}


def push_text(post_id: int) -> dict[str, Any]:
    """Send local title/detail/tag edits to an existing remote post."""
    post = post_store.get(post_id)
    if post is None:
        raise ValueError("Post not found.")
    remote_id = post.get("remote_post_id")
    if not remote_id:
        raise ValueError("This post is not on CivitAI yet.")

    title = post.get("title") or ""
    detail = post.get("detail") or ""
    trpc.post_update(remote_id, title=title, detail=detail)
    confirmed = _read_text_confirmation(remote_id)
    _require_text_confirmation(post_id, confirmed, title=title, detail=detail)

    _sync_tags(post_id, remote_id, confirmed)
    confirmed = _read_text_confirmation(remote_id)
    _require_text_confirmation(post_id, confirmed, title=title, detail=detail)

    sync.snapshot_pushed(post_id, confirmed)
    events.record("updated", post_id=post_id, remote_post_id=remote_id)
    sync.sync_one(post_id)
    return {"post_id": post_id, "updated": True}


def _text_confirmed(remote: Any, *, title: str, detail: str) -> bool:
    return (
        isinstance(remote, dict)
        and (remote.get("title") or "") == title
        and (remote.get("detail") or "") == detail
    )


def _read_text_confirmation(remote_id: int) -> dict[str, Any]:
    try:
        remote = trpc.post_get(remote_id)
    except CivitaiError as exc:
        raise TextUpdateConfirmationFailed() from exc
    if not isinstance(remote, dict):
        raise TextUpdateConfirmationFailed()
    return remote


def _require_text_confirmation(
    post_id: int, remote: dict[str, Any], *, title: str, detail: str
) -> None:
    expected = {"title": title, "detail": detail}
    confirmed = {
        key: remote.get(key)
        for key, value in expected.items()
        if key in remote and (remote.get(key) or "") == value
    }
    if confirmed:
        # Only fields this application wrote and CivitAI confirmed may move the
        # baseline. A pre-existing remote tag or schedule divergence remains a
        # choice for the maintainer (`LIF-11`).
        sync.snapshot_pushed(post_id, confirmed, clean=False)
    if any(key not in remote for key in expected):
        raise TextUpdateConfirmationFailed()
    if _text_confirmed(remote, title=title, detail=detail):
        return
    raise TextUpdateNotConfirmed()


def _sync_tags(post_id: int, remote_id: int, remote: dict[str, Any]) -> None:
    """Make the remote tag set match the local one."""
    local = post_store.tags(post_id)
    local_names = {row["name"] for row in local}

    # tRPC, not MCP: the MCP get_post tool does not return tags. The caller has
    # already read this snapshot to confirm the text update.
    remote_tags = {
        str(tag.get("name", "")).lower(): tag.get("id") or tag.get("tagId")
        for tag in (remote.get("tags") or [])
        if isinstance(tag, dict)
    }

    for name, tag_id in remote_tags.items():
        if name not in local_names and isinstance(tag_id, int):
            try:
                trpc.post_remove_tag(remote_id, tag_id)
                events.record("tag_removed", post_id=post_id, detail={"tag": name})
            except CivitaiError:
                pass

    for row in local:
        if row["name"] in remote_tags:
            post_store.set_tag_remote_id(post_id, row["name"], remote_tags[row["name"]])
            continue
        try:
            result = trpc.post_add_tag(remote_id, row["name"])
            tag_id = result.get("id") if isinstance(result, dict) else None
            post_store.set_tag_remote_id(post_id, row["name"], tag_id)
            events.record("tag_added", post_id=post_id, detail={"tag": row["name"]})
        except CivitaiError:
            pass


def set_hide_meta(post_image_id: int, hide: bool) -> dict[str, Any]:
    """Show or hide the generation data under one image."""
    row = db.get_connection().execute(
        "SELECT * FROM post_images WHERE id=?", (post_image_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Image not found.")

    post_store.set_image_flag(post_image_id, "hide_meta", hide)
    if row["remote_image_id"]:
        trpc.post_update_image(row["remote_image_id"], hide_meta=hide)
    return {"post_image_id": post_image_id, "hide_meta": hide}


def delete_remote(post_id: int) -> dict[str, Any]:
    """Remove the post from CivitAI, keeping the local plan.

    This is also the only way back from published or scheduled: CivitAI has no
    unpublish. The dedup history is kept but marked withdrawn, so the images can
    be reused without a warning while the record of what happened survives.
    """
    post = post_store.get(post_id)
    if post is None:
        raise ValueError("Post not found.")
    remote_id = post.get("remote_post_id")
    if not remote_id:
        raise ValueError("This post is not on CivitAI.")

    # Checked before the call, not after: a 403 here would leave the local state
    # claiming the post is gone while it is still on CivitAI.
    scopes.require("MediaDelete")

    # Already gone is the outcome we wanted; the local cleanup below still applies.
    with contextlib.suppress(NotFound):
        mcp.delete_post(remote_id)

    post_store.set_fields(
        post_id,
        remote_post_id=None,
        remote_published_at=None,
        remote_state=None,
        remote_url=None,
        remote_snapshot_json=None,
        pushed_fields_json=None,
        bound_model_version_id=None,
        remote_diverged=0,
        last_error=None,
    )
    post_store.clear_uploads(post_id)
    usage.set_status_for_post(post_id, usage.WITHDRAWN)
    events.record("deleted", post_id=post_id, remote_post_id=remote_id)
    with contextlib.suppress(lifecycle.TransitionError):
        lifecycle.transition(post_id, lifecycle.DRAFT)
    return {"post_id": post_id, "deleted_remote": remote_id}


def rebuild(post_id: int) -> dict[str, Any]:
    """Delete on CivitAI and return the post to a local draft.

    The escape hatch for the model binding, which ``post.update`` cannot change:
    an "update" of ``modelVersionId`` is accepted and ignored, so the only honest
    fix is to start over with the same images and text.
    """
    result = delete_remote(post_id)
    post_store.set_fields(post_id, last_error=None)
    return {**result, "rebuilt": True}


def adopt_remote_snapshot(post_id: int) -> dict[str, Any]:
    """Take CivitAI's version as the truth after an edit made on the site."""
    post = post_store.get(post_id)
    if post is None:
        raise ValueError("Post not found.")
    remote = post.get("remote_snapshot") or {}
    fields = {
        key: remote.get(key) or "" for key in ("title", "detail") if key in remote
    }
    post_store.update(post_id, **fields)
    sync.snapshot_pushed(post_id, remote)

    post_store.set_fields(
        post_id,
        remote_diverged=0,
    )
    return {"post_id": post_id, "adopted": True}
