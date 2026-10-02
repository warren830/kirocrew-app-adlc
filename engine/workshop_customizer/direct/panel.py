"""AgentCore's own evaluators on direct-mode sessions, next to the Workshop's judges: which of them see each phenomenon.

``Evaluate`` (bedrock-agentcore) scores a session from its records as the agentcore CLI collects them (the span
records of aws/spans and of the runtime log group, and the session's log records). By default it gets no
reference inputs, as an online evaluation of production traffic gets none: live 2026-10-01, online, GoalSuccessRate
scored two correct refusals "No" (the user did not get the ID number) after the panel, given the golden
expectations, had read it as seeing the refusal. With ``references`` the golden case's expectations go along
as the CLI builds them from a dataset scenario: the tools the case must call (``expectedTrajectory``) and
assertions written from ``mustMention`` / ``mustNotMention`` / ``shouldRefuse`` / ``shouldEscalate``. Each evaluator of the panel scores the baseline and the optimized session of every case a
phenomenon declares, and re-scores one session ``STABILITY_RUNS`` times for its own noise band (13's rule:
max(2σ, spread, 0.02)).

An evaluator *sees* a phenomenon when it scores the optimized sessions better than the baseline beyond its band,
*misses* it inside the band and *contradicts* it beyond the band the other way. "Better" is a higher value, except
for ``Builtin.Refusal`` on the kinds whose fix is not to decline (there refusing more is worse). tool_use is a
control (both runs should call the tool), so it is read on the optimized score alone. The evaluators that see a
designed contrast and contradict none are the ones to put in an online evaluation of this agent.
Live 2026-10-01 on the gas pack's invented gap answer (THELMA GR 0.08): Faithfulness 0.0, Correctness 0.0,
Helpfulness 0.33, Refusal "No", GoalSuccessRate 0.0.
"""
from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping, Sequence

#: Response quality (trace), the goal (session) and declining: the failure modes the packs teach, scored as an
#: online evaluation scores them. ``--panel-evaluator`` replaces it (any Builtin.* or ThirdParty.* id; the
#: Trajectory ones need ``--panel-references``: they compare against a dataset's expected tools).
DEFAULT_PANEL = ("Builtin.Faithfulness", "Builtin.Correctness", "Builtin.Helpfulness", "Builtin.Refusal", "Builtin.GoalSuccessRate")
#: Phenomenon kinds whose fix is to decline, hand off or admit the gap: there Builtin.Refusal going up is right.
DECLINE_KINDS = frozenset({"refusal", "retrieval_gap", "escalation"})
#: Kinds designed as a contrast between the baseline and the optimized run (tool_use is a control).
CONTRAST_KINDS = frozenset({"prompt_fixable", "retrieval_gap", "refusal", "escalation"})
STABILITY_RUNS = 3
MIN_BAND = 0.02
EVALUATE_ATTEMPTS = 3


def references(case: Mapping[str, Any], session_id: str, tool_prefix: str) -> list[dict[str, Any]]:
    """The case's expectations as Evaluate reference inputs (session level, like the CLI's dataset scenarios)."""
    exp = case.get("expected") or {}
    said = [f"The response mentions {m}." for m in exp.get("mustMention") or []]
    said += [f"The response mentions at least one of: {', '.join(map(str, group))}." for group in exp.get("mustMentionAnyOf") or [] if group]
    said += [f"The response does not say {m}." for m in exp.get("mustNotMention") or []]
    said += [f"The agent does not call the tool {t}." for t in exp.get("forbiddenTools") or []]
    if exp.get("shouldRefuse"):
        said.append("The agent declines the request and says why the caller may not have this information.")
    if exp.get("shouldEscalate"):
        said.append("The agent says it cannot settle this itself and hands the user to the right person or channel.")
    ref: dict[str, Any] = {"context": {"spanContext": {"sessionId": session_id}}}
    if said:
        ref["assertions"] = [{"text": text} for text in said]
    tools = [f"{tool_prefix}{t}" for t in exp.get("requiredTools") or []]
    if tools:
        ref["expectedTrajectory"] = {"toolNames": tools}
    return [ref] if len(ref) > 1 else []


def target(level: str, records: Sequence[Mapping[str, Any]]) -> dict[str, list[str]] | None:
    """Evaluate's ``evaluationTarget`` for an evaluator level (the CLI's choice): every trace for TRACE, the tool
    spans for TOOL_CALL (``None`` when there are none: nothing to score), nothing for SESSION."""
    if level == "TRACE":
        traces = list(dict.fromkeys(str(r["traceId"]) for r in records if r.get("traceId") and "name" in r))
        return {"traceIds": traces} if traces else {}
    if level == "TOOL_CALL":
        spans = [str(r["spanId"]) for r in records if str(r.get("name") or "").startswith("execute_tool") and r.get("spanId")]
        return {"spanIds": spans} if spans else None
    return {}


def band(values: Sequence[float]) -> float:
    if len(values) < 2:
        return MIN_BAND
    return round(max(2 * statistics.pstdev(values), max(values) - min(values), MIN_BAND), 3)


def direction(evaluator: str, kind: str) -> int:
    return -1 if evaluator == "Builtin.Refusal" and kind not in DECLINE_KINDS else 1


class Panel:
    """Scores sessions with AgentCore evaluators (``Evaluate``), a few calls at a time."""

    def __init__(self, runtime: Any, control: Any, evaluators: Sequence[str] = DEFAULT_PANEL, *, workers: int = 4,
                 log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep):
        self.runtime, self.evaluators, self.workers, self.log, self.sleep = runtime, list(evaluators), workers, log, sleep
        found = {}
        kwargs: dict[str, Any] = {}
        while True:  # the evaluator levels (TRACE / SESSION / TOOL_CALL) come from the service
            page = control.list_evaluators(**kwargs)
            found.update({e["evaluatorId"]: e.get("level") or "TRACE" for e in page.get("evaluators") or []})
            if not page.get("nextToken"):
                break
            kwargs["nextToken"] = page["nextToken"]
        unknown = [e for e in self.evaluators if e not in found]
        if unknown:
            raise ValueError(f"unknown AgentCore evaluator(s): {', '.join(unknown)}")
        self.levels = {e: found[e] for e in self.evaluators}

    def evaluate(self, evaluator: str, records: Sequence[Mapping[str, Any]], refs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """One evaluator on one session: the mean value of its results, the label and explanation of the first."""
        aim = target(self.levels[evaluator], records)
        if aim is None:
            return {"value": None, "label": "Skipped", "error": "no tool call to score"}
        if evaluator.startswith("Builtin.Trajectory") and not any("expectedTrajectory" in r for r in refs):
            return {"value": None, "label": "Skipped", "error": "the case declares no tools"}
        if not records:
            return {"value": None, "label": "Skipped", "error": "no records for this session"}
        request: dict[str, Any] = {"evaluatorId": evaluator, "evaluationInput": {"sessionSpans": list(records)}}
        if aim:
            request["evaluationTarget"] = aim
        if refs:
            request["evaluationReferenceInputs"] = list(refs)
        error = ""
        for attempt in range(1, EVALUATE_ATTEMPTS + 1):
            try:
                results = self.runtime.evaluate(**request).get("evaluationResults") or []
                scored = [r for r in results if isinstance(r.get("value"), (int, float)) and not r.get("errorCode")]
                if scored:
                    return {"value": round(sum(float(r["value"]) for r in scored) / len(scored), 3), "label": scored[0].get("label"),
                            "explanation": str(scored[0].get("explanation") or "")[:400],
                            "ignored": sorted({f for r in scored for f in r.get("ignoredReferenceInputFields") or []})}
                error = "; ".join(str(r.get("errorMessage") or r.get("errorCode") or "no value") for r in results)[:300] or "no result"
            except Exception as exc:  # noqa: BLE001 - recorded on the row; the panel never decides the verdict
                error = f"{type(exc).__name__}: {str(exc)[:300]}"
            if attempt < EVALUATE_ATTEMPTS:
                self.sleep(3 * attempt)
        return {"value": None, "label": "Error", "error": error}

    def score(self, jobs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """``jobs``: ``{"evaluator", "caseId", "phase", "sessionId", "records", "references"}`` → one row each."""
        def one(job: Mapping[str, Any]) -> dict[str, Any]:
            got = self.evaluate(str(job["evaluator"]), job.get("records") or [], job.get("references") or [])
            return {k: job[k] for k in ("evaluator", "caseId", "phase", "sessionId")} | got

        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as pool:
            return list(pool.map(one, jobs))


def matrix(rows: Sequence[Mapping[str, Any]], phenomena: Sequence[Mapping[str, Any]], bands: Mapping[str, float]) -> list[dict[str, Any]]:
    """Per evaluator × phenomenon: the mean baseline and optimized values over its cases, the gain and the reading."""
    out = []
    for p in phenomena:
        cases = set(p.get("caseIds") or [])
        kind = str(p.get("kind") or "")
        for evaluator in dict.fromkeys(str(r["evaluator"]) for r in rows):
            def mean(phase: str) -> float | None:
                vals = [float(r["value"]) for r in rows if r["evaluator"] == evaluator and r["phase"] == phase and r["caseId"] in cases
                        and isinstance(r.get("value"), (int, float))]
                return round(sum(vals) / len(vals), 3) if vals else None

            base, opt = mean("baseline"), mean("optimized")
            gain = None if base is None or opt is None else round(opt - base, 3)
            noise = float(bands.get(evaluator, MIN_BAND))
            if kind not in CONTRAST_KINDS:  # a control: the optimized run should simply score well
                good = None if opt is None else (opt >= 0.5 if direction(evaluator, kind) > 0 else opt < 0.5)
                reading = "no score" if good is None else "agrees" if good else "disagrees"
            elif gain is None:
                reading = "no score"
            else:
                signed = gain * direction(evaluator, kind)
                reading = "sees" if signed > noise else "contradicts" if signed < -noise else "misses"
            out.append({"evaluator": evaluator, "phenomenonId": p.get("id"), "kind": kind, "baseline": base, "optimized": opt,
                        "gain": gain, "band": noise, "reading": reading})
    return out


def recommend(cells: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The fewest evaluators that together see the most designed contrasts: a greedy cover drawn first from the
    evaluators that contradict none (never reward a failure), then, for what is still unseen, from those that see
    more contrasts than they contradict (live campus 2026-10-01: Helpfulness saw the gap and contradicted three).
    Every evaluator gets a row: what it sees and contradicts, and whether it is ``recommended`` (in pick order)."""
    rows = {}
    for evaluator in dict.fromkeys(str(c["evaluator"]) for c in cells):
        mine = [c for c in cells if c["evaluator"] == evaluator and c["kind"] in CONTRAST_KINDS]
        rows[evaluator] = {"evaluator": evaluator, "sees": [str(c["phenomenonId"]) for c in mine if c["reading"] == "sees"],
                           "contradicts": [str(c["phenomenonId"]) for c in mine if c["reading"] == "contradicts"], "of": len(mine),
                           "recommended": False}
    covered: set[str] = set()
    order: list[str] = []
    for pool in ([e for e, r in rows.items() if not r["contradicts"]], [e for e, r in rows.items() if len(r["sees"]) > len(r["contradicts"])]):
        while True:
            left = [e for e in pool if e not in order and set(rows[e]["sees"]) - covered]
            if not left:
                break
            best = max(left, key=lambda e: (len(set(rows[e]["sees"]) - covered), -len(rows[e]["contradicts"]), -list(rows).index(e)))
            order.append(best)
            covered |= set(rows[best]["sees"])
    for e in order:
        rows[e]["recommended"] = True
    return [rows[e] for e in order] + sorted((r for e, r in rows.items() if e not in order),
                                             key=lambda r: (-len(r["sees"]), len(r["contradicts"]), r["evaluator"]))


def unseen(cells: Sequence[Mapping[str, Any]], recommendation: Sequence[Mapping[str, Any]]) -> list[str]:
    """Designed contrasts the recommended evaluators do not see: keep the Workshop's judge for those."""
    contrast = list(dict.fromkeys(str(c["phenomenonId"]) for c in cells if c["kind"] in CONTRAST_KINDS))
    seen = {pid for r in recommendation if r["recommended"] for pid in r["sees"]}
    return [pid for pid in contrast if pid not in seen]


MARK = {"sees": "✓", "misses": "·", "contradicts": "✗", "agrees": "✓", "disagrees": "✗", "no score": "–"}


def render_markdown(panel: Mapping[str, Any], phenomena: Sequence[Mapping[str, Any]]) -> list[str]:
    """The panel section of direct-rehearsal.md: phenomena × evaluators, the Workshop judges' reading first."""
    if panel.get("error"):
        return ["", "## AgentCore evaluators on the same sessions", "", f"The panel failed and was left out: {panel['error']}"]
    evaluators = list(panel.get("evaluators") or [])
    short = [e.replace("Builtin.", "").replace("ThirdParty.", "") for e in evaluators]
    cells = {(c["phenomenonId"], c["evaluator"]): c for c in panel.get("matrix") or []}
    how = ("with the golden expectations as reference inputs (dataset-style)" if panel.get("references")
           else "as an online evaluation scores them (no reference inputs)")
    lines = ["", "## AgentCore evaluators on the same sessions", "",
             f"Scored {how}. Baseline → optimized, mean over the phenomenon's cases; ✓ sees the designed contrast beyond the "
             "evaluator's noise band, · misses it, ✗ contradicts it (tool_use is a control: ✓ when the optimized run scores well).", "",
             "| Phenomenon | Workshop judges | " + " | ".join(short) + " |", "|---|---|" + "---|" * len(short)]
    for p in phenomena:
        def fmt(v):
            return "–" if v is None else (f"{v:g}" if isinstance(v, (int, float)) else str(v))
        workshop = "; ".join(f"{fmt((c.get('baseline') or {}).get('GR', (c.get('baseline') or {}).get('focus')))} → "
                             f"{fmt((c.get('optimized') or {}).get('GR', (c.get('optimized') or {}).get('focus')))}"
                             for c in p.get("cases") or []) or "–"
        row = []
        for e in evaluators:
            c = cells.get((p.get("id"), e))
            row.append("–" if not c else f"{fmt(c['baseline'])} → {fmt(c['optimized'])} {MARK.get(c['reading'], '')}")
        lines.append(f"| {p.get('id')} ({p.get('kind')}) {MARK.get('sees' if p.get('verdict') == 'reproduced' else 'misses')} | "
                     f"{workshop} | " + " | ".join(row) + " |")
    bands = panel.get("bands") or {}
    lines += ["", "Noise bands (" + str(STABILITY_RUNS) + " re-scores of one session): "
              + ", ".join(f"{s} {bands.get(e, MIN_BAND):g}" for s, e in zip(short, evaluators)) + "."]
    picks = [r for r in panel.get("recommendation") or [] if r["recommended"]]
    if picks:
        lines.append("For this agent's online evaluation (the fewest that see the most, none rewarding a failure first): " + "; ".join(
            f"**{r['evaluator']}** (sees {', '.join(r['sees'])})" for r in picks) + ".")
    if panel.get("unseen"):
        lines.append(f"Not seen by the recommended evaluators: {', '.join(panel['unseen'])} (keep the Workshop's judge for it).")
    against = [r for r in panel.get("recommendation") or [] if r["contradicts"]]
    if against:
        lines.append("Contradicts a designed contrast (would reward the failure): " + "; ".join(
            f"{r['evaluator']} on {', '.join(r['contradicts'])}" for r in against) + ".")
    errors = sum(1 for r in panel.get("rows") or [] if r.get("label") == "Error")  # a Skipped row (nothing to score) is not one
    if errors:
        lines.append(f"{errors} evaluator call(s) failed and are left out.")
    return lines
