"""engine console.registry: registries, the three kinds of record in the GA Agent Registry's shapes, submit → approve,
a verification attached as evidence, search, and the console-only rules. No AWS: an in-memory registry that keeps the
rules the live service showed on 2026-10-01 and records every request."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer.console import registry as rg  # noqa: E402

REGION, ACCOUNT = "us-west-2", "111122223333"
RID = "CPsFRMA0KTcZSByo"
REG_ARN = f"arn:aws:agent-registry:{REGION}:{ACCOUNT}:registry/{RID}"
T = datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)
CONSOLE = {"adlc:console": "1"}
HARNESS_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:harness/hr_agent-1"
TOOLS = [{"name": "retrieve_policy", "description": "Search the HR policies",
          "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]
SKILL_MD = "---\nname: leave-calculator\ndescription: Calculate leave balances\n  and plan leave\nversion: 1.2.0\n---\n# Leave calculator\n"


def aws_error(code: str, status: int, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": status}}, "Op")


def unwrap(value):
    """UpdateRegistryRecord's PATCH wrappers, applied (an empty wrapper unsets)."""
    if isinstance(value, dict):
        if set(value) == {"optionalValue"}:
            return unwrap(value["optionalValue"])
        return {k: unwrap(v) for k, v in value.items() if v != {}}
    return value


class Control:
    """agent-registry-control as it behaved live: records settle to DRAFT, an edit returns them to DRAFT, DEPRECATED is
    terminal, only APPROVED / REJECTED / DEPRECATED are targets, a registry holding records is not deleted."""

    def __init__(self, auto: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.registry = {"name": "adlc-catalog", "registryId": RID, "registryArn": REG_ARN, "status": "READY",
                         "discoveryConfiguration": {"authorizerType": "AWS_IAM"},
                         "approvalConfiguration": {"autoApprovalRules": ["APPROVE_ALL"] if auto else []}, "createdAt": T, "updatedAt": T}
        self.tags: dict[str, dict] = {REG_ARN: dict(CONSOLE)}
        self.records: dict[str, dict] = {}
        self.updating: dict[str, int] = {}
        self.conflicts = 0

    def _log(self, op: str, **kw):
        self.calls.append((op, kw))

    def sent(self, op: str) -> list[dict]:
        return [kw for o, kw in self.calls if o == op]

    def add(self, name: str, rtype: str, descriptors: dict, *, status: str = "DRAFT", console: bool = True) -> str:
        """A record already in the registry (not a request under test)."""
        out = self.create_registry_record(RID, name=name, recordType=rtype, descriptors=descriptors, recordVersion="1.0.0",
                                          tags=dict(CONSOLE) if console else {})
        self.calls.pop()
        rec = out["recordArn"].rsplit("/", 1)[-1]
        self.records[rec]["status"] = status
        return rec

    def list_registries(self, **kw):
        self._log("list_registries", **kw)
        if "nextToken" not in kw:
            return {"registries": [], "nextToken": "page-2"}  # live: an empty first page that carries a token
        return {"registries": [{k: v for k, v in self.registry.items() if k != "approvalConfiguration"}]}

    def get_registry(self, registryId):
        self._log("get_registry", registryId=registryId)
        if registryId not in (RID, REG_ARN):
            raise aws_error("ResourceNotFoundException", 404, f"Registry with ARN {registryId} not found.")
        return dict(self.registry)

    def create_registry(self, **kw):
        self._log("create_registry", **kw)
        return {"registryArn": f"arn:aws:agent-registry:{REGION}:{ACCOUNT}:registry/NewRegistry00001"}

    def delete_registry(self, registryId):
        self._log("delete_registry", registryId=registryId)
        if self.records or self.conflicts:
            self.conflicts = max(0, self.conflicts - 1)
            raise aws_error("ConflictException", 409, "Cannot delete registry because it contains registry records.")
        return {"status": "DELETING"}

    def list_tags_for_resource(self, resourceArn):
        self._log("list_tags_for_resource", resourceArn=resourceArn)
        return {"tags": dict(self.tags.get(resourceArn, {}))}

    def create_registry_record(self, registryId, **kw):
        self._log("create_registry_record", registryId=registryId, **kw)
        rec = f"rec{len(self.records) + 1:09d}"
        arn = f"{REG_ARN}/record/{rec}"
        self.records[rec] = {"registryArn": REG_ARN, "recordArn": arn, "recordId": rec, "name": kw["name"], "description": kw.get("description"),
                             "recordType": kw["recordType"], "descriptors": kw["descriptors"], "recordVersion": kw.get("recordVersion"),
                             "status": "DRAFT", "createdAt": T, "updatedAt": T, "createdBy": ACCOUNT}
        self.tags[arn] = dict(kw.get("tags") or {})
        return {"recordArn": arn, "status": "CREATING"}

    def get_registry_record(self, registryId, recordId):
        self._log("get_registry_record", registryId=registryId, recordId=recordId)
        if recordId not in self.records:
            raise aws_error("ResourceNotFoundException", 404, f"Registry record with ID {recordId} not found.")
        if self.updating.get(recordId):
            self.updating[recordId] -= 1
            return {**self.records[recordId], "status": "UPDATING"}
        return dict(self.records[recordId])

    def list_registry_records(self, registryId, **kw):
        self._log("list_registry_records", registryId=registryId, **kw)
        found = list(self.records.values())
        for f in kw.get("filters") or []:
            found = [r for r in found if r.get(f["name"]) in f["values"]]
        return {"registryRecords": [{k: v for k, v in r.items() if k != "descriptors"} for r in found]}

    def update_registry_record(self, registryId, recordId, **kw):
        self._log("update_registry_record", registryId=registryId, recordId=recordId, **kw)
        r = self.records[recordId]
        if r["status"] == "DEPRECATED":
            raise aws_error("ValidationException", 400, "Cannot update registry record in DEPRECATED status (terminal state)")
        if "description" in kw:
            r["description"] = kw["description"].get("optionalValue")
        if "recordVersion" in kw:
            r["recordVersion"] = kw["recordVersion"]
        if "descriptors" in kw:
            r["descriptors"] = unwrap(kw["descriptors"])
        r["status"] = "DRAFT"
        self.updating[recordId] = 1  # read once as UPDATING, then DRAFT
        return {**r, "status": "UPDATING"}

    def submit_registry_record_for_approval(self, registryId, recordId):
        self._log("submit_registry_record_for_approval", registryId=registryId, recordId=recordId)
        r = self.records[recordId]
        if r["status"] not in ("DRAFT", "PENDING_APPROVAL"):
            raise aws_error("ValidationException", 400, f"Cannot submit the record for approval in current status: {r['status']}")
        r["status"] = "APPROVED" if self.registry["approvalConfiguration"]["autoApprovalRules"] else "PENDING_APPROVAL"
        return {"registryArn": REG_ARN, "recordArn": r["recordArn"], "recordId": recordId, "status": r["status"], "updatedAt": T}

    def update_registry_record_status(self, registryId, recordId, status, statusReason):
        self._log("update_registry_record_status", registryId=registryId, recordId=recordId, status=status, statusReason=statusReason)
        r = self.records[recordId]
        if r["status"] == "DEPRECATED":
            raise aws_error("ValidationException", 400, "Cannot update registry record in DEPRECATED status (terminal state)")
        allowed = {"APPROVED": ("PENDING_APPROVAL", "REJECTED", "APPROVED"), "REJECTED": ("PENDING_APPROVAL",),
                   "DEPRECATED": ("DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED")}
        if r["status"] not in allowed.get(status, ()):
            raise aws_error("ValidationException", 400, f"Invalid status transition from {r['status']} to {status}")
        r.update(status=status, statusReason=statusReason)
        return {"registryArn": REG_ARN, "recordArn": r["recordArn"], "recordId": recordId, "status": status, "statusReason": statusReason, "updatedAt": T}

    def delete_registry_record(self, registryId, recordId):
        self._log("delete_registry_record", registryId=registryId, recordId=recordId)
        self.records.pop(recordId)
        return {}


class Discovery:
    """agent-registry: the search sees APPROVED records only."""

    def __init__(self, ctl: Control):
        self.ctl, self.calls = ctl, []

    def search_discoverable_registry_records(self, **kw):
        self.calls.append(kw)
        hits = [r for r in self.ctl.records.values() if r["status"] == "APPROVED"]
        wanted = ((kw.get("filters") or {}).get("recordType") or {}).get("$eq")
        return {"registryRecords": [{**r, "recordVersion": r["recordVersion"]} for r in hits if not wanted or r["recordType"] == wanted]}


class AgentCore:
    """bedrock-agentcore-control: a Gateway with an inline-schema target and an OpenAPI one, and the Harness."""

    def list_gateways(self, **kw):
        return {"items": [{"gatewayId": "hrgw-abc", "name": "hrgateway", "status": "READY", "protocolType": "MCP", "authorizerType": "AWS_IAM",
                           "description": "HR tools"}, {"gatewayId": "other-1", "name": "other", "status": "READY", "protocolType": "A2A"}]}

    def get_gateway(self, gatewayIdentifier):
        return {"gatewayId": "hrgw-abc", "name": "hrgateway", "protocolType": "MCP", "authorizerType": "AWS_IAM", "description": "HR tools",
                "gatewayUrl": "https://hrgw-abc.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"}

    def list_gateway_targets(self, gatewayIdentifier, **kw):
        return {"items": [{"targetId": "T1", "name": "hrtools"}, {"targetId": "T2", "name": "crm"}]}

    def get_gateway_target(self, gatewayIdentifier, targetId):
        if targetId == "T1":
            return {"name": "hrtools", "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": "arn:aws:lambda:us-west-2:111122223333:function:hr",
                                                                                    "toolSchema": {"inlinePayload": TOOLS}}}}}
        return {"name": "crm", "targetConfiguration": {"mcp": {"openApiSchema": {"s3": {"uri": "s3://specs/crm.json"}}}}}

    def get_harness(self, harnessId):
        return {"harness": {"harnessName": "hr_agent", "harnessId": harnessId, "arn": HARNESS_ARN, "status": "READY",
                            "systemPrompt": [{"text": "You help employees with HR policies.\n\n## Rules\nNever share salaries."}],
                            "model": {"bedrockModelConfig": {"modelId": "m"}}, "tools": [{"type": "agentcore_gateway", "name": "hrtools"}],
                            "skills": [{"s3": {"uri": "s3://skills-bucket/skills/leave-calculator/"}}],
                            "environment": {"agentCoreRuntimeEnvironment": {"agentRuntimeId": "harness_hr_agent-x",
                                                                            "agentRuntimeArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/harness_hr_agent-x"}}}}

    def list_tags_for_resource(self, resourceArn):
        return {"tags": {}}


class S3:
    def __init__(self):
        self.objects = {("skills-bucket", "skills/leave-calculator/SKILL.md"): SKILL_MD.encode(),
                        ("skills-bucket", "skills/plain/SKILL.md"): b"# A skill without frontmatter\n"}
        self.gets: list[tuple[str, str]] = []

    def get_object(self, Bucket, Key):
        self.gets.append((Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise aws_error("NoSuchKey", 404, "The specified key does not exist.")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


class Session:
    def __init__(self, auto: bool = False):
        self.ctl, self.core, self.s3 = Control(auto), AgentCore(), S3()
        self.disc = Discovery(self.ctl)

    def client(self, name, region_name=None, **kw):
        if name == "sts":
            return type("Sts", (), {"get_caller_identity": lambda _s: {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/sa"}})()
        return {"agent-registry-control": self.ctl, "agent-registry": self.disc, "bedrock-agentcore-control": self.core, "s3": self.s3}[name]


class Jobs:
    """console.jobs: list newest first (without results), get by id."""

    def __init__(self, jobs):
        self.jobs = jobs

    def list(self, *, workspace=None, kind=None, limit=50):
        return [{k: v for k, v in j.items() if k not in ("result", "log")} for j in self.jobs if j["workspace"] == workspace and j["kind"] == kind][:limit]

    def get(self, jid):
        return next(j for j in self.jobs if j["id"] == jid)


def verify_job(jid, agent_id, *, status="succeeded", holds=(True, True, False)):
    return {"id": jid, "kind": "verify", "workspace": "dev", "status": status, "finishedAt": "2026-10-01T12:00:00Z",
            "params": {"agent": "hr_agent", "agentId": agent_id, "contractSet": "cs-hr", "repeat": 3, "panel": True},
            "result": {"robust": all(holds), "holding": sum(holds), "repeat": 3, "generatedAt": "2026-10-01T11:59:00Z", "harness": {"model": "m"},
                       "contracts": [{"id": f"c{i}", "holds": h} for i, h in enumerate(holds, 1)]}}


HARNESS = {"kind": "harness", "name": "hr_agent", "id": "hr_agent-1", "arn": HARNESS_ARN, "status": "READY",
           "systemPrompt": "# HR assistant\n\nYou help employees with HR policies and leave.\n\n## Rules\nNever share salaries.",
           "tools": [{"type": "agentcore_gateway", "name": "hrtools"}], "skills": [{"s3": {"uri": "s3://skills-bucket/skills/leave-calculator/"}}],
           "runtimeArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/harness_hr_agent-x"}


# -- registries ----------------------------------------------------------------------------------------------------

def test_registries_follow_empty_pages_and_are_created_with_the_console_tag():
    s = Session()
    [found] = rg.registries(s, REGION)
    assert len(s.ctl.sent("list_registries")) == 2  # the empty first page's token is followed
    assert found["id"] == RID and found["autoApproval"] is False and found["console"] is True and found["authorizer"] == "AWS_IAM"
    assert found["createdAt"] == "2026-10-01T13:00:00+00:00"  # JSON-safe
    made = rg.create_registry(s, REGION, {"name": "adlc-catalog", "description": "Agents and tools", "autoApproval": True})
    assert s.ctl.sent("create_registry")[-1] == {"name": "adlc-catalog", "description": "Agents and tools", "tags": CONSOLE,
                                                 "approvalConfiguration": {"autoApprovalRules": ["APPROVE_ALL"]}}
    assert made == {"id": "NewRegistry00001", "arn": f"arn:aws:agent-registry:{REGION}:{ACCOUNT}:registry/NewRegistry00001", "name": "adlc-catalog",
                    "status": "CREATING", "autoApproval": True}
    rg.create_registry(s, REGION, {"name": "manual"})
    assert s.ctl.sent("create_registry")[-1]["approvalConfiguration"] == {"autoApprovalRules": []}  # manual review is the default
    for bad in ("", "-dash", "has space", "x" * 65):
        with pytest.raises(rg.RegistryError):
            rg.create_registry(s, REGION, {"name": bad})


# -- what goes in records -----------------------------------------------------------------------------------------

def test_an_agent_card_is_filled_from_a_harness_or_a_runtime():
    card = rg.agent_card(HARNESS, REGION, {"s3://skills-bucket/skills/leave-calculator/": "Calculate leave balances"})
    assert rg.check_card(card) is card
    assert card["protocolVersion"] == "0.3.0" and card["version"] == "1.0.0" and card["description"] == "You help employees with HR policies and leave."
    assert card["url"] == f"https://bedrock-agentcore.{REGION}.amazonaws.com/harnesses/invoke?harnessArn=arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3A111122223333%3Aharness%2Fhr_agent-1"
    assert [(s["id"], s["description"]) for s in card["skills"]] == [("hrtools", "Tools of the AgentCore Gateway target hrtools"),
                                                                     ("leave-calculator", "Calculate leave balances")]
    assert all(s["tags"] for s in card["skills"]) and "preferredTransport" not in card
    assert rg.extension(card, rg.SOURCE) == {"kind": "harness", "id": "hr_agent-1", "name": "hr_agent", "arn": HARNESS_ARN, "region": REGION,
                                             "runtimeArn": HARNESS["runtimeArn"], "protocol": "HARNESS"}
    runtime = {"kind": "runtime", "name": "byoc", "id": "byoc-1", "arn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/byoc-1",
               "protocol": {"serverProtocol": "A2A"}}
    a2a = rg.agent_card(runtime, REGION)
    assert a2a["url"].endswith("/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3A111122223333%3Aruntime%2Fbyoc-1/invocations/")
    assert a2a["preferredTransport"] == "JSONRPC" and a2a["capabilities"]["streaming"] is True
    assert [s["id"] for s in a2a["skills"]] == ["conversation"] and a2a["description"] == "AgentCore Runtime byoc"  # nothing else to list


def test_a_card_missing_what_the_service_requires_is_named_here():
    card = rg.agent_card(HARNESS, REGION)
    for field in rg.CARD_FIELDS:
        broken = {k: v for k, v in card.items() if k != field}
        with pytest.raises(rg.RegistryError, match=field):  # the service only says "does not match any supported version"
            rg.check_card(broken)
    with pytest.raises(rg.RegistryError, match="tags"):
        rg.check_card({**card, "skills": [{"id": "a", "name": "A", "description": "d"}]})
    with pytest.raises(rg.RegistryError, match="not JSON"):
        rg.check_card("{name")
    assert rg.check_card(json.dumps(card)) == card


def test_records_of_the_three_kinds_are_sent_in_the_ga_shapes():
    s = Session()
    card = rg.agent_card(HARNESS, REGION)
    made = rg.create_record(s, REGION, RID, {"type": "agent", "card": card})
    sent = s.ctl.sent("create_registry_record")[-1]
    assert {k: v for k, v in sent.items() if k != "descriptors"} == {"registryId": RID, "name": "hr_agent", "recordType": "AGENT", "recordVersion": "1.0.0",
                                                                     "tags": CONSOLE, "description": card["description"]}
    assert set(sent["descriptors"]) == {"a2aAgentCard"} and json.loads(sent["descriptors"]["a2aAgentCard"]["data"]) == card
    assert "dataSchemaVersion" not in sent["descriptors"]["a2aAgentCard"]  # the service picks the version that validates
    assert made["id"] == "rec000000001" and made["status"] == "CREATING" and made["type"] == "agent"

    made = rg.create_record(s, REGION, RID, {"type": "mcp", "gatewayId": "hrgw-abc", "name": "hr-tools", "version": "2.0.0"})
    sent = s.ctl.sent("create_registry_record")[-1]
    mcp = sent["descriptors"]["mcpServer"]
    assert sent["recordType"] == "MCP" and sent["recordVersion"] == "2.0.0" and sent["description"] == "HR tools"
    assert json.loads(mcp["data"]) == {"name": "io.adlc/hr-tools", "description": "HR tools", "version": "1.0.0",
                                       "remotes": [{"type": "streamable-http", "url": "https://hrgw-abc.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"}]}
    assert json.loads(mcp["additionalData"]["tools"]["data"]) == {"tools": [{**TOOLS[0], "name": "hrtools___retrieve_policy"}]}  # an object, as the Gateway names it
    assert made["unlistedTargets"] == ["crm"]  # an OpenAPI target's tools are known only to the Gateway

    rg.create_record(s, REGION, RID, {"type": "mcp", "url": "https://mcp.example.com/mcp", "name": "remote", "description": "x" * 150, "transport": "sse"})
    sent = s.ctl.sent("create_registry_record")[-1]
    server = json.loads(sent["descriptors"]["mcpServer"]["data"])
    assert "additionalData" not in sent["descriptors"]["mcpServer"] and server["remotes"] == [{"type": "sse", "url": "https://mcp.example.com/mcp"}]
    assert len(server["description"]) == 100 and len(sent["description"]) == 150  # server.json takes 100 characters (live), the record more
    for bad in ({"type": "mcp", "name": "x", "url": "ftp://x"}, {"type": "mcp", "name": "x", "url": "https://x", "transport": "ws"}, {"type": "tool", "name": "x"}):
        with pytest.raises(rg.RegistryError):
            rg.create_record(s, REGION, RID, bad)

    for uri in ("s3://skills-bucket/skills/leave-calculator/", "s3://skills-bucket/skills/leave-calculator/SKILL.md", "s3://skills-bucket/skills/leave-calculator"):
        rg.create_record(s, REGION, RID, {"type": "skill", "uri": uri})
        assert s.s3.gets[-1] == ("skills-bucket", "skills/leave-calculator/SKILL.md")
    sent = s.ctl.sent("create_registry_record")[-1]
    skill = sent["descriptors"]["agentSkillsDefinition"]
    assert sent["name"] == "leave-calculator" and sent["recordType"] == "SKILL" and sent["description"] == "Calculate leave balances and plan leave"
    assert json.loads(skill["data"]) == {"name": "leave-calculator", "description": "Calculate leave balances and plan leave", "version": "1.2.0",
                                         "path": "s3://skills-bucket/skills/leave-calculator/"}
    assert skill["additionalData"] == {"skillMd": {"data": SKILL_MD}}


def test_a_skill_the_registry_would_refuse_is_refused_first():
    s = Session()
    with pytest.raises(rg.RegistryError, match="frontmatter"):
        rg.create_record(s, REGION, RID, {"type": "skill", "uri": "s3://skills-bucket/skills/plain/"})
    for meta, match in (("name: Leave Calc\ndescription: d", "lowercase"), ("name: a--b\ndescription: d", "lowercase"), ("name: ok", "description")):
        with pytest.raises(rg.RegistryError, match=match):
            rg.skill_definition("s3://skills-bucket/p/", f"---\n{meta}\n---\nbody\n")
    with pytest.raises(rg.RegistryError, match="s3://"):
        rg.s3_location("https://bucket/skills/")
    with pytest.raises(ClientError):  # a missing SKILL.md is S3's NoSuchKey (a 404 on the route)
        rg.create_record(s, REGION, RID, {"type": "skill", "uri": "s3://skills-bucket/skills/none/"})
    assert s.ctl.sent("create_registry_record") == []


# -- the lifecycle -------------------------------------------------------------------------------------------------

def test_submit_approve_reject_and_deprecate_send_what_the_service_takes():
    s = Session()
    rec = rg.create_record(s, REGION, RID, {"type": "agent", "card": rg.agent_card(HARNESS, REGION)})["id"]
    assert rg.submit(s, REGION, RID, rec) == {"id": rec, "status": "PENDING_APPROVAL"}
    assert s.ctl.sent("submit_registry_record_for_approval") == [{"registryId": RID, "recordId": rec}]
    for target in ("DRAFT", "PENDING_APPROVAL", "published"):
        with pytest.raises(rg.RegistryError):  # live: "Invalid target status"
            rg.set_status(s, REGION, RID, rec, target)
    assert s.ctl.sent("update_registry_record_status") == []
    out = rg.set_status(s, REGION, RID, rec, "rejected", "no evidence", by="admin")
    assert s.ctl.sent("update_registry_record_status")[-1] == {"registryId": RID, "recordId": rec, "status": "REJECTED",
                                                               "statusReason": "rejected in the ADLC console by admin: no evidence"}
    assert out["status"] == "REJECTED"
    assert rg.set_status(s, REGION, RID, rec, "APPROVED")["status"] == "APPROVED"  # a curator may still approve a rejected record
    assert rg.get_record(s, REGION, RID, rec)["statusReason"] == "approved in the ADLC console"
    assert rg.set_status(s, REGION, RID, rec, "DEPRECATED", "x" * 300)["status"] == "DEPRECATED"
    assert len(s.ctl.sent("update_registry_record_status")[-1]["statusReason"]) == 255
    with pytest.raises(rg.RegistryError, match="terminal"):
        rg.update_record(s, REGION, RID, rec, {"description": "after deprecation"})


def test_an_update_is_sent_in_patch_wrappers_and_returns_the_record_to_draft():
    s = Session()
    card = rg.agent_card(HARNESS, REGION)
    rec = rg.create_record(s, REGION, RID, {"type": "agent", "card": card})["id"]
    rg.submit(s, REGION, RID, rec)
    rg.set_status(s, REGION, RID, rec, "APPROVED")
    edited = {**card, "description": "HR policies, leave and benefits"}
    out = rg.update_record(s, REGION, RID, rec, {"description": "HR assistant", "version": "1.1.0", "card": edited})
    sent = s.ctl.sent("update_registry_record")[-1]
    assert sent["description"] == {"optionalValue": "HR assistant"} and sent["recordVersion"] == "1.1.0"
    assert json.loads(sent["descriptors"]["optionalValue"]["a2aAgentCard"]["optionalValue"]["data"]["optionalValue"]) == edited
    assert out == {"id": rec, "status": "UPDATING", "version": "1.1.0"} and rg.get_record(s, REGION, RID, rec)["status"] == "UPDATING"
    assert rg.get_record(s, REGION, RID, rec)["status"] == "DRAFT"  # an edit of an approved record needs approving again
    with pytest.raises(rg.RegistryError, match="nothing to change"):
        rg.update_record(s, REGION, RID, rec, {})
    mcp = rg.create_record(s, REGION, RID, {"type": "mcp", "gatewayId": "hrgw-abc", "name": "hr-tools"})["id"]
    rg.update_record(s, REGION, RID, mcp, {"url": "https://mcp.example.com/mcp"})
    patch = s.ctl.sent("update_registry_record")[-1]["descriptors"]["optionalValue"]["mcpServer"]["optionalValue"]
    assert patch["additionalData"] == {} and json.loads(patch["data"]["optionalValue"])["remotes"][0]["url"] == "https://mcp.example.com/mcp"
    assert rg.get_record(s, REGION, RID, mcp)["tools"] == []  # live: an empty wrapper unsets the Gateway's tools


# -- the console-only rule -----------------------------------------------------------------------------------------

def test_only_console_records_are_changed_or_deleted():
    s = Session()
    theirs = s.ctl.add("launchpad_agent", "AGENT", {"a2aAgentCard": {"data": json.dumps(rg.agent_card(HARNESS, REGION))}}, console=False)
    mine = rg.create_record(s, REGION, RID, {"type": "agent", "card": rg.agent_card(HARNESS, REGION), "name": "hr_agent_card"})["id"]
    for change in (lambda: rg.delete_record(s, REGION, RID, theirs), lambda: rg.update_record(s, REGION, RID, theirs, {"description": "x"}),
                   lambda: rg.publish(s, REGION, RID, theirs, lambda a: {"jobId": "verify-0000000001"})):
        with pytest.raises(rg.RegistryError, match="only records created from this console"):
            change()
    assert s.ctl.sent("delete_registry_record") == [] and s.ctl.sent("update_registry_record") == []
    assert rg.get_record(s, REGION, RID, theirs)["console"] is False  # it can still be read, and submitted and curated when acknowledged
    with pytest.raises(rg.RegistryError, match="acknowledged"):
        rg.submit(s, REGION, RID, theirs)  # someone's draft, into a registry that may approve on submit
    assert s.ctl.sent("submit_registry_record_for_approval") == []
    assert rg.submit(s, REGION, RID, theirs, acknowledged=True)["status"] == "PENDING_APPROVAL"
    with pytest.raises(rg.RegistryError, match="acknowledged"):
        rg.set_status(s, REGION, RID, theirs, "DEPRECATED", "obsolete", by="admin")  # terminal: never on another team's record unconfirmed
    assert s.ctl.sent("update_registry_record_status") == []
    assert rg.set_status(s, REGION, RID, theirs, "APPROVED", by="admin", acknowledged=True)["status"] == "APPROVED"
    assert rg.delete_record(s, REGION, RID, mine) == {"id": mine, "deleted": True}
    assert s.ctl.sent("delete_registry_record") == [{"registryId": RID, "recordId": mine}]
    for bad in ("NOPE", "rec00000000/1"):
        with pytest.raises(rg.RegistryError, match="no record"):
            rg.delete_record(s, REGION, RID, bad)


def test_a_registry_is_deleted_only_when_it_and_all_its_records_are_the_consoles():
    s = Session()
    s.ctl.add("mine", "MCP", {"mcpServer": {"data": "{}"}})
    with pytest.raises(rg.RegistryError, match="delete them with it"):  # live: DeleteRegistry refuses a registry holding records
        rg.delete_registry(s, REGION, RID)
    theirs = s.ctl.add("theirs", "MCP", {"mcpServer": {"data": "{}"}}, console=False)
    with pytest.raises(rg.RegistryError, match="did not create"):
        rg.delete_registry(s, REGION, RID, with_records=True)
    assert s.ctl.sent("delete_registry_record") == [] and s.ctl.sent("delete_registry") == []
    s.ctl.records.pop(theirs)
    s.ctl.conflicts = 1  # a record just deleted may still be counted for a moment
    slept = []
    out = rg.delete_registry(s, REGION, RID, with_records=True, sleep=slept.append)
    assert out == {"id": RID, "status": "DELETING", "deleted": True, "records": 1} and slept == [2.0]
    assert len(s.ctl.sent("delete_registry_record")) == 1 and len(s.ctl.sent("delete_registry")) == 2
    s.ctl.tags[REG_ARN] = {}
    with pytest.raises(rg.RegistryError, match="only registries created from this console"):
        rg.delete_registry(s, REGION, RID)


# -- evidence ------------------------------------------------------------------------------------------------------

def test_publish_attaches_the_agents_latest_verification_and_submits():
    jobs = Jobs([verify_job("verify-0000000005", "hr_agent-1", status="running"), verify_job("verify-0000000004", "other-1"),
                 verify_job("verify-0000000003", "hr_agent-1", status="failed"), verify_job("verify-0000000002", "hr_agent-1"),
                 verify_job("verify-0000000001", "hr_agent-1", holds=(True, True, True))])
    evidence = rg.latest_verification(jobs, "dev", "hr_agent-1")
    assert evidence == {"jobId": "verify-0000000002", "agent": "hr_agent", "agentId": "hr_agent-1", "robust": False, "holding": 2, "contracts": 3,
                        "repeat": 3, "notHolding": ["c3"], "contractSet": "cs-hr", "panel": True, "model": "m", "verifiedAt": "2026-10-01T11:59:00Z"}
    assert rg.latest_verification(jobs, "prod", "hr_agent-1") is None
    s = Session()
    stale = rg.attach_evidence(rg.agent_card(HARNESS, REGION), {"jobId": "verify-0000000001", "robust": True, "holding": 3, "contracts": 3, "repeat": 3})
    rec = rg.create_record(s, REGION, RID, {"type": "agent", "card": stale})["id"]
    slept = []
    out = rg.publish(s, REGION, RID, rec, lambda agent: rg.latest_verification(jobs, "dev", agent), sleep=slept.append)
    card = json.loads(s.ctl.sent("update_registry_record")[-1]["descriptors"]["optionalValue"]["a2aAgentCard"]["optionalValue"]["data"]["optionalValue"])
    assert [e["uri"] for e in card["capabilities"]["extensions"]] == [rg.SOURCE, rg.EVIDENCE]  # the older evidence replaced, the agent kept
    assert rg.extension(card, rg.EVIDENCE) == evidence and "2/3 contracts" in card["capabilities"]["extensions"][1]["description"]
    assert slept == [1.0] and out == {"id": rec, "status": "PENDING_APPROVAL", "evidence": evidence}  # waited out UPDATING, then submitted
    got = rg.get_record(s, REGION, RID, rec)
    assert got["evidence"] == evidence and got["source"]["id"] == "hr_agent-1" and got["status"] == "PENDING_APPROVAL"
    updates = len(s.ctl.sent("update_registry_record"))
    rg.set_status(s, REGION, RID, rec, "REJECTED", "c3 does not hold")
    s.ctl.records[rec]["status"] = "DRAFT"
    rg.publish(s, REGION, RID, rec, lambda agent: evidence)  # the same evidence on a draft: submitted as it is
    assert len(s.ctl.sent("update_registry_record")) == updates


def test_publish_needs_a_verification_and_an_agent_record():
    s = Session()
    rec = rg.create_record(s, REGION, RID, {"type": "agent", "card": rg.agent_card(HARNESS, REGION)})["id"]
    with pytest.raises(rg.RegistryError, match="verify it first"):
        rg.publish(s, REGION, RID, rec, lambda agent: None)
    bare = rg.agent_card(HARNESS, REGION)
    bare["capabilities"].pop("extensions")
    nameless = rg.create_record(s, REGION, RID, {"type": "agent", "card": bare, "name": "pasted"})["id"]
    with pytest.raises(rg.RegistryError, match="names no agent"):
        rg.publish(s, REGION, RID, nameless, lambda agent: {"jobId": "x"})
    seen = []
    rg.publish(s, REGION, RID, nameless, lambda agent: seen.append(agent) or {"jobId": "verify-0000000001"}, agent_id="hr_agent-1", sleep=lambda _: None)
    assert seen == ["hr_agent-1"]
    tool = rg.create_record(s, REGION, RID, {"type": "mcp", "url": "https://mcp.example.com/mcp", "name": "remote"})["id"]
    with pytest.raises(rg.RegistryError, match="agent record"):
        rg.publish(s, REGION, RID, tool, lambda agent: {"jobId": "x"})
    auto = Session(auto=True)
    rec = rg.create_record(auto, REGION, RID, {"type": "agent", "card": rg.agent_card(HARNESS, REGION)})["id"]
    assert rg.publish(auto, REGION, RID, rec, lambda agent: {"jobId": "verify-0000000001"}, sleep=lambda _: None)["status"] == "APPROVED"


# -- discovery -----------------------------------------------------------------------------------------------------

def test_search_sends_one_registry_and_an_operator_filter():
    s = Session()
    card = rg.attach_evidence(rg.agent_card(HARNESS, REGION), {"jobId": "verify-0000000001", "robust": True, "holding": 3, "contracts": 3, "repeat": 3})
    rec = s.ctl.add("hr_agent", "AGENT", {"a2aAgentCard": {"data": json.dumps(card)}}, status="APPROVED")
    s.ctl.add("draft_agent", "AGENT", {"a2aAgentCard": {"data": json.dumps(card)}})
    s.ctl.add("hr-tools", "MCP", {"mcpServer": {"data": json.dumps(rg.mcp_server("hr-tools", "https://x.example/mcp"))}}, status="APPROVED")
    [hit] = rg.search(s, REGION, RID, " leave balance ", kind="agent")
    assert s.disc.calls[-1] == {"registryIds": [RID], "searchQuery": "leave balance", "maxResults": 20, "filters": {"recordType": {"$eq": "AGENT"}}}
    assert hit["id"] == rec and hit["evidence"]["jobId"] == "verify-0000000001" and hit["card"]["name"] == "hr_agent"
    assert [r["type"] for r in rg.search(s, REGION, RID, "", limit=50)] == ["agent", "mcp"]  # approved only, every type
    assert "filters" not in s.disc.calls[-1] and s.disc.calls[-1]["maxResults"] == 20
    assert [r["url"] for r in rg.search(s, REGION, RID, "tools", kind="mcp")] == ["https://x.example/mcp"]


# -- the routes ----------------------------------------------------------------------------------------------------

spec = importlib.util.spec_from_file_location("adlc_console_server_registry", REPO / "app" / "console" / "server.py")
console_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console_mod)  # type: ignore[union-attr]


class Client:
    def __init__(self, port):
        self.base, self.cookie = f"http://127.0.0.1:{port}/api/console", None

    def call(self, method, path, body=None):
        headers = {"Content-Type": "application/json", **({"Cookie": self.cookie} if self.cookie else {})}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.headers.get("Set-Cookie"):
                    self.cookie = resp.headers["Set-Cookie"].split(";")[0]
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def login(self, username):
        self.cookie = None
        assert self.call("POST", "/login", {"username": username, "password": "0123456789"})[0] == 200


@pytest.fixture()
def served(tmp_path):
    fake = Session()
    c = console_mod.Console(tmp_path / "data", session_factory=lambda **kw: fake, clients_factory=lambda cfg: None)
    server = console_mod.create_server(c)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Client(server.server_address[1]), c, fake
    server.shutdown()
    server.server_close()


def test_routes_curation_is_for_admins_and_refusals_are_answers(served):
    client, console, fake = served
    assert client.call("POST", "/workspaces", {"id": "dev", "accountId": ACCOUNT, "region": REGION, "profile": "default"})[0] == 201
    assert client.call("POST", "/users", {"username": "admin", "password": "0123456789", "role": "admin"})[0] == 201
    client.login("admin")
    assert client.call("POST", "/users", {"username": "ann", "password": "0123456789", "role": "member", "workspaces": ["dev"]})[0] == 201
    base = "/workspaces/dev/registries"
    status, made = client.call("POST", base, {"name": "adlc-catalog"})
    assert status == 201 and made["status"] == "CREATING"

    client.login("ann")  # a member: registers, submits, publishes; never creates registries or decides
    status, listed = client.call("GET", base)
    assert status == 200 and [r["id"] for r in listed["registries"]] == [RID]
    assert client.call("POST", base, {"name": "member-made", "autoApproval": True})[0] == 403  # the approval policy is the admin's
    assert client.call("DELETE", f"{base}/{RID}")[0] == 403
    status, card = client.call("GET", f"{base}/agent-card?kind=harness&ident=hr_agent-1")
    assert status == 200 and card["agent"]["id"] == "hr_agent-1" and card["card"]["skills"][1]["description"] == "Calculate leave balances and plan leave"
    assert [g["id"] for g in client.call("GET", f"{base}/gateways")[1]["gateways"]] == ["hrgw-abc"]  # MCP Gateways only
    status, rec = client.call("POST", f"{base}/{RID}/records", {"type": "agent", "card": card["card"]})
    assert status == 201
    path = f"{base}/{RID}/records/{rec['id']}"
    status, got = client.call("GET", path)
    assert status == 200 and got["console"] is True and got["latestVerification"] is None
    job = verify_job("verify-00000000aa", "hr_agent-1", holds=(True, True))
    (console.data_dir / "console" / "jobs" / f"{job['id']}.json").write_text(json.dumps(job), encoding="utf-8")
    assert client.call("GET", path)[1]["latestVerification"]["jobId"] == "verify-00000000aa"
    status, out = client.call("POST", f"{path}/publish", {})
    assert status == 200 and out["status"] == "PENDING_APPROVAL" and out["evidence"]["robust"] is True
    assert client.call("POST", f"{path}/status", {"status": "APPROVED"})[0] == 403

    client.login("admin")
    status, out = client.call("POST", f"{path}/status", {"status": "APPROVED", "reason": "2/2 robust"})
    assert status == 200 and out["statusReason"] == "approved in the ADLC console by admin: 2/2 robust"
    assert client.call("GET", f"{base}/{RID}/search?q=leave&type=agent")[1]["records"][0]["evidence"]["jobId"] == "verify-00000000aa"
    assert client.call("GET", f"{base}/{RID}/records?status=APPROVED")[1]["records"][0]["id"] == rec["id"]
    status, body = client.call("POST", f"{path}/submit", {})
    assert status == 400 and body["error"].startswith("ValidationException: Cannot submit")  # AWS's refusal, passed on as such
    assert client.call("POST", f"{path}/status", {"status": "DRAFT"}) == (400, {"error": "status: APPROVED, REJECTED or DEPRECATED"})
    assert client.call("GET", f"{base}/{RID}/records/NOPE")[0] == 400
    assert client.call("GET", f"{base}/{RID}/records/AAAAAAAAAAAA") == (404, {"error": "ResourceNotFoundException: Registry record with ID AAAAAAAAAAAA not found."})
    assert client.call("POST", f"{base}/{RID}/records", {"type": "agent", "card": {"name": "x"}})[0] == 400
    assert client.call("GET", f"{base}/agent-card?kind=gateway&ident=x")[0] == 400  # agents.get_agent's own refusal
    assert "delete them with it" in client.call("DELETE", f"{base}/{RID}")[1]["error"]
    status, out = client.call("DELETE", f"{base}/{RID}?withRecords=1")
    assert status == 200 and out["records"] == 1 and fake.ctl.records == {}


# -- the review's findings, each pinned ------------------------------------------------------------------------------------

def test_the_evidence_is_a_verification_of_the_agent_itself_never_a_treatments_or_a_candidates():
    """review M4: a newer, robust verify job under the agent's id that verified an A/B treatment, a canary's candidate or
    another prompt / model is not the agent's evidence."""
    treatment = verify_job("verify-0000000009", "hr_agent-1", holds=(True,))
    treatment["params"]["treatment"] = {"fingerprint": "abcd", "experiment": "exp-12345678"}
    candidate = verify_job("verify-0000000008", "hr_agent-1", holds=(True,))
    candidate["params"].update(treatment={"fingerprint": "ef01", "canary": "can-12345678"}, runtime="hr_agent_c12345678")
    override = verify_job("verify-0000000007", "hr_agent-1", holds=(True,))
    override["result"].update(promptOverride=True, modelOverride="m-candidate")
    own = verify_job("verify-0000000006", "hr_agent-1")
    evidence = rg.latest_verification(Jobs([treatment, candidate, override, own]), "dev", "hr_agent-1")
    assert evidence["jobId"] == "verify-0000000006" and evidence["robust"] is False and evidence["notHolding"] == ["c3"]
    assert rg.latest_verification(Jobs([treatment, candidate, override]), "dev", "hr_agent-1") is None
