"""Fetching images that do not exist locally.

Generate straight on CivitAI and post immediately, and the files were never on
disk. Without them the archive stays incomplete.
"""

from __future__ import annotations

from pathlib import Path

from backend import archive, db, jobs
from backend.posts import fetch_images, lifecycle
from backend.store import images as image_store
from backend.store import posts as post_store
from backend.store import sources as source_store
from backend.store import usage


def _remote_post(*, published: bool, remote_image_id: int = 501) -> int:
    post_id = post_store.create(title="Von CivitAI")
    post_store.set_fields(
        post_id,
        remote_post_id=901,
        origin="imported",
        remote_published_at="2020-01-01T00:00:00Z" if published else "2099-01-01T00:00:00Z",
    )
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO post_images(post_id, image_id, position, source_path, sha256,"
            " content_type, remote_uuid, remote_image_id, remote_url)"
            " VALUES(?,NULL,0,'','','image/png',?,?,?)",
            (post_id, "uuid-z", remote_image_id, "https://image.civitai.com/ns/uuid-z/width=450"),
        )
    return post_id


def _serve(monkeypatch, source: Path):
    def fake(url, target):
        assert url.endswith("/original=true"), "the original, not the preview"
        target.write_bytes(source.read_bytes())

    monkeypatch.setattr(fetch_images.media, "download", fake)


def _configure_adopted(client, tmp_path) -> Path:
    folder = tmp_path / "adopted"
    response = client.put("/api/settings", json={"adopted_folder": str(folder)})
    assert response.status_code == 200
    return folder


def test_a_fetched_published_post_takes_the_ordinary_archive_route(
    client, fixture_images, tmp_path, monkeypatch
):
    archive_dir = tmp_path / "archiv"
    archive_dir.mkdir()
    source_store.add_root(str(archive_dir), label="Archiv", is_archive=True)
    adopted = _configure_adopted(client, tmp_path)
    _serve(monkeypatch, fixture_images[1][0])

    post_id = _remote_post(published=True)
    post_store.set_fields(post_id, state=lifecycle.PUBLISHED)
    fetched = fetch_images.run(jobs.Job(id=0, kind="fetch"), post_id)

    assert fetched["fetched"] == 1
    staged = list((adopted / "post_id_901").iterdir())
    assert len(staged) == 1
    assert archive_dir not in staged[0].parents, "fetching does not archive behind the UI"
    assert archive.plan() == [], "publication alone does not select a post for archiving"

    lifecycle.transition(post_id, lifecycle.ARCHIVED, reason="user archived post")
    plan = archive.plan()
    assert [item["source_path"] for item in plan] == [str(staged[0])]

    archived = archive.run(jobs.Job(id=0, kind="archive"), None)

    assert archived["moved"] == 1
    assert post_store.get(post_id)["state"] == lifecycle.ARCHIVED
    files = list((archive_dir / "2020-01-01__901").iterdir())
    assert len(files) == 1
    assert files[0].name.endswith("_501.png"), "the archive move adds the image id"


def test_an_unpublished_post_lands_in_the_adopted_folder(
    client, scanned, tmp_path, monkeypatch
):
    """What is not public yet has no business in the archive."""
    adopted = _configure_adopted(client, tmp_path)
    _serve(monkeypatch, Path(image_store.get(scanned[0])["absolute_path"]))

    post_id = _remote_post(published=False, remote_image_id=502)
    result = fetch_images.run(jobs.Job(id=0, kind="fetch"), post_id)

    assert result["fetched"] == 1
    assert result["target"] == str(adopted)
    assert (adopted / "post_id_901").exists()


def test_a_published_post_needs_the_adopted_setting(client, scanned, monkeypatch):
    _serve(monkeypatch, Path(image_store.get(scanned[0])["absolute_path"]))
    post_id = _remote_post(published=True, remote_image_id=503)

    job = jobs.Job(id=0, kind="fetch")
    fetch_images.run(job, post_id)

    assert job.error and "adopted folder" in job.error and "Settings" in job.error


def test_a_published_post_needs_no_archive_folder(
    client, scanned, tmp_path, monkeypatch
):
    adopted = _configure_adopted(client, tmp_path)
    _serve(monkeypatch, Path(image_store.get(scanned[0])["absolute_path"]))
    post_id = _remote_post(published=True, remote_image_id=503)

    job = jobs.Job(id=0, kind="fetch")
    result = fetch_images.run(job, post_id)

    assert job.error is None
    assert result["fetched"] == 1
    assert (adopted / "post_id_901").is_dir()


def test_the_adopted_setting_accepts_only_a_separate_folder(client, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    source_store.add_root(str(source), label="Source")
    source_store.add_root(str(archive_dir), label="Archive", is_archive=True)

    valid = tmp_path / "adopted"
    saved = client.put("/api/settings", json={"adopted_folder": str(valid)})
    assert saved.status_code == 200
    assert saved.json()["adopted_folder"] == str(valid)

    for parent in (source, archive_dir):
        response = client.put(
            "/api/settings", json={"adopted_folder": str(parent / "nested")}
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["code"] == "bad_adopted_folder"
        assert "Choose a separate folder" in detail["message"]


def test_a_fetched_image_joins_the_library(client, scanned, tmp_path, monkeypatch):
    """From then on it is an image like any other - with hashes, metadata and the
    duplicate check."""
    _configure_adopted(client, tmp_path)
    original = Path(image_store.get(scanned[0])["absolute_path"])
    _serve(monkeypatch, original)

    post_id = _remote_post(published=True, remote_image_id=504)
    fetch_images.run(jobs.Job(id=0, kind="fetch"), post_id)

    row = post_store.images(post_id)[0]
    assert row["image_id"], "the row now points at a library image"
    assert row["sha256"], "and knows its hash"

    indexed = image_store.get(row["image_id"])
    assert indexed["parsed"]["prompt"], "the metadata was read"
    assert indexed["thumbnail_path"], "and a thumbnail was produced"

    # and it counts as used, or the same file would later look free
    assert usage.check(indexed["sha256"], indexed["phash"])["has_exact"]


def test_an_existing_adopted_folder_is_reused_without_overwriting(
    client, scanned, tmp_path, monkeypatch
):
    adopted = _configure_adopted(client, tmp_path)
    (adopted / "post_id_901").mkdir(parents=True)
    _serve(monkeypatch, Path(image_store.get(scanned[0])["absolute_path"]))

    post_id = _remote_post(published=True, remote_image_id=505)
    existing = adopted / "post_id_901" / "uuid-z.png"
    existing.write_bytes(b"already there")

    fetch_images.run(jobs.Job(id=0, kind="fetch"), post_id)

    assert existing.read_bytes() == b"already there", "the existing file stays"
    assert (adopted / "post_id_901" / "uuid-z-1.png").exists(), "the new one gives way"


def test_an_image_that_is_already_local_is_left_alone(client, scanned, tmp_path):
    _configure_adopted(client, tmp_path)

    post_id = post_store.create(title="Normal")
    post_store.set_fields(post_id, remote_post_id=902, remote_published_at="2020-01-01T00:00:00Z")
    post_store.set_images(post_id, scanned)

    result = fetch_images.run(jobs.Job(id=0, kind="fetch"), post_id)
    assert result["fetched"] == 0, "nothing to fetch when every image is already local"


def test_a_failing_download_does_not_abort_the_run(client, scanned, tmp_path, monkeypatch):
    _configure_adopted(client, tmp_path)

    def boom(url, target):
        raise RuntimeError("network gone")

    monkeypatch.setattr(fetch_images.media, "download", boom)
    post_id = _remote_post(published=True, remote_image_id=506)

    result = fetch_images.run(jobs.Job(id=0, kind="fetch"), post_id)
    assert result["failed"] == 1
    assert result["fetched"] == 0


def test_the_staging_folder_is_isolated_in_tests(client, tmp_path):
    """Meta test. Without the environment variable these tests write into the real
    project folder - which is exactly what turned up during a cleanup."""
    from backend import config

    pending = config.pending_images_dir()
    assert str(tmp_path) in str(pending), f"not isolated: {pending}"
    assert "tmp_images" not in str(config.project_root() / "x") or pending != (
        config.project_root() / "tmp_images"
    )


# --- Videos ------------------------------------------------------------------


def _video_post() -> int:
    """A mixed post: one image and one video, both only on CivitAI."""
    post_id = post_store.create(title="Gemischt")
    post_store.set_fields(
        post_id, remote_post_id=903, remote_published_at="2020-01-01T00:00:00Z"
    )
    with db.transaction() as conn:
        for index, (kind, mime, remote_id) in enumerate(
            [("image", "image/png", 601), ("video", "video/mp4", 602)]
        ):
            conn.execute(
                "INSERT INTO post_images(post_id, image_id, position, source_path, sha256,"
                " content_type, media_type, remote_uuid, remote_image_id, remote_url)"
                " VALUES(?,NULL,?,'','',?,?,?,?,?)",
                (
                    post_id,
                    index,
                    mime,
                    kind,
                    f"uuid-{kind}",
                    remote_id,
                    f"https://image.civitai.com/ns/uuid-{kind}/width=450",
                ),
            )
    return post_id


def test_a_mixed_post_fetches_only_the_images(client, scanned, tmp_path, monkeypatch):
    """Videos are handled outside this app - and the base64 route could not move
    them anyway."""
    adopted = _configure_adopted(client, tmp_path)
    _serve(monkeypatch, Path(image_store.get(scanned[0])["absolute_path"]))

    post_id = _video_post()
    result = fetch_images.run(jobs.Job(id=0, kind="fetch"), post_id)

    assert result["fetched"] == 1, "only the image"
    assert result["videos"] == 1, "and the video is counted, not passed over silently"

    files = [p.name for p in (adopted / "post_id_903").iterdir()]
    assert len(files) == 1
    assert files[0].endswith("uuid-image.png")


def test_the_video_row_stays_so_the_post_is_shown_truthfully(client, tmp_path):
    """Hiding the row would misrepresent the post."""
    post_id = _video_post()
    rows = post_store.images(post_id)

    assert len(rows) == 2
    video = next(row for row in rows if row["media_type"] == "video")
    assert video["remote_url"], "and it leads to the video"
    assert fetch_images.is_video(video)


def test_a_video_is_recognised_by_mime_type_alone(client):
    """Some responses carry no type, but a MIME type."""
    assert fetch_images.is_video({"content_type": "video/mp4"})
    assert fetch_images.is_video({"media_type": "video"})
    assert not fetch_images.is_video({"content_type": "image/png", "media_type": "image"})


def test_matching_leaves_videos_alone(client, scanned):
    from backend.posts import matching

    post_id = _video_post()
    assert matching.resolve(post_id, download=False)["checked"] == 1, "only the image"
