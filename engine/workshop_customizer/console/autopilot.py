"""Autopilot: from a customer brief to a class-ready release, as one console job.

The job runs the platform's own autonomous loop, ``tools/kiro_generate.py --loop``, as a subprocess of the console:
Kiro writes the whole scenario pack from the brief through the local kiro-cli, the fail-closed gates validate it and
Kiro repairs what they find, the release is built, the local pre-rehearsal predicts the class (Bedrock, minutes), and
direct mode deploys the release straight on AgentCore in the workspace (``direct_<agent>``, tagged
``adlc:mode=direct``) and rehearses it N rounds, Kiro repairing from the rehearsal's remediation until every round
holds. The project is written into the console's workshop data, so the 工作坊 page shows it; its Guided Run on the
Workshop stack stays the class's last check. Live 2026-10-01 the gas-utility brief went this way to a class-ready
release in 50.6 minutes of loop with no human step in the content (``docs/platform/evidence/autonomous-gas-utility-…``).

It needs the local kiro-cli, logged in, and a workspace reached through a local AWS profile (the loop is a separate
process that runs for about an hour: assumed spoke-role credentials would expire under it). Launchpad has no
counterpart: every step of its loop is a click.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[3]
LOOP = REPO / "tools" / "kiro_generate.py"
#: The KiroCrew App's own data (tools/kiro_generate.py's LIVE_DATA_DIR). The loop writes there only when told to (SPEC
#: invariant 6). Inside KiroCrew it is the console's workshop data, and an admin starting Autopilot from the App's own
#: page is that explicit request.
LIVE_APP_DATA = Path.home() / ".kiro" / "crew" / "apps" / "workshop-customizer" / "data"
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{1,47}$")
#: What the job keeps of the loop's report (the rest stays in its out dir).
SUMMARY_KEYS = ("projectId", "converged", "exit", "seconds", "build", "error")


class AutopilotError(ValueError):
    pass


def check(body: Mapping[str, Any]) -> dict[str, Any]:
    pid = str(body.get("projectId") or "").strip()
    if not PROJECT_ID.match(pid):
        raise AutopilotError("projectId: lowercase letters, digits and hyphens, starting with a letter (2-48)")
    brief = str(body.get("brief") or "").strip()
    if len(brief) < 200 or len(brief) > 60_000:
        raise AutopilotError("brief: the customer's situation in 200 to 60 000 characters (the users, their questions, the systems and the rules)")
    direct = body.get("direct", True) is not False
    return {"projectId": pid, "displayName": str(body.get("displayName") or pid)[:80], "customer": str(body.get("customer") or "")[:80], "brief": brief,
            "packKind": "reference" if body.get("packKind") == "reference" else "customer",
            "maxRounds": max(1, min(int(body.get("maxRounds") or 6), 10)), "preRehearse": body.get("preRehearse", True) is not False,
            "direct": direct, "directRepeat": max(1, min(int(body.get("directRepeat") or 3), 3)) if direct else 0,
            "directRounds": max(1, min(int(body.get("directRounds") or 3), 6)) if direct else 0}


def command(params: Mapping[str, Any], *, brief: Path, data_dir: Path, out: Path, workspace: Mapping[str, Any]) -> list[str]:
    """The loop's command line, exactly as a person would run it."""
    from ..direct.aws import tools_python  # the checkout's own interpreter: the loop and every script it runs need its packages

    cmd = [tools_python(), str(LOOP), "--loop", "--brief", str(brief), "--project-id", params["projectId"], "--display-name", params["displayName"],
           "--customer", params["customer"], "--pack-kind", params["packKind"], "--data-dir", str(Path(data_dir).resolve()), "--max-rounds", str(params["maxRounds"]),
           "--build", "--out", str(out)]
    if params["packKind"] == "reference":
        cmd.append("--review-reference")  # a fictional pack: the SA's batch review of sa_synthetic items, as the acceptance runs did
    if Path(data_dir).expanduser().resolve() == LIVE_APP_DATA.resolve():
        cmd.append("--allow-live-data-dir")  # the App's own data, from the App's own page (LIVE_APP_DATA)
    if params["preRehearse"] or params["direct"]:
        cmd += ["--pre-rehearse", "--aws-profile", str(workspace.get("profile") or "default"), "--aws-region", workspace["region"]]
    if params["direct"]:
        cmd += ["--direct", "--aws-account", workspace["accountId"], "--direct-rounds", str(params["directRounds"]),
                "--direct-repeat", str(params["directRepeat"])]
    return cmd


PHASES = (("rounds", "生成与修复"), ("pre-rehearsal", "本地预测"), ("direct", "直连彩排（已完成）"))


def phase(run: Path) -> str | None:
    """Where the loop is, from what it has written last: a generation round, a local prediction, a direct rehearsal."""
    found = [(d.stat().st_mtime, f"{label} {d.name}") for folder, label in PHASES if (run / folder).is_dir() for d in (run / folder).iterdir() if d.is_dir()]
    return max(found)[1] if found else None


def summary(out: Path) -> dict[str, Any]:
    """What the page shows from the loop's ``report.json``: the outcome, the rounds, the predictions and the rehearsals."""
    try:
        report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"report": None}
    rounds = [{k: r.get(k) for k in ("n", "mode", "status", "kiroSeconds", "filesChanged")} | {"findings": len(r.get("findings") or r.get("errors") or [])}
              for r in report.get("rounds") or []]
    pre = [{"prediction": p.get("prediction"), "findings": (p.get("findings") or [])[:3], "seconds": p.get("seconds")} for p in report.get("preRehearsals") or []]
    direct = [{k: d.get(k) for k in ("verdict", "reasonCode", "blocking", "phenomena", "seconds", "build", "robust", "flaky")}
              for d in report.get("directRehearsals") or []]
    final = report.get("final") or {}
    last = direct[-1] if direct else {}
    ready = bool(last) and last.get("verdict") == "ready" and last.get("robust", True) is not False  # with --direct-repeat: every round
    return {**{k: report.get(k) for k in SUMMARY_KEYS}, "rounds": rounds, "preRehearsals": pre, "directRehearsals": direct,
            "validationOk": final.get("validationOk"), "readyForClassDirect": ready}


def start(console: Any, workspace: str, body: Mapping[str, Any]) -> dict[str, Any]:
    params = check(body)
    ws = console.workspaces.get(workspace)
    if params["direct"] or params["preRehearse"]:
        if ws.get("roleArn") or not ws.get("profile"):
            raise AutopilotError("autopilot needs a workspace reached through a local AWS profile: the loop runs for about an hour in its own process")
        console.workspaces.verify(workspace)
    kiro = shutil.which("kiro-cli")
    if not kiro:
        raise AutopilotError("kiro-cli is not on this machine's PATH: install it and log in (kiro-cli login)")
    data_dir = Path(console.workshop_data)
    if (data_dir / "projects" / params["projectId"]).exists() and body.get("reuse") is not True:
        raise AutopilotError(f"project {params['projectId']} already exists in the workshop data: pick another id (or send reuse: true)")
    running = [j for j in console.jobs.list(workspace=workspace, kind="autopilot") if j.get("status") == "running"]
    if running:
        raise AutopilotError(f"an autopilot is already running ({running[0]['id']}): one at a time, Kiro and the direct stack are shared")

    def work(ctx: Any) -> dict[str, Any]:
        out = Path(console.data_dir) / "console" / "autopilot" / ctx.job["id"]
        out.mkdir(parents=True, exist_ok=True)
        brief = out / "brief.md"
        brief.write_text(params["brief"] + "\n", encoding="utf-8")
        cmd = command(params, brief=brief, data_dir=data_dir, out=out / "run", workspace=ws)
        ctx.log("$ " + " ".join(c if " " not in c else repr(c) for c in cmd[1:]))
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(cmd, cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, bufsize=1)
        ctx.progress(pid=proc.pid)
        done = threading.Event()

        def watch() -> None:  # the loop prints little between phases: show where it is from what it writes
            last = None
            while not done.wait(15):
                now = phase(out / "run")
                if now and now != last:
                    last = now
                    ctx.progress(phase=now)

        threading.Thread(target=watch, name=f"autopilot-watch-{ctx.job['id']}", daemon=True).start()
        assert proc.stdout is not None
        try:
            in_report = False
            for line in proc.stdout:
                line = line.rstrip()
                in_report = in_report or line == "{"  # the loop ends by printing its JSON report: that is read from report.json
                if line and not in_report:
                    ctx.log(line[:400])
                    if line.lstrip().startswith("direct "):
                        ctx.progress(phase="直连彩排 " + line.strip().split(":", 1)[0].split(" ", 1)[-1] + " 进行中")
            code = proc.wait()
        finally:
            done.set()
        result = {"exitCode": code, "out": str(out / "run"), **summary(out / "run")}
        # exit 1 is an outcome (not converged, needs input: the report says why); 2 a refusal or a setup error, 3 Kiro
        # CLI failing: the job failed. So did a loop that wrote no report.
        if code >= 2 or (code and result.get("report", True) is None):
            raise AutopilotError(f"the loop exited {code}: {result.get('error') or 'see the job log'}")
        return result

    job = console.jobs.start("autopilot", workspace, {k: v for k, v in params.items() if k != "brief"} | {"briefChars": len(params["brief"])}, work,
                             label=f"自动驾驶 {params['projectId']}")
    return {"job": job}


def register(router: Any) -> None:
    router.add("POST", "/workspaces/{wid}/autopilot", lambda r: (202, start(r.console, r.workspace(), r.body)), admin=True)
    router.add("GET", "/workspaces/{wid}/autopilot", lambda r: (200, {"jobs": r.console.jobs.list(workspace=r.workspace(), kind="autopilot")}))
    router.add("GET", "/autopilot/briefs", lambda r: (200, {"briefs": sorted(p.stem for p in (REPO / "acceptance" / "briefs").glob("*.md"))}))
    router.add("GET", "/autopilot/briefs/{name}", lambda r: (200, {"name": r.params["name"], "brief": _brief(r.params["name"])}))


def _brief(name: str) -> str:
    if not re.match(r"^[a-z0-9-]{1,64}$", name):
        raise AutopilotError("no such sample brief")
    path = REPO / "acceptance" / "briefs" / f"{name}.md"
    if not path.exists():
        raise AutopilotError("no such sample brief")
    return path.read_text(encoding="utf-8")
