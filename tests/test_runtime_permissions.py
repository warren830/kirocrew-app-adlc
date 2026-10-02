import importlib.util
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "sync/host/runtime_permissions.py"
spec = importlib.util.spec_from_file_location("runtime_permissions", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_image_pull_permissions_are_read_only_and_repository_scoped():
    policy = module.image_pull_policy("123456789012.dkr.ecr.us-west-2.amazonaws.com/harness-us-west-2:latest")
    token, pull = policy["Statement"]
    assert token["Action"] == ["ecr:GetAuthorizationToken"]
    assert pull["Resource"] == "arn:aws:ecr:us-west-2:123456789012:repository/harness-us-west-2"
    assert set(pull["Action"]) == {"ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"}


@pytest.mark.parametrize("uri", ["https://example.com/image:latest", "evil.dkr.ecr.us-west-2.amazonaws.com/repo:tag",
                               "123456789012.dkr.ecr.us-west-2.amazonaws.com/../admin:latest"])
def test_non_ecr_or_escaping_repository_is_refused(uri):
    with pytest.raises(ValueError):
        module.image_pull_policy(uri)


@pytest.fixture
def deployed(tmp_path):
    version = "demo-0123456789ab"
    release = tmp_path / "releases" / version
    release.mkdir(parents=True)
    (tmp_path / "current").symlink_to(release)
    (release / "RELEASE.json").write_text(json.dumps({"namespace": {"agentName": "demoagent"}}))
    path = tmp_path / "demoagent/agentcore/.cli/deployed-state.json"
    path.parent.mkdir(parents=True)
    role = "arn:aws:iam::123456789012:role/demoagent_demoagent"
    path.write_text(json.dumps({"targets": {"default": {"resources": {"harnesses": {"demoagent": {
        "harnessId": "demoagent-0123456789", "roleArn": role,
        "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/demo-runtime",
    }}}}}}))
    config = tmp_path / "demoagent/app/demoagent/harness.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"name": "demoagent"}))
    return {"targetRoot": str(tmp_path), "region": "us-west-2"}, version, path, role


def test_configure_grants_only_the_bound_runtime_repository(deployed, monkeypatch):
    contract, version, _path, role = deployed
    writes = []

    def request(region, service, operation, *args):
        if service == "sts":
            return {"Account": "123456789012"}
        if operation == "get-harness":
            return {"harness": {"status": "READY", "environmentVariables": {"UNIFIED_TRACES_DESTINATION_ENABLED": "false"},
                               "environmentArtifact": {"containerConfiguration": {
                "containerUri": "234567890123.dkr.ecr.us-west-2.amazonaws.com/harness-us-west-2:latest",
            }}}}
        if operation == "get-agent-runtime":
            return {"roleArn": role, "agentRuntimeArtifact": {"containerConfiguration": {
                "containerUri": "234567890123.dkr.ecr.us-west-2.amazonaws.com/harness-us-west-2:latest",
            }}}
        writes.append((service, operation, args))
        return {}

    monkeypatch.setattr(module, "aws", request)
    result = module.configure(contract, version)
    assert result["repositoryArn"] == "arn:aws:ecr:us-west-2:234567890123:repository/harness-us-west-2"
    assert len(writes) == 1 and writes[0][:2] == ("iam", "put-role-policy")
    assert writes[0][2][:2] == ("--role-name", "demoagent_demoagent")
    config = Path(contract["targetRoot"]) / "demoagent/app/demoagent/harness.json"
    assert json.loads(config.read_text())["containerUri"].endswith("/harness-us-west-2:latest")
    assert json.loads(config.read_text())["environmentVariables"]["UNIFIED_TRACES_DESTINATION_ENABLED"] == "false"


def test_configure_refuses_a_role_outside_the_namespace(deployed, monkeypatch):
    contract, version, path, _role = deployed
    state = json.loads(path.read_text())
    state["targets"]["default"]["resources"]["harnesses"]["demoagent"]["roleArn"] = "arn:aws:iam::123456789012:role/admin"
    path.write_text(json.dumps(state))
    calls = []

    def request(region, service, operation, *args):
        calls.append(service)
        return {"Account": "123456789012"}

    monkeypatch.setattr(module, "aws", request)
    with pytest.raises(ValueError, match="namespace/account"):
        module.configure(contract, version)
    assert calls == ["sts"]


def test_service_default_image_is_explicitly_refreshed(deployed, monkeypatch):
    contract, version, _path, role = deployed
    image = "234567890123.dkr.ecr.us-west-2.amazonaws.com/harness-us-west-2:latest"
    updates = []

    def request(region, service, operation, *args):
        if service == "sts":
            return {"Account": "123456789012"}
        if operation == "get-harness":
            return {"harness": {"status": "READY", "environmentArtifact": {"containerConfiguration": {"containerUri": image}} if updates else None,
                               "environmentVariables": {"UNIFIED_TRACES_DESTINATION_ENABLED": "false"}}}
        if operation == "get-agent-runtime":
            return {"roleArn": role, "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image}}}
        if operation == "update-harness":
            updates.append(json.loads(args[3]))
        return {}

    monkeypatch.setattr(module, "aws", request)
    module.configure(contract, version)
    assert updates == [{"optionalValue": {"containerConfiguration": {"containerUri": image}}}]


def test_trace_compatibility_and_target_file_ownership(deployed, monkeypatch):
    contract, version, _path, role = deployed
    contract.update(user="workshop-user", group="workshop-group")
    image = "234567890123.dkr.ecr.us-west-2.amazonaws.com/harness-us-west-2:latest"
    environment = {"EXISTING_SETTING": "keep"}
    owners = []

    def request(region, service, operation, *args):
        if service == "sts":
            return {"Account": "123456789012"}
        if operation == "get-harness":
            return {"harness": {"status": "READY", "environmentVariables": dict(environment),
                               "environmentArtifact": {"containerConfiguration": {"containerUri": image}}}}
        if operation == "get-agent-runtime":
            return {"roleArn": role, "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image}}}
        if operation == "update-harness":
            environment.update(json.loads(args[3]))
        return {}

    monkeypatch.setattr(module, "aws", request)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=1234))
    monkeypatch.setattr(module.grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=5678))
    monkeypatch.setattr(module.os, "chown", lambda path, uid, gid: owners.append((uid, gid)))
    module.configure(contract, version)
    assert environment == {"EXISTING_SETTING": "keep", "UNIFIED_TRACES_DESTINATION_ENABLED": "false"}
    assert owners == [(1234, 5678)]
