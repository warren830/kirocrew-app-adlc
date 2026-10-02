"""A session's spans as the AgentCore evaluators receive them, from CloudWatch.

Spans are in ``aws/spans`` (Transaction Search); the gen_ai message events of each span are OTel log records in
the Harness runtime's log group (``otel-rt-logs`` streams). The evaluator Lambdas get both joined: each span
carries ``span_events`` = ``[{"body": …}]``, which THELMA's ``span_adapter`` and Mind the Goal's
``session_adapter`` read. Reassembled this way, a live baseline answer scored the same as when the Workshop's
own evaluator re-scored it (2026-10-01: local 0.33–0.38, live re-score 0.25–0.35).

A trace is complete for the evaluator only some time after aws/spans lists it (09 once scored a trace while
its final answer was missing: GR 1.0 instead of 0.31), so :meth:`TraceStore.wait_complete` waits until the
final answer of every session is among its events, or until ``timeout``.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

SPAN_LOG_GROUP = "aws/spans"


def _attrs(span: Mapping[str, Any]) -> Mapping[str, Any]:
    attrs = span.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _body(record: Mapping[str, Any]) -> Any:
    body = record.get("body")
    if isinstance(body, str):
        try:
            return json.loads(body)
        except ValueError:
            return {"raw": body}
    return body


def answer_marker(text: str, length: int = 16) -> str:
    """A contiguous fragment of an answer's last line that JSON leaves as it is: the longest run of that line
    without a quote or a backslash (only those two are escaped), at most ``length`` characters from its end.
    Live 2026-10-01 a marker with the Markdown stripped ("…教务处。1" for "…教务处。**1**") matched nothing."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    tail = lines[-1] if lines else ""
    runs = [run.strip() for run in tail.replace("\\", '"').split('"') if run.strip()]
    best = max(reversed(runs), key=len, default="")  # the longest run, the last one on a tie
    return best[-length:].strip()


class TraceStore:
    """Spans (aws/spans) and their events (the runtime log group), fetched per session and per trace."""

    def __init__(self, logs: Any, runtime_log_group: str, since_ms: int, *, pause: float = 0.2, sleep: Callable[[float], None] = time.sleep):
        self.logs, self.runtime_log_group, self.since_ms, self.pause, self.sleep = logs, runtime_log_group, int(since_ms), pause, sleep
        self._records: dict[str, list[dict[str, Any]]] = {}

    def _events(self, group: str, needle: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        paginator = self.logs.get_paginator("filter_log_events")
        for page in paginator.paginate(logGroupName=group, startTime=self.since_ms, filterPattern=f'"{needle}"'):
            for event in page.get("events", []):
                try:
                    record = json.loads(event.get("message") or "")
                except ValueError:
                    continue
                if isinstance(record, dict):
                    out.append(record)
            if self.pause:
                self.sleep(self.pause)
        return out

    def session_spans(self, session_id: str) -> list[dict[str, Any]]:
        """Every span of the session's traces, each with its ``span_events`` (``[{"body": …}]``)."""
        first = [s for s in self._events(SPAN_LOG_GROUP, session_id)
                 if str(_attrs(s).get("session.id") or s.get("session_id") or "") == session_id]
        traces = list(dict.fromkeys(str(s.get("traceId") or s.get("trace_id") or "") for s in first if s.get("traceId") or s.get("trace_id")))
        spans: dict[str, dict[str, Any]] = {str(s.get("spanId")): s for s in first if s.get("spanId")}
        events: dict[str, list[dict[str, Any]]] = {}
        raw: list[dict[str, Any]] = []
        for trace in traces:
            for span in self._events(SPAN_LOG_GROUP, trace):
                if str(span.get("traceId")) == trace and span.get("spanId"):
                    spans.setdefault(str(span["spanId"]), span)
            for record in self._events(self.runtime_log_group, trace):
                if str(record.get("traceId")) == trace and record.get("spanId"):
                    events.setdefault(str(record["spanId"]), []).append({"body": _body(record)})
                    raw.append(record)
        self._records[session_id] = [dict(s) for s in spans.values()] + raw
        joined = []
        for span_id, span in spans.items():
            joined.append({**span, "span_events": events.get(span_id, [])})
        return sorted(joined, key=lambda s: int(s.get("startTimeUnixNano") or 0))

    def evaluation_records(self, session_id: str) -> list[dict[str, Any]]:
        """The session's records as the agentcore CLI hands them to ``Evaluate``: span records and log records, flat
        (from the last :meth:`session_spans` of the session)."""
        return list(self._records.get(session_id) or [])

    @staticmethod
    def has_answer(spans: Sequence[Mapping[str, Any]], answer: str) -> bool:
        marker = answer_marker(answer)
        if not marker:
            return any(s.get("span_events") for s in spans)
        blob = json.dumps([e.get("body") for s in spans for e in s.get("span_events") or []], ensure_ascii=False)
        return marker in blob or marker in blob.encode("unicode_escape").decode("ascii", "ignore")

    def wait_complete(self, sessions: Mapping[str, str], *, timeout: float = 420, poll: float = 20,
                      progress: Callable[[str], None] | None = None) -> dict[str, list[dict[str, Any]]]:
        """``session id → spans`` once every session's final answer (``sessions[sid]``) is among its events, or at
        ``timeout`` (the sessions still incomplete keep what CloudWatch had)."""
        deadline = time.monotonic() + timeout
        done: dict[str, list[dict[str, Any]]] = {}
        while True:
            for sid, answer in sessions.items():
                if sid in done:
                    continue
                spans = self.session_spans(sid)
                if spans and self.has_answer(spans, answer):
                    done[sid] = spans
            left = [sid for sid in sessions if sid not in done]
            if progress:
                progress(f"traces complete for the evaluator: {len(done)}/{len(sessions)}")
            if not left or time.monotonic() > deadline:
                for sid in left:
                    done[sid] = self.session_spans(sid)
                return done
            self.sleep(poll)


def trace_ids(spans: Iterable[Mapping[str, Any]]) -> list[str]:
    return list(dict.fromkeys(str(s.get("traceId")) for s in spans if s.get("traceId")))


def retrieval_trace(spans: Sequence[Mapping[str, Any]], retrieval_tool: str) -> str | None:
    """The trace whose spans call the retrieval tool (each direct invocation is one trace)."""
    for span in spans:
        name = f"{span.get('name') or ''} {_attrs(span).get('gen_ai.tool.name') or ''}"
        if retrieval_tool and retrieval_tool in name:
            return str(span.get("traceId"))
    ids = trace_ids(spans)
    return ids[-1] if ids else None
