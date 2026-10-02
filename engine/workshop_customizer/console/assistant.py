"""Console module ``assistant``: the architect assistant. A person describes the agent they need; the assistant asks
clarifying questions, reads what the workspace has (knowledge bases, skills, Gateways, agents, models) and proposes a
reviewable agent specification with the behaviour contracts that will verify it, as numbered revisions. Nothing is
created until a person approves an exact revision. (Launchpad: ``backend/app/assistant/``; its invariants are kept.)

Invariants
* **Principal + workspace bound.** A conversation belongs to one workspace and one owner principal: the username and
  the user's creation time, so a recreated username inherits nothing (``local#open`` in open mode). Anything else is a
  404. An admin may share a conversation they can reach with the workspace: every member granted the workspace may then
  read it, take turns, edit and approve. Deleting it stays with the owner.
* **Discussion never writes AWS.** Turns, edits and the checks before an approval run on a :class:`ReadOnlySession`.
  Its clients are made up front and perform only the read operations in ``READ_ONLY`` (plus ``bedrock-runtime``
  Converse). Each call is recorded; anything else is refused before it is sent. The assistant has no tool that changes
  anything: ``propose_agent`` validates, and the turn stores a revision in the console's own store.
* **One in-flight turn per conversation.** A turn is claimed atomically on the conversation record (409 while another
  runs). A claim whose worker is not alive in this process (a restart, a crash) is taken over, and a worker whose
  claim was taken writes nothing more. The turn runs on its own thread: a closed browser does not lose it, the stream
  is only a view of it.
* **Server-owned bounded replay.** Earlier turns are replayed as text: the person's messages and the assistant's
  replies, the newest that fit ``REPLAY_CHARS`` / ``REPLAY_TURNS``, with any omission disclosed to the model. The
  current proposal and its validation are always in the system prompt. Within a turn the model's messages go back
  exactly as they came.
* **Monotonic, unique revisions.** A turn's last ``propose_agent`` submission and a person's edit both take the next
  number inside the store's lock. The content is stored verbatim with its validation errors and warnings, never
  "fixed", and a new revision supersedes the drafts before it. The hash covers the content and the resources it binds:
  knowledge bases by id and name, skills by version and content digest, Gateways by ARN, and the tool names L1 will see.
* **Approval is the only executor: the exact reviewed resources or nothing.** An approval names a revision and its
  hash, and only the latest valid draft can be approved. Before anything is written, the revision is validated again
  on a fresh read of the workspace, and its bindings are compared with the reviewed ones (each skill's bytes in S3
  with its digest). Any drift is refused, with what changed. Then one job, recording every step on the revision:
  creates the Harness, attaches each knowledge base (``kb.attach``), applies each skill version
  (``skills_lab.apply_to_agent``), saves the contract set and starts a verification (``evaluation``). A repeated
  approval returns the recorded outcome; a failed or interrupted one can be resumed from the step that did not finish.

Beyond Launchpad
* Every proposal carries **behaviour contracts** derived from the conversation, each with the words it comes from:
  what the agent must say, must not say, must refuse or hand off, and which tools it must or must not use. They use
  the console's contract schema (``direct.verify``) and are checked further here: value types; tool names as L1 sees
  them for exactly this agent's knowledge bases, skills and Gateways; handoffs L1 can detect; literal phrases L1 will
  miss (the refusal lesson of 2026-10-01).
* An approval creates the agent **and** its contract set **and** starts its verification: a new agent arrives with
  evidence, not just a deployment.
* Factual contracts are grounded in the knowledge base: ``search_knowledge_base`` runs the Retrieve the agent will use.
  ``list_models`` carries the platform's own evidence on which models load skills.

Live (one AWS account, us-west-2, 2026-10-02; the assistant on Claude Opus 5.5; the agent a loyalty-points FAQ agent
on a managed KB with the skill ``loyalty-reply-style``):
* **The conversation: 5 turns and one hand edit, 6 revisions.**
  - The turns took 129, 66, 71, 58 and 58 s, with 35, 21, 15, 12 and 9 AWS calls. Every call was a read on the ``READ_ONLY``
    allowlist: 14 distinct operations (Converse, the KB / Harness / model listings, Retrieve, s3 GetObject), no writes.
  - Before it proposed, turn 1 read the KB's 7 documents, the skill, the models and the agents, and searched the KB 7
    times. It found the superseded ``referral-policy-legacy-2023.md`` and added a contract against its stale
    "3天内 / 300积分".
  - It submitted twice in one reply; the second submission became revision 1, with 7 contracts. Each contract quotes
    the person or names its document.
  - Where the KB is silent (device points, transfers, membership tiers), the contracts check that the agent says it
    does not know.
  - The hand edit (one prompt line) became revision 3, with its diff. The next turns kept it.
* **The prompt said "3 to 8 contracts", and the model took that for a limit.** When asked to add one, it dropped
  another. The prompt now says up to 20.
* **UpdateHarness allows at most 64 characters per ``allowedTools`` entry.**
  - The first approval (revision 5) created the Harness, then ``kb.attach`` failed. The agent's deep search,
    ``@adlckb/agentic-adlc-probe-assistant-30f1a9___AgenticRetrieveStream``, is 67 characters.
  - It left the agent's ``agentic-…`` target behind, created before the update was refused. ``kb.detach`` removed it.
  - Since then ``kb.agentic_target`` shortens a long agent's target (a hash) to stay within 64, and a refused attach
    puts the target back; ``check_proposal`` still checks every resulting entry, and a Gateway toolName over 61.
  - Resuming revision 5 was then refused by the re-validation, before any write.
* **The approval of revision 6 took 3 min 39 s:**
  - Harness and role: 14 s;
  - READY: 2 min 32 s;
  - ``kb.attach``: 32 s;
  - the skill: 18 s;
  - the contract set and the verification start: 2 s.
* **The verification took 5.2 min: 2 rounds × 9 contracts, 6 held in both.** L1 saw the tools as ``Retrieve`` and
  ``skills``, the names the validator expects. Of the three that held in only 1 of 2 rounds:
  - Two failed only on a literal "I don't know": a reply said "没有在知识库中找到" / "没有在规则中写明", which does not
    contain "没有找到" / "没有写明". Such negation-led terms are now warned about, and the system prompt says so.
  - One was a real deviation: asked to look up a wife's points, the agent refused without saying "本人" and without
    loading the reply skill (no signature).
* **A chat turn with the agent took 12 s.** It used Retrieve and the skill, answered from 《推荐有礼规则》 and signed.
* **Caching.** With cache points after the tools, the system prompt and the newest message, a turn's later model calls
  read the earlier ones from the cache: 6-10 uncached input tokens per turn after the first.
* **Gotchas in other modules.**
  - A second console data dir against the same account must carry the first one's ``kb_attachments_<workspace>``:
    ``kb._apply`` rewrites the shared KB Gateway role's Retrieve grant from its own store, and a fresh store's detach
    would revoke the demo KB for the demo agent. This run was seeded with it, and the grant was
    byte-identical before and after.
  - ``agents.delete_agent`` right after an update is a ConflictException (the Harness is UPDATING), which the console
    reports as a 500.
  - ``skills_lab.remove_from_agent`` leaves the ``adlc-console-skills`` grant on the role.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import queue
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping, Sequence

from ..direct import verify
from ..direct.aws import client
from . import agents, evaluation, kb, registry, skills_lab
from .web import sse

DEFAULT_MODEL = "us.anthropic.claude-opus-5-5"
#: The assistant's own models, as the page offers them (any Bedrock model id is accepted).
ASSISTANT_MODELS = (("us.anthropic.claude-opus-5-5", "Claude Opus 5.5"), ("us.anthropic.claude-sonnet-5-5", "Claude Sonnet 5.5（更快）"))
CID = re.compile(r"^asst-[0-9a-f]{10}$")
MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9.:_\-/]{2,199}$")
MAX_MESSAGE = 20_000
MAX_TURNS = 100
MAX_REVISIONS = 50
MAX_MODEL_CALLS = 16
MAX_SUBMISSIONS = 4
MAX_TOKENS = 16_000
REPLAY_CHARS = 80_000
REPLAY_TURNS = 24
TOOL_RESULT_CHARS = 12_000
PROPOSAL_MAX_BYTES = 64_000
MAX_CONTRACTS = 20
DEFAULT_ROUNDS = 2
HEARTBEAT = 8.0
#: This process: a turn claimed under another boot belongs to a dead process.
BOOT = uuid.uuid4().hex
_sleep: Callable[[float], None] = time.sleep  # the tests make every wait instant

#: Everything a turn, an edit or an approval's checks may call: read operations only (and the model).
READ_ONLY: dict[str, frozenset[str]] = {
    "bedrock-runtime": frozenset({"converse"}),
    "bedrock-agent": frozenset({"list_knowledge_bases", "get_knowledge_base", "list_tags_for_resource", "list_data_sources", "get_data_source",
                                "list_ingestion_jobs", "list_knowledge_base_documents"}),
    "bedrock-agent-runtime": frozenset({"retrieve"}),
    "bedrock-agentcore-control": frozenset({"list_harnesses", "get_harness", "list_agent_runtimes", "list_tags_for_resource", "list_gateways",
                                            "get_gateway", "list_gateway_targets", "get_gateway_target"}),
    "s3": frozenset({"get_object"}),
    "bedrock": frozenset({"list_inference_profiles", "list_foundation_models"}),
}


class AssistantError(ValueError):
    """Refused, with the HTTP status the route answers and any structured detail."""

    def __init__(self, message: str, *, status: int = 400, **extra: Any):
        super().__init__(message)
        self.status, self.extra = status, extra


class ReadOnlyViolation(AssistantError):
    def __init__(self, message: str):
        super().__init__(message, status=403)


class _ClaimLost(Exception):
    """This turn's claim was taken over: it writes nothing more."""


class _Recorded(Exception):
    def __init__(self, approval: Mapping[str, Any]):
        super().__init__("recorded")
        self.approval = dict(approval)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- the read-only session ------------------------------------------------------------------------------------------------

class _ReadOnlyClient:
    def __init__(self, service: str, inner: Any, calls: list[str]):
        self._service, self._inner, self._calls = service, inner, calls

    def __getattr__(self, op: str) -> Any:
        if op.startswith("__"):
            raise AttributeError(op)
        if op not in READ_ONLY.get(self._service, ()):
            raise ReadOnlyViolation(f"the discussion is read-only: {self._service}:{op} was refused")
        fn = getattr(self._inner, op)

        def call(*args: Any, **kwargs: Any) -> Any:
            self._calls.append(f"{self._service}:{op}")
            return fn(*args, **kwargs)

        return call


class ReadOnlySession:
    """What a turn, an edit and an approval's checks get instead of the workspace's session. Its clients are made up
    front, on the request's thread (a boto3 session must not make clients from several threads at once), and perform
    only the operations in :data:`READ_ONLY`, each recorded in :attr:`calls`."""

    def __init__(self, session: Any, region: str, calls: list[str] | None = None):
        self.calls: list[str] = calls if calls is not None else []
        self._made: dict[str, Any] = {}
        for service in READ_ONLY:
            try:
                self._made[service] = _ReadOnlyClient(service, client(session, service, region), self.calls)
            except Exception as exc:  # noqa: BLE001 - raised when the service is used
                self._made[service] = exc

    def client(self, name: str, region_name: str | None = None, **_: Any) -> Any:
        made = self._made.get(name)
        if made is None:
            raise ReadOnlyViolation(f"the discussion is read-only: {name} is not available to it")
        if isinstance(made, Exception):
            raise made
        return made


# -- what the assistant may read ---------------------------------------------------------------------------------------

NON_TEXT = re.compile(r"embed|rerank|stability|stable-|pegasus|upscale|image|canvas|reel|sonic|twelvelabs", re.I)
#: Shown with list_models: the platform's own evidence (Skill Lab, 2026-10-01) on how these models use skills.
RECOMMENDED_MODELS = (
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "Claude Haiku 4.5: fast and cheap; loaded the console's skills on every call in Skill Lab's live runs"),
    ("us.anthropic.claude-sonnet-5-5", "Claude Sonnet 5.5: stronger reasoning and instruction following, slower; loaded every skill in Skill Lab"),
    ("us.amazon.nova-2-lite-v1:0", "Nova 2 Lite: fastest and cheapest; skipped a generically described skill on every call in Skill Lab"),
    ("us.anthropic.claude-opus-5-5", "Claude Opus 5.5: the strongest, slowest and most expensive"),
)


def _attachable(g: Mapping[str, Any]) -> tuple[bool, str | None]:
    name = str(g.get("name") or "")
    if name == kb.GATEWAY:
        return False, "the console's knowledge-base Gateway: choose knowledge bases (knowledgeBases) instead"
    if name.startswith(("adlc-exp", "adlc-console-exp", "adlc-console-can")):  # an A/B test's or a canary's (and those made before the rename)
        return False, "an A/B test's Gateway, managed by the experiment"
    if g.get("status") != "READY":
        return False, f"it is {g.get('status')}, not READY"
    if g.get("authorizer") != "AWS_IAM":
        return False, f"its authorizer is {g.get('authorizer')}: a console Harness calls Gateways with its own role (AWS_IAM only)"
    return True, None


def _try(fn: Callable[[], Any]) -> tuple[Any, str | None]:
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001 - reported where it matters
        return None, f"{type(exc).__name__}: {str(exc)[:200]}"


class Discovery:
    """The workspace as the assistant reads it (one turn, edit or approval check); each listing is read once."""

    def __init__(self, console: Any, workspace: str, session: Any, region: str, account: str):
        self.console, self.workspace, self.session, self.region, self.account = console, workspace, session, region, account
        self._cache: dict[Any, Any] = {}

    def _once(self, key: Any, load: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = load()
        return self._cache[key]

    def kbs(self) -> list[dict[str, Any]]:
        return self._once("kbs", lambda: kb.list_kbs(self.session, self.region))

    def kb_detail(self, kb_id: str) -> dict[str, Any]:
        return self._once(("kb", kb_id), lambda: kb.detail(self.session, self.region, kb_id))

    def library(self) -> dict[str, Any]:
        return skills_lab.library(self.console, self.workspace)

    def bucket(self) -> str:
        return kb.console_bucket(self.account, self.region)

    def skill_bytes(self, name: str, version: str) -> bytes:
        got = client(self.session, "s3", self.region).get_object(Bucket=self.bucket(), Key=f"skills/{name}/{version}/SKILL.md",
                                                                  ExpectedBucketOwner=self.account)
        return got["Body"].read()

    def gateways(self) -> list[dict[str, Any]]:
        def load() -> list[dict[str, Any]]:
            return [{**g, "attachable": _attachable(g)[0], "reason": _attachable(g)[1]} for g in registry.gateways(self.session, self.region)]

        return self._once("gateways", load)

    def gateway(self, gid: str) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            got = client(self.session, "bedrock-agentcore-control", self.region).get_gateway(gatewayIdentifier=gid)
            g = {"id": got.get("gatewayId"), "name": got.get("name"), "arn": got.get("gatewayArn"), "status": got.get("status"),
                 "authorizer": got.get("authorizerType"), "description": got.get("description") or ""}
            ok, why = _attachable(g)
            if got.get("protocolType") != "MCP":
                ok, why = False, f"its protocol is {got.get('protocolType')}, not MCP"
            mcp = registry.gateway_mcp(self.session, self.region, gid) if got.get("protocolType") == "MCP" else {"tools": [], "unlistedTargets": []}
            listed = [{"name": t.get("name"), "description": str(t.get("description") or "")[:200]} for t in mcp.get("tools") or []]
            return {**g, "attachable": ok, "reason": why, "listed": listed, "unlistedTargets": list(mcp.get("unlistedTargets") or []),
                    "tools": None if mcp.get("unlistedTargets") else [t["name"] for t in listed]}

        return self._once(("gateway", gid), load)

    def agents(self) -> list[dict[str, Any]]:
        return self._once("agents", lambda: agents.list_agents(self.session, self.region))

    def harnesses(self) -> list[dict[str, Any]]:
        return [a for a in self.agents() if a.get("kind") == "harness"]

    def models(self) -> tuple[dict[str, str] | None, str | None]:
        """``({model id: name}, None)`` of the active text models (inference profiles and on-demand models), or
        ``(None, why)`` when they cannot be read."""
        def load() -> tuple[dict[str, str] | None, str | None]:
            bedrock = client(self.session, "bedrock", self.region)
            out: dict[str, str] = {}
            try:
                kwargs: dict[str, Any] = {"typeEquals": "SYSTEM_DEFINED", "maxResults": 1000}
                for _ in range(20):
                    page = bedrock.list_inference_profiles(**kwargs)
                    for p in page.get("inferenceProfileSummaries") or []:
                        pid = str(p.get("inferenceProfileId") or "")
                        if pid and p.get("status") == "ACTIVE" and not NON_TEXT.search(pid):
                            out[pid] = str(p.get("inferenceProfileName") or "")
                    if not page.get("nextToken"):
                        break
                    kwargs["nextToken"] = page["nextToken"]
                for m in bedrock.list_foundation_models(byInferenceType="ON_DEMAND").get("modelSummaries") or []:
                    mid = str(m.get("modelId") or "")
                    if (mid and (m.get("modelLifecycle") or {}).get("status", "ACTIVE") == "ACTIVE" and "TEXT" in (m.get("outputModalities") or [])
                            and not NON_TEXT.search(mid)):
                        out.setdefault(mid, str(m.get("modelName") or ""))
            except Exception as exc:  # noqa: BLE001 - the proposal says so instead of guessing
                return None, f"{type(exc).__name__}: {str(exc)[:160]}"
            return out, None

        return self._once("models", load)


def _arg(args: Mapping[str, Any], key: str) -> str:
    value = str(args.get(key) or "").strip()
    if not value:
        raise AssistantError(f"{key} is required")
    return value


def run_tool(name: str, args: Mapping[str, Any], disc: Discovery) -> Any:
    """One read-only discovery tool's result (propose_agent is the turn's)."""
    if name == "list_knowledge_bases":
        return {"knowledgeBases": [{k: x.get(k) for k in ("id", "name", "status", "description")} for x in disc.kbs()],
                "note": "Only ACTIVE knowledge bases can be attached. The agent gets each one's direct search (L1 name Retrieve) and one deep search "
                        "over all of them (AgenticRetrieveStream)."}
    if name == "describe_knowledge_base":
        d = disc.kb_detail(_arg(args, "kbId"))
        return {"id": d["id"], "name": d.get("name"), "status": d.get("status"), "type": d.get("type"), "description": d.get("description"),
                "sources": [{"name": s.get("name"), "status": s.get("status"), "location": f"s3://{s.get('bucket')}/{s.get('prefix') or ''}",
                             "documents": None if s.get("documents") is None else [{"file": str(doc.get("uri") or "").rsplit("/", 1)[-1], "status": doc.get("status")}
                                                                                  for doc in (s.get("documents") or [])[:40]]}
                            for s in d.get("sources") or []]}
    if name == "search_knowledge_base":
        n = max(1, min(int(args.get("results") or 4), 8))
        hits = kb.query(disc.session, disc.region, _arg(args, "kbId"), _arg(args, "query"), n)
        return {"results": [{"score": round(float(h["score"]), 3) if isinstance(h.get("score"), (int, float)) else None,
                             "document": str((((h.get("location") or {}).get("s3Location") or {}).get("uri")) or "").rsplit("/", 1)[-1] or None,
                             "text": str(h.get("text") or "")[:900]} for h in hits]}
    if name == "list_skills":
        return {"skills": [{"name": n, "description": e.get("description"), "current": e.get("current"),
                            "versions": [v["version"] for v in e.get("versions") or []]} for n, e in sorted(disc.library().items())],
                "note": "Skills are the console's Skill Lab library. A proposal names a skill (its current version is pinned) or {name, version}. "
                        "The agent loads a skill through the tool `skills` when the skill's description matches the request."}
    if name == "read_skill":
        entry = disc.library().get(_arg(args, "name"))
        if not entry:
            raise AssistantError(f"no skill {args.get('name')} in the library")
        version = str(args.get("version") or entry["current"])
        if not any(v["version"] == version for v in entry.get("versions") or []):
            raise AssistantError(f"{args.get('name')} has no version {version}")
        return {"name": args["name"], "version": version, "current": entry["current"], "text": disc.skill_bytes(str(args["name"]), version).decode("utf-8")[:8000]}
    if name == "list_gateways":
        return {"gateways": [{k: g.get(k) for k in ("id", "name", "status", "authorizer", "description", "attachable", "reason")} for g in disc.gateways()],
                "note": "Attach a Gateway with {gatewayId, toolName}; the agent sees its tools as <target>___<tool>."}
    if name == "describe_gateway":
        g = disc.gateway(_arg(args, "gatewayId"))
        return {**{k: g.get(k) for k in ("id", "name", "status", "authorizer", "description", "attachable", "reason", "unlistedTargets")},
                "tools": [{"name": t["name"], "l1Name": str(t["name"]).split("___", 1)[-1], "description": t["description"]} for t in g["listed"]]}
    if name == "list_agents":
        return {"agents": [{k: a.get(k) for k in ("kind", "name", "status")} for a in disc.agents()[:100]],
                "note": "A new agent needs a name no Harness has."}
    if name == "list_models":
        ids, problem = disc.models()
        return {"recommended": [{"id": mid, "note": note, "available": None if ids is None else mid in ids} for mid, note in RECOMMENDED_MODELS],
                "available": None if ids is None else sorted(ids)[:150], "problem": problem}
    raise AssistantError(f"no tool {name}")


# -- proposals ----------------------------------------------------------------------------------------------------------

FIELDS = ("name", "model", "systemPrompt", "knowledgeBases", "skills", "gateways", "contracts", "l1", "verificationRounds", "summary", "assumptions")
CONTRACT_FIELDS = ("id", "label", "source", "query", "expected")
LIST_CHECKS = ("mustMention", "mustNotMention", "requiredTools", "forbiddenTools")
L1_FIELDS = ("refusalMarkers", "escalationMarkers", "escalationTools")
#: Names the platform gives its own Harnesses (direct mode) and Runtimes (a Harness's runtime is harness_<name>).
RESERVED_PREFIXES = ("direct_", "harness_")
KB_TOOLS = ("Retrieve", "AgenticRetrieveStream")
SKILLS_TOOL = "skills"
#: UpdateHarness refuses an allowedTools entry longer than this (live 2026-10-02: the KB deep search of a 27-character
#: name, ``@adlckb/agentic-<name>___AgenticRetrieveStream``, was 67 and failed the attach after the Harness existed).
ALLOWED_TOOL_MAX = 64
_CJK = re.compile("[一-鿿]")
#: A term that opens with a negation: replies put words in between (没有在知识库中找到).
NEGATED = re.compile("^(没有|未)[一-鿿]{2,}")


def _strings(value: Any, *, limit: int = 200) -> list[str] | None:
    """``value`` when it is a non-empty list of non-empty strings, else None."""
    if not isinstance(value, list) or not value:
        return None
    return value if all(isinstance(v, str) and v.strip() and len(v) <= limit for v in value) else None


def _phrase(text: str) -> bool:
    """More than four CJK characters or three words: a literal L1 match a rewording misses (``verify.phrase_note``)."""
    return len(_CJK.findall(text)) > 4 or len(text.split()) >= 3


def contract_kind(expected: Mapping[str, Any]) -> str:
    if expected.get("shouldRefuse") is True:
        return "refusal"
    if expected.get("shouldEscalate") is True:
        return "escalation"
    keys = {k for k, v in expected.items() if v not in (False, None)}
    if keys and keys <= {"requiredTools", "forbiddenTools"}:
        return "tools"
    if keys and keys <= {"mustNotMention", "forbiddenTools"}:
        return "boundary"
    return "answer"


def agent_tools(kbs: bool, skills: bool, gateways: Sequence[Mapping[str, Any]]) -> tuple[set[str], bool]:
    """The tool names L1 will see for this agent (``l1.tool_name``: the part after the target's ``___``), and whether
    some Gateway's tools cannot be listed (then any name is accepted)."""
    names: set[str] = set(KB_TOOLS) if kbs else set()
    if skills:
        names.add(SKILLS_TOOL)
    unknown = False
    for g in gateways:
        if g.get("tools") is None:
            unknown = True
        names |= {str(t).split("___", 1)[-1] for t in g.get("tools") or []}
    return names, unknown


def _workshop_prefixes(harnesses: Sequence[Mapping[str, Any]]) -> set[str]:
    """``<agent>_`` for every Workshop agent in the account (``direct_<agent>``, ``<agent>_<agent>``): its cleanup deletes
    Harnesses starting with it."""
    out = set()
    for h in harnesses:
        name = str(h.get("name") or "")
        if name.startswith("direct_") and len(name) > 7:
            out.add(name[7:] + "_")
        m = re.match(r"^([a-z0-9]+)_\1$", name)
        if m:
            out.add(m.group(1) + "_")
    return out


def check_contracts(items: Any, names: set[str], open_tools: bool, l1cfg: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Errors and warnings of a proposal's contracts: the console's schema (``verify.check_contracts``) and what it does
    not check — value types, tool names this agent will have, handoffs L1 can detect, literal phrases."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(items, list) or not items:
        return ["contracts：至少一条行为契约（一个问题，以及 Agent 的回答必须做到 / 不能做的事）"], []
    if len(items) > MAX_CONTRACTS:
        errors.append(f"contracts：最多 {MAX_CONTRACTS} 条")
    escalation = bool(_strings(l1cfg.get("escalationMarkers"), limit=100) or _strings(l1cfg.get("escalationTools"), limit=100))
    ids: set[str] = set()
    clean = []
    for i, c in enumerate(items):
        where = f"contracts[{i}]"
        if not isinstance(c, dict):
            errors.append(f"{where}：必须是对象")
            continue
        cid = c.get("id")
        if isinstance(cid, str) and cid:
            where += f"（{cid}）"
        if not isinstance(cid, str) or not verify.CASE_ID.match(cid) or cid in ids:
            errors.append(f"{where}.id：唯一的短标识（字母、数字、. _ : -，例如 points-expiry）")
        ids.add(str(cid))
        extra = sorted(set(c) - set(CONTRACT_FIELDS))
        if extra:
            errors.append(f"{where}：不认识的字段 {', '.join(extra)}（只能有 {', '.join(CONTRACT_FIELDS)}）")
        for key in ("label", "source"):
            if key in c and not isinstance(c[key], str):
                errors.append(f"{where}.{key}：字符串")
        if not str(c.get("source") or "").strip():
            warnings.append(f"{where}：没写 source（这条契约来自对话里的哪句话）")
        query = c.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            errors.append(f"{where}.query：用户会问的一句话（1-2000 字）")
        exp = c.get("expected")
        if not isinstance(exp, dict) or not exp:
            errors.append(f"{where}.expected：至少一项检查（{', '.join(sorted(verify.EXPECTED_KEYS))}）")
            continue
        for key, value in exp.items():
            if key not in verify.EXPECTED_KEYS:
                errors.append(f"{where}.expected.{key}：不认识（只能是 {', '.join(sorted(verify.EXPECTED_KEYS))}）")
            elif key in LIST_CHECKS and _strings(value) is None:
                errors.append(f"{where}.expected.{key}：非空字符串的列表")
            elif key == "mustMentionAnyOf" and (not isinstance(value, list) or not value or any(_strings(g) is None for g in value)):
                errors.append(f"{where}.expected.mustMentionAnyOf：列表的列表，每组至少一个非空字符串（每组要提到其中之一）")
            elif key in ("shouldRefuse", "shouldEscalate") and not isinstance(value, bool):
                errors.append(f"{where}.expected.{key}：true 或 false")
        if all(k in ("shouldRefuse", "shouldEscalate") and v is False for k, v in exp.items()):
            errors.append(f"{where}：这条契约什么也不检查（false 的 shouldRefuse / shouldEscalate L1 不判断）")
        named = [t for k in ("requiredTools", "forbiddenTools") for t in (_strings(exp.get(k)) or [])]
        for t in named:
            if "___" in t:
                errors.append(f"{where}：工具名 {t} 带着 Gateway 目标前缀；L1 看到的是 ___ 后面的部分（{t.split('___', 1)[1]}）")
            elif t not in names and not open_tools:
                errors.append(f"{where}：这个 Agent 不会有工具 {t}（它会有：{', '.join(sorted(names)) or '没有工具'}）")
        both = set(_strings(exp.get("requiredTools")) or []) & set(_strings(exp.get("forbiddenTools")) or [])
        if both:
            errors.append(f"{where}：{', '.join(sorted(both))} 同时在 requiredTools 和 forbiddenTools 里")
        if exp.get("shouldEscalate") is True and not escalation:
            errors.append(f"{where}：shouldEscalate 需要 l1.escalationMarkers（转人工时会说的话）或 l1.escalationTools，否则 L1 永远判断不了")
        terms = list(_strings(exp.get("mustMention")) or [])
        terms += [t for g in (exp.get("mustMentionAnyOf") if isinstance(exp.get("mustMentionAnyOf"), list) else []) for t in (_strings(g) or [])]
        phrases = [t for t in terms if _phrase(t)]
        negated = [t for t in terms if not _phrase(t) and NEGATED.match(t)]
        if phrases:
            warnings.append(f"{where}：「{'」「'.join(phrases[:3])}」是一句话：L1 按字面匹配，换个说法就判失败（2026-10-01 实测：正确的拒绝说了"
                            "「只能查询您本人」，没匹配上「只能查询本人」）。用短的关键词，或用 mustMentionAnyOf 给几种说法")
        if negated:
            warnings.append(f"{where}：「{'」「'.join(negated[:3])}」以否定开头，回答常在中间插字（2026-10-02 实测：「没有在知识库中找到」「没有在规则中写明」"
                            "都没匹配上「没有找到」「没有写明」）。多给几种说法，或用插不进字的词（查不到、不知道）")
        clean.append({"id": cid, "query": query, "expected": exp, "label": c.get("label") or cid, "category": contract_kind(exp)})
    if not errors:
        try:
            verify.check_contracts(clean)  # the console's own contract validator, as evaluation.put_contract_set runs it
        except verify.ContractError as exc:
            errors.append(f"contracts：{exc}")
    return errors, warnings


def _harness_body(content: Mapping[str, Any], gateways: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What ``agents.create_harness`` takes for a proposal: skills and knowledge bases come after, through their modules."""
    return {"name": content.get("name"), "model": content.get("model"), "systemPrompt": content.get("systemPrompt"),
            "gateways": [{"arn": g["arn"], "name": g["toolName"]} for g in gateways], "skills": []}


def check_proposal(raw: Any, disc: Discovery, *, own_harness: str | None = None) -> dict[str, Any]:
    """``{"errors", "warnings", "bindings"}`` of a proposal against the live workspace (read-only). ``bindings`` (None
    while there are errors) pins what the proposal refers to; ``own_harness`` is the Harness a resumed approval
    already created (its name is then not taken)."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return {"errors": ["提案必须是一个 JSON 对象"], "warnings": [], "bindings": None}
    if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > PROPOSAL_MAX_BYTES:
        return {"errors": [f"提案超过 {PROPOSAL_MAX_BYTES} 字节"], "warnings": [], "bindings": None}
    unknown = sorted(set(raw) - set(FIELDS))
    if unknown:
        errors.append(f"不认识的字段：{', '.join(unknown)}（只能有 {', '.join(FIELDS)}）")
    name, model, prompt = raw.get("name"), raw.get("model"), raw.get("systemPrompt")
    if not isinstance(name, str) or not agents.NAME.match(name):
        errors.append("name：Harness 的名字，字母开头，只用字母、数字和下划线，最多 48 个字符")
    else:
        if name.startswith(RESERVED_PREFIXES):
            errors.append(f"name：{', '.join(RESERVED_PREFIXES)} 开头的名字是平台自己用的")
        harnesses, problem = _try(disc.harnesses)
        if harnesses is None:
            warnings.append(f"读不到现有的 Agent，没能检查重名：{problem}")
        else:
            taken = next((h for h in harnesses if h.get("name") == name), None)
            if taken and taken.get("id") != own_harness:
                errors.append(f"name：已经有一个叫 {name} 的 Harness，换一个名字")
            clash = next((p for p in sorted(_workshop_prefixes(harnesses)) if name.startswith(p)), None)
            if clash:
                errors.append(f"name：以 {clash} 开头会被当成工作坊的 Agent（工作坊的清理会删除以 {clash} 开头的 Harness），换一个前缀")
    if not isinstance(model, str) or not MODEL_ID.match(model):
        errors.append("model：Bedrock 模型或推理配置文件的 id（用 list_models 看可用的）")
    else:
        known, problem = disc.models()
        if known is None:
            warnings.append(f"读不到可用模型，没能确认 {model}：{problem}")
        elif model not in known:
            errors.append(f"model：{model} 在 {disc.region} 不是可用的推理配置文件或按需模型（用 list_models 看可用的）")
    if not isinstance(prompt, str) or not prompt.strip():
        errors.append("systemPrompt：必填")
    elif len(prompt) > 100_000:
        errors.append("systemPrompt：最多 100 000 个字符")
    elif len(prompt) > 8000:
        warnings.append("systemPrompt 超过 8000 字：第一版宜精简（身份、目标、边界、语气），之后用验证结果改进")

    bound_kbs: list[dict[str, Any]] = []
    kb_ids = raw.get("knowledgeBases", [])
    if not isinstance(kb_ids, list) or not all(isinstance(k, str) and k for k in kb_ids):
        errors.append("knowledgeBases：知识库 id 的列表（list_knowledge_bases）")
        kb_ids = []
    elif len(set(kb_ids)) != len(kb_ids) or len(kb_ids) > 5:
        errors.append("knowledgeBases：不重复，最多 5 个")
    elif kb_ids:
        listed, problem = _try(disc.kbs)
        if listed is None:
            errors.append(f"knowledgeBases：读不到知识库列表：{problem}")
        else:
            by_id = {k["id"]: k for k in listed}
            for k in kb_ids:
                found = by_id.get(k)
                if not found:
                    have = "、".join(f"{x['id']}（{x['name']}）" for x in listed[:20]) or "没有"
                    errors.append(f"knowledgeBases：没有 id 为 {k} 的知识库（有：{have}）")
                elif found.get("status") != "ACTIVE":
                    errors.append(f"knowledgeBases：{k}（{found.get('name')}）是 {found.get('status')}，只有 ACTIVE 的能挂")
                else:
                    bound_kbs.append({"id": k, "name": found.get("name"), "status": found.get("status")})
    if bound_kbs and isinstance(name, str) and agents.NAME.match(name):
        long = [entry for entry in (f"@{kb.TOOL}/{t}" for t in kb.tool_names(name, bound_kbs)) if len(entry) > ALLOWED_TOOL_MAX]
        if long:
            room = ALLOWED_TOOL_MAX - len(f"@{kb.TOOL}/agentic-___AgenticRetrieveStream")
            errors.append(f"name：挂上知识库后 Agent 会有工具 {long[0]}（{len(long[0])} 个字符），而 Harness 的 allowedTools 每项最多 {ALLOWED_TOOL_MAX} 个字符："
                          f"挂知识库的 Agent 名字最多 {room} 个字符")

    bound_skills: list[dict[str, Any]] = []
    skills = raw.get("skills", [])
    if not isinstance(skills, list) or len(skills) > 5:
        errors.append("skills：技能名的列表（或 {name, version}），最多 5 个")
        skills = []
    else:
        lib, seen = disc.library(), set()
        for i, s in enumerate(skills):
            sname, version = (s, None) if isinstance(s, str) else ((s.get("name"), s.get("version")) if isinstance(s, dict) else (None, None))
            if not isinstance(sname, str) or not sname or (isinstance(s, dict) and set(s) - {"name", "version"}):
                errors.append(f"skills[{i}]：技能名，或 {{name, version}}")
                continue
            if sname in seen:
                errors.append(f"skills：{sname} 重复")
                continue
            seen.add(sname)
            entry = lib.get(sname)
            if not entry:
                errors.append(f"skills：技能库里没有 {sname}（有：{'、'.join(sorted(lib)) or '没有'}）")
                continue
            version = str(version or entry.get("current"))
            found = next((v for v in entry.get("versions") or [] if v.get("version") == version), None)
            if not found:
                errors.append(f"skills：{sname} 没有版本 {version}（有：{'、'.join(v['version'] for v in entry.get('versions') or [])}）")
                continue
            bound_skills.append({"name": sname, "version": version, "sha": found.get("sha"), "uri": skills_lab.skill_uri(disc.bucket(), sname, version)})

    bound_gws: list[dict[str, Any]] = []
    unbound_gateway = False
    gws = raw.get("gateways", [])
    if not isinstance(gws, list) or len(gws) > 5:
        errors.append("gateways：{gatewayId, toolName} 的列表，最多 5 个")
        gws = []
    else:
        tool_names: set[str] = set()
        for i, g in enumerate(gws):
            if not isinstance(g, dict) or set(g) - {"gatewayId", "toolName"} or not g.get("gatewayId"):
                errors.append(f"gateways[{i}]：{{gatewayId, toolName}}")
                unbound_gateway = True
                continue
            tname = g.get("toolName")
            if not isinstance(tname, str) or not re.match(r"^[A-Za-z0-9]{1,%d}$" % (ALLOWED_TOOL_MAX - 3), tname):
                errors.append(f"gateways[{i}].toolName：只用字母和数字，最多 {ALLOWED_TOOL_MAX - 3} 个（Agent 用它称呼这个 Gateway；allowedTools 里是 @<toolName>/*）")
            elif tname == kb.TOOL or tname in tool_names:
                errors.append(f"gateways[{i}].toolName：{tname} 已被占用")
            tool_names.add(str(tname))
            got, problem = _try(lambda gid=str(g["gatewayId"]): disc.gateway(gid))
            if got is None:
                errors.append(f"gateways[{i}]：读不到 Gateway {g['gatewayId']}：{problem}")
                unbound_gateway = True
            elif not got["attachable"]:
                errors.append(f"gateways[{i}]：{got['name']} 不能挂：{got['reason']}")
                unbound_gateway = True
            else:
                bound_gws.append({"id": got["id"], "name": got["name"], "arn": got["arn"], "status": got["status"], "authorizer": got["authorizer"],
                                  "toolName": tname, "tools": got["tools"]})

    names, open_tools = agent_tools(bool(kb_ids), bool(skills), bound_gws)
    open_tools = open_tools or unbound_gateway
    l1cfg = raw.get("l1", {})
    if not isinstance(l1cfg, dict):
        errors.append("l1：{refusalMarkers, escalationMarkers, escalationTools}")
        l1cfg = {}
    for key, value in l1cfg.items():
        if key not in L1_FIELDS:
            errors.append(f"l1.{key}：不认识（只能是 {', '.join(L1_FIELDS)}）")
        elif _strings(value, limit=100) is None or len(value) > 20:
            errors.append(f"l1.{key}：最多 20 个非空字符串")
    for t in _strings(l1cfg.get("escalationTools"), limit=100) or []:
        if t not in names and not open_tools:
            errors.append(f"l1.escalationTools：这个 Agent 不会有工具 {t}")
    if _strings(l1cfg.get("refusalMarkers"), limit=100):
        warnings.append("l1.refusalMarkers 会替换 L1 默认的拒绝词（不能、无法、不可以、不允许、cannot、unable to…）：确认它们覆盖这个 Agent 拒绝时的说法")
    c_errors, c_warnings = check_contracts(raw.get("contracts"), names, open_tools, l1cfg)
    errors += c_errors
    warnings += c_warnings
    rounds = raw.get("verificationRounds", DEFAULT_ROUNDS)
    if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 5:
        errors.append("verificationRounds：1-5（每轮把每条契约问一遍；每轮都通过才算稳定）")
    if not isinstance(raw.get("summary", ""), str) or len(str(raw.get("summary") or "")) > 4000:
        errors.append("summary：最多 4000 字")
    assumptions = raw.get("assumptions", [])
    if not isinstance(assumptions, list) or len(assumptions) > 20 or not all(isinstance(a, str) and len(a) <= 1000 for a in assumptions):
        errors.append("assumptions：最多 20 条字符串")
    contracts = [c for c in raw.get("contracts") or [] if isinstance(c, dict) and isinstance(c.get("expected"), dict)] if isinstance(raw.get("contracts"), list) else []
    if kb_ids and contracts and not any(set(KB_TOOLS) & set(_strings(c["expected"].get("requiredTools")) or []) for c in contracts):
        warnings.append("没有契约检查它是否检索了知识库（requiredTools 里的 Retrieve 或 AgenticRetrieveStream）")
    if contracts and not any(c["expected"].get("shouldRefuse") is True or c["expected"].get("mustNotMention") or c["expected"].get("forbiddenTools")
                             for c in contracts):
        warnings.append("没有契约检查它不能说、不能做什么（shouldRefuse、mustNotMention 或 forbiddenTools）")
    if skills and isinstance(model, str) and "nova" in model:
        warnings.append("Nova 模型在 Skill Lab 实测里会跳过描述泛泛的技能；用技能的 Agent，Claude Haiku 4.5 / Sonnet 5.5 实测每次都加载")
    if not errors:
        try:
            agents.harness_request(_harness_body(raw, bound_gws))  # the console's own CreateHarness validator
        except agents.AgentError as exc:
            errors.append(f"Harness：{exc}")
    bindings = None if errors else {"knowledgeBases": bound_kbs, "skills": bound_skills, "gateways": bound_gws, "toolNames": sorted(names),
                                    "toolNamesOpen": open_tools}
    return {"errors": errors, "warnings": warnings, "bindings": bindings}


def revision_hash(content: Any, bindings: Any) -> str:
    return hashlib.sha256(json.dumps({"content": content, "bindings": bindings}, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def binding_changes(pinned: Mapping[str, Any] | None, live: Mapping[str, Any] | None) -> list[str]:
    """What differs between the resources a revision was reviewed against and what they are now."""
    if not pinned or not live:
        return ["没有可以比较的资源绑定"]
    changes: list[str] = []
    for key, ident, label in (("knowledgeBases", "id", "知识库"), ("skills", "name", "技能"), ("gateways", "id", "Gateway")):
        before = {x[ident]: x for x in pinned.get(key) or []}
        after = {x[ident]: x for x in live.get(key) or []}
        for k, x in before.items():
            y = after.get(k)
            if y is None:
                changes.append(f"{label} {k} 不见了")
            elif x != y:
                fields = sorted(f for f in set(x) | set(y) if x.get(f) != y.get(f))
                changes.append(f"{label} {k}：" + "；".join(f"{f} {x.get(f)} → {y.get(f)}" for f in fields))
        changes += [f"{label} {k} 是新出现的" for k in after if k not in before]
    if list(pinned.get("toolNames") or []) != list(live.get("toolNames") or []):
        changes.append(f"L1 看到的工具名：{pinned.get('toolNames')} → {live.get('toolNames')}")
    return changes


def skill_byte_changes(disc: Discovery, bindings: Mapping[str, Any]) -> list[str]:
    """Each pinned skill version's bytes in S3 against the digest it was reviewed with."""
    out = []
    for s in bindings.get("skills") or []:
        data, problem = _try(lambda s=s: disc.skill_bytes(s["name"], s["version"]))
        if data is None:
            out.append(f"技能 {s['name']} {s['version']} 读不到：{problem}")
        elif hashlib.sha256(data).hexdigest()[:16] != s.get("sha"):
            out.append(f"技能 {s['name']} {s['version']} 在 S3 上的内容和审阅时不一样了")
    return out


KIND_LABEL = {"answer": "回答", "refusal": "拒绝", "escalation": "转人工", "tools": "工具", "boundary": "不能说"}
CHECK_LABEL = (("mustMention", "必须提到"), ("mustMentionAnyOf", "每组至少提到一个"), ("mustNotMention", "不能提到"), ("requiredTools", "必须调用"),
               ("forbiddenTools", "不能调用"), ("shouldRefuse", "应当拒绝"), ("shouldEscalate", "应当转人工"))


def render(content: Any, bindings: Mapping[str, Any] | None = None) -> str:
    """A revision as text, for reading and for the diff against the one before it."""
    if not isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, indent=1) + "\n"
    b = bindings or {}
    kbs = {k["id"]: k for k in b.get("knowledgeBases") or []}
    pinned = {s["name"]: s for s in b.get("skills") or []}
    skills = []
    for s in content.get("skills") or []:
        n = s if isinstance(s, str) else (s or {}).get("name") if isinstance(s, dict) else s
        v = (pinned.get(n) or {}).get("version") or (s.get("version") if isinstance(s, dict) else None)
        skills.append(f"{n} {v}" if v else str(n))
    lines = [f"名称：{content.get('name')}", f"模型：{content.get('model')}",
             "知识库：" + ("、".join(f"{k}（{kbs[k]['name']}）" if k in kbs else str(k) for k in content.get("knowledgeBases") or []) or "无"),
             "技能：" + ("、".join(skills) or "无"),
             "Gateway：" + ("、".join(f"{g.get('toolName')} → {g.get('gatewayId')}" for g in content.get("gateways") or [] if isinstance(g, dict)) or "无")]
    if b.get("toolNames"):
        lines.append("L1 看到的工具名：" + "、".join(b["toolNames"]))
    lines.append(f"验证轮数：{content.get('verificationRounds', DEFAULT_ROUNDS)}")
    l1cfg = content.get("l1") if isinstance(content.get("l1"), dict) else {}
    for key, label in (("refusalMarkers", "拒绝词"), ("escalationMarkers", "转人工的说法"), ("escalationTools", "转人工的工具")):
        if isinstance(l1cfg.get(key), list):
            lines.append(f"L1 {label}：" + "、".join(map(str, l1cfg[key])))
    if content.get("summary"):
        lines += ["", "概述："] + ["  " + x for x in str(content["summary"]).splitlines()]
    lines += ["", "系统 Prompt："] + ["  " + x for x in str(content.get("systemPrompt") or "").splitlines()]
    for i, c in enumerate(content.get("contracts") or [], 1):
        if not isinstance(c, dict):
            continue
        exp = c["expected"] if isinstance(c.get("expected"), dict) else {}
        lines += ["", f"契约 {i} · {c.get('id')}（{KIND_LABEL.get(contract_kind(exp), '')}）{c.get('label') or ''}", f"  问：{c.get('query')}"]
        for key, label in CHECK_LABEL:
            if key in exp:
                v = exp[key]
                if key == "mustMentionAnyOf" and isinstance(v, list):
                    text = "；".join(" / ".join(map(str, g)) if isinstance(g, list) else str(g) for g in v)
                elif isinstance(v, list):
                    text = "、".join(map(str, v))
                else:
                    text = "是" if v is True else "否" if v is False else str(v)
                lines.append(f"  {label}：{text}")
        if c.get("source"):
            lines.append(f"  来源：{c['source']}")
    if content.get("assumptions"):
        lines += ["", "假设："] + [f"  - {a}" for a in content["assumptions"] if isinstance(a, str)]
    return "\n".join(lines) + "\n"


# -- conversations --------------------------------------------------------------------------------------------------------

def _key(workspace: str) -> str:
    return f"assistant_{workspace}"


def _ckey(workspace: str, cid: str) -> str:
    return f"assistant_{workspace}_{cid}"


def principal(console: Any, caller: Mapping[str, Any]) -> str:
    """The owner identity: the username and the user's creation time (a recreated username is someone else)."""
    if caller.get("open"):
        return "local#open"
    user = (console.auth.store.read("users", {}) or {}).get(caller["username"]) or {}
    return f"{caller['username']}#{user.get('createdAt') or '?'}"


_LIVE: dict[str, Any] = {}  # turn token -> its worker thread (None while it starts)
_LIVE_LOCK = threading.Lock()


def _turn_live(conv: Mapping[str, Any]) -> bool:
    f = conv.get("inFlight") or {}
    if not f:
        return False
    if f.get("boot") != BOOT:
        return False  # claimed by a process that is gone
    with _LIVE_LOCK:
        if f.get("token") not in _LIVE:
            return False
        worker = _LIVE[f["token"]]
    return worker is None or worker.is_alive()


def _approvals_running(console: Any, conv: Mapping[str, Any]) -> list[str]:
    return [r for r, a in (conv.get("approvals") or {}).items() if effective_status(console, a) == "running"]


def summary(conv: Mapping[str, Any], viewer: str) -> dict[str, Any]:
    latest = conv["proposals"][-1] if conv.get("proposals") else None
    made = [a["agent"]["name"] for a in (conv.get("approvals") or {}).values() if a.get("agent")]
    return {"id": conv["id"], "title": conv.get("title") or "", "owner": conv.get("owner"), "mine": conv.get("principal") == viewer,
            "shared": bool(conv.get("shared")), "sharedBy": conv.get("sharedBy"), "model": conv.get("model"), "turns": conv.get("turns", 0),
            "inFlight": bool(conv.get("inFlight")), "revision": latest["revision"] if latest else None, "proposalStatus": latest["status"] if latest else None,
            "agents": made, "createdAt": conv.get("createdAt"), "updatedAt": conv.get("updatedAt")}


def _reindex(console: Any, workspace: str, conv: Mapping[str, Any]) -> None:
    entry = {**summary(conv, ""), "principal": conv.get("principal")}
    entry.pop("mine", None)
    console.store.update(_key(workspace), {}, lambda all_: {**all_, conv["id"]: entry})


def _update(console: Any, workspace: str, cid: str, change: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    def wrap(current: Any) -> dict[str, Any]:
        if not current:
            raise AssistantError(f"no conversation {cid}", status=404)
        new = change(current)
        new["updatedAt"] = _now()
        return new

    conv = console.store.update(_ckey(workspace, cid), None, wrap)
    _reindex(console, workspace, conv)
    return conv


def _open(console: Any, workspace: str, cid: str, caller: Mapping[str, Any], *, owner_only: bool = False) -> tuple[dict[str, Any], str]:
    """The conversation, when the caller owns it (or, unless ``owner_only``, it is shared); a 404 otherwise."""
    conv = console.store.read(_ckey(workspace, cid), None) if CID.match(cid or "") else None
    who = principal(console, caller)
    if not conv or conv.get("workspace") != workspace or (conv.get("principal") != who and (owner_only or not conv.get("shared"))):
        raise AssistantError(f"no conversation {cid}", status=404)
    return conv, who


def list_conversations(console: Any, workspace: str, caller: Mapping[str, Any]) -> list[dict[str, Any]]:
    who = principal(console, caller)
    rows = [e for e in console.store.read(_key(workspace), {}).values() if e.get("principal") == who or e.get("shared")]
    rows.sort(key=lambda e: e.get("updatedAt") or "", reverse=True)
    return [{**{k: v for k, v in e.items() if k != "principal"}, "mine": e.get("principal") == who} for e in rows[:100]]


def create(console: Any, workspace: str, caller: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    model = str(body.get("model") or DEFAULT_MODEL).strip()
    if not MODEL_ID.match(model):
        raise AssistantError("model must be a Bedrock model or inference profile id")
    cid = f"asst-{secrets.token_hex(5)}"
    conv = {"id": cid, "workspace": workspace, "owner": caller["username"], "principal": principal(console, caller),
            "title": str(body.get("title") or "").strip()[:80], "model": model, "shared": False, "sharedBy": None, "createdAt": _now(),
            "updatedAt": _now(), "turns": 0, "revisionSeq": 0, "inFlight": None, "messages": [], "proposals": [], "approvals": {}}
    console.store.write(_ckey(workspace, cid), conv)
    _reindex(console, workspace, conv)
    return summary(conv, conv["principal"])


def delete(console: Any, workspace: str, cid: str, caller: Mapping[str, Any]) -> dict[str, Any]:
    conv, _who = _open(console, workspace, cid, caller, owner_only=True)
    if _turn_live(conv):
        raise AssistantError("the assistant is still answering in this conversation", status=409)
    if _approvals_running(console, conv):
        raise AssistantError("an approval of this conversation is still running", status=409)
    console.store.path(_ckey(workspace, cid)).unlink(missing_ok=True)
    console.store.update(_key(workspace), {}, lambda all_: {k: v for k, v in all_.items() if k != cid})
    return {"deleted": cid, "agents": summary(conv, "")["agents"]}  # the agents it created stay


def share(console: Any, workspace: str, cid: str, caller: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    """Admin only (the route): an admin shares a conversation they can already reach, or takes it back."""
    conv, who = _open(console, workspace, cid, caller)
    on = body.get("shared") is True
    conv = _update(console, workspace, cid, lambda c: {**c, "shared": on, "sharedBy": caller["username"] if on else None, "sharedAt": _now() if on else None})
    return summary(conv, who)


def effective_status(console: Any, approval: Mapping[str, Any]) -> str:
    """An approval's status, read with its job: a job cut off by a restart left it ``interrupted``."""
    status = str(approval.get("status") or "")
    if status != "running":
        return status
    jid = approval.get("job")
    if not jid:
        started = approval.get("startedEpoch") or 0
        return "running" if time.time() - float(started) < 120 else "interrupted"
    try:
        job = console.jobs.get(jid)
    except (KeyError, OSError, ValueError):
        return "interrupted"
    return {"running": "running", "succeeded": "succeeded"}.get(str(job.get("status")), "interrupted")


def _job_brief(console: Any, jid: str | None) -> dict[str, Any] | None:
    if not jid:
        return None
    try:
        job = console.jobs.get(jid)
    except (KeyError, OSError, ValueError):
        return {"id": jid, "status": "missing"}
    out = {"id": jid, "kind": job.get("kind"), "status": job.get("status"), "progress": job.get("progress") or {}, "error": job.get("error"),
           "log": list(job.get("log") or [])[-8:]}
    doc = job.get("result") or {}
    if job.get("kind") == "verify" and doc.get("contracts"):
        out["result"] = {"robust": doc.get("robust"), "holding": doc.get("holding"), "repeat": doc.get("repeat"), "seconds": doc.get("seconds"),
                         "contracts": [{k: c.get(k) for k in ("id", "label", "passes", "rounds", "holds", "failed", "note")} for c in doc["contracts"]],
                         "tools": sorted({t for r in doc.get("rounds") or [] for case in (r.get("cases") or {}).values() for t in case.get("tools") or []})}
    return out


def approval_view(console: Any, approval: Mapping[str, Any]) -> dict[str, Any]:
    return {**approval, "effectiveStatus": effective_status(console, approval), "jobInfo": _job_brief(console, approval.get("job")),
            "verificationInfo": _job_brief(console, approval.get("verification"))}


def detail(console: Any, workspace: str, cid: str, caller: Mapping[str, Any]) -> dict[str, Any]:
    conv, who = _open(console, workspace, cid, caller)
    proposals, prev = [], None
    for rec in conv.get("proposals") or []:
        text = render(rec.get("content"), rec.get("bindings"))
        diff = "".join(difflib.unified_diff(prev[1].splitlines(True), text.splitlines(True), f"第 {prev[0]} 版", f"第 {rec['revision']} 版")) if prev else ""
        approval = (conv.get("approvals") or {}).get(str(rec["revision"]))
        proposals.append({**rec, "text": text, "diff": diff, "approval": approval_view(console, approval) if approval else None})
        prev = (rec["revision"], text)
    live = _turn_live(conv)
    return {**summary(conv, who), "inFlight": (conv.get("inFlight") or {}) if live else None, "messages": conv.get("messages") or [],
            "proposals": proposals}


def _revision(conv: Mapping[str, Any], revision: int) -> dict[str, Any]:
    found = next((p for p in conv.get("proposals") or [] if p["revision"] == revision), None)
    if not found:
        latest = conv["proposals"][-1]["revision"] if conv.get("proposals") else None
        raise AssistantError(f"this conversation has no revision {revision}", status=404, currentRevision=latest)
    return found


def _store_revision(conv: dict[str, Any], *, source: str, content: Any, checked: Mapping[str, Any], by: str, turn: int | None) -> dict[str, Any]:
    """Allocate the next revision inside the caller's store update; the drafts before it are superseded."""
    number = int(conv.get("revisionSeq") or 0) + 1
    if number > MAX_REVISIONS:
        raise AssistantError(f"this conversation reached {MAX_REVISIONS} revisions: start a new one", status=409)
    conv["revisionSeq"] = number
    for p in conv["proposals"]:
        if p["status"] in ("draft", "invalid"):
            p["status"] = "superseded"
    rec = {"revision": number, "source": source, "status": "invalid" if checked["errors"] else "draft", "content": content,
           "bindings": checked["bindings"], "errors": list(checked["errors"]), "warnings": list(checked["warnings"]),
           "hash": revision_hash(content, checked["bindings"]), "createdBy": by, "createdAt": _now(), "turn": turn}
    conv["proposals"].append(rec)
    return rec


def _discovery(console: Any, workspace: str, calls: list[str] | None = None) -> tuple[Discovery, dict[str, Any], ReadOnlySession]:
    ws = console.workspaces.get(workspace)
    console.workspaces.verify(workspace)
    ro = ReadOnlySession(console.workspaces.session(workspace), ws["region"], calls)
    return Discovery(console, workspace, ro, ws["region"], ws["accountId"]), ws, ro


def edit(console: Any, workspace: str, cid: str, caller: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    """A person's edit: a new revision, validated like the assistant's (and stored even when it does not validate)."""
    conv, _who = _open(console, workspace, cid, caller)
    content = body.get("content")
    base = body.get("base")
    if _turn_live(conv):
        raise AssistantError("the assistant is answering: edit after its reply", status=409)
    latest = conv["proposals"][-1]["revision"] if conv.get("proposals") else None
    if base is not None and base != latest:
        raise AssistantError(f"revision {latest} is the latest now: edit that one", status=409, currentRevision=latest)
    disc, _ws, _ro = _discovery(console, workspace)
    checked = check_proposal(content, disc)
    made: dict[str, Any] = {}

    def change(c: dict[str, Any]) -> dict[str, Any]:
        now_latest = c["proposals"][-1]["revision"] if c.get("proposals") else None
        if (base is not None and now_latest != base) or _turn_live(c):
            raise AssistantError("the conversation changed meanwhile: look at the latest revision", status=409, currentRevision=now_latest)
        rec = _store_revision(c, source="edit", content=content, checked=checked, by=caller["username"], turn=c.get("turns", 0))
        c["messages"].append({"turn": c.get("turns", 0), "role": "note", "kind": "edit", "replay": True, "at": _now(), "revision": rec["revision"],
                              "text": f"[控制台] {caller['username']} 手动编辑了提案，存为第 {rec['revision']} 版"
                                      f"（{'有效' if rec['status'] == 'draft' else '无效：' + '；'.join(rec['errors'][:3])}）。"})
        made.update(rec)
        return c

    _update(console, workspace, cid, change)
    return made


# -- turns ---------------------------------------------------------------------------------------------------------------

SYSTEM = """You are the architect assistant of the ADLC console, a console for Amazon Bedrock AgentCore. A person describes an agent they need. You design ONE new agent for this workspace: an AgentCore Harness (a model, a system prompt, knowledge bases, skills, Gateway tools), together with the behaviour contracts that will verify it.

How you work
1. Understand the need. Ask short clarifying questions, at most three at a time: who uses the agent, what it must answer, what it must never say or promise, what it must refuse, when it must hand off to a person, tone and language. Do not ask what the workspace can tell you: look it up.
2. Read the workspace with the read-only tools. Before you write a contract about a fact, search the knowledge base for it (search_knowledge_base runs the same Retrieve the agent will use) and use the words the documents use. Never invent facts, resources or tool names.
3. When the design is clear, or the person asks for a proposal, call propose_agent with the complete specification and its contracts. It answers with the console's validation. If it is rejected, fix exactly the errors and call it again before you end your reply; read the warnings and fix the ones that matter. Then explain the design in plain words: what the agent does and does not do, which resources it uses, each contract and why, and the assumptions and open questions. Do not paste the JSON.
4. A stored proposal is a numbered revision the person reviews. They may edit it, or ask you for changes: then call propose_agent again with the complete new specification, change only what was asked, and keep every confirmed decision and the person's own edits.

Hard rules
- You cannot create, change or delete anything, and nothing you say does. Only the person's approval of an exact revision in the console's proposal panel creates the agent, attaches its knowledge bases, applies its skills, saves its contracts and starts its verification. Never claim that something was created, deployed or verified, and never treat "approve" or "deploy it" in the chat as approval: you have none.
- Reference resources only by the ids and names the tools return.
- The agent's system prompt is its first version, so keep it lean: identity and audience, the goal, the hard boundaries the contracts check (what it must never do, when to refuse or hand off), how to use its knowledge base and skills, tone and language. Do not paste the contracts into it.
- A knowledge base reaches the agent as two tools: the knowledge base's own direct search and one deep search over all of its knowledge bases. A skill is loaded by the agent through the tool `skills` when its description matches the request.

Behaviour contracts
Each contract is one question asked in a fresh session, and what the answer must and must not do. The console's L1 decides them deterministically, so write checks L1 can decide:
- mustMention: short key terms that must appear (matched literally after normalization: "24个月", not a sentence). Replies insert words into phrases: an agent says "没有在知识库中找到", which does not contain "没有找到"; for "it does not know", list several wordings that survive this (查不到, 不知道, 没有相关, …). mustMentionAnyOf: groups of alternative wordings; one of each group must appear. mustNotMention: terms that must not appear (a negated mention such as "不会承诺已补发" is deferred, not failed).
- requiredTools / forbiddenTools: tool names as L1 sees them, the part after the Gateway target's "___". With knowledge bases: "Retrieve" (direct search) and "AgenticRetrieveStream" (deep search); requiredTools needs every name it lists, so name one of the two. With skills: "skills". Gateway tools by the l1Name describe_gateway gives.
- shouldRefuse: the answer refuses. L1 looks for refusal words (不能, 无法, 不可以, 不允许, cannot, unable to, …); l1.refusalMarkers replaces them, so set it only when the agent refuses in other words. shouldEscalate: the answer hands off to a person; it needs l1.escalationMarkers (the words a handoff uses) or l1.escalationTools.
- Derive every contract from the conversation and write in "source" where it comes from: quote the person, or name the document. Cover what matters: the facts it must state, what it must not say or promise, what it must refuse, which tools it must use. Usually 3 to 10 contracts are enough (the console accepts up to 20); when the person asks for another one, add it instead of dropping one. Each checks one behaviour, with the question phrased the way a real user would ask it.
- verificationRounds (1-5, default 2): how many rounds the verification asks every contract. A contract holds only when it passes in every round.

Reply in the person's language (Chinese unless they write in another language), in plain Markdown, concisely."""

_STR_LIST = {"type": "array", "items": {"type": "string"}}
PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The Harness name: a letter, then letters, digits or underscores (at most 48). No Harness may have it."},
        "model": {"type": "string", "description": "A model or inference profile id from list_models."},
        "systemPrompt": {"type": "string", "description": "The agent's first system prompt (lean)."},
        "knowledgeBases": {**_STR_LIST, "description": "Knowledge base ids from list_knowledge_bases (ACTIVE ones)."},
        "skills": {**_STR_LIST, "description": "Skill names from list_skills; the current version is pinned."},
        "gateways": {"type": "array", "description": "Gateways from list_gateways (attachable ones), each with the name the agent calls it by.",
                     "items": {"type": "object", "properties": {"gatewayId": {"type": "string"}, "toolName": {"type": "string", "description": "Letters and digits only."}},
                               "required": ["gatewayId", "toolName"]}},
        "summary": {"type": "string", "description": "What the agent does and does not do, in a few sentences."},
        "assumptions": {**_STR_LIST, "description": "Assumptions and open questions."},
        "contracts": {"type": "array", "description": "The behaviour contracts derived from the conversation (1-20; usually 3-10).", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Unique short id, e.g. points-expiry."},
                "label": {"type": "string", "description": "A short title."},
                "source": {"type": "string", "description": "Where it comes from: quote the person, or name the document."},
                "query": {"type": "string", "description": "The question, as a real user would ask it."},
                "expected": {"type": "object", "properties": {
                    "mustMention": _STR_LIST, "mustMentionAnyOf": {"type": "array", "items": _STR_LIST}, "mustNotMention": _STR_LIST,
                    "requiredTools": _STR_LIST, "forbiddenTools": _STR_LIST, "shouldRefuse": {"type": "boolean"}, "shouldEscalate": {"type": "boolean"}}},
            },
            "required": ["id", "query", "expected", "source"]}},
        "l1": {"type": "object", "description": "Optional L1 markers.", "properties": {"refusalMarkers": _STR_LIST, "escalationMarkers": _STR_LIST,
                                                                                       "escalationTools": _STR_LIST}},
        "verificationRounds": {"type": "integer", "minimum": 1, "maximum": 5},
    },
    "required": ["name", "model", "systemPrompt", "contracts"],
}


def _tool_spec(name: str, description: str, properties: Mapping[str, Any] | None = None, required: Sequence[str] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": dict(properties or {})}
    if required:
        schema["required"] = list(required)
    return {"toolSpec": {"name": name, "description": description, "inputSchema": {"json": schema}}}


TOOLS = [
    _tool_spec("list_knowledge_bases", "Every knowledge base in this workspace: id, name, status, description."),
    _tool_spec("describe_knowledge_base", "One knowledge base: its data sources and documents (file names, indexing status).", {"kbId": {"type": "string"}}, ["kbId"]),
    _tool_spec("search_knowledge_base", "Search a knowledge base the way the agent will (Retrieve): the best passages for a query, with their documents.",
               {"kbId": {"type": "string"}, "query": {"type": "string"}, "results": {"type": "integer", "minimum": 1, "maximum": 8}}, ["kbId", "query"]),
    _tool_spec("list_skills", "The workspace's skill library: name, description, current version."),
    _tool_spec("read_skill", "A skill's SKILL.md.", {"name": {"type": "string"}, "version": {"type": "string"}}, ["name"]),
    _tool_spec("list_gateways", "The workspace's MCP Gateways and whether a new agent can attach each."),
    _tool_spec("describe_gateway", "One Gateway's tools (name as the agent sees it, l1Name as L1 sees it, description).", {"gatewayId": {"type": "string"}},
               ["gatewayId"]),
    _tool_spec("list_agents", "The agents (Harnesses and Runtimes) that already exist in this workspace."),
    _tool_spec("list_models", "The models an agent can use here, with the platform's notes on the recommended ones."),
    {"toolSpec": {"name": "propose_agent", "description": (
        "Submit the agent specification and its behaviour contracts for the console's validation. Returns accepted (it becomes the next revision when "
        "your reply ends) or rejected with the errors to fix. Nothing is created: only the person's approval in the console creates the agent."),
        "inputSchema": {"json": PROPOSAL_SCHEMA}}},
]
_CACHE_OK: dict[str, bool] = {}  # model -> whether it took cache points (dropped once refused)


def _converse(rt: Any, model: str, system: Sequence[Mapping[str, Any]], messages: list[dict[str, Any]]) -> dict[str, Any]:
    """One model call. With cache points after the tools, the system prompt and the newest message (on the request's
    copy: the stored messages stay as they came), every call of a turn's tool loop reads the one before it from the
    cache (live 2026-10-02: each call wrote only its new tail)."""
    cache = _CACHE_OK.get(model, True)
    point = {"cachePoint": {"type": "default"}}
    sent = [*messages[:-1], {**messages[-1], "content": [*messages[-1]["content"], point]}] if cache else messages
    request: dict[str, Any] = {"modelId": model, "messages": sent, "inferenceConfig": {"maxTokens": MAX_TOKENS},
                               "system": list(system) + ([point] if cache else []), "toolConfig": {"tools": TOOLS + ([point] if cache else [])}}
    try:
        return rt.converse(**request)
    except Exception as exc:  # noqa: BLE001 - a model that takes no cache points: once more without them
        if not cache or "cach" not in str(exc).lower():
            raise
        _CACHE_OK[model] = False
        return _converse(rt, model, system, messages)


def system_prompt(conv: Mapping[str, Any], ws: Mapping[str, Any], omitted: int) -> list[dict[str, Any]]:
    parts = [f"## This workspace\nWorkspace `{conv['workspace']}`, region {ws['region']}. The person is {conv.get('owner')}."]
    base = next((p for p in reversed(conv.get("proposals") or []) if isinstance(p.get("content"), dict)), None)
    if base:
        parts.append(f"## Current proposal: revision {base['revision']} ({base['status']}, {'your proposal' if base['source'] == 'model' else 'edited by ' + str(base['createdBy'])})\n"
                     f"```json\n{json.dumps(base['content'], ensure_ascii=False)}\n```")
        if base.get("errors"):
            parts.append("It does not validate:\n" + "\n".join(f"- {e}" for e in base["errors"][:30]))
        if base.get("warnings"):
            parts.append("Its warnings:\n" + "\n".join(f"- {w}" for w in base["warnings"][:20]))
        if base.get("bindings"):
            b = base["bindings"]
            parts.append("Pinned: " + ("; ".join(f"skill {s['name']} {s['version']}" for s in b.get("skills") or []) or "no skills")
                         + f"; tool names L1 will see: {', '.join(b.get('toolNames') or []) or 'none'}.")
    for revision, a in sorted((conv.get("approvals") or {}).items(), key=lambda kv: int(kv[0])):
        if a.get("agent"):
            parts.append(f"Revision {revision} was approved and created the agent {a['agent'].get('name')}: a later revision would create another agent, "
                         "with another name.")
    if omitted:
        parts.append(f"Replay note: {omitted} earlier turn(s) of this conversation were left out to fit the request; ask the person for anything you need from them.")
    return [{"text": SYSTEM}, {"text": "\n\n".join(parts)}]


def compose(conv: Mapping[str, Any], turn: int) -> tuple[list[dict[str, Any]], int]:
    """The bounded replay: earlier turns as text pairs, newest first into the budget, then this turn's message (the
    person's edits since the last turn, as console notes, in front of it)."""
    slots: dict[int, dict[str, list[str]]] = {}
    for m in conv.get("messages") or []:
        slot = slots.setdefault(int(m.get("turn") or 0), {"user": [], "assistant": [], "error": [], "notes": []})
        if m["role"] in ("user", "assistant", "error"):
            slot[m["role"]].append(str(m.get("text") or ""))
        elif m["role"] == "note" and m.get("replay"):
            slot["notes"].append(str(m.get("text") or ""))
    pairs: list[tuple[str, str]] = []
    carried: list[str] = []
    for t in sorted(slots):
        slot = slots[t]
        if t >= turn:
            break
        if slot["user"]:
            reply = "\n\n".join(x for x in slot["assistant"] if x.strip()) or (
                "（这一轮没有完成：" + (slot["error"][-1][:300] if slot["error"] else "没有回复") + "）")
            pairs.append(("\n\n".join(carried + slot["user"]), reply))
            carried = []
        carried += slot["notes"]
    current = "\n\n".join(carried + (slots.get(turn) or {}).get("user", []))
    budget = REPLAY_CHARS - len(current)
    kept: list[tuple[str, str]] = []
    for user, reply in reversed(pairs):
        size = len(user) + len(reply)
        if len(kept) >= REPLAY_TURNS or size > budget:
            break
        kept.insert(0, (user, reply))
        budget -= size
    messages: list[dict[str, Any]] = []
    for user, reply in kept:
        messages += [{"role": "user", "content": [{"text": user}]}, {"role": "assistant", "content": [{"text": reply}]}]
    messages.append({"role": "user", "content": [{"text": current}]})
    return messages, len(pairs) - len(kept)


def _bounded(result: Any) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    return text if len(text) <= TOOL_RESULT_CHARS else text[:TOOL_RESULT_CHARS] + "…(truncated)"


def _input_brief(name: str, args: Any) -> str:
    if name == "propose_agent":
        return f"提案 {args.get('name')}" if isinstance(args, dict) else "提案"
    return json.dumps(args, ensure_ascii=False)[:600] if args else ""


def _result_brief(name: str, ok: bool, result: Mapping[str, Any]) -> str:
    """One line for the transcript: what a tool call found."""
    if not ok:
        return ("被拒绝" if name == "propose_agent" else "错误：") + ("" if name == "propose_agent" else str(result.get("error"))[:200])
    if name == "propose_agent":
        return f"通过，将存为第 {result.get('revision')} 版"
    counts = {"list_knowledge_bases": ("knowledgeBases", "个知识库"), "search_knowledge_base": ("results", "段内容"), "list_skills": ("skills", "个技能"),
              "list_gateways": ("gateways", "个 Gateway"), "describe_gateway": ("tools", "个工具"), "list_agents": ("agents", "个 Agent")}
    if name in counts:
        key, unit = counts[name]
        return f"{len(result.get(key) or [])} {unit}"
    if name == "describe_knowledge_base":
        return f"{result.get('name')} · {sum(len(s.get('documents') or []) for s in result.get('sources') or [])} 个文档"
    if name == "read_skill":
        return f"{result.get('name')} {result.get('version')}"
    if name == "list_models":
        return "读不到模型列表" if result.get("available") is None else f"{len(result['available'])} 个可用模型"
    return ""


def _claim(conv: dict[str, Any], token: str, caller: Mapping[str, Any], message: str) -> dict[str, Any]:
    if _turn_live(conv):
        raise AssistantError(f"turn {conv['inFlight'].get('turn')} of this conversation is still running: wait for it", status=409,
                             activeTurn=conv["inFlight"].get("turn"))
    if int(conv.get("turns") or 0) >= MAX_TURNS:
        raise AssistantError(f"this conversation reached {MAX_TURNS} turns: start a new one", status=409)
    turn = int(conv.get("turns") or 0) + 1
    conv["turns"] = turn
    conv["inFlight"] = {"turn": turn, "token": token, "boot": BOOT, "startedAt": _now(), "by": caller["username"]}
    conv["messages"].append({"turn": turn, "role": "user", "text": message, "author": caller["username"], "at": _now()})
    if not conv.get("title"):
        conv["title"] = message.strip().splitlines()[0][:60]
    return conv


def _append(console: Any, workspace: str, cid: str, token: str, rows: Sequence[Mapping[str, Any]], *, release: bool = False,
            also: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
    """Write a turn's rows, only while this worker still holds the turn's claim."""
    out: dict[str, Any] = {}

    def change(c: dict[str, Any]) -> dict[str, Any]:
        if (c.get("inFlight") or {}).get("token") != token:
            raise _ClaimLost()
        if also:
            out["value"] = also(c)
        c["messages"].extend(dict(r) for r in rows)
        if release:
            c["inFlight"] = None
        return c

    _update(console, workspace, cid, change)
    return out


def _release(console: Any, workspace: str, cid: str, token: str) -> None:
    def change(c: dict[str, Any]) -> dict[str, Any]:
        if (c.get("inFlight") or {}).get("token") == token:
            c["inFlight"] = None
        return c

    try:
        _update(console, workspace, cid, change)
    except AssistantError:
        pass  # deleted meanwhile


def start_turn(console: Any, workspace: str, cid: str, caller: Mapping[str, Any], body: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Claim the next turn (409 while one runs) and run it on a thread; the events it yields are a view of that run:
    ``turn``, ``thinking``, ``text``, ``tool`` / ``toolResult``, ``proposal``, then ``done`` or ``error`` (``ping``
    while the model works)."""
    message = str(body.get("message") or "").strip()
    if not message:
        raise AssistantError("message is empty")
    if len(message) > MAX_MESSAGE:
        raise AssistantError(f"message: at most {MAX_MESSAGE} characters", status=413)
    _open(console, workspace, cid, caller)
    calls: list[str] = []
    disc, ws, ro = _discovery(console, workspace, calls)  # the clients are made here, on the request's thread
    token = uuid.uuid4().hex
    with _LIVE_LOCK:
        _LIVE[token] = None  # alive from before the claim: nobody may take it over as an orphan
    try:
        conv = _update(console, workspace, cid, lambda c: _claim(c, token, caller, message))
    except Exception:
        with _LIVE_LOCK:
            _LIVE.pop(token, None)
        raise
    events: queue.Queue = queue.Queue()
    worker = threading.Thread(target=_run_turn, args=(console, workspace, cid, conv["inFlight"]["turn"], token, caller, disc, ws, ro, events),
                              daemon=True, name=f"assistant-{cid}")
    with _LIVE_LOCK:
        _LIVE[token] = worker
    worker.start()

    def drain() -> Iterator[dict[str, Any]]:
        while True:
            try:
                event = events.get(timeout=HEARTBEAT)
            except queue.Empty:
                yield {"type": "ping"}
                continue
            if event is None:
                return
            yield event

    return drain()


def _run_turn(console: Any, workspace: str, cid: str, turn: int, token: str, caller: Mapping[str, Any], disc: Discovery, ws: Mapping[str, Any],
              ro: ReadOnlySession, events: queue.Queue) -> None:
    emit = events.put
    started = time.monotonic()
    usage = {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}
    submitted: list[dict[str, Any]] = []
    calls = 0
    released = False
    try:
        emit({"type": "turn", "turn": turn, "conversation": cid})
        conv = console.store.read(_ckey(workspace, cid), None) or {}
        messages, omitted = compose(conv, turn)
        system = system_prompt(conv, ws, omitted)
        model = str(conv.get("model") or DEFAULT_MODEL)
        rt = ro.client("bedrock-runtime")
        stop = None
        for calls in range(1, MAX_MODEL_CALLS + 1):
            emit({"type": "thinking", "call": calls})
            out = _converse(rt, model, system, messages)
            for k in usage:
                usage[k] += int((out.get("usage") or {}).get(k) or 0)
            msg = (out.get("output") or {}).get("message") or {"role": "assistant", "content": []}
            stop = out.get("stopReason")
            content = list(msg.get("content") or [])
            messages.append({"role": "assistant", "content": content})  # exactly as it came (reasoning blocks included)
            text = "".join(str(b.get("text") or "") for b in content if "text" in b).strip()
            if text:
                _append(console, workspace, cid, token, [{"turn": turn, "role": "assistant", "text": text, "at": _now()}])
                emit({"type": "text", "text": text})
            uses = [b["toolUse"] for b in content if "toolUse" in b]
            if not uses:
                break
            results, rows = [], []
            for use in uses:
                name, args = str(use.get("name") or ""), use.get("input") if isinstance(use.get("input"), dict) else {}
                emit({"type": "tool", "name": name, "input": _input_brief(name, args)})
                if stop == "max_tokens":
                    ok, result = False, {"error": "your reply was cut off at the output limit, so this call is incomplete: send a shorter one"}
                elif name == "propose_agent":
                    ok, result = _submit(args, disc, conv, submitted)
                else:
                    try:
                        ok, result = True, run_tool(name, args, disc)
                    except Exception as exc:  # noqa: BLE001 - the model reads the error and carries on
                        ok, result = False, {"error": f"{type(exc).__name__}: {str(exc)[:400]}"}
                brief = _result_brief(name, ok, result)
                rows.append({"turn": turn, "role": "tool", "name": name, "input": _input_brief(name, args), "ok": ok, "summary": brief,
                             "errors": result.get("errors") if name == "propose_agent" else None, "at": _now()})
                emit({"type": "toolResult", "name": name, "ok": ok, "summary": brief, "errors": result.get("errors") if name == "propose_agent" else None})
                results.append({"toolResult": {"toolUseId": use["toolUseId"], "content": [{"text": _bounded(result)}], "status": "success" if ok else "error"}})
            _append(console, workspace, cid, token, rows)
            messages.append({"role": "user", "content": results})
        else:
            _append(console, workspace, cid, token, [{"turn": turn, "role": "note", "kind": "limit", "at": _now(),
                                                       "text": f"这一轮调用模型达到 {MAX_MODEL_CALLS} 次上限，停下了"}])
        rows = [{"turn": turn, "role": "meta", "at": _now(), "seconds": round(time.monotonic() - started, 1), "modelCalls": calls, "usage": usage,
                 "stopReason": stop, "awsCalls": len(ro.calls), "operations": sorted(set(ro.calls))}]
        proposal: dict[str, Any] = {}
        if submitted:
            last = submitted[-1]

            def record(c: dict[str, Any]) -> dict[str, Any]:
                rec = _store_revision(c, source="model", content=last["content"], checked=last, by=str(caller["username"]), turn=turn)
                c["messages"].append({"turn": turn, "role": "note", "kind": "proposal", "replay": False, "at": _now(), "revision": rec["revision"],
                                      "status": rec["status"], "text": f"第 {rec['revision']} 版（{'有效' if rec['status'] == 'draft' else '无效'}）"})
                return {k: rec[k] for k in ("revision", "status", "hash", "errors", "warnings")}

            proposal = _append(console, workspace, cid, token, rows, release=True, also=record).get("value") or {}
        else:
            _append(console, workspace, cid, token, rows, release=True)
        released = True
        if proposal:
            emit({"type": "proposal", **proposal})
        emit({"type": "done", "turn": turn, "seconds": rows[0]["seconds"], "usage": usage})
    except _ClaimLost:
        released = True
        emit({"type": "error", "error": "this turn's claim was taken over: its reply was not kept"})
    except Exception as exc:  # noqa: BLE001 - recorded on the turn; the next one replays it as unfinished
        message = f"{type(exc).__name__}: {str(exc)[:500]}"
        try:
            _append(console, workspace, cid, token, [{"turn": turn, "role": "error", "text": message, "at": _now()}], release=True)
            released = True
        except Exception:  # noqa: BLE001
            pass
        emit({"type": "error", "error": message})
    finally:
        if not released:
            _release(console, workspace, cid, token)
        with _LIVE_LOCK:
            _LIVE.pop(token, None)
        emit(None)


def _submit(args: Mapping[str, Any], disc: Discovery, conv: Mapping[str, Any], submitted: list[dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
    """The console's verdict on one propose_agent call; the turn's last submission becomes its revision."""
    if len(submitted) >= MAX_SUBMISSIONS:
        return False, {"status": "rejected", "errors": [f"submission limit reached ({MAX_SUBMISSIONS} per reply): end the reply, list the open problems "
                                                        "for the person and wait for their answer"]}
    checked = check_proposal(dict(args), disc)
    submitted.append({"content": dict(args), **checked})
    candidate = int(conv.get("revisionSeq") or 0) + 1
    if checked["errors"]:
        return False, {"status": "rejected", "errors": checked["errors"], "warnings": checked["warnings"],
                       "next": "Fix exactly these errors and call propose_agent again before ending your reply."}
    b = checked["bindings"]
    return True, {"status": "accepted", "revision": candidate, "warnings": checked["warnings"],
                  "pinned": {"skills": [f"{s['name']} {s['version']}" for s in b["skills"]], "toolNames": b["toolNames"]},
                  "next": f"It becomes revision {candidate} when your reply ends. Explain the design and each contract in plain words; the person can edit "
                          f"it, or approve revision {candidate} in the proposal panel. Do not print the JSON."}


# -- approval: the only executor -----------------------------------------------------------------------------------------

def approve(console: Any, workspace: str, cid: str, caller: Mapping[str, Any], body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Approve exactly ``revision`` with ``hash``. 202 with the started approval, 200 with a recorded one, 409 on a
    stale, invalid or drifted revision (nothing written)."""
    try:
        revision = int(body.get("revision"))
    except (TypeError, ValueError):
        raise AssistantError("revision: the number of the revision you reviewed") from None
    digest, resume = str(body.get("hash") or ""), body.get("resume") is True
    conv, _who = _open(console, workspace, cid, caller)
    rec = _revision(conv, revision)
    if rec["hash"] != digest:
        raise AssistantError(f"revision {revision} is not what you reviewed (its hash differs): look at it again", status=409, currentRevision=revision)
    recorded = (conv.get("approvals") or {}).get(str(revision))
    resumable = bool(recorded) and resume and effective_status(console, recorded) in ("failed", "interrupted")
    if recorded and not resumable:
        return 200, {"approval": approval_view(console, recorded), "recorded": True}
    if not recorded:
        if rec["status"] != "draft":
            raise AssistantError(f"revision {revision} is {rec['status']}: only the latest valid draft can be approved", status=409, errors=rec.get("errors"))
        if conv["proposals"][-1]["revision"] != revision:
            raise AssistantError(f"revision {conv['proposals'][-1]['revision']} is the latest now: review it", status=409,
                                 currentRevision=conv["proposals"][-1]["revision"])
    if _turn_live(conv):
        raise AssistantError("the assistant is answering: approve after its reply", status=409)
    others = [r for r in _approvals_running(console, conv) if r != str(revision)]
    if others:
        raise AssistantError(f"the approval of revision {others[0]} is still running", status=409)
    disc, _ws, _ro = _discovery(console, workspace)  # a fresh, read-only look at the workspace before anything is written
    own = ((recorded or {}).get("agent") or {}).get("id") if resumable else None
    checked = check_proposal(rec["content"], disc, own_harness=own)
    if checked["errors"]:
        raise AssistantError(f"revision {revision} no longer validates in this workspace", status=409, errors=checked["errors"])
    changes = binding_changes(rec["bindings"], checked["bindings"]) + skill_byte_changes(disc, rec["bindings"])
    if changes:
        raise AssistantError(f"what revision {revision} refers to changed after it was reviewed: propose or edit a new revision", status=409, changes=changes)

    def claim(c: dict[str, Any]) -> dict[str, Any]:
        a = (c.get("approvals") or {}).get(str(revision))
        if a and not (resume and effective_status(console, a) in ("failed", "interrupted")):
            raise _Recorded(a)
        r = _revision(c, revision)
        if not a and (r["status"] != "draft" or c["proposals"][-1]["revision"] != revision or _turn_live(c)):
            raise AssistantError("the conversation changed meanwhile: look at the latest revision", status=409)
        r["status"] = "approved"
        c.setdefault("approvals", {})[str(revision)] = {
            "revision": revision, "hash": digest, "approvedBy": (a or {}).get("approvedBy") or caller["username"],
            "approvedAt": (a or {}).get("approvedAt") or _now(), "resumedBy": caller["username"] if a else None, "status": "running", "startedEpoch": time.time(),
            "steps": list((a or {}).get("steps") or []), "agent": (a or {}).get("agent"), "contractSet": None, "verification": None, "error": None,
            "job": None, "jobs": list((a or {}).get("jobs") or [])}
        c["messages"].append({"turn": c.get("turns", 0), "role": "note", "kind": "approval", "replay": True, "at": _now(), "revision": revision,
                              "text": f"[控制台] {caller['username']} {'继续执行' if a else '批准了'}第 {revision} 版：控制台正在创建 Agent、挂知识库、用上技能、保存契约并开始验证。"})
        return c

    try:
        _update(console, workspace, cid, claim)
    except _Recorded as done:
        return 200, {"approval": approval_view(console, done.approval), "recorded": True}
    name = str(rec["content"].get("name"))
    job = console.jobs.start("assistant-approve", workspace, {"conversation": cid, "revision": revision, "agent": name},
                             lambda j: _execute(console, workspace, cid, revision, j), label=f"批准 {name}（第 {revision} 版）")

    def link(c: dict[str, Any]) -> dict[str, Any]:
        a = c["approvals"][str(revision)]
        a["job"] = job["id"]
        a["jobs"] = a.get("jobs", []) + [job["id"]]
        return c

    conv = _update(console, workspace, cid, link)
    return 202, {"approval": approval_view(console, conv["approvals"][str(revision)]), "recorded": False}


def _set(console: Any, workspace: str, cid: str, revision: int, **fields: Any) -> None:
    def change(c: dict[str, Any]) -> dict[str, Any]:
        c["approvals"][str(revision)].update(fields)
        return c

    _update(console, workspace, cid, change)


def _mark(console: Any, workspace: str, cid: str, revision: int, key: str, label: str, status: str, **fields: Any) -> None:
    def change(c: dict[str, Any]) -> dict[str, Any]:
        steps = c["approvals"][str(revision)].setdefault("steps", [])
        found = next((s for s in steps if s["key"] == key), None)
        if found is None:
            found = {"key": key, "label": label}
            steps.append(found)
        found.update(status=status, at=_now(), **fields)
        return c

    _update(console, workspace, cid, change)


def _wait_ready(session: Any, region: str, harness_id: str, *, attempts: int = 72, pause: float = 5.0) -> dict[str, Any]:
    ctl = client(session, "bedrock-agentcore-control", region)
    status = None
    for _ in range(attempts):
        status = ctl.get_harness(harnessId=harness_id)["harness"].get("status")
        if status == "READY":
            return {"status": status}
        if status and "FAIL" in status:
            raise AssistantError(f"Harness {harness_id} is {status}")
        _sleep(pause)
    raise AssistantError(f"Harness {harness_id} was still {status} after {int(attempts * pause)} s")


def _execute(console: Any, workspace: str, cid: str, revision: int, job: Any) -> dict[str, Any]:
    """The approval job: every step recorded on the revision; steps a resumed approval already did are skipped."""
    conv = console.store.read(_ckey(workspace, cid), None)
    rec = _revision(conv, revision)
    content, bindings = rec["content"], rec["bindings"]
    done = {s["key"]: s for s in conv["approvals"][str(revision)].get("steps") or [] if s.get("status") == "done"}
    ws = console.workspaces.get(workspace)
    console.workspaces.verify(workspace)
    session, region = console.workspaces.session(workspace), ws["region"]

    def step(key: str, label: str, fn: Callable[[], Any]) -> Any:
        if key in done:
            job.log(f"{label}：上次已完成")
            return done[key].get("result")
        _mark(console, workspace, cid, revision, key, label, "running")
        job.log(f"{label}…")
        job.progress(step=key, label=label)
        try:
            result = fn()
        except Exception as exc:
            _mark(console, workspace, cid, revision, key, label, "failed", error=f"{type(exc).__name__}: {str(exc)[:600]}")
            raise
        _mark(console, workspace, cid, revision, key, label, "done", result=result)
        job.log(f"{label}：完成")
        return result

    try:
        made = step("harness", f"创建 Harness {content['name']}（和它的执行角色）", lambda: {
            k: v for k, v in agents.create_harness(session, account=ws["accountId"], region=region, body=_harness_body(content, bindings["gateways"]),
                                                   boundary=ws.get("permissionsBoundaryArn")).items() if k in ("id", "arn", "name", "status", "role")})
        hid = made["id"]
        _set(console, workspace, cid, revision, agent={"id": hid, "name": made["name"], "arn": made["arn"], "role": made.get("role")})
        step("ready", "等 Harness 就绪", lambda: _wait_ready(session, region, hid))
        for k in bindings["knowledgeBases"]:
            def attach(k: Mapping[str, Any] = k) -> dict[str, Any]:
                out = kb.attach(console, workspace, k["id"], {"harnessId": hid})
                _wait_ready(session, region, hid)
                return {"tools": out.get("tools"), "gatewayArn": out.get("gatewayArn")}

            step(f"kb:{k['id']}", f"挂上知识库 {k['name']}（{k['id']}）", attach)
        for s in bindings["skills"]:
            def apply(s: Mapping[str, Any] = s) -> dict[str, Any]:
                out = skills_lab.apply_to_agent(console, workspace, s["name"], {"harnessId": hid, "version": s["version"]})
                _wait_ready(session, region, hid)
                return {"version": out.get("version"), "skills": out.get("skills")}

            step(f"skill:{s['name']}", f"用上技能 {s['name']} {s['version']}", apply)
        contracts = [{"id": c["id"], "query": c["query"], "expected": c["expected"], "label": c.get("label") or c["id"],
                      "category": contract_kind(c["expected"])} for c in content["contracts"]]
        saved = step("contracts", f"保存 {len(contracts)} 条行为契约", lambda: {k: v for k, v in evaluation.put_contract_set(console.store, workspace, {
            "id": f"cs-{cid[5:]}-r{revision}", "name": f"{content['name']} · 助手第 {revision} 版", "contracts": contracts, "l1": dict(content.get("l1") or {}),
            "source": f"assistant:{cid}:r{revision}"}).items() if k in ("id", "name")})
        rounds = int(content.get("verificationRounds") or DEFAULT_ROUNDS)
        started = step("verification", f"开始验证（{rounds} 轮 × {len(contracts)} 条契约）", lambda: {k: v for k, v in evaluation.start_verification(console, workspace, {
            "contractSet": saved["id"], "agentKind": "harness", "agentId": hid, "repeat": rounds, "panel": False}).items() if k in ("id", "status")})
    except Exception as exc:
        _set(console, workspace, cid, revision, status="failed", error=f"{type(exc).__name__}: {str(exc)[:600]}", finishedAt=_now())
        raise
    _set(console, workspace, cid, revision, status="succeeded", contractSet=saved["id"], verification=started["id"], finishedAt=_now(), error=None)
    return {"agent": {"id": hid, "name": made["name"], "arn": made["arn"]}, "contractSet": saved["id"], "verification": started["id"]}


# -- routes --------------------------------------------------------------------------------------------------------------

def register(router: Any) -> None:
    """Add this module's /api/console routes."""
    def handle(fn: Callable[[Any], Any], status: int = 200) -> Callable[[Any], Any]:
        def route(r: Any) -> Any:
            try:
                out = fn(r)
            except AssistantError as exc:
                return exc.status, {"error": str(exc), **exc.extra}
            return out if isinstance(out, tuple) else (status, out)

        return route

    def turn(r: Any) -> Any:
        try:
            return sse(start_turn(r.console, r.workspace(), r.params["cid"], r.caller, r.body))
        except AssistantError as exc:
            return exc.status, {"error": str(exc), **exc.extra}

    add, base = router.add, "/workspaces/{wid}/assistant"
    add("GET", base + "/models", handle(lambda r: (r.workspace(), {"models": [{"id": m, "label": label} for m, label in ASSISTANT_MODELS],
                                                                    "default": DEFAULT_MODEL})[1]))
    add("GET", base + "/conversations", handle(lambda r: {"conversations": list_conversations(r.console, r.workspace(), r.caller)}))
    add("POST", base + "/conversations", handle(lambda r: create(r.console, r.workspace(), r.caller, r.body), 201))
    add("GET", base + "/conversations/{cid}", handle(lambda r: detail(r.console, r.workspace(), r.params["cid"], r.caller)))
    add("DELETE", base + "/conversations/{cid}", handle(lambda r: delete(r.console, r.workspace(), r.params["cid"], r.caller)))
    add("POST", base + "/conversations/{cid}/share", handle(lambda r: share(r.console, r.workspace(), r.params["cid"], r.caller, r.body)), admin=True)
    add("POST", base + "/conversations/{cid}/turns", turn)
    add("POST", base + "/conversations/{cid}/proposals", handle(lambda r: edit(r.console, r.workspace(), r.params["cid"], r.caller, r.body), 201))
    add("POST", base + "/conversations/{cid}/approve", handle(lambda r: approve(r.console, r.workspace(), r.params["cid"], r.caller, r.body)))
