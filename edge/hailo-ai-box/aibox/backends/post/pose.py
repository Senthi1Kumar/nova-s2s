"""YOLOv8-pose decoding adapted from Hailo's pose_estimation_utils.py.

MIT License, Copyright (c) 2024 Hailo. See the hailo-apps LICENSE.
"""

from __future__ import annotations

import numpy as np

from .detection import unletterbox


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - np.max(x, axis=-1, keepdims=True))
    return e / np.sum(e, axis=-1, keepdims=True)


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
        area = ((boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1]))
        union = area[best] + area[rest] - inter
        order = rest[inter / np.maximum(union, 1e-9) < threshold]
    return keep


def _decode_branch(box: np.ndarray, score: np.ndarray, kpts: np.ndarray, stride: int,
                   in_hw: tuple[int, int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    h, w = box.shape[:2]
    grid_x, grid_y = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    centers = np.stack((grid_x.ravel() * stride, grid_y.ravel() * stride), axis=1)

    distributions = box.reshape(1, h * w, 4, 16)
    distances = np.sum(_softmax(distributions) * np.arange(16), axis=-1) * stride
    decoded = np.empty((h * w, 4), dtype=np.float32)
    decoded[:, 0] = centers[:, 0] - distances[0, :, 0]
    decoded[:, 1] = centers[:, 1] - distances[0, :, 1]
    decoded[:, 2] = centers[:, 0] + distances[0, :, 2]
    decoded[:, 3] = centers[:, 1] + distances[0, :, 3]

    points = kpts.reshape(h * w, 17, 3).copy()
    points[..., :2] *= 2
    points[..., :2] = stride * (points[..., :2] - 0.5) + centers[:, None, :]
    return decoded, score.reshape(h * w), points


def parse(raw: dict[str, np.ndarray], in_hw: tuple[int, int], scale: float, px: int, py: int,
          img_w: int, img_h: int, conf: float = 0.35) -> list[dict]:
    branches: list[tuple[np.ndarray, int, int, int]] = []
    for value in raw.values():
        tensor = value[0] if value.ndim == 4 and value.shape[0] == 1 else value
        if tensor.ndim != 3:
            raise ValueError(f"unexpected pose output shape {value.shape}")
        h, _, channels = tensor.shape
        stride = in_hw[0] // h
        branches.append((tensor, h, channels, stride))

    decoded_boxes = []
    decoded_scores = []
    decoded_kpts = []
    for grid_h in sorted({b[1] for b in branches}, reverse=True):
        group = [b for b in branches if b[1] == grid_h]
        box = next(b[0] for b in group if b[2] == 64)
        score = next(b[0] for b in group if b[2] == 1)
        kpts = next(b[0] for b in group if b[2] == 51)
        stride = in_hw[0] // grid_h
        boxes, scores, points = _decode_branch(box, score, kpts, stride, in_hw)
        decoded_boxes.append(boxes)
        decoded_scores.append(scores)
        decoded_kpts.append(points)

    boxes = np.concatenate(decoded_boxes)
    scores = np.concatenate(decoded_scores)
    points = np.concatenate(decoded_kpts)
    selected = np.flatnonzero(scores >= conf)
    if not selected.size:
        return []
    boxes, scores, points = boxes[selected], scores[selected], points[selected]
    keep = _nms(boxes, scores, 0.7)[:100]

    detections = []
    for i in keep:
        xs, ys = unletterbox(boxes[i, [0, 2]], boxes[i, [1, 3]], scale, px, py, img_w, img_h)
        kx, ky = unletterbox(points[i, :, 0], points[i, :, 1], scale, px, py, img_w, img_h)
        joint_scores = _sigmoid(points[i, :, 2])
        detections.append({
            "class": "person",
            "confidence": round(float(scores[i]), 4),
            "bbox": [round(float(xs[0]), 1), round(float(ys[0]), 1),
                     round(float(xs[1]), 1), round(float(ys[1]), 1)],
            "keypoints": [[round(float(x), 1), round(float(y), 1), round(float(s), 4)]
                          for x, y, s in zip(kx, ky, joint_scores)],
        })
    detections.sort(key=lambda d: -d["confidence"])
    return detections
