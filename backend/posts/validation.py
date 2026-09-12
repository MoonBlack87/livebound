"""Everything that must be true before a post may be pushed.

The same function drives the live checklist in the editor and the preflight in
the pipeline, so what the user sees is exactly what the push enforces.

Levels: ``error`` blocks the push outright, ``warning`` blocks it until
acknowledged, ``info`` is only a remark.
"""

from __future__ import annotations

import dataclasses
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import config, db
from ..civitai import ratelimit
from ..store import images as image_store
from . import schedule

ERROR = "error"
WARNING = "warning"
INFO = "info"


@dataclass(frozen=True)
class Issue:
    level: str
    code: str
    message: str
    field: str | None = None
    #: The values the message names. The backend speaks English only; these are
    #: what let the frontend render the same sentence in the user's language
    #: instead of falling back to this one.
    #: ``dataclasses.field`` spelled out: this class already has an
    #: attribute called ``field``, which shadows the plain import.
    params: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate(
    post: dict[str, Any],
    images: list[dict[str, Any]],
    tags: list[str],
    *,
    now: datetime | None = None,
    check_budget: bool = True,
) -> list[Issue]:
    now = now or schedule.utcnow()
    issues: list[Issue] = []

    issues.extend(_check_images(images))
    issues.extend(_check_text(post, tags))
    issues.extend(_check_schedule(post, now))
    issues.extend(_check_binding(post))
    if check_budget:
        issues.extend(_check_budget())
    return issues


def summary(issues: list[Issue]) -> dict[str, Any]:
    errors = [i for i in issues if i.level == ERROR]
    warnings = [i for i in issues if i.level == WARNING]
    return {
        "issues": [i.to_dict() for i in issues],
        "error_count": len(errors),
        "warning_count": len(warnings),
        "can_push": not errors and not warnings,
        "blocked": bool(errors),
    }


# --- individual checks -------------------------------------------------------


def _check_images(images: list[dict[str, Any]]) -> list[Issue]:
    issues: list[Issue] = []
    if not images:
        return [Issue(ERROR, "no_images", "The post has no image.", "images")]

    local_count = sum(1 for image in images if not image.get("remote_image_id"))
    if local_count > config.CIVITAI_POST_IMAGE_LIMIT:
        excess = local_count - config.CIVITAI_POST_IMAGE_LIMIT
        issues.append(
            Issue(
                ERROR,
                "post_image_limit",
                f"A post can have at most {config.CIVITAI_POST_IMAGE_LIMIT} images; "
                f"{local_count} were selected. Remove {excess} image(s).",
                "images",
                {
                    "limit": config.CIVITAI_POST_IMAGE_LIMIT,
                    "count": local_count,
                    "excess": excess,
                },
            )
        )

    for image in images:
        # An image already hanging on the post needs no local file - it is
        # already there. That is the normal case for an adopted post.
        if image.get("remote_image_id"):
            continue

        name = Path(image.get("source_path") or "").name or "?"
        path = Path(image["source_path"]) if image.get("source_path") else None

        if path is None or not path.exists():
            issues.append(
                Issue(
                    ERROR, "file_missing", f"File not found: {name}", "images", {"name": name}
                )
            )
            continue

        size = path.stat().st_size
        if size > config.MAX_IMAGE_FILE_SIZE:
            issues.append(
                Issue(
                    ERROR,
                    "file_too_large",
                    f"{name} is {size / 1024**2:.1f} MB. CivitAI accepts up to "
                    f"{config.MAX_IMAGE_FILE_SIZE / 1024**2:.0f} MB.",
                    "images",
                    {
                        "name": name,
                        "size": f"{size / 1024**2:.1f}",
                        "limit": f"{config.MAX_IMAGE_FILE_SIZE / 1024**2:.0f}",
                    },
                )
            )
        elif size > config.MCP_UPLOAD_MAX_BYTES:
            issues.append(
                Issue(
                    INFO,
                    "large_upload",
                    f"{name} is {size / 1024**2:.1f} MB and therefore goes over the "
                    "direct upload rather than the MCP route.",
                    "images",
                    {"name": name, "size": f"{size / 1024**2:.1f}"},
                )
            )

        unresolved = list(
            dict.fromkeys(
                str(resource.get("name_in_prompt") or resource.get("resource_type") or "?")
                for resource in image_store.resources_for(int(image["image_id"]))
                if resource.get("model_version_id") is None
                and not resource.get("deleted_by_user")
            )
        )
        if unresolved:
            issues.append(
                Issue(
                    INFO,
                    "unresolved_resources",
                    f"{name}: CivitAI may not display or attribute: "
                    f"{', '.join(unresolved)}. Attach a model version to each resource "
                    "you want recognised.",
                    "images",
                    {"name": name, "resources": ", ".join(unresolved)},
                )
            )

        duplicates = image.get("duplicates") or {}
        # A decoded-pixel match counts the same as identical bytes. Generation
        # details cannot promote a perceptual candidate: one request can produce
        # several different images.
        if duplicates.get("has_certain") and not image.get("dedup_ack"):
            previous = (duplicates.get("exact") or duplicates.get("confirmed") or [{}])[0]
            where = previous.get("post_title") or f"Post {previous.get('remote_post_id') or '?'}"
            how = previous.get("match_reason") or (
                "identical bytes" if duplicates.get("has_exact") else "identical pixels"
            )
            issues.append(
                Issue(
                    WARNING,
                    "duplicate_exact",
                    f"{name} was already in a post: {where} ({how}).",
                    "images",
                    {"name": name, "where": where, "how": how},
                )
            )
        elif duplicates.get("has_similar") and not image.get("dedup_ack"):
            near = duplicates["similar"][0]
            issues.append(
                Issue(
                    INFO,
                    "duplicate_similar",
                    f"{name} resembles an image that was already posted "
                    f"(distance {near.get('distance')}).",
                    "images",
                    {"name": name, "distance": near.get("distance")},
                )
            )

    return issues


def _check_text(post: dict[str, Any], tags: list[str]) -> list[Issue]:
    issues: list[Issue] = []
    if not (post.get("title") or "").strip():
        level = ERROR if db.get_setting("require_title") == "1" else INFO
        issues.append(Issue(level, "no_title", "The post has no title.", "title"))
    if not tags:
        issues.append(
            Issue(INFO, "no_tags", "No tags set - the post is harder to find.", "tags")
        )
    return issues


def _check_schedule(post: dict[str, Any], now: datetime) -> list[Issue]:
    mode = post.get("publish_mode") or "schedule"
    if mode == "draft_only":
        return [
            Issue(
                INFO,
                "draft_only",
                "The post is only created as a draft on CivitAI, not published.",
                "schedule",
            )
        ]
    if mode == "now":
        return [
            Issue(
                WARNING,
                "publish_now",
                "This post is published immediately. That cannot be undone - CivitAI "
                "has no unpublish, only delete.",
                "schedule",
            )
        ]

    when = schedule.resolve(
        mode=post.get("schedule_mode") or "relative",
        offset_minutes=post.get("schedule_offset_minutes"),
        scheduled_at=post.get("scheduled_at"),
        now=now,
    )
    if when is None:
        return [
            Issue(ERROR, "no_schedule", "No publish time is set.", "schedule")
        ]

    check = schedule.validate(when, now=now)
    if not check.ok:
        # A relative plan is resolved at push time and cannot be too soon, so only
        # an absolute one can land here for that reason.
        return [Issue(ERROR, check.code, check.message, "schedule", check.params)]
    return []


def _check_binding(post: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    bound = post.get("bound_model_version_id")
    wanted = post.get("model_version_id")
    if bound is not None and wanted != bound:
        issues.append(
            Issue(
                ERROR,
                "binding_locked",
                "The model binding was changed after creation. CivitAI accepts "
                "modelVersionId only at creation - that is what 'rebuild' is for.",
                "model_version_id",
            )
        )
    # A binding is optional, so its absence is not something to fix and has no
    # place on a list of things to fix (`PRE-01`). Where one exists, the editor
    # shows it as the fact it is.
    return issues


def _check_budget() -> list[Issue]:
    hold = ratelimit.check(1)
    if hold is None:
        return []
    return [Issue(WARNING, hold.code, hold.message, "schedule", hold.params)]
