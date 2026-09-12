"""The post state machine. The only place ``posts.state`` is ever written.

Centralising this is not tidiness. Three of CivitAI's rules are irreversible and
cannot be expressed as UI hints alone:

* a published post cannot be rescheduled - ``publishedAt`` is frozen once passed;
* there is no unpublish, so the way back to a draft is deletion;
* ``modelVersionId`` is settable only at creation - ``post.update`` has no such
  field, so an "update" of the binding is silently ignored rather than refused.

If any of those could be violated by a code path that forgot to check, the user
would find out by looking at their profile. So the guards live on the transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import db

# --- states -----------------------------------------------------------------

DRAFT = "draft"                     # local only, no publish time yet
READY = "ready"                     # local only, time set, waiting to be pushed
PUSHING = "pushing"                 # a push run owns this post right now
NEEDS_RECONCILE = "needs_reconcile"  # create_post may or may not have landed
REMOTE_DRAFT = "remote_draft"       # exists on CivitAI, unpublished
SCHEDULED = "scheduled"             # on CivitAI with a future publishedAt
PUBLISHED = "published"             # live
FAILED = "failed"                   # push aborted; resumable
REMOTE_MISSING = "remote_missing"   # we hold an id CivitAI no longer knows
ARCHIVED = "archived"               # filed away locally

LOCAL_STATES = {DRAFT, READY, FAILED, ARCHIVED}
REMOTE_STATES = {REMOTE_DRAFT, SCHEDULED, PUBLISHED}
BUSY_STATES = {PUSHING, NEEDS_RECONCILE}

ALL_STATES = LOCAL_STATES | REMOTE_STATES | BUSY_STATES | {REMOTE_MISSING}

#: Allowed transitions. Anything absent here is refused - including the three
#: rules above, which appear as the deliberate *absence* of an edge:
#: SCHEDULED/PUBLISHED never go back to REMOTE_DRAFT, and PUBLISHED never
#: returns to SCHEDULED.
#: `ARC-01`: **only posts verified as published can be archived.** The archive is
#: the historical record of what actually went out; a plan that was given up on
#: is deleted instead - "would then never have been a post at all". So `draft`,
#: `ready` and `failed` have no edge to `archived`: none of them was ever
#: published. `remote_missing` keeps one, but only for a post that carries a
#: publication date - see `_refuse_unpublished_archive`.
TRANSITIONS: dict[str, set[str]] = {
    DRAFT: {READY, PUSHING},
    READY: {DRAFT, PUSHING},
    PUSHING: {REMOTE_DRAFT, SCHEDULED, PUBLISHED, FAILED, NEEDS_RECONCILE, READY},
    NEEDS_RECONCILE: {REMOTE_DRAFT, SCHEDULED, PUBLISHED, DRAFT, READY, FAILED},
    REMOTE_DRAFT: {SCHEDULED, PUBLISHED, PUSHING, FAILED, REMOTE_MISSING, DRAFT},
    SCHEDULED: {SCHEDULED, PUBLISHED, PUSHING, FAILED, REMOTE_MISSING, DRAFT},
    PUBLISHED: {PUBLISHED, REMOTE_MISSING, DRAFT, ARCHIVED},
    FAILED: {READY, DRAFT, PUSHING, NEEDS_RECONCILE, REMOTE_MISSING},
    REMOTE_MISSING: {DRAFT, READY, ARCHIVED},
    ARCHIVED: {DRAFT, READY},
}


ERROR_UNKNOWN_STATE = "transition_unknown_state"
ERROR_POST_NOT_FOUND = "transition_post_not_found"
ERROR_NOT_ALLOWED = "transition_not_allowed"
ERROR_BINDING_LOCKED = "transition_binding_locked"
ERROR_RESCHEDULE_PUBLISHED = "transition_reschedule_published"
ERROR_ARCHIVE_UNPUBLISHED = "transition_archive_unpublished"

TRANSITION_ERROR_CODES = frozenset(
    {
        ERROR_UNKNOWN_STATE,
        ERROR_POST_NOT_FOUND,
        ERROR_NOT_ALLOWED,
        ERROR_BINDING_LOCKED,
        ERROR_RESCHEDULE_PUBLISHED,
        ERROR_ARCHIVE_UNPUBLISHED,
    }
)


class TransitionError(RuntimeError):
    """A refused state change with a stable frontend translation contract."""

    def __init__(self, code: str, message: str, **params: Any):
        super().__init__(message)
        self.code = code
        self.params = params


@dataclass(frozen=True)
class StateInfo:
    state: str
    label: str
    #: Can the publish time still be changed?
    can_schedule: bool
    #: Can title/detail/tags still be changed on CivitAI?
    can_edit_remote: bool
    #: Does a CivitAI post exist that deletion would remove?
    has_remote: bool


#: The label is for error messages and the log, not for the UI - the frontend
#: translates a state itself, from the state name.
INFO: dict[str, StateInfo] = {
    DRAFT: StateInfo(DRAFT, "draft", True, False, False),
    READY: StateInfo(READY, "ready", True, False, False),
    PUSHING: StateInfo(PUSHING, "pushing", False, False, False),
    NEEDS_RECONCILE: StateInfo(NEEDS_RECONCILE, "needs reconcile", False, False, False),
    REMOTE_DRAFT: StateInfo(REMOTE_DRAFT, "draft on CivitAI", True, True, True),
    SCHEDULED: StateInfo(SCHEDULED, "scheduled", True, True, True),
    PUBLISHED: StateInfo(PUBLISHED, "published", False, True, True),
    FAILED: StateInfo(FAILED, "failed", True, False, False),
    REMOTE_MISSING: StateInfo(REMOTE_MISSING, "remote missing", True, False, False),
    ARCHIVED: StateInfo(ARCHIVED, "archived", False, False, False),
}


def _label(state: str) -> str:
    info = INFO.get(state)
    return info.label if info else state


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, set())


def transition(post_id: int, target: str, *, reason: str = "", conn=None) -> str:
    """Move a post to ``target``, or raise :class:`TransitionError`.

    ``conn`` lets a caller fold the state change into a transaction it already
    owns - the push pipeline does this so that "post created" and "state is now
    remote_draft" become durable together.
    """
    if target not in ALL_STATES:
        raise TransitionError(
            ERROR_UNKNOWN_STATE,
            f"Unknown state: {target}",
            target=target,
        )

    connection = conn or db.get_connection()
    row = connection.execute("SELECT state FROM posts WHERE id=?", (post_id,)).fetchone()
    if row is None:
        raise TransitionError(
            ERROR_POST_NOT_FOUND,
            f"Post {post_id} does not exist.",
            post_id=post_id,
        )

    current = row["state"]
    if current == target:
        return target
    if not can_transition(current, target):
        raise TransitionError(
            ERROR_NOT_ALLOWED,
            f"The step {_label(current)} -> {_label(target)} is not allowed. "
            f"{reason}".strip(),
            current=current,
            target=target,
        )

    if target == ARCHIVED:
        _refuse_unpublished_archive(connection, post_id, current)

    if conn is not None:
        conn.execute(
            "UPDATE posts SET state=?, updated_at=? WHERE id=?", (target, db.now_iso(), post_id)
        )
    else:
        with db.transaction() as tx:
            tx.execute(
                "UPDATE posts SET state=?, updated_at=? WHERE id=?",
                (target, db.now_iso(), post_id),
            )
    return target


def _refuse_unpublished_archive(connection, post_id: int, current: str) -> None:
    """`ARC-01`: nothing that was never published may be archived.

    The table already refuses `draft`, `ready` and `failed`. `remote_missing` is
    the one state that can mean either: the post was live and CivitAI lost it,
    or it was a remote draft that was deleted before it ever went out. Only the
    first belongs in the archive, and the publication date is what tells them
    apart.
    """
    if current != REMOTE_MISSING:
        return
    row = connection.execute(
        "SELECT remote_published_at FROM posts WHERE id=?", (post_id,)
    ).fetchone()
    # In the past, not merely present. A post scheduled on CivitAI carries a
    # *future* publish time, and deleting it there moves it to remote_missing
    # without clearing the date - which is precisely "a remote draft deleted
    # before it ever went out".
    if row is not None:
        from . import schedule

        published = schedule.parse_iso(row["remote_published_at"])
        if published is not None and published <= schedule.utcnow():
            return
    raise TransitionError(
        ERROR_ARCHIVE_UNPUBLISHED,
        "This post was never published, so there is nothing to archive. "
        "Delete it instead - it would then never have been a post at all.",
        current=current,
        target=ARCHIVED,
    )


def guard_binding_change(post: dict, new_model_version_id: int | None) -> None:
    """Refuse a binding change once CivitAI holds one.

    This is the rule with the nastiest failure mode: ``post.update`` accepts the
    call and ignores the field, so without this guard the app would report
    success and the post would stay bound to the wrong model.
    """
    bound = post.get("bound_model_version_id")
    if bound is None:
        return
    if new_model_version_id == bound:
        return
    raise TransitionError(
        ERROR_BINDING_LOCKED,
        "The model binding cannot be changed after the push - CivitAI "
        "accepts modelVersionId only at creation. That is what 'rebuild' is for: the "
        "post is deleted on CivitAI and recreated locally with the same images.",
        current=post.get("state") or "remote",
        target="model_binding",
    )


def guard_reschedule(post: dict) -> None:
    """Refuse a reschedule of something already public."""
    from . import schedule

    if post.get("state") == PUBLISHED:
        raise TransitionError(
            ERROR_RESCHEDULE_PUBLISHED,
            "A published post can no longer be rescheduled - CivitAI freezes "
            "publishedAt once the time is reached.",
            current=PUBLISHED,
            target=SCHEDULED,
        )
    published = schedule.parse_iso(post.get("remote_published_at"))
    if published is not None and published <= schedule.utcnow():
        raise TransitionError(
            ERROR_RESCHEDULE_PUBLISHED,
            "This post is already published; its publish time can no longer be changed.",
            current=post.get("state") or SCHEDULED,
            target=SCHEDULED,
        )
