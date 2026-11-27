import io
import time

import httpx
import pytest
from PIL import Image

from aether.database import Database
from aether.metadata import Metadata, MetadataError, rank, save_image
from aether.monitor import scan


class FixtureKey:
    key = "test-fixture"


def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (32, 24), color="teal").save(stream, "PNG")
    return stream.getvalue()


def setup_games(tmp_path, copies=1):
    db = Database(tmp_path / "Data")
    for i in range(copies):
        root = tmp_path / f"Games{i}"
        game = root / "Orbit"
        game.mkdir(parents=True)
        (game / "Orbit.exe").write_bytes(b"Game")
        db.add_folder(root)
    for folder in db.folders():
        scan(db, folder)
    return db


def test_search_match_art_offline_and_merge(tmp_path):
    db = setup_games(tmp_path, 2)
    calls = []

    def handle(req):
        calls.append(req)
        if req.url.host == "media.rawg.io":
            assert "key" not in req.url.params
            return httpx.Response(200, content=image_bytes())
        assert req.url.params["key"] == "test-fixture"
        hit = {
            "id": 42,
            "name": "Orbit",
            "released": "2022-05-06",
            "platforms": [{"platform": {"name": "PC"}}],
        }
        if req.url.path == "/api/games":
            return httpx.Response(200, json={"results": [hit]})
        if req.url.path.endswith("/screenshots"):
            return httpx.Response(200, json={"results": [{"image": "https://media.rawg.io/shots/1.jpg"}]})
        return httpx.Response(
            200,
            json=hit
            | {
                "slug": "orbit",
                "description_raw": "A space game.",
                "background_image": "https://media.rawg.io/cover.jpg",
            },
        )

    meta = Metadata(db, FixtureKey(), httpx.MockTransport(handle))
    a, b = db.library()
    db.edit_game(a["id"], {"title": "Custom Orbit"}, ["Space"], ["To play"])
    assert meta.search("Orbit")[0]["score"] == 100
    assert meta.match(a["id"], 42) == (a["id"], 0)
    db.favorite(b["id"])
    assert meta.match(b["id"], 42) == (a["id"], 0)
    (game,) = db.library()
    assert game["copies"] == 2 and game["favorite"] and game["tags"] == ["Space"]
    assert game["title"] == "Custom Orbit" and game["description"] == "A space game."
    assert {a["kind"] for a in db.gallery(game["id"])} == {"background", "screenshot"}
    assert (db.art / game["cover"]).is_file()
    meta.stop()

    def offline(_):
        raise AssertionError("Fresh cached data should be available entirely offline")

    class NoKey:
        key = ""

    cached = Metadata(Database(db.home), NoKey(), httpx.MockTransport(offline))
    assert cached.search("Orbit")[0]["id"] == 42
    assert cached.match(game["id"], 42) == (game["id"], 0)
    assert len(calls) == 5
    cached.stop()


def test_temporary_network_errors_preserve_local_library_and_retry(tmp_path):
    db = setup_games(tmp_path)
    count = []

    def limited(req):
        count.append(1)
        return httpx.Response(429, headers={"Retry-After": "90"})

    m = Metadata(db, FixtureKey(), httpx.MockTransport(limited))
    with pytest.raises(MetadataError, match="limit"):
        m.search("Orbit")
    with pytest.raises(MetadataError, match="pause"):
        m.search("Orbit")
    assert len(count) == 1 and m.retry_after >= time.time() + 80
    assert db.library()[0]["title"] == "Orbit"
    m.stop()


def test_matching_and_image_validation(tmp_path):
    hits = rank(
        "Orbit II",
        [
            {"id": 1, "name": "Orbit"},
            {"id": 2, "name": "Orbit II", "platforms": [{"platform": {"name": "PC"}}]},
        ],
    )
    assert [h["id"] for h in hits] == [2, 1]
    file = save_image(image_bytes(), tmp_path)
    assert save_image(image_bytes(), tmp_path) == file
    with Image.open(tmp_path / file) as im:
        assert im.format == "WEBP" and im.size == (32, 24)
    with pytest.raises(MetadataError):
        save_image(b"not an image", tmp_path)
