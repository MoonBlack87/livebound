"""How many more posts may be created today.

CivitAI caps post creation per account (``postRateLimits`` in post.schema.ts) and
answers a breach with a 429 in the middle of a batch. Counting locally beforehand
turns that into "N posts deferred to tomorrow", which is a far better outcome
than half a batch pushed and half failed.

The count comes from ``post_events``, not from ``COUNT(posts)``: a post deleted
locally still counted against the server's limit, and the event row survives.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db


def daily_limit() -> int | None:
    """The account's daily ceiling.

    An explicit setting always wins. CivitAI does not report the score that
    selects its daily tier, so without that setting the ceiling is unknown.
    """
    override = db.get_setting("rate_limit_posts_per_day")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass

    return None


def _created_since(hours: float) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    row = db.get_connection().execute(
        "SELECT COUNT(*) AS n FROM post_events WHERE event='created' AND at > ?", (cutoff,)
    ).fetchone()
    return int(row["n"]) if row else 0


def budget() -> dict[str, int | None]:
    """The local count and, where explicitly configured, its ceiling."""
    limit = daily_limit()
    used = _created_since(24)
    state: dict[str, int | None] = {"limit": limit, "used": used}
    if limit is not None:
        state["remaining"] = max(0, limit - used)
    return state


@dataclass(frozen=True)
class Hold:
    """A reason to hold back, with the values its sentence names.

    The message stays English - the backend has no locale. The code and the
    parameters are what let the frontend say the same thing in German.
    """

    code: str
    message: str
    params: dict[str, Any]


#: The codes :func:`check` can return. The push preflight lets these through
#: rather than treating them as an unacknowledged warning: the rate limit is
#: not something the user acknowledges, it is a reason to stop the whole run.
#: Named here so splitting a code again cannot silently desync the two.
RATE_LIMIT_CODES = frozenset({"rate_limit_daily"})


def check(pending: int = 1) -> Hold | None:
    """Return a reason to hold back, or ``None`` when clear."""
    state = budget()
    limit = state["limit"]
    if limit is not None and state["used"] + pending > limit:
        return Hold(
            "rate_limit_daily",
            f"Daily limit reached: {state['used']} of {limit} posts in the last "
            "24 hours. The push stops here; remaining posts keep their publish times.",
            {"used": state["used"], "limit": limit},
        )
    return None
