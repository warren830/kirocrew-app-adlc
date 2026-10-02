"""Tests for engine/workshop_customizer/sync.py using duck-typed stub AWS clients."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from workshop_customizer import compiler, render, sync
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = REPO_ROOT / "upstream"
TEMPLATE_COMMIT = json.loads((REPO_ROOT / "template-lock.json").read_text())["template"]["commit"]
ACCOUNT = "123456789012"
INSTANCE = "i-0123456789abcdef0"
BUCKET = "workshop-skills-123456789012-us-west-2"
DOC = "WorkshopCustomizerApplyRelease"
RUN_DOC = "WorkshopCustomizerRunStep"
CONTRACT_PARAM = "/workshop-customizer/target-contract"


class ClientError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.response = {"Error": {"Code": code, "Message": message or code}}


class StubClients:
    """Records calls; behaviour is driven by simple attributes tests can tweak."""

    def __init__(self, *, account=ACCOUNT, expiry=None, contract=None, ping="Online", stacks=None, existing_params=(), runtimes=()):
        self.calls: list[tuple[str, dict]] = []
        self._account = account
        self._expiry = expiry
        self._contract = contract if contract is not None else default_contract()
        self._ping = ping
        self._stacks = stacks if stacks is not None else default_stacks()
        self._existing_params = set(existing_params)
        self._runtimes = list(runtimes)
        self.command_invocations: list[dict] = [{"Status": "InProgress"}, {"Status": "Success", "StandardOutputContent": json.dumps({"status": "pending-commit", "version": "x"})}]
        outer = self

        class _Sts:
            def get_caller_identity(self):
                outer.calls.append(("sts.get_caller_identity", {}))
                return {"Account": outer._account, "Arn": f"arn:aws:sts::{outer._account}:assumed-role/WorkshopSync/sa"}

        class _Cfn:
            def describe_stacks(self, StackName):
                outer.calls.append(("cloudformation.describe_stacks", {"StackName": StackName}))
                if StackName not in outer._stacks:
                    raise ClientError("ValidationError", f"Stack with id {StackName} does not exist")
                return {"Stacks": [outer._stacks[StackName]]}

        class _Ssm:
            def get_parameter(self, Name):
                outer.calls.append(("ssm.get_parameter", {"Name": Name}))
                if Name == CONTRACT_PARAM:
                    return {"Parameter": {"Value": json.dumps(outer._contract)}}
                if Name in outer._existing_params:
                    return {"Parameter": {"Value": "exists"}}
                raise ClientError("ParameterNotFound")

            def describe_instance_information(self, Filters):
                outer.calls.append(("ssm.describe_instance_information", {"Filters": Filters}))
                return {"InstanceInformationList": [{"InstanceId": INSTANCE, "PingStatus": outer._ping, "PlatformType": "Linux"}]}

            def send_command(self, **kwargs):
                outer.calls.append(("ssm.send_command", kwargs))
                return {"Command": {"CommandId": "cmd-123"}}

            def get_command_invocation(self, CommandId, InstanceId):
                outer.calls.append(("ssm.get_command_invocation", {"CommandId": CommandId, "InstanceId": InstanceId}))
                return outer.command_invocations.pop(0) if len(outer.command_invocations) > 1 else outer.command_invocations[0]

        class _S3:
            def put_object(self, **kwargs):
                body = kwargs.pop("Body")
                kwargs["BodyBytes"] = len(body.read() if hasattr(body, "read") else body)
                outer.calls.append(("s3.put_object", kwargs))
                return {}

        class _AgentCore:
            def list_agent_runtimes(self):
                outer.calls.append(("agentcore.list_agent_runtimes", {}))
                return {"agentRuntimes": [{"agentRuntimeName": n} for n in outer._runtimes]}

        self.sts, self.cloudformation, self.ssm, self.s3, self.agentcore = _Sts(), _Cfn(), _Ssm(), _S3(), _AgentCore()

    def credential_expiry(self):
        return self._expiry

    def names(self):
        return [c[0] for c in self.calls]


def default_contract(**overrides):
    contract = {
        "schema": "workshop-customizer/target-contract/1",
        "targetRoot": "/home/ssm-user/workshop",
        "user": "ssm-user",
        "group": "ssm-user",
        "templateCommit": TEMPLATE_COMMIT,
        "packSchemaVersion": 1,
        "allowedReleasePrefixes": [f"s3://{BUCKET}/customizer-releases/"],
        "documentName": DOC,
        "runDocumentName": RUN_DOC,
        "instanceId": INSTANCE,
    }
    contract.update(overrides)
    return contract


#: The namespace the stub add-ons stack is deployed for (tools/deploy_addons.py --scenario it-helpdesk).
IT_NAMESPACE = yaml.safe_load((REPO_ROOT / "scenarios" / "it-helpdesk" / "scenario.yaml").read_text(encoding="utf-8"))["namespace"]


def addons_parameters(namespace: dict) -> list[dict]:
    return [{"ParameterKey": param, "ParameterValue": namespace[key]} for param, key in sync.ADDONS_NAMESPACE_PARAMETERS]


def default_stacks(namespace: dict | None = None):
    return {
        "workshop-infra": {"StackStatus": "CREATE_COMPLETE", "Outputs": [{"OutputKey": "InstanceId", "OutputValue": INSTANCE}, {"OutputKey": "SkillsBucketName", "OutputValue": BUCKET}]},
        "workshop-customizer-addons": {"StackStatus": "CREATE_COMPLETE", "Parameters": addons_parameters(namespace or IT_NAMESPACE),
                                       "Outputs": [{"OutputKey": "ApplyDocumentName", "OutputValue": DOC}, {"OutputKey": "RunStepDocumentName", "OutputValue": RUN_DOC}, {"OutputKey": "TargetContractParameter", "OutputValue": CONTRACT_PARAM}, {"OutputKey": "RunStepDocumentSha256", "OutputValue": sync.run_document_sha256()}]},
    }


@pytest.fixture(scope="module")
def cfg():
    return sync.TargetConfig(profile="align-workshop", region="us-west-2", expected_account_id=ACCOUNT)


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    base = tmp_path_factory.mktemp("rel")
    scenario = load_scenario(REPO_ROOT / "scenarios" / "it-helpdesk" / "scenario.yaml")
    pack = compiler.compile_pack(scenario, base / "build", template_commit=TEMPLATE_COMMIT)
    return render.render_release(pack, UPSTREAM, base / "release", template_commit=TEMPLATE_COMMIT)


NAMESPACE = {"ssmParameterPrefix": "/app/it", "agentName": "itassistant"}


def test_target_config_validates_shape():
    with pytest.raises(sync.SyncError):
        sync.TargetConfig(profile="p", region="us-west-2", expected_account_id="12")
    with pytest.raises(sync.SyncError):
        sync.TargetConfig(profile="p", region="west", expected_account_id=ACCOUNT)


def test_preflight_passes_on_a_healthy_target(cfg):
    clients = StubClients(expiry=datetime.now(timezone.utc) + timedelta(hours=2))
    pf = sync.preflight(cfg, clients, expected_template_commit=TEMPLATE_COMMIT, pack_namespace=NAMESPACE)
    assert pf.ok, pf.failures()
    assert pf.instance_id == INSTANCE and pf.bucket == BUCKET and pf.document_name == DOC
    assert pf.run_document_name == RUN_DOC
    assert {c.name for c in pf.checks} >= {"identity.account", "credentials.ttl", "stack.workshop", "stack.addons", "contract.templateCommit", "contract.runDocument", "instance.online", "guard.knowledge_base_id", "guard.gateway_arn", "guard.agentRuntime"}
    assert "ssm.send_command" not in clients.names() and "s3.put_object" not in clients.names()


@pytest.mark.parametrize(
    "kwargs, failing_check",
    [
        ({"account": "999999999999"}, "identity.account"),
        ({"expiry": datetime.now(timezone.utc) + timedelta(minutes=5)}, "credentials.ttl"),
        ({"contract": default_contract(templateCommit="0" * 40)}, "contract.templateCommit"),
        ({"contract": default_contract(instanceId="i-0000000000000000f")}, "contract.instance"),
        ({"contract": default_contract(allowedReleasePrefixes=["s3://other-bucket/customizer-releases/"])}, "contract.releasePrefix"),
        ({"contract": default_contract(documentName="AWS-RunShellScript")}, "contract.document"),
        ({"contract": default_contract(runDocumentName="AWS-RunShellScript")}, "contract.runDocument"),
        ({"ping": "ConnectionLost"}, "instance.online"),
        ({"existing_params": ["/app/it/knowledge_base_id"]}, "guard.knowledge_base_id"),
        ({"runtimes": ["itassistant"]}, "guard.agentRuntime"),
    ],
)
def test_preflight_fails_closed(cfg, kwargs, failing_check):
    clients = StubClients(**kwargs)
    pf = sync.preflight(cfg, clients, expected_template_commit=TEMPLATE_COMMIT, pack_namespace=NAMESPACE)
    assert not pf.ok
    assert failing_check in {c.name for c in pf.checks if c.status == "fail"}


def test_a_direct_mode_harness_is_not_a_workshop_clash(cfg):
    """direct.names: the pack's direct Harness runtime (harness_direct_<agent>) is no Workshop resource; an older
    ``<agent>_direct`` one would be (09 and 13 select ``harness_<agent>_*``), so it still fails closed."""
    agent = NAMESPACE["agentName"]
    pf = sync.preflight(cfg, StubClients(runtimes=[f"harness_direct_{agent}"]), expected_template_commit=TEMPLATE_COMMIT, pack_namespace=NAMESPACE)
    assert pf.ok, pf.failures()
    pf = sync.preflight(cfg, StubClients(runtimes=[f"harness_{agent}_direct"]), expected_template_commit=TEMPLATE_COMMIT, pack_namespace=NAMESPACE)
    assert [c.status for c in pf.checks if c.name == "guard.agentRuntime"] == ["fail"]


def test_preflight_refuses_add_ons_bound_to_another_namespace(cfg):
    """The add-ons' execution IAM names one pack namespace: a release with another one is refused before
    sync (steps 01/02 would be AccessDenied), naming the redeploy."""
    hr = yaml.safe_load((REPO_ROOT / "scenarios" / "hr-default" / "scenario.yaml").read_text(encoding="utf-8"))["namespace"]
    clients = StubClients(stacks=default_stacks(hr))
    pf = sync.preflight(cfg, clients, expected_template_commit=TEMPLATE_COMMIT, pack_namespace=IT_NAMESPACE)
    [check] = [c for c in pf.checks if c.name == "stack.addons.namespace"]
    assert check.status == "fail" and "'hrassistant' vs pack 'itassistant'" in check.detail
    assert "deploy_addons.py --scenario" in check.detail and not pf.ok
    ok = sync.preflight(cfg, StubClients(), expected_template_commit=TEMPLATE_COMMIT, pack_namespace=IT_NAMESPACE)
    assert ok.ok and [c.status for c in ok.checks if c.name == "stack.addons.namespace"] == ["ok"]


def test_preflight_checks_a_previous_namespace_of_the_project(cfg):
    """A release whose namespace changed must also find the earlier namespace's resources gone."""
    old = {**IT_NAMESPACE, "agentName": "acmeit", "ssmParameterPrefix": "/app/acme-it"}
    clients = StubClients(existing_params=["/app/acme-it/gateway_arn"], runtimes=["harness_acmeit_x"])
    pf = sync.preflight(cfg, clients, expected_template_commit=TEMPLATE_COMMIT, pack_namespace=IT_NAMESPACE,
                        previous_namespaces=[old, IT_NAMESPACE, old])
    previous = {c.name: c for c in pf.checks if c.name.startswith("guard.previous.")}
    assert set(previous) == {"guard.previous.knowledge_base_id", "guard.previous.gateway_arn", "guard.previous.agentRuntime"}
    assert previous["guard.previous.gateway_arn"].status == "fail" and "99-cleanup.sh --scenario-only" in previous["guard.previous.gateway_arn"].detail
    # that release's cleanup is denied unless the add-ons are bound to its namespace (SKILL §6.5)
    assert "add-ons bound to that namespace (tools/deploy_addons.py --scenario" in previous["guard.previous.gateway_arn"].detail
    assert previous["guard.previous.agentRuntime"].status == "fail" and previous["guard.previous.knowledge_base_id"].status == "ok"
    assert not pf.ok and sync.resources_present(pf.checks)


def test_preflight_stops_early_when_stacks_are_missing(cfg):
    clients = StubClients(stacks={"workshop-infra": default_stacks()["workshop-infra"]})
    pf = sync.preflight(cfg, clients, expected_template_commit=TEMPLATE_COMMIT, pack_namespace=NAMESPACE)
    assert not pf.ok and any(c.name == "stack.addons" and "customizer-addons.json" in c.detail for c in pf.checks)
    assert "ssm.get_parameter" not in clients.names()


def test_bundle_is_deterministic_and_verified(release, tmp_path):
    a, sha_a = sync.build_bundle(release.release_dir, tmp_path / "a" / "release.zip")
    b, sha_b = sync.build_bundle(release.release_dir, tmp_path / "b" / "release.zip")
    assert sha_a == sha_b and a.with_suffix(".sha256").read_text().startswith(sha_a)
    import shutil

    tampered = tmp_path / "tampered-release"
    shutil.copytree(release.release_dir, tampered)
    (tampered / "pack" / "pack.env").write_text("tampered\n")
    with pytest.raises(render.RenderError):
        sync.build_bundle(tampered, tmp_path / "c" / "release.zip")


def test_release_key_confined_to_prefix():
    assert sync.release_key("align-pilot", "it-helpdesk-688e337c86de") == "customizer-releases/align-pilot/it-helpdesk-688e337c86de/release.zip"
    with pytest.raises(sync.SyncError):
        sync.release_key("../escape", "v")


def test_send_apply_uses_typed_parameters_only():
    clients = StubClients()
    cid = sync.send_apply(clients, instance_id=INSTANCE, document_name=DOC, source_uri=f"s3://{BUCKET}/customizer-releases/p/it-helpdesk-688e337c86de/release.zip", sha256="a" * 64, version="it-helpdesk-688e337c86de")
    assert cid == "cmd-123"
    call = dict(clients.calls[-1][1])
    assert call["DocumentName"] == DOC and call["InstanceIds"] == [INSTANCE]
    assert set(call["Parameters"]) == {"Action", "SourceUri", "ExpectedSha256", "ExpectedVersion", "WatchdogMinutes"}
    assert call["Parameters"]["Action"] == ["apply"]
    with pytest.raises(sync.SyncError):
        sync.send_apply(clients, instance_id=INSTANCE, document_name=DOC, source_uri=f"s3://{BUCKET}/hr/release.zip", sha256="a" * 64, version="it-helpdesk-688e337c86de")


def test_send_action_sanitises_reason_and_rejects_unknown_actions():
    clients = StubClients()
    sync.send_action(clients, instance_id=INSTANCE, document_name=DOC, action="rollback", reason="smoke failed; $(rm -rf /)")
    params = clients.calls[-1][1]["Parameters"]
    assert params["Action"] == ["rollback"] and "$(" not in params["Reason"][0] and ";" not in params["Reason"][0]
    with pytest.raises(sync.SyncError):
        sync.send_action(clients, instance_id=INSTANCE, document_name=DOC, action="apply")


def test_wait_for_command_parses_applier_json():
    clients = StubClients()
    slept: list[float] = []
    outcome = sync.wait_for_command(clients, "cmd-123", INSTANCE, poll=1, sleep=slept.append)
    assert outcome["ssmStatus"] == "Success" and outcome["result"]["status"] == "pending-commit" and slept == [1]


def test_sync_release_end_to_end_with_stubs(cfg, release, tmp_path):
    clients = StubClients(expiry=datetime.now(timezone.utc) + timedelta(hours=1))
    seen: dict = {}

    def confirm(plan):
        seen["plan"] = plan
        return True

    result = sync.sync_release(cfg, clients, release.release_dir, project="align-pilot", work_dir=tmp_path / "work", confirm=confirm, sleep=lambda _s: None)
    plan = seen["plan"]
    assert plan.version == release.version and plan.pack_id == "it-helpdesk" and plan.instance_id == INSTANCE
    assert result["status"] == "pending-commit" and result["commandId"] == "cmd-123"
    names = clients.names()
    assert names.index("s3.put_object") < names.index("ssm.send_command")
    uploads = [c[1]["Key"] for c in clients.calls if c[0] == "s3.put_object"]
    assert uploads == [
        f"customizer-releases/align-pilot/{release.version}/release.zip",
        f"customizer-releases/align-pilot/{release.version}/release.sha256",
        f"customizer-releases/align-pilot/{release.version}/RELEASE.json",
    ]
    send = next(c[1] for c in clients.calls if c[0] == "ssm.send_command")
    assert send["Parameters"]["ExpectedVersion"] == [release.version] and send["Parameters"]["ExpectedSha256"] == [plan.sha256]


def test_sync_release_respects_the_confirm_gate_and_preflight(cfg, release, tmp_path):
    clients = StubClients()
    result = sync.sync_release(cfg, clients, release.release_dir, project="align-pilot", work_dir=tmp_path / "w", confirm=lambda _plan: False)
    assert result["status"] == "not-confirmed"
    assert "s3.put_object" not in clients.names() and "ssm.send_command" not in clients.names()

    bad = StubClients(account="999999999999")
    with pytest.raises(sync.SyncError, match="preflight failed"):
        sync.sync_release(cfg, bad, release.release_dir, project="align-pilot", work_dir=tmp_path / "w2", confirm=lambda _plan: True)
    assert "s3.put_object" not in bad.names()


def _stacks_with_run_document_sha(value):
    stacks = default_stacks()
    outputs = [o for o in stacks["workshop-customizer-addons"]["Outputs"] if o["OutputKey"] != "RunStepDocumentSha256"]
    if value is not None:
        outputs.append({"OutputKey": "RunStepDocumentSha256", "OutputValue": value})
    stacks["workshop-customizer-addons"]["Outputs"] = outputs
    return stacks


@pytest.mark.parametrize("deployed, expected", [("0" * 64, "is stale (stack 000000000000"), (None, "has no RunStepDocumentSha256 output")])
def test_preflight_refuses_a_stale_or_unknown_run_step_document(cfg, deployed, expected):
    clients = StubClients(stacks=_stacks_with_run_document_sha(deployed))
    pf = sync.preflight(cfg, clients, expected_template_commit=TEMPLATE_COMMIT, pack_namespace=NAMESPACE)
    [check] = [c for c in pf.checks if c.name == "stack.addons.runDocument"]
    assert not pf.ok and check.status == "fail" and expected in check.detail and "customizer-addons.json" in check.detail
    assert pf.run_document_sha256 == (deployed or "")


def test_preflight_compares_with_the_local_document(cfg, tmp_path, monkeypatch):
    clients = StubClients()
    pf = sync.preflight(cfg, clients, expected_template_commit=TEMPLATE_COMMIT, pack_namespace=NAMESPACE)
    [check] = [c for c in pf.checks if c.name == "stack.addons.runDocument"]
    assert check.status == "ok" and pf.run_document_sha256 == sync.run_document_sha256()
    edited = json.loads(sync.RUN_DOCUMENT_PATH.read_text(encoding="utf-8"))
    edited["description"] += " (edited locally)"
    local = tmp_path / "WorkshopCustomizerRunStep.json"
    local.write_text(json.dumps(edited), encoding="utf-8")
    assert sync.run_document_sha256(local) != sync.run_document_sha256()
    monkeypatch.setattr(sync, "RUN_DOCUMENT_PATH", local)
    pf = sync.preflight(cfg, clients, expected_template_commit=TEMPLATE_COMMIT, pack_namespace=NAMESPACE)
    assert "stack.addons.runDocument" in {c.name for c in pf.checks if c.status == "fail"}
    monkeypatch.setattr(sync, "RUN_DOCUMENT_PATH", tmp_path / "missing.json")
    with pytest.raises(sync.SyncError, match="cannot read the local RunStep document"):
        sync.run_document_sha256()
    pf = sync.preflight(cfg, clients, expected_template_commit=TEMPLATE_COMMIT, pack_namespace=NAMESPACE)
    [check] = [c for c in pf.checks if c.name == "stack.addons.runDocument"]
    assert check.status == "fail" and "cannot read" in check.detail


def test_run_document_sha_is_canonical_json():
    document = json.loads(sync.RUN_DOCUMENT_PATH.read_text(encoding="utf-8"))
    reordered = json.loads(json.dumps(document, sort_keys=True, indent=4))
    assert sync.document_sha256(reordered) == sync.run_document_sha256()
