"""Duplicate detection.

Two questions with two different answers: "are these the same bytes" and "is this
the same picture". The second is what catches a re-export, and it is the reason a
byte hash alone is not enough.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from backend import db, jobs, watchers
from backend.civitai.errors import CivitaiError, NotFound, RateLimited
from backend.hashing import dhash64, file_sha256, hamming, pixel_sha256
from backend.posts import sync
from backend.store import posts as post_store
from backend.store import usage


def _rescan():
    from backend import jobs
    from backend.scanner import service

    return service.scan_roots(jobs.Job(id=0, kind="scan"))


@pytest.fixture
def run_post_check(monkeypatch):
    """Run the watch job inline so its rows and log are settled for assertions."""
    started: list[jobs.Job] = []

    def start(kind, target, *, exclusive=None):
        job = jobs.Job(
            id=9000 + len(started),
            kind=kind,
            exclusive=exclusive,
            status="running",
        )
        started.append(job)
        target(job)
        job.status = "done"
        return job

    monkeypatch.setattr(jobs, "start", start)
    watchers.reset_state()

    def run(now=lambda: 1000.0):
        result = watchers.check_due_posts(now=now)
        return result, started[-1] if started else None

    yield run
    watchers.reset_state()


def test_identical_bytes_share_both_hashes(fixture_images):
    _, images = fixture_images
    source = images[0]
    copy = source.parent / "copy.png"
    copy.write_bytes(source.read_bytes())

    assert file_sha256(copy) == file_sha256(source)
    assert dhash64(copy) == dhash64(source)


def test_a_re_encode_changes_the_byte_hash_but_not_the_picture(fixture_images, tmp_path):
    """The case a byte hash misses entirely: same image, different file."""
    _, images = fixture_images
    source = images[0]
    reencoded = tmp_path / "reencoded.jpg"
    with Image.open(source) as image:
        image.convert("RGB").save(reencoded, "JPEG", quality=70)

    assert file_sha256(reencoded) != file_sha256(source)
    assert hamming(dhash64(reencoded), dhash64(source)) <= 6


def test_a_resize_is_still_the_same_picture(fixture_images, tmp_path):
    _, images = fixture_images
    source = images[0]
    smaller = tmp_path / "small.png"
    with Image.open(source) as image:
        image.resize((image.width // 2, image.height // 2)).save(smaller)

    assert hamming(dhash64(smaller), dhash64(source)) <= 6


def test_different_pictures_are_far_apart(fixture_images):
    _, images = fixture_images
    if len(images) < 2:
        return
    assert hamming(dhash64(images[0]), dhash64(images[1])) > 6


def test_an_unreadable_file_yields_no_hash_instead_of_raising(tmp_path):
    """One broken file must never abort a scan."""
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    assert pixel_sha256(broken) is None
    assert dhash64(broken) is None


@pytest.mark.parametrize(
    ("suffix", "image_format", "save_options"),
    [
        (".png", "PNG", {}),
        (".jpg", "JPEG", {"quality": 81}),
        (".webp", "WEBP", {"quality": 81}),
    ],
)
def test_a_shared_decode_preserves_both_hashes(
    tmp_path, suffix, image_format, save_options
):
    path = tmp_path / f"shared{suffix}"
    image = Image.new("RGB", (83, 61))
    for y in range(image.height):
        for x in range(image.width):
            image.putpixel(
                (x, y),
                ((x * 7 + y) % 256, (y * 9 + x) % 256, x * y % 256),
            )
    image.save(path, image_format, **save_options)

    expected_pixels = pixel_sha256(path)
    expected_dhash = dhash64(path)
    with Image.open(path) as shared:
        actual_pixels = pixel_sha256(path, image=shared)
        actual_dhash = dhash64(path, image=shared)

    assert actual_pixels == expected_pixels
    assert actual_dhash == expected_dhash


def test_hamming_treats_a_missing_hash_as_maximally_distant():
    assert hamming(None, "0000000000000000") == 64
    assert hamming("zzz", "0000000000000000") == 64


def test_usage_survives_the_post_being_deleted(client, scanned):
    """The dedup memory is the point: deleting the plan must not erase the fact
    that these bytes were published."""
    post = client.post("/api/posts", json={"image_ids": scanned[:1]}).json()
    images = client.get(f"/api/posts/{post['id']}").json()["images"]
    sha = images[0]["sha256"]

    usage.record(
        sha256=sha,
        phash=images[0].get("phash"),
        post_id=post["id"],
        remote_post_id=999001,
        post_title="Old post",
        status=usage.PUSHED,
    )
    assert usage.check(sha, None)["has_exact"]

    client.delete(f"/api/posts/{post['id']}")

    result = usage.check(sha, None)
    assert result["has_exact"], "the history has to outlive the post"
    assert result["exact"][0]["remote_post_id"] == 999001


def test_a_withdrawn_post_stops_warning(client, scanned):
    """After the post was taken down the images are free again - but the record
    of what happened stays."""
    post = client.post("/api/posts", json={"image_ids": scanned[:1]}).json()
    images = client.get(f"/api/posts/{post['id']}").json()["images"]
    sha = images[0]["sha256"]

    usage.record(sha256=sha, phash=None, post_id=post["id"], status=usage.PUSHED)
    assert usage.check(sha, None)["has_exact"]

    usage.set_status_for_post(post["id"], usage.WITHDRAWN)
    assert not usage.check(sha, None)["has_exact"]


def test_missing_orphaned_posts_release_push_and_adopted_usage(
    client, scanned, monkeypatch, run_post_check
):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")

    pushed = client.post("/api/posts", json={"image_ids": scanned[:2]}).json()
    pushed_images = client.get(f"/api/posts/{pushed['id']}").json()["images"]
    post_store.set_fields(pushed["id"], remote_post_id=8101)
    for image in pushed_images:
        usage.record(
            sha256=image["sha256"],
            phash=image.get("phash"),
            post_id=pushed["id"],
            remote_post_id=8101,
            status=usage.PUSHED,
        )
    client.delete(f"/api/posts/{pushed['id']}")

    adopted = post_store.create(title="Adopted", origin="imported")
    post_store.set_fields(adopted, remote_post_id=8102)
    adopted_sha = "f" * 64
    usage.record(
        sha256=adopted_sha,
        phash=None,
        post_id=adopted,
        remote_post_id=8102,
        status=usage.PUBLISHED,
        origin="fetch",
    )
    post_store.delete(adopted)

    calls: list[int] = []

    def missing(remote_post_id: int):
        calls.append(remote_post_id)
        raise NotFound(f"Post {remote_post_id} was not found")

    monkeypatch.setattr(sync, "_fetch", missing)
    result, job = run_post_check()

    rows = [
        dict(row)
        for row in db.get_connection().execute(
            "SELECT * FROM image_usage ORDER BY remote_post_id, id"
        )
    ]
    assert result["checked"] == 2
    assert calls == [8101, 8102], "one read per post, not one per image"
    assert job.result["released"] == 3
    assert len(rows) == 3, "withdrawing retains the append-only history"
    assert all(row["post_id"] is None for row in rows)
    assert all(row["status"] == usage.WITHDRAWN for row in rows)
    adopted_row = next(row for row in rows if row["remote_post_id"] == 8102)
    assert adopted_row["origin"] == "fetch"
    assert not usage.check(adopted_sha, None)["has_exact"]
    assert all(not usage.check(image["sha256"], None)["has_exact"] for image in pushed_images)


def test_orphaned_usage_stays_held_on_plain_and_rate_limit_errors(
    monkeypatch, run_post_check
):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    failures = {
        8201: CivitaiError("server failed", status=500),
        8202: RateLimited("request limit reached", retry_after=60),
    }
    for remote_post_id in failures:
        usage.record(
            sha256=f"{remote_post_id:064x}",
            phash=None,
            post_id=None,
            remote_post_id=remote_post_id,
            status=usage.PUSHED,
        )

    def unavailable(remote_post_id: int):
        raise failures[remote_post_id]

    monkeypatch.setattr(sync, "_fetch", unavailable)
    _, job = run_post_check()

    rows = db.get_connection().execute(
        "SELECT sha256, status FROM image_usage ORDER BY remote_post_id"
    ).fetchall()
    assert [row["status"] for row in rows] == [usage.PUSHED, usage.PUSHED]
    assert all(usage.check(row["sha256"], None)["has_exact"] for row in rows)
    assert job.failed == 2
    assert job.result["released"] == 0


def test_existing_orphan_stays_held_and_local_usage_is_not_checked(
    monkeypatch, run_post_check
):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    orphan_sha = "a" * 64
    usage.record(
        sha256=orphan_sha,
        phash=None,
        post_id=None,
        remote_post_id=8301,
        status=usage.PUSHED,
    )
    local_post = post_store.create(title="Still local")
    post_store.set_fields(local_post, remote_post_id=8302)
    local_sha = "b" * 64
    usage.record(
        sha256=local_sha,
        phash=None,
        post_id=local_post,
        remote_post_id=8302,
        status=usage.PUBLISHED,
    )
    calls: list[int] = []

    def present(remote_post_id: int):
        calls.append(remote_post_id)
        return {"id": remote_post_id}

    monkeypatch.setattr(sync, "_fetch", present)
    result, job = run_post_check()

    assert result["checked"] == 1
    assert calls == [8301]
    assert job.result["released"] == 0
    assert usage.check(orphan_sha, None)["has_exact"]
    assert usage.check(local_sha, None)["has_exact"]


def test_orphaned_usage_check_is_off_with_the_post_setting(
    monkeypatch, run_post_check
):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "0")
    usage.record(
        sha256="c" * 64,
        phash=None,
        post_id=None,
        remote_post_id=8401,
        status=usage.PUSHED,
    )
    monkeypatch.setattr(
        sync,
        "_fetch",
        lambda remote_post_id: pytest.fail("the disabled check performed a remote read"),
    )

    result, job = run_post_check()

    assert result == {"checked": 0, "skipped": "off"}
    assert job is None
    assert usage.check("c" * 64, None)["has_exact"]


def test_every_active_orphan_is_rechecked_within_bounded_ticks(
    monkeypatch, run_post_check
):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    active_orphans = set(
        range(8500, 8500 + watchers.POST_CHECK_BATCH_SIZE + 2)
    )
    for remote_post_id in active_orphans:
        usage.record(
            sha256=f"{remote_post_id:064x}",
            phash=None,
            post_id=None,
            remote_post_id=remote_post_id,
            status=usage.PUSHED,
        )
    calls: list[int] = []

    def unavailable(remote_post_id: int):
        calls.append(remote_post_id)
        raise CivitaiError("try later")

    monkeypatch.setattr(sync, "_fetch", unavailable)
    moment = [1000.0]
    initial_ticks = (
        len(active_orphans) + watchers.POST_CHECK_BATCH_SIZE - 1
    ) // watchers.POST_CHECK_BATCH_SIZE
    for _ in range(initial_ticks):
        result, _ = run_post_check(now=lambda: moment[0])
        assert result["checked"] <= watchers.POST_CHECK_BATCH_SIZE
        moment[0] += watchers.POST_CHECK_SECONDS
    assert set(calls) == active_orphans

    calls.clear()
    moment[0] += watchers.ORPHAN_REFRESH_SECONDS
    bounded_ticks = initial_ticks
    next_remote_post_id = 9000
    for _ in range(bounded_ticks):
        for remote_post_id in range(
            next_remote_post_id,
            next_remote_post_id + watchers.POST_CHECK_BATCH_SIZE,
        ):
            usage.record(
                sha256=f"{remote_post_id:064x}",
                phash=None,
                post_id=None,
                remote_post_id=remote_post_id,
                status=usage.PUSHED,
            )
        next_remote_post_id += watchers.POST_CHECK_BATCH_SIZE
        result, _ = run_post_check(now=lambda: moment[0])
        assert result["checked"] <= watchers.POST_CHECK_BATCH_SIZE
        moment[0] += watchers.POST_CHECK_SECONDS

    asked_active_orphans = set(calls) & active_orphans
    assert asked_active_orphans == active_orphans


def test_a_post_does_not_warn_against_itself(client, scanned):
    post = client.post("/api/posts", json={"image_ids": scanned[:1]}).json()
    detail = client.get(f"/api/posts/{post['id']}").json()
    sha = detail["images"][0]["sha256"]
    usage.record(sha256=sha, phash=None, post_id=post["id"], status=usage.PUSHED)

    assert not usage.check(sha, None, exclude_post_id=post["id"])["has_exact"]


def test_the_banded_lookup_finds_what_the_single_lookup_finds():
    """The bulk path uses indexed bands, the single path compares everything.
    They must agree, or the library grid would show different warnings than the
    post editor."""
    from backend.store import usage as usage_store

    usage_store.record(sha256="a" * 64, phash="1f98995b66dfb678", post_id=None, post_title="Alt")

    one_bit_off = {"sha256": "b" * 64, "phash": "1f98995b66dfb679"}
    far_away = {"sha256": "c" * 64, "phash": "ffffffffffffffff"}

    bulk = usage_store.check_many([one_bit_off, far_away])
    for item in (one_bit_off, far_away):
        single = usage_store.check(item["sha256"], item["phash"])
        assert bulk[item["sha256"]]["has_similar"] == single["has_similar"]
        assert bulk[item["sha256"]]["has_exact"] == single["has_exact"]

    assert bulk[one_bit_off["sha256"]]["has_similar"] is True
    assert bulk[one_bit_off["sha256"]]["similar"][0]["distance"] == 1
    assert bulk[far_away["sha256"]]["has_similar"] is False


def test_bands_are_written_for_every_recorded_use():
    """Without them the indexed lookup silently finds nothing."""
    from backend import db
    from backend.store import usage as usage_store

    usage_store.record(sha256="d" * 64, phash="0123456789abcdef", post_id=None)
    row = db.get_connection().execute(
        "SELECT * FROM image_usage WHERE sha256=?", ("d" * 64,)
    ).fetchone()
    assert row["phash_b0"] == 0xEF
    assert row["phash_b7"] == 0x01


def test_a_use_without_a_phash_is_still_recorded():
    """Videos and unreadable files have no perceptual hash; the exact match must
    still work for them."""
    from backend.store import usage as usage_store

    usage_store.record(sha256="e" * 64, phash=None, post_id=None)
    assert usage_store.check("e" * 64, None)["has_exact"]
    assert usage_store.check_many([{"sha256": "e" * 64}])["e" * 64]["has_exact"]


# --- files removed outside the application -----------------------------------


def test_a_vanished_unheld_image_leaves_no_library_row_or_usage_change(client, scanned):
    from backend import db
    from backend.store import images as image_store

    image = image_store.get(scanned[0])
    usage.record(
        sha256=image["sha256"],
        pixel_sha256=image["pixel_sha256"],
        phash=image["phash"],
        post_id=None,
        remote_post_id=4242,
        source_path=image["absolute_path"],
        status=usage.PUBLISHED,
    )
    before_usage = [
        tuple(row) for row in db.get_connection().execute("SELECT * FROM image_usage")
    ]

    Path(image["absolute_path"]).unlink()
    result = _rescan()

    assert result["missing"] == 1
    assert image_store.get(image["id"]) is None
    assert [
        tuple(row) for row in db.get_connection().execute("SELECT * FROM image_usage")
    ] == before_usage


def test_a_post_held_vanished_image_stays_visible_and_preflight_reports_it(
    client, scanned
):
    from backend.store import images as image_store

    image = image_store.get(scanned[0])
    post = client.post("/api/posts", json={"image_ids": [image["id"]]}).json()

    Path(image["absolute_path"]).unlink()
    result = _rescan()

    assert result["missing"] == 1
    assert image_store.get(image["id"])["is_missing"] is True

    # "unused" means available since task 84, and a planned image is not - but a
    # *missing* one stays listed under the default filter, because that
    # placeholder is the only way the maintainer learns the file is gone.
    library = client.get("/api/images", params={"include_missing": True}).json()
    missing = next(item for item in library["items"] if item["id"] == image["id"])
    assert missing["is_missing"] is True
    assert missing["planned"] is True

    response = client.get(f"/api/posts/{post['id']}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["images"][0]["image_id"] == image["id"]
    assert detail["images"][0]["is_missing"] is True
    assert "file_missing" in {issue["code"] for issue in detail["checklist"]["issues"]}
    thumbnail = client.get(f"/api/images/{image['id']}/thumbnail")
    assert thumbnail.status_code == 404
    assert thumbnail.json()["detail"]["code"] == "source_file_not_found"


def test_a_file_removed_from_reachable_trash_loses_only_its_image_row(
    client, scanned, fixture_images
):
    from backend import db, duplicates
    from backend.store import images as image_store

    folder, _ = fixture_images
    trash = folder / "trash"
    client.put("/api/settings", json={"trash_folder": str(trash)})
    duplicates.move_to_trash([scanned[0]])
    image = image_store.get(scanned[0])
    usage.record(
        sha256=image["sha256"],
        phash=image["phash"],
        post_id=None,
        remote_post_id=4343,
        status=usage.PUBLISHED,
    )
    before_usage = [
        tuple(row) for row in db.get_connection().execute("SELECT * FROM image_usage")
    ]

    Path(image["absolute_path"]).unlink()
    _rescan()

    assert image_store.get(image["id"]) is None
    assert [
        tuple(row) for row in db.get_connection().execute("SELECT * FROM image_usage")
    ] == before_usage


def test_an_image_removed_from_the_archive_is_not_reconciled(fixture_images):
    from backend.store import images as image_store
    from backend.store import sources as source_store

    folder, paths = fixture_images
    source_store.add_root(str(folder), label="Archive", is_archive=True)
    _rescan()
    image = image_store.get_by_path(str(paths[0]))

    paths[0].unlink()
    result = _rescan()

    assert result["missing"] == 0
    assert image_store.get(image["id"])["is_missing"] is False


def test_an_existing_but_unreadable_image_is_not_treated_as_vanished(scanned):
    from backend.store import images as image_store

    image = image_store.get(scanned[0])
    Path(image["absolute_path"]).write_bytes(b"not an image")

    _rescan()

    assert image_store.get(image["id"])["is_missing"] is False


# --- the confirmation ladder --------------------------------------------------


def _fingerprint(path):
    """File, pixel and perceptual hashes plus generation comparison details."""
    from backend.metadata import infotext, png_io
    from backend.store import usage as usage_store

    parsed = infotext.parse((png_io.read(path) or object()).infotext)
    fields = parsed.get("fields") or {}
    return {
        "sha256": file_sha256(path),
        "pixel_sha256": pixel_sha256(path),
        "phash": dhash64(path),
        "seed": str(fields.get("Seed")) if fields.get("Seed") is not None else None,
        "prompt_hash": usage_store.prompt_fingerprint(parsed.get("prompt")),
    }


def test_editing_metadata_changes_the_bytes_but_not_the_picture(fixture_images, tmp_path):
    """The question this ladder exists for. Editing an image's metadata rewrites
    the file, so sha256 diverges - but it is obviously still the same picture."""
    from PIL import Image, PngImagePlugin

    _, images = fixture_images
    source = images[0]
    original = _fingerprint(source)
    assert original["seed"], "the fixture carries metadata"

    # rewrite with a changed description, prompt and seed untouched
    edited = tmp_path / "edited.png"
    with Image.open(source) as image:
        info = PngImagePlugin.PngInfo()
        for key, value in (getattr(image, "text", {}) or {}).items():
            info.add_text(key, value)
        info.add_text("Comment", "added afterwards")
        image.save(edited, pnginfo=info)

    changed = _fingerprint(edited)
    assert changed["sha256"] != original["sha256"], "the bytes are different"
    assert changed["pixel_sha256"] == original["pixel_sha256"]
    assert changed["phash"] == original["phash"], "the picture is the same"
    assert changed["seed"] == original["seed"]
    assert changed["prompt_hash"] == original["prompt_hash"]


@pytest.mark.parametrize(
    ("distance", "seed"),
    [(5, "3324970406"), (6, "458558052"), (4, "1779548050")],
)
def test_reported_matching_generations_remain_probable_in_usage_history(
    fixture_images, distance, seed
):
    from backend.store import usage as usage_store

    _, images = fixture_images
    candidate = _fingerprint(images[0])
    history_phash = f"{int(candidate['phash'], 16) ^ ((1 << distance) - 1):016x}"
    prompt_hash = usage_store.prompt_fingerprint(
        f"reported multi-image generation {seed}"
    )
    usage_store.record(
        sha256=f"history-{seed}",
        pixel_sha256=f"history-pixels-{seed}",
        phash=history_phash,
        post_id=None,
        remote_post_id=4711,
        post_title="Posted earlier",
        status=usage_store.PUBLISHED,
        origin="backfill",
        seed=seed,
        prompt_hash=prompt_hash,
    )

    result = usage_store.check(
        candidate["sha256"],
        candidate["phash"],
        pixel_sha256=candidate["pixel_sha256"],
    )
    assert not result["has_exact"], "the bytes do not match"
    assert result["has_similar"]
    assert not result["has_certain"]
    assert result["confirmed"] == []
    assert result["similar"][0]["confidence"] == usage_store.PROBABLE
    assert result["similar"][0]["distance"] == distance


def test_without_generation_data_a_lookalike_stays_a_hint(fixture_images):
    """A series of near-identical portraits must not warn against itself."""
    from backend.store import usage as usage_store

    _, images = fixture_images
    original = _fingerprint(images[0])

    usage_store.record(
        sha256="f" * 64,
        phash=original["phash"],
        post_id=None,
        remote_post_id=4712,
        status=usage_store.PUBLISHED,
        origin="backfill",
        seed=None,
        prompt_hash=None,
    )

    result = usage_store.check(original["sha256"], original["phash"])
    assert result["has_similar"]
    assert not result["has_certain"], "a perceptual candidate stays a guess"
    assert result["similar"][0]["confidence"] == usage_store.PROBABLE


def test_a_different_seed_keeps_it_a_hint_even_at_distance_zero(fixture_images):
    """Same-looking but demonstrably a different generation."""
    from backend.store import usage as usage_store

    _, images = fixture_images
    original = _fingerprint(images[0])

    usage_store.record(
        sha256="e" * 64,
        phash=original["phash"],
        post_id=None,
        status=usage_store.PUBLISHED,
        seed="999999",
        prompt_hash=original["prompt_hash"],
    )
    result = usage_store.check(original["sha256"], original["phash"])
    assert not result["has_certain"]
