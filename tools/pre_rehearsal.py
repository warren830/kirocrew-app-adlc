"""Predict a pack's rehearsal locally on the Workshop model, before the AWS Guided Run.

    .venv/bin/python3 tools/pre_rehearsal.py --project-dir <App data>/projects/<id> --profile default [--repeat 2]

Compiles the project's scenario into a temporary pack (what the release ships), runs every practice
question for the baseline and the optimization candidate on Bedrock (Nova 2 Lite by default, with the
pack's tools and a local Titan-embedding copy of its knowledge base), applies L1, and writes
``pre-rehearsal.json`` and ``pre-rehearsal.md`` under ``--out`` (default: ``<project>/build/pre-rehearsal``).
Exit 0 when every tool_use / refusal / escalation phenomenon held in every run, 3 when one did not, 2
unknown. The THELMA phenomena are not predicted; ``run/rehearsal.json`` decides readyForClass. Costs a few
cents of Bedrock calls (Nova 2 Lite and Titan embeddings) and about three minutes.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer import compiler, pre_rehearsal, pre_thelma  # noqa: E402
from workshop_customizer.scenario import load_scenario  # noqa: E402

TEMPLATE_COMMIT = json.loads((REPO / "template-lock.json").read_text())["template"]["commit"]


#: The pinned evaluator code the release ships (evaluators/thelma_eval); only its span adapter's markers differ per pack.
THELMA_DIR = REPO / "upstream" / "evaluators" / "thelma_eval"


def pre_rehearse(project_dir: Path, *, profile: str, region: str = "us-west-2", model: str = pre_rehearsal.DEFAULT_MODEL,
                 repeat: int = 2, cases: list[str] | None = None, out: Path | None = None, progress=None,
                 thelma: bool = True) -> dict:
    """Compile the project's scenario and pre-rehearse it; writes pre-rehearsal.json / .md under ``out``. With
    ``thelma`` the probe cases' first searches are scored by the pinned THELMA evaluator on the pack's judge model."""
    import boto3
    from botocore.config import Config

    scenario = load_scenario(project_dir / "scenario.yaml")
    out = out or project_dir / "build" / "pre-rehearsal"
    client = boto3.Session(profile_name=profile, region_name=region).client(
        "bedrock-runtime", config=Config(read_timeout=300, retries={"max_attempts": 4, "mode": "standard"}))
    with tempfile.TemporaryDirectory() as tmp:
        pack = compiler.compile_pack(scenario, Path(tmp) / "out", template_commit=TEMPLATE_COMMIT)
        inputs = pre_rehearsal.inputs_from_pack(pack.pack_dir, scenario.data)
        judge = str((scenario.data.get("evaluation") or {}).get("judgeModel") or pre_rehearsal.DEFAULT_MODEL)
        scorer = (lambda triplets: pre_thelma.score(triplets, thelma_dir=THELMA_DIR, model=judge, profile=profile,
                                                    region=region)) if thelma else None
        doc = pre_rehearsal.run(inputs, client, model=model, case_ids=cases, repeat=repeat, cache=out, progress=progress,
                                thelma=scorer)
    doc["findings"] = pre_rehearsal.findings(doc, scenario.data)
    out.mkdir(parents=True, exist_ok=True)
    (out / "pre-rehearsal.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "pre-rehearsal.md").write_text(pre_rehearsal.render_markdown(doc), encoding="utf-8")
    return doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--model", default=pre_rehearsal.DEFAULT_MODEL)
    parser.add_argument("--repeat", type=int, default=2, help="runs per question; an L1 phenomenon must hold in every run")
    parser.add_argument("--case", action="append", dest="cases", help="only these practice case ids (repeatable)")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--no-thelma", action="store_true", help="leave the prompt_fixable and retrieval_gap phenomena unpredicted")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")

    doc = pre_rehearse(args.project_dir, profile=args.profile, region=args.region, model=args.model, repeat=args.repeat,
                       cases=args.cases, out=args.out, progress=lambda s: print(f"  {s}", flush=True), thelma=not args.no_thelma)
    print(pre_rehearsal.render_markdown(doc))
    return {"likely_ready": 0, "likely_not_ready": 3}.get(doc["prediction"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
