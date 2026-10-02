"""One direct-mode round: provision, ask every question twice, judge, and rehearse the result.

The questions are asked the way 06 / 09 / 10 ask them: the first conversation once, then every practice
question with the baseline prompt and again with the candidate prompt, each as its own session and memory actor
``<persona>-<phase>-<epoch>-q<i>``. The prompt goes with each call (InvokeHarness ``systemPrompt``), so the
Harness never has to be updated between the phases. The run records take the Guided Run's shapes on disk
(``sessions.tsv``, ``q<i>.out``, ``scores.tsv``) and in the step records, so L1 (``l1.evaluate_run``), the report
and the rehearsal read them unchanged.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from .. import l1, rehearsal
from . import judges
from .aws import Provisioner, client as aws_client
from .panel import STABILITY_RUNS as PANEL_STABILITY_RUNS, Panel, band as panel_band, matrix as panel_matrix
from .panel import recommend as panel_recommend, references as panel_references, render_markdown as panel_markdown, unseen as panel_unseen
from .names import DirectNames
from .traces import TraceStore, retrieval_trace

#: 13-judge-stability.sh: three re-scores of one trace, then ✅ std ≤ 0.03 / ⚠️ ≤ 0.08 / ❌ above.
STABILITY_RUNS = 3
#: The phases of a round, in order; ``DirectRun(on_stage=…)`` is called as each begins.
STAGES = ("provision", "ask", "settle", "judge", "rehearse")
#: A call that fails with no text is asked again (in a new session), as a student would retry.
ASK_ATTEMPTS = 3
#: Settle-before-eval (render): never judge a trace whose final answer the evaluator cannot see yet.
SETTLE_TIMEOUT = 420


@dataclass
class Asked:
    index: int
    case_id: str
    query: str
    session_id: str
    actor: str
    started_ms: int
    probe: bool
    text: str = ""
    stop_reason: str | None = None
    error: str | None = None
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    tools: list[str] = field(default_factory=list)


def ask_rows(client: Any, harness_arn: str, rows: Sequence[Asked], *, prompt: str | None = None, model: str | None = None,
             workers: int = 4, log: Callable[[str], None] = print) -> list[Asked]:
    """Ask every row on a Harness, ``workers`` at a time. ``prompt`` / ``model`` override the Harness's own for these
    calls only (InvokeHarness takes both per call; the Harness is never updated); without them it answers as
    configured. A call that fails with no text is asked again in a new session (``ASK_ATTEMPTS``)."""
    override: dict[str, Any] = {}
    if model:
        override["model"] = {"bedrockModelConfig": {"modelId": model}}
    if prompt is not None:
        override["systemPrompt"] = [{"text": prompt}]

    def one(asked: Asked) -> Asked:
        for attempt in range(1, ASK_ATTEMPTS + 1):
            started = time.monotonic()
            asked.started_ms = int(time.time() * 1000)
            asked.text, asked.error, asked.stop_reason, asked.tools = "", None, None, []
            asked.input_tokens = asked.output_tokens = 0
            try:
                response = client.invoke_harness(harnessArn=harness_arn, runtimeSessionId=asked.session_id, actorId=asked.actor,
                                                 messages=[{"role": "user", "content": [{"text": asked.query}]}], **override)
                stream_result(response["stream"], asked)
            except Exception as exc:  # noqa: BLE001 - the CLI prints the error line, L1 reads it as an invoke error
                asked.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            asked.seconds = round(time.monotonic() - started, 1)
            # Live 2026-10-01 the first call on a new Harness failed with no text; ask again in a new session.
            if not asked.error or asked.text.strip() or attempt == ASK_ATTEMPTS:
                return asked
            log(f"    {asked.case_id}: {asked.error[:120]} — asking again ({attempt}/{ASK_ATTEMPTS - 1})")
            asked.session_id = new_session_id(int(time.time()))
            time.sleep(5 * attempt)
        return asked

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, rows))


def resources_path(project_dir: Path) -> Path:
    """The pack's direct resources (``Provisioner`` state), wherever a run writes its records: the App, the
    cleanup and every run read this one file."""
    return project_dir / "build" / "direct" / "resources.json"


def new_session_id(epoch: int) -> str:
    """InvokeHarness wants at least 33 characters; the Workshop CLI's ids look like this too."""
    return f"direct-{uuid.uuid4().hex}-{epoch}"


def stream_result(stream: Any, asked: Asked) -> Asked:
    """The text the CLI would print (every turn's text), the tool calls, the stop reason and token usage."""
    parts: list[str] = []
    for event in stream:
        if "contentBlockStart" in event:
            use = (event["contentBlockStart"].get("start") or {}).get("toolUse")
            if use:
                asked.tools.append(str(use.get("name") or ""))
        elif "contentBlockDelta" in event:
            text = (event["contentBlockDelta"].get("delta") or {}).get("text")
            if text:
                parts.append(text)
        elif "messageStop" in event:
            asked.stop_reason = event["messageStop"].get("stopReason")
        elif "metadata" in event:
            usage = event["metadata"].get("usage") or {}
            asked.input_tokens += int(usage.get("inputTokens") or 0)
            asked.output_tokens += int(usage.get("outputTokens") or 0)
        else:
            for key, value in event.items():  # a stream exception (throttling, model error, validation…)
                if key.endswith("Exception") or key.endswith("Error"):
                    asked.error = f"{key}: {str((value or {}).get('message') or value)[:300]}"
    asked.text = "".join(parts)
    return asked


def out_text(asked: Asked) -> str:
    """q<i>.out as 09 leaves it: the streamed answer, the CLI's error line if any, the session footer."""
    lines = [asked.text.rstrip()] if asked.text.strip() else []
    if asked.stop_reason == "max_tokens":
        lines.append(l1.TRUNCATED_RE.pattern.lstrip("^"))
    if asked.error:
        lines.append(f"Error: {asked.error}")
    lines.append(f"Session: {asked.session_id}")
    return "\n".join(lines) + "\n"


def prices(release_dir: Path, model: str, region: str) -> tuple[float, float] | None:
    """The verified price snapshot 11-cost-latency.sh carries for ``region:model`` (USD per 1M tokens)."""
    script = (release_dir / "11-cost-latency.sh").read_text(encoding="utf-8")
    found = re.search(rf"^\s*{re.escape(region)}:{re.escape(model)}\)\s*DEFAULT_PRICE_IN=([0-9.]+);\s*DEFAULT_PRICE_OUT=([0-9.]+)", script, re.M)
    return (float(found.group(1)), float(found.group(2))) if found else None


def release_model(release_dir: Path) -> str:
    """The Agent model the release pins (00-config.sh WORKSHOP_MODEL_ID, which 04 deploys the Harness with)."""
    found = re.search(r'^export WORKSHOP_MODEL_ID="\$\{WORKSHOP_MODEL_ID:-([^}"]+)\}"', (release_dir / "00-config.sh").read_text(encoding="utf-8"), re.M)
    if not found:
        raise ValueError("00-config.sh pins no WORKSHOP_MODEL_ID")
    return found.group(1)


def stability_verdict(values: Sequence[float]) -> dict[str, Any]:
    mean = sum(values) / len(values)
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    verdict = "stable" if std <= 0.03 else "moderate" if std <= 0.08 else "unstable"
    return {"values": [round(v, 3) for v in values], "mean": round(mean, 3), "std": round(std, 3),
            "spread": round(max(values) - min(values), 3), "verdict": verdict}


class DirectRun:
    """One round for a built project (``<project>/build/release``), from the SA's machine."""

    def __init__(self, project_dir: Path, *, session: Any, profile: str | None, account: str, region: str, out: Path,
                 log: Callable[[str], None] = print, workers: int = 4, settle_timeout: float = SETTLE_TIMEOUT,
                 compare_models: Sequence[str] = (), on_stage: Callable[[str], None] | None = None, panel: Sequence[str] = (),
                 repeat: int = 1, panel_references: bool = False):
        self.project_dir, self.session, self.profile = project_dir, session, profile
        self.account, self.region, self.out, self.log = account, region, out, log
        self.workers, self.settle_timeout = workers, settle_timeout
        self.compare_models = [m for m in compare_models if m]
        self.panel = list(dict.fromkeys(e for e in panel if e))  # AgentCore evaluators to run next to the Workshop's (direct.panel)
        self.repeat = max(1, int(repeat))  # rounds after one provisioning: each phenomenon's reproduction count
        self.scorer: Panel | None = None  # built in run(), before provisioning
        self.panel_references = panel_references  # dataset-style scoring; the default scores as online evaluation does
        self.on_stage = on_stage or (lambda _stage: None)  # STAGES, in order (the App's job shows them)
        self.release_dir = project_dir / "build" / "release"
        self.manifest = json.loads((self.release_dir / "RELEASE.json").read_text(encoding="utf-8"))
        self.pack = json.loads((self.release_dir / "pack" / "pack.json").read_text(encoding="utf-8"))
        self.practice = json.loads((self.release_dir / "pack" / "golden" / "practice.json").read_text(encoding="utf-8"))
        snapshot = project_dir / "build" / "instructor" / "scenario-snapshot.yaml"
        self.scenario = yaml.safe_load(snapshot.read_text(encoding="utf-8"))
        self.names = DirectNames.of(self.pack["namespace"], account=account, region=region)
        self.model = release_model(self.release_dir)
        self.judge_model = str((self.scenario.get("evaluation") or {}).get("judgeModel") or self.model)
        self.documents = rehearsal.build_documents(self.scenario, project_dir / "build")
        self.guides = rehearsal.guide_status(project_dir / "build")

    # ------------------------------------------------------------------ questions
    def order(self) -> list[dict[str, Any]]:
        """The practice cases in 09's evaluation order (probes last, the stability case at the very end)."""
        by_id = {c["id"]: c for c in self.practice}
        ids = [cid for cid in (self.pack.get("teaching") or {}).get("evalOrder") or [] if cid in by_id]
        return [by_id[cid] for cid in ids] + [c for c in self.practice if c["id"] not in ids]

    def ask(self, harness_arn: str, rows: Sequence[Asked], prompt: str, model: str | None = None) -> list[Asked]:
        """Ask every row with ``prompt`` (and ``model``: InvokeHarness takes both per call, so neither a phase nor
        a model comparison updates the Harness)."""
        return ask_rows(aws_client(self.session, "bedrock-agentcore", self.region), harness_arn, rows, prompt=prompt, model=model,
                        workers=self.workers, log=self.log)

    def phase_rows(self, phase: str, epoch: int) -> list[Asked]:
        probes = set((self.pack.get("teaching") or {}).get("probeCaseIds") or [])
        tag = f"{phase}-{epoch}"
        return [Asked(index=i, case_id=c["id"], query=c["query"], session_id=new_session_id(epoch),
                      actor=f"{c.get('actorId') or 'student'}-{tag}-q{i}", started_ms=0, probe=c["id"] in probes)
                for i, c in enumerate(self.order(), 1)]

    def write_run(self, phase: str, epoch: int, rows: Sequence[Asked]) -> Path:
        run_dir = self.out / "runs" / f"{phase}-{epoch}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "sessions.tsv").write_text("".join(
            f"{r.index}\t{r.case_id}\t{r.session_id}\t{r.actor}\t{r.started_ms}\t{1 if r.probe else 0}\n" for r in rows), encoding="utf-8")
        for r in rows:
            (run_dir / f"q{r.index}.out").write_text(out_text(r), encoding="utf-8")
        return run_dir

    # ------------------------------------------------------------------ judging
    def cost_latency(self, rows: Sequence[Asked], model: str) -> dict[str, Any] | None:
        """11's figures for ``rows``: mean latency and tokens, and the cost at the release's verified price."""
        usable = [r for r in rows if not r.error and r.input_tokens > 0]
        if not usable:
            return None
        price = prices(self.release_dir, model, self.region)
        cost = [((r.input_tokens * price[0] + r.output_tokens * price[1]) / 1e6) if price else 0.0 for r in usable]
        return {"averageLatencySeconds": round(statistics.mean(r.seconds for r in usable), 1),
                "averageInputTokens": round(statistics.mean(r.input_tokens for r in usable)),
                "averageOutputTokens": round(statistics.mean(r.output_tokens for r in usable)),
                "averageCostUsd": round(statistics.mean(cost), 6), "totalCostUsd": round(sum(cost), 6), "sampleCount": len(usable),
                "inputPricePerMillionUsd": price[0] if price else None, "outputPricePerMillionUsd": price[1] if price else None,
                "modelId": model, "estimateOnly": True, "priced": price is not None}

    def judge_phase(self, phase: str, rows: Sequence[Asked], spans: Mapping[str, list[dict[str, Any]]], run_dir: Path,
                    goal: bool = True) -> list[dict[str, Any]]:
        evaluators = self.release_dir / "evaluators"
        tool = str(self.pack.get("retrievalToolName") or "")
        rag = [r for r in rows if r.probe]
        rag_jobs = [{"sessionSpans": spans.get(r.session_id) or [], "targetTraceId": retrieval_trace(spans.get(r.session_id) or [], tool)} for r in rag]
        goal_jobs = [{"sessionSpans": spans.get(r.session_id) or []} for r in rows] if goal else []
        kwargs = {"evaluators_dir": evaluators, "model": self.judge_model, "profile": self.profile, "region": self.region}
        self.log(f"  {phase}: THELMA on {len(rag_jobs)} probe(s), Mind the Goal on {len(goal_jobs)} question(s)")
        rag_results = judges.judge("thelma", rag_jobs, **kwargs)
        goal_results = judges.judge("mtg", goal_jobs, **kwargs)
        out = [judges.score_row("thelma", r.case_id, r.session_id, job["targetTraceId"], res) for r, job, res in zip(rag, rag_jobs, rag_results)]
        out += [judges.score_row("mtg", r.case_id, r.session_id, None, res) for r, res in zip(rows, goal_results)]
        (run_dir / "scores.tsv").write_text("".join(judges.tsv_line(row) + "\n" for row in out), encoding="utf-8")
        return out

    def l1_compact(self, phase: str, run_dir: Path, since_ms: int, baseline: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        logs = aws_client(self.session, "logs", self.region)
        doc = l1.evaluate_run(pack=self.pack, cases=self.practice, run_dir=run_dir, phase=phase, since_ms=since_ms,
                              source=l1.LogsSpanSource(logs, since_ms), release_version=str(self.manifest["version"]), baseline=baseline)
        (run_dir / "l1.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return doc, l1.compact(doc)

    # ------------------------------------------------------------------ the round
    def panel_phase(self, store: TraceStore, baseline_rows: Sequence[Asked], optimized_rows: Sequence[Asked]) -> dict[str, Any]:
        """AgentCore's evaluators on the sessions of every case a phenomenon declares, both phases, and the
        stability case re-scored for each evaluator's noise band (``direct.panel``)."""
        teaching = self.pack.get("teaching") or {}
        phenomena = teaching.get("phenomena") or []
        declared = {cid for p in phenomena for cid in p.get("caseIds") or []}
        by_id = {c["id"]: c for c in self.practice}
        prefix = f"{self.names.target}___"
        scorer = self.scorer

        def jobs_for(phase: str, row: Asked, times: int = 1) -> list[dict[str, Any]]:
            records = store.evaluation_records(row.session_id)
            refs = panel_references(by_id[row.case_id], row.session_id, prefix) if self.panel_references else []
            return [{"evaluator": e, "caseId": row.case_id, "phase": phase, "sessionId": row.session_id, "records": records, "references": refs}
                    for e in self.panel for _ in range(times)]

        jobs = [j for phase, rows in (("baseline", baseline_rows), ("optimized", optimized_rows))
                for r in rows if r.case_id in declared and r.case_id in by_id for j in jobs_for(phase, r)]
        steady = next((r for r in optimized_rows if r.case_id == teaching.get("stabilityCaseId") and r.case_id in by_id), None)
        jobs += jobs_for("stability", steady, PANEL_STABILITY_RUNS) if steady else []
        self.log(f"panel: {len(self.panel)} AgentCore evaluator(s), {len(jobs)} calls")
        rows = scorer.score(jobs)
        bands = {e: panel_band([float(r["value"]) for r in rows if r["phase"] == "stability" and r["evaluator"] == e
                                and isinstance(r.get("value"), (int, float))]) for e in self.panel}
        cells = panel_matrix([r for r in rows if r["phase"] != "stability"], phenomena, bands)
        recommendation = panel_recommend(cells)
        return {"evaluators": list(self.panel), "levels": scorer.levels, "references": self.panel_references, "bands": bands, "rows": rows,
                "matrix": cells, "recommendation": recommendation, "unseen": panel_unseen(cells, recommendation)}

    def run(self) -> dict[str, Any]:
        """Provision once, then ``repeat`` rounds of asking, settling and judging; the rehearsal judges the last
        round with the earlier ones as its same-release history (each phenomenon's ``replication``)."""
        started = time.monotonic()
        # The panel's evaluator ids are checked now (a typo fails before anything is provisioned, not after the rounds).
        self.scorer = (Panel(aws_client(self.session, "bedrock-agentcore", self.region), aws_client(self.session, "bedrock-agentcore-control", self.region),
                             self.panel, workers=self.workers, log=self.log) if self.panel else None)
        prov = Provisioner(self.session, self.names, release_dir=self.release_dir, pack=self.pack, account=self.account, region=self.region,
                           state_path=resources_path(self.project_dir), log=self.log)
        mark = time.monotonic()
        self.on_stage("provision")
        self.log("provision: bucket, knowledge base, tools, memory, skills, Harness")
        prov.ensure_bucket()
        kb_id = prov.ensure_kb(profile=self.profile)
        tools = prov.ensure_tools(kb_id)
        memory = prov.ensure_memory()
        skills = prov.ensure_skills()
        role = prov.ensure_harness_role(tools["gatewayArn"], memory["arn"])
        prompts = {"baseline": (self.release_dir / "pack" / "prompts" / "baseline.md").read_text(encoding="utf-8"),
                   "candidate": (self.release_dir / "pack" / "prompts" / "optimization-candidate.md").read_text(encoding="utf-8")}
        harness = prov.ensure_harness(prov.harness_config(prompt=prompts["baseline"], role_arn=role, tools=tools, memory=memory,
                                                          skills=skills, model=self.model))
        provisioned = round(time.monotonic() - mark, 1)

        rounds: list[dict[str, Any]] = []
        conversation: list[Asked] = []
        for number in range(1, self.repeat + 1):
            last = number == self.repeat
            if self.repeat > 1:
                self.log(f"round {number} of {self.repeat}")
            rounds.append(self.ask_round(harness, prompts, conversation, last=last))
            conversation = rounds[-1]["conversation"]
        final = rounds[-1]
        self.on_stage("rehearse")
        history = [(f"direct round {i}", r["state"]) for i, r in enumerate(rounds[:-1], 1)]
        doc = rehearsal.build_rehearsal(final["state"], self.scenario, history=history, documents=self.documents, guides=self.guides,
                                        release_verified=False)
        doc["generatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc["direct"] = {"names": self.names.__dict__, "harness": harness, "knowledgeBaseId": kb_id, "skippedSkills": list(prov.skipped_skills),
                         "timings": {"provision": provisioned, **final["timings"]},
                         "sweep": [sweep_summary(entry, self.model, final["state"]["steps"]) for entry in final["sweep"]],
                         "seconds": round(time.monotonic() - started, 1), "invokeErrors": final["invokeErrors"]}
        if self.repeat > 1:
            doc["direct"]["rounds"] = [{"round": i, "verdict": r["verdict"], "timings": r["timings"], "invokeErrors": len(r["invokeErrors"]),
                                        "phenomena": r["phenomena"]} for i, r in enumerate(rounds, 1)]
            robust(doc, rounds)
        if final["panel"]:
            doc["direct"]["panel"] = final["panel"]
        self.out.mkdir(parents=True, exist_ok=True)
        for i, r in enumerate(rounds[:-1], 1):
            (self.out / f"state-round-{i}.json").write_text(json.dumps(r["state"], ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        (self.out / "state.json").write_text(json.dumps(final["state"], ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        (self.out / "direct-rehearsal.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        (self.out / "direct-rehearsal.md").write_text(render_markdown(doc), encoding="utf-8")
        return doc

    def ask_round(self, harness: Mapping[str, Any], prompts: Mapping[str, str], conversation: list[Asked], *, last: bool) -> dict[str, Any]:
        """One round: the first conversation (first round only), every practice question with the baseline and the
        candidate, the comparison models and the panel (last round only), settled traces and the judges.
        The baseline / optimize records carry ``direct-<epoch>-…`` evidence ids so rounds are distinct runs."""
        timings: dict[str, float] = {}
        steps: dict[str, Any] = {}
        epoch = int(time.time())
        since_ms = epoch * 1000 - 60_000
        first = (self.pack.get("teaching") or {}).get("firstConversation") or {}
        mark = time.monotonic()
        self.on_stage("ask")
        if first.get("query") and not conversation:
            conversation = self.ask(harness["arn"], [Asked(index=0, case_id="first-conversation", query=str(first["query"]),
                                                           session_id=new_session_id(epoch), probe=False, started_ms=0,
                                                           actor=f"{first.get('actorId') or 'student'}-conversation-{epoch}")], prompts["baseline"])
        self.log(f"ask: {len(self.practice)} practice questions with the baseline, then with the candidate")
        baseline_rows = self.ask(harness["arn"], self.phase_rows("baseline", epoch), prompts["baseline"])
        optimized_rows = self.ask(harness["arn"], self.phase_rows("optimized", epoch), prompts["candidate"])
        # 12, for every comparison model: the candidate prompt on another model, through the per-call override.
        sweep_rows = {}
        for model in self.compare_models if last else ():
            self.log(f"ask: the practice questions with the candidate on {model}")
            sweep_rows[model] = self.ask(harness["arn"], self.phase_rows(f"models{len(sweep_rows) + 1}", epoch), prompts["candidate"], model)
        timings["ask"] = round(time.monotonic() - mark, 1)
        runs = {"baseline": self.write_run("baseline", epoch, baseline_rows), "optimized": self.write_run("optimized", epoch, optimized_rows)}
        for i, (model, rows) in enumerate(sweep_rows.items(), 1):
            runs[model] = self.write_run(f"models{i}", epoch, rows)

        mark = time.monotonic()
        self.on_stage("settle")
        runtime_group = f"/aws/bedrock-agentcore/runtimes/{harness['runtimeId']}-DEFAULT"
        store = TraceStore(aws_client(self.session, "logs", self.region), runtime_group, since_ms)
        everything = {r.session_id: r.text for r in baseline_rows + optimized_rows + [r for rows in sweep_rows.values() for r in rows]}
        spans = store.wait_complete(everything, timeout=self.settle_timeout, progress=self.log)
        timings["settle"] = round(time.monotonic() - mark, 1)

        mark = time.monotonic()
        self.on_stage("judge")
        baseline_scores = self.judge_phase("baseline", baseline_rows, spans, runs["baseline"])
        optimized_scores = self.judge_phase("optimized", optimized_rows, spans, runs["optimized"])
        baseline_doc, baseline_l1 = self.l1_compact("baseline", runs["baseline"], since_ms, None)
        _optimized_doc, optimized_l1 = self.l1_compact("optimized", runs["optimized"], since_ms, baseline_doc)
        stability_case = (self.pack.get("teaching") or {}).get("stabilityCaseId")
        target = next((r for r in optimized_rows if r.case_id == stability_case), None)
        values: list[float] = []
        if target is not None:
            job = {"sessionSpans": spans.get(target.session_id) or [],
                   "targetTraceId": retrieval_trace(spans.get(target.session_id) or [], str(self.pack.get("retrievalToolName") or ""))}
            for result in judges.judge("thelma", [job] * STABILITY_RUNS, evaluators_dir=self.release_dir / "evaluators", model=self.judge_model,
                                       profile=self.profile, region=self.region):
                if isinstance(result.get("value"), (int, float)):
                    values.append(float(result["value"]))
        sweep = []
        for model, rows in sweep_rows.items():
            scores = self.judge_phase(f"models ({model})", rows, spans, runs[model], goal=False)
            sweep.append({"model": model, "scores": scores, "costLatency": self.cost_latency(rows, model),
                          "invokeErrors": [{"caseId": r.case_id, "error": r.error} for r in rows if r.error]})
        panel_doc = None
        if self.panel and last:
            try:
                panel_doc = self.panel_phase(store, baseline_rows, optimized_rows)
            except Exception as exc:  # noqa: BLE001 - the panel never decides the verdict, nor loses the run
                self.log(f"panel failed: {type(exc).__name__}: {exc}")
                panel_doc = {"evaluators": list(self.panel), "references": self.panel_references, "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                             "matrix": [], "recommendation": [], "unseen": [], "bands": {}, "rows": []}
        timings["judge"] = round(time.monotonic() - mark, 1)
        finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def record(label: str, outputs: Mapping[str, Any], ok: bool = True, command: str | None = None) -> dict[str, Any]:
            return {"status": "passed" if ok else "failed", "label": label, "script": "direct", "commandId": command, "ssmStatus": None,
                    "documentVersion": None, "durationSeconds": None, "summary": "", "error": "", "outputs": dict(outputs),
                    "finishedAt": finished}

        if conversation:
            c = conversation[0]
            steps["conversation"] = record("Conversation smoke", {"conversation": {
                "schema": "workshop-customizer/conversation/1", "response": c.text, "responseChars": len(c.text),
                "sessionId": c.session_id, "truncated": c.stop_reason == "max_tokens"}}, ok=not c.error)
            steps["conversation"]["error"] = c.error or ""
        steps["baseline"] = record("Run baseline evaluation", {"scores": baseline_scores, "l1": baseline_l1}, command=f"direct-{epoch}-baseline")
        steps["optimize"] = record("Optimize prompt and re-evaluate", {"scores": optimized_scores, "l1": optimized_l1},
                                   command=f"direct-{epoch}-optimize")
        baseline_cost = self.cost_latency(baseline_rows, self.model)
        if baseline_cost:
            steps["cost-latency"] = record("Cost and latency", {"costLatency": {k: v for k, v in baseline_cost.items() if k != "priced"}},
                                           ok=baseline_cost["priced"])
        if sweep and sweep[0]["costLatency"] and not sweep[0]["invokeErrors"]:  # 12's evidence only when the comparison ran clean
            top = sweep[0]
            steps["models"] = record("Compare models", {
                "models": {"baseline": self.model, "comparison": top["model"]}, "scores": top["scores"],
                "costLatency": {k: v for k, v in (top["costLatency"] or {}).items() if k != "priced"}})
        if len(values) >= 2:
            steps["judge-stability"] = record("Judge stability", {"judgeStability": stability_verdict(values)})
        state = {"projectId": self.project_dir.name, "releaseVersion": str(self.manifest["version"]),
                 "templateCommit": str(self.manifest.get("templateCommit") or self.pack.get("templateCommit") or ""),
                 "mode": "direct", "status": "passed" if all(s["status"] == "passed" for s in steps.values()) else "failed",
                 "stepOrder": list(steps), "steps": steps, "updatedAt": finished}
        alone = rehearsal.build_rehearsal(state, self.scenario, documents=self.documents, guides=self.guides, release_verified=False)
        return {"state": state, "timings": timings, "sweep": sweep, "panel": panel_doc, "conversation": conversation,
                "phenomena": {str(p["id"]): p["verdict"] for p in alone["phenomena"]}, "verdict": alone["verdict"],
                "hints": [h for h in alone["remediation"] if h.get("severity") == "blocking" and h.get("code") != "RELEASE_NOT_VERIFIED"],
                "invokeErrors": [{"phase": p, "caseId": r.case_id, "error": r.error}
                                 for p, rs in (("conversation", conversation), ("baseline", baseline_rows), ("optimized", optimized_rows))
                                 for r in rs if r.error]}


def robust(doc: dict[str, Any], rounds: Sequence[Mapping[str, Any]]) -> None:
    """``direct.robust``: every round's own verdict was ready. The rehearsal follows the last round (its rule for
    any history), so a release ready in the last round can still have failed an earlier one: then the phenomena
    whose status changed are ``direct.flaky`` and the failing rounds' blocking hints join the remediation
    (``round`` names it; ids ``round<k>-…``), which a repair reads like any other."""
    doc["direct"]["readyRounds"] = sum(r["verdict"] == "ready" for r in rounds)
    doc["direct"]["robust"] = doc["direct"]["readyRounds"] == len(rounds)
    statuses: dict[str, set[str]] = {}
    for r in rounds:
        for pid, status in r["phenomena"].items():
            statuses.setdefault(pid, set()).add(str(status))
    doc["direct"]["flaky"] = sorted(pid for pid, seen in statuses.items() if len(seen) > 1)
    if doc["verdict"] != "ready" or doc["direct"]["robust"]:
        return
    seen_hints = {(h.get("code"), h.get("caseId")) for h in doc.get("remediation") or []}
    for number, r in enumerate(rounds, 1):
        if r["verdict"] == "ready":
            continue
        for hint in r["hints"]:
            if (hint.get("code"), hint.get("caseId")) not in seen_hints:
                seen_hints.add((hint.get("code"), hint.get("caseId")))
                doc["remediation"].append({**hint, "id": f"round{number}-{hint.get('id')}", "round": number})


def sweep_summary(entry: Mapping[str, Any], baseline_model: str, steps: Mapping[str, Any]) -> dict[str, Any]:
    """One comparison model next to the candidate on the baseline model: mean GR and SQC of the probes, cost, latency."""
    def means(rows):
        values = [r for r in rows if r.get("evaluator") == "thelma_rag_quality" and isinstance(r.get("value"), (int, float))]
        sqc = [r["metrics"]["SQC"] for r in values if isinstance((r.get("metrics") or {}).get("SQC"), (int, float))]
        return (round(statistics.mean(r["value"] for r in values), 3) if values else None,
                round(statistics.mean(sqc), 3) if sqc else None, len(values))
    gr, sqc, n = means(entry["scores"])
    base_gr, base_sqc, base_n = means((steps.get("optimize") or {}).get("outputs", {}).get("scores") or [])
    cost = entry.get("costLatency") or {}
    return {"model": entry["model"], "meanGR": gr, "meanSQC": sqc, "scored": n, "candidateOn": baseline_model, "candidateMeanGR": base_gr,
            "candidateMeanSQC": base_sqc, "averageLatencySeconds": cost.get("averageLatencySeconds"),
            "averageCostUsd": cost.get("averageCostUsd"), "priced": cost.get("priced"), "invokeErrors": len(entry.get("invokeErrors") or [])}


def render_markdown(doc: Mapping[str, Any]) -> str:
    direct = doc.get("direct") or {}
    timings = ", ".join(f"{k} {v} s" for k, v in (direct.get("timings") or {}).items())
    lines = [f"# Direct rehearsal — {doc.get('releaseVersion')}", "",
             f"Verdict: **{doc['verdict']}** ({doc['reasonCode']}); {direct.get('seconds')} s ({timings}). Harness "
             f"`{(direct.get('harness') or {}).get('id')}` on AgentCore, judged by the Workshop's own evaluators. Not a class "
             "verdict: readyForClass needs the Guided Run.", "",
             "| Phenomenon | Kind | Verdict | Code |", "|---|---|---|---|"]
    lines += [f"| {p['id']} | {p['kind']} | {p['verdict']} | {p.get('reasonCode')} |" for p in doc.get("phenomena") or []]
    if direct.get("rounds"):
        rounds = direct["rounds"]
        said = ("every round ready" if direct.get("robust") else
                f"ready in {direct.get('readyRounds', '?')} of {len(rounds)} rounds; {', '.join(direct.get('flaky') or []) or 'a phenomenon'} changed")
        lines += ["", f"Rounds on the same release ({len(rounds)}; the verdict follows the last; {said}):", "",
                  "| Phenomenon | " + " | ".join(f"round {r['round']}" for r in rounds) + " | reproduced |", "|---|" + "---|" * (len(rounds) + 1)]
        for p in doc.get("phenomena") or []:
            seen = [r["phenomena"].get(p["id"]) for r in rounds]
            lines.append(f"| {p['id']} | " + " | ".join(str(v or "—") for v in seen) + f" | {sum(v == 'reproduced' for v in seen)}/{len(seen)} |")
    blocking = [h for h in doc.get("remediation") or [] if h["severity"] == "blocking" and h["code"] != "RELEASE_NOT_VERIFIED"]
    if blocking:
        lines += ["", "Blocking remediation:", ""] + [f"- {h['code']} ({h.get('caseId') or '—'}): {h.get('actionEn') or h['action']}" for h in blocking]
    if direct.get("sweep"):
        lines += ["", "Model comparison (the candidate prompt, the same questions):", "",
                  "| Model | mean GR | mean SQC | latency s | cost USD | invoke errors |", "|---|---|---|---|---|---|"]
        first = direct["sweep"][0]
        lines.append(f"| {first['candidateOn']} (candidate) | {first['candidateMeanGR']} | {first['candidateMeanSQC']} | — | — | — |")
        lines += [f"| {s['model']} | {s['meanGR']} | {s['meanSQC']} | {s['averageLatencySeconds']} | "
                  f"{s['averageCostUsd'] if s['priced'] else 'unpriced'} | {s['invokeErrors']} |" for s in direct["sweep"]]
    if direct.get("skippedSkills"):
        lines += ["", f"Skills left out (no SKILL.md frontmatter; the Workshop's runtime skipped them too): {', '.join(direct['skippedSkills'])}."]
    if direct.get("invokeErrors"):
        lines += ["", "Invoke errors:", ""] + [f"- {e['phase']} `{e['caseId']}`: {e['error']}" for e in direct["invokeErrors"]]
    if direct.get("panel"):
        lines += panel_markdown(direct["panel"], doc.get("phenomena") or [])
    return "\n".join(lines) + "\n"
