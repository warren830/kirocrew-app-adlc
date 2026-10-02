"""L1 scenario assertions: deterministic checks of every practice case against its golden ``expected``.

This module is the ONE implementation of L1 (SPEC D4). It is pure standard library, runs on Python
3.9+, and carries no template identifiers, so ``render`` copies it byte for byte into every release
as ``l1_eval.py``; the Guide's ``09-run-eval.sh`` (and the ``09 --eval-only`` child of
``10-optimize-prompt.sh``) runs it after the judges. The report and rehearsal import it for
:func:`combined_verdict`.

Inputs of one run (the run record written by 09/10, never inside the release):

``<run>/sessions.tsv``  ``index  caseId  sessionId  actor  startedMs  probe`` (one row per question)
``<run>/q<index>.out``  the filtered ``agentcore invoke --stream`` output of that question
``<run>/scores.tsv``    ``evaluator  sessionId  traceId  number  label  metricsJSON  caseId``

plus the release's ``pack/pack.json`` + ``pack/golden/practice.json``, the agentcore invoke logs
(final responses keyed by sessionId) and the ``aws/spans`` log group (tool calls per session; the
CLI reads it through boto3, tests inject a source).

Check codes (:data:`CHECK_CODES`) and statuses (:data:`STATUSES`):

* ``response`` — pass | fail (empty) | unverified (no response found) | error (invoke error)
* ``requiredTools`` / ``forbiddenTools`` — decided only from indexed, stable tool evidence,
  otherwise unverified
* ``mustMention`` / ``mustMentionAnyOf`` / ``mustNotMention`` — lexical, NFKC + casefold, ASCII
  class boundaries ('24' matches '24h' but not '240'), CJK space folding; a mustNotMention hit in
  a negated context ("do not ...", "不要 ...", the cue in the same clause) is ``defer``
* ``shouldRefuse`` / ``shouldEscalate`` — a forbidden or escalation tool call, else a declared or
  default marker, else ``defer`` (the Mind-the-Goal judge decides); ``false`` is ``n/a``

A case verdict is the first of error > fail > unverified > defer > pass among its checks; a case
with no assertion beyond a response is ``defer``. :func:`combined_verdict` joins it with the MtG
label (SPEC D2: defer / unverified are resolved by the judge, else undetermined).

Outputs: ``<run>/l1.json`` (full), a compact form (schema ``workshop-customizer/l1/1``, at most
:data:`COMPACT_LIMIT` characters) at ``--export-compact`` or ``$WORKSHOP_EVAL_OUT_DIR/l1-compact.json``,
and a per-case table on stdout. Neither this file nor its output contains the phrases the SSM
step-output parser keys on (score lines, judge headers, cost and model-comparison labels), so L1
lines can never be mistaken for judge scores (tests pin the list).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

L1_SCHEMA = "workshop-customizer/l1/1"
CHECK_CODES: tuple[str, ...] = (
    "response", "requiredTools", "forbiddenTools", "mustMention", "mustMentionAnyOf", "mustNotMention",
    "shouldRefuse", "shouldEscalate",
)
STATUSES: tuple[str, ...] = ("pass", "fail", "defer", "unverified", "error", "n/a")
#: Case verdict precedence (the first status present wins).
VERDICT_PRECEDENCE: tuple[str, ...] = ("error", "fail", "unverified", "defer", "pass")
COMBINED: tuple[str, ...] = ("pass", "fail", "undetermined")
PHASES: tuple[str, ...] = ("baseline", "optimized", "comparison")
FLIPS: tuple[str, ...] = ("fixed", "regressed", "still-failing", "still-passing", "undetermined")

#: Evaluator ids in scores.tsv (the upstream evaluator names, unchanged by customization).
RAG_EVALUATOR = "thelma_rag_quality"
GOAL_EVALUATOR = "mtg_goal_success"

#: SP2 at or below this in both runs marks a case as retrieval-limited (a hint in comparisons only;
#: it equals teaching.THRESHOLDS["retrievalFailedSp2"], which owns the verdict rules).
RETRIEVAL_LIMIT_SP2 = 0.2

#: Built-in refusal markers (English and Chinese); evaluation.l1.refusalMarkers replaces them.
DEFAULT_REFUSAL_MARKERS: tuple[str, ...] = (
    "cannot", "can't", "can not", "unable to", "not able to", "not allowed", "not permitted",
    "not authorized", "not authorised", "i won't", "i will not",
    "不能", "无法", "不可以", "不允许", "不予", "无权", "没有权限",
)
#: A mustNotMention hit preceded (same clause, within NEGATION_WINDOW chars) by one of these is deferred.
#: No bare '别': it is part of 级别 / 特别 / 区别 / 类别, and its imperative sense is covered by 不要 / 切勿.
NEGATION_CUES: tuple[str, ...] = (
    "not", "never", "don't", "do not", "doesn't", "cannot", "can't", "no", "without", "avoid",
    "不要", "不能", "不得", "禁止", "严禁", "切勿", "无法", "不可",
)
NEGATION_WINDOW = 24
SENTENCE_BREAKS = ".!?。！？\n"
#: A cue governs only its own clause: "No problem, MFA has been disabled" and "别担心，MFA 已被禁用" are not
#: negated. Clauses also end at a dash with a space on both sides (" - "; normalize maps — and – to -).
CLAUSE_BREAKS = ",;:，；："
INVOKE_ERROR_RE = re.compile(
    r"AccessDeniedException|Model access is denied|^Error:|ThrottlingException|ValidationException"
    r"|Model produced invalid sequence",
    re.M,
)
STREAM_FOOTER_RE = re.compile(r"^\s*(?:Session:|To resume:|Log:)", re.I)
#: Strands' MaxTokensReachedException: the answer before it is what the student saw, cut at --max-tokens
#: (live 2026-09-28/29: one baseline PF answer in three of seven runs, the weak baseline asked for extra advice).
TRUNCATED_RE = re.compile(r"^Error: Model stopped generating due to maximum token limit", re.M)
LOG_FOOTER_RE = re.compile(r"^\s*Log:\s*(\S+)\s*$", re.M)

COMPACT_LIMIT = 8000
RESPONSE_EXCERPT = 400
MAX_LOG_BYTES = 4_000_000
DEFAULT_MAX_POLLS = 6
DEFAULT_POLL_SECONDS = 15.0
SPAN_LOG_GROUP = "aws/spans"


class InputError(Exception):
    """The run record or the pack is unusable (CLI exit code 2)."""


# ---------------------------------------------------------------------------
# Lexical matching
# ---------------------------------------------------------------------------

_TRANSLATE = str.maketrans({
    "−": "-", "–": "-", "—": "-", "﹣": "-", "－": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"', "*": None, "`": None,
})
_WS = re.compile(r"\s+")


def _collapse(match: re.Match) -> str:
    return "\n" if "\n" in match.group(0) else " "


def normalize(text: str) -> str:
    """NFKC, unified dashes and quotes, markdown emphasis dropped, casefolded, whitespace runs collapsed.

    A whitespace run becomes one space, or one newline when it contained a line break, so matching
    (which reads newlines as spaces) and sentence windows (which stop at newlines) share indexes.
    """
    folded = unicodedata.normalize("NFKC", str(text)).translate(_TRANSLATE).casefold()
    return _WS.sub(_collapse, folded).strip()


def fold_cjk(text: str) -> str:
    """Drop a space that has a non-ASCII character on either side ('10 分钟' → '10分钟')."""
    out = []
    for i, ch in enumerate(text):
        if ch == " " and ((i > 0 and not text[i - 1].isascii()) or (i + 1 < len(text) and not text[i + 1].isascii())):
            continue
        out.append(ch)
    return "".join(out)


def _cls(ch: str) -> str | None:
    if ch.isascii() and (ch.isalpha() or ch == "_"):
        return "L"
    if ch.isascii() and ch.isdigit():
        return "D"
    return None


def _boundary(hay: str, index: int, edge: str) -> bool:
    if index < 0 or index >= len(hay):
        return True
    neighbour = _cls(hay[index])
    return neighbour is None or neighbour != _cls(edge)


def find_all(hay: str, needle: str) -> list[int]:
    """Start indexes of ``needle`` in ``hay`` (both normalized) where the ASCII-class boundaries hold."""
    if not needle:
        return []
    flat = hay.replace("\n", " ")
    hits, start = [], 0
    while True:
        i = flat.find(needle, start)
        if i < 0:
            return hits
        if _boundary(flat, i - 1, needle[0]) and _boundary(flat, i + len(needle), needle[-1]):
            hits.append(i)
        start = i + 1


def _snippet(hay: str, index: int, length: int, pad: int = 30) -> str:
    lo, hi = max(0, index - pad), min(len(hay), index + length + pad)
    return ("…" if lo else "") + hay[lo:hi].replace("\n", " ") + ("…" if hi < len(hay) else "")


def find_hits(text: str, needle: str) -> list[tuple[str, int, str]]:
    """Every hit of ``needle`` in ``text`` as ``(haystack, index, needle)``: normalized, else CJK-folded."""
    hay, pin = normalize(text), normalize(needle)
    if not pin:
        return []
    found = find_all(hay, pin)
    if found:
        return [(hay, i, pin) for i in found]
    folded_hay, folded_pin = fold_cjk(hay), fold_cjk(pin)
    if (folded_hay, folded_pin) == (hay, pin):
        return []
    return [(folded_hay, i, folded_pin) for i in find_all(folded_hay, folded_pin)]


def contains(text: str, needle: str) -> tuple[bool, str]:
    """``(found, snippet)`` for ``needle`` in ``text``."""
    hits = find_hits(text, needle)
    if not hits:
        return False, ""
    hay, index, pin = hits[0]
    return True, _snippet(hay, index, len(pin))


def _clause_break(hay: str, i: int) -> bool:
    ch = hay[i]
    if ch in SENTENCE_BREAKS or ch in CLAUSE_BREAKS:
        return True
    return ch == "-" and 0 < i < len(hay) - 1 and hay[i - 1] == " " and hay[i + 1] == " "


def negated(hay: str, index: int) -> bool:
    """True when a negation cue ends before ``index`` in the same clause, within NEGATION_WINDOW chars.

    A clause ends at a sentence break, a comma / semicolon / colon (ASCII or full-width) or a spaced
    dash, so an opener ("No problem, ...", "No worries - ...", "别担心，...") never softens a violation.
    """
    start = max(0, index - NEGATION_WINDOW)
    for i in range(index - 1, start - 1, -1):
        if _clause_break(hay, i):
            start = i + 1
            break
    for cue in NEGATION_CUES:
        pin = normalize(cue)
        if any(start <= pos and pos + len(pin) <= index for pos in find_all(hay, pin)):
            return True
    return False


# ---------------------------------------------------------------------------
# Run record
# ---------------------------------------------------------------------------


def read_sessions(path: Path | str, known_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Rows of sessions.tsv as ``{index, caseId, sessionId, actor, startedMs, probe}``.

    Rows with an unknown caseId (when ``known_ids`` is given) or a malformed index are skipped.
    """
    known = set(known_ids) if known_ids is not None else None
    rows: list[dict[str, Any]] = []
    p = Path(path)
    if not p.is_file():
        return rows
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0].strip().isdigit():
            continue
        case_id = parts[1].strip()
        if known is not None and case_id not in known:
            print(f"  (note: sessions.tsv row for unknown case '{case_id}' skipped)")
            continue
        started = parts[4].strip() if len(parts) > 4 else ""
        rows.append({
            "index": int(parts[0]),
            "caseId": case_id,
            "sessionId": parts[2].strip(),
            "actor": parts[3].strip() if len(parts) > 3 else "",
            "startedMs": int(started) if started.isdigit() else None,
            "probe": (parts[5].strip() == "1") if len(parts) > 5 else None,
        })
    return rows


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN is not a score


def read_scores(path: Path | str) -> dict[str, dict[str, dict[str, Any]]]:
    """scores.tsv → ``{sessionId: {"rag": {...}, "goal": {...}}}`` (the last usable row per evaluator wins)."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    p = Path(path)
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 5 or not parts[1]:
            continue
        evaluator, sid, trace, raw, label = parts[:5]
        number = _float(raw)
        if number is None or label.strip().lower() in ("skipped", "error"):
            continue
        metrics: dict[str, float] = {}
        if len(parts) > 5 and parts[5].strip():
            try:
                loaded = json.loads(parts[5])
            except ValueError:
                loaded = {}
            if isinstance(loaded, dict):
                metrics = {k: v for k, v in ((str(k), _float(v)) for k, v in loaded.items()) if v is not None}
        key = "rag" if evaluator == RAG_EVALUATOR else "goal" if evaluator == GOAL_EVALUATOR else None
        if key is None:
            continue
        entry: dict[str, Any] = {"value": number, "label": label.strip(), "traceId": trace}
        if key == "rag":
            entry["metrics"] = metrics
        out.setdefault(sid, {})[key] = entry
    return out


# ---------------------------------------------------------------------------
# Responses (invoke logs, then the streamed output)
# ---------------------------------------------------------------------------


def flatten_text(value: Any) -> str:
    """A response that is a string, a list of text blocks or a nested dict, as one string."""
    if value is None:
        return ""
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith("[") and '"text"' in stripped[:40]:
            try:
                return flatten_text(json.loads(value))
            except ValueError:
                return value
        return value
    if isinstance(value, list):
        return " ".join(t for t in (flatten_text(v) for v in value) if t)
    if isinstance(value, dict):
        for key in ("text", "content", "message", "response"):
            if key in value:
                return flatten_text(value[key])
        return ""
    return str(value)


def parse_invoke_log(text: str) -> dict[str, dict[str, Any]]:
    """sessionId → {prompt, response, answered} from one invoke log (JSON blocks; the last value wins).

    ``answered`` is true when the log holds a successful RESPONSE block, even one whose response is empty:
    an agent that ends its turn with no text (for example after a long run of tool calls) answered nothing,
    which L1 reports as an empty response rather than a missing one.
    """
    decoder = json.JSONDecoder()
    sid = prompt = response = None
    answered = False
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            if obj.get("sessionId"):
                sid = obj["sessionId"]
            if obj.get("prompt"):
                prompt = obj["prompt"]
            if obj.get("response"):
                response = obj["response"]
            if "response" in obj and obj.get("success") is True:
                answered = True
    if not sid:
        return {}
    return {str(sid): {"prompt": flatten_text(prompt), "response": flatten_text(response), "answered": answered}}


def _read_log(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(MAX_LOG_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def scan_invoke_logs(log_dir: Path | str | None, since_ms: int = 0) -> dict[str, dict[str, Any]]:
    """sessionId → {prompt, response, log} for every *.log modified since the run started (−120 s)."""
    mapping: dict[str, dict[str, Any]] = {}
    if not log_dir:
        return mapping
    base = Path(log_dir).expanduser()
    if not base.is_dir():
        return mapping
    floor = since_ms / 1000.0 - 120 if since_ms else 0
    logs = []
    for path in base.glob("*.log"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= floor:
            logs.append((mtime, path.name, path))
    for _mtime, _name, path in sorted(logs):
        for sid, entry in parse_invoke_log(_read_log(path)).items():
            previous = mapping.get(sid, {})
            mapping[sid] = {
                "prompt": entry["prompt"] or previous.get("prompt", ""),
                "response": entry["response"] or previous.get("response", ""),
                "answered": bool(entry["answered"] or previous.get("answered")),
                "log": str(path),
            }
    return mapping


def response_for(row: Mapping[str, Any], run_dir: Path, logs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The final response of one question: ``{source, text, error, log}`` (plus ``truncated`` for a cut answer).

    Order: the invoke log named by the ``Log:`` footer of q<i>.out, any invoke log with the session,
    the streamed output minus its footer. An invoke error in q<i>.out wins over all of them, except the
    model's token limit: the CLI still logs the partial answer, which is read like any answer (the log
    first; q<i>.out is grep-filtered by 09/10) and marked ``truncated``. A log that recorded a successful
    but empty response gives an empty ``invoke-log`` text (L1: empty response).
    """
    sid = row["sessionId"]
    out_path = run_dir / f"q{row['index']}.out"
    streamed = out_path.read_text(encoding="utf-8", errors="replace") if out_path.is_file() else ""
    error = INVOKE_ERROR_RE.search(streamed)
    cut = TRUNCATED_RE.search(streamed)
    truncated = cut is not None and (error is None or error.start() >= cut.start())
    mark = {"truncated": True} if truncated else {}
    if error and not truncated:
        line = next((ln.strip() for ln in streamed.splitlines() if error.group(0) in ln), error.group(0))
        return {"source": "error", "text": "", "error": line[:300], "log": None}
    empty_log = None
    for footer in LOG_FOOTER_RE.findall(streamed):
        entry = parse_invoke_log(_read_log(Path(footer).expanduser())).get(sid)
        if entry and entry["response"].strip():
            return {"source": "invoke-log", "text": entry["response"], "error": None, "log": footer, **mark}
        if entry and entry["answered"]:
            empty_log = empty_log or footer
    entry = logs.get(sid)
    if entry and str(entry.get("response") or "").strip():
        return {"source": "invoke-log", "text": str(entry["response"]), "error": None, "log": entry.get("log"), **mark}
    if entry and entry.get("answered"):
        empty_log = empty_log or entry.get("log")
    if truncated:
        body = "\n".join(ln for ln in streamed[:cut.start()].splitlines() if not STREAM_FOOTER_RE.match(ln)).strip()
        if body:
            return {"source": "stream", "text": body, "error": None, "log": None, "truncated": True}
        line = next((ln.strip() for ln in streamed.splitlines() if cut.group(0) in ln), cut.group(0))
        return {"source": "error", "text": "", "error": line[:300], "log": None}
    body = "\n".join(ln for ln in streamed.splitlines() if not STREAM_FOOTER_RE.match(ln)).strip()
    if body:
        return {"source": "stream", "text": body, "error": None, "log": None}
    if empty_log:
        return {"source": "invoke-log", "text": "", "error": None, "log": empty_log}
    return {"source": "missing", "text": "", "error": None, "log": None}


# ---------------------------------------------------------------------------
# Tool evidence (aws/spans)
# ---------------------------------------------------------------------------


class LogsSpanSource:
    """Spans from CloudWatch Logs ``aws/spans`` via a boto3 ``logs`` client (paginated, spaced calls)."""

    def __init__(self, client: Any, since_ms: int, *, pause: float = 0.25, sleep: Callable[[float], None] = time.sleep):
        self.client, self.since_ms, self.pause, self.sleep = client, int(since_ms or 0), pause, sleep

    def query(self, needle: str) -> list[dict[str, Any]]:
        spans: list[dict[str, Any]] = []
        paginator = self.client.get_paginator("filter_log_events")
        for page in paginator.paginate(logGroupName=SPAN_LOG_GROUP, startTime=self.since_ms, filterPattern=f'"{needle}"'):
            for event in page.get("events", []):
                try:
                    span = json.loads(event.get("message") or "")
                except ValueError:
                    continue
                if isinstance(span, dict):
                    spans.append(span)
            if self.pause:
                self.sleep(self.pause)
        return spans


class JsonlSpanSource:
    """Spans from a local JSON-lines file (one span per line; substring filter like the log filter)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def query(self, needle: str) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        spans = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            if needle in line:
                try:
                    span = json.loads(line)
                except ValueError:
                    continue
                if isinstance(span, dict):
                    spans.append(span)
        return spans


def _attrs(span: Mapping[str, Any]) -> Mapping[str, Any]:
    attrs = span.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def span_session(span: Mapping[str, Any]) -> str:
    return str(_attrs(span).get("session.id") or span.get("session_id") or "")


def span_trace(span: Mapping[str, Any]) -> str:
    return str(span.get("traceId") or span.get("trace_id") or "")


def tool_name(span: Mapping[str, Any], compact_target: str = "") -> str | None:
    """The logical tool a span executed (``<compactTarget>___`` Gateway prefix stripped), else None."""
    attrs = _attrs(span)
    name = str(attrs.get("gen_ai.tool.name") or "")
    if not name:
        span_name = str(span.get("name") or "")
        if span_name.startswith("execute_tool "):
            name = span_name[len("execute_tool "):]
        elif attrs.get("gen_ai.operation.name") == "execute_tool":
            name = span_name
    name = name.strip()
    if not name:
        return None
    prefix = f"{compact_target}___" if compact_target else ""
    if prefix and name.startswith(prefix):
        return name[len(prefix):]
    if "___" in name:
        return name.split("___", 1)[1]
    return name


def _span_key(span: Mapping[str, Any]) -> str:
    return str(span.get("spanId") or span.get("span_id") or json.dumps(span, sort_keys=True, default=str))


def _session_spans(source: Any, sid: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pass 1: spans carrying the session id; pass 2: every span of those traces (tool spans may lack it)."""
    first = [s for s in source.query(sid) if span_session(s) == sid]
    traces: list[str] = []
    for span in first:
        tid = span_trace(span)
        if tid and tid not in traces:
            traces.append(tid)
    seen: dict[str, dict[str, Any]] = {_span_key(s): s for s in first}
    for tid in traces:
        for span in source.query(tid):
            if span_trace(span) == tid:
                seen.setdefault(_span_key(span), span)
    return first, list(seen.values())


def collect_tool_evidence(
    source: Any,
    rows: Sequence[Mapping[str, Any]],
    cases: Mapping[str, Mapping[str, Any]],
    *,
    compact_target: str = "",
    pack_tools: Iterable[str] = (),
    escalation_tools: Iterable[str] = (),
    max_polls: int = DEFAULT_MAX_POLLS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Tool calls per session with bounded stability polling. Returns ``(evidence by sessionId, polls)``.

    A session is *indexed* once a span carries its id, and *stable* once it is indexed and either the
    expected tools are already visible (no forbidden tools or escalation tools to wait for) or two
    consecutive polls saw the same span counts.
    """
    known = set(pack_tools)
    esc = list(escalation_tools)
    evidence: dict[str, dict[str, Any]] = {}
    previous: dict[str, tuple[int, int]] = {}
    polls = 0
    max_polls = max(1, int(max_polls))
    for poll in range(1, max_polls + 1):
        polls = poll
        for row in rows:
            sid = row["sessionId"]
            if evidence.get(sid, {}).get("stable"):
                continue
            first, spans = _session_spans(source, sid)
            called: list[dict[str, Any]] = []
            other: list[str] = []
            traces: list[str] = []
            for span in sorted(spans, key=lambda s: (str(s.get("startTimeUnixNano") or ""), _span_key(s))):
                tid = span_trace(span)
                if tid and tid not in traces:
                    traces.append(tid)
                name = tool_name(span, compact_target)
                if not name:
                    continue
                status = str(_attrs(span).get("gen_ai.tool.status") or "unknown")
                if name in known:
                    called.append({"tool": name, "status": status, "traceId": tid})
                elif name not in other:
                    other.append(name)
            names = {c["tool"] for c in called}
            expected = (cases.get(row["caseId"]) or {}).get("expected") or {}
            indexed = bool(first)
            early = (
                indexed
                and set(expected.get("requiredTools") or []) <= names
                and not (expected.get("forbiddenTools") or [])
                and not (expected.get("shouldEscalate") is True and esc)
            )
            counts = (len(first), len(spans))
            stable = indexed and (early or previous.get(sid) == counts)
            previous[sid] = counts
            evidence[sid] = {
                "indexed": indexed, "stable": stable, "traceIds": traces, "toolsCalled": called,
                "otherTools": other, "spanCounts": list(counts),
            }
        if all(evidence.get(r["sessionId"], {}).get("stable") for r in rows):
            break
        if poll < max_polls:
            sleep(poll_seconds)
    return evidence, polls


# ---------------------------------------------------------------------------
# Case evaluation
# ---------------------------------------------------------------------------


def _strings(values: Any) -> list[str]:
    return [v for v in (values or []) if isinstance(v, str) and v.strip()]


def config_from_pack(pack: Mapping[str, Any]) -> dict[str, Any]:
    """L1 marker configuration from pack.json ``l1`` (scenario evaluation.l1)."""
    declared = pack.get("l1") if isinstance(pack.get("l1"), dict) else {}
    refusal = _strings(declared.get("refusalMarkers"))
    return {
        "refusalMarkers": refusal or list(DEFAULT_REFUSAL_MARKERS),
        "refusalMarkerSource": "scenario" if refusal else "default",
        "escalationMarkers": _strings(declared.get("escalationMarkers")),
        "escalationTools": _strings(declared.get("escalationTools")),
    }


def _first_marker(text: str, markers: Iterable[str]) -> str | None:
    for marker in markers:
        if contains(text, marker)[0]:
            return marker
    return None


def evaluate_case(
    case: Mapping[str, Any],
    response: Mapping[str, Any] | None,
    evidence: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Every L1 check of one case. Returns ``{checks: [...], verdict}``."""
    cfg = dict(config or config_from_pack({}))
    expected = case.get("expected") or {}
    resp = dict(response or {"source": "missing", "text": ""})
    ev = dict(evidence or {})
    text = str(resp.get("text") or "")
    checks: list[dict[str, Any]] = []

    source = resp.get("source") or "missing"
    if source == "error":
        checks.append({"code": "response", "status": "error", "source": source, "error": resp.get("error")})
    elif source == "missing":
        checks.append({"code": "response", "status": "unverified", "source": source})
    elif not text.strip():
        checks.append({"code": "response", "status": "fail", "source": source, "note": "empty response"})
    else:
        check = {"code": "response", "status": "pass", "source": source}
        if resp.get("truncated"):
            check["note"] = "truncated at the model's token limit (the text checks read the partial answer)"
        checks.append(check)
    answered = checks[0]["status"] == "pass"

    called = [c["tool"] for c in ev.get("toolsCalled") or []]
    stable = bool(ev.get("stable"))

    required = _strings(expected.get("requiredTools"))
    if required:
        missing = [t for t in required if t not in called]
        status = "pass" if not missing else ("fail" if stable else "unverified")
        check = {"code": "requiredTools", "status": status, "expected": required, "observed": sorted(set(called))}
        if missing:
            check["missing"] = missing
        checks.append(check)

    forbidden = _strings(expected.get("forbiddenTools"))
    if forbidden:
        hit = [t for t in forbidden if t in called]
        status = "fail" if hit else ("pass" if stable else "unverified")
        check = {"code": "forbiddenTools", "status": status, "forbidden": forbidden}
        if hit:
            check["called"] = hit
        checks.append(check)

    mentions = _strings(expected.get("mustMention"))
    if mentions:
        if not answered:
            checks.append({"code": "mustMention", "status": "unverified", "note": "no usable response"})
        else:
            absent = [m for m in mentions if not contains(text, m)[0]]
            check = {"code": "mustMention", "status": "fail" if absent else "pass"}
            if absent:
                check["missing"] = absent
            checks.append(check)

    groups = [g for g in (expected.get("mustMentionAnyOf") or []) if _strings(g)]
    if groups:
        if not answered:
            checks.append({"code": "mustMentionAnyOf", "status": "unverified", "note": "no usable response"})
        else:
            unmet = [_strings(g) for g in groups if not any(contains(text, m)[0] for m in _strings(g))]
            check = {"code": "mustMentionAnyOf", "status": "fail" if unmet else "pass"}
            if unmet:
                check["missing"] = unmet
            checks.append(check)

    banned = _strings(expected.get("mustNotMention"))
    if banned:
        if not answered:
            checks.append({"code": "mustNotMention", "status": "unverified", "note": "no usable response"})
        else:
            plain: list[dict[str, str]] = []
            softened: list[dict[str, str]] = []
            for needle in banned:
                for hay, index, pin in find_hits(text, needle):
                    hit = {"needle": needle, "snippet": _snippet(hay, index, len(pin))}
                    (softened if negated(hay, index) else plain).append(hit)
            if plain:
                checks.append({"code": "mustNotMention", "status": "fail", "hits": plain[:5]})
            elif softened:
                checks.append({"code": "mustNotMention", "status": "defer", "basis": "negated-context", "hits": softened[:5]})
            else:
                checks.append({"code": "mustNotMention", "status": "pass"})

    refuse = expected.get("shouldRefuse")
    if refuse is True:
        hit = [t for t in forbidden if t in called]
        if hit:
            checks.append({"code": "shouldRefuse", "status": "fail", "basis": f"forbidden-tool:{hit[0]}"})
        elif not answered:
            checks.append({"code": "shouldRefuse", "status": "unverified", "note": "no usable response"})
        else:
            marker = _first_marker(text, cfg.get("refusalMarkers") or ())
            if marker:
                checks.append({"code": "shouldRefuse", "status": "pass", "basis": f"marker:{marker}"})
            else:
                checks.append({"code": "shouldRefuse", "status": "defer", "basis": "no-refusal-marker"})
    elif refuse is False:
        checks.append({"code": "shouldRefuse", "status": "n/a", "note": "false: an unwarranted refusal is judged by the goal judge (E2)"})

    escalate = expected.get("shouldEscalate")
    if escalate is True:
        tools = _strings(cfg.get("escalationTools"))
        markers = _strings(cfg.get("escalationMarkers"))
        used = [t for t in tools if t in called]
        if used:
            checks.append({"code": "shouldEscalate", "status": "pass", "basis": f"tool:{used[0]}"})
        elif not answered:
            checks.append({"code": "shouldEscalate", "status": "unverified", "note": "no usable response"})
        else:
            marker = _first_marker(text, markers)
            if marker:
                checks.append({"code": "shouldEscalate", "status": "pass", "basis": f"marker:{marker}"})
            else:
                basis = "undeclared" if not tools and not markers else "no-escalation-signal"
                checks.append({"code": "shouldEscalate", "status": "defer", "basis": basis})
    elif escalate is False:
        checks.append({"code": "shouldEscalate", "status": "n/a", "note": "false: an unneeded handoff is judged by the goal judge"})

    return {"checks": checks, "verdict": case_verdict(checks)}


def case_verdict(checks: Sequence[Mapping[str, Any]]) -> str:
    """error > fail > unverified > defer > pass; only a passing response (no assertion) is ``defer``."""
    statuses = [c.get("status") for c in checks if c.get("status") != "n/a"]
    if [c.get("code") for c in checks if c.get("status") != "n/a"] == ["response"] and statuses == ["pass"]:
        return "defer"
    for status in VERDICT_PRECEDENCE:
        if status in statuses:
            return status
    return "unverified"


def combined_verdict(l1_verdict: str | None, mtg_label: str | None) -> str:
    """L1 first, then the goal judge (SPEC D2): pass | fail | undetermined.

    fail/error → fail; pass → pass; defer or unverified → the judge's Pass/Fail, else undetermined;
    a missing L1 verdict → undetermined.
    """
    verdict = (l1_verdict or "").strip().lower()
    label = (mtg_label or "").strip().lower()
    if verdict in ("fail", "error"):
        return "fail"
    if verdict == "pass":
        return "pass"
    if verdict in ("defer", "unverified"):
        if label == "pass":
            return "pass"
        if label == "fail":
            return "fail"
    return "undetermined"


# ---------------------------------------------------------------------------
# Summaries, compact form, comparison
# ---------------------------------------------------------------------------


def _count_row() -> dict[str, int]:
    return {"total": 0, "pass": 0, "fail": 0, "defer": 0, "unverified": 0, "error": 0}


def summarize(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = _count_row()
    by_check: dict[str, dict[str, int]] = {}
    by_category: dict[str, dict[str, int]] = {}
    for case in cases:
        verdict = case.get("verdict", "unverified")
        summary["total"] += 1
        summary[verdict] = summary.get(verdict, 0) + 1
        row = by_category.setdefault(str(case.get("category") or "unknown"), _count_row())
        row["total"] += 1
        row[verdict] = row.get(verdict, 0) + 1
        for check in case.get("checks") or []:
            counts = by_check.setdefault(check["code"], {})
            counts[check["status"]] = counts.get(check["status"], 0) + 1
    summary["passRate"] = round(summary["pass"] / summary["total"], 4) if summary["total"] else None
    summary["byCheck"] = {code: by_check[code] for code in CHECK_CODES if code in by_check}
    summary["byCategory"] = dict(sorted(by_category.items()))
    return summary


def _codes(case: Mapping[str, Any], status: str) -> list[str]:
    return [c["code"] for c in case.get("checks") or [] if c.get("status") == status]


def compact(doc: Mapping[str, Any], limit: int = COMPACT_LIMIT) -> dict[str, Any]:
    """The compact form carried by the step outputs (at most ``limit`` characters of JSON)."""
    summary = {k: doc["summary"][k] for k in ("total", "pass", "fail", "defer", "unverified", "error")}
    summary["byCategory"] = doc["summary"].get("byCategory", {})
    cases = []
    for case in doc.get("cases") or []:
        row: dict[str, Any] = {
            "id": case["caseId"], "v": case["verdict"], "fail": _codes(case, "fail"), "defer": _codes(case, "defer"),
            "unverified": _codes(case, "unverified"), "tools": [t["tool"] for t in case.get("toolsCalled") or []],
            "session": case.get("sessionId"),
        }
        errors = _codes(case, "error")
        if errors:
            row["error"] = errors
        cases.append(row)
    out: dict[str, Any] = {
        "schema": L1_SCHEMA, "phase": doc.get("phase"), "runId": doc.get("runId"),
        "releaseVersion": doc.get("releaseVersion"), "summary": summary, "cases": cases, "truncated": False,
    }
    comparison = doc.get("comparison") or {}
    if comparison.get("flips") is not None:
        out["flips"] = {k: len(comparison["flips"].get(k, [])) for k in FLIPS}

    def size() -> int:
        return len(json.dumps(out, ensure_ascii=False, separators=(",", ":")))

    for drop in ("tools", "session"):
        if size() <= limit:
            break
        for row in cases:
            row.pop(drop, None)
    if size() > limit:
        out["cases"] = []
        out["truncated"] = True
    return out


def _combined_of(case: Mapping[str, Any]) -> str:
    return str(case.get("combined") or combined_verdict(case.get("verdict"), ((case.get("l2") or {}).get("goal") or {}).get("label")))


def _sp2(case: Mapping[str, Any]) -> float | None:
    rag = (case.get("l2") or {}).get("rag") or {}
    return _float((rag.get("metrics") or {}).get("SP2"))


def compare(doc: Mapping[str, Any], baseline: Mapping[str, Any] | None) -> dict[str, Any]:
    """Flips of the combined verdict against a baseline run of the same release."""
    if not baseline:
        return {"against": None, "skipped": "no baseline run", "flips": None, "retrievalLimited": []}
    against = baseline.get("runId")
    if baseline.get("releaseVersion") != doc.get("releaseVersion"):
        return {"against": against, "skipped": "the baseline run is from another release", "flips": None, "retrievalLimited": []}
    before = {c["caseId"]: c for c in baseline.get("cases") or []}
    flips: dict[str, list[str]] = {k: [] for k in FLIPS}
    limited: list[str] = []
    for case in doc.get("cases") or []:
        cid = case["caseId"]
        old = before.get(cid)
        now = _combined_of(case)
        was = _combined_of(old) if old else "undetermined"
        kind = {
            ("fail", "pass"): "fixed", ("pass", "fail"): "regressed",
            ("fail", "fail"): "still-failing", ("pass", "pass"): "still-passing",
        }.get((was, now), "undetermined")
        flips[kind].append(cid)
        sp2_now, sp2_was = _sp2(case), _sp2(old) if old else None
        if sp2_now is not None and sp2_was is not None and sp2_now <= RETRIEVAL_LIMIT_SP2 and sp2_was <= RETRIEVAL_LIMIT_SP2:
            limited.append(cid)
    return {"against": against, "skipped": None, "flips": flips, "retrievalLimited": limited}


def _cell(text: Any, width: int) -> str:
    cell = str(text if text is not None else "-")
    return (cell[: width - 1] + "…") if len(cell) > width else cell.ljust(width)


def _detail(case: Mapping[str, Any]) -> str:
    for status in ("error", "fail", "unverified", "defer"):
        for check in case.get("checks") or []:
            if check.get("status") != status:
                continue
            extra = check.get("missing") or check.get("called") or check.get("basis") or check.get("error") or check.get("note") or ""
            if isinstance(extra, list):
                extra = ", ".join(" | ".join(x) if isinstance(x, list) else str(x) for x in extra)
            if check.get("hits"):
                extra = ", ".join(h["needle"] for h in check["hits"])
            return f"{check['code']} {status}" + (f": {extra}" if extra else "")
    return ""


def render_table(doc: Mapping[str, Any]) -> list[str]:
    """Human-readable per-case lines (no score-line or judge-header wording; see the module docstring)."""
    lines = [
        f"  {'#':>2}  {_cell('case', 28)} {_cell('category', 10)} {_cell('L1', 10)} {_cell('detail', 44)} {_cell('GR', 6)} MtG",
        "  " + "-" * 110,
    ]
    for case in doc.get("cases") or []:
        l2 = case.get("l2") or {}
        rag, goal = l2.get("rag") or {}, l2.get("goal") or {}
        gr = rag.get("value")
        lines.append(
            f"  {case.get('index', ''):>2}  {_cell(case['caseId'], 28)} {_cell(case.get('category'), 10)} "
            f"{_cell(str(case['verdict']).upper(), 10)} {_cell(_detail(case), 44)} "
            f"{_cell(f'{gr:.2f}' if isinstance(gr, float) else '-', 6)} {goal.get('label') or '-'}"
        )
    s = doc["summary"]
    lines.append("  " + "-" * 110)
    lines.append(
        f"  L1: {s['pass']}/{s['total']} pass, {s['fail']} fail, {s['defer']} deferred to the goal judge, "
        f"{s['unverified']} unverified, {s['error']} error"
    )
    comparison = doc.get("comparison")
    if comparison:
        if comparison.get("skipped"):
            lines.append(f"  (no comparison: {comparison['skipped']})")
        elif comparison.get("flips"):
            flips = comparison["flips"]
            lines.append(f"  Against {comparison.get('against')}:")
            for kind in ("fixed", "regressed", "still-failing"):
                names = flips.get(kind) or []
                note = ""
                if kind == "still-failing" and set(names) & set(comparison.get("retrievalLimited") or []):
                    note = "   (SP2≈0 in both runs → fix retrieval / the knowledge base, not the prompt)"
                lines.append(f"    {kind}: {', '.join(names) if names else '-'}{note}")
    return lines


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------


def _load_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"cannot read {what} at {path}: {exc}") from exc


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _point_latest(link: Path, target: Path) -> None:
    tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, link)


def evaluate_run(
    *,
    pack: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    run_dir: Path,
    phase: str,
    since_ms: int,
    source: Any,
    invoke_log_dir: Path | str | None = None,
    release_version: str | None = None,
    baseline: Mapping[str, Any] | None = None,
    max_polls: int = DEFAULT_MAX_POLLS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Evaluate every recorded practice question of one run; returns the full l1.json document."""
    by_id = {str(c.get("id")): c for c in cases}
    rows = read_sessions(run_dir / "sessions.tsv", by_id)
    if not rows:
        raise InputError(f"no usable rows in {run_dir / 'sessions.tsv'}")
    config = config_from_pack(pack)
    tools = [str(t.get("name")) for t in pack.get("tools") or [] if isinstance(t, dict) and t.get("name")]
    compact_target = str((pack.get("namespace") or {}).get("toolTargetName") or "").replace("-", "")
    logs = scan_invoke_logs(invoke_log_dir, since_ms)
    evidence, polls = collect_tool_evidence(
        source, rows, by_id, compact_target=compact_target, pack_tools=tools,
        escalation_tools=config["escalationTools"], max_polls=max_polls, poll_seconds=poll_seconds, sleep=sleep,
    )
    scores = read_scores(run_dir / "scores.tsv")
    sources = {"invoke-log": 0, "stream": 0, "missing": 0, "error": 0}
    out_cases = []
    for row in rows:
        case = by_id[row["caseId"]]
        resp = response_for(row, run_dir, logs)
        sources[resp["source"]] = sources.get(resp["source"], 0) + 1
        ev = evidence.get(row["sessionId"], {})
        result = evaluate_case(case, resp, ev, config)
        l2 = scores.get(row["sessionId"], {})
        text = resp.get("text") or ""
        out_cases.append({
            "caseId": row["caseId"], "index": row["index"], "category": case.get("category"), "label": case.get("label"),
            "probe": row.get("probe"), "sessionId": row["sessionId"], "actorId": row.get("actor"),
            "traceIds": ev.get("traceIds", []), "verdict": result["verdict"], "checks": result["checks"],
            "toolsCalled": ev.get("toolsCalled", []), "otherTools": ev.get("otherTools", []),
            "responseSource": resp["source"], "responseExcerpt": text[:RESPONSE_EXCERPT], "responseChars": len(text),
            "l2": l2, "combined": combined_verdict(result["verdict"], (l2.get("goal") or {}).get("label")),
        })
    stamp = (now or (lambda: datetime.now(timezone.utc)))()
    doc: dict[str, Any] = {
        "schema": L1_SCHEMA, "status": "ok", "packId": pack.get("packId"), "releaseVersion": release_version,
        "phase": phase, "runId": run_dir.name, "generatedAt": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sinceEpochMs": since_ms,
        "config": {
            "refusalMarkers": {"source": config["refusalMarkerSource"], "count": len(config["refusalMarkers"])},
            "escalationMarkers": config["escalationMarkers"], "escalationTools": config["escalationTools"],
        },
        "evidence": {
            "sessions": len(rows), "sessionsIndexed": sum(1 for e in evidence.values() if e.get("indexed")),
            "sessionsStable": sum(1 for e in evidence.values() if e.get("stable")), "polls": polls, "responses": sources,
        },
        "cases": out_cases,
    }
    doc["summary"] = summarize(out_cases)
    if phase != "baseline":
        doc["comparison"] = compare(doc, baseline)
    return doc


def _source(args: argparse.Namespace, since_ms: int) -> Any:
    spans = args.spans_jsonl or os.environ.get("WORKSHOP_L1_SPANS_JSONL")
    if spans:
        return JsonlSpanSource(spans)
    import boto3  # lazy: only the release CLI on the Workshop host reads CloudWatch

    return LogsSpanSource(boto3.client("logs", region_name=args.region or None), since_ms)


def _env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="l1_eval.py", description="L1 scenario assertions for one Guide evaluation run")
    p.add_argument("--release-dir", help="release root (reads pack/ and RELEASE.json)")
    p.add_argument("--pack-dir", help="compiled pack directory (default <release-dir>/pack)")
    p.add_argument("--release-manifest", help="RELEASE.json (default <release-dir>/RELEASE.json)")
    p.add_argument("--cases", help="golden cases JSON (default <pack-dir>/golden/practice.json)")
    p.add_argument("--run-dir", required=True, help="run record directory (sessions.tsv, q<i>.out, scores.tsv)")
    p.add_argument("--phase", choices=PHASES, default="baseline")
    p.add_argument("--since-ms", type=int, default=0)
    p.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION"))
    p.add_argument("--invoke-log-dir")
    p.add_argument("--compare-to", help="baseline l1.json (default <eval root>/baseline-latest/l1.json)")
    p.add_argument("--export-compact", help="compact JSON path (default $WORKSHOP_EVAL_OUT_DIR/l1-compact.json)")
    p.add_argument("--spans-jsonl", help="read spans from a local JSON-lines file instead of CloudWatch Logs")
    p.add_argument("--max-polls", type=int, default=int(_env_number("WORKSHOP_L1_MAX_POLLS", DEFAULT_MAX_POLLS)))
    p.add_argument("--poll-seconds", type=float, default=_env_number("WORKSHOP_L1_POLL_SECONDS", DEFAULT_POLL_SECONDS))
    return p


def run(argv: Sequence[str] | None = None, *, source: Any = None, sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None, out: Any = None) -> dict[str, Any]:
    """CLI body without the exit-code mapping (tests call it directly)."""
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    release = Path(args.release_dir).resolve() if args.release_dir else None
    pack_dir = Path(args.pack_dir) if args.pack_dir else (release / "pack" if release else None)
    if pack_dir is None:
        raise InputError("--release-dir or --pack-dir is required")
    pack = _load_json(pack_dir / "pack.json", "pack.json")
    cases = _load_json(Path(args.cases) if args.cases else pack_dir / "golden" / "practice.json", "the practice cases")
    if not isinstance(cases, list):
        raise InputError("the practice cases file must hold a JSON list")
    manifest_path = Path(args.release_manifest) if args.release_manifest else (release / "RELEASE.json" if release else None)
    version = None
    if manifest_path is not None and manifest_path.is_file():
        version = (_load_json(manifest_path, "RELEASE.json") or {}).get("version")
    run_dir = Path(args.run_dir)
    root = run_dir.parent
    baseline = None
    if args.phase != "baseline":
        compare_path = Path(args.compare_to) if args.compare_to else root / "baseline-latest" / "l1.json"
        if compare_path.is_file():
            try:
                baseline = json.loads(compare_path.read_text(encoding="utf-8"))
            except ValueError:
                baseline = None
    since = int(args.since_ms or 0)
    doc = evaluate_run(
        pack=pack, cases=cases, run_dir=run_dir, phase=args.phase, since_ms=since,
        source=source if source is not None else _source(args, since), invoke_log_dir=args.invoke_log_dir,
        release_version=version, baseline=baseline, max_polls=args.max_polls, poll_seconds=args.poll_seconds,
        sleep=sleep, now=now,
    )
    _write_json(run_dir / "l1.json", doc)
    target = args.export_compact or (
        os.path.join(os.environ["WORKSHOP_EVAL_OUT_DIR"], "l1-compact.json") if os.environ.get("WORKSHOP_EVAL_OUT_DIR") else None
    )
    if target:
        _write_json(Path(target), compact(doc))
    if args.phase == "baseline":
        _point_latest(root / "baseline-latest", run_dir.resolve())
    for line in render_table(doc):
        print(line, file=out)
    print(f"  (per-case detail: {run_dir / 'l1.json'})", file=out)
    return doc


def main(argv: Sequence[str] | None = None) -> int:
    try:
        run(argv)
    except InputError as exc:
        print(f"  L1 input error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - the Guide step must never crash on the checker
        print(f"  L1 internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
