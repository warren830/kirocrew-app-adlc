"""Pre-rehearsal: predict the teaching verdicts on the Workshop model before the AWS Guided Run.

Every acceptance scenario's first rehearsal found a defect the generator had not avoided (a refusal
that still called the gated tool, a baseline that was already grounded, a gap question retrieval
covered), and each round costs a scenario-only cleanup plus a 42-minute Guided Run. This module runs
the practice questions of a built pack locally, in minutes, against the same model and the same
inputs the Workshop agent gets:

* the system prompt is the pack's baseline or optimization candidate;
* the tools are the pack's gateway schema, named ``<toolTargetName>___<name>`` as the gateway names
  them; every call runs through the release's own tools handler (``lambda_handler``: mock tools answer
  from the pack's fixtures) and the model reads its whole ``{statusCode, body}`` response, as live;
* the retrieval tool searches a local copy of the knowledge base: Titan Text Embeddings V2 over
  fixed-size chunks of about the Workshop KB's size (128 tokens, 20% overlap), top 3;
* the loop stops at the harness limits (04-deploy.sh: ``--max-iterations 30 --max-tokens 8192``).

L1 runs for real on each answer (``l1.evaluate_case`` on the text of every turn, joined as the CLI streams
it), so the tool_use, refusal and escalation phenomena are predicted from what the agent actually did. With a
``thelma`` scorer the Workshop's own THELMA code scores what its Lambda would (:mod:`pre_thelma`: the first
search, its result and the last reply), and each gap question is also sampled as the first search itself.
Backtested on the thirteen live rehearsals of 2026-09-28 to 10-01 (their releases, tools/pre_rehearsal_backtest.py;
campus-registrar round 2, whose miss led to the question-as-search samples, among them): an absent gap
predicted ``likely_reproduced`` reproduced 4 of 4 times, one predicted ``likely_not_reproduced`` failed 4 of 4
times, ``uncertain`` failed 1 of 5; so that prediction is a finding and counts in the class prediction. The
prompt_fixable predictions agreed with 9 of 24 live verdicts (the live baseline was often already grounded where
the local one was not) and are shown only. Without a scorer both stay ``not_predicted``. Flags name what students would see: an empty reply, a
retrieval loop, an answer cut at the token limit, a model error (that question's invoke error, as live: it
does not stop the run). The skills are announced as the Harness does (``with_skills``); Memory is not reproduced and the model is
sampled, so every question runs ``repeat`` times (default 2): an L1 phenomenon is ``likely_not_reproduced``
when it broke in any run (in class some students would see that), ``likely_reproduced`` when it held in every
run of every case, ``unknown`` when a case was not run or a run it reads ended in an invoke error, else
``needs_goal_judge``. The output never decides ``readyForClass`` (only ``run/rehearsal.json`` does) and never
replaces the AWS rehearsal.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import l1, pre_thelma, teaching, teaching_policy
from .direct.aws import _code_error
from .rehearsal import FOCUS_CHECKS, RETRIEVAL_CAP
from .script_facts import UPSTREAM_MAX_ITERATIONS

SCHEMA = "workshop-customizer/pre-rehearsal/1"
DEFAULT_MODEL = "us.amazon.nova-2-lite-v1:0"
EMBED_MODEL = "amazon.titan-embed-text-v2:0"
#: 04-deploy.sh deploys the harness with --max-tokens 8192.
MAX_TOKENS = 8192
#: knowledge-base/create_kb.py: FIXED_SIZE chunks of 128 tokens with 20% overlap; the tools handler asks for 3.
CHUNK_TOKENS, CHUNK_OVERLAP, TOP_K = 128, 0.2, 3
PHASES = ("baseline", "optimized")
HANDLER = Path(__file__).resolve().parent / "templates" / "scenario_tools_handler.py"

_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_UNITS = re.compile(r"[㐀-鿿豈-﫿]|[^\s㐀-鿿豈-﫿]+|\s+")

# ---------------------------------------------------------------------------
# Knowledge base: chunks and a local vector search
# ---------------------------------------------------------------------------


def _weight(unit: str) -> float:
    if unit.isspace():
        return 0.0
    return 1.0 if _CJK.fullmatch(unit) else max(1.0, len(unit) / 4)


def chunk_text(text: str, max_tokens: int = CHUNK_TOKENS, overlap: float = CHUNK_OVERLAP) -> list[str]:
    """Fixed-size chunks of about ``max_tokens`` estimated tokens (a CJK character is one, a word about
    len/4) that overlap by ``overlap`` of a chunk, like the Workshop KB's FIXED_SIZE chunking."""
    units = _UNITS.findall(text)
    chunks: list[str] = []
    start = 0
    while start < len(units):
        size, end = 0.0, start
        while end < len(units) and (size + _weight(units[end]) <= max_tokens or end == start):
            size += _weight(units[end])
            end += 1
        piece = "".join(units[start:end]).strip()
        if piece:
            chunks.append(piece)
        if end >= len(units):
            break
        back, kept = 0.0, end
        while kept > start + 1 and back + _weight(units[kept - 1]) <= max_tokens * overlap:
            kept -= 1
            back += _weight(units[kept])
        start = kept
    return chunks


@dataclass
class Index:
    chunks: list[tuple[str, str]] = field(default_factory=list)  # (source, text)
    vectors: list[list[float]] = field(default_factory=list)

    def search(self, vector: Sequence[float], k: int = TOP_K) -> list[tuple[str, str, float]]:
        scored = [(sum(a * b for a, b in zip(vector, v)), i) for i, v in enumerate(self.vectors)]
        scored.sort(key=lambda item: -item[0])
        return [(self.chunks[i][0], self.chunks[i][1], round(score, 4)) for score, i in scored[:k]]


# ---------------------------------------------------------------------------
# Bedrock calls (a boto3 bedrock-runtime client, or any object with the same two methods)
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    embeddings: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"converseCalls": self.calls, "inputTokens": self.input_tokens, "outputTokens": self.output_tokens,
                "embeddings": self.embeddings}


#: Network errors worth another attempt (the SSL EOF seen from this Mac on 2026-09-29 among them).
TRANSIENT_ERRORS = frozenset({"ReadTimeoutError", "ConnectTimeoutError", "EndpointConnectionError", "ConnectionClosedError",
                              "SSLError", "ProxyConnectionError"})
#: Converse errors that belong to one question, as live (L1 reads them as that question's invoke error, and the
#: rehearsal as insufficient evidence): Nova's "Model produced invalid sequence as part of ToolUse"
#: (ValidationException), ModelErrorException, ModelTimeoutException. Anything else (access, credentials) stops the run.
MODEL_ERRORS = frozenset({"ValidationException", "ModelErrorException", "ModelTimeoutException"})
#: The CLI's line for Strands' MaxTokensReachedException (l1.TRUNCATED_RE).
TRUNCATED_ERROR = "Error: Model stopped generating due to maximum token limit"


#: An embedding has no question to fail: Titan's "unexpected error during processing. Try your request again."
#: (ModelErrorException, 2026-10-01) is retried like throttling instead of ending the run.
EMBED_RETRIED = frozenset({"ModelErrorException", "ModelTimeoutException", "InternalServerException"})


def _retrying(fn: Callable[..., Any], *, attempts: int = 6, pause: float = 2.0, sleep: Callable[[float], None] = time.sleep,
              also: frozenset[str] = frozenset(), **kwargs):
    for attempt in range(attempts):
        try:
            return fn(**kwargs)
        except Exception as exc:  # noqa: BLE001 - botocore throttling, read timeouts
            transient = _code_error(exc) in ("ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException") or \
                _code_error(exc) in also or type(exc).__name__ in TRANSIENT_ERRORS
            if not transient or attempt == attempts - 1:
                raise
            sleep(pause * (2 ** attempt))
    raise AssertionError("unreachable")


def embed(client: Any, text: str, usage: Usage, *, model: str = EMBED_MODEL) -> list[float]:
    body = json.dumps({"inputText": text[:8000], "dimensions": 1024, "normalize": True})
    response = _retrying(client.invoke_model, also=EMBED_RETRIED, modelId=model, body=body)
    usage.embeddings += 1
    raw = response["body"].read() if hasattr(response["body"], "read") else response["body"]
    return list(json.loads(raw)["embedding"])


def build_index(client: Any, documents: Mapping[str, str], usage: Usage, *, cache: Path | None = None) -> Index:
    """Chunk and embed ``documents`` (source → text); embeddings are cached by text hash under ``cache``."""
    index = Index()
    stored: dict[str, list[float]] = {}
    cache_file = cache / "embeddings.json" if cache else None
    if cache_file and cache_file.is_file():
        stored = json.loads(cache_file.read_text(encoding="utf-8"))
    for source, text in documents.items():
        for piece in chunk_text(text):
            key = hashlib.sha256(piece.encode("utf-8")).hexdigest()
            if key not in stored:
                stored[key] = embed(client, piece, usage)
            index.chunks.append((source, piece))
            index.vectors.append(stored[key])
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(stored), encoding="utf-8")
    return index


# ---------------------------------------------------------------------------
# The pack's tools
# ---------------------------------------------------------------------------


def load_handler(fixtures: Mapping[str, Any]):
    """The release's own tools handler module, reading ``fixtures`` (``lambda_handler`` is what the gateway invokes)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(fixtures, fh, ensure_ascii=False)
        path = fh.name
    previous = os.environ.get("TOOL_FIXTURES_PATH")
    os.environ["TOOL_FIXTURES_PATH"] = path
    try:
        spec = importlib.util.spec_from_file_location("pre_rehearsal_tools_handler", HANDLER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    finally:
        if previous is None:
            os.environ.pop("TOOL_FIXTURES_PATH", None)
        else:
            os.environ["TOOL_FIXTURES_PATH"] = previous
        os.unlink(path)
    return module


@dataclass
class Toolbox:
    target: str
    specs: list[dict[str, Any]]
    fixtures: dict[str, Any]
    retrieval_tool: str
    retrieve: Callable[[str], dict[str, Any]]
    handler: Any = None

    def config(self) -> dict[str, Any]:
        return {"tools": [{"toolSpec": {"name": f"{self.target}___{t['name']}", "description": t.get("description") or t["name"],
                                        "inputSchema": {"json": t.get("inputSchema") or {"type": "object", "properties": {}}}}}
                          for t in self.specs]}

    def run(self, full_name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """The release handler's Lambda response to one call, ``{statusCode, body}`` with a JSON ``body``: the
        gateway hands the model that whole envelope as the tool result text (haiku-compatibility-probe.json),
        and an exception in the tool is the handler's 500 envelope. Retrieval searches the local index."""
        if self.handler is None:
            self.handler = load_handler(self.fixtures)
            self.handler.run_retrieval = lambda call: self.retrieve(str(call.get("query", "")))
        context = SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": full_name}))
        return self.handler.lambda_handler(dict(args), context)


def _body(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """The JSON body of a successful handler response; ``{}`` otherwise."""
    if envelope.get("statusCode") != 200:
        return {}
    try:
        body = json.loads(str(envelope.get("body") or "{}"))
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


# ---------------------------------------------------------------------------
# One conversation
# ---------------------------------------------------------------------------


def converse(client: Any, *, model: str, system: str, query: str, tools: Toolbox, usage: Usage,
             max_iterations: int = UPSTREAM_MAX_ITERATIONS, max_tokens: int = MAX_TOKENS) -> dict[str, Any]:
    """The agent loop of one practice question: model turns until it stops asking for tools, or the limits.

    ``answer`` is the text of every assistant turn joined with no separator, the string ``agentcore invoke --stream``
    writes and logs (``u += delta.text`` over the whole agent loop) and L1 reads: a refusal said before a tool call
    counts, and so does a banned term. A model error (:data:`MODEL_ERRORS`) ends this question only: ``stopReason``
    ``error`` with the ``error`` message, as live."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": query}]}]
    calls: list[dict[str, Any]] = []
    passages: list[str] = []
    texts: list[str] = []
    stop, error = None, None
    for turn in range(1, max_iterations + 1):
        try:
            response = _retrying(client.converse, modelId=model, system=[{"text": system}], messages=messages,
                                 toolConfig=tools.config(), inferenceConfig={"maxTokens": max_tokens})
        except Exception as exc:  # noqa: BLE001 - botocore ClientError
            if _code_error(exc) not in MODEL_ERRORS:
                raise
            stop, error = "error", str(exc)[:300]
            break
        usage.calls += 1
        usage.input_tokens += int((response.get("usage") or {}).get("inputTokens") or 0)
        usage.output_tokens += int((response.get("usage") or {}).get("outputTokens") or 0)
        message = response["output"]["message"]
        content = message.get("content") or []
        stop = response.get("stopReason")
        texts.append("".join(block.get("text", "") for block in content if "text" in block))
        uses = [block["toolUse"] for block in content if "toolUse" in block]
        if stop != "tool_use" or not uses:
            break
        # Strands drops the blank text blocks of a tool-use turn before it sends the history back (Converse rejects them).
        messages.append({**message, "content": [block for block in content if "text" not in block or block["text"].strip()]})
        results = []
        for use in uses:
            name = use["name"].split("___", 1)[1] if "___" in use["name"] else use["name"]
            envelope = tools.run(use["name"], use.get("input") or {})
            calls.append({"tool": name, "input": use.get("input") or {}})
            if name == tools.retrieval_tool and _body(envelope).get("answer"):
                passages.append(str(_body(envelope)["answer"]))
                calls[-1]["output"] = passages[-1]  # the source THELMA scores for the trace's first retrieval
                calls[-1]["sources"] = list(_body(envelope).get("sources") or [])
            text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))  # compact, as the gateway sends it
            results.append({"toolResult": {"toolUseId": use["toolUseId"], "content": [{"text": text}], "status": "success"}})
        messages.append({"role": "user", "content": results})
    else:
        stop = "max_iterations"
    # "final": the last reply, the response THELMA scores (span_adapter: the latest assistant text of the trace).
    out = {"answer": "".join(texts).strip(), "final": next((t.strip() for t in reversed(texts) if t.strip()), ""),
           "stopReason": stop, "turns": turn, "toolsCalled": calls, "passages": passages, "truncated": stop == "max_tokens"}
    return {**out, "error": error} if error else out


# ---------------------------------------------------------------------------
# Predictions (the rehearsal's rules, on local evidence)
# ---------------------------------------------------------------------------


def _l1_row(case: Mapping[str, Any], run: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    if run["stopReason"] == "error":  # l1.response_for: an invoke error in the stream wins over any text before it
        response = {"source": "error", "text": "", "error": run.get("error")}
    elif run["answer"]:
        response = {"source": "stream", "text": run["answer"], "truncated": run["truncated"]}
    elif run["truncated"]:  # l1.response_for: a cut with no text before it is the CLI's error line, not an empty reply
        response = {"source": "error", "text": "", "error": TRUNCATED_ERROR}
    else:
        response = {"source": "invoke-log", "text": ""}
    evidence = {"toolsCalled": [{"tool": c["tool"]} for c in run["toolsCalled"]], "stable": True}
    result = l1.evaluate_case(case, response, evidence, config)
    by_status: dict[str, list[str]] = {"fail": [], "defer": [], "unverified": [], "error": []}
    for check in result["checks"]:
        if check["status"] in by_status:
            by_status[check["status"]].append(check["code"])
    row = {"verdict": result["verdict"], **by_status}
    said = sorted({str(hit["needle"]) for check in result["checks"] if check["code"] == "mustNotMention" and check["status"] == "fail"
                   for hit in check.get("hits") or []})
    return {**row, "mentioned": said} if said else row


def _focus(row: Mapping[str, Any], codes: Sequence[str]) -> str:
    """pass | fail | error | unresolved, in the order of ``rehearsal._focus``: an invoke error neither holds nor breaks."""
    if any(code in row["fail"] for code in codes) or ("response" in row["fail"] and any(
            code in row["unverified"] for code in codes if code not in ("requiredTools", "forbiddenTools"))):
        return "fail"
    if row["verdict"] == "error" or any(code in row["error"] for code in codes):
        return "error"
    if any(code in row["defer"] or code in row["unverified"] for code in codes):
        return "unresolved"  # the goal judge would decide; it does not run here
    return "pass"


def _share(values: Iterable[bool]) -> float:
    values = list(values)
    return round(sum(values) / len(values), 2) if values else 0.0


def predict(data: Mapping[str, Any], runs: Mapping[str, Mapping[str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """One prediction per labs.teaching phenomenon from ``runs[phase][caseId]`` (one entry per repeat).

    tool_use / refusal / escalation: the rehearsal's focus checks (``rehearsal.FOCUS_CHECKS``) and design on
    the local L1, reproduced only when it held in every run of every case; ``unknown`` when nothing broke but a
    run it reads ended in an invoke error or one of its cases was not run (``missingCases``, e.g. ``--case``: live,
    both are insufficient evidence). prompt_fixable / retrieval_gap: ``not_predicted``.
    """
    out: list[dict[str, Any]] = []
    for phenomenon in teaching.phenomena(dict(data)):
        kind, pid = phenomenon.get("kind"), phenomenon.get("id")
        if kind in ("prompt_fixable", "retrieval_gap"):
            out.append({"id": pid, "kind": kind, "prediction": "not_predicted", "cases": [],
                        "note": "THELMA verdicts are not predicted locally; the AWS rehearsal decides them"})
            continue
        if kind not in FOCUS_CHECKS:
            continue
        design = teaching.design(dict(phenomenon))
        entry: dict[str, Any] = {"id": pid, "kind": kind, "design": design, "cases": []}
        missing: list[str] = []
        for cid in phenomenon.get("caseIds") or []:
            pairs = list(zip(runs.get("baseline", {}).get(cid, []), runs.get("optimized", {}).get(cid, [])))
            if not pairs:
                missing.append(str(cid))
                continue
            case = next((c for c in teaching.practice_cases(dict(data)) if c.get("id") == cid), {})
            codes = [code for code in FOCUS_CHECKS[kind] if (case.get("expected") or {}).get(code)]
            focus = [(_focus(b["l1"], codes), _focus(o["l1"], codes)) for b, o in pairs]
            held = [fo == "pass" and (design != "contrast" or fb == "fail") for fb, fo in focus]
            broke = [fo == "fail" or (design == "contrast" and fb == "pass") for fb, fo in focus]
            entry["cases"].append({"caseId": cid, "checks": codes, "heldShare": _share(held), "failedShare": _share(broke),
                                   "baselineFocus": [fb for fb, _ in focus], "optimizedFocus": [fo for _, fo in focus],
                                   "optimizedTools": [[c["tool"] for c in o["toolsCalled"]] for _, o in pairs]})
        if missing:
            entry["missingCases"] = missing
        errored = any("error" in c["optimizedFocus"] or (design == "contrast" and "error" in c["baselineFocus"])
                      for c in entry["cases"])
        if not entry["cases"]:
            entry["prediction"] = "unknown"
        elif any(c["failedShare"] > 0 for c in entry["cases"]):
            entry["prediction"] = "likely_not_reproduced"  # it broke in at least one run: in class, some students see that
        elif errored or missing:  # the live check needs every case (rehearsal.teaching_contrast)
            entry["prediction"] = "unknown"
        elif all(c["heldShare"] == 1.0 for c in entry["cases"]):
            entry["prediction"] = "likely_reproduced"
        else:  # no run broke, some deferred to Mind the Goal (no refusal or escalation marker in the answer)
            entry["prediction"] = "needs_goal_judge"
        out.append(entry)
    return out


def class_prediction(phenomena: Sequence[Mapping[str, Any]]) -> str:
    """``likely_ready`` / ``likely_not_ready`` for the predicted part of the class bar: every tool_use, refusal and
    escalation phenomenon reproduced and, when THELMA ran, one retrieval_gap phenomenon; prompt_fixable is left to
    the AWS rehearsal."""
    l1_kinds = [p["prediction"] for p in phenomena if p["kind"] in FOCUS_CHECKS]
    gaps = [p["prediction"] for p in phenomena if p["kind"] == "retrieval_gap" and p["prediction"] != "not_predicted"]
    if "likely_not_reproduced" in l1_kinds or (gaps and set(gaps) == {"likely_not_reproduced"}):
        return "likely_not_ready"
    if not l1_kinds or any(p != "likely_reproduced" for p in l1_kinds) or (gaps and "likely_reproduced" not in gaps):
        return "unknown"
    return "likely_ready"


def finding_scopes(doc: Mapping[str, Any]) -> list[str]:
    """The repair scopes the findings need: the L1 levers are tools, prompts and golden; a gap the first search
    covers is fixed in the knowledge base, the question and its declaration."""
    gap = any(p["kind"] == "retrieval_gap" and p.get("prediction") == "likely_not_reproduced" for p in doc.get("phenomena") or [])
    return ["tools", "prompts", "golden"] + (["knowledge", "labs"] if gap else [])


# ---------------------------------------------------------------------------
# The whole pre-rehearsal
# ---------------------------------------------------------------------------


@dataclass
class PackInputs:
    data: dict[str, Any]
    prompts: dict[str, str]           # phase → system prompt
    specs: list[dict[str, Any]]       # gateway schema
    fixtures: dict[str, Any]
    documents: dict[str, str]         # source file → text
    practice: list[dict[str, Any]]
    pack: dict[str, Any]
    skills: dict[str, str] = field(default_factory=dict)  # skill name → SKILL.md (the Harness mounts them)


def inputs_from_pack(pack_dir: Path, data: Mapping[str, Any]) -> PackInputs:
    """The compiled pack (``compiler.compile_pack`` → ``pack/``): what the release ships to the Workshop."""
    pack = json.loads((pack_dir / "pack.json").read_text(encoding="utf-8"))
    prompts = {"baseline": (pack_dir / "prompts" / "baseline.md").read_text(encoding="utf-8"),
               "optimized": (pack_dir / "prompts" / "optimization-candidate.md").read_text(encoding="utf-8")}
    docs_dir = pack_dir / "knowledge-base" / "docs"
    documents = {f"knowledge-base/docs/{p.name}": p.read_text(encoding="utf-8") for p in sorted(docs_dir.glob("*.md"))}
    return PackInputs(
        data=dict(data), prompts=prompts,
        specs=json.loads((pack_dir / "tools" / "schema.json").read_text(encoding="utf-8")),
        fixtures=json.loads((pack_dir / "tools" / "fixtures.json").read_text(encoding="utf-8")),
        documents=documents,
        practice=json.loads((pack_dir / "golden" / "practice.json").read_text(encoding="utf-8")),
        pack=pack,
        skills={p.parent.name: p.read_text(encoding="utf-8") for p in sorted((pack_dir / "skills").glob("*/SKILL.md"))},
    )


_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def skill_metadata(text: str) -> tuple[str, str] | None:
    """(name, description) from a SKILL.md's frontmatter; ``None`` without one (the Harness then skips the skill)."""
    match = _FRONTMATTER.match(text.lstrip("\ufeff"))
    if not match:
        return None
    fields = {k.strip(): v.strip().strip('"') for k, _, v in (line.partition(":") for line in match.group(1).splitlines()) if v}
    return (fields["name"], fields.get("description", "")) if fields.get("name") else None


def with_skills(prompt: str, skills: Mapping[str, str]) -> str:
    """The system prompt as the Harness sends it: the prompt, then an ``<available_skills>`` block naming each skill
    that loaded (live 2026-10-01, the runtime's gen_ai.system.message: name, description, location)."""
    entries = [(meta, folder) for folder, text in skills.items() if (meta := skill_metadata(text))]
    if not entries:
        return prompt
    block = "\n".join(f"<skill>\n<name>{name}</name>\n<description>{description}</description>\n"
                      f"<location>/mnt/skills/skills/{folder}/SKILL.md</location>\n</skill>" for (name, description), folder in entries)
    return prompt.rstrip("\n") + "\n" + f"<available_skills>\n{block}\n</available_skills>"


def run(inputs: PackInputs, client: Any, *, model: str = DEFAULT_MODEL,
        phases: Sequence[str] = PHASES, case_ids: Sequence[str] | None = None, repeat: int = 2,
        cache: Path | None = None, progress: Callable[[str], None] | None = None,
        thelma: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None, skills: bool = True) -> dict[str, Any]:
    """Run the practice questions (``case_ids``, default all) ``repeat`` times per phase and predict.

    ``thelma`` scores the probe cases' first-retrieval turns (``pre_thelma.score`` bound to the pinned evaluator
    and the judge model); without it the prompt_fixable and retrieval_gap phenomena stay ``not_predicted``."""
    usage = Usage()
    started = time.time()
    index = build_index(client, inputs.documents, usage, cache=cache)

    def retrieve(query: str) -> dict[str, Any]:
        hits = index.search(embed(client, query, usage))
        if not hits:
            return {"answer": "No relevant policy documents found for your query.", "sources": []}
        return {"answer": "\n\n".join(text for _, text, _ in hits), "sources": [source for source, _, _ in hits]}

    # The gateway target is renamed without hyphens (render: nova-compatible-target-name), so the model sees
    # "freightclaimstools___track_shipment" live (probe of 2026-10-01), not the namespace's toolTargetName.
    tools = Toolbox(target=str((inputs.pack.get("namespace") or {}).get("toolTargetName") or "tools").replace("-", ""), specs=inputs.specs,
                    fixtures=inputs.fixtures, retrieval_tool=str(inputs.pack.get("retrievalToolName") or ""), retrieve=retrieve)
    config = l1.config_from_pack(inputs.pack)
    wanted = [c for c in inputs.practice if case_ids is None or c["id"] in case_ids]
    runs: dict[str, dict[str, list[dict[str, Any]]]] = {phase: {} for phase in phases}
    for phase in phases:
        for case in wanted:
            for n in range(max(1, repeat)):
                if progress:
                    progress(f"{phase} {case['id']} ({n + 1}/{repeat})")
                system = with_skills(inputs.prompts[phase], inputs.skills) if skills else inputs.prompts[phase]
                result = converse(client, model=model, system=system, query=case["query"], tools=tools, usage=usage)
                result["l1"] = _l1_row(case, result, config)
                result["retrievalCalls"] = sum(c["tool"] == tools.retrieval_tool for c in result["toolsCalled"])
                runs[phase].setdefault(case["id"], []).append(result)
    phenomena = predict(inputs.data, runs) if set(PHASES) <= set(phases) else []
    if thelma is not None and phenomena:
        probes = [cid for p in teaching.phenomena(inputs.data, pre_thelma.THELMA_KINDS) for cid in p.get("caseIds") or []]
        if progress:
            progress(f"THELMA on the first search of {len(probes)} probe case(s)")
        questions = {c["id"]: c["query"] for c in wanted}
        gaps = [cid for p in teaching.phenomena(inputs.data, "retrieval_gap") for cid in p.get("caseIds") or [] if cid in questions]
        turns = {cid: pre_thelma.question_probe(questions[cid], retrieve(questions[cid])) for cid in gaps}
        sampled = pre_thelma.score_runs(runs, probes, tools.retrieval_tool, thelma,
                                        probes={cid: {k: v for k, v in turn.items() if k != "documents"} for cid, turn in turns.items()})
        scored = {p["id"]: p for p in pre_thelma.predict(inputs.data, runs, probes=sampled, probe_turns=turns)}
        phenomena = [scored.get(p["id"], p) for p in phenomena]
    return {
        "schema": SCHEMA,
        "packId": inputs.pack.get("packId"),
        "model": model, "repeat": max(1, repeat),
        "limits": {"maxIterations": UPSTREAM_MAX_ITERATIONS, "maxTokens": MAX_TOKENS, "chunkTokens": CHUNK_TOKENS, "topK": TOP_K},
        "prediction": class_prediction(phenomena) if phenomena else "unknown",
        "thelma": thelma is not None and bool(phenomena),
        "skills": sorted(inputs.skills) if skills else [],
        "phenomena": phenomena,
        "flags": _flags(runs, tools.retrieval_tool),
        "runs": {phase: {cid: [_brief(r) for r in rs] for cid, rs in by_case.items()} for phase, by_case in runs.items()},
        "usage": usage.as_dict(),
        "seconds": round(time.time() - started, 1),
        "note": ("a local prediction on the Workshop model: L1, and with THELMA the retrieval gaps (prompt_fixable shown "
                 "only); readyForClass is decided by the AWS rehearsal (run/rehearsal.json)"),
    }


def _flags(runs: Mapping[str, Mapping[str, list[dict[str, Any]]]], retrieval_tool: str) -> list[dict[str, Any]]:
    flags = []
    for phase, by_case in runs.items():
        for cid, results in by_case.items():
            for r in results:
                if r["stopReason"] == "error":
                    flags.append({"phase": phase, "caseId": cid, "flag": "invoke_error", "error": r.get("error")})
                elif not r["answer"] and not r["truncated"]:  # a cut with no text is an invoke error: max_tokens says so
                    flags.append({"phase": phase, "caseId": cid, "flag": "empty_reply", "stopReason": r["stopReason"], "turns": r["turns"],
                                  "retrievalCalls": r["retrievalCalls"]})
                if r["truncated"]:
                    flags.append({"phase": phase, "caseId": cid, "flag": "max_tokens"})
                if r["retrievalCalls"] > RETRIEVAL_CAP:
                    flags.append({"phase": phase, "caseId": cid, "flag": "retrieval_loop", "calls": r["retrievalCalls"]})
    return flags


def _brief(r: Mapping[str, Any]) -> dict[str, Any]:
    brief = {"answer": r["answer"][:600], "answerChars": len(r["answer"]), "stopReason": r["stopReason"], "turns": r["turns"],
             "tools": [c["tool"] for c in r["toolsCalled"]], "retrievalCalls": r["retrievalCalls"], "l1": r["l1"]}
    for key in ("thelma", "thelmaTurn", "thelmaError"):
        if r.get(key) is not None:
            brief[key] = r[key]
    return {**brief, "error": r["error"]} if r.get("error") else brief


def _names(values: Iterable[str]) -> str:
    return ", ".join(values)


def _forbidden_lever(kind: str, case_def: Mapping[str, Any], tool: str, data: Mapping[str, Any]) -> str:
    """The lever for a forbidden tool the optimized agent called. The role levers of the live rehearsal only when the
    refusal is role-gated (``teaching_policy.role_gated_refusals``), the argument one only when the tool has a role
    argument; a refusal about the request or whose data it is gets a request lever."""
    gated = {(c.get("id"), t): name for c, _, name, t in teaching_policy.role_gated_refusals(dict(data))} if kind == "refusal" else {}
    name = gated.get((case_def.get("id"), tool))
    if name is None:
        return (f"the optimized agent called {tool}: in the candidate, refuse this kind of request before calling {tool}"
                if kind == "refusal" else
                f"the optimized agent called {tool}, which this question must not use: tell the candidate not to call {tool} "
                "for this kind of question")
    gate = f"the optimized agent called {tool}, which is gated on the asker's role: "
    if teaching_policy.role_not_in_query(dict(data), dict(case_def)):  # rehearsal: L1_ROLE_NOT_IN_QUERY
        return gate + f"state in the question that the asker is {name} (the agent gets only an actor id)"
    spec = next((t for t in data.get("tools") or [] if isinstance(t, dict) and t.get("name") == tool), {})
    arg = teaching_policy.role_argument(spec)
    return gate + f"write the candidate's refusal rule for {name} and {tool}, not for 'other roles'" + (
        f", and take the caller-role argument '{arg}' out of {tool} (with it the lease-contract refusal broke in 2 of 5 "
        "local runs, without it in 0 of 5)" if arg else "")


def _rule(kind: str, expected: Mapping[str, Any]) -> str:
    """The candidate rule an L1 phenomenon of ``kind`` teaches (``rehearsal._rule``)."""
    required = [t for t in expected.get("requiredTools") or [] if isinstance(t, str)]
    forbidden = [t for t in expected.get("forbiddenTools") or [] if isinstance(t, str)]
    if kind == "tool_use":
        return f"call {_names(required)} for this kind of question" if required else "call the case's tool"
    if kind == "refusal":
        return f"refuse it and never call {_names(forbidden)}" if forbidden else "refuse it"
    return "hand it off (name the hand-off target)"


def _empty(run: Mapping[str, Any], checks: Sequence[str]) -> bool:
    """A failing run whose reply was empty and no focus check failed on what the agent did: the fix is the reply
    (rehearsal: L1_EMPTY_RESPONSE), not the rule the case checks."""
    fail = (run.get("l1") or {}).get("fail") or []
    return "response" in fail and not any(code in fail for code in checks)


def _empty_lever(runs: Sequence[Mapping[str, Any]], tool: str, rule: str | None = None) -> tuple[str, bool]:
    """The evidence and lever for empty optimized replies (``rehearsal._empty_reply_fix``): the retrieval cap only after
    more than RETRIEVAL_CAP retrieval calls, else a reply at the end of every turn. Also says whether it was the cap."""
    run = max(runs, key=lambda r: r.get("retrievalCalls") or 0)
    calls = int(run.get("retrievalCalls") or 0)
    seen = f"{run.get('stopReason')}, {run.get('turns')} turns" + (f", {calls} {tool} calls" if calls else "")
    if calls > RETRIEVAL_CAP:
        return (f"({seen}): make \"at most {RETRIEVAL_CAP} {tool} calls per question, then {rule or 'the knowledge-base hand-off'}\" "
                "the candidate's first rule"), True
    act = f"make the candidate's rule explicit ({rule}) and have it" if rule else "have the candidate"
    return f"({seen}): {act} end every turn with a reply, also when a search or tool call finds nothing", False


def _optimized_levers(kind: str, case_def: Mapping[str, Any], checks: Sequence[str], failing: Sequence[Mapping[str, Any]],
                      data: Mapping[str, Any]) -> list[str]:
    """The levers for the optimized runs whose focus failed, from what those runs did (not the passing ones)."""
    expected = case_def.get("expected") or {}
    required = [t for t in expected.get("requiredTools") or [] if isinstance(t, str)]
    forbidden = [t for t in expected.get("forbiddenTools") or [] if isinstance(t, str)]
    failed = [[code for code in checks if code in ((r.get("l1") or {}).get("fail") or [])] for r in failing]
    levers: list[str] = []
    for tool in [t for t in forbidden if any(t in (r.get("tools") or []) for r in failing)]:
        levers.append(_forbidden_lever(kind, case_def, tool, data))
    unmet = [r for r, codes in zip(failing, failed) if "requiredTools" in codes]
    if unmet:
        called = sorted({t for r in unmet for t in r.get("tools") or []})
        missing = [t for t in required if any(t not in (r.get("tools") or []) for r in unmet)]
        did = f"called {_names(called)} instead of the required {_names(missing)}" if called else "called no tool"
        levers.append(f"the optimized agent {did}: state every id the tool needs in the question, and name {_names(missing)} in "
                      "the candidate for this kind of question")
    if any("mustNotMention" in codes for codes in failed):
        said = sorted({m for r in failing for m in (r.get("l1") or {}).get("mentioned") or []})
        levers.append(f"the optimized answer said {_names(repr(m) for m in said) or 'a mustNotMention term'}: tell the candidate not "
                      "to say it for this kind of request")
    empty = [r for r in failing if _empty(r, checks)]
    if empty:
        levers.append("the optimized reply was empty " + _empty_lever(empty, _retrieval_tool(data), _rule(kind, expected))[0])
    if not levers:
        levers.append(f"the optimized run failed the {kind} checks: make the candidate's rule explicit ({_rule(kind, expected)})")
    return levers


def _retrieval_tool(data: Mapping[str, Any]) -> str:
    return next((str(t["name"]) for t in data.get("tools") or [] if isinstance(t, dict) and t.get("kind") == "retrieval"
                 and t.get("name")), "the retrieval tool")


def findings(doc: Mapping[str, Any], data: Mapping[str, Any]) -> list[str]:
    """What a repair should fix before the AWS run, each with its evidence and the lever the live runs showed.

    Each cause is its own finding, counted in its own runs: under ``contrast`` a baseline that already passed, and
    an optimized run that failed (its levers read only the failing runs). Empty when every L1 phenomenon held in
    every run and no optimized answer was empty or looped. An empty reply a phenomenon finding names is not repeated
    from its flag, and the retrieval cap is asked for once per question."""
    golden = {x.get("id"): x for x in data.get("evaluation", {}).get("goldenSet", []) if isinstance(x, dict)}
    tool = _retrieval_tool(data)
    out: list[str] = []
    named: set[str] = set()   # questions whose empty optimized reply a phenomenon finding names
    capped: set[str] = set()  # questions given the retrieval cap
    for p in doc.get("phenomena") or []:
        if p.get("prediction") != "likely_not_reproduced" or p["kind"] in pre_thelma.THELMA_KINDS:
            continue
        head = f"{p['kind']} '{p['id']}'"
        for c in p["cases"]:
            cid, runs = c["caseId"], len(c["optimizedFocus"])
            passed = sum(f == "pass" for f in c["baselineFocus"]) if p.get("design") == "contrast" else 0
            if passed:  # rehearsal: L1_BASELINE_ALREADY_PASSES (labs.teaching is not in the repair's scope)
                out.append(f"{head} broke in {passed} of {runs} local runs of '{cid}': the baseline already passed the {p['kind']} "
                           "checks there: remove the rule this contrast shows from the baseline prompt.")
            briefs = ((doc.get("runs") or {}).get("optimized") or {}).get(cid) or []
            failing = [{"tools": tools, **(briefs[i] if i < len(briefs) else {})}
                       for i, (focus, tools) in enumerate(zip(c["optimizedFocus"], c["optimizedTools"])) if focus == "fail"]
            if failing:
                levers = _optimized_levers(p["kind"], golden.get(cid) or {}, c["checks"], failing, data)
                out.append(f"{head} broke in {len(failing)} of {runs} local runs of '{cid}': {'; '.join(levers)}.")
                empty = [r for r in failing if _empty(r, c["checks"])]
                if empty:
                    named.add(cid)
                    if _empty_lever(empty, tool)[1]:
                        capped.add(cid)
    for p in doc.get("phenomena") or []:
        if p["kind"] != "retrieval_gap" or p.get("prediction") != "likely_not_reproduced":
            continue
        for c in p["cases"]:
            if c.get("prediction") != "likely_not_reproduced":
                continue
            asked = c.get("questionAsSearch") or {}
            turns = 2 * len(c["pairs"]) + len(asked.get("samples") or [])
            covered = round(c["retrievalOkShare"] * turns)
            searches = sorted({q for v in c["pairs"] for q in (v.get("firstSearch") or {}).values() if q})
            docs = sorted({d for v in c["pairs"] for d in v.get("documents") or []} | set(asked.get("documents") or []))
            what = ", ".join(f"'{q}'" for q in searches) + (" and the question itself" if asked else "")
            out.append(f"retrieval_gap '{p['id']}' would likely not reproduce: THELMA scores the agent's first search, and {covered} "
                       f"of {turns} scored first searches of '{c['caseId']}' ({what}) were covered by "
                       f"{', '.join(docs) or 'the knowledge base'}. Ask ONE question about a topic no document covers (no extra "
                       "clause, no detail of a documented topic), and keep every bait entry on an unrelated subject: no entry may "
                       "share the question's domain, answer it or say where to ask.")
    for f in doc.get("flags") or []:
        cid = f["caseId"]
        if f["phase"] != "optimized":
            continue
        if f["flag"] == "empty_reply" and cid not in named:
            text, cap = _empty_lever([f], tool)
            out.append(f"the optimized agent gave an empty reply to '{cid}' {text}.")
            if cap:
                capped.add(cid)
        elif f["flag"] == "retrieval_loop" and cid not in capped:
            out.append(f"the optimized agent called {tool} {f['calls']} times on '{cid}': cap retrieval in the candidate at "
                       f"{RETRIEVAL_CAP} calls per question, then the knowledge-base hand-off.")
            capped.add(cid)
    return list(dict.fromkeys(out))


def _thelma_pair(v: Mapping[str, Any]) -> str:
    def run(key: str) -> str:
        t = v.get(key) or {}
        return f"GR {t.get('GR')} SQC {t.get('SQC')} SP2 {t.get('SP2')}" if t else "not scored"
    search = (v.get("firstSearch") or {}).get("optimized")
    return f"{v['code']} (baseline {run('baseline')}; optimized {run('optimized')}" + (f"; first search '{search}'" if search else "") + ")"


def render_markdown(doc: Mapping[str, Any]) -> str:
    label = "Prediction (L1 and retrieval gap)" if doc.get("thelma") else "L1 prediction"
    lines = [f"# Pre-rehearsal — {doc.get('packId')}", "",
             f"{label}: **{doc['prediction']}** (model `{doc['model']}`, {doc['repeat']} run(s) per question, "
             f"{doc['usage']['converseCalls']} model calls, {doc['seconds']} s). {doc['note']}.", "",
             "| Phenomenon | Kind | Prediction | Evidence |", "|---|---|---|---|"]
    for p in doc["phenomena"]:
        if p["prediction"] == "not_predicted":
            lines.append(f"| {p['id']} | {p['kind']} | not predicted | {p['note']} |")
            continue
        if p["kind"] in pre_thelma.THELMA_KINDS:
            bits = [f"{c['caseId']}: " + (f"chance both live runs fail {c['reproduceChance']:.0%}; " if "reproduceChance" in c else "")
                    + ", ".join(_thelma_pair(v) for v in c["pairs"]) for c in p["cases"]]
            lines.append(f"| {p['id']} | {p['kind']} (local THELMA) | {p['prediction']} | {'; '.join(bits)} |")
            continue
        bits = [f"{c['caseId']}: held {c['heldShare']:.0%}, " + (f"baseline {c['baselineFocus']}, " if p["design"] == "contrast" else "")
                + f"optimized {c['optimizedFocus']}, tools {c['optimizedTools']}" for c in p["cases"]]
        bits += [f"{cid}: not run" for cid in p.get("missingCases") or []]
        lines.append(f"| {p['id']} | {p['kind']} ({p['design']}) | {p['prediction']} | {'; '.join(bits)} |")
    if doc.get("findings"):
        lines += ["", "To fix before the AWS run:", ""] + [f"- {f}" for f in doc["findings"]]
    if doc["flags"]:
        lines += ["", "What students would see:", ""]
        for f in doc["flags"]:
            detail = f.get("calls") or f.get("error") or f.get("stopReason")
            lines.append(f"- {f['phase']} `{f['caseId']}`: {f['flag']}" + (f" ({detail})" if detail else ""))
    return "\n".join(lines) + "\n"
