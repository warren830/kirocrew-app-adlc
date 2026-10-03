"""Canonical Guided Workshop Run state machine (upstream 00→13).

This module is deterministic: it defines ordering and legal transitions only. Remote execution is a
separate, allow-listed SSM adapter; 99-cleanup is intentionally absent because it requires its own
destructive confirmation flow.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class GuideStep:
    id: str
    label: str
    script: str
    depends_on: tuple[str, ...] = ()


def _steps() -> tuple[GuideStep, ...]:
    rows = (
        ("setup", "Environment setup", "00-setup.sh"),
        ("infra", "Deploy infrastructure", "00-deploy-infra.sh"),
        ("knowledge-base", "Create knowledge base", "01-create-kb.sh"),
        ("gateway", "Create Gateway", "02-create-gateway.sh"),
        ("skills", "Configure Skills", "03-configure-skills.sh"),
        ("agent", "Deploy Agent", "04-deploy.sh"),
        ("memory", "Configure Memory", "05-setup-memory.sh"),
        ("conversation", "Conversation smoke", "06-test-conversation.sh"),
        ("eval-env", "Evaluation environment", "07-setup-eval-env.sh"),
        ("evaluators", "Create evaluators", "08-create-evaluators.sh"),
        ("baseline", "Run baseline evaluation", "09-run-eval.sh"),
        ("optimize", "Optimize prompt and re-evaluate", "10-optimize-prompt.sh"),
        ("cost-latency", "Cost and latency", "11-cost-latency.sh"),
        ("models", "Compare models", "12-compare-models.sh"),
        ("judge-stability", "Judge stability", "13-judge-stability.sh"),
    )
    out: list[GuideStep] = []
    for i, (sid, label, script) in enumerate(rows):
        out.append(GuideStep(sid, label, script, () if i == 0 else (rows[i - 1][0],)))
    return tuple(out)

GUIDE_STEPS = _steps()
STEP_BY_ID = {step.id: step for step in GUIDE_STEPS}


def initial_state() -> dict[str, dict[str, Any]]:
    return {
        step.id: {
            "status": "not_started",
            "label": step.label,
            "script": step.script,
            "commandId": None,
            "ssmStatus": None,
            "documentVersion": None,
            "startedAt": None,
            "finishedAt": None,
            "durationSeconds": None,
            "summary": "",
            "error": "",
            "outputs": {},
        }
        for step in GUIDE_STEPS
    }


def runnable(step_id: str, state: dict[str, dict[str, Any]]) -> bool:
    step = STEP_BY_ID[step_id]
    return state[step_id]["status"] in ("not_started", "failed", "blocked") and all(state[d]["status"] == "passed" for d in step.depends_on)


def next_runnable(state: dict[str, dict[str, Any]]) -> str | None:
    return next((step.id for step in GUIDE_STEPS if runnable(step.id, state)), None)


def start(step_id: str, state: dict[str, dict[str, Any]], at: str) -> None:
    if not runnable(step_id, state):
        raise ValueError(f"step {step_id} is not runnable")
    state[step_id].update(
        status="running",
        commandId=None,
        ssmStatus=None,
        documentVersion=None,
        startedAt=at,
        finishedAt=None,
        durationSeconds=None,
        summary="",
        error="",
        outputs={},
    )


def finish(step_id: str, state: dict[str, dict[str, Any]], *, passed: bool, at: str, duration: float, summary: str = "", error: str = "", outputs: dict[str, Any] | None = None) -> None:
    if state[step_id]["status"] != "running":
        raise ValueError(f"step {step_id} is not running")
    state[step_id].update(status="passed" if passed else "failed", finishedAt=at, durationSeconds=max(0, duration), summary=summary, error=error, outputs=dict(outputs or {}))
    if not passed:
        seen = False
        for step in GUIDE_STEPS:
            if step.id == step_id:
                seen = True
            elif seen and state[step.id]["status"] == "not_started":
                state[step.id]["status"] = "blocked"
