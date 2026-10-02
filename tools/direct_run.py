"""Direct mode: run a built pack's teaching loop straight on AgentCore and rehearse it, in minutes.

    .venv/bin/python3 tools/direct_run.py --project-dir <App data>/projects/<id> --profile default --account 123456789012

No Workshop instance, no SSM, no VPC: the pack's own resources (``direct.names``: a knowledge base, the tools
Lambda and Gateway, a memory, a role and a Harness on the default network) are created or updated in place,
every practice question is asked with the baseline and the candidate prompt, the Workshop's own judges score
the traces locally, and the rehearsal rules judge the result. Writes ``direct-rehearsal.json`` / ``.md`` and the
run records under ``--out`` (default ``<project>/build/direct/<release version>``). Not a class verdict
(``readyForClass`` stays false). ``--cleanup`` removes the pack's direct-mode resources instead.
Exit 0 when the verdict is ready, 3 when not, 2 on an environment or run failure.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer.direct.panel import DEFAULT_PANEL  # noqa: E402
from workshop_customizer.direct.run import DirectRun, resources_path  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--workers", type=int, default=4, help="questions asked at once (default 4)")
    parser.add_argument("--settle-timeout", type=int, default=420, help="seconds to wait for complete traces (default 420)")
    parser.add_argument("--compare-model", action="append", default=[], dest="compare_models",
                        help="also ask the questions with the candidate on this model (repeatable; 12's comparison, per call)")
    parser.add_argument("--repeat", type=int, default=1, help="rounds on the same release after one provisioning (default 1): "
                        "how often each phenomenon reproduces; the verdict follows the last round")
    parser.add_argument("--panel", action="store_true", help="also score the phenomena's sessions with AgentCore's own evaluators "
                        "(direct.panel.DEFAULT_PANEL; about 2 more minutes) and say which of them see each phenomenon")
    parser.add_argument("--panel-evaluator", action="append", default=[], dest="panel_evaluators",
                        help="an AgentCore evaluator id for the panel instead of the default ones (repeatable; implies --panel)")
    parser.add_argument("--panel-references", action="store_true",
                        help="give the panel's evaluators the golden expectations as reference inputs (dataset-style); by default "
                             "they score as an online evaluation does, without any")
    parser.add_argument("--cleanup", action="store_true", help="remove the pack's direct-mode resources and exit")
    args = parser.parse_args(argv)

    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if session.client("sts").get_caller_identity()["Account"] != args.account:
        print("the AWS caller account differs from --account", file=sys.stderr)
        return 2
    release = args.project_dir / "build" / "release"
    if not (release / "RELEASE.json").is_file():
        print(f"no built release under {release}: build the pack first", file=sys.stderr)
        return 2
    if args.cleanup:
        from workshop_customizer.direct.cleanup import cleanup
        from workshop_customizer.direct.names import DirectNames

        pack = json.loads((release / "pack" / "pack.json").read_text(encoding="utf-8"))
        names = DirectNames.of(pack["namespace"], account=args.account, region=args.region)
        done = cleanup(session, names, release_dir=release, region=args.region, profile=args.profile,
                       state_path=resources_path(args.project_dir), log=print)
        return 2 if any(v.startswith("failed") for v in done.values()) else 0
    version = json.loads((release / "RELEASE.json").read_text(encoding="utf-8"))["version"]
    out = args.out or args.project_dir / "build" / "direct" / version
    run = DirectRun(args.project_dir, session=session, profile=args.profile, account=args.account, region=args.region, out=out,
                    log=lambda s: print(f"  {s}", flush=True), workers=args.workers, settle_timeout=args.settle_timeout,
                    compare_models=args.compare_models,
                    panel=args.panel_evaluators or (DEFAULT_PANEL if args.panel else ()), repeat=args.repeat,
                    panel_references=args.panel_references)
    doc = run.run()
    print((out / "direct-rehearsal.md").read_text(encoding="utf-8"))
    return 0 if doc["verdict"] == "ready" else 3


if __name__ == "__main__":
    raise SystemExit(main())
