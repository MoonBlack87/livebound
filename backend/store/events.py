"""Append-only audit trail.

Also the source of truth for the rate-limit budget: a post deleted locally still
counted against CivitAI's daily ceiling, and this row survives that deletion,
which ``COUNT(posts)`` would not.
"""

from __future__ import annotations

import json
from typing import Any

from .. import db


def record(
    event: str,
    *,
    post_id: int | None = None,
    remote_post_id: int | None = None,
    detail: dict[str, Any] | None = None,
    conn: Any = None,
) -> None:
    sql = (
        "INSERT INTO post_events(post_id, remote_post_id, event, at, detail_json)"
        " VALUES(?,?,?,?,?)"
    )
    params = (
        post_id,
        remote_post_id,
        event,
        db.now_iso(),
        json.dumps(detail, ensure_ascii=False) if detail else None,
    )
    if conn is not None:
        conn.execute(sql, params)
    else:
        with db.transaction() as tx:
            tx.execute(sql, params)


def for_post(post_id: int, limit: int = 100) -> list[dict[str, Any]]:
    rows = db.get_connection().execute(
        "SELECT * FROM post_events WHERE post_id=? ORDER BY id DESC LIMIT ?", (post_id, limit)
    )
    return [_row(row) for row in rows]


def _row(row: Any) -> dict[str, Any]:
    value = dict(row)
    raw = value.pop("detail_json", None)
    try:
        value["detail"] = json.loads(raw) if raw else None
    except ValueError:
        value["detail"] = None
    return value
