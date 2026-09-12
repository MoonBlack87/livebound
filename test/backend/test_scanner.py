"""Scanner ingestion boundaries."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image

from backend import cli, db, hashing, jobs, thumbnails
from backend.metadata import png_io
from backend.scanner import service
from backend.store import images as image_store
from backend.store import sources as source_store


def _image_root(tmp_path: Path, suffix: str = ".png") -> tuple[Path, dict]:
    folder = tmp_path / "scanner"
    folder.mkdir()
    path = folder / f"image{suffix}"
    Image.new("RGB", (12, 12), color=(40, 80, 120)).save(path)
    return path, source_store.add_root(str(folder), label="Scanner")


def _scan() -> dict:
    return service.scan_roots(jobs.Job(id=0, kind="scan"))


def _write_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (12, 12), color=(40, 80, 120)).save(path)


def test_only_the_configured_trash_path_is_excluded_across_source_roots(client, tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    configured = first_root / "trash" / "nested" / "configured.png"
    other = second_root / "trash" / "other.png"
    _write_image(configured)
    _write_image(other)
    source_store.add_root(str(first_root), label="First")
    source_store.add_root(str(second_root), label="Second")

    client.put("/api/settings", json={"trash_folder": str(configured.parent.parent)})
    _scan()

    assert image_store.get_by_path(str(configured)) is None
    assert image_store.get_by_path(str(other)) is not None


def test_a_second_source_root_does_not_inherit_the_trash_name(client, tmp_path):
    configured_trash = tmp_path / "configured-trash"
    client.put("/api/settings", json={"trash_folder": str(configured_trash)})
    second_root = tmp_path / "second"
    image = second_root / "trash" / "image.png"
    _write_image(image)
    root = source_store.add_root(str(second_root), label="Second", excluded=["kept"])

    _scan()

    assert root["excluded_folders"] == ["kept"]
    assert image_store.get_by_path(str(image)) is not None


def test_a_trash_folder_passed_as_the_root_yields_no_images(tmp_path):
    trash = tmp_path / "trash"
    image = trash / "image.png"
    _write_image(image)

    assert list(service.iter_images(trash, trash_folder=trash)) == []


def test_changing_the_trash_folder_scans_the_old_one_again(client, tmp_path):
    root = tmp_path / "images"
    old_trash = root / "old-trash"
    new_trash = root / "new-trash"
    old_image = old_trash / "old.png"
    new_image = new_trash / "new.png"
    _write_image(old_image)
    _write_image(new_image)
    source_store.add_root(str(root), label="Images")
    client.put("/api/settings", json={"trash_folder": str(old_trash)})
    _scan()

    client.put("/api/settings", json={"trash_folder": str(new_trash)})
    _scan()

    assert image_store.get_by_path(str(old_image)) is not None
    assert image_store.get_by_path(str(new_image)) is None
    assert source_store.list_roots()[0]["excluded_folders"] == []


def test_changing_the_trash_folder_preserves_user_exclusions(client, tmp_path):
    root = tmp_path / "images"
    old_trash = root / "old-trash"
    source_store.add_root(str(root), label="Images", excluded=[old_trash.name])
    client.put("/api/settings", json={"trash_folder": str(old_trash)})

    client.put("/api/settings", json={"trash_folder": str(root / "new-trash")})

    assert source_store.list_roots()[0]["excluded_folders"] == [old_trash.name]


def test_clearing_the_trash_folder_excludes_nothing(client, tmp_path):
    root = tmp_path / "images"
    trash = root / "trash"
    image = trash / "image.png"
    _write_image(image)
    source_store.add_root(str(root), label="Images")
    client.put("/api/settings", json={"trash_folder": str(trash)})
    _scan()

    client.put("/api/settings", json={"trash_folder": ""})
    _scan()

    assert image_store.get_by_path(str(image)) is not None
    assert source_store.list_roots()[0]["excluded_folders"] == []


def test_an_unchanged_current_file_only_refreshes_last_seen_at(tmp_path, monkeypatch):
    path, _root = _image_root(tmp_path)
    _scan()
    before = image_store.get_by_path(str(path))

    monkeypatch.setattr(db, "now_iso", lambda: "2026-08-29T20:00:00Z")

    def unexpected_read(_path: Path):
        raise AssertionError("an unchanged current file must not be read again")

    monkeypatch.setattr(png_io, "read", unexpected_read)
    totals = _scan()
    after = image_store.get_by_path(str(path))

    assert totals == {
        "scanned": 1,
        "added": 0,
        "failed": 0,
        "missing": 0,
        "unchanged": 1,
        "reapplied": 0,
    }
    assert after == {**before, "last_seen_at": "2026-08-29T20:00:00Z"}


def test_an_older_ingest_revision_is_read_again(tmp_path, monkeypatch):
    path, _root = _image_root(tmp_path)
    _scan()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE images SET ingest_revision=? WHERE absolute_path=?",
            (service.INGEST_REVISION - 1, str(path)),
        )

    original_read = png_io.read
    calls: list[Path] = []

    def counted_read(candidate: Path, *, image=None):
        calls.append(candidate)
        return original_read(candidate, image=image)

    monkeypatch.setattr(png_io, "read", counted_read)
    totals = _scan()

    assert calls == [path]
    assert totals["unchanged"] == 0
    assert image_store.get_by_path(str(path))["ingest_revision"] == service.INGEST_REVISION


@pytest.mark.parametrize(
    ("suffix", "expected_opens"),
    [(".png", 1), (".jpg", 2), (".webp", 1)],
)
def test_a_new_image_is_opened_only_as_required_for_metadata_and_hashing(
    tmp_path, monkeypatch, suffix, expected_opens
):
    path, _root = _image_root(tmp_path, suffix)
    original_open = Image.open
    opened: list[Path] = []

    def counted_open(candidate, *args, **kwargs):
        opened.append(Path(candidate))
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(hashing.Image, "open", counted_open)
    monkeypatch.setattr(thumbnails, "ensure", lambda *_args, **_kwargs: None)

    totals = _scan()

    assert totals["scanned"] == 1
    assert opened == [path] * expected_opens


def test_an_unchanged_file_refreshes_its_location_after_root_reconfiguration(tmp_path):
    layout = tmp_path / "layout"
    original_root = layout / "nested"
    original_root.mkdir(parents=True)
    path = original_root / "image.png"
    Image.new("RGB", (12, 12), color=(40, 80, 120)).save(path)
    root = source_store.add_root(str(original_root), label="Scanner")
    _scan()
    existing = image_store.get_by_path(str(path))

    with db.transaction() as conn:
        conn.execute("UPDATE source_roots SET path=? WHERE id=?", (str(layout), root["id"]))
    totals = _scan()

    refreshed = image_store.get_by_path(str(path))
    assert totals["unchanged"] == 1
    assert refreshed["id"] == existing["id"]
    assert refreshed["absolute_path"] == str(path)
    assert refreshed["relative_path"] == "nested/image.png"
    assert refreshed["folder"] == "nested"


def test_an_interrupted_scan_is_completed_correctly_by_the_next_scan(
    tmp_path, monkeypatch
):
    first, _root = _image_root(tmp_path)
    second = first.with_name("second.png")
    Image.new("RGB", (12, 12), color=(120, 80, 40)).save(second)
    _scan()

    with db.transaction() as conn:
        conn.execute("UPDATE images SET last_seen_at='before-interruption'")

    class StopAfterOne(jobs.Job):
        def should_stop(self) -> bool:
            return self.processed >= 1

    monkeypatch.setattr(db, "now_iso", lambda: "during-interruption")
    stopped = service.scan_roots(StopAfterOne(id=1, kind="scan"))
    rows = [image_store.get_by_path(str(path)) for path in (first, second)]

    assert stopped["unchanged"] == 1
    assert [row["is_missing"] for row in rows] == [False, False]
    assert sorted(row["last_seen_at"] for row in rows) == [
        "before-interruption",
        "during-interruption",
    ]

    monkeypatch.setattr(db, "now_iso", lambda: "after-restart")
    completed = _scan()
    rows = [image_store.get_by_path(str(path)) for path in (first, second)]

    assert completed["unchanged"] == 2
    assert completed["missing"] == 0
    assert all(row["last_seen_at"] == "after-restart" for row in rows)
    assert all(not row["is_missing"] for row in rows)


def test_cli_full_reads_a_current_unchanged_file_again(tmp_path, monkeypatch):
    path, _root = _image_root(tmp_path)
    _scan()
    original_read = png_io.read
    calls: list[Path] = []

    def counted_read(candidate: Path, *, image=None):
        calls.append(candidate)
        return original_read(candidate, image=image)

    monkeypatch.setattr(png_io, "read", counted_read)
    monkeypatch.setattr(sys, "argv", ["livebound", "scan", "--full"])

    assert cli.main() == 0
    assert calls == [path]


def test_a_scan_reports_done_only_after_thumbnail_housekeeping(monkeypatch):
    job = jobs.Job(id=0, kind="scan")
    stages: list[str] = []
    monkeypatch.setattr(
        thumbnails,
        "prune",
        lambda *_args, **_kwargs: stages.append(job.stage),
    )

    service.scan_roots(job)

    assert stages == [""]
    assert job.stage == "Done"


def test_a_scan_counts_and_logs_an_unreadable_image(tmp_path):
    readable, _root = _image_root(tmp_path)

    first_job = jobs.Job(id=0, kind="scan")
    first_totals = service.scan_roots(first_job)

    unreadable = readable.with_name("unreadable.png")
    unreadable.write_bytes(b"not an image")
    job = jobs.Job(id=1, kind="scan")
    totals = service.scan_roots(job)

    assert first_totals["failed"] == first_job.failed == 0
    assert totals["failed"] == job.failed == 1
    assert totals["scanned"] == 1
    assert job.items[0]["path"] == str(unreadable)
    assert job.items[0]["status"] == "error"
    assert "could not be read as an image" in job.items[0]["message"]
