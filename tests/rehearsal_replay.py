"""Replay the recorded live rehearsals (docs/replace-generic/evidence/*/rehearsal-*.json) through the engine.

A recorded ``run/rehearsal.json`` carries the per-case evidence it judged (``caseTable``: every practice
case's L1, THELMA and Mind the Goal cells for baseline, optimized and comparison), the teaching
declarations it judged them against (``phenomena`` / ``advisory``) and its inputs (noise band, run status,
guides). :func:`replay_inputs` turns one record back into the arguments of ``rehearsal.build_rehearsal``:

* the Guided Run **state**: every step passed, the baseline / optimize / models outputs rebuilt from the
  case-table cells (scores with caseId and metrics, compact L1 rows), the recorded judge stability, a
  first-conversation excerpt consistent with the recorded memory reading;
* the **scenario**: the evidence directory's ``scenario.yaml`` (the final build of that acceptance run) with
  ``labs.teaching`` set to the declarations the record judged (a first rehearsal judged an earlier build
  whose phenomena Kiro later rewrote; the golden cases, tools and documents are the same in both builds);
* the knowledge **documents**: the evidence ships no knowledge base, so every document of the scenario is
  present (as the record searched them) and a document the record names for a retrieval hint states that
  phenomenon's terms (absentTerms for an absent gap, the case's expected.mustMention otherwise);
* the **guides** and the scenario source / release verification the record reports.

The replay is faithful when :func:`verdict_projection` of the replayed document equals the recorded one:
every code, status, verdict, severity, asset, number and id, with the human-readable text left out
(:data:`TEXT_KEYS`). Offline and deterministic.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "docs" / "replace-generic" / "evidence"
#: The recorded rehearsals of the two acceptance scenarios (first round not ready, second ready for class),
#: written by the English-only engine (workshop-customizer/rehearsal/1). Later recordings are version 2 and
#: already carry the localized text, so there is nothing to replay.
RECORDS: tuple[tuple[str, str], ...] = tuple(
    (path.parent.name, path.name) for path in sorted(EVIDENCE.glob("*/rehearsal-*.json"))
    if json.loads(path.read_text(encoding="utf-8")).get("schema") == "workshop-customizer/rehearsal/1"
)

import sys  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "engine"))
from workshop_customizer import guided_report  # noqa: E402
from workshop_customizer.guided_run import GUIDE_STEPS  # noqa: E402
from workshop_customizer.l1 import GOAL_EVALUATOR, L1_SCHEMA, RAG_EVALUATOR  # noqa: E402

#: Human-readable fields of a rehearsal document (and of report.teachingContrast); everything else is
#: the machine-readable verdict that a change of wording must never touch.
TEXT_KEYS = frozenset({
    "reason", "reasonEn", "action", "actionEn", "because", "becauseEn", "message", "messageEn", "note", "noteEn",
    "warning", "warningEn", "scopeWarning", "scopeWarningEn", "language", "blockerTexts",
})


#: The human-readable fields :func:`text_fields` lists (each with its English twin ``<field>En``).
TEXT_FIELDS = ("reason", "warning", "note", "message", "action", "because", "text", "scopeWarning")


def text_fields(doc: Any) -> list[tuple[str, str, str | None]]:
    """``(where, text, english twin)`` of every human-readable field of a rehearsal document, in order.

    ``where`` names list items by position and id (``remediation[3:h04].action``), so two documents with
    the same verdicts list the same places."""
    out: list[tuple[str, str, str | None]] = []

    def walk(value: Any, where: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                at = f"{where}.{key}" if where else key
                if key in TEXT_FIELDS and isinstance(item, str):
                    twin = value.get(key + "En")
                    out.append((at, item, twin if isinstance(twin, str) else None))
                elif isinstance(item, (dict, list)):
                    walk(item, at)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                label = next((item[k] for k in ("id", "caseId", "code") if isinstance(item, dict) and isinstance(item.get(k), str)), "")
                walk(item, f"{where}[{i}:{label}]" if label else f"{where}[{i}]")

    walk(doc, "")
    return out


def record(directory: str, name: str) -> dict[str, Any]:
    return json.loads((EVIDENCE / directory / name).read_text(encoding="utf-8"))


def verdict_projection(value: Any) -> Any:
    """``value`` without its human-readable text (:data:`TEXT_KEYS`), recursively."""
    if isinstance(value, dict):
        return {k: verdict_projection(v) for k, v in value.items() if k not in TEXT_KEYS}
    if isinstance(value, list):
        return [verdict_projection(v) for v in value]
    return value


def _scenario(directory: str, rec: dict[str, Any]) -> dict[str, Any]:
    data = yaml.safe_load((EVIDENCE / directory / "scenario.yaml").read_text(encoding="utf-8"))
    block = data["labs"]["teaching"]
    declared = {p["id"]: p for p in block.get("phenomena") or []}
    phenomena: list[dict[str, Any]] = []
    for judged in rec["phenomena"]:
        item = copy.deepcopy(declared.get(judged["id"]) or {"id": judged["id"]})
        item.update(kind=judged["kind"], caseIds=list(judged["caseIds"]), teachingPoint=judged["teachingPoint"])
        for key in ("mechanism", "design"):
            if judged.get(key) is not None:
                item[key] = judged[key]
            else:
                item.pop(key, None)
        phenomena.append(item)
    for advisory in rec["advisory"]:
        if advisory["kind"] == "noise_grounding":
            item = copy.deepcopy(declared.get(advisory["id"]) or {"id": advisory["id"], "kind": "noise_grounding"})
            item.update(documentIds=list(advisory["documentIds"]), teachingPoint=advisory["teachingPoint"])
            phenomena.append(item)
        elif advisory["kind"] == "memory":
            first = block.setdefault("firstConversation", {})
            first["teachingPoint"] = advisory["teachingPoint"]
            if advisory.get("mustNotMention"):
                first["mustNotMention"] = list(advisory["mustNotMention"])
            else:
                first.pop("mustNotMention", None)
    block["phenomena"] = phenomena
    # The recordings predate the marker-aware PF remediation (a baseline whose marker already asks every answer to
    # leave the sources points at the probe question, not the baseline). The evidence scenario is the final build,
    # whose markers the first rounds did not have (loyalty-points r1 ran with the weak "fill in when silent" marker).
    for defect in block.get("baselineDefects") or []:
        defect.pop("baselineMarker", None)
    return data


def _documents(data: dict[str, Any], rec: dict[str, Any]) -> dict[str, str]:
    texts = {d["id"]: "" for d in data["knowledge"]["documents"]}
    by_file = {d["file"]: d["id"] for d in data["knowledge"]["documents"]}
    declared = {p["id"]: p for p in data["labs"]["teaching"]["phenomena"]}
    cases = {c["id"]: c for c in data["evaluation"]["goldenSet"]}
    for hint in rec["remediation"]:
        asset = hint["asset"]
        if asset["kind"] != "kb_doc" or asset["file"] not in by_file:
            continue
        phenomenon = declared.get(hint["phenomenonId"]) or {}
        if phenomenon.get("mechanism") == "absent":
            terms = phenomenon.get("absentTerms") or []
        else:
            terms = (cases.get(hint["caseId"]) or {}).get("expected", {}).get("mustMention") or []
        doc = by_file[asset["file"]]
        texts[doc] = " ".join([texts[doc], *terms]).strip()
    return texts


def _l1_row(case_id: str, cell: dict[str, Any]) -> dict[str, Any]:
    row = {"id": case_id, "v": cell["verdict"]}
    for key in ("fail", "defer", "unverified", "error"):
        row[key] = list(cell.get(key) or [])
    return row


def _phase_outputs(rows: list[dict[str, Any]], phase: str, l1_status: str, release: str, step_id: str) -> dict[str, Any]:
    scores: list[dict[str, Any]] = []
    l1_rows: list[dict[str, Any]] = []
    for row in rows:
        cell, cid = row[phase], row["caseId"]
        if cell.get("thelma") is not None:
            thelma = cell["thelma"]
            scores.append({"caseId": cid, "evaluator": RAG_EVALUATOR, "value": thelma["value"], "label": thelma["label"],
                           "metrics": dict(thelma.get("metrics") or {}), "traceId": f"trace-{phase}-{cid}",
                           "sessionId": f"session-{phase}-{cid}"})
        if cell.get("mtg") is not None:
            scores.append({"caseId": cid, "evaluator": GOAL_EVALUATOR, "value": cell["mtg"]["value"], "label": cell["mtg"]["label"],
                           "traceId": f"trace-{phase}-{cid}-goal", "sessionId": f"session-{phase}-{cid}"})
        if cell.get("l1") is not None:
            l1_rows.append(_l1_row(cid, cell["l1"]))
    outputs: dict[str, Any] = {"script": next(s.script for s in GUIDE_STEPS if s.id == step_id), "exitCode": 0, "scores": scores}
    if l1_status == "present":
        verdicts = [r["v"] for r in l1_rows]
        summary = {"total": len(l1_rows), **{v: verdicts.count(v) for v in ("pass", "fail", "defer", "unverified", "error")},
                   "byCategory": {}}
        outputs["l1"] = {"schema": L1_SCHEMA, "phase": phase, "runId": f"{phase}-replay", "releaseVersion": release,
                         "summary": summary, "cases": l1_rows, "truncated": False}
    return outputs


def _state(data: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
    [current] = [r for r in rec["runs"] if r["current"]]
    assert len(rec["runs"]) == 1, "the recorded rehearsals judged one run each"
    rows = [{k: v for k, v in row.items() if k != "phenomenonIds"} for row in rec["caseTable"]["cases"]]
    at = current["optimizeFinishedAt"]
    steps: dict[str, dict[str, Any]] = {}
    for n, step in enumerate(GUIDE_STEPS):
        steps[step.id] = {"status": "passed", "label": step.label, "script": step.script, "commandId": f"cmd-replay-{n:02d}",
                          "ssmStatus": "Success", "documentVersion": "1", "startedAt": at, "finishedAt": at, "durationSeconds": 1,
                          "summary": "", "error": None, "outputs": {"script": step.script, "exitCode": 0}}
    steps["baseline"]["commandId"] = current["baselineCommandId"]
    steps["optimize"]["commandId"] = current["optimizeCommandId"]
    phases = rec["inputs"]["evidenceSources"]["phases"]
    for phase, step_id in guided_report.PHASE_STEPS:
        steps[step_id]["outputs"] = _phase_outputs(rows, phase, phases[phase]["l1"], rec["releaseVersion"], step_id)
    steps["models"]["outputs"]["models"] = {"baseline": "replay-model-baseline", "comparison": "replay-model-comparison"}
    steps["cost-latency"]["outputs"]["costLatency"] = {"averageLatencySeconds": 5.0, "averageInputTokens": 4000, "averageOutputTokens": 300,
                                                       "totalCostUsd": 0.01}
    band = rec["inputs"]["noiseBand"]
    assert band["source"] == "13-judge-stability.sh (max(2σ, spread, 0.02))", band  # derived by 13, not configured
    spread = band["value"] if band["value"] > 0.02 else 0.0
    steps["judge-stability"]["outputs"]["judgeStability"] = {"values": [0.5, round(0.5 + spread, 6)], "mean": 0.5, "std": 0.0,
                                                             "spread": spread, "verdict": band["stabilityVerdict"]}
    memory = next((a for a in rec["advisory"] if a["kind"] == "memory"), None)
    if rec["inputs"]["evidenceSources"]["conversation"] == "present":
        hits = (memory or {}).get("hits") or []
        response = " ".join(h["snippet"] for h in hits) if hits else "replayed first answer"
        steps["conversation"]["outputs"]["conversation"] = {"response": response, "sessionId": (memory or {}).get("sessionId")}
    return {
        "schemaVersion": 1, "projectId": rec["projectId"], "releaseVersion": rec["releaseVersion"], "templateCommit": rec["templateCommit"],
        "status": current["runStatus"], "createdAt": current["updatedAt"], "updatedAt": current["updatedAt"], "currentStepId": None,
        "report": None, "stepOrder": [step.id for step in GUIDE_STEPS], "steps": steps,
    }


def replay_inputs(directory: str, name: str, *, language: str | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(record, positional args, keyword args) of ``rehearsal.build_rehearsal`` for one recorded rehearsal.

    ``language`` overrides the scenario's pack language (the same evidence judged for an English pack).
    """
    rec = record(directory, name)
    data = _scenario(directory, rec)
    if language is not None:
        data["language"] = language
    kwargs = {
        "documents": _documents(data, rec),
        "guides": {name: bool(guide["present"]) for name, guide in rec["readiness"]["guides"].items()},
        "scenario_source": rec["inputs"]["scenario"],
        "release_verified": rec["inputs"]["releaseVerified"],
    }
    return rec, {"state": _state(data, rec), "scenario": data}, kwargs
