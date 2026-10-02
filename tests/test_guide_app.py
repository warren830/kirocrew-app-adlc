"""App routes of the Workshop Guide (SPEC D10): built guides, draft preview, instructor bundle, narrative review."""

from __future__ import annotations

import io
import json
import sys
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_app_backend import Client, server_mod  # noqa: E402
from test_sync import StubClients  # noqa: E402

MARKER = "workshop-customizer:instructor-only"


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("guide-app")
    stub = StubClients(expiry=datetime.now(timezone.utc) + timedelta(hours=2))
    srv, svc = server_mod.create_server(data_dir, home=REPO_ROOT, clients_factory=lambda cfg: stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"client": Client(srv.server_address[1]), "service": svc}
    srv.shutdown()
    srv.server_close()


def _project(app, pid: str, *, build: bool = True) -> str:
    c = app["client"]
    status, body, _ = c.call("POST", "/projects", {"id": pid, "template": "it-helpdesk", "packKind": "reference", "displayName": "ACME IT"})
    assert status == 201, body
    if build:
        status, body, _ = c.call("POST", f"/projects/{pid}/build")
        assert status == 200, body
    return pid


@pytest.fixture(scope="module")
def built(app):
    return _project(app, "guide-built")


def _zip_names(data: bytes) -> list[str]:
    return zipfile.ZipFile(io.BytesIO(data)).namelist()


def test_student_guide_is_the_release_readme(app, built):
    c, svc = app["client"], app["service"]
    status, body, _ = c.call("GET", f"/projects/{built}/guide?audience=student")
    assert status == 200, body
    release = Path(svc._current_build(built)["releaseDir"])
    assert body["markdown"] == (release / "README.md").read_text(encoding="utf-8")
    assert body["fileName"] == "README.md" and body["audience"] == "student" and body["version"].startswith(f"{built}-")
    assert body["rehearsalEvidence"] is False and "warning" not in body
    status, raw, headers = c.call("GET", f"/projects/{built}/guide?audience=student&download=1", raw=True)
    assert status == 200 and raw == (release / "README.md").read_bytes()
    assert headers["Content-Type"].startswith("text/markdown") and 'filename="README.md"' in headers["Content-Disposition"]
    assert c.call("GET", f"/projects/{built}/guide")[1]["audience"] == "student"  # default audience


def test_instructor_guide_carries_the_marker_and_a_warning(app, built):
    c = app["client"]
    status, body, _ = c.call("GET", f"/projects/{built}/guide?audience=instructor")
    assert status == 200, body
    assert body["markdown"].startswith(f"<!-- {MARKER} pack={built} -->")  # a project's pack id is its project id
    assert "holdout" in body["warning"] and body["fileName"].endswith("-instructor-guide.md")
    assert "No rehearsal evidence is recorded for this build yet" in body["markdown"]
    assert c.call("GET", f"/projects/{built}/guide?audience=everyone")[0] == 400


def test_the_default_export_is_the_release_with_its_readme(app, built):
    c = app["client"]
    status, data, _ = c.call("GET", f"/projects/{built}/export", raw=True)
    assert status == 200
    names = _zip_names(data)
    assert "README.md" in names and "RELEASE.json" in names and not any(n.startswith("instructor/") or n.endswith("instructor-guide.md") for n in names)
    assert c.call("GET", f"/projects/{built}/export?bundle=release", raw=True)[1] == data
    assert c.call("GET", f"/projects/{built}/export?bundle=everything", raw=True)[0] == 400


def test_instructor_bundle_holds_only_instructor_material(app, built):
    c = app["client"]
    status, data, headers = c.call("GET", f"/projects/{built}/export?bundle=instructor", raw=True)
    assert status == 200 and headers["Content-Type"] == "application/zip"
    assert headers["Content-Disposition"].endswith('-instructor.zip"')
    names = _zip_names(data)
    assert {"instructor-guide.md", "golden/holdout.json", "golden/all-cases.json", "provenance-report.md", "teaching.json",
            "scenario-snapshot.yaml", "student-guide.md"} == set(names)
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert archive.read("instructor-guide.md").decode("utf-8").startswith(f"<!-- {MARKER}")
    assert len(json.loads(archive.read("golden/holdout.json"))) == 9
    assert c.call("GET", f"/projects/{built}/export?bundle=instructor", raw=True)[1] == data  # deterministic zip


def test_rehearsal_evidence_joins_the_instructor_view_and_bundle(app):
    pid = _project(app, "guide-rehearsed")
    c, svc = app["client"], app["service"]
    version = svc._current_build(pid)["version"]
    run = svc.store.project_dir(pid) / "run"
    run.mkdir(exist_ok=True)
    (run / "rehearsal.json").write_text(json.dumps({
        "verdict": "ready", "readyForClass": True, "releaseVersion": version, "generatedAt": "2026-09-20T10:00:00Z",
        "phenomena": [{"id": "prompt-fix-where-retrieval-good", "kind": "prompt_fixable", "verdict": "reproduced", "evidence": "GR 0.45 -> 0.85"}],
    }), encoding="utf-8")
    body = c.call("GET", f"/projects/{pid}/guide?audience=instructor")[1]
    assert body["rehearsalEvidence"] is True and "Verdict: **ready**" in body["markdown"] and "GR 0.45 -&gt; 0.85" in body["markdown"]
    assert "not the release of this guide" not in body["markdown"]
    status, data, _ = c.call("GET", f"/projects/{pid}/export?bundle=instructor", raw=True)
    archive = zipfile.ZipFile(io.BytesIO(data))
    evidence = archive.read("rehearsal-evidence.md").decode("utf-8")
    assert evidence.startswith(f"<!-- {MARKER} pack={pid} -->\n# Rehearsal evidence — {version}\n") and "Ready for class: **yes**" in evidence
    # The built instructor guide in the bundle is the hash-verified build file, not the attached view.
    assert "No rehearsal evidence is recorded" in archive.read("instructor-guide.md").decode("utf-8")
    (run / "rehearsal.json").write_text("not json", encoding="utf-8")
    assert c.call("GET", f"/projects/{pid}/guide?audience=instructor")[1]["rehearsalEvidence"] is False
    # A JSON-valid record with values of the wrong type is rendered defensively, never a 500.
    (run / "rehearsal.json").write_text(json.dumps({"verdict": "ready", "phenomena": 5, "remediation": 3, "readiness": [1]}), encoding="utf-8")
    for route in ("guide?audience=instructor", "guide/preview?audience=instructor", "export?bundle=instructor"):
        status, body, _ = c.call("GET", f"/projects/{pid}/{route}", raw=route.startswith("export"))
        assert status == 200, (route, body)
    assert "Verdict: **ready**" in c.call("GET", f"/projects/{pid}/guide?audience=instructor")[1]["markdown"]


def test_a_chinese_pack_bundles_chinese_rehearsal_evidence(app):
    """rehearsal-evidence.md follows the build snapshot's language: a zh-CN pack's file has a Chinese heading
    around the Chinese rehearsal text, not an English one."""
    c, svc = app["client"], app["service"]
    status, body, _ = c.call("POST", "/projects", {"id": "guide-rehearsed-zh", "template": "hr-default", "packKind": "reference"})
    assert status == 201, body
    status, body, _ = c.call("POST", "/projects/guide-rehearsed-zh/build")
    assert status == 200, body
    version = svc._current_build("guide-rehearsed-zh")["version"]
    (svc.store.project_dir("guide-rehearsed-zh") / "run").mkdir(exist_ok=True)
    (svc.store.project_dir("guide-rehearsed-zh") / "run" / "rehearsal.json").write_text(json.dumps({
        "language": "zh-CN", "verdict": "ready", "readyForClass": True, "releaseVersion": version,
        "reasonCode": "CONTRASTS_REPRODUCED", "reason": "最近一次完整运行里，所有决定性教学对比都复现了",
        "reasonEn": "every decisive teaching contrast reproduced in the latest complete run", "phenomena": [],
    }, ensure_ascii=False), encoding="utf-8")
    status, data, _ = c.call("GET", "/projects/guide-rehearsed-zh/export?bundle=instructor", raw=True)
    assert status == 200
    evidence = zipfile.ZipFile(io.BytesIO(data)).read("rehearsal-evidence.md").decode("utf-8")
    assert evidence.startswith(f"<!-- {MARKER} pack=guide-rehearsed-zh -->\n# 彩排证据——{version}\n\n- 结论：**ready**\n")
    assert "可以上课：**是**" in evidence and "所有决定性教学对比都复现了" in evidence
    assert "Rehearsal evidence" not in evidence and "every decisive" not in evidence


def test_tampered_or_stale_builds_are_refused(app):
    pid = _project(app, "guide-tamper")
    c, svc = app["client"], app["service"]
    guide_path = svc.store.project_dir(pid) / "build" / "instructor" / "instructor-guide.md"
    guide_path.write_text(guide_path.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
    status, body, _ = c.call("GET", f"/projects/{pid}/guide?audience=instructor")
    assert status == 409 and "checksums" in body["error"]
    assert c.call("GET", f"/projects/{pid}/export?bundle=instructor", raw=True)[0] == 409
    assert c.call("GET", f"/projects/{pid}/guide?audience=student")[0] == 200  # the student guide is intact
    status, body, _ = c.call("PUT", f"/projects/{pid}/files/knowledge-base/docs/vpn_access.md", {"content": "# VPN\n\nEdited.\n"})
    assert status == 200, body
    status, body, _ = c.call("GET", f"/projects/{pid}/guide?audience=student")
    assert status == 409 and "rebuild" in body["error"]


def test_preview_works_before_a_build_on_an_unreviewed_draft(app):
    pid = _project(app, "guide-draft", build=False)
    c, svc = app["client"], app["service"]
    path = svc.store.project_dir(pid) / "scenario.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["labs"]["guide"]["provenance"] = "ai_draft"
    data["facts"][0]["provenance"] = "ai_draft"
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    assert c.call("GET", f"/projects/{pid}/guide?audience=student")[0] == 409  # no build yet
    status, body, _ = c.call("GET", f"/projects/{pid}/guide/preview?audience=student")
    assert status == 200, body
    assert body["draft"] is True and "workshop-customizer:draft-preview" in body["markdown"] and "**DRAFT PREVIEW**" in body["markdown"]
    assert "[Draft — not for class]" in body["markdown"] and isinstance(body["findings"], list)
    status, body, _ = c.call("GET", f"/projects/{pid}/guide/preview?audience=instructor")
    assert status == 200 and body["markdown"].startswith(f"<!-- {MARKER}") and "holdout" in body["warning"]
    data["labs"]["guide"]["memoryLesson"] = "Try the password-rules question."  # a holdout id in student prose
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    body = c.call("GET", f"/projects/{pid}/guide/preview?audience=student")[1]
    assert any(f["code"] == "guide.holdout_leak" and f["scopes"] == ["guides"] for f in body["findings"]), body["findings"]
    path.write_text("schemaVersion: 1\nid: broken\n", encoding="utf-8")
    status, body, _ = c.call("GET", f"/projects/{pid}/guide/preview")
    assert status == 409 and "does not load" in body["error"]
    assert c.call("GET", f"/projects/{pid}/guide/preview?audience=all")[0] == 400


def test_the_narrative_is_one_reviewable_item(app):
    pid = _project(app, "guide-review", build=False)
    c, svc = app["client"], app["service"]
    path = svc.store.project_dir(pid) / "scenario.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["labs"]["guide"]["provenance"] = "ai_draft"
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    items = c.call("GET", f"/projects/{pid}/items")[1]["items"]
    narrative = [i for i in items if i["kind"] == "narrative"]
    # P4 adds class/origin/criticality to every row; the narrative is advisory teaching prose.
    assert [{k: i[k] for k in ("id", "kind", "provenance", "label", "confirmable", "criticality")} for i in narrative] == [
        {"id": "guide-narrative", "kind": "narrative", "provenance": "ai_draft",
         "label": data["labs"]["guide"]["memoryLesson"][:200], "confirmable": True, "criticality": "advisory"}]
    status, v, _ = c.call("POST", f"/projects/{pid}/validate", {"dryBuild": False})
    assert v["ok"] is False and any(g["item_id"] == "guide-narrative" for g in v["gate"]) and v["summary"]["pending"] == 1
    status, body, _ = c.call("POST", f"/projects/{pid}/items/guide-narrative/confirm",
                             {"provenance": "customer_confirmed", "confirmedBy": "x", "confirmationRef": "y"})
    assert status == 400 and "teaching prose" in body["error"]
    status, body, _ = c.call("POST", f"/projects/{pid}/items/confirm-batch", {"ids": ["guide-narrative"], "acknowledged": True})
    assert status == 200, body
    reviewed = yaml.safe_load(path.read_text(encoding="utf-8"))["labs"]["guide"]
    assert reviewed["provenance"] == "sa_synthetic" and "confirmedBy" not in reviewed
    status, v, _ = c.call("POST", f"/projects/{pid}/validate", {"dryBuild": False})
    assert v["ok"] is True, v
    status, body, _ = c.call("POST", f"/projects/{pid}/items/guide-narrative/confirm", {"provenance": "sa_synthetic"})
    assert status == 200 and body == {"itemId": "guide-narrative", "provenance": "sa_synthetic"}


def test_a_guide_error_in_build_and_an_unparsable_scenario_in_preview_are_409s(app, monkeypatch):
    pid = _project(app, "guide-errors", build=False)
    c, svc = app["client"], app["service"]
    g = svc._require_engine()["guide"]

    def fail(*_args, **_kwargs):
        raise g.GuideError("synthetic guide failure")

    monkeypatch.setattr(g, "build_guides", fail)
    status, body, _ = c.call("POST", f"/projects/{pid}/build")
    assert status == 409 and "Workshop Guides could not be generated: synthetic guide failure" in body["error"], body
    monkeypatch.undo()
    path = svc.store.project_dir(pid) / "scenario.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "\nfoo: [unclosed\n", encoding="utf-8")
    assert c.call("GET", f"/projects/{pid}/items")[0] == 409  # the same file is a 409 elsewhere too
    for broken in (None, b"schemaVersion: 1\nid: caf\xe9\n"):
        if broken is not None:
            path.write_bytes(broken)  # not UTF-8
        status, body, _ = c.call("GET", f"/projects/{pid}/guide/preview?audience=student")
        assert status == 409 and "does not load" in body["error"], body
