"""Verify any AgentCore Harness against behaviour contracts, over N rounds, with AgentCore's evaluators.

    .venv/bin/python3 tools/verify_agent.py --harness <name|id|ARN> --contracts contracts.yaml --profile default --account 123456789012
        [--from-pack <App data>/projects/<id>] [--repeat 3] [--panel] [--prompt-file prompt.md] [--model <id>] [--online-plan]

The Harness can be any in the account (Launchpad's, the agentcore CLI's, this platform's); it is only invoked.
Contracts (engine ``direct.verify``) are questions plus L1's checks, from ``--contracts`` (a list, or
``{contracts: [...], l1: {...}}``) or from a pack's practice set (``--from-pack``). Every round asks every contract
in a fresh session; a contract holds when it passes in every round. ``--panel`` scores the last round with
AgentCore's built-in evaluators as an online evaluation would and reads them against L1; ``--online-plan`` prints
the CreateOnlineEvaluationConfig request for the Harness's runtime from that reading (nothing is created; see
tools/online_eval.py). Writes ``verify.json`` / ``verify.md`` under ``--out`` (default
``build/verify/<harness>-<UTC stamp>``). Exit 0 when every contract holds in every round, 3 when not, 2 on an
environment or input problem.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer.direct import online, verify  # noqa: E402
from workshop_customizer.direct.aws import client  # noqa: E402
from workshop_customizer.direct.panel import DEFAULT_PANEL  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--harness", required=True, help="the Harness name, id or ARN")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--contracts", type=Path, help="a YAML / JSON file of contracts")
    source.add_argument("--from-pack", type=Path, help="a built project's practice cases as the contracts")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--repeat", type=int, default=3, help="rounds (default 3)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--panel", action="store_true", help="score the last round with AgentCore's built-in evaluators")
    parser.add_argument("--panel-evaluator", action="append", default=[], dest="panel_evaluators", help="instead of the default panel")
    parser.add_argument("--prompt-file", type=Path, help="a system prompt for these calls only (the Harness keeps its own)")
    parser.add_argument("--model", help="a model for these calls only")
    parser.add_argument("--online-plan", action="store_true", help="print the online evaluation plan for the Harness's runtime")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if session.client("sts").get_caller_identity()["Account"] != args.account:
        print("the AWS caller account differs from --account", file=sys.stderr)
        return 2
    try:
        cases, l1cfg = verify.load_contracts(args.contracts) if args.contracts else verify.contracts_from_pack(args.from_pack)
        info = verify.harness_info(client(session, "bedrock-agentcore-control", args.region), args.harness)
    except (verify.ContractError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not info.get("runtimeId"):
        print(f"Harness {info['name']} has no runtime yet (is it READY?)", file=sys.stderr)
        return 2
    out = args.out or REPO / "build" / "verify" / f"{info['name']}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    panel = args.panel_evaluators or (DEFAULT_PANEL if args.panel else ())
    run = verify.Verify(session, info, cases, l1cfg, region=args.region, out=out, repeat=args.repeat, workers=args.workers, panel=panel,
                        prompt=args.prompt_file.read_text(encoding="utf-8") if args.prompt_file else None, model=args.model,
                        log=lambda s: print(f"  {s}", flush=True))
    doc = run.run()
    out.mkdir(parents=True, exist_ok=True)
    (out / "verify.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "verify.md").write_text(verify.render_markdown(doc), encoding="utf-8")
    print((out / "verify.md").read_text(encoding="utf-8"))
    if args.online_plan and doc.get("panel"):
        try:
            planned = online.plan(doc["panel"]["online"], agent=str(info["name"]), kind="verified", runtime_id=str(info["runtimeId"]),
                                  role_arn=f"arn:aws:iam::{args.account}:role/<an evaluation role>")
            print(json.dumps(planned, ensure_ascii=False, indent=1))
        except ValueError as exc:
            print(f"no online plan: {exc}")
    return 0 if doc["robust"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
