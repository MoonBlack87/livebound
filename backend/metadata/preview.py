"""Produce the placeholder CivitAI shows while an image loads.

CivitAI's web client computes a blurhash before the upload and sends it along as
``hash``. Without it the edit view keeps an empty or broken placeholder - that is
the visible difference between a post created in the browser and one created over
the API.

The parameters are deliberately the same as there (``src/utils/blurhash.ts``):
longest edge to 64 px, then 4x4 components. Other values yield a valid but
different-looking placeholder.
"""

from __future__ import annotations

from pathlib import Path

#: How far the image is scaled down before encoding. A blurhash describes only
#: coarse colour areas; more pixels cost time and barely change the result.
MAX_EDGE = 64

#: Resolution of the placeholder. 4x4 yields the 36 characters CivitAI stores.
COMPONENTS = (4, 4)

#: Said once per process, not once per image.
_WARNED_MISSING = False


def clamped_size(width: int, height: int, maximum: int = MAX_EDGE) -> tuple[int, int]:
    """Clamp to the longest edge, keeping the aspect ratio."""
    if width >= height and width > maximum:
        return maximum, max(1, round(height / width * maximum))
    if height > width and height > maximum:
        return max(1, round(width / height * maximum)), maximum
    return width, height


def blurhash_for(path: Path) -> str | None:
    """Blurhash of an image file, or ``None`` if it cannot be read.

    A missing placeholder is a cosmetic flaw, not a reason to abort a push - which
    is why a failure on one file is swallowed.

    A *missing library* is a different thing and is not swallowed quietly: it
    means every image goes up without a placeholder, forever, and that is the
    one reason the tRPC create path exists at all. It was undeclared for a whole
    release and nobody noticed, because this function answered ``None`` either
    way. It says so once now.
    """
    try:
        import blurhash
    except ImportError:  # pragma: no cover - a broken install, not a bad file
        global _WARNED_MISSING
        if not _WARNED_MISSING:
            _WARNED_MISSING = True
            print(
                "[blurhash] the blurhash library is missing, so uploads carry no "
                "loading placeholder. Reinstall with: uv pip install -e ."
            )
        return None

    try:
        from PIL import Image

        with Image.open(path) as image:
            image = image.convert("RGB")
            width, height = clamped_size(image.width, image.height)
            small = image.resize((width, height), Image.Resampling.LANCZOS)
            pixels = small.load()
            return blurhash.encode(
                [[pixels[x, y] for x in range(width)] for y in range(height)], *COMPONENTS
            )
    except Exception:
        # One unreadable file must not stop a push.
        return None
