"""Pushing to CivitAI, and reading the result back."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Query

from .. import archive, config
from ..models import (
    AdoptBatchRequest,
    AdoptRequest,
    ArchiveRequest,
    DiscoveryResponse,
    PushRequest,
)
from ..posts import push, schedule, sync
from ..store import runs as run_store
from .common import api_error, guarded, start_job

router = APIRouter(prefix="/api", tags=["publish"])


@router.post("/push/preview")
def preview(payload: PushRequest) -> dict[str, Any]:
    """Every call the push would make, without making any of them.

    The safe way to inspect a batch: it resolves the relative schedules, runs the
    full validation and lists the exact API calls in order.
    """
    return {"items": [push.preview(post_id) for post_id in payload.post_ids]}


@router.post("/push")
def start_push(payload: PushRequest) -> dict[str, Any]:
    """Push one or many posts. Refuses a concurrent run."""
    if not payload.post_ids:
        raise api_error("no_posts_selected", "No posts selected.", 400)
    return start_job(
        "push",
        lambda job: push.push_posts(job, payload.post_ids, dry_run=payload.dry_run),
        code="push_running",
    )


@router.get("/push/runs")
def list_runs(
    limit: int = Query(20, ge=1, le=200),
    hours: int | None = Query(None, ge=1, le=8760),
) -> dict[str, Any]:
    """Recent runs. ``hours`` narrows the log without pruning the table.

    The rows themselves stay: a resume reads them, and they are small. What the
    review page needs is a short list, not a short history.
    """
    since = None
    if hours:
        since = schedule.iso_z(schedule.utcnow() - timedelta(hours=hours))
    return {"items": run_store.list_runs(limit, since)}


@router.get("/push/runs/{run_id}")
def get_run(run_id: int) -> dict[str, Any]:
    run = run_store.get_run(run_id)
    if run is None:
        raise api_error("run_not_found", "Run not found.", 404)
    return run


@router.post("/push/runs/{run_id}/resume")
def resume_run(run_id: int) -> dict[str, Any]:
    """Continue an interrupted run from each post's own cursor."""
    run = run_store.get_run(run_id)
    if run is None:
        raise api_error("run_not_found", "Run not found.", 404)
    unfinished = [
        item["post_id"] for item in run["items"] if item["status"] not in ("done",)
    ]
    if not unfinished:
        raise api_error("nothing_open", "Nothing is left open in this run.", 400)

    return start_job("push", lambda job: push.push_posts(job, unfinished), code="push_running")


@router.post("/sync")
def start_sync(post_ids: list[int] | None = None) -> dict[str, Any]:
    return start_job("sync", lambda job: sync.sync_all(job, post_ids), code="sync_running")


@router.get("/sync/discover", response_model=DiscoveryResponse)
def discover(
    count: int = Query(config.DISCOVER_DEFAULT_LIMIT, ge=1, le=config.DISCOVER_MAX_LIMIT),
    days: int | None = Query(None, ge=1, le=config.DISCOVER_MAX_DAYS),
    cursor: str | None = Query(None, max_length=2000),
) -> dict[str, Any]:
    """Posts on the account that this app does not know about."""
    return guarded(sync.discover, count, days, cursor)


@router.get("/archive/status")
def archive_status() -> dict[str, Any]:
    """Is an archive configured, and how many images would be due?"""
    return guarded(archive.status)


@router.get("/archive/plan")
def archive_plan(limit: int = Query(500, ge=1, le=2000)) -> dict[str, Any]:
    """What would be moved. Moves nothing."""
    return {"items": guarded(archive.plan, limit)}


@router.post("/archive/run")
def start_archive(payload: ArchiveRequest) -> dict[str, Any]:
    """Move. Explicitly manual - never runs by itself."""
    return start_job(
        "archive",
        lambda job: archive.run(job, payload.image_ids or None),
        code="archive_running",
    )


@router.post("/sync/import")
def import_remote(payload: AdoptRequest) -> dict[str, Any]:
    return guarded(sync.import_remote, payload.remote_post_id)


@router.post("/sync/import/batch")
def import_remote_batch(payload: AdoptBatchRequest) -> dict[str, Any]:
    return start_job(
        "adopt",
        lambda job: sync.import_remote_batch(job, payload.remote_post_ids),
        code="adopt_running",
    )
