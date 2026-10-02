"""engine console.kb: managed KB sources, the native Gateway retrieval targets, attach and detach (no AWS)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer.console import kb  # noqa: E402
from workshop_customizer.console.store import Store  # noqa: E402

KB = {"id": "KBEXAMPLE1", "name": "loyalty-rules", "description": "积分规则"}


def test_target_and_tool_names_follow_the_kb_and_the_agent():
    assert kb.retrieve_target("KBEXAMPLE1", "Loyalty Rules (v2)") == "loyalty-rules-v2-kbexample1"
    assert kb.agentic_target("hr_agent") == "agentic-hr-agent"
    assert kb.tool_names("hr_agent", [KB]) == ["loyalty-rules-kbexample1___Retrieve", "agentic-hr-agent___AgenticRetrieveStream"]
    assert kb.tool_names("hr_agent", []) == []


def test_the_gateway_role_retrieves_only_from_attached_kbs():
    policy = kb.gateway_role_policy("111122223333", "us-west-2", ["B", "A", "A"])
    retrieve, agentic = policy["Statement"]
    assert retrieve["Resource"] == ["arn:aws:bedrock:us-west-2:111122223333:knowledge-base/A", "arn:aws:bedrock:us-west-2:111122223333:knowledge-base/B"]
    assert agentic == {"Sid": "AgenticRetrieve", "Effect": "Allow", "Action": "bedrock:AgenticRetrieveStream", "Resource": "*"}
    assert kb.gateway_role_policy("111122223333", "us-west-2", [])["Statement"][0]["Resource"][0].endswith("knowledge-base/none")


def test_a_managed_sources_parameters_are_read_back_from_a_json_string():
    params = {"type": "S3", "connectionConfiguration": {"bucketName": "b"}, "filterConfiguration": {"inclusionPrefixes": ["kb/X/"]}}
    ds = {"dataSourceConfiguration": {"managedKnowledgeBaseConnectorConfiguration": {"connectorParameters": json.dumps(params)}}}
    assert kb._location(ds) == ("b", "kb/X/")  # live 2026-10-01: sent as a document, returned as a string
    assert kb._location({"dataSourceConfiguration": {"s3Configuration": {"bucketArn": "arn:aws:s3:::own", "inclusionPrefixes": ["p/"]}}}) == ("own", "p/")


class Ctl:
    """bedrock-agentcore-control: one READY Gateway, its targets, and one Harness."""

    def __init__(self, console_harness: bool = True):
        self.targets: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.harness = {"harnessId": "h-1", "harnessName": "hr_agent", "arn": "arn:h-1", "status": "READY", "executionRoleArn": "arn:aws:iam::1:role/adlc-console/hr-role",
                        "tools": [{"type": "agentcore_gateway", "name": "hr", "config": {"agentCoreGateway": {"gatewayArn": "arn:gw-hr"}}}],
                        "allowedTools": ["@hr/*", "skills"]}
        self.console_harness = console_harness

    def list_gateways(self, **kw):
        return {"items": [{"name": kb.GATEWAY, "gatewayId": "gw-1"}]}

    def get_gateway(self, gatewayIdentifier):
        return {"status": "READY", "gatewayArn": "arn:gw-kb"}

    def list_gateway_targets(self, gatewayIdentifier, **kw):
        return {"items": [dict(t) for t in self.targets.values()]}

    def get_gateway_target(self, gatewayIdentifier, targetId):
        return next(t for t in self.targets.values() if t["targetId"] == targetId)

    def create_gateway_target(self, **request):
        self.calls.append(("create", request))
        self.targets[request["name"]] = {"name": request["name"], "targetId": f"t-{len(self.targets)}", "status": "READY", "request": request}
        return {"targetId": self.targets[request["name"]]["targetId"]}

    def update_gateway_target(self, **request):
        self.calls.append(("update", request))
        self.targets[request["name"]]["request"] = request
        return {}

    def delete_gateway_target(self, gatewayIdentifier, targetId):
        self.calls.append(("delete", {"targetId": targetId}))
        self.targets = {n: t for n, t in self.targets.items() if t["targetId"] != targetId}

    def get_harness(self, harnessId):
        return {"harness": json.loads(json.dumps(self.harness))}

    def list_tags_for_resource(self, resourceArn):
        return {"tags": {"adlc:console": "1"} if self.console_harness else {}}

    def update_harness(self, harnessId, **changes):
        self.calls.append(("update_harness", changes))
        self.harness.update(changes)
        return {"harness": {"status": "UPDATING"}}


class Iam:
    def __init__(self):
        self.policies: dict[tuple[str, str], dict] = {}

    def get_role(self, RoleName):  # the console's own roles, on its path
        return {"Role": {"Arn": f"arn:aws:iam::1:role/adlc-console/{RoleName}", "Path": "/adlc-console/"}}

    def list_role_tags(self, RoleName):
        return {"Tags": [{"Key": "adlc:console", "Value": "1"}]}

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.policies[(RoleName, PolicyName)] = json.loads(PolicyDocument)

    def delete_role_policy(self, RoleName, PolicyName):
        if (RoleName, PolicyName) not in self.policies:
            raise RuntimeError("NoSuchEntity")
        del self.policies[(RoleName, PolicyName)]


class Agent:
    """bedrock-agent: the one KB, with no sources."""

    def get_knowledge_base(self, knowledgeBaseId):
        return {"knowledgeBase": {"knowledgeBaseId": knowledgeBaseId, "name": KB["name"], "description": KB["description"], "status": "ACTIVE",
                                  "knowledgeBaseArn": f"arn:kb/{knowledgeBaseId}", "knowledgeBaseConfiguration": {"type": "MANAGED"}}}

    def list_tags_for_resource(self, resourceArn):
        return {"tags": {"adlc:console": "1"}}

    def list_data_sources(self, knowledgeBaseId):
        return {"dataSourceSummaries": []}

    def delete_knowledge_base(self, knowledgeBaseId):
        self.deleted = knowledgeBaseId


class Console:
    def __init__(self, root: Path, ctl: Ctl):
        self.store, self.ctl, self.iam, self.agent = Store(root), ctl, Iam(), Agent()
        clients = {"bedrock-agentcore-control": ctl, "iam": self.iam, "bedrock-agent": self.agent}
        session = type("S", (), {"client": lambda _s, name, region_name=None, **kw: clients[name]})()
        ws = {"id": "dev", "accountId": "111122223333", "region": "us-west-2"}
        self.workspaces = type("W", (), {"get": lambda _s, wid: ws, "verify": lambda _s, wid: {}, "session": lambda _s, wid: session})()


def test_attach_gives_the_agent_exactly_its_own_kb_tools_and_detach_takes_them_back(tmp_path):
    ctl = Ctl()
    console = Console(tmp_path / "console", ctl)
    out = kb.attach(console, "dev", KB["id"], {"harnessId": "h-1"})
    assert out["tools"] == ["loyalty-rules-kbexample1___Retrieve", "agentic-hr-agent___AgenticRetrieveStream"]
    retrieve = ctl.targets["loyalty-rules-kbexample1"]["request"]["targetConfiguration"]["mcp"]["connector"]
    assert retrieve["source"] == {"connectorId": "bedrock-knowledge-bases"}
    assert retrieve["configurations"] == [{"name": "Retrieve", "description": "积分规则", "parameterValues": {"knowledgeBaseId": "KBEXAMPLE1"}}]
    agentic = ctl.targets["agentic-hr-agent"]["request"]["targetConfiguration"]["mcp"]["connector"]["configurations"][0]
    assert agentic["parameterValues"]["retrievers"] == [{"description": "积分规则", "configuration": {"knowledgeBase": {"knowledgeBaseId": "KBEXAMPLE1"}}}]
    # the agent's own tools stay; the KB Gateway is added with only this agent's targets allowed
    assert [t["name"] for t in ctl.harness["tools"]] == ["hr", "adlckb"]
    assert ctl.harness["allowedTools"] == ["@hr/*", "skills", "@adlckb/loyalty-rules-kbexample1___Retrieve", "@adlckb/agentic-hr-agent___AgenticRetrieveStream"]
    assert console.iam.policies[("hr-role", "adlc-console-kb-gateway")]["Statement"][0]["Resource"] == "arn:gw-kb"
    assert console.iam.policies[(kb.GATEWAY_ROLE, "kb-retrieve")]["Statement"][0]["Resource"] == ["arn:aws:bedrock:us-west-2:111122223333:knowledge-base/KBEXAMPLE1"]
    assert kb.attached(console, "dev", KB["id"]) == ["h-1"]

    again = kb.attach(console, "dev", KB["id"], {"harnessId": "h-1"})  # idempotent: no second Retrieve target, the tools unchanged
    assert again["tools"] == out["tools"] and sum(1 for c, r in ctl.calls if c == "create") == 2
    assert ctl.harness["allowedTools"].count("@adlckb/loyalty-rules-kbexample1___Retrieve") == 1

    off = kb.detach(console, "dev", KB["id"], {"harnessId": "h-1"})
    assert off["tools"] == [] and "agentic-hr-agent" not in ctl.targets and "loyalty-rules-kbexample1" in ctl.targets  # the KB's target is shared
    assert [t["name"] for t in ctl.harness["tools"]] == ["hr"] and ctl.harness["allowedTools"] == ["@hr/*", "skills"]
    assert ("hr-role", "adlc-console-kb-gateway") not in console.iam.policies and kb.attached(console, "dev", KB["id"]) == []


def test_another_teams_agent_is_changed_only_when_acknowledged(tmp_path):
    ctl = Ctl(console_harness=False)
    console = Console(tmp_path / "console", ctl)
    with pytest.raises(kb.KnowledgeError, match="acknowledged"):
        kb.attach(console, "dev", KB["id"], {"harnessId": "h-1"})
    assert not ctl.calls
    assert kb.attach(console, "dev", KB["id"], {"harnessId": "h-1", "acknowledged": True})["harness"] == "hr_agent"


def test_deleting_a_kb_takes_it_off_its_agents_and_removes_its_target(tmp_path):
    ctl = Ctl()
    console = Console(tmp_path / "console", ctl)
    kb.attach(console, "dev", KB["id"], {"harnessId": "h-1"})
    out = kb.delete_kb(console, "dev", KB["id"])
    assert out == {"deleted": KB["id"], "detachedFrom": ["hr_agent"]}
    assert ctl.targets == {} and console.agent.deleted == KB["id"] and [t["name"] for t in ctl.harness["tools"]] == ["hr"]


def test_a_long_agent_name_keeps_its_allowed_tools_within_64_characters():
    for name in ("hr_agent", "adlc_probe_assistant_30f1a9", "a" * 48):
        entries = [f"@{kb.TOOL}/{t}" for t in kb.tool_names(name, [{"id": "KBEXAMPLE1", "name": "x" * 60}])]
        assert all(len(e) <= 64 for e in entries), entries  # live 2026-10-02: UpdateHarness refuses a longer entry
    assert kb.agentic_target("hr_agent") == "agentic-hr-agent" and kb.agentic_target("a" * 48) != kb.agentic_target("a" * 47)


def test_the_gateway_role_keeps_the_kbs_other_agents_read_and_a_refused_update_restores_the_targets(tmp_path):
    ctl = Ctl()
    console = Console(tmp_path / "console", ctl)
    other = {"name": "other-kbexample1", "targetId": "t-other", "status": "READY", "request": {
        "targetConfiguration": {"mcp": {"connector": {"configurations": [{"name": "Retrieve", "parameterValues": {"knowledgeBaseId": "OTHERKB123"}}]}}}}}
    ctl.targets["other-kbexample1"] = other
    ctl.get_gateway_target = lambda gatewayIdentifier, targetId: {**next(t for t in ctl.targets.values() if t["targetId"] == targetId),
                                                                  **next(t for t in ctl.targets.values() if t["targetId"] == targetId).get("request", {})}
    kb.attach(console, "dev", KB["id"], {"harnessId": "h-1"})  # this console's store knows nothing of OTHERKB123
    granted = console.iam.policies[(kb.GATEWAY_ROLE, "kb-retrieve")]["Statement"][0]["Resource"]
    assert "arn:aws:bedrock:us-west-2:111122223333:knowledge-base/OTHERKB123" in granted
    kb.detach(console, "dev", KB["id"], {"harnessId": "h-1"})
    ctl.update_harness = lambda harnessId, **changes: (_ for _ in ()).throw(RuntimeError("ValidationException: allowedTools"))
    with pytest.raises(RuntimeError):
        kb.attach(console, "dev", KB["id"], {"harnessId": "h-1"})
    assert "agentic-hr-agent" not in ctl.targets  # the refused attach did not leave its deep-search target
