"""Local LLM suggestions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import db
from ..llm import endpoint, profiles, service
from ..models import Ok, ProfilePatch, ProfileUpsert, ShortenRequest, SuggestRequest
from ..posts import lifecycle
from .common import api_error, guard_post_mutation, start_job

router = APIRouter(prefix="/api/llm", tags=["llm"])


@router.get("/status")
def status() -> dict[str, Any]:
    return service.status()


@router.get("/endpoint/models")
def endpoint_models() -> dict[str, Any]:
    """What the configured server has, so a model is picked and never typed.

    A server that is not running answers with an empty list rather than an
    error: the settings page uses this to decide what to show, and "not running"
    is something it can say.
    """
    url = service.configured_endpoint() or endpoint.DEFAULT_URL
    names = endpoint.list_models(url)
    # A server that is down and one that is empty need different advice.
    return {"items": names or [], "url": url, "reachable": names is not None}


@router.get("/profiles")
def list_profiles() -> dict[str, Any]:
    return {
        "items": profiles.list_profiles(),
        "default_id": db.get_setting("llm_default_profile_id"),
    }


@router.post("/profiles")
def create_profile(payload: ProfileUpsert) -> dict[str, Any]:
    # No name check: the name is a label, the uuid is the identity, and the uuid
    # is generated here rather than supplied.
    return profiles.create(payload.model_dump())


@router.patch("/profiles/{profile_id}")
def patch_profile(profile_id: int, payload: ProfilePatch) -> dict[str, Any]:
    result = profiles.update(profile_id, payload.model_dump(exclude_unset=True))
    if result is None:
        raise api_error("profile_not_found", "Profile not found.", 404)
    return result


@router.post("/profiles/restore")
def restore_profiles() -> dict[str, Any]:
    """Put the four shipped profiles back. The user's own are left alone."""
    return {"restored": profiles.restore_builtins()}


@router.get("/profiles/{profile_id}/seed")
def profile_seed(profile_id: int) -> dict[str, str]:
    system_prompt = profiles.seed_system_prompt(profile_id)
    if system_prompt is None:
        raise api_error("profile_seed_not_found", "This profile has no built-in seed.", 404)
    return {"system_prompt": system_prompt}


@router.delete("/profiles/{profile_id}", response_model=Ok)
def delete_profile(profile_id: int) -> Ok:
    try:
        deleted = profiles.delete(profile_id)
    except profiles.LastProfileError as exc:
        raise api_error(
            "last_profile",
            "Create another profile before deleting the last one.",
            409,
        ) from exc
    if not deleted:
        raise api_error("profile_not_found", "Profile not found.", 404)
    return Ok()


@router.post("/profiles/{profile_id}/default", response_model=Ok)
def set_default(profile_id: int) -> Ok:
    if not profiles.set_default(profile_id):
        raise api_error("profile_not_found", "Profile not found.", 404)
    return Ok()


@router.post("/suggest")
def suggest(payload: SuggestRequest) -> dict[str, Any]:
    """Start a suggestion run. One at a time - the model is a single GPU resource."""
    if not payload.post_ids:
        raise api_error("no_posts_selected", "No posts selected.", 400)
    state = service.status()
    if not state["ready"]:
        raise api_error(
            "llm_not_ready",
            "The local model is not ready: "
            + ("no interpreter. " if not state["python_exists"] else "")
            + ("no model folder." if not state["model_dir_exists"] else ""),
            400,
        )

    return start_job(
        "llm",
        lambda job: service.suggest(
            job, payload.post_ids, payload.profile_id, payload.hint, payload.seed,
            payload.material,
        ),
        code="llm_running",
    )


@router.post("/shorten")
def shorten(payload: ShortenRequest) -> dict[str, Any]:
    """Generate prompt suggestions; the edit bulk endpoint is the accept step."""
    state = service.status()
    if not state["ready"]:
        raise api_error("llm_not_ready", "The local model is not ready.", 400)
    return start_job(
        "llm", lambda job: service.shorten_prompts(job, payload.image_ids), code="llm_running"
    )


@router.get("/suggestions/{post_id}")
def suggestions(post_id: int) -> dict[str, Any]:
    return {"items": service.suggestions_for(post_id)}


@router.post("/suggestions/{suggestion_id}/accept")
def accept(suggestion_id: int) -> dict[str, Any]:
    suggestion = db.get_connection().execute(
        "SELECT post_id FROM llm_suggestions WHERE id=?", (suggestion_id,)
    ).fetchone()
    if suggestion is not None:
        guard_post_mutation(int(suggestion["post_id"]), {lifecycle.PUSHING})
    try:
        return service.accept(suggestion_id)
    except service.LlmError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/suggestions/{suggestion_id}")
def dismiss(suggestion_id: int) -> dict[str, Any]:
    try:
        return service.dismiss(suggestion_id)
    except service.LlmError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/stop", response_model=Ok)
def stop() -> Ok:
    """Unload the model and free the GPU."""
    service.stop_worker()
    return Ok()
