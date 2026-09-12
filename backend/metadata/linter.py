"""Data-quality warnings for an effective parsed infotext."""

from __future__ import annotations

from typing import Any

from . import resources

TEMPLATE_TOKENS = ("_HERE", "PLACEHOLDER", "TODO", "INSERT_")


def lint(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    fields = parsed.get("fields") or {}
    result: list[dict[str, Any]] = []

    targets = [("prompt", parsed.get("prompt") or "")]
    targets.append(("negative_prompt", parsed.get("negative_prompt") or ""))
    targets.append(("Hires prompt", fields.get("Hires prompt") or ""))
    for key, text in targets:
        hits = [token for token in TEMPLATE_TOKENS if token in str(text).upper()]
        if hits:
            result.append(
                _warning(
                    "UNSUBSTITUTED_TEMPLATE_TOKEN",
                    f"{key} contains an unsubstituted template token ({', '.join(hits)}).",
                    key,
                    key=key,
                    tokens=", ".join(hits),
                )
            )

    has_checkpoint = any(
        resource.get("resource_type") == resources.CHECKPOINT
        and resource.get("model_version_id")
        for resource in resources.extract(parsed)
    )
    if fields.get("Model") and not fields.get("Model hash") and not has_checkpoint:
        result.append(
            _warning(
                "MISSING_MODEL_HASH",
                "The checkpoint has no hash, so CivitAI cannot identify it.",
                "Model hash",
            )
        )
    return result


def _warning(
    code: str,
    message: str,
    field: str,
    **params: Any,
) -> dict[str, Any]:
    """A finding the UI can translate, with an English fallback.

    Same shape as :func:`backend.api.common.api_error`: the code picks the
    sentence out of the catalogue, ``params`` fills its holes, and ``message`` is
    what to show when the catalogue does not know the code.
    """
    return {
        "code": code,
        "severity": "warning",
        "message": message,
        "field": field,
        "params": params,
    }
