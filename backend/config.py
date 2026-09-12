"""Paths, app identity and the CivitAI constants the whole app has to agree on.

The CivitAI numbers here are not guesses: each one is transcribed from the
civitai/civitai source and the comment names where it lives. They are collected
in one place because several of them (the 60 minute lead time above all) have to
be enforced identically by the scheduler, the validator and the push pipeline.
"""

from __future__ import annotations

import json
import os
import threading
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

APP_NAME = "livebound"
APP_VERSION = "1.0.0"
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"
PYTHON_VERSION_FLOOR = (3, 12)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_APPLICATION_ROOT = _PROJECT_ROOT
with (_PROJECT_ROOT / "pyproject.toml").open("rb") as _project_file:
    _PROJECT_METADATA = tomllib.load(_project_file)["project"]
APP_LICENCE = _PROJECT_METADATA["license"]
APP_RIGHTS_HOLDER = _PROJECT_METADATA["authors"][0]["name"]


_local = threading.local()

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8430

DEFAULT_USAGE_PING_URL = "https://livebound.legandor.com/ping"

# A first-start import is staged before SQLite can validate it. Keep that
# temporary file bounded even when the browser sends an accidental large file.
SETUP_BACKUP_MAX_BYTES = 512 * 1024**2


def usage_ping_url() -> str:
    return os.environ.get("LIVEBOUND_USAGE_PING_URL", "").strip() or DEFAULT_USAGE_PING_URL

# --- CivitAI endpoints ------------------------------------------------------

#: Official MCP server for upload_image, get_post, publish_post, delete_post and
#: whoami. Scheduling and full image metadata use the tRPC surface instead.
#:
#: Fixed to .com: there is no mcp.civitai.red - the host does not resolve.
MCP_URL = "https://mcp.civitai.com/mcp"

#: Which CivitAI domain the REST and tRPC surfaces use, and links point at.
#:
#: Default is .red, not .com, and that is not cosmetic: civitai.com restricts
#: results to SFW in some regions, so a listing fetched from there can silently
#: omit images that exist. Both domains serve the same site, the same REST API
#: and the same tRPC surface - verified against both.
DEFAULT_SITE_BASE = "https://civitai.red"
CIVITAI_OFFICIAL_CREATOR = "CivitaiOfficial"

#: Hosts the CivitAI credential may be sent to. Every REST and tRPC call carries an
#: `Authorization: Bearer` header with write and delete access to a public
#: account, so where that goes is not a free choice: a mistyped or pasted value
#: would hand the credential to a stranger, and `http://` would hand it to
#: anyone on the path. Overridable for a local mock, the same way the host guard
#: is - deliberately an environment variable, not a setting, so it cannot be
#: reached from the browser.
ALLOWED_SITE_HOSTS = ("civitai.red", "civitai.com")

#: CivitAI's identity host. It is not the API host and does not follow the
#: configured site: the OAuth provider lives in one place for both domains.
DEFAULT_OAUTH_BASE = "https://auth.civitai.com"

#: The public OAuth client this application is registered as. A public client:
#: it runs on the user's own machine and cannot keep a secret, which is why the
#: flow uses PKCE instead (`civitai/oauth.py`). Registered for exactly the scope
#: below and no more - the registration is the ceiling, not the request.
CIVITAI_OAUTH_CLIENT_ID = "114baae9-14ae-438e-a027-99a138de7fd0"

#: Where CivitAI sends the browser back to after the user consents. Only the
#: protocol and this path have to match what was registered - RFC 8252 lets a
#: loopback redirect use any port, which is what makes a local application
#: workable at all.
OAUTH_REDIRECT_PATH = "/api/oauth/callback"

#: What this application asks for:
#: ``UserRead(1) | ModelsRead(4) | MediaRead(32) | MediaWrite(64) | MediaDelete(128)``.
#: CivitAI takes the scope as a **decimal bitmask**, not as space-separated
#: names - a standard OAuth client library gets this wrong.
#:
#: This is the *requested* set, and it is deliberately only what the application
#: actually uses: asking for a permission nothing exercises puts a line on the
#: user's consent screen that the program then never honours. The registered
#: client's ``allowedScopes`` is a separate, wider ceiling. Widening this
#: constant later needs no new client and no re-registration - CivitAI bounces
#: the user back to the consent screen whenever a request asks for more than was
#: remembered, and ``oauthClient.update`` can raise the ceiling if it is too low
#: (unlike ``grants``, which cannot be changed at all).
OAUTH_SCOPE = 1 | 4 | 32 | 64 | 128

#: Refresh this long before the access token actually expires, so a call that
#: starts just inside the window still carries a valid token when it arrives.
OAUTH_REFRESH_MARGIN_SECONDS = 60


def oauth_base() -> str:
    """The OAuth provider's base URL, without a trailing slash."""
    value = os.environ.get("LIVEBOUND_OAUTH_BASE", "").strip().rstrip("/")
    return value or DEFAULT_OAUTH_BASE


def site_host_allowed(url: str) -> bool:
    """Is this a host the token may be sent to, over a channel that protects it?"""
    extra = tuple(
        name.strip().lower()
        for name in os.environ.get("LIVEBOUND_ALLOWED_SITES", "").split(",")
        if name.strip()
    )
    try:
        parsed = urlsplit(url)
        if parsed.username is not None or parsed.password is not None:
            return False
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if parsed.scheme == "https":
        allowed = ALLOWED_SITE_HOSTS + extra
    elif parsed.scheme == "http":
        allowed = extra
    else:
        return False
    return any(host == name or host.endswith(f".{name}") for name in allowed)

#: Where uploaded images are served from. The path segment after the host is a
#: fixed delivery namespace; it is re-learned from any full image URL the REST
#: API returns, so a change on their side heals itself.
DEFAULT_IMAGE_BASE = "https://image.civitai.com/xG1nkqKTMzGDvpLrqFT7WA"


def site_base() -> str:
    """The configured CivitAI domain, without a trailing slash."""
    from . import db

    value = (db.get_setting("site_base") or "").strip().rstrip("/")
    return value or DEFAULT_SITE_BASE


def trpc_base() -> str:
    """Internal tRPC surface. Used ONLY for what MCP cannot express - above all
    ``post.update``, the single way to set a future publishedAt."""
    return f"{site_base()}/api/trpc"


def rest_base() -> str:
    """Public REST API (model lookup by hash, model search, own images)."""
    return f"{site_base()}/api/v1"


def image_base() -> str:
    from . import db

    value = (db.get_setting("image_base") or "").strip().rstrip("/")
    return value or DEFAULT_IMAGE_BASE


def image_url(key: str, *, width: int | None = None, original: bool = False) -> str:
    """Delivery URL for an uploaded image, addressed by its upload key.

    ``post.getEdit`` returns only that key, so this is the only way to show an
    image whose local file we do not have - an adopted post, for instance.
    """
    transform = "original=true" if original else f"width={width or 450}"
    return f"{image_base()}/{key}/{transform}"

# --- CivitAI rules ----------------------------------------------------------

#: server/common/constants.ts :: POST_MINIMUM_SCHEDULE_MINUTES
POST_MINIMUM_SCHEDULE_MINUTES = 60

#: CivitAI's editor limits a post to 20 images; we deliberately keep that
#: interface limit although no server-side enforcement was found. Twenty is
#: learned behaviour, relying on a missing API check would build on a defect,
#: and the effect of larger posts on CivitAI's index is not worth discovering.
CIVITAI_POST_IMAGE_LIMIT = 20

#: components/Post/EditV2/SchedulePostModal.tsx - the modal's maxDate.
POST_MAXIMUM_SCHEDULE_MONTHS = 3

#: server/common/constants.ts :: constants.mediaUpload
MAX_IMAGE_FILE_SIZE = 50 * 1024**2

#: MCP `upload_image` carries the file base64 encoded inside a JSON-RPC body,
#: which inflates it by ~33%. This is our own ceiling, well below CivitAI's
#: 50 MB, above which we use the presign+PUT fallback instead.
MCP_UPLOAD_MAX_BYTES = 10 * 1024**2

#: An upload UUID that was never consumed by a create_post goes stale; reusing a
#: stale one makes create_post fail, so past this age we simply upload again.
UPLOAD_UUID_TTL_HOURS = 12

# --- local behaviour --------------------------------------------------------

SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

#: Produced during the scan, for the default grid.
THUMBNAIL_MAX_EDGE = 512
#: Produced on first request, for the two larger grid steps.
THUMBNAIL_LARGE_EDGE = 768

#: Hamming distance between two 64-bit dHashes below which two images are
#: considered "the same picture". 6 is deliberately conservative: a series of
#: near-identical portraits from one LoRA must not all warn against each other.
PHASH_DISTANCE_THRESHOLD = 6

#: Filename masks used to decide which duplicate was generated first. The
#: tokens are parsed by ``duplicates.generation_time``; keeping the default
#: here prevents the settings endpoint and duplicate ordering from drifting.
DEFAULT_GENERATION_TIME_PATTERNS = ("yyyymmddHHMMSS--seed",)

HASH_CACHE_STALE_DAYS = 14

#: Local diagnostics are deliberately small and short-lived: the active file
#: plus two rotated copies occupy at most 3 MiB in the data directory.
DIAGNOSTIC_LOG_FILENAME = "diagnostics.log"
DIAGNOSTIC_LOG_MAX_BYTES = 1024**2
DIAGNOSTIC_LOG_BACKUP_COUNT = 2

#: User-facing discovery requests one explicit newest-first ``post.getInfinite``
#: page at a time. CivitAI accepts at most 200 rows and returns an opaque cursor.
DISCOVER_DEFAULT_LIMIT = 100
DISCOVER_MAX_LIMIT = 200
DISCOVER_MAX_DAYS = 3650


def config_file() -> Path:
    """Where the data directory's location is remembered.

    Not in the database: that is the thing being located. Every application
    root owns a small local pointer, so separate installations stay separate.
    """
    return _APPLICATION_ROOT / ".livebound" / "config.json"


#: Cached because ``data_dir()`` is called on nearly every request and reading a
#: file each time would be pure waste. Cleared by :func:`set_data_dir`.
_pointer: Path | None = None
_pointer_read = False


def _pointed_data_dir() -> Path | None:
    global _pointer, _pointer_read
    if _pointer_read:
        return _pointer
    _pointer_read = True
    try:
        raw = json.loads(config_file().read_text(encoding="utf-8"))
        value = str(raw.get("data_dir") or "").strip()
        _pointer = Path(value).expanduser() if value else None
    except (OSError, ValueError):
        _pointer = None
    return _pointer


def default_data_dir() -> Path:
    return _APPLICATION_ROOT / ".livebound" / "data"


def data_dir_source() -> str:
    """Which of the three configured locations currently wins."""
    if os.environ.get("LIVEBOUND_DATA_DIR"):
        return "environment"
    if _pointed_data_dir() is not None:
        return "pointer"
    return "default"


def setup_required() -> bool:
    """Whether a first start still needs an explicit data-directory choice."""
    if os.environ.get("LIVEBOUND_DATA_DIR"):
        return False
    return _pointed_data_dir() is None


@contextmanager
def provisional_data_dir(path: Path) -> Iterator[None]:
    """Select an unremembered setup target for work on the current thread."""
    previous = getattr(_local, "provisional_data_dir", None)
    _local.provisional_data_dir = path
    try:
        yield
    finally:
        _local.provisional_data_dir = previous


def data_dir(*, create: bool = True) -> Path:
    """Where the database and the thumbnail cache live.

    Three sources, in this order: the environment, the installation-local
    pointer file, the installation-local default. The environment stays on top
    because that is how the test suite points the whole app at a tmp_path.
    Before any source has been chosen the default is returned but not created:
    the first-start screen owns that decision. Read-only tooling may resolve a
    configured path without creating it.
    """
    override = os.environ.get("LIVEBOUND_DATA_DIR")
    pointed = _pointed_data_dir() if not override else None
    provisional = getattr(_local, "provisional_data_dir", None)
    path = (
        Path(override).expanduser()
        if override
        else pointed or provisional or default_data_dir()
    )
    if create and (override or pointed is not None or provisional):
        path.mkdir(parents=True, exist_ok=True)
    return path


def set_data_dir(path: Path | None) -> None:
    """Remember a new location, or forget it and fall back to the default."""
    global _pointer, _pointer_read
    target = config_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"data_dir": str(path) if path else None}, indent=2) + "\n",
        encoding="utf-8",
    )
    _pointer = path
    _pointer_read = True


def db_path(*, create_parent: bool = True) -> Path:
    return data_dir(create=create_parent) / "publisher.db"


def diagnostic_log_path() -> Path:
    return data_dir() / DIAGNOSTIC_LOG_FILENAME


def thumbnail_dir() -> Path:
    path = data_dir() / "thumbnails"
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_root() -> Path:
    return _PROJECT_ROOT


# Deliberate fallback with no current production caller; test/live_smoke.py and
# the test fixtures set LIVEBOUND_PENDING_DIR to keep it isolated.
def pending_images_dir() -> Path:
    """Staging area for downloaded images of posts that are not public yet.

    Deliberately inside the project and excluded from version control: this is an
    intermediate state, not a store. These files reach the archive through the
    manual move - like everything else.

    The environment variable is not decoration: without it the tests write into
    exactly this project folder, and that promptly happened during a cleanup.
    """
    override = os.environ.get("LIVEBOUND_PENDING_DIR")
    path = Path(override).expanduser() if override else project_root() / "tmp_images"
    path.mkdir(parents=True, exist_ok=True)
    return path


def adopted_images_dir() -> Path | None:
    """The maintainer-chosen store for images fetched from adopted posts."""
    from . import db

    raw = (db.get_setting("adopted_folder") or "").strip()
    return Path(raw) if raw else None


def frontend_dist() -> Path:
    return _PROJECT_ROOT / "frontend" / "dist"


def shipped_licence() -> Path:
    """The licence file that accompanies this application."""
    return _PROJECT_ROOT / "LICENSE"


#: CivitAI's content rating per image, a bitmask.
#: `NsfwLevel` in `src/server/common/enums.ts:250` and the labels in
#: `src/shared/constants/browsingLevel.constants.ts:32`, read from the local
#: checkout at revision `e9155069`. `0` is not PG - it means CivitAI has not
#: rated the image yet, which can take minutes after an upload.
NSFW_LEVEL_LABELS = {
    1: "PG",
    2: "PG-13",
    4: "R",
    8: "X",
    16: "XXX",
    32: "Blocked",
}
