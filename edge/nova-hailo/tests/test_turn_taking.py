"""Who has the turn while Nova talks: a backchannel, an interruption, or Nova's own voice.
The XVF3800's speech energy is measured after its echo canceller, so it rises for the driver
and not for Nova."""
import pytest

from nova_hailo.web.turn_taking import TurnTaker, classify_short
from nova_hailo.xvf3800 import BoardState


def _board(energy=0.0, converged=True, path=False):
    return BoardState(0.0, (0.0, 0.0, 0.0, energy), converged, path)


def _run(tt, frames, *, audible=True):
    """frames: (seconds, vad_speech, board energy) at 20 ms steps; returns the decisions."""
    out, t = [], 0.0
    for dur, vad, energy in frames:
        for _ in range(int(round(dur / 0.02))):
            out.append(tt.frame(t, nova_audible=audible, vad_speech=vad, board=_board(energy)))
            t += 0.02
    return out


def test_the_driver_talking_over_nova_takes_the_turn_after_the_hold():
    tt = TurnTaker(hold_s=0.5)
    out = _run(tt, [(0.2, False, 0), (0.7, True, 9e5)])
    assert "barge" in out
    first = out.index("barge")
    assert 0.2 + 0.48 <= first * 0.02 <= 0.2 + 0.54     # about 500 ms into the speech
    assert out.count("barge") == 1                       # decided once


def test_the_vad_decides_even_when_the_board_energy_is_zero():
    """Board energy overlaps between Nova and the driver, while the VAD on
    the board's echo-cancelled audio never fired on Nova alone. The VAD decides."""
    tt = TurnTaker(hold_s=0.5)
    assert "barge" in _run(tt, [(1.0, True, 0)])


def test_a_short_mm_hm_is_left_for_its_words():
    tt = TurnTaker(hold_s=0.5)
    out = _run(tt, [(0.35, True, 8e5), (0.5, False, 0)])
    assert "barge" not in out
    assert tt.last_short_s == pytest.approx(0.36, abs=0.04)


def test_an_unsettled_canceller_falls_back_to_the_old_rule_and_no_board_trusts_the_vad():
    tt = TurnTaker()
    assert tt.frame(0.0, nova_audible=True, vad_speech=True, board=None) == "listening"
    assert tt.frame(0.0, nova_audible=True, vad_speech=True, board=_board(9e5, converged=False)) == "fallback"
    assert tt.frame(0.0, nova_audible=True, vad_speech=True, board=_board(9e5, path=True)) == "fallback"


def test_nothing_happens_while_nova_is_quiet():
    tt = TurnTaker(hold_s=0.5)
    assert set(_run(tt, [(1.0, True, 9e5)], audible=False)) == {"idle"}


@pytest.mark.parametrize("text,kind", [
    ("Mm-hm.", "backchannel"), ("uh huh", "backchannel"), ("Okay.", "backchannel"), ("Yeah, right.", "backchannel"),
    ("Got it.", "backchannel"),
    ("Stop.", "barge"), ("Wait.", "barge"), ("No.", "barge"), ("Hey Nova", "barge"), ("Okay stop", "barge"),
    ("Take me home instead", "barge"), ("", "ignore"),
])
def test_what_a_short_utterance_over_nova_was(text, kind):
    assert classify_short(text) == kind


def test_a_long_utterance_leaves_no_short_length_behind():
    tt = TurnTaker(hold_s=0.5)
    _run(tt, [(0.3, True, 8e5), (0.4, False, 0)])
    assert tt.last_short_s > 0
    _run(tt, [(0.8, True, 8e5), (0.4, False, 0)])
    assert tt.last_short_s == 0.0



# ---- V2b: duck at the first syllable, then decide --------------------------------------

from nova_hailo.web.turn_taking import Duck  # noqa: E402


def test_speech_that_goes_on_past_the_hold_takes_the_turn():
    d = Duck(hold_s=0.5)
    assert d.start(10.0) is True and d.active
    assert d.tick(10.3, vad_speech=True, nova_audible=True) == "none"
    assert d.tick(10.52, vad_speech=True, nova_audible=True) == "barge"
    assert not d.active


def test_a_short_backchannel_restores_nova_and_a_stop_word_takes_the_turn():
    d = Duck()
    d.start(0.0)
    assert d.short_ended("Mm-hm.") == "restore" and not d.active
    d.start(5.0)
    assert d.short_ended("Stop.") == "barge"
    d.start(9.0)
    assert d.short_ended("") == "restore"


def test_the_duck_never_outlives_novas_speech_or_its_timeout():
    d = Duck(timeout_s=3.0)
    d.start(0.0)
    assert d.tick(1.0, vad_speech=False, nova_audible=False) == "restore"
    d.start(10.0)
    assert d.tick(13.1, vad_speech=False, nova_audible=True) == "restore"
    assert d.tick(14.0, vad_speech=True, nova_audible=True) == "none"    # nothing active any more


def test_starting_twice_keeps_the_first_syllable_time():
    d = Duck(hold_s=0.5)
    assert d.start(1.0) is True
    assert d.start(1.2) is False
    assert d.tick(1.51, vad_speech=True, nova_audible=True) == "barge"


def test_a_short_mm_hm_with_the_vads_hangover_is_not_a_barge():
    """Silero stays "in speech" for 600 ms after the voice stops, so measuring
    elapsed time turned every 0.3 s "mm-hm" into a barge. Only voiced frames count."""
    d = Duck(hold_s=0.5)
    d.start(0.0)
    t, out = 0.0, []
    for i in range(45):                      # 0.3 s voiced, then 0.6 s of hangover
        t += 0.02
        out.append(d.tick(t, vad_speech=True, nova_audible=True, voiced=i < 15))
    assert "barge" not in out and d.active


def test_half_a_second_of_real_speech_over_nova_still_takes_the_turn():
    d = Duck(hold_s=0.5)
    d.start(0.0)
    t, out = 0.0, []
    for _ in range(40):
        t += 0.02
        out.append(d.tick(t, vad_speech=True, nova_audible=True, voiced=True))
    assert "barge" in out
    assert 0.48 <= (out.index("barge") + 1) * 0.02 <= 0.56
