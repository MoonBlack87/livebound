"""Job polling."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from .. import jobs as job_registry
from ..models import Ok
from .common import api_error

router = APIRouter(prefix="/api", tags=["jobs"])


@router.get("/jobs/active")
def get_job_updates(after: int | None = Query(None, ge=0)) -> dict[str, Any]:
    """Jobs created since ``after``, including ones already finished.

    The first request establishes a cursor and still reports currently running
    work. Later requests cannot miss a short background check merely because it
    started and ended between the browser's five-second polls.
    """
    items, cursor = job_registry.updates(after)
    return {"items": [job.to_dict() for job in items], "cursor": cursor}


# The path converter rejects words during routing. A Python ``int`` annotation
# only rejects them during validation, after the generic route has already won.
@router.get("/jobs/{job_id:int}")
def get_job(job_id: int) -> dict[str, Any]:
    job = job_registry.get(job_id)
    if job is None:
        raise api_error("job_not_found", "Job not found.", 404)
    return job.to_dict()


@router.post("/jobs/{job_id}/cancel", response_model=Ok)
def cancel_job(job_id: int) -> Ok:
    job = job_registry.get(job_id)
    if job is None:
        raise api_error("job_not_found", "Job not found.", 404)
    job.cancel()
    return Ok()
