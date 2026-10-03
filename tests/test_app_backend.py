"""HTTP tests for app/backend/server.py (in-process server, real sockets on loopback)."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PY = REPO_ROOT / "app" / "backend" / "server.py"
PREFIX = "/api/apps/workshop-customizer"

# Reuse the duck-typed AWS stubs from the engine sync tests.
sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_sync import ACCOUNT, BUCKET, DOC, INSTANCE, StubClients  # noqa: E402
import teaching_run  # noqa: E402


def _load_server():
    spec = importlib.util.spec_from_file_location("wc_app_server", SERVER_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


server_mod = _load_server()


def wait_job(jobs, job: dict, timeout: float) -> None:
    """Poll until ``job`` is no longer its project's active job (its thread has finished and released it)."""
    deadline = time.monotonic() + timeout
    while jobs.active(job["project"]) == job["id"] and time.monotonic() < deadline:
        time.sleep(0.05)


class Client:
    def __init__(self, port: int, secret: str = ""):
        self.base = f"http://127.0.0.1:{port}"
        self.secret = secret

    def call(self, method: str, path: str, body=None, *, sign: bool = True, raw: bool = False, headers: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        if self.secret and sign:
            hdrs["X-KiroCrew-Proxy"] = server_mod.sign_for_tests(self.secret, method, PREFIX + path, data)
        req = urllib.request.Request(self.base + PREFIX + path, method=method, data=data if body is not None else None, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
                return resp.status, (payload if raw else json.loads(payload)), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            return exc.code, (payload if raw else json.loads(payload)), dict(exc.headers)


@pytest.fixture(scope="module")
def running(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("appdata")
    made: dict[str, StubClients] = {}

    def factory(cfg):
        if "clients" not in made:
            made["clients"] = StubClients(expiry=datetime.now(timezone.utc) + timedelta(hours=2))
        return made["clients"]

    srv, svc = server_mod.create_server(data_dir, home=REPO_ROOT, clients_factory=factory)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield {"client": Client(srv.server_address[1]), "service": svc, "data_dir": data_dir, "stubs": made}
    srv.shutdown()
    srv.server_close()


@pytest.fixture(scope="module")
def project(running):
    c = running["client"]
    status, body, _ = c.call("POST", "/projects", {"id": "acme-it", "template": "it-helpdesk", "packKind": "reference", "displayName": "ACME IT", "customer": "ACME"})
    assert status == 201, body
    return "acme-it"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_signature_is_required_when_secret_present(tmp_path):
    srv, _svc = server_mod.create_server(tmp_path / "d", home=REPO_ROOT, proxy_secret="s3cret")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = Client(srv.server_address[1], secret="s3cret")
        # the gateway's liveness probe is unsigned by design and must answer < 400
        status, body, _ = c.call("GET", "/health", sign=False)
        assert status == 200 and body["engine"] is True
        assert c.call("GET", "/projects", sign=False)[0] == 401
        assert c.call("GET", "/projects", headers={"X-KiroCrew-Proxy": "123:deadbeef"}, sign=False)[0] == 401
        assert c.call("POST", "/projects", {"id": "x-unsigned"}, sign=False)[0] == 401
        status, body, _ = c.call("GET", "/projects")
        assert status == 200 and body == {"projects": []}
        stale = server_mod.sign_for_tests("s3cret", "GET", PREFIX + "/projects", b"", ts=int(datetime.now(timezone.utc).timestamp()) - 3600)
        assert c.call("GET", "/projects", headers={"X-KiroCrew-Proxy": stale}, sign=False)[0] == 401
    finally:
        srv.shutdown()
        srv.server_close()


def test_verify_signature_rejects_body_tampering():
    sig = server_mod.sign_for_tests("k", "POST", "/x", b'{"a":1}')
    assert server_mod.verify_proxy_signature("k", sig, "POST", ["/x"], b'{"a":1}')
    assert not server_mod.verify_proxy_signature("k", sig, "POST", ["/x"], b'{"a":2}')
    assert not server_mod.verify_proxy_signature("", sig, "POST", ["/x"], b'{"a":1}')


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def test_health_and_templates(running):
    c = running["client"]
    status, body, _ = c.call("GET", "/health")
    assert status == 200 and body["status"] == "ok" and body["engine"] is True
    assert c.call("GET", "/templates")[1]["templates"] == ["hr-default", "it-helpdesk", "maintenance", "blank"]


def test_project_validation_rejects_bad_ids_and_duplicates(running, project):
    c = running["client"]
    assert c.call("POST", "/projects", {"id": "Bad_Id"})[0] == 400
    assert c.call("POST", "/projects", {"id": project, "template": "blank"})[0] == 409
    assert c.call("GET", "/projects/nope-nope")[0] == 404
    assert c.call("GET", "/projects/../etc")[0] in (400, 404)


def test_template_project_validates_and_builds(running, project):
    c = running["client"]
    status, v, _ = c.call("POST", f"/projects/{project}/validate")
    assert status == 200 and v["ok"] is True, v
    assert v["summary"] == {"documents": 6, "facts": 12, "goldenCases": 16, "holdout": 9, "pending": 0, "tools": 4}
    status, b, _ = c.call("POST", f"/projects/{project}/build")
    assert status == 200 and b["version"].startswith("acme-it-") and b["files"] > 60, b
    status, rel, _ = c.call("GET", f"/projects/{project}/release")
    assert status == 200 and rel["manifest"]["version"] == b["version"] and rel["fileCount"] == b["files"]
    meta = c.call("GET", f"/projects/{project}")[1]
    assert meta["status"] == "built" and meta["lastBuild"]["version"] == b["version"] and "scenarioYaml" in meta


def test_golden_hides_holdout_unless_instructor(running, project):
    c = running["client"]
    body = c.call("GET", f"/projects/{project}/golden")[1]
    assert len(body["practice"]) == 7 and body["holdoutCount"] == 9 and "holdout" not in body
    body = c.call("GET", f"/projects/{project}/golden?instructor=1")[1]
    assert len(body["holdout"]) == 9 and "instructor-only" in body["warning"]


def test_export_is_a_verified_release_zip(running, project):
    c = running["client"]
    status, data, headers = c.call("GET", f"/projects/{project}/export", raw=True)
    assert status == 200 and headers["Content-Type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "RELEASE.json" in names and "pack/pack.env" in names and not any(n.startswith("instructor/") for n in names)


def test_blank_customer_pack_is_gate_blocked_until_confirmed(running):
    c = running["client"]
    status, meta, _ = c.call("POST", "/projects", {"id": "globex-hr", "template": "blank", "packKind": "customer", "displayName": "Globex HR"})
    assert status == 201 and meta["status"] == "intake"
    status, v, _ = c.call("POST", "/projects/globex-hr/validate")
    assert status == 200 and v["ok"] is False and (v["schema"] or v["gate"])  # empty skeleton cannot pass
    assert c.call("POST", "/projects/globex-hr/build")[0] == 409  # fail closed

    # scenario edits must keep the id and must parse
    assert c.call("PUT", "/projects/globex-hr/scenario", {"yaml": "id: other\n"})[0] == 400
    assert c.call("PUT", "/projects/globex-hr/scenario", {"yaml": "id: [unclosed"})[0] == 400
    yaml_text = c.call("GET", "/projects/globex-hr")[1]["scenarioYaml"].replace("facts: []", "facts:\n  - id: pto-days\n    statement: 15 PTO days per year\n    criticality: blocking\n    provenance: pending\n")
    status, saved, _ = c.call("PUT", "/projects/globex-hr/scenario", {"yaml": yaml_text})
    assert status == 200 and saved["saved"] is True

    # confirming requires evidence for customer_confirmed
    assert c.call("POST", "/projects/globex-hr/items/pto-days/confirm", {"provenance": "customer_confirmed"})[0] == 400
    status, body, _ = c.call("POST", "/projects/globex-hr/items/pto-days/confirm", {"provenance": "customer_confirmed", "confirmedBy": "Jane Doe (Globex HR)", "confirmationRef": "meeting notes 2026-09-01"})
    assert status == 200 and body["provenance"] == "customer_confirmed"
    text = c.call("GET", "/projects/globex-hr")[1]["scenarioYaml"]
    assert "confirmedBy: Jane Doe (Globex HR)" in text and "confirmationRef: meeting notes 2026-09-01" in text
    assert c.call("POST", "/projects/globex-hr/items/missing-item/confirm", {"provenance": "sa_synthetic"})[0] == 404
    # the confirmation fields the app writes must be the schema's own: no schema complaint may mention them
    v = c.call("POST", "/projects/globex-hr/validate")[1]
    assert not any("confirm" in msg for msg in v["schema"]), v["schema"]


def test_calibrate_endpoint_writes_noise_band(running, project):
    c = running["client"]
    log = "  value = 0.83\n  value = 0.80\n  value = 0.86\n"
    assert c.call("POST", f"/projects/{project}/calibrate", {"log": "nothing"})[0] == 400
    assert c.call("POST", f"/projects/{project}/calibrate", {"log": log, "metric": "Bad Metric"})[0] == 400
    status, dry, _ = c.call("POST", f"/projects/{project}/calibrate", {"log": log, "dryRun": True})
    assert status == 200 and dry == {"metric": "thelma_rag_quality", "runs": 3, "mean": 0.83, "std": 0.0245, "spread": 0.06, "noiseBand": 0.06, "verdict": "stable", "applied": False}
    status, applied, _ = c.call("POST", f"/projects/{project}/calibrate", {"log": log})
    assert status == 200 and applied["applied"] is True
    meta = c.call("GET", f"/projects/{project}")[1]
    assert meta["lastCalibration"]["noiseBand"] == 0.06 and meta["lastBuild"] is None  # a calibrated scenario needs a rebuild
    assert "noiseBand:\n    thelma_rag_quality: 0.06" in meta["scenarioYaml"]
    # the calibrated scenario still validates and rebuilds (later tests rely on a build existing)
    assert c.call("POST", f"/projects/{project}/validate")[1]["ok"] is True
    assert c.call("POST", f"/projects/{project}/build")[0] == 200


def test_put_file_stays_inside_project(running, project):
    c = running["client"]
    assert c.call("PUT", f"/projects/{project}/files/agent/notes.md", {"content": "# notes\n"})[0] == 200
    assert c.call("PUT", f"/projects/{project}/files/../escape.md", {"content": "x"})[0] == 400
    assert c.call("PUT", f"/projects/{project}/files/build/build.json", {"content": "{}"})[0] == 400
    assert c.call("PUT", f"/projects/{project}/files/project.json", {"content": "{}"})[0] == 400


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def test_target_rejects_credential_material_and_stores_only_names(running, project):
    c = running["client"]
    assert c.call("PUT", f"/projects/{project}/target", {"profile": "p", "region": "us-west-2", "expectedAccountId": ACCOUNT, "secretAccessKey": "x"})[0] == 400
    assert c.call("PUT", f"/projects/{project}/target", {"profile": "p", "region": "west", "expectedAccountId": ACCOUNT})[0] == 400
    status, target, _ = c.call("PUT", f"/projects/{project}/target", {"profile": "align-workshop", "region": "us-west-2", "expectedAccountId": ACCOUNT, "releaseProject": "align-pilot"})
    assert status == 200 and target == {"profile": "align-workshop", "region": "us-west-2", "expected_account_id": ACCOUNT, "workshop_stack": "workshop-infra", "addons_stack": "workshop-customizer-addons", "contract_parameter": "/workshop-customizer/target-contract", "releaseProject": "align-pilot"}
    on_disk = json.loads((running["data_dir"] / "projects" / project / "project.json").read_text())
    assert "secret" not in json.dumps(on_disk).lower()


def test_apply_requires_preflight_token(running, project):
    c = running["client"]
    # The file-edit test invalidates the prior build. Sync must use a fresh build.
    assert c.call("POST", f"/projects/{project}/build")[0] == 200
    assert c.call("POST", f"/projects/{project}/sync/apply", {"confirmToken": "nope"})[0] == 409  # no preflight yet
    status, pf, _ = c.call("POST", f"/projects/{project}/sync/preflight")
    assert status == 200 and pf["ok"] is True and pf["confirmToken"], pf
    assert pf["plan"]["instanceId"] == INSTANCE and pf["plan"]["bucket"] == BUCKET and pf["plan"]["documentName"] == DOC
    assert pf["plan"]["sourceUri"].startswith(f"s3://{BUCKET}/customizer-releases/align-pilot/acme-it-")
    stubs = running["stubs"]["clients"]
    assert "ssm.send_command" not in stubs.names() and "s3.put_object" not in stubs.names()

    assert c.call("POST", f"/projects/{project}/sync/apply", {"confirmToken": "wrong"})[0] == 403
    status, applied, _ = c.call("POST", f"/projects/{project}/sync/apply", {"confirmToken": pf["confirmToken"]})
    assert status == 200 and applied["status"] == "pending-commit" and applied["commandId"] == "cmd-123", applied
    names = stubs.names()
    assert names.index("s3.put_object") < names.index("ssm.send_command")
    send = next(call[1] for call in stubs.calls if call[0] == "ssm.send_command")
    assert send["DocumentName"] == DOC and send["Parameters"]["Action"] == ["apply"]

    # token is single-use
    assert c.call("POST", f"/projects/{project}/sync/apply", {"confirmToken": pf["confirmToken"]})[0] == 409
    meta = c.call("GET", f"/projects/{project}")[1]
    assert meta["status"] == "synced-pending-commit"
    # The synced namespace is remembered: a later release with another one must find its resources gone.
    assert [ns["agentName"] for ns in running["service"].store.read_meta(project)["syncedNamespaces"]] == ["itassistant"]
    assert any(c_["name"] == "stack.addons.namespace" and c_["status"] == "ok" for c_ in pf["checks"])


def test_commit_and_history(running, project):
    c = running["client"]
    stubs = running["stubs"]["clients"]
    stubs.command_invocations = [{"Status": "Success", "StandardOutputContent": json.dumps({"status": "committed", "version": "acme-it-x"})}]
    status, body, _ = c.call("POST", f"/projects/{project}/sync/commit", {})
    assert status == 200 and body["applier"]["status"] == "committed"
    assert c.call("GET", f"/projects/{project}")[1]["status"] == "synced"
    assert c.call("POST", f"/projects/{project}/sync/apply-now", {})[0] == 404  # unknown action is not a route
    history = c.call("GET", f"/projects/{project}/sync/history")[1]["history"]
    assert [h["event"] for h in history] == ["preflight", "apply", "commit"]


def test_rollback_refuses_rolled_back_claim_while_aws_resources_exist(running, project):
    c = running["client"]
    stubs = running["stubs"]["clients"]
    stubs.command_invocations = [{"Status": "Success", "StandardOutputContent": json.dumps({"status": "rolled-back", "version": "acme-it-x"})}]
    stubs._existing_params.add("/app/it/knowledge_base_id")  # scenario KB already created on the workshop account
    status, body, _ = c.call("POST", f"/projects/{project}/sync/rollback", {"reason": "smoke failed"})
    assert status == 200 and body["status"] == "rolled-back-files-only" and body["awsResourcesPresent"] is True
    assert any(g["name"] == "guard.knowledge_base_id" and g["status"] == "fail" for g in body["resourceGuard"])
    assert c.call("GET", f"/projects/{project}")[1]["status"] == "rollback-incomplete"

    stubs._existing_params.clear()
    status, body, _ = c.call("POST", f"/projects/{project}/sync/rollback", {"reason": "retry"})
    assert status == 200 and body["status"] == "rolled-back" and body["awsResourcesPresent"] is False
    assert c.call("GET", f"/projects/{project}")[1]["status"] == "rolled-back"
    events = [h["event"] for h in c.call("GET", f"/projects/{project}/sync/history")[1]["history"]]
    assert events[-2:] == ["rollback", "rollback"]


def test_guided_run_requires_build_and_persists(running, project):
    c = running["client"]
    # Self-contained: never depend on another test having built the shared project.
    assert c.call("POST", f"/projects/{project}/validate")[1]["ok"] is True
    assert c.call("POST", f"/projects/{project}/build")[0] == 200
    status, state, _ = c.call("GET", f"/projects/{project}/run")
    assert status == 200 and state["projectId"] == project
    assert state["releaseVersion"].startswith("acme-it-")
    assert len(state["steps"]) == 15
    assert state["stepOrder"][0] == "setup" and state["stepOrder"][-1] == "judge-stability"
    created = state["createdAt"]
    assert c.call("GET", f"/projects/{project}/run")[1]["createdAt"] == created

    status, _meta, _ = c.call("POST", "/projects", {"id": "run-unbuilt", "template": "blank", "packKind": "customer"})
    assert status == 201
    status, body, _ = c.call("GET", "/projects/run-unbuilt/run")
    assert status == 409 and "build" in body["error"]


def test_guided_run_dispatch_poll_and_retry_fail_closed(running):
    c = running["client"]
    project_id = "run-exec"
    status, body, _ = c.call("POST", "/projects", {"id": project_id, "template": "it-helpdesk", "packKind": "reference"})
    assert status == 201, body
    assert c.call("POST", f"/projects/{project_id}/validate")[1]["ok"] is True
    assert c.call("POST", f"/projects/{project_id}/build")[0] == 200

    # A build alone cannot execute remotely; target + preflight + committed sync are mandatory.
    assert c.call("POST", f"/projects/{project_id}/run/next", {})[0] == 409
    assert c.call("PUT", f"/projects/{project_id}/target", {"profile": "align-workshop", "region": "us-west-2", "expectedAccountId": ACCOUNT})[0] == 200
    status, preflight, _ = c.call("POST", f"/projects/{project_id}/sync/preflight")
    assert status == 200 and preflight["ok"] is True
    stubs = running["stubs"]["clients"]
    stubs.command_invocations = [{"Status": "Success", "StandardOutputContent": json.dumps({"status": "pending-commit", "version": "x"})}]
    assert c.call("POST", f"/projects/{project_id}/sync/apply", {"confirmToken": preflight["confirmToken"]})[0] == 200
    assert c.call("POST", f"/projects/{project_id}/run/next", {})[0] == 409
    stubs.command_invocations = [{"Status": "Success", "StandardOutputContent": json.dumps({"status": "committed", "version": "x"})}]
    assert c.call("POST", f"/projects/{project_id}/sync/commit", {})[0] == 200

    # Hard gate: the add-ons stack must run the checked-in RunStep document (recorded at preflight).
    preflight_path = running["data_dir"] / "projects" / project_id / "sync" / "last-preflight.json"
    recorded = json.loads(preflight_path.read_text(encoding="utf-8"))
    assert recorded["plan"]["runDocumentSha256"] == teaching_run.run_document_sha256()
    stale = json.loads(json.dumps(recorded))
    stale["plan"]["runDocumentSha256"] = "0" * 64
    preflight_path.write_text(json.dumps(stale), encoding="utf-8")
    sends_before = stubs.names().count("ssm.send_command")
    status, body, _ = c.call("POST", f"/projects/{project_id}/run/next", {})
    assert status == 409 and "RunStep document" in body["error"] and "redeploy" in body["error"]
    assert stubs.names().count("ssm.send_command") == sends_before
    preflight_path.write_text(json.dumps(recorded), encoding="utf-8")

    # Dependencies and the allowlist are enforced before SSM dispatch.
    assert c.call("POST", f"/projects/{project_id}/run/steps/knowledge-base/start", {})[0] == 409
    assert c.call("POST", f"/projects/{project_id}/run/steps/cleanup/start", {})[0] == 400
    sends_before = stubs.names().count("ssm.send_command")
    status, state, _ = c.call("POST", f"/projects/{project_id}/run/next", {})
    assert status == 200 and state["currentStepId"] == "setup"
    assert state["steps"]["setup"]["commandId"] == "cmd-123"
    assert stubs.names().count("ssm.send_command") == sends_before + 1
    send = [payload for name, payload in stubs.calls if name == "ssm.send_command"][-1]
    assert send["DocumentName"] == "WorkshopCustomizerRunStep"
    assert send["Parameters"]["StepId"] == ["setup"] and send["Parameters"]["Script"] == ["00-setup.sh"]
    assert c.call("POST", f"/projects/{project_id}/run/next", {})[0] == 409
    assert stubs.names().count("ssm.send_command") == sends_before + 1

    stubs.command_invocations = [{"Status": "InProgress"}]
    state = c.call("POST", f"/projects/{project_id}/run/poll", {})[1]
    assert state["steps"]["setup"]["ssmStatus"] == "InProgress"
    stubs.command_invocations = [{"Status": "Success", "DocumentVersion": "7", "StandardOutputContent": json.dumps({"status": "passed", "summary": "setup complete", "outputs": {"python": "ok"}})}]
    state = c.call("POST", f"/projects/{project_id}/run/poll", {})[1]
    assert state["status"] == "in_progress" and state["currentStepId"] is None
    assert state["steps"]["setup"]["status"] == "passed"
    assert state["steps"]["setup"]["outputs"] == {"python": "ok"}
    assert state["steps"]["setup"]["documentVersion"] == "7"

    # A failed dependency blocks all later steps; Run next retries that same failed step.
    assert c.call("POST", f"/projects/{project_id}/run/next", {})[1]["currentStepId"] == "infra"
    stubs.command_invocations = [{"Status": "Failed", "StandardErrorContent": "stack failed"}]
    state = c.call("POST", f"/projects/{project_id}/run/poll", {})[1]
    assert state["status"] == "failed" and state["steps"]["infra"]["status"] == "failed"
    assert state["steps"]["knowledge-base"]["status"] == "blocked"
    state = c.call("POST", f"/projects/{project_id}/run/next", {})[1]
    assert state["currentStepId"] == "infra" and state["steps"]["infra"]["error"] == ""
    stubs.command_invocations = [{"Status": "Success", "StandardOutputContent": json.dumps({"status": "passed", "summary": "infra complete"})}]
    state = c.call("POST", f"/projects/{project_id}/run/poll", {})[1]
    assert state["steps"]["infra"]["status"] == "passed"
    assert state["steps"]["knowledge-base"]["status"] == "blocked"

    assert c.call("GET", f"/projects/{project_id}/run/report")[0] == 409
    pack = teaching_run.scenario("it-helpdesk")
    snapshot = running["data_dir"] / "projects" / project_id / "build" / "instructor" / "scenario-snapshot.yaml"
    for expected_step in state["stepOrder"][2:]:
        started = c.call("POST", f"/projects/{project_id}/run/next", {})[1]
        assert started["currentStepId"] == expected_step
        outputs = {"script": started["steps"][expected_step]["script"], "exitCode": 0}
        if expected_step in ("baseline", "optimize"):
            outputs = teaching_run.complete_outputs(pack, expected_step, script=outputs["script"])
            if expected_step == "baseline":
                # Evidence is judged against the build snapshot: a baseline without per-case L1 fails
                # even though the script exited 0; the retry then delivers complete evidence.
                stubs.command_invocations = [{"Status": "Success", "StandardOutputContent": json.dumps({"status": "passed", "summary": "baseline complete", "outputs": {k: v for k, v in outputs.items() if k != "l1"}})}]
                failed = c.call("POST", f"/projects/{project_id}/run/poll", {})[1]
                assert failed["steps"]["baseline"]["status"] == "failed"
                assert "L1 scenario assertions (every practice case) from 09-run-eval.sh" in failed["steps"]["baseline"]["error"]
                assert c.call("POST", f"/projects/{project_id}/run/next", {})[1]["currentStepId"] == "baseline"
        elif expected_step == "cost-latency":
            outputs["costLatency"] = {"averageLatencySeconds": 1.2, "averageInputTokens": 100, "averageOutputTokens": 40, "totalCostUsd": 0.001}
        elif expected_step == "models":
            outputs.update({"models": {"baseline": "model-a", "comparison": "model-b"}, "scores": [{"evaluator": "thelma_rag_quality", "traceId": "model", "value": 0.75, "label": "Pass"}], "costLatency": {"averageLatencySeconds": 0.9, "totalCostUsd": 0.0008}})
        elif expected_step == "judge-stability":
            outputs["judgeStability"] = {"values": [0.8, 0.82, 0.79], "mean": 0.803, "std": 0.012, "spread": 0.03, "verdict": "stable"}
            # The report reads the scenario the release was built from, never the live scenario.yaml.
            text = snapshot.read_text(encoding="utf-8")
            assert "retrievalToolName: retrieve_it_policy\n" in text
            snapshot.write_text(text.replace("retrievalToolName: retrieve_it_policy", "retrievalToolName: retrieve_it_policy_snapshot", 1), encoding="utf-8")
        stubs.command_invocations = [{"Status": "Success", "StandardOutputContent": json.dumps({"status": "passed", "summary": f"{expected_step} complete", "outputs": outputs})}]
        state = c.call("POST", f"/projects/{project_id}/run/poll", {})[1]
        assert state["steps"][expected_step]["status"] == "passed", state["steps"][expected_step]["error"]

    assert state["status"] == "passed" and state["report"]["status"] == "complete", state["report"]["completion"]
    status, report, _ = c.call("GET", f"/projects/{project_id}/run/report")
    assert status == 200 and report["completion"]["passed"] == 15
    assert report["quality"]["delta"]["thelma_rag_quality"]["delta"] == 0.2
    assert report["agentEvidence"]["retrievalTool"] == "retrieve_it_policy_snapshot"
    assert report["teachingContrast"]["schema"] == "workshop-customizer/teaching-contrast/1"
    assert [row["caseId"] for row in report["caseTable"]["cases"]] == [c["id"] for c in pack["evaluation"]["goldenSet"] if c["set"] == "practice"]
    report_path = running["data_dir"] / "projects" / project_id / "run" / "report.json"
    assert json.loads(report_path.read_text())["status"] == "complete"


def test_an_installed_app_finds_its_home_where_kirocrew_installed_it_from(tmp_path, monkeypatch):
    """KiroCrew's installed copy holds only app/: the engine is in the checkout it was installed from (a path source's
    parent), or for a registry install (registry:<name>, subdirectory app) in KiroCrew's persistent clone of the
    repository, ~/.kiro/crew/app-sources/<name>."""
    monkeypatch.delenv("WORKSHOP_CUSTOMIZER_HOME", raising=False)
    crew = tmp_path / "crew"
    installed = crew / "apps" / "workshop-customizer"
    installed.mkdir(parents=True)

    def checkout(root: Path) -> Path:
        (root / "engine" / "workshop_customizer").mkdir(parents=True)
        (root / "template-lock.json").write_text("{}", encoding="utf-8")
        return root

    clone = checkout(crew / "app-sources" / "workshop-customizer")
    (installed / "installed.json").write_text(json.dumps({"source": "registry:workshop-customizer"}), encoding="utf-8")
    assert server_mod.installed_homes(installed) == [clone]
    monkeypatch.setattr(server_mod, "APP_DIR", installed)
    assert server_mod.resolve_home(tmp_path / "data") == clone.resolve()

    repo = checkout(tmp_path / "repo")
    (installed / "installed.json").write_text(json.dumps({"source": str(repo / "app")}), encoding="utf-8")
    assert server_mod.installed_homes(installed) == [repo] and server_mod.resolve_home(tmp_path / "data") == repo.resolve()
    (installed / "installed.json").write_text("not json", encoding="utf-8")
    assert server_mod.installed_homes(installed) == []  # a damaged file: the other candidates decide
