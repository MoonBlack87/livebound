"""All scheduling arithmetic, in one place.

The 60-minute rule is the app's single most dangerous constraint, because
CivitAI does not enforce it the way one would expect. From ``updatePostHandler``:

    if (input.publishedAt && dayjs(input.publishedAt).isBefore(minimumScheduleTime))
      input.publishedAt = today;

A date that is too close is **not** rejected - the post is published immediately.
There is no unpublish. So every path that could produce such a date has to be
checked here immediately before the call.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    """The single wire format: UTC, milliseconds, trailing ``Z``.

    Milliseconds rather than seconds because that is what CivitAI's own client
    sends, and the superjson Date hint parses it either way.
    """
    return (
        value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO string into an aware UTC datetime. Naive input is read as UTC.

    Treating a naive string as local time would be the more "helpful" guess and
    exactly the wrong one: the database only ever holds UTC, and a silent
    two-hour shift here is a post published at the wrong time.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def add_months(value: datetime, months: int) -> datetime:
    """Calendar-correct month arithmetic, clamping the day (31 Jan + 1 month = 28/29 Feb)."""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def earliest_publish(now: datetime | None = None) -> datetime:
    """The first instant CivitAI will accept as a *scheduled* time."""
    now = now or utcnow()
    return now + timedelta(minutes=config.POST_MINIMUM_SCHEDULE_MINUTES)


def latest_publish(now: datetime | None = None) -> datetime:
    return add_months(now or utcnow(), config.POST_MAXIMUM_SCHEDULE_MONTHS)


@dataclass(frozen=True)
class ScheduleCheck:
    ok: bool
    code: str = ""
    message: str = ""
    #: The values the message names, so a translation can keep them.
    params: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


OK = ScheduleCheck(True)


def validate(when: datetime | str | None, *, now: datetime | None = None) -> ScheduleCheck:
    """Is this a legal scheduled publish time?"""
    now = now or utcnow()
    moment = parse_iso(when) if isinstance(when, str) else when
    if moment is None:
        return ScheduleCheck(False, "missing", "No publish time is set.")

    floor = earliest_publish(now)
    if moment < floor:
        minutes = max(0, int((moment - now).total_seconds() // 60))
        return ScheduleCheck(
            False,
            "too_soon",
            f"CivitAI requires at least {config.POST_MINIMUM_SCHEDULE_MINUTES} minutes of "
            f"lead time; this one is {minutes} minutes ahead. A time that is too "
            "close is not rejected but published immediately.",
            {"lead": config.POST_MINIMUM_SCHEDULE_MINUTES, "minutes": minutes},
        )

    ceiling = latest_publish(now)
    if moment > ceiling:
        return ScheduleCheck(
            False,
            "too_far",
            f"CivitAI schedules at most {config.POST_MAXIMUM_SCHEDULE_MONTHS} months ahead.",
            {"months": config.POST_MAXIMUM_SCHEDULE_MONTHS},
        )
    return OK


def spread(
    count: int,
    start: datetime | str,
    interval_minutes: int,
    *,
    now: datetime | None = None,
) -> list[datetime]:
    """Evenly spaced publish times, starting at ``start``.

    The first slot is pulled forward to the earliest legal instant if the caller
    asked for something too soon, so a spread never silently produces a
    publish-now. Later slots keep the requested spacing from there.
    """
    now = now or utcnow()
    begin = parse_iso(start) if isinstance(start, str) else start
    if begin is None:
        raise ValueError("Not a valid start time")
    floor = earliest_publish(now)
    begin = max(begin, floor)
    step = timedelta(minutes=max(1, interval_minutes))
    return [begin + step * index for index in range(max(0, count))]


# --- relative planning -------------------------------------------------------
#
# A post can sit in the local queue for days before it is pushed. An absolute
# time picked while drafting may therefore already be in the past when the push
# finally happens - and a past publishedAt does not fail, it publishes at once.
#
# A relative plan ("in 3h 20m") sidesteps that entirely: it is resolved against
# the clock in the closing publish-time step, after every image is attached, so
# it cannot go stale or spend its lead time on an upload.

RELATIVE = "relative"
ABSOLUTE = "absolute"


def resolve(
    *,
    mode: str,
    offset_minutes: int | None,
    scheduled_at: str | datetime | None,
    now: datetime | None = None,
) -> datetime | None:
    """Turn a stored plan into the wall-clock time to send.

    During a push, callers resolve here only in the closing publish-time step.
    Relative offsets below the legal minimum are raised to the earliest allowed
    instant instead of being rejected: "in 30 minutes" is a clear intent, and the
    honest answer is "the soonest CivitAI permits", not a failed push. Absolute
    times are returned untouched so that :func:`validate` can judge them.
    """
    now = now or utcnow()
    if mode == RELATIVE:
        if offset_minutes is None:
            return None
        moment = now + timedelta(minutes=max(0, offset_minutes))
        floor = earliest_publish(now)
        return max(moment, floor)
    return parse_iso(scheduled_at) if isinstance(scheduled_at, str) else scheduled_at


def describe_offset(minutes: int | None) -> str:
    """"in 3 h 20 min" - the label the board and the calendar show."""
    if minutes is None:
        return ""
    minutes = max(0, int(minutes))
    days, rest = divmod(minutes, 60 * 24)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} d")
    if hours:
        parts.append(f"{hours} h")
    if mins or not parts:
        parts.append(f"{mins} min")
    return "in " + " ".join(parts)


def parse_offset(text: str) -> int | None:
    """Read "90", "1h30", "3h 20min", "2d 4h" into minutes. ``None`` if unparsable.

    Accepting a bare number as minutes matters: it is what someone types first,
    and guessing hours there would schedule a post two days late.
    """
    import re

    text = (text or "").strip().lower().replace(",", ".")
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        return int(text)

    total = 0.0
    found = False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(d|h|m|min|std|tage?|stunden?)?", text):
        if not amount:
            continue
        value = float(amount)
        unit = unit or "m"
        if unit.startswith(("d", "tag")):
            total += value * 60 * 24
        elif unit.startswith(("h", "std", "stunde")):
            total += value * 60
        else:
            total += value
        found = True
    return round(total) if found else None


def relative_floor_minutes() -> int:
    """Smallest offset the UI should offer, in whole minutes."""
    return config.POST_MINIMUM_SCHEDULE_MINUTES


def effective_publish_at(post: dict, *, now: datetime | None = None) -> datetime | None:
    """When this post actually goes live, as far as anyone can know.

    Once CivitAI has confirmed a ``publishedAt``, that is the answer - it is what
    the platform will act on, and nothing local overrides it.

    Only an unpushed post falls back to the local plan. That distinction is the
    whole bug this function exists to prevent: a relative plan re-resolved
    against the current clock slides forward every time it is displayed, so a
    post already fixed at 01:55 on CivitAI kept being shown an hour later each
    time the page was opened - and in the wrong order relative to its neighbours.
    """
    confirmed = parse_iso(post.get("remote_published_at"))
    if confirmed is not None:
        return confirmed
    return resolve(
        mode=post.get("schedule_mode") or RELATIVE,
        offset_minutes=post.get("schedule_offset_minutes"),
        scheduled_at=post.get("scheduled_at"),
        now=now,
    )


def is_pinned(post: dict) -> bool:
    """Is the time fixed on CivitAI rather than still a local intention?"""
    return parse_iso(post.get("remote_published_at")) is not None
