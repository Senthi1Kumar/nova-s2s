"""The web Studio is plain static files served by create_app()."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aibox.app import create_app

STATIC = Path(__file__).resolve().parents[1] / "studio_web"


@pytest.fixture
def client(services):
    with TestClient(create_app(services, static_dir=STATIC)) as c:
        yield c


def test_index_has_brand_and_nav(client):
    html = client.get("/").text
    assert "AI BOX" in html
    for label in ("Overview", "Voice AI", "Computer Vision", "Chat & VLM", "Device Status", "Inspector"):
        assert label in html


def test_overview_counters(client):
    html = client.get("/").text
    for label in ("AI Services", "API Endpoints", "Clients", "Requests / Sec"):
        assert label in html


@pytest.mark.parametrize("name", ["api", "app", "state", "overview", "device", "inspector", "vision", "chat", "voice", "avatar", "face"])
def test_modules_served(client, name):
    r = client.get(f"/js/{name}.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_api_helpers_exported(client):
    src = client.get("/js/api.js").text
    for sym in ("apiBase", "getJSON", "postJSON", "openWS", "sse"):
        assert f"export" in src and sym in src
    assert "aiboxKey" in src


def test_no_external_hosts():
    for p in list(STATIC.rglob("*.js")) + list(STATIC.rglob("*.html")) + list(STATIC.rglob("*.css")):
        text = p.read_text()
        assert "https://cdn" not in text and "//fonts.googleapis" not in text, p


def test_api_still_routed(client):
    assert client.get("/health").json()["status"] == "ok"
    ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
    assert "qwen3:1.7b" in ids


def test_vision_chat_voice_use_contract_routes(client):
    assert "/v1/vision/stream" in client.get("/js/vision.js").text
    assert "/v1/vision/ask" in client.get("/js/vision.js").text
    assert "/v1/chat/completions" in client.get("/js/chat.js").text
    assert "/v1/voice/stream" in client.get("/js/voice.js").text


def test_voice_protocol_and_controls(client):
    src = client.get("/js/voice.js").text
    for needle in ("type: 'cancel'", "AudioWorklet", "down16", "Unmute", "isSecureContext", "/v1/models"):
        assert needle in src or needle.replace("'", '"') in src, needle
    assert "qwen3:1.7b" in src
    assert "transcript" in src and "token" in src and "timings" in src


def test_avatar_matches_lvgl_states(client):
    src = client.get("/js/avatar.js").text
    for state in ("idle", "listening", "thinking", "speaking", "muted"):
        assert f"'{state}'" in src
    assert "hue: 258" in src and "hue: 164" in src   # thinking / speaking colours from avatar_model.c


def test_vision_and_face_stop_clear_view(client):
    v = client.get("/js/vision.js").text
    assert "Stopped — press Start" in v and "clearView" in v
    f = client.get("/js/face.js").text
    assert "Stopped — press Start" in f and "stopPreview" in f


def test_default_model_is_qwen3_vl(client):
    assert "DEFAULT_MODEL = 'qwen3-vl'" in client.get("/js/voice.js").text
    assert "'qwen3-vl'" in client.get("/js/chat.js").text


def test_vision_video_sources_and_dms(client):
    v = client.get("/js/vision.js").text
    for needle in ("/v1/vision/videos", "Upload video", "video_id", "video_t", "'loading'", "dms.summary".split(".")[1],
                   "phone_in_hand", "eyes_not_visible", "eyes_visible"):
        assert needle in v, needle
    assert "dms-summary" in client.get("/styles.css").text
