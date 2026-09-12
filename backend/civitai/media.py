"""Fetch delivered files from CivitAI.

Small and standalone because two places need it: recording older posts, and
matching an adopted post to local files. Both download a file only to hash it, and
throw it away again right after.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from .. import config
from .errors import TransportError

_TIMEOUT = httpx.Timeout(120.0, connect=15.0)


def download(url: str, target: Path, *, max_bytes: int | None = None) -> None:
    """Stream it, do not load it into memory - originals run to tens of MB.

    Bounded, because the URL comes out of a CivitAI response rather than from
    here: a reply with no Content-Length and no end would otherwise fill the
    disk, and what fills up is the image archive - the one folder holding
    pictures that exist nowhere else. A partial file is removed rather than left
    for the next scan to ingest as a corrupt image.
    """
    ceiling = max_bytes or config.MAX_IMAGE_FILE_SIZE
    try:
        with (
            httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as http,
            http.stream("GET", url, headers={"User-Agent": config.USER_AGENT}) as response,
        ):
            response.raise_for_status()
            written = 0
            with target.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 256):
                    written += len(chunk)
                    if written > ceiling:
                        raise TransportError(
                            f"The download exceeded {ceiling / 1024**2:.0f} MB and was stopped."
                        )
                    handle.write(chunk)
    except httpx.HTTPError as exc:
        target.unlink(missing_ok=True)
        raise TransportError(f"File not retrievable: {exc}") from exc
    except TransportError:
        target.unlink(missing_ok=True)
        raise
