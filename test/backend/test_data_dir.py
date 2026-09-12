"""Moving the data directory.

The location cannot live in the database it points at, so it lives in a small
file next to the other per-user configuration - and the move has to leave the
old copy intact until the new one is proven.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend import config, db, jobs, relocate
from backend.store import images as image_store


def test_installation_local_defaults_ignore_per_user_locations(tmp_path, monkeypatch):
    application_root = tmp_path / "installation"
    fake_home = tmp_path / "home"
    monkeypatch.setattr(config, "_APPLICATION_ROOT", application_root)
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / "xdg-config"))
    monkeypatch.setenv("LOCALAPPDATA", str(fake_home / "local-app-data"))

    assert config.default_data_dir() == application_root / ".livebound" / "data"
    assert config.config_file() == application_root / ".livebound" / "config.json"
    # The name, not the number: the release export stamps the public version
    # into this file, so a literal here passes at home and fails in the very
    # tree that gets published.
    expected_user_agent = f"livebound/{config.APP_VERSION}"
    assert expected_user_agent == config.USER_AGENT


def _move(target: Path) -> dict:
    return relocate.run(jobs.Job(id=0, kind="move-data"), str(target))


@pytest.fixture
def pointed(tmp_path, monkeypatch):
    """A run where the pointer file decides, not the environment."""
    application_root = tmp_path / "installation"
    monkeypatch.setattr(config, "_APPLICATION_ROOT", application_root)
    monkeypatch.delenv("LIVEBOUND_DATA_DIR", raising=False)
    monkeypatch.setattr(config, "_pointer", None)
    monkeypatch.setattr(config, "_pointer_read", False)
    chosen = tmp_path / "chosen"
    config.set_data_dir(chosen)
    db.relocated()
    db.init_db()
    return chosen


def test_the_environment_still_wins_over_the_pointer(tmp_path, monkeypatch):
    """The explicit isolated-run target wins over this installation's pointer."""
    monkeypatch.setattr(config, "_APPLICATION_ROOT", tmp_path / "installation")
    monkeypatch.delenv("LIVEBOUND_DATA_DIR", raising=False)
    monkeypatch.setattr(config, "_pointer", None)
    monkeypatch.setattr(config, "_pointer_read", False)
    config.set_data_dir(tmp_path / "pointed")

    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(tmp_path / "forced"))
    assert config.data_dir() == tmp_path / "forced"


def test_environment_override_does_not_access_the_local_pointer(tmp_path, monkeypatch):
    application_root = tmp_path / "installation"
    override = tmp_path / "isolated-run"
    monkeypatch.setattr(config, "_APPLICATION_ROOT", application_root)
    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(override))

    def pointer_must_not_be_read() -> Path | None:
        raise AssertionError("the environment override must not read the local pointer")

    monkeypatch.setattr(config, "_pointed_data_dir", pointer_must_not_be_read)

    assert config.data_dir() == override
    assert config.setup_required() is False
    assert not config.config_file().exists()


def test_the_pointer_file_decides_when_the_environment_is_silent(tmp_path, monkeypatch):
    application_root = tmp_path / "installation"
    monkeypatch.setattr(config, "_APPLICATION_ROOT", application_root)
    monkeypatch.delenv("LIVEBOUND_DATA_DIR", raising=False)
    monkeypatch.setattr(config, "_pointer", None)
    monkeypatch.setattr(config, "_pointer_read", False)

    config.set_data_dir(tmp_path / "elsewhere")

    assert config.data_dir() == tmp_path / "elsewhere"
    stored = json.loads(config.config_file().read_text(encoding="utf-8"))
    assert stored["data_dir"] == str(tmp_path / "elsewhere")
    assert config.config_file() == application_root / ".livebound" / "config.json"


def test_a_fresh_installation_does_not_create_the_livebound_default(tmp_path, monkeypatch):
    application_root = tmp_path / "installation"
    expected = application_root / ".livebound" / "data"
    monkeypatch.setattr(config, "_APPLICATION_ROOT", application_root)
    monkeypatch.delenv("LIVEBOUND_DATA_DIR", raising=False)
    monkeypatch.setattr(config, "_pointer", None)
    monkeypatch.setattr(config, "_pointer_read", False)

    assert config.data_dir() == expected
    assert not expected.exists()
    assert not config.config_file().exists()


def test_two_installations_keep_independent_pointers_under_one_user_home(
    tmp_path, monkeypatch
):
    first_root = tmp_path / "first-installation"
    second_root = tmp_path / "second-installation"
    first_data = tmp_path / "first-data"
    second_data = tmp_path / "second-data"
    fake_home = tmp_path / "home"
    monkeypatch.setattr(config.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.delenv("LIVEBOUND_DATA_DIR", raising=False)

    monkeypatch.setattr(config, "_APPLICATION_ROOT", first_root)
    monkeypatch.setattr(config, "_pointer", None)
    monkeypatch.setattr(config, "_pointer_read", False)
    config.set_data_dir(first_data)

    monkeypatch.setattr(config, "_APPLICATION_ROOT", second_root)
    monkeypatch.setattr(config, "_pointer", None)
    monkeypatch.setattr(config, "_pointer_read", False)
    config.set_data_dir(second_data)

    monkeypatch.setattr(config, "_APPLICATION_ROOT", first_root)
    monkeypatch.setattr(config, "_pointer", None)
    monkeypatch.setattr(config, "_pointer_read", False)
    assert config.data_dir(create=False) == first_data

    monkeypatch.setattr(config, "_APPLICATION_ROOT", second_root)
    monkeypatch.setattr(config, "_pointer", None)
    monkeypatch.setattr(config, "_pointer_read", False)
    assert config.data_dir(create=False) == second_data


def test_a_move_takes_the_state_and_the_thumbnails_along(pointed, tmp_path):
    from fastapi.testclient import TestClient

    from backend.main import create_app
    from backend.store import posts as post_store

    post_store.create(title="Before the move")
    (config.thumbnail_dir() / "abc.webp").write_bytes(b"thumb")
    source = config.data_dir()
    target = tmp_path / "moved"

    with TestClient(create_app()) as client:
        response = client.post("/api/settings/data-dir", json={"path": str(target)})
        assert response.status_code == 200
        job = jobs.get(response.json()["id"])
        assert job is not None
        deadline = time.monotonic() + 2.0
        while job.status in ("starting", "running") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert job.status == "done", job.error
        result = job.result

    assert result["to"] == str(target)
    assert (target / "publisher.db").exists()
    assert (target / "thumbnails" / "abc.webp").read_bytes() == b"thumb"
    assert not (source / "publisher.db").exists(), "the old copy is gone once it is proven"
    assert config.data_dir() == target
    stored = json.loads(config.config_file().read_text(encoding="utf-8"))
    assert stored["data_dir"] == str(target)
    titles = [row["title"] for row in post_store.list_posts()]
    assert "Before the move" in titles, "the state came with it"


def test_thumbnails_are_still_found_after_a_move(pointed, tmp_path, fixture_images):
    """This is why v6 stores a name rather than an absolute path."""
    from backend.scanner import service

    folder, _ = fixture_images
    from backend.store import sources as source_store

    source_store.add_root(str(folder), label="Images")
    service.scan_roots(jobs.Job(id=0, kind="scan"))
    rows, _ = image_store.search(limit=10)
    stored = rows[0]["thumbnail_path"]

    _move(tmp_path / "moved")

    assert image_store.search(limit=10)[0][0]["thumbnail_path"] == stored
    assert (config.thumbnail_dir() / stored).exists()


def test_a_target_that_is_not_empty_is_refused(pointed, tmp_path):
    target = tmp_path / "busy"
    target.mkdir()
    (target / "something").write_text("x")

    with pytest.raises(ValueError, match="not empty"):
        relocate.check_target(str(target))


def test_a_target_inside_the_current_directory_is_refused(pointed):
    inside = config.data_dir() / "nested"

    with pytest.raises(ValueError, match="inside"):
        relocate.check_target(str(inside))


def test_a_relative_path_is_refused(pointed):
    with pytest.raises(ValueError, match="absolute"):
        relocate.check_target("somewhere")


def test_the_reported_source_names_the_branch_that_actually_won(monkeypatch, tmp_path):
    """`data_dir_source()` restates `data_dir()`'s precedence, so it can drift.

    The settings screen tells the user whether the location is remembered, and a
    wrong answer there is acted upon: believing an environment override was
    stored, they restart without it and meet an empty installation.
    """
    chosen = tmp_path / "from-environment"
    pointed = tmp_path / "from-pointer"
    fallback = tmp_path / "from-default"
    monkeypatch.setattr(config, "default_data_dir", lambda: fallback)

    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(chosen))
    monkeypatch.setattr(config, "_pointed_data_dir", lambda: pointed)
    assert config.data_dir_source() == "environment"
    assert config.data_dir(create=False) == chosen

    monkeypatch.delenv("LIVEBOUND_DATA_DIR")
    assert config.data_dir_source() == "pointer"
    assert config.data_dir(create=False) == pointed

    monkeypatch.setattr(config, "_pointed_data_dir", lambda: None)
    assert config.data_dir_source() == "default"
    assert config.data_dir(create=False) == fallback
