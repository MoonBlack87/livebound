"""Shared helpers for the routers."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from ..civitai.errors import (
    AccountNotReady,
    AuthError,
    CivitaiError,
    NotFound,
    RateLimited,
    TransportError,
)
from ..civitai.scopes import MissingScope
from ..posts.lifecycle import TransitionError
from ..store import posts as post_store


def api_error(
    code: str, message: str, status: int = 400, **params: Any
) -> HTTPException:
    """An error the frontend can translate, with an English fallback.

    ``detail`` carries three things: the code for the message catalogue, the
    English sentence to show when the catalogue does not know it, and the values
    the sentence names. Without the values a translated message would have to
    drop them - and "folder not found" without the folder is not much use.

    Every error a user can run into repeatedly is worth a code; a one-off
    diagnostic is not, and passes its text straight through.
    """
    return HTTPException(status, detail={"code": code, "message": message, "params": params})


def guard_post_mutation(
    post_id: int,
    blocked_states: set[str],
    *,
    message: str | None = None,
    **params: str | int,
) -> None:
    post = post_store.get(post_id)
    if post is not None and post["state"] in blocked_states:
        params.setdefault(
            "post",
            f'Post "{post["title"]}" ({post_id})' if post["title"] else f"Post {post_id}",
        )
        raise api_error(
            "post_push_in_progress",
            message
            or "This post is being pushed. Wait for the push to finish before changing it.",
            409,
            **params,
        )


def guard_image_mutation(image_ids: list[int], blocked_states: set[str]) -> None:
    """Refuse an image edit when one of its posts belongs to an active run."""
    holders = post_store.posts_holding_images(image_ids)
    blocked = next(
        (
            post
            for image_id in image_ids
            for post in holders.get(image_id, [])
            if post["state"] in blocked_states
        ),
        None,
    )
    if blocked is None:
        return
    post_label = (
        f'Post "{blocked["title"]}" ({blocked["post_id"]})'
        if blocked["title"]
        else f'Post {blocked["post_id"]}'
    )
    guard_post_mutation(
        blocked["post_id"],
        blocked_states,
        message=f"{post_label} is being pushed. "
        "Wait for the push to finish before changing its images.",
        post=post_label,
    )


def start_job(kind: str, target, *, code: str, exclusive: str | None = None):
    """Start a background job, refusing a concurrent one in the same breath.

    The refusal has to happen under the registry's lock, not before it: asking
    ``jobs.active()`` and starting afterwards is two steps, and two clicks land
    between them often enough to matter - two archive runs moving the same files
    is not something to leave to timing.
    """
    from .. import jobs

    try:
        return jobs.start(kind, target, exclusive=exclusive or kind).to_dict()
    except jobs.AlreadyRunning as exc:
        raise api_error(
            code,
            f"A {exc.job.kind} run is already going (job {exc.job.id}).",
            409,
            job=exc.job.id,
        ) from exc


def handle_civitai(exc: Exception) -> HTTPException:
    """Translate a CivitAI failure into a status the UI can react to.

    The distinctions matter: 401 means "fix your key", 429 means "wait", and a
    refused transition is a 409 the user can resolve, not a server error.
    """
    if isinstance(exc, AuthError):
        code = getattr(exc, "code", None)
        if code:
            return api_error(code, str(exc), 401)
        return HTTPException(401, str(exc))
    if isinstance(exc, NotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, RateLimited):
        return HTTPException(429, str(exc))
    if isinstance(exc, AccountNotReady):
        return HTTPException(409, str(exc))
    if isinstance(exc, TransitionError):
        return api_error(exc.code, str(exc), 409, **exc.params)
    if isinstance(exc, MissingScope):
        return HTTPException(403, str(exc))
    if isinstance(exc, TransportError):
        # The one CivitAI failure a user meets often enough to want in German.
        return api_error(
            "civitai_unreachable", str(exc), 502, procedure=exc.procedure or ""
        )
    if isinstance(exc, CivitaiError):
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code:
            return api_error(code, str(exc), exc.status or 502)
        return HTTPException(502, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(400, str(exc))
    raise exc


def guarded(fn, *args, **kwargs) -> Any:
    """Run a call that may hit CivitAI, mapping its failures to HTTP."""
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:
        raise handle_civitai(exc) from exc


def edit_target_image_ids(image_ids: list[int], post_id: int | None) -> list[int]:
    """Which images a metadata edit affects.

    A post wins over an explicit selection: an edit given a post applies to the
    whole post, not to whatever happened to be ticked. One rule, because the
    completion list has to offer exactly the fields of the images that will be
    changed - if the two drifted apart the list would be a lie.
    """
    if post_id is not None:
        post = post_store.get(post_id)
        if post is None:
            raise api_error("post_not_found", "Post not found.", 404)
        image_ids = [
            int(row["image_id"])
            for row in post_store.images(post_id)
            if row.get("image_id") is not None
        ]
    return list(dict.fromkeys(image_ids))
