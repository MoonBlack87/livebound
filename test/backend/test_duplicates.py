"""Finding the same picture twice inside the library.

A different question from "was this published before?" - that one looks into the
history, this one looks at the library against itself.
"""

from __future__ import annotations

import itertools
import shutil
import threading
import time
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from backend import db, duplicates, hashing, jobs
from backend.api import images as image_api
from backend.hashing import dhash64, file_sha256, pixel_sha256
from backend.scanner import service
from backend.store import images as image_store
from backend.store import posts as post_store
from backend.store import sources as source_store
from backend.store import usage


def _rescan() -> None:
    service.scan_roots(jobs.Job(id=0, kind="scan"))


def _wait_for_job(client, started: dict, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    current = started
    while current["status"] in ("starting", "running") and time.monotonic() < deadline:
        time.sleep(0.01)
        current = client.get(f"/api/jobs/{started['id']}").json()
    assert current["status"] not in ("starting", "running"), current
    return current


def _second_root(tmp_path) -> Path:
    folder = tmp_path / "second"
    folder.mkdir(exist_ok=True)
    source_store.add_root(str(folder), label="Second")
    return folder


def _single_image_root(tmp_path: Path) -> Path:
    folder = tmp_path / "single"
    folder.mkdir()
    path = folder / "image.png"
    Image.new("RGB", (12, 12), color=(40, 80, 120)).save(path)
    source_store.add_root(str(folder), label="Single")
    return path


def test_the_same_bytes_in_two_folders_form_a_certain_group(client, scanned, tmp_path):
    original = Path(image_store.get(scanned[0])["absolute_path"])
    folder = _second_root(tmp_path)
    shutil.copy2(original, folder / "copy.png")
    _rescan()

    groups = duplicates.exact_groups()
    assert len(groups) == 1
    assert groups[0]["confidence"] == duplicates.CERTAIN
    paths = sorted(member["absolute_path"] for member in groups[0]["members"])
    assert paths == sorted([str(original), str(folder / "copy.png")])


def test_dismissed_exact_stack_is_absent_from_fresh_and_cached_reports(
    client, scanned, tmp_path, monkeypatch
):
    original = Path(image_store.get(scanned[0])["absolute_path"])
    folder = _second_root(tmp_path)
    shutil.copy2(original, folder / "copy-a.png")
    shutil.copy2(original, folder / "copy-b.png")
    _rescan()
    group = next(group for group in duplicates.exact_groups() if len(group["members"]) == 3)
    image_ids = [member["id"] for member in group["members"]]
    snapshot = jobs.Job(
        id=1,
        kind="duplicates",
        status="done",
        result={"groups": [group]},
        finished_at="2026-08-28T10:00:00Z",
    )
    monkeypatch.setattr(jobs, "all_jobs", lambda: [snapshot])
    monkeypatch.setattr(jobs, "active", lambda _kind: None)

    response = client.post(
        "/api/duplicates/dismiss", json={"image_ids": list(reversed(image_ids))}
    )

    assert response.status_code == 200
    assert response.json() == {"dismissed": 3}
    assert [
        (row["image_id_a"], row["image_id_b"])
        for row in db.get_connection().execute(
            "SELECT image_id_a, image_id_b FROM duplicate_dismissals ORDER BY 1, 2"
        )
    ] == [
        (image_ids[0], image_ids[1]),
        (image_ids[0], image_ids[2]),
        (image_ids[1], image_ids[2]),
    ]
    assert client.get("/api/duplicates").json()["groups"] == []

    fresh = jobs.Job(id=2, kind="duplicates")
    duplicates.run(fresh)
    assert fresh.result == {"groups": []}


def test_dismissed_exact_member_keeps_duplicates_scanned_later(
    client, scanned, tmp_path
):
    original_id = scanned[0]
    original = Path(image_store.get(original_id)["absolute_path"])
    folder = _second_root(tmp_path)
    second = folder / "copy-a.png"
    shutil.copy2(original, second)
    _rescan()
    pair_group = next(
        group for group in duplicates.exact_groups() if original_id in {
            member["id"] for member in group["members"]
        }
    )
    second_id = next(
        member["id"] for member in pair_group["members"] if member["id"] != original_id
    )
    assert duplicates.dismiss([original_id, second_id]) == 1
    assert duplicates.exact_groups() == []

    third = folder / "copy-b.png"
    shutil.copy2(original, third)
    _rescan()
    third_id = db.get_connection().execute(
        "SELECT id FROM images WHERE absolute_path=?", (str(third),)
    ).fetchone()["id"]
    expected = {
        frozenset((original_id, third_id)),
        frozenset((second_id, third_id)),
    }
    base_key = f"pixel:{image_store.get(original_id)['pixel_sha256']}"
    expected_keys = {base_key, f"{base_key}:{second_id}"}

    for groups in (
        duplicates.exact_groups(),
        duplicates.exact_groups(image_ids=[original_id, second_id, third_id]),
    ):
        assert {
            frozenset(member["id"] for member in group["members"])
            for group in groups
        } == expected
        assert {group["key"] for group in groups} == expected_keys
        assert {group["confidence"] for group in groups} == {duplicates.CERTAIN}
        assert {group["reason"] for group in groups} == {pair_group["reason"]}
        assert not any(
            {original_id, second_id}
            <= {member["id"] for member in group["members"]}
            for group in groups
        )


def test_cached_exact_group_repartitions_around_one_dismissed_pair(
    client, scanned, tmp_path
):
    original = Path(image_store.get(scanned[0])["absolute_path"])
    folder = _second_root(tmp_path)
    shutil.copy2(original, folder / "copy-a.png")
    shutil.copy2(original, folder / "copy-b.png")
    _rescan()
    snapshot = next(
        group for group in duplicates.exact_groups() if len(group["members"]) == 3
    )
    first_id, second_id, third_id = sorted(
        member["id"] for member in snapshot["members"]
    )

    assert duplicates.dismiss([first_id, second_id]) == 1

    current = duplicates.current_groups([snapshot])
    assert {
        frozenset(member["id"] for member in group["members"])
        for group in current
    } == {
        frozenset((first_id, third_id)),
        frozenset((second_id, third_id)),
    }
    assert {group["key"] for group in current} == {
        snapshot["key"],
        f"{snapshot['key']}:{second_id}",
    }


def test_library_stack_does_not_rejoin_a_dismissed_exact_pair(
    client, scanned, tmp_path
):
    original_id = scanned[0]
    original = Path(image_store.get(original_id)["absolute_path"])
    folder = _second_root(tmp_path)
    shutil.copy2(original, folder / "copy-a.png")
    _rescan()
    first_group = next(
        group for group in duplicates.exact_groups() if original_id in {
            member["id"] for member in group["members"]
        }
    )
    second_id = next(
        member["id"] for member in first_group["members"] if member["id"] != original_id
    )
    assert duplicates.dismiss([original_id, second_id]) == 1

    third = folder / "copy-b.png"
    shutil.copy2(original, third)
    _rescan()
    third_id = db.get_connection().execute(
        "SELECT id FROM images WHERE absolute_path=?", (str(third),)
    ).fetchone()["id"]
    report_groups = duplicates.exact_groups()
    assert {
        frozenset(member["id"] for member in group["members"])
        for group in report_groups
    } == {
        frozenset((original_id, third_id)),
        frozenset((second_id, third_id)),
    }

    library = duplicates.library_groups()
    stacked_ids = [member["id"] for group in library for member in group["members"]]
    assert [
        {member["id"] for member in group["members"]} for group in library
    ] == [{original_id, third_id}]
    assert len(stacked_ids) == len(set(stacked_ids))
    assert not any(
        {original_id, second_id}
        <= {member["id"] for member in group["members"]}
        for group in library
    )


def test_identical_files_in_archive_roots_form_an_archive_duplicate(
    client, scanned, tmp_path
):
    original_row = image_store.get(scanned[0])
    original = Path(original_row["absolute_path"])
    source_store.add_root(
        source_store.get_root(original_row["source_root_id"])["path"], is_archive=True
    )
    second_archive = tmp_path / "second-archive"
    second_archive.mkdir()
    source_store.add_root(str(second_archive), label="Second archive", is_archive=True)
    shutil.copy2(original, second_archive / "copy.png")
    _rescan()

    group = next(group for group in duplicates.exact_groups() if len(group["members"]) == 2)

    assert group["archive_duplicate"] is True


def test_one_archived_and_one_loose_file_remain_an_ordinary_duplicate(
    client, scanned, tmp_path
):
    original = Path(image_store.get(scanned[0])["absolute_path"])
    archive = tmp_path / "archive"
    archive.mkdir()
    source_store.add_root(str(archive), label="Archive", is_archive=True)
    shutil.copy2(original, archive / "copy.png")
    _rescan()

    group = next(group for group in duplicates.exact_groups() if len(group["members"]) == 2)

    assert group["archive_duplicate"] is False


def test_duplicate_search_removes_an_unbound_missing_archive_candidate(
    client, scanned, tmp_path
):
    original_row = image_store.get(scanned[0])
    original = Path(original_row["absolute_path"])
    source_store.add_root(
        source_store.get_root(original_row["source_root_id"])["path"], is_archive=True
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    source_store.add_root(str(archive), label="Archive copy", is_archive=True)
    copy = archive / "copy.png"
    shutil.copy2(original, copy)
    _rescan()
    copy_id = image_store.get_by_path(str(copy))["id"]
    copy.unlink()

    job = jobs.Job(id=1, kind="duplicates")
    duplicates.run(job)

    assert image_store.get(copy_id) is None
    assert job.result == {"groups": []}


def test_duplicate_search_marks_a_post_bound_missing_archive_candidate(
    client, scanned, tmp_path
):
    original_row = image_store.get(scanned[0])
    original = Path(original_row["absolute_path"])
    source_store.add_root(
        source_store.get_root(original_row["source_root_id"])["path"], is_archive=True
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    source_store.add_root(str(archive), label="Archive copy", is_archive=True)
    copy = archive / "copy.png"
    shutil.copy2(original, copy)
    _rescan()
    copy_id = image_store.get_by_path(str(copy))["id"]
    post_id = post_store.create(title="Bound copy")
    post_store.set_images(post_id, [copy_id])
    copy.unlink()

    job = jobs.Job(id=1, kind="duplicates")
    duplicates.run(job)

    assert image_store.get(copy_id)["is_missing"] is True
    assert post_store.images(post_id)[0]["image_id"] == copy_id
    assert job.result == {"groups": []}


def test_duplicate_search_leaves_a_missing_candidate_in_an_unreachable_root(
    client, scanned, tmp_path
):
    original_row = image_store.get(scanned[0])
    original = Path(original_row["absolute_path"])
    source_store.add_root(
        source_store.get_root(original_row["source_root_id"])["path"], is_archive=True
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    source_store.add_root(str(archive), label="Archive copy", is_archive=True)
    copy = archive / "copy.png"
    shutil.copy2(original, copy)
    _rescan()
    copy_id = image_store.get_by_path(str(copy))["id"]
    shutil.rmtree(archive)

    job = jobs.Job(id=1, kind="duplicates")
    duplicates.run(job)

    assert image_store.get(copy_id)["is_missing"] is False
    assert {member["id"] for member in job.result["groups"][0]["members"]} == {
        scanned[0],
        copy_id,
    }


def test_duplicate_search_regroups_after_a_missing_perceptual_bridge(tmp_path):
    root_id = source_store.add_root(str(tmp_path), label="Archive", is_archive=True)["id"]
    image_ids = [
        _add_similar_row(root_id, tmp_path / name, width=width, height=100)
        for name, width in (("square.png", 100), ("bridge.png", 120), ("wide.png", 144))
    ]
    for name in ("square.png", "bridge.png", "wide.png"):
        (tmp_path / name).touch()
    assert len(duplicates.similar_groups()) == 1
    (tmp_path / "bridge.png").unlink()

    job = jobs.Job(id=1, kind="duplicates")
    duplicates.run(job)

    assert image_store.get(image_ids[1]) is None
    assert job.result == {"groups": []}


def test_duplicate_report_marks_members_bound_to_a_post(client, scanned, tmp_path, monkeypatch):
    original = Path(image_store.get(scanned[0])["absolute_path"])
    folder = _second_root(tmp_path)
    copy = folder / "copy.png"
    shutil.copy2(original, copy)
    _rescan()
    copy_id = image_store.get_by_path(str(copy))["id"]
    post_id = post_store.create(title="Bound copy")
    post_store.set_images(post_id, [copy_id])

    job = jobs.Job(id=1, kind="duplicates", status="done")
    duplicates.run(job)
    job.status = "done"
    monkeypatch.setattr(jobs, "all_jobs", lambda: [job])
    monkeypatch.setattr(jobs, "active", lambda _kind: None)

    response = client.get("/api/duplicates")

    assert response.status_code == 200
    members = {member["id"]: member for member in response.json()["groups"][0]["members"]}
    bound = duplicates.post_bound_image_ids([scanned[0], copy_id])
    assert bound == {copy_id}
    assert {image_id for image_id, member in members.items() if member["post_bound"]} == bound


def test_post_holders_names_the_posts_a_selection_is_already_in(client, scanned):
    """The warning before a second post has to say which posts, not how many.

    Two posts holding one picture is allowed and sometimes meant. Forty
    thumbnails are not read badge by badge, though, so the answer that lets
    somebody decide is the post's own title and state - "Draft" is a different
    decision from "Published".
    """
    bound_id, free_id = scanned
    draft_id = post_store.create(title="Evening set", state="draft")
    post_store.set_images(draft_id, [bound_id])
    published_id = post_store.create(title="", state="published")
    post_store.set_images(published_id, [bound_id])

    response = client.post("/api/images/post-holders", json={"image_ids": scanned})

    assert response.json() == {
        "items": [
            {
                "image_id": bound_id,
                "posts": [
                    {"post_id": draft_id, "title": "Evening set", "state": "draft"},
                    {"post_id": published_id, "title": "", "state": "published"},
                ],
            }
        ]
    }, "an image no post holds contributes nothing to the warning"

    free_only = client.post("/api/images/post-holders", json={"image_ids": [free_id]})
    assert free_only.json() == {"items": []}


@pytest.mark.parametrize("state", ["draft", "published"])
def test_trash_refuses_post_bound_images_and_moves_the_rest(client, scanned, fixture_images, state):
    folder, _ = fixture_images
    trash = folder / "trash"
    client.put("/api/settings", json={"trash_folder": str(trash)})
    bound_id, movable_id = scanned
    post_id = post_store.create(title="Keeps its image", state=state)
    post_store.set_images(post_id, [bound_id])
    bound_path = Path(image_store.get(bound_id)["absolute_path"])
    movable_path = Path(image_store.get(movable_id)["absolute_path"])

    planned = client.post("/api/images/trash-plan", json={"image_ids": scanned})
    started = client.post("/api/images/trash", json={"image_ids": scanned}).json()
    finished = _wait_for_job(client, started)

    assert planned.json() == {
        "post_bound_image_ids": [bound_id],
        "items": [
            {
                "id": bound_id,
                "absolute_path": str(bound_path),
                "post_bound": True,
            },
            {
                "id": movable_id,
                "absolute_path": str(movable_path),
                "post_bound": False,
            },
        ],
    }
    assert finished["status"] == "done"
    assert finished["result"] == {
        "moved": 1,
        "sidecars": 0,
        "failed": [],
        "post_bound_image_ids": [bound_id],
        "unstarted_image_ids": [],
    }
    assert bound_path.exists() and image_store.get(bound_id)["is_trashed"] is False
    assert not movable_path.exists() and image_store.get(movable_id)["is_trashed"] is True


@pytest.mark.parametrize("endpoint", ["/api/images/trash", "/api/images/delete"])
def test_archive_duplicate_endpoints_refuse_a_crafted_request(
    client, scanned, tmp_path, endpoint
):
    original_row = image_store.get(scanned[0])
    original = Path(original_row["absolute_path"])
    source_store.add_root(
        source_store.get_root(original_row["source_root_id"])["path"], is_archive=True
    )
    second_archive = tmp_path / "second-archive"
    second_archive.mkdir()
    source_store.add_root(str(second_archive), label="Second archive", is_archive=True)
    copy = second_archive / "copy.png"
    shutil.copy2(original, copy)
    _rescan()
    group = next(group for group in duplicates.exact_groups() if len(group["members"]) == 2)
    target_id = next(member["id"] for member in group["members"] if member["id"] != scanned[0])
    client.put("/api/settings", json={"trash_folder": str(tmp_path / "trash")})

    response = client.post(endpoint, json={"image_ids": [target_id]})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "archive_duplicate_exempt"
    assert original.exists() and copy.exists()
    assert image_store.get(target_id) is not None


def test_exact_pixel_members_are_certain_while_lossy_lookalikes_remain_visible(
    client, scanned, tmp_path
):
    original = Path(image_store.get(scanned[0])["absolute_path"])
    folder = _second_root(tmp_path)
    with Image.open(original) as image:
        rgb = image.convert("RGB")
        rgb.save(folder / "reencoded.png")
        rgb.save(folder / "lossless.webp", "WEBP", lossless=True)
        rgb.save(folder / "lossy.jpg", "JPEG", quality=70)
        rgb.resize((rgb.width // 2, rgb.height // 2)).save(folder / "resized.png")
    _rescan()

    exact = duplicates.exact_groups()
    exact_group = next(group for group in exact if group["reason"] == "identical pixels")
    exact_paths = {member["absolute_path"] for member in exact_group["members"]}
    assert exact_group["confidence"] == duplicates.CERTAIN
    assert exact_paths == {
        str(original),
        str(folder / "reencoded.png"),
        str(folder / "lossless.webp"),
    }

    similar = duplicates.similar_groups(exact=exact)
    similar_group = next(
        group
        for group in similar
        if str(folder / "lossy.jpg")
        in {member["absolute_path"] for member in group["members"]}
    )
    similar_paths = {member["absolute_path"] for member in similar_group["members"]}
    assert similar_group["confidence"] == duplicates.PROBABLE
    assert {str(folder / "lossy.jpg"), str(folder / "resized.png")} <= similar_paths
    assert len(exact_paths & similar_paths) == 1, (
        "one exact member remains as the comparison anchor"
    )

def test_pixel_hash_is_exact_across_lossless_wrappers_but_not_a_lossy_one(
    fixture_images, tmp_path
):
    source = fixture_images[1][0]
    metadata_edit = tmp_path / "metadata.png"
    lossless_webp = tmp_path / "lossless.webp"
    lossy_jpeg = tmp_path / "lossy.jpg"

    with Image.open(source) as image:
        info = PngImagePlugin.PngInfo()
        for key, value in (getattr(image, "text", {}) or {}).items():
            info.add_text(key, value)
        info.add_text("Comment", "rewritten metadata")
        image.save(metadata_edit, "PNG", pnginfo=info, compress_level=9)
        image.convert("RGB").save(lossless_webp, "WEBP", lossless=True)
        image.convert("RGB").save(lossy_jpeg, "JPEG", quality=70)

    original_pixels = pixel_sha256(source)
    assert file_sha256(metadata_edit) != file_sha256(source)
    assert pixel_sha256(metadata_edit) == original_pixels
    assert pixel_sha256(lossless_webp) == original_pixels
    assert pixel_sha256(lossy_jpeg) != original_pixels

    portrait = tmp_path / "portrait.png"
    landscape = tmp_path / "landscape.png"
    Image.new("RGB", (32, 48), "black").save(portrait)
    Image.new("RGB", (48, 32), "black").save(landscape)
    assert pixel_sha256(portrait) != pixel_sha256(landscape)

    usage.record(
        sha256=file_sha256(metadata_edit),
        pixel_sha256=pixel_sha256(metadata_edit),
        phash=dhash64(metadata_edit),
        post_id=None,
    )
    result = usage.check(
        file_sha256(source),
        dhash64(source),
        pixel_sha256=original_pixels,
    )
    assert result["has_certain"]
    assert result["confirmed"][0]["confidence"] == usage.CERTAIN


def test_an_exact_twin_is_not_listed_a_second_time_as_similar(client, scanned, tmp_path):
    original = Path(image_store.get(scanned[0])["absolute_path"])
    folder = _second_root(tmp_path)
    shutil.copy2(original, folder / "copy.png")
    _rescan()

    assert duplicates.exact_groups()
    assert duplicates.similar_groups() == [], "an exact group already says it all"


def _add_similar_row(
    root_id: int,
    path: Path,
    *,
    width: int | None = 100,
    height: int | None = 100,
    phash: str = "5555555555555555",
    seed: str | None = None,
    prompt: str | None = None,
    folder: str = "",
) -> int:
    parsed = None
    if seed is not None or prompt is not None:
        parsed = {"prompt": prompt, "fields": {"Seed": seed}}
    return image_store.upsert(
        root_id,
        {
            "absolute_path": str(path),
            "relative_path": f"{folder}/{path.name}" if folder else path.name,
            "folder": folder,
            "width": width,
            "height": height,
            "sha256": f"sha-{path.name}",
            "pixel_sha256": f"pixel-{path.name}",
            "phash": phash,
            "parsed": parsed,
        },
    )


def test_dismissed_perceptual_pair_is_absent_from_fresh_and_cached_reports(tmp_path):
    root_id = source_store.add_root(str(tmp_path), label="Test")["id"]
    image_ids = [
        _add_similar_row(root_id, tmp_path / name)
        for name in ("similar-a.png", "similar-b.png")
    ]
    snapshot = duplicates.similar_groups()
    assert len(snapshot) == 1

    assert duplicates.dismiss(list(reversed(image_ids))) == 1

    assert duplicates.current_groups(snapshot) == []
    fresh = jobs.Job(id=1, kind="duplicates")
    duplicates.run(fresh)
    assert fresh.result == {"groups": []}


def test_library_merges_an_exact_group_with_its_probable_comparison_anchor(tmp_path):
    root_id = source_store.add_root(str(tmp_path), label="Test")["id"]
    exact_ids = [
        _add_similar_row(
            root_id,
            tmp_path / name,
            phash="5555555555555555",
            seed=seed,
            prompt=f"different prompt {seed}",
        )
        for name, seed in (("exact-a.png", "1"), ("exact-b.png", "2"))
    ]
    _add_similar_row(
        root_id,
        tmp_path / "lookalike.png",
        phash="5555555555555555",
        seed="3",
        prompt="a third different prompt",
    )
    with db.transaction() as conn:
        conn.execute(
            "UPDATE images SET pixel_sha256='shared-pixels' WHERE id IN (?, ?)",
            exact_ids,
        )

    groups = duplicates.library_groups()

    assert len(groups) == 1
    assert groups[0]["confidence"] == duplicates.PROBABLE
    assert {member["absolute_path"] for member in groups[0]["members"]} == {
        str(tmp_path / "exact-a.png"),
        str(tmp_path / "exact-b.png"),
        str(tmp_path / "lookalike.png"),
    }


def test_same_dhash_with_very_different_aspect_ratios_does_not_group(client, tmp_path):
    root_id = source_store.add_root(str(tmp_path), label="Test")["id"]
    _add_similar_row(root_id, tmp_path / "portrait.png", width=100, height=200)
    _add_similar_row(root_id, tmp_path / "landscape.png", width=200, height=100)

    assert duplicates.similar_groups() == []


@pytest.mark.parametrize(
    ("first_dimensions", "second_dimensions", "admissible"),
    [
        ((None, None), (100, 100), True),
        ((None, 100), (100, 100), True),
        ((None, None), (None, None), True),
        ((100, 200), (200, 100), False),
    ],
)
def test_unknown_dimensions_do_not_veto_a_same_dhash_pair(
    client,
    tmp_path,
    first_dimensions,
    second_dimensions,
    admissible,
):
    root_id = source_store.add_root(str(tmp_path), label="Test")["id"]
    shared_phash = "D5D5D5D5D5D5D5D5"
    image_ids = [
        _add_similar_row(
            root_id,
            tmp_path / name,
            width=width,
            height=height,
            phash=shared_phash,
        )
        for name, (width, height) in zip(
            ("first.png", "second.png"),
            (first_dimensions, second_dimensions),
            strict=True,
        )
    ]

    groups = duplicates.similar_groups()

    assert bool(groups) is admissible
    if admissible:
        assert [member["id"] for member in groups[0]["members"]] == image_ids


def test_grouping_asks_the_index_once_not_once_per_image(client, tmp_path):
    """The banded lookup was eight queries per row - thirty thousand of them on
    a four-thousand-image library, on every library page load."""
    root_id = source_store.add_root(str(tmp_path), label="Test")["id"]
    for index in range(12):
        _add_similar_row(
            root_id,
            tmp_path / f"row-{index}.png",
            phash=f"{index:016x}",
            width=100 + index,
            height=100,
        )

    statements: list[str] = []
    connection = db.get_connection()
    connection.set_trace_callback(statements.append)
    try:
        duplicates.similar_groups()
    finally:
        connection.set_trace_callback(None)

    selects = [line for line in statements if line.lstrip().upper().startswith("SELECT")]
    assert len(selects) < 12, (
        f"one query per image is back: {len(selects)} selects for 12 images"
    )


def test_no_image_with_an_admissible_partner_is_left_out(client, tmp_path):
    """The invariant: every image that forms an admissible pair with at least
    one other is in exactly one group, whichever row the scan starts from.

    Grouping used to grow from a seed and mark its members taken, so an image
    comparable to a later row but not to the seed was skipped - and once its
    partners were taken, it was reported nowhere. Here `wider` is comparable to
    `wide` but not to `square`, and `wide` is comparable to both.
    """
    root_id = source_store.add_root(str(tmp_path), label="Test")["id"]
    shared_phash = "D5D5D5D5D5D5D5D5"
    shapes = {"a-square.png": (100, 100), "b-wide.png": (120, 100), "c-wider.png": (144, 100)}
    ids = {
        name: _add_similar_row(
            root_id, tmp_path / name, width=width, height=height, phash=shared_phash
        )
        for name, (width, height) in shapes.items()
    }
    rows = {value: image_store.get(value) for value in ids.values()}
    admissible = {
        frozenset((first, second))
        for first, second in itertools.combinations(sorted(ids.values()), 2)
        if duplicates._comparable_dimensions(rows[first], rows[second])
    }
    assert admissible == {
        frozenset((ids["a-square.png"], ids["b-wide.png"])),
        frozenset((ids["b-wide.png"], ids["c-wider.png"])),
    }, "the chain this test needs: a-b and b-c admissible, a-c not"

    reported = [
        member["id"] for group in duplicates.similar_groups() for member in group["members"]
    ]

    with_a_partner = {value for pair in admissible for value in pair}
    assert sorted(reported) == sorted(with_a_partner), "each is reported"
    assert len(reported) == len(set(reported)), "and none of them twice"


def test_same_dhash_in_separate_shape_groups_has_distinct_keys(client, tmp_path):
    root_id = source_store.add_root(str(tmp_path), label="Test")["id"]
    shared_phash = "D5D5D5D5D5D5D5D5"
    wide_ids = [
        _add_similar_row(
            root_id, tmp_path / name, width=200, height=100, phash=shared_phash
        )
        for name in ("a-wide.png", "b-wide.png")
    ]
    tall_ids = [
        _add_similar_row(
            root_id, tmp_path / name, width=100, height=200, phash=shared_phash
        )
        for name in ("c-tall.png", "d-tall.png")
    ]

    groups = duplicates.similar_groups()

    assert {group["key"] for group in groups} == {
        f"phash:{shared_phash}:{wide_ids[0]}",
        f"phash:{shared_phash}:{tall_ids[0]}",
    }
    assert [
        [member["id"] for member in group["members"]] for group in groups
    ] == [wide_ids, tall_ids]


def test_a_dimensionless_row_does_not_bridge_incompatible_shape_groups(
    client, tmp_path
):
    root_id = source_store.add_root(str(tmp_path), label="Test")["id"]
    shared_phash = "D5D5D5D5D5D5D5D5"
    wide_ids = [
        _add_similar_row(
            root_id, tmp_path / name, width=200, height=100, phash=shared_phash
        )
        for name in ("a-wide.png", "b-wide.png")
    ]
    tall_ids = [
        _add_similar_row(
            root_id, tmp_path / name, width=100, height=200, phash=shared_phash
        )
        for name in ("c-tall.png", "d-tall.png")
    ]
    unknown_id = _add_similar_row(
        root_id,
        tmp_path / "e-unknown.png",
        width=None,
        height=None,
        phash=shared_phash,
    )

    groups = duplicates.similar_groups()

    assert [
        [member["id"] for member in group["members"]] for group in groups
    ] == [wide_ids + [unknown_id], tall_ids]


@pytest.mark.parametrize(
    ("distance", "seed"),
    [(5, "3324970406"), (6, "458558052"), (4, "1779548050")],
)
def test_reported_matching_generations_remain_probable_groups(
    client, tmp_path, distance, seed
):
    root_id = source_store.add_root(str(tmp_path), label="Test")["id"]
    prompt = f"reported multi-image generation {seed}"
    first_phash = "0000000000000000"
    second_phash = f"{(1 << distance) - 1:016x}"
    _add_similar_row(
        root_id, tmp_path / "first.png", phash=first_phash, seed=seed, prompt=prompt
    )
    _add_similar_row(
        root_id, tmp_path / "second.png", phash=second_phash, seed=seed, prompt=prompt
    )

    groups = duplicates.similar_groups()

    assert hashing.hamming(first_phash, second_phash) == distance
    assert len(groups) == 1
    assert groups[0]["confidence"] == duplicates.PROBABLE
    assert len(groups[0]["members"]) == 2


def test_library_collapses_certain_and_probable_groups_before_paging_and_filtering(
    tmp_path,
):
    root_id = source_store.add_root(str(tmp_path), label="Test")["id"]
    exact_ids = [
        _add_similar_row(
            root_id,
            tmp_path / name,
            phash="1111111111111111",
        )
        for name in ("certain-a.png", "certain-b.png")
    ]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE images SET pixel_sha256='shared-exact-pixels' WHERE id IN (?, ?)",
            exact_ids,
        )
    for name, seed in (("probable-a.png", "100"), ("probable-b.png", "101")):
        _add_similar_row(
            root_id,
            tmp_path / name,
            phash="AAAAAAAAAAAAAAAA",
            seed=seed,
            prompt=f"different prompt {seed}",
        )

    def library_page(**overrides):
        params = {
            "source_id": None,
            "folder": None,
            "q": None,
            "usage_state": "all",
            "include_missing": False,
            "limit": 200,
            "offset": 0,
        }
        params.update(overrides)
        return image_api.list_images(**params)

    first = library_page(limit=1)
    second = library_page(limit=1, offset=1)

    assert first["total"] == second["total"] == 2
    assert len(first["items"]) == len(second["items"]) == 1
    assert first["items"][0]["duplicate_confidence"] == duplicates.CERTAIN
    assert first["items"][0]["duplicate_count"] == 2
    assert second["items"][0]["duplicate_confidence"] == duplicates.PROBABLE
    assert second["items"][0]["duplicate_count"] == 2

    filtered = library_page(q="probable")
    assert filtered["total"] == 1
    assert len(filtered["items"]) == 1
    assert filtered["items"][0]["duplicate_confidence"] == duplicates.PROBABLE
    assert {
        member["absolute_path"] for member in filtered["items"][0]["duplicate_members"]
    } == {str(tmp_path / "probable-a.png"), str(tmp_path / "probable-b.png")}


def test_usage_state_filters_each_image_before_stacking_and_paging(tmp_path):
    root_id = source_store.add_root(str(tmp_path), label="Test")["id"]
    prompt = "a detailed coffee machine on a kitchen counter"
    used_id = _add_similar_row(
        root_id,
        tmp_path / "coffee-used.png",
        phash="1111111111111111",
        seed="42",
        prompt=prompt,
        folder="series",
    )
    probable_id = _add_similar_row(
        root_id,
        tmp_path / "coffee-unused.png",
        phash="1111111111111111",
        seed="99",
        prompt="a different detailed coffee machine",
        folder="series",
    )
    withdrawn_id = _add_similar_row(
        root_id,
        tmp_path / "coffee-withdrawn.png",
        phash="FFFFFFFFFFFFFFFF",
        folder="series",
    )
    used_image = image_store.get(used_id)
    usage.record(
        sha256="historic-used",
        pixel_sha256=used_image["pixel_sha256"],
        phash="1111111111111111",
        post_id=None,
        seed="42",
        prompt_hash=usage.prompt_fingerprint(prompt),
    )
    withdrawn = image_store.get(withdrawn_id)
    usage.record(
        sha256=withdrawn["sha256"],
        pixel_sha256=withdrawn["pixel_sha256"],
        phash=withdrawn["phash"],
        post_id=None,
        status=usage.WITHDRAWN,
    )
    with db.transaction() as conn:
        conn.execute("UPDATE images SET is_missing=1 WHERE id=?", (withdrawn_id,))

    params = {
        "source_id": root_id,
        "folder": "series",
        "q": "coffee",
        "include_missing": True,
        "limit": 1,
        "offset": 0,
    }
    all_images = image_api.list_images(usage_state="all", **params)
    all_complete = image_api.list_images(
        usage_state="all", **{**params, "limit": 200}
    )
    used = image_api.list_images(usage_state="used", **params)
    unused_first = image_api.list_images(usage_state="unused", **params)
    unused_second = image_api.list_images(usage_state="unused", **{**params, "offset": 1})

    assert all_images["total"] == 2
    assert all_images["items"][0]["duplicate_count"] == 2
    assert used["total"] == 1
    assert used["items"][0]["id"] == used_id
    assert used["items"][0]["used"] is True
    assert used["items"][0]["similar"] is True
    assert used["items"][0]["duplicate_count"] == 1
    assert unused_first["total"] == unused_second["total"] == 2
    unused_items = unused_first["items"] + unused_second["items"]
    assert {item["id"] for item in unused_items} == {probable_id, withdrawn_id}
    assert all(item["used"] is False for item in unused_items)
    probable = next(item for item in unused_items if item["id"] == probable_id)
    withdrawn_item = next(item for item in unused_items if item["id"] == withdrawn_id)
    assert probable["similar"] is True
    assert withdrawn_item["similar"] is False
    assert all(item["duplicate_count"] == 1 for item in unused_items)

    all_ids = {
        member["id"]
        for item in all_complete["items"]
        for member in (item["duplicate_members"] or [item])
    }
    used_ids = {item["id"] for item in used["items"]}
    unused_ids = {item["id"] for item in unused_items}
    assert used_ids.isdisjoint(unused_ids)
    assert used_ids | unused_ids == all_ids


def test_bulk_usage_classification_does_not_query_history_per_image():
    count = 401
    phash = "5555555555555555"
    with db.transaction() as conn:
        for index in range(count):
            usage.record(
                sha256=f"history-{index}",
                pixel_sha256=f"history-pixels-{index}",
                phash=phash,
                post_id=None,
                conn=conn,
            )
    candidates = [
        {
            "sha256": f"candidate-{index}",
            "pixel_sha256": f"candidate-pixels-{index}",
            "phash": phash,
            "seed": None,
            "prompt_hash": None,
        }
        for index in range(count)
    ]
    history_queries = []
    conn = db.get_connection()
    conn.set_trace_callback(
        lambda sql: history_queries.append(sql)
        if sql.startswith("SELECT") and "image_usage" in sql
        else None
    )
    try:
        result = usage.check_many(candidates)
    finally:
        conn.set_trace_callback(None)

    band_queries = [
        sql for sql in history_queries if "phash_b" in sql and " IN (" in sql
    ]
    assert len(history_queries) <= 13
    assert len(history_queries) < count
    assert len(band_queries) == usage.BANDS
    assert not any("phash IS NOT NULL" in sql for sql in history_queries)
    assert all(mark["has_similar"] and not mark["has_certain"] for mark in result.values())


@pytest.mark.parametrize(
    ("state", "deleted_members", "expected_groups", "expected_members"),
    [
        ("no delete", 0, 1, 4),
        ("one member deleted", 1, 1, 3),
        ("all but one deleted", 3, 0, 0),
        ("all deleted", 4, 0, 0),
        ("one member missing", 0, 1, 3),
        ("two runs finished", 0, 1, 4),
    ],
)
def test_duplicates_endpoint_returns_the_latest_current_snapshot(
    client,
    scanned,
    tmp_path,
    monkeypatch,
    state,
    deleted_members,
    expected_groups,
    expected_members,
):
    original = Path(image_store.get(scanned[0])["absolute_path"])
    folder = _second_root(tmp_path)
    for index in range(3):
        shutil.copy2(original, folder / f"copy-{index}.png")
    _rescan()
    group = duplicates.exact_groups()[0]
    members = group["members"]
    assert len(members) == 4

    if state == "one member missing":
        Path(members[0]["absolute_path"]).unlink()
        _rescan()
        assert image_store.get(members[0]["id"]) is None
    else:
        for member in members[:deleted_members]:
            image_store.delete(member["id"])

    newest = jobs.Job(
        id=2,
        kind="duplicates",
        status="done",
        result={"groups": [group]},
        finished_at="2026-08-22T12:00:00Z",
    )
    finished = [newest]
    if state == "two runs finished":
        oldest = jobs.Job(
            id=1,
            kind="duplicates",
            status="done",
            result={"groups": [{**group, "key": "old", "members": members[:2]}]},
            finished_at="2026-08-22T11:00:00Z",
        )
        finished.append(oldest)
    monkeypatch.setattr(jobs, "all_jobs", lambda: finished)
    monkeypatch.setattr(jobs, "active", lambda _kind: None)

    response = client.get("/api/duplicates")

    assert response.status_code == 200
    report = response.json()
    assert len(report["groups"]) == expected_groups
    assert sum(len(item["members"]) for item in report["groups"]) == expected_members
    assert report["checked_at"] == "2026-08-22T12:00:00Z"


def test_similar_group_members_are_oldest_first_with_id_as_the_tie_breaker(
    client, scanned, tmp_path
):
    original = Path(image_store.get(scanned[0])["absolute_path"])
    folder = _second_root(tmp_path)
    with Image.open(original) as image:
        for index, quality in enumerate((30, 60, 90)):
            image.convert("RGB").save(folder / f"copy-{index}.jpg", quality=quality)
    _rescan()
    image_ids = sorted(member["id"] for member in duplicates.similar_groups()[0]["members"])
    seen_at = {
        image_ids[0]: "2026-01-04T00:00:00Z",
        image_ids[1]: "2026-01-01T00:00:00Z",
        image_ids[2]: "2026-01-01T00:00:00Z",
        image_ids[3]: "2026-01-03T00:00:00Z",
    }
    with db.transaction() as conn:
        conn.executemany(
            "UPDATE images SET first_seen_at=? WHERE id=?",
            [(seen_at[image_id], image_id) for image_id in image_ids],
        )

    members = duplicates.similar_groups()[0]["members"]

    assert [member["id"] for member in members] == [
        image_ids[1],
        image_ids[2],
        image_ids[3],
        image_ids[0],
    ]


def test_exact_group_members_are_oldest_first_with_id_as_the_tie_breaker(
    client, scanned, tmp_path
):
    original = Path(image_store.get(scanned[0])["absolute_path"])
    folder = _second_root(tmp_path)
    for index in range(3):
        shutil.copy2(original, folder / f"copy-{index}.png")
    _rescan()
    image_ids = sorted(member["id"] for member in duplicates.exact_groups()[0]["members"])
    seen_at = {
        image_ids[0]: "2026-01-04T00:00:00Z",
        image_ids[1]: "2026-01-01T00:00:00Z",
        image_ids[2]: "2026-01-01T00:00:00Z",
        image_ids[3]: "2026-01-03T00:00:00Z",
    }
    with db.transaction() as conn:
        conn.executemany(
            "UPDATE images SET first_seen_at=? WHERE id=?",
            [(seen_at[image_id], image_id) for image_id in image_ids],
        )

    members = duplicates.exact_groups()[0]["members"]

    assert [member["id"] for member in members] == [
        image_ids[1],
        image_ids[2],
        image_ids[3],
        image_ids[0],
    ]


def test_rescan_reuses_the_pixel_hash_when_the_file_is_unchanged(
    client, scanned, monkeypatch
):
    before = {image_id: image_store.get(image_id)["pixel_sha256"] for image_id in scanned}

    def unexpected_decode(_path: Path) -> str:
        pytest.fail("an unchanged image must not be decoded again for its pixel hash")

    monkeypatch.setattr(hashing, "pixel_sha256", unexpected_decode)
    _rescan()

    assert {image_id: image_store.get(image_id)["pixel_sha256"] for image_id in scanned} == before


def test_rescan_reuses_the_phash_when_the_file_is_unchanged(tmp_path, monkeypatch):
    path = _single_image_root(tmp_path)
    calls: list[Path] = []

    def counted_dhash(candidate: Path, image=None) -> str:
        assert image is not None
        calls.append(candidate)
        return "1111111111111111"

    monkeypatch.setattr(hashing, "dhash64", counted_dhash)

    _rescan()
    first = image_store.get_by_path(str(path))["phash"]
    _rescan()

    assert calls == [path]
    assert first == "1111111111111111"
    assert image_store.get_by_path(str(path))["phash"] == first


def test_rescan_fills_a_missing_phash_on_an_otherwise_unchanged_file(
    tmp_path, monkeypatch
):
    path = _single_image_root(tmp_path)
    _rescan()
    conn = db.get_connection()
    conn.execute("UPDATE images SET phash=NULL WHERE absolute_path=?", (str(path),))
    conn.commit()
    calls: list[Path] = []

    def counted_dhash(candidate: Path, image=None) -> str:
        assert image is not None
        calls.append(candidate)
        return "2222222222222222"

    monkeypatch.setattr(hashing, "dhash64", counted_dhash)

    _rescan()

    assert calls == [path]
    assert image_store.get_by_path(str(path))["phash"] == "2222222222222222"


def test_rescan_recomputes_the_phash_when_the_file_changes(tmp_path, monkeypatch):
    path = _single_image_root(tmp_path)
    _rescan()
    before = image_store.get_by_path(str(path))["phash"]
    calls: list[Path] = []

    def fresh_dhash(candidate: Path, image=None) -> str:
        assert image is not None
        calls.append(candidate)
        return "3333333333333333"

    monkeypatch.setattr(hashing, "dhash64", fresh_dhash)
    Image.new("RGB", (12, 12), color=(120, 80, 40)).save(path)

    _rescan()

    assert calls == [path]
    assert image_store.get_by_path(str(path))["phash"] == "3333333333333333"
    assert image_store.get_by_path(str(path))["phash"] != before


def test_schema_thirteen_invalidates_pixel_hashes_without_decoding_at_startup(
    client, scanned
):
    image = image_store.get(scanned[0])
    usage.record(
        sha256=image["sha256"],
        pixel_sha256=image["pixel_sha256"],
        phash=image["phash"],
        post_id=None,
    )
    conn = db.get_connection()
    conn.execute("PRAGMA user_version=12")
    conn.commit()

    db.init_db()

    stored_image_hash = conn.execute(
        "SELECT pixel_sha256 FROM images WHERE id=?", (image["id"],)
    ).fetchone()[0]
    assert stored_image_hash is None
    assert conn.execute("SELECT pixel_sha256 FROM image_usage").fetchone()[0] is None


def test_deleting_removes_the_file_its_sidecars_and_the_row(client, scanned, tmp_path):
    original = Path(image_store.get(scanned[0])["absolute_path"])
    folder = _second_root(tmp_path)
    copy = folder / "copy.png"
    shutil.copy2(original, copy)
    (folder / "copy.txt").write_text("prompt")
    (folder / "copy.png.json").write_text("{}")
    _rescan()

    row = next(
        item
        for item in image_store.search(include_missing=False, limit=500)[0]
        if item["absolute_path"] == str(copy)
    )
    result = duplicates.delete_files([row["id"]])

    assert result["deleted"] == 1 and result["sidecars"] == 2
    assert not copy.exists()
    assert not (folder / "copy.txt").exists() and not (folder / "copy.png.json").exists()
    assert image_store.get(row["id"]) is None
    assert original.exists(), "only the one that was asked for"


def test_delete_plan_lists_every_image_and_sidecar_without_truncation(
    client, scanned, tmp_path
):
    original = Path(image_store.get(scanned[0])["absolute_path"])
    folder = _second_root(tmp_path)
    images = []
    expected_paths = []
    for index in range(11):
        image = folder / f"copy-{index}.png"
        prompt = folder / f"copy-{index}.txt"
        metadata = folder / f"copy-{index}.png.json"
        shutil.copy2(original, image)
        prompt.write_text("prompt")
        metadata.write_text("{}")
        images.append(image)
        expected_paths.extend((str(image), str(metadata), str(prompt)))
    _rescan()
    image_ids = [image_store.get_by_path(str(path))["id"] for path in images]

    response = client.post(
        "/api/images/delete-plan",
        json={"image_ids": image_ids, "with_sidecars": True},
    )

    assert response.status_code == 200
    assert response.json() == {"paths": expected_paths}


def test_sidecar_unlink_failure_is_reported_after_the_image_is_deleted(
    client, scanned, tmp_path, monkeypatch
):
    original = Path(image_store.get(scanned[0])["absolute_path"])
    folder = _second_root(tmp_path)
    copy = folder / "copy.png"
    blocked_sidecar = folder / "copy.png.json"
    removable_sidecar = folder / "copy.txt"
    shutil.copy2(original, copy)
    blocked_sidecar.write_text("{}")
    removable_sidecar.write_text("prompt")
    _rescan()
    row = image_store.get_by_path(str(copy))
    real_unlink = Path.unlink

    def fail_one_sidecar(path: Path, *args, **kwargs) -> None:
        if path == blocked_sidecar:
            raise PermissionError("sidecar is read-only")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_one_sidecar)

    result = duplicates.delete_files([row["id"]])

    assert result == {
        "deleted": 1,
        "sidecars": 1,
        "failed": [
            {"path": str(blocked_sidecar), "reason": "sidecar is read-only"}
        ],
    }
    assert not copy.exists()
    assert blocked_sidecar.exists()
    assert not removable_sidecar.exists()
    assert image_store.get(row["id"]) is None


def test_the_usage_history_outlives_a_deleted_file(client, scanned, tmp_path):
    """That memory is the whole point of the duplicate warning."""
    from backend.store import usage

    image = image_store.get(scanned[0])
    usage.record(
        sha256=image["sha256"],
        phash=image["phash"],
        post_id=None,
        remote_post_id=4242,
        source_path=image["absolute_path"],
        status=usage.PUBLISHED,
    )

    duplicates.delete_files([image["id"]])

    assert image_store.get(image["id"]) is None
    assert usage.check(image["sha256"], image["phash"])["has_exact"]


def test_a_file_outside_the_configured_folders_is_refused(client, scanned, tmp_path):
    """A stale row must not turn this into a general file remover."""
    from backend import db

    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(b"x")
    image = image_store.get(scanned[0])
    with db.transaction() as conn:
        conn.execute(
            "UPDATE images SET absolute_path=? WHERE id=?", (str(outside), image["id"])
        )

    result = duplicates.delete_files([image["id"]])

    assert result["deleted"] == 0 and len(result["failed"]) == 1
    assert outside.exists()
    assert image_store.get(image["id"]) is not None


def test_trashing_a_library_selection_moves_only_the_selected_image_and_keeps_usage_untouched(
    client, scanned, fixture_images
):
    folder, _ = fixture_images
    original = Path(image_store.get(scanned[0])["absolute_path"])
    prompt = original.with_suffix(".txt")
    prompt.write_text("keep with the image")
    trash = folder / "trash"
    usage.record(
        sha256=image_store.get(scanned[0])["sha256"],
        phash=image_store.get(scanned[0])["phash"],
        post_id=None,
        remote_post_id=4242,
        source_path=str(original),
        status=usage.PUBLISHED,
    )
    before_usage = [
        tuple(row)
        for row in db.get_connection().execute("SELECT * FROM image_usage ORDER BY id")
    ]

    settings = client.put("/api/settings", json={"trash_folder": str(trash)})
    response = client.post(
        "/api/images/trash",
        json={"image_ids": [scanned[0]], "with_sidecars": True},
    )
    job = _wait_for_job(client, response.json())

    assert settings.status_code == 200
    assert job["status"] == "done"
    assert job["result"] == {
        "moved": 1,
        "sidecars": 1,
        "failed": [],
        "post_bound_image_ids": [],
        "unstarted_image_ids": [],
    }
    row = image_store.get(scanned[0])
    moved = Path(row["absolute_path"])
    assert row["is_trashed"] is True
    assert row["trash_original_path"] == str(original)
    assert moved.parent == trash / folder.name and moved.exists() and not original.exists()
    assert (trash / folder.name / prompt.name).read_text() == "keep with the image"
    assert scanned[0] not in {
        item["id"] for item in client.get("/api/images").json()["items"]
    }
    assert [item["id"] for item in client.get("/api/trash").json()["items"]] == [
        scanned[0]
    ]
    assert image_store.get(scanned[1])["is_trashed"] is False
    assert client.get("/api/duplicates/trash").status_code == 404
    after_usage = [
        tuple(row)
        for row in db.get_connection().execute("SELECT * FROM image_usage ORDER BY id")
    ]
    assert after_usage == before_usage


def test_trash_job_returns_before_the_batch_and_reports_monotonic_progress(
    client, scanned, fixture_images, monkeypatch
):
    folder, _ = fixture_images
    trash = folder / "trash"
    client.put("/api/settings", json={"trash_folder": str(trash)})
    originals = [Path(image_store.get(image_id)["absolute_path"]) for image_id in scanned]
    release = threading.Semaphore(0)
    entered = threading.Event()
    real_move = duplicates.relocate.move_image

    def held_move(*args, **kwargs):
        entered.set()
        assert release.acquire(timeout=5)
        return real_move(*args, **kwargs)

    monkeypatch.setattr(duplicates.relocate, "move_image", held_move)

    started = client.post("/api/images/trash", json={"image_ids": scanned}).json()

    assert started["kind"] == "trash"
    assert started["status"] in ("starting", "running")
    assert entered.wait(timeout=2)
    current = client.get(f"/api/jobs/{started['id']}").json()
    assert current["status"] == "running"
    assert current["total"] == len(scanned)
    assert current["processed"] == 0
    assert current["stage"] == originals[0].name
    assert all(path.exists() for path in originals)

    observed = [(current["processed"], current["total"])]
    for expected in range(1, len(scanned) + 1):
        release.release()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = client.get(f"/api/jobs/{started['id']}").json()
            if current["processed"] >= expected:
                break
            time.sleep(0.01)
        observed.append((current["processed"], current["total"]))

    finished = _wait_for_job(client, current)

    assert finished["status"] == "done"
    assert finished["succeeded"] == len(scanned)
    assert finished["failed"] == 0
    assert observed == sorted(observed)
    assert {total for _, total in observed} == {len(scanned)}
    assert [processed for processed, _ in observed] == list(range(len(scanned) + 1))


def test_trash_job_records_a_failure_and_moves_the_independent_file(
    client, scanned, fixture_images, monkeypatch
):
    folder, _ = fixture_images
    trash = folder / "trash"
    client.put("/api/settings", json={"trash_folder": str(trash)})
    failed_id, moved_id = scanned
    failed_path = Path(image_store.get(failed_id)["absolute_path"])
    moved_path = Path(image_store.get(moved_id)["absolute_path"])
    real_move = duplicates.relocate.move_image

    def fail_one(image_id, *args, **kwargs):
        if image_id == failed_id:
            raise OSError("simulated move failure")
        return real_move(image_id, *args, **kwargs)

    monkeypatch.setattr(duplicates.relocate, "move_image", fail_one)

    started = client.post("/api/images/trash", json={"image_ids": scanned}).json()
    finished = _wait_for_job(client, started)

    assert finished["status"] == "done"
    assert (finished["processed"], finished["total"]) == (2, 2)
    assert (finished["succeeded"], finished["failed"]) == (1, 1)
    assert finished["result"]["moved"] == 1
    assert finished["result"]["failed"] == [
        {
            "image_id": failed_id,
            "path": str(failed_path),
            "reason": "simulated move failure",
        }
    ]
    assert [item["status"] for item in finished["items"]] == ["error", "ok"]
    assert failed_path.exists()
    assert not moved_path.exists()
    assert image_store.get(failed_id)["is_trashed"] is False
    assert image_store.get(moved_id)["is_trashed"] is True


def test_cancelling_a_trash_job_stops_before_the_next_file_and_keeps_the_move_restorable(
    client, scanned, fixture_images, monkeypatch
):
    folder, _ = fixture_images
    trash = folder / "trash"
    client.put("/api/settings", json={"trash_folder": str(trash)})
    first_id, second_id = scanned
    first_path = Path(image_store.get(first_id)["absolute_path"])
    second_path = Path(image_store.get(second_id)["absolute_path"])
    first_logged = threading.Event()
    continue_worker = threading.Event()
    real_log = jobs.Job.log

    def pause_after_first_move(self, **entry):
        real_log(self, **entry)
        if self.kind == "trash" and entry.get("status") == "ok" and not first_logged.is_set():
            first_logged.set()
            assert continue_worker.wait(timeout=5)

    monkeypatch.setattr(jobs.Job, "log", pause_after_first_move)

    started = client.post("/api/images/trash", json={"image_ids": scanned}).json()
    assert first_logged.wait(timeout=2)
    current = client.get(f"/api/jobs/{started['id']}").json()
    assert current["processed"] == 1
    assert client.post(f"/api/jobs/{started['id']}/cancel").json()["ok"] is True
    continue_worker.set()
    finished = _wait_for_job(client, current)

    assert finished["status"] == "cancelled"
    assert (finished["processed"], finished["total"]) == (1, 2)
    assert (finished["succeeded"], finished["failed"]) == (1, 0)
    assert finished["result"]["unstarted_image_ids"] == [second_id]
    assert not first_path.exists()
    assert second_path.exists()
    assert image_store.get(first_id)["is_trashed"] is True
    assert image_store.get(second_id)["is_trashed"] is False

    restored = duplicates.restore_from_trash([first_id])

    assert restored["restored"] == 1
    assert first_path.exists()


def test_a_rescan_never_reimports_or_marks_a_trashed_file_missing(
    client, scanned, fixture_images
):
    folder, _ = fixture_images
    trash = folder / "global-trash"
    client.put("/api/settings", json={"trash_folder": str(trash)})
    started = client.post("/api/images/trash", json={"image_ids": [scanned[0]]}).json()
    _wait_for_job(client, started)
    before = image_store.get(scanned[0])
    before_count = db.get_connection().execute("SELECT COUNT(*) FROM images").fetchone()[0]
    source = source_store.list_roots()[0]
    assert trash.name not in source["excluded_folders"]
    # The live trash path stays out of scan input independently of the user's
    # name-based folder exclusions.
    with db.transaction() as conn:
        conn.execute("UPDATE source_roots SET excluded_folders='[]' WHERE id=?", (source["id"],))

    _rescan()

    after = image_store.get(scanned[0])
    assert db.get_connection().execute("SELECT COUNT(*) FROM images").fetchone()[0] == before_count
    assert after["absolute_path"] == before["absolute_path"]
    assert after["is_trashed"] is True and after["is_missing"] is False


def test_restore_puts_the_image_and_sidecars_back_at_their_exact_paths(
    client, scanned, fixture_images
):
    folder, _ = fixture_images
    original = Path(image_store.get(scanned[0])["absolute_path"])
    sidecar = original.with_name(f"{original.name}.json")
    sidecar.write_text('{"workflow": true}')
    trash = folder / "trash"
    client.put("/api/settings", json={"trash_folder": str(trash)})
    started = client.post("/api/images/trash", json={"image_ids": [scanned[0]]}).json()
    _wait_for_job(client, started)

    response = client.post("/api/images/restore", json={"image_ids": [scanned[0]]})

    assert response.json() == {"restored": 1, "sidecars": 1, "failed": []}
    row = image_store.get(scanned[0])
    assert row["absolute_path"] == str(original)
    assert row["is_trashed"] is False
    assert row["trash_original_path"] is None and row["trashed_at"] is None
    assert original.exists() and sidecar.read_text() == '{"workflow": true}'
    assert scanned[0] in {item["id"] for item in client.get("/api/images").json()["items"]}
    assert client.get("/api/trash").json()["items"] == []


def test_trash_groups_one_source_folder_and_separates_matching_folder_names(
    client, fixture_images, tmp_path
):
    """The database-recorded original paths choose the readable trash folder."""
    source_a = tmp_path / "set-a" / "output"
    source_b = tmp_path / "set-b" / "output"
    source_a.mkdir(parents=True)
    source_b.mkdir(parents=True)
    first, second = fixture_images[1]
    same_folder = source_a / "same-folder.png"
    other_folder = source_a / "other-folder.png"
    colliding_folder = source_b / "other-folder.png"
    shutil.copy2(first, same_folder)
    shutil.copy2(second, other_folder)
    shutil.copy2(first, colliding_folder)
    source_store.add_root(str(source_a.parent), label="Set A")
    source_store.add_root(str(source_b.parent), label="Set B")
    _rescan()
    image_ids = {
        path: db.get_connection()
        .execute("SELECT id FROM images WHERE absolute_path=?", (str(path),))
        .fetchone()["id"]
        for path in (same_folder, other_folder, colliding_folder)
    }
    trash = tmp_path / "trash"
    client.put("/api/settings", json={"trash_folder": str(trash)})

    result = duplicates.move_to_trash(
        [image_ids[same_folder], image_ids[other_folder], image_ids[colliding_folder]]
    )

    assert result == {
        "moved": 3,
        "sidecars": 0,
        "failed": [],
        "post_bound_image_ids": [],
        "unstarted_image_ids": [],
    }
    assert (trash / "output" / same_folder.name).exists()
    assert (trash / "output" / other_folder.name).exists()
    assert (trash / "set-b_output" / colliding_folder.name).exists()
    restored = duplicates.restore_from_trash(list(image_ids.values()))
    assert restored == {"restored": 3, "sidecars": 0, "failed": []}
    assert all(path.exists() for path in image_ids)


def test_trash_keeps_a_name_collision_and_its_sidecar_together(
    client, scanned, fixture_images
):
    folder, _ = fixture_images
    original = Path(image_store.get(scanned[0])["absolute_path"])
    original_bytes = original.read_bytes()
    sidecar = original.with_suffix(".txt")
    sidecar.write_text("move with the renamed image")
    trash = folder / "trash"
    target_folder = trash / folder.name
    target_folder.mkdir(parents=True)
    (target_folder / original.name).write_bytes(b"already here")
    client.put("/api/settings", json={"trash_folder": str(trash)})

    result = duplicates.move_to_trash([scanned[0]])

    moved = Path(image_store.get(scanned[0])["absolute_path"])
    assert result == {
        "moved": 1,
        "sidecars": 1,
        "failed": [],
        "post_bound_image_ids": [],
        "unstarted_image_ids": [],
    }
    assert moved == target_folder / f"{original.stem}-1{original.suffix}"
    assert moved.read_bytes() == original_bytes
    assert (target_folder / f"{original.stem}-1.txt").read_text() == "move with the renamed image"


def test_restore_refuses_to_overwrite_a_new_file_at_the_original_path(
    client, scanned, fixture_images
):
    folder, _ = fixture_images
    original = Path(image_store.get(scanned[0])["absolute_path"])
    trash = folder / "trash"
    client.put("/api/settings", json={"trash_folder": str(trash)})
    started = client.post("/api/images/trash", json={"image_ids": [scanned[0]]}).json()
    _wait_for_job(client, started)
    moved = Path(image_store.get(scanned[0])["absolute_path"])
    original.write_bytes(b"new file")

    result = client.post("/api/images/restore", json={"image_ids": [scanned[0]]}).json()

    assert result["restored"] == 0 and len(result["failed"]) == 1
    assert original.read_bytes() == b"new file"
    assert moved.exists() and image_store.get(scanned[0])["is_trashed"] is True


def test_the_delete_endpoint_never_deletes_from_the_trash(
    client, scanned, fixture_images
):
    folder, _ = fixture_images
    trash = folder / "trash"
    client.put("/api/settings", json={"trash_folder": str(trash)})
    started = client.post("/api/images/trash", json={"image_ids": [scanned[0]]}).json()
    _wait_for_job(client, started)
    moved = Path(image_store.get(scanned[0])["absolute_path"])

    result = client.post(
        "/api/images/delete", json={"image_ids": [scanned[0]]}
    ).json()

    assert result["deleted"] == 0 and len(result["failed"]) == 1
    assert moved.exists() and image_store.get(scanned[0])["is_trashed"] is True


def test_filename_generation_time_orders_a_group_only_when_every_member_has_one(
    client, scanned, tmp_path
):
    original = Path(image_store.get(scanned[0])["absolute_path"])
    folder = _second_root(tmp_path)
    later = folder / "20260819200315--1801438905.png"
    earlier = folder / "20260818200315--1801438905.png"
    shutil.copy2(original, later)
    shutil.copy2(original, earlier)
    _rescan()
    group = next(group for group in duplicates.exact_groups() if len(group["members"]) == 3)
    ids = {Path(member["absolute_path"]).name: member["id"] for member in group["members"]}
    with db.transaction() as conn:
        conn.execute(
            "UPDATE images SET first_seen_at='2026-01-01T00:00:00Z' WHERE id=?",
            (ids[original.name],),
        )
        conn.execute(
            "UPDATE images SET first_seen_at='2026-01-03T00:00:00Z' WHERE id=?",
            (ids[earlier.name],),
        )
        conn.execute(
            "UPDATE images SET first_seen_at='2026-01-02T00:00:00Z' WHERE id=?",
            (ids[later.name],),
        )

    fallback = next(group for group in duplicates.exact_groups() if len(group["members"]) == 3)
    assert fallback["first_seen_by"] == "library_entry"
    assert fallback["members"][0]["id"] == ids[original.name]

    renamed = original.with_name("20260820200315--1801438905.png")
    original.rename(renamed)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE images SET absolute_path=?, relative_path=? WHERE id=?",
            (str(renamed), renamed.name, ids[original.name]),
        )
    timed = next(group for group in duplicates.exact_groups() if len(group["members"]) == 3)
    assert timed["first_seen_by"] == "generation_time"
    assert [Path(member["absolute_path"]).name for member in timed["members"]] == [
        earlier.name,
        later.name,
        renamed.name,
    ]


def test_generation_time_patterns_are_validated_and_have_the_documented_default(client):
    settings = client.get("/api/settings").json()
    assert settings["generation_time_patterns"] == ["yyyymmddHHMMSS--seed"]

    invalid = client.put(
        "/api/settings", json={"generation_time_patterns": ["yyyy-mm-dd"]}
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "invalid_generation_time_pattern"


def test_schema_eighteen_migrates_existing_image_rows_without_losing_them(client, scanned):
    conn = db.get_connection()
    before = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    conn.execute("DROP INDEX idx_images_trash")
    conn.execute("ALTER TABLE images DROP COLUMN trashed_at")
    conn.execute("ALTER TABLE images DROP COLUMN trash_original_path")
    conn.execute("ALTER TABLE images DROP COLUMN is_trashed")
    conn.execute("PRAGMA user_version=17")
    conn.commit()

    db.init_db()

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(images)")}
    assert {"is_trashed", "trash_original_path", "trashed_at"} <= columns
    assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == before
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_the_trash_folder_grows_leftwards_until_it_separates(tmp_path, monkeypatch):
    """Mirrored trees share their last two components, and that is where
    duplicates come from - so one extra level is not always enough."""
    from backend import duplicates

    trash = tmp_path / "trash"
    origins: dict[Path, set[Path]] = {}

    first = tmp_path / "x" / "gen" / "output" / "img.png"
    second = tmp_path / "y" / "gen" / "output" / "img.png"

    one = duplicates._trash_target(trash, first, origins)
    assert one.parent == trash / "output"
    origins.setdefault(one.parent, set()).add(first.parent)

    two = duplicates._trash_target(trash, second, origins)
    assert two.parent == trash / "gen_output"
    origins.setdefault(two.parent, set()).add(second.parent)

    third = tmp_path / "z" / "gen" / "output" / "img.png"
    assert duplicates._trash_target(trash, third, origins).parent.name.endswith("gen_output")
    assert duplicates._trash_target(trash, third, origins).parent not in (
        trash / "output",
        trash / "gen_output",
    )

    # The same folder twice keeps one subfolder; `relocate` numbers the file.
    again = tmp_path / "x" / "gen" / "output" / "other.png"
    assert duplicates._trash_target(trash, again, origins).parent == trash / "output"
