"""Model bindings and resource resolution."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from fastapi import APIRouter, Query

from .. import config, db
from ..civitai import rest, trpc
from ..civitai.errors import AuthError, CivitaiError
from ..models import ResourceAdd, ResourceAttach, ResourcePatch
from ..posts import lifecycle
from ..resources import cache as resource_cache
from ..store import images as image_store
from .common import api_error, guard_image_mutation, guarded, handle_civitai

router = APIRouter(prefix="/api", tags=["resources"])

_MODEL_ID = re.compile(r"^\d+$")
_MODEL_HASH = re.compile(r"^[0-9a-fA-F]{8,64}$")


def model_search_lookup(query: str) -> tuple[str, int | str]:
    """Classify a binding-picker query without making a CivitAI request."""
    value = query.strip()
    if "/models/" in value:
        try:
            _, version_id = resource_cache.parse_model_url(value)
        except resource_cache.ModelVersionRequired as exc:
            # A URL without a version still names a model, which is an answer
            # worth giving. An unusable id is not, and asking CivitAI for model
            # zero would only spend a request to be told so.
            return ("model", exc.model_id) if exc.model_id >= 1 else ("name", value)
        except resource_cache.InvalidModelUrl:
            return "name", value
        return "version", version_id
    if _MODEL_ID.fullmatch(value):
        return "id", int(value)
    if _MODEL_HASH.fullmatch(value) and any(char.isalpha() for char in value):
        return "hash", value
    return "name", value


@router.get("/models/search")
def search(q: str, types: str | None = None, limit: int = Query(20, ge=1, le=50)) -> dict[str, Any]:
    """Search CivitAI directly, for a model not present in any local image."""
    kinds = [t.strip() for t in types.split(",")] if types else None
    kind, value = model_search_lookup(q)
    item = None
    if kind == "version":
        item = guarded(rest.model_version_by_id, value)
    elif kind == "model":
        item = guarded(rest.model_by_id, value)
    elif kind == "id":
        item = guarded(rest.model_version_by_id, value)
        if item is None:
            item = guarded(rest.model_by_id, value)
    elif kind == "hash":
        payload = guarded(rest.model_version_by_hash, value)
        item = rest.shape_model_version(payload) if payload else None
    if kind == "name" or item is None:
        # An unrecognised identifier can still be a useful name search, rather
        # than leaving a user who pasted it with an unexplained empty panel.
        items = guarded(rest.search_models, q, types=kinds, limit=limit)
    else:
        items = [item]
    return {
        "items": rest.prefer_creators(
            items,
            {
                name
                for name in (db.get_setting("civitai_username"), config.CIVITAI_OFFICIAL_CREATOR)
                if name
            },
        )
    }


@router.get("/tags/suggest")
def suggest_tags(q: str = "", limit: int = Query(15, ge=1, le=50)) -> dict[str, Any]:
    """Tag autocomplete against CivitAI's own tag list.

    Suggesting existing tags matters for discovery: a made-up spelling is a tag
    nobody browses. A failed lookup answers with an empty list rather than an
    error, because a typeahead must never block typing - but not a refused key:
    "no suggestions" would send the user hunting for a tag problem that is
    really an expired token, so that one comes back as a 401.
    """
    try:
        result = trpc.post_get_tags(q, limit=limit)
    except AuthError as exc:
        raise handle_civitai(exc) from exc
    except (CivitaiError, OSError, ValueError):
        return {"items": []}

    items = result.get("items") if isinstance(result, dict) else result
    names = []
    for item in items or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name:
            names.append(name)
    return {"items": names[:limit]}


def _guard_resource(resource_id: int) -> None:
    """Refuse a write to a resource whose image a push is uploading right now.

    A resource row is what the uploaded CivitAI block credits, so changing one
    mid-push changes what the run is sending. A resource that does not exist is
    not this function's business - the route's own 404 says that better.
    """
    image_id = image_store.resource_image_id(resource_id)
    if image_id is not None:
        guard_image_mutation([image_id], {lifecycle.PUSHING})


@router.post("/images/{image_id}/resources")
def add_image_resource(image_id: int, payload: ResourceAdd) -> dict[str, Any]:
    if image_store.get(image_id) is None:
        raise api_error("image_not_found", "Image not found.", 404)
    guard_image_mutation([image_id], {lifecycle.PUSHING})
    digest = (payload.hash or "").strip().upper() or None
    known = resource_cache.get_exact(digest) if digest else None
    try:
        row = image_store.add_resource(
            image_id,
            {
                "resource_type": payload.resource_type,
                "name_in_prompt": payload.name.strip(),
                "weight": payload.weight,
                "hash": digest,
                # A hash the map already answers for is an identity we have; it
                # belongs on the row immediately, not after the next resolve run.
                "model_id": (known or {}).get("model_id"),
                "model_version_id": (known or {}).get("model_version_id"),
                "model_name": (known or {}).get("model_name"),
                "version_name": (known or {}).get("version_name"),
                "resolved_from": "resource_map" if (known or {}).get("model_version_id") else None,
            },
        )
    except sqlite3.IntegrityError as exc:
        # UNIQUE(image_id, resource_type, name_in_prompt): the row is already there.
        raise api_error("resource_exists", "This resource already exists.", 409) from exc
    return row


@router.patch("/resources/{resource_id}")
def patch_image_resource(resource_id: int, payload: ResourcePatch) -> dict[str, Any]:
    current = image_store.resource(resource_id)
    if current is None:
        raise api_error("resource_not_found", "Resource not found.", 404)
    guard_image_mutation([int(current["image_id"])], {lifecycle.PUSHING})
    values: dict[str, Any] = {}
    renamed = payload.name is not None and payload.name.strip() != current["name_in_prompt"]
    if payload.name is not None:
        if not payload.name.strip():
            raise api_error("resource_name_required", "A resource name is required.", 400)
        values["name_in_prompt"] = payload.name.strip()
    if "weight" in payload.model_fields_set:
        values["weight"] = payload.weight
    if values:
        values["locked_by_user"] = 1
    if renamed and not current["added_by_user"]:
        # Keep a tombstone for the file-side identity and add the renamed row as
        # user-owned. Both survive a rebuild keyed by (type, prompt name).
        image_store.update_resource(resource_id, {"deleted_by_user": 1})
        row = image_store.add_resource(
            int(current["image_id"]),
            {
                **current,
                **values,
                "name_in_prompt": payload.name.strip(),
                "locked_by_user": 1,
            },
        )
    else:
        row = image_store.update_resource(resource_id, values)
    return row or {}


@router.post("/resources/{resource_id}/attach")
def attach_image_resource(resource_id: int, payload: ResourceAttach) -> dict[str, Any]:
    current = image_store.resource(resource_id)
    if current is None:
        raise api_error("resource_not_found", "Resource not found.", 404)
    guard_image_mutation([int(current["image_id"])], {lifecycle.PUSHING})
    model, version = payload.model, payload.version
    row = image_store.update_resource(
        resource_id,
        {
            "model_id": model.get("id"),
            "model_version_id": version.get("id"),
            "model_name": model.get("name"),
            "version_name": version.get("name"),
            "hash": version.get("hash_autov2") or version.get("hash_sha256") or current.get("hash"),
            "resolved_from": "manual",
            "locked_by_user": 1,
        },
    )
    file_hash = current.get("hash")
    reapplied = 0
    if file_hash:
        # Counted before the projection runs, because afterwards the rows no
        # longer match the predicate that selects them - and it is the same
        # count the search dialog showed beforehand.
        reapplied = resource_cache.projected_images(
            file_hash, exclude_image_id=int(current["image_id"])
        )
        resource_cache.assign_manual(
            file_hash,
            model.get("id"),
            version.get("id"),
            model_name=model.get("name"),
            version_name=version.get("name"),
            resource_type=model.get("type"),
            thumbnail_url=version.get("thumbnail_url") or model.get("thumbnail_url"),
        )
        resource_cache.reapply_hash(file_hash)
    return {"resource": row or {}, "reapplied": reapplied, "hash": file_hash}


@router.get("/resources/{resource_id}/hash-scope")
def image_resource_hash_scope(resource_id: int) -> dict[str, Any]:
    current = image_store.resource(resource_id)
    if current is None:
        raise api_error("resource_not_found", "Resource not found.", 404)
    file_hash = current.get("hash")
    return {
        "hash": file_hash,
        "images": (
            resource_cache.projected_images(
                file_hash, exclude_image_id=int(current["image_id"])
            )
            if file_hash
            else 0
        ),
    }


@router.post("/resources/{resource_id}/unlock")
def unlock_image_resource(resource_id: int) -> dict[str, Any]:
    current = image_store.resource(resource_id)
    if current is None:
        raise api_error("resource_not_found", "Resource not found.", 404)
    guard_image_mutation([int(current["image_id"])], {lifecycle.PUSHING})
    return image_store.update_resource(resource_id, {"locked_by_user": 0}) or {}


@router.delete("/resources/{resource_id}")
def delete_image_resource(resource_id: int) -> dict[str, Any]:
    _guard_resource(resource_id)
    row = image_store.delete_resource(resource_id)
    if row is None:
        raise api_error("resource_not_found", "Resource not found.", 404)
    return row


@router.post("/resources/{resource_id}/restore")
def restore_image_resource(resource_id: int) -> dict[str, Any]:
    _guard_resource(resource_id)
    row = image_store.restore_resource(resource_id)
    if row is None:
        raise api_error("resource_not_found", "Resource not found.", 404)
    return row
