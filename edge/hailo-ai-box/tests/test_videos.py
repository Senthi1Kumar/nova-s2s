import dataclasses
import io
import os
import time

import cv2
import numpy as np
import pytest
from starlette.testclient import TestClient

from aibox import videos
from aibox.app import create_app
from aibox.fakes import make_fake_services


def make_video(path, frames=24, fps=12.0, size=(64, 48)):
    wr = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    for i in range(frames):
        wr.write(np.full((size[1], size[0], 3), (i * 8 % 255, 40, 90), np.uint8))
    wr.release()
    return path


@pytest.fixture
def lib_env(tmp_path, settings):
    presets, uploads = tmp_path / "presets", tmp_path / "uploads"
    presets.mkdir()
    make_video(presets / "road_1.mp4")
    os.symlink(tmp_path / "missing.mp4", presets / "road_9.mp4")  # dangling symlink is skipped
    s = dataclasses.replace(settings, video_dir=str(presets), upload_dir=str(uploads))
    return s, presets, uploads


@pytest.fixture
def client(lib_env):
    s = lib_env[0]
    with TestClient(create_app(make_fake_services(s))) as c:
        yield c


def test_list_presets(client):
    d = client.get("/v1/vision/videos").json()["data"]
    assert [v["id"] for v in d] == ["road_1"]
    v = d[0]
    assert v["source"] == "preset" and v["width"] == 64 and v["height"] == 48
    assert v["fps"] == 12.0 and v["duration_s"] == 2.0
    assert set(v) == {"id", "name", "duration_s", "width", "height", "fps", "source"}


def test_upload_then_list_and_reject_garbage(client, tmp_path):
    src = make_video(tmp_path / "clip.mp4")
    r = client.post("/v1/vision/videos", files={"file": ("my clip!.mp4", src.read_bytes(), "video/mp4")})
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["source"] == "upload" and item["id"].startswith("up_")
    ids = [v["id"] for v in client.get("/v1/vision/videos").json()["data"]]
    assert item["id"] in ids and "road_1" in ids
    bad = client.post("/v1/vision/videos", files={"file": ("x.mp4", b"not a video", "video/mp4")})
    assert bad.status_code == 415
    bad = client.post("/v1/vision/videos", files={"file": ("x.txt", b"hi", "text/plain")})
    assert bad.status_code == 415
    assert client.post("/v1/vision/videos", files={"nope": ("x.mp4", b"1")}).status_code == 400


def test_prune_removes_old_uploads_only(lib_env):
    s, presets, uploads = lib_env
    lib = videos.VideoLibrary(str(presets), str(uploads))
    uploads.mkdir()
    old, new = uploads / "up_old.mp4", uploads / "up_new.mp4"
    make_video(old)
    make_video(new)
    past = time.time() - 25 * 3600
    os.utime(old, (past, past))
    assert lib.prune() == 1
    assert not old.exists() and new.exists()
    assert (presets / "road_1.mp4").exists()


def test_stream_video_source_loops_and_reports_video_t(client):
    with client.websocket_connect("/v1/vision/stream") as ws:
        ws.send_json({"source": "video", "video_id": "road_1", "task": "detection", "fps": 12,
                      "preview": "none"})
        ts = []
        for _ in range(200):
            m = ws.receive_json()
            if m["type"] == "result":
                ts.append(m["video_t"])
            if len(ts) >= 30:
                break
    assert len(ts) >= 30
    assert ts[0] == 0.0
    assert any(b < a for a, b in zip(ts, ts[1:])), f"expected a loop restart in {ts}"  # 2 s clip


def test_stream_unknown_video_is_an_error(client):
    with client.websocket_connect("/v1/vision/stream") as ws:
        ws.send_json({"source": "video", "video_id": "nope", "task": "detection"})
        m = ws.receive_json()
        assert m["type"] == "error" and m["code"] == "bad_video"


async def test_play_paces_to_min_of_requested_and_video_fps(tmp_path):
    p = make_video(tmp_path / "a.mp4", frames=30, fps=30.0)
    got, t0 = [], time.monotonic()
    async for frame, t in videos.play(str(p), 5.0):
        got.append(t)
        if len(got) == 4:
            break
    assert got == [0.0, 0.2, 0.4, 0.6]  # every 6th frame of a 30 fps clip
    assert 0.5 < time.monotonic() - t0 < 1.5
