"""P4 apply: acknowledgement, orphan pruning behind a snapshot, exact revert, snapshot retention,
rollback of writes and deletions, the generation/.lock flock, YAML scenario writes, editLog and
createdFromTemplate.  All offline; routes.py is driven directly."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
from test_generation_routes import ACK, BRIEF, PID, _answer, _generate, _on_disk, _payload, _project  # noqa: E402
from test_kiro_generation import gen  # noqa: E402


@pytest.fixture(autouse=True)
def _default_generation_timeouts(monkeypatch):
    monkeypatch.delenv(gen.TIMEOUT_ENV, raising=False)


def _template_project(data: Path, template: str = "hr-default", *, project_id: str = PID) -> Path:
    """A project created from a template the way the app did before P4 (every template file copied)."""
    pdir = data / "projects" / project_id
    shutil.copytree(REPO / "scenarios" / template, pdir)
    meta = {"id": project_id, "displayName": "Cold Chain Test", "packKind": "reference", "customer": "",
            "status": "intake", "template": template}
    (pdir / "project.json").write_text(json.dumps(meta), encoding="utf-8")
    return pdir


def _tree(pdir: Path) -> dict[str, str]:
    """sha256 of every file outside the app-managed dirs (what a revert must restore exactly)."""
    return {p.relative_to(pdir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(pdir.rglob("*")) if p.is_file() and p.relative_to(pdir).parts[0] not in gen.APP_MANAGED_DIRS
            and p.name != "project.json"}


HR_ORPHANS = {
    "skills/deep-policy-analysis/SKILL.md", "skills/leave-calculator/SKILL.md", "tools/upstream-hr-tools-schema.json",
}


def test_apply_requires_an_explicit_acknowledgement(tmp_path):
    data = tmp_path / "data"
    _project(data)
    record = _generate(data, _payload())
    for body in (None, {}, {"acknowledged": "true"}, {"acknowledged": 1}):
        with pytest.raises(gen.GenerationError, match="acknowledged must be true"):
            gen.apply_generation(record["id"], data, body)
    assert _on_disk(data, record["id"])["status"] == "ready"


def test_apply_prunes_template_orphans_behind_a_snapshot(tmp_path):
    data = tmp_path / "data"
    pdir = _template_project(data)
    (pdir / "NOTES.txt").write_text("SA notes kept at the project root\n", encoding="utf-8")
    (pdir / "agent" / "notes.md").write_text("# stray draft notes\n", encoding="utf-8")
    before = _tree(pdir)
    record = _generate(data, _payload())
    result = record["result"]
    hr_docs = {f"knowledge-base/docs/{p.name}" for p in (REPO / "scenarios" / "hr-default" / "knowledge-base" / "docs").iterdir()}
    expected = sorted((HR_ORPHANS | hr_docs | {"agent/notes.md"}) - set(result["files"]))
    assert result["deleteOrphans"] == expected and result["untracked"] == ["NOTES.txt"]
    assert result["diff"]["files"]["deleted"] == expected
    assert gen._public_record(_on_disk(data, record["id"]))["deleteOrphans"] == expected

    applied = gen.apply_generation(record["id"], data, ACK)
    assert applied["deleted"] == expected and applied["untracked"] == ["NOTES.txt"]
    for rel in expected:
        assert not (pdir / rel).exists(), rel
    assert not (pdir / "skills" / "leave-calculator").exists() and not (pdir / "tools").exists()  # empty dirs removed
    assert (pdir / "NOTES.txt").is_file() and (pdir / "knowledge-base" / "docs").is_dir()
    assert gen.orphan_files(pdir, yaml.safe_load((pdir / "scenario.yaml").read_text()))[0] == []

    snapshot = pdir / applied["snapshot"] / "snapshot.json"
    manifest = json.loads(snapshot.read_text())
    rels = {e["rel"] for e in manifest["entries"]}
    assert set(expected) <= rels and "scenario.yaml" in rels
    assert manifest["packDigestAfter"] == gen.pack_digest(pdir)
    meta = json.loads((pdir / "project.json").read_text())
    assert meta["lastGeneration"]["deleted"] == len(expected) and meta["createdFromTemplate"] == "hr-default"
    assert meta["template"] == "kiro-generated" and meta["editLog"] == {
        "manualScenarioSaves": 0, "manualFileSaves": 0, "kiroApplies": 1, "kiroReverts": 0}

    reverted = gen.revert_generation(record["id"], data, {"acknowledged": True})
    assert reverted["reverted"] is True and set(expected) <= set(reverted["restored"])
    assert _tree(pdir) == before  # exact bytes, every deleted file recreated
    meta = json.loads((pdir / "project.json").read_text())
    assert meta["editLog"]["kiroReverts"] == 1 and meta["lastGeneration"] is None and meta["status"] == "truth-review"
    assert _on_disk(data, record["id"])["status"] == "reverted"
    with pytest.raises(gen.GenerationError, match="only an applied generation"):
        gen.revert_generation(record["id"], data, {"acknowledged": True})


def test_revert_requires_ack_and_refuses_after_a_later_edit(tmp_path):
    data = tmp_path / "data"
    pdir = _template_project(data)
    record = _generate(data, _payload())
    gen.apply_generation(record["id"], data, ACK)
    with pytest.raises(gen.GenerationError, match="acknowledged must be true"):
        gen.revert_generation(record["id"], data, {})
    doc = next(iter(sorted((pdir / "knowledge-base" / "docs").iterdir())))
    doc.write_text(doc.read_text() + "\nSA edit after apply.\n")
    with pytest.raises(gen.GenerationError, match="project changed after apply"):
        gen.revert_generation(record["id"], data, {"acknowledged": True})


def test_apply_never_writes_through_a_link_into_app_managed_state(tmp_path):
    """Generation apply shares the editor's confinement rule (routes.pack_path): a link inside the project
    (agent -> run) cannot carry Kiro output into run/, where rehearsal.json and readyForClass live
    (review finding: apply used to check only that the resolved path stays inside the project)."""
    data = tmp_path / "data"
    pdir = _project(data)
    (pdir / "run").mkdir()
    (pdir / "agent").symlink_to(pdir / "run", target_is_directory=True)
    record = _generate(data, _payload())
    assert any(rel.startswith("agent/") for rel in record["result"]["files"])
    scenario_before = (pdir / "scenario.yaml").read_bytes()
    with pytest.raises(gen.GenerationError, match="managed by the app: agent/"):
        gen.apply_generation(record["id"], data, ACK)
    assert list((pdir / "run").iterdir()) == [] and (pdir / "scenario.yaml").read_bytes() == scenario_before
    assert not (pdir / "generation" / "snapshots" / record["id"]).exists()  # refused before the snapshot is written
    assert _on_disk(data, record["id"])["status"] == "ready"


def test_revert_never_touches_app_managed_state(tmp_path):
    data = tmp_path / "data"
    pdir = _project(data)
    record = _generate(data, _payload())
    gen.apply_generation(record["id"], data, ACK)
    (pdir / "run").mkdir()
    (pdir / "run" / "rehearsal.json").write_text('{"readyForClass": false}', encoding="utf-8")
    manifest_path = pdir / "generation" / "snapshots" / record["id"] / "snapshot.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"].append({"rel": "Run/rehearsal.json", "existed": False, "sha256Before": None})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(gen.GenerationError, match="managed by the app: Run/rehearsal.json"):
        gen.revert_generation(record["id"], data, {"acknowledged": True})
    assert (pdir / "run" / "rehearsal.json").read_text(encoding="utf-8") == '{"readyForClass": false}'
    assert _on_disk(data, record["id"])["status"] == "applied"


@pytest.mark.parametrize("rel", ["run/rehearsal.json", "Run/rehearsal.json", "agent/link/rehearsal.json", "project.json",
                                 "agent/Scenario.YAML", "../other/agent/x.md", "materials/text/x.txt", "Generation/x"])
def test_pack_path_refuses_app_managed_state_by_case_or_link(tmp_path, rel):
    pdir = tmp_path / "p"
    (pdir / "run").mkdir(parents=True)
    (pdir / "agent").mkdir()
    (pdir / "agent" / "link").symlink_to(pdir / "run", target_is_directory=True)
    with pytest.raises(gen.GenerationError, match="managed by the app|stay inside the project"):
        gen.pack_path(pdir, rel)
    assert gen.pack_path(pdir, "agent/baseline-prompt.md") == (pdir / "agent" / "baseline-prompt.md").resolve()


def test_revert_restores_the_previous_applied_generation_as_last_generation(tmp_path):
    data = tmp_path / "data"
    pdir = _project(data)
    first = _generate(data, _payload())
    gen.apply_generation(first["id"], data, ACK)
    payload = _payload()
    payload["summary"] = "second draft"
    changed = sorted(payload["files"])[0]
    payload["files"][changed] += "\nSecond draft paragraph.\n"
    second = _generate(data, payload)
    assert second["result"]["changedFiles"] == [changed]
    applied = gen.apply_generation(second["id"], data, ACK)
    assert applied["written"] == [changed]
    with pytest.raises(gen.GenerationError, match="project changed after apply"):
        gen.revert_generation(first["id"], data, {"acknowledged": True})  # the second apply came after it
    gen.revert_generation(second["id"], data, {"acknowledged": True})
    meta = json.loads((pdir / "project.json").read_text())
    assert meta["lastGeneration"]["id"] == first["id"]
    gen.revert_generation(first["id"], data, {"acknowledged": True})  # now the project is as the first apply left it
    assert yaml.safe_load((pdir / "scenario.yaml").read_text()) == {"schemaVersion": 1, "id": PID}


def test_snapshot_retention_keeps_five(tmp_path):
    data = tmp_path / "data"
    pdir = _project(data)
    ids = []
    for index in range(7):
        payload = _payload()
        payload["files"][sorted(payload["files"])[0]] += f"\nround {index}\n"
        record = _generate(data, payload)
        gen.apply_generation(record["id"], data, ACK)
        ids.append(record["id"])
        time.sleep(0.01)
    kept = sorted(p.name for p in (pdir / "generation" / "snapshots").iterdir())
    # Seven applies within the same second: 'at' cannot order them, the write time can (the generation id is a
    # content hash, so ordering by it dropped the newest snapshot and this assertion flaked).
    assert len(kept) == gen.SNAPSHOT_KEEP and sorted(ids[-gen.SNAPSHOT_KEEP:]) == kept
    with pytest.raises(gen.GenerationError, match="no longer kept|project changed"):
        gen.revert_generation(ids[0], data, {"acknowledged": True})


def test_apply_rolls_back_writes_and_deletions_on_failure(tmp_path, monkeypatch):
    data = tmp_path / "data"
    pdir = _template_project(data)
    before = _tree(pdir)
    meta_before = (pdir / "project.json").read_bytes()
    record = _generate(data, _payload())
    real = gen._atomic_text

    def failing(path, text):
        if Path(path).name == "scenario.yaml":
            raise OSError("disk full")
        return real(path, text)

    monkeypatch.setattr(gen, "_atomic_text", failing)
    with pytest.raises(OSError, match="disk full"):
        gen.apply_generation(record["id"], data, ACK)
    assert _tree(pdir) == before and (pdir / "project.json").read_bytes() == meta_before
    assert not (pdir / "generation" / "snapshots" / record["id"]).exists()
    assert _on_disk(data, record["id"])["status"] == "ready"


def test_verify_bundle_refuses_a_tampered_orphan_list(tmp_path):
    data = tmp_path / "data"
    pdir = _template_project(data)
    record = _generate(data, _payload())
    path = data / "generations" / f"{record['id']}.json"
    stored = json.loads(path.read_text())
    stored["result"]["deleteOrphans"] = [*stored["result"]["deleteOrphans"], "NOTES-not-an-orphan.md"]
    path.write_text(json.dumps(stored))
    with pytest.raises(gen.GenerationError, match="orphan list differs"):
        gen.apply_generation(record["id"], data, ACK)
    assert (pdir / "tools" / "upstream-hr-tools-schema.json").is_file()


def test_apply_writes_yaml_and_only_changed_files(tmp_path):
    data = tmp_path / "data"
    pdir = _project(data)
    record = _generate(data, _payload())
    gen.apply_generation(record["id"], data, ACK)
    text = (pdir / "scenario.yaml").read_text(encoding="utf-8")
    assert not text.lstrip().startswith("{") and yaml.safe_load(text) == record["result"]["scenario"]
    again = _generate(data, _payload())  # the same draft again: nothing on disk differs
    applied = gen.apply_generation(again["id"], data, ACK)
    assert applied["written"] == [] and applied["deleted"] == []
    assert json.loads((pdir / "project.json").read_text())["editLog"]["kiroApplies"] == 2


def test_orphan_files_classification(tmp_path):
    pdir = tmp_path / "p"
    for rel in ("knowledge-base/docs/a.md", "knowledge-base/docs/b.md", "agent/baseline.md", "tools/x.json",
                "generate_content.py", "NOTES.txt", "docs/extra.md", "build/validate.json", "materials/raw/m.pdf",
                "generation/brief.md", "knowledge-base/docs/.draft.md.tmp", "skills/s/__pycache__/m.pyc"):
        (pdir / rel).parent.mkdir(parents=True, exist_ok=True)
        (pdir / rel).write_text(rel)
    scenario = {"knowledge": {"documents": [{"file": "knowledge-base/docs/a.md"}]},
                "prompts": {"baselineFile": "./agent/baseline.md"}}
    orphans, untracked = gen.orphan_files(pdir, scenario)
    assert orphans == ["generate_content.py", "knowledge-base/docs/b.md", "tools/x.json"]
    assert untracked == ["NOTES.txt", "docs/extra.md"]


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe.tmp"
    probe.write_text("x")
    try:
        return (directory / "caseprobe.tmp").exists()
    finally:
        probe.unlink()


def test_a_case_only_rename_never_deletes_the_written_file(tmp_path):
    """On a case-insensitive filesystem (macOS APFS) the old spelling IS the new file: it is never an orphan."""
    data = tmp_path / "data"
    pdir = _project(data)
    gen.apply_generation(_generate(data, _payload())["id"], data, ACK)
    payload = _payload()
    doc = payload["scenario"]["knowledge"]["documents"][0]
    old = doc["file"]
    new = old.rsplit("/", 1)[0] + "/" + old.rsplit("/", 1)[1][:-3].upper() + ".md"
    payload["files"][new] = payload["files"].pop(old) + "\nrevised\n"
    doc["file"] = new
    record = _generate(data, payload)
    assert record["status"] == "ready", record.get("error")
    assert old not in record["result"]["deleteOrphans"]
    applied = gen.apply_generation(record["id"], data, ACK)
    assert old not in applied["deleted"] and (pdir / new).read_text().endswith("revised\n")
    insensitive = _case_insensitive(pdir)
    assert (old in applied["untracked"]) is not insensitive  # a distinct file only on a case-sensitive filesystem


def test_orphan_files_skips_case_variants_of_referenced_files(tmp_path, monkeypatch):
    pdir = tmp_path / "p"
    (pdir / "knowledge-base" / "docs").mkdir(parents=True)
    (pdir / "knowledge-base" / "docs" / "guide.md").write_text("x")
    scenario = {"knowledge": {"documents": [{"file": "knowledge-base/docs/GUIDE.md"}]}}
    monkeypatch.setattr(gen, "_same_file", lambda a, b: True)  # a case-insensitive filesystem
    assert gen.orphan_files(pdir, scenario) == ([], [])
    monkeypatch.setattr(gen, "_same_file", lambda a, b: False)  # a case-sensitive one: reported, never deleted
    assert gen.orphan_files(pdir, scenario) == ([], ["knowledge-base/docs/guide.md"])


def test_referenced_files_differing_only_by_case_are_refused(tmp_path):
    data = tmp_path / "data"
    _project(data)
    payload = _payload()
    docs = payload["scenario"]["knowledge"]["documents"]
    clash = docs[0]["file"].rsplit("/", 1)[0] + "/" + docs[0]["file"].rsplit("/", 1)[1].upper()
    docs[1]["file"] = clash
    payload["files"][clash] = "# clash\n"
    record = _generate(data, payload)
    assert record["status"] == "failed" and "differ only by letter case" in record["error"]


def test_app_managed_dirs_are_outside_the_pack_digest(tmp_path):
    pdir = _project(tmp_path / "data")
    before = gen.pack_digest(pdir)
    for rel in ("materials/index.json", "materials/raw/mat-0123456789ab.pdf", "generation/brief.md", "generation/.lock"):
        (pdir / rel).parent.mkdir(parents=True, exist_ok=True)
        (pdir / rel).write_text("x")
    assert gen.pack_digest(pdir) == before
    assert {"materials", "generation"} <= gen.APP_MANAGED_DIRS


def test_project_lock_is_shared_reentrant_and_times_out(tmp_path):
    pdir = _project(tmp_path / "data")
    with gen.project_lock(pdir):
        with gen.project_lock(pdir):  # reentrant in the same thread
            pass
        failures = []

        def other():
            try:
                with gen.project_lock(pdir, timeout=0.2):
                    failures.append("acquired")
            except gen.ProjectBusy as exc:
                failures.append(str(exc))

        worker = threading.Thread(target=other)
        worker.start()
        worker.join()
        assert failures and failures[0].startswith("project busy")
    with gen.project_lock(pdir, timeout=0.2):  # released
        pass


def test_apply_waits_for_the_lock_then_refuses_with_409(tmp_path, monkeypatch):
    data = tmp_path / "data"
    pdir = _project(data)
    record = _generate(data, _payload())
    monkeypatch.setattr(gen, "LOCK_TIMEOUT_SECS", 0.2)
    held = threading.Event()
    release = threading.Event()

    def writer():
        with gen.project_lock(pdir):
            held.set()
            release.wait(5)

    worker = threading.Thread(target=writer)
    worker.start()
    held.wait(5)
    try:
        with pytest.raises(gen.ProjectBusy):
            gen.apply_generation(record["id"], data, ACK)
    finally:
        release.set()
        worker.join()
    assert gen.apply_generation(record["id"], data, ACK)["applied"] is True
