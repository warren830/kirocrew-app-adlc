"""P1 generation contract for the teaching skeleton: contracts/teaching.md and contracts/l1.md are
assembled into the Kiro task, their examples are schema-valid, and the agent prompt and the SA skill
describe the same rules (offline, deterministic)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import jsonschema

from workshop_customizer.scenario import load_schema

REPO_ROOT = Path(__file__).resolve().parents[1]


def _routes():
    spec = importlib.util.spec_from_file_location("wc_routes_teaching", REPO_ROOT / "app" / "backend" / "routes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generation_contract_asks_for_the_teaching_skeleton_and_l1_expectations():
    gen = _routes()
    task, shas = gen._compose_task(project_id="cold-chain-test", display_name="Cold Chain", pack_kind="customer",
                                   customer="", brief="A cold-chain incident assistant for warehouse operators.")
    for needle in ("TEACHING SKELETON", "requiredTools EXACTLY", 'mechanism "absent"', "candidateFix", "absentTerms",
                   "baitTerms", "unknownContext", "stabilityCaseId", "12-16 golden cases", "5-7 practice cases",
                   "Declare 4 probes", "written in the content language (en)", "teaching_design",
                   "L1 EXPECTATIONS", "mustMentionAnyOf", "escalationMarkers", "judge.userLabel",
                   "GUIDE NARRATIVE", '"id":"guide-narrative","provenance":"ai_draft"', "never the word holdout"):
        assert needle in task, needle
    assert shas["teaching"] and shas["l1"] and shas["guides"]
    assert "${" not in task and "<!-- contract:" not in task
    assert len(task.encode("utf-8")) < gen.MAX_TASK_BYTES // 20
    zh = gen._build_task(project_id="cold-chain-test", display_name="冷链", pack_kind="customer", customer="",
                         brief="一个为仓库操作员处理冷链事故的助手，需要回答温度偏差和交接流程的问题。")
    assert "written in the content language (zh-CN)" in zh


def test_contract_examples_are_schema_valid():
    gen = _routes()
    task = gen._build_task(project_id="cold-chain-test", display_name="Cold Chain", pack_kind="customer", customer="",
                           brief="A cold-chain incident assistant for warehouse operators.")
    example = json.loads(task.split("Example labs.teaching (ids are illustrative):\n", 1)[1].split("\nTEACHING SELF-CHECK", 1)[0])
    schema = load_schema()
    teaching_schema = {"$schema": schema.get("$schema"), "$defs": schema["$defs"], "$ref": "#/$defs/teaching"}
    jsonschema.validate(example, teaching_schema)
    assert example["firstConversation"]["mustNotMention"]  # the example declares the Memory check it asks for
    # T3: every prompt_fixable phenomenon of the example lists a defect whose baselineMarker bites (asks EVERY
    # answer to go beyond the documents), and the engine's live-learned rule agrees.
    defects = {d["id"]: d for d in example["baselineDefects"]}
    fixable = [p for p in example["phenomena"] if p["kind"] == "prompt_fixable"]
    assert fixable
    for phenomenon in fixable:
        markers = [defects[d].get("baselineMarker") for d in phenomenon["defectIds"] if defects[d].get("baselineMarker")]
        assert markers, phenomenon["id"]
        assert any("every answer" in m.lower() and "beyond the documents" in m for m in markers), markers
    from workshop_customizer import teaching_policy

    codes = {f.code for f in teaching_policy.check_teaching({"labs": {"teaching": example}}).findings}
    assert "teaching.defect_marker_absent" not in codes
    shape = json.loads(task.split("WORKSHOP_PACK_JSON_BEGIN\n")[-1].split("\nWORKSHOP_PACK_JSON_END")[0])
    assert set(shape["scenario"]["labs"]["teaching"]) == {"firstConversation", "baselineDefects", "phenomena", "stabilityCaseId"}
    assert {"l1", "judge"} <= set(shape["scenario"]["evaluation"])


def test_agent_prompt_carries_the_teaching_rule_and_stays_tool_less():
    agent = json.loads((REPO_ROOT / "app" / "agents" / "workshop-customizer.json").read_text(encoding="utf-8"))
    assert agent["name"] == "workshop-customizer" and agent["tools"] == [] and agent["allowedTools"] == []
    prompt = agent["prompt"]
    for needle in ("labs.teaching", "prompt_fixable", "'absent' retrieval_gap", "candidateFix", "teaching_design",
                   "knowledge-base language", "mustMentionAnyOf"):
        assert needle in prompt, needle


def test_skill_documents_the_teaching_gate():
    skill = (REPO_ROOT / "app" / "skills" / "workshop-customization" / "SKILL.md").read_text(encoding="utf-8")
    assert "## 3b. Teaching skeleton (`labs.teaching`)" in skill
    assert "| Teaching skeleton (`labs.teaching`" in skill and "| `Validate` | build |" in skill
    for code in ("teaching.missing", "teaching.probe_tools", "teaching.baseline_no_retrieval_hint", "tools.retrieval_marker_collision",
                 "tools.retrieval_count", "namespace.agent_name_too_long", "prose_names_holdout", "escalation_deferred"):
        assert code in skill, code


def test_guides_contract_names_every_guide_step_and_every_passthrough_field():
    """contracts/guides.md is the only place Kiro learns the labs.guide shape: its step ids and fields are
    exactly what the canonicalizer passes through (_pt_guide)."""
    gen = _routes()
    text = (gen.CONTRACTS_DIR / "guides.md").read_text(encoding="utf-8")
    ids = text[text.index("- Step ids:") + len("- Step ids:"):text.index("\n- Teaching prose")]
    assert {i.strip().rstrip(".") for i in ids.replace("\n", " ").split(",")} == set(gen.GUIDE_STEP_IDS)
    for name, _shape, limit in gen.GUIDE_FIELDS:
        assert name in text, name
        if name not in ("experiments", "stepNotes", "facilitatorNotes"):
            assert f"{name} (<={limit}" in text, name
    task, _shas = gen._compose_task(project_id="cold-chain-test", display_name="X", pack_kind="reference", customer="",
                                    brief="A cold-chain incident assistant for warehouse operators.", mode="regenerate",
                                    scoped=True, repair={"allowedScopes": ["guides"]})
    assert "GUIDE NARRATIVE" in task and "labs.guide=" in task
