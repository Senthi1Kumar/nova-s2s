"""UFLD v2 decoding adapted from Hailo's lane_detection_utils.py.

MIT License, Copyright (c) 2024 Hailo. See the hailo-apps LICENSE.
"""

from __future__ import annotations

import cv2
import numpy as np


def preprocess(image: np.ndarray, input_hw: tuple[int, int]) -> np.ndarray:
    height, width = input_hw
    resized = cv2.resize(image, (width, int(height / 0.8)), interpolation=cv2.INTER_CUBIC)
    return resized[-height:]


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - np.max(x))
    return e / np.sum(e)


def _coordinates(loc_row: np.ndarray, loc_col: np.ndarray, exist_row: np.ndarray,
                 exist_col: np.ndarray, img_w: int, img_h: int) -> list[list[list[int]]]:
    row_anchor = np.linspace(160, 710, 56) / 720
    col_anchor = np.linspace(0, 1, 41)
    max_row = np.argmax(loc_row, axis=1)
    valid_row = np.argmax(exist_row, axis=1)
    max_col = np.argmax(loc_col, axis=1)
    valid_col = np.argmax(exist_col, axis=1)
    coords = []

    for lane_idx in (1, 2):
        lane = []
        if np.sum(valid_row[0, :, lane_idx]) > 28:
            for k in range(valid_row.shape[1]):
                if not valid_row[0, k, lane_idx]:
                    continue
                center = max_row[0, k, lane_idx]
                lo, hi = max(0, center - 1), min(100, center + 2)
                x = (np.sum(_softmax(loc_row[0, lo:hi, k, lane_idx]) * np.arange(lo, hi)) + 0.5)
                x = x / 99 * img_w
                y = row_anchor[k] * img_h
                lane.append([int(np.clip(x, 0, img_w - 1)), int(np.clip(y, 0, img_h - 1))])
        if len(lane) >= 2:
            coords.append(lane)

    for lane_idx in (0, 3):
        lane = []
        if np.sum(valid_col[0, :, lane_idx]) > 10:
            for k in range(valid_col.shape[1]):
                if not valid_col[0, k, lane_idx]:
                    continue
                center = max_col[0, k, lane_idx]
                lo, hi = max(0, center - 1), min(100, center + 2)
                y = (np.sum(_softmax(loc_col[0, lo:hi, k, lane_idx]) * np.arange(lo, hi)) + 0.5)
                y = y / 99 * img_h
                x = col_anchor[k] * img_w
                lane.append([int(np.clip(x, 0, img_w - 1)), int(np.clip(y, 0, img_h - 1))])
        if len(lane) >= 2:
            coords.append(lane)
    return coords


def parse(raw: dict[str, np.ndarray], img_w: int, img_h: int) -> list[list[list[int]]]:
    output = np.concatenate([np.asarray(v).reshape(1, -1) for v in raw.values()], axis=1)
    loc_row = output[:, :22400].reshape(-1, 100, 56, 4)
    loc_col = output[:, 22400:38800].reshape(-1, 100, 41, 4)
    exist_row = output[:, 38800:39248].reshape(-1, 2, 56, 4)
    exist_col = output[:, 39248:].reshape(-1, 2, 41, 4)
    if output.shape[1] != 39576:
        raise ValueError(f"unexpected UFLD output width {output.shape[1]}")
    return _coordinates(loc_row, loc_col, exist_row, exist_col, img_w, img_h)
