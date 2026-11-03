"""The web Studio is plain static files served by create_app()."""

from pathlib import Path
import shutil
import subprocess

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


def test_voice_reports_actual_browser_audio_processing(client):
    src = client.get("/js/voice.js").text
    assert "getSettings" in src
    assert "audio_processing" in src
    assert "echoCancellation" in src and "noiseSuppression" in src


def test_voice_protocol_two_client_streams_sentence_pcm(client):
    src = client.get("/js/voice.js").text
    for needle in ("transcript_partial", "tts_start", "tts_end", "interrupted", "ignored", "turn_id", "sentence_seq"):
        assert f'"{needle}"' in src or f"'{needle}'" in src or needle in src, needle
    for event in ("tts_start", "tts_end", "interrupted", "ignored"):
        assert f"'{event}'" in src
    # raw PCM is scheduled on the AudioContext clock, never assembled into a WAV
    assert "createBuffer(1" in src and "createBufferSource" in src and "playHead" in src
    assert "CHUNK_BYTES = 3200" in src and "checkTurnEnd" in src
    for gone in ("wavTotal", "maybePlay", "playQueue", "createObjectURL(new Blob(next", "createMediaElementSource"):
        assert gone not in src, gone


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_voice_queued_audio_does_not_get_another_jitter_lead():
    script = f"""
        import assert from 'node:assert/strict';
        globalThis.location = new URL('http://127.0.0.1:8442');
        const voice = await import({str((STATIC / 'js' / 'voice.js').as_uri())!r});
        assert.equal(typeof voice.playbackStartTime, 'function');
        assert.equal(voice.playbackStartTime(0, 10), 10.8);
        assert.equal(voice.playbackStartTime(12, 10), 12);
        assert.equal(voice.playbackStartTime(10.5, 10), 10.5);
        assert.equal(voice.playbackStartTime(9.5, 10), 10.8);
    """
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_voice_accepts_unknown_streaming_sentence_total(client):
    src = client.get("/js/voice.js").text
    assert "expect: m.bytes || 0" in src
    assert "V.sent.expect && V.sent.got !== V.sent.expect" in src
    assert "m.bytes != null && m.bytes !== V.sent.got" in src


def test_voice_prefers_xvf3800_when_browser_exposes_it(client):
    src = client.get("/js/voice.js").text
    assert "XVF3800" in src
    assert "reSpeaker" in src
    assert "deviceId: { exact:" in src
    assert "setSinkId" in src


def test_xvf_capture_uses_onboard_dsp_without_browser_processing(client):
    src = client.get("/js/voice.js").text
    xvf_capture = src.split("const input =", 1)[1].split("const output", 1)[0]
    assert "echoCancellation: false" in xvf_capture
    assert "noiseSuppression: false" in xvf_capture


def test_voice_barge_in_toggle_is_opt_in_and_persisted(client):
    src = client.get("/js/voice.js").text
    assert "Barge-in (beta)" in src and "vo-barge" in src
    assert "bargeIn: false" in src and "barge_in: true" in src
    assert "localStorage.getItem(BARGE_KEY)" in src and "localStorage.setItem(BARGE_KEY" in src
    assert src.count("localStorage") == 2 and src.count("catch") >= 2


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
