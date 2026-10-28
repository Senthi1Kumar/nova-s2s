from fastapi.testclient import TestClient

from aibox.app import create_app
from aibox.backends.post.facetrack import FaceSmoother


def face(x):
    return {"class": "face", "confidence": 0.9, "bbox": [x, 10, x + 100, 130],
            "landmarks": [[x + 30, 40], [x + 70, 40], [x + 50, 70], [x + 35, 100], [x + 65, 100]]}


def test_smoother_blends_and_drops_track():
    s = FaceSmoother(alpha=0.5)
    a = s.update(face(100))
    b = s.update(face(120))
    assert a["bbox"][0] == 100.0 and b["bbox"][0] == 110.0
    assert len(b["landmarks"]) == 5
    held = s.update(None)
    assert held["held"] is True
    for _ in range(4):
        out = s.update(None)
    assert out is None


def test_smoother_resets_on_jump():
    s = FaceSmoother(alpha=0.5)
    s.update(face(100))
    assert s.update(face(400))["bbox"][0] == 400.0


def test_face_track_stream(services):
    client = TestClient(create_app(services))
    with client.websocket_connect('/v1/vision/stream') as ws:
        ws.send_json({"source": "f455", "task": "face_track", "fps": 10, "preview": "none"})
        msg = ws.receive_json()
        assert msg["type"] == "result" and msg["task"] == "face_track"
        face = msg["results"]["face"]
        assert len(face["bbox"]) == 4 and len(face["landmarks"]) == 5
        assert msg["results"]["detections"][0]["bbox"] == face["bbox"]
