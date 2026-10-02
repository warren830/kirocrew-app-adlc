"""Score sessions with a Workshop evaluator's own code, as its Lambda handler does, on spans given as input.

    python trace_judge_runner.py <evaluators/thelma_eval | evaluators/mtg_eval dir> <thelma|mtg> < jobs.json

stdin: ``[{"sessionSpans": [...], "targetTraceId": "..."}]`` (THELMA scores the target trace's first retrieval
turn; Mind the Goal the whole session). stdout: one ``{"value", "label", "metrics", "explanation"}`` (or
``{"error"}``) per job, in order: THELMA's value is GR rounded to 3 places and its metrics to 2 (as the
Lambda prints them), Mind the Goal's value is GSR / 100. Environment: AWS credentials, THELMA_MODEL /
MTG_MODEL (the judge), JUDGE_WORKERS (jobs at once, default 3). Each evaluator's packages are named
``shared`` and ``evaluators``, so each kind runs in its own process.
"""
import json
import os
import re
import sys
import types
from concurrent.futures import ThreadPoolExecutor

EVALUATOR_DIR, KIND = sys.argv[1], sys.argv[2]
sys.dont_write_bytecode = True  # the evaluator is a release's tree: leave no __pycache__ in it
sys.path.insert(0, EVALUATOR_DIR)
try:
    import numpy  # noqa: F401 - only THELMA's self-distinctness (embeddings) uses it, and it is not scored here
except ImportError:
    sys.modules["numpy"] = types.ModuleType("numpy")

from shared.llm_client import LLMClient  # noqa: E402

_LAMBDA = open(os.path.join(EVALUATOR_DIR, "lambda_function.py"), encoding="utf-8").read()
REGION = os.environ.get("AWS_REGION", "us-west-2")


def _threshold(name: str, env: str, default: str) -> float:
    found = re.search(rf"^{name}\s*=.*?([0-9]+(?:\.[0-9]+)?)", _LAMBDA, re.M)
    return float(os.environ.get(env, found.group(1) if found else default))


if KIND == "thelma":
    from evaluators.thelma.metrics import evaluate_turn_detailed  # noqa: E402
    from span_adapter import extract_turns_for_trace  # noqa: E402

    PASS = _threshold("GR_PASS_THRESHOLD", "THELMA_GR_THRESHOLD", "0.7")
    MODEL = os.environ.get("THELMA_MODEL", "us.amazon.nova-2-lite-v1:0")

    def judge(job, llm):
        turns = extract_turns_for_trace(job["sessionSpans"], job.get("targetTraceId"))
        rag = [t for t in turns if t.get("sources") and t.get("query")]
        if not rag:
            return {"value": None, "label": "Skipped", "explanation": "no retrieval turn with sources in this trace"}
        t = rag[0]
        scores, _ = evaluate_turn_detailed(query=t["query"], sources=t["sources"], response=t["response"], llm=llm,
                                           embed_fn=None, skip_sp2=False)
        gr = round(scores.groundedness, 3)
        metrics = {"GR": gr, "SP1": round(scores.source_precision_chunk, 2), "SP2": round(scores.source_precision_fact, 2),
                   "SQC": round(scores.source_query_coverage, 2), "RP": round(scores.response_precision, 2),
                   "RQC": round(scores.response_query_coverage, 2)}
        return {"value": gr, "label": "Pass" if gr >= PASS else "Fail", "metrics": metrics,
                "explanation": f"query: {t['query'][:60]}", "query": t["query"]}
elif KIND == "mtg":
    from evaluators.mind_the_goal.gsr import build_summary  # noqa: E402
    from evaluators.mind_the_goal.segmentation import segment_goals  # noqa: E402
    from evaluators.mind_the_goal.turn_quality import evaluate_session  # noqa: E402
    from session_adapter import rebuild_session  # noqa: E402

    PASS = _threshold("GSR_PASS_THRESHOLD", "MTG_GSR_THRESHOLD", "100")
    MODEL = os.environ.get("MTG_MODEL", "us.amazon.nova-2-lite-v1:0")

    def judge(job, llm):
        session = rebuild_session(job["sessionSpans"])
        if not session.turns:
            return {"value": None, "label": "Skipped", "explanation": "no turn to evaluate in this session"}
        raw_turns, qualities, _ = evaluate_session(session, llm)
        summary = build_summary(segment_goals(raw_turns, qualities))
        gsr = summary.get("gsr", 0.0)
        return {"value": round(gsr / 100.0, 3), "label": "Pass" if gsr >= PASS else "Fail",
                "explanation": f"Mind the Goal: GSR={gsr:.1f}% ({summary.get('successful_goals', 0)}/{summary.get('total_goals', 0)})"}
else:
    raise SystemExit(f"unknown evaluator kind {KIND!r}")


def run(job, llm):
    try:
        return judge(job, llm)
    except Exception as exc:  # noqa: BLE001 - the Lambda reports *_EVAL_FAILED
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def main():
    jobs = json.load(sys.stdin)
    llm = LLMClient(model_id=MODEL, region=REGION)
    with ThreadPoolExecutor(max_workers=int(os.environ.get("JUDGE_WORKERS", "3"))) as pool:
        results = list(pool.map(lambda job: run(job, llm), jobs))
    json.dump(results, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
