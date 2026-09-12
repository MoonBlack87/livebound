"""Model folders and the hashes already read from their files."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .. import db


def list_roots(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM model_roots"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id"
    return [_root_row(row) for row in db.get_connection().execute(sql)]


def get_root(root_id: int) -> dict[str, Any] | None:
    row = db.get_connection().execute(
        "SELECT * FROM model_roots WHERE id=?", (root_id,)
    ).fetchone()
    return _root_row(row) if row else None


def add_root(path: str, *, label: str = "") -> dict[str, Any]:
    resolved = str(Path(path).expanduser().resolve())
    with db.transaction() as conn:
        conn.execute(
            # An empty label keeps whatever is stored: adding the same folder
            # again through the directory browser sends none, and that must not
            # erase a name the maintainer typed earlier.
            "INSERT INTO model_roots(path, label, enabled, created_at) VALUES(?,?,1,?)"
            " ON CONFLICT(path) DO UPDATE SET"
            " label=CASE WHEN excluded.label = '' THEN model_roots.label"
            " ELSE excluded.label END, enabled=1",
            (resolved, label, db.now_iso()),
        )
    row = db.get_connection().execute(
        "SELECT * FROM model_roots WHERE path=?", (resolved,)
    ).fetchone()
    return _root_row(row)


def delete_root(root_id: int) -> None:
    """Forget the folder inventory; resource_map deliberately remains untouched."""
    with db.transaction() as conn:
        conn.execute("DELETE FROM model_roots WHERE id=?", (root_id,))


def get_file(root_id: int, path: Path) -> dict[str, Any] | None:
    row = db.get_connection().execute(
        "SELECT * FROM model_files WHERE model_root_id=? AND absolute_path=?",
        (root_id, str(path)),
    ).fetchone()
    return dict(row) if row else None


def get_file_by_id(file_id: int) -> dict[str, Any] | None:
    row = db.get_connection().execute(
        "SELECT * FROM model_files WHERE id=?", (file_id,)
    ).fetchone()
    return dict(row) if row else None


def upsert_file(
    root_id: int,
    path: Path,
    relative_path: str,
    *,
    mtime: float,
    size: int,
    sha256: str,
    file_stem: str,
    ss_output_name: str | None = None,
    modelspec_title: str | None = None,
) -> None:
    digest = sha256.upper()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO model_files(model_root_id, absolute_path, relative_path, file_mtime,"
            " file_size, sha256, hash_prefix, file_stem, ss_output_name, modelspec_title,"
            " hashed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(model_root_id, absolute_path) DO UPDATE SET"
            " relative_path=excluded.relative_path, file_mtime=excluded.file_mtime,"
            " file_size=excluded.file_size, sha256=excluded.sha256,"
            " hash_prefix=excluded.hash_prefix, file_stem=excluded.file_stem,"
            " ss_output_name=excluded.ss_output_name,"
            " modelspec_title=excluded.modelspec_title, hashed_at=excluded.hashed_at",
            (
                root_id,
                str(path),
                relative_path,
                mtime,
                size,
                digest,
                digest[:10],
                file_stem,
                ss_output_name,
                modelspec_title,
                db.now_iso(),
            ),
        )


def hash_for_prompt_tag(tag: str) -> str | None:
    """Return one local file hash whose stored identity matches ``tag``."""
    from ..metadata.resources import normalize

    key = normalize(tag)
    if not key:
        return None
    try:
        rows = db.get_connection().execute(
            "SELECT files.sha256, files.file_stem, files.ss_output_name, files.modelspec_title"
            " FROM model_files AS files"
            " JOIN model_roots AS roots ON roots.id=files.model_root_id"
            " WHERE roots.enabled=1"
        )
    except sqlite3.OperationalError:
        # Resource rows are re-extracted before the schema script during an old
        # database migration; the model inventory may not exist yet.
        return None
    matches = {
        row["sha256"]
        for row in rows
        if any(
            normalize(row[column]) == key
            for column in ("file_stem", "ss_output_name", "modelspec_title")
            if row[column]
        )
    }
    return next(iter(matches)) if len(matches) == 1 else None


def suggest_resources(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Names a hand-added resource can be given, with the hash that identifies it.

    Two sources, because a model reaches somebody two ways. Files in the local
    inventory are offered under the name the resolver actually matches on, so
    typing the suggestion is enough. Identities already in `resource_map` are
    offered as well, and those matter most: a model assigned once through Find
    model but never downloaded has no file to match a name against, so without
    this it could only ever be found by searching CivitAI again.

    Every entry carries its hash. That is what lets the caller record a choice
    as a hash identity rather than a guess about a name.
    """
    typed = query.strip()
    if not typed:
        return []
    # `_` is in half the model filenames there are, and LIKE reads it as "any one
    # character". Escaped, so typing a name matches that name.
    escaped = typed.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    # SQLite LIKE is case-insensitive for ASCII and case-sensitive beyond it;
    # that is SQLite's matching behaviour, not a policy chosen by this function.
    key = f"%{escaped}%"
    conn = db.get_connection()
    seen: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        """
        SELECT files.sha256, files.file_stem, files.ss_output_name, files.modelspec_title,
               identity.model_id, identity.model_version_id,
               identity.model_name, identity.version_name
        FROM model_files AS files
        JOIN model_roots AS roots ON roots.id = files.model_root_id
        LEFT JOIN resource_map AS identity ON identity.hash = files.sha256
        WHERE roots.enabled = 1 AND files.file_stem != '' AND files.sha256 != ''
          AND (files.file_stem LIKE ? ESCAPE '\\'
               OR COALESCE(files.ss_output_name, '') LIKE ? ESCAPE '\\'
               OR COALESCE(files.modelspec_title, '') LIKE ? ESCAPE '\\')
        ORDER BY LENGTH(files.file_stem), files.file_stem
        """,
        (key, key, key),
    ):
        digest = str(row["sha256"]).upper()
        seen.setdefault(
            digest,
            {
                "name": row["file_stem"],
                "hash": digest,
                "model_id": row["model_id"],
                "model_version_id": row["model_version_id"],
                "model_name": row["model_name"],
                "version_name": row["version_name"],
                "source": "local",
            },
        )

    for row in conn.execute(
        """
        SELECT hash, model_id, model_version_id, model_name, version_name
        FROM resource_map
        WHERE model_version_id IS NOT NULL AND model_name IS NOT NULL
          AND model_name LIKE ? ESCAPE '\\'
        ORDER BY LENGTH(model_name), model_name
        """,
        (key,),
    ):
        digest = str(row["hash"]).upper()
        # A local file that also has an identity is already listed, under the
        # name the resolver matches. Offering it a second time under its CivitAI
        # name would be two entries for one model.
        if digest in seen:
            continue
        seen[digest] = {
            "name": row["model_name"],
            "hash": digest,
            "model_id": row["model_id"],
            "model_version_id": row["model_version_id"],
            "model_name": row["model_name"],
            "version_name": row["version_name"],
            "source": "known",
        }

    ascii_fold = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    folded_typed = typed.translate(ascii_fold)

    def suggestion_order(item: dict[str, Any]) -> tuple[int, int, str]:
        name = str(item["name"])
        folded_name = name.translate(ascii_fold)
        group = (
            0
            if folded_name == folded_typed
            else 1
            if folded_name.startswith(folded_typed)
            else 2
        )
        return group, len(name), name

    items = sorted(seen.values(), key=suggestion_order)
    return items[:limit]


def forget_vanished(root_id: int, seen_paths: set[str]) -> int:
    """Drop rows for files that are no longer in a root the scan actually walked.

    A hard delete, unlike ``images.mark_missing()`` - and safe to be one, because
    this table is an inventory, not a memory. What a model *is* lives in
    ``resource_map`` keyed by its hash and survives regardless.

    The caller must only pass a root it could read. An unreachable folder yields
    no paths, and wiping its inventory because a drive was unmounted would mean
    hashing it all again for nothing.
    """
    with db.transaction() as conn:
        if seen_paths:
            placeholders = ",".join("?" * len(seen_paths))
            cursor = conn.execute(
                f"DELETE FROM model_files WHERE model_root_id=?"
                f" AND absolute_path NOT IN ({placeholders})",
                (root_id, *seen_paths),
            )
        else:
            cursor = conn.execute(
                "DELETE FROM model_files WHERE model_root_id=?", (root_id,)
            )
        return cursor.rowcount


def mark_hashed(root_ids: list[int]) -> None:
    if not root_ids:
        return
    placeholders = ",".join("?" * len(root_ids))
    with db.transaction() as conn:
        conn.execute(
            f"UPDATE model_roots SET last_hashed_at=? WHERE id IN ({placeholders})",
            (db.now_iso(), *root_ids),
        )


def identity_counts() -> dict[str, int]:
    row = db.get_connection().execute(
        """
        WITH identities AS (
            SELECT sha256 AS hash FROM model_files
            UNION
            SELECT hash FROM resource_map WHERE length(hash)=64
        )
        SELECT
            COUNT(DISTINCT CASE WHEN resource.model_version_id IS NOT NULL
                                THEN identities.hash END) AS recognized,
            COUNT(DISTINCT CASE WHEN resource.http_status=404
                                THEN identities.hash END) AS unrecognized,
            COUNT(DISTINCT CASE WHEN resource.hash IS NULL
                                THEN identities.hash END) AS unqueried
        FROM identities
        LEFT JOIN resource_map AS resource ON resource.hash=identities.hash
        """
    ).fetchone()
    return {
        "recognized": int(row["recognized"] or 0),
        "unrecognized": int(row["unrecognized"] or 0),
        "unqueried": int(row["unqueried"] or 0),
    }


def inventory(
    *,
    filter_name: str = "all",
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """One bounded page of local files and durable model identities.

    ``model_files`` is only the path inventory. A full-hash ``resource_map`` row
    remains after its last path disappears and is returned as locally
    unavailable, preserving the stable CivitAI identity without pretending the
    file can still be opened.
    """
    where: list[str] = []
    params: list[Any] = []
    if filter_name == "recognized":
        where.append("inventory.model_version_id IS NOT NULL")
    elif filter_name == "unrecognized":
        where.append("inventory.http_status=404")
    elif filter_name == "unqueried":
        where.append("inventory.model_version_id IS NULL AND inventory.http_status IS NULL")
    elif filter_name == "duplicates":
        where.append(
            "inventory.locally_available=1 AND "
            "EXISTS (SELECT 1 FROM model_files AS copy"
            " WHERE copy.sha256=inventory.sha256 AND copy.id!=inventory.id)"
        )
    if query and (needle := query.strip().lower()):
        fields = (
            "inventory.file_stem",
            "inventory.ss_output_name",
            "inventory.modelspec_title",
            "inventory.model_name",
            "inventory.version_name",
            "inventory.sha256",
        )
        searches = [f"instr(lower(COALESCE({field}, '')), ?) > 0" for field in fields]
        where.append(f"({' OR '.join(searches)})")
        params.extend([needle] * len(fields))

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    inventory = (
        " WITH inventory AS ("
        " SELECT files.id, files.model_root_id, files.absolute_path, files.relative_path,"
        " files.file_mtime, files.file_size, files.sha256, files.hash_prefix,"
        " files.file_stem, files.ss_output_name, files.modelspec_title, files.hashed_at,"
        " roots.path AS root_path, roots.label AS root_label, resource.model_id,"
        " resource.model_version_id, resource.model_name, resource.version_name,"
        " resource.source, resource.http_status, 1 AS locally_available"
        " FROM model_files AS files"
        " JOIN model_roots AS roots ON roots.id=files.model_root_id"
        " LEFT JOIN resource_map AS resource ON resource.hash=files.sha256"
        " UNION ALL"
        " SELECT NULL, NULL, NULL, NULL, NULL, NULL, resource.hash,"
        " resource.hash_prefix, NULL, NULL, NULL, resource.queried_at, NULL, NULL,"
        " resource.model_id, resource.model_version_id, resource.model_name,"
        " resource.version_name, resource.source, resource.http_status, 0"
        " FROM resource_map AS resource"
        " WHERE length(resource.hash)=64"
        " AND NOT EXISTS (SELECT 1 FROM model_files WHERE model_files.sha256=resource.hash)"
        ")"
    )
    conn = db.get_connection()
    total = int(
        conn.execute(
            f"{inventory} SELECT COUNT(*) AS n FROM inventory {clause}", params
        ).fetchone()["n"]
    )
    rows = conn.execute(
        f"{inventory} SELECT * FROM inventory {clause}"
        " ORDER BY locally_available DESC,"
        " lower(COALESCE(NULLIF(file_stem, ''), model_name, version_name, sha256)),"
        " COALESCE(absolute_path, ''), COALESCE(id, 0)"
        " LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return [_inventory_row(row) for row in rows], total


def duplicate_groups(
    *, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int, int]:
    """One bounded page of identical model-file groups, including every path."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    conn = db.get_connection()
    summary = conn.execute(
        """
        SELECT COUNT(*) AS groups, COALESCE(SUM(redundant_size), 0) AS redundant_size
        FROM (
            SELECT MAX(file_size) * (COUNT(*) - 1) AS redundant_size
            FROM model_files
            GROUP BY sha256 HAVING COUNT(*) > 1
        )
        """
    ).fetchone()
    rows = conn.execute(
        """
        WITH duplicate_hashes AS (
            SELECT sha256, COUNT(*) AS copies, MAX(file_size) AS file_size,
                   MAX(file_size) * (COUNT(*) - 1) AS redundant_size
            FROM model_files
            GROUP BY sha256 HAVING COUNT(*) > 1
        ), page AS (
            SELECT * FROM duplicate_hashes
            ORDER BY redundant_size DESC, sha256
            LIMIT ? OFFSET ?
        )
        SELECT page.*, files.absolute_path, files.relative_path,
               files.model_root_id, roots.path AS root_path, roots.label AS root_label
        FROM page
        JOIN model_files AS files ON files.sha256=page.sha256
        JOIN model_roots AS roots ON roots.id=files.model_root_id
        ORDER BY page.redundant_size DESC, page.sha256, files.absolute_path
        """,
        (limit, offset),
    )
    groups: list[dict[str, Any]] = []
    by_hash: dict[str, dict[str, Any]] = {}
    for row in rows:
        group = by_hash.get(row["sha256"])
        if group is None:
            group = {
                "sha256": row["sha256"],
                "copies": row["copies"],
                "file_size": row["file_size"],
                "redundant_size": row["redundant_size"],
                "paths": [],
            }
            by_hash[row["sha256"]] = group
            groups.append(group)
        group["paths"].append(
            {
                "absolute_path": row["absolute_path"],
                "relative_path": row["relative_path"],
                "model_root_id": row["model_root_id"],
                "root_path": row["root_path"],
                "root_label": row["root_label"],
            }
        )
    return groups, int(summary["groups"]), int(summary["redundant_size"])


def _inventory_row(row: Any) -> dict[str, Any]:
    value = dict(row)
    value["folder"] = (
        str(Path(value["absolute_path"]).parent) if value.get("absolute_path") else None
    )
    value["recognized"] = value.get("model_version_id") is not None
    value["locally_available"] = bool(value.get("locally_available"))
    value["identities"] = [
        {"source": source, "name": value[column]}
        for column, source in (
            ("file_stem", "file_stem"),
            ("ss_output_name", "ss_output_name"),
            ("modelspec_title", "modelspec_title"),
        )
        if value.get(column)
    ]
    return value


def _root_row(row: Any) -> dict[str, Any]:
    value = dict(row)
    value["enabled"] = bool(value.get("enabled"))
    value["watch_enabled"] = bool(value.get("watch_enabled"))
    value["exists"] = Path(value["path"]).is_dir()
    return value


def set_watch_enabled(root_id: int, enabled: bool) -> None:
    """Switch the periodic re-hash for one model folder. Off is the default."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE model_roots SET watch_enabled=? WHERE id=?",
            (1 if enabled else 0, root_id),
        )
