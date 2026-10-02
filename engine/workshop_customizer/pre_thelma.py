"""Local THELMA: the Workshop's own evaluator code on the pre-rehearsal's probe runs.

Live, ``thelma_rag_quality`` scores one turn per trace (evaluators/thelma_eval/lambda_function.py): the
FIRST retrieval call, with that call's own ``query`` argument as the query (not the student's question),
that call's result as the only source, and the agent's last reply as the response (span_adapter.py). A
gap question that also touches a documented topic fails that way: the agent's first search asks for both,
and the documented half is covered (SQC 0.5, ops-support and freight-claims, 2026-09-28/29).

This module builds the same triplet from each local run (``pre_rehearsal.converse`` records every
retrieval call's query and answer, and the last reply), scores it in a subprocess with the pinned
evaluator (``templates/thelma_runner.py``: its packages are named ``shared`` and ``evaluators``) on the
pack's judge model, and judges every baseline/optimized pair with the rehearsal's own case rules
(``rehearsal._prompt_fixable_case`` / ``_retrieval_gap_case``) at the band floor of 13-judge-stability
(:data:`LOCAL_BAND`). What stays local: the retrieval (a Titan-embedding copy of the knowledge base) and
the sampled agent, so each case is predicted from all its repeats.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import rehearsal, teaching

RUNNER = Path(__file__).resolve().parent / "templates" / "thelma_runner.py"
#: Judge samples of each gap question used as the first search (:func:`question_probe`).
PROBE_SAMPLES = 3
#: 13-judge-stability.sh reports max(2σ, spread, 0.02); every live band so far was that floor.
LOCAL_BAND = 0.02
THELMA_KINDS = ("prompt_fixable", "retrieval_gap")


def triplet(run: Mapping[str, Any], retrieval_tool: str) -> dict[str, Any] | None:
    """The turn the Lambda scores, or ``None`` when it would skip the trace (no retrieval with a query and a result)."""
    first = next((c for c in run.get("toolsCalled") or [] if c.get("tool") == retrieval_tool), None)
    if first is None:
        return None
    query, output = str((first.get("input") or {}).get("query") or ""), str(first.get("output") or "")
    if not query or not output:
        return None
    return {"query": query, "sources": [output], "response": str(run.get("final") or "")}


def score(triplets: Sequence[Mapping[str, Any]], *, thelma_dir: Path, model: str, profile: str | None = None,
          region: str = "us-west-2", workers: int = 3, timeout: int = 1800) -> list[dict[str, Any]]:
    """One ``{"value", "label", "metrics"}`` (or ``{"error"}``) per triplet, scored by the pinned evaluator."""
    if not triplets:
        return []
    env = {**os.environ, "AWS_REGION": region, "THELMA_MODEL": model, "THELMA_WORKERS": str(workers)}
    if profile:
        env["AWS_PROFILE"] = profile
    done = subprocess.run([sys.executable, str(RUNNER), str(thelma_dir)], input=json.dumps(list(triplets), ensure_ascii=False),
                          capture_output=True, text=True, env=env, timeout=timeout)
    if done.returncode != 0:
        raise RuntimeError(f"THELMA runner failed: {done.stderr.strip()[-400:]}")
    return json.loads(done.stdout)


def score_runs(runs: Mapping[str, Mapping[str, list[dict[str, Any]]]], case_ids: Sequence[str], retrieval_tool: str,
               scorer, *, probes: Mapping[str, Mapping[str, Any]] | None = None, samples: int = PROBE_SAMPLES) -> dict[str, list]:
    """Score the probe cases' runs in place: each run gets ``thelma`` (the Lambda's result, ``None`` when skipped)
    and ``thelmaTurn`` (the first search and the start of its result). ``probes`` (case id → the question itself
    as the first search, :func:`question_probe`) are scored ``samples`` times each; returns case id → their
    results (``None`` for a failed score)."""
    jobs = []
    for phase, by_case in runs.items():
        for cid in case_ids:
            for run in by_case.get(cid) or []:
                turn = triplet(run, retrieval_tool)
                run["thelma"], run["thelmaTurn"] = None, None
                if turn is not None:
                    first = next(c for c in run["toolsCalled"] if c.get("tool") == retrieval_tool)
                    run["thelmaTurn"] = {"query": turn["query"], "source": turn["sources"][0][:300],
                                         "documents": list(first.get("sources") or [])}
                    jobs.append((run, turn))
    extra = [(cid, probe) for cid, probe in (probes or {}).items() for _ in range(samples)]
    results = scorer([turn for _, turn in jobs] + [probe for _, probe in extra])
    for (run, _), result in zip(jobs, results):
        run["thelma"] = None if "error" in result else result
        if "error" in result:
            run["thelmaError"] = result["error"]
    scored: dict[str, list] = {cid: [] for cid in probes or {}}
    for (cid, _), result in zip(extra, results[len(jobs):]):
        scored[cid].append(None if "error" in result else result)
    return scored


def question_probe(question: str, retrieved: Mapping[str, Any]) -> dict[str, Any]:
    """The student's question itself as the first search: live agents often search with it verbatim
    (freight-claims round 3), and a clause the knowledge base covers then counts for SQC even when the agent's
    local searches left it out (campus-registrar round 2: 0 of 4 local first searches covered, live SQC 0.33)."""
    return {"query": question, "sources": [str(retrieved.get("answer") or "")], "response": "-",
            "documents": list(retrieved.get("sources") or [])}


def _cell(run: Mapping[str, Any]) -> dict[str, Any]:
    return {"thelma": run.get("thelma"), "l1": run.get("l1"), "mtg": None}


def predict(data: Mapping[str, Any], runs: Mapping[str, Mapping[str, list[dict[str, Any]]]], *,
            band: float = LOCAL_BAND, probes: Mapping[str, list] | None = None,
            probe_turns: Mapping[str, Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One prediction per prompt_fixable / retrieval_gap phenomenon, from every scored baseline/optimized pair.

    A prompt_fixable case is ``likely_reproduced`` when every pair reproduces, ``likely_not_reproduced`` when
    none does, ``uncertain`` when they split, ``unknown`` when a pair is insufficient evidence (a skipped trace).
    A retrieval_gap case reproduces live only when the retrieval fails in both runs, and the judge is sampled
    (the same first search and source scored SQC 0.0 and 1.0 on loyalty-points round 1), so it is predicted from
    the share of its scored turns whose retrieval did not fail (``retrievalOkShare``), the agent's first searches
    and the question-as-search samples (``probes``) pooled: the chance that both live runs fail is
    ``(1 - share)²`` (``reproduceChance``); ``likely_reproduced`` at share 0, ``likely_not_reproduced`` below an
    even chance, else ``uncertain``. As live, a prompt_fixable phenomenon needs one reproduced case and a
    retrieval_gap phenomenon every case.
    """
    cases = {c.get("id"): c for c in teaching.practice_cases(dict(data))}
    out = []
    for phenomenon in teaching.phenomena(dict(data), THELMA_KINDS):
        kind, pid = phenomenon.get("kind"), phenomenon.get("id")
        entry: dict[str, Any] = {"id": pid, "kind": kind, "cases": []}
        for cid in phenomenon.get("caseIds") or []:
            pairs = list(zip(runs.get("baseline", {}).get(cid, []), runs.get("optimized", {}).get(cid, [])))
            verdicts = []
            for b, o in pairs:
                row = {"caseId": cid, "baseline": _cell(b), "optimized": _cell(o)}
                if kind == "prompt_fixable":
                    verdict = rehearsal._prompt_fixable_case(row, band)
                else:
                    verdict = rehearsal._retrieval_gap_case(row, cases.get(cid) or {}, str(phenomenon.get("mechanism") or ""), band)
                verdicts.append({"status": verdict["status"], "code": verdict["code"],
                                 "baseline": verdict.get("baseline"), "optimized": verdict.get("optimized"),
                                 "firstSearch": {"baseline": (b.get("thelmaTurn") or {}).get("query"),
                                                 "optimized": (o.get("thelmaTurn") or {}).get("query")},
                                 "documents": sorted({d for r in (b, o) for d in (r.get("thelmaTurn") or {}).get("documents") or []})})
            statuses = {v["status"] for v in verdicts}
            case_entry: dict[str, Any] = {"caseId": cid, "pairs": verdicts}
            if not verdicts or "insufficient_evidence" in statuses:
                prediction = "unknown"
            elif kind == "retrieval_gap":
                sampled = [t for t in (probes or {}).get(cid) or [] if t]
                turns = [r["thelma"] for pair in pairs for r in pair] + sampled
                ok = sum(1 for t in turns if teaching.retrieval_failed(t.get("metrics")) is False)
                share = ok / len(turns)
                case_entry["retrievalOkShare"] = round(share, 2)
                case_entry["reproduceChance"] = round((1 - share) ** 2, 2)
                if sampled:
                    turn = (probe_turns or {}).get(cid) or {}
                    case_entry["questionAsSearch"] = {
                        "query": turn.get("query"), "documents": sorted(set(turn.get("documents") or [])),
                        "covered": sum(1 for t in sampled if teaching.retrieval_failed(t.get("metrics")) is False),
                        "samples": [{k: (t.get("metrics") or {}).get(k) for k in ("SQC", "SP2")} for t in sampled]}
                prediction = ("likely_reproduced" if ok == 0 else
                              "likely_not_reproduced" if (1 - share) ** 2 < 0.5 else "uncertain")
            elif statuses == {"reproduced"}:
                prediction = "likely_reproduced"
            elif statuses == {"not_reproduced"}:
                prediction = "likely_not_reproduced"
            else:
                prediction = "uncertain"
            entry["cases"].append({**case_entry, "prediction": prediction})
        found = [c["prediction"] for c in entry["cases"]]
        if not found:
            entry["prediction"] = "unknown"
        elif kind == "prompt_fixable":
            entry["prediction"] = ("likely_reproduced" if "likely_reproduced" in found else
                                   "likely_not_reproduced" if set(found) == {"likely_not_reproduced"} else
                                   "uncertain" if "uncertain" in found else "unknown")
        else:
            entry["prediction"] = ("likely_reproduced" if set(found) == {"likely_reproduced"} else
                                   "likely_not_reproduced" if "likely_not_reproduced" in found else
                                   "uncertain" if "uncertain" in found else "unknown")
        out.append(entry)
    return out
