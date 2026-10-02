"""Plan (and, with --apply, create) an AgentCore online evaluation of a deployed agent from its evaluator panel.

    .venv/bin/python3 tools/online_eval.py --project-dir <App data>/projects/<id> --profile default --account 123456789012
        [--runtime direct|class] [--sampling 10] [--apply | --delete]

Reads the current build's direct rehearsal (``build/direct/<version>/direct-rehearsal.json``, run with ``--panel``)
and prints the CreateOnlineEvaluationConfig request (engine ``direct.online``): the built-in evaluators the panel
recommends for this agent, plus the Workshop's THELMA evaluator for the contrasts none of them sees, over the
chosen runtime (``direct``: the pack's direct Harness; ``class``: the Workshop's ``harness_<agent>_<agent>``),
with the reasons. Nothing is created without ``--apply``: then the execution role ``<agent>-<runtime>-online-eval`` and the
configuration (enabled, ``--sampling`` percent of sessions) are created and recorded in
``build/direct/online-eval-<runtime>.json``. An online evaluation runs, and is billed, for every sampled session
until it is deleted: ``--delete`` removes the configuration and the role. Exit 0, 2 on a missing input or account.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer.direct import online  # noqa: E402
from workshop_customizer.direct.aws import client  # noqa: E402
from workshop_customizer.direct.names import DirectNames  # noqa: E402
from workshop_customizer.direct.run import resources_path  # noqa: E402


def class_runtime(ctl, agent: str) -> str | None:
    """The Workshop's runtime of this pack (its Harness is ``<agent>_<agent>``), never the direct one."""
    wanted = f"harness_{agent}_{agent}"
    for page in _pages(ctl.list_agent_runtimes, "agentRuntimes"):
        if page.get("agentRuntimeName") == wanted:
            return str(page["agentRuntimeId"])
    return None


def _pages(call, key: str):
    kwargs: dict = {}
    while True:
        page = call(**kwargs)
        yield from page.get(key) or []
        if not page.get("nextToken"):
            return
        kwargs["nextToken"] = page["nextToken"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--runtime", choices=("direct", "class"), default="direct")
    parser.add_argument("--sampling", type=float, default=online.SAMPLING_PERCENT, help="percent of sessions evaluated (default 10)")
    parser.add_argument("--session-timeout", type=int, default=online.SESSION_TIMEOUT_MINUTES,
                        help="minutes without a new turn before a session is evaluated (default 15)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="create the role and the configuration (it runs, and is billed, until deleted)")
    action.add_argument("--delete", action="store_true", help="delete the configuration and the role recorded for this runtime")
    action.add_argument("--results", action="store_true", help="summarize the recorded configuration's results: per evaluator, "
                        "and the failing live sessions with their questions (candidates for new golden cases)")
    parser.add_argument("--hours", type=float, default=24.0, help="--results: how far back (default 24 hours)")
    args = parser.parse_args(argv)

    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if session.client("sts").get_caller_identity()["Account"] != args.account:
        print("the AWS caller account differs from --account", file=sys.stderr)
        return 2
    release = args.project_dir / "build" / "release"
    pack = json.loads((release / "pack" / "pack.json").read_text(encoding="utf-8"))
    version = json.loads((release / "RELEASE.json").read_text(encoding="utf-8"))["version"]
    names = DirectNames.of(pack["namespace"], account=args.account, region=args.region)
    record_path = args.project_dir / "build" / "direct" / f"online-eval-{args.runtime}.json"
    ctl = client(session, "bedrock-agentcore-control", args.region)
    iam = client(session, "iam")
    role = f"{names.agent}-{args.runtime}-online-eval"[:64]  # one per runtime: deleting one never breaks the other

    if args.delete:
        record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
        for done in online.delete_config(session, region=args.region, config_id=record.get("configId"), role=role):
            print(f"deleted {done}")
        if record:  # kept, marked deleted: its results log group stays readable (--results)
            record["deletedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            record_path.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        return 0

    if args.results:
        return results(session, args, record_path)

    try:
        doc = json.loads((args.project_dir / "build" / "direct" / version / "direct-rehearsal.json").read_text(encoding="utf-8"))
        panel = doc["direct"]["panel"]
    except (OSError, ValueError, KeyError):
        print(f"no evaluator panel for {version}: run tools/direct_run.py --panel first", file=sys.stderr)
        return 2
    if args.runtime == "direct":
        runtime_id = ((json.loads(resources_path(args.project_dir).read_text(encoding="utf-8")) if resources_path(args.project_dir).is_file()
                       else {}).get("harness") or {}).get("runtimeId")
    else:
        runtime_id = class_runtime(ctl, names.agent)
    if not runtime_id:
        print(f"no {args.runtime} runtime of {names.agent} is deployed", file=sys.stderr)
        return 2
    thelma = online.workshop_thelma_id([e["evaluatorId"] for e in _pages(ctl.list_evaluators, "evaluators")], names.agent)
    found = online.existing_role(iam, role)  # IAM's own ARN (its path) when the role exists; --apply always gives the one IAM returns
    role_arn = str(found["Arn"]) if found else f"arn:aws:iam::{args.account}:role/{role}"
    try:
        planned = online.plan(panel, agent=names.agent, kind=args.runtime, runtime_id=runtime_id, role_arn=role_arn, workshop_thelma=thelma,
                              sampling=args.sampling, session_timeout=args.session_timeout,
                              tags={**names.tags(str(pack.get("packId") or args.project_dir.name)), online.ONLINE_EVAL_TAG: args.runtime})
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(planned, ensure_ascii=False, indent=1))
    if not args.apply:
        print("(a plan only: --apply creates it; it then runs, and is billed, for every sampled session until --delete)")
        return 0
    record = {**online.apply_plan(session, account=args.account, region=args.region, planned=planned, role=role),
              "runtimeId": runtime_id, "releaseVersion": version, "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print(f"created {record['configId']} on {runtime_id}; results go to /aws/bedrock-agentcore/evaluations/results/{record['configId']}")
    return 0


def results(session, args, record_path: Path) -> int:
    """The recorded configuration's results, summarized; the failing sessions' questions from the runtime's logs."""
    if not record_path.is_file():
        print(f"no online evaluation recorded for the {args.runtime} runtime ({record_path})", file=sys.stderr)
        return 2
    record = json.loads(record_path.read_text(encoding="utf-8"))
    got = online.fetch_results(session, region=args.region, config_id=record["configId"], hours=args.hours,
                               runtime_group=record.get("runtimeGroup") or f"/aws/bedrock-agentcore/runtimes/{record['runtimeId']}-DEFAULT")
    summary, questions = got, got["questions"]
    out = args.project_dir / "build" / "direct" / f"online-results-{args.runtime}.json"
    out.write_text(json.dumps(got, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    text = online.render_results(summary, config=record["configId"], questions=questions)
    out.with_suffix(".md").write_text(text, encoding="utf-8")
    print(text)
    asked = [q for q in dict.fromkeys(questions.values()) if q]
    if asked:  # live failures become practice cases through Kiro, reviewed like any other draft
        brief = out.with_name(f"online-results-{args.runtime}.instructions.txt")
        brief.write_text("Live sessions failed the online evaluation (" + record["configId"] + "). Add a practice golden case for each "
                         "question below that the pack does not cover yet, with expected checks for what the agent must do; keep "
                         "provenance ai_draft for review:\n" + "\n".join(f"- {q}" for q in asked) + "\n", encoding="utf-8")
        print(f"to turn them into golden cases: tools/kiro_generate.py --loop --project-dir {args.project_dir} --start-mode regenerate "
              f"--scope golden --instructions \"$(cat {brief})\" ...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
