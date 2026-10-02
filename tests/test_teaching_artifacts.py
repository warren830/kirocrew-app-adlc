"""labs.teaching derived artifacts (designs[0].dataModel): the student-safe pack/pack.json ``teaching``
object, the instructor-only instructor/teaching.json and RELEASE.json ``teaching``.

All three record the same derived order (teaching.derived_order), which is also the order the rendered
09/10 ask the practice cases in (ScriptFacts). Bait / absent terms, defects, mechanisms and designs stay
out of everything a student receives.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from workshop_customizer import compiler, render, script_facts, teaching
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = REPO_ROOT / "upstream"
TEMPLATE_COMMIT = json.loads((REPO_ROOT / "template-lock.json").read_text())["template"]["commit"]
PACKS = ("hr-default", "it-helpdesk", "maintenance")
STUDENT_KEYS = {"firstConversation", "evalOrder", "probeCaseIds", "probeStartIndex", "stabilityCaseId", "phenomena"}
INSTRUCTOR_ONLY_KEYS = {"baselineDefects", "baitTerms", "absentTerms", "mechanism", "design", "defectIds",
                        "candidateFix", "baselineMarker", "mustNotMention"}

_BUILT: dict[str, tuple] = {}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    def get(pack_id: str):
        if pack_id not in _BUILT:
            base = tmp_path_factory.mktemp(f"ta-{pack_id}")
            scenario = load_scenario(REPO_ROOT / "scenarios" / pack_id / "scenario.yaml")
            pack = compiler.compile_pack(scenario, base / "out", template_commit=TEMPLATE_COMMIT)
            release = render.render_release(pack, UPSTREAM, base / "out" / "release", template_commit=TEMPLATE_COMMIT)
            _BUILT[pack_id] = (scenario.data, pack, release)
        return _BUILT[pack_id]

    yield get
    _BUILT.clear()


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


@pytest.mark.parametrize("pack_id", PACKS)
def test_pack_json_carries_the_student_safe_teaching_object(pack_id, built):
    data, pack, release = built(pack_id)
    manifest = json.loads((release.release_dir / "pack" / "pack.json").read_text(encoding="utf-8"))
    view = manifest["teaching"]
    assert set(view) == STUDENT_KEYS
    assert not (_keys(view) & INSTRUCTOR_ONLY_KEYS), _keys(view) & INSTRUCTOR_ONLY_KEYS
    first = data["labs"]["teaching"]["firstConversation"]
    assert view["firstConversation"] == {k: first[k] for k in ("query", "actorId", "unknownContext", "label") if k in first}
    assert [p["id"] for p in view["phenomena"]] == [p["id"] for p in data["labs"]["teaching"]["phenomena"]]
    for item in view["phenomena"]:
        assert set(item) == {"id", "kind", "teachingPoint", "documentIds" if item["kind"] == "noise_grounding" else "caseIds"}
    holdout = {c["id"] for c in data["evaluation"]["goldenSet"] if c["set"] == "holdout"}
    assert not holdout & {cid for item in view["phenomena"] for cid in item.get("caseIds", [])}
    assert "labs.teaching" in pack.derivation["pack/pack.json"]


@pytest.mark.parametrize("pack_id", PACKS)
def test_every_artifact_records_the_order_the_scripts_ask(pack_id, built):
    data, pack, release = built(pack_id)
    facts = script_facts.compute(data)
    student = json.loads((release.release_dir / "pack" / "pack.json").read_text(encoding="utf-8"))["teaching"]
    manifest = json.loads((release.release_dir / render.MANIFEST_NAME).read_text(encoding="utf-8"))
    instructor = json.loads((pack.instructor_dir / "teaching.json").read_text(encoding="utf-8"))
    order = teaching.derived_order(data)
    assert order["evalOrder"] == list(facts.golden_ids) == list(teaching.eval_order_ids(data))
    assert order["probeStartIndex"] == list(facts.golden_probe).index(1)
    assert order["evalOrder"][-1] == order["stabilityCaseId"]  # 13 re-scores the last probe asked
    assert set(order["probeCaseIds"]) == set(facts.probe_case_ids)
    for view in (student, manifest["teaching"], instructor):
        assert {k: view[k] for k in order} == order
    assert manifest["teaching"]["firstConversationActorId"] == facts.first_conversation.actor_id


@pytest.mark.parametrize("pack_id", PACKS)
def test_instructor_teaching_json_is_complete_and_never_ships(pack_id, built):
    data, pack, release = built(pack_id)
    instructor = json.loads((pack.instructor_dir / "teaching.json").read_text(encoding="utf-8"))
    assert instructor["schema"] == "workshop-customizer/teaching/1"
    assert instructor["labsTeaching"] == data["labs"]["teaching"]  # defects, bait and absent terms included
    assert instructor["thresholds"] == dict(teaching.THRESHOLDS)
    assert set(instructor["designs"]) == {p["id"] for p in teaching.phenomena(data, teaching.L1_KINDS)}
    assert "instructor/teaching.json" in pack.files and pack.derivation["instructor/teaching.json"]
    assert not [rel for rel in release.files if rel.endswith("teaching.json")]
    assert not list(release.release_dir.rglob("teaching.json"))


def test_student_view_drops_holdout_ids_and_is_absent_without_a_declaration():
    data = yaml.safe_load((REPO_ROOT / "scenarios" / "it-helpdesk" / "scenario.yaml").read_text(encoding="utf-8"))
    raw = copy.deepcopy(data)
    holdout = next(c["id"] for c in raw["evaluation"]["goldenSet"] if c["set"] == "holdout")
    raw["labs"]["teaching"]["phenomena"][0]["caseIds"].append(holdout)  # the loader refuses this; the view still drops it
    assert holdout not in teaching.student_view(raw)["phenomena"][0]["caseIds"]
    del raw["labs"]["teaching"]
    assert teaching.student_view(raw) is None and teaching.instructor_view(raw) is None and teaching.release_view(raw) is None
    assert "teaching" not in compiler.render_pack_manifest(raw, TEMPLATE_COMMIT, [])
