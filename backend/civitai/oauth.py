"""Connecting an account, without a key going through the clipboard.

CivitAI runs a full OAuth 2.0 provider at ``auth.civitai.com``. There are two
levels here and they are easy to confuse:

**The client** identifies the *application*. It is registered once and its
``client_id`` stays the same afterwards. It is public - a public client has no
secret at all.

**The grant** is per account: the user approves this application on CivitAI and
the tokens that come back belong to them. That is what has to happen again after
a revocation, and what a second user would do on their own machine.

Which flow
----------

CivitAI officially supports the device grant as well. This application uses
authorization code with PKCE and a browser loopback: the provider follows
RFC 8252 §7.3 for loopback redirects: protocol and path have to match what was
registered, **the port does not**. This application already serves HTTP locally,
so the browser comes back to a route we own and no second listener is needed.

Two details differ from a textbook OAuth client, and both fail quietly:

- **The scope is a decimal bitmask**, not space-separated names. A generic
  library sends ``"media:write"`` and gets ``invalid_scope``.
- **The refresh returns a new refresh token.** Keep using the old one and the
  next refresh fails an hour later, far from the cause.

Why these calls do not go through :mod:`client`: that module resolves a
credential for every request, and resolving a credential is what calls this
module. Going through it would recurse. The connection pool is shared, the
request machinery is not.

Nothing here reaches a log or the application's own HTTP surface (``CVT-02``) -
not the access token, not the refresh token, and not the PKCE verifier.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from .. import config, db
from .client import http_client
from .errors import AuthError, CivitaiError, OAuthRegistrationRejected

log = logging.getLogger(__name__)

#: Settings rows that together are one connection. They are written and cleared
#: as a unit; a half-present set is treated as no connection at all.
ACCESS_TOKEN_KEY = "civitai_oauth_access_token"
REFRESH_TOKEN_KEY = "civitai_oauth_refresh_token"
EXPIRES_AT_KEY = "civitai_oauth_expires_at"
SCOPE_KEY = "civitai_oauth_scope"

SETTING_KEYS = (ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, EXPIRES_AT_KEY, SCOPE_KEY)

#: A row older installations may still carry, from when the client id could be
#: replaced from the settings screen. Nothing reads it any more; `db._migrate`
#: deletes it, and a backup strips it in case one predates that migration.
LEGACY_CLIENT_ID_KEY = "civitai_oauth_client_id"


#: Everything a backup must not carry (`SET-05`). The legacy client-id row goes
#: with them: the application's identity is a constant now, so a copy of that row
#: could only point a restored installation at a registration nobody controls.
BACKUP_STRIPPED_KEYS = (*SETTING_KEYS, LEGACY_CLIENT_ID_KEY)

#: One refresh at a time. Two threads spending the same refresh token would
#: rotate it twice, and the loser's new token would already be void.
_refresh_lock = threading.Lock()

_TIMEOUT = httpx.Timeout(30.0)


# --- the client: registered once --------------------------------------------


def client_id() -> str:
    """The application's own registration. Not configurable, deliberately.

    It used to be overridable from the settings screen, which made the identity
    the application authenticates as something a user could be talked into
    changing. A fork that wants different permissions registers its own client
    and puts it in `config.py`, where the change is visible in the source.
    """
    return config.CIVITAI_OAUTH_CLIENT_ID


# --- the grant: per account --------------------------------------------------


def begin(redirect_uri: str) -> dict[str, str]:
    """Build the URL the user approves, plus the secrets that verify the answer.

    The verifier and the state stay with the caller and never leave this
    machine; only their hash and the state go to CivitAI.
    """
    value = client_id()
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    state = secrets.token_urlsafe(24)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": value,
            "redirect_uri": redirect_uri,
            # Decimal, not names - see the module docstring.
            "scope": str(config.OAUTH_SCOPE),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return {
        "authorize_url": f"{config.oauth_base()}/api/auth/oauth/authorize?{query}",
        "state": state,
        "verifier": verifier,
    }


def complete(code: str, verifier: str, redirect_uri: str) -> int | None:
    """Trade the authorization code for tokens. Returns the granted scope."""
    value = client_id()
    status, body = _post(
        "/api/auth/oauth/token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": value,
            "code_verifier": verifier,
        },
    )
    if status != 200 or not body.get("access_token"):
        raise _from_error(body, status, "The connection could not be completed.")
    _store(body)
    return _stored_scope()


def refresh() -> str | None:
    """Trade the refresh token for a new pair. Returns the new access token.

    A refusal here means the connection is gone - revoked on CivitAI, or the
    refresh token aged out after 30 days. The rows are cleared so every later
    call sees the honest disconnected state.
    """
    value = client_id()
    with _refresh_lock:
        # Another thread may have refreshed while this one waited. Re-read
        # rather than spend a refresh token that is no longer current.
        current = _read()
        if current and not _expiring(current):
            return current[ACCESS_TOKEN_KEY]
        token = current[REFRESH_TOKEN_KEY] if current else None
        if not token:
            return None
        status, body = _post(
            "/api/auth/oauth/token",
            {"grant_type": "refresh_token", "refresh_token": token, "client_id": value},
        )
        if status != 200 or not body.get("access_token"):
            disconnect()
            log.warning(
                "The CivitAI OAuth connection was refused and has been cleared. "
                "Reconnect in the settings."
            )
            return None
        _store(body)
        return str(body["access_token"])


def access_token() -> str | None:
    """A usable access token, refreshed if it is about to expire.

    ``None`` means there is no OAuth connection - not that something failed.
    """
    current = _read()
    if current is None:
        return None
    if _expiring(current):
        return refresh()
    return current[ACCESS_TOKEN_KEY]


def connected() -> bool:
    """Is a complete connection stored? Says nothing about it still being valid."""
    return _read() is not None


def scope() -> int | None:
    """The scope CivitAI granted, or ``None`` when not connected."""
    return _stored_scope() if connected() else None


def disconnect() -> None:
    """Forget the connection. Local only - the grant stays until revoked.

    The registered client is kept: it is the application's identity, and
    reconnecting should not mean registering a second one.
    """
    with db.transaction():
        for key in SETTING_KEYS:
            db.set_setting(key, None)
        db.set_setting("account_json", None)
        db.set_setting("civitai_username", None)


# --- internals ---------------------------------------------------------------


def _post(path: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """One form post to the identity host.

    Returns status and parsed body together because OAuth carries its meaning in
    the *body* of an HTTP 400: ``invalid_grant`` and ``invalid_client`` need
    telling apart, and the status alone does not.
    """
    url = f"{config.oauth_base()}{path}"
    if not config.site_host_allowed(url):
        raise AuthError(
            f"The OAuth host {url} is not an allowed destination for credentials. "
            "Allow it through LIVEBOUND_ALLOWED_SITES or leave the default."
        )
    response = http_client().post(
        url,
        data=data,
        headers={"Accept": "application/json", "User-Agent": config.USER_AGENT},
        timeout=_TIMEOUT,
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return response.status_code, body


def _store(body: dict[str, Any]) -> None:
    """Write one token response as a unit.

    ``expires_in`` is turned into an absolute instant here so a later read does
    not have to know when the response arrived.
    """
    # A missing value means an hour; an explicit 0 means already expired, and
    # honouring it beats inventing an hour of validity the server did not give.
    expires_in = _int(body.get("expires_in"), default=3600) or 0
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    values = [(ACCESS_TOKEN_KEY, str(body["access_token"]))]
    # The provider rotates the refresh token on every use. Dropping the new one
    # here would break the *next* refresh, an hour later.
    if body.get("refresh_token"):
        values.append((REFRESH_TOKEN_KEY, str(body["refresh_token"])))
    values.append((EXPIRES_AT_KEY, expires_at.isoformat(timespec="seconds")))
    granted = _scope_text(body.get("scope"))
    if granted is not None:
        values.append((SCOPE_KEY, granted))

    with db.transaction():
        for key, value in values:
            db.set_setting(key, value)


def _read() -> dict[str, str] | None:
    """The stored connection, or ``None`` when it is absent or incomplete."""
    values = {key: (db.get_setting(key) or "").strip() for key in SETTING_KEYS}
    if not values[ACCESS_TOKEN_KEY] or not values[REFRESH_TOKEN_KEY]:
        return None
    return values


def _expiring(current: dict[str, str]) -> bool:
    """Is the access token past its refresh margin?

    An unreadable or missing instant counts as expiring: refreshing once too
    often costs a request, using a dead token costs the call.
    """
    raw = current.get(EXPIRES_AT_KEY) or ""
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    margin = timedelta(seconds=config.OAUTH_REFRESH_MARGIN_SECONDS)
    return when - margin <= datetime.now(timezone.utc)


def _scope_text(raw: Any) -> str | None:
    """The granted scope, as a plain number, whichever shape it arrived in.

    Measured, not assumed: the device-token endpoint answers ``scope`` as a
    string, the token endpoint as a **one-element list**. Storing ``str(value)``
    of the second turns 229 into ``"['229']"``, which then parses as nothing and
    the settings screen shows no permissions at all - the connection works, but
    claims to grant nothing.
    """
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw is None:
        return None
    return str(raw).strip() or None


def _stored_scope() -> int | None:
    return _int(db.get_setting(SCOPE_KEY), default=None)


def _int(value: Any, *, default: int | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _from_error(body: dict[str, Any], status: int, fallback: str) -> CivitaiError:
    """Turn an OAuth error body into something that says what to do next."""
    error = str(body.get("error") or "")
    described = str(body.get("error_description") or "")
    if error in {"invalid_client", "invalid_scope"}:
        return OAuthRegistrationRejected()
    if error == "invalid_grant":
        return AuthError(
            "CivitAI refused the authorization. It may have expired - start the "
            "connection again and approve it without a long pause."
        )
    detail = described or error or f"HTTP {status}"
    return CivitaiError(f"{fallback} CivitAI said: {detail}")
