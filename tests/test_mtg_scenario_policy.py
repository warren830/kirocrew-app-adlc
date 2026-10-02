"""Mind the Goal judges each scenario against its own policy (SPEC D5).

render patches only the release copy of mind_the_goal/prompts.py (five anchors; confinement is pinned in
test_regression_surfaces). These tests import the patched module in isolation and check the prompt it
builds: domain wording, the pack's user label, the scenario policy, unchanged output format and RCOF
codes, and that hostile scenario text stays inert.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
import yaml

from workshop_customizer import compiler, render
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = REPO_ROOT / "upstream"
TEMPLATE_COMMIT = json.loads((REPO_ROOT / "template-lock.json").read_text())["template"]["commit"]
PACKS = ("hr-default", "it-helpdesk", "maintenance")
TURNS = [{"turn_number": 1, "user_message": "Can I get admin rights?", "agent_response": "I cannot grant that.",
          "tool_calls": [{"tool_name": "lookup", "output": {"status": "success"}}]}]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _render_prompts(data: dict, tmp_path: Path) -> Path:
    """render's MtG patches (the same function render_release applies) on a copy of the upstream module."""
    target = tmp_path / "prompts.py"
    records: list[render.PatchRecord] = []
    text = (UPSTREAM / render.MTG_PROMPTS_REL).read_text(encoding="utf-8")
    target.write_text(render.patch_mtg_prompts(text, data, records), encoding="utf-8")
    assert [r.name for r in records] == ["mtg-judge-scenario-policy", "mtg-dialog-source", "mtg-rcof-e2-policy",
                                         "mtg-rcof-e6-policy", "mtg-user-label"]
    return target


@pytest.fixture(scope="module", params=PACKS)
def release(request, tmp_path_factory):
    base = tmp_path_factory.mktemp(f"mtg-{request.param}")
    scenario = load_scenario(REPO_ROOT / "scenarios" / request.param / "scenario.yaml")
    pack = compiler.compile_pack(scenario, base / "build", template_commit=TEMPLATE_COMMIT)
    rendered = render.render_release(pack, UPSTREAM, base / "release", template_commit=TEMPLATE_COMMIT)
    return scenario.data, rendered.release_dir


def test_prompt_contract(release):
    data, root = release
    module = _load(root / render.MTG_PROMPTS_REL, f"mtg_prompts_{data['id'].replace('-', '_')}")
    prompt = module.build_evaluation_prompt(TURNS)
    assert "output format:" in prompt and "dialog_id: xx" in prompt
    for code in ("E1", "E2", "E3", "E4", "E5", "E6", "E7"):
        assert f"  {code} " in prompt, code
    assert "employee chatbot" not in prompt and "employee experience chatbot" not in prompt
    assert data["displayName"] in prompt and "must hand off or escalate when" in prompt
    assert render._clip(data["agent"]["prohibitedBehaviors"][0]) in prompt
    assert "a refusal or handoff the scenario policy requires is success, not E2" in prompt
    expected_label = {"hr-default": "Employee"}.get(data["id"], "User")  # two roles and no judge.userLabel → User
    assert module.USER_ROLE_LABEL == render.mtg_user_label(data) == expected_label
    assert f"  {expected_label}: Can I get admin rights?" in prompt
    assert "[Tool: lookup → success]" in prompt


def test_user_label_rules():
    data = yaml.safe_load((REPO_ROOT / "scenarios" / "it-helpdesk" / "scenario.yaml").read_text(encoding="utf-8"))
    assert render.mtg_user_label(data) == "User"
    data["agent"]["roles"] = data["agent"]["roles"][:1]
    assert render.mtg_user_label(data) == "Employee"
    data["evaluation"]["judge"] = {"userLabel": "Staff member"}
    assert render.mtg_user_label(data) == "Staff member"


def test_hostile_scenario_text_is_inert(tmp_path):
    data = yaml.safe_load((REPO_ROOT / "scenarios" / "maintenance" / "scenario.yaml").read_text(encoding="utf-8"))
    hostile = 'Never print {x} or {0} or """triple""" or a back\\slash \\n or\na newline'
    data["agent"]["prohibitedBehaviors"] = [hostile] + data["agent"]["prohibitedBehaviors"]
    data["displayName"] = "Plant {assistant} 'quoted'"
    module = _load(_render_prompts(data, tmp_path), "mtg_prompts_hostile")
    prompt = module.build_evaluation_prompt(TURNS)  # no KeyError / IndexError from str.format
    assert "Never print {x} or {0} or \"\"\"triple\"\"\" or a back\\slash \\n or a newline" in prompt
    assert "Plant {assistant} 'quoted'" in prompt


def _long_policy(handoffs: int, prohibitions: int) -> dict:
    data = yaml.safe_load((REPO_ROOT / "scenarios" / "it-helpdesk" / "scenario.yaml").read_text(encoding="utf-8"))
    long_item = "x" * 400
    data["agent"]["scope"] = [f"scope {i} {long_item}" for i in range(12)]
    data["agent"]["outOfScope"] = [f"out {i} {long_item}" for i in range(12)]
    data["agent"]["handoffConditions"] = [f"handoff {i} {long_item}" for i in range(handoffs)]
    data["agent"]["prohibitedBehaviors"] = [f"never {i} {long_item}" for i in range(prohibitions)]
    return data


def test_prompt_is_bounded_by_dropping_scope_bullets():
    prompt = render.mtg_judge_system_prompt(_long_policy(6, 6))
    assert len(prompt) <= render.MTG_PROMPT_LIMIT
    assert "out 0 " not in prompt  # out-of-scope bullets are dropped first
    assert "scope 0 " in prompt and "scope 11 " not in prompt  # then scope bullets from the end; one survives
    assert all(f"- handoff {i} " in prompt for i in range(6)) and all(f"- never {i} " in prompt for i in range(6))
    assert all(len(line) <= 302 for line in prompt.splitlines())  # every bullet clipped to 300 characters


def test_handoffs_and_prohibitions_are_never_dropped():
    prompt = render.mtg_judge_system_prompt(_long_policy(12, 12))
    assert len(prompt) > render.MTG_PROMPT_LIMIT  # the bound yields to the policy that decides refusals
    assert all(f"- handoff {i} " in prompt for i in range(12)) and all(f"- never {i} " in prompt for i in range(12))
    assert "out 0 " not in prompt and prompt.count("- scope ") == 1


def test_small_policy_is_complete():
    data = yaml.safe_load((REPO_ROOT / "scenarios" / "hr-default" / "scenario.yaml").read_text(encoding="utf-8"))
    prompt = render.mtg_judge_system_prompt(data)
    for key in ("scope", "outOfScope", "handoffConditions", "prohibitedBehaviors"):
        for item in data["agent"][key]:
            assert f"- {render._clip(item)}\n" in prompt, item
    assert prompt.startswith("You are a helpful AI assistant. You will act as a judge to evaluate quality of Enterprise HR Q&A Agent")
