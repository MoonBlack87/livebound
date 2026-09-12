"""Model folders and their watched hashing job."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Query

from ..models import ModelFileAssignment, ModelRootCreate, Ok, WatchToggle
from ..resources import cache as resource_cache
from ..resources import model_hashing
from ..store import model_roots as model_store
from .common import api_error, guarded, start_job

router = APIRouter(prefix="/api", tags=["model-roots"])


@router.get("/model-roots")
def list_model_roots() -> dict[str, Any]:
    return {"items": model_store.list_roots(), **model_store.identity_counts()}


@router.get("/model-files")
def list_model_files(
    filter_name: Literal[
        "all", "recognized", "unrecognized", "unqueried", "duplicates"
    ] = "all",
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    items, total = model_store.inventory(
        filter_name=filter_name,
        query=q,
        limit=limit,
        offset=offset,
    )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/model-file-duplicates")
def list_model_file_duplicates(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    groups, total, redundant_size = model_store.duplicate_groups(
        limit=limit, offset=offset
    )
    return {
        "groups": groups,
        "total": total,
        "redundant_size": redundant_size,
        "limit": limit,
        "offset": offset,
    }


@router.get("/model-suggestions")
def model_suggestions(
    q: str = Query("", max_length=200),
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    """Names for a resource somebody is adding by hand, each with its hash.

    Almost nobody knows a model's exact name by heart, and a name that is one
    character off resolves to nothing. Every suggestion carries the hash it came
    from, so choosing one records an identity instead of a guess.
    """
    return {"items": model_store.suggest_resources(q, limit=limit)}


@router.post("/model-files/{file_id}/assignment")
def assign_model_file(file_id: int, payload: ModelFileAssignment) -> dict[str, Any]:
    file = model_store.get_file_by_id(file_id)
    if file is None:
        raise api_error("model_file_not_found", "Model file not found.", 404)
    try:
        model_id, version_id = resource_cache.parse_model_url(payload.url)
    except resource_cache.ModelVersionRequired as exc:
        raise api_error(
            "model_version_required",
            "The URL must include modelVersionId; a model alone is ambiguous.",
        ) from exc
    except resource_cache.InvalidModelUrl as exc:
        raise api_error("model_url_invalid", str(exc)) from exc
    assignment = resource_cache.assign_manual(file["sha256"], model_id, version_id)
    return {
        "assignment": assignment,
        "reapplied": resource_cache.reapply_hash(file["sha256"]),
    }


@router.post("/model-files/{file_id}/resolve")
def resolve_model_file(file_id: int) -> dict[str, Any]:
    file = model_store.get_file_by_id(file_id)
    if file is None:
        raise api_error("model_file_not_found", "Model file not found.", 404)
    stats = guarded(resource_cache.resolve_hashes_bulk, [file["sha256"]], force=True)
    stats["reapplied"] = resource_cache.reapply_hash(file["sha256"])
    return stats


@router.post("/model-roots")
def add_model_root(payload: ModelRootCreate) -> dict[str, Any]:
    if not Path(payload.path).expanduser().is_dir():
        raise api_error(
            "folder_not_found", f"Folder not found: {payload.path}", 400, path=payload.path
        )
    return model_store.add_root(payload.path, label=payload.label)


@router.post("/model-roots/{root_id}/watch", response_model=Ok)
def set_model_root_watch(root_id: int, payload: WatchToggle) -> Ok:
    """Turn the periodic re-hash on or off for this folder. Off by default."""
    model_store.set_watch_enabled(root_id, payload.enabled)
    return Ok()


@router.delete("/model-roots/{root_id}", response_model=Ok)
def delete_model_root(root_id: int) -> Ok:
    model_store.delete_root(root_id)
    return Ok()


@router.post("/model-roots/hash")
def hash_model_roots(root_id: int | None = None) -> dict[str, Any]:
    if root_id is not None and model_store.get_root(root_id) is None:
        raise api_error("model_root_not_found", "Model folder not found.", 404)
    ids = [root_id] if root_id is not None else None
    return start_job(
        "model-hash",
        lambda job: model_hashing.hash_roots(job, ids, user_requested=True),
        code="model_hash_running",
    )
