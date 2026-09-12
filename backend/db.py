"""SQLite access layer.

One connection per thread (FastAPI runs sync endpoints in a threadpool), WAL mode so reads and
the background push/scan jobs do not block each other.

One rule governs every caller: **never hold a transaction open across a network call.** The push
pipeline talks to CivitAI between its steps, and a 13 MB base64 upload inside a write transaction
would freeze the whole UI. Every step is therefore ``commit -> HTTP -> commit``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

SCHEMA_VERSION = 32
MIGRATE_WITHOUT_BACKUP_ENV = "LIVEBOUND_MIGRATE_WITHOUT_BACKUP"

_local = threading.local()

#: Bumped when the database moves. ``get_connection`` compares against it, so a
#: thread that has been holding a connection since before the move reopens
#: instead of writing into the file that was left behind.
_generation = 0

#: Set while the data directory is being moved; see :func:`frozen`.
_freeze_owner: int | None = None


class DatabaseBusy(RuntimeError):
    """The database is briefly unavailable because its file is being moved."""


class MigrationBackupError(RuntimeError):
    """The safety copy failed, so the pending migration was not started."""


def now_iso() -> str:
    """The one timestamp format used everywhere: UTC, second precision, trailing ``Z``.

    Scheduling is the app's core job and a 60-minute boundary is unforgiving, so no local time
    and no naive datetime is ever persisted.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.db_path(), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_connection() -> sqlite3.Connection:
    owner = _freeze_owner
    if owner is not None and owner != threading.get_ident():
        raise DatabaseBusy("The data directory is being moved. Try again in a moment.")
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "generation", 0) != _generation:
        conn.close()
        conn = None
    if conn is None:
        conn = _connect()
        _local.conn = conn
        _local.generation = _generation
    return conn


def close_connection() -> None:
    """Drop this thread's connection. Push worker threads call this when they finish."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def relocated() -> None:
    """The database file moved; every cached connection is now stale.

    Only this thread's can be closed from here - the others notice on their next
    ``get_connection`` and reopen. That is enough because the move happens inside
    :func:`frozen`, so no other thread is holding one open across it.
    """
    global _generation
    _generation += 1
    close_connection()


@contextmanager
def frozen() -> Iterator[None]:
    """Shut every other thread out of the database for the duration.

    The move copies the file and then repoints at the copy, and anything written
    to the original in between would be silently thrown away. Refusing a job is
    not enough - an ordinary request writes on its own thread too - so the door
    is closed rather than watched: :func:`get_connection` raises
    :class:`DatabaseBusy` for everyone else, which the API answers with a 503.
    """
    global _freeze_owner
    if _freeze_owner is not None:
        raise DatabaseBusy("The data directory is already being moved.")
    _freeze_owner = threading.get_ident()
    try:
        yield
    finally:
        _freeze_owner = None


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """A write transaction, held from the first statement to the commit.

    ``BEGIN IMMEDIATE`` rather than the default, because the default is not a
    transaction at all for the part that matters: sqlite3 in its legacy mode
    emits ``BEGIN`` before the first *write*, so a ``SELECT`` inside this block
    runs outside any transaction and another thread can change what it read.

    That is not academic here. ``store/images.py::replace_resources`` reads the
    user's resource override flags, then deletes and re-inserts the rows. A scan
    on a job thread and an edit on a request thread overlap the moment the user
    keeps working while a scan runs - and the flag they just set is read as
    unset and silently thrown away.

    ``IMMEDIATE`` takes the write lock up front, so the read is protected too.
    The 30 s busy timeout on the connection is what makes that safe rather than
    a source of "database is locked".
    """
    conn = get_connection()
    # A nested call would fail on the BEGIN; the outer block already holds the
    # lock and its commit covers this work.
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    """Create the schema if needed. Idempotent."""
    conn = get_connection()
    schema_sql = (Path(__file__).resolve().parent / "db_schema.sql").read_text(encoding="utf-8")
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    rebuild_image_search = not _has_table(conn, "image_search")

    # Migrations run *before* the schema script: the script creates indexes over columns that
    # older databases do not have yet, and CREATE INDEX would fail on those.
    if current < SCHEMA_VERSION:
        _safety_backup(current)
        _migrate(conn, current)

    conn.executescript(schema_sql)

    if rebuild_image_search:
        _create_image_search(conn)

    if current < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    _regular_backup()


def _safety_backup(from_version: int) -> None:
    """Put a copy aside before every structural change.

    A migration writes into the only copy there is. If something goes wrong there,
    it cannot be repaired without a backup - and the user finds out only when the
    application no longer starts.

    """
    if not config.db_path().exists():
        return
    has_tables = get_connection().execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
    ).fetchone()
    if has_tables is None:
        return

    if os.environ.get(MIGRATE_WITHOUT_BACKUP_ENV) == "1":
        print(
            f"[startup] {MIGRATE_WITHOUT_BACKUP_ENV}=1: safety backup skipped; "
            "the migration is running without a backup."
        )
        return

    from . import backup

    backup_path = config.data_dir(create=False) / "backups"
    try:
        backup.create(reason=f"before-migration-v{from_version}", automatic=True)
    except (OSError, sqlite3.Error) as exc:
        raise MigrationBackupError(
            f"Safety backup before the database migration failed: {exc}. "
            f"No migration was run. Backup path: {backup_path}. "
            f"To start anyway, set {MIGRATE_WITHOUT_BACKUP_ENV}=1."
        ) from exc


def _regular_backup() -> None:
    """Take the due daily backup without making a precaution block startup."""
    try:
        from . import backup

        backup.create_regular_if_due()
    except (OSError, sqlite3.Error) as exc:
        print(
            "[startup] regular backup failed; create one from Settings before "
            f"closing the application: {exc}"
        )


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return rows.fetchone() is not None


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Idempotent ``ALTER TABLE ... ADD COLUMN`` (SQLite has no ``IF NOT EXISTS`` for this).

    A missing table means a brand-new database, where ``db_schema.sql`` creates the column
    anyway - so there is nothing to migrate.
    """
    if not _has_table(conn, table):
        return
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """Bring an older database up to :data:`SCHEMA_VERSION`.

    This runs *before* ``db_schema.sql``, because that script creates indexes
    over columns an older database does not have yet - and ``CREATE INDEX`` on a
    missing column aborts the whole script, leaving the app unable to open an
    existing database at all.

    ``_add_column`` is idempotent, so a migration may safely re-state a column
    that is already there.
    """
    if from_version < 2:
        # The duplicate check grew beyond "same bytes?": identical decoded
        # pixels are also certain, while every perceptual candidate is probable.
        # Generation fields remain useful comparison details.
        _add_column(conn, "image_usage", "origin", "TEXT NOT NULL DEFAULT 'push'")
        _add_column(conn, "image_usage", "seed", "TEXT")
        _add_column(conn, "image_usage", "prompt_hash", "TEXT")
        # Banded perceptual index, so a library page costs one query instead of
        # one full scan per tile.
        for band in range(8):
            _add_column(conn, "image_usage", f"phash_b{band}", "INTEGER")
        # Hashes are written at three different lengths by different generators;
        # everything compares on a common 10-character prefix.
        _add_column(conn, "resource_map", "hash_prefix", "TEXT NOT NULL DEFAULT ''")
        _add_column(conn, "image_resources", "hash_prefix", "TEXT NOT NULL DEFAULT ''")
        # Relative planning, added after the first schema.
        _add_column(conn, "posts", "schedule_mode", "TEXT NOT NULL DEFAULT 'relative'")
        _add_column(conn, "posts", "schedule_offset_minutes", "INTEGER")
        _backfill_bands(conn)
    if from_version < 3:
        # The loading placeholder, which the earlier upload path could not send.
        _add_column(conn, "post_images", "blurhash", "TEXT")
    if from_version < 4:
        # The archive folder: a move target for images that are already public.
        _add_column(conn, "source_roots", "is_archive", "INTEGER NOT NULL DEFAULT 0")
    if from_version < 5:
        # Videos are handled outside this app; the rows are kept but skipped.
        _add_column(conn, "post_images", "media_type", "TEXT NOT NULL DEFAULT 'image'")
    if from_version < 6:
        # Thumbnails are addressed by name, so the data directory can move
        # without every row pointing nowhere.
        _basename_thumbnails(conn)
    if from_version < 7:
        # The banded perceptual index on the library itself, so finding the
        # pictures that look alike is not a pass over every pair.
        for band in range(8):
            _add_column(conn, "images", f"phash_b{band}", "INTEGER")
        _backfill_image_bands(conn)
    if from_version < 8:
        # Metadata the user changed, kept out of the file itself.
        for column in ("locked_by_user", "added_by_user", "deleted_by_user"):
            _add_column(conn, "image_resources", column, "INTEGER NOT NULL DEFAULT 0")
    if from_version < 15:
        # Ahead of the re-extraction below, which resolves a prompt tag through
        # `model_files.file_stem`: added later, that lookup would raise on a
        # missing column and be swallowed, leaving the re-extraction quietly
        # without it. Existing hashes stay valid - the next model-folder scan
        # reads only the cheap safetensors header to fill these.
        _add_column(conn, "model_files", "file_stem", "TEXT NOT NULL DEFAULT ''")
        _add_column(conn, "model_files", "ss_output_name", "TEXT")
        _add_column(conn, "model_files", "modelspec_title", "TEXT")
    if from_version < 16:
        # v9 first corrected the extractor's matching rules. v16 deliberately
        # repeats the derivation after `Hires upscaler` became evidence, then
        # applies the cache so newly created rows receive known identities.
        _reextract_image_resources(conn)
    if from_version < 10:
        _strip_derived_resource_edits(conn)
    if from_version < 11:
        # Byte size belongs to the staged asset: post image rows may be rebuilt
        # from the library while the same unconsumed UUID remains reusable.
        _add_column(conn, "upload_assets", "file_size", "INTEGER")
    if from_version < 12:
        # Decoding every existing image at startup would make the migration
        # unbounded. The next ordinary scan fills these columns instead.
        _add_column(conn, "images", "pixel_sha256", "TEXT")
        _add_column(conn, "image_usage", "pixel_sha256", "TEXT")
    if 12 <= from_version < 13:
        # v12 omitted image geometry from the digest. Clearing the unsafe values
        # lets the next ordinary scan rebuild them without blocking startup.
        conn.execute("UPDATE images SET pixel_sha256=NULL")
        conn.execute("UPDATE image_usage SET pixel_sha256=NULL")
    if from_version < 17:
        _create_metadata_field_inventory(conn)
    if from_version < 18:
        # A trashed image keeps its row and original location so the move can
        # be undone without manufacturing a new library entry on restore.
        _add_column(conn, "images", "is_trashed", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "images", "trash_original_path", "TEXT")
        _add_column(conn, "images", "trashed_at", "TEXT")
    if from_version < 19:
        _create_image_search(conn)
    if from_version < 20:
        # Owner-visible image metadata survives CivitAI's delivery hiding. Keep
        # only the derived origin fact; existing rows stay unknown until sync.
        _add_column(
            conn,
            "post_images",
            "remote_on_site",
            "INTEGER CHECK(remote_on_site IN (0, 1) OR remote_on_site IS NULL)",
        )
    if from_version < 21:
        _create_duplicate_dismissals(conn)
    if from_version < 22:
        # Zero marks rows written before the scanner could tell whether its own
        # reading behaviour had changed. The next ordinary scan refreshes them.
        _add_column(conn, "images", "ingest_revision", "INTEGER NOT NULL DEFAULT 0")
    if from_version < 23:
        # NULL until a sync has read the rating from CivitAI. Deliberately
        # nullable: 0 would be indistinguishable from "not rated yet".
        _add_column(conn, "post_images", "nsfw_level", "INTEGER")
    if from_version < 24:
        # Zero: every existing folder keeps behaving exactly as before. The
        # periodic re-scan is opt-in, per folder.
        _add_column(conn, "source_roots", "watch_enabled", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "model_roots", "watch_enabled", "INTEGER NOT NULL DEFAULT 0")
    if from_version < 25:
        # Nullable on purpose: every existing suggestion was produced before the
        # seed was drawn by us, so its seed is genuinely unknown. A zero there
        # would claim a number that was never used.
        _add_column(conn, "llm_suggestions", "seed", "INTEGER")
    # Measured, not preferred: at 512 the model missed a detail the pictures
    # plainly show and invented one they do not, at no saving in time. And since
    # the contact sheet carries every picture at full tile size, the second image
    # only repeated one of them. A profile still holding an old default cannot be
    # told apart from one deliberately set to it - the value is moved anyway, per
    # column, and anything else is left alone. A missing table means a brand-new
    # database, where db_schema.sql creates the columns with the new defaults -
    # the same guard _add_column makes, since migrations run before the schema.
    if from_version < 26 and _has_table(conn, "llm_profiles"):
        conn.execute("UPDATE llm_profiles SET vision_max_side=768 WHERE vision_max_side=512")
        conn.execute("UPDATE llm_profiles SET max_images=1 WHERE max_images=2")
    # A profile's identity moves off its name and onto a UUID. This step is
    # mechanical on purpose: it hands every existing row a fresh one and does not
    # try to recognise anybody's profiles. Matching an old installation's German
    # seed names onto the shipped identities is a one-off, done by hand after a
    # backup, not something every database has to carry for ever.
    if from_version < 27 and _has_table(conn, "llm_profiles"):
        _add_column(conn, "llm_profiles", "uuid", "TEXT NOT NULL DEFAULT ''")
        for row in conn.execute("SELECT id FROM llm_profiles WHERE uuid=''").fetchall():
            conn.execute(
                "UPDATE llm_profiles SET uuid=? WHERE id=?", (str(uuid.uuid4()), row["id"])
            )
        # SQLite cannot add a UNIQUE column in one statement, so the constraint
        # arrives as an index - and only once every row holds a value, or it
        # would collide on the empty default.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_profiles_uuid ON llm_profiles(uuid)"
        )
        # DROP COLUMN needs SQLite 3.35. Nothing reads the column any more, so
        # leaving it in place is harmless - and a migration that cannot be taken
        # back must not fail on an older library. Version 28 rebuilds the table
        # and removes it for good, whatever happens here.
        try:
            conn.execute("ALTER TABLE llm_profiles DROP COLUMN is_builtin")
        except sqlite3.OperationalError as exc:
            print(f"llm_profiles.is_builtin stays, unread ({exc})", flush=True)

    # The name stops being the identity. `ALTER TABLE` cannot drop an inline
    # UNIQUE - SQLite backs it with an implicit autoindex that `DROP INDEX`
    # refuses - so the table is rebuilt. That is also what finally gives a
    # migrated database and a freshly created one the same shape: version 27
    # had to add `uuid` with a `DEFAULT ''`, because `ADD COLUMN` demands one
    # for a NOT NULL column, and the rebuild is what sheds it. Nothing holds a
    # foreign key on this table, so no reference breaks. Columns are copied by
    # name: a migrated table carries them in a different order.
    if from_version < 28 and _has_table(conn, "llm_profiles"):
        columns = (
            "id, uuid, name, description, system_prompt, title_max_words, tag_count,"
            " include_images, max_images, vision_max_side, max_new_tokens, temperature,"
            " top_p, top_k, sort_order, created_at"
        )
        conn.execute("DROP TABLE IF EXISTS llm_profiles_rebuild")
        conn.execute(
            """
            CREATE TABLE llm_profiles_rebuild (
                id              INTEGER PRIMARY KEY,
                uuid            TEXT NOT NULL,
                name            TEXT NOT NULL,
                description     TEXT NOT NULL DEFAULT '',
                system_prompt   TEXT NOT NULL,
                title_max_words INTEGER NOT NULL DEFAULT 7,
                tag_count       INTEGER NOT NULL DEFAULT 7,
                include_images  INTEGER NOT NULL DEFAULT 1,
                max_images      INTEGER NOT NULL DEFAULT 1,
                vision_max_side INTEGER NOT NULL DEFAULT 768,
                max_new_tokens  INTEGER NOT NULL DEFAULT 700,
                temperature     REAL NOT NULL DEFAULT 0.7,
                top_p           REAL NOT NULL DEFAULT 0.8,
                top_k           INTEGER NOT NULL DEFAULT 20,
                sort_order      INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"INSERT INTO llm_profiles_rebuild({columns}) SELECT {columns} FROM llm_profiles"
        )
        conn.execute("DROP TABLE llm_profiles")
        conn.execute("ALTER TABLE llm_profiles_rebuild RENAME TO llm_profiles")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_profiles_uuid ON llm_profiles(uuid)"
        )
    if (
        from_version < 29
        and _has_table(conn, "source_roots")
        and _has_table(conn, "settings")
    ):
        row = conn.execute(
            "SELECT value FROM settings WHERE key='trash_folder'"
        ).fetchone()
        trash_name = Path(row["value"].strip()).name if row and row["value"] else ""
        if trash_name:
            for source in conn.execute(
                "SELECT id, excluded_folders FROM source_roots"
            ).fetchall():
                try:
                    configured = json.loads(source["excluded_folders"] or "[]")
                except ValueError:
                    continue
                exclusions = [name for name in configured if name != trash_name]
                if exclusions != configured:
                    conn.execute(
                        "UPDATE source_roots SET excluded_folders=? WHERE id=?",
                        (json.dumps(exclusions), source["id"]),
                    )
    if from_version < 30 and _has_table(conn, "settings"):
        # Diagnostics used to be implicit. Make the fresh-install default
        # explicit without changing an existing choice to keep them on.
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('debug_logging', '0') "
            "ON CONFLICT(key) DO NOTHING"
        )
    if from_version < 31 and _has_table(conn, "settings"):
        conn.execute("DELETE FROM settings WHERE key=?", ("civitai_api_token",))
    if from_version < 32 and _has_table(conn, "settings"):
        # The client id could be replaced from the settings screen until
        # 2026-09-09. Nothing reads the row now, but leaving it would keep a
        # stranger's registration sitting in the database of anybody who once
        # pasted one.
        conn.execute("DELETE FROM settings WHERE key=?", ("civitai_oauth_client_id",))


def _create_duplicate_dismissals(conn: sqlite3.Connection) -> None:
    """Store explicit pairwise exceptions to automatic duplicate detection."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS duplicate_dismissals (
            image_id_a   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
            image_id_b   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
            dismissed_at TEXT NOT NULL,
            CHECK(image_id_a < image_id_b),
            PRIMARY KEY(image_id_a, image_id_b)
        )
        """
    )


def _create_image_search(conn: sqlite3.Connection) -> None:
    """Create and backfill the literal substring index when ``images`` exists."""
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS image_search USING fts5(
                relative_path,
                raw_infotext,
                content='images',
                content_rowid='id',
                tokenize='trigram'
            )
            """
        )
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "Image search requires SQLite with FTS5 trigram support. "
            "Use a Python SQLite build that provides ENABLE_FTS5 and the trigram tokenizer."
        ) from exc

    images_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='images'"
    ).fetchone()
    if images_exists is None:
        return
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS images_search_insert
        AFTER INSERT ON images
        BEGIN
            INSERT INTO image_search(rowid, relative_path, raw_infotext)
            VALUES (NEW.id, NEW.relative_path, NEW.raw_infotext);
        END;

        CREATE TRIGGER IF NOT EXISTS images_search_delete
        AFTER DELETE ON images
        BEGIN
            INSERT INTO image_search(image_search, rowid, relative_path, raw_infotext)
            VALUES ('delete', OLD.id, OLD.relative_path, OLD.raw_infotext);
        END;

        CREATE TRIGGER IF NOT EXISTS images_search_update
        AFTER UPDATE OF relative_path, raw_infotext ON images
        BEGIN
            INSERT INTO image_search(image_search, rowid, relative_path, raw_infotext)
            VALUES ('delete', OLD.id, OLD.relative_path, OLD.raw_infotext);
            INSERT INTO image_search(rowid, relative_path, raw_infotext)
            VALUES (NEW.id, NEW.relative_path, NEW.raw_infotext);
        END;
        """
    )
    try:
        conn.execute("INSERT INTO image_search(image_search) VALUES('rebuild')")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "Image search requires SQLite with FTS5 trigram support. "
            "Use a Python SQLite build that provides ENABLE_FTS5 and the trigram tokenizer."
        ) from exc


def _create_metadata_field_inventory(conn: sqlite3.Connection) -> None:
    """Create the scan-derived field inventory and its per-image membership."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata_field_inventory (
            field_name   TEXT PRIMARY KEY,
            image_count  INTEGER NOT NULL DEFAULT 0,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS image_metadata_fields (
            image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
            field_name TEXT NOT NULL REFERENCES metadata_field_inventory(field_name),
            PRIMARY KEY(image_id, field_name)
        );

        CREATE TRIGGER IF NOT EXISTS metadata_field_membership_insert
        AFTER INSERT ON image_metadata_fields
        BEGIN
            UPDATE metadata_field_inventory
            SET image_count = image_count + 1
            WHERE field_name = NEW.field_name;
        END;

        CREATE TRIGGER IF NOT EXISTS metadata_field_membership_delete
        AFTER DELETE ON image_metadata_fields
        BEGIN
            UPDATE metadata_field_inventory
            SET image_count = MAX(image_count - 1, 0)
            WHERE field_name = OLD.field_name;
        END;
        """
    )


def _reextract_image_resources(conn: sqlite3.Connection) -> None:
    """Correct stored credits from each image's already-scanned infotext."""
    try:
        rows = conn.execute(
            "SELECT id, raw_infotext FROM images WHERE raw_infotext IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return

    from .metadata import infotext, resources
    from .resources import cache
    from .store import images as image_store

    for row in rows:
        parsed = infotext.parse(row["raw_infotext"])
        found = resources.extract(parsed)
        cache.enrich(found)
        image_store.replace_resources(row["id"], found)


def _strip_derived_resource_edits(conn: sqlite3.Connection) -> tuple[int, int]:
    """Remove upload-only resource fields from stored edit documents."""
    try:
        rows = conn.execute("SELECT * FROM image_edits").fetchall()
    except sqlite3.OperationalError:
        return 0, 0

    from .metadata.resource_edits import RESOURCE_FIELDS

    derived = set(RESOURCE_FIELDS)
    updated = 0
    deleted_rows = 0
    for row in rows:
        try:
            draft = json.loads(row["draft_json"] or "{}")
        except (TypeError, ValueError):
            draft = {}
        try:
            touched = json.loads(row["touched_fields_json"] or "[]")
        except (TypeError, ValueError):
            touched = []
        try:
            deleted = json.loads(row["deleted_fields_json"] or "[]")
        except (TypeError, ValueError):
            deleted = []

        fields = draft.get("fields") if isinstance(draft, dict) else None
        had_derived = bool(
            (set(fields or {}) | set(touched or []) | set(deleted or [])) & derived
        )
        if not had_derived:
            continue
        if isinstance(fields, dict):
            draft["fields"] = {key: value for key, value in fields.items() if key not in derived}
        touched = [key for key in touched if key not in derived]
        deleted = [key for key in deleted if key not in derived]
        if not touched and not deleted:
            conn.execute("DELETE FROM image_edits WHERE image_id=?", (row["image_id"],))
            deleted_rows += 1
            continue
        conn.execute(
            "UPDATE image_edits SET draft_json=?, touched_fields_json=?,"
            " deleted_fields_json=? WHERE image_id=?",
            (
                json.dumps(draft, ensure_ascii=False),
                json.dumps(touched, ensure_ascii=False),
                json.dumps(deleted, ensure_ascii=False),
                row["image_id"],
            ),
        )
        updated += 1
    return updated, deleted_rows


def _backfill_image_bands(conn: sqlite3.Connection) -> None:
    """Fill the band columns for rows that predate them.

    Same reasoning as :func:`_backfill_bands`: without it the indexed lookup
    finds nothing for old rows, which is worse than a slow query because it is
    silent.
    """
    try:
        rows = conn.execute(
            "SELECT id, phash FROM images WHERE phash IS NOT NULL AND phash_b0 IS NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for row in rows:
        try:
            value = int(row["phash"], 16)
        except (ValueError, TypeError):
            continue
        bands = [(value >> (index * 8)) & 0xFF for index in range(8)]
        conn.execute(
            "UPDATE images SET phash_b0=?, phash_b1=?, phash_b2=?, phash_b3=?,"
            " phash_b4=?, phash_b5=?, phash_b6=?, phash_b7=? WHERE id=?",
            (*bands, row["id"]),
        )


def _basename_thumbnails(conn: sqlite3.Connection) -> None:
    """Turn stored absolute thumbnail paths into bare file names.

    The cache key is unchanged, so the files themselves stay valid - only the
    way a row addresses them changes.
    """
    try:
        rows = conn.execute(
            "SELECT id, thumbnail_path FROM images WHERE thumbnail_path LIKE '%/%'"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for row in rows:
        conn.execute(
            "UPDATE images SET thumbnail_path=? WHERE id=?",
            (row["thumbnail_path"].rsplit("/", 1)[-1], row["id"]),
        )


def _backfill_bands(conn: sqlite3.Connection) -> None:
    """Fill the band columns for rows that predate them.

    Without this the indexed lookup finds nothing for old rows - they would
    silently stop matching, which is worse than a slow query.
    """
    try:
        rows = conn.execute(
            "SELECT id, phash FROM image_usage WHERE phash IS NOT NULL AND phash_b0 IS NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for row in rows:
        try:
            value = int(row["phash"], 16)
        except (ValueError, TypeError):
            continue
        bands = [(value >> (index * 8)) & 0xFF for index in range(8)]
        conn.execute(
            "UPDATE image_usage SET phash_b0=?, phash_b1=?, phash_b2=?, phash_b3=?,"
            " phash_b4=?, phash_b5=?, phash_b6=?, phash_b7=? WHERE id=?",
            (*bands, row["id"]),
        )


# --- settings -----------------------------------------------------------------------------------


def get_setting(key: str, default: str | None = None) -> str | None:
    row = get_connection().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return default if row is None else row["value"]


def get_setting_read_only(key: str, default: str | None = None) -> str | None:
    """Read a setting without creating or migrating the configured database."""
    path = config.db_path(create_parent=False)
    if not path.is_file():
        return default
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    finally:
        conn.close()
    return default if row is None else row[0]


def int_setting(key: str, default: int) -> int:
    """A stored number, or the default when it is absent or not a number.

    Settings are written through validated endpoints, but the table is a plain
    key/value store on the maintainer's disk. One bad value must not take the
    whole settings request with it.
    """
    value = get_setting(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def set_setting(key: str, value: str | None) -> None:
    with transaction() as conn:
        if value is None:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )


def get_json_setting(key: str, default: Any = None) -> Any:
    raw = get_setting(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def set_json_setting(key: str, value: Any) -> None:
    set_setting(key, None if value is None else json.dumps(value, ensure_ascii=False))
