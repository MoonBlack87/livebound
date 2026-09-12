"""Who the connection belongs to, and whether that account may write at all.

``whoami`` is cheap and its answer changes rarely, so it is cached in settings
and refreshed on demand. Checking it before a batch is worth the round trip: an
un-onboarded account fails every guarded write and a muted one fails most, and
finding that out after twenty uploads is a bad way to learn it.
"""

from __future__ import annotations

from typing import Any

from .. import db
from . import mcp, rest
from .errors import AccountNotReady, CivitaiError

_SETTING = "account_json"


def refresh() -> dict[str, Any]:
    """Resolve the account behind the key and cache it.

    Two sources, in this order and for a reason. The documented REST endpoint
    ``/api/v1/me`` is authoritative for identity and is what proves the key
    works. The MCP ``whoami`` tool adds the status flags that endpoint does not
    carry - onboarding, muted, moderator - but it currently fails upstream
    (``user.getSelfStatus: Invalid input``), so its failure must not take the
    whole lookup down with it.
    """
    account: dict[str, Any] = dict(rest.me())

    try:
        status = mcp.whoami()
    except CivitaiError as exc:
        account.setdefault("status_source", "rest-only")
        account["status_error"] = str(exc)
    else:
        account.update({k: v for k, v in status.items() if v is not None})
        account["status_source"] = "mcp"

    db.set_json_setting(_SETTING, account)
    username = account.get("username")
    if isinstance(username, str) and username:
        db.set_setting("civitai_username", username)
    return account


def cached() -> dict[str, Any] | None:
    value = db.get_json_setting(_SETTING)
    return value if isinstance(value, dict) else None


def get(*, refresh_if_missing: bool = True) -> dict[str, Any] | None:
    account = cached()
    if account is None and refresh_if_missing:
        account = refresh()
    return account


def username() -> str | None:
    name = db.get_setting("civitai_username")
    if name:
        return name
    account = cached()
    value = account.get("username") if account else None
    return value if isinstance(value, str) else None


def ensure_can_write(account: dict[str, Any] | None = None) -> dict[str, Any]:
    """Raise unless the account may perform guarded writes."""
    account = account or get() or {}
    # Only refuse on a flag we actually saw. When the status lookup was
    # unavailable, the server-side guards still apply - failing closed here
    # would block every push over a broken upstream tool.
    if account.get("muted"):
        raise AccountNotReady(
            "The account is muted - CivitAI refuses posts."
        )
    onboarded = account.get("isOnboarded")
    if onboarded is False:
        raise AccountNotReady(
            "The account has not finished onboarding - posting is blocked."
        )
    return account
