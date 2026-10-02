"""Console module ``studio`` — Strands Studio: a canvas of nodes that becomes a Strands agent on AgentCore Runtime.

The page (``app/console/static/pages/studio.mjs``) edits a **flow** — ``{nodes, edges, graphMode}`` — and this module
checks it (:func:`validate`, every problem explained in Chinese and tied to its node), turns it into Python
(:func:`generate`: ``main.py``, ``requirements.txt`` and ``studio_flow.json``) and deploys it through the console's
own code deployment (:func:`deploy_project` → ``deploy.start_deploy``: the zip, its requirements installed for
linux/aarch64 by CodeBuild, the runtime role, CreateAgentRuntime or a new version, a smoke call). Nothing a user
writes runs on the console's machine: a custom tool is parsed (``ast``) and the generated module compiled, never
executed here.

Nodes and their ports (``source → target``; Graph mode adds Agent → Agent as "runs after"):

==============  =========================  ===========================================================================
type            ports                      becomes
==============  =========================  ===========================================================================
input           out                        the payload's ``prompt`` (``sample`` is the smoke call's question)
output          in                         the reply, ``text/plain`` (``traceTools``: a 〔工具〕 line of the turn's calls)
agent           in, tools, parent, out     ``Agent(model=BedrockModel(...), system_prompt, tools)``
orchestrator    in, tools, parent, out,    an Agent whose tools include its sub-agents (``sub.as_tool(name,
                sub                        description)``; the sub-agent's 职责说明 tells the orchestrator when to call it)
swarm           in, out, sub               ``Swarm([members], entry_point, max_handoffs, ...)``: members hand off
tool            tool                       a built-in tool (:data:`BUILTIN_TOOLS`)
custom-tool     tool                       one ``@tool`` function (:func:`check_tool`)
kb              tool                       ``@tool`` over ``bedrock-agent-runtime.Retrieve`` on a workspace KB
==============  =========================  ===========================================================================

A custom tool's function name may not be one the generated module binds: its own names (:data:`RESERVED`), the
locals of ``build()`` (``trace``, ``builder``) and the flow's agents' variables (``agent_<name>``, ``swarm_<name>``),
which would shadow the tool there (the Agent would get the local instead), nor a builtin or a keyword.

Without Graph mode exactly one Agent, 编排 Agent or Swarm takes the input and gives the answer; with it, every Agent
or Swarm without a parent is a Strands Graph node, an edge between two of them is a dependency and the Output's
upstream nodes make the answer. The generated ``main.py`` builds the agents once per runtime session (an AgentCore
session is one microVM, so an Agent keeps the conversation natively; a Swarm or Graph, which reset each run, gets the
last turns prepended), records every tool call with a hook (``AfterToolCallEvent``, sub-agents and swarm members
included) and answers ``text/plain`` — the console's chat shows a runtime's raw body, and the deploy module's smoke
call reads text, JSON or SSE alike — or, for ``{"format": "json"}``, ``{"result", "tools", "seconds", "flow"}``.

Projects (``studio_projects`` in the console store) keep every saved version of a flow (at most
:data:`MAX_VERSIONS`) and the deployments made from them; the zip carries ``studio_flow.json``, so a deployed
runtime reopens in Studio even from another console (:func:`open_runtime` reads it back from the deployment's
``source.zip``). A runtime named ``adlc_probe_<module>_*`` is a live probe and deploys with its module's own names
(``deploy.Names("adlc-probe-<module>")``: role ``adlc-probe-<module>-rt-<name>``, build project
``adlc-probe-<module>-build``), never touching the console's shared ``adlc-console-build`` nor another module's probe.

Live 2026-10-02 (us-west-2, ``adlc_probe_studio_860b83``: the 知识库客服 template — an Agent on Claude Sonnet 4.5 with a
custom ledger tool and a Retrieve tool on a managed KB):

* **Deploy, 118 s**: validate 1.8 s, upload 2.0 s (the zip is 6 KB), build 63.8 s (CodeBuild 0.8 min — provisioning
  7 s, pip 33 s — into a 28.1 MB bundle of 50 linux/aarch64 cp313 wheels; with strands-agents-tools 77 wheels,
  48 MB), runtime 13.5 s (a new role and its 10 s pause), ready 16.9 s, smoke 12.8 s (the Input node's sample
  question: both tools, 7.5 s of it the agent). **A new version, 95 s**: build 53 s, UpdateAgentRuntime 1.7 s,
  ready 11.5 s, smoke 11.8 s; the role's policy is put again with the KB grant.
* **Chat through the console's runtime route**: a new session's first turn 15 s (8.2 s agent: ~7 s is the microVM's
  cold start), the next 9.3 s and 4.8 s; the same session reaches the same process, so the Agent built for it keeps
  the conversation (a third turn recalled the member id). The answer is plain text, so the chat shows it as it is.
* **Retrieve on a managed KB** needs only ``bedrock:Retrieve`` on the KB's ARN in the runtime role (no
  GetKnowledgeBase, no model access) and ``managedSearchConfiguration``, which boto3 1.43.90 sends and the AWS CLI's
  bundled botocore refuses client-side ("Unknown parameter in retrievalConfiguration").
* **AgentCore drops the body of a non-2xx answer**: InvokeAgentRuntime raises ``RuntimeClientError: Received error
  (400) from runtime. Please check your CloudWatch logs`` — hence :func:`runtime_logs` and the one-line
  ``turn failed {...}`` the generated code logs. The DEFAULT endpoint's log group holds one stream per session
  (``<yyyy/mm/dd>/[runtime-logs-<session id>]<id>``, besides ``otel-rt-logs`` and ``spans``); a line is found by
  ``FilterLogEvents`` some 30 s after it is written.
* **bedrock-agentcore 1.24** passes a returned starlette ``Response`` through (status and media type); a str or dict
  is JSON-encoded (``"..."``) and a generator becomes ``data:`` lines, both of which the console's chat would show
  raw. ``app.run()`` binds 127.0.0.1 outside Docker and still serves on AgentCore's code runtime.
* **strands-agents-tools 0.8.9** deprecates calculator, current_time and http_request ("an error log in v0.9.0"; they
  still work — pinned); its http_request asks a console to confirm POST/PUT/PATCH/DELETE unless
  ``BYPASS_TOOL_CONSENT=true``. strands 1.57's vended ``http_request`` never asks but returns whole bodies: locally
  an orchestrator's researcher read five AWS pages and Bedrock threw a context window overflow, so HTTP is the
  Studio's own (httpx, HTML to text, capped at the node's ``maxChars``).
* **strands 1.57**: ``Agent.as_tool(name, description)`` makes a sub-agent a tool (its context reset on every call);
  Swarm and Graph reset their agents on every run; an ``AfterToolCallEvent`` hook sees the calls of sub-agents and
  swarm members (``handoff_to_agent`` included); a tool that raises becomes an error result with ``event.exception``.
* A canary probe of another module was using ``adlc-probe-build`` at the same time: hence per-module probe names.
"""
from __future__ import annotations

import ast
import base64
import builtins
import hashlib
import io
import json
import keyword
import re
import secrets
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..direct.aws import client
from . import deploy
from .kb import console_bucket

# -- versions the generated agent pins -------------------------------------------------------------------------------

STRANDS = "strands-agents==1.57.1"
AGENTCORE = "bedrock-agentcore==1.24.0"
#: calculator and current_time (deprecated in 0.8.9 — "an error log in v0.9.0" — but working, hence the pin).
TOOLS_PACKAGE = "strands-agents-tools==0.8.9"
#: Retrieve on a managed KB takes ``managedSearchConfiguration``, which older botocore models refuse client-side.
BOTO = ("boto3>=1.43.90,<2", "botocore>=1.43.90,<2")
PYTHON = "PYTHON_3_13"
FORMAT = "adlc-studio-flow/1"

# -- limits ----------------------------------------------------------------------------------------------------------

MAX_NODES, MAX_EDGES, MAX_EXECUTABLES, MAX_TOOLS = 60, 200, 20, 30
MAX_PROMPT, MAX_CODE, MAX_FLOW_BYTES = 20_000, 20_000, 400_000
MAX_VERSIONS, MAX_DEPLOYMENTS, MAX_PROJECTS = 50, 50, 200
COLLECTION = "studio_projects"

NODE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
PROJECT_ID = re.compile(r"^sp-[0-9a-f]{10}$")
IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")  # agent names: Strands names, Graph node ids, sub-agent tool names
TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")  # what the model calls (Bedrock: [a-zA-Z0-9_-]{1,64})
MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9.:_\-/]{2,199}$")
KB_ID = deploy.KB_ID

EXECUTABLE = ("agent", "orchestrator", "swarm")
TOOLISH = ("tool", "custom-tool", "kb")
NODE_TYPES = ("input", "output", *EXECUTABLE, *TOOLISH)
TYPE_LABEL = {"input": "输入", "output": "输出", "agent": "Agent", "orchestrator": "编排 Agent", "swarm": "Swarm", "tool": "内置工具",
              "custom-tool": "自定义工具", "kb": "知识库检索"}

#: Each node type's ports: name → ``source`` (an edge starts there) or ``target``.
PORTS: dict[str, dict[str, str]] = {
    "input": {"out": "source"},
    "output": {"in": "target"},
    "agent": {"in": "target", "tools": "target", "parent": "target", "out": "source"},
    "orchestrator": {"in": "target", "tools": "target", "parent": "target", "out": "source", "sub": "source"},
    "swarm": {"in": "target", "out": "source", "sub": "source"},
    "tool": {"tool": "source"}, "custom-tool": {"tool": "source"}, "kb": {"tool": "source"},
}

#: Which port may connect to which: (source types, source port, target types, target port, Graph mode only, kind).
RULES: tuple[tuple[tuple[str, ...], str, tuple[str, ...], str, bool, str], ...] = (
    (("input",), "out", EXECUTABLE, "in", False, "input"),
    (TOOLISH, "tool", ("agent", "orchestrator"), "tools", False, "tool"),
    (("orchestrator",), "sub", ("agent", "orchestrator"), "parent", False, "member"),
    (("swarm",), "sub", ("agent",), "parent", False, "member"),
    (EXECUTABLE, "out", ("output",), "in", False, "output"),
    (EXECUTABLE, "out", EXECUTABLE, "in", True, "dependency"),
)

DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
MODELS = (
    ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "Claude Sonnet 4.5"),
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "Claude Haiku 4.5（快）"),
    ("us.anthropic.claude-sonnet-5", "Claude Sonnet 5"),
    ("us.anthropic.claude-opus-4-8", "Claude Opus 4.8"),
    ("us.amazon.nova-2-lite-v1:0", "Nova 2 Lite（便宜）"),
    ("us.amazon.nova-pro-v1:0", "Nova Pro"),
)

#: The built-in tools a node may add, all working in a headless runtime (no console to confirm anything on). ``name``
#: is what the model calls, ``symbol`` the Python object the agent gets. HTTP is the Studio's own, on httpx (a strands
#: dependency): strands' vended http_request returns whole bodies, and live five web pages overflowed a context window.
BUILTIN_TOOLS: dict[str, dict[str, Any]] = {
    "calculator": {"label": "计算器", "name": "calculator", "symbol": "calculator", "imports": ("from strands_tools import calculator",),
                   "package": TOOLS_PACKAGE, "description": "精确计算算式、方程和单位换算（strands-agents-tools，sympy）"},
    "current_time": {"label": "当前时间", "name": "current_time", "symbol": "current_time", "imports": ("from strands_tools import current_time",),
                     "package": TOOLS_PACKAGE, "description": "当前日期和时间，可指定时区（strands-agents-tools）；节点上设默认时区"},
    "http_request": {"label": "HTTP 请求", "name": "http_request", "symbol": "_http_request", "imports": (), "package": None,
                     "description": "GET/POST/PUT/PATCH/DELETE 任意 http(s) 地址；网页转成纯文本并按字数截断（httpx）。Runtime 里没有人确认：写操作会直接发出"},
}

#: What a custom tool may import: the standard library's harmless parts and what the runtime's bundle has.
SAFE_MODULES = frozenset({
    "base64", "binascii", "bisect", "calendar", "cmath", "collections", "copy", "csv", "dataclasses", "datetime", "decimal", "difflib", "enum",
    "fractions", "functools", "hashlib", "heapq", "hmac", "html", "io", "ipaddress", "itertools", "json", "math", "operator", "pprint", "random",
    "re", "statistics", "string", "textwrap", "time", "typing", "unicodedata", "urllib", "urllib.error", "urllib.parse", "urllib.request", "uuid",
    "zoneinfo", "boto3", "botocore", "httpx", "requests"})
#: Imports that add a requirement (the rest ship with Python or with strands / bedrock-agentcore).
EXTRA_PACKAGES = {"requests": "requests>=2.31,<3"}
FORBIDDEN_CALLS = frozenset({"eval", "exec", "compile", "__import__", "open", "input", "breakpoint", "exit", "quit", "globals", "locals", "vars",
                             "help", "setattr", "delattr", "memoryview"})
ANNOTATION_NAMES = frozenset({"str", "int", "float", "bool", "list", "dict", "tuple", "set", "bytes"})
#: Modules the generated module imports: a custom tool's own ``import re`` binds the same module and is fine.
GENERATED_MODULES = frozenset({"json", "logging", "os", "threading", "time", "re", "html", "httpx", "boto3"})
#: The locals the generated ``build()`` binds besides each executable node's ``agent_<name>`` / ``swarm_<name>``
#: (:func:`_var`): a custom tool of the same name would be shadowed there, the Agent given the local instead.
BUILD_LOCALS = frozenset({"trace", "builder"})
#: Names the generated module defines or imports (besides its ``_private`` ones), and the locals of ``build()``: a
#: custom tool may not take them (an ``agent_<name>`` / ``swarm_<name>`` of the flow's own nodes is refused by
#: :func:`validate`, which knows the flow).
RESERVED = frozenset({"app", "build", "invoke", "logger", "REGION", "FLOW", "TRACE_TOOLS", "MAX_SESSIONS", "HISTORY_TURNS", "OUTPUTS", "LABELS",
                      "HTTP_MAX_CHARS", "Agent", "tool", "BedrockModel", "Swarm", "GraphBuilder", "HookProvider", "HookRegistry", "AfterToolCallEvent",
                      "BedrockAgentCoreApp", "Response", *GENERATED_MODULES, *BUILTIN_TOOLS, *BUILD_LOCALS}) | {
    n for n in dir(builtins) if not n.startswith("_")} | set(keyword.kwlist) | set(keyword.softkwlist)

DEFAULTS: dict[str, dict[str, Any]] = {
    "input": {"label": "用户输入", "sample": ""},
    "output": {"label": "回答", "traceTools": True},
    "agent": {"label": "Agent", "name": "", "description": "", "model": DEFAULT_MODEL, "systemPrompt": "你是一个乐于助人的助手，用中文简洁地回答。",
              "temperature": None, "maxTokens": 4096},
    "orchestrator": {"label": "编排 Agent", "name": "", "description": "", "model": DEFAULT_MODEL,
                     "systemPrompt": "你是协调者：把问题拆开，交给合适的子 Agent，最后汇总成一个完整的回答。", "temperature": None, "maxTokens": 4096},
    "swarm": {"label": "Swarm", "name": "", "entry": "", "maxHandoffs": 8, "maxIterations": 12, "executionTimeout": 300, "nodeTimeout": 120},
    "tool": {"label": "内置工具", "tool": "calculator", "timezone": "Asia/Shanghai", "maxChars": 12000},
    "custom-tool": {"label": "自定义工具", "code": ""},
    "kb": {"label": "知识库检索", "kbId": "", "kbName": "", "kbType": "MANAGED", "toolName": "", "description": "", "topK": 5},
}
CUSTOM_TOOL_TEMPLATE = '''def lookup_order(order_id: str) -> str:
    """查询订单的状态（示例：换成你自己的逻辑）。

    Args:
        order_id: 订单号，例如 A1001
    """
    orders = {"A1001": "已发货", "A1002": "待付款"}
    return f"订单 {order_id}：{orders.get(order_id.strip().upper(), '没有这个订单')}"
'''


class NotFound(LookupError):
    """No such project, version or runtime in this workspace (the route answers 404)."""


class StudioError(ValueError):
    """A request the caller must fix; ``issues`` are a flow's validation problems when that is why."""

    def __init__(self, message: str, issues: Sequence[Mapping[str, Any]] | None = None):
        super().__init__(message)
        self.issues = list(issues or [])


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- text ----------------------------------------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(text: Any, limit: int | None = None) -> str:
    """A string as the flow keeps it: no control characters but newline and tab, ``\\n`` line ends, valid UTF-8."""
    out = _CONTROL.sub("", str("" if text is None else text).replace("\r\n", "\n").replace("\r", "\n"))
    out = out.encode("utf-8", "replace").decode("utf-8")
    return out[:limit] if limit else out


def py_str(text: str) -> str:
    """A Python literal for ``text``: triple-quoted when it has lines (readable in the preview), checked to read
    back exactly."""
    text = _clean(text)
    if "\n" in text and len(text) > 40:
        body = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        if body.endswith('"'):
            body = body[:-1] + '\\"'
        literal = '"""' + body + '"""'
        try:
            if ast.literal_eval(literal) == text:
                return literal
        except (SyntaxError, ValueError):  # pragma: no cover - the escaping above is total; the check is the guard
            pass
    return json.dumps(text, ensure_ascii=False)


def _line(text: Any, limit: int = 80) -> str:
    """One line of text for a comment in the generated code."""
    return re.sub(r"\s+", " ", _clean(text)).strip()[:limit]


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:limit].strip("_")


# -- the flow ------------------------------------------------------------------------------------------------------

def _num(value: Any, default: float | None, lo: float, hi: float, *, integer: bool = False) -> float | int | None:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    number = max(lo, min(hi, number))
    return int(round(number)) if integer else round(number, 3)


def _node_data(kind: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """A node's properties, typed and bounded (unknown keys dropped)."""
    d = {**DEFAULTS[kind], **{k: v for k, v in data.items() if k in DEFAULTS[kind]}}
    out: dict[str, Any] = {"label": _line(d.get("label"), 40) or DEFAULTS[kind]["label"]}
    if kind == "input":
        out["sample"] = _clean(d.get("sample"), 500).strip()
    elif kind == "output":
        out["traceTools"] = d.get("traceTools") is not False
    elif kind in ("agent", "orchestrator"):
        out.update(name=_line(d.get("name"), 48), description=_clean(d.get("description"), 500).strip(), model=_line(d.get("model"), 200),
                   systemPrompt=_clean(d.get("systemPrompt"), MAX_PROMPT).strip(), temperature=_num(d.get("temperature"), None, 0, 1),
                   maxTokens=_num(d.get("maxTokens"), 4096, 256, 64_000, integer=True))
    elif kind == "swarm":
        out.update(name=_line(d.get("name"), 48), entry=_line(d.get("entry"), 40), maxHandoffs=_num(d.get("maxHandoffs"), 8, 1, 50, integer=True),
                   maxIterations=_num(d.get("maxIterations"), 12, 1, 60, integer=True),
                   executionTimeout=_num(d.get("executionTimeout"), 300, 30, 900, integer=True), nodeTimeout=_num(d.get("nodeTimeout"), 120, 10, 900, integer=True))
    elif kind == "tool":
        out.update(tool=_line(d.get("tool"), 40), timezone=_line(d.get("timezone"), 64) or "Asia/Shanghai",
                   maxChars=_num(d.get("maxChars"), 12000, 1000, 100_000, integer=True))
    elif kind == "custom-tool":
        out["code"] = _clean(d.get("code"), MAX_CODE).strip("\n")
    elif kind == "kb":
        kb_type = _line(d.get("kbType"), 20).upper()
        out.update(kbId=_line(d.get("kbId"), 20), kbName=_line(d.get("kbName"), 100), kbType=kb_type if kb_type in ("MANAGED", "VECTOR") else "MANAGED",
                   toolName=_line(d.get("toolName"), 64), description=_clean(d.get("description"), 500).strip(),
                   topK=_num(d.get("topK"), 5, 1, 20, integer=True))
    return out


def normalize(flow: Any) -> dict[str, Any]:
    """The flow as it is kept and generated from: typed node data, positions, edges with their ports. Refuses only
    what is not a flow at all; everything else is :func:`validate`'s to explain."""
    if not isinstance(flow, Mapping):
        raise StudioError("flow: an object {nodes, edges, graphMode}")
    if len(json.dumps(flow, ensure_ascii=False, default=str)) > MAX_FLOW_BYTES:
        raise StudioError(f"flow: at most {MAX_FLOW_BYTES // 1000} KB")
    raw_nodes, raw_edges = flow.get("nodes") or [], flow.get("edges") or []
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise StudioError("flow: nodes and edges are lists")
    if len(raw_nodes) > MAX_NODES or len(raw_edges) > MAX_EDGES:
        raise StudioError(f"flow: at most {MAX_NODES} nodes and {MAX_EDGES} edges")
    nodes, seen = [], set()
    for raw in raw_nodes:
        if not isinstance(raw, Mapping):
            raise StudioError("flow: every node is an object")
        nid, kind = str(raw.get("id") or ""), str(raw.get("type") or "")
        if not NODE_ID.match(nid):
            raise StudioError(f"flow: node id {nid!r} (letters, digits, - and _, at most 40)")
        if nid in seen:
            raise StudioError(f"flow: two nodes are {nid}")
        if kind not in NODE_TYPES:
            raise StudioError(f"flow: node {nid} has an unknown type {kind!r}")
        seen.add(nid)
        data = raw.get("data") if isinstance(raw.get("data"), Mapping) else {}
        nodes.append({"id": nid, "type": kind, "x": _num(raw.get("x"), 0, -20_000, 20_000, integer=True),
                      "y": _num(raw.get("y"), 0, -20_000, 20_000, integer=True), "data": _node_data(kind, data)})
    edges, ids = [], set()
    for raw in raw_edges:
        if not isinstance(raw, Mapping):
            raise StudioError("flow: every edge is an object")
        edge = {k: _line(raw.get(k), 40) for k in ("source", "sourcePort", "target", "targetPort")}
        eid = _line(raw.get("id"), 60) or f"e-{edge['source']}-{edge['sourcePort']}-{edge['target']}-{edge['targetPort']}"
        while eid in ids:
            eid += "x"
        ids.add(eid)
        edges.append({"id": eid, **edge})
    return {"nodes": nodes, "edges": edges, "graphMode": flow.get("graphMode") is True}


def flow_hash(flow: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(flow, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def rule_for(source_type: str, source_port: str, target_type: str, target_port: str, graph: bool) -> tuple | None:
    for sources, sport, targets, tport, graph_only, kind in RULES:
        if source_type in sources and source_port == sport and target_type in targets and target_port == tport and (graph or not graph_only):
            return sources, sport, targets, tport, graph_only, kind
    return None


# -- custom tools ------------------------------------------------------------------------------------------------------

#: Packages whose submodules a custom tool may import too (``collections.abc``, ``botocore.exceptions``...).
SAFE_PACKAGES = frozenset({"collections", "typing", "boto3", "botocore", "httpx", "requests"})


def _module_ok(name: str) -> bool:
    return name in SAFE_MODULES or name.split(".", 1)[0] in SAFE_PACKAGES


TYPING_NAMES = frozenset({"Any", "Optional", "List", "Dict", "Tuple", "Set", "Union", "Literal"})
ANNOTATION_NODES = (ast.Name, ast.Subscript, ast.Constant, ast.BinOp, ast.BitOr, ast.Tuple, ast.List, ast.Attribute, ast.Load)


def _annotation_problems(node: ast.AST | None, imported: set[str], where: str) -> list[str]:
    """An annotation is evaluated when the module loads: only type names (and ``X[Y]``, ``X | None``, literals)."""
    if node is None:
        return []
    problems = []
    for sub in ast.walk(node):
        if not isinstance(sub, ANNOTATION_NODES) or (isinstance(sub, ast.BinOp) and not isinstance(sub.op, ast.BitOr)):
            return [f"{where}的类型注解只能是类型名（如 str、list[str]、str | None），不能是表达式"]
        if isinstance(sub, ast.Name) and sub.id not in imported:
            if sub.id in TYPING_NAMES:
                problems.append(f"{where}用了 {sub.id}：先写 from typing import {sub.id}")
            elif sub.id not in ANNOTATION_NAMES:
                problems.append(f"{where}的类型 {sub.id} 不认识：用 str、int、float、bool、list、dict（或先 import 它）")
    return problems


def check_tool(code: str) -> dict[str, Any]:
    """Check a custom tool's code without running it: one function (sync or async) with a docstring and type
    annotations, imports from :data:`SAFE_MODULES` only, nothing else at the top level, no dynamic execution, no
    dunder attributes. Returns ``{ok, problems, name, params, doc, imports, packages}``; ``problems`` are in
    Chinese with line numbers."""
    info: dict[str, Any] = {"ok": False, "problems": [], "name": None, "params": [], "doc": "", "imports": [], "packages": []}
    problems: list[str] = info["problems"]
    text = _clean(code)
    if not text.strip():
        problems.append("还没有代码：写一个带文档字符串的 Python 函数")
        return info
    if len(text) > MAX_CODE or text.count("\n") > 400:
        problems.append(f"代码太长（最多 {MAX_CODE} 个字符、400 行）")
        return info
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        problems.append(f"第 {exc.lineno} 行语法错误：{exc.msg}")
        return info
    functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    imported: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            continue  # a module docstring or a comment string
        problems.append(f"第 {stmt.lineno} 行：函数外只能有 import（工具在加载时不能执行别的代码）")
    if len(functions) != 1:
        problems.append("只能有一个函数（就是这个工具）" if functions else "没有找到函数：写一个 def 名字(参数: 类型) -> str:")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _module_ok(alias.name):
                    problems.append(f"第 {node.lineno} 行：不能 import {alias.name}（可用：{', '.join(sorted(m for m in SAFE_MODULES if '.' not in m))}）")
                info["imports"].append(alias.name)
                if node in tree.body:
                    imported.add((alias.asname or alias.name).split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level or not _module_ok(module):
                problems.append(f"第 {node.lineno} 行：不能 from {'.' * node.level}{module} import（可用的模块见上）")
            info["imports"].append(module)
            for alias in node.names:
                if alias.name == "*":
                    problems.append(f"第 {node.lineno} 行：不能 import *")
                elif node in tree.body:
                    imported.add(alias.asname or alias.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append(f"第 {node.lineno} 行：工具不能用 global / nonlocal（每次调用都应只依赖它的参数）")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            problems.append(f"第 {node.lineno} 行：不能调用 {node.func.id}()")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and not (
                len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
                and not node.args[1].value.startswith("_")):
            problems.append(f"第 {node.lineno} 行：getattr 的属性名要写成不以 _ 开头的字符串")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            problems.append(f"第 {node.lineno} 行：不能访问 {node.attr}")
        elif isinstance(node, ast.Name) and node.id.startswith("__") and node.id != "__name__":
            problems.append(f"第 {node.lineno} 行：不能使用 {node.id}")
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for alias in stmt.names:
                bound = alias.asname or (alias.name.split(".", 1)[0] if isinstance(stmt, ast.Import) else alias.name)
                same = isinstance(stmt, ast.Import) and alias.asname is None and bound in GENERATED_MODULES
                if (bound in RESERVED or bound.startswith("_")) and not same and bound != "*":
                    problems.append(f"第 {stmt.lineno} 行：import 进来的名字 {bound} 和生成的代码冲突：换一个别名（import … as …）")
    info["imports"] = sorted(set(info["imports"]))
    info["packages"] = sorted({EXTRA_PACKAGES[m.split(".", 1)[0]] for m in info["imports"] if m.split(".", 1)[0] in EXTRA_PACKAGES})
    if len(functions) != 1:
        return info
    fn = functions[0]
    info["name"], info["async"] = fn.name, isinstance(fn, ast.AsyncFunctionDef)
    if not TOOL_NAME.match(fn.name):
        problems.append(f"函数名 {fn.name} 要以英文字母开头，只用字母、数字和下划线（模型按这个名字调用它）")
    elif fn.name in RESERVED:
        problems.append(f"函数名 {fn.name} 和生成的代码或 Python 内置名冲突：换一个名字")
    if fn.decorator_list:
        problems.append(f"第 {fn.lineno} 行：不要加装饰器（生成代码时会加 @tool）")
    doc = ast.get_docstring(fn) or ""
    if not doc.strip():
        problems.append("函数要有文档字符串（\"\"\"做什么、什么时候用\"\"\"）：模型靠它决定什么时候调用这个工具")
    info["doc"] = doc.strip().split("\n\n", 1)[0].strip()[:300]
    args = fn.args
    if args.vararg or args.kwarg:
        problems.append("参数里不能有 *args / **kwargs：模型要知道每个参数的名字和类型")
    if args.posonlyargs:
        problems.append("参数里不能有仅位置参数（/）")
    params = [*args.args, *args.kwonlyargs]
    defaults = [None] * (len(args.args) - len(args.defaults)) + list(args.defaults) + list(args.kw_defaults)
    for arg, default in zip(params, defaults):
        if arg.annotation is None:
            problems.append(f"参数 {arg.arg} 需要类型注解（如 {arg.arg}: str）：模型靠它知道该传什么")
        problems += _annotation_problems(arg.annotation, imported, f"参数 {arg.arg} ")
        shown = None
        if default is not None:
            try:
                shown = ast.literal_eval(default)
            except (ValueError, SyntaxError):
                problems.append(f"参数 {arg.arg} 的默认值只能是常量（数字、字符串、True/False/None）")
        info["params"].append({"name": arg.arg, "type": ast.unparse(arg.annotation) if arg.annotation is not None else "",
                               **({"default": shown} if default is not None else {})})
    problems += _annotation_problems(fn.returns, imported, "返回值")
    for node in ast.walk(fn):
        if isinstance(node, (ast.Yield, ast.YieldFrom)) and _owner(fn, node) is fn:
            problems.append(f"第 {node.lineno} 行：工具要 return 结果，不能 yield")
            break
    info["ok"] = not problems
    return info


def _owner(fn: ast.AST, target: ast.AST) -> ast.AST | None:
    """The innermost function around ``target`` within ``fn``."""
    def walk(node: ast.AST, current: ast.AST) -> ast.AST | None:
        for child in ast.iter_child_nodes(node):
            if child is target:
                return current
            found = walk(child, child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) else current)
            if found is not None:
                return found
        return None
    return walk(fn, fn)


def kb_tool_name(data: Mapping[str, Any]) -> str:
    """The name the model calls a KB node's tool by: its own, else ``search_<KB name>`` (or the KB id)."""
    if data.get("toolName"):
        return str(data["toolName"])
    base = slug(data.get("kbName") or "", 50)
    return f"search_{base}" if base and base[0].isalpha() else f"search_kb_{str(data.get('kbId') or 'unset').lower()}"


# -- validation --------------------------------------------------------------------------------------------------------

class _Graph:
    """A normalized flow's relations, for validation and code generation."""

    def __init__(self, flow: Mapping[str, Any]):
        self.flow = flow
        self.graph = bool(flow.get("graphMode"))
        self.nodes = {n["id"]: n for n in flow["nodes"]}
        self.order = [n["id"] for n in flow["nodes"]]
        self.edges: list[dict[str, Any]] = []
        self.bad: list[tuple[dict[str, Any], str]] = []
        seen = set()
        for e in flow["edges"]:
            s, t = self.nodes.get(e["source"]), self.nodes.get(e["target"])
            if not s or not t:
                self.bad.append((e, "连线的一端节点不存在"))
                continue
            key = (e["source"], e["sourcePort"], e["target"], e["targetPort"])
            if e["source"] == e["target"]:
                self.bad.append((e, f"「{s['data']['label']}」不能连到它自己"))
                continue
            rule = rule_for(s["type"], e["sourcePort"], t["type"], e["targetPort"], self.graph)
            if not rule:
                graph_rule = rule_for(s["type"], e["sourcePort"], t["type"], e["targetPort"], True)
                why = ("Agent 之间的连线只在 Graph 模式下有意义（表示先后依赖）：打开 Graph 模式，或改连到输出" if graph_rule
                       else f"{TYPE_LABEL[s['type']]}「{s['data']['label']}」的 {e['sourcePort']} 不能连到 {TYPE_LABEL[t['type']]}「{t['data']['label']}」的 {e['targetPort']}")
                self.bad.append((e, why))
                continue
            if key in seen:
                self.bad.append((e, "这条连线重复了"))
                continue
            seen.add(key)
            self.edges.append({**e, "kind": rule[5]})

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [self.nodes[i] for i in self.order if self.nodes[i]["type"] == kind]

    def out(self, nid: str, kind: str) -> list[str]:
        return [e["target"] for e in self.edges if e["source"] == nid and e["kind"] == kind]

    def into(self, nid: str, kind: str) -> list[str]:
        return [e["source"] for e in self.edges if e["target"] == nid and e["kind"] == kind]

    def members(self, nid: str) -> list[str]:
        return self.out(nid, "member")

    def parents(self, nid: str) -> list[str]:
        return self.into(nid, "member")

    def tools(self, nid: str) -> list[str]:
        return self.into(nid, "tool")

    def label(self, nid: str) -> str:
        node = self.nodes[nid]
        return node["data"].get("label") or TYPE_LABEL[node["type"]]

    def descendants(self, roots: Iterable[str]) -> list[str]:
        """The roots and everything under them (sub-agents, swarm members), children first."""
        out: list[str] = []

        def visit(nid: str, path: tuple[str, ...]) -> None:
            if nid in path or nid in out:
                return
            for child in self.members(nid):
                visit(child, path + (nid,))
            out.append(nid)

        for root in roots:
            visit(root, ())
        return out


def _cycle(nodes: Sequence[str], next_of) -> list[str] | None:
    """A cycle in the relation ``next_of`` among ``nodes``, if there is one."""
    color: dict[str, int] = {}

    def dfs(nid: str, path: list[str]) -> list[str] | None:
        color[nid] = 1
        for nxt in next_of(nid):
            if color.get(nxt) == 1:
                return path[path.index(nxt):] + [nxt] if nxt in path else [nid, nxt]
            if not color.get(nxt):
                found = dfs(nxt, path + [nxt])
                if found:
                    return found
        color[nid] = 2
        return None

    for nid in nodes:
        if not color.get(nid):
            found = dfs(nid, [nid])
            if found:
                return found
    return None


def validate(flow: Mapping[str, Any]) -> dict[str, Any]:
    """Every reason the flow cannot run (``error``) or may not do what it seems to (``warning``), tied to its node or
    edge, plus what each custom tool was read as. ``ok`` when there are no errors."""
    g = _Graph(flow)
    issues: list[dict[str, Any]] = []

    def add(level: str, message: str, node: str | None = None, edge: str | None = None) -> None:
        issues.append({"level": level, "message": message, "node": node, "edge": edge})

    for e, why in g.bad:
        add("error", why, edge=e["id"])
    inputs, outputs = g.of("input"), g.of("output")
    executables = [n for n in flow["nodes"] if n["type"] in EXECUTABLE]
    toolish = [n for n in flow["nodes"] if n["type"] in TOOLISH]
    if len(inputs) != 1:
        add("error", "需要恰好一个「输入」节点：它代表用户发来的问题" if not inputs else "只能有一个「输入」节点（用户每次只发一个问题）",
            node=inputs[1]["id"] if len(inputs) > 1 else None)
    if len(outputs) != 1:
        add("error", "需要恰好一个「输出」节点：连到给出最终回答的 Agent" if not outputs else "只能有一个「输出」节点",
            node=outputs[1]["id"] if len(outputs) > 1 else None)
    if not executables:
        add("error", "还没有 Agent：从左边拖一个 Agent（或编排 Agent、Swarm）进来")
    if len(executables) > MAX_EXECUTABLES:
        add("error", f"Agent 太多了（最多 {MAX_EXECUTABLES} 个）")
    if len(toolish) > MAX_TOOLS:
        add("error", f"工具太多了（最多 {MAX_TOOLS} 个）")

    # -- each node on its own --------------------------------------------------------------------------------------
    names: dict[str, str] = {}
    known_models = {m for m, _ in MODELS}
    for n in executables:
        d, nid = n["data"], n["id"]
        if not IDENT.match(d.get("name") or ""):
            add("error", f"「{d['label']}」需要一个英文名称（字母开头，字母、数字、下划线）：代码、子 Agent 工具和 Graph 都用它", node=nid)
        elif d["name"] in names:
            add("error", f"名称 {d['name']} 重复了（「{g.label(names[d['name']])}」也叫这个）：每个 Agent 的名称要不同", node=nid)
        else:
            names[d["name"]] = nid
        if n["type"] == "swarm":
            continue
        if not MODEL_ID.match(d.get("model") or ""):
            add("error", f"「{d['label']}」要选一个模型", node=nid)
        elif d["model"] not in known_models:
            add("warning", f"「{d['label']}」的模型 {d['model']} 不在常用列表里：确认这个工作区的区域开通了它", node=nid)
        if not d.get("systemPrompt"):
            add("error", f"「{d['label']}」的系统 Prompt 是空的：写清楚它是谁、做什么", node=nid)
    tool_info: dict[str, dict[str, Any]] = {}
    function_names: dict[str, str] = {}
    locals_ = {_var(n): n["id"] for n in executables if IDENT.match(n["data"].get("name") or "")}  # build()'s agent_<name> / swarm_<name>
    for n in toolish:
        d, nid = n["data"], n["id"]
        if not g.out(nid, "tool"):
            add("warning", f"{TYPE_LABEL[n['type']]}「{d['label']}」没有连到任何 Agent，不会被用到", node=nid)
        if n["type"] == "tool":
            if d.get("tool") not in BUILTIN_TOOLS:
                add("error", f"「{d['label']}」要选一个内置工具（{'、'.join(t['label'] for t in BUILTIN_TOOLS.values())}）", node=nid)
        elif n["type"] == "custom-tool":
            info = check_tool(d.get("code") or "")
            tool_info[nid] = info
            for problem in info["problems"]:
                add("error", f"自定义工具「{d['label']}」：{problem}", node=nid)
            if info.get("name"):
                if info["name"] in function_names:
                    add("error", f"工具名 {info['name']} 已经被「{g.label(function_names[info['name']])}」用了：函数名要不同", node=nid)
                elif info["name"] in locals_:
                    add("error", f"函数名 {info['name']} 和生成代码里「{g.label(locals_[info['name']])}」的变量同名（Agent 会拿到那个变量而不是这个工具）："
                                 "换一个名字", node=nid)
                function_names[info["name"]] = nid
        elif n["type"] == "kb":
            if not KB_ID.match(d.get("kbId") or ""):
                add("error", f"「{d['label']}」要选一个知识库", node=nid)
            name = kb_tool_name(d)
            if not TOOL_NAME.match(name):
                add("error", f"「{d['label']}」的工具名 {name} 要以英文字母开头，只用字母、数字和下划线", node=nid)
            elif name in RESERVED:
                add("error", f"「{d['label']}」的工具名 {name} 和生成的代码冲突：换一个", node=nid)
            elif name in function_names:
                add("error", f"工具名 {name} 已经被「{g.label(function_names[name])}」用了", node=nid)
            function_names[name] = nid

    # -- tools per agent ------------------------------------------------------------------------------------------------
    for n in executables:
        if n["type"] == "swarm":
            continue
        seen: dict[str, str] = {}
        offered = [(g.nodes[t], _tool_name(g.nodes[t], tool_info)) for t in g.tools(n["id"])]
        offered += [(g.nodes[m], g.nodes[m]["data"].get("name") or "") for m in g.members(n["id"])]
        for node, name in offered:
            if not name:
                continue
            if name in seen and seen[name] != node["id"]:
                add("error", f"「{n['data']['label']}」有两个工具都叫 {name}（「{g.label(seen[name])}」和「{node['data']['label']}」）", node=n["id"])
            seen[name] = node["id"]

    # -- membership: sub-agents and swarm members ------------------------------------------------------------------
    for n in executables:
        nid, d = n["id"], n["data"]
        parents = g.parents(nid)
        if len(parents) > 1:
            add("error", f"「{d['label']}」同时属于 {'、'.join('「' + g.label(p) + '」' for p in parents)}：一个 Agent 只能有一个上级", node=nid)
        if parents:
            if g.into(nid, "input"):
                add("error", f"「{d['label']}」是「{g.label(parents[0])}」的成员，不能直接接输入：输入要连到上级", node=nid)
            if g.out(nid, "output"):
                add("error", f"「{d['label']}」是「{g.label(parents[0])}」的成员，不能直接连输出：让上级连到输出", node=nid)
            if g.into(nid, "dependency") or g.out(nid, "dependency"):
                add("error", f"「{d['label']}」是「{g.label(parents[0])}」的成员，不能再作为 Graph 的节点", node=nid)
            if not d.get("description") and g.nodes[parents[0]]["type"] == "orchestrator":
                add("warning", f"子 Agent「{d['label']}」没有职责说明：编排 Agent 靠它决定什么时候找它（现在用系统 Prompt 的开头代替）", node=nid)
        if n["type"] == "orchestrator" and not g.members(nid):
            add("error", f"编排 Agent「{d['label']}」还没有子 Agent：把它的「子 Agent」端口连到 Agent 的「上级」端口", node=nid)
        if n["type"] == "swarm":
            members = g.members(nid)
            if len(members) < 2:
                add("error", f"Swarm「{d['label']}」至少要两个成员才能互相交接（现在 {len(members)} 个）", node=nid)
            if d.get("entry") and d["entry"] not in members:
                add("error", f"Swarm「{d['label']}」的入口不是它的成员：重新选一个入口", node=nid)
            for m in members:
                if not g.nodes[m]["data"].get("description"):
                    add("warning", f"Swarm 成员「{g.label(m)}」没有职责说明：别的成员靠它决定交接给谁", node=m)
    loop = _cycle([n["id"] for n in executables if n["type"] == "orchestrator"], lambda nid: [m for m in g.members(nid) if g.nodes[m]["type"] == "orchestrator"])
    if loop:
        add("error", "编排关系绕成了一个圈：" + " → ".join(f"「{g.label(x)}」" for x in loop), node=loop[0])

    # -- the shape of the run ------------------------------------------------------------------------------------------
    roots = [n["id"] for n in executables if not g.parents(n["id"])]
    the_input = inputs[0]["id"] if len(inputs) == 1 else None
    the_output = outputs[0]["id"] if len(outputs) == 1 else None
    if not g.graph:
        entries = g.out(the_input, "input") if the_input else []
        answers = g.into(the_output, "output") if the_output else []
        if the_input and not entries and executables:
            add("error", "「输入」还没连到 Agent：把输入连到接收用户问题的 Agent", node=the_input)
        if len(entries) > 1:
            add("error", "「输入」连了多个 Agent：不是 Graph 模式时只有一个 Agent 接收问题（要多个 Agent 协作，用编排 Agent、Swarm 或打开 Graph 模式）",
                node=the_input)
        if the_output and not answers and executables:
            add("error", "「输出」还没有连接：把给出最终回答的 Agent 连到输出", node=the_output)
        if len(answers) > 1:
            add("error", "「输出」连了多个 Agent：不是 Graph 模式时只能有一个 Agent 给出回答", node=the_output)
        if len(entries) == 1 and len(answers) == 1 and entries[0] != answers[0]:
            add("error", f"接收问题的「{g.label(entries[0])}」和给出回答的「{g.label(answers[0])}」不是同一个："
                         "不是 Graph 模式时它们得是同一个（要先后执行，打开 Graph 模式并把它们连起来）", node=answers[0])
        if len(entries) == 1:
            running = set(g.descendants(entries))
            for n in executables:
                if n["id"] not in running and not g.parents(n["id"]):
                    add("warning", f"「{n['data']['label']}」不在这个流程里（没有接到输入那条线上），不会运行", node=n["id"])
    else:
        deps = {nid: g.into(nid, "dependency") for nid in roots}
        loop = _cycle(roots, lambda nid: g.out(nid, "dependency"))
        if loop:
            add("error", "Graph 里有环：" + " → ".join(f"「{g.label(x)}」" for x in loop) + "（Graph 的依赖必须是无环的）", node=loop[0])
        entries = [nid for nid in roots if not deps[nid]]
        fed = set(g.out(the_input, "input")) if the_input else set()
        for nid in entries:
            if nid not in fed:
                add("error", f"「{g.label(nid)}」没有上游，是 Graph 的入口：把「输入」连到它（入口会拿到用户的问题）", node=nid)
        for nid in fed:
            if nid in deps and deps[nid]:
                add("error", f"「{g.label(nid)}」有上游依赖，不能再直接接输入：只有入口接输入", node=nid)
        answers = g.into(the_output, "output") if the_output else []
        if the_output and not answers and roots:
            add("error", "「输出」还没有连接：把给出最终结果的节点连到输出（可以连多个，结果按执行顺序拼起来）", node=the_output)
        upstream: set[str] = set()
        stack = list(answers)
        while stack:
            nid = stack.pop()
            if nid not in upstream:
                upstream.add(nid)
                stack += g.into(nid, "dependency")
        for nid in roots:
            if answers and nid not in upstream:
                add("warning", f"「{g.label(nid)}」的结果不会出现在回答里（它到不了输出）", node=nid)
    order = {nid: i for i, nid in enumerate(g.order)}
    issues.sort(key=lambda i: (i["level"] != "error", order.get(i["node"] or "", -1)))
    return {"ok": not any(i["level"] == "error" for i in issues), "issues": issues, "tools": tool_info}


def _tool_name(node: Mapping[str, Any], tool_info: Mapping[str, Mapping[str, Any]]) -> str:
    if node["type"] == "tool":
        return BUILTIN_TOOLS.get(node["data"].get("tool"), {}).get("name") or ""
    if node["type"] == "custom-tool":
        return str((tool_info.get(node["id"]) or {}).get("name") or "")
    return kb_tool_name(node["data"])


# -- code generation ------------------------------------------------------------------------------------------------

def _var(node: Mapping[str, Any]) -> str:
    return f"{'swarm' if node['type'] == 'swarm' else 'agent'}_{node['data']['name']}"


def _kb_func(node: Mapping[str, Any]) -> str:
    return f"_kb_{slug(node['id'], 30) or 'tool'}"


def _plan(flow: Mapping[str, Any], kbs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    g = _Graph(flow)
    checked = validate(flow)
    roots_all = [n["id"] for n in flow["nodes"] if n["type"] in EXECUTABLE and not g.parents(n["id"])]
    the_input, the_output = g.of("input")[0], g.of("output")[0]
    if g.graph:
        graph_nodes = roots_all
        build = g.descendants(graph_nodes)
        kind = "graph"
        entries = [nid for nid in graph_nodes if not g.into(nid, "dependency")]
        outputs = g.into(the_output["id"], "output")
        entry = None
    else:
        entry = g.out(the_input["id"], "input")[0]
        build = g.descendants([entry])
        kind = "swarm" if g.nodes[entry]["type"] == "swarm" else "agent"
        graph_nodes, entries, outputs = [], [], []
    used_tools: list[str] = []  # tool node ids in first-use order
    for nid in build:
        for t in g.tools(nid):
            if t not in used_tools:
                used_tools.append(t)
    kb_nodes = [g.nodes[t] for t in used_tools if g.nodes[t]["type"] == "kb"]
    resolved = {}
    for node in kb_nodes:
        found = kbs.get(node["data"]["kbId"]) or {}
        resolved[node["id"]] = {"type": found.get("type") or node["data"].get("kbType") or "MANAGED",
                                "name": found.get("name") or node["data"].get("kbName") or node["data"]["kbId"], "description": found.get("description") or ""}
    return {"g": g, "kind": kind, "entry": entry, "build": build, "graph_nodes": graph_nodes, "entries": entries, "outputs": outputs,
            "used_tools": used_tools, "kb_nodes": kb_nodes, "kb_resolved": resolved, "tool_info": checked["tools"],
            "builtins": [g.nodes[t]["data"]["tool"] for t in used_tools if g.nodes[t]["type"] == "tool"],
            "custom": [g.nodes[t] for t in used_tools if g.nodes[t]["type"] == "custom-tool"],
            "trace": bool(the_output["data"].get("traceTools")), "input": the_input, "output": the_output}


def _tool_expr(node: Mapping[str, Any], plan: Mapping[str, Any]) -> str:
    if node["type"] == "tool":
        return BUILTIN_TOOLS[node["data"]["tool"]]["symbol"]
    if node["type"] == "custom-tool":
        return str(plan["tool_info"][node["id"]]["name"])
    return _kb_func(node)


def _sub_description(node: Mapping[str, Any]) -> str:
    d = node["data"]
    text = d.get("description") or f"{d['label']}：{_line(d.get('systemPrompt'), 200)}"
    return _clean(text, 500)


def _render_agent(node: Mapping[str, Any], plan: Mapping[str, Any]) -> list[str]:
    g, d = plan["g"], node["data"]
    tools = [_tool_expr(g.nodes[t], plan) for t in g.tools(node["id"])]
    tools += [f"{_var(g.nodes[m])}.as_tool(name={py_str(g.nodes[m]['data']['name'])}, description={py_str(_sub_description(g.nodes[m]))})"
              for m in g.members(node["id"])]
    model = [f"model_id={py_str(d['model'])}", "region_name=REGION", f"max_tokens={int(d['maxTokens'])}"]
    if d.get("temperature") is not None:
        model.append(f"temperature={float(d['temperature'])}")
    lines = [f"    # {TYPE_LABEL[node['type']]}「{_line(d['label'], 40)}」",
             f"    {_var(node)} = Agent(",
             f"        name={py_str(d['name'])},"]
    if d.get("description"):
        lines.append(f"        description={py_str(d['description'])},")
    lines += [f"        model=BedrockModel({', '.join(model)}),",
              f"        system_prompt={py_str(d['systemPrompt'])},"]
    if tools:
        if len(", ".join(tools)) <= 80:
            lines.append(f"        tools=[{', '.join(tools)}],")
        else:
            lines.append("        tools=[")
            lines += [f"            {t}," for t in tools]
            lines.append("        ],")
    lines += ["        callback_handler=None,", "        hooks=[trace],", "    )"]
    return lines


def _render_swarm(node: Mapping[str, Any], plan: Mapping[str, Any]) -> list[str]:
    g, d = plan["g"], node["data"]
    members = g.members(node["id"])
    entry = d.get("entry") if d.get("entry") in members else members[0]
    return [f"    # Swarm「{_line(d['label'], 40)}」：成员之间可以交接（handoff_to_agent）",
            f"    {_var(node)} = Swarm(",
            f"        [{', '.join(_var(g.nodes[m]) for m in members)}],",
            f"        entry_point={_var(g.nodes[entry])},",
            f"        max_handoffs={int(d['maxHandoffs'])},",
            f"        max_iterations={int(d['maxIterations'])},",
            f"        execution_timeout={float(d['executionTimeout'])},",
            f"        node_timeout={float(d['nodeTimeout'])},",
            f"        id={py_str(d['name'])},",
            "    )"]


RUNTIME_HELPERS = '''
# -- one turn ---------------------------------------------------------------------------------------------------------
class _Trace(HookProvider):
    """Every tool call of a turn, by any agent of the flow (sub-agents and swarm members included)."""

    def __init__(self):
        self.calls = []

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(AfterToolCallEvent, self._after)

    def _after(self, event) -> None:
        result = event.result if isinstance(event.result, dict) else {}
        self.calls.append({"agent": getattr(event.agent, "name", None), "tool": (event.tool_use or {}).get("name"),
                           "ok": event.exception is None and result.get("status") != "error"})
'''

ENTRYPOINT = '''
app = BedrockAgentCoreApp()
_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()


def _session(session_id):
    """The flow's agents for one session: an AgentCore session is one microVM, so this is usually the only one."""
    with _SESSIONS_LOCK:
        found = _SESSIONS.get(session_id)
        if found is None:
            if len(_SESSIONS) >= MAX_SESSIONS:
                _SESSIONS.pop(next(iter(_SESSIONS)))
            entry, trace = build()
            found = _SESSIONS[session_id] = {"entry": entry, "trace": trace, "history": [], "lock": threading.Lock()}
        return found


def _footer(calls):
    if not calls:
        return "\\n\\n〔这一轮没有调用工具〕"
    counted = {}
    for call in calls:
        key = (call.get("agent") or "agent", call.get("tool") or "?", call.get("ok", True))
        counted[key] = counted.get(key, 0) + 1
    parts = [f"{agent}·{name}{'×' + str(n) if n > 1 else ''}{'' if ok else '（失败）'}" for (agent, name, ok), n in counted.items()]
    return "\\n\\n〔工具〕" + "，".join(parts)


def _error(body, status):
    return Response(json.dumps(body, ensure_ascii=False), status_code=status, media_type="application/json")


@app.entrypoint
def invoke(payload, context=None):
    """One turn: {"prompt": ...} → the reply as text/plain ({"format": "json"}: {"result", "tools", "seconds", "flow"})."""
    payload = payload if isinstance(payload, dict) else {"prompt": payload}
    prompt = str(payload.get("prompt") or payload.get("message") or "").strip()
    if not prompt:
        return _error({"error": "the payload needs a non-empty prompt"}, 400)
    session_id = str(getattr(context, "session_id", None) or payload.get("sessionId") or "local")
    session = _session(session_id)
    started = time.time()
    with session["lock"]:
        session["trace"].calls.clear()
        try:
            answer = _run(session, prompt)
        except Exception as exc:  # noqa: BLE001 - one line to find in the log (AgentCore drops a 500's body), then the traceback
            failed = {"error": f"{type(exc).__name__}: {exc}"[:2000], "tools": list(session["trace"].calls)}
            logger.error("turn failed %s", json.dumps({"session": session_id[:16], **failed}, ensure_ascii=False))
            logger.exception("traceback")
            return _error(failed, 500)
        calls = list(session["trace"].calls)
    seconds = round(time.time() - started, 2)
    logger.info("turn %s", json.dumps({"session": session_id[:16], "seconds": seconds, "tools": [c["tool"] for c in calls]}, ensure_ascii=False))
    if str(payload.get("format") or "").lower() == "json":
        return {"result": answer, "tools": calls, "seconds": seconds, "flow": FLOW}
    trace = payload.get("trace")
    if TRACE_TOOLS if trace is None else bool(trace):
        answer += _footer(calls)
    return Response(answer, media_type="text/plain; charset=utf-8")


if __name__ == "__main__":
    app.run()
'''

RUN_AGENT = '''

def _run(session, prompt):
    """An Agent keeps the session's conversation itself."""
    return str(session["entry"](prompt)).strip()
'''

RUN_MULTI = '''

def _with_history(history, prompt):
    """A Swarm or Graph starts every run afresh: the session's last turns go in front of the question."""
    if not history:
        return prompt
    turns = "\\n\\n".join(f"用户：{question}\\n助手：{answer[:1500]}" for question, answer in history[-HISTORY_TURNS:])
    return f"之前的对话：\\n{turns}\\n\\n用户现在说：{prompt}"


def _run(session, prompt):
    result = session["entry"](_with_history(session["history"], prompt))
    answer = _answer(result)
    session["history"].append((prompt, answer))
    del session["history"][:-HISTORY_TURNS]
    return answer
'''

ANSWER_SWARM = '''

def _answer(result):
    """A Swarm's answer: what its last member said."""
    for node in reversed(list(getattr(result, "node_history", None) or [])):
        found = (result.results or {}).get(node.node_id)
        if found is not None and not isinstance(found.result, Exception):
            text = str(found.result).strip()
            if text:
                return text
    status = getattr(getattr(result, "status", None), "value", "?")
    raise RuntimeError(f"the swarm gave no answer (status {status})")
'''

ANSWER_GRAPH = '''

def _answer(result):
    """A Graph's answer: the results of the nodes connected to the Output, in the order they ran."""
    ran = [node.node_id for node in getattr(result, "execution_order", None) or []]
    parts = []
    for node_id in [n for n in ran if n in OUTPUTS] + [n for n in OUTPUTS if n not in ran]:
        found = (result.results or {}).get(node_id)
        if found is not None and not isinstance(found.result, Exception):
            text = str(found.result).strip()
            if text:
                parts.append((node_id, text))
    if not parts:
        status = getattr(getattr(result, "status", None), "value", "?")
        raise RuntimeError(f"the graph gave no answer (status {status}, {getattr(result, 'completed_nodes', 0)}/{getattr(result, 'total_nodes', 0)} nodes done)")
    if len(parts) == 1:
        return parts[0][1]
    return "\\n\\n".join(f"【{LABELS.get(node_id, node_id)}】\\n{text}" for node_id, text in parts)
'''

HTTP_HELPER = '''
# -- HTTP 请求: a web page reaches the model as text, at most HTTP_MAX_CHARS characters -------------------------------
_TAGS = re.compile(r"<(script|style|noscript|svg)\\b[^>]*>.*?</\\1\\s*>|<!--.*?-->|<[^>]+>", re.S | re.I)


@tool(name="http_request")
def _http_request(method: str, url: str, headers: dict | None = None, body: str | None = None) -> str:
    """发一个 HTTP 请求，返回状态码和响应正文（网页会转成纯文本并截断）。读网页、调用 REST API 时用。

    Args:
        method: HTTP 方法：GET、POST、PUT、PATCH、DELETE 或 HEAD
        url: 完整的 http:// 或 https:// 地址
        headers: 可选的请求头，例如 {"Accept": "application/json"}
        body: 可选的请求体字符串（例如 JSON）
    """
    method = str(method or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD") or not str(url).lower().startswith(("http://", "https://")):
        return "只支持 GET、POST、PUT、PATCH、DELETE、HEAD 和 http(s) 地址"
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as http:
            response = http.request(method, str(url), headers=headers or None, content=body)
    except httpx.HTTPError as exc:  # raised: the call is marked failed, and the model reads why
        raise RuntimeError(f"请求失败：{type(exc).__name__}: {exc}") from exc
    text = response.text
    if "html" in response.headers.get("content-type", "").lower():
        text = re.sub(r"\\s+", " ", html.unescape(_TAGS.sub(" ", text))).strip()
    more = f"\\n…（正文共 {len(text)} 个字符，这里只有前 {HTTP_MAX_CHARS} 个）" if len(text) > HTTP_MAX_CHARS else ""
    return f"HTTP {response.status_code} {response.reason_phrase}\\n{text[:HTTP_MAX_CHARS]}{more}"
'''

KB_HELPERS = '''
# -- knowledge bases: Retrieve (a managed KB takes managedSearchConfiguration, a vector KB vectorSearchConfiguration) ---
_KB_CLIENT = None
_KB_LOCK = threading.Lock()


def _kb_client():
    global _KB_CLIENT
    with _KB_LOCK:
        if _KB_CLIENT is None:
            _KB_CLIENT = boto3.client("bedrock-agent-runtime", region_name=REGION)
        return _KB_CLIENT


def _retrieve(kb_id, query, top_k, managed):
    search = "managedSearchConfiguration" if managed else "vectorSearchConfiguration"
    try:
        out = _kb_client().retrieve(knowledgeBaseId=kb_id, retrievalQuery={"text": str(query)[:1000]},
                                    retrievalConfiguration={search: {"numberOfResults": int(top_k)}})
    except Exception as exc:  # noqa: BLE001 - raised: the call is marked failed, and the model reads why
        logger.warning("retrieve %s failed: %s", kb_id, exc)
        raise RuntimeError(f"知识库 {kb_id} 检索失败：{type(exc).__name__}: {str(exc)[:300]}") from exc
    hits = []
    for index, found in enumerate(out.get("retrievalResults") or [], 1):
        text = ((found.get("content") or {}).get("text") or "").strip()
        where = found.get("location") or {}
        uri = ((where.get("s3Location") or {}).get("uri") or (where.get("webLocation") or {}).get("url")
               or (where.get("confluenceLocation") or {}).get("url") or (where.get("customDocumentLocation") or {}).get("id") or "")
        score = found.get("score")
        head = f"[{index}] 来源 {uri.rsplit('/', 1)[-1] or '未知'}" + (f"，相关度 {score:.2f}" if isinstance(score, (int, float)) else "")
        hits.append(f"{head}\\n{text}")
    return "\\n\\n".join(hits) if hits else "知识库里没有找到相关内容。"
'''


def _render_main(plan: Mapping[str, Any], *, project: Mapping[str, Any] | None, version: int | None, generated_at: str) -> str:
    g, kind = plan["g"], plan["kind"]
    name = (project or {}).get("name") or "Studio 流程"
    if kind == "graph":
        mode = "Graph（按依赖先后运行）"
    elif kind == "swarm":
        mode = "Swarm（成员之间交接）"
    else:
        mode = "单个 Agent" if g.nodes[plan["entry"]]["type"] == "agent" else "编排 Agent（子 Agent 作为工具）"
    doc = (f"{_line(name, 60)} — a Strands agent generated by ADLC Console Studio.\n\n"
           f"Project {(project or {}).get('id') or '-'} · flow version {version or '-'} · generated {generated_at} · {mode}\n\n"
           "On AgentCore Runtime (direct code deploy, Python 3.13, ARM64), POST /invocations with {\"prompt\": \"...\"} answers the reply as\n"
           "text/plain; {\"format\": \"json\"} answers {\"result\", \"tools\", \"seconds\", \"flow\"}. Locally:\n\n"
           "    pip install -r requirements.txt && python main.py\n"
           "    curl -s localhost:8080/invocations -H 'Content-Type: application/json' -d '{\"prompt\": \"你好\"}'\n\n"
           "Change the flow in Studio rather than this file: every deployment generates it again.\n")
    http = "http_request" in plan["builtins"]
    std = {"json", "logging", "os", "threading", "time"} | ({"html", "re"} if http else set())
    out: list[str] = [py_str(doc), *[f"import {m}" for m in sorted(std)], ""]
    out += sorted((["import boto3"] if plan["kb_nodes"] else []) + (["import httpx"] if http else []))
    out += ["from bedrock_agentcore.runtime import BedrockAgentCoreApp", "from starlette.responses import Response",
            "from strands import Agent, tool", "from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry",
            "from strands.models import BedrockModel"]
    multi = [n for n in ("Swarm",) if any(g.nodes[b]["type"] == "swarm" for b in plan["build"])]
    if kind == "graph":
        multi.append("GraphBuilder")
    if multi:
        out.append(f"from strands.multiagent import {', '.join(sorted(multi))}")
    for imp in sorted({i for t in plan["builtins"] for i in BUILTIN_TOOLS[t]["imports"]}):
        out.append(imp)
    flow_meta = {"project": (project or {}).get("id"), "name": name, "version": version, "mode": kind}
    out += ["", 'logger = logging.getLogger("studio")  # its own handler: bedrock_agentcore logs through its own',
            "logger.setLevel(logging.INFO)", "logger.propagate = False", "if not logger.handlers:",
            '    logger.addHandler(logging.StreamHandler())',
            '    logger.handlers[0].setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))',
            'REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"',
            f"FLOW = {py_dict(flow_meta)}",
            f"TRACE_TOOLS = {plan['trace']!r}  # 输出节点「附上工具调用」：回答末尾列出这一轮调用的工具",
            "MAX_SESSIONS = 8"]
    if kind != "agent":
        out.append("HISTORY_TURNS = 6")
    if kind == "graph":
        out.append(f"OUTPUTS = {py_list([g.nodes[o]['data']['name'] for o in plan['outputs']])}")
        out.append(f"LABELS = {py_dict({g.nodes[n]['data']['name']: g.nodes[n]['data']['label'] for n in plan['graph_nodes']})}")
    timezones = [g.nodes[t]["data"].get("timezone") for t in plan["used_tools"] if g.nodes[t]["type"] == "tool" and g.nodes[t]["data"]["tool"] == "current_time"]
    if timezones:
        out.append(f"os.environ.setdefault(\"DEFAULT_TIMEZONE\", {py_str(timezones[0] or 'Asia/Shanghai')})  # current_time 的默认时区")
    if http:
        limit = max(int(g.nodes[t]["data"].get("maxChars") or 12000) for t in plan["used_tools"]
                    if g.nodes[t]["type"] == "tool" and g.nodes[t]["data"]["tool"] == "http_request")
        out.append(f"HTTP_MAX_CHARS = {limit}  # HTTP 请求返回给模型的最多字符数")
    if plan["custom"]:
        out += ["", "", "# -- custom tools (自定义工具 nodes, as written; @tool added) " + "-" * 50]
        for node in plan["custom"]:
            code = node["data"]["code"].strip("\n")
            tree = ast.parse(code)
            fn = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
            lines = code.split("\n")
            before, rest = lines[:fn.lineno - 1], lines[fn.lineno - 1:]
            out += ["", f"# 自定义工具「{_line(node['data']['label'], 40)}」"] + before + ["@tool"] + rest
    if http:
        out += ["", HTTP_HELPER.rstrip("\n")]
    if plan["kb_nodes"]:
        out += ["", KB_HELPERS.rstrip("\n")]
        for node in plan["kb_nodes"]:
            d, r = node["data"], plan["kb_resolved"][node["id"]]
            what = d.get("description") or (f"在知识库「{r['name']}」中检索相关原文" + (f"：{r['description']}" if r["description"] else "") +
                                            "。回答涉及这些内容时先检索，按检索到的原文回答并说明来源。")
            out += ["", "",
                    f"@tool(name={py_str(kb_tool_name(d))}, description={py_str(_clean(what, 800))})",
                    f"def {_kb_func(node)}(query: str) -> str:",
                    '    """在知识库里检索相关原文。',
                    "",
                    "    Args:",
                    "        query: 要检索的问题或关键词（用用户的原话或其中的关键概念）",
                    '    """',
                    f"    return _retrieve({py_str(d['kbId'])}, query, {int(d['topK'])}, managed={r['type'] != 'VECTOR'})"]
    out += ["", RUNTIME_HELPERS.rstrip("\n"), "", "",
            "def build():",
            '    """The flow\'s agents, built for one session (children before the agents that use them)."""',
            "    trace = _Trace()"]
    for nid in plan["build"]:
        node = g.nodes[nid]
        out += _render_swarm(node, plan) if node["type"] == "swarm" else _render_agent(node, plan)
    if kind == "graph":
        out += ["    # Graph：节点按依赖先后运行，入口拿到用户的问题", "    builder = GraphBuilder()"]
        for nid in plan["graph_nodes"]:
            out.append(f"    builder.add_node({_var(g.nodes[nid])}, {py_str(g.nodes[nid]['data']['name'])})")
        for e in g.edges:
            if e["kind"] == "dependency":
                out.append(f"    builder.add_edge({py_str(g.nodes[e['source']]['data']['name'])}, {py_str(g.nodes[e['target']]['data']['name'])})")
        for nid in plan["entries"]:
            out.append(f"    builder.set_entry_point({py_str(g.nodes[nid]['data']['name'])})")
        out += ["    builder.set_execution_timeout(300.0)", "    builder.set_node_timeout(180.0)", "    return builder.build(), trace"]
    else:
        out.append(f"    return {_var(g.nodes[plan['entry']])}, trace")
    out.append(RUN_AGENT.rstrip("\n") if kind == "agent" else (RUN_MULTI + (ANSWER_SWARM if kind == "swarm" else ANSWER_GRAPH)).rstrip("\n"))
    out += ["", ENTRYPOINT.rstrip("\n")]
    return "\n".join(out) + "\n"


def py_dict(value: Mapping[str, Any]) -> str:
    return "{" + ", ".join(f"{py_str(str(k))}: {py_value(v)}" for k, v in value.items()) + "}"


def py_list(values: Sequence[Any]) -> str:
    return "[" + ", ".join(py_value(v) for v in values) + "]"


def py_value(value: Any) -> str:
    if isinstance(value, str):
        return py_str(value)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, Mapping):
        return py_dict(value)
    if isinstance(value, (list, tuple)):
        return py_list(list(value))
    return py_str(str(value))


def requirements(plan: Mapping[str, Any]) -> str:
    lines = ["# generated by ADLC Console Studio: installed as linux/aarch64 wheels by the console's CodeBuild", STRANDS, AGENTCORE, *BOTO]
    if any(BUILTIN_TOOLS[t]["package"] for t in plan["builtins"]):
        lines.append(f"{TOOLS_PACKAGE}  # {', '.join(sorted({t for t in plan['builtins'] if BUILTIN_TOOLS[t]['package']}))}")
    extra = sorted({p for node in plan["custom"] for p in plan["tool_info"][node["id"]].get("packages") or []})
    lines += extra
    return "\n".join(lines) + "\n"


def generate(flow: Any, *, project: Mapping[str, Any] | None = None, version: int | None = None,
             kbs: Mapping[str, Mapping[str, Any]] | None = None, generated_at: str | None = None) -> dict[str, str]:
    """``main.py``, ``requirements.txt`` and ``studio_flow.json`` for a flow that :func:`validate` passes (otherwise
    :class:`StudioError` with its issues). ``kbs`` are the KBs as AWS describes them (``type``: MANAGED | VECTOR);
    without them the KB nodes' own ``kbType`` is used."""
    flow = normalize(flow)
    checked = validate(flow)
    if not checked["ok"]:
        errors = [i["message"] for i in checked["issues"] if i["level"] == "error"]
        raise StudioError(f"这个流程还不能生成代码（{len(errors)} 个问题）：" + "；".join(errors[:3]), checked["issues"])
    when = generated_at or _now()
    plan = _plan(flow, kbs or {})
    main = _render_main(plan, project=project, version=version, generated_at=when)
    try:
        compile(main, "main.py", "exec")
    except SyntaxError as exc:  # pragma: no cover - a generator bug, never the user's
        raise StudioError(f"生成的 main.py 编译不过（第 {exc.lineno} 行：{exc.msg}）：这是控制台的问题，请把流程导出反馈") from exc
    record = {"format": FORMAT, "project": {"id": (project or {}).get("id"), "name": (project or {}).get("name")}, "version": version,
              "generatedAt": when, "flow": flow}
    return {"main.py": main, "requirements.txt": requirements(plan), "studio_flow.json": json.dumps(record, ensure_ascii=False, indent=1) + "\n"}


def bundle(files: Mapping[str, str]) -> bytes:
    """The deployment zip: the files at its root, fixed timestamps (the same flow makes the same zip)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name].encode("utf-8"))
    return buffer.getvalue()


def read_bundle(data: bytes) -> dict[str, Any]:
    """The ``studio_flow.json`` record inside a deployment zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [n for n in archive.namelist() if n.rsplit("/", 1)[-1] == "studio_flow.json"]
            if not names:
                raise StudioError("这个 Runtime 的代码里没有 studio_flow.json：它不是 Studio 部署的")
            record = json.loads(archive.read(sorted(names, key=len)[0]).decode("utf-8"))
    except zipfile.BadZipFile as exc:
        raise StudioError("这个 Runtime 的代码不是 zip") from exc
    if not isinstance(record, Mapping) or record.get("format") != FORMAT or not isinstance(record.get("flow"), Mapping):
        raise StudioError("studio_flow.json 不是 Studio 的流程格式")
    return dict(record)


# -- templates -------------------------------------------------------------------------------------------------------

def _n(nid: str, kind: str, x: int, y: int, **data: Any) -> dict[str, Any]:
    return {"id": nid, "type": kind, "x": x, "y": y, "data": {**DEFAULTS[kind], **data}}


def _e(source: str, sport: str, target: str, tport: str) -> dict[str, Any]:
    return {"id": f"e-{source}-{target}-{tport}", "source": source, "sourcePort": sport, "target": target, "targetPort": tport}


POINTS_TOOL = '''def lookup_points(member_id: str) -> str:
    """查询会员当前的可用积分和最近一笔积分变动（演示账本：M1001、M1002）。问到某位会员有多少积分时调用。

    Args:
        member_id: 会员号，例如 M1001
    """
    ledger = {"M1001": (2380, "2026-09-28 购买扫地机并绑定 +1200"), "M1002": (560, "2026-09-30 兑换滤网 -300")}
    found = ledger.get(member_id.strip().upper())
    if not found:
        return f"积分账本里没有会员 {member_id}"
    balance, last = found
    return f"会员 {member_id.strip().upper()} 当前可用积分 {balance}；最近一笔：{last}（账本流水号 LEDGER-7731）"
'''

TEMPLATES: tuple[dict[str, Any], ...] = (
    {"id": "assistant", "name": "单个 Agent + 内置工具", "description": "一个 Agent 用计算器和当前时间回答问题：最简单的起点。",
     "flow": {"graphMode": False, "nodes": [
         _n("in", "input", 40, 150, sample="现在北京时间几点？顺便算一下 (17 + 25) × 3。"),
         _n("a1", "agent", 330, 130, label="助手", name="assistant", description="通用助手",
            systemPrompt="你是一个乐于助人的助手。需要计算时用 calculator，问到时间时用 current_time。用中文简洁回答。"),
         _n("t1", "tool", 230, 320, label="计算器", tool="calculator"), _n("t2", "tool", 470, 320, label="当前时间", tool="current_time"),
         _n("out", "output", 640, 150)],
      "edges": [_e("in", "out", "a1", "in"), _e("t1", "tool", "a1", "tools"), _e("t2", "tool", "a1", "tools"), _e("a1", "out", "out", "in")]}},
    {"id": "kb-support", "name": "知识库客服", "description": "客服 Agent 用知识库检索规则、用自定义工具查账本：选一个工作区的知识库就能部署。",
     "flow": {"graphMode": False, "nodes": [
         _n("in", "input", 40, 150, sample="我是会员 M1001，我现在有多少积分？这些积分多久会过期？"),
         _n("a1", "agent", 330, 130, label="积分客服", name="points_support", description="回答会员积分问题",
            systemPrompt="你是拾光家居的积分客服。会员的积分余额只能用 lookup_points 查账本得到，不能自己估算；积分规则（获取、有效期、兑换、补偿）"
                         "先用知识库检索，按检索到的原文回答。用中文回答，简洁、准确，并说明依据。"),
         _n("k1", "kb", 200, 320, label="积分规则知识库", toolName="search_points_rules"),
         _n("c1", "custom-tool", 470, 320, label="查积分账本", code=POINTS_TOOL),
         _n("out", "output", 640, 150)],
      "edges": [_e("in", "out", "a1", "in"), _e("k1", "tool", "a1", "tools"), _e("c1", "tool", "a1", "tools"), _e("a1", "out", "out", "in")]}},
    {"id": "orchestrator", "name": "编排 Agent + 子 Agent", "description": "主管把问题交给调研员（能访问网页）和写作者，再汇总：子 Agent 作为工具。",
     "flow": {"graphMode": False, "nodes": [
         _n("in", "input", 40, 120, sample="帮我调研一下 AWS 在 us-west-2 区域的含义，并写成三句话的介绍。"),
         _n("o1", "orchestrator", 320, 100, label="主管", name="lead",
            systemPrompt="你是主管。先让 researcher 收集事实，再让 writer 写成最终回答；最后检查是否回答了用户的问题。用中文。"),
         _n("a1", "agent", 180, 300, label="调研员", name="researcher", description="查资料、收集事实（可以访问网页）",
            systemPrompt="你是调研员：收集与问题相关的事实，必要时用 http_request 读取网页，列出要点和出处。"),
         _n("a2", "agent", 460, 300, label="写作者", name="writer", description="把要点写成通顺的中文",
            systemPrompt="你是写作者：把给你的要点写成准确、通顺、简洁的中文。"),
         _n("t1", "tool", 180, 470, label="HTTP 请求", tool="http_request"),
         _n("out", "output", 640, 120)],
      "edges": [_e("in", "out", "o1", "in"), _e("o1", "sub", "a1", "parent"), _e("o1", "sub", "a2", "parent"), _e("t1", "tool", "a1", "tools"),
                _e("o1", "out", "out", "in")]}},
    {"id": "swarm", "name": "Swarm 客服分诊", "description": "分诊、账单、技术三个成员互相交接，由最合适的成员回答。",
     "flow": {"graphMode": False, "nodes": [
         _n("in", "input", 40, 120, sample="我上个月被多扣了一次会员费，而且 App 登录一直报错 502。"),
         _n("s1", "swarm", 320, 100, label="客服团队", name="support_team", entry="a1"),
         _n("a1", "agent", 140, 300, label="分诊", name="triage", description="判断问题类型并交接给合适的同事",
            systemPrompt="你负责分诊：判断用户的问题属于账单还是技术，交接（handoff_to_agent）给对应的同事；两类都有就依次交接。"),
         _n("a2", "agent", 360, 300, label="账单专员", name="billing", description="处理扣费、退款、发票",
            systemPrompt="你是账单专员：解释扣费规则并给出处理步骤。处理完如果还有技术问题，交接给 tech。"),
         _n("a3", "agent", 580, 300, label="技术支持", name="tech", description="处理登录、报错、设备连接",
            systemPrompt="你是技术支持：给出排查步骤。最后给用户一份完整的中文答复，覆盖所有问题。"),
         _n("out", "output", 640, 120)],
      "edges": [_e("in", "out", "s1", "in"), _e("s1", "sub", "a1", "parent"), _e("s1", "sub", "a2", "parent"), _e("s1", "sub", "a3", "parent"),
                _e("s1", "out", "out", "in")]}},
    {"id": "graph", "name": "Graph 流水线", "description": "调研 → 撰写 → 审校，按依赖先后运行（Graph 模式），审校的结果就是回答。",
     "flow": {"graphMode": True, "nodes": [
         _n("in", "input", 40, 150, sample="写一段 100 字左右的介绍：什么是 Amazon Bedrock AgentCore。"),
         _n("a1", "agent", 230, 130, label="调研", name="research", description="列出要点",
            systemPrompt="列出回答这个问题需要的 5 个关键事实要点。"),
         _n("a2", "agent", 470, 130, label="撰写", name="draft", description="写初稿", systemPrompt="根据上游给出的要点写一段通顺的中文初稿。"),
         _n("a3", "agent", 710, 130, label="审校", name="review", description="审校定稿",
            systemPrompt="审校上游的初稿：纠正错误、精简表达，只输出定稿。"),
         _n("out", "output", 950, 150)],
      "edges": [_e("in", "out", "a1", "in"), _e("a1", "out", "a2", "in"), _e("a2", "out", "a3", "in"), _e("a3", "out", "out", "in")]}},
    {"id": "blank", "name": "空白", "description": "一个输入、一个 Agent、一个输出。",
     "flow": {"graphMode": False, "nodes": [_n("in", "input", 60, 150), _n("a1", "agent", 330, 130, label="Agent", name="agent_1"),
                                             _n("out", "output", 620, 150)],
              "edges": [_e("in", "out", "a1", "in"), _e("a1", "out", "out", "in")]}},
)


def catalog() -> dict[str, Any]:
    """What the page needs to draw and check a flow: node types and their ports, the connection rules, defaults,
    models, built-in tools, what a custom tool may import, the templates."""
    return {"types": [{"type": t, "label": TYPE_LABEL[t], "ports": PORTS[t]} for t in NODE_TYPES],
            "rules": [{"sources": list(s), "sourcePort": sp, "targets": list(t), "targetPort": tp, "graphOnly": go, "kind": k} for s, sp, t, tp, go, k in RULES],
            "defaults": DEFAULTS, "models": [{"id": m, "label": label} for m, label in MODELS], "defaultModel": DEFAULT_MODEL,
            "builtinTools": [{"id": k, "label": v["label"], "description": v["description"], "package": v["package"] or "strands-agents"}
                             for k, v in BUILTIN_TOOLS.items()],
            "safeModules": sorted(SAFE_MODULES), "customToolTemplate": CUSTOM_TOOL_TEMPLATE,
            "templates": [{"id": t["id"], "name": t["name"], "description": t["description"], "flow": normalize(t["flow"])} for t in TEMPLATES],
            "versions": {"strands": STRANDS, "agentcore": AGENTCORE, "tools": TOOLS_PACKAGE, "python": PYTHON}}


# -- projects ----------------------------------------------------------------------------------------------------------

def _all(store: Any) -> dict[str, dict[str, Any]]:
    return store.read(COLLECTION, {}) or {}


def _project(store: Any, workspace: str, pid: str) -> dict[str, Any]:
    if not PROJECT_ID.match(pid or ""):
        raise StudioError(f"not a project id: {pid}")
    found = _all(store).get(pid)
    if not found or found.get("workspace") != workspace:
        raise NotFound(f"no Studio project {pid} in this workspace")
    return found


def _summary(p: Mapping[str, Any]) -> dict[str, Any]:
    versions = p.get("versions") or []
    latest = versions[-1] if versions else {}
    deployments = p.get("deployments") or []
    return {"id": p["id"], "name": p.get("name"), "description": p.get("description"), "createdAt": p.get("createdAt"), "updatedAt": p.get("updatedAt"),
            "createdBy": p.get("createdBy"), "version": latest.get("version"), "versions": len(versions), "summary": latest.get("summary"),
            "deployments": len(deployments), "lastDeployment": deployments[-1] if deployments else None, "importedFrom": p.get("importedFrom")}


def _flow_summary(flow: Mapping[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for n in flow["nodes"]:
        counts[n["type"]] = counts.get(n["type"], 0) + 1
    return {"nodes": len(flow["nodes"]), "agents": sum(counts.get(t, 0) for t in EXECUTABLE), "tools": sum(counts.get(t, 0) for t in TOOLISH),
            "graphMode": flow["graphMode"], "types": counts}


def _version_entry(flow: Mapping[str, Any], number: int, caller: Mapping[str, Any], note: str) -> dict[str, Any]:
    return {"version": number, "savedAt": _now(), "savedBy": caller.get("username"), "note": _line(note, 200), "hash": flow_hash(flow),
            "summary": _flow_summary(flow), "flow": flow}


def list_projects(console: Any, workspace: str) -> list[dict[str, Any]]:
    mine = [p for p in _all(console.store).values() if p.get("workspace") == workspace]
    return [_summary(p) for p in sorted(mine, key=lambda p: str(p.get("updatedAt") or ""), reverse=True)]


def create_project(console: Any, workspace: str, body: Mapping[str, Any], caller: Mapping[str, Any]) -> dict[str, Any]:
    """A new project from a flow, a template (``template``) or the blank one; its first version is saved."""
    name = _line(body.get("name"), 80)
    if not name:
        raise StudioError("name: the project's name")
    if body.get("flow") is not None:
        flow = normalize(body["flow"])
    else:
        template = next((t for t in TEMPLATES if t["id"] == (body.get("template") or "blank")), None)
        if not template:
            raise StudioError(f"template: one of {', '.join(t['id'] for t in TEMPLATES)}")
        flow = normalize(template["flow"])
    pid = f"sp-{secrets.token_hex(5)}"
    project = {"id": pid, "workspace": workspace, "name": name, "description": _clean(body.get("description"), 500).strip(), "createdAt": _now(),
               "updatedAt": _now(), "createdBy": caller.get("username"), "versions": [_version_entry(flow, 1, caller, body.get("note") or "创建")],
               "deployments": []}
    if body.get("importedFrom"):
        project["importedFrom"] = body["importedFrom"]

    def change(all_: dict[str, Any]) -> dict[str, Any]:
        if sum(1 for p in all_.values() if p.get("workspace") == workspace) >= MAX_PROJECTS:
            raise StudioError(f"this workspace has {MAX_PROJECTS} Studio projects: delete some first")
        return {**all_, pid: project}

    console.store.update(COLLECTION, {}, change)
    return get_project(console, workspace, pid)


def get_project(console: Any, workspace: str, pid: str, version: Any = None) -> dict[str, Any]:
    """A project with its versions (newest first, without their flows) and one version's flow (the latest by default)."""
    p = _project(console.store, workspace, pid)
    versions = p.get("versions") or []
    if version not in (None, ""):
        try:
            wanted = int(version)
        except (TypeError, ValueError) as exc:
            raise StudioError("version: a version number") from exc
        chosen = next((v for v in versions if v["version"] == wanted), None)
        if not chosen:
            raise NotFound(f"{pid} has no version {wanted}")
    else:
        chosen = versions[-1]
    return {**_summary(p), "versionsList": [{k: v[k] for k in ("version", "savedAt", "savedBy", "note", "summary", "hash") if k in v} for v in reversed(versions)],
            "flowVersion": chosen["version"], "flow": chosen["flow"],
            "deploymentsList": [_deployment_view(console, d) for d in reversed(p.get("deployments") or [])]}


def save_project(console: Any, workspace: str, pid: str, body: Mapping[str, Any], caller: Mapping[str, Any]) -> dict[str, Any]:
    """Save the canvas: a new version when the flow changed (``saved``), and the name / description."""
    flow = normalize(body.get("flow")) if body.get("flow") is not None else None
    outcome: dict[str, Any] = {}

    def change(all_: dict[str, Any]) -> dict[str, Any]:
        p = all_.get(pid)
        if not p or p.get("workspace") != workspace:
            raise NotFound(f"no Studio project {pid} in this workspace")
        p = dict(p)
        versions = list(p.get("versions") or [])
        if flow is not None and (not versions or versions[-1]["hash"] != flow_hash(flow)):
            versions.append(_version_entry(flow, (versions[-1]["version"] if versions else 0) + 1, caller, body.get("note") or ""))
            outcome["saved"] = True
        p["versions"] = versions[-MAX_VERSIONS:]
        if body.get("name") is not None:
            p["name"] = _line(body["name"], 80) or p["name"]
        if body.get("description") is not None:
            p["description"] = _clean(body["description"], 500).strip()
        p["updatedAt"] = _now()
        return {**all_, pid: p}

    if not PROJECT_ID.match(pid or ""):
        raise StudioError(f"not a project id: {pid}")
    console.store.update(COLLECTION, {}, change)
    return {**get_project(console, workspace, pid), "saved": bool(outcome.get("saved"))}


def delete_project(console: Any, workspace: str, pid: str) -> dict[str, Any]:
    """Forget a project and its versions (its deployed runtimes stay: delete them from 部署代码)."""
    p = _project(console.store, workspace, pid)
    console.store.update(COLLECTION, {}, lambda all_: {k: v for k, v in all_.items() if k != pid})
    return {"deleted": pid, "runtimesLeft": sorted({_deployment_view(console, d).get("runtimeId") or d.get("name") for d in p.get("deployments") or [] if d})}


# -- deployments -----------------------------------------------------------------------------------------------------

PROBE = re.compile(r"^adlc_probe_([a-z]{2,12})_")


def names_for(runtime_name: str) -> deploy.Names:
    """A runtime named ``adlc_probe_*`` is a live probe: its role and build project are a probe's (``adlc-probe-*``),
    and ``adlc_probe_<module>_*`` gets its module's own (``adlc-probe-<module>-build``), so probes of several modules
    running at once never share — or delete — one another's build project."""
    name = str(runtime_name or "")
    found = PROBE.match(name)
    if found:
        return deploy.Names(f"adlc-probe-{found.group(1)}")
    return deploy.Names("adlc-probe") if name.startswith("adlc_probe_") else deploy.NAMES


def resolve_kbs(session: Any, region: str, flow: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The KBs a flow's KB nodes name, as AWS describes them: each must exist, be ACTIVE and take Retrieve."""
    out: dict[str, dict[str, Any]] = {}
    ids = sorted({n["data"]["kbId"] for n in flow["nodes"] if n["type"] == "kb" and n["data"].get("kbId")})
    if not ids:
        return out
    agent = client(session, "bedrock-agent", region)
    for kb_id in ids:
        try:
            kb = agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
        except Exception as exc:  # noqa: BLE001
            if deploy._code(exc) == "ResourceNotFoundException":
                raise StudioError(f"知识库 {kb_id} 不在这个工作区（{region}）") from exc
            raise
        kind = str((kb.get("knowledgeBaseConfiguration") or {}).get("type") or "")
        if kb.get("status") != "ACTIVE":
            raise StudioError(f"知识库 {kb.get('name')}（{kb_id}）现在是 {kb.get('status')}：等它 ACTIVE 再部署")
        if kind not in ("MANAGED", "VECTOR"):
            raise StudioError(f"知识库 {kb.get('name')}（{kb_id}）是 {kind} 类型：Studio 的检索工具只支持 MANAGED 和 VECTOR")
        out[kb_id] = {"id": kb_id, "name": kb.get("name"), "type": kind, "arn": kb.get("knowledgeBaseArn"), "description": kb.get("description") or ""}
    return out


def list_kbs(session: Any, region: str) -> list[dict[str, Any]]:
    """The workspace's KBs with their type (a KB node offers the ACTIVE MANAGED / VECTOR ones)."""
    agent = client(session, "bedrock-agent", region)
    out, kwargs = [], {}
    while True:
        page = agent.list_knowledge_bases(**kwargs)
        for summary in page.get("knowledgeBaseSummaries") or []:
            entry = {"id": summary.get("knowledgeBaseId"), "name": summary.get("name"), "status": summary.get("status"),
                     "description": summary.get("description") or "", "type": None}
            try:
                kb = agent.get_knowledge_base(knowledgeBaseId=entry["id"])["knowledgeBase"]
                entry["type"] = (kb.get("knowledgeBaseConfiguration") or {}).get("type")
            except Exception:  # noqa: BLE001 - listed without its type
                pass
            out.append(entry)
        if not page.get("nextToken"):
            return out
        kwargs["nextToken"] = page["nextToken"]


def _job(console: Any, job_id: str | None) -> dict[str, Any] | None:
    if not job_id:
        return None
    try:
        return console.jobs.get(job_id)
    except (KeyError, OSError, ValueError):
        return None


def _deployment_view(console: Any, record: Mapping[str, Any]) -> dict[str, Any]:
    job = _job(console, record.get("jobId")) or {}
    result = job.get("result") or {}
    progress = job.get("progress") or {}
    imported = record.get("mode") == "imported"  # a runtime this project was read back from: deployed, by another console
    return {**record, "runtimeId": record.get("runtimeId") or result.get("runtimeId") or progress.get("runtimeId"),
            "runtimeVersion": result.get("version") or progress.get("version") or record.get("runtimeVersion"),
            "jobStatus": job.get("status") or ("succeeded" if imported else None), "jobError": job.get("error"), "stage": progress.get("stage"),
            "smoke": result.get("smoke")}


def project_deployments(console: Any, workspace: str, pid: str) -> list[dict[str, Any]]:
    p = _project(console.store, workspace, pid)
    return [_deployment_view(console, d) for d in reversed(p.get("deployments") or [])]


def studio_runtimes(console: Any, workspace: str) -> list[dict[str, Any]]:
    """Every runtime deployed from a Studio project of this workspace (its latest deployment), newest first."""
    latest: dict[str, dict[str, Any]] = {}
    for p in _all(console.store).values():
        if p.get("workspace") != workspace:
            continue
        for d in p.get("deployments") or []:
            view = {**_deployment_view(console, d), "projectId": p["id"], "projectName": p.get("name")}
            key = view.get("runtimeId") or f"job:{d.get('jobId')}"
            if key not in latest or str(view.get("at")) >= str(latest[key].get("at")):
                latest[key] = view
    return sorted(latest.values(), key=lambda v: str(v.get("at") or ""), reverse=True)


def deploy_project(console: Any, workspace: str, pid: str, body: Mapping[str, Any], caller: Mapping[str, Any]) -> dict[str, Any]:
    """Deploy a saved version of a project (the latest by default): a new runtime (``name``) or a new version of
    one this project deployed (``runtimeId``). Returns the deploy job (``deploy.start_deploy``)."""
    p = _project(console.store, workspace, pid)
    chosen = get_project(console, workspace, pid, body.get("version"))
    flow, number = chosen["flow"], chosen["flowVersion"]
    runtime_id = str(body.get("runtimeId") or "").strip() or None
    if runtime_id:
        record = deploy._records(console.store, workspace).get(runtime_id) or {}
        name = str(record.get("name") or next((d.get("name") for d in p.get("deployments") or [] if _deployment_view(console, d).get("runtimeId") == runtime_id), "")
                   or "")
        if not name:
            raise StudioError(f"{runtime_id} 不是这个控制台部署的 Runtime")
    else:
        name = str(body.get("name") or "").strip()
        if not deploy.NAME.match(name):
            raise StudioError("name: Runtime 名称（字母开头，字母、数字或下划线，最多 48 个）")
    ws = console.workspaces.get(workspace)
    console.workspaces.verify(workspace)
    session = console.workspaces.session(workspace)
    kbs = resolve_kbs(session, ws["region"], flow)
    files = generate(flow, project={"id": pid, "name": p.get("name")}, version=number, kbs=kbs)
    archive = bundle(files)
    sample = next((n["data"].get("sample") for n in flow["nodes"] if n["type"] == "input"), "") or ""
    request = {"source": "zip", "archive": base64.b64encode(archive).decode("ascii"), "filename": f"studio-{pid}-v{number}.zip", "entryPoint": "main.py",
               "pythonRuntime": PYTHON, "installRequirements": True, "protocol": "HTTP", "environment": {}, "smoke": body.get("smoke", True) is not False,
               "smokePrompt": _clean(body.get("smokePrompt") or sample or deploy.SMOKE_PROMPT, 2000), "knowledgeBases": sorted(kbs)}
    if not runtime_id:
        request["name"] = name
    else:
        request.pop("protocol")
        request.pop("environment")
    job = deploy.start_deploy(console, workspace, request, runtime_id=runtime_id, names=names_for(name))
    entry = {"jobId": job["id"], "mode": "update" if runtime_id else "create", "name": name, "runtimeId": runtime_id, "flowVersion": number,
             "at": _now(), "by": caller.get("username"), "knowledgeBases": sorted(kbs)}

    def change(all_: dict[str, Any]) -> dict[str, Any]:
        q = dict(all_.get(pid) or p)
        q["deployments"] = (list(q.get("deployments") or []) + [entry])[-MAX_DEPLOYMENTS:]
        q["updatedAt"] = _now()
        return {**all_, pid: q}

    console.store.update(COLLECTION, {}, change)
    return {**job, "studio": entry}


def open_runtime(console: Any, workspace: str, runtime_id: str, caller: Mapping[str, Any]) -> dict[str, Any]:
    """The Studio project and flow version a runtime was deployed from: found in this console's projects, else read
    back from the deployment's own ``source.zip`` (its ``studio_flow.json``) into a new project."""
    if not deploy.RUNTIME_ID.match(runtime_id or ""):
        raise StudioError(f"not a runtime id: {runtime_id}")
    for view in studio_runtimes(console, workspace):
        if view.get("runtimeId") == runtime_id:
            return {"projectId": view["projectId"], "version": view["flowVersion"], "imported": False}
    ws = console.workspaces.get(workspace)
    console.workspaces.verify(workspace)
    session = console.workspaces.session(workspace)
    ctl = client(session, "bedrock-agentcore-control", ws["region"])
    runtime = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
    tags = ctl.list_tags_for_resource(resourceArn=runtime["agentRuntimeArn"]).get("tags") or {}
    if tags.get("adlc:console") != "1":
        raise StudioError("只有这个控制台部署的 Runtime 能在 Studio 打开")
    code = (((runtime.get("agentRuntimeArtifact") or {}).get("codeConfiguration") or {}).get("code") or {}).get("s3") or {}
    bucket, key = str(code.get("bucket") or ""), str(code.get("prefix") or "")
    if bucket != console_bucket(ws["accountId"], ws["region"]) or not key.startswith("deployments/"):
        raise StudioError("这个 Runtime 的代码不在控制台的桶里：它不是 Studio 部署的")
    if key.endswith("/bundle.zip"):
        key = key[: -len("bundle.zip")] + "source.zip"  # the upload CodeBuild built the bundle from: small, ours
    s3 = client(session, "s3", ws["region"])
    try:
        obj = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=ws["accountId"])
    except Exception as exc:  # noqa: BLE001
        if deploy._missing(exc) or deploy._code(exc) == "NoSuchKey":
            raise StudioError(f"找不到这个 Runtime 的源码 s3://{bucket}/{key}") from exc
        raise
    if int(obj.get("ContentLength") or 0) > deploy.MAX_ARCHIVE:
        raise StudioError("这个 Runtime 的源码太大：它不是 Studio 部署的")
    record = read_bundle(obj["Body"].read())
    name = f"{(record.get('project') or {}).get('name') or runtime.get('agentRuntimeName')}（从 {runtime.get('agentRuntimeName')} 导入）"
    created = create_project(console, workspace, {"name": name, "flow": record["flow"], "note": f"从 Runtime {runtime_id} 版本 {runtime.get('agentRuntimeVersion')} 导入",
                                                  "importedFrom": {"runtimeId": runtime_id, "runtimeVersion": runtime.get("agentRuntimeVersion"),
                                                                   "project": record.get("project"), "version": record.get("version")}}, caller)
    link = {"jobId": None, "mode": "imported", "name": runtime.get("agentRuntimeName"), "runtimeId": runtime_id, "flowVersion": 1,
            "runtimeVersion": runtime.get("agentRuntimeVersion"), "at": _now(), "by": caller.get("username")}
    console.store.update(COLLECTION, {}, lambda all_: {**all_, created["id"]: {**all_[created["id"]], "deployments": [link]}})
    return {"projectId": created["id"], "version": 1, "imported": True}


#: What a runtime's log lines worth showing look like: the generated code's turns and failures, warnings, tracebacks.
LOG_PATTERN = '?ERROR ?WARNING ?Traceback ?"studio: turn"'
SESSION_STREAM = re.compile(r"\[runtime-logs-([^\]]+)\]")


def runtime_logs(console: Any, workspace: str, runtime_id: str, *, session_id: str | None = None, minutes: Any = 60) -> dict[str, Any]:
    """A runtime's recent turns, warnings and errors from its DEFAULT endpoint's log group (one stream per session,
    ``<date>/[runtime-logs-<session>]<id>``), oldest first. AgentCore drops the body of a non-2xx answer — the
    caller only reads "Received error (500) from runtime" — so this is where a failed turn says why."""
    if not deploy.RUNTIME_ID.match(runtime_id or ""):
        raise StudioError(f"not a runtime id: {runtime_id}")
    try:
        minutes = max(1, min(int(minutes or 60), 24 * 60))
    except (TypeError, ValueError) as exc:
        raise StudioError("minutes: a number of minutes") from exc
    ws = console.workspaces.get(workspace)
    console.workspaces.verify(workspace)
    logs = client(console.workspaces.session(workspace), "logs", ws["region"])
    group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    kwargs: dict[str, Any] = {"logGroupName": group, "filterPattern": LOG_PATTERN, "startTime": int((time.time() - minutes * 60) * 1000), "limit": 200}
    found: list[dict[str, Any]] = []
    try:
        for _page in range(10):  # a filter scans: pages may come back empty with a token
            page = logs.filter_log_events(**kwargs)
            found += page.get("events") or []
            if not page.get("nextToken") or len(found) >= 400:
                break
            kwargs["nextToken"] = page["nextToken"]
    except Exception as exc:  # noqa: BLE001
        if deploy._code(exc) == "ResourceNotFoundException":
            return {"logGroup": group, "events": [], "missing": True}
        raise
    events = []
    for e in sorted(found, key=lambda e: e.get("timestamp") or 0):
        stream = SESSION_STREAM.search(str(e.get("logStreamName") or ""))
        sid = stream.group(1) if stream else None
        if session_id and sid != session_id:
            continue
        when = datetime.fromtimestamp((e.get("timestamp") or 0) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        events.append({"at": when, "session": sid, "message": str(e.get("message") or "").rstrip()[:2000]})
    return {"logGroup": group, "events": events[-80:], "minutes": minutes}


# -- routes ----------------------------------------------------------------------------------------------------------

def _safe(fn):
    """Refusals answer 400 (with a flow's issues when they are why), a missing project or runtime 404."""
    def route(r: Any) -> Any:
        try:
            return fn(r)
        except StudioError as exc:
            return 400, {"error": str(exc), **({"issues": exc.issues} if exc.issues else {})}
        except deploy.DeployError as exc:
            return 400, {"error": str(exc)}
        except NotFound as exc:
            return 404, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            if deploy._code(exc) == "ResourceNotFoundException":
                return 404, {"error": f"not found: {str(exc)[:300]}"}
            raise
    return route


def _preview(r: Any) -> tuple[int, dict[str, Any]]:
    """Validation and, when it passes, the generated files of an unsaved canvas (no AWS call)."""
    flow = normalize(r.body.get("flow"))
    checked = validate(flow)
    out: dict[str, Any] = {"ok": checked["ok"], "issues": checked["issues"], "tools": checked["tools"], "files": None}
    if checked["ok"]:
        project = r.body.get("project") if isinstance(r.body.get("project"), Mapping) else {}
        out["files"] = generate(flow, project={"id": project.get("id"), "name": project.get("name")}, version=project.get("version"))
    return 200, out


def _bundle(r: Any) -> tuple[int, dict[str, Any]]:
    project = r.body.get("project") if isinstance(r.body.get("project"), Mapping) else {}
    files = generate(r.body.get("flow"), project={"id": project.get("id"), "name": project.get("name")}, version=project.get("version"))
    data = bundle(files)
    base = slug(project.get("name") or "", 30) or "studio-agent"
    return 200, {"filename": f"{base}{'-v' + str(project.get('version')) if project.get('version') else ''}.zip", "archive": base64.b64encode(data).decode("ascii"),
                 "size": len(data), "files": sorted(files)}


def _download(r: Any) -> Any:
    from .web import Stream

    chosen = get_project(r.console, r.workspace(), r.params["pid"], r.query.get("version"))
    files = generate(chosen["flow"], project={"id": chosen["id"], "name": chosen["name"]}, version=chosen["flowVersion"])
    return Stream([bundle(files)], "application/zip")


def register(router: Any) -> None:
    """``/workspaces/{wid}/studio/...``: the catalog; projects (list, create, open a version, save, delete — admin);
    preview (validate + generate), bundle and download; the workspace's KBs; deploy (admin) and the deployments;
    opening a deployed runtime in Studio; a runtime's recent turns and errors from its logs."""
    add = router.add
    base = "/workspaces/{wid}/studio"

    def region(r: Any) -> str:
        return r.console.workspaces.get(r.workspace())["region"]

    add("GET", f"{base}/catalog", _safe(lambda r: (r.workspace(), (200, catalog()))[1]))
    add("GET", f"{base}/projects", _safe(lambda r: (200, {"projects": list_projects(r.console, r.workspace())})))
    add("POST", f"{base}/projects", _safe(lambda r: (201, create_project(r.console, r.workspace(), r.body, r.caller))))
    add("GET", f"{base}/projects/{{pid}}", _safe(lambda r: (200, get_project(r.console, r.workspace(), r.params["pid"], r.query.get("version")))))
    add("PUT", f"{base}/projects/{{pid}}", _safe(lambda r: (200, save_project(r.console, r.workspace(), r.params["pid"], r.body, r.caller))))
    add("DELETE", f"{base}/projects/{{pid}}", _safe(lambda r: (200, delete_project(r.console, r.workspace(), r.params["pid"]))), admin=True)
    add("GET", f"{base}/projects/{{pid}}/download", _safe(_download))
    add("POST", f"{base}/projects/{{pid}}/deploy", _safe(lambda r: (202, deploy_project(r.console, r.workspace(), r.params["pid"], r.body, r.caller))),
        admin=True)
    add("GET", f"{base}/projects/{{pid}}/deployments", _safe(lambda r: (200, {"deployments": project_deployments(r.console, r.workspace(), r.params["pid"])})))
    add("POST", f"{base}/preview", _safe(lambda r: (r.workspace(), _preview(r))[1]))
    add("POST", f"{base}/bundle", _safe(lambda r: (r.workspace(), _bundle(r))[1]))
    add("GET", f"{base}/knowledge-bases", _safe(lambda r: (200, {"knowledgeBases": list_kbs(r.session(), region(r))})))
    add("GET", f"{base}/runtimes", _safe(lambda r: (200, {"runtimes": studio_runtimes(r.console, r.workspace())})))
    add("POST", f"{base}/open-runtime", _safe(lambda r: (200, open_runtime(r.console, r.workspace(), str(r.body.get("runtimeId") or ""), r.caller))))
    add("GET", f"{base}/runtimes/{{rid}}/logs", _safe(lambda r: (200, runtime_logs(r.console, r.workspace(), r.params["rid"], session_id=r.query.get("session") or None,
                                                                                 minutes=r.query.get("minutes") or 60))))
