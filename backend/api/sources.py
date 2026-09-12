"""Source folders and the scan job."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..models import Ok, SourceCreate, WatchToggle
from ..scanner import service as scanner
from ..store import images as image_store
from ..store import sources as source_store
from .common import api_error, start_job

router = APIRouter(prefix="/api", tags=["sources"])


@router.get("/sources")
def list_sources() -> dict[str, Any]:
    return {"items": source_store.list_roots()}


@router.post("/sources")
def add_source(payload: SourceCreate) -> dict[str, Any]:
    from pathlib import Path

    if not Path(payload.path).expanduser().is_dir():
        raise api_error(
            "folder_not_found", f"Folder not found: {payload.path}", 400, path=payload.path
        )
    return source_store.add_root(
        payload.path,
        label=payload.label,
        excluded=payload.excluded_folders,
        is_archive=payload.is_archive,
    )


@router.post("/sources/{source_id}/watch", response_model=Ok)
def set_source_watch(source_id: int, payload: WatchToggle) -> Ok:
    """Turn the periodic re-scan on or off for this folder. Off by default."""
    source_store.set_watch_enabled(source_id, payload.enabled)
    return Ok()


@router.delete("/sources/{source_id}", response_model=Ok)
def delete_source(source_id: int) -> Ok:
    source_store.delete_root(source_id)
    return Ok()


@router.get("/sources/{source_id}/delete-impact")
def source_deletion_impact(source_id: int) -> dict[str, int]:
    return source_store.deletion_impact(source_id)


@router.post("/sources/scan")
def scan_all(source_id: int | None = None) -> dict[str, Any]:
    """Start a scan. Refuses a second one: two scans would fight over the same rows."""
    ids = [source_id] if source_id else None
    return start_job("scan", lambda job: scanner.scan_roots(job, ids), code="scan_running")


@router.get("/folders")
def list_folders(source_id: int | None = None) -> dict[str, Any]:
    return {"items": image_store.folders(source_id)}
