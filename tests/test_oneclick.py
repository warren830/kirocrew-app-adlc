"""One-click delivery: validate → build → preflight → apply → verify → confirm → Guided Run, as a background job."""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_app_backend import Client, server_mod  # noqa: E402
from test_sync import ACCOUNT, StubClients  # noqa: E402
from workshop_customizer.guided_run import GUIDE_STEPS  # noqa: E402

PROJECT = "acme-oneclick"
TARGET = {"profile": "align-workshop", "region": "us-west-2", "expectedAccountId": ACCOUNT, "releaseProject": PROJECT}


class HostStub(StubClients):
    """StubClients whose SSM side keeps EC2 applier state across apply / status / commit."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.host: dict = {"active": None, "previous": None, "status": "no-active-release", "releases": []}
        self._results: dict[str, dict] = {}
        base_ssm = self.ssm
        outer = self

        class _Ssm:
            def __getattr__(self, name):  # preflight reads (get_parameter, describe_instance_information)
                return getattr(base_ssm, name)

            def send_command(self, **kwargs):
                outer.calls.append(("ssm.send_command", kwargs))
                params = kwargs["Parameters"]
                command_id = f"cmd-{len(outer._results) + 1}"
                outer._results[command_id] = outer._host_action(params["Action"][0], (params.get("ExpectedVersion") or [""])[0])
                return {"Command": {"CommandId": command_id}}

            def get_command_invocation(self, CommandId, InstanceId):
                outer.calls.append(("ssm.get_command_invocation", {"CommandId": CommandId, "InstanceId": InstanceId}))
                return {"Status": "Success", "StandardOutputContent": json.dumps(outer._results[CommandId])}

        self.ssm = _Ssm()

    def _host_action(self, action: str, version: str) -> dict:
        host = self.host
        if action == "apply":
            if host["active"] == version:
                return {"status": "no-op", "active": version, "version": version}
            if version not in host["releases"]:
                host["releases"].append(version)
            host.update(previous=host["active"], active=version, status="pending-commit")
            return {"status": "pending-commit", "version": version, "active": version}
        if action == "status":
            return {**host, "currentLink": f"releases/{host['active']}" if host["active"] else None, "releases": sorted(host["releases"])}
        if action == "commit":
            if host["active"] != version or host["status"] not in ("pending-commit", "committed"):
                return {"status": "error", "error": "nothing to commit"}
            host["status"] = "committed"
            return {"status": "committed", "active": version}
        if action == "rollback":
            if not host["previous"]:
                return {"status": "error", "error": "no previous release to roll back to"}
            host.update(active=host["previous"], previous=None, status="rolled-back")
            return {"status": "rolled-back", "active": host["active"]}
        raise AssertionError(f"unexpected action {action}")


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("oneclick")
    stub = HostStub(expiry=datetime.now(timezone.utc) + timedelta(hours=2))
    srv, svc = server_mod.create_server(data_dir, home=REPO_ROOT, clients_factory=lambda cfg: stub)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    client = Client(srv.server_address[1])
    status, body, _ = client.call("POST", "/projects", {"id": PROJECT, "template": "it-helpdesk", "packKind": "reference", "displayName": "ACME One-click"})
    assert status == 201, body
    yield {"client": client, "service": svc, "stub": stub}
    srv.shutdown()
    srv.server_close()


def _run(env, body=None) -> dict:
    client, svc = env["client"], env["service"]
    status, job, _ = client.call("POST", f"/projects/{PROJECT}/oneclick", {"acknowledged": True, **(body or {})})
    assert status == 202 and job["status"] == "running", job
    svc.jobs.wait(job["id"], timeout=180)
    status, latest, _ = client.call("GET", f"/projects/{PROJECT}/oneclick")
    assert status == 200 and latest["job"]["id"] == job["id"], latest
    return latest["job"]


def _stages(job: dict) -> dict:
    return {stage["id"]: stage for stage in job["stages"]}


def _ssm_actions(stub: HostStub) -> list[str]:
    return [call[1]["Parameters"]["Action"][0] for call in stub.calls if call[0] == "ssm.send_command"]


def test_oneclick_refuses_without_target_and_creates_no_job(env):
    client = env["client"]
    status, body, _ = client.call("POST", f"/projects/{PROJECT}/oneclick", {})
    assert status == 409 and "target" in body["error"], body
    assert client.call("GET", f"/projects/{PROJECT}/oneclick")[1] == {"job": None}
    assert client.call("POST", f"/projects/{PROJECT}/oneclick", {"watchdogMinutes": 1})[0] in (400, 409)


def test_oneclick_delivers_release_and_binds_guided_run(env):
    client, stub = env["client"], env["stub"]
    assert client.call("PUT", f"/projects/{PROJECT}/target", TARGET)[0] == 200
    job = _run(env)
    assert job["status"] == "succeeded", json.dumps(job, indent=2)
    assert [stage["id"] for stage in job["stages"]] == list(server_mod.ONECLICK_STAGES)
    assert all(stage["status"] == "succeeded" for stage in job["stages"]), job["stages"]
    stages = _stages(job)
    version = stages["build"]["detail"]["version"]
    assert stages["build"]["detail"]["reused"] is False
    assert stages["apply"]["detail"]["status"] == "pending-commit"
    assert stages["verify"]["detail"]["currentLink"] == f"releases/{version}"
    assert job["result"] == {"version": version, "live": True, "nextStep": GUIDE_STEPS[0].id}

    assert stub.host["active"] == version and stub.host["status"] == "committed"
    assert _ssm_actions(stub) == ["apply", "status", "commit"]
    names = stub.names()
    assert names.index("s3.put_object") < names.index("ssm.send_command")
    assert client.call("GET", f"/projects/{PROJECT}")[1]["status"] == "synced"
    history = [event["event"] for event in client.call("GET", f"/projects/{PROJECT}/sync/history")[1]["history"]]
    assert history == ["preflight", "apply", "status", "commit"]
    run = client.call("GET", f"/projects/{PROJECT}/run")[1]
    assert run["releaseVersion"] == version and run["status"] == "not_started"


def test_oneclick_rerun_reuses_build_and_keeps_guided_progress(env):
    client, stub = env["client"], env["stub"]
    before = client.call("GET", f"/projects/{PROJECT}/run")[1]
    stub.calls.clear()
    job = _run(env)
    assert job["status"] == "succeeded", json.dumps(job, indent=2)
    stages = _stages(job)
    assert stages["build"]["detail"]["reused"] is True
    assert stages["apply"]["detail"]["status"] == "no-op"
    assert stages["guided"]["detail"]["reset"] is False
    assert _ssm_actions(stub) == ["apply", "status", "commit"]
    assert client.call("GET", f"/projects/{PROJECT}/run")[1]["createdAt"] == before["createdAt"]


def test_sync_actions_run_as_background_jobs(env):
    # Behind the host's 30-second proxy the synchronous sync/<action> routes can 504 mid-command;
    # the /job variants answer 202 at once and keep the exact same records and history.
    client, svc, stub = env["client"], env["service"], env["stub"]
    version = stub.host["active"]
    assert client.call("POST", f"/projects/{PROJECT}/sync/bogus/job", {})[0] == 400
    assert client.call("GET", f"/projects/{PROJECT}/sync/bogus/job")[0] == 404

    def run_action(action: str) -> dict:
        status, job, _ = client.call("POST", f"/projects/{PROJECT}/sync/{action}/job", {})
        assert status == 202 and job["kind"] == f"sync-{action}" and job["status"] == "running", job
        svc.jobs.wait(job["id"], timeout=60)
        status, body, _ = client.call("GET", f"/projects/{PROJECT}/jobs/{job['id']}")
        assert status == 200 and body["job"]["status"] == "succeeded", json.dumps(body, indent=2)
        assert [(s["id"], s["status"]) for s in body["job"]["stages"]] == [(action, "succeeded")]
        assert client.call("GET", f"/projects/{PROJECT}/sync/{action}/job")[1]["job"]["id"] == job["id"]
        return body["job"]

    assert run_action("status")["result"]["applier"]["active"] == version
    assert run_action("commit")["result"]["applier"]["status"] == "committed"
    assert client.call("GET", f"/projects/{PROJECT}")[1]["status"] == "synced"

    stub.host["previous"] = "older-release"  # pretend an earlier release is still on the EC2
    rollback = run_action("rollback")
    assert stub.host["active"] == "older-release"
    assert rollback["result"]["status"] in ("rolled-back", "rolled-back-files-only")
    assert client.call("GET", f"/projects/{PROJECT}")[1]["status"] in ("rolled-back", "rollback-incomplete")
    history = [event["event"] for event in client.call("GET", f"/projects/{PROJECT}/sync/history")[1]["history"]]
    assert history[-3:] == ["status", "commit", "rollback"]


def test_oneclick_with_target_requires_explicit_acknowledgement(env):
    # The target is configured by now, so only the acknowledgement gate can refuse these requests.
    client, stub = env["client"], env["stub"]
    before = client.call("GET", f"/projects/{PROJECT}/oneclick")[1]["job"]
    stub.calls.clear()
    for body in ({}, {"acknowledged": False}, {"acknowledged": "true"}, {"acknowledged": 1}):
        status, reply, _ = client.call("POST", f"/projects/{PROJECT}/oneclick", body)
        assert status == 400 and "acknowledged" in reply["error"], (body, reply)
    assert client.call("GET", f"/projects/{PROJECT}/oneclick")[1]["job"] == before
    assert "s3.put_object" not in stub.names() and "ssm.send_command" not in stub.names()


def test_oneclick_stops_at_failed_preflight_before_any_upload(env):
    stub = env["stub"]
    stub.calls.clear()
    stub._ping = "ConnectionLost"
    try:
        job = _run(env)
    finally:
        stub._ping = "Online"
    stages = _stages(job)
    assert job["status"] == "failed", json.dumps(job, indent=2)
    assert stages["validate"]["status"] == "succeeded" and stages["build"]["status"] == "succeeded"
    assert stages["preflight"]["status"] == "failed" and stages["preflight"]["detail"]["failed"]
    assert [stages[s]["status"] for s in ("apply", "verify", "confirm", "guided")] == ["not-run"] * 4
    assert "s3.put_object" not in stub.names() and "ssm.send_command" not in stub.names()


def test_running_job_rejects_second_start_and_other_writes(env):
    client, svc = env["client"], env["service"]
    release = threading.Event()
    job = svc.jobs.start(PROJECT, "oneclick", ("hold",), lambda ctx: release.wait(30) and {"held": True})
    try:
        status, body, _ = client.call("POST", f"/projects/{PROJECT}/oneclick", {})
        assert status == 409 and body["jobId"] == job["id"], body
        assert client.call("POST", f"/projects/{PROJECT}/build")[0] == 409
        assert client.call("PUT", f"/projects/{PROJECT}/target", TARGET)[0] == 409
        assert client.call("GET", f"/projects/{PROJECT}")[0] == 200
        status, body, _ = client.call("GET", f"/projects/{PROJECT}/jobs/{job['id']}")
        assert status == 200 and body["job"]["status"] == "running"
    finally:
        release.set()
        svc.jobs.wait(job["id"], timeout=30)
    assert client.call("GET", f"/projects/{PROJECT}/jobs/{job['id']}")[1]["job"]["status"] == "succeeded"
    assert client.call("GET", f"/projects/{PROJECT}/jobs/not-a-job")[0] == 400
    assert client.call("GET", f"/projects/{PROJECT}/jobs/oneclick-20000101t000000000000z-000000")[0] == 404


def test_job_left_running_by_a_restart_is_marked_interrupted(tmp_path):
    data_dir = tmp_path / "data"
    srv, svc = server_mod.create_server(data_dir, home=REPO_ROOT)
    srv.server_close()
    svc.create_project({"id": "restart-demo", "template": "it-helpdesk", "packKind": "reference"})
    jobs = data_dir / "projects" / "restart-demo" / "jobs"
    jobs.mkdir()
    job_id = "oneclick-20260926t000000000000z-abcdef"
    stages = [{"id": "validate", "status": "succeeded"}, {"id": "build", "status": "running"}, {"id": "preflight", "status": "pending"}]
    (jobs / f"{job_id}.json").write_text(json.dumps({"id": job_id, "project": "restart-demo", "kind": "oneclick", "status": "running", "stages": stages}))

    srv2, svc2 = server_mod.create_server(data_dir, home=REPO_ROOT)
    srv2.server_close()
    job = svc2.jobs.get("restart-demo", job_id)
    assert job["status"] == "interrupted" and "restarted" in job["error"]
    assert [stage["status"] for stage in job["stages"]] == ["succeeded", "interrupted", "not-run"]
    assert svc2.jobs.active("restart-demo") is None
