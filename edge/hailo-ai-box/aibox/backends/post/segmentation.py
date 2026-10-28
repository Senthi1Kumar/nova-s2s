"""YOLOv5 segmentation decoding adapted from Hailo's postprocessing.py.

MIT License, Copyright (c) 2024 Hailo. See the hailo-apps LICENSE.
"""

from __future__ import annotations

import cv2
import numpy as np

from .detection import COCO_NAMES, unletterbox

ANCHORS = {
    32: np.array([116, 90, 156, 198, 373, 326], dtype=np.float32).reshape(3, 2),
    16: np.array([30, 61, 62, 45, 59, 119], dtype=np.float32).reshape(3, 2),
    8: np.array([10, 13, 16, 30, 33, 23], dtype=np.float32).reshape(3, 2),
}


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _nms(boxes: np.ndarray, scores: np.ndarray, threshold: float) -> list[int]:
    order = np.argsort(scores)[::-1]
    keep: list[int] = []
    while order.size:
        best = int(order[0])
        keep.append(best)
        if order.size == 1:
            break
        rest = order[1:]
        x1 = np.maximum(boxes[best, 0], boxes[rest, 0])
        y1 = np.maximum(boxes[best, 1], boxes[rest, 1])
        x2 = np.minimum(boxes[best, 2], boxes[rest, 2])
        y2 = np.minimum(boxes[best, 3], boxes[rest, 3])
        inter = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
        area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        union = area[best] + area[rest] - inter
        order = rest[inter / np.maximum(union, 1e-9) < threshold]
    return keep


def _decode_branch(output: np.ndarray, stride: int) -> np.ndarray:
    h, w = output.shape[:2]
    values = output.transpose(2, 0, 1).reshape(3, 117, h, w).transpose(0, 2, 3, 1)
    xy, wh, obj, classes, masks = np.array_split(values, [2, 4, 5, 85], axis=3)

    grid_y, grid_x = np.meshgrid(np.arange(h), np.arange(w), indexing="ij")
    grid = np.stack((grid_x, grid_y), axis=-1)[None, ...] - 0.5
    anchors = ANCHORS[stride][:, None, None, :]
    xy = (_sigmoid(xy) * 2 + grid) * stride
    wh = (_sigmoid(wh) * 2) ** 2 * anchors
    decoded = np.concatenate((xy, wh, _sigmoid(obj), classes, masks), axis=3)
    return decoded.reshape(h * w * 3, 117)


def _xywh_to_xyxy(boxes: np.ndarray) -> np.ndarray:
    out = boxes.copy()
    out[:, 0] = boxes[:, 0] - boxes[:, 2] / 2
    out[:, 1] = boxes[:, 1] - boxes[:, 3] / 2
    out[:, 2] = boxes[:, 0] + boxes[:, 2] / 2
    out[:, 3] = boxes[:, 1] + boxes[:, 3] / 2
    return out


def _mask_rle(mask: np.ndarray) -> dict:
    flat = mask.astype(np.uint8).flatten(order="F")
    changes = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    boundaries = np.concatenate(([0], changes, [flat.size]))
    counts = np.diff(boundaries).tolist()
    if flat.size and flat[0]:
        counts.insert(0, 0)
    return {"size": [int(mask.shape[0]), int(mask.shape[1])], "counts": counts}


def _original_mask(proto: np.ndarray, coeffs: np.ndarray, box: np.ndarray,
                   scale: float, px: int, py: int, img_w: int, img_h: int,
                   original_box: np.ndarray) -> np.ndarray:
    mh, mw, channels = proto.shape
    mask = _sigmoid(coeffs @ proto.reshape(-1, channels).T).reshape(mh, mw)
    mask = cv2.resize(mask, (640, 640), interpolation=cv2.INTER_LINEAR)

    x1, y1, x2, y2 = np.round(box).astype(int)
    x1, x2 = np.clip([x1, x2], 0, 640)
    y1, y2 = np.clip([y1, y2], 0, 640)
    cropped = np.zeros_like(mask)
    cropped[y1:y2, x1:x2] = mask[y1:y2, x1:x2]

    valid_h = min(640 - py, round(img_h * scale))
    valid_w = min(640 - px, round(img_w * scale))
    unpadded = cropped[py:py + valid_h, px:px + valid_w]
    original = cv2.resize(unpadded, (img_w, img_h), interpolation=cv2.INTER_LINEAR) >= 0.4

    ox1, oy1, ox2, oy2 = np.round(original_box).astype(int)
    ox1, ox2 = np.clip([ox1, ox2], 0, img_w)
    oy1, oy2 = np.clip([oy1, oy2], 0, img_h)
    bounded = np.zeros((img_h, img_w), dtype=bool)
    bounded[oy1:oy2 + 1, ox1:ox2 + 1] = original[oy1:oy2 + 1, ox1:ox2 + 1]
    return bounded


def parse(raw: dict[str, np.ndarray], in_hw: tuple[int, int], scale: float, px: int, py: int,
          img_w: int, img_h: int, conf: float = 0.35) -> list[dict]:
    tensors = [v[0] if v.ndim == 4 and v.shape[0] == 1 else v for v in raw.values()]
    proto = next(v for v in tensors if v.ndim == 3 and v.shape[-1] == 32)
    branches = [v for v in tensors if v.ndim == 3 and v.shape[-1] == 351]
    decoded = []
    for branch in sorted(branches, key=lambda v: v.shape[0], reverse=True):
        decoded.append(_decode_branch(branch, in_hw[0] // branch.shape[0]))
    predictions = np.concatenate(decoded)

    objectness = predictions[:, 4]
    candidates = predictions[objectness > 0.001].copy()
    if not candidates.size:
        return []
    candidates[:, 5:85] = _sigmoid(candidates[:, 5:85]) * candidates[:, 4:5]
    class_ids = np.argmax(candidates[:, 5:85], axis=1)
    scores = candidates[np.arange(len(candidates)), 5 + class_ids]
    prefilter = min(conf, 0.05)
    selected = np.flatnonzero(scores >= prefilter)
    if not selected.size:
        return []
    candidates, scores, class_ids = candidates[selected], scores[selected], class_ids[selected]
    order = np.argsort(scores)[::-1][:300]
    candidates, scores, class_ids = candidates[order], scores[order], class_ids[order]

    boxes = _xywh_to_xyxy(candidates[:, :4])
    shifted = boxes + class_ids[:, None] * 7680
    keep = _nms(shifted, scores, 0.6)
    detections = []
    for i in keep:
        if scores[i] < conf:
            continue
        xs, ys = unletterbox(boxes[i, [0, 2]], boxes[i, [1, 3]], scale, px, py, img_w, img_h)
        original_box = np.array([xs[0], ys[0], xs[1], ys[1]])
        mask = _original_mask(proto, candidates[i, 85:], boxes[i], scale, px, py,
                              img_w, img_h, original_box)
        if not mask.any():
            continue
        class_id = int(class_ids[i])
        detections.append({
            "class": COCO_NAMES[class_id] if class_id < len(COCO_NAMES) else str(class_id),
            "confidence": round(float(scores[i]), 4),
            "bbox": [round(float(v), 1) for v in original_box],
            "mask_rle": _mask_rle(mask),
        })
        if len(detections) >= 50:
            break
    detections.sort(key=lambda d: -d["confidence"])
    return detections
