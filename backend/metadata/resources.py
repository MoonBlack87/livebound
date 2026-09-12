"""Derive model rows from parameter evidence, with prompt-side weights.

``Civitai resources`` and the model hash fields say which resources exist. A
prompt tag can only add its weight to one of those rows; it is never evidence by
itself. An exact hash-map key is direct evidence; an unambiguous credit weight
is the final fallback.
"""

from __future__ import annotations

import contextlib
import math
import re
from typing import Any, Literal

from . import infotext

CHECKPOINT = "checkpoint"
LORA = "lora"
EMBEDDING = "embedding"
UPSCALER = "upscaler"

#: CivitAI's own type strings, mapped onto ours.
_TYPE_ALIASES = {
    "checkpoint": CHECKPOINT,
    "model": CHECKPOINT,
    "lora": LORA,
    "lycoris": LORA,
    "locon": LORA,
    "embed": EMBEDDING,
    "embedding": EMBEDDING,
    "textualinversion": EMBEDDING,
    "upscaler": UPSCALER,
}

_MatchRung = Literal["hash", "version", "name"]


def normalize(name: str) -> str:
    """Lowercase alphanumerics only - the join key across all three representations."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


#: AutoV2 is the first 10 hex characters of the SHA256. Generators write 10, 12
#: or all 64 of them for the same file, so every hash comparison in this app goes
#: through this prefix - matching the raw strings would miss the majority.
HASH_PREFIX_LEN = 10


def hash_prefix(value: Any) -> str:
    """The common comparison key for a model hash. Empty string when there is none."""
    text = str(value or "").strip().upper()
    return text[:HASH_PREFIX_LEN] if text else ""


def extract(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Build resource rows from parameters, then attach prompt weights."""
    return _extract(parsed)[0]


def extract_with_prompt_links(
    parsed: dict[str, Any],
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    """Build rows and identify those linked to prompt tags."""
    rows, linked = _extract(parsed)
    return rows, {
        (str(row.get("resource_type") or ""), str(row.get("name_in_prompt") or ""))
        for index, row in enumerate(rows)
        if index in linked
    }


def _extract(parsed: dict[str, Any]) -> tuple[list[dict[str, Any]], set[int]]:
    fields = parsed.get("fields") or {}
    prompt = parsed.get("prompt") or ""

    credits = _civitai_entries(fields)
    out: list[dict[str, Any]] = []
    credit_rows: set[int] = set()

    checkpoint_name = str(fields.get("Model") or "")
    consolidated_model_hash = _lookup_hash(_hash_map(fields), "model")
    checkpoint_hash = consolidated_model_hash or fields.get("Model hash")
    checkpoint_credit = _single_credit(credits, CHECKPOINT)
    if checkpoint_name or checkpoint_hash or checkpoint_credit:
        checkpoint_label = (
            checkpoint_name
            or (checkpoint_credit or {}).get("modelName")
            or (checkpoint_credit or {}).get("modelVersionName")
            or "model"
        )
        out.append(
            _resource(
                CHECKPOINT,
                str(checkpoint_label),
                hash_value=checkpoint_hash,
                entry=checkpoint_credit,
            )
        )

    upscaler_name = str(fields.get("Hires upscaler") or "").strip()
    upscaler_credit = _single_credit(credits, UPSCALER)
    if upscaler_name:
        from ..store import model_roots

        out.append(
            _resource(
                UPSCALER,
                upscaler_name,
                hash_value=model_roots.hash_for_prompt_tag(upscaler_name),
                entry=upscaler_credit,
            )
        )

    # Several generators state the weight beside the hash instead of writing a
    # `<lora:name:0.8>` tag into the prompt the way A1111 does. Their weight is
    # evidence of the same kind, so it is taken here rather than waiting for a
    # tag that will never come.
    stated_weights = infotext.parse_hash_map(fields.get("Lora weights"))
    named_hashes = _named_hashes(fields)
    for kind, name, digest in named_hashes:
        key = (kind, normalize(name))
        if any((row["resource_type"], normalize(row["name_in_prompt"])) == key for row in out):
            continue
        row = _resource(kind, name, hash_value=digest)
        stated = _lookup_hash(stated_weights, name)
        if stated is not None:
            with contextlib.suppress(TypeError, ValueError):
                row["weight"] = float(stated)
        out.append(row)

    hashed_loras = {normalize(name) for kind, name, _digest in named_hashes if kind == LORA}
    for name, stated in stated_weights.items():
        if normalize(name) in hashed_loras or any(
            row["resource_type"] == LORA and normalize(row["name_in_prompt"]) == normalize(name)
            for row in out
        ):
            continue
        from ..store import model_roots

        row = _resource(
            LORA,
            name,
            hash_value=model_roots.hash_for_prompt_tag(name),
        )
        with contextlib.suppress(TypeError, ValueError):
            row["weight"] = float(stated)
        out.append(row)

    for entry in credits:
        if entry is checkpoint_credit or (entry is upscaler_credit and upscaler_name):
            continue
        kind = _TYPE_ALIASES.get(str(entry.get("type", "")).lower(), LORA)
        label = entry.get("modelName") or entry.get("modelVersionName") or "?"
        match = _matching_hash_row(out, kind, entry)
        if match is not None:
            existing, rung = match
            _apply_credit(out[existing], entry, rung)
            credit_rows.add(existing)
            continue
        out.append(_resource(kind, str(label), entry=entry))
        credit_rows.add(len(out) - 1)

    claimed: set[int] = set()
    for name, weight in infotext.lora_tags(prompt):
        direct = [
            index
            for index, row in enumerate(out)
            if index not in claimed
            and row.get("hash")
            and row["resource_type"] in (LORA, EMBEDDING)
            and normalize(row["name_in_prompt"]) == normalize(name)
        ]
        if len(direct) == 1:
            _attach_prompt(out[direct[0]], name, weight)
            claimed.add(direct[0])
            continue

        local = _local_credit(out, credit_rows, claimed, name)
        if local is not None:
            index, digest = local
            out[index]["hash"] = digest
            out[index]["hash_prefix"] = hash_prefix(digest)
            _attach_prompt(out[index], name, weight)
            claimed.add(index)
            continue

        weighted = [
            index
            for index in credit_rows
            if index not in claimed
            and out[index]["resource_type"] == LORA
            and isinstance(out[index].get("weight"), (int, float))
            and math.isclose(float(out[index]["weight"]), weight, abs_tol=1e-6)
        ]
        if len(weighted) == 1:
            _attach_prompt(out[weighted[0]], name, weight)
            claimed.add(weighted[0])

    return out, claimed


def _matching_hash_row(
    rows: list[dict[str, Any]], kind: str, entry: dict[str, Any]
) -> tuple[int, _MatchRung] | None:
    from ..resources import cache

    credit_prefix = hash_prefix(entry.get("hash"))
    version_id = entry.get("modelVersionId")
    credit_name = normalize(entry.get("modelName") or entry.get("modelVersionName"))
    candidates = [
        (index, row)
        for index, row in enumerate(rows)
        if row.get("resource_type") == kind and row.get("hash")
    ]
    # Compare every row at one confidence rung before trying the next. A name
    # on an earlier row must never beat an exact hash on a later one.
    if credit_prefix:
        for index, row in candidates:
            if row.get("hash_prefix") == credit_prefix:
                return index, "hash"
    if version_id is not None:
        for index, row in candidates:
            identity = cache.get(row["hash"])
            if (identity or {}).get("model_version_id") == version_id:
                return index, "version"
    if credit_name:
        for index, row in candidates:
            if normalize(row.get("name_in_prompt")) == credit_name:
                return index, "name"
    return None


def _apply_credit(
    resource: dict[str, Any], entry: dict[str, Any], rung: _MatchRung
) -> None:
    resource["weight"] = entry.get("weight")
    if rung == "name":
        resource["name_in_prompt"] = str(
            entry.get("modelName") or entry.get("modelVersionName")
        )
        return
    resource.update(
        {
            "model_id": entry.get("modelId"),
            "model_version_id": entry.get("modelVersionId"),
            "model_name": entry.get("modelName"),
            "version_name": entry.get("modelVersionName"),
            "resolved_from": "embedded",
        }
    )


def _local_credit(
    rows: list[dict[str, Any]],
    credit_rows: set[int],
    claimed: set[int],
    tag: str,
) -> tuple[int, str] | None:
    from ..resources import cache
    from ..store import model_roots

    eligible_versions = {
        rows[index].get("model_version_id")
        for index in credit_rows
        if index not in claimed and rows[index].get("model_version_id") is not None
    }
    if not eligible_versions:
        return None
    digest = model_roots.hash_for_prompt_tag(tag)
    identity = cache.get(digest) if digest else None
    version_id = (identity or {}).get("model_version_id")
    if version_id not in eligible_versions:
        return None
    matches = [
        index
        for index in credit_rows
        if index not in claimed and rows[index].get("model_version_id") == version_id
    ]
    return (matches[0], digest) if len(matches) == 1 else None


def _attach_prompt(resource: dict[str, Any], name: str, weight: float) -> None:
    resource["name_in_prompt"] = name
    resource["weight"] = weight


def _single_credit(
    credits: list[dict[str, Any]], kind: str
) -> dict[str, Any] | None:
    matches = [
        entry
        for entry in credits
        if _TYPE_ALIASES.get(str(entry.get("type", "")).lower()) == kind
    ]
    return matches[0] if len(matches) == 1 else None


def _resource(
    kind: str,
    name: str,
    *,
    hash_value: Any = None,
    entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    digest = str(hash_value).upper() if hash_value else None
    resource: dict[str, Any] = {
        "resource_type": kind,
        "name_in_prompt": name,
        "hash": digest,
        "hash_prefix": hash_prefix(digest),
        "weight": (entry or {}).get("weight"),
        "model_id": (entry or {}).get("modelId"),
        "model_version_id": (entry or {}).get("modelVersionId"),
        "model_name": (entry or {}).get("modelName"),
        "version_name": (entry or {}).get("modelVersionName"),
        "resolved_from": "embedded" if entry else "unresolved",
    }
    return resource


def _civitai_entries(fields: dict[str, Any]) -> list[dict[str, Any]]:
    raw = fields.get("Civitai resources")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _hash_map(fields: dict[str, Any]) -> dict[str, str]:
    value = fields.get("Hashes")
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {}


def _lookup_hash(mapping: dict[str, str], name: str) -> str | None:
    key = normalize(name)
    return next(
        (digest for raw_name, digest in mapping.items() if normalize(raw_name) == key),
        None,
    )


def _named_hashes(fields: dict[str, Any]) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    found.extend(
        (LORA, name, digest)
        for name, digest in infotext.parse_hash_map(fields.get("Lora hashes")).items()
    )
    found.extend(
        (EMBEDDING, name, digest)
        for name, digest in infotext.parse_hash_map(fields.get("TI hashes")).items()
    )
    for raw_name, digest in _hash_map(fields).items():
        prefix, separator, name = raw_name.partition(":")
        kind = {
            "lora": LORA,
            "embed": EMBEDDING,
            "embedding": EMBEDDING,
            "ti": EMBEDDING,
        }.get(prefix.lower())
        if separator and kind and name:
            found.append((kind, name, digest))
    return found
