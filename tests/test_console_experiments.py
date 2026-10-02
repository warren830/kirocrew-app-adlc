"""engine console.experiments: configuration bundles, the A/B test through a Gateway, the canary ramp, traffic, the
evidence gate and its override, cleanup (fakes for every AWS call; the shapes are the ones the live probe used)."""
from __future__ import annotations

import binascii
import json
import struct
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer.console import evaluation as ev  # noqa: E402
from workshop_customizer.console import experiments as ex  # noqa: E402
from workshop_customizer.console.jobs import Jobs  # noqa: E402
from workshop_customizer.console.store import Store  # noqa: E402

ACCOUNT, REGION = "111122223333", "us-west-2"
AGENT_ID = "hr_agent-AbCdEfGhIj"
AGENT_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:harness/{AGENT_ID}"
RUNTIME_ID = "harness_hr_agent-RtRtRtRtRt"
MEMORY = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:memory/hr_agent-MeMeMeMeMe"
MODEL = "us.amazon.nova-2-lite-v1:0"
PROMPT = "You help with HR."
TREATMENT = "You help with HR. Cite the policy you used."


class AwsError(Exception):
    """What botocore raises: the code in ``response["Error"]``."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"An error occurred ({code}): {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


def agent_harness() -> dict:
    return {"harnessId": AGENT_ID, "harnessName": "hr_agent", "arn": AGENT_ARN, "status": "READY", "harnessVersion": "3",
            "executionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/hr_agent-console-harness",
            "model": {"bedrockModelConfig": {"modelId": MODEL, "apiFormat": "converse_stream"}}, "systemPrompt": [{"text": PROMPT}],
            "tools": [{"type": "agentcore_gateway", "name": "hrtools", "config": {"agentCoreGateway": {"gatewayArn": "arn:gw"}}}],
            "allowedTools": ["@hrtools/*"], "skills": [], "memory": {"managedMemoryConfiguration": {"arn": MEMORY}},
            "environment": {"agentCoreRuntimeEnvironment": {"agentRuntimeArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID}",
                                                            "agentRuntimeName": "harness_hr_agent", "agentRuntimeId": RUNTIME_ID,
                                                            "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
                                                            "networkConfiguration": {"networkMode": "PUBLIC"}}},
            "environmentVariables": {"UNIFIED_TRACES_DESTINATION_ENABLED": "false"},
            "truncation": {"strategy": "sliding_window", "config": {"slidingWindow": {"messagesCount": 150}}},
            "maxIterations": 30, "maxTokens": 8192, "timeoutSeconds": 300}


class Ctl:
    """bedrock-agentcore-control: every call recorded as (operation, kwargs)."""

    def __init__(self):
        self.calls, self.harnesses, self.bundles, self.tags = [], {AGENT_ID: agent_harness()}, {}, {}
        self.endpoints, self.gateways, self.targets, self.evals, self.seq = {}, {}, {}, {}, 0

    def _id(self, name: str) -> str:
        self.seq += 1
        return f"{name}-{self.seq:010d}"

    def ops(self, op: str) -> list[dict]:
        return [kw for o, kw in self.calls if o == op]

    # -- configuration bundles
    def create_configuration_bundle(self, **kw):
        self.calls.append(("create_configuration_bundle", kw))
        bid = self._id(kw["bundleName"])
        arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:configuration-bundle/{bid}"
        version = f"{9 - self.seq:08x}-0000-4000-8000-000000000000"  # later versions sort first by id, as live
        self.bundles[bid] = {"name": kw["bundleName"], "arn": arn, "versions": [{"versionId": version, "components": kw["components"],
                                                                                "parents": [], "message": kw.get("commitMessage"), "at": f"t{self.seq:03d}"}]}
        self.tags[arn] = dict(kw.get("tags") or {})
        return {"bundleId": bid, "bundleArn": arn, "versionId": version, "createdAt": "t"}

    def update_configuration_bundle(self, **kw):
        self.calls.append(("update_configuration_bundle", kw))
        b = self.bundles[kw["bundleId"]]
        assert kw["parentVersionIds"] == [b["versions"][-1]["versionId"]], "the first parent must be the latest version (live rule)"
        self.seq += 1
        version = f"{9 - self.seq:08x}-0000-4000-8000-000000000000"
        b["versions"].append({"versionId": version, "components": kw["components"], "parents": kw["parentVersionIds"],
                              "message": kw.get("commitMessage"), "at": f"t{self.seq:03d}"})
        return {"bundleId": kw["bundleId"], "bundleArn": b["arn"], "versionId": version, "updatedAt": "t"}

    def get_configuration_bundle(self, bundleId, **kw):
        b = self.bundles[bundleId]
        latest = b["versions"][-1]
        return {"bundleId": bundleId, "bundleArn": b["arn"], "bundleName": b["name"], "versionId": latest["versionId"], "components": latest["components"]}

    def get_configuration_bundle_version(self, bundleId, versionId):
        v = next(v for v in self.bundles[bundleId]["versions"] if v["versionId"] == versionId)
        return {"bundleId": bundleId, "versionId": versionId, "components": v["components"], "versionCreatedAt": v["at"],
                "lineageMetadata": {"parentVersionIds": v["parents"], "branchName": "mainline", "commitMessage": v["message"]}}

    def list_configuration_bundle_versions(self, bundleId, **kw):
        listed = sorted(self.bundles[bundleId]["versions"], key=lambda v: v["versionId"])  # by id, not by time (live)
        return {"versions": [{"versionId": v["versionId"], "versionCreatedAt": v["at"],
                              "lineageMetadata": {"parentVersionIds": v["parents"], "commitMessage": v["message"]}} for v in listed]}

    def list_configuration_bundles(self, **kw):
        return {"bundles": [{"bundleId": k, "bundleArn": b["arn"], "bundleName": b["name"], "createdAt": b["versions"][0]["at"]}
                            for k, b in self.bundles.items()]}

    def delete_configuration_bundle(self, bundleId):
        self.calls.append(("delete_configuration_bundle", {"bundleId": bundleId}))
        self.bundles.pop(bundleId)
        return {"bundleId": bundleId, "status": "DELETING"}

    def list_tags_for_resource(self, resourceArn):
        return {"tags": dict(self.tags.get(resourceArn, {}))}

    # -- harnesses
    def list_harnesses(self, **kw):
        return {"harnesses": [{"harnessId": h["harnessId"], "harnessName": h["harnessName"], "arn": h["arn"]} for h in self.harnesses.values()]}

    def get_harness(self, harnessId):
        if harnessId not in self.harnesses:
            raise AwsError("ResourceNotFoundException", harnessId)
        return {"harness": json.loads(json.dumps(self.harnesses[harnessId]))}

    def create_harness(self, **kw):
        self.calls.append(("create_harness", kw))
        hid = f"{kw['harnessName']}-TwTwTwTwTw"
        arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:harness/{hid}"
        runtime = f"harness_{kw['harnessName']}-TrTrTrTrTr"
        self.harnesses[hid] = {**{k: v for k, v in kw.items() if k not in ("tags", "clientToken")}, "harnessId": hid, "arn": arn, "status": "READY",
                               "harnessVersion": "1", "environment": {"agentCoreRuntimeEnvironment": {"agentRuntimeId": runtime}}}
        self.tags[arn] = dict(kw["tags"])
        return {"harness": {"harnessId": hid, "harnessName": kw["harnessName"], "arn": arn, "status": "CREATING",
                            "environment": {"agentCoreRuntimeEnvironment": {"agentRuntimeId": runtime}}}}

    def list_harness_versions(self, harnessId, **kw):
        if harnessId not in self.harnesses:
            raise AwsError("ResourceNotFoundException", harnessId)
        last = int(self.harnesses[harnessId]["harnessVersion"])
        return {"harnessVersions": [{"harnessId": harnessId, "harnessVersion": str(v), "status": "READY"} for v in range(1, last + 1)]}

    def update_harness(self, harnessId, **kw):
        self.calls.append(("update_harness", {"harnessId": harnessId, **kw}))
        h = self.harnesses[harnessId]
        for key in ("systemPrompt", "model"):
            if key in kw:
                assert "optionalValue" not in kw[key], "UpdateHarness takes systemPrompt / model as they are (botocore 1.43.90)"
                h[key] = kw[key]
        h["harnessVersion"] = str(int(h["harnessVersion"]) + 1)
        return {"harness": {"harnessId": harnessId, "status": "UPDATING"}}

    def delete_harness(self, harnessId):
        self.calls.append(("delete_harness", {"harnessId": harnessId}))
        if harnessId not in self.harnesses:
            raise AwsError("ResourceNotFoundException", harnessId)
        self.harnesses.pop(harnessId)

    def create_harness_endpoint(self, **kw):
        self.calls.append(("create_harness_endpoint", kw))
        self.endpoints[(kw["harnessId"], kw["endpointName"])] = kw
        return {"endpoint": {"endpointName": kw["endpointName"], "status": "CREATING"}}

    def get_harness_endpoint(self, harnessId, endpointName):
        return {"endpoint": {"endpointName": endpointName, "status": "READY", "liveVersion": self.endpoints[(harnessId, endpointName)]["targetVersion"]}}

    def delete_harness_endpoint(self, harnessId, endpointName, **kw):
        self.calls.append(("delete_harness_endpoint", {"harnessId": harnessId, "endpointName": endpointName}))
        if (harnessId, endpointName) not in self.endpoints:
            raise AwsError("ResourceNotFoundException", endpointName)
        self.endpoints.pop((harnessId, endpointName))

    # -- gateway
    def create_gateway(self, **kw):
        self.calls.append(("create_gateway", kw))
        gid = f"{kw['name']}-gwgwgwgwgw"
        self.gateways[gid] = kw
        return {"gatewayId": gid, "gatewayArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/{gid}",
                "gatewayUrl": f"https://{gid}.gateway.bedrock-agentcore.{REGION}.amazonaws.com", "status": "CREATING"}

    def get_gateway(self, gatewayIdentifier):
        return {"gatewayId": gatewayIdentifier, "status": "READY", "gatewayUrl": f"https://{gatewayIdentifier}.gateway.bedrock-agentcore.{REGION}.amazonaws.com"}

    def delete_gateway(self, gatewayIdentifier):
        self.calls.append(("delete_gateway", {"gatewayIdentifier": gatewayIdentifier}))
        if gatewayIdentifier not in self.gateways:
            raise AwsError("ResourceNotFoundException", gatewayIdentifier)
        if any(t["gw"] == gatewayIdentifier for t in self.targets.values()):
            raise AwsError("ValidationException", "the gateway still has targets")
        self.gateways.pop(gatewayIdentifier)

    def create_gateway_target(self, **kw):
        self.calls.append(("create_gateway_target", kw))
        tid = f"T{len(self.targets):09d}"
        self.targets[tid] = {"gw": kw["gatewayIdentifier"], **kw}
        return {"targetId": tid, "status": "CREATING"}

    def get_gateway_target(self, gatewayIdentifier, targetId):
        return {"targetId": targetId, "status": "READY"}

    def delete_gateway_target(self, gatewayIdentifier, targetId):
        self.calls.append(("delete_gateway_target", {"gatewayIdentifier": gatewayIdentifier, "targetId": targetId}))
        if targetId not in self.targets:
            raise AwsError("ResourceNotFoundException", targetId)
        self.targets.pop(targetId)

    # -- online evaluation
    def create_online_evaluation_config(self, **kw):
        self.calls.append(("create_online_evaluation_config", kw))
        cid = f"{kw['onlineEvaluationConfigName']}-OeOeOeOeOe"
        self.evals[cid] = kw
        return {"onlineEvaluationConfigId": cid, "onlineEvaluationConfigArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:online-evaluation-config/{cid}",
                "status": "CREATING"}

    def get_online_evaluation_config(self, onlineEvaluationConfigId):
        return {"onlineEvaluationConfigId": onlineEvaluationConfigId, "status": "ACTIVE"}

    def delete_online_evaluation_config(self, onlineEvaluationConfigId):
        self.calls.append(("delete_online_evaluation_config", {"onlineEvaluationConfigId": onlineEvaluationConfigId}))
        if onlineEvaluationConfigId not in self.evals:
            raise AwsError("ResourceNotFoundException", onlineEvaluationConfigId)
        self.evals.pop(onlineEvaluationConfigId)


class Data:
    """bedrock-agentcore: A/B tests behave as live (weights only while paused, summing to 100)."""

    def __init__(self):
        self.calls, self.tests, self.results = [], {}, None

    def create_ab_test(self, **kw):
        self.calls.append(("create_ab_test", kw))
        aid = f"{kw['name']}-abababab12"
        self.tests[aid] = {"abTestId": aid, "abTestArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:ab-test/{aid}", "name": kw["name"],
                           "status": "ACTIVE", "executionStatus": "RUNNING" if kw.get("enableOnCreate") else "NOT_STARTED", "variants": kw["variants"]}
        return {"abTestId": aid, "abTestArn": self.tests[aid]["abTestArn"], "status": "CREATING", "executionStatus": "NOT_STARTED"}

    def get_ab_test(self, abTestId):
        if abTestId not in self.tests:
            raise AwsError("ResourceNotFoundException", abTestId)
        return {**json.loads(json.dumps(self.tests[abTestId])), **({"results": self.results} if self.results else {})}

    def update_ab_test(self, abTestId, clientToken=None, **kw):
        self.calls.append(("update_ab_test", kw))
        test = self.tests[abTestId]
        if "variants" in kw:
            if test["executionStatus"] not in ("PAUSED", "NOT_STARTED"):
                raise AwsError("ValidationException", "Config updates only allowed when execution status is PAUSED or NOT_STARTED")
            if sum(v["weight"] for v in kw["variants"]) != 100:
                raise AwsError("ValidationException", "Variant weights must sum to 100")
            test["variants"] = kw["variants"]
        if "executionStatus" in kw:
            test["executionStatus"] = kw["executionStatus"]
        return {"abTestId": abTestId, "status": "UPDATING", "executionStatus": test["executionStatus"]}

    def delete_ab_test(self, abTestId):
        self.calls.append(("delete_ab_test", {"abTestId": abTestId}))
        assert self.tests[abTestId]["executionStatus"] == "STOPPED", "an A/B test is deleted once stopped"
        self.tests.pop(abTestId)


class Iam:
    def __init__(self):
        self.calls, self.roles = [], {}

    def create_role(self, **kw):
        self.calls.append(("create_role", kw))
        path = kw.get("Path") or "/"
        self.roles[kw["RoleName"]] = {"tags": kw["Tags"], "policies": {}, "path": path}
        return {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role{path}{kw['RoleName']}"}}

    def get_role(self, RoleName):
        role = self.roles.get(RoleName) or {}
        path = role.get("path") or "/"
        out = {"Arn": f"arn:aws:iam::{ACCOUNT}:role{path}{RoleName}", "Path": path}
        if role.get("boundary"):
            out["PermissionsBoundary"] = {"PermissionsBoundaryType": "Policy", "PermissionsBoundaryArn": role["boundary"]}
        return {"Role": out}

    def put_role_permissions_boundary(self, RoleName, PermissionsBoundary):
        self.calls.append(("put_role_permissions_boundary", {"RoleName": RoleName, "PermissionsBoundary": PermissionsBoundary}))
        self.roles[RoleName]["boundary"] = PermissionsBoundary

    def put_role_policy(self, **kw):
        self.calls.append(("put_role_policy", kw))
        self.roles[kw["RoleName"]]["policies"][kw["PolicyName"]] = json.loads(kw["PolicyDocument"])

    def list_role_tags(self, RoleName):
        if RoleName not in self.roles:
            raise AwsError("NoSuchEntity", f"role {RoleName}")
        return {"Tags": self.roles[RoleName]["tags"]}

    def list_role_policies(self, RoleName):
        return {"PolicyNames": list(self.roles[RoleName]["policies"])}

    def delete_role_policy(self, RoleName, PolicyName):
        self.roles[RoleName]["policies"].pop(PolicyName)

    def delete_role(self, RoleName):
        self.calls.append(("delete_role", {"RoleName": RoleName}))
        assert not self.roles[RoleName]["policies"], "a role's inline policies go first"
        self.roles.pop(RoleName)


class Logs:
    def __init__(self):
        self.created, self.deleted = [], []

    def create_log_group(self, logGroupName, tags=None):
        self.created.append(logGroupName)

    def delete_log_group(self, logGroupName):
        self.deleted.append(logGroupName)


class Session:
    def __init__(self):
        self.ctl, self.data, self.iam, self.logs = Ctl(), Data(), Iam(), Logs()

    def client(self, name, region_name=None, **kw):
        return {"bedrock-agentcore-control": self.ctl, "bedrock-agentcore": self.data, "iam": self.iam, "logs": self.logs}[name]


ADMIN = {"username": "ann", "role": "admin", "workspaces": ["*"]}


@pytest.fixture(autouse=True)
def instant(monkeypatch):
    monkeypatch.setattr(ex, "_sleep", lambda seconds: None)


@pytest.fixture()
def world(tmp_path):
    session = Session()
    console = SimpleNamespace(store=Store(tmp_path / "console"), jobs=Jobs(tmp_path / "console" / "jobs"), data_dir=tmp_path)
    return SimpleNamespace(session=session, console=console, x=ex.Ctx(console, "dev", session, REGION, ACCOUNT, ADMIN))


def finish(console, job_id: str) -> dict:
    deadline = time.monotonic() + 10
    while (job := console.jobs.get(job_id))["status"] == "running":
        assert time.monotonic() < deadline, "the job did not finish"
        time.sleep(0.02)
    return job


def running_experiment(w, weight=None, evaluators=None) -> dict:
    bundle = ex.create_bundle(w.x, {"agentId": AGENT_ID, "systemPrompt": TREATMENT})
    body = {"agentId": AGENT_ID, "bundleId": bundle["id"], "controlVersion": bundle["controlVersion"], "treatmentVersion": bundle["treatmentVersion"],
            "acknowledged": True, **({"treatmentWeight": weight} if weight else {}), **({"evaluators": evaluators} if evaluators else {})}
    out = ex.start_experiment(w.x, body)
    job = finish(w.console, out["job"]["id"])
    assert job["status"] == "succeeded", job.get("error")
    return ex._get(w.x, out["experiment"]["id"])


def results(c_mean, c_n, t_mean, t_n, evaluator="Builtin.Correctness"):
    """GetABTest's results as live (2026-10-01): the evaluator ARN, controlStats, variantResults with the statistics."""
    return {"analysisTimestamp": "2026-10-01T14:17:33Z", "evaluatorMetrics": [{
        "evaluatorArn": f"arn:aws:bedrock-agentcore:::evaluator/{evaluator}", "controlStats": {"variantName": "C", "sampleSize": c_n, "mean": c_mean},
        "variantResults": [{"variantName": "T1", "sampleSize": t_n, "mean": t_mean, "absoluteChange": t_mean - c_mean,
                            "percentChange": (t_mean - c_mean) / c_mean * 100, "pValue": 0.2, "confidenceInterval": {"lower": -0.3, "upper": 0.2},
                            "isSignificant": False}]}]}


def verification(w, rec, *, robust=True, bands=None, agent_version="3"):
    """A finished console verification of the experiment's treatment (what verify_treatment records)."""
    params = {"agent": "hr_agent", "agentId": AGENT_ID, "contractSet": "cs-1", "repeat": 3, "panel": True,
              "treatment": {"fingerprint": rec["treatment"]["fingerprint"], "agentVersion": agent_version}}
    doc = {"robust": robust, "holding": 4 if robust else 3, "contracts": [{"id": f"c{i}"} for i in range(4)], "repeat": 3,
           "promptOverride": True, "panel": {"bands": bands or {}}}
    return finish(w.console, w.console.jobs.start("verify", "dev", params, lambda job: doc)["id"])


# -- bundles -------------------------------------------------------------------------------------------------------------

def test_a_bundle_is_the_agents_current_configuration_then_the_treatment_as_its_next_version(world):
    w = world
    out = ex.create_bundle(w.x, {"agentId": AGENT_ID, "systemPrompt": TREATMENT, "name": "hr_agent_bundle"})
    [created] = w.session.ctl.ops("create_configuration_bundle")
    assert created["bundleName"] == "hr_agent_bundle" and created["tags"] == {"adlc:console": "1"}
    assert created["components"] == {AGENT_ARN: {"configuration": {"systemPrompt": PROMPT, "modelId": MODEL}}}  # keyed by the Harness ARN
    assert created["commitMessage"].startswith("control: hr_agent version 3")
    [updated] = w.session.ctl.ops("update_configuration_bundle")
    assert updated["parentVersionIds"] == [out["controlVersion"]] and updated["components"][AGENT_ARN]["configuration"] == {"systemPrompt": TREATMENT,
                                                                                                                             "modelId": MODEL}
    assert updated["commitMessage"].startswith("treatment: system prompt changed")
    more = ex.add_version(w.x, out["id"], {"model": "us.amazon.nova-pro-v1:0"})
    detail = ex.bundle_detail(w.x, out["id"])
    assert [v["versionId"] for v in detail["versions"]] == [out["controlVersion"], out["treatmentVersion"], more["versionId"]]  # by time, not by id
    assert detail["versions"][2]["components"][AGENT_ARN] == {"systemPrompt": TREATMENT, "model": "us.amazon.nova-pro-v1:0"} and detail["versions"][2]["latest"]
    assert [b["id"] for b in ex.list_bundles(w.x) if b["console"]] == [out["id"]]
    for bad in ({"agentId": AGENT_ID, "model": "Not A Model"}, {"agentId": AGENT_ID, "name": "1bad"}, {}):
        with pytest.raises(ex.ExperimentError):
            ex.create_bundle(w.x, bad)
    with pytest.raises(ex.ExperimentError, match="nothing changed"):
        ex.add_version(w.x, out["id"], {"model": "us.amazon.nova-pro-v1:0"})
    w.session.ctl.tags[detail["arn"]] = {}
    with pytest.raises(ex.ExperimentError) as refused:
        ex.delete_bundle(w.x, out["id"])
    assert refused.value.status == 403  # only the console's own
    w.session.ctl.tags[detail["arn"]] = {"adlc:console": "1"}
    assert ex.delete_bundle(w.x, out["id"]) == {"deleted": out["id"]} and ex.list_bundles(w.x) == []


def test_bundle_configurations_are_read_in_the_cli_and_launchpad_spellings_too():
    assert ex.read_configuration({"systemPrompt": "a", "modelId": "m"}) == {"systemPrompt": "a", "model": "m"}
    assert ex.read_configuration({"system_prompt": "b", "tools": {}}) == {"systemPrompt": "b", "model": None}  # Launchpad's
    assert ex.read_configuration({"systemPrompt": [{"text": "c"}], "model": {"bedrockModelConfig": {"modelId": "n"}}}) == {"systemPrompt": "c", "model": "n"}


# -- the experiment ----------------------------------------------------------------------------------------------------

def test_an_experiment_needs_an_acknowledgement_and_the_agent_still_on_the_bundles_control(world):
    w = world
    bundle = ex.create_bundle(w.x, {"agentId": AGENT_ID, "systemPrompt": TREATMENT})
    body = {"agentId": AGENT_ID, "bundleId": bundle["id"], "controlVersion": bundle["controlVersion"], "treatmentVersion": bundle["treatmentVersion"]}
    with pytest.raises(ex.ExperimentError, match="acknowledged") as refused:
        ex.start_experiment(w.x, body)
    assert refused.value.status == 400
    for bad in ({"treatmentWeight": 100}, {"treatmentWeight": 0}, {"evaluators": ["Builtin.Refusal"]}, {"metric": "Builtin.Faithfulness"},
                {"treatmentVersion": bundle["controlVersion"]}):
        with pytest.raises(ex.ExperimentError):
            ex.start_experiment(w.x, {**body, "acknowledged": True, **bad})
    w.session.ctl.harnesses[AGENT_ID]["systemPrompt"] = [{"text": "Someone changed it."}]
    with pytest.raises(ex.ExperimentError, match="no longer runs this bundle's control"):
        ex.start_experiment(w.x, {**body, "acknowledged": True})
    assert not w.session.ctl.ops("create_harness") and ex.list_experiments(w.x) == []  # nothing made on a refusal


def test_in_a_spoke_workspace_the_treatment_copy_runs_with_the_agents_role_only_when_it_is_the_consoles_own_and_bounded(world):
    """The copy is given the agent's own role (PassRole, which takes no boundary key): in a workspace with a permissions
    boundary only a console role on the console's path, given the boundary first; a role on / is refused before
    anything is created."""
    w = world
    boundary = f"arn:aws:iam::{ACCOUNT}:policy/adlc-console-spoke-boundary"
    x = ex.Ctx(w.console, "dev", w.session, REGION, ACCOUNT, ADMIN, boundary=boundary)
    iam, role = w.session.iam, "hr_agent-console-harness"
    iam.roles[role] = {"tags": [{"Key": "adlc:console", "Value": "1"}], "policies": {}, "path": "/"}  # made before roles had a path
    bundle = ex.create_bundle(x, {"agentId": AGENT_ID, "systemPrompt": TREATMENT})
    body = {"agentId": AGENT_ID, "bundleId": bundle["id"], "controlVersion": bundle["controlVersion"], "treatmentVersion": bundle["treatmentVersion"],
            "acknowledged": True}
    job = finish(w.console, ex.start_experiment(x, body)["job"]["id"])
    assert job["status"] == "failed" and "passes only the console's own roles" in job["error"]
    assert not w.session.ctl.ops("create_harness") and not iam.calls  # refused before anything was created or changed
    iam.roles[role]["path"] = "/adlc-console/"  # the console's own, made before the workspace had a boundary
    w.session.ctl.harnesses[AGENT_ID]["executionRoleArn"] = f"arn:aws:iam::{ACCOUNT}:role/adlc-console/{role}"
    job = finish(w.console, ex.start_experiment(x, body)["job"]["id"])
    assert job["status"] == "succeeded", job.get("error")
    [twin] = w.session.ctl.ops("create_harness")
    assert twin["executionRoleArn"] == f"arn:aws:iam::{ACCOUNT}:role/adlc-console/{role}" and iam.roles[role]["boundary"] == boundary
    assert iam.calls[0] == ("put_role_permissions_boundary", {"RoleName": role, "PermissionsBoundary": boundary})  # before the experiment's own role


def test_an_experiment_sets_up_the_treatment_harness_the_gateway_and_a_target_based_ab_test(world):
    w = world
    rec = running_experiment(w, evaluators=["Builtin.Correctness", "Builtin.Faithfulness"])
    hexid = rec["id"][4:]
    ctl, data = w.session.ctl, w.session.data
    [twin] = ctl.ops("create_harness")
    assert twin["harnessName"] == f"hr_agent_x{hexid}" and twin["executionRoleArn"] == agent_harness()["executionRoleArn"]
    assert twin["systemPrompt"] == [{"text": TREATMENT}] and twin["model"] == agent_harness()["model"]
    assert twin["tools"] == agent_harness()["tools"] and twin["allowedTools"] == ["@hrtools/*"] and twin["maxTokens"] == 8192
    assert twin["memory"] == {"agentCoreMemoryConfiguration": {"arn": MEMORY, "actorId": "{actorId}"}}  # the agent's memory, shared
    assert twin["environment"] == {"agentCoreRuntimeEnvironment": {"lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
                                                                   "networkConfiguration": {"networkMode": "PUBLIC"}}}
    assert twin["tags"] == {"adlc:console": "1", "adlc:experiment": rec["id"]}
    [endpoint] = ctl.ops("create_harness_endpoint")
    assert (endpoint["harnessId"], endpoint["endpointName"], endpoint["targetVersion"]) == (AGENT_ID, f"adlc_console_exp_{hexid}", "3")  # DEFAULT untouched
    [role] = [kw for o, kw in w.session.iam.calls if o == "create_role"]
    # on the console's path; the Gateway, both online evaluations and the A/B test get IAM's own ARN of it, path included
    assert role["Path"] == "/adlc-console/" and rec["role"]["arn"] == f"arn:aws:iam::{ACCOUNT}:role/adlc-console/adlc-console-exp-{hexid}"
    trust = json.loads(role["AssumeRolePolicyDocument"])["Statement"][0]
    assert trust["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"} and trust["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT}}
    policy = w.session.iam.roles[f"adlc-console-exp-{hexid}"]["policies"]["experiment"]["Statement"]
    arms = next(s for s in policy if s["Sid"] == "InvokeArms")
    assert "bedrock-agentcore:InvokeAgentRuntime" in arms["Action"] and AGENT_ARN in arms["Resource"] and rec["treatmentHarness"]["arn"] in arms["Resource"]
    assert {"LogQueries", "Judges", "ABTestRouting"} <= {s["Sid"] for s in policy}
    [gateway] = ctl.ops("create_gateway")
    assert gateway["authorizerType"] == "AWS_IAM" and "protocolType" not in gateway and gateway["roleArn"] == rec["role"]["arn"]
    targets = ctl.ops("create_gateway_target")
    for target, (arn, qualifier) in zip(targets, ((AGENT_ARN, f"adlc_console_exp_{hexid}"), (rec["treatmentHarness"]["arn"], "DEFAULT"))):
        passthrough = target["targetConfiguration"]["http"]["passthrough"]
        assert passthrough["endpoint"] == "https://bedrock-agentcore.us-west-2.amazonaws.com/harnesses" and passthrough["protocolType"] == "CUSTOM"
        assert passthrough["staticQueryParameters"] == {"harnessArn": arn, "qualifier": qualifier}
        assert target["credentialProviderConfigurations"] == [{"credentialProviderType": "GATEWAY_IAM_ROLE", "credentialProvider": {
            "iamCredentialProvider": {"service": "bedrock-agentcore", "region": REGION}}}]
    twin_runtime = rec["treatmentHarness"]["runtimeId"]
    assert w.session.logs.created == [f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-adlc_console_exp_{hexid}", f"/aws/bedrock-agentcore/runtimes/{twin_runtime}-DEFAULT"]
    c_eval, t_eval = ctl.ops("create_online_evaluation_config")
    assert c_eval["dataSourceConfig"] == {"cloudWatchLogs": {"logGroupNames": [f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-adlc_console_exp_{hexid}"],
                                                             "serviceNames": [f"harness_hr_agent.adlc_console_exp_{hexid}"]}}
    assert t_eval["dataSourceConfig"]["cloudWatchLogs"]["serviceNames"] == [f"harness_hr_agent_x{hexid}.DEFAULT"]
    assert c_eval["evaluators"] == [{"evaluatorId": "Builtin.Correctness"}, {"evaluatorId": "Builtin.Faithfulness"}]
    assert c_eval["rule"]["samplingConfig"] == {"samplingPercentage": 100.0} and c_eval["evaluationExecutionRoleArn"] == rec["role"]["arn"]
    [ab] = data.calls
    assert ab[0] == "create_ab_test" and ab[1]["enableOnCreate"] is True and ab[1]["roleArn"] == rec["role"]["arn"]
    assert ab[1]["variants"] == [{"name": "C", "weight": 95, "variantConfiguration": {"target": {"name": f"adlc-console-exp-{hexid}-c"}}},
                                 {"name": "T1", "weight": 5, "variantConfiguration": {"target": {"name": f"adlc-console-exp-{hexid}-t"}}}]  # the canary's first step
    assert ab[1]["gatewayFilter"] == {"targetPaths": [f"/adlc-console-exp-{hexid}-c/*"]}
    assert ab[1]["evaluationConfig"] == {"perVariantOnlineEvaluationConfig": [{"name": "C", "onlineEvaluationConfigArn": rec["onlineEvaluations"]["C"]["arn"]},
                                                                              {"name": "T1", "onlineEvaluationConfigArn": rec["onlineEvaluations"]["T1"]["arn"]}]}
    assert rec["status"] == "running" and rec["invokeUrl"] == f"{rec['gateway']['url']}/adlc-console-exp-{hexid}-c/invoke"
    with pytest.raises(ex.ExperimentError, match="already has an experiment"):
        running_experiment(w)


def test_the_canary_ramp_pauses_changes_the_weights_and_resumes(world):
    w = world
    rec = running_experiment(w)
    w.session.data.calls.clear()
    out = ex.set_split(w.x, rec["id"], {"treatmentWeight": 25})
    assert [kw.get("executionStatus") or [v["weight"] for v in kw["variants"]] for _, kw in w.session.data.calls] == ["PAUSED", [75, 25], "RUNNING"]
    assert out["weights"] == {"C": 75, "T1": 25} and [s["weights"]["T1"] for s in out["ramp"]] == [5, 25]
    for bad in (100, 0, "half"):
        with pytest.raises(ex.ExperimentError):
            ex.set_split(w.x, rec["id"], {"treatmentWeight": bad})
    w.session.data.results = results(0.8, 20, 0.6, 10)  # worse beyond the minimum band
    with pytest.raises(ex.ExperimentError, match="worse than the control beyond the noise band"):
        ex.set_split(w.x, rec["id"], {"treatmentWeight": 50})
    assert ex.set_split(w.x, rec["id"], {"treatmentWeight": 5})["weights"]["T1"] == 5  # back down: always allowed
    up = ex.set_split(w.x, rec["id"], {"treatmentWeight": 50, "acknowledged": True})
    assert up["weights"]["T1"] == 50 and up["ramp"][-1]["acknowledged"] is True
    assert ex.set_state(w.x, rec["id"], {"executionStatus": "PAUSED"})["status"] == "paused"
    assert ex.set_state(w.x, rec["id"], {"executionStatus": "RUNNING"})["status"] == "running"


def event(kind: str, payload: dict, message_type: str = "event") -> bytes:
    """One AWS event-stream message (prelude, string headers, payload, CRCs), as InvokeHarness streams them."""
    names = ((":event-type", kind), (":content-type", "application/json"), (":message-type", message_type))
    if message_type == "exception":
        names = ((":exception-type", kind), (":content-type", "application/json"), (":message-type", "exception"))
    headers = b"".join(bytes([len(n)]) + n.encode() + b"\x07" + struct.pack(">H", len(v)) + v.encode() for n, v in names)
    body = json.dumps(payload).encode()
    prelude = struct.pack(">II", 12 + len(headers) + len(body) + 4, len(headers))
    prelude += struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
    message = prelude + headers + body
    return message + struct.pack(">I", binascii.crc32(message) & 0xFFFFFFFF)


def test_traffic_goes_through_the_gateway_one_signed_session_per_question(world, monkeypatch):
    w = world
    rec = running_experiment(w)
    ev.put_contract_set(w.console.store, "dev", {"id": "cs-hr", "name": "HR", "contracts": [
        {"id": "leave", "query": "How many days of leave?", "expected": {"mustMention": ["15"]}},
        {"id": "salary", "query": "What does my colleague earn?", "expected": {"shouldRefuse": True}}]})
    sent = []

    def post(session, region, url, data, headers, **kw):
        sent.append((url, json.loads(data), dict(headers)))
        if "colleague" in sent[-1][1]["messages"][0]["content"][0]["text"]:
            return 200, "application/vnd.amazon.eventstream", event("runtimeClientError", {"message": "boom"}, "exception")
        return 200, "application/vnd.amazon.eventstream", event("contentBlockDelta", {"delta": {"text": "15 "}}) + event("contentBlockDelta", {"delta": {"text": "days."}})

    monkeypatch.setattr(ex, "post_signed", post)
    job = finish(w.console, ex.send_traffic(w.x, rec["id"], {"contractSet": "cs-hr", "repeat": 2})["id"])
    assert job["status"] == "succeeded" and len(sent) == 4
    url, body, headers = next(s for s in sent if "leave" in s[1]["messages"][0]["content"][0]["text"])  # four at a time: any order
    assert url == rec["invokeUrl"] and body["messages"] == [{"role": "user", "content": [{"text": "How many days of leave?"}]}]
    assert body["actorId"].startswith(f"exp-{rec['id'][4:]}-") and headers["Content-Type"] == "application/json"
    sids = {h["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"] for _, _, h in sent}
    assert len(sids) == 4 and all(33 <= len(s) <= 100 for s in sids)  # a new session each: the Gateway picks its arm
    result = job["result"]
    assert (result["sent"], result["failed"]) == (2, 2) and {s["answer"] for s in result["samples"] if not s["error"]} == {"15 days."}
    assert any("runtimeClientError" in (s["error"] or "") for s in result["samples"])
    with pytest.raises(ex.ExperimentError):
        ex.send_traffic(w.x, rec["id"], {"prompts": ["x"] * 201})


def test_ab_results_are_read_per_evaluator_with_their_polarity():
    [m] = ex.ab_metrics({"results": results(0.4057, 21, 0.084, 10, "Builtin.Helpfulness")})
    assert m["evaluator"] == "Builtin.Helpfulness" and m["control"] == {"n": 21, "mean": 0.4057} and m["polarity"] == 1
    assert m["treatment"]["n"] == 10 and m["treatment"]["ci"] == [-0.3, 0.2] and m["treatment"]["significant"] is False
    assert ex.polarity("arn:aws:bedrock-agentcore:::evaluator/Builtin.Harmfulness") == -1
    assert ex.ab_metrics({}) == []


# -- the gate ----------------------------------------------------------------------------------------------------------

def test_promotion_is_refused_without_a_robust_verification_of_the_treatment(world):
    w = world
    rec = running_experiment(w)
    w.session.data.results = results(0.70, 12, 0.80, 12)  # the treatment is better
    with pytest.raises(ex.ExperimentError) as refused:
        ex.promote(w.x, rec["id"], {})
    gate = refused.value.extra["gate"]
    assert refused.value.status == 409 and [(c["id"], c["ok"]) for c in gate["conditions"]] == [("verification", False), ("ab", True), ("agent", True)]
    assert "no console verification of this treatment" in str(refused.value)
    verification(w, rec, robust=False)
    with pytest.raises(ex.ExperimentError, match="not robust"):
        ex.promote(w.x, rec["id"], {})
    verification(w, rec, robust=True, agent_version="2")  # the treatment on another version of the agent
    with pytest.raises(ex.ExperimentError, match="ran on version 2"):
        ex.promote(w.x, rec["id"], {})
    assert not w.session.ctl.ops("update_harness") and ex._get(w.x, rec["id"])["status"] == "running"


def test_promotion_is_refused_when_the_treatment_is_worse_beyond_the_noise_band(world):
    w = world
    rec = running_experiment(w)
    verification(w, rec, bands={"Builtin.Correctness": 0.05})
    w.session.data.results = results(0.80, 12, 0.70, 10)  # Δ −0.10 < −0.05
    with pytest.raises(ex.ExperimentError, match="worse beyond the noise band") as refused:
        ex.promote(w.x, rec["id"], {})
    ab = refused.value.extra["gate"]["conditions"][1]
    assert (ab["ok"], ab["worse"], ab["delta"], ab["band"]) == (False, True, -0.1, 0.05) and "measured by verification" in ab["bandSource"]
    w.session.data.results = results(0.80, 2, 0.90, 2)
    with pytest.raises(ex.ExperimentError, match="too few scored sessions"):
        ex.promote(w.x, rec["id"], {})
    assert not w.session.ctl.ops("update_harness")
    w.session.data.results = results(0.80, 12, 0.77, 10)  # Δ −0.03: inside the band
    done = ex.promote(w.x, rec["id"], {})
    [update] = w.session.ctl.ops("update_harness")
    assert update["systemPrompt"] == [{"text": TREATMENT}] and "model" not in update  # only what the treatment changes, as the API takes it
    assert (done["fromVersion"], done["toVersion"], done["override"], done["gate"]["ok"]) == ("3", "4", False, True)
    assert w.session.data.tests[rec["abTest"]["id"]]["executionStatus"] == "STOPPED"  # the split ends, its results kept
    after = ex._get(w.x, rec["id"])
    assert after["status"] == "promoted" and after["finalResults"]["metrics"][0]["treatment"]["mean"] == 0.77
    assert ex.promotions(w.x)[0]["experiment"] == rec["id"]
    shown = ex.experiment_detail(w.x, rec["id"])["gate"]  # the agent is now version 4: the page shows the gate as decided
    assert shown["ok"] and shown["decidedAt"] == done["at"]
    with pytest.raises(ex.ExperimentError):
        ex.promote(w.x, rec["id"], {})  # once


def test_the_minimum_band_applies_when_the_panel_did_not_measure_the_metric(world):
    w = world
    rec = running_experiment(w)
    verification(w, rec, bands={})  # no panel band for Correctness: the strictest band
    w.session.data.results = results(0.80, 12, 0.77, 10)
    with pytest.raises(ex.ExperimentError) as refused:
        ex.promote(w.x, rec["id"], {})
    ab = refused.value.extra["gate"]["conditions"][1]
    assert ab["band"] == ex.MIN_BAND and ab["worse"] and "minimum band" in ab["bandSource"]


def test_an_admin_override_is_recorded_with_the_conditions_it_overrode(world):
    w = world
    rec = running_experiment(w)
    member = ex.Ctx(w.console, "dev", w.session, REGION, ACCOUNT, {"username": "bob", "role": "member", "workspaces": ["dev"]})
    with pytest.raises(ex.ExperimentError) as refused:
        ex.promote(member, rec["id"], {"acknowledged": True, "reason": "ship it"})
    assert refused.value.status == 403
    with pytest.raises(ex.ExperimentError, match="needs a reason"):
        ex.promote(w.x, rec["id"], {"acknowledged": True})
    done = ex.promote(w.x, rec["id"], {"acknowledged": True, "reason": "the customer demo needs the citations today"})
    assert done["override"] is True and done["failed"] == ["verification", "ab"] and done["reason"].startswith("the customer demo")
    assert done["by"] == "ann" and done["gate"]["ok"] is False and len(w.session.ctl.ops("update_harness")) == 1
    assert w.console.store.read("promotions", [])[0]["override"] is True  # the audit trail


def test_the_gate_refuses_a_promotion_onto_an_agent_that_changed_under_the_experiment(world):
    w = world
    rec = running_experiment(w)
    verification(w, rec, bands={"Builtin.Correctness": 0.05})
    w.session.data.results = results(0.70, 12, 0.80, 12)
    w.session.ctl.harnesses[AGENT_ID]["harnessVersion"] = "5"
    with pytest.raises(ex.ExperimentError, match="moved from version 3 to 5"):
        ex.promote(w.x, rec["id"], {})


def test_a_treatment_verification_is_a_console_job_the_gate_finds(world, monkeypatch):
    w = world
    rec = running_experiment(w)
    ev.put_contract_set(w.console.store, "dev", {"id": "cs-hr", "name": "HR", "contracts": [
        {"id": "leave", "query": "How many days of leave?", "expected": {"mustMention": ["15"]}}]})
    seen = {}

    class FakeVerify:
        def __init__(self, session, info, contracts, l1, **kw):
            seen.update(kw, info=info)

        def run(self):
            return {"robust": True, "holding": 1, "contracts": [{"id": "leave"}], "repeat": 2, "promptOverride": True,
                    "panel": {"bands": {"Builtin.Correctness": 0.04}}}

    monkeypatch.setattr(ex.verify, "Verify", FakeVerify)
    monkeypatch.setattr(ex.verify, "render_markdown", lambda doc: "# verification")
    job = finish(w.console, ex.verify_treatment(w.x, {"experiment": rec["id"], "contractSet": "cs-hr", "repeat": 2})["id"])
    assert job["kind"] == "verify" and job["params"]["treatment"]["fingerprint"] == rec["treatment"]["fingerprint"]
    assert seen["prompt"] == TREATMENT and seen["model"] is None and seen["repeat"] == 2 and seen["info"]["id"] == AGENT_ID  # per call, never an update
    found, running = ex.find_verification(w.x, ex._get(w.x, rec["id"]))
    assert found["id"] == job["id"] and found["robust"] and found["bands"] == {"Builtin.Correctness": 0.04} and not running
    with pytest.raises(ex.ExperimentError, match="current configuration"):
        ex.verify_treatment(w.x, {"agentId": AGENT_ID, "bundleId": rec["bundle"]["id"], "versionId": rec["bundle"]["control"], "contractSet": "cs-hr"})


# -- cleanup and routes ------------------------------------------------------------------------------------------------

def test_cleanup_deletes_what_the_experiment_made_and_nothing_else(world):
    w = world
    rec = running_experiment(w)
    w.session.data.results = results(0.70, 12, 0.80, 12)
    job = finish(w.console, ex.cleanup_experiment(w.x, rec["id"])["id"])
    assert job["status"] == "succeeded"
    ctl = w.session.ctl
    assert [r["result"] for r in job["result"]["cleanup"]] == ["deleted"] * len(job["result"]["cleanup"])
    assert not w.session.data.tests and not ctl.evals and not ctl.targets and not ctl.gateways and not ctl.endpoints
    assert set(ctl.harnesses) == {AGENT_ID} and not w.session.iam.roles  # the agent itself stays
    hexid = rec["id"][4:]
    assert f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-adlc_console_exp_{hexid}" in w.session.logs.deleted
    assert f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT" not in w.session.logs.deleted  # never the agent's own logs
    after = ex._get(w.x, rec["id"])
    assert after["status"] == "cleaned" and after["finalResults"]["metrics"][0]["treatment"]["mean"] == 0.80  # the evidence is kept


def test_cleanup_leaves_a_treatment_harness_that_is_not_this_experiments(world):
    w = world
    rec = running_experiment(w)
    w.session.ctl.tags[rec["treatmentHarness"]["arn"]] = {"adlc:console": "1", "adlc:experiment": "exp-00000000"}
    job = finish(w.console, ex.cleanup_experiment(w.x, rec["id"])["id"])
    left = next(r for r in job["result"]["cleanup"] if r["resource"].startswith("treatment Harness"))
    assert left["result"].startswith("left:") and rec["treatmentHarness"]["id"] in w.session.ctl.harnesses


def test_the_routes_live_under_experiments_and_promotion_is_admin_only():
    added = []
    ex.register(SimpleNamespace(add=lambda method, path, fn, admin=False: added.append((method, path, admin))))
    assert all(p.startswith("/workspaces/{wid}/experiments/") for _, p, _ in added)
    assert [(m, p) for m, p, admin in added if admin] == [("POST", "/workspaces/{wid}/experiments/ab-tests/{eid}/split"),
                                                          ("POST", "/workspaces/{wid}/experiments/ab-tests/{eid}/promote")]  # the split decides who gets the treatment
    assert ("DELETE", "/workspaces/{wid}/experiments/ab-tests/{eid}", False) in added and ("POST", "/workspaces/{wid}/experiments/bundles", False) in added


# -- the review's findings, each pinned ------------------------------------------------------------------------------------

def test_a_bundle_the_console_did_not_create_takes_a_new_version_only_when_acknowledged(world):
    """review L3: add_version checks the console's tag (as delete_bundle does): agent code reading that bundle would run it."""
    w = world
    out = ex.create_bundle(w.x, {"agentId": AGENT_ID, "systemPrompt": TREATMENT})
    arn = ex.bundle_detail(w.x, out["id"])["arn"]
    w.session.ctl.tags[arn] = {"team": "theirs"}
    with pytest.raises(ex.ExperimentError, match="acknowledged") as refused:
        ex.add_version(w.x, out["id"], {"model": "us.amazon.nova-pro-v1:0"})
    assert refused.value.status == 403 and len(w.session.ctl.ops("update_configuration_bundle")) == 1  # only the treatment of create_bundle
    assert ex.add_version(w.x, out["id"], {"model": "us.amazon.nova-pro-v1:0", "acknowledged": True})["versionId"]


def test_the_treatment_harness_is_pinned_and_a_changed_copy_or_control_endpoint_fails_the_gate(world):
    """review H4: a verification of the treatment Harness counts only on the version set up, the gate's (c) checks the
    copy and the control endpoint (not only the agent), and the console refuses to change the copy directly."""
    w = world
    rec = running_experiment(w)
    twin = rec["treatmentHarness"]
    assert twin["version"] == "1"  # pinned at setup
    w.session.data.results = results(0.70, 12, 0.80, 12)

    def of_twin(version):
        params = {"agent": twin["name"], "agentId": twin["id"], "agentVersion": version, "contractSet": "cs-1", "repeat": 2, "panel": False}
        doc = {"robust": True, "holding": 4, "contracts": [{"id": f"c{i}"} for i in range(4)], "repeat": 2, "panel": {"bands": {}}}
        return finish(w.console, w.console.jobs.start("verify", "dev", params, lambda job: doc)["id"])

    pinned = of_twin("1")
    time.sleep(1.05)  # a newer job file
    of_twin("2")  # the copy after someone changed it
    found, _ = ex.find_verification(w.x, ex._get(w.x, rec["id"]))
    assert found["id"] == pinned["id"] and found["of"] == "treatment Harness"
    assert ex.experiment_detail(w.x, rec["id"])["gate"]["ok"]
    w.session.ctl.harnesses[twin["id"]]["harnessVersion"] = "2"  # the copy changed under the A/B test
    agent = ex.experiment_detail(w.x, rec["id"])["gate"]["conditions"][2]
    assert not agent["ok"] and "treatment Harness" in agent["evidence"] and agent["arm"]["now"] == "2"
    w.session.ctl.harnesses[twin["id"]]["harnessVersion"] = "1"
    w.session.ctl.endpoints[(AGENT_ID, rec["controlEndpoint"])]["targetVersion"] = "5"  # the control endpoint moved
    with pytest.raises(ex.ExperimentError, match="control endpoint"):
        ex.promote(w.x, rec["id"], {})
    assert ex.twin_of(w.console, "dev", twin["id"])["id"] == rec["id"] and ex.twin_of(w.console, "dev", AGENT_ID) is None


def test_an_experiment_set_up_before_the_copy_was_pinned_is_pinned_to_its_first_version(world):
    """review 3 #11: a record from before ``treatmentHarness.version`` existed credited no verification of the treatment
    Harness; it is pinned to the copy's first version (the setup made it, the console never changes it), so a
    verification on that version counts and one on a later version does not, and the gate's (c) sees the change."""
    w = world
    rec = running_experiment(w)
    twin = rec["treatmentHarness"]
    w.console.store.update("experiments", {}, lambda all_: {**all_, rec["id"]: {**all_[rec["id"]], "treatmentHarness": {
        k: v for k, v in twin.items() if k != "version"}}})  # as the setup saved it before the upgrade
    w.session.data.results = results(0.70, 12, 0.80, 12)
    params = {"agent": twin["name"], "agentId": twin["id"], "agentVersion": "1", "contractSet": "cs-1", "repeat": 2, "panel": False}
    doc = {"robust": True, "holding": 4, "contracts": [{"id": f"c{i}"} for i in range(4)], "repeat": 2, "panel": {"bands": {}}}
    job = finish(w.console, w.console.jobs.start("verify", "dev", params, lambda j: doc)["id"])
    detail = ex.experiment_detail(w.x, rec["id"])
    assert detail["verification"]["id"] == job["id"] and detail["gate"]["ok"], detail["gate"]["conditions"]
    assert ex._get(w.x, rec["id"])["treatmentHarness"]["version"] == "1"  # kept on the record
    w.session.ctl.update_harness(twin["id"], systemPrompt="changed outside the console")
    agent = ex.experiment_detail(w.x, rec["id"])["gate"]["conditions"][2]
    assert not agent["ok"] and agent["arm"]["now"] == "2" and agent["arm"]["expected"] == "1"


def _hold(w, rec, kind, field, status):
    """A job that held the experiment when the console stopped (a new Jobs() marks it interrupted)."""
    jid = f"{kind}-{'0' * 9}1"
    w.console.jobs._save({"id": jid, "kind": kind, "workspace": "dev", "label": kind, "params": {"experiment": rec["id"]}, "status": "running",
                          "createdAt": "2026-10-01T00:00:00Z", "finishedAt": None, "log": [], "progress": {}, "result": None, "error": None})
    ex._save(w.x, rec["id"], status=status, **{field: jid})


def test_an_experiment_whose_job_the_console_restart_cut_off_is_released(world, tmp_path):
    """review M3: creating / cleaning no longer last forever after a restart: the setup is failed, the cleanup incomplete,
    and cleanup (and a new experiment) can run."""
    w = world
    rec = running_experiment(w)
    _hold(w, rec, "experiment", "job", "creating")
    with pytest.raises(ex.ExperimentError, match="still being set up"):
        ex.cleanup_experiment(w.x, rec["id"])  # its job still runs
    w.console.jobs = Jobs(tmp_path / "console" / "jobs")  # the console restarts
    after = ex._get(w.x, rec["id"])
    assert after["status"] == "failed" and "restarted" in after["error"]
    _hold(w, after, "experiment-cleanup", "cleanupJob", "cleaning")
    w.console.jobs = Jobs(tmp_path / "console" / "jobs")
    assert ex._get(w.x, rec["id"])["status"] == "cleanup_incomplete"
    job = finish(w.console, ex.cleanup_experiment(w.x, rec["id"])["id"])
    assert job["status"] == "succeeded" and ex._get(w.x, rec["id"])["status"] == "cleaned" and not w.session.ctl.gateways
    assert running_experiment(w)["status"] == "running"  # the agent is free for a new experiment


def test_promotion_decides_on_the_experiments_own_metric_and_another_one_is_an_override(world):
    """review M5: body.metric is not the gate's metric: asking for another evaluator is an override, recorded as such."""
    w = world
    rec = running_experiment(w, evaluators=["Builtin.Correctness", "Builtin.Helpfulness"])
    verification(w, rec, bands={"Builtin.Correctness": 0.05})
    w.session.data.results = results(0.92, 20, 0.55, 20)
    w.session.data.results["evaluatorMetrics"] += results(0.80, 20, 0.81, 20, "Builtin.Helpfulness")["evaluatorMetrics"]
    with pytest.raises(ex.ExperimentError, match="deciding on Builtin.Helpfulness instead is an override") as refused:
        ex.promote(w.x, rec["id"], {"metric": "Builtin.Helpfulness"})
    assert [c["id"] for c in refused.value.extra["gate"]["conditions"] if not c["ok"]] == ["ab", "metric"]
    assert not w.session.ctl.ops("update_harness")
    done = ex.promote(w.x, rec["id"], {"metric": "Builtin.Helpfulness", "acknowledged": True, "reason": "helpfulness is the point"})
    assert (done["metric"], done["requestedMetric"], done["override"], done["failed"]) == ("Builtin.Correctness", "Builtin.Helpfulness", True, ["ab", "metric"])


def test_the_split_is_an_admins_and_goes_above_half_only_once_the_gate_holds(world):
    """review M6: no member split, no start above 50 %, and no 50+ % before the gate holds (acknowledged or not)."""
    w = world
    bundle = ex.create_bundle(w.x, {"agentId": AGENT_ID, "systemPrompt": TREATMENT})
    with pytest.raises(ex.ExperimentError, match="at most 50 %"):
        ex.start_experiment(w.x, {"agentId": AGENT_ID, "bundleId": bundle["id"], "controlVersion": bundle["controlVersion"],
                                  "treatmentVersion": bundle["treatmentVersion"], "acknowledged": True, "treatmentWeight": 99})
    rec = running_experiment(w)
    member = ex.Ctx(w.console, "dev", w.session, REGION, ACCOUNT, {"username": "bob", "role": "member", "workspaces": ["dev"]})
    with pytest.raises(ex.ExperimentError) as refused:
        ex.set_split(member, rec["id"], {"treatmentWeight": 25})
    assert refused.value.status == 403
    with pytest.raises(ex.ExperimentError, match="above 50 % only once the gate holds"):
        ex.set_split(w.x, rec["id"], {"treatmentWeight": 99, "acknowledged": True})
    assert w.session.data.tests[rec["abTest"]["id"]]["variants"][1]["weight"] == 5
    verification(w, rec, bands={"Builtin.Correctness": 0.05})
    w.session.data.results = results(0.70, 12, 0.80, 12)
    assert ex.set_split(w.x, rec["id"], {"treatmentWeight": 80})["weights"] == {"C": 20, "T1": 80}


def test_a_cleanup_that_left_something_is_incomplete_and_runs_again(world, monkeypatch):
    """review L6: an AWS refusal leaves the experiment cleanup_incomplete (not cleaned), and the cleanup runs again."""
    w = world
    rec = running_experiment(w)
    real, refusing = w.session.ctl.delete_gateway, [True]

    def delete_gateway(gatewayIdentifier):
        if refusing[0]:
            raise AwsError("AccessDeniedException", "not authorized to perform: bedrock-agentcore:DeleteGateway")
        return real(gatewayIdentifier)

    monkeypatch.setattr(w.session.ctl, "delete_gateway", delete_gateway)
    finish(w.console, ex.cleanup_experiment(w.x, rec["id"])["id"])
    after = ex._get(w.x, rec["id"])
    assert after["status"] == "cleanup_incomplete" and not after.get("cleanedAt") and after["cleanupLeft"] == [f"Gateway {rec['gateway']['id']}"]
    refusing[0] = False
    finish(w.console, ex.cleanup_experiment(w.x, rec["id"])["id"])
    after = ex._get(w.x, rec["id"])
    assert after["status"] == "cleaned" and after["cleanedAt"] and not w.session.ctl.gateways
    with pytest.raises(ex.ExperimentError, match="already cleaned"):
        ex.cleanup_experiment(w.x, rec["id"])


def test_two_promotions_of_one_experiment_at_once_apply_the_treatment_once(world):
    """review 5 (the A/B module's twin of the canary's race): two promotions that passed their first checks together.
    The second reads the record again under the agent's lock, finds it promoted, and is refused: one UpdateHarness,
    one release."""
    import threading

    w = world
    rec = running_experiment(w)
    verification(w, rec)
    w.session.data.results = results(0.70, 12, 0.80, 12)
    outcomes: list = []

    def attempt():
        try:
            outcomes.append(("ok", ex.promote(w.x, rec["id"], {})))
        except ex.ExperimentError as exc:
            outcomes.append(("refused", exc))

    with ex.one_at_a_time(AGENT_ID):  # both wait here
        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:
            t.start()
        time.sleep(1.0)
    for t in threads:
        t.join(20)
    assert sorted(kind for kind, _ in outcomes) == ["ok", "refused"]
    assert "is promoted: nothing to promote" in str(next(out for kind, out in outcomes if kind == "refused"))
    assert len(w.session.ctl.ops("update_harness")) == 1
    assert len([p for p in w.console.store.read("promotions", []) if p.get("experiment") == rec["id"]]) == 1


def test_a_settle_writes_only_while_the_record_is_as_it_read_it(world):
    """A settle decides on what it read; a newer state another action saved meanwhile is not overwritten."""
    w = world
    rec = running_experiment(w)
    stale = ex._save(w.x, rec["id"], expect={"status": "cleaning", "cleanupJob": "experiment-cleanup-0000000001"}, status="cleanup_incomplete")
    assert stale["status"] == "running" and ex._get(w.x, rec["id"])["status"] == "running"
    assert ex._save(w.x, rec["id"], expect={"status": "running"}, error="x")["error"] == "x"
