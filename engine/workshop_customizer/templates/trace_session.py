"""Resolve the session required by AgentCore evaluation for a Guide trace ID."""
import json
import re
import sys
import time

import boto3


def session_for_trace(client, trace_id, *, start_ms):
    if not re.fullmatch(r"[0-9a-f]{16,32}", trace_id):
        raise ValueError("expected a hexadecimal trace ID")
    sessions = set()
    for page in client.get_paginator("filter_log_events").paginate(
        logGroupName="aws/spans", startTime=start_ms, filterPattern=f'"{trace_id}"',
    ):
        for event in page.get("events", []):
            span = json.loads(event["message"])
            if (span.get("traceId") or span.get("trace_id")) != trace_id:
                continue
            session = (span.get("attributes") or {}).get("session.id") or span.get("session_id")
            if session:
                sessions.add(session)
    if len(sessions) != 1:
        raise RuntimeError(f"trace {trace_id} resolved to {len(sessions)} sessions; expected exactly one")
    return sessions.pop()


if __name__ == "__main__":
    print(session_for_trace(boto3.client("logs"), sys.argv[1], start_ms=int((time.time() - 86400) * 1000)))
