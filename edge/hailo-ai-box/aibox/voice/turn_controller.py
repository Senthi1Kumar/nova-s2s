"""Deterministic turn actions from acoustic state and semantic judgments."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from .turn_judge import TimeoutJudge, TurnJudge, TurnQuestions


class TurnAction(StrEnum):
    CONTINUE = "continue"
    END_TURN = "end_turn"
    INTERRUPT = "interrupt"
    IGNORE = "ignore"


class TurnController:
    def __init__(self, judge: TurnJudge | None = None) -> None:
        self.judge = judge or TimeoutJudge()
        self.fallback = TimeoutJudge()
        self._latest_revision = 0

    def reset(self) -> None:
        self._latest_revision = 0

    async def update(
        self, partial: str, audio_state: dict[str, Any], now: float | None = None
    ) -> TurnAction:
        del now
        revision = int(audio_state.get("revision", 0))
        if revision < self._latest_revision:
            return TurnAction.CONTINUE
        self._latest_revision = max(self._latest_revision, revision)
        state = dict(audio_state)
        state["partial_transcript"] = partial.strip()
        try:
            scores = await self.judge.judge(state, TurnQuestions.all())
        except Exception:  # noqa: BLE001 - an unavailable semantic model must not freeze a turn
            scores = await self.fallback.judge(state, TurnQuestions.all())

        if audio_state.get("agent_state") == "speaking":
            if scores["taking_floor"] >= 0.7:
                return TurnAction.INTERRUPT
            if scores["taking_floor"] < 0.3:
                return TurnAction.IGNORE
            return TurnAction.CONTINUE
        if not state["partial_transcript"]:
            return TurnAction.END_TURN if audio_state.get("stop_requested") else TurnAction.CONTINUE
        if scores["addressed_to_agent"] < 0.4:
            return TurnAction.IGNORE
        if scores["holding_floor"] >= 0.85:
            if audio_state.get("elapsed_ms", 0) >= 12000 and audio_state.get("stop_requested"):
                return TurnAction.END_TURN
            return TurnAction.CONTINUE
        if not audio_state.get("stop_requested"):
            return TurnAction.CONTINUE
        if audio_state.get("elapsed_ms", 0) >= 5000:
            return TurnAction.END_TURN
        if scores["holding_floor"] >= 0.65:
            return TurnAction.CONTINUE
        if scores["turn_complete"] >= 0.72 and audio_state.get("silence_ms", 0) >= 250:
            return TurnAction.END_TURN
        return TurnAction.CONTINUE
