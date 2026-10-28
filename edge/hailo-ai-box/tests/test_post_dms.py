import os

import numpy as np

from aibox.backends.post import dms

DATA = os.path.join(os.path.dirname(__file__), "data")


def load_raw(name):
    with np.load(os.path.join(DATA, name)) as fixture:
        return {str(key): fixture[f"arr_{i}"] for i, key in enumerate(fixture["names"])}


def test_scrfd_decodes_real_face_and_head_yaw():
    faces = dms.parse_faces(load_raw("dms_sample_face.npz"), (640, 640), 0.5, 0, 160,
                            1280, 720, 0.4)
    assert faces
    assert faces[0]["class"] == "face"
    assert len(faces[0]["landmarks"]) == 5
    yaw = dms.head_yaw(faces[0])
    assert isinstance(yaw, float)
    assert abs(yaw) < 25


def test_phone_in_hand_requires_phone_box_near_a_wrist():
    poses = [{"keypoints": [[0, 0, 1]] * 9 + [[100, 100, 1], [200, 200, 1]] + [[0, 0, 1]] * 6}]
    objects = [{"class": "cell phone", "bbox": [90, 90, 110, 110]}]
    assert dms.phone_in_hand(objects, poses, 640)
    objects[0]["bbox"] = [400, 400, 420, 420]
    assert not dms.phone_in_hand(objects, poses, 640)


def test_face_bbox_is_expanded_to_contain_landmarks():
    bbox = dms.bbox_with_landmarks([100, 100, 150, 150], [[120, 120], [130, 20], [125, 140]], 352, 640)
    assert bbox[1] < 20
    assert all(x >= bbox[0] and x <= bbox[2] and y >= bbox[1] and y <= bbox[3]
               for x, y in [[120, 120], [130, 20], [125, 140]])


def _cases():
    import json
    with open(os.path.join(DATA, "dms_cases.json")) as f:
        return json.load(f)


def _assess_case(c):
    faces = dms.select_driver(c["faces"], c["w"], c["h"])
    return dms.assess(faces, c["objects"], c["poses"], c["w"], c["h"]), faces


def test_real_frames_hand_over_eyes_and_book_are_eyes_not_visible():
    for name in ("eyes_covered", "eyes_covered_3", "book"):
        out, faces = _assess_case(_cases()[name])
        assert faces, name
        assert out["status"] == "eyes_not_visible", name
        assert out["eyes_visible"] is False and out["eyes_on_road"] is False
        assert out["summary"] == "Eyes not visible: hands over the face?"


def test_real_face_without_occluder_is_attentive_with_summary():
    faces = dms.parse_faces(load_raw("dms_sample_face.npz"), (640, 640), 0.5, 0, 160,
                            1280, 720, 0.4)
    faces = dms.select_driver(faces, 1280, 720)
    assert len(faces) == 1
    out = dms.assess(faces, [], [], 1280, 720)
    assert out["status"] == "attentive"
    assert out["summary"] == "Driver attentive: looking ahead"
    assert out["eyes_visible"] and out["eyes_on_road"]
    assert set(out) == {"status", "summary", "faces", "head_yaw_deg", "eyes_visible",
                        "eyes_on_road", "phone_in_hand"}


def test_no_face_is_no_driver():
    out = dms.assess([], [], [], 640, 360)
    assert out["status"] == "no_driver" and out["faces"] == 0
    assert out["eyes_on_road"] is False and out["head_yaw_deg"] is None


def test_wall_like_detections_are_rejected():
    low = {"confidence": 0.45, "bbox": [100, 100, 180, 190], "landmarks": [[1, 1]] * 5}
    tiny = {"confidence": 0.9, "bbox": [100, 100, 108, 110], "landmarks": [[1, 1]] * 5}
    huge = {"confidence": 0.9, "bbox": [0, 0, 640, 360], "landmarks": [[1, 1]] * 5}
    flat = {"confidence": 0.9, "bbox": [0, 0, 300, 40], "landmarks": [[1, 1]] * 5}
    assert dms.select_driver([low, tiny, huge, flat], 640, 360) == []


def test_only_the_largest_central_face_is_reported():
    side = {"confidence": 0.9, "bbox": [10, 100, 60, 160], "landmarks": [[1, 1]] * 5}
    driver = {"confidence": 0.8, "bbox": [270, 90, 370, 220], "landmarks": [[1, 1]] * 5}
    assert dms.select_driver([side, driver], 640, 360) == [driver]


def test_turned_head_is_looking_away_and_phone_wins():
    face = {"confidence": 0.9, "bbox": [270, 90, 370, 220],
            "landmarks": [[300, 130], [340, 130], [375, 160], [305, 190], [335, 190]]}
    out = dms.assess([face], [], [], 640, 360)
    assert out["status"] == "looking_away" and not out["eyes_on_road"]
    assert out["eyes_visible"] is True
    poses = [{"keypoints": [[0, 0, 1]] * 9 + [[500, 300, 1], [505, 305, 1]] + [[0, 0, 1]] * 6}]
    phone = [{"class": "cell phone", "bbox": [490, 290, 510, 310]}]
    assert dms.assess([face], phone, poses, 640, 360)["status"] == "phone_in_hand"


def test_real_profile_frame_is_looking_away():
    out, faces = _assess_case(_cases()["looking_away"])
    assert faces
    assert out["status"] == "looking_away"
    assert out["eyes_visible"] is True and out["eyes_on_road"] is False
    assert out["summary"] == "Looking away from the road"
