"""Run the real backend HTTP workflow against an explicitly selected AWS account.

Example:
  .venv/bin/python tools/e2e_aws.py prepare --profile default --account 123456789012
  .venv/bin/python tools/e2e_aws.py apply --profile default --account 123456789012
  .venv/bin/python tools/e2e_aws.py run --profile default --account 123456789012
  .venv/bin/python tools/e2e_aws.py rehearse --profile default --account 123456789012
  .venv/bin/python tools/e2e_aws.py prepare --project aws-ops-support --template maintenance ...

Infrastructure must exist first. This runner persists the app data under build/aws-e2e/appdata and the
evidence of each project under build/aws-e2e/<project>/ (build/aws-e2e/ itself for the original
aws-it-helpdesk project), resumes existing command IDs, stops on failures, and never invokes cleanup.
``rehearse`` computes and saves run/rehearsal.json through the app (POST run/rehearsal); it exits 0 when
the pack is ready for class, 2 insufficient evidence, 3 not ready, 4 verdict ready but not ready for class.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
#: Shared app data (every e2e project) and the root of the per-project evidence directories.
E2E_ROOT = REPO / "build" / "aws-e2e"
#: The first live project keeps its evidence directly in build/aws-e2e (the files recorded before).
LEGACY_PROJECT = "aws-it-helpdesk"
DEFAULT_TEMPLATE = "it-helpdesk"


def evidence_dir(project: str, root: Path | None = None) -> Path:
    """Where a project's evidence files go: ``build/aws-e2e/<project>/``, or ``build/aws-e2e/`` for aws-it-helpdesk."""
    root = E2E_ROOT if root is None else root
    return root if project == LEGACY_PROJECT else root / project


def templates() -> tuple[str, ...]:
    """The reference packs a live project can start from (scenarios/<id>/scenario.yaml)."""
    return tuple(sorted(p.parent.name for p in (REPO / "scenarios").glob("*/scenario.yaml")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("prepare", "apply", "run", "status", "reset", "rehearse"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--project", default=LEGACY_PROJECT)
    parser.add_argument("--template", default=DEFAULT_TEMPLATE, choices=templates(),
                        help="For prepare: the reference pack a new project starts from (default: it-helpdesk).")
    parser.add_argument("--stop-after")
    parser.add_argument("--from-step", help="For reset: retain earlier passed steps of the same release.")
    return parser


TRANSIENT_POLL_ERRORS = ("SSLError", "EndpointConnectionError", "ConnectionClosedError", "ReadTimeoutError",
                         "ConnectTimeoutError", "Connection reset")


def poll_with_retry(call, pid: str, *, attempts: int = 6, pause: float = 20.0):
    """POST run/poll, retrying a transient network error between this workstation and SSM.

    Live 2026-09-28: one poll failed with an SSL EOF while the step kept running on the EC2; the
    driver used to abort the whole run. The step state lives in the App, so a later poll resumes it.
    """
    for attempt in range(1, attempts + 1):
        try:
            return call("POST", pid + "/run/poll", {})
        except RuntimeError as exc:
            if attempt == attempts or not any(marker in str(exc) for marker in TRANSIENT_POLL_ERRORS):
                raise
            print(f"transient poll error ({attempt}/{attempts - 1} retries): {str(exc)[:160]}", flush=True)
            time.sleep(pause)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    E2E_ROOT.mkdir(parents=True, exist_ok=True)
    evidence = evidence_dir(args.project)
    evidence.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("workshop_live_server", REPO / "app/backend/server.py")
    server_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server_module)
    proxy_secret = secrets.token_urlsafe(32)
    server, _ = server_module.create_server(E2E_ROOT / "appdata", home=REPO, proxy_secret=proxy_secret)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/api/apps/workshop-customizer"
    pid = f"/projects/{args.project}"

    def call(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        signature = server_module.sign_for_tests(proxy_secret, method, server_module.ROUTE_PREFIX + path, data or b"")
        request = urllib.request.Request(base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json", "X-KiroCrew-Proxy": signature})
        try:
            with urllib.request.urlopen(request, timeout=1800) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode()}") from exc

    def save(name, data):
        path = evidence / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    try:
        if args.action == "prepare":
            projects = {project["id"]: project for project in call("GET", "/projects")["projects"]}
            if args.project not in projects:
                display = "IT Helpdesk — live AWS validation" if args.template == DEFAULT_TEMPLATE else f"{args.template} — live AWS validation"
                call("POST", "/projects", {"id": args.project, "template": args.template,
                     "packKind": "reference", "displayName": display})
            elif projects[args.project].get("template") != args.template:
                print(f"note: {args.project} already exists from template {projects[args.project].get('template')}; "
                      f"--template {args.template} applies only to a new project", flush=True)
            call("PUT", pid + "/target", {"profile": args.profile, "region": args.region,
                 "expectedAccountId": args.account, "workshopStack": "workshop-infra",
                 "addonsStack": "workshop-customizer-addons"})
            result = call("POST", pid + "/build", {})
            save("build.json", result)
            print(json.dumps(result, indent=2), flush=True)
        else:
            target = call("GET", pid).get("target") or {}
            if any(target.get(key) != value for key, value in (
                ("profile", args.profile), ("region", args.region),
                ("expected_account_id", args.account),
            )):
                raise RuntimeError("saved target differs from the explicitly requested target; prepare it first")
            if args.action == "apply":
                preflight = call("POST", pid + "/sync/preflight", {})
                save("preflight.json", {key: value for key, value in preflight.items() if key != "confirmToken"})
                if not preflight["ok"]:
                    raise RuntimeError(json.dumps(preflight, ensure_ascii=False))
                applied = call("POST", pid + "/sync/apply", {"confirmToken": preflight["confirmToken"]})
                save("apply.json", applied)
                print(json.dumps(applied, indent=2), flush=True)
                if applied["status"] not in ("pending-commit", "no-op"):
                    raise RuntimeError("release did not apply successfully")
                confirmed = call("POST", pid + "/sync/commit", {})
                save("confirm.json", confirmed)
                if (confirmed.get("applier") or {}).get("status") != "committed":
                    raise RuntimeError("release confirmation failed")
                print("Release confirmed.", flush=True)
            elif args.action == "reset":
                print(json.dumps(call("POST", pid + "/run/reset", {"fromStep": args.from_step}), indent=2), flush=True)
            elif args.action == "rehearse":
                from workshop_customizer.rehearsal import exit_code  # the engine is on sys.path once the server loaded it

                rehearsal = call("POST", pid + "/run/rehearsal", {})
                save("rehearsal.json", rehearsal)
                print(json.dumps({key: rehearsal.get(key) for key in ("verdict", "reasonCode", "readyForClass", "readiness")}
                                 | {"phenomena": {p["id"]: p["verdict"] for p in rehearsal.get("phenomena") or []},
                                    "remediation": [f"{h['id']} {h['code']}: {h['action']}" for h in rehearsal.get("remediation") or []]},
                                 indent=2, ensure_ascii=False), flush=True)
                return exit_code(rehearsal)
            else:
                state = call("GET", pid + "/run")
                if args.action == "status":
                    print(json.dumps(state, indent=2, ensure_ascii=False), flush=True)
                    return 0
                while True:
                    if state["status"] == "passed":
                        report = call("GET", pid + "/run/report")
                        save("report.json", report)
                        print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)
                        return 0 if report["status"] == "complete" else 2
                    if not state.get("currentStepId"):
                        for step_id, previous in state["steps"].items():
                            if previous["status"] in ("passed", "failed") and previous.get("commandId"):
                                save(f"steps/{step_id}-{previous['commandId']}.json", previous)
                        state = call("POST", pid + "/run/next", {})
                        step_id = state["currentStepId"]
                        print(f"START {step_id}: {state['steps'][step_id]['commandId']}", flush=True)
                    step_id = state["currentStepId"]
                    time.sleep(10)
                    state = poll_with_retry(call, pid)
                    save("run-state.json", state)
                    step = state["steps"][step_id]
                    print(f"{step_id}: {step['status']} ({step['ssmStatus']})", flush=True)
                    if step["status"] in ("passed", "failed"):
                        save(f"steps/{step_id}-{step['commandId']}.json", step)
                    if step["status"] == "failed":
                        print(json.dumps(step, indent=2, ensure_ascii=False), flush=True)
                        return 1
                    if step["status"] == "passed" and step_id == args.stop_after:
                        return 0
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
