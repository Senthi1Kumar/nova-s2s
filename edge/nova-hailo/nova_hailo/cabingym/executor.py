# nova_hailo/cabingym/executor.py
"""Runs routed vehicle calls: gate → handler → store → adapter.

One executor per gateway/episode. A confirm_first call is held as a
pending confirmation for ``confirm_ttl_s``; any new routed turn drops it.
Callers: nova_hailo.edge_harness.tool_broker (via OemToolGateway),
cabingym.env.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nova_hailo.cabingym import gate
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.cabingym.types import RouteOutcome, VehicleCall
from nova_hailo.cabingym.vss import Change, SignalError

logger = logging.getLogger(__name__)


@dataclass
class PendingConfirm:
    calls: list[VehicleCall]
    expires_at: float
    trace: dict[str, Any] = field(default_factory=dict)


def _status(verdicts: list[str]) -> str:
    for verdict, status in (("execute", "success"), ("confirm_first", "confirm"),
                            ("clarify", "clarify"), ("refuse", "refused")):
        if verdict in verdicts:
            return status
    return "error"


class VehicleExecutor:
    def __init__(
        self, cabin: CabinEnv, *, clock: Callable[[], float] = time.monotonic,
        confirm_ttl_s: float = 30.0,
    ):
        self.cabin = cabin
        self._clock = clock
        self._ttl = float(confirm_ttl_s)
        self._pending: PendingConfirm | None = None

    @property
    def confirm_ttl_s(self) -> float:
        """How long a question waits: it has to be spoken, the mic waits out its
        echo, then the driver answers -- 10 s ran out on real drives."""
        return self._ttl

    @property
    def pending(self) -> PendingConfirm | None:
        if self._pending is not None and self._clock() > self._pending.expires_at:
            self._pending = None
        return self._pending

    def drop_pending(self) -> None:
        self._pending = None

    def offer(self, calls: list[VehicleCall], *, ttl_s: float,
              trace: dict[str, Any] | None = None) -> None:
        """Hold calls Nova proposed (not asked for) until the driver says yes or no."""
        self._pending = PendingConfirm(list(calls), self._clock() + ttl_s, dict(trace or {}))

    def run(self, outcome: RouteOutcome) -> dict[str, Any]:
        self._pending = None
        if outcome.kind == "clarify":
            return self._result("clarify", outcome.speak or "Could you say that again?",
                                [], ["clarify"], [], outcome.trace)
        if outcome.kind == "decline":
            return self._result("declined", outcome.speak or "That isn't available in this car.",
                                [], ["decline"], [], outcome.trace)
        return self._execute(outcome.calls, trace=outcome.trace, confirmed=False)

    def resolve_confirm(self, yes: bool) -> dict[str, Any] | None:
        pending = self.pending
        self._pending = None
        if pending is None:
            return None
        if not yes:
            return self._result("cancelled", "Okay, I won't.", [], ["cancelled"], [],
                                {**pending.trace, "confirmation": "no"})
        return self._execute(pending.calls, trace={**pending.trace, "confirmation": "yes"},
                             confirmed=True)

    def _execute(
        self, calls: list[VehicleCall], *, trace: dict[str, Any], confirmed: bool
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        verdicts: list[str] = []
        speaks: list[str] = []
        diff: list[Change] = []
        held: list[VehicleCall] = []
        for call in calls:
            row = call.to_dict()
            rows.append(row)
            verdict = gate.EXECUTE if confirmed else gate.check(call, self.cabin.store.snapshot())
            row["gate"] = verdict.to_dict()
            if verdict.verdict == "refuse":
                row["status"] = "refused"
                verdicts.append("refuse")
                speaks.append(verdict.speak or "I can't do that right now.")
                continue
            if verdict.verdict == "confirm_first":
                row["status"] = "pending_confirm"
                verdicts.append("confirm_first")
                held.append(call)
                speaks.append(verdict.speak or "Should I go ahead?")
                continue
            api = self.cabin.devices[call.device].apis[call.api]
            args = {**call.args, **verdict.arg_overrides}
            try:
                res = api.handler(self.cabin.context(), args)
            except Exception:  # noqa: BLE001 -- fail closed, store untouched
                logger.exception("cabingym:handler_failed %s.%s", call.device, call.api)
                row["status"] = "error"
                verdicts.append("error")
                speaks.append("I couldn't do that.")
                continue
            if res.status != "success":
                row["status"] = "clarify"
                verdicts.append("clarify")
                speaks.append(res.speak)
                continue
            try:
                changes = self.cabin.store.set_targets(res.targets) if res.targets else []
            except SignalError as exc:
                logger.info("cabingym:invalid_target %s", exc)
                row["status"] = "invalid_args"
                verdicts.append("clarify")
                speaks.append("I can't set it to that.")
                continue
            diff.extend(changes)
            row["status"] = "success"
            verdicts.append("execute")
            speaks.append(self._honest(res.speak, res.targets))
        if held:
            self._pending = PendingConfirm(held, self._clock() + self._ttl, dict(trace))
        return self._result(_status(verdicts), " ".join(s for s in speaks if s), rows, verdicts,
                            diff, trace)

    def _honest(self, speak: str, targets: dict[str, Any]) -> str:
        if not self.cabin.hybrid or not targets:
            return speak
        return speak.rstrip(".") + " in the simulated cabin."

    @staticmethod
    def _result(
        status: str, speak: str, rows: list[dict[str, Any]], verdicts: list[str],
        diff: list[Change], trace: dict[str, Any],
    ) -> dict[str, Any]:
        diff_rows = [ch.to_dict() for ch in diff]
        full_trace = {**trace, "calls": rows, "gate": verdicts, "diff": diff_rows, "speak": speak}
        return {
            "ok": True, "name": "vehicle", "status": status, "speak": speak,
            "result": {"calls": rows, "verdicts": verdicts, "diff": diff_rows, "trace": full_trace},
        }
