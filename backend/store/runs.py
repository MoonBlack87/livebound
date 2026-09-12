"""Push runs and their per-post resume cursor.

The cursor exists because ``create_post`` has no idempotency key. If the response
to it is lost, a blind retry creates a *second* post. So the attempt is recorded
before the call, and a resume that finds an attempt without an id must go looking
for the post rather than making another one.
"""

from __future__ import annotations

import json
from typing import Any

from .. import db

# push_items.step - ordered, and each one is safe to re-enter
QUEUED = "queued"
PREFLIGHT_OK = "preflight_ok"
CREATE_ATTEMPTED = "create_attempted"
CREATED = "created"
TAGGED = "tagged"
UPLOADING = "uploading"
UPLOADED = "uploaded"
ATTACHING = "attaching"
ATTACHED = "attached"
PUBLISH_APPLIED = "publish_applied"
VERIFIED = "verified"
DONE = "done"

STEP_ORDER = [
    QUEUED,
    PREFLIGHT_OK,
    CREATE_ATTEMPTED,
    CREATED,
    TAGGED,
    UPLOADING,
    UPLOADED,
    ATTACHING,
    ATTACHED,
    PUBLISH_APPLIED,
    VERIFIED,
    DONE,
]

STEP_LABEL = {
    QUEUED: "Queued",
    PREFLIGHT_OK: "Preflight passed",
    CREATE_ATTEMPTED: "Creating the post",
    CREATED: "Post created",
    TAGGED: "Tags set",
    UPLOADING: "Uploading images",
    UPLOADED: "Images uploaded",
    ATTACHING: "Attaching images",
    ATTACHED: "Images attached",
    PUBLISH_APPLIED: "Publish action complete",
    VERIFIED: "Confirmed",
    DONE: "Done",
}


def create_run(kind: str, post_ids: list[int], options: dict[str, Any] | None = None) -> int:
    now = db.now_iso()
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO push_runs(kind, status, options_json, total, created_at, started_at)"
            " VALUES(?,?,?,?,?,?)",
            (
                kind,
                "running",
                json.dumps(options or {}, ensure_ascii=False),
                len(post_ids),
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for post_id in post_ids:
            conn.execute(
                "INSERT OR IGNORE INTO push_items(run_id, post_id, status, step, updated_at)"
                " VALUES(?,?,?,?,?)",
                (run_id, post_id, "pending", QUEUED, now),
            )
        # claim the posts so a second run cannot pick them up
        placeholders = ",".join("?" * len(post_ids)) if post_ids else "NULL"
        if post_ids:
            conn.execute(
                f"UPDATE posts SET push_run_id=? WHERE id IN ({placeholders})",
                (run_id, *post_ids),
            )
    return run_id


def get_run(run_id: int) -> dict[str, Any] | None:
    row = db.get_connection().execute(
        "SELECT * FROM push_runs WHERE id=?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    value = dict(row)
    raw = value.pop("options_json", None)
    try:
        value["options"] = json.loads(raw) if raw else {}
    except ValueError:
        value["options"] = {}
    value["items"] = items(run_id)
    return value


def list_runs(limit: int = 20, since: str | None = None) -> list[dict[str, Any]]:
    """The newest runs, optionally only those since a timestamp.

    The table is never pruned - it is small, and a resume needs it - so the log
    is narrowed on the way out instead.
    """
    sql = "SELECT * FROM push_runs"
    params: list[Any] = []
    if since:
        sql += " WHERE created_at >= ?"
        params.append(since)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in db.get_connection().execute(sql, params)]


def items(run_id: int) -> list[dict[str, Any]]:
    rows = db.get_connection().execute(
        """
        SELECT pi.*, p.title, p.state AS post_state, p.remote_post_id AS post_remote_id
        FROM push_items pi JOIN posts p ON p.id = pi.post_id
        WHERE pi.run_id=? ORDER BY pi.id
        """,
        (run_id,),
    )
    result = []
    for row in rows:
        value = dict(row)
        value["step_label"] = STEP_LABEL.get(value.get("step", ""), value.get("step", ""))
        value.pop("create_request_json", None)
        result.append(value)
    return result


def get_item(run_id: int, post_id: int) -> dict[str, Any] | None:
    row = db.get_connection().execute(
        "SELECT * FROM push_items WHERE run_id=? AND post_id=?", (run_id, post_id)
    ).fetchone()
    return dict(row) if row else None


def unresolved_attempt(post_id: int, *, before_run_id: int | None = None) -> dict[str, Any] | None:
    """An earlier attempt at this post whose ``create_post`` answer never arrived.

    The row that says "a post may exist on CivitAI that we do not know the id
    of". Looked up across runs on purpose: a retry starts a *new* run with a
    fresh, empty cursor, so without this the one safeguard against creating a
    second post is simply not consulted. That is not theoretical - it is what
    happens on every press of Push after a lost response.
    """
    sql = (
        "SELECT * FROM push_items WHERE post_id=? AND create_attempted_at IS NOT NULL"
        " AND remote_post_id IS NULL"
    )
    params: list[Any] = [post_id]
    if before_run_id is not None:
        sql += " AND run_id != ?"
        params.append(before_run_id)
    sql += " ORDER BY id DESC LIMIT 1"
    row = db.get_connection().execute(sql, params).fetchone()
    return dict(row) if row else None


#: Column names are interpolated into SQL below, so the writable set is closed.
_ITEM_COLUMNS = {
    "status",
    "step",
    "images_uploaded",
    "create_attempted_at",
    "create_request_json",
    "reconcile_tag",
    "remote_post_id",
    "attempts",
    "error",
}


def set_item(run_id: int, post_id: int, *, conn: Any = None, **fields: Any) -> None:
    fields = {key: value for key, value in fields.items() if key in _ITEM_COLUMNS}
    if not fields:
        return
    sets = [f"{key}=?" for key in fields]
    params = [*fields.values(), db.now_iso(), run_id, post_id]
    sql = f"UPDATE push_items SET {', '.join(sets)}, updated_at=? WHERE run_id=? AND post_id=?"
    if conn is not None:
        conn.execute(sql, params)
    else:
        with db.transaction() as tx:
            tx.execute(sql, params)


def finish_run(run_id: int, status: str, error: str | None = None) -> None:
    with db.transaction() as conn:
        counts = conn.execute(
            "SELECT SUM(status='done') AS ok, SUM(status='error') AS bad,"
            " SUM(status='skipped') AS skipped FROM push_items WHERE run_id=?",
            (run_id,),
        ).fetchone()
        conn.execute(
            "UPDATE push_runs SET status=?, error=?, finished_at=?, succeeded=?, failed=?,"
            " skipped=? WHERE id=?",
            (
                status,
                error,
                db.now_iso(),
                int(counts["ok"] or 0),
                int(counts["bad"] or 0),
                int(counts["skipped"] or 0),
                run_id,
            ),
        )
        conn.execute("UPDATE posts SET push_run_id=NULL WHERE push_run_id=?", (run_id,))


def recover_interrupted() -> dict[str, int]:
    """Called at startup: nothing can still be running after a restart.

    Three outcomes, and the distinction between them is the whole point:

    * ``create_post`` was attempted and no id came back - the post may exist on
      CivitAI, so it goes to ``needs_reconcile`` and the next run looks for it
      instead of creating a second one;
    * the id *did* come back - the post exists there as a draft, so saying
      "failed" would hide a real post;
    * nothing reached CivitAI - ``failed``, and resumable.

    The state changes go through :func:`lifecycle.transition` like every other
    one, inside the same transaction, so a refused edge is noticed here rather
    than written past.
    """
    from ..posts import lifecycle, schedule

    counts = {
        "runs": 0,
        "needs_reconcile": 0,
        "remote_draft": 0,
        "scheduled": 0,
        "published": 0,
        "failed": 0,
    }
    with db.transaction() as conn:
        open_runs = conn.execute("SELECT id FROM push_runs WHERE status='running'")
        run_ids = [int(row["id"]) for row in open_runs]
        counts["runs"] = conn.execute(
            "UPDATE push_runs SET status='interrupted', finished_at=? WHERE status='running'",
            (db.now_iso(),),
        ).rowcount

        # Scoped to the runs that were interrupted just now: an unresolved
        # attempt from some run that finished long ago says nothing about this
        # post's current push.
        attempted: set[int] = set()
        if run_ids:
            placeholders = ",".join("?" * len(run_ids))
            attempted = {
                int(row["post_id"])
                for row in conn.execute(
                    f"SELECT post_id FROM push_items WHERE run_id IN ({placeholders})"
                    " AND create_attempted_at IS NOT NULL AND remote_post_id IS NULL",
                    run_ids,
                )
            }

        stranded = conn.execute(
            "SELECT p.id, p.remote_post_id, p.remote_published_at,"
            " COUNT(pi.id) AS image_count,"
            " SUM(CASE WHEN pi.remote_image_id IS NOT NULL THEN 1 ELSE 0 END) AS attached_count"
            " FROM posts p LEFT JOIN post_images pi ON pi.post_id=p.id"
            " WHERE p.state='pushing' GROUP BY p.id"
        ).fetchall()
        for row in stranded:
            post_id = int(row["id"])
            if row["remote_post_id"]:
                published_at = schedule.parse_iso(row["remote_published_at"])
                if published_at is None:
                    target, key = lifecycle.REMOTE_DRAFT, "remote_draft"
                elif published_at > schedule.utcnow():
                    target, key = lifecycle.SCHEDULED, "scheduled"
                else:
                    target, key = lifecycle.PUBLISHED, "published"
                note = "The push was interrupted after the post had been created on CivitAI."
                missing = int(row["image_count"] or 0) - int(row["attached_count"] or 0)
                note += f" {missing} of {int(row['image_count'] or 0)} images are missing."
                if row["remote_published_at"]:
                    note += (
                        f" The original publish time ({row['remote_published_at']}) still "
                        "stands; reschedule it, publish with the attached images, or discard "
                        "the post."
                    )
                else:
                    note += " The post is still a draft and cannot publish by itself."
            elif post_id in attempted:
                target, note = lifecycle.NEEDS_RECONCILE, (
                    "The push was interrupted before the post was confirmed."
                )
                key = "needs_reconcile"
            else:
                target, note = lifecycle.FAILED, "The push was interrupted."
                key = "failed"
            lifecycle.transition(post_id, target, conn=conn)
            conn.execute(
                "UPDATE posts SET push_run_id=NULL, last_error=?, updated_at=? WHERE id=?",
                (note, db.now_iso(), post_id),
            )
            counts[key] += 1

        conn.execute(
            "UPDATE push_items SET status='error', error='interrupted', updated_at=?"
            " WHERE status='running'",
            (db.now_iso(),),
        )
    return counts
