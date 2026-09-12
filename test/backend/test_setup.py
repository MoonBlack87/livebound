"""First-start setup before the database exists."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

from fastapi.testclient import TestClient

from backend import backup, config, db, usage_ping
from backend.store import posts as post_store


def _pending(tmp_path: Path, monkeypatch) -> Path:
    application_root = tmp_path / "installation"
    monkeypatch.setattr(config, "_APPLICATION_ROOT", application_root)
    monkeypatch.delenv("LIVEBOUND_DATA_DIR", raising=False)
    monkeypatch.setattr(config, "_pointer", None)
    monkeypatch.setattr(config, "_pointer_read", False)
    db.relocated()
    return application_root / ".livebound" / "data"


def _app():
    # Import only after the autouse fixture has isolated the data paths. The
    # module owns a process-wide app as well as the factory.
    from backend.main import create_app

    return create_app()


def test_first_start_serves_without_creating_a_database(tmp_path, monkeypatch):
    default = _pending(tmp_path, monkeypatch)

    with TestClient(_app()) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/health").status_code == 200
        state = client.get("/api/setup")

        assert state.status_code == 200
        assert state.json() == {
            "setup_required": True,
            "data_directory_ready": False,
            "default_path": str(default),
        }
        assert not default.exists()

        refused = client.get("/api/posts")
        assert refused.status_code == 428
        assert refused.json()["detail"]["code"] == "setup_required"
        assert not default.exists()


def test_the_decision_initialises_the_database_without_a_restart(tmp_path, monkeypatch):
    default = _pending(tmp_path, monkeypatch)

    with TestClient(_app()) as client:
        chosen = client.post("/api/setup", data={"path": str(default)})

        assert chosen.status_code == 200
        assert chosen.json()["setup_required"] is True
        assert chosen.json()["data_directory_ready"] is True
        assert chosen.json()["restored"] is False
        assert (default / "publisher.db").is_file()
        assert json.loads(config.config_file().read_text(encoding="utf-8"))["data_dir"] == str(
            default
        )
        state = client.get("/api/setup").json()
        assert state["setup_required"] is True
        assert state["data_directory_ready"] is True
        assert client.get("/api/posts").status_code == 200


def test_the_decision_can_import_a_backup(tmp_path, monkeypatch):
    post_store.create(title="Restored post")
    saved = Path(backup.create(reason="setup-source")["path"])
    default = _pending(tmp_path, monkeypatch)

    with TestClient(_app()) as client:
        chosen = client.post(
            "/api/setup",
            data={"path": str(default)},
            files={"backup": (saved.name, saved.read_bytes(), "application/octet-stream")},
        )

        assert chosen.status_code == 200
        assert chosen.json()["restored"] is True
        assert [post["title"] for post in client.get("/api/posts").json()["items"]] == [
            "Restored post"
        ]


def test_an_oversized_backup_is_removed_before_initialisation(tmp_path, monkeypatch):
    default = _pending(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    staging.mkdir()
    limit = 1024**2
    monkeypatch.setattr(config, "SETUP_BACKUP_MAX_BYTES", limit)
    monkeypatch.setattr("backend.api.setup.tempfile.tempdir", str(staging))

    with TestClient(_app()) as client:
        refused = client.post(
            "/api/setup",
            data={"path": str(default)},
            files={
                "backup": (
                    "too-large.db",
                    b"x" * (limit + 1),
                    "application/octet-stream",
                )
            },
        )

        assert refused.status_code == 413
        assert refused.json()["detail"] == {
            "code": "setup_backup_too_large",
            "message": (
                "The backup exceeds the 1 MiB import limit. "
                "Choose a smaller Livebound backup or start empty."
            ),
            "params": {"limit": "1 MiB"},
        }
        assert list(staging.iterdir()) == []
        assert not default.exists()
        assert not config.config_file().exists()


def test_a_foreign_sqlite_database_is_not_imported(tmp_path, monkeypatch):
    foreign = tmp_path / "foreign.db"
    with sqlite3.connect(foreign) as connection:
        connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute(f"PRAGMA user_version={db.SCHEMA_VERSION}")
    default = _pending(tmp_path, monkeypatch)

    with TestClient(_app()) as client:
        refused = client.post(
            "/api/setup",
            data={"path": str(default)},
            files={"backup": (foreign.name, foreign.read_bytes(), "application/octet-stream")},
        )

        assert refused.status_code == 400
        assert refused.json()["detail"]["code"] == "bad_setup_backup"
        assert not default.exists()
        assert not config.config_file().exists()


def test_an_existing_pointer_never_enters_setup(tmp_path, monkeypatch):
    chosen = tmp_path / "chosen"
    _pending(tmp_path, monkeypatch)
    config.set_data_dir(chosen)
    db.relocated()
    db.init_db()
    db.relocated()

    with TestClient(_app()) as client:
        assert client.get("/api/setup").json()["setup_required"] is False
        assert client.get("/api/posts").status_code == 200
        refused = client.post("/api/setup", data={"path": str(tmp_path / "other")})
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "setup_already_completed"
        assert config.data_dir() == chosen


def test_global_livebound_state_is_ignored_by_a_fresh_installation(
    tmp_path, monkeypatch
):
    fake_home = tmp_path / "home"
    legacy_data = fake_home / ".local" / "share" / "livebound"
    old_data = fake_home / ".local" / "share" / "an-older-name"
    legacy_pointers = (
        fake_home / ".config" / "livebound" / "config.json",
        fake_home / "AppData" / "Local" / "livebound" / "config.json",
        fake_home / "Library" / "Preferences" / "livebound" / "config.json",
    )
    monkeypatch.setattr(config.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / ".local" / "share"))
    monkeypatch.setenv("LOCALAPPDATA", str(fake_home / "AppData" / "Local"))
    for legacy_pointer in legacy_pointers:
        legacy_pointer.parent.mkdir(parents=True, exist_ok=True)
        legacy_pointer.write_text(
            json.dumps({"data_dir": str(legacy_data)}), encoding="utf-8"
        )
    legacy_data.mkdir(parents=True)
    old_data.mkdir(parents=True)
    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(legacy_data))
    db.relocated()
    db.init_db()
    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(old_data))
    db.relocated()
    db.init_db()

    application_root = tmp_path / "fresh-installation"
    monkeypatch.setattr(config, "_APPLICATION_ROOT", application_root)
    monkeypatch.delenv("LIVEBOUND_DATA_DIR")
    monkeypatch.setattr(config, "_pointer", None)
    monkeypatch.setattr(config, "_pointer_read", False)
    db.relocated()

    assert config.setup_required() is True
    assert not config.config_file().exists()
    with TestClient(_app()) as client:
        state = client.get("/api/setup").json()
        assert state["setup_required"] is True
        assert state["default_path"] == str(application_root / ".livebound" / "data")
        assert client.get("/api/posts").status_code == 428
        assert not (application_root / ".livebound").exists()


def test_a_failed_backup_import_leaves_no_pointer(tmp_path, monkeypatch):
    saved = Path(backup.create(reason="setup-source")["path"])
    default = _pending(tmp_path, monkeypatch)
    failed_copy = Mock(side_effect=OSError("copy failed"))
    monkeypatch.setattr("backend.api.setup.shutil.copyfile", failed_copy)

    with TestClient(_app()) as client:
        response = client.post(
            "/api/setup",
            data={"path": str(default)},
            files={"backup": (saved.name, saved.read_bytes(), "application/octet-stream")},
        )

        assert response.status_code == 500
        assert response.json()["detail"]["code"] == "setup_failed"
        assert not config.config_file().exists()
        assert client.get("/api/setup").json()["setup_required"] is True


def test_without_consent_there_is_no_uuid_and_no_ping(tmp_path, monkeypatch):
    default = _pending(tmp_path, monkeypatch)
    sent = Mock()
    monkeypatch.setattr(usage_ping, "_send", sent)

    with TestClient(_app()) as client:
        client.post("/api/setup", data={"path": str(default)}).raise_for_status()
        usage_ping.send_at_start()

    assert db.get_setting(usage_ping.ENABLED_SETTING) == "0"
    assert db.get_setting(usage_ping.INSTALLATION_ID_SETTING) is None
    sent.assert_not_called()


def test_the_wizard_finishes_only_after_its_minimum(tmp_path, monkeypatch):
    default = _pending(tmp_path, monkeypatch)
    image_folder = tmp_path / "images"
    archive_folder = tmp_path / "archive"
    image_folder.mkdir()
    archive_folder.mkdir()
    sent = Mock()
    monkeypatch.setattr("backend.api.setup.oauth.connected", lambda: True)
    monkeypatch.setattr(usage_ping, "send_at_start", sent)

    with TestClient(_app()) as client:
        client.post("/api/setup", data={"path": str(default)}).raise_for_status()
        incomplete = client.post("/api/setup/finish")
        assert incomplete.status_code == 409
        assert incomplete.json()["detail"]["code"] == "setup_incomplete"

        client.post("/api/sources", json={"path": str(image_folder)}).raise_for_status()
        client.post(
            "/api/sources", json={"path": str(archive_folder), "is_archive": True}
        ).raise_for_status()
        finished = client.post("/api/setup/finish")

        assert finished.json() == {"setup_required": False}
        assert client.get("/api/setup").json()["setup_required"] is False
        sent.assert_called_once_with()


def test_two_simultaneous_finish_requests_complete_only_once(tmp_path, monkeypatch):
    default = _pending(tmp_path, monkeypatch)
    image_folder = tmp_path / "images"
    archive_folder = tmp_path / "archive"
    image_folder.mkdir()
    archive_folder.mkdir()
    sent = Mock()
    monkeypatch.setattr("backend.api.setup.oauth.connected", lambda: True)
    monkeypatch.setattr(usage_ping, "send_at_start", sent)

    with TestClient(_app()) as client:
        client.post("/api/setup", data={"path": str(default)}).raise_for_status()
        client.post("/api/sources", json={"path": str(image_folder)}).raise_for_status()
        client.post(
            "/api/sources", json={"path": str(archive_folder), "is_archive": True}
        ).raise_for_status()

        overlap = Barrier(2)
        original_get_setting = db.get_setting

        def overlapping_setup_read(key: str, default: str | None = None):
            value = original_get_setting(key, default)
            if key == "setup_completed":
                overlap.wait(timeout=2)
            return value

        monkeypatch.setattr(db, "get_setting", overlapping_setup_read)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.post("/api/setup/finish"), range(2)))

        assert sorted(response.status_code for response in responses) == [200, 409]
        assert {
            response.json().get("detail", {}).get("code")
            for response in responses
            if response.status_code == 409
        } == {"setup_already_completed"}
        sent.assert_called_once_with()
