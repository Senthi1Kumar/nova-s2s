import os

import numpy as np

from aibox.backends.post import pose

DATA = os.path.join(os.path.dirname(__file__), "data")


def load_raw(name):
    with np.load(os.path.join(DATA, name)) as fixture:
        return {str(key): fixture[f"arr_{i}"] for i, key in enumerate(fixture["names"])}


def test_pose_decodes_real_person_with_17_keypoints():
    detections = pose.parse(
        load_raw("pose_c920e.npz"),
        in_hw=(640, 640),
        scale=0.5,
        px=0,
        py=160,
        img_w=1280,
        img_h=720,
        conf=0.35,
    )

    assert detections
    person = detections[0]
    assert person["class"] == "person"
    assert person["confidence"] >= 0.35
    assert len(person["keypoints"]) == 17
    x1, y1, x2, y2 = person["bbox"]
    assert 0 <= x1 < x2 <= 1280
    assert 0 <= y1 < y2 <= 720
    for x, y, score in person["keypoints"]:
        assert 0 <= x < 1280
        assert 0 <= y < 720
        assert 0 <= score <= 1
