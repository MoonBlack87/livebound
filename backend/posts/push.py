"""The push pipeline: local post -> CivitAI, resumable at every step.

Two rules shape all of it.

**The step cursor advances only after the write that makes the step durable.**
So resuming always re-enters a step that is safe to repeat, and nothing is done
twice by accident.

**Never hold a transaction across a network call.** Each step is
``commit -> HTTP -> commit``. A 13 MB base64 upload inside a write transaction
would block every reader, including the UI polling for progress.

The order is deliberate: a scheduled post is created as a draft, then every
image is uploaded and attached, and only then is its publish time set. That last
write is what puts the attached images into the feeds, and an interrupted upload
can never leave a half-filled post scheduled to go public.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .. import config, db, diagnostics, jobs
from ..civitai import account, mcp, ratelimit, rest, trpc
from ..civitai.errors import (
    AccountNotReady,
    CivitaiError,
    LeadTimeExpired,
    NotFound,
)
from ..metadata.preview import blurhash_for
from ..store import events, uploads, usage
from ..store import posts as post_store
from ..store import runs as run_store
from . import lifecycle, materialise, reconcile, schedule, sync, validation

_PREVIEW_UPLOAD_ROUTE_MARGIN_BYTES = 64 * 1024


class PushAborted(RuntimeError):
    """Stop the whole run, not just this post (bad account, rate limit)."""


def push_posts(job: jobs.Job, post_ids: list[int], *, dry_run: bool = False) -> dict[str, Any]:
    """Push several posts, one after another."""
    if dry_run:
        job.stage = "Preview"
        job.result = {"preview": [preview(post_id) for post_id in post_ids]}
        return job.result

    try:
        acc = account.ensure_can_write(account.refresh())
    except (AccountNotReady, CivitaiError) as exc:
        job.status = "error"
        job.error = str(exc)
        return {"error": str(exc)}

    run_id = run_store.create_run("push", post_ids, {"account": acc.get("username")})
    job.result = {"run_id": run_id}
    job.total = len(post_ids)

    aborted: str | None = None
    for post_id in post_ids:
        if job.should_stop():
            run_store.set_item(run_id, post_id, status="skipped", error="cancelled")
            job.skipped += 1
            continue
        try:
            outcome = push_one(run_id, post_id, job=job)
            job.succeeded += 1
            job.log(post_id=post_id, status="ok", **outcome)
        except PushAborted as exc:
            aborted = str(exc)
            run_store.set_item(run_id, post_id, status="skipped", error=str(exc))
            job.skipped += 1
            job.log(post_id=post_id, status="skipped", message=str(exc))
            break
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            job.failed += 1
            message = _fail(run_id, post_id, message)
            job.log(post_id=post_id, status="error", message=message)
        finally:
            job.processed += 1

    status = "interrupted" if aborted else ("cancelled" if job.should_stop() else "done")
    run_store.finish_run(run_id, status, aborted)
    job.result = {**job.result, **(run_store.get_run(run_id) or {})}
    if aborted:
        job.error = aborted
    return job.result


def push_one(run_id: int, post_id: int, *, job: jobs.Job | None = None) -> dict[str, Any]:
    """Run every remaining step for one post."""
    item = run_store.get_item(run_id, post_id) or {}
    step = item.get("step") or run_store.QUEUED
    run_store.set_item(run_id, post_id, status="running", attempts=(item.get("attempts") or 0) + 1)

    post = post_store.get(post_id)
    if post is None:
        raise RuntimeError("The post no longer exists locally.")
    if post["state"] not in (lifecycle.PUSHING,):
        lifecycle.transition(post_id, lifecycle.PUSHING)

    # A retry is a *new* run with a fresh cursor, so this run's own row says
    # nothing about what an earlier one already attempted. Without looking
    # across runs, an attempt whose create_post answer was lost is invisible
    # here and the post is created a second time on a public account - which is
    # the exact outcome create_attempted_at exists to prevent.
    if step != run_store.CREATE_ATTEMPTED and not post.get("remote_post_id"):
        earlier = run_store.unresolved_attempt(post_id, before_run_id=run_id)
        if earlier:
            step = run_store.CREATE_ATTEMPTED
            run_store.set_item(
                run_id,
                post_id,
                step=step,
                create_attempted_at=earlier["create_attempted_at"],
                create_request_json=earlier.get("create_request_json"),
                reconcile_tag=earlier.get("reconcile_tag"),
            )

    # A post that may already exist remotely must be found, never re-created.
    if step == run_store.CREATE_ATTEMPTED and not post.get("remote_post_id"):
        _stage(job, "Looking for a post already created")
        result = reconcile.auto_adopt(post_id, run_id)
        if not result["adopted"]:
            lifecycle.transition(post_id, lifecycle.NEEDS_RECONCILE)
            post_store.set_fields(post_id, last_error=result["reason"])
            run_store.set_item(run_id, post_id, status="error", error=result["reason"])
            raise RuntimeError(f"Reconcile needed: {result['reason']}")
        post = post_store.get(post_id) or post
        if post["state"] != lifecycle.PUSHING:
            lifecycle.transition(post_id, lifecycle.PUSHING)
            post = post_store.get(post_id) or post
        step = run_store.CREATED
        run_store.set_item(run_id, post_id, step=step, remote_post_id=post["remote_post_id"])

    images = post_store.images(post_id)
    tags = [row["name"] for row in post_store.tags(post_id)]

    # --- 1. preflight (always re-run: it is cheap and time-sensitive) --------
    _stage(job, "Preflight")
    _preflight(post, images, tags)
    run_store.set_item(run_id, post_id, step=run_store.PREFLIGHT_OK, error=None)

    # --- 2/3. create the empty draft -----------------------------------------
    if not post.get("remote_post_id"):
        _stage(job, "Creating the post")
        _create(run_id, post_id, post, tags)
        post = post_store.get(post_id) or post
        step = run_store.CREATED

    # --- 4. tags -------------------------------------------------------------
    if _step_before(step, run_store.TAGGED):
        _stage(job, "Setting tags")
        with _action_step(
            "push.tag_sync",
            run_id,
            post_id,
            remote_post_id=post["remote_post_id"],
            tag_count=len(tags),
        ):
            _sync_tags(post_id, post["remote_post_id"])
        run_store.set_item(run_id, post_id, step=run_store.TAGGED)
        step = run_store.TAGGED

    # --- 5/6. upload, then attach every image -------------------------------
    if _step_before(step, run_store.ATTACHED):
        _stage(job, "Uploading images")
        _upload_images(
            run_id,
            post_id,
            images,
            job=job,
            record_step=_step_before(step, run_store.ATTACHING),
        )
        images = post_store.images(post_id)
        if _step_before(step, run_store.ATTACHING):
            run_store.set_item(run_id, post_id, step=run_store.UPLOADED)
            step = run_store.UPLOADED

        _stage(job, "Attaching images")
        with _action_step(
            "push.attach_images",
            run_id,
            post_id,
            remote_post_id=post["remote_post_id"],
            image_count=len(images),
        ) as outcome:
            _attach_images(run_id, post_id, post, images, job=job)
            images = post_store.images(post_id)
            _apply_image_settings(images)
            outcome["attached_image_count"] = sum(
                bool(image.get("remote_image_id")) for image in images
            )
        images = post_store.images(post_id)
        run_store.set_item(run_id, post_id, step=run_store.ATTACHED)
        step = run_store.ATTACHED

    # --- 7. set the publish time last, or apply an explicit immediate action -
    if _step_before(step, run_store.PUBLISH_APPLIED):
        _stage(job, "Applying the publish action")
        with _action_step(
            "push.publish_time",
            run_id,
            post_id,
            remote_post_id=post["remote_post_id"],
            publish_mode=post.get("publish_mode") or "schedule",
        ) as outcome:
            published = _apply_publish(run_id, post_id, post)
            outcome["publish_time_applied"] = published is not None
        run_store.set_item(run_id, post_id, step=run_store.PUBLISH_APPLIED)
        step = run_store.PUBLISH_APPLIED

    # --- 8. verify -----------------------------------------------------------
    _stage(job, "Confirming")
    confirmation = sync.sync_one(post_id)
    if (
        (post.get("publish_mode") or "schedule") == "schedule"
        and published
        and not _same_publish_time(confirmation.get("published_at"), published)
    ):
        raise RuntimeError(
            "CivitAI did not keep the requested publish time. Refresh the post "
            "before trying again."
        )
    run_store.set_item(run_id, post_id, step=run_store.VERIFIED)
    # Record what CivitAI now holds, so a later edit made on the site shows up as
    # a divergence instead of being silently overwritten by the next push.
    sync.snapshot_pushed(post_id)
    run_store.set_item(run_id, post_id, step=run_store.DONE, status="done", error=None)

    final = post_store.get(post_id) or {}
    return {
        "remote_post_id": final.get("remote_post_id"),
        "url": final.get("remote_url"),
        "state": final.get("state"),
        "published_at": published,
    }


# --- steps ------------------------------------------------------------------


def _preflight(post: dict[str, Any], images: list[dict[str, Any]], tags: list[str]) -> None:
    from ..store import usage as usage_store

    enriched = []
    for image in images:
        entry = dict(image)
        entry["duplicates"] = usage_store.check(
            image.get("sha256"),
            image.get("phash"),
            pixel_sha256=image.get("pixel_sha256"),
            exclude_post_id=post["id"],
        )
        enriched.append(entry)

    issues = validation.validate(post, enriched, tags)
    blocking = [i for i in issues if i.level == validation.ERROR]
    # The rate limit is not a warning the user acknowledges - it aborts the whole
    # run a few lines below, as PushAborted, so that the remaining posts are left
    # alone rather than each being marked failed in turn.
    unacknowledged = [
        i
        for i in issues
        if i.level == validation.WARNING and i.code not in ratelimit.RATE_LIMIT_CODES
    ]
    if blocking:
        raise RuntimeError("; ".join(i.message for i in blocking))
    if unacknowledged:
        raise RuntimeError("; ".join(i.message for i in unacknowledged))

    hold = ratelimit.check(1)
    if hold:
        raise PushAborted(hold.message)

    # The plan was made for specific bytes. If the file changed since, uploading
    # it anyway would publish something the user never reviewed. Images that are
    # already on the post have no local file and nothing left to upload.
    for image in images:
        if image.get("remote_image_id"):
            continue
        path = Path(image["source_path"])
        from ..hashing import file_sha256

        if file_sha256(path) != image["sha256"]:
            raise RuntimeError(
                f"{path.name} has changed since it was planned. Please review the post again."
            )


def _upload_images(
    run_id: int,
    post_id: int,
    images: list[dict[str, Any]],
    *,
    job: jobs.Job | None,
    record_step: bool = True,
) -> None:
    done = 0
    with TemporaryDirectory(prefix="livebound-upload-") as temporary:
        directory = Path(temporary)
        for image in images:
            # If it already hangs on the post (an adopted post, or a second run after
            # creation) there is nothing to upload - and no local file needed for it.
            if image.get("remote_image_id"):
                done += 1
                continue

            projected_infotext = materialise.upload_infotext(image)
            cache_key = materialise.upload_key(image, projected_infotext)

            # This post's own upload from an earlier attempt. Checked before
            # anything is written: a run that got past the upload and failed at a
            # later step must not send the same bytes a second time, and there is
            # no reason to materialise a copy only to throw it away.
            if uploads.is_fresh(image.get("uploaded_at")) and uploads.staged_for(
                image.get("remote_uuid"), cache_key
            ):
                done += 1
                run_store.set_item(
                    run_id,
                    post_id,
                    **({"step": run_store.UPLOADING} if record_step else {}),
                    images_uploaded=done,
                )
                continue

            # Same bytes may already be staged from an earlier attempt on another post.
            # Looking it up by the full key also invalidates a UUID staged before an
            # edit changed. Old cache rows without the uploaded byte size are not
            # reused: sizeKB must describe what CivitAI actually holds.
            reuse = uploads.fresh_uuid(cache_key)
            if reuse and reuse.get("file_size") is not None:
                file_size = int(reuse["file_size"])
                source = Path(image["source_path"])
                blurhash = image.get("blurhash") or blurhash_for(source)
                with db.transaction() as conn:
                    post_store.record_upload(
                        image["id"],
                        uuid=reuse["uuid"],
                        width=reuse.get("width"),
                        height=reuse.get("height"),
                        file_size=file_size,
                        blurhash=blurhash,
                        conn=conn,
                    )
                image.update(
                    remote_uuid=reuse["uuid"],
                    width=reuse.get("width") or image.get("width"),
                    height=reuse.get("height") or image.get("height"),
                    blurhash=blurhash,
                    file_size=file_size,
                )
                done += 1
                run_store.set_item(
                    run_id,
                    post_id,
                    **({"step": run_store.UPLOADING} if record_step else {}),
                    images_uploaded=done,
                )
                continue

            path = materialise.path_for(image, directory, projected_infotext)
            file_size = path.stat().st_size
            blurhash = image.get("blurhash") or blurhash_for(path)
            source_name = Path(image["source_path"]).name
            _stage(job, f"Uploading {source_name} ({done + 1}/{len(images)})")
            result = _upload(path, image.get("content_type"))

            with db.transaction() as conn:
                post_store.record_upload(
                    image["id"],
                    uuid=result["uuid"],
                    width=result.get("width"),
                    height=result.get("height"),
                    file_size=file_size,
                    blurhash=blurhash,
                    conn=conn,
                )
                uploads.remember(
                    cache_key,
                    result["uuid"],
                    width=result.get("width"),
                    height=result.get("height"),
                    content_type=result.get("content_type"),
                    file_size=file_size,
                    conn=conn,
                )
            path.unlink()
            image.update(
                remote_uuid=result["uuid"],
                width=result.get("width") or image.get("width"),
                height=result.get("height") or image.get("height"),
                blurhash=blurhash,
                file_size=file_size,
            )
            done += 1
            run_store.set_item(
                run_id,
                post_id,
                **({"step": run_store.UPLOADING} if record_step else {}),
                images_uploaded=done,
            )


def _upload(path: Path, content_type: str | None) -> dict[str, Any]:
    """Upload one file, choosing the transport by size.

    The documented MCP tool carries the file base64-encoded inside a JSON-RPC
    body, which inflates it by a third and has to exist as one string in memory
    on both ends. That is fine for a typical render and wrong for a large one, so
    past a threshold the pre-signed PUT is used instead: same bytes, no
    inflation, streamed.

    Preference, not fallback - the documented path is tried first for everything
    it can actually handle.
    """
    size = path.stat().st_size
    if size <= config.MCP_UPLOAD_MAX_BYTES:
        return mcp.upload_image(path, content_type=content_type)

    result = rest.upload_presigned(path, content_type=content_type)
    # The presign path does not probe the image, so fill the dimensions in here;
    # create_post wants them and the file is local anyway.
    try:
        from PIL import Image

        with Image.open(path) as image:
            result["width"], result["height"] = image.size
    except Exception:
        result.setdefault("width", None)
        result.setdefault("height", None)
    return result


def _create(
    run_id: int,
    post_id: int,
    post: dict[str, Any],
    tags: list[str],
) -> None:
    # A marker tag is the strongest signal for finding this post again if the
    # create response is lost. It is removed immediately after creation, before
    # the first image upload begins.
    reconcile_tag = f"livebound-r{run_id}-p{post_id}"
    arguments = {
        "title": post.get("title") or None,
        "detail": post.get("detail") or None,
        "tags": [*tags, reconcile_tag],
        "model_version_id": post.get("model_version_id"),
        "collection_id": post.get("collection_id"),
    }

    # Committed BEFORE the call. If the answer never arrives, this row is the
    # only evidence that a post may exist, and it is what stops a retry from
    # creating a second one.
    import json

    with db.transaction() as conn:
        run_store.set_item(
            run_id,
            post_id,
            conn=conn,
            step=run_store.CREATE_ATTEMPTED,
            create_attempted_at=db.now_iso(),
            create_request_json=json.dumps(arguments, ensure_ascii=False),
            reconcile_tag=reconcile_tag,
        )
        post_store.add_transient_tag(post_id, reconcile_tag, conn=conn)

    with _action_step("push.create_remote", run_id, post_id) as outcome:
        created = _create_remote(arguments)
        outcome["remote_post_id"] = created["id"]
    remote_id = created["id"]
    actual_published_at = created.get("publishedAt")
    remote_state, _ = _remote_state(actual_published_at)

    with db.transaction() as conn:
        post_store.set_fields(
            post_id,
            conn=conn,
            remote_post_id=remote_id,
            remote_url=f"{config.site_base()}/posts/{remote_id}",
            bound_model_version_id=post.get("model_version_id"),
            remote_published_at=actual_published_at,
            remote_state=remote_state,
            last_error=None,
        )
        run_store.set_item(
            run_id, post_id, conn=conn, step=run_store.CREATED, remote_post_id=remote_id
        )
        events.record("created", post_id=post_id, remote_post_id=remote_id, conn=conn)

    if actual_published_at is not None:
        raise RuntimeError(
            "CivitAI created the post with an unexpected publish time. It was requested "
            "as a draft; refresh the post before deciding what to do next."
        )


def _attach_images(
    run_id: int,
    post_id: int,
    post: dict[str, Any],
    images: list[dict[str, Any]],
    *,
    job: jobs.Job | None,
) -> None:
    done = 0
    remote_post_id = post["remote_post_id"]
    for position, image in enumerate(images):
        if image.get("remote_image_id"):
            done += 1
            continue
        if not image.get("remote_uuid"):
            raise RuntimeError("An image has not been uploaded yet. Resume the push.")

        source_name = Path(image["source_path"]).name
        _stage(job, f"Attaching {source_name} ({done + 1}/{len(images)})")
        result = trpc.post_add_image(
            remote_post_id, _create_image_payload(image, position)
        )
        remote_image_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(remote_image_id, int):
            raise CivitaiError(
                "post.addImage returned no image id. Resume the push to check the upload."
            )

        remote_uuid = str(result.get("url") or image["remote_uuid"])
        with db.transaction() as conn:
            post_store.record_attachment(
                image["id"],
                remote_image_id=remote_image_id,
                remote_url=config.image_url(remote_uuid),
                conn=conn,
            )
            uploads.mark_consumed([image["remote_uuid"]], remote_post_id, conn=conn)
            usage.record(
                sha256=image["sha256"],
                pixel_sha256=image.get("pixel_sha256"),
                phash=image.get("phash"),
                post_id=post_id,
                remote_post_id=remote_post_id,
                remote_image_id=remote_image_id,
                source_path=image.get("source_path"),
                post_title=post.get("title"),
                status=usage.PUSHED,
                conn=conn,
            )
        image.update(remote_image_id=remote_image_id, remote_url=config.image_url(remote_uuid))
        done += 1
        run_store.set_item(
            run_id,
            post_id,
            step=run_store.ATTACHING,
            images_uploaded=done,
        )


def _apply_image_settings(images: list[dict[str, Any]]) -> None:
    """Apply image settings only after every image has been attached."""
    for image in images:
        if not image.get("hide_meta"):
            continue
        remote_image_id = image.get("remote_image_id")
        if not isinstance(remote_image_id, int):
            raise RuntimeError("An image has not been attached yet. Resume the push.")
        trpc.post_update_image(remote_image_id, hide_meta=True)


def _create_image_payload(image: dict[str, Any], position: int) -> dict[str, Any]:
    metadata = {
        key: value
        for key, value in (
            ("hash", image.get("blurhash")),
            ("size", image.get("file_size")),
            ("width", image.get("width")),
            ("height", image.get("height")),
        )
        if value is not None
    }
    meta = materialise.upload_document(image)
    return {
        "url": image["remote_uuid"],
        "index": position,
        "type": "image",
        "name": Path(image["source_path"]).name,
        **({"hash": image["blurhash"]} if image.get("blurhash") else {}),
        **({"width": image["width"]} if image.get("width") else {}),
        **({"height": image["height"]} if image.get("height") else {}),
        **({"mimeType": image["content_type"]} if image.get("content_type") else {}),
        **(
            {"sizeKB": round(image["file_size"] / 1024)}
            if image.get("file_size")
            else {}
        ),
        **({"metadata": metadata} if metadata else {}),
        **({"meta": meta} if meta else {}),
    }


def _create_remote(arguments: dict[str, Any]) -> dict[str, Any]:
    """Create the post empty; there is no image-bearing fallback for this order."""
    try:
        result = trpc.post_create(**arguments)
        if isinstance(result, dict) and isinstance(result.get("id"), int):
            return result
        raise CivitaiError("post.create returned no post id")
    except CivitaiError as exc:
        events.record("error", detail={"step": "post.create", "message": str(exc)})
        raise


def _sync_tags(post_id: int, remote_post_id: int) -> None:
    """Bring remote tags in line, and get rid of the reconcile marker first.

    Removing the marker comes before anything else: if it cannot be removed, the
    pipeline stops here and the post stays an unpublished draft. Better an
    invisible draft than a public post carrying an internal marker tag.
    """
    all_tags = post_store.tags(post_id, include_transient=True)
    transient = [tag for tag in all_tags if tag["is_transient"]]

    if transient:
        remote = _remote_tags(remote_post_id)
        if remote is None:
            raise RuntimeError(
                "The post's tags could not be read, so it is unclear whether the internal "
                "marker tag is still on it. The post stays a draft, so it cannot go public "
                "carrying the marker."
            )

        for tag in transient:
            tag_id = tag.get("remote_tag_id") or remote.get(tag["name"].lower())
            if tag_id is None:
                # Confirmed absent: the lookup succeeded and did not list it.
                post_store.drop_tag(post_id, tag["name"])
                continue
            try:
                trpc.post_remove_tag(remote_post_id, tag_id)
                post_store.drop_tag(post_id, tag["name"])
            except NotFound:
                post_store.drop_tag(post_id, tag["name"])
            except CivitaiError as exc:
                raise RuntimeError(
                    f"The internal marker tag '{tag['name']}' could not be removed "
                    f"({exc}). The post stays a draft, so it cannot go public carrying "
                    "the tag."
                ) from exc

    for tag in [t for t in all_tags if not t["is_transient"] and not t.get("remote_tag_id")]:
        try:
            result = trpc.post_add_tag(remote_post_id, tag["name"])
            tag_id = result.get("id") if isinstance(result, dict) else None
            post_store.set_tag_remote_id(post_id, tag["name"], tag_id)
            events.record("tag_added", post_id=post_id, detail={"tag": tag["name"]})
        except CivitaiError as exc:
            # A tag is not worth losing the post over; record and move on.
            events.record(
                "error", post_id=post_id, detail={"tag": tag["name"], "message": str(exc)}
            )


def _remote_tags(remote_post_id: int) -> dict[str, int] | None:
    """``{name: tagId}`` for a post, or ``None`` when it cannot be determined.

    Deliberately via tRPC: the MCP ``get_post`` tool does not return tags at all,
    so asking it would always answer "no such tag" - and the marker tag would
    stay on the post right through publishing.

    ``None`` and ``{}`` mean different things here. Empty means "asked, and there
    are none"; None means "could not ask", which is not the same as safe.
    """
    try:
        detail = trpc.post_get(remote_post_id)
    except CivitaiError:
        return None
    if not isinstance(detail, dict):
        return None
    found: dict[str, int] = {}
    for tag in detail.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        name = tag.get("name")
        tag_id = tag.get("id") or tag.get("tagId")
        if isinstance(name, str) and isinstance(tag_id, int):
            found[name.lower()] = tag_id
    return found


def _apply_publish(run_id: int, post_id: int, post: dict[str, Any]) -> str | None:
    """Set the schedule after attachment, publish now, or leave a draft.

    A schedule re-entry reads first. If the update landed but its response or
    local commit was lost, that read makes the step repeatable without sliding a
    relative time forward on every resume.
    """
    mode = post.get("publish_mode") or "schedule"
    remote_id = post["remote_post_id"]

    if mode == "draft_only":
        return None

    if mode == "now":
        mcp.publish_post(remote_id)
        events.record("published", post_id=post_id, remote_post_id=remote_id)
        return schedule.iso_z(schedule.utcnow())

    confirmed = post.get("remote_published_at")
    if isinstance(confirmed, str) and schedule.parse_iso(confirmed) is not None:
        return confirmed

    remote = trpc.post_get(remote_id)
    remote_published_at = remote.get("publishedAt") if isinstance(remote, dict) else None
    if isinstance(remote_published_at, str) and schedule.parse_iso(remote_published_at):
        _record_publish_time(post_id, remote_id, remote_published_at)
        return remote_published_at

    now = schedule.utcnow()
    when = schedule.resolve(
        mode=post.get("schedule_mode") or schedule.RELATIVE,
        offset_minutes=post.get("schedule_offset_minutes"),
        scheduled_at=post.get("scheduled_at"),
        now=now,
    )
    if when is None:
        raise RuntimeError(
            "No publish time is set. Choose a time and resume the push; the post "
            "is still a draft."
        )

    check = schedule.validate(when, now=now)
    if not check.ok:
        raise LeadTimeExpired(
            f"The publish time cannot be sent ({check.message}). The post is still "
            "a draft; choose a later time and resume the push."
        )

    requested = schedule.iso_z(when)
    with _action_step(
        "push.post_update", run_id, post_id, remote_post_id=remote_id
    ) as outcome:
        remote = trpc.post_update(remote_id, published_at=requested)
        actual = remote.get("publishedAt") if isinstance(remote, dict) else None
        if not _same_publish_time(actual, requested):
            raise RuntimeError(
                "CivitAI did not keep the requested publish time. Refresh the post "
                "before trying again."
            )
        outcome["publish_time_kept"] = True
    _record_publish_time(post_id, remote_id, actual)
    return actual


def _record_publish_time(post_id: int, remote_id: int, published_at: str) -> None:
    """Commit the time read back from CivitAI and the matching local state."""
    remote_state, lifecycle_state = _remote_state(published_at)
    with db.transaction() as conn:
        post_store.set_fields(
            post_id,
            conn=conn,
            remote_published_at=published_at,
            remote_state=remote_state,
            last_error=None,
        )
        lifecycle.transition(post_id, lifecycle_state, conn=conn)
        events.record(
            "scheduled",
            post_id=post_id,
            remote_post_id=remote_id,
            detail={"published_at": published_at},
            conn=conn,
        )


def _remote_state(published_at: Any) -> tuple[str, str]:
    moment = schedule.parse_iso(published_at) if isinstance(published_at, str) else None
    if moment is None:
        return "draft", lifecycle.REMOTE_DRAFT
    if moment > schedule.utcnow():
        return "scheduled", lifecycle.SCHEDULED
    return "published", lifecycle.PUBLISHED


def _same_publish_time(actual: Any, expected: str) -> bool:
    actual_moment = schedule.parse_iso(actual) if isinstance(actual, str) else None
    expected_moment = schedule.parse_iso(expected)
    return expected_moment is not None and actual_moment == expected_moment


# --- helpers ----------------------------------------------------------------


@contextlib.contextmanager
def _action_step(
    stage: str,
    run_id: int,
    post_id: int,
    **detail: Any,
) -> Iterator[dict[str, Any]]:
    """Log both sides of a push step without changing its exception flow."""
    shared = {"run_id": run_id, **detail}
    diagnostics.action(stage, post_id, {**shared, "outcome": "started"})
    outcome: dict[str, Any] = {}
    try:
        yield outcome
    except BaseException as exc:
        diagnostics.action(
            stage,
            post_id,
            {
                **shared,
                **outcome,
                "outcome": "failure",
                "error_type": type(exc).__name__,
            },
        )
        raise
    else:
        diagnostics.action(stage, post_id, {**shared, **outcome, "outcome": "success"})


def _stage(job: jobs.Job | None, text: str) -> None:
    if job is not None:
        job.stage = text


def _step_before(step: str, target: str) -> bool:
    order = run_store.STEP_ORDER
    try:
        return order.index(step) < order.index(target)
    except ValueError:
        return True


def _fail(run_id: int, post_id: int, message: str) -> str:
    post = post_store.get(post_id)
    if post and post.get("remote_post_id"):
        # Writing the interruption diagnostic must not replace the failure that
        # stopped the push. A readback can still make the missing count exact
        # when an addImage response was lost after the write landed.
        with contextlib.suppress(Exception):
            sync.sync_one(post_id, preserve_hide_meta=True)
        post = post_store.get(post_id) or post
        images = post_store.images(post_id)
        missing = sum(not image.get("remote_image_id") for image in images)
        message += f" {missing} of {len(images)} images are missing from CivitAI."
        if post.get("remote_published_at"):
            message += (
                f" The original publish time ({post['remote_published_at']}) still stands; "
                "reschedule it, publish with the attached images, or discard the post."
            )
        else:
            message += (
                " The post is still a draft, so it cannot publish until the push is resumed "
                "or a publish action is chosen."
            )

    run_store.set_item(run_id, post_id, status="error", error=message)
    post_store.set_fields(post_id, last_error=message)
    if post and post["state"] == lifecycle.PUSHING:
        if post.get("remote_post_id"):
            _, target = _remote_state(post.get("remote_published_at"))
        else:
            target = lifecycle.FAILED
        with contextlib.suppress(lifecycle.TransitionError):
            lifecycle.transition(post_id, target)
    return message


def preview(post_id: int) -> dict[str, Any]:
    """Everything the push would send, without sending any of it."""
    post = post_store.detail(post_id)
    if post is None:
        return {"post_id": post_id, "error": "Post not found"}

    tags = post["tags"]
    issues = validation.validate(post, post["images"], tags)
    when = schedule.resolve(
        mode=post.get("schedule_mode") or "relative",
        offset_minutes=post.get("schedule_offset_minutes"),
        scheduled_at=post.get("scheduled_at"),
    )

    return {
        "post_id": post_id,
        "title": post.get("title"),
        "publish_mode": post.get("publish_mode"),
        "resolved_publish_at": schedule.iso_z(when) if when else None,
        "calls": _planned_calls(post, tags, when),
        "comfyui_workflow_replaced_images": sorted(
            Path(image["source_path"]).name
            for image in post["images"]
            if materialise.comfyui_workflow_replaced(image)
        ),
        **validation.summary(issues),
    }


def _planned_calls(
    post: dict[str, Any], tags: list[str], when: datetime | None
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    mode = post.get("publish_mode") or "schedule"
    creates_post = not post.get("remote_post_id")
    if creates_post:
        calls.append(
            {
                "transport": "trpc",
                "call": "post.create",
                "detail": f"empty draft, modelVersionId={post.get('model_version_id')}",
            }
        )
        calls.extend(
            [
                {
                    "transport": "trpc",
                    "call": "post.get",
                    "detail": "find the marker tag id returned with the draft",
                },
                {
                    "transport": "trpc",
                    "call": "post.removeTag",
                    "detail": "if post.get returns the marker tag id",
                },
            ]
        )
    for tag in tags:
        calls.append({"transport": "trpc", "call": "post.addTag", "detail": tag})

    pending = [image for image in post["images"] if not image.get("remote_image_id")]

    # Match the resumable pipeline phases: every required upload completes
    # before the first image is attached. The stored source size determines the
    # route unless it is close enough to the threshold for rewritten metadata
    # to matter; only that narrow case needs an exact materialised copy.
    with contextlib.ExitStack() as preview_files:
        directory: Path | None = None
        for image in pending:
            projected_infotext = materialise.upload_infotext(image)
            cache_key = materialise.upload_key(image, projected_infotext)
            staged = uploads.is_fresh(image.get("uploaded_at")) and uploads.staged_for(
                image.get("remote_uuid"), cache_key
            )
            reuse = None if staged else uploads.fresh_uuid(cache_key)
            if staged or (reuse and reuse.get("file_size") is not None):
                continue

            changed = projected_infotext != (image.get("raw_infotext") or "")
            calls.append(
                {
                    "transport": "local",
                    "call": "materialise_copy",
                    "detail": Path(image["source_path"]).name,
                    "changed": changed,
                }
            )
            stored_size = image.get("file_size")
            near_threshold = not isinstance(stored_size, int) or abs(
                stored_size - config.MCP_UPLOAD_MAX_BYTES
            ) <= _PREVIEW_UPLOAD_ROUTE_MARGIN_BYTES
            if near_threshold:
                if directory is None:
                    directory = Path(
                        preview_files.enter_context(
                            TemporaryDirectory(prefix="livebound-preview-")
                        )
                    )
                upload_size = materialise.path_for(
                    image, directory, projected_infotext
                ).stat().st_size
            else:
                upload_size = stored_size

            if upload_size <= config.MCP_UPLOAD_MAX_BYTES:
                transport, call = "mcp", "upload_image"
            else:
                transport, call = "rest", "upload_presigned"
            calls.append(
                {
                    "transport": transport,
                    "call": call,
                    "detail": Path(image["source_path"]).name,
                }
            )

    for image in pending:
        calls.append(
            {
                "transport": "trpc",
                "call": "post.addImage",
                "detail": Path(image["source_path"]).name,
            }
        )

    for image in post["images"]:
        if image.get("hide_meta"):
            calls.append(
                {
                    "transport": "trpc",
                    "call": "post.updateImage",
                    "detail": (
                        f"{Path(image['source_path']).name}: hideMeta=true; "
                        "image id comes from post.addImage if not already attached"
                    ),
                }
            )

    if mode == "now":
        calls.append({"transport": "mcp", "call": "publish_post", "detail": "immediately"})
    elif (
        mode == "schedule"
        and when
        and schedule.parse_iso(post.get("remote_published_at")) is None
    ):
        calls.append(
            {
                "transport": "trpc",
                "call": "post.get",
                "detail": "check whether an earlier publish-time write succeeded",
            }
        )
        detail = (
            "publishedAt resolved after upload"
            if (post.get("schedule_mode") or schedule.RELATIVE) == schedule.RELATIVE
            else f"publishedAt={schedule.iso_z(when)}"
        )
        calls.append({"transport": "trpc", "call": "post.update", "detail": detail})
        calls.append(
            {
                "transport": "trpc",
                "call": "post.get",
                "detail": "confirm the publish time returned by post.update",
            }
        )
    return calls
