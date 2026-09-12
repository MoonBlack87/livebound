"""Finding the same picture twice inside the library.

A different question from the one ``store/usage.py`` answers. That one asks "was
this published before?" and looks into the history; this one asks "is this file
lying here twice?" and looks at the library against itself.

Two answers, the same rule the duplicate warning uses:

- **certain** - matching non-empty file SHA-256 or decoded-pixel SHA-256. A
  metadata edit or lossless container change rewrites the file without touching
  a pixel.
- **probable** - the pictures look alike. A series of
  near-identical portraits out of one LoRA lands here, and that is why nothing is
  ever deleted without being asked.

Candidates come from the banded index on ``images`` (see ``phash_bands``), so
this is eight indexed probes per image rather than a pass over every pair.
"""

from __future__ import annotations

import re
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import config, db, jobs, paths, relocate, security
from .hashing import hamming
from .store import images as image_store
from .store import usage

CERTAIN = usage.CERTAIN
PROBABLE = usage.PROBABLE
_MAX_ASPECT_RATIO_DIFFERENCE = 0.25
_TIME_TOKENS = {
    "yyyy": ("year", r"\d{4}"),
    "mm": ("month", r"\d{2}"),
    "dd": ("day", r"\d{2}"),
    "HH": ("hour", r"\d{2}"),
    "MM": ("minute", r"\d{2}"),
    "SS": ("second", r"\d{2}"),
}
_REQUIRED_TIME_TOKENS = tuple(_TIME_TOKENS)


def exact_groups(
    limit: int | None = None, *, image_ids: list[int] | None = None
) -> list[dict[str, Any]]:
    """Files with identical pixels or bytes, grouped by the strongest answer."""
    if image_ids is not None:
        return _exact_groups_from_rows(_candidate_rows(image_ids), limit)
    conn = db.get_connection()
    archive_root_ids = _archive_root_ids()
    dismissed_pairs = _dismissed_pairs()
    pixel_rows = conn.execute(
        """
        SELECT pixel_sha256, COUNT(*) AS n FROM images
        WHERE is_missing=0 AND is_trashed=0
          AND pixel_sha256 IS NOT NULL AND pixel_sha256 != ''
        GROUP BY pixel_sha256 HAVING n > 1
        ORDER BY n DESC, pixel_sha256
        """
    ).fetchall()
    groups: list[dict[str, Any]] = []
    covered_ids: set[int] = set()
    for row in pixel_rows:
        members = _members(
            "SELECT * FROM images WHERE is_missing=0 AND is_trashed=0 AND pixel_sha256=?",
            (row["pixel_sha256"],),
            archive_root_ids,
        )
        base_key = f"pixel:{row['pixel_sha256']}"
        for group in _split_exact_groups(base_key, members, dismissed_pairs):
            groups.append(group)
            covered_ids.update(member["id"] for member in group["members"])
            if limit and len(groups) >= limit:
                return groups

    byte_rows = conn.execute(
        """
        SELECT sha256, COUNT(*) AS n FROM images
        WHERE is_missing=0 AND is_trashed=0 AND sha256 IS NOT NULL AND sha256 != ''
        GROUP BY sha256 HAVING n > 1
        ORDER BY n DESC, sha256
        """
    ).fetchall()
    for row in byte_rows:
        members = _members(
            "SELECT * FROM images WHERE is_missing=0 AND is_trashed=0 AND sha256=?",
            (row["sha256"],),
            archive_root_ids,
        )
        base_key = f"sha:{row['sha256']}"
        for group in _split_exact_groups(
            base_key, members, dismissed_pairs, reason="identical bytes"
        ):
            if all(member["id"] in covered_ids for member in group["members"]):
                continue
            groups.append(group)
            if limit and len(groups) >= limit:
                return groups
    return groups


def _exact_groups_from_rows(
    rows: list[dict[str, Any]], limit: int | None
) -> list[dict[str, Any]]:
    """Exact groups for a bounded set of indexed search candidates."""
    dismissed_pairs = _dismissed_pairs()
    pixel_members: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        digest = row.get("pixel_sha256")
        if digest:
            pixel_members.setdefault(digest, []).append(row)

    groups: list[dict[str, Any]] = []
    covered_ids: set[int] = set()
    ordered_pixels = sorted(pixel_members.items(), key=lambda item: (-len(item[1]), item[0]))
    for digest, members in ordered_pixels:
        base_key = f"pixel:{digest}"
        for group in _split_exact_groups(base_key, members, dismissed_pairs):
            groups.append(group)
            covered_ids.update(member["id"] for member in group["members"])
            if limit and len(groups) >= limit:
                return groups

    byte_members: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        digest = row.get("sha256")
        if digest:
            byte_members.setdefault(digest, []).append(row)
    ordered_bytes = sorted(byte_members.items(), key=lambda item: (-len(item[1]), item[0]))
    for digest, members in ordered_bytes:
        base_key = f"sha:{digest}"
        for group in _split_exact_groups(
            base_key, members, dismissed_pairs, reason="identical bytes"
        ):
            if all(member["id"] in covered_ids for member in group["members"]):
                continue
            groups.append(group)
            if limit and len(groups) >= limit:
                return groups
    return groups


def similar_groups(
    job: jobs.Job | None = None,
    limit: int | None = None,
    *,
    exact: list[dict[str, Any]] | None = None,
    image_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Files whose pixels look the same but whose bytes do not.

    Each exact group retains its oldest member as an anchor. Its other members
    are omitted here because the exact group already explains them, while the
    anchor keeps lossy or resized lookalikes visible for human comparison.
    """
    conn = db.get_connection()
    archive_root_ids = _archive_root_ids()
    exact = exact_groups(image_ids=image_ids) if exact is None else exact
    collapsed_ids = {
        member["id"]
        for group in exact
        for member in group["members"][1:]
    }
    if image_ids is None:
        rows = [
            _row(row)
            for row in conn.execute(
                "SELECT * FROM images"
                " WHERE is_missing=0 AND is_trashed=0 AND phash IS NOT NULL AND phash != ''"
            )
        ]
        for row in rows:
            row["is_archive"] = row["source_root_id"] in archive_root_ids
    else:
        rows = [row for row in _candidate_rows(image_ids) if row.get("phash")]
    candidates = [row for row in rows if row["id"] not in collapsed_ids]
    if job is not None:
        job.total = len(candidates)

    threshold = usage.threshold()
    dismissed_pairs = _dismissed_pairs()
    by_id = {row["id"]: row for row in candidates}

    # Admissible pairs first, groups second. The walk used to grow a group from
    # whichever row it reached first and mark every member as taken, so an image
    # comparable to a later row but not to that seed was skipped - and once its
    # partners were taken it appeared in no group at all. Known shapes therefore
    # form connected components. Unknown shapes form their own components and
    # may attach to one known component, but never union two known, incompatible
    # shapes merely because they have no dimensions with which to contradict
    # either one.
    union = _Union(row["id"] for row in candidates)
    unknown_to_known: list[tuple[int, int]] = []
    bands = _band_index(candidates)
    for index, row in enumerate(candidates):
        if job is not None:
            job.processed = index + 1
            if job.should_stop():
                break
        for other_id in _candidates_for(row, threshold, by_id, bands):
            # Each pair once: the comparison is symmetric and so is the union.
            if other_id <= row["id"]:
                continue
            other = by_id.get(other_id)
            if other is None:
                continue
            if hamming(row["phash"], other["phash"]) > threshold:
                continue
            if not _comparable_dimensions(row, other):
                continue
            if _pair(row["id"], other["id"]) in dismissed_pairs:
                continue
            row_known = _has_known_dimensions(row)
            other_known = _has_known_dimensions(other)
            if row_known == other_known:
                union.join(row["id"], other["id"])
            else:
                unknown_to_known.append(
                    (row["id"], other["id"])
                    if not row_known
                    else (other["id"], row["id"])
                )

    known_components: dict[int, list[dict[str, Any]]] = {}
    unknown_components: dict[int, list[dict[str, Any]]] = {}
    for row in candidates:
        target = known_components if _has_known_dimensions(row) else unknown_components
        target.setdefault(union.root(row["id"]), []).append(row)

    attachment_targets: dict[int, set[int]] = {}
    for unknown_id, known_id in unknown_to_known:
        attachment_targets.setdefault(union.root(unknown_id), set()).add(
            union.root(known_id)
        )

    unattached_unknown: list[list[dict[str, Any]]] = []
    for root, members in unknown_components.items():
        targets = attachment_targets.get(root, set())
        if not targets:
            unattached_unknown.append(members)
            continue
        chosen = min(
            targets,
            key=lambda target: min(member["id"] for member in known_components[target]),
        )
        known_components[chosen].extend(members)

    components = [*known_components.values(), *unattached_unknown]

    groups: list[dict[str, Any]] = []
    for members in components:
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda item: (item["first_seen_at"], item["id"]))
        anchor = ordered[0]
        groups.append(
            _similar_group(
                f"phash:{anchor['phash']}:{anchor['id']}",
                _group_confidence(ordered),
                ordered,
            )
        )
        if limit and len(groups) >= limit:
            return groups[:limit]
    return groups


class _Union:
    """Disjoint sets over image ids, so grouping does not depend on row order."""

    def __init__(self, ids):
        self._parent = {value: value for value in ids}

    def root(self, value: int) -> int:
        parent = self._parent
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def join(self, first: int, second: int) -> None:
        left, right = self.root(first), self.root(second)
        if left != right:
            self._parent[right] = left


def library_groups(image_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Disjoint duplicate groups for one-tile-per-picture presentation.

    The duplicate report keeps one exact member as the comparison anchor for a
    probable group.  That overlap is useful there, but would make the same file
    appear in two library stacks.  Merge only those overlapping presentation
    groups and retain the least certain confidence found in the component.
    """
    exact = exact_groups(image_ids=image_ids)
    groups = exact + similar_groups(exact=exact, image_ids=image_ids)
    dismissed_pairs = _dismissed_pairs()
    merged: list[dict[str, Any]] = []
    for group in groups:
        incoming = {**group, "members": list(group["members"])}
        member_ids = {member["id"] for member in incoming["members"]}
        combined_ids = set(member_ids)
        overlapping: list[int] = []
        for index, candidate in enumerate(merged):
            candidate_ids = {member["id"] for member in candidate["members"]}
            shared_ids = member_ids.intersection(candidate_ids)
            if not shared_ids:
                continue
            if _contains_dismissed_pair(combined_ids | candidate_ids, dismissed_pairs):
                # A member claimed by two incompatible stacks stays with the
                # first one. The incoming group loses it and, below two, drops
                # away so its other member becomes an ordinary library tile.
                incoming["members"] = [
                    member
                    for member in incoming["members"]
                    if member["id"] not in shared_ids
                ]
                member_ids.difference_update(shared_ids)
                combined_ids.difference_update(shared_ids)
                continue
            overlapping.append(index)
            combined_ids.update(candidate_ids)

        if len(incoming["members"]) < 2:
            continue
        if not overlapping:
            merged.append(incoming)
            continue

        first = overlapping[0]
        combined = [merged[index] for index in overlapping] + [incoming]
        members = {
            member["id"]: member
            for candidate in combined
            for member in candidate["members"]
        }
        merged[first] = {
            **merged[first],
            "key": f"library:{min(members)}",
            "confidence": (
                PROBABLE
                if any(candidate["confidence"] == PROBABLE for candidate in combined)
                else CERTAIN
            ),
            "reason": (
                "same picture"
                if any(candidate["confidence"] == PROBABLE for candidate in combined)
                else merged[first]["reason"]
            ),
            "archive_duplicate": all(
                candidate["archive_duplicate"] for candidate in combined
            ),
            "members": list(members.values()),
        }
        for index in reversed(overlapping[1:]):
            del merged[index]
    return merged


def _contains_dismissed_pair(
    image_ids: set[int], dismissed_pairs: set[tuple[int, int]]
) -> bool:
    return any(first in image_ids and second in image_ids for first, second in dismissed_pairs)


def run(job: jobs.Job) -> dict[str, Any]:
    """Both passes, as a job so the search reports progress."""
    job.stage = "Looking for exact copies"
    exact = exact_groups()
    job.stage = "Looking for pictures that match"
    similar = similar_groups(job, exact=exact)
    candidate_ids = list(
        dict.fromkeys(member["id"] for group in exact + similar for member in group["members"])
    )
    reconciled = image_store.reconcile_missing_candidates(candidate_ids)
    surviving_ids = [image_id for image_id in candidate_ids if image_id not in reconciled]
    exact = exact_groups(image_ids=surviving_ids)
    similar = similar_groups(exact=exact, image_ids=surviving_ids)
    groups = _with_post_bindings(exact + similar)
    job.stage = "Done"
    job.result = {"groups": groups}
    return {
        "exact": len(exact),
        "similar": len(similar),
        "files": sum(len(group["members"]) for group in groups),
    }


def current_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter a finished search snapshot against the rows still in the library."""
    image_ids = list(
        dict.fromkeys(member["id"] for group in groups for member in group["members"])
    )
    if not image_ids:
        return []
    existing: dict[int, bool] = {}
    conn = db.get_connection()
    for start in range(0, len(image_ids), 500):
        chunk = image_ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        existing.update(
            {
                row["id"]: bool(row["is_archive"])
                for row in conn.execute(
                    f"SELECT i.id, r.is_archive FROM images i"
                    " JOIN source_roots r ON r.id=i.source_root_id"
                    f" WHERE i.id IN ({placeholders})"
                    " AND i.is_missing=0 AND i.is_trashed=0",
                    chunk,
                )
            }
        )
    current = []
    dismissed_pairs = _dismissed_pairs()
    for group in groups:
        members = [member for member in group["members"] if member["id"] in existing]
        for index, member_group in enumerate(
            _groups_without_dismissed_pairs(members, dismissed_pairs)
        ):
            ordered, first_seen_by = _order_members(member_group)
            current.append(
                {
                    **group,
                    "key": _split_group_key(group["key"], member_group, index),
                    "archive_duplicate": all(
                        existing[member["id"]] for member in member_group
                    ),
                    "members": ordered,
                    "first_seen_by": first_seen_by,
                }
            )
    return _with_post_bindings(current)


def _with_post_bindings(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark reported members held by a post without querying once per image."""
    image_ids = list(
        dict.fromkeys(member["id"] for group in groups for member in group["members"])
    )
    bound = post_bound_image_ids(image_ids)
    for group in groups:
        for member in group["members"]:
            member["post_bound"] = member["id"] in bound
    return groups


def post_bound_image_ids(image_ids: list[int]) -> set[int]:
    """Return selected images that a local post still holds.

    The same question the library asks before building another post out of a
    selection, so it is the same query: one place to be right about, and one
    place to change when a post can hold an image in a new way.
    """
    from .store import posts as post_store

    return set(post_store.posts_holding_images(image_ids))


def dismiss(image_ids: list[int]) -> int:
    """Persist every canonical pair in a small selected set."""
    unique_ids = sorted(set(image_ids))
    if len(unique_ids) < 2:
        raise ValueError("Choose at least two different images.")
    pairs = list(combinations(unique_ids, 2))
    dismissed_at = db.now_iso()
    with db.transaction() as conn:
        existing_ids = {
            row["id"]
            for row in conn.execute(
                f"SELECT id FROM images WHERE id IN ({','.join('?' for _ in unique_ids)})",
                unique_ids,
            )
        }
        if existing_ids != set(unique_ids):
            raise LookupError("One or more selected images no longer exist.")
        changes_before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO duplicate_dismissals"
            " (image_id_a, image_id_b, dismissed_at) VALUES(?,?,?)",
            [(first, second, dismissed_at) for first, second in pairs],
        )
        return conn.total_changes - changes_before


def _dismissed_pairs() -> set[tuple[int, int]]:
    return {
        (row["image_id_a"], row["image_id_b"])
        for row in db.get_connection().execute(
            "SELECT image_id_a, image_id_b FROM duplicate_dismissals"
        )
    }


def _without_dismissed_pairs(
    members: list[dict[str, Any]], dismissed_pairs: set[tuple[int, int]]
) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for member in members:
        if any(_pair(member["id"], other["id"]) in dismissed_pairs for other in kept):
            continue
        kept.append(member)
    return kept


def _groups_without_dismissed_pairs(
    members: list[dict[str, Any]], dismissed_pairs: set[tuple[int, int]]
) -> list[list[dict[str, Any]]]:
    """Repeat the pair filter so a rejected member can anchor another group."""
    remaining = sorted(members, key=lambda member: member["id"])
    groups: list[list[dict[str, Any]]] = []
    while len(remaining) >= 2:
        kept = _without_dismissed_pairs(remaining, dismissed_pairs)
        if len(kept) >= 2:
            groups.append(kept)
        if len(kept) == len(remaining):
            break
        remaining = remaining[1:]
    return groups


def _split_exact_groups(
    base_key: str,
    members: list[dict[str, Any]],
    dismissed_pairs: set[tuple[int, int]],
    *,
    reason: str | None = None,
) -> list[dict[str, Any]]:
    groups = _groups_without_dismissed_pairs(members, dismissed_pairs)
    return [
        _group(
            _split_group_key(base_key, member_group, index),
            _group_confidence(member_group),
            reason or _exact_reason(member_group),
            member_group,
        )
        for index, member_group in enumerate(groups)
    ]


def _split_group_key(
    base_key: str, members: list[dict[str, Any]], index: int
) -> str:
    return base_key if index == 0 else f"{base_key}:{min(member['id'] for member in members)}"


def _pair(first: int, second: int) -> tuple[int, int]:
    return (first, second) if first < second else (second, first)


def _band_index(rows: list[dict[str, Any]]) -> list[dict[int, list[int]]]:
    """Which candidates share each 8-bit band value, built once for the set.

    The lookup used to be eight indexed queries per row - thirty thousand
    queries for a library of four thousand images, on every library page load.
    Same shape as ``store/usage.py::check_many``, which builds its buckets in
    memory for exactly this reason.
    """
    buckets: list[dict[int, list[int]]] = [{} for _ in range(usage.BANDS)]
    for row in rows:
        for band in range(usage.BANDS):
            value = row.get(f"phash_b{band}")
            if value is not None:
                buckets[band].setdefault(value, []).append(row["id"])
    return buckets


def _candidates_for(
    row: dict[str, Any],
    threshold: int,
    by_id: dict[int, dict[str, Any]],
    bands: list[dict[int, list[int]]],
) -> set[int]:
    """Ids worth comparing against ``row``.

    The banded index only narrows the field while the pigeonhole guarantee
    holds: two 64-bit hashes within Hamming distance <= 7 must agree on one of
    the eight 8-bit bands. Above that they need not, so a loose threshold has to
    compare everything or it silently reports fewer duplicates than exist - and
    the threshold is user-settable up to 32.
    """
    if threshold >= usage.BANDS:
        return set(by_id)
    found: set[int] = set()
    for band, buckets in enumerate(bands):
        value = row.get(f"phash_b{band}")
        if value is not None:
            found.update(buckets.get(value, ()))
    return found


def _comparable_dimensions(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """A perceptual fingerprint cannot overrule plainly different shapes."""
    if not (_has_known_dimensions(first) and _has_known_dimensions(second)):
        # A scan normally fills both values from the image container.  A
        # legacy or otherwise incomplete row has no shape evidence, so it
        # cannot contradict the perceptual match; only two known shapes can.
        return True
    first_ratio = first["width"] / first["height"]
    second_ratio = second["width"] / second["height"]
    return max(first_ratio, second_ratio) <= min(first_ratio, second_ratio) * (
        1 + _MAX_ASPECT_RATIO_DIFFERENCE
    )


def _has_known_dimensions(row: dict[str, Any]) -> bool:
    return all(
        isinstance(value, int) and value > 0
        for value in (row.get("width"), row.get("height"))
    )


def _group_confidence(members: list[dict[str, Any]]) -> str:
    """Classify a group from the shared exact-byte-or-pixel rule."""
    first = members[0]
    return (
        CERTAIN
        if all(
            usage.match_confidence(
                first.get("sha256"), first.get("pixel_sha256"), member
            )
            == CERTAIN
            for member in members[1:]
        )
        else PROBABLE
    )


def _exact_reason(members: list[dict[str, Any]]) -> str:
    """Name the strongest non-empty exact hash shared by the group."""
    file_hashes = [member.get("sha256") for member in members]
    return (
        "identical bytes"
        if all(file_hashes) and len(set(file_hashes)) == 1
        else "identical pixels"
    )


def _similar_group(
    key: str, confidence: str, members: list[dict[str, Any]]
) -> dict[str, Any]:
    return _group(key, confidence, "same picture", members)


def _group(
    key: str, confidence: str, reason: str, members: list[dict[str, Any]]
) -> dict[str, Any]:
    patterns = _generation_time_patterns()
    ordered, first_seen_by = _order_members(members, patterns)
    return {
        "key": key,
        "confidence": confidence,
        "reason": reason,
        "archive_duplicate": bool(members) and all(member["is_archive"] for member in members),
        "first_seen_by": first_seen_by,
        "members": [_present(member, patterns) for member in ordered],
    }


def _members(
    sql: str, params: tuple[Any, ...], archive_root_ids: set[int]
) -> list[dict[str, Any]]:
    members = [_row(row) for row in db.get_connection().execute(sql, params)]
    for member in members:
        member["is_archive"] = member["source_root_id"] in archive_root_ids
    return members


def _candidate_rows(image_ids: list[int]) -> list[dict[str, Any]]:
    """Load only live library rows from an already bounded search result."""
    unique_ids = list(dict.fromkeys(image_ids))
    if not unique_ids:
        return []
    archive_root_ids = _archive_root_ids()
    rows: list[dict[str, Any]] = []
    conn = db.get_connection()
    for start in range(0, len(unique_ids), 500):
        chunk = unique_ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(
            _row(row)
            for row in conn.execute(
                f"SELECT * FROM images WHERE id IN ({placeholders})"
                " AND is_missing=0 AND is_trashed=0",
                chunk,
            )
        )
    for row in rows:
        row["is_archive"] = row["source_root_id"] in archive_root_ids
    rows.sort(key=lambda row: row["id"])
    return rows


def _archive_root_ids() -> set[int]:
    return {
        row["id"]
        for row in db.get_connection().execute(
            "SELECT id FROM source_roots WHERE is_archive=1"
        )
    }


def _order_members(
    members: list[dict[str, Any]],
    patterns: list[re.Pattern[str]] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Apply IMG-11's all-or-nothing filename-time rule to one group."""
    compiled = _generation_time_patterns() if patterns is None else patterns
    timed = [
        (generation_time(member["absolute_path"], compiled), member) for member in members
    ]
    if timed and all(value is not None for value, _ in timed):
        return (
            [
                member
                for _, member in sorted(
                    timed,
                    key=lambda item: (
                        item[0] or datetime.max,
                        item[1]["first_seen_at"],
                        item[1]["id"],
                    ),
                )
            ],
            "generation_time",
        )
    return (
        sorted(members, key=lambda item: (item["first_seen_at"], item["id"])),
        "library_entry",
    )


def generation_time(
    filename: str, patterns: list[re.Pattern[str]] | None = None
) -> datetime | None:
    """Read a generation timestamp from a filename using configured masks."""
    stem = Path(filename).stem
    compiled_patterns = _generation_time_patterns() if patterns is None else patterns
    for compiled in compiled_patterns:
        match = compiled.fullmatch(stem)
        if match is None:
            continue
        try:
            return datetime(**{name: int(match.group(name)) for name, _ in _TIME_TOKENS.values()})
        except ValueError:
            continue
    return None


def _generation_time_patterns() -> list[re.Pattern[str]]:
    configured = db.get_json_setting(
        "generation_time_patterns", list(config.DEFAULT_GENERATION_TIME_PATTERNS)
    )
    if not isinstance(configured, list):
        return []
    return [
        compiled
        for pattern in configured
        if isinstance(pattern, str)
        and (compiled := compile_generation_time_pattern(pattern)) is not None
    ]


def compile_generation_time_pattern(pattern: str) -> re.Pattern[str] | None:
    """Compile the small literal mask language used in settings.

    Each date/time token must occur exactly once. ``seed`` is optional and
    matches digits; every other character is literal, so a setting cannot turn
    into an unbounded regular expression.
    """
    if not pattern or len(pattern) > 200:
        return None
    parts: list[str] = []
    seen: set[str] = set()
    index = 0
    tokens = (*_TIME_TOKENS, "seed")
    while index < len(pattern):
        token = next((value for value in tokens if pattern.startswith(value, index)), None)
        if token is None:
            parts.append(re.escape(pattern[index]))
            index += 1
            continue
        if token in seen:
            return None
        seen.add(token)
        if token == "seed":
            parts.append(r"\d+")
        else:
            name, expression = _TIME_TOKENS[token]
            parts.append(f"(?P<{name}>{expression})")
        index += len(token)
    if not all(token in seen for token in _REQUIRED_TIME_TOKENS):
        return None
    try:
        return re.compile("".join(parts))
    except re.error:
        return None


def _row(row: Any) -> dict[str, Any]:
    import json

    value = dict(row)
    raw = value.pop("parsed_json", None)
    try:
        value["parsed"] = json.loads(raw) if raw else None
    except ValueError:
        value["parsed"] = None
    return value


def _present(
    row: dict[str, Any], patterns: list[re.Pattern[str]] | None = None
) -> dict[str, Any]:
    """Just enough to compare two files by eye and decide which one to keep."""
    return {
        "id": row["id"],
        "absolute_path": row["absolute_path"],
        "relative_path": row["relative_path"],
        "folder": row["folder"],
        "file_size": row["file_size"],
        "file_mtime": row["file_mtime"],
        "width": row["width"],
        "height": row["height"],
        "sha256": row["sha256"],
        "phash": row["phash"],
        "thumbnail_path": row.get("thumbnail_path"),
        "seed": usage.generation_identity(row.get("parsed")).get("seed"),
        "first_seen_at": row["first_seen_at"],
        "generation_time": (
            value.isoformat(timespec="seconds")
            if (value := generation_time(row["absolute_path"], patterns)) is not None
            else None
        ),
        "post_bound": False,
    }


def archive_duplicate_image_ids(image_ids: list[int]) -> set[int]:
    """Selected rows that belong to a duplicate group wholly in the archive."""
    requested = set(image_ids)
    if not requested:
        return set()
    archive_root_ids = _archive_root_ids()
    selected_archived = {
        image["id"]
        for image in image_store.get_many(list(requested))
        if image["source_root_id"] in archive_root_ids
    }
    if not selected_archived:
        return set()

    exact = exact_groups()
    groups = exact + similar_groups(exact=exact)
    protected = {
        member["id"]
        for group in groups
        if group["archive_duplicate"]
        for member in group["members"]
    }
    return selected_archived.intersection(protected)


def move_to_trash(
    image_ids: list[int],
    *,
    with_sidecars: bool = True,
    job: jobs.Job | None = None,
) -> dict[str, Any]:
    """Move selected library files into the configured global trash folder."""
    folder = require_trash_folder()
    origins = _trashed_origins(folder)
    post_bound = post_bound_image_ids(image_ids)
    images = [
        image for image in image_store.get_many(image_ids) if image["id"] not in post_bound
    ]
    if job is not None:
        job.total = len(images)

    moved, sidecars, failed = 0, 0, []
    for image in images:
        if job is not None and job.should_stop():
            break

        source = Path(image["absolute_path"])
        if job is not None:
            job.stage = source.name

        reason: str | None = None
        result: dict[str, Any] | None = None
        if image.get("is_trashed"):
            reason = "The file is already in the trash."
        else:
            try:
                security.serveable(source)
                target = _trash_target(folder, source, origins)
                # Two files of one batch must see each other, or a second
                # same-named parent would land in the first one's folder.
                origins.setdefault(target.parent, set()).add(source.parent)
                result = relocate.move_image(
                    image["id"],
                    source,
                    target,
                    with_sidecars=with_sidecars,
                    mark_trashed=True,
                )
            except HTTPException as exc:
                reason = str(exc.detail)
            except Exception as exc:
                # One file's failure must not abort a selected batch; every other
                # member can still be moved safely and gets its own result.
                reason = str(exc)

        if reason is not None:
            failure = {"image_id": image["id"], "path": str(source), "reason": reason}
            failed.append(failure)
            if job is not None:
                job.failed += 1
                job.processed += 1
                job.log(
                    image_id=image["id"],
                    path=str(source),
                    status="error",
                    message=reason,
                )
        else:
            moved += 1
            sidecars += (result or {}).get("sidecars", 0)
            if job is not None:
                job.succeeded += 1
                job.processed += 1
                job.log(
                    image_id=image["id"],
                    path=str(source),
                    status="ok",
                    target=(result or {}).get("target"),
                )

    processed = job.processed if job is not None else len(images)
    aggregate = {
        "moved": moved,
        "sidecars": sidecars,
        "failed": failed,
        "post_bound_image_ids": sorted(post_bound),
        "unstarted_image_ids": [image["id"] for image in images[processed:]],
    }
    if job is not None:
        job.result = aggregate
        job.stage = ""
    return aggregate


def require_trash_folder() -> Path:
    """Return the configured reachable trash folder or explain how to fix it."""
    folder = trash_folder()
    if folder is None:
        raise ValueError("Configure the trash folder in Settings before moving files.")
    if not folder.is_dir():
        raise ValueError(
            f"The trash folder is not available: {folder}. Reconnect it or choose another folder."
        )
    return folder


def restore_from_trash(image_ids: list[int]) -> dict[str, Any]:
    """Put trashed files back at their exact original paths."""
    restored, sidecars, failed = 0, 0, []
    for image in image_store.get_many(image_ids):
        source = Path(image["absolute_path"])
        original = image.get("trash_original_path")
        if not image.get("is_trashed") or not original:
            failed.append({"path": str(source), "reason": "The file is not in the trash."})
            continue
        target = Path(original)
        root = image_store.source_root_path(image["source_root_id"])
        if root is None or not root.is_dir() or not _within(target, root):
            failed.append(
                {
                    "path": str(source),
                    "reason": (
                        "The original source folder is not available. "
                        "Reconnect it before restoring."
                    ),
                }
            )
            continue
        try:
            result = relocate.move_image(
                image["id"],
                source,
                target,
                exact_target=True,
                mark_trashed=False,
            )
        except Exception as exc:
            # Restore is a batch endpoint too; a collision or vanished file for
            # one row must not keep independent rows in the trash.
            failed.append({"path": str(source), "reason": str(exc)})
            continue
        restored += 1
        sidecars += result["sidecars"]
    return {"restored": restored, "sidecars": sidecars, "failed": failed}


def trash_folder() -> Path | None:
    raw = (db.get_setting("trash_folder") or "").strip()
    return Path(raw).resolve() if raw else None


def _trashed_origins(folder: Path) -> dict[Path, set[Path]]:
    """For each existing trash subfolder, the source folders it already holds.

    Read once per batch rather than once per file: a move of five hundred images
    would otherwise walk every trashed row five hundred times.
    """
    origins: dict[Path, set[Path]] = {}
    for row in db.get_connection().execute(
        "SELECT absolute_path, trash_original_path FROM images"
        " WHERE is_trashed=1 AND trash_original_path IS NOT NULL"
    ):
        here = Path(row["absolute_path"]).parent
        if here != folder and folder not in here.parents:
            continue
        origins.setdefault(here, set()).add(Path(row["trash_original_path"]).parent)
    return origins


def _trash_target(folder: Path, source: Path, origins: dict[Path, set[Path]]) -> Path:
    """Choose a trash subfolder that says where the file came from.

    The name is the source's parent folder. Where that name would merge two
    different source folders, it grows leftwards - `gen_output` instead of
    `output`, then `x_gen_output` - until it separates them. Mirrored trees on
    two drives are the case that needs the third component, and they are exactly
    where duplicates come from.

    Two source folders that are genuinely the same folder can never be separated
    this way; that collision is a file name in one folder and `relocate` settles
    it by numbering the file.
    """
    parent = source.parent
    if not parent.name:
        return folder / source.name

    parts = [part for part in parent.parts if part not in ("/", "\\")]
    for depth in range(1, len(parts) + 1):
        candidate = folder / "_".join(parts[-depth:])
        taken = origins.get(candidate)
        if not taken or taken == {parent}:
            return candidate / source.name
    return folder / "_".join(parts) / source.name



def _within(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    parent = root.resolve()
    return resolved == parent or parent in resolved.parents


def _planned_deletions(
    image_ids: list[int], *, with_sidecars: bool
) -> list[tuple[dict[str, Any], Path, list[Path]]]:
    """Resolve every image and sidecar before permanent deletion starts."""
    planned = []
    for image in image_store.get_many(image_ids):
        path = Path(image["absolute_path"])
        sidecars = paths.sidecars_for(path) if with_sidecars else []
        planned.append((image, path, sidecars))
    return planned


def deletion_plan(image_ids: list[int], *, with_sidecars: bool = True) -> list[str]:
    """Return the complete flat path list shown before permanent deletion."""
    return [
        str(planned_path)
        for _image, image_path, sidecars in _planned_deletions(
            image_ids, with_sidecars=with_sidecars
        )
        for planned_path in (image_path, *sidecars)
    ]


def delete_files(image_ids: list[int], *, with_sidecars: bool = True) -> dict[str, Any]:
    """Delete the files for good, and their rows with them.

    Bounded to the configured source folders by the same check that bounds file
    serving: a stale row must not turn this into a general file remover.

    The row goes too - the schema is built for it. ``post_images`` carries its
    own snapshot of the file and lets ``image_id`` fall to NULL, and
    ``image_usage`` is denormalised, so the record that these bytes were once
    published outlives the file.
    """
    deleted, deleted_sidecars, failed = 0, 0, []
    planned = _planned_deletions(image_ids, with_sidecars=with_sidecars)
    for image, path, sidecars in planned:
        if image.get("is_trashed"):
            failed.append(
                {
                    "path": str(path),
                    "reason": "Trash files are deleted manually outside the application.",
                }
            )
            continue
        try:
            security.serveable(path)
        except HTTPException as exc:
            # 404 and 403 are different answers to the user: "the drive is not
            # mounted" is not "you are trying to delete something you may not".
            failed.append({"path": str(path), "reason": str(exc.detail)})
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            failed.append({"path": str(path), "reason": str(exc)})
            continue
        image_store.delete(image["id"])
        deleted += 1
        for sidecar in sidecars:
            try:
                sidecar.unlink(missing_ok=True)
            except OSError as exc:
                failed.append({"path": str(sidecar), "reason": str(exc)})
                continue
            deleted_sidecars += 1
    return {"deleted": deleted, "sidecars": deleted_sidecars, "failed": failed}
