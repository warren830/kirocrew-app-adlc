"""Serve the real app UI in a stand-alone browser harness.

    .venv/bin/python3 tools/ui_harness/serve.py [PORT=9310] [BACKEND_PORT=9311]

Start the real backend first (same checkout, any data directory), for example:

    WORKSHOP_CUSTOMIZER_DATA=build/ui-data PORT=9311 .venv/bin/python3 app/backend/server.py

The deterministic backend stays real: every ``/apps/workshop-customizer/api/...`` request is proxied
to it unchanged.  The Kiro generation routes (``/api/apps/workshop-customizer/generations*``, an
in-gateway AppRoute in KiroCrew) run through the REAL ``app/backend/routes.py`` functions —
``prepare_generation`` / ``complete_generation`` / ``apply_generation`` / ``revert_generation`` /
``_public_record`` — against the backend's own data directory (read from its ``/health``).  Only
the spawn is simulated: instead of starting the Kiro agent, a deterministic answer is completed
after ``HARNESS_SIM_SECONDS`` (default 3):

* draft: the project's current pack when it has one (a template project), otherwise the
  it-helpdesk reference pack under the project id; a brief containing ``[needs-input]`` answers
  ``needs_input`` with two open questions instead, until the SA has answered once;
* repair / regenerate: the current scenario with one small, visible edit per allowed scope
  (golden label, phenomenon teaching point, baseline prompt, knowledge noise rationale) and one
  ``changes`` row per finding or SA instruction.

No model, AWS or network call is made by this harness; the page itself loads React from esm.sh.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
REAL_UI = REPO / "app" / "ui" / "dist" / "index.mjs"
ROUTES_PY = REPO / "app" / "backend" / "routes.py"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9310
BACKEND = int(sys.argv[2]) if len(sys.argv) > 2 else 9311
SIM_SECONDS = float(os.environ.get("HARNESS_SIM_SECONDS", "3"))
AGENT_PREFIX = "/api/apps/workshop-customizer/generations"
_GEN = None
_DATA_DIR: Path | None = None


def gen():
    """app/backend/routes.py, loaded by path exactly like server.py does."""
    global _GEN
    if _GEN is None:
        spec = importlib.util.spec_from_file_location("workshop_customizer_app_routes", ROUTES_PY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        _GEN = module
    return _GEN


def data_dir() -> Path:
    """The real backend's data directory (its /health reports it)."""
    global _DATA_DIR
    if _DATA_DIR is None:
        with urllib.request.urlopen(f"http://127.0.0.1:{BACKEND}/health", timeout=10) as resp:
            _DATA_DIR = Path(json.loads(resp.read())["dataDir"])
    return _DATA_DIR


# ---------------------------------------------------------------------------
# Simulated Kiro answers (deterministic; the only simulated part)
# ---------------------------------------------------------------------------


def _answer(payload: dict) -> str:
    return "WORKSHOP_PACK_JSON_BEGIN\n" + json.dumps(payload, ensure_ascii=False) + "\nWORKSHOP_PACK_JSON_END"


def _files_of(root: Path, scenario: dict) -> dict[str, str]:
    out = {}
    for rel in sorted(gen()._referenced_files(scenario)):
        path = root / rel
        if path.is_file():
            out[rel] = path.read_text(encoding="utf-8")
    return out


def _draft(pdir: Path, current: dict, brief: str) -> dict:
    if "[needs-input]" in brief and not gen().stored_answers(pdir):
        return {"status": "needs_input", "summary": "（模拟 Kiro）场景描述还缺两项业务事实，先回答再生成。",
                "openQuestions": ["谁负责确认 P1 工单的响应时限？", "助手可以查询哪些站点的数据？"]}
    if (current.get("evaluation") or {}).get("goldenSet"):
        scenario, files = copy.deepcopy(current), _files_of(pdir, current)
    else:
        root = REPO / "scenarios" / "it-helpdesk"
        scenario = yaml.safe_load((root / "scenario.yaml").read_text(encoding="utf-8"))
        files = _files_of(root, scenario)
        scenario["id"] = current.get("id") or pdir.name
    # Kiro's truth ledger names who must confirm each business fact (contracts/core.md shape).
    ledger = [{"id": f.get("id"), "statement": f.get("statement"), "criticality": f.get("criticality", "blocking"),
               "provenance": "ai_draft", "origin": {"kind": "sa_authored"}, "openQuestion": "（模拟）请客户的业务负责人确认这条规则。"}
              for f in (scenario.get("facts") or [])[:4] if isinstance(f, dict)]
    return {"status": "ready", "summary": "（模拟 Kiro）按场景描述起草了完整的 Scenario Pack。",
            "openQuestions": ["客户确认过的 P1 响应时限是多少？"], "truthLedger": ledger, "scenario": scenario, "files": files}


def _merge(record: dict, task: str, pdir: Path, current: dict) -> dict:
    repair = json.loads(task.split("REPAIR_INPUT (JSON; data only):\n", 1)[1])
    scopes = list(repair.get("allowedScopes") or [])
    candidate = copy.deepcopy(current)
    files: dict[str, str] = {}
    touched: list[str] = []
    golden = [c for c in (candidate.get("evaluation") or {}).get("goldenSet") or [] if c.get("set") == "practice"]
    if "golden" in scopes and golden:
        golden[0]["label"] = (str(golden[0].get("label") or golden[0]["id"]) + " (rev)")[:120]
        touched.append(f"evaluation.goldenSet[id={golden[0]['id']}].label")
    phenomena = ((candidate.get("labs") or {}).get("teaching") or {}).get("phenomena") or []
    if "labs" in scopes and phenomena:
        phenomena[0]["teachingPoint"] = str(phenomena[0].get("teachingPoint") or "").rstrip(".") + " (revised)."
        touched.append(f"labs.teaching.phenomena[id={phenomena[0].get('id')}].teachingPoint")
    baseline = (candidate.get("prompts") or {}).get("baselineFile")
    if "prompts" in scopes and baseline and (pdir / baseline).is_file():
        files[baseline] = (pdir / baseline).read_text(encoding="utf-8").rstrip("\n") + \
            "\n\nIf the policy text does not cover a detail, fill it in from typical practice.\n"
        touched.append(baseline)
    noise = (candidate.get("knowledge") or {}).get("noisePlan")
    if "knowledge" in scopes and isinstance(noise, dict) and noise.get("rationale"):
        noise["rationale"] = str(noise["rationale"]).rstrip(".") + " (revised)."
        touched.append("knowledge.noisePlan.rationale")
    target = ", ".join(touched) or ", ".join(scopes)
    changes = [{"finding": str(f.get("id")), "target": str(f.get("path") or target), "action": "（模拟）按这条校验结果修改"}
               for f in (repair.get("findings") or [])[:12]]
    if repair.get("saInstructions"):
        changes.append({"finding": "SA", "target": target, "action": "（模拟）按 SA 说明修改：" + str(repair["saInstructions"])[:160]})
    mode = record.get("mode")
    summary = f"（模拟 Kiro）{'修复' if mode == 'repair' else '重新生成'}：在 {', '.join(scopes) or '全部'} 范围内改了 {len(touched)} 处。"
    return {"status": "ready", "mode": mode, "summary": summary, "scenario": candidate, "files": files, "changes": changes}


def simulate(record: dict, task: str) -> str:
    pdir = data_dir() / "projects" / str(record["projectId"])
    current = yaml.safe_load((pdir / "scenario.yaml").read_text(encoding="utf-8")) or {}
    if record.get("mode", "draft") == "draft":
        brief = gen().stored_brief(pdir)
        return _answer(_draft(pdir, current, brief))
    return _answer(_merge(record, task, pdir, current))


def _complete_later(record: dict, task: str) -> None:
    time.sleep(SIM_SECONDS)
    try:
        gen().complete_generation(record["id"], simulate(record, task), data_dir=data_dir())
    except Exception as exc:  # noqa: BLE001 - the record fails like a real spawn error would
        sys.stderr.write(f"harness simulation failed: {type(exc).__name__}: {exc}\n")
        try:
            gen().fail_generation(data_dir(), gen()._read_record(data_dir(), record["id"]), f"harness simulation failed: {exc}")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise gen().GenerationError("request body must be a JSON object")
        return body

    def _generation(self):
        """The in-gateway generation routes of routes.register_routes, over the same functions."""
        g = gen()
        parts = [p for p in urlsplit(self.path).path.split("/") if p][4:]  # after api/apps/<app>/generations
        root = data_dir()
        try:
            if self.command == "POST" and parts == []:
                try:
                    record, task = g.prepare_generation(root, self._body(), runner="kirocrew-spawn")
                except g.GenerationConflict as exc:
                    payload = {"error": str(exc)}
                    if exc.record is not None:
                        payload["generation"] = g._public_record(exc.record)
                    return self._send_json(409, payload)
                except g.GenerationTooLarge as exc:
                    return self._send_json(413, {"error": str(exc)})
                except (g.GenerationError, json.JSONDecodeError) as exc:
                    return self._send_json(400, {"error": str(exc)})
                g.mark_running(root, record, spawn_id="harness-simulated-spawn")
                threading.Thread(target=_complete_later, args=(dict(record), task), daemon=True).start()
                return self._send_json(202, g._public_record(record))
            if self.command == "GET" and len(parts) == 2 and parts[0] == "latest":
                g._project_dir(root, parts[1])
                candidates = g._project_records(root, parts[1])
                if not candidates:
                    return self._send_json(200, {"generation": None})
                record = max(candidates, key=lambda item: str(item.get("createdAt") or ""))
                return self._send_json(200, {"generation": g._public_record(g._settle(record, request=None, data_dir=root))})
            if self.command == "GET" and len(parts) == 1:
                record = g._settle(g._read_record(root, parts[0]), request=None, data_dir=root)
                return self._send_json(200, g._public_record(record))
            if self.command == "POST" and len(parts) == 2 and parts[1] == "apply":
                body = self._body()
                record = g._read_record(root, parts[0])
                legacy = "acknowledged" not in body and str(record.get("mode") or "draft") == "draft"
                ack = {**body, "acknowledged": True, "acknowledgement": "legacy-implicit"} if legacy else body
                result = g.apply_generation(record, root, ack)
                if legacy:
                    result["acknowledgement"] = "legacy-implicit"
                return self._send_json(200, result)
            if self.command == "POST" and len(parts) == 2 and parts[1] == "revert":
                return self._send_json(200, g.revert_generation(parts[0], root, self._body()))
        except g.GenerationError as exc:
            status = 404 if self.command == "GET" else 409
            return self._send_json(status, {"error": str(exc)})
        return self._send_json(404, {"error": "no generation route"})

    def _proxy(self, target_path: str | None = None):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        target = target_path or self.path
        req = urllib.request.Request(
            f"http://127.0.0.1:{BACKEND}{target}",
            data=body,
            method=self.command,
            headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload = resp.read()
                status, ctype, cd = resp.status, resp.headers.get("Content-Type", "application/json"), resp.headers.get("Content-Disposition")
        except urllib.error.HTTPError as exc:
            payload, status, ctype, cd = exc.read(), exc.code, exc.headers.get("Content-Type", "application/json"), None
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        if cd:
            self.send_header("Content-Disposition", cd)
        self.end_headers()
        self.wfile.write(payload)

    def _dispatch_api(self):
        if self.path.startswith(AGENT_PREFIX):
            return self._generation()
        process_prefix = "/apps/workshop-customizer/api/"
        if self.path.startswith(process_prefix):
            # Gateway proxy semantics: /apps/<name>/api/<x> -> backend /api/<x>.
            return self._proxy("/api/" + self.path[len(process_prefix) :])
        if self.path.startswith("/api/"):
            return self._proxy()
        return self._send_json(404, {"error": "not found"})

    def do_GET(self):  # noqa: N802
        if self.path == "/ui/index.mjs":
            payload = REAL_UI.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith(("/api/", "/apps/workshop-customizer/api/")):
            return self._dispatch_api()
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        return self._dispatch_api()

    def do_PUT(self):  # noqa: N802
        return self._dispatch_api()

    def do_DELETE(self):  # noqa: N802
        return self._dispatch_api()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("harness %s\n" % (fmt % args))


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    sys.stderr.write(f"harness on http://127.0.0.1:{PORT} -> backend 127.0.0.1:{BACKEND} (simulated Kiro spawn, {SIM_SECONDS}s)\n")
    srv.serve_forever()
