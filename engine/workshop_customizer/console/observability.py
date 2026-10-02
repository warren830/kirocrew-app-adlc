"""Observability of an agent: its CloudWatch metrics, its recent sessions and one session's trace.

* :func:`metrics` — ``AWS/Bedrock-AgentCore`` per Harness (``HarnessId``) or Runtime (``Name``): invocations,
  latency, errors, throttles and sessions over a window, plus the online evaluation scores this agent's service has
  in ``Bedrock-AgentCore/Evaluations``. The metric's dimension set with the fewest dimensions is the aggregate.
* :func:`sessions` — a Logs Insights query over the runtime's log group: the sessions of the window, newest first,
  each with the scores every online evaluation of this runtime gave it (:func:`session_scores`), so a failing session
  is found from the list rather than from a dashboard.
* :func:`session_trace` — the session's spans (aws/spans) and log records (the runtime log group), as a timeline:
  every span with its duration, model, tokens and tool, and the conversation read from the gen_ai events.
* :func:`score_session` — AgentCore evaluators on one session now (``Evaluate`` on its records, as an online
  evaluation scores it), nothing stored. Optional reference inputs (assertions, the tools it should call) make the
  ground-truth evaluators usable here too; Launchpad never sends them and refuses those evaluators.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ..direct import online
from ..direct.aws import client
from ..direct.traces import TraceStore

NAMESPACE = "AWS/Bedrock-AgentCore"
EVALUATIONS = "Bedrock-AgentCore/Evaluations"
#: (metric, statistic) shown for an agent.
SERIES = (("Invocations", "Sum"), ("Sessions", "Sum"), ("Latency", "Average"), ("Errors", "Sum"), ("SystemErrors", "Sum"),
          ("UserErrors", "Sum"), ("Throttles", "Sum"))
MAX_HOURS = 24 * 14


def _window(hours: float) -> tuple[datetime, datetime, int]:
    hours = max(1.0, min(float(hours), MAX_HOURS))
    end = datetime.now(timezone.utc)
    period = 300 if hours <= 24 else 3600
    return end - timedelta(hours=hours), end, period


def _dimension(agent: Mapping[str, Any]) -> dict[str, str]:
    return {"Name": "HarnessId", "Value": str(agent["id"])} if agent["kind"] == "harness" else {"Name": "Name", "Value": str(agent["name"])}


def metrics(session: Any, region: str, agent: Mapping[str, Any], hours: float = 24) -> dict[str, Any]:
    cw = client(session, "cloudwatch", region)
    start, end, period = _window(hours)
    queries, labels = [], []
    for name, stat in SERIES:
        found = []
        for page in cw.get_paginator("list_metrics").paginate(Namespace=NAMESPACE, MetricName=name, Dimensions=[_dimension(agent)]):
            found += page.get("Metrics") or []
        if not found:
            continue
        aggregate = min(found, key=lambda m: len(m.get("Dimensions") or []))
        queries.append({"Id": f"m{len(queries)}", "MetricStat": {"Metric": aggregate, "Period": period, "Stat": stat}, "ReturnData": True})
        labels.append((name, stat))
    service = f"{agent.get('runtimeName') or ''}"
    evaluations = []
    if service:
        for page in cw.get_paginator("list_metrics").paginate(Namespace=EVALUATIONS, Dimensions=[{"Name": "service.name", "Value": service}]):
            for m in page.get("Metrics") or []:
                if not any(d["Name"] == "label" for d in m.get("Dimensions") or []):
                    evaluations.append(m)
    for m in evaluations[:10]:
        queries.append({"Id": f"m{len(queries)}", "MetricStat": {"Metric": m, "Period": period, "Stat": "Average"}, "ReturnData": True})
        labels.append((f"eval:{m['MetricName']}", "Average"))
    series: dict[str, Any] = {}
    if queries:
        got = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end, ScanBy="TimestampAscending")
        for result in got.get("MetricDataResults") or []:
            name, stat = labels[int(result["Id"][1:])]
            points = [{"t": t.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(t, "strftime") else str(t), "v": round(float(v), 3)}
                      for t, v in zip(result.get("Timestamps") or [], result.get("Values") or [])]
            values = [p["v"] for p in points]
            series[name] = {"stat": stat, "points": points,
                            "total": round(sum(values), 3) if stat == "Sum" else (round(sum(values) / len(values), 3) if values else None)}
    return {"agent": agent["name"], "hours": hours, "period": period, "series": series}


def _insights(logs: Any, group: str, query: str, start: datetime, end: datetime, *, timeout: float = 60) -> list[dict[str, str]]:
    qid = logs.start_query(logGroupName=group, startTime=int(start.timestamp()), endTime=int(end.timestamp()), queryString=query, limit=100)["queryId"]
    deadline = time.monotonic() + timeout
    while True:
        out = logs.get_query_results(queryId=qid)
        if out.get("status") in ("Complete", "Failed", "Cancelled", "Timeout") or time.monotonic() > deadline:
            return [{f["field"]: f["value"] for f in row} for row in out.get("results") or []]
        time.sleep(1)


def runtime_group(agent: Mapping[str, Any]) -> str:
    return f"/aws/bedrock-agentcore/runtimes/{agent['runtimeId']}-DEFAULT"


def session_scores(session: Any, region: str, agent: Mapping[str, Any], hours: float = 24) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """``{session id: {evaluator: {value, label, failed}}}`` from every online evaluation reading this agent's runtime
    log group (only read), and those evaluations' names."""
    group = runtime_group(agent)
    ctl, logs = client(session, "bedrock-agentcore-control", region), client(session, "logs", region)
    configs, token = [], None
    while True:
        page = ctl.list_online_evaluation_configs(**({"nextToken": token} if token else {}))
        configs += page.get("onlineEvaluationConfigs") or []
        token = page.get("nextToken")
        if not token:
            break
    since = int((time.time() - float(hours) * 3600) * 1000)
    scores: dict[str, dict[str, Any]] = {}
    names = []
    for summary in configs:
        cid = summary.get("onlineEvaluationConfigId")
        got = ctl.get_online_evaluation_config(onlineEvaluationConfigId=cid)
        if group not in (((got.get("dataSourceConfig") or {}).get("cloudWatchLogs") or {}).get("logGroupNames") or []):
            continue
        names.append(str(summary.get("onlineEvaluationConfigName") or cid))
        try:
            found = online._records(logs, f"/aws/bedrock-agentcore/evaluations/results/{cid}", since)
        except Exception as exc:  # noqa: BLE001 - no result yet: the group comes with the first one
            if "ResourceNotFound" not in str(exc):
                raise
            continue
        summarized = online.summarize(found)
        failing = {(f["sessionId"], f["evaluator"]) for f in summarized["failing"]}
        for entry in summarized["bySession"]:
            for evaluator, score in entry["scores"].items():
                scores.setdefault(entry["sessionId"], {})[evaluator] = {**score, "failed": (entry["sessionId"], evaluator) in failing}
    return scores, names


def sessions(session: Any, region: str, agent: Mapping[str, Any], hours: float = 24) -> dict[str, Any]:
    if not agent.get("runtimeId"):
        return {"sessions": []}
    start, end, _ = _window(hours)
    query = ("fields @timestamp, attributes.session.id as sid | filter ispresent(sid) "
             "| stats count(*) as records, min(@timestamp) as first, max(@timestamp) as last by sid | sort last desc | limit 50")
    try:
        rows = _insights(client(session, "logs", region), runtime_group(agent), query, start, end)
    except Exception as exc:  # noqa: BLE001 - a runtime that never logged has no group yet
        if "ResourceNotFound" in str(exc):
            return {"sessions": []}
        raise
    try:
        scores, evaluations = session_scores(session, region, agent, hours)
    except Exception as exc:  # noqa: BLE001 - the list still helps without the scores
        scores, evaluations = {}, [f"(scores unavailable: {type(exc).__name__})"]
    return {"sessions": [{"sessionId": r.get("sid"), "records": int(r.get("records") or 0), "first": r.get("first"), "last": r.get("last"),
                          "scores": scores.get(str(r.get("sid")), {})} for r in rows if r.get("sid")], "evaluations": evaluations}


def _text(value: Any) -> str:
    """The text of a gen_ai message content (a string, JSON blocks, or {"content"/"message": …})."""
    if isinstance(value, dict):
        value = value.get("content") or value.get("message") or value.get("text") or ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, list):
        return " ".join(str(b.get("text") or "") for b in value if isinstance(b, dict) and b.get("text")).strip()
    return str(value or "")


def session_trace(session: Any, region: str, agent: Mapping[str, Any], session_id: str, hours: float = 24) -> dict[str, Any]:
    start, _end, _ = _window(hours)
    store = TraceStore(client(session, "logs", region), runtime_group(agent), int(start.timestamp() * 1000))
    spans = store.session_spans(session_id)
    timeline, conversation = [], []
    for span in spans:
        attrs = span.get("attributes") or {}
        begin, finish = int(span.get("startTimeUnixNano") or 0), int(span.get("endTimeUnixNano") or 0)
        timeline.append({"traceId": span.get("traceId"), "spanId": span.get("spanId"), "parent": span.get("parentSpanId"),
                         "name": span.get("name"), "startMs": begin // 1_000_000, "durationMs": max(0, (finish - begin) // 1_000_000),
                         "status": (span.get("status") or {}).get("code"), "model": attrs.get("gen_ai.request.model"),
                         "inputTokens": attrs.get("gen_ai.usage.input_tokens"), "outputTokens": attrs.get("gen_ai.usage.output_tokens"),
                         "tool": attrs.get("gen_ai.tool.name")})
        for event in span.get("span_events") or []:
            body = event.get("body") or {}
            for direction in ("input", "output"):
                for message in ((body.get(direction) or {}).get("messages") or []) if isinstance(body, dict) else []:
                    text = _text(message.get("content")).strip()
                    role = message.get("role")
                    # A tool's result travels as a message too (a JSON payload): the conversation is the people's turns.
                    if not text or role not in ("user", "assistant") or text.startswith(("{", "[")):
                        continue
                    if not any(c["role"] == role and c["text"][:200] == text[:200] for c in conversation):
                        conversation.append({"role": role, "text": text[:4000]})
    first = min((t["startMs"] for t in timeline), default=0)
    for t in timeline:
        t["offsetMs"] = t["startMs"] - first
    tokens = {k: sum(int(t[k] or 0) for t in timeline if str(t[k] or "").isdigit()) for k in ("inputTokens", "outputTokens")}
    return {"sessionId": session_id, "spans": sorted(timeline, key=lambda t: t["startMs"]), "conversation": conversation, **tokens,
            "durationMs": max((t["offsetMs"] + t["durationMs"] for t in timeline), default=0)}


#: The evaluators offered first on a session (the panel's): quality, the goal, declining.
SESSION_EVALUATORS = ("Builtin.Faithfulness", "Builtin.Correctness", "Builtin.Helpfulness", "Builtin.Refusal", "Builtin.GoalSuccessRate")
MAX_SESSION_EVALUATORS = 5


def score_session(session: Any, region: str, agent: Mapping[str, Any], session_id: str, body: Mapping[str, Any], hours: float = 24) -> dict[str, Any]:
    """Up to five evaluators on one session, with the assertions and expected tools given as its reference inputs."""
    from ..direct.panel import Panel

    evaluators = [str(e).strip() for e in body.get("evaluators") or SESSION_EVALUATORS if str(e).strip()]
    if not 1 <= len(evaluators) <= MAX_SESSION_EVALUATORS:
        raise ValueError(f"evaluators: 1 to {MAX_SESSION_EVALUATORS}")
    assertions = [str(a).strip() for a in body.get("assertions") or [] if str(a).strip()][:20]
    tools = [str(t).strip() for t in body.get("expectedTools") or [] if str(t).strip()][:20]
    start, _end, _ = _window(hours)
    store = TraceStore(client(session, "logs", region), runtime_group(agent), int(start.timestamp() * 1000))
    store.session_spans(session_id)
    records = store.evaluation_records(session_id)
    if not records:
        raise ValueError("no records for this session in the window: spans arrive a few minutes after a turn")
    reference: dict[str, Any] = {"context": {"spanContext": {"sessionId": session_id}}}
    if assertions:
        reference["assertions"] = [{"text": a} for a in assertions]
    if tools:
        reference["expectedTrajectory"] = {"toolNames": tools}
    refs = [reference] if len(reference) > 1 else []
    panel = Panel(client(session, "bedrock-agentcore", region), client(session, "bedrock-agentcore-control", region), evaluators,
                  workers=len(evaluators), log=lambda _m: None)
    rows = panel.score([{"evaluator": e, "caseId": "-", "phase": "-", "sessionId": session_id, "records": records, "references": refs} for e in evaluators])
    return {"sessionId": session_id, "records": len(records), "references": refs,
            "scores": [{k: r.get(k) for k in ("evaluator", "value", "label", "explanation", "error", "ignored")} for r in rows]}


def register(router: Any) -> None:
    from . import agents

    def agent_of(r: Any) -> tuple[Any, str, dict[str, Any]]:
        session = r.session()
        region = r.console.workspaces.get(r.workspace())["region"]
        agent = agents.get_agent(session, region, r.params["kind"], r.params["ident"])
        if agent.get("runtimeArn"):
            agent["runtimeName"] = str(agent["runtimeArn"]).rsplit("/", 1)[-1].rsplit("-", 1)[0] + ".DEFAULT"
        return session, region, agent

    def hours(r: Any) -> float:
        try:
            return float(r.query.get("hours") or 24)
        except ValueError:
            return 24.0

    def get_metrics(r: Any):
        session, region, agent = agent_of(r)
        return 200, metrics(session, region, agent, hours(r))

    def get_sessions(r: Any):
        session, region, agent = agent_of(r)
        return 200, sessions(session, region, agent, hours(r))

    def get_trace(r: Any):
        session, region, agent = agent_of(r)
        return 200, session_trace(session, region, agent, r.params["sid"], hours(r))

    router.add("GET", "/workspaces/{wid}/agents/{kind}/{ident}/metrics", get_metrics)
    router.add("GET", "/workspaces/{wid}/agents/{kind}/{ident}/sessions", get_sessions)
    def post_score(r: Any):
        session, region, agent = agent_of(r)
        return 200, score_session(session, region, agent, r.params["sid"], r.body, hours(r))

    router.add("GET", "/workspaces/{wid}/agents/{kind}/{ident}/sessions/{sid}", get_trace)
    router.add("POST", "/workspaces/{wid}/agents/{kind}/{ident}/sessions/{sid}/evaluate", post_score)
