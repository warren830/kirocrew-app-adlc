"""P0b: draft-mode validation in the app backend (enforce_gate=False, policy always runs with the
scenario root, dry build, build/validate.json bound to scenario sha256 + pack digest) and the 409
mapping of output-check / render failures in build and one-click."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_app_backend import server_mod, wait_job  # noqa: E402
from test_policy_seam import Recorder  # noqa: E402

PID = "draft-check"


def _no_aws(cfg):  # nothing in these tests may reach AWS
    raise AssertionError("AWS clients must not be created")


@pytest.fixture
def svc(tmp_path):
    service = server_mod.Service(tmp_path / "data", home=REPO_ROOT, clients_factory=_no_aws)
    service.create_project({"id": PID, "template": "it-helpdesk", "packKind": "reference"})
    return service


def _data(svc) -> dict:
    return yaml.safe_load(svc.get_project(PID)["scenarioYaml"])


def _save(svc, data: dict) -> None:
    svc.put_scenario(PID, {"yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True)})


def _all_ai_draft(data: dict) -> dict:
    for group in (data["facts"], data["tools"], data["knowledge"]["documents"], data["evaluation"]["goldenSet"]):
        for item in group:
            item["provenance"] = "ai_draft"
            for key in ("confirmedBy", "confirmedAt", "confirmationRef"):
                item.pop(key, None)
    return data


def _leak_holdout_into_a_document(svc) -> str:
    data = _data(svc)
    holdout = next(c for c in data["evaluation"]["goldenSet"] if c["set"] == "holdout")
    rel = data["knowledge"]["documents"][0]["file"]
    pdir = svc.store.project_dir(PID)
    text = (pdir / rel).read_text(encoding="utf-8")
    svc.put_file(PID, rel, text + "\n" + holdout["query"] + "\n")
    return rel


def _policy_errors(v: dict) -> list[dict]:
    """Policy errors only: the IT template carries one pinned advisory teaching warning."""
    return [f for f in v["policy"] if f["severity"] == "error"]


def _leftovers(svc) -> list[str]:
    build = svc.store.project_dir(PID) / "build"
    return [p.name for p in build.iterdir() if p.name.startswith((".dry-", ".staging-", ".retired-"))] if build.exists() else []


def test_reference_template_passes_draft_validation_with_a_dry_build(svc):
    v = svc.validate(PID)
    assert v["ok"] is True and v["mode"] == "draft", v
    assert v["dryBuild"]["requested"] and v["dryBuild"]["ran"] and v["dryBuild"]["skipped"] is None
    assert v["gate"] == [] and v["gateWaived"] == [] and v["schema"] == [] and v["schemaKind"] is None
    assert v["output"] == [] and v["render"] == []
    assert v["summary"] == {"documents": 6, "facts": 12, "goldenCases": 16, "holdout": 9, "pending": 0, "tools": 4}
    assert not _leftovers(svc) and not (svc.store.project_dir(PID) / "build" / "release").exists()
    meta = svc.store.read_meta(PID)
    assert meta["status"] == "validated"
    assert meta["lastValidation"] | {"at": None} == {
        "at": None, "ok": True, "gate": 0, "schema": 0, "policyErrors": 0, "outputErrors": 0, "renderErrors": 0, "dryBuild": True,
        "workspaceErrors": 0, "repairable": 0, "saOnly": 0, "anchorsMissing": [],
    }
    # P4: a template project holds scenario.yaml and its referenced files only (no generator script).
    assert v["workspace"] == [] and not (svc.store.project_dir(PID) / "generate_content.py").exists()
    assert v["repair"]["suggestedScopes"] == [] and v["repair"]["saOnly"] == []
    assert all(f["id"].startswith("F") and f["scopes"] for f in v["repair"]["repairable"])
    assert v["anchors"]["packKind"] == "reference" and v["classes"]["synthetic-setting"] > 0


def test_all_ai_draft_draft_shows_policy_findings_next_to_gate_violations(svc):
    data = _all_ai_draft(_data(svc))
    holdout = [c for c in data["evaluation"]["goldenSet"] if c["set"] == "holdout"]
    data["evaluation"]["goldenSet"] = [c for c in data["evaluation"]["goldenSet"] if c not in holdout[:5]]  # 11 cases
    _save(svc, data)
    v = svc.validate(PID)
    assert v["ok"] is False and v["schema"] == []
    assert v["gate"] and all(g["reason"] == "provenance 'ai_draft' cannot enter a formal pack" for g in v["gate"])
    too_few = [f for f in v["policy"] if f["code"] == "golden.too_few"]
    assert too_few and too_few[0]["severity"] == "error" and too_few[0]["scopes"] == ["golden"]
    assert v["dryBuild"] == {"requested": True, "ran": False, "skipped": "policy errors", "seconds": 0.0}
    assert v["summary"]["pending"] == 6 + 12 + 4 + 11
    meta = svc.store.read_meta(PID)["lastValidation"]
    assert meta["gate"] == len(v["gate"]) and meta["policyErrors"] >= 1


def test_all_ai_draft_draft_with_clean_policy_still_gets_a_dry_build(svc):
    _save(svc, _all_ai_draft(_data(svc)))
    v = svc.validate(PID)
    assert v["ok"] is False and v["gate"] and not _policy_errors(v)
    assert v["dryBuild"]["ran"] and v["output"] == [] and v["render"] == []
    with pytest.raises(server_mod.HttpError) as excinfo:  # the real build stays fail-closed on the gate
        svc.build(PID)
    assert excinfo.value.status == 409


def test_dry_build_surfaces_output_findings_that_a_plain_validation_cannot_see(svc):
    rel = _leak_holdout_into_a_document(svc)
    v = svc.validate(PID)
    assert v["ok"] is False and not _policy_errors(v) and v["dryBuild"]["ran"]
    leaked = [f for f in v["output"] if f["code"] == "holdout.leaked"]
    assert leaked and leaked[0]["stage"] == "compile" and leaked[0]["scopes"] == ["knowledge"]
    assert leaked[0]["path"] == "knowledge-base/docs/" + Path(rel).name
    assert svc.store.read_meta(PID)["lastValidation"]["outputErrors"] == len(leaked)
    assert not _leftovers(svc)

    plain = svc.validate(PID, dry_build=False)
    assert plain["ok"] is True and plain["dryBuild"] == {"requested": False, "ran": False, "skipped": "not requested", "seconds": 0.0}


def test_build_maps_output_check_failures_to_409_with_findings(svc):
    _leak_holdout_into_a_document(svc)
    with pytest.raises(server_mod.HttpError) as excinfo:
        svc.build(PID)
    assert excinfo.value.status == 409
    findings = excinfo.value.payload["findings"]
    assert any(f["code"] == "holdout.leaked" and f["scopes"] == ["knowledge"] for f in findings)
    assert not _leftovers(svc) and svc.store.read_meta(PID)["lastBuild"] is None

    status, body, _raw, _headers = _route(svc, "POST", f"/projects/{PID}/build")
    assert status == 409 and "output checks" in body["error"] and body["findings"]


def test_build_and_dry_build_map_render_errors(svc, monkeypatch):
    render = svc.engine["render"]

    def broken(pack, upstream, release_dir, *, template_commit):
        raise render.RenderError("09-run-eval.sh: anchor 'recent-n' matched 0 times, expected 1")

    monkeypatch.setattr(render, "render_release", broken)
    v = svc.validate(PID)
    assert v["ok"] is False and v["render"] == ["09-run-eval.sh: anchor 'recent-n' matched 0 times, expected 1"]
    assert svc.store.read_meta(PID)["lastValidation"]["renderErrors"] == 1
    with pytest.raises(server_mod.HttpError) as excinfo:
        svc.build(PID)
    assert excinfo.value.status == 409 and excinfo.value.payload["render"] == v["render"]
    assert not _leftovers(svc)


def test_dry_build_engine_crash_is_a_finding_not_a_500(svc, monkeypatch):
    compiler = svc.engine["compiler"]

    def crash(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(compiler, "compile_pack", crash)
    v = svc.validate(PID)
    assert v["ok"] is False and v["render"] == ["dry build error: OSError: disk full"] and not _leftovers(svc)


def test_validate_json_is_bound_to_scenario_sha_and_pack_digest(svc):
    pdir = svc.store.project_dir(PID)
    # No rehearsal of the current release: the repair panel is told no R findings will be added.
    assert svc.validation(PID) == {"validation": None, "fresh": False, "rehearsalFindings": []}
    v = svc.validate(PID)
    on_disk = json.loads((pdir / "build" / "validate.json").read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(v))
    assert v["scenarioSha256"] == hashlib.sha256((pdir / "scenario.yaml").read_bytes()).hexdigest()
    assert v["packDigest"] == server_mod.pack_digest(pdir) == svc._source_hash(PID)
    assert svc.validation(PID) == {"validation": on_disk, "fresh": True, "rehearsalFindings": []}

    svc.put_file(PID, "agent/baseline-prompt.md", "A changed baseline prompt that uses retrieve_it_policy.\n")
    assert svc.validation(PID)["fresh"] is False  # a pack file changed
    svc.validate(PID, dry_build=False)
    assert svc.validation(PID)["fresh"] is True
    data = _data(svc)
    data["description"] += " Edited."
    _save(svc, data)
    stale = svc.validation(PID)
    assert stale["fresh"] is False and stale["validation"]["scenarioSha256"] != hashlib.sha256((pdir / "scenario.yaml").read_bytes()).hexdigest()

    (pdir / "build" / "validate.json").write_text("{not json", encoding="utf-8")
    assert svc.validation(PID) == {"validation": None, "fresh": False, "rehearsalFindings": []}


def test_schema_and_cross_reference_errors_are_listed_one_per_entry(svc):
    data = _data(svc)
    data["namespace"]["agentName"] = "Bad-Name!"
    data["tools"][0]["description"] = ""
    _save(svc, data)
    v = svc.validate(PID)
    assert v["ok"] is False and v["schemaKind"] == "schema" and len(v["schema"]) >= 2
    assert any(e.startswith("namespace/agentName") for e in v["schema"])
    assert v["policy"] == [] and v["dryBuild"]["skipped"] == "the scenario did not load" and "summary" not in v

    data = _data(svc)
    data["namespace"]["agentName"] = "itassistant"
    data["tools"][0]["description"] = "Search the IT knowledge base"
    data["evaluation"]["goldenSet"][0]["basis"] = ["no-such-fact"]
    _save(svc, data)
    v = svc.validate(PID)
    assert v["schemaKind"] == "xref" and any("cites unknown fact 'no-such-fact'" in e for e in v["schema"])

    svc.store.project_dir(PID).joinpath("scenario.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    v = svc.validate(PID)
    assert v["schemaKind"] == "load" and v["schema"][0].startswith("loader error:")


def test_waived_gate_violations_are_reported_without_blocking(svc):
    data = _data(svc)
    data["packKind"] = "customer"
    for group in (data["facts"], data["tools"], data["knowledge"]["documents"], data["evaluation"]["goldenSet"]):
        for item in group:
            item.update(provenance="customer_confirmed", confirmedBy="Jane Doe (ACME IT)",
                        confirmedAt="2026-09-01T10:00:00Z", confirmationRef="meeting 2026-09-01")
    for doc in data["knowledge"]["documents"]:  # documents carry provenance only
        for key in ("confirmedBy", "confirmedAt", "confirmationRef"):
            doc.pop(key)
    fact = data["facts"][0]
    fact["provenance"] = "sa_synthetic"
    for key in ("confirmedBy", "confirmedAt", "confirmationRef"):
        fact.pop(key)
    data["governance"] = {"exceptions": [{"itemId": fact["id"], "reason": "legal review pending",
                                          "approvedBy": ["Workshop Owner", "SA"], "approvedAt": "2026-09-05T09:00:00Z"}]}
    _save(svc, data)
    v = svc.validate(PID, dry_build=False)
    assert v["ok"] is True and v["gate"] == []
    assert [g["item_id"] for g in v["gateWaived"]] == [fact["id"]] and v["gateWaived"][0]["waived"] is True


def test_validate_passes_the_project_root_to_the_policy(svc, monkeypatch):
    validator = svc.engine["validator"]
    rec = Recorder()
    monkeypatch.setattr(validator, "POLICY_CHECKS", validator.POLICY_CHECKS + (validator.PolicySubCheck("recorder", rec, ("labs",)),))
    svc.validate(PID, dry_build=False)
    assert rec.roots == [svc.store.project_dir(PID)]
    rec.roots.clear()
    svc.validate(PID)  # the dry build's compile_pack runs the policy again with the same root
    assert rec.roots == [svc.store.project_dir(PID)] * 2


def test_build_and_oneclick_validate_without_a_dry_build(svc, monkeypatch):
    calls: list[bool] = []
    original = svc.validate

    def spy(project_id, *, dry_build=True):
        calls.append(dry_build)
        return original(project_id, dry_build=dry_build)

    monkeypatch.setattr(svc, "validate", spy)
    svc.build(PID)
    assert calls == [False]


def _route(svc, method: str, path: str, body: dict | None = None, query: dict | None = None):
    try:
        return server_mod.route(svc, method, path, query or {}, body or {})
    except server_mod.HttpError as exc:
        return exc.status, exc.payload, None, {}


def test_validate_and_validation_routes(svc):
    status, body, _, _ = _route(svc, "POST", f"/projects/{PID}/validate", {"dryBuild": "yes"})
    assert status == 400 and "dryBuild" in body["error"]
    status, body, _, _ = _route(svc, "POST", f"/projects/{PID}/validate", {"dryBuild": False})
    assert status == 200 and body["ok"] is True and body["dryBuild"]["ran"] is False
    status, body, _, _ = _route(svc, "POST", f"/projects/{PID}/validate")
    assert status == 200 and body["dryBuild"]["ran"] is True
    status, body, _, _ = _route(svc, "GET", f"/projects/{PID}/validation")
    assert status == 200 and body["fresh"] is True and body["validation"]["at"]


def test_oneclick_build_stage_reports_findings_instead_of_an_internal_error(tmp_path):
    service = server_mod.Service(tmp_path / "data", home=REPO_ROOT, clients_factory=_no_aws)
    service.create_project({"id": PID, "template": "it-helpdesk", "packKind": "reference"})
    service.put_target(PID, {"profile": "align-workshop", "region": "us-west-2", "expectedAccountId": "123456789012"})
    _leak_holdout_into_a_document(service)
    job = service.start_oneclick(PID, {"acknowledged": True})
    wait_job(service.jobs, job, timeout=180)
    done = service.jobs.get(PID, job["id"])
    stages = {s["id"]: s for s in done["stages"]}
    assert done["status"] == "failed" and "internal error" not in done["error"], done
    assert stages["validate"]["status"] == "succeeded" and stages["build"]["status"] == "failed"
    assert any(f["code"] == "holdout.leaked" for f in stages["build"]["detail"]["findings"])
    assert stages["preflight"]["status"] == "not-run"


@pytest.mark.parametrize("kind", ["workshop"])
def test_workshop_pack_kind_plumbing_in_the_app(tmp_path, kind):
    service = server_mod.Service(tmp_path / "data", home=REPO_ROOT, clients_factory=_no_aws)
    meta = service.create_project({"id": "class-pack", "template": "it-helpdesk", "packKind": kind})
    assert meta["packKind"] == kind and "packKind: workshop" in service.get_project("class-pack")["scenarioYaml"]
    v = service.validate("class-pack", dry_build=False)
    assert v["ok"] is False and v["gate"]  # unlabeled sa_synthetic blocking items and no customer anchors (D12)
    assert any("workshop pack must carry origin" in g["reason"] for g in v["gate"])
    assert any(g["item_id"] == "customer-anchors" and not g["waived"] for g in v["gate"])
    with pytest.raises(server_mod.HttpError) as excinfo:
        service.create_project({"id": "bad-kind", "template": "blank", "packKind": "class"})
    assert excinfo.value.status == 400
    data = yaml.safe_load(service.get_project("class-pack")["scenarioYaml"])
    data["packKind"] = "reference"
    service.put_scenario("class-pack", {"yaml": yaml.safe_dump(data, sort_keys=False)})
    assert service.store.read_meta("class-pack")["packKind"] == "reference"
    data["packKind"] = "workshop"
    service.put_scenario("class-pack", {"yaml": yaml.safe_dump(data, sort_keys=False)})
    assert service.store.read_meta("class-pack")["packKind"] == "workshop"
