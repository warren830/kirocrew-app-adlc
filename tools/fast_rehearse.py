"""Fast re-rehearsal: check a repaired release on the environment the last rehearsal ran in, in about 20 minutes.

    .venv/bin/python3 tools/fast_rehearse.py --project-dir <App data>/projects/<id> --profile default --account 123456789012

After a rehearsal says not ready and Kiro repaired the pack (a new build), the regular loop removes the
scenario's resources (99-cleanup.sh --scenario-only, about 17 min) and runs all 15 Guide steps of the new
release (about 42 min). This tool checks the repair first, on the resources that are still there:

1. the new build's release goes to the release bucket under ``customizer-releases/<project>/fast/<version>/``
   (never ``~/workshop/current``: the synced release and its records stay as they are);
2. on the Workshop EC2 (SSM, as the Guide user), the new release's own 01/02/03 update the knowledge base,
   the tools Lambda and gateway schema, and the skills in place (they re-run cleanly on existing resources),
   the deployed Harness gets the new baseline prompt and skill list with update-harness (04 would rebuild the
   agent project and lose 08's evaluators), and 08 runs again only when the evaluators differ;
3. the new release's 06, 09, 10 and 13 run there with the Guided Run's environment, and the Guided Run's own
   step-output parser (from sync/ssm/WorkshopCustomizerRunStep.json) turns each into a step record;
4. locally, the rehearsal rules judge those steps (the other steps come from the last complete run) and write
   ``fast-rehearsal.json`` / ``.md`` under ``--out``.

The result is a real run of the repaired prompts, questions, tools and knowledge on the real Harness, evaluators
and knowledge base, but not a clean release: ``readyForClass`` stays false (release not verified) and only the
regular loop (cleanup, sync, 15 steps, rehearsal) gives the class verdict. The namespace must not change.
Exit 0 when the fast verdict is ready, 3 when not, 2 on an environment or run failure.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

import yaml  # noqa: E402

from workshop_customizer import guided_report, rehearsal  # noqa: E402
from workshop_customizer.guided_execution import parse_invocation  # noqa: E402
from workshop_customizer.guided_run import STEP_BY_ID  # noqa: E402
from workshop_customizer.scenario import load_scenario  # noqa: E402

RUN_DOC = REPO / "sync" / "ssm" / "WorkshopCustomizerRunStep.json"
#: The steps a fast re-rehearsal runs again: the rehearsal judges conversation, baseline, optimize and
#: judge-stability; the others keep the last complete run's records.
FAST_STEPS = ("conversation", "baseline", "optimize", "judge-stability")
CONTENT_STEPS = ("knowledge-base", "gateway", "skills")

HOST_SCRIPT = r'''#!/bin/bash
# Workshop Customizer fast re-rehearsal (tools/fast_rehearse.py): generated, run once as root through SSM.
set -uo pipefail
REGION='@REGION@'; BUCKET='@BUCKET@'; PREFIX='@PREFIX@'; STEPS='@STEPS@'; CONTENT='@CONTENT@'
TARGET_ROOT=/home/ssm-user/workshop; TARGET_USER=ssm-user; HOME_DIR=/home/ssm-user
PATHS="/opt/workshop-customizer/bin:$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin"
WORK=/tmp/wsc-fast; REL=$WORK/release; OUT=$WORK/run
rm -rf "$WORK"; mkdir -p "$REL" "$OUT"
finish() { echo "$1" > "$OUT/status"; aws s3 cp "$OUT/status" "s3://$BUCKET/$PREFIX/results/status" --region "$REGION" --only-show-errors; exit "${2:-0}"; }
aws s3 cp "s3://$BUCKET/$PREFIX/release.zip" "$WORK/release.zip" --region "$REGION" --only-show-errors || finish "download failed" 81
aws s3 cp "s3://$BUCKET/$PREFIX/parser.py" "$WORK/parser.py" --region "$REGION" --only-show-errors || finish "download failed" 81
(cd "$REL" && python3 -m zipfile -e ../release.zip .) || finish "unzip failed" 82
chmod 755 "$REL"/*.sh
CURRENT=$(readlink -f "$TARGET_ROOT/current")
agent_of() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["namespace"]["agentName"])' "$1"; }
AGENT=$(agent_of "$REL/RELEASE.json"); CURRENT_AGENT=$(agent_of "$CURRENT/RELEASE.json")
[ "$AGENT" = "$CURRENT_AGENT" ] || finish "namespace $AGENT is not the current release's $CURRENT_AGENT" 83
# 05 grants the runtime's image pull to the ACTIVE release only (runtime_permissions.py refuses another version);
# the fast copy is never active, so its 05 asks for the current release, whose namespace it shares.
CURRENT_VERSION=$(basename "$CURRENT")
sed -i "s|--release-version \"\$RELEASE_VERSION\"|--release-version \"$CURRENT_VERSION\"|" "$REL/05-setup-memory.sh"
grep -q -- "--release-version \"$CURRENT_VERSION\"" "$REL/05-setup-memory.sh" || finish "could not point 05 at the current release" 83
chown -R "$TARGET_USER" "$WORK"

run_step() {  # <step id> <script> [args]: the Guided Run's environment, then its step-output parser
  local id=$1 script=$2 since='' log rc t0
  shift 2
  log=$OUT/$id; mkdir -p "$log"; chown "$TARGET_USER" "$log"
  case $id in
    baseline|optimize) since=$(($(date +%s) * 1000)); echo "$since" > "$log/since-ms" ;;
  esac
  case $id in
    memory|conversation|eval-env|evaluators|baseline|optimize|cost-latency|models|judge-stability)
      python3 /opt/workshop-customizer/runtime_permissions.py --release-version "$(basename "$CURRENT")" > "$log/runtime-permissions.json" 2>&1 || true ;;
  esac
  t0=$(date +%s)
  runuser -u "$TARGET_USER" -- env HOME="$HOME_DIR" AWS_DEFAULT_REGION="$REGION" SINCE_EPOCH_MS="$since" \
    WORKSHOP_EVAL_OUT_DIR="$log" WORKSHOP_NONINTERACTIVE=1 PATH="$PATHS" \
    bash -c 'cd "$1" && script="$2" && shift 2 && exec "./$script" "$@"' fast-step "$REL" "$script" "$@" \
    > "$log/stdout.log" 2> "$log/stderr.log"
  rc=$?
  python3 "$WORK/parser.py" "$rc" "$log/stdout.log" "$log/stderr.log" "$script" "$(($(date +%s) - t0))" "$id" \
    "$HOME_DIR/workshop/eval-runs" > "$log/result.json" 2> "$log/parser.err"
  aws s3 cp "$log/result.json" "s3://$BUCKET/$PREFIX/results/$id.json" --region "$REGION" --only-show-errors
  echo "$id rc=$rc $(($(date +%s) - t0))s"
  return $rc
}

# 1. Content in place: the new release's 01/02/03 update the KB, the tools Lambda + gateway schema and the skills.
for pair in $CONTENT; do run_step "${pair%%:*}" "${pair#*:}" || finish "content step ${pair%%:*} failed" 84; done

# 2. The deployed Harness: the new baseline prompt and skill list (04 would rebuild the project and lose 08's
#    evaluators). The agent project's prompt file follows, so the next agentcore deploy (10) starts from it.
HARNESS_ID=$(runuser -u "$TARGET_USER" -- env HOME="$HOME_DIR" PATH="$PATHS" aws bedrock-agentcore-control list-harnesses \
  --region "$REGION" --query "harnesses[?harnessName=='${AGENT}_${AGENT}'].harnessId | [0]" --output text)
[ -n "$HARNESS_ID" ] && [ "$HARNESS_ID" != None ] || finish "no Harness ${AGENT}_${AGENT}" 85
PROJECT_APP=$TARGET_ROOT/$AGENT/app/$AGENT
install -m 0644 -o "$TARGET_USER" "$REL/pack/prompts/baseline.md" "$PROJECT_APP/system-prompt.md"
python3 - "$HARNESS_ID" "$REL" "$PROJECT_APP/harness.json" > "$WORK/update-harness.json" <<'PY'
import json, re, sys
harness_id, rel, project_harness = sys.argv[1], sys.argv[2], sys.argv[3]
prompt = open(f"{rel}/pack/prompts/baseline.md", encoding="utf-8").read()
update = {"harnessId": harness_id, "systemPrompt": [{"text": prompt}]}
match = re.search(r"jq '\.skills = (\[[^\]]*\])'", open(f"{rel}/04-deploy.sh", encoding="utf-8").read())
if match:
    paths = json.loads(match.group(1))
    update["skills"] = [{"path": path} for path in paths]
    # The agent project follows, so 10's agentcore deploy keeps the new skills instead of the old release's.
    config = json.load(open(project_harness, encoding="utf-8"))
    config["skills"] = paths
    json.dump(config, open(project_harness, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(json.dumps(update, ensure_ascii=False))
PY
chown "$TARGET_USER" "$WORK/update-harness.json" "$PROJECT_APP/harness.json"
runuser -u "$TARGET_USER" -- env HOME="$HOME_DIR" PATH="$PATHS" aws bedrock-agentcore-control update-harness --region "$REGION" \
  --cli-input-json "file://$WORK/update-harness.json" --query harness.status --output text > "$OUT/update-harness.log" 2>&1 \
  || finish "update-harness failed: $(tail -3 "$OUT/update-harness.log")" 86
for i in $(seq 1 60); do
  S=$(runuser -u "$TARGET_USER" -- env HOME="$HOME_DIR" PATH="$PATHS" aws bedrock-agentcore-control get-harness --region "$REGION" \
      --harness-id "$HARNESS_ID" --query harness.status --output text)
  [ "$S" = READY ] && break
  sleep 5
done
[ "$S" = READY ] || finish "Harness $HARNESS_ID is $S after the prompt update" 86

# 3. The evaluators again only when the new release's differ (their scenario wording lives in the Lambdas).
if ! diff -rq "$CURRENT/evaluators" "$REL/evaluators" > /dev/null 2>&1; then
  run_step evaluators 08-create-evaluators.sh || finish "evaluators failed" 87
fi

# 4. The steps the rehearsal judges.
for pair in $STEPS; do run_step "${pair%%:*}" "${pair#*:}" || finish "step ${pair%%:*} failed" 88; done
finish "ok" 0
'''


def parser_source() -> str:
    """The Guided Run's step-output parser: the Python heredoc of the RunStep document."""
    commands = json.loads(RUN_DOC.read_text(encoding="utf-8"))["mainSteps"][0]["inputs"]["runCommand"]
    start = next(i for i, line in enumerate(commands) if line.startswith('python3 - "$RC"'))
    return "\n".join(commands[start + 1 : commands.index("PY", start)]) + "\n"


def host_script(*, region: str, bucket: str, prefix: str, steps=FAST_STEPS, content=CONTENT_STEPS) -> str:
    pairs = lambda ids: " ".join(f"{sid}:{STEP_BY_ID[sid].script}" for sid in ids)  # noqa: E731
    return (HOST_SCRIPT.replace("@REGION@", region).replace("@BUCKET@", bucket).replace("@PREFIX@", prefix)
            .replace("@STEPS@", pairs(steps)).replace("@CONTENT@", pairs(content)))


def release_zip(release_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(release_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(release_dir).as_posix())
    return buffer.getvalue()


def step_record(step_id: str, line: str) -> dict:
    """One Guided Run step record from the parser's output line (the shape GuidedRunStore keeps)."""
    result = parse_invocation({"Status": "Success", "StandardOutputContent": line, "StandardErrorContent": ""})
    step = STEP_BY_ID[step_id]
    return {"status": "passed" if result["passed"] else "failed", "label": step.label, "script": step.script,
            "commandId": f"fast-{step_id}", "ssmStatus": "Success", "documentVersion": result["documentVersion"],
            "startedAt": None, "finishedAt": None, "durationSeconds": (result["outputs"] or {}).get("durationSeconds"),
            "summary": result["summary"], "error": result["error"], "outputs": result["outputs"]}


def fast_state(previous: dict, version: str, records: dict) -> dict:
    """The last complete run's state with the fast-run steps in place, bound to the new release."""
    state = json.loads(json.dumps(previous))
    state["releaseVersion"] = version
    state["steps"].update(records)
    state["status"] = "passed" if all(s["status"] == "passed" for s in state["steps"].values()) else "failed"
    state["fast"] = {"from": previous["releaseVersion"], "steps": sorted(records)}
    return state


def judge(project_dir: Path, previous: dict, version: str, records: dict) -> dict:
    snapshot = project_dir / "build" / "instructor" / "scenario-snapshot.yaml"  # what the build compiled, as the app judges
    scenario = yaml.safe_load(snapshot.read_text(encoding="utf-8")) if snapshot.is_file() else load_scenario(project_dir / "scenario.yaml").data
    state = fast_state(previous, version, records)
    doc = rehearsal.build_rehearsal(state, scenario, documents=rehearsal.build_documents(scenario, project_dir / "build"),
                                    guides=rehearsal.guide_status(project_dir / "build"), release_verified=False)
    doc["fast"] = state["fast"]
    doc["report"] = guided_report.build_report(state, scenario, generated_at=doc["generatedAt"])["teachingContrast"]
    return doc


def render_markdown(doc: dict) -> str:
    lines = [f"# Fast re-rehearsal — {doc.get('releaseVersion')}", "",
             f"Fast verdict: **{doc['verdict']}** ({doc['reasonCode']}). Steps run again on the environment of "
             f"`{doc['fast']['from']}`: {', '.join(doc['fast']['steps'])}. Not a class verdict: readyForClass needs the "
             "regular loop (cleanup, sync, 15 steps, rehearsal).", "",
             "| Phenomenon | Kind | Verdict | Code |", "|---|---|---|---|"]
    lines += [f"| {p['id']} | {p['kind']} | {p['verdict']} | {p.get('reasonCode')} |" for p in doc["phenomena"]]
    # RELEASE_NOT_VERIFIED is what every fast run says (the fast copy is never the synced release): not a finding.
    blocking = [h for h in doc.get("remediation") or [] if h["severity"] == "blocking" and h["code"] != "RELEASE_NOT_VERIFIED"]
    if blocking:
        lines += ["", "Blocking remediation:", ""] + [f"- {h['code']} ({h.get('caseId') or '—'}): {h.get('actionEn') or h['action']}"
                                                      for h in blocking]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--workshop-stack", default="workshop-infra")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--timeout", type=int, default=3600, help="seconds to wait for the host run (default 3600)")
    parser.add_argument("--judge-only", action="store_true",
                        help="judge the steps.json of an earlier fast run under --out again (no AWS call)")
    args = parser.parse_args(argv)

    import boto3

    project_dir = args.project_dir
    release_dir = project_dir / "build" / "release"
    manifest = json.loads((release_dir / "RELEASE.json").read_text(encoding="utf-8"))
    version = str(manifest["version"])
    previous = json.loads((project_dir / "run" / "state.json").read_text(encoding="utf-8"))
    if previous.get("status") != "passed":
        print("the last Guided Run is not complete: run the regular loop first", file=sys.stderr)
        return 2
    if previous["releaseVersion"] == version:
        print(f"the build {version} is the release the last run executed: rebuild the repaired pack first", file=sys.stderr)
        return 2
    out = args.out or project_dir / "build" / "fast-rehearsal" / version
    if args.judge_only:
        records = json.loads((out / "steps.json").read_text(encoding="utf-8"))
        return _write_verdict(project_dir, previous, version, records, out, seconds=None)
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if session.client("sts").get_caller_identity()["Account"] != args.account:
        print("the AWS caller account differs from --account", file=sys.stderr)
        return 2
    outputs = {o["OutputKey"]: o["OutputValue"] for o in
               session.client("cloudformation").describe_stacks(StackName=args.workshop_stack)["Stacks"][0]["Outputs"]}
    bucket, instance = outputs["SkillsBucketName"], outputs["InstanceId"]
    prefix = f"customizer-releases/{project_dir.name}/fast/{version}"
    s3, ssm = session.client("s3"), session.client("ssm")
    s3.put_object(Bucket=bucket, Key=f"{prefix}/release.zip", Body=release_zip(release_dir))
    s3.put_object(Bucket=bucket, Key=f"{prefix}/parser.py", Body=parser_source().encode("utf-8"))
    script = host_script(region=args.region, bucket=bucket, prefix=prefix)
    s3.put_object(Bucket=bucket, Key=f"{prefix}/fast.sh", Body=script.encode("utf-8"))
    command = ssm.send_command(
        InstanceIds=[instance], DocumentName="AWS-RunShellScript", TimeoutSeconds=args.timeout,
        Comment=f"Workshop Customizer fast re-rehearsal of {version}"[:100],
        Parameters={"executionTimeout": [str(args.timeout)], "commands": [
            f"aws s3 cp s3://{bucket}/{prefix}/fast.sh /tmp/wsc-fast.sh --region {args.region} --only-show-errors",
            "chmod 700 /tmp/wsc-fast.sh", "/tmp/wsc-fast.sh 2>&1 | tail -40"]},
    )["Command"]["CommandId"]
    print(f"fast re-rehearsal of {version} on {instance}: command {command}", flush=True)
    started = time.monotonic()
    while True:
        time.sleep(20)
        try:
            inv = ssm.get_command_invocation(CommandId=command, InstanceId=instance)
        except Exception as exc:  # noqa: BLE001 - transient SSL / throttling while polling
            print(f"  (poll error: {type(exc).__name__}; retrying)", flush=True)
            continue
        if inv["Status"] not in ("Pending", "InProgress", "Delayed"):
            break
        if time.monotonic() - started > args.timeout + 300:
            print("the host run did not finish in time", file=sys.stderr)
            return 2
    print(inv.get("StandardOutputContent", "")[-3000:], flush=True)
    status = s3.get_object(Bucket=bucket, Key=f"{prefix}/results/status")["Body"].read().decode().strip()
    records = {}
    for step_id in FAST_STEPS:
        try:
            line = s3.get_object(Bucket=bucket, Key=f"{prefix}/results/{step_id}.json")["Body"].read().decode("utf-8")
        except Exception:  # noqa: BLE001 - a step that never ran
            continue
        records[step_id] = step_record(step_id, line)
    out.mkdir(parents=True, exist_ok=True)
    (out / "steps.json").write_text(json.dumps(records, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if status != "ok" or set(records) != set(FAST_STEPS):
        print(f"the host run did not complete: {status}; steps recorded: {sorted(records)}", file=sys.stderr)
        return 2
    return _write_verdict(project_dir, previous, version, records, out, seconds=round(time.monotonic() - started))


def _write_verdict(project_dir: Path, previous: dict, version: str, records: dict, out: Path, *, seconds: int | None) -> int:
    doc = judge(project_dir, previous, version, records)
    doc["generatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc["fast"]["seconds"] = seconds
    out.mkdir(parents=True, exist_ok=True)
    (out / "fast-rehearsal.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "fast-rehearsal.md").write_text(render_markdown(doc), encoding="utf-8")
    print(render_markdown(doc))
    return 0 if doc["verdict"] == "ready" else 3


if __name__ == "__main__":
    raise SystemExit(main())
