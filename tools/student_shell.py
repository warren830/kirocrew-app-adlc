"""Run the active release on the Workshop EC2 the way a student does: in a clean shell, not the Guided Run.

The Guided Run (WorkshopCustomizerRunStep) puts /opt/workshop-customizer/bin first on PATH, sets
WORKSHOP_NONINTERACTIVE and runs runtime_permissions.py before steps 05-13. A student's terminal has none
of that. Each command here runs as the target user under ``env -i`` with a plain PATH, from
``~/workshop/current``, so the release must work on its own (live 2026-11-17: the host's python3 is 3.9
without boto3).

  smoke  read-only: 00-setup.sh, every release module parses under python3.12, boto3 knows the services,
         no script names a bare python3/pip3.
  run    the student guide's order (06 twice, 12 answered "y"); stops at the first failing script.
         It creates and changes the scenario's AWS resources, like the Guided Run; it never runs 99-cleanup.

Example:
  .venv/bin/python tools/student_shell.py smoke --profile default --account 123456789012 --instance i-0abc --release orderplan-0123456789ab
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

import boto3

REPO = Path(__file__).resolve().parents[1]
CONTRACT = "/etc/workshop-customizer/target.json"
STUDENT_ORDER = ("00-setup.sh", "00-deploy-infra.sh", "01-create-kb.sh", "02-create-gateway.sh", "03-configure-skills.sh",
                 "04-deploy.sh", "05-setup-memory.sh", "06-test-conversation.sh", "07-setup-eval-env.sh",
                 "06-test-conversation.sh", "08-create-evaluators.sh", "09-run-eval.sh", "10-optimize-prompt.sh",
                 "11-cost-latency.sh", "12-compare-models.sh", "13-judge-stability.sh")
ANSWERS = {"12-compare-models.sh": "y"}  # its one confirmation prompt, typed by the student

SMOKE = r"""
set -e
echo "python3 on PATH: $(python3 --version 2>&1)"
./00-setup.sh
python3.12 - <<'PY'
import pathlib, sys
import boto3
bad = []
for path in sorted(pathlib.Path(".").rglob("*.py")):
    if path.parts[0] in ("pack", "static"):
        continue
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except SyntaxError as exc:
        bad.append(f"{path}: {exc}")
services = boto3.session.Session().get_available_services()
missing = [s for s in ("s3vectors", "bedrock-agent", "bedrock-agentcore-control") if s not in services]
print(f"python3.12 parses every module: {not bad} {bad}; boto3 {boto3.__version__}; missing services: {missing}")
sys.exit(1 if bad or missing else 0)
PY
BARE=$(grep -rnE '(^|[^[:alnum:]_./-])(python3|pip3)([^[:alnum:]_.]|$)' --include='*.sh' . | grep -v '^\./pack/' || true)
[ -z "$BARE" ] || { echo "scripts naming a bare python3/pip3:"; echo "$BARE"; exit 1; }
echo "SMOKE OK"
"""


def student_command(script_body: str, *, release: str, stdin: str | None = None) -> list[str]:
    """Root shell lines: check the active release, then run ``script_body`` as the contract's user in a clean env."""
    feed = f"printf '%s\\n' {shlex.quote(stdin)} | " if stdin else ""
    inner = f"cd ~/workshop/current && {feed}{script_body}"
    return [
        "set -o pipefail",
        f'USER_NAME=$(python3 -c \'import json; print(json.load(open("{CONTRACT}"))["user"])\')',
        f'REGION=$(python3 -c \'import json; print(json.load(open("{CONTRACT}"))["region"])\')',
        'HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6)',
        f'ACTIVE=$(python3 -c \'import json,sys; print(json.load(open(sys.argv[1]))["version"])\' "$HOME_DIR/workshop/current/RELEASE.json")',
        f'[ "$ACTIVE" = {shlex.quote(release)} ] || {{ echo "active release is $ACTIVE, not {release}"; exit 90; }}',
        'runuser -u "$USER_NAME" -- env -i HOME="$HOME_DIR" USER="$USER_NAME" LOGNAME="$USER_NAME" LANG=C.UTF-8 '
        'AWS_DEFAULT_REGION="$REGION" PATH="$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin" '
        f"bash -c {shlex.quote(inner)}",
    ]


def send(ssm, instance: str, lines: list[str], *, timeout: int = 3600) -> tuple[str, str, str]:
    cid = ssm.send_command(InstanceIds=[instance], DocumentName="AWS-RunShellScript", TimeoutSeconds=timeout,
                           Parameters={"commands": lines, "executionTimeout": [str(timeout)]})["Command"]["CommandId"]
    while True:
        time.sleep(10)
        try:
            inv = ssm.get_command_invocation(CommandId=cid, InstanceId=instance)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        except Exception as exc:  # noqa: BLE001 - a dropped poll must not abandon a running step
            print(f"  poll error, retrying: {str(exc)[:120]}", flush=True)
            continue
        if inv["Status"] not in ("Pending", "InProgress", "Delayed"):
            return inv["Status"], inv.get("StandardOutputContent", ""), inv.get("StandardErrorContent", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("smoke", "run"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--instance", required=True)
    parser.add_argument("--release", required=True, help="the release version that must be active on the instance")
    parser.add_argument("--from-index", type=int, default=0, help="for run: resume at this position of the student order")
    args = parser.parse_args(argv)

    session = boto3.session.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != args.account:
        raise SystemExit(f"caller account {account} is not the requested {args.account}")
    ssm = session.client("ssm")
    evidence = REPO / "build" / "aws-e2e" / "student" / args.release
    evidence.mkdir(parents=True, exist_ok=True)

    steps = [("smoke", SMOKE, None)] if args.action == "smoke" else [
        (f"{i:02d}-{s}", f"./{s}", ANSWERS.get(s)) for i, s in enumerate(STUDENT_ORDER)][args.from_index:]
    for name, body, answer in steps:
        print(f"START {name}", flush=True)
        started = time.time()
        status, out, err = send(ssm, args.instance, student_command(body, release=args.release, stdin=answer))
        (evidence / f"{name}.log").write_text(f"status: {status}\n--- stdout\n{out}\n--- stderr\n{err}\n", encoding="utf-8")
        print(f"{name}: {status} ({int(time.time() - started)} s)", flush=True)
        if status != "Success":
            print("\n".join((out + "\n" + err).strip().splitlines()[-40:]), flush=True)
            return 1
    print(json.dumps({"release": args.release, "action": args.action, "evidence": str(evidence)}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
