"""Database backups.

The database is the entire state of the application. These tests check above all
that a backup is really complete and that a restore does not destroy the last
copy that was left.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import backup, db
from backend.store import images as image_store
from backend.store import posts as post_store


def _setting(path: str | Path, key: str) -> str | None:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    # FTS shadow rows are physical index segments, not application records. A
    # rebuild may merge them while preserving the indexed rows and all answers.
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name NOT GLOB 'image_search_*'"
            " ORDER BY name"
        )
    ]
    return {
        table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in tables
    }


def test_a_backup_captures_data_written_moments_ago(client, scanned):
    """In WAL mode recent writes sit in a side file - a plain file copy would be
    incomplete depending on timing."""
    post_store.create(title="Gerade eben")

    result = backup.create(reason="test")
    assert not any(sidecar.exists() for sidecar in backup._sidecars(Path(result["path"])))

    conn = sqlite3.connect(f"file:{result['path']}?mode=ro&immutable=1", uri=True)
    try:
        titles = [row[0] for row in conn.execute("SELECT title FROM posts")]
    finally:
        conn.close()
    assert "Gerade eben" in titles
    assert result["contents"]["posts"] >= 1
    assert result["contents"]["schema_version"] == db.SCHEMA_VERSION


def test_restoring_a_backup_rebuilds_search_without_changing_any_table_count(
    client, scanned
):
    image_id = scanned[0]
    db.get_connection().execute(
        "UPDATE images SET raw_infotext=? WHERE id=?",
        ("Prompt: restoration sentinel phrase", image_id),
    )
    raw_resource = '{"only_record":{"name":"Vanished model version"}}'
    db.get_connection().execute(
        "INSERT INTO resource_map(hash, source, queried_at, raw_json)"
        " VALUES('ONLYRECORD', 'rest', '2026-09-06T00:00:00Z', ?)",
        (raw_resource,),
    )
    db.get_connection().commit()
    before = _table_counts(db.get_connection())
    assert [row["id"] for row in image_store.search_candidates(query="sentinel")] == [
        image_id
    ]

    snapshot = backup.create(reason="without-search-index")
    saved = sqlite3.connect(f"file:{snapshot['path']}?mode=ro&immutable=1", uri=True)
    try:
        saved_tables = {
            row[0]
            for row in saved.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
    finally:
        saved.close()
    assert not any(
        name == "image_search" or name.startswith("image_search_")
        for name in saved_tables
    )

    backup.restore(snapshot["name"])

    assert _table_counts(db.get_connection()) == before
    restored_resource = db.get_connection().execute(
        "SELECT raw_json FROM resource_map WHERE hash='ONLYRECORD'"
    ).fetchone()
    assert restored_resource["raw_json"] == raw_resource
    assert [row["id"] for row in image_store.search_candidates(query="sentinel")] == [
        image_id
    ]


def test_two_backups_with_the_same_name_keep_both_files(client, monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 28, 12, 34, 56, tzinfo=tz)

    monkeypatch.setattr(backup, "datetime", FrozenDatetime)

    first = backup.create(reason="same-second")
    second = backup.create(reason="same-second")

    assert first["name"] == "publisher-20260828-123456-manual-same-second.db"
    assert second["name"] == "publisher-20260828-123456-manual-same-second-2.db"
    assert Path(first["path"]).exists()
    assert Path(second["path"]).exists()


def test_backups_are_listed_newest_first(client):
    backup.create(reason="eins")
    backup.create(reason="zwei")
    items = backup.list_backups()
    assert len(items) >= 2
    assert items[0]["created_at"] >= items[1]["created_at"]


def test_every_backup_trigger_strips_the_api_token(client):
    db.set_setting("civitai_api_token", "account-access")

    created = [
        backup.create(reason="manual-token-check"),
        backup.create(reason="automatic-token-check", automatic=True),
        client.post("/api/backups", params={"reason": "http-token-check"}).json(),
    ]

    assert all(_setting(item["path"], "civitai_api_token") is None for item in created)


def test_a_backup_carries_no_oauth_connection_either(client):
    """A refresh token lives thirty days - longer than the key it replaces, so a
    backup keeping one would be worse, not better (`SET-05`)."""
    from backend.civitai import oauth

    for key in oauth.BACKUP_STRIPPED_KEYS:
        db.set_setting(key, f"secret-{key}")

    created = backup.create(reason="manual-oauth-check")

    assert [_setting(created["path"], key) for key in oauth.BACKUP_STRIPPED_KEYS] == [None] * 5


def test_any_token_redaction_failure_removes_the_unsafe_backup(client, monkeypatch):
    db.set_setting("civitai_api_token", "must-not-survive")
    before = set(backup.backup_dir().glob(f"{backup.PREFIX}-*.db"))

    def fail_redaction(conn):
        raise OSError("journal unavailable")

    monkeypatch.setattr(backup, "_strip_token", fail_redaction)

    with pytest.raises(OSError, match="journal unavailable"):
        backup.create(reason="non-sqlite-redaction-failure")

    assert set(backup.backup_dir().glob(f"{backup.PREFIX}-*.db")) == before


def test_restoring_keeps_a_copy_of_what_was_there(client, scanned):
    """A restore must not destroy the only copy that was left."""
    post_store.create(title="Old state")
    snapshot = backup.create(reason="alt")

    post_store.create(title="New state")
    response = client.post(f"/api/backups/{snapshot['name']}/restore")
    assert response.status_code == 200
    result = response.json()

    assert result["restored"] == snapshot["name"]
    safety = next(
        item for item in backup.list_backups() if item["name"] == result["safety_copy"]
    )
    conn = sqlite3.connect(f"file:{safety['path']}?mode=ro", uri=True)
    try:
        titles = [row[0] for row in conn.execute("SELECT title FROM posts")]
    finally:
        conn.close()
    assert "New state" in titles, "the overwritten state is backed up"


def test_the_restored_database_is_the_old_one(client, scanned):
    post_store.create(title="Old state")
    db.set_setting("civitai_api_token", "old-token")
    snapshot = backup.create(reason="alt")
    assert _setting(snapshot["path"], "civitai_api_token") is None

    # Backups made by older versions contained the token. Restoring one must
    # still leave the current application without it.
    legacy = sqlite3.connect(snapshot["path"])
    try:
        legacy.execute(
            "INSERT INTO settings(key, value) VALUES('civitai_api_token', 'legacy-token')"
        )
        legacy.commit()
    finally:
        legacy.close()

    post_store.create(title="New state")
    db.set_setting("civitai_api_token", "new-token")

    backup.restore(snapshot["name"])

    titles = [row["title"] for row in db.get_connection().execute("SELECT title FROM posts")]
    assert "Old state" in titles
    assert "New state" not in titles
    assert db.get_setting("civitai_api_token") is None
    settings = client.get("/api/settings")
    assert settings.status_code == 200
    assert settings.json()["civitai_auth_mode"] == "none"


def test_open_connections_survive_a_restore(client, scanned):
    """Swapping the file under open connections earns an I/O error from SQLite -
    which is why the restore goes through the connection."""
    post_store.create(title="x")
    snapshot = backup.create(reason="alt")
    post_store.create(title="y")

    live = db.get_connection()
    backup.restore(snapshot["name"])

    # the same connection, not a fresh one
    titles = [row["title"] for row in live.execute("SELECT title FROM posts")]
    assert "x" in titles and "y" not in titles


def test_an_older_backup_is_migrated_forward(client):
    """Yesterday's backup must not land with yesterday's schema."""
    snapshot = backup.create(reason="alt")
    db.get_connection().execute("PRAGMA user_version=0")
    db.get_connection().commit()

    result = backup.restore(snapshot["name"])
    assert result["schema_version"] == db.SCHEMA_VERSION


def test_manual_backup_survives_when_regular_and_pre_migration_backups_are_pruned(
    client,
):
    for earlier in backup.backup_dir().glob(f"{backup.PREFIX}-*"):
        earlier.unlink()
    manual = backup.create(reason="made-by-hand")
    migration = backup.create(reason="before-migration-v28", automatic=True)
    regular = backup.create(reason=backup.REGULAR_REASON, automatic=True)

    now = datetime.now(timezone.utc).timestamp()
    os.utime(migration["path"], (now - 10, now - 10))
    os.utime(regular["path"], (now, now))

    backup.prune(keep=1)

    names = {item["name"] for item in backup.list_backups()}
    assert manual["name"] in names, "the named manual backup is kept"
    assert regular["name"] in names
    assert migration["name"] not in names


def test_pruning_removes_sidecars_with_a_backup_and_cleans_existing_orphans(client):
    folder = backup.backup_dir()
    for earlier in folder.glob(f"{backup.PREFIX}-*"):
        earlier.unlink()

    old = folder / "publisher-20260801-000000-auto-regular.db"
    current = folder / "publisher-20260802-000000-auto-regular.db"
    old.write_bytes(b"old")
    current.write_bytes(b"current")
    old_sidecars = [Path(f"{old}-wal"), Path(f"{old}-shm")]
    for sidecar in old_sidecars:
        sidecar.write_bytes(b"sidecar")
    orphan_sidecars = [
        folder / "publisher-20260701-000000-auto-regular.db-wal",
        folder / "publisher-20260701-000000-auto-regular.db-shm",
    ]
    for sidecar in orphan_sidecars:
        sidecar.write_bytes(b"orphan")
    current_sidecars = [Path(f"{current}-wal"), Path(f"{current}-shm")]
    current_sidecars[0].write_bytes(b"")
    current_sidecars[1].write_bytes(b"derived shared memory")

    backup.prune(keep=1)

    assert current.exists()
    assert not old.exists()
    assert not any(
        sidecar.exists()
        for sidecar in old_sidecars + orphan_sidecars + current_sidecars
    )


def test_the_regular_schedule_creates_a_backup_and_prunes_the_oldest(
    client, monkeypatch
):
    for path in backup.backup_dir().glob(
        f"{backup.PREFIX}-*-auto-{backup.REGULAR_REASON}.db"
    ):
        path.unlink()

    now = datetime.now(timezone.utc)
    old = []
    for index in range(3):
        item = backup.create(reason=f"old-{index}", automatic=True)
        age = now - timedelta(days=10 - index)
        os.utime(item["path"], (age.timestamp(), age.timestamp()))
        old.append(item)

    monkeypatch.setattr(backup, "KEEP_AUTOMATIC", 2)
    db.init_db()

    automatic = [item for item in backup.list_backups() if item["automatic"]]
    regular = [item for item in automatic if item["name"].endswith("-auto-regular.db")]
    assert len(automatic) == 2
    assert len(regular) == 1
    assert old[0]["name"] not in {item["name"] for item in automatic}

    first_name = regular[0]["name"]
    db.init_db()
    regular = [
        item
        for item in backup.list_backups()
        if item["name"].endswith("-auto-regular.db")
    ]
    assert [item["name"] for item in regular] == [first_name]


def test_a_path_outside_the_backup_folder_is_refused(client):
    import pytest

    with pytest.raises(FileNotFoundError):
        backup.restore("../../etc/passwd")


def test_a_migration_makes_a_safety_copy_first(client):
    """A migration writes into the only copy there is."""
    # Keep the assertions below about this migration's backup and nothing else.
    for earlier in backup.backup_dir().glob(f"{backup.PREFIX}-*.db"):
        earlier.unlink()
    db.set_setting("civitai_api_token", "migration-token")
    db.get_connection().execute("PRAGMA user_version=0")
    db.get_connection().commit()
    db.close_connection()

    db.init_db()

    automatic = [item for item in backup.list_backups() if "before-migration" in item["name"]]
    assert automatic, "a backup was taken before the migration"
    assert all(_setting(item["path"], "civitai_api_token") is None for item in automatic)


def test_a_fresh_database_is_not_backed_up_before_schema_creation(tmp_path, monkeypatch):
    fresh = tmp_path / "fresh-data"
    fresh.mkdir()
    sqlite3.connect(fresh / "publisher.db").close()
    db.close_connection()
    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(fresh))

    db.init_db()

    migration_backups = list(
        backup.backup_dir().glob(f"{backup.PREFIX}-*-auto-before-migration-*.db")
    )
    assert migration_backups == []
    assert db.get_connection().execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_the_backup_over_http(client):
    created = client.post("/api/backups", params={"reason": "per-api"}).json()
    assert created["name"].endswith(".db")

    listing = client.get("/api/backups").json()
    assert any(item["name"] == created["name"] for item in listing["items"])

    assert client.delete(f"/api/backups/{created['name']}").json()["ok"] is True
    assert not any(
        item["name"] == created["name"] for item in client.get("/api/backups").json()["items"]
    )

    missing = client.delete(f"/api/backups/{created['name']}")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "backup_not_found"


# --- archive backup ----------------------------------------------------------


def _archive_with_files(tmp_path, count=3, size=200_000):
    from backend.store import sources as source_store

    root = tmp_path / "archiv"
    (root / "post_id_1").mkdir(parents=True)
    for index in range(count):
        (root / "post_id_1" / f"bild_{index}.png").write_bytes(b"x" * size)
    source_store.add_root(str(root), label="Archiv", is_archive=True)
    return root


def test_the_archive_status_counts_without_touching_anything(client, tmp_path):
    root = _archive_with_files(tmp_path)
    state = backup.archive_status()

    assert state["configured"] and state["exists"]
    assert state["files"] == 3
    assert state["bytes"] == 600_000
    assert len(list(root.rglob("*.png"))) == 3, "nothing was touched"


def test_the_archive_is_bundled_into_one_zip(client, tmp_path):
    import zipfile

    from backend import jobs

    _archive_with_files(tmp_path)
    result = backup.create_archive_zip(jobs.Job(id=0, kind="archive-backup"))

    assert result["files"] == 3
    with zipfile.ZipFile(result["path"]) as bundle:
        names = sorted(bundle.namelist())
    assert names == [
        "post_id_1/bild_0.png",
        "post_id_1/bild_1.png",
        "post_id_1/bild_2.png",
    ], "the folder structure is preserved"


def test_archive_status_and_zip_exclude_a_symlink_escape(client, tmp_path, monkeypatch):
    import zipfile

    from backend import jobs

    root = _archive_with_files(tmp_path)
    outside = tmp_path / "outside-secret.png"
    outside.write_bytes(b"must not enter the archive backup")
    link = root / "post_id_1" / "escape.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available")

    state = backup.archive_status()

    # Exercise the resolved-boundary guard independently for the ZIP walk. A
    # platform-specific junction that is not reported as a symlink must still
    # be unable to lead the backup outside the configured archive.
    path_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: False if path == link else path_is_symlink(path),
    )
    result = backup.create_archive_zip(jobs.Job(id=0, kind="archive-backup"))

    assert state["files"] == 3
    assert state["bytes"] == 600_000
    assert result["files"] == 3
    with zipfile.ZipFile(result["path"]) as bundle:
        names = bundle.namelist()
    assert len(names) == 3
    assert "post_id_1/escape.png" not in names


def test_splitting_produces_parts_that_reassemble(client, tmp_path, monkeypatch):
    """Reassembled with `cat part.* > whole.zip` - which is why the numbers have
    to sort ascending and the bytes have to match exactly."""
    import zipfile

    from backend import jobs

    _archive_with_files(tmp_path, count=4, size=300_000)
    unsplit = None
    real_split = backup._split

    def capture_unsplit(path, size_mb):
        nonlocal unsplit
        unsplit = path.read_bytes()
        return real_split(path, size_mb)

    monkeypatch.setattr(backup, "_split", capture_unsplit)
    result = backup.create_archive_zip(jobs.Job(id=0, kind="archive-backup"), split_mb=1)

    assert len(result["parts"]) > 1, "the file was split"
    assert result["path"] is None, "the unsplit zip is not left behind"

    folder = backup.backup_dir()
    parts = sorted(folder.glob("archive-*.zip.*"))
    joined = tmp_path / "rejoined.zip"
    joined.write_bytes(b"".join(part.read_bytes() for part in parts))

    assert unsplit is not None
    assert joined.read_bytes() == unsplit
    assert all(part["bytes"] == 1024**2 for part in result["parts"][:-1])
    assert result["parts"][-1]["bytes"] <= 1024**2
    assert 0 < backup._SPLIT_BUFFER_BYTES <= 4 * 1024**2
    with zipfile.ZipFile(joined) as bundle:
        assert len(bundle.namelist()) == 4
        assert bundle.testzip() is None, "the reassembled zip is intact"


def test_without_an_archive_folder_it_says_so(client):
    from backend import jobs

    assert backup.archive_status()["configured"] is False
    job = jobs.Job(id=0, kind="archive-backup")
    backup.create_archive_zip(job)
    assert job.error


def test_a_backup_is_one_file_and_nothing_beside_it():
    """A backup has to stand alone - no WAL, no SHM, nothing to forget to copy.

    It is produced through SQLite's own backup API rather than by copying the
    file, so it is consistent even under a write in flight; this asserts the
    other half, that the artefact is a single file. A sidecar left behind would
    make a backup depend on a journal nobody carries with it, and the folder
    once held 76 orphans from exactly that.
    """
    backup.create(reason="single-file")
    files = sorted(p.name for p in backup.backup_dir().iterdir())
    assert [name for name in files if name.endswith((".db-wal", ".db-shm"))] == []
    assert all(name.endswith(".db") for name in files), files

