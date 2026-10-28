import os

import numpy as np

from aibox.backends.post import segmentation

DATA = os.path.join(os.path.dirname(__file__), "data")


def load_raw(name):
    with np.load(os.path.join(DATA, name)) as fixture:
        return {str(key): fixture[f"arr_{i}"] for i, key in enumerate(fixture["names"])}


def decode_rle(rle):
    h, w = rle["size"]
    flat = np.zeros(h * w, dtype=np.uint8)
    pos = 0
    value = 0
    for count in rle["counts"]:
        if value:
            flat[pos:pos + count] = 1
        pos += count
        value ^= 1
    assert pos == h * w
    return flat.reshape((w, h)).T


def test_segmentation_decodes_real_mask_at_image_scale():
    detections = segmentation.parse(
        load_raw("segmentation_c920e.npz"),
        in_hw=(640, 640),
        scale=0.5,
        px=0,
        py=160,
        img_w=1280,
        img_h=720,
        conf=0.35,
    )

    assert detections
    masked = [d for d in detections if d.get("mask_rle")]
    assert masked
    for detection in masked:
        assert 0 <= detection["confidence"] <= 1
        mask = decode_rle(detection["mask_rle"])
        assert mask.shape == (720, 1280)
        assert mask.any()
        x1, y1, x2, y2 = np.array(detection["bbox"]).astype(int)
        assert not mask[:max(0, y1)].any()
        assert not mask[max(0, y2 + 1):].any()
        assert not mask[:, :max(0, x1)].any()
        assert not mask[:, max(0, x2 + 1):].any()
