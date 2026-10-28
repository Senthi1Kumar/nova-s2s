"""YOLOv8 on-chip-NMS output (HAILO NMS BY CLASS, FLOAT32) to COCO detections."""

from __future__ import annotations

import numpy as np

COCO = (
    "person bicycle car motorcycle airplane bus train truck boat traffic_light fire_hydrant "
    "stop_sign parking_meter bench bird cat dog horse sheep cow elephant bear zebra giraffe "
    "backpack umbrella handbag tie suitcase frisbee skis snowboard sports_ball kite baseball_bat "
    "baseball_glove skateboard surfboard tennis_racket bottle wine_glass cup fork knife spoon bowl "
    "banana apple sandwich orange broccoli carrot hot_dog pizza donut cake chair couch "
    "potted_plant bed dining_table toilet tv laptop mouse remote keyboard cell_phone microwave "
    "oven toaster sink refrigerator book clock vase scissors teddy_bear hair_drier toothbrush"
).split()
COCO_NAMES = [n.replace("_", " ") for n in COCO]


def letterbox(img: np.ndarray, size: tuple[int, int]) -> tuple[np.ndarray, float, int, int]:
    """Resize keeping aspect into `size` (h, w), pad with 114. Returns (canvas, scale, padx, pady)."""
    import cv2

    th, tw = size
    h, w = img.shape[:2]
    s = min(tw / w, th / h)
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((th, tw, 3), 114, dtype=np.uint8)
    px, py = (tw - nw) // 2, (th - nh) // 2
    canvas[py:py + nh, px:px + nw] = resized
    return canvas, s, px, py


def unletterbox(xs: np.ndarray, ys: np.ndarray, scale: float, px: int, py: int,
                w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    return (np.clip((xs - px) / scale, 0, w - 1), np.clip((ys - py) / scale, 0, h - 1))


def parse_nms_by_class(per_class: list, in_hw: tuple[int, int], scale: float, px: int, py: int,
                       img_w: int, img_h: int, conf: float = 0.35) -> list[dict]:
    """`per_class[c]` is (n, 5): ymin, xmin, ymax, xmax (normalised to input) and score."""
    th, tw = in_hw
    dets: list[dict] = []
    for cls, arr in enumerate(per_class):
        arr = np.asarray(arr)
        if arr.size == 0:
            continue
        for ymin, xmin, ymax, xmax, score in arr.reshape(-1, 5):
            if score < conf:
                continue
            xs, ys = unletterbox(np.array([xmin * tw, xmax * tw]), np.array([ymin * th, ymax * th]),
                                 scale, px, py, img_w, img_h)
            dets.append({
                "class": COCO_NAMES[cls] if cls < len(COCO_NAMES) else str(cls),
                "confidence": round(float(score), 4),
                "bbox": [round(float(xs[0]), 1), round(float(ys[0]), 1),
                         round(float(xs[1]), 1), round(float(ys[1]), 1)],
            })
    dets.sort(key=lambda d: -d["confidence"])
    return dets
