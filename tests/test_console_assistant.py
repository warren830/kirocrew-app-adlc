"""engine console.assistant: conversations, turns with read-only tools, revisions, approval of an exact revision,
drift refusal, and the "discussion never writes AWS" invariant. The console runs in process on loopback; every AWS
service is a fake that records each call."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import sys
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))
spec = importlib.util.spec_from_file_location("adlc_console_server_assistant", REPO / "app" / "console" / "server.py")
console_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console_mod)  # type: ignore[union-attr]

from workshop_customizer.console import agents, assistant, evaluation, kb  # noqa: E402

ACCOUNT = "111122223333"
REGION = "us-west-2"
BUCKET = f"adlc-console-{ACCOUNT}-{REGION}"
HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
SKILL = "---\nname: reply-style\ndescription: 回答会员关于积分的任何问题时使用。\n---\n# 规范\n\n1. 第一句给结论。\n"
WRITES = ("create", "update", "delete", "put", "attach", "tag", "untag", "start", "invoke", "sync")


def wait(fn, timeout: float = 10.0):
    """Poll ``fn`` until it returns something truthy (without time.sleep, which a test may stub)."""
    pause = threading.Event()
    for _ in range(int(timeout / 0.02)):
        got = fn()
        if got:
            return got
        pause.wait(0.02)
    raise AssertionError("timed out")


# -- the fake account ----------------------------------------------------------------------------------------------------

class World:
    """Every AWS service the console reaches, as fakes over one shared state; ``calls`` records each operation."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.script: list = []
        self.requests: list[dict] = []
        self.kbs = {"KB1": {"knowledgeBaseId": "KB1", "name": "loyalty-rules", "status": "ACTIVE", "description": "积分规则"}}
        self.harnesses: dict[str, dict] = {}
        self.gateways = {"gw-kb": {"gatewayId": "gw-kb", "name": kb.GATEWAY, "status": "READY", "authorizerType": "AWS_IAM", "protocolType": "MCP",
                                   "gatewayArn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/gw-kb"},
                         "gw-hr": {"gatewayId": "gw-hr", "name": "hr-tools", "status": "READY", "authorizerType": "AWS_IAM", "protocolType": "MCP",
                                   "gatewayArn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/gw-hr", "description": "HR lookups"},
                         "gw-jwt": {"gatewayId": "gw-jwt", "name": "partner-tools", "status": "READY", "authorizerType": "CUSTOM_JWT", "protocolType": "MCP",
                                    "gatewayArn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/gw-jwt"}}
        self.targets: dict[str, dict[str, dict]] = {"gw-kb": {}, "gw-hr": {"hrtools": {"targetId": "T-HR", "name": "hrtools", "status": "READY",
            "targetConfiguration": {"mcp": {"lambda": {"toolSchema": {"inlinePayload": [{"name": "lookup_points", "description": "A member's balance"}]}}}}}},
            "gw-jwt": {}}
        self.s3: dict[tuple[str, str], bytes] = {(BUCKET, "skills/reply-style/v0001/SKILL.md"): SKILL.encode()}
        self.roles: dict[str, dict] = {kb.GATEWAY_ROLE: {"tags": [{"Key": "adlc:console", "Value": "1"}], "path": "/adlc-console/"}}
        self.fail_target = 0
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        services = {"bedrock-runtime": Runtime, "bedrock-agent": BedrockAgent, "bedrock-agent-runtime": AgentRuntime,
                    "bedrock-agentcore-control": Control, "s3": S3, "iam": Iam, "bedrock": Bedrock, "sts": Sts}
        self.services = {name: cls(self, name) for name, cls in services.items()}

    def session(self):
        world = self
        return type("Session", (), {"client": lambda _s, name, region_name=None, **kw: world.services[name]})()

    def since(self, mark: int) -> list[tuple[str, str]]:
        return self.calls[mark:]


class Fake:
    def __init__(self, world: World, service: str):
        self.w, self.service = world, service

    def __getattribute__(self, name):
        attr = object.__getattribute__(self, name)
        if callable(attr) and not name.startswith("_"):
            object.__getattribute__(self, "w").calls.append((object.__getattribute__(self, "service"), name))
        return attr


class Sts(Fake):
    def get_caller_identity(self):
        return {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/sa"}


class Runtime(Fake):
    """bedrock-runtime: answers from the test's script (each step a response or a function of the request)."""

    def converse(self, **request):
        self.w.requests.append(copy.deepcopy(request))
        self.w.entered.set()
        if self.w.gate is not None:
            assert self.w.gate.wait(10), "the test never released the model"
        step = self.w.script.pop(0)
        return step(request) if callable(step) else step


class BedrockAgent(Fake):
    def list_knowledge_bases(self, **kw):
        return {"knowledgeBaseSummaries": [{"knowledgeBaseId": k["knowledgeBaseId"], "name": k["name"], "status": k["status"], "description": k["description"]}
                                           for k in self.w.kbs.values()]}

    def get_knowledge_base(self, knowledgeBaseId):
        k = self.w.kbs[knowledgeBaseId]
        return {"knowledgeBase": {**k, "knowledgeBaseArn": f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/{knowledgeBaseId}",
                                  "knowledgeBaseConfiguration": {"type": "MANAGED"}}}

    def list_tags_for_resource(self, resourceArn):
        return {"tags": {}}

    def list_data_sources(self, knowledgeBaseId):
        return {"dataSourceSummaries": [{"dataSourceId": "DS1"}]}

    def get_data_source(self, knowledgeBaseId, dataSourceId):
        return {"dataSource": {"dataSourceId": dataSourceId, "name": "docs", "status": "AVAILABLE",
                               "dataSourceConfiguration": {"s3Configuration": {"bucketArn": f"arn:aws:s3:::{BUCKET}", "inclusionPrefixes": ["kb/KB1/"]}}}}

    def list_ingestion_jobs(self, **kw):
        return {"ingestionJobSummaries": []}

    def list_knowledge_base_documents(self, **kw):
        return {"documentDetails": [{"identifier": {"s3": {"uri": f"s3://{BUCKET}/kb/KB1/积分获取与有效期规则.md"}}, "status": "INDEXED"}]}


class AgentRuntime(Fake):
    def retrieve(self, knowledgeBaseId, retrievalQuery, retrievalConfiguration):
        return {"retrievalResults": [{"content": {"text": "每笔积分自获得之日起24个月有效，到期当月月底清零。"}, "score": 0.62,
                                      "location": {"type": "S3", "s3Location": {"uri": f"s3://{BUCKET}/kb/KB1/积分获取与有效期规则.md"}}}]}


class Control(Fake):
    """bedrock-agentcore-control: Harnesses (READY one read after a change), Gateways and their targets."""

    def list_harnesses(self, **kw):
        return {"harnesses": [{"harnessName": h["harnessName"], "harnessId": h["harnessId"], "arn": h["arn"], "status": h["status"]} for h in self.w.harnesses.values()]}

    def list_agent_runtimes(self, **kw):
        return {"agentRuntimes": []}

    def get_harness(self, harnessId):
        h = self.w.harnesses[harnessId]
        out = {"harness": copy.deepcopy(h)}
        if h["status"] in ("CREATING", "UPDATING"):
            h["status"] = "READY"
        return out

    def list_tags_for_resource(self, resourceArn):
        h = next((h for h in self.w.harnesses.values() if h["arn"] == resourceArn), None)
        return {"tags": dict(h.get("tags") or {}) if h else {}}

    def create_harness(self, **request):
        hid = f"{request['harnessName']}-{len(self.w.harnesses) + 1:04d}"
        self.w.harnesses[hid] = {**copy.deepcopy(request), "harnessId": hid, "arn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:harness/{hid}",
                                 "status": "CREATING", "environment": {"agentCoreRuntimeEnvironment": {"agentRuntimeId": f"harness_{hid}"}}}
        return {"harness": {"harnessId": hid, "arn": self.w.harnesses[hid]["arn"], "status": "CREATING"}}

    def update_harness(self, harnessId, **changes):
        self.w.harnesses[harnessId].update(copy.deepcopy(changes), status="UPDATING")
        return {"harness": {"status": "UPDATING"}}

    def list_gateways(self, **kw):
        return {"items": [{k: g.get(k) for k in ("gatewayId", "name", "status", "authorizerType", "protocolType", "description")} for g in self.w.gateways.values()]}

    def get_gateway(self, gatewayIdentifier):
        return copy.deepcopy(self.w.gateways[gatewayIdentifier])

    def list_gateway_targets(self, gatewayIdentifier, **kw):
        return {"items": [{"targetId": t["targetId"], "name": t["name"], "status": t["status"]} for t in self.w.targets[gatewayIdentifier].values()]}

    def get_gateway_target(self, gatewayIdentifier, targetId):
        return copy.deepcopy(next(t for t in self.w.targets[gatewayIdentifier].values() if t["targetId"] == targetId))

    def create_gateway_target(self, **request):
        if self.w.fail_target:
            self.w.fail_target -= 1
            raise RuntimeError("An error occurred (ThrottlingException) when calling the CreateGatewayTarget operation: slow down")
        gid = request["gatewayIdentifier"]
        tid = f"T{len(self.w.targets[gid]) + 1}"
        self.w.targets[gid][request["name"]] = {"targetId": tid, "name": request["name"], "status": "READY", "targetConfiguration": request["targetConfiguration"]}
        return {"targetId": tid}

    def update_gateway_target(self, **request):
        self.w.targets[request["gatewayIdentifier"]][request["name"]]["targetConfiguration"] = request["targetConfiguration"]
        return {}


class S3(Fake):
    def get_object(self, Bucket, Key, ExpectedBucketOwner=None):
        if (Bucket, Key) not in self.w.s3:
            raise RuntimeError("An error occurred (NoSuchKey) when calling the GetObject operation")
        return {"Body": io.BytesIO(self.w.s3[(Bucket, Key)])}

    def head_bucket(self, Bucket, ExpectedBucketOwner=None):
        return {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.w.s3[(Bucket, Key)] = Body


class Iam(Fake):
    def get_role(self, RoleName):
        if RoleName not in self.w.roles:
            raise RuntimeError("An error occurred (NoSuchEntity) when calling the GetRole operation")
        path = self.w.roles[RoleName].get("path") or "/"
        return {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role{path}{RoleName}", "Path": path}}

    def list_role_tags(self, RoleName):
        return {"Tags": self.w.roles[RoleName].get("tags") or []}

    def create_role(self, RoleName, AssumeRolePolicyDocument, Description=None, Tags=None, Path="/"):
        self.w.roles[RoleName] = {"tags": Tags, "path": Path}
        return {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role{Path}{RoleName}"}}

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.w.roles.setdefault(RoleName, {})[PolicyName] = json.loads(PolicyDocument)


class Bedrock(Fake):
    def list_inference_profiles(self, **kw):
        return {"inferenceProfileSummaries": [{"inferenceProfileId": m, "status": "ACTIVE"} for m in (HAIKU, assistant.DEFAULT_MODEL, "us.cohere.embed-v4:0")]}

    def list_foundation_models(self, **kw):
        return {"modelSummaries": []}


# -- the model's script ------------------------------------------------------------------------------------------------

def says(text: str) -> dict:
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}}, "stopReason": "end_turn",
            "usage": {"inputTokens": 100, "outputTokens": 20, "cacheReadInputTokens": 80}}


def uses(*calls: tuple[str, dict], text: str | None = None) -> dict:
    content = ([{"text": text}] if text else []) + [{"toolUse": {"toolUseId": f"tu-{name}-{i}", "name": name, "input": args, "type": "tool_use"}}
                                                    for i, (name, args) in enumerate(calls)]
    return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": "tool_use", "usage": {"inputTokens": 100, "outputTokens": 30}}


def good(**changes) -> dict:
    proposal = {"name": "points_faq", "model": HAIKU, "systemPrompt": "你是拾光家居的积分客服。只根据知识库回答，不承诺补发积分。",
                "knowledgeBases": ["KB1"], "skills": ["reply-style"], "summary": "回答积分规则的 FAQ Agent。", "verificationRounds": 1,
                "contracts": [{"id": "points-expiry", "label": "积分有效期", "source": "用户：积分多久过期一定要答准", "query": "积分多久过期？",
                               "expected": {"mustMention": ["24个月"], "requiredTools": ["Retrieve"]}},
                              {"id": "no-promise", "label": "不承诺补发", "source": "用户：不能承诺补发", "query": "积分没到账，你们一定会给我补上吧？",
                               "expected": {"mustNotMention": ["已补发"], "shouldRefuse": True}}]}
    proposal.update(changes)
    return proposal


def tool_results(request: dict) -> list[dict]:
    return [b["toolResult"] for b in request["messages"][-1]["content"] if "toolResult" in b]


# -- the console -------------------------------------------------------------------------------------------------------

class Client:
    def __init__(self, port: int):
        self.base, self.cookie = f"http://127.0.0.1:{port}/api/console", None

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", **({"Cookie": self.cookie} if self.cookie else {})}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Set-Cookie"):
                    self.cookie = resp.headers["Set-Cookie"].split(";")[0]
                if "event-stream" in resp.headers.get("Content-Type", ""):
                    return resp.status, [json.loads(line[6:]) for line in raw.decode().split("\n") if line.startswith("data: ")]
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    world = World()
    monkeypatch.setattr(assistant, "_sleep", lambda s: None)
    monkeypatch.setattr(agents, "time", types.SimpleNamespace(sleep=lambda s: None, time=__import__("time").time, monotonic=__import__("time").monotonic))
    verifications: list[dict] = []

    def fake_verification(console, workspace, body):  # the real one runs rounds on the agent and reads its traces
        verifications.append(dict(body))
        return console.jobs.start("verify", workspace, {"agentId": body["agentId"], "contractSet": body["contractSet"]},
                                  lambda job: {"robust": True, "holding": 2, "repeat": body["repeat"], "rounds": [],
                                               "contracts": [{"id": "points-expiry", "passes": 1, "rounds": 1, "holds": True, "failed": []}]}, label="验证")

    monkeypatch.setattr(evaluation, "start_verification", fake_verification)
    console = console_mod.Console(tmp_path / "data", session_factory=lambda **kw: world.session(), clients_factory=lambda cfg: None)
    console.workspaces.put({"id": "dev", "accountId": ACCOUNT, "region": REGION, "profile": "default"})
    console.store.write("skills_dev", {"reply-style": {"name": "reply-style", "current": "v0001", "description": "回答会员关于积分的任何问题时使用。", "agents": {},
                                                       "versions": [{"version": "v0001", "sha": hashlib.sha256(SKILL.encode()).hexdigest()[:16], "source": "sample"}]}})
    server = console_mod.create_server(console)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield types.SimpleNamespace(world=world, console=console, client=Client(server.server_address[1]), verifications=verifications)
    server.shutdown()
    server.server_close()


def new_conversation(c: Client) -> str:
    status, conv = c.call("POST", "/workspaces/dev/assistant/conversations", {})
    assert status == 201
    return conv["id"]


def turn(c: Client, cid: str, message: str):
    return c.call("POST", f"/workspaces/dev/assistant/conversations/{cid}/turns", {"message": message})


def detail(c: Client, cid: str) -> dict:
    status, body = c.call("GET", f"/workspaces/dev/assistant/conversations/{cid}")
    assert status == 200, body
    return body


def read_only(calls) -> bool:
    return all(op in assistant.READ_ONLY.get(service, ()) or service == "sts" for service, op in calls)


# -- turns -----------------------------------------------------------------------------------------------------------------

def test_a_turn_reads_the_workspace_through_its_tools_and_its_proposal_becomes_revision_1(env):
    c, w = env.client, env.world
    cid = new_conversation(c)
    w.script = [uses(("list_knowledge_bases", {}), ("list_skills", {}), text="先看看这个工作区有什么。"),
                uses(("search_knowledge_base", {"kbId": "KB1", "query": "积分有效期"}), ("describe_knowledge_base", {"kbId": "KB1"})),
                uses(("propose_agent", good())),
                says("这是第一版设计：积分 FAQ Agent，两条契约。")]
    mark = len(w.calls)
    status, events = turn(c, cid, "我需要一个回答积分问题的客服 Agent，积分多久过期一定要答准，不能承诺补发。")
    assert status == 200
    kinds = [e["type"] for e in events if e["type"] != "ping"]
    assert kinds[0] == "turn" and kinds[-2:] == ["proposal", "done"]
    assert [e["name"] for e in events if e["type"] == "tool"] == ["list_knowledge_bases", "list_skills", "search_knowledge_base", "describe_knowledge_base",
                                                                  "propose_agent"]
    assert next(e for e in events if e["type"] == "proposal")["revision"] == 1
    # the model got the tools' results back, the KB's passage among them
    assert "loyalty-rules" in tool_results(w.requests[1])[0]["content"][0]["text"]
    assert "24个月" in tool_results(w.requests[2])[0]["content"][0]["text"]
    assert json.loads(tool_results(w.requests[3])[0]["content"][0]["text"])["status"] == "accepted"
    assert w.requests[0]["messages"][-1]["content"][0]["text"].startswith("我需要一个")
    assert w.requests[0]["toolConfig"]["tools"][-1] == {"cachePoint": {"type": "default"}}  # tools and system prompt are cached across the loop
    # discussion never writes AWS: every call of the turn was a read on the allowlist
    assert w.since(mark) and read_only(w.since(mark)), w.since(mark)
    assert not [op for _s, op in w.since(mark) if op.startswith(WRITES)]
    conv = detail(c, cid)
    [rev] = conv["proposals"]
    assert (rev["revision"], rev["status"], rev["source"], len(rev["hash"])) == (1, "draft", "model", 64)
    assert rev["bindings"]["knowledgeBases"] == [{"id": "KB1", "name": "loyalty-rules", "status": "ACTIVE"}]
    assert rev["bindings"]["skills"] == [{"name": "reply-style", "version": "v0001", "sha": hashlib.sha256(SKILL.encode()).hexdigest()[:16],
                                          "uri": f"s3://{BUCKET}/skills/reply-style/v0001/"}]
    assert rev["bindings"]["toolNames"] == ["AgenticRetrieveStream", "Retrieve", "skills"]
    assert rev["content"] == good()  # stored verbatim
    assert "契约 1 · points-expiry（回答）积分有效期" in rev["text"] and "必须提到：24个月" in rev["text"]
    roles = [m["role"] for m in conv["messages"]]
    assert roles[0] == "user" and roles[-1] == "meta" and roles.count("tool") == 5 and "note" in roles
    meta = conv["messages"][-1]
    assert meta["modelCalls"] == 4 and meta["usage"]["cacheReadInputTokens"] == 80 and "bedrock-runtime:converse" in meta["operations"]
    assert conv["inFlight"] is None and conv["title"].startswith("我需要一个回答积分问题")


def test_validation_errors_go_back_to_the_model_and_only_its_last_submission_is_kept(env):
    c, w = env.client, env.world
    cid = new_conversation(c)
    bad = good(name="1bad", knowledgeBases=["NOPE"], skills=["missing-skill"], extra="x",
               contracts=[{"id": "lookup", "source": "s", "query": "我的积分还有多少？", "expected": {"requiredTools": ["lookup_points"]}},
                          {"id": "handoff", "source": "s", "query": "我要投诉", "expected": {"shouldEscalate": True}},
                          {"id": "phrase", "source": "s", "query": "能查别人吗", "expected": {"mustMention": ["只能查询本人的积分"], "shouldRefuse": True}},
                          {"id": "prefixed", "source": "s", "query": "q", "expected": {"forbiddenTools": ["loyalty-rules-kb1___Retrieve"]}},
                          {"id": "nothing", "source": "s", "query": "q", "expected": {"shouldRefuse": False}},
                          {"id": "typed", "source": "s", "query": "q", "expected": {"mustMention": "24个月"}}])
    w.script = [uses(("propose_agent", bad)), uses(("propose_agent", good())), says("改好了。")]
    status, events = turn(c, cid, "设计一个积分客服")
    assert status == 200
    verdict = json.loads(tool_results(w.requests[1])[0]["content"][0]["text"])
    assert tool_results(w.requests[1])[0]["status"] == "error" and verdict["status"] == "rejected"
    errors = "\n".join(verdict["errors"])
    for expected in ("name：", "extra", "NOPE", "missing-skill", "lookup_points", "shouldEscalate 需要 l1.escalationMarkers", "___ 后面的部分（Retrieve）",
                     "什么也不检查", "mustMention：非空字符串的列表"):
        assert expected in errors, (expected, errors)
    assert any("只能查询本人的积分" in x for x in verdict["warnings"])  # a literal phrase L1 would miss
    assert [e["summary"] for e in events if e["type"] == "toolResult"] == ["被拒绝", "通过，将存为第 1 版"]
    conv = detail(c, cid)
    assert [(p["revision"], p["status"]) for p in conv["proposals"]] == [(1, "draft")]  # the rejected attempt is no revision
    rejected = next(m for m in conv["messages"] if m["role"] == "tool")
    assert rejected["ok"] is False and any("NOPE" in e for e in rejected["errors"])


def test_a_gateways_tools_are_checked_by_the_names_l1_sees_and_unattachable_gateways_are_refused(env):
    c, w = env.client, env.world
    cid = new_conversation(c)
    gw = good(gateways=[{"gatewayId": "gw-hr", "toolName": "hrtools"}],
              contracts=good()["contracts"] + [{"id": "balance", "source": "s", "query": "我还有多少积分？", "expected": {"requiredTools": ["lookup_points"]}}])
    refused = good(gateways=[{"gatewayId": "gw-kb", "toolName": "kbgw"}, {"gatewayId": "gw-jwt", "toolName": "partner"}])
    w.script = [uses(("describe_gateway", {"gatewayId": "gw-hr"}), ("propose_agent", refused)), uses(("propose_agent", gw)), says("好了。")]
    turn(c, cid, "加上 HR 的查询工具")
    described = json.loads(tool_results(w.requests[1])[0]["content"][0]["text"])
    assert described["tools"] == [{"name": "hrtools___lookup_points", "l1Name": "lookup_points", "description": "A member's balance"}]
    errors = "\n".join(json.loads(tool_results(w.requests[1])[1]["content"][0]["text"])["errors"])
    assert "knowledge-base Gateway" in errors and "AWS_IAM only" in errors
    [rev] = detail(c, cid)["proposals"]
    assert rev["status"] == "draft" and "lookup_points" in rev["bindings"]["toolNames"]
    assert rev["bindings"]["gateways"][0]["arn"] == w.gateways["gw-hr"]["gatewayArn"]


def test_a_long_name_with_kbs_fits_allowed_tools_and_a_gateway_tool_name_that_cannot_is_refused(env):
    from workshop_customizer.console import kb

    c = env.client
    cid = new_conversation(c)
    edits = f"/workspaces/dev/assistant/conversations/{cid}/proposals"
    # live 2026-10-02: a 25-character agent's deep search was 65 characters and UpdateHarness refused it; kb now shortens
    # a long agent's target, so the proposal is valid and every entry it will get fits
    status, long = c.call("POST", edits, {"content": good(name="points_faq_for_the_family")})
    assert status == 201 and long["status"] == "draft", long.get("errors")
    assert all(len(f"@{kb.TOOL}/{t}") <= 64 for t in kb.tool_names("points_faq_for_the_family", [{"id": "KBEXAMPLE1", "name": "x" * 60}]))
    status, gw = c.call("POST", edits, {"content": good(gateways=[{"gatewayId": "gw-hr", "toolName": "h" * 62}])})
    assert gw["status"] == "invalid" and any("最多 61 个" in e for e in gw["errors"])


def test_one_turn_at_a_time_per_conversation(env):
    c, w = env.client, env.world
    cid = new_conversation(c)
    w.gate = threading.Event()
    w.script = [says("第一轮"), says("第三轮")]
    first: dict = {}
    runner = threading.Thread(target=lambda: first.update(out=turn(c, cid, "第一个问题")))
    runner.start()
    assert w.entered.wait(10)
    status, body = turn(c, cid, "第二个问题")
    assert status == 409 and body["activeTurn"] == 1  # refused before any stream opens
    assert detail(c, cid)["inFlight"]["turn"] == 1
    w.gate.set()
    runner.join(10)
    assert first["out"][0] == 200 and first["out"][1][-1]["type"] == "done"
    status, events = turn(c, cid, "第三个问题")
    assert status == 200 and events[-1]["type"] == "done"
    conv = detail(c, cid)
    assert [m["text"] for m in conv["messages"] if m["role"] == "user"] == ["第一个问题", "第三个问题"] and conv["turns"] == 2


def test_a_failed_turn_is_kept_as_unfinished_releases_its_claim_and_is_replayed_as_such(env):
    c, w = env.client, env.world
    cid = new_conversation(c)

    def boom(request):
        raise RuntimeError("An error occurred (ThrottlingException) when calling the Converse operation: too many requests")

    w.script = [boom, says("好的，继续。")]
    status, events = turn(c, cid, "第一个问题")
    assert status == 200 and events[-1]["type"] == "error" and "ThrottlingException" in events[-1]["error"]
    conv = detail(c, cid)
    assert conv["inFlight"] is None and conv["messages"][-1]["role"] == "error" and conv["proposals"] == []
    status, events = turn(c, cid, "再试一次")
    assert events[-1]["type"] == "done"
    replay = w.requests[-1]["messages"]
    assert [m["role"] for m in replay] == ["user", "assistant", "user"] and "这一轮没有完成：RuntimeError" in replay[1]["content"][0]["text"]


def test_a_claim_left_by_a_dead_process_is_taken_over(env):
    c, w = env.client, env.world
    cid = new_conversation(c)
    key = f"assistant_dev_{cid}"
    env.console.store.update(key, None, lambda conv: {**conv, "turns": 1, "inFlight": {"turn": 1, "token": "gone", "boot": "an-earlier-process"}})
    w.script = [says("接上了。")]
    status, events = turn(c, cid, "还在吗？")
    assert status == 200 and events[-1]["type"] == "done"


# -- revisions -----------------------------------------------------------------------------------------------------------

def test_revisions_are_monotonic_and_unique_under_concurrent_edits(env):
    c = env.client
    cid = new_conversation(c)
    results = []
    threads = [threading.Thread(target=lambda i=i: results.append(c.call("POST", f"/workspaces/dev/assistant/conversations/{cid}/proposals",
                                                                          {"content": good(systemPrompt=f"版本 {i}")}))) for i in range(8)]
    [t.start() for t in threads]
    [t.join(20) for t in threads]
    assert sorted(s for s, _ in results) == [201] * 8
    assert sorted(r["revision"] for _, r in results) == list(range(1, 9))
    conv = detail(c, cid)
    assert [p["revision"] for p in conv["proposals"]] == list(range(1, 9))
    assert [p["status"] for p in conv["proposals"]] == ["superseded"] * 7 + ["draft"]
    assert len({p["hash"] for p in conv["proposals"]}) == 8
    status, body = c.call("POST", f"/workspaces/dev/assistant/conversations/{cid}/proposals", {"content": good(), "base": 3})
    assert status == 409 and body["currentRevision"] == 8  # an edit of a revision that is no longer the latest


def test_an_edit_is_a_new_revision_with_a_diff_and_the_next_turn_works_from_it(env):
    c, w = env.client, env.world
    cid = new_conversation(c)
    w.script = [uses(("propose_agent", good())), says("第一版。")]
    turn(c, cid, "设计一个积分客服")
    status, edited = c.call("POST", f"/workspaces/dev/assistant/conversations/{cid}/proposals",
                            {"base": 1, "content": good(systemPrompt="你是拾光家居的积分客服。回答要简短。", verificationRounds=3)})
    assert status == 201 and (edited["revision"], edited["status"], edited["source"]) == (2, "draft", "edit")
    status, broken = c.call("POST", f"/workspaces/dev/assistant/conversations/{cid}/proposals", {"base": 2, "content": good(model="no such model!")})
    assert status == 201 and broken["status"] == "invalid" and any("model" in e for e in broken["errors"])  # stored verbatim, with its errors
    conv = detail(c, cid)
    second = conv["proposals"][1]
    assert "-  你是拾光家居的积分客服。只根据知识库回答，不承诺补发积分。" in second["diff"] and "+验证轮数：3" in second["diff"]
    w.script = [says("看到你的修改了。")]
    turn(c, cid, "我改了 Prompt，你看看")
    request = w.requests[-1]
    assert "revision 3 (invalid, edited by local)" in request["system"][1]["text"] and "no such model!" in request["system"][1]["text"]
    assert "[控制台] local 手动编辑了提案，存为第 2 版（有效）" in request["messages"][-1]["content"][0]["text"]


# -- approval ------------------------------------------------------------------------------------------------------------

def approve(c: Client, cid: str, revision: int, digest: str, **extra):
    return c.call("POST", f"/workspaces/dev/assistant/conversations/{cid}/approve", {"revision": revision, "hash": digest, **extra})


def proposed(env, **changes) -> tuple[str, dict]:
    cid = new_conversation(env.client)
    env.world.script = [uses(("propose_agent", good(**changes))), says("第一版。")]
    turn(env.client, cid, "设计一个积分客服")
    return cid, detail(env.client, cid)["proposals"][-1]


def finished(env, cid: str, revision: int) -> dict:
    return wait(lambda: next((p["approval"] for p in detail(env.client, cid)["proposals"] if p["revision"] == revision
                              and p["approval"] and p["approval"]["effectiveStatus"] != "running"), None))


def test_approval_names_an_exact_revision_and_creates_the_agent_its_contracts_and_a_verification(env):
    c, w = env.client, env.world
    cid, first = proposed(env)
    status, second = c.call("POST", f"/workspaces/dev/assistant/conversations/{cid}/proposals", {"base": 1, "content": good(verificationRounds=2)})
    assert approve(c, cid, 1, first["hash"])[0] == 409  # superseded by revision 2
    assert approve(c, cid, 2, first["hash"])[0] == 409  # revision 2, but not what was reviewed
    assert approve(c, cid, 7, second["hash"])[0] == 404
    mark = len(w.calls)
    status, out = approve(c, cid, 2, second["hash"])
    assert status == 202 and out["approval"]["revision"] == 2
    done = finished(env, cid, 2)
    assert done["effectiveStatus"] == "succeeded", done
    assert [(s["key"], s["status"]) for s in done["steps"]] == [("harness", "done"), ("ready", "done"), ("kb:KB1", "done"), ("skill:reply-style", "done"),
                                                              ("contracts", "done"), ("verification", "done")]
    [(hid, harness)] = w.harnesses.items()
    assert harness["harnessName"] == "points_faq" and harness["tags"] == {"adlc:console": "1"} and done["agent"]["id"] == hid
    assert harness["model"] == {"bedrockModelConfig": {"modelId": HAIKU}} and "points_faq-console-harness" in w.roles
    assert set(w.targets["gw-kb"]) == {"loyalty-rules-kb1", "agentic-points-faq"}  # kb.attach: the KB's Retrieve and the agent's deep search
    assert [t["name"] for t in harness["tools"]] == ["adlckb"] and harness["skills"] == [{"s3": {"uri": f"s3://{BUCKET}/skills/reply-style/v0001/"}}]
    saved = evaluation.contract_set(env.console.store, "dev", done["contractSet"])
    assert [x["id"] for x in saved["contracts"]] == ["points-expiry", "no-promise"] and saved["source"] == f"assistant:{cid}:r2"
    assert env.verifications == [{"contractSet": done["contractSet"], "agentKind": "harness", "agentId": hid, "repeat": 2, "panel": False}]
    verified = wait(lambda: (lambda v: v if v["status"] == "succeeded" else None)(detail(c, cid)["proposals"][1]["approval"]["verificationInfo"]))
    assert verified["result"]["robust"] is True and verified["result"]["contracts"][0]["id"] == "points-expiry"
    writes = [(s, op) for s, op in w.since(mark) if op.startswith(WRITES)]
    assert ("bedrock-agentcore-control", "create_harness") in writes and ("iam", "put_role_policy") in writes
    # a repeated approval returns the recorded outcome and does nothing again
    status, again = approve(c, cid, 2, second["hash"])
    assert status == 200 and again["recorded"] is True and again["approval"]["job"] == done["job"]
    assert sum(1 for _s, op in w.calls if op == "create_harness") == 1
    assert detail(c, cid)["agents"] == ["points_faq"]


def test_drift_after_review_is_refused_before_anything_is_written(env):
    c, w = env.client, env.world
    cid, rev = proposed(env)
    w.kbs["KB1"]["name"] = "loyalty-rules-v2"
    mark = len(w.calls)
    status, body = approve(c, cid, 1, rev["hash"])
    assert status == 409 and any("知识库 KB1：name loyalty-rules → loyalty-rules-v2" in x for x in body["changes"])
    w.kbs["KB1"]["name"] = "loyalty-rules"
    w.s3[(BUCKET, "skills/reply-style/v0001/SKILL.md")] = SKILL.replace("第一句给结论", "先寒暄").encode()
    status, body = approve(c, cid, 1, rev["hash"])
    assert status == 409 and any("在 S3 上的内容和审阅时不一样了" in x for x in body["changes"])
    w.s3[(BUCKET, "skills/reply-style/v0001/SKILL.md")] = SKILL.encode()
    w.harnesses["points_faq-9999"] = {"harnessName": "points_faq", "harnessId": "points_faq-9999", "arn": "arn:other", "status": "READY"}
    status, body = approve(c, cid, 1, rev["hash"])
    assert status == 409 and any("已经有一个叫 points_faq 的 Harness" in x for x in body["errors"])
    assert read_only(w.since(mark)) and not [op for _s, op in w.since(mark) if op.startswith(WRITES)]
    assert detail(c, cid)["proposals"][0]["status"] == "draft"  # still approvable once the drift is gone
    del w.harnesses["points_faq-9999"]
    assert approve(c, cid, 1, rev["hash"])[0] == 202
    assert finished(env, cid, 1)["effectiveStatus"] == "succeeded"


def test_an_invalid_revision_cannot_be_approved(env):
    c, w = env.client, env.world
    cid = new_conversation(c)
    w.script = [uses(("propose_agent", good(knowledgeBases=["NOPE"]))), says("我还缺一个知识库。")]
    turn(c, cid, "设计一个积分客服")
    [rev] = detail(c, cid)["proposals"]
    assert rev["status"] == "invalid" and rev["bindings"] is None
    status, body = approve(c, cid, 1, rev["hash"])
    assert status == 409 and "invalid" in body["error"] and not w.harnesses


def test_a_failed_approval_resumes_from_the_step_that_failed(env):
    c, w = env.client, env.world
    cid, rev = proposed(env)
    w.fail_target = 1
    assert approve(c, cid, 1, rev["hash"])[0] == 202
    failed = finished(env, cid, 1)
    assert failed["effectiveStatus"] == "failed" and "ThrottlingException" in failed["error"]
    assert [(s["key"], s["status"]) for s in failed["steps"]] == [("harness", "done"), ("ready", "done"), ("kb:KB1", "failed")]
    status, recorded = approve(c, cid, 1, rev["hash"])
    assert status == 200 and recorded["recorded"]  # without resume: the recorded outcome
    status, resumed = approve(c, cid, 1, rev["hash"], resume=True)
    assert status == 202
    done = finished(env, cid, 1)
    assert done["effectiveStatus"] == "succeeded" and len(done["jobs"]) == 2
    assert sum(1 for _s, op in w.calls if op == "create_harness") == 1  # the Harness it had made is the one it finished


# -- access --------------------------------------------------------------------------------------------------------------

def test_conversations_are_bound_to_their_owner_and_workspace_and_an_admin_may_share_one(env):
    console = env.console
    port = env.client.base.split(":")[2].split("/")[0]
    console.workspaces.put({"id": "prod", "accountId": ACCOUNT, "region": REGION, "profile": "default"})
    admin, ann, bob = Client(int(port)), Client(int(port)), Client(int(port))
    assert admin.call("POST", "/users", {"username": "admin", "password": "0123456789", "role": "admin"})[0] == 201
    admin.call("POST", "/login", {"username": "admin", "password": "0123456789"})
    for name in ("ann", "bob"):
        assert admin.call("POST", "/users", {"username": name, "password": "0123456789", "role": "member", "workspaces": ["dev"]})[0] == 201
    ann.call("POST", "/login", {"username": "ann", "password": "0123456789"})
    bob.call("POST", "/login", {"username": "bob", "password": "0123456789"})
    mine = new_conversation(ann)
    assert bob.call("GET", f"/workspaces/dev/assistant/conversations/{mine}")[0] == 404
    assert bob.call("GET", "/workspaces/dev/assistant/conversations")[1]["conversations"] == []
    assert admin.call("POST", f"/workspaces/dev/assistant/conversations/{mine}/share", {"shared": True})[0] == 404  # not the admin's to share
    assert ann.call("GET", f"/workspaces/prod/assistant/conversations/{mine}")[0] == 403  # no grant to prod
    shared = new_conversation(admin)
    assert bob.call("POST", f"/workspaces/dev/assistant/conversations/{shared}/share", {"shared": True})[0] == 403  # admins only
    assert admin.call("POST", f"/workspaces/dev/assistant/conversations/{shared}/share", {"shared": True})[1]["shared"] is True
    assert [x["id"] for x in bob.call("GET", "/workspaces/dev/assistant/conversations")[1]["conversations"]] == [shared]
    env.world.script = [says("你好，bob。")]
    assert turn(bob, shared, "我也想参与")[1][-1]["type"] == "done"  # a shared conversation is open to the workspace's members
    assert bob.call("DELETE", f"/workspaces/dev/assistant/conversations/{shared}")[0] == 404  # deleting stays with the owner
    assert admin.call("DELETE", f"/workspaces/dev/assistant/conversations/{shared}")[0] == 200
    # a recreated username is someone else: ann's conversation stays the first ann's
    threading.Event().wait(1.1)  # creation times have a resolution of one second
    admin.call("DELETE", "/users/ann")
    admin.call("POST", "/users", {"username": "ann", "password": "9876543210", "role": "member", "workspaces": ["dev"]})
    again = Client(int(port))
    again.call("POST", "/login", {"username": "ann", "password": "9876543210"})
    assert again.call("GET", f"/workspaces/dev/assistant/conversations/{mine}")[0] == 404
    assert again.call("GET", "/workspaces/dev/assistant/conversations")[1]["conversations"] == []


# -- units -----------------------------------------------------------------------------------------------------------------

def test_the_read_only_session_refuses_every_write_before_it_is_sent():
    world = World()
    ro = assistant.ReadOnlySession(world.session(), REGION)
    ctl = ro.client("bedrock-agentcore-control")
    assert ctl.list_harnesses()["harnesses"] == [] and ro.calls == ["bedrock-agentcore-control:list_harnesses"]
    for call in (lambda: ctl.create_harness(harnessName="x"), lambda: ctl.update_harness(harnessId="x"), lambda: ro.client("iam"),
                 lambda: ro.client("s3").put_object(Bucket="b", Key="k", Body=b""), lambda: ro.client("bedrock-agentcore")):
        with pytest.raises(assistant.ReadOnlyViolation):
            call()
    assert ("bedrock-agentcore-control", "create_harness") not in world.calls and ("s3", "put_object") not in world.calls


def test_contract_checks_catch_what_l1_could_not_decide():
    names, _ = assistant.agent_tools(True, True, [{"tools": ["hrtools___lookup_points"]}])
    assert names == {"Retrieve", "AgenticRetrieveStream", "skills", "lookup_points"}
    ok = [{"id": "a", "source": "s", "query": "q", "expected": {"mustMentionAnyOf": [["24个月", "两年"]], "requiredTools": ["Retrieve"]}}]
    assert assistant.check_contracts(ok, names, False, {}) == ([], [])
    cases = [
        ({"requiredTools": ["Retrieve"], "forbiddenTools": ["Retrieve"]}, {}, "同时在"),
        ({"shouldEscalate": True}, {}, "escalationMarkers"),
        ({"mustMentionAnyOf": [[]]}, {}, "列表的列表"),
        ({"shouldRefuse": "yes"}, {}, "true 或 false"),
        ({"mustSay": ["x"]}, {}, "不认识"),
    ]
    for expected, l1cfg, message in cases:
        errors, _ = assistant.check_contracts([{"id": "a", "source": "s", "query": "q", "expected": expected}], names, False, l1cfg)
        assert any(message in e for e in errors), (expected, errors)
    assert assistant.check_contracts([{"id": "a", "source": "s", "query": "q", "expected": {"shouldEscalate": True}}], names, False,
                                     {"escalationMarkers": ["人工客服"]}) == ([], [])
    errors, _ = assistant.check_contracts([{"id": "a", "query": "q", "expected": {"mustMention": ["x"]}}, {"id": "a", "query": "", "expected": {"x": 1}}],
                                          names, False, {})
    assert any(".id" in e for e in errors) and any(".query" in e for e in errors)
    _, warnings = assistant.check_contracts([{"id": "a", "query": "q", "expected": {"mustMention": ["到期当月月底清零"]}}], names, False, {})
    assert any("没写 source" in x for x in warnings) and any("到期当月月底清零" in x for x in warnings)
    _, warnings = assistant.check_contracts([{"id": "a", "source": "s", "query": "q", "expected": {"mustMentionAnyOf": [["没有找到", "查不到", "不知道"]]}}],
                                            names, False, {})
    assert len(warnings) == 1 and "「没有找到」以否定开头" in warnings[0]  # live 2026-10-02: the reply said 没有在知识库中找到
    assert assistant.contract_kind({"shouldRefuse": True, "mustNotMention": ["x"]}) == "refusal"
    assert assistant.contract_kind({"forbiddenTools": ["x"]}) == "tools" and assistant.contract_kind({"mustNotMention": ["x"]}) == "boundary"


def test_the_replay_is_bounded_alternates_and_says_what_it_left_out():
    rows = []
    for t in range(1, 41):
        rows += [{"turn": t, "role": "user", "text": f"问题 {t} " + "长" * 3000}, {"turn": t, "role": "tool", "name": "list_skills"},
                 {"turn": t, "role": "assistant", "text": f"回答 {t}"}, {"turn": t, "role": "note", "kind": "proposal", "replay": False, "text": "第 N 版"}]
    rows += [{"turn": 40, "role": "note", "kind": "edit", "replay": True, "text": "[控制台] ann 手动编辑了提案"}, {"turn": 41, "role": "user", "text": "现在呢？"}]
    conv = {"workspace": "dev", "owner": "ann", "messages": rows, "proposals": [], "approvals": {}}
    messages, omitted = assistant.compose(conv, 41)
    assert [m["role"] for m in messages] == ["user", "assistant"] * ((len(messages) - 1) // 2) + ["user"]
    assert sum(len(m["content"][0]["text"]) for m in messages) <= assistant.REPLAY_CHARS and omitted > 0
    assert messages[-1]["content"][0]["text"] == "[控制台] ann 手动编辑了提案\n\n现在呢？" and messages[-2]["content"][0]["text"] == "回答 40"
    assert "第 N 版" not in json.dumps(messages, ensure_ascii=False)
    assert f"{omitted} earlier turn(s)" in assistant.system_prompt(conv, {"region": REGION}, omitted)[1]["text"]


def test_drift_is_described_field_by_field():
    pinned = {"knowledgeBases": [{"id": "K", "name": "a", "status": "ACTIVE"}], "skills": [{"name": "s", "version": "v0001", "sha": "1", "uri": "u"}],
              "gateways": [], "toolNames": ["Retrieve"]}
    assert assistant.binding_changes(pinned, copy.deepcopy(pinned)) == []
    live = copy.deepcopy(pinned)
    live["knowledgeBases"] = []
    live["skills"][0]["sha"] = "2"
    assert assistant.binding_changes(pinned, live) == ["知识库 K 不见了", "技能 s：sha 1 → 2"]
