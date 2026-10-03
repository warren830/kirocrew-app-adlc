"""Deterministic final report for the 00→13 Guided Workshop Run.

Inputs are the Guided Run state (the step outputs the RunStep document parsed on the Workshop host)
and the scenario **as the release was built** (``build/instructor/scenario-snapshot.*``), never the
live ``scenario.yaml``.

Besides the per-evaluator summary, the report carries two per-case views (SPEC D8):

* :func:`case_table` — for every practice case and phase (baseline / optimized / comparison): the L1
  verdict with its fail/defer codes, THELMA ``{value, label, metrics}``, Mind the Goal ``{label}``
  and the combined verdict (:func:`l1.combined_verdict`). Scores are attributed through the
  ``caseId`` the RunStep parser read from the ``case=`` tags of the Score lines.
* ``teachingContrast`` — :func:`rehearsal.teaching_contrast` (re-exported here), a view of the
  ``labs.teaching`` phenomena on the current run, judged with the single threshold table
  ``teaching.THRESHOLDS`` and its helpers. The rules live in rehearsal.py (SPEC D2); the view carries
  no readiness boolean: only ``run/rehearsal.json`` decides whether the pack is ready for class.

``completion.missingEvidence`` stays evidence-only: :func:`step_evidence_errors` adds the per-case
L1 and Mind the Goal coverage a teaching pack's baseline and optimize steps must deliver.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any, Mapping

from . import teaching
from .calibration import is_real_change
from .l1 import GOAL_EVALUATOR, L1_SCHEMA, RAG_EVALUATOR, combined_verdict
# The teaching verdict rules have one owner, rehearsal.py (SPEC D2); the report re-exports them.
from .rehearsal import (  # noqa: F401  (re-exported)
    CONTRAST_STATUSES,
    FOCUS_CHECKS,
    HONESTY_CHECKS,
    READINESS_NOTE,
    TEACHING_CONTRAST_SCHEMA,
    teaching_contrast,
)
from .rehearsal import _number
from .scenario import provenance_policy

PRIMARY_EVALUATOR = RAG_EVALUATOR
#: Report phases, in order, and the Guided Run step each one is read from.
PHASE_STEPS: tuple[tuple[str, str], ...] = (("baseline", "baseline"), ("optimized", "optimize"), ("comparison", "models"))
CASE_TABLE_SCHEMA = "workshop-customizer/case-table/1"


def _usable(row: Any) -> bool:
    return (isinstance(row, dict) and _number(row.get("value"))
            and str(row.get("label", "")).lower() not in ("skipped", "error"))


def _score_summary(outputs: dict[str, Any]) -> dict[str, Any]:
    rows = [row for row in outputs.get("scores", []) if _usable(row)]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("evaluator") or "unknown")].append(row)
    by_evaluator: dict[str, Any] = {}
    for evaluator, items in sorted(grouped.items()):
        values = [float(item["value"]) for item in items]
        labels = [str(item.get("label") or "").lower() for item in items]
        by_evaluator[evaluator] = {
            "count": len(items),
            "mean": round(mean(values), 6),
            "min": min(values),
            "max": max(values),
            "passRate": round(sum(label == "pass" for label in labels) / len(labels), 6),
        }
    return {"cases": rows, "byEvaluator": by_evaluator}


def _configured_noise_band(scenario: dict[str, Any]) -> tuple[float | None, str]:
    configured = (scenario.get("evaluation") or {}).get("noiseBand") or {}
    if isinstance(configured, (int, float)):
        return float(configured), "scenario.evaluation.noiseBand"
    if isinstance(configured, dict):
        value = configured.get(PRIMARY_EVALUATOR)
        if isinstance(value, (int, float)):
            return float(value), f"scenario.evaluation.noiseBand.{PRIMARY_EVALUATOR}"
    return None, ""


def effective_noise_band(steps: Mapping[str, Any], scenario: dict[str, Any]) -> tuple[float | None, str]:
    """The judge noise band: the scenario's calibrated value, else ``max(2σ, spread, 0.02)`` of 13."""
    band, source = _configured_noise_band(scenario)
    if band is not None:
        return band, source
    stability = ((steps.get("judge-stability") or {}).get("outputs") or {}).get("judgeStability") or {}
    if _number(stability.get("std")) and _number(stability.get("spread")):
        return (round(max(2 * float(stability["std"]), float(stability["spread"]), 0.02), 6),
                "13-judge-stability.sh (max(2σ, spread, 0.02))")
    return None, ""


# ---------------------------------------------------------------------------
# Per-case evidence from the step outputs
# ---------------------------------------------------------------------------


def l1_evidence(outputs: Mapping[str, Any]) -> dict[str, Any]:
    """The compact L1 result of one step: ``status`` present | truncated | missing | invalid.

    ``cases`` maps case id → compact row ``{id, v, fail, defer, unverified, error?}`` (empty unless
    present). ``error`` carries the parser's ``l1Error`` when it could not read the compact file.
    """
    raw = outputs.get("l1")
    error = outputs.get("l1Error") if isinstance(outputs.get("l1Error"), str) else None
    if not isinstance(raw, dict):
        return {"status": "invalid" if error else "missing", "error": error, "phase": None, "runId": None,
                "releaseVersion": None, "summary": None, "cases": {}}
    if raw.get("schema") != L1_SCHEMA or not isinstance(raw.get("cases"), list) or not isinstance(raw.get("summary"), dict):
        return {"status": "invalid", "error": error or "unexpected l1 schema", "phase": raw.get("phase"), "runId": raw.get("runId"),
                "releaseVersion": raw.get("releaseVersion"), "summary": None, "cases": {}}
    cases = {row["id"]: row for row in raw["cases"] if isinstance(row, dict) and isinstance(row.get("id"), str)}
    return {"status": "truncated" if raw.get("truncated") else "present", "error": error, "phase": raw.get("phase"),
            "runId": raw.get("runId"), "releaseVersion": raw.get("releaseVersion"), "summary": raw["summary"], "cases": cases}


def case_scores(outputs: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """``caseId → evaluator → {value, label, metrics?, traceId, sessionId?}``: the first usable row each."""
    by_case: dict[str, dict[str, dict[str, Any]]] = {}
    for row in outputs.get("scores") or []:
        if not _usable(row) or not isinstance(row.get("caseId"), str):
            continue
        evaluator = str(row.get("evaluator") or "unknown")
        slot = by_case.setdefault(row["caseId"], {})
        if evaluator in slot:
            continue
        entry: dict[str, Any] = {"value": float(row["value"]), "label": str(row.get("label") or ""), "traceId": row.get("traceId")}
        if isinstance(row.get("sessionId"), str):
            entry["sessionId"] = row["sessionId"]
        metrics = row.get("metrics")
        if isinstance(metrics, dict):
            entry["metrics"] = {k: float(v) for k, v in metrics.items() if _number(v)}
        slot[evaluator] = entry
    return by_case


def _practice(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """Practice cases (goldenSet order) that carry an id; tolerant of partial legacy scenarios."""
    return [c for c in teaching.practice_cases(scenario) if isinstance(c.get("id"), str)]


def _codes(row: Mapping[str, Any], key: str) -> list[str]:
    return [str(code) for code in row.get(key) or [] if isinstance(code, str)]


def _phase_cell(l1: Mapping[str, Any], scores: Mapping[str, Mapping[str, Any]], case_id: str) -> dict[str, Any]:
    row = l1["cases"].get(case_id) if l1["status"] == "present" else None
    l1_cell = None if row is None else {
        "verdict": row.get("v"), "fail": _codes(row, "fail"), "defer": _codes(row, "defer"),
        "unverified": _codes(row, "unverified"), "error": _codes(row, "error"),
    }
    rag, goal = scores.get(RAG_EVALUATOR), scores.get(GOAL_EVALUATOR)
    thelma = None if rag is None else {"value": rag["value"], "label": rag["label"], "metrics": dict(rag.get("metrics") or {})}
    mtg = None if goal is None else {"value": goal["value"], "label": goal["label"]}
    return {
        "l1": l1_cell,
        "thelma": thelma,
        "mtg": mtg,
        "combined": combined_verdict(l1_cell["verdict"] if l1_cell else None, mtg["label"] if mtg else None),
    }


def case_table(scenario: dict[str, Any], steps: Mapping[str, Any]) -> dict[str, Any]:
    """Every practice case × phase: L1 verdict (+codes), THELMA, Mind the Goal and the combined verdict.

    ``scenario`` must be the build snapshot the release was rendered from. Cases are listed in
    goldenSet order; ``evalIndex`` is the 1-based position 09/10/12 asked the case in.
    """
    practice = _practice(scenario)
    declared = teaching.declared(scenario)
    probes = set(teaching.probe_case_ids(scenario)) if declared else set()
    kinds = teaching.case_kinds(scenario) if declared else {}
    ids = teaching.eval_order_ids(scenario) if declared else tuple(c["id"] for c in practice)
    order = {cid: i + 1 for i, cid in enumerate(ids)}
    phases: dict[str, Any] = {}
    evidence: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for phase, step_id in PHASE_STEPS:
        outputs = (steps.get(step_id) or {}).get("outputs") or {}
        l1 = l1_evidence(outputs)
        scores = case_scores(outputs)
        evidence[phase] = (l1, scores)
        phases[phase] = {"step": step_id, "l1": l1["status"], "l1Error": l1["error"], "l1Phase": l1["phase"],
                         "runId": l1["runId"], "l1Summary": l1["summary"],
                         "scoredCases": {ev: sorted(cid for cid, s in scores.items() if ev in s) for ev in (RAG_EVALUATOR, GOAL_EVALUATOR)}}
    cases = []
    for case in practice:
        cid = case["id"]
        row: dict[str, Any] = {
            "caseId": cid, "label": case.get("label"), "category": case.get("category"),
            "provenance": case.get("provenance"), "evalIndex": order.get(cid), "probe": cid in probes,
            "kinds": list(kinds.get(cid, ())),
        }
        for phase, _step in PHASE_STEPS:
            l1, scores = evidence[phase]
            row[phase] = _phase_cell(l1, scores.get(cid, {}), cid)
        cases.append(row)
    return {"schema": CASE_TABLE_SCHEMA, "phases": phases, "cases": cases}


# ---------------------------------------------------------------------------
# Evidence gates and the report
# ---------------------------------------------------------------------------


def _per_case_evidence_errors(step_id: str, outputs: Mapping[str, Any], scenario: dict[str, Any]) -> list[str]:
    phase, script = ("baseline", "09-run-eval.sh") if step_id == "baseline" else ("optimized", "10-optimize-prompt.sh")
    practice = [c["id"] for c in _practice(scenario)]
    scores = case_scores(outputs)
    errors: list[str] = []
    l1 = l1_evidence(outputs)
    if l1["status"] in ("missing", "invalid"):
        detail = f" ({l1['error']})" if l1["error"] else ""
        if not scores:
            detail += (" — no case-tagged scores either: the add-ons RunStep document is probably stale; "
                       "redeploy sync/cfn/customizer-addons.json and run preflight again")
        errors.append(f"L1 scenario assertions (every practice case) from {script}{detail}")
    elif l1["status"] == "truncated":
        errors.append(f"L1 verdicts for every practice case ({phase}; the compact L1 result was truncated to fit the step output)")
    else:
        if l1["phase"] != phase:
            errors.append(f"L1 verdicts for the {phase} run (the L1 result is for phase {l1['phase']})")
        missing = [cid for cid in practice if cid not in l1["cases"]]
        if missing:
            errors.append(f"L1 verdicts for every practice case ({phase}; missing: {', '.join(missing)})")
    unjudged = [cid for cid in practice if GOAL_EVALUATOR not in scores.get(cid, {})]
    if unjudged:
        errors.append(f"Mind the Goal verdicts for every practice case ({phase}; missing: {', '.join(unjudged)})")
    return errors


def step_evidence_errors(step_id: str, outputs: dict[str, Any], scenario: dict[str, Any] | None = None) -> list[str]:
    """A successful process exit cannot substitute for the metrics a guide step promises.

    With the build ``scenario`` of a pack that declares ``labs.teaching``, baseline and optimize must
    also carry a compact L1 verdict and a case-tagged Mind the Goal score for every practice case.
    """
    if step_id in ("baseline", "optimize"):
        errors: list[str] = []
        if PRIMARY_EVALUATOR not in _score_summary(outputs)["byEvaluator"]:
            errors.append(f"{'baseline' if step_id == 'baseline' else 'optimized'} evaluation scores from "
                          f"{'09-run-eval.sh' if step_id == 'baseline' else '10-optimize-prompt.sh'}")
        if scenario is not None and teaching.declared(scenario):
            errors.extend(_per_case_evidence_errors(step_id, outputs, scenario))
        return errors
    if step_id == "cost-latency":
        metrics = outputs.get("costLatency") or {}
        positive = ("averageLatencySeconds", "averageInputTokens", "averageOutputTokens")
        if (not isinstance(metrics, dict)
                or not all(_number(metrics.get(key)) and metrics[key] > 0 for key in positive)
                or not _number(metrics.get("totalCostUsd")) or metrics["totalCostUsd"] < 0):
            return ["latency/token/cost metrics from 11-cost-latency.sh"]
    elif step_id == "models":
        models = outputs.get("models") or {}
        if (not isinstance(models, dict)
                or not all(isinstance(models.get(key), str) and models[key].strip() for key in ("baseline", "comparison"))
                or PRIMARY_EVALUATOR not in _score_summary(outputs)["byEvaluator"]):
            return ["model identity and comparison scores from 12-compare-models.sh"]
    elif step_id == "judge-stability":
        stability = outputs.get("judgeStability") or {}
        values = stability.get("values", []) if isinstance(stability, dict) else []
        if (not isinstance(values, list) or len(values) < 2 or not all(_number(value) for value in values)
                or not all(_number(stability.get(key)) for key in ("mean", "std", "spread"))
                or not stability.get("verdict")):
            return ["judge stability values/noise band from 13-judge-stability.sh"]
    return []


#: A direct run (``state["mode"] == "direct"``, :mod:`workshop_customizer.direct`) may leave out the model
#: comparison: its report is complete without one, and it is never a class verdict anyway.
DIRECT_OPTIONAL_STEPS = ("models",)


def build_report(state: dict[str, Any], scenario: dict[str, Any], *, generated_at: str) -> dict[str, Any]:
    """The final report. ``scenario`` is the build snapshot of the release the run executed."""
    steps = state["steps"]
    baseline = _score_summary(steps["baseline"].get("outputs") or {})
    optimized = _score_summary(steps["optimize"].get("outputs") or {})
    cost_latency = ((steps.get("cost-latency") or {}).get("outputs") or {}).get("costLatency") or {}
    model_outputs = (steps.get("models") or {}).get("outputs") or {}
    model_scores = _score_summary(model_outputs)
    # A run without 13's evidence (a direct round whose stability question never answered) is incomplete, not a crash.
    stability = ((steps.get("judge-stability") or {}).get("outputs") or {}).get("judgeStability") or {}

    band, band_source = effective_noise_band(steps, scenario)

    deltas: dict[str, Any] = {}
    regressions: list[dict[str, Any]] = []
    comparison_warnings: list[str] = []
    common = sorted(set(baseline["byEvaluator"]) & set(optimized["byEvaluator"]))
    for evaluator in common:
        before = baseline["byEvaluator"][evaluator]["mean"]
        after = optimized["byEvaluator"][evaluator]["mean"]
        delta = round(after - before, 6)
        before_count = baseline["byEvaluator"][evaluator]["count"]
        after_count = optimized["byEvaluator"][evaluator]["count"]
        counts_match = before_count == after_count
        meaningful = counts_match and is_real_change(delta, band)
        deltas[evaluator] = {"baselineMean": before, "optimizedMean": after, "delta": delta,
                             "baselineCount": before_count, "optimizedCount": after_count,
                             "sampleCountsMatch": counts_match, "clearsNoiseBand": meaningful}
        if not counts_match:
            comparison_warnings.append(
                f"{evaluator}: baseline has {before_count} usable scores and optimized has {after_count}; "
                "the mean difference is descriptive only, not evidence of improvement or regression."
            )
        if delta < 0 and meaningful:
            regressions.append({"evaluator": evaluator, "baselineMean": before, "optimizedMean": after, "delta": delta, "noiseBand": band})

    evaluation = scenario.get("evaluation") or {}
    golden = evaluation.get("goldenSet") or []
    tools = scenario.get("tools") or []
    missing: list[str] = []
    optional = DIRECT_OPTIONAL_STEPS if state.get("mode") == "direct" else ()
    for step_id in ("baseline", "optimize", "cost-latency", "models", "judge-stability"):
        if step_id in optional and step_id not in steps:
            continue
        missing.extend(step_evidence_errors(step_id, (steps.get(step_id) or {}).get("outputs") or {}, scenario))
    if band is None and "judge stability values/noise band from 13-judge-stability.sh" not in missing:
        missing.append("judge stability values/noise band from 13-judge-stability.sh")

    table = case_table(scenario, steps)
    contrast = teaching_contrast(scenario, table, band=band, band_source=band_source,
                                 conversation=((steps.get("conversation") or {}).get("outputs") or {}).get("conversation"))

    all_steps_passed = all(step.get("status") == "passed" for step in steps.values())
    report = {
        "schemaVersion": 1,
        "status": "complete" if all_steps_passed and not missing else "incomplete",
        "generatedAt": generated_at,
        "projectId": state["projectId"],
        "releaseVersion": state["releaseVersion"],
        "templateCommit": state["templateCommit"],
        "completion": {
            "allStepsPassed": all_steps_passed,
            "passed": sum(step.get("status") == "passed" for step in steps.values()),
            "total": len(steps),
            "missingEvidence": missing,
        },
        "quality": {"baseline": baseline, "optimized": optimized, "delta": deltas,
                    "regressions": regressions, "comparisonWarnings": comparison_warnings,
                    "l1": {phase: table["phases"][phase]["l1Summary"] for phase in ("baseline", "optimized")}},
        "caseTable": table,
        "teachingContrast": contrast,
        # SPEC D12: the provenance policy of the build snapshot and its customer anchors.
        "provenancePolicy": provenance_policy(scenario),
        "agentEvidence": {
            "tools": [{"name": tool.get("name"), "description": tool.get("description")} for tool in tools if isinstance(tool, dict)],
            "retrievalTool": evaluation.get("retrievalToolName"),
            "judgeModel": evaluation.get("judgeModel"),
            "goldenSet": {
                "total": len(golden),
                "practice": sum(case.get("set") == "practice" for case in golden if isinstance(case, dict)),
                "holdout": sum(case.get("set") == "holdout" for case in golden if isinstance(case, dict)),
                "categories": {category: sum(case.get("category") == category for case in golden if isinstance(case, dict)) for category in ("normal", "boundary", "prohibited")},
            },
        },
        "operations": {"costLatency": cost_latency},
        "modelComparison": {"models": model_outputs.get("models") or {}, "scores": model_scores, "costLatency": model_outputs.get("costLatency") or {}},
        "judgeStability": {**stability, "noiseBand": band, "noiseBandSource": band_source},
        "stepEvidence": [
            {
                "id": step_id,
                "script": steps[step_id].get("script"),
                "status": steps[step_id].get("status"),
                "commandId": steps[step_id].get("commandId"),
                "ssmStatus": steps[step_id].get("ssmStatus"),
                "documentVersion": steps[step_id].get("documentVersion"),
                "durationSeconds": steps[step_id].get("durationSeconds"),
            }
            for step_id in state["stepOrder"]
        ],
        "scopeWarning": "This report validates the Workshop run and Scenario Pack; it is not a production launch approval.",
    }
    return report
