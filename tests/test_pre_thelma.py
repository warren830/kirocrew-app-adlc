"""Tests for engine/workshop_customizer/pre_thelma.py: the local THELMA predictions (no AWS)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer import pre_thelma  # noqa: E402


def _hr():
    return yaml.safe_load((REPO / "scenarios" / "hr-default" / "scenario.yaml").read_text(encoding="utf-8"))


def _run(queries_outputs, final="answer", l1=None):
    calls = [{"tool": "check_leave_balance", "input": {"employee_id": "EMP-001"}}]
    calls += [{"tool": "retrieve_hr_policy", "input": {"query": q}, "output": o} for q, o in queries_outputs]
    return {"toolsCalled": calls, "final": final, "l1": l1 or {"verdict": "pass", "fail": [], "defer": [], "unverified": [], "error": []}}


def test_the_scored_turn_is_the_first_retrieval_with_its_own_query_and_the_last_reply():
    run = _run([("sick leave days", "Sick leave: 10 days."), ("sick leave carry over", "No carry over.")], final="10 days.")
    assert pre_thelma.triplet(run, "retrieve_hr_policy") == {"query": "sick leave days", "sources": ["Sick leave: 10 days."],
                                                             "response": "10 days."}
    assert pre_thelma.triplet(_run([]), "retrieve_hr_policy") is None  # the Lambda skips a trace with no retrieval
    assert pre_thelma.triplet(_run([("q", "")]), "retrieve_hr_policy") is None


def test_scored_runs_keep_the_lambda_result_and_skipped_ones_none():
    runs = {"baseline": {"a": [_run([("q1", "s1")]), _run([])]}, "optimized": {"a": [_run([("q2", "s2")]), _run([("q3", "s3")])]}}
    seen = []

    def scorer(triplets):
        seen.extend(t["query"] for t in triplets)
        return [{"value": 0.5, "label": "Fail", "metrics": {"SQC": 1.0}}, {"error": "THELMA_EVAL_FAILED"}, {"value": 1.0, "label": "Pass",
                                                                                                           "metrics": {"SQC": 1.0}}]

    pre_thelma.score_runs(runs, ["a"], "retrieve_hr_policy", scorer)
    assert seen == ["q1", "q2", "q3"]
    assert runs["baseline"]["a"][0]["thelma"]["value"] == 0.5 and runs["baseline"]["a"][1]["thelma"] is None
    assert runs["optimized"]["a"][0]["thelma"] is None and runs["optimized"]["a"][0]["thelmaError"] == "THELMA_EVAL_FAILED"
    assert runs["optimized"]["a"][1]["thelmaTurn"] == {"query": "q3", "source": "s3", "documents": []}


def _scored(gr, sqc, sp2=0.8):
    return {"value": gr, "label": "Pass" if gr >= 0.7 else "Fail", "metrics": {"GR": gr, "SQC": sqc, "SP2": sp2}}


def test_each_pair_is_judged_by_the_rehearsal_rules_and_the_repeats_decide_the_prediction():
    data = _hr()
    pf = next(p for p in data["labs"]["teaching"]["phenomena"] if p["kind"] == "prompt_fixable")
    gap = next(p for p in data["labs"]["teaching"]["phenomena"] if p.get("mechanism") == "absent")
    pf_case, gap_case = pf["caseIds"][0], gap["caseIds"][0]

    def with_scores(pairs):
        return [{**_run([("q", "s")]), "thelma": b, "thelmaTurn": {"query": "q", "source": "s"}} for b in pairs]

    runs = {"baseline": {pf_case: with_scores([_scored(0.4, 1.0), _scored(0.5, 1.0)]),
                         gap_case: with_scores([_scored(0.2, 0.0), _scored(0.1, 0.5, 0.6)])},
            "optimized": {pf_case: with_scores([_scored(0.9, 1.0), _scored(0.9, 1.0)]),
                          gap_case: with_scores([_scored(0.8, 0.0), _scored(0.9, 0.0)])}}
    by_id = {p["id"]: p for p in pre_thelma.predict(data, runs)}
    assert by_id[pf["id"]]["prediction"] == "likely_reproduced"
    assert [v["code"] for v in by_id[pf["id"]]["cases"][0]["pairs"]] == ["PF_REPRODUCED", "PF_REPRODUCED"]
    gap_entry = by_id[gap["id"]]
    assert [v["code"] for v in gap_entry["cases"][0]["pairs"]] == ["RG_REPRODUCED", "RG_RETRIEVAL_OK_BASELINE"]
    # One covered first search in four: both live runs fail with a chance of (3/4)² ≈ 0.56.
    assert (gap_entry["prediction"], gap_entry["cases"][0]["reproduceChance"]) == ("uncertain", 0.56)
    assert gap_entry["cases"][0]["pairs"][1]["firstSearch"]["optimized"] == "q"
    runs["optimized"][gap_case][1]["thelma"] = _scored(0.9, 0.5, 0.6)  # two in four: a chance of 0.25
    assert {p["id"]: p for p in pre_thelma.predict(data, runs)}[gap["id"]]["prediction"] == "likely_not_reproduced"
    runs["baseline"][gap_case][1]["thelma"] = runs["optimized"][gap_case][1]["thelma"] = _scored(0.1, 0.0)
    assert {p["id"]: p for p in pre_thelma.predict(data, runs)}[gap["id"]]["prediction"] == "likely_reproduced"

    runs["baseline"][pf_case][1]["thelma"] = _scored(0.8, 1.0)  # already grounded in one run: still one reproduced case
    runs["baseline"][gap_case][1]["thelma"] = None  # a skipped trace is insufficient evidence
    by_id = {p["id"]: p for p in pre_thelma.predict(data, runs)}
    assert by_id[pf["id"]]["cases"][0]["prediction"] == "uncertain" and by_id[gap["id"]]["prediction"] == "unknown"


def test_the_runner_scores_like_the_lambda(tmp_path):
    """templates/thelma_runner.py on a stand-in evaluator package: the Lambda's rounding, pass bar and errors."""
    root = tmp_path / "thelma_eval"
    (root / "evaluators" / "thelma").mkdir(parents=True)
    (root / "shared").mkdir()
    (root / "lambda_function.py").write_text('GR_PASS_THRESHOLD = float(os.environ.get("THELMA_GR_THRESHOLD", "0.7"))\n')
    (root / "shared" / "__init__.py").write_text("")
    (root / "shared" / "llm_client.py").write_text("class LLMClient:\n    def __init__(self, model_id, region):\n        self.model_id = model_id\n")
    (root / "evaluators" / "__init__.py").write_text("")
    (root / "evaluators" / "thelma" / "__init__.py").write_text("")
    (root / "evaluators" / "thelma" / "metrics.py").write_text(
        "from types import SimpleNamespace\n"
        "def evaluate_turn_detailed(query, sources, response, llm, embed_fn=None, skip_sp2=False):\n"
        "    assert embed_fn is None and skip_sp2 is False and llm.model_id == 'judge-1'\n"
        "    if query == 'boom':\n        raise ValueError('bad')\n"
        "    return SimpleNamespace(groundedness=0.6666, source_precision_chunk=1, source_precision_fact=0.375,\n"
        "        source_query_coverage=0.5, response_precision=1, response_query_coverage=0.333), {}\n")
    items = [{"query": "q", "sources": ["s"], "response": "r"}, {"query": "boom", "sources": ["s"], "response": "r"}]
    done = subprocess.run([sys.executable, str(pre_thelma.RUNNER), str(root)], input=json.dumps(items), capture_output=True,
                          text=True, env={"THELMA_MODEL": "judge-1", "PATH": "/usr/bin:/bin"}, check=True)
    ok, failed = json.loads(done.stdout)
    assert ok == {"value": 0.667, "label": "Fail", "metrics": {"GR": 0.667, "SP1": 1, "SP2": 0.38, "SQC": 0.5, "RP": 1, "RQC": 0.33}}
    assert failed["error"].startswith("ValueError: bad")


def test_score_reports_a_runner_failure(tmp_path):
    with pytest.raises(RuntimeError, match="THELMA runner failed"):
        pre_thelma.score([{"query": "q", "sources": ["s"], "response": "r"}], thelma_dir=tmp_path / "missing", model="m")
    assert pre_thelma.score([], thelma_dir=tmp_path, model="m") == []


def test_a_covered_gap_is_a_finding_with_its_first_search_and_widens_the_repair():
    from workshop_customizer import pre_rehearsal

    data = _hr()
    gap = next(p for p in data["labs"]["teaching"]["phenomena"] if p.get("mechanism") == "absent")
    pair = {"status": "not_reproduced", "code": "RG_RETRIEVAL_OK_BASELINE", "baseline": None, "optimized": None,
            "firstSearch": {"baseline": "volunteer leave days", "optimized": "volunteer leave days"},
            "documents": ["knowledge-base/docs/faq.md"]}
    entry = {"id": gap["id"], "kind": "retrieval_gap", "prediction": "likely_not_reproduced",
             "cases": [{"caseId": gap["caseIds"][0], "prediction": "likely_not_reproduced", "retrievalOkShare": 0.75,
                        "reproduceChance": 0.06, "pairs": [pair, pair]}]}
    l1 = {"id": "t", "kind": "tool_use", "design": "control", "prediction": "likely_reproduced", "cases": []}
    doc = {"phenomena": [l1, entry], "flags": [], "runs": {}}
    [finding] = pre_rehearsal.findings(doc, data)
    assert finding.startswith(f"retrieval_gap '{gap['id']}' would likely not reproduce") and "3 of 4 scored first searches" in finding
    assert "'volunteer leave days'" in finding and "knowledge-base/docs/faq.md" in finding
    assert pre_rehearsal.finding_scopes(doc) == ["tools", "prompts", "golden", "knowledge", "labs"]
    assert pre_rehearsal.class_prediction(doc["phenomena"]) == "likely_not_ready"
    entry["prediction"] = entry["cases"][0]["prediction"] = "uncertain"  # at an even chance or better: shown, not a finding
    assert pre_rehearsal.findings(doc, data) == [] and pre_rehearsal.finding_scopes(doc) == ["tools", "prompts", "golden"]
    assert pre_rehearsal.class_prediction(doc["phenomena"]) == "unknown"
    entry["prediction"] = "likely_reproduced"
    assert pre_rehearsal.class_prediction(doc["phenomena"]) == "likely_ready"


def test_the_question_itself_is_sampled_as_a_first_search_and_pooled_with_the_runs():
    """Campus round 2 (live 2026-10-01): the agent's local first searches left the question's first clause out
    (SQC 0.0 in 4 of 4), the live optimized one searched with the whole question and the bait covered that
    clause (SQC 0.33): the question-as-search samples carry that risk into the prediction."""
    data = _hr()
    gap = next(p for p in data["labs"]["teaching"]["phenomena"] if p.get("mechanism") == "absent")
    cid = gap["caseIds"][0]
    runs = {phase: {cid: [{**_run([("q", "s")]), "thelma": _scored(0.2, 0.0, 0.4), "thelmaTurn": {"query": "q", "source": "s"}}
                          for _ in range(2)]} for phase in ("baseline", "optimized")}
    assert {p["id"]: p for p in pre_thelma.predict(data, runs)}[gap["id"]]["prediction"] == "likely_reproduced"
    probe = pre_thelma.question_probe("the whole question", {"answer": "faq text", "sources": ["docs/faq.md"]})
    assert probe == {"query": "the whole question", "sources": ["faq text"], "response": "-", "documents": ["docs/faq.md"]}
    covered = [_scored(0.5, 0.5, 0.35)] * 3
    entry = {p["id"]: p for p in pre_thelma.predict(data, runs, probes={cid: covered}, probe_turns={cid: probe})}[gap["id"]]
    case = entry["cases"][0]
    assert (entry["prediction"], case["retrievalOkShare"], case["reproduceChance"]) == ("likely_not_reproduced", 0.43, 0.33)
    assert case["questionAsSearch"]["covered"] == 3 and case["questionAsSearch"]["documents"] == ["docs/faq.md"]

    seen = []
    scorer = lambda triplets: (seen.extend(triplets), [_scored(0.5, 0.0)] * len(triplets))[1]  # noqa: E731
    sampled = pre_thelma.score_runs(runs, [cid], "retrieve_hr_policy", scorer,
                                    probes={cid: {k: v for k, v in probe.items() if k != "documents"}})
    assert len(sampled[cid]) == pre_thelma.PROBE_SAMPLES and seen[-1]["query"] == "the whole question"
