"""The ADLC console: the platform pages and their API, served inside KiroCrew or on a local server of its own.

Inside KiroCrew, the primary way to open it, the Workshop Customizer App's page (``app/ui/dist/platform.mjs``, the
sidebar's ADLC 控制台) frames the console from the App's own process backend, which mounts it under ``/api/console``
(:class:`Mount`). The browser reaches it at ``/apps/workshop-customizer/api/console/``, through the gateway's signed
proxy. There KiroCrew's sign-in is the authentication, the 工作坊 item opens KiroCrew's own Workshop Customizer view, and
the state lives in the App's data directory (``<data>/console``).

On its own server, with no KiroCrew (development):

    .venv/bin/python3 app/console/server.py [--port 8770] [--data ~/.adlc-console] [--workshop-data <App data dir>]

* ``/`` — the console (``static/console.mjs``, React from ``static/vendor``), whose 工作坊 page mounts the workshop
  App's own UI (``app/ui/dist/index.mjs``) unchanged.
* ``/apps/workshop-customizer/api/apps/workshop-customizer/...`` — the App's process backend (``app/backend/server.py``),
  in process, so every workshop route behaves exactly as under KiroCrew.
* ``/api/apps/workshop-customizer/generations...`` — Kiro generation, which KiroCrew runs through ``ctx.spawn``; here
  the record is prepared, run by the local kiro-cli (``tools/kiro_generate.run_kiro``) on a thread and completed the
  way the headless loop does (runner ``kiro-cli``).
* ``/api/console/...`` — the platform: workspaces, agents, knowledge bases, chat, evaluation, registry, governance
  (``engine/workshop_customizer/console``), behind the console's users (none defined: open mode, localhost only).
* ``/v1/...`` — the public API (``X-Api-Key``), the same invoke chain as the console's chat.

A standalone console's state moves into the App's data directory once (the source is left as it is):

    .venv/bin/python3 app/console/server.py --import-from /tmp/adlc-console-live --into ~/.kiro/crew/apps/workshop-customizer/data
"""
from __future__ import annotations

import argparse
import codecs
import errno
import html
import importlib.util
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import parse_qs, urlsplit

#: The App: ``<checkout>/app``, or KiroCrew's installed copy of it (``~/.kiro/crew/apps/workshop-customizer``).
APP = Path(__file__).resolve().parents[1]


def _checkout() -> Path:
    """The checkout with ``engine/`` and ``tools/``: the one around ``app/``. KiroCrew's installed copy of the App has
    neither, so there it is the App's home, resolved as its backend resolves it: where KiroCrew installed it from
    (``installed.json``: a path's parent, or a registry install's clone in ``~/.kiro/crew/app-sources/<name>``), else
    ``WORKSHOP_CUSTOMIZER_HOME``, else ``data/config.json`` ``homeDir``."""
    candidates = [APP.parent]
    try:
        source = str(json.loads((APP / "installed.json").read_text(encoding="utf-8")).get("source") or "")
    except (OSError, ValueError, AttributeError):
        source = ""
    if source.startswith("registry:"):
        candidates.append(APP.parent.parent / "app-sources" / source[len("registry:"):])
    elif source:
        candidates.append(Path(source).expanduser().parent)
    if os.environ.get("WORKSHOP_CUSTOMIZER_HOME"):
        candidates.append(Path(os.environ["WORKSHOP_CUSTOMIZER_HOME"]).expanduser())
    try:
        candidates.append(Path(json.loads((APP / "data" / "config.json").read_text(encoding="utf-8"))["homeDir"]).expanduser())
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return next((c.resolve() for c in candidates if (c / "engine" / "workshop_customizer").is_dir()), APP.parent)


REPO = _checkout()
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(APP / "backend"))

import routes as generation  # noqa: E402  (the Kiro generation lifecycle, shared with KiroCrew's routes)

from workshop_customizer.console.auth import Auth, AuthError  # noqa: E402
from workshop_customizer.console.skills_lab import MAX_UPLOAD_BODY  # noqa: E402
from workshop_customizer.console.store import Store  # noqa: E402
from workshop_customizer.console.web import Stream  # noqa: E402
from workshop_customizer.console.workspaces import WorkspaceError, Workspaces  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
APP_UI = APP / "ui" / "dist" / "index.mjs"
PROCESS_PREFIX = "/apps/workshop-customizer/api/apps/workshop-customizer"
AGENT_PREFIX = "/api/apps/workshop-customizer"
CONSOLE_PREFIX = "/api/console"
MAX_BODY = 25 * 1024 * 1024
#: Routes that take a larger body, by exact path: the Skill Lab's task files (Launchpad's 25 MiB a file, base64).
BODY_LIMITS = ((re.compile(r"^/api/console/workspaces/[^/]+/skill-lab/assets$"), MAX_UPLOAD_BODY),)
COOKIE = "adlc_session"


def body_limit(path: str) -> int:
    """The largest request body ``path`` takes."""
    return next((limit for pattern, limit in BODY_LIMITS if pattern.match(path)), MAX_BODY)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.payload = status, {"error": message, **extra}


class Router:
    """``/api/console`` routes: ``add("GET", "/workspaces/{wid}/agents", fn)``; ``fn(req) -> (status, payload)`` or a
    :class:`Stream`."""

    def __init__(self):
        self._routes: list[tuple[str, re.Pattern[str], Callable[..., Any], bool]] = []

    def add(self, method: str, pattern: str, fn: Callable[..., Any], *, admin: bool = False) -> None:
        regex = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$"
        self._routes.append((method, re.compile(regex), fn, admin))

    def match(self, method: str, path: str) -> tuple[Callable[..., Any], dict[str, str], bool] | None:
        allowed = False
        for m, regex, fn, admin in self._routes:
            found = regex.match(path)
            if found:
                if m == method:
                    return fn, found.groupdict(), admin
                allowed = True
        if allowed:
            raise HttpError(405, f"{method} is not allowed on {path}")
        return None


class Request:
    def __init__(self, console: "Console", method: str, path: str, params: dict[str, str], query: dict[str, str], body: dict[str, Any],
                 caller: dict[str, Any], raw: bytes):
        self.console, self.method, self.path, self.params, self.query, self.body = console, method, path, params, query, body
        self.caller, self.raw = caller, raw

    def workspace(self) -> str:
        wid = self.params.get("wid") or ""
        Auth.may(self.caller, wid)
        return wid

    def session(self) -> Any:
        """The verified boto3 session of the route's workspace."""
        wid = self.workspace()
        self.console.workspaces.verify(wid)
        return self.console.workspaces.session(wid)


def json_body(raw: bytes) -> dict[str, Any]:
    """A request's JSON object body (``{}`` when there is none)."""
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HttpError(400, f"body must be JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HttpError(400, "body must be a JSON object")
    return payload


def refusal(console: "Console | None", exc: BaseException) -> tuple[int, dict[str, Any]]:
    """The status and payload a console request answers with when its handling raised ``exc``. Called inside the
    ``except``: an unexpected error's traceback is logged locally, never sent."""
    if isinstance(exc, HttpError):
        return exc.status, exc.payload
    backend = getattr(console, "backend", None)
    if backend is not None and isinstance(exc, backend.HttpError):  # the workshop App's own refusals keep their status and payload
        return exc.status, exc.payload
    if isinstance(exc, AuthError):
        return (403 if "no access" in str(exc) or "needs an admin" in str(exc) else 401), {"error": str(exc)}
    if isinstance(exc, WorkspaceError):
        return 409, {"error": str(exc)}
    if isinstance(exc, ValueError):  # a module's own refusal (AgentError, KnowledgeError, SkillLabError, …): the caller's to fix
        return 400, {"error": str(exc)}
    sys.stderr.write(traceback.format_exc())
    return 500, {"error": f"internal error: {type(exc).__name__}: {str(exc)[:300]}"}


def run_route(console: "Console", method: str, path: str, query: dict[str, str], body: dict[str, Any], raw: bytes,
              caller: dict[str, Any]) -> Any:
    """``/api/console<path>`` for ``caller``: ``(status, payload)`` or a :class:`Stream`."""
    found = console.router.match(method, path)
    if not found:
        raise HttpError(404, f"no console route {path}")
    fn, params, admin = found
    if admin:
        Auth.may(caller, admin=True)
    return fn(Request(console, method, path, params, query, body, caller, raw))


def page(*, base: str = "/", api: str = CONSOLE_PREFIX, host: str = "standalone") -> bytes:
    """The console's HTML with where it is: ``base`` (its document base, under which ``static/`` is), ``api`` (the
    ``/api/console`` the page calls) and ``host`` (``standalone`` or ``kirocrew``)."""
    text = (STATIC / "index.html").read_text(encoding="utf-8")
    for old, new in (('<base href="/">', f'<base href="{html.escape(base)}">'),
                     ('<meta name="adlc-api" content="/api/console">', f'<meta name="adlc-api" content="{html.escape(api)}">'),
                     ('<meta name="adlc-host" content="standalone">', f'<meta name="adlc-host" content="{html.escape(host)}">')):
        if old not in text:
            raise RuntimeError(f"static/index.html lost its {old}")
        text = text.replace(old, new, 1)
    return text.encode("utf-8")


def content_type(target: Path) -> str:
    if target.suffix in (".mjs", ".js"):
        return "text/javascript; charset=utf-8"
    if target.suffix == ".html":
        return "text/html; charset=utf-8"
    return mimetypes.guess_type(target.name)[0] or "application/octet-stream"


def static_file(sub: str) -> Path:
    """``static/<sub>``, never a file outside it."""
    target = (STATIC / sub).resolve()
    if STATIC.resolve() not in target.parents or not target.is_file():
        raise HttpError(404, "not found")
    return target


class KiroRunner:
    """The generation routes KiroCrew serves through ``ctx.spawn``, run by the local kiro-cli instead."""

    def __init__(self, data_dir: Path, *, run: Callable[..., tuple[str, str, int, float]] | None = None):
        self.data_dir = data_dir
        self._run = run

    def runner(self) -> Callable[..., tuple[str, str, int, float]]:
        if self._run is None:
            self._run = _load("kiro_generate_console", REPO / "tools" / "kiro_generate.py").run_kiro
        return self._run

    def start(self, body: dict[str, Any]) -> tuple[int, Any]:
        try:
            record, task = generation.prepare_generation(self.data_dir, body, runner="kiro-cli")
        except generation.GenerationConflict as exc:
            payload: dict[str, Any] = {"error": str(exc)}
            if exc.record is not None:
                payload["generation"] = generation._public_record(exc.record)
            return 409, payload
        except generation.GenerationTooLarge as exc:
            return 413, {"error": str(exc)}
        except generation.GenerationError as exc:
            return 400, {"error": str(exc)}
        timeout = int(record["timeoutSecs"])
        generation.mark_running(self.data_dir, record, timeout_secs=timeout)

        def work() -> None:
            try:
                stdout, stderr, code, _seconds = self.runner()(task, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - recorded on the generation, the UI shows it
                generation.fail_generation(self.data_dir, record, f"Kiro CLI could not run: {type(exc).__name__}: {exc}"[:1000])
                return
            if code != 0 and not stdout.strip():
                generation.fail_generation(self.data_dir, record, f"Kiro CLI exited {code}: {stderr.strip()[-800:]}")
                return
            generation.complete_generation(record, stdout, data_dir=self.data_dir)

        threading.Thread(target=work, name=f"kiro-{record['id']}", daemon=True).start()
        return 202, generation._public_record(record)

    def latest(self, project_id: str) -> tuple[int, Any]:
        generation._project_dir(self.data_dir, project_id)
        candidates = generation._project_records(self.data_dir, project_id)
        if not candidates:
            return 200, {"generation": None}
        record = max(candidates, key=lambda item: str(item.get("createdAt") or ""))
        return 200, {"generation": generation._public_record(generation._settle(record, data_dir=self.data_dir))}

    def poll(self, generation_id: str) -> tuple[int, Any]:
        record = generation._read_record(self.data_dir, generation_id)
        return 200, generation._public_record(generation._settle(record, data_dir=self.data_dir))

    def apply(self, generation_id: str, body: dict[str, Any]) -> tuple[int, Any]:
        record = generation._settle(generation._read_record(self.data_dir, generation_id), data_dir=self.data_dir)
        return 200, generation.apply_generation(record, self.data_dir, body)

    def revert(self, generation_id: str, body: dict[str, Any]) -> tuple[int, Any]:
        return 200, generation.revert_generation(generation_id, self.data_dir, body)

    def dispatch(self, method: str, parts: list[str], body: dict[str, Any]) -> tuple[int, Any]:
        try:
            if parts == ["generations"] and method == "POST":
                return self.start(body)
            if len(parts) == 3 and parts[:2] == ["generations", "latest"] and method == "GET":
                return self.latest(parts[2])
            if len(parts) == 2 and parts[0] == "generations" and method == "GET":
                return self.poll(parts[1])
            if len(parts) == 3 and parts[0] == "generations" and parts[2] in ("apply", "revert") and method == "POST":
                return (self.apply if parts[2] == "apply" else self.revert)(parts[1], body)
        except generation.GenerationError as exc:
            return (404 if method == "GET" else 409), {"error": str(exc)}
        return 404, {"error": "no such generation route"}


class Console:
    """The console's state and routes. ``workshop`` mounts the workshop App's backend in process (the standalone
    server's ``/apps/...`` and Kiro generation routes); inside KiroCrew the App serves those itself, so the
    :class:`Mount` leaves it out."""

    def __init__(self, data_dir: Path, *, workshop_data: Path | None = None, home: Path | None = None,
                 session_factory: Callable[..., Any] | None = None, clients_factory: Callable[[Any], Any] | None = None,
                 kiro_run: Callable[..., Any] | None = None, workshop: bool = True):
        self.data_dir = Path(data_dir).expanduser()
        self.store = Store(self.data_dir / "console")
        self.workspaces = Workspaces(self.store, session_factory=session_factory)
        self.auth = Auth(self.store)
        self.workshop_data = Path(workshop_data).expanduser() if workshop_data else self.data_dir / "workshop"
        self.workshop_data.mkdir(parents=True, exist_ok=True)
        self.backend = self.workshop = self.kiro = None
        if workshop:
            self.backend = _load("wc_app_server_console", APP / "backend" / "server.py")
            self.workshop = self.backend.Service(self.workshop_data, home=home or REPO, clients_factory=clients_factory)
            self.kiro = KiroRunner(self.workshop_data, run=kiro_run)
        from workshop_customizer.console.jobs import Jobs

        self.jobs = Jobs(self.data_dir / "console" / "jobs")
        self.router = Router()
        from workshop_customizer.console import api

        api.register(self.router)


def make_handler(console: Console) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "adlc-console"
        timeout = 120  # an idle or stalled connection is dropped instead of holding a thread and a descriptor

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
            if not str(args[0] if args else "").startswith(("GET /static", "GET /app")):
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        # -- plumbing ------------------------------------------------------------
        def _length(self, path: str) -> int:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise HttpError(400, "bad Content-Length") from exc
            if length < 0:
                raise HttpError(400, "bad Content-Length")
            if length > body_limit(path):
                raise HttpError(413, "request too large")
            return length

        def _guard(self, length: int, *, api_key: bool) -> None:
            """Requests a browser could be tricked into sending: a page of another origin (no CORS preflight is needed
            for a text/plain POST) or a rebound DNS name pointing at this console. Writes must be same-origin JSON;
            bound to localhost, only a localhost Host is served; in open mode only loopback clients are."""
            bound = str(self.server.server_address[0])
            loopback = bound in ("127.0.0.1", "::1", "localhost")
            host = (self.headers.get("Host") or "").strip().lower()
            if loopback and host.rsplit(":", 1)[0].strip("[]") not in ("127.0.0.1", "localhost", "::1"):
                raise HttpError(403, "this console answers only to localhost")
            if console.auth.open_mode() and (not loopback or self.client_address[0] not in ("127.0.0.1", "::1")):
                raise HttpError(403, "open mode is for this machine only: create an admin user first")
            if self.command in ("POST", "PUT", "DELETE") and not api_key:
                origin = (self.headers.get("Origin") or "").strip().lower()
                if origin and origin.split("://", 1)[-1] != host:
                    raise HttpError(403, "cross-origin request refused")
                if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
                    raise HttpError(403, "cross-site request refused")
                kind = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if length and kind != "application/json":
                    raise HttpError(415, "send the body as application/json")

        def _send(self, status: int, payload: Any = None, *, raw: bytes | None = None, content_type: str = "application/json",
                  headers: dict[str, str] | None = None) -> None:
            body = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type if raw is not None else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _stream(self, stream: Stream) -> None:
            self.send_response(200)
            self.send_header("Content-Type", stream.content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for chunk in stream.chunks:
                    if chunk:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _reply(self, result: Any) -> None:
            if isinstance(result, Stream):
                self._stream(result)
            else:
                status, payload = result
                self._send(status, payload)

        def _cookie(self) -> str | None:
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE:
                    return value
            return None

        # -- static ----------------------------------------------------------------
        def _static(self, path: str) -> bool:
            if path in ("/", "/index.html"):
                self._send(200, raw=page(), content_type="text/html; charset=utf-8")
                return True
            if path == "/app/index.mjs":
                target = APP_UI
            elif path.startswith("/static/"):
                target = static_file(path[len("/static/"):])
            else:
                return False
            if not target.is_file():
                raise HttpError(404, "not found")
            self._send(200, raw=target.read_bytes(), content_type=content_type(target))
            return True

        # -- dispatch ----------------------------------------------------------------
        def _dispatch(self) -> None:
            try:
                split = urlsplit(self.path)
                path, query = split.path, {k: v[-1] for k, v in parse_qs(split.query).items()}
                if self.command == "GET" and self._static(path):
                    return
                length = self._length(path)
                self._guard(length, api_key=path.startswith("/v1/"))  # the headers are checked before any body is read
                if length > MAX_BODY:  # the one larger body (task files) is read only from a signed-in caller
                    self._caller()
                raw = self.rfile.read(length) if length else b""
                if path.startswith(PROCESS_PREFIX):
                    # the workshop App acts with the host's own AWS profiles (sync, direct deploys, deletes): admins only
                    Auth.may(self._caller(), admin=True)
                    sub = path[len(PROCESS_PREFIX):] or "/"
                    status, result, blob, headers = console.backend.route(console.workshop, self.command, sub, query, json_body(raw))
                    if blob is not None:
                        self._send(status, raw=blob, content_type=headers.pop("Content-Type", "application/octet-stream"), headers=headers)
                    else:
                        self._send(status, result)
                    return
                if path.startswith(AGENT_PREFIX + "/generations"):
                    Auth.may(self._caller(), admin=True)  # Kiro generations write into the workshop data, as the App does
                    status, result = console.kiro.dispatch(self.command, [p for p in path[len(AGENT_PREFIX):].split("/") if p], json_body(raw))
                    self._send(status, result)
                    return
                if path.startswith("/v1/"):
                    from workshop_customizer.console import public

                    status, result = public.handle(console, self.command, path[len("/v1"):], self.headers, json_body(raw))
                    self._reply(result if isinstance(result, Stream) else (status, result))
                    return
                if path.startswith(CONSOLE_PREFIX + "/") or path == CONSOLE_PREFIX:
                    self._console(path[len(CONSOLE_PREFIX):] or "/", query, raw)
                    return
                raise HttpError(404, f"no route {path}")
            except Exception as exc:  # noqa: BLE001 - each kind of refusal keeps its status (refusal); never a traceback in the UI
                self._send(*refusal(console, exc))

        def _caller(self) -> dict[str, Any]:
            return console.auth.whoami(self._cookie())

        def _console(self, path: str, query: dict[str, str], raw: bytes) -> None:
            body = json_body(raw)
            if path == "/login" and self.command == "POST":
                token = console.auth.login(str(body.get("username") or ""), str(body.get("password") or ""))
                self._send(200, console.auth.whoami(token), headers={"Set-Cookie": f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"})
                return
            if path == "/logout" and self.command == "POST":
                console.auth.logout(self._cookie() or "")
                self._send(200, {"ok": True}, headers={"Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/"})
                return
            self._reply(run_route(console, self.command, path, query, body, raw, self._caller()))

        do_GET = do_POST = do_PUT = do_DELETE = _dispatch  # noqa: N815

    return Handler


def create_server(console: Console, *, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(console))
    server.daemon_threads = True
    return server


# ---------------------------------------------------------------------------------------------------------------------
# Inside KiroCrew: the console mounted in the App's process backend
# ---------------------------------------------------------------------------------------------------------------------

#: Who every request through KiroCrew's gateway acts as. KiroCrew's sign-in is the console's there: the App's backend
#: refuses a request without the gateway's signature before it reaches the :class:`Mount`, and KiroCrew is one person
#: on their own machine. The console's own users and sign-in are the standalone server's.
KIROCREW_CALLER: Mapping[str, Any] = {"username": "kirocrew", "role": "admin", "workspaces": ["*"], "open": False, "host": "kirocrew"}
#: KiroCrew's proxy cuts every request at 30 s (``kiro_crew.apps.routes._PROXY_TIMEOUT``, total, a stream included): a
#: request still running after this long answers with a ticket instead, and the page collects its answer from it.
DEFER_AFTER = 20.0
#: How long one poll of a ticket waits for news: a short GET, far inside the proxy's limit.
POLL_WAIT = 1.5
#: A finished ticket is kept this long for its page to collect, and at most this many tickets at once.
TICKET_TTL = 600.0
MAX_TICKETS = 200
TICKET_PATH = re.compile(r"^/tickets/([A-Za-z0-9_-]{16,40})$")
#: The host checks are kept this long (every page load shows their banner); ``GET /host?fresh=1`` runs them again.
HOST_TTL = 120.0
#: The console's page inside KiroCrew is on the dashboard's origin: only the dashboard may frame it, and it loads
#: nothing from anywhere else.
FRAME_HEADERS = {"X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'")}
#: Where kiro-cli installs itself. KiroCrew starts an App's backend with a short PATH (``/opt/homebrew/bin:/usr/bin:
#: /bin:/usr/sbin:/sbin``) that misses ``~/.local/bin``; Autopilot's loop and the kiro check run kiro-cli by name.
KIRO_PLACES = ("~/.local/bin/kiro-cli", "/opt/homebrew/bin/kiro-cli", "/usr/local/bin/kiro-cli")


class Reply:
    """What the :class:`Mount` answers: a whole body, or ``chunks`` to stream (the App's backend writes either)."""

    def __init__(self, status: int, body: bytes = b"", content_type: str = "application/json; charset=utf-8", *,
                 headers: Mapping[str, str] | None = None, chunks: Any = None):
        self.status, self.body, self.content_type, self.headers, self.chunks = status, body, content_type, dict(headers or {}), chunks

    @classmethod
    def json(cls, status: int, payload: Any, headers: Mapping[str, str] | None = None) -> "Reply":
        return cls(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers)

    @classmethod
    def of(cls, result: Any) -> "Reply":
        """A route's result: ``(status, payload)`` or a :class:`Stream`."""
        if isinstance(result, Stream):
            return cls(200, content_type=result.content_type, chunks=result.chunks)
        status, payload = result
        return cls.json(status, payload)


def stream_events(stream: Stream) -> Iterator[dict[str, Any]]:
    """The events of a server-sent-events :class:`Stream` (``web.sse``), one ``data:`` line of JSON each."""
    decoder, buffer = codecs.getincrementaldecoder("utf-8")("replace"), ""
    for chunk in stream.chunks:
        buffer += decoder.decode(chunk)
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            for line in block.split("\n"):
                if line.startswith("data: "):
                    yield json.loads(line[6:])


def _late(result: Any) -> tuple[int, Any]:
    """A result nobody waits on any more, as its ticket keeps it: a status and a JSON payload."""
    if isinstance(result, Stream):  # its bytes would go nowhere: say so instead of starting it
        close = getattr(result.chunks, "close", None)
        if close:
            close()
        return 504, {"error": "this answer is a stream that took longer than KiroCrew's 30-second proxy limit to start: open it again"}
    return result


class Tickets:
    """Work that outlives one proxied request: a late answer (``answer``: its status and payload once done) or a turn's
    events (``events``: buffered as they come, then done; a turn refused before its first event keeps the refusal)."""

    def __init__(self):
        self._items: dict[str, dict[str, Any]] = {}
        self._cond = threading.Condition()

    def new(self, kind: str) -> dict[str, Any]:
        now = time.monotonic()
        item = {"id": secrets.token_urlsafe(16), "kind": kind, "events": [], "done": False, "result": None, "finished": None}
        with self._cond:
            self._items = {k: v for k, v in self._items.items() if not v["done"] or now - v["finished"] < TICKET_TTL}
            while len(self._items) >= MAX_TICKETS:  # the oldest go first (a dict keeps its insertion order)
                self._items.pop(next(iter(self._items)))
            self._items[item["id"]] = item
        return item

    def get(self, tid: str) -> dict[str, Any]:
        with self._cond:
            found = self._items.get(tid)
        if not found:
            raise HttpError(404, "no such ticket: it was collected or it expired (the App's backend may have restarted)")
        return found

    def push(self, item: dict[str, Any], event: dict[str, Any]) -> None:
        with self._cond:
            item["events"].append(event)
            self._cond.notify_all()

    def finish(self, item: dict[str, Any], result: tuple[int, Any] | None = None) -> None:
        with self._cond:
            item.update(done=True, result=result, finished=time.monotonic())
            self._cond.notify_all()

    def wait(self, item: dict[str, Any], *, after: int, timeout: float) -> dict[str, Any]:
        """What ``item`` holds after its event ``after``, once it has more or is done, or after ``timeout`` seconds."""
        with self._cond:
            self._cond.wait_for(lambda: item["done"] or len(item["events"]) > after, timeout)
            events, done, result = item["events"][after:], item["done"], item["result"]
        out: dict[str, Any] = {"ticket": item["id"], "kind": item["kind"], "events": events, "next": after + len(events), "done": done}
        if done and result is not None:
            out["status"], out["payload"] = result
        return out


def find_kiro() -> str | None:
    """kiro-cli: on ``PATH``, else where it installs itself (:data:`KIRO_PLACES`)."""
    found = shutil.which("kiro-cli")
    if found:
        return found
    for place in KIRO_PLACES:
        path = Path(place).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _denied(exc: BaseException) -> bool:
    """Whether the OS refused ``exc``'s operation (EPERM, "Operation not permitted"): what a sandbox denial looks like."""
    return isinstance(exc, PermissionError) or getattr(exc, "errno", None) == errno.EPERM or "Operation not permitted" in str(exc)


def _check(cid: str, label: str, fn: Callable[[], tuple[Any, ...]], fix: str) -> dict[str, Any]:
    """One host check: ``fn()`` gives ``(ok, detail[, blocked])`` (``ok`` None: nothing to check). An exception is a
    failure, and ``blocked`` when the OS refused it; a failure carries its ``fix``."""
    try:
        ok, detail, *rest = fn()
        blocked = bool(rest and rest[0])
    except Exception as exc:  # noqa: BLE001 - the check's finding
        ok, detail, blocked = False, f"{type(exc).__name__}: {str(exc)[:400]}", _denied(exc)
    return {"id": cid, "label": label, "ok": ok, "detail": detail, "blocked": blocked, "fix": fix if ok is False else None}


class Mount:
    """The console inside KiroCrew, mounted in the App's process backend under ``/api/console`` (the browser's path is
    ``/apps/<app>/api/console/``, through the gateway's signed proxy).

    * ``GET /api/console/`` — the console's page, written for its base path; ``/api/console/static/...`` — its files.
    * ``/api/console/...`` — the same routes as the standalone server's, as :data:`KIROCREW_CALLER`; ``/login`` and
      ``/logout`` are refused (KiroCrew's sign-in is the console's).
    * The proxy's 30-second limit: a turn (a POST with ``X-Adlc-Events: poll``) runs on a thread that buffers its events,
      which the page polls (``GET /api/console/tickets/{id}?after=N``, each poll at most :data:`POLL_WAIT`); any other
      request still running after :data:`DEFER_AFTER` answers ``202`` with ``X-Adlc-Ticket``, and the page collects its
      answer from the ticket the same way.
    * ``GET /api/console/host`` — what the App's backend can do from KiroCrew's sandbox (the AWS profile files, AWS over
      the network, the data directory, subprocesses, kiro-cli); ``POST /api/console/host/kiro`` runs one real kiro-cli
      turn as a job.
    * ``/v1/...`` (:meth:`public`) — the public API, on the App backend's own local port (``app/backend/server.py``'s
      ``start_public_api``), not through the gateway: the gateway admits only a signed-in browser, and cuts every
      request at 30 s. ``public_api`` is where that port listens (or why it does not).

    The console's state is in the App's data directory (``<data>/console``), and its workshop data is the App's own."""

    PREFIX = CONSOLE_PREFIX

    def __init__(self, data_dir: Path, *, app: str = "workshop-customizer", session_factory: Callable[..., Any] | None = None,
                 kiro_run: Callable[..., tuple[str, str, int, float]] | None = None, run: Callable[..., Any] | None = None,
                 public_api: dict[str, Any] | None = None):
        """``session_factory`` (boto3 sessions), ``kiro_run`` (one kiro-cli turn) and ``run`` (``subprocess.run`` of the
        host checks) stand in for the real ones in tests; ``public_api`` is the App backend's public-API port, as it
        starts it (kept by reference)."""
        self.data_dir = Path(data_dir)
        self.public_api = public_api if public_api is not None else {}
        self.base = f"/apps/{app}{self.PREFIX}"
        self.tickets = Tickets()
        self._factory, self._kiro_run, self._run = session_factory, kiro_run, run
        self._console: Console | None = None
        self._lock = threading.Lock()
        self._host: tuple[float, dict[str, Any]] | None = None  # the last host checks, for the banner every page load shows

    @classmethod
    def handles(cls, path: str) -> bool:
        return path == cls.PREFIX or path.startswith(cls.PREFIX + "/")

    @staticmethod
    def body_limit(path: str) -> int:
        return body_limit(path)

    @property
    def console(self) -> Console:
        """The console, made on first use: the App's backend starts, and its Workshop pages work, without it."""
        with self._lock:
            if self._console is None:
                kiro = find_kiro()
                if kiro and not shutil.which("kiro-cli"):  # Autopilot's loop runs kiro-cli by name
                    os.environ["PATH"] = f"{Path(kiro).parent}{os.pathsep}{os.environ.get('PATH', '')}"
                self._console = Console(self.data_dir, workshop_data=self.data_dir, session_factory=self._factory, workshop=False)
            return self._console

    # -- requests -------------------------------------------------------------------------------------------------
    def handle(self, method: str, target: str, raw: bytes, headers: Mapping[str, str] | None = None) -> Reply:
        """One request whose gateway signature the App's backend verified (``target``: its path and query)."""
        split = urlsplit(target)
        path = split.path[len(self.PREFIX):] or "/"
        query = {k: v[-1] for k, v in parse_qs(split.query).items()}
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return Reply(200, page(base=self.base + "/", api=self.base, host="kirocrew"), "text/html; charset=utf-8", headers=FRAME_HEADERS)
            if method == "GET" and path.startswith("/static/"):
                found = static_file(path[len("/static/"):])
                return Reply(200, found.read_bytes(), content_type(found))
            if path in ("/login", "/logout"):
                raise HttpError(409, "inside KiroCrew the console signs in with KiroCrew: the console's own users sign in on the standalone server "
                                     "(app/console/server.py)")
            ticket = TICKET_PATH.match(path)
            if ticket and method == "GET":
                try:
                    after = max(0, int(query.get("after") or 0))
                except ValueError as exc:
                    raise HttpError(400, "after: the number of events already seen") from exc
                return Reply.json(200, self.tickets.wait(self.tickets.get(ticket.group(1)), after=after, timeout=POLL_WAIT))
            work = self._work(method, path, query, raw)
            if method == "POST" and str((headers or {}).get("X-Adlc-Events") or "").lower() == "poll":
                return self._events(work)
            return self._deferred(work)
        except Exception as exc:  # noqa: BLE001 - each kind of refusal keeps its status
            return Reply.json(*refusal(self._console, exc))

    def public(self, method: str, target: str, raw: bytes, headers: Mapping[str, str]) -> Reply:
        """``/v1/...`` on the App backend's own local port: the public API behind its ``X-Api-Key``, as the standalone
        server serves it, with no gateway in between (no 30-second limit; a stream streams)."""
        from workshop_customizer.console import public

        path = urlsplit(target).path
        if not path.startswith("/v1/"):
            return Reply.json(404, {"error": f"no route {path}: this port serves only the public API under /v1"})
        try:
            status, result = public.handle(self.console, method, path[len("/v1"):], headers, json_body(raw))
            return Reply.of(result if isinstance(result, Stream) else (status, result))
        except Exception as exc:  # noqa: BLE001 - each kind of refusal keeps its status
            return Reply.json(*refusal(self._console, exc))

    def _work(self, method: str, path: str, query: dict[str, str], raw: bytes) -> Callable[[], Any]:
        body = json_body(raw)  # a body that is not JSON is refused now, not on a thread

        def work() -> Any:
            if path == "/host" and method == "GET":
                cached = self._host
                if query.get("fresh") != "1" and cached and time.monotonic() - cached[0] < HOST_TTL:
                    return 200, cached[1]
                found = self.host()
                self._host = (time.monotonic(), found)
                return 200, found
            if path == "/host/kiro" and method == "POST":
                return 202, {"job": self.kiro_check()}
            return run_route(self.console, method, path, query, body, raw, dict(KIROCREW_CALLER))

        return work

    def _deferred(self, work: Callable[[], Any]) -> Reply:
        """``work`` on a thread: its answer when it comes within :data:`DEFER_AFTER`, else a ticket that gets it."""
        lock, came, box = threading.Lock(), threading.Event(), {"ticket": None, "result": None}

        def run() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - the request's refusal
                result = refusal(self._console, exc)
            with lock:
                if box["ticket"] is None:  # the request still waits: it answers
                    box["result"] = result
                    came.set()
                    return
            self.tickets.finish(box["ticket"], _late(result))

        threading.Thread(target=run, name="console-request", daemon=True).start()
        came.wait(DEFER_AFTER)
        with lock:
            if not came.is_set():
                box["ticket"] = self.tickets.new("answer")
        if box["ticket"] is None:
            return Reply.of(box["result"])
        tid = box["ticket"]["id"]
        return Reply.json(202, {"ticket": tid, "kind": "answer"}, headers={"X-Adlc-Ticket": tid})

    def _events(self, work: Callable[[], Any]) -> Reply:
        """A turn on a thread, its events buffered on a ticket the page polls. A refusal before the first event (a turn
        already running, a message too long) answers this request, as it would have without the ticket."""
        item = self.tickets.new("events")

        def run() -> None:
            result = None
            try:
                out = work()
                if isinstance(out, Stream):
                    for event in stream_events(out):
                        if event.get("type") != "ping":  # the stream's keep-alive: a poll needs none
                            self.tickets.push(item, event)
                else:
                    result = out
            except Exception as exc:  # noqa: BLE001 - the turn's refusal, or a stream that broke part-way
                status, payload = refusal(self._console, exc)
                if item["events"]:
                    self.tickets.push(item, {"type": "error", "error": payload.get("error") or f"HTTP {status}"})
                else:
                    result = (status, payload)
            self.tickets.finish(item, result)

        threading.Thread(target=run, name="console-turn", daemon=True).start()
        first = self.tickets.wait(item, after=0, timeout=DEFER_AFTER)
        if first["done"] and not first["events"] and first.get("status", 200) >= 400:
            return Reply.json(first["status"], first["payload"])
        return Reply.json(202, {"ticket": item["id"], "kind": "events"}, headers={"X-Adlc-Ticket": item["id"]})

    # -- the host -------------------------------------------------------------------------------------------------
    def host(self) -> dict[str, Any]:
        """What the App's backend can do for the console from where KiroCrew runs it. Every check is read-only; the AWS
        ones call STS GetCallerIdentity."""
        console, run = self.console, self._run or subprocess.run
        workspaces = console.workspaces.list()
        profiled = [w for w in workspaces if w.get("profile") and not w.get("roleArn")]

        def aws_files() -> tuple[Any, ...]:
            seen = []
            for env, name in (("AWS_CONFIG_FILE", "config"), ("AWS_SHARED_CREDENTIALS_FILE", "credentials")):
                path = Path(os.environ.get(env) or Path.home() / ".aws" / name).expanduser()
                try:
                    with open(path, "rb") as fh:
                        fh.read(1)
                    seen.append(f"{path} 可读")
                except FileNotFoundError:
                    seen.append(f"{path} 不存在")
            return True, "；".join(seen)

        def aws() -> tuple[Any, ...]:
            if not workspaces:
                return None, "还没有工作区"
            import socket
            import urllib.request

            from botocore.config import Config

            proxy = urllib.request.getproxies().get("https")  # what botocore uses: HTTPS_PROXY, else the macOS system proxy
            lines, ok, blocked = [f"出站：{'经代理 ' + proxy if proxy else '直连'}"], True, False
            for ws in workspaces:
                started = time.monotonic()
                try:
                    session = console.workspaces.session(ws["id"])
                    creds = getattr(session, "get_credentials", lambda: None)()
                    how = getattr(creds, "method", None) or ("assume-role" if ws.get("roleArn") else "?")
                    try:
                        sts = session.client("sts", region_name=ws["region"], config=Config(connect_timeout=5, read_timeout=10,
                                                                                              retries={"max_attempts": 2, "mode": "standard"}))
                    except TypeError:  # a test double without the config keyword
                        sts = session.client("sts", region_name=ws["region"])
                    who = sts.get_caller_identity()
                    same = who["Account"] == ws["accountId"]
                    ok = ok and same
                    via = f"profile {ws['profile']}" if ws.get("profile") and not ws.get("roleArn") else "spoke 角色"
                    lines.append(f"{ws['id']}：{via}（凭证来自 {how}）→ {who['Arn']}，{time.monotonic() - started:.1f} 秒"
                                 + ("" if same else f"，不是工作区的账号 {ws['accountId']}"))
                except Exception as exc:  # noqa: BLE001 - this workspace's finding, with what a bare connection says
                    ok, blocked = False, blocked or _denied(exc)
                    lines.append(f"{ws['id']}：{type(exc).__name__}: {str(exc)[:300]}")
                    host_port = (proxy.split("://", 1)[-1].rsplit(":", 1) if proxy else [f"sts.{ws['region']}.amazonaws.com", "443"])
                    try:
                        socket.create_connection((host_port[0], int(host_port[1])), timeout=5).close()
                        lines.append(f"TCP {host_port[0]}:{host_port[1]} 连得上")
                    except OSError as sock:
                        blocked = blocked or _denied(sock)
                        lines.append(f"TCP {host_port[0]}:{host_port[1]}：{sock}")
            return ok, "；".join(lines), blocked

        def data_write() -> tuple[Any, ...]:
            probe = console.store.root / f".write-probe-{secrets.token_hex(4)}"
            probe.write_text("ok", encoding="utf-8")
            try:
                same = probe.read_text(encoding="utf-8") == "ok"
            finally:
                probe.unlink()
            return same, f"{console.store.root} 可写" if same else f"{console.store.root}：写进去的读不回来"

        def child() -> tuple[Any, ...]:
            # The shape of the subprocesses deploys and direct rehearsals run: this Python, boto3, the profile, the network.
            ws = profiled[0] if profiled else {}
            code = ("import json, sys, boto3\n"
                    "out = {'python': sys.version.split()[0], 'boto3': boto3.__version__}\n"
                    "if sys.argv[1]:\n"
                    "    s = boto3.Session(profile_name=sys.argv[1], region_name=sys.argv[2])\n"
                    "    out['account'] = s.client('sts').get_caller_identity()['Account']\n"
                    "print(json.dumps(out))\n")
            done = run([sys.executable, "-c", code, ws.get("profile") or "", ws.get("region") or "us-west-2"], capture_output=True, text=True, timeout=60)
            if done.returncode != 0:
                tail = (done.stderr or done.stdout).strip()[-400:]
                return False, f"退出码 {done.returncode}：{tail}", "Operation not permitted" in tail
            facts = json.loads(done.stdout.strip().splitlines()[-1])
            return True, (f"{sys.executable}：Python {facts['python']}，boto3 {facts['boto3']}"
                          + (f"，子进程里 STS 到账号 {facts['account']}" if facts.get("account") else ""))

        def checkout_python() -> tuple[Any, ...]:
            # Direct rehearsals' scripts, the judges and Autopilot's loop run on the checkout's interpreter (its packages).
            from workshop_customizer.direct.aws import tools_python, with_vendor

            python = tools_python()
            done = run([python, "-c", "import sys, boto3, retrying; print(sys.version.split()[0])"], capture_output=True, text=True, timeout=60,
                       env=with_vendor(os.environ))
            if done.returncode != 0:
                tail = (done.stderr or done.stdout).strip()[-400:]
                return False, f"{python} 退出码 {done.returncode}：{tail}", "Operation not permitted" in tail
            return True, f"{python}：Python {done.stdout.strip()}，有 boto3 和 retrying"

        def kiro() -> tuple[Any, ...]:
            found = find_kiro()
            if not found:
                return False, "找不到 kiro-cli（PATH 和 " + "、".join(KIRO_PLACES) + "）"
            version = run([found, "--version"], capture_output=True, text=True, timeout=30)
            if version.returncode != 0:
                tail = (version.stderr or version.stdout).strip()[-300:]
                return False, f"{found} --version 退出码 {version.returncode}：{tail}", "Operation not permitted" in tail
            who = run([found, "whoami"], capture_output=True, text=True, timeout=30)
            lines = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", who.stdout or who.stderr).strip().splitlines()
            first = lines[0][:160] if lines else "没有输出"
            if who.returncode != 0:
                return False, f"{found}（{version.stdout.strip()}）能启动，whoami 退出码 {who.returncode}：{first}（没登录就 kiro-cli login）"
            return True, f"{found}（{version.stdout.strip()}）能启动，{first}；要真跑一轮 Kiro，点「真跑一轮 kiro-cli」"

        fixes = {
            "aws-files": "KiroCrew 给 App 后端的是标准沙箱，它本来不遮 ~/.aws（strict 档才遮）：让这个 App 留在标准档。",
            "aws": "沙箱里的后端调不通 AWS：先看工作区的 profile 和本机代理（没有 HTTPS_PROXY 时 boto3 用 macOS 的系统代理）；"
                   "如果是沙箱挡了出站网络，最小的放开是只给这个 App 的后端放开到 AWS 和代理的出站连接。",
            "data-write": "App 的数据目录（~/.kiro/crew/apps/workshop-customizer/data）要可写：KiroCrew 的沙箱只该挡凭证和它自己的目录。",
            "subprocess": "部署和直连彩排的 Python 子进程启动不了：最小的放开是只允许这个 App 的后端执行它自己的 Python（sys.executable），不是关掉沙箱。",
            "checkout-python": "直连彩排的建库脚本、裁判和自动驾驶要用仓库 .venv 的 Python（它装着 retrying 等依赖）：最小的放开是只允许这个 App 的后端执行"
                               "仓库的 .venv/bin/python3；或者在仓库里重建 .venv（python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt）。",
            "kiro-cli": "沙箱里的后端启动不了 kiro-cli：最小的放开是只给这个 App 的后端允许执行 kiro-cli 和它读写的 ~/.kiro、"
                        "~/Library/Application Support/kiro-cli；不是 agent.sandbox_allow_unsandboxed_exec。由你决定。",
        }
        checks = (("aws-files", "读 ~/.aws（boto3 的 profile 文件）", aws_files), ("aws", "用工作区的凭证经网络调用 AWS（STS，只读）", aws),
                  ("data-write", "写 App 的数据目录", data_write), ("subprocess", "启动 Python 子进程（部署、直连彩排的形状：boto3 和网络）", child),
                  ("checkout-python", "启动直连彩排脚本用的 Python（仓库的 .venv，或 KiroCrew 装好依赖的 Python）", checkout_python),
                  ("kiro-cli", "启动 kiro-cli（自动驾驶用；工作坊的 Kiro 生成走 KiroCrew 自己的 ctx.spawn）", kiro))
        with ThreadPoolExecutor(max_workers=len(checks)) as pool:
            results = [f.result() for f in [pool.submit(_check, cid, label, fn, fixes[cid]) for cid, label, fn in checks]]
        return {"mode": "kirocrew", "sandbox": {"active": os.environ.get("KIROCREW_SANDBOX_ACTIVE") == "1",
                                                "level": os.environ.get("KIROCREW_SANDBOX_LEVEL") or None},
                "dataDir": str(self.data_dir), "consoleData": str(console.store.root), "home": str(REPO), "python": sys.executable,
                "publicApi": dict(self.public_api) or None,
                "checkedAt": _now(), "checks": results}

    def kiro_check(self) -> dict[str, Any]:
        """One real kiro-cli turn with the App's restricted agent, the way Autopilot's loop runs Kiro, as a job."""
        console = self.console

        def work(ctx: Any) -> dict[str, Any]:
            run = self._kiro_run or _load("kiro_generate_check", REPO / "tools" / "kiro_generate.py").run_kiro
            ctx.log(f"kiro-cli：{find_kiro() or '找不到'}")
            stdout, stderr, code, seconds = run("This is a connectivity check, not a scenario. Reply with the single word OK.", timeout=300)
            ctx.log(f"退出码 {code}，{seconds:.1f} 秒")
            if code != 0:
                raise RuntimeError(f"kiro-cli exited {code}: {(stderr or stdout).strip()[-500:]}")
            return {"exitCode": code, "seconds": round(seconds, 1), "stdout": stdout.strip()[-500:], "stderr": stderr.strip()[-300:]}

        return console.jobs.start("kiro-check", "", {}, work, label="真跑一轮 kiro-cli")


# ---------------------------------------------------------------------------------------------------------------------
# Moving a standalone console's state into the App
# ---------------------------------------------------------------------------------------------------------------------

def import_state(source: Path, target: Path) -> dict[str, Any]:
    """Copy a standalone console's state (``<source>/console``: workspaces, users, keys, jobs, the knowledge-base and
    skill-library records and every other collection) into ``<target>/console``, once: refused when the target already
    has console state. The source is only read."""
    src, dst = Path(source).expanduser() / "console", Path(target).expanduser() / "console"
    if not src.is_dir():
        raise ValueError(f"no console state in {source} (expected {src})")
    if dst.is_dir() and any(p.is_file() for p in dst.rglob("*")):
        raise FileExistsError(f"{dst} already holds console state: nothing was imported")
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.json.tmp", "__pycache__"))
    files = sorted(str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file())
    record = {"from": str(src.resolve()), "at": _now(), "files": len(files),
              "collections": sorted(p.stem for p in dst.glob("*.json")), "jobs": len(list((dst / "jobs").glob("*.json")))}
    (dst / ".imported.json").write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process entry point
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default localhost; define users before binding wider)")
    parser.add_argument("--data", type=Path, default=Path.home() / ".adlc-console")
    parser.add_argument("--workshop-data", type=Path, help="the workshop App's data dir (default <data>/workshop)")
    parser.add_argument("--import-from", type=Path, help="copy this standalone console's data dir (its console/) into --into, once, and exit")
    parser.add_argument("--into", type=Path, help="the App's data dir under KiroCrew (~/.kiro/crew/apps/workshop-customizer/data)")
    args = parser.parse_args(argv)
    if args.import_from or args.into:
        if not (args.import_from and args.into):
            parser.error("--import-from and --into go together")
        print(json.dumps(import_state(args.import_from, args.into), ensure_ascii=False, indent=1))
        return 0
    console = Console(args.data, workshop_data=args.workshop_data)
    if args.host not in ("127.0.0.1", "localhost") and console.auth.open_mode():
        print("refusing to bind beyond localhost in open mode: create an admin user first", file=sys.stderr)
        return 2
    server = create_server(console, host=args.host, port=args.port)
    print(f"ADLC console on http://{args.host}:{server.server_address[1]}  (data {console.data_dir}, workshop data {console.workshop_data})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
