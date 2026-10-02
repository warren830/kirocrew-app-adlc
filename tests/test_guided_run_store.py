import json
import pytest
from workshop_customizer.guided_run import GUIDE_STEPS
from workshop_customizer.guided_run_store import GuidedRunStore


def test_create_reload_and_bind_release(tmp_path):
    store = GuidedRunStore(tmp_path / "project")
    state = store.create(project_id="northstar", release_version="northstar-abc", template_commit="2450922")
    assert state["releaseVersion"] == "northstar-abc"
    assert state["stepOrder"] == [s.id for s in GUIDE_STEPS]
    assert list(state["steps"]) == [s.id for s in GUIDE_STEPS]
    assert store.load()["templateCommit"] == "2450922"
    assert not store.path.with_suffix(".json.tmp").exists()


def test_create_is_idempotent(tmp_path):
    store = GuidedRunStore(tmp_path / "project")
    first = store.create(project_id="p", release_version="v1", template_commit="c1")
    second = store.create(project_id="p", release_version="v2", template_commit="c2")
    assert second["releaseVersion"] == first["releaseVersion"] == "v1"


def test_incompatible_state_fails_closed(tmp_path):
    store = GuidedRunStore(tmp_path / "project")
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"schemaVersion": 1, "steps": {}}))
    with pytest.raises(ValueError, match="incompatible"):
        store.load()


def test_partial_reset_retains_earlier_passed_evidence(tmp_path):
    store = GuidedRunStore(tmp_path / "project")
    state = store.create(project_id="p", release_version="v1", template_commit="c1")
    for step in state["steps"].values():
        step.update(status="passed", commandId="old-command")
    store.save(state)
    reset = store.reset(project_id="p", release_version="v1", template_commit="c1", from_step="baseline")
    assert reset["steps"]["evaluators"]["commandId"] == "old-command"
    assert reset["steps"]["baseline"]["status"] == "not_started"
    assert reset["steps"]["optimize"]["commandId"] is None
    assert reset["report"] is None
    with pytest.raises(ValueError, match="different releases"):
        store.reset(project_id="p", release_version="v2", template_commit="c1", from_step="baseline")
