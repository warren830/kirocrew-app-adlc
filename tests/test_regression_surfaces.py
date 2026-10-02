"""Regression + security gates: evaluator surfaces survive rendering; no credential/PII residue anywhere."""

from __future__ import annotations

import difflib
import json
import shutil
from pathlib import Path

import pytest

from workshop_customizer import compiler, render, validator
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = REPO_ROOT / "upstream"
TEMPLATE_COMMIT = json.loads((REPO_ROOT / "template-lock.json").read_text())["template"]["commit"]

EVALUATOR_SURFACES = ["07-setup-eval-env.sh", "08-create-evaluators.sh", "09-run-eval.sh", "13-judge-stability.sh", "evaluators/README.md"]


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    base = tmp_path_factory.mktemp("reg")
    scenario = load_scenario(REPO_ROOT / "scenarios" / "it-helpdesk" / "scenario.yaml")
    pack = compiler.compile_pack(scenario, base / "build", template_commit=TEMPLATE_COMMIT)
    return render.render_release(pack, UPSTREAM, base / "release", template_commit=TEMPLATE_COMMIT)


def test_release_preserves_every_evaluator_surface(rendered):
    rel = rendered.release_dir
    for surface in EVALUATOR_SURFACES:
        assert (rel / surface).is_file(), surface
    upstream_eval = {p.relative_to(UPSTREAM).as_posix() for p in (UPSTREAM / "evaluators").rglob("*") if p.is_file()}
    release_eval = {p.relative_to(rel).as_posix() for p in (rel / "evaluators").rglob("*") if p.is_file()}
    assert upstream_eval == release_eval, "prompt/tool customisation must never add or drop evaluator files"
    create = (rel / "08-create-evaluators.sh").read_text()
    run = (rel / "09-run-eval.sh").read_text()
    for token in ("thelma_rag_quality", "thelma_eval"):
        assert token in create, token
    assert "thelma" in run.lower()
    # The evaluator implementation is byte-identical to upstream. Exceptions: the namespace-bearing
    # adapters (token-rendered by design) and exactly one scenario-patched file, the Mind the Goal
    # prompt (SPEC D5), whose change is confined to its five anchors (test below).
    assert render.SCENARIO_PATCHED_EVALUATOR_FILES == (MTG_PROMPTS,)
    for py in (UPSTREAM / "evaluators").rglob("*.py"):
        rel_py = py.relative_to(UPSTREAM)
        if rel_py.as_posix() in render.SCENARIO_PATCHED_EVALUATOR_FILES:
            continue
        if "hr" in py.read_text().lower():
            continue  # namespace-bearing adapters are token-rendered by design
        assert (rel / rel_py).read_bytes() == py.read_bytes(), rel_py
    # Evaluator ids are unchanged (the add-ons stack, 09 and 13 look them up by these names).
    for token in ('register_evaluator "thelma_rag_quality"', 'register_evaluator "mtg_goal_success"'):
        assert token in create, token
    assert 'get_ev_arn "thelma_rag_quality"' in run and 'get_ev_arn "mtg_goal_success"' in run


#: The five upstream lines render replaces in the Mind the Goal prompt, and nothing else (SPEC D5).
MTG_ANCHOR_LINES = (
    'EVALUATION_SYSTEM_PROMPT = "You are a helpful AI assistant. You will act as a judge to evaluate quality of employee experience chatbot."',
    "You are provided with a dialog from an employee chatbot.",
    "  E2 Refusal to Answer - unwarranted refusal",
    "  E6 Incorrect Routing - wrong domain/department",
    "        lines.append(f\"  Employee: {t['user_message']}\")",
)
MTG_PROMPTS = "evaluators/mtg_eval/evaluators/mind_the_goal/prompts.py"


def test_mind_the_goal_prompt_change_is_confined_to_its_five_anchors(rendered):
    upstream = (UPSTREAM / MTG_PROMPTS).read_text(encoding="utf-8").splitlines()
    release = (rendered.release_dir / MTG_PROMPTS).read_text(encoding="utf-8").splitlines()
    diff = list(difflib.ndiff(upstream, release))
    removed = [line[2:] for line in diff if line.startswith("- ")]
    added = [line[2:] for line in diff if line.startswith("+ ")]
    assert removed == list(MTG_ANCHOR_LINES)
    assert len(added) == 6  # the system prompt line becomes two (EVALUATION_SYSTEM_PROMPT + USER_ROLE_LABEL)
    assert added[0].startswith("EVALUATION_SYSTEM_PROMPT = ") and added[1].startswith("USER_ROLE_LABEL = ")
    kept = [line[2:] for line in diff if line.startswith("  ")]
    assert kept == [line for line in upstream if line not in MTG_ANCHOR_LINES]  # every other line, in order
    compile("\n".join(release), MTG_PROMPTS, "exec")
    patches = {(p.file, p.name) for p in rendered.patches if p.file == MTG_PROMPTS}
    assert patches == {(MTG_PROMPTS, name) for name in (
        "mtg-judge-scenario-policy", "mtg-dialog-source", "mtg-rcof-e2-policy", "mtg-rcof-e6-policy", "mtg-user-label")}


def test_rendered_release_has_no_secret_residue(rendered):
    report = validator.check_secret_residue(rendered.release_dir, exclude=[render.MANIFEST_NAME])
    assert report.ok, report.describe()


def test_secret_residue_gate_blocks_a_render(rendered, tmp_path):
    poisoned = tmp_path / "poisoned"
    shutil.copytree(REPO_ROOT / "scenarios" / "it-helpdesk", poisoned)
    doc = next((poisoned / "knowledge-base" / "docs").glob("*.md"))
    fake_access_key = "AKIA" + "IOSFODNN7EXAMPLE"
    doc.write_text(doc.read_text() + f"\n\nInternal note: aws_access_key_id = {fake_access_key}\n")
    scenario = load_scenario(poisoned / "scenario.yaml")
    pack = compiler.compile_pack(scenario, tmp_path / "build", template_commit=TEMPLATE_COMMIT)
    with pytest.raises(validator.PackValidationError) as excinfo:
        render.render_release(pack, UPSTREAM, tmp_path / "release", template_commit=TEMPLATE_COMMIT)
    assert any(f.code == "secret-residue/aws-access-key-id" for f in excinfo.value.report.errors)


def test_secret_patterns_catch_shapes_and_ignore_placeholders(tmp_path):
    fake_secret_key = "wJalrXUtnFEMI/" + "K7MDENG/bPxRfiCY" + "EXAMPLEKEY"
    private_key_header = "-" * 5 + "BEGIN " + "RSA PRIVATE KEY" + "-" * 5
    (tmp_path / "a.txt").write_text(
        f"aws_secret_access_key = {fake_secret_key}\n"
        f"{private_key_header}\n"
        "身份证 11010519491231002X\n"
        "ssn 123-45-6789\n"
    )
    (tmp_path / "b.txt").write_text("profile: align-workshop\nexpectedAccountId: 123456789012\nemployee-001 asked about VPN\n{{arg:employee_id}}\n")
    report = validator.check_secret_residue(tmp_path)
    codes = sorted(f.code for f in report.findings)
    assert codes == ["secret-residue/aws-secret-access-key", "secret-residue/cn-national-id", "secret-residue/private-key", "secret-residue/us-ssn"]
    assert all(f.path == "a.txt" for f in report.findings)
    (tmp_path / "c.txt").write_text("员工身份证号11010519491231002X请核实\n社保号123-45-6789已登记\n", encoding="utf-8")
    cjk = [(f.code, f.line) for f in validator.check_secret_residue(tmp_path).findings if f.path == "c.txt"]
    assert cjk == [("secret-residue/cn-national-id", 1), ("secret-residue/us-ssn", 2)]  # no space around the ID


def test_repository_sources_and_packs_are_clean():
    """The engine, the app, the packs and the sync material never carry credential or PII shapes."""
    for sub in ("engine", "app", "scenarios", "sync", "tests"):
        report = validator.check_secret_residue(REPO_ROOT / sub, exclude=["test_regression_surfaces.py"] if sub == "tests" else ())
        assert report.ok, f"{sub}: {report.describe()}"
