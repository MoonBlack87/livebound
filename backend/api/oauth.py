"""Connecting an account through the shipped OAuth client.

The PKCE verifier and the state never cross this boundary (`CVT-02`). They live
in module state for the minute the user spends in the browser - not in the
database, because a half-finished authorization has no business surviving a
restart.
"""

from __future__ import annotations

import secrets
import threading
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import config, db
from ..civitai import oauth
from ..civitai.errors import CivitaiError, OAuthRegistrationRejected
from ..models import Ok
from .common import api_error

router = APIRouter(prefix="/api/oauth", tags=["oauth"])


class _Pending:
    """The authorization currently waiting in the user's browser, if any."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: str | None = None
        self.verifier: str | None = None
        self.redirect_uri: str | None = None
        self.outcome: str = "idle"
        self.error_code: str | None = None

    def clear(self) -> None:
        self.state = None
        self.verifier = None
        self.redirect_uri = None
        self.error_code = None


_pending = _Pending()

def _redirect_uri(request: Request) -> str:
    """Where CivitAI sends the browser back to - this application's own route.

    Built from the request rather than from a constant so it matches whatever
    host and port the user actually reached us on. Only protocol and path have
    to match what was registered; RFC 8252 lets the port differ.
    """
    base = str(request.base_url).rstrip("/")
    return f"{base}{config.OAUTH_REDIRECT_PATH}"


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    """Where the connection stands.

    ``state`` is one of ``idle``, ``pending``, ``connected``, ``denied`` or
    ``failed``.
    """
    if oauth.connected():
        return {
            "state": "connected",
            "scope": oauth.scope(),
            "redirect_uri": _redirect_uri(request),
        }
    with _pending.lock:
        state = "pending" if _pending.state else _pending.outcome
        error_code = _pending.error_code
    return {
        "state": state,
        "scope": None,
        "redirect_uri": _redirect_uri(request),
        "error_code": error_code,
    }


@router.post("/start")
def start(request: Request) -> dict[str, Any]:
    """Begin an authorization and hand back the URL to open."""
    if oauth.connected():
        raise api_error(
            "oauth_already_connected",
            "An account is already connected. Disconnect it before connecting another.",
            409,
        )
    redirect_uri = _redirect_uri(request)
    try:
        begun = oauth.begin(redirect_uri)
    except CivitaiError as exc:
        raise api_error(
            getattr(exc, "code", "oauth_failed"), str(exc), 502, reason=str(exc)
        ) from exc

    with _pending.lock:
        _pending.state = begun["state"]
        _pending.verifier = begun["verifier"]
        _pending.redirect_uri = redirect_uri
        _pending.outcome = "pending"
        _pending.error_code = None

    # Deliberately no verifier and no state in the response (`CVT-02`).
    return {"state": "pending", "authorize_url": begun["authorize_url"]}


@router.get("/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Where CivitAI sends the browser back to.

    Always ends in a redirect into the application: the user is looking at a
    browser tab, and a JSON body would be a dead end for them. What happened is
    read back through ``/status``.
    """
    redirect_target = (
        "/"
        if request.app.state.setup_required
        or db.get_setting("setup_completed") == "0"
        else "/?settings=civitai"
    )
    with _pending.lock:
        expected, verifier, redirect_uri = (
            _pending.state,
            _pending.verifier,
            _pending.redirect_uri,
        )

    # Error callbacks need the same state binding as a successful exchange.
    # An unrelated callback must not clear a connection attempt still in progress.
    if not expected or not state or not secrets.compare_digest(state, expected):
        return RedirectResponse(redirect_target, status_code=303)

    if error:
        if error in {"invalid_client", "invalid_scope"}:
            _finish("failed", error_code=OAuthRegistrationRejected.code)
        else:
            _finish("denied")
        return RedirectResponse(redirect_target, status_code=303)
    if not code:
        _finish("failed")
        return RedirectResponse(redirect_target, status_code=303)

    try:
        oauth.complete(code, verifier or "", redirect_uri or _redirect_uri(request))
    except CivitaiError as exc:
        _finish("failed", error_code=getattr(exc, "code", None))
        return RedirectResponse(redirect_target, status_code=303)
    _finish("connected")
    return RedirectResponse(redirect_target, status_code=303)


@router.post("/cancel")
def cancel() -> Ok:
    """Abandon an authorization the user did not finish."""
    _finish("idle")
    return Ok()


@router.post("/disconnect")
def disconnect() -> Ok:
    """Forget the stored connection, keeping the registered application.

    Local only: the grant itself stays until the user revokes it on CivitAI.
    """
    oauth.disconnect()
    _finish("idle")
    return Ok()


def _finish(outcome: str, *, error_code: str | None = None) -> None:
    with _pending.lock:
        _pending.clear()
        _pending.outcome = outcome
        _pending.error_code = error_code
