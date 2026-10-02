"""A minimal AgentCore Runtime HTTP agent, standard library only (the console's BYOC sample and live probe).

The Runtime's HTTP contract: listen on 0.0.0.0:8080; ``GET /ping`` answers ``{"status": "Healthy",
"time_of_last_update": <unix seconds>}``; ``POST /invocations`` takes the JSON payload the caller sent to
InvokeAgentRuntime and answers JSON (or SSE). The session id arrives as the
``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`` header. No model is called, so an invocation costs nothing beyond
the Runtime's own seconds; the answer says which machine served it (``aarch64`` on AgentCore).

Deploy it as a zip of this directory (direct code deploy, entry point ``main.py``) or as a Docker build context
(the ``Dockerfile`` next to it).
"""
from __future__ import annotations

import json
import os
import platform
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STARTED = int(time.time())
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


def answer(payload: dict, session_id: str | None) -> dict:
    prompt = str(payload.get("prompt") or payload.get("message") or "").strip()
    return {"result": f"adlc byoc probe: received {prompt!r}", "sessionId": session_id, "machine": platform.machine(),
            "python": platform.python_version(), "pid": os.getpid()}


class Handler(BaseHTTPRequestHandler):
    server_version = "adlc-byoc-probe"

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        if self.path.split("?", 1)[0] == "/ping":
            self._send(200, {"status": "Healthy", "time_of_last_update": STARTED})
        else:
            self._send(404, {"error": f"no route {self.path}"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/invocations":
            self._send(404, {"error": f"no route {self.path}"})
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            self._send(400, {"error": "the payload must be JSON"})
            return
        self._send(200, answer(payload if isinstance(payload, dict) else {"prompt": payload}, self.headers.get(SESSION_HEADER)))

    def log_message(self, fmt: str, *args) -> None:  # one line per request on stdout (the Runtime's log group)
        sys.stdout.write("%s %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()


def serve(port: int | None = None, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, int(port if port is not None else os.environ.get("PORT", "8080"))), Handler)


if __name__ == "__main__":
    server = serve()
    print(f"adlc byoc probe listening on {server.server_address[0]}:{server.server_address[1]}", flush=True)
    server.serve_forever()
