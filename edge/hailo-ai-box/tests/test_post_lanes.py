import os

import numpy as np

from aibox.backends.post import lanes

DATA = os.path.join(os.path.dirname(__file__), "data")


def test_lanes_decode_real_outputs_to_bounded_polylines():
    with np.load(os.path.join(DATA, "lanes_c920e.npz")) as fixture:
        raw = {str(key): fixture[f"arr_{i}"] for i, key in enumerate(fixture["names"])}
    result = lanes.parse(raw, 1280, 720)
    assert isinstance(result, list)
    for lane in result:
        assert len(lane) >= 2
        for x, y in lane:
            assert 0 <= x < 1280
            assert 0 <= y < 720
