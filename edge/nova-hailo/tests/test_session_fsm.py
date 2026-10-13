"""SessionFSM transitions the realtime door relies on."""
from nova_hailo.session_fsm import SessionFSM, SessionState, TurnTerminal


def test_force_idle_ends_a_running_turn_from_any_state():
    seen = []
    fsm = SessionFSM(on_change=lambda snap: seen.append(snap["state"]))
    fsm.arm()
    fsm.begin_listen()
    ctx = fsm.begin_turn()
    fsm.set_thinking(ctx.generation_id)
    fsm.set_speaking(ctx.generation_id)
    fsm.force_idle()
    assert fsm.state == SessionState.IDLE and seen[-1] == "IDLE"
    assert ctx.terminal == TurnTerminal.CANCELLED
    assert not fsm.is_current(ctx.generation_id)      # late output of that turn is dropped
    assert fsm.complete(ctx.generation_id, TurnTerminal.COMPLETED) is False
    assert fsm.maybe_expire_arm() is False             # no follow-up window left behind


def test_force_idle_when_already_idle_emits_nothing():
    seen = []
    fsm = SessionFSM(on_change=lambda snap: seen.append(snap["state"]))
    fsm.force_idle()
    assert fsm.state == SessionState.IDLE and seen == []
