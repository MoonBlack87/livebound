"""Project resource rows into the shared upload metadata model."""

from __future__ import annotations

from typing import Any

from . import infotext, resources

RESOURCE_FIELDS = (
    "Civitai resources",
    "Hashes",
    "Model hash",
    "Lora hashes",
    "Lora weights",
    "TI hashes",
)


def project(
    fields: dict[str, Any],
    resource_rows: list[dict[str, Any]],
    retained_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], tuple[dict[str, Any], ...]]:
    """Return canonical rows and their A1111 and CivitAI representations."""
    canonical_rows = _canonical_resources(fields, retained_rows)
    values = _rendered_fields(fields, resource_rows, canonical_rows)
    credits = tuple(
        credit for row in canonical_rows if (credit := _credit(row)) is not None
    )
    return canonical_rows, values, credits


def _canonical_resources(
    fields: dict[str, Any], retained_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    canonical: list[dict[str, Any]] = []
    for row in retained_rows:
        item = dict(row)
        digest = str(row.get("hash") or "")
        source_digest = _source_hash(fields, row)
        if (
            source_digest
            and resources.hash_prefix(source_digest) == resources.hash_prefix(digest)
        ):
            item["hash"] = source_digest
        else:
            item["hash"] = digest or None
        canonical.append(item)
    return canonical


def _source_hash(fields: dict[str, Any], row: dict[str, Any]) -> str | None:
    kind = row.get("resource_type")
    digest = str(row.get("hash") or "")
    if kind == resources.CHECKPOINT and fields.get("Model hash"):
        candidate = str(fields["Model hash"])
        if resources.hash_prefix(candidate) == resources.hash_prefix(digest):
            return candidate

    named_field = {
        resources.LORA: "Lora hashes",
        resources.EMBEDDING: "TI hashes",
    }.get(kind)
    if named_field:
        mapping = infotext.parse_hash_map(fields.get(named_field))
        key = _named_hash_key(mapping, row)
        if key is not None and (
            resources.hash_prefix(mapping[key]) == resources.hash_prefix(digest)
        ):
            return mapping[key]

    hashes = fields.get("Hashes") if isinstance(fields.get("Hashes"), dict) else {}
    key = _consolidated_name(hashes, row)
    if key is not None and (
        resources.hash_prefix(str(hashes[key])) == resources.hash_prefix(digest)
    ):
        return str(hashes[key])
    return _preserved_hash_spelling(fields, digest)


def _preserved_hash_spelling(fields: dict[str, Any], digest: str) -> str | None:
    """Find this hash's generator spelling anywhere in the source document."""
    wanted = resources.hash_prefix(digest)
    if not wanted:
        return None
    for key, value in fields.items():
        if key == "Model hash":
            candidates = (value,)
        elif key in ("Hashes", "Lora hashes", "TI hashes"):
            candidates = infotext.parse_hash_map(value).values()
        else:
            continue
        for candidate in candidates:
            if resources.hash_prefix(str(candidate)) == wanted:
                return str(candidate)
    return None


def _rendered_fields(
    fields: dict[str, Any],
    resource_rows: list[dict[str, Any]],
    retained_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build each A1111 resource representation from the canonical rows."""
    values: dict[str, Any] = {}

    if "Hashes" in fields:
        original = fields.get("Hashes")
        if isinstance(original, dict):
            hashes = dict(original)
            for row in resource_rows:
                key = _consolidated_name(hashes, row)
                if key is not None:
                    hashes.pop(key)
            for row in retained_rows:
                digest = row.get("hash")
                if not digest:
                    continue
                key = _consolidated_name(original, row) or _row_consolidated_name(row)
                if key is not None:
                    hashes[key] = digest
            if hashes:
                values["Hashes"] = hashes
        elif not resource_rows:
            values["Hashes"] = original

    if "Model hash" in fields:
        checkpoints = [
            row for row in resource_rows if row.get("resource_type") == resources.CHECKPOINT
        ]
        retained_checkpoint = next(
            (
                row
                for row in retained_rows
                if row.get("resource_type") == resources.CHECKPOINT and row.get("hash")
            ),
            None,
        )
        if retained_checkpoint is not None:
            values["Model hash"] = retained_checkpoint["hash"]
        elif not checkpoints:
            values["Model hash"] = fields["Model hash"]

    for key, kind in (("Lora hashes", resources.LORA), ("TI hashes", resources.EMBEDDING)):
        if key not in fields:
            continue
        original_value = fields.get(key)
        original = infotext.parse_hash_map(original_value)
        mapping = dict(original)
        for row in resource_rows:
            if row.get("resource_type") != kind:
                continue
            name = _named_hash_key(mapping, row)
            if name is not None:
                mapping.pop(name)
        for row in retained_rows:
            if row.get("resource_type") != kind or not row.get("hash"):
                continue
            name = _named_hash_key(original, row)
            if name is None:
                name = str(row.get("name_in_prompt") or "").strip() or None
            if name is not None:
                mapping[name] = row["hash"]
        if mapping:
            values[key] = (
                mapping
                if isinstance(original_value, dict)
                else ", ".join(f"{name}: {digest}" for name, digest in mapping.items())
            )

    if "Lora weights" in fields:
        original_value = fields.get("Lora weights")
        original = infotext.parse_hash_map(original_value)
        mapping = dict(original)
        for row in resource_rows:
            if row.get("resource_type") == resources.LORA:
                name = _named_hash_key(mapping, row)
                if name is not None:
                    mapping.pop(name)
        for row in retained_rows:
            if row.get("resource_type") != resources.LORA or row.get("weight") is None:
                continue
            name = _named_hash_key(original, row)
            if name is None:
                name = str(row.get("name_in_prompt") or "").strip() or None
            if name is not None:
                mapping[name] = row["weight"]
        if mapping:
            values["Lora weights"] = (
                mapping
                if isinstance(original_value, dict)
                else ", ".join(f"{name}: {weight}" for name, weight in mapping.items())
            )
    return values


def _credit(row: dict[str, Any]) -> dict[str, Any] | None:
    version_id = row.get("model_version_id")
    if version_id is None:
        return None
    credit: dict[str, Any] = {"modelVersionId": version_id}
    if row.get("resource_type") is not None:
        kind = str(row["resource_type"])
        credit["type"] = kind
    if row.get("weight") is not None:
        credit["weight"] = row["weight"]
    return credit


def _named_hash_key(mapping: dict[str, Any], row: dict[str, Any]) -> str | None:
    wanted = resources.normalize(str(row.get("name_in_prompt") or ""))
    return next((key for key in mapping if resources.normalize(key) == wanted), None)


def _consolidated_name(mapping: dict[str, Any], row: dict[str, Any]) -> str | None:
    kind = row.get("resource_type")
    if kind == resources.CHECKPOINT:
        return next((key for key in mapping if str(key).lower() == "model"), None)
    prefix = {
        resources.EMBEDDING: "embed:",
        resources.UPSCALER: "upscaler:",
    }.get(kind, "lora:")
    wanted = resources.normalize(str(row.get("name_in_prompt") or ""))
    return next(
        (
            key
            for key in mapping
            if str(key).lower().startswith(prefix)
            and resources.normalize(str(key)[len(prefix) :]) == wanted
        ),
        None,
    )


def _row_consolidated_name(row: dict[str, Any]) -> str | None:
    kind = row.get("resource_type")
    if kind == resources.CHECKPOINT:
        return "model"
    prefix = {
        resources.LORA: "lora",
        resources.EMBEDDING: "embed",
        resources.UPSCALER: "upscaler",
    }.get(kind)
    name = str(row.get("name_in_prompt") or "").strip()
    return f"{prefix}:{name}" if prefix and name else None
