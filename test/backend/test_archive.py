"""Moving images from explicitly archived posts into the archive folder.

This moves real files and cannot be undone. The tests therefore check above all
what it does *not* do: overwrite nothing, discard nothing, take nothing along that
merely looks similar.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from backend import archive, db, duplicates, jobs
from backend.api import posts as posts_api
from backend.posts import lifecycle, push
from backend.store import images as image_store
from backend.store import posts as post_store
from backend.store import runs as run_store
from backend.store import sources as source_store
from backend.store import usage


def _record_published_usage(
    image_id: int, *, post_id: int, remote_image_id: int | None = 555
) -> None:
    """Record a local image as already published, the way a push would."""
    image = image_store.get(image_id)
    post = post_store.get_by_remote(post_id)
    if post is None:
        local_post_id = post_store.create(title=f"Post {post_id}", state=lifecycle.PUBLISHED)
        post_store.set_fields(local_post_id, remote_post_id=post_id)
    else:
        local_post_id = post["id"]
    usage.record(
        sha256=image["sha256"],
        phash=image["phash"],
        post_id=local_post_id,
        remote_post_id=post_id,
        remote_image_id=remote_image_id,
        source_path=image["absolute_path"],
        post_title=f"Post {post_id}",
        status=usage.PUBLISHED,
    )


def _publish_archived(
    image_id: int, *, post_id: int, remote_image_id: int | None = 555
) -> None:
    """Record the publication, then explicitly mark its post for archiving."""
    _record_published_usage(image_id, post_id=post_id, remote_image_id=remote_image_id)
    post = post_store.get_by_remote(post_id)
    assert post is not None
    if post["state"] != lifecycle.ARCHIVED:
        lifecycle.transition(post["id"], lifecycle.ARCHIVED, reason="test archives post")


def test_the_plan_asks_the_history_once_for_the_whole_library(
    client, scanned, tmp_path, monkeypatch
):
    """One bulk lookup, no perceptual stage, no per-image scan.

    The per-image variant computed a perceptual distance for every image in the
    library and threw it away - 1.6 million comparisons and 40 seconds on a
    3,800-image library, for a plan that only accepts identical bytes or pixels.
    """
    _archive_dir(tmp_path)
    _publish_archived(scanned[0], post_id=4242)

    batches: list[int] = []
    real = usage.check_many

    def counting(items, **options):
        batches.append(len(items))
        return real(items, **options)

    monkeypatch.setattr(archive.usage, "check_many", counting)
    monkeypatch.setattr(
        archive.usage,
        "check",
        lambda *args, **kwargs: pytest.fail("the per-image history scan is back"),
    )

    items = archive.plan()

    assert [item["image_id"] for item in items] == [scanned[0]]
    assert len(batches) == 1, "one lookup for the whole library, not one per image"


def _archive_dir(tmp_path) -> Path:
    target = tmp_path / "archiv"
    target.mkdir(exist_ok=True)
    source_store.add_root(str(target), label="Archiv", is_archive=True)
    return target


def _post(remote_post_id: int, *, published_at: str | None) -> int:
    post_id = post_store.create(title=f"Post {remote_post_id}", state=lifecycle.PUBLISHED)
    post_store.set_fields(
        post_id,
        remote_post_id=remote_post_id,
        remote_published_at=published_at,
    )
    return post_id


def _archived_post(
    remote_post_id: int | None,
    *,
    published_at: str | None = None,
    published_event_at: str | None = None,
    title: str = "",
) -> int:
    post_id = post_store.create(title=title)
    fields = {"state": lifecycle.ARCHIVED}
    if remote_post_id is not None:
        fields["remote_post_id"] = remote_post_id
    if published_at is not None:
        fields["remote_published_at"] = published_at
    post_store.set_fields(post_id, **fields)
    if published_event_at is not None:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO post_events(post_id, remote_post_id, event, at)"
                " VALUES(?, ?, 'published', ?)",
                (post_id, remote_post_id, published_event_at),
            )
    return post_id


def _run(image_ids=None) -> dict:
    job = jobs.Job(id=0, kind="archive")
    return archive.run(job, image_ids)


def test_an_archived_image_moves_into_a_folder_named_after_the_post(client, scanned, tmp_path):
    target = _archive_dir(tmp_path)
    _post(30497397, published_at="2026-08-24T21:15:00Z")
    _publish_archived(scanned[0], post_id=30497397, remote_image_id=140252440)
    original = Path(image_store.get(scanned[0])["absolute_path"])

    assert archive.plan()[0]["how"] == "identical bytes"
    result = _run()

    assert result["moved"] == 1
    assert not original.exists(), "moved, not copied"
    moved = target / "2026-08-24__30497397" / f"{original.stem}_140252440{original.suffix}"
    assert moved.exists(), "the name stays, the image id is appended"
    archived_post = client.get("/api/posts/archive").json()["items"][0]
    assert archived_post["archive_folder"] == str(target / "2026-08-24__30497397")
    assert archived_post["archive_folder_rename"] is None


def test_archive_preview_names_an_exact_pixel_match(client, scanned, tmp_path):
    _archive_dir(tmp_path)
    image = image_store.get(scanned[0])
    post_id = post_store.create(title="Post 71", state=lifecycle.PUBLISHED)
    post_store.set_fields(post_id, remote_post_id=71)
    usage.record(
        sha256="different-container",
        pixel_sha256=image["pixel_sha256"],
        phash=image["phash"],
        post_id=post_id,
        remote_post_id=71,
        remote_image_id=710,
        status=usage.PUBLISHED,
    )
    lifecycle.transition(post_id, lifecycle.ARCHIVED, reason="user archived post")

    item = archive.plan()[0]

    assert item["how"] == "identical pixels"
    assert Path(item["source_path"]).exists(), "the preview moves nothing"


def test_a_scheduled_post_attached_by_the_push_path_never_enters_the_plan(
    client, scanned, tmp_path, monkeypatch
):
    _archive_dir(tmp_path)
    post_id = post_store.create(title="Still scheduled", state=lifecycle.PUSHING)
    post_store.set_images(post_id, [scanned[0]])
    post_store.set_fields(post_id, remote_post_id=7001)
    image = post_store.images(post_id)[0]
    post_store.record_upload(
        image["id"],
        uuid="scheduled-upload",
        width=image["width"],
        height=image["height"],
    )
    run_id = run_store.create_run("push", [post_id])
    monkeypatch.setattr(
        push.trpc,
        "post_add_image",
        lambda remote_post_id, payload: {"id": 8001, "url": payload["url"]},
    )

    push._attach_images(
        run_id,
        post_id,
        post_store.get(post_id),
        post_store.images(post_id),
        job=None,
    )
    post_store.set_fields(
        post_id,
        state=lifecycle.SCHEDULED,
        remote_published_at="2099-08-31T12:00:00Z",
    )

    pushed = db.get_connection().execute(
        "SELECT status, post_id, remote_post_id FROM image_usage"
    ).fetchone()
    assert dict(pushed) == {
        "status": usage.PUSHED,
        "post_id": post_id,
        "remote_post_id": 7001,
    }
    assert archive.plan() == []
    assert Path(image["source_path"]).exists()


def test_a_backfill_usage_without_a_local_post_never_enters_the_plan(
    client, scanned, tmp_path
):
    _archive_dir(tmp_path)
    image = image_store.get(scanned[0])
    usage.record(
        sha256=image["sha256"],
        pixel_sha256=image["pixel_sha256"],
        phash=image["phash"],
        post_id=None,
        remote_post_id=7002,
        remote_image_id=8002,
        source_path=image["absolute_path"],
        status=usage.PUBLISHED,
        origin="backfill",
    )

    assert archive.plan() == []
    assert Path(image["absolute_path"]).exists()


def test_a_published_event_supplies_the_folder_date(client, scanned, tmp_path):
    target = _archive_dir(tmp_path)
    _publish_archived(scanned[0], post_id=30, remote_image_id=300)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO post_events(remote_post_id, event, at) VALUES(?, 'published', ?)",
            (30, "2026-07-03T23:59:00Z"),
        )

    assert _run()["moved"] == 1
    assert (target / "2026-07-03__30").is_dir()


def test_archive_listing_sorts_and_filters_before_pagination(client):
    older = _archived_post(101, published_at="2026-08-20T08:00:00Z")
    unknown_remote = _archived_post(106)
    newest_event = _archived_post(
        102,
        published_at="not-a-date",
        published_event_at="2026-08-24T09:30:00Z",
    )
    equal_low = _archived_post(204, published_at="2026-08-21T10:00:00Z")
    middle = _archived_post(103, published_at="2026-08-22T12:00:00Z")
    equal_high = _archived_post(205, published_at="2026-08-21T10:00:00Z")
    unknown_local = _archived_post(None)

    pages = [
        client.get("/api/posts/archive", params={"limit": 3, "offset": offset}).json()
        for offset in (0, 3, 6)
    ]
    ordered = [row for page in pages for row in page["items"]]

    assert [page["total"] for page in pages] == [7, 7, 7]
    assert [row["id"] for row in ordered] == [
        newest_event,
        middle,
        equal_high,
        equal_low,
        older,
        unknown_local,
        unknown_remote,
    ]
    assert pages[0]["items"][-1]["archive_date"] >= pages[1]["items"][0]["archive_date"]
    assert ordered[0]["archive_published_at"] == "2026-08-24T09:30:00Z"
    assert [row["archive_date"] for row in ordered[-2:]] == [None, None]
    assert all(
        row["archive_date"] == archive.publication_date(row["remote_post_id"])
        for row in ordered
        if row["remote_post_id"] is not None
    )

    from_only = client.get(
        "/api/posts/archive", params={"from_date": "2026-08-22"}
    ).json()
    assert from_only["total"] == 2
    assert [row["id"] for row in from_only["items"]] == [newest_event, middle]

    to_only = client.get(
        "/api/posts/archive", params={"to_date": "2026-08-21"}
    ).json()
    assert to_only["total"] == 3
    assert [row["id"] for row in to_only["items"]] == [equal_high, equal_low, older]

    inclusive = client.get(
        "/api/posts/archive",
        params={"from_date": "2026-08-21", "to_date": "2026-08-22", "limit": 1},
    ).json()
    assert inclusive["total"] == 3
    assert [row["id"] for row in inclusive["items"]] == [middle]


@pytest.mark.parametrize(
    "params",
    [
        {"from_date": "2026-8-01"},
        {"to_date": "2026-02-30"},
        {"from_date": "2026-08-22", "to_date": "2026-08-21"},
    ],
)
def test_archive_listing_rejects_invalid_date_ranges(client, params):
    _archived_post(201, published_at="2026-08-21T10:00:00Z")

    response = client.get("/api/posts/archive", params=params)

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_archive_date_range"
    assert client.get("/api/posts/archive").json()["total"] == 1


def test_archive_listing_returns_five_ordered_non_video_previews(client, scanned):
    assert len(scanned) >= 2
    post_id = _archived_post(700, published_at="2026-08-23T10:00:00Z")
    post_store.set_images(post_id, scanned[:2])
    with db.transaction() as conn:
        conn.execute(
            "UPDATE post_images SET remote_url=? WHERE post_id=? AND position=0",
            ("https://example.invalid/local-fallback.jpg", post_id),
        )
        for position, content_type, media_type, remote_url in (
            (2, "video/mp4", "video", "https://example.invalid/video.mp4"),
            (3, "image/jpeg", "image", "https://example.invalid/remote-3.jpg"),
            (4, "image/png", "image", None),
            (5, "image/jpeg", "image", "https://example.invalid/remote-5.jpg"),
            (6, "image/jpeg", "image", "https://example.invalid/remote-6.jpg"),
        ):
            conn.execute(
                """
                INSERT INTO post_images(
                    post_id, image_id, position, source_path, sha256,
                    content_type, media_type, remote_url
                ) VALUES(?, NULL, ?, '', '', ?, ?, ?)
                """,
                (post_id, position, content_type, media_type, remote_url),
            )

    row = client.get("/api/posts/archive").json()["items"][0]

    assert row["image_count"] == 7
    assert [preview["position"] for preview in row["previews"]] == [0, 1, 3, 4, 5]
    assert row["previews"][0] == {
        "position": 0,
        "image_id": scanned[0],
        "thumbnail_path": image_store.get(scanned[0])["thumbnail_path"],
        "remote_url": "https://example.invalid/local-fallback.jpg",
    }
    assert row["previews"][2]["remote_url"] == "https://example.invalid/remote-3.jpg"
    assert row["previews"][3] == {
        "position": 4,
        "image_id": None,
        "thumbnail_path": None,
        "remote_url": None,
    }


def test_file_away_considers_every_published_post_beyond_the_first_page(
    client, monkeypatch
):
    published = [
        {
            "id": post_id,
            "remote_published_at": "2000-01-01T00:00:00Z",
            "scheduled_at": None,
        }
        for post_id in range(1, 502)
    ]
    pages = []
    transitioned = []

    def list_page(*, states, limit, offset):
        assert states == [lifecycle.PUBLISHED]
        pages.append((limit, offset))
        return published[offset : offset + limit]

    monkeypatch.setattr(posts_api.post_store, "list_posts", list_page)
    monkeypatch.setattr(
        posts_api.lifecycle,
        "transition",
        lambda post_id, state, *, reason: transitioned.append((post_id, state, reason)),
    )

    result = client.post("/api/posts/archive-published", params={"days": 30}).json()

    assert pages == [(500, 0), (500, 500)]
    assert "considered" not in result
    assert result["archived"] == 501
    assert [post_id for post_id, _, _ in transitioned] == list(range(1, 502))


def test_a_post_without_a_known_date_uses_its_id_alone(client, scanned, tmp_path):
    target = _archive_dir(tmp_path)
    _publish_archived(scanned[0], post_id=31, remote_image_id=310)

    assert _run()["moved"] == 1
    assert (target / "31").is_dir()


def test_every_file_sharing_the_stem_comes_along(client, scanned, tmp_path):
    """The prompt .txt is not the only thing a generator writes next to an image."""
    target = _archive_dir(tmp_path)
    original = Path(image_store.get(scanned[0])["absolute_path"])
    text = original.with_suffix(".txt")
    text.write_text("prompt notes")
    data = original.with_suffix(".json")
    data.write_text('{"seed": 1}')
    # The form where a tool appends rather than replaces the extension.
    appended = original.with_name(f"{original.name}.json")
    appended.write_text('{"workflow": true}')
    _publish_archived(scanned[0], post_id=42, remote_image_id=777)

    assert _run()["sidecars"] == 3
    assert not text.exists() and not data.exists() and not appended.exists()

    folder = target / "42"
    stem = f"{original.stem}_777"
    assert (folder / f"{stem}.txt").read_text() == "prompt notes"
    assert (folder / f"{stem}.json").read_text() == '{"seed": 1}'
    assert (folder / f"{stem}{original.suffix}.json").read_text() == '{"workflow": true}', (
        "the whole extension chain is kept"
    )


def test_another_image_with_the_same_stem_is_not_a_sidecar(client, scanned, tmp_path):
    """A .webp next to the .png is its own picture, not something to drag along."""
    _archive_dir(tmp_path)
    original = Path(image_store.get(scanned[0])["absolute_path"])
    twin = original.with_suffix(".webp")
    twin.write_bytes(b"not really a webp")
    _publish_archived(scanned[0], post_id=43, remote_image_id=778)

    assert _run()["sidecars"] == 0
    assert twin.exists(), "the other image stays where it is"


def test_the_usage_history_follows_the_move(client, scanned, tmp_path):
    """Otherwise the history keeps pointing at a path that no longer exists."""
    from backend import db

    target = _archive_dir(tmp_path)
    original = Path(image_store.get(scanned[0])["absolute_path"])
    _publish_archived(scanned[0], post_id=44, remote_image_id=779)

    _run()

    moved = target / "44" / f"{original.stem}_779{original.suffix}"
    paths = [
        row["source_path"]
        for row in db.get_connection().execute("SELECT source_path FROM image_usage")
    ]
    assert str(moved) in paths
    assert str(original) not in paths


def _duplicate_setup(tmp_path, twin_name: str):
    """The same picture twice in the source folders, under two names."""
    from backend.scanner import service

    target = _archive_dir(tmp_path)
    first = Path(image_store.get(1)["absolute_path"])
    twin = first.with_name(twin_name)
    shutil.copy2(first, twin)
    service.scan_roots(jobs.Job(id=0, kind="scan"))

    for row in image_store.search(limit=50)[0]:
        if row["absolute_path"] in (str(first), str(twin)):
            _publish_archived(row["id"], post_id=7, remote_image_id=999)
    return target


def test_two_files_of_the_same_shot_are_reported_as_duplicates(client, scanned, tmp_path):
    """With different filenames they do not collide in the target folder - which
    is exactly why they are reported as well, instead of going unnoticed."""
    target = _duplicate_setup(tmp_path, "kopie.png")

    job = jobs.Job(id=0, kind="archive")
    result = archive.run(job, None)

    assert result["moved"] == 2, "both are moved, nothing is discarded"
    assert result["duplicates"] == 1, "and reported as one duplicate"
    group = job.result["duplicate_groups"][0]
    assert group["remote_image_id"] == 999
    assert len(group["paths"]) == 2

    names = sorted(p.name for p in (target / "7").iterdir())
    assert names == ["00003-2003483145_139428400_999.png", "kopie_999.png"]
    assert all("_999" in name for name in names), "the same id makes it visible in the folder"


def test_an_actual_name_collision_keeps_both_files(client, scanned, tmp_path):
    """The same filename from two source folders - here the target name has to
    give way instead of overwriting the first file."""
    from backend.scanner import service

    target = _archive_dir(tmp_path)
    first = Path(image_store.get(scanned[0])["absolute_path"])

    second_root = tmp_path / "second-source"
    second_root.mkdir()
    shutil.copy2(first, second_root / first.name)
    source_store.add_root(str(second_root), label="Second")
    service.scan_roots(jobs.Job(id=0, kind="scan"))

    for row in image_store.search(limit=50)[0]:
        if Path(row["absolute_path"]).name == first.name:
            _publish_archived(row["id"], post_id=7, remote_image_id=999)

    result = _run()

    names = sorted(p.name for p in (target / "7").iterdir())
    assert result["collisions"] >= 1
    assert len(names) == 2, "beide bleiben erhalten"
    assert any(name.endswith("-1.png") for name in names), "the second one gives way to -1"


@pytest.mark.parametrize(
    ("distance", "seed"),
    [(5, "3324970406"), (6, "458558052"), (4, "1779548050")],
)
def test_reported_matching_generations_are_excluded_from_the_archive_plan(
    client, scanned, tmp_path, distance, seed
):
    """The old rule would have planned a real move for every reported shape."""
    _archive_dir(tmp_path)
    image = image_store.get(scanned[0])
    prompt = f"reported multi-image generation {seed}"
    with db.transaction() as conn:
        conn.execute(
            "UPDATE images SET parsed_json=? WHERE id=?",
            (json.dumps({"prompt": prompt, "fields": {"Seed": seed}}), image["id"]),
        )
    history_phash = f"{int(image['phash'], 16) ^ ((1 << distance) - 1):016x}"
    usage.record(
        sha256=f"history-{seed}",
        pixel_sha256=f"history-pixels-{seed}",
        phash=history_phash,
        post_id=None,
        remote_post_id=99,
        remote_image_id=990,
        status=usage.PUBLISHED,
        seed=seed,
        prompt_hash=usage.prompt_fingerprint(prompt),
    )

    assert archive.plan() == []
    assert _run()["moved"] == 0
    assert Path(image["absolute_path"]).exists()


def test_an_unpublished_image_is_left_alone(client, scanned, tmp_path):
    _archive_dir(tmp_path)
    assert _run()["moved"] == 0
    assert all(Path(image_store.get(i)["absolute_path"]).exists() for i in scanned)


def test_the_archive_is_never_a_source_for_itself(client, scanned, tmp_path):
    """Otherwise every run would reshuffle the files inside the archive."""
    target = _archive_dir(tmp_path)
    _publish_archived(scanned[0], post_id=5, remote_image_id=50)
    _run()

    from backend.scanner import service

    service.scan_roots(jobs.Job(id=0, kind="scan"))
    moved = next(
        row for row in image_store.search(limit=50)[0] if str(target) in row["absolute_path"]
    )
    _publish_archived(moved["id"], post_id=5, remote_image_id=50)

    assert _run()["moved"] == 0, "was im Archiv liegt, bleibt liegen"


def test_an_existing_legacy_folder_is_reused_without_renaming(client, scanned, tmp_path):
    target = _archive_dir(tmp_path)
    legacy = target / "post_id_51"
    legacy.mkdir()
    _post(51, published_at="2026-08-20T12:00:00Z")
    _publish_archived(scanned[0], post_id=51, remote_image_id=510)

    assert _run()["moved"] == 1
    assert any(legacy.iterdir())
    assert not (target / "2026-08-20__51").exists()


def test_a_legacy_folder_is_scanned_and_included_in_duplicate_search(
    client, scanned, tmp_path
):
    from backend.scanner import service

    target = _archive_dir(tmp_path)
    legacy = target / "post_id_52"
    legacy.mkdir()
    original = Path(image_store.get(scanned[0])["absolute_path"])
    first = legacy / "first.png"
    second = legacy / "second.png"
    shutil.copy2(original, first)
    shutil.copy2(original, second)

    service.scan_roots(jobs.Job(id=0, kind="scan"))

    assert image_store.get_by_path(str(first))
    assert image_store.get_by_path(str(second))
    expected = {str(first), str(second)}
    assert any(
        expected.issubset({member["absolute_path"] for member in group["members"]})
        for group in duplicates.exact_groups()
    )


def test_an_id_only_folder_is_renamed_only_by_the_explicit_action(
    client, scanned, tmp_path
):
    target = _archive_dir(tmp_path)
    post_id = _post(53, published_at=None)
    _publish_archived(scanned[0], post_id=53, remote_image_id=530)
    assert _run()["moved"] == 1
    undated = target / "53"
    assert undated.is_dir()
    archived_post = client.get("/api/posts/archive").json()["items"][0]
    assert archived_post["archive_folder_rename"] is None

    post_store.set_fields(post_id, remote_published_at="2026-08-21T01:00:00Z")
    archive.status()
    archive.plan()
    assert undated.is_dir(), "read-only checks never rename files"

    archived_post = client.get("/api/posts/archive").json()["items"][0]
    assert archived_post["archive_folder_rename"] == {
        "from": str(undated),
        "to": str(target / "2026-08-21__53"),
    }
    response = client.post(f"/api/posts/{post_id}/archive-folder/rename")
    assert response.status_code == 200
    result = response.json()

    dated = target / "2026-08-21__53"
    assert result["renamed"] is True
    assert dated.is_dir()
    assert not undated.exists()
    assert Path(image_store.get(scanned[0])["absolute_path"]).parent == dated
    usage_paths = {
        row["source_path"]
        for row in db.get_connection().execute(
            "SELECT source_path FROM image_usage WHERE remote_post_id=53"
        )
    }
    assert all(Path(path).parent == dated for path in usage_paths)
    assert client.get("/api/posts/archive").json()["items"][0]["archive_folder_rename"] is None


def test_an_explicit_folder_rename_never_merges_into_an_existing_target(
    client, scanned, tmp_path
):
    target = _archive_dir(tmp_path)
    post_id = _post(54, published_at=None)
    _publish_archived(scanned[0], post_id=54, remote_image_id=540)
    assert _run()["moved"] == 1
    post_store.set_fields(
        post_id,
        state=lifecycle.ARCHIVED,
        remote_published_at="2026-08-22T01:00:00Z",
    )
    dated = target / "2026-08-22__54"
    dated.mkdir()

    response = client.post(f"/api/posts/{post_id}/archive-folder/rename")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "archive_folder_target_exists",
        "message": (
            f"The target folder already exists: {dated}. "
            "Merge the folders manually before retrying."
        ),
        "params": {"path": str(dated)},
    }
    assert (target / "54").is_dir()
    assert not any(dated.iterdir())


def test_a_failed_folder_rename_puts_the_files_back(client, scanned, tmp_path):
    target = _archive_dir(tmp_path)
    post_id = _post(55, published_at=None)
    _publish_archived(scanned[0], post_id=55, remote_image_id=550)
    assert _run()["moved"] == 1
    undated = target / "55"
    original = Path(image_store.get(scanned[0])["absolute_path"])
    post_store.set_fields(post_id, remote_published_at="2026-08-23T01:00:00Z")
    with db.transaction() as conn:
        conn.execute(
            """
            CREATE TRIGGER refuse_archive_folder_rename
            BEFORE UPDATE OF absolute_path ON images
            BEGIN
                SELECT RAISE(ABORT, 'refused for test');
            END
            """
        )

    with pytest.raises(sqlite3.DatabaseError, match="refused for test"):
        archive.rename_undated_folder(55)

    assert undated.is_dir()
    assert not (target / "2026-08-23__55").exists()
    assert Path(image_store.get(scanned[0])["absolute_path"]) == original


def test_an_archive_inside_a_source_folder_is_refused(client, scanned, fixture_images):
    """Otherwise the run moves files within the tree it is walking."""
    folder, _ = fixture_images
    nested = folder / "archiv"
    nested.mkdir()
    source_store.add_root(str(nested), label="Archiv", is_archive=True)

    state = archive.status()
    assert state["blocked"], state
    # Raised rather than reported on the job: a run that never started must not
    # come back as "done", or the job bar renders a cheerful "0 moved".
    with pytest.raises(RuntimeError):
        archive.run(jobs.Job(id=0, kind="archive"), None)


def test_a_file_changed_since_the_preview_is_skipped(client, scanned, tmp_path):
    """The file can have been replaced between preview and run."""
    _archive_dir(tmp_path)
    _publish_archived(scanned[0], post_id=8, remote_image_id=80)
    items = archive.plan()
    assert items

    Path(items[0]["source_path"]).write_bytes(b"different bytes")
    job = jobs.Job(id=0, kind="archive")
    result = archive.run(job, None)

    assert result["moved"] == 0
    assert result["skipped"] == 1


def test_the_database_follows_the_file(client, scanned, tmp_path):
    """Without it the file would surface as a new image on the next scan."""
    target = _archive_dir(tmp_path)
    _publish_archived(scanned[0], post_id=11, remote_image_id=110)
    _run()

    row = image_store.get(scanned[0])
    assert str(target) in row["absolute_path"]
    assert row["is_missing"] is False
    assert Path(row["absolute_path"]).exists()


def test_without_an_archive_folder_nothing_happens(client, scanned):
    assert archive.status()["configured"] is False
    assert archive.plan() == []
    with pytest.raises(RuntimeError, match="archive folder"):
        archive.run(jobs.Job(id=0, kind="archive"), None)


def test_only_the_selected_images_move(client, scanned, tmp_path):
    _archive_dir(tmp_path)
    for image_id in scanned:
        _publish_archived(image_id, post_id=12, remote_image_id=120 + image_id)

    assert _run([scanned[0]])["moved"] == 1
    assert Path(image_store.get(scanned[1])["absolute_path"]).exists()


def test_only_a_post_marked_archived_is_due(client, scanned, tmp_path):
    _archive_dir(tmp_path)
    _record_published_usage(scanned[0], post_id=61, remote_image_id=610)
    post = post_store.get_by_remote(61)
    assert post is not None and post["state"] == lifecycle.PUBLISHED
    original = Path(image_store.get(scanned[0])["absolute_path"])

    assert archive.plan() == []
    assert archive.status()["candidates"] == 0
    assert archive.status()["posts"] == 0
    assert _run()["moved"] == 0
    assert original.exists(), "a published post was not selected for archiving"

    lifecycle.transition(post["id"], lifecycle.ARCHIVED, reason="user archived post")

    assert [item["image_id"] for item in archive.plan()] == [scanned[0]]
    assert archive.status()["candidates"] == 1
    assert archive.status()["posts"] == 1


def _adopted_root(tmp_path) -> Path:
    """The configured adopted folder, registered as a source root."""
    folder = tmp_path / "adoptiert"
    folder.mkdir(exist_ok=True)
    db.set_setting("adopted_folder", str(folder))
    source_store.add_root(str(folder), label="Adopted")
    return folder


def _fetched_post_folder(
    client, adopted: Path, fixture_images, remote_post_id: int, *, extra: str = ""
) -> tuple[Path, int]:
    """One adopted post's folder with its image, the way fetch_images leaves it."""
    from backend.scanner import service

    _, images = fixture_images
    folder = adopted / f"post_id_{remote_post_id}"
    folder.mkdir()
    shutil.copy2(images[0], folder / images[0].name)
    if extra:
        (folder / extra).write_text("not a sidecar of the image", encoding="utf-8")

    service.scan_roots(jobs.Job(id=0, kind="scan"))
    image_id = next(
        item["id"]
        for item in client.get("/api/images").json()["items"]
        if Path(item["absolute_path"]).parent == folder
    )
    return folder, image_id


def test_an_emptied_adopted_folder_is_removed_after_the_run(
    client, fixture_images, tmp_path
):
    _archive_dir(tmp_path)
    adopted = _adopted_root(tmp_path)
    folder, image_id = _fetched_post_folder(client, adopted, fixture_images, 30497397)
    _post(30497397, published_at="2026-08-24T21:15:00Z")
    _publish_archived(image_id, post_id=30497397, remote_image_id=140252440)

    result = _run()

    assert result["moved"] == 1
    assert result["folders_removed"] == 1
    assert not folder.exists(), "the emptied post folder is gone"
    assert adopted.exists(), "only the post folder goes, never the adopted folder"


def test_an_adopted_folder_with_anything_left_in_it_survives(
    client, fixture_images, tmp_path
):
    _archive_dir(tmp_path)
    adopted = _adopted_root(tmp_path)
    folder, image_id = _fetched_post_folder(
        client, adopted, fixture_images, 30497398, extra="notiz.txt"
    )
    _post(30497398, published_at="2026-08-24T21:15:00Z")
    _publish_archived(image_id, post_id=30497398, remote_image_id=140252441)

    result = _run()

    assert result["moved"] == 1
    assert result["folders_removed"] == 0
    assert folder.exists(), "a folder that still holds a file is left alone"
    assert (folder / "notiz.txt").exists(), "and nothing in it is discarded"
