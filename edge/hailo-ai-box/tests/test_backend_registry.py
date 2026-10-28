import numpy as np

from aibox.backends.post.detection import letterbox, parse_nms_by_class
from aibox.backends.registry import ModelSpec, Registry
from aibox.inspector import Inspector


def _reg():
    ins = Inspector()
    reg = Registry(ins)
    reg.register(ModelSpec("yolov8m", "vision", "hailort", task="detection"))
    reg.register(ModelSpec("scrfd_10g", "vision", "hailort", task="dms"))
    return ins, reg


def test_failed_load_is_reported():
    ins, reg = _reg()

    def boom():
        raise RuntimeError("HAILO_INVALID_HEF(26)")

    assert reg.load("yolov8m", boom) is None
    item = next(i for i in reg.items() if i["id"] == "yolov8m")
    assert item["state"] == "failed" and "HAILO_INVALID_HEF(26)" in item["error"]
    ev = [e for e in ins.recent() if e.get("event") == "model_state"]
    assert ev[-1]["detail"]["state"] == "failed"
    # other models keep working
    assert reg.load("scrfd_10g", lambda: object()) is not None
    assert next(i for i in reg.items() if i["id"] == "scrfd_10g")["resident"] is True


def test_state_events_and_stats():
    ins, reg = _reg()
    reg.load("yolov8m", lambda: 1)
    states = [e["detail"]["state"] for e in ins.recent() if e.get("event") == "model_state"]
    assert states == ["loading", "resident"]
    for ms in (10, 20, 30, 40):
        reg.record_call("yolov8m", ms)
    st = next(s for s in reg.stats() if s["id"] == "yolov8m")
    assert st["requests"] == 4 and st["p50_ms"] in (20, 30) and st["p95_ms"] == 40


def test_nms_parse_maps_back_through_letterbox():
    img = np.zeros((360, 640, 3), np.uint8)
    canvas, s, px, py = letterbox(img, (640, 640))
    assert canvas.shape == (640, 640, 3) and s == 1.0 and py == 140 and px == 0
    per_class = [np.zeros((0, 5), np.float32) for _ in range(80)]
    # box at input px x 64..320, y 240..400 -> image y 100..260
    per_class[0] = np.array([[240 / 640, 64 / 640, 400 / 640, 320 / 640, 0.9]], np.float32)
    per_class[2] = np.array([[0, 0, 0.1, 0.1, 0.2]], np.float32)  # below conf
    dets = parse_nms_by_class(per_class, (640, 640), s, px, py, 640, 360, 0.35)
    assert len(dets) == 1 and dets[0]["class"] == "person"
    assert dets[0]["bbox"] == [64.0, 100.0, 320.0, 260.0]
