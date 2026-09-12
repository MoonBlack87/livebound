"""Calendar view and batch scheduling."""

from __future__ import annotations

import contextlib
from collections import defaultdict
from typing import Any

from fastapi import APIRouter

from .. import config
from ..civitai import ratelimit
from ..models import SpreadRequest
from ..posts import lifecycle
from ..posts import schedule as sched
from ..store import posts as post_store
from .common import api_error, guard_post_mutation

router = APIRouter(prefix="/api", tags=["schedule"])


@router.get("/schedule/calendar")
def calendar(start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """Everything with a resolved publish time in the window, grouped by day.

    The window is a parameter rather than "everything": with a growing history
    this returned every post that ever had a time, and the calendar can only draw
    a handful of weeks anyway. Without one it still answers for everything, which
    is what an unfiltered first load wants.

    The blocked window is returned rather than described, so the calendar can
    draw the 60 minutes nobody may schedule into instead of explaining them.
    """
    now = sched.utcnow()
    entries: list[dict[str, Any]] = []
    per_day: dict[str, int] = defaultdict(int)

    for post in post_store.list_calendar_candidates(start=start, end=end):
        # The confirmed remote time wins over the local plan; see
        # schedule.effective_publish_at for why re-resolving would drift.
        when = sched.effective_publish_at(post, now=now)
        if when is None:
            continue
        pinned = sched.is_pinned(post)
        stamp = sched.iso_z(when)
        if (start and stamp < start) or (end and stamp > end):
            continue
        day = stamp[:10]
        per_day[day] += 1
        entries.append(
            {
                "post_id": post["id"],
                "title": post["title"],
                "state": post["state"],
                "publish_at": stamp,
                "day": day,
                # A pushed post is no longer "relative" in any meaningful sense -
                # its time is fixed on CivitAI.
                "is_relative": not pinned
                and (post.get("schedule_mode") or "relative") == sched.RELATIVE,
                "pinned": pinned,
                "offset_minutes": None if pinned else post.get("schedule_offset_minutes"),
                "offset_label": ""
                if pinned
                else sched.describe_offset(post.get("schedule_offset_minutes")),
                "remote_post_id": post.get("remote_post_id"),
                "locked": post["state"] == "published",
            }
        )

    budget = ratelimit.budget()
    return {
        "entries": sorted(entries, key=lambda item: item["publish_at"]),
        "per_day": dict(per_day),
        "now": sched.iso_z(now),
        "earliest": sched.iso_z(sched.earliest_publish(now)),
        "latest": sched.iso_z(sched.latest_publish(now)),
        "min_lead_minutes": config.POST_MINIMUM_SCHEDULE_MINUTES,
        "daily_limit": budget["limit"],
        "used_today": budget["used"],
    }


@router.post("/schedule/spread")
def spread(payload: SpreadRequest) -> dict[str, Any]:
    """Stagger many posts at a fixed interval. A preview unless ``apply`` is set.

    Relative offsets are produced when the caller gave a relative start, so a
    staggered batch keeps its spacing no matter when it is finally pushed.
    """
    if not payload.post_ids:
        raise api_error("no_posts_selected", "No posts selected.", 400)
    if payload.interval_minutes < 1:
        raise api_error("interval_too_small", "The interval has to be at least one minute.", 400)

    now = sched.utcnow()
    relative = payload.start_offset_minutes is not None

    if relative:
        base = max(payload.start_offset_minutes, sched.relative_floor_minutes())
        plan = [
            {
                "post_id": post_id,
                "offset_minutes": base + index * payload.interval_minutes,
                "publish_at": sched.iso_z(
                    sched.resolve(
                        mode=sched.RELATIVE,
                        offset_minutes=base + index * payload.interval_minutes,
                        scheduled_at=None,
                        now=now,
                    )
                ),
            }
            for index, post_id in enumerate(payload.post_ids)
        ]
    else:
        if not payload.start:
            raise api_error("no_start_given", "Give a start time or a start offset.", 400)
        times = sched.spread(
            len(payload.post_ids), payload.start, payload.interval_minutes, now=now
        )
        plan = [
            {"post_id": post_id, "publish_at": sched.iso_z(when), "offset_minutes": None}
            for post_id, when in zip(payload.post_ids, times, strict=False)
        ]

    for entry in plan:
        post = post_store.get(entry["post_id"])
        entry["title"] = post["title"] if post else "?"
        entry["locked"] = bool(post and post["state"] == "published")

    if payload.apply:
        # A selected batch is one edit: refuse it before changing any member.
        for entry in plan:
            guard_post_mutation(entry["post_id"], {lifecycle.PUSHING})
        for entry in plan:
            if entry["locked"]:
                continue
            if relative:
                post_store.update(
                    entry["post_id"],
                    schedule_mode=sched.RELATIVE,
                    schedule_offset_minutes=entry["offset_minutes"],
                    publish_mode="schedule",
                )
            else:
                post_store.update(
                    entry["post_id"],
                    schedule_mode=sched.ABSOLUTE,
                    scheduled_at=entry["publish_at"],
                    publish_mode="schedule",
                )
            with contextlib.suppress(lifecycle.TransitionError):
                lifecycle.transition(entry["post_id"], lifecycle.READY)

    return {"applied": payload.apply, "relative": relative, "plan": plan}


@router.get("/schedule/parse-offset")
def parse_offset(text: str) -> dict[str, Any]:
    """Turn "3h 20min" into minutes, for the live hint under the input."""
    minutes = sched.parse_offset(text)
    if minutes is None:
        return {"ok": False, "minutes": None, "label": "", "publish_at": None}
    floor = sched.relative_floor_minutes()
    effective = max(minutes, floor)
    when = sched.resolve(mode=sched.RELATIVE, offset_minutes=minutes, scheduled_at=None)
    return {
        "ok": True,
        "minutes": minutes,
        "effective_minutes": effective,
        "raised": effective > minutes,
        "label": sched.describe_offset(effective),
        "publish_at": sched.iso_z(when) if when else None,
        "floor_minutes": floor,
    }
