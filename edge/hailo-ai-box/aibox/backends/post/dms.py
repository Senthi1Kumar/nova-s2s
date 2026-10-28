"""SCRFD face decoding and driver-monitoring heuristics.

Face decoding is adapted from Hailo's scrfd.cpp. MIT License, Copyright (c) 2024 Hailo.
"""

from __future__ import annotations

import numpy as np

from .detection import unletterbox


def _nms(boxes: np.ndarray, scores: np.ndarray, threshold: float = 0.4) -> list[int]:
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
        inter = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
        area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        union = area[best] + area[rest] - inter
        order = rest[inter / np.maximum(union, 1e-9) < threshold]
    return keep


def bbox_with_landmarks(bbox: list[float], landmarks: list[list[float]], img_w: int,
                        img_h: int) -> list[float]:
    points = np.asarray(landmarks, dtype=np.float32)
    x1, y1, x2, y2 = bbox
    inside = bool(np.all((points[:, 0] >= x1) & (points[:, 0] <= x2)
                         & (points[:, 1] >= y1) & (points[:, 1] <= y2)))
    if inside:
        return [round(float(v), 1) for v in bbox]
    eye_distance = float(np.linalg.norm(points[0] - points[1]))
    margin = max(8.0, eye_distance * 0.75)
    return [
        round(float(np.clip(points[:, 0].min() - margin, 0, img_w - 1)), 1),
        round(float(np.clip(points[:, 1].min() - margin, 0, img_h - 1)), 1),
        round(float(np.clip(points[:, 0].max() + margin, 0, img_w - 1)), 1),
        round(float(np.clip(points[:, 1].max() + margin, 0, img_h - 1)), 1),
    ]


def parse_faces(raw: dict[str, np.ndarray], in_hw: tuple[int, int], scale: float,
                px: int, py: int, img_w: int, img_h: int, conf: float = 0.4) -> list[dict]:
    tensors = [v[0] if v.ndim == 4 and v.shape[0] == 1 else v for v in raw.values()]
    all_boxes, all_scores, all_landmarks = [], [], []
    for grid_h in sorted({v.shape[0] for v in tensors}, reverse=True):
        group = [v for v in tensors if v.shape[0] == grid_h]
        boxes = next(v for v in group if v.shape[-1] == 8).reshape(-1, 4)
        scores = next(v for v in group if v.shape[-1] == 2).reshape(-1)
        landmarks = next(v for v in group if v.shape[-1] == 20).reshape(-1, 10)
        stride = in_hw[0] // grid_h
        grid_w = in_hw[1] // stride
        grid_x, grid_y = np.meshgrid(np.arange(grid_w), np.arange(grid_h))
        centers = np.stack((grid_x.ravel() * stride / in_hw[1],
                            grid_y.ravel() * stride / in_hw[0]), axis=1)
        centers = np.repeat(centers, 2, axis=0)
        scales = np.full((len(centers), 2), [stride / in_hw[1], stride / in_hw[0]])
        decoded_boxes = np.empty_like(boxes)
        decoded_boxes[:, 0] = centers[:, 0] - boxes[:, 0] * scales[:, 0]
        decoded_boxes[:, 1] = centers[:, 1] - boxes[:, 1] * scales[:, 1]
        decoded_boxes[:, 2] = centers[:, 0] + boxes[:, 2] * scales[:, 0]
        decoded_boxes[:, 3] = centers[:, 1] + boxes[:, 3] * scales[:, 1]
        decoded_landmarks = np.repeat(centers, 5, axis=1) + landmarks * np.repeat(scales, 5, axis=1)
        selected = scores >= conf
        all_boxes.append(decoded_boxes[selected])
        all_scores.append(scores[selected])
        all_landmarks.append(decoded_landmarks[selected])

    boxes = np.concatenate(all_boxes)
    scores = np.concatenate(all_scores)
    landmarks = np.concatenate(all_landmarks)
    faces = []
    for i in _nms(boxes, scores):
        xs, ys = unletterbox(boxes[i, [0, 2]] * in_hw[1], boxes[i, [1, 3]] * in_hw[0],
                             scale, px, py, img_w, img_h)
        points = landmarks[i].reshape(5, 2)
        lx, ly = unletterbox(points[:, 0] * in_hw[1], points[:, 1] * in_hw[0],
                             scale, px, py, img_w, img_h)
        bbox = [xs[0], ys[0], xs[1], ys[1]]
        landmark_values = [[round(float(x), 1), round(float(y), 1)] for x, y in zip(lx, ly)]
        faces.append({
            "class": "face",
            "confidence": round(float(scores[i]), 4),
            "bbox": bbox_with_landmarks(bbox, landmark_values, img_w, img_h),
            "landmarks": landmark_values,
        })
    faces.sort(key=lambda f: -f["confidence"])
    return faces


def head_yaw(face: dict) -> float:
    points = np.asarray(face["landmarks"], dtype=np.float32)
    eye_mid = (points[0] + points[1]) / 2
    eye_distance = max(float(np.linalg.norm(points[0] - points[1])), 1.0)
    return float(np.degrees(np.arctan2(points[2, 0] - eye_mid[0], eye_distance)))


def phone_in_hand(objects: list[dict], poses: list[dict], img_w: int) -> bool:
    phones = [d for d in objects if d["class"] == "cell phone"]
    if not phones:
        return False
    wrists = []
    for pose in poses:
        for index in (9, 10):
            point = pose["keypoints"][index]
            if point[2] >= 0.3:
                wrists.append(np.array(point[:2]))
    for phone in phones:
        x1, y1, x2, y2 = phone["bbox"]
        center = np.array([(x1 + x2) / 2, (y1 + y2) / 2])
        if any(np.linalg.norm(center - wrist) <= 0.15 * img_w for wrist in wrists):
            return True
    return False


MIN_FACE_SCORE = 0.6          # SCRFD scores below this on walls / textures are not faces
MIN_FACE_FRAC = 0.04          # face box width as a fraction of the image width
MAX_FACE_FRAC = 0.85
MIN_ASPECT, MAX_ASPECT = 0.45, 1.9   # width / height of a plausible face box
MIN_EYE_CONF = 0.5            # pose eye keypoint confidence
YAW_LIMIT_DEG = 25.0


def plausible_face(face: dict, img_w: int, img_h: int) -> bool:
    x1, y1, x2, y2 = face["bbox"]
    w, h = x2 - x1, y2 - y1
    if face["confidence"] < MIN_FACE_SCORE or w <= 0 or h <= 0:
        return False
    if not MIN_FACE_FRAC <= w / img_w <= MAX_FACE_FRAC:
        return False
    return MIN_ASPECT <= w / h <= MAX_ASPECT


def select_driver(faces: list[dict], img_w: int, img_h: int) -> list[dict]:
    """Keep only plausible faces and return the driver: the largest, most central one."""
    good = [f for f in faces if plausible_face(f, img_w, img_h)]
    if not good:
        return []

    def rank(f: dict) -> float:
        x1, y1, x2, y2 = f["bbox"]
        area = (x2 - x1) * (y2 - y1) / (img_w * img_h)
        off = abs((x1 + x2) / 2 - img_w / 2) / (img_w / 2)
        return area * (1.0 - 0.5 * min(off, 1.0))

    return [max(good, key=rank)]


# things a driver holds up in front of the face; background objects (chairs, TVs) are ignored
OCCLUDERS = frozenset({
    "book", "cell phone", "laptop", "remote", "bottle", "cup", "wine glass", "bowl", "mouse",
    "keyboard", "scissors", "toothbrush", "umbrella", "handbag", "teddy bear", "frisbee",
})
OCCLUSION_FRAC = 0.10   # share of the face box covered by a held object


def face_occluded(face: dict, objects: list[dict], poses: list[dict]) -> bool:
    """A held object overlaps the face, or a wrist is in front of it (hands over the face).
    Pose eye keypoints are not enough: the pose net hallucinates confident eyes behind a book."""
    x1, y1, x2, y2 = face["bbox"]
    area = max((x2 - x1) * (y2 - y1), 1.0)
    for o in objects:
        if o.get("class") not in OCCLUDERS:
            continue
        ox1, oy1, ox2, oy2 = o["bbox"]
        inter = max(0.0, min(x2, ox2) - max(x1, ox1)) * max(0.0, min(y2, oy2) - max(y1, oy1))
        if inter / area >= OCCLUSION_FRAC:
            return True
    for pose in poses:
        for index in (9, 10):
            wx, wy, ws = pose["keypoints"][index]
            # a wrist raised to the face (beside it, up to the chin) is a hand over the eyes
            if (ws >= 0.3 and x1 - (x2 - x1) <= wx <= x2 + (x2 - x1)
                    and y1 - 0.3 * (y2 - y1) <= wy <= y2 + 0.1 * (y2 - y1)):
                return True
    return False


def eyes_visible(face: dict, poses: list[dict], objects: list[dict] | None = None) -> bool:
    """Both eyes visible: face score is high, nothing held over the face and, when a pose person
    owns this face, both eye keypoints (COCO 1, 2) are confident."""
    if face["confidence"] < MIN_FACE_SCORE:
        return False
    if face_occluded(face, objects or [], poses):
        return False
    x1, y1, x2, y2 = face["bbox"]
    owner = None
    for pose in poses:
        nose = pose["keypoints"][0]
        if x1 <= nose[0] <= x2 and y1 <= nose[1] <= y2 or (
                x1 <= pose["keypoints"][1][0] <= x2 and y1 <= pose["keypoints"][1][1] <= y2):
            owner = pose
            break
    if owner is None:
        return True  # no body pose to contradict the face detector
    return owner["keypoints"][1][2] >= MIN_EYE_CONF and owner["keypoints"][2][2] >= MIN_EYE_CONF


SUMMARIES = {
    "attentive": "Driver attentive: looking ahead",
    "looking_away": "Looking away from the road",
    "eyes_not_visible": "Eyes not visible: hands over the face?",
    "phone_in_hand": "Phone in hand while driving",
    "no_driver": "No driver in view",
}


def assess(faces: list[dict], objects: list[dict], poses: list[dict], img_w: int,
           img_h: int | None = None) -> dict:
    """Driver status from the driver face. `faces` should already be filtered by select_driver."""
    face = faces[0] if faces else None
    yaw = head_yaw(face) if face else None
    phone = phone_in_hand(objects, poses, img_w)
    visible = bool(face and eyes_visible(face, poses, objects))
    on_road = bool(visible and yaw is not None and abs(yaw) < YAW_LIMIT_DEG)
    if face is None:
        status = "no_driver"
    elif phone:
        status = "phone_in_hand"
    elif not visible:
        status = "eyes_not_visible"
    elif not on_road:
        status = "looking_away"
    else:
        status = "attentive"
    return {
        "status": status,
        "summary": SUMMARIES[status],
        "faces": len(faces),
        "head_yaw_deg": round(yaw, 1) if yaw is not None else None,
        "eyes_visible": visible,
        "eyes_on_road": on_road,
        "phone_in_hand": phone,
    }
