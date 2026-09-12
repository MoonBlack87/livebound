"""hash -> CivitAI model version, cached locally.

The completed v1.1 migration seeded this table from the former metadata app;
ongoing entries come from public REST lookups. Negative results are cached too -
without that, every rescan re-queries the same permanently unknown hashes and
burns through the rate limit for nothing.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import config, db
from ..civitai import rest
from ..civitai.errors import AuthError, CivitaiError, RateLimited
from ..metadata import resources as resource_extract

_IDENTITY_SOURCE_RANK = {
    "unresolved": 0,
    "local_name": 1,
    "embedded": 2,
    "resource_map": 2,
    "rest": 2,
}


def _identity_rank(resource: dict[str, Any]) -> int:
    """Rank an existing identity; an unlabelled identity stays protected."""
    if resource.get("model_version_id") is None:
        return 0
    return _IDENTITY_SOURCE_RANK.get(str(resource.get("resolved_from")), 2)


def _can_project(resource: dict[str, Any], source: str) -> bool:
    """Whether an automatic projector may replace this resource's identity.

    User override states are absolute: no automatic projector rewrites them.
    Otherwise, a projector can only replace a lower-ranked identity. Embedded
    and hash identities rank above a local-name match, which ranks above none.
    """
    if any(resource.get(flag) for flag in _PROJECTION_BLOCKING_FLAGS):
        return False
    if str(resource.get("resolved_from")) == _MANUAL_IDENTITY:
        return False
    return _identity_rank(resource) < _IDENTITY_SOURCE_RANK[source]


#: The flags that forbid an automatic projector from touching a row at all.
#: `added_by_user` is deliberately not among them, unlike in
#: `store/images.py::_RESOURCE_OVERRIDES`, which decides what survives a rescan
#: and must keep all three. The two questions are different: adding a row says
#: the user created it, never that they chose its identity.
#:
#: What protects a decision instead is `resolved_from='manual'`, the mark every
#: user-set identity carries - including one only half made, a model chosen
#: without a version yet, which has no `model_version_id` to be recognised by.
#: That case is why `added_by_user` used to be listed here, and it stays safe.
#:
#: What is no longer blocked is an added row carrying nothing at all. It holds
#: no decision to protect, and blocking it only sent the user through Find model
#: for a model already sitting on their own disk.
_PROJECTION_BLOCKING_FLAGS = ("locked_by_user", "deleted_by_user")
_MANUAL_IDENTITY = "manual"


def _projectable_resource_where(source: str) -> str:
    """SQL counterpart to :func:`_can_project` for automatic projectors."""
    source_rank = _IDENTITY_SOURCE_RANK[source]
    existing = "model_version_id IS NULL"
    if source_rank > _IDENTITY_SOURCE_RANK["local_name"]:
        existing += " OR resolved_from='local_name'"
    return " AND ".join(
        [
            *(f"{flag}=0" for flag in _PROJECTION_BLOCKING_FLAGS),
            # `resolved_from` is nullable, and `NULL <> 'manual'` is unknown
            # rather than true, which would silently exclude every row that has
            # none. The Python spelling treats such a row as projectable, and
            # the two must not disagree.
            f"(resolved_from IS NULL OR resolved_from <> '{_MANUAL_IDENTITY}')",
            f"({existing})",
        ]
    )


def get(file_hash: str) -> dict[str, Any] | None:
    """Look a hash up by its AutoV2 prefix, so 10-, 12- and 64-char forms all hit."""
    prefix = resource_extract.hash_prefix(file_hash)
    if not prefix:
        return None
    digest = str(file_hash).strip().upper()
    row = db.get_connection().execute(
        "SELECT * FROM resource_map WHERE hash_prefix=?"
        " ORDER BY (source='manual') DESC, (hash=?) DESC,"
        " model_version_id IS NULL, queried_at DESC LIMIT 1",
        # A manual assignment is deliberately prefix-wide and therefore still
        # outranks an exact automated row for a different full hash. Among
        # automatic answers, the exact spelling must beat query recency.
        (prefix, digest),
    ).fetchone()
    return dict(row) if row else None


class InvalidModelUrl(ValueError):
    pass


class ModelVersionRequired(ValueError):
    def __init__(self, message: str, *, model_id: int):
        super().__init__(message)
        self.model_id = model_id


def put(
    file_hash: str,
    payload: dict[str, Any] | None,
    *,
    source: str,
    status: int | None,
    manual_identity: tuple[int, int] | None = None,
    manual_details: dict[str, Any] | None = None,
) -> bool:
    """Store a lookup unless it would overwrite a manual decision."""
    model = (payload or {}).get("model") or {}
    digest = file_hash.upper()
    prefix = resource_extract.hash_prefix(file_hash)
    if manual_identity is None:
        model_id = (payload or {}).get("modelId")
        model_version_id = (payload or {}).get("id")
        raw_json = json.dumps(payload, ensure_ascii=False) if payload else None
    else:
        model_id, model_version_id = manual_identity
        raw_json = None
    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT source FROM resource_map WHERE hash=?"
            " OR (hash_prefix=? AND source='manual')"
            " ORDER BY (source='manual') DESC LIMIT 1",
            (digest, prefix),
        ).fetchone()
        if existing and existing["source"] == "manual" and source != "manual":
            return False
        if manual_identity is not None:
            conn.execute("DELETE FROM resource_map WHERE hash_prefix=?", (prefix,))
        conn.execute(
            "INSERT OR REPLACE INTO resource_map(hash, hash_prefix, resource_type, model_id,"
            " model_version_id, model_name, version_name, thumbnail_url, http_status, source,"
            " queried_at, raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                digest,
                prefix,
                (model.get("type") or "").lower() or None
                if manual_identity is None
                else (
                    str(manual_details.get("resource_type")).lower()
                    if manual_details and manual_details.get("resource_type")
                    else None
                ),
                model_id,
                model_version_id,
                model.get("name")
                if manual_identity is None
                else manual_details.get("model_name") if manual_details else None,
                (payload or {}).get("name")
                if manual_identity is None
                else manual_details.get("version_name") if manual_details else None,
                _first_image(payload)
                if manual_identity is None
                else manual_details.get("thumbnail_url") if manual_details else None,
                status,
                source,
                db.now_iso(),
                raw_json,
            ),
        )
    return True


def parse_model_url(value: str) -> tuple[int, int]:
    """Read the model and version ids from a pasted URL without trusting its host."""
    parsed = urlparse(value.strip())
    match = re.search(r"(?:^|/)models/(\d+)(?:/|$)", parsed.path)
    if match is None:
        raise InvalidModelUrl("Paste a CivitAI model URL.")
    # The regex already matched digits, so this cannot raise. It is read here
    # rather than below only because `ModelVersionRequired` carries it now; the
    # refusals keep the order they had, so the other caller of this parser -
    # `api/model_roots.py::assign_model_file` - answers with the same error
    # codes as before for the same input.
    model_id = int(match.group(1))
    versions = parse_qs(parsed.query).get("modelVersionId") or []
    if not versions:
        raise ModelVersionRequired("The URL must include a model version.", model_id=model_id)
    try:
        version_id = int(versions[0])
    except ValueError as exc:
        raise InvalidModelUrl("The model URL contains an invalid id.") from exc
    if model_id < 1 or version_id < 1:
        raise InvalidModelUrl("The model URL contains an invalid id.")
    return model_id, version_id


def assign_manual(
    file_hash: str,
    model_id: int,
    version_id: int,
    *,
    model_name: str | None = None,
    version_name: str | None = None,
    resource_type: str | None = None,
    thumbnail_url: str | None = None,
) -> dict[str, Any]:
    """Make a hash-wide identity a durable user decision."""
    manual_details = {
        "model_name": model_name,
        "version_name": version_name,
        "resource_type": resource_type,
        "thumbnail_url": thumbnail_url,
    }
    put(
        file_hash,
        None,
        source="manual",
        status=None,
        manual_identity=(model_id, version_id),
        manual_details=(
            manual_details if any(value is not None for value in manual_details.values()) else None
        ),
    )
    return get_exact(file_hash) or {}


def _first_image(payload: dict[str, Any] | None) -> str | None:
    images = (payload or {}).get("images") or []
    return images[0].get("url") if images and isinstance(images[0], dict) else None


def is_stale(row: dict[str, Any]) -> bool:
    when = row.get("queried_at")
    if not when:
        return True
    try:
        parsed = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except ValueError:
        return True
    age = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
    return age > timedelta(days=config.HASH_CACHE_STALE_DAYS)


def enrich(resources: list[dict[str, Any]]) -> None:
    """Fill in model identity from the local cache. Never hits the network.

    The scan path must stay offline: a folder of 2000 images would otherwise fire
    2000 requests. Anything still unresolved is looked up on demand by
    :func:`resolve_missing`.
    """
    for resource in resources:
        if not resource.get("hash") or not _can_project(resource, "resource_map"):
            continue
        row = get(resource["hash"])
        if row and row.get("model_version_id"):
            resource["model_id"] = row["model_id"]
            resource["model_version_id"] = row["model_version_id"]
            resource["model_name"] = row["model_name"]
            resource["version_name"] = row["version_name"]
            resource["thumbnail_url"] = row.get("thumbnail_url")
            resource["resolved_from"] = "resource_map"


def resolve_missing(
    hashes: list[str], *, force: bool = False, stop_on_refusal: bool = False
) -> dict[str, Any]:
    """Look unresolved hashes up against CivitAI, one request each.

    Called explicitly by the user, never by the scanner. A 404 is remembered so
    the same dead hash is not asked about again for two weeks. A batch owner can
    ask to stop when CivitAI refuses every remaining request.
    """
    stats = {"resolved": 0, "unknown": 0, "cached": 0, "failed": 0}
    for file_hash in {h.upper() for h in hashes if h}:
        row = get(file_hash)
        if row and (
            row.get("source") == "manual"
            or (not force and (row.get("model_version_id") or not is_stale(row)))
        ):
            stats["cached"] += 1
            continue
        try:
            payload = rest.model_version_by_hash(file_hash)
        except (AuthError, RateLimited):
            if stop_on_refusal:
                raise
            stats["failed"] += 1
            continue
        except CivitaiError:
            stats["failed"] += 1
            continue
        if payload:
            if put(file_hash, payload, source="rest", status=200):
                stats["resolved"] += 1
        else:
            if put(file_hash, None, source="rest", status=404):
                stats["unknown"] += 1
    return stats


def resolve_hashes_bulk(
    hashes: list[str], *, force: bool = False, stop_on_refusal: bool = False
) -> dict[str, int]:
    """Resolve full hashes, optionally returning a batch-wide refusal to the caller."""
    stats = {"resolved": 0, "unknown": 0, "cached": 0, "failed": 0}
    pending: list[str] = []
    for file_hash in dict.fromkeys(hash_value.upper() for hash_value in hashes if hash_value):
        prefix_row = get(file_hash)
        row = (
            prefix_row
            if prefix_row and prefix_row.get("source") == "manual"
            else get_exact(file_hash)
        )
        if row and (
            row.get("source") == "manual"
            or (not force and (row.get("model_version_id") or not is_stale(row)))
        ):
            stats["cached"] += 1
        else:
            pending.append(file_hash)

    if not pending:
        return stats

    # Chunked here rather than only inside the transport: a failure in a later
    # chunk must not discard the answers the earlier ones already paid for.
    for start in range(0, len(pending), rest.BULK_HASH_LIMIT):
        chunk = pending[start : start + rest.BULK_HASH_LIMIT]
        try:
            by_hash = rest.model_versions_by_hash(chunk)
        except (AuthError, RateLimited):
            if stop_on_refusal:
                raise
            stats["failed"] += len(chunk)
            continue
        except CivitaiError:
            stats["failed"] += len(chunk)
            continue
        for file_hash in chunk:
            payload = by_hash.get(file_hash)
            if payload:
                if put(file_hash, payload, source="rest", status=200):
                    stats["resolved"] += 1
            else:
                if put(file_hash, None, source="rest", status=404):
                    stats["unknown"] += 1
    return stats


def unresolved_image_hashes(image_ids: list[int] | None = None) -> list[str]:
    """Return projectable image-resource hashes, optionally for selected images."""
    if image_ids == []:
        return []

    clauses = ["hash IS NOT NULL", _projectable_resource_where("resource_map")]
    params: list[Any] = []
    if image_ids is not None:
        placeholders = ",".join("?" * len(image_ids))
        clauses.insert(0, f"image_id IN ({placeholders})")
        params.extend(image_ids)
    rows = db.get_connection().execute(
        f"""
        SELECT DISTINCT hash
        FROM image_resources
        WHERE {' AND '.join(clauses)}
        ORDER BY hash
        """,
        params,
    )
    return [str(row["hash"]).strip().upper() for row in rows if row["hash"]]


def resolve_for_images(image_ids: list[int]) -> dict[str, int]:
    """Resolve the still-unknown resources of selected images on demand.

    Full SHA256 values share the bulk endpoint. Generators also write 10- or
    12-character AutoV2 hashes, which that endpoint does not accept, so those
    use the single-hash endpoint. Both paths keep positive and negative cache
    answers, making a later attachment local-only.
    """
    stats = {"resolved": 0, "unknown": 0, "cached": 0, "failed": 0, "reapplied": 0}
    hashes = unresolved_image_hashes(image_ids)
    full = [value for value in hashes if is_full_sha256(value)]
    partial = [value for value in hashes if value not in full]

    for result in (resolve_hashes_bulk(full), resolve_missing(partial)):
        for key in ("resolved", "unknown", "cached", "failed"):
            stats[key] += result[key]
    stats["reapplied"] = sum(reapply_hash(value) for value in hashes)
    return stats


def is_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789ABCDEF" for char in value)


def get_exact(file_hash: str) -> dict[str, Any] | None:
    """Look a hash up by the full digest, with no prefix widening.

    `get()` deliberately answers for the AutoV2 prefix, which is what an
    infotext carries. A caller holding one exact hash needs the other question:
    two full hashes can share a prefix, and a manual assignment is prefix-wide
    on purpose, so the prefix answer may belong to a different model.
    """
    row = db.get_connection().execute(
        "SELECT * FROM resource_map WHERE hash=?", (file_hash.upper(),)
    ).fetchone()
    return dict(row) if row else None


def reapply_hash(file_hash: str) -> int:
    """Apply one cache decision only where it outranks the stored identity."""
    prefix = resource_extract.hash_prefix(file_hash)
    row = get_exact(file_hash)
    if not prefix or not row or row.get("model_version_id") is None:
        return 0
    with db.transaction() as conn:
        return conn.execute(
            f"""
            UPDATE image_resources SET
                model_id=?, model_version_id=?, model_name=?, version_name=?,
                resolved_from = 'resource_map'
            WHERE hash_prefix=? AND {_projectable_resource_where("resource_map")}
            """,
            (
                row.get("model_id"),
                row.get("model_version_id"),
                row.get("model_name"),
                row.get("version_name"),
                prefix,
            ),
        ).rowcount


def projected_images(file_hash: str, *, exclude_image_id: int | None = None) -> int:
    """Count the other images a hash assignment would resolve.

    Deliberately images, not rows: one picture can carry two resource rows with
    the same hash, and the number is shown to the user twice - once before the
    assignment and once after it. Counting rows in one place and images in the
    other makes the second number contradict the first for no reason the user
    can see, so both come from here.
    """
    prefix = resource_extract.hash_prefix(file_hash)
    if not prefix:
        return 0
    clauses = [f"hash_prefix=? AND {_projectable_resource_where('resource_map')}"]
    params: list[Any] = [prefix]
    if exclude_image_id is not None:
        clauses.append("image_id != ?")
        params.append(exclude_image_id)
    row = db.get_connection().execute(
        f"SELECT COUNT(DISTINCT image_id) AS image_count FROM image_resources"
        f" WHERE {' AND '.join(clauses)}",
        params,
    ).fetchone()
    return int(row["image_count"]) if row else 0


def reapply_unresolved() -> int:
    """Resolve unowned rows from their hash cache, then local file names.

    Image resources retain only their common AutoV2 prefix, unlike local model
    files which retain a full SHA256. Resolve each distinct prefix through
    :func:`get`, then use an exact, unambiguous normalized match against a
    file stem, safetensors output name or ModelSpec title for rows the hash
    route could not answer. The latter is deliberately weaker evidence, so it
    is recorded separately from a cache resolution.
    """
    prefixes = db.get_connection().execute(
        f"""
        SELECT DISTINCT hash_prefix
        FROM image_resources
        WHERE {_projectable_resource_where("resource_map")}
          AND hash_prefix != ''
        ORDER BY hash_prefix
        """
    ).fetchall()
    identities = [
        (str(prefix["hash_prefix"]), identity)
        for prefix in prefixes
        if (identity := get(str(prefix["hash_prefix"])))
        and identity.get("model_version_id") is not None
    ]
    reapplied = 0
    with db.transaction() as conn:
        for prefix, identity in identities:
            reapplied += conn.execute(
                f"""
                UPDATE image_resources SET
                    model_id=?, model_version_id=?, model_name=?, version_name=?,
                    resolved_from = 'resource_map'
                WHERE hash_prefix=?
                  AND {_projectable_resource_where("resource_map")}
                """,
                (
                    identity.get("model_id"),
                    identity.get("model_version_id"),
                    identity.get("model_name"),
                    identity.get("version_name"),
                    prefix,
                ),
            ).rowcount
        reapplied += _reapply_local_names(conn)
    return reapplied


def _reapply_local_names(conn: Any) -> int:
    """Apply identities from unique local file names to remaining resource rows."""
    by_name: dict[str, dict[str, dict[str, Any]]] = {}
    for file in conn.execute(
        """
        SELECT files.file_stem, files.ss_output_name, files.modelspec_title,
               files.sha256, resource.model_id,
               resource.model_version_id, resource.model_name, resource.version_name
        FROM model_files AS files
        LEFT JOIN resource_map AS resource ON resource.hash=files.sha256
        WHERE files.sha256 != ''
        """
    ):
        for column in ("file_stem", "ss_output_name", "modelspec_title"):
            key = resource_extract.normalize(file[column])
            if key:
                by_name.setdefault(key, {})[str(file["sha256"])] = dict(file)

    candidates: dict[str, dict[str, Any] | None] = {}
    for name, files in by_name.items():
        # Two *different* files under one name make the name ambiguous, even
        # when only one of them currently has a CivitAI identity: there is no
        # way to tell which one the prompt meant. The same file appearing twice
        # is not that. A backup copy beside the original is one identity, and
        # counting rows instead of identities strands every library that keeps
        # one - which is most of them.
        if len(files) != 1:
            candidates[name] = None
            continue
        only = next(iter(files.values()))
        candidates[name] = only if only["model_version_id"] is not None else None

    if not candidates:
        return 0

    rows = conn.execute(
        f"""
        SELECT id, name_in_prompt
        FROM image_resources
        WHERE {_projectable_resource_where("local_name")}
          AND name_in_prompt != ''
        """
    )
    resolved = 0
    for row in rows:
        identity = candidates.get(resource_extract.normalize(row["name_in_prompt"]))
        if identity is None:
            continue
        resolved += conn.execute(
            f"""
            UPDATE image_resources SET
                model_id=?, model_version_id=?, model_name=?, version_name=?,
                resolved_from='local_name'
            WHERE id=? AND {_projectable_resource_where("local_name")}
                """,
            (
                identity["model_id"],
                identity["model_version_id"],
                identity["model_name"],
                identity["version_name"],
                row["id"],
            ),
        ).rowcount
    return resolved


def suggest_bindings(image_ids: list[int]) -> list[dict[str, Any]]:
    """Model versions worth binding a post to, given its images.

    Ordered by how many of the post's images used them, so the LoRA the set was
    actually made with comes first. Checkpoints are listed too but ranked below
    LoRAs: a post is far more often published on a LoRA's page.
    """
    if not image_ids:
        return []
    placeholders = ",".join("?" * len(image_ids))
    rows = db.get_connection().execute(
        f"""
        SELECT model_version_id, model_id, model_name, version_name, resource_type,
               COUNT(DISTINCT image_id) AS image_count
        FROM image_resources
        WHERE image_id IN ({placeholders}) AND model_version_id IS NOT NULL
        GROUP BY model_version_id
        ORDER BY (resource_type='lora') DESC, image_count DESC, model_name
        """,
        image_ids,
    )
    return [dict(row) for row in rows]
