"""Tests for the pack compiler and pack-level policy validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workshop_customizer import compiler, validator
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
HR_SCENARIO = REPO_ROOT / "scenarios" / "hr-default" / "scenario.yaml"
TEMPLATE_COMMIT = json.loads((REPO_ROOT / "template-lock.json").read_text())["template"]["commit"]


@pytest.fixture(scope="module")
def hr_scenario():
    return load_scenario(HR_SCENARIO)


def test_hr_default_meets_golden_policy(hr_scenario):
    report = validator.validate_scenario_policy(hr_scenario.data)
    assert report.ok, report.describe()


def test_golden_policy_rejects_small_or_unbalanced_sets(hr_scenario):
    data = json.loads(json.dumps(hr_scenario.data))
    data["evaluation"]["goldenSet"] = data["evaluation"]["goldenSet"][:3]
    codes = {f.code for f in validator.check_golden_set(data).errors}
    assert "golden.too_few" in codes
    assert "golden.category_floor" in codes

    data = json.loads(json.dumps(hr_scenario.data))
    for case in data["evaluation"]["goldenSet"]:
        case["set"] = "practice"
    codes = {f.code for f in validator.check_golden_set(data).errors}
    assert "golden.holdout_ratio" in codes


def test_compile_hr_default_produces_pack_and_instructor_trees(tmp_path: Path, hr_scenario):
    result = compiler.compile_pack(hr_scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    pack = result.pack_dir
    assert (pack / "pack.env").is_file()
    assert (pack / "tools" / "schema.json").is_file()
    assert (pack / "tools" / "fixtures.json").is_file()
    assert (pack / "golden" / "practice.json").is_file()
    assert (pack / "prompts" / "baseline.md").is_file()
    assert (pack / "skills" / "leave-calculator" / "SKILL.md").is_file()
    assert len(list((pack / "knowledge-base" / "docs").glob("*.md"))) == 11
    assert (result.instructor_dir / "golden" / "holdout.json").is_file()
    assert (result.instructor_dir / "provenance-report.md").is_file()

    practice = json.loads((pack / "golden" / "practice.json").read_text())
    holdout = json.loads((result.instructor_dir / "golden" / "holdout.json").read_text())
    assert len(practice) == 6 and len(holdout) == 10
    assert {c["set"] for c in practice} == {"practice"}

    env = (pack / "pack.env").read_text()
    assert "WS_AGENT_NAME=hrassistant" in env
    assert "WS_RETRIEVAL_TOOL=retrieve_hr_policy" in env
    assert f"WS_TEMPLATE_COMMIT={TEMPLATE_COMMIT}" in env

    schema = json.loads((pack / "tools" / "schema.json").read_text())
    assert [t["name"] for t in schema] == [
        "retrieve_hr_policy",
        "check_leave_balance",
        "submit_leave_request",
        "query_salary_info",
    ]
    fixtures = json.loads((pack / "tools" / "fixtures.json").read_text())
    assert fixtures["kbIdSsmParameter"] == "/app/hr/knowledge_base_id"
    assert fixtures["tools"]["check_leave_balance"]["kind"] == "mock"
    assert "fixtures" not in fixtures["tools"]["retrieve_hr_policy"]


def test_compiled_pack_matches_upstream_tool_schema(tmp_path: Path, hr_scenario):
    result = compiler.compile_pack(hr_scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    generated = json.loads((result.pack_dir / "tools" / "schema.json").read_text())
    upstream = json.loads((REPO_ROOT / "upstream" / "gateway" / "hr-tools-schema.json").read_text())
    assert generated == upstream


def test_holdout_never_enters_student_pack(tmp_path: Path, hr_scenario):
    result = compiler.compile_pack(hr_scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    report = validator.check_holdout_isolation(hr_scenario.data, result.pack_dir)
    assert report.ok, report.describe()
    holdout_queries = [c["query"] for c in hr_scenario.golden_set if c["set"] == "holdout"]
    all_text = "\n".join(p.read_text() for p in result.pack_dir.rglob("*") if p.is_file())
    assert not any(q in all_text for q in holdout_queries)


def test_holdout_leak_is_detected(tmp_path: Path, hr_scenario):
    result = compiler.compile_pack(hr_scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    leaked = next(c for c in hr_scenario.golden_set if c["set"] == "holdout")
    (result.pack_dir / "prompts" / "baseline.md").write_text("cheat sheet: " + leaked["query"] + "\n")
    report = validator.check_holdout_isolation(hr_scenario.data, result.pack_dir)
    assert [f.code for f in report.errors] == ["holdout.leaked"]


def test_compile_is_deterministic(tmp_path: Path, hr_scenario):
    first = compiler.compile_pack(hr_scenario, tmp_path / "a", template_commit=TEMPLATE_COMMIT)
    second = compiler.compile_pack(hr_scenario, tmp_path / "b", template_commit=TEMPLATE_COMMIT)
    assert first.files == second.files
    assert first.derivation == second.derivation
    assert (tmp_path / "a" / "checksums.json").read_text() == (tmp_path / "b" / "checksums.json").read_text()


def test_every_output_has_a_derivation(tmp_path: Path, hr_scenario):
    result = compiler.compile_pack(hr_scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    assert set(result.files) == set(result.derivation)
    assert all(result.derivation[rel] for rel in result.files)


def test_residue_tokens_are_empty_for_hr_default(hr_scenario):
    assert validator.residue_tokens(hr_scenario.namespace, "retrieve_hr_policy") == []


def test_residue_scan_finds_upstream_identifiers_for_other_namespace(tmp_path: Path, hr_scenario):
    data = json.loads(json.dumps(hr_scenario.data))
    data["id"] = "it-helpdesk"
    data["namespace"].update(
        {
            "agentName": "itassistant",
            "toolTargetName": "it-tools",
            "gatewayName": "itgateway",
            "knowledgeBaseName": "it-knowledge-base",
            "ssmParameterPrefix": "/app/it",
            "lambdaFunctionName": "it-tools-handler",
        }
    )
    data["evaluation"]["retrievalToolName"] = "retrieve_it_policy"
    release = tmp_path / "release"
    (release / "scripts").mkdir(parents=True)
    (release / "scripts" / "04-deploy.sh").write_text("npx agentcore create --name hrassistant\njq '.allowedTools = [\"@hr-tools/*\"]'\n")
    (release / "scripts" / "clean.sh").write_text("echo itassistant\n")
    report = validator.check_namespace_residue(data, release)
    hits = {(f.path, f.line) for f in report.errors}
    assert ("scripts/04-deploy.sh", 1) in hits and ("scripts/04-deploy.sh", 2) in hits
    assert not any(f.path == "scripts/clean.sh" for f in report.errors)
    tokens = validator.residue_tokens(data["namespace"], data["evaluation"]["retrievalToolName"])
    assert "hr-tools-handler" in tokens and tokens.index("hr-tools-handler") < tokens.index("hr-tools")


def test_prose_residue_is_advisory_only(tmp_path: Path, hr_scenario):
    data = json.loads(json.dumps(hr_scenario.data))
    data["id"] = "it-helpdesk"
    root = tmp_path / "pack"
    root.mkdir()
    (root / "prompt.md").write_text("You are an HR policy assistant\n")
    report = validator.check_prose_residue(data, root)
    assert report.ok and [f.code for f in report.warnings] == ["residue.prose"]
    assert validator.check_prose_residue(hr_scenario.data, root).findings == []


def test_every_built_skill_starts_with_the_frontmatter_the_harness_needs(tmp_path):
    """Live 2026-10-01: "failed to load skill: SKILL.md must start with --- frontmatter delimiter" for every
    Kiro-generated pack; the build adds name + description when the file has none and keeps one that exists."""
    import shutil

    from workshop_customizer.compiler import skill_markdown

    built = skill_markdown("pm-planner", "# Skill: PM planner\n\nPlans preventive maintenance by line.\nUse it weekly.\n\n## Steps\n")
    assert built.startswith('---\nname: pm-planner\ndescription: "Plans preventive maintenance by line. Use it weekly."\n---\n# Skill')
    written = "---\nname: x\ndescription: d\n---\n# T\n"
    assert skill_markdown("x", written) == written
    root = tmp_path / "maintenance"
    shutil.copytree(REPO_ROOT / "scenarios" / "maintenance", root)
    pack = compiler.compile_pack(load_scenario(root / "scenario.yaml"), tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    for path in sorted((pack.pack_dir / "skills").glob("*/SKILL.md")):
        head = path.read_text(encoding="utf-8").split("---\n")
        assert head[0] == "" and f"name: {path.parent.name}\n" in head[1] and "description: " in head[1], path
