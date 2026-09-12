"""Content hashes for duplicate detection.

Three progressively broader questions, three hashes:

* ``file_sha256`` - "are these the exact same bytes?" Cheap, exact, and what the
  push pipeline uses to prove a planned file has not changed since.
* ``pixel_sha256`` - "are these the exact same RGB picture?" Covers dimensions
  and decoded pixels, survives metadata edits and lossless container changes,
  but not a resize or lossy re-encode.
* ``dhash64`` - "does this look like the same picture?" Survives a re-encode, a
  resize or a different container, which an exact pixel hash cannot.

Neither ever mutates the source: hashing opens it read-only. Edited uploads keep
this source identity even though their temporary container metadata differs.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

#: One megabyte at a time - shared with the model-folder hashing, which walks
#: multi-gigabyte checkpoints and must not hold one in memory.
CHUNK = 1024 * 1024


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pixel_digest(image: Image.Image) -> str:
    rgb = image.convert("RGB")
    header = f"{rgb.mode}:{rgb.width}x{rgb.height}|".encode("ascii")
    digest = hashlib.sha256(header)
    digest.update(rgb.tobytes())
    return digest.hexdigest()


def pixel_sha256(path: Path, image: Image.Image | None = None) -> str | None:
    """SHA-256 of the decoded RGB dimensions and buffer, or ``None`` on failure.

    Normalising to RGB makes palette and alpha-channel differences irrelevant:
    this answers whether the picture itself has identical geometry and pixels
    after decoding, independently of its container and metadata.
    """
    try:
        if image is not None:
            return _pixel_digest(image)
        with Image.open(path) as opened:
            return _pixel_digest(opened)
    # An unreadable or truncated file must not abort a scan over thousands of
    # them; the image simply has no exact-pixel answer (guardrails, rule 11).
    except Exception:
        # One unreadable image must not abort the scan batch it belongs to.
        return None


def _dhash_pixels(image: Image.Image) -> bytes:
    image.draft("L", (32, 32))  # JPEG fast path; a no-op for PNG
    small = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    # "L" is one byte per pixel in row-major order, so the raw buffer is
    # already the pixel list - and it sidesteps getdata()'s deprecation.
    return small.tobytes()


def dhash64(path: Path, image: Image.Image | None = None) -> str | None:
    """64-bit difference hash, returned as 16 hex characters.

    Rows of a 9x8 greyscale thumbnail are compared pixel to pixel; each "is the
    left pixel brighter than the right one" answer is one bit. Gradients survive
    rescaling and re-encoding, which is exactly the case a byte hash misses.

    Returns None for anything Pillow cannot open - an unreadable file must not
    abort a scan.
    """
    try:
        if image is not None and image.format != "JPEG":
            pixels = _dhash_pixels(image)
        else:
            # JPEG must be opened here so draft() reaches its decoder before
            # another shared-image operation loads the full-resolution pixels.
            with Image.open(path) as opened:
                pixels = _dhash_pixels(opened)
    except Exception:
        # One unreadable image must not abort the scan batch it belongs to.
        return None

    bits = 0
    for row in range(8):
        offset = row * 9
        for col in range(8):
            bits <<= 1
            if pixels[offset + col] > pixels[offset + col + 1]:
                bits |= 1
    return f"{bits:016x}"


def hamming(left: str | None, right: str | None) -> int:
    """Bit distance between two hex dHashes. 64 (the maximum) when either is absent."""
    if not left or not right:
        return 64
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return 64
