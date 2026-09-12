"""Finding a post whose ``create_post`` answer never arrived.

``create_post`` has no idempotency key. If the response is lost - timeout, crash,
network drop - the post may or may not exist, and a blind retry would create a
second one. So the attempt is committed *before* the call, and a resume that
finds an attempt without an id ends up here instead of creating anything.

Matching is deliberately conservative. Adopting the wrong post would attach this
app's schedule to somebody's existing work, so an ambiguous result is reported as
ambiguous and left for the user rather than guessed at.

**Known limitation, measured against the live API.** The discovery listing does
not return our own drafts. ``post.getInfinite`` only applies ``draftOnly`` inside
its ``isOwnerRequest`` branch; every other path filters ``publishedAt <= NOW()``,
and a draft has no publishedAt at all. Measured over bearer authentication, with
the removed API key and with OAuth - both times the listing came back without
drafts. So the automatic search below returns nothing for the exact case it was
written for.

It is kept because it costs one request and would start working the moment that
changes. The path that actually resolves the situation today is the manual one:
the user opens their drafts in the web UI - where the session does make them the
owner - and pastes the post id. :func:`adopt` then verifies it properly.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .. import config
from ..civitai import account, mcp, trpc
from ..civitai.errors import CivitaiError
from ..store import posts as post_store
from ..store import runs as run_store
from . import schedule

#: How far around the recorded attempt a candidate may have been created.
_WINDOW = timedelta(minutes=30)


def candidates(post_id: int, run_id: int | None = None) -> dict[str, Any]:
    """Drafts on CivitAI that could be the post we may have created."""
    post = post_store.get(post_id)
    if post is None:
        return {"candidates": [], "reason": "The post no longer exists locally."}

    item = run_store.get_item(run_id, post_id) if run_id else None
    attempted = schedule.parse_iso((item or {}).get("create_attempted_at"))
    reconcile_tag = (item or {}).get("reconcile_tag")

    name = account.username()
    if not name:
        return {"candidates": [], "reason": "No CivitAI username known."}

    manual_url = f"{config.site_base()}/user/{name}/posts?section=draft"
    try:
        listing = trpc.post_get_infinite(username=name, draft_only=True, limit=50)
    except CivitaiError as exc:
        return {
            "candidates": [],
            "reason": f"Draft list not retrievable: {exc}",
            "manual_url": manual_url,
        }

    known = _known_remote_ids()
    expected_images = len(list(post_store.images(post_id)))
    expected_title = (post.get("title") or "").strip()

    found: list[dict[str, Any]] = []
    for entry in _items(listing):
        remote_id = entry.get("id")
        if not isinstance(remote_id, int) or remote_id in known:
            continue

        score, reasons = 0, []
        if reconcile_tag and _has_tag(entry, reconcile_tag):
            score += 100
            reasons.append("reconcile tag found")
        if expected_title and (entry.get("title") or "").strip() == expected_title:
            score += 10
            reasons.append("title matches")
        created = schedule.parse_iso(entry.get("createdAt") or entry.get("publishedAt"))
        if attempted and created and abs(created - attempted) <= _WINDOW:
            score += 5
            reasons.append("the time fits")

        if score == 0:
            continue

        detail = _detail(remote_id)
        image_count = detail.get("imageCount") if detail else None
        if isinstance(image_count, int) and expected_images:
            if image_count == expected_images:
                score += 5
                reasons.append(f"{image_count} images, as expected")
            else:
                score -= 10
                reasons.append(f"{image_count} images instead of {expected_images}")

        found.append(
            {
                "remote_post_id": remote_id,
                "title": entry.get("title"),
                "created_at": entry.get("createdAt"),
                "image_count": image_count,
                "score": score,
                "reasons": reasons,
                "url": f"{config.site_base()}/posts/{remote_id}",
            }
        )

    found.sort(key=lambda item: item["score"], reverse=True)
    return {
        "candidates": found,
        "reason": ""
        if found
        else (
            "Drafts do not appear in this list. Open the draft among your own "
            "drafts in CivitAI's web UI, read the post id there, and enter it here."
        ),
        "manual_url": manual_url,
        "reconcile_tag": reconcile_tag,
    }


def auto_adopt(post_id: int, run_id: int | None = None) -> dict[str, Any]:
    """Adopt the single obvious candidate, or report that a decision is needed.

    "Obvious" means one candidate that carries our reconcile tag, or a clear
    lead over the runner-up. Anything else is handed back to the user: a wrong
    adoption would bind this app's schedule to an unrelated post.
    """
    result = candidates(post_id, run_id)
    found = result["candidates"]
    if not found:
        return {"adopted": None, "candidates": [], "reason": result["reason"] or "nothing found"}

    best = found[0]
    runner_up = found[1]["score"] if len(found) > 1 else -999
    if best["score"] >= 100 or (best["score"] >= 15 and best["score"] - runner_up >= 10):
        adopt(post_id, best["remote_post_id"])
        return {"adopted": best, "candidates": found, "reason": ""}
    return {
        "adopted": None,
        "candidates": found,
        "reason": "Several possible matches - please match one by hand.",
    }


def adopt(post_id: int, remote_post_id: int) -> dict[str, Any]:
    """Bind a local post to an existing remote one, after checking it is sane.

    Verified rather than trusted, because a mistyped id would attach this app's
    schedule - and its delete button - to an unrelated post. The checks are:
    the post must exist, must not already belong to another local post, and must
    not be published. A published post is refused outright: the local post is by
    definition not published yet, so that pairing is always a mistake.
    """
    from . import sync

    existing = post_store.get_by_remote(remote_post_id)
    if existing and existing["id"] != post_id:
        raise ValueError(
            f"Post {remote_post_id} is already known locally (as #{existing['id']})."
        )

    try:
        remote = mcp.get_post(remote_post_id)
    except CivitaiError as exc:
        raise ValueError(f"Post {remote_post_id} is not retrievable: {exc}") from exc

    published = schedule.parse_iso(remote.get("publishedAt"))
    if published is not None and published <= schedule.utcnow():
        raise ValueError(
            f"Post {remote_post_id} is already published. An interrupted push cannot "
            "have created it - please check the id."
        )

    post_store.set_fields(
        post_id,
        remote_post_id=remote_post_id,
        remote_url=f"{config.site_base()}/posts/{remote_post_id}",
        last_error=None,
    )
    sync.sync_one(post_id)
    return {"remote_post_id": remote_post_id, "title": remote.get("title")}


def _known_remote_ids() -> set[int]:
    from .. import db

    return {
        row["remote_post_id"]
        for row in db.get_connection().execute(
            "SELECT remote_post_id FROM posts WHERE remote_post_id IS NOT NULL"
        )
    }


def _items(listing: Any) -> list[dict[str, Any]]:
    if isinstance(listing, dict):
        for key in ("items", "posts", "data"):
            value = listing.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    if isinstance(listing, list):
        return [item for item in listing if isinstance(item, dict)]
    return []


def _has_tag(entry: dict[str, Any], tag: str) -> bool:
    tags = entry.get("tags")
    if not isinstance(tags, list):
        return False
    for item in tags:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.lower() == tag.lower():
            return True
    return False


def _detail(remote_id: int) -> dict[str, Any] | None:
    try:
        return mcp.get_post(remote_id)
    except CivitaiError:
        return None
