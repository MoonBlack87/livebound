"""Source-folder deletion previews and removes only database state."""

from backend.store import edits as edit_store
from backend.store import images as image_store
from backend.store import sources as source_store


def _add_image(root_id: int, path) -> int:
    return image_store.upsert(
        root_id,
        {
            "absolute_path": str(path),
            "relative_path": path.name,
        },
    )


def test_deletion_impact_counts_images_edits_and_resource_overrides(client, tmp_path):
    source = tmp_path / "with-library-state"
    source.mkdir()
    root = source_store.add_root(str(source))
    first = _add_image(root["id"], source / "first.png")
    _add_image(root["id"], source / "second.png")

    edit_store.save(
        first,
        draft={"prompt": "changed", "negative_prompt": None, "fields": {}},
        touched=["prompt"],
        deleted=[],
    )
    image_store.replace_resources(
        first,
        [
            {"resource_type": "lora", "name_in_prompt": "kept"},
            {"resource_type": "lora", "name_in_prompt": "derived-only"},
        ],
    )
    kept = next(
        resource for resource in image_store.resources_for(first)
        if resource["name_in_prompt"] == "kept"
    )
    image_store.update_resource(kept["id"], {"locked_by_user": 1})

    response = client.get(f"/api/sources/{root['id']}/delete-impact")

    assert response.status_code == 200
    assert response.json() == {
        "images": 2,
        "edits": 1,
        "resource_overrides": 1,
    }


def test_empty_source_has_zero_impact_and_can_still_be_deleted(client, tmp_path):
    source = tmp_path / "empty"
    source.mkdir()
    root = source_store.add_root(str(source))

    response = client.get(f"/api/sources/{root['id']}/delete-impact")

    assert response.status_code == 200
    assert response.json() == {
        "images": 0,
        "edits": 0,
        "resource_overrides": 0,
    }

    deleted = client.delete(f"/api/sources/{root['id']}")

    assert deleted.status_code == 200
    assert source_store.get_root(root["id"]) is None
