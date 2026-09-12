"""Bounded, redacted diagnostics for failures on the local HTTP surface.

Only routing facts and validation reasons belong here. Request data does not:
headers, query values and bodies are deliberately never inspected by this
module. The log is for finding an intermittent defect, not for auditing use.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import config, db

_logger = logging.getLogger("livebound.diagnostics")
_logger.setLevel(logging.INFO)
_logger.propagate = False
_lock = threading.Lock()
_active_path: Path | None = None
_INSTALLATION_ROOT = Path(__file__).resolve().parents[1]
#: Where a frame's path stops being this machine's and starts being the code's.
#: The first two name a dependency wherever its environment lives; the rest are
#: this application's own top-level directories, for a frame that resolved
#: against no installation root at all.
#: A dependency, wherever its environment lives.
_DEPENDENCY_MARKERS = ("site-packages", "dist-packages")

#: Plus this application's own top-level directories, for a frame that resolved
#: against no installation root at all.
_PACKAGE_PATH_MARKERS = _DEPENDENCY_MARKERS + ("backend", "frontend", "tools", "test")


class _UtcFormatter(logging.Formatter):
    converter = time.gmtime


def configure() -> Path:
    """Point the one diagnostic logger at the current data directory.

    ``create_app`` can be called repeatedly by tests, and the configured data
    directory can move. Keeping exactly one active handler avoids duplicate
    entries and makes the path printed by the CLI truthful in both cases.
    """
    global _active_path
    path = config.diagnostic_log_path().resolve()
    try:
        with _lock:
            if _active_path == path and _logger.handlers:
                return path

            handler = RotatingFileHandler(
                path,
                maxBytes=config.DIAGNOSTIC_LOG_MAX_BYTES,
                backupCount=config.DIAGNOSTIC_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(
                _UtcFormatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%SZ")
            )

            previous = list(_logger.handlers)
            _logger.handlers[:] = [handler]
            _active_path = path
            for old in previous:
                old.close()
    except Exception:
        # Setting up a diagnostic must not stop the application it describes.
        return path
    return path


def validation_response(request: Request, exc: RequestValidationError) -> JSONResponse:
    diagnostic_id = _diagnostic_id()
    issues = [_validation_issue(error) for error in exc.errors()]
    method = request.method
    endpoint = request.url.path
    _write(
        {
            "event": "request_validation",
            "diagnostic_id": diagnostic_id,
            "method": method,
            "path": endpoint,
            "errors": issues,
        }
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "request_validation_error",
                "message": (
                    f"Request {method} {endpoint} failed validation "
                    f"(diagnostic {diagnostic_id})."
                ),
                "params": {
                    "diagnostic_id": diagnostic_id,
                    "method": method,
                    "endpoint": endpoint,
                },
                "errors": issues,
            }
        },
    )


def unexpected_response(request: Request, exc: Exception) -> JSONResponse:
    diagnostic_id = _diagnostic_id()
    method = request.method
    endpoint = request.url.path
    written = _write(
        {
            "event": "unexpected_error",
            "diagnostic_id": diagnostic_id,
            "method": method,
            "path": endpoint,
            "exception_type": type(exc).__name__,
        },
        exc=exc,
    )
    if written:
        code = "unexpected_error"
        message = (
            "An unexpected error occurred. Inspect diagnostic "
            f"{diagnostic_id} in the application log."
        )
    else:
        code = "unexpected_error_no_diagnostic"
        # True whether diagnostics are off - which since task 161 is the ordinary
        # state - or the write itself failed. Either way there is no entry, and
        # naming one would send somebody looking for a file that says nothing.
        message = (
            "An unexpected error occurred, and it could not be recorded. Check "
            "that diagnostics are on in the settings, then do it again."
        )
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "code": code,
                "message": message,
                "params": {"diagnostic_id": diagnostic_id},
            }
        },
    )


def action(stage: str, post_id: int | None, detail: dict[str, Any]) -> None:
    """Write one structured operational event when debug logging is enabled."""
    try:
        if db.get_setting("debug_logging") != "1":
            return
        _write(
            {
                "event": "action",
                "timestamp": db.now_iso(),
                "stage": stage,
                "post_id": post_id,
                "detail": dict(detail),
            },
            level=logging.INFO,
        )
    except Exception:
        # Writing a diagnostic must not break the operation it describes.
        pass


def _validation_issue(error: dict[str, Any]) -> dict[str, str]:
    location = ".".join(str(part) for part in error.get("loc", ()))
    return {
        "type": str(error.get("type") or "validation_error"),
        "location": location,
        "message": str(error.get("msg") or "Invalid input."),
        "input_type": type(error.get("input")).__name__,
    }


def _diagnostic_id() -> str:
    return secrets.token_hex(4)


def _traceback_frames(exc: Exception) -> list[dict[str, str | int]]:
    """Keep code locations while omitting machine paths and source context."""
    return [
        {
            "module": _frame_module(frame.filename),
            "function": frame.name,
            "line": frame.lineno,
        }
        for frame in traceback.extract_tb(exc.__traceback__)
    ]


def _frame_module(filename: str) -> str:
    is_windows_path = len(filename) > 2 and filename[1] == ":" and filename[2] in "\\/"
    if not is_windows_path:
        try:
            inside = Path(filename).resolve().relative_to(_INSTALLATION_ROOT).as_posix()
        except (OSError, ValueError):
            inside = None
        if inside is not None:
            # Inside the installation the relative path already names the code.
            # Only a dependency is reduced further: the virtual environment sits
            # in here too, and relative to the root it would read as
            # `.venv/lib/python3.12/site-packages/…` - this machine's layout
            # rather than the package's identity.
            return _package_path(inside, _DEPENDENCY_MARKERS) or inside

    return _package_path(filename, _PACKAGE_PATH_MARKERS) or _last_segment(filename)


def _package_path(filename: str, markers: tuple[str, ...]) -> str | None:
    """The part of a path that names a package rather than a machine."""
    parts = tuple(part for part in filename.replace("\\", "/").split("/") if part)
    lowered = tuple(part.casefold() for part in parts)
    for marker in markers:
        if marker in lowered:
            return "/".join(parts[lowered.index(marker) :])
    return None


def _last_segment(filename: str) -> str:
    parts = tuple(part for part in filename.replace("\\", "/").split("/") if part)
    return parts[-1] if parts else "<unknown>"


def _write(
    event: dict[str, Any],
    *,
    exc: Exception | None = None,
    level: int = logging.ERROR,
) -> bool:
    """Write one diagnostic. Returns whether it was written at all.

    The caller needs the answer: a message that sends somebody to look up an
    entry is wrong when diagnostics are off, which since task 161 is the
    ordinary state rather than the exception.
    """
    try:
        if db.get_setting("debug_logging") != "1":
            return False
        if exc is not None:
            event["frames"] = _traceback_frames(exc)
        message = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
        if level == logging.INFO:
            _logger.info(message)
        else:
            _logger.error(message)
        return True
    except Exception:
        # Writing a diagnostic must not replace the response it describes.
        pass
    return False
