"""engine console.autopilot: the brief's checks, the loop's command line, the report summary (no Kiro, no AWS)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer.console import autopilot  # noqa: E402

BRIEF = "某燃气公司客服中心：坐席要回答缴费、账单、抄表、开通与过户的问题。" * 10


def test_a_brief_is_checked_and_becomes_the_loops_own_command_line(tmp_path):
    with pytest.raises(autopilot.AutopilotError, match="projectId"):
        autopilot.check({"projectId": "Bad Id", "brief": BRIEF})
    with pytest.raises(autopilot.AutopilotError, match="200"):
        autopilot.check({"projectId": "gas", "brief": "too short"})
    params = autopilot.check({"projectId": "gas-auto", "brief": BRIEF, "packKind": "reference"})
    assert (params["directRepeat"], params["directRounds"], params["maxRounds"], params["preRehearse"]) == (3, 3, 6, True)
    ws = {"accountId": "111122223333", "region": "us-west-2", "profile": "default"}
    cmd = autopilot.command(params, brief=tmp_path / "brief.md", data_dir=tmp_path / "data", out=tmp_path / "out", workspace=ws)
    assert cmd[1] == str(autopilot.LOOP) and cmd[2] == "--loop" and "--review-reference" in cmd
    joined = " ".join(cmd)
    for part in ("--build", "--pre-rehearse --aws-profile default --aws-region us-west-2", "--direct --aws-account 111122223333 --direct-rounds 3 --direct-repeat 3",
                 f"--data-dir {(tmp_path / 'data').resolve()}", "--pack-kind reference"):
        assert part in joined, part
    local = autopilot.command(autopilot.check({"projectId": "gas", "brief": BRIEF, "direct": False, "preRehearse": False}), brief=tmp_path / "b",
                              data_dir=tmp_path, out=tmp_path, workspace=ws)
    assert "--direct" not in local and "--pre-rehearse" not in local and "--review-reference" not in local


def test_the_report_says_whether_every_direct_round_held(tmp_path):
    report = {"projectId": "gas", "converged": True, "exit": 0, "seconds": 3035.8, "build": {"version": "gas-1"}, "final": {"validationOk": True},
              "rounds": [{"n": 1, "mode": "draft", "status": "ready", "kiroSeconds": 187.1, "filesChanged": 8}],
              "preRehearsals": [{"prediction": "likely_not_ready", "findings": ["gap"], "seconds": 185.9}],
              "directRehearsals": [{"verdict": "not_ready", "reasonCode": "PHENOMENON_NOT_REPRODUCED", "phenomena": {"gap": "not_reproduced"}, "robust": False,
                                    "flaky": ["gap"]},
                                   {"verdict": "ready", "phenomena": {"gap": "reproduced"}, "robust": True, "flaky": []}]}
    (tmp_path / "report.json").write_text(json.dumps(report))
    got = autopilot.summary(tmp_path)
    assert got["readyForClassDirect"] is True and got["validationOk"] is True and got["rounds"][0]["kiroSeconds"] == 187.1
    report["directRehearsals"][-1]["robust"] = False
    (tmp_path / "report.json").write_text(json.dumps(report))
    assert autopilot.summary(tmp_path)["readyForClassDirect"] is False  # ready in one round, not in every one
    assert autopilot.summary(tmp_path / "missing") == {"report": None}


def test_autopilot_refuses_a_spoke_role_workspace_and_a_second_run(tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot.shutil, "which", lambda name: "/usr/local/bin/kiro-cli")
    ws = {"id": "dev", "accountId": "111122223333", "region": "us-west-2", "roleArn": "arn:aws:iam::111122223333:role/adlc-console-spoke"}
    running = [{"id": "autopilot-0123456789", "status": "running"}]
    console = type("C", (), {"workshop_data": tmp_path, "data_dir": tmp_path,
                             "workspaces": type("W", (), {"get": lambda _s, w: ws, "verify": lambda _s, w: {}})(),
                             "jobs": type("J", (), {"list": lambda _s, **kw: running})()})()
    with pytest.raises(autopilot.AutopilotError, match="local AWS profile"):
        autopilot.start(console, "dev", {"projectId": "gas", "brief": BRIEF})
    ws.pop("roleArn")
    ws["profile"] = "default"
    with pytest.raises(autopilot.AutopilotError, match="already running"):
        autopilot.start(console, "dev", {"projectId": "gas", "brief": BRIEF})


def test_the_phase_is_read_from_what_the_loop_wrote_last(tmp_path):
    import os
    import time as _time

    assert autopilot.phase(tmp_path) is None
    (tmp_path / "rounds" / "01-draft").mkdir(parents=True)
    os.utime(tmp_path / "rounds" / "01-draft", (_time.time() - 60, _time.time() - 60))
    assert autopilot.phase(tmp_path) == "生成与修复 01-draft"
    (tmp_path / "pre-rehearsal" / "01").mkdir(parents=True)
    assert autopilot.phase(tmp_path) == "本地预测 01"


def test_the_loop_may_write_the_kirocrew_apps_own_data_only_when_that_is_the_consoles_workshop_data(tmp_path):
    """Inside KiroCrew the console's workshop data is the App's own (the loop's LIVE_DATA_DIR, which it refuses unless
    told): Autopilot started from the App's page passes --allow-live-data-dir for exactly that directory."""
    params = autopilot.check({"projectId": "gas", "brief": BRIEF, "direct": False, "preRehearse": False})
    ws = {"id": "dev", "accountId": "111122223333", "region": "us-west-2", "profile": "default"}
    live = autopilot.command(params, brief=tmp_path / "b", data_dir=autopilot.LIVE_APP_DATA, out=tmp_path / "o", workspace=ws)
    other = autopilot.command(params, brief=tmp_path / "b", data_dir=tmp_path / "data", out=tmp_path / "o", workspace=ws)
    assert "--allow-live-data-dir" in live and "--allow-live-data-dir" not in other


def test_a_loop_that_refused_or_whose_kiro_failed_fails_the_job(tmp_path, monkeypatch):
    """Exit 2 (a refusal, e.g. the live data dir) or 3 (Kiro CLI) is a failed job with the report's error; exit 1 (not
    converged) is an outcome the job reports."""
    monkeypatch.setattr(autopilot.shutil, "which", lambda name: "/usr/local/bin/kiro-cli")
    ws = {"id": "dev", "accountId": "111122223333", "region": "us-west-2", "profile": "default"}

    class Jobs:
        def list(self, **kw):
            return []

        def start(self, kind, workspace, params, work, label=""):
            ctx = type("Ctx", (), {"job": {"id": "autopilot-0123456789"}, "log": lambda s, m: None, "progress": lambda s, **kw: None})()
            try:
                return {"result": work(ctx), "status": "succeeded"}
            except autopilot.AutopilotError as exc:
                return {"error": str(exc), "status": "failed"}

    console = type("C", (), {"workshop_data": tmp_path / "data", "data_dir": tmp_path, "jobs": Jobs(),
                             "workspaces": type("W", (), {"get": lambda _s, w: ws, "verify": lambda _s, w: {}})()})()

    def loop(code, report):
        def popen(cmd, **kw):
            out = Path(cmd[cmd.index("--out") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "report.json").write_text(json.dumps({**report, "exit": code}), encoding="utf-8")
            return type("P", (), {"stdout": iter([]), "pid": 1, "wait": lambda s: code})()
        monkeypatch.setattr(autopilot.subprocess, "Popen", popen)

    loop(2, {"projectId": "gas", "error": "refusing the live KiroCrew App data dir"})
    job = autopilot.start(console, "dev", {"projectId": "gas", "brief": BRIEF, "direct": False, "preRehearse": False})["job"]
    assert job["status"] == "failed" and "exited 2: refusing the live KiroCrew App data dir" in job["error"]
    loop(1, {"projectId": "gas", "converged": False, "rounds": []})
    job = autopilot.start(console, "dev", {"projectId": "gas", "brief": BRIEF, "direct": False, "preRehearse": False})["job"]
    assert job["status"] == "succeeded" and job["result"]["exitCode"] == 1 and job["result"]["converged"] is False


def test_the_loop_and_the_release_scripts_run_on_the_checkouts_python(tmp_path, monkeypatch):
    """Inside KiroCrew the console runs on the gateway's bundled Python, which lacks what the release's create_kb.py
    imports (retrying): the loop, and through it every script and judge, runs on the checkout's .venv when it is there."""
    from workshop_customizer.direct import aws

    params = autopilot.check({"projectId": "gas", "brief": BRIEF, "direct": False, "preRehearse": False})
    ws = {"id": "dev", "accountId": "111122223333", "region": "us-west-2", "profile": "default"}
    monkeypatch.setattr(aws, "CHECKOUT_PYTHON", tmp_path / "missing" / "python3")
    assert aws.tools_python() == sys.executable  # no checkout venv: this process's own
    venv = tmp_path / "python3"
    venv.write_text("#!/bin/sh\n", encoding="utf-8")
    venv.chmod(0o755)
    monkeypatch.setattr(aws, "CHECKOUT_PYTHON", venv)
    assert aws.tools_python() == str(venv)
    assert autopilot.command(params, brief=tmp_path / "b", data_dir=tmp_path / "data", out=tmp_path / "o", workspace=ws)[0] == str(venv)
