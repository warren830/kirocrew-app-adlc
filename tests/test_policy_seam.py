"""P0b: the validation seam — Finding.scopes, one composed validate_scenario_policy(data, root) with
registered sub-checks, and the scenario root passed by every production caller."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from workshop_customizer import cli, compiler, validator
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKS = ("hr-default", "it-helpdesk", "maintenance")
TEMPLATE_COMMIT = json.loads((REPO_ROOT / "template-lock.json").read_text())["template"]["commit"]


def _pack_path(pack: str) -> Path:
    return REPO_ROOT / "scenarios" / pack / "scenario.yaml"


class Recorder:
    """A policy sub-check that records the root it was given."""

    def __init__(self, findings=()):
        self.roots: list[Path | None] = []
        self.findings = list(findings)

    def __call__(self, data, root):
        self.roots.append(root)
        return validator.ValidationReport(findings=list(self.findings))


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(validator, "POLICY_CHECKS", validator.POLICY_CHECKS + (validator.PolicySubCheck("recorder", rec, ("labs",)),))
    return rec


# ---------------------------------------------------------------------------
# Finding.scopes
# ---------------------------------------------------------------------------


def test_finding_scopes_is_a_trailing_optional_field():
    legacy = validator.Finding("error", "x.code", "message", "a/b.md", 3)
    assert legacy.scopes == () and legacy.describe() == "ERROR x.code: message [a/b.md:3]"
    scoped = validator.Finding("warning", "x.code", "message", scopes=("golden",))
    assert asdict(scoped) == {"severity": "warning", "code": "x.code", "message": "message", "path": None, "line": None, "scopes": ("golden",)}
    assert validator.SCOPES == ("agent", "facts", "knowledge", "tools", "skills", "prompts", "golden", "labs", "guides")


@pytest.mark.parametrize(
    "path, scopes",
    [
        ("knowledge-base/docs/vpn.md", ("knowledge",)),
        ("pack/knowledge-base/docs/vpn.md", ("knowledge",)),
        ("knowledge-base/create_kb.py", ()),  # upstream code in a release
        ("prompts/baseline.md", ("prompts",)),
        ("pack/prompts/optimization-candidate.md", ("prompts",)),
        ("skills/ticket-triage/SKILL.md", ("skills",)),
        ("labs/student-guide.md", validator.GUIDE_CONTENT_SCOPES),  # built from every content section
        ("pack/labs/student-guide.md", validator.GUIDE_CONTENT_SCOPES),
        ("README.md", validator.GUIDE_CONTENT_SCOPES),  # the release copy of the student guide
        ("golden/practice.json", ("golden",)),
        ("pack/tools/fixtures.json", ("tools",)),
        ("pack.json", ("golden", "labs")),
        ("pack/pack.env", ()),
        ("09-run-eval.sh", ()),
        ("RELEASE.json", ()),
        (None, ()),
    ],
)
def test_scopes_for_generated_paths(path, scopes):
    assert validator.scopes_for_path(path) == scopes
    assert set(scopes) <= set(validator.SCOPES)


def test_golden_and_gateway_findings_carry_scopes():
    data = yaml.safe_load(_pack_path("it-helpdesk").read_text(encoding="utf-8"))
    data["evaluation"]["goldenSet"] = data["evaluation"]["goldenSet"][:4]
    data["tools"][-1]["inputSchema"]["properties"]["bad"] = None
    report = validator.validate_scenario_policy(data)
    codes = {f.code: f.scopes for f in report.errors}
    assert codes["golden.too_few"] == ("golden",) and codes["golden.category_floor"] == ("golden",)
    assert codes["tools.gateway_schema"] == ("tools",)


def test_output_findings_are_scoped_by_path(tmp_path: Path):
    hr = load_scenario(_pack_path("hr-default"))
    pack = compiler.compile_pack(hr, tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    holdout = next(c for c in hr.golden_set if c["set"] == "holdout")
    doc = sorted((pack.pack_dir / "knowledge-base" / "docs").glob("*.md"))[0]
    doc.write_text(doc.read_text(encoding="utf-8") + "\n" + holdout["query"] + "\n", encoding="utf-8")
    (pack.pack_dir / "prompts" / "baseline.md").write_text("aws_secret_access_key = " + "A" * 40 + "\n", encoding="utf-8")
    leaked = validator.check_holdout_isolation(hr.data, pack.pack_dir).errors
    assert leaked and all(f.scopes == ("knowledge",) for f in leaked if f.path.startswith("knowledge-base/"))
    secrets = validator.check_secret_residue(tmp_path / "out")
    assert [f.scopes for f in secrets.errors if f.path == "pack/prompts/baseline.md"] == [("prompts",)]
    data = json.loads(json.dumps(hr.data))
    data["id"], data["namespace"]["agentName"] = "it-helpdesk", "itassistant"
    (tmp_path / "rel").mkdir()
    (tmp_path / "rel" / "04-deploy.sh").write_text("npx agentcore create --name hrassistant\n", encoding="utf-8")
    assert [f.scopes for f in validator.check_namespace_residue(data, tmp_path / "rel").errors] == [()]
    prose_root = tmp_path / "prose" / "knowledge-base" / "docs"
    prose_root.mkdir(parents=True)
    (prose_root / "x.md").write_text("our HR policy says\n", encoding="utf-8")
    assert [f.scopes for f in validator.check_prose_residue(data, tmp_path / "prose").warnings] == [("knowledge",)]


def test_generated_guide_findings_belong_to_the_section_that_produced_them(tmp_path: Path):
    """The student guide is generated from every section: a holdout leak that came from a fact is a
    facts repair, one from the narrative a guides repair; template problems have no scope (engine)."""
    from workshop_customizer import guide

    hr = load_scenario(_pack_path("hr-default"))
    holdout = next(c for c in hr.golden_set if c["set"] == "holdout")
    data = json.loads(json.dumps(hr.data))
    data["facts"].append({"id": "extra-fact", "statement": "FAQ: " + holdout["query"], "criticality": "advisory",
                          "provenance": "sa_synthetic"})
    scenario_path = tmp_path / "scenario.yaml"
    shutil.copytree(hr.root, tmp_path, dirs_exist_ok=True)
    scenario_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    leaky = load_scenario(scenario_path)
    with pytest.raises(validator.PackValidationError) as excinfo:
        compiler.compile_pack(leaky, tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    guide_findings = [f for f in excinfo.value.report.errors if validator.is_generated_guide(f.path)]
    assert {f.code for f in guide_findings} == {"holdout.leaked", "guide.holdout_leak"}
    assert all(f.scopes == ("facts",) for f in guide_findings), guide_findings

    assert validator.guide_source_scopes(data, tmp_path, lambda t: holdout["query"] in t) == ("facts",)
    data["labs"]["guide"] = {"id": "guide-narrative", "provenance": "sa_synthetic", "tagline": holdout["query"]}
    assert validator.guide_source_scopes(data, tmp_path, lambda t: holdout["query"] in t) == ("facts", "guides")
    assert validator.guide_source_scopes(data, tmp_path, lambda t: "zz-nowhere" in t) == ()

    guides = guide.build_guides(hr, template_commit=TEMPLATE_COMMIT, generator_version="test")
    broken = guide.Guides(language=guides.language, student="no marker\n", instructor=guides.instructor,
                          student_sources=guides.student_sources, instructor_sources=guides.instructor_sources)
    engine_only = guide.check_guides(hr.data, broken, root=hr.root).errors
    assert {f.code for f in engine_only} == {"guide.command_parity", "guide.marker"}
    assert all(f.scopes == () for f in engine_only)  # a template or engine problem: no repair can fix it


def test_strict_prose_keeps_scopes_when_promoting_warnings(tmp_path: Path):
    root = tmp_path / "it"
    shutil.copytree(_pack_path("it-helpdesk").parent, root)
    doc = root / "knowledge-base" / "docs" / "vpn_access.md"
    doc.write_text(doc.read_text(encoding="utf-8") + "\nSee the HR policy for leave.\n", encoding="utf-8")
    scenario = load_scenario(root / "scenario.yaml")
    with pytest.raises(validator.PackValidationError) as excinfo:
        compiler.compile_pack(scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT, strict_prose=True)
    prose = [f for f in excinfo.value.report.findings if f.code == "residue.prose" and f.path == "knowledge-base/docs/vpn_access.md"]
    assert prose and all(f.severity == "error" and f.scopes == ("knowledge",) for f in prose)


# ---------------------------------------------------------------------------
# The composed entry point
# ---------------------------------------------------------------------------


def test_policy_sub_checks_are_registered_in_order():
    assert [c.name for c in validator.POLICY_CHECKS] == ["golden", "namespace", "gateway", "knowledge", "teaching", "l1", "guide"]
    for sub_check in validator.POLICY_CHECKS:
        assert sub_check.default_scopes and set(sub_check.default_scopes) <= set(validator.SCOPES)


@pytest.mark.parametrize("pack", PACKS)
def test_reference_packs_raise_no_l1_or_guide_findings_and_pass_the_teaching_policy(pack):
    path = _pack_path(pack)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "teaching" in data["labs"]
    for name in ("l1", "guide"):
        sub_check = next(c for c in validator.POLICY_CHECKS if c.name == name)
        assert sub_check.check(data, path.parent).findings == []
    report = validator.validate_scenario_policy(data, root=path.parent)
    assert report.ok and not any(f.code.startswith(("l1.", "guide.")) for f in report.findings)
    assert all(f.scopes for f in report.findings)


@pytest.mark.parametrize("pack", PACKS)
def test_teaching_is_mandatory(pack):
    data = yaml.safe_load(_pack_path(pack).read_text(encoding="utf-8"))
    del data["labs"]["teaching"]
    report = validator.validate_scenario_policy(data, root=_pack_path(pack).parent)
    assert [(f.severity, f.code) for f in report.errors] == [("error", "teaching.missing")]
    assert report.errors[0].scopes == ("golden", "knowledge", "prompts", "labs")


def test_findings_default_to_their_sub_check_scopes(monkeypatch):
    unscoped = validator.Finding("warning", "x.unscoped", "no scopes given")
    scoped = validator.Finding("warning", "x.scoped", "explicit scopes", scopes=("facts",))
    rec = Recorder([unscoped, scoped])
    monkeypatch.setattr(validator, "POLICY_CHECKS", (validator.PolicySubCheck("x", rec, ("labs", "guides")),))
    report = validator.validate_scenario_policy({}, root="/tmp/somewhere")
    assert [(f.code, f.scopes) for f in report.findings] == [("x.unscoped", ("labs", "guides")), ("x.scoped", ("facts",))]
    assert rec.roots == [Path("/tmp/somewhere")]


def test_compile_pack_passes_the_scenario_root(recorder, tmp_path: Path):
    scenario = load_scenario(_pack_path("hr-default"))
    compiler.compile_pack(scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    assert recorder.roots == [scenario.root]


def test_cli_validate_passes_the_scenario_root(recorder, capsys):
    assert cli.main(["validate", str(_pack_path("maintenance")), "--json"]) == 0
    capsys.readouterr()
    assert recorder.roots == [_pack_path("maintenance").parent]


def test_policy_without_root_still_works_for_callers_that_have_no_files(recorder):
    data = yaml.safe_load(_pack_path("hr-default").read_text(encoding="utf-8"))
    assert validator.validate_scenario_policy(data).ok
    assert recorder.roots == [None]
