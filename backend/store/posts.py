"""Post persistence: the post itself, its ordered images and its tags."""

from __future__ import annotations

import json
from typing import Any

from .. import config, db
from . import usage

_EDITABLE = {
    "title",
    "detail",
    "model_version_id",
    "model_name",
    "version_name",
    "collection_id",
    "collection_tag_id",
    "publish_mode",
    "schedule_mode",
    "schedule_offset_minutes",
    "scheduled_at",
    "notes",
}


class PostImageLimitExceeded(ValueError):
    """A local image selection exceeds the post size this app permits."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.limit = config.CIVITAI_POST_IMAGE_LIMIT
        self.excess = count - self.limit
        super().__init__(
            f"A post can have at most {self.limit} images; {self.count} were selected. "
            f"Remove {self.excess} image(s)."
        )


def check_image_limit(image_ids: list[int]) -> None:
    """Refuse a local selection that would exceed the deliberate post limit."""
    if len(image_ids) > config.CIVITAI_POST_IMAGE_LIMIT:
        raise PostImageLimitExceeded(len(image_ids))


def create(**fields: Any) -> int:
    now = db.now_iso()
    values = {key: fields.get(key) for key in _EDITABLE}
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO posts(title, detail, state, origin, model_version_id, model_name,
                version_name, collection_id, collection_tag_id, publish_mode, schedule_mode,
                schedule_offset_minutes, scheduled_at, notes, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                values.get("title") or "",
                values.get("detail") or "",
                fields.get("state", "draft"),
                fields.get("origin", "local"),
                values.get("model_version_id"),
                values.get("model_name"),
                values.get("version_name"),
                values.get("collection_id"),
                values.get("collection_tag_id"),
                values.get("publish_mode") or "schedule",
                values.get("schedule_mode") or "relative",
                values.get("schedule_offset_minutes"),
                values.get("scheduled_at"),
                values.get("notes") or "",
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


def update(post_id: int, **fields: Any) -> None:
    """Patch editable columns. Marks the post dirty so a re-push knows to run."""
    sets, params = [], []
    for key, value in fields.items():
        if key not in _EDITABLE:
            continue
        sets.append(f"{key}=?")
        params.append(value)
    if not sets:
        return
    sets.extend(["updated_at=?", "dirty=1"])
    params.extend([db.now_iso(), post_id])
    with db.transaction() as conn:
        conn.execute(f"UPDATE posts SET {', '.join(sets)} WHERE id=?", params)


#: Columns :func:`set_fields` may write. The function interpolates column names
#: into SQL, so the set is closed rather than trusting its callers - a typo would
#: be a silent no-op at best, and anything reaching it from a request body would
#: be an injection point.
_INTERNAL = {
    "state",
    "origin",
    "bound_model_version_id",
    "remote_post_id",
    "remote_published_at",
    "remote_state",
    "remote_url",
    "remote_synced_at",
    "remote_snapshot_json",
    "remote_diverged",
    "pushed_fields_json",
    "dirty",
    "push_run_id",
    "last_error",
    "scheduled_at",
    "schedule_mode",
    "schedule_offset_minutes",
}


def set_fields(post_id: int, *, conn: Any = None, **fields: Any) -> None:
    """Set a bookkeeping column directly (remote ids, sync results, error state).

    Separate from :func:`update` on purpose: this must not set ``dirty``, or a
    successful sync would immediately mark the post as needing another push.
    """
    fields = {key: value for key, value in fields.items() if key in _INTERNAL}
    if not fields:
        return
    sets = [f"{key}=?" for key in fields]
    params = [*fields.values(), db.now_iso(), post_id]
    sql = f"UPDATE posts SET {', '.join(sets)}, updated_at=? WHERE id=?"
    if conn is not None:
        conn.execute(sql, params)
    else:
        with db.transaction() as tx:
            tx.execute(sql, params)


def get(post_id: int) -> dict[str, Any] | None:
    row = db.get_connection().execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    return _row(row) if row else None


def get_by_remote(remote_post_id: int) -> dict[str, Any] | None:
    row = db.get_connection().execute(
        "SELECT * FROM posts WHERE remote_post_id=?", (remote_post_id,)
    ).fetchone()
    return _row(row) if row else None


def list_posts(
    *, states: list[str] | None = None, limit: int = 500, offset: int = 0
) -> list[dict[str, Any]]:
    # The count drives the board's "find local files" control: a post with no
    # unmatched image has nothing to search for.
    sql = (
        "SELECT posts.*, ("
        "  SELECT COUNT(*) FROM post_images"
        "  WHERE post_images.post_id = posts.id"
        "    AND post_images.image_id IS NULL"
        "    AND post_images.remote_image_id IS NOT NULL"
        ") AS unmatched_images FROM posts"
    )
    params: list[Any] = []
    if states:
        sql += f" WHERE state IN ({','.join('?' * len(states))})"
        params.extend(states)
    sql += " ORDER BY COALESCE(scheduled_at, '9999'), id LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return [_row(row) for row in db.get_connection().execute(sql, params)]


def list_calendar_candidates(
    *, start: str | None = None, end: str | None = None
) -> list[dict[str, Any]]:
    """Posts whose effective publish time could fall in the calendar window.

    Relative plans deliberately remain candidates: their wall-clock time is
    resolved from the current clock by ``schedule.effective_publish_at``, not
    stored in the database.  That function also gives a confirmed remote time
    precedence over any local plan, so this query only narrows the rows it must
    inspect; it does not decide their effective time.
    """
    sql = "SELECT * FROM posts WHERE state != 'archived'"
    params: list[Any] = []
    if start or end:
        remote_window, absolute_window = [], []
        remote_params: list[str] = []
        absolute_params: list[str] = []
        # Compared by date, not by instant. The exact filter runs in the caller
        # on a normalised timestamp; this one runs on the stored string, and
        # CivitAI sends `publishedAt` both with and without milliseconds, so
        # `2026-08-26T12:00:00Z` sorts *after* `2026-08-26T12:00:00.000Z` and a
        # post on the window's last second would be dropped before anyone could
        # look at it. A date prefix is always a superset of the window, which is
        # all a prefilter may be.
        if start:
            remote_window.append("substr(remote_published_at, 1, 10) >= substr(?, 1, 10)")
            absolute_window.append("substr(scheduled_at, 1, 10) >= substr(?, 1, 10)")
            remote_params.append(start)
            absolute_params.append(start)
        if end:
            remote_window.append("substr(remote_published_at, 1, 10) <= substr(?, 1, 10)")
            absolute_window.append("substr(scheduled_at, 1, 10) <= substr(?, 1, 10)")
            remote_params.append(end)
            absolute_params.append(end)
        sql += (
            " AND ((remote_published_at IS NOT NULL AND "
            + " AND ".join(remote_window)
            + ") OR (remote_published_at IS NULL AND schedule_mode = 'absolute' AND "
            + " AND ".join(absolute_window)
            + ") OR (remote_published_at IS NULL AND COALESCE(schedule_mode, 'relative') = "
            "'relative'))"
        )
        params.extend(remote_params)
        params.extend(absolute_params)
    return [_row(row) for row in db.get_connection().execute(sql, params)]


_ARCHIVE_ROWS_CTE = """
WITH first_published AS (
    SELECT remote_post_id, at
    FROM (
        SELECT remote_post_id, at,
               ROW_NUMBER() OVER (
                   PARTITION BY remote_post_id ORDER BY at, id
               ) AS occurrence
        FROM post_events
        WHERE event='published' AND remote_post_id IS NOT NULL
    )
    WHERE occurrence=1
),
archive_rows AS (
    SELECT p.*,
           CASE
               WHEN julianday(p.remote_published_at) IS NOT NULL
                   THEN p.remote_published_at
               WHEN julianday(first_published.at) IS NOT NULL
                   THEN first_published.at
               ELSE NULL
           END AS archive_published_at,
           CASE
               WHEN julianday(p.remote_published_at) IS NOT NULL
                   THEN date(p.remote_published_at)
               WHEN julianday(first_published.at) IS NOT NULL
                   THEN date(first_published.at)
               ELSE NULL
           END AS archive_date
    FROM posts p
    LEFT JOIN first_published ON first_published.remote_post_id=p.remote_post_id
    WHERE p.state='archived'
)
"""


def list_archived_posts(
    *,
    limit: int = 50,
    offset: int = 0,
    from_date: str | None = None,
    to_date: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Chronological archive rows and their identically filtered total.

    The first durable published event deliberately uses the same ``at, id``
    precedence as :func:`backend.archive.publication_date`. Date filtering and
    ordering happen inside this query, before pagination.
    """
    filters: list[str] = []
    params: list[Any] = []
    if from_date is not None:
        filters.append("archive_date >= ?")
        params.append(from_date)
    if to_date is not None:
        filters.append("archive_date <= ?")
        params.append(to_date)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""

    conn = db.get_connection()
    total = int(
        conn.execute(
            _ARCHIVE_ROWS_CTE + "SELECT COUNT(*) AS n FROM archive_rows" + where,
            params,
        ).fetchone()["n"]
    )
    rows = conn.execute(
        _ARCHIVE_ROWS_CTE
        + "SELECT * FROM archive_rows"
        + where
        + """
          ORDER BY archive_date IS NULL,
                   archive_date DESC,
                   julianday(archive_published_at) DESC,
                   CASE WHEN archive_date IS NOT NULL THEN remote_post_id END DESC,
                   id DESC
          LIMIT ? OFFSET ?
          """,
        [*params, limit, offset],
    )
    return [_row(row) for row in rows], total


def delete(post_id: int) -> None:
    """Delete locally. ``image_usage`` rows survive - that is the whole point."""
    with db.transaction() as conn:
        conn.execute("DELETE FROM posts WHERE id=?", (post_id,))


# --- images -----------------------------------------------------------------


def posts_holding_images(image_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Which posts already hold each of these images.

    Answers the question the library asks before building another post out of a
    selection: an image can be in two posts, nothing breaks, and it is almost
    never what somebody meant. The post's own state comes along because it
    changes the answer - a draft is often one being abandoned, while a published
    post means the picture has already been shown.
    """
    holders: dict[int, list[dict[str, Any]]] = {}
    if not image_ids:
        return holders
    conn = db.get_connection()
    unique = sorted({int(image_id) for image_id in image_ids})
    # SQLite caps the parameters of one statement, and a selection can be a
    # whole page of the library.
    for start in range(0, len(unique), 500):
        chunk = unique[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"""
            SELECT post_images.image_id, posts.id AS post_id, posts.title, posts.state
            FROM post_images
            JOIN posts ON posts.id = post_images.post_id
            WHERE post_images.image_id IN ({placeholders})
            ORDER BY posts.id
            """,
            chunk,
        ):
            holders.setdefault(int(row["image_id"]), []).append(
                {
                    "post_id": int(row["post_id"]),
                    "title": row["title"] or "",
                    "state": row["state"],
                }
            )
    return holders


def images(post_id: int) -> list[dict[str, Any]]:
    rows = db.get_connection().execute(
        """
        SELECT pi.*, i.thumbnail_path, i.relative_path, i.folder, i.is_missing,
               i.pixel_sha256, i.parsed_json, i.raw_infotext
        FROM post_images pi LEFT JOIN images i ON i.id = pi.image_id
        WHERE pi.post_id=? ORDER BY pi.position
        """,
        (post_id,),
    )
    result = []
    for row in rows:
        value = dict(row)
        raw = value.pop("parsed_json", None)
        try:
            value["parsed"] = json.loads(raw) if raw else None
        except ValueError:
            value["parsed"] = None
        value["hide_meta"] = bool(value.get("hide_meta"))
        if value.get("remote_on_site") is not None:
            value["remote_on_site"] = bool(value["remote_on_site"])
        value["dedup_ack"] = bool(value.get("dedup_ack"))
        # The bits live in one place; the frontend gets the word CivitAI uses.
        value["nsfw_label"] = config.NSFW_LEVEL_LABELS.get(value.get("nsfw_level") or 0)
        result.append(value)
    from . import edits as edit_store

    edited = edit_store.edited_ids(
        [int(value["image_id"]) for value in result if value.get("image_id") is not None]
    )
    for value in result:
        value["edited"] = value.get("image_id") in edited
    return result


def set_images(post_id: int, image_ids: list[int]) -> None:
    """Replace the library-image set, keeping unmatched tiles and known values.

    Upload UUIDs and remote image ids are preserved for images that stay in the
    post, so adding one more picture never re-uploads the rest. Rows without a
    library image keep their place among the requested local images.
    """
    from . import images as image_store

    current = images(post_id)
    unmatched_ids = [int(row["id"]) for row in current if row.get("image_id") is None]
    check_image_limit([*image_ids, *unmatched_ids])
    existing = {row["image_id"]: row for row in current if row.get("image_id")}
    records = image_store.get_many(image_ids)
    found = {record["id"]: record for record in records}
    requested = iter(image_ids)
    ordered: list[int | dict[str, Any]] = []
    for row in current:
        if row.get("image_id") is None:
            ordered.append(row)
            continue
        image_id = next(requested, None)
        if image_id is not None:
            ordered.append(image_id)
    ordered.extend(requested)

    with db.transaction() as conn:
        conn.execute("DELETE FROM post_images WHERE post_id=?", (post_id,))
        for position, item in enumerate(ordered):
            if isinstance(item, dict):
                image_id = None
                record = item
                previous = item
            else:
                image_id = item
                record = found.get(image_id)
                if record is None:
                    continue
                previous = existing.get(image_id, {})
            conn.execute(
                """
                INSERT INTO post_images(post_id, image_id, position, source_path, sha256, phash,
                    file_size, width, height, content_type, media_type, remote_uuid,
                    uploaded_at, blurhash, remote_image_id, remote_url, hide_meta, remote_on_site,
                    nsfw_level, dedup_ack)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    post_id,
                    image_id,
                    position,
                    record.get("absolute_path") or record["source_path"],
                    record.get("sha256") or "",
                    record.get("phash"),
                    record.get("file_size"),
                    record.get("width"),
                    record.get("height"),
                    record.get("content_type"),
                    # Not derived from the image row: it is learned from CivitAI
                    # (posts/sync.py) and says "this app does not touch this
                    # one". Losing it here made the next fetch try to download a
                    # video the app deliberately leaves alone.
                    previous.get("media_type") or "image",
                    previous.get("remote_uuid"),
                    previous.get("uploaded_at"),
                    previous.get("blurhash"),
                    previous.get("remote_image_id"),
                    previous.get("remote_url"),
                    1 if previous.get("hide_meta") else 0,
                    previous.get("remote_on_site"),
                    # Learned from CivitAI like remote_on_site above. A reorder
                    # must not throw the rating away and make it look unrated.
                    previous.get("nsfw_level"),
                    1 if previous.get("dedup_ack") else 0,
                ),
            )
        conn.execute(
            "UPDATE posts SET updated_at=?, dirty=1 WHERE id=?", (db.now_iso(), post_id)
        )


def set_image_order(post_id: int, post_image_ids: list[int]) -> None:
    """Replace a post's tile sequence by ``post_images.id``."""
    check_image_limit(post_image_ids)

    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT id FROM post_images WHERE post_id=?", (post_id,)
        ).fetchall()
        existing = {int(row["id"]) for row in rows}

        # Positions are unique per post, so move every tile out of the positive
        # range before assigning its final position.
        conn.execute(
            "UPDATE post_images SET position=-position-1 WHERE post_id=?", (post_id,)
        )
        for post_image_id in existing.difference(post_image_ids):
            conn.execute("DELETE FROM post_images WHERE id=?", (post_image_id,))
        for position, post_image_id in enumerate(post_image_ids):
            if post_image_id in existing:
                conn.execute(
                    "UPDATE post_images SET position=? WHERE id=? AND post_id=?",
                    (position, post_image_id, post_id),
                )
        conn.execute(
            "UPDATE posts SET updated_at=?, dirty=1 WHERE id=?", (db.now_iso(), post_id)
        )


def set_image_flag(post_image_id: int, field: str, value: bool) -> None:
    if field not in ("hide_meta", "dedup_ack"):
        raise ValueError(f"Unknown flag: {field}")
    with db.transaction() as conn:
        conn.execute(
            f"UPDATE post_images SET {field}=? WHERE id=?", (1 if value else 0, post_image_id)
        )


def record_upload(
    post_image_id: int,
    *,
    uuid: str,
    width: int | None,
    height: int | None,
    file_size: int | None = None,
    blurhash: str | None = None,
    conn: Any = None,
) -> None:
    sql = (
        "UPDATE post_images SET remote_uuid=?, uploaded_at=?,"
        " width=COALESCE(?, width), height=COALESCE(?, height),"
        " file_size=COALESCE(?, file_size), blurhash=COALESCE(?, blurhash) WHERE id=?"
    )
    params = (uuid, db.now_iso(), width, height, file_size, blurhash, post_image_id)
    if conn is not None:
        conn.execute(sql, params)
    else:
        with db.transaction() as tx:
            tx.execute(sql, params)


def record_attachment(
    post_image_id: int,
    *,
    remote_image_id: int,
    remote_url: str | None,
    conn: Any = None,
) -> None:
    sql = "UPDATE post_images SET remote_image_id=?, remote_url=? WHERE id=?"
    params = (remote_image_id, remote_url, post_image_id)
    if conn is not None:
        conn.execute(sql, params)
    else:
        with db.transaction() as tx:
            tx.execute(sql, params)


def clear_uploads(post_id: int) -> None:
    """Forget staged uploads, e.g. after a rebuild. Forces a fresh upload next time."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE post_images SET remote_uuid=NULL, uploaded_at=NULL, remote_image_id=NULL,"
            " remote_url=NULL WHERE post_id=?",
            (post_id,),
        )


# --- tags -------------------------------------------------------------------


def tags(post_id: int, *, include_transient: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM post_tags WHERE post_id=?"
    if not include_transient:
        sql += " AND is_transient=0"
    sql += " ORDER BY position, id"
    return [dict(row) for row in db.get_connection().execute(sql, (post_id,))]


def set_tags(post_id: int, names: list[str]) -> None:
    """Replace the user-visible tags, keeping remote ids for tags that stay.

    Losing a ``remote_tag_id`` would mean the tag can never be removed again -
    ``post.removeTag`` takes the numeric id, not the name.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for name in names:
        value = (name or "").strip().lower()
        if value and value not in seen:
            seen.add(value)
            cleaned.append(value)

    known = {row["name"]: row for row in tags(post_id, include_transient=True)}
    with db.transaction() as conn:
        conn.execute("DELETE FROM post_tags WHERE post_id=? AND is_transient=0", (post_id,))
        for position, name in enumerate(cleaned):
            previous = known.get(name, {})
            conn.execute(
                "INSERT OR REPLACE INTO post_tags(post_id, name, position, remote_tag_id,"
                " is_transient, pushed_at) VALUES(?,?,?,?,0,?)",
                (post_id, name, position, previous.get("remote_tag_id"), previous.get("pushed_at")),
            )
        conn.execute(
            "UPDATE posts SET updated_at=?, dirty=1 WHERE id=?", (db.now_iso(), post_id)
        )


def add_transient_tag(post_id: int, name: str, *, conn: Any = None) -> None:
    """Store the reconcile marker so it can be found and removed again after a crash."""
    sql = (
        "INSERT OR REPLACE INTO post_tags(post_id, name, position, is_transient)"
        " VALUES(?,?,999,1)"
    )
    if conn is not None:
        conn.execute(sql, (post_id, name))
    else:
        with db.transaction() as tx:
            tx.execute(sql, (post_id, name))


def set_tag_remote_id(post_id: int, name: str, tag_id: int | None, *, conn: Any = None) -> None:
    sql = "UPDATE post_tags SET remote_tag_id=?, pushed_at=? WHERE post_id=? AND name=?"
    params = (tag_id, db.now_iso(), post_id, name)
    if conn is not None:
        conn.execute(sql, params)
    else:
        with db.transaction() as tx:
            tx.execute(sql, params)


def drop_tag(post_id: int, name: str, *, conn: Any = None) -> None:
    sql = "DELETE FROM post_tags WHERE post_id=? AND name=?"
    if conn is not None:
        conn.execute(sql, (post_id, name))
    else:
        with db.transaction() as tx:
            tx.execute(sql, (post_id, name))


# --- composed views ----------------------------------------------------------


def detail(post_id: int) -> dict[str, Any] | None:
    post = get(post_id)
    if post is None:
        return None
    post["images"] = images(post_id)
    post["tags"] = [row["name"] for row in tags(post_id)]
    for image in post["images"]:
        image["duplicates"] = usage.check(
            image.get("sha256"),
            image.get("phash"),
            pixel_sha256=image.get("pixel_sha256"),
            exclude_post_id=post_id,
        )
    return post


def _row(row: Any) -> dict[str, Any]:
    value = dict(row)
    for key in ("pushed_fields_json", "remote_snapshot_json"):
        raw = value.pop(key, None)
        try:
            value[key.replace("_json", "")] = json.loads(raw) if raw else None
        except ValueError:
            value[key.replace("_json", "")] = None
    value["dirty"] = bool(value.get("dirty"))
    value["remote_diverged"] = bool(value.get("remote_diverged"))
    return value
