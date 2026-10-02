"""Tests for the sync control plane: custom SSM document and the generated add-ons template."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC = REPO_ROOT / "sync"
APPLIER = SYNC / "host" / "apply_release.py"
APPLY_DOC = SYNC / "ssm" / "WorkshopCustomizerApplyRelease.json"
RUN_DOC = SYNC / "ssm" / "WorkshopCustomizerRunStep.json"
ADDONS = SYNC / "cfn" / "customizer-addons.json"
BUILDER = SYNC / "cfn" / "build_addons_template.py"


@pytest.fixture(scope="module")
def apply_doc() -> dict:
    return json.loads(APPLY_DOC.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def run_doc() -> dict:
    return json.loads(RUN_DOC.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def addons() -> dict:
    return json.loads(ADDONS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location("build_addons_template", BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ---------------------------------------------------------------------------
# Apply document
# ---------------------------------------------------------------------------


def test_apply_document_parameters_are_all_constrained(apply_doc):
    for name, spec in apply_doc["parameters"].items():
        assert "allowedPattern" in spec or "allowedValues" in spec, name
        assert spec["type"] == "String"
        if "allowedPattern" in spec:
            # defaults must satisfy their own pattern and patterns must exclude shell metacharacters
            assert re.fullmatch(spec["allowedPattern"], spec["default"]) is not None, name
            for bad in ["a;b", "$(id)", "`id`", "x' y", "a|b", "a&b", "a>b", "\n"]:
                assert re.fullmatch(spec["allowedPattern"], bad) is None, (name, bad)


def test_apply_document_only_execs_the_installed_applier(apply_doc):
    steps = apply_doc["mainSteps"]
    assert len(steps) == 1 and steps[0]["action"] == "aws:runShellScript"
    commands = steps[0]["inputs"]["runCommand"]
    exec_lines = [c for c in commands if c.startswith("exec ")]
    assert len(exec_lines) == 1
    assert exec_lines[0].startswith("exec /usr/bin/python3 /opt/workshop-customizer/apply_release.py --action '{{ Action }}'")
    # every substituted parameter is single-quoted so a pattern-constrained value cannot become syntax
    for placeholder in re.findall(r"\{\{ \w+ \}\}", exec_lines[0]):
        assert f"'{placeholder}'" in exec_lines[0], placeholder
    assert any("apply_release.py ||" in c for c in commands)  # missing applier fails closed
    assert any("target.json ||" in c for c in commands)  # missing contract fails closed


def test_apply_document_source_pattern_matches_release_prefix_only(apply_doc):
    pattern = apply_doc["parameters"]["SourceUri"]["allowedPattern"]
    good = "s3://workshop-skills-123456789012-us-west-2/customizer-releases/align-pilot/it-helpdesk-688e337c86de/release.zip"
    assert re.fullmatch(pattern, good)
    for bad in [
        "s3://workshop-skills-123456789012-us-west-2/hr/release.zip",
        "s3://workshop-skills-123456789012-us-west-2/customizer-releases/align-pilot/it-helpdesk-688e337c86de/other.zip",
        "s3://bucket/customizer-releases/../it-helpdesk-688e337c86de/release.zip",
        "https://bucket.s3.amazonaws.com/customizer-releases/p/it-helpdesk-688e337c86de/release.zip",
    ]:
        assert re.fullmatch(pattern, bad) is None, bad


# ---------------------------------------------------------------------------
# Guided Run document
# ---------------------------------------------------------------------------


def test_run_document_is_allowlisted_and_release_bound(run_doc):
    params = run_doc["parameters"]
    assert set(params) == {"ReleaseVersion", "StepId", "Script"}
    assert len(params["StepId"]["allowedValues"]) == 15
    scripts = params["Script"]["allowedValues"]
    assert len(scripts) == 15 and "99-cleanup.sh" not in scripts
    version_pattern = params["ReleaseVersion"]["allowedPattern"]
    assert re.fullmatch(version_pattern, "northstar-abcdef123456")
    for bad in ["../release-abcdef123456", "x;id-abcdef123456", "$(id)-abcdef123456"]:
        assert re.fullmatch(version_pattern, bad) is None

    steps = run_doc["mainSteps"]
    assert len(steps) == 1 and steps[0]["action"] == "aws:runShellScript"
    commands = steps[0]["inputs"]["runCommand"]
    assert sum("EXPECTED_SCRIPT=" in line for line in commands) == 15
    assert any("step/script pair mismatch" in line for line in commands)
    assert any("requested release is not active" in line for line in commands)
    assert any(line.startswith("runuser -u") for line in commands)
    parser_start = next(i for i, line in enumerate(commands) if line.startswith("python3 - \"$RC\""))
    parser_end = commands.index("PY", parser_start)
    parser = "\n".join(commands[parser_start + 1 : parser_end])
    compile(parser, "WorkshopCustomizerRunStep embedded parser", "exec")
    for field in ("scores", "costLatency", "models", "judgeStability", "stdoutTail", "stderrTail"):
        assert field in parser
    subprocess.run(["bash", "-n"], input="\n".join(commands), text=True, check=True)
    assert any("baseline|optimize)" in line for line in commands)
    assert any('SINCE_EPOCH_MS="$SINCE_MS"' in line for line in commands)
    assert any("optimize/since-ms" in line for line in commands)
    assert any('STEP_ARGS+=("$COMPARE_MODEL")' in line for line in commands)
    assert any('"${STEP_ARGS[@]}"' in line for line in commands)


# ---------------------------------------------------------------------------
# Add-ons template
# ---------------------------------------------------------------------------


def test_committed_template_matches_generator_output(builder, addons):
    assert builder.build() == addons, "run sync/cfn/build_addons_template.py — customizer-addons.json is stale"


def test_install_document_embeds_the_exact_applier(addons):
    content = addons["Resources"]["InstallApplierDocument"]["Properties"]["Content"]
    lines = content["mainSteps"][0]["inputs"]["runCommand"]
    start = lines.index("cat > /opt/workshop-customizer/apply_release.b64 <<'B64EOF'") + 1
    end = lines.index("B64EOF")
    embedded = base64.b64decode("".join(lines[start:end]))
    assert hashlib.sha256(embedded).hexdigest() == hashlib.sha256(APPLIER.read_bytes()).hexdigest()
    assert addons["Metadata"]["WorkshopCustomizer"]["applierSha256"] == hashlib.sha256(APPLIER.read_bytes()).hexdigest()
    assert len(json.dumps(content)) < 64 * 1024
    assert all(len(line) <= 200 for line in lines)
    for name, spec in content["parameters"].items():
        assert "allowedPattern" in spec, name


def test_fixed_document_resources_are_checked_in_documents(addons, apply_doc, run_doc):
    assert addons["Resources"]["ApplyReleaseDocument"]["Properties"]["Content"] == apply_doc
    assert addons["Resources"]["ApplyReleaseDocument"]["Properties"]["Name"] == "WorkshopCustomizerApplyRelease"
    assert addons["Resources"]["RunStepDocument"]["Properties"]["Content"] == run_doc
    assert addons["Resources"]["RunStepDocument"]["Properties"]["Name"] == "WorkshopCustomizerRunStep"


def test_run_step_document_sha_output_matches_the_preflight_digest(addons, builder, run_doc):
    from workshop_customizer import sync

    expected = sync.run_document_sha256(RUN_DOC)
    assert addons["Outputs"]["RunStepDocumentSha256"]["Value"] == expected
    assert addons["Metadata"]["WorkshopCustomizer"]["runDocumentSha256"] == expected
    assert builder.document_sha256(run_doc) == sync.document_sha256(run_doc) == expected


def test_association_runs_only_the_custom_install_document(addons):
    assoc = addons["Resources"]["InstallApplierAssociation"]["Properties"]
    assert assoc["Name"] == {"Ref": "InstallApplierDocument"}
    assert assoc["Targets"] == [{"Key": "InstanceIds", "Values": [{"Ref": "WorkshopInstanceId"}]}]
    assert set(assoc["Parameters"]) == {"TargetRoot", "TargetUser", "TargetGroup", "ContractParameterName", "Region"}
    assert assoc["Parameters"]["Region"] == [{"Ref": "AWS::Region"}]

def test_workshop_dependencies_and_execution_permissions_are_separate_from_sync(addons):
    install = addons["Resources"]["InstallApplierDocument"]["Properties"]["Content"]
    commands = "\n".join(install["mainSteps"][0]["inputs"]["runCommand"])
    assert "flock -x 9" in commands
    assert "python3.12-pip" in commands and "boto3 botocore retrying uv" in commands
    assert "@aws/agentcore@1.0.0-preview.30" in commands
    assert "npx --no-install agentcore --version" in commands
    execution = addons["Resources"]["WorkshopExecutionPolicy"]["Properties"]
    assert execution["Roles"] == [{"Fn::Sub": "workshop-ec2-role-${AWS::Region}"}]
    statements = {item["Sid"]: item for item in execution["PolicyDocument"]["Statement"]}
    assert "lambda:UpdateFunctionCode" in statements["ScenarioLambdas"]["Action"]
    assert "${ToolFunctionName}" in json.dumps(statements["ScenarioLambdas"]["Resource"])
    assert "${SsmParameterPrefix}" in json.dumps(statements["ScenarioParameters"]["Resource"])
    assert all(item["Resource"] != "*" for name, item in statements.items()
               if name not in {"AgentCoreWorkshop", "TransactionSearch"})

def test_skills_use_s3_files_access_point_and_private_nfs_mounts(addons):
    resources = addons["Resources"]
    assert resources["SkillsFileSystem"]["Type"] == "AWS::S3Files::FileSystem"
    assert resources["SkillsFilesAccessPoint"]["Type"] == "AWS::S3Files::AccessPoint"
    for i in (1, 2):
        assert resources[f"SkillsFilesMountTarget{i}"]["Type"] == "AWS::S3Files::MountTarget"
    ingress = resources["SkillsFilesSecurityGroup"]["Properties"]["SecurityGroupIngress"]
    assert ingress[0]["FromPort"] == ingress[0]["ToPort"] == 2049
    assert "SourceSecurityGroupId" in ingress[0] and "CidrIp" not in ingress[0]
    trust = resources["SkillsFilesRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Principal"]["Service"] == "elasticfilesystem.amazonaws.com"


def test_target_contract_parameter_carries_the_trust_anchor(addons):
    value = addons["Resources"]["TargetContractParameter"]["Properties"]["Value"]
    template, variables = value["Fn::Sub"]
    assert "WorkshopBucket" in variables and "Fn::ImportValue" in variables["WorkshopBucket"]
    rendered = json.loads(re.sub(r"\$\{[^}]+\}", "X", template))
    assert rendered["schema"] == "workshop-customizer/target-contract/1"
    assert rendered["allowedReleasePrefixes"] == ["s3://X/customizer-releases/"]
    assert rendered["documentName"] == "WorkshopCustomizerApplyRelease"
    assert rendered["runDocumentName"] == "WorkshopCustomizerRunStep"
    assert rendered["packSchemaVersion"] == 1
    assert set(rendered["comparisonModel"]) == {"id", "inputPricePerMillionUsd", "outputPricePerMillionUsd"}


def _statements(addons) -> dict[str, dict]:
    doc = addons["Resources"]["SyncPolicy"]["Properties"]["PolicyDocument"]
    return {s["Sid"]: s for s in doc["Statement"]}


def test_sync_policy_scopes_send_command_and_denies_generic_shell(addons):
    st = _statements(addons)
    invoke = st["InvokeOnlyFixedDocumentsOnWorkshopInstance"]
    assert invoke["Action"] == ["ssm:SendCommand"]
    assert all(isinstance(r, dict) and "Fn::Sub" in r for r in invoke["Resource"])
    assert any("instance/${WorkshopInstanceId}" in r["Fn::Sub"] for r in invoke["Resource"])
    assert any("document/WorkshopCustomizerApplyRelease" in r["Fn::Sub"] for r in invoke["Resource"])
    assert any("document/WorkshopCustomizerRunStep" in r["Fn::Sub"] for r in invoke["Resource"])

    deny = st["DenyEveryOtherDocument"]
    assert deny["Effect"] == "Deny" and "ssm:SendCommand" in deny["Action"] and "ssm:StartSession" in deny["Action"]
    allowed = deny["Condition"]["ArnNotEquals"]["ssm:DocumentArn"]
    assert len(allowed) == 2
    assert {item["Fn::Sub"].rsplit("/", 1)[-1] for item in allowed} == {"WorkshopCustomizerApplyRelease", "WorkshopCustomizerRunStep"}

    # no Allow statement grants SendCommand on "*"
    for sid, statement in st.items():
        if statement["Effect"] == "Allow" and "ssm:SendCommand" in statement["Action"]:
            assert statement["Resource"] != "*", sid


def test_sync_policy_confines_s3_writes_to_release_prefix(addons):
    st = _statements(addons)
    objects = st["ReleaseObjects"]
    assert "s3:DeleteObject" not in objects["Action"]
    assert objects["Resource"]["Fn::Sub"][0].endswith("/customizer-releases/*")
    listing = st["ListReleasePrefixOnly"]
    assert listing["Condition"]["StringLike"]["s3:prefix"] == ["customizer-releases/*"]
    deny = st["DenyWritesOutsideReleasePrefix"]
    assert deny["Effect"] == "Deny" and "s3:PutObject" in deny["Action"] and "NotResource" in deny
    destructive = st["DenyDestructiveAndIam"]
    assert "iam:*" in destructive["Action"] and "cloudformation:DeleteStack" in destructive["Action"]


def test_sync_policy_guard_reads_are_read_only(addons):
    st = _statements(addons)
    guard = st["GuardReadPackParameters"]
    assert guard["Action"] == ["ssm:GetParameter"] and guard["Resource"]["Fn::Sub"].endswith(":parameter/app/*")
    assert st["GuardListAgentRuntimes"]["Action"] == ["bedrock-agentcore:ListAgentRuntimes"]
    for sid, s in st.items():
        if s["Effect"] != "Allow":
            continue
        for action in s["Action"]:
            service, verb = action.split(":", 1)
            if service in {"ssm", "bedrock-agentcore-control"}:
                assert verb.startswith(("Get", "List", "Describe")) or action == "ssm:SendCommand", (sid, action)


def test_template_parameters_are_pattern_constrained(addons):
    params = addons["Parameters"]
    assert params["TemplateCommit"]["AllowedPattern"] == "^[0-9a-f]{40}$"
    assert params["WorkshopInstanceId"]["Type"] == "AWS::EC2::Instance::Id"
    for name in ("TargetRoot", "TargetUser", "TargetGroup"):
        assert "AllowedPattern" in params[name], name
