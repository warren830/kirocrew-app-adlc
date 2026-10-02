"""engine console.runtime_canary: a candidate version of a console code agent beside production, split at a Gateway
with two runtime targets, the ramp, traffic, the console chat and /v1 through it, the evidence gate, promotion,
rollback before and after it, and cleanup. Every AWS request goes through the deploy tests' fake, which checks it
against the botocore model of its service; the shapes are the ones the live probe used."""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_console_deploy import ACCOUNT, BUCKET, REGION, REGISTRY, FakeAWS, FakeSession, b64, console_mod, digest, err, zip_of  # noqa: E402

from workshop_customizer.console import deploy as dp  # noqa: E402
from workshop_customizer.console import experiments as ex  # noqa: E402
from workshop_customizer.console import runtime_canary as rc  # noqa: E402
from workshop_customizer.console.jobs import Jobs  # noqa: E402
from workshop_customizer.console.store import Store  # noqa: E402

PROD = "team_agent"
ADMIN = {"username": "ann", "role": "admin", "workspaces": ["*"]}
MEMBER = {"username": "bob", "role": "member", "workspaces": ["dev"]}
CODE_KEY = "deployments/team_agent/deploy-0000000001/bundle.zip"
MAIN = "from bedrock_agentcore.runtime import BedrockAgentCoreApp\napp = BedrockAgentCoreApp()\n"


class CanaryAWS(FakeAWS):
    """The deploy fake plus what a canary calls: versioned runtimes (GetAgentRuntime of an old version), endpoint ARNs
    and tags, the DEFAULT endpoint refusing to be moved (live), a Gateway with targets, online evaluations, A/B tests
    that take weights only while paused (live), log groups and Logs Insights."""

    def __init__(self):
        super().__init__()
        self.snapshots: dict[str, dict[str, dict]] = {}
        self.gateways, self.targets, self.evals, self.tests, self.results = {}, {}, {}, {}, None
        self.queries: list[str] = []
        self.query_results: list = []

    def _snap(self, rid: str) -> None:
        rt = self.runtimes[rid]
        self.snapshots.setdefault(rid, {})[rt["agentRuntimeVersion"]] = json.loads(json.dumps({k: v for k, v in rt.items() if k not in ("polls", "versions")}))

    def seed_production(self, *, container: bool = False) -> dict:
        artifact = ({"containerConfiguration": {"containerUri": f"{REGISTRY}/adlc-console/team-agent@{digest('v1')}"}} if container else
                    {"codeConfiguration": {"code": {"s3": {"bucket": BUCKET, "prefix": CODE_KEY}}, "runtime": "PYTHON_3_13",
                                           "entryPoint": ["opentelemetry-instrument", "main.py"]}})
        rt = self.seed_runtime(PROD, source="dockerfile" if container else "zip", agentRuntimeArtifact=artifact, environmentVariables={"MODEL_ID": "m1"},
                               networkConfiguration={"networkMode": "PUBLIC"}, lifecycleConfiguration={"idleRuntimeSessionTimeout": 300, "maxLifetime": 3600})
        self.endpoints[rt["agentRuntimeId"]]["live"] = {"name": "live", "liveVersion": "1", "targetVersion": "1", "status": "READY", "polls": 0}
        self.seed_role(f"adlc-console-rt-{PROD}", {"adlc:console": "1", "adlc:runtime": PROD})
        self.seed_object(CODE_KEY)
        if container:
            self.seed_repo("adlc-console/team-agent", {"adlc:console": "1", "adlc:runtime": PROD})
        self._snap(rt["agentRuntimeId"])
        return rt

    # -- runtimes, versioned; endpoints with ARNs and tags --------------------------------------------------------------
    def ctl_get_agent_runtime(self, agentRuntimeId, agentRuntimeVersion=None, **p):
        if agentRuntimeVersion:
            return json.loads(json.dumps(self.snapshots[agentRuntimeId][agentRuntimeVersion]))
        out = super().ctl_get_agent_runtime(agentRuntimeId)
        if out.get("status") == "READY":
            self._snap(agentRuntimeId)
        return out

    def ctl_create_agent_runtime(self, **p):
        out = super().ctl_create_agent_runtime(**p)
        self._snap(out["agentRuntimeId"])
        return out

    def ctl_update_agent_runtime(self, agentRuntimeId, **p):
        out = super().ctl_update_agent_runtime(agentRuntimeId, **p)
        self._snap(agentRuntimeId)  # the new version answers GetAgentRuntime from UpdateAgentRuntime on
        return out

    def ctl_create_agent_runtime_endpoint(self, agentRuntimeId, name, agentRuntimeVersion=None, **p):
        out = super().ctl_create_agent_runtime_endpoint(agentRuntimeId, name, agentRuntimeVersion)
        self.tags[f"{self.runtimes[agentRuntimeId]['agentRuntimeArn']}/runtime-endpoint/{name}"] = dict(p.get("tags") or {})
        return out

    def ctl_get_agent_runtime_endpoint(self, agentRuntimeId, endpointName):
        out = super().ctl_get_agent_runtime_endpoint(agentRuntimeId, endpointName)
        return {**out, "agentRuntimeEndpointArn": f"{self.runtimes[agentRuntimeId]['agentRuntimeArn']}/runtime-endpoint/{endpointName}"}

    def ctl_update_agent_runtime_endpoint(self, agentRuntimeId, endpointName, agentRuntimeVersion=None, **p):
        if endpointName == "DEFAULT":
            raise err("ConflictException", "Default endpoints are managed through agent updates. Please use the update agent operation.")
        return super().ctl_update_agent_runtime_endpoint(agentRuntimeId, endpointName, agentRuntimeVersion)

    def ctl_list_harnesses(self, **p):
        return {"harnesses": []}

    # -- gateway ------------------------------------------------------------------------------------------------------
    def ctl_create_gateway(self, **p):
        gid = f"{p['name']}-gwgwgwgwgw"
        self.gateways[gid] = p
        return {"gatewayId": gid, "gatewayArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/{gid}",
                "gatewayUrl": f"https://{gid}.gateway.bedrock-agentcore.{REGION}.amazonaws.com", "status": "CREATING", "name": p["name"],
                "authorizerType": "AWS_IAM", "createdAt": "t", "updatedAt": "t"}

    def ctl_get_gateway(self, gatewayIdentifier):
        return {"gatewayId": gatewayIdentifier, "status": "READY", "gatewayUrl": f"https://{gatewayIdentifier}.gateway.bedrock-agentcore.{REGION}.amazonaws.com"}

    def ctl_delete_gateway(self, gatewayIdentifier):
        if gatewayIdentifier not in self.gateways:
            raise err("ResourceNotFoundException", f"gateway {gatewayIdentifier}")
        if any(t["gw"] == gatewayIdentifier for t in self.targets.values()):
            raise err("ValidationException", f"Gateway with ID: {gatewayIdentifier} has targets associated with it")
        self.gateways.pop(gatewayIdentifier)
        return {"gatewayId": gatewayIdentifier, "status": "DELETING"}

    def ctl_create_gateway_target(self, **p):
        tid = f"T{len(self.targets):09d}"
        self.targets[tid] = {"gw": p["gatewayIdentifier"], **p}
        return {"targetId": tid, "status": "CREATING", "gatewayArn": "arn:gw", "name": p["name"], "createdAt": "t", "updatedAt": "t",
                "targetConfiguration": p["targetConfiguration"], "credentialProviderConfigurations": p["credentialProviderConfigurations"]}

    def ctl_get_gateway_target(self, gatewayIdentifier, targetId):
        return {"targetId": targetId, "status": "READY"}

    def ctl_delete_gateway_target(self, gatewayIdentifier, targetId):
        if targetId not in self.targets:
            raise err("ResourceNotFoundException", f"target {targetId}")
        self.targets.pop(targetId)
        return {"targetId": targetId, "status": "DELETING"}

    # -- online evaluation ------------------------------------------------------------------------------------------
    def ctl_create_online_evaluation_config(self, **p):
        cid = f"{p['onlineEvaluationConfigName']}-OeOeOeOeOe"
        self.evals[cid] = p
        return {"onlineEvaluationConfigId": cid, "onlineEvaluationConfigArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:online-evaluation-config/{cid}",
                "status": "CREATING", "executionStatus": "ENABLED", "createdAt": "t"}

    def ctl_get_online_evaluation_config(self, onlineEvaluationConfigId):
        return {"onlineEvaluationConfigId": onlineEvaluationConfigId, "status": "ACTIVE"}

    def ctl_delete_online_evaluation_config(self, onlineEvaluationConfigId):
        if onlineEvaluationConfigId not in self.evals:
            raise err("ResourceNotFoundException", f"online evaluation {onlineEvaluationConfigId}")
        self.evals.pop(onlineEvaluationConfigId)
        return {"onlineEvaluationConfigId": onlineEvaluationConfigId, "status": "DELETING"}

    # -- A/B tests ------------------------------------------------------------------------------------------------------
    def data_create_ab_test(self, **p):
        aid = f"{p['name']}-abababab12"
        self.tests[aid] = {"abTestId": aid, "abTestArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:ab-test/{aid}", "name": p["name"], "status": "ACTIVE",
                           "executionStatus": "RUNNING" if p.get("enableOnCreate") else "NOT_STARTED", "variants": p["variants"]}
        return {"abTestId": aid, "abTestArn": self.tests[aid]["abTestArn"], "status": "CREATING", "executionStatus": "NOT_STARTED", "name": p["name"],
                "createdAt": "t"}

    def data_get_ab_test(self, abTestId):
        if abTestId not in self.tests:
            raise err("ResourceNotFoundException", abTestId)
        return {**json.loads(json.dumps(self.tests[abTestId])), **({"results": self.results} if self.results else {})}

    def data_update_ab_test(self, abTestId, clientToken=None, **p):
        test = self.tests[abTestId]
        if "variants" in p:
            if test["executionStatus"] not in ("PAUSED", "NOT_STARTED"):
                raise err("ValidationException", "Config updates only allowed when execution status is PAUSED or NOT_STARTED")
            if sum(v["weight"] for v in p["variants"]) != 100:
                raise err("ValidationException", "Variant weights must sum to 100")
            test["variants"] = p["variants"]
        if "executionStatus" in p:
            test["executionStatus"] = p["executionStatus"]
        return {"abTestId": abTestId, "abTestArn": test["abTestArn"], "status": "UPDATING", "executionStatus": test["executionStatus"]}

    def data_delete_ab_test(self, abTestId):
        assert self.tests[abTestId]["executionStatus"] == "STOPPED", "an A/B test is deleted once stopped"
        self.tests.pop(abTestId)
        return {"abTestId": abTestId, "status": "DELETING"}

    # -- logs, ecr -------------------------------------------------------------------------------------------------------
    def logs_create_log_group(self, logGroupName, **p):
        if logGroupName in self.log_groups:
            raise err("ResourceAlreadyExistsException", "exists")
        self.log_groups.add(logGroupName)
        return {}

    def logs_start_query(self, **p):
        self.queries.append(p["queryString"])
        return {"queryId": "q-1"}

    def logs_get_query_results(self, queryId):
        return {"status": "Complete", "results": self.query_results}

    def ecr_batch_delete_image(self, repositoryName, imageIds, **p):
        images = self.repos[repositoryName]["images"]
        for ident in imageIds:
            for tag, found in list(images.items()):
                if found == ident.get("imageDigest"):
                    images.pop(tag)
        return {"imageIds": imageIds, "failures": []}


class Workspaces:
    def __init__(self, session):
        self._session = session

    def get(self, wid):
        return {"id": wid, "region": REGION, "accountId": ACCOUNT}

    def verify(self, wid):
        return {"account": ACCOUNT}

    def session(self, wid):
        return self._session


@pytest.fixture(autouse=True)
def instant(monkeypatch):
    monkeypatch.setattr(ex, "_sleep", lambda seconds: None)
    monkeypatch.setattr(rc, "_sleep", lambda seconds: None)
    for owner, attrs in ((dp.Pipeline, ("BUILD_POLL", "RUNTIME_POLL", "ENDPOINT_POLL", "IAM_PAUSE")), (dp.Teardown, ("POLL",))):
        for attr in attrs:
            monkeypatch.setattr(owner, attr, 0.0)


def make_world(tmp_path, *, container=False):
    aws = CanaryAWS()
    prod = aws.seed_production(container=container)
    session = FakeSession(aws)
    console = SimpleNamespace(store=Store(tmp_path / "console"), jobs=Jobs(tmp_path / "console" / "jobs"), data_dir=tmp_path, workspaces=Workspaces(session))
    rid = prod["agentRuntimeId"]
    console.store.write("deployments", {rid: {"workspace": "dev", "runtimeId": rid, "runtimeArn": prod["agentRuntimeArn"], "name": PROD,
                                              "source": "dockerfile" if container else "zip", "role": f"adlc-console-rt-{PROD}", "endpoint": "live",
                                              "history": [{"version": "1", "jobId": "deploy-0000000001"}]}})
    return SimpleNamespace(aws=aws, session=session, console=console, rid=rid, arn=prod["agentRuntimeArn"],
                           x=ex.Ctx(console, "dev", session, REGION, ACCOUNT, ADMIN))


@pytest.fixture()
def world(tmp_path):
    return make_world(tmp_path)


def finish(console, job_id: str) -> dict:
    deadline = time.monotonic() + 20
    while (job := console.jobs.get(job_id))["status"] == "running":
        assert time.monotonic() < deadline, "the job did not finish"
        time.sleep(0.02)
    return job


def candidate(**extra) -> dict:
    return {"source": "zip", "archive": b64(zip_of({"main.py": MAIN})), "filename": "agent.zip", "installRequirements": False, "instrument": True,
            "environment": {"VARIANT": "v2"}, **extra}


def running(w, **body) -> dict:
    out = rc.start_canary(w.x, {"runtimeId": w.rid, "candidate": candidate(), "acknowledged": True, **body})
    job = finish(w.console, out["job"]["id"])
    assert job["status"] == "succeeded", (job.get("error"), job["log"][-6:])
    return rc._get(w.x, out["canary"]["id"])


def prod_writes(w) -> list[tuple[str, dict]]:
    """Every call that would change production itself: its runtime, its endpoints other than the canary's, its role."""
    out = []
    for service, op, params in w.aws.calls:
        if service == "bedrock-agentcore-control" and params.get("agentRuntimeId") == w.rid and op in ("update_agent_runtime", "delete_agent_runtime"):
            out.append((op, params))
        if service == "bedrock-agentcore-control" and params.get("agentRuntimeId") == w.rid and op.endswith("_agent_runtime_endpoint") and \
                not op.startswith("get") and not str(params.get("name") or params.get("endpointName") or "").startswith("adlc_console_can_"):
            out.append((op, params))
        if service == "iam" and params.get("RoleName") == f"adlc-console-rt-{PROD}" and op in ("put_role_policy", "delete_role", "update_assume_role_policy"):
            out.append((op, params))
    return out


def results(c_mean, c_n, t_mean, t_n, evaluator="Builtin.Correctness"):
    return {"analysisTimestamp": "2026-10-01T17:40:00Z", "evaluatorMetrics": [{
        "evaluatorArn": f"arn:aws:bedrock-agentcore:::evaluator/{evaluator}", "controlStats": {"variantName": "C", "sampleSize": c_n, "mean": c_mean},
        "variantResults": [{"variantName": "T1", "sampleSize": t_n, "mean": t_mean, "absoluteChange": t_mean - c_mean, "percentChange": 1.0,
                            "pValue": 0.3, "confidenceInterval": {"lower": -0.2, "upper": 0.3}, "isSignificant": False}]}]}


def verified(w, rec, *, robust=True, bands=None):
    """A finished console verification of the canary's candidate (what verify_candidate records)."""
    params = {"agent": PROD, "agentId": w.rid, "contractSet": "cs-1", "repeat": 2, "panel": True,
              "treatment": {"fingerprint": rec["treatment"]["fingerprint"], "agentVersion": rec["agent"]["version"], "canary": rec["id"],
                            "candidateVersion": rec["copy"]["version"]}}
    doc = {"robust": robust, "holding": 3 if robust else 2, "contracts": [{"id": f"c{i}"} for i in range(3)], "repeat": 2, "promptOverride": False,
           "panel": {"bands": bands or {}}}
    return finish(w.console, w.console.jobs.start("verify", "dev", params, lambda job: doc)["id"])


# -- requests and refusals -------------------------------------------------------------------------------------------------

def test_names_fit_the_services_and_the_role_invokes_both_runtimes():
    n = rc.names("0a1b2c3d", "a" * 48)
    assert len(n["copy"]) <= 48 and dp.NAME.match(n["copy"]) and all(dp.NAME.match(n[k]) for k in ("control", "treatment", "evalC", "evalT1", "abTest"))
    assert n["role"].startswith("adlc-console-") and n["C"].endswith("-c")  # a spoke role allows the console's roles under adlc-console-*
    target = rc.target_request("adlc-console-can-0a1b2c3d-c", "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/team_agent-AbCdEf1234", "adlc_x")
    assert target["targetConfiguration"] == {"http": {"agentcoreRuntime": {"arn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/team_agent-AbCdEf1234",
                                                                           "qualifier": "adlc_x"}}}
    assert target["credentialProviderConfigurations"] == [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]  # the live shape
    trust, policy = rc.role_documents(ACCOUNT, REGION, ["arn:a", "arn:b"])
    arms = next(s for s in policy["Statement"] if s["Sid"] == "InvokeArms")
    assert arms["Action"] == ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeAgentRuntimeForUser"]
    assert arms["Resource"] == ["arn:a", "arn:a/*", "arn:b", "arn:b/*"]  # the runtimes and their endpoints
    assert trust["Statement"][0]["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}


def test_a_canary_needs_an_admin_an_acknowledgement_and_an_http_console_runtime(world, tmp_path):
    w = world
    body = {"runtimeId": w.rid, "candidate": candidate(), "acknowledged": True}
    member = ex.Ctx(w.console, "dev", w.session, REGION, ACCOUNT, MEMBER)
    with pytest.raises(ex.ExperimentError) as refused:
        rc.start_canary(member, body)
    assert refused.value.status == 403
    with pytest.raises(ex.ExperimentError, match="acknowledged") as refused:
        rc.start_canary(w.x, {**body, "acknowledged": False})
    assert refused.value.status == 400
    theirs = w.aws.seed_runtime("someone_elses", console=False)["agentRuntimeId"]
    with pytest.raises(ex.ExperimentError, match="adlc:console=1"):
        rc.start_canary(w.x, {**body, "runtimeId": theirs})
    for bad, message in (({"candidate": candidate(source="dockerfile", archive=b64(zip_of({"Dockerfile": "FROM x"})))}, "is a zip too"),
                         ({"candidate": candidate(archive="@@")}, "base64"), ({"treatmentWeight": 100}, "100 % is the promotion"),
                         ({"evaluators": ["Builtin.Refusal"]}, "refused"), ({"candidate": "x"}, "candidate"),
                         ({"candidate": candidate(environment={"1BAD": "v"})}, "candidate: environment")):
        with pytest.raises(ex.ExperimentError, match=message):
            rc.start_canary(w.x, {**body, **bad})
    w.aws.runtimes[w.rid]["protocolConfiguration"] = {"serverProtocol": "MCP"}
    with pytest.raises(ex.ExperimentError, match="speaks MCP"):
        rc.start_canary(w.x, body)
    w.aws.runtimes[w.rid]["protocolConfiguration"] = {"serverProtocol": "HTTP"}
    w.aws.runtimes[w.rid]["authorizerConfiguration"] = {"customJWTAuthorizer": {"discoveryUrl": "https://idp/.well-known/openid-configuration"}}
    with pytest.raises(ex.ExperimentError, match="JWT"):
        rc.start_canary(w.x, body)
    assert rc.list_canaries(w.x) == [] and not w.aws.named("bedrock-agentcore-control", "create_agent_runtime_endpoint")  # nothing made
    c = make_world(tmp_path / "c", container=True)
    image = {"source": "image", "imageUri": f"{REGISTRY}/someone/else:v2"}
    with pytest.raises(ex.ExperimentError, match="pulls from adlc-console/team-agent"):
        rc.start_canary(c.x, {"runtimeId": c.rid, "candidate": image, "acknowledged": True})
    same = {"source": "image", "imageUri": f"{REGISTRY}/adlc-console/team-agent@{digest('v1')}"}
    with pytest.raises(ex.ExperimentError, match="nothing to compare"):
        rc.start_canary(c.x, {"runtimeId": c.rid, "candidate": same, "acknowledged": True})


# -- setup: production does not move ---------------------------------------------------------------------------------------

def test_setup_runs_the_candidate_beside_production_and_production_does_not_move(world):
    w = world
    rec = running(w, treatmentWeight=50, evaluators=["Builtin.Correctness", "Builtin.Helpfulness"])
    hexid, n, aws = rec["id"][4:], rec["names"], w.aws
    ctl = lambda op: aws.named("bedrock-agentcore-control", op)  # noqa: E731
    control = next(e for e in ctl("create_agent_runtime_endpoint") if e["agentRuntimeId"] == w.rid)
    assert (control["name"], control["agentRuntimeVersion"], control["tags"]) == (f"adlc_console_can_{hexid}_c", "1",
                                                                                  {"adlc:console": "1", "adlc:canary": rec["id"]})
    assert prod_writes(w) == []  # no new version, no endpoint moved, nothing written to production's role
    state = rc.production_state(aws.client("bedrock-agentcore-control"), w.rid)
    assert state["version"] == "1" and state["endpoints"]["DEFAULT"] == "1" and state["endpoints"]["live"] == "1"
    [create] = ctl("create_agent_runtime")
    assert create["agentRuntimeName"] == f"{PROD}_c{hexid}" and create["roleArn"] == f"arn:aws:iam::{ACCOUNT}:role/adlc-console-rt-{PROD}"  # production's role
    assert create["lifecycleConfiguration"] == {"idleRuntimeSessionTimeout": 300, "maxLifetime": 3600} and create["networkConfiguration"] == {"networkMode": "PUBLIC"}
    assert create["environmentVariables"] == {"MODEL_ID": "m1", "VARIANT": "v2"}  # production's variables under the candidate's
    assert create["tags"] == {"adlc:console": "1", "adlc:source": "zip", "adlc:canary": rec["id"]}
    job = rec["job"]
    assert create["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"] == {"bucket": BUCKET, "prefix": f"deployments/{PROD}/{job}/code.zip"}
    assert create["agentRuntimeArtifact"]["codeConfiguration"]["entryPoint"] == ["opentelemetry-instrument", "main.py"]
    assert not aws.named("iam", "create_role") or all(r["RoleName"] == n["role"] for r in aws.named("iam", "create_role"))  # no role for the candidate
    smoke = aws.named("bedrock-agentcore", "invoke_agent_runtime")[0]
    assert smoke["agentRuntimeArn"] == rec["copy"]["arn"] and "qualifier" not in smoke  # the smoke call on the candidate's DEFAULT
    treat = next(e for e in ctl("create_agent_runtime_endpoint") if e["agentRuntimeId"] == rec["copy"]["id"])
    assert (treat["name"], treat["agentRuntimeVersion"]) == (f"adlc_console_can_{hexid}_t", "1")
    policy = next(json.loads(p["PolicyDocument"]) for p in aws.named("iam", "put_role_policy") if p["RoleName"] == n["role"])
    arms = next(s for s in policy["Statement"] if s["Sid"] == "InvokeArms")
    assert w.arn in arms["Resource"] and rec["copy"]["arn"] in arms["Resource"] and "bedrock-agentcore:InvokeAgentRuntimeForUser" in arms["Action"]
    [gateway] = ctl("create_gateway")
    assert gateway["authorizerType"] == "AWS_IAM" and "protocolType" not in gateway and gateway["tags"]["adlc:canary"] == rec["id"]
    role_arn = f"arn:aws:iam::{ACCOUNT}:role/adlc-console/{n['role']}"  # the canary's role on the console's path, by IAM's own ARN
    assert [r["Path"] for r in aws.named("iam", "create_role") if r["RoleName"] == n["role"]] == ["/adlc-console/"]
    assert rec["role"]["arn"] == gateway["roleArn"] == role_arn
    c_target, t_target = ctl("create_gateway_target")
    assert c_target["targetConfiguration"]["http"]["agentcoreRuntime"] == {"arn": w.arn, "qualifier": n["control"]}
    assert t_target["targetConfiguration"]["http"]["agentcoreRuntime"] == {"arn": rec["copy"]["arn"], "qualifier": n["treatment"]}
    c_eval, t_eval = ctl("create_online_evaluation_config")
    assert c_eval["dataSourceConfig"]["cloudWatchLogs"] == {"logGroupNames": [f"/aws/bedrock-agentcore/runtimes/{w.rid}-{n['control']}"],
                                                            "serviceNames": [f"{PROD}.{n['control']}"]}
    assert t_eval["dataSourceConfig"]["cloudWatchLogs"]["serviceNames"] == [f"{PROD}_c{hexid}.{n['treatment']}"]
    assert set(rec["logGroups"]) == {f"/aws/bedrock-agentcore/runtimes/{w.rid}-{n['control']}", f"/aws/bedrock-agentcore/runtimes/{rec['copy']['id']}-{n['treatment']}"}
    assert c_eval["evaluationExecutionRoleArn"] == t_eval["evaluationExecutionRoleArn"] == role_arn
    [ab] = aws.named("bedrock-agentcore", "create_ab_test")
    assert ab["roleArn"] == role_arn
    assert ab["variants"] == ex.variants({"C": n["C"], "T1": n["T1"]}, 50) and ab["gatewayFilter"] == {"targetPaths": [f"/{n['C']}/*"]}
    assert ab["evaluationConfig"]["perVariantOnlineEvaluationConfig"][1]["onlineEvaluationConfigArn"] == rec["onlineEvaluations"]["T1"]["arn"]
    assert rec["status"] == "running" and rec["invokeUrl"].endswith(f"/{n['C']}/invocations") and rec["treatment"]["fingerprint"]
    with pytest.raises(ex.ExperimentError, match="already has a canary"):
        rc.start_canary(w.x, {"runtimeId": w.rid, "candidate": candidate(), "acknowledged": True})


def test_a_dockerfile_candidate_is_built_into_productions_own_repository(tmp_path):
    w = make_world(tmp_path, container=True)
    out = rc.start_canary(w.x, {"runtimeId": w.rid, "acknowledged": True,
                                "candidate": {"source": "dockerfile", "archive": b64(zip_of({"main.py": MAIN, "Dockerfile": "FROM python:3.13-slim\n"}))}})
    job = finish(w.console, out["job"]["id"])
    assert job["status"] == "succeeded", job.get("error")
    rec = rc._get(w.x, out["canary"]["id"])
    [start] = w.aws.named("codebuild", "start_build")
    env = {e["name"]: e["value"] for e in start["environmentVariablesOverride"]}
    assert env["IMAGE_URI"] == f"{REGISTRY}/adlc-console/team-agent:{rec['job']}"  # production's repository, which its role pulls from
    assert not w.aws.named("ecr", "create_repository") and rec["copy"]["artifact"]["containerConfiguration"]["containerUri"].startswith(
        f"{REGISTRY}/adlc-console/team-agent@sha256:")
    assert rec["candidate"]["changed"] == ["image (Dockerfile)"] and prod_writes(w) == []


def test_a_failed_setup_keeps_what_it_made_on_the_record_for_cleanup(world, monkeypatch):
    w = world
    monkeypatch.setattr(w.aws, "ctl_create_gateway", lambda **p: (_ for _ in ()).throw(err("ServiceQuotaExceededException", "gateways")))
    out = rc.start_canary(w.x, {"runtimeId": w.rid, "candidate": candidate(), "acknowledged": True})
    assert finish(w.console, out["job"]["id"])["status"] == "failed"
    rec = rc._get(w.x, out["canary"]["id"])
    assert rec["status"] == "failed" and "ServiceQuotaExceeded" in rec["error"] and rec["copy"]["id"] and rec["controlEndpoint"] and rec["role"]
    job = finish(w.console, rc.cleanup_canary(w.x, rec["id"])["id"])
    assert job["status"] == "succeeded" and rec["copy"]["id"] not in w.aws.runtimes and w.rid in w.aws.runtimes
    assert set(w.aws.endpoints[w.rid]) == {"DEFAULT", "live"} and prod_writes(w) == []


# -- the split, traffic, the chat --------------------------------------------------------------------------------------------

def test_the_ramp_pauses_changes_the_weights_and_resumes(world):
    w = world
    rec = running(w)
    calls = len(w.aws.calls)
    out = rc.set_split(w.x, rec["id"], {"treatmentWeight": 25})
    updates = [p for s, o, p in w.aws.calls[calls:] if o == "update_ab_test"]
    assert [p.get("executionStatus") or [v["weight"] for v in p["variants"]] for p in updates] == ["PAUSED", [75, 25], "RUNNING"]
    assert out["weights"] == {"C": 75, "T1": 25} and [s["weights"]["T1"] for s in out["ramp"]] == [5, 25]
    w.aws.results = results(0.8, 20, 0.6, 10)
    with pytest.raises(ex.ExperimentError, match="worse than production beyond the noise band"):
        rc.set_split(w.x, rec["id"], {"treatmentWeight": 50})
    assert rc.set_split(w.x, rec["id"], {"treatmentWeight": 5})["weights"]["T1"] == 5  # down: always
    assert rc.set_split(w.x, rec["id"], {"treatmentWeight": 50, "acknowledged": True})["ramp"][-1]["acknowledged"] is True
    assert rc.set_state(w.x, rec["id"], {"executionStatus": "PAUSED"})["status"] == "paused"
    assert rc.route_for(w.console, "dev", w.rid) is None  # paused: every session goes to production anyway
    assert rc.set_state(w.x, rec["id"], {"executionStatus": "RUNNING"})["status"] == "running"
    assert rc.route_for(w.console, "dev", w.rid)["id"] == rec["id"] and rc.route_for(w.console, "prod", w.rid) is None


def test_traffic_posts_the_runtimes_own_payload_through_the_gateway(world, monkeypatch):
    w = world
    rec = running(w)
    sent = []

    def post(session, region, url, data, headers, **kw):
        sent.append((url, json.loads(data), dict(headers)))
        if "boom" in sent[-1][1]["prompt"]:
            return 502, "application/json", b'{"message": "upstream"}'
        return 200, "application/json", json.dumps({"result": f"answer to {sent[-1][1]['prompt']}"}).encode()

    monkeypatch.setattr(rc, "post_signed", post)
    job = finish(w.console, rc.send_traffic(w.x, rec["id"], {"prompts": ["What is 2+2?", "boom"], "repeat": 2})["id"])
    assert job["status"] == "succeeded" and job["kind"] == "runtime-canary-traffic" and len(sent) == 4
    url, body, headers = sent[0]
    assert url == rec["invokeUrl"] and set(body) == {"prompt", "actorId"} and body["actorId"].startswith(f"can-{rec['id'][4:]}-")
    assert len({h[rc.SESSION_HEADER] for _, _, h in sent}) == 4 and all(len(h[rc.SESSION_HEADER]) >= 33 for _, _, h in sent)
    assert all(h[rc.USER_HEADER] and h["Content-Type"] == "application/json" for _, _, h in sent)
    result = job["result"]
    assert (result["sent"], result["failed"], result["statuses"]) == (2, 2, {"200": 2, "502": 2})
    assert {s["answer"] for s in result["samples"] if not s["error"]} == {"answer to What is 2+2?"}
    with pytest.raises(ex.ExperimentError):
        rc.send_traffic(w.x, rec["id"], {"prompts": ["x"] * 201})


def test_a_post_that_never_left_is_sent_again_and_one_that_did_is_not(monkeypatch):
    class ProxyConnectionError(Exception):
        pass

    class ReadTimeoutError(Exception):
        pass

    attempts = []

    def flaky(session, region, url, data, headers, **kw):
        attempts.append(url)
        if len(attempts) < 3:
            raise ProxyConnectionError('Failed to connect to proxy URL: "http://127.0.0.1:7890"')  # live: a local proxy under load
        return 200, "application/json", b"{}"

    monkeypatch.setattr(ex, "post_signed", flaky)
    assert rc.post_signed(None, REGION, "https://gw/t/invocations", b"{}", {}) == (200, "application/json", b"{}") and len(attempts) == 3
    attempts.clear()
    monkeypatch.setattr(ex, "post_signed", lambda *a, **kw: (attempts.append(1), (_ for _ in ()).throw(ReadTimeoutError("read timed out")))[1])
    with pytest.raises(ReadTimeoutError):
        rc.post_signed(None, REGION, "https://gw/t/invocations", b"{}", {})
    assert len(attempts) == 1  # it may have reached the agent: never a second turn


def test_a_chat_turn_goes_through_the_canary_and_production_answers_only_if_the_request_never_left(world, monkeypatch):
    w = world
    rec = running(w)
    sent = []
    monkeypatch.setattr(rc, "post_signed", lambda session, region, url, data, headers, **kw: (sent.append((url, json.loads(data), dict(headers))),
                                                                                              (200, "application/json", b'{"result": "[v2] Four."}'))[1])
    events = list(rc.invoke_through(w.session, REGION, rec, message="2+2?", session_id=None, actor="ann"))
    assert [e["type"] for e in events] == ["session", "experiment", "text", "stop"]
    assert events[1] == {"type": "experiment", "kind": "runtime-canary", "id": rec["id"], "name": rec["name"], "weights": rec["weights"]}
    assert events[2]["text"] == '{"result": "[v2] Four."}'  # the runtime's answer as it came, as agents.invoke gives it
    assert sent[0][1] == {"prompt": "2+2?", "actorId": "ann"} and sent[0][2][rc.SESSION_HEADER] == events[0]["sessionId"]
    before = len(w.aws.named("bedrock-agentcore", "invoke_agent_runtime"))

    class ProxyConnectionError(Exception):  # botocore's, by name: the proxy refused the connection, nothing reached AWS
        pass

    monkeypatch.setattr(rc, "post_signed", lambda *a, **kw: (_ for _ in ()).throw(ProxyConnectionError("Failed to connect to proxy URL")))
    events = list(rc.invoke_through(w.session, REGION, rec, message="2+2?", session_id="s" * 40, actor="ann"))
    assert [e["type"] for e in events][:3] == ["session", "experiment", "fallback"] and "ProxyConnectionError" in events[2]["error"]
    direct = w.aws.named("bedrock-agentcore", "invoke_agent_runtime")[before:]
    assert [d["agentRuntimeArn"] for d in direct] == [w.arn] and "qualifier" not in direct[0]  # production's DEFAULT, which did not move
    before = len(w.aws.named("bedrock-agentcore", "invoke_agent_runtime"))
    for failure in (lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("read timed out")),  # it may have reached an arm: never a second run
                    lambda *a, **kw: (502, "application/json", b'{"message": "upstream"}')):
        monkeypatch.setattr(rc, "post_signed", failure)
        events = list(rc.invoke_through(w.session, REGION, rec, message="2+2?", session_id="s" * 40, actor="ann"))
        assert [e["type"] for e in events] == ["session", "experiment", "error"], events
        assert "not sent again" in events[2]["error"] or "answered 502" in events[2]["error"]
    assert len(w.aws.named("bedrock-agentcore", "invoke_agent_runtime")) == before  # production was not asked the turn again


# -- the gate, promotion, rollback ---------------------------------------------------------------------------------------------

def test_promotion_is_refused_without_evidence_and_publishes_the_measured_candidate_when_it_holds(world):
    w = world
    rec = running(w)
    with pytest.raises(ex.ExperimentError) as refused:
        rc.promote(w.x, rec["id"], {})
    gate = refused.value.extra["gate"]
    assert refused.value.status == 409 and [(c["id"], c["ok"]) for c in gate["conditions"]] == [("verification", False), ("ab", False), ("agent", True)]
    assert "verify the candidate runtime" in gate["conditions"][0]["evidence"]
    verified(w, rec, bands={"Builtin.Correctness": 0.1})
    w.aws.results = results(0.60, 12, 0.66, 10)
    detail = rc.canary_detail(w.x, rec["id"])
    assert detail["gate"]["ok"] and detail["metrics"][0]["band"] == 0.1 and detail["production"]["version"] == "1"
    member = ex.Ctx(w.console, "dev", w.session, REGION, ACCOUNT, MEMBER)
    with pytest.raises(ex.ExperimentError) as refused:
        rc.promote(member, rec["id"], {})
    assert refused.value.status == 403
    out = rc.promote(w.x, rec["id"], {})
    assert out["gate"]["ok"] and out["canary"]["status"] == "promoting"
    job = finish(w.console, out["job"]["id"])
    assert job["status"] == "succeeded", (job.get("error"), job["log"][-5:])
    [update] = w.aws.named("bedrock-agentcore-control", "update_agent_runtime")
    copy = w.aws.snapshots[rec["copy"]["id"]]["1"]
    assert update["agentRuntimeId"] == w.rid and update["agentRuntimeArtifact"] == copy["agentRuntimeArtifact"]  # the very artifact measured
    assert update["environmentVariables"] == {"MODEL_ID": "m1", "VARIANT": "v2"} and update["roleArn"] == f"arn:aws:iam::{ACCOUNT}:role/adlc-console-rt-{PROD}"
    assert update["lifecycleConfiguration"] == {"idleRuntimeSessionTimeout": 300, "maxLifetime": 3600}  # carried over by the deploy module
    assert not [p for p in w.aws.named("iam", "put_role_policy") if p["RoleName"] == f"adlc-console-rt-{PROD}"]  # production's role untouched
    assert not [p for p in w.aws.named("s3", "put_object") if p["Key"].startswith(f"deployments/{PROD}/{out['job']['id']}")]  # nothing uploaded
    moved = [p for p in w.aws.named("bedrock-agentcore-control", "update_agent_runtime_endpoint") if p["agentRuntimeId"] == w.rid]
    assert moved == [{"agentRuntimeId": w.rid, "endpointName": "live", "agentRuntimeVersion": "2"}]
    after = rc._get(w.x, rec["id"])
    p = after["promotion"]
    assert after["status"] == "promoted" and (p["fromVersion"], p["toVersion"], p["endpoint"], p["override"]) == ("1", "2", "live", False)
    assert w.aws.tests[rec["abTest"]["id"]]["executionStatus"] == "STOPPED" and after["finalResults"]["metrics"][0]["treatment"]["mean"] == 0.66
    [release] = [r for r in w.console.store.read("promotions", []) if r["experiment"] == rec["id"]]
    assert release["kind"] == "runtime-canary" and release["toVersion"] == "2" and release["changed"] == ["code", "env:VARIANT"]
    assert [h["version"] for h in w.console.store.read("deployments", {})[w.rid]["history"]] == ["1", "2"]  # the deploy page's history
    with pytest.raises(ex.ExperimentError):
        rc.promote(w.x, rec["id"], {})  # once


def test_an_admin_override_promotes_against_the_gate_with_its_reason_recorded(world):
    w = world
    rec = running(w)
    with pytest.raises(ex.ExperimentError, match="needs a reason"):
        rc.promote(w.x, rec["id"], {"acknowledged": True})
    out = rc.promote(w.x, rec["id"], {"acknowledged": True, "reason": "the demo needs the new tool today"})
    assert finish(w.console, out["job"]["id"])["status"] == "succeeded"
    p = rc._get(w.x, rec["id"])["promotion"]
    assert p["override"] is True and p["failed"] == ["verification", "ab"] and p["reason"].startswith("the demo") and p["by"] == "ann"
    assert p["gate"]["ok"] is False


def test_the_gate_refuses_a_candidate_worse_beyond_the_band_and_a_production_that_moved(world):
    w = world
    rec = running(w)
    verified(w, rec, bands={"Builtin.Correctness": 0.05})
    w.aws.results = results(0.80, 12, 0.70, 10)
    with pytest.raises(ex.ExperimentError, match="worse beyond the noise band"):
        rc.promote(w.x, rec["id"], {})
    w.aws.results = results(0.70, 12, 0.80, 12)
    w.aws.runtimes[w.rid]["agentRuntimeVersion"] = "7"  # someone deployed production during the canary
    with pytest.raises(ex.ExperimentError, match="moved from version 1 to 7"):
        rc.promote(w.x, rec["id"], {})
    assert not w.aws.named("bedrock-agentcore-control", "update_agent_runtime")


def test_a_rollback_before_promotion_stops_the_split_and_production_was_never_moved(world):
    w = world
    rec = running(w)
    w.aws.results = results(0.8, 6, 0.3, 5)
    member = ex.Ctx(w.console, "dev", w.session, REGION, ACCOUNT, MEMBER)
    with pytest.raises(ex.ExperimentError) as refused:
        rc.rollback(member, rec["id"], {})
    assert refused.value.status == 403
    out = rc.rollback(w.x, rec["id"], {"reason": "worse answers"})["canary"]
    rb = out["rollback"]
    assert out["status"] == "rolled_back" and rb["moved"] is False and rb["production"]["version"] == "1" and rb["production"]["endpoints"]["live"] == "1"
    assert "nothing to move back" in rb["evidence"] and w.aws.tests[rec["abTest"]["id"]]["executionStatus"] == "STOPPED"
    assert out["finalResults"]["metrics"][0]["treatment"]["mean"] == 0.3 and prod_writes(w) == []
    assert rc.route_for(w.console, "dev", w.rid) is None
    with pytest.raises(ex.ExperimentError, match="nothing to promote"):
        rc.promote(w.x, rec["id"], {"acknowledged": True, "reason": "too late"})


def test_a_rollback_after_promotion_moves_the_endpoint_back_and_publishes_the_old_version_again(world):
    w = world
    rec = running(w)
    finish(w.console, rc.promote(w.x, rec["id"], {"acknowledged": True, "reason": "ship it for the test"})["job"]["id"])
    calls = len(w.aws.calls)
    out = rc.rollback(w.x, rec["id"], {"reason": "customers complain"})
    assert out["canary"]["status"] == "rolling_back"
    job = finish(w.console, out["job"]["id"])
    assert job["status"] == "succeeded", (job.get("error"), job["log"][-5:])
    later = w.aws.calls[calls:]
    pointed = [p for s, o, p in later if o == "update_agent_runtime_endpoint"]
    assert pointed == [{"agentRuntimeId": w.rid, "endpointName": "live", "agentRuntimeVersion": "1"}]  # the endpoint back at once
    [again] = [p for s, o, p in later if o == "update_agent_runtime"]
    old = w.aws.snapshots[w.rid]["1"]
    assert again["agentRuntimeArtifact"] == old["agentRuntimeArtifact"] and again["environmentVariables"] == {"MODEL_ID": "m1"}  # version 1, again
    rb = rc._get(w.x, rec["id"])["rollback"]
    assert (rb["endpoint"], rb["endpointVersion"], rb["defaultVersion"], rb["restoredFrom"]) == ("live", "1", "3", "1")
    assert rb["production"]["endpoints"] == {"DEFAULT": "3", "live": "1", rec["names"]["control"]: "1"}
    assert w.aws.endpoints[w.rid]["live"]["liveVersion"] == "1"  # not moved again to version 3
    kinds = [r["kind"] for r in w.console.store.read("promotions", []) if r["experiment"] == rec["id"]]
    assert kinds == ["runtime-canary", "runtime-canary-rollback"]


# -- verification ---------------------------------------------------------------------------------------------------------

def test_the_candidate_is_verified_on_its_own_default_and_the_gate_finds_it(world, monkeypatch):
    w = world
    rec = running(w, evaluators=["Builtin.Correctness", "Builtin.Refusal"], metric="Builtin.Correctness")
    from workshop_customizer.console import evaluation as ev

    ev.put_contract_set(w.console.store, "dev", {"id": "cs-math", "name": "math", "contracts": [
        {"id": "sum", "query": "What is 2+2?", "expected": {"mustMention": ["4"]}}]})
    seen = {}

    class FakeVerify:
        def __init__(self, session, info, contracts, l1cfg, **kw):
            seen.update(kw, info=info, contracts=contracts)

        def run(self):
            return {"robust": True, "holding": 1, "contracts": [{"id": "sum", "passes": 2, "rounds": 2}], "repeat": 2, "rounds": [],
                    "harness": {"name": seen["info"]["name"]}, "panel": {"bands": {"Builtin.Correctness": 0.04}}}

    monkeypatch.setattr(rc, "RuntimeVerify", FakeVerify)
    monkeypatch.setattr(rc.verify, "render_markdown", lambda doc: "# verification")
    job = finish(w.console, rc.verify_candidate(w.x, rec["id"], {"contractSet": "cs-math", "repeat": 2})["id"])
    assert job["kind"] == "verify" and job["params"]["agentId"] == w.rid and job["params"]["treatment"]["fingerprint"] == rec["treatment"]["fingerprint"]
    assert seen["info"]["arn"] == rec["copy"]["arn"] and seen["info"]["runtimeId"] == rec["copy"]["id"]  # the candidate runtime, never production
    assert seen["panel"] == ("Builtin.Correctness",) and seen["repeat"] == 2  # the canary's quality evaluators measure their bands
    found, running_ = ex.find_verification(w.x, rc._get(w.x, rec["id"]))
    assert found["id"] == job["id"] and found["robust"] and found["bands"] == {"Builtin.Correctness": 0.04} and not running_
    assert rc.canary_detail(w.x, rec["id"])["gate"]["conditions"][0]["ok"]


def test_ask_runtime_sends_the_console_payload_and_reads_the_answer():
    class Data:
        def __init__(self):
            self.calls = []

        def invoke_agent_runtime(self, **kw):
            self.calls.append(kw)
            if "fail" in json.loads(kw["payload"])["prompt"]:
                raise RuntimeError("ThrottlingException")
            return {"response": io.BytesIO(b'{"result": "4", "variant": "v2"}'), "contentType": "application/json", "statusCode": 200}

    from workshop_customizer.direct.run import Asked

    data = Data()
    rows = [Asked(index=i, case_id=f"c{i}", query=q, session_id=f"direct-{'a' * 32}-{i}", actor=f"a{i}", started_ms=0, probe=False)
            for i, q in enumerate(["2+2?", "fail please"], 1)]
    out = rc.ask_runtime(data, "arn:rt", rows, workers=2, log=lambda line: None)
    assert out[0].text == "4" and out[0].error is None and "ThrottlingException" in out[1].error
    assert json.loads(data.calls[0]["payload"]) == {"prompt": "2+2?", "actorId": "a1"} and "qualifier" not in data.calls[0]
    assert all("stop_runtime_session" not in str(c) for c in data.calls)


# -- cleanup -------------------------------------------------------------------------------------------------------------------

def test_cleanup_deletes_what_the_canary_made_and_nothing_of_production(world):
    w = world
    rec = running(w)
    w.aws.results = results(0.70, 12, 0.80, 12)  # kept with the record when the A/B test is stopped
    w.aws.log_groups |= {f"/aws/bedrock-agentcore/runtimes/{w.rid}-DEFAULT", f"/aws/bedrock-agentcore/runtimes/{w.rid}-live",
                         f"/aws/bedrock-agentcore/runtimes/{rec['copy']['id']}-DEFAULT"}
    w.aws.log_groups |= {f"/aws/bedrock-agentcore/evaluations/results/{e['id']}" for e in rec["onlineEvaluations"].values()}
    member = ex.Ctx(w.console, "dev", w.session, REGION, ACCOUNT, MEMBER)
    with pytest.raises(ex.ExperimentError):
        rc.cleanup_canary(member, rec["id"])
    job = finish(w.console, rc.cleanup_canary(w.x, rec["id"])["id"])
    assert job["status"] == "succeeded"
    assert all(r["result"] in ("deleted",) or r["result"].endswith("deleted") for r in job["result"]["cleanup"]), job["result"]["cleanup"]
    aws = w.aws
    assert not aws.tests and not aws.evals and not aws.targets and not aws.gateways and rec["copy"]["id"] not in aws.runtimes
    assert w.rid in aws.runtimes and set(aws.endpoints[w.rid]) == {"DEFAULT", "live"} and f"adlc-console-rt-{PROD}" in aws.roles
    assert rec["names"]["role"] not in aws.roles and CODE_KEY in aws.objects  # production's own sources stay
    assert not [k for k in aws.objects if k.startswith(f"deployments/{PROD}/{rec['job']}/")]  # the candidate's go: it was never promoted
    assert aws.log_groups == {f"/aws/bedrock-agentcore/runtimes/{w.rid}-DEFAULT", f"/aws/bedrock-agentcore/runtimes/{w.rid}-live"}
    after = rc._get(w.x, rec["id"])
    assert after["status"] == "cleaned" and after["cleanedAt"] and prod_writes(w) == []
    shown = rc.canary_detail(w.x, rec["id"])  # the evidence stays readable: the final results with their bands
    assert shown["live"]["final"] and shown["metrics"][0]["band"] == ex.MIN_BAND and shown["production"]["version"] == "1"
    with pytest.raises(ex.ExperimentError, match="already cleaned"):
        rc.cleanup_canary(w.x, rec["id"])


def test_cleanup_keeps_a_promoted_artifact_and_leaves_what_is_not_this_canarys(world):
    w = world
    rec = running(w)
    finish(w.console, rc.promote(w.x, rec["id"], {"acknowledged": True, "reason": "keep the artifact"})["job"]["id"])
    w.aws.tags[rec["copy"]["arn"]] = {"adlc:console": "1", "adlc:canary": "can-00000000"}  # someone else's candidate now
    job = finish(w.console, rc.cleanup_canary(w.x, rec["id"])["id"])
    left = next(r for r in job["result"]["cleanup"] if r["resource"].startswith("candidate runtime"))
    assert left["result"].startswith("left:") and rec["copy"]["id"] in w.aws.runtimes
    assert [k for k in w.aws.objects if k.startswith(f"deployments/{PROD}/{rec['job']}/")]  # production runs this artifact now
    kept = next(r for r in job["result"]["cleanup"] if r["resource"].startswith("candidate artifact"))
    assert kept["result"].startswith("kept: production's version 2 runs it")
    after = rc._get(w.x, rec["id"])
    assert after["status"] == "promoted" and after["cleanedAt"]
    out = rc.rollback(w.x, rec["id"], {})  # a promotion can still be undone once the canary is cleaned up
    assert finish(w.console, out["job"]["id"])["status"] == "succeeded"


def test_the_split_as_aws_spans_saw_it(world):
    w = world
    rec = running(w)
    n = rec["names"]
    w.aws.query_results = [[{"field": "service", "value": f"{PROD}.{n['control']}"}, {"field": "sessions", "value": "7"}],
                           [{"field": "service", "value": f"{PROD}_c{rec['id'][4:]}.{n['treatment']}"}, {"field": "sessions", "value": "5"}]]
    out = rc.observed_split(w.x, rec["id"])
    assert out["arms"]["C"]["sessions"] == 7 and out["arms"]["T1"]["sessions"] == 5 and out["share"] == {"C": 0.583, "T1": 0.417}
    assert f'"{PROD}.{n["control"]}"' in w.aws.queries[-1] and "count_distinct" in w.aws.queries[-1]


# -- routes, the console chat and /v1 ----------------------------------------------------------------------------------------

def test_the_routes_live_under_experiments_and_the_production_changing_ones_are_admin_only():
    added = []
    rc.register(SimpleNamespace(add=lambda method, path, fn, admin=False: added.append((method, path, admin))))
    base = "/workspaces/{wid}/experiments/runtime-canaries"
    assert all(p.startswith(base) for _, p, _ in added)
    assert sorted((m, p[len(base):]) for m, p, admin in added if admin) == [("DELETE", "/{cid}"), ("POST", ""), ("POST", "/{cid}/promote"),
                                                                            ("POST", "/{cid}/rollback"), ("POST", "/{cid}/split")]


@pytest.fixture()
def server(tmp_path):
    aws = CanaryAWS()
    prod = aws.seed_production()
    c = console_mod.Console(tmp_path / "data", session_factory=lambda **kw: FakeSession(aws), clients_factory=lambda cfg: None)
    srv = console_mod.create_server(c)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import urllib.request

    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(method, path, body=None, headers=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if "json" in resp.headers.get("Content-Type", "") else raw.decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    assert call("POST", "/api/console/workspaces", {"id": "dev", "accountId": ACCOUNT, "region": REGION, "profile": "default"})[0] == 201
    yield SimpleNamespace(call=call, aws=aws, console=c, rid=prod["agentRuntimeId"], arn=prod["agentRuntimeArn"])
    srv.shutdown()
    srv.server_close()


def test_the_console_chat_and_v1_reach_a_runtime_in_a_running_canary_through_its_gateway(server, monkeypatch):
    s = server
    rec = {"id": "can-0a1b2c3d", "workspace": "dev", "name": "team_agent 金丝雀", "status": "running", "weights": {"C": 50, "T1": 50},
           "agent": {"id": s.rid, "name": PROD, "arn": s.arn}, "invokeUrl": "https://gw.example/adlc-console-can-0a1b2c3d-c/invocations"}
    s.console.store.write(rc.COLLECTION, {rec["id"]: rec, "can-ffffffff": {**rec, "id": "can-ffffffff", "workspace": "prod"}})
    sent = []
    monkeypatch.setattr(rc, "post_signed", lambda session, region, url, data, headers, **kw: (sent.append((url, json.loads(data), headers)),
                                                                                              (200, "application/json", b'{"result": "[v2] 15 days."}'))[1])
    status, text = s.call("POST", f"/api/console/workspaces/dev/agents/runtime/{s.rid}/chat", {"message": "Leave?"})
    events = [json.loads(line[6:]) for line in text.split("\n") if line.startswith("data: ")]
    assert status == 200 and [e["type"] for e in events] == ["session", "experiment", "text", "stop"]
    assert events[1]["kind"] == "runtime-canary" and events[1]["weights"] == {"C": 50, "T1": 50} and sent[0][0] == rec["invokeUrl"]
    assert sent[0][1] == {"prompt": "Leave?", "actorId": "local"} and sent[0][2][rc.SESSION_HEADER] == events[0]["sessionId"]
    status, text = s.call("POST", f"/api/console/workspaces/dev/agents/runtime/{s.rid}/chat", {"message": "Leave?", "bypassExperiment": True})
    assert "experiment" not in text and len(sent) == 1  # production directly, on request
    key = s.call("POST", "/api/console/keys", {"workspace": "dev", "agent": PROD, "label": "crm"})[1]
    status, answer = s.call("POST", "/v1/chat", {"agent": PROD, "message": "Leave?"}, headers={"X-Api-Key": key["key"]})
    assert status == 200 and answer["experiment"] == rec["id"] and answer["text"] == '{"result": "[v2] 15 days."}' and len(sent) == 2
    s.console.store.write(rc.COLLECTION, {rec["id"]: {**rec, "status": "rolled_back"}})
    status, answer = s.call("POST", "/v1/chat", {"agent": PROD, "message": "Leave?"}, headers={"X-Api-Key": key["key"]})
    assert status == 200 and "experiment" not in answer and len(sent) == 2  # after the canary: production directly
    status, listed = s.call("GET", "/api/console/workspaces/dev/experiments/runtime-canaries")
    assert status == 200 and [c["id"] for c in listed["canaries"]] == [rec["id"]]
    assert s.call("GET", "/api/console/workspaces/dev/experiments/runtime-canaries/can-12345678")[0] == 404


# -- the review's findings, each pinned ------------------------------------------------------------------------------------

def test_an_image_candidate_is_compared_by_digest_and_cleanup_keeps_an_image_the_canary_did_not_build(tmp_path):
    """review H1: production's own image by its tag, with other variables, is a candidate (the same variables: nothing to
    compare); its cleanup never deletes that image, which production runs."""
    w = make_world(tmp_path, container=True)
    repo = w.aws.repos["adlc-console/team-agent"]
    same = {"source": "image", "imageUri": f"{REGISTRY}/adlc-console/team-agent:v1"}  # a tag: a string unlike production's @digest
    with pytest.raises(ex.ExperimentError, match="nothing to compare"):
        rc.start_canary(w.x, {"runtimeId": w.rid, "candidate": same, "acknowledged": True})
    out = rc.start_canary(w.x, {"runtimeId": w.rid, "candidate": {**same, "environment": {"STYLE": "verbose"}}, "acknowledged": True})
    assert finish(w.console, out["job"]["id"])["status"] == "succeeded"
    rec = rc._get(w.x, out["canary"]["id"])
    assert rec["candidate"]["changed"] == ["env:STYLE"] and rec["candidate"]["digest"] == digest("v1") and rec["candidate"]["built"] is False
    rc.rollback(w.x, rec["id"], {"reason": "not better"})
    job = finish(w.console, rc.cleanup_canary(w.x, rec["id"])["id"])
    kept = next(r for r in job["result"]["cleanup"] if r["resource"].startswith("candidate image"))
    assert kept["result"].startswith("left: an image the canary did not build") and not kept.get("failed")
    assert repo["images"] == {"v1": digest("v1")} and not w.aws.named("ecr", "batch_delete_image")  # production's live image stays
    assert rc._get(w.x, rec["id"])["cleanedAt"]  # left by design: the cleanup is done


def _dockerfile_canary(w) -> dict:
    out = rc.start_canary(w.x, {"runtimeId": w.rid, "acknowledged": True,
                                "candidate": {"source": "dockerfile", "archive": b64(zip_of({"main.py": MAIN, "Dockerfile": "FROM python:3.13-slim\n"}))}})
    assert finish(w.console, out["job"]["id"])["status"] == "succeeded"
    return rc._get(w.x, out["canary"]["id"])


def test_cleanup_deletes_the_image_the_canarys_build_pushed_unless_a_version_of_production_runs_it(tmp_path):
    """review H1: the image a Dockerfile candidate's build pushed goes with the canary, never a digest any version of
    production runs (a rollback can return to it)."""
    w = make_world(tmp_path / "a", container=True)
    rec = _dockerfile_canary(w)
    pushed = rec["copy"]["artifact"]["containerConfiguration"]["containerUri"].split("@", 1)[1]
    assert rec["candidate"]["built"] is True and pushed in w.aws.repos["adlc-console/team-agent"]["images"].values()
    rc.rollback(w.x, rec["id"], {})
    finish(w.console, rc.cleanup_canary(w.x, rec["id"])["id"])
    assert w.aws.named("ecr", "batch_delete_image") == [{"repositoryName": "adlc-console/team-agent", "imageIds": [{"imageDigest": pushed}]}]
    assert w.aws.repos["adlc-console/team-agent"]["images"] == {"v1": digest("v1")}
    w = make_world(tmp_path / "b", container=True)
    rec = _dockerfile_canary(w)
    pushed = rec["copy"]["artifact"]["containerConfiguration"]["containerUri"].split("@", 1)[1]
    w.aws.snapshots[w.rid]["2"] = {**w.aws.snapshots[w.rid]["1"], "agentRuntimeVersion": "2",
                                   "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": f"{REGISTRY}/adlc-console/team-agent@{pushed}"}}}
    w.aws.runtimes[w.rid]["versions"].append("2")  # a version of production runs that digest
    rc.rollback(w.x, rec["id"], {})
    job = finish(w.console, rc.cleanup_canary(w.x, rec["id"])["id"])
    kept = next(r for r in job["result"]["cleanup"] if r["resource"].startswith("candidate image"))
    assert kept["result"].startswith("left: a version of production runs it") and not w.aws.named("ecr", "batch_delete_image")


def test_what_a_canary_compares_cannot_be_moved_from_the_deploy_page_and_the_gate_checks_every_arm(world):
    """review H4: the deploy page refuses to move or delete the canary's endpoints, republish or delete its candidate,
    or delete production while the canary runs; the gate's (c) checks the control endpoint and the candidate too, and a
    verification of another version of the candidate is not credited."""
    w = world
    rec = running(w)
    n, copy = rec["names"], rec["copy"]
    for change in (lambda: dp.point_endpoint(w.console, "dev", w.rid, {"name": n["control"], "version": "1"}),
                   lambda: dp.delete_endpoint(w.console, "dev", w.rid, n["control"]),
                   lambda: dp.delete_endpoint(w.console, "dev", copy["id"], n["treatment"]),
                   lambda: dp.start_deploy(w.console, "dev", {"source": "zip", "archive": b64(zip_of({"main.py": MAIN}))}, runtime_id=copy["id"]),
                   lambda: dp.start_delete(w.console, "dev", copy["id"]), lambda: dp.start_delete(w.console, "dev", w.rid)):
        with pytest.raises(dp.DeployError, match=f"runtime canary {rec['id']}"):
            change()
    assert dp.point_endpoint(w.console, "dev", w.rid, {"name": "live", "version": "1"})["action"] == "moved"  # production's own endpoint: as ever
    verified(w, rec, bands={"Builtin.Correctness": 0.1})
    w.aws.results = results(0.60, 12, 0.66, 10)
    assert rc.canary_detail(w.x, rec["id"])["gate"]["ok"]
    w.aws.endpoints[w.rid][n["control"]].update(liveVersion="7", targetVersion="7")  # moved outside the console: DEFAULT is still version 1
    agent = rc.canary_detail(w.x, rec["id"])["gate"]["conditions"][2]
    assert not agent["ok"] and agent["arm"]["now"] == "7" and "control endpoint" in agent["evidence"]
    with pytest.raises(ex.ExperimentError, match="control endpoint"):
        rc.promote(w.x, rec["id"], {})
    w.aws.endpoints[w.rid][n["control"]].update(liveVersion="1", targetVersion="1")
    w.aws.runtimes[copy["id"]]["agentRuntimeVersion"] = "2"  # a new version of the candidate, outside the console
    assert "the candidate" in rc.canary_detail(w.x, rec["id"])["gate"]["conditions"][2]["evidence"]
    from workshop_customizer.console import evaluation as ev

    ev.put_contract_set(w.console.store, "dev", {"id": "cs-math", "name": "math", "contracts": [{"id": "sum", "query": "2+2?", "expected": {"mustMention": ["4"]}}]})
    with pytest.raises(ex.ExperimentError, match="is version 2 now"):
        rc.verify_candidate(w.x, rec["id"], {"contractSet": "cs-math"})
    later = {"agent": PROD, "agentId": w.rid, "contractSet": "cs-1", "repeat": 2, "panel": True,
             "treatment": {"fingerprint": rec["treatment"]["fingerprint"], "agentVersion": "1", "canary": rec["id"], "candidateVersion": "2"}}
    finish(w.console, w.console.jobs.start("verify", "dev", later, lambda job: {"robust": True, "holding": 3, "contracts": [{}] * 3, "repeat": 2})["id"])
    found, _ = ex.find_verification(w.x, rc._get(w.x, rec["id"]))
    assert found["treatmentVersion"] == "1"  # the newest is about version 2: the one credited is the version-1 verification


def _cut_off(w, kind: str, rec: dict, field: str, status: str, **fields) -> str:
    """A job that held the canary when the console stopped: its file says running; a new Jobs() marks it interrupted."""
    jid = f"{kind}-{'0' * 9}1"
    w.console.jobs._save({"id": jid, "kind": kind, "workspace": "dev", "label": kind, "params": {"canary": rec["id"]}, "status": "running",
                          "createdAt": "2026-10-01T00:00:00Z", "finishedAt": None, "log": [], "progress": {}, "result": None, "error": None})
    rc._save(w.x, rec["id"], status=status, **{field: jid}, **fields)
    return jid


def test_a_canary_whose_job_the_console_restart_cut_off_is_released(tmp_path, monkeypatch):
    """review M3: after a restart the record no longer stays creating / promoting / rolling_back / cleaning: it is
    released so cleanup, rollback and a new canary are possible, and nothing is leaked."""
    w = make_world(tmp_path)
    hang = threading.Event()
    real = w.aws.ctl_create_gateway
    monkeypatch.setattr(w.aws, "ctl_create_gateway", lambda **p: (hang.wait(10), real(**p))[1])
    out = rc.start_canary(w.x, {"runtimeId": w.rid, "candidate": candidate(), "acknowledged": True})
    cid = out["canary"]["id"]
    deadline = time.monotonic() + 10
    while not (rc._get(w.x, cid).get("role") or {}).get("arn"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    w.console.jobs = Jobs(tmp_path / "console" / "jobs")  # the console restarts: the running setup job is interrupted
    rec = rc._get(w.x, cid)
    assert rec["status"] == "failed" and "restarted" in rec["error"]
    job = finish(w.console, rc.cleanup_canary(w.x, cid)["id"])
    assert job["status"] == "succeeded" and set(w.aws.endpoints[w.rid]) == {"DEFAULT", "live"}  # the control endpoint on production went
    assert not w.aws.evals and rec["copy"]["id"] not in w.aws.runtimes
    hang.set()

    w = make_world(tmp_path / "promoting")
    rec = running(w)
    decision = {"at": "2026-10-01T00:00:00Z", "by": "ann", "metric": rec["metric"], "gate": {"ok": False, "conditions": []}, "override": True,
                "failed": ["verification", "ab"], "reason": "ship it"}
    _cut_off(w, "runtime-canary-promote", rec, "promoteJob", "promoting", decision=decision)
    candidate_rt = w.aws.runtimes[rec["copy"]["id"]]
    w.aws.ctl_update_agent_runtime(w.rid, agentRuntimeArtifact=candidate_rt["agentRuntimeArtifact"],  # the promotion's UpdateAgentRuntime ran
                                   environmentVariables=candidate_rt["environmentVariables"])
    w.console.jobs = Jobs(tmp_path / "promoting" / "console" / "jobs")
    after = rc._get(w.x, rec["id"])
    assert after["status"] == "promoted" and "restarted" in after["promotion"]["incomplete"] and after["promotion"]["toVersion"] == "2"
    [release] = [r for r in w.console.store.read("promotions", []) if r["experiment"] == rec["id"]]
    assert release["override"] is True and release["incomplete"]  # the release log shows it
    _cut_off(w, "runtime-canary-rollback", after, "rollbackJob", "rolling_back")
    w.console.jobs = Jobs(tmp_path / "promoting" / "console" / "jobs")
    assert rc._get(w.x, rec["id"])["status"] == "promoted"  # roll back again
    _cut_off(w, "runtime-canary-cleanup", after, "cleanupJob", "cleaning", cleanupFrom="promoted")
    w.console.jobs = Jobs(tmp_path / "promoting" / "console" / "jobs")
    assert rc._get(w.x, rec["id"])["status"] == "promoted" and not rc._get(w.x, rec["id"]).get("cleanedAt")
    assert finish(w.console, rc.cleanup_canary(w.x, rec["id"])["id"])["status"] == "succeeded"

    w = make_world(tmp_path / "stopped")
    rec = running(w)
    _cut_off(w, "runtime-canary-promote", rec, "promoteJob", "promoting", decision=decision)
    w.console.jobs = Jobs(tmp_path / "stopped" / "console" / "jobs")
    assert rc._get(w.x, rec["id"])["status"] == "stopped"  # production did not move: nothing was promoted
    assert [c["id"] for c in rc.list_canaries(w.x)] == [rec["id"]]


def test_promotion_decides_on_the_canarys_own_metric_and_another_one_is_an_override(world):
    """review M5: a promotion's body.metric does not replace the declared metric: asking for another is an override."""
    w = world
    rec = running(w, evaluators=["Builtin.Correctness", "Builtin.Helpfulness"], metric="Builtin.Correctness")
    verified(w, rec)
    w.aws.results = results(0.92, 20, 0.55, 20, "Builtin.Correctness")
    w.aws.results["evaluatorMetrics"] += results(0.80, 20, 0.81, 20, "Builtin.Helpfulness")["evaluatorMetrics"]
    with pytest.raises(ex.ExperimentError, match="metric: the experiment decides on Builtin.Correctness") as refused:
        rc.promote(w.x, rec["id"], {"metric": "Builtin.Helpfulness"})
    assert [c["id"] for c in refused.value.extra["gate"]["conditions"] if not c["ok"]] == ["ab", "metric"]
    out = rc.promote(w.x, rec["id"], {"metric": "Builtin.Helpfulness", "acknowledged": True, "reason": "helpfulness is what this agent is for"})
    assert finish(w.console, out["job"]["id"])["status"] == "succeeded"
    p = rc._get(w.x, rec["id"])["promotion"]
    assert (p["metric"], p["requestedMetric"], p["override"], p["failed"]) == ("Builtin.Correctness", "Builtin.Helpfulness", True, ["ab", "metric"])


def test_the_ramp_is_an_admins_and_goes_above_half_only_once_the_gate_holds(world):
    """review M6: a member cannot move the split, and no one moves it past 50 % (nor starts there) before the gate holds."""
    w = world
    with pytest.raises(ex.ExperimentError, match="at most 50 %") as refused:
        rc.start_canary(w.x, {"runtimeId": w.rid, "candidate": candidate(), "acknowledged": True, "treatmentWeight": 75})
    assert refused.value.status == 400 and rc.list_canaries(w.x) == []
    rec = running(w)
    member = ex.Ctx(w.console, "dev", w.session, REGION, ACCOUNT, MEMBER)
    with pytest.raises(ex.ExperimentError) as refused:
        rc.set_split(member, rec["id"], {"treatmentWeight": 25})
    assert refused.value.status == 403
    with pytest.raises(ex.ExperimentError, match="above 50 % only once the gate holds") as refused:
        rc.set_split(w.x, rec["id"], {"treatmentWeight": 99, "acknowledged": True})  # no acknowledgement opens it
    assert [v["weight"] for v in w.aws.tests[rec["abTest"]["id"]]["variants"]] == [95, 5] and refused.value.extra["gate"]["ok"] is False
    verified(w, rec, bands={"Builtin.Correctness": 0.1})
    w.aws.results = results(0.60, 12, 0.66, 10)
    assert rc.set_split(w.x, rec["id"], {"treatmentWeight": 75})["weights"] == {"C": 25, "T1": 75}  # the gate holds


def test_a_promotion_that_moved_production_but_did_not_finish_is_in_the_release_log(world, monkeypatch):
    """review L1: an incomplete promotion (a later stage failed after UpdateAgentRuntime) is recorded with its override."""
    w = world
    rec = running(w)
    verified(w, rec)
    w.aws.results = results(0.8, 10, 0.7, 10)
    real = w.aws.ctl_update_agent_runtime_endpoint

    def move(agentRuntimeId, endpointName, agentRuntimeVersion=None, **p):
        if endpointName == "live":
            raise err("ThrottlingException", "Rate exceeded")
        return real(agentRuntimeId, endpointName, agentRuntimeVersion, **p)

    monkeypatch.setattr(w.aws, "ctl_update_agent_runtime_endpoint", move)
    out = rc.promote(w.x, rec["id"], {"acknowledged": True, "reason": "ship it anyway"})
    assert finish(w.console, out["job"]["id"])["status"] == "failed"
    after = rc._get(w.x, rec["id"])
    assert after["status"] == "promoted" and after["promotion"]["incomplete"] and w.aws.runtimes[w.rid]["agentRuntimeVersion"] == "2"
    [release] = [r for r in w.console.store.read("promotions", []) if r["experiment"] == rec["id"]]
    assert release["override"] is True and release["reason"] == "ship it anyway" and "ThrottlingException" in release["incomplete"]
    assert (release["fromVersion"], release["toVersion"]) == ("1", "2") and ex.promotions(w.x)[0]["experiment"] == rec["id"]


def test_a_cleanup_that_left_something_is_not_done_and_runs_again(world, monkeypatch):
    """review L6: a resource AWS refused to delete leaves the canary incomplete (not cleaned), and cleanup runs again."""
    w = world
    rec = running(w)
    rc.rollback(w.x, rec["id"], {"reason": "no"})
    real, refusing = w.aws.ctl_delete_online_evaluation_config, [True]

    def delete(**p):
        if refusing[0]:
            raise err("ConflictException", "referenced by an A/B test being deleted")
        return real(**p)

    monkeypatch.setattr(w.aws, "ctl_delete_online_evaluation_config", delete)
    finish(w.console, rc.cleanup_canary(w.x, rec["id"])["id"])
    after = rc._get(w.x, rec["id"])
    assert after["status"] == "rolled_back" and not after.get("cleanedAt") and len(after["cleanupLeft"]) == 2 and "clean up again" in after["error"]
    assert all(c["failed"] for c in after["cleanup"] if c["result"].startswith("left: ConflictException"))
    refusing[0] = False
    job = finish(w.console, rc.cleanup_canary(w.x, rec["id"])["id"])
    after = rc._get(w.x, rec["id"])
    assert job["status"] == "succeeded" and after["cleanedAt"] and not after["cleanupLeft"] and not w.aws.evals
    with pytest.raises(ex.ExperimentError, match="already cleaned"):
        rc.cleanup_canary(w.x, rec["id"])


def test_a_canary_in_a_spoke_workspace_makes_every_role_with_its_boundary(server):
    """review H3: the routes give the canary the workspace's permissionsBoundaryArn: its role and the candidate's build
    role are created with it (on the console's path). Production's role is passed to the candidate only when it is the
    console's own, on that path, and gets the boundary first when it lacks it; a legacy role on / is refused there."""
    s = server
    boundary = f"arn:aws:iam::{ACCOUNT}:policy/adlc-console-spoke-boundary"
    assert s.call("POST", "/api/console/workspaces", {"id": "spoke", "accountId": ACCOUNT, "region": REGION, "profile": "default",
                                                      "permissionsBoundaryArn": boundary})[0] == 201
    deps = candidate(archive=b64(zip_of({"main.py": MAIN, "requirements.txt": "httpx\n"})), installRequirements=True)

    def started():
        status, out = s.call("POST", "/api/console/workspaces/spoke/experiments/runtime-canaries", {"runtimeId": s.rid, "candidate": deps,
                                                                                                  "acknowledged": True})
        assert status == 202, out
        deadline = time.monotonic() + 20
        while s.call("GET", f"/api/console/jobs/{out['job']['id']}")[1]["status"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        return out, s.call("GET", f"/api/console/jobs/{out['job']['id']}")[1]

    role = f"adlc-console-rt-{PROD}"  # production's role is on / (made before roles had a path): never passed from a spoke workspace
    out, job = started()
    assert job["status"] == "failed" and "passes only the console's own roles" in job["error"] and "on /" in job["error"]
    assert not s.aws.named("bedrock-agentcore-control", "create_agent_runtime") and not s.aws.named("iam", "put_role_permissions_boundary")
    s.aws.roles[role].update(Path="/adlc-console/", Arn=f"arn:aws:iam::{ACCOUNT}:role/adlc-console/{role}")  # on the path, no boundary yet
    s.aws.runtimes[s.rid]["roleArn"] = s.aws.roles[role]["Arn"]
    out, job = started()
    assert job["status"] == "succeeded", job.get("error")
    made = {r["RoleName"]: r.get("PermissionsBoundary") for r in s.aws.named("iam", "create_role")}
    assert made == {out["canary"]["names"]["role"]: boundary, f"adlc-console-build-{REGION}": boundary}
    ops = [(o, p.get("RoleName")) for svc, o, p in s.aws.calls if svc in ("iam", "bedrock-agentcore-control")]
    assert ops.index(("put_role_permissions_boundary", role)) < ops.index(("create_agent_runtime", None))  # bounded before it is passed
    [create] = s.aws.named("bedrock-agentcore-control", "create_agent_runtime")
    assert create["roleArn"] == f"arn:aws:iam::{ACCOUNT}:role/adlc-console/{role}" and s.aws.roles[role]["boundary"] == boundary


# -- review 3: production moves under one job, cut-off rollbacks, older records, requests that went out whole ---------------

DECISION = {"at": "2026-10-01T00:00:00Z", "by": "ann", "metric": "Builtin.Correctness", "gate": {"ok": False, "conditions": []}, "override": True,
            "failed": ["verification", "ab"], "reason": "ship it"}


def _blocked(monkeypatch, owner, name: str) -> threading.Event:
    """``owner.name`` waits for the returned event before it runs: a job held mid-stage."""
    gate = threading.Event()
    real = getattr(owner, name)
    monkeypatch.setattr(owner, name, lambda self, *a, **kw: (gate.wait(10), real(self, *a, **kw))[1])
    return gate


def _promoted(w) -> dict:
    verified(w, w.rec)
    w.aws.results = results(0.8, 10, 0.82, 10)
    assert finish(w.console, rc.promote(w.x, w.rec["id"], {})["job"]["id"])["status"] == "succeeded"
    return rc._get(w.x, w.rec["id"])


def _outside(w, version: str = "1", **env) -> None:
    """Someone's UpdateAgentRuntime of production outside the console: version ``version``'s artifact, other variables."""
    old = w.aws.snapshots[w.rid][version]
    w.aws.ctl_update_agent_runtime(w.rid, agentRuntimeArtifact=old["agentRuntimeArtifact"], environmentVariables={**old["environmentVariables"], **env})


def test_nothing_else_moves_production_while_its_canary_promotes_and_the_canary_waits_for_deploy_jobs(tmp_path, monkeypatch):
    """review 3 #5: while the promotion publishes production, the deploy page neither publishes a version of it nor
    moves or deletes any of its endpoints (a version made then was taken for the promotion); a promotion or rollback
    does not start while a deploy job of production runs; a record a restart left promoting holds nothing."""
    w = make_world(tmp_path / "a")
    rec = running(w)
    verified(w, rec)
    w.aws.results = results(0.8, 10, 0.82, 10)
    gate = _blocked(monkeypatch, rc.PublishPipeline, "_validate")
    out = rc.promote(w.x, rec["id"], {})
    zipped = {"source": "zip", "archive": b64(zip_of({"main.py": MAIN}))}
    try:
        for move in (lambda: dp.start_deploy(w.console, "dev", zipped, runtime_id=w.rid),
                     lambda: dp.point_endpoint(w.console, "dev", w.rid, {"name": "live", "version": "1"}),
                     lambda: dp.point_endpoint(w.console, "dev", w.rid, {"name": "blue", "version": "1"}),
                     lambda: dp.delete_endpoint(w.console, "dev", w.rid, "live"),
                     lambda: dp.start_delete(w.console, "dev", w.rid)):
            with pytest.raises(dp.DeployError, match="is promoting, and its job is publishing production"):
                move()
    finally:
        gate.set()
    assert finish(w.console, out["job"]["id"])["status"] == "succeeded" and not dp.deploy_jobs(w.console, "dev")
    after = rc._get(w.x, rec["id"])
    assert after["status"] == "promoted" and after["promotion"]["toVersion"] == "2" and w.aws.endpoints[w.rid]["live"]["targetVersion"] == "2"
    assert dp.point_endpoint(w.console, "dev", w.rid, {"name": "blue", "version": "2"})["action"] == "created"  # done: production is free

    held = _blocked(monkeypatch, dp.Pipeline, "_upload")
    deploying = dp.start_deploy(w.console, "dev", zipped, runtime_id=w.rid)
    try:
        with pytest.raises(ex.ExperimentError, match="has a deploy job running") as refused:
            rc.rollback(w.x, rec["id"], {"reason": "worse"})
        assert refused.value.status == 409 and rc._get(w.x, rec["id"])["status"] == "promoted"
    finally:
        held.set()
    assert finish(w.console, deploying["id"])["status"] == "succeeded"

    w = make_world(tmp_path / "b")
    rec = running(w)
    verified(w, rec)
    w.aws.results = results(0.8, 10, 0.82, 10)
    held = _blocked(monkeypatch, dp.Pipeline, "_upload")
    deploying = dp.start_deploy(w.console, "dev", zipped, runtime_id=w.rid)  # a running canary: allowed, the gate's (c) refuses later
    try:
        with pytest.raises(ex.ExperimentError, match="has a deploy job running") as refused:
            rc.promote(w.x, rec["id"], {})
        assert refused.value.status == 409 and rc._get(w.x, rec["id"])["status"] == "running"
    finally:
        held.set()
    finish(w.console, deploying["id"])

    w = make_world(tmp_path / "c")
    rec = running(w)
    _cut_off(w, "runtime-canary-promote", rec, "promoteJob", "promoting", decision=DECISION)
    w.console.jobs = Jobs(tmp_path / "c" / "console" / "jobs")  # restarted: the record still says promoting, its job is gone
    assert dp.point_endpoint(w.console, "dev", w.rid, {"name": "blue", "version": "1"})["action"] == "created"


def test_a_promotion_that_did_not_finish_records_the_candidates_version_never_one_made_beside_it(tmp_path, monkeypatch):
    """review 3 #5: the version a promotion published is the first after production's that runs the candidate's
    artifact and variables; a version made meanwhile outside the console is not taken for it, and a promotion that
    made none is not recorded as one."""
    w = make_world(tmp_path / "a")
    rec = running(w)
    verified(w, rec)
    w.aws.results = results(0.8, 10, 0.82, 10)

    def outside_then_fail(self):
        _outside(w, MODEL_ID="m9")  # while the promotion waits for READY
        raise dp.DeployError("the runtime is still UPDATING after 0 min")

    monkeypatch.setattr(rc.PublishPipeline, "_ready", outside_then_fail)
    job = finish(w.console, rc.promote(w.x, rec["id"], {})["job"]["id"])
    after = rc._get(w.x, rec["id"])
    assert job["status"] == "failed" and after["status"] == "promoted" and after["promotion"]["toVersion"] == "2"
    assert "still UPDATING" in after["promotion"]["incomplete"] and "moved on to version 3" in after["promotion"]["incomplete"]
    [release] = [p for p in w.console.store.read("promotions", []) if p["experiment"] == rec["id"]]
    assert release["toVersion"] == "2" and release["incomplete"]
    job = finish(w.console, rc.rollback(w.x, rec["id"], {"reason": "worse"})["job"]["id"])  # production moved on: from the deploy page
    assert job["status"] == "failed" and "is version 3 now, not the promoted 2" in rc._get(w.x, rec["id"])["error"]
    assert rc._get(w.x, rec["id"])["status"] == "promoted" and w.aws.runtimes[w.rid]["agentRuntimeVersion"] == "3"

    w = make_world(tmp_path / "b")
    rec = running(w)
    _cut_off(w, "runtime-canary-promote", rec, "promoteJob", "promoting", decision=DECISION)
    _outside(w, MODEL_ID="m9")  # the version made before the restart is not the candidate
    w.console.jobs = Jobs(tmp_path / "b" / "console" / "jobs")
    after = rc._get(w.x, rec["id"])
    assert after["status"] == "stopped" and "version 2, which does not run the candidate" in after["error"] and not after.get("promotion")
    assert not [p for p in w.console.store.read("promotions", []) if p["experiment"] == rec["id"]]


def test_a_rollback_cut_off_after_it_published_the_version_before_again_is_rolled_back(tmp_path, monkeypatch):
    """review 3 #6: a rollback that the restart, or a later stage's failure, cut off after its UpdateAgentRuntime is
    rolled back — incomplete, and in the release log once — not left promoted with a retry that can never pass; one cut
    off before it stays promoted, and rolling back again finishes it."""
    w = make_world(tmp_path / "a")
    w.rec = running(w)
    promoted = _promoted(w)
    jid = _cut_off(w, "runtime-canary-rollback", promoted, "rollbackJob", "rolling_back",
                   rollbackRequest={"at": "2026-10-02T00:00:00Z", "by": "ann", "reason": "worse"})
    _outside(w)  # what the rollback's UpdateAgentRuntime published: version 1's artifact and variables, as version 3
    w.console.jobs = Jobs(tmp_path / "a" / "console" / "jobs")
    after = rc._get(w.x, w.rec["id"])
    back = after["rollback"]
    assert after["status"] == "rolled_back" and "restarted" in back["incomplete"] and after["error"] == back["incomplete"]
    assert (back["defaultVersion"], back["restoredFrom"], back["by"], back["reason"], back["job"]) == ("3", "1", "ann", "worse", jid)
    rc._get(w.x, w.rec["id"])
    [logged] = [p for p in w.console.store.read("promotions", []) if p["experiment"] == w.rec["id"] and p["kind"] == "runtime-canary-rollback"]
    assert (logged["fromVersion"], logged["toVersion"], logged["job"]) == ("2", "3", jid) and logged["incomplete"] and logged["reason"] == "worse"
    with pytest.raises(ex.ExperimentError, match="nothing to roll back"):
        rc.rollback(w.x, w.rec["id"], {})

    w = make_world(tmp_path / "b")
    w.rec = running(w)
    _cut_off(w, "runtime-canary-rollback", _promoted(w), "rollbackJob", "rolling_back")
    w.console.jobs = Jobs(tmp_path / "b" / "console" / "jobs")
    assert rc._get(w.x, w.rec["id"])["status"] == "promoted"  # it had published nothing: roll back again
    job = finish(w.console, rc.rollback(w.x, w.rec["id"], {"reason": "again"})["job"]["id"])
    after = rc._get(w.x, w.rec["id"])
    assert job["status"] == "succeeded" and after["status"] == "rolled_back" and not after["error"] and after["rollback"]["defaultVersion"] == "3"
    assert w.aws.endpoints[w.rid]["live"]["targetVersion"] == "1"

    w = make_world(tmp_path / "c")
    w.rec = running(w)
    _promoted(w)
    monkeypatch.setattr(rc.PublishPipeline, "_smoke", lambda self: (_ for _ in ()).throw(dp.DeployError("the smoke call failed")))
    job = finish(w.console, rc.rollback(w.x, w.rec["id"], {"reason": "worse"})["job"]["id"])
    after = rc._get(w.x, w.rec["id"])
    assert job["status"] == "failed" and after["status"] == "rolled_back" and "smoke call failed" in after["rollback"]["incomplete"]
    assert [p["kind"] for p in w.console.store.read("promotions", []) if p["experiment"] == w.rec["id"]] == ["runtime-canary", "runtime-canary-rollback"]


def test_records_and_jobs_from_before_the_upgrade_are_read_as_if_they_had_the_new_fields(tmp_path):
    """review 3 #11: a canary's verification job without candidateVersion is credited (the candidate's DEFAULT moves
    only to new versions, and the gate's (c) checks it is still on the pinned one); a cleanup the upgrade's restart cut
    off before cleanupFrom existed returns to promoted; a Dockerfile candidate without the built flag is the
    canary's own build, and its image goes with the canary."""
    w = make_world(tmp_path / "a")
    rec = running(w)
    old = {"agent": PROD, "agentId": w.rid, "contractSet": "cs-1", "repeat": 2, "panel": True,
           "treatment": {"fingerprint": rec["treatment"]["fingerprint"], "agentVersion": rec["agent"]["version"], "canary": rec["id"]}}
    finish(w.console, w.console.jobs.start("verify", "dev", old, lambda job: {"robust": True, "holding": 3, "contracts": [{}] * 3, "repeat": 2})["id"])
    found, _ = ex.find_verification(w.x, rc._get(w.x, rec["id"]))
    assert found and found["treatmentVersion"] == rec["copy"]["version"] == "1"
    w.aws.results = results(0.8, 10, 0.82, 10)
    gate = rc.canary_detail(w.x, rec["id"])["gate"]
    assert gate["ok"], gate["conditions"]
    assert finish(w.console, rc.promote(w.x, rec["id"], {})["job"]["id"])["status"] == "succeeded"
    w.console.store.update(rc.COLLECTION, {}, lambda all_: {**all_, rec["id"]: {**all_[rec["id"]], "status": "cleaning",  # what the old cleanup saved
                                                                                "updatedAt": "2026-10-01T00:00:00Z"}})
    after = rc._get(w.x, rec["id"])
    assert after["status"] == "promoted" and after["cleanupFrom"] == "promoted" and after["promotion"]
    assert finish(w.console, rc.cleanup_canary(w.x, rec["id"])["id"])["status"] == "succeeded"
    after = rc._get(w.x, rec["id"])
    assert after["status"] == "promoted" and after["cleanedAt"]

    w = make_world(tmp_path / "b", container=True)
    rec = _dockerfile_canary(w)
    pushed = rec["copy"]["artifact"]["containerConfiguration"]["containerUri"].split("@", 1)[1]
    w.console.store.update(rc.COLLECTION, {}, lambda all_: {**all_, rec["id"]: {**all_[rec["id"]], "candidate": {
        k: v for k, v in all_[rec["id"]]["candidate"].items() if k != "built"}}})
    assert rc._get(w.x, rec["id"])["candidate"]["built"] is True
    rc.rollback(w.x, rec["id"], {})
    finish(w.console, rc.cleanup_canary(w.x, rec["id"])["id"])
    assert w.aws.named("ecr", "batch_delete_image") == [{"repositoryName": "adlc-console/team-agent", "imageIds": [{"imageDigest": pushed}]}]


def test_a_turn_that_went_out_whole_is_never_sent_again_nor_answered_by_production(world, monkeypatch):
    """review 3 #14: botocore raises SSLError for a TLS record broken while the answer is read too, after the arm got
    the whole turn: only a failure before the request's last byte went out is sent again, or answered by production."""
    class SSLError(Exception):  # botocore's, by name
        pass

    posts = []

    def broken_after_sending(session, region, url, data, headers, **kw):
        posts.append(url)
        while data.read(8192):  # the HTTP client sends the body block by block, until a read comes back empty
            pass
        raise SSLError("[SSL: RECORD_LAYER_FAILURE] record layer failure")

    monkeypatch.setattr(ex, "post_signed", broken_after_sending)
    with pytest.raises(rc.SentError, match="SSLError after the whole request was sent"):
        rc.post_signed(None, REGION, "https://gw/t/invocations", b'{"prompt": "x"}', {})
    assert len(posts) == 1
    w = world
    rec = running(w)
    before = len(w.aws.named("bedrock-agentcore", "invoke_agent_runtime"))
    events = list(rc.invoke_through(w.session, REGION, rec, message="2+2?", session_id="s" * 40, actor="ann"))
    assert [e["type"] for e in events] == ["session", "experiment", "error"] and "not sent again" in events[2]["error"]
    assert len(w.aws.named("bedrock-agentcore", "invoke_agent_runtime")) == before and len(posts) == 2

    posts.clear()
    monkeypatch.setattr(ex, "post_signed", lambda session, region, url, data, headers, **kw: (posts.append(url), (_ for _ in ()).throw(
        SSLError("EOF occurred in violation of protocol")))[1])  # the handshake broke: nothing went out
    events = list(rc.invoke_through(w.session, REGION, rec, message="2+2?", session_id="s" * 40, actor="ann"))
    assert len(posts) == rc.POST_ATTEMPTS and [e["type"] for e in events][:3] == ["session", "experiment", "fallback"]
    assert len(w.aws.named("bedrock-agentcore", "invoke_agent_runtime")) == before + 1


class _Creds:
    def get_credentials(self):
        from botocore.credentials import Credentials

        return Credentials("AKIDEXAMPLE", "secret", "token")


def _no_proxy(monkeypatch) -> None:
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


def _listener(handle) -> tuple[socket.socket, list]:
    """A local server: ``handle(raw socket)`` for every connection, returning what it read (kept in the list)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    got: list = []

    def serve() -> None:
        while True:
            try:
                raw, _ = srv.accept()
            except OSError:
                return
            try:
                got.append(handle(raw))
            except Exception as exc:  # noqa: BLE001 - the client's side is what is checked
                got.append(exc)
            finally:
                raw.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv, got


def _read_request(conn) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        data += conn.recv(65536)
    head, _, body = data.partition(b"\r\n\r\n")
    length = int(next(line.split(b":")[1] for line in head.split(b"\r\n") if line.lower().startswith(b"content-length")))
    while len(body) < length:
        body += conn.recv(65536)
    return body


def test_the_sent_mark_follows_botocores_http_stack(monkeypatch):
    """``experiments.SentBody`` through botocore's urllib3 session, as the canary sends a turn: not sent when the
    connection is refused; sent once the server has read the whole request, even though it then drops the connection."""
    _no_proxy(monkeypatch)
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()  # nothing listens there
    body = ex.SentBody(b'{"prompt": "x"}')
    with pytest.raises(Exception) as refused:
        ex.post_signed(_Creds(), REGION, f"http://127.0.0.1:{port}/t/invocations", body, {"Content-Type": "application/json"}, timeout=5)
    assert type(refused.value).__name__ == "EndpointConnectionError" and not body.sent

    def read_then_reset(raw):
        got = _read_request(raw)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # then the connection breaks (RST)
        return got

    srv, got = _listener(read_then_reset)
    body = ex.SentBody(b'{"prompt": "x"}')
    with pytest.raises(Exception):
        ex.post_signed(_Creds(), REGION, f"http://127.0.0.1:{srv.getsockname()[1]}/t/invocations", body, {"Content-Type": "application/json"}, timeout=5)
    srv.close()
    assert got == [b'{"prompt": "x"}'] and body.sent


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl makes the test certificate")
def test_a_tls_record_broken_after_the_whole_turn_arrived_is_not_sent_again(tmp_path, monkeypatch):
    """review 3 #14, over TLS: a server that read the whole turn and then breaks the TLS session gets it once (botocore
    raises SSLError, the canary SentError); one that drops the connection in the handshake got nothing, and the turn is
    sent again."""
    import botocore.httpsession

    _no_proxy(monkeypatch)
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True)
    monkeypatch.setattr(botocore.httpsession, "get_cert_path", lambda verify: str(cert))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    def read_then_break(raw):
        conn = context.wrap_socket(raw, server_side=True)
        got = _read_request(conn)
        os.write(conn.fileno(), b"\x17\x03\x03\x00\x10" + b"\x00" * 16)  # a TLS record no key decrypts
        return got

    srv, got = _listener(read_then_break)
    url = f"https://localhost:{srv.getsockname()[1]}/t/invocations"
    with pytest.raises(rc.SentError, match="SSLError"):
        rc.post_signed(_Creds(), REGION, url, b'{"prompt": "transfer 100"}', {"Content-Type": "application/json"}, timeout=5)
    srv.close()
    assert got == [b'{"prompt": "transfer 100"}']  # once

    srv, got = _listener(lambda raw: raw.recv(10))  # the ClientHello's first bytes, then the connection goes
    url = f"https://localhost:{srv.getsockname()[1]}/t/invocations"
    with pytest.raises(Exception) as dropped:
        rc.post_signed(_Creds(), REGION, url, b'{"prompt": "transfer 100"}', {"Content-Type": "application/json"}, timeout=5)
    srv.close()
    assert type(dropped.value).__name__ in rc.UNSENT and len(got) == rc.POST_ATTEMPTS  # nothing went out: sent again, each time


# -- review 4: one move of production at a time across canaries, and a cut whose check could not read -----------------------

def _ids(monkeypatch, *hexes):
    """start_canary's next canary ids, in order (the store keeps canaries sorted by id)."""
    queue, real = list(hexes), rc.secrets.token_hex
    monkeypatch.setattr(rc, "secrets", SimpleNamespace(token_hex=lambda n: queue.pop(0) if n == 4 and queue else real(n)))


def _first_call_waits(monkeypatch, owner, name: str) -> tuple[threading.Event, threading.Event]:
    """``owner.name``'s first call waits for the returned gate (``entered`` says it got there); later calls run at once."""
    gate, entered, calls = threading.Event(), threading.Event(), []
    real = getattr(owner, name)

    def wrapped(self, *a, **kw):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            gate.wait(20)
        return real(self, *a, **kw)

    monkeypatch.setattr(owner, name, wrapped)
    return gate, entered


def _attempt(fn) -> tuple[str, object]:
    try:
        return "ok", fn()
    except Exception as exc:  # noqa: BLE001 - the outcome is what is checked
        return "refused", exc


@pytest.mark.parametrize("order", [("ffffffff", "00000000"), ("00000000", "ffffffff")])
def test_while_one_canary_moves_production_no_other_canary_or_deploy_does(tmp_path, monkeypatch, order):
    """review 4 #1, #2: canary A promoted and canary B running on one production. While A's rollback job publishes
    production, the deploy page refuses a new version and an endpoint move, and B's promotion is refused, whichever
    canary sorts first in the store."""
    _ids(monkeypatch, *order)
    w = make_world(tmp_path)
    w.rec = running(w)
    a = _promoted(w)
    b = running(w)
    verified(w, b)
    gate, entered = _first_call_waits(monkeypatch, rc.PublishPipeline, "_validate")
    out = rc.rollback(w.x, a["id"], {"reason": "worse"})
    try:
        assert entered.wait(10)
        for move in (lambda: dp.start_deploy(w.console, "dev", {"source": "zip", "archive": b64(zip_of({"main.py": MAIN}))}, runtime_id=w.rid),
                     lambda: dp.point_endpoint(w.console, "dev", w.rid, {"name": "blue", "version": "1"})):
            with pytest.raises(dp.DeployError, match=f"canary {a['id']} of team_agent is rolling_back, and its job is publishing production"):
                move()
        with pytest.raises(ex.ExperimentError, match=f"the canary {a['id']} of team_agent is rolling_back") as refused:
            rc.promote(w.x, b["id"], {})
        assert refused.value.status == 409 and rc._get(w.x, b["id"])["status"] == "running"
    finally:
        gate.set()
    assert finish(w.console, out["job"]["id"])["status"] == "succeeded"
    assert [(p["experiment"], p["kind"]) for p in w.console.store.read("promotions", [])] == [(a["id"], "runtime-canary"), (a["id"], "runtime-canary-rollback")]


def test_two_promotions_of_one_canary_at_once_start_one_job(world):
    """review 4 #2: two promotions that passed the gate together: the second reads the record again under
    production's lock, finds it promoting and is refused. One job, one release."""
    w = world
    rec = running(w)
    verified(w, rec)
    w.aws.results = results(0.8, 10, 0.82, 10)
    outcomes: list = []
    with dp.moves(w.rid):  # both pass the gate, then wait here
        threads = [threading.Thread(target=lambda: outcomes.append(_attempt(lambda: rc.promote(w.x, rec["id"], {})))) for _ in range(2)]
        for t in threads:
            t.start()
        time.sleep(1.0)
    for t in threads:
        t.join(20)
    assert sorted(kind for kind, _ in outcomes) == ["ok", "refused"]
    refused = next(out for kind, out in outcomes if kind == "refused")
    assert isinstance(refused, ex.ExperimentError) and "is promoting now: nothing to promote" in str(refused)
    accepted = next(out for kind, out in outcomes if kind == "ok")
    assert finish(w.console, accepted["job"]["id"])["status"] == "succeeded"
    assert [(p["kind"], p["toVersion"]) for p in w.console.store.read("promotions", [])] == [("runtime-canary", "2")]


def test_a_cut_off_promotion_or_rollback_whose_check_could_not_read_is_checked_again(tmp_path, monkeypatch):
    """review 4 #3: an outage that fails a promotion's or rollback's later stage also fails the reads that tell how far
    it came. The record then stays promoting / rolling_back (no job holds it, so nothing waits on it) and is settled
    when it is next read: not a final 'stopped', nor a 'roll back again' whose retry can never pass."""
    from botocore.exceptions import EndpointConnectionError

    def outage_at_ready(w, patch) -> None:
        def ready(self):
            w.aws.down = 1  # the next read fails too
            raise EndpointConnectionError(endpoint_url="https://bedrock-agentcore-control.us-west-2.amazonaws.com")

        patch.setattr(rc.PublishPipeline, "_ready", ready)
        real = w.aws.ctl_get_agent_runtime

        def get(agentRuntimeId, **p):
            if getattr(w.aws, "down", 0) > 0:
                w.aws.down -= 1
                raise EndpointConnectionError(endpoint_url="https://bedrock-agentcore-control.us-west-2.amazonaws.com")
            return real(agentRuntimeId, **p)

        patch.setattr(w.aws, "ctl_get_agent_runtime", get)

    w = make_world(tmp_path / "a")
    rec = running(w)
    verified(w, rec)
    w.aws.results = results(0.8, 10, 0.82, 10)
    with monkeypatch.context() as patch:
        outage_at_ready(w, patch)
        job = finish(w.console, rc.promote(w.x, rec["id"], {})["job"]["id"])
    assert job["status"] == "failed" and w.console.store.read(rc.COLLECTION, {})[rec["id"]]["status"] == "promoting"  # not decided yet
    assert dp.point_endpoint(w.console, "dev", w.rid, {"name": "blue", "version": "1"})["action"] == "created"  # nothing waits on it
    after = rc._get(w.x, rec["id"])
    assert after["status"] == "promoted" and after["promotion"]["toVersion"] == "2" and "EndpointConnectionError" in after["promotion"]["incomplete"]
    assert [p["kind"] for p in w.console.store.read("promotions", [])] == ["runtime-canary"]

    w = make_world(tmp_path / "b")
    w.rec = running(w)
    _promoted(w)
    with monkeypatch.context() as patch:
        outage_at_ready(w, patch)
        job = finish(w.console, rc.rollback(w.x, w.rec["id"], {"reason": "worse"})["job"]["id"])
    assert job["status"] == "failed" and w.console.store.read(rc.COLLECTION, {})[w.rec["id"]]["status"] == "rolling_back"
    after = rc._get(w.x, w.rec["id"])
    assert after["status"] == "rolled_back" and after["rollback"]["defaultVersion"] == "3" and "EndpointConnectionError" in after["rollback"]["incomplete"]
    assert [p["kind"] for p in w.console.store.read("promotions", [])] == ["runtime-canary", "runtime-canary-rollback"]
    with pytest.raises(ex.ExperimentError, match="nothing to roll back"):
        rc.rollback(w.x, w.rec["id"], {})


def test_two_reads_that_settle_one_cut_off_promotion_at_once_log_it_once(world, tmp_path, monkeypatch):
    """review 4 #5: the release log's entry for an incomplete promotion is decided inside the store's update, so the
    list and the detail settling the record at the same moment log it once."""
    w = world
    rec = running(w)
    _cut_off(w, "runtime-canary-promote", rec, "promoteJob", "promoting", decision=DECISION)
    candidate_rt = w.aws.runtimes[rec["copy"]["id"]]
    w.aws.ctl_update_agent_runtime(w.rid, agentRuntimeArtifact=candidate_rt["agentRuntimeArtifact"], environmentVariables=candidate_rt["environmentVariables"])
    w.console.jobs = Jobs(tmp_path / "console" / "jobs")
    barrier, real = threading.Barrier(2, timeout=10), rc._published
    monkeypatch.setattr(rc, "_published", lambda *a, **kw: (barrier.wait(), real(*a, **kw))[1])
    threads = [threading.Thread(target=rc._get, args=(w.x, rec["id"])) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert [p["job"] for p in w.console.store.read("promotions", []) if p["experiment"] == rec["id"]] == [f"runtime-canary-promote-{'0' * 9}1"]
    assert w.console.store.read(rc.COLLECTION, {})[rec["id"]]["status"] == "promoted"


def test_the_move_lock_is_one_per_runtime_and_a_long_wait_is_refused(monkeypatch):
    """review 4 #4: an action on one runtime never waits behind another runtime's; one on the same runtime waits up to
    MOVE_WAIT and is then refused (a canary's action with a 409)."""
    monkeypatch.setattr(dp, "MOVE_WAIT", 0.2)
    with dp.moves("team_agent-AbCdEf1234"):
        started = time.monotonic()
        with dp.moves("other_agent-AbCdEf1234"):
            assert time.monotonic() - started < 0.1
        with pytest.raises(dp.DeployError, match="another action on the runtime team_agent-AbCdEf1234 is still running"):
            with dp.moves("team_agent-AbCdEf1234"):
                pass
        with pytest.raises(ex.ExperimentError) as refused:
            with dp.moves("team_agent-AbCdEf1234", busy=rc._busy_error):
                pass
        assert refused.value.status == 409
    with dp.moves("team_agent-AbCdEf1234"):
        pass  # free again


def test_the_deploy_page_says_wait_only_while_a_job_moves_production(tmp_path):
    """review 4 #6: a record a restart left rolling_back moves nothing: the deploy page does not tell the admin to
    wait for a job that is gone, and production's endpoints move again."""
    w = make_world(tmp_path)
    w.rec = running(w)
    _cut_off(w, "runtime-canary-rollback", _promoted(w), "rollbackJob", "rolling_back")
    w.console.jobs = Jobs(tmp_path / "console" / "jobs")
    with pytest.raises(dp.DeployError) as refused:
        dp.start_delete(w.console, "dev", w.rid)
    assert "wait for it" not in str(refused.value) and "on the A/B page" in str(refused.value)
    assert dp.point_endpoint(w.console, "dev", w.rid, {"name": "blue", "version": "1"})["action"] == "created"


# -- review 5: a settle never writes over a newer state; nothing stays undecided; one create of a name at a time ----------

def test_a_settle_that_read_before_a_new_promotion_does_not_write_over_it(tmp_path, monkeypatch):
    """review 5 #1: the canary page's poll settles a cut-off promotion while its AWS reads are slow; meanwhile the
    admin's own read settles it and a new promotion starts. The poll's late decision is not written: the record stays
    promoting with the new job, and nothing else moves production under it."""
    w = make_world(tmp_path)
    rec = running(w)
    verified(w, rec)
    w.aws.results = results(0.8, 10, 0.82, 10)
    _cut_off(w, "runtime-canary-promote", rec, "promoteJob", "promoting", decision=DECISION)
    w.console.jobs = Jobs(tmp_path / "console" / "jobs")  # restarted: production did not move
    slow, entered, calls, real = threading.Event(), threading.Event(), [], rc._published

    def published(*a, **kw):
        calls.append(1)
        if len(calls) == 1:  # the poll's reads, slow
            entered.set()
            slow.wait(20)
        return real(*a, **kw)

    monkeypatch.setattr(rc, "_published", published)
    poll = threading.Thread(target=rc._get, args=(w.x, rec["id"]))
    poll.start()
    gate, started = _first_call_waits(monkeypatch, rc.PublishPipeline, "_validate")
    try:
        assert entered.wait(10)
        out = rc.promote(w.x, rec["id"], {})  # its own read settles the record stopped; then it promotes
        assert started.wait(10)
        slow.set()
        poll.join(20)
        stored = w.console.store.read(rc.COLLECTION, {})[rec["id"]]
        assert stored["status"] == "promoting" and stored["promoteJob"] == out["job"]["id"]  # the poll's 'stopped' was not written
        assert rc.mover(w.console, "dev", w.rid)["id"] == rec["id"]
        with pytest.raises(dp.DeployError, match="is promoting, and its job is publishing production"):
            dp.point_endpoint(w.console, "dev", w.rid, {"name": "live", "version": "1"})
    finally:
        slow.set()
        gate.set()
    assert finish(w.console, out["job"]["id"])["status"] == "succeeded"
    after = rc._get(w.x, rec["id"])
    assert after["status"] == "promoted" and after["error"] is None  # the earlier cut's error does not stay on it
    assert [(p["kind"], p["toVersion"]) for p in w.console.store.read("promotions", [])] == [("runtime-canary", "2")]


def test_a_cut_off_promotion_or_rollback_that_stays_unreadable_is_settled_as_not_known(tmp_path, monkeypatch):
    """review 5 #3: a check that keeps failing (here AccessDenied, which is not 'gone') leaves the record undecided only
    for UNDECIDED_LIMIT after its job ended; then it is settled as not known, and it can be cleaned up."""
    def denied(w):
        real = w.aws.ctl_get_agent_runtime
        return lambda agentRuntimeId, **p: (_ for _ in ()).throw(err("AccessDeniedException", "not allowed")) if p.get(
            "agentRuntimeVersion") else real(agentRuntimeId, **p)

    w = make_world(tmp_path / "a")
    rec = running(w)
    _cut_off(w, "runtime-canary-promote", rec, "promoteJob", "promoting", decision=DECISION)
    w.console.jobs = Jobs(tmp_path / "a" / "console" / "jobs")
    with monkeypatch.context() as patch:
        patch.setattr(w.aws, "ctl_get_agent_runtime", denied(w))
        assert rc._get(w.x, rec["id"])["status"] == "promoting"  # undecided: read again with the record
        with pytest.raises(ex.ExperimentError, match="wait for its job"):
            rc.cleanup_canary(w.x, rec["id"])
        patch.setattr(rc, "UNDECIDED_LIMIT", -1.0)  # its job ended longer ago than the limit
        after = rc._get(w.x, rec["id"])
    assert after["status"] == "stopped" and "not known, so no promotion is recorded" in after["error"] and not after.get("promotion")
    assert finish(w.console, rc.cleanup_canary(w.x, rec["id"])["id"])["status"] == "succeeded"

    w = make_world(tmp_path / "b")
    w.rec = running(w)
    _cut_off(w, "runtime-canary-rollback", _promoted(w), "rollbackJob", "rolling_back")
    w.console.jobs = Jobs(tmp_path / "b" / "console" / "jobs")
    with monkeypatch.context() as patch:
        patch.setattr(w.aws, "ctl_get_agent_runtime", denied(w))
        assert rc._get(w.x, w.rec["id"])["status"] == "rolling_back"
        patch.setattr(rc, "UNDECIDED_LIMIT", -1.0)
        after = rc._get(w.x, w.rec["id"])
    assert after["status"] == "promoted" and "check production on the deploy page before rolling back again" in after["error"]


def test_two_creates_of_one_new_runtime_name_start_one_job(world, monkeypatch):
    """review 5 #2: the create path runs its check and its job's start under a lock for the name: the second of two
    creates at once is refused, so no failed twin takes back the role the first runtime runs with."""
    w = world
    body = {"source": "zip", "name": "new_agent", "archive": b64(zip_of({"main.py": MAIN})), "filename": "a.zip"}
    gate, _ = _first_call_waits(monkeypatch, dp.Pipeline, "_validate")  # the first job stays running
    outcomes: list = []
    try:
        with dp.moves("create:new_agent"):  # both requests pass their first checks, then wait here
            threads = [threading.Thread(target=lambda: outcomes.append(_attempt(lambda: dp.start_deploy(w.console, "dev", body)))) for _ in range(2)]
            for t in threads:
                t.start()
            time.sleep(1.0)
        for t in threads:
            t.join(20)
    finally:
        gate.set()
    assert sorted(kind for kind, _ in outcomes) == ["ok", "refused"]
    assert "new_agent has a deploy job running" in str(next(out for kind, out in outcomes if kind == "refused"))
    job = finish(w.console, next(out for kind, out in outcomes if kind == "ok")["id"])
    assert job["status"] == "succeeded" and "adlc-console-rt-new_agent" in w.aws.roles


def test_a_rollback_jobs_endpoint_move_does_not_wait_on_the_runtimes_lock(tmp_path, monkeypatch):
    """review 5 #4: another action holding production's lock (a second canary's ramp across its A/B waits) does not
    hold up the rollback job's endpoint move, which its rolling-back state already protects; anyone else waits."""
    monkeypatch.setattr(dp, "MOVE_WAIT", 0.3)
    w = make_world(tmp_path)
    w.rec = running(w)
    promoted = _promoted(w)
    with dp.moves(w.rid):
        started = time.monotonic()
        assert dp.point_endpoint(w.console, "dev", w.rid, {"name": "live", "version": "1"}, but=promoted["id"])["action"] == "moved"
        assert time.monotonic() - started < 0.3
        with pytest.raises(dp.DeployError, match="another action on the runtime .* is still running"):
            dp.point_endpoint(w.console, "dev", w.rid, {"name": "live", "version": "2"})
