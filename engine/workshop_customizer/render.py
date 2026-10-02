"""Release rendering: turn the pinned upstream template + a compiled pack into a runnable release.

A *release* is the complete directory that is synced to the workshop EC2 and run by students:

```
release/
├── RELEASE.json                 manifest: pack id, content-derived version, template commit,
│                                per-file SHA-256, patches applied, token replacement counts
├── README.md                    the scenario's generated Workshop Guide (= pack/labs/student-guide.md)
├── pack/                        the compiled student-facing pack (from compiler.compile_pack)
├── 00-config.sh … 99-cleanup.sh upstream scripts, patched + token-rendered
├── cfn/, evaluators/, gateway/, knowledge-base/, lambda/
└── LICENSE
```

Rendering is deliberately conservative:

* **Anchored patches** replace known upstream constructs (prompt heredocs, golden arrays, skill
  lists, the HR docs generator call, the Lambda zip line). Every anchor must match the expected
  number of times against the pinned commit, otherwise rendering fails — a template drift is a
  hard error, never a silent partial render.
* **Ordered token map** rewrites the upstream HR identifiers (``hrassistant``, ``hr-tools`` …)
  to the pack namespace across every text file except ``pack/``; counts are recorded per file.
* **Gates** run on the result: no upstream identifier may remain (unless the pack keeps the
  same value), and no holdout golden query may appear anywhere in the release.
* The release **version** derives from the content hashes, so identical inputs produce an
  identical version and manifest; a same-version-different-hash bundle is detectable on the host.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__, script_facts, teaching
from .compiler import LAMBDA_MODULE, PACK_DIR, CompiledPack, sha256_file
from .scenario import provenance_policy
from .script_facts import ScriptFacts
from .validator import RESERVED_BUCKET_PREFIXES, PackValidationError, check_script_residue, iter_text_files, validate_output_tree

TEMPLATES_DIR = Path(__file__).parent / "templates"
MANIFEST_NAME = "RELEASE.json"

#: The single L1 implementation (SPEC D4), shipped byte for byte at the release root.
L1_SOURCE = Path(__file__).parent / "l1.py"
L1_RELEASE_NAME = "l1_eval.py"

#: The Mind the Goal judge prompt: the one evaluator file render patches (SPEC D5, five anchored
#: patches). Every other evaluator file ships byte-identical to the pinned upstream.
MTG_PROMPTS_REL = "evaluators/mtg_eval/evaluators/mind_the_goal/prompts.py"
SCENARIO_PATCHED_EVALUATOR_FILES: tuple[str, ...] = (MTG_PROMPTS_REL,)
MTG_PROMPT_LIMIT = 6000

#: The filter 09/10 hand `grep -vE` on every `agentcore invoke` (upstream wording, kept verbatim).
_INVOKE_NOISE = r"(?P<noise>[^']*)"

#: Upstream files that are replaced by generated artifacts and must not ship.
REMOVED_UPSTREAM_FILES = (
    "knowledge-base/generate_hr_docs.py",
    "knowledge-base/domain_faqs.py",
    "lambda/hr_tools_handler.py",
    "gateway/hr-tools-schema.json",
    "03-configure-skills.sh",
)

#: Upstream paths never copied into a release (documentation, media, VCS).
EXCLUDED_UPSTREAM = (".git", ".gitignore", "assets", "docs", "README.md", "README.zh-CN.md", "CODE_OF_CONDUCT.md", "CONTRIBUTING.md")

TEXT_SUFFIXES = {".sh", ".py", ".yaml", ".yml", ".json", ".md", ".txt", ".env", ""}


class RenderError(Exception):
    """A template anchor did not match as expected, or a required input is missing."""


@dataclass
class PatchRecord:
    file: str
    name: str
    matches: int


@dataclass
class RenderedRelease:
    pack: CompiledPack
    release_dir: Path
    version: str
    files: dict[str, str] = field(default_factory=dict)
    patches: list[PatchRecord] = field(default_factory=list)
    token_counts: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def manifest_path(self) -> Path:
        return self.release_dir / MANIFEST_NAME


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def bash_quote(value: str) -> str:
    """Double-quoted bash literal for a golden query / label."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"{escaped}"'


def token_map(data: dict[str, Any]) -> list[tuple[str, str]]:
    """Ordered (upstream identifier → pack value) replacements; longest identifiers first."""
    ns = data["namespace"]
    return [
        # Nova Pro can fail its tool-use protocol on Gateway tool names containing
        # hyphens. Gateway target names cannot contain underscores, so compact the
        # target prefix while retaining the student's logical tool alias.
        ("hr-tools___", ns["toolTargetName"].replace("-", "") + "___"),
        ("hr-tools-handler", ns["lambdaFunctionName"]),
        ("hr-knowledge-base", ns["knowledgeBaseName"]),
        ("retrieve_hr_policy", data["evaluation"]["retrievalToolName"]),
        ("hr_tools_handler", LAMBDA_MODULE),
        ("hrassistant", ns["agentName"]),
        ("hrgateway", ns["gatewayName"]),
        ("hr-tools", ns["toolTargetName"]),
        ("/app/hr", ns["ssmParameterPrefix"]),
        ('"hr/"', f'"{ns["kbPrefix"]}"'),
    ]


def _copy_upstream(upstream: Path, release: Path) -> None:
    if not (upstream / "04-deploy.sh").is_file():
        raise RenderError(f"upstream template not found at {upstream} (missing 04-deploy.sh)")
    release.mkdir(parents=True, exist_ok=True)
    for entry in sorted(upstream.iterdir()):
        if entry.name in EXCLUDED_UPSTREAM:
            continue
        target = release / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copyfile(entry, target)
    for rel in REMOVED_UPSTREAM_FILES:
        path = release / rel
        if path.exists():
            path.unlink()


def _apply(text: str, pattern: str, replacement: str | Callable[[re.Match[str]], str], *, expect: int, flags: int, name: str, file: str, records: list[PatchRecord]) -> str:
    matches = list(re.finditer(pattern, text, flags))
    if len(matches) != expect:
        raise RenderError(f"{file}: anchor '{name}' matched {len(matches)} times, expected {expect}")
    records.append(PatchRecord(file=file, name=name, matches=len(matches)))
    return re.sub(pattern, replacement, text, count=expect, flags=flags)


# ---------------------------------------------------------------------------
# Generated files
# ---------------------------------------------------------------------------


def render_skills_script(data: dict[str, Any]) -> str:
    skills = " ".join(s["name"] for s in data.get("skills", []))
    return f"""#!/bin/bash
# =============================================================================
# Phase 2c: Configure Skills — generated by workshop-customizer for pack '{data['id']}'
# Skills come from pack/skills/<name>/SKILL.md; they are uploaded to S3 and mounted
# into the Harness via BYO Filesystem (configured in 04-deploy.sh).
# =============================================================================
set -e
REGION=${{AWS_DEFAULT_REGION:-us-west-2}}
SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
PACK_SKILLS="$SCRIPT_DIR/pack/skills"
SKILLS="{skills}"

echo "========================================="
echo "Phase 2c: Configure Skills (pack: {data['id']})"
echo "========================================="

echo "📝 Staging Skill files from the pack..."
for NAME in $SKILLS; do
  mkdir -p ~/workshop/skills/$NAME
  cp "$PACK_SKILLS/$NAME/SKILL.md" ~/workshop/skills/$NAME/SKILL.md
  echo "   $NAME"
done

echo "📤 Uploading Skills to S3..."
SKILLS_BUCKET=$(aws cloudformation describe-stacks \\
  --stack-name workshop-infra \\
  --query 'Stacks[0].Outputs[?OutputKey==`SkillsBucketName` || OutputKey==`DataBucketName`].OutputValue | [0]' \\
  --output text --region $REGION 2>/dev/null || echo "")

if [ -n "$SKILLS_BUCKET" ] && [ "$SKILLS_BUCKET" != "None" ]; then
  for NAME in $SKILLS; do
    aws s3 cp ~/workshop/skills/$NAME/SKILL.md s3://$SKILLS_BUCKET/skills/$NAME/SKILL.md
  done
  echo "  ✅ Uploaded to: s3://$SKILLS_BUCKET/skills/"
else
  echo "❌ No S3 bucket found (workshop-infra stack not deployed?)"
  exit 1
fi

echo ""
echo "✅ Skills configured"
echo "   Next: Run 04-deploy.sh"
"""


def _golden_arrays(facts: ScriptFacts) -> tuple[str, str]:
    queries = "GOLDEN_QUERIES=(\n" + "".join(f"  {bash_quote(q)}\n" for q in facts.golden_queries) + ")\n"
    labels = "GOLDEN_LABELS=(" + " ".join(bash_quote(label) for label in facts.golden_labels) + ")\n"
    return queries, labels


def _case_arrays(facts: ScriptFacts) -> str:
    """GOLDEN_IDS / GOLDEN_ACTORS / GOLDEN_PROBE, parallel to GOLDEN_QUERIES (eval order)."""
    return (
        "GOLDEN_IDS=(" + " ".join(bash_quote(v) for v in facts.golden_ids) + ")\n"
        + "GOLDEN_ACTORS=(" + " ".join(bash_quote(v) for v in facts.golden_actors) + ")\n"
        + "GOLDEN_PROBE=(" + " ".join(str(v) for v in facts.golden_probe) + ")\n"
    )


def _clip(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bullets(items: Any) -> str:
    return "".join(f"- {_clip(item)}\n" for item in list(items or [])[:12])


def mtg_judge_system_prompt(data: dict[str, Any]) -> str:
    """The Mind the Goal system prompt for this scenario (SPEC D5).

    Domain wording from agent.audience/purpose plus a bounded scenario policy (scope, out of scope,
    handoff conditions, prohibited behaviors) so a refusal or handoff the policy requires is judged a
    success. Over MTG_PROMPT_LIMIT characters, out-of-scope then scope bullets are dropped from the
    end; handoff conditions and prohibited behaviors are only clipped, never dropped.
    """
    agent = data["agent"]
    scope = list(agent.get("scope") or [])
    out_of_scope = list(agent.get("outOfScope") or [])

    def build() -> str:
        return (
            "You are a helpful AI assistant. You will act as a judge to evaluate quality of "
            f"{_clip(data['displayName'], 120)}, a chatbot for {_clip(agent['audience'])}.\n"
            f"Its purpose: {_clip(agent['purpose'])}\n"
            "Scenario policy (the assistant was instructed to follow it; use it to judge refusals and handoffs):\n"
            "In scope:\n" + _bullets(scope)
            + ("Out of scope (a polite decline or redirect is the correct behavior):\n" + _bullets(out_of_scope) if out_of_scope else "")
            + "The assistant must hand off or escalate when:\n" + _bullets(agent.get("handoffConditions"))
            + "The assistant must never:\n" + _bullets(agent.get("prohibitedBehaviors"))
            + "Judging rules for this scenario:\n"
            "- A refusal, decline or handoff that this policy requires is quality=success with rcof=null; it is not E2 and not E7.\n"
            "- Use E2 only for a refusal the policy does not require (the request was in scope and permitted).\n"
            '- If the assistant performs, promises or helps with anything under "must never", or skips a required handoff, '
            "the turn is quality=failure with rcof=E6."
        )

    text = build()
    while len(text) > MTG_PROMPT_LIMIT and out_of_scope:
        out_of_scope.pop()
        text = build()
    while len(text) > MTG_PROMPT_LIMIT and len(scope) > 1:
        scope.pop()
        text = build()
    return text


def mtg_user_label(data: dict[str, Any]) -> str:
    """The dialog label of the user's turns: evaluation.judge.userLabel, the only role, else 'User'."""
    label = ((data.get("evaluation") or {}).get("judge") or {}).get("userLabel")
    if label:
        return _clip(label, 40)
    roles = (data.get("agent") or {}).get("roles") or []
    if len(roles) == 1 and roles[0].get("name"):
        return _clip(roles[0]["name"], 40)
    return "User"


def patch_mtg_prompts(text: str, data: dict[str, Any], records: list[PatchRecord]) -> str:
    """The five anchored patches on the Mind the Goal prompt module (SPEC D5).

    Domain wording, the pack's user label and a bounded scenario policy, so a refusal or handoff the
    policy requires is judged a success. The prompt text lives only in repr() literals passed to
    EVALUATION_TEMPLATE.format as a value, so braces, quotes and backslashes in scenario text stay
    inert; the output format, the RCOF code set, parsing and the GSR logic are unchanged.
    """
    judge_prompt, user_label = mtg_judge_system_prompt(data), mtg_user_label(data)
    for pattern, replacement, name, flags in (
        (r'^EVALUATION_SYSTEM_PROMPT = "You are a helpful AI assistant\. You will act as a judge to evaluate quality of employee experience chatbot\."$',
         f"EVALUATION_SYSTEM_PROMPT = {judge_prompt!r}\nUSER_ROLE_LABEL = {user_label!r}", "mtg-judge-scenario-policy", re.M),
        (r"^You are provided with a dialog from an employee chatbot\.$",
         "You are provided with a dialog between a user and the assistant described above.", "mtg-dialog-source", re.M),
        (r"^  E2 Refusal to Answer - unwarranted refusal$",
         "  E2 Refusal to Answer - unwarranted refusal (a refusal or handoff the scenario policy requires is success, not E2)",
         "mtg-rcof-e2-policy", re.M),
        (r"^  E6 Incorrect Routing - wrong domain/department$",
         "  E6 Incorrect Routing - wrong domain/department, a required handoff skipped, or a prohibited action performed",
         "mtg-rcof-e6-policy", re.M),
        (re.escape("lines.append(f\"  Employee: {t['user_message']}\")"),
         "lines.append(f\"  {USER_ROLE_LABEL}: {t['user_message']}\")", "mtg-user-label", 0),
    ):
        text = _apply(text, pattern, lambda _m, r=replacement: r, expect=1, flags=flags, name=name, file=MTG_PROMPTS_REL, records=records)
    return text


def _case_labels(data: dict[str, Any], ids: list[str]) -> str:
    by_id = {c["id"]: c for c in data["evaluation"]["goldenSet"]}
    return " / ".join(str(by_id[cid]["label"]) for cid in ids if cid in by_id)


def optimize_closing_lines(data: dict[str, Any], facts: ScriptFacts) -> list[str]:
    """10's closing reading of the contrast, from labs.teaching and per retrieval-gap mechanism (SPEC D2/D6e).

    A buried gap declared next to an absent one reads as advisory (the guide's ``TeachingView.buried_advisory``).
    """
    probes = set(facts.probe_case_ids)

    def ids(kind: str, mechanism: str | None = None) -> list[str]:
        out: list[str] = []
        for p in teaching.phenomena(data, kind):
            if mechanism is not None and p.get("mechanism") != mechanism:
                continue
            out += [cid for cid in p.get("caseIds") or [] if cid in probes and cid not in out]
        return out

    fixable, buried, absent = ids("prompt_fixable"), ids("retrieval_gap", "buried"), ids("retrieval_gap", "absent")
    zh = data.get("language") == "zh-CN"
    lines = ["对照 09 基线的输出解读（上方 L1 表列出每个 practice 问题）：" if zh
             else "How to read this against the 09 baseline (the L1 table above lists every practice question):"]
    if fixable:
        lines.append(f"  - 检索质量好：{_case_labels(data, fixable)} → GR / SP2 / RP 应明显提升（Prompt 优化奏效）" if zh
                     else f"  - Retrieval is good: {_case_labels(data, fixable)} → GR / SP2 / RP should rise: the prompt fix works")
    if buried and absent:  # the absent gap carries the contrast; a rephrased retrieval can find a buried answer
        lines.append(f"  - 答案被埋没：{_case_labels(data, buried)} → 检索常常失效（SP2≈0），但换关键词再检索可能找到答案、被判 Pass（仅供参考，可靠的对比看下一条）" if zh
                     else f"  - Buried answer: {_case_labels(data, buried)} → retrieval often fails (SP2≈0), but a rephrased retrieval may find the answer and pass: advisory, the next line is the reliable contrast")
    elif buried:
        lines.append(f"  - 检索失效（SP2≈0）：{_case_labels(data, buried)} → 仍 Fail（改 Prompt 救不了，根因在检索 / 知识库）" if zh
                     else f"  - Retrieval fails (SP2≈0): {_case_labels(data, buried)} → stays Fail: a prompt change cannot fix it; fix retrieval / the knowledge base")
    if absent:
        lines.append(f"  - 知识库里没有答案：{_case_labels(data, absent)} → 检索什么也找不到；优化后的 Agent 承认没有并转交处理（Prompt 修好的是诚实，不是覆盖面）" if zh
                     else f"  - The knowledge base lacks the answer: {_case_labels(data, absent)} → retrieval finds nothing; the optimized agent admits it and hands off: the prompt fixed honesty, not coverage")
    return lines


def _pack_sources_text(pack: CompiledPack) -> str:
    """The pack's own text (scenario file + compiled student pack) for the script-residue gate."""
    parts = [pack.scenario.path.read_text(encoding="utf-8", errors="replace")] if pack.scenario.path.is_file() else []
    for path in iter_text_files(pack.pack_dir):
        if path.relative_to(pack.pack_dir).parts[:1] == ("labs",):
            continue  # the generated guide is not a pack source (it must not legitimize residue)
        parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Patches (anchors are written against the pinned upstream commit)
# ---------------------------------------------------------------------------


#: 09's per-case invoke: fresh runtime actor, a sessions.tsv row written BEFORE the invoke, output teed.
_PER_CASE_INVOKE_09 = r'''    local ACTOR="${GOLDEN_ACTORS[$i]}-${RUN_TAG}-q$((i+1))"
    echo "    case: ${GOLDEN_IDS[$i]}   persona: ${GOLDEN_ACTORS[$i]}   fresh memory actor: $ACTOR"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$((i+1))" "${GOLDEN_IDS[$i]}" "$SID" "$ACTOR" "$(( $(date +%s) * 1000 ))" "${GOLDEN_PROBE[$i]}" >> "$GOLDEN_SESSIONS_FILE"
    npx agentcore invoke --session-id "$SID" --actor-id "$ACTOR" --stream "$Q" \
      2>&1 | grep -vE '@@NOISE@@' | tee "$EVAL_RUN_DIR/q$((i+1)).out" || true'''
#: 10's loop body is top-level (no `local`) and writes into the exported optimized run dir.
_PER_CASE_INVOKE_10 = r'''  ACTOR="${GOLDEN_ACTORS[$i]}-${RUN_TAG}-q$((i+1))"
  echo "    case: ${GOLDEN_IDS[$i]}   persona: ${GOLDEN_ACTORS[$i]}   fresh memory actor: $ACTOR"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$((i+1))" "${GOLDEN_IDS[$i]}" "$SID" "$ACTOR" "$(( $(date +%s) * 1000 ))" "${GOLDEN_PROBE[$i]}" >> "$WORKSHOP_EVAL_RUN_DIR/sessions.tsv"
  npx agentcore invoke --session-id "$SID" --actor-id "$ACTOR" --stream "$Q" \
    2>&1 | grep -vE '@@NOISE@@' | tee "$WORKSHOP_EVAL_RUN_DIR/q$((i+1)).out" || true'''


def _patch_eval_runtime(edit: Callable[[str, Callable[[str], str]], None], records: list[PatchRecord],
                        data: dict[str, Any], facts: ScriptFacts) -> None:
    """The evaluation runtime of 09/10/11/12/13/99: the ONE owner of these patches (SPEC D3/D6d/D6e).

    * 09/10 ask the practice cases in eval order (non-probes, probes, stability case last); case i is
      asked as the fresh runtime actor ``${GOLDEN_ACTORS[i]}-${RUN_TAG}-q<i+1>`` computed in bash
      (RUN_TAG = <baseline|optimized|comparison>-<epoch>), so no answer reuses Memory from another
      question or an earlier run.
    * Each run writes its record under ``${WORKSHOP_ROOT:-$HOME/workshop}/eval-runs/<agent>/<RUN_TAG>/``:
      sessions.tsv (index, caseId, sessionId, actor, startedMs, probe), q<i>.out, scores.tsv, l1.json.
    * SINCE_EPOCH_MS is the run start and is never reset. The RAG-judge trace selection and both
      index waits (09 and 10) count the retrieval traces of this run's probe sessions only (one per
      session); want = N = --eval-only = RECENT_N (09, 11) = |P|.
    * The goal judge scores every practice session; its failures stay fatal under
      WORKSHOP_NONINTERACTIVE=1 (the SSM runner) and are counted otherwise. Score lines end with
      `` case=<id>``; L1 (l1_eval.py) runs after the judges.
    * Evaluator, runtime and harness lookups match ``<agentName>_`` exactly (agent names have no
      underscore), so another deployment in the account is never scored or deleted.
    """
    n = facts.probe_count
    total = len(facts.eval_cases)
    queries, labels = _golden_arrays(facts)
    arrays = _case_arrays(facts)

    def one(file: str, pattern: str, replacement: str | Callable[[re.Match[str]], str], name: str, *, flags: int = 0, expect: int = 1) -> None:
        repl = replacement if callable(replacement) else (lambda _m, r=replacement: r)
        edit(file, lambda t: _apply(t, pattern, repl, expect=expect, flags=flags, name=name, file=file, records=records))

    def literal(file: str, old: str, new: str, name: str, *, expect: int = 1) -> None:
        one(file, re.escape(old), new, name, expect=expect)

    def golden(file: str) -> None:
        one(file, r"^GOLDEN_QUERIES=\(\n.*?^\)\n", queries, "golden-queries", flags=re.M | re.S)
        one(file, r"^GOLDEN_LABELS=\(.*\)\n", labels, "golden-labels", flags=re.M)
        one(file, r"^GOLDEN_LABELS=\(.*\)\n", lambda m: m.group(0) + arrays, "golden-case-arrays", flags=re.M)

    s09, s10 = "09-run-eval.sh", "10-optimize-prompt.sh"

    # ---- 09: arrays, run context, counts --------------------------------------------------------
    one(s09, r"^# content 063_run_eval 中的三个 golden 问题.*$",
        "# The scenario's practice golden questions in eval order: non-probe cases first, then the\n"
        "# retrieval probes (labs.teaching), the stability case last. GOLDEN_IDS / GOLDEN_ACTORS /\n"
        "# GOLDEN_PROBE are parallel to GOLDEN_QUERIES.", "golden-order-comment", flags=re.M)
    golden(s09)
    one(s09, r'^ACTOR_ID="employee-001"$', r'''SCRIPT_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Run record (never inside the release): $EVAL_ROOT/<RUN_TAG>/ holds sessions.tsv, q<i>.out,
# scores.tsv and l1.json. RUN_TAG = <phase>-<epoch>; every question is asked as its own fresh
# actor <persona>-<RUN_TAG>-q<i>, so no answer reuses Memory from another question or run.
EVAL_PHASE="${WORKSHOP_EVAL_PHASE:-baseline}"
case "$EVAL_PHASE" in baseline|optimized|comparison) ;; *) EVAL_PHASE=baseline ;; esac
EVAL_ROOT="${WORKSHOP_ROOT:-$HOME/workshop}/eval-runs/hrassistant"
EVAL_RUN_DIR="${WORKSHOP_EVAL_RUN_DIR:-}"
RUN_TAG=""
GOLDEN_SESSIONS_FILE=""
EVAL_SCORES_FILE=""
if [ -n "$EVAL_RUN_DIR" ] && [ -s "$EVAL_RUN_DIR/sessions.tsv" ]; then
  RUN_TAG="$(basename "$EVAL_RUN_DIR")"
  GOLDEN_SESSIONS_FILE="$EVAL_RUN_DIR/sessions.tsv"
  EVAL_SCORES_FILE="$EVAL_RUN_DIR/scores.tsv"
fi
export GOLDEN_SESSIONS_FILE EVAL_SCORES_FILE''', "eval-run-context", flags=re.M)
    one(s09, r"^RECENT_N=3(?=\s)", f"RECENT_N={n}", "recent-n", flags=re.M)
    # Upstream caches the session → {query, response} map in one fixed /tmp file, which two runs on one
    # host (or two test runs) overwrite under each other; every 09 process gets its own, removed on exit.
    one(s09, r'^SESSION_IO_JSON="/tmp/agentcore-session-io\.json"(?P<rest>.*)$',
        lambda m: 'SESSION_IO_JSON="${TMPDIR:-/tmp}/agentcore-session-io-$$.json"' + m.group("rest")
        + "\ntrap 'rm -f \"$SESSION_IO_JSON\"' EXIT",
        "session-io-per-process", flags=re.M)
    one(s09, r"^run_golden_conversations\(\) \{$", r'''run_golden_conversations() {
  SINCE_EPOCH_MS=$(( $(date +%s) * 1000 ))
  export SINCE_EPOCH_MS
  RUN_TAG="$EVAL_PHASE-$(( SINCE_EPOCH_MS / 1000 ))"
  EVAL_RUN_DIR="$EVAL_ROOT/$RUN_TAG"
  mkdir -p "$EVAL_RUN_DIR"
  : > "$EVAL_RUN_DIR/sessions.tsv"
  : > "$EVAL_RUN_DIR/scores.tsv"
  GOLDEN_SESSIONS_FILE="$EVAL_RUN_DIR/sessions.tsv"
  EVAL_SCORES_FILE="$EVAL_RUN_DIR/scores.tsv"
  export GOLDEN_SESSIONS_FILE EVAL_SCORES_FILE
  echo ""
  echo "📁 Run record: $EVAL_RUN_DIR"''', "fresh-baseline-traces", flags=re.M)
    literal(s09, 'echo "🗣️  运行三个 golden 问题（绩效 / 福利 / 病假）..."',
            'echo "🗣️  运行 ${#GOLDEN_QUERIES[@]} 个 practice golden 问题（先问非检索 case，最后 '
            f'{n} 个是检索 probe；每个问题一个新的 memory actor）..."', "golden-count-message")
    one(s09, r'^    npx agentcore invoke --session-id "\$SID" --actor-id "\$ACTOR_ID" --stream "\$Q" \\\n'
             rf"      2>&1 \| grep -vE '{_INVOKE_NOISE}' \|\| true$",
        lambda m: _PER_CASE_INVOKE_09.replace("@@NOISE@@", m.group("noise")),
        "per-case-actor-session-log", flags=re.M)
    literal(s09, 'local want="${#GOLDEN_QUERIES[@]}"', f'local want="{n}"', "expected-retrieval-count")
    one(s09, r"^    run_golden_conversations$", "    run_golden_conversations\n    build_session_io_map",
        "refresh-session-io-after-conversations", flags=re.M)
    literal(s09, "N=${#GOLDEN_QUERIES[@]}", f"N={n}", "evaluate-all-retrieval-cases")

    # ---- 09: explicit-trace mode and the evaluation retries -------------------------------------
    literal(s09, 'run_eval "THELMA (RAG quality) — trace $1" "$THELMA_ARN" --trace-id "$1"',
            'SID=$(python3 "$SCRIPT_HOME/trace_session.py" "$1")\n'
            '    run_eval "THELMA (RAG quality) — trace $1" "$THELMA_ARN" --session-id "$SID" --trace-id "$1"',
            "guide-trace-only-evaluation")
    literal(s09, '    if echo "$out" | grep -q \'"success":true\'; then break; fi', '''    if echo "$out" | grep -q '"success":true'; then
      if echo "$out" | python3 "$SCRIPT_HOME/evaluation_ready.py"; then break; fi
      if [ "$attempt" -lt "$attempts" ]; then
        echo "    (span evidence incomplete, retry in ${pause}s ${attempt}/${attempts})"
        sleep "$pause"
        continue
      fi
    fi''', "wait-for-usable-evaluation-evidence")
    # Live 2026-09-27: the newest trace of a run was still invisible to the evaluation service after
    # 5 x 25 s. The budget is configurable and defaults to 10 x 30 s.
    literal(s09, "  for attempt in 1 2 3 4 5; do",
            f'  local attempts="${{WORKSHOP_EVAL_ATTEMPTS:-{script_facts.EVAL_ATTEMPTS}}}" '
            f'pause="${{WORKSHOP_EVAL_RETRY_SECONDS:-{script_facts.EVAL_RETRY_SECONDS}}}"\n'
            '  for attempt in $(seq 1 "$attempts"); do', "evaluation-retry-budget")
    literal(s09, '      [ $attempt -lt 5 ] && { echo "    (spans not indexed yet, retry in 25s ${attempt}/5)"; sleep 25; continue; }',
            '      [ "$attempt" -lt "$attempts" ] && { echo "    (spans not indexed yet, retry in ${pause}s ${attempt}/${attempts})"; sleep "$pause"; continue; }',
            "evaluation-retry-budget-index")
    for old, new, name in (
        ("print('  (无结果或解析失败)'); sys.exit()", "print('  (无结果或解析失败)'); sys.exit(1)", "reject-empty-evaluation"),
        ("print('  ERROR:', d.get('error')); sys.exit()", "print('  ERROR:', d.get('error')); sys.exit(1)", "reject-evaluation-error"),
        ("for s in d['run']['results'][0]['sessionScores']:",
         "usable = 0\nfor s in d['run']['results'][0]['sessionScores']:", "count-usable-scores"),
        ("    if lbl in ('Skipped',): continue",
         "    if lbl in ('Skipped',): continue\n"
         "    if not isinstance(s.get('value'), (int, float)) or str(lbl).lower() == 'error':\n"
         "        print('  ERROR: evaluator did not return a usable score'); sys.exit(1)\n"
         "    usable += 1", "reject-invalid-score"),
        ('    if s.get(\'explanation\'): print(f\\"     {s[\'explanation\']}\\")\n"',
         '    if s.get(\'explanation\'): print(f\\"     {s[\'explanation\']}\\")\n'
         'if not usable:\n    print(\'  ERROR: no usable evaluation scores\'); sys.exit(1)\n"', "reject-all-skipped"),
    ):
        literal(s09, old, new, name)

    # ---- 09: case-tagged scores + the run's scores.tsv ------------------------------------------
    one(s09, r'^  echo "\$out" \| python3 -c "$', '  echo "$out" | EVAL_LABEL="$label" EVAL_ARGS="$*" python3 -c "',
        "case-tagged-scores-env", flags=re.M)
    literal(s09, "import sys, json, os\nTRUNC = $RESPONSE_TRUNCATE\n", r'''import sys, json, os
TRUNC = $RESPONSE_TRUNCATE
import re
CASES = {}
_sf = os.environ.get('GOLDEN_SESSIONS_FILE') or ''
if _sf and os.path.isfile(_sf):
    for _l in open(_sf, encoding='utf-8'):
        _p = _l.rstrip('\n').split('\t')
        if len(_p) >= 3: CASES[_p[2]] = _p[1]
_a = os.environ.get('EVAL_ARGS', '').split()
REQ_SID = _a[_a.index('--session-id') + 1] if '--session-id' in _a[:-1] else ''
SCORES_FILE = os.environ.get('EVAL_SCORES_FILE') or ''
EV = 'thelma_rag_quality' if 'THELMA' in os.environ.get('EVAL_LABEL', '') else 'mtg_goal_success'
''', "case-tagged-scores-load")
    literal(s09, "    print(f\\\"  Score:    trace={s.get('traceId','?')[:16]} value={s.get('value')} [{lbl}]\\\")", r'''    _case = CASES.get(sid) or CASES.get(REQ_SID)
    print(f\"  Score:    trace={s.get('traceId','?')[:16]} value={s.get('value')} [{lbl}]\" + (f' case={_case}' if _case else ''))
    if SCORES_FILE and (sid or REQ_SID):
        _m = dict(re.findall(r'(GR|SP1|SP2|SQC|RP|RQC|SD)\([^)]*\)=([0-9.]+)', str(s.get('explanation') or '')))
        with open(SCORES_FILE, 'a', encoding='utf-8') as _fh:
            _fh.write('\t'.join([EV, sid or REQ_SID, str(s.get('traceId') or ''), str(s.get('value')), str(lbl), json.dumps(_m, sort_keys=True), _case or '']) + '\n')''',
            "case-tagged-scores-print")

    # ---- 09: the RAG judge sees only this run's probe sessions, one trace each -----------------
    literal(s09, "import sys, json\nseen = {}\n", r'''import sys, json, os
_sf = os.environ.get('GOLDEN_SESSIONS_FILE') or ''
SCOPED = bool(_sf and os.path.isfile(_sf))
PROBES = set()
if SCOPED:
    for _l in open(_sf, encoding='utf-8'):
        _p = _l.rstrip('\n').split('\t')
        if len(_p) >= 6 and _p[5] == '1': PROBES.add(_p[2])
seen = {}
''', "probe-sessions-only-scope")
    literal(s09, "    sid = (d.get('attributes') or {}).get('session.id') or d.get('session_id') or ''\n    ts  = ",
            "    sid = (d.get('attributes') or {}).get('session.id') or d.get('session_id') or ''\n"
            "    if SCOPED and sid not in PROBES: continue\n    ts  = ", "probe-sessions-only-filter")
    literal(s09, "for tid, (ts, sid) in sorted(seen.items(), key=lambda x: x[1][0], reverse=True)[:$n]:\n", '''rows = sorted(seen.items(), key=lambda x: x[1][0], reverse=True)
if SCOPED:
    latest = {}
    for tid, (ts, sid) in rows:
        latest.setdefault(sid, (tid, ts))
    rows = sorted(((tid, (ts, sid)) for sid, (tid, ts) in latest.items()), key=lambda x: x[1][0], reverse=True)
for tid, (ts, sid) in rows[:$n]:
''', "one-trace-per-probe-session")
    literal(s09, '''  if [ -z "$ROWS" ]; then
    if [ -n "$SINCE_EPOCH_MS" ]; then
      echo "  ❌ 严格时间下界 ($SINCE_EPOCH_MS) 之后未找到任何含检索 trace。"
      echo "     说明本次 3 个对话全失败（如 ToolUse / ConverseStream 报错），无 trace 可评。"
      echo "     这本身就是评估结果——"该模型/配置与当前 Agent 拓扑不兼容"。"
      echo "     检查上面 invoke 输出找具体原因。退出，不退化拿旧 trace。"
      exit 1
    fi
    echo "  ⚠️  最近 ${LOOKBACK_SECONDS}s 内未找到含检索的 trace。"
    echo "      若用了 --eval-only，请先不带参数运行本脚本（会自动跑 3 个对话）。"
    exit 0
  fi
''', '''  if [ -z "$ROWS" ]; then
    if [ -n "$SINCE_EPOCH_MS" ]; then
      echo "  ❌ 严格时间下界 ($SINCE_EPOCH_MS) 之后未找到任何含检索 trace。"
      echo "     说明本次检索 probe 对话全失败（如 ToolUse / ConverseStream 报错），无 trace 可评。"
      echo "     这本身就是评估结果——"该模型/配置与当前 Agent 拓扑不兼容"。"
      echo "     检查上面 invoke 输出找具体原因。不退化拿旧 trace。"
      if [ ! -s "${GOLDEN_SESSIONS_FILE:-}" ]; then exit 1; fi
      echo "     (L1 and the goal judge still score every practice question of this run; the script then exits 1)"
      RETRIEVAL_MISSING=1
    else
      echo "  ⚠️  最近 ${LOOKBACK_SECONDS}s 内未找到含检索的 trace。"
      echo "      若用了 --eval-only，请先不带参数运行本脚本（会自动跑全部 practice 问题）。"
      exit 0
    fi
  fi
''', "no-retrieval-still-scores-l1")

    # ---- 09: the goal judge on every practice session; L1 after the judges -----------------------
    literal(s09, '''  SESSIONS=$(for row in $ROWS; do sid="${row#*|}"; [ -n "$sid" ] && echo "$sid"; done | awk '!seen[$0]++')''',
            '''  SESSIONS=$( { if [ -s "${GOLDEN_SESSIONS_FILE:-}" ]; then cut -f3 "$GOLDEN_SESSIONS_FILE"; fi; '''
            '''for row in $ROWS; do sid="${row#*|}"; [ -n "$sid" ] && echo "$sid"; done; } | awk 'NF && !seen[$0]++')''',
            "mtg-every-practice-session")
    literal(s09, '      run_eval "Mind the Goal — session #$j ${sid:0:24}" "$MTG_ARN" --session-id "$sid"', '''      if ! run_eval "Mind the Goal — session #$j ${sid:0:24}" "$MTG_ARN" --session-id "$sid"; then
        if [ "${WORKSHOP_NONINTERACTIVE:-0}" = "1" ]; then
          echo "  ❌ no usable goal-judge verdict for session ${sid:0:24} (fatal in the guided runner)"
          exit 1
        fi
        MTG_UNAVAILABLE=$(( ${MTG_UNAVAILABLE:-0} + 1 ))
        echo "    ⚠️  no usable goal-judge verdict for this session (counted; not fatal in direct Guide use)"
      fi''', "mtg-fatal-only-noninteractive")
    one(s09, r'^echo ""\necho "✅ Evaluation complete"$', '''if [ -n "${GOLDEN_SESSIONS_FILE:-}" ] && [ -s "$GOLDEN_SESSIONS_FILE" ]; then
  echo ""
  echo "═══ L1 scenario assertions — deterministic checks of every practice question ═══"
  if ! python3 "$SCRIPT_HOME/l1_eval.py" --release-dir "$SCRIPT_HOME" --run-dir "$EVAL_RUN_DIR" \\
      --phase "$EVAL_PHASE" --since-ms "${SINCE_EPOCH_MS:-0}" --region "$REGION" --invoke-log-dir "$INVOKE_LOG_DIR"; then
    echo "  ⚠️  L1 scenario assertions did not complete (see the message above)"
  fi
fi
if [ "${MTG_UNAVAILABLE:-0}" -gt 0 ]; then
  echo "  ⚠️  $MTG_UNAVAILABLE session(s) have no usable goal-judge verdict"
fi
if [ "${RETRIEVAL_MISSING:-0}" = "1" ]; then
  echo "❌ No retrieval trace from this run's probe questions: the RAG judge had nothing to score"
  exit 1
fi
echo ""
echo "✅ Evaluation complete"''', "l1-after-judges", flags=re.M)

    # ---- 09 / 13: lookups scoped to this deployment (SPEC D6d) -----------------------------------
    runtime_old = "--query \"agentRuntimes[?contains(agentRuntimeName,'hrassistant')].agentRuntimeArn\""
    runtime_new = ("--query \"agentRuntimes[?starts_with(agentRuntimeName,'harness_hrassistant_') || "
                   "starts_with(agentRuntimeName,'hrassistant_')].agentRuntimeArn\"")
    literal(s09, runtime_old, runtime_new, "runtime-scoped-lookup")
    literal(s09, "--query \"evaluators[?contains(evaluatorId,'$1')].evaluatorArn\"",
            "--query \"evaluators[?starts_with(evaluatorId,'hrassistant_$1')].evaluatorArn\"", "evaluator-scoped-lookup")
    s13 = "13-judge-stability.sh"
    literal(s13, runtime_old, runtime_new, "runtime-scoped-lookup")
    literal(s13, "--query \"evaluators[?contains(evaluatorId,'thelma_rag_quality')].evaluatorArn\"",
            "--query \"evaluators[?starts_with(evaluatorId,'hrassistant_thelma_rag_quality')].evaluatorArn\"",
            "evaluator-scoped-lookup")
    literal(s13, 'TID="$1"; SID=""', 'TID="$1"; SID=$(python3 "$SCRIPT_DIR/trace_session.py" "$TID")',
            "guide-explicit-trace-stability")

    # ---- 10: the same questions, per-case fresh actors, probe-scoped wait -----------------------
    golden(s10)
    one(s10, r'^ACTOR_ID="user-emp-v2"$',
        "# Run record root (same as 09): this run's record is $EVAL_ROOT/optimized-<epoch>/.\n"
        'EVAL_ROOT="${WORKSHOP_ROOT:-$HOME/workshop}/eval-runs/hrassistant"', "eval-run-context", flags=re.M)
    one(s10, r"^cat > app/hrassistant/system-prompt\.md << 'PROMPT'\n.*?^PROMPT\n",
        'cp "$SCRIPT_DIR/pack/prompts/optimization-candidate.md" app/hrassistant/system-prompt.md\n',
        "candidate-prompt-from-pack", flags=re.M | re.S)
    literal(s10, 'echo "📝 Step 1: 写入优化后的 System Prompt（content 071 原文，含抗幻觉约束）..."',
            'echo "📝 Step 1: 写入优化后的 System Prompt（场景包的 optimization candidate，含抗幻觉约束）..."',
            "candidate-prompt-message")
    literal(s10, 'echo "🗣️  Step 3: 用优化后的 Agent 重新跑三个 golden 问题（v2）..."',
            'echo "🗣️  Step 3: 用优化后的 Agent 重新跑全部 ${#GOLDEN_QUERIES[@]} 个 practice 问题（每个问题一个新的 memory actor）..."',
            "golden-count-message")
    one(s10, r'^for i in "\$\{!GOLDEN_QUERIES\[@\]\}"; do$', '''export SINCE_EPOCH_MS=$(( $(date +%s) * 1000 ))
RUN_TAG="optimized-$(( SINCE_EPOCH_MS / 1000 ))"
export WORKSHOP_EVAL_PHASE=optimized
export WORKSHOP_EVAL_RUN_DIR="$EVAL_ROOT/$RUN_TAG"
mkdir -p "$WORKSHOP_EVAL_RUN_DIR"
: > "$WORKSHOP_EVAL_RUN_DIR/sessions.tsv"
: > "$WORKSHOP_EVAL_RUN_DIR/scores.tsv"
echo "📁 Run record: $WORKSHOP_EVAL_RUN_DIR"
for i in "${!GOLDEN_QUERIES[@]}"; do''', "fresh-optimized-traces", flags=re.M)
    one(s10, r'^  npx agentcore invoke --session-id "\$SID" --actor-id "\$ACTOR_ID" --stream "\$Q" \\\n'
             rf"    2>&1 \| grep -vE '{_INVOKE_NOISE}' \|\| true$",
        lambda m: _PER_CASE_INVOKE_10.replace("@@NOISE@@", m.group("noise")),
        "per-case-actor-session-log", flags=re.M)
    literal(s10, "--start-time $(( ($(date +%s) - 600) * 1000 ))", '--start-time "$SINCE_EPOCH_MS"', "wait-for-current-batch")
    literal(s10, "grep -c 'traceId' 2>/dev/null || echo 0)", r'''GOLDEN_SESSIONS_FILE="$WORKSHOP_EVAL_RUN_DIR/sessions.tsv" python3 -c "
import sys, json, os
probe = set()
for l in open(os.environ['GOLDEN_SESSIONS_FILE'], encoding='utf-8'):
    p = l.rstrip('\n').split('\t')
    if len(p) >= 6 and p[5] == '1': probe.add(p[2])
traced = set()
for line in sys.stdin:
    line = line.strip()
    if not line.startswith('{'): continue
    try: d = json.loads(line)
    except Exception: continue
    sid = (d.get('attributes') or {}).get('session.id') or d.get('session_id') or ''
    if (d.get('traceId') or d.get('trace_id')) and sid in probe: traced.add(sid)
print(len(traced))
" 2>/dev/null || true)''', "probe-trace-count")
    literal(s10, '  echo "     [$((i*10))s] 近 10 分钟含检索 span: $CNT"',
            f'  echo "     [$((i*10))s] 已索引检索 trace 的 probe 问题: $CNT/{n}"', "probe-trace-wait-message")
    literal(s10, '[ "${CNT:-0}" -ge 3 ]', f'[ "${{CNT:-0}}" -ge {n} ]', "wait-for-all-retrieval-cases")
    one(s10, r'^"\$SCRIPT_DIR/09-run-eval.sh" --eval-only 3$', f'"$SCRIPT_DIR/09-run-eval.sh" --eval-only {n}',
        "matching-optimized-sample-count", flags=re.M)
    literal(s10, '''echo "对照 content 072 的对比表解读："
echo "  - 绩效 / 福利（检索质量好）→ GR / SP2 / RP 应明显提升（Prompt 优化奏效）"
echo "  - 病假（SP1=1.0 假象、SP2≈0 检索失效）→ 仍 Fail（改 Prompt 救不了，根因在 KB）"
''', "".join(f"echo {bash_quote(line)}\n" for line in optimize_closing_lines(data, facts)), "optimize-closing-from-teaching")

    # ---- 11 / 12: the same |P| and question counts ---------------------------------------------
    one("11-cost-latency.sh", r"^RECENT_N=3(?=\s)", f"RECENT_N={n}", "recent-n-cost", flags=re.M)
    s12 = "12-compare-models.sh"
    one(s12, r'^"\$SCRIPT_DIR/09-run-eval\.sh"$', 'WORKSHOP_EVAL_PHASE=comparison WORKSHOP_EVAL_RUN_DIR= "$SCRIPT_DIR/09-run-eval.sh"',
        "comparison-eval-phase", flags=re.M)
    literal(s12, 'echo "  本脚本会：在 harness.json 里把模型换成对比模型 → 重新部署 → 重跑 3 个 golden"',
            f'echo "  本脚本会：在 harness.json 里把模型换成对比模型 → 重新部署 → 重跑全部 {total} 个 practice"',
            "comparison-plan-message")
    literal(s12, 'echo "🗣️  Step 2: 用对比模型重跑 3 个 golden 问题并评估..."',
            f'echo "🗣️  Step 2: 用对比模型重跑全部 {total} 个 practice 问题并评估（每个问题一个新的 memory actor）..."',
            "comparison-rerun-message")
    literal(s12, 'echo "（content 063 的表，$BASELINE_MODEL）并排比："',
            'echo "（09-run-eval.sh 基线运行的输出，$BASELINE_MODEL）并排比："', "comparison-reading-reference")

    # ---- 99: delete only this deployment's harness/runtime/KB, and its run records ---------------
    s99 = "99-cleanup.sh"
    literal(s99, "starts_with(harnessName,'hrassistant')", "starts_with(harnessName,'hrassistant_')",
            "cleanup-scoped-harness-lookup", expect=2)
    literal(s99, "starts_with(agentRuntimeName,'harness_hrassistant')", "starts_with(agentRuntimeName,'harness_hrassistant_')",
            "cleanup-scoped-runtime-lookup")
    literal(s99, "knowledgeBaseSummaries[?contains(name,'hr') || contains(name,'workshop')].knowledgeBaseId | [0]",
            "knowledgeBaseSummaries[?name=='hr-knowledge-base'].knowledgeBaseId | [0]", "cleanup-scoped-kb-fallback")
    literal(s99, 'echo "  ✅ Local workshop/hrassistant removed"',
            'rm -rf "${WORKSHOP_ROOT:-$HOME/workshop}/eval-runs/hrassistant"\n'
            'echo "  ✅ Local workshop/hrassistant removed (and its eval-runs records)"', "cleanup-eval-runs")


_PARALLEL_EVAL_HELPERS = """# Workshop Customizer: at most WORKSHOP_EVAL_PARALLEL evaluator calls run at once (default 4; a value that is
# not a positive integer means 1). The RAG-judge and goal-judge loops each run twice: eval_queue starts the
# calls in the background (a call waits in the queue until a slot is free), then eval_result, called in the
# same order, waits for each call and prints its output, so the log keeps the question order and each result
# appears as soon as it and every earlier one have finished. eval_result returns the call's status, so a
# failed call means what it meant before (set -e stops 09 at a failed RAG-judge call). With
# WORKSHOP_EVAL_PARALLEL=1 eval_queue does nothing and eval_result is run_eval: the old sequential behaviour.
# Job files live in a private temp dir removed on exit, a call's scores.tsv rows are added when its output is
# printed, and INT/TERM stop the running calls (with their children) and exit 130.
EVAL_PARALLEL="${WORKSHOP_EVAL_PARALLEL:-4}"
if [[ "$EVAL_PARALLEL" =~ ^[0-9]+$ ]] && [ "$((10#$EVAL_PARALLEL))" -ge 1 ]; then
  EVAL_PARALLEL=$((10#$EVAL_PARALLEL))
else
  echo "  (WORKSHOP_EVAL_PARALLEL=$EVAL_PARALLEL is not a positive integer: the evaluator calls run one at a time)"
  EVAL_PARALLEL=1
fi
EVAL_JOB_DIR=""
EVAL_JOBS_QUEUED=0
EVAL_JOBS_STARTED=0
EVAL_JOBS_PRINTED=0
EVAL_JOB_ARGV=()
EVAL_JOB_FROM=()
EVAL_JOB_ARGC=()
EVAL_JOB_PID=()
trap 'eval_stop || true; rm -f "$SESSION_IO_JSON"; [ -z "$EVAL_JOB_DIR" ] || rm -rf "$EVAL_JOB_DIR"' EXIT
# Queue one run_eval call (arguments as for run_eval) and start it if a slot is free.
eval_queue() {
  [ "$EVAL_PARALLEL" -gt 1 ] || return 0
  if [ -z "$EVAL_JOB_DIR" ]; then EVAL_JOB_DIR=$(mktemp -d "${TMPDIR:-/tmp}/workshop-eval-jobs.XXXXXX"); fi
  trap 'eval_stop || true; exit 130' INT TERM
  EVAL_JOBS_QUEUED=$((EVAL_JOBS_QUEUED + 1))
  EVAL_JOB_FROM[$EVAL_JOBS_QUEUED]=${#EVAL_JOB_ARGV[@]}
  EVAL_JOB_ARGC[$EVAL_JOBS_QUEUED]=$#
  EVAL_JOB_ARGV+=("$@")
  eval_start
}
eval_running() { [ ! -e "$EVAL_JOB_DIR/$1.rc" ] && kill -0 "${EVAL_JOB_PID[$1]}" 2>/dev/null; }
# Start queued calls, in queue order, while fewer than EVAL_PARALLEL run.
eval_start() {
  local k running=0
  for ((k = 1; k <= EVAL_JOBS_STARTED; k++)); do
    if eval_running "$k"; then running=$((running + 1)); fi
  done
  while [ "$running" -lt "$EVAL_PARALLEL" ] && [ "$EVAL_JOBS_STARTED" -lt "$EVAL_JOBS_QUEUED" ]; do
    k=$((EVAL_JOBS_STARTED + 1))
    ( set +e
      if [ -n "$EVAL_SCORES_FILE" ]; then EVAL_SCORES_FILE="$EVAL_JOB_DIR/$k.tsv"; fi
      run_eval "${EVAL_JOB_ARGV[@]:${EVAL_JOB_FROM[$k]}:${EVAL_JOB_ARGC[$k]}}" > "$EVAL_JOB_DIR/$k.out" 2> "$EVAL_JOB_DIR/$k.err"
      echo $? > "$EVAL_JOB_DIR/$k.rc" ) &
    EVAL_JOB_PID[$k]=$!
    EVAL_JOBS_STARTED=$k
    running=$((running + 1))
  done
}
# Print the next queued call's output once it finished and return its status; with nothing queued, run_eval.
eval_result() {
  if [ "$EVAL_JOBS_PRINTED" -ge "$EVAL_JOBS_QUEUED" ]; then run_eval "$@"; return; fi
  local k=$((EVAL_JOBS_PRINTED + 1)) rc
  EVAL_JOBS_PRINTED=$k
  eval_start
  while eval_running "$k"; do
    if [ "$EVAL_JOBS_STARTED" -lt "$EVAL_JOBS_QUEUED" ]; then
      sleep 1  # a finishing call frees a slot for the queue
      eval_start
    else
      wait "${EVAL_JOB_PID[$k]}" 2>/dev/null || true
      break
    fi
  done
  cat "$EVAL_JOB_DIR/$k.out" 2>/dev/null || true
  cat "$EVAL_JOB_DIR/$k.err" >&2 2>/dev/null || true
  if [ -n "$EVAL_SCORES_FILE" ] && [ -f "$EVAL_JOB_DIR/$k.tsv" ]; then cat "$EVAL_JOB_DIR/$k.tsv" >> "$EVAL_SCORES_FILE"; fi
  rc=$(cat "$EVAL_JOB_DIR/$k.rc" 2>/dev/null || true)
  if [ "$EVAL_JOBS_PRINTED" -ge "$EVAL_JOBS_QUEUED" ]; then trap - INT TERM; fi
  return "${rc:-1}"
}
# Drop the queue and stop the calls still running (the jobs started so far and bash's job list, which also
# has a job whose pid was not recorded yet; the eval calls are the only background jobs of 09).
eval_stop() {
  local k pids="" tree="" live
  EVAL_JOBS_QUEUED=$EVAL_JOBS_STARTED
  live=$(jobs -rp)  # a command substitution keeps bash's job table; a pipeline element does not
  for ((k = 1; k <= EVAL_JOBS_STARTED; k++)); do
    if eval_running "$k"; then pids="$pids ${EVAL_JOB_PID[$k]}"; fi
  done
  pids=$( { [ -z "$pids" ] || printf '%s\\n' $pids; [ -z "$live" ] || printf '%s\\n' $live; } | sort -un | tr '\\n' ' ')
  [ -n "$pids" ] || return 0
  # Freeze the jobs and every process they started (npx, sleep, python3) until the set stops growing, so
  # none starts another child in between; then terminate and resume them all. The set only grows, so
  # every frozen process is resumed even if ps or awk fail.
  while [ "$pids" != "$tree" ]; do
    tree="$pids"
    kill -STOP $tree 2>/dev/null || true
    pids=$( { printf '%s\\n' $tree; ps -A -o pid= -o ppid= | awk -v roots="$tree" '
      BEGIN { n = split(roots, r, " "); for (i = 1; i <= n; i++) keep[r[i]] = 1 }
      { pid[NR] = $1; parent[NR] = $2 }
      END {
        do { more = 0; for (i = 1; i <= NR; i++) if (!(pid[i] in keep) && (parent[i] in keep)) { keep[pid[i]] = 1; more = 1 } } while (more)
        for (p in keep) print p
      }'; } | sort -un | tr '\\n' ' ')
  done
  kill -TERM $tree 2>/dev/null || true
  kill -CONT $tree 2>/dev/null || true
}

"""

_THELMA_LOOP_SEQUENTIAL = """    if [ -n "$sid" ]; then
      run_eval "THELMA — trace #$i ${tid:0:16}" "$THELMA_ARN" --session-id "$sid" --trace-id "$tid"
    else
      run_eval "THELMA — trace #$i ${tid:0:16}" "$THELMA_ARN" --trace-id "$tid"
    fi
  done
"""
_THELMA_LOOP_PARALLEL = """    if [ -n "$sid" ]; then
      eval_queue "THELMA — trace #$i ${tid:0:16}" "$THELMA_ARN" --session-id "$sid" --trace-id "$tid"
    else
      eval_queue "THELMA — trace #$i ${tid:0:16}" "$THELMA_ARN" --trace-id "$tid"
    fi
  done
  i=0
  for row in $ROWS; do
    i=$((i+1))
    tid="${row%%|*}"
    sid="${row#*|}"
    if [ -n "$sid" ]; then
      eval_result "THELMA — trace #$i ${tid:0:16}" "$THELMA_ARN" --session-id "$sid" --trace-id "$tid"
    else
      eval_result "THELMA — trace #$i ${tid:0:16}" "$THELMA_ARN" --trace-id "$tid"
    fi
  done
"""
_MTG_LOOP_SEQUENTIAL = """    for sid in $SESSIONS; do
      j=$((j+1))
      if ! run_eval "Mind the Goal — session #$j ${sid:0:24}" "$MTG_ARN" --session-id "$sid"; then
"""
_MTG_LOOP_PARALLEL = """    for sid in $SESSIONS; do
      j=$((j+1))
      eval_queue "Mind the Goal — session #$j ${sid:0:24}" "$MTG_ARN" --session-id "$sid"
    done
    j=0
    for sid in $SESSIONS; do
      j=$((j+1))
      if ! eval_result "Mind the Goal — session #$j ${sid:0:24}" "$MTG_ARN" --session-id "$sid"; then
"""


def _patch_parallel_eval(text: str, records: list[PatchRecord]) -> str:
    """09: run the per-trace RAG judge and the per-session goal judge with bounded concurrency."""
    text = _apply(text, r"^# 从 aws/spans 取最近 N 条", lambda m: _PARALLEL_EVAL_HELPERS + m.group(0),
                  expect=1, flags=re.M, name="parallel-eval-helpers", file="09-run-eval.sh", records=records)
    text = _apply(text, re.escape(_THELMA_LOOP_SEQUENTIAL), lambda _m: _THELMA_LOOP_PARALLEL,
                  expect=1, flags=0, name="parallel-eval-rag-judge", file="09-run-eval.sh", records=records)
    text = _apply(text, re.escape(_MTG_LOOP_SEQUENTIAL), lambda _m: _MTG_LOOP_PARALLEL,
                  expect=1, flags=0, name="parallel-eval-goal-judge", file="09-run-eval.sh", records=records)
    return text


#: The evaluator reads its own index of a trace, which fills in after aws/spans already lists the trace: a score
#: taken right after the last answer can see part of it. Live 2026-10-01 a baseline answer with six invented
#: "industry practices" scored GR 1.0 in 09 and 0.35 / 0.25 / 0.33 when re-scored later, and 13 scored one trace
#: [0.0, 0.611, 0.611]. So 09 (and 10 / 12, which run it) waits until its newest probe question is
#: WORKSHOP_EVAL_SETTLE_SECONDS old before the judges run, and 13 waits the same for its trace.
EVAL_SETTLE_SECONDS = 150
_SETTLE_HELPER = f"""# render: settle-before-eval — wait until the newest retrieval probe of this run is old enough for the
# evaluator to see its whole trace (a score taken earlier can judge part of the answer).
settle_probe_traces() {{
  local need="${{WORKSHOP_EVAL_SETTLE_SECONDS:-{EVAL_SETTLE_SECONDS}}}" newest=0 age
  if [ -n "${{GOLDEN_SESSIONS_FILE:-}}" ] && [ -s "$GOLDEN_SESSIONS_FILE" ]; then
    newest=$(awk -F'\t' '$6 == "1" && $5 + 0 > m {{ m = $5 + 0 }} END {{ printf "%d", m }}' "$GOLDEN_SESSIONS_FILE")
  fi
  [ "${{newest:-0}}" -gt 0 ] 2>/dev/null || return 0
  age=$(( $(date +%s) - newest / 1000 ))
  if [ "$age" -lt "$need" ]; then
    echo "⏳ 等待 $(( need - age ))s，让评估框架看到完整的 trace（最新的检索问题 ${{age}}s 前才问）..."
    sleep $(( need - age ))
  fi
}}

"""
_SETTLE_13 = f"""
# render: settle-before-eval — the evaluator sees the whole trace only some time after aws/spans lists it.
if [ -n "${{TS_NS:-}}" ] && [ "$TS_NS" -gt 0 ] 2>/dev/null; then
  AGE=$(( $(date +%s) - TS_NS / 1000000000 )); NEED="${{WORKSHOP_EVAL_SETTLE_SECONDS:-{EVAL_SETTLE_SECONDS}}}"
  if [ "$AGE" -lt "$NEED" ]; then echo "  ⏳ 等待 $(( NEED - AGE ))s，让评估框架看到完整的 trace..."; sleep $(( NEED - AGE )); fi
fi"""


def _patch_eval_settle(text: str, rel: str, records: list[PatchRecord]) -> str:
    """09 waits before its judges run, 13 before its first score, until the trace is complete for the evaluator."""
    if rel == "09-run-eval.sh":
        text = _apply(text, r"^recent_retrieve_traces\(\) \{", lambda m: _SETTLE_HELPER + m.group(0),
                      expect=1, flags=re.M, name="settle-before-eval-helper", file=rel, records=records)
        return _apply(text, r'^  echo "═══ THELMA \(RAG quality\) — \$N 条 trace ═══"',
                      lambda m: "  settle_probe_traces\n" + m.group(0),
                      expect=1, flags=re.M, name="settle-before-eval", file=rel, records=records)
    text = _apply(text, re.escape("if best: print(f'{best[0]}|{best[1]}')"), lambda _m: "if best: print(f'{best[0]}|{best[1]}|{bts}')",
                  expect=1, flags=0, name="settle-before-eval-trace-time", file=rel, records=records)
    text = _apply(text, re.escape('  TID="${ROW%%|*}"; SID="${ROW#*|}"'),
                  lambda _m: '  TID="${ROW%%|*}"; REST="${ROW#*|}"; SID="${REST%%|*}"; TS_NS="${REST#*|}"',
                  expect=1, flags=0, name="settle-before-eval-row", file=rel, records=records)
    return _apply(text, r'^echo "  目标 trace: \$\{TID:0:16\}  （连打 \$RUNS 次 THELMA）"$', lambda m: m.group(0) + _SETTLE_13,
                  expect=1, flags=re.M, name="settle-before-eval", file=rel, records=records)


#: The AgentCore CLI packs every code-based evaluator into ${TMPDIR:-/tmp}/<evaluator>-<uuid>.zip (about 34 MB)
#: on each deploy and never removes it. /tmp is a 1.9 GB tmpfs on the Workshop instance, so a rehearsal
#: environment filled up after a few Guided Runs (live 2026-09-29: 59 zips, CDK synth failed with ENOSPC).
_PRUNE_CLI_ZIPS = ("find \"${TMPDIR:-/tmp}\" -maxdepth 1 -type f -user \"$(id -u)\" -mmin +5 "
                   "-name '*-????????-????-????-????-????????????.zip' -delete 2>/dev/null || true"
                   "  # AgentCore CLI evaluator zips (render: prune-cli-zips)")
#: 10 and 12 are not here: they update the deployed Harness in place (optimize-prompt-in-place,
#: compare-models-in-place) and never deploy.
_DEPLOY_SCRIPTS = {"04-deploy.sh": 1, "08-create-evaluators.sh": 1}


def _patch_prune_cli_zips(text: str, rel: str, records: list[PatchRecord]) -> str:
    """Before every ``npx agentcore deploy``: remove the CLI's stale evaluator zips from /tmp."""
    return _apply(text, r"^([ \t]*)(npx agentcore deploy )", lambda m: f"{m.group(1)}{_PRUNE_CLI_ZIPS}\n{m.group(1)}{m.group(2)}",
                  expect=_DEPLOY_SCRIPTS[rel], flags=re.M, name="prune-cli-zips", file=rel, records=records)


#: 12's model switch on the deployed Harness itself. harness.json keeps the baseline model, so the CDK project and
#: the Harness agree again as soon as the model is switched back; memory, tools and skills are not touched, so
#: 05-setup-memory.sh does not have to run again (measured 2026-10-01 on this account: update-harness changes the
#: model only, READY after 23-26 s, from the Workshop EC2's instance role with the host's AWS CLI 2.37).
_COMPARE_IN_PLACE_HELPERS = """# Workshop Customizer: the comparison switches the model of the deployed Harness in place (UpdateHarness,
# about 30 s) and switches it back on exit; harness.json keeps the baseline model, so nothing drifts from the
# CDK project, and memory, tools and skills stay as 04/05 set them.
HARNESS_NAME="@AGENT@_@AGENT@"
MODEL_SWITCHED=0
harness_status() {
  aws bedrock-agentcore-control get-harness --region "$REGION" --harness-id "$HARNESS_ID" --query harness.status --output text
}
set_harness_model() {
  local status i
  aws bedrock-agentcore-control update-harness --region "$REGION" --harness-id "$HARNESS_ID" --model "$1" \\
    --query harness.status --output text >/dev/null || return 1
  for ((i = 0; i < 60; i++)); do
    status=$(harness_status) || return 1
    case "$status" in
      READY) return 0 ;;
      *FAILED*) echo "  ❌ Harness $HARNESS_ID: $status"; return 1 ;;
    esac
    sleep 5
  done
  echo "  ❌ Harness $HARNESS_ID not READY after 5 min (status $status)"
  return 1
}

# 退出时（无论成功/失败/中断）把 Harness 的模型切回基线
restore_model() {
  if [ "$MODEL_SWITCHED" = "1" ]; then
    echo ""
    echo "♻️  把 Harness 的模型切回 $BASELINE_MODEL ..."
    set_harness_model "$ORIGINAL_MODEL_JSON" || return 1
    MODEL_SWITCHED=0
    echo "  ✅ 已还原为基线模型"
  fi
}
trap restore_model EXIT

# -----------------------------------------------------------------------------
# Step 1: 在已部署的 Harness 上把模型切换为对比模型（UpdateHarness，不重新部署）
# -----------------------------------------------------------------------------
echo ""
echo "🔧 Step 1: 在已部署的 Harness 上切换模型到 $COMPARE_MODEL（约 30 秒，无需重新部署）..."
HARNESS_ID=$(aws bedrock-agentcore-control list-harnesses --region "$REGION" \\
  --query "harnesses[?harnessName=='$HARNESS_NAME'].harnessId | [0]" --output text)
if [ -z "$HARNESS_ID" ] || [ "$HARNESS_ID" = "None" ]; then
  echo "❌ 找不到 Harness $HARNESS_NAME：请先完成 04-deploy.sh。已中止，未做任何改动。"
  exit 1
fi
ORIGINAL_MODEL_JSON=$(aws bedrock-agentcore-control get-harness --region "$REGION" --harness-id "$HARNESS_ID" \\
  --query harness.model --output json)
COMPARE_MODEL_JSON=$(printf '%s' "$ORIGINAL_MODEL_JSON" | jq -c --arg m "$COMPARE_MODEL" '.bedrockModelConfig.modelId = $m')
MODEL_SWITCHED=1
set_harness_model "$COMPARE_MODEL_JSON"
echo "  ✅ Harness $HARNESS_ID 现在使用 $(aws bedrock-agentcore-control get-harness --region "$REGION" --harness-id "$HARNESS_ID" \\
  --query harness.model.bedrockModelConfig.modelId --output text)"

"""


_OPTIMIZE_IN_PLACE = """# Step 2: 在已部署的 Harness 上就地更新 System Prompt（UpdateHarness，约 30 秒，不重新部署）
# -----------------------------------------------------------------------------
# Workshop Customizer: the prompt is part of the Harness configuration, so 10 updates it on the deployed Harness
# (like the launchpad console) instead of a CDK deploy of about 5 minutes that also re-applied a generic memory
# configuration. Step 1 already wrote the same prompt into the agent project, so a later deploy keeps it.
echo ""
echo "🚀 Step 2: 在已部署的 Harness 上更新 System Prompt（UpdateHarness，约 30 秒，无需重新部署）..."
HARNESS_NAME="@AGENT@_@AGENT@"
HARNESS_ID=$(aws bedrock-agentcore-control list-harnesses --region "$REGION" \\
  --query "harnesses[?harnessName=='$HARNESS_NAME'].harnessId | [0]" --output text)
if [ -z "$HARNESS_ID" ] || [ "$HARNESS_ID" = "None" ]; then
  echo "❌ 找不到 Harness $HARNESS_NAME：请先完成 04-deploy.sh。"
  exit 1
fi
PROMPT_UPDATE=$(mktemp)
jq -Rs --arg id "$HARNESS_ID" '{harnessId: $id, systemPrompt: [{text: .}]}' app/@AGENT@/system-prompt.md > "$PROMPT_UPDATE"
aws bedrock-agentcore-control update-harness --region "$REGION" --cli-input-json "file://$PROMPT_UPDATE" \\
  --query harness.status --output text >/dev/null
rm -f "$PROMPT_UPDATE"
S=UNKNOWN
for i in $(seq 1 60); do
  S=$(aws bedrock-agentcore-control get-harness --region "$REGION" --harness-id "$HARNESS_ID" --query harness.status --output text)
  case "$S" in
    READY) break ;;
    *FAILED*) echo "  ❌ Harness $HARNESS_ID: $S"; exit 1 ;;
  esac
  sleep 5
done
[ "$S" = "READY" ] || { echo "  ❌ Harness $HARNESS_ID 5 分钟内没有就绪（$S）"; exit 1; }
echo "  ✅ Harness $HARNESS_ID 已换上优化后的 System Prompt"

"""


def _patch_optimize_in_place(text: str, records: list[PatchRecord], agent: str) -> str:
    """10: Step 2 updates the deployed Harness's system prompt with update-harness instead of a CDK deploy."""
    text = _apply(text, r"^# Step 2: 重新部署（Prompt 是 Harness 配置的一部分，约 3-5 分钟）\n.*?(?=^# -{77}\n# Step 3: )",
                  lambda _m: _OPTIMIZE_IN_PLACE.replace("@AGENT@", agent),
                  expect=1, flags=re.M | re.S, name="optimize-prompt-in-place", file="10-optimize-prompt.sh", records=records)
    return _apply(text, re.escape("#   2. agentcore deploy 重新部署（Prompt 是 Harness 配置的一部分）"),
                  lambda _m: "#   2. 在已部署的 Harness 上就地更新 System Prompt（UpdateHarness，约 30 秒；Prompt 是 Harness 配置的一部分）",
                  expect=1, flags=0, name="optimize-prompt-in-place-header", file="10-optimize-prompt.sh", records=records)


def _patch_compare_models_in_place(text: str, records: list[PatchRecord], agent: str) -> str:
    """12: the restore trap and Step 1 switch the Harness model with update-harness instead of redeploying."""
    body = _COMPARE_IN_PLACE_HELPERS.replace("@AGENT@", agent)
    # The match ends with the rule line above "# Step 2:", which the replacement puts back.
    text = _apply(text, r"^# 退出时（无论成功/失败/中断）把模型还原回基线并重新部署\n.*?(?=^# Step 2: )", lambda _m: body + "# " + "-" * 77 + "\n",
                  expect=1, flags=re.M | re.S, name="compare-models-in-place", file="12-compare-models.sh", records=records)
    header = ("# 做法：把当前 Harness 的模型【非破坏性地】换成对比模型、重新部署、用同一套\n"
              "# 3 个 golden 问题重跑 + 用同一个 THELMA 评估，再和 Phase 4 基线对比 质量/成本/延迟。\n")
    text = _apply(text, re.escape(header), lambda _m: (
        "# 做法：在已部署的 Harness 上把模型【就地】切换为对比模型（UpdateHarness，约 30 秒，不重新部署），\n"
        "# 用同一套 practice 问题重跑 + 用同样的评估器评估，再和 Phase 4 基线对比 质量/成本/延迟，退出时切回基线模型。\n"),
        expect=1, flags=0, name="compare-models-header", file="12-compare-models.sh", records=records)
    for old, new, name in (
        ("# ⚠️  可选延伸，不在 2 小时主线内。含两次重新部署（换模型 + 还原），约 10-15 分钟。",
         "# ⚠️  可选延伸，不在 2 小时主线内。模型切换与还原各约 30 秒（Workshop Customizer：原版是两次重新部署，约 10-15 分钟），"
         "\n#     整步约 5 分钟。", "compare-models-header-duration"),
        ("#   - `agentcore deploy` 是否能让改后的模型生效。",
         "#   - （Workshop Customizer）换模型不再走 `agentcore deploy`：update-harness 只改模型，harness.json 保持基线模型。",
         "compare-models-header-deploy"),
        ('echo "  本脚本会：在 harness.json 里把模型换成对比模型 → 重新部署 → 重跑全部',
         'echo "  本脚本会：在已部署的 Harness 上把模型切换为对比模型（约 30 秒）→ 重跑全部', "compare-models-plan-in-place"),
        ('问题并评估 → 算成本/延迟 → 自动还原回基线模型。约 10-15 分钟（不删评估器）。"',
         '问题并评估 → 算成本/延迟 → 自动切回基线模型。约 5 分钟（不重新部署，不删评估器）。"', "compare-models-duration"),
        ('echo "✅ 对比数据已产出（退出时模型自动还原为基线）"', 'echo "✅ 对比数据已产出（退出时 Harness 的模型自动切回基线）"',
         "compare-models-done-message"),
    ):
        text = _apply(text, re.escape(old), lambda _m, new=new: new, expect=1, flags=0, name=name,
                      file="12-compare-models.sh", records=records)
    return text


def _patch_scripts(release: Path, data: dict[str, Any], facts: ScriptFacts) -> list[PatchRecord]:
    """Apply every anchored patch. Practice-related values (06/09/10/11/12) come only from ``facts``.

    This is the single owner of the 06/09/10/11/12/13 runtime patches (SPEC D3): eval-order arrays,
    |P| counts, the per-case runtime actor computed in bash, the run record, probe-scoped trace
    selection and waits, case-tagged scores, the judge on every practice session and L1.
    """
    if facts.semantics != "teaching" or facts.actor_mode != "per_case":
        raise RenderError(f"script facts use '{facts.semantics}' semantics; the renderer emits only the teaching runtime")
    if not facts.eval_cases or facts.first_conversation is None:
        raise RenderError("pack has no practice golden cases")
    if facts.probe_count < 1:
        raise RenderError(
            "labs.teaching declares no retrieval probes (|P| = 0: no prompt_fixable or retrieval_gap practice case); "
            "09/10 would wait for no retrieval trace — run Validate"
        )
    records: list[PatchRecord] = []
    skills = list(facts.skills)
    fc = facts.first_conversation

    def edit(rel: str, fn: Callable[[str], str]) -> None:
        path = release / rel
        if not path.is_file():
            raise RenderError(f"expected upstream file missing: {rel}")
        path.write_text(fn(path.read_text(encoding="utf-8")), encoding="utf-8")

    # 01: knowledge documents come from the pack instead of the HR generator. The docs dir starts
    # empty (SPEC D6c): documents of an earlier pack or release on this host are never ingested.
    edit(
        "01-create-kb.sh",
        lambda t: _apply(
            t,
            r'^python3 "\$KB_DIR/generate_hr_docs\.py" "\$DOCS_DIR"\n',
            'find "$DOCS_DIR" -mindepth 1 -delete\n'
            'cp "$SCRIPT_DIR/pack/knowledge-base/docs/"*.md "$DOCS_DIR/"\n',
            expect=1, flags=re.M, name="docs-from-pack", file="01-create-kb.sh", records=records,
        ),
    )
    # create_kb.py: after the copy the KB prefix must hold exactly this pack's documents, so objects
    # a previous pack or release uploaded under it are deleted before the upload + sync (SPEC D6c).
    edit(
        "knowledge-base/create_kb.py",
        lambda t: _apply(
            t, r'^def get_data_bucket_from_cfn\(',
            lambda _m: '''def prune_stale_documents(s3_client, bucket_name, prefix, docs_path):
    """Delete objects under the KB prefix that are not documents of this pack (workshop-customizer).

    01-create-kb.sh empties docs_path and copies only this pack's documents into it, so after this and
    the upload the prefix holds exactly them: an earlier pack's or release's documents are never
    ingested again. The bucket's own layout prefixes (skills, release bundles, multimodal staging)
    are never pruned.
    """
    if not prefix or not prefix.endswith("/") or prefix.strip("/").lower() in ''' + repr(tuple([""] + [p.strip("/") for p in RESERVED_BUCKET_PREFIXES])) + ''':
        raise RuntimeError(f"refusing to prune the knowledge-base prefix {prefix!r}")
    keep = set()
    for _root, _dirs, files in os.walk(docs_path):
        for name in files:
            keep.add(f"{prefix}{name}")
    stale = []
    for page in s3_client.get_paginator("list_objects_v2").paginate(Bucket=bucket_name, Prefix=prefix):
        stale += [obj["Key"] for obj in page.get("Contents", []) if obj["Key"] not in keep]
    for start in range(0, len(stale), 1000):
        s3_client.delete_objects(
            Bucket=bucket_name,
            Delete={"Objects": [{"Key": key} for key in stale[start:start + 1000]], "Quiet": True},
        )
    print(f"  Removed {len(stale)} stale object(s) from s3://{bucket_name}/{prefix}")
    return stale


def get_data_bucket_from_cfn(''',
            expect=1, flags=re.M, name="kb-prune-helper", file="knowledge-base/create_kb.py", records=records,
        ),
    )
    edit(
        "knowledge-base/create_kb.py",
        lambda t: _apply(
            t, re.escape("        kb.upload_directory(\n            DOCS_PATH,\n"),
            lambda _m: "        prune_stale_documents(kb.s3_client, kb.get_data_bucket_name(), INCLUSION_PREFIX, DOCS_PATH)\n"
            "        kb.upload_directory(\n            DOCS_PATH,\n",
            expect=1, flags=0, name="kb-prune-stale-documents", file="knowledge-base/create_kb.py", records=records,
        ),
    )

    # 02: the Lambda zip carries the generic handler plus its fixtures.
    edit(
        "02-create-gateway.sh",
        lambda t: _apply(
            t,
            r"zip -j /tmp/hr-tools-lambda\.zip hr_tools_handler\.py",
            "zip -j /tmp/hr-tools-lambda.zip hr_tools_handler.py fixtures.json",
            expect=1, flags=0, name="lambda-zip-fixtures", file="02-create-gateway.sh", records=records,
        ),
    )
    # 02: a function pre-created by workshop-infra (the hr-default namespace's hr-tools-handler) keeps
    # its upstream handler module; the update branch must point it at the scenario handler, wait for
    # each Lambda update, and fail closed instead of `|| true`.
    edit(
        "02-create-gateway.sh",
        lambda t: _apply(
            t,
            r'  aws lambda update-function-configuration \\\n'
            r'    --function-name "\$FUNCTION_NAME" \\\n'
            r'    --environment "Variables=\{KB_ID_SSM_PARAM=\$KB_ID_SSM_PARAM\}" \\\n'
            r'    --region \$REGION > /dev/null 2>&1 \|\| true\n',
            '  aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region $REGION\n'
            '  aws lambda update-function-configuration \\\n'
            '    --function-name "$FUNCTION_NAME" \\\n'
            '    --handler scenario_tools_handler.lambda_handler \\\n'
            '    --runtime python3.12 \\\n'
            '    --environment "Variables={KB_ID_SSM_PARAM=$KB_ID_SSM_PARAM}" \\\n'
            '    --region $REGION > /dev/null\n'
            '  aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region $REGION\n',
            expect=1, flags=0, name="lambda-update-handler", file="02-create-gateway.sh", records=records,
        ),
    )

    # 04: baseline prompt and skills list come from the pack.
    skills_json = ", ".join(f'\\"/mnt/skills/skills/{name}/SKILL.md\\"' for name in skills)

    def patch_04(t: str) -> str:
        t = _apply(
            t, r" --memory longAndShortTerm", "",
            expect=1, flags=0, name="harness-create-flags", file="04-deploy.sh", records=records,
        )
        t = _apply(
            t, r"^cd hrassistant\n",
            'cd hrassistant\n'
            'npx agentcore add memory --name hrassistantmemory --strategies SEMANTIC,USER_PREFERENCE\n'
            'jq \'.memory = {"mode":"existing","name":"hrassistantmemory","actorId":"{actorId}"} | del(.systemPrompt)\' '
            'app/hrassistant/harness.json > memory-config.json\n'
            'mv memory-config.json app/hrassistant/harness.json\n',
            expect=1, flags=re.M, name="explicit-workshop-memory", file="04-deploy.sh", records=records,
        )
        t = _apply(
            t, r"SKILLS_AP_ARN=\$\(aws cloudformation describe-stacks \\\n.*?--output text --region \$REGION 2>/dev/null \|\| echo \"\"\)",
            lambda _m: 'SKILLS_AP_ARN=$(aws cloudformation describe-stacks \\\n'
            '  --stack-name workshop-customizer-addons \\\n'
            '  --query "Stacks[0].Outputs[?OutputKey==\'SkillsFilesAccessPointArn\'].OutputValue" \\\n'
            '  --output text --region $REGION)',
            expect=1, flags=re.S, name="skills-files-access-point", file="04-deploy.sh", records=records,
        )
        t = _apply(
            t,
            r'\.environment\.agentCoreRuntimeEnvironment\.filesystemConfigurations = \[\{"mountPath": "/mnt/skills", "s3FilesAccessPoint": \{"accessPointArn": \$arn\}\}\]',
            '.s3AccessPoints = [{"mountPath": "/mnt/skills", "accessPointArn": $arn}]',
            expect=1, flags=0, name="harness-filesystem-schema", file="04-deploy.sh", records=records,
        )
        t = _apply(
            t,
            r"^cat > \$WORKDIR/system-prompt\.txt << 'PROMPT'\n.*?^PROMPT\n",
            'cp "$SCRIPT_DIR/pack/prompts/baseline.md" "$WORKDIR/system-prompt.txt"\n',
            expect=1, flags=re.M | re.S, name="baseline-prompt-from-pack", file="04-deploy.sh", records=records,
        )
        t = _apply(
            t,
            r"jq '\.skills = \[[^\]]*\]'",
            lambda _m: "jq '.skills = [" + skills_json.replace('\\"', '"') + "]'",
            expect=1, flags=0, name="skills-list-from-pack", file="04-deploy.sh", records=records,
        )
        return t

    edit("04-deploy.sh", patch_04)

    # Direct Guide commands must receive the same runtime compatibility setup as
    # the guided SSM runner, including ECR access and the aws/spans destination.
    edit(
        "05-setup-memory.sh",
        lambda t: _apply(
            t, r"^cd \$WORKDIR\n",
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
            'RELEASE_VERSION=$(python3 -c \'import json,sys; print(json.load(open(sys.argv[1]))["version"])\' "$SCRIPT_DIR/RELEASE.json")\n'
            'python3 /opt/workshop-customizer/runtime_permissions.py --release-version "$RELEASE_VERSION"\n'
            'cd $WORKDIR\n',
            expect=1, flags=re.M, name="direct-command-runtime-setup", file="05-setup-memory.sh", records=records,
        ),
    )
    edit(
        "06-test-conversation.sh",
        lambda t: _apply(
            t,
            re.escape('"I\'d like to know about the annual leave policy. How many days am I entitled to and what\'s the application process?"'),
            lambda _m: bash_quote(fc.query),
            expect=1, flags=0, name="first-conversation-from-pack", file="06-test-conversation.sh", records=records,
        ),
    )
    # 06 asks the first conversation as its declared actor (fixed: steps 8 and 10 run it twice so
    # Memory learns this user) and announces its topic from labs.teaching.firstConversation.
    edit(
        "06-test-conversation.sh",
        lambda t: _apply(
            t, re.escape('--actor-id "employee-001"'), lambda _m: f"--actor-id {bash_quote(fc.actor_id)}",
            expect=1, flags=0, name="first-conversation-actor", file="06-test-conversation.sh", records=records,
        ),
    )
    edit(
        "06-test-conversation.sh",
        lambda t: _apply(
            t, r'^echo "🗣️  Asking about annual leave policy\.\.\."$',
            lambda _m: "echo " + bash_quote(script_facts.first_conversation_topic(fc)),
            expect=1, flags=re.M, name="first-conversation-topic", file="06-test-conversation.sh", records=records,
        ),
    )
    # HR wording the upstream scripts print or register, left untouched by the patches above; every
    # scenario showed it to participants (step 01/06 console, the Bedrock KB description).
    edit(
        "01-create-kb.sh",
        lambda t: _apply(
            t,
            re.escape('echo "📝 Generating HR policy documents into $DOCS_DIR ..."'),
            lambda _m: 'echo "📝 Copying the scenario\'s knowledge documents into $DOCS_DIR ..."',
            expect=1, flags=0, name="docs-copy-message", file="01-create-kb.sh", records=records,
        ),
    )
    edit(
        "knowledge-base/create_kb.py",
        lambda t: _apply(
            t,
            re.escape('KB_DESCRIPTION = "HR policy KB for AgentCore workshop"'),
            lambda _m: f"KB_DESCRIPTION = {(str(data['id']) + ' knowledge base for AgentCore workshop')[:200]!r}",
            expect=1, flags=0, name="kb-description-from-pack", file="knowledge-base/create_kb.py", records=records,
        ),
    )
    edit(
        "06-test-conversation.sh",
        lambda t: _apply(
            t,
            re.escape(
                'echo "Notice: The answer is GENERIC — the Agent doesn\'t know your tenure,"\n'
                'echo "department, or specific leave balance yet."'
            ),
            lambda _m: "echo " + bash_quote(script_facts.memory_notice(fc)),
            expect=1, flags=0, name="memory-notice-from-pack", file="06-test-conversation.sh", records=records,
        ),
    )
    edit(
        "06-test-conversation.sh",
        lambda t: _apply(
            t,
            re.escape('echo "Phase 3: First Conversation with HR Agent"'),
            lambda _m: 'echo "Phase 3: First Conversation with "' + bash_quote(str(data["displayName"])),
            expect=1, flags=0, name="phase3-title-from-pack", file="06-test-conversation.sh", records=records,
        ),
    )
    for anchor, replacement, name in (
        ('echo "📦 Deploying HR Tools Lambda ($FUNCTION_NAME)..."', 'echo "📦 Deploying the scenario tools Lambda ($FUNCTION_NAME)..."', "gateway-deploy-message"),
        ('echo "🔍 Resolving HR Tools Lambda ARN..."', 'echo "🔍 Resolving the scenario tools Lambda ARN..."', "gateway-resolve-message"),
        ("返回内置示例 HR 政策，无真实检索", "返回内置示例数据，无真实检索", "gateway-mock-notice"),
    ):
        edit(
            "02-create-gateway.sh",
            lambda t, a=anchor, r=replacement, n=name: _apply(
                t, re.escape(a), lambda _m, r=r: r, expect=1, flags=0, name=n, file="02-create-gateway.sh", records=records,
            ),
        )
    edit(
        "gateway/create_gateway.py",
        lambda t: _apply(
            t,
            re.escape('Description="Allows AgentCore Gateway to invoke the HR Tools Lambda"'),
            lambda _m: 'Description="Allows AgentCore Gateway to invoke the scenario tools Lambda"',
            expect=1, flags=0, name="gateway-role-description", file="gateway/create_gateway.py", records=records,
        ),
    )

    # Mind the Goal judges this scenario (SPEC D5).
    edit(MTG_PROMPTS_REL, lambda t: patch_mtg_prompts(t, data, records))
    edit(
        "05-setup-memory.sh",
        lambda t: _apply(
            t, r"if 'runtimeId' in info:\n        print\(info\['runtimeId'\]\)",
            "if 'agentRuntimeArn' in info:\n        print(info['agentRuntimeArn'].rsplit('/', 1)[-1])",
            expect=1, flags=0, name="memory-runtime-status", file="05-setup-memory.sh", records=records,
        ),
    )

    # Migrate an existing target in place; creating a second target would expose
    # both the valid and invalid tool names to the model. Also update changed
    # schemas on a re-run instead of silently keeping the previous schema.
    edit(
        "gateway/create_gateway.py",
        lambda t: _apply(
            t,
            r'    target_id = find_target_id\(gateway_id, args.target_name\)\n    if target_id:\n        print\(f"  Target already exists: \{target_id\} \(skipping\)"\)\n    else:\n        target_config = \{.*?        \}\n        resp = gateway_client.create_gateway_target\(',
            '''    legacy_name = args.target_name
    args.target_name = legacy_name.replace("-", "")
    target_id = find_target_id(gateway_id, args.target_name) or find_target_id(gateway_id, legacy_name)
    target_config = {"mcp": {"lambda": {
        "lambdaArn": args.lambda_arn,
        "toolSchema": {"inlinePayload": tool_schema},
    }}}
    if target_id:
        gateway_client.update_gateway_target(
            gatewayIdentifier=gateway_id, targetId=target_id,
            name=args.target_name, targetConfiguration=target_config,
            credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        )
        print(f"  Target updated: {target_id} ({args.target_name})")
    else:
        resp = gateway_client.create_gateway_target(''',
            expect=1, flags=re.S, name="nova-compatible-target-name", file="gateway/create_gateway.py", records=records,
        ),
    )

    # 09 / 10 / 11 / 12 / 13 / 99: the evaluation runtime (one owner; SPEC D3, D6d, D6e).
    _patch_eval_runtime(edit, records, data, facts)
    # 09: bounded-parallel evaluator calls (live 2026-09-28: 09 took ~7 min, 10 ~9 min, mostly one
    # evaluator call after another). Runs after every other 09 patch; output order is unchanged.
    edit("09-run-eval.sh", lambda t: _patch_parallel_eval(t, records))
    for script in ("09-run-eval.sh", "13-judge-stability.sh"):
        edit(script, lambda t, script=script: _patch_eval_settle(t, script, records))
    for script in _DEPLOY_SCRIPTS:
        edit(script, lambda t, script=script: _patch_prune_cli_zips(t, script, records))

    # A CDK deployment re-applies its generic memory configuration: 08's evaluator deploy restores the Guide's
    # preference/fact retrieval settings (10 and 12 update the Harness in place and never deploy).
    edit(
        "08-create-evaluators.sh",
        lambda t: _apply(
            t, r'^echo "🔐 Granting Bedrock permissions to evaluator roles\.\.\."$',
            '"$SCRIPT_DIR/05-setup-memory.sh"\n'
            'echo "🔐 Granting Bedrock permissions to evaluator roles..."',
            expect=1, flags=re.M, name="preserve-memory-on-evaluator-deploy",
            file="08-create-evaluators.sh", records=records,
        ),
    )

    # The guided SSM runner is noninteractive. The explicit opt-in is supplied only by that
    # allow-listed runner; direct shell users keep the original confirmation prompt.
    edit(
        "12-compare-models.sh",
        lambda t: _apply(
            t,
            r'^read -p "  继续？\(y/N\) " ok\n',
            'if [ "${WORKSHOP_NONINTERACTIVE:-0}" = "1" ]; then\n  ok=y\nelse\n  read -p "  继续？(y/N) " ok\nfi\n',
            expect=1, flags=re.M, name="noninteractive-model-confirmation",
            file="12-compare-models.sh", records=records,
        ),
    )
    edit(
        "12-compare-models.sh",
        lambda t: _apply(
            t, r'^"\$SCRIPT_DIR/11-cost-latency\.sh" \|\| true$',
            'WORKSHOP_MODEL_ID="$COMPARE_MODEL" PRICE_IN="${COMPARE_PRICE_IN:-}" PRICE_OUT="${COMPARE_PRICE_OUT:-}" "$SCRIPT_DIR/11-cost-latency.sh"',
            expect=1, flags=re.M, name="comparison-model-pricing",
            file="12-compare-models.sh", records=records,
        ),
    )
    # Nova: AWS Price List API, US geography, effective 2026-08-01.
    # Haiku: AWS Bedrock pricing page and its regional data, published 2026-09-11.
    # Both verified 2026-09-13. Other models/regions require an explicit override.
    # Estimates exclude cache discounts and infrastructure.
    pricing = """# WORKSHOP_PRICING_BEGIN
if [ -z "${PRICE_IN:-}" ] || [ -z "${PRICE_OUT:-}" ]; then
  case "$REGION:$MODEL_ID" in
    us-west-2:us.amazon.nova-2-lite-v1:0) DEFAULT_PRICE_IN=0.33; DEFAULT_PRICE_OUT=2.75 ;;
    us-west-2:us.amazon.nova-pro-v1:0) DEFAULT_PRICE_IN=0.80; DEFAULT_PRICE_OUT=3.20 ;;
    us-west-2:us.anthropic.claude-haiku-4-5-20251001-v1:0) DEFAULT_PRICE_IN=1.10; DEFAULT_PRICE_OUT=5.50 ;;
    us-west-2:global.anthropic.claude-haiku-4-5-20251001-v1:0) DEFAULT_PRICE_IN=1.00; DEFAULT_PRICE_OUT=5.00 ;;
    *) echo "Set verified PRICE_IN and PRICE_OUT for $REGION / $MODEL_ID"; exit 1 ;;
  esac
fi
PRICE_IN="${PRICE_IN:-$DEFAULT_PRICE_IN}"
PRICE_OUT="${PRICE_OUT:-$DEFAULT_PRICE_OUT}"
# WORKSHOP_PRICING_END"""
    edit(
        "11-cost-latency.sh",
        lambda t: _apply(
            t, r'^PRICE_IN=.*\nPRICE_OUT=.*$', lambda _m: pricing,
            expect=1, flags=re.M, name="model-specific-price-snapshot",
            file="11-cost-latency.sh", records=records,
        ),
    )
    for old, new, name in (
        ("tok_in, tok_out = 0, 0", "tok_in, tok_out = 0, 0\nspans = []\nsys.path.insert(0, '$SCRIPT_DIR')\nfrom trace_metrics import count_tokens",
         "cost-token-counter"),
        ("    if (d.get('traceId') or d.get('trace_id')) != tid: continue",
         "    if (d.get('traceId') or d.get('trace_id')) != tid: continue\n    spans.append(d)",
         "collect-cost-spans"),
        ("        tok_in += ti; tok_out += to", "        pass  # Usage is counted once across the span hierarchy below.",
         "exclude-wrapper-token-totals"),
        ("cost = tok_in / 1_000_000 * pin + tok_out / 1_000_000 * pout",
         "tok_in, tok_out = count_tokens(spans)\ncost = tok_in / 1_000_000 * pin + tok_out / 1_000_000 * pout",
         "deduplicate-token-usage"),
    ):
        edit("11-cost-latency.sh", lambda t, old=old, new=new, name=name: _apply(
            t, re.escape(old), lambda _m: new, expect=1, flags=0, name=name,
            file="11-cost-latency.sh", records=records,
        ))
    # Deployment pipes must propagate the deploy exit status to the Guide caller.
    for script in ("10-optimize-prompt.sh", "12-compare-models.sh"):
        edit(script, lambda t, script=script: _apply(
            t, r"^set -e$", "set -e\nset -o pipefail",
            expect=1, flags=re.M, name="propagate-deploy-failure", file=script, records=records,
        ))
    # 12: switch the model on the deployed Harness (UpdateHarness, about 25 s) instead of two CDK deploys and two
    # memory re-setups (616-786 s live). Last of the 12 patches: it replaces the blocks the ones above edited.
    edit("12-compare-models.sh", lambda t: _patch_compare_models_in_place(t, records, str(data["namespace"]["agentName"])))
    # 10: update the prompt on the deployed Harness (about 30 s) instead of a CDK deploy (about 5 min).
    edit("10-optimize-prompt.sh", lambda t: _patch_optimize_in_place(t, records, str(data["namespace"]["agentName"])))

    # Customizer's S3 Files mount imports the base stack's VPC/bucket outputs.
    # Remove that dependency before the Guide empties buckets and deletes infra.
    edit(
        "99-cleanup.sh",
        lambda t: _apply(
            t, r'^echo "🗑️  Step 5: Emptying S3 buckets \(all versions\) before stack deletion\.\.\."$',
            '''echo "🗑️  Removing Customizer add-ons before base infrastructure..."
ADDONS_STACK="${ADDONS_STACK_NAME:-workshop-customizer-addons}"
if aws cloudformation describe-stacks --stack-name "$ADDONS_STACK" --region "$REGION" >/dev/null 2>&1; then
  aws cloudformation delete-stack --stack-name "$ADDONS_STACK" --region "$REGION" || exit 1
  aws cloudformation wait stack-delete-complete --stack-name "$ADDONS_STACK" --region "$REGION" || exit 1
fi
echo "🗑️  Step 5: Emptying S3 buckets (all versions) before stack deletion..."''',
            expect=1, flags=re.M, name="cleanup-addons-before-infra", file="99-cleanup.sh", records=records,
        ),
    )
    # --scenario-only (or WORKSHOP_CLEANUP_SCENARIO_ONLY=1): remove this scenario's AgentCore project,
    # Gateway, knowledge base, tools Lambda (unless workshop-infra owns it), SSM parameters and run records,
    # and keep workshop-infra plus the Customizer add-ons, so a changed pack can be synced and rehearsed
    # again without waiting hours for managed ENIs. Never run automatically.
    edit(
        "99-cleanup.sh",
        lambda t: _apply(
            t, r'^ACCOUNT_ID=\$\(aws sts get-caller-identity --query Account --output text 2>/dev/null\)$',
            '''ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
SCENARIO_ONLY="${WORKSHOP_CLEANUP_SCENARIO_ONLY:-0}"
[ "${1:-}" = "--scenario-only" ] && SCENARIO_ONLY=1
if [ "$SCENARIO_ONLY" = "1" ]; then
  echo "Mode:   --scenario-only (keeps $STACK_NAME and the Customizer add-ons)"
  echo ""
fi''',
            expect=1, flags=re.M, name="cleanup-scenario-only-flag", file="99-cleanup.sh", records=records,
        ),
    )
    edit(
        "99-cleanup.sh",
        lambda t: _apply(
            t, r'^echo "🗑️  Removing Customizer add-ons before base infrastructure\.\.\."$',
            '''if [ "$SCENARIO_ONLY" = "1" ]; then
  echo "🗑️  Scenario-only: tools Lambda, SSM parameters and local run records..."
  SCENARIO_FAILED=0
  FUNC_NAME="hr-tools-handler"
  # Fail closed: a throttled, denied or unreachable CloudFormation call keeps the function (and every
  # SSM parameter below) instead of reading as "workshop-infra owns nothing".
  if ! OWNED=$(aws cloudformation describe-stack-resources --stack-name "$STACK_NAME" --region $REGION \\
      --query "length(StackResources[?PhysicalResourceId=='$FUNC_NAME'])" --output text); then
    echo "  ❌ could not check whether $STACK_NAME owns Lambda $FUNC_NAME (see the error above); kept"; SCENARIO_FAILED=1
  elif [ "$OWNED" = "0" ]; then
    if ERR=$(aws lambda delete-function --function-name "$FUNC_NAME" --region $REGION 2>&1 >/dev/null); then
      echo "  ✅ Lambda $FUNC_NAME deleted"
    elif printf '%s' "$ERR" | grep -q ResourceNotFoundException; then
      echo "  ⚠️  Lambda $FUNC_NAME not found"
    else
      echo "  ❌ Lambda $FUNC_NAME was not deleted: $ERR"; SCENARIO_FAILED=1
    fi
    if ERR=$(aws logs delete-log-group --log-group-name "/aws/lambda/$FUNC_NAME" --region $REGION 2>&1 >/dev/null); then
      echo "  ✅ log group /aws/lambda/$FUNC_NAME deleted"
    elif ! printf '%s' "$ERR" | grep -q ResourceNotFoundException; then
      echo "  ❌ log group /aws/lambda/$FUNC_NAME was not deleted: $ERR"; SCENARIO_FAILED=1
    fi
  else
    echo "  ⏭️  Lambda $FUNC_NAME belongs to $STACK_NAME; kept (02 re-points it at the scenario handler)"
  fi
  if ! INFRA_IDS=$(aws cloudformation describe-stack-resources --stack-name "$STACK_NAME" --region $REGION \\
      --query "StackResources[].PhysicalResourceId" --output text); then
    echo "  ❌ could not list what $STACK_NAME owns (see the error above); no SSM parameter was deleted"; SCENARIO_FAILED=1
  elif PARAMS=$(aws ssm get-parameters-by-path --path "/app/hr" --recursive --region $REGION \\
      --query "Parameters[].Name" --output text 2>&1); then
    for PARAM in $PARAMS; do
      [ "$PARAM" = "None" ] && continue
      case " $INFRA_IDS " in *" $PARAM "*) continue ;; esac
      if ERR=$(aws ssm delete-parameter --name "$PARAM" --region $REGION 2>&1 >/dev/null); then
        echo "  ✅ SSM $PARAM deleted"
      else
        echo "  ❌ SSM $PARAM was not deleted: $ERR"; SCENARIO_FAILED=1
      fi
    done
  else
    echo "  ❌ could not list the SSM parameters under /app/hr: $PARAMS"; SCENARIO_FAILED=1
  fi
  rm -rf "$WORKDIR" "${WORKSHOP_ROOT:-$HOME/workshop}/eval-runs/hrassistant"
  echo ""
  if [ "$SCENARIO_FAILED" = "1" ]; then
    echo "❌ Scenario-only cleanup left resources behind (see ❌ above): fix the permission or delete them, then re-run."
    exit 1
  fi
  echo "✅ Scenario-only cleanup pass complete. $STACK_NAME and the Customizer add-ons were kept."
  echo "   Run preflight in the App before syncing a changed pack again."
  exit 0
fi
echo "🗑️  Removing Customizer add-ons before base infrastructure..."''',
            expect=1, flags=re.M, name="cleanup-scenario-only-stop", file="99-cleanup.sh", records=records,
        ),
    )
    edit(
        "99-cleanup.sh",
        lambda t: _apply(
            t, r'^if aws cloudformation describe-stacks --stack-name AgentCore-hrassistant-default',
            'aws iam delete-role-policy --role-name hrassistant_hrassistant --policy-name WorkshopHarnessImagePull 2>/dev/null || true\n'
            'if aws cloudformation describe-stacks --stack-name AgentCore-hrassistant-default',
            expect=1, flags=re.M, name="cleanup-runtime-image-policy", file="99-cleanup.sh", records=records,
        ),
    )
    edit(
        "99-cleanup.sh",
        lambda t: _apply(
            t, r'^WORKDIR=~/workshop/hrassistant$',
            'WORKDIR="${WORKSHOP_ROOT:-$HOME/workshop}/hrassistant"',
            expect=1, flags=re.M, name="cleanup-controller-workdir", file="99-cleanup.sh", records=records,
        ),
    )
    edit(
        "99-cleanup.sh",
        lambda t: _apply(
            t, r'^rm -rf ~/workshop/hrassistant$',
            'rm -rf "$WORKDIR"',
            expect=1, flags=re.M, name="cleanup-bound-workdir", file="99-cleanup.sh", records=records,
        ),
    )
    edit(
        "99-cleanup.sh",
        lambda t: _apply(
            t, r'^  elif \[ "\$STACK_STATE" = "DELETE_FAILED" \]; then\n.*?^  else\n',
            '  elif [ "$STACK_STATE" = "DELETE_FAILED" ]; then\n'
            '    echo "❌ Infrastructure deletion failed. Inspect stack events and retry after managed ENIs are released."\n'
            '    exit 1\n'
            '  else\n',
            expect=1, flags=re.M | re.S, name="cleanup-must-not-retain-and-pass", file="99-cleanup.sh", records=records,
        ),
    )
    edit(
        "knowledge-base/create_kb.py",
        lambda t: _apply(
            t, re.escape('        kb_details = self.bedrock_agent_client.get_knowledge_base(knowledgeBaseId=kb_id)'),
            '        if kb_id is None:\n'
            '            print(f"Knowledge Base {kb_name} already deleted.")\n'
            '            return\n'
            '        kb_details = self.bedrock_agent_client.get_knowledge_base(knowledgeBaseId=kb_id)',
            expect=1, flags=0, name="idempotent-kb-cleanup", file="knowledge-base/create_kb.py", records=records,
        ),
    )
    edit(
        "knowledge-base/create_kb.py",
        lambda t: _apply(
            t, re.escape('        smm_client.delete_parameter(Name=SSM_KB_ID_PARAM)'),
            '        try:\n'
            '            smm_client.delete_parameter(Name=SSM_KB_ID_PARAM)\n'
            '        except smm_client.exceptions.ParameterNotFound:\n'
            '            pass\n',
            expect=1, flags=0, name="idempotent-kb-parameter-cleanup", file="knowledge-base/create_kb.py", records=records,
        ),
    )
    edit(
        "99-cleanup.sh",
        lambda t: _apply(
            t,
            r'^#   - Lambda\(VPC\) \+ AgentCore runtime.*?^# 失败不中断',
            '#   - AgentCore shares ENIs for each subnet/security-group combination. Its service may\n'
            '#     retain them for up to 8 hours after agent deletion. Do not force-detach service ENIs.\n'
            '#     Keep a failed stack and retry this command after ENI removal; retained VPC resources\n'
            '#     are not automatically deleted by the service.\n'
            '#     https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html\n'
            '#\n# 失败不中断',
            expect=1, flags=re.M | re.S, name="document-service-eni-retention", file="99-cleanup.sh", records=records,
        ),
    )
    edit(
        "99-cleanup.sh",
        lambda t: _apply(
            t,
            r'^#   删栈通常会卡在网络资源：.*?^# -----------------------------------------------------------------------------\n',
            '#   AgentCore service-managed ENIs may remain for up to 8 hours after deletion.\n'
            '#   Retry after they disappear. Do not force-detach them or retain resources to report success.\n'
            '# -----------------------------------------------------------------------------\n',
            expect=1, flags=re.M | re.S, name="document-cleanup-retry", file="99-cleanup.sh", records=records,
        ),
    )
    edit(
        "99-cleanup.sh",
        lambda t: _apply(
            t,
            re.escape('    echo "  ⚠️  等待超时（栈仍 $STACK_STATE）。ENI 尚未回收完，稍后重跑本脚本或到控制台检查。"'),
            '    echo "  ⏳ Cleanup pending ($STACK_STATE). AgentCore ENIs may remain for up to 8 hours; retry after service cleanup."\n'
            '    exit 75',
            expect=1, flags=0, name="deferred-cleanup-exit-status", file="99-cleanup.sh", records=records,
        ),
    )
    edit(
        "99-cleanup.sh",
        lambda t: _apply(
            t,
            r'^echo "ℹ️  若 Step 6 走了 --retain-resources：.*\n.*\n.*\n',
            'echo "CloudFormation infrastructure deletion completed. Retained resources are not treated as a completed cleanup."\n',
            expect=1, flags=re.M, name="remove-retained-resource-success-claim", file="99-cleanup.sh", records=records,
        ),
    )

    # 00-setup: pre-create pack skill directories instead of the HR ones.
    mkdirs = "".join(f"mkdir -p ~/workshop/skills/{name}\n" for name in skills) or "mkdir -p ~/workshop/skills\n"
    edit(
        "00-setup.sh",
        lambda t: _apply(t, r"(?:^mkdir -p ~/workshop/skills/[a-z-]+\n)+", lambda _m: mkdirs, expect=1, flags=re.M, name="skill-dirs", file="00-setup.sh", records=records),
    )

    # CFN: EC2 UserData no longer bakes HR skill files; it only pre-creates pack skill directories.
    def patch_cfn(t: str) -> str:
        def mkdir_block(m: re.Match[str]) -> str:
            indent = m.group("indent")
            return "".join(f"{indent}mkdir -p /home/ssm-user/workshop/skills/{name}\n" for name in skills) or f"{indent}mkdir -p /home/ssm-user/workshop/skills\n"

        t = _apply(
            t,
            r"(?P<indent> +)mkdir -p /home/ssm-user/workshop/skills/[a-z-]+\n(?:(?P=indent)mkdir -p /home/ssm-user/workshop/skills/[a-z-]+\n)*",
            mkdir_block,
            expect=1, flags=0, name="userdata-skill-dirs", file="cfn/workshop-infra.yaml", records=records,
        )
        t = _apply(
            t,
            r"^ +cat > /home/ssm-user/workshop/skills/[a-z-]+/SKILL\.md << 'SKILLEOF'\n.*?^ +SKILLEOF\n",
            "",
            expect=2, flags=re.M | re.S, name="userdata-skill-heredocs", file="cfn/workshop-infra.yaml", records=records,
        )
        return t

    edit("cfn/workshop-infra.yaml", patch_cfn)

    # 03: fully generated.
    (release / "03-configure-skills.sh").write_text(render_skills_script(data), encoding="utf-8")
    (release / "03-configure-skills.sh").chmod(0o755)
    records.append(PatchRecord(file="03-configure-skills.sh", name="generated", matches=1))

    # Helper wording names the scenario, not the HR workshop: the Gateway and target descriptions are
    # visible in the AWS console (the release gate scans them, check_script_residue), and the 01/02/09/10
    # header comments described the HR generator, its three questions and the Workshop Studio pages.
    pack_id = str(data["id"])
    for rel, old, new, name in (
        ("gateway/create_gateway.py", 'description="HR assistant tools gateway",',
         f"description={json.dumps(pack_id + ' assistant tools gateway')},", "gateway-description"),
        ("gateway/create_gateway.py", 'description="HR tools backed by Lambda",',
         f"description={json.dumps(pack_id + ' scenario tools backed by Lambda')},", "gateway-target-description"),
        ("gateway/create_gateway.py", "the HR Tools Lambda (GATEWAY_IAM_ROLE", "the scenario tools Lambda (GATEWAY_IAM_ROLE", "gateway-role-docstring"),
        ("gateway/create_gateway.py", "# Permission: invoke the HR Tools Lambda", "# Permission: invoke the scenario tools Lambda", "gateway-role-comment"),
        ("gateway/create_gateway.py", 'help="HR Tools Lambda ARN"', 'help="scenario tools Lambda ARN"', "gateway-lambda-help"),
        ("02-create-gateway.sh", "# Phase 2b: Deploy HR Tools Lambda + Create Gateway", "# Phase 2b: Deploy the scenario tools Lambda + Create Gateway",
         "gateway-header-neutral"),
        ("02-create-gateway.sh", "# Deploys the HR Tools Lambda + provisions", "# Deploys the scenario tools Lambda + provisions", "gateway-intro-neutral"),
        ("02-create-gateway.sh", "# 1. Deploy the HR Tools Lambda (creates", "# 1. Deploy the scenario tools Lambda (creates", "gateway-step1-neutral"),
        ("02-create-gateway.sh", "# 2. Resolve the HR Tools Lambda ARN", "# 2. Resolve the scenario tools Lambda ARN", "gateway-step2-neutral"),
        ("01-create-kb.sh", "#   1. generate_hr_docs.py  —— 生成 HR 政策 markdown 文档到 ~/workshop/knowledge-base/",
         "#   1. 把场景包的知识库文档（pack/knowledge-base/docs/*.md）复制到 ~/workshop/knowledge-base/", "kb-header-neutral"),
        ("01-create-kb.sh", "（KB 名称、hr/ 前缀、文档路径", "（KB 名称、KB 前缀、文档路径", "kb-prefix-neutral"),
        ("01-create-kb.sh", "# 1. 生成 HR 政策文档（写入 ~/workshop/knowledge-base/）", "# 1. 复制场景包的知识库文档（写入 ~/workshop/knowledge-base/）",
         "kb-step1-neutral"),
        ("09-run-eval.sh",
         "# 一站式：跑 content 文档里的【三个代表性问题】（绩效 / 福利 / 病假），各产生\n"
         "# 一条含检索的 trace，等索引完成后，对这三条跑 THELMA + Mind the Goal 评估并\n"
         "# 打印分数。分数即与 063_run_eval 文档对应。\n",
         "# 一站式：按评估顺序跑场景包的练习问题（检索探针在前、稳定性问题最后），探针各产生\n"
         "# 一条含检索的 trace，等索引完成后，对这些 trace 跑 THELMA、对每个练习会话跑 Mind the Goal\n"
         "# 并打印分数。\n", "eval-header-neutral"),
        ("10-optimize-prompt.sh",
         "# 与优化前对比（GR/SP2/RP 升降）由你对照 content 072 的表格判断：脚本只负责产出\n"
         "# 优化后的分数。绩效/福利（检索好）应明显提升；病假（检索失效）改 Prompt 救不了，\n"
         "# 仍 Fail——这正印证 Phase 4 的诊断。\n",
         "# 与优化前的对比（GR/SP2/RP 升降）打印在脚本末尾：检索好的 prompt_fixable 问题应明显\n"
         "# 提升；检索缺口问题改 Prompt 救不了——这正印证 Phase 4 的诊断。\n", "optimize-header-neutral"),
        ("10-optimize-prompt.sh",
         "# 与 content 071 一致的中文优化 Prompt。实测：Prompt 语言与中文 KB 政策文档一致时，\n"
         "# 「严格基于检索 + 忽略无关内容 + 简洁聚焦」这几条抗幻觉约束最见效（检索质量好的\n"
         "# 绩效/福利问题，GR 与 RP 明显提升）。相比英文 Prompt 少一层语言切换，接地对齐更顺。\n",
         "# 场景包的优化 Prompt（optimization candidate，与知识库同语言）。「严格基于检索 + 忽略无关\n"
         "# 内容 + 简洁聚焦」这几条抗幻觉约束最见效（检索质量好的问题 GR 与 RP 明显提升）。\n", "optimize-prompt-comment-neutral"),
    ):
        edit(rel, lambda t, old=old, new=new, name=name, rel=rel: _apply(
            t, re.escape(old), lambda _m, new=new: new, expect=1, flags=0, name=name, file=rel, records=records))
    return records


def _replace_tokens(release: Path, data: dict[str, Any]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    mapping = token_map(data)
    for path in sorted(release.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        rel = path.relative_to(release).as_posix()
        if rel.startswith(f"{PACK_DIR}/") or rel == MANIFEST_NAME:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        file_counts: dict[str, int] = {}
        for token, value in mapping:
            if token == value:
                continue
            n = text.count(token)
            if n:
                file_counts[token] = n
                text = text.replace(token, value)
        if file_counts:
            path.write_text(text, encoding="utf-8")
            counts[rel] = file_counts
    return counts


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def render_release(pack: CompiledPack, upstream: Path, release_dir: Path, *, template_commit: str) -> RenderedRelease:
    """Render a complete release for ``pack`` from the pinned ``upstream`` checkout."""
    data = pack.scenario.data
    release_dir = Path(release_dir)
    if release_dir.exists():
        shutil.rmtree(release_dir)
    handler_template = TEMPLATES_DIR / f"{LAMBDA_MODULE}.py"
    if not handler_template.is_file():
        raise RenderError(f"generic Lambda handler template missing: {handler_template}")

    _copy_upstream(Path(upstream), release_dir)
    shutil.copytree(pack.pack_dir, release_dir / PACK_DIR)

    facts = script_facts.compute(data)
    if not facts.eval_cases:
        raise RenderError("pack has no practice golden cases")

    patches = _patch_scripts(release_dir, data, facts)

    # Generated runtime artifacts (written in upstream terms; the token pass renames them).
    shutil.copyfile(handler_template, release_dir / "lambda" / f"{LAMBDA_MODULE}.py")
    shutil.copyfile(TEMPLATES_DIR / "trace_session.py", release_dir / "trace_session.py")
    shutil.copyfile(TEMPLATES_DIR / "trace_metrics.py", release_dir / "trace_metrics.py")
    shutil.copyfile(TEMPLATES_DIR / "evaluation_ready.py", release_dir / "evaluation_ready.py")
    shutil.copyfile(pack.pack_dir / "tools" / "fixtures.json", release_dir / "lambda" / "fixtures.json")
    schema_name = f"{data['namespace']['toolTargetName']}-schema.json"
    shutil.copyfile(pack.pack_dir / "tools" / "schema.json", release_dir / "gateway" / schema_name)

    token_counts = _replace_tokens(release_dir, data)
    # The single L1 implementation, byte for byte (after the token pass: it carries no template
    # identifier, and the release copy must equal engine/workshop_customizer/l1.py).
    shutil.copyfile(L1_SOURCE, release_dir / L1_RELEASE_NAME)

    # The public Guide starts with `cd static/scripts`. Preserve the release's
    # existing root entry points for SSM and also ship that copy/paste layout.
    guide_dir = release_dir / "static" / "scripts"
    guide_dir.mkdir(parents=True)
    for script in sorted(release_dir.glob("*.sh")):
        script.chmod(0o755)
        wrapper = guide_dir / script.name
        wrapper.write_text(
            '#!/bin/bash\nset -e\n'
            'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"\n'
            'if [ -d /opt/workshop-customizer/bin ]; then\n'
            '  export PATH="/opt/workshop-customizer/bin:$HOME/.local/bin:$PATH"\n'
            'fi\n'
            f'exec bash "$ROOT/{script.name}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

    # The Workshop Guide (SPEC D10): the release root README.md is the pack's generated student guide,
    # byte for byte (written after the token pass; the upstream READMEs stay excluded).
    student_guide = pack.pack_dir / "labs" / "student-guide.md"
    if not student_guide.is_file():
        raise RenderError("compiled pack has no student guide (pack/labs/student-guide.md); recompile the pack")
    shutil.copyfile(student_guide, release_dir / "README.md")

    # The scripts must do exactly what the script facts (and so the Workshop Guide) say.
    disagreements = script_facts.verify_rendered(release_dir, facts)
    if disagreements:
        raise RenderError("script facts disagree with the rendered scripts:\n- " + "\n- ".join(disagreements))

    report = validate_output_tree(
        data,
        student_root=release_dir,
        release_root=release_dir,
        residue_exclude=[MANIFEST_NAME],
        prose_check=False,
        source_root=pack.scenario.root,
    )
    # Printed HR Workshop wording (its three golden questions, fixed counts, Workshop Studio content
    # pages) must not reach a non-HR release's console (SPEC D6e).
    report.extend(check_script_residue(data, release_dir, sources_text=_pack_sources_text(pack)))
    if not report.ok:
        raise PackValidationError(report)

    files = {p.relative_to(release_dir).as_posix(): sha256_file(p) for p in sorted(release_dir.rglob("*")) if p.is_file()}
    content_hash = hashlib.sha256("\n".join(f"{k} {v}" for k, v in sorted(files.items())).encode("utf-8")).hexdigest()
    version = f"{data['id']}-{content_hash[:12]}"
    manifest = {
        "schema": "workshop-customizer/release/1",
        "packId": data["id"],
        "packKind": data["packKind"],
        "version": version,
        "contentHash": content_hash,
        "templateCommit": template_commit,
        "generatorVersion": __version__,
        "lambdaModule": LAMBDA_MODULE,
        "namespace": data["namespace"],
        "retrievalToolName": data["evaluation"]["retrievalToolName"],
        "evaluationFeatures": sorted(facts.features),
        # SPEC D12: the provenance policy the pack was gated under; holdout anchor ids stay instructor-only.
        "provenancePolicy": provenance_policy(data, student_safe=True),
        "patches": [{"file": p.file, "name": p.name, "matches": p.matches} for p in patches],
        "tokenReplacements": token_counts,
        "files": files,
    }
    release_teaching = teaching.release_view(data)
    if release_teaching is not None:
        manifest["teaching"] = release_teaching
    (release_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return RenderedRelease(pack=pack, release_dir=release_dir, version=version, files=files, patches=patches, token_counts=token_counts)


def verify_release(release_dir: Path) -> dict[str, Any]:
    """Recompute hashes and compare with RELEASE.json. Returns the manifest; raises on mismatch."""
    release_dir = Path(release_dir)
    manifest_path = release_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise RenderError(f"{MANIFEST_NAME} missing in {release_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected: dict[str, str] = manifest.get("files", {})
    actual = {p.relative_to(release_dir).as_posix(): sha256_file(p) for p in sorted(release_dir.rglob("*")) if p.is_file() and p.name != MANIFEST_NAME}
    problems: list[str] = []
    for rel, digest in expected.items():
        if rel not in actual:
            problems.append(f"missing: {rel}")
        elif actual[rel] != digest:
            problems.append(f"hash mismatch: {rel}")
    for rel in actual:
        if rel not in expected:
            problems.append(f"unexpected file: {rel}")
    content_hash = hashlib.sha256("\n".join(f"{k} {v}" for k, v in sorted(actual.items())).encode("utf-8")).hexdigest()
    if content_hash != manifest.get("contentHash"):
        problems.append("contentHash mismatch")
    if problems:
        raise RenderError("release verification failed:\n- " + "\n- ".join(problems))
    return manifest
