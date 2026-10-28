import os

import numpy as np

from aibox.backends.post import ocr

DATA = os.path.join(os.path.dirname(__file__), "data")


def test_detector_finds_text_region_on_real_frame():
    with np.load(os.path.join(DATA, "ocr_det_sample_face.npz")) as fixture:
        heatmap = fixture["arr_0"]
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    regions = ocr.detect_regions(heatmap, image, (544, 960))
    assert regions
    assert any(region["bbox"][2] - region["bbox"][0] > 100 for region in regions)


def test_ctc_decode_removes_blanks_and_repeated_tokens():
    text = "RACQUET"
    raw = np.zeros((1, len(text), len(ocr.CHARACTERS)), dtype=np.float32)
    for i, char in enumerate(text):
        raw[0, i, ocr.CHARACTERS.index(char)] = 1
    decoded, confidence = ocr.decode_text(raw)
    assert decoded == text
    assert confidence == 1


def test_boxes_hug_a_text_line_in_letterboxed_frame():
    """A 640x360 frame letterboxed into 544x960: a text blob at known pixels (the book-cover
    'LIFE SKILLS' line, x 215-322, y 148-166) must give a box within a few px of it, not 3x."""
    model_h, model_w = 544, 960
    img_w, img_h = 640, 360
    scale = min(model_w / img_w, model_h / img_h)
    vw, vh = round(img_w * scale), round(img_h * scale)
    px, py = (model_w - vw) // 2, (model_h - vh) // 2
    heat = np.zeros((model_h, model_w), np.float32)
    x1, y1, x2, y2 = 215, 148, 322, 166
    heat[py + int(y1 * scale):py + int(y2 * scale), px + int(x1 * scale):px + int(x2 * scale)] = 0.9
    image = np.full((img_h, img_w, 3), 255, np.uint8)
    (region,) = ocr.detect_regions(heat, image, (model_h, model_w))
    bx1, by1, bx2, by2 = region["bbox"]
    assert abs(bx1 - x1) <= 6 and abs(bx2 - x2) <= 6
    assert abs(by1 - y1) <= 6 and abs(by2 - y2) <= 6
    assert (bx2 - bx1) < 1.2 * (x2 - x1) and (by2 - by1) < 2.0 * (y2 - y1)
    assert region["crop"].shape[1] > x2 - x1  # recognition crop keeps a margin
