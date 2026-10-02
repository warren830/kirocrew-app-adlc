"""Render an App page headless: the real UI and backend on a copy of one project, a screenshot, the page's errors.

    .venv/bin/python3 tools/ui_preview.py --data build/aws-e2e/appdata --project gas-utility --route 4/rehearse --out /tmp/p.png

The UI (app/ui/dist/index.mjs) is hand-written ESM for the KiroCrew host, so no test runner renders it. This
serves it the way the host does: React 18 (UMD builds from ``--react-dir``) registered as the host registers
its shared modules (``window.__kirocrew_modules``), the host's own vendor stubs behind the same import map, a
stand-in ``@kirocrew/app-sdk`` (no Kiro routes: generation reads as none), and the process backend
(app/backend/server.py, never an AWS client) on a copy of the project. Headless Chrome opens the deep link
``#wc=<project>/<route>``, waits ``--wait`` seconds and saves a screenshot. Window errors, rejected promises and
console errors are printed; any of them makes the exit code 1.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI = REPO / "app" / "ui" / "dist" / "index.mjs"
VENDOR = Path("/Applications/KiroCrew.app/Contents/Resources/backend-dist/kirocrew-backend-arm64/lib/python3.12/site-packages/"
              "kiro_crew/static/dist/vendor")
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
#: The old headless mode (Playwright's chrome-headless-shell) honours --virtual-time-budget for screenshots of a
#: page that keeps polling; new-headless Chrome's --screenshot waits for an idle page that never comes.
SHELLS = sorted(Path.home().glob("Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"))
PROCESS_PREFIX = "/apps/workshop-customizer"  # the gateway strips it before the app process sees the path

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>preview</title>
<script src="/umd/react.js"></script><script src="/umd/react-dom.js"></script>
<script>
window.__errors = [];
addEventListener('error', (e) => __errors.push('error: ' + e.message + ' @' + (e.filename || '') + ':' + (e.lineno || '')));
addEventListener('unhandledrejection', (e) => __errors.push('rejection: ' + ((e.reason && e.reason.message) || e.reason)));
const consoleError = console.error;
console.error = (...a) => { __errors.push('console: ' + a.map(String).join(' ').slice(0, 600)); consoleError(...a) };
const icon = (name) => function Icon() { return React.createElement('span', { 'data-icon': name, style: { display: 'inline-block', width: 13 } }) };
const noKiro = async (url) => {
  if (String(url).includes('/generations/latest/')) return { generation: null };
  throw Object.assign(new Error('no Kiro routes in the preview'), { status: 404 });
};
const appApi = { get: noKiro, post: noKiro, put: noKiro, delete: noKiro };
const notify = { error: (m) => console.warn('notify.error', m), success: (m) => console.log('notify.success', m) };
window.__kirocrew_modules = {
  react: React, 'react-dom': ReactDOM,
  'lucide-react': new Proxy({}, { get: (_, p) => (typeof p === 'string' ? icon(p) : undefined) }),
  '@kirocrew/app-sdk': { useAppApi: () => appApi, useNotify: () => notify },
};
</script>
<script type="importmap">{"imports": {"react": "/vendor/react.mjs", "react-dom/client": "/vendor/react-dom-client.mjs",
  "@kirocrew/app-sdk": "/vendor/kirocrew-app-sdk.mjs", "@kirocrew/app-sdk/ui": "/vendor/kirocrew-ui.mjs", "lucide-react": "/vendor/lucide-react.mjs"}}</script>
</head><body style="margin:0"><div id="root" style="height:100vh;display:flex;flex-direction:column"></div>
<script type="module">
import App from '/app/index.mjs';
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(App));
setTimeout(() => {
  const pre = document.createElement('pre'); pre.id = '__errors'; pre.textContent = JSON.stringify(window.__errors);
  document.body.appendChild(pre);
}, __WAIT_MS__);
</script></body></html>"""


def _load_server():
    spec = importlib.util.spec_from_file_location("wc_app_server_preview", REPO / "app" / "backend" / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _no_aws(_cfg):
    raise RuntimeError("the preview never creates AWS clients")


def serve(backend_port: int, react_dir: Path, wait_ms: int) -> ThreadingHTTPServer:
    files = {"/umd/react.js": react_dir / "react" / "umd" / "react.development.js",
             "/umd/react-dom.js": react_dir / "react-dom" / "umd" / "react-dom.development.js", "/app/index.mjs": UI}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # quiet
            pass

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _proxy(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length) if length else None
            req = urllib.request.Request(f"http://127.0.0.1:{backend_port}{self.path[len(PROCESS_PREFIX):]}", data=data, method=self.command,
                                         headers={"Content-Type": self.headers.get("Content-Type") or "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    self._send(resp.status, resp.read(), resp.headers.get("Content-Type") or "application/json")
            except urllib.error.HTTPError as exc:
                self._send(exc.code, exc.read(), exc.headers.get("Content-Type") or "application/json")

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path.startswith(PROCESS_PREFIX + "/"):
                return self._proxy()
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.replace("__WAIT_MS__", str(wait_ms)).encode(), "text/html; charset=utf-8")
            target = files.get(path) or (VENDOR / path[len("/vendor/"):] if path.startswith("/vendor/") else None)
            if target is None or not target.is_file():
                return self._send(404, b"not found", "text/plain")
            kind = "text/javascript; charset=utf-8" if target.suffix in (".js", ".mjs") else "application/octet-stream"
            return self._send(200, target.read_bytes(), kind)

        do_POST = do_PUT = do_DELETE = _proxy  # noqa: N815 - the UI's writes go to the backend

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", type=Path, required=True, help="an App data dir (its projects/<id> is copied, never changed)")
    parser.add_argument("--project", required=True)
    parser.add_argument("--route", default="4/rehearse", help="<step>/<tab> after the project id (default 4/rehearse)")
    parser.add_argument("--out", type=Path, required=True, help="the screenshot (.png)")
    parser.add_argument("--size", default="1440,2600", help="window size (default 1440,2600)")
    parser.add_argument("--wait", type=float, default=6.0, help="seconds to let the page load and settle (default 6)")
    parser.add_argument("--react-dir", type=Path, default=Path(os.environ.get("UI_PREVIEW_REACT_DIR", "node_modules")),
                        help="a node_modules with react and react-dom 18 (their umd/ builds; default $UI_PREVIEW_REACT_DIR or ./node_modules)")
    args = parser.parse_args(argv)

    if not VENDOR.is_dir() or not (SHELLS or CHROME.is_file()):
        print(f"needs the KiroCrew app ({VENDOR}) and Google Chrome ({CHROME})", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="wc-preview-") as tmp:
        data = Path(tmp) / "data"
        shutil.copytree(args.data / "projects" / args.project, data / "projects" / args.project,
                        ignore=shutil.ignore_patterns("*.zip", "live", "__pycache__"))
        server_mod = _load_server()
        backend, _service = server_mod.create_server(data, home=REPO, clients_factory=_no_aws)
        threading.Thread(target=backend.serve_forever, daemon=True).start()
        front = serve(backend.server_address[1], args.react_dir, int(args.wait * 1000))
        url = f"http://127.0.0.1:{front.server_address[1]}/#wc={args.project}/{args.route}"
        browser = str(SHELLS[-1]) if SHELLS else str(CHROME)
        chrome = [browser, *([] if SHELLS else ["--headless=new"]), "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                  "--hide-scrollbars", f"--user-data-dir={tmp}/chrome", f"--window-size={args.size}"]
        budget = f"--virtual-time-budget={int(args.wait * 1000) + 2000}"
        try:
            dom = subprocess.run(chrome + [budget, "--dump-dom", url], capture_output=True, text=True, timeout=120).stdout
            subprocess.run(chrome + [budget, f"--screenshot={args.out.resolve()}", url], capture_output=True, text=True, timeout=120)
        finally:
            front.shutdown()
            backend.shutdown()
    marker = dom.find('<pre id="__errors">')
    errors = json.loads(_unescape(dom[marker + len('<pre id="__errors">'): dom.find("</pre>", marker)])) if marker >= 0 else ["the page never finished"]
    errors = [e for e in errors if "ReactDOM.render is no longer supported" not in e and "importing createRoot from" not in e]
    print(f"screenshot: {args.out}")
    for error in errors:
        print(f"  ! {error}")
    return 1 if errors else 0


def _unescape(text: str) -> str:
    import html

    return html.unescape(text)


if __name__ == "__main__":
    raise SystemExit(main())
