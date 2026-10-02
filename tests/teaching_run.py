"""Helpers over the canonical Guided Run fixture in tests/fixtures/teaching_run/ (see its manifest.json).

* :func:`run_parser` executes the step-output parser embedded in sync/ssm/WorkshopCustomizerRunStep.json
  exactly as the document does (``python3 - RC STDOUT STDERR SCRIPT DURATION STEP EVAL_ROOT``, with the
  step's LOG_DIR holding stdout.log / stderr.log / l1-compact.json) and returns the printed line.
* :func:`parse_run` turns a fixture run into Guided Run step records (parser → ``parse_invocation``);
  :func:`load_steps` reads the committed result (``<run>/steps.json``); :func:`run_state` wraps steps
  into the state ``guided_report.build_report`` reads.
* :func:`complete_outputs` synthesizes minimal complete baseline/optimize outputs for any pack (every
  practice case has an L1 verdict and a case-tagged Mind the Goal score), for app-level tests.

Offline and deterministic: the parser runs as a local subprocess, nothing else is executed.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "teaching_run"
RUN_DOC = REPO_ROOT / "sync" / "ssm" / "WorkshopCustomizerRunStep.json"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
RUNS = tuple(MANIFEST["runs"])
DOCUMENT_VERSION = MANIFEST["documentVersion"]
#: What the runner passes as the eval-runs root; the fixture paths live under it on the Workshop host.
HOST_EVAL_ROOT = "/home/ssm-user/workshop/eval-runs"
L1_SCHEMA = "workshop-customizer/l1/1"

sys.path.insert(0, str(REPO_ROOT / "engine"))
from workshop_customizer.guided_execution import parse_invocation  # noqa: E402
from workshop_customizer.guided_run import GUIDE_STEPS, STEP_BY_ID  # noqa: E402
from workshop_customizer.sync import run_document_sha256  # noqa: E402,F401  (re-exported for app tests)


def parser_source() -> str:
    """The Python heredoc of the RunStep document (between ``python3 - "$RC" ... <<'PY'`` and ``PY``)."""
    commands = json.loads(RUN_DOC.read_text(encoding="utf-8"))["mainSteps"][0]["inputs"]["runCommand"]
    start = next(i for i, line in enumerate(commands) if line.startswith('python3 - "$RC"'))
    return "\n".join(commands[start + 1 : commands.index("PY", start)]) + "\n"


def run_parser(
    log_dir: Path,
    stdout: str,
    *,
    step_id: str,
    script: str | None = None,
    rc: int = 0,
    stderr: str = "",
    compact: Any = None,
    eval_root: str | Path = HOST_EVAL_ROOT,
    duration: int = 42,
    python: str = sys.executable,
) -> str:
    """Run the embedded parser for one step; ``compact`` (dict or raw str) becomes LOG_DIR/l1-compact.json."""
    log_dir.mkdir(parents=True, exist_ok=True)
    out, err = log_dir / "stdout.log", log_dir / "stderr.log"
    out.write_text(stdout, encoding="utf-8")
    err.write_text(stderr, encoding="utf-8")
    if compact is not None:
        text = compact if isinstance(compact, str) else json.dumps(compact, ensure_ascii=False)
        (log_dir / "l1-compact.json").write_text(text, encoding="utf-8")
    script = script or STEP_BY_ID[step_id].script
    result = subprocess.run(
        [python, "-", str(rc), str(out), str(err), script, str(duration), step_id, str(eval_root)],
        input=parser_source(), capture_output=True, text=True, encoding="utf-8", timeout=60, check=True,
    )
    return result.stdout


def invocation(line: str, *, status: str = "Success", version: str | None = DOCUMENT_VERSION) -> dict[str, Any]:
    """A GetCommandInvocation response carrying the parser's line as StandardOutputContent."""
    inv: dict[str, Any] = {"Status": status, "StandardOutputContent": line, "StandardErrorContent": ""}
    if version is not None:
        inv["DocumentVersion"] = version
    return inv


def run_pack(run: str) -> str:
    return MANIFEST["runs"][run]["pack"]


def scenario(run_or_pack: str) -> dict[str, Any]:
    """The reference pack's scenario (the fixture runs were rendered from these exact files)."""
    pack = MANIFEST["runs"][run_or_pack]["pack"] if run_or_pack in MANIFEST["runs"] else run_or_pack
    return yaml.safe_load((REPO_ROOT / "scenarios" / pack / "scenario.yaml").read_text(encoding="utf-8"))


def raw(run: str, name: str) -> str | None:
    path = FIXTURES / run / name
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _step_stdout(run: str, step_id: str) -> tuple[str, str | None]:
    common = MANIFEST["commonSteps"].get(step_id)
    if common:
        return (FIXTURES / common).read_text(encoding="utf-8"), None
    stdout = raw(run, f"{step_id}.stdout.log")
    if stdout is None:
        script = STEP_BY_ID[step_id].script
        stdout = f"=========================================\n{script}\n=========================================\n✅ {step_id} complete\n"
    return stdout, raw(run, f"{step_id}.l1-compact.json")


def parse_run(run: str, tmp_path: Path) -> dict[str, dict[str, Any]]:
    """Every step of a fixture run as the Guided Run stores it after a passing poll."""
    steps: dict[str, dict[str, Any]] = {}
    for n, step in enumerate(GUIDE_STEPS):
        stdout, compact = _step_stdout(run, step.id)
        line = run_parser(tmp_path / run / step.id, stdout, step_id=step.id, script=step.script, compact=compact)
        result = parse_invocation(invocation(line))
        steps[step.id] = {
            "status": "passed" if result["passed"] else "failed", "label": step.label, "script": step.script,
            "commandId": f"cmd-{run}-{n:02d}", "ssmStatus": "Success", "documentVersion": result["documentVersion"],
            "startedAt": None, "finishedAt": None, "durationSeconds": 42, "summary": result["summary"],
            "error": result["error"], "outputs": result["outputs"],
        }
    return steps


def load_steps(run: str) -> dict[str, dict[str, Any]]:
    return json.loads((FIXTURES / run / "steps.json").read_text(encoding="utf-8"))["steps"]


def write_steps(run: str, steps: dict[str, dict[str, Any]]) -> None:
    body = {"_comment": f"Parsed step records of fixture run {run} (see ../manifest.json); regenerate with WSC_UPDATE_TEACHING_RUN=1.",
            "run": run, "pack": run_pack(run), "steps": steps}
    (FIXTURES / run / "steps.json").write_text(json.dumps(body, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def run_state(run: str, steps: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    steps = steps if steps is not None else load_steps(run)
    compact = ((steps.get("baseline") or {}).get("outputs") or {}).get("l1") or {}
    return {
        "projectId": run_pack(run), "releaseVersion": compact.get("releaseVersion") or f"{run_pack(run)}-000000000000",
        "templateCommit": "2" * 40, "stepOrder": [step.id for step in GUIDE_STEPS], "steps": steps,
    }


# ---------------------------------------------------------------------------
# Synthetic complete outputs (app-level tests)
# ---------------------------------------------------------------------------


def _practice(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in data["evaluation"]["goldenSet"] if c.get("set") == "practice"]


def complete_outputs(data: dict[str, Any], step_id: str, *, thelma: float | None = None, script: str | None = None) -> dict[str, Any]:
    """Minimal outputs that satisfy every evidence gate of a teaching pack's baseline / optimize step.

    One THELMA row (``thelma`` value, default 0.6 baseline / 0.8 optimized) per retrieval probe, a
    Mind the Goal Pass per practice case, and a compact L1 result where every practice case passes.
    """
    from workshop_customizer import teaching

    phase = "baseline" if step_id == "baseline" else "optimized"
    value = thelma if thelma is not None else (0.6 if phase == "baseline" else 0.8)
    cases = _practice(data)
    probes = teaching.probe_case_ids(data)
    scores = [{"evaluator": "thelma_rag_quality", "traceId": f"t{i:015d}", "value": value, "label": "Pass" if value >= 0.7 else "Fail",
               "caseId": cid, "sessionId": f"session-{phase}-{cid}", "metrics": {"GR": value, "SP1": 1.0, "SP2": 0.6, "SQC": 0.7}}
              for i, cid in enumerate(probes)]
    scores += [{"evaluator": "mtg_goal_success", "traceId": "?", "value": 1.0, "label": "Pass", "caseId": c["id"],
                "sessionId": f"session-{phase}-{c['id']}"} for c in cases]
    summary = {"total": len(cases), "pass": len(cases), "fail": 0, "defer": 0, "unverified": 0, "error": 0, "byCategory": {}}
    l1 = {"schema": L1_SCHEMA, "phase": phase, "runId": f"{phase}-1789279227", "releaseVersion": None, "summary": summary,
          "cases": [{"id": c["id"], "v": "pass", "fail": [], "defer": [], "unverified": []} for c in cases], "truncated": False}
    return {"script": script or STEP_BY_ID[step_id].script, "exitCode": 0, "scores": scores, "l1": l1}
