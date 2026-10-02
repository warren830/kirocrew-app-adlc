"""Score (query, sources, response) triplets with the Workshop's THELMA evaluator code, as its Lambda does.

    python thelma_runner.py <evaluators/thelma_eval dir> < triplets.json > scores.json

stdin: ``[{"query", "sources", "response"}, ...]``; stdout: one ``{"value", "label", "metrics"}`` (or
``{"error"}``) per triplet, in order. ``value`` is GR rounded to 3 places and the metrics to 2, as the
Lambda's explanation line prints them and the RunStep parser reads them back. Environment: the AWS
credentials (AWS_PROFILE / AWS_REGION), THELMA_MODEL (the judge, as 08-create-evaluators.sh sets it on the
Lambda) and THELMA_WORKERS (triplets scored at once, default 3; each runs its own 8 match threads).
The evaluator's packages are named ``shared`` and ``evaluators``, so it runs in its own process.
"""
import json
import os
import re
import sys
import types
from concurrent.futures import ThreadPoolExecutor

THELMA_DIR = sys.argv[1]
sys.dont_write_bytecode = True  # the evaluator is the pinned upstream tree (or a release): leave no __pycache__ in it
sys.path.insert(0, THELMA_DIR)
try:
    import numpy  # noqa: F401 - only the self-distinctness score (SD, embeddings) uses it
except ImportError:  # SD is not scored here (embed_fn=None), so the embedding client's import needs no numpy
    sys.modules["numpy"] = types.ModuleType("numpy")

from evaluators.thelma.metrics import evaluate_turn_detailed  # noqa: E402
from shared.llm_client import LLMClient  # noqa: E402

# lambda_function.py imports the AgentCore evaluator SDK, so its pass bar is read, not imported.
_LAMBDA = open(os.path.join(THELMA_DIR, "lambda_function.py"), encoding="utf-8").read()
GR_PASS = float(os.environ.get("THELMA_GR_THRESHOLD",
                               re.search(r"^GR_PASS_THRESHOLD\s*=.*?([0-9]+\.[0-9]+)", _LAMBDA, re.M).group(1)))


def score(item, llm):
    try:
        scores, _ = evaluate_turn_detailed(query=item["query"], sources=item["sources"], response=item["response"],
                                           llm=llm, embed_fn=None, skip_sp2=False)
    except Exception as exc:  # noqa: BLE001 - the Lambda reports THELMA_EVAL_FAILED
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}
    gr = round(scores.groundedness, 3)
    metrics = {"GR": gr, "SP1": round(scores.source_precision_chunk, 2), "SP2": round(scores.source_precision_fact, 2),
               "SQC": round(scores.source_query_coverage, 2), "RP": round(scores.response_precision, 2),
               "RQC": round(scores.response_query_coverage, 2)}
    return {"value": gr, "label": "Pass" if gr >= GR_PASS else "Fail", "metrics": metrics}


def main():
    items = json.load(sys.stdin)
    llm = LLMClient(model_id=os.environ.get("THELMA_MODEL", "us.amazon.nova-2-lite-v1:0"),
                    region=os.environ.get("AWS_REGION", "us-west-2"))
    with ThreadPoolExecutor(max_workers=int(os.environ.get("THELMA_WORKERS", "3"))) as pool:
        results = list(pool.map(lambda item: score(item, llm), items))
    json.dump(results, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
