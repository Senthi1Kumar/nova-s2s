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


def load_img(name):
    return np.array(Image.open(os.path.join(DATA, name)).convert("RGB"))


@pytest.fixture(scope="module")
def vision():
    ins = Emitter(Inspector())
    reg = Registry(ins)
    pool = NpuPool(Settings(), ins, reg)
    service = HailoVision(pool, reg)
    yield service, pool
    service.close()
    pool.close()


async def test_pose_on_frame(vision):
    service, pool = vision
    await service.detect(load_img("c920e.png"), "pose")
    result = await service.detect(load_img("c920e.png"), "pose")
    assert result["detections"]
    assert len(result["detections"][0]["keypoints"]) == 17
    assert result["inference_ms"] < 150
    print("pose", result["inference_ms"], "ms", len(result["detections"]), "people", pool.stats())


async def test_segmentation_on_frame(vision):
    service, pool = vision
    await service.detect(load_img("sample_face.png"), "segmentation")
    result = await service.detect(load_img("sample_face.png"), "segmentation")
    masked = [d for d in result["detections"] if d.get("mask_rle")]
    assert masked
    assert masked[0]["mask_rle"]["size"] == [720, 1280]
    assert result["inference_ms"] < 200  # allow normal shared-NPU and mask post-processing variance
    print("segmentation", result["inference_ms"], "ms", len(masked), "masks", pool.stats())


async def test_lanes_on_frame(vision):
    service, pool = vision
    await service.detect(load_img("c920e.png"), "lanes")
    result = await service.detect(load_img("c920e.png"), "lanes")
    assert isinstance(result["lanes"], list)
    for lane in result["lanes"]:
        assert len(lane) >= 2
        for x, y in lane:
            assert 0 <= x < 1280 and 0 <= y < 720
    print("lanes", result["inference_ms"], "ms", len(result["lanes"]), "lanes", pool.stats())


async def test_dms_on_frame(vision):
    service, pool = vision
    await service.detect(load_img("sample_face.png"), "dms")
    result = await service.detect(load_img("sample_face.png"), "dms")
    assert result["dms"]["faces"] >= 1
    assert isinstance(result["dms"]["head_yaw_deg"], float)
    assert result["inference_ms"] < 300
    print("dms", result["inference_ms"], "ms", result["dms"], pool.stats())


async def test_ocr_on_frame(vision):
    service, pool = vision
    await service.detect(load_img("sample_face.png"), "ocr")
    result = await service.detect(load_img("sample_face.png"), "ocr")
    assert any("RACQUET" in line["text"].upper() for line in result["text"])
    assert result["inference_ms"] < 300
    print("ocr", result["inference_ms"], "ms", result["text"], pool.stats())
