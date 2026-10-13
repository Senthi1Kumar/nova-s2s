# nova_hailo/cabingym/scoring.py
"""VehicleWorld-style state-diff scoring plus gate and abstention checks.

F1-positive: paths that should change did, to the right value.
F1-negative: paths that should stay put did (collateral writes cost here).
Acc: every scored path ends exactly where the task expects.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class StepLike(Protocol):
    kind: str
    verdicts: list[str]


@dataclass
class TaskScore:
    task_id: str
    f1_pos: float
    f1_neg: float
    acc: bool
    gate_ok: bool | None
    abstain_true: bool
    abstain_pred: bool
    wrong_paths: list[str]


def _norm(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)):
        return tuple(_norm(v) for v in value)
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    return value


def _f1(p: float, r: float) -> float:
    return 0.0 if p + r == 0 else 2 * p * r / (p + r)


def score_task(
    task: Any, init: Mapping[str, Any], final: Mapping[str, Any],
    infos: Sequence[StepLike], scored_paths: Iterable[str],
) -> TaskScore:
    scored = set(scored_paths)
    expected = dict(init)
    expected.update(task.expected_diff)
    exp_changed = {p for p in task.expected_diff if _norm(init.get(p)) != _norm(task.expected_diff[p])}
    pred_changed = {p for p in scored if _norm(init.get(p)) != _norm(final.get(p))}
    correct = {p for p in exp_changed if _norm(final.get(p)) == _norm(expected[p])}
    if exp_changed or pred_changed:
        prec = len(correct) / len(pred_changed) if pred_changed else 0.0
        rec = len(correct) / len(exp_changed) if exp_changed else 0.0
        f1_pos = _f1(prec, rec)
    else:
        f1_pos = 1.0
    stay = scored - exp_changed
    pred_stay = scored - pred_changed
    tn = len(stay & pred_stay)
    p_neg = tn / len(pred_stay) if pred_stay else 1.0
    r_neg = tn / len(stay) if stay else 1.0
    wrong = sorted(p for p in scored | set(task.expected_diff)
                   if _norm(final.get(p)) != _norm(expected.get(p)))
    checked = [(t, i) for t, i in zip(task.turns, infos) if t.expect_gate is not None]
    gate_ok = None if not checked else all(list(i.verdicts) == list(t.expect_gate) for t, i in checked)
    if checked and len(infos) < sum(1 for t in task.turns if t.expect_gate is not None):
        gate_ok = False
    return TaskScore(
        task_id=task.id, f1_pos=f1_pos, f1_neg=_f1(p_neg, r_neg), acc=not wrong, gate_ok=gate_ok,
        abstain_true=bool({"clarify", "decline"} & set(task.tags)),
        abstain_pred=bool(infos) and infos[0].kind in ("clarify", "decline"),
        wrong_paths=wrong,
    )


def aggregate(scores: Sequence[TaskScore]) -> dict[str, Any]:
    n = len(scores)
    if n == 0:
        return {"n": 0}
    gated = [s for s in scores if s.gate_ok is not None]
    tp = sum(1 for s in scores if s.abstain_true and s.abstain_pred)
    pred = sum(1 for s in scores if s.abstain_pred)
    true = sum(1 for s in scores if s.abstain_true)
    return {
        "n": n,
        "acc": sum(s.acc for s in scores) / n,
        "f1_pos": sum(s.f1_pos for s in scores) / n,
        "f1_neg": sum(s.f1_neg for s in scores) / n,
        "gate_acc": (sum(bool(s.gate_ok) for s in gated) / len(gated)) if gated else None,
        "abstain_precision": tp / pred if pred else None,
        "abstain_recall": tp / true if true else None,
        "failed": [s.task_id for s in scores if not s.acc or s.gate_ok is False],
    }
