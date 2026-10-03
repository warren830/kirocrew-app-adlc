"""Local end-to-end exercise of the whole SA flow — no AWS, no KiroCrew trust grant needed.

What it drives (all real code paths, not mocks):

A. The app backend as the gateway launches it: ``app/backend/server.py`` under the KiroCrew-bundled
   Python (which lacks jsonschema → the process must re-exec into the checkout's .venv), with
   ``KIROCREW_PROXY_SECRET`` set so every request must carry a valid signature.
   create customer project → gate blocks → confirm items with evidence → validate → build → export →
   calibrate → golden (holdout hidden) → target (names only) → preflight refused without a usable
   AWS profile → apply refused without a token → unsigned request refused.
B. The host applier on a fake EC2 target (file:// bundles, real contract file): apply HR fallback →
   commit → apply the customer release → rollback (HR is back) → apply + commit again → tampered
   bundle refused → status.
C. Engine CLI verify-release for the three committed packs.

Usage: ``.venv/bin/python tools/e2e_local.py`` — writes ``build/e2e-report.md`` and exits non-zero on
any failed step.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))
from workshop_customizer import sync as engine_sync  # noqa: E402
from workshop_customizer.compiler import compile_pack  # noqa: E402
from workshop_customizer.render import render_release  # noqa: E402
from workshop_customizer.scenario import load_scenario  # noqa: E402

GATEWAY_PY = Path("/Applications/KiroCrew.app/Contents/Resources/backend-dist/kirocrew-backend-arm64/bin/python3")
APP = "workshop-customizer"
PREFIX = f"/api/apps/{APP}"
SECRET = "e2e-proxy-secret"
REPORT = REPO / "build" / "e2e-report.md"

steps: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    steps.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Api:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"

    def call(self, method: str, path: str, body=None, *, sign=True, raw=False):
        data = json.dumps(body).encode() if body is not None else b""
        headers = {"Content-Type": "application/json"}
        if sign:
            ts = int(time.time())
            msg = f"{ts}:{method}:{PREFIX + path}:{hashlib.sha256(data).hexdigest()}"
            headers["X-KiroCrew-Proxy"] = f"{ts}:{hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()}"
        req = urllib.request.Request(self.base + PREFIX + path, method=method, data=data if body is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                payload = r.read()
                return r.status, payload if raw else json.loads(payload)
        except urllib.error.HTTPError as e:
            payload = e.read()
            return e.code, payload if raw else json.loads(payload)


def phase_a(scratch: Path) -> dict:
    data_dir = scratch / "appdata"
    data_dir.mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps({"homeDir": str(REPO)}))
    port = free_port()
    py = GATEWAY_PY if GATEWAY_PY.is_file() else Path(sys.executable)
    env = {**os.environ, "PORT": str(port), "KIROCREW_APP_NAME": APP, "KIROCREW_PROXY_SECRET": SECRET, "WORKSHOP_CUSTOMIZER_DATA": str(data_dir)}
    log = (scratch / "backend.log").open("w")
    proc = subprocess.Popen([str(py), str(REPO / "app" / "backend" / "server.py")], env=env, stdout=log, stderr=subprocess.STDOUT)
    api = Api(port)
    out: dict = {}
    try:
        for _ in range(40):
            try:
                status, health = api.call("GET", "/health")
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.25)
        else:
            step("A0 backend starts", False, "no /health within 10s")
            return out
        step("A0 backend starts like the gateway launches it", status == 200 and health["engine"] is True, f"python={Path(health['python']).relative_to(REPO) if health['python'].startswith(str(REPO)) else health['python']} engine={health['engine']}")
        step("A1 unsigned data request is refused, unsigned gateway health probe allowed", api.call("GET", "/projects", sign=False)[0] == 401 and api.call("GET", "/health", sign=False)[0] == 200)

        status, meta = api.call("POST", "/projects", {"id": "globex-hr", "template": "hr-default", "packKind": "customer", "displayName": "Globex HR", "customer": "Globex"})
        step("A2 create customer project from hr-default", status == 201 and meta["status"] == "intake")

        status, v = api.call("POST", "/projects/globex-hr/validate")
        step("A3 provenance gate blocks an unconfirmed customer pack", status == 200 and v["ok"] is False and len(v["gate"]) > 0, f"{len(v['gate'])} gate violations")
        status, b = api.call("POST", "/projects/globex-hr/build")
        step("A4 build refused while blocked (fail closed)", status == 409)

        scenario = yaml.safe_load(api.call("GET", "/projects/globex-hr")[1]["scenarioYaml"])
        items = [i["id"] for i in scenario.get("facts", [])] + [t["name"] for t in scenario.get("tools", [])] + [d["id"] for d in scenario["knowledge"]["documents"]] + [c["id"] for c in scenario["evaluation"]["goldenSet"]]
        confirmed = 0
        for item_id in items:
            s, _ = api.call("POST", f"/projects/globex-hr/items/{item_id}/confirm", {"provenance": "customer_confirmed", "confirmedBy": "Jane Doe (Globex HR Director)", "confirmationRef": "intake call notes 2026-09-01"})
            confirmed += s == 200
        step("A5 record customer confirmations with evidence", confirmed == len(items), f"{confirmed}/{len(items)} items")
        data = yaml.safe_load(api.call("GET", "/projects/globex-hr")[1]["scenarioYaml"])
        step("A6 tool provenance recorded through confirmation endpoint", all(t["provenance"] == "customer_confirmed" for t in data["tools"]))

        status, v = api.call("POST", "/projects/globex-hr/validate")
        step("A7 validation passes after confirmation", status == 200 and v["ok"] is True, json.dumps(v.get("summary")) if v.get("ok") else json.dumps((v.get("gate") or v.get("schema") or v.get("policy"))[:3]))
        status, b = api.call("POST", "/projects/globex-hr/build")
        step("A8 build release", status == 200, f"{b.get('version')} files={b.get('files')} patches={b.get('patches')}" if status == 200 else json.dumps(b)[:300])
        out["version"] = b.get("version")
        status, blob = api.call("GET", "/projects/globex-hr/export", raw=True)
        names = zipfile.ZipFile(io.BytesIO(blob)).namelist() if status == 200 else []
        step("A9 export zip is the verified release without instructor files", status == 200 and "RELEASE.json" in names and not any(n.startswith("instructor/") for n in names), f"{len(names)} entries, {len(blob)} bytes")
        (scratch / "export.zip").write_bytes(blob)

        log_text = "    value = 0.83\n    value = 0.80\n    value = 0.86\n"
        s1, dry = api.call("POST", "/projects/globex-hr/calibrate", {"log": log_text, "dryRun": True})
        s2, applied = api.call("POST", "/projects/globex-hr/calibrate", {"log": log_text})
        step("A10 judge-noise calibration (dry run, then apply)", s1 == 200 and dry["applied"] is False and s2 == 200 and applied["applied"] is True, f"band={applied.get('noiseBand')} verdict={applied.get('verdict')}")
        s3, b2 = api.call("POST", "/projects/globex-hr/build")
        step("A11 rebuild after calibration yields a new content-derived version", s3 == 200 and b2["version"] != out["version"], f"{out['version']} → {b2['version']}")
        out["version"] = b2["version"]
        out["release_dir"] = b2["releaseDir"]

        g = api.call("GET", "/projects/globex-hr/golden")[1]
        gi = api.call("GET", "/projects/globex-hr/golden?instructor=1")[1]
        step("A12 holdout hidden from students, visible to instructor", "holdout" not in g and g["holdoutCount"] == len(gi["holdout"]) > 0, f"practice={len(g['practice'])} holdout={g['holdoutCount']}")

        s, t = api.call("PUT", "/projects/globex-hr/target", {"profile": "e2e-no-such-profile", "region": "us-west-2", "expectedAccountId": "123456789012"})
        step("A13 target stores names only", s == 200 and "secret" not in json.dumps(t).lower())
        s, pf = api.call("POST", "/projects/globex-hr/sync/preflight")
        step("A14 preflight refuses without a usable AWS profile (no AWS touched)", s == 409 and "not usable" in pf.get("error", ""), pf.get("error", "")[:120])
        s, _ = api.call("POST", "/projects/globex-hr/sync/apply", {"confirmToken": "guess"})
        step("A15 apply refused without a preflight token", s == 409)
        s, _ = api.call("PUT", "/projects/globex-hr/target", {"profile": "p", "region": "us-west-2", "expectedAccountId": "123456789012", "secretAccessKey": "x"})
        step("A16 target refuses credential material", s == 400)
        out["data_dir"] = data_dir
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    out["backend_log"] = (scratch / "backend.log").read_text()
    return out


def run_applier(contract: Path, *argv: str) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, str(REPO / "sync" / "host" / "apply_release.py"), "--contract", str(contract), *argv], capture_output=True, text=True)
    try:
        payload = json.loads(proc.stdout[proc.stdout.index("{"):]) if "{" in proc.stdout else {}
    except json.JSONDecodeError:
        payload = {}
    return proc.returncode, payload or {"stderr": proc.stderr[-400:]}


def phase_b(scratch: Path, customer_release: Path | None) -> None:
    import pwd

    bundles = scratch / "bundles"
    target_root = scratch / "workshop"
    hr_release = scratch / "references" / "hr-default" / "release"
    hr_zip, hr_sha = engine_sync.build_bundle(hr_release, bundles / "hr" / "release.zip")
    hr_version = json.loads((hr_release / "RELEASE.json").read_text())["version"]
    contract = {
        "targetRoot": str(target_root),
        "user": pwd.getpwuid(os.getuid()).pw_name,
        "group": "staff",
        "templateCommit": json.loads((REPO / "template-lock.json").read_text())["template"]["commit"],
        "packSchemaVersion": 1,
        "allowedReleasePrefixes": [f"file://{bundles}/"],
        "documentName": "WorkshopCustomizerApplyRelease",
    }
    contract_path = scratch / "target.json"
    contract_path.write_text(json.dumps(contract))

    code, res = run_applier(contract_path, "--action", "apply", "--source", f"file://{hr_zip}", "--sha256", hr_sha, "--version", hr_version, "--no-watchdog")
    step("B1 apply HR fallback release on the fake EC2", code == 0 and res.get("status") == "pending-commit", f"{res.get('status')} smoke={res.get('smoke', {}).get('ok') if isinstance(res.get('smoke'), dict) else res.get('smoke')}")
    code, res = run_applier(contract_path, "--action", "commit", "--version", hr_version)
    step("B2 commit HR fallback", code == 0 and res.get("status") == "committed", res.get("status", ""))

    if customer_release and customer_release.is_dir():
        cust_zip, cust_sha = engine_sync.build_bundle(customer_release, bundles / "cust" / "release.zip")
        cust_version = json.loads((customer_release / "RELEASE.json").read_text())["version"]
        code, res = run_applier(contract_path, "--action", "apply", "--source", f"file://{cust_zip}", "--sha256", cust_sha, "--version", cust_version, "--no-watchdog")
        step("B3 apply customer release (HR becomes `previous`)", code == 0 and res.get("status") == "pending-commit", cust_version)
        code, res = run_applier(contract_path, "--action", "rollback", "--reason", "e2e drill")
        current = os.path.realpath(target_root / "current") if (target_root / "current").exists() else ""
        step("B4 rollback restores the HR fallback by symlink switch", code == 0 and hr_version in current, f"current → {Path(current).name}")
        code, res = run_applier(contract_path, "--action", "apply", "--source", f"file://{cust_zip}", "--sha256", cust_sha, "--version", cust_version, "--no-watchdog")
        code2, res2 = run_applier(contract_path, "--action", "commit", "--version", cust_version)
        step("B5 re-apply + commit customer release", code == 0 and code2 == 0 and res2.get("status") == "committed", res2.get("status", ""))
        bad_sha = "0" * 64
        code, res = run_applier(contract_path, "--action", "apply", "--source", f"file://{cust_zip}", "--sha256", bad_sha, "--version", cust_version, "--no-watchdog")
        step("B6 tampered/mismatched bundle is refused", code != 0, (res.get("error") or res.get("stderr") or "")[:100])
        code, res = run_applier(contract_path, "--action", "apply", "--source", f"file:///etc/release.zip", "--sha256", cust_sha, "--version", cust_version, "--no-watchdog")
        step("B7 source outside the allow-listed prefix is refused", code != 0)
    code, res = run_applier(contract_path, "--action", "status")
    step("B8 status reports the deployment manifest", code == 0 and "current" in json.dumps(res), json.dumps(res)[:160])


def phase_c(scratch: Path) -> None:
    for pack in ("hr-default", "it-helpdesk", "maintenance"):
        proc = subprocess.run([sys.executable, "-m", "workshop_customizer", "verify-release", str(scratch / "references" / pack / "release"), "--json"], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO / "engine")})
        ok = proc.returncode == 0 and '"status": "ok"' in proc.stdout
        version = json.loads(proc.stdout).get("version") if ok else proc.stderr[-200:]
        step(f"C verify-release {pack}", ok, str(version))


def main() -> int:
    scratch = Path(os.environ.get("KIROCREW_SCRATCH", "/tmp")) / "wc-e2e"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    started = datetime.now(timezone.utc)
    commit = json.loads((REPO / "template-lock.json").read_text())["template"]["commit"]
    for pack_id in ("hr-default", "it-helpdesk", "maintenance"):
        destination = scratch / "references" / pack_id
        pack = compile_pack(load_scenario(REPO / "scenarios" / pack_id / "scenario.yaml"),
                            destination / "build", template_commit=commit)
        render_release(pack, REPO / "upstream", destination / "release", template_commit=commit)
    out = phase_a(scratch)
    phase_b(scratch, Path(out["release_dir"]) if out.get("release_dir") else None)
    phase_c(scratch)
    failed = [s for s in steps if not s[1]]
    lines = [
        "# Local end-to-end run — Workshop Customizer",
        "",
        f"Run at {started.strftime('%Y-%m-%d %H:%M:%SZ')} on {socket.gethostname()}; no AWS calls were made; KiroCrew trust grant not required (backend launched directly as the gateway would).",
        "",
        f"Result: **{len(steps) - len(failed)}/{len(steps)} steps passed**" + ("" if not failed else " — FAILURES: " + ", ".join(s[0] for s in failed)),
        "",
        "| Step | Result | Detail |",
        "|---|---|---|",
        *[f"| {n} | {'PASS' if ok else 'FAIL'} | {d.replace('|', '/')} |" for n, ok, d in steps],
        "",
        "## Backend log (as launched by the gateway)",
        "",
        "```",
        out.get("backend_log", "").strip()[:4000],
        "```",
    ]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nreport: {REPORT}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
