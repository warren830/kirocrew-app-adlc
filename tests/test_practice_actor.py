"""Practice actors: the runtime asks every practice case as its own fresh actor (SPEC D3), so the
policy neither warns about several practice actors (golden.single_eval_actor is gone) nor requires
unique authored actorIds (reviews[0]: probe_actor_shared / first_actor_collision are dropped)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from workshop_customizer import script_facts, validator

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKS = ("hr-default", "it-helpdesk", "maintenance")


def _pack(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / "scenarios" / name / "scenario.yaml").read_text(encoding="utf-8"))


def _root(name: str) -> Path:
    return REPO_ROOT / "scenarios" / name


@pytest.mark.parametrize("name", PACKS)
def test_no_single_actor_warning_and_no_actor_rules(name):
    report = validator.validate_scenario_policy(_pack(name), root=_root(name))
    codes = {f.code for f in report.findings}
    assert "golden.single_eval_actor" not in codes
    assert not codes & {"teaching.probe_actor_shared", "teaching.first_actor_collision"}


def test_multi_role_practice_set_passes_without_an_actor_warning():
    data = _pack("maintenance")
    actors = {c["actorId"] for c in data["evaluation"]["goldenSet"] if c["set"] == "practice"}
    assert actors == {"technician-001", "operator-001"}
    report = validator.check_golden_set(data)
    assert report.ok and report.findings == []


def test_shared_authored_actors_are_not_a_policy_problem():
    """hr-default asks every probe and the first conversation as employee-001, like upstream."""
    data = _pack("hr-default")
    probes = [c for c in data["evaluation"]["goldenSet"] if c["set"] == "practice"]
    assert {c["actorId"] for c in probes} == {data["labs"]["teaching"]["firstConversation"]["actorId"]} == {"employee-001"}
    assert validator.validate_scenario_policy(data, root=_root("hr-default")).ok


@pytest.mark.parametrize("name", PACKS)
def test_teaching_facts_give_every_case_its_own_persona(name):
    data = _pack(name)
    facts = script_facts.compute(data, semantics="teaching")
    by_id = {c["id"]: c for c in data["evaluation"]["goldenSet"]}
    assert facts.actor_mode == "per_case"
    assert [c.actor_id for c in facts.eval_cases] == [by_id[c.case_id]["actorId"] for c in facts.eval_cases]
    assert facts.baseline_actor is None and facts.optimize_actor is None
