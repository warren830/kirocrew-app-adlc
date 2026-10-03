"""The console inside KiroCrew: mounted in the App's process backend (app/backend/server.py) under /api/console, behind
the gateway's signed proxy (app/console/server.py's Mount). In-process backend on loopback; this test signs requests as
KiroCrew's proxy does (handle_app_api_proxy: ``<ts>:hmac(secret, "<ts>:<METHOD>:<target>:<sha256(body)>")``); AWS is a
fake session."""
from __future__ import annotations

import errno
import hashlib
import http.client
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin

import pytest

REPO = Path(__file__).resolve().parents[1]
SECRET = "kirocrew-proxy-test-secret"
BASE = "/apps/workshop-customizer/api/console"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


backend = _load("wc_app_server_for_kirocrew_tests", REPO / "app" / "backend" / "server.py")
T = _load("console_tests_for_kirocrew", REPO / "tests" / "test_console.py")  # its fake AWS session and the console server module
ACCOUNT = T.ACCOUNT


class Gateway:
    """KiroCrew's proxy, for the test: the browser's /apps/workshop-customizer/api/<path> is the backend's /api/<path>,
    signed with the App's secret."""

    def __init__(self, port: int, secret: str = SECRET):
        self.port, self.secret = port, secret

    def request(self, method: str, target: str, body=None, *, sign: bool = True, secret: str | None = None, ts: int | None = None,
                headers: dict | None = None) -> http.client.HTTPResponse:
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else b"")
        hdrs = {"Content-Type": "application/json", "Origin": "http://localhost:5476", **(headers or {})}
        if sign:
            hdrs["X-KiroCrew-Proxy"] = backend.sign_for_tests(secret or self.secret, method, target, data, ts)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, target, body=data or None, headers=hdrs)
        return conn.getresponse()

    def call(self, method: str, target: str, body=None, **kw):
        response = self.request(method, target, body, **kw)
        raw = response.read()
        kind = response.headers.get("Content-Type", "")
        return response.status, (json.loads(raw) if "json" in kind and raw else raw.decode("utf-8", "replace")), dict(response.headers)

    def collect(self, ticket: str, *, after: int = 0, deadline: float = 20) -> dict:
        """Poll a ticket as the page does until it is done: its events, and its status and payload when it has them."""
        events, ends = [], time.monotonic() + deadline
        while time.monotonic() < ends:
            status, got, _ = self.call("GET", f"/api/console/tickets/{ticket}?after={after}")
            assert status == 200, got
            events += got["events"]
            after = got["next"]
            if got["done"]:
                return {**got, "events": events}
        raise AssertionError("the ticket never finished")


class HeldRuntime:
    """A Harness whose answer stops after its first words until the test lets it go on."""

    def __init__(self):
        self.go_on = threading.Event()

    def invoke_harness(self, **kw):
        def stream():
            yield {"contentBlockDelta": {"delta": {"text": "Annual leave is "}}}
            self.go_on.wait(15)
            yield {"contentBlockStart": {"start": {"toolUse": {"name": "hrtools___retrieve_policy"}}}}
            yield {"contentBlockDelta": {"delta": {"text": "15 days."}}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            yield {"metadata": {"usage": {"inputTokens": 120, "outputTokens": 9}}}

        return {"stream": stream()}


@pytest.fixture()
def kirocrew(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws-credentials"))
    (tmp_path / "aws-config").write_text("[default]\nregion = us-west-2\n")
    fake = T.FakeSession()
    srv, _svc = backend.create_server(tmp_path / "appdata", home=REPO, proxy_secret=SECRET, clients_factory=lambda cfg: None,
                                      session_factory=lambda **kw: fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    gw = Gateway(srv.server_address[1])
    yield SimpleNamespace(gw=gw, server=srv, fake=fake, data=tmp_path / "appdata", mount=srv.console.get)
    srv.shutdown()
    srv.server_close()


def _workspace(gw: Gateway) -> None:
    status, ws, _ = gw.call("POST", "/api/console/workspaces", {"id": "dev", "name": "Dev", "accountId": ACCOUNT, "region": "us-west-2", "profile": "default"})
    assert status == 201, ws


def _events(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.split("\n") if line.startswith("data: ")]


# -- the gateway's signature is the console's sign-in -------------------------------------------------------------------

def test_a_signed_request_reaches_the_console_and_an_unsigned_or_wrongly_signed_one_is_refused(kirocrew):
    gw = kirocrew.gw
    status, me, _ = gw.call("GET", "/api/console/me")
    assert status == 200 and me["role"] == "admin" and me["workspaces"] == ["*"] and me["host"] == "kirocrew" and me["open"] is False
    assert gw.call("GET", "/api/console/me", sign=False)[0] == 401
    assert gw.call("GET", "/api/console/me", secret="another-apps-secret")[0] == 401
    assert gw.call("GET", "/api/console/me", ts=int(time.time()) - 3600)[0] == 401  # a replayed, stale signature
    signed_for_another = backend.sign_for_tests(SECRET, "GET", "/api/console/keys", b"")
    assert gw.call("GET", "/api/console/me", sign=False, headers={"X-KiroCrew-Proxy": signed_for_another})[0] == 401
    body = {"id": "dev", "accountId": ACCOUNT, "region": "us-west-2", "profile": "default"}
    tampered = backend.sign_for_tests(SECRET, "POST", "/api/console/workspaces", b'{"id": "other"}')
    assert gw.call("POST", "/api/console/workspaces", body, sign=False, headers={"X-KiroCrew-Proxy": tampered})[0] == 401  # the body is signed too
    status, out, _ = gw.call("POST", "/api/console/login", {"username": "admin", "password": "0123456789"})
    assert status == 409 and "KiroCrew" in out["error"]  # the console's own sign-in is the standalone server's
    assert gw.call("GET", "/health", sign=False)[0] == 200  # the gateway's unsigned liveness probe, as before
    status, projects, _ = gw.call("GET", "/api/apps/workshop-customizer/projects")
    assert status == 200 and projects == {"projects": []}  # the Workshop's own routes beside it
    assert gw.call("GET", "/api/apps/workshop-customizer/projects", sign=False)[0] == 401


def test_without_kirocrews_secret_the_backend_serves_no_console(tmp_path):
    srv, _svc = backend.create_server(tmp_path / "appdata", home=REPO)  # local development: no KIROCREW_PROXY_SECRET
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        gw = Gateway(srv.server_address[1])
        status, out, _ = gw.call("GET", "/api/console/me", sign=False)
        assert status == 403 and "app/console/server.py" in out["error"]
        assert gw.call("GET", "/api/console/", sign=False)[0] == 403
        assert gw.call("GET", "/api/apps/workshop-customizer/projects", sign=False)[0] == 200  # the Workshop's dev mode is unchanged
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_console_body_takes_the_consoles_limit_and_an_unsigned_one_is_not_read(kirocrew):
    gw = kirocrew.gw
    big = json.dumps({"id": "dev", "pad": "x" * (9 * 1024 * 1024)}).encode()
    status, out, _ = gw.call("POST", "/api/console/workspaces", big)
    assert status == 409 and "accountId" in out["error"]  # read (9 MiB is over the Workshop's 8, under the console's 25) and checked
    def announced(path: str, length: int, **headers: str) -> int:  # a body announced, never sent: refused before any of it is read
        conn = http.client.HTTPConnection("127.0.0.1", gw.port, timeout=10)
        conn.putrequest("POST", path)
        for key, value in {"Content-Type": "application/json", "Content-Length": str(length), **headers}.items():
            conn.putheader(key, value)
        conn.endheaders()
        return conn.getresponse().status

    assert announced("/api/apps/workshop-customizer/projects", len(big)) == 413  # the Workshop's routes keep their 8 MiB
    assert announced("/api/console/workspaces", 20 * 1024 * 1024) == 401  # no gateway signature: nothing is read
    assert announced("/api/console/workspaces", 30 * 1024 * 1024, **{"X-KiroCrew-Proxy": "1:x"}) == 413  # over the console's 25 MiB


# -- the page under its base path ---------------------------------------------------------------------------------------

def test_the_console_page_and_its_files_load_under_the_base_path(kirocrew):
    gw = kirocrew.gw
    status, html, headers = gw.call("GET", "/api/console/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert f'<base href="{BASE}/">' in html and f'<meta name="adlc-api" content="{BASE}">' in html and '<meta name="adlc-host" content="kirocrew">' in html
    assert headers["X-Frame-Options"] == "SAMEORIGIN" and "frame-ancestors 'self'" in headers["Content-Security-Policy"]
    urls = re.findall(r'src="([^"]+)"', html) + re.findall(r'"(\./static/[^"]+)"', html) + re.findall(r"from '(\./static/[^']+)'", html)
    assert len(urls) >= 7 and not [u for u in urls if u.startswith("/")]  # every one relative: it resolves under the base
    for url in urls:
        browser = urljoin(BASE + "/", url)  # what the browser asks the gateway for
        assert browser.startswith(BASE + "/static/"), browser
        status, body, headers = gw.call("GET", browser[len("/apps/workshop-customizer"):])  # what the gateway asks the backend for
        assert status == 200 and headers["Content-Type"].startswith("text/javascript"), browser
    assert gw.call("GET", "/api/console")[0] == 200  # without the slash too: the base is absolute
    assert gw.call("GET", "/api/console/static/../server.py")[0] == 404
    assert gw.call("GET", "/api/console/static/nope.mjs")[0] == 404
    standalone = T.console_mod.page().decode()  # the standalone server's page keeps its root
    assert '<base href="/">' in standalone and '<meta name="adlc-api" content="/api/console">' in standalone and 'content="standalone"' in standalone


def test_the_pages_call_the_api_the_page_names_and_inside_kirocrew_poll_their_turns():
    static = REPO / "app" / "console" / "static"
    ui = (static / "ui.mjs").read_text(encoding="utf-8")
    assert "const API = meta('adlc-api', '/api/console')" in ui and "export const IN_KIROCREW = HOST === 'kirocrew'" in ui
    assert "'X-Adlc-Events': 'poll'" in ui and "response.headers.get('X-Adlc-Ticket')" in ui and "/tickets/${ticket}?after=${after}" in ui
    assert "postMessage({ type: 'adlc-console:open', view: 'workshop' }, window.location.origin)" in ui
    pages = [static / "console.mjs", *sorted((static / "pages").glob("*.mjs"))]
    for path in pages:  # no page fetches on its own, so none calls an absolute /api/console
        text = path.read_text(encoding="utf-8")
        assert "fetch(" not in text and "'/api/console" not in text and '"/api/console' not in text, path.name
    console = (static / "console.mjs").read_text(encoding="utf-8")
    assert "if (p === 'workshops' && IN_KIROCREW) { openWorkshop(); return }" in console and "import(new URL('app/index.mjs', document.baseURI).href)" in console
    node = shutil.which("node") or str(Path.home() / ".nvm" / "versions" / "node" / "v25.2.1" / "bin" / "node")
    if not Path(node).exists():
        pytest.skip("no node to parse the modules")
    for path in (static / "ui.mjs", static / "console.mjs", REPO / "app" / "ui" / "dist" / "platform.mjs"):
        checked = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        assert checked.returncode == 0, (path.name, checked.stderr)


def test_the_apps_page_frames_the_console_and_its_workshop_view_is_kirocrews_own():
    manifest = json.loads((REPO / "app" / "app.json").read_text(encoding="utf-8"))
    assert manifest["ui"]["entry"] == "dist/platform.mjs"  # KiroCrew mounts ui.entry at /apps/<name>: one sidebar entry per App
    [page] = manifest["ui"]["pages"]
    assert page == {"route": "/apps/workshop-customizer", "label": "ADLC 控制台", "icon": "Layers"}
    assert manifest["permissions"]["api"] == ["/api/apps/workshop-customizer", "/apps/workshop-customizer/api"]
    text = (REPO / "app" / "ui" / "dist" / "platform.mjs").read_text(encoding="utf-8")
    assert re.findall(r"^import .* from '([^']+)'", text, flags=re.M) == ["react", "./index.mjs"]  # the host's React, the Workshop page itself
    assert "export const CONSOLE_URL = '/apps/workshop-customizer/api/console/'" in text and "h(WorkshopCustomizerApp)" in text
    # only the console's own frame, on this origin, switches the view
    assert "e.origin !== window.location.origin || !frame.current || e.source !== frame.current.contentWindow" in text
    assert "data.type === 'adlc-console:open'" in text and "/(?:^|[#?&])wc=/.test(window.location.hash)" in text  # a Workshop deep link opens 工作坊


# -- turns and long requests within the proxy's 30 seconds --------------------------------------------------------------

def test_a_chat_turn_streams_through_the_mount_as_it_comes(kirocrew):
    gw, fake = kirocrew.gw, kirocrew.fake
    _workspace(gw)
    held = fake.rt = HeldRuntime()
    response = gw.request("POST", "/api/console/workspaces/dev/agents/harness/hr_agent-1/chat", {"message": "How many days of leave?"})
    assert response.status == 200 and response.headers["Content-Type"] == "text/event-stream" and response.headers["Transfer-Encoding"] == "chunked"
    seen = b""
    while b"Annual leave is " not in seen:
        seen += response.read1(65536)
    assert not held.go_on.is_set()  # the first words arrived while the rest of the answer was still held
    held.go_on.set()
    events = _events((seen + response.read()).decode())
    assert [e["type"] for e in events] == ["session", "text", "tool", "text", "turn", "stop"] and events[-1]["inputTokens"] == 120


def test_inside_kirocrew_a_turn_runs_on_its_own_and_the_page_polls_its_events(kirocrew):
    gw, fake = kirocrew.gw, kirocrew.fake
    _workspace(gw)
    held = fake.rt = HeldRuntime()
    started = time.monotonic()
    status, out, headers = gw.call("POST", "/api/console/workspaces/dev/agents/harness/hr_agent-1/chat", {"message": "How many days of leave?"},
                                   headers={"X-Adlc-Events": "poll"})
    assert status == 202 and out["kind"] == "events" and headers["X-Adlc-Ticket"] == out["ticket"] and time.monotonic() - started < 5
    ticket, seen, after = out["ticket"], [], 0
    while not any(e["type"] == "text" for e in seen):  # the first words, while the rest of the answer is held
        status, got, _ = gw.call("GET", f"/api/console/tickets/{ticket}?after={after}")
        assert status == 200 and got["done"] is False
        seen, after = seen + got["events"], got["next"]
    assert [e["type"] for e in seen] == ["session", "text"]
    started = time.monotonic()
    status, idle, _ = gw.call("GET", f"/api/console/tickets/{ticket}?after={after}")
    assert idle["events"] == [] and idle["done"] is False and time.monotonic() - started < 4  # a poll is a short GET, news or not
    held.go_on.set()
    rest = gw.collect(ticket, after=after)
    assert [e["type"] for e in seen + rest["events"]] == ["session", "text", "tool", "text", "turn", "stop"] and "status" not in rest
    assert "".join(e["text"] for e in seen + rest["events"] if e["type"] == "text") == "Annual leave is 15 days."
    status, out, _ = gw.call("POST", "/api/console/workspaces/dev/agents/harness/hr_agent-1/chat", {"message": ""}, headers={"X-Adlc-Events": "poll"})
    assert status == 400 and out["error"]  # refused before the turn starts: this request says so, as the stream's would have
    assert gw.call("GET", "/api/console/tickets/" + "x" * 22)[0] == 404


def test_a_request_still_running_after_the_limit_answers_with_a_ticket_that_gets_its_answer(kirocrew, monkeypatch):
    gw = kirocrew.gw
    module = backend.console_module()
    monkeypatch.setattr(module, "DEFER_AFTER", 0.3)
    router = kirocrew.mount().console.router

    def slow(r):
        time.sleep(1.0)
        return 201, {"made": r.body.get("name")}

    def slow_refusal(r):
        time.sleep(1.0)
        raise ValueError("this name is taken")

    router.add("POST", "/slow-test", slow)
    router.add("POST", "/slow-refusal", slow_refusal)
    started = time.monotonic()
    status, out, headers = gw.call("POST", "/api/console/slow-test", {"name": "a"})
    assert status == 202 and out == {"ticket": headers["X-Adlc-Ticket"], "kind": "answer"} and time.monotonic() - started < 0.9
    done = gw.collect(out["ticket"])
    assert done["status"] == 201 and done["payload"] == {"made": "a"} and done["events"] == []
    status, out, _ = gw.call("POST", "/api/console/slow-refusal", {})
    assert status == 202 and gw.collect(out["ticket"])["status"] == 400
    status, fast, headers = gw.call("GET", "/api/console/me")
    assert status == 200 and fast["host"] == "kirocrew" and "X-Adlc-Ticket" not in headers  # a quick answer comes as itself


# -- what the sandboxed backend can do ----------------------------------------------------------------------------------

def test_the_host_checks_say_what_the_backend_can_do_and_what_the_sandbox_blocks(kirocrew, monkeypatch):
    gw = kirocrew.gw
    _workspace(gw)
    mount = kirocrew.mount()
    module = backend.console_module()
    monkeypatch.setattr(module, "find_kiro", lambda: "/opt/fake/kiro-cli")
    ran = []

    def run(cmd, **kw):
        ran.append(cmd)
        if "retrying" in " ".join(cmd):  # the checkout's Python, with what the release's scripts import
            return subprocess.CompletedProcess(cmd, 0, "3.14.5\n", "")
        if cmd[0] == sys.executable:
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"python": "3.12.14", "boto3": "1.43.103", "account": ACCOUNT}) + "\n", "")
        if cmd[1:] == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, "kiro-cli 2.27.0\n", "")
        return subprocess.CompletedProcess(cmd, 0, "Logged in with IAM Identity Center (https://example.awsapps.com/start)\n", "")

    mount._run = run
    status, host, _ = gw.call("GET", "/api/console/host")
    assert status == 200 and host["mode"] == "kirocrew" and host["consoleData"] == str(kirocrew.data / "console")
    checks = {c["id"]: c for c in host["checks"]}
    assert list(checks) == ["aws-files", "aws", "data-write", "subprocess", "checkout-python", "kiro-cli"] and all(c["ok"] for c in checks.values()), checks
    assert "Python 3.14.5，有 boto3 和 retrying" in checks["checkout-python"]["detail"]
    assert "aws-config 可读" in checks["aws-files"]["detail"] and "aws-credentials 不存在" in checks["aws-files"]["detail"]
    assert f"arn:aws:iam::{ACCOUNT}:user/sa" in checks["aws"]["detail"] and ACCOUNT in checks["subprocess"]["detail"]
    [child] = [cmd for cmd in ran if cmd[0] == sys.executable and "retrying" not in " ".join(cmd)]
    assert child[1] == "-c" and child[3:] == ["default", "us-west-2"]  # the child Python, with the workspace's profile
    assert ["/opt/fake/kiro-cli", "whoami"] in ran and "Logged in" in checks["kiro-cli"]["detail"]
    assert not list((kirocrew.data / "console").glob(".write-probe-*"))  # the write check leaves nothing behind

    def denied(cmd, **kw):
        if cmd[0] == "/opt/fake/kiro-cli":
            raise PermissionError(errno.EPERM, "Operation not permitted", cmd[0])
        return run(cmd, **kw)

    mount._run = denied
    cached = {c["id"]: c for c in gw.call("GET", "/api/console/host")[1]["checks"]}["kiro-cli"]
    assert cached["ok"] is True  # kept a while, for the banner every page load shows
    kiro = {c["id"]: c for c in gw.call("GET", "/api/console/host?fresh=1")[1]["checks"]}["kiro-cli"]
    assert kiro["ok"] is False and kiro["blocked"] is True and "Operation not permitted" in kiro["detail"]
    assert "agent.sandbox_allow_unsandboxed_exec" in kiro["fix"]  # the least-privilege fix is proposed, never that setting


def test_the_subprocess_check_starts_a_real_child_python_and_the_kiro_check_is_a_job(kirocrew, monkeypatch):
    gw, mount = kirocrew.gw, kirocrew.mount()
    monkeypatch.setattr(backend.console_module(), "find_kiro", lambda: None)  # no real kiro-cli in a unit test
    child = {c["id"]: c for c in gw.call("GET", "/api/console/host")[1]["checks"]}["subprocess"]
    assert child["ok"] is True and "boto3" in child["detail"]  # no workspace yet: the child only imports boto3, offline
    calls = []
    mount._kiro_run = lambda task, timeout: (calls.append((task, timeout)), ("OK", "", 0, 2.5))[1]
    status, out, _ = gw.call("POST", "/api/console/host/kiro", {})
    assert status == 202 and out["job"]["kind"] == "kiro-check"
    for _ in range(50):
        job = gw.call("GET", f"/api/console/jobs/{out['job']['id']}")[1]
        if job["status"] != "running":
            break
        time.sleep(0.1)
    assert job["status"] == "succeeded" and job["result"]["exitCode"] == 0 and calls[0][1] == 300


# -- the standalone console's state -------------------------------------------------------------------------------------

def test_the_standalone_state_is_imported_once_and_the_source_is_left_alone(kirocrew, tmp_path):
    source = tmp_path / "standalone"
    (source / "console" / "jobs").mkdir(parents=True)
    (source / "console" / "autopilot" / "autopilot-0123456789").mkdir(parents=True)
    ws = {"dev": {"id": "dev", "name": "Dev", "accountId": ACCOUNT, "region": "us-west-2", "profile": "default", "roleArn": None, "externalId": None,
                  "createdAt": "2026-10-01T12:49:50Z", "updatedAt": "2026-10-01T12:49:50Z"}}
    files = {"workspaces.json": ws, "kb_attachments_dev.json": {"a-1": {"kbs": [{"id": "KB12345678"}], "name": "a"}},
             "skills_dev.json": {"loyalty-reply-style": {"current": "v0001"}}, "users.json": {}, "keys.json": {},
             "jobs/kb-0123456789.json": {"id": "kb-0123456789", "kind": "kb", "workspace": "dev", "status": "succeeded", "label": "知识库",
                                         "params": {}, "createdAt": "2026-10-01T13:39:16Z", "log": [], "progress": {}, "result": {}, "error": None},
             "autopilot/autopilot-0123456789/brief.md": "a brief"}
    for rel, value in files.items():
        (source / "console" / rel).write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    (source / "console" / "workspaces.json.tmp").write_text("half-written")
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*") if p.is_file()}
    (kirocrew.data / "console" / "jobs").mkdir(parents=True)  # empty directories are no state
    record = T.console_mod.import_state(source, kirocrew.data)
    assert record["files"] == len(files) and record["jobs"] == 1 and {"workspaces", "kb_attachments_dev", "skills_dev", "users", "keys"} <= set(record["collections"])
    assert not (kirocrew.data / "console" / "workspaces.json.tmp").exists() and (kirocrew.data / "console" / ".imported.json").is_file()
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*") if p.is_file()} == before  # only read
    with pytest.raises(FileExistsError):
        T.console_mod.import_state(source, kirocrew.data)  # once
    gw = kirocrew.gw
    status, listed, _ = gw.call("GET", "/api/console/workspaces")
    assert status == 200 and [w["id"] for w in listed["workspaces"]] == ["dev"]  # what the App's console now shows
    assert gw.call("GET", "/api/console/jobs/kb-0123456789")[1]["status"] == "succeeded"
    with pytest.raises(ValueError):
        T.console_mod.import_state(tmp_path / "nothing-here", tmp_path / "elsewhere")


# -- the public API's own local port -------------------------------------------------------------------------------------

def _public(port: int, method: str, path: str, body=None, headers=None) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json", **(headers or {})})
    return conn.getresponse()


def test_the_public_api_has_a_local_port_of_its_own_behind_its_keys_and_its_streams_stream(kirocrew):
    """KiroCrew's gateway admits only a signed-in browser and cuts every request at 30 s: /v1 is on the App backend's
    own 127.0.0.1 port, behind the console's X-Api-Key, the same API as the standalone server's, streams included."""
    gw, fake = kirocrew.gw, kirocrew.fake
    _workspace(gw)
    public = backend.start_public_api(kirocrew.server.console, 0)
    try:
        port = public.server_address[1]
        assert kirocrew.server.console.public_api == {"listening": True, "port": port, "url": f"http://127.0.0.1:{port}/v1", "error": None}
        assert _public(port, "GET", "/v1/agents").status == 401  # no key
        status, issued, _ = gw.call("POST", "/api/console/keys", {"workspace": "dev", "label": "a script"})
        assert status == 201, issued
        key = {"X-Api-Key": issued["key"]}
        listed = _public(port, "GET", "/v1/agents", headers=key)
        assert listed.status == 200 and [a["name"] for a in json.loads(listed.read())["agents"]] == ["hr_agent", "byoc_agent"]
        assert _public(port, "GET", "/api/console/me", headers=key).status == 404  # only the public API on this port
        assert _public(port, "GET", "/v1/agents", headers={**key, "Host": "rebound.example:8772"}).status == 403  # no DNS rebinding
        held = fake.rt = HeldRuntime()
        response = _public(port, "POST", "/v1/chat", {"agent": "hr_agent", "message": "How many days of leave?", "stream": True}, headers=key)
        assert response.status == 200 and response.headers["Transfer-Encoding"] == "chunked"
        seen = b""
        while b"Annual leave is " not in seen:
            seen += response.read1(65536)
        assert not held.go_on.is_set()  # the first words came while the rest of the answer was still held
        held.go_on.set()
        assert [e["type"] for e in _events((seen + response.read()).decode())][-1] == "stop"
        whole = _public(port, "POST", "/v1/chat", {"agent": "hr_agent", "message": "How many days of leave?"}, headers=key)
        assert whole.status == 200 and json.loads(whole.read())["text"]
        info = gw.call("GET", "/api/console/host")[1]
        assert info["publicApi"]["url"] == f"http://127.0.0.1:{port}/v1"  # what the API 密钥 page shows
    finally:
        public.shutdown()
        public.server_close()


def test_a_public_api_port_that_does_not_open_says_why(kirocrew):
    taken = backend.start_public_api(kirocrew.server.console, 0)
    try:
        other = backend.ConsoleMount(kirocrew.server.console.service)
        assert backend.start_public_api(other, taken.server_address[1]) is None
        assert other.public_api["listening"] is False and "Address already in use" in other.public_api["error"]
        off = backend.ConsoleMount(kirocrew.server.console.service)
        assert backend.start_public_api(off, -1) is None and "turned off" in off.public_api["error"]
    finally:
        taken.shutdown()
        taken.server_close()
