"""Verify any AgentCore Harness against behaviour contracts: N rounds, L1, AgentCore's evaluators, an online plan.

A contract is a question plus what the agent must and must not do: the golden-case ``expected`` checks L1 reads
(mustMention / mustMentionAnyOf / mustNotMention / requiredTools / forbiddenTools / shouldRefuse / shouldEscalate).
``tools/verify_agent.py`` takes them from a YAML or JSON file, or from a pack's practice set. The Harness can be any
in the account (built by Launchpad, the agentcore CLI or this platform). It is only invoked, never changed: its
own system prompt and model answer unless a prompt or model is given for the run (per call).

Every round asks every contract once, in a fresh session; the traces settle; L1 checks each answer on its spans
and text. A contract holds when it passes in every round (``robust``), the bar a single run hides (one rehearsal
overstated three of four packs, 2026-10-01). With a panel, AgentCore's built-in evaluators score every round's
sessions as an online evaluation would (no reference inputs), and each is read against L1: it *agrees* when it
scores the sessions L1 passed above the ones L1 failed by more than its noise band, and *contradicts* the other way
round. The agreeing ones are this agent's online evaluators; a failing contract none of them scores low stays
with L1 (:func:`online_panel` feeds ``direct.online.plan``).
"""
from __future__ import annotations

import json
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import l1
from .aws import client
from .panel import STABILITY_RUNS, Panel, band as panel_band
from .run import Asked, SETTLE_TIMEOUT, ask_rows, new_session_id, out_text
from .traces import TraceStore

EXPECTED_KEYS = frozenset({"mustMention", "mustMentionAnyOf", "mustNotMention", "requiredTools", "forbiddenTools",
                           "shouldRefuse", "shouldEscalate"})
CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
#: Fewer failing sessions than this give no reading (live 2026-10-01: one failure in the scored round made two
#: evaluators "agree", one of them flagging nothing).
MIN_FAILURES = 2


class ContractError(ValueError):
    pass


def check_contracts(cases: Sequence[Any]) -> list[dict[str, Any]]:
    """Each contract: ``id``, ``query``, ``expected`` (L1's checks; at least one), optional ``actorId`` and ``label``."""
    out, seen = [], set()
    for i, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            raise ContractError(f"contract {i} is not a mapping")
        cid, query, expected = case.get("id"), case.get("query"), case.get("expected")
        if not isinstance(cid, str) or not CASE_ID.match(cid) or cid in seen:
            raise ContractError(f"contract {i}: id must be a unique short identifier, not {cid!r}")
        if not isinstance(query, str) or not query.strip():
            raise ContractError(f"contract {cid}: query is empty")
        if not isinstance(expected, dict) or not expected or set(expected) - EXPECTED_KEYS:
            raise ContractError(f"contract {cid}: expected needs one or more of {', '.join(sorted(EXPECTED_KEYS))} "
                                f"(unknown: {', '.join(sorted(set(expected or {}) - EXPECTED_KEYS)) or 'none'})")
        seen.add(cid)
        out.append({"id": cid, "query": query.strip(), "expected": dict(expected), "actorId": str(case.get("actorId") or "verify"),
                    "label": str(case.get("label") or cid), "category": case.get("category")})
    if not out:
        raise ContractError("no contracts")
    return out


def load_contracts(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``(contracts, l1 config)`` from a YAML / JSON file: a list of contracts, or ``{contracts: [...], l1: {...}}``
    (``l1``: refusalMarkers / escalationMarkers / escalationTools, as a scenario's ``evaluation.l1``)."""
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases, l1cfg = (data, {}) if isinstance(data, list) else ((data or {}).get("contracts"), (data or {}).get("l1") or {})
    return check_contracts(cases or []), dict(l1cfg)


def contracts_from_pack(project_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """A built pack's practice cases as contracts, with its L1 markers."""
    release = project_dir / "build" / "release" / "pack"
    pack = json.loads((release / "pack.json").read_text(encoding="utf-8"))
    practice = json.loads((release / "golden" / "practice.json").read_text(encoding="utf-8"))
    return check_contracts([{k: c.get(k) for k in ("id", "query", "expected", "actorId", "label", "category")} for c in practice
                            if c.get("expected")]), dict(pack.get("l1") or {})


def harness_info(ctl: Any, ident: str) -> dict[str, Any]:
    """The Harness named, or with this id or ARN: its ARN, runtime, model and gateway tool targets."""
    found = None
    kwargs: dict[str, Any] = {}
    while found is None:
        page = ctl.list_harnesses(**kwargs)
        found = next((h for h in page.get("harnesses") or [] if ident in (h.get("harnessName"), h.get("harnessId"), h.get("arn"))), None)
        if found is not None or not page.get("nextToken"):
            break
        kwargs["nextToken"] = page["nextToken"]
    if found is None:
        raise ContractError(f"no Harness named {ident!r} in this account and region")
    harness = ctl.get_harness(harnessId=found["harnessId"])["harness"]
    runtime = (harness.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {}
    targets = [str(t.get("name")) for t in harness.get("tools") or [] if isinstance(t, dict) and t.get("type") == "agentcore_gateway"]
    return {"id": found["harnessId"], "name": harness.get("harnessName") or found.get("harnessName"), "arn": harness["arn"],
            "runtimeId": runtime.get("agentRuntimeId"), "model": ((harness.get("model") or {}).get("bedrockModelConfig") or {}).get("modelId"),
            "targets": targets}


def l1_pack(info: Mapping[str, Any], cases: Sequence[Mapping[str, Any]], l1cfg: Mapping[str, Any]) -> dict[str, Any]:
    """The few pack fields L1 reads, for an agent that has no pack: its tools (named by the contracts), the gateway
    target (span tool names are ``<target>___<tool>``) and the refusal / escalation markers."""
    tools = sorted({t for c in cases for key in ("requiredTools", "forbiddenTools") for t in c["expected"].get(key) or [] if isinstance(t, str)}
                   | {t for t in l1cfg.get("escalationTools") or [] if isinstance(t, str)})
    target = (info.get("targets") or [""])[0]
    return {"packId": info.get("name"), "l1": dict(l1cfg), "tools": [{"name": t} for t in tools], "namespace": {"toolTargetName": target}}


def agreement(rows: Sequence[Mapping[str, Any]], verdicts: Mapping[str, str], bands: Mapping[str, float]) -> list[dict[str, Any]]:
    """Per evaluator: its mean on the sessions L1 passed and on those L1 failed, and the reading (``agrees`` /
    ``contradicts`` / ``misses`` / ``no failures`` when every session passed, so nothing can be told)."""
    out = []
    for evaluator in dict.fromkeys(str(r["evaluator"]) for r in rows):
        scored = [(verdicts.get(str(r["sessionId"])), float(r["value"])) for r in rows
                  if r["evaluator"] == evaluator and r.get("phase") != "stability" and isinstance(r.get("value"), (int, float))]
        passed = [v for verdict, v in scored if verdict == "pass"]
        failed = [v for verdict, v in scored if verdict == "fail"]
        noise = float(bands.get(evaluator, 0.02))
        low_on = sorted({str(r["caseId"]) for r in rows if r["evaluator"] == evaluator and verdicts.get(str(r["sessionId"])) == "fail"
                         and isinstance(r.get("value"), (int, float)) and r["value"] < 0.5})
        if not passed or len(failed) < MIN_FAILURES:
            reading = ("no passes" if not passed else "no failures" if not failed else f"too few failures ({len(failed)})")
            gap = None
        else:
            gap = round(statistics.mean(passed) - statistics.mean(failed), 3)
            reading = "agrees" if gap > noise else "contradicts" if gap < -noise else "misses"
        if evaluator == "Builtin.Refusal":  # 1 = refused: what happened, not how good it was
            reading, gap = "rate only", None
        out.append({"evaluator": evaluator, "passMean": round(statistics.mean(passed), 3) if passed else None,
                    "failMean": round(statistics.mean(failed), 3) if failed else None, "gap": gap, "band": noise,
                    "reading": reading, "flags": low_on})
    return out


def online_panel(readings: Sequence[Mapping[str, Any]], failing: Sequence[str]) -> dict[str, Any]:
    """The shape ``direct.online.plan`` takes: the agreeing evaluators (what they flag), and the failing contracts
    none of them flags (``unseen``: keep L1's check for those)."""
    picks = [{"evaluator": r["evaluator"], "sees": list(r["flags"]), "contradicts": [], "recommended": True} for r in readings
             if r["reading"] == "agrees" and r["flags"]]  # it must score some failing contract low, not just a little lower
    flagged = {cid for p in picks for cid in p["sees"]}
    return {"recommendation": picks, "unseen": [cid for cid in failing if cid not in flagged], "references": False}


class Verify:
    """Rounds of contracts on one Harness, judged by L1 and (optionally) AgentCore's evaluators."""

    def __init__(self, session: Any, harness: Mapping[str, Any], cases: Sequence[Mapping[str, Any]], l1cfg: Mapping[str, Any], *,
                 region: str, out: Path, repeat: int = 3, workers: int = 4, panel: Sequence[str] = (), prompt: str | None = None,
                 model: str | None = None, settle_timeout: float = SETTLE_TIMEOUT, log: Callable[[str], None] = print):
        self.session, self.harness, self.cases, self.l1cfg = session, dict(harness), list(cases), dict(l1cfg)
        self.region, self.out, self.repeat, self.workers = region, out, max(1, int(repeat)), workers
        self.panel, self.prompt, self.model, self.settle_timeout, self.log = list(panel), prompt, model, settle_timeout, log
        self.pack = l1_pack(self.harness, self.cases, self.l1cfg)

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        scorer = (Panel(client(self.session, "bedrock-agentcore", self.region), client(self.session, "bedrock-agentcore-control", self.region),
                        self.panel, workers=self.workers, log=self.log) if self.panel else None)  # evaluator ids checked first
        runtime = client(self.session, "bedrock-agentcore", self.region)
        logs = client(self.session, "logs", self.region)
        group = f"/aws/bedrock-agentcore/runtimes/{self.harness['runtimeId']}-DEFAULT"
        rounds, panel_rows, verdicts, jobs = [], [], {}, []
        for number in range(1, self.repeat + 1):
            epoch = int(time.time())
            since = epoch * 1000 - 60_000
            rows = [Asked(index=i, case_id=c["id"], query=c["query"], session_id=new_session_id(epoch), probe=False, started_ms=0,
                          actor=f"{c['actorId']}-verify-r{number}-{epoch}-q{i}") for i, c in enumerate(self.cases, 1)]
            self.log(f"round {number} of {self.repeat}: {len(rows)} contract(s)")
            asked = ask_rows(runtime, self.harness["arn"], rows, prompt=self.prompt, model=self.model, workers=self.workers, log=self.log)
            run_dir = self.out / "runs" / f"round{number}-{epoch}"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "sessions.tsv").write_text("".join(f"{r.index}\t{r.case_id}\t{r.session_id}\t{r.actor}\t{r.started_ms}\t0\n" for r in asked),
                                                  encoding="utf-8")
            for r in asked:
                (run_dir / f"q{r.index}.out").write_text(out_text(r), encoding="utf-8")
            store = TraceStore(logs, group, since)
            store.wait_complete({r.session_id: r.text for r in asked}, timeout=self.settle_timeout, progress=self.log)
            doc = l1.evaluate_run(pack=self.pack, cases=self.cases, run_dir=run_dir, phase=f"round{number}", since_ms=since,
                                  source=l1.LogsSpanSource(logs, since))
            (run_dir / "l1.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            results = {c["caseId"]: c for c in doc["cases"]}
            rounds.append({"round": number, "cases": {
                cid: {"verdict": c["verdict"], "failed": [k["code"] for k in c["checks"] if k["status"] == "fail"],
                      "tools": [t["tool"] for t in c.get("toolsCalled") or []], "sessionId": c["sessionId"],
                      "answer": c.get("responseExcerpt", "")[:300]} for cid, c in results.items()},
                "invokeErrors": [{"caseId": r.case_id, "error": r.error} for r in asked if r.error]})
            if scorer:  # every round's sessions: more failing sessions to read the evaluators against
                verdicts.update({c["sessionId"]: c["verdict"] for c in doc["cases"]})
                jobs += [{"evaluator": e, "caseId": r.case_id, "phase": f"round{number}", "sessionId": r.session_id,
                          "records": store.evaluation_records(r.session_id), "references": []} for r in asked for e in self.panel]
        if scorer and jobs:
            jobs += [dict(jobs[i], phase="stability") for i in range(len(self.panel)) for _ in range(STABILITY_RUNS)]
            self.log(f"panel: {len(self.panel)} AgentCore evaluator(s), {len(jobs)} calls")
            panel_rows = scorer.score(jobs)
        return self.document(rounds, panel_rows, verdicts, seconds=time.monotonic() - started)

    def document(self, rounds: Sequence[Mapping[str, Any]], panel_rows: Sequence[Mapping[str, Any]], verdicts: Mapping[str, str], *,
                 seconds: float) -> dict[str, Any]:
        contracts = []
        for case in self.cases:
            seen = [r["cases"].get(case["id"]) or {"verdict": "missing", "failed": []} for r in rounds]
            passes = sum(s["verdict"] == "pass" for s in seen)
            failed = sorted({f for s in seen for f in s.get("failed") or []})
            contracts.append({"id": case["id"], "label": case["label"], "query": case["query"], "passes": passes, "rounds": len(seen),
                              "holds": passes == len(seen), "verdicts": [s["verdict"] for s in seen], "failed": failed,
                              "expected": case["expected"], "note": phrase_note(case["expected"], failed)})
        doc: dict[str, Any] = {
            "schema": "workshop-customizer/verify/1", "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "harness": {k: self.harness.get(k) for k in ("name", "id", "arn", "runtimeId", "model")},
            "promptOverride": self.prompt is not None, "modelOverride": self.model, "repeat": self.repeat, "seconds": round(seconds, 1),
            "robust": all(c["holds"] for c in contracts), "holding": sum(c["holds"] for c in contracts), "contracts": contracts,
            "rounds": list(rounds)}
        if panel_rows:
            bands = {e: panel_band([float(r["value"]) for r in panel_rows if r["phase"] == "stability" and r["evaluator"] == e
                                    and isinstance(r.get("value"), (int, float))]) for e in self.panel}
            readings = agreement(panel_rows, verdicts, bands)
            failing = sorted({cid for r in rounds for cid, c in r["cases"].items() if c["verdict"] == "fail"})
            doc["panel"] = {"evaluators": list(self.panel), "references": False, "bands": bands, "readings": readings,
                            "rows": [r for r in panel_rows if r["phase"] != "stability"], "online": online_panel(readings, failing)}
        return doc


_CJK = re.compile(r"[\u4e00-\u9fff]")


def phrase_note(expected: Mapping[str, Any], failed: Sequence[str]) -> str | None:
    """When a contract fails only on mustMention / mustMentionAnyOf and some expected item is a phrase (more than four
    CJK characters, or three words), L1's literal match may be what failed, not the answer (live 2026-10-01: every
    correct refusal missed '只能查询本人' by saying '只能查询您本人')."""
    if not failed or set(failed) - {"mustMention", "mustMentionAnyOf"}:
        return None
    items = list(expected.get("mustMention") or []) + [t for group in expected.get("mustMentionAnyOf") or [] for t in group or []]
    phrases = [t for t in items if isinstance(t, str) and (len(_CJK.findall(t)) > 4 or len(t.split()) >= 3)]
    return f"literal phrase check ({', '.join(phrases[:3])}): read the answer before trusting this failure" if phrases else None


def render_markdown(doc: Mapping[str, Any]) -> str:
    h = doc["harness"]
    lines = [f"# Verification of `{h.get('name')}`", "",
             f"{doc['holding']} of {len(doc['contracts'])} contract(s) hold in every one of {doc['repeat']} round(s): "
             f"**{'robust' if doc['robust'] else 'not robust'}**. Model `{doc.get('modelOverride') or h.get('model')}`"
             f"{', prompt overridden for the run' if doc.get('promptOverride') else ', its own prompt'}; {doc['seconds'] / 60:.1f} min.", "",
             "| Contract | Rounds passed | Failed checks | Question |", "|---|---|---|---|"]
    lines += [f"| {c['id']} | {c['passes']}/{c['rounds']} | {', '.join(c['failed']) or '—'} | {c['query'][:80]} |" for c in doc["contracts"]]
    failing = [c for c in doc["contracts"] if not c["holds"]]
    if failing:
        lines += ["", "## What failed", ""]
        for c in failing:
            answer = next((r["cases"][c["id"]].get("answer") for r in doc["rounds"] if (r["cases"].get(c["id"]) or {}).get("verdict") == "fail"), "")
            lines.append(f"- **{c['id']}** expected {json.dumps(c.get('expected') or {}, ensure_ascii=False)[:200]}; a failing answer: "
                         f"\"{(answer or '').strip()[:160]}\"" + (f" — {c['note']}" if c.get("note") else ""))
    panel = doc.get("panel")
    if panel:
        lines += ["", "## AgentCore evaluators against L1 (every round's sessions, scored as online evaluation does)", "",
                  "| Evaluator | Mean on L1 passes | Mean on L1 failures | Reading | Flags (failing contracts it scored low) |",
                  "|---|---|---|---|---|"]
        lines += [f"| {r['evaluator']} | {r['passMean'] if r['passMean'] is not None else '—'} | "
                  f"{r['failMean'] if r['failMean'] is not None else '—'} | {r['reading']} | {', '.join(r['flags']) or '—'} |"
                  for r in panel["readings"]]
        picks = [p["evaluator"] for p in panel["online"]["recommendation"]]
        lines.append("")
        lines.append(f"For this agent's online evaluation: {', '.join(picks)}." if picks else
                     "No evaluator separated the passing sessions from the failing ones (or nothing failed): keep L1's checks.")
        if panel["online"]["unseen"]:
            lines.append(f"Failing contracts no recommended evaluator scores low: {', '.join(panel['online']['unseen'])} (keep L1 for them).")
    errors = [e for r in doc["rounds"] for e in r.get("invokeErrors") or []]
    if errors:
        lines += ["", f"{len(errors)} invocation error(s), e.g. {errors[0]['caseId']}: {errors[0]['error'][:160]}"]
    return "\n".join(lines) + "\n"
