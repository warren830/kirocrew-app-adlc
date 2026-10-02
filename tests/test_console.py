"""The standalone console (app/console/server.py): static UI, the workshop backend in process, workspaces, users,
agents, streaming chat, API keys and /v1. In-process server on loopback; AWS is a fake session."""
from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("adlc_console_server", REPO / "app" / "console" / "server.py")
console_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console_mod)  # type: ignore[union-attr]
ACCOUNT = "111122223333"


class FakeControl:
    def __init__(self):
        self.created = []

    def list_harnesses(self, **kw):
        return {"harnesses": [{"harnessName": "hr_agent", "harnessId": "hr_agent-1", "arn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/hr_agent-1",
                               "status": "READY"}]}

    def list_agent_runtimes(self, **kw):
        return {"agentRuntimes": [{"agentRuntimeName": "harness_hr_agent", "agentRuntimeId": "rt-1"},
                                  {"agentRuntimeName": "byoc_agent", "agentRuntimeId": "rt-2", "agentRuntimeArn": "arn:rt-2", "status": "READY"}]}

    def get_harness(self, harnessId):
        return {"harness": {"harnessName": "hr_agent", "harnessId": harnessId, "arn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/hr_agent-1",
                            "status": "READY", "systemPrompt": [{"text": "You help with HR."}], "model": {"bedrockModelConfig": {"modelId": "m"}},
                            "environment": {"agentCoreRuntimeEnvironment": {"agentRuntimeId": "rt-1"}}, "tools": []}}

    def list_tags_for_resource(self, resourceArn):
        return {"tags": {}}


class FakeRuntime:
    def invoke_harness(self, **kw):
        self.last = kw
        return {"stream": [{"contentBlockDelta": {"delta": {"text": "Annual leave is "}}},
                           {"contentBlockStart": {"start": {"toolUse": {"name": "hrtools___retrieve_policy"}}}},
                           {"contentBlockDelta": {"delta": {"text": "15 days."}}}, {"messageStop": {"stopReason": "end_turn"}},
                           {"metadata": {"usage": {"inputTokens": 120, "outputTokens": 9}}}]}


class FakeSession:
    def __init__(self, account=ACCOUNT):
        self.account, self.ctl, self.rt = account, FakeControl(), FakeRuntime()

    def client(self, name, region_name=None, **kw):
        if name == "sts":
            return type("Sts", (), {"get_caller_identity": lambda _s: {"Account": self.account, "Arn": f"arn:aws:iam::{self.account}:user/sa"}})()
        return {"bedrock-agentcore-control": self.ctl, "bedrock-agentcore": self.rt}[name]


class Client:
    def __init__(self, port):
        self.base, self.cookie = f"http://127.0.0.1:{port}", None

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Set-Cookie"):
                    self.cookie = resp.headers["Set-Cookie"].split(";")[0]
                kind = resp.headers.get("Content-Type", "")
                return resp.status, (json.loads(raw) if "json" in kind else raw.decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture()
def console(tmp_path):
    sessions = {"account": ACCOUNT}
    fake = FakeSession()

    def factory(**kw):
        fake.account = sessions["account"]
        return fake

    c = console_mod.Console(tmp_path / "data", session_factory=factory, clients_factory=lambda cfg: None)
    server = console_mod.create_server(c)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"client": Client(server.server_address[1]), "console": c, "fake": fake, "sessions": sessions}
    server.shutdown()
    server.server_close()


def test_the_console_serves_its_ui_the_workshop_app_and_its_backend(console):
    c = console["client"]
    status, html = c.call("GET", "/")
    assert status == 200 and "/static/console.mjs" in html and "importmap" in html
    assert "export default function Console" in c.call("GET", "/static/console.mjs")[1]
    assert "WorkshopCustomizerApp" in c.call("GET", "/app/index.mjs")[1]
    assert c.call("GET", "/static/../server.py")[0] == 404
    status, body = c.call("GET", "/apps/workshop-customizer/api/apps/workshop-customizer/projects")
    assert status == 200 and body == {"projects": []}  # the App's backend, in process
    status, body = c.call("GET", "/api/apps/workshop-customizer/generations/latest/no-such-project")
    assert status == 404  # the Kiro routes, served by the console


def test_workspaces_are_verified_and_agents_listed_and_chatted_with(console):
    c, sessions = console["client"], console["sessions"]
    assert c.call("POST", "/api/console/workspaces", {"id": "dev", "accountId": "12", "region": "us-west-2", "profile": "default"})[0] == 409
    status, ws = c.call("POST", "/api/console/workspaces", {"id": "dev", "name": "Dev", "accountId": ACCOUNT, "region": "us-west-2", "profile": "default"})
    assert status == 201 and ws["accountId"] == ACCOUNT
    assert c.call("POST", "/api/console/workspaces/dev/verify", {})[1]["account"] == ACCOUNT
    status, listed = c.call("GET", "/api/console/workspaces/dev/agents")
    assert [(a["kind"], a["name"]) for a in listed["agents"]] == [("harness", "hr_agent"), ("runtime", "byoc_agent")]  # the Harness's runtime folded in
    status, text = c.call("POST", "/api/console/workspaces/dev/agents/harness/hr_agent-1/chat", {"message": "How many days of leave?"})
    events = [json.loads(line[6:]) for line in text.split("\n") if line.startswith("data: ")]
    assert [e["type"] for e in events] == ["session", "text", "tool", "text", "turn", "stop"] and events[-1]["inputTokens"] == 120
    sid = events[0]["sessionId"]
    c.call("POST", "/api/console/workspaces/dev/agents/harness/hr_agent-1/chat", {"message": "And sick leave?", "sessionId": sid})
    assert console["fake"].rt.last["runtimeSessionId"] == sid  # a second turn continues the same session
    sessions["account"] = "999999999999"
    console["console"].workspaces._cache.clear()
    status, body = c.call("GET", "/api/console/workspaces/dev/agents")
    assert status == 409 and "999999999999" in body["error"]  # never acts in another account


def test_users_sign_in_and_members_see_only_their_workspaces(console):
    c = console["client"]
    c.call("POST", "/api/console/workspaces", {"id": "dev", "accountId": ACCOUNT, "region": "us-west-2", "profile": "default"})
    c.call("POST", "/api/console/workspaces", {"id": "prod", "accountId": ACCOUNT, "region": "us-east-1", "profile": "default"})
    assert c.call("GET", "/api/console/me")[1]["open"] is True
    assert c.call("POST", "/api/console/users", {"username": "ann", "password": "0123456789", "role": "member"})[0] == 401  # the first must be an admin
    assert c.call("POST", "/api/console/users", {"username": "admin", "password": "0123456789", "role": "admin"})[0] == 201
    assert c.call("GET", "/api/console/me")[0] == 401  # no longer open: sign in
    assert c.call("POST", "/api/console/login", {"username": "admin", "password": "wrong-password"})[0] == 401
    assert c.call("POST", "/api/console/login", {"username": "admin", "password": "0123456789"})[0] == 200
    assert c.call("POST", "/api/console/users", {"username": "ann", "password": "0123456789", "role": "member", "workspaces": ["dev"]})[0] == 201
    c.call("POST", "/api/console/logout", {})
    c.cookie = None
    assert c.call("POST", "/api/console/login", {"username": "ann", "password": "0123456789"})[0] == 200
    assert [w["id"] for w in c.call("GET", "/api/console/workspaces")[1]["workspaces"]] == ["dev"]
    assert c.call("GET", "/api/console/workspaces/prod/agents")[0] == 403
    assert c.call("POST", "/api/console/workspaces", {"id": "x1", "accountId": ACCOUNT, "region": "us-west-2", "profile": "p"})[0] == 403  # admins only
    assert c.call("GET", "/api/console/workspaces/dev/agents")[0] == 200


def test_api_keys_open_the_public_api_scoped_to_a_workspace(console):
    c = console["client"]
    c.call("POST", "/api/console/workspaces", {"id": "dev", "accountId": ACCOUNT, "region": "us-west-2", "profile": "default"})
    status, key = c.call("POST", "/api/console/keys", {"workspace": "dev", "agent": "hr_agent", "label": "crm"})
    assert status == 201 and key["key"].startswith("adlc_live_") and "hash" not in key
    assert c.call("GET", "/v1/agents")[0] == 401
    status, listed = c.call("GET", "/v1/agents", headers={"X-Api-Key": key["key"]})
    assert status == 200 and listed["agents"] == [{"name": "hr_agent", "kind": "harness", "status": "READY"}]
    status, answer = c.call("POST", "/v1/chat", {"agent": "hr_agent", "message": "Leave?"}, headers={"X-Api-Key": key["key"]})
    assert status == 200 and answer["text"] == "Annual leave is 15 days." and answer["tools"] == ["hrtools___retrieve_policy"]
    assert c.call("POST", "/v1/chat", {"agent": "byoc_agent", "message": "hi"}, headers={"X-Api-Key": key["key"]})[0] == 403  # one agent only
    c.call("DELETE", f"/api/console/keys/{key['id']}")
    assert c.call("GET", "/v1/agents", headers={"X-Api-Key": key["key"]})[0] == 401


def test_observability_reads_metrics_sessions_and_a_trace(tmp_path):
    """engine console.observability on fake CloudWatch and Logs."""
    import sys
    sys.path.insert(0, str(REPO / "engine"))
    from datetime import datetime, timezone

    from workshop_customizer.console import observability as obs

    class CW:
        def get_paginator(self, name):
            def paginate(**kw):
                if kw["Namespace"] == obs.NAMESPACE and kw["MetricName"] in ("Invocations", "Latency"):
                    dims = [{"Name": "HarnessId", "Value": "h-1"}]
                    return [{"Metrics": [{"Namespace": obs.NAMESPACE, "MetricName": kw["MetricName"], "Dimensions": dims + [{"Name": "Operation", "Value": "x"}]},
                                         {"Namespace": obs.NAMESPACE, "MetricName": kw["MetricName"], "Dimensions": dims}]}]
                return [{"Metrics": []}]
            return type("P", (), {"paginate": staticmethod(paginate)})()

        def get_metric_data(self, MetricDataQueries, **kw):
            assert all(len(q["MetricStat"]["Metric"]["Dimensions"]) == 1 for q in MetricDataQueries)  # the aggregate dimension set
            t = datetime(2026, 10, 1, tzinfo=timezone.utc)
            return {"MetricDataResults": [{"Id": q["Id"], "Timestamps": [t, t], "Values": [3.0, 5.0]} for q in MetricDataQueries]}

    class Logs:
        def start_query(self, **kw):
            assert "attributes.session.id" in kw["queryString"]
            return {"queryId": "q"}

        def get_query_results(self, queryId):
            return {"status": "Complete", "results": [[{"field": "sid", "value": "s-1"}, {"field": "records", "value": "6"}, {"field": "last", "value": "t"}]]}

    session = type("S", (), {"client": lambda self, name, region_name=None, **kw: {"cloudwatch": CW(), "logs": Logs()}[name]})()
    agent = {"kind": "harness", "id": "h-1", "name": "hr", "runtimeId": "harness_hr-x"}
    m = obs.metrics(session, "us-west-2", agent, 24)
    assert m["series"]["Invocations"]["total"] == 8.0 and m["series"]["Latency"]["total"] == 4.0 and set(m["series"]) == {"Invocations", "Latency"}
    assert obs.sessions(session, "us-west-2", agent)["sessions"] == [{"sessionId": "s-1", "records": 6, "first": None, "last": "t", "scores": {}}]
    assert obs._text('[{"text": "Leave is 15 days."}]') == "Leave is 15 days." and obs._text({"content": "plain"}) == "plain"


def test_a_console_harness_role_covers_the_memory_the_service_creates_with_it():
    from workshop_customizer.console import agents

    policy = agents.role_policy(account="111122223333", region="us-west-2", gateways=[], memory_arn=None, skill_buckets=[], harness_name="hr_agent")
    memory = next(s for s in policy["Statement"] if s["Sid"] == "Memory")
    # live 2026-10-01: a Harness created without memory gets memory/<name>-<suffix>, and its first turn calls ListEvents on it
    assert memory["Resource"] == ["arn:aws:bedrock-agentcore:us-west-2:111122223333:memory/hr_agent-*"]
    assert "bedrock-agentcore:ListEvents" in memory["Action"] and "bedrock-agentcore:CreateEvent" in memory["Action"]
    given = agents.role_policy(account="111122223333", region="us-west-2", gateways=[], memory_arn="arn:m", skill_buckets=[], harness_name="hr_agent")
    assert next(s for s in given["Statement"] if s["Sid"] == "Memory")["Resource"][0] == "arn:m"


def _spoke_template():
    import re as _re

    import yaml

    class Loader(yaml.SafeLoader):
        pass

    ctx = {"AWS::Partition": "aws", "AWS::AccountId": "111122223333", "AWS::Region": "us-west-2", "RoleName": "adlc-console-spoke"}
    Loader.add_constructor("!Sub", lambda loader, node: _re.sub(r"\$\{([^}]+)\}", lambda m: ctx[m.group(1)], loader.construct_scalar(node)))
    Loader.add_constructor("!Ref", lambda loader, node: {"Ref": loader.construct_scalar(node)})
    Loader.add_constructor("!GetAtt", lambda loader, node: {"GetAtt": loader.construct_scalar(node)})
    return yaml.load((REPO / "app" / "console" / "spoke-role.yaml").read_text(encoding="utf-8"), Loader=Loader)


def _spoke_policy():
    role = _spoke_template()["Resources"]["SpokeRole"]["Properties"]
    return role, {s["Sid"]: s for s in role["Policies"][0]["PolicyDocument"]["Statement"]}


def _listed(value):
    return value if isinstance(value, list) else [value]


def _allows(statements, action, resource=None):
    """Whether an Allow statement (no Deny is in a boundary) names ``action`` and, when given, a pattern that covers
    ``resource`` (an identity policy's own wildcard resource is covered when the boundary allows the action at all:
    the boundary narrows it on purpose)."""
    from fnmatch import fnmatchcase

    for s in statements:
        if s["Effect"] != "Allow" or not any(fnmatchcase(action, a) for a in _listed(s["Action"])):
            continue
        if resource is None or "*" in resource or any(fnmatchcase(resource, r) for r in _listed(s["Resource"])):
            return True
    return False


def _concrete(action, resource, account="111122223333", region="us-west-2"):
    """A role policy's resource as what the role really uses ``action`` on: a pattern with its wildcards filled in (the
    console bucket's whole-bucket grant as a skill file, any function as a code evaluator), ``*`` as the ARN the action is
    called on, or None for an action the resource's type does not take (GetObject on a bucket, ListBucket on an object)."""
    bucket = f"adlc-console-{account}-{region}"
    if resource.startswith("arn:aws:s3:::"):
        key = resource[len("arn:aws:s3:::"):]
        if (action in ("s3:GetObject", "s3:GetObjectVersion", "s3:PutObject") and "/" not in key) or (action == "s3:ListBucket" and "/" in key):
            return None
    if resource == "*":
        return {"bedrock-agentcore": f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/g1",
                "logs": f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/r1-DEFAULT:*"}.get(action.split(":")[0], "*")
    samples = {f"arn:aws:s3:::{bucket}/*": f"arn:aws:s3:::{bucket}/skills/reply-style/v0001/SKILL.md",
               f"arn:aws:lambda:{region}:{account}:function:*": f"arn:aws:lambda:{region}:{account}:function:hrassistant-eval-thelma_rag_quality"}
    return samples.get(resource, resource.replace("*", "x"))


def _console_role_policies():
    """Every inline policy a console module writes to a role it creates (or grants to an agent's role), as it builds it."""
    from workshop_customizer.console import agents, deploy, experiments, governance, kb, runtime_canary, skills_lab
    from workshop_customizer.direct import online

    account, region = "111122223333", "us-west-2"
    bucket = kb.console_bucket(account, region)
    gateway = f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/adlc-console-kb-gateway-abcdefghij"
    harness = f"arn:aws:bedrock-agentcore:{region}:{account}:harness/hr_agent-AbCdEfGhIj"
    runtime = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/faq_bot-AbCdEf1234"
    yield "Harness role", agents.role_policy(account=account, region=region, gateways=[gateway], skill_buckets=[bucket], harness_name="hr_agent",
                                             memory_arn=f"arn:aws:bedrock-agentcore:{region}:{account}:memory/hr_agent-MeMeMeMeMe")
    yield "Harness role: the KB Gateway grant", {"Statement": [{"Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway", "Resource": gateway}]}
    yield "Harness / runtime role: the skill library", skills_lab.read_policy(bucket)
    yield "Claude Agent SDK runtime role: the skill library and Skill Lab's task files", skills_lab.runtime_read_policy(bucket)
    yield "KB role", kb._s3_read(bucket, "kb/")
    yield "KB Gateway role", kb.gateway_role_policy(account, region, ["KB12345678"])
    yield "runtime role", deploy.runtime_role_policy(account=account, region=region, knowledge_bases=["KB12345678"],
                                                     repository_arn=f"arn:aws:ecr:{region}:{account}:repository/adlc-console/faq-bot")
    yield "build role", deploy.build_role_policy(account=account, region=region, bucket=bucket)
    yield "online-evaluation role", online.role_documents(account, region)[1]
    yield "A/B test role", experiments.role_documents(account, region, [harness, f"{harness}x"])[1]
    yield "canary role", runtime_canary.role_documents(account, region, [runtime, f"{runtime}x"])[1]
    yield "Gateway role: a policy engine", governance.role_statement(f"arn:aws:bedrock-agentcore:{region}:{account}:policy-engine/pe-abcdefghij", gateway)


def test_the_spoke_role_trusts_only_the_hub_with_its_external_id_and_covers_every_name_the_console_creates():
    from fnmatch import fnmatch

    from workshop_customizer.console import agents, deploy, experiments, kb, runtime_canary

    role, sids = _spoke_policy()
    [trust] = role["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Principal"] == {"AWS": {"Ref": "HubPrincipalArn"}} and trust["Condition"] == {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}}
    path = "arn:aws:iam::111122223333:role/adlc-console/*"  # the console's own IAM path: the only roles the spoke creates, tags or passes
    assert agents.ROLE_PATH == "/adlc-console/" and sids["ConsoleRoles"]["Resource"] == path
    names = deploy.Names()
    made = [agents.role_name("hr_agent"), kb.KB_ROLE, kb.GATEWAY_ROLE, names.build_role("us-west-2"), names.runtime_role("faq_bot"),
            "hr_agent-online-eval", experiments.names("1a2b3c4d", "hr_agent")["role"], runtime_canary.names("1a2b3c4d", "faq_bot")["role"]]
    for name in made:  # every role a module creates or edits (the A/B test's role too: adlc-console-exp-<id>), on the path only
        assert fnmatch(f"arn:aws:iam::111122223333:role{agents.ROLE_PATH}{name}", path), name
        assert not fnmatch(f"arn:aws:iam::111122223333:role/{name}", path), name  # the same name off the path: not the console's
    for sid in ("ConsoleRoleReads", "ConsoleRoleTags", "ConsoleRoleTrust", "PassConsoleRoles"):
        assert sids[sid]["Resource"] == path, sid
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    for s in statements:  # every role statement is held to the path; only reads and the tag-and-boundary grant reach a role elsewhere
        actions = {a for a in _listed(s["Action"]) if a.startswith("iam:")}
        if s["Effect"] != "Allow" or not actions or actions <= {"iam:GetRole", "iam:ListRoleTags"}:
            continue
        if s["Sid"] == "TaggedAgentRoles":
            assert actions == {"iam:PutRolePolicy", "iam:DeleteRolePolicy"}, actions
            continue
        assert s["Resource"] == path, s["Sid"]
    allowed = [r for s in statements if s["Effect"] == "Allow" for r in _listed(s["Resource"]) if isinstance(r, str) and ":role/" in r]
    assert set(allowed) == {path, "arn:aws:iam::111122223333:role/*"}, allowed  # no name patterns left
    assert sids["NotItself"]["Effect"] == "Deny"
    assert sids["TaggedAgentRoles"]["Condition"] == {"StringEquals": {"iam:ResourceTag/adlc:console": "1", "iam:PermissionsBoundary": {"Ref": "ConsoleRoleBoundary"}}}
    bucket = kb.console_bucket("111122223333", "us-west-2")
    assert sids["ConsoleBucket"]["Resource"] == [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]
    assert fnmatch(f"arn:aws:codebuild:us-west-2:111122223333:project/{names.build_project}", sids["Builds"]["Resource"])
    assert fnmatch(f"arn:aws:ecr:us-west-2:111122223333:repository/{names.repository('faq_bot')}", sids["Images"]["Resource"])
    assert "ecr:BatchDeleteImage" in sids["Images"]["Action"]  # a canary's cleanup deletes the image its own build pushed
    assert fnmatch("arn:aws:logs:us-west-2:111122223333:log-group:/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/gw-1",
                   sids["DecisionLogGroups"]["Resource"])
    arm = "arn:aws:logs:us-west-2:111122223333:log-group:/aws/bedrock-agentcore/runtimes/faq_bot-AbCdEf1234-adlc_console_can_1a2b3c4d_c"
    results = "arn:aws:logs:us-west-2:111122223333:log-group:/aws/bedrock-agentcore/evaluations/results/x-OeOeOeOeOe"
    groups = sids["ConsoleLogGroups"]
    assert {"logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:TagResource"} <= set(groups["Action"])  # an arm's group, made before its evaluation
    assert any(fnmatch(arm, r) for r in groups["Resource"]) and any(fnmatch(results, r) for r in groups["Resource"])


def test_every_role_the_spoke_role_creates_carries_the_boundary_and_the_boundary_holds_every_console_role_policy():
    doc = _spoke_template()
    role, sids = _spoke_policy()
    boundary = doc["Resources"]["ConsoleRoleBoundary"]
    assert boundary["Type"] == "AWS::IAM::ManagedPolicy" and boundary["Properties"]["ManagedPolicyName"] == "adlc-console-spoke-boundary"
    assert doc["Outputs"]["PermissionsBoundaryArn"]["Value"] == {"Ref": "ConsoleRoleBoundary"}
    ours = {"StringEquals": {"iam:PermissionsBoundary": {"Ref": "ConsoleRoleBoundary"}}}
    assert sids["ConsoleRoles"]["Condition"] == ours
    assert {"iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:DeleteRolePolicy", "iam:DetachRolePolicy"} <= set(sids["ConsoleRoles"]["Action"])
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    changing = {"iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:DeleteRolePolicy", "iam:DetachRolePolicy", "iam:PutRolePermissionsBoundary",
                "iam:UpdateAssumeRolePolicy", "iam:DeleteRole"}  # every action whose request carries iam:PermissionsBoundary (AWS's service reference)
    for s in statements:  # no Allow changes a role's permissions unless the role carries (or is created with) the boundary
        if s["Effect"] == "Allow" and changing & set(_listed(s["Action"])):
            assert s["Condition"]["StringEquals"]["iam:PermissionsBoundary"] == {"Ref": "ConsoleRoleBoundary"}, s["Sid"]
        assert not (s["Effect"] == "Allow" and "iam:*" in _listed(s["Action"])), s["Sid"]
    # PassRole and TagRole are the role actions whose requests have no iam:PermissionsBoundary (AWS's service reference,
    # checked 2026-10-02): both are held to the console's path (PassRole also to its tag and the services), so a role someone
    # else made off the path can never be tagged adlc:console=1 and then passed; a trust change or a delete needs the boundary too
    path = "arn:aws:iam::111122223333:role/adlc-console/*"
    for sid in ("ConsoleRoleTrust", "PassConsoleRoles"):
        assert sids[sid]["Condition"]["StringEquals"]["iam:ResourceTag/adlc:console"] == "1"
    assert "iam:PermissionsBoundary" not in json.dumps(sids["PassConsoleRoles"]["Condition"])
    unbounded = {s["Sid"] for s in statements if s["Effect"] == "Allow" and {"iam:PassRole", "iam:TagRole"} & set(_listed(s["Action"]))}
    assert unbounded == {"PassConsoleRoles", "ConsoleRoleTags"} and all(sids[sid]["Resource"] == path for sid in unbounded)
    assert sids["ConsoleRoleTrust"]["Condition"]["StringEquals"]["iam:PermissionsBoundary"] == {"Ref": "ConsoleRoleBoundary"}
    assert sids["KeepTheBoundary"] == {"Sid": "KeepTheBoundary", "Effect": "Deny", "Action": "iam:DeleteRolePermissionsBoundary",
                                       "Resource": "arn:aws:iam::111122223333:role/*"}
    assert sids["OnlyThisBoundary"]["Effect"] == "Deny" and sids["OnlyThisBoundary"]["Condition"] == {
        "StringNotEquals": {"iam:PermissionsBoundary": {"Ref": "ConsoleRoleBoundary"}}}
    assert sids["BoundaryUnchanged"]["Effect"] == "Deny" and sids["BoundaryUnchanged"]["Resource"] == {"Ref": "ConsoleRoleBoundary"}
    assert {"iam:CreatePolicyVersion", "iam:DeletePolicy"} <= set(sids["BoundaryUnchanged"]["Action"])

    caps = boundary["Properties"]["PolicyDocument"]["Statement"]
    assert all(s["Effect"] == "Allow" and "NotAction" not in s and "NotResource" not in s for s in caps)
    granted = {a for s in caps for a in _listed(s["Action"])}
    assert not any(a.split(":")[0] in ("iam", "sts", "codebuild", "organizations") or a.endswith(":*") for a in granted), granted
    used = set()
    for what, policy in _console_role_policies():
        for s in policy["Statement"]:
            for action in _listed(s["Action"]):  # every resource, as the concrete ARN the role uses the action on (no wildcard passes)
                used.add(action)
                for r in _listed(s["Resource"]):
                    c = _concrete(action, r)
                    assert c is None or _allows(caps, action, c), f"{what}: {action} on {r} ({c}) is outside the boundary"
    assert granted <= used, f"the boundary grants what no console role uses: {sorted(granted - used)}"
    # the console's own S3 and images only (a source or image elsewhere needs the account's owner to widen the boundary)
    assert _allows(caps, "s3:GetObject", "arn:aws:s3:::adlc-console-111122223333-us-west-2/kb/KB12345678/a.pdf")
    assert _allows(caps, "s3:GetObject", "arn:aws:s3:::adlc-console-111122223333-us-west-2/skill-lab/assets/0f3a9c")  # a Skill Lab task's file
    assert not _allows(caps, "s3:GetObject", "arn:aws:s3:::payroll-exports/2026/q3.csv")
    assert not _allows(caps, "s3:PutObject", "arn:aws:s3:::adlc-console-111122223333-us-west-2/kb/x")
    assert not _allows(caps, "ecr:PutImage", "arn:aws:ecr:us-west-2:111122223333:repository/payments/api")
    assert not _allows(caps, "lambda:InvokeFunction", "arn:aws:lambda:us-west-2:111122223333:function:payroll-run")
    assert _allows(caps, "lambda:InvokeFunction", "arn:aws:lambda:us-west-2:111122223333:function:hrassistant-eval-thelma_rag_quality")
    import re as _re  # IAM's quotas, white space not counted: a role's inline policies 10 240 characters, a managed policy 6 144

    size = lambda d: len(_re.sub(r"\s", "", json.dumps(d, separators=(",", ":"))))  # noqa: E731
    assert size(role["Policies"][0]["PolicyDocument"]) < 10_240 - 500 and size(boundary["Properties"]["PolicyDocument"]) < 6_144 - 500
    assert len(doc["Description"].encode("utf-8")) <= 1024  # CloudFormation refuses a longer template description


def test_a_role_this_workspace_may_not_change_is_refused_with_what_to_add():
    from workshop_customizer.console import agents

    class Iam:
        def put_role_policy(self, **kw):
            raise RuntimeError("An error occurred (AccessDenied) when calling the PutRolePolicy operation")

    session = type("S", (), {"client": lambda self, name, region_name=None, **kw: Iam()})()
    with pytest.raises(agents.AgentError, match="tag it adlc:console=1.*adlc-console-skills.*s3:GetObject"):
        agents.put_inline_policy(session, "arn:aws:iam::1:role/their-role", "adlc-console-skills",
                                 {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}]}, what="skills")


# -- the console's IAM path (/adlc-console/) ---------------------------------------------------------------------------------

SPOKE_BOUNDARY = f"arn:aws:iam::{ACCOUNT}:policy/adlc-console-spoke-boundary"
CONSOLE_PATH = "/adlc-console/"


class PathIam:
    """IAM with role paths (a role name is unique in its account whatever its path). With ``spoke`` it refuses what
    app/console/spoke-role.yaml refuses: creating a role off the console's path or without the boundary; tagging one off
    the path; changing a role's policies unless it is on the path with the boundary (or, Put/DeleteRolePolicy only, tagged
    adlc:console=1 with the boundary anywhere); putting the boundary off the path; a trust change or a delete unless on
    the path, tagged and bounded. Every call that would change a role is recorded, refused or not."""

    def __init__(self, roles=None, *, spoke=False):
        self.roles = {name: {"path": "/", "tags": {}, "boundary": None, "policies": {}, **r} for name, r in (roles or {}).items()}
        self.calls, self.spoke = [], spoke

    def _role(self, name):
        if name not in self.roles:
            raise RuntimeError(f"An error occurred (NoSuchEntity) when calling the GetRole operation: no role {name}")
        return self.roles[name]

    def _arn(self, name):
        return f"arn:aws:iam::{ACCOUNT}:role{self.roles[name]['path']}{name}"

    def _change(self, op, name, allowed):
        self.calls.append((op, name))
        if self.spoke and not allowed(self._role(name)):
            raise RuntimeError(f"An error occurred (AccessDenied) when calling the {op} operation: not authorized")

    @staticmethod
    def _ours(r):
        return r["path"] == CONSOLE_PATH and r["boundary"] == SPOKE_BOUNDARY

    def get_role(self, RoleName):
        r = self._role(RoleName)
        out = {"RoleName": RoleName, "Path": r["path"], "Arn": self._arn(RoleName), "AssumeRolePolicyDocument": {}}
        if r["boundary"]:
            out["PermissionsBoundary"] = {"PermissionsBoundaryType": "Policy", "PermissionsBoundaryArn": r["boundary"]}
        return {"Role": out}

    def list_role_tags(self, RoleName):
        return {"Tags": [{"Key": k, "Value": v} for k, v in self._role(RoleName)["tags"].items()]}

    def create_role(self, RoleName, AssumeRolePolicyDocument, Path="/", Tags=(), PermissionsBoundary=None, Description=None):
        self.calls.append(("create_role", RoleName))
        if self.spoke and (Path != CONSOLE_PATH or PermissionsBoundary != SPOKE_BOUNDARY):
            raise RuntimeError("An error occurred (AccessDenied) when calling the CreateRole operation: not authorized")
        if RoleName in self.roles:
            raise RuntimeError(f"An error occurred (EntityAlreadyExists) when calling the CreateRole operation: Role with name {RoleName} already exists.")
        self.roles[RoleName] = {"path": Path, "tags": {t["Key"]: t["Value"] for t in Tags}, "boundary": PermissionsBoundary, "policies": {}}
        return {"Role": {"RoleName": RoleName, "Path": Path, "Arn": self._arn(RoleName)}}

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self._change("put_role_policy", RoleName, lambda r: self._ours(r) or (r["tags"].get("adlc:console") == "1" and r["boundary"] == SPOKE_BOUNDARY))
        self._role(RoleName)["policies"][PolicyName] = json.loads(PolicyDocument)

    def delete_role_policy(self, RoleName, PolicyName):
        self._change("delete_role_policy", RoleName, lambda r: self._ours(r) or (r["tags"].get("adlc:console") == "1" and r["boundary"] == SPOKE_BOUNDARY))
        self._role(RoleName)["policies"].pop(PolicyName)

    def attach_role_policy(self, RoleName, PolicyArn):
        self._change("attach_role_policy", RoleName, self._ours)

    def put_role_permissions_boundary(self, RoleName, PermissionsBoundary):
        self._change("put_role_permissions_boundary", RoleName, lambda r: r["path"] == CONSOLE_PATH and PermissionsBoundary == SPOKE_BOUNDARY)
        self._role(RoleName)["boundary"] = PermissionsBoundary

    def tag_role(self, RoleName, Tags):
        self._change("tag_role", RoleName, lambda r: r["path"] == CONSOLE_PATH)
        self._role(RoleName)["tags"].update({t["Key"]: t["Value"] for t in Tags})

    def list_role_policies(self, RoleName):
        return {"PolicyNames": list(self._role(RoleName)["policies"])}

    def delete_role(self, RoleName):
        self._change("delete_role", RoleName, lambda r: self._ours(r) and r["tags"].get("adlc:console") == "1")
        del self.roles[RoleName]

    def changed(self, name=None):
        return [op for op, n in self.calls if name is None or n == name]


def _iam_session(iam, **others):
    return type("S", (), {"client": lambda self, name, region_name=None, **kw: iam if name == "iam" else others[name]})()


def test_every_module_creates_its_role_on_the_console_path_and_hands_aws_the_arn_iam_gives(monkeypatch):
    from workshop_customizer.console import agents, experiments, kb
    from workshop_customizer.direct import online

    monkeypatch.setattr(agents.time, "sleep", lambda s: None)
    monkeypatch.setattr(kb.time, "sleep", lambda s: None)

    class Ctl:
        def create_online_evaluation_config(self, **kw):
            self.request = kw
            return {"onlineEvaluationConfigId": "c-1", "onlineEvaluationConfigArn": "arn:c-1"}

    planned = {"request": {"tags": dict(agents.CONSOLE_TAG), "dataSourceConfig": {"cloudWatchLogs": {"logGroupNames": ["/aws/bedrock-agentcore/runtimes/x-DEFAULT"]}},
                           "evaluators": [{"evaluatorId": "Builtin.Correctness"}], "rule": {"samplingConfig": {"samplingPercentage": 10.0}},
                           "evaluationExecutionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/hr_agent-online-eval"}}  # a stale plan's guess: replaced
    for boundary in (None, SPOKE_BOUNDARY):  # a profile workspace and a spoke one alike
        iam, ctl = PathIam(spoke=bool(boundary)), Ctl()
        session = _iam_session(iam, **{"bedrock-agentcore-control": ctl})
        arns = {"harness": agents._ensure_role(session, "hr_agent-console-harness", ACCOUNT, "us-west-2", {"Statement": []}, boundary=boundary),
                "kb": kb.ensure_kb_role(session, ACCOUNT, "us-west-2", boundary),
                "gateway": kb._ensure_role(session, kb.GATEWAY_ROLE, {}, "kb-retrieve", {"Statement": []}, boundary=boundary),
                "experiment": experiments._ensure_role(iam, "adlc-console-exp-1a2b3c4d", {}, {"Statement": []}, dict(agents.CONSOLE_TAG), boundary),
                "online": online.apply_plan(session, account=ACCOUNT, region="us-west-2", planned=planned, role="hr_agent-online-eval", boundary=boundary,
                                            path=agents.ROLE_PATH, sleep=lambda s: None)["roleArn"]}
        root = f"arn:aws:iam::{ACCOUNT}:role/adlc-console/"
        assert arns == {"harness": root + "hr_agent-console-harness", "kb": root + kb.KB_ROLE, "gateway": root + kb.GATEWAY_ROLE,
                        "experiment": root + "adlc-console-exp-1a2b3c4d", "online": root + "hr_agent-online-eval"}
        assert ctl.request["evaluationExecutionRoleArn"] == root + "hr_agent-online-eval"  # the configuration gets IAM's ARN, path included
        assert {(r["path"], r["boundary"], r["tags"].get("adlc:console")) for r in iam.roles.values()} == {(CONSOLE_PATH, boundary, "1")}


def test_a_same_name_role_off_the_console_path_is_refused_in_a_spoke_workspace_and_never_changed(monkeypatch):
    from workshop_customizer.console import agents, evaluation, experiments, kb
    from workshop_customizer.direct import online

    monkeypatch.setattr(agents.time, "sleep", lambda s: None)
    legacy = {"tags": {"adlc:console": "1"}, "boundary": SPOKE_BOUNDARY}  # a console role made before roles had a path (on /)
    foreign = {"tags": {"adlc:probe": "foreign"}}  # someone else's role whose name happens to be the console's
    elsewhere = {"tags": {"adlc:console": "1"}, "path": "/team/"}
    iam = PathIam({"hr_agent-console-harness": legacy, kb.KB_ROLE: foreign, kb.GATEWAY_ROLE: elsewhere, "adlc-console-exp-1a2b3c4d": legacy,
                   "hr_agent-online-eval": foreign, "faq_bot-online-eval": legacy}, spoke=True)
    session = _iam_session(iam, **{"bedrock-agentcore-control": object()})  # (the deploy module's refusals: test_console_deploy)
    with pytest.raises(agents.AgentError, match="on the path /, not the console's /adlc-console/"):
        agents._ensure_role(session, "hr_agent-console-harness", ACCOUNT, "us-west-2", {"Statement": []}, boundary=SPOKE_BOUNDARY)
    with pytest.raises(kb.KnowledgeError, match="not tagged adlc:console=1"):
        kb.ensure_kb_role(session, ACCOUNT, "us-west-2", SPOKE_BOUNDARY)
    with pytest.raises(kb.KnowledgeError, match="not tagged adlc:console=1"):  # a source's grant goes onto the console's KB role only
        kb.create_data_source(_iam_session(iam, **{"bedrock-agent": type("A", (), {"list_data_sources": lambda _s, **kw: {}})()}), ACCOUNT,
                              "us-west-2", "KB12345678", {"mode": "s3", "bucket": "team-docs"}, SPOKE_BOUNDARY)
    with pytest.raises(kb.KnowledgeError, match="on the path /team/"):
        kb._ensure_role(session, kb.GATEWAY_ROLE, {}, "kb-retrieve", {"Statement": []}, boundary=SPOKE_BOUNDARY)
    with pytest.raises(experiments.ExperimentError, match="on the path /"):
        experiments._ensure_role(iam, "adlc-console-exp-1a2b3c4d", {}, {"Statement": []}, dict(agents.CONSOLE_TAG), SPOKE_BOUNDARY)
    rule = evaluation._console_rule(SPOKE_BOUNDARY)
    with pytest.raises(ValueError, match="not tagged adlc:console=1"):
        online.apply_plan(session, account=ACCOUNT, region="us-west-2", planned={"request": {"tags": {}}}, role="hr_agent-online-eval",
                          boundary=SPOKE_BOUNDARY, path=agents.ROLE_PATH, refusal=rule)
    with pytest.raises(evaluation.EvaluationError, match="on the path /"):  # refused when planned, before anything is shown as the plan
        evaluation.online_role_arn(session, ACCOUNT, "faq_bot-online-eval", SPOKE_BOUNDARY)
    left: list[str] = []  # deleting leaves it too, and says why
    assert online.delete_config(session, region="us-west-2", config_id=None, role="faq_bot-online-eval", boundary=SPOKE_BOUNDARY, refusal=rule,
                                left=left) == [] and "on the path /" in left[0]
    # the experiment's role is tried as a new one first (CreateRole on the path, which the spoke allows: EntityAlreadyExists), nothing else
    assert iam.changed() == ["create_role"] and set(iam.roles) == {"hr_agent-console-harness", kb.KB_ROLE, kb.GATEWAY_ROLE,
                                                                    "adlc-console-exp-1a2b3c4d", "hr_agent-online-eval", "faq_bot-online-eval"}
    assert all(not r["policies"] for r in iam.roles.values()) and iam.roles["hr_agent-online-eval"]["tags"] == {"adlc:probe": "foreign"}


def test_a_tagged_console_role_on_root_is_still_adopted_in_a_profile_workspace_and_an_untagged_one_never(monkeypatch):
    from workshop_customizer.console import agents, evaluation, experiments, kb
    from workshop_customizer.direct import online

    monkeypatch.setattr(agents.time, "sleep", lambda s: None)
    legacy = {"tags": {"adlc:console": "1"}}  # e.g. adlc_kb_demo_agent-console-harness, adlc-console-kb-gateway, adlc-console-kb-role
    iam = PathIam({"adlc_kb_demo_agent-console-harness": legacy, kb.KB_ROLE: legacy, kb.GATEWAY_ROLE: legacy, "adlc-console-exp-1a2b3c4d": legacy,
                   "hr_agent-online-eval": legacy, "their_agent-console-harness": {"tags": {"team": "theirs"}}})
    session = _iam_session(iam)
    root = f"arn:aws:iam::{ACCOUNT}:role/"
    assert agents._ensure_role(session, "adlc_kb_demo_agent-console-harness", ACCOUNT, "us-west-2", {"Statement": []}) == root + "adlc_kb_demo_agent-console-harness"
    assert kb.ensure_kb_role(session, ACCOUNT, "us-west-2") == root + kb.KB_ROLE
    assert kb._ensure_role(session, kb.GATEWAY_ROLE, {}, "kb-retrieve", {"Statement": []}) == root + kb.GATEWAY_ROLE
    assert experiments._ensure_role(iam, "adlc-console-exp-1a2b3c4d", {}, {"Statement": []}, dict(agents.CONSOLE_TAG)) == root + "adlc-console-exp-1a2b3c4d"
    assert evaluation.online_role_arn(session, ACCOUNT, "hr_agent-online-eval", None) == root + "hr_agent-online-eval"  # IAM's own ARN, no path
    assert evaluation.online_role_arn(session, ACCOUNT, "new_agent-online-eval", None) == root + "adlc-console/new_agent-online-eval"  # the one it gets
    assert all(r["policies"] for name, r in iam.roles.items() if name != "hr_agent-online-eval" and r["tags"].get("adlc:console") == "1")
    with pytest.raises(agents.AgentError, match="not tagged adlc:console=1"):  # a profile workspace takes over no one else's role either
        agents._ensure_role(session, "their_agent-console-harness", ACCOUNT, "us-west-2", {"Statement": []})
    assert not iam.roles["their_agent-console-harness"]["policies"] and {r["path"] for r in iam.roles.values()} == {"/"}
    left: list[str] = []
    assert online.delete_config(session, region="us-west-2", config_id=None, role="hr_agent-online-eval", refusal=evaluation._console_rule(None),
                                left=left) == ["hr_agent-online-eval"] and not left and "hr_agent-online-eval" not in iam.roles


def test_an_online_evaluation_is_planned_created_and_deleted_with_its_roles_arn_on_the_console_path(tmp_path):
    from workshop_customizer.console import evaluation
    from workshop_customizer.console.store import Store

    panel = {"references": False, "recommendation": [{"evaluator": "Builtin.Correctness", "recommended": True, "sees": ["wrong facts"]}], "unseen": []}
    job = {"id": "verify-0123456789", "workspace": "spoke", "kind": "verify",
           "result": {"panel": {"online": panel}, "harness": {"name": "hr_agent", "runtimeId": "harness_hr_agent-AbCdEf1234"}}}

    class Ctl:
        def __init__(self):
            self.created, self.deleted = [], []

        def list_evaluators(self):
            return {"evaluators": [{"evaluatorId": "Builtin.Correctness"}]}

        def create_online_evaluation_config(self, **kw):
            self.created.append(kw)
            return {"onlineEvaluationConfigId": "c-1", "onlineEvaluationConfigArn": "arn:c-1"}

        def delete_online_evaluation_config(self, onlineEvaluationConfigId):
            self.deleted.append(onlineEvaluationConfigId)

    iam, ctl = PathIam(spoke=True), Ctl()
    session = _iam_session(iam, **{"bedrock-agentcore-control": ctl})
    ws = {"id": "spoke", "accountId": ACCOUNT, "region": "us-west-2", "permissionsBoundaryArn": SPOKE_BOUNDARY}
    console = type("C", (), {"store": Store(tmp_path), "jobs": type("J", (), {"get": lambda _s, jid: job})(),
                             "workspaces": type("W", (), {"get": lambda _s, wid: ws, "session": lambda _s, wid: session})()})()
    arn = f"arn:aws:iam::{ACCOUNT}:role/adlc-console/hr_agent-online-eval"
    assert evaluation.plan_online(console, "spoke", job["id"], {})["request"]["evaluationExecutionRoleArn"] == arn and not iam.calls  # a plan changes nothing
    record = evaluation.create_online(console, "spoke", job["id"], {"acknowledged": True})
    assert ctl.created[0]["evaluationExecutionRoleArn"] == record["roleArn"] == arn  # IAM's own ARN of the role it made
    made = iam.roles["hr_agent-online-eval"]
    assert (made["path"], made["boundary"], made["tags"]) == (CONSOLE_PATH, SPOKE_BOUNDARY, {"adlc:console": "1"}) and made["policies"]
    assert evaluation.delete_online(console, "spoke", "c-1") == {"deleted": ["c-1", "hr_agent-online-eval"]} and not iam.roles


def test_a_role_given_to_run_an_agent_in_a_spoke_workspace_is_the_consoles_own_or_refused_before_anything_is_created():
    from workshop_customizer.console import agents

    class Ctl:
        def __init__(self):
            self.created = []

        def create_harness(self, **kw):
            self.created.append(kw)
            return {"harness": {"harnessId": "h-1", "arn": "arn:h-1", "status": "CREATING"}}

    iam = PathIam({"adlc-console-probe-foreign": {"tags": {"adlc:probe": "foreign"}},  # someone else's, on /
                   "tagged-on-root": {"tags": {"adlc:console": "1"}, "boundary": SPOKE_BOUNDARY},
                   "elsewhere-console-harness": {"tags": {"adlc:console": "1"}, "path": "/team/", "boundary": SPOKE_BOUNDARY},
                   "unbounded-console-harness": {"tags": {"adlc:console": "1"}, "path": CONSOLE_PATH},  # made before the workspace had one
                   "hr_agent-console-harness": {"tags": {"adlc:console": "1"}, "path": CONSOLE_PATH, "boundary": SPOKE_BOUNDARY}}, spoke=True)
    ctl = Ctl()
    session = _iam_session(iam, **{"bedrock-agentcore-control": ctl})
    body = {"name": "adlc_probe_x", "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "systemPrompt": "Be brief."}
    for arn, why in ((f"arn:aws:iam::{ACCOUNT}:role/adlc-console-probe-foreign", "is on /, tagged adlc:console=\\(none\\)"),
                     (f"arn:aws:iam::{ACCOUNT}:role/tagged-on-root", "is on /, tagged adlc:console=1"),
                     (f"arn:aws:iam::{ACCOUNT}:role/adlc-console/elsewhere-console-harness", "is on /team/"),  # an ARN naming the path: IAM's decides
                     ("arn:aws:iam::999999999999:role/x", "not a role of this workspace's account")):
        with pytest.raises(agents.AgentError, match=why):
            agents.create_harness(session, account=ACCOUNT, region="us-west-2", body={**body, "executionRoleArn": arn}, boundary=SPOKE_BOUNDARY)
    assert not ctl.created and not iam.changed()  # nothing was created, nothing changed
    out = agents.create_harness(session, account=ACCOUNT, region="us-west-2", boundary=SPOKE_BOUNDARY,
                                body={**body, "executionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/hr_agent-console-harness"})  # the path left out
    assert out["role"] == ctl.created[0]["executionRoleArn"] == f"arn:aws:iam::{ACCOUNT}:role/adlc-console/hr_agent-console-harness"
    assert not iam.changed()
    agents.create_harness(session, account=ACCOUNT, region="us-west-2", boundary=SPOKE_BOUNDARY,
                          body={**body, "executionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/adlc-console/unbounded-console-harness"})
    assert iam.changed() == ["put_role_permissions_boundary"] and iam.roles["unbounded-console-harness"]["boundary"] == SPOKE_BOUNDARY  # bounded, then passed
    profile = agents.create_harness(_iam_session(None, **{"bedrock-agentcore-control": Ctl()}), account=ACCOUNT, region="us-west-2",
                                    body={**body, "executionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/their-role"})
    assert profile["role"] == f"arn:aws:iam::{ACCOUNT}:role/their-role"  # a profile workspace passes the role it is given, as given


def test_a_grant_onto_another_agents_role_on_the_console_path_is_made_by_its_name():
    from workshop_customizer.console import agents

    iam = PathIam({"hr_agent-console-harness": {"tags": {"adlc:console": "1"}, "path": CONSOLE_PATH, "boundary": SPOKE_BOUNDARY},
                   "team-agent-role": {"tags": {"adlc:console": "1"}, "path": "/service-role/", "boundary": SPOKE_BOUNDARY},
                   "their-role": {"tags": {"team": "theirs"}, "path": CONSOLE_PATH}}, spoke=True)
    session = _iam_session(iam)
    grant = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b/skills/*"}]}
    for arn in (f"arn:aws:iam::{ACCOUNT}:role/adlc-console/hr_agent-console-harness", f"arn:aws:iam::{ACCOUNT}:role/service-role/team-agent-role"):
        agents.put_inline_policy(session, arn, "adlc-console-skills", grant, what="skills", boundary=SPOKE_BOUNDARY)
    assert iam.roles["hr_agent-console-harness"]["policies"] == iam.roles["team-agent-role"]["policies"] == {"adlc-console-skills": grant}
    with pytest.raises(agents.AgentError, match="tag it adlc:console=1"):  # untagged and unbounded: the owner adds it
        agents.put_inline_policy(session, f"arn:aws:iam::{ACCOUNT}:role/adlc-console/their-role", "adlc-console-skills", grant, what="skills",
                                 boundary=SPOKE_BOUNDARY)
    assert not iam.roles["their-role"]["policies"]


def test_the_boundary_is_put_only_on_the_consoles_own_roles_never_on_another_teams_tagged_one():
    """review: in a profile workspace with a boundary set, a KB attach (acknowledged) onto another team's role its owner
    tagged adlc:console=1 put the console's boundary on it, capping it for good. Only a role on the console's path is
    given the boundary; the other gets the grant and keeps its permissions."""
    from workshop_customizer.console import agents

    iam = PathIam({"teamx-payroll-agent-role": {"tags": {"adlc:console": "1"}},  # tagged by its owner so the console may add grants
                   "team-svc-role": {"tags": {"adlc:console": "1"}, "path": "/service-role/"},
                   "old_agent-console-harness": {"tags": {"adlc:console": "1"}, "path": CONSOLE_PATH}})  # the console's own, made before
    session = _iam_session(iam)
    grant = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway", "Resource": "arn:gw"}]}
    for name in ("teamx-payroll-agent-role", "team-svc-role", "old_agent-console-harness"):
        agents.put_inline_policy(session, iam.get_role(RoleName=name)["Role"]["Arn"], "adlc-console-kb-gateway", grant, what="the knowledge-base Gateway",
                                 boundary=SPOKE_BOUNDARY)
    assert [n for op, n in iam.calls if op == "put_role_permissions_boundary"] == ["old_agent-console-harness"]
    assert iam.roles["teamx-payroll-agent-role"]["boundary"] is None and iam.roles["team-svc-role"]["boundary"] is None
    assert all(r["policies"] == {"adlc-console-kb-gateway": grant} for r in iam.roles.values())
    assert not agents.ensure_boundary(iam, "teamx-payroll-agent-role", SPOKE_BOUNDARY)


def test_the_hub_principal_is_the_role_behind_an_assumed_role_session():
    from workshop_customizer.console import api

    def console_with(arn: str, role: str | None):
        class Sts:
            def get_caller_identity(self):
                return {"Arn": arn}

        class Iam:
            def get_role(self, RoleName):
                if role is None:
                    raise RuntimeError("AccessDenied")
                return {"Role": {"Arn": role}}

        base = type("S", (), {"client": lambda self, name, **kw: Sts() if name == "sts" else Iam()})()
        return type("C", (), {"workspaces": type("W", (), {"_boto": lambda self, **kw: base})()})()

    sso = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_Admin_abc"
    assert api.hub_principal(console_with("arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_Admin_abc/me", sso)) == {"arn": sso, "exact": True}
    assert api.hub_principal(console_with("arn:aws:sts::111122223333:assumed-role/Ops/me", None)) == {
        "arn": "arn:aws:iam::111122223333:role/Ops", "exact": False}
    assert api.hub_principal(console_with("arn:aws:iam::111122223333:user/admin", None)) == {"arn": "arn:aws:iam::111122223333:user/admin", "exact": True}
    out = api.spoke_role(console_with("arn:aws:iam::111122223333:user/admin", None))
    assert "HubPrincipalArn=arn:aws:iam::111122223333:user/admin ExternalId=" + out["externalId"] in out["command"] and len(out["externalId"]) >= 16


def test_an_agent_in_a_running_experiment_is_chatted_with_through_its_gateway(console, monkeypatch):
    from workshop_customizer.console import experiments

    c = console["client"]
    c.call("POST", "/api/console/workspaces", {"id": "dev", "accountId": ACCOUNT, "region": "us-west-2", "profile": "default"})
    rec = {"id": "exp-0a1b2c3d", "workspace": "dev", "name": "hr_agent A/B", "status": "running", "weights": {"C": 75, "T1": 25},
           "agent": {"id": "hr_agent-1", "name": "hr_agent"}, "invokeUrl": "https://gw.example/adlc-exp-0a1b2c3d-c/invoke"}
    console["console"].store.write("experiments", {rec["id"]: rec, "exp-ffffffff": {**rec, "id": "exp-ffffffff", "workspace": "prod"}})
    sent = []
    monkeypatch.setattr(experiments, "post_signed", lambda session, region, url, data, headers, **kw: (sent.append((url, json.loads(data), headers)),
                                                                                                         (200, "application/vnd.amazon.eventstream", b""))[1])
    monkeypatch.setattr(experiments, "stream_events", lambda kind, raw: [
        ("contentBlockStart", {"contentBlockIndex": 0, "start": {"toolUse": {"name": "hrtools___retrieve_policy"}}}),
        ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"toolUse": {"input": '{"q": "leave"}'}}}), ("contentBlockStop", {"contentBlockIndex": 0}),
        ("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"text": "15 days."}}), ("messageStop", {"stopReason": "end_turn"}),
        ("metadata", {"usage": {"inputTokens": 50, "outputTokens": 4}})])
    status, text = c.call("POST", "/api/console/workspaces/dev/agents/harness/hr_agent-1/chat", {"message": "Leave?"})
    events = [json.loads(line[6:]) for line in text.split("\n") if line.startswith("data: ")]
    assert [e["type"] for e in events] == ["session", "experiment", "tool", "toolInput", "text", "turn", "stop"]
    assert events[1]["weights"] == {"C": 75, "T1": 25} and events[3]["input"] == {"q": "leave"} and events[-1]["inputTokens"] == 50
    url, payload, headers = sent[0]
    assert url == rec["invokeUrl"] and payload["messages"][0]["content"][0]["text"] == "Leave?"
    assert headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"] == events[0]["sessionId"]  # the Gateway keeps a session on its arm
    status, text = c.call("POST", "/api/console/workspaces/dev/agents/harness/hr_agent-1/chat", {"message": "Leave?", "systemPrompt": "Be brief."})
    assert "experiment" not in text and console["fake"].rt.last["systemPrompt"] == [{"text": "Be brief."}] and len(sent) == 1  # an override is direct
    key = c.call("POST", "/api/console/keys", {"workspace": "dev", "agent": "hr_agent", "label": "crm"})[1]
    status, answer = c.call("POST", "/v1/chat", {"agent": "hr_agent", "message": "Leave?"}, headers={"X-Api-Key": key["key"]})
    assert status == 200 and answer["experiment"] == "exp-0a1b2c3d" and answer["text"] == "15 days." and len(sent) == 2


def test_each_session_carries_the_scores_of_the_online_evaluations_reading_its_runtime():
    from workshop_customizer.console import observability as obs

    group = "/aws/bedrock-agentcore/runtimes/harness_hr-x-DEFAULT"

    class Ctl:
        def list_online_evaluation_configs(self, **kw):
            return {"onlineEvaluationConfigs": [{"onlineEvaluationConfigId": "mine", "onlineEvaluationConfigName": "hr_online"},
                                                {"onlineEvaluationConfigId": "other", "onlineEvaluationConfigName": "someone_else"}]}

        def get_online_evaluation_config(self, onlineEvaluationConfigId):
            names = [group] if onlineEvaluationConfigId == "mine" else ["/aws/bedrock-agentcore/runtimes/other-DEFAULT"]
            return {"dataSourceConfig": {"cloudWatchLogs": {"logGroupNames": names}}}

    def result(sid, name, value, label):
        return {"traceId": "t", "attributes": {"session.id": sid, "gen_ai.evaluation.name": name, "gen_ai.evaluation.score.value": value,
                                               "gen_ai.evaluation.score.label": label}}

    class Logs:
        def get_paginator(self, name):
            assert name == "filter_log_events"
            return type("P", (), {"paginate": lambda _s, logGroupName, **kw: [{"events": [{"message": json.dumps(r)} for r in (
                [result("s-1", "Builtin.Correctness", 0.2, "Fail"), result("s-1", "Builtin.Helpfulness", 0.9, "Good")]
                if logGroupName.endswith("/mine") else [])]}]})()

    session = type("S", (), {"client": lambda self, name, region_name=None, **kw: {"bedrock-agentcore-control": Ctl(), "logs": Logs()}[name]})()
    scores, names = obs.session_scores(session, "us-west-2", {"runtimeId": "harness_hr-x"})
    assert names == ["hr_online"]  # only the evaluation reading this runtime
    assert scores == {"s-1": {"Builtin.Correctness": {"value": 0.2, "label": "Fail", "failed": True},
                              "Builtin.Helpfulness": {"value": 0.9, "label": "Good", "failed": False}}}


def test_one_session_is_scored_now_with_the_references_given(monkeypatch):
    from workshop_customizer.console import observability as obs
    from workshop_customizer.direct import panel

    class Store:
        def __init__(self, logs, group, since):
            self.group = group

        def session_spans(self, sid):
            return []

        def evaluation_records(self, sid):
            return [{"traceId": "t1", "name": "invoke_agent"}] if sid == "s-1" else []

    seen = {}

    class Panel:
        def __init__(self, runtime, control, evaluators, **kw):
            seen["evaluators"] = list(evaluators)

        def score(self, jobs):
            seen["jobs"] = jobs
            return [{"evaluator": j["evaluator"], "value": 1.0, "label": "Yes", "explanation": "ok", "ignored": []} for j in jobs]

    monkeypatch.setattr(obs, "TraceStore", Store)
    monkeypatch.setattr(panel, "Panel", Panel)
    session = type("S", (), {"client": lambda self, name, region_name=None, **kw: object()})()
    agent = {"runtimeId": "harness_hr-x"}
    out = obs.score_session(session, "us-west-2", agent, "s-1", {"evaluators": ["Builtin.GoalSuccessRate"], "assertions": ["Says 15 days."],
                                                                 "expectedTools": ["hrtools___retrieve_policy"]})
    assert out["scores"][0]["value"] == 1.0 and seen["evaluators"] == ["Builtin.GoalSuccessRate"]
    assert seen["jobs"][0]["references"] == [{"context": {"spanContext": {"sessionId": "s-1"}}, "assertions": [{"text": "Says 15 days."}],
                                              "expectedTrajectory": {"toolNames": ["hrtools___retrieve_policy"]}}]
    with pytest.raises(ValueError, match="no records"):
        obs.score_session(session, "us-west-2", agent, "s-2", {})
    with pytest.raises(ValueError, match="1 to 5"):
        obs.score_session(session, "us-west-2", agent, "s-1", {"evaluators": [f"Builtin.E{i}" for i in range(6)]})


def test_a_runtime_answer_split_inside_a_character_is_decoded_whole_and_an_updating_harness_is_deleted_once_ready(monkeypatch):
    from workshop_customizer.console import agents

    raw = "积分自获得之日起24个月有效。".encode("utf-8")
    body = type("B", (), {"iter_chunks": lambda self: iter([raw[:4], raw[4:9], raw[9:]])})()  # cuts through 积 and 获
    data = type("D", (), {"invoke_agent_runtime": lambda self, **kw: {"response": body}})()
    events = list(agents.invoke(None, region="us-west-2", agent={"kind": "runtime", "arn": "arn:rt"}, message="有效期？", session_id=None, actor="a", data=data))
    text = "".join(e["text"] for e in events if e["type"] == "text")
    assert text == "积分自获得之日起24个月有效。" and "�" not in text

    calls = []

    class Ctl:
        def get_harness(self, harnessId):
            return {"harness": {"arn": "arn:h"}}

        def list_tags_for_resource(self, resourceArn):
            return {"tags": {"adlc:console": "1"}}

        def delete_harness(self, harnessId):
            calls.append(harnessId)
            if len(calls) < 3:
                raise RuntimeError("ConflictException: the harness is UPDATING")

    monkeypatch.setattr(agents, "client", lambda session, name, region=None: Ctl())
    monkeypatch.setattr(agents.time, "sleep", lambda s: None)
    assert agents.delete_agent(None, region="us-west-2", ident="h-1") == {"id": "h-1", "deleted": True} and len(calls) == 3


# -- the review's findings, each pinned ------------------------------------------------------------------------------------

def test_a_workspace_names_a_permissions_boundary_of_its_own_account_and_the_runbook_says_where_it_comes_from():
    """review H3: a workspace may carry permissionsBoundaryArn (the spoke stack's output); the spoke card's runbook names it."""
    from workshop_customizer.console import api
    from workshop_customizer.console.workspaces import WorkspaceError, boundary_of, check

    base = {"id": "spoke", "accountId": ACCOUNT, "region": "us-west-2", "roleArn": f"arn:aws:iam::{ACCOUNT}:role/adlc-console-spoke", "externalId": "x" * 20}
    ws = check({**base, "permissionsBoundaryArn": f" arn:aws:iam::{ACCOUNT}:policy/adlc-console-spoke-boundary "})
    assert boundary_of(ws) == f"arn:aws:iam::{ACCOUNT}:policy/adlc-console-spoke-boundary" and boundary_of(check(base)) is None
    for bad, why in ((f"arn:aws:iam::999999999999:policy/b", "workspace's account"), (f"arn:aws:iam::{ACCOUNT}:role/b", "managed policy ARN")):
        with pytest.raises(WorkspaceError, match=why):
            check({**base, "permissionsBoundaryArn": bad})

    class Sts:
        def get_caller_identity(self):
            return {"Arn": f"arn:aws:iam::{ACCOUNT}:user/admin"}

    base_session = type("S", (), {"client": lambda self, name, **kw: Sts()})()
    out = api.spoke_role(type("C", (), {"workspaces": type("W", (), {"_boto": lambda self, **kw: base_session})()})())
    assert out["boundaryOutput"] == "PermissionsBoundaryArn" and any("PermissionsBoundaryArn" in step and out["externalId"] in step for step in out["runbook"])
    assert "describe-stacks" in out["outputsCommand"] and "ConsoleRoleBoundary" in out["template"]


def test_an_experiments_treatment_harness_is_not_changed_directly(console):
    """review H4: the PUT route refuses the treatment Harness an A/B test runs (its evidence is about the version set up)."""
    c = console["client"]
    c.call("POST", "/api/console/workspaces", {"id": "dev", "accountId": ACCOUNT, "region": "us-west-2", "profile": "default"})
    rec = {"id": "exp-0a1b2c3d", "workspace": "dev", "name": "hr_agent A/B", "status": "paused", "agent": {"id": "hr_agent-1", "name": "hr_agent"},
           "treatmentHarness": {"id": "hr_agent_x0a1b2c3d-TwTwTwTwTw", "name": "hr_agent_x0a1b2c3d", "version": "1"}}
    console["console"].store.write("experiments", {rec["id"]: rec})
    status, out = c.call("PUT", "/api/console/workspaces/dev/agents/harness/hr_agent_x0a1b2c3d-TwTwTwTwTw", {"systemPrompt": "changed"})
    assert status == 400 and "treatment of the A/B test exp-0a1b2c3d" in out["error"]
    console["console"].store.write("experiments", {rec["id"]: {**rec, "status": "cleaned", "cleanedAt": "2026-10-01T00:00:00Z"}})
    from workshop_customizer.console import experiments

    assert experiments.twin_of(console["console"], "dev", rec["treatmentHarness"]["id"]) is None  # cleaned up: no longer held


def test_the_pages_keep_skill_pins_name_the_boundary_and_confirm_another_teams_records():
    """review L7, H3, L3, L4: the Claude SDK form keeps @version pins when a skill box is toggled; the workspace form
    takes the boundary; a bundle the console did not create offers no new version; another team's registry record is
    submitted or decided only after a confirmation (acknowledged)."""
    import re as _re
    import shutil
    import subprocess

    pages = REPO / "app" / "console" / "static"
    deploy_page = (pages / "pages" / "deploy.mjs").read_text(encoding="utf-8")
    assert "onChange: (names) => set('skills')(keepPins(form.skills, names))" in deploy_page
    workspace = (pages / "console.mjs").read_text(encoding="utf-8")
    assert "permissionsBoundaryArn" in workspace and "权限边界 ARN" in workspace and "PermissionsBoundaryArn" in workspace
    experiments_page = (pages / "pages" / "experiments.mjs").read_text(encoding="utf-8")
    assert "bundle.console ? h(Card, { title: '在最新版本上加一个版本'" in experiments_page and "cleanup_incomplete" in experiments_page
    registry_page = (pages / "pages" / "registry.mjs").read_text(encoding="utf-8")
    assert "rec.console ? {} : { acknowledged: true }" in registry_page and "...(rec.console ? {} : { acknowledged: true })" in registry_page
    node_bin = shutil.which("node") or str(Path.home() / ".nvm" / "versions" / "node" / "v25.2.1" / "bin" / "node")
    if not Path(node_bin).exists():
        pytest.skip("no node to run the pages' code")
    for page in ("console.mjs", "pages/deploy.mjs", "pages/experiments.mjs", "pages/registry.mjs"):
        checked = subprocess.run([node_bin, "--check", str(pages / page)], capture_output=True, text=True)
        assert checked.returncode == 0, (page, checked.stderr)
    keep = _re.search(r"function keepPins\(current, names\) \{.*?\n\}", deploy_page, _re.S).group(0)
    boundary = _re.search(r"function boundaryFor\(roleArn\) \{.*?\n\}", workspace, _re.S).group(0)
    script = (f"{keep}\n{boundary}\nconsole.log(JSON.stringify([keepPins(['ledger@v0003', 'faq'], ['ledger', 'faq', 'new']), keepPins(['ledger@v0003'], []),"
              f" boundaryFor('arn:aws:iam::{ACCOUNT}:role/adlc-console-spoke'), boundaryFor('nope')]))")
    ran = subprocess.run([node_bin, "-e", script], capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout) == [["ledger@v0003", "faq", "new"], [], f"arn:aws:iam::{ACCOUNT}:policy/adlc-console-spoke-boundary", ""]
