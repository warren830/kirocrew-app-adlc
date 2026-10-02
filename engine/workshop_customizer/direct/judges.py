"""The Workshop's own judges (THELMA, Mind the Goal) on direct-mode sessions, through their release code."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .aws import RETRY_ENV, tools_python

RUNNER = Path(__file__).resolve().parent.parent / "templates" / "trace_judge_runner.py"
#: 08-create-evaluators.sh names the evaluators like this; the scores rows carry the same names.
EVALUATORS = {"thelma": "thelma_rag_quality", "mtg": "mtg_goal_success"}


def judge(kind: str, jobs: Sequence[Mapping[str, Any]], *, evaluators_dir: Path, model: str, profile: str | None, region: str,
          workers: int = 3, timeout: int = 3600) -> list[dict[str, Any]]:
    """One result per job (``{"sessionSpans", "targetTraceId"}``) from the release's ``evaluators/<kind>_eval``."""
    if not jobs:
        return []
    env = {**os.environ, **RETRY_ENV, "AWS_REGION": region, "JUDGE_WORKERS": str(workers),
           "THELMA_MODEL" if kind == "thelma" else "MTG_MODEL": model}
    if profile:
        env["AWS_PROFILE"] = profile
    done = subprocess.run([tools_python(), str(RUNNER), str(evaluators_dir / f"{kind}_eval"), kind],
                          input=json.dumps(list(jobs), ensure_ascii=False), capture_output=True, text=True, env=env, timeout=timeout)
    if done.returncode != 0:
        raise RuntimeError(f"{kind} judge failed: {done.stderr.strip()[-500:]}")
    return json.loads(done.stdout)


def score_row(kind: str, case_id: str, session_id: str, trace_id: str | None, result: Mapping[str, Any]) -> dict[str, Any]:
    """A scores row as the RunStep parser records one (``guided_report.case_scores`` reads it)."""
    row = {"evaluator": EVALUATORS[kind], "caseId": case_id, "sessionId": session_id, "traceId": trace_id or "",
           "value": result.get("value"), "label": result.get("label") or ("Error" if "error" in result else None)}
    if result.get("metrics"):
        row["metrics"] = dict(result["metrics"])
    if "error" in result:
        row["error"] = result["error"]
    return row


def tsv_line(row: Mapping[str, Any]) -> str:
    """The scores.tsv line 09 writes (``l1.read_scores`` reads it back for the combined L1 verdicts)."""
    value = "" if row.get("value") is None else str(row["value"])
    return "\t".join([str(row["evaluator"]), str(row["sessionId"]), str(row.get("traceId") or ""), value, str(row.get("label") or ""),
                      json.dumps(row.get("metrics") or {}, ensure_ascii=False), str(row["caseId"])])
