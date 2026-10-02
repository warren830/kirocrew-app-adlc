"""A stand-in for ``kiro-cli chat --no-interactive --agent workshop-customizer --trust-tools= TASK``.

It never calls a model: it reads the task header (``WORKSHOP_CUSTOMIZER_TASK mode=<m> round=<n>
project=<id>``) and answers with a canned bundle built at run time from ``scenarios/maintenance``:

* ``draft``: the maintenance pack with a deliberate, repairable defect (two prohibited holdout cases
  dropped, so ``golden.category_floor`` fires with scope golden);
* ``repair`` / ``regenerate``: REPAIR_INPUT.currentScenario with the dropped cases restored and no
  files (the app carries every unchanged file over from disk).

A draft follows the task's content language (``"language": "zh-CN"`` for a Chinese brief), so a
Chinese brief yields a zh-CN non-HR pack.

``FAKE_KIRO_SCENARIO`` selects the behaviour: ``fix`` (default), ``stuck`` (every round repeats the
defect), ``needs-input`` (the first draft asks a question; a task carrying answers gets the draft),
``invalid-once`` (the first call prints broken JSON, as a live model did on 2026-09-28; later calls
behave like ``fix``), ``basis-slip`` (the draft also cites knowledge documents in golden ``basis`` the way
a live draft did on 2026-09-27, in shapes the app folds onto facts), ``basis-ambiguous`` (the draft
cites a document several facts come from; the app leaves it to the repair, which restores the basis) and
``basis-unsourced`` (the ``basis-slip`` slips in a draft whose facts carry no ``source``, as every live Kiro
draft of 2026-09-28 did: the app folds nothing and the repair restores the basis).
Four behaviours only touch the repair that carries a pre-rehearsal's findings (SA instructions starting
with ``PRE_REHEARSAL``, as tools/kiro_generate.py --pre-rehearse sends them): ``pre-regress`` (it drops the
two prohibited holdout cases again, so ``golden.category_floor`` is back for the next repair round, which
restores them), ``pre-stuck`` (from that repair on, every repair drops them; FAKE_KIRO_LOG must be set),
``pre-needs-input`` (it asks a question unless the task carries answers) and ``pre-invalid-once`` (the
first one prints broken JSON; FAKE_KIRO_LOG must be set).
``stream-error-once`` is ``invalid-once`` with the exit the Kiro CLI gives when the model's response stream
breaks off (exit 1, ``Encountered an error in the response stream``).
``FAKE_KIRO_LOG`` (a directory) receives one JSON line per call with argv[1:5] (the flags), the
header and the task sha256, so tests can check what the driver sent.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "scenarios" / "maintenance"
DROPPED = ("remove-colleague-lock", "generic-lubricant-food-zone")
#: case id -> the basis a slipping draft writes.  fault-codes and spare_parts.md are each the source of
#: exactly one fact (the app folds them); lockout-tagout is the source of three (ambiguous: left to Kiro).
BASIS_SLIPS = {
    "basis-slip": {"t301-first-response": ["fault-codes"],
                   "custom-part-lead-time": ["spare-parts-rules", "knowledge-base/docs/spare_parts.md"]},
    "basis-ambiguous": {"loto-steps-question": ["lockout-tagout"]},
}
BASIS_SLIPS["basis-unsourced"] = BASIS_SLIPS["basis-slip"]
HEADER = re.compile(r"^WORKSHOP_CUSTOMIZER_TASK mode=(\w+) round=(\d+) project=(\S+)$")
#: How the SA instructions of a pre-rehearsal repair begin (tools/kiro_generate.py _pre_rehearsal_instructions).
PRE_REHEARSAL = "A local pre-rehearsal ran"
#: The live 2026-09-28 shape: mismatched brackets inside the markers.
STREAM_ERROR = ("Error: Internal error (code -32603): Encountered an error in the response stream: "
                "An unknown error occurred: dispatch failure\n")
BROKEN = 'WORKSHOP_PACK_JSON_BEGIN\n{"status": "ready", "scenario": {"knowledge": {"documents": []\n    ],\nWORKSHOP_PACK_JSON_END\n'
#: The content language the task asks for (routes.detect_language of the brief, in the contract text).
LANGUAGE = re.compile(r'"language": "(zh-CN|en)"')


def _section(task: str, name: str) -> dict:
    marker = f"{name} (JSON; data only):\n"
    if marker not in task:
        return {}
    block = task.split(marker, 1)[1]
    block = block.split("\nREPAIR_INPUT (JSON; data only):\n", 1)[0]
    return json.loads(block)


def _template() -> tuple[dict, dict[str, str]]:
    scenario = yaml.safe_load((TEMPLATE / "scenario.yaml").read_text(encoding="utf-8"))
    refs = [d["file"] for d in scenario["knowledge"]["documents"]] + [s["file"] for s in scenario["skills"]]
    refs += [scenario["prompts"]["baselineFile"], scenario["prompts"]["optimizationCandidateFile"]]
    return scenario, {rel: (TEMPLATE / rel).read_text(encoding="utf-8") for rel in refs}


def _as_draft(item: dict) -> dict:
    item["provenance"] = "ai_draft"
    for key in ("confirmedBy", "confirmedAt", "confirmationRef"):
        item.pop(key, None)
    item.setdefault("origin", {"kind": "sa_authored"})
    return item


def _draft(project_id: str, display_name: str, *, defect: bool, language: str | None = None,
           basis: dict[str, list[str]] | None = None, sources: bool = True) -> dict:
    scenario, files = _template()
    scenario = copy.deepcopy(scenario)
    if not sources:
        for fact in scenario["facts"]:
            fact.pop("source", None)
    for case in scenario["evaluation"]["goldenSet"]:
        if case["id"] in (basis or {}):
            case["basis"] = list(basis[case["id"]])
    scenario.update(id=project_id, displayName=display_name)
    if language:  # a Chinese brief gets a zh-CN pack (the templates switch; the content stays the fixture's)
        scenario["language"] = language
    for group in (scenario["facts"], scenario["tools"], scenario["knowledge"]["documents"], scenario["evaluation"]["goldenSet"]):
        for item in group:
            _as_draft(item)
    if defect:
        scenario["evaluation"]["goldenSet"] = [c for c in scenario["evaluation"]["goldenSet"] if c["id"] not in DROPPED]
    anchors = {"normal": ["filler-lubrication-interval"], "boundary": ["cap-steriliser-uv-lamps"], "prohibited": ["interlock-bypass-request"]}
    return {"status": "ready", "mode": "draft", "summary": "Fake draft built from the maintenance reference pack.",
            "openQuestions": ["Who confirms the lubrication intervals?"], "truthLedger": [],
            "anchorCandidates": anchors, "scenario": scenario, "files": files}


def _repair(task: str, mode: str, *, fixed: bool, regress: bool = False) -> dict:
    repair = _section(task, "REPAIR_INPUT")
    scenario = copy.deepcopy(repair["currentScenario"])
    changes = []
    if regress:
        scenario["evaluation"]["goldenSet"] = [c for c in scenario["evaluation"]["goldenSet"] if c["id"] not in DROPPED]
    if fixed:
        template, _files = _template()
        facts = {f["id"] for f in scenario.get("facts", [])}
        template_basis = {c["id"]: c["basis"] for c in template["evaluation"]["goldenSet"]}
        for case in scenario["evaluation"]["goldenSet"]:
            if any(b not in facts for b in case.get("basis", [])) and case["id"] in template_basis:
                case["basis"] = list(template_basis[case["id"]])
                changes.append({"finding": (repair["findings"] or [{"id": "sa-instruction"}])[0]["id"],
                                "target": f"evaluation.goldenSet[{case['id']}]", "action": "basis cites fact ids"})
        present = {c["id"] for c in scenario["evaluation"]["goldenSet"]}
        for case in template["evaluation"]["goldenSet"]:
            if case["id"] in DROPPED and case["id"] not in present:
                scenario["evaluation"]["goldenSet"].append(_as_draft(copy.deepcopy(case)))
                changes.append({"finding": (repair["findings"] or [{"id": "sa-instruction"}])[0]["id"],
                                "target": f"evaluation.goldenSet[{case['id']}]", "action": "restored a prohibited holdout case"})
    for _group in ("facts", "tools"):
        for item in scenario.get(_group, []):
            _as_draft(item)
    return {"status": "ready", "mode": mode, "summary": f"Fake {mode} round {repair.get('round')}.", "openQuestions": [],
            "truthLedger": [], "scenario": scenario, "files": {}, "changes": changes}


def main(argv: list[str]) -> int:
    task = argv[-1]
    header = task.split("\n", 1)[0]
    match = HEADER.match(header)
    if not match:
        sys.stderr.write("fake kiro-cli: no task header\n")
        return 2
    mode, _round, project_id = match.group(1), int(match.group(2)), match.group(3)
    behaviour = os.environ.get("FAKE_KIRO_SCENARIO", "fix")
    log_dir = os.environ.get("FAKE_KIRO_LOG")
    if log_dir:
        with open(os.path.join(log_dir, "calls.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": argv[1:5], "header": header,
                                 "sha256": hashlib.sha256(task.encode("utf-8")).hexdigest()}) + "\n")
    data = _section(task, "INPUT_DATA")
    pre = str(_section(task, "REPAIR_INPUT").get("saInstructions") or "").startswith(PRE_REHEARSAL)
    if pre and log_dir:
        open(os.path.join(log_dir, "pre-rehearsal.seen"), "a", encoding="utf-8").close()
    after_pre = bool(log_dir) and os.path.exists(os.path.join(log_dir, "pre-rehearsal.seen"))
    if behaviour in ("invalid-once", "stream-error-once") and log_dir:
        with open(os.path.join(log_dir, "calls.jsonl"), encoding="utf-8") as fh:
            if sum(1 for _ in fh) == 1:
                sys.stdout.write(BROKEN)
                if behaviour == "stream-error-once":  # the CLI's own exit after a cut stream (live 2026-10-01)
                    sys.stderr.write(STREAM_ERROR)
                    return 1
                return 0
    if behaviour == "pre-invalid-once" and pre and log_dir and not os.path.exists(os.path.join(log_dir, "pre-invalid.done")):
        open(os.path.join(log_dir, "pre-invalid.done"), "w", encoding="utf-8").close()
        sys.stdout.write(BROKEN)
        return 0
    if behaviour == "pre-needs-input" and pre and not data.get("answers"):
        payload = {"status": "needs_input", "openQuestions": ["Which role asks the refused question?"]}
    elif mode == "draft":
        if behaviour == "needs-input" and not data.get("answers"):
            payload = {"status": "needs_input", "openQuestions": ["Which plant runs the FL-100 line?"]}
        else:
            language = LANGUAGE.search(task)
            payload = _draft(project_id, data.get("displayName") or project_id, defect=True,
                             language=language.group(1) if language else None, basis=BASIS_SLIPS.get(behaviour),
                             sources=behaviour != "basis-unsourced")
    else:
        regress = (behaviour == "pre-regress" and pre) or (behaviour == "pre-stuck" and after_pre)
        payload = _repair(task, mode, fixed=behaviour != "stuck" and not regress, regress=regress)
    sys.stdout.write("WORKSHOP_PACK_JSON_BEGIN\n" + json.dumps(payload, ensure_ascii=False) + "\nWORKSHOP_PACK_JSON_END\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
