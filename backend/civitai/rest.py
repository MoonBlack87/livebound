"""The public, read-only REST API (``/api/v1``).

Only used to look things up: which model version a file hash belongs to, and the
model search behind the binding picker. Nothing here writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import config
from . import client
from .errors import CivitaiError, NotFound

#: CivitAI's documented maximum per bulk request; the caller chunks by the
#: same number so one failed chunk cannot discard the ones already paid for.
BULK_HASH_LIMIT = 100


def me() -> dict[str, Any]:
    """The account behind the connection.

    Preferred over the MCP ``whoami`` tool, which currently fails upstream
    (``user.getSelfStatus: Invalid input``). This endpoint is part of the
    documented public API and needs the key, so it doubles as the key test.
    """
    value = client.get_json(
        f"{config.rest_base()}/me", procedure="me", transport="rest", authenticated=True
    )
    return value if isinstance(value, dict) else {}


def upload_presigned(path: Path, *, content_type: str | None = None) -> dict[str, Any]:
    """Upload by asking for a signed URL and PUTting the raw bytes to it.

    The alternative to the MCP ``upload_image`` tool, and the one CivitAI's own
    web client uses. Two differences matter:

    * The bytes go as they are. Base64 inflates a file by a third, so a 12 MB
      PNG becomes a 16 MB JSON body that both ends must hold in memory as one
      string. Here it is streamed.
    * There is no practical size ceiling below CivitAI's own (50 MB per image),
      which is what makes large PNGs and video possible at all.

    The trade-off is that this endpoint is not part of the documented public API,
    so it is used only where the documented path would fail.
    """
    import mimetypes

    mime = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    ticket = client.post_json(
        f"{config.site_base()}/api/v1/image-upload",
        {"filename": path.name, "metadata": {}},
        procedure="image-upload",
        transport="rest",
    )
    upload_url = ticket.get("uploadURL")
    key = ticket.get("id")
    if not isinstance(upload_url, str) or not isinstance(key, str):
        raise CivitaiError("image-upload returned no upload URL")

    client.put_file(upload_url, path, content_type=mime, procedure="image-upload/put")
    return {"uuid": key, "content_type": mime}


def model_version_by_hash(file_hash: str) -> dict[str, Any] | None:
    """Resolve an AutoV2/SHA256 hash to a model version. ``None`` means unknown to CivitAI."""
    try:
        value = client.get_json(
            f"{config.rest_base()}/model-versions/by-hash/{file_hash}",
            procedure="model-versions/by-hash/{hash}",
            transport="rest",
            authenticated=False,
        )
    except NotFound:
        return None
    return value if isinstance(value, dict) else None


def model_by_id(model_id: int) -> dict[str, Any] | None:
    """Look up one model in the binding-picker shape, or return ``None`` when unknown."""
    try:
        value = client.get_json(
            f"{config.rest_base()}/models/{model_id}",
            procedure="models/{id}",
            transport="rest",
            authenticated=False,
        )
    except NotFound:
        return None
    return _shape_model(value) if isinstance(value, dict) else None


def model_version_by_id(version_id: int) -> dict[str, Any] | None:
    """Look up one model version in the binding-picker shape, or return ``None`` when unknown."""
    try:
        value = client.get_json(
            f"{config.rest_base()}/model-versions/{version_id}",
            procedure="model-versions/{id}",
            transport="rest",
            authenticated=False,
        )
    except NotFound:
        return None
    return shape_model_version(value) if isinstance(value, dict) else None


def model_versions_by_hash(file_hashes: list[str]) -> dict[str, dict[str, Any]]:
    """Resolve full SHA256 hashes and keep CivitAI's response shape in this module."""
    hashes = _full_sha256_hashes(file_hashes)
    results: dict[str, dict[str, Any]] = {}
    requested = set(hashes)
    for offset in range(0, len(hashes), BULK_HASH_LIMIT):
        value = client.post_json(
            f"{config.rest_base()}/model-versions/by-hash",
            hashes[offset : offset + BULK_HASH_LIMIT],
            procedure="model-versions/by-hash",
            transport="rest",
            authenticated=False,
            response_type=list,
        )
        for payload in value:
            if not isinstance(payload, dict):
                continue
            for file_hash in _payload_sha256_hashes(payload):
                if file_hash in requested:
                    results[file_hash] = payload
    return results


def _full_sha256_hashes(file_hashes: list[str]) -> list[str]:
    hashes = list(dict.fromkeys(value.strip().upper() for value in file_hashes))
    invalid = any(
        len(value) != 64 or any(char not in "0123456789ABCDEF" for char in value)
        for value in hashes
    )
    if invalid:
        raise ValueError("Bulk model lookup requires full SHA256 hashes.")
    return hashes


def _payload_sha256_hashes(payload: dict[str, Any]) -> list[str]:
    hashes: list[str] = []
    for file_value in payload.get("files") or []:
        if not isinstance(file_value, dict):
            continue
        file_hash = (file_value.get("hashes") or {}).get("SHA256")
        if isinstance(file_hash, str) and len(file_hash) == 64:
            hashes.append(file_hash.upper())
    return hashes


def search_models(
    query: str, *, types: list[str] | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Search models for the binding picker.

    Duplicate display names are common on CivitAI, so each result carries its
    creator and every version - a name-only list cannot be picked from reliably.
    """
    params: dict[str, Any] = {"query": query, "limit": max(1, min(limit, 50))}
    if types:
        params["types"] = types
    value = client.get_json(
        f"{config.rest_base()}/models", params, procedure="models", transport="rest",
        authenticated=False,
    )
    items = value.get("items") if isinstance(value, dict) else None
    return [_shape_model(item) for item in items] if isinstance(items, list) else []


def _shape_model(item: dict[str, Any]) -> dict[str, Any]:
    versions = [_shape_version(version) for version in item.get("modelVersions") or []]
    creator = item.get("creator") or {}
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "type": item.get("type"),
        "nsfw": item.get("nsfw"),
        "creator": creator.get("username"),
        "download_count": (item.get("stats") or {}).get("downloadCount"),
        "versions": versions,
        "thumbnail_url": versions[0]["thumbnail_url"] if versions else None,
    }


def shape_model_version(item: dict[str, Any]) -> dict[str, Any]:
    """Put a version response into the model list shape used by the binding picker."""
    model = item.get("model") or {}
    version = _shape_version(item)
    return {
        "id": item.get("modelId"),
        "name": model.get("name"),
        "type": model.get("type"),
        "nsfw": model.get("nsfw"),
        "creator": None,
        "download_count": None,
        "versions": [version],
        "thumbnail_url": version["thumbnail_url"],
    }


def _shape_version(version: dict[str, Any]) -> dict[str, Any]:
    files = version.get("files") or []
    primary = next((file for file in files if file.get("primary")), files[0] if files else {})
    hashes = primary.get("hashes") or {}
    return {
        "id": version.get("id"),
        "name": version.get("name"),
        "base_model": version.get("baseModel"),
        "published_at": version.get("publishedAt"),
        "hash_autov2": hashes.get("AutoV2"),
        "hash_sha256": hashes.get("SHA256"),
        "thumbnail_url": (version.get("images") or [{}])[0].get("url"),
    }


def prefer_creators(
    items: list[dict[str, Any]], preferred_creators: set[str]
) -> list[dict[str, Any]]:
    """Move preferred creators ahead only for results sharing a version hash."""
    preferred = {name.casefold() for name in preferred_creators}
    official = config.CIVITAI_OFFICIAL_CREATOR.casefold()
    earliest_by_hash: dict[str, int] = {}
    item_hashes: list[list[str]] = []
    for index, item in enumerate(items):
        hashes = [
            str(version.get("hash_autov2") or version.get("hash_sha256")).casefold()
            for version in item.get("versions") or []
            if version.get("hash_autov2") or version.get("hash_sha256")
        ]
        item_hashes.append(hashes)
        for file_hash in hashes:
            earliest_by_hash.setdefault(file_hash, index)

    def sort_key(pair: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
        index, item = pair
        group_index = min(
            (earliest_by_hash[value] for value in item_hashes[index]), default=index
        )
        creator = str(item.get("creator") or "").casefold()
        creator_rank = (
            0 if creator in preferred and creator != official else 1 if creator == official else 2
        )
        return group_index, creator_rank, index

    return [item for _, item in sorted(enumerate(items), key=sort_key)]
