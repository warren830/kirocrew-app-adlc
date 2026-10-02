"""The AgentCore Registry in the console: registries, and records of three kinds — agents (an A2A agent card), MCP
tools (a Gateway or a server URL) and skills (a SKILL.md in S3) — through submit and approval to discovery.

It speaks the GA Agent Registry API: ``agent-registry-control`` for the catalogue and its curation, ``agent-registry``
for discovery. Live 2026-10-01 (us-west-2, botocore 1.43.90) the registry operations ``bedrock-agentcore-control`` also
models (CreateRegistry … UpdateRegistryRecordStatus) were refused to an AdministratorAccess user ("not authorized to
perform: bedrock-agentcore:ListRegistries", while IAM simulates them as allowed), and ``bedrock-agentcore``
SearchRegistryRecords did not know the GA registries ("Registry not found").

* **Records** — ``AGENT``: an A2A 0.3 card (``a2aAgentCard``), filled from a Harness or Runtime through
  :func:`agents.get_agent` (:func:`agent_card`). ``MCP``: an MCP registry ``server.json`` (``mcpServer``; a namespaced
  ASCII name ``io.adlc/<name>``, a description of at most 100 characters) and, for a Gateway, its tools
  ``{"tools": [...]}`` read from its targets' inline schemas (``<target>___<tool>``, as the Gateway names them).
  ``SKILL``: ``{name, description, version, path}`` (``agentSkillsDefinition``) with the SKILL.md itself, read from S3;
  the service wants frontmatter whose ``name`` is lowercase letters, digits and single hyphens, and a ``description``.
  The service validates a descriptor when it is sent (a 400 that never names the missing field: the card's required
  fields are checked here first) and chooses the schema version itself (A2A 0.3, server.json 2025-12-11, tools
  2025-11-25, skill 0.1.0). Names are unique per (name, recordVersion).
* **Lifecycle** — CREATING → DRAFT (about a second) → submit → PENDING_APPROVAL, or APPROVED at once in a registry with
  auto-approval → APPROVED or REJECTED (a curator may still approve a rejected record; it cannot be resubmitted until
  it is edited) → DEPRECATED, which is terminal and removes it from discovery. Any edit returns a record to DRAFT,
  while its approved version stays discoverable. Only APPROVED, REJECTED and DEPRECATED can be set as a status, and a
  DRAFT cannot be approved without being submitted. Deprecating a DRAFT whose earlier version was approved left that
  version listed by ListDiscoverableRegistryRecords (not by search) until the record was deleted: the page offers
  deprecation for APPROVED records only.
* **Evidence** — :func:`publish` attaches the agent's latest console verification (a ``verify`` job: robust, holding,
  the job id) to its card as an A2A extension (``urn:adlc:console:verification``), then submits the record, so whoever
  approves it sees what the agent was shown to do. Only a verification of the agent as it is counts: not an A/B
  experiment's treatment nor a canary's candidate (their jobs carry a ``treatment`` marker), nor a run with another
  prompt or model. The card names its agent in a second extension (``urn:adlc:console:agent``).
* **Discovery** — :func:`search`: the registry's semantic search, which sees APPROVED records only.

Everything the console creates is tagged ``adlc:console=1``; only those records may be changed or deleted from it, and
a registry only when it and every record in it are the console's (DeleteRegistry refuses a registry that still holds
records, so they go first, on request). Submitting and deciding (approve, reject, deprecate — terminal) act on another
team's record only with ``acknowledged: true``: a member must not submit someone's draft into a registry that
approves on submit. Creating or deleting a registry (which sets how its records are approved) and approving are admin
routes.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

from ..direct.aws import client
from .agents import CONSOLE_TAG, AgentError

CONTROL, DISCOVERY = "agent-registry-control", "agent-registry"
#: The console's record kinds and the service's record types.
KINDS = {"agent": "AGENT", "mcp": "MCP", "skill": "SKILL"}
TYPE_KIND = {**{v: k for k, v in KINDS.items()}, "CUSTOM": "custom", "GATEWAY": "gateway"}
RECORD_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED", "DEPRECATED", "CREATING", "UPDATING", "CREATE_FAILED", "UPDATE_FAILED")
#: What UpdateRegistryRecordStatus takes (live: DRAFT and PENDING_APPROVAL are "Invalid target status").
TARGETS = {"APPROVED": "approved", "REJECTED": "rejected", "DEPRECATED": "deprecated"}
REGISTRY_ID = re.compile(r"^[A-Za-z0-9]{12,16}$")
RECORD_ID = re.compile(r"^[A-Za-z0-9]{12}$")
REGISTRY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,63}$")
RECORD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,254}$")
VERSION = re.compile(r"^[A-Za-z0-9.-]{1,255}$")
SKILL_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
S3_URI = re.compile(r"^s3://([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])/(.*)$")
A2A_VERSION = "0.3.0"
CARD_FIELDS = ("protocolVersion", "name", "description", "url", "version", "capabilities", "defaultInputModes", "defaultOutputModes", "skills")
SKILL_FIELDS = ("id", "name", "description", "tags")
EVIDENCE = "urn:adlc:console:verification"
SOURCE = "urn:adlc:console:agent"
MAX_DATA = 102_400  # every descriptor's data
MCP_DESCRIPTION = 100  # server.json (live: 150 characters refused, 100 taken)
DEFAULT_VERSION = "1.0.0"


class RegistryError(ValueError):
    pass


def _ctl(session: Any, region: str) -> Any:
    return client(session, CONTROL, region)


def _ts(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def _pages(call: Any, key: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Every page (live: ListRegistries answers an empty first page that still carries a nextToken)."""
    out: list[dict[str, Any]] = []
    for _ in range(100):
        page = call(**kwargs)
        out += page.get(key) or []
        if not page.get("nextToken"):
            break
        kwargs["nextToken"] = page["nextToken"]
    return out


def _tags(ctl: Any, arn: str) -> dict[str, str]:
    return ctl.list_tags_for_resource(resourceArn=arn).get("tags") or {}


def _console(tags: Mapping[str, str]) -> bool:
    return tags.get("adlc:console") == "1"


def _loads(text: Any) -> Any:
    try:
        return json.loads(text) if isinstance(text, str) and text else None
    except ValueError:
        return None


def _data(value: Any, what: str) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) > MAX_DATA:
        raise RegistryError(f"{what} is {len(text)} characters: the registry keeps at most {MAX_DATA}")
    return text


def registry_id(value: str) -> str:
    if not REGISTRY_ID.match(str(value or "")):
        raise RegistryError(f"no registry {value!r}: a registry id is 12-16 letters and digits")
    return str(value)


def record_id(value: str) -> str:
    if not RECORD_ID.match(str(value or "")):
        raise RegistryError(f"no record {value!r}: a record id is 12 letters and digits")
    return str(value)


# -- registries ---------------------------------------------------------------------------------------------------

def _registry(r: Mapping[str, Any]) -> dict[str, Any]:
    rules = (r.get("approvalConfiguration") or {}).get("autoApprovalRules")
    return {"id": r.get("registryId"), "arn": r.get("registryArn"), "name": r.get("name"), "description": r.get("description") or "",
            "status": r.get("status"), "statusReason": r.get("statusReason"),
            "authorizer": (r.get("discoveryConfiguration") or {}).get("authorizerType"),
            "autoApproval": None if rules is None else "APPROVE_ALL" in rules,
            "createdAt": _ts(r.get("createdAt")), "updatedAt": _ts(r.get("updatedAt"))}


def registries(session: Any, region: str) -> list[dict[str, Any]]:
    """The account's registries, each with its approval mode (ListRegistries leaves it out) and whether it is the
    console's."""
    ctl = _ctl(session, region)
    out = []
    for summary in _pages(ctl.list_registries, "registries", maxResults=100):
        item = _registry(summary)
        try:
            item.update({k: v for k, v in _registry(ctl.get_registry(registryId=summary["registryId"])).items() if v is not None})
            item["console"] = _console(_tags(ctl, str(summary["registryArn"])))
        except Exception as exc:  # noqa: BLE001 - one registry being deleted must not hide the others
            if "ResourceNotFound" not in str(exc):
                raise
            item["console"] = False
        out.append(item)
    return out


def registry_request(body: Mapping[str, Any]) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    if not REGISTRY_NAME.match(name):
        raise RegistryError("name: a letter or digit, then letters, digits, '_', '-', '.' or '/' (at most 64)")
    request: dict[str, Any] = {"name": name, "tags": dict(CONSOLE_TAG),
                               "approvalConfiguration": {"autoApprovalRules": ["APPROVE_ALL"] if body.get("autoApproval") is True else []}}
    description = str(body.get("description") or "").strip()
    if description:
        request["description"] = description[:4096]
    return request


def create_registry(session: Any, region: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """A registry (CREATING for about a minute, then READY); records need manual approval unless ``autoApproval``."""
    request = registry_request(body)
    arn = str(_ctl(session, region).create_registry(**request)["registryArn"])
    return {"id": arn.rsplit("/", 1)[-1], "arn": arn, "name": request["name"], "status": "CREATING",
            "autoApproval": bool(request["approvalConfiguration"]["autoApprovalRules"])}


def delete_registry(session: Any, region: str, ident: str, *, with_records: bool = False,
                    sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Delete a console registry. DeleteRegistry refuses one that still holds records (live, whatever its
    documentation says), so its records are deleted first when ``with_records`` — and only if every one is the
    console's."""
    ctl = _ctl(session, region)
    got = ctl.get_registry(registryId=registry_id(ident))
    if not _console(_tags(ctl, str(got["registryArn"]))):
        raise RegistryError("only registries created from this console can be deleted here")
    held = _pages(ctl.list_registry_records, "registryRecords", registryId=ident, maxResults=100)
    foreign = [str(r.get("name")) for r in held if not _console(_tags(ctl, str(r["recordArn"])))]
    if foreign:
        raise RegistryError(f"the registry holds {len(foreign)} record(s) this console did not create ({', '.join(foreign[:5])}): it stays")
    if held and not with_records:
        raise RegistryError(f"the registry holds {len(held)} record(s): delete them first, or delete them with it (withRecords)")
    for r in held:
        ctl.delete_registry_record(registryId=ident, recordId=r["recordId"])
    for attempt in range(6):  # a record just deleted may still be counted for a moment
        try:
            out = ctl.delete_registry(registryId=ident)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 5 or "ConflictException" not in type(exc).__name__ + str(exc):
                raise
            sleep(2.0)
    return {"id": ident, "status": out.get("status"), "deleted": True, "records": len(held)}


# -- records: reading -----------------------------------------------------------------------------------------------

def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise RegistryError("type: agent, mcp or skill")
    return KINDS[kind]


def summary(r: Mapping[str, Any]) -> dict[str, Any]:
    rtype = str(r.get("recordType") or "")
    return {"id": r.get("recordId"), "arn": r.get("recordArn"), "name": r.get("name"), "displayName": r.get("displayName"),
            "description": r.get("description") or "", "type": TYPE_KIND.get(rtype, rtype.lower()), "recordType": rtype,
            "version": r.get("recordVersion"), "status": r.get("status"), "createdAt": _ts(r.get("createdAt")),
            "updatedAt": _ts(r.get("updatedAt")), "createdBy": r.get("createdBy"), "autoDetected": bool(r.get("createdByAutoDetection"))}


def extension(card: Any, uri: str) -> dict[str, Any] | None:
    """The params of the card's A2A extension ``uri`` (``capabilities.extensions``)."""
    caps = card.get("capabilities") if isinstance(card, dict) else None
    for ext in (caps.get("extensions") if isinstance(caps, dict) else None) or []:
        if isinstance(ext, dict) and ext.get("uri") == uri:
            return dict(ext.get("params") or {})
    return None


def detail(r: Mapping[str, Any]) -> dict[str, Any]:
    """A record with its descriptor read: the card (and its evidence and agent), the MCP server and tools, or the
    skill's definition and SKILL.md."""
    out = {**summary(r), "statusReason": r.get("statusReason")}
    d = r.get("descriptors") or {}
    if "a2aAgentCard" in d:
        card = _loads((d["a2aAgentCard"] or {}).get("data"))
        out.update(card=card, evidence=extension(card, EVIDENCE), source=extension(card, SOURCE))
    if "mcpServer" in d:
        server = _loads((d["mcpServer"] or {}).get("data"))
        server = server if isinstance(server, dict) else {}
        tools = _loads((((d["mcpServer"] or {}).get("additionalData") or {}).get("tools") or {}).get("data")) or {}
        out.update(server=server, tools=list(tools.get("tools") or []) if isinstance(tools, dict) else [],
                   url=next((x.get("url") for x in server.get("remotes") or [] if isinstance(x, dict)), None))
    if "agentSkillsDefinition" in d:
        sd = d["agentSkillsDefinition"] or {}
        out.update(definition=_loads(sd.get("data")), skillMd=((sd.get("additionalData") or {}).get("skillMd") or {}).get("data"))
    if "custom" in d:
        raw = (d["custom"] or {}).get("data")
        parsed = _loads(raw)
        out["data"] = raw if parsed is None else parsed
    out["schemaVersion"] = next((v.get("dataSchemaVersion") for v in d.values() if isinstance(v, dict) and v.get("dataSchemaVersion")), None)
    return out


def records(session: Any, region: str, ident: str, kind: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    filters = []
    if kind:
        filters.append({"name": "recordType", "values": [_kind(kind)]})
    if status:
        if status not in RECORD_STATUSES:
            raise RegistryError(f"status: one of {', '.join(RECORD_STATUSES)}")
        filters.append({"name": "status", "values": [status]})
    kwargs: dict[str, Any] = {"registryId": registry_id(ident), "maxResults": 100}
    if filters:
        kwargs["filters"] = filters
    return [summary(r) for r in _pages(_ctl(session, region).list_registry_records, "registryRecords", **kwargs)]


def get_record(session: Any, region: str, ident: str, rec: str) -> dict[str, Any]:
    ctl = _ctl(session, region)
    out = detail(ctl.get_registry_record(registryId=registry_id(ident), recordId=record_id(rec)))
    tags = _tags(ctl, str(out["arn"]))
    return {**out, "tags": tags, "console": _console(tags)}


# -- records: what goes in them -------------------------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:64] or "skill"


def describe_prompt(prompt: str, limit: int = 300) -> str:
    """The prompt's first paragraph that is not a heading, on one line (a card's description)."""
    para = next((p for p in re.split(r"\n\s*\n", prompt or "") if p.strip() and not p.lstrip().startswith("#")), "")
    text = " ".join(para.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def invoke_url(agent: Mapping[str, Any], region: str) -> str:
    """Where the agent is invoked: a Runtime's invocations (its A2A endpoint when it serves A2A), a Harness's InvokeHarness."""
    arn = quote(str(agent.get("arn") or ""), safe="")
    if agent.get("kind") == "runtime":
        return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{arn}/invocations/"
    return f"https://bedrock-agentcore.{region}.amazonaws.com/harnesses/invoke?harnessArn={arn}"


def agent_card(agent: Mapping[str, Any], region: str, notes: Mapping[str, str] | None = None) -> dict[str, Any]:
    """An A2A 0.3 card for an agent from :func:`agents.get_agent`: its prompt's opening as the description, a skill per
    Gateway tool target and per S3 skill (``notes``: SKILL.md descriptions by URI), and the agent it describes."""
    kind, name = str(agent.get("kind") or ""), str(agent.get("name") or "")
    description = describe_prompt(str(agent.get("systemPrompt") or "")) or f"AgentCore {'Harness' if kind == 'harness' else 'Runtime'} {name}"
    skills: list[dict[str, Any]] = []
    for tool in agent.get("tools") or []:
        if tool.get("name"):
            skills.append({"id": _slug(tool["name"]), "name": str(tool["name"]), "tags": ["tools", str(tool.get("type") or "tool")],
                           "description": f"Tools of the AgentCore Gateway target {tool['name']}"})
    for skill in agent.get("skills") or []:
        uri = str(((skill or {}).get("s3") or {}).get("uri") or "")
        if uri:
            label = uri.rstrip("/").rsplit("/", 1)[-1]
            skills.append({"id": _slug(label), "name": label, "tags": ["skill"], "description": (notes or {}).get(uri) or f"Agent skill {label}"})
    if not skills:
        skills = [{"id": "conversation", "name": name, "description": description, "tags": ["chat"]}]
    seen: set[str] = set()
    for s in skills:  # ids are unique in a card
        base, n = s["id"], 2
        while s["id"] in seen:
            s["id"], n = f"{base[:60]}-{n}", n + 1
        seen.add(s["id"])
    protocol = ((agent.get("protocol") or {}).get("serverProtocol")) if kind == "runtime" else "HARNESS"
    source = {"kind": kind, "id": agent.get("id"), "name": name, "arn": agent.get("arn"), "runtimeArn": agent.get("runtimeArn"),
              "region": region, "protocol": protocol}
    card: dict[str, Any] = {"protocolVersion": A2A_VERSION, "name": name, "description": description, "url": invoke_url(agent, region),
                            "version": DEFAULT_VERSION,
                            "capabilities": {"streaming": kind == "harness" or protocol == "A2A", "extensions": [
                                {"uri": SOURCE, "description": "The AgentCore agent this card describes (ADLC console)", "required": False,
                                 "params": {k: v for k, v in source.items() if v}}]},
                            "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"], "skills": skills}
    if protocol == "A2A":
        card["preferredTransport"] = "JSONRPC"
    return card


def check_card(card: Any) -> dict[str, Any]:
    """The card as the service will take it, or an error naming what is missing (the service's 400 does not)."""
    if isinstance(card, str):
        try:
            card = json.loads(card)
        except ValueError as exc:
            raise RegistryError(f"card: not JSON ({exc})") from exc
    if not isinstance(card, dict):
        raise RegistryError("card: an A2A agent card (a JSON object)")
    missing = [f for f in CARD_FIELDS if card.get(f) in (None, "")]
    if missing:
        raise RegistryError(f"card: an A2A 0.3 card needs {', '.join(missing)}")
    if not isinstance(card["skills"], list) or not isinstance(card["capabilities"], dict):
        raise RegistryError("card: skills is a list and capabilities an object")
    for i, s in enumerate(card["skills"]):
        lacking = [f for f in SKILL_FIELDS if not isinstance(s, dict) or s.get(f) in (None, "")]
        if lacking:
            raise RegistryError(f"card: skill {i + 1} needs {', '.join(lacking)} (tags: a list of words)")
    _data(card, "the card")
    return card


def attach_evidence(card: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    """The card with ``evidence`` as its verification extension (an earlier one replaced)."""
    out = json.loads(json.dumps(card))
    caps = out.setdefault("capabilities", {})
    others = [e for e in caps.get("extensions") or [] if not (isinstance(e, dict) and e.get("uri") == EVIDENCE)]
    holding = f"{evidence.get('holding')}/{evidence.get('contracts')}"
    caps["extensions"] = others + [{"uri": EVIDENCE, "required": False, "params": dict(evidence),
                                    "description": f"ADLC console verification {evidence.get('jobId')}: {holding} contracts held in every one of "
                                                   f"{evidence.get('repeat')} round(s) ({'robust' if evidence.get('robust') else 'not robust'})"}]
    return out


def mcp_server(name: str, url: str, description: str = "", transport: str = "streamable-http") -> dict[str, Any]:
    """An MCP registry server.json for a remote server (live: an ASCII ``namespace/name`` and at most 100 characters
    of description, or "mcp.server data does not match any supported version")."""
    if not re.match(r"^https?://\S+$", str(url or "")):
        raise RegistryError("url: the MCP server's http(s) endpoint")
    if transport not in ("streamable-http", "sse"):
        raise RegistryError("transport: streamable-http or sse")
    short = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip("-.")[:180] or "server"
    text = " ".join(str(description or f"MCP server {name}").split())
    return {"name": f"io.adlc/{short}", "description": text if len(text) <= MCP_DESCRIPTION else text[:MCP_DESCRIPTION - 1] + "…",
            "version": DEFAULT_VERSION, "remotes": [{"type": transport, "url": str(url)}]}


def mcp_tools(tools: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The tools document (live: an object ``{"tools": [...]}``; a bare list is refused)."""
    out = []
    for t in tools:
        if not isinstance(t, Mapping) or not t.get("name"):
            raise RegistryError("each tool needs a name")
        tool = {"name": str(t["name"]), "description": str(t.get("description") or ""), "inputSchema": dict(t.get("inputSchema") or {"type": "object"})}
        if isinstance(t.get("outputSchema"), Mapping):
            tool["outputSchema"] = dict(t["outputSchema"])
        out.append(tool)
    return {"tools": out}


def gateways(session: Any, region: str) -> list[dict[str, Any]]:
    """The workspace's MCP Gateways (what an MCP record can describe)."""
    ctl = client(session, "bedrock-agentcore-control", region)
    return [{"id": g.get("gatewayId"), "name": g.get("name"), "status": g.get("status"), "authorizer": g.get("authorizerType"),
             "description": g.get("description") or ""} for g in _pages(ctl.list_gateways, "items", maxResults=100) if g.get("protocolType") == "MCP"]


def gateway_mcp(session: Any, region: str, gateway: str) -> dict[str, Any]:
    """A Gateway as an MCP server: its URL and the tools its targets declare inline (``<target>___<tool>``)."""
    ctl = client(session, "bedrock-agentcore-control", region)
    g = ctl.get_gateway(gatewayIdentifier=gateway)
    if g.get("protocolType") != "MCP":
        raise RegistryError(f"Gateway {g.get('name')} is not an MCP Gateway")
    tools, opaque = [], []
    for target in _pages(ctl.list_gateway_targets, "items", gatewayIdentifier=g["gatewayId"]):
        got = ctl.get_gateway_target(gatewayIdentifier=g["gatewayId"], targetId=target["targetId"])
        mcp = (got.get("targetConfiguration") or {}).get("mcp") or {}
        inline = next((((v or {}).get("toolSchema") or {}).get("inlinePayload") for v in mcp.values()
                       if isinstance(v, dict) and ((v.get("toolSchema") or {}).get("inlinePayload"))), None)
        if inline is None:
            opaque.append(str(got.get("name")))  # OpenAPI, Smithy or MCP-server targets: their tools are known only to the Gateway
        for tool in inline or []:
            tools.append({**tool, "name": f"{got.get('name')}___{tool.get('name')}"})
    return {"id": g["gatewayId"], "name": g.get("name"), "url": g.get("gatewayUrl"), "authorizer": g.get("authorizerType"),
            "description": g.get("description") or f"AgentCore Gateway {g.get('name')}", "tools": tools, "unlistedTargets": opaque}


def s3_location(uri: str) -> tuple[str, str, str]:
    """``(bucket, prefix, key)`` of a skill: ``s3://bucket/prefix/`` (the folder holding SKILL.md) or its SKILL.md."""
    found = S3_URI.match(str(uri or "").strip())
    if not found:
        raise RegistryError("uri: s3://bucket/prefix/ (the folder holding SKILL.md) or s3://bucket/prefix/SKILL.md")
    bucket, rest = found.groups()
    if rest.endswith("SKILL.md"):
        return bucket, rest[:-len("SKILL.md")], rest
    prefix = rest if (rest.endswith("/") or not rest) else rest + "/"
    return bucket, prefix, prefix + "SKILL.md"


def frontmatter(markdown: str) -> dict[str, Any]:
    text = markdown.lstrip("﻿")
    end = text.find("\n---", 3)
    if not text.startswith("---") or end < 0:
        raise RegistryError("SKILL.md has no frontmatter: it must start with --- name / description --- (the registry refuses it)")
    import yaml

    try:
        meta = yaml.safe_load(text[3:end])
    except yaml.YAMLError as exc:
        raise RegistryError(f"SKILL.md frontmatter is not YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise RegistryError("SKILL.md frontmatter is not a mapping")
    return meta


def skill_definition(uri: str, markdown: str) -> dict[str, Any]:
    bucket, prefix, _key = s3_location(uri)
    meta = frontmatter(markdown)
    name, description = str(meta.get("name") or ""), " ".join(str(meta.get("description") or "").split())
    if not (SKILL_NAME.match(name) and len(name) <= 64):
        raise RegistryError(f"SKILL.md name {name!r}: 1-64 lowercase letters, digits and single hyphens (the registry refuses others)")
    if not description:
        raise RegistryError("SKILL.md frontmatter needs a description")
    _data(markdown, "SKILL.md")
    return {"name": name, "description": description, "version": str(meta.get("version") or DEFAULT_VERSION), "path": f"s3://{bucket}/{prefix}"}


def read_skill(session: Any, region: str, uri: str) -> str:
    bucket, _prefix, key = s3_location(uri)
    body = client(session, "s3", region).get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RegistryError(f"s3://{bucket}/{key} is not UTF-8 text") from exc


def skill_notes(session: Any, region: str, agent: Mapping[str, Any]) -> dict[str, str]:
    """The descriptions in an agent's SKILL.md files, by S3 URI (best effort: an unreadable one keeps a generic line)."""
    notes = {}
    for skill in agent.get("skills") or []:
        uri = str(((skill or {}).get("s3") or {}).get("uri") or "")
        try:
            notes[uri] = " ".join(str(frontmatter(read_skill(session, region, uri)).get("description") or "").split())[:300]
        except Exception:  # noqa: BLE001 - the card is a draft the user reviews
            continue
    return {k: v for k, v in notes.items() if v}


def descriptor(kind: str, content: Mapping[str, Any]) -> dict[str, Any]:
    """CreateRegistryRecord ``descriptors`` for prepared ``content`` (card / server+tools / definition+markdown).
    No dataSchemaVersion: the service picks the version that validates."""
    if kind == "agent":
        return {"a2aAgentCard": {"data": _data(content["card"], "the card")}}
    if kind == "mcp":
        mcp: dict[str, Any] = {"data": _data(content["server"], "server.json")}
        if content.get("tools"):
            mcp["additionalData"] = {"tools": {"data": _data(mcp_tools(content["tools"]), "the tools")}}
        return {"mcpServer": mcp}
    return {"agentSkillsDefinition": {"data": _data(content["definition"], "the skill definition"),
                                      "additionalData": {"skillMd": {"data": _data(content["markdown"], "SKILL.md")}}}}


def descriptor_update(kind: str, content: Mapping[str, Any]) -> dict[str, Any]:
    """The same content in UpdateRegistryRecord's PATCH wrappers (an MCP server without tools unsets them)."""
    def opt(value: Any) -> dict[str, Any]:
        return {"optionalValue": value}

    if kind == "agent":
        inner = {"a2aAgentCard": opt({"data": opt(_data(content["card"], "the card"))})}
    elif kind == "mcp":
        tools = content.get("tools")
        extra = opt({"tools": opt({"data": opt(_data(mcp_tools(tools), "the tools"))})}) if tools else {}
        inner = {"mcpServer": opt({"data": opt(_data(content["server"], "server.json")), "additionalData": extra})}
    else:
        inner = {"agentSkillsDefinition": opt({"data": opt(_data(content["definition"], "the skill definition")),
                                               "additionalData": opt({"skillMd": opt({"data": opt(_data(content["markdown"], "SKILL.md"))})})})}
    return opt(inner)


def content(session: Any, region: str, kind: str, body: Mapping[str, Any], name: str = "") -> dict[str, Any]:
    """What a record of ``kind`` holds, from the form: ``card`` (agent); ``gatewayId`` or ``url`` (+ ``tools``,
    ``transport``) (mcp); ``uri`` (skill, read from S3). Also the defaults it suggests: name, description."""
    if kind == "agent":
        card = check_card(body.get("card"))
        return {"card": card, "name": card["name"], "description": card["description"]}
    if kind == "mcp":
        if body.get("gatewayId"):
            g = gateway_mcp(session, region, str(body["gatewayId"]))
            return {"server": mcp_server(name or g["name"], g["url"], g["description"]), "tools": g["tools"], "name": g["name"],
                    "description": g["description"], "unlistedTargets": g["unlistedTargets"]}
        server = mcp_server(name or "server", str(body.get("url") or ""), str(body.get("description") or ""), str(body.get("transport") or "streamable-http"))
        tools = body.get("tools") or []
        if not isinstance(tools, list):
            raise RegistryError("tools: a list of {name, description, inputSchema}")
        return {"server": server, "tools": tools, "name": name, "description": server["description"]}
    if kind == "skill":
        uri = str(body.get("uri") or "")
        s3_location(uri)
        markdown = read_skill(session, region, uri)
        definition = skill_definition(uri, markdown)
        return {"definition": definition, "markdown": markdown, "name": definition["name"], "description": definition["description"]}
    raise RegistryError("type: agent, mcp or skill")


def record_request(kind: str, body: Mapping[str, Any], prepared: Mapping[str, Any]) -> dict[str, Any]:
    """CreateRegistryRecord's fields (registryId apart): name, type, descriptors, version, description, the console tag."""
    name = str(body.get("name") or prepared.get("name") or "").strip()
    if not RECORD_NAME.match(name):
        raise RegistryError("name: a letter or digit, then letters, digits, '_', '-', '.' or '/' (at most 255)")
    version = str(body.get("version") or DEFAULT_VERSION).strip()
    if not VERSION.match(version):
        raise RegistryError("version: letters, digits, '.' and '-' (such as 1.0.0)")
    request: dict[str, Any] = {"name": name, "recordType": _kind(kind), "descriptors": descriptor(kind, prepared), "recordVersion": version,
                               "tags": dict(CONSOLE_TAG)}
    description = " ".join(str(body.get("description") or prepared.get("description") or "").split())
    if description:
        request["description"] = description[:4096]
    display = str(body.get("displayName") or "").strip()
    if display:
        request["displayName"] = display[:255]
    return request


# -- records: changing them -----------------------------------------------------------------------------------------

def create_record(session: Any, region: str, ident: str, body: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(body.get("type") or "")
    _kind(kind)
    prepared = content(session, region, kind, body, str(body.get("name") or "").strip())
    request = record_request(kind, body, prepared)
    out = _ctl(session, region).create_registry_record(registryId=registry_id(ident), **request)
    return {"id": str(out["recordArn"]).rsplit("/", 1)[-1], "arn": out["recordArn"], "status": out.get("status"), "name": request["name"],
            "type": kind, "version": request["recordVersion"], "unlistedTargets": prepared.get("unlistedTargets") or []}


def _mine(ctl: Any, ident: str, rec: str) -> dict[str, Any]:
    """The record, if this console created it and it can still change."""
    got = ctl.get_registry_record(registryId=registry_id(ident), recordId=record_id(rec))
    if not _console(_tags(ctl, str(got["recordArn"]))):
        raise RegistryError("only records created from this console can be changed or deleted here")
    if got.get("status") == "DEPRECATED":
        raise RegistryError("a DEPRECATED record is terminal: it cannot change any more")
    return got


def update_record(session: Any, region: str, ident: str, rec: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Change a console record's description, display name, version and/or content (the form's fields for its type).
    The record returns to DRAFT: it needs approving again."""
    ctl = _ctl(session, region)
    current = _mine(ctl, ident, rec)
    kind = TYPE_KIND.get(str(current.get("recordType")), "")
    changes: dict[str, Any] = {}
    if body.get("description"):
        changes["description"] = {"optionalValue": " ".join(str(body["description"]).split())[:4096]}
    if body.get("displayName"):
        changes["displayName"] = {"optionalValue": str(body["displayName"]).strip()[:255]}
    if body.get("version"):
        if not VERSION.match(str(body["version"])):
            raise RegistryError("version: letters, digits, '.' and '-' (such as 1.1.0)")
        changes["recordVersion"] = str(body["version"])
    if kind in KINDS and any(body.get(k) for k in ("card", "gatewayId", "url", "uri")):
        changes["descriptors"] = descriptor_update(kind, content(session, region, kind, body, str(current.get("name") or "")))
    if not changes:
        raise RegistryError("nothing to change: give description, displayName, version or the content")
    out = ctl.update_registry_record(registryId=ident, recordId=rec, **changes)
    return {"id": rec, "status": out.get("status"), "version": out.get("recordVersion")}


def delete_record(session: Any, region: str, ident: str, rec: str) -> dict[str, Any]:
    """Delete a record this console created (tag ``adlc:console=1``); anything else is refused."""
    ctl = _ctl(session, region)
    got = ctl.get_registry_record(registryId=registry_id(ident), recordId=record_id(rec))
    if not _console(_tags(ctl, str(got["recordArn"]))):
        raise RegistryError("only records created from this console can be deleted here")
    ctl.delete_registry_record(registryId=ident, recordId=rec)
    return {"id": rec, "deleted": True}


def _theirs_acknowledged(ctl: Any, ident: str, rec: str, acknowledged: Any, verb: str) -> None:
    """A record the console did not create is acted on only when the caller confirms (``acknowledged: true``)."""
    got = ctl.get_registry_record(registryId=registry_id(ident), recordId=record_id(rec))
    if not _console(_tags(ctl, str(got["recordArn"]))) and acknowledged is not True:
        raise RegistryError(f"{got.get('name') or rec} was not created from this console: send acknowledged: true to {verb} it")


def submit(session: Any, region: str, ident: str, rec: str, acknowledged: Any = False) -> dict[str, Any]:
    """DRAFT → PENDING_APPROVAL (APPROVED at once where the registry auto-approves). Another team's record only with
    ``acknowledged: true``."""
    ctl = _ctl(session, region)
    _theirs_acknowledged(ctl, ident, rec, acknowledged, "submit")
    out = ctl.submit_registry_record_for_approval(registryId=registry_id(ident), recordId=record_id(rec))
    return {"id": rec, "status": out.get("status")}


def set_status(session: Any, region: str, ident: str, rec: str, status: str, reason: str = "", by: str = "", acknowledged: Any = False) -> dict[str, Any]:
    """Approve, reject or deprecate (the curator's decision; DEPRECATED cannot be undone). Another team's record only
    with ``acknowledged: true``."""
    target = str(status or "").upper()
    if target not in TARGETS:
        raise RegistryError("status: APPROVED, REJECTED or DEPRECATED")
    ctl = _ctl(session, region)
    _theirs_acknowledged(ctl, ident, rec, acknowledged, {"APPROVED": "approve", "REJECTED": "reject", "DEPRECATED": "deprecate"}[target])
    note = " ".join(str(reason or "").split())
    text = f"{TARGETS[target]} in the ADLC console{f' by {by}' if by else ''}{f': {note}' if note else ''}"[:255]
    out = ctl.update_registry_record_status(registryId=registry_id(ident), recordId=record_id(rec), status=target, statusReason=text)
    return {"id": rec, "status": out.get("status"), "statusReason": out.get("statusReason")}


# -- evidence -------------------------------------------------------------------------------------------------------

def latest_verification(jobs: Any, workspace: str, agent_id: str) -> dict[str, Any] | None:
    """The newest finished console verification (``verify`` job) of this agent as it is, as evidence: robust, how many
    contracts held in every round, which did not, and the job that shows it. A job that verified something else under
    the agent's id — an experiment's treatment or a canary's candidate (a ``treatment`` marker), or a run with another
    prompt or model — is not this agent's evidence."""
    for job in jobs.list(workspace=workspace, kind="verify", limit=500):
        params = job.get("params") or {}
        if job.get("status") != "succeeded" or params.get("agentId") != agent_id or params.get("treatment") or params.get("runtime"):
            continue
        try:
            full = jobs.get(job["id"])
        except (KeyError, OSError, ValueError):  # removed, or being rewritten
            continue
        doc = full.get("result") or {}
        if doc.get("treatment") or doc.get("promptOverride") or doc.get("modelOverride"):
            continue
        contracts = doc.get("contracts") or []
        return {"jobId": job["id"], "agent": params.get("agent"), "agentId": agent_id, "robust": bool(doc.get("robust")),
                "holding": int(doc.get("holding") or 0), "contracts": len(contracts), "repeat": doc.get("repeat") or params.get("repeat"),
                "notHolding": [str(c.get("id")) for c in contracts if not c.get("holds")][:20], "contractSet": params.get("contractSet"),
                "panel": bool(params.get("panel")), "model": (doc.get("harness") or {}).get("model"),
                "verifiedAt": doc.get("generatedAt") or full.get("finishedAt")}
    return None


def publish(session: Any, region: str, ident: str, rec: str, verification: Callable[[str], Mapping[str, Any] | None], *,
            agent_id: str | None = None, sleep: Callable[[float], None] = time.sleep, timeout: float = 60.0) -> dict[str, Any]:
    """Attach the agent's latest verification to its card and submit the record: the approver sees the evidence.

    ``verification(agent_id)`` gives the evidence (:func:`latest_verification`); the agent is the one the card names
    (``urn:adlc:console:agent``) unless ``agent_id`` is given. The update returns the record to DRAFT (about a second),
    then it is submitted."""
    ctl = _ctl(session, region)
    current = _mine(ctl, ident, rec)
    if current.get("recordType") != "AGENT":
        raise RegistryError("evidence is a verification of an agent: publish an agent record")
    card = _loads(((current.get("descriptors") or {}).get("a2aAgentCard") or {}).get("data"))
    if not isinstance(card, dict):
        raise RegistryError("the record has no readable agent card")
    agent = agent_id or (extension(card, SOURCE) or {}).get("id")
    if not agent:
        raise RegistryError("the card names no agent (no urn:adlc:console:agent extension): give agentId")
    evidence = verification(str(agent))
    if not evidence:
        raise RegistryError(f"no finished console verification of {agent}: verify it first (评估 → 验证)")
    if current.get("status") != "DRAFT" or extension(card, EVIDENCE) != dict(evidence):
        ctl.update_registry_record(registryId=ident, recordId=rec,
                                   descriptors=descriptor_update("agent", {"card": attach_evidence(card, evidence)}))
        deadline = time.monotonic() + timeout
        while True:
            got = ctl.get_registry_record(registryId=ident, recordId=rec)
            if got.get("status") not in ("CREATING", "UPDATING") or time.monotonic() > deadline:
                break
            sleep(1.0)
        if got.get("status") != "DRAFT":
            reason = got.get("statusReason")
            raise RegistryError(f"the record is {got.get('status')} after the update, not DRAFT" + (f": {reason}" if reason else ""))
    out = ctl.submit_registry_record_for_approval(registryId=ident, recordId=rec)
    return {"id": rec, "status": out.get("status"), "evidence": dict(evidence)}


# -- discovery ------------------------------------------------------------------------------------------------------

def search(session: Any, region: str, ident: str, query: str, kind: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """The registry's semantic search: APPROVED records only (an empty query lists them)."""
    request: dict[str, Any] = {"registryIds": [registry_id(ident)], "searchQuery": str(query or "").strip()[:256],
                               "maxResults": max(1, min(int(limit), 20))}
    if kind:
        request["filters"] = {"recordType": {"$eq": _kind(kind)}}  # live: a bare value is refused ("must be a JSON object with an operator")
    got = client(session, DISCOVERY, region).search_discoverable_registry_records(**request)
    return [detail(r) for r in got.get("registryRecords") or []]


# -- routes ---------------------------------------------------------------------------------------------------------

def _aws(exc: BaseException) -> tuple[int, str] | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict) or "Error" not in response:
        return None
    error = response.get("Error") or {}
    status = int((response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 500)
    return (status if 400 <= status < 500 else 502), f"{error.get('Code') or type(exc).__name__}: {error.get('Message') or exc}"


def guarded(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """A route whose refusals are answers: the console's own checks a 400, AWS's errors their own 4xx (502 for 5xx)."""
    def run(r: Any) -> Any:
        try:
            return fn(r)
        except (RegistryError, AgentError) as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - only AWS errors are answered here; the rest stays a 500
            if type(exc).__name__ == "ParamValidationError":
                return 400, {"error": " ".join(str(exc).split())[:600]}
            found = _aws(exc)
            if found is None:
                raise
            return found[0], {"error": found[1]}

    run.__name__ = getattr(fn, "__name__", "route")
    return run


def register(router: Any) -> None:
    from . import agents

    def sess(r: Any) -> tuple[Any, str]:
        session = r.session()
        return session, r.console.workspaces.get(r.workspace())["region"]

    def card(r: Any):
        session, region = sess(r)
        agent = agents.get_agent(session, region, str(r.query.get("kind") or "harness"), str(r.query.get("ident") or ""))
        return 200, {"card": agent_card(agent, region, skill_notes(session, region, agent)),
                     "agent": {k: agent.get(k) for k in ("kind", "name", "id", "arn", "status")}}

    def one(r: Any):
        session, region = sess(r)
        out = get_record(session, region, r.params["rid"], r.params["rec"])
        if (out.get("source") or {}).get("id"):
            out["latestVerification"] = latest_verification(r.console.jobs, r.workspace(), str(out["source"]["id"]))
        return 200, out

    def publish_route(r: Any):
        session, region = sess(r)
        wid = r.workspace()
        return 200, publish(session, region, r.params["rid"], r.params["rec"], lambda agent: latest_verification(r.console.jobs, wid, agent),
                            agent_id=str(r.body.get("agentId") or "") or None)

    def status_route(r: Any):
        session, region = sess(r)
        return 200, set_status(session, region, r.params["rid"], r.params["rec"], str(r.body.get("status") or ""), str(r.body.get("reason") or ""),
                               by=str(r.caller.get("username") or ""), acknowledged=r.body.get("acknowledged"))

    base, record = "/workspaces/{wid}/registries", "/workspaces/{wid}/registries/{rid}/records/{rec}"
    routes = [
        ("GET", base, lambda r: (200, {"registries": registries(*sess(r))}), False),
        ("POST", base, lambda r: (201, create_registry(*sess(r), r.body)), True),
        ("GET", f"{base}/agent-card", card, False),
        ("GET", f"{base}/gateways", lambda r: (200, {"gateways": gateways(*sess(r))}), False),
        ("DELETE", f"{base}/{{rid}}", lambda r: (200, delete_registry(*sess(r), r.params["rid"],
                                                                      with_records=r.query.get("withRecords") in ("1", "true"))), True),
        ("GET", f"{base}/{{rid}}/records", lambda r: (200, {"records": records(*sess(r), r.params["rid"], r.query.get("type") or None,
                                                                               r.query.get("status") or None)}), False),
        ("POST", f"{base}/{{rid}}/records", lambda r: (201, create_record(*sess(r), r.params["rid"], r.body)), False),
        ("GET", f"{base}/{{rid}}/search", lambda r: (200, {"records": search(*sess(r), r.params["rid"], r.query.get("q") or "",
                                                                             r.query.get("type") or None)}), False),
        ("GET", record, one, False),
        ("PUT", record, lambda r: (200, update_record(*sess(r), r.params["rid"], r.params["rec"], r.body)), False),
        ("DELETE", record, lambda r: (200, delete_record(*sess(r), r.params["rid"], r.params["rec"])), False),
        ("POST", f"{record}/submit", lambda r: (200, submit(*sess(r), r.params["rid"], r.params["rec"], r.body.get("acknowledged"))), False),
        ("POST", f"{record}/publish", publish_route, False),
        ("POST", f"{record}/status", status_route, True),  # approve / reject / deprecate: the curator
    ]
    for method, pattern, fn, admin in routes:
        router.add(method, pattern, guarded(fn), admin=admin)
