"""The console's defects found by review, each pinned (no AWS): the store, sign-in, the request guard, agent changes,
uploads, contract sets, the public API's failed turns, the online evaluation's role."""
from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))
_spec = importlib.util.spec_from_file_location("adlc_console_server_hardening", REPO / "app" / "console" / "server.py")
console_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(console_mod)  # type: ignore[union-attr]

from workshop_customizer.console import agents, evaluation, kb  # noqa: E402
from workshop_customizer.console.auth import Auth, AuthError  # noqa: E402
from workshop_customizer.console.store import Store  # noqa: E402


# -- the store and sign-in fail closed -------------------------------------------------------------------------------

def test_a_collection_that_cannot_be_read_is_never_taken_for_empty(tmp_path):
    store = Store(tmp_path)
    assert store.read("users", {}) == {}  # missing: the default
    store.write("keys", {"crm": {"id": "crm"}})
    store.path("keys").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        store.read("keys", {})
    with pytest.raises(ValueError):  # an update must not write a new collection over the one it could not read
        store.update("keys", {}, lambda all_: {**all_, "another": {}})
    assert store.path("keys").read_text(encoding="utf-8") == "{not json"


def test_the_console_never_reopens_and_an_edit_keeps_what_it_does_not_change(tmp_path):
    store = Store(tmp_path)
    auth = Auth(store)
    auth.put_user({"username": "admin", "password": "a" * 12, "role": "admin"})
    auth.put_user({"username": "bob", "password": "b" * 12, "workspaces": ["dev"]})
    with pytest.raises(AuthError, match="last admin"):
        auth.delete_user("admin")
    with pytest.raises(AuthError, match="at least one admin"):
        auth.put_user({"username": "admin", "role": "member"})
    token = auth.login("bob", "b" * 12)
    assert auth.whoami(token)["username"] == "bob"
    auth.put_user({"username": "bob", "password": "c" * 12})  # a password reset: role and grants stay, the session ends
    assert [(u["username"], u["role"], u["workspaces"]) for u in auth.users()] == [("admin", "admin", []), ("bob", "member", ["dev"])]
    with pytest.raises(AuthError):
        auth.whoami(token)
    store.path("users").write_text("", encoding="utf-8")  # unreadable: closed, never open mode
    with pytest.raises(ValueError):
        auth.whoami(None)


# -- the request guard -----------------------------------------------------------------------------------------------

@pytest.fixture()
def served(tmp_path):
    c = console_mod.Console(tmp_path / "data", session_factory=lambda **kw: None, clients_factory=lambda cfg: None)
    httpd = console_mod.create_server(c, host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield c, httpd.server_address[1]
    httpd.shutdown()


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    conn.request(method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})} if data is not None else (headers or {}))
    response = conn.getresponse()
    return response.status, response.read().decode("utf-8", "replace")


def test_requests_a_browser_could_be_tricked_into_are_refused(served):
    c, port = served
    assert call(port, "GET", "/api/console/me")[0] == 200  # open mode, from this machine
    assert call(port, "GET", "/api/console/me", headers={"Host": "evil.example:8770"})[0] == 403  # DNS rebinding
    assert call(port, "POST", "/api/console/keys", {"workspace": "x"}, headers={"Origin": "http://evil.example"})[0] == 403
    assert call(port, "POST", "/api/console/users", b'{"username":"x"}', headers={"Content-Type": "text/plain"})[0] == 415
    assert call(port, "POST", "/api/console/users", {"username": "x"}, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert call(port, "POST", "/api/console/users", b"", headers={"Content-Length": "-1"})[0] == 400


def test_the_workshop_app_needs_an_admin_and_keeps_its_own_refusals(served):
    c, port = served
    c.auth.put_user({"username": "admin", "password": "a" * 12, "role": "admin"})
    c.auth.put_user({"username": "bob", "password": "b" * 12, "workspaces": []})
    token = c.auth.login("bob", "b" * 12)
    app = "/apps/workshop-customizer/api/apps/workshop-customizer/projects"
    assert call(port, "GET", app, headers={"Cookie": f"adlc_session={token}"})[0] == 403  # the App acts with the host's AWS profiles
    admin = c.auth.login("admin", "a" * 12)
    status, body = call(port, "GET", app + "/no-such-project", headers={"Cookie": f"adlc_session={admin}"})
    assert status == 404 and "internal error" not in body  # the App's own 404, not a 500


# -- agents ------------------------------------------------------------------------------------------------------------

def test_another_teams_harness_changes_only_when_acknowledged_and_keeps_its_model_settings(monkeypatch):
    sent = {}

    class Ctl:
        def get_harness(self, harnessId):
            return {"harness": {"arn": "arn:h", "harnessName": "their_agent", "model": {"bedrockModelConfig": {"modelId": "m-old", "maxTokens": 4096,
                                                                                                         "temperature": 0.2}}}}

        def list_tags_for_resource(self, resourceArn):
            return {"tags": {}}

        def update_harness(self, harnessId, **changes):
            sent.update(changes)
            return {"harness": {"status": "UPDATING"}}

    monkeypatch.setattr(agents, "client", lambda session, name, region=None: Ctl())
    with pytest.raises(agents.AgentError, match="acknowledged"):
        agents.update_harness(None, region="us-west-2", ident="h-1", body={"model": "us.anthropic.claude-haiku-4-5-20251001-v1:0"})
    agents.update_harness(None, region="us-west-2", ident="h-1", body={"model": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "acknowledged": True})
    assert sent["model"] == {"bedrockModelConfig": {"modelId": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "maxTokens": 4096, "temperature": 0.2}}


def test_long_harness_names_get_their_own_roles_and_a_bad_turn_is_refused_before_the_stream():
    a, b = "x" * 45 + "_one", "x" * 45 + "_two"
    assert agents.role_name(a) != agents.role_name(b) and all(len(agents.role_name(n)) <= 64 for n in (a, b))
    assert agents.role_name("hr_agent") == "hr_agent-console-harness"
    with pytest.raises(agents.AgentError, match="message"):
        agents.check_turn("x" * 20_001, None)


# -- knowledge bases, contract sets, translations -------------------------------------------------------------------------

def test_uploads_keep_their_names_and_go_only_where_the_kb_reads(tmp_path):
    put = {}

    class Agent:
        def get_knowledge_base(self, knowledgeBaseId):
            return {"knowledgeBase": {"knowledgeBaseArn": "arn:kb", "name": "rules"}}

        def list_tags_for_resource(self, resourceArn):
            return {"tags": {"adlc:console": "1"}}

        def list_data_sources(self, knowledgeBaseId):
            return {"dataSourceSummaries": [{"dataSourceId": "ds"}]}

        def get_data_source(self, knowledgeBaseId, dataSourceId):
            params = {"connectionConfiguration": {"bucketName": bucket}, "filterConfiguration": {"inclusionPrefixes": [prefix]}}
            return {"dataSource": {"dataSourceConfiguration": {"managedKnowledgeBaseConnectorConfiguration": {"connectorParameters": json.dumps(params)}}}}

        def start_ingestion_job(self, knowledgeBaseId, dataSourceId):
            return {"ingestionJob": {"ingestionJobId": "j", "status": "STARTING"}}

    class S3:
        def head_bucket(self, **kw):
            return {}

        def put_object(self, Bucket, Key, Body):
            put[Key] = Body

    clients = {"bedrock-agent": Agent(), "s3": S3()}
    session = type("S", (), {"client": lambda self, name, region_name=None, **kw: clients[name]})()
    ws = {"id": "dev", "accountId": "111122223333", "region": "us-west-2"}
    console = type("C", (), {"workspaces": type("W", (), {"get": lambda _s, w: ws, "session": lambda _s, w: session})()})()
    bucket, prefix = kb.console_bucket("111122223333", "us-west-2"), "kb/KB1/"
    files = [{"name": name, "contentBase64": "aGk="} for name in ("积分规则.pdf", "补偿政策.pdf", "积分规则.pdf")]
    assert kb.upload(console, "dev", "KB1", files)["uploaded"] == ["积分规则.pdf", "补偿政策.pdf", "积分规则-3.pdf"]
    assert sorted(put) == ["kb/KB1/积分规则-3.pdf", "kb/KB1/积分规则.pdf", "kb/KB1/补偿政策.pdf"]
    bucket, prefix = "their-bucket", "docs/"  # a KB built on another S3 location is not fed through the console's folder
    with pytest.raises(kb.KnowledgeError, match="s3://their-bucket/docs/"):
        kb.upload(console, "dev", "KB1", files[:1])


def test_contract_sets_stay_in_their_workspace_and_a_cut_translation_is_refused(tmp_path):
    store = Store(tmp_path)
    contracts = [{"id": "a", "query": "q", "expected": {"mustMention": ["x"]}}]
    evaluation.put_contract_set(store, "team-a", {"id": "cs-shared", "name": "A", "contracts": contracts})
    with pytest.raises(evaluation.EvaluationError, match="another workspace"):
        evaluation.put_contract_set(store, "team-b", {"id": "cs-shared", "name": "B", "contracts": contracts})
    with pytest.raises(evaluation.EvaluationError, match="project"):
        evaluation.import_from_pack(store, "team-a", tmp_path, "../../etc")

    class Runtime:
        def converse(self, **kw):
            return {"stopReason": "max_tokens", "output": {"message": {"content": [{"text": "You are the"}]}}}

    session = type("S", (), {"client": lambda self, name, region_name=None, **kw: Runtime()})()
    with pytest.raises(evaluation.EvaluationError, match="too long"):
        evaluation.translate(session, "us-west-2", "你是客服。" * 100, "English")


# -- the public API and the online evaluation's role ----------------------------------------------------------------------

def test_a_turn_that_failed_part_way_is_a_502_with_what_arrived(monkeypatch):
    from workshop_customizer.console import public

    monkeypatch.setattr(public, "_agent", lambda console, scope, name: (None, "us-west-2", {"kind": "harness", "id": "h", "name": "bot", "arn": "a"}))
    monkeypatch.setattr(public.experiments, "route_for", lambda *a, **kw: None)
    monkeypatch.setattr(public.agents, "invoke", lambda *a, **kw: iter([{"type": "session", "sessionId": "s"}, {"type": "text", "text": "Your refund of "},
                                                                        {"type": "error", "error": "EventStreamError: cut"}]))
    console = type("C", (), {"auth": type("A", (), {"key_scope": lambda _s, key: {"workspace": "dev", "keyId": "k"}})()})()
    status, body = public.handle(console, "POST", "/chat", {"X-Api-Key": "adlc_live_x"}, {"agent": "bot", "message": "refund?"})
    assert status == 502 and body["partialText"] == "Your refund of " and "cut" in body["error"]


def test_an_online_evaluation_role_someone_else_made_is_neither_reused_nor_deleted():
    from workshop_customizer.direct import online

    calls = []

    class Iam:
        def get_role(self, RoleName):
            return {"Role": {"Arn": f"arn:aws:iam::1:role/{RoleName}"}}

        def list_role_tags(self, RoleName):
            return {"Tags": [{"Key": "team", "Value": "theirs"}]}

        def __getattr__(self, name):
            return lambda **kw: calls.append(name)

    session = type("S", (), {"client": lambda self, name, region_name=None, **kw: Iam() if name == "iam" else type("X", (), {
        "delete_online_evaluation_config": lambda _s, **kw: calls.append("delete_config")})()})()
    with pytest.raises(ValueError, match="not made by this platform"):
        online.apply_plan(session, account="1", region="us-west-2", planned={"request": {"tags": {}}}, role="hr-online-eval")
    assert online.delete_config(session, region="us-west-2", config_id="c-1", role="hr-online-eval") == ["c-1"]
    assert "delete_role" not in calls and "put_role_policy" not in calls


def test_the_online_eval_cli_tags_its_role_as_direct_mode_so_delete_removes_it_and_apply_runs_again(tmp_path, monkeypatch):
    """review: tools/online_eval.py tagged its role adlc:pack + adlc:online-eval only, which _ours did not count as the
    platform's: --delete left the role (model calls on *, Lambda invoke on every function) and the next --apply refused.
    Its role now carries the direct mode tag, and a role tagged the old way is still the platform's."""
    import importlib.util
    import sys

    from workshop_customizer.direct import online

    class Iam:
        def __init__(self):
            self.roles = {}

        def get_role(self, RoleName):
            if RoleName not in self.roles:
                raise RuntimeError("An error occurred (NoSuchEntity) when calling the GetRole operation")
            return {"Role": {"RoleName": RoleName, "Path": "/", "Arn": f"arn:aws:iam::111122223333:role/{RoleName}"}}

        def create_role(self, RoleName, Tags, Path="/", **kw):
            self.roles[RoleName] = {"tags": {t["Key"]: t["Value"] for t in Tags}, "policies": {}}
            return {"Role": {"RoleName": RoleName, "Path": Path, "Arn": f"arn:aws:iam::111122223333:role{Path}{RoleName}"}}

        def list_role_tags(self, RoleName):
            return {"Tags": [{"Key": k, "Value": v} for k, v in self.roles[RoleName]["tags"].items()]}

        def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
            self.roles[RoleName]["policies"][PolicyName] = PolicyDocument

        def list_role_policies(self, RoleName):
            return {"PolicyNames": list(self.roles[RoleName]["policies"])}

        def delete_role_policy(self, RoleName, PolicyName):
            del self.roles[RoleName]["policies"][PolicyName]

        def delete_role(self, RoleName):
            del self.roles[RoleName]

    class Ctl:
        def __init__(self):
            self.configs = {}

        def list_agent_runtimes(self, **kw):
            return {"agentRuntimes": []}

        def list_evaluators(self, **kw):
            return {"evaluators": []}

        def create_online_evaluation_config(self, **kw):
            self.configs["cfg-1"] = kw
            return {"onlineEvaluationConfigId": "cfg-1", "onlineEvaluationConfigArn": "arn:cfg-1"}

        def delete_online_evaluation_config(self, onlineEvaluationConfigId):
            self.configs.pop(onlineEvaluationConfigId)

    iam, ctl = Iam(), Ctl()
    sts = type("Sts", (), {"get_caller_identity": lambda _s: {"Account": "111122223333"}})()

    class Session:
        def __init__(self, **kw):
            pass

        def client(self, name, region_name=None, **kw):
            return {"iam": iam, "bedrock-agentcore-control": ctl, "sts": sts}[name]

    import boto3

    monkeypatch.setattr(boto3, "Session", Session)
    project = tmp_path / "hr"
    namespace = {"agentName": "hragent", "gatewayName": "hr-gw", "toolTargetName": "hr-tools", "lambdaFunctionName": "hr-fn", "knowledgeBaseName": "hr-kb"}
    for path, doc in ((project / "build/release/pack/pack.json", {"namespace": namespace, "packId": "hr-pack"}),
                      (project / "build/release/RELEASE.json", {"version": "v1"}),
                      (project / "build/direct/resources.json", {"harness": {"runtimeId": "harness_direct_hragent-AbCdEf1234"}}),
                      (project / "build/direct/v1/direct-rehearsal.json", {"direct": {"panel": {"references": False, "unseen": [], "recommendation": [
                          {"evaluator": "Builtin.Helpfulness", "recommended": True, "sees": ["vague answers"]}]}}})):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("online_eval_cli", REPO / "tools" / "online_eval.py")
    cli = importlib.util.module_from_spec(spec)
    sys.modules["online_eval_cli"] = cli
    spec.loader.exec_module(cli)  # type: ignore[union-attr]
    args = ["--project-dir", str(project), "--profile", "p", "--account", "111122223333"]
    role = "hragent-direct-online-eval"
    assert cli.main(args + ["--apply"]) == 0
    assert iam.roles[role]["tags"] == {"adlc:mode": "direct", "adlc:pack": "hr-pack", "adlc:online-eval": "direct"}
    assert ctl.configs["cfg-1"]["evaluationExecutionRoleArn"] == f"arn:aws:iam::111122223333:role/{role}"
    assert cli.main(args + ["--delete"]) == 0 and role not in iam.roles and not ctl.configs  # the role goes with it
    assert cli.main(args + ["--apply"]) == 0 and role in iam.roles  # and it applies again
    iam.roles[role]["tags"] = {"adlc:pack": "hr-pack", "adlc:online-eval": "direct"}  # a role from before the mode tag
    assert online.platform_refusal(iam, role, iam.get_role(RoleName=role)["Role"]) is None
    assert cli.main(args + ["--delete"]) == 0 and role not in iam.roles


# -- the permissions boundary (review H3) -----------------------------------------------------------------------------------

BOUNDARY = "arn:aws:iam::111122223333:policy/adlc-console-spoke-boundary"


class BoundaryIam:
    """IAM as a spoke role sees it: roles keep their boundary; every call recorded."""

    def __init__(self, roles=None, *, spoke=True):
        self.calls, self.roles, self.spoke = [], dict(roles or {}), spoke

    def _role(self, RoleName):
        if RoleName not in self.roles:
            raise RuntimeError("An error occurred (NoSuchEntity) when calling the GetRole operation: NoSuchEntity")
        return self.roles[RoleName]

    def get_role(self, RoleName):
        r = self._role(RoleName)
        path = r.get("path") or "/"  # IAM's own default, for a role made without a path
        out = {"RoleName": RoleName, "Path": path, "Arn": f"arn:aws:iam::111122223333:role{path}{RoleName}", "AssumeRolePolicyDocument": {}}
        if r.get("boundary"):
            out["PermissionsBoundary"] = {"PermissionsBoundaryType": "Policy", "PermissionsBoundaryArn": r["boundary"]}
        return {"Role": out}

    def create_role(self, **kw):
        self.calls.append(("create_role", kw))
        if kw["RoleName"] in self.roles:  # a role name is unique in its account, whatever the path
            raise RuntimeError("An error occurred (EntityAlreadyExists) when calling the CreateRole operation")
        self.roles[kw["RoleName"]] = {"tags": kw.get("Tags") or [], "boundary": kw.get("PermissionsBoundary"), "path": kw.get("Path") or "/"}
        return {"Role": {"Arn": f"arn:aws:iam::111122223333:role{kw.get('Path') or '/'}{kw['RoleName']}"}}

    def list_role_tags(self, RoleName):
        return {"Tags": self._role(RoleName)["tags"]}

    def put_role_permissions_boundary(self, RoleName, PermissionsBoundary):
        self.calls.append(("put_role_permissions_boundary", {"RoleName": RoleName, "PermissionsBoundary": PermissionsBoundary}))
        self._role(RoleName)["boundary"] = PermissionsBoundary

    def put_role_policy(self, **kw):
        self.calls.append(("put_role_policy", kw))
        if self.spoke and self._role(kw["RoleName"]).get("boundary") != BOUNDARY:  # the spoke role changes only roles that carry the boundary
            raise RuntimeError("An error occurred (AccessDenied) when calling the PutRolePolicy operation")

    def attach_role_policy(self, **kw):
        self.calls.append(("attach_role_policy", kw))


def _ops(iam, role):
    return [op for op, kw in iam.calls if kw.get("RoleName") == role]


def test_every_role_the_console_creates_carries_the_workspace_boundary_and_an_older_one_gets_it(monkeypatch):
    from workshop_customizer.console import experiments
    from workshop_customizer.direct import online

    iam = BoundaryIam({"old_agent-console-harness": {"tags": [{"Key": "adlc:console", "Value": "1"}], "path": agents.ROLE_PATH},
                       "their-role": {"tags": [{"Key": "team", "Value": "theirs"}]}})
    monkeypatch.setattr(agents.time, "sleep", lambda s: None)
    monkeypatch.setattr(kb.time, "sleep", lambda s: None)
    session = type("S", (), {"client": lambda self, name, region_name=None, **kw: iam})()
    agents._ensure_role(session, "new_agent-console-harness", "111122223333", "us-west-2", {"Statement": []}, boundary=BOUNDARY)
    kb.ensure_kb_role(session, "111122223333", "us-west-2", BOUNDARY)
    experiments._ensure_role(iam, "adlc-console-exp-1a2b3c4d", {}, {"Statement": []}, {"adlc:console": "1"}, BOUNDARY)
    made = {kw["RoleName"]: (kw.get("PermissionsBoundary"), kw.get("Path")) for op, kw in iam.calls if op == "create_role"}
    assert made == {"new_agent-console-harness": (BOUNDARY, "/adlc-console/"), kb.KB_ROLE: (BOUNDARY, "/adlc-console/"),
                    "adlc-console-exp-1a2b3c4d": (BOUNDARY, "/adlc-console/")}
    # a console role on the path made before the workspace had a boundary gets it before its policy is put again; another owner's is left
    agents._ensure_role(session, "old_agent-console-harness", "111122223333", "us-west-2", {"Statement": []}, boundary=BOUNDARY)
    assert _ops(iam, "old_agent-console-harness") == ["put_role_permissions_boundary", "put_role_policy"]
    with pytest.raises(agents.AgentError, match="carry the console's permissions boundary"):
        agents.put_inline_policy(session, "arn:aws:iam::111122223333:role/their-role", "adlc-console-skills", {"Statement": []}, what="skills",
                                 boundary=BOUNDARY)
    assert "put_role_permissions_boundary" not in _ops(iam, "their-role")
    plain = BoundaryIam(spoke=False)  # a profile workspace: no boundary anywhere
    agents._ensure_role(type("S", (), {"client": lambda self, name, region_name=None, **kw: plain})(), "x-console-harness", "1", "us-west-2", {})
    assert "PermissionsBoundary" not in plain.calls[0][1]


def test_the_online_evaluation_role_carries_the_workspace_boundary():
    from workshop_customizer.direct import online

    iam = BoundaryIam()

    class Ctl:
        def create_online_evaluation_config(self, **kw):
            return {"onlineEvaluationConfigId": "c-1", "onlineEvaluationConfigArn": "arn:c-1"}

    session = type("S", (), {"client": lambda self, name, region_name=None, **kw: iam if name == "iam" else Ctl()})()
    planned = {"request": {"tags": {"adlc:console": "1"}, "dataSourceConfig": {"cloudWatchLogs": {"logGroupNames": ["/aws/bedrock-agentcore/runtimes/x-DEFAULT"]}},
                           "evaluators": [{"evaluatorId": "Builtin.Correctness"}], "rule": {"samplingConfig": {"samplingPercentage": 10.0}}}}
    online.apply_plan(session, account="111122223333", region="us-west-2", planned=planned, role="hr-online-eval", boundary=BOUNDARY)
    assert [(op, kw.get("PermissionsBoundary")) for op, kw in iam.calls][:2] == [("create_role", BOUNDARY), ("put_role_policy", None)]
    iam.roles["hr-online-eval"]["boundary"] = None  # made before the workspace had one
    online.apply_plan(session, account="111122223333", region="us-west-2", planned=planned, role="hr-online-eval", boundary=BOUNDARY)
    assert _ops(iam, "hr-online-eval")[-2:] == ["put_role_permissions_boundary", "put_role_policy"]


def test_a_saved_external_id_is_never_sent_back_and_an_edit_without_it_keeps_it(served):
    c, port = served
    body = {"id": "spoke", "accountId": "111122223333", "region": "us-west-2", "profile": "default",
            "roleArn": "arn:aws:iam::111122223333:role/adlc-console-spoke", "externalId": "s3cret-external-id-value"}
    status, text = call(port, "POST", "/api/console/workspaces", body)
    saved = json.loads(text)
    assert status == 201 and "externalId" not in saved and saved["externalIdSet"] is True and "s3cret" not in text
    call(port, "POST", "/api/console/workspaces", {**body, "externalId": "", "name": "Renamed"})
    status, listed = call(port, "GET", "/api/console/workspaces")
    [ws] = json.loads(listed)["workspaces"]
    assert ws["name"] == "Renamed" and "s3cret" not in listed and c.workspaces.get("spoke")["externalId"] == "s3cret-external-id-value"


def test_a_large_body_is_read_only_from_a_signed_in_caller_and_small_fixes_hold(served, tmp_path):
    c, port = served
    c.auth.put_user({"username": "admin", "password": "a" * 12, "role": "admin"})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("POST", "/api/console/workspaces/dev/skill-lab/assets")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(60 * 1024 * 1024))  # announced, never sent: refused before the body is read
    conn.endheaders()
    assert conn.getresponse().status == 401
    assert c.auth.put_user({"username": "bob", "password": "b" * 12, "workspaces": None})["workspaces"] == []  # null is no grant, not a 500
    store = Store(tmp_path / "s")
    project = "a-project-id-long-enough-to-be-truncated-x"
    store.write("contract_sets", {f"cs-pack-{project}"[:40]: {"id": f"cs-pack-{project}"[:40], "workspace": "dev", "name": "old", "contracts": [],
                                                                   "updatedAt": "t"}})
    pack = tmp_path / "w" / "projects" / project / "build" / "release" / "pack"
    (pack / "golden").mkdir(parents=True)
    (pack / "pack.json").write_text("{}")
    (pack / "golden" / "practice.json").write_text(json.dumps([{"id": "a", "query": "q", "expected": {"mustMention": ["x"]}}]))
    again = evaluation.import_from_pack(store, "dev", tmp_path / "w", project)
    assert again["id"] == f"cs-pack-{project}"[:40] and len(store.read("contract_sets", {})) == 1  # the same set, updated
    assert evaluation.packs(tmp_path / "w") == [{"id": project, "name": project}]


def test_an_agent_in_a_paused_ab_test_is_not_changed_directly(served):
    c, port = served
    c.workspaces.put({"id": "dev", "accountId": "111122223333", "region": "us-west-2", "profile": "default"})
    c.store.write("experiments", {"exp-0a1b2c3d": {"id": "exp-0a1b2c3d", "workspace": "dev", "status": "paused", "agent": {"id": "h-1"}}})
    status, body = call(port, "PUT", "/api/console/workspaces/dev/agents/harness/h-1", {"systemPrompt": "new"})
    assert status == 400 and "A/B test (paused)" in body  # review: only "running" was refused
