"""The dedup memory: which bytes have already been in a post.

Append-only and deliberately denormalised. Deleting the local post, the image row
or the file itself must not erase the fact that this picture was published once -
that *is* the warning the user asked for. Hence the copied file, pixel and
perceptual hashes and ``ON DELETE SET NULL`` rather than ``CASCADE``.
"""

from __future__ import annotations

from typing import Any

from .. import config, db
from ..hashing import hamming

#: How sure we are that a candidate really is the same picture.
CERTAIN = "certain"      # matching non-empty file or decoded-pixel SHA-256
PROBABLE = "probable"    # pixels look alike; generation data is comparison detail only

PUSHED = "pushed"
PUBLISHED = "published"
WITHDRAWN = "withdrawn"


#: A 64-bit hash split into eight 8-bit bands. By the pigeonhole principle two
#: hashes within Hamming distance <= 7 must agree on at least one band, so only
#: the candidates sharing a band need comparing. The bands live in indexed
#: columns, so finding them is eight equality probes rather than a scan.
#: The 64-bit hash is split into this many 8-bit bands. Also the threshold
#: above which the pigeonhole guarantee stops holding - see below.
BANDS = 8
_BAND_BITS = 8


def _bands(phash: str | None) -> list[int | None]:
    if not phash:
        return [None] * BANDS
    try:
        value = int(phash, 16)
    except ValueError:
        return [None] * BANDS
    return [(value >> (index * _BAND_BITS)) & 0xFF for index in range(BANDS)]


def generation_identity(parsed: Any) -> dict[str, str | None]:
    """Seed and prompt fingerprint out of a parsed infotext.

    Derived rather than stored on the image row so history recording and manual
    match ranking use the same normalisation.
    """
    fields = (parsed or {}).get("fields") or {}
    seed = fields.get("Seed")
    return {
        "seed": None if seed in (None, "") else str(seed),
        "prompt_hash": prompt_fingerprint((parsed or {}).get("prompt")),
    }


def prompt_fingerprint(prompt: str | None) -> str | None:
    """A short, comparable stand-in for a positive prompt.

    Whitespace and case are normalised away so that a prompt which survived a
    round trip through another tool still matches. Stored as a digest rather
    than the text: it is only ever compared, never read.
    """
    import hashlib
    import re

    text = re.sub(r"\s+", " ", (prompt or "")).strip().lower()
    if len(text) < 8:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record(
    *,
    sha256: str,
    phash: str | None,
    pixel_sha256: str | None = None,
    post_id: int | None,
    remote_post_id: int | None = None,
    remote_image_id: int | None = None,
    source_path: str | None = None,
    post_title: str | None = None,
    status: str = PUSHED,
    origin: str = "push",
    seed: str | None = None,
    prompt_hash: str | None = None,
    used_at: str | None = None,
    conn: Any = None,
) -> None:
    if pixel_sha256 is None:
        row = (conn or db.get_connection()).execute(
            "SELECT pixel_sha256 FROM images"
            " WHERE sha256=? AND pixel_sha256 IS NOT NULL LIMIT 1",
            (sha256,),
        ).fetchone()
        pixel_sha256 = row["pixel_sha256"] if row else None
    bands = _bands(phash) if phash else [None] * BANDS
    sql = (
        "INSERT INTO image_usage(sha256, pixel_sha256, phash, post_id, remote_post_id,"
        " remote_image_id,"
        " source_path, post_title, used_at, status, origin, seed, prompt_hash,"
        " phash_b0, phash_b1, phash_b2, phash_b3, phash_b4, phash_b5, phash_b6, phash_b7)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    params = (
        sha256,
        pixel_sha256,
        phash,
        post_id,
        remote_post_id,
        remote_image_id,
        source_path,
        post_title,
        used_at if used_at is not None else db.now_iso(),
        status,
        origin,
        str(seed) if seed not in (None, "") else None,
        prompt_hash,
        *bands,
    )
    if conn is not None:
        conn.execute(sql, params)
    else:
        with db.transaction() as tx:
            tx.execute(sql, params)


def record_or_complete_remote_image(
    *,
    sha256: str,
    pixel_sha256: str | None,
    phash: str | None,
    post_id: int,
    remote_post_id: int,
    remote_image_id: int,
    source_path: str,
    post_title: str,
    used_at: str,
    status: str,
    origin: str,
    seed: str | None,
    prompt_hash: str | None,
    conn: Any = None,
) -> None:
    """Record one remote publication, or complete the row that already knows it.

    A CivitAI image id identifies one publication. Matching and sync may revisit
    it, but that must enrich the existing memory rather than count the same
    publication twice. An existing origin is retained because it describes how
    that row first entered the history; the supplied origin is for a new row.
    """
    if conn is None:
        with db.transaction() as tx:
            record_or_complete_remote_image(
                sha256=sha256,
                pixel_sha256=pixel_sha256,
                phash=phash,
                post_id=post_id,
                remote_post_id=remote_post_id,
                remote_image_id=remote_image_id,
                source_path=source_path,
                post_title=post_title,
                used_at=used_at,
                status=status,
                origin=origin,
                seed=seed,
                prompt_hash=prompt_hash,
                conn=tx,
            )
        return

    existing = conn.execute(
        "SELECT id FROM image_usage WHERE remote_image_id=? ORDER BY id LIMIT 1",
        (remote_image_id,),
    ).fetchone()
    if existing is None:
        record(
            sha256=sha256,
            pixel_sha256=pixel_sha256,
            phash=phash,
            post_id=post_id,
            remote_post_id=remote_post_id,
            remote_image_id=remote_image_id,
            source_path=source_path,
            post_title=post_title,
            used_at=used_at,
            status=status,
            origin=origin,
            seed=seed,
            prompt_hash=prompt_hash,
            conn=conn,
        )
        return

    bands = _bands(phash)
    conn.execute(
        "UPDATE image_usage SET sha256=?, pixel_sha256=?, phash=?, post_id=?,"
        " remote_post_id=?, source_path=?, post_title=?, used_at=?, status=?, seed=?,"
        " prompt_hash=?, phash_b0=?, phash_b1=?, phash_b2=?, phash_b3=?, phash_b4=?,"
        " phash_b5=?, phash_b6=?, phash_b7=? WHERE id=?",
        (
            sha256,
            pixel_sha256,
            phash,
            post_id,
            remote_post_id,
            source_path,
            post_title,
            used_at,
            status,
            seed,
            prompt_hash,
            *bands,
            existing["id"],
        ),
    )


def fill_pixel_hash(sha256: str, pixel_sha256: str, *, conn: Any = None) -> None:
    """Give historic uses of these exact bytes their pixel hash.

    A NULL-fill keyed on an identical ``sha256``, so the append-only rule holds:
    nothing is replaced, only a column the v12 migration deliberately left empty
    is filled once the scan has decoded the image anyway.
    """
    sql = (
        "UPDATE image_usage SET pixel_sha256=?"
        " WHERE sha256=? AND pixel_sha256 IS NULL"
    )
    if conn is not None:
        conn.execute(sql, (pixel_sha256, sha256))
        return
    with db.transaction() as tx:
        tx.execute(sql, (pixel_sha256, sha256))


def set_status_for_post(post_id: int, status: str, *, conn: Any = None) -> None:
    sql = "UPDATE image_usage SET status=? WHERE post_id=?"
    if conn is not None:
        conn.execute(sql, (status, post_id))
    else:
        with db.transaction() as tx:
            tx.execute(sql, (status, post_id))


def orphaned_remote_post_ids() -> list[int]:
    """Remote posts remembered only by active usage-history rows."""
    return [
        int(row["remote_post_id"])
        for row in db.get_connection().execute(
            "SELECT DISTINCT remote_post_id FROM image_usage"
            " WHERE post_id IS NULL AND remote_post_id IS NOT NULL AND status != ?"
            " ORDER BY remote_post_id",
            (WITHDRAWN,),
        )
    ]


def withdraw_remote_post(remote_post_id: int) -> int:
    """Withdraw every active use of one remote post without erasing history."""
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE image_usage SET status=?"
            " WHERE remote_post_id=? AND status != ?",
            (WITHDRAWN, remote_post_id, WITHDRAWN),
        )
    return cursor.rowcount


def threshold() -> int:
    stored = db.get_setting("phash_threshold")
    if stored:
        try:
            return max(0, min(int(stored), 32))
        except ValueError:
            pass
    return config.PHASH_DISTANCE_THRESHOLD


def exact_match_reason(
    sha256: str | None,
    pixel_sha256: str | None,
    candidate: dict[str, Any],
) -> str | None:
    """Name the exact fact shared by two images, if there is one."""
    candidate_sha256 = candidate.get("sha256")
    if sha256 and candidate_sha256 and candidate_sha256 == sha256:
        return "identical bytes"
    candidate_pixels = candidate.get("pixel_sha256")
    if pixel_sha256 and candidate_pixels and candidate_pixels == pixel_sha256:
        return "identical pixels"
    return None


def match_confidence(
    sha256: str | None,
    pixel_sha256: str | None,
    candidate: dict[str, Any],
) -> str:
    """Apply the confidence rule shared by every duplicate comparison.

    Matching non-empty file or decoded-pixel SHA-256 values are certain. Every
    remaining dHash candidate is probable; seed and prompt describe a generation
    request, not which output of that request this image is.
    """
    return CERTAIN if exact_match_reason(sha256, pixel_sha256, candidate) else PROBABLE


def _classified(
    candidate: dict[str, Any],
    sha256: str | None,
    pixel_sha256: str | None,
    *,
    distance: int,
) -> dict[str, Any]:
    """Present one history candidate with the shared confidence decision."""
    entry = dict(candidate)
    entry["distance"] = distance
    entry["match_reason"] = exact_match_reason(sha256, pixel_sha256, entry)
    entry["confidence"] = match_confidence(sha256, pixel_sha256, entry)
    return entry


def check(
    sha256: str | None,
    phash: str | None,
    *,
    pixel_sha256: str | None = None,
    exclude_post_id: int | None = None,
) -> dict[str, Any]:
    """Has this picture been used before?

    Exact non-empty file and decoded-RGB-pixel hashes are settled. A close dHash
    remains a probable comparison hint regardless of generation data, which can
    describe one request that produced several images.
    """
    conn = db.get_connection()
    exact: list[dict[str, Any]] = []
    if sha256:
        sql = "SELECT * FROM image_usage WHERE sha256=? AND status != ?"
        params: list[Any] = [sha256, WITHDRAWN]
        if exclude_post_id is not None:
            sql += " AND (post_id IS NULL OR post_id != ?)"
            params.append(exclude_post_id)
        sql += " ORDER BY used_at DESC LIMIT 10"
        exact = [
            _classified(dict(row), sha256, pixel_sha256, distance=0)
            for row in conn.execute(sql, params)
        ]

    similar: list[dict[str, Any]] = []
    if pixel_sha256 and not exact:
        sql = (
            "SELECT * FROM image_usage WHERE pixel_sha256=? AND sha256 != ?"
            " AND status != ?"
        )
        params = [pixel_sha256, sha256 or "", WITHDRAWN]
        if exclude_post_id is not None:
            sql += " AND (post_id IS NULL OR post_id != ?)"
            params.append(exclude_post_id)
        sql += " ORDER BY used_at DESC LIMIT 10"
        similar = [
            _classified(dict(row), sha256, pixel_sha256, distance=0)
            for row in conn.execute(sql, params)
        ]

    if phash and not exact and not similar:
        limit = threshold()
        # Full scan with a Python popcount: exact, dependency-free and
        # microseconds at this table's size. See the schema note for the
        # banded-index migration if it ever grows past ~100k rows.
        sql = "SELECT * FROM image_usage WHERE phash IS NOT NULL AND sha256 != ? AND status != ?"
        params = [sha256 or "", WITHDRAWN]
        if exclude_post_id is not None:
            sql += " AND (post_id IS NULL OR post_id != ?)"
            params.append(exclude_post_id)
        for row in conn.execute(sql, params):
            distance = hamming(phash, row["phash"])
            if distance <= limit:
                similar.append(
                    _classified(dict(row), sha256, pixel_sha256, distance=distance)
                )
        # Exact matches first, then by how close the candidates look.
        similar.sort(key=lambda item: (item["confidence"] != CERTAIN, item["distance"]))
        similar = similar[:10]

    confirmed = [row for row in similar if row["confidence"] == CERTAIN]
    return {
        "exact": exact,
        "similar": similar,
        "confirmed": confirmed,
        "has_exact": bool(exact),
        "has_similar": bool(similar),
        # The question the UI actually asks: is this definitely a re-post?
        "has_certain": bool(exact) or bool(confirmed),
    }


def check_many(
    items: list[dict[str, Any]], *, exact_only: bool = False
) -> dict[str, dict[str, Any]]:
    """Bulk variant for the library grid, keyed by sha256.

    Written separately from :func:`check` because the naive loop is a scaling
    trap: one full scan of the history per tile meant ~0.9s for a 200-image page
    once a few thousand images had been posted, and it grows from there.

    Three stages, because they have very different costs. Exact file and pixel
    matches are indexed bulk queries. Perceptual candidates are fetched by at
    most eight indexed band queries for the whole set, not one query per image.

    ``exact_only`` stops after the second stage. The archive plan needs it: it
    accepts identical bytes or identical pixels and nothing else, so computing
    perceptual distances for every image in the library only to discard them
    cost 1.6 million comparisons per run.
    """
    wanted = [item for item in items if item.get("sha256")]
    if not wanted:
        return {}

    conn = db.get_connection()
    shas = list(dict.fromkeys(item["sha256"] for item in wanted))
    by_sha: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(shas), 400):
        chunk = shas[start : start + 400]
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT * FROM image_usage WHERE status != ?"
            f" AND sha256 IN ({placeholders}) ORDER BY used_at DESC",
            (WITHDRAWN, *chunk),
        ):
            by_sha.setdefault(row["sha256"], []).append(dict(row))

    result: dict[str, dict[str, Any]] = {}
    for item in wanted:
        exact = [
            _classified(row, item["sha256"], item.get("pixel_sha256"), distance=0)
            for row in by_sha.get(item["sha256"], [])[:10]
        ]
        result[item["sha256"]] = {
            "exact": exact,
            "similar": [],
            "confirmed": [],
            "has_exact": bool(exact),
            "has_similar": False,
            "has_certain": bool(exact),
        }
    pixel_hashes = list(
        dict.fromkeys(
            item["pixel_sha256"]
            for item in wanted
            if item.get("pixel_sha256") and not result[item["sha256"]]["has_exact"]
        )
    )
    by_pixels: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(pixel_hashes), 400):
        chunk = pixel_hashes[start : start + 400]
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT * FROM image_usage WHERE status != ?"
            f" AND pixel_sha256 IN ({placeholders}) ORDER BY used_at DESC",
            (WITHDRAWN, *chunk),
        ):
            by_pixels.setdefault(row["pixel_sha256"], []).append(dict(row))

    if by_pixels:
        for item in wanted:
            if result[item["sha256"]]["has_exact"] or not item.get("pixel_sha256"):
                continue
            matches = [
                _classified(
                    row,
                    item["sha256"],
                    item.get("pixel_sha256"),
                    distance=0,
                )
                for row in by_pixels.get(item["pixel_sha256"], [])
                if row["sha256"] != item["sha256"]
            ][:10]
            if not matches:
                continue
            result[item["sha256"]].update(
                similar=matches,
                confirmed=matches,
                has_similar=True,
                has_certain=True,
            )

    needs_similar = (
        []
        if exact_only
        else [
            item
            for item in wanted
            if not result[item["sha256"]]["has_certain"] and item.get("phash")
        ]
    )

    if not needs_similar:
        return result

    limit = threshold()
    hits: dict[str, list[tuple[int, int]]] = {}
    history_candidates: dict[int, dict[str, Any]] = {}
    candidate_ids: dict[str, set[int]] = {
        item["sha256"]: set() for item in needs_similar
    }

    if limit < BANDS:
        band_buckets: list[dict[int, set[str]]] = [{} for _ in range(BANDS)]
        for item in needs_similar:
            bands = _bands(item["phash"])
            if bands[0] is None:
                continue
            for index, value in enumerate(bands):
                if value is not None:
                    band_buckets[index].setdefault(value, set()).add(item["sha256"])

        # Each band has only 256 possible values, so these stay below SQLite's
        # parameter limit even for a library containing many thousands of rows.
        for index, buckets in enumerate(band_buckets):
            values = sorted(buckets)
            if not values:
                continue
            placeholders = ",".join("?" * len(values))
            for row in conn.execute(
                f"SELECT id, sha256, pixel_sha256, phash, phash_b{index}"
                f" FROM image_usage WHERE status != ?"
                f" AND phash_b{index} IN ({placeholders})",
                (WITHDRAWN, *values),
            ):
                entry = dict(row)
                history_candidates[entry["id"]] = entry
                for sha in buckets.get(entry[f"phash_b{index}"], ()):
                    if entry["sha256"] != sha:
                        candidate_ids[sha].add(entry["id"])
    else:
        # Above seven bits the band guarantee no longer holds. Load history once
        # for the complete batch rather than repeating a full query per image.
        history_candidates = {
            row["id"]: dict(row)
            for row in conn.execute(
                "SELECT id, sha256, pixel_sha256, phash FROM image_usage"
                " WHERE status != ? AND phash IS NOT NULL",
                (WITHDRAWN,),
            )
        }
        all_ids = set(history_candidates)
        candidate_ids = {item["sha256"]: all_ids for item in needs_similar}

    for item in needs_similar:
        found = []
        for row_id in candidate_ids[item["sha256"]]:
            row = history_candidates[row_id]
            if row["sha256"] == item["sha256"]:
                continue
            distance = hamming(item["phash"], row["phash"])
            if distance <= limit:
                found.append((row_id, distance))
        if found:
            found.sort(key=lambda pair: pair[1])
            hits[item["sha256"]] = found[:10]

    if hits:
        ids = sorted({row_id for pairs in hits.values() for row_id, _ in pairs})
        rows = {}
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            placeholders = ",".join("?" * len(chunk))
            rows.update(
                {
                    row["id"]: dict(row)
                    for row in conn.execute(
                        f"SELECT * FROM image_usage WHERE id IN ({placeholders})",
                        chunk,
                    )
                }
            )
        wanted_by_sha = {item["sha256"]: item for item in wanted}
        for sha, pairs in hits.items():
            item = wanted_by_sha.get(sha, {})
            similar = []
            for row_id, distance in pairs:
                row = rows.get(row_id)
                if not row:
                    continue
                entry = _classified(
                    row,
                    item.get("sha256"),
                    item.get("pixel_sha256"),
                    distance=distance,
                )
                similar.append(entry)
            similar.sort(key=lambda entry: (entry["confidence"] != CERTAIN, entry["distance"]))
            confirmed = [row for row in similar if row["confidence"] == CERTAIN]
            result[sha]["similar"] = similar
            result[sha]["confirmed"] = confirmed
            result[sha]["has_similar"] = bool(similar)
            result[sha]["has_certain"] = result[sha]["has_exact"] or bool(confirmed)

    return result
