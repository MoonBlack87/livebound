"""The single place that speaks HTTP to CivitAI.

Everything above this module works with typed results; everything CivitAI-shaped
stops here. That matters because one of the three surfaces (tRPC) is internal and
could change: if it does, the blast radius is this directory.

The client is synchronous on purpose. Every call happens either inside a daemon
push thread or inside a sync FastAPI endpoint (which runs in the threadpool);
threading an event loop through those would fight the thread-local SQLite model
for no gain.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .. import config, db
from .errors import (
    AuthError,
    CivitaiError,
    NotFound,
    RateLimited,
    TransportError,
    UploadUrlRejected,
)

#: Keep the diagnostic log bounded; it exists to debug the tRPC surface, not to
#: be an archive.
API_LOG_LIMIT = 500
_LOG_BODY_CHARS = 4000

# Body logging is a structural trace, not a second copy of account content.
# Containers let ids, counts and result shape survive while unknown fields are
# omitted by default. In particular, post text and generation metadata never
# need to be present to diagnose a procedure, status or duration.
_LOGGED_BODY_FIELDS = frozenset(
    {
        "arguments",
        "body",
        "bytes",
        "code",
        "content",
        "count",
        "cursor",
        "data",
        "downloadurl",
        "error",
        "errortype",
        "height",
        "href",
        "httpstatus",
        "id",
        "iserror",
        "json",
        "jsonrpc",
        "length",
        "limit",
        "method",
        "nextcursor",
        "ok",
        "page",
        "params",
        "result",
        "size",
        "status",
        "structuredcontent",
        "success",
        "total",
        "type",
        "uploadurl",
        "url",
        "width",
    }
)
_LOGGED_TEXT_FIELDS = frozenset({"code", "errortype", "jsonrpc", "method", "status", "type"})
_SECRET_BODY_FIELDS = frozenset(
    {"authorization", "token", "api_key", "apikey", "access_token", "password", "secret"}
)
_URL_BODY_FIELDS = frozenset({"downloadurl", "href", "uploadurl", "url"})

_TIMEOUT = httpx.Timeout(180.0, connect=15.0)

_pool_lock = threading.Lock()
_pool: httpx.Client | None = None

#: 429 and 5xx are retried; everything else is a decision, not a hiccup.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3


def get_token() -> str | None:
    """The OAuth credential every authenticated call carries.

    Imported here rather than at module scope: ``oauth`` borrows this module's
    connection pool, so the dependency only runs one way at import time.
    """
    from . import oauth

    return oauth.access_token()


def require_token() -> str:
    token = get_token()
    if not token:
        raise AuthError(
            "No CivitAI credential stored. Connect an account in the settings."
        )
    return token


def _request_token(authenticated: bool) -> str | None:
    if not authenticated:
        return None
    if not config.site_host_allowed(config.site_base()):
        raise AuthError(
            "The configured CivitAI site is not allowed to receive the credential. "
            "Choose civitai.red or civitai.com in settings, or allow the host through "
            "LIVEBOUND_ALLOWED_SITES."
        )
    return require_token()


def _headers(token: str | None, *, accept: str) -> dict[str, str]:
    """Headers accepted by all three surfaces.

    ``x-client``/``x-client-date`` are required by CivitAI's ``enforceClientVersion``
    middleware on the tRPC routes; sending them everywhere keeps one code path.
    """
    headers = {
        "Accept": accept,
        "Content-Type": "application/json",
        "User-Agent": config.USER_AGENT,
        "x-client": "civitai",
        "x-client-date": datetime.now(timezone.utc).isoformat(),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _truncate(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    except (TypeError, ValueError):
        text = str(value)
    return text[:_LOG_BODY_CHARS]


def _scrub(payload: Any) -> Any:
    """Project a request or response onto bounded, non-content diagnostics."""
    if isinstance(payload, dict):
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            lowered = key.casefold().replace("-", "_")
            compact = lowered.replace("_", "")
            if lowered in _SECRET_BODY_FIELDS or compact in _SECRET_BODY_FIELDS:
                cleaned[key] = "***"
                continue
            if compact not in _LOGGED_BODY_FIELDS:
                continue
            if isinstance(value, str) and compact in _URL_BODY_FIELDS:
                cleaned[key] = _scrub_url(value) or _text_shape(value)
            elif isinstance(value, str) and compact in _LOGGED_TEXT_FIELDS:
                cleaned[key] = value
            else:
                cleaned[key] = _scrub(value)
        return cleaned
    if isinstance(payload, list):
        return {"type": "list", "length": len(payload)}
    if isinstance(payload, str):
        return _scrub_url(payload) or _text_shape(payload)
    return payload


def _text_shape(value: str) -> dict[str, Any]:
    return {"type": "str", "length": len(value)}


def _scrub_url(value: str) -> str | None:
    """Remove bearer material while retaining a URL's diagnostic location."""
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None

    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    # Query strings and fragments add no location information, but commonly
    # carry presigned bearer credentials or user-authored search/content text.
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _logged_response(response: Any, *, status: int | None, ok: bool) -> str | None:
    if ok:
        return _truncate(_scrub(response)) if response is not None else None
    body = _scrub(response) if response is not None else None
    return _truncate(
        {
            "error_type": _logged_error_type(status),
            **({"body": body} if body is not None else {}),
        }
    )


def _logged_error_type(status: int | None) -> str:
    if status is None:
        return TransportError.__name__
    if status in (401, 403):
        return AuthError.__name__
    if status == 404:
        return NotFound.__name__
    if status == 429:
        return RateLimited.__name__
    return CivitaiError.__name__


def log_call(
    transport: str,
    procedure: str,
    *,
    status: int | None,
    duration_ms: int,
    ok: bool,
    request: Any = None,
    response: Any = None,
) -> None:
    try:
        # Inside the guard on purpose. This runs after CivitAI has already
        # carried the call out, so a database that is briefly unavailable must
        # not turn a completed operation into an error - `post.create` is not
        # safe to repeat (`CVT-07`).
        if db.get_setting("debug_logging") != "1":
            return
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO api_calls(at, transport, procedure, http_status, duration_ms, ok,"
                " request_json, response_json) VALUES(?,?,?,?,?,?,?,?)",
                (
                    db.now_iso(),
                    transport,
                    procedure,
                    status,
                    duration_ms,
                    1 if ok else 0,
                    _truncate(_scrub(request)) if request is not None else None,
                    _logged_response(response, status=status, ok=ok),
                ),
            )
            conn.execute(
                "DELETE FROM api_calls WHERE id NOT IN "
                "(SELECT id FROM api_calls ORDER BY id DESC LIMIT ?)",
                (API_LOG_LIMIT,),
            )
    except Exception:
        # diagnostics must never break the call they describe
        pass


def http_client() -> httpx.Client:
    """The one connection pool this process uses for CivitAI.

    A batch push makes dozens of requests to the same host; opening a client per
    call repeats DNS, TCP and TLS setup every time, which costs more than the
    request itself. ``httpx.Client`` is thread-safe, which the push threads need.
    """
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = httpx.Client(timeout=_TIMEOUT)
        return _pool


def close_http_client() -> None:
    """Drop the pool - on shutdown, and between tests."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.close()


def _raise_for_status(status: int, body: str, procedure: str) -> None:
    if status in (401, 403):
        raise AuthError(
            "CivitAI rejected the connection. Reconnect the account in the settings.",
            detail=body,
            status=status,
        )
    if status == 404:
        raise NotFound(f"{procedure}: not found.", detail=body, status=status)
    if status == 429:
        raise RateLimited(
            "CivitAI rate limit reached. The run pauses.", detail=body, status=status
        )
    raise CivitaiError(f"{procedure}: HTTP {status}", detail=body, status=status)


def post_json(
    url: str,
    payload: Any,
    *,
    procedure: str,
    transport: str,
    accept: str = "application/json",
    timeout: httpx.Timeout | None = None,
    authenticated: bool = True,
    response_type: type[dict] | type[list] = dict,
    retryable: bool = True,
) -> Any:
    """POST JSON and return the expected container shape, with retries and logging."""
    token = _request_token(authenticated)
    headers = _headers(token, accept=accept)
    started = time.monotonic()
    attempts = _MAX_ATTEMPTS if retryable else 1

    for attempt in range(1, attempts + 1):
        try:
            response = http_client().post(
                url,
                json=payload,
                headers=headers,
                **({"timeout": timeout} if timeout is not None else {}),
            )
        except httpx.HTTPError as exc:
            error = TransportError(
                f"Network error during {procedure}: {exc}", procedure=procedure
            )
            if attempt == attempts:
                duration = int((time.monotonic() - started) * 1000)
                log_call(
                    transport,
                    procedure,
                    status=None,
                    duration_ms=duration,
                    ok=False,
                    request=payload,
                    response=str(error),
                )
                raise error from exc
            time.sleep(2**attempt)
            continue

        duration = int((time.monotonic() - started) * 1000)
        if response.status_code in _RETRY_STATUSES and attempt < attempts:
            time.sleep(_retry_delay(response, attempt))
            continue

        if response.status_code >= 400:
            log_call(
                transport,
                procedure,
                status=response.status_code,
                duration_ms=duration,
                ok=False,
                request=payload,
                response=response.text,
            )
            _raise_for_status(response.status_code, response.text, procedure)

        try:
            value = response.json()
        except ValueError as exc:
            log_call(
                transport,
                procedure,
                status=response.status_code,
                duration_ms=duration,
                ok=False,
                request=payload,
                response=response.text,
            )
            raise CivitaiError(f"{procedure}: invalid JSON response ({exc})") from exc

        log_call(
            transport,
            procedure,
            status=response.status_code,
            duration_ms=duration,
            ok=True,
            request=payload,
            response=value,
        )
        if not isinstance(value, response_type):
            raise CivitaiError(f"{procedure}: unexpected response shape")
        return value

    raise CivitaiError(f"{procedure}: failed")


def put_file(
    url: str, path: Path, *, content_type: str, procedure: str, transport: str = "rest"
) -> None:
    """Stream a file to a pre-signed URL.

    No Authorization header: the signature in the URL is the credential, and
    sending the OAuth token to a storage host would leak it outside CivitAI.
    """
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        scheme = None
    if scheme != "https":
        raise UploadUrlRejected()

    started = time.monotonic()
    try:
        with path.open("rb") as handle:
            response = http_client().put(
                url, content=handle, headers={"Content-Type": content_type}
            )
    except httpx.HTTPError as exc:
        raise TransportError(
            f"Network error during upload: {exc}", procedure=procedure
        ) from exc

    duration = int((time.monotonic() - started) * 1000)
    ok = response.status_code < 400
    log_call(
        transport,
        procedure,
        status=response.status_code,
        duration_ms=duration,
        ok=ok,
        request={"file": path.name, "bytes": path.stat().st_size},
    )
    if not ok:
        _raise_for_status(response.status_code, response.text[:500], procedure)


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    procedure: str,
    transport: str,
    authenticated: bool = True,
) -> Any:
    """GET and return the decoded body. Used by tRPC queries and the public REST API."""
    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": config.USER_AGENT,
        "x-client": "civitai",
        "x-client-date": datetime.now(timezone.utc).isoformat(),
    }
    token = _request_token(authenticated)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    started = time.monotonic()
    try:
        response = http_client().get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise TransportError(
            f"Network error during {procedure}: {exc}", procedure=procedure
        ) from exc

    duration = int((time.monotonic() - started) * 1000)
    if response.status_code >= 400:
        log_call(
            transport,
            procedure,
            status=response.status_code,
            duration_ms=duration,
            ok=False,
            request=params,
            response=response.text,
        )
        _raise_for_status(response.status_code, response.text, procedure)

    try:
        value = response.json()
    except ValueError as exc:
        raise CivitaiError(f"{procedure}: invalid JSON response ({exc})") from exc

    log_call(
        transport,
        procedure,
        status=response.status_code,
        duration_ms=duration,
        ok=True,
        request=params,
        response=value,
    )
    return value


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), 60.0)
        except ValueError:
            pass
    return float(2**attempt)
