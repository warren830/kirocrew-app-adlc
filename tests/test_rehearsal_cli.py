"""``python -m workshop_customizer rehearsal`` over a compiled project directory or a scenario file (offline)."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

import teaching_run as tr
from workshop_customizer import cli, compiler, rehearsal
from workshop_customizer.guided_run_store import GuidedRunStore
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_COMMIT = json.loads((REPO_ROOT / "template-lock.json").read_text(encoding="utf-8"))["template"]["commit"]
RELEASE = "hr-default-0123456789ab"


def _write_state(project: Path, run: str, *, release: str = RELEASE, tag: str = "", updated: str = "2026-09-20T10:00:00Z") -> Path:
    store = GuidedRunStore(project)
    state = store.create(project_id=tr.run_pack(run), release_version=release, template_commit=TEMPLATE_COMMIT)
    steps = tr.load_steps(run)
    state["steps"] = {sid: copy.deepcopy(steps[sid]) for sid in state["stepOrder"]}
    for step in state["steps"].values():
        step["commandId"] = f"{step['commandId']}{tag}"
    state["status"] = "passed"
    store.save(state)
    state = json.loads(store.path.read_text(encoding="utf-8"))
    state["updatedAt"] = updated  # save() stamps the wall clock; pin it for the history ordering
    store.path.write_text(json.dumps(state), encoding="utf-8")
    return store.path


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    """Compile each reference pack once (student pack + instructor bundle with the scenario snapshot)."""
    out: dict[str, Path] = {}

    def get(pack: str) -> Path:
        if pack not in out:
            build = tmp_path_factory.mktemp(pack) / "build"
            compiler.compile_pack(load_scenario(REPO_ROOT / "scenarios" / pack / "scenario.yaml"), build, template_commit=TEMPLATE_COMMIT)
            out[pack] = build
        return out[pack]

    return get


def _project(tmp_path: Path, compiled, run: str) -> Path:
    project = tmp_path / "project"
    (project / "build").mkdir(parents=True)
    for child in compiled(tr.run_pack(run)).iterdir():
        target = project / "build" / child.name
        target.symlink_to(child, target_is_directory=child.is_dir())
    _write_state(project, run)
    return project


def _run(capsys, *argv: str) -> tuple[int, str]:
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


@pytest.mark.parametrize(("run", "code"), [("hr-reproduced", 4), ("hr-boundary-sp2", 3), ("it-2026-09-13", 2)])
def test_project_dir_exit_codes_and_json(tmp_path, compiled, capsys, run, code):
    project = _project(tmp_path, compiled, run)
    status, out = _run(capsys, "rehearsal", str(project), "--json", "--out", str(tmp_path / "rehearsal.json"))
    doc = json.loads(out)
    assert status == code and rehearsal.validate_document(doc) == []
    assert json.loads((tmp_path / "rehearsal.json").read_text(encoding="utf-8")) == doc
    assert doc["inputs"]["documentsSearched"] == len(tr.scenario(run)["knowledge"]["documents"])


@pytest.fixture(scope="module")
def released(tmp_path_factory):
    """hr-default compiled and rendered once: a build whose guides and release verify (checksums, RELEASE.json)."""
    from workshop_customizer import render

    build = tmp_path_factory.mktemp("released") / "build"
    pack = compiler.compile_pack(load_scenario(REPO_ROOT / "scenarios" / "hr-default" / "scenario.yaml"), build,
                                 template_commit=TEMPLATE_COMMIT)
    release = render.render_release(pack, REPO_ROOT / "upstream", build / "release", template_commit=TEMPLATE_COMMIT)
    return build, release.version


def _guided_project(tmp_path: Path, released, *, manifest: bool = True) -> Path:
    """An hr-default project whose real build carries both guides and, unless ``manifest`` is False, RELEASE.json."""
    import shutil

    source, version = released
    project = tmp_path / "project"
    build = project / "build"
    build.mkdir(parents=True)
    for child in source.iterdir():
        if child.name == "release" and not manifest:
            shutil.copytree(child, build / "release", symlinks=True)
            (build / "release" / "RELEASE.json").unlink()
        else:
            (build / child.name).symlink_to(child, target_is_directory=child.is_dir())
    _write_state(project, "hr-reproduced", release=version)
    return project


def test_both_guides_make_a_ready_pack_ready_for_class(tmp_path, released, capsys):
    project = _guided_project(tmp_path, released)
    status, out = _run(capsys, "rehearsal", str(project))
    assert status == 0
    assert "verdict: ready" in out and "readyForClass: True" in out and "prompt-fix-where-retrieval-good (prompt_fixable): reproduced" in out
    assert "scenario: build snapshot\n" in out


def test_a_build_without_a_release_manifest_is_never_ready_for_class(tmp_path, released, capsys):
    project = _guided_project(tmp_path, released, manifest=False)
    status, out = _run(capsys, "rehearsal", str(project), "--json")
    doc = json.loads(out)
    assert status == 4 and rehearsal.validate_document(doc) == []
    # The student guide is the release's README.md: without the manifest it cannot be verified either.
    assert (doc["verdict"], doc["readyForClass"], doc["readiness"]["blockers"]) == ("ready", False, ["GUIDES_MISSING", "RELEASE_NOT_VERIFIED"])
    assert (doc["inputs"]["scenario"], doc["inputs"]["releaseVerified"]) == ("build snapshot", False)
    [hint] = [h for h in doc["remediation"] if h["code"] == "RELEASE_NOT_VERIFIED"]
    assert hint["severity"] == "blocking" and hint["asset"]["kind"] == "run"


def test_a_bare_scenario_file_is_labelled_and_never_ready_for_class(tmp_path, capsys):
    """SPEC D8: without a build the live scenario file is judged; the output says so and is never ready."""
    state = _write_state(tmp_path / "p", "hr-reproduced", release="any-release-at-all")
    scenario = REPO_ROOT / "scenarios" / "hr-default" / "scenario.yaml"
    status, out = _run(capsys, "rehearsal", str(scenario), "--run-state", str(state), "--json")
    doc = json.loads(out)
    assert status == 4 and rehearsal.validate_document(doc) == []
    assert (doc["verdict"], doc["readyForClass"]) == ("ready", False)
    assert (doc["inputs"]["scenario"], doc["inputs"]["releaseVerified"]) == ("scenario file", False)
    assert {"SCENARIO_NOT_BUILD_SNAPSHOT", "RELEASE_NOT_VERIFIED"} <= set(doc["readiness"]["blockers"])
    assert {"SCENARIO_NOT_BUILD_SNAPSHOT", "RELEASE_NOT_VERIFIED"} <= {h["code"] for h in doc["remediation"] if h["severity"] == "blocking"}
    status, out = _run(capsys, "rehearsal", str(scenario), "--run-state", str(state))
    assert status == 4 and "scenario: scenario file (release not verified)" in out


def test_a_scenario_file_with_a_build_dir_must_be_the_build_snapshot(tmp_path, released, capsys):
    project = _guided_project(tmp_path, released)
    scenario = REPO_ROOT / "scenarios" / "hr-default" / "scenario.yaml"
    state = project / "run" / "state.json"
    build = project / "build"
    # Byte-identical to build/instructor/scenario-snapshot.yaml: the snapshot is judged, ready for class.
    status, out = _run(capsys, "rehearsal", str(scenario), "--run-state", str(state), "--build-dir", str(build), "--json")
    doc = json.loads(out)
    assert status == 0 and doc["readyForClass"] is True and doc["inputs"]["scenario"] == "build snapshot"
    assert doc["inputs"]["releaseVerified"] is True
    # Edited after the build: refused, never judged as if it were the release.
    edited = tmp_path / "scenario.yaml"
    edited.write_text(scenario.read_text(encoding="utf-8") + "# edited after the build\n", encoding="utf-8")
    status, out = _run(capsys, "rehearsal", str(edited), "--run-state", str(state), "--build-dir", str(build), "--json")
    assert status == 1 and "differs from the build snapshot" in out
    # A build dir without a snapshot is refused as well.
    status, out = _run(capsys, "rehearsal", str(scenario), "--run-state", str(state), "--build-dir", str(tmp_path / "nobuild"))
    assert status == 1 and "has no build snapshot" in out


def test_history_of_the_same_release_is_read(tmp_path, compiled, capsys):
    project = _project(tmp_path, compiled, "hr-reproduced")
    archive = project / "run" / "history" / "0"
    archive.mkdir(parents=True)
    older = tmp_path / "older"
    _write_state(older, "hr-boundary-sp2", tag="-old", updated="2026-09-18T10:00:00Z")
    (archive / "state.json").write_text((older / "run" / "state.json").read_text(encoding="utf-8"), encoding="utf-8")
    status, out = _run(capsys, "rehearsal", str(project), "--json")
    doc = json.loads(out)
    assert status == 4 and [r["source"] for r in doc["runs"]] == ["history/0", "current"] and doc["consistency"] == "mixed"


def test_scenario_file_mode_needs_a_run_state(tmp_path, capsys):
    state = _write_state(tmp_path / "p", "hr-boundary-sp2")
    scenario = REPO_ROOT / "scenarios" / "hr-default" / "scenario.yaml"
    status, out = _run(capsys, "rehearsal", str(scenario), "--json")
    assert status == 1 and "--run-state is required" in out
    status, out = _run(capsys, "rehearsal", str(scenario), "--run-state", str(state), "--json")
    doc = json.loads(out)
    assert status == 3 and doc["remediation"][0]["asset"]["file"] == "knowledge-base/docs/time_off_report.md"


def test_run_state_bound_to_another_release_is_refused(tmp_path, compiled, capsys):
    project = _project(tmp_path, compiled, "hr-reproduced")
    (project / "build" / "release").mkdir()
    (project / "build" / "release" / "RELEASE.json").write_text(json.dumps({"version": "hr-default-ffffffffffff"}), encoding="utf-8")
    status, out = _run(capsys, "rehearsal", str(project))
    assert status == 1 and "bound to release hr-default-0123456789ab" in out
    status, out = _run(capsys, "rehearsal", str(tmp_path / "missing"), "--run-state", str(tmp_path / "nope.json"))
    assert status == 1 and "cannot read" in out


def test_module_entry_point_runs(tmp_path, compiled):
    project = _project(tmp_path, compiled, "hr-boundary-sp2")
    result = subprocess.run([sys.executable, "-m", "workshop_customizer", "rehearsal", str(project), "--json"],
                            cwd=REPO_ROOT, env={"PYTHONPATH": str(REPO_ROOT / "engine"), "PATH": "/usr/bin:/bin"},
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 3, result.stderr
    assert json.loads(result.stdout)["verdict"] == "not_ready"


def test_a_tampered_release_is_never_ready_for_class(tmp_path, released, capsys):
    """The CLI verifies the release like the app: a changed release file is RELEASE_NOT_VERIFIED."""
    project = _guided_project(tmp_path, released, manifest=False)
    source, _version = released
    (project / "build" / "release" / "RELEASE.json").write_bytes((source / "release" / "RELEASE.json").read_bytes())
    script = project / "build" / "release" / "09-run-eval.sh"
    script.write_text(script.read_text(encoding="utf-8") + "# changed after the build\n", encoding="utf-8")
    status, out = _run(capsys, "rehearsal", str(project), "--json")
    doc = json.loads(out)
    assert status == 4 and doc["readiness"]["blockers"] == ["RELEASE_NOT_VERIFIED"] and doc["inputs"]["releaseVerified"] is False
