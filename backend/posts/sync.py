"""Reading CivitAI's version of the truth back.

Local intent and remote state are stored separately, so this never overwrites
what the user wants - it records what CivitAI actually has and flags the
difference. Three things have to be noticed:

* a scheduled post whose time has passed is now published, and its schedule has
  become immutable;
* a post deleted on the site leaves a dangling id here;
* a post edited on the site diverges from what we last sent, and the user has to
  choose which side wins rather than have one silently clobber the other.
"""

from __future__ import annotations

import contextlib
import json
from datetime import timedelta
from typing import Any

from .. import config, db, jobs
from ..civitai import account, mcp, trpc
from ..civitai.errors import CivitaiError, NotFound
from ..store import events, usage
from ..store import posts as post_store
from . import lifecycle, matching, schedule


def sync_one(post_id: int, *, preserve_hide_meta: bool = False) -> dict[str, Any]:
    """Refresh one post from CivitAI and reconcile its local state."""
    post = post_store.get(post_id)
    if post is None:
        return {
            "post_id": post_id,
            "status": "missing_local",
            "changed": 0,
            "divergences": 0,
            "ratings_updated": 0,
        }
    remote_id = post.get("remote_post_id")
    if not remote_id:
        return {
            "post_id": post_id,
            "status": "no_remote",
            "changed": 0,
            "divergences": 0,
            "ratings_updated": 0,
        }

    try:
        remote = _fetch(remote_id)
    except NotFound:
        return _mark_missing(post)
    except CivitaiError as exc:
        post_store.set_fields(post_id, last_error=f"Sync failed: {exc}")
        return {
            "post_id": post_id,
            "status": "error",
            "message": str(exc),
            "changed": 0,
            "divergences": 0,
            "ratings_updated": 0,
        }

    published_at = remote.get("publishedAt")
    remote_state = _classify(published_at)
    diverged = _diverged(post, remote)

    post_store.set_fields(
        post_id,
        remote_published_at=published_at,
        remote_state=remote_state,
        remote_synced_at=db.now_iso(),
        remote_snapshot_json=json.dumps(remote, ensure_ascii=False),
        remote_diverged=1 if diverged else 0,
        remote_url=f"{config.site_base()}/posts/{remote_id}",
        last_error=None,
    )

    target = {
        "draft": lifecycle.REMOTE_DRAFT,
        "scheduled": lifecycle.SCHEDULED,
        "published": lifecycle.PUBLISHED,
    }[remote_state]
    # A refused transition here is information, not a failure: the remote state
    # is recorded either way and the UI shows the mismatch.
    with contextlib.suppress(lifecycle.TransitionError):
        lifecycle.transition(post_id, target)

    ratings_updated = _map_image_ids(
        post_id, remote_id, preserve_hide_meta=preserve_hide_meta
    )

    if remote_state == "published":
        usage.set_status_for_post(post_id, usage.PUBLISHED)
        events.record("published", post_id=post_id, remote_post_id=remote_id)

    fresh = post_store.get(post_id)
    return {
        "post_id": post_id,
        "status": remote_state,
        "published_at": published_at,
        "diverged": diverged,
        "changed": int(
            fresh is not None
            and _reported_post_facts(fresh) != _reported_post_facts(post)
        ),
        "divergences": int(diverged and not post.get("remote_diverged")),
        "ratings_updated": ratings_updated,
    }


def sync_all(job: jobs.Job | None = None, post_ids: list[int] | None = None) -> dict[str, Any]:
    """Refresh every post that has a remote id."""
    if post_ids:
        targets = [
            pid
            for pid in post_ids
            if (post := post_store.get(pid))
            and post.get("remote_post_id")
            and post["state"] != lifecycle.PUSHING
        ]
    else:
        # Archived work is finished. A pushing post belongs to its active push
        # run, so a global sync must not rewrite its remote facts or state.
        targets = [
            row["id"]
            for row in db.get_connection().execute(
                "SELECT id FROM posts WHERE remote_post_id IS NOT NULL"
                " AND state NOT IN ('archived', 'pushing')"
            )
        ]

    totals = {"checked": 0, "published": 0, "missing": 0, "diverged": 0, "errors": 0}
    if job:
        job.total = len(targets)
    for post_id in targets:
        if job and job.should_stop():
            break
        result = sync_one(post_id)
        totals["checked"] += 1
        if result["status"] == "published":
            totals["published"] += 1
        elif result["status"] == "missing_remote":
            totals["missing"] += 1
        elif result["status"] == "error":
            totals["errors"] += 1
        if result.get("diverged"):
            totals["diverged"] += 1
        if job:
            job.processed += 1
            job.log(post_id=post_id, status=result["status"])
    if job:
        job.result = totals
    return totals


def discover(
    limit: int = config.DISCOVER_DEFAULT_LIMIT,
    days: int | None = None,
    cursor: str | int | None = None,
) -> dict[str, Any]:
    """The account's posts that this app does not know about yet.

    Each candidate is classified by its own ``publishedAt``, not by the query
    flag that produced it: ``post.getInfinite({scheduled: true})`` turned out to
    return already published posts, so trusting it would offer years of finished
    work as "scheduled" - and adopting one of those puts a real post within reach
    of the delete button.

    This listing does not return drafts: ``draftOnly`` applies only inside the
    owner branch of ``post.getInfinite``, and every other path filters on
    ``publishedAt``, which a draft has none of (see
    :mod:`~backend.posts.reconcile`). A draft whose post id is already known is
    a different call - ``post.get`` returns it, which is the manual-id route in
    the discover dialog. Scheduled posts do come through, because the service
    has an explicit carve-out for the requester's own future posts.
    """
    name = account.username()
    if not name:
        return {
            "items": [],
            "summary": _empty_discovery_summary(),
            "reason": "No CivitAI username known.",
            "since": None,
            "next_cursor": None,
            "range_complete": True,
            "drafts_not_listable": True,
            "drafts_url": None,
        }

    since = schedule.utcnow() - timedelta(days=days) if days is not None else None

    known = {
        row["remote_post_id"]
        for row in db.get_connection().execute(
            "SELECT remote_post_id FROM posts WHERE remote_post_id IS NOT NULL"
        )
    }

    try:
        listing = trpc.post_get_infinite(
            username=name,
            scheduled=True,
            limit=limit,
            sort="Newest",
            cursor=cursor,
        )
    except CivitaiError as exc:
        return {
            "items": [],
            "summary": _empty_discovery_summary(),
            "reason": str(exc),
            "since": schedule.iso_z(since) if since is not None else None,
            "next_cursor": None,
            "range_complete": False,
            "drafts_not_listable": True,
            "drafts_url": f"{config.site_base()}/user/{name}/posts?section=draft",
        }

    summary = _empty_discovery_summary()
    seen: set[int] = set()
    found: list[dict[str, Any]] = []
    range_complete = False

    for entry in _items(listing):
        remote_id = entry.get("id")
        if not isinstance(remote_id, int) or isinstance(remote_id, bool) or remote_id in seen:
            continue
        seen.add(remote_id)
        summary["examined"] += 1

        published_at = entry.get("publishedAt")
        if not isinstance(published_at, str):
            published_at = None
        published_when = schedule.parse_iso(published_at)
        if since is not None and published_when is not None and published_when < since:
            summary["outside_period"] += 1
            range_complete = True
            continue
        if remote_id in known:
            summary["already_known"] += 1
            continue

        images = entry.get("images")
        cover = images[0] if isinstance(images, list) and images else None
        cover_key = cover.get("url") if isinstance(cover, dict) else None
        cover_url = (
            config.image_url(cover_key, width=450)
            if isinstance(cover_key, str) and cover_key.strip()
            else None
        )
        image_count = entry.get("imageCount")
        if not isinstance(image_count, int) or isinstance(image_count, bool) or image_count < 0:
            image_count = 0
        title = entry.get("title")
        found.append(
            {
                "remote_post_id": remote_id,
                "title": title if isinstance(title, str) else None,
                "published_at": published_at,
                "image_count": image_count,
                "state": _classify(published_at),
                "url": f"{config.site_base()}/posts/{remote_id}",
                "cover_url": cover_url,
            }
        )
        summary["adoptable"] += 1

    found.sort(key=_discovery_newest_key)
    next_cursor = listing.get("nextCursor") if isinstance(listing, dict) else None
    if not isinstance(next_cursor, (str, int)) or isinstance(next_cursor, bool):
        next_cursor = None
    if range_complete:
        next_cursor = None
    return {
        "items": found,
        "summary": summary,
        "reason": "",
        "since": schedule.iso_z(since) if since is not None else None,
        "next_cursor": next_cursor,
        "range_complete": range_complete,
        "drafts_not_listable": True,
        "drafts_url": f"{config.site_base()}/user/{name}/posts?section=draft",
    }


def import_remote_batch(job: jobs.Job, remote_post_ids: list[int]) -> dict[str, Any]:
    """Adopt a bounded list sequentially while keeping each result observable."""
    job.total = len(remote_post_ids)
    outcomes: list[dict[str, Any]] = []

    for remote_post_id in remote_post_ids:
        if job.should_stop():
            break
        job.stage = str(remote_post_id)
        db.get_connection().commit()
        try:
            result = import_remote(remote_post_id)
            db.get_connection().commit()
            created = bool(result.get("created"))
            post_id = result.get("post_id")
            # Adoption already matched the local files without downloading
            # anything (import_remote). Counting the rows afterwards says how
            # far that got, without asking it to report differently.
            images = post_store.images(post_id) if post_id else []
            outcome = {
                "remote_post_id": remote_post_id,
                "post_id": post_id,
                "outcome": "created" if created else "skipped",
                "images": len(images),
                "images_local": sum(1 for row in images if row.get("image_id")),
                "message": "",
            }
            if created:
                job.succeeded += 1
            else:
                job.skipped += 1
        except NotYours as exc:
            db.get_connection().rollback()
            outcome = {
                "remote_post_id": remote_post_id,
                "post_id": None,
                "outcome": "not_yours",
                "images": 0,
                "images_local": 0,
                "message": str(exc),
            }
            job.failed += 1
        except NotFound as exc:
            db.get_connection().rollback()
            outcome = {
                "remote_post_id": remote_post_id,
                "post_id": None,
                "outcome": "not_found",
                "images": 0,
                "images_local": 0,
                "message": str(exc),
            }
            job.failed += 1
        # One post must not abort the rest of an explicitly requested batch.
        except Exception as exc:
            db.get_connection().rollback()
            outcome = {
                "remote_post_id": remote_post_id,
                "post_id": None,
                "outcome": "failed",
                "images": 0,
                "images_local": 0,
                "message": str(exc),
            }
            job.failed += 1
        outcomes.append(outcome)
        job.processed += 1
        job.log(**outcome)
        job.result = {"outcomes": outcomes}

    job.stage = ""
    job.result = {"outcomes": outcomes}
    return job.result


class NotYours(ValueError):
    """The post exists on CivitAI but belongs to somebody else.

    A ValueError so every existing caller keeps handling it as before; the type
    only lets a batch tell "not yours" apart from "no such post".
    """


def import_remote(remote_post_id: int) -> dict[str, Any]:
    """Adopt an existing CivitAI post so it can be managed from here.

    The images stay remote-only: their local files are unknown. The remote state
    still governs scheduling, while title, detail and tags remain editable.

    Refuses a post that belongs to someone else. Not as a safeguard - CivitAI
    enforces that itself, ``post.update`` and ``post.delete`` both run through
    ``isOwnerOrModerator`` and reject a stranger outright. Reading is public, so
    a mistyped id resolves happily to somebody else's post, and without this
    check it would land in the local board as if it were ours: shown as
    scheduled, counted, offered for editing, and answering every write with a
    403. Refusing it here says why, once, instead.
    """
    existing = post_store.get_by_remote(remote_post_id)
    if existing:
        return {"post_id": existing["id"], "created": False}

    remote = _fetch(remote_post_id)

    owner = (remote.get("user") or {}).get("username") if isinstance(remote, dict) else None
    mine = account.username()
    if owner and mine and str(owner).lower() != str(mine).lower():
        raise NotYours(
            f"Post {remote_post_id} belongs to {owner}, not to you. Please check the id."
        )

    published_at = remote.get("publishedAt")
    initial_state = {
        "draft": lifecycle.REMOTE_DRAFT,
        "scheduled": lifecycle.SCHEDULED,
        "published": lifecycle.PUBLISHED,
    }[_classify(published_at)]
    post_id = post_store.create(
        title=remote.get("title") or "",
        detail=remote.get("detail") or "",
        state=initial_state,
        publish_mode="schedule",
        schedule_mode=schedule.ABSOLUTE,
        scheduled_at=published_at,
        origin="imported",
    )
    post_store.set_fields(
        post_id,
        origin="imported",
        remote_post_id=remote_post_id,
        remote_url=f"{config.site_base()}/posts/{remote_post_id}",
    )
    # Fetches the images too - an adopted post must not look empty.
    sync_one(post_id)
    # Cheap matching: if the history already knows these image ids, the local
    # file is there at once. Downloading stays a separate, deliberate action.
    matching.resolve(post_id, download=False)
    return {"post_id": post_id, "created": True}


# --- helpers ----------------------------------------------------------------


def _fetch(remote_id: int) -> dict[str, Any]:
    """Read a post, preferring the richer source.

    The MCP tool returns only id, title and publishedAt. Detecting that someone
    edited the description or the tags on the site needs those fields, so tRPC
    ``post.get`` is asked first and MCP is the fallback - which keeps the sync
    working (just coarser) if that surface ever changes.
    """
    try:
        detail = trpc.post_get(remote_id)
        if isinstance(detail, dict) and "id" in detail:
            return detail
    except NotFound:
        raise
    except CivitaiError:
        pass
    return mcp.get_post(remote_id)


def check_orphaned_usage(remote_post_id: int) -> dict[str, Any]:
    """Release orphaned usage only when CivitAI confirms the post is gone."""
    try:
        _fetch(remote_post_id)
    except NotFound:
        return {
            "status": "missing_remote",
            "released": usage.withdraw_remote_post(remote_post_id),
        }
    except CivitaiError as exc:
        return {"status": "error", "message": str(exc), "released": 0}
    return {"status": "present", "released": 0}


def _map_image_ids(
    post_id: int, remote_id: int, *, preserve_hide_meta: bool = False
) -> int:
    """Learn CivitAI's numeric id for each image of this post.

    Needed for anything that addresses a single image later - hiding its
    metadata, removing it, and naming it when the file is archived. The join is
    exact rather than positional: the remote image carries the very upload key we
    sent, so there is no guessing which local file it came from.

    ``post.getEdit`` is the only view that lists the images of an unpublished
    post, so it is asked here even though the caller already has the post.
    """
    try:
        detail = trpc.post_get_edit(remote_id)
    except CivitaiError:
        return 0
    images = (detail or {}).get("images") if isinstance(detail, dict) else None
    if not isinstance(images, list):
        return 0

    remote_by_uuid = {
        str(image.get("url")): image
        for image in images
        if isinstance(image, dict) and image.get("url") and isinstance(image.get("id"), int)
    }
    remote_by_id = {
        image["id"]: image
        for image in images
        if isinstance(image, dict) and isinstance(image.get("id"), int)
    }
    if not remote_by_id:
        return 0

    local = post_store.images(post_id)
    ratings_updated = 0
    with db.transaction() as conn:
        for row in local:
            remote = remote_by_uuid.get(str(row.get("remote_uuid")))
            if remote is None:
                remote = remote_by_id.get(row.get("remote_image_id"))
            if remote is None:
                continue
            key = remote.get("url")
            nsfw_level = _nsfw_level(remote)
            if row.get("nsfw_level") != nsfw_level:
                ratings_updated += 1
            conn.execute(
                "UPDATE post_images SET remote_image_id=?, remote_url=?, hide_meta=?,"
                " remote_on_site=?, nsfw_level=? WHERE id=?",
                (
                    remote["id"],
                    config.image_url(str(key)) if key else row.get("remote_url"),
                    row.get("hide_meta")
                    if preserve_hide_meta
                    else (1 if remote.get("hideMeta") else 0),
                    1 if _remote_on_site(remote.get("meta")) else 0,
                    nsfw_level,
                    row["id"],
                ),
            )
            conn.execute(
                "UPDATE image_usage SET remote_image_id=? WHERE post_id=? AND sha256=?"
                " AND remote_image_id IS NULL",
                (remote["id"], post_id, row["sha256"]),
            )

    return ratings_updated + _add_remote_only_images(post_id, images, local)


def _add_remote_only_images(
    post_id: int, images: list[Any], local: list[dict[str, Any]]
) -> int:
    """Take in images that exist only on CivitAI.

    An adopted post has no local file for any of its images. Without these rows it
    would stand there as an empty post although it visibly has images - which is
    exactly the impression the first adoption gave.

    Such rows carry no file but the delivery URL: the image can be displayed with
    it, not uploaded. Nor does it need to be - it already hangs on the post.
    """
    known = {str(row.get("remote_uuid")) for row in local if row.get("remote_uuid")}
    known |= {row.get("remote_image_id") for row in local if row.get("remote_image_id")}
    position = max((row["position"] for row in local), default=-1)
    ratings_added = 0

    with db.transaction() as conn:
        for _index, remote in enumerate(images):
            if not isinstance(remote, dict):
                continue
            key = remote.get("url")
            image_id = remote.get("id")
            if not key or not isinstance(image_id, int):
                continue
            if str(key) in known or image_id in known:
                continue

            position += 1
            nsfw_level = _nsfw_level(remote)
            if nsfw_level is not None:
                ratings_added += 1
            conn.execute(
                """
                INSERT INTO post_images(post_id, image_id, position, source_path, sha256,
                    width, height, content_type, media_type, remote_uuid, remote_image_id,
                    remote_url, blurhash, hide_meta, remote_on_site, nsfw_level, dedup_ack)
                VALUES(?,NULL,?,'','',?,?,?,?,?,?,?,?,?,?,?,0)
                """,
                (
                    post_id,
                    position,
                    remote.get("width"),
                    remote.get("height"),
                    remote.get("mimeType"),
                    str(remote.get("type") or "image"),
                    str(key),
                    image_id,
                    config.image_url(str(key)),
                    remote.get("hash"),
                    1 if remote.get("hideMeta") else 0,
                    1 if _remote_on_site(remote.get("meta")) else 0,
                    nsfw_level,
                ),
            )
    return ratings_added


def _nsfw_level(remote: dict[str, Any]) -> int | None:
    """CivitAI's content rating for this image, as the bitmask it sends.

    ``post.getEdit`` carries ``nsfwLevel`` per image
    (``editPostImageSelect``, ``src/server/selectors/post.selector.ts``); the
    value arrived on every sync and was thrown away. ``0`` is stored as ``None``
    on purpose: it means CivitAI has not rated the image yet, which is not the
    same as PG and can still change minutes after the upload.
    """
    value = remote.get("nsfwLevel")
    if not isinstance(value, int) or value <= 0:
        return None
    return value


def _remote_on_site(meta: Any) -> bool:
    """Mirror CivitAI's shared ``isImageMetaOnSite`` predicate exactly."""
    if not isinstance(meta, dict) or "civitaiResources" not in meta or "Version" in meta:
        return False
    model = meta.get("Model")
    return "Model" not in meta or (isinstance(model, str) and model.startswith("urn:air:"))


def _classify(published_at: Any) -> str:
    when = schedule.parse_iso(published_at) if isinstance(published_at, str) else None
    if when is None:
        return "draft"
    return "published" if when <= schedule.utcnow() else "scheduled"


def _diverged(post: dict[str, Any], remote: dict[str, Any]) -> bool:
    """Did someone change the post outside this app?

    Compared against what we last successfully sent, not against local intent -
    otherwise every unsaved local edit would look like remote drift.

    A field the remote read did not carry is skipped rather than treated as
    empty: the coarse MCP fallback returns no ``detail``, and reading that as
    "the description was deleted" would raise a false alarm on every sync.
    """
    pushed = post.get("pushed_fields") or {}
    if not pushed:
        return False

    for key in ("title", "detail"):
        if key in pushed and key in remote and (remote.get(key) or "") != (pushed.get(key) or ""):
            return True

    if "tags" in pushed and "tags" in remote:
        remote_tags = {
            str(tag.get("name", "")).lower()
            for tag in (remote.get("tags") or [])
            if isinstance(tag, dict)
        }
        if remote_tags != {str(name).lower() for name in pushed.get("tags") or []}:
            return True

    if "publishedAt" in pushed:
        left = schedule.parse_iso(pushed.get("publishedAt"))
        right = schedule.parse_iso(remote.get("publishedAt"))
        if left != right:
            return True
    return False


def snapshot_pushed(
    post_id: int, remote: dict[str, Any] | None = None, *, clean: bool = True
) -> None:
    """Record what CivitAI holds right now as the baseline for future comparisons.

    Remote reads are not all equally rich, so fields absent from this read keep
    their last confirmed value. ``clean=False`` records a partially confirmed
    write without clearing local intent. Without a baseline :func:`_diverged`
    can never fire.
    """
    if remote is None:
        post = post_store.get(post_id)
        if not post or not post.get("remote_post_id"):
            return
        try:
            remote = _fetch(post["remote_post_id"])
        except CivitaiError:
            return

    post = post_store.get(post_id)
    baseline: dict[str, Any] = dict((post or {}).get("pushed_fields") or {})
    for key in ("title", "detail", "publishedAt"):
        if key in remote:
            baseline[key] = remote.get(key)
    if "tags" in remote:
        baseline["tags"] = [
            tag.get("name") for tag in (remote.get("tags") or []) if isinstance(tag, dict)
        ]
    fields: dict[str, Any] = {
        "pushed_fields_json": json.dumps(baseline, ensure_ascii=False)
    }
    if clean:
        fields["dirty"] = 0
    post_store.set_fields(post_id, **fields)


def _mark_missing(post: dict[str, Any]) -> dict[str, Any]:
    post_id = post["id"]
    post_store.set_fields(
        post_id,
        remote_state="missing",
        last_error="The post no longer exists on CivitAI.",
    )
    with contextlib.suppress(lifecycle.TransitionError):
        lifecycle.transition(post_id, lifecycle.REMOTE_MISSING)
    usage.set_status_for_post(post_id, usage.WITHDRAWN)
    events.record("deleted", post_id=post_id, remote_post_id=post.get("remote_post_id"))
    fresh = post_store.get(post_id)
    return {
        "post_id": post_id,
        "status": "missing_remote",
        "changed": int(
            fresh is not None
            and _reported_post_facts(fresh) != _reported_post_facts(post)
        ),
        "divergences": 0,
        "ratings_updated": 0,
    }


def _reported_post_facts(post: dict[str, Any]) -> tuple[Any, ...]:
    """User-visible post facts that a reconciliation can change."""
    return (
        post.get("state"),
        post.get("remote_state"),
        post.get("remote_published_at"),
        post.get("remote_diverged"),
        post.get("remote_url"),
        post.get("last_error"),
    )


def _items(listing: Any) -> list[dict[str, Any]]:
    if isinstance(listing, dict):
        for key in ("items", "posts", "data"):
            value = listing.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    if isinstance(listing, list):
        return [item for item in listing if isinstance(item, dict)]
    return []


def _empty_discovery_summary() -> dict[str, int]:
    return {"examined": 0, "outside_period": 0, "already_known": 0, "adoptable": 0}


def _discovery_newest_key(item: dict[str, Any]) -> tuple[bool, float, int]:
    when = schedule.parse_iso(item.get("published_at"))
    return (when is None, -(when.timestamp() if when is not None else 0), -item["remote_post_id"])
