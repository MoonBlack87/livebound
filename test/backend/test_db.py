"""SQLite access invariants."""

import json
import sqlite3
from pathlib import Path

import pytest

from backend import config, db, duplicates
from backend.store import images as image_store
from backend.store import posts as post_store
from backend.store import sources as source_store


def test_reading_a_stored_setting_does_not_migrate_the_database():
    db.set_setting("civitai_api_token", "test-token")
    conn = db.get_connection()
    previous_version = db.SCHEMA_VERSION - 1
    conn.execute(f"PRAGMA user_version={previous_version}")
    conn.commit()
    db.close_connection()

    assert db.get_setting_read_only("civitai_api_token") == "test-token"

    conn = db.get_connection()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == previous_version


def test_a_failed_safety_backup_stops_before_the_migration(monkeypatch):
    from backend import backup

    previous_version = db.SCHEMA_VERSION - 1
    conn = db.get_connection()
    conn.execute(f"PRAGMA user_version={previous_version}")
    conn.commit()
    monkeypatch.setattr(db, "_regular_backup", lambda: None)
    monkeypatch.setattr(
        backup,
        "create",
        lambda **kwargs: (_ for _ in ()).throw(OSError("disk is full")),
    )

    with pytest.raises(db.MigrationBackupError) as caught:
        db.init_db()

    message = str(caught.value)
    assert "disk is full" in message
    assert str(config.data_dir(create=False) / "backups") in message
    assert "LIVEBOUND_MIGRATE_WITHOUT_BACKUP=1" in message
    assert conn.execute("PRAGMA user_version").fetchone()[0] == previous_version


def test_the_explicit_escape_hatch_skips_the_backup_and_migrates(
    monkeypatch, capsys
):
    from backend import backup

    previous_version = db.SCHEMA_VERSION - 1
    conn = db.get_connection()
    conn.execute(f"PRAGMA user_version={previous_version}")
    conn.commit()
    monkeypatch.setenv(db.MIGRATE_WITHOUT_BACKUP_ENV, "1")
    monkeypatch.setattr(db, "_regular_backup", lambda: None)
    monkeypatch.setattr(
        backup,
        "create",
        lambda **kwargs: (_ for _ in ()).throw(OSError("disk is full")),
    )

    db.init_db()

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    output = capsys.readouterr().out
    assert output.count("LIVEBOUND_MIGRATE_WITHOUT_BACKUP=1") == 1
    assert "safety backup skipped" in output


def test_a_working_safety_backup_is_unchanged(monkeypatch):
    from backend import backup

    previous_version = db.SCHEMA_VERSION - 1
    conn = db.get_connection()
    conn.execute(f"PRAGMA user_version={previous_version}")
    conn.commit()
    created = []
    monkeypatch.setattr(db, "_regular_backup", lambda: None)
    monkeypatch.setattr(
        backup,
        "create",
        lambda **kwargs: created.append(kwargs) or {"path": "safety.db"},
    )

    db.init_db()

    assert created == [
        {"reason": f"before-migration-v{previous_version}", "automatic": True}
    ]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


@pytest.mark.parametrize("empty_database", [False, True])
def test_a_fresh_database_needs_no_safety_backup(
    tmp_path, monkeypatch, capsys, empty_database
):
    from backend import backup

    data_dir = tmp_path / "fresh-data"
    if empty_database:
        data_dir.mkdir()
        sqlite3.connect(data_dir / "publisher.db").close()
    db.close_connection()
    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(data_dir))
    monkeypatch.setenv(db.MIGRATE_WITHOUT_BACKUP_ENV, "1")
    monkeypatch.setattr(
        backup,
        "create",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("nothing to copy")),
    )

    db._safety_backup(0)

    assert capsys.readouterr().out == ""


def test_app_startup_exits_with_only_the_migration_backup_message(monkeypatch):
    from backend import main

    message = "Safety backup failed. Set LIVEBOUND_MIGRATE_WITHOUT_BACKUP=1."
    monkeypatch.setattr(
        db,
        "init_db",
        lambda: (_ for _ in ()).throw(db.MigrationBackupError(message)),
    )

    with pytest.raises(SystemExit) as caught:
        main.create_app()

    assert caught.value.code == message
    assert caught.value.__suppress_context__ is True


def test_v29_migration_copy_removes_only_the_current_legacy_trash_name(
    tmp_path, monkeypatch
):
    first = source_store.add_root(
        str(tmp_path / "first"), excluded=["DublicatesTrash", "keep"]
    )
    second = source_store.add_root(
        str(tmp_path / "second"), excluded=["keep", "DublicatesTrash"]
    )
    third = source_store.add_root(str(tmp_path / "third"), excluded=["keep"])
    db.set_setting("trash_folder", str(tmp_path / "DublicatesTrash"))
    conn = db.get_connection()
    conn.execute("PRAGMA user_version=28")
    conn.commit()

    copy_dir = tmp_path / "migration-v29-copy"
    copy_dir.mkdir()
    copied = sqlite3.connect(copy_dir / "publisher.db")
    try:
        conn.backup(copied)
    finally:
        copied.close()

    db.close_connection()
    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(copy_dir))
    db.init_db()

    migrated = db.get_connection()
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    rows = {
        row["id"]: json.loads(row["excluded_folders"])
        for row in migrated.execute("SELECT id, excluded_folders FROM source_roots")
    }
    assert rows == {
        first["id"]: ["keep"],
        second["id"]: ["keep"],
        third["id"]: ["keep"],
    }


def test_v30_migration_keeps_enabled_diagnostics(tmp_path, monkeypatch):
    db.set_setting("debug_logging", "1")
    conn = db.get_connection()
    conn.execute("PRAGMA user_version=29")
    conn.commit()

    copy_dir = tmp_path / "migration-v30-copy"
    copy_dir.mkdir()
    copied = sqlite3.connect(copy_dir / "publisher.db")
    try:
        conn.backup(copied)
    finally:
        copied.close()

    db.close_connection()
    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(copy_dir))
    db.init_db()

    migrated = db.get_connection()
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.get_setting("debug_logging") == "1"


def test_v31_migration_removes_only_the_legacy_api_token(tmp_path, monkeypatch):
    db.set_setting("civitai_api_token", "legacy-token")
    db.set_setting("keep", "value")
    conn = db.get_connection()
    conn.execute("PRAGMA user_version=30")
    conn.commit()

    copy_dir = tmp_path / "migration-v31-copy"
    copy_dir.mkdir()
    copied = sqlite3.connect(copy_dir / "publisher.db")
    try:
        conn.backup(copied)
    finally:
        copied.close()

    db.close_connection()
    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(copy_dir))
    db.init_db()

    migrated = db.get_connection()
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.get_setting("civitai_api_token") is None
    assert db.get_setting("keep") == "value"


def test_immediately_previous_schema_migrates_generated_database(tmp_path, monkeypatch):
    data_dir = tmp_path / "previous-schema"
    data_dir.mkdir()
    database = data_dir / "publisher.db"
    fixture = sqlite3.connect(database)
    try:
        schema = (Path(__file__).resolve().parents[2] / "backend" / "db_schema.sql").read_text(
            encoding="utf-8"
        )
        fixture.executescript(schema)
        fixture.execute(
            "INSERT INTO settings(key, value) VALUES('trash_folder', ?)",
            (str(data_dir / "DublicatesTrash"),),
        )
        fixture.executemany(
            "INSERT INTO source_roots(id, path, label, excluded_folders, created_at) "
            "VALUES(?, ?, ?, ?, '2026-01-01T00:00:00Z')",
            [
                (1, "/fixture/first", "First", '["keep"]'),
                (2, "/fixture/second", "Second", '["keep"]'),
            ],
        )
        fixture.execute(f"PRAGMA user_version={db.SCHEMA_VERSION - 1}")
        fixture.commit()
        before = {
            table: fixture.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("settings", "source_roots")
        }
    finally:
        fixture.close()

    db.close_connection()
    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(data_dir))
    db.init_db()

    migrated = db.get_connection()
    after = {
        table: migrated.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert after == before
    exclusions = {
        row["id"]: json.loads(row["excluded_folders"])
        for row in migrated.execute("SELECT id, excluded_folders FROM source_roots ORDER BY id")
    }
    assert exclusions == {1: ["keep"], 2: ["keep"]}


def _search_ids(**filters) -> list[int]:
    """Ids for a filtered search, in id order - what the search tests compare.

    `search_candidates` is the product's own path through `_search_filters`;
    the store no longer carries an id-only variant for this test alone.
    """
    return [row["id"] for row in image_store.search_candidates(**filters)]


def test_image_search_is_literal_indexed_and_tracks_row_changes(tmp_path):
    root = source_store.add_root(str(tmp_path))
    absolute_path = str(tmp_path / 'Alpha%_*-"Café.png')
    image_id = image_store.upsert(
        root["id"],
        {
            "absolute_path": absolute_path,
            "relative_path": 'folder/Alpha%_*-"Café.png',
            "folder": "folder",
            "raw_infotext": 'Prompt: velvet_%*-" Café 東京景',
        },
    )

    for query in ("alpha%_", "%_*-", '*-"', '"CAFÉ', "velvet_%*", "東京景"):
        assert _search_ids(query=query) == [image_id]
    for query in ("plain", '" OR "plain', "___", "***", "---"):
        assert _search_ids(query=query) == []

    plan = db.get_connection().execute(
        "EXPLAIN QUERY PLAN SELECT i.id FROM images i"
        " WHERE i.id IN (SELECT rowid FROM image_search WHERE image_search MATCH ?)",
        ('"alpha%_"',),
    )
    details = [row["detail"] for row in plan]
    assert any("image_search VIRTUAL TABLE" in detail for detail in details)
    assert all("raw_infotext" not in detail for detail in details)

    image_store.upsert(
        root["id"],
        {
            "absolute_path": absolute_path,
            "relative_path": "renamed/Final_日本語.png",
            "folder": "renamed",
            "raw_infotext": "Prompt: cobalt horizon",
        },
    )
    assert _search_ids(query="alpha%_") == []
    assert _search_ids(query="cobalt") == [image_id]
    assert _search_ids(query="日本語") == [image_id]

    image_store.delete(image_id)
    assert _search_ids(query="cobalt") == []


def test_v19_migration_backfills_image_search_without_changing_images(tmp_path):
    root = source_store.add_root(str(tmp_path))
    image_id = image_store.upsert(
        root["id"],
        {
            "absolute_path": str(tmp_path / "migration.png"),
            "relative_path": "legacy/migration.png",
            "folder": "legacy",
            "raw_infotext": "Prompt: backfilled nebula",
        },
    )
    conn = db.get_connection()
    before = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    conn.executescript(
        """
        DROP TRIGGER images_search_insert;
        DROP TRIGGER images_search_delete;
        DROP TRIGGER images_search_update;
        DROP TABLE image_search;
        PRAGMA user_version=18;
        """
    )
    db.close_connection()

    db.init_db()

    conn = db.get_connection()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == before
    assert _search_ids(query="backfilled") == [image_id]


def test_v20_migration_copy_preserves_post_images_as_unknown(tmp_path, monkeypatch):
    post_id = post_store.create(title="Migration copy")
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO post_images(post_id, image_id, position, source_path, sha256,"
            " remote_image_id, remote_on_site) VALUES(?,NULL,?,'','',?,?)",
            [(post_id, 0, 901, 1), (post_id, 1, 902, 0)],
        )
        before = conn.execute("SELECT COUNT(*) FROM post_images").fetchone()[0]
        conn.execute("ALTER TABLE post_images DROP COLUMN remote_on_site")
        conn.execute("PRAGMA user_version=19")

    copy_dir = tmp_path / "migration-copy"
    copy_dir.mkdir()
    copied = sqlite3.connect(copy_dir / "publisher.db")
    try:
        db.get_connection().backup(copied)
    finally:
        copied.close()

    db.close_connection()
    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(copy_dir))
    db.init_db()

    migrated = db.get_connection()
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert migrated.execute("SELECT COUNT(*) FROM post_images").fetchone()[0] == before
    assert [
        row["remote_on_site"]
        for row in migrated.execute("SELECT remote_on_site FROM post_images ORDER BY position")
    ] == [None, None]
    assert config.db_path() == copy_dir / "publisher.db"


def test_v21_migration_copy_adds_empty_dismissals_without_changing_duplicates(
    tmp_path, monkeypatch
):
    root_id = source_store.add_root(str(tmp_path), label="Migration source")["id"]
    image_ids = [
        image_store.upsert(
            root_id,
            {
                "absolute_path": str(tmp_path / name),
                "relative_path": name,
                "sha256": "same-bytes",
                "pixel_sha256": "same-pixels",
            },
        )
        for name in ("first.png", "second.png")
    ]
    before_images = db.get_connection().execute("SELECT COUNT(*) FROM images").fetchone()[0]
    before_group_ids = [
        member["id"] for member in duplicates.exact_groups()[0]["members"]
    ]
    with db.transaction() as conn:
        conn.execute("DROP TABLE duplicate_dismissals")
        conn.execute("PRAGMA user_version=20")

    copy_dir = tmp_path / "migration-v21-copy"
    copy_dir.mkdir()
    copied = sqlite3.connect(copy_dir / "publisher.db")
    try:
        db.get_connection().backup(copied)
    finally:
        copied.close()

    db.close_connection()
    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(copy_dir))
    db.init_db()

    migrated = db.get_connection()
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert migrated.execute("SELECT COUNT(*) FROM images").fetchone()[0] == before_images
    assert migrated.execute("SELECT COUNT(*) FROM duplicate_dismissals").fetchone()[0] == 0
    assert [
        member["id"] for member in duplicates.exact_groups()[0]["members"]
    ] == before_group_ids

    assert duplicates.dismiss(image_ids) == 1
    assert migrated.execute("SELECT COUNT(*) FROM duplicate_dismissals").fetchone()[0] == 1
    assert duplicates.exact_groups() == []


def test_v22_migration_copy_marks_existing_images_for_one_refresh(tmp_path, monkeypatch):
    root_id = source_store.add_root(str(tmp_path), label="Migration source")["id"]
    image_store.upsert(
        root_id,
        {
            "absolute_path": str(tmp_path / "legacy.png"),
            "relative_path": "legacy.png",
            "sha256": "legacy-bytes",
        },
    )
    conn = db.get_connection()
    before = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    conn.execute("ALTER TABLE images DROP COLUMN ingest_revision")
    conn.execute("PRAGMA user_version=21")
    conn.commit()

    copy_dir = tmp_path / "migration-v22-copy"
    copy_dir.mkdir()
    copied = sqlite3.connect(copy_dir / "publisher.db")
    try:
        conn.backup(copied)
    finally:
        copied.close()

    db.close_connection()
    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(copy_dir))
    db.init_db()

    migrated = db.get_connection()
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert migrated.execute("SELECT COUNT(*) FROM images").fetchone()[0] == before
    assert migrated.execute("SELECT ingest_revision FROM images").fetchone()[0] == 0


def test_v27_migration_gives_every_profile_a_uuid(tmp_path, monkeypatch):
    """A migration cannot be taken back, and this one adds a NOT NULL UNIQUE
    column to rows that have no value for it. Left half-done, the app cannot
    open the database at all."""
    from backend.llm import profiles

    profiles.ensure_builtins()
    mine = profiles.create({"name": "Meins", "system_prompt": "Mein Prompt."})
    conn = db.get_connection()
    before = conn.execute("SELECT COUNT(*) FROM llm_profiles").fetchone()[0]

    # Back to what a version 26 database looked like.
    conn.execute("DROP INDEX IF EXISTS idx_llm_profiles_uuid")
    conn.execute("ALTER TABLE llm_profiles DROP COLUMN uuid")  # plain column now
    conn.execute("ALTER TABLE llm_profiles ADD COLUMN is_builtin INTEGER NOT NULL DEFAULT 0")
    conn.execute("PRAGMA user_version=26")
    conn.commit()

    copy_dir = tmp_path / "migration-v27-copy"
    copy_dir.mkdir()
    copied = sqlite3.connect(copy_dir / "publisher.db")
    try:
        conn.backup(copied)
    finally:
        copied.close()

    db.close_connection()
    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(copy_dir))
    db.init_db()

    migrated = db.get_connection()
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    rows = migrated.execute("SELECT name, uuid FROM llm_profiles").fetchall()
    assert len(rows) == before, "no profile was lost or added"
    uuids = {row["uuid"] for row in rows}
    assert len(uuids) == before, "every profile got its own"
    assert all(len(value) == 36 for value in uuids), uuids

    # Mechanical on purpose: it hands out fresh values and recognises nobody, so
    # the profiles that were built-in before are ordinary rows afterwards. Only
    # a fresh seeding or the one-off script makes them shipped profiles again.
    assert not any(profiles.is_builtin(row["uuid"]) for row in rows)
    assert migrated.execute(
        "SELECT name FROM llm_profiles WHERE name=?", (mine["name"],)
    ).fetchone() is not None

    # The index is the one thing this migration cannot repair afterwards: two
    # rows sharing a uuid means two profiles claiming one identity, and a
    # restore would then pick whichever came first.
    taken = rows[0]["uuid"]
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO llm_profiles(uuid, name, system_prompt, created_at)"
            " VALUES(?,?,?,?)",
            (taken, "Zwilling", "x", "now"),
        )

    # And the name is only a label: the same one twice is allowed.
    migrated.execute(
        "INSERT INTO llm_profiles(uuid, name, system_prompt, created_at) VALUES(?,?,?,?)",
        ("free-uuid", rows[0]["name"], "x", "now"),
    )
