"""99-cleanup.sh --scenario-only removes one scenario and keeps workshop-infra and the add-ons."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from workshop_customizer import compiler, render
from workshop_customizer.scenario import load_scenario

REPO = Path(__file__).resolve().parents[1]
TEMPLATE_COMMIT = json.loads((REPO / "template-lock.json").read_text())["template"]["commit"]


@pytest.fixture(scope="module", params=["hr-default", "it-helpdesk", "maintenance"])
def cleanup_script(request, tmp_path_factory):
    out = tmp_path_factory.mktemp(f"cleanup-{request.param}")
    path = REPO / "scenarios" / request.param / "scenario.yaml"
    pack = compiler.compile_pack(load_scenario(path), out / "build", template_commit=TEMPLATE_COMMIT)
    render.render_release(pack, REPO / "upstream", out / "release", template_commit=TEMPLATE_COMMIT)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return (out / "release" / "99-cleanup.sh").read_text(encoding="utf-8"), data["namespace"]


def test_scenario_only_mode_is_parsed_once(cleanup_script):
    text, _ns = cleanup_script
    assert text.count('[ "${1:-}" = "--scenario-only" ] && SCENARIO_ONLY=1') == 1
    assert text.count('SCENARIO_ONLY="${WORKSHOP_CLEANUP_SCENARIO_ONLY:-0}"') == 1


def test_scenario_only_stops_before_addons_buckets_and_infra(cleanup_script):
    text, _ns = cleanup_script
    stop = text.index("Scenario-only cleanup pass complete")
    exit_after_stop = text.index("  exit 0\nfi\n", stop)
    for later in ("Removing Customizer add-ons before base infrastructure",
                  "Step 5: Emptying S3 buckets", "Step 6: Deleting CloudFormation stack"):
        assert text.index(later) > exit_after_stop, later
    # The scenario-scoped steps 1-4 run before the stop in both modes.
    for earlier in ("Step 1: Deleting AgentCore Harness", "Step 2: Deleting standalone Gateway",
                    "Step 3: Deleting Knowledge Base", "Step 4: Cleaning up standalone Lambda role"):
        assert text.index(earlier) < stop, earlier


def test_scenario_only_uses_this_scenarios_names(cleanup_script):
    text, ns = cleanup_script
    block = text[text.index('echo "🗑️  Scenario-only: tools Lambda'):text.index("Scenario-only cleanup pass complete")]
    assert f'FUNC_NAME="{ns["lambdaFunctionName"]}"' in block
    assert f'--path "{ns["ssmParameterPrefix"]}"' in block
    assert f'/eval-runs/{ns["agentName"]}"' in block
    # A function owned by workshop-infra (hr-default's hr-tools-handler) is kept, never deleted.
    assert "length(StackResources[?PhysicalResourceId=='$FUNC_NAME'])" in block


def test_cleanup_script_is_valid_bash(cleanup_script, tmp_path):
    text, _ns = cleanup_script
    script = tmp_path / "99-cleanup.sh"
    script.write_text(text, encoding="utf-8")
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_the_sa_facing_docs_name_the_scenario_only_rehearsal_loop():
    """The rehearsal loop re-syncs a changed pack into the same environment: every SA-facing text says
    --scenario-only, so nobody following it deletes workshop-infra and the add-ons."""
    skill = (REPO / "app" / "skills" / "workshop-customization" / "SKILL.md").read_text(encoding="utf-8")
    loop = skill[skill.index("**New release**"):skill.index("Holdout cases are never executed by rehearsal")]
    assert "./99-cleanup.sh --scenario-only" in loop
    assert "./99-cleanup.sh --scenario-only" in (REPO / "README.md").read_text(encoding="utf-8")
    ui = (REPO / "app" / "ui" / "dist" / "index.mjs").read_text(encoding="utf-8")
    assert "./99-cleanup.sh --scenario-only" in ui and "换了 Release 要用新的 Workshop 环境" not in ui
    for lang in ("en", "zh-CN"):
        template = (REPO / "engine" / "workshop_customizer" / "templates" / "guide" / f"instructor.{lang}.md").read_text(encoding="utf-8")
        assert "`./99-cleanup.sh --scenario-only`" in template and "WORKSHOP_CLEANUP_SCENARIO_ONLY=1" in template


FAKE_AWS = """#!/bin/bash
echo "$*" >> "$FAKE_AWS_LOG"
case "$1 $2" in
  "lambda delete-function")
    if [ "$FAKE_AWS_DENY" = "1" ]; then
      echo "An error occurred (AccessDeniedException) when calling the DeleteFunction operation" >&2; exit 254
    fi
    echo "An error occurred (ResourceNotFoundException) when calling the DeleteFunction operation" >&2; exit 254 ;;
  "logs delete-log-group")
    echo "An error occurred (ResourceNotFoundException) when calling the DeleteLogGroup operation" >&2; exit 254 ;;
  "ssm get-parameters-by-path") printf '%s/gateway_arn\\t%s/knowledge_base_id\\n' "$FAKE_PREFIX" "$FAKE_PREFIX" ;;
  "cloudformation describe-stack-resources")
    case "$FAKE_CFN" in
      throttle) echo "An error occurred (Throttling) when calling the DescribeStackResources operation: Rate exceeded" >&2; exit 254 ;;
      owned) case "$*" in *length*) echo 1 ;; *) printf 'sg-0abc\\t%s\\n' "$FAKE_FUNC" ;; esac ;;
      *) case "$*" in *length*) echo 0 ;; *) printf 'sg-0abc\\tvpc-0def\\n' ;; esac ;;
    esac ;;
esac
exit 0
"""


def _run_scenario_only(text: str, ns: dict, tmp_path: Path, **fake: str) -> tuple[subprocess.CompletedProcess, str]:
    work = tmp_path / "release"
    work.mkdir()
    script = work / "99-cleanup.sh"  # alone: no gateway/ or knowledge-base/ helper can reach AWS
    script.write_text(text, encoding="utf-8")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "aws").write_text(FAKE_AWS, encoding="utf-8")
    (fakebin / "aws").chmod(0o755)
    log = tmp_path / "aws.log"
    env = {"PATH": f"{fakebin}:/usr/bin:/bin", "HOME": str(tmp_path), "WORKSHOP_ROOT": str(tmp_path / "root"),
           "FAKE_AWS_LOG": str(log), "FAKE_PREFIX": ns["ssmParameterPrefix"], "FAKE_FUNC": ns["lambdaFunctionName"],
           "AWS_DEFAULT_REGION": "us-west-2", **fake}
    proc = subprocess.run(["bash", str(script), "--scenario-only"], cwd=work, env=env, capture_output=True, text=True, timeout=60)
    return proc, log.read_text(encoding="utf-8")


@pytest.mark.parametrize("deny", [False, True])
def test_scenario_only_reports_what_it_could_not_delete(cleanup_script, tmp_path, deny):
    """Offline run of the scenario-only branch against a fake aws CLI: a denied delete is reported and
    exits 1 (never silently 'not found'); a missing function is only a warning."""
    text, ns = cleanup_script
    proc, calls = _run_scenario_only(text, ns, tmp_path, FAKE_AWS_DENY="1" if deny else "0")
    assert f"ssm delete-parameter --name {ns['ssmParameterPrefix']}/gateway_arn" in calls
    assert "cloudformation delete-stack --stack-name workshop-customizer-addons" not in calls
    assert "cloudformation delete-stack --stack-name workshop-infra" not in calls
    if deny:
        assert proc.returncode == 1, proc.stdout
        assert f"❌ Lambda {ns['lambdaFunctionName']} was not deleted: An error occurred (AccessDeniedException)" in proc.stdout
        assert "Scenario-only cleanup pass complete" not in proc.stdout
    else:
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert f"⚠️  Lambda {ns['lambdaFunctionName']} not found" in proc.stdout and "❌" not in proc.stdout


def test_a_release_cleanup_run_from_its_own_directory_cleans_only_its_namespace(tmp_path):
    """SKILL §6.5: while current belongs to another pack, the SA finds the earlier namespace's release with the
    runbook's lookup and runs cd ~/workshop/releases/<release> && ./99-cleanup.sh --scenario-only. Its names
    are rendered in and its helpers run from its own directory, so every call names that namespace, never
    current's, and current stays."""
    home = tmp_path / "home"
    root = home / "workshop"
    releases = {}
    for n, pack in enumerate(("hr-default", "it-helpdesk")):
        path = REPO / "scenarios" / pack / "scenario.yaml"
        built = compiler.compile_pack(load_scenario(path), tmp_path / "build" / pack, template_commit=TEMPLATE_COMMIT)
        release = render.render_release(built, REPO / "upstream", tmp_path / "stage" / pack, template_commit=TEMPLATE_COMMIT)
        target = root / "releases" / release.version
        shutil.copytree(release.release_dir, target)
        os.utime(target, (1_000_000 + n, 1_000_000 + n))
        namespace = yaml.safe_load(path.read_text(encoding="utf-8"))["namespace"]
        (root / namespace["agentName"]).mkdir()
        releases[pack] = (target.resolve(), namespace)
    (root / "current").symlink_to(releases["it-helpdesk"][0])
    (hr_release, hr), (it_release, it) = releases["hr-default"], releases["it-helpdesk"]

    skill = (REPO / "app" / "skills" / "workshop-customization" / "SKILL.md").read_text(encoding="utf-8")
    step = skill[skill.index("2. Find its release"):]
    lookup = step.split("```bash\n", 1)[1].split("```", 1)[0].strip()
    listed = subprocess.run(["bash", "-c", lookup], env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "HOME": str(home)},
                            capture_output=True, text=True, timeout=60, check=True).stdout
    assert listed.splitlines() == [f"{r.name} {ns['agentName']} {ns['ssmParameterPrefix']}"
                                   for r, ns in ((it_release, it), (hr_release, hr))]  # newest first
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "aws").write_text(FAKE_AWS, encoding="utf-8")
    (fakebin / "python3").write_text('#!/bin/bash\necho "python3 $*" >> "$FAKE_AWS_LOG"\n', encoding="utf-8")  # no helper reaches AWS
    for stub in fakebin.iterdir():
        stub.chmod(0o755)
    log = tmp_path / "calls.log"
    env = {"PATH": f"{fakebin}:/usr/bin:/bin", "HOME": str(home), "FAKE_AWS_LOG": str(log),
           "FAKE_PREFIX": hr["ssmParameterPrefix"], "FAKE_FUNC": hr["lambdaFunctionName"], "AWS_DEFAULT_REGION": "us-west-2"}
    proc = subprocess.run(["./99-cleanup.sh", "--scenario-only"], cwd=hr_release, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = log.read_text(encoding="utf-8")
    assert f"starts_with(harnessName,'{hr['agentName']}_')" in calls
    assert f"--stack-name AgentCore-{hr['agentName']}-default " in calls
    assert f"python3 {hr_release}/gateway/create_gateway.py delete --name {hr['gatewayName']}" in calls
    assert f"python3 {hr_release}/knowledge-base/create_kb.py --mode delete" in calls
    assert f"ssm get-parameters-by-path --path {hr['ssmParameterPrefix']} " in calls
    for key in ("agentName", "gatewayName", "knowledgeBaseName", "lambdaFunctionName", "ssmParameterPrefix", "toolTargetName"):
        assert it[key] not in calls, key
    assert str(it_release) not in calls and "/current/" not in calls
    assert not (root / hr["agentName"]).exists() and (root / it["agentName"]).is_dir()
    assert (root / "current").resolve() == it_release and it_release.is_dir()


def _tools_function_deletes(calls: str, ns: dict) -> list[str]:
    func = ns["lambdaFunctionName"]
    return [line for line in calls.splitlines()
            if line.startswith(f"lambda delete-function --function-name {func} ")
            or line.startswith(f"logs delete-log-group --log-group-name /aws/lambda/{func} ")]


def test_a_function_workshop_infra_owns_is_kept(cleanup_script, tmp_path):
    text, ns = cleanup_script
    proc, calls = _run_scenario_only(text, ns, tmp_path, FAKE_CFN="owned")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"Lambda {ns['lambdaFunctionName']} belongs to workshop-infra; kept" in proc.stdout
    assert _tools_function_deletes(calls, ns) == []
    assert f"ssm delete-parameter --name {ns['ssmParameterPrefix']}/gateway_arn" in calls


def test_an_unanswered_ownership_check_fails_closed(cleanup_script, tmp_path):
    """A throttled (or denied, or offline) describe-stack-resources is not "workshop-infra owns nothing":
    the function, its log group and every SSM parameter are kept and the pass exits 1 (review finding:
    hr-default's workshop-infra hr-tools-handler used to be deleted on a throttle)."""
    text, ns = cleanup_script
    proc, calls = _run_scenario_only(text, ns, tmp_path, FAKE_CFN="throttle")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert _tools_function_deletes(calls, ns) == []
    assert "ssm get-parameters-by-path" not in calls and f"ssm delete-parameter --name {ns['ssmParameterPrefix']}/gateway_arn" not in calls
    assert f"❌ could not check whether workshop-infra owns Lambda {ns['lambdaFunctionName']}" in proc.stdout
    assert "❌ could not list what workshop-infra owns" in proc.stdout and "Throttling" in proc.stderr
    assert "Scenario-only cleanup pass complete" not in proc.stdout


def test_addons_grant_exactly_what_the_scenario_only_cleanup_calls():
    addons = json.loads((REPO / "sync" / "cfn" / "customizer-addons.json").read_text(encoding="utf-8"))
    statements = {s["Sid"]: s for s in addons["Resources"]["WorkshopExecutionPolicy"]["Properties"]["PolicyDocument"]["Statement"]}
    expected = {"ScenarioOnlyCleanupListParameters": ("ssm:GetParametersByPath", "${SsmParameterPrefix}"),
                "ScenarioOnlyCleanupLambda": ("lambda:DeleteFunction", "function:${ToolFunctionName}"),
                "ScenarioOnlyCleanupLambdaLogs": ("logs:DeleteLogGroup", "/aws/lambda/${ToolFunctionName}")}
    for sid, (action, scope) in expected.items():
        statement = statements[sid]
        assert statement["Effect"] == "Allow" and statement["Action"] == [action]
        resources = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
        assert all(scope in r["Fn::Sub"] for r in resources), resources  # bound to this pack's namespace
    # One source of truth per permission: exactly these statements grant a Lambda or log-group delete
    # (a merge once kept a second copy of the scenario-only pair, which this test did not see).
    granting = {action: sorted(sid for sid, s in statements.items() if action in s["Action"])
                for action in ("lambda:DeleteFunction", "logs:DeleteLogGroup")}
    assert granting == {"lambda:DeleteFunction": ["ScenarioOnlyCleanupLambda"],
                        "logs:DeleteLogGroup": ["EvaluatorLogReplacement", "ScenarioOnlyCleanupLambdaLogs"]}
    evaluator_logs = statements["EvaluatorLogReplacement"]["Resource"]["Fn::Sub"]
    assert evaluator_logs.endswith(":log-group:/aws/lambda/${AgentName}-eval-*:*")
