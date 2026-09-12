"""What the OAuth connection is actually allowed to do.

CivitAI keys carry a scope bitmask, and a key that can *create* a post cannot
necessarily *delete* one - ``MediaWrite`` and ``MediaDelete`` are separate bits.
That asymmetry is dangerous for this app in particular: without the delete bit
every "remove it again" path fails, and a post created by mistake can only be
cleaned up by hand in the web UI.

So the scope is decoded up front and checked before the actions that need it,
rather than discovered from a 403 after the post already exists.

Bit values from ``packages/civitai-auth/src/token-scope.ts``.
"""

from __future__ import annotations

from typing import Any

from . import account

SCOPES: dict[str, int] = {
    "UserRead": 1 << 0,
    "UserWrite": 1 << 1,
    "ModelsRead": 1 << 2,
    "ModelsWrite": 1 << 3,
    "ModelsDelete": 1 << 4,
    "MediaRead": 1 << 5,
    "MediaWrite": 1 << 6,
    "MediaDelete": 1 << 7,
    "ArticlesRead": 1 << 8,
    "ArticlesWrite": 1 << 9,
    "ArticlesDelete": 1 << 10,
    "BountiesRead": 1 << 11,
    "BountiesWrite": 1 << 12,
    "BountiesDelete": 1 << 13,
    "AIServicesRead": 1 << 14,
    "AIServicesWrite": 1 << 15,
    "BuzzRead": 1 << 16,
    "CollectionsRead": 1 << 17,
    "CollectionsWrite": 1 << 18,
    "SocialWrite": 1 << 19,
    "SocialTip": 1 << 20,
    "NotificationsRead": 1 << 21,
    "NotificationsWrite": 1 << 22,
}

#: What this app needs, and what breaks without it.
REQUIRED: dict[str, tuple[str, str]] = {
    "MediaRead": ("read posts", "Reconciliation and status checks do not work."),
    "MediaWrite": (
        "create and schedule posts",
        "Without this scope nothing can be pushed at all.",
    ),
    "MediaDelete": (
        "delete posts",
        (
            "'Delete remotely' and 'rebuild' fail - a post created by accident can "
            "then only be removed in the web UI."
        ),
    ),
}


def current(data: dict[str, Any] | None = None) -> int:
    """The connection's scope bitmask, or -1 when it is not known yet."""
    data = data or account.cached() or {}
    value = data.get("tokenScope")
    return int(value) if isinstance(value, (int, float)) else -1


def has(name: str, data: dict[str, Any] | None = None) -> bool:
    """Does the key carry this scope?

    An unknown scope answers ``True``: the server enforces the real rule anyway,
    and refusing locally on missing information would block a working key.
    """
    mask = current(data)
    if mask < 0:
        return True
    return bool(mask & SCOPES.get(name, 0))


def describe(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Scope report for the settings page."""
    data = data or account.cached() or {}
    mask = current(data)
    if mask < 0:
        return {"known": False, "granted": [], "missing": [], "problems": []}

    granted = sorted(name for name, bit in SCOPES.items() if mask & bit)
    problems = [
        {"scope": name, "label": label, "consequence": consequence}
        for name, (label, consequence) in REQUIRED.items()
        if not (mask & SCOPES[name])
    ]
    return {
        "known": True,
        "mask": mask,
        "granted": granted,
        "missing": sorted(name for name, bit in SCOPES.items() if not (mask & bit)),
        "problems": problems,
    }


class MissingScope(RuntimeError):
    """A local refusal, raised before the call that would 403."""


def require(name: str) -> None:
    if has(name):
        return
    label, consequence = REQUIRED.get(name, (name, ""))
    raise MissingScope(
        f"The CivitAI connection does not carry the '{name}' scope ({label}). "
        f"{consequence} Disconnect and connect again in the settings, and grant "
        "every permission the consent screen asks for."
    )
