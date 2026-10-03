"""The public ``/v1`` API: the console's invoke chain for other systems, behind an ``X-Api-Key``.

    GET  /v1/agents                         the agents the key may call
    POST /v1/chat  {agent, message, sessionId?, actorId?, stream?}

A key is scoped to one workspace and optionally one agent (``console.auth``). ``agent`` is a Harness or Runtime
name in that workspace. Without ``stream`` the answer comes back whole (``text``, ``tools``, ``sessionId``,
``latencyMs``, ``usage``); with it, as server-sent events like the console's chat. Pass the returned ``sessionId``
to continue the conversation. While the agent is in a running A/B test (``console.experiments``), or a code agent in a
running canary (``console.runtime_canary``), its turns go through that Gateway, which decides each session's arm
(``experiment`` names the test or the canary).
"""
from __future__ import annotations

from typing import Any, Mapping

from . import agents, runtime_canary
from .auth import AuthError
from .web import sse


def _agent(console: Any, scope: Mapping[str, Any], name: str) -> tuple[Any, str, dict[str, Any]]:
    if scope.get("agent") and scope["agent"] != name:
        raise AuthError(f"this key may call {scope['agent']} only")
    ws = console.workspaces.get(scope["workspace"])
    console.workspaces.verify(ws["id"])
    session = console.workspaces.session(ws["id"])
    found = next((a for a in agents.list_agents(session, ws["region"]) if a["name"] == name), None)
    if not found:
        raise agents.AgentError(f"no agent {name} in workspace {ws['id']}")
    return session, ws["region"], agents.get_agent(session, ws["region"], found["kind"], found["id"])


def handle(console: Any, method: str, path: str, headers: Mapping[str, str], body: Mapping[str, Any]) -> tuple[int, Any]:
    try:
        scope = console.auth.key_scope(headers.get("X-Api-Key"))
    except AuthError as exc:
        return 401, {"error": str(exc)}
    try:
        if path == "/agents" and method == "GET":
            ws = console.workspaces.get(scope["workspace"])
            console.workspaces.verify(ws["id"])
            listed = agents.list_agents(console.workspaces.session(ws["id"]), ws["region"])
            return 200, {"agents": [{"name": a["name"], "kind": a["kind"], "status": a["status"]} for a in listed
                                    if not scope.get("agent") or a["name"] == scope["agent"]]}
        if path == "/chat" and method == "POST":
            session, region, agent = _agent(console, scope, str(body.get("agent") or ""))
            message, actor = str(body.get("message") or ""), str(body.get("actorId") or f"key-{scope['keyId']}")
            sid = agents.check_turn(message, body.get("sessionId") or None)  # a 400 before any stream starts
            events, route = runtime_canary.routed_turn(console, scope["workspace"], session, region, agent, message=message, session_id=sid, actor=actor)
            if body.get("stream"):
                return 200, sse(events)
            text, tools, usage, sid, error, seconds = [], [], {}, None, None, None
            for event in events:
                kind = event["type"]
                if kind == "session":
                    sid = event["sessionId"]
                elif kind == "text":
                    text.append(event["text"])
                elif kind == "tool":
                    tools.append(event["name"])
                elif kind == "error":
                    error = event["error"]
                elif kind == "stop":
                    seconds = event.get("seconds")
                    usage = {k: event[k] for k in ("inputTokens", "outputTokens") if k in event}
            if error:  # a turn that failed part-way is not an answer: say so, with what had arrived
                return 502, {"error": error, "sessionId": sid, "partialText": "".join(text), "tools": tools}
            return 200, {"agent": agent["name"], "text": "".join(text), "tools": tools, "sessionId": sid,
                         "latencyMs": int((seconds or 0) * 1000), "usage": usage, **({"experiment": route["id"]} if route else {})}
    except AuthError as exc:
        return 403, {"error": str(exc)}
    except (agents.AgentError, ValueError) as exc:
        return 400, {"error": str(exc)}
    return 404, {"error": f"no route /v1{path}"}
