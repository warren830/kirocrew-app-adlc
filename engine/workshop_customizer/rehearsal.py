"""Rehearsal: the single owner of the teaching verdict rules, the class verdict and ``readyForClass``.

SPEC D2 / D8. Two layers, one module:

* **Rules** — :func:`teaching_contrast` judges the ``labs.teaching`` phenomena on ONE Guided Run,
  over :func:`guided_report.case_table`, with the ``teaching.THRESHOLDS`` helpers and
  :func:`l1.combined_verdict`. ``report.teachingContrast`` is this function's output: guided_report
  re-exports it and implements no rule of its own. The view carries no readiness boolean.
* **Rehearsal** — :func:`build_rehearsal` aggregates the runs of the same release (the current Guided
  Run plus the archived ``run/history/*/state.json``). A phenomenon counts as reproduced when it
  reproduced in the **latest complete run**; every run's result and the consistency across the
  complete runs are reported. Each failure maps to a remediation on a concrete asset (a knowledge
  document, the baseline or candidate prompt, a golden case, ``labs.teaching``, the judge, ops).
  ``readyForClass`` = verdict ``ready`` ∧ the current run's report complete ∧ both guides built
  (:func:`guide_status`), judged on the build snapshot of the release the run is bound to (the CLI
  can judge other inputs, never ready for class). Nothing else in the engine or the app sets it.

Both layers read the scenario **as the release was built** (the build snapshot), never the live
``scenario.yaml``. Pure and deterministic: no clock reads, no network, no writes (the app persists
``run/rehearsal.json``). Holdout execution is out of scope (SPEC D9): only practice evidence is read.

Every human-readable string (case reasons, the verdict reason, remediation actions and becauses, advisory
notes, warnings, readiness blocker texts, the scope warning) is a template of :mod:`rehearsal_strings`,
rendered in the pack language (``language``: ``zh-*`` → zh-CN, else en) with an English twin (``<field>En``);
this module writes no prose. The machine codes are the same in every language.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import rehearsal_strings as rs
from . import teaching, teaching_policy
from .l1 import combined_verdict, contains
from .rehearsal_strings import Given, Join, Msg
from .script_facts import UPSTREAM_MAX_ITERATIONS

# guided_report imports the rules below at module level; this module imports guided_report (case table,
# evidence gates, report) only inside the rehearsal functions, so neither import is circular.

# ---------------------------------------------------------------------------
# Rules: the teaching contrast of one run (report.teachingContrast)
# ---------------------------------------------------------------------------

TEACHING_CONTRAST_SCHEMA = "workshop-customizer/teaching-contrast/1"
#: Status vocabulary of a teaching phenomenon (and of the current run) in the contrast view.
CONTRAST_STATUSES: tuple[str, ...] = ("reproduced", "not_reproduced", "insufficient_evidence")
#: The L1 checks that decide each L1 phenomenon kind (only those the case declares count).
FOCUS_CHECKS: Mapping[str, tuple[str, ...]] = {
    "tool_use": ("requiredTools", "forbiddenTools"),
    "refusal": ("shouldRefuse", "forbiddenTools", "mustNotMention"),
    "escalation": ("shouldEscalate",),
}
#: The L1 checks that show an absent-gap answer admits the gap / hands off (advisory reading).
HONESTY_CHECKS: tuple[str, ...] = ("shouldEscalate", "mustMention", "mustMentionAnyOf")
#: The L1 checks that read the reply text. When a reply is empty (L1 ``response: fail``) L1 leaves them unread
#: (unverified) and they count as failed for the teaching verdicts: Mind the Goal does not resolve them (live
#: 2026-09-29: it passed a turn that ended with no text). Span checks (requiredTools, forbiddenTools, a
#: shouldEscalate passed through an escalation tool) keep their own status.
TEXT_CHECKS: tuple[str, ...] = ("shouldRefuse", "shouldEscalate", "mustMention", "mustMentionAnyOf", "mustNotMention")
#: report.teachingContrast.readiness (English; the view carries it in the pack language plus readinessEn).
READINESS_NOTE = rs.text("en", "contrast.readiness")
#: Readings of the advisory checks (they never decide the contrast).
NOISE_READINGS: tuple[str, ...] = ("observed", "not_observed", "no_evidence")
MEMORY_READINGS: tuple[str, ...] = ("generic", "personalized", "unknown")
#: Readings of an absent-gap answer (retrieval_gap case ``honesty``; machine values, never localized).
HONESTY_READINGS: tuple[str, ...] = ("admits the gap", "does not admit the gap", "unknown", "no answer")
ADMITS, DOES_NOT_ADMIT, HONESTY_UNKNOWN, NO_ANSWER = HONESTY_READINGS
#: A baselineMarker that asks EVERY answer to go beyond the documents (the kind that bites live), as opposed to
#: "fill in when the documents are silent" (loyalty-points r1), which still answers from the sources.
ALWAYS_MARKER = re.compile(
    r"(?i)\b(?:in|for)\s+(?:every|each|any|all)\s+(?:answers?|responses?|replies|repl(?:y|ies)|questions?)\b"
    r"|\b(?:every|each|any|all)\s+(?:answers?|responses?|replies|reply)\b|\bwhenever\s+you\s+answer\b"
    r"|\balways\b[^.;]{0,60}\b(?:beyond|outside)\b|每次回答|每一次回答|每个回答|每一个回答|每条回答|任何回答|所有回答|无论[^，。；]{0,20}都")
#: ... unless it only applies when the documents are silent (the kind that did not bite).
SILENT_CONDITION = re.compile(
    r"(?i)\b(?:if|when|where)\b[^.;]{0,60}\b(?:silent|do(?:es)?\s*n[o']t\s+(?:cover|say|answer|state)|not\s+(?:cover|stated|in\s+the))"
    r"|(?:如果|若|当)[^，。；]{0,30}(?:没写|没有写|未写|没有说明|未涉及|没有涉及|没有提到|未提及)|(?:没写|未写明|没有写明)[^，。；]{0,6}时")


def marker_bites(marker: str) -> bool:
    """A baselineMarker that asks EVERY answer to leave the documents (not only when they are silent)."""
    return bool(ALWAYS_MARKER.search(marker)) and not SILENT_CONDITION.search(marker)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _practice(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in teaching.practice_cases(scenario) if isinstance(c.get("id"), str)]


def _metric(thelma: Mapping[str, Any] | None, key: str) -> float | None:
    value = ((thelma or {}).get("metrics") or {}).get(key)
    return float(value) if _number(value) else None


def _thelma_brief(thelma: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if thelma is None:
        return None
    return {"GR": thelma["value"], "label": thelma["label"], "SP2": _metric(thelma, "SP2"), "SQC": _metric(thelma, "SQC")}


def _fmt(value: float | None) -> str | Msg:
    return Msg("value.none") if value is None else f"{value:g}"


def _threshold(name: str) -> str | Msg:
    return _fmt(teaching.THRESHOLDS[name])


def _run_label(run: str) -> Msg:
    return Msg(f"run.{run}")


def _verdict(case_id: str, status: str, code: str, reason: Msg, **extra: Any) -> dict[str, Any]:
    """One case verdict; ``reason`` (and a later ``warning``) is rendered by :func:`rs.localize`."""
    return {"caseId": case_id, "status": status, "code": code, "reason": reason, **extra}


def _band_gate(case_id: str, band: float | None, *, required: bool, **extra: Any) -> dict[str, Any] | None:
    if band is None and required:
        return _verdict(case_id, "insufficient_evidence", "NOISE_BAND_MISSING", Msg("case.NOISE_BAND_MISSING"), **extra)
    if teaching.judge_too_noisy(band):
        return _verdict(case_id, "insufficient_evidence", teaching.JUDGE_TOO_NOISY,
                        Msg("case.JUDGE_TOO_NOISY", band=_fmt(band), max=_threshold("maxUsableBand")), **extra)
    return None


def _prompt_fixable_case(row: Mapping[str, Any], band: float | None) -> dict[str, Any]:
    cid = row["caseId"]
    b, o = row["baseline"]["thelma"], row["optimized"]["thelma"]
    extra: dict[str, Any] = {"baseline": _thelma_brief(b), "optimized": _thelma_brief(o), "delta": None}
    gated = _band_gate(cid, band, required=True, **extra)
    if gated:
        return gated
    if b is None:
        return _verdict(cid, "insufficient_evidence", "PF_NOT_SCORED", Msg("case.NOT_SCORED", run=_run_label("baseline")), **extra)
    if teaching.gr_pass(b["value"]):
        return _verdict(cid, "not_reproduced", "PF_BASELINE_ALREADY_PASSES",
                        Msg("case.PF_BASELINE_ALREADY_PASSES", gr=_fmt(b["value"]), bar=_threshold("grPass")), **extra)
    if o is None:
        return _verdict(cid, "insufficient_evidence", "PF_NOT_SCORED", Msg("case.NOT_SCORED", run=_run_label("optimized")), **extra)
    extra["delta"] = round(o["value"] - b["value"], 6)
    retrieval = teaching.retrieval_ok(b.get("metrics"), o.get("metrics"))
    if retrieval is None:
        return _verdict(cid, "insufficient_evidence", "METRICS_MISSING", Msg("case.PF_METRICS_MISSING"), **extra)
    if not retrieval:
        best = max(v for v in (_metric(b, "SQC"), _metric(o, "SQC")) if v is not None)
        return _verdict(cid, "not_reproduced", "PF_RETRIEVAL_FAILED",
                        Msg("case.PF_RETRIEVAL_FAILED", sqc=_fmt(best), bar=_threshold("retrievalOkSqc")), **extra)
    bar = teaching.improvement_bar(band)
    if not teaching.is_improvement(extra["delta"], band):
        return _verdict(cid, "not_reproduced", "PF_NO_GAIN",
                        Msg("case.PF_NO_GAIN", delta=_fmt(extra["delta"]), min=_threshold("minImprovement"), bar=_fmt(bar)), **extra)
    verdict = _verdict(cid, "reproduced", "PF_REPRODUCED",
                       Msg("case.PF_REPRODUCED", baseline=_fmt(b["value"]), optimized=_fmt(o["value"]), delta=_fmt(extra["delta"]),
                           bar=_fmt(bar)), **extra)
    if str(o.get("label") or "").lower() != "pass":
        verdict["warning"] = Msg("case.PF_OPTIMIZED_NOT_PASS", label=str(o.get("label")))
    return verdict


def _retrieval_failed_reason(run: str, thelma: Mapping[str, Any]) -> Msg:
    return Msg("case.RG_RETRIEVAL_OK", run=_run_label(run), sp2=_fmt(_metric(thelma, "SP2")), sqc=_fmt(_metric(thelma, "SQC")),
               sp2max=_threshold("retrievalFailedSp2"), sqcmax=_threshold("retrievalFailedSqc"))


def _declared(case: Mapping[str, Any], code: str) -> bool:
    value = (case.get("expected") or {}).get(code)
    return value is True or (isinstance(value, list) and bool(value))


def _empty_reply(cell: Mapping[str, Any]) -> bool:
    """The phase's reply was logged with no text (L1 ``response: fail``)."""
    return "response" in ((cell.get("l1") or {}).get("fail") or [])


def _unread(cell: Mapping[str, Any], codes: Iterable[str]) -> list[str]:
    """The text checks of ``codes`` an empty reply left to Mind the Goal (defer/unverified): they count as failed."""
    l1 = cell.get("l1") or {}
    if not _empty_reply(cell):
        return []
    return [code for code in codes if code in TEXT_CHECKS and (code in (l1.get("defer") or []) or code in (l1.get("unverified") or []))]


def _empty_reply_fails(cell: Mapping[str, Any], codes: Sequence[str]) -> list[str]:
    """The checks of ``codes`` that fail only because the reply is empty (no check failed on what the agent did or
    said); ``[]`` otherwise."""
    if any(code in ((cell.get("l1") or {}).get("fail") or []) for code in codes):
        return []
    return _unread(cell, codes)


def _focus(cell: Mapping[str, Any], codes: Iterable[str]) -> str | None:
    """pass | fail | error | unresolved for the given L1 checks of one phase; ``None`` without L1. After an empty
    reply the text checks it left unread fail (:data:`TEXT_CHECKS`) instead of going to Mind the Goal."""
    l1 = cell.get("l1")
    if l1 is None:
        return None
    codes = tuple(codes)
    if any(code in l1["fail"] for code in codes) or _unread(cell, codes):
        return "fail"
    if l1.get("verdict") == "error" or any(code in l1["error"] for code in codes):
        return "error"
    if any(code in l1["defer"] or code in l1["unverified"] for code in codes):
        resolved = combined_verdict("defer", (cell.get("mtg") or {}).get("label"))
        return resolved if resolved in ("pass", "fail") else "unresolved"
    return "pass"


def _honesty(cell: Mapping[str, Any], case: Mapping[str, Any]) -> str:
    # An empty reply (L1 response: fail) admits nothing, whatever Mind the Goal made of the trace (live
    # 2026-09-29: ~30 retrievals, then a turn with no text).
    if _empty_reply(cell):
        return NO_ANSWER
    codes = [code for code in HONESTY_CHECKS if _declared(case, code)]
    focus = _focus(cell, codes) if codes else None
    return {"pass": ADMITS, "fail": DOES_NOT_ADMIT}.get(focus or "", HONESTY_UNKNOWN)


def _retrieval_gap_case(row: Mapping[str, Any], case: Mapping[str, Any], mechanism: str, band: float | None) -> dict[str, Any]:
    cid = row["caseId"]
    b, o = row["baseline"]["thelma"], row["optimized"]["thelma"]
    extra: dict[str, Any] = {"baseline": _thelma_brief(b), "optimized": _thelma_brief(o)}
    if mechanism == "absent":
        extra["honesty"] = _honesty(row["optimized"], case)
    gated = _band_gate(cid, band, required=False, **extra)
    if gated:
        return gated
    for run, thelma in (("baseline", b), ("optimized", o)):
        if thelma is None:
            return _verdict(cid, "insufficient_evidence", "RG_NOT_SCORED", Msg("case.NOT_SCORED", run=_run_label(run)), **extra)
        failed = teaching.retrieval_failed(thelma.get("metrics"))
        if failed is None:
            return _verdict(cid, "insufficient_evidence", "METRICS_MISSING", Msg("case.RG_METRICS_MISSING", run=_run_label(run)), **extra)
        if not failed:
            return _verdict(cid, "not_reproduced", f"RG_RETRIEVAL_OK_{run.upper()}", _retrieval_failed_reason(run, thelma), **extra)
    if mechanism == "buried":
        if str(o.get("label") or "").lower() != "fail":
            return _verdict(cid, "not_reproduced", "RG_OPTIMIZED_RESOLVED",
                            Msg("case.RG_OPTIMIZED_RESOLVED", label=str(o.get("label"))), **extra)
        return _verdict(cid, "reproduced", "RG_REPRODUCED", Msg("case.RG_REPRODUCED.buried"), **extra)
    admitted = extra["honesty"] == ADMITS
    return _verdict(cid, "reproduced", "RG_REPRODUCED", Msg("case.RG_REPRODUCED.absent_admitted" if admitted else "case.RG_REPRODUCED.absent"),
                    **extra)


def _l1_case(row: Mapping[str, Any], case: Mapping[str, Any], kind: str, design: str) -> dict[str, Any]:
    cid = row["caseId"]
    codes = [code for code in FOCUS_CHECKS[kind] if _declared(case, code)]
    fb, fo = _focus(row["baseline"], codes), _focus(row["optimized"], codes)
    extra: dict[str, Any] = {
        "checks": codes,
        "baseline": {"focus": fb, "l1": (row["baseline"]["l1"] or {}).get("verdict"), "mtg": (row["baseline"]["mtg"] or {}).get("label")},
        "optimized": {"focus": fo, "l1": (row["optimized"]["l1"] or {}).get("verdict"), "mtg": (row["optimized"]["mtg"] or {}).get("label")},
    }
    if not codes:
        return _verdict(cid, "insufficient_evidence", "L1_NO_FOCUS_CHECK",
                        Msg("case.L1_NO_FOCUS_CHECK", kind=kind, checks=Join(FOCUS_CHECKS[kind])), **extra)

    def unusable(focus: str | None, run: str) -> dict[str, Any] | None:
        if focus in (None, "error", "unresolved"):
            code = {None: "L1_MISSING", "error": "L1_ERROR", "unresolved": "L1_UNRESOLVED"}[focus]
            return _verdict(cid, "insufficient_evidence", code, Msg(f"case.{code}", run=_run_label(run)), **extra)
        return None

    if design == "contrast":
        blocked = unusable(fb, "baseline")
        if blocked:
            return blocked
        if fb == "pass":
            return _verdict(cid, "not_reproduced", "L1_BASELINE_ALREADY_PASSES", Msg("case.L1_BASELINE_ALREADY_PASSES"), **extra)
    blocked = unusable(fo, "optimized")
    if blocked:
        return blocked
    if fo == "fail":
        unread = _empty_reply_fails(row["optimized"], codes)
        if unread:  # an empty reply is its own failure: the fix is the reply, not the rule the case checks
            return _verdict(cid, "not_reproduced", "L1_EMPTY_RESPONSE", Msg("case.L1_EMPTY_RESPONSE", checks=Join(unread)), **extra)
        return _verdict(cid, "not_reproduced", "L1_OPTIMIZED_FAILS", Msg("case.L1_OPTIMIZED_FAILS"), **extra)
    if design == "contrast":
        return _verdict(cid, "reproduced", "L1_CONTRAST_REPRODUCED", Msg("case.L1_CONTRAST_REPRODUCED"), **extra)
    return _verdict(cid, "reproduced", "L1_CONTROL_HELD", Msg("case.L1_CONTROL_HELD"), **extra)


def _aggregate(case_verdicts: list[dict[str, Any]], need: int) -> str:
    reproduced = sum(v["status"] == "reproduced" for v in case_verdicts)
    insufficient = sum(v["status"] == "insufficient_evidence" for v in case_verdicts)
    if not case_verdicts:
        return "insufficient_evidence"
    if reproduced >= need:
        return "reproduced"
    if reproduced + insufficient >= need:
        return "insufficient_evidence"
    return "not_reproduced"


def _group(statuses: list[str], *, every: bool) -> str:
    if not statuses:
        return "missing"
    if every:
        if all(s == "reproduced" for s in statuses):
            return "reproduced"
        return "not_reproduced" if "not_reproduced" in statuses else "insufficient_evidence"
    if "reproduced" in statuses:
        return "reproduced"
    return "insufficient_evidence" if "insufficient_evidence" in statuses else "not_reproduced"


def _noise_reading(observations: Sequence[Mapping[str, Any]], gap_cases: set[str]) -> str:
    """Advisory, threshold-free: chunk precision above fact precision on a probe that is not a gap.

    ``SP1 > SP2`` is the teaching point itself ("the chunks look on topic but carry off-topic lines");
    retrieval-gap probes are left out because their low SP2 is the gap, not noise.
    """
    usable = [o for o in observations if o.get("caseId") not in gap_cases and _number(o.get("SP1")) and _number(o.get("SP2"))]
    if not usable:
        return "no_evidence"
    return "observed" if any(o["SP1"] > o["SP2"] for o in usable) else "not_observed"


def _memory_view(scenario: dict[str, Any], conversation: Mapping[str, Any] | None) -> dict[str, Any] | None:
    first = teaching.first_conversation(scenario)
    if first is None:
        return None
    terms = [t for t in first.get("mustNotMention") or [] if isinstance(t, str)]
    view: dict[str, Any] = {"id": "first-conversation", "kind": "memory", "status": "advisory",
                            "teachingPoint": first.get("teachingPoint"), "mustNotMention": terms, "hits": [], "reading": "unknown"}
    response = (conversation or {}).get("response") if isinstance(conversation, dict) else None
    if not isinstance(response, str) or not response.strip():
        view["evidence"] = "missing"
        return view
    view["evidence"] = "present"
    view["sessionId"] = conversation.get("sessionId")
    view["hits"] = [{"term": term, "snippet": snippet} for term in terms for found, snippet in [contains(response, term)] if found]
    # Where else the agent got a value (freight-claims, live 2026-09-29: a word of the question, and the tool
    # description's example tracking number): then the answer is not memory, and a fresh actorId changes nothing.
    tools = {str(t.get("name")): json.dumps({"description": t.get("description"), "inputSchema": t.get("inputSchema")},
                                            ensure_ascii=False) for t in scenario.get("tools") or [] if isinstance(t, dict)}
    for hit in view["hits"]:
        if contains(str(first.get("query") or ""), hit["term"])[0]:
            hit["given"] = "query"
        else:
            hit["given"] = next((f"tool:{name}" for name, spec in tools.items() if contains(spec, hit["term"])[0]), None)
    if terms:
        view["reading"] = "personalized" if view["hits"] else "generic"
    return view


def teaching_contrast(
    scenario: dict[str, Any],
    table: Mapping[str, Any],
    *,
    band: float | None,
    band_source: str = "",
    conversation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The labs.teaching phenomena judged on the current run (a view; no readiness boolean).

    Rules (SPEC D2, thresholds from ``teaching.THRESHOLDS``):

    * prompt_fixable case: baseline GR < grPass ∧ GR gain > max(band, minImprovement) ∧
      max(SQC) >= retrievalOkSqc; the phenomenon is reproduced when at least one case is.
    * retrieval_gap case: retrieval failed (SP2 <= 0.2 or SQC < 0.3) in both runs, and for a
      ``buried`` gap the optimized label is Fail; an ``absent`` gap ignores the label and reports
      whether the optimized answer admits the gap (``no answer`` for an empty reply). Every case
      must reproduce.
    * tool_use / refusal / escalation case: ``control`` — the optimized focus checks pass;
      ``contrast`` — the baseline fails and the optimized run passes. L1 defer/unverified are
      resolved by Mind the Goal through ``l1.combined_verdict``, else insufficient; after an empty
      reply the text checks fail instead (:data:`TEXT_CHECKS`), and an optimized run that fails
      only for that is ``L1_EMPTY_RESPONSE``. Every case.
    * a noise band above maxUsableBand makes THELMA-judged cases insufficient (JUDGE_TOO_NOISY).
    * the current run reproduces the contrast when ≥1 prompt_fixable and ≥1 retrieval_gap
      phenomenon and every L1 phenomenon reproduce; noise_grounding and the first conversation
      (memory) are advisory readings that never decide it.

    The case reasons, warnings, advisory notes and ``readiness`` are in the pack language
    (``language``) with an English twin (``reasonEn``, ``warningEn``, ``noteEn``, ``readinessEn``).
    """
    lang = rs.lang_key(scenario.get("language"))
    base = {"schema": TEACHING_CONTRAST_SCHEMA, "scope": "current run", "language": lang, "readiness": Msg("contrast.readiness")}
    if not teaching.declared(scenario):
        return rs.localize({**base, "currentRun": "undeclared", "phenomena": [], "advisory": []}, lang)
    rows = {row["caseId"]: row for row in table.get("cases") or []}
    cases = {c["id"]: c for c in _practice(scenario)}
    gap_cases = {cid for p in teaching.phenomena(scenario, "retrieval_gap") for cid in p.get("caseIds") or []}
    decisive: list[dict[str, Any]] = []
    advisory: list[dict[str, Any]] = []
    for phenomenon in teaching.phenomena(scenario):
        kind = phenomenon.get("kind")
        entry: dict[str, Any] = {"id": phenomenon.get("id"), "kind": kind, "caseIds": list(phenomenon.get("caseIds") or []),
                                 "teachingPoint": phenomenon.get("teachingPoint")}
        if kind in teaching.ADVISORY_KINDS:
            observations = [{"caseId": row["caseId"], "SP1": _metric(row["baseline"]["thelma"], "SP1"), "SP2": _metric(row["baseline"]["thelma"], "SP2")}
                            for row in rows.values() if row.get("probe") and row["baseline"]["thelma"] is not None]
            advisory.append({**entry, "status": "advisory", "documentIds": list(phenomenon.get("documentIds") or []),
                             "observations": observations, "reading": _noise_reading(observations, gap_cases),
                             "note": Msg("advisory.noise_note")})
            continue
        verdicts: list[dict[str, Any]] = []
        for cid in entry["caseIds"]:
            row = rows.get(cid)
            if row is None:
                verdicts.append(_verdict(cid, "insufficient_evidence", "CASE_NOT_PRACTICE", Msg("case.CASE_NOT_PRACTICE")))
            elif kind == "prompt_fixable":
                verdicts.append(_prompt_fixable_case(row, band))
            elif kind == "retrieval_gap":
                entry["mechanism"] = phenomenon.get("mechanism")
                verdicts.append(_retrieval_gap_case(row, cases.get(cid) or {}, str(phenomenon.get("mechanism")), band))
            elif kind in FOCUS_CHECKS:
                entry["design"] = teaching.design(phenomenon)
                verdicts.append(_l1_case(row, cases.get(cid) or {}, kind, entry["design"]))
            else:
                verdicts.append(_verdict(cid, "insufficient_evidence", "UNSUPPORTED_KIND", Msg("case.UNSUPPORTED_KIND", kind=repr(kind))))
        need = 1 if kind == "prompt_fixable" else len(verdicts)
        decisive.append({**entry, "status": _aggregate(verdicts, need), "cases": verdicts})
    groups = {
        "prompt_fixable": _group([p["status"] for p in decisive if p["kind"] == "prompt_fixable"], every=False),
        "retrieval_gap": _group([p["status"] for p in decisive if p["kind"] == "retrieval_gap"], every=False),
        "l1": _group([p["status"] for p in decisive if p["kind"] in FOCUS_CHECKS], every=True),
    }
    values = list(groups.values())
    if any(v in ("not_reproduced", "missing") for v in values):
        current = "not_reproduced"
    elif "insufficient_evidence" in values:
        current = "insufficient_evidence"
    else:
        current = "reproduced"
    memory = _memory_view(scenario, conversation)
    if memory is not None:
        advisory.append(memory)
    return rs.localize({
        **base,
        "currentRun": current,
        "groups": groups,
        "noiseBand": band,
        "noiseBandSource": band_source,
        "improvementBar": teaching.improvement_bar(band),
        "thresholds": dict(teaching.THRESHOLDS),
        "phenomena": decisive,
        "advisory": advisory,
    }, lang)


# ---------------------------------------------------------------------------
# Rehearsal: same-release runs → class verdict, remediation, readyForClass
# ---------------------------------------------------------------------------

#: Version 2 writes every human-readable text in the pack language (``language``) with an English twin
#: (``<field>En``) and adds ``readiness.blockerTexts``. Version 1 (the English-only engine; the recorded
#: acceptance evidence) keeps its own schema file, so a record validates against the version it declares;
#: the UI, the guide and the repair findings fall back to the text of a record without twins.
SCHEMA = "workshop-customizer/rehearsal/2"
SCHEMA_PATH = Path(__file__).parent / "schemas" / "rehearsal.schema.json"
SCHEMA_V1 = "workshop-customizer/rehearsal/1"
SCHEMA_PATHS: Mapping[str, Path] = {SCHEMA: SCHEMA_PATH, SCHEMA_V1: Path(__file__).parent / "schemas" / "rehearsal-1.schema.json"}
VERDICTS: tuple[str, ...] = ("ready", "not_ready", "insufficient_evidence")
#: A run's contrast (teaching_contrast currentRun) → the rehearsal verdict.
RUN_VERDICT: Mapping[str, str] = {
    "reproduced": "ready",
    "not_reproduced": "not_ready",
    "insufficient_evidence": "insufficient_evidence",
    "undeclared": "not_ready",
}
CONSISTENCY: tuple[str, ...] = ("none", "single_run", "consistent", "mixed")
#: The guides SPEC D10 builds (P3): the release-root student guide and the instructor-bundle guide.
GUIDE_FILES: Mapping[str, tuple[str, ...]] = {"student": ("release", "README.md"), "instructor": ("instructor", "instructor-guide.md")}
#: The most recent distinct same-release runs rehearsal reads (the current run included).
HISTORY_LIMIT = 10
#: CLI / e2e exit codes. 4: the verdict is ready but the class conditions (complete report, guides) are not met.
EXIT_CODES: Mapping[str, int] = {"ready": 0, "insufficient_evidence": 2, "not_ready": 3}
EXIT_READY_NOT_FOR_CLASS = 4
SCENARIO_FILE = "scenario.yaml"
#: remediation[].asset.kind → the generation scopes (validator.SCOPES) a repair of that asset touches.
ASSET_SCOPES: Mapping[str, tuple[str, ...]] = {
    "kb_doc": ("knowledge",),
    "baseline_prompt": ("prompts",),
    "candidate_prompt": ("prompts",),
    "golden_case": ("golden",),
    "teaching": ("labs",),
    "tool_fixture": ("tools",),
    "judge": (),
    "ops": (),
    "run": (),
    "guide": ("guides",),
}
SEVERITIES: tuple[str, ...] = ("blocking", "advisory")
#: The retrieval calls per question a candidate prompt allows before it hands off (app/backend/contracts/teaching.md).
#: An empty reply after more calls shows that the candidate's cap, if it has one, did not hold.
RETRIEVAL_CAP = 3
#: inputs.scenario: the build snapshot of the release the run executed (the app; the CLI on a project
#: directory or with --build-dir), or a bare scenario file (the CLI without --build-dir). Only the build
#: snapshot, with the run's release verified against the build, can be ready for class (SPEC D8).
SCENARIO_SOURCES: tuple[str, ...] = ("build snapshot", "scenario file")
#: readiness.blockers vocabulary; readyForClass is true exactly when none applies.
BLOCKERS: tuple[str, ...] = ("VERDICT_NOT_READY", "VERDICT_INSUFFICIENT_EVIDENCE", "REPORT_INCOMPLETE", "GUIDES_MISSING",
                             "SCENARIO_NOT_BUILD_SNAPSHOT", "RELEASE_NOT_VERIFIED")
#: warnings[].code vocabulary (_warnings).
WARNINGS: tuple[str, ...] = ("DECISIVE_RUN_ARCHIVED", "VERDICT_MIXED", "PHENOMENON_MIXED", "PF_OPTIMIZED_NOT_PASS", "ABSENT_GAP_NOT_ADMITTED",
                             "ABSENT_GAP_NO_ANSWER", "MTG_REFUSAL_CONFLICT")
#: scopeWarning in English (the document carries it in the pack language plus scopeWarningEn).
SCOPE_WARNING = rs.text("en", "scope_warning")


#: The build's integrity records the guides are checked against (compiler checksums, render manifest).
BUILD_CHECKSUMS = "checksums.json"
RELEASE_MANIFEST = "RELEASE.json"
#: A guide's entry in build/checksums.json (the compiled pack) and, for the student guide, in RELEASE.json.
GUIDE_CHECKSUM_KEYS: Mapping[str, str] = {"student": "pack/labs/student-guide.md", "instructor": "instructor/instructor-guide.md"}


def _file_digest(path: Path) -> str | None:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _recorded_files(path: Path) -> Mapping[str, Any]:
    try:
        files = json.loads(path.read_text(encoding="utf-8")).get("files")
    except (OSError, ValueError, AttributeError):
        return {}
    return files if isinstance(files, dict) else {}


def guide_status(build_dir: Path | str | None) -> dict[str, bool]:
    """Which of the two guides are built **and verified** in the current build.

    The instructor guide (``build/instructor/instructor-guide.md``) must match its build/checksums.json
    entry; the student guide (``build/release/README.md``) must match the compiled
    ``pack/labs/student-guide.md`` entry of build/checksums.json and the ``README.md`` entry of
    build/release/RELEASE.json. A guide that exists but changed after the build does not count: the
    app refuses to serve or export it.
    """
    if build_dir is None:
        return {name: False for name in GUIDE_FILES}
    base = Path(build_dir)
    checksums = _recorded_files(base / BUILD_CHECKSUMS)
    manifest = _recorded_files(base / "release" / RELEASE_MANIFEST)
    status: dict[str, bool] = {}
    for name, parts in GUIDE_FILES.items():
        digest = _file_digest(base.joinpath(*parts))
        ok = digest is not None and digest == checksums.get(GUIDE_CHECKSUM_KEYS[name])
        if name == "student":
            ok = ok and digest == manifest.get(parts[-1])
        status[name] = ok
    return status


def build_documents(scenario: Mapping[str, Any], build_dir: Path | str | None) -> dict[str, str] | None:
    """Knowledge document id → text as built (``build/pack/knowledge-base/docs/<basename>``), or ``None``."""
    if build_dir is None:
        return None
    docs = Path(build_dir) / "pack" / "knowledge-base" / "docs"
    texts: dict[str, str] = {}
    for doc in (scenario.get("knowledge") or {}).get("documents") or []:
        if not isinstance(doc, dict) or not isinstance(doc.get("id"), str) or not isinstance(doc.get("file"), str):
            continue
        path = docs / Path(doc["file"]).name  # the compiler's layout: basenames only, never a relative path
        if path.is_file():
            texts[doc["id"]] = path.read_text(encoding="utf-8", errors="replace")
    return texts


def _step(state: Mapping[str, Any], step_id: str) -> Mapping[str, Any]:
    return ((state.get("steps") or {}).get(step_id) or {})


def evidence_key(state: Mapping[str, Any]) -> tuple[str, str] | None:
    """The evaluation evidence a run carries: (baseline commandId, optimize commandId), or ``None``."""
    baseline, optimize = _step(state, "baseline").get("commandId"), _step(state, "optimize").get("commandId")
    return (baseline, optimize) if isinstance(baseline, str) and baseline and isinstance(optimize, str) and optimize else None


def _run(source: str, state: dict[str, Any], scenario: dict[str, Any], *, current: bool) -> dict[str, Any]:
    from . import guided_report  # see the module note on import direction

    report = guided_report.build_report(state, scenario, generated_at=str(state.get("updatedAt") or ""))
    contrast = report["teachingContrast"]
    return {
        "source": source,
        "current": current,
        "state": state,
        "report": report,
        "contrast": contrast,
        "key": evidence_key(state),
        "time": (str(_step(state, "optimize").get("finishedAt") or ""), str(state.get("updatedAt") or ""), source),
    }


def _run_summary(run: Mapping[str, Any]) -> dict[str, Any]:
    state, report, contrast = run["state"], run["report"], run["contrast"]
    return {
        "source": run["source"],
        "current": run["current"],
        "runStatus": state.get("status"),
        "reportStatus": report["status"],
        "missingEvidence": list(report["completion"]["missingEvidence"]),
        "teachingContrast": contrast["currentRun"],
        "verdict": RUN_VERDICT[contrast["currentRun"]],
        "baselineCommandId": _step(state, "baseline").get("commandId"),
        "optimizeCommandId": _step(state, "optimize").get("commandId"),
        "optimizeFinishedAt": _step(state, "optimize").get("finishedAt"),
        "updatedAt": state.get("updatedAt"),
        "phenomena": {str(p["id"]): p["status"] for p in contrast["phenomena"]},
    }


def _select_runs(current: dict[str, Any], history: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Distinct same-evidence runs, oldest first; the current run is the newest by construction.

    Archived states that share the current run's (baseline, optimize) commands are the same
    evaluation (a partial reset after 10): one representative is kept, the complete one first, the
    current run on a tie. History without evaluation evidence is ignored.
    """
    picked: dict[tuple[str, str], dict[str, Any]] = {}
    for run in sorted(history, key=lambda r: r["time"]):
        if run["key"] is None:
            continue
        prior = picked.get(run["key"])
        if prior is None or run["report"]["status"] == "complete" or prior["report"]["status"] != "complete":
            picked[run["key"]] = run
    runs = sorted(picked.values(), key=lambda r: r["time"])
    if current["key"] is not None and current["key"] in picked:
        prior = picked[current["key"]]
        if current["report"]["status"] == "complete" or prior["report"]["status"] != "complete":
            runs = [r for r in runs if r is not prior] + [current]
        else:
            prior["sharesEvidenceWithCurrent"] = True
    else:
        runs.append(current)
    return runs[-HISTORY_LIMIT:]


def _consistency(statuses: Sequence[str]) -> str:
    if not statuses:
        return "none"
    if len(statuses) == 1:
        return "single_run"
    return "consistent" if len(set(statuses)) == 1 else "mixed"


# -- remediation -------------------------------------------------------------


class _Hints:
    """remediation[]: de-duplicated by (code, caseId, file, scenarioPath), in phenomenon/case order.

    A hint shared by several phenomena (the case-less ones: METRICS_MISSING, NOISE_BAND_MISSING,
    JUDGE_TOO_NOISY, ops) lists every phenomenon in ``phenomenonIds``. It is ``blocking`` as soon as
    one caller blocks; ``phenomenonId`` and ``because`` then name that caller (the reason it blocks).
    ``action`` and ``because`` are :class:`rehearsal_strings.Text` values (``build_rehearsal`` renders
    them as ``action``/``actionEn`` and ``because``/``becauseEn``); a plain string stays as it is.
    """

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self._seen: dict[tuple[Any, ...], dict[str, Any]] = {}

    def add(self, code: str, asset: dict[str, Any], action: rs.Text | str, because: rs.Text | str, *, severity: str = "blocking",
            phenomenon_id: str | None = None, case_id: str | None = None) -> str:
        key = (code, case_id, asset.get("file"), asset.get("scenarioPath"))
        hint = self._seen.get(key)
        if hint is None:
            hint = {"id": f"h{len(self.items) + 1:02d}", "code": code, "severity": severity, "phenomenonId": phenomenon_id,
                    "phenomenonIds": [], "caseId": case_id, "asset": asset, "action": action, "because": because}
            self._seen[key] = hint
            self.items.append(hint)
        elif severity == "blocking" and hint["severity"] != "blocking":
            hint.update(severity="blocking", phenomenonId=phenomenon_id, because=because)
        if phenomenon_id is not None and phenomenon_id not in hint["phenomenonIds"]:
            hint["phenomenonIds"].append(phenomenon_id)
        return hint["id"]


def _asset(kind: str, file: str | None = None, path: str | None = None) -> dict[str, Any]:
    return {"kind": kind, "file": file, "scenarioPath": path, "scopes": list(ASSET_SCOPES[kind])}


class _Assets:
    """Concrete asset references in the SA's project (the files and scenario paths a repair edits)."""

    def __init__(self, scenario: Mapping[str, Any], documents: Mapping[str, str] | None):
        self.scenario = scenario
        self.documents = documents
        self.doc_files = {d["id"]: d.get("file") for d in (scenario.get("knowledge") or {}).get("documents") or []
                          if isinstance(d, dict) and isinstance(d.get("id"), str)}
        prompts = scenario.get("prompts") or {}
        self.baseline = _asset("baseline_prompt", prompts.get("baselineFile"), "prompts.baselineFile")
        self.candidate = _asset("candidate_prompt", prompts.get("optimizationCandidateFile"), "prompts.optimizationCandidateFile")
        name = (scenario.get("evaluation") or {}).get("retrievalToolName")
        self.retrieval_tool_name = str(name) if name else None
        self.retrieval_tool: str | Msg = self.retrieval_tool_name or Msg("retrieval_tool")

    def case(self, cid: str, field: str | None = None) -> dict[str, Any]:
        return _asset("golden_case", SCENARIO_FILE, f"evaluation.goldenSet[id={cid}]" + (f".{field}" if field else ""))

    def phenomenon(self, pid: str | None) -> dict[str, Any]:
        return _asset("teaching", SCENARIO_FILE, f"labs.teaching.phenomena[id={pid}]" if pid else "labs.teaching.phenomena")

    def teaching(self, path: str = "labs.teaching") -> dict[str, Any]:
        return _asset("teaching", SCENARIO_FILE, path)

    def tool(self, name: str) -> dict[str, Any]:
        return _asset("tool_fixture", SCENARIO_FILE, f"tools[name={name}].fixtures")

    def doc(self, did: str) -> dict[str, Any]:
        return _asset("kb_doc", self.doc_files.get(did), f"knowledge.documents[id={did}]")

    def topic_document(self, cid: str, absent_terms: Sequence[str]) -> tuple[str, float] | None:
        """``(document id, share)`` of the document holding the largest share (≥ ``teaching_policy.TOPIC_SHARE``) of
        the gap question's subject words, the absent terms left out; ``None`` when no document comes close."""
        case = next((c for c in (self.scenario.get("evaluation") or {}).get("goldenSet") or [] if c.get("id") == cid), None)
        if not self.documents or case is None:
            return None
        found = teaching_policy.topic_share(str(case.get("query") or ""), absent_terms, dict(self.documents))
        return found if found and found[1] >= teaching_policy.TOPIC_SHARE else None

    def docs_for(self, cid: str, *, absent_terms: Sequence[str] | None = None, limit: int = 3) -> tuple[list[dict[str, Any]], Msg | None]:
        """The knowledge documents a repair of ``cid`` edits, with a note when none can be named.

        By default the documents carrying the expected answer (``expected.mustMention``; every term
        first). For an absent gap (``absent_terms`` given) the answer terms are the hand-off wording,
        so the documents stating an ``absentTerm`` are named instead, else the bait documents.
        """
        nowhere = [_asset("kb_doc", None, "knowledge.documents")]
        if self.documents is None:
            return nowhere, Msg("why.docs_unavailable")
        try:
            found = teaching.answer_documents(self.scenario, cid, texts=self.documents)
        except KeyError:
            return nowhere, Msg("why.not_golden", case=cid)
        if absent_terms is None:
            ids = list(found.complete) + [d for d in found.answer if d not in found.complete]
            missing = Msg("why.no_answer_doc")
        else:
            order = [d for d in self.doc_files if d in self.documents]
            ids = []
            for term in absent_terms:
                ids += [d for d in teaching.documents_containing(self.documents, term, order=order) if d not in ids]
            ids = ids or list(found.bait)
            missing = Msg("why.no_absent_doc")
        if not ids:
            return nowhere, missing
        return [self.doc(d) for d in ids[:limit]], None


def _because(case: Mapping[str, Any], note: Msg | None = None) -> rs.Text:
    """The case verdict's reason (already localized), then ``note``: the ``because`` of its hints."""
    reason = Given.of(case, "reason")
    if note is not None and reason.value:
        return Msg("because.join", reason=reason, note=note)
    return note if note is not None else reason


def _l1_failed(table_rows: Mapping[str, Any], cid: str, phase: str, codes: Sequence[str]) -> list[str]:
    cell = ((table_rows.get(cid) or {}).get(phase) or {}).get("l1") or {}
    return [code for code in codes if code in (cell.get("fail") or [])]


def _tool_calls(l1_rows: Mapping[str, Mapping[str, Any]], phase: str, cid: str, tool: str | None) -> int | None:
    """``tool`` calls on ``cid`` in the phase's compact L1 row, or ``None`` without a ``tools`` list (the compact form
    drops it to fit the step output) or a named retrieval tool. One entry per tool span: a lower bound when L1
    stopped polling before every span was indexed."""
    tools = ((l1_rows.get(phase) or {}).get(cid) or {}).get("tools")
    if tool is None or not isinstance(tools, list):
        return None
    return sum(t == tool for t in tools)


def _empty_reply_why(run: str, cid: str, tool: rs.Text | str, calls: int | None) -> Msg:
    """Why an empty-reply hint fires, with the retrieval evidence: more calls than RETRIEVAL_CAP are named, and a
    count that reaches the harness iteration limit (04-deploy.sh) says the turn most likely ended there."""
    if calls is not None and calls >= UPSTREAM_MAX_ITERATIONS:
        return Msg("why.EMPTY_REPLY.limit", run=_run_label(run), case=cid, tool=tool, count=calls, limit=UPSTREAM_MAX_ITERATIONS)
    if calls is not None and calls > RETRIEVAL_CAP:
        return Msg("why.EMPTY_REPLY.calls", run=_run_label(run), case=cid, tool=tool, count=calls)
    return Msg("why.EMPTY_REPLY", run=_run_label(run), case=cid)


def _empty_reply_fix(assets: _Assets, calls: int | None, rule: Msg, otherwise: Msg) -> Msg:
    """The candidate-prompt action for an empty optimized reply. After more retrieval calls than RETRIEVAL_CAP the
    candidate's cap, if it has one, did not hold: the action says so instead of prescribing the cap as new."""
    if calls is not None and calls > RETRIEVAL_CAP:
        return Msg("fix.EMPTY_REPLY.over_cap", tool=assets.retrieval_tool, cap=RETRIEVAL_CAP, rule=rule)
    return otherwise


def _rule(kind: Any, case_def: Mapping[str, Any]) -> Msg:
    """The candidate-prompt rule an L1 phenomenon of ``kind`` teaches, naming the case's tools."""
    expected = case_def.get("expected") or {}
    required = [t for t in expected.get("requiredTools") or [] if isinstance(t, str)]
    forbidden = [t for t in expected.get("forbiddenTools") or [] if isinstance(t, str)]
    return {
        "tool_use": Msg("rule.tool_use", tools=Join(required) if required else Msg("rule.case_tool")),
        "refusal": Msg("rule.refusal_forbidden", tools=Join(forbidden)) if forbidden else Msg("rule.refusal"),
        "escalation": Msg("rule.escalation"),
    }.get(str(kind), Msg("rule.default"))


def _case_hints(hints: _Hints, assets: _Assets, phenomenon: Mapping[str, Any], case: Mapping[str, Any],
                declared: Mapping[str, Any], case_def: Mapping[str, Any], table_rows: Mapping[str, Any],
                l1_rows: Mapping[str, Mapping[str, Any]], *, severity: str) -> list[str]:
    """remediation for one not-reproduced / insufficient case verdict of the decisive run."""
    code, cid, pid, kind = case.get("code"), case.get("caseId"), phenomenon.get("id"), phenomenon.get("kind")
    rt = assets.retrieval_tool
    ids: list[str] = []

    def add(asset: dict[str, Any], action: Msg, note: Msg | None = None, *, hint_code: str | None = None,
            case_id: str | None = cid) -> None:
        ids.append(hints.add(hint_code or str(code), asset, action, _because(case, note), severity=severity,
                             phenomenon_id=pid, case_id=case_id))

    if code == "PF_BASELINE_ALREADY_PASSES":
        # Live 2026-09-29 (lease-contract): the baseline carried a marker that bites and still scored GR 1.0 on
        # two narrow threshold questions; it kept the added advice apart from the documented answer.
        defects = {d.get("id"): d for d in ((assets.scenario.get("labs") or {}).get("teaching") or {}).get("baselineDefects") or []
                   if isinstance(d, dict)}
        markers = [defects[d]["baselineMarker"] for d in declared.get("defectIds") or []
                   if isinstance((defects.get(d) or {}).get("baselineMarker"), str) and marker_bites(defects[d]["baselineMarker"])]
        if markers:
            add(assets.case(str(cid), "query"), Msg("fix.PF_BASELINE_ALREADY_PASSES.query", case=cid, marker=markers[0]))
        else:
            add(assets.baseline, Msg("fix.PF_BASELINE_ALREADY_PASSES.baseline"))
        add(assets.phenomenon(pid), Msg("fix.PF_BASELINE_ALREADY_PASSES.teaching", case=cid))
    elif code == "PF_NO_GAIN":
        add(assets.candidate, Msg("fix.PF_NO_GAIN"))
    elif code == "PF_RETRIEVAL_FAILED":
        docs, note = assets.docs_for(str(cid))
        for asset in docs:
            add(asset, Msg("fix.PF_RETRIEVAL_FAILED.doc"), note)
        add(assets.phenomenon(pid), Msg("fix.PF_RETRIEVAL_FAILED.teaching", case=cid))
    elif code in ("PF_NOT_SCORED", "RG_NOT_SCORED"):
        add(assets.case(str(cid), "query"), Msg("fix.NOT_SCORED.query", tool=rt))
        add(assets.baseline, Msg("fix.NOT_SCORED.baseline", tool=rt))
        if code == "RG_NOT_SCORED" and declared.get("mechanism") == "absent":
            add(_asset("ops"), Msg("fix.RG_ABSENT_NOT_SCORED"), hint_code="RG_ABSENT_NOT_SCORED")
    elif code == "METRICS_MISSING":
        add(_asset("ops"), Msg("fix.METRICS_MISSING"), case_id=None)
    elif code == "NOISE_BAND_MISSING":
        add(_asset("judge", SCENARIO_FILE, "evaluation.noiseBand"), Msg("fix.NOISE_BAND_MISSING"), case_id=None)
    elif code == teaching.JUDGE_TOO_NOISY:
        # The judge is not the pack's choice: 00-config.sh judges with WORKSHOP_MODEL_ID and the generation
        # loop pins evaluation.judgeModel, so the only lever is a better-measured noise band.
        add(_asset("judge", SCENARIO_FILE, "evaluation.noiseBand"), Msg("fix.JUDGE_TOO_NOISY"), case_id=None)
    elif code == "RG_OPTIMIZED_RESOLVED":  # buried gaps only: retrieval failed twice, the optimized label is not Fail
        add(assets.phenomenon(pid), Msg("fix.RG_OPTIMIZED_RESOLVED"))
    elif code in ("RG_RETRIEVAL_OK_BASELINE", "RG_RETRIEVAL_OK_OPTIMIZED"):
        absent_terms = [t for t in declared.get("absentTerms") or [] if isinstance(t, str)]
        if declared.get("mechanism") == "absent":
            docs, note = assets.docs_for(str(cid), absent_terms=absent_terms)
            action = (Msg("fix.RG_RETRIEVAL_OK.absent_terms", terms=Join(absent_terms)) if absent_terms
                      else Msg("fix.RG_RETRIEVAL_OK.absent"))
            topic = None
            if absent_terms and assets.documents is not None and not any(
                    teaching.documents_containing(assets.documents, t) for t in absent_terms):
                # A release never states an absentTerm (teaching.absent_term_in_kb blocks the build), so retrieval
                # covered the rest of the question: the bait lines (ops-support 09-28) or a documented topic the
                # question qualifies (lease-contract 09-29: "can cross-border clinics use the same standard template?").
                if note is None:
                    action = Msg("fix.RG_RETRIEVAL_OK.absent_bait")
                else:
                    docs = []  # no bait document to edit
                topic = assets.topic_document(str(cid), absent_terms)
        else:
            docs, note = assets.docs_for(str(cid))
            action = Msg("fix.RG_RETRIEVAL_OK.buried")
            topic = None
        for asset in docs:
            add(asset, action, note)
        if topic is not None:  # after the bait hints: a possibility with its evidence, not a certainty
            did, share = topic
            add(assets.case(str(cid), "query"), Msg("fix.RG_RETRIEVAL_OK.absent_query", case=cid, terms=Join(absent_terms),
                                                    doc=assets.doc(did)["file"] or did, share=f"{share:.0%}"),
                hint_code="RG_ABSENT_TOPIC_COVERED")
        add(assets.phenomenon(pid), Msg("fix.RG_REDECLARE", case=cid), hint_code="RG_REDECLARE")
    elif code == "L1_BASELINE_ALREADY_PASSES":
        add(assets.baseline, Msg("fix.L1_BASELINE_ALREADY_PASSES.baseline", kind=kind))
        add(assets.phenomenon(pid), Msg("fix.L1_BASELINE_ALREADY_PASSES.teaching", phenomenon=pid))
    elif code == "L1_OPTIMIZED_FAILS":
        failed = _l1_failed(table_rows, str(cid), "optimized", case.get("checks") or FOCUS_CHECKS.get(str(kind), ()))
        note = Msg("why.L1_OPTIMIZED_FAILS", checks=Join(failed)) if failed else None
        expected = case_def.get("expected") or {}
        forbidden = [t for t in expected.get("forbiddenTools") or [] if isinstance(t, str)]
        if (kind == "refusal" and failed == ["mustNotMention"] and expected.get("shouldRefuse")
                and all(_tool_calls(l1_rows, "optimized", str(cid), t) in (0, None) for t in forbidden)):
            # Live 2026-10-01 (loyalty-points, three rounds): all three optimized answers refused and called nothing,
            # two said the forbidden "积分余额" while sending the member to the app; the refusal looked 1-in-3.
            terms = [t for t in expected.get("mustNotMention") or [] if isinstance(t, str)]
            add(assets.case(str(cid), "expected"), Msg("fix.L1_REFUSAL_TERM_SAID", case=cid, terms=Join(terms)), note,
                hint_code="L1_REFUSAL_TERM_SAID")
            return ids
        role = teaching_policy.role_not_in_query(dict(assets.scenario), dict(case_def)) if kind == "refusal" else None
        calls = _tool_calls(l1_rows, "optimized", str(cid), role[2]) if role else None
        # Live 2026-09-29 (lease-contract): the candidate had the rule; the agent did not know the role and called the
        # gated tool. Only that evidence (forbiddenTools failed, the gated tool called or the tools list dropped) says so.
        if role and "forbiddenTools" in failed and (calls is None or calls > 0):
            add(assets.case(str(cid), "query"), Msg("fix.L1_ROLE_NOT_IN_QUERY", role=role[1], tool=role[2]), note,
                hint_code="L1_ROLE_NOT_IN_QUERY")
        gated = next(((rid, name, tool) for c, rid, name, tool in teaching_policy.role_gated_refusals(dict(assets.scenario))
                      if c.get("id") == cid), None) if kind == "refusal" else None
        if gated and not role and "forbiddenTools" in failed:
            # Live 2026-09-29 (freight-claims): the question named the role, yet the agent called the gated tool with
            # its own role "to check" and refused after the access_denied, in both runs, even once the candidate's
            # rule named the role. The lever that removes the call is the tool's role argument.
            arg = teaching_policy.role_argument(next((t for t in assets.scenario.get("tools") or []
                                                      if isinstance(t, dict) and t.get("name") == gated[2]), {}))
            if arg:
                add(_asset("tool_fixture", SCENARIO_FILE, f"tools[name={gated[2]}]"),
                    Msg("fix.L1_GATED_TOOL_ROLE_ARG", tool=gated[2], arg=arg), note, hint_code="L1_GATED_TOOL_ROLE_ARG")
            add(assets.candidate, Msg("fix.L1_ROLE_RULE", role=gated[1], tool=gated[2]), note, hint_code="L1_ROLE_RULE")
        else:
            add(assets.candidate, Msg("fix.L1_OPTIMIZED_FAILS", rule=_rule(kind, case_def)), note)
        for tool in expected.get("requiredTools") or []:
            if isinstance(tool, str) and tool != assets.retrieval_tool_name and kind == "tool_use":
                add(assets.tool(tool), Msg("fix.L1_TOOL_FIXTURE", tool=tool), note, hint_code="L1_TOOL_FIXTURE")
        add(assets.case(str(cid), "expected"), Msg("fix.L1_EXPECTED_CHECK", case=cid), note, hint_code="L1_EXPECTED_CHECK")
    elif code == "L1_EMPTY_RESPONSE":
        # The reason already says the reply is empty; the retrieval evidence is added when it says more.
        calls = _tool_calls(l1_rows, "optimized", str(cid), assets.retrieval_tool_name)
        note = _empty_reply_why("optimized", str(cid), rt, calls) if calls is not None and calls > RETRIEVAL_CAP else None
        rule = _rule(kind, case_def)
        add(assets.candidate, _empty_reply_fix(assets, calls, rule, Msg("fix.L1_EMPTY_RESPONSE", rule=rule)), note)
    elif code in ("L1_MISSING", "L1_ERROR"):
        add(_asset("ops"), Msg("fix.L1_MISSING"))
    elif code == "L1_UNRESOLVED":
        add(assets.case(str(cid), "expected"), Msg("fix.L1_UNRESOLVED"))
    elif code == "L1_NO_FOCUS_CHECK":
        add(assets.case(str(cid), "expected"), Msg("fix.L1_NO_FOCUS_CHECK", kind=kind, case=cid, checks=Join(FOCUS_CHECKS.get(str(kind), ()))))
    elif code in ("CASE_NOT_PRACTICE", "UNSUPPORTED_KIND"):
        add(assets.phenomenon(pid), Msg("fix.PHENOMENON_DECLARATION"))
    return ids


def _advisory_hints(hints: _Hints, assets: _Assets, advisory: Sequence[Mapping[str, Any]]) -> None:
    for item in advisory:
        if item.get("kind") == "memory":
            path = "labs.teaching.firstConversation"
            if item.get("evidence") == "missing":
                hints.add("CONVERSATION_EVIDENCE_MISSING", _asset("ops"), Msg("fix.CONVERSATION_EVIDENCE_MISSING"),
                          Msg("why.CONVERSATION_EVIDENCE_MISSING"), severity="advisory")
            elif item.get("reading") == "personalized":
                hits = item.get("hits") or []
                terms = Join(h.get("term", "") for h in hits)
                if all(h.get("given") for h in hits):
                    hints.add("MEMORY_TERM_GIVEN", assets.teaching(path + ".mustNotMention"), Msg("fix.MEMORY_TERM_GIVEN"),
                              Msg("why.MEMORY_TERM_GIVEN", terms=terms,
                                  where=Join(sorted({str(h["given"]).replace("tool:", "") for h in hits}))), severity="advisory")
                else:
                    hints.add("MEMORY_PERSONALIZED", assets.teaching(path + ".actorId"), Msg("fix.MEMORY_PERSONALIZED"),
                              Msg("why.MEMORY_PERSONALIZED", terms=terms), severity="advisory")
            elif item.get("reading") == "unknown" and not item.get("mustNotMention"):
                hints.add("MEMORY_UNCHECKED", assets.teaching(path + ".mustNotMention"), Msg("fix.MEMORY_UNCHECKED"),
                          Msg("why.MEMORY_UNCHECKED"), severity="advisory")
        elif item.get("kind") == "noise_grounding" and item.get("reading") == "not_observed":
            for did in item.get("documentIds") or []:
                hints.add("NOISE_NOT_OBSERVED", assets.doc(str(did)), Msg("fix.NOISE_NOT_OBSERVED"), Msg("why.NOISE_NOT_OBSERVED"),
                          severity="advisory", phenomenon_id=item.get("id"))


def _warnings(contrast: Mapping[str, Any], phenomena: Sequence[Mapping[str, Any]], runs: Sequence[Mapping[str, Any]],
              decisive: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if decisive is not None and not decisive["current"]:
        out.append({"code": "DECISIVE_RUN_ARCHIVED", "refs": [decisive["source"]],
                    "message": Msg("warn.DECISIVE_RUN_ARCHIVED", source=decisive["source"])})
    complete = [r for r in runs if r["report"]["status"] == "complete"]
    if _consistency([RUN_VERDICT[r["contrast"]["currentRun"]] for r in complete]) == "mixed":
        out.append({"code": "VERDICT_MIXED", "refs": [r["source"] for r in complete], "message": Msg("warn.VERDICT_MIXED")})
    for entry in phenomena:
        if entry["replication"]["consistency"] == "mixed":
            out.append({"code": "PHENOMENON_MIXED", "refs": [entry["id"]],
                        "message": Msg("warn.PHENOMENON_MIXED", phenomenon=entry["id"], reproduced=entry["replication"]["reproducedIn"],
                                       runs=entry["replication"]["completeRuns"])})
    if decisive is not None:
        for p in contrast.get("phenomena") or []:
            for case in p.get("cases") or []:
                if case.get("warning"):
                    out.append({"code": "PF_OPTIMIZED_NOT_PASS", "refs": [p["id"], case["caseId"]], "message": Given.of(case, "warning")})
                if case.get("honesty") == DOES_NOT_ADMIT:
                    out.append({"code": "ABSENT_GAP_NOT_ADMITTED", "refs": [p["id"], case["caseId"]],
                                "message": Msg("warn.ABSENT_GAP_NOT_ADMITTED")})
                if case.get("honesty") == NO_ANSWER:  # further from the lesson than a reply that does not admit the gap
                    out.append({"code": "ABSENT_GAP_NO_ANSWER", "refs": [p["id"], case["caseId"]],
                                "message": Msg("warn.ABSENT_GAP_NO_ANSWER")})
                optimized = case.get("optimized") or {}
                if (p.get("kind") in FOCUS_CHECKS and optimized.get("focus") == "pass"
                        and str(optimized.get("mtg") or "").lower() == "fail"):
                    out.append({"code": "MTG_REFUSAL_CONFLICT", "refs": [p["id"], case["caseId"]], "message": Msg("warn.MTG_REFUSAL_CONFLICT")})
    return out


def build_rehearsal(
    state: dict[str, Any],
    scenario: dict[str, Any],
    *,
    history: Sequence[tuple[str, dict[str, Any]]] = (),
    documents: Mapping[str, str] | None = None,
    guides: Mapping[str, bool] | None = None,
    scenario_source: str = "build snapshot",
    release_verified: bool = True,
) -> dict[str, Any]:
    """``run/rehearsal.json`` for the current Guided Run ``state`` and its same-release ``history``.

    ``scenario`` is the build snapshot; ``history`` is ``[(source, archived state)]`` of the same
    release and template (``GuidedRunStore.history_states``); ``documents`` maps knowledge document
    ids to their built text (remediation names the documents carrying an answer); ``guides`` is
    :func:`guide_status` of the current build.

    ``scenario_source`` (:data:`SCENARIO_SOURCES`) and ``release_verified`` state what the caller
    checked: the app always judges the build snapshot of the build the run is bound to. The CLI can
    also judge a bare scenario file, or a build without a release manifest; the verdict is then
    computed as usual, but ``readyForClass`` stays false (blockers ``SCENARIO_NOT_BUILD_SNAPSHOT`` /
    ``RELEASE_NOT_VERIFIED``).

    ``language`` is the scenario's text language (:func:`rehearsal_strings.lang_key`): every human-readable
    field is in that language and carries an English twin (``reasonEn``, ``actionEn``, ``becauseEn``,
    ``messageEn``, ``noteEn``, ``warningEn``, ``scopeWarningEn``, ``readiness.blockerTexts[].textEn``).
    """
    if scenario_source not in SCENARIO_SOURCES:
        raise ValueError(f"scenario_source must be one of {SCENARIO_SOURCES}, not {scenario_source!r}")
    current = _run("current", state, scenario, current=True)
    archived = [_run(source, past, scenario, current=False) for source, past in history
                if past.get("releaseVersion") == state.get("releaseVersion") and past.get("templateCommit") == state.get("templateCommit")]
    runs = _select_runs(current, archived)
    complete = [r for r in runs if r["report"]["status"] == "complete"]
    decisive = complete[-1] if complete else None
    evidence = decisive or current
    contrast = evidence["contrast"]
    declared = teaching.declared(scenario)
    table_rows = {row["caseId"]: row for row in (evidence["report"]["caseTable"].get("cases") or [])}
    from . import guided_report  # see the module note on import direction

    # The evidence run's compact L1 rows per phase: the case table keeps their codes, the tool calls live here.
    l1_rows = {phase: guided_report.l1_evidence(_step(evidence["state"], step).get("outputs") or {})["cases"]
               for phase, step in guided_report.PHASE_STEPS}
    groups = dict(contrast.get("groups") or {})
    if decisive is None:
        groups = {g: ("missing" if s == "missing" else "insufficient_evidence") for g, s in groups.items()}
    missing_groups = [g for g, s in groups.items() if s == "missing"]

    lang = rs.lang_key(scenario.get("language"))
    if not declared:
        verdict, reason_code = "not_ready", "TEACHING_UNDECLARED"
        reason = Msg("verdict.TEACHING_UNDECLARED")
    elif missing_groups:
        verdict, reason_code = "not_ready", "PARITY_MISSING"
        reason = Msg("verdict.PARITY_MISSING", groups=Join(missing_groups))
    elif decisive is None:
        verdict, reason_code = "insufficient_evidence", "RUN_INCOMPLETE"
        reason = Msg("verdict.RUN_INCOMPLETE")
    else:
        verdict = RUN_VERDICT[contrast["currentRun"]]
        reason_code = {"ready": "CONTRASTS_REPRODUCED", "not_ready": "PHENOMENON_NOT_REPRODUCED",
                       "insufficient_evidence": "PHENOMENON_INSUFFICIENT"}[verdict]
        reason = Msg(f"verdict.{reason_code}")

    hints = _Hints()
    assets = _Assets(scenario, documents)
    declarations = {p.get("id"): p for p in teaching.phenomena(scenario)}
    cases_by_id = {c["id"]: c for c in _practice(scenario)}
    phenomena: list[dict[str, Any]] = []
    for p in contrast.get("phenomena") or []:
        statuses = [next((q["status"] for q in r["contrast"].get("phenomena") or [] if q["id"] == p["id"]), "insufficient_evidence")
                    for r in complete]
        entry: dict[str, Any] = {k: p[k] for k in ("id", "kind", "caseIds", "teachingPoint", "mechanism", "design") if k in p}
        if decisive is None:
            entry.update(verdict="insufficient_evidence", reasonCode="RUN_INCOMPLETE", source=None, cases=[])
        else:
            deciding = None if p["status"] == "reproduced" else (
                next((c for c in p["cases"] if c["status"] == p["status"]), None)
                or next((c for c in p["cases"] if c["status"] != "reproduced"), None))
            entry.update(verdict=p["status"], reasonCode=deciding["code"] if deciding else "REPRODUCED",
                         source=decisive["source"], cases=p["cases"])
        entry["replication"] = {
            "completeRuns": len(complete),
            "reproducedIn": sum(s == "reproduced" for s in statuses),
            "runs": [{"source": r["source"], "status": s} for r, s in zip(complete, statuses)],
            "consistency": _consistency(statuses),
        }
        hint_ids: list[str] = []
        if decisive is not None:
            # A failing case blocks only when its phenomenon fails and so does its group (≥1 prompt_fixable
            # and ≥1 retrieval_gap suffice; every L1 phenomenon must hold).
            group = {"prompt_fixable": "prompt_fixable", "retrieval_gap": "retrieval_gap"}.get(str(p["kind"]), "l1")
            severity = "blocking" if p["status"] != "reproduced" and groups.get(group) != "reproduced" else "advisory"
            for case in entry["cases"]:
                if case["status"] != "reproduced":
                    case_ids = _case_hints(hints, assets, p, case, declarations.get(p["id"]) or {},
                                           cases_by_id.get(case["caseId"]) or {}, table_rows, l1_rows, severity=severity)
                    hint_ids += [h for h in case_ids if h not in hint_ids]
                baseline_cell = (table_rows.get(case["caseId"]) or {}).get("baseline") or {}
                if p.get("design") == "contrast" and _empty_reply_fails(baseline_cell, case.get("checks") or ()):
                    # The contrast counts the empty baseline reply as the missing behavior; students see no reply.
                    calls = _tool_calls(l1_rows, "baseline", case["caseId"], assets.retrieval_tool_name)
                    hint = hints.add("L1_BASELINE_EMPTY_RESPONSE", assets.baseline,
                                     Msg("fix.L1_BASELINE_EMPTY_RESPONSE", kind=p["kind"], phenomenon=p["id"]),
                                     _empty_reply_why("baseline", case["caseId"], assets.retrieval_tool, calls), severity="advisory",
                                     phenomenon_id=p["id"], case_id=case["caseId"])
                    hint_ids += [hint] if hint not in hint_ids else []
                if case.get("honesty") == NO_ANSWER:
                    calls = _tool_calls(l1_rows, "optimized", case["caseId"], assets.retrieval_tool_name)
                    rule = Msg("rule.absent_gap")
                    action = _empty_reply_fix(assets, calls, rule,
                                              Msg("fix.ABSENT_GAP_EMPTY_ANSWER", tool=assets.retrieval_tool, cap=RETRIEVAL_CAP, rule=rule))
                    hint = hints.add("ABSENT_GAP_EMPTY_ANSWER", assets.candidate, action,
                                     _empty_reply_why("optimized", case["caseId"], assets.retrieval_tool, calls), severity="advisory",
                                     phenomenon_id=p["id"], case_id=case["caseId"])
                    hint_ids += [hint] if hint not in hint_ids else []
        entry["hintIds"] = hint_ids
        phenomena.append(entry)

    if not declared:
        hints.add("TEACHING_UNDECLARED", assets.teaching(), Msg("fix.TEACHING_UNDECLARED"), reason)
    for group in missing_groups:
        what = Msg("parity.l1") if group == "l1" else group
        hints.add("PARITY_MISSING", assets.phenomenon(None), Msg("fix.PARITY_MISSING", what=what), Msg("why.PARITY_MISSING", group=group))
    current_report = current["report"]
    if current_report["status"] != "complete":
        missing = current_report["completion"]["missingEvidence"]
        hints.add("RUN_INCOMPLETE", _asset("run"), Msg("fix.RUN_INCOMPLETE"),
                  Msg("why.RUN_INCOMPLETE.missing", missing=Join(missing, "sep.clause")) if missing else Msg("why.RUN_INCOMPLETE"))
    guide_flags = {name: bool((guides or {}).get(name)) for name in GUIDE_FILES}
    if not all(guide_flags.values()):
        absent = [("/".join(("build",) + GUIDE_FILES[name])) for name, ok in guide_flags.items() if not ok]
        hints.add("GUIDE_MISSING", _asset("guide", None, "labs.guide"), Msg("fix.GUIDE_MISSING"), Msg("why.GUIDE_MISSING", paths=Join(absent)))
    snapshot = scenario_source == "build snapshot"
    if not snapshot:
        hints.add("SCENARIO_NOT_BUILD_SNAPSHOT", _asset("run"), Msg("fix.SCENARIO_NOT_BUILD_SNAPSHOT"), Msg("why.SCENARIO_NOT_BUILD_SNAPSHOT"))
    if not release_verified:
        hints.add("RELEASE_NOT_VERIFIED", _asset("run"), Msg("fix.RELEASE_NOT_VERIFIED"),
                  Msg("why.RELEASE_NOT_VERIFIED", release=str(state.get("releaseVersion"))))
    advisory = [dict(a) for a in contrast.get("advisory") or []]
    _advisory_hints(hints, assets, advisory)
    for item in advisory:
        item["source"] = evidence["source"]

    report_complete = current_report["status"] == "complete"
    blockers = ([] if verdict == "ready" else ["VERDICT_" + verdict.upper()]) + ([] if report_complete else ["REPORT_INCOMPLETE"]) \
        + ([] if all(guide_flags.values()) else ["GUIDES_MISSING"]) + ([] if snapshot else ["SCENARIO_NOT_BUILD_SNAPSHOT"]) \
        + ([] if release_verified else ["RELEASE_NOT_VERIFIED"])
    ready_for_class = not blockers  # verdict ready ∧ report complete ∧ both guides ∧ the build snapshot of the run's release
    # Every practice case of the evidence run (the decisive run, else the current one), with the phenomena listing it.
    listed: dict[str, list[str]] = {}
    for declaration in teaching.phenomena(scenario):
        for cid in declaration.get("caseIds") or []:
            if isinstance(cid, str) and isinstance(declaration.get("id"), str) and declaration["id"] not in listed.setdefault(cid, []):
                listed[cid].append(declaration["id"])
    evidence_table = evidence["report"]["caseTable"]
    case_rows = [{**row, "phenomenonIds": listed.get(row["caseId"], [])} for row in evidence_table.get("cases") or []]
    conversation = (_step(evidence["state"], "conversation").get("outputs") or {}).get("conversation")
    verdict_counts = [p["verdict"] for p in phenomena]
    summaries = [_run_summary(r) for r in runs]
    for summary, run in zip(summaries, runs):
        if run.get("sharesEvidenceWithCurrent"):
            summary["sharesEvidenceWithCurrent"] = True
    considered = runs if any(r is current for r in runs) else runs + [current]
    stamps = [str(r["state"]["updatedAt"]) for r in considered if r["state"].get("updatedAt")]
    return rs.localize({
        "schema": SCHEMA,
        "language": lang,
        "generatedAt": max(stamps) if stamps else None,
        "projectId": state.get("projectId"),
        "releaseVersion": state.get("releaseVersion"),
        "templateCommit": state.get("templateCommit"),
        "inputs": {
            "scenario": scenario_source,
            "releaseVerified": bool(release_verified),
            "runStatus": state.get("status"),
            "reportStatus": current_report["status"],
            "missingEvidence": list(current_report["completion"]["missingEvidence"]),
            "decisiveRun": decisive["source"] if decisive else None,
            "runsConsidered": len(runs),
            "completeRuns": len(complete),
            "noiseBand": {"value": contrast.get("noiseBand"), "source": contrast.get("noiseBandSource") or None,
                          "stabilityVerdict": (evidence["report"].get("judgeStability") or {}).get("verdict")},
            "improvementBar": contrast.get("improvementBar"),
            "thresholds": dict(teaching.THRESHOLDS),
            "documentsSearched": None if documents is None else len(documents),
            "evidenceSources": {
                "source": evidence["source"],
                "phases": {phase: {"l1": info.get("l1"), "scored": {ev: len(ids) for ev, ids in (info.get("scoredCases") or {}).items()}}
                           for phase, info in (evidence_table.get("phases") or {}).items()},
                "conversation": "present" if isinstance(conversation, dict) and str(conversation.get("response") or "").strip() else "missing",
            },
        },
        "verdict": verdict,
        "reasonCode": reason_code,
        "reason": reason,
        "readyForClass": ready_for_class,
        "readiness": {
            "verdict": verdict,
            "reportComplete": report_complete,
            "guidesBuilt": all(guide_flags.values()),
            "guides": {name: {"path": "/".join(("build",) + GUIDE_FILES[name]), "present": ok} for name, ok in guide_flags.items()},
            "blockers": blockers,
            "blockerTexts": [{"code": code, "text": Msg(f"blocker.{code}")} for code in blockers],
        },
        "groups": groups,
        "summary": {
            "phenomena": len(phenomena),
            "reproduced": verdict_counts.count("reproduced"),
            "notReproduced": verdict_counts.count("not_reproduced"),
            "insufficientEvidence": verdict_counts.count("insufficient_evidence"),
            "practiceCases": len(case_rows),
            "casesInPhenomena": sum(bool(row["phenomenonIds"]) for row in case_rows),
            "blockingHints": sum(h["severity"] == "blocking" for h in hints.items),
            "advisoryHints": sum(h["severity"] == "advisory" for h in hints.items),
        },
        "phenomena": phenomena,
        "caseTable": {"source": evidence["source"], "schema": evidence_table.get("schema"), "cases": case_rows},
        "advisory": advisory,
        "runs": summaries,
        "consistency": _consistency([RUN_VERDICT[r["contrast"]["currentRun"]] for r in complete]),
        "warnings": _warnings(contrast, phenomena, runs, decisive),
        "remediation": hints.items,
        "scopeWarning": Msg("scope_warning"),
    }, lang)


def exit_code(rehearsal: Mapping[str, Any]) -> int:
    """0 ready for class; 2 insufficient evidence; 3 not ready; 4 verdict ready but not ready for class."""
    if rehearsal.get("readyForClass") is True:
        return 0
    verdict = str(rehearsal.get("verdict"))
    if verdict == "ready":
        return EXIT_READY_NOT_FOR_CLASS
    return EXIT_CODES.get(verdict, 1)


def validate_document(rehearsal: Mapping[str, Any]) -> list[str]:
    """JSON Schema errors of a rehearsal document against the schema version it declares (:data:`SCHEMA_PATHS`;
    an unknown or missing ``schema`` is checked against the current one, which rejects it); empty when valid."""
    import jsonschema  # engine dependency (scenario.py); imported here so the rules stay stdlib-only

    declared = rehearsal.get("schema") if isinstance(rehearsal, Mapping) else None
    path = SCHEMA_PATHS.get(declared, SCHEMA_PATH) if isinstance(declared, str) else SCHEMA_PATH
    schema = json.loads(path.read_text(encoding="utf-8"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    return sorted(f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}" for err in validator.iter_errors(rehearsal))
