"""Matching the images of an adopted post to local files.

An adopted post knows its images only as delivery URLs. Often the same pictures
have long been sitting in the source folders - they were the files the post was
made from.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backend import db
from backend.civitai.errors import AuthError, TransportError
from backend.posts import lifecycle, matching, push
from backend.store import images as image_store
from backend.store import posts as post_store
from backend.store import usage


def _remote_only_post(
    image_id_remote: int = 555,
    *,
    published_at: str | None = None,
    remote_post_id: int = 900,
) -> int:
    """A post with one image that exists only on CivitAI."""
    post_id = post_store.create(
        title="Adopted",
        state=lifecycle.PUBLISHED if published_at else lifecycle.REMOTE_DRAFT,
    )
    post_store.set_fields(
        post_id,
        remote_post_id=remote_post_id,
        remote_published_at=published_at,
        remote_state="published" if published_at else "draft",
        origin="imported",
    )
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO post_images(post_id, image_id, position, source_path, sha256,"
            " remote_uuid, remote_image_id, remote_url) VALUES(?,NULL,0,'','',?,?,?)",
            (post_id, "uuid-x", image_id_remote, "https://image.civitai.com/ns/uuid-x/width=450"),
        )
    return post_id


def test_the_board_run_covers_every_post_that_still_misses_a_file(client, scanned):
    """Adding a source folder later leaves every earlier adoption without local
    files; going into each post to press the same button is the work this takes
    over."""
    from backend import jobs

    first = _remote_only_post(image_id_remote=811, remote_post_id=911)
    second = _remote_only_post(image_id_remote=812, remote_post_id=912)
    settled = _remote_only_post(image_id_remote=813, remote_post_id=913)
    image = image_store.get(scanned[0])
    matching._link(post_store.images(settled)[0]["id"], image, settled)

    assert matching.unmatched_post_ids() == [first, second]

    usage.record(
        sha256=image["sha256"],
        phash=image["phash"],
        post_id=None,
        remote_post_id=901,
        remote_image_id=811,
        source_path=image["absolute_path"],
        post_title="earlier",
        status=usage.PUBLISHED,
        origin="backfill",
    )
    job = jobs.Job(id=1, kind="match")

    totals = matching.resolve_all(job, download=False)

    assert (totals["posts"], totals["matched"]) == (2, 1)
    assert post_store.images(first)[0]["image_id"] == image["id"]
    assert post_store.images(second)[0]["image_id"] is None, "unfound stays untouched"
    assert matching.unmatched_post_ids() == [second]


def test_the_board_run_skips_a_pushing_post_and_keeps_processing(client, scanned):
    from backend import jobs

    pushing = _remote_only_post(image_id_remote=814, remote_post_id=914)
    available = _remote_only_post(image_id_remote=815, remote_post_id=915)
    post_store.set_fields(pushing, state=lifecycle.PUSHING)
    image = image_store.get(scanned[0])
    usage.record(
        sha256=image["sha256"],
        phash=image["phash"],
        post_id=None,
        remote_post_id=915,
        remote_image_id=815,
        source_path=image["absolute_path"],
        post_title="earlier",
        status=usage.PUBLISHED,
        origin="backfill",
    )

    assert matching.unmatched_post_ids() == [available]

    totals = matching.resolve_all(jobs.Job(id=1, kind="match"), download=False)

    assert (totals["posts"], totals["matched"]) == (1, 1)
    assert post_store.images(pushing)[0]["image_id"] is None
    assert post_store.images(available)[0]["image_id"] == image["id"]


def test_the_board_run_survives_one_failing_post(client, scanned, monkeypatch):
    """One post that cannot be read must not end a run over all of them."""
    from backend import jobs

    first = _remote_only_post(image_id_remote=821, remote_post_id=921)
    second = _remote_only_post(image_id_remote=822, remote_post_id=922)
    real = matching.resolve

    def explode(post_id, **options):
        if post_id == first:
            raise TransportError("Network error during post.get: reset")
        return real(post_id, **options)

    monkeypatch.setattr(matching, "resolve", explode)
    job = jobs.Job(id=1, kind="match")

    totals = matching.resolve_all(job, download=False)

    assert totals["posts"] == 2
    assert job.failed == 1
    assert any(entry.get("post_id") == first for entry in job.items)
    assert second in matching.unmatched_post_ids()


def test_a_refused_key_ends_the_board_run(client, scanned, monkeypatch):
    """Answering "nothing found" to a refused key would be a lie the user acts on."""
    from backend import jobs

    _remote_only_post(image_id_remote=831, remote_post_id=931)
    _remote_only_post(image_id_remote=832, remote_post_id=932)
    monkeypatch.setattr(
        matching, "resolve", lambda post_id, **options: (_ for _ in ()).throw(AuthError("no"))
    )

    with pytest.raises(AuthError):
        matching.resolve_all(jobs.Job(id=1, kind="match"), download=False)


def test_the_history_resolves_it_without_touching_the_network(client, scanned):
    """If the image was ever recorded - by the backfill or a push - its CivitAI id
    sits next to the hash. That is one query, not a network call."""
    local = image_store.get(scanned[0])
    usage.record(
        sha256=local["sha256"],
        pixel_sha256="",
        phash=None,
        post_id=None,
        remote_post_id=900,
        remote_image_id=555,
        status=usage.PUBLISHED,
        origin="backfill",
    )
    existing_id = db.get_connection().execute(
        "SELECT id FROM image_usage WHERE remote_image_id=555"
    ).fetchone()["id"]
    published_at = "2024-03-02T12:34:56Z"
    post_id = _remote_only_post(published_at=published_at)

    result = matching.resolve(post_id, download=False)

    assert result["matched"] == 1
    assert result["from_history"] == 1
    assert result["downloaded"] == 0

    row = post_store.images(post_id)[0]
    assert row["image_id"] == local["id"], "the local file now hangs on it"
    assert row["source_path"] == local["absolute_path"]
    assert row["sha256"] == local["sha256"]

    history = db.get_connection().execute(
        "SELECT * FROM image_usage WHERE remote_image_id=555"
    ).fetchall()
    assert len(history) == 1
    assert history[0]["id"] == existing_id
    assert history[0]["pixel_sha256"] == local["pixel_sha256"]
    assert history[0]["phash"] == local["phash"]
    assert history[0]["post_id"] == post_id
    assert history[0]["source_path"] == local["absolute_path"]
    assert history[0]["post_title"] == "Adopted"
    assert history[0]["used_at"] == published_at
    assert history[0]["origin"] == "backfill"


def test_the_delivery_url_is_kept_as_a_fallback(client, scanned):
    """If the file is lost later, the image can still be displayed."""
    local = image_store.get(scanned[0])
    usage.record(
        sha256=local["sha256"], phash=local["phash"], post_id=None,
        remote_image_id=555, status=usage.PUBLISHED,
    )
    post_id = _remote_only_post()
    matching.resolve(post_id, download=False)

    assert post_store.images(post_id)[0]["remote_url"]


def test_an_unknown_image_stays_remote_only(client, scanned):
    post_id = _remote_only_post(image_id_remote=999)

    result = matching.resolve(post_id, download=False)

    assert result["matched"] == 0
    assert post_store.images(post_id)[0]["image_id"] is None


def test_a_download_that_matches_byte_for_byte_is_linked(client, scanned, monkeypatch):
    """The expensive route: fetch, hash, compare."""
    local = image_store.get(scanned[0])
    source = Path(local["absolute_path"])

    def fake_download(url, target):
        assert url.endswith("/original=true"), "the original, not the preview"
        target.write_bytes(source.read_bytes())

    monkeypatch.setattr(matching.media, "download", fake_download)
    post_id = _remote_only_post(image_id_remote=777)

    result = matching.resolve(post_id, download=True)

    assert result["downloaded"] == 1
    assert post_store.images(post_id)[0]["image_id"] == local["id"]


def test_a_matched_published_adoption_enters_usage_history_once(
    client, scanned, monkeypatch
):
    """A downloaded match is the missing bridge from an adopted publication to
    both the library badge and the later push safeguard."""
    from backend import jobs
    from backend.scanner import service

    original = image_store.get(scanned[0])
    copy = Path(original["absolute_path"]).with_name("777-identical-copy.png")
    shutil.copy2(original["absolute_path"], copy)
    service.scan_roots(jobs.Job(id=0, kind="scan"))
    twin_ids = [
        row["id"]
        for row in db.get_connection().execute(
            "SELECT id FROM images WHERE sha256=? ORDER BY id", (original["sha256"],)
        )
    ]
    twins = image_store.get_many(twin_ids)
    assert len(twins) == 2

    monkeypatch.setattr(
        matching.media,
        "download",
        lambda url, target: target.write_bytes(Path(original["absolute_path"]).read_bytes()),
    )
    published_at = "2024-03-02T12:34:56Z"
    post_id = _remote_only_post(image_id_remote=777, published_at=published_at)

    assert matching.resolve(post_id, download=True)["matched"] == 1
    assert matching.resolve(post_id, download=True)["matched"] == 0

    rows = [
        dict(row)
        for row in db.get_connection().execute(
            "SELECT * FROM image_usage WHERE remote_image_id=?", (777,)
        )
    ]
    assert len(rows) == 1
    recorded = rows[0]
    linked = post_store.images(post_id)[0]
    assert Path(linked["source_path"]).name == copy.name, "the complete remote id is prioritised"
    identity = usage.generation_identity(original["parsed"])
    assert recorded["sha256"] == original["sha256"]
    assert recorded["pixel_sha256"] == original["pixel_sha256"]
    assert recorded["phash"] == original["phash"]
    assert recorded["seed"] == identity["seed"]
    assert recorded["prompt_hash"] == identity["prompt_hash"]
    assert recorded["post_id"] == post_id
    assert recorded["remote_post_id"] == 900
    assert recorded["remote_image_id"] == 777
    assert recorded["source_path"] == linked["source_path"]
    assert recorded["post_title"] == "Adopted"
    assert recorded["used_at"] == published_at
    assert recorded["status"] == usage.PUBLISHED
    assert recorded["origin"] == "match"

    for twin in twins:
        library_match = client.get(
            "/api/images",
            params={
                "q": Path(twin["absolute_path"]).name,
                "usage_state": "used",
            },
        ).json()["items"]
        assert len(library_match) == 1
        assert library_match[0]["used"]

    for twin in twins:
        result = usage.check(
            twin["sha256"],
            twin["phash"],
            pixel_sha256=twin["pixel_sha256"],
        )
        assert result["has_certain"]
        assert len(result["exact"]) == 1

        later_id = post_store.create(
            title="Later",
            state=lifecycle.READY,
            schedule_mode="relative",
            schedule_offset_minutes=120,
        )
        post_store.set_images(later_id, [twin["id"]])
        post_store.set_tags(later_id, ["test"])
        later = post_store.get(later_id)
        later_images = post_store.images(later_id)

        with pytest.raises(RuntimeError, match="already in a post"):
            push._preflight(later, later_images, ["test"])

        post_store.set_image_flag(later_images[0]["id"], "dedup_ack", True)
        push._preflight(later, post_store.images(later_id), ["test"])

    assert db.get_connection().execute(
        "SELECT COUNT(*) FROM image_usage WHERE remote_image_id=?", (777,)
    ).fetchone()[0] == 1


def test_a_lossless_reencode_is_linked_by_exact_pixels(
    client, scanned, monkeypatch, tmp_path
):
    """Hidden delivery bytes can differ while decoded RGB pixels stay exact."""
    from PIL import Image

    local = image_store.get(scanned[0])
    reencoded = tmp_path / "anders.png"
    with Image.open(local["absolute_path"]) as image:
        image.convert("RGB").save(reencoded)  # loses the metadata
    assert reencoded.read_bytes() != Path(local["absolute_path"]).read_bytes()

    monkeypatch.setattr(
        matching.media, "download", lambda url, target: target.write_bytes(reencoded.read_bytes())
    )
    post_id = _remote_only_post(
        image_id_remote=778, published_at="2024-03-02T12:34:56Z"
    )
    db.get_connection().execute(
        "UPDATE post_images SET hide_meta=1 WHERE post_id=?", (post_id,)
    )
    db.get_connection().commit()

    assert matching.resolve(post_id, download=True)["matched"] == 1
    assert matching.resolve(post_id, download=True)["matched"] == 0
    assert post_store.images(post_id)[0]["image_id"] == local["id"]
    assert db.get_connection().execute(
        "SELECT COUNT(*) FROM image_usage WHERE remote_image_id=778"
    ).fetchone()[0] == 1


def test_a_complete_remote_id_and_close_dhash_never_replace_exact_identity(
    client, tmp_path, monkeypatch
):
    """A filename and perceptual similarity narrow a search; neither identifies it."""
    from PIL import Image

    from backend import jobs
    from backend.scanner import service
    from backend.store import sources as source_store

    remote = tmp_path / "remote.png"
    image = Image.new("RGB", (96, 128))
    for y in range(image.height):
        for x in range(image.width):
            image.putpixel((x, y), ((x * 3 + y) % 256, (y * 5 + x) % 256, x * y % 256))
    image.save(remote)

    source = tmp_path / "filename-candidates"
    source.mkdir()
    local = source / "download-781.png"
    with Image.open(remote) as candidate:
        changed = candidate.copy()
    changed.putpixel((0, 0), (255, 255, 255))
    changed.save(local)

    source_store.add_root(str(source))
    service.scan_roots(jobs.Job(id=0, kind="scan"))
    local_row = image_store.get_by_path(str(local))
    assert matching._filename_has_remote_token(local.name, {"remote_image_id": 781})
    assert not matching._filename_has_remote_token("download-1781.png", {"remote_image_id": 781})
    assert local_row["pixel_sha256"] != matching.hashing.pixel_sha256(remote)
    assert matching.hashing.hamming(local_row["phash"], matching.hashing.dhash64(remote)) <= 7

    dhash_calls = 0
    real_dhash = matching.hashing.dhash64

    def observed_dhash(path: Path):
        nonlocal dhash_calls
        dhash_calls += 1
        return real_dhash(path)

    monkeypatch.setattr(matching.hashing, "dhash64", observed_dhash)
    monkeypatch.setattr(
        matching.media, "download", lambda url, target: target.write_bytes(remote.read_bytes())
    )
    post_id = _remote_only_post(image_id_remote=781)

    assert matching.resolve(post_id, download=True)["matched"] == 0
    assert dhash_calls == 1
    assert post_store.images(post_id)[0]["image_id"] is None


def test_manual_picker_suggests_seed_prompt_and_close_jpeg_without_linking(
    client, scanned, monkeypatch, tmp_path
):
    """Owner metadata and one remote decode rank candidates; they never authorise a link."""
    from PIL import Image

    local = image_store.get(scanned[0])
    remote_jpeg = tmp_path / "remote.jpg"
    with Image.open(local["absolute_path"]) as image:
        image.convert("RGB").save(remote_jpeg, quality=92)
    assert matching.hashing.file_sha256(remote_jpeg) != local["sha256"]
    assert matching.hashing.pixel_sha256(remote_jpeg) != local["pixel_sha256"]

    parsed = local["parsed"]
    detail = {
        "images": [
            {
                "id": 782,
                "url": "uuid-x",
                "name": "changed-online-name.jpg",
                "width": local["width"],
                "height": local["height"],
                "hideMeta": True,
                "meta": {
                    "seed": parsed["fields"]["Seed"],
                    "prompt": parsed["prompt"],
                },
            }
        ]
    }
    monkeypatch.setattr(matching.trpc, "post_get_edit", lambda remote_id: detail)
    monkeypatch.setattr(
        matching.media,
        "download",
        lambda url, target: target.write_bytes(remote_jpeg.read_bytes()),
    )

    calls = {"file": 0, "pixels": 0, "dhash": 0}
    real_file = matching.hashing.file_sha256
    real_pixels = matching.hashing.pixel_sha256
    real_dhash = matching.hashing.dhash64

    def counted_file(path):
        calls["file"] += 1
        return real_file(path)

    def counted_pixels(path):
        calls["pixels"] += 1
        return real_pixels(path)

    def counted_dhash(path):
        calls["dhash"] += 1
        return real_dhash(path)

    monkeypatch.setattr(matching.hashing, "file_sha256", counted_file)
    monkeypatch.setattr(matching.hashing, "pixel_sha256", counted_pixels)
    monkeypatch.setattr(matching.hashing, "dhash64", counted_dhash)
    post_id = _remote_only_post(image_id_remote=782)
    post_image_id = post_store.images(post_id)[0]["id"]

    response = client.get(
        f"/api/posts/{post_id}/images/{post_image_id}/match-candidates"
    )

    assert response.status_code == 200
    payload = response.json()
    suggestion = next(item for item in payload["items"] if item["id"] == local["id"])
    assert suggestion["thumbnail_path"] == local["thumbnail_path"]
    reason_codes = {reason["code"] for reason in suggestion["reasons"]}
    assert "same_seed_prompt" in reason_codes
    assert "close_dhash" in reason_codes
    assert "identical_bytes" not in reason_codes
    assert "identical_pixels" not in reason_codes
    assert payload["remote_facts"] == {"seed": True, "prompt": True, "dhash": True}
    assert calls == {"file": 1, "pixels": 1, "dhash": 1}
    assert post_store.images(post_id)[0]["image_id"] is None
    assert not db.get_connection().execute("SELECT 1 FROM image_usage").fetchone()


def test_manual_match_uses_the_same_publication_history_and_can_be_corrected(
    client, scanned
):
    published_at = "2024-03-02T12:34:56Z"
    post_id = _remote_only_post(image_id_remote=783, published_at=published_at)
    post_image_id = post_store.images(post_id)[0]["id"]
    first = image_store.get(scanned[0])
    second = image_store.get(scanned[1])

    response = client.post(
        f"/api/posts/{post_id}/images/{post_image_id}/match-local/{first['id']}"
    )

    assert response.status_code == 200
    assert response.json()["images"][0]["image_id"] == first["id"]
    history = db.get_connection().execute(
        "SELECT * FROM image_usage WHERE remote_image_id=783"
    ).fetchall()
    assert len(history) == 1
    assert history[0]["sha256"] == first["sha256"]
    assert history[0]["pixel_sha256"] == first["pixel_sha256"]
    assert history[0]["post_id"] == post_id
    assert history[0]["remote_post_id"] == 900
    assert history[0]["used_at"] == published_at
    assert history[0]["status"] == usage.PUBLISHED
    assert history[0]["origin"] == "match"

    first_in_library = client.get(
        "/api/images",
        params={
            "q": Path(first["absolute_path"]).name,
            "usage_state": "used",
        },
    ).json()["items"]
    assert first_in_library[0]["used"]

    corrected = client.post(
        f"/api/posts/{post_id}/images/{post_image_id}/match-local/{second['id']}"
    )
    assert corrected.status_code == 200
    assert corrected.json()["images"][0]["image_id"] == second["id"]
    history = db.get_connection().execute(
        "SELECT * FROM image_usage WHERE remote_image_id=783"
    ).fetchall()
    assert len(history) == 1
    assert history[0]["sha256"] == second["sha256"]
    assert history[0]["pixel_sha256"] == second["pixel_sha256"]


def test_manual_match_rejects_a_local_image_already_used_by_another_post_tile(
    client, scanned
):
    post_id = _remote_only_post(image_id_remote=784)
    first_post_image = post_store.images(post_id)[0]
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO post_images(post_id, image_id, position, source_path, sha256,"
            " remote_uuid, remote_image_id, remote_url) VALUES(?,NULL,1,'','',?,?,?)",
            (post_id, "uuid-y", 785, "https://image.civitai.com/ns/uuid-y/width=450"),
        )
    second_post_image = post_store.images(post_id)[1]
    image_id = scanned[0]
    assert client.post(
        f"/api/posts/{post_id}/images/{first_post_image['id']}/match-local/{image_id}"
    ).status_code == 200

    refused = client.post(
        f"/api/posts/{post_id}/images/{second_post_image['id']}/match-local/{image_id}"
    )

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "image_already_in_post"


def test_a_failing_download_is_not_fatal(client, scanned, monkeypatch):
    def boom(url, target):
        raise TransportError("connection reset")

    monkeypatch.setattr(matching.media, "download", boom)
    post_id = _remote_only_post(image_id_remote=779)

    result = matching.resolve(post_id, download=True)
    assert result["matched"] == 0, "nothing is matched"
    assert result["unreadable"] == 1, "and the failure is counted, not swallowed"
    assert post_store.images(post_id), "and the row stays"


def test_a_refused_key_stops_matching_instead_of_reporting_no_match(
    client, scanned, monkeypatch
):
    """Every remaining image would fail the same way. Answering "no local file
    found" would send the user looking for a library problem that is really an
    expired token."""
    def refused(url, target):
        raise AuthError("401 Unauthorized")

    monkeypatch.setattr(matching.media, "download", refused)
    post_id = _remote_only_post(image_id_remote=780)

    with pytest.raises(AuthError):
        matching.resolve(post_id, download=True)


def test_a_post_with_local_files_needs_no_matching(client, scanned):
    post_id = post_store.create(title="Normal")
    post_store.set_images(post_id, scanned)

    assert matching.resolve(post_id)["checked"] == 0


def test_the_usage_history_outlives_the_post_it_was_written_for(client, scanned):
    """`IMG-13`, and the link the whole no-download re-match hangs on.

    Delete a post locally, adopt it again by id, run find images: the match comes
    out of the history without a network call. That works only because deleting
    the post sets `image_usage.post_id` to NULL instead of cascading the row
    away. One `ON DELETE CASCADE` added later would take the duplicate history
    and the re-match with it, and every other test would stay green.
    """
    local = image_store.get(scanned[0])
    post_id = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    usage.record(
        sha256=local["sha256"],
        pixel_sha256=local.get("pixel_sha256"),
        phash=local.get("phash"),
        post_id=post_id,
        remote_post_id=900,
        remote_image_id=555,
        source_path=local["absolute_path"],
        status=usage.PUSHED,
    )

    client.delete(f"/api/posts/{post_id}")

    rows = db.get_connection().execute("SELECT * FROM image_usage").fetchall()
    assert len(rows) == 1, "the record that those bytes were published survives"
    assert rows[0]["post_id"] is None, "only the link to the gone post is cleared"
    assert rows[0]["sha256"] == local["sha256"]
    assert rows[0]["remote_image_id"] == 555

    adopted = _remote_only_post(image_id_remote=555)
    result = matching.resolve(adopted, download=False)
    assert (result["matched"], result["from_history"], result["downloaded"]) == (1, 1, 0)
    assert post_store.images(adopted)[0]["image_id"] == local["id"]
