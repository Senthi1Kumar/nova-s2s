"""Two-stage Paddle OCR post-processing using OpenCV and NumPy.

CTC decoding and region rectification are adapted from Hailo's paddle_ocr_utils.py.
MIT License, Copyright (c) 2024 Hailo. DB post-processing is derived from PaddleOCR,
Copyright (c) 2020 PaddlePaddle Authors, Apache License 2.0.
"""

from __future__ import annotations

import cv2
import numpy as np

CHARACTERS = [
    "blank", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", ":", ";", "<", "=", ">", "?", "@",
    "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "R",
    "S", "T", "U", "V", "W", "X", "Y", "Z", "[", "\\", "]", "^", "_", "`", "a", "b", "c", "d",
    "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o", "p", "q", "r", "s", "t", "u", "v",
    "w", "x", "y", "z", "{", "|", "}", "~", "!", '"', "#", "$", "%", "&", "'", "(", ")", "*",
    "+", ",", "-", ".", "/", " ", " ",
]


def _order_points(points: np.ndarray) -> np.ndarray:
    points = sorted(points, key=lambda p: p[0])
    left, right = points[:2], points[2:]
    left = sorted(left, key=lambda p: p[1])
    right = sorted(right, key=lambda p: p[1])
    return np.array([left[0], right[0], right[1], left[1]], dtype=np.float32)


def _warp(image: np.ndarray, box: np.ndarray) -> np.ndarray:
    width = max(1, round(float(np.linalg.norm(box[0] - box[1]))))
    height = max(1, round(float(np.linalg.norm(box[0] - box[3]))))
    destination = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
                           dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(box.astype(np.float32), destination)
    return cv2.warpPerspective(image, matrix, (width, height), flags=cv2.INTER_LINEAR)


def detect_regions(heatmap: np.ndarray, image: np.ndarray, model_hw: tuple[int, int],
                   bin_thresh: float = 0.3, box_thresh: float = 0.6,
                   unclip_ratio: float = 3.0, min_area: float = 40.0,
                   box_pad: float = 2.0) -> list[dict]:
    """DB post-processing. The reported `bbox` hugs the detected text (the contour's rotated
    rectangle plus `box_pad` px); the recognition `crop` uses the DB-style unclipped box
    (each side grows by area * unclip_ratio / perimeter) so glyph edges are not cut."""
    model_h, model_w = model_hw
    img_h, img_w = image.shape[:2]
    heatmap = np.squeeze(heatmap)
    if heatmap.shape != model_hw:
        raise ValueError(f"expected OCR heatmap {model_hw}, got {heatmap.shape}")

    scale = min(model_w / img_w, model_h / img_h)
    valid_w, valid_h = round(img_w * scale), round(img_h * scale)
    px, py = (model_w - valid_w) // 2, (model_h - valid_h) // 2
    unpadded = heatmap[py:py + valid_h, px:px + valid_w]
    probability = cv2.resize(unpadded, (img_w, img_h), interpolation=cv2.INTER_CUBIC)
    bitmap = (probability > bin_thresh).astype(np.uint8)
    contours, _ = cv2.findContours(bitmap * 255, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    regions = []
    for contour in contours[:1000]:
        rect = cv2.minAreaRect(contour)
        if min(rect[1]) < 3 or rect[1][0] * rect[1][1] < min_area:
            continue
        points = _order_points(cv2.boxPoints(rect))
        x1 = max(0, int(np.floor(points[:, 0].min())))
        y1 = max(0, int(np.floor(points[:, 1].min())))
        x2 = min(img_w, int(np.ceil(points[:, 0].max())) + 1)
        y2 = min(img_h, int(np.ceil(points[:, 1].max())) + 1)
        if x1 >= x2 or y1 >= y2:
            continue
        mask = np.zeros((y2 - y1, x2 - x1), dtype=np.uint8)
        shifted = (points - [x1, y1]).reshape(1, -1, 2).astype(np.int32)
        cv2.fillPoly(mask, shifted, 1)
        score = float(cv2.mean(probability[y1:y2, x1:x2], mask)[0])
        if score < box_thresh:
            continue

        center, size, angle = rect
        area = size[0] * size[1]
        distance = area * unclip_ratio / max(2.0 * (size[0] + size[1]), 1e-6)
        expanded = cv2.boxPoints((center, (size[0] + 2 * distance, size[1] + 2 * distance), angle))
        box = _order_points(expanded)
        box[:, 0] = np.clip(box[:, 0], 0, img_w - 1)
        box[:, 1] = np.clip(box[:, 1], 0, img_h - 1)
        if min(cv2.minAreaRect(box)[1]) < 5:
            continue
        crop = _warp(image, box)
        if crop.size:
            tight = cv2.boxPoints((center, (size[0] + 2 * box_pad, size[1] + 2 * box_pad), angle))
            regions.append({
                "crop": crop,
                "bbox": [int(np.clip(tight[:, 0].min(), 0, img_w - 1)),
                         int(np.clip(tight[:, 1].min(), 0, img_h - 1)),
                         int(np.clip(tight[:, 0].max(), 0, img_w - 1)),
                         int(np.clip(tight[:, 1].max(), 0, img_h - 1))],
                "detection_confidence": round(score, 4),
            })
    regions.sort(key=lambda r: (r["bbox"][1], r["bbox"][0]))
    return regions


def resize_recognition(image: np.ndarray, target_hw: tuple[int, int] = (48, 320)) -> np.ndarray:
    target_h, target_w = target_hw
    h, w = image.shape[:2]
    scale = min(target_w / w, target_h / h)
    new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
    top, left = (target_h - new_h) // 2, (target_w - new_w) // 2
    return cv2.copyMakeBorder(resized, top, target_h - new_h - top, left,
                              target_w - new_w - left, cv2.BORDER_CONSTANT, value=[128, 128, 128])


def decode_text(raw: np.ndarray) -> tuple[str, float]:
    values = raw[0] if raw.ndim == 3 else raw
    if values.ndim != 2 or values.shape[1] > len(CHARACTERS):
        raise ValueError(f"unexpected OCR output shape {raw.shape}")
    indices = values.argmax(axis=1)
    probabilities = values.max(axis=1)
    selected = np.ones(len(indices), dtype=bool)
    selected[1:] = indices[1:] != indices[:-1]
    selected &= indices != 0
    chars = [CHARACTERS[i] for i in indices[selected]]
    confidence = float(np.mean(probabilities[selected])) if selected.any() else 0.0
    return "".join(chars), confidence
