"""The official CivitAI MCP server for supported post and image operations.

``https://mcp.civitai.com/mcp`` speaks JSON-RPC 2.0 over HTTP POST and
authenticates with the same credential as the other CivitAI surfaces. It covers
uploading images and reading, publishing and deleting posts. Creation, scheduling
and the full image metadata shape go through :mod:`.trpc`, because MCP cannot
express those operations completely.

Worth knowing about the upload path: ``addPostImage`` on the server falls back
to reading the metadata out of the uploaded file itself when the caller sends
none. Every image uses a temporary copy carrying the currently applicable
metadata; its pixels are never re-encoded.
"""

from __future__ import annotations

import base64
import itertools
import mimetypes
from pathlib import Path
from typing import Any

from .. import config
from . import client
from .errors import AccountNotReady, CivitaiError, NotFound

_request_ids = itertools.count(1)


def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke one MCP tool and return its ``result`` object."""
    payload = {
        "jsonrpc": "2.0",
        "id": next(_request_ids),
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    response = client.post_json(
        config.MCP_URL,
        payload,
        procedure=name,
        transport="mcp",
        # the server may answer as a stream even for a single call
        accept="application/json, text/event-stream",
    )

    if "error" in response:
        raise CivitaiError(f"{name}: {response['error']}")

    result = response.get("result")
    if not isinstance(result, dict):
        raise CivitaiError(f"{name}: the response carries no result")

    structured = result.get("structuredContent")
    is_error = result.get("isError") or (
        isinstance(structured, dict) and structured.get("ok") is False
    )
    if is_error:
        text = " ".join(
            item.get("text", "")
            for item in result.get("content", [])
            if isinstance(item, dict)
        ).strip()
        message = text or str(structured)
        # Measured 2026-08-22 on the deleted posts 30548631 and 30548684:
        # CivitAI answers "Could not find entity", which matches neither of the
        # phrases this used to look for. tRPC keys off httpStatus 404 and was
        # never affected; what broke here is `operations.delete_remote`, which
        # suppresses NotFound so that deleting an already-deleted post is a
        # no-op.
        lowered = message.lower()
        if any(
            phrase in lowered
            for phrase in ("not found", "no post", "could not find", "does not exist")
        ):
            raise NotFound(f"{name}: {message}")
        if "muted" in lowered or "onboard" in lowered:
            raise AccountNotReady(f"{name}: {message}")
        raise CivitaiError(f"{name}: {message}")
    return result


def find_key(value: Any, key: str) -> Any:
    """Depth-first search for ``key`` anywhere in a nested result.

    The MCP tools do not guarantee where in ``structuredContent`` a field sits,
    and the shape has moved between versions. Searching is more durable than
    hard-coding a path, and a wrong guess would be caught by the callers below,
    which all validate the type of what they get back.
    """
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = find_key(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_key(child, key)
            if found is not None:
                return found
    return None


def _structured(result: dict[str, Any], tool: str) -> Any:
    structured = result.get("structuredContent")
    if structured is None:
        raise CivitaiError(f"{tool}: the response carries no structured data")
    return structured


# --- tools ------------------------------------------------------------------


def whoami() -> dict[str, Any]:
    """Resolve the account behind the connection, with its authoritative status.

    Checked before any batch: an un-onboarded account fails every guarded write
    and a muted one fails most, so finding out here beats finding out after
    twenty uploads.
    """
    structured = _structured(call("whoami", {}), "whoami")
    return structured if isinstance(structured, dict) else {"raw": structured}


def upload_image(path: Path, *, content_type: str | None = None) -> dict[str, Any]:
    """Upload one file and return ``{uuid, width, height}``.

    The chosen temporary copy is sent byte for byte, base64 encoded. This
    function never resizes or re-saves it.
    """
    mime = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    result = call("upload_image", {"data": encoded, "contentType": mime})
    structured = _structured(result, "upload_image")

    uuid = find_key(structured, "uuid")
    if not isinstance(uuid, str) or not uuid:
        raise CivitaiError(f"upload_image returned no UUID for {path.name}")

    width = find_key(structured, "width")
    height = find_key(structured, "height")
    return {
        "uuid": uuid,
        "width": width if isinstance(width, int) else None,
        "height": height if isinstance(height, int) else None,
        "content_type": mime,
    }


def get_post(post_id: int) -> dict[str, Any]:
    structured = _structured(call("get_post", {"id": post_id}), "get_post")
    if not isinstance(structured, dict):
        raise CivitaiError(f"get_post({post_id}) returned no structured data")
    return structured


def delete_post(post_id: int) -> None:
    call("delete_post", {"id": post_id})


def publish_post(post_id: int) -> dict[str, Any]:
    """Publish immediately. Only ever called from an explicit user action."""
    structured = _structured(call("publish_post", {"id": post_id}), "publish_post")
    return structured if isinstance(structured, dict) else {}
