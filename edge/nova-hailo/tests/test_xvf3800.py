"""reSpeaker XVF3800 status reader: the USB control protocol, and what the board says while
Nova talks and while the driver talks."""
import math
import struct

import pytest

from nova_hailo import xvf3800 as xvf


class _Dev:
    """Answers read requests the way the board does: status byte, then little-endian values."""

    def __init__(self, values, retries=0):
        self.values = dict(values)        # (resid, cmdid) -> tuple
        self.retries = retries
        self.calls = []

    def ctrl_transfer(self, bm, req, wvalue, windex, length, timeout=None):
        self.calls.append((bm, req, wvalue, windex, length))
        if self.retries:
            self.retries -= 1
            return bytes([64] + [0] * (length - 1))
        cmd = wvalue & 0x7F
        for name, (resid, cmdid, count, code) in xvf.COMMANDS.items():
            if (resid, cmdid) == (windex, cmd):
                body = struct.pack("<" + code * count, *self.values[(resid, cmdid)])
                return bytes([0]) + body
        raise AssertionError(f"unexpected read {windex}/{cmd}")


def _values(energy=(0.0, 0.0, 0.0, 0.0), converged=1, path=0, azimuth=(math.nan, math.nan)):
    return {(33, 80): energy, (33, 3): (converged,), (33, 0): (path,), (35, 11): azimuth, (48, 0): (2, 0, 6)}


def test_a_read_is_a_vendor_control_transfer_in_with_the_command_and_resource():
    dev = _Dev(_values(energy=(1.0, 2.0, 3.0, 4.0)))
    assert xvf.read(dev, "AEC_SPENERGY_VALUES") == (1.0, 2.0, 3.0, 4.0)
    bm, req, wvalue, windex, length = dev.calls[0]
    assert bm == 0xC0 and req == 0 and wvalue == 0x80 | 80 and windex == 33 and length == 4 * 4 + 1


def test_a_busy_board_is_asked_again():
    dev = _Dev(_values(converged=1), retries=2)
    assert xvf.read(dev, "AEC_AECCONVERGED") == (1,)
    assert len(dev.calls) == 3


def test_a_board_that_stays_busy_gives_nothing():
    dev = _Dev(_values(), retries=50)
    assert xvf.read(dev, "AEC_AECCONVERGED") is None


def test_one_poll_makes_a_board_state():
    dev = _Dev(_values(energy=(0.0, 0.0, 120.0, 2_080_656.0), converged=1, path=0, azimuth=(1.2, 1.1)))
    state = xvf.poll(dev, now=10.0, with_azimuth=True)
    assert state.speech is True and state.speech_energy == 2_080_656.0
    assert state.converged is True and state.path_change is False
    assert state.azimuth == pytest.approx(1.2)
    quiet = xvf.poll(_Dev(_values()), now=11.0, with_azimuth=True)
    assert quiet.speech is False and quiet.azimuth is None    # NaN: no speech in either beam


def test_the_reader_keeps_the_latest_state_and_lets_it_go_stale():
    t = [0.0]
    r = xvf.Xvf3800(find=lambda: _Dev(_values(energy=(0, 0, 0, 5.0))), clock=lambda: t[0])
    assert r.open() is True
    r.poll_once()
    assert r.latest().speech_energy == 5.0
    t[0] = 1.0
    assert r.latest(max_age_s=0.25) is None


def test_no_board_or_no_permission_means_unavailable_and_nothing_breaks():
    assert xvf.Xvf3800(find=lambda: None).open() is False

    def denied():
        raise PermissionError("Access denied (insufficient permissions)")

    r = xvf.Xvf3800(find=denied)
    assert r.open() is False and r.latest() is None
