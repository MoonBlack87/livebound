"""Scanned image rows and their parsed metadata."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .. import db
from ..metadata.resources import hash_prefix

#: The 64-bit dHash split into eight 8-bit bands, mirroring ``image_usage``.
#: Two hashes within Hamming distance <= 7 must agree on at least one band, so
#: the candidate lookup is eight indexed probes instead of a pass over every
#: pair of rows in the library.
_BANDS = 8


def phash_bands(phash: str | None) -> list[int | None]:
    if not phash:
        return [None] * _BANDS
    try:
        value = int(phash, 16)
    except ValueError:
        return [None] * _BANDS
    return [(value >> (index * 8)) & 0xFF for index in range(_BANDS)]


def upsert(root_id: int, record: dict[str, Any]) -> int:
    """Insert or refresh one scanned file, returning its row id.

    ``first_seen_at`` is preserved across rescans so the library can be sorted by
    when a picture entered the collection rather than when it was last touched.
    """
    now = db.now_iso()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO images(
                source_root_id, absolute_path, relative_path, folder, file_mtime, file_size,
                content_type, width, height, sha256, pixel_sha256, phash, raw_infotext, parsed_json,
                ingest_revision, thumbnail_path, is_missing, first_seen_at, last_seen_at,
                phash_b0, phash_b1, phash_b2, phash_b3,
                phash_b4, phash_b5, phash_b6, phash_b7)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(absolute_path) DO UPDATE SET
                source_root_id=excluded.source_root_id,
                relative_path=excluded.relative_path,
                folder=excluded.folder,
                file_mtime=excluded.file_mtime,
                file_size=excluded.file_size,
                content_type=excluded.content_type,
                width=excluded.width,
                height=excluded.height,
                sha256=excluded.sha256,
                pixel_sha256=excluded.pixel_sha256,
                phash=excluded.phash,
                raw_infotext=excluded.raw_infotext,
                parsed_json=excluded.parsed_json,
                ingest_revision=excluded.ingest_revision,
                thumbnail_path=excluded.thumbnail_path,
                is_missing=0,
                last_seen_at=excluded.last_seen_at,
                phash_b0=excluded.phash_b0,
                phash_b1=excluded.phash_b1,
                phash_b2=excluded.phash_b2,
                phash_b3=excluded.phash_b3,
                phash_b4=excluded.phash_b4,
                phash_b5=excluded.phash_b5,
                phash_b6=excluded.phash_b6,
                phash_b7=excluded.phash_b7
            """,
            (
                root_id,
                record["absolute_path"],
                record.get("relative_path", ""),
                record.get("folder", ""),
                record.get("file_mtime"),
                record.get("file_size"),
                record.get("content_type"),
                record.get("width"),
                record.get("height"),
                record.get("sha256"),
                record.get("pixel_sha256"),
                record.get("phash"),
                record.get("raw_infotext"),
                json.dumps(record["parsed"], ensure_ascii=False) if record.get("parsed") else None,
                record.get("ingest_revision", 0),
                record.get("thumbnail_path"),
                now,
                now,
                *phash_bands(record.get("phash")),
            ),
        )
        row = conn.execute(
            "SELECT id FROM images WHERE absolute_path=?", (record["absolute_path"],)
        ).fetchone()
    return int(row["id"])


def mark_seen_many(rows: list[tuple[int, str, str, str]]) -> None:
    """Refresh unchanged scan rows in one transaction."""
    if not rows:
        return
    seen_at = db.now_iso()
    with db.transaction() as conn:
        conn.executemany(
            "UPDATE images SET absolute_path=?, relative_path=?, folder=?,"
            " last_seen_at=? WHERE id=?",
            [
                (absolute_path, relative_path, folder, seen_at, image_id)
                for image_id, absolute_path, relative_path, folder in rows
            ],
        )


def content_hashes() -> list[str]:
    """Content currently represented by image rows, for cache retention."""
    rows = db.get_connection().execute(
        "SELECT DISTINCT sha256 FROM images WHERE sha256 IS NOT NULL"
    ).fetchall()
    return [str(row["sha256"]) for row in rows]


def thumbnail_names() -> list[str]:
    """Stored small variants, including legacy rows not rescanned yet."""
    rows = db.get_connection().execute(
        "SELECT DISTINCT thumbnail_path FROM images WHERE thumbnail_path IS NOT NULL"
    ).fetchall()
    return [Path(str(row["thumbnail_path"])).name for row in rows]


#: What the user set on a resource row, and what a rescan must not undo.
_RESOURCE_OVERRIDES = ("locked_by_user", "added_by_user", "deleted_by_user")


def replace_resources(image_id: int, resources: list[dict[str, Any]]) -> None:
    """Rebuild the resource rows from the file, keeping what the user decided.

    The rows are derived from the infotext and are thrown away on every scan.
    The three override flags are not derived from anything - they are the user's
    answer - so they are read back out before the delete and restored after it.
    A row the user added by hand survives even though the file never mentioned it.
    """
    with db.transaction() as conn:
        kept = {
            (row["resource_type"], row["name_in_prompt"]): dict(row)
            for row in conn.execute(
                "SELECT * FROM image_resources WHERE image_id=?",
                (image_id,),
            )
            if any(row[name] for name in _RESOURCE_OVERRIDES)
        }
        conn.execute("DELETE FROM image_resources WHERE image_id=?", (image_id,))

        incoming = {
            (r.get("resource_type", "lora"), r.get("name_in_prompt", "")) for r in resources
        }
        rows = list(resources)
        # A resource the user added by hand is not in the infotext, so nothing
        # would bring it back. Re-create the row from what was kept.
        for (kind, name), flags in kept.items():
            if (kind, name) not in incoming and flags.get("added_by_user"):
                rows.append(flags)

        for resource in rows:
            key = (resource.get("resource_type", "lora"), resource.get("name_in_prompt", ""))
            flags = kept.get(key, {})
            # A lock protects the chosen resolution, not just the boolean bit.
            # The scanner's freshly parsed row otherwise overwrites the manual
            # model version before the flag has a chance to mean anything.
            if flags.get("locked_by_user"):
                resource = {
                    **resource,
                    **{
                        field: flags.get(field)
                        for field in (
                            "hash",
                            "hash_prefix",
                            "weight",
                            "model_id",
                            "model_version_id",
                            "model_name",
                            "version_name",
                            "resolved_from",
                        )
                    },
                }
            conn.execute(
                "INSERT OR IGNORE INTO image_resources(image_id, resource_type, name_in_prompt,"
                " hash, hash_prefix, weight, model_id, model_version_id, model_name,"
                " version_name, resolved_from, locked_by_user, added_by_user, deleted_by_user)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    image_id,
                    resource.get("resource_type", "lora"),
                    resource.get("name_in_prompt", ""),
                    resource.get("hash"),
                    resource.get("hash_prefix") or hash_prefix(resource.get("hash")),
                    resource.get("weight"),
                    resource.get("model_id"),
                    resource.get("model_version_id"),
                    resource.get("model_name"),
                    resource.get("version_name"),
                    resource.get("resolved_from", "unresolved"),
                    int(bool(flags.get("locked_by_user"))),
                    int(bool(flags.get("added_by_user"))),
                    int(bool(flags.get("deleted_by_user"))),
                ),
            )


def record_metadata_fields(image_id: int, field_names: Iterable[str]) -> None:
    """Replace one image's field membership without inflating the inventory."""
    names = sorted({name for name in field_names if isinstance(name, str) and name})
    seen_at = db.now_iso()
    with db.transaction() as conn:
        previous = {
            row["field_name"]
            for row in conn.execute(
                "SELECT field_name FROM image_metadata_fields WHERE image_id=?",
                (image_id,),
            )
        }

        for name in names:
            conn.execute(
                "INSERT INTO metadata_field_inventory(field_name, image_count, last_seen_at)"
                " VALUES(?, 0, ?)"
                " ON CONFLICT(field_name) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (name, seen_at),
            )

        removed = previous.difference(names)
        if removed:
            placeholders = ",".join("?" * len(removed))
            conn.execute(
                f"DELETE FROM image_metadata_fields WHERE image_id=?"
                f" AND field_name IN ({placeholders})",
                (image_id, *sorted(removed)),
            )

        for name in set(names).difference(previous):
            conn.execute(
                "INSERT INTO image_metadata_fields(image_id, field_name) VALUES(?, ?)",
                (image_id, name),
            )


def metadata_field_inventory(image_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Return fields most commonly present first.

    Library-wide by default. With ``image_ids`` the answer is restricted to the
    fields those images actually carry, counted among them - which is what a
    completion list for an edit of exactly those images should offer.
    """
    if image_ids is None:
        return [
            dict(row)
            for row in db.get_connection().execute(
                "SELECT field_name, image_count, last_seen_at"
                " FROM metadata_field_inventory"
                " ORDER BY image_count DESC, field_name COLLATE NOCASE, field_name"
            )
        ]
    if not image_ids:
        return []
    placeholders = ",".join("?" * len(image_ids))
    return [
        dict(row)
        for row in db.get_connection().execute(
            "SELECT f.field_name AS field_name, COUNT(*) AS image_count,"
            " MAX(i.last_seen_at) AS last_seen_at"
            f" FROM image_metadata_fields f"
            f" JOIN metadata_field_inventory i ON i.field_name = f.field_name"
            f" WHERE f.image_id IN ({placeholders})"
            " GROUP BY f.field_name"
            " ORDER BY image_count DESC, f.field_name COLLATE NOCASE, f.field_name",
            tuple(image_ids),
        )
    ]


def planned_image_ids(image_ids: list[int]) -> set[int]:
    """Which of these images are already placed in a local, unpushed post.

    Derived from ``post_images`` and the post's state, never from
    ``image_usage``: that table is the published history (`IMG-13`,
    `db_schema.sql:75`) and its rows outlive the post, while planning is intent
    that must disappear without trace when the draft is deleted.
    """
    if not image_ids:
        return set()
    # Asked of `post_images`, which holds hundreds of rows, and intersected here
    # - rather than an IN clause with one placeholder per image, which grows with
    # the library and stops working above SQLITE_LIMIT_VARIABLE_NUMBER.
    planned = {
        int(row["image_id"])
        for row in db.get_connection().execute(
            "SELECT DISTINCT p.image_id AS image_id FROM post_images p"
            " JOIN posts o ON o.id = p.post_id"
            " WHERE o.state IN ('draft', 'ready') AND p.image_id IS NOT NULL"
        )
    }
    return planned.intersection(image_ids)


def mark_missing(root_id: int, seen_ids: set[int]) -> int:
    """Reconcile rows not seen in a completed pass of an available root.

    A post-held image remains addressable and is marked missing. With no post
    holding it, the scan-derived row has no remaining owner and is removed. The
    denormalised ``image_usage`` table is independent of this row and is never
    changed here.
    """
    missing = "source_root_id=? AND is_trashed=0"
    params: tuple[Any, ...] = (root_id,)
    if seen_ids:
        placeholders = ",".join("?" * len(seen_ids))
        missing += f" AND id NOT IN ({placeholders})"
        params = (root_id, *seen_ids)

    with db.transaction() as conn:
        deleted = conn.execute(
            f"DELETE FROM images WHERE {missing}"
            " AND NOT EXISTS (SELECT 1 FROM post_images WHERE post_images.image_id=images.id)",
            params,
        ).rowcount
        marked = conn.execute(
            f"UPDATE images SET is_missing=1 WHERE {missing} AND is_missing=0",
            params,
        ).rowcount
    return deleted + marked


def reconcile_missing_candidates(image_ids: list[int]) -> set[int]:
    """Reconcile vanished files from one explicitly bounded candidate set.

    Duplicate search supplies only rows its hash passes already reported. A
    reachable source root lets that deliberate action apply the same ownership
    rule as a full scan without turning the search into a library-wide stat.
    """
    unique_ids = list(dict.fromkeys(image_ids))
    vanished: set[int] = set()
    conn = db.get_connection()
    for start in range(0, len(unique_ids), 500):
        chunk = unique_ids[start : start + 500]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT i.id, i.absolute_path, r.path AS root_path FROM images i"
            " JOIN source_roots r ON r.id=i.source_root_id"
            f" WHERE i.id IN ({placeholders})"
            " AND i.is_missing=0 AND i.is_trashed=0",
            chunk,
        )
        vanished.update(
            int(row["id"])
            for row in rows
            if Path(row["root_path"]).is_dir() and not Path(row["absolute_path"]).is_file()
        )
    if not vanished:
        return vanished

    with db.transaction() as conn:
        for start in range(0, len(vanished), 500):
            chunk = list(vanished)[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM images WHERE id IN ({placeholders})"
                " AND NOT EXISTS (SELECT 1 FROM post_images"
                " WHERE post_images.image_id=images.id)",
                chunk,
            )
            conn.execute(
                f"UPDATE images SET is_missing=1 WHERE id IN ({placeholders})"
                " AND EXISTS (SELECT 1 FROM post_images"
                " WHERE post_images.image_id=images.id)",
                chunk,
            )
    return vanished


def remove_vanished_trashed(trash_folder: Path | None) -> int:
    """Forget trash rows whose files vanished while the trash is reachable."""
    if trash_folder is None or not trash_folder.is_dir():
        return 0

    folder = trash_folder.resolve()
    vanished = []
    for row in db.get_connection().execute(
        "SELECT id, absolute_path FROM images WHERE is_trashed=1"
    ):
        path = Path(row["absolute_path"])
        resolved = path.resolve()
        if (resolved == folder or folder in resolved.parents) and not path.exists():
            vanished.append(int(row["id"]))
    if not vanished:
        return 0

    placeholders = ",".join("?" * len(vanished))
    with db.transaction() as conn:
        cursor = conn.execute(
            f"DELETE FROM images WHERE id IN ({placeholders}) AND is_trashed=1",
            vanished,
        )
    return cursor.rowcount


def get(image_id: int) -> dict[str, Any] | None:
    row = db.get_connection().execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
    return _row(row) if row else None


def get_by_path(absolute_path: str) -> dict[str, Any] | None:
    row = db.get_connection().execute(
        "SELECT * FROM images WHERE absolute_path=?", (absolute_path,)
    ).fetchone()
    return _row(row) if row else None


def get_many(image_ids: list[int]) -> list[dict[str, Any]]:
    if not image_ids:
        return []
    placeholders = ",".join("?" * len(image_ids))
    rows = db.get_connection().execute(
        f"SELECT * FROM images WHERE id IN ({placeholders})", image_ids
    )
    by_id = {row["id"]: _row(row) for row in rows}
    return [by_id[i] for i in image_ids if i in by_id]


def source_root_path(root_id: int) -> Path | None:
    row = db.get_connection().execute(
        "SELECT path FROM source_roots WHERE id=?", (root_id,)
    ).fetchone()
    if row is None:
        return None
    return Path(row["path"])


def resources_for(image_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.get_connection().execute(
            "SELECT * FROM image_resources WHERE image_id=? ORDER BY resource_type, id",
            (image_id,),
        )
    ]


def resource_override_ids(image_ids: list[int]) -> set[int]:
    """Which of these images carry a user override on a resource row."""
    if not image_ids:
        return set()
    placeholders = ",".join("?" * len(image_ids))
    rows = db.get_connection().execute(
        f"SELECT DISTINCT image_id FROM image_resources"
        f" WHERE image_id IN ({placeholders})"
        " AND (locked_by_user OR added_by_user OR deleted_by_user)",
        image_ids,
    )
    return {int(row["image_id"]) for row in rows}


def resource_image_id(resource_id: int) -> int | None:
    """Which image a resource row belongs to, and nothing else.

    Deliberately narrower than :func:`resource`: a caller that only needs to know
    whose image this is should not issue the whole-row read. That read is held
    inside a transaction where it matters (see the delete path), and a second one
    outside it would be the first read a tracing test sees.
    """
    row = db.get_connection().execute(
        "SELECT image_id FROM image_resources WHERE id=?", (resource_id,)
    ).fetchone()
    return int(row["image_id"]) if row else None


def resource(resource_id: int) -> dict[str, Any] | None:
    row = db.get_connection().execute(
        "SELECT * FROM image_resources WHERE id=?", (resource_id,)
    ).fetchone()
    return dict(row) if row else None


def add_resource(image_id: int, values: dict[str, Any]) -> dict[str, Any]:
    """Add a durable user-owned resource row."""
    digest = str(values.get("hash") or "").strip().upper() or None
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO image_resources(image_id, resource_type, name_in_prompt, hash,"
            " hash_prefix, weight, model_id, model_version_id, model_name, version_name,"
            " resolved_from, locked_by_user, added_by_user)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (
                image_id,
                values.get("resource_type", "lora"),
                values.get("name_in_prompt", ""),
                digest,
                hash_prefix(digest),
                values.get("weight"),
                values.get("model_id"),
                values.get("model_version_id"),
                values.get("model_name"),
                values.get("version_name"),
                # `resolved_from` describes the identity, not who created the
                # row. A row added with nothing but a name has no identity, so
                # it is unresolved; stamping it `manual` made every hand-added
                # resource permanently unresolvable, because the projectors
                # read that mark as "the user decided this".
                values.get("resolved_from")
                or ("manual" if values.get("model_version_id") else "unresolved"),
                int(bool(values.get("locked_by_user"))),
            ),
        )
        resource_id = int(cursor.lastrowid)
    return resource(resource_id) or {}


def delete_resource(resource_id: int) -> dict[str, Any] | None:
    """Delete an added row, or tombstone a row derived from the file."""
    with db.transaction() as conn:
        current = conn.execute(
            "SELECT * FROM image_resources WHERE id=?", (resource_id,)
        ).fetchone()
        if current is None:
            return None
        if current["added_by_user"]:
            conn.execute("DELETE FROM image_resources WHERE id=?", (resource_id,))
            return {}
        conn.execute(
            "UPDATE image_resources SET deleted_by_user=1 WHERE id=?",
            (resource_id,),
        )
        updated = conn.execute(
            "SELECT * FROM image_resources WHERE id=?", (resource_id,)
        ).fetchone()
        return dict(updated) if updated else None


def restore_resource(resource_id: int) -> dict[str, Any] | None:
    """Restore a file row, while cleaning up only a legacy added tombstone."""
    with db.transaction() as conn:
        current = conn.execute(
            "SELECT * FROM image_resources WHERE id=?", (resource_id,)
        ).fetchone()
        if current is None:
            return None
        if current["added_by_user"] and current["deleted_by_user"]:
            conn.execute("DELETE FROM image_resources WHERE id=?", (resource_id,))
            return {}
        conn.execute(
            "UPDATE image_resources SET deleted_by_user=0 WHERE id=?",
            (resource_id,),
        )
        updated = conn.execute(
            "SELECT * FROM image_resources WHERE id=?", (resource_id,)
        ).fetchone()
        return dict(updated) if updated else None


def update_resource(resource_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
    """Update only editable columns; override flags remain explicit."""
    allowed = {
        "name_in_prompt",
        "hash",
        "weight",
        "model_id",
        "model_version_id",
        "model_name",
        "version_name",
        "resolved_from",
        "locked_by_user",
        "deleted_by_user",
    }
    changes = {key: value for key, value in values.items() if key in allowed}
    if "hash" in changes:
        digest = str(changes["hash"] or "").strip().upper() or None
        changes["hash"] = digest
        changes["hash_prefix"] = hash_prefix(digest)
        allowed.add("hash_prefix")
    if not changes:
        return resource(resource_id)
    assignments = ", ".join(f"{key}=?" for key in changes)
    with db.transaction() as conn:
        conn.execute(
            f"UPDATE image_resources SET {assignments} WHERE id=?",
            (*changes.values(), resource_id),
        )
    return resource(resource_id)


def search_candidates(
    *,
    root_id: int | None = None,
    folder: str | None = None,
    query: str | None = None,
    include_missing: bool = False,
) -> list[dict[str, Any]]:
    """Return the complete filtered set before usage and stack presentation."""
    where, params = _search_filters(
        root_id=root_id,
        folder=folder,
        query=query,
        include_missing=include_missing,
    )
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return [
        _row(row)
        for row in db.get_connection().execute(
            f"SELECT i.* FROM images i {clause} ORDER BY i.id", params
        )
    ]


def search(
    *,
    root_id: int | None = None,
    folder: str | None = None,
    query: str | None = None,
    include_missing: bool = False,
    candidate_ids: set[int] | None = None,
    duplicate_groups: list[dict[str, Any]] | None = None,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Image rows, optionally collapsed into duplicate-group library tiles."""
    where, params = _search_filters(
        root_id=root_id,
        folder=folder,
        query=query,
        include_missing=include_missing,
    )
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    conn = db.get_connection()
    if duplicate_groups is None and candidate_ids is None:
        total = int(
            conn.execute(f"SELECT COUNT(*) AS n FROM images i {clause}", params).fetchone()["n"]
        )
        rows = conn.execute(
            f"SELECT i.* FROM images i {clause}"
            " ORDER BY i.folder, i.relative_path LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [_row(row) for row in rows], total

    group_by_id = {
        member["id"]: group["key"]
        for group in duplicate_groups
        for member in group["members"]
    }
    candidates = [
        dict(row)
        for row in conn.execute(
            f"SELECT i.id, i.is_missing, i.folder, i.relative_path FROM images i {clause}"
            " ORDER BY i.is_missing, i.folder, i.relative_path, i.id",
            params,
        )
    ]
    if candidate_ids is not None:
        candidates = [row for row in candidates if row["id"] in candidate_ids]
    seen: set[tuple[str, str | int]] = set()
    representatives = []
    for candidate in candidates:
        image_id = candidate["id"]
        key: tuple[str, str | int] = (
            ("group", group_by_id[image_id])
            if image_id in group_by_id
            else ("image", image_id)
        )
        if key in seen:
            continue
        seen.add(key)
        representatives.append(candidate)

    representatives.sort(
        key=lambda row: (row["folder"], row["relative_path"], row["id"])
    )

    total = len(representatives)
    page_ids = [row["id"] for row in representatives[offset : offset + limit]]
    if not page_ids:
        return [], total
    placeholders = ",".join("?" for _ in page_ids)
    by_id = {
        row["id"]: _row(row)
        for row in conn.execute(
            f"SELECT i.* FROM images i WHERE i.id IN ({placeholders})", page_ids
        )
    }
    return [by_id[image_id] for image_id in page_ids], total


def _search_filters(
    *,
    root_id: int | None,
    folder: str | None,
    query: str | None,
    include_missing: bool,
) -> tuple[list[str], list[Any]]:
    """The shared library filter used for both tiles and their copy badges."""
    where, params = [], []
    if not include_missing:
        where.append("i.is_missing=0")
    where.append("i.is_trashed=0")
    if root_id is not None:
        where.append("i.source_root_id=?")
        params.append(root_id)
    if folder:
        where.append("(i.folder=? OR instr(i.folder, ? || '/')=1)")
        params.extend([folder, folder])
    literal_query = query.strip() if query else ""
    if literal_query:
        where.append(
            "i.id IN (SELECT rowid FROM image_search WHERE image_search MATCH ?)"
        )
        # A quoted FTS phrase makes punctuation ordinary input. Doubling quotes
        # is FTS5's string escape, so user text never becomes query syntax.
        escaped_query = literal_query.replace('"', '""')
        params.append(f'"{escaped_query}"')
    return where, params


def duplicate_groups_for(
    rows: list[dict[str, Any]],
    duplicate_groups: list[dict[str, Any]],
    *,
    root_id: int | None = None,
    folder: str | None = None,
    query: str | None = None,
    include_missing: bool = False,
) -> dict[int, dict[str, Any]]:
    """Filtered duplicate stacks represented by the current library page."""
    group_by_id = {
        member["id"]: group
        for group in duplicate_groups
        for member in group["members"]
    }
    represented = {
        group_by_id[row["id"]]["key"]: (row["id"], group_by_id[row["id"]])
        for row in rows
        if row["id"] in group_by_id
    }
    if not represented:
        return {}
    where, filter_params = _search_filters(
        root_id=root_id,
        folder=folder,
        query=query,
        include_missing=include_missing,
    )
    members: dict[str, list[dict[str, Any]]] = {key: [] for key in represented}
    for row in db.get_connection().execute(
        "SELECT i.id, i.source_root_id, i.absolute_path, i.relative_path, i.folder,"
        " i.file_size, i.width, i.height, i.thumbnail_path, i.is_missing FROM images i"
        f" WHERE {' AND '.join(where)} ORDER BY i.folder, i.relative_path, i.id",
        filter_params,
    ):
        group = group_by_id.get(row["id"])
        if group is None or group["key"] not in represented:
            continue
        member = dict(row)
        member["is_missing"] = bool(member["is_missing"])
        members[group["key"]].append(member)
    return {
        representative_id: {"confidence": group["confidence"], "members": members[key]}
        for key, (representative_id, group) in represented.items()
        if len(members[key]) > 1
    }


def folders(root_id: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT folder, COUNT(*) AS count FROM images WHERE is_missing=0 AND is_trashed=0"
    params: list[Any] = []
    if root_id is not None:
        sql += " AND source_root_id=?"
        params.append(root_id)
    sql += " GROUP BY folder ORDER BY folder"
    return [dict(row) for row in db.get_connection().execute(sql, params)]


def delete(image_id: int) -> None:
    """Remove the row for good.

    A hard delete, unlike :func:`mark_missing`: this is only called once the file
    itself is gone deliberately. The schema is built for it - ``post_images``
    keeps its own file snapshot and lets ``image_id`` go to NULL, and
    ``image_usage`` is denormalised, so the record that these bytes were once
    published survives.
    """
    with db.transaction() as conn:
        conn.execute("DELETE FROM images WHERE id=?", (image_id,))


def trashed() -> list[dict[str, Any]]:
    """Rows whose files are waiting in the global trash folder."""
    return [
        _row(row)
        for row in db.get_connection().execute(
            "SELECT * FROM images WHERE is_trashed=1"
            " ORDER BY trashed_at DESC, id DESC"
        )
    ]


def _row(row: Any) -> dict[str, Any]:
    value = dict(row)
    value.pop("library_rank", None)
    raw = value.pop("parsed_json", None)
    try:
        value["parsed"] = json.loads(raw) if raw else None
    except ValueError:
        value["parsed"] = None
    value["is_missing"] = bool(value.get("is_missing"))
    value["is_trashed"] = bool(value.get("is_trashed"))
    return value
