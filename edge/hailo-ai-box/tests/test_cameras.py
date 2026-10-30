import asyncio
import time

import numpy as np
import pytest

from aibox.backends.cameras import Cameras
from aibox.backends.governor import HailoGovernor
from aibox.backends.registry import Registry
from aibox.config import Settings
from aibox.inspector import Inspector


class FakeCapture:
    opened = 0
    instances: list = []

    def __init__(self, ok=True):
        self.ok = ok
        self.released = False
        FakeCapture.opened += 1
        FakeCapture.instances.append(self)

    def isOpened(self):
        return self.ok

    def read(self):
        time.sleep(0.005)
        return True, np.full((36, 64, 3), (10, 20, 30), np.uint8)  # BGR

    def release(self):
        self.released = True


@pytest.fixture(autouse=True)
def _reset():
    FakeCapture.opened = 0
    FakeCapture.instances = []


async def _take(cams, n, fps=50):
    got = []
    async for f in cams.frames("c920e", fps):
        got.append(f)
        if len(got) == n:
            break
    return got


async def test_two_subscribers_share_one_capture():
    cams = Cameras(Settings(), opener=lambda name, path: FakeCapture())
    a, b = await asyncio.gather(_take(cams, 3), _take(cams, 5))
    assert len(a) == 3 and len(b) == 5
    assert FakeCapture.opened == 1
    assert tuple(a[0][0, 0]) == (30, 20, 10)  # BGR -> RGB
    assert a[0].flags.writeable
    cap = FakeCapture.instances[0]
    for _ in range(100):
        if cap.released:
            break
        await asyncio.sleep(0.01)
    assert cap.released and cams.active() == {}
    # a later subscriber reopens
    await _take(cams, 1)
    assert FakeCapture.opened == 2


async def test_capture_stays_open_while_one_subscriber_remains():
    cams = Cameras(Settings(), opener=lambda name, path: FakeCapture())
    long = asyncio.create_task(_take(cams, 40, fps=50))
    await _take(cams, 2)
    await asyncio.sleep(0.05)
    assert not FakeCapture.instances[0].released
    await long
    await asyncio.sleep(0.2)
    assert FakeCapture.instances[0].released


async def test_unopenable_camera_raises():
    cams = Cameras(Settings(), opener=lambda n, p: FakeCapture(ok=False))
    with pytest.raises(RuntimeError):
        await _take(cams, 1)
    with pytest.raises(KeyError):
        async for _ in cams.frames("nope", 5):
            pass


def test_thermal_steps():
    ins = Inspector()
    gov = HailoGovernor(Registry(ins), slot=None, pool=None, telemetry_temp_fn=lambda: None)
    caps = [gov.observe_temp(t) for t in (79, 80, 83, 75)]
    assert caps == [None, 5.0, 2.0, None]
    steps = [e for e in ins.recent() if e.get("event") == "thermal_step"]
    assert len(steps) == 3
    assert gov.observe_temp(78) is None
    gov.observe_temp(84)
    assert gov.observe_temp(78) == 2.0  # hysteresis: no loosening until < 76
    assert gov.vision_fps_cap() == 2.0


async def test_reopen_waits_for_previous_stream_to_release_device():
    """Stop then immediately start on the same camera must not fail with 'could not be opened'
    while the old capture thread still holds the (exclusive) V4L2 device."""
    state = {"held": False}

    class ExclusiveCapture(FakeCapture):
        def __init__(self):
            super().__init__(ok=not state["held"])
            if self.ok:
                state["held"] = True

        def read(self):
            time.sleep(0.03)
            return super().read()

        def release(self):
            time.sleep(0.2)  # slow V4L2 close
            state["held"] = False
            super().release()

    cams = Cameras(Settings(), opener=lambda name, path: ExclusiveCapture())
    assert len(await _take(cams, 2)) == 2
    assert len(await _take(cams, 2)) == 2  # immediate reopen succeeds


def test_resolve_device_prefers_first_pattern_and_skips_excluded(tmp_path):
    from aibox.backends.cameras import resolve_device

    for name in ("usb-Intel_Intel_F450_X-video-index0", "usb-Sonix_Cam-video-index0"):
        (tmp_path / name).touch()
    any_usb = f"{tmp_path}/usb-*-video-index0"
    c920e = f"{tmp_path}/usb-046d_Logi_Webcam_C920e_*-video-index0"
    realsense = f"{tmp_path}/usb-Intel_Intel_F45*"
    # No C920e: the USB source falls back to the other webcam, never the RealSense.
    assert resolve_device(f"{c920e}|{any_usb}", (realsense,)) == f"{tmp_path}/usb-Sonix_Cam-video-index0"
    (tmp_path / "usb-046d_Logi_Webcam_C920e_A1-video-index0").touch()
    assert resolve_device(f"{c920e}|{any_usb}", (realsense,)).endswith("C920e_A1-video-index0")
    assert resolve_device("/dev/video7") == "/dev/video7"  # explicit paths pass through


def test_resolve_device_none_when_nothing_connected(tmp_path):
    from aibox.backends.cameras import resolve_device

    assert resolve_device(f"{tmp_path}/usb-*-video-index0") is None
