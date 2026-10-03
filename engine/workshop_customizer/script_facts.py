"""Script facts: the ONE derivation of what the rendered Guide scripts ask, count and name.

``render._patch_scripts`` writes the practice-related parts of 06/09/10/11 from a
:class:`ScriptFacts`, and the Workshop Guide (P3) describes the same object, so the scripts and the
guide cannot drift. :func:`verify_rendered` re-parses the final release scripts and reports any
disagreement; render treats one as a hard failure.

Facts are pure data derived from a loaded scenario (no I/O in :func:`compute`):

* the first conversation 06 asks (query, actor, label, unknown-context notice);
* the eval cases in the order 09/10/12 ask them — the ``GOLDEN_QUERIES / GOLDEN_LABELS /
  GOLDEN_IDS / GOLDEN_ACTORS / GOLDEN_PROBE`` arrays — with each case's persona and probe flag;
* the probe count (09 ``want`` and ``N``, 10 ``--eval-only N`` and its trace wait) and 09/11
  ``RECENT_N``;
* namespace-derived names (agent, memory, gateway, target, compact target, Lambda, KB, SSM prefix,
  retrieval tool, the THELMA retrieval-span filter, evaluator ids) and skills;
* ``features``: evaluation features the release implements (vocabulary :data:`FEATURES`).

The derivation is the teaching runtime render emits (SPEC D3): ``labs.teaching``-driven
:func:`teaching.eval_order`, probe flags from P, probe count = ``RECENT_N`` = |P|, per-case personas
(the scripts derive a fresh runtime actor ``${GOLDEN_ACTORS[i]}-${RUN_TAG}-q$((i+1))``), and 06 from
``labs.teaching.firstConversation`` (``practice[0].query`` as the template's actor when none is declared).

The 06 console lines (:func:`first_conversation_topic`, :func:`memory_notice`) are built here so
render writes and :func:`verify_rendered` checks the same text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import teaching

#: Evaluation features a release may implement (RELEASE.json ``evaluationFeatures``, guide wording).
FEATURES: frozenset[str] = frozenset({"l1-scenario-assertions/1", "mtg-scenario-policy/1", "per-case-actors/1"})

#: Values the pinned upstream scripts hard-code (06 actor, 13 RUNS default).
UPSTREAM_FIRST_ACTOR = "employee-001"
UPSTREAM_STABILITY_RUNS = 3
#: 04-deploy.sh ``npx agentcore create --max-iterations``: render keeps the flag and no scenario field sets it, so
#: every release's harness ends a turn after this many agent iterations (rehearsal names it on an empty reply).
UPSTREAM_MAX_ITERATIONS = 30
#: 09's evaluation retry budget (WORKSHOP_EVAL_ATTEMPTS x WORKSHOP_EVAL_RETRY_SECONDS defaults). Render writes
#: it into 09 and the guide prints it in the retry-message table; verify_rendered checks they agree.
EVAL_ATTEMPTS = 10
EVAL_RETRY_SECONDS = 30
DEFAULT_JUDGE_MODEL = "us.amazon.nova-2-lite-v1:0"

#: SPEC D3 run-record contract (teaching semantics): RUN_TAG = <phase>-<epoch>, per-case runtime
#: actor computed in bash, records under the eval-runs root (never inside the release dir).
RUNTIME_ACTOR_EXPR = "${GOLDEN_ACTORS[$i]}-${RUN_TAG}-q$((i+1))"
EVAL_RUNS_ROOT_EXPR = "${WORKSHOP_ROOT:-$HOME/workshop}/eval-runs"


@dataclass(frozen=True)
class CaseRun:
    """One practice question as the eval scripts ask it."""

    index: int  # 0-based position in the eval order (scripts print Q{index+1})
    case_id: str
    label: str
    query: str
    category: str
    provenance: str
    actor_id: str  # persona the script asks as (GOLDEN_ACTORS[index])
    probe: bool  # its retrieval trace is waited for and THELMA-scored (GOLDEN_PROBE[index])
    requires_retrieval: bool  # expected.requiredTools names the retrieval tool


@dataclass(frozen=True)
class FirstConversation:
    """What 06-test-conversation.sh asks (Guide steps 8 and 10)."""

    query: str
    actor_id: str
    source: str  # "teaching" (labs.teaching.firstConversation) | "practice" (fallback: practice[0])
    case_id: str | None = None  # "practice" only: the practice case whose query is reused
    label: str | None = None
    unknown_context: tuple[str, ...] = ()
    must_not_mention: tuple[str, ...] = ()
    teaching_point: str | None = None
    language: str | None = None  # the pack language; picks the 06 console wording (see console_language)


@dataclass(frozen=True)
class ScriptFacts:
    pack_id: str
    display_name: str
    language: str
    teaching_declared: bool
    first_conversation: FirstConversation | None
    eval_cases: tuple[CaseRun, ...]
    probe_count: int  # 09 want / N, 10 --eval-only N and its span wait
    recent_n: int  # 09 and 11 RECENT_N
    agent_name: str
    gateway_name: str
    target_name: str
    lambda_name: str
    kb_name: str
    kb_prefix: str
    ssm_prefix: str
    retrieval_tool: str
    judge_model: str
    skills: tuple[str, ...]
    features: frozenset[str] = frozenset()
    stability_runs: int = UPSTREAM_STABILITY_RUNS  # 13 RUNS default

    def __post_init__(self) -> None:
        unknown = set(self.features) - FEATURES
        if unknown:
            raise ValueError(f"unknown evaluation feature(s): {', '.join(sorted(unknown))}")
        if [c.index for c in self.eval_cases] != list(range(len(self.eval_cases))):
            raise ValueError("eval case indexes must be 0..n-1 in order")
        if self.probe_count != sum(c.probe for c in self.eval_cases):
            raise ValueError("probe_count must equal the number of probe cases")

    # -- names ---------------------------------------------------------------

    @property
    def memory_name(self) -> str:
        return f"{self.agent_name}memory"

    @property
    def compact_target(self) -> str:
        """Gateway target as deployed (hyphens removed; trace tool prefix ``<compact>___``)."""
        return self.target_name.replace("-", "")

    @property
    def retrieval_span(self) -> str:
        """The aws/spans filter 09/10 use to find retrieval traces."""
        return f"execute_tool {self.compact_target}___{self.retrieval_tool}"

    @property
    def thelma_evaluator(self) -> str:
        return f"{self.agent_name}_thelma_rag_quality"

    @property
    def mtg_evaluator(self) -> str:
        return f"{self.agent_name}_mtg_goal_success"

    @property
    def eval_runs_root(self) -> str:
        """Bash expression of this deployment's run-record root (``<root>/<RUN_TAG>/``)."""
        return f"{EVAL_RUNS_ROOT_EXPR}/{self.agent_name}"

    # -- eval arrays (eval order) ---------------------------------------------

    @property
    def golden_queries(self) -> tuple[str, ...]:
        return tuple(c.query for c in self.eval_cases)

    @property
    def golden_labels(self) -> tuple[str, ...]:
        return tuple(c.label for c in self.eval_cases)

    @property
    def golden_ids(self) -> tuple[str, ...]:
        return tuple(c.case_id for c in self.eval_cases)

    @property
    def golden_actors(self) -> tuple[str, ...]:
        return tuple(c.actor_id for c in self.eval_cases)

    @property
    def golden_probe(self) -> tuple[int, ...]:
        return tuple(int(c.probe) for c in self.eval_cases)

    @property
    def probe_case_ids(self) -> tuple[str, ...]:
        return tuple(c.case_id for c in self.eval_cases if c.probe)

    @property
    def stability_case_id(self) -> str | None:
        """The last probe asked: the retrieval trace 13-judge-stability re-scores by default."""
        probes = self.probe_case_ids
        return probes[-1] if probes else None


# ---------------------------------------------------------------------------
# 06 console lines (render writes them, verify_rendered checks them)
# ---------------------------------------------------------------------------


def _is_ascii(text: str) -> bool:
    return all(ch.isascii() for ch in text)


def _items(fc: FirstConversation) -> list[str]:
    return [i.strip() for i in fc.unknown_context if i and i.strip()]


def console_language(fc: FirstConversation) -> str:
    """``"zh"``, ``"en"`` or ``"legacy"``: the wording of 06's topic and memory-notice lines.

    The pack language decides: an ``en`` (any non-``zh-CN``) pack always prints English, whatever
    characters its label or items contain (a curly apostrophe, 'Café', '°'). A ``zh-CN`` pack prints
    Chinese unless its label and every unknownContext item are ASCII — the upstream Workshop's own
    English lines, which hr-default reproduces. Without a language (``"legacy"``: a
    FirstConversation built by hand) each line falls back to its own text's script.
    """
    if fc.language is None:
        return "legacy"
    if fc.language != "zh-CN":
        return "en"
    texts = [(fc.label or "").strip(), *_items(fc)]
    return "en" if all(_is_ascii(t) for t in texts) else "zh"


def first_conversation_topic(fc: FirstConversation) -> str:
    """The line 06 prints before asking (upstream: '🗣️  Asking about annual leave policy...')."""
    label = (fc.label or "").strip()
    lang = console_language(fc)
    if lang == "zh" or (lang == "legacy" and label and not _is_ascii(label)):
        return f"🗣️  正在询问：{label or '你的第一个问题'}..."
    return f"🗣️  Asking about {label or 'your first question'}..."


def memory_notice(fc: FirstConversation) -> str:
    """06's closing notice: what the agent cannot know yet (labs.teaching.firstConversation.unknownContext).

    English joins items as 'a, b, or c' (hr-default reproduces the upstream sentence on one line);
    Chinese (:func:`console_language`) joins them with '、'.
    """
    items = _items(fc)
    lang = console_language(fc)
    zh = lang == "zh" or (lang == "legacy" and bool(items) and not all(_is_ascii(i) for i in items))
    if not items:
        if zh:
            return "提示：回答是通用的——Agent 还不了解你，也不记得你之前的问题。"
        return "Notice: The answer is GENERIC — the Agent doesn't know anything about you or your earlier questions yet."
    if zh:
        return f"提示：回答是通用的——Agent 还不知道你的{'、'.join(items)}。"
    if len(items) == 1:
        joined = items[0]
    elif len(items) == 2:
        joined = f"{items[0]} or {items[1]}"
    else:
        joined = ", ".join(items[:-1]) + f", or {items[-1]}"
    return f"Notice: The answer is GENERIC — the Agent doesn't know your {joined} yet."


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def _requires_retrieval(case: dict[str, Any], retrieval_tool: str) -> bool:
    return retrieval_tool in ((case.get("expected") or {}).get("requiredTools") or [])


def _case_run(index: int, case: dict[str, Any], *, actor: str, probe: bool, retrieval_tool: str) -> CaseRun:
    return CaseRun(
        index=index,
        case_id=case["id"],
        label=case["label"],
        query=case["query"],
        category=case["category"],
        provenance=case.get("provenance", ""),
        actor_id=actor,
        probe=probe,
        requires_retrieval=_requires_retrieval(case, retrieval_tool),
    )


def _practice_first_conversation(practice: list[dict[str, Any]], language: str | None = None) -> FirstConversation | None:
    if not practice:
        return None
    return FirstConversation(query=practice[0]["query"], actor_id=UPSTREAM_FIRST_ACTOR, source="practice",
                             case_id=practice[0]["id"], language=language)


def _declared_first_conversation(data: dict[str, Any]) -> FirstConversation | None:
    fc = teaching.first_conversation(data)
    if fc is None:
        return None
    return FirstConversation(
        query=fc["query"],
        actor_id=fc["actorId"],
        source="teaching",
        label=fc.get("label"),
        unknown_context=tuple(fc.get("unknownContext") or ()),
        must_not_mention=tuple(fc.get("mustNotMention") or ()),
        teaching_point=fc.get("teachingPoint"),
        language=str(data.get("language") or "") or None,
    )


def compute(data: dict[str, Any]) -> ScriptFacts:
    """Derive the script facts of a loaded scenario."""
    ns = data["namespace"]
    evaluation = data["evaluation"]
    retrieval_tool = evaluation["retrievalToolName"]
    practice = teaching.practice_cases(data)
    probes = set(teaching.probe_case_ids(data))
    cases = tuple(
        _case_run(i, c, actor=c["actorId"], probe=c["id"] in probes, retrieval_tool=retrieval_tool)
        for i, c in enumerate(teaching.eval_order(data))
    )
    first = _declared_first_conversation(data) or _practice_first_conversation(practice, str(data.get("language") or "") or None)

    return ScriptFacts(
        pack_id=str(data["id"]),
        display_name=str(data.get("displayName", data["id"])),
        language=str(data.get("language", "")),
        teaching_declared=teaching.declared(data),
        first_conversation=first,
        eval_cases=cases,
        probe_count=sum(c.probe for c in cases),
        recent_n=len(probes),
        agent_name=ns["agentName"],
        gateway_name=ns["gatewayName"],
        target_name=ns["toolTargetName"],
        lambda_name=ns["lambdaFunctionName"],
        kb_name=ns["knowledgeBaseName"],
        kb_prefix=ns["kbPrefix"],
        ssm_prefix=ns["ssmParameterPrefix"],
        retrieval_tool=retrieval_tool,
        judge_model=evaluation.get("judgeModel", DEFAULT_JUDGE_MODEL),
        skills=tuple(s["name"] for s in data.get("skills", [])),
        # The teaching render ships per-case runtime actors, the L1 checker (l1_eval.py) and the
        # scenario-policy Mind-the-Goal judge prompt together.
        features=FEATURES,
    )


# ---------------------------------------------------------------------------
# Verification against the rendered release
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r'\s*(?:"((?:[^"\\]|\\.)*)"|([^\s()"\\]+)|(\)))', re.S)
_UNESCAPE = re.compile(r'\\([\\"$`])')


def bash_unquote(inner: str) -> str:
    """Inverse of ``render.bash_quote`` for the inside of a double-quoted bash literal."""
    return _UNESCAPE.sub(r"\1", inner)


def bash_arrays(text: str, name: str) -> list[list[str] | None]:
    """Every ``NAME=( ... )`` definition at line start, parsed into words (``None`` if malformed).

    Handles the multi-line and one-line forms, double-quoted elements and bare words.
    """
    found: list[list[str] | None] = []
    for match in re.finditer(rf"^{re.escape(name)}=\(", text, re.M):
        pos, items = match.end(), []
        while True:
            token = _TOKEN.match(text, pos)
            if token is None:
                items = None
                break
            pos = token.end()
            if token.group(3):
                break
            items.append(bash_unquote(token.group(1)) if token.group(1) is not None else token.group(2))
        found.append(items)
    return found


def _one(problems: list[str], where: str, what: str, values: list[Any], expected: Any) -> None:
    if len(values) != 1:
        problems.append(f"{where}: {what} defined {len(values)} times, expected once")
        return
    actual = values[0]
    if actual == expected:
        return
    if actual is None:
        problems.append(f"{where}: {what} could not be parsed")
    elif isinstance(actual, list) and isinstance(expected, list):
        if len(actual) != len(expected):
            problems.append(f"{where}: {what} has {len(actual)} entries, script facts say {len(expected)}")
        else:
            i = next(i for i, (a, e) in enumerate(zip(actual, expected)) if a != e)
            problems.append(f"{where}: {what}[{i}] is {actual[i]!r}, script facts say {expected[i]!r}")
    else:
        problems.append(f"{where}: {what} is {actual!r}, script facts say {expected!r}")


def _ints(pattern: str, text: str, flags: int = re.M) -> list[int]:
    return [int(v) for v in re.findall(pattern, text, flags)]


def verify_rendered(release_dir: Path | str, facts: ScriptFacts) -> list[str]:
    """Parse the rendered 06/09/10/11/13 (and the shipped practice set) against ``facts``.

    Returns human-readable disagreements; an empty list means the scripts do what the facts say.
    """
    release = Path(release_dir)
    problems: list[str] = []

    def read(rel: str) -> str:
        path = release / rel
        if not path.is_file():
            problems.append(f"{rel}: missing from the release")
            return ""
        return path.read_text(encoding="utf-8")

    s06 = read("06-test-conversation.sh")
    if facts.first_conversation is not None:
        fc = facts.first_conversation
        _one(problems, "06-test-conversation.sh", "--actor-id", re.findall(r'--actor-id "([^"]*)"', s06), fc.actor_id)
        queries = [bash_unquote(q) for q in re.findall(r'--stream \\\n[ \t]*"((?:[^"\\]|\\.)*)" 2>&1\)', s06, re.S)]
        _one(problems, "06-test-conversation.sh", "first-conversation query", queries, fc.query)
        echoed = [bash_unquote(v) for v in re.findall(r'^echo "((?:[^"\\]|\\.)*)"$', s06, re.M)]
        for what, line in (("first-conversation topic", first_conversation_topic(fc)), ("memory notice", memory_notice(fc))):
            if echoed.count(line) != 1:
                problems.append(f"06-test-conversation.sh: {what} line {line!r} printed {echoed.count(line)} times, expected once")

    arrays = [
        ("GOLDEN_QUERIES", list(facts.golden_queries)), ("GOLDEN_LABELS", list(facts.golden_labels)),
        ("GOLDEN_IDS", list(facts.golden_ids)), ("GOLDEN_ACTORS", list(facts.golden_actors)),
        ("GOLDEN_PROBE", [str(p) for p in facts.golden_probe]),
    ]
    for rel in ("09-run-eval.sh", "10-optimize-prompt.sh"):
        text = read(rel)
        for name, expected in arrays:
            _one(problems, rel, name, bash_arrays(text, name), expected)
        # One fresh runtime actor per case and run, computed in bash; one run-record root.
        if text.count(RUNTIME_ACTOR_EXPR) != 1:
            problems.append(f"{rel}: runtime actor {RUNTIME_ACTOR_EXPR} appears {text.count(RUNTIME_ACTOR_EXPR)} times, expected once")
        if re.findall(r'^ACTOR_ID=', text, re.M):
            problems.append(f"{rel}: a shared ACTOR_ID line remains")
        _one(problems, rel, "EVAL_ROOT", re.findall(r'^EVAL_ROOT="([^"]*)"$', text, re.M), facts.eval_runs_root)

    s09 = read("09-run-eval.sh")
    _one(problems, "09-run-eval.sh", "want", _ints(r'local want="(\d+)"', s09), facts.probe_count)
    _one(problems, "09-run-eval.sh", "N", _ints(r"^[ \t]*N=(\d+)[ \t]*$", s09), facts.probe_count)
    _one(problems, "09-run-eval.sh", "RECENT_N", _ints(r"^RECENT_N=(\d+)\b", s09), facts.recent_n)
    s10 = read("10-optimize-prompt.sh")
    _one(problems, "10-optimize-prompt.sh", "--eval-only", _ints(r'^"\$SCRIPT_DIR/09-run-eval\.sh" --eval-only (\d+)$', s10), facts.probe_count)
    _one(problems, "10-optimize-prompt.sh", "retrieval span wait", _ints(r'\[ "\$\{CNT:-0\}" -ge (\d+) \]', s10), facts.probe_count)
    s11 = read("11-cost-latency.sh")
    _one(problems, "11-cost-latency.sh", "RECENT_N", _ints(r"^RECENT_N=(\d+)\b", s11), facts.recent_n)
    budget = re.findall(r'local attempts="\$\{WORKSHOP_EVAL_ATTEMPTS:-(\d+)\}" pause="\$\{WORKSHOP_EVAL_RETRY_SECONDS:-(\d+)\}"', s09)
    if budget != [(str(EVAL_ATTEMPTS), str(EVAL_RETRY_SECONDS))]:
        problems.append(f"09-run-eval.sh: evaluation retry budget {budget}, expected one "
                        f"{EVAL_ATTEMPTS} x {EVAL_RETRY_SECONDS}s (the guide's retry messages)")
    s13 = read("13-judge-stability.sh")
    _one(problems, "13-judge-stability.sh", "RUNS default", _ints(r'^RUNS="\$\{2:-(\d+)\}"', s13), facts.stability_runs)

    practice_path = release / "pack" / "golden" / "practice.json"
    if practice_path.is_file():
        shipped = [c.get("id") for c in json.loads(practice_path.read_text(encoding="utf-8"))]
        if sorted(shipped) != sorted(facts.golden_ids):
            problems.append("pack/golden/practice.json: practice case ids differ from the eval cases")
    else:
        problems.append("pack/golden/practice.json: missing from the release")
    return problems
