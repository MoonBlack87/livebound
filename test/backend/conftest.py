"""Test fixtures.

Every test gets its own data directory via the environment override, so the
suite never touches the real profile database or thumbnail cache.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "metadata"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVEBOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    # Otherwise tests that fetch images download into the real project folder -
    # it happened once, which is why this is here.
    monkeypatch.setenv("LIVEBOUND_PENDING_DIR", str(tmp_path / "pending"))

    from backend import config

    # The installation-local pointer is written relative to this, so a test that
    # calls `set_data_dir` without redirecting it writes into the maintainer's
    # own `.livebound/config.json` and sends his next start at a pytest temp
    # directory. That happened, repeatedly, and it is the same class of mistake
    # as the pending directory above: the default is the real thing, so the
    # default has to be moved for every test rather than by each test.
    monkeypatch.setattr(config, "_APPLICATION_ROOT", tmp_path / "installation")
    monkeypatch.setattr(config, "_pointer", None)
    monkeypatch.setattr(config, "_pointer_read", False)

    from backend import db

    # The connection is thread-local and cached; a stale one would point at the
    # previous test's database.
    db.close_connection()
    if hasattr(db._local, "conn"):
        delattr(db._local, "conn")
    db.init_db()
    yield tmp_path
    db.close_connection()
    # The HTTP pool is process-wide, so a test that put a stand-in in it must
    # not leave it there for the next one.
    from backend.civitai import client as civitai_client

    civitai_client.close_http_client()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from backend.main import create_app

    return TestClient(create_app())


@pytest.fixture
def fixture_images(tmp_path):
    """Small pictures carrying the two real generated infotext fixtures."""
    from PIL import Image, PngImagePlugin

    target = tmp_path / "images"
    target.mkdir()
    copied = []
    for index, source in enumerate(sorted(FIXTURE_DIR.glob("*.txt"))):
        destination = target / f"{source.stem}.png"
        width, height = 96 + index * 8, 128 + index * 8
        image = Image.new("RGB", (width, height))
        for y in range(height):
            for x in range(width):
                image.putpixel(
                    (x, y),
                    (
                        (x * (3 + index) + y) % 256,
                        (y * (5 + index) + x // 2) % 256,
                        (x * y + 40 * index) % 256,
                    ),
                )
        info = PngImagePlugin.PngInfo()
        info.add_text("parameters", source.read_text(encoding="utf-8").strip())
        image.save(destination, "PNG", pnginfo=info)
        copied.append(destination)
    if not copied:
        pytest.fail("Metadata text fixtures are missing")
    return target, copied


@pytest.fixture
def scanned(client, fixture_images):
    """A source folder that has been scanned, with its image ids."""
    folder, _ = fixture_images
    client.post("/api/sources", json={"path": str(folder), "label": "Test"})

    from backend import jobs
    from backend.scanner import service

    job = jobs.Job(id=0, kind="scan")
    service.scan_roots(job)
    images = client.get("/api/images").json()["items"]
    return [image["id"] for image in images]
