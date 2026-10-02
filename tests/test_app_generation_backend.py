"""P4 process backend (app/backend/server.py): customer materials routes, the materials approval,
workshop review rules (origin labels, no downgrade, no simulated confirmation), list_items classes
and anchors, editLog, the shared generation/.lock, and the repair classification persisted in
build/validate.json.  In-process server on loopback; no AWS, no model."""

from __future__ import annotations

import base64
import io
import json
import sys
import threading
import zipfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_app_backend import Client, server_mod  # noqa: E402
from test_scenario import CONFIRMATION  # noqa: E402

from workshop_customizer import materials as mat  # noqa: E402

PID = "acme-loyalty"


def _no_aws(cfg):  # nothing in these tests may reach AWS
    raise AssertionError("AWS clients must not be created")


@pytest.fixture()
def app(tmp_path):
    srv, svc = server_mod.create_server(tmp_path / "data", home=REPO_ROOT, clients_factory=_no_aws)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = Client(srv.server_address[1])
    yield {"client": client, "service": svc, "data": tmp_path / "data"}
    srv.shutdown()
    srv.server_close()


def _create(app, *, template: str = "it-helpdesk", pack_kind: str = "reference", pid: str = PID) -> Path:
    status, body, _ = app["client"].call("POST", "/projects", {"id": pid, "template": template, "packKind": pack_kind})
    assert status == 201, body
    return app["service"].store.project_dir(pid)


def _upload(app, name: str, content: bytes, **extra):
    body = {"name": name, "contentBase64": base64.b64encode(content).decode(), "acknowledged": True, **extra}
    return app["client"].call("POST", f"/projects/{PID}/materials", body)[:2]


RULES = ("# Loyalty points rules\n\nPoints post 48 hours after purchase. Gold tier starts at 5000 points.\n"
         "Receipts older than 30 days cannot be claimed.\n").encode()


# ---------------------------------------------------------------------------
# materials
# ---------------------------------------------------------------------------


def test_upload_http_limits_and_ack(app, monkeypatch):
    pdir = _create(app)
    client = app["client"]
    status, body = app["client"].call("POST", f"/projects/{PID}/materials",
                                      {"name": "rules.md", "contentBase64": base64.b64encode(RULES).decode()})[:2]
    assert status == 400 and "acknowledged" in body["error"]
    status, body = _upload(app, "tool.exe", b"MZ\x90")
    assert status == 415
    status, body = _upload(app, "fake.docx", b"not a zip")
    assert status == 415
    status, body = client.call("POST", f"/projects/{PID}/materials", {"name": "x.md", "contentBase64": "%%%", "acknowledged": True})[:2]
    assert status == 400 and "base64" in body["error"]
    secret = RULES + b"\nkey AKIA" + b"ABCDEFGHIJKLMNOP" + b"\nid " + b"110101" + b"19900307" + b"4514\n"
    status, body = _upload(app, "leak.md", secret)
    assert status == 422 and {f["code"] for f in body["findings"]} == {"aws-access-key-id", "cn-national-id"}
    assert "AKIA" not in json.dumps(body) and "110101" not in json.dumps(body)  # content never echoed
    assert not (pdir / "materials").exists()

    status, record = _upload(app, "../Loyalty Rules.md", RULES, generationUse="source", author="customer", notes="from the kickoff")
    assert status == 201, record
    mid = record["id"]
    assert mid == "mat-" + __import__("hashlib").sha256(RULES).hexdigest()[:12] and record["name"] == "Loyalty Rules.md"
    assert record["extraction"]["status"] == "ok" and record["mediaType"] == "md"
    assert (pdir / "materials" / "raw" / f"{mid}.md").read_bytes() == RULES
    assert "Gold tier starts at 5000 points." in (pdir / "materials" / "text" / f"{mid}.txt").read_text()
    index = json.loads((pdir / "materials" / "index.json").read_text())
    assert index["schema"] == "workshop-customizer/materials/1" and [r["id"] for r in index["materials"]] == [mid]
    status, body = _upload(app, "again.md", RULES)
    assert status == 409 and body["existingId"] == mid

    monkeypatch.setattr(mat, "MAX_FILE_BYTES", 100)
    status, body = _upload(app, "big.md", b"x" * 200)
    assert status == 413
    monkeypatch.setattr(mat, "MAX_FILE_BYTES", 5 * 1024 * 1024)
    monkeypatch.setattr(mat, "MAX_FILES", 1)
    status, body = _upload(app, "second.md", b"# another customer note\n")
    assert status == 409 and "at most 1 materials" in body["error"]


def test_materials_routes_list_update_text_delete_and_approval(app):
    pdir = _create(app)
    client = app["client"]
    _status, record = _upload(app, "rules.md", RULES)
    mid = record["id"]
    status, listing, _ = client.call("GET", f"/projects/{PID}/materials")
    gen = server_mod._gen()
    expected = gen.allocate_materials(pdir, gen.material_budget("draft"))
    assert status == 200 and listing["materials"][0]["id"] == mid and listing["policy"] is None
    assert listing["budget"]["perMaterial"] == {mid: {"sentChars": expected[0]["sentChars"], "truncated": False}}
    assert listing["limits"]["maxFiles"] == 20 and ".docx" in listing["limits"]["types"]

    status, text, _ = client.call("GET", f"/projects/{PID}/materials/{mid}/text?offset=2&limit=7")
    assert status == 200 and text["text"] == "Loyalty" and text["chars"] == len(RULES.decode())
    status, updated, _ = client.call("PUT", f"/projects/{PID}/materials/{mid}", {"generationUse": "background", "notes": "tone only"})
    assert status == 200 and updated["generationUse"] == "background" and updated["notes"] == "tone only"
    assert client.call("PUT", f"/projects/{PID}/materials/{mid}", {"generationUse": "sometimes"})[0] == 400

    status, policy, _ = client.call("POST", f"/projects/{PID}/materials/approval", {"approvalRef": "x", "dataClassification": "internal"})
    assert status == 400
    status, policy, _ = client.call("POST", f"/projects/{PID}/materials/approval",
                                    {"approvalRef": "ACME legal email 2026-09-01", "dataClassification": "internal"})
    assert status == 200 and policy["dataClassification"] == "internal" and policy["recordedAt"]
    assert app["service"].get_project(PID)["materialsPolicy"]["approvalRef"] == "ACME legal email 2026-09-01"

    data = yaml.safe_load((pdir / "scenario.yaml").read_text())
    data["facts"][0]["origin"] = {"kind": "customer_material", "materials": [mid]}
    app["service"].put_scenario(PID, {"yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True)})
    status, deleted, _ = client.call("DELETE", f"/projects/{PID}/materials/{mid}")
    assert status == 200 and deleted == {"deleted": mid, "referencedBy": [data["facts"][0]["id"]]}
    assert not list((pdir / "materials" / "raw").iterdir()) and not (pdir / "materials" / "text" / f"{mid}.txt").exists()
    v = app["service"].validate(PID, dry_build=False)
    unknown = [f for f in v["workspace"] if f["code"] == "provenance.material_unknown"]
    # The template fact is SA-reviewed (sa_synthetic): its origin is the SA's label, fixed in Review.
    assert unknown and unknown[0]["scopes"] == [] and unknown[0]["saOnly"] is True and v["ok"] is False
    assert any(f["code"] == "provenance.material_unknown" for f in v["repair"]["saOnly"])
    assert client.call("GET", f"/projects/{PID}/materials/mat-000000000000/text")[0] == 404


def test_app_managed_paths_are_refused_whatever_their_case_or_link(app):
    """On a case-insensitive filesystem (macOS APFS) 'Materials/' IS 'materials/': the editor must not write
    material text past the upload scan, change packKind past its audit, or forge run/rehearsal.json or a
    generation snapshot, by spelling or by a symlink inside the project."""
    pdir = _create(app)
    client = app["client"]
    meta_before = (pdir / "project.json").read_bytes()
    scenario_before = (pdir / "scenario.yaml").read_bytes()
    for rel in ("Materials/text/mat-000000000000.txt", "MATERIALS/index.json", "Project.json", "Scenario.yaml",
                "agent/SCENARIO.YAML", "Run/rehearsal.json", "Generation/snapshots/x/scenario.yaml", "Build/build.json",
                "Jobs/x.json", "Sync/plan.json"):
        status, body, _ = client.call("PUT", f"/projects/{PID}/files/{rel}", {"content": "packKind: reference\n"})
        assert status == 400 and "managed by the app" in body["error"], (rel, status, body)
    (pdir / "run").mkdir(exist_ok=True)
    (pdir / "agent").mkdir(exist_ok=True)
    (pdir / "agent" / "link").symlink_to(pdir / "run", target_is_directory=True)
    status, body, _ = client.call("PUT", f"/projects/{PID}/files/agent/link/rehearsal.json", {"content": '{"readyForClass": true}'})
    assert status == 400 and "managed by the app" in body["error"]
    assert not (pdir / "run" / "rehearsal.json").exists()
    assert (pdir / "project.json").read_bytes() == meta_before and (pdir / "scenario.yaml").read_bytes() == scenario_before
    assert client.call("PUT", f"/projects/{PID}/files/agent/notes.md", {"content": "# notes\n"})[0] == 200


def test_materials_never_enter_hash_build_or_export(app):
    pdir = _create(app)
    service = app["service"]
    before = service._source_hash(PID)
    assert _upload(app, "rules.md", RULES)[0] == 201
    assert service._source_hash(PID) == before
    status, body, _ = app["client"].call("PUT", f"/projects/{PID}/files/materials/text/x.txt", {"content": "sneaky"})
    assert status == 400 and "managed by the app" in body["error"]
    assert app["client"].call("PUT", f"/projects/{PID}/files/generation/brief.md", {"content": "x"})[0] == 400
    summary = service.build(PID)
    release = Path(summary["releaseDir"])
    assert not [p for p in release.rglob("*") if "materials" in p.relative_to(release).parts]
    archive, _name = service.export_zip(PID)
    assert not [n for n in zipfile.ZipFile(io.BytesIO(archive)).namelist() if n.startswith("materials/")]


def test_docx_upload_through_the_backend(app):
    _create(app)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                         "<w:body><w:p><w:r><w:t>Points post after 48 hours.</w:t></w:r></w:p></w:body></w:document>")
    status, record = _upload(app, "rules.docx", buf.getvalue())
    assert status == 201 and record["extraction"]["method"] == "docx-xml" and record["mediaType"] == "docx"


def test_malformed_uploads_are_client_errors(app, monkeypatch):
    """Damaged customer files answer 4xx without echoing content; nothing is stored."""
    pdir = _create(app)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
                         "<w:body><w:p><w:r><w:t>" + "hello " * 200 + "</w:t></w:r></w:p></w:body></w:document>")
    raw = bytearray(buf.getvalue())
    at = raw.find(b"word/document.xml") + len("word/document.xml") + 10
    raw[at] ^= 0xFF
    status, body = _upload(app, "bad.docx", bytes(raw))
    assert status == 400 and "damaged" in body["error"], body
    status, record = _upload(app, "big.csv", ('id,text\n1,"' + "x" * 140_000 + '"\n').encode())
    assert status == 201 and record["extraction"]["status"] == "partial", record

    def crash(name, data, **_kw):
        raise KeyError("an extractor bug")

    monkeypatch.setattr(mat, "extract", crash)
    status, body = _upload(app, "notes.md", b"# SA notes\n")
    assert status == 400 and "KeyError" in body["error"] and "extractor bug" not in body["error"]
    assert [r["name"] for r in json.loads((pdir / "materials" / "index.json").read_text())["materials"]] == ["big.csv"]


# ---------------------------------------------------------------------------
# review rules
# ---------------------------------------------------------------------------


def test_simulated_confirmations_are_refused_by_the_app(app):
    _create(app)
    fact = yaml.safe_load(app["service"].get_project(PID)["scenarioYaml"])["facts"][0]["id"]
    for body in ({"confirmedBy": "SIMULATED SA REVIEW (e2e_generate)", "confirmationRef": "meeting 1"},
                 {"confirmedBy": "Ops lead", "confirmationRef": "e2e_generate:run-1"}):
        status, payload, _ = app["client"].call("POST", f"/projects/{PID}/items/{fact}/confirm",
                                                {"provenance": "customer_confirmed", **body})
        assert status == 400 and "simulated confirmation" in payload["error"]


def test_confirm_with_material_id_and_origin(app):
    pdir = _create(app)
    _status, record = _upload(app, "rules.md", RULES)
    mid = record["id"]
    data = yaml.safe_load((pdir / "scenario.yaml").read_text())
    fact, doc = data["facts"][0]["id"], data["knowledge"]["documents"][0]["id"]
    client = app["client"]
    status, body, _ = client.call("POST", f"/projects/{PID}/items/{fact}/confirm",
                                  {"provenance": "customer_confirmed", "confirmedBy": "Ops lead", "confirmationRef": "call 9/1",
                                   "materialId": mid, "origin": {"kind": "customer_material", "materials": [mid]}})
    assert status == 200, body
    saved = next(f for f in yaml.safe_load((pdir / "scenario.yaml").read_text())["facts"] if f["id"] == fact)
    assert saved["confirmationRef"] == f"call 9/1 (material {mid})" and saved["origin"]["materials"] == [mid]
    status, body, _ = client.call("POST", f"/projects/{PID}/items/{doc}/confirm",
                                  {"provenance": "sa_synthetic", "origin": {"kind": "customer_material", "materials": ["mat-ffffffffffff"]}})
    assert status == 400 and "unknown materials" in body["error"]
    status, body, _ = client.call("POST", f"/projects/{PID}/items/{doc}/confirm", {"provenance": "sa_synthetic", "materialId": mid})
    assert status == 400


def _workshop_project(app) -> tuple[Path, dict]:
    pdir = _create(app, pack_kind="workshop")
    data = yaml.safe_load((pdir / "scenario.yaml").read_text())
    for group in (data["facts"], data["tools"], data["knowledge"]["documents"], data["evaluation"]["goldenSet"]):
        for item in group:
            item["provenance"] = "ai_draft"
            item.pop("origin", None)
            for key in ("confirmedBy", "confirmedAt", "confirmationRef"):
                item.pop(key, None)
    app["service"].put_scenario(PID, {"yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True)})
    return pdir, data


def test_workshop_batch_review_needs_origins_and_anchors(app):
    pdir, data = _workshop_project(app)
    client = app["client"]
    ids = [f["id"] for f in data["facts"]] + [t["name"] for t in data["tools"]] + [d["id"] for d in data["knowledge"]["documents"]]
    golden = data["evaluation"]["goldenSet"]
    anchors = {c: next(g for g in golden if g["category"] == c) for c in ("normal", "boundary", "prohibited")}
    others = [g["id"] for g in golden if g["id"] not in {a["id"] for a in anchors.values()}]
    status, body, _ = client.call("POST", f"/projects/{PID}/items/confirm-batch", {"ids": ids + others, "acknowledged": True})
    assert status == 400 and "labels every synthetic setting with its origin" in body["error"]
    status, body, _ = client.call("POST", f"/projects/{PID}/items/confirm-batch",
                                  {"ids": ids + others, "acknowledged": True, "origin": {"kind": "sa_authored", "note": "SA workshop design"}})
    assert status == 200, body

    status, listing, _ = client.call("GET", f"/projects/{PID}/items")
    assert listing["anchors"]["required"] is True and listing["anchors"]["missing"] == ["normal", "boundary", "prohibited"]
    assert listing["classes"]["synthetic-setting"] == len(ids) + len(others)
    row = next(i for i in listing["items"] if i["id"] == others[0])
    assert row["class"] == "synthetic-setting" and row["origin"]["kind"] == "sa_authored" and row["set"] in ("practice", "holdout")
    v = app["service"].validate(PID, dry_build=False)
    assert v["ok"] is False and any(g["item_id"] == "customer-anchors" for g in v["gate"])
    assert any(f["itemId"] == "customer-anchors" for f in v["repair"]["saOnly"])
    assert app["service"].store.read_meta(PID)["lastValidation"]["anchorsMissing"] == ["normal", "boundary", "prohibited"]

    # Customer confirmations of the anchors and their basis facts (a real reference, never simulated).
    basis = sorted({fid for a in anchors.values() for fid in a["basis"]})
    for fid in basis:
        assert client.call("POST", f"/projects/{PID}/items/{fid}/confirm", {"provenance": "customer_confirmed", **CONFIRMATION})[0] == 200
    for anchor in anchors.values():
        assert client.call("POST", f"/projects/{PID}/items/{anchor['id']}/confirm", {"provenance": "customer_confirmed", **CONFIRMATION})[0] == 200
    status, body, _ = client.call("POST", f"/projects/{PID}/items/confirm-batch", {"ids": [basis[0]], "acknowledged": True})
    assert status == 409 and "downgrade" in body["error"]
    v = app["service"].validate(PID, dry_build=False)
    assert v["gate"] == [], v["gate"]
    assert v["anchors"]["anchorsMissing"] == [] and v["ok"] is True, (v["policy"], v["output"], v["render"])


def test_workshop_guide_narrative_is_reviewed_without_an_origin(app):
    """labs.guide has no origin field (schema): a workshop pack reviews its narrative as sa_synthetic
    without one, single or batch, and a supplied origin is refused or skipped, never written."""
    pdir = _create(app, pack_kind="workshop")
    data = yaml.safe_load((pdir / "scenario.yaml").read_text())
    data.setdefault("labs", {})["guide"] = {"id": "guide-narrative", "provenance": "ai_draft", "tagline": "A tagline for the class."}
    app["service"].put_scenario(PID, {"yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True)})
    client = app["client"]

    def narrative() -> dict:
        return yaml.safe_load((pdir / "scenario.yaml").read_text())["labs"]["guide"]

    status, body, _ = client.call("POST", f"/projects/{PID}/items/guide-narrative/confirm",
                                  {"provenance": "sa_synthetic", "origin": {"kind": "teaching_design"}})
    assert status == 400 and "carries no origin" in body["error"] and narrative()["provenance"] == "ai_draft"
    status, body, _ = client.call("POST", f"/projects/{PID}/items/guide-narrative/confirm", {"provenance": "sa_synthetic"})
    assert status == 200, body
    assert narrative()["provenance"] == "sa_synthetic" and "origin" not in narrative()
    v = app["service"].validate(PID, dry_build=False)
    assert v["schema"] == [] and "guide-narrative" not in {g["item_id"] for g in v["gate"]}, (v["schema"], v["gate"])

    data = yaml.safe_load((pdir / "scenario.yaml").read_text())
    data["labs"]["guide"]["provenance"] = "ai_draft"
    fact = data["facts"][0]
    fact.update(provenance="ai_draft")
    fact.pop("origin", None)
    for key in ("confirmedBy", "confirmedAt", "confirmationRef"):
        fact.pop(key, None)
    app["service"].put_scenario(PID, {"yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True)})
    status, body, _ = client.call("POST", f"/projects/{PID}/items/confirm-batch", {"ids": ["guide-narrative"], "acknowledged": True})
    assert status == 200, body
    status, body, _ = client.call("POST", f"/projects/{PID}/items/confirm-batch",
                                  {"ids": ["guide-narrative", fact["id"]], "acknowledged": True})
    assert status == 400 and body.get("itemIds") == [fact["id"]]  # only the fact needs an origin
    status, body, _ = client.call("POST", f"/projects/{PID}/items/confirm-batch",
                                  {"ids": ["guide-narrative", fact["id"]], "acknowledged": True, "origin": {"kind": "sa_authored"}})
    assert status == 200, body
    saved = yaml.safe_load((pdir / "scenario.yaml").read_text())
    assert "origin" not in saved["labs"]["guide"] and saved["facts"][0]["origin"] == {"kind": "sa_authored"}
    assert app["service"].validate(PID, dry_build=False)["schema"] == []


def test_edit_log_counts_manual_saves(app):
    pdir = _create(app)
    service = app["service"]
    project = service.get_project(PID)
    assert project["editLog"] == {"manualScenarioSaves": 0, "manualFileSaves": 0, "kiroApplies": 0, "kiroReverts": 0}
    assert project["brief"] == "" and project["materialsPolicy"] is None
    service.put_scenario(PID, {"yaml": project["scenarioYaml"]})
    service.put_file(PID, "agent/notes.md", "# notes\n")
    log = service.get_project(PID)["editLog"]
    assert log["manualScenarioSaves"] == 1 and log["manualFileSaves"] == 1
    (pdir / "generation").mkdir(exist_ok=True)
    (pdir / "generation" / "brief.md").write_text("Loyalty points helpdesk for ACME.\n")
    assert service.get_project(PID)["brief"] == "Loyalty points helpdesk for ACME.\n"


def test_writers_share_the_generation_lock(app, monkeypatch):
    _create(app)
    gen = server_mod._gen()
    monkeypatch.setattr(gen, "LOCK_TIMEOUT_SECS", 0.2)
    pdir = app["service"].store.project_dir(PID)
    held, release = threading.Event(), threading.Event()

    def holder():
        with gen.project_lock(pdir):
            held.set()
            release.wait(5)

    worker = threading.Thread(target=holder)
    worker.start()
    held.wait(5)
    try:
        status, body, _ = app["client"].call("PUT", f"/projects/{PID}/files/agent/notes.md", {"content": "x"})
        assert status == 409 and body["error"].startswith("project busy")
        assert _upload(app, "rules.md", RULES)[0] == 409
    finally:
        release.set()
        worker.join()
    assert app["client"].call("PUT", f"/projects/{PID}/files/agent/notes.md", {"content": "x"})[0] == 200


def test_blank_project_namespace_is_the_generation_namespace(app):
    long_id = "insurance-claims-intake-assistant"
    status, _body, _ = app["client"].call("POST", "/projects", {"id": long_id, "template": "blank"})
    assert status == 201
    data = yaml.safe_load(app["service"].get_project(long_id)["scenarioYaml"])
    assert data["namespace"] == server_mod._gen().derive_namespace(long_id)
    assert len(data["namespace"]["agentName"]) <= 19


# ---------------------------------------------------------------------------
# repair classification
# ---------------------------------------------------------------------------


def test_schema_and_xref_errors_are_classified_with_scopes(app):
    pdir = _create(app)
    data = yaml.safe_load((pdir / "scenario.yaml").read_text())
    data["evaluation"]["goldenSet"][0]["basis"] = ["no-such-fact"]
    data["tools"][1]["kind"] = "retrieval-ish"
    app["service"].put_scenario(PID, {"yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True)})
    v = app["service"].validate(PID)
    assert v["schemaKind"] == "schema" and v["repair"]["repairable"][0]["scopes"] == ["tools"]
    data["tools"][1]["kind"] = "mock"
    app["service"].put_scenario(PID, {"yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True)})
    v = app["service"].validate(PID)
    assert v["schemaKind"] == "xref"
    finding = v["repair"]["repairable"][0]
    assert finding["source"] == "xref" and finding["scopes"] == ["facts", "golden"] and finding["id"] == "F1"
    assert v["repair"]["suggestedScopes"] == ["facts", "golden"]
    stored = json.loads((pdir / "build" / "validate.json").read_text())
    assert stored["repair"] == v["repair"]


def test_classify_findings_routes_every_source():
    gen = server_mod._gen()
    validation = {
        "schemaKind": None, "schema": [],
        "policy": [{"severity": "error", "code": "teaching.no_gap", "message": "m", "path": "labs.teaching", "scopes": ["golden", "labs"]},
                   {"severity": "warning", "code": "teaching.bait_weak", "message": "w", "path": "x", "scopes": ["knowledge"]}],
        "output": [{"severity": "error", "code": "holdout.leaked", "message": "leak", "path": "pack/knowledge-base/docs/a.md", "scopes": ["knowledge"]},
                   {"severity": "error", "code": "residue.script", "message": "r", "path": "09-run-eval.sh", "scopes": []}],
        "render": ["pack has no practice golden cases", "anchor drift in 09"],
        "workspace": [{"severity": "warning", "code": "project.orphan_file", "path": "agent/x.md", "scopes": []},
                      {"severity": "warning", "code": "materials.reference_pack_customer_source", "path": "packKind", "scopes": []}],
        "gate": [{"item_type": "golden case", "item_id": "c1", "reason": "ai_draft", "waived": False},
                 {"item_type": "fact", "item_id": "f1", "reason": "x", "waived": True}],
    }
    repair = gen.classify_findings(validation)
    assert [r["code"] for r in repair["repairable"]] == ["teaching.no_gap", "teaching.bait_weak", "holdout.leaked", "render"]
    assert [r["code"] for r in repair["engine"]] == ["residue.script", "render"]
    assert [r["code"] for r in repair["saOnly"]] == ["materials.reference_pack_customer_source", "gate.golden_case"]
    assert repair["suggestedScopes"] == ["knowledge", "golden", "labs"]
    ids = [r["id"] for group in ("repairable", "saOnly", "engine") for r in repair[group]]
    assert len(ids) == len(set(ids)) == 8


def test_unknown_material_on_a_reviewed_item_is_the_sas_to_fix(app):
    """Generation preserves a reviewed item's origin verbatim, so a citation of a deleted material on
    it is saOnly (re-label in Review); on a draft item it stays repairable."""
    pdir = _create(app)
    svc = app["service"]
    svc.record_material_approval(PID, {"approvalRef": "email from ACME legal", "dataClassification": "internal"})
    mid = _upload(app, "rules.md", RULES)[1]["id"]
    data = yaml.safe_load((pdir / "scenario.yaml").read_text())
    reviewed, drafted = data["facts"][0]["id"], data["facts"][1]["id"]
    data["facts"][1].update(provenance="ai_draft", origin={"kind": "customer_material", "materials": [mid]})
    svc.put_scenario(PID, {"yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True)})
    svc.confirm_item(PID, reviewed, {"provenance": "sa_synthetic", "origin": {"kind": "customer_material", "materials": [mid]}})
    assert svc.delete_material(PID, mid)["referencedBy"] == sorted([reviewed, drafted])
    repair = svc.validate(PID, dry_build=False)["repair"]
    unknown = {group: [r["path"] for r in repair[group] if r["code"] == "provenance.material_unknown"]
               for group in ("repairable", "saOnly", "engine")}
    assert unknown == {"repairable": [f"fact.{drafted}.origin"], "saOnly": [f"fact.{reviewed}.origin"], "engine": []}
    sa_row = next(r for r in repair["saOnly"] if r["code"] == "provenance.material_unknown")
    assert sa_row["scopes"] == [] and "re-label its origin in Review" in sa_row["message"]



# ---------------------------------------------------------------------------
# project files and pack kind (designs[4]: create, POST /files/prune, POST /pack-kind)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template", ["hr-default", "it-helpdesk", "maintenance"])
def test_template_projects_hold_only_referenced_files(app, template):
    pdir = _create(app, template=template)
    gen = server_mod._gen()
    data = yaml.safe_load((pdir / "scenario.yaml").read_text())
    files = {p.relative_to(pdir).as_posix() for p in pdir.rglob("*") if p.is_file()} - {"project.json", "scenario.yaml"}
    assert files == gen._referenced_files(data)
    assert gen.orphan_files(pdir, data) == ([], [])


def test_prune_lists_then_moves_orphans_out_of_the_pack(app):
    pdir = _create(app)
    client = app["client"]
    for rel in ("agent/sa-notes.md", "knowledge-base/docs/old.md", "NOTES.txt"):
        (pdir / rel).parent.mkdir(parents=True, exist_ok=True)
        (pdir / rel).write_text(f"# {rel}\n")
    digest = server_mod.pack_digest(pdir)
    status, dry, _ = client.call("POST", f"/projects/{PID}/files/prune", {})
    assert status == 200 and dry == {"dryRun": True, "orphans": ["agent/sa-notes.md", "knowledge-base/docs/old.md"],
                                     "untracked": ["NOTES.txt"], "deleted": [], "trash": None}
    assert server_mod.pack_digest(pdir) == digest
    status, body, _ = client.call("POST", f"/projects/{PID}/files/prune", {"dryRun": False})
    assert status == 400 and "acknowledged" in body["error"]
    status, done, _ = client.call("POST", f"/projects/{PID}/files/prune", {"dryRun": False, "acknowledged": True})
    assert status == 200 and done["deleted"] == dry["orphans"] and done["trash"].startswith("generation/pruned/")
    assert not (pdir / "agent" / "sa-notes.md").exists() and (pdir / "NOTES.txt").is_file()
    assert (pdir / done["trash"] / "agent" / "sa-notes.md").read_text() == "# agent/sa-notes.md\n"  # recoverable
    assert server_mod.pack_digest(pdir) != digest and app["service"].store.read_meta(PID)["lastBuild"] is None
    status, again, _ = client.call("POST", f"/projects/{PID}/files/prune", {"dryRun": False, "acknowledged": True})
    assert status == 200 and again["deleted"] == [] and again["orphans"] == []


def test_pack_kind_switch_is_audited(app):
    pdir = _create(app)
    client, svc = app["client"], app["service"]
    call = lambda body: client.call("POST", f"/projects/{PID}/pack-kind", body)[:2]  # noqa: E731
    assert call({"packKind": "class", "acknowledged": True, "reason": "a long enough reason"})[0] == 400
    assert call({"packKind": "workshop", "reason": "a long enough reason"})[0] == 400
    assert call({"packKind": "workshop", "acknowledged": True, "reason": "short"})[0] == 400
    before = (pdir / "scenario.yaml").read_text()
    status, body = call({"packKind": "workshop", "acknowledged": True, "reason": "ACME teaches this in class next week"})
    assert status == 200 and body["packKind"] == "workshop" and body["previous"] == "reference"
    after = (pdir / "scenario.yaml").read_text()
    assert after == before.replace("packKind: reference", "packKind: workshop")  # only that line changes
    meta = svc.store.read_meta(PID)
    assert meta["packKind"] == "workshop" and meta["lastBuild"] is None
    assert [(h["from"], h["to"], h["via"], h["reason"]) for h in meta["packKindHistory"]] == [
        ("reference", "workshop", "pack-kind", "ACME teaches this in class next week")]
    assert call({"packKind": "workshop", "acknowledged": True, "reason": "ACME teaches this in class next week"})[0] == 409
    # The scenario editor still switches kinds (the current UI says so), but never silently.
    data = yaml.safe_load(after)
    data["packKind"] = "reference"
    svc.put_scenario(PID, {"yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True)})
    history = svc.store.read_meta(PID)["packKindHistory"]
    assert (history[-1]["from"], history[-1]["to"], history[-1]["via"], history[-1]["reason"]) == (
        "workshop", "reference", "scenario-editor", None)
    svc.put_scenario(PID, {"yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True)})  # no change: no entry
    assert len(svc.store.read_meta(PID)["packKindHistory"]) == 2


def test_the_whole_task_is_scanned_before_it_goes_to_kiro(app):
    """Repair / regenerate send the project's scenario and files verbatim, and material texts are sent
    long after their upload scan: a credential or personal identifier anywhere in the task is refused,
    naming where it is (never echoing it), and no record is written."""
    pdir = _create(app, template="maintenance")
    svc, gen = app["service"], server_mod._gen()
    key = "AKIA" + "QWERTYUIOPASDFGH"
    doc = "knowledge-base/docs/spare_parts.md"
    svc.put_file(PID, doc, (pdir / doc).read_text(encoding="utf-8") + f"\nVendor portal key: {key}\n")
    with pytest.raises(gen.GenerationError) as excinfo:
        gen.prepare_generation(app["data"], {"projectId": PID, "mode": "regenerate"}, runner="kiro-cli")
    assert "remove credential-shaped material" in str(excinfo.value) and doc in str(excinfo.value)
    assert key not in str(excinfo.value) and not list((app["data"] / "generations").glob("*.json"))
    svc.put_file(PID, doc, (pdir / doc).read_text(encoding="utf-8").replace(key, "see the vendor portal"))

    svc.record_material_approval(PID, {"approvalRef": "email from ACME legal", "dataClassification": "internal"})
    mid = _upload(app, "rules.md", RULES)[1]["id"]
    national_id = "110101" + "199003074514"
    (pdir / "materials" / "text" / f"{mid}.txt").write_text(RULES.decode() + f"Customer ID card {national_id}\n", encoding="utf-8")
    with pytest.raises(gen.GenerationError) as excinfo:
        gen.prepare_generation(app["data"], {"projectId": PID, "brief": "Plant maintenance assistant for ACME technicians."},
                               runner="kiro-cli")
    assert f"material {mid}" in str(excinfo.value) and national_id not in str(excinfo.value)


@pytest.mark.parametrize("sample", [
    "AKIA" + "QWERTYUIOPASDFGH", "-" * 5 + "BEGIN " + "RSA PRIVATE KEY" + "-" * 5, "Bearer " + "a" * 24,
    "aws_secret_access_key = " + "A" * 40, "aws_session_token=" + "B" * 120, "110101" + "199003074514", "123-" + "45-6789",
])
def test_generation_refuses_every_shape_the_upload_scan_refuses(sample):
    for text in (f"x {sample} y", f"员工{sample}请核实"):  # Chinese prose puts no space around an ID
        assert mat.scan_sensitive(text), text  # the upload scan refuses it...
        assert server_mod._gen()._secret_hit(text), text  # ...and so does every generation boundary
    if sample[0].isdigit():  # a number glued to ASCII letters or digits is not an ID
        assert not mat.scan_sensitive(f"x1{sample}0") and not server_mod._gen()._secret_hit(f"A{sample}9"), sample


def test_a_national_id_written_into_chinese_prose_never_reaches_kiro(app):
    """``\\b`` sees no boundary between a CJK character and a digit: the upload scan and the Kiro-task scan use
    ASCII boundaries, so ``身份证号110101…已登记`` is refused like ``id 110101…`` (review finding)."""
    pdir = _create(app)
    national_id, ssn = "110101" + "199003074514", "123-" + "45-6789"
    for name, text in (("id.md", f"会员张三身份证号{national_id}已登记\n"), ("ssn.md", f"社保号{ssn}已登记\n")):
        status, body = _upload(app, name, RULES + text.encode("utf-8"))
        assert status == 422 and body["findings"], (name, body)
        assert national_id not in json.dumps(body, ensure_ascii=False) and ssn not in json.dumps(body, ensure_ascii=False)
    assert not (pdir / "materials").exists()
    app["service"].record_material_approval(PID, {"approvalRef": "email from ACME legal", "dataClassification": "internal"})
    gen = server_mod._gen()
    with pytest.raises(gen.GenerationError, match="credential"):
        gen.prepare_generation(app["data"], {"projectId": PID, "brief": f"门店会员身份证号为{national_id}。"}, runner="kiro-cli")
    assert not list((app["data"] / "generations").glob("*.json"))


def _rehearsal_doc(version: str) -> dict:
    return {"releaseVersion": version, "generatedAt": "2026-09-28T00:00:00Z", "verdict": "not_ready", "remediation": [
        {"id": "h01", "code": "PF_BASELINE_ALREADY_PASSES", "severity": "blocking", "caseId": "p1",
         "asset": {"kind": "baseline_prompt", "file": "agent/baseline-prompt.md", "scenarioPath": "prompts.baselineFile", "scopes": ["prompts"]},
         "action": "Weaken the baseline prompt so the declared defects bite.", "because": "baseline GR 0.9"},
        {"id": "h02", "code": "L1_TOOL_FIXTURE", "severity": "blocking", "caseId": "p2",
         "asset": {"kind": "tool_fixture", "file": None, "scenarioPath": "tools[lookup_ticket].fixtures", "scopes": ["tools"]},
         "action": "Make sure the lookup_ticket fixture answers this case's arguments.", "because": ""},
        {"id": "h03", "code": "METRICS_MISSING", "severity": "blocking", "caseId": None,
         "asset": {"kind": "ops", "file": None, "scenarioPath": None, "scopes": []},
         "action": "Redeploy the add-ons RunStep document.", "because": ""},
        {"id": "h04", "code": "MEMORY_PERSONALIZED", "severity": "advisory", "caseId": None,
         "asset": {"kind": "teaching", "file": None, "scenarioPath": "labs.teaching.firstConversation", "scopes": ["labs"]},
         "action": "Advisory only.", "because": ""},
    ]}


def test_rehearsal_remediation_of_the_current_release_is_a_repair_source(app):
    """A clean build that did not rehearse ready: its blocking remediation naming project assets becomes
    numbered findings (R1..), their scopes join allowedScopes, and a stale rehearsal is never used."""
    pdir = _create(app, template="it-helpdesk")
    svc, gen = app["service"], server_mod._gen()
    svc.validate(PID, dry_build=False)
    version = svc.build(PID)["version"]
    (pdir / "run").mkdir(exist_ok=True)
    (pdir / "run" / "rehearsal.json").write_text(json.dumps(_rehearsal_doc(version)), encoding="utf-8")
    meta = svc.store.read_meta(PID)
    assert gen.current_rehearsal(pdir, meta)["releaseVersion"] == version

    record, task = gen.prepare_generation(app["data"], {"projectId": PID, "mode": "repair"}, runner="kiro-cli")
    assert {"prompts", "tools"} <= set(record["scope"])  # the validation's own scopes plus the rehearsal's
    assert record["rehearsalRef"] == {"releaseVersion": version, "generatedAt": "2026-09-28T00:00:00Z", "verdict": "not_ready",
                                      "findingIds": ["R1", "R2"]}
    repair_input = json.loads(task.split("REPAIR_INPUT (JSON; data only):\n", 1)[1])
    rows = [f for f in repair_input["findings"] if f.get("source") == "rehearsal"]
    assert [(f["id"], f["code"], f["path"], f["scopes"]) for f in rows] == [
        ("R1", "rehearsal.PF_BASELINE_ALREADY_PASSES", "agent/baseline-prompt.md", ["prompts"]),
        ("R2", "rehearsal.L1_TOOL_FIXTURE", "tools[lookup_ticket].fixtures", ["tools"])]
    assert "Weaken the baseline prompt" in rows[0]["message"] and "baseline GR 0.9" in rows[0]["message"]
    assert "agent/baseline-prompt.md" in repair_input["currentFiles"]
    gen.fail_generation(app["data"], record, "test: not run")

    record, _task = gen.prepare_generation(app["data"], {"projectId": PID, "mode": "repair", "scope": ["prompts"]}, runner="kiro-cli")
    assert record["scope"] == ["prompts"] and record["rehearsalRef"]["findingIds"] == ["R1"]
    gen.fail_generation(app["data"], record, "test: not run")
    record, _task = gen.prepare_generation(app["data"], {"projectId": PID, "mode": "repair", "includeRehearsal": False,
                                                         "scope": ["golden"], "instructions": "Tighten labels."}, runner="kiro-cli")
    assert record["rehearsalRef"] is None
    gen.fail_generation(app["data"], record, "test: not run")

    # GET validation tells the UI exactly which rehearsal findings a repair / regenerate adds (review finding:
    # the repair panel counted the validation only).
    status, view, _ = app["client"].call("GET", f"/projects/{PID}/validation")
    assert status == 200 and [(f["id"], f["scopes"]) for f in view["rehearsalFindings"]] == [("R1", ["prompts"]), ("R2", ["tools"])]
    assert view["rehearsalFindings"] == gen._rehearsal_findings(gen.current_rehearsal(pdir, svc.store.read_meta(PID)))

    # Another release's rehearsal (or a changed source) is not evidence about this pack.
    (pdir / "run" / "rehearsal.json").write_text(json.dumps(_rehearsal_doc("it-helpdesk-000000000000")), encoding="utf-8")
    assert gen.current_rehearsal(pdir, svc.store.read_meta(PID)) is None
    assert app["client"].call("GET", f"/projects/{PID}/validation")[1]["rehearsalFindings"] == []


def test_rehearsal_findings_speak_the_rehearsal_language_with_the_english_instruction():
    """A zh-CN pack's rehearsal writes its hints in Chinese with English twins: the repair finding the SA reads
    is Chinese (action + 原因), messageEn carries the English instruction for the English repair contract."""
    gen = server_mod._gen()
    doc = _rehearsal_doc("ops-support-000000000000")
    doc["language"] = "zh-CN"
    doc["remediation"][0].update(action="削弱基线提示词，让声明的缺陷真正起作用。", actionEn="Weaken the baseline prompt so the declared defects bite.",
                                 because="基线回答已经有据（GR 0.9 ≥ 0.7）", becauseEn="baseline GR 0.9")
    doc["remediation"][1].update(action="确认 lookup_ticket 的 fixture 能响应这道题的参数。",
                                 actionEn="Make sure the lookup_ticket fixture answers this case's arguments.", becauseEn="")
    rows = gen._rehearsal_findings(doc)
    assert [(r["id"], r["message"], r["messageEn"]) for r in rows] == [
        ("R1", "削弱基线提示词，让声明的缺陷真正起作用。原因：基线回答已经有据（GR 0.9 ≥ 0.7）",
         "Weaken the baseline prompt so the declared defects bite. Because: baseline GR 0.9"),
        ("R2", "确认 lookup_ticket 的 fixture 能响应这道题的参数。", "Make sure the lookup_ticket fixture answers this case's arguments.")]
    # A record of the English-only engine (no language, no twins): English, and messageEn repeats it.
    legacy = gen._rehearsal_findings(_rehearsal_doc("ops-support-000000000000"))
    assert legacy[0]["message"] == legacy[0]["messageEn"] == "Weaken the baseline prompt so the declared defects bite. Because: baseline GR 0.9"


def test_the_newest_rehearsal_of_the_current_build_is_the_repair_source(app):
    """A direct run writes build/direct/<version>/direct-rehearsal.json; when it and the Guided Run's
    run/rehearsal.json both judge the current build, the one generated last is what a repair reads."""
    pdir = _create(app, template="it-helpdesk")
    svc, gen = app["service"], server_mod._gen()
    svc.validate(PID, dry_build=False)
    version = svc.build(PID)["version"]
    direct = pdir / "build" / "direct" / version
    direct.mkdir(parents=True)
    direct_doc = {**_rehearsal_doc(version), "generatedAt": "2026-09-29T00:00:00Z"}
    (direct / "direct-rehearsal.json").write_text(json.dumps(direct_doc), encoding="utf-8")
    meta = svc.store.read_meta(PID)
    assert gen.current_rehearsal(pdir, meta)["generatedAt"] == "2026-09-29T00:00:00Z"
    assert [f["id"] for f in gen._rehearsal_findings(gen.current_rehearsal(pdir, meta))] == ["R1", "R2"]
    (pdir / "run").mkdir(exist_ok=True)
    (pdir / "run" / "rehearsal.json").write_text(json.dumps(_rehearsal_doc(version)), encoding="utf-8")  # generated 09-28: older
    assert gen.current_rehearsal(pdir, meta)["generatedAt"] == "2026-09-29T00:00:00Z"
    (pdir / "run" / "rehearsal.json").write_text(json.dumps({**_rehearsal_doc(version), "generatedAt": "2026-09-30T00:00:00Z"}), encoding="utf-8")
    assert gen.current_rehearsal(pdir, meta)["generatedAt"] == "2026-09-30T00:00:00Z"  # a newer Guided Run wins
    (pdir / "run" / "rehearsal.json").write_text(json.dumps({**_rehearsal_doc(version), "generatedAt": "2026-09-29T00:00:00Z",
                                                            "verdict": "ready"}), encoding="utf-8")
    assert gen.current_rehearsal(pdir, meta)["verdict"] == "ready"  # a tie: the Guided Run's
    (pdir / "run" / "rehearsal.json").write_text(json.dumps(_rehearsal_doc("it-helpdesk-000000000000")), encoding="utf-8")
    assert gen.current_rehearsal(pdir, meta)["generatedAt"] == "2026-09-29T00:00:00Z"  # a stale Guided Run rehearsal does not hide it
    (direct / "direct-rehearsal.json").write_text(json.dumps(_rehearsal_doc("it-helpdesk-000000000000")), encoding="utf-8")
    assert gen.current_rehearsal(pdir, meta) is None
