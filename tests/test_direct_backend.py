"""Direct mode in the App (POST/GET /projects/{pid}/direct, chat, cleanup): staged jobs, the current build's
rehearsal, a chat with the pack's Harness and the cleanup acknowledgement.  In-process server on loopback; the
AWS session and DirectRun are fakes, so nothing reaches AWS."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from test_app_backend import Client, server_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
PID = "direct-hr"
ACCOUNT = "111122223333"


class FakeRuntime:
    def __init__(self):
        self.calls = []

    def invoke_harness(self, **kwargs):
        self.calls.append(kwargs)
        return {"stream": [{"contentBlockDelta": {"delta": {"text": "年假有 10 天。"}}},
                           {"contentBlockStart": {"start": {"toolUse": {"name": "hrtools___retrieve_hr_policy"}}}},
                           {"messageStop": {"stopReason": "end_turn"}}]}


class FakeSession:
    def __init__(self):
        self.runtime = FakeRuntime()

    def client(self, name, region_name=None, **kw):  # the chat's client carries direct mode's retry config
        assert name == "bedrock-agentcore", name
        return self.runtime


@pytest.fixture()
def app(tmp_path, monkeypatch):
    def no_aws(cfg):
        raise AssertionError("direct mode must not create the Workshop clients")

    srv, svc = server_mod.create_server(tmp_path / "data", home=REPO_ROOT, clients_factory=no_aws)
    session = FakeSession()
    monkeypatch.setattr(svc, "_direct_session", lambda pid: (session, ACCOUNT, svc._target_cfg(pid)[0]))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"client": Client(srv.server_address[1]), "service": svc, "pdir": tmp_path / "data" / "projects" / PID, "session": session}
    srv.shutdown()
    srv.server_close()


def _ready_project(c):
    assert c.call("POST", "/projects", {"id": PID, "template": "hr-default", "packKind": "reference"})[0] == 201
    assert c.call("PUT", f"/projects/{PID}/target", {"profile": "p", "region": "us-west-2", "expectedAccountId": ACCOUNT})[0] == 200


def _wait(c, job_id, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = c.call("GET", f"/projects/{PID}/jobs/{job_id}")[1]["job"]
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still running")


def test_a_direct_job_reports_each_stage_and_the_status_shows_the_current_builds_verdict(app, monkeypatch):
    c, svc, pdir = app["client"], app["service"], app["pdir"]
    _ready_project(c)
    status, body, _ = c.call("POST", f"/projects/{PID}/direct", {})
    assert status == 409 and "build" in body["error"]
    assert c.call("POST", f"/projects/{PID}/build")[0] == 200
    version = json.loads((pdir / "build" / "build.json").read_text(encoding="utf-8"))["version"]
    seen = {}

    class FakeRun:
        def __init__(self, project_dir, **kw):
            seen.update(kw, project_dir=project_dir)

        def run(self):
            for stage in ("provision", "ask", "settle", "judge", "rehearse"):
                seen["on_stage"](stage)
            seen["out"].mkdir(parents=True)
            doc = {"releaseVersion": version, "verdict": "ready", "reasonCode": "CONTRASTS_REPRODUCED", "reason": "全部复现",
                   "phenomena": [{"id": "gap", "kind": "retrieval_gap", "verdict": "reproduced", "reasonCode": "OK", "teachingPoint": "t",
                                  "cases": [{"caseId": "c1", "status": "reproduced", "baseline": {"GR": 0.2}, "optimized": {"GR": 0.2},
                                             "extra": "dropped"}]}],
                   "remediation": [{"code": "RELEASE_NOT_VERIFIED", "severity": "blocker"}, {"code": "X", "severity": "advisory", "caseId": "c1"}],
                   "direct": {"seconds": 451.6, "timings": {"provision": 34.2}, "sweep": [], "invokeErrors": [],
                              "harness": {"id": "h"}, "names": {"harness": "hr_direct"},
                              "panel": {"evaluators": ["Builtin.Faithfulness"], "bands": {}, "matrix": [], "recommendation": [], "rows": [{"x": 1}]}}}
            (seen["out"] / "direct-rehearsal.json").write_text(json.dumps(doc), encoding="utf-8")
            (seen["out"].parent / "resources.json").write_text(json.dumps({"harness": {"arn": "arn:h"}}), encoding="utf-8")
            return doc

    monkeypatch.setattr(svc, "_direct_run_class", lambda: FakeRun)
    assert c.call("POST", f"/projects/{PID}/direct", {"repeat": 5})[0] == 400
    assert c.call("POST", f"/projects/{PID}/direct", {"compareModels": ["nova pro; rm -rf"]})[0] == 400
    status, job, _ = c.call("POST", f"/projects/{PID}/direct", {"compareModels": ["us.amazon.nova-pro-v1:0", " ", 7], "repeat": 3, "panel": True})
    assert status == 202 and job["kind"] == "direct" and [s["id"] for s in job["stages"]] == list(svc.DIRECT_STAGES)
    job = _wait(c, job["id"])
    assert job["status"] == "succeeded" and all(s["status"] == "succeeded" for s in job["stages"]), job
    assert job["result"] == {"version": version, "verdict": "ready", "reasonCode": "CONTRASTS_REPRODUCED", "seconds": 451.6}
    assert seen["compare_models"] == ["us.amazon.nova-pro-v1:0"] and seen["account"] == ACCOUNT and seen["out"] == pdir / "build" / "direct" / version
    assert seen["repeat"] == 3 and "Builtin.Faithfulness" in seen["panel"] and job["params"] == {"compareModels": ["us.amazon.nova-pro-v1:0"],
                                                                                              "repeat": 3, "panel": True}

    status, got, _ = c.call("GET", f"/projects/{PID}/direct")
    assert status == 200 and got["version"] == version and got["job"]["id"] == job["id"]
    assert got["rehearsal"]["verdict"] == "ready" and got["rehearsal"]["direct"]["seconds"] == 451.6
    assert got["rehearsal"]["phenomena"][0]["cases"] == [{"caseId": "c1", "status": "reproduced", "reason": None, "reasonEn": None,
                                                         "baseline": {"GR": 0.2}, "optimized": {"GR": 0.2}}]
    assert [h["code"] for h in got["rehearsal"]["remediation"]] == ["X"]  # never verified on a Workshop: that is the Guided Run's job
    assert "names" not in got["rehearsal"]["direct"] and got["resources"] == {"harness": {"arn": "arn:h"}}
    assert got["rehearsal"]["direct"]["panel"] == {"evaluators": ["Builtin.Faithfulness"], "bands": {}, "matrix": [], "recommendation": [], "unseen": None,
                                                   "references": None, "error": None}

    # A rebuild of a changed scenario is another version: its direct rehearsal is not this one.
    (pdir / "build" / "build.json").write_text(json.dumps({**json.loads((pdir / "build" / "build.json").read_text()), "version": "other"}))
    assert c.call("GET", f"/projects/{PID}/direct")[1]["rehearsal"] is None


def test_a_failed_stage_is_marked_and_the_job_fails(app, monkeypatch):
    c, svc = app["client"], app["service"]
    _ready_project(c)
    assert c.call("POST", f"/projects/{PID}/build")[0] == 200

    class Broken:
        def __init__(self, project_dir, **kw):
            self.on_stage = kw["on_stage"]

        def run(self):
            self.on_stage("provision")
            self.on_stage("ask")
            raise RuntimeError("AccessDeniedException: no InvokeHarness")

    monkeypatch.setattr(svc, "_direct_run_class", lambda: Broken)
    job = _wait(c, c.call("POST", f"/projects/{PID}/direct", {})[1]["id"])
    stages = {s["id"]: s["status"] for s in job["stages"]}
    assert job["status"] == "failed" and "AccessDeniedException" in job["error"]
    assert stages == {"provision": "succeeded", "ask": "failed", "settle": "not-run", "judge": "not-run", "rehearse": "not-run"}


def test_the_chat_asks_the_direct_harness_with_the_chosen_prompt(app):
    c, session, pdir = app["client"], app["session"], app["pdir"]
    _ready_project(c)
    assert c.call("POST", f"/projects/{PID}/build")[0] == 200
    status, body, _ = c.call("POST", f"/projects/{PID}/direct/chat", {"question": "年假几天？"})
    assert status == 409 and "direct rehearsal first" in body["error"]
    (pdir / "build" / "direct").mkdir(parents=True)
    (pdir / "build" / "direct" / "resources.json").write_text(json.dumps({"harness": {"arn": "arn:aws:h"}}), encoding="utf-8")
    assert c.call("POST", f"/projects/{PID}/direct/chat", {"question": ""})[0] == 400
    assert c.call("POST", f"/projects/{PID}/direct/chat", {"question": "q", "prompt": "other"})[0] == 400
    assert c.call("POST", f"/projects/{PID}/direct/chat", {"question": "q", "model": "bad model; rm"})[0] == 400

    status, chat, _ = c.call("POST", f"/projects/{PID}/direct/chat", {"question": "年假几天？", "prompt": "baseline",
                                                                     "model": "us.amazon.nova-pro-v1:0"})
    assert status == 202 and chat["status"] == "running"
    deadline = time.monotonic() + 10
    while chat["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        chat = c.call("GET", f"/projects/{PID}/direct/chat/{chat['id']}")[1]
    assert (chat["status"], chat["answer"], chat["tools"]) == ("done", "年假有 10 天。", ["hrtools___retrieve_hr_policy"])
    call = session.runtime.calls[-1]
    baseline = (pdir / "build" / "release" / "pack" / "prompts" / "baseline.md").read_text(encoding="utf-8")
    assert call["harnessArn"] == "arn:aws:h" and call["systemPrompt"] == [{"text": baseline}] and len(call["runtimeSessionId"]) >= 33
    assert call["model"] == {"bedrockModelConfig": {"modelId": "us.amazon.nova-pro-v1:0"}}
    assert c.call("GET", f"/projects/{PID}/direct/chat/chat-000000000000")[0] == 404
    assert c.call("GET", f"/projects/{PID}/direct/chat/..%2Fresources")[0] == 400


def test_cleanup_needs_an_acknowledgement_and_reports_what_failed(app, monkeypatch):
    from workshop_customizer.direct import cleanup as cleanup_mod

    c = app["client"]
    _ready_project(c)
    assert c.call("POST", f"/projects/{PID}/build")[0] == 200
    assert c.call("POST", f"/projects/{PID}/direct/cleanup", {})[0] == 400
    calls = []

    def fake_cleanup(session, names, **kw):
        calls.append(names.harness)
        return {"harness": "deleted 1", "roles": "failed: AccessDenied"}

    monkeypatch.setattr(cleanup_mod, "cleanup", fake_cleanup)
    status, job, _ = c.call("POST", f"/projects/{PID}/direct/cleanup", {"acknowledged": True})
    assert status == 202 and job["kind"] == "direct-cleanup"
    job = _wait(c, job["id"])
    assert job["status"] == "failed" and job["stages"][0]["detail"] == {"roles": "failed: AccessDenied"}
    assert calls == ["direct_hrassistant"]
    got = c.call("GET", f"/projects/{PID}/direct")[1]
    assert got["job"] is None and got["cleanupJob"]["id"] == job["id"]  # a cleanup is not a rehearsal


def test_a_direct_job_lets_the_guided_run_and_the_chat_through_and_holds_the_rest(app, monkeypatch):
    """Review 2026-10-01: a direct round (8-25 min) answered 409 to run/poll, run/next and the chat."""
    c, svc = app["client"], app["service"]
    _ready_project(c)
    assert c.call("POST", f"/projects/{PID}/build")[0] == 200
    gate = threading.Event()

    class Slow:
        def __init__(self, project_dir, **kw):
            self.on_stage = kw["on_stage"]

        def run(self):
            self.on_stage("provision")
            gate.wait(10)
            raise RuntimeError("stopped by the test")

    monkeypatch.setattr(svc, "_direct_run_class", lambda: Slow)
    job = c.call("POST", f"/projects/{PID}/direct", {})[1]
    try:
        assert c.call("POST", f"/projects/{PID}/build")[0] == 409  # the release must not change under the round
        for path, body in ((f"/projects/{PID}/run/poll", {}), (f"/projects/{PID}/direct/chat", {"question": "q"})):
            status, got, _ = c.call("POST", path, body)
            assert "still running" not in str(got.get("error") or ""), (path, status, got)  # a 409 for "no Harness yet" is the chat's own
    finally:
        gate.set()
        _wait(c, job["id"])
