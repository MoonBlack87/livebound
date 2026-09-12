"""Content-addressed memory of what has already been uploaded to CivitAI.

This is what makes an interrupted batch resumable without re-sending megabytes.
An upload returns a UUID that ``create_post`` later consumes; if the run dies in
between, the UUID is still good for a while and the file need not go up again.

"For a while" is the catch: an unconsumed UUID goes stale, and passing a stale
one to ``create_post`` makes the call fail. So freshness is checked rather than
assumed, and a stale entry simply causes a re-upload.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .. import config, db


def remember(
    cache_key: str,
    uuid: str,
    *,
    width: int | None,
    height: int | None,
    content_type: str | None,
    file_size: int | None,
    conn: Any = None,
) -> None:
    sql = (
        "INSERT OR REPLACE INTO upload_assets(sha256, uuid, width, height, content_type,"
        " file_size, uploaded_at) VALUES(?,?,?,?,?,?,?)"
    )
    # ``upload_assets.sha256`` predates metadata editing and is a plain TEXT
    # column. Edited variants use ``<original sha256>:<edit digest>`` here; the
    # library and usage history continue to keep the original image hash.
    params = (cache_key, uuid, width, height, content_type, file_size, db.now_iso())
    if conn is not None:
        conn.execute(sql, params)
    else:
        with db.transaction() as tx:
            tx.execute(sql, params)


def fresh_uuid(cache_key: str) -> dict[str, Any] | None:
    """A still-usable, unconsumed upload for these bytes, if there is one."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=config.UPLOAD_UUID_TTL_HOURS)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    row = db.get_connection().execute(
        "SELECT * FROM upload_assets WHERE sha256=? AND consumed_by_post_id IS NULL"
        " AND uploaded_at > ? ORDER BY uploaded_at DESC LIMIT 1",
        (cache_key, cutoff),
    ).fetchone()
    return dict(row) if row else None


def staged_for(uuid: str | None, cache_key: str) -> bool:
    """Is this post's own staged upload still the right bytes?

    Asked of ``upload_assets`` rather than of the post row, because the post row
    does not record *which* metadata variant was uploaded - only that something
    was. The asset's ``sha256`` column carries the cache key, so comparing it is
    what makes the skip edit-aware: a UUID staged before an edit changed no
    longer matches and is uploaded again.

    Deliberately blind to ``consumed_by_post_id``: after a successful create the
    UUID *is* consumed, and a run that failed at a later step must not re-send
    the bytes it already sent.
    """
    if not uuid:
        return False
    row = db.get_connection().execute(
        "SELECT sha256 FROM upload_assets WHERE uuid=? LIMIT 1", (uuid,)
    ).fetchone()
    return bool(row) and row["sha256"] == cache_key


def is_fresh(uploaded_at: str | None) -> bool:
    if not uploaded_at:
        return False
    try:
        when = datetime.fromisoformat(uploaded_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - when.astimezone(timezone.utc)
    return age < timedelta(hours=config.UPLOAD_UUID_TTL_HOURS)


def mark_consumed(uuids: list[str], post_id: int, *, conn: Any = None) -> None:
    if not uuids:
        return
    placeholders = ",".join("?" * len(uuids))
    sql = f"UPDATE upload_assets SET consumed_by_post_id=? WHERE uuid IN ({placeholders})"
    params = (post_id, *uuids)
    if conn is not None:
        conn.execute(sql, params)
    else:
        with db.transaction() as tx:
            tx.execute(sql, params)


def prune(days: int = 7) -> int:
    """Drop consumed or long-stale rows; they can never be reused."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    with db.transaction() as conn:
        return conn.execute(
            "DELETE FROM upload_assets WHERE uploaded_at < ?", (cutoff,)
        ).rowcount
