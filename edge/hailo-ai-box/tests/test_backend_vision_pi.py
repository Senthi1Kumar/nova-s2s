import os

import numpy as np
import pytest
from PIL import Image

from aibox.backends.npu import NpuPool
from aibox.backends.registry import Emitter, Registry
from aibox.backends.vision import HailoVision
from aibox.config import Settings
from aibox.inspector import Inspector

pytestmark = pytest.mark.pi
DATA = os.path.join(os.path.dirname(__file__), "data")


def load_img(name: str) -> np.ndarray:
    return np.array(Image.open(os.path.join(DATA, name)).convert("RGB"))  # np.array: writable


@pytest.fixture(scope="module")
def vision():
    ins = Emitter(Inspector())
    reg = Registry(ins)
    pool = NpuPool(Settings(), ins, reg)
    v = HailoVision(pool, reg)
    v.load_resident(["yolov8m"])
    yield v, pool, reg
    v.close()
    pool.close()


async def test_detection_on_frame(vision):
    v, pool, reg = vision
    img = load_img("c920e.png")
    await v.detect(img, "detection")  # warm
    res = await v.detect(img, "detection", conf=0.5)
    persons = [d for d in res["detections"] if d["class"] == "person" and d["confidence"] >= 0.5]
    assert persons, res
    assert res["inference_ms"] < 100
    x1, y1, x2, y2 = persons[0]["bbox"]
    assert 0 <= x1 < x2 <= img.shape[1] and 0 <= y1 < y2 <= img.shape[0]
    print("npu_stats", pool.stats())
    print("residency", [(i["id"], i["state"], i["load_s"]) for i in reg.items() if i["resident"]])
    print("inference_ms", res["inference_ms"])
