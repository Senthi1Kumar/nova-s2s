from aibox.voice.turn_controller import TurnAction, TurnController
from aibox.voice.turn_judge import TurnQuestions


def audio(stop=True, silence=400, agent="listening", revision=1, elapsed=0):
    return {
        "stop_requested": stop,
        "silence_ms": silence,
        "agent_state": agent,
        "revision": revision,
        "elapsed_ms": elapsed,
        "session_state": "manual",
    }


async def test_unfinished_pause_waits_then_uses_hard_timeout():
    controller = TurnController()
    assert await controller.update("so what I want to know is", audio()) == TurnAction.CONTINUE
    assert await controller.update(
        "so what I want to know is", audio(elapsed=6000, revision=2)
    ) == TurnAction.END_TURN


async def test_explicit_holding_floor_extends_wait():
    controller = TurnController()
    assert await controller.update(
        "hold on let me type something", audio(elapsed=6000)
    ) == TurnAction.CONTINUE


async def test_completed_request_ends_turn():
    controller = TurnController()
    assert await controller.update("what is the weather today", audio()) == TurnAction.END_TURN


async def test_side_talk_is_ignored():
    controller = TurnController()
    assert await controller.update("hey can you hold my coffee please", audio()) == TurnAction.IGNORE


async def test_backchannel_does_not_interrupt_but_real_interruption_does():
    controller = TurnController()
    assert await controller.update("right", audio(agent="speaking")) == TurnAction.IGNORE
    assert await controller.update(
        "wait wait let me stop you", audio(agent="speaking", revision=2)
    ) == TurnAction.INTERRUPT


async def test_stale_revision_cannot_end_turn():
    controller = TurnController()
    await controller.update("what is the weather", audio(revision=4))
    assert await controller.update("what is the weather", audio(revision=3)) == TurnAction.CONTINUE


async def test_new_turn_reset_allows_revision_one_again():
    controller = TurnController()
    await controller.update("what is the weather", audio(revision=4))
    controller.reset()
    assert await controller.update("what is the weather", audio(revision=1)) == TurnAction.END_TURN


async def test_unavailable_model_falls_back_to_timeout():
    class BrokenJudge:
        async def judge(self, state, questions):
            raise RuntimeError("model unavailable")

    controller = TurnController(judge=BrokenJudge())
    assert await controller.update("what is the weather today", audio()) == TurnAction.END_TURN


async def test_state_contains_manual_session_and_partial_revision():
    class CaptureJudge:
        def __init__(self):
            self.state = None
            self.questions = None

        async def judge(self, state, questions):
            self.state = state
            self.questions = questions
            return {"turn_complete": 0.9, "holding_floor": 0.0,
                    "addressed_to_agent": 0.9, "taking_floor": 0.0}

    judge = CaptureJudge()
    controller = TurnController(judge=judge)
    await controller.update("what is the weather", audio(revision=7))
    assert judge.state["partial_transcript"] == "what is the weather"
    assert judge.state["revision"] == 7
    assert judge.state["session_state"] == "manual"
    assert judge.state["silence_ms"] == 400
    assert set(judge.questions) == set(TurnQuestions.all())


def test_turn_judge_settings_select_backend_and_model():
    from aibox.config import load_settings

    settings = load_settings({"AIBOX_TURN_JUDGE": "d3_edge", "AIBOX_D3_MODEL_ID": "/models/d3"})
    assert settings.turn_judge_backend == "d3_edge"
    assert settings.d3_model_id == "/models/d3"
