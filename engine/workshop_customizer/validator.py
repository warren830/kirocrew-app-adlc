"""Pack-level policy validation.

These checks are engine policy (not per-scenario configuration), so a pack cannot lower
its own bar:

* **Golden-set minimums** — at least ``MIN_GOLDEN_CASES`` cases, at least
  ``MIN_PER_CATEGORY`` in each of normal / boundary / prohibited, and at least
  ``MIN_HOLDOUT_RATIO`` of the cases held out.
* **Holdout isolation** — no holdout query text may appear in any student-facing file
  (prompts, skills, knowledge documents, practice set, rendered scripts).
* **Namespace residue** — after rendering, none of the upstream HR identifiers may remain
  in the release unless the pack deliberately uses the same value (the hr-default pack).
  Identifiers are hard failures; a bilingual prose word list is an advisory smoke check. The
  printed lines of the release scripts must not carry the HR Workshop's own wording (three golden
  questions, fixed counts, Workshop Studio content pages): ``residue.script`` (SPEC D6e).

* **Teaching skeleton** — ``labs.teaching`` is required (``teaching.missing`` blocks the build)
  and must declare a reproducible version of the HR Workshop's teaching contrasts: the first
  conversation, at least three retrieval probes (≥ 2 prompt_fixable, ≥ 1 retrieval_gap), baseline
  defects the optimization candidate fixes, noise, tool-use and refusal cases
  (:mod:`teaching_policy`).

* **AWS names and the retrieval tool** (SPEC D6a/b) — ``agentName`` at most
  :data:`AGENT_NAME_MAX`, ``knowledgeBaseName`` at most :data:`KNOWLEDGE_BASE_NAME_MAX`, the
  evaluator names at most :data:`EVALUATOR_NAME_MAX` (``namespace.*``); exactly one
  ``kind: retrieval`` tool whose inputSchema requires a string ``query`` (``tools.retrieval_*``).

``validate_scenario_policy(data, root=None)`` is the ONE policy entry point. It runs the
registered sub-checks in :data:`POLICY_CHECKS` (golden-set minimums, AWS name limits, the Gateway
SDK tool contract, knowledge-document files, the teaching skeleton, the L1 declaration advisories,
and the Workshop Guide narrative rules of :mod:`guide`). Production callers
(compiler, CLI, app backend) pass the scenario ``root`` so file-aware sub-checks can read the
pack's prompts and documents.

Every :class:`Finding` carries ``scopes``: the scenario sections (see :data:`SCOPES`) a repair
has to touch to fix it. Policy findings get their sub-check's default scopes when they do not
name their own; generated-file findings are scoped by path; an empty tuple means the finding is
not fixable by editing the scenario (an engine or template problem).
"""

from __future__ import annotations

import html
import json
import re
from functools import lru_cache
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable

MIN_GOLDEN_CASES = 12
MIN_PER_CATEGORY = 3
MIN_HOLDOUT_RATIO = 1 / 3
CATEGORIES = ("normal", "boundary", "prohibited")

#: AWS name limits (SPEC D6a). ``app/backend/routes.py`` derives generated namespaces within them.
AGENT_NAME_MAX = 19  # harness <agentName>_<agentName> <= 40
KNOWLEDGE_BASE_NAME_MAX = 50  # S3 Vectors bucket <knowledgeBaseName>-vectors-<suffix> <= 63
EVALUATOR_NAME_MAX = 48
EVALUATOR_SUFFIXES = ("_thelma_rag_quality", "_mtg_goal_success")
#: Key prefixes of the workshop bucket (workshop-infra's SkillsBucket) that are not a pack's knowledge
#: base: 03's skills, the Customizer's release bundles (sync.RELEASE_PREFIX) and create_kb's multimodal
#: staging. 01 prunes everything under namespace.kbPrefix, so a kbPrefix may never be one of them.
RESERVED_BUCKET_PREFIXES: tuple[str, ...] = ("skills/", "customizer-releases/", "multimodal/")
#: Entries of the Workshop instance's ~/workshop (the sync target root) an agentName could name: the
#: applier's current, previous and releases/, RunStep's run/ and 00-setup's skills/. 04-deploy and
#: 99-cleanup.sh remove ~/workshop/<agentName>, so a pack named like one would delete it. The app
#: (routes.RESERVED_AGENT_NAMES) and the host applier keep copies; a test keeps them in step.
RESERVED_AGENT_NAMES: tuple[str, ...] = ("current", "previous", "releases", "run", "skills")

#: Upstream (template) identifiers → the scenario namespace key that replaces them.
#: Ordered longest-first so a longer identifier is never split by a shorter one.
UPSTREAM_TOKENS: tuple[tuple[str, str], ...] = (
    ("hr-tools-handler", "lambdaFunctionName"),
    ("hr-knowledge-base", "knowledgeBaseName"),
    ("retrieve_hr_policy", "retrievalToolName"),
    ("hrassistant", "agentName"),
    ("hrgateway", "gatewayName"),
    ("hr-tools", "toolTargetName"),
    ("/app/hr", "ssmParameterPrefix"),
)

#: Prose words that usually mean HR content leaked into a non-HR pack. Advisory only.
PROSE_RESIDUE_WORDS: tuple[str, ...] = (
    "HR policy",
    "HR助手",
    "人力资源",
    "leave balance",
    "休假",
    "薪资",
    "salary",
)

TEXT_SUFFIXES = {".md", ".txt", ".sh", ".py", ".json", ".yaml", ".yml", ".env", ".cfg", ".ini", ".toml", ""}

#: SPEC D6e: wording the pinned upstream Guide scripts print about the HR Workshop itself — its three
#: golden questions as fixed counts, and its Workshop Studio content pages. A release of any other pack
#: must not print it (echo / printf / print lines of the release scripts).
SCRIPT_RESIDUE_PHRASES: tuple[str, ...] = (
    "三个 golden", "3 个 golden", "3 个对话", "三个问题", "三条 v2", "三个代表性问题",
    "content 06", "content 07",
)
#: HR topic words (the Workshop Guide's GUIDE_HR_TOKENS). On a printed script line of a non-HR release
#: they are residue unless the pack's own sources use the same word (then the line is pack content).
HR_TOPIC_PATTERNS: tuple[str, ...] = (
    r"\bHR\b", r"(?i)human resources?", r"(?i)\bemployees?\b", r"(?i)annual leave", r"(?i)sick leave",
    r"(?i)\bleave (?:balance|request|policy)\b", r"(?i)performance review", r"(?i)\bbenefits?\b",
    r"(?i)\bsalary\b", r"(?i)\bpayroll\b", r"(?i)\btenure\b", "HR-MultiWOZ",
    "人力资源", "员工", "年假", "病假", "休假", "绩效", "福利", "薪资", "薪酬", "工龄",
)
_PRINTED_LINE = re.compile(r"^\s*(?:echo\b|printf\b|read\s+-p\b|print\()")

#: Scenario sections a repair may touch. A Finding's ``scopes`` is a subset of these.
SCOPES: tuple[str, ...] = ("agent", "facts", "knowledge", "tools", "skills", "prompts", "golden", "labs", "guides")

#: Generated-file path prefix (relative to the compiled pack; a leading ``pack/`` is stripped for
#: release paths) → the scenario scopes that produce that file. Anything else (upstream scripts,
#: pack.env, RELEASE.json) is engine output and gets no scope.
_PATH_SCOPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("knowledge-base/docs/", ("knowledge",)),
    ("prompts/", ("prompts",)),
    ("skills/", ("skills",)),
    ("labs/", ("guides",)),
    ("golden/", ("golden",)),
    ("tools/", ("tools",)),
)


#: The generated student guide (pack-relative) and its release copy (the release root README.md).
STUDENT_GUIDE_FILES: tuple[str, ...] = ("labs/student-guide.md", "README.md")
#: The scopes a generated guide is built from (every content section but the prompts). A finding
#: in a guide belongs to the section whose text produced it: :func:`guide_source_scopes` attributes
#: it when the scenario is at hand; a path-only caller gets all of them.
GUIDE_CONTENT_SCOPES: tuple[str, ...] = ("agent", "facts", "knowledge", "tools", "skills", "golden", "labs", "guides")


def scopes_for_path(path: str | None) -> tuple[str, ...]:
    """Scopes of a generated file path (pack-relative, or release-relative under ``pack/``)."""
    if not path:
        return ()
    rel = path[len("pack/"):] if path.startswith("pack/") else path
    if rel in STUDENT_GUIDE_FILES:
        return GUIDE_CONTENT_SCOPES
    for prefix, scopes in _PATH_SCOPES:
        if rel.startswith(prefix):
            return scopes
    if rel == "pack.json":  # practice cases + lab observations
        return ("golden", "labs")
    return ()


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" | "warning"
    code: str
    message: str
    path: str | None = None
    line: int | None = None
    scopes: tuple[str, ...] = ()

    def describe(self) -> str:
        where = f" [{self.path}:{self.line}]" if self.path and self.line else (f" [{self.path}]" if self.path else "")
        return f"{self.severity.upper()} {self.code}: {self.message}{where}"


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "ValidationReport") -> None:
        self.findings.extend(other.findings)

    def describe(self) -> str:
        if not self.findings:
            return "OK: no findings"
        return "\n".join(f.describe() for f in self.findings)


class PackValidationError(Exception):
    def __init__(self, report: ValidationReport):
        self.report = report
        super().__init__("pack validation failed:\n" + "\n".join(f.describe() for f in report.errors))


# ---------------------------------------------------------------------------
# Golden-set policy
# ---------------------------------------------------------------------------


def check_golden_set(data: dict[str, Any], root: Path | None = None) -> ValidationReport:
    report = ValidationReport()
    cases = data.get("evaluation", {}).get("goldenSet", [])
    total = len(cases)
    if total < MIN_GOLDEN_CASES:
        report.findings.append(
            Finding("error", "golden.too_few", f"golden set has {total} cases; at least {MIN_GOLDEN_CASES} required", scopes=("golden",))
        )
    for category in CATEGORIES:
        count = sum(1 for c in cases if c.get("category") == category)
        if count < MIN_PER_CATEGORY:
            report.findings.append(
                Finding(
                    "error",
                    "golden.category_floor",
                    f"category '{category}' has {count} cases; at least {MIN_PER_CATEGORY} required",
                    scopes=("golden",),
                )
            )
    holdout = sum(1 for c in cases if c.get("set") == "holdout")
    if total and holdout / total < MIN_HOLDOUT_RATIO:
        report.findings.append(
            Finding(
                "error",
                "golden.holdout_ratio",
                f"{holdout}/{total} cases are holdout; at least {MIN_HOLDOUT_RATIO:.0%} required",
                scopes=("golden",),
            )
        )
    if total and holdout == total:
        report.findings.append(Finding("error", "golden.no_practice", "at least one practice case is required", scopes=("golden",)))
    # Actors are not policed: under the teaching render (SPEC D3) 09/10/12 ask every practice case as
    # its own fresh runtime actor (<actorId>-<RUN_TAG>-q<i>), so golden.single_eval_actor is gone.
    return report


# ---------------------------------------------------------------------------
# Text scanning helpers
# ---------------------------------------------------------------------------


def iter_text_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            yield path


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def unescape_markup(text: str) -> str:
    """``text`` as a reader sees it: backslash escapes (Markdown, bash double quotes) dropped and HTML
    entities decoded. The guide writes ``<`` as ``&lt;`` and ``|`` as ``\\|``, and the scripts
    backslash-escape ``"``, so a rendered file never carries a scenario string byte for byte."""
    return html.unescape(re.sub(r"\\(.)", r"\1", text))


#: Separates the JSON string values of one file, so no match spans two of them.
_VALUE_SEPARATOR = "\n\0\n"


def reader_views(rel: str, text: str) -> list[str]:
    """The normalized forms of a rendered file a student can read: the raw bytes, the markup-decoded
    text (:func:`unescape_markup`) and, for a ``.json`` file, its decoded string keys and values.
    Isolation checks match source strings against every view, never the raw bytes alone."""
    views = [text, unescape_markup(text)]
    if rel.lower().endswith(".json"):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            views.append(_VALUE_SEPARATOR.join(_strings_and_keys(parsed)))
    return list(dict.fromkeys(_normalize(v) for v in views))


def _strings_and_keys(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings_and_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings_and_keys(item)


def reader_contains(text: str, needle: str, *, rel: str = "") -> bool:
    """True when the normalized ``needle`` is in any :func:`reader_views` of ``text``."""
    return bool(needle) and any(needle in view for view in reader_views(rel, text))


def find_occurrences(root: Path, needles: Iterable[str], *, exclude: Iterable[str] = ()) -> list[tuple[str, int, str]]:
    """Return (relative path, line number, needle) for every literal hit under root."""
    excluded = {Path(e).as_posix() for e in exclude}
    hits: list[tuple[str, int, str]] = []
    needles = [n for n in needles if n]
    for path in iter_text_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in excluded:
            continue
        text = _read_text(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for needle in needles:
                if needle in line:
                    hits.append((rel, lineno, needle))
    return hits


# ---------------------------------------------------------------------------
# Holdout isolation
# ---------------------------------------------------------------------------


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def is_generated_guide(path: str | None) -> bool:
    """True for the generated student guide (pack or release path) and the release README.md."""
    if not path:
        return False
    rel = path[len("pack/"):] if path.startswith("pack/") else path
    return rel in STUDENT_GUIDE_FILES


def guide_source_texts(data: dict[str, Any], root: Path | None = None, *, audience: str = "student") -> dict[str, list[str]]:
    """Per repair scope, the scenario text a generated guide embeds.

    The student guide is built from the agent, facts, documents, tools, skills, practice cases,
    labs.teaching/observations and the labs.guide narrative (plus labs.studentGuideFile when ``root``
    is given); the instructor guide adds every golden case, the prompts and labs.instructorGuideFile.
    """
    labs = data.get("labs") if isinstance(data.get("labs"), dict) else {}
    evaluation = data.get("evaluation") if isinstance(data.get("evaluation"), dict) else {}
    cases = [c for c in evaluation.get("goldenSet") or [] if isinstance(c, dict)]
    if audience == "student":
        cases = [c for c in cases if c.get("set") == "practice"]
    sources: dict[str, list[str]] = {
        "agent": [*_strings(data.get("agent")), *_strings(data.get("description"))],
        "facts": list(_strings(data.get("facts"))),
        "knowledge": list(_strings(data.get("knowledge"))),
        "tools": [*_strings(data.get("tools")), *_strings(evaluation.get("retrievalToolName"))],
        "skills": list(_strings(data.get("skills"))),
        "golden": list(_strings(cases)),
        "labs": [*_strings(labs.get("teaching")), *_strings(labs.get("observations"))],
        "guides": list(_strings(labs.get("guide"))),
    }
    if root is not None:
        from .scenario import resolve_inside

        def body(rel: Any) -> str | None:
            if not isinstance(rel, str):
                return None
            try:
                target = resolve_inside(Path(root), rel)
                return target.read_text(encoding="utf-8", errors="replace") if target.is_file() else None
            except Exception:  # noqa: BLE001 - a bad path is the cross-reference check's finding
                return None

        supplements = ["studentGuideFile"] + (["instructorGuideFile"] if audience != "student" else [])
        sources["guides"] += [t for t in (body(labs.get(k)) for k in supplements) if t is not None]
        if audience != "student":
            prompts = data.get("prompts") if isinstance(data.get("prompts"), dict) else {}
            sources["prompts"] = [t for t in (body(v) for v in prompts.values()) if t is not None]
    return sources


def guide_source_scopes(data: dict[str, Any], root: Path | None, match: Callable[[str], bool], *,
                        audience: str = "student") -> tuple[str, ...]:
    """The scopes whose guide source text ``match`` finds (in :data:`SCOPES` order). Empty means no
    scenario text produced it: the template or the engine did, and no repair can fix it."""
    sources = guide_source_texts(data, root, audience=audience)
    return tuple(scope for scope in SCOPES if any(match(text) for text in sources.get(scope, ())))


def check_holdout_isolation(data: dict[str, Any], student_root: Path, *, root: Path | None = None) -> ValidationReport:
    """No holdout query may appear in any student-facing file under ``student_root``.

    A leak in a generated student guide is attributed to the scenario section that carries the
    query (``root`` is the scenario root, for the student supplement), else it is engine output.
    """
    report = ValidationReport()
    holdout_queries = [
        (c["id"], _normalize(c["query"]))
        for c in data.get("evaluation", {}).get("goldenSet", [])
        if c.get("set") == "holdout"
    ]
    if not holdout_queries or not student_root.exists():
        return report
    # A compound holdout id (it has a dash, so it is never an ordinary word) is instructor-only too: it
    # marks instructor text (answer keys, supplements) that reached a student-facing file.
    id_patterns = [(cid, re.compile(rf"(?<![A-Za-z0-9-]){re.escape(cid)}(?![A-Za-z0-9-])")) for cid, _q in holdout_queries if "-" in cid]
    for path in iter_text_files(student_root):
        text = _read_text(path)
        if text is None:
            continue
        rel = path.relative_to(student_root).as_posix()
        for case_id, pattern in id_patterns:
            if pattern.search(text):
                scopes = (guide_source_scopes(data, root, lambda t, p=pattern: bool(p.search(t))) if is_generated_guide(rel)
                          else scopes_for_path(rel))
                report.findings.append(Finding("error", "holdout.id_leaked",
                                               f"holdout case id '{case_id}' appears in a student-facing file", path=rel, scopes=scopes))
        # Match what a student reads, not the escaped bytes: the guide HTML-escapes ``<``/``>`` and
        # pack.json JSON-escapes ``"``, so a raw match alone misses a query copied into labs.teaching.
        views = reader_views(rel, text)
        for case_id, query in holdout_queries:
            if query and any(query in view for view in views):
                if is_generated_guide(rel):
                    scopes = guide_source_scopes(data, root, lambda text, q=query: reader_contains(text, q))
                else:
                    scopes = scopes_for_path(rel)
                report.findings.append(
                    Finding(
                        "error",
                        "holdout.leaked",
                        f"holdout case '{case_id}' query appears in a student-facing file",
                        path=rel,
                        scopes=scopes,
                    )
                )
    return report


# ---------------------------------------------------------------------------
# Namespace residue
# ---------------------------------------------------------------------------


def residue_tokens(namespace: dict[str, str], retrieval_tool_name: str) -> list[str]:
    """Upstream identifiers that must NOT appear because the pack replaced them."""
    values = dict(namespace)
    values["retrievalToolName"] = retrieval_tool_name
    return [token for token, key in UPSTREAM_TOKENS if values.get(key) != token]


def check_namespace_residue(
    data: dict[str, Any], release_root: Path, *, exclude: Iterable[str] = ()
) -> ValidationReport:
    report = ValidationReport()
    if not release_root.exists():
        return report
    tokens = residue_tokens(data["namespace"], data["evaluation"]["retrievalToolName"])
    for rel, lineno, token in find_occurrences(release_root, tokens, exclude=exclude):
        report.findings.append(
            Finding(
                "error",
                "residue.identifier",
                f"upstream identifier '{token}' remains in the rendered release",
                path=rel,
                line=lineno,
                scopes=scopes_for_path(rel),
            )
        )
    return report


def check_script_residue(data: dict[str, Any], release_root: Path, *, sources_text: str = "") -> ValidationReport:
    """SPEC D6e release gate: no HR Workshop wording on a printed line of a non-HR release script.

    Scans the echo / printf / ``read -p`` / ``print(`` lines of the release's top-level ``*.sh``
    scripts. :data:`SCRIPT_RESIDUE_PHRASES` are always residue; an :data:`HR_TOPIC_PATTERNS` hit is
    residue only when ``sources_text`` (the pack's own scenario and compiled files) never matches the
    same pattern. The hr-default pack is exempt. Findings are engine problems (no scopes).
    """
    report = ValidationReport()
    if data.get("id") == "hr-default" or not release_root.exists():
        return report
    allowed = {p for p in HR_TOPIC_PATTERNS if re.search(p, sources_text or "")}
    for path in sorted(release_root.glob("*.sh")):
        rel = path.relative_to(release_root).as_posix()
        text = _read_text(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not _PRINTED_LINE.match(line):
                continue
            hit = next((ph for ph in SCRIPT_RESIDUE_PHRASES if ph in line), None)
            if hit is None:
                hit = next((m.group(0) for p in HR_TOPIC_PATTERNS if p not in allowed for m in [re.search(p, line)] if m), None)
            if hit is not None:
                report.findings.append(Finding(
                    "error", "residue.script",
                    f"release script prints HR Workshop wording '{hit}'; a non-HR release must print the pack's own counts and cases",
                    path=rel, line=lineno,
                ))
    # AWS resource descriptions the helpers pass (description="..."): they show in the AWS console.
    for path in sorted(release_root.rglob("*.py")):
        rel = path.relative_to(release_root).as_posix()
        if rel.startswith("evaluators/"):
            continue  # the pinned evaluators carry no AWS descriptions; their prompts are confined elsewhere (D5)
        text = _read_text(path)
        for lineno, line in enumerate((text or "").splitlines(), start=1):
            literal = _AWS_DESCRIPTION.search(line)
            if literal is None:
                continue
            hit = next((m.group(0) for p in HR_TOPIC_PATTERNS if p not in allowed for m in [re.search(p, literal.group(1))] if m), None)
            if hit is not None:
                report.findings.append(Finding(
                    "error", "residue.script",
                    f"release helper passes an AWS description with HR Workshop wording '{hit}'; name the pack instead",
                    path=rel, line=lineno,
                ))
    return report


#: A ``description="..."`` argument of a release helper (an AWS resource description, not argparse help).
_AWS_DESCRIPTION = re.compile(r'^\s+description\s*=\s*"([^"]*)"')


def check_prose_residue(data: dict[str, Any], root: Path, *, words: Iterable[str] = PROSE_RESIDUE_WORDS,
                        source_root: Path | None = None) -> ValidationReport:
    """Advisory: HR prose in a pack whose namespace is not the HR default (``source_root``: the scenario
    root, to attribute a word in the generated guide to the section that carries it)."""
    report = ValidationReport()
    if data.get("id") == "hr-default" or not root.exists():
        return report
    for rel, lineno, word in find_occurrences(root, words):
        scopes = (guide_source_scopes(data, source_root, lambda text, w=word: w in text) if is_generated_guide(rel)
                  else scopes_for_path(rel))
        report.findings.append(
            Finding("warning", "residue.prose", f"possible template prose '{word}' left in pack", path=rel, line=lineno,
                    scopes=scopes)
        )
    return report


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _gateway_tool_shape():
    import botocore.session

    shape = botocore.session.get_session().get_service_model(
        "bedrock-agentcore-control"
    ).operation_model("CreateGatewayTarget").input_shape
    for field in ("targetConfiguration", "mcp", "lambda", "toolSchema", "inlinePayload"):
        shape = shape.members[field]
    return shape.member


def check_namespace(data: dict[str, Any], root: Path | None = None) -> ValidationReport:
    """SPEC D6a: the AWS resource names the Guide scripts derive from the namespace fit AWS limits.

    04-deploy names the harness ``<agentName>_<agentName>`` (at most 40 characters), 08 names the
    evaluators ``<agentName>_thelma_rag_quality`` / ``<agentName>_mtg_goal_success`` (at most 48),
    and 01 names the S3 Vectors bucket ``<knowledgeBaseName>-vectors-<suffix>`` (at most 63). The
    schema mirrors the first and last limit; this check also covers data that skipped the schema.
    An agentName is never one of :data:`RESERVED_AGENT_NAMES` and a kbPrefix never one of
    :data:`RESERVED_BUCKET_PREFIXES`.
    """
    report = ValidationReport()
    ns = data.get("namespace") or {}
    agent, kb = ns.get("agentName"), ns.get("knowledgeBaseName")
    if isinstance(agent, str):
        if agent in RESERVED_AGENT_NAMES:
            report.findings.append(Finding(
                "error", "namespace.agent_name_reserved",
                f"namespace.agentName '{agent}' names an entry of the Workshop instance's ~/workshop "
                f"({', '.join(RESERVED_AGENT_NAMES)}): 04-deploy and 99-cleanup.sh remove ~/workshop/<agentName>",
                path="namespace.agentName", scopes=("agent",),
            ))
        if len(agent) > AGENT_NAME_MAX:
            report.findings.append(Finding(
                "error", "namespace.agent_name_too_long",
                f"namespace.agentName '{agent}' has {len(agent)} characters; at most {AGENT_NAME_MAX} (04-deploy names the "
                f"harness '<agentName>_<agentName>', which AWS limits to {2 * AGENT_NAME_MAX + 2} characters)",
                path="namespace.agentName", scopes=("agent",),
            ))
        for suffix in EVALUATOR_SUFFIXES:
            name = agent + suffix
            if len(name) > EVALUATOR_NAME_MAX:
                report.findings.append(Finding(
                    "error", "namespace.evaluator_name_too_long",
                    f"evaluator name '{name}' has {len(name)} characters; AWS allows at most {EVALUATOR_NAME_MAX} "
                    "(shorten namespace.agentName)",
                    path="namespace.agentName", scopes=("agent",),
                ))
    prefix = ns.get("kbPrefix")
    if isinstance(prefix, str) and prefix.strip("/").lower() + "/" in RESERVED_BUCKET_PREFIXES:
        report.findings.append(Finding(
            "error", "namespace.kb_prefix_reserved",
            f"namespace.kbPrefix '{prefix}' is a reserved prefix of the workshop bucket "
            f"({', '.join(RESERVED_BUCKET_PREFIXES)}): 01-create-kb deletes every object under the "
            "knowledge-base prefix that is not one of this pack's documents",
            path="namespace.kbPrefix", scopes=("agent",),
        ))
    if isinstance(kb, str) and len(kb) > KNOWLEDGE_BASE_NAME_MAX:
        report.findings.append(Finding(
            "error", "namespace.kb_name_too_long",
            f"namespace.knowledgeBaseName '{kb}' has {len(kb)} characters; at most {KNOWLEDGE_BASE_NAME_MAX} (01-create-kb "
            "names the S3 Vectors bucket '<knowledgeBaseName>-vectors-<suffix>', which AWS limits to 63 characters)",
            path="namespace.knowledgeBaseName", scopes=("agent",),
        ))
    report.extend(check_tool_target_mentions(data, root))
    return report


#: A Gateway tool-target name as the packs write it (``<name>-tools``, the derive_namespace convention).
_TARGET_MENTION = re.compile(r"(?<![A-Za-z0-9_-])([a-z0-9][a-z0-9-]*-tools)(?![A-Za-z0-9_-])")


def check_tool_target_mentions(data: dict[str, Any], root: Path | None = None) -> ValidationReport:
    """Prompts and skills name only this pack's Gateway target (namespace.toolTargetName).

    A prompt that says "Use it-tools to ..." in a pack whose target is another name points the agent at
    a target that does not exist (and hides the retrieval hint the teaching rules require). Files are
    read only when ``root`` is given.
    """
    report = ValidationReport()
    target = (data.get("namespace") or {}).get("toolTargetName")
    if root is None or not isinstance(target, str):
        return report
    from .scenario import resolve_inside

    files = [(f"prompts.{key}", rel, ("prompts",)) for key, rel in (data.get("prompts") or {}).items() if isinstance(rel, str)]
    files += [(f"skills[{s.get('name')}]", s.get("file"), ("skills",)) for s in data.get("skills") or []
              if isinstance(s, dict) and isinstance(s.get("file"), str)]
    for where, rel, scopes in files:
        try:
            path = resolve_inside(Path(root), rel)
        except Exception:  # noqa: BLE001 - a bad path is the cross-reference check's finding
            continue
        text = _read_text(path) if path.is_file() else None
        for name in sorted({m.group(1) for m in _TARGET_MENTION.finditer(text or "")} - {target}):
            report.findings.append(Finding(
                "error", "namespace.foreign_tool_target",
                f"{where} ({rel}) names the tool target '{name}', but this pack's Gateway target is '{target}' "
                "(namespace.toolTargetName); name the pack's own target and retrieval tool",
                path=rel, scopes=scopes,
            ))
    return report


def _retrieval_query_ok(tool: dict[str, Any]) -> bool:
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    query = properties.get("query")
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    return isinstance(query, dict) and query.get("type") == "string" and "query" in required


def check_gateway_tools(data: dict[str, Any], root: Path | None = None) -> ValidationReport:
    """Every tool must be accepted by the Gateway SDK's CreateGatewayTarget inline tool schema.

    SPEC D6b: exactly one ``kind: retrieval`` tool, and its inputSchema has a required string
    property ``query`` (the Lambda handler forwards only ``query`` to the knowledge base).
    """
    from botocore.exceptions import ParamValidationError
    from botocore.validate import validate_parameters

    report = ValidationReport()
    for tool in data.get("tools", []):
        payload = {key: tool[key] for key in ("name", "description", "inputSchema")}
        try:
            validate_parameters(payload, _gateway_tool_shape())
        except ParamValidationError as exc:
            report.findings.append(Finding(
                "error", "tools.gateway_schema", f"{tool['name']}: {exc}",
                path=f"tools.{tool['name']}.inputSchema", scopes=("tools",),
            ))
    retrieval = [t for t in data.get("tools", []) if isinstance(t, dict) and t.get("kind") == "retrieval"]
    if len(retrieval) != 1:
        names = ", ".join(f"'{t.get('name')}'" for t in retrieval) or "none"
        report.findings.append(Finding(
            "error", "tools.retrieval_count",
            f"exactly one tool of kind 'retrieval' is required (the knowledge-base search the Guide scores); found "
            f"{len(retrieval)}: {names}. Make the other tools kind 'mock'",
            path="tools", scopes=("tools",),
        ))
    for tool in retrieval:
        if not _retrieval_query_ok(tool):
            report.findings.append(Finding(
                "error", "tools.retrieval_query_schema",
                f"retrieval tool '{tool.get('name')}' must declare inputSchema.properties.query of type 'string' and list "
                "'query' in inputSchema.required; the Lambda handler sends only 'query' to the knowledge base",
                path=f"tools.{tool.get('name')}.inputSchema", scopes=("tools",),
            ))
    return report


def check_knowledge_documents(data: dict[str, Any], root: Path | None = None) -> ValidationReport:
    """SPEC D6c: knowledge documents are ``.md`` files with unique file names.

    01-create-kb.sh ingests only ``*.md`` from the compiled pack, and the compiler stores documents by
    file name (so two documents with one name would silently overwrite each other; S3 keys and the
    host's case-insensitive file systems make the comparison case-insensitive).
    """
    report = ValidationReport()
    seen: dict[str, str] = {}
    for doc in data.get("knowledge", {}).get("documents", []):
        did, rel = str(doc.get("id")), str(doc.get("file", ""))
        name = Path(rel).name
        where = f"knowledge.documents.{did}"
        if not name.endswith(".md"):
            report.findings.append(Finding(
                "error", "knowledge.document_not_markdown",
                f"knowledge document '{did}' is '{rel}'; knowledge documents must be Markdown files ending in '.md' "
                "(01-create-kb.sh ingests only *.md)",
                path=where, scopes=("knowledge",),
            ))
        key = name.casefold()
        if key in seen:
            report.findings.append(Finding(
                "error", "knowledge.duplicate_basename",
                f"knowledge documents '{seen[key]}' and '{did}' share the file name '{name}'; the compiled pack stores "
                "documents by file name, so one would overwrite the other — rename one file",
                path=where, scopes=("knowledge",),
            ))
        else:
            seen[key] = did
    return report


def check_teaching_policy(data: dict[str, Any], root: Path | None = None) -> ValidationReport:
    """Teaching-skeleton policy (``teaching.*`` and ``tools.retrieval_marker_collision`` codes).

    Structure rules always run; file rules (prompts, knowledge documents) run when ``root`` is
    given. ``teaching.missing`` is an error: a pack cannot skip the teaching contrasts.
    """
    from .teaching_policy import check_teaching  # lazy: teaching_policy imports this module

    return check_teaching(data, root)


#: expected.* keys L1 turns into a check (a case without any is judged by Mind the Goal only).
L1_ASSERTION_KEYS: tuple[str, ...] = (
    "mustMention", "mustMentionAnyOf", "mustNotMention", "requiredTools", "forbiddenTools", "shouldRefuse", "shouldEscalate",
)
#: L1's lexical checks need short literal tokens; longer phrases rarely appear verbatim.
L1_MAX_PHRASE = 40


def _l1_strings(values: Any) -> list[str]:
    return [v for v in (values or []) if isinstance(v, str) and v.strip()] if isinstance(values, list) else []


def handoff_evidence(expected: dict[str, Any], l1_config: dict[str, Any] | None) -> tuple[str, ...]:
    """What L1 can read a hand-off from, for a case with ``expected.shouldEscalate: true``.

    ``evaluation.l1.escalationTools`` / ``escalationMarkers`` let the shouldEscalate check pass;
    ``mustMention`` / ``mustMentionAnyOf`` naming the hand-off target let L1 fail an answer without
    it (the report's honesty reading of an absent gap uses the same checks,
    ``rehearsal.HONESTY_CHECKS``). Empty: L1 can only defer the hand-off to Mind the Goal.
    """
    cfg = l1_config if isinstance(l1_config, dict) else {}
    sources = [f"evaluation.l1.{key}" for key in ("escalationTools", "escalationMarkers") if _l1_strings(cfg.get(key))]
    if _l1_strings(expected.get("mustMention")):
        sources.append("mustMention")
    if any(_l1_strings(group) for group in expected.get("mustMentionAnyOf") or [] if isinstance(group, list)):
        sources.append("mustMentionAnyOf")
    return tuple(sources)


def _has_l1_assertion(expected: dict[str, Any]) -> bool:
    for key in L1_ASSERTION_KEYS:
        value = expected.get(key)
        if key in ("shouldRefuse", "shouldEscalate"):
            if value is True:
                return True
        elif key == "mustMentionAnyOf":
            if any(_l1_strings(group) for group in value or [] if isinstance(group, list)):
                return True
        elif _l1_strings(value):
            return True
    return False


def check_l1_policy(data: dict[str, Any], root: Path | None = None) -> ValidationReport:
    """L1 declaration advisories (designs[2]); warnings only, scoped to the golden set (tools for names).

    * ``l1.no_assertions`` — a practice case whose ``expected`` gives L1 nothing to check (Mind the
      Goal judges it alone).
    * ``l1.escalation_deferred`` — ``shouldEscalate: true`` but no hand-off evidence L1 can read
      (:func:`handoff_evidence`): no escalationTools / escalationMarkers, no mustMention(AnyOf).
    * ``l1.long_phrase`` — a mustMention / mustMentionAnyOf / mustNotMention item over
      :data:`L1_MAX_PHRASE` characters.
    * ``l1.tool_name_separator`` — a tool name containing ``___``, the separator of the Gateway's
      ``<target>___<tool>`` span names (L1 and THELMA would split it wrongly).
    """
    report = ValidationReport()
    evaluation = data.get("evaluation") or {}
    cfg = evaluation.get("l1")
    for case in evaluation.get("goldenSet") or []:
        if not isinstance(case, dict):
            continue
        cid, expected = case.get("id"), case.get("expected") if isinstance(case.get("expected"), dict) else {}
        where = f"evaluation.goldenSet.{cid}.expected"
        if case.get("set") == "practice" and not _has_l1_assertion(expected):
            report.findings.append(Finding(
                "warning", "l1.no_assertions",
                f"practice case '{cid}' has no L1 assertion (mustMention, mustMentionAnyOf, mustNotMention, required/"
                "forbiddenTools, shouldRefuse, shouldEscalate); only Mind the Goal judges it",
                path=where, scopes=("golden",),
            ))
        if expected.get("shouldEscalate") is True and not handoff_evidence(expected, cfg):
            report.findings.append(Finding(
                "warning", "l1.escalation_deferred",
                f"case '{cid}' sets shouldEscalate but L1 cannot see a hand-off: declare evaluation.l1.escalationMarkers "
                "(or escalationTools) or name the hand-off target in expected.mustMention / mustMentionAnyOf; otherwise "
                "L1 always defers it to Mind the Goal",
                path=where, scopes=("golden",),
            ))
        phrases = _l1_strings(expected.get("mustMention")) + _l1_strings(expected.get("mustNotMention"))
        phrases += [p for group in expected.get("mustMentionAnyOf") or [] if isinstance(group, list) for p in _l1_strings(group)]
        for phrase in phrases:
            if len(phrase) > L1_MAX_PHRASE:
                report.findings.append(Finding(
                    "warning", "l1.long_phrase",
                    f"case '{cid}': '{phrase[:60]}' has {len(phrase)} characters; L1 matches literally, so use short tokens "
                    f"(at most {L1_MAX_PHRASE} characters: numbers, codes, names, one or two words)",
                    path=where, scopes=("golden",),
                ))
    for tool in data.get("tools") or []:
        name = tool.get("name") if isinstance(tool, dict) else None
        if isinstance(name, str) and "___" in name:
            report.findings.append(Finding(
                "warning", "l1.tool_name_separator",
                f"tool '{name}' contains '___', the separator of the Gateway's '<target>___<tool>' span names; L1 and "
                "THELMA would read the wrong tool name from its spans. Use single underscores",
                path=f"tools.{name}", scopes=("tools",),
            ))
    return report


def check_guide_policy(data: dict[str, Any], root: Path | None = None) -> ValidationReport:
    """Guide narrative policy (``guide.*`` codes): the labs.guide narrative and the guide supplements.

    No commands, headings, HTML or stray links; no template identifier; student text never names the
    holdout or carries the instructor marker (:func:`guide.check_guide_narrative`). The supplement files
    are read only when ``root`` is given.
    """
    from .guide import check_guide_narrative  # lazy: guide imports this module

    return check_guide_narrative(data, root)


PolicyCheck = Callable[[dict[str, Any], "Path | None"], ValidationReport]


@dataclass(frozen=True)
class PolicySubCheck:
    """One registered policy sub-check and the scopes its findings default to."""

    name: str
    check: PolicyCheck
    default_scopes: tuple[str, ...]


#: The policy sub-checks, in run order. ``validate_scenario_policy`` looks this up at call time.
POLICY_CHECKS: tuple[PolicySubCheck, ...] = (
    PolicySubCheck("golden", check_golden_set, ("golden",)),
    PolicySubCheck("namespace", check_namespace, ("agent",)),
    PolicySubCheck("gateway", check_gateway_tools, ("tools",)),
    PolicySubCheck("knowledge", check_knowledge_documents, ("knowledge",)),
    PolicySubCheck("teaching", check_teaching_policy, ("golden", "knowledge", "prompts", "labs")),
    PolicySubCheck("l1", check_l1_policy, ("golden",)),
    PolicySubCheck("guide", check_guide_policy, ("guides",)),
)


def validate_scenario_policy(data: dict[str, Any], root: Path | None = None) -> ValidationReport:
    """Run every registered policy sub-check without any AWS calls.

    ``root`` is the scenario root (the directory holding scenario.yaml); file-aware sub-checks use it
    to read prompts and knowledge documents and skip their file rules when it is ``None``. Every
    returned finding carries scopes (its own, else the sub-check's defaults).
    """
    root = Path(root) if root is not None else None
    report = ValidationReport()
    for sub_check in POLICY_CHECKS:
        for finding in sub_check.check(data, root).findings:
            report.findings.append(finding if finding.scopes else replace(finding, scopes=sub_check.default_scopes))
    return report


# ---------------------------------------------------------------------------
# Secret / PII residue (Security gate)
# ---------------------------------------------------------------------------

#: ASCII-only word boundaries. ``\b`` is Unicode-aware, so it finds no boundary between a CJK character
#: and a digit, and ``身份证号110101…`` (how Chinese prose writes an ID) would slip past every scan.
ASCII_START = r"(?<![0-9A-Za-z])"
ASCII_END = r"(?![0-9A-Za-z])"

SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("aws-access-key-id", ASCII_START + r"(?:AKIA|ASIA)[0-9A-Z]{16}" + ASCII_END),
    ("aws-secret-access-key", r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}"),
    ("aws-session-token", r"(?i)(?:aws_session_token|\"SessionToken\")\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{100,}"),
    ("private-key", r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ("bearer-token", r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._\-]{20,}"),
    ("cn-national-id", ASCII_START + r"[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]" + ASCII_END),
    ("us-ssn", ASCII_START + r"(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}" + ASCII_END),
)
SECRET_SCAN_EXCLUDE = {".png", ".jpg", ".jpeg", ".gif", ".zip", ".pdf", ".ico", ".woff", ".woff2"}


def check_secret_residue(root: Path, *, exclude: Iterable[str] = ()) -> ValidationReport:
    """Fail closed on credential material or real-PII shapes anywhere under ``root``."""
    report = ValidationReport()
    excluded = set(exclude)
    compiled = [(code, re.compile(pattern)) for code, pattern in SECRET_PATTERNS]
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel in excluded or path.suffix.lower() in SECRET_SCAN_EXCLUDE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for code, regex in compiled:
                if regex.search(line):
                    report.findings.append(Finding("error", f"secret-residue/{code}", "credential or PII-shaped material must never enter a pack, release, export or log", rel, lineno, scopes_for_path(rel)))
                    break
    return report


def validate_output_tree(
    data: dict[str, Any],
    *,
    student_root: Path | None = None,
    release_root: Path | None = None,
    residue_exclude: Iterable[str] = (),
    prose_check: bool = True,
    source_root: Path | None = None,
) -> ValidationReport:
    """Policy checks that need generated files (``source_root``: the scenario root, used to attribute
    findings in the generated guide to the scenario section that produced them)."""
    report = ValidationReport()
    if student_root is not None:
        report.extend(check_holdout_isolation(data, student_root, root=source_root))
        if prose_check:
            report.extend(check_prose_residue(data, student_root, source_root=source_root))
    if release_root is not None:
        report.extend(check_namespace_residue(data, release_root, exclude=residue_exclude))
        report.extend(check_secret_residue(release_root, exclude=residue_exclude))
        from .guide import check_instructor_isolation  # lazy: guide imports this module

        report.extend(check_instructor_isolation(release_root))
    return report
