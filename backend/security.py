"""Guards for a local web app that holds a credential.

This app has no login, which is normal for a single-user tool on localhost - but
it does hold a credential that can post to a public CivitAI account. That combination
deserves two specific protections.

**DNS rebinding.** A page on the open internet cannot read a response from
127.0.0.1 (no CORS headers, and a JSON body triggers a preflight that fails).
It can, however, make a hostname it controls resolve to 127.0.0.1 after the page
has loaded, and then talk to this server as same-origin. The defence is to check
the ``Host`` header, because the browser sends the attacker's hostname there and
it will not match a loopback name.

**Serving arbitrary files.** Image paths come from the scanner rather than from
the request, but a stale or manipulated row must not turn the preview endpoint
into a general file reader. So a path is served only if it sits under a folder
the user actually configured.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path, PurePath

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from . import config

_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0", "testserver"}


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    if host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def allowed_hosts() -> set[str]:
    """Host names this server answers to.

    Extendable through ``LIVEBOUND_ALLOWED_HOSTS`` for the case where the
    app is deliberately reachable from another machine - binding to a LAN address
    is a conscious act, and it should not silently also lower this guard.
    """
    extra = os.environ.get("LIVEBOUND_ALLOWED_HOSTS", "")
    return {name.strip().lower() for name in extra.split(",") if name.strip()}


#: Methods that change something. A GET can be read by nobody cross-origin, so
#: the browser check below only has to cover these.
_WRITING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _foreign_origin(request: Request) -> str | None:
    """The originating site, when it is not this app.

    Two independent signals, because either can be absent:

    * ``Origin`` - sent by every browser on a cross-origin write, and on
      same-origin writes too. A non-loopback value is somebody else's page.
    * ``Sec-Fetch-Site: cross-site`` - sent by current browsers even where
      ``Origin`` is omitted.

    A request with neither is not a browser: curl, the test client, the smoke
    test. Those are the user's own tools on the user's own machine and are left
    alone - this guard is about a *page* the user did not write, not about
    authenticating a client.
    """
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return request.headers.get("origin") or "cross-site"

    origin = request.headers.get("origin", "").strip()
    if not origin:
        return None
    host = origin.split("://", 1)[-1].rsplit(":", 1)[0].strip().lower()
    if origin.startswith("[") or host.startswith("["):
        host = origin.split("]")[0].split("[")[-1]
        host = f"[{host}]"
    if _is_loopback(host) or host in allowed_hosts():
        return None
    return origin


def is_under_any_root(path: PurePath, roots: list[PurePath]) -> bool:
    """Whether an already-resolved path lies within one of the resolved roots."""
    return any(path == root or root in path.parents for root in roots)


async def guard_request(request: Request, call_next):
    """Two checks a local app with no login cannot do without.

    **Host** stops DNS rebinding: a page on the open internet cannot read from
    127.0.0.1, but it can point its own hostname there, and the browser then
    sends the attacker's name - which never looks like loopback.

    **Origin** stops cross-site request forgery, which the host check does not
    touch. A page anywhere can POST to an absolute ``http://127.0.0.1:8430/...``
    URL; that request carries a loopback ``Host`` and sails through. It cannot
    read the answer, but the effect lands - and the effects here include
    deleting a post on CivitAI, which has no undo. The typed confirmation lives
    in the browser and is not a boundary.
    """
    if request.method in _WRITING_METHODS:
        origin = _foreign_origin(request)
        if origin is not None:
            return JSONResponse(
                status_code=403,
                content={
                    "detail": {
                        "code": "foreign_origin",
                        "message": f"Refused a {request.method} from {origin}.",
                        "params": {"origin": origin},
                    }
                },
            )

    header = request.headers.get("host", "")
    host = header.rsplit(":", 1)[0].strip().lower() if header else ""
    # IPv6 literals arrive bracketed and contain colons of their own.
    if header.startswith("["):
        host = header.split("]")[0] + "]"

    if not _is_loopback(host) and host not in allowed_hosts():
        return JSONResponse(
            status_code=421,
            content={
                "error": f"Unexpected Host header: {host}",
                "hint": (
                    "The app only answers on localhost. If it is meant to be reachable "
                    "from another machine, put that hostname in "
                    "LIVEBOUND_ALLOWED_HOSTS."
                ),
            },
        )
    return await call_next(request)


def serveable(path: Path) -> Path:
    """Return ``path`` if it is safe to send to the browser, else raise 403/404.

    Allowed: anything under a configured source folder, and the thumbnail cache.
    Symlinks are resolved first, so a link pointing out of a source folder cannot
    be used to walk somewhere else.
    """
    from .store import sources as source_store

    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise HTTPException(404, "File not found.") from None

    roots = [Path(root["path"]).resolve() for root in source_store.list_roots()]
    roots.append(config.thumbnail_dir().resolve())

    if is_under_any_root(resolved, roots):
        return resolved

    raise HTTPException(
        403, "That file lies outside the configured image folders."
    )


def warn_if_exposed(host: str) -> str | None:
    """A one-line warning for a non-loopback bind, or None."""
    if _is_loopback(host) and host != "0.0.0.0":
        return None
    return (
        f"Careful: the server listens on {host} and is therefore reachable on the "
        "network. There is no login - whoever reaches the address can create posts on "
        "your CivitAI account. Only do this on a network you trust."
    )
