"""The customized Workshop Guide (SPEC D10, designs[1] as adapted by the unified contract).

``compile_pack`` writes two deterministic Markdown files in the pack language (``zh-CN`` or ``en``):

``pack/labs/student-guide.md``      the participants' guide; ``render_release`` copies it byte for
                                    byte to the release root as ``README.md``.
``instructor/instructor-guide.md``  the facilitator's guide (answer key, truth sheet, contrasts,
                                    readiness, rehearsal evidence); never part of a release.

Text comes from three sources only: the fixed templates ``templates/guide/{student,instructor}.
{en,zh-CN}.md`` plus :mod:`guide_strings`; pack data, read through :func:`script_facts.compute`
(what the scripts ask, count and name — never re-derived here), :mod:`teaching` (the labs.teaching
declaration and the verdict THRESHOLDS) and :func:`render.optimize_closing_lines` (10's closing text),
so the guide and the scripts agree; and the optional reviewed narrative ``labs.guide``.

The student guide is built from a :class:`StudentView` only: a projection with no ``expected``, no
``basis``, no holdout case, no confirmer or confirmation reference, no material approval, no lab
observations and no instructor narrative, so the student builder cannot emit them by construction.
Its bash command lines equal the 28 lines of the pinned upstream README (:data:`UPSTREAM_GUIDE_COMMANDS`).

There is no model anywhere: the same inputs give the same bytes. :func:`check_guide_narrative` is the
``guide`` policy sub-check (validate time), :func:`check_guides` gates the generated files (compile
time) and :func:`check_instructor_isolation` gates the release.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import guide_strings as gs
from . import script_facts, teaching
from .rehearsal import FOCUS_CHECKS  # the rules live in rehearsal.py (SPEC D2)
from .l1 import DEFAULT_MAX_POLLS, DEFAULT_POLL_SECONDS
from .scenario import customer_anchors, is_customer_confirmed, item_class, item_class_label
from .script_facts import ScriptFacts
from .validator import (
    HR_TOPIC_PATTERNS,
    Finding,
    ValidationReport,
    _normalize,
    guide_source_scopes,
    reader_contains,
    reader_views,
    residue_tokens,
)

TEMPLATE_DIR = Path(__file__).parent / "templates" / "guide"
AUDIENCES: tuple[str, ...] = ("student", "instructor")
STUDENT_GUIDE_PATH = "pack/labs/student-guide.md"
INSTRUCTOR_GUIDE_PATH = "instructor/instructor-guide.md"

#: The pinned upstream READMEs the student templates were written against (template commit 2450922).
UPSTREAM_GUIDE_SHA256: Mapping[str, str] = {
    "README.md": "a1fbacff25d217ffb01cd5b973a106e805e7afb10710b848194908475bafe07d",
    "README.zh-CN.md": "c33f7c4d1878fd5cf275995ed5dc4de1f2dbb729ba4b658f9c3f70b61b420aef",
}
#: The bash command lines of the upstream Guide (both languages), in order: the student guide keeps them.
UPSTREAM_GUIDE_COMMANDS: tuple[str, ...] = (
    "export AWS_DEFAULT_REGION=us-west-2",
    "cd static/scripts",
    "chmod +x *.sh",
    "./00-setup.sh",
    "./00-deploy-infra.sh",
    "./01-create-kb.sh",
    "./02-create-gateway.sh",
    "./03-configure-skills.sh",
    "./04-deploy.sh",
    "./05-setup-memory.sh",
    "./06-test-conversation.sh",
    "./07-setup-eval-env.sh",
    'export PATH="$HOME/.local/bin:$PATH"',
    "./06-test-conversation.sh",
    "./08-create-evaluators.sh",
    "./09-run-eval.sh",
    "./09-run-eval.sh --eval-only [N]",
    "./09-run-eval.sh <trace-id>",
    "./09-run-eval.sh <session-id> session",
    "./10-optimize-prompt.sh",
    "./11-cost-latency.sh",
    "./11-cost-latency.sh <trace-id>",
    "./12-compare-models.sh",
    "./12-compare-models.sh us.amazon.nova-pro-v1:0",
    "./12-compare-models.sh us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "./13-judge-stability.sh",
    "./13-judge-stability.sh <trace-id> [N]",
    "./99-cleanup.sh",
)

STUDENT_MARKER_TOKEN = "workshop-customizer:student-guide"
#: Must never appear in any release file (check_instructor_isolation).
INSTRUCTOR_MARKER_TOKEN = "workshop-customizer:instructor-only"
DRAFT_MARKER = "<!-- workshop-customizer:draft-preview -->"
REHEARSAL_BEGIN = "<!-- workshop-customizer:rehearsal-evidence:begin -->"
REHEARSAL_END = "<!-- workshop-customizer:rehearsal-evidence:end -->"
#: The class fallback the instructor guide names (the upstream-compatible reference pack); its own guide omits the line.
FALLBACK_PACK_ID = "hr-default"
#: The Guide's HR topic words (one list, shared with the release script-residue gate).
GUIDE_HR_TOKENS: tuple[str, ...] = HR_TOPIC_PATTERNS
#: labs.guide stepNotes / facilitatorNotes keys (schema $defs.guideStepId), in guide order.
GUIDE_STEP_IDS: tuple[str, ...] = (
    "prerequisites", "setup", "infra", "knowledge-base", "gateway", "skills", "agent", "memory", "conversation",
    "eval-env", "conversation-rerun", "evaluators", "baseline", "optimize", "cost-latency", "models",
    "judge-stability", "cleanup",
)
#: Guide step → (script, Guided Run id or None, template timing) for the run sheet.
RUN_SHEET: tuple[tuple[str, str, str | None, str], ...] = (
    ("setup", "00-setup.sh", "setup", "~5s"),
    ("infra", "00-deploy-infra.sh", "infra", "~5 min"),
    ("knowledge-base", "01-create-kb.sh", "knowledge-base", "~2 min"),
    ("gateway", "02-create-gateway.sh", "gateway", "~30s"),
    ("skills", "03-configure-skills.sh", "skills", "~5s"),
    ("agent", "04-deploy.sh", "agent", "~6 min"),
    ("memory", "05-setup-memory.sh", "memory", "~1–2 min"),
    ("conversation", "06-test-conversation.sh", "conversation", "~20s"),
    ("eval-env", "07-setup-eval-env.sh", "eval-env", "~30s"),
    ("conversation-rerun", "06-test-conversation.sh", None, "~20s"),
    ("evaluators", "08-create-evaluators.sh", "evaluators", "~2 min"),
    ("baseline", "09-run-eval.sh", "baseline", "~2–3 min\\*"),
    ("optimize", "10-optimize-prompt.sh", "optimize", "~4–5 min\\*"),
    ("cost-latency", "11-cost-latency.sh", "cost-latency", "~30s"),
    ("models", "12-compare-models.sh", "models", "~5 min\\*"),
    ("judge-stability", "13-judge-stability.sh", "judge-stability", "~1–2 min"),
    ("cleanup", "99-cleanup.sh", None, "~10–15 min"),
)

_PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_FENCE = re.compile(r"^\s*(?:>\s*)*(```|~~~)")
_NARRATIVE_COMMAND = re.compile(r"^\s*(\$ |\./|bash |sh |aws |npx |python3? |export |cd )")
_NARRATIVE_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_NARRATIVE_HTML = re.compile(r"<[A-Za-z/!]")
_CJK = re.compile(r"[　-〿㐀-䶿一-鿿＀-￯]+")
_NUMBER_PREFIX = re.compile(r"^\d+\.\s+")


class GuideError(Exception):
    """A guide template or engine bug (a missing/unused placeholder, a malformed template, bad input)."""


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------


def md_inline(value: Any, *, table: bool = False) -> str:
    """One line of prose: whitespace collapsed, ``<``/``>`` and backticks escaped (``|`` in tables)."""
    text = " ".join(str(value if value is not None else "").split())
    text = text.replace("<", "&lt;").replace(">", "&gt;").replace("`", "\\`")
    return text.replace("|", "\\|") if table else text


def md_para(value: Any) -> str:
    """Prose paragraphs (blank-line separated) with ``<``/``>`` escaped and lines kept."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "\n\n".join(p.replace("<", "&lt;").replace(">", "&gt;") for p in paragraphs)


def code(value: Any) -> str:
    """An identifier as inline code (identifiers never contain backticks; strip any defensively)."""
    return f"`{str(value).replace('`', '')}`"


def quote_block(value: Any) -> str:
    return "> " + md_inline(value)


def text_block(lines: Iterable[str]) -> str:
    body = "\n".join(line.replace("```", "'''") for line in lines)
    return f"```text\n{body}\n```"


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    rows = [list(r) for r in rows]
    head = "| " + " | ".join(headers) + " |"
    rule = "|" + "|".join("---" for _ in headers) + "|"
    body = ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join([head, rule, *body])


def mermaid_label(value: Any) -> str:
    text = " ".join(str(value).split()).replace('"', "'")
    return re.sub(r"[\[\]{}()|<>]", "", text)


def github_slug(heading: str) -> str:
    """The anchor GitHub gives a heading (lower case; punctuation and symbols dropped; spaces → '-')."""
    text = heading.strip().lower()
    kept = "".join(ch for ch in text if ch in " -_" or unicodedata.category(ch)[0] in "LNM")
    return kept.replace(" ", "-")


def fmt_num(value: float) -> str:
    return f"{value:g}"


def _join(lang: str, items: Sequence[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return gs.text(lang, "join.list").join(items[:-1]) + gs.text(lang, "join.and") + items[-1]


def extract_bash_commands(markdown: str) -> list[str]:
    """Command lines of every ```bash fence (a ``> `` prefix allowed), comments stripped, whitespace collapsed."""
    out: list[str] = []
    in_bash = False
    for raw in markdown.splitlines():
        line = re.sub(r"^\s*(?:>\s?)*", "", raw)
        stripped = line.strip()
        if not in_bash:
            if re.match(r"^```bash\s*$", stripped):
                in_bash = True
            continue
        if stripped.startswith("```"):
            in_bash = False
            continue
        if not stripped.startswith("export PATH="):
            stripped = re.sub(r"\s+#.*$", "", stripped)
            if stripped.startswith("#"):
                stripped = ""
        stripped = " ".join(stripped.split())
        if stripped:
            out.append(stripped)
    return out


def headings(markdown: str) -> list[tuple[int, str]]:
    """(level, text) of every ATX heading outside fenced code blocks."""
    found: list[tuple[int, str]] = []
    fenced = False
    for line in markdown.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if match:
            found.append((len(match.group(1)), match.group(2)))
    return found


def demote_headings(markdown: str, levels: int = 3) -> str:
    """Push every heading down ``levels`` levels (at most h6), leaving fenced blocks alone."""
    out: list[str] = []
    fenced = False
    for line in markdown.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            match = re.match(r"^(\s{0,3})(#{1,6})(\s.*)$", line)
            if match:
                line = "#" * min(6, len(match.group(2)) + levels) + match.group(3)
        out.append(line)
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------
# The template engine
# ---------------------------------------------------------------------------


def template_path(audience: str, lang: str) -> Path:
    if audience not in AUDIENCES:
        raise GuideError(f"unknown guide audience '{audience}'")
    return TEMPLATE_DIR / f"{audience}.{gs.lang_key(lang)}.md"


def load_template(audience: str, lang: str) -> str:
    path = template_path(audience, lang)
    if not path.is_file():
        raise GuideError(f"guide template missing: {path}")
    return path.read_text(encoding="utf-8")


def placeholders(template: str) -> set[str]:
    return set(_PLACEHOLDER.findall(template))


def render_template(template: str, values: Mapping[str, str]) -> str:
    """Fill ``{{name}}`` slots in ONE pass (inserted values are never re-scanned).

    A slot without a value, or a value no slot uses, is a :class:`GuideError`.
    """
    wanted = placeholders(template)
    missing = sorted(wanted - set(values))
    if missing:
        raise GuideError(f"guide template needs values for: {', '.join(missing)}")
    unused = sorted(set(values) - wanted)
    if unused:
        raise GuideError(f"guide values not used by the template: {', '.join(unused)}")
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), template)


def normalize_markdown(text: str) -> str:
    """NFC, ``\\n`` line ends, no trailing spaces, blank-line runs collapsed to one, one final newline."""
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [line.rstrip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if line == "" and (not out or out[-1] == ""):
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def navigation(template: str, links: Mapping[str, int]) -> dict[str, str]:
    """``toc`` and the ``links`` placeholders (name → section number) from the TEMPLATE's ``## N.`` headings.

    The numbered sections are fixed template text, so the table of contents and the anchors are
    computed before any pack value is inserted: pack prose is emitted literally and never re-scanned
    (a pack string cannot add, remove or rename a section, or inject a table of contents).
    """
    sections = []
    for level, text in headings(template):
        match = re.match(r"^(\d+)\.\s", text) if level == 2 else None
        if match:
            if _PLACEHOLDER.search(text):
                raise GuideError(f"guide template section heading '{text}' may not contain a placeholder")
            sections.append((int(match.group(1)), text))
    anchors = {n: f"#{github_slug(text)}" for n, text in sections}
    toc = [f"{n}. [{_NUMBER_PREFIX.sub('', text)}]({anchors[n]})" for n, text in sections]
    values = {"toc": "\n".join(toc)}
    for name, n in links.items():
        if n not in anchors:
            raise GuideError(f"guide template links section {n}, which has no '## {n}.' heading")
        values[name] = anchors[n]
    return values


# ---------------------------------------------------------------------------
# Teaching declaration (the ONLY reader of labs.teaching here)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Phenomenon:
    id: str
    kind: str
    case_ids: tuple[str, ...]
    document_ids: tuple[str, ...]
    teaching_point: str | None
    mechanism: str | None  # retrieval_gap only (buried | absent)
    design: str | None  # tool_use / refusal / escalation only (contrast | control)


@dataclass(frozen=True)
class TeachingView:
    declared: bool
    phenomena: tuple[Phenomenon, ...]

    def of_kind(self, kind: str, mechanism: str | None = None) -> tuple[Phenomenon, ...]:
        return tuple(p for p in self.phenomena if p.kind == kind and (mechanism is None or p.mechanism == mechanism))

    def case_ids(self, kind: str, mechanism: str | None = None) -> tuple[str, ...]:
        out: list[str] = []
        for p in self.of_kind(kind, mechanism):
            out += [cid for cid in p.case_ids if cid not in out]
        return tuple(out)

    @property
    def buried_advisory(self) -> bool:
        """A buried gap next to an absent one is advisory: the absent gap carries the contrast, since a rephrased
        retrieval can find a buried answer (teaching.buried_gap_unreliable)."""
        return bool(self.of_kind("retrieval_gap", "buried") and self.of_kind("retrieval_gap", "absent"))


def teaching_view(data: dict[str, Any]) -> TeachingView:
    """labs.teaching as the guide reads it. A phenomenon naming a non-practice case is a GuideError."""
    practice = {c["id"] for c in teaching.practice_cases(data)}
    items: list[Phenomenon] = []
    for p in teaching.phenomena(data):
        cases = tuple(c for c in p.get("caseIds") or [] if isinstance(c, str))
        stray = [c for c in cases if c not in practice]
        if stray:
            raise GuideError(f"labs.teaching phenomenon '{p.get('id')}' names non-practice case(s): {', '.join(stray)}")
        items.append(Phenomenon(
            id=str(p.get("id")), kind=str(p.get("kind")), case_ids=cases,
            document_ids=tuple(d for d in p.get("documentIds") or [] if isinstance(d, str)),
            teaching_point=p.get("teachingPoint") if isinstance(p.get("teachingPoint"), str) else None,
            mechanism=p.get("mechanism") if p.get("kind") == "retrieval_gap" else None,
            design=teaching.design(p),
        ))
    return TeachingView(declared=teaching.declared(data), phenomena=tuple(items))


# ---------------------------------------------------------------------------
# The student projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StudentCase:
    index: int
    case_id: str
    label: str
    query: str
    category: str
    provenance: str
    persona: str
    probe: bool


@dataclass(frozen=True)
class StudentNarrative:
    provenance: str | None = None
    tagline: str | None = None
    scenario_intro: str | None = None
    memory_lesson: str | None = None
    retrieval_contrast: str | None = None
    step_notes: tuple[tuple[str, str], ...] = ()
    experiments: tuple[tuple[str, str], ...] = ()
    attribution: str | None = None


@dataclass(frozen=True)
class StudentView:
    """Everything the student guide may show; nothing else is reachable from it."""

    pack_id: str
    display_name: str
    language: str
    pack_kind: str
    customer_name: str | None
    data_classification: str | None
    customer_block: bool
    #: Banner V1 vs V2 (designs[1] D): any customer_confirmed item in the pack — the same rule the
    #: instructor guide uses, so both guides show the same banner (a bit, never the item itself).
    any_customer_confirmed: bool
    audience: str
    purpose: str
    scope: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    handoff: tuple[str, ...]
    prohibited: tuple[str, ...]
    roles: tuple[tuple[str, str, tuple[str, ...]], ...]
    facts: tuple[tuple[str, str, str], ...]  # (statement, criticality, provenance)
    documents: tuple[tuple[str, str, str, str, bool], ...]  # (id, title, file, provenance, noise)
    noise_text: str | None
    tools: tuple[tuple[str, str, str, str], ...]  # (name, description, kind, provenance)
    cases: tuple[StudentCase, ...]
    phenomena: tuple[Phenomenon, ...]
    teaching_declared: bool
    facts_script: ScriptFacts
    closing_lines: tuple[str, ...]
    narrative: StudentNarrative
    supplement: str | None


def _provenance_groups(data: dict[str, Any]) -> tuple[tuple[str, list[Any]], ...]:
    """Every provenance-bearing item of the pack, grouped as the instructor provenance matrix counts them."""
    narrative = _narrative_block(data)
    return (
        ("matrix.facts", list(data.get("facts") or [])),
        ("matrix.tools", list(data.get("tools") or [])),
        ("matrix.documents", list((data.get("knowledge") or {}).get("documents") or [])),
        ("matrix.golden", list((data.get("evaluation") or {}).get("goldenSet") or [])),
        ("matrix.narrative", [narrative] if narrative else []),
    )


def any_customer_confirmed(data: dict[str, Any]) -> bool:
    """Whether any item of the pack is really customer_confirmed (banner V2 unless the pack is a reference
    pack). A simulated confirmation (scenario.is_simulated_confirmation) never counts, as in the engine."""
    return any(isinstance(i, dict) and is_customer_confirmed(i) for _k, items in _provenance_groups(data) for i in items)


def _tag_key(item: Mapping[str, Any]) -> str:
    """The provenance tag an item is shown with, from its derived class (scenario.item_class): a customer
    fact is customer-confirmed, every reviewed synthetic class a synthetic teaching setting, anything else
    (a simulated confirmation included) a draft."""
    cls = item_class(dict(item))
    return {"customer-fact": "customer_confirmed", "draft": "ai_draft"}.get(cls, "sa_synthetic")


def _student_facts(data: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """Facts cited by at least one practice case, or by no case at all (holdout-only facts stay out)."""
    cited_practice, cited_any = set(), set()
    for case in data["evaluation"]["goldenSet"]:
        for fid in case.get("basis") or []:
            cited_any.add(fid)
            if case.get("set") == "practice":
                cited_practice.add(fid)
    return tuple(
        (str(f["statement"]), str(f.get("criticality", "blocking")), _tag_key(f))
        for f in data.get("facts", [])
        if f["id"] in cited_practice or f["id"] not in cited_any
    )


def _narrative_block(data: dict[str, Any]) -> dict[str, Any]:
    block = (data.get("labs") or {}).get("guide")
    return block if isinstance(block, dict) else {}


def _student_narrative(data: dict[str, Any]) -> StudentNarrative:
    block = _narrative_block(data)
    if not block:
        return StudentNarrative()

    def s(key: str) -> str | None:
        value = block.get(key)
        return value if isinstance(value, str) and value.strip() else None

    notes = block.get("stepNotes") if isinstance(block.get("stepNotes"), dict) else {}
    experiments = [e for e in block.get("experiments") or [] if isinstance(e, dict) and e.get("audience") == "student"]
    return StudentNarrative(
        provenance=str(block.get("provenance") or "") or None,
        tagline=s("tagline"), scenario_intro=s("scenarioIntro"), memory_lesson=s("memoryLesson"),
        retrieval_contrast=s("retrievalContrast"),
        step_notes=tuple((k, str(notes[k])) for k in GUIDE_STEP_IDS if isinstance(notes.get(k), str) and notes[k].strip()),
        experiments=tuple((str(e.get("title", "")), str(e.get("body", ""))) for e in experiments),
        attribution=s("attribution"),
    )


def _pack_text(path: Path) -> str:
    """A pack file as text. Undecodable bytes are replaced, never fatal: ``check_guide_narrative``
    reports a supplement that is not UTF-8 (``guide.supplement_encoding``)."""
    return path.read_text(encoding="utf-8", errors="replace")


def _supplement_path(root: Path | None, rel: Any) -> Path | None:
    if root is None or not isinstance(rel, str):
        return None
    from .scenario import resolve_inside

    path = resolve_inside(Path(root), rel)
    return path if path.is_file() else None


def _supplement(root: Path | None, rel: Any) -> str | None:
    path = _supplement_path(root, rel)
    return _pack_text(path) if path is not None else None


def _closing_lines(data: dict[str, Any], facts: ScriptFacts) -> tuple[str, ...]:
    from .render import optimize_closing_lines  # lazy: render imports compiler, which imports this module

    return tuple(optimize_closing_lines(data, facts))


def student_view(data: dict[str, Any], facts: ScriptFacts, teach: TeachingView, *, root: Path | None = None) -> StudentView:
    """The projection the student builder receives (designs[1] dataModel §4)."""
    agent = data["agent"]
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
    fc = facts.first_conversation
    safe_first = replace(fc, must_not_mention=(), teaching_point=None) if fc is not None else None
    safe_facts = replace(facts, first_conversation=safe_first)
    noise_plan = (data.get("knowledge") or {}).get("noisePlan") or {}
    golden_by_id = {c.get("id"): c for c in data["evaluation"]["goldenSet"] if isinstance(c, dict)}
    noise_text = None
    if noise_plan.get("enabled"):
        noise_text = str(noise_plan.get("description") or noise_plan.get("rationale") or "") or None
    return StudentView(
        pack_id=str(data["id"]), display_name=str(data.get("displayName", data["id"])), language=gs.lang_key(data.get("language")),
        pack_kind=str(data.get("packKind", "")),
        customer_name=str(customer.get("name")) if customer.get("name") else None,
        data_classification=str(customer.get("dataClassification")) if customer.get("dataClassification") else None,
        customer_block=bool(customer),
        any_customer_confirmed=any_customer_confirmed(data),
        audience=str(agent.get("audience", "")), purpose=str(agent.get("purpose", "")),
        scope=tuple(agent.get("scope") or ()), out_of_scope=tuple(agent.get("outOfScope") or ()),
        handoff=tuple(agent.get("handoffConditions") or ()), prohibited=tuple(agent.get("prohibitedBehaviors") or ()),
        roles=tuple((str(r.get("name", "")), str(r.get("description", "")), tuple(r.get("permissions") or ())) for r in agent.get("roles") or []),
        facts=_student_facts(data),
        documents=tuple((str(d["id"]), str(d["title"]), Path(d["file"]).name, _tag_key(d), bool(d.get("noise")))
                        for d in data["knowledge"]["documents"]),
        noise_text=noise_text,
        tools=tuple((str(t["name"]), str(t.get("description", "")), str(t.get("kind", "")), _tag_key(t)) for t in data["tools"]),
        cases=tuple(StudentCase(c.index, c.case_id, c.label, c.query, c.category, _tag_key(golden_by_id.get(c.case_id, {"provenance": c.provenance})),
                                c.actor_id, c.probe) for c in facts.eval_cases),
        phenomena=teach.phenomena,
        teaching_declared=teach.declared,
        facts_script=safe_facts,
        closing_lines=_closing_lines(data, facts),
        narrative=_student_narrative(data),
        supplement=_supplement(root, (data.get("labs") or {}).get("studentGuideFile")),
    )


# ---------------------------------------------------------------------------
# Shared blocks (both audiences)
# ---------------------------------------------------------------------------


def _tag(lang: str, provenance: str) -> str:
    key = f"tag.{provenance}" if provenance in ("customer_confirmed", "sa_synthetic", "ai_draft", "pending") else "tag.ai_draft"
    return str(gs.text(lang, key))


def _banner(lang: str, *, pack_kind: str, customer_name: str | None, classification: str | None, any_confirmed: bool) -> str:
    if pack_kind == "reference" or not any_confirmed:
        lines = [str(gs.text(lang, "banner.reference"))]
    else:
        name = md_inline(customer_name) if customer_name else str(gs.text(lang, "the_customer"))
        lines = [str(gs.text(lang, "banner.customer")).format(name=name)]
    if classification in ("internal", "confidential"):
        name = md_inline(customer_name) if customer_name else str(gs.text(lang, "the_customer"))
        lines.append(str(gs.text(lang, "banner.classification")).format(
            name=name, classification=gs.text(lang, f"classification.{classification}")))
    return "> [!IMPORTANT]\n" + "\n>\n".join(f"> {line}" for line in lines)


def _thresholds() -> Mapping[str, float]:
    return teaching.THRESHOLDS


def _shared_values(lang: str) -> dict[str, str]:
    t = _thresholds()
    legend_rows = []
    for (key, printed), (metric, question, low) in zip(gs.THELMA_TAGS, gs.text(lang, "legend")):  # type: ignore[arg-type]
        legend_rows.append((code(printed), f"**{key}** {md_inline(metric, table=True)}",
                            md_inline(str(question).format(gr=fmt_num(t["grPass"])), table=True), md_inline(low, table=True)))
    diagnosis_rows = [(code(pattern), md_inline(meaning, table=True), md_inline(component, table=True))
                      for (pattern, component), meaning in zip(gs.DIAGNOSIS_PATTERNS, gs.text(lang, "diagnosis"))]  # type: ignore[arg-type]
    diagnosis_rows += [(code(p), md_inline(m, table=True), md_inline(c, table=True)) for p, m, c in gs.text(lang, "diagnosis.extra")]  # type: ignore[misc]
    return {
        "thelma_legend": table(gs.text(lang, "h.legend"), legend_rows),  # type: ignore[arg-type]
        "diagnosis_table": table(gs.text(lang, "h.diagnosis"), diagnosis_rows),  # type: ignore[arg-type]
        "diagnosis_levels": str(gs.text(lang, "diagnosis.levels")).format(low=fmt_num(gs.INTERPLAY_LOW), high=fmt_num(gs.INTERPLAY_HIGH)),
        "mtg_reading": str(gs.text(lang, "mtg.reading")).format(gsr=gs.MTG_PASS_PERCENT),
        "rcof_table": table(gs.text(lang, "h.rcof"), [(code(c), name) for c, name in gs.RCOF]),  # type: ignore[arg-type]
        "l1_statuses": table(gs.text(lang, "h.l1"), [(code(s), m) for s, m in gs.text(lang, "l1.statuses")]),  # type: ignore[misc]
        "l1_compare": str(gs.text(lang, "l1.compare")),
        # 09's retry budget, from the constants render writes into 09 (verify_rendered keeps them equal).
        "retry_messages": table(gs.text(lang, "h.messages"), [  # type: ignore[arg-type]
            (message.format(pause=script_facts.EVAL_RETRY_SECONDS, attempts=script_facts.EVAL_ATTEMPTS), meaning)
            for message, meaning in gs.text(lang, "messages")]),  # type: ignore[misc]
    }


def _step_notes(lang: str, notes: Iterable[tuple[str, str]]) -> dict[str, str]:
    given = dict(notes)
    return {
        f"note_{sid.replace('-', '_')}": str(gs.text(lang, "scenario_note")).format(text=md_inline(given[sid])) if sid in given else ""
        for sid in GUIDE_STEP_IDS
    }


def _names_table(lang: str, f: ScriptFacts) -> str:
    rows = [
        (gs.text(lang, "name.agent"), code(f.agent_name)),
        (gs.text(lang, "name.memory"), code(f.memory_name)),
        (gs.text(lang, "name.gateway"), code(f.gateway_name)),
        (gs.text(lang, "name.target"), f"{code(f.target_name)} ({code(f.compact_target + '___<tool>')})"),
        (gs.text(lang, "name.lambda"), code(f.lambda_name)),
        (gs.text(lang, "name.retrieval"), code(f.retrieval_tool)),
        (gs.text(lang, "name.kb"), code(f.kb_name)),
        (gs.text(lang, "name.kb_prefix"), code(f.kb_prefix)),
        (gs.text(lang, "name.ssm"), f"{code(f.ssm_prefix + '/knowledge_base_id')}, {code(f.ssm_prefix + '/gateway_arn')}"),
        (gs.text(lang, "name.evaluators"), f"{code(f.thelma_evaluator)}, {code(f.mtg_evaluator)}"),
        (gs.text(lang, "name.skills"), ", ".join(code(s) for s in f.skills) or gs.text(lang, "no_skills")),
        (gs.text(lang, "name.runs"), code(f"~/workshop/eval-runs/{f.agent_name}/")),
    ]
    return table(gs.text(lang, "h.names"), rows)  # type: ignore[arg-type]


def _case_label(cases: Mapping[str, Any], cid: str) -> str:
    case = cases.get(cid)
    label = case.label if isinstance(case, StudentCase) else (case or {}).get("label", cid)
    return f"**{md_inline(label)}**"


def _labels(lang: str, cases: Mapping[str, Any], ids: Sequence[str]) -> str:
    return _join(lang, [_case_label(cases, cid) for cid in ids])


def _contrast_paragraph(lang: str, teach_phenomena: Sequence[Phenomenon], cases: Mapping[str, Any], declared: bool) -> str:
    view = TeachingView(declared=declared, phenomena=tuple(teach_phenomena))
    fixable, buried, absent = view.case_ids("prompt_fixable"), view.case_ids("retrieval_gap", "buried"), view.case_ids("retrieval_gap", "absent")
    if not declared or not fixable or not (buried or absent):
        return str(gs.text(lang, "contrast.undeclared"))
    lines = [str(gs.text(lang, "contrast.intro"))]
    if fixable:
        lines.append("- " + str(gs.text(lang, "contrast.fixable")).format(labels=_labels(lang, cases, fixable)))
    if buried:
        key = "contrast.buried.advisory" if view.buried_advisory else "contrast.buried"
        lines.append("- " + str(gs.text(lang, key)).format(labels=_labels(lang, cases, buried)))
    if absent:
        lines.append("- " + str(gs.text(lang, "contrast.absent")).format(labels=_labels(lang, cases, absent)))
    lines += ["", str(gs.text(lang, "contrast.close"))]
    return "\n".join(lines)


def _adlc_tip(lang: str, teach_phenomena: Sequence[Phenomenon], cases: Mapping[str, Any], declared: bool) -> str:
    view = TeachingView(declared=declared, phenomena=tuple(teach_phenomena))
    fixable = view.case_ids("prompt_fixable")
    buried, absent = view.case_ids("retrieval_gap", "buried"), view.case_ids("retrieval_gap", "absent")
    if not declared or not fixable or not (buried or absent):
        return str(gs.text(lang, "adlc.undeclared"))
    if view.buried_advisory:  # a rephrased retrieval may find the buried answer: promise only the absent gaps
        gaps, why = absent, gs.text(lang, "adlc.tip.absent")
    else:
        gaps = buried + absent
        why = gs.text(lang, "adlc.tip.mixed" if buried and absent else ("adlc.tip.buried" if buried else "adlc.tip.absent"))
    return str(gs.text(lang, "adlc.tip")).format(fixable=_labels(lang, cases, fixable), gaps=_labels(lang, cases, gaps), why=why)


def _expect_text(lang: str, p: Phenomenon, *, buried_advisory: bool = False) -> str:
    t = _thresholds()
    values = {"gr": fmt_num(t["grPass"]), "sqc": fmt_num(t["retrievalOkSqc"]), "bar": fmt_num(t["minImprovement"]),
              "sp2": fmt_num(t["retrievalFailedSp2"])}
    if p.kind == "retrieval_gap":
        values["sqc"] = fmt_num(t["retrievalFailedSqc"])
        mechanism = p.mechanism if p.mechanism in teaching.MECHANISMS else "buried"
        key = f"expect.retrieval_gap.{mechanism}" + (".advisory" if mechanism == "buried" and buried_advisory else "")
    elif p.kind in teaching.L1_KINDS:
        key = f"expect.{p.kind}.{p.design or teaching.DEFAULT_DESIGN}"
    elif p.kind in ("prompt_fixable", "noise_grounding"):
        key = f"expect.{p.kind}"
    else:
        return ""
    return str(gs.text(lang, key)).format(**values)


def _kind_label(lang: str, p: Phenomenon) -> str:
    if p.kind == "retrieval_gap":
        return str(gs.text(lang, f"kind.retrieval_gap.{p.mechanism if p.mechanism in teaching.MECHANISMS else 'buried'}"))
    key = f"kind.{p.kind}"
    return str(gs.text(lang, key)) if key in gs.STRINGS[gs.lang_key(lang)] else p.kind


# ---------------------------------------------------------------------------
# The student guide
# ---------------------------------------------------------------------------


def _student_values(v: StudentView, *, lang: str, template_commit: str, generator_version: str, draft: bool) -> dict[str, str]:
    f = v.facts_script
    t = _thresholds()
    cases = {c.case_id: c for c in v.cases}
    nar = v.narrative
    fc = f.first_conversation
    dash = str(gs.text(lang, "dash"))

    def fact_table(items: list[tuple[str, str, str]], none_key: str) -> str:
        if not items:
            return str(gs.text(lang, none_key))
        return table(gs.text(lang, "h.facts"), [(md_inline(s, table=True), gs.text(lang, f"criticality.{c}") if c in ("blocking", "advisory") else md_inline(c, table=True),
                                _tag(lang, p)) for s, c, p in items])

    confirmed = [x for x in v.facts if x[2] == "customer_confirmed"]
    others = [x for x in v.facts if x[2] != "customer_confirmed"]

    def tag_counts(provenances: Iterable[str]) -> str:
        counts: dict[str, int] = {}
        for p in provenances:
            counts[p] = counts.get(p, 0) + 1
        order = ["customer_confirmed", "sa_synthetic", "ai_draft", "pending"]
        return ", ".join(str(gs.text(lang, "count_tag")).format(n=counts[p], tag=_tag(lang, p))
                         for p in sorted(counts, key=lambda p: order.index(p) if p in order else 9))

    noise_docs = [d for d in v.documents if d[4]]
    mocks = [x for x in v.tools if x[2] != "retrieval"]
    assets = str(gs.text(lang, "assets")).format(
        docs=len(v.documents), doc_tags=tag_counts(d[3] for d in v.documents), noise=len(noise_docs),
        tools=len(v.tools), mocks=len(mocks), cases=len(v.cases), case_tags=tag_counts(c.provenance for c in v.cases))

    scenario_rows = [
        (gs.text(lang, "row.audience"), md_inline(v.audience, table=True)),
        (gs.text(lang, "row.purpose"), md_inline(v.purpose, table=True)),
        (gs.text(lang, "row.scope"), "; ".join(md_inline(s, table=True) for s in v.scope)),
    ]
    if v.out_of_scope:
        scenario_rows.append((gs.text(lang, "row.out_of_scope"), "; ".join(md_inline(s, table=True) for s in v.out_of_scope)))
    scenario_rows += [
        (gs.text(lang, "row.handoff"), "; ".join(md_inline(s, table=True) for s in v.handoff)),
        (gs.text(lang, "row.prohibited"), "; ".join(md_inline(s, table=True) for s in v.prohibited)),
    ]

    user_label = mermaid_label(", ".join(r[0] for r in v.roles) or gs.text(lang, "diagram.user"))
    mock_names = mermaid_label(", ".join(x[0] for x in mocks) or dash)
    skill_names = mermaid_label(", ".join(f.skills) or gs.text(lang, "no_skills"))
    diagram = "\n".join([
        "```mermaid",
        "flowchart LR",
        f'  U["{gs.text(lang, "diagram.user")}: {user_label}"] --> H["{gs.text(lang, "diagram.harness")} {mermaid_label(f.agent_name)} - VPC"]',
        f'  H --> G["{gs.text(lang, "diagram.gateway")} {mermaid_label(f.gateway_name)}"]',
        f'  G --> L["{gs.text(lang, "diagram.lambda")} {mermaid_label(f.lambda_name)}"]',
        f'  L --> R["{mermaid_label(f.retrieval_tool)}"]',
        f'  R --> KB["{gs.text(lang, "diagram.kb")} {mermaid_label(f.kb_name)} - S3 Vectors"]',
        f'  L --> M["{gs.text(lang, "diagram.mocks")}: {mock_names}"]',
        f'  H --- MEM["{gs.text(lang, "diagram.memory")} {mermaid_label(f.memory_name)}"]',
        f'  H --- SK["{gs.text(lang, "diagram.skills")}: {skill_names}"]',
        '  H -.-> CW["CloudWatch aws/spans"]',
        '  CW -.-> T["THELMA - TRACE"]',
        '  CW -.-> MG["Mind the Goal - SESSION"]',
        '  CW -.-> L1["L1 - l1_eval.py"]',
        "```",
    ])

    skill_list = ", ".join(code(f"~/workshop/skills/{s}") for s in f.skills) or code("~/workshop/skills")
    total, n = len(v.cases), f.probe_count
    l1_wait = int(DEFAULT_MAX_POLLS * DEFAULT_POLL_SECONDS)
    exec_rows = [
        ("1", code("00-setup.sh"), "0", "~5s", str(gs.text(lang, "exec.setup")).format(skills=f" ({skill_list})" if f.skills else "")),
        ("2", code("00-deploy-infra.sh"), "0", gs.text(lang, "exec.required_time"), gs.text(lang, "exec.infra")),
        ("3", code("01-create-kb.sh"), "0", "~2 min", str(gs.text(lang, "exec.kb")).format(kb=f.kb_name, docs=len(v.documents))),
        ("4", code("02-create-gateway.sh"), "2", "~30s", str(gs.text(lang, "exec.gateway")).format(fn=f.lambda_name, gw=f.gateway_name, tools=len(v.tools))),
        ("5", code("03-configure-skills.sh"), "2", "~5s", str(gs.text(lang, "exec.skills")).format(skills=f" ({', '.join(code(s) for s in f.skills)})" if f.skills else "")),
        ("6", code("04-deploy.sh"), "2", "~6 min", str(gs.text(lang, "exec.agent")).format(agent=f.agent_name, memory=f.memory_name)),
        ("7", code("05-setup-memory.sh"), "2", "~1–2 min", gs.text(lang, "exec.memory")),
        ("8", code("06-test-conversation.sh"), "3", "~20s", str(gs.text(lang, "exec.conversation")).format(actor=fc.actor_id if fc else dash)),
        ("9", code("07-setup-eval-env.sh"), "4", "~30s", gs.text(lang, "exec.eval_env")),
        ("10", f"{code('06-test-conversation.sh')} {gs.text(lang, 'exec.again')}", "4", "~20s", gs.text(lang, "exec.rerun")),
        ("11", code("08-create-evaluators.sh"), "4", "~2 min", gs.text(lang, "exec.evaluators")),
        ("12", code("09-run-eval.sh"), "4", "~2–3 min\\*", str(gs.text(lang, "exec.baseline")).format(total=total, n=n)),
        ("13", code("10-optimize-prompt.sh"), "5", "~4–5 min\\*", str(gs.text(lang, "exec.optimize")).format(total=total, n=n)),
        ("—", code("11-cost-latency.sh"), "6", "~30s", str(gs.text(lang, "exec.cost")).format(n=f.recent_n)),
        ("—", code("12-compare-models.sh"), "—", "~5 min\\*", str(gs.text(lang, "exec.models")).format(total=total)),
        ("—", code("13-judge-stability.sh"), "—", "~1–2 min", str(gs.text(lang, "exec.stability")).format(runs=f.stability_runs)),
        ("—", code("99-cleanup.sh"), "—", "~10–15 min", gs.text(lang, "exec.cleanup")),
    ]

    doc_rows = [(md_inline(title, table=True), code(name), _tag(lang, prov), gs.text(lang, "yes" if noise else "no"))
                for _id, title, name, prov, noise in v.documents]
    tool_rows = [(code(name), md_inline(desc, table=True), gs.text(lang, "tool.retrieval" if kind == "retrieval" else "tool.mock"), _tag(lang, prov))
                 for name, desc, kind, prov in v.tools]
    practice_rows = [
        (str(c.index + 1), md_inline(c.label, table=True), md_inline(c.query, table=True),
         gs.text(lang, f"category.{c.category}") if c.category in ("normal", "boundary", "prohibited") else md_inline(c.category, table=True),
         code(c.persona), gs.text(lang, "scored.probe" if c.probe else "scored.other"), _tag(lang, c.provenance))
        for c in v.cases
    ]

    teach = TeachingView(declared=v.teaching_declared, phenomena=v.phenomena)
    doc_titles = {d[0]: d[1] for d in v.documents}
    look: list[str] = []
    noise_ids = [d for p in teach.of_kind("noise_grounding") for d in p.document_ids]
    if noise_ids:
        look.append("- " + str(gs.text(lang, "look.noise")).format(docs=_join(lang, [f"**{md_inline(doc_titles.get(d, d))}**" for d in dict.fromkeys(noise_ids)])))
    if teach.case_ids("prompt_fixable"):
        look.append("- " + str(gs.text(lang, "look.fixable")).format(labels=_labels(lang, cases, teach.case_ids("prompt_fixable")),
                                                                       sqc=fmt_num(t["retrievalOkSqc"]), gr=fmt_num(t["grPass"])))
    gaps = teach.case_ids("retrieval_gap")
    if gaps:
        look.append("- " + str(gs.text(lang, "look.gap")).format(labels=_labels(lang, cases, gaps)))

    expect_rows = []
    if fc is not None:
        expect_rows.append((gs.text(lang, "kind.memory"), gs.text(lang, "expect.memory.where"),
                            str(gs.text(lang, "expect.memory")).format(notice=md_inline(script_facts.memory_notice(fc), table=True)), dash))
    for p in teach.phenomena:
        text_ = _expect_text(lang, p, buried_advisory=teach.buried_advisory)
        if not text_:
            continue
        if p.kind == "noise_grounding":
            where = str(gs.text(lang, "expect.documents")).format(titles=_join(lang, [f"**{md_inline(doc_titles.get(d, d), table=True)}**" for d in p.document_ids]))
        else:
            where = _join(lang, [md_inline(cases[c].label, table=True) for c in p.case_ids if c in cases])
        expect_rows.append((_kind_label(lang, p), where, text_, md_inline(p.teaching_point or dash, table=True)))

    if nar.experiments:
        exp = [f"### {gs.text(lang, 'experiments.heading')}", ""]
        for title, body in nar.experiments:
            exp += [f"**{md_inline(title)}** — {md_inline(body)}", ""]
        student_experiments = "\n".join(exp)
    else:
        student_experiments = ""

    attribution = [str(gs.text(lang, "attr.kb")).format(docs=len(v.documents), doc_tags=tag_counts(d[3] for d in v.documents), noise=len(noise_docs))]
    if v.customer_block:
        attribution.append(str(gs.text(lang, "attr.customer")))
    if nar.attribution:
        attribution.append(md_para(nar.attribution))

    stability = cases.get(f.stability_case_id or "")
    first_console = text_block([script_facts.first_conversation_topic(fc), "…", script_facts.memory_notice(fc)]) if fc else dash
    values = {
        "marker": f"<!-- {STUDENT_MARKER_TOKEN} pack={v.pack_id} lang={lang} -->",
        "draft_banner": f"{DRAFT_MARKER}\n{gs.text(lang, 'banner.draft')}" if draft else "",
        "title": md_inline(v.display_name),
        "tagline": md_inline(nar.tagline or v.purpose),
        "generated_by": str(gs.text(lang, "generated_by")).format(version=generator_version, pack=v.pack_id, commit=template_commit[:12]),
        "scenario_intro": md_para(nar.scenario_intro) if nar.scenario_intro else f"**{md_inline(v.display_name)}**: {md_inline(v.purpose)}",
        "scenario_table": table(("", ""), scenario_rows),
        "roles_table": table(gs.text(lang, "h.roles"), [(md_inline(n_, table=True), md_inline(d, table=True), ", ".join(code(p) for p in perms) or dash)  # type: ignore[arg-type]
                                                        for n_, d, perms in v.roles]),
        "provenance_banner": _banner(lang, pack_kind=v.pack_kind, customer_name=v.customer_name, classification=v.data_classification,
                                     any_confirmed=v.any_customer_confirmed),
        "customer_facts": fact_table(confirmed, "none_facts"),
        "synthetic_facts": fact_table(others, "none_synthetic"),
        "asset_summary": assets,
        "gr_pass": fmt_num(t["grPass"]),
        "mtg_pass": str(gs.MTG_PASS_PERCENT),
        "judge_model": str(f.judge_model).replace("`", ""),
        "noise_paragraph": (str(gs.text(lang, "noise.lead")) + " " + md_para(v.noise_text)) if v.noise_text else str(gs.text(lang, "noise.none")),
        "architecture_diagram": diagram,
        "names_table": _names_table(lang, f),
        "adlc_tip": _adlc_tip(lang, v.phenomena, cases, v.teaching_declared),
        "execution_table": table(gs.text(lang, "h.execution"), exec_rows),  # type: ignore[arg-type]
        "timing_footnote": str(gs.text(lang, "timing_footnote")).format(total=total, l1_wait=l1_wait),
        "skill_dirs": skill_list,
        "doc_count": str(len(v.documents)),
        "kb_name": f.kb_name,
        "ssm_prefix": f.ssm_prefix,
        "documents_table": table(gs.text(lang, "h.documents"), doc_rows),  # type: ignore[arg-type]
        "noise_sentence": str(gs.text(lang, "noise.docs")).format(n=len(noise_docs)) if noise_docs else "",
        "lambda_name": f.lambda_name,
        "gateway_name": f.gateway_name,
        "target_name": f.target_name,
        "tool_count": str(len(v.tools)),
        "tools_table": table(gs.text(lang, "h.tools"), tool_rows),  # type: ignore[arg-type]
        "compact_target": f.compact_target,
        "skills_sentence": str(gs.text(lang, "skills.some")).format(n=len(f.skills), names=", ".join(code(s) for s in f.skills)) if f.skills else str(gs.text(lang, "skills.none")),
        "agent_name": f.agent_name,
        "memory_name": f.memory_name,
        "first_actor": fc.actor_id if fc else dash,
        "first_query": quote_block(fc.query) if fc else dash,
        "first_console": first_console,
        "memory_lesson": md_para(nar.memory_lesson) if nar.memory_lesson else "",
        "thelma_evaluator": f.thelma_evaluator,
        "mtg_evaluator": f.mtg_evaluator,
        "total_questions": str(total),
        "probe_count": str(n),
        "practice_table": table(gs.text(lang, "h.practice"), practice_rows),  # type: ignore[arg-type]
        "what_to_look_for": "\n".join(look) or dash,
        "contrast_paragraph": _contrast_paragraph(lang, v.phenomena, cases, v.teaching_declared),
        "closing_lines": text_block(v.closing_lines),
        "retrieval_contrast": md_para(nar.retrieval_contrast) if nar.retrieval_contrast else "",
        "expectations_table": table(gs.text(lang, "h.expect"), expect_rows),  # type: ignore[arg-type]
        "noise_band_note": str(gs.text(lang, "expect.noise_band")).format(bar=fmt_num(t["minImprovement"])),
        "recent_n": str(f.recent_n),
        "stability_runs": str(f.stability_runs),
        "stability_case": f"**{md_inline(stability.label)}**" if stability else dash,
        "stable_std": fmt_num(gs.STABILITY_STABLE),
        "moderate_std": fmt_num(gs.STABILITY_MODERATE),
        "student_experiments": student_experiments,
        "attribution_block": "\n\n".join(attribution),
        "template_commit_short": template_commit[:12],
        "supplement": (f"\n## {gs.text(lang, 'supplement.heading')}\n\n{demote_headings(v.supplement)}\n") if v.supplement else "",
    }
    values.update(_shared_values(lang))
    values.update(_step_notes(lang, nar.step_notes))
    return values


#: Student-template link placeholders → the numbered section they point at.
STUDENT_LINKS: Mapping[str, int] = {"anchor_prerequisites": 6, "anchor_methodology": 5}


def build_student_guide(view: StudentView, *, template_commit: str, generator_version: str, draft: bool = False) -> str:
    lang = view.language
    template = load_template("student", lang)
    values = _student_values(view, lang=lang, template_commit=template_commit, generator_version=generator_version, draft=draft)
    values.update(navigation(template, STUDENT_LINKS))
    return normalize_markdown(render_template(template, values))


# ---------------------------------------------------------------------------
# The instructor guide
# ---------------------------------------------------------------------------


def _compact(value: Any, limit: int | None = 160) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    if limit is not None and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _expected_text(expected: Mapping[str, Any]) -> str:
    parts = []
    for key in sorted(expected):
        value = expected[key]
        if isinstance(value, list):
            if value and all(isinstance(v, list) for v in value):
                shown = " | ".join("(" + ", ".join(str(x) for x in group) + ")" for group in value)
            else:
                shown = ", ".join(str(x) for x in value)
        else:
            shown = json.dumps(value, ensure_ascii=False)
        parts.append(f"`{key}`: {md_inline(shown, table=True)}")
    return "; ".join(parts) or "—"


@dataclass(frozen=True)
class InstructorContext:
    data: dict[str, Any]
    facts: ScriptFacts
    teach: TeachingView
    root: Path | None
    scenario_sha256: str
    warnings: tuple[Finding, ...]
    rehearsal: Mapping[str, Any] | None
    release_version: str | None


def _instructor_values(ctx: InstructorContext, *, lang: str, template_commit: str, generator_version: str, draft: bool) -> dict[str, str]:
    data, f, teach = ctx.data, ctx.facts, ctx.teach
    t = _thresholds()
    dash = str(gs.text(lang, "dash"))
    golden = data["evaluation"]["goldenSet"]
    by_id = {c["id"]: c for c in golden}
    facts_by_id = {x["id"]: x for x in data.get("facts", [])}
    practice_order = [by_id[c.case_id] for c in f.eval_cases if c.case_id in by_id]
    holdout = [c for c in golden if c.get("set") == "holdout"]
    narrative = _narrative_block(data)

    def crit(value: Any) -> str:
        return str(gs.text(lang, f"criticality.{value}")) if value in ("blocking", "advisory") else md_inline(value, table=True)

    def origin(item: Mapping[str, Any]) -> str:
        """The item's derived class in the pack language (scenario.item_class_label), plus its origin kind."""
        o = item.get("origin")
        label = md_inline(item_class_label(dict(item), lang), table=True)
        return f"{label} ({code(o.get('kind'))})" if isinstance(o, dict) and o.get("kind") else label

    # -- provenance ------------------------------------------------------------------------------
    groups = _provenance_groups(data)
    columns = ("customer_confirmed", "sa_synthetic", "ai_draft", "pending")
    matrix_rows = [(gs.text(lang, key), *[str(sum(1 for i in items if i.get("provenance") == p)) for p in columns]) for key, items in groups]
    from .scenario import provenance_gate

    waived = [x for x in provenance_gate(data) if x.waived]
    any_confirmed = any_customer_confirmed(data)
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}

    anchor_rows = []
    anchors = customer_anchors(data)  # the engine's rule: real confirmations only, every basis fact confirmed
    for category in ("normal", "boundary", "prohibited"):
        ok = anchors.get(category) or []
        anchor_rows.append((gs.text(lang, f"category.{category}"), ", ".join(code(i) for i in ok) or dash,
                            gs.text(lang, "anchor.ok" if ok else "anchor.missing")))
    classification = customer.get("dataClassification")

    # -- declarations -----------------------------------------------------------------------------
    fc = f.first_conversation
    declared = str(gs.text(lang, "decl.declared" if teach.declared else "decl.undeclared"))
    gap_items = []
    for p in teach.of_kind("retrieval_gap"):
        gap_items += [f"{code(c)} ({p.mechanism})" for c in p.case_ids]
    decl_rows = [
        (gs.text(lang, "decl.first"), (f"{md_inline(fc.label or dash, table=True)} — {code(fc.actor_id)}" if fc else dash)),
        (gs.text(lang, "decl.probes"), f"{f.probe_count}: " + (", ".join(code(c) for c in f.probe_case_ids) or dash)),
        (gs.text(lang, "decl.fixable"), ", ".join(code(c) for c in teach.case_ids("prompt_fixable")) or dash),
        (gs.text(lang, "decl.gaps"), ", ".join(gap_items) or dash),
        (gs.text(lang, "decl.stability"), code(f.stability_case_id) if f.stability_case_id else dash),
        (gs.text(lang, "decl.order"), " → ".join(code(c) for c in f.golden_ids) or dash),
        ("labs.teaching", declared),
    ]
    warning_lines = sorted({f"- {code(w.code)}" + (f" [{code(w.path)}]" if w.path else "") + f": {md_inline(w.message)}"
                            for w in ctx.warnings if w.severity == "warning"})

    fixable_labels = ", ".join(code(c) for c in teach.case_ids("prompt_fixable")) or dash
    gap_labels = ", ".join(code(c) for c in teach.case_ids("retrieval_gap")) or dash
    gap_outcome = gs.text(lang, "gap_outcome.absent" if teach.case_ids("retrieval_gap", "absent") else "gap_outcome.buried")
    if teach.buried_advisory:  # sign off on the absent gaps; the buried ones are named as advisory
        gap_labels = ", ".join(code(c) for c in teach.case_ids("retrieval_gap", "absent"))
        gap_outcome = str(gs.text(lang, "gap_outcome.mixed")).format(buried=", ".join(code(c) for c in teach.case_ids("retrieval_gap", "buried")))
    notes = narrative.get("facilitatorNotes") if isinstance(narrative.get("facilitatorNotes"), dict) else {}
    checklist = "\n".join("- [ ] " + str(line).format(
        version=ctx.release_version or f"{data['id']}-<version>", n=f.probe_count, fixable=fixable_labels, gaps=gap_labels,
        bar=fmt_num(t["minImprovement"]), sp2=fmt_num(t["retrievalFailedSp2"]), sqc=fmt_num(t["retrievalFailedSqc"]),
        gap_outcome=gap_outcome, band_max=fmt_num(t["maxUsableBand"])) for line in gs.text(lang, "checklist"))  # type: ignore[union-attr]
    if isinstance(notes.get("prerequisites"), str) and notes["prerequisites"].strip():  # no run-sheet row: before class
        checklist += "\n\n" + str(gs.text(lang, "scenario_note")).format(text=md_inline(notes["prerequisites"]))

    # -- run sheet --------------------------------------------------------------------------------
    see_values = {"kb": f.kb_name, "docs": len(data["knowledge"]["documents"]), "fn": f.lambda_name, "gw": f.gateway_name,
                  "skills": len(f.skills), "agent": f.agent_name, "actor": fc.actor_id if fc else dash, "thelma": f.thelma_evaluator,
                  "mtg": f.mtg_evaluator, "total": len(f.eval_cases), "n": f.probe_count, "recent": f.recent_n, "runs": f.stability_runs}
    run_rows = []
    for number, (sid, script, guided, timing) in enumerate(RUN_SHEET, start=1):
        guided_cell = code(guided) if guided else gs.text(lang, "run.separate" if sid == "cleanup" else "run.not_guided")
        note = str(gs.text(lang, f"run.note.{sid}"))
        if isinstance(notes.get(sid), str) and notes[sid].strip():
            note = (note + " " if note else "") + md_inline(notes[sid], table=True)
        step = str(number) if sid != "cleanup" else "—"
        if sid in ("cost-latency", "models", "judge-stability"):
            step = "—"
        run_rows.append((step, code(script), guided_cell, timing, str(gs.text(lang, f"run.see.{sid}")).format(**see_values), note or dash))

    # -- truth sheet ------------------------------------------------------------------------------
    def fact_rows(confirmed: bool) -> list[tuple[str, ...]]:
        return [(code(x["id"]), md_inline(x["statement"], table=True), crit(x.get("criticality", "blocking")),
                 md_inline(x.get("confirmedBy") or dash, table=True),
                 md_inline("; ".join(str(x[k]) for k in ("confirmationRef", "source") if x.get(k)) or dash, table=True), origin(x))
                for x in data.get("facts", []) if is_customer_confirmed(x) == confirmed]

    texts = teaching.document_texts(data, ctx.root) if ctx.root is not None else None
    teaching_docs = set(teaching.teaching_document_ids(data, texts=texts)) if texts is not None else set(teaching.teaching_document_ids(data))
    answers: dict[str, list[str]] = {}
    bait_docs: set[str] = set()
    if texts is not None:
        # Answer documents are meaningful where the answer is in the knowledge base (prompt_fixable and
        # buried gaps); an absent gap's mustMention names the hand-off, not an answer.
        for cid in teach.case_ids("prompt_fixable") + teach.case_ids("retrieval_gap", "buried"):
            try:
                found = teaching.answer_documents(data, cid, texts=texts)
            except (KeyError, ValueError):
                continue
            for did in found.answer:
                answers.setdefault(did, []).append(cid)
            bait_docs.update(found.bait)
    noise_ids = {d for p in teach.of_kind("noise_grounding") for d in p.document_ids}
    doc_rows = []
    for d in data["knowledge"]["documents"]:
        roles = []
        if d["id"] in noise_ids:
            roles.append(str(gs.text(lang, "doc.role.noise")))
        if d["id"] in bait_docs or (d["id"] in teaching_docs and d["id"] not in noise_ids):
            roles.append(str(gs.text(lang, "doc.role.bait")))
        if answers.get(d["id"]):
            roles.append(str(gs.text(lang, "doc.role.answer")).format(cases=", ".join(code(c) for c in answers[d["id"]])))
        doc_rows.append((code(d["id"]), md_inline(d["title"], table=True), code(Path(d["file"]).name), code(d.get("provenance", "")),
                         gs.text(lang, "yes" if d.get("noise") else "no"), "; ".join(roles) or dash))
    noise_plan = (data.get("knowledge") or {}).get("noisePlan") or {}
    noise_plan_text = "\n\n".join(md_para(noise_plan[k]) for k in ("rationale", "description") if isinstance(noise_plan.get(k), str) and noise_plan[k].strip())

    tool_rows = []
    for tool in data["tools"]:
        if tool.get("kind") == "retrieval":
            behavior = str(gs.text(lang, "tool.retrieval"))
        else:
            fx = tool.get("fixtures") or {}
            parts = [f"{gs.text(lang, 'tool.default')}: `{md_inline(_compact(fx.get('default')), table=True)}`"]
            for case in fx.get("cases") or []:
                parts.append(f"{gs.text(lang, 'tool.when')} `{md_inline(_compact(case.get('when'), None), table=True)}` → `{md_inline(_compact(case.get('return')), table=True)}`")
            for err in fx.get("errors") or []:
                parts.append(f"{gs.text(lang, 'tool.when')} `{md_inline(_compact(err.get('when'), None), table=True)}` → {gs.text(lang, 'tool.error')}: {md_inline(err.get('error'), table=True)}")
            behavior = "<br>".join(parts)
        tool_rows.append((code(tool["name"]), code(tool.get("kind", "")), code(tool.get("provenance", "")), behavior))

    block = teaching.block(data) or {}
    defects = [d for d in block.get("baselineDefects") or [] if isinstance(d, dict)]
    defects_table = (str(gs.text(lang, "defects.intro")) + "\n\n" + table(gs.text(lang, "h.defects"), [  # type: ignore[arg-type]
        (code(d.get("id")), md_inline(d.get("description", ""), table=True), md_inline(d.get("candidateFix", ""), table=True)) for d in defects])) if defects else ""
    prompt_diff = ""
    prompts = data.get("prompts") or {}
    if ctx.root is not None and prompts.get("baselineFile") and prompts.get("optimizationCandidateFile"):
        from .scenario import resolve_inside

        base = _pack_text(resolve_inside(ctx.root, prompts["baselineFile"])).splitlines()
        cand = _pack_text(resolve_inside(ctx.root, prompts["optimizationCandidateFile"])).splitlines()
        diff = list(difflib.unified_diff(base, cand, "baseline.md", "optimization-candidate.md", n=1, lineterm=""))
        prompt_diff = str(gs.text(lang, "prompt.diff")) + "\n\n```diff\n" + "\n".join(line.replace("```", "'''") for line in diff) + "\n```"
    rationale = narrative.get("designRationale")
    design_rationale = (str(gs.text(lang, "design_rationale")) + "\n\n" + md_para(rationale)) if isinstance(rationale, str) and rationale.strip() else ""

    # -- contrasts -------------------------------------------------------------------------------
    memory = []
    if fc is not None:
        memory += [f"{gs.text(lang, 'decl.first')}{gs.text(lang, 'colon')}{md_inline(fc.label or dash)} — {code(fc.actor_id)}", "", quote_block(fc.query), "",
                   text_block([script_facts.first_conversation_topic(fc), "…", script_facts.memory_notice(fc)])]
        if fc.teaching_point:
            memory += ["", md_para(fc.teaching_point)]
        if fc.must_not_mention:
            memory += ["", str(gs.text(lang, "memory.check")).format(terms=", ".join(code(x) for x in fc.must_not_mention))]
    if isinstance(narrative.get("memoryLesson"), str):
        memory += ["", md_para(narrative["memoryLesson"])]

    def case_ref(cid: str) -> str:
        return f"**{md_inline(by_id.get(cid, {}).get('label', cid))}** ({code(cid)})"

    raw = {p.get("id"): p for p in teaching.phenomena(data)}
    contrast = []
    for p in teach.of_kind("prompt_fixable") + teach.of_kind("retrieval_gap"):
        contrast.append(f"**{_kind_label(lang, p)}** — {', '.join(case_ref(c) for c in p.case_ids)}")
        contrast.append("")
        contrast.append(_expect_text(lang, p, buried_advisory=teach.buried_advisory))
        if p.teaching_point:
            contrast += ["", md_para(p.teaching_point)]
        rp = raw.get(p.id) or {}
        if p.kind == "retrieval_gap":
            bait = ", ".join(code(x) for x in rp.get("baitTerms") or []) or dash
            if rp.get("absentTerms"):
                contrast += ["", str(gs.text(lang, "gap.terms")).format(terms=", ".join(code(x) for x in rp["absentTerms"]), bait=bait)]
            else:
                contrast += ["", str(gs.text(lang, "gap.bait")).format(bait=bait)]
            if texts is not None:
                for cid in p.case_ids:
                    found = teaching.answer_documents(data, cid, texts=texts)
                    answer_docs = ", ".join(code(d) for d in found.answer) if p.mechanism == "buried" else ""
                    contrast += ["", str(gs.text(lang, "gap.docs")).format(answer=answer_docs or dash,
                                                                              bait=", ".join(code(d) for d in found.bait) or dash)]
        if rp.get("defectIds"):
            contrast += ["", f"{gs.text(lang, 'defects.intro')} " + ", ".join(code(d) for d in rp["defectIds"])]
        contrast.append("")
    contrast.append(text_block(_closing_lines(data, f)))
    if isinstance(narrative.get("retrievalContrast"), str):
        contrast += ["", md_para(narrative["retrievalContrast"])]

    l1_lines = []
    for p in teach.phenomena:
        if p.kind not in FOCUS_CHECKS:
            continue
        checks = [c for cid in p.case_ids for c in FOCUS_CHECKS[p.kind] if (by_id[cid].get("expected") or {}).get(c) not in (None, False, [])]
        l1_lines += [f"**{_kind_label(lang, p)}** — {', '.join(case_ref(c) for c in p.case_ids)}", "",
                     _expect_text(lang, p), "", str(gs.text(lang, "l1.kind_note")).format(design=code(p.design), checks=", ".join(code(c) for c in dict.fromkeys(checks)) or dash)]
        if p.teaching_point:
            l1_lines += ["", md_para(p.teaching_point)]
        l1_lines.append("")
    doc_titles = {d["id"]: d["title"] for d in data["knowledge"]["documents"]}
    noise_lines = []
    for p in teach.of_kind("noise_grounding"):
        noise_lines += [f"**{_kind_label(lang, p)}** — " + ", ".join(f"{md_inline(doc_titles.get(d, d))} ({code(d)})" for d in p.document_ids), "",
                        _expect_text(lang, p)]
        if p.teaching_point:
            noise_lines += ["", md_para(p.teaching_point)]
        noise_lines.append("")

    # -- answer key -------------------------------------------------------------------------------
    stability = f.stability_case_id
    probe_ids = set(f.probe_case_ids)
    index_of = {c.case_id: c.index for c in f.eval_cases}

    def answer(case: Mapping[str, Any]) -> str:
        cid = case["id"]
        roles = [_kind_label(lang, p) for p in teach.phenomena if cid in p.case_ids]
        if cid in probe_ids:
            roles.append(str(gs.text(lang, "answer.probe")))
        if cid == stability:
            roles.append(str(gs.text(lang, "answer.stability")))
        tools = [f"tool:{x}" for x in (case.get("expected") or {}).get("requiredTools") or []]
        tools += [f"forbidden:{x}" for x in (case.get("expected") or {}).get("forbiddenTools") or []]
        role_id = case.get("roleId") or dash
        if case.get("set") == "practice":
            asked = str(gs.text(lang, "answer.persona")).format(persona=case.get("actorId"), index=index_of.get(cid, 0) + 1, role=role_id)
            evaluated = str(gs.text(lang, "scored.probe" if cid in probe_ids else "scored.other"))
        else:
            asked = str(gs.text(lang, "answer.declared_actor")).format(actor=case.get("actorId"), role=role_id)
            evaluated = str(gs.text(lang, "answer.holdout_eval"))
        basis = "<br>".join(
            f"{code(b)} — {md_inline((facts_by_id.get(b) or {}).get('statement', ''), table=True)} {_tag(lang, _tag_key(facts_by_id.get(b) or {'provenance': ''}))}"
            for b in case.get("basis") or []) or dash
        category = gs.text(lang, f"category.{case.get('category')}") if case.get("category") in ("normal", "boundary", "prohibited") else case.get("category")
        head = f"#### {md_inline(case.get('label', cid))} — {code(cid)} ({case.get('set')}, {category}, {_tag(lang, _tag_key(case))})"
        rows = [
            (gs.text(lang, "answer.question"), md_inline(case.get("query", ""), table=True)),
            (gs.text(lang, "answer.asked"), asked),
            (gs.text(lang, "answer.expected"), _expected_text(case.get("expected") or {})),
            (gs.text(lang, "answer.basis"), basis),
            (gs.text(lang, "answer.roles"), " · ".join(x for x in (", ".join(md_inline(r, table=True) for r in roles), ", ".join(code(x) for x in tools)) if x) or dash),
            (gs.text(lang, "answer.evaluated"), evaluated),
        ]
        return head + "\n\n" + table(gs.text(lang, "h.answer"), rows)  # type: ignore[arg-type]

    experiments = [str(gs.text(lang, "experiments.fix"))]
    for e in narrative.get("experiments") or []:
        if isinstance(e, dict) and e.get("audience") == "instructor":
            experiments.append(f"**{md_inline(e.get('title', ''))}** — {md_inline(e.get('body', ''))}")
    observations = [f"- {md_inline(o)}" for o in (data.get("labs") or {}).get("observations") or [] if isinstance(o, str)]
    supplement = _supplement(ctx.root, (data.get("labs") or {}).get("instructorGuideFile"))

    values = {
        "marker": f"<!-- {INSTRUCTOR_MARKER_TOKEN} pack={data['id']} -->",
        "instructor_banner": str(gs.text(lang, "banner.instructor")),
        "draft_banner": f"{DRAFT_MARKER}\n{gs.text(lang, 'banner.draft')}" if draft else "",
        "title": md_inline(data.get("displayName", data["id"])),
        "meta_table": table(gs.text(lang, "h.meta"), [  # type: ignore[arg-type]
            (gs.text(lang, "meta.pack"), code(data["id"])), (gs.text(lang, "meta.kind"), code(data.get("packKind", ""))),
            (gs.text(lang, "meta.language"), code(data.get("language", ""))), (gs.text(lang, "meta.template"), code(template_commit)),
            (gs.text(lang, "meta.generator"), code(generator_version)), (gs.text(lang, "meta.scenario"), code(ctx.scenario_sha256)),
        ]),
        "provenance_matrix": table(gs.text(lang, "h.matrix"), matrix_rows),  # type: ignore[arg-type]
        "waived_line": str(gs.text(lang, "waived")).format(items=", ".join(f"{x.item_type} {code(x.item_id)}" for x in waived)) if waived else "",
        "provenance_banner": _banner(lang, pack_kind=str(data.get("packKind", "")), customer_name=customer.get("name"),
                                     classification=classification, any_confirmed=any_confirmed),
        "anchors_table": table(gs.text(lang, "h.anchors"), anchor_rows),  # type: ignore[arg-type]
        "anchors_note": str(gs.text(lang, "anchors.note")),
        "classification_line": " ".join(x for x in (
            str(gs.text(lang, "customer.line")).format(name=md_inline(customer.get("name") or dash),
                                                       approval=md_inline(customer.get("materialApproval") or dash)) if customer else "",
            str(gs.text(lang, "classification.line")).format(value=code(classification) if classification else gs.text(lang, "classification.unset")),
        ) if x),
        "declarations_table": table(gs.text(lang, "h.declarations"), decl_rows),  # type: ignore[arg-type]
        "warnings_list": "\n".join(warning_lines) or str(gs.text(lang, "warnings.none")),
        "checklist": checklist,
        "runsheet_table": table(gs.text(lang, "h.runsheet"), run_rows),  # type: ignore[arg-type]
        "truth_customer_facts": table(gs.text(lang, "h.truth_facts"), fact_rows(True)) if fact_rows(True) else str(gs.text(lang, "none_facts")),  # type: ignore[arg-type]
        "truth_synthetic_facts": table(gs.text(lang, "h.truth_facts"), fact_rows(False)) if fact_rows(False) else str(gs.text(lang, "none_synthetic")),  # type: ignore[arg-type]
        "truth_documents": table(gs.text(lang, "h.truth_docs"), doc_rows),  # type: ignore[arg-type]
        "noise_plan_text": noise_plan_text,
        "truth_tools": table(gs.text(lang, "h.truth_tools"), tool_rows),  # type: ignore[arg-type]
        "defects_table": defects_table,
        "prompt_diff": prompt_diff,
        "design_rationale": design_rationale,
        "memory_contrast": "\n".join(memory) or dash,
        "retrieval_contrast": "\n".join(contrast),
        "probe_count": str(f.probe_count),
        "l1_contrast": "\n".join(l1_lines) or dash,
        "noise_contrast": "\n".join(noise_lines) or dash,
        "stability_case": case_ref(stability) if stability else dash,
        "stability_runs": str(f.stability_runs),
        "band_max": fmt_num(t["maxUsableBand"]),
        "answer_practice": "\n\n".join(answer(c) for c in practice_order) or dash,
        "answer_holdout": "\n\n".join(answer(c) for c in holdout) or dash,
        "holdout_use": str(gs.text(lang, "holdout.use")).format(agent=f.agent_name),
        "report_fields": table(gs.text(lang, "h.report"), gs.text(lang, "report.fields")),  # type: ignore[arg-type]
        "facilitation_table": table(gs.text(lang, "h.facilitation"), [  # type: ignore[arg-type]
            # PF_RETRIEVAL_FAILED reads the retrieval-ok bar ({sqc}); RG_RETRIEVAL_OK_* the retrieval-failed one ({sqc_gap}).
            (code(c), str(w).format(gr=fmt_num(t["grPass"]), bar=fmt_num(t["minImprovement"]), sqc=fmt_num(t["retrievalOkSqc"]),
                                    sqc_gap=fmt_num(t["retrievalFailedSqc"]), sp2=fmt_num(t["retrievalFailedSp2"]),
                                    band_max=fmt_num(t["maxUsableBand"])), s, a)
            for c, w, s, a in gs.text(lang, "facilitation")]),  # type: ignore[misc]
        "troubleshooting_table": table(gs.text(lang, "h.troubleshoot"), gs.text(lang, "troubleshooting")),  # type: ignore[arg-type]
        "instructor_experiments": "\n\n".join(experiments),
        "rehearsal_block": rehearsal_block(ctx.rehearsal, lang, release_version=ctx.release_version),
        # designs[1] H §8: the generic HR release is plan B (README: hr-default is the upstream-compatible fallback).
        "fallback_line": "" if data["id"] == FALLBACK_PACK_ID else str(gs.text(lang, "fallback.generic")),
        "agent_name": f.agent_name,
        "gateway_name": f.gateway_name,
        "lambda_name": f.lambda_name,
        "kb_name": f.kb_name,
        "ssm_prefix": f.ssm_prefix,
        "names_table": _names_table(lang, f),
        "retrieval_span": f.retrieval_span,
        "observations": "\n".join(observations) or str(gs.text(lang, "observations.none")),
        "supplement": demote_headings(supplement) if supplement else str(gs.text(lang, "supplement.none")),
    }
    values.update(_shared_values(lang))
    return values


def build_instructor_guide(ctx: InstructorContext, *, template_commit: str, generator_version: str, draft: bool = False) -> str:
    lang = gs.lang_key(ctx.data.get("language"))
    template = load_template("instructor", lang)
    values = _instructor_values(ctx, lang=lang, template_commit=template_commit, generator_version=generator_version, draft=draft)
    values.update(navigation(template, {}))
    return normalize_markdown(render_template(template, values))


# ---------------------------------------------------------------------------
# Rehearsal evidence (an optional dict; P2's run/rehearsal.json or any compatible record)
# ---------------------------------------------------------------------------


def _clip(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else _compact(value, None)
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _items(value: Any) -> list[Any]:
    """A JSON array as a list; any other value (a scalar, an object, null) is no items."""
    return list(value) if isinstance(value, (list, tuple)) else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _said(item: Mapping[str, Any], key: str, lang: str) -> str | None:
    """A human-readable field of a rehearsal record in the guide language.

    rehearsal.py writes each text in the pack language (``key``) with an English twin (``key`` + ``En``):
    an English guide reads the twin, a Chinese guide the pack-language text. An older record without the
    twin falls back to ``key``. Anything but a non-blank string is no text.
    """
    for name in ((key + "En", key) if lang == "en" else (key,)):
        value = item.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _case_evidence(case: Mapping[str, Any], lang: str) -> str:
    code_value = case.get("reasonCode") or case.get("code") or case.get("verdict") or case.get("status")
    reason = _said(case, "reason", lang)
    if reason is None:
        return str(gs.text(lang, "rehearsal.case")).format(case=case.get("caseId"), code=code_value)
    return str(gs.text(lang, "rehearsal.case_reason")).format(case=case.get("caseId"), code=code_value, reason=reason)


def render_rehearsal_evidence(rehearsal: Mapping[str, Any] | None, lang: str, *, release_version: str | None = None) -> str:
    """Markdown for a rehearsal record (P2's ``run/rehearsal.json`` or any compatible record).

    Read: ``verdict``, ``reasonCode``/``reason``, ``readyForClass`` (or ``readyToReplaceHR``),
    ``readiness{blockers, blockerTexts, reportComplete, guidesBuilt}``, ``inputs{noiseBand{value, source,
    stabilityVerdict} | number, missingEvidence}``, ``releaseVersion``, ``generatedAt``,
    ``phenomena[{id, kind, verdict|status, evidence | reasonCode/reason/cases[]}]``, ``warnings[]``,
    ``remediation[]`` and ``scopeWarning``, so a not-ready record says why. The texts are read in the guide
    language (:func:`_said`: the English twins ``<field>En`` for an English guide). Rendered defensively:
    unknown keys are ignored, a value of the wrong type is skipped, every value is escaped and clipped, a
    missing or malformed record renders the "no evidence yet" note, and a record bound to another release
    says so.
    """
    lang = gs.lang_key(lang)
    if not isinstance(rehearsal, Mapping):
        return str(gs.text(lang, "rehearsal.none"))
    yes_no = {True: gs.text(lang, "yes"), False: gs.text(lang, "no")}
    sep = str(gs.text(lang, "rehearsal.list_sep"))  # the guide language's list punctuation (、 in Chinese)
    lines: list[str] = []
    verdict = rehearsal.get("verdict")
    lines.append(str(gs.text(lang, "rehearsal.verdict")).format(verdict=md_inline(_clip(verdict if verdict is not None else "—", 60))))
    reason_code = code(_clip(rehearsal["reasonCode"], 80)) if isinstance(rehearsal.get("reasonCode"), str) and rehearsal["reasonCode"] else None
    said = _said(rehearsal, "reason", lang)
    said = md_inline(_clip(said)) if said is not None else None
    if reason_code and said:
        lines.append(str(gs.text(lang, "rehearsal.reason")).format(reason=str(gs.text(lang, "rehearsal.code_reason")).format(code=reason_code, reason=said)))
    elif reason_code or said:
        lines.append(str(gs.text(lang, "rehearsal.reason")).format(reason=reason_code or said))
    ready = rehearsal.get("readyForClass", rehearsal.get("readyToReplaceHR"))
    if isinstance(ready, bool):
        lines.append(str(gs.text(lang, "rehearsal.ready")).format(ready=yes_no[ready]))
    readiness = _mapping(rehearsal.get("readiness"))
    meaning = {str(t.get("code")): _said(t, "text", lang) for t in _items(readiness.get("blockerTexts")) if isinstance(t, Mapping)}
    blockers = [str(gs.text(lang, "rehearsal.blocker")).format(code=code(_clip(b, 80)), text=md_inline(_clip(meaning[b], 120)))
                if meaning.get(b) else code(_clip(b, 80))
                for b in _items(readiness.get("blockers")) if isinstance(b, str) and b][:20]
    if blockers:
        lines.append(str(gs.text(lang, "rehearsal.blockers")).format(blockers=sep.join(blockers)))
    for key, text_key in (("reportComplete", "rehearsal.report_complete"), ("guidesBuilt", "rehearsal.guides_built")):
        if isinstance(readiness.get(key), bool):
            lines.append(str(gs.text(lang, text_key)).format(value=yes_no[readiness[key]]))
    inputs = _mapping(rehearsal.get("inputs"))
    band = inputs.get("noiseBand")
    band_value, band_details = (band.get("value"), band) if isinstance(band, Mapping) else (band, {})
    if _is_number(band_value) or (isinstance(band_value, str) and band_value):
        shown = fmt_num(band_value) if _is_number(band_value) else md_inline(_clip(band_value, 40))
        details = []
        if isinstance(band_details.get("source"), str) and band_details["source"]:
            details.append(str(gs.text(lang, "rehearsal.noise_source")).format(source=code(_clip(band_details["source"], 80))))
        if isinstance(band_details.get("stabilityVerdict"), str) and band_details["stabilityVerdict"]:
            details.append(str(gs.text(lang, "rehearsal.noise_verdict")).format(verdict=code(_clip(band_details["stabilityVerdict"], 40))))
        if details:
            shown = str(gs.text(lang, "rehearsal.noise_details")).format(value=shown, details=sep.join(details))
        lines.append(str(gs.text(lang, "rehearsal.noise_band")).format(value=shown))
    missing = [code(_clip(m, 80)) for m in _items(inputs.get("missingEvidence")) if isinstance(m, str) and m][:20]
    if missing:
        lines.append(str(gs.text(lang, "rehearsal.missing")).format(items=sep.join(missing)))
    recorded = rehearsal.get("releaseVersion")
    if isinstance(recorded, str) and recorded:
        lines.append(str(gs.text(lang, "rehearsal.release")).format(release=md_inline(_clip(recorded, 80)).replace("`", "")))
    if rehearsal.get("generatedAt") is not None:
        lines.append(str(gs.text(lang, "rehearsal.generated")).format(at=md_inline(_clip(rehearsal.get("generatedAt"), 60))))
    out = ["\n".join(f"- {line}" for line in lines)]
    if release_version and isinstance(recorded, str) and recorded and recorded != release_version:
        out.append("> [!WARNING]\n> " + str(gs.text(lang, "rehearsal.stale")).format(release=md_inline(_clip(recorded, 80)).replace("`", "")))
    rows = []
    for p in _items(rehearsal.get("phenomena")):
        if not isinstance(p, Mapping):
            continue
        evidence = p.get("evidence")
        if evidence is None:
            parts = [str(p["reasonCode"])] if isinstance(p.get("reasonCode"), str) else []
            parts += [text for text in [_said(p, "reason", lang)] if text is not None]
            cases = [_case_evidence(c, lang) for c in _items(p.get("cases")) if isinstance(c, Mapping)]
            evidence = str(gs.text(lang, "rehearsal.evidence_sep")).join(parts + cases) or "—"
        rows.append((md_inline(_clip(p.get("id", "—"), 80), table=True), md_inline(_clip(p.get("kind", "—"), 40), table=True),
                     md_inline(_clip(p.get("verdict", p.get("status", "—")), 40), table=True), md_inline(_clip(evidence, 600), table=True)))
    out.append(table(gs.text(lang, "h.rehearsal"), rows) if rows else str(gs.text(lang, "rehearsal.empty")))  # type: ignore[arg-type]
    warnings = [line for w in _items(rehearsal.get("warnings")) if isinstance(w, Mapping) for line in [_warning_line(w, lang)] if line]
    if warnings:
        out.append(str(gs.text(lang, "rehearsal.warnings")) + "\n\n" + "\n".join(f"- {w}" for w in warnings[:20]))
    hints = [h for h in _items(rehearsal.get("remediation")) if isinstance(h, Mapping)]
    if hints:
        out.append(str(gs.text(lang, "rehearsal.remediation")) + "\n\n" + "\n".join(f"- {_hint_line(h, lang)}" for h in hints[:20]))
    scope = _said(rehearsal, "scopeWarning", lang)
    if scope is not None:
        out.append("> " + md_inline(_clip(scope, 400)))
    return "\n\n".join(out)


def rehearsal_evidence_file(rehearsal: Mapping[str, Any] | None, language: Any, *, release_version: str, pack_id: str) -> str:
    """``rehearsal-evidence.md`` of the instructor bundle: the instructor-only marker, a heading naming the
    release and :func:`render_rehearsal_evidence`, all in the guide language of ``language`` (the build
    snapshot's language: a zh-CN pack gets a Chinese file, every other pack an English one)."""
    lang = gs.lang_key(language)
    title = str(gs.text(lang, "rehearsal.bundle_title")).format(version=release_version)
    body = render_rehearsal_evidence(rehearsal, lang, release_version=release_version)
    return f"<!-- {INSTRUCTOR_MARKER_TOKEN} pack={pack_id} -->\n# {title}\n\n{body}\n"


def _warning_line(warning: Mapping[str, Any], lang: str) -> str | None:
    """One rehearsal warning in the guide language, led by its refs (the phenomenon and case, or the runs) as
    code spans like a hint's asset and case, except the refs the message already names; None without a message."""
    message = _said(warning, "message", lang)
    if message is None:
        return None
    text = md_inline(_clip(message, 300))
    refs = [code(_clip(r, 80)) for r in _items(warning.get("refs")) if isinstance(r, str) and r and r not in message][:10]
    return str(gs.text(lang, "rehearsal.warning_refs")).format(text=text, refs=" · ".join(refs)) if refs else text


def _hint_line(hint: Mapping[str, Any], lang: str) -> str:
    """One remediation hint in the guide language: severity, the asset it edits, the action and why."""
    action = md_inline(_clip(_said(hint, "action", lang) or hint.get("code") or "—", 600))
    because = _said(hint, "because", lang)
    if because:
        end = str(gs.text(lang, "rehearsal.sentence_end"))
        if end and not action.endswith(("。", "！", "？", ".", "!", "?")):
            action += end
        text = str(gs.text(lang, "rehearsal.hint")).format(action=action, because=md_inline(_clip(because, 300)))
    else:
        text = action
    asset = _mapping(hint.get("asset"))
    # The scenario path is the precise place inside scenario.yaml; any other file is named as it is.
    where = next((v for v in ((asset.get("file") if asset.get("file") != "scenario.yaml" else None), asset.get("scenarioPath"),
                              asset.get("file")) if isinstance(v, str) and v), None)
    located = [code(_clip(v, 120)) for v in (where, hint.get("caseId")) if isinstance(v, str) and v]
    if located:
        text = str(gs.text(lang, "rehearsal.hint_where")).format(where=" · ".join(located), text=text)
    severity = hint.get("severity")
    if severity in ("blocking", "advisory"):
        text = str(gs.text(lang, "rehearsal.hint_tagged")).format(severity=gs.text(lang, f"rehearsal.severity.{severity}"), text=text)
    return text


def rehearsal_block(rehearsal: Mapping[str, Any] | None, lang: str, *, release_version: str | None = None) -> str:
    return f"{REHEARSAL_BEGIN}\n{render_rehearsal_evidence(rehearsal, lang, release_version=release_version)}\n{REHEARSAL_END}"


def attach_rehearsal(instructor_markdown: str, rehearsal: Mapping[str, Any] | None, lang: str, *, release_version: str | None = None) -> str:
    """The built instructor guide with its rehearsal section replaced by ``rehearsal`` (markers kept)."""
    start, end = instructor_markdown.find(REHEARSAL_BEGIN), instructor_markdown.find(REHEARSAL_END)
    if start < 0 or end < start:
        raise GuideError("the instructor guide has no rehearsal-evidence section")
    block = rehearsal_block(rehearsal, lang, release_version=release_version)
    return normalize_markdown(instructor_markdown[:start] + block + instructor_markdown[end + len(REHEARSAL_END):])


# ---------------------------------------------------------------------------
# Building both guides
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Guides:
    language: str
    student: str
    instructor: str
    student_sources: tuple[str, ...]
    instructor_sources: tuple[str, ...]
    findings: tuple[Finding, ...] = field(default_factory=tuple)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def build_guides(
    scenario: Any,
    *,
    template_commit: str,
    generator_version: str,
    warnings: Iterable[Finding] = (),
    rehearsal: Mapping[str, Any] | None = None,
    release_version: str | None = None,
    draft: bool = False,
) -> Guides:
    """Both guides for a loaded :class:`Scenario` (``path``, ``root``, ``data``). Pure apart from reading pack files."""
    data = scenario.data
    root = Path(scenario.root) if getattr(scenario, "root", None) is not None else None
    path = Path(scenario.path) if getattr(scenario, "path", None) is not None else None
    facts = script_facts.compute(data)
    teach = teaching_view(data)
    lang = gs.lang_key(data.get("language"))
    findings: list[Finding] = []
    view = TeachingView(declared=teach.declared, phenomena=teach.phenomena)
    if not teach.declared or not view.case_ids("prompt_fixable") or not view.case_ids("retrieval_gap"):
        findings.append(Finding("warning", "guide.contrast_undeclared",
                                "labs.teaching declares no prompt_fixable + retrieval_gap contrast; the guide uses the generic wording",
                                path="labs.teaching", scopes=("labs",)))
    student = build_student_guide(student_view(data, facts, teach, root=root), template_commit=template_commit,
                                  generator_version=generator_version, draft=draft)
    ctx = InstructorContext(data=data, facts=facts, teach=teach, root=root, scenario_sha256=_file_sha256(path) if path else "",
                            warnings=tuple(warnings) + tuple(findings), rehearsal=rehearsal, release_version=release_version)
    instructor = build_instructor_guide(ctx, template_commit=template_commit, generator_version=generator_version, draft=draft)
    labs = data.get("labs") or {}
    student_sources = ["id", "displayName", "language", "packKind", "customer", "agent", "facts", "knowledge.documents", "knowledge.noisePlan",
                       "tools", "skills", "namespace", "evaluation.retrievalToolName", "evaluation.judgeModel",
                       "evaluation.goldenSet[set=practice]", "labs.teaching", "labs.guide", f"<template:guide/student.{lang}.md>"]
    if "studentGuideFile" in labs:
        student_sources.append("labs.studentGuideFile")
    instructor_sources = ["<scenario file>", "evaluation.goldenSet", "governance.exceptions", "prompts.baselineFile",
                          "prompts.optimizationCandidateFile", "labs.observations", f"<template:guide/instructor.{lang}.md>"] + [
        s for s in student_sources if not s.startswith("<template")]
    if "instructorGuideFile" in labs:
        instructor_sources.append("labs.instructorGuideFile")
    return Guides(language=lang, student=student, instructor=instructor, student_sources=tuple(sorted(set(student_sources))),
                  instructor_sources=tuple(sorted(set(instructor_sources))), findings=tuple(findings))


def pack_sources_text(scenario: Any) -> str:
    """The pack's own text: the scenario file plus every file it references (never a generated guide)."""
    data, root = scenario.data, Path(scenario.root)
    parts: list[str] = []
    path = Path(scenario.path)
    if path.is_file():
        parts.append(path.read_text(encoding="utf-8", errors="replace"))
    from .scenario import resolve_inside

    rels = [d["file"] for d in data["knowledge"]["documents"]] + [s["file"] for s in data.get("skills", [])]
    rels += [v for k, v in (data.get("prompts") or {}).items() if isinstance(v, str)]
    rels += [v for k, v in (data.get("labs") or {}).items() if k in ("studentGuideFile", "instructorGuideFile") and isinstance(v, str)]
    for rel in rels:
        target = resolve_inside(root, rel)
        if target.is_file():
            parts.append(target.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _holdout(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in data.get("evaluation", {}).get("goldenSet", []) if isinstance(c, dict) and c.get("set") == "holdout"]


def holdout_leaks(data: dict[str, Any], text: str) -> list[tuple[int, str]]:
    """(line, what) for every holdout query, holdout label of ≥ 12 characters or holdout id in ``text``.

    ``text`` is matched as a reader sees it too (:func:`validator.reader_views`): the guide escapes
    ``<``, ``>``, ``|`` and backticks, so a holdout query carrying one of them is still found."""
    hits: list[tuple[int, str]] = []
    lines = text.splitlines() or [text]
    views_all = reader_views("", text)
    for case in _holdout(data):
        cid = str(case.get("id", ""))
        query = _normalize(str(case.get("query", "")))
        label = _normalize(str(case.get("label", "")))
        needles = []
        if query and any(query in view for view in views_all):
            needles.append((query, f"holdout case '{cid}' query"))
        if len(label) >= 12 and any(label in view for view in views_all):
            needles.append((label, f"holdout case '{cid}' label"))
        for needle, what in needles:
            line = next((i for i, raw in enumerate(lines, 1) if reader_contains(raw, needle)), None)
            hits.append((line or 0, what))
        if cid:
            pattern = re.compile(rf"(?<![a-z0-9-]){re.escape(cid)}(?![a-z0-9-])")
            for i, raw in enumerate(lines, 1):
                if pattern.search(raw):
                    hits.append((i, f"holdout case id '{cid}'"))
                    break
    return hits


def _narrative_strings(block: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """(path, audience, text) of every narrative string of labs.guide."""
    out: list[tuple[str, str, str]] = []
    for key in ("tagline", "scenarioIntro", "memoryLesson", "retrievalContrast", "attribution"):
        if isinstance(block.get(key), str):
            out.append((f"labs.guide.{key}", "student", block[key]))
    for key, audience in (("stepNotes", "student"), ("facilitatorNotes", "instructor")):
        notes = block.get(key)
        if isinstance(notes, dict):
            for sid in sorted(notes):
                if isinstance(notes[sid], str):
                    out.append((f"labs.guide.{key}.{sid}", audience, notes[sid]))
    if isinstance(block.get("designRationale"), str):
        out.append(("labs.guide.designRationale", "instructor", block["designRationale"]))
    for i, e in enumerate(block.get("experiments") or []):
        if isinstance(e, dict):
            audience = "instructor" if e.get("audience") == "instructor" else "student"
            for key in ("title", "body"):
                if isinstance(e.get(key), str):
                    out.append((f"labs.guide.experiments[{i}].{key}", audience, e[key]))
    return out


def _err(code_: str, message: str, path: str, line: int | None = None) -> Finding:
    return Finding("error", code_, message, path=path, line=line, scopes=("guides",))


def check_guide_narrative(data: dict[str, Any], root: Path | None = None) -> ValidationReport:
    """The ``guide`` policy sub-check: labs.guide narrative and guide supplements (designs[1] §K).

    * no code fence or command line (``guide.narrative_command``), no heading in narrative strings
      (``guide.narrative_heading``; supplements are demoted instead), no HTML (``guide.narrative_html``);
    * links only in ``attribution`` and instructor fields (``guide.narrative_link``);
    * no template identifier the pack replaced (``guide.narrative_identifier``);
    * student fields and the student supplement never name the holdout set or a holdout case
      (``guide.holdout_leak``) and never carry the instructor marker (``guide.instructor_marker_leak``);
    * a supplement file is UTF-8 text (``guide.supplement_encoding``).

    The instructor supplement gets the HTML check and a no-bash-fence check (the instructor guide
    carries no command blocks).
    """
    from .teaching_policy import HOLDOUT_WORDS

    report = ValidationReport()
    labs = data.get("labs") or {}
    block = labs.get("guide") if isinstance(labs.get("guide"), dict) else {}
    ns = data.get("namespace") or {}
    tokens = residue_tokens(ns, (data.get("evaluation") or {}).get("retrievalToolName", "")) if ns else []

    def scan(path: str, audience: str, text: str, *, supplement: bool) -> None:
        lines = text.splitlines()
        if not supplement or audience == "student":
            if "```" in text or "~~~" in text or any(_NARRATIVE_COMMAND.match(l) for l in lines):
                report.findings.append(_err("guide.narrative_command", f"{path}: narrative is prose only — no code fences or command lines", path))
        elif re.search(r"(?m)^\s*```\s*(bash|sh|shell)\b", text):
            report.findings.append(_err("guide.narrative_command", f"{path}: the instructor guide carries no bash blocks; write commands as inline code", path))
        if not supplement and any(_NARRATIVE_HEADING.match(l) for l in lines):
            report.findings.append(_err("guide.narrative_heading", f"{path}: narrative fields may not contain Markdown headings", path))
        if _NARRATIVE_HTML.search(text):
            report.findings.append(_err("guide.narrative_html", f"{path}: narrative may not contain HTML", path))
        if audience == "student" and not path.endswith(".attribution") and ("http" in text or "](" in text):
            report.findings.append(_err("guide.narrative_link", f"{path}: links are allowed only in labs.guide.attribution and instructor fields", path))
        hit = next((tok for tok in tokens if tok in text), None)
        if hit:
            report.findings.append(_err("guide.narrative_identifier", f"{path}: template identifier '{hit}' is not this pack's name", path))
        if audience == "student":
            if INSTRUCTOR_MARKER_TOKEN in text:
                report.findings.append(_err("guide.instructor_marker_leak", f"{path}: the instructor marker may not appear in student text", path))
            word = HOLDOUT_WORDS.search(text)
            leaks = holdout_leaks(data, text)
            if word:
                report.findings.append(_err("guide.holdout_leak", f"{path}: student text names the holdout ('{word.group(0)}')", path))
            for _line, what in leaks:
                report.findings.append(_err("guide.holdout_leak", f"{path}: student text contains the {what}", path))

    for path, audience, text in _narrative_strings(block):
        scan(path, audience, text, supplement=False)
    if root is not None:
        for key, audience in (("studentGuideFile", "student"), ("instructorGuideFile", "instructor")):
            supplement_path = _supplement_path(Path(root), labs.get(key))
            body = _pack_text(supplement_path) if supplement_path is not None else None
            if supplement_path is not None:
                try:
                    supplement_path.read_bytes().decode("utf-8")
                except UnicodeDecodeError as exc:
                    report.findings.append(_err("guide.supplement_encoding", f"labs.{key}: the supplement is embedded in a Markdown "
                                                f"guide and must be UTF-8 text ({exc.reason} at byte {exc.start})", f"labs.{key}"))
            if body is not None:
                if audience == "instructor":
                    if _NARRATIVE_HTML.search(body):
                        report.findings.append(_err("guide.narrative_html", f"labs.{key}: the supplement may not contain HTML", f"labs.{key}"))
                    if re.search(r"(?m)^\s*```\s*(bash|sh|shell)\b", body):
                        report.findings.append(_err("guide.narrative_command", f"labs.{key}: the instructor guide carries no bash blocks", f"labs.{key}"))
                else:
                    scan(f"labs.{key}", audience, body, supplement=True)
    return report


def check_guides(data: dict[str, Any], guides: Guides, *, sources_text: str = "", root: Path | None = None) -> ValidationReport:
    """Compile-time gate on the generated guides (designs[1] §I, §J, §L).

    The guides are generated from every scenario section, so each finding carries the scopes of the
    section whose text produced it (:func:`validator.guide_source_scopes`; ``root`` is the scenario
    root, for the supplements). A problem no scenario text explains — command parity, the markers, a
    template word — is engine output and gets no scope.
    """
    report = ValidationReport()
    student, instructor = guides.student, guides.instructor

    def err(code_: str, message: str, path: str, line: int | None = None, scopes: Iterable[str] = ()) -> Finding:
        return Finding("error", code_, message, path=path, line=line, scopes=tuple(scopes))

    def sources(match: Any, audience: str = "student") -> tuple[str, ...]:
        return guide_source_scopes(data, root, match, audience=audience)

    commands = extract_bash_commands(student)
    if commands != list(UPSTREAM_GUIDE_COMMANDS):
        report.findings.append(err("guide.command_parity", f"student guide has {len(commands)} command lines that differ from the "
                                   f"{len(UPSTREAM_GUIDE_COMMANDS)} upstream Guide commands", STUDENT_GUIDE_PATH))
    if extract_bash_commands(instructor):
        report.findings.append(err("guide.command_parity", "the instructor guide may not contain bash command blocks", INSTRUCTOR_GUIDE_PATH))
    if not student.startswith(f"<!-- {STUDENT_MARKER_TOKEN} pack={data['id']} "):
        report.findings.append(err("guide.marker", "the student guide must start with its marker line", STUDENT_GUIDE_PATH, 1))
    if not instructor.startswith(f"<!-- {INSTRUCTOR_MARKER_TOKEN} pack={data['id']} -->"):
        report.findings.append(err("guide.marker", "the instructor guide must start with the instructor marker", INSTRUCTOR_GUIDE_PATH, 1))
    marker_lines = [i for i, line in enumerate(student.splitlines(), 1) if INSTRUCTOR_MARKER_TOKEN in line]
    if marker_lines:
        marker_scopes = sources(lambda text: INSTRUCTOR_MARKER_TOKEN in text)
        for i in marker_lines:
            report.findings.append(err("guide.instructor_marker_leak", "the instructor marker appears in the student guide",
                                       STUDENT_GUIDE_PATH, i, marker_scopes))
    for case in _holdout(data):
        single = {"evaluation": {"goldenSet": [case]}}
        leaks = holdout_leaks(single, student)
        if not leaks:
            continue
        leak_scopes = sources(lambda text, one=single: bool(holdout_leaks(one, text)))
        for line, what in leaks:
            report.findings.append(err("guide.holdout_leak", f"the student guide contains the {what}", STUDENT_GUIDE_PATH,
                                       line or None, leak_scopes))
    ns = data.get("namespace") or {}
    tokens = residue_tokens(ns, (data.get("evaluation") or {}).get("retrievalToolName", "")) if ns else []
    allowed = {p for p in GUIDE_HR_TOKENS if re.search(p, sources_text or "")}
    for rel, text, audience in ((STUDENT_GUIDE_PATH, student, "student"), (INSTRUCTOR_GUIDE_PATH, instructor, "instructor")):
        token_scopes: dict[str, tuple[str, ...]] = {}
        for i, line in enumerate(text.splitlines(), 1):
            token = next((tok for tok in tokens if tok in line), None)
            if token:
                if token not in token_scopes:
                    token_scopes[token] = sources(lambda t, tok=token: tok in t, audience)
                report.findings.append(err("guide.residue", f"template identifier '{token}' in a generated guide", rel, i,
                                           token_scopes[token]))
            if data.get("id") == "hr-default":
                continue
            hit = next((m.group(0) for p in GUIDE_HR_TOKENS if p not in allowed for m in [re.search(p, line)] if m), None)
            if hit:  # nowhere in the pack's own sources, so the template wrote it
                report.findings.append(err("guide.residue", f"template domain word '{hit}' in a generated guide, and nowhere in the pack's own sources", rel, i))
    return report


def check_instructor_isolation(release_root: Path) -> ValidationReport:
    """Release gate: no instructor guide and no instructor marker in any file; README.md is the student guide."""
    report = ValidationReport()
    release_root = Path(release_root)
    if not release_root.exists():
        return report
    for path in sorted(p for p in release_root.rglob("*") if p.is_file()):
        rel = path.relative_to(release_root).as_posix()
        if path.name == "instructor-guide.md":
            report.findings.append(Finding("error", "guide.instructor_marker_leak", "an instructor guide is inside the release", path=rel))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if INSTRUCTOR_MARKER_TOKEN in text:
            report.findings.append(Finding("error", "guide.instructor_marker_leak", "instructor-only material is inside the release", path=rel))
    guide = release_root / STUDENT_GUIDE_PATH
    readme = release_root / "README.md"
    if guide.is_file() and (not readme.is_file() or readme.read_bytes() != guide.read_bytes()):
        report.findings.append(Finding("error", "guide.readme_mismatch", "README.md must be the pack's student guide, byte for byte", path="README.md"))
    return report


# ---------------------------------------------------------------------------
# Preview (no build, gate not enforced)
# ---------------------------------------------------------------------------


def preview(scenario_path: Path | str, audience: str, *, template_commit: str, generator_version: str | None = None,
            rehearsal: Mapping[str, Any] | None = None) -> tuple[str, list[Finding]]:
    """A draft guide straight from a scenario file (provenance gate not enforced; draft banner added).

    Returns the markdown and the guide findings (narrative rules and output checks). Scenario schema or
    cross-reference errors propagate as :class:`scenario.ScenarioError`.
    """
    from . import __version__
    from .scenario import load_scenario

    if audience not in AUDIENCES:
        raise GuideError(f"unknown guide audience '{audience}'")
    scenario = load_scenario(scenario_path, enforce_gate=False)
    guides = build_guides(scenario, template_commit=template_commit, generator_version=generator_version or __version__,
                          rehearsal=rehearsal, draft=True)
    findings = list(guides.findings)
    findings += check_guide_narrative(scenario.data, scenario.root).findings
    findings += check_guides(scenario.data, guides, sources_text=pack_sources_text(scenario), root=scenario.root).findings
    return (guides.student if audience == "student" else guides.instructor), findings
