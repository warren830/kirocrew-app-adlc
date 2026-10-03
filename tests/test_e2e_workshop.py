"""End to end: a validated scenario → one-click overwrite → the whole Guided Workshop, step by step.

AWS is simulated: ``HostStub`` keeps the EC2 applier state (apply/status/commit) and this module adds the
Guided Run document, where every allowlisted step passes with the outputs the report needs.
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_app_backend import Client, server_mod, wait_job  # noqa: E402
from test_oneclick import HostStub  # noqa: E402
from test_sync import ACCOUNT, default_stacks  # noqa: E402
import teaching_run  # noqa: E402

RUN_DOCUMENT = "WorkshopCustomizerRunStep"


def _step_outputs(step: str, script: str, pack: dict | None) -> dict:
    outputs: dict = {"script": script, "exitCode": 0}
    if step in ("baseline", "optimize"):
        # Every practice case gets an L1 verdict and a case-tagged goal-judge score (the teaching evidence gate).
        outputs = teaching_run.complete_outputs(pack, step, script=script)
    elif step == "cost-latency":
        outputs["costLatency"] = {"averageLatencySeconds": 1.2, "averageInputTokens": 100, "averageOutputTokens": 40, "totalCostUsd": 0.001}
    elif step == "models":
        outputs.update({
            "models": {"baseline": "model-a", "comparison": "model-b"},
            "scores": [{"evaluator": "thelma_rag_quality", "traceId": "model", "value": 0.75, "label": "Pass"}],
            "costLatency": {"averageLatencySeconds": 0.9, "totalCostUsd": 0.0008},
        })
    elif step == "judge-stability":
        outputs["judgeStability"] = {"values": [0.8, 0.82, 0.79], "mean": 0.803, "std": 0.012, "spread": 0.03, "verdict": "stable"}
    return outputs


class WorkshopHostStub(HostStub):
    """HostStub plus the Guided Run document; results are fixed at send time and read back by poll."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pack: dict | None = None
        sync_ssm, outer = self.ssm, self

        class _Ssm:
            def __getattr__(self, name):
                return getattr(sync_ssm, name)

            def send_command(self, **kwargs):
                if kwargs.get("DocumentName") != RUN_DOCUMENT:
                    return sync_ssm.send_command(**kwargs)
                outer.calls.append(("ssm.send_command", kwargs))
                step, script = kwargs["Parameters"]["StepId"][0], kwargs["Parameters"]["Script"][0]
                command_id = f"run-{step}"
                outer._results[command_id] = {"status": "passed", "summary": f"{step} complete", "outputs": _step_outputs(step, script, outer.pack)}
                return {"Command": {"CommandId": command_id}}

        self.ssm = _Ssm()


@pytest.fixture()
def workshop(tmp_path):
    stub = WorkshopHostStub(expiry=datetime.now(timezone.utc) + timedelta(hours=2))
    srv, svc = server_mod.create_server(tmp_path, home=REPO_ROOT, clients_factory=lambda cfg: stub)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield {"client": Client(srv.server_address[1]), "service": svc, "stub": stub}
    srv.shutdown()
    srv.server_close()


@pytest.mark.parametrize("template", ["it-helpdesk", "maintenance"])
def test_scenario_oneclick_then_full_guided_workshop(workshop, template):
    client, svc, stub = workshop["client"], workshop["service"], workshop["stub"]
    pid = f"e2e-{template}"
    status, body, _ = client.call("POST", "/projects", {"id": pid, "template": template, "packKind": "reference", "displayName": f"E2E {template}"})
    assert status == 201, body
    stub.pack = teaching_run.scenario(template)
    stub._stacks = default_stacks(stub.pack["namespace"])  # the add-ons are deployed for this pack (--scenario)
    assert client.call("POST", f"/projects/{pid}/validate")[1]["ok"] is True
    target = {"profile": "align-workshop", "region": "us-west-2", "expectedAccountId": ACCOUNT, "releaseProject": pid}
    assert client.call("PUT", f"/projects/{pid}/target", target)[0] == 200

    # One click: validate → build → preflight → apply → verify → confirm → bind Workshop Run.
    status, job, _ = client.call("POST", f"/projects/{pid}/oneclick", {"acknowledged": True})
    assert status == 202, job
    wait_job(svc.jobs, job, timeout=180)
    job = client.call("GET", f"/projects/{pid}/jobs/{job['id']}")[1]["job"]
    assert job["status"] == "succeeded", json.dumps(job, indent=2)
    version = job["result"]["version"]
    assert stub.host["active"] == version and stub.host["status"] == "committed"
    assert client.call("GET", f"/projects/{pid}")[1]["status"] == "synced"

    # Then the whole Workshop, one step at a time; cleanup is never part of the run.
    run = client.call("GET", f"/projects/{pid}/run")[1]
    order = run["stepOrder"]
    assert run["releaseVersion"] == version and len(order) == 15 and "cleanup" not in order
    assert job["result"]["nextStep"] == order[0]
    for step in order:
        status, started, _ = client.call("POST", f"/projects/{pid}/run/next", {})
        assert status == 200 and started["currentStepId"] == step, started
        state = client.call("POST", f"/projects/{pid}/run/poll", {})[1]
        assert state["steps"][step]["status"] == "passed", state["steps"][step]

    assert state["status"] == "passed" and state["report"]["status"] == "complete"
    status, report, _ = client.call("GET", f"/projects/{pid}/run/report")
    assert status == 200 and report["completion"]["passed"] == 15
    assert report["quality"]["delta"]["thelma_rag_quality"]["delta"] == 0.2
    # The synthetic outputs improve every probe with good retrieval: prompt-fixable reproduces, the gap does not.
    assert report["teachingContrast"]["groups"]["prompt_fixable"] == "reproduced"
    assert report["teachingContrast"]["currentRun"] == "not_reproduced"
    sends = [(p.get("DocumentName"), p["Parameters"]) for n, p in stub.calls if n == "ssm.send_command"]
    assert [params["Action"][0] for doc, params in sends if doc != RUN_DOCUMENT] == ["apply", "status", "commit"]
    assert [params["StepId"][0] for doc, params in sends if doc == RUN_DOCUMENT] == order
