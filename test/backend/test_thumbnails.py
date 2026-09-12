"""The thumbnail cache.

Two sizes and a name rather than a path - both so the data directory can be
moved later without every row pointing nowhere.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from PIL import Image

from backend import config, db, thumbnails
from backend.store import images as image_store


def test_a_scan_stores_a_name_not_a_path(client, scanned):
    stored = image_store.get(scanned[0])["thumbnail_path"]
    assert stored and "/" not in stored, "an absolute path would not survive a move"
    assert (config.thumbnail_dir() / stored).exists()


def test_the_two_sizes_live_side_by_side(client, scanned):
    image = image_store.get(scanned[0])
    source = Path(image["absolute_path"])
    small = thumbnails.ensure(source, image["sha256"])
    large = thumbnails.ensure(
        source, image["sha256"], edge=config.THUMBNAIL_LARGE_EDGE
    )

    assert small != large, "the edge length is part of the key"
    assert thumbnails.path_for(small).exists() and thumbnails.path_for(large).exists()

    with Image.open(thumbnails.path_for(large)) as opened:
        assert max(opened.size) <= config.THUMBNAIL_LARGE_EDGE
    with Image.open(thumbnails.path_for(small)) as opened:
        assert max(opened.size) <= config.THUMBNAIL_MAX_EDGE


def test_the_large_variant_is_made_on_request(client, scanned):
    """Producing it during the scan would double the cost for a size most
    images are never shown at."""
    response = client.get(f"/api/images/{scanned[0]}/thumbnail?size=large")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    with Image.open(io.BytesIO(response.content)) as opened:
        assert max(opened.size) <= config.THUMBNAIL_LARGE_EDGE


def test_a_scan_rebuilds_a_changed_file_with_the_same_mtime_and_size(client, tmp_path):
    folder = tmp_path / "same-stat"
    folder.mkdir()
    source = folder / "source.png"
    original = Image.new("RGB", (32, 32), (10, 20, 30))
    original.save(source, "PNG", compress_level=0)
    client.post("/api/sources", json={"path": str(folder), "label": "Test"})

    from backend import jobs
    from backend.scanner import service

    service.scan_roots(jobs.Job(id=0, kind="scan"))
    before = image_store.get_by_path(str(source))
    before_name = before["thumbnail_path"]
    before_stat = source.stat()

    changed = Image.new("RGB", (32, 32), (30, 20, 10))
    changed.save(source, "PNG", compress_level=0)
    os.utime(source, (before_stat.st_atime, before_stat.st_mtime))

    service.scan_roots(jobs.Job(id=1, kind="scan"))
    after = image_store.get_by_path(str(source))

    assert source.stat().st_size == before_stat.st_size
    assert source.stat().st_mtime == before_stat.st_mtime
    assert after["sha256"] != before["sha256"]
    assert after["thumbnail_path"] != before_name
    assert thumbnails.path_for(after["thumbnail_path"]).exists()
    assert not thumbnails.path_for(before_name).exists()


def test_a_versioned_thumbnail_is_immutable_and_changes_url_with_content(
    client, fixture_images
):
    folder, paths = fixture_images
    client.post("/api/sources", json={"path": str(folder), "label": "Test"})

    from backend import jobs
    from backend.scanner import service

    service.scan_roots(jobs.Job(id=0, kind="scan"))
    first = image_store.get_by_path(str(paths[0]))
    first_url = f'/api/images/{first["id"]}/thumbnail?v={first["thumbnail_path"]}'
    response = client.get(first_url)

    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    other = image_store.get_by_path(str(paths[1]))
    other_url = f'/api/images/{other["id"]}/thumbnail?v={other["thumbnail_path"]}'
    assert other["thumbnail_path"] != first["thumbnail_path"]
    assert other_url != first_url


def test_only_the_current_served_name_is_immutable(client, scanned):
    image = image_store.get(scanned[0])
    current_name = image["thumbnail_path"]

    unversioned = client.get(f'/api/images/{image["id"]}/thumbnail')
    assert "cache-control" not in unversioned.headers

    legacy_name = "legacy-thumbnail.webp"
    thumbnails.path_for(legacy_name).write_bytes(
        thumbnails.path_for(current_name).read_bytes()
    )
    with db.transaction() as conn:
        conn.execute(
            "UPDATE images SET thumbnail_path=? WHERE id=?",
            (legacy_name, image["id"]),
        )

    mismatched = client.get(
        f'/api/images/{image["id"]}/thumbnail?v={current_name}'
    )
    assert mismatched.status_code == 200
    assert "cache-control" not in mismatched.headers


def test_pruning_skips_one_cache_file_that_cannot_be_removed(tmp_path, monkeypatch):
    failed = config.thumbnail_dir() / "failed.webp"
    removable = config.thumbnail_dir() / "removable.webp"
    failed.write_bytes(b"failed")
    removable.write_bytes(b"removable")
    real_unlink = Path.unlink

    def unlink(candidate: Path, *args, **kwargs):
        if candidate == failed:
            raise OSError("held open")
        return real_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    assert thumbnails.prune([]) == 1
    assert failed.exists()
    assert not removable.exists()


def test_the_migration_turns_stored_paths_into_names(client, scanned):
    """A v5 database holds absolute paths; the files stay valid, the rows change."""
    stored = image_store.get(scanned[0])["thumbnail_path"]
    absolute = str(config.thumbnail_dir() / stored)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE images SET thumbnail_path=? WHERE id=?", (absolute, scanned[0])
        )
        conn.execute("PRAGMA user_version=5")
    db.close_connection()

    db.init_db()

    assert image_store.get(scanned[0])["thumbnail_path"] == stored
