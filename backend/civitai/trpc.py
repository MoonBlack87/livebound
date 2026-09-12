"""The internal tRPC surface - used for shapes MCP cannot express safely.

By far the most important of these is :func:`post_update`: setting a future
``publishedAt`` is the whole point of a scheduling app, and the MCP server has no
tool for it (``publish_post`` stamps "now"). Creating an empty post with that
time and attaching a full browser-shaped image are also separate tRPC calls;
tags, image reordering and per-image metadata visibility are missing from MCP.

This surface supports OAuth bearer authentication, but its individual procedure
schemas are less stable than the public REST contract. Everything that touches it
lives in this one file so a breakage has an obvious blast radius.

Wire format is superjson. A plain JSON body is not enough for a Date: the server
needs the type hint in ``meta.values`` or it stores a string and the schedule is
silently wrong.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .. import config
from . import client
from .errors import CivitaiError, NotFound


def _url(procedure: str) -> str:
    return f"{config.trpc_base()}/{procedure}"


def _decode_devalue(payload: str) -> Any:
    """Decode the flattened array format CivitAI is migrating its responses to.

    Instead of a plain object the body is a JSON array where every value is an
    index into that same array - so shared and cyclic structures survive. Index
    0 is the root; a negative index is a sentinel (-1 undefined, -2 hole, -3 NaN,
    -4/-5 infinities).

    Worth handling rather than ignoring: the switch is per server pool, so the
    same procedure can answer in either format depending on which pod replies.
    Reading only superjson would silently hand callers a raw string.
    """
    try:
        flat = json.loads(payload)
    except ValueError:
        return payload
    if not isinstance(flat, list) or not flat:
        return flat

    sentinels = {-1: None, -2: None, -3: float("nan"), -4: float("inf"), -5: float("-inf")}

    def hydrate(index: Any, seen: frozenset[int] = frozenset()) -> Any:
        if not isinstance(index, int):
            return index
        if index < 0:
            return sentinels.get(index)
        if index >= len(flat) or index in seen:
            return None
        value = flat[index]
        seen = seen | {index}
        if isinstance(value, list):
            return [hydrate(item, seen) for item in value]
        if isinstance(value, dict):
            return {key: hydrate(item, seen) for key, item in value.items()}
        return value

    return hydrate(0)


def _unwrap(response: dict[str, Any], procedure: str) -> Any:
    """Peel the response envelope, turning a tRPC error into a typed exception.

    Two wire formats are in play: superjson (``{"json": ...}``) and the newer
    flattened devalue array. Both appear today depending on which server pool
    answers, so both are read.
    """
    if "error" in response:
        error = response["error"]
        payload = error.get("json", error) if isinstance(error, dict) else {}
        message = payload.get("message") if isinstance(payload, dict) else None
        data = payload.get("data") if isinstance(payload, dict) else None
        status = data.get("httpStatus") if isinstance(data, dict) else None
        text = message or json.dumps(error, ensure_ascii=False)
        if status == 404 or "not found" in text.lower():
            raise NotFound(f"{procedure}: {text}", status=status)
        raise CivitaiError(f"{procedure}: {text}", status=status)

    result = response.get("result")
    if isinstance(result, dict) and "data" in result:
        data = result["data"]
        if isinstance(data, dict) and "json" in data:
            return data["json"]
        if isinstance(data, str):
            return _decode_devalue(data)
        return data
    return result


def mutate(
    procedure: str,
    payload: dict[str, Any],
    *,
    date_fields: tuple[str, ...] = (),
    retryable: bool = True,
) -> Any:
    """POST a superjson mutation.

    ``date_fields`` names the keys whose values are ISO strings that the server
    must interpret as Dates. Without the hint CivitAI stores the raw string and
    the schedule quietly does not apply.
    """
    body: dict[str, Any] = {"json": payload}
    if date_fields:
        body["meta"] = {"values": {field: ["Date"] for field in date_fields}, "v": 1}
    response = client.post_json(
        _url(procedure),
        body,
        procedure=procedure,
        transport="trpc",
        retryable=retryable,
    )
    return _unwrap(response, procedure)


def query(procedure: str, payload: dict[str, Any] | None = None) -> Any:
    """GET a superjson query (``?input={"json":{...}}``)."""
    params = {"input": json.dumps({"json": payload or {}}, ensure_ascii=False)}
    response = client.get_json(_url(procedure), params, procedure=procedure, transport="trpc")
    if not isinstance(response, dict):
        raise CivitaiError(f"{procedure}: unexpected response shape")
    return _unwrap(response, procedure)


# --- post -------------------------------------------------------------------


def post_update(
    post_id: int,
    *,
    title: str | None = None,
    detail: str | None = None,
    published_at: str | None = None,
    collection_id: int | None = None,
) -> Any:
    """Update a post; the only way to set a future ``publishedAt``.

    Callers must have re-validated the 60-minute lead time immediately before
    calling this. CivitAI does not reject a too-near date - it publishes at once.
    ``published_at`` is expected as an ISO-8601 UTC string ending in ``Z``.
    """
    payload: dict[str, Any] = {"id": post_id}
    if title is not None:
        payload["title"] = title
    if detail is not None:
        payload["detail"] = detail
    if collection_id is not None:
        payload["collectionId"] = collection_id

    date_fields: tuple[str, ...] = ()
    if published_at is not None:
        payload["publishedAt"] = published_at
        date_fields = ("publishedAt",)

    result = mutate("post.update", payload, date_fields=date_fields)
    if published_at is not None:
        remote = post_get(post_id)
        actual = remote.get("publishedAt") if isinstance(remote, dict) else None
        if not _same_instant(actual, published_at):
            raise CivitaiError(
                "post.update did not keep the requested publish time. "
                "Refresh the post before trying again."
            )
        return remote
    return result


def post_create(
    *,
    title: str | None = None,
    detail: str | None = None,
    tags: list[str] | None = None,
    model_version_id: int | None = None,
    collection_id: int | None = None,
) -> Any:
    """Create an empty, unpublished post."""
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if detail is not None:
        payload["detail"] = detail
    if tags:
        payload["tags"] = ",".join(tags)
    if model_version_id is not None:
        payload["modelVersionId"] = model_version_id
    if collection_id is not None:
        payload["collectionId"] = collection_id

    return mutate("post.create", payload, retryable=False)


def post_add_image(post_id: int, image: dict[str, Any]) -> Any:
    """Attach one staged upload without changing any post-level field."""
    payload = {key: value for key, value in image.items() if key != "publishedAt"}
    payload["postId"] = post_id
    return mutate("post.addImage", payload)


def _same_instant(actual: Any, expected: str) -> bool:
    def parse(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    expected_instant = parse(expected)
    return expected_instant is not None and parse(actual) == expected_instant


def post_get(post_id: int) -> Any:
    """Full post detail, including its tags.

    The MCP ``get_post`` tool returns only id, title and publishedAt - no tags.
    Removing a tag needs its numeric id, so this is the only way to find one.
    """
    return query("post.get", {"id": post_id})


def post_get_edit(post_id: int) -> Any:
    """Owner-edit detail, including stored image metadata hidden from delivery."""
    return query("post.getEdit", {"id": post_id})


def post_add_tag(post_id: int, name: str) -> Any:
    """Attach a tag by name. Returns the tag, whose id is needed to remove it again."""
    return mutate("post.addTag", {"id": post_id, "name": name.strip().lower()})


def post_remove_tag(post_id: int, tag_id: int) -> Any:
    """Detach a tag. Takes the numeric id, not the name - hence we store both."""
    return mutate("post.removeTag", {"id": post_id, "tagId": tag_id})


def post_update_image(image_id: int, *, hide_meta: bool) -> Any:
    """Toggle whether the generation data is shown under one image."""
    return mutate("post.updateImage", {"id": image_id, "hideMeta": hide_meta})


def post_reorder_images(post_id: int, image_ids: list[int]) -> Any:
    """Set the order of images already attached to a post."""
    return mutate("post.reorderImages", {"id": post_id, "imageIds": image_ids})


def post_get_tags(query_text: str = "", limit: int = 20) -> Any:
    """Tag autocomplete for the editor."""
    return query("post.getTags", {"query": query_text, "limit": limit})


def post_get_infinite(
    *,
    username: str,
    draft_only: bool = False,
    scheduled: bool = False,
    limit: int = 100,
    sort: str | None = None,
    cursor: str | int | None = None,
) -> Any:
    """List the account's own posts.

    Two uses: discovering posts made outside this app, and - more importantly -
    finding a post whose ``create_post`` response was lost, by searching the
    drafts for the reconcile tag we attached before the call.
    """
    payload: dict[str, Any] = {"username": username, "limit": limit}
    if draft_only:
        payload["draftOnly"] = True
    if scheduled:
        payload["scheduled"] = True
    if sort is not None:
        payload["sort"] = sort
    if cursor is not None:
        payload["cursor"] = cursor
    return query("post.getInfinite", payload)
