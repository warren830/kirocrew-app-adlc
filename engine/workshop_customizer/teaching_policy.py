"""Teaching-skeleton policy: the ``teaching.*`` (and THELMA ``tools.retrieval_marker_collision``) rules.

A pack must reproduce the HR Workshop's teaching contrasts in its own domain, and the pack cannot
lower that bar: ``labs.teaching`` is required (``teaching.missing`` is an error) and every rule
below is engine policy. The rules are deterministic checks on the declaration and, when the
scenario ``root`` is given, on the files it references (prompts and knowledge documents). They
guarantee design intent, not runtime behaviour; rehearsal is the runtime proof.

Rule groups (designs[0] §C, with the reviews[0] corrections):

* **Always** — THELMA's span adapter treats any tool span whose name contains ``retrieve``,
  ``knowledge``, ``kb_`` or the retrieval tool's own name (render writes it into the adapter's
  marker list) as retrieval: no mock tool span may look like that
  (``tools.retrieval_marker_collision``). ``teaching.missing`` stops the teaching rules.
* **Structure** (no files) — probes (practice cases of prompt_fixable ∪ retrieval_gap), their
  counts and shape, the first conversation, defects, the stability case, kind coverage, the
  tool_use / refusal / escalation cases, noise flags and the practice set. Three warnings go beyond
  designs[0] (reviews[0] "missing": the holdout stays an unseen check):
  ``teaching.probe_near_holdout`` (a probe shares :data:`NEAR_HOLDOUT_SHARED_WORDS` subject words
  with a holdout query), ``teaching.gap_near_holdout`` (an absent/bait term occurs in one) and
  ``teaching.prose_names_holdout`` (student-facing prose — labs.observations, the first
  conversation, teachingPoints — names the holdout set or a holdout case id or label).
* **Files** (only with ``root``; every production caller passes it) — :data:`FILE_RULE_CODES`:
  the baseline/candidate prompt checks and the lexical knowledge-base checks (answers present,
  bait present and apart from answers, absent facts really absent, noise next to answers).

Probe actors are not policed: the runtime asks every practice case as its own fresh actor
(``<actorId>-<RUN_TAG>-q<i>``), so sharing an authored ``actorId`` cannot leak Memory
(reviews[0]: C1's ``probe_actor_shared`` / ``first_actor_collision`` are dropped).

Every finding names ids, so a repair loop can hand the message back to the generator verbatim,
and carries ``scopes`` (the scenario sections a fix touches).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from . import teaching
from .teaching import contains_term, count_term, norm
from .validator import Finding, ValidationReport

MIN_PROBES = 3
MAX_PROBES = 6
MIN_FIXABLE = 2
MIN_GAPS = 1
MAX_PRACTICE = 8
BAIT_MIN_OCCURRENCES = 3
#: Mirrors upstream evaluators/thelma_eval/span_adapter.py RETRIEVE_TOOL_MARKERS (generic part).
RETRIEVAL_MARKERS: tuple[str, ...] = ("retrieve", "knowledge", "kb_")
#: Tool-name words that say nothing about the tool's subject (ignored by probe_tool_overlap).
TOOL_TOKEN_STOPWORDS = frozenset({
    "get", "check", "lookup", "look", "find", "search", "query", "fetch", "list", "create", "submit",
    "request", "update", "set", "info", "data", "status", "my", "own",
})
PROBE_CATEGORIES = ("normal", "boundary")
#: A probe sharing this many content words with a holdout query rehearses the holdout (warning).
NEAR_HOLDOUT_SHARED_WORDS = 3
#: Words that carry no subject when comparing a probe with a holdout question.
QUESTION_STOPWORDS = TOOL_TOKEN_STOPWORDS | frozenset({
    "the", "and", "what", "how", "for", "can", "does", "are", "you", "your", "with", "this", "that", "need",
    "after", "before", "from", "long", "who", "which", "when", "where", "why", "will", "there", "have", "has",
    "into", "about", "should", "must", "any", "not", "our", "its", "their", "they", "them", "then", "than",
    "also", "just", "now", "right", "out", "too", "very", "all", "been", "was", "were", "would", "could",
})
#: Words that say nothing about a question's subject once it is compared with a whole document
#: (:func:`topic_share`): a long document holds most of them somewhere. Review 2026-09-29: "days", "many",
#: "company", "year" and "office" made English gap questions look covered by unrelated IT documents. Kept apart
#: from :data:`QUESTION_STOPWORDS`, which the question-to-question and question-to-paragraph rules are calibrated on.
COMMON_WORDS = frozenset({
    "may", "might", "shall", "did", "doing", "done", "had", "being", "but", "nor", "yet", "yes", "please",
    "him", "her", "his", "she", "mine", "yours", "ours", "these", "those", "whom", "whose", "one", "ones",
    "something", "anything", "nothing", "everything", "someone", "anyone", "everyone", "myself", "yourself",
    "because", "though", "although", "whether", "unless", "while", "since", "until", "during", "within",
    "without", "over", "under", "off", "onto", "upon", "via", "between", "through", "across", "against", "here",
    "even", "really", "maybe", "actually", "currently", "usually", "normally", "typically", "generally",
    "exactly", "automatically", "still", "again", "ever", "never", "always", "already", "often", "soon",
    "later", "once", "twice", "ago", "first", "last", "next", "longer",
    "many", "much", "each", "every", "some", "more", "most", "less", "few", "other", "others", "another", "same",
    "such", "only", "both", "either", "per", "several", "enough", "whole", "lot", "lots", "total",
    "day", "days", "week", "weeks", "month", "months", "year", "years", "time", "times", "today", "tomorrow",
    "yesterday", "hour", "hours", "minute", "minutes", "daily", "weekly", "monthly", "yearly",
    "give", "gives", "given", "got", "gets", "getting", "take", "takes", "taken", "taking", "make", "makes",
    "made", "use", "used", "uses", "using", "know", "want", "wants", "needs", "like", "tell", "say", "see",
    "keep", "put", "come", "comes", "goes", "going", "let", "able", "allow", "allowed", "happen", "happens",
    "mean", "means", "apply", "applies", "bring", "bringing", "stay", "try", "ask", "open", "new", "personal",
    "available", "possible", "different", "general", "special",
    "company", "office", "people", "person", "thing", "things", "way", "ways", "number", "amount", "kind",
    "type", "business", "staff", "paid", "work", "works", "working", "policy", "policies", "rule", "rules",
    "process",
})
#: Kinds every pack must declare, and the Workshop moment each one carries.
REQUIRED_KINDS: tuple[tuple[str, str], ...] = (
    ("noise_grounding", "the Guide's noisy knowledge base"),
    ("tool_use", "tool use for the user's own data"),
    ("refusal", "a prohibited request that must be refused"),
)
#: Codes that need the scenario root (prompt and knowledge-document text).
FILE_RULE_CODES = frozenset({
    "teaching.baseline_no_retrieval_hint",
    "teaching.candidate_no_retrieval_hint",
    "teaching.defect_fix_missing",
    "teaching.defect_already_fixed",
    "teaching.defect_marker_missing",
    "teaching.defect_marker_kept",
    "teaching.fixable_answer_not_in_kb",
    "teaching.buried_answer_not_in_kb",
    "teaching.bait_missing",
    "teaching.bait_weak",
    "teaching.bait_near_answer",
    "teaching.absent_term_in_kb",
    "teaching.gap_question_in_kb",
    "teaching.gap_topic_documented",
    "teaching.refusal_rule_names_no_role",
    "teaching.noise_not_coretrieved",
})

MISSING_MESSAGE = (
    "labs.teaching is required: declare firstConversation, baselineDefects and phenomena so the Workshop "
    "reproduces the HR teaching contrasts (first conversation, prompt-fixable vs retrieval-gap probes, "
    "tool and refusal cases)"
)

# Scopes per rule family (validator.SCOPES vocabulary).
_S_TOOLS = ("tools",)
_S_GOLDEN = ("golden",)
_S_LABS = ("labs",)
_S_PROBES = ("golden", "labs")
_S_PROMPTS = ("prompts", "labs")
_S_KB = ("knowledge",)
_S_KB_LABS = ("knowledge", "labs")
_S_KB_GOLDEN = ("knowledge", "golden")


class _Report:
    def __init__(self) -> None:
        self.report = ValidationReport()

    def error(self, code: str, message: str, path: str | None, scopes: tuple[str, ...]) -> None:
        self.report.findings.append(Finding("error", code, message, path=path, scopes=scopes))

    def warning(self, code: str, message: str, path: str | None, scopes: tuple[str, ...]) -> None:
        self.report.findings.append(Finding("warning", code, message, path=path, scopes=scopes))


def _case_path(cid: str) -> str:
    return f"evaluation.goldenSet.{cid}"


def _phenomenon_path(pid: Any) -> str:
    return f"labs.teaching.phenomena.{pid}"


def _expected(case: dict[str, Any]) -> dict[str, Any]:
    value = case.get("expected")
    return value if isinstance(value, dict) else {}


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _unique(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def compact_target(data: dict[str, Any]) -> str:
    """The Gateway target prefix of tool spans (render compacts hyphens away)."""
    return str((data.get("namespace") or {}).get("toolTargetName") or "").replace("-", "")


def tool_tokens(name: str) -> set[str]:
    return {w for w in name.lower().split("_") if len(w) >= 3} - TOOL_TOKEN_STOPWORDS


def query_tokens(query: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", norm(query)))


def content_words(query: str) -> set[str]:
    """Subject words of a question (ASCII words of 3+ characters minus :data:`QUESTION_STOPWORDS`)."""
    return query_tokens(query) - QUESTION_STOPWORDS


def subject_words(text: str) -> set[str]:
    """:func:`content_words` minus :data:`COMMON_WORDS`: what a question is about, to compare with a document."""
    return content_words(text) - COMMON_WORDS


def fixture_values(tool: dict[str, Any]) -> list[str]:
    """String values (≥ 3 chars) of a mock tool's fixture ``when`` conditions, in declaration order."""
    fixtures = tool.get("fixtures") if isinstance(tool.get("fixtures"), dict) else {}
    values: list[str] = []
    for key in ("cases", "errors"):
        for row in fixtures.get(key) or []:
            when = row.get("when") if isinstance(row, dict) else None
            if isinstance(when, dict):
                values += [v for v in when.values() if isinstance(v, str) and len(v) >= 3]
    return _unique(values)


#: A fixture value that identifies a record (has a digit: "LC-2026-0142", "S2023-0417"), unlike a category
#: ("annual" leave), which a question may name without identifying the asker.
_RECORD_ID = re.compile(r"\d")


def _default_is_error(tool: dict[str, Any]) -> bool:
    fixtures = tool.get("fixtures") if isinstance(tool.get("fixtures"), dict) else {}
    default = fixtures.get("default")
    return isinstance(default, dict) and "error" in default


# ---------------------------------------------------------------------------
# Always: THELMA retrieval markers
# ---------------------------------------------------------------------------


def _marker_rules(data: dict[str, Any], out: _Report) -> None:
    tools = [t for t in data.get("tools") or [] if isinstance(t, dict) and isinstance(t.get("name"), str)]
    compact = compact_target(data)
    rt = (data.get("evaluation") or {}).get("retrievalToolName")
    markers = RETRIEVAL_MARKERS + ((rt.lower(),) if isinstance(rt, str) and rt else ())
    target_markers = [m for m in markers if m in compact.lower()]
    mocks = [t for t in tools if t.get("kind") == "mock"]
    if target_markers and mocks:
        target = (data.get("namespace") or {}).get("toolTargetName")
        out.error(
            "tools.retrieval_marker_collision",
            f"namespace.toolTargetName '{target}' contains '{target_markers[0]}', so every mock tool span "
            f"('execute_tool {compact}___<tool>') looks like retrieval to THELMA's span adapter; rename the tool target",
            "namespace.toolTargetName", _S_TOOLS,
        )
    for tool in mocks:
        found = [m for m in markers if m in tool["name"].lower()]
        if found:
            out.error(
                "tools.retrieval_marker_collision",
                f"mock tool '{tool['name']}' contains '{found[0]}', which THELMA's span adapter treats as a retrieval "
                "tool; its output would be scored as knowledge-base sources. Rename it (e.g. lookup_*, get_*)",
                f"tools.{tool['name']}", _S_TOOLS,
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def check_teaching(data: dict[str, Any], root: Path | str | None = None) -> ValidationReport:
    """Every ``teaching.*`` / ``tools.retrieval_marker_collision`` finding for ``data``.

    Structure rules always run; file rules (:data:`FILE_RULE_CODES`) run only with ``root``.
    """
    out = _Report()
    _marker_rules(data, out)
    block = teaching.block(data)
    if block is None:
        out.error("teaching.missing", MISSING_MESSAGE, "labs.teaching", ("golden", "knowledge", "prompts", "labs"))
        return out.report
    ctx = _Context(data, block)
    _structure_rules(ctx, out)
    if root is not None:
        _file_rules(ctx, Path(root), out)
    return out.report


class _Context:
    """Derived sets shared by the rules (computed once per check)."""

    def __init__(self, data: dict[str, Any], block: dict[str, Any]) -> None:
        self.data = data
        self.block = block
        evaluation = data.get("evaluation") or {}
        self.rt = evaluation.get("retrievalToolName")
        self.cases = [c for c in evaluation.get("goldenSet") or [] if isinstance(c, dict) and "id" in c]
        self.by_id = {c["id"]: c for c in self.cases}
        self.practice = teaching.practice_cases(data)
        self.practice_ids = {c["id"] for c in self.practice}
        self.holdout = [c for c in self.cases if c.get("set") == "holdout"]
        self.phenomena = teaching.phenomena(data)
        self.tools = {t["name"]: t for t in data.get("tools") or [] if isinstance(t, dict) and isinstance(t.get("name"), str)}
        self.mocks = [t for t in self.tools.values() if t.get("kind") == "mock"]
        self.probes = list(teaching.probe_case_ids(data))
        self.fixable = self._practice_ids_of("prompt_fixable")
        self.gaps = self._practice_ids_of("retrieval_gap")
        self.buried = [p for p in teaching.phenomena(data, "retrieval_gap") if p.get("mechanism") == "buried"]
        self.buried_ids = {cid for p in self.buried for cid in _strings(p.get("caseIds"))}
        self.documents = [d for d in (data.get("knowledge") or {}).get("documents") or [] if isinstance(d, dict) and "id" in d]
        self.docs_by_id = {d["id"]: d for d in self.documents}
        self.noise_ids = [d["id"] for d in self.documents if d.get("noise") is True]

    def _practice_ids_of(self, kind: str) -> list[str]:
        """Practice case ids listed by ``kind`` phenomena, in goldenSet order."""
        listed = {cid for p in teaching.phenomena(self.data, kind) for cid in _strings(p.get("caseIds"))}
        return [c["id"] for c in self.practice if c["id"] in listed]

    def practice_cases_of(self, phenomenon: dict[str, Any]) -> list[dict[str, Any]]:
        return [self.by_id[cid] for cid in _unique(_strings(phenomenon.get("caseIds"))) if cid in self.practice_ids]


# ---------------------------------------------------------------------------
# Structure rules (no files)
# ---------------------------------------------------------------------------


#: Required tool arguments that carry the asker's identity or a record id (live 2026-09-27: the Workshop
#: model has no authenticated identity, so it asks instead of calling when the question omits the id).
IDENTITY_ARG = re.compile(r"(?i)(^|_)(id|ids|no|number|code|serial|account|user|employee|member)$|编号|工号|账号|会员号|序列号")
ID_LIKE = re.compile(r"[A-Za-z]{1,12}[-_]?\d{2,}|\d{3,}")


def _live_learning_rules(ctx: _Context, out: _Report) -> None:
    """Warnings from the 2026-09-27 live control run (docs/replace-generic/evidence/live-hr-default-2026-09-27)."""
    for phenomenon in ctx.buried:
        out.warning(
            "teaching.buried_gap_unreliable",
            f"retrieval_gap '{phenomenon.get('id')}' is buried: agents call the retrieval tool again with rephrased "
            "terms and find the answer (in the 2026-09-27 live control run the buried gap was answered on the second "
            "retrieval); declare an absent gap for the class contrast",
            _phenomenon_path(phenomenon.get("id")), _S_PROBES,
        )
    defects = {d.get("id"): d for d in ctx.block.get("baselineDefects") or [] if isinstance(d, dict)}
    for phenomenon in teaching.phenomena(ctx.data, "prompt_fixable"):
        if not any(isinstance((defects.get(did) or {}).get("baselineMarker"), str) for did in _strings(phenomenon.get("defectIds"))):
            out.warning(
                "teaching.defect_marker_absent",
                f"prompt_fixable '{phenomenon.get('id')}': no listed defect names the baseline sentence that causes it "
                "(baselineMarker); a baseline that is merely silent on grounding may already answer from the sources "
                "(the 2026-09-27 live control baseline scored GR 0.82 and 1.0) — add a baselineMarker sentence that asks "
                "EVERY answer to go beyond the documents (e.g. three or more general practices with concrete figures); a "
                "marker that only says 'fill in when the documents are silent' still answers from the sources",
                _phenomenon_path(phenomenon.get("id")), _S_PROMPTS,
            )
    for case in ctx.practice:
        query = str(case.get("query") or "")
        for name in _strings(_expected(case).get("requiredTools")):
            tool = ctx.tools.get(name)
            if tool is None or tool.get("kind") != "mock":
                continue
            schema = tool.get("inputSchema") or {}
            for arg in _strings(schema.get("required")):
                values = _unique(str(fc.get("when", {}).get(arg)) for fc in (tool.get("fixtures") or {}).get("cases") or []
                                 if isinstance(fc, dict) and isinstance(fc.get("when"), dict) and fc["when"].get(arg) not in (None, ""))
                if values:
                    missing = not any(contains_term(query, v) for v in values)
                else:
                    missing = bool(IDENTITY_ARG.search(arg)) and not ID_LIKE.search(query)
                if missing:
                    out.warning(
                        "teaching.tool_arg_not_in_query",
                        f"practice case '{case['id']}' requires '{name}', whose required argument '{arg}' is never stated in "
                        "the question; the Workshop model has no authenticated identity and asks instead of calling "
                        "(seen in the 2026-09-27 live control run)",
                        _case_path(case["id"]), ("golden", "tools"),
                    )
    _role_in_query_rule(ctx, out)
    _gated_tool_argument_rule(ctx, out)


#: An input property that is the caller's own role: role, roleId, role_name, callerRole(Id), caller_role, userRole,
#: 角色, 调用者角色, 岗位... Not a role the tool acts on (targetRole, requestedRole) nor a name that merely contains
#: "role" (roleDescription, controlEnabled, enroleeId): review 2026-09-29.
ROLE_ARG = re.compile(r"(?i)^(?:caller|requester|asker|actor|user|current|my|own)?[_-]?role(?:[_-]?(?:id|name|code))?$"
                      r"|^(?:调用者|调用方|请求人|当前|用户|我的)?(?:角色|岗位)(?:编号|名称|代码|id)?$")
#: A fixture ``when`` key that says who calls (besides a :data:`ROLE_ARG` name): caller, callerId, requester_role...
CALLER_KEY = re.compile(r"(?i)^(?:caller|requester|asker|actor|user)(?:[_-]?(?:role|id|name))*$")


def role_argument(tool: dict[str, Any]) -> str | None:
    """The tool's input property that carries the caller's role (``callerRoleId``, ``roleId``, 角色...), if any."""
    props = ((tool.get("inputSchema") or {}).get("properties") or {}) if isinstance(tool.get("inputSchema"), dict) else {}
    return next((str(name) for name in props if ROLE_ARG.search(str(name))), None)


def _role_gated(tool: dict[str, Any], role_terms: set[str]) -> bool:
    """A tool whose fixtures branch on the caller's role (a ``cases``/``errors`` ``when`` key naming the caller, whose
    value is a declared role id or name, e.g. a finance-only allow case with a default deny) or whose input takes the
    caller's role (:func:`role_argument`). (Not its description: loyalty-points and ops-support name roles there for
    refusals about other people's data; nor a role value under another key, such as ``stage: settlement``.)"""
    fixtures = tool.get("fixtures") if isinstance(tool.get("fixtures"), dict) else {}
    for key in ("cases", "errors"):
        for row in fixtures.get(key) or []:
            when = row.get("when") if isinstance(row, dict) else None
            if isinstance(when, dict) and any(norm(str(v)) in role_terms for k, v in when.items()
                                              if ROLE_ARG.search(str(k)) or CALLER_KEY.search(str(k))):
                return True
    return role_argument(tool) is not None


def _name_words(name: str) -> set[str]:
    """Lower-case words of an identifier in snake, kebab or camel case."""
    return {w.lower() for w in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", name)}


def _tool_subject(tool: str) -> set[str]:
    return _name_words(tool) - TOOL_TOKEN_STOPWORDS


def permission_names_tool(permission: str, tool: str) -> bool:
    """A role permission names ``tool``: its words hold every subject word of the tool name (freight-claims lists
    ``get_settlement_account`` itself, lease-contract ``view_credit_rating`` for ``get_credit_rating``)."""
    subject = _tool_subject(tool)
    return bool(subject) and subject <= _name_words(permission)


def roles_calling(data: dict[str, Any], tool: str) -> set[str]:
    """Ids of the roles expected to call ``tool``: a permission of theirs names it (:func:`permission_names_tool`)
    or a golden case of theirs (practice or holdout) requires it."""
    ids = {str(r.get("id")) for r in (data.get("agent") or {}).get("roles") or []
           if isinstance(r, dict) and any(permission_names_tool(p, tool) for p in _strings(r.get("permissions")))}
    cases = (data.get("evaluation") or {}).get("goldenSet") or []
    return ids | {str(c["roleId"]) for c in cases
                  if isinstance(c, dict) and c.get("roleId") and tool in _strings(_expected(c).get("requiredTools"))}


def _refused_for_role(data: dict[str, Any], tool: str, rid: Any, role_terms: set[str]) -> bool:
    """``tool`` is refused to role ``rid`` because of the role: the tool takes or branches on the caller's role
    (:func:`_role_gated`), or another role is expected to call it (:func:`roles_calling`) and nothing says ``rid``
    may, not even a permission sharing a word with the tool. The second signal is what is left once the role
    argument is dropped, as the contract asks (freight-claims: only the settlement role's permissions name
    get_settlement_account). A refusal about other people's data is not one: the asker's own role calls the tool for
    its own records (hr-default query_salary_info, ops-support check_lab_status) or no role is expected to call it
    (loyalty-points check_points_ledger).

    Limitation: once the argument is gone, a pack whose permissions name the tool in other words (prose, another
    language, a synonym) and whose allowed role has no golden case requiring it is not recognised."""
    if _role_gated(next((t for t in data.get("tools") or [] if isinstance(t, dict) and t.get("name") == tool), {}), role_terms):
        return True
    callers = roles_calling(data, tool)
    if not callers or str(rid) in callers:
        return False
    own = next((r for r in (data.get("agent") or {}).get("roles") or [] if isinstance(r, dict) and r.get("id") == rid), {})
    return not any(_name_words(p) & _tool_subject(tool) for p in _strings(own.get("permissions")))


def role_not_in_query(data: dict[str, Any], case: dict[str, Any]) -> tuple[str, str, str] | None:
    """``(role id, role name, gated tool)`` when ``case`` is refused because of the asker's role (a forbidden
    tool is refused to that role, see :func:`_refused_for_role`) and its question names neither the case's role nor
    its id."""
    roles = {r.get("id"): r for r in (data.get("agent") or {}).get("roles") or [] if isinstance(r, dict)}
    rid = case.get("roleId")
    if rid not in roles:
        return None
    role_terms = {norm(str(term)) for r in roles.values() for term in (r.get("id"), r.get("name")) if term}
    gated = [name for name in _strings(_expected(case).get("forbiddenTools")) if _refused_for_role(data, name, rid, role_terms)]
    name = str(roles[rid].get("name") or rid)
    if gated and not any(contains_term(str(case.get("query") or ""), term) for term in _strings([name, rid])):
        return str(rid), name, gated[0]
    return None


def _gated_tool_argument_rule(ctx: _Context, out: _Report) -> None:
    """A refused, role-gated mock tool must not take the caller's role as an argument (live 2026-09-29,
    freight-claims: with get_settlement_account(roleId) the optimized agent passed its own role "to check" and
    refused only after the access_denied, in both runs, though the question and the rule named the role; the HR
    refusal tool has no role argument and held in every live run). Access control belongs to the identity the
    gateway passes, not to a parameter the agent fills in."""
    seen: set[str] = set()
    for case, _rid, _name, tool in role_gated_refusals(ctx.data):
        arg = role_argument(ctx.tools.get(tool) or {})
        if arg and tool not in seen:
            seen.add(tool)
            out.warning(
                "teaching.gated_tool_role_argument",
                f"mock tool '{tool}' (refused in '{case['id']}') takes the caller's role as argument '{arg}': the agent "
                "fills it in and calls the tool to check before refusing (seen in both 2026-09-29 freight-claims runs) — "
                "drop the argument and the fixture branches on it; keep the access rule in the prompts",
                f"tools.{tool}", _S_TOOLS,
            )


def _role_in_query_rule(ctx: _Context, out: _Report) -> None:
    """A refusal that depends on the asker's role needs the role in the question (live 2026-09-29, lease-contract:
    the agent got only an actor id, called the role-gated tool to find out, and L1 failed forbiddenTools)."""
    for case in ctx.practice:
        found = role_not_in_query(ctx.data, case)
        if found:
            rid, name, tool = found
            out.warning(
                "teaching.role_not_in_query",
                f"practice case '{case['id']}' is refused because the asker is '{rid}', but the question never states the "
                f"role; the Workshop agent gets only an actor id and calls '{tool}' to find out (seen in the 2026-09-29 "
                f"live run) — state the role in the question (for example \"I'm in {name}, ...\")",
                _case_path(case["id"]), ("golden",),
            )


def _structure_rules(ctx: _Context, out: _Report) -> None:
    _phenomenon_case_rules(ctx, out)
    _live_learning_rules(ctx, out)
    _probe_set_rules(ctx, out)
    for cid in ctx.probes:
        _probe_rules(ctx, ctx.by_id[cid], out)
    _first_conversation_rules(ctx, out)
    _defect_and_stability_rules(ctx, out)
    _kind_rules(ctx, out)
    _gap_structure_rules(ctx, out)
    _practice_set_rules(ctx, out)
    _student_prose_rules(ctx, out)


def _phenomenon_case_rules(ctx: _Context, out: _Report) -> None:
    # The scenario cross-references already refuse holdout (and unknown) ids at load; this keeps the
    # policy self-contained for callers that validate raw data.
    for phenomenon in ctx.phenomena:
        for cid in _unique(_strings(phenomenon.get("caseIds"))):
            case = ctx.by_id.get(cid)
            if case is not None and case.get("set") != "practice":
                out.error(
                    "teaching.case_not_practice",
                    f"phenomenon '{phenomenon.get('id')}' uses holdout case '{cid}'; teaching phenomena are demonstrated "
                    "in class by 09/10 and must use practice cases",
                    _phenomenon_path(phenomenon.get("id")), _S_PROBES,
                )
    for cid in ctx.fixable:
        if cid in ctx.gaps:
            out.error(
                "teaching.case_double_role",
                f"case '{cid}' is declared both prompt_fixable and retrieval_gap; a probe has exactly one expected outcome",
                _case_path(cid), _S_PROBES,
            )


def _probe_set_rules(ctx: _Context, out: _Report) -> None:
    n = len(ctx.probes)
    if n < MIN_PROBES:
        out.error(
            "teaching.too_few_probes",
            f"{n} retrieval probes declared (prompt_fixable + retrieval_gap cases); at least {MIN_PROBES} are required, "
            "like the Guide's three golden questions",
            "labs.teaching.phenomena", _S_PROBES,
        )
    elif n > MAX_PROBES:
        out.warning(
            "teaching.many_probes",
            f"{n} retrieval probes declared; more than {MAX_PROBES} makes 09/10/12 much slower than the Guide's timing",
            "labs.teaching.phenomena", _S_PROBES,
        )
    if len(ctx.fixable) < MIN_FIXABLE:
        out.error(
            "teaching.too_few_fixable",
            f"{len(ctx.fixable)} prompt_fixable probe case(s); at least {MIN_FIXABLE} are required so 'the prompt fix works "
            "where retrieval is good' does not hinge on one judge score",
            "labs.teaching.phenomena", _S_PROBES,
        )
    if len(ctx.gaps) < MIN_GAPS:
        out.error(
            "teaching.no_gap",
            "no retrieval_gap probe; at least 1 is required so THELMA can show a failure that the optimized prompt cannot fix",
            "labs.teaching.phenomena", _S_PROBES,
        )
    gap_phenomena = teaching.phenomena(ctx.data, "retrieval_gap")
    if gap_phenomena and all(p.get("mechanism") == "buried" for p in gap_phenomena):
        out.warning(
            "teaching.gap_only_buried",
            "every retrieval_gap uses mechanism 'buried', which depends on retrieval ranking; add an 'absent' gap so "
            "rehearsal reliably reproduces the contrast",
            "labs.teaching.phenomena", _S_PROBES,
        )


def _probe_rules(ctx: _Context, case: dict[str, Any], out: _Report) -> None:
    cid = case["id"]
    expected = _expected(case)
    path = _case_path(cid)
    category = case.get("category")
    if category not in PROBE_CATEGORIES:
        out.error(
            "teaching.probe_category",
            f"probe '{cid}' has category '{category}'; retrieval probes must be normal or boundary questions "
            "(a refusal cannot show a grounding change)",
            path, _S_GOLDEN,
        )
    tools = sorted(set(_strings(expected.get("requiredTools"))))
    if tools != [ctx.rt]:
        out.error(
            "teaching.probe_tools",
            f"probe '{cid}' requiredTools must be exactly ['{ctx.rt}'] (found {tools}); tool output in the answer is not "
            "among THELMA's sources and lowers GR whatever the prompt",
            path, _S_GOLDEN,
        )
    if expected.get("shouldRefuse") is True:
        out.error(
            "teaching.probe_refuse",
            f"probe '{cid}' sets shouldRefuse; retrieval probes must be answerable questions",
            path, _S_GOLDEN,
        )
    if (cid in ctx.fixable or cid in ctx.buried_ids) and not _strings(expected.get("mustMention")):
        out.error(
            "teaching.probe_needs_mustmention",
            f"probe '{cid}' needs expected.mustMention (short literal answer terms) so the answer's presence in the "
            "knowledge base can be checked",
            path, _S_GOLDEN,
        )
    query = str(case.get("query") or "")
    subject = content_words(query)
    for holdout in ctx.holdout:
        shared = sorted(subject & content_words(str(holdout.get("query") or "")))
        if len(shared) >= NEAR_HOLDOUT_SHARED_WORDS:
            out.warning(
                "teaching.probe_near_holdout",
                f"probe '{cid}' shares {shared} with holdout case '{holdout['id']}'; a class probe that near-duplicates "
                "a holdout question spoils the holdout as the unseen generalization check",
                path, _S_GOLDEN,
            )
    words = query_tokens(query)
    for tool in ctx.mocks:
        shared = sorted(words & tool_tokens(tool["name"]))
        if len(shared) >= 2:
            out.warning(
                "teaching.probe_tool_overlap",
                f"probe '{cid}' shares {shared} with mock tool '{tool['name']}'; the baseline may call the tool instead of "
                "retrieving, leaving fewer retrieval traces than 09 waits for",
                path, _S_GOLDEN,
            )
    for tool in ctx.mocks:
        for value in fixture_values(tool):
            if contains_term(query, value):
                out.warning(
                    "teaching.probe_fixture_value",
                    f"probe '{cid}' mentions '{value}', a fixture value of mock tool '{tool['name']}'; keep probes purely "
                    "informational",
                    path, _S_GOLDEN,
                )
                break


def _first_conversation_rules(ctx: _Context, out: _Report) -> None:
    first = teaching.first_conversation(ctx.data) or {}
    raw = str(first.get("query") or "")
    query = norm(raw)
    if not query:
        return
    for case in ctx.holdout:
        if norm(str(case.get("query") or "")) == query:
            out.error(
                "teaching.first_is_holdout",
                f"firstConversation.query equals holdout case '{case['id']}'; the first conversation is student-facing",
                "labs.teaching.firstConversation", _S_LABS,
            )
    # The Memory lesson: the first answer is generic because the agent does not know the asker yet. Rehearsal
    # reads it with mustNotMention, so a value the agent gets another way reads as "already knows you".
    path = "labs.teaching.firstConversation"
    for value in _unique(v for tool in ctx.mocks for v in fixture_values(tool) if _RECORD_ID.search(v)):
        if contains_term(raw, value):
            out.error(
                "teaching.first_query_names_record",
                f"firstConversation.query names '{value}', a record a mock tool looks up: the agent answers for that "
                "record without knowing the asker (lease-contract, live 2026-09-29: the first reply named the customer); "
                "ask about the asker's own context and leave the id out",
                path + ".query", _S_LABS,
            )
    for term in _unique(_strings(first.get("mustNotMention"))):
        if contains_term(raw, term):
            out.error(
                "teaching.first_term_in_query",
                f"firstConversation.mustNotMention '{term}' occurs in its own query, so every answer repeats it and "
                "rehearsal reads the first conversation as already knowing the asker (freight-claims, live 2026-09-29); "
                "name a value only the asker's record holds",
                path + ".mustNotMention", _S_LABS,
            )
            continue
        spec = next((t["name"] for t in ctx.tools.values()
                     if contains_term(json.dumps({"description": t.get("description"), "inputSchema": t.get("inputSchema")},
                                                 ensure_ascii=False), term)), None)
        if spec is not None:
            out.warning(
                "teaching.first_term_in_tool_spec",
                f"firstConversation.mustNotMention '{term}' is in tool '{spec}''s description or input schema: the agent "
                "is given it, quotes it as a format example or calls the tool with it (freight-claims, live 2026-09-29: "
                "\"运单号通常格式如 yt-7730021\"), and rehearsal reads that as already knowing the asker; describe the "
                "format without a fixture value",
                f"tools.{spec}", ("tools", "labs"),
            )
        elif len(norm(term)) <= 3:
            out.warning(
                "teaching.first_term_generic",
                f"firstConversation.mustNotMention '{term}' is {len(norm(term))} character(s); a generic answer can "
                "contain it too (a credit count, a year), which reads as already knowing the asker — use the asker's "
                "record id or another value only their record holds",
                path + ".mustNotMention", _S_LABS,
            )


def _defect_and_stability_rules(ctx: _Context, out: _Report) -> None:
    exercised = {
        did for p in teaching.phenomena(ctx.data, teaching.PROBE_KINDS) for did in _strings(p.get("defectIds"))
    }
    for defect in ctx.block.get("baselineDefects") or []:
        did = defect.get("id") if isinstance(defect, dict) else None
        if isinstance(did, str) and did not in exercised:
            out.error(
                "teaching.defect_unexercised",
                f"baseline defect '{did}' is not exercised by any prompt_fixable or retrieval_gap phenomenon",
                f"labs.teaching.baselineDefects.{did}", _S_LABS,
            )
    stability = ctx.block.get("stabilityCaseId")
    if stability is not None and stability not in ctx.fixable:
        out.error(
            "teaching.stability_not_fixable",
            f"stabilityCaseId '{stability}' must be a prompt_fixable probe; 13-judge-stability re-scores the last probe asked",
            "labs.teaching.stabilityCaseId", _S_LABS,
        )


def _kind_rules(ctx: _Context, out: _Report) -> None:
    declared = {p.get("kind") for p in ctx.phenomena}
    for kind, why in REQUIRED_KINDS:
        if kind not in declared:
            out.error(
                "teaching.missing_kind",
                f"no {kind} phenomenon; the Workshop needs it for {why}",
                "labs.teaching.phenomena", _S_PROBES,
            )
    mock_names = {t["name"] for t in ctx.mocks}
    for phenomenon in teaching.phenomena(ctx.data, "tool_use"):
        for case in ctx.practice_cases_of(phenomenon):
            required = _strings(_expected(case).get("requiredTools"))
            cid = case["id"]
            if not mock_names.intersection(required):
                out.error(
                    "teaching.tool_use_case",
                    f"tool_use case '{cid}' must require at least one mock tool",
                    _case_path(cid), _S_PROBES,
                )
                continue
            query = str(case.get("query") or "")
            for name in required:
                tool = ctx.tools.get(name)
                if tool is None or tool.get("kind") != "mock" or not _default_is_error(tool):
                    continue
                if not any(contains_term(query, v) for v in fixture_values(tool)):
                    out.warning(
                        "teaching.tool_fixture_unreachable",
                        f"tool_use case '{cid}': mock tool '{name}' returns an error by default and no fixture 'when' value "
                        "appears in the query",
                        _case_path(cid), ("golden", "tools"),
                    )
    for phenomenon in teaching.phenomena(ctx.data, "refusal"):
        for case in ctx.practice_cases_of(phenomenon):
            cid, expected = case["id"], _expected(case)
            if case.get("category") != "prohibited" or expected.get("shouldRefuse") is not True:
                out.error(
                    "teaching.refusal_case",
                    f"refusal case '{cid}' must be category 'prohibited' with expected.shouldRefuse: true",
                    _case_path(cid), _S_PROBES,
                )
            if not _strings(expected.get("forbiddenTools")) and not _strings(expected.get("mustNotMention")):
                out.warning(
                    "teaching.refusal_unmeasurable",
                    f"refusal case '{cid}' has neither forbiddenTools nor mustNotMention; only the refusal wording can be checked",
                    _case_path(cid), _S_GOLDEN,
                )
    for phenomenon in teaching.phenomena(ctx.data, "escalation"):
        for case in ctx.practice_cases_of(phenomenon):
            if _expected(case).get("shouldEscalate") is not True:
                out.error(
                    "teaching.escalation_case",
                    f"escalation case '{case['id']}' must set expected.shouldEscalate: true",
                    _case_path(case["id"]), _S_PROBES,
                )
    noise_grounding = teaching.phenomena(ctx.data, "noise_grounding")
    for phenomenon in noise_grounding:
        for did in _unique(_strings(phenomenon.get("documentIds"))):
            doc = ctx.docs_by_id.get(did)
            if doc is not None and doc.get("noise") is not True:
                out.error(
                    "teaching.noise_doc_flag",
                    f"noise_grounding phenomenon '{phenomenon.get('id')}' lists document '{did}' without noise: true",
                    _phenomenon_path(phenomenon.get("id")), _S_KB_LABS,
                )
    if noise_grounding:
        plan = (ctx.data.get("knowledge") or {}).get("noisePlan")
        if not isinstance(plan, dict) or plan.get("enabled") is not True:
            out.error(
                "teaching.noise_disabled",
                "knowledge.noisePlan.enabled must be true when noise_grounding is declared",
                "knowledge.noisePlan", _S_KB,
            )


def _gap_structure_rules(ctx: _Context, out: _Report) -> None:
    for phenomenon in teaching.phenomena(ctx.data, "retrieval_gap"):
        pid = phenomenon.get("id")
        bait = _strings(phenomenon.get("baitTerms"))
        for term in _unique(_strings(phenomenon.get("absentTerms")) + bait):
            for holdout in ctx.holdout:
                if contains_term(str(holdout.get("query") or ""), term):
                    out.warning(
                        "teaching.gap_near_holdout",
                        f"retrieval_gap '{pid}': '{term}' also occurs in the query of holdout case '{holdout['id']}'; the "
                        "class gap must not rehearse a holdout question",
                        _phenomenon_path(pid), _S_PROBES,
                    )
        for case in ctx.practice_cases_of(phenomenon):
            query = str(case.get("query") or "")
            for term in bait:
                if not contains_term(query, term):
                    out.error(
                        "teaching.bait_not_in_query",
                        f"retrieval_gap '{pid}': baitTerm '{term}' does not occur in the query of '{case['id']}'; bait must "
                        "reuse the question's own wording",
                        _phenomenon_path(pid), _S_PROBES,
                    )
            if phenomenon.get("mechanism") == "absent" and _expected(case).get("shouldEscalate") is not True:
                out.error(
                    "teaching.absent_needs_escalation",
                    f"retrieval_gap '{pid}' (absent): case '{case['id']}' must set expected.shouldEscalate: true (correct "
                    "behavior is to say the KB lacks it and hand off)",
                    _case_path(case["id"]), _S_GOLDEN,
                )


#: Words that name the holdout set in student-facing prose (English and Chinese).
HOLDOUT_WORDS = re.compile(r"(?i)\bhold[- ]?outs?\b|\bheld[- ]out\b|留出集|保留集|留出")


def _student_prose(ctx: _Context) -> list[tuple[str, str]]:
    """(path, text) of the teaching prose that ships to students (pack.json observations and teaching)."""
    labs = ctx.data.get("labs") or {}
    items = [(f"labs.observations[{i}]", s) for i, s in enumerate(_strings(labs.get("observations")))]
    first = teaching.first_conversation(ctx.data) or {}
    for key in ("label", "teachingPoint"):
        if isinstance(first.get(key), str):
            items.append((f"labs.teaching.firstConversation.{key}", first[key]))
    for phenomenon in ctx.phenomena:
        if isinstance(phenomenon.get("teachingPoint"), str):
            items.append((f"{_phenomenon_path(str(phenomenon.get('id')))}.teachingPoint", phenomenon["teachingPoint"]))
    return items


def _student_prose_rules(ctx: _Context, out: _Report) -> None:
    """The holdout stays an unseen check: student-facing prose never names the set or its cases."""
    for path, text in _student_prose(ctx):
        word = HOLDOUT_WORDS.search(text)
        named = next((c for c in ctx.holdout for term in (c.get("id"), c.get("label"))
                      if isinstance(term, str) and len(norm(term)) >= 4 and contains_term(text, term)), None)
        if word or named:
            what = f"the holdout case '{named['id']}'" if named else f"the holdout ('{word.group(0)}')"
            out.warning(
                "teaching.prose_names_holdout",
                f"{path} names {what}; it ships to students in pack/pack.json, so the holdout would no longer be an "
                "unseen check. Keep holdout topics in instructor material",
                path, _S_LABS,
            )


def _practice_set_rules(ctx: _Context, out: _Report) -> None:
    probes = set(ctx.probes)
    for case in ctx.practice:
        if case["id"] not in probes and ctx.rt in _strings(_expected(case).get("requiredTools")):
            out.warning(
                "teaching.retrieval_case_not_probe",
                f"practice case '{case['id']}' requires '{ctx.rt}' but is not a declared probe; 09/10 ask it before the probes "
                "and exclude its trace from THELMA (L1 checks only)",
                _case_path(case["id"]), _S_PROBES,
            )
    if len(ctx.practice) > MAX_PRACTICE:
        out.warning(
            "teaching.too_many_practice",
            f"{len(ctx.practice)} practice cases; 09/10 ask every one, so steps 12/13 take much longer than the Guide's "
            "2–3 minutes",
            "evaluation.goldenSet", _S_GOLDEN,
        )
    if not ctx.data.get("skills"):
        out.warning(
            "teaching.no_skill",
            "no skills declared; Guide step 5 (03-configure-skills.sh) uploads nothing",
            "skills", ("skills",),
        )


# ---------------------------------------------------------------------------
# File rules (need the scenario root)
# ---------------------------------------------------------------------------


def _read_inside(root: Path, rel: Any) -> str | None:
    if not isinstance(rel, str):
        return None
    base = root.resolve()
    path = (base / rel).resolve()
    if path != base and base not in path.parents:
        return None  # escapes the root; the scenario cross-references report it
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
    except OSError:
        return None


def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def _file_rules(ctx: _Context, root: Path, out: _Report) -> None:
    _prompt_rules(ctx, root, out)
    texts = teaching.document_texts(ctx.data, root)
    order = [d["id"] for d in ctx.documents]
    files = {d["id"]: d.get("file") for d in ctx.documents}

    def docs_with(term: str) -> list[str]:
        return list(teaching.documents_containing(texts, term, order=order))

    for cid in ctx.fixable:
        for term in _unique(_strings(_expected(ctx.by_id[cid]).get("mustMention"))):
            if not docs_with(term):
                out.error(
                    "teaching.fixable_answer_not_in_kb",
                    f"prompt_fixable probe '{cid}': mustMention '{term}' appears in no knowledge document; retrieval cannot "
                    "be 'good' for an answer the KB does not contain",
                    _case_path(cid), _S_KB_GOLDEN,
                )

    for phenomenon in teaching.phenomena(ctx.data, "retrieval_gap"):
        pid = phenomenon.get("id")
        bait = _unique(_strings(phenomenon.get("baitTerms")))
        cases = ctx.practice_cases_of(phenomenon)
        if phenomenon.get("mechanism") == "buried":
            for case in cases:
                for term in _unique(_strings(_expected(case).get("mustMention"))):
                    if not docs_with(term):
                        out.error(
                            "teaching.buried_answer_not_in_kb",
                            f"retrieval_gap '{pid}' (buried): mustMention '{term}' of case '{case['id']}' appears in no "
                            "knowledge document; use mechanism 'absent' if the answer is intentionally missing",
                            _case_path(case["id"]), ("knowledge", "golden", "labs"),
                        )
        if phenomenon.get("mechanism") == "absent":
            _gap_question_rule(pid, bait, cases, texts, files, out)
            _gap_topic_rule(pid, _strings(phenomenon.get("absentTerms")), cases,
                            {did: text for did, text in texts.items() if did not in ctx.noise_ids}, files, out)
            for term in _unique(_strings(phenomenon.get("absentTerms"))):
                form = next((f for f in (term, concept_form(term)) if f and docs_with(f)), None)
                if form:
                    where = ", ".join(str(files.get(did) or did) for did in docs_with(form))
                    what = f"'{term}'" if form == term else f"'{term}' (as the concept '{form}', without its question words)"
                    out.error(
                        "teaching.absent_term_in_kb",
                        f"retrieval_gap '{pid}' (absent): {what} appears in {where}; the knowledge base must not contain "
                        "the missing fact",
                        _phenomenon_path(pid), _S_KB_LABS,
                    )
        if not bait:
            continue
        noise_texts = {did: texts[did] for did in ctx.noise_ids if did in texts}
        if not any(contains_term(text, term) for text in noise_texts.values() for term in bait):
            out.error(
                "teaching.bait_missing",
                f"retrieval_gap '{pid}': no noise document (noise: true) contains baitTerm(s) {bait}",
                _phenomenon_path(pid), _S_KB_LABS,
            )
            continue
        occurrences = sum(count_term(text, term) for text in noise_texts.values() for term in bait)
        if occurrences < BAIT_MIN_OCCURRENCES:
            out.warning(
                "teaching.bait_weak",
                f"retrieval_gap '{pid}': bait terms occur {occurrences} time(s) in noise documents; fewer than "
                f"{BAIT_MIN_OCCURRENCES} may not take the top-3 retrieval slots",
                _phenomenon_path(pid), _S_KB,
            )
        if phenomenon.get("mechanism") == "buried":
            answers = _unique(t for case in cases for t in _strings(_expected(case).get("mustMention")))
            for did in order:
                hit = _bait_near_answer(texts.get(did), bait, answers)
                if hit:
                    out.warning(
                        "teaching.bait_near_answer",
                        f"retrieval_gap '{pid}': a paragraph in '{files.get(did) or did}' contains both bait '{hit[0]}' and "
                        f"answer '{hit[1]}'; the retriever may return the answer",
                        str(files.get(did) or did), _S_KB,
                    )

    _noise_coretrieval_rule(ctx, texts, out)


_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


#: Share of an absent-gap question's subject words (absent terms left out) one knowledge document must hold before
#: the rehearsal's gap-question hint names it: lease-contract r1, where the question qualified the documented
#: standard template, reached 0.36 and 0.27; its rewritten question and the other live gaps stayed at 0.08-0.21 on
#: the documents that are not noise-heavy FAQs. No English pack with documents that are not noise has run live yet:
#: English questions count only :func:`subject_words` (review 2026-09-29).
TOPIC_SHARE = 0.25
#: The fewest distinct subject words (CJK character pairs) a document must share with the question to count: once the
#: absent terms are left out an English question keeps two to eight words, so one shared word is already 0.12-0.5.
TOPIC_MIN_WORDS = 2


def has_cjk(text: str) -> bool:
    return bool(_CJK.search(text))


def question_signature(text: str, cjk: bool) -> set[str]:
    """CJK character bigrams (Chinese) or subject words (other languages) of ``text``."""
    if cjk:
        chars = "".join(_CJK.findall(norm(text)))
        return {chars[i:i + 2] for i in range(len(chars) - 1)}
    return content_words(text)


def _topic_signature(text: str, cjk: bool) -> set[str]:
    return question_signature(text, True) if cjk else subject_words(text)


def topic_share(query: str, absent_terms: Iterable[str], texts: dict[str, str]) -> tuple[str, float] | None:
    """``(document id, share)`` of the document holding the largest share of ``query``'s subject words (CJK character
    pairs, or :func:`subject_words`), the absent terms left out; ``None`` when the question has no other subject
    words or no document shares :data:`TOPIC_MIN_WORDS` of them."""
    cjk = has_cjk(query)
    words = _topic_signature(query, cjk)
    for term in absent_terms:
        words -= _topic_signature(term, cjk)
    if not words or not texts:
        return None
    shared, did = max((len(words & _topic_signature(text, cjk)), did) for did, text in texts.items())
    return (did, shared / len(words)) if shared >= TOPIC_MIN_WORDS else None


def _gap_topic_rule(pid: Any, absent_terms: list[str], cases: list[dict[str, Any]], texts: dict[str, str],
                    files: dict[str, Any], out: _Report) -> None:
    """An absent gap's question must be about a topic no document covers, not a qualifier on a documented one.

    Live lease-contract rehearsal (2026-09-29): no document said 跨境 (cross-border), but "跨境诊所可以用同一份标准模板吗？"
    asks about the documented standard template; THELMA scored retrieval as covering it (SP2 0.6 / SQC 1.0), so the
    gap did not reproduce. ``texts`` are the documents that are not noise: a noise FAQ carrying the bait is expected
    to share the question's words.
    """
    for case in cases:
        found = topic_share(str(case.get("query") or ""), absent_terms, texts)
        if found and found[1] >= TOPIC_SHARE:
            did, share = found
            out.warning(
                "teaching.gap_topic_documented",
                f"retrieval_gap '{pid}' (absent): {share:.0%} of the other words of '{case['id']}' ('{case.get('query')}') "
                f"are in {files.get(did) or did}, a document that is not noise; if the question qualifies that documented "
                "topic, retrieval will cover it (seen in the 2026-09-29 lease-contract rehearsal) — ask about a topic no "
                "document covers",
                _case_path(case["id"]), ("golden", "labs"),
            )


def _gap_question_rule(pid: Any, bait: list[str], cases: list[dict[str, Any]], texts: dict[str, str],
                       files: dict[str, Any], out: _Report) -> None:
    """An absent gap's question must not be restated in the knowledge base.

    Live ops-support rehearsal (2026-09-28): a noise FAQ entry repeated the gap question ("新诊所正式接入需要多长时间？")
    and answered "not recorded here; ask the business team"; THELMA scored that chunk as covering the query
    (SQC 0.5), so the gap did not reproduce although no absentTerm was in the KB.
    """
    handoff = _unique(t for case in cases for t in _handoff_terms(case))
    for did, text in texts.items():
        where = str(files.get(did) or did)
        for paragraph in _paragraphs(text):
            # One entry (a line or bullet) carrying two bait phrases recreates the question; separate
            # bullets that each reuse one phrase in an unrelated topic are exactly what bait should be.
            baits = max(([term for term in bait if contains_term(line, term)] for line in paragraph.splitlines() or [paragraph]),
                        key=len, default=[])
            # An entry (a Q&A paragraph, or one bullet) that carries a bait phrase and the gap's hand-off target
            # answers the question: "exchange credits … go through a registrar ticket" (campus draft, 2026-10-01).
            handed = next(((term, target) for entry in _entries(paragraph) for term in bait if contains_term(entry, term)
                           for target in handoff if _names_target(entry, target)), None)
            if handed is not None:
                snippet = paragraph.strip().replace("\n", " ")[:80]
                out.error(
                    "teaching.gap_question_in_kb",
                    f"retrieval_gap '{pid}' (absent): {where} hands the gap question off ('{snippet}': bait '{handed[0]}' "
                    f"next to the hand-off '{handed[1]}'); retrieval then 'covers' it and THELMA does not see a gap — a "
                    "bait entry reuses one bait phrase in an unrelated topic and never names the hand-off target",
                    _phenomenon_path(pid), _S_KB_LABS,
                )
                break
            restated = None
            for case in cases:
                query = str(case.get("query") or "")
                cjk = bool(_CJK.search(query))
                wanted = question_signature(query, cjk)
                if len(wanted) >= (6 if cjk else 3) and len(wanted & question_signature(paragraph, cjk)) >= 0.6 * len(wanted):
                    restated = case
                    break
            if (len(bait) >= 2 and len(baits) >= 2) or restated is not None:
                snippet = paragraph.strip().replace("\n", " ")[:80]
                out.error(
                    "teaching.gap_question_in_kb",
                    f"retrieval_gap '{pid}' (absent): {where} restates the gap question ('{snippet}'); retrieval then "
                    "'covers' it and THELMA does not see a gap — bait entries must be unrelated topics that each reuse "
                    "one bait phrase, never the question or a hand-off answer to it",
                    _phenomenon_path(pid), _S_KB_LABS,
                )
                break


def _handoff_terms(case: dict[str, Any]) -> list[str]:
    """The hand-off target(s) an absent gap's answer names: expected.mustMention and mustMentionAnyOf."""
    expected = _expected(case)
    groups = [g if isinstance(g, list) else [g] for g in expected.get("mustMentionAnyOf") or []]
    return _strings(expected.get("mustMention")) + [t for g in groups for t in _strings(g)]


#: A clause that names a target only to say it is unrelated ("与任何系统、工单或业务无关", ops-support, whose gap
#: reproduced live) does not hand anything off.
_NEGATED = re.compile(r"无关|不涉及|没有关系|(?i:unrelated|nothing to do with)")


def _names_target(entry: str, target: str) -> bool:
    return any(contains_term(clause, target) and not _NEGATED.search(clause)
               for clause in re.split(r"[，,。；;！!？?\n]", entry))


_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)、])\s+")


def _entries(paragraph: str) -> list[str]:
    """The entries of a paragraph: its bullets when it is a list, else the whole paragraph (a Q&A pair)."""
    lines = paragraph.splitlines()
    if sum(1 for line in lines if _BULLET.match(line)) < 2:
        return [paragraph]
    entries: list[str] = []
    for line in lines:
        if _BULLET.match(line) or not entries:
            entries.append(line)
        else:
            entries[-1] += "\n" + line
    return entries


#: Chinese question words: an absentTerm written as a question fragment ("学分怎么认定") names its concept
#: ("学分认定") only without them, and a document states the concept, not the question.
_QUESTION_WORDS = re.compile(r"怎么样|怎么|如何|怎样|是否|能否|能不能|可不可以|会不会|有没有|为什么|多长时间|多久|吗|呢|[？?]")


def concept_form(term: str) -> str | None:
    """A CJK absentTerm without its question words, when that differs and keeps 4+ CJK characters."""
    if not _CJK.search(term):
        return None
    form = _QUESTION_WORDS.sub("", term).strip()
    return form if form != term.strip() and len(_CJK.findall(form)) >= 4 else None


def _bait_near_answer(text: str | None, bait: list[str], answers: list[str]) -> tuple[str, str] | None:
    if not text or not answers:
        return None
    for paragraph in _paragraphs(text):
        for term in bait:
            if contains_term(paragraph, term):
                for answer in answers:
                    if contains_term(paragraph, answer):
                        return term, answer
    return None


def _noise_coretrieval_rule(ctx: _Context, texts: dict[str, str], out: _Report) -> None:
    noise_docs = _unique(
        did for p in teaching.phenomena(ctx.data, "noise_grounding") for did in _strings(p.get("documentIds")) if did in texts
    )
    fixable_terms = [
        _unique(_strings(_expected(ctx.by_id[cid]).get("mustMention"))) for cid in ctx.fixable
    ]
    fixable_terms = [terms for terms in fixable_terms if terms]
    if not noise_docs or not fixable_terms:
        return
    if not any(all(contains_term(texts[did], t) for t in terms) for did in noise_docs for terms in fixable_terms):
        out.warning(
            "teaching.noise_not_coretrieved",
            "no prompt_fixable probe's answer shares a document with declared noise; the baseline GR drop from co-retrieved "
            "noise may not appear",
            "labs.teaching.phenomena", _S_KB_LABS,
        )


def role_gated_refusals(data: dict[str, Any]) -> list[tuple[dict[str, Any], str, str, str]]:
    """``(case, role id, role name, gated tool)`` for every practice refusal whose forbidden tool is refused to the
    asker's role (:func:`_refused_for_role`)."""
    roles = {r.get("id"): r for r in (data.get("agent") or {}).get("roles") or [] if isinstance(r, dict)}
    role_terms = {norm(str(term)) for r in roles.values() for term in (r.get("id"), r.get("name")) if term}
    out = []
    for case in teaching.practice_cases(data):
        rid = case.get("roleId")
        if rid not in roles or _expected(case).get("shouldRefuse") is not True:
            continue
        for name in _strings(_expected(case).get("forbiddenTools")):
            if _refused_for_role(data, name, rid, role_terms):
                out.append((case, str(rid), str(roles[rid].get("name") or rid), name))
                break
    return out


def _refusal_role_rule(ctx: _Context, candidate: str, candidate_file: str, out: _Report) -> None:
    """The candidate's refusal rule names the role it refuses (live 2026-09-29, freight-claims: a rule for "other
    roles" did not stop the optimized agent calling the gated tool for a cs-agent who said who they were; the
    lease-contract rule that named 销售运营 held)."""
    for case, rid, name, tool in role_gated_refusals(ctx.data):
        lines = [line for line in candidate.splitlines() if contains_term(line, tool)]
        if not any(contains_term(line, name) or contains_term(line, rid) for line in lines):
            out.warning(
                "teaching.refusal_rule_names_no_role",
                f"refusal case '{case['id']}': no line of the optimization candidate names both '{tool}' and the asker's "
                f"role '{name}'; a rule for 'other roles' did not stop the agent calling the gated tool live (2026-09-29) — "
                f"write it for the role (for example \"{name}问到这类信息时，直接说明无权限，不调用 {tool}\")",
                candidate_file, ("prompts",),
            )


def _prompt_rules(ctx: _Context, root: Path, out: _Report) -> None:
    prompts = ctx.data.get("prompts") or {}
    baseline_file = prompts.get("baselineFile")
    candidate_file = prompts.get("optimizationCandidateFile")
    baseline = _read_inside(root, baseline_file)
    candidate = _read_inside(root, candidate_file)
    target = str((ctx.data.get("namespace") or {}).get("toolTargetName") or "")
    hints = [h.casefold() for h in _unique([str(ctx.rt or ""), target, target.replace("-", "")]) if h]
    if baseline is not None and not any(h in baseline.casefold() for h in hints):
        out.error(
            "teaching.baseline_no_retrieval_hint",
            f"baseline prompt never names '{ctx.rt}' or '{target}'; a baseline that does not retrieve produces no THELMA "
            f"traces (09 waits for {len(ctx.probes)})",
            str(baseline_file), ("prompts",),
        )
    if candidate is not None and not any(h in candidate.casefold() for h in hints):
        out.warning(
            "teaching.candidate_no_retrieval_hint",
            f"optimization candidate never names '{ctx.rt}' or '{target}'; the optimized agent may stop retrieving and 10 "
            f"would wait for {len(ctx.probes)} retrieval traces in vain",
            str(candidate_file), ("prompts",),
        )
    if candidate is not None:
        _refusal_role_rule(ctx, candidate, str(candidate_file), out)
    for defect in ctx.block.get("baselineDefects") or []:
        if not isinstance(defect, dict):
            continue
        did = defect.get("id")
        path = f"labs.teaching.baselineDefects.{did}"
        fix = defect.get("candidateFix")
        if isinstance(fix, str) and candidate is not None and not contains_term(candidate, fix):
            out.error(
                "teaching.defect_fix_missing",
                f"baseline defect '{did}': candidateFix text not found in {candidate_file}",
                path, _S_PROMPTS,
            )
        if isinstance(fix, str) and baseline is not None and contains_term(baseline, fix):
            out.error(
                "teaching.defect_already_fixed",
                f"baseline defect '{did}': candidateFix text already appears in {baseline_file}, so the baseline does not "
                "have this defect",
                path, _S_PROMPTS,
            )
        marker = defect.get("baselineMarker")
        if isinstance(marker, str) and baseline is not None and not contains_term(baseline, marker):
            out.error(
                "teaching.defect_marker_missing",
                f"baseline defect '{did}': baselineMarker not found in the baseline prompt",
                path, _S_PROMPTS,
            )
        if isinstance(marker, str) and candidate is not None and contains_term(candidate, marker):
            out.error(
                "teaching.defect_marker_kept",
                f"baseline defect '{did}': baselineMarker still appears in the optimization candidate",
                path, _S_PROMPTS,
            )
