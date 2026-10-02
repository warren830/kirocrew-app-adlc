"""GET / POST /projects/{pid}/run/rehearsal (in-process server on loopback; no AWS client is ever created)."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

import pytest

import teaching_run as tr
from test_app_backend import Client, server_mod
from workshop_customizer import rehearsal
from workshop_customizer.guided_run_store import GuidedRunStore

REPO_ROOT = Path(__file__).resolve().parents[1]
PID = "rehearse-hr"


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("appdata")

    def no_aws(cfg):  # rehearsal is computed from local files only
        raise AssertionError("rehearsal must not create AWS clients")

    srv, svc = server_mod.create_server(data_dir, home=REPO_ROOT, clients_factory=no_aws)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"client": Client(srv.server_address[1]), "service": svc, "pdir": data_dir / "projects" / PID}
    srv.shutdown()
    srv.server_close()


def _write_run(pdir: Path, run: str = "hr-reproduced") -> Path:
    build = json.loads((pdir / "build" / "build.json").read_text(encoding="utf-8"))
    store = GuidedRunStore(pdir)
    state = store.create(project_id=PID, release_version=build["version"], template_commit=build["templateCommit"])
    steps = tr.load_steps(run)
    state["steps"] = {sid: copy.deepcopy(steps[sid]) for sid in state["stepOrder"]}
    state["status"] = "passed"
    store.save(state)
    return store.path


def test_rehearsal_routes(app):
    c, pdir = app["client"], app["pdir"]
    assert c.call("POST", "/projects", {"id": PID, "template": "hr-default", "packKind": "reference"})[0] == 201
    status, body, _ = c.call("GET", f"/projects/{PID}/run/rehearsal")
    assert status == 409 and "build" in body["error"]
    assert c.call("POST", f"/projects/{PID}/build")[0] == 200
    status, body, _ = c.call("GET", f"/projects/{PID}/run/rehearsal")
    assert status == 409 and "no Guided Run" in body["error"] and not (pdir / "run" / "state.json").exists()

    # The build snapshot, not the live scenario.yaml, is judged (SPEC D8).
    snapshot = pdir / "build" / "instructor" / "scenario-snapshot.yaml"
    text = snapshot.read_text(encoding="utf-8")
    assert "SP2≈0：病假规定被 FAQ 噪声埋没" in text
    snapshot.write_text(text.replace("SP2≈0：病假规定被 FAQ 噪声埋没", "SNAPSHOT SP2≈0：病假规定被 FAQ 噪声埋没", 1), encoding="utf-8")

    state_path = _write_run(pdir)
    before = state_path.read_bytes()
    status, got, _ = c.call("GET", f"/projects/{PID}/run/rehearsal")
    assert status == 200, got
    assert rehearsal.validate_document(got) == []
    # P3 is merged: the build ships README.md and the instructor guide, so a ready verdict is ready for class.
    assert (got["verdict"], got["readyForClass"], got["readiness"]["blockers"]) == ("ready", True, [])
    assert got["inputs"]["documentsSearched"] == 11 and got["projectId"] == PID
    gap = next(p for p in got["phenomena"] if p["id"] == "sick-leave-retrieval-gap")
    assert gap["teachingPoint"].startswith("SNAPSHOT ")
    # GET writes nothing: no rehearsal.json, no meta, the run state untouched.
    assert not (pdir / "run" / "rehearsal.json").exists() and state_path.read_bytes() == before
    assert "lastRehearsal" not in c.call("GET", f"/projects/{PID}")[1]

    status, posted, _ = c.call("POST", f"/projects/{PID}/run/rehearsal", {})
    assert status == 200 and posted == got
    assert json.loads((pdir / "run" / "rehearsal.json").read_text(encoding="utf-8")) == posted
    meta = c.call("GET", f"/projects/{PID}")[1]
    assert {k: meta["lastRehearsal"][k] for k in ("releaseVersion", "verdict", "readyForClass", "generatedAt")} == {
        "releaseVersion": got["releaseVersion"], "verdict": "ready", "readyForClass": True, "generatedAt": got["generatedAt"]}

    # Guides replaced after the build are not the release's guides: not ready for class, and the app
    # refuses to serve or export them as well.
    readme, instructor = pdir / "build" / "release" / "README.md", pdir / "build" / "instructor" / "instructor-guide.md"
    originals = readme.read_bytes(), instructor.read_bytes()
    readme.write_text("# Student guide\n", encoding="utf-8")
    instructor.write_text("# Instructor guide\n", encoding="utf-8")
    status, tampered, _ = c.call("POST", f"/projects/{PID}/run/rehearsal", {})
    assert status == 200 and tampered["readyForClass"] is False and tampered["readiness"]["guidesBuilt"] is False
    assert tampered["readiness"]["blockers"] == ["GUIDES_MISSING", "RELEASE_NOT_VERIFIED"], tampered["readiness"]
    assert tampered["inputs"]["releaseVerified"] is False
    assert c.call("GET", f"/projects/{PID}/guide?audience=instructor")[0] == 409
    status, body, _ = c.call("GET", f"/projects/{PID}/export")
    assert status == 409 and "does not verify" in body["error"]
    readme.write_bytes(originals[0])
    instructor.write_bytes(originals[1])
    status, ready, _ = c.call("POST", f"/projects/{PID}/run/rehearsal", {})
    assert status == 200 and ready["readyForClass"] is True and ready["readiness"]["guidesBuilt"] is True
    assert c.call("GET", f"/projects/{PID}")[1]["lastRehearsal"]["readyForClass"] is True

    # A running job blocks the write, never the read.
    app["service"].jobs._active[PID] = "oneclick-20260927t000000000000z-abcdef"
    try:
        assert c.call("POST", f"/projects/{PID}/run/rehearsal", {})[0] == 409
        assert c.call("GET", f"/projects/{PID}/run/rehearsal")[0] == 200
    finally:
        app["service"].jobs._active.pop(PID, None)

    # A run bound to another release is refused.
    state = json.loads(state_path.read_text(encoding="utf-8"))
    bound = state["releaseVersion"]
    state_path.write_text(json.dumps({**state, "releaseVersion": "hr-default-ffffffffffff"}), encoding="utf-8")
    status, body, _ = c.call("GET", f"/projects/{PID}/run/rehearsal")
    assert status == 409 and "different release" in body["error"]
    state_path.write_text(json.dumps({**state, "releaseVersion": bound}), encoding="utf-8")

    # A refused reset changes nothing: the rehearsal and its meta mirror stay.
    status, body, _ = c.call("POST", f"/projects/{PID}/run/reset", {"fromStep": "no-such-step"})
    assert status == 409 and (pdir / "run" / "rehearsal.json").exists()
    assert c.call("GET", f"/projects/{PID}")[1]["lastRehearsal"]["readyForClass"] is True

    # Reset archives run/rehearsal.json and clears meta.lastRehearsal (same release: the version rule alone
    # would not notice); the archived complete run still decides, the new run is incomplete.
    assert c.call("POST", f"/projects/{PID}/run/reset", {"fromStep": "judge-stability"})[0] == 200
    assert not (pdir / "run" / "rehearsal.json").exists()
    meta = c.call("GET", f"/projects/{PID}")[1]
    assert meta["lastRehearsal"] is None and meta["lastBuild"]["version"] == bound
    [archive] = [p for p in (pdir / "run" / "history").iterdir()]
    assert json.loads((archive / "rehearsal.json").read_text(encoding="utf-8"))["readyForClass"] is True
    status, after, _ = c.call("GET", f"/projects/{PID}/run/rehearsal")
    assert status == 200 and after["inputs"]["decisiveRun"] == f"history/{archive.name}"
    assert (after["verdict"], after["readyForClass"], after["readiness"]["blockers"]) == ("ready", False, ["REPORT_INCOMPLETE"])
    assert after["caseTable"]["source"] == f"history/{archive.name}"

    # A full reset after a new POST clears the mirror again (the one-click job resets through the same path).
    assert c.call("POST", f"/projects/{PID}/run/rehearsal", {})[0] == 200
    assert c.call("GET", f"/projects/{PID}")[1]["lastRehearsal"]["readyForClass"] is False
    assert c.call("POST", f"/projects/{PID}/run/reset", {})[0] == 200
    assert c.call("GET", f"/projects/{PID}")[1]["lastRehearsal"] is None
    assert len(list((pdir / "run" / "history").iterdir())) == 2

    # A source change unbinds the build.
    assert c.call("PUT", f"/projects/{PID}/files/agent/notes.md", {"content": "# notes\n"})[0] == 200
    status, body, _ = c.call("GET", f"/projects/{PID}/run/rehearsal")
    assert status == 409 and "rebuild" in body["error"]
