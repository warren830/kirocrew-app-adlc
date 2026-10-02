"""The generated Workshop Guide (SPEC D10, designs[1]): goldens, parity, isolation, residue, wording.

Golden files live in tests/golden/guides/<pack>/<audience>.md; ``WSC_UPDATE_GOLDENS=1`` rewrites them
(review the diff: every reference-pack or script-fact change moves them).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
import yaml

from workshop_customizer import __version__, compiler, guide, render, script_facts, teaching
from workshop_customizer import guide_strings as gs
from workshop_customizer.scenario import CrossReferenceError, ProvenanceGateError, SchemaViolation, load_scenario
from workshop_customizer.validator import (
    HR_TOPIC_PATTERNS,
    PROSE_RESIDUE_WORDS,
    UPSTREAM_TOKENS,
    PackValidationError,
    validate_scenario_policy,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = REPO_ROOT / "upstream"
TEMPLATE_COMMIT = json.loads((REPO_ROOT / "template-lock.json").read_text())["template"]["commit"]
PACKS = ("hr-default", "it-helpdesk", "maintenance")
GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "guides"
UPDATE = os.environ.get("WSC_UPDATE_GOLDENS") == "1"

_BUILT: dict[str, tuple] = {}


def _scenario(pack: str):
    return load_scenario(REPO_ROOT / "scenarios" / pack / "scenario.yaml")


def _guides(scenario, **kw):
    return guide.build_guides(scenario, template_commit=TEMPLATE_COMMIT, generator_version=__version__, **kw)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Compile + render a reference pack once per module: (scenario, compiled pack, rendered release)."""

    def get(pack: str):
        if pack not in _BUILT:
            base = tmp_path_factory.mktemp(f"guide-{pack}")
            scenario = _scenario(pack)
            compiled = compiler.compile_pack(scenario, base / "out", template_commit=TEMPLATE_COMMIT)
            release = render.render_release(compiled, UPSTREAM, base / "out" / "release", template_commit=TEMPLATE_COMMIT)
            _BUILT[pack] = (scenario, compiled, release)
        return _BUILT[pack]

    yield get
    _BUILT.clear()


def _copy_pack(tmp_path: Path, pack: str = "it-helpdesk") -> tuple[Path, dict]:
    root = tmp_path / pack
    shutil.copytree(REPO_ROOT / "scenarios" / pack, root)
    return root, yaml.safe_load((root / "scenario.yaml").read_text(encoding="utf-8"))


def _write(root: Path, data: dict) -> Path:
    path = root / "scenario.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Upstream basis and command parity
# ---------------------------------------------------------------------------


def test_upstream_guide_basis_pinned():
    for name, digest in guide.UPSTREAM_GUIDE_SHA256.items():
        assert hashlib.sha256((UPSTREAM / name).read_bytes()).hexdigest() == digest, f"{name} moved: review the guide templates"


def test_upstream_commands_constant():
    en = guide.extract_bash_commands((UPSTREAM / "README.md").read_text(encoding="utf-8"))
    zh = guide.extract_bash_commands((UPSTREAM / "README.zh-CN.md").read_text(encoding="utf-8"))
    assert en == zh == list(guide.UPSTREAM_GUIDE_COMMANDS)
    assert len(guide.UPSTREAM_GUIDE_COMMANDS) == 28


@pytest.mark.parametrize("pack", PACKS)
def test_student_guide_command_parity_and_entry_points(pack, built):
    _scenario_, compiled, release = built(pack)
    student = (compiled.pack_dir / "labs" / "student-guide.md").read_text(encoding="utf-8")
    instructor = (compiled.instructor_dir / "instructor-guide.md").read_text(encoding="utf-8")
    assert guide.extract_bash_commands(student) == list(guide.UPSTREAM_GUIDE_COMMANDS)
    assert guide.extract_bash_commands(instructor) == []
    assert "```bash" not in instructor
    for command in guide.UPSTREAM_GUIDE_COMMANDS:
        match = re.match(r"^\./(\d\d-[a-z-]+\.sh)\b", command)
        if match:
            for where in (release.release_dir, release.release_dir / "static" / "scripts"):
                script = where / match.group(1)
                assert script.is_file() and os.access(script, os.X_OK), script


# ---------------------------------------------------------------------------
# Goldens, determinism, language, template structure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pack", PACKS)
@pytest.mark.parametrize("audience", guide.AUDIENCES)
def test_guide_golden_files(pack, audience, built):
    _scenario_, compiled, _release = built(pack)
    rel = guide.STUDENT_GUIDE_PATH if audience == "student" else guide.INSTRUCTOR_GUIDE_PATH
    actual = (compiled.out_dir / rel).read_text(encoding="utf-8")
    scenario = _scenario(pack)
    warnings = validate_scenario_policy(scenario.data, root=scenario.root).warnings
    assert actual == getattr(_guides(scenario, warnings=warnings), audience)  # build_guides is what compile writes
    path = GOLDEN_DIR / pack / f"{audience}.md"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
    assert actual == path.read_text(encoding="utf-8"), f"{pack} {audience} guide changed (WSC_UPDATE_GOLDENS=1 after review)"


@pytest.mark.parametrize("pack", PACKS)
def test_guide_determinism(pack, tmp_path):
    scenario = _scenario(pack)
    first, second = _guides(scenario), _guides(scenario)
    assert (first.student, first.instructor) == (second.student, second.instructor)
    source = scenario.path.read_text(encoding="utf-8")
    for text in (first.student, first.instructor):
        # no timestamps, except ones the pack data itself carries (tool fixtures)
        assert all(stamp in source for stamp in re.findall(r"\d{4}-\d\d-\d\dT\d\d:\d\d", text))
        assert text.endswith("\n") and not text.endswith("\n\n") and "\n\n\n" not in text
        assert not any(line != line.rstrip() for line in text.splitlines())


def test_changing_a_holdout_query_moves_only_the_instructor_guide(tmp_path):
    root, data = _copy_pack(tmp_path)
    before = _guides(load_scenario(_write(root, data)))
    holdout = next(c for c in data["evaluation"]["goldenSet"] if c["set"] == "holdout")
    holdout["query"] = "How do I rotate my VPN certificate on a managed laptop?"
    after = _guides(load_scenario(_write(root, data)))
    assert after.student == before.student
    assert after.instructor != before.instructor and "rotate my VPN certificate" in after.instructor


@pytest.mark.parametrize("pack, lang", [("hr-default", "zh-CN"), ("it-helpdesk", "en"), ("maintenance", "en")])
def test_guide_language_follows_the_pack(pack, lang):
    scenario = _scenario(pack)
    g = _guides(scenario)
    assert g.language == lang and g.student.splitlines()[0] == f"<!-- workshop-customizer:student-guide pack={pack} lang={lang} -->"
    if lang == "zh-CN":
        assert "逐步说明" in g.student and "讲师指南" in g.instructor
        return
    assert "Step-by-step" in g.student and "Instructor guide" in g.instructor
    pack_text = guide.pack_sources_text(scenario)
    allowed = list(gs.PRINTED_CJK) + guide._CJK.findall(pack_text)
    for text in (g.student, g.instructor):
        assert guide.cjk_outside(text, allowed) == []


def _template_headings(text: str) -> list[int]:
    return [level for level, _t in guide.headings(text)]


@pytest.mark.parametrize("audience", guide.AUDIENCES)
def test_template_structure_parity(audience):
    en, zh = guide.load_template(audience, "en"), guide.load_template(audience, "zh-CN")
    assert guide.placeholders(en) == guide.placeholders(zh)
    assert _template_headings(en) == _template_headings(zh)
    assert en.splitlines()[0] == zh.splitlines()[0] == "{{marker}}"
    assert guide.extract_bash_commands(en) == guide.extract_bash_commands(zh)


def test_render_template_is_strict_and_single_pass():
    assert guide.render_template("a {{x}} b", {"x": "{{y}}"}) == "a {{y}} b"  # inserted values are never re-scanned
    with pytest.raises(guide.GuideError, match="needs values for: x"):
        guide.render_template("{{x}}", {})
    with pytest.raises(guide.GuideError, match="not used by the template: y"):
        guide.render_template("{{x}}", {"x": "1", "y": "2"})
    assert guide.render_template("{{arg:x}} {{Upper}}", {}) == "{{arg:x}} {{Upper}}"  # not placeholder syntax


def test_pack_placeholder_syntax_is_emitted_literally():
    instructor = _guides(_scenario("it-helpdesk")).instructor
    assert '"requester_id":"{{arg:requester_id}}"' in instructor  # a fixture template string, verbatim


def test_pack_text_that_looks_like_navigation_is_emitted_literally(tmp_path):
    """The TOC and anchors come from the template's headings; pack prose is never re-scanned for them."""
    root, data = _copy_pack(tmp_path)
    data["knowledge"]["documents"][0]["title"] = "Password policy (see @@ANCHOR:42@@)"
    fact = next(f for f in data["facts"] if f["id"] == "priority-sla")
    fact["statement"] = fact["statement"] + " See @@TOC@@ here."
    data["labs"]["guide"]["memoryLesson"] = "Look @@ANCHOR:99@@ and @@ANCHOR:6@@."
    scenario = load_scenario(_write(root, data))
    compiled = compiler.compile_pack(scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT)  # no GuideError
    student = (compiled.pack_dir / "labs" / "student-guide.md").read_text(encoding="utf-8")
    instructor = (compiled.instructor_dir / "instructor-guide.md").read_text(encoding="utf-8")
    assert "Password policy (see @@ANCHOR:42@@)" in student and "See @@TOC@@ here." in student
    assert "Look @@ANCHOR:99@@ and @@ANCHOR:6@@." in student and "Look @@ANCHOR:99@@ and @@ANCHOR:6@@." in instructor
    assert student.count("1. [The scenario](#1-the-scenario)") == 1
    assert "(see [Prerequisites](#6-prerequisites))" in student  # the template's own links still resolve
    with pytest.raises(guide.GuideError, match="links section 42"):
        guide.navigation("## 1. One\n", {"x": 42})
    with pytest.raises(guide.GuideError, match="may not contain a placeholder"):
        guide.navigation("## 1. {{title}}\n", {})


def test_normalize_and_slugs():
    assert guide.normalize_markdown("a  \r\n\r\n\r\n\nb") == "a\n\nb\n"
    assert guide.github_slug("11. Cleanup — `99-cleanup.sh`") == "11-cleanup--99-cleanupsh"
    assert guide.github_slug("1. 这套东西在搭什么，为什么这么搭") == "1-这套东西在搭什么为什么这么搭"
    assert guide.github_slug("4. The eval-first loop (ADLC)") == "4-the-eval-first-loop-adlc"


# ---------------------------------------------------------------------------
# Release: README.md, links, instructor isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pack", PACKS)
def test_readme_is_the_generated_student_guide(pack, built):
    _scenario_, compiled, release = built(pack)
    root = release.release_dir
    readme = (root / "README.md").read_bytes()
    assert readme == (root / guide.STUDENT_GUIDE_PATH).read_bytes() == (compiled.pack_dir / "labs" / "student-guide.md").read_bytes()
    assert readme.decode("utf-8").splitlines()[0].startswith(f"<!-- {guide.STUDENT_MARKER_TOKEN} pack={pack} ")
    assert readme not in ((UPSTREAM / "README.md").read_bytes(), (UPSTREAM / "README.zh-CN.md").read_bytes())
    manifest = render.verify_release(root)
    assert "README.md" in manifest["files"]
    for absent in ("assets", "README.zh-CN.md", "CONTRIBUTING.md", "instructor"):
        assert not (root / absent).exists(), absent


@pytest.mark.parametrize("pack", PACKS)
def test_markdown_links_resolve(pack, built):
    _scenario_, _compiled, release = built(pack)
    text = (release.release_dir / "README.md").read_text(encoding="utf-8")
    slugs = {guide.github_slug(t) for _level, t in guide.headings(text)}
    targets = re.findall(r"\]\(([^)\s]+)\)", text)
    assert targets
    for target in targets:
        if target.startswith(("http://", "https://")):
            continue
        if target.startswith("#"):
            assert target[1:] in slugs, target
        else:
            assert (release.release_dir / target).exists(), target
    toc = re.findall(r"^\d+\. \[[^\]]+\]\(#([^)]+)\)$", text, re.M)
    assert len(toc) == 12 and set(toc) <= slugs


@pytest.mark.parametrize("pack", PACKS)
def test_instructor_marker_never_in_a_release(pack, built):
    _scenario_, _compiled, release = built(pack)
    for path in release.release_dir.rglob("*"):
        if path.is_file():
            assert path.name != "instructor-guide.md"
            assert guide.INSTRUCTOR_MARKER_TOKEN.encode() not in path.read_bytes(), path
    assert guide.check_instructor_isolation(release.release_dir).findings == []


def test_instructor_isolation_gate(tmp_path):
    root = tmp_path / "release"
    (root / "pack" / "labs").mkdir(parents=True)
    (root / guide.STUDENT_GUIDE_PATH).write_text("student\n", encoding="utf-8")
    (root / "README.md").write_text("other\n", encoding="utf-8")
    (root / "notes.md").write_text(f"<!-- {guide.INSTRUCTOR_MARKER_TOKEN} pack=x -->\n", encoding="utf-8")
    (root / "instructor-guide.md").write_text("x\n", encoding="utf-8")
    codes = sorted((f.code, f.path) for f in guide.check_instructor_isolation(root).findings)
    assert codes == [("guide.instructor_marker_leak", "instructor-guide.md"), ("guide.instructor_marker_leak", "notes.md"),
                     ("guide.readme_mismatch", "README.md")]


def test_the_render_gate_refuses_an_instructor_marker_in_the_release(tmp_path, monkeypatch):
    scenario = _scenario("maintenance")
    compiled = compiler.compile_pack(scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    guide_path = compiled.pack_dir / "labs" / "student-guide.md"
    guide_path.write_text(guide_path.read_text(encoding="utf-8") + f"\n<!-- {guide.INSTRUCTOR_MARKER_TOKEN} -->\n", encoding="utf-8")
    with pytest.raises(PackValidationError, match="guide.instructor_marker_leak"):
        render.render_release(compiled, UPSTREAM, tmp_path / "release", template_commit=TEMPLATE_COMMIT)


# ---------------------------------------------------------------------------
# Canary: nothing instructor-only reaches the release; no practice answer reaches the guide (SPEC D10)
# ---------------------------------------------------------------------------


def test_guide_canary_isolation(tmp_path):
    root, data = _copy_pack(tmp_path)
    instructor_only: list[str] = []
    practice_expected: list[str] = []
    counter = iter(range(1000))

    def sentinel(bucket: list[str]) -> str:
        token = f"zqcanary{next(counter):03d}"
        bucket.append(token)
        return token

    cases = data["evaluation"]["goldenSet"]
    probes = set(teaching.probe_case_ids(data))
    holdout = [c for c in cases if c["set"] == "holdout"]
    holdout[0]["id"] = "canary-holdout-a"
    instructor_only.append("canary-holdout-a")
    for case in holdout:
        case["query"] = f"{case['query']} {sentinel(instructor_only)}"
        case["label"] = f"{case['label']} {sentinel(instructor_only)}"
        case["expected"].setdefault("mustMention", []).append(sentinel(instructor_only))
        case["expected"].setdefault("mustNotMention", []).append(sentinel(instructor_only))
    for case in cases:
        if case["set"] != "practice":
            continue
        if case["id"] not in probes:  # a probe's mustMention must stay in the knowledge base (teaching rules)
            case["expected"].setdefault("mustMention", []).append(sentinel(practice_expected))
        case["expected"].setdefault("mustNotMention", []).append(sentinel(practice_expected))
    for fact in data["facts"]:
        fact["source"] = sentinel(instructor_only)
    confirmed = next(f for f in data["facts"] if f["id"] == "priority-sla")
    confirmed.update(provenance="customer_confirmed", confirmedBy=sentinel(instructor_only),
                     confirmedAt="2026-09-01T00:00:00Z", confirmationRef=sentinel(instructor_only))
    data["customer"] = {"name": "Northwind Robotics", "materialApproval": sentinel(instructor_only), "dataClassification": "synthetic"}
    # Who confirmed a practice case, and where, is instructor-only too (practice.json ships only what L1 needs).
    case_confirmation: list[str] = []
    practice_case = next(c for c in cases if c["set"] == "practice")
    practice_case.update(provenance="customer_confirmed", confirmedBy=sentinel(case_confirmation),
                         confirmedAt="2026-09-01T00:00:00Z", confirmationRef=sentinel(case_confirmation),
                         origin={"kind": "sa_authored", "note": sentinel(case_confirmation)})
    first = data["labs"]["teaching"]["firstConversation"]
    first["teachingPoint"] = sentinel(instructor_only)
    first["mustNotMention"] = [sentinel(instructor_only)]
    gap = next(p for p in data["labs"]["teaching"]["phenomena"] if p["kind"] == "retrieval_gap")
    gap["absentTerms"] = [*gap["absentTerms"], sentinel(instructor_only)]
    narrative = data["labs"]["guide"]
    narrative["facilitatorNotes"] = {"baseline": sentinel(instructor_only)}
    narrative["designRationale"] = sentinel(instructor_only)
    narrative["experiments"] = [*narrative["experiments"], {"audience": "instructor", "title": "Holdout drill", "body": sentinel(instructor_only)}]
    observation = sentinel(practice_expected)  # labs.observations ship in pack.json, never in the guide
    data["labs"]["observations"] = [*data["labs"]["observations"], f"Observation {observation}."]
    supplement_token = sentinel(instructor_only)
    (root / "guides").mkdir()
    (root / "guides" / "facilitator.md").write_text(f"# Facilitator\n\nRemember {supplement_token}.\n", encoding="utf-8")
    data["labs"]["instructorGuideFile"] = "guides/facilitator.md"

    scenario = load_scenario(_write(root, data))
    compiled = compiler.compile_pack(scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    release = render.render_release(compiled, UPSTREAM, tmp_path / "release", template_commit=TEMPLATE_COMMIT)

    release_text = {p.relative_to(release.release_dir).as_posix(): p.read_text(encoding="utf-8", errors="replace")
                    for p in release.release_dir.rglob("*") if p.is_file()}
    for token in instructor_only + case_confirmation:
        hits = [rel for rel, text in release_text.items() if token in text]
        assert hits == [], (token, hits)
    all_cases = (compiled.instructor_dir / "golden" / "all-cases.json").read_text(encoding="utf-8")
    assert all(t in all_cases for t in case_confirmation)  # positive control: the instructor bundle keeps them
    student = release_text["README.md"]
    for token in practice_expected:
        assert token not in student and token not in release_text[guide.STUDENT_GUIDE_PATH], token
    # They legitimately ship: practice expected.* in pack/golden/practice.json (L1), observations in pack.json.
    assert all(t in release_text["pack/golden/practice.json"] for t in practice_expected if t != observation)
    assert observation in release_text["pack/pack.json"]
    # Positive control: the instructor guide carries every instructor-only and practice answer sentinel.
    instructor = (compiled.instructor_dir / "instructor-guide.md").read_text(encoding="utf-8")
    missing = [t for t in instructor_only + practice_expected if t not in instructor]
    snapshot = (compiled.instructor_dir / "scenario-snapshot.yaml").read_text(encoding="utf-8")
    assert missing == [], [(t, snapshot[max(0, snapshot.find(t) - 120):snapshot.find(t) + 20]) for t in missing]


# ---------------------------------------------------------------------------
# Residue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pack", ("it-helpdesk", "maintenance"))
def test_guide_residue_non_hr(pack, built):
    scenario, compiled, _release = built(pack)
    sources = guide.pack_sources_text(scenario)
    upstream_ids = [tok for tok, _key in UPSTREAM_TOKENS]
    for rel in (guide.STUDENT_GUIDE_PATH, guide.INSTRUCTOR_GUIDE_PATH):
        text = (compiled.out_dir / rel).read_text(encoding="utf-8")
        for pattern in HR_TOPIC_PATTERNS:
            if re.search(pattern, text):
                assert re.search(pattern, sources), (rel, pattern)
        assert [tok for tok in upstream_ids if tok in text] == []
        assert [w for w in PROSE_RESIDUE_WORDS if w.lower() in text.lower()] == []
    if pack == "maintenance":
        assert not re.search(r"(?i)employee", (compiled.pack_dir / "labs" / "student-guide.md").read_text(encoding="utf-8"))


def test_guide_residue_is_template_free():
    texts = [p.read_text(encoding="utf-8") for p in sorted(guide.TEMPLATE_DIR.glob("*.md"))]
    texts.append((Path(gs.__file__)).read_text(encoding="utf-8"))
    assert len(texts) == 5
    for text in texts:
        assert [p for p in HR_TOPIC_PATTERNS if re.search(p, text)] == []
        assert [tok for tok, _key in UPSTREAM_TOKENS if tok in text] == []
        assert [w for w in PROSE_RESIDUE_WORDS if w.lower() in text.lower()] == []


def test_guide_residue_detects_a_template_leak(monkeypatch, tmp_path):
    strings = copy.deepcopy(gs.STRINGS)
    strings["en"]["banner.reference"] = str(strings["en"]["banner.reference"]) + " Ask about sick leave."
    strings["zh-CN"]["banner.reference"] = str(strings["zh-CN"]["banner.reference"]) + " Ask about sick leave."
    monkeypatch.setattr(gs, "STRINGS", strings)
    with pytest.raises(PackValidationError, match=r"guide.residue: template domain word 'sick leave'"):
        compiler.compile_pack(_scenario("maintenance"), tmp_path / "m", template_commit=TEMPLATE_COMMIT)
    compiler.compile_pack(_scenario("hr-default"), tmp_path / "h", template_commit=TEMPLATE_COMMIT)  # the HR pack is exempt


def test_student_strings_have_the_same_keys_in_both_languages():
    assert set(gs.STRINGS["en"]) == set(gs.STRINGS["zh-CN"])
    for key, value in gs.STRINGS["en"].items():
        other = gs.STRINGS["zh-CN"][key]
        assert type(value) is type(other), key
        if isinstance(value, tuple):
            assert len(value) == len(other), key
        if isinstance(value, str):
            assert set(re.findall(r"\{(\w+)\}", value)) == set(re.findall(r"\{(\w+)\}", str(other))), key


# ---------------------------------------------------------------------------
# Content: scenario, provenance, contrast, expectations, scored-by
# ---------------------------------------------------------------------------


def test_student_view_is_a_projection_without_answers():
    names = {f.name for f in guide.StudentView.__dataclass_fields__.values()}
    assert not names & {"expected", "basis", "holdout", "observations", "confirmedBy", "confirmationRef", "materialApproval"}
    data = _scenario("it-helpdesk").data
    facts = script_facts.compute(data)
    view = guide.student_view(data, facts, guide.teaching_view(data))
    assert facts.first_conversation.must_not_mention and view.facts_script.first_conversation.must_not_mention == ()
    assert view.facts_script.first_conversation.teaching_point is None
    assert {c.case_id for c in view.cases} == {c["id"] for c in teaching.practice_cases(data)}
    assert "NWR-4471" not in repr(view.facts_script) and "canary" not in repr(view)


def _customer_copy(tmp_path: Path) -> SimpleNamespace:
    root, data = _copy_pack(tmp_path)
    data["packKind"] = "customer"
    data["customer"] = {"name": "Northwind Robotics", "materialApproval": "APPROVAL-7", "dataClassification": "internal"}
    for fid in ("priority-sla", "password-policy"):
        fact = next(f for f in data["facts"] if f["id"] == fid)
        fact.update(provenance="customer_confirmed", confirmedBy="Dana Ops", confirmedAt="2026-09-01T00:00:00Z", confirmationRef="MEET-42")
    scenario = load_scenario(_write(root, data), enforce_gate=False)
    return scenario


def test_provenance_tables_and_banners(tmp_path):
    scenario = _customer_copy(tmp_path)
    g = _guides(scenario)
    student = g.student
    customer_table = student.split("**Customer-confirmed facts**")[1].split("**Synthetic teaching settings**")[0]
    synthetic_table = student.split("**Synthetic teaching settings**")[1].split("In this scenario:")[0]
    assert "acknowledged in 15 minutes 24x7" in customer_table and "[Customer-confirmed]" in customer_table
    assert "[Synthetic teaching setting]" not in customer_table
    assert "company-managed, enrolled device" in synthetic_table and "[Customer-confirmed]" not in synthetic_table
    assert "Dana Ops" not in student and "MEET-42" not in student and "APPROVAL-7" not in student
    assert "Only statements tagged [Customer-confirmed] describe Northwind Robotics's actual rules" in student
    assert "Contains Northwind Robotics internal material" in student
    # A fact cited only by holdout cases stays out of the student guide, and is in the instructor truth sheet.
    assert "self-reset VPN credentials once every 24 hours" not in student
    assert "self-reset VPN credentials once every 24 hours" in g.instructor and "Dana Ops" in g.instructor
    assert "Everything in this scenario is synthetic teaching material" in _guides(_scenario("it-helpdesk")).student


def test_the_banner_counts_every_confirmed_item_in_both_guides(tmp_path):
    """A customer pack whose only confirmed items are a tool and a practice case gets banner V2 in both guides."""
    root, data = _copy_pack(tmp_path)
    data["packKind"] = "customer"
    data["customer"] = {"name": "Northwind Robotics", "materialApproval": "APPROVAL-7"}
    stamp = {"provenance": "customer_confirmed", "confirmedBy": "Dana Ops", "confirmedAt": "2026-09-01T00:00:00Z", "confirmationRef": "MEET-42"}
    assert not any(f.get("provenance") == "customer_confirmed" for f in data["facts"])
    next(t for t in data["tools"] if t.get("kind") != "retrieval").update(stamp)
    next(c for c in data["evaluation"]["goldenSet"] if c["set"] == "practice").update(stamp)
    g = _guides(load_scenario(_write(root, data), enforce_gate=False))
    v1 = "Everything in this scenario is synthetic teaching material"
    v2 = "Only statements tagged [Customer-confirmed] describe Northwind Robotics's actual rules"
    assert "[Customer-confirmed]" in g.student.split("## 2.")[1]  # the tools and practice tables tag them
    assert v1 not in g.student and v2 in g.student
    assert v1 not in g.instructor and v2 in g.instructor
    assert guide.any_customer_confirmed(data) and not guide.any_customer_confirmed(_scenario("it-helpdesk").data)


def test_simulated_confirmations_count_nowhere_in_the_guides(tmp_path):
    """The guides use the engine's rules (scenario.customer_anchors / is_customer_confirmed / item_class):
    a simulated confirmation is no anchor, no banner V2 and no [Customer-confirmed] tag."""
    root, data = _copy_pack(tmp_path)
    data["packKind"] = "workshop"
    case = next(c for c in data["evaluation"]["goldenSet"] if c["category"] == "normal" and c["set"] == "practice")
    simulated = {"provenance": "customer_confirmed", "confirmedBy": "SIMULATED reviewer (e2e_generate)",
                 "confirmedAt": "2026-09-28T00:00:00Z", "confirmationRef": "simulated"}
    case.update(simulated)
    for fact in data["facts"]:
        if fact["id"] in case["basis"]:
            fact.update(simulated)
    g = _guides(load_scenario(_write(root, data), enforce_gate=False))
    anchors = g.instructor.split("### Customer-confirmed anchors")[1].split("###")[0]
    assert f"`{case['id']}`" not in anchors and anchors.count("MISSING") == 3, anchors
    assert "Everything in this scenario is synthetic teaching material" in g.student
    assert not guide.any_customer_confirmed(data)
    assert "[Customer-confirmed]" not in g.student.split("## 2.")[1]
    assert f"{case['id']}` (practice, normal, [Draft — not for class])" in g.instructor
    # The truth sheet prints each fact's derived class (scenario.item_class_label) in the pack language.
    assert "| draft (not reviewed) |" in g.instructor and "| synthetic teaching setting |" in g.instructor
    zh = _guides(_scenario("hr-default")).instructor
    assert "| 合成教学设定 |" in zh or "| 合成教学设定 (" in zh


def test_contrast_is_worded_per_gap_mechanism():
    it = _guides(_scenario("it-helpdesk"))
    step13 = it.student.split("### Step 13")[1].split("## 9.")[0]
    assert "**P2 response target**" in step13 and "**VPN session limit (not in the knowledge base)**" in step13
    assert "**retrieval finds nothing**" in step13 and "**admits it and hands off**" in step13 and "stays Fail" not in step13
    data = _scenario("it-helpdesk").data
    for line in render.optimize_closing_lines(data, script_facts.compute(data)):
        assert line in step13  # the guide quotes 10's closing text verbatim
    hr = _guides(_scenario("hr-default"))
    step13 = hr.student.split("### 第 13 步")[1].split("## 9.")[0]
    # Next to the absent volunteer-days gap the buried sick-leave gap is advisory: nothing promises it stays Fail.
    assert "**病假 sick leave (retrieval-quality case)**（答案被埋没）：检索常常失败（SP2≈0）" in step13 and "仅供参考" in step13
    assert "仍然 Fail" not in step13 and "仍 Fail" not in step13
    assert "**志愿者假期 volunteer days (not in the knowledge base)**：**检索什么也找不到**" in step13
    assert "Bait terms (in noise documents): `VPN session`" in it.instructor
    assert "Absent terms (must not be in the knowledge base): `session timeout`" in it.instructor


def test_a_buried_gap_is_advisory_only_next_to_an_absent_gap():
    """SPEC D2 wording: a buried gap stays Fail; declared next to an absent gap it cannot decide the class, so every
    student-facing line, 10's closing text and the sign-off read it as advisory."""
    hr = _guides(_scenario("hr-default"))
    seen = hr.student.split("## 9. ")[1].split("## 10. ")[0]
    row = next(line for line in seen.splitlines() if line.startswith("| 检索缺口（答案被埋没） |"))
    assert "仅供参考" in row and "仍然 Fail" not in row
    [signoff] = [line for line in hr.instructor.splitlines() if line.startswith("- [ ] 检索缺口（")]
    assert signoff.startswith("- [ ] 检索缺口（`volunteer-days-gap`）：") and "仍然 Fail" not in signoff
    assert "被埋没的缺口（`sick-leave-certificate`）仅供参考" in signoff
    assert "仍然 Fail" not in hr.student and "仍 Fail" not in hr.student
    # The §4 ADLC tip promises only the absent gap: the buried one may pass after 10 (both live runs did).
    tip = next(line for line in hr.student.splitlines() if "改提示词应当提升" in line)
    assert "志愿者假期" in tip and "病假" not in tip

    data = copy.deepcopy(_scenario("hr-default").data)
    data["labs"]["teaching"]["phenomena"] = [p for p in data["labs"]["teaching"]["phenomena"] if p["id"] != "volunteer-days-gap"]
    alone = _guides(SimpleNamespace(data=data, root=REPO_ROOT / "scenarios" / "hr-default", path=REPO_ROOT / "scenarios" / "hr-default" / "scenario.yaml"))
    step13 = alone.student.split("### 第 13 步")[1].split("## 9.")[0]
    assert "**病假 sick leave (retrieval-quality case)**（SP2≈0，检索失败）**仍然 Fail**" in step13 and "仅供参考" not in step13
    assert any("病假 sick leave (retrieval-quality case) → 仍 Fail" in line for line in render.optimize_closing_lines(data, script_facts.compute(data)))
    [signoff] = [line for line in alone.instructor.splitlines() if line.startswith("- [ ] 检索缺口（")]
    assert signoff.startswith("- [ ] 检索缺口（`sick-leave-certificate`）：") and "被埋没的缺口在 10 之后仍然 Fail" in signoff


def test_undeclared_contrast_uses_generic_wording_and_warns():
    data = copy.deepcopy(_scenario("it-helpdesk").data)
    data["labs"]["teaching"]["phenomena"] = [p for p in data["labs"]["teaching"]["phenomena"] if p["kind"] != "retrieval_gap"]
    fake = SimpleNamespace(data=data, root=REPO_ROOT / "scenarios" / "it-helpdesk", path=REPO_ROOT / "scenarios" / "it-helpdesk" / "scenario.yaml")
    g = _guides(fake)
    assert [f.code for f in g.findings] == ["guide.contrast_undeclared"]
    assert "a question whose retrieval failed (SP2≈0) stays Fail" in g.student
    assert "guide.contrast_undeclared" in g.instructor  # listed under the validator warnings


def test_a_phenomenon_on_a_holdout_case_is_a_guide_error():
    data = copy.deepcopy(_scenario("it-helpdesk").data)
    data["labs"]["teaching"]["phenomena"][0]["caseIds"].append("vpn-self-reset")
    with pytest.raises(guide.GuideError, match="non-practice case"):
        guide.teaching_view(data)


def test_what_you_should_see_comes_from_labs_teaching_and_thresholds(monkeypatch):
    student = _guides(_scenario("maintenance")).student
    table = student.split("## 9. What you should see")[1].split("## 10.")[0]
    for kind in ("Memory (first conversation)", "Prompt-fixable", "Retrieval gap (answer not in the knowledge base)", "Noisy sources",
                 "Tool use", "Refusal"):
        assert f"| {kind} |" in table, kind
    assert "Filler lubrication interval and P1 response target for a stopped line" in table
    assert "GR below 0.7" in table and "SP2 ≤ 0.2 or SQC < 0.3" in table and "max(noise band, 0.05)" in table
    # The numbers are read from teaching.THRESHOLDS, never re-encoded in the guide.
    changed = MappingProxyType({**teaching.THRESHOLDS, "grPass": 0.8, "retrievalFailedSp2": 0.15})
    monkeypatch.setattr(teaching, "THRESHOLDS", changed)
    table = _guides(_scenario("maintenance")).student.split("## 9. What you should see")[1].split("## 10.")[0]
    assert "GR below 0.8" in table and "SP2 ≤ 0.15" in table


def test_facilitation_rows_state_the_bar_their_code_uses(monkeypatch):
    """PF_RETRIEVAL_FAILED fails the retrieval-ok bar; RG_RETRIEVAL_OK_* passes the retrieval-failed one (teaching.retrieval_failed)."""
    def rows(pack: str) -> dict[str, str]:
        text = _guides(_scenario(pack)).instructor
        return {line.split(" | ")[0].strip("| `"): line for line in text.splitlines()
                if line.startswith(("| `PF_RETRIEVAL_FAILED`", "| `RG_RETRIEVAL_OK_BASELINE"))}

    t = teaching.THRESHOLDS
    en, zh = rows("it-helpdesk"), rows("hr-default")
    assert f"max SQC < {t['retrievalOkSqc']:g}" in en["PF_RETRIEVAL_FAILED"] and f"最大 SQC < {t['retrievalOkSqc']:g}" in zh["PF_RETRIEVAL_FAILED"]
    gap = f"SP2 > {t['retrievalFailedSp2']:g}"
    assert f"{gap} and SQC ≥ {t['retrievalFailedSqc']:g}" in en["RG_RETRIEVAL_OK_BASELINE / RG_RETRIEVAL_OK_OPTIMIZED"]
    assert f"{gap} 且 SQC ≥ {t['retrievalFailedSqc']:g}" in zh["RG_RETRIEVAL_OK_BASELINE / RG_RETRIEVAL_OK_OPTIMIZED"]
    monkeypatch.setattr(teaching, "THRESHOLDS", MappingProxyType({**t, "retrievalFailedSqc": 0.25, "retrievalOkSqc": 0.6}))
    en = rows("it-helpdesk")
    assert "max SQC < 0.6" in en["PF_RETRIEVAL_FAILED"] and "SQC ≥ 0.25" in en["RG_RETRIEVAL_OK_BASELINE / RG_RETRIEVAL_OK_OPTIMIZED"]


def test_scored_by_column_follows_the_probe_flags():
    data = _scenario("it-helpdesk").data
    student = _guides(_scenario("it-helpdesk")).student
    facts = script_facts.compute(data)
    for case in facts.eval_cases:
        row = next(line for line in student.splitlines() if line.startswith(f"| {case.index + 1} | {case.label} |"))
        assert row.split(" | ")[5] == ("L1 + THELMA + Mind the Goal" if case.probe else "L1 + Mind the Goal"), row
    assert f"(default {facts.probe_count})" in student and f"`09-run-eval.sh --eval-only {facts.probe_count}`" in student


@pytest.mark.parametrize("pack", PACKS)
def test_guide_and_scripts_agree(pack, built):
    scenario, _compiled, release = built(pack)
    facts = script_facts.compute(scenario.data)
    readme = (release.release_dir / "README.md").read_text(encoding="utf-8")
    s06 = (release.release_dir / "06-test-conversation.sh").read_text(encoding="utf-8")
    s10 = (release.release_dir / "10-optimize-prompt.sh").read_text(encoding="utf-8")
    fc = facts.first_conversation
    for line in (script_facts.first_conversation_topic(fc), script_facts.memory_notice(fc)):
        assert line in readme and render.bash_quote(line) in s06
    for line in render.optimize_closing_lines(scenario.data, facts):
        assert line in readme and f"echo {render.bash_quote(line)}" in s10
    assert f"> {guide.md_inline(fc.query)}" in readme and f"actor-id {fc.actor_id}" in readme
    for case in facts.eval_cases:
        assert guide.md_inline(case.query, table=True) in readme


def test_prerequisites_state_the_prepared_environment():
    for pack, needle in (("it-helpdesk", "only in the Workshop environment your facilitator prepared"),
                         ("hr-default", "只能在讲师用 Workshop Customizer 为本场景准备好的 Workshop 环境里运行")):
        student = _guides(_scenario(pack)).student
        assert needle in student
        for requirement in ("workshop-customizer-addons", "SkillsFilesAccessPointArn", "/opt/workshop-customizer/runtime_permissions.py",
                            "~/workshop/current"):
            assert requirement in student, requirement


def test_instructor_guide_sections():
    g = _guides(_scenario("it-helpdesk"))
    text = g.instructor
    assert text.splitlines()[0] == "<!-- workshop-customizer:instructor-only pack=it-helpdesk -->"
    assert "**INSTRUCTOR ONLY**" in text
    for needle in ("### 4.2 Holdout cases", "#### VPN self-service reset — `vpn-self-reset` (holdout", "not executed (holdout)",
                   "`mustMention`: 24", "PF_BASELINE_ALREADY_PASSES", "RG_OPTIMIZED_RESOLVED", "JUDGE_TOO_NOISY",
                   "Ask the class why the VPN session question can pass GR", "```diff", guide.REHEARSAL_BEGIN,
                   "No rehearsal evidence is recorded for this build yet", "Retrieval-trace filter of 09/10/11/13: `execute_tool ittools___retrieve_it_policy`"):
        assert needle in text, needle
    sha = hashlib.sha256((REPO_ROOT / "scenarios" / "it-helpdesk" / "scenario.yaml").read_bytes()).hexdigest()
    assert f"`{sha}`" in text
    # designs[1] H §8: the cleanup section names the generic HR release as the class fallback (not in its own guide).
    cleanup = text.split("## 10. Cleanup, fallback and rollback")[1].split("## Appendix A.")[0]
    assert "teach the generic workshop (`hr-default`, the upstream-compatible reference pack) instead" in cleanup
    # ...but never as a sync into this environment: the add-ons are bound to one pack namespace.
    assert "only in an environment prepared for it" in cleanup and "cannot run in this scenario's environment" in cleanup
    assert "tools/deploy_addons.py --scenario scenarios/hr-default/scenario.yaml" in cleanup and "roll back to it" not in cleanup
    hr = _guides(_scenario("hr-default")).instructor.split("## 10. ")[1].split("## 附录 A.")[0]
    assert "hr-default" not in hr and "课堂备选方案" not in hr
    zh = copy.deepcopy(_scenario("it-helpdesk").data)
    zh["language"] = "zh-CN"
    fake = SimpleNamespace(data=zh, root=REPO_ROOT / "scenarios" / "it-helpdesk", path=REPO_ROOT / "scenarios" / "it-helpdesk" / "scenario.yaml")
    assert "就改讲通用 Workshop（`hr-default`，与上游兼容的参考场景包）——但只能在为它准备好的环境里讲" in _guides(fake).instructor


def test_hr_default_keeps_the_upstream_attribution():
    student = _guides(_scenario("hr-default")).student
    attribution = student.split("## 12. 数据来源与署名")[1]
    assert "HR-MultiWOZ" in attribution and "arXiv:2402.01018" in attribution and "Apache-2.0" in attribution


# ---------------------------------------------------------------------------
# Narrative rules and gate
# ---------------------------------------------------------------------------


def _narrative_findings(data: dict, root: Path | None = None) -> list[tuple[str, str]]:
    return sorted((f.code, f.path) for f in guide.check_guide_narrative(data, root).findings)


@pytest.mark.parametrize(
    "field, value, code",
    [
        ("memoryLesson", "Run this:\n```\nls\n```", "guide.narrative_command"),
        ("memoryLesson", "./09-run-eval.sh to see it", "guide.narrative_command"),
        ("retrievalContrast", "# A heading", "guide.narrative_heading"),
        ("tagline", "Try <script>alert(1)</script>", "guide.narrative_html"),
        ("scenarioIntro", "See [the docs](https://example.com).", "guide.narrative_link"),
        ("memoryLesson", "The hrassistant agent knows nothing yet.", "guide.narrative_identifier"),
        ("memoryLesson", f"x {guide.INSTRUCTOR_MARKER_TOKEN}", "guide.instructor_marker_leak"),
        ("memoryLesson", "Compare with the holdout set later.", "guide.holdout_leak"),
    ],
)
def test_narrative_rules(field, value, code):
    data = copy.deepcopy(_scenario("maintenance").data)
    data["labs"]["guide"][field] = value
    assert (code, f"labs.guide.{field}") in _narrative_findings(data)
    report = validate_scenario_policy(data)
    assert any(f.code == code and f.scopes == ("guides",) for f in report.errors)


def test_links_are_allowed_in_attribution_and_instructor_fields():
    data = copy.deepcopy(_scenario("maintenance").data)
    data["labs"]["guide"].update(attribution="Data: https://example.com/data", designRationale="See https://example.com/why",
                                 facilitatorNotes={"baseline": "Show [the chart](https://example.com/c)."})
    assert _narrative_findings(data) == []


def test_holdout_leak_via_narrative_or_supplement(tmp_path):
    root, data = _copy_pack(tmp_path)
    holdout = next(c for c in data["evaluation"]["goldenSet"] if c["id"] == "password-rules")
    for text in (holdout["query"], holdout["label"], "Ask the password-rules question too."):
        variant = copy.deepcopy(data)
        variant["labs"]["guide"]["memoryLesson"] = text
        assert ("guide.holdout_leak", "labs.guide.memoryLesson") in _narrative_findings(variant), text
        variant["labs"]["guide"].pop("memoryLesson")
        variant["labs"]["guide"]["facilitatorNotes"] = {"baseline": text}  # instructor fields may name holdout cases
        assert _narrative_findings(variant) == []
    (root / "guides").mkdir()
    (root / "guides" / "notes.md").write_text(f"# Notes\n\n{holdout['query']}\n", encoding="utf-8")
    data["labs"]["studentGuideFile"] = "guides/notes.md"
    assert ("guide.holdout_leak", "labs.studentGuideFile") in _narrative_findings(data, root)
    scenario = load_scenario(_write(root, data))
    with pytest.raises(PackValidationError, match="guide.holdout_leak"):
        compiler.compile_pack(scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT)


ESCAPED_HOLDOUT_QUERY = 'Can I get admin rights if my role is <contractor> and "urgent" | today?'


@pytest.mark.parametrize("surface", ["teachingPoint", "observations"])
def test_a_holdout_query_with_escaped_characters_is_caught_where_students_read_it(tmp_path, surface):
    """The guide writes ``<`` as ``&lt;`` and ``|`` as ``\\|``, pack.json writes ``"`` as ``\\"``: the isolation
    gates match what a student reads, so a holdout query copied into labs.teaching or labs.observations with
    those characters never ships (review finding: it used to build and leak into README.md and pack.json)."""
    root, data = _copy_pack(tmp_path)
    holdout = next(c for c in data["evaluation"]["goldenSet"] if c["id"] == "non-catalog-software")
    holdout["query"] = ESCAPED_HOLDOUT_QUERY
    if surface == "teachingPoint":
        phenomenon = next(p for p in data["labs"]["teaching"]["phenomena"] if p["id"] == "disable-mfa-refusal")
        phenomenon["teachingPoint"] = ESCAPED_HOLDOUT_QUERY
    else:
        data["labs"]["observations"].append(ESCAPED_HOLDOUT_QUERY)
    with pytest.raises(PackValidationError) as excinfo:
        compiler.compile_pack(load_scenario(_write(root, data)), tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    leaked = {(f.code, f.path) for f in excinfo.value.report.errors if "holdout" in f.code}
    assert ("holdout.leaked", "pack.json") in leaked, excinfo.value.report.describe()
    if surface == "teachingPoint":  # the "what you should see" table of the student guide carries it too
        assert ("holdout.leaked", "labs/student-guide.md") in leaked
        assert any(code == "guide.holdout_leak" for code, _path in leaked)


def test_reader_views_decode_guide_json_and_script_escapes(tmp_path):
    from workshop_customizer import validator

    query = validator._normalize(ESCAPED_HOLDOUT_QUERY)
    md = "| Refusal | " + guide.md_inline(ESCAPED_HOLDOUT_QUERY, table=True) + " |\n"
    assert query not in validator._normalize(md) and validator.reader_contains(md, query, rel="README.md")
    as_json = json.dumps({"teachingPoint": ESCAPED_HOLDOUT_QUERY, "line": "a\nb"}, ensure_ascii=True)
    assert query not in validator._normalize(as_json) and validator.reader_contains(as_json, query, rel="pack.json")
    assert validator.reader_contains("echo " + render.bash_quote(ESCAPED_HOLDOUT_QUERY), query, rel="10-optimize.sh")
    assert not validator.reader_contains(json.dumps(["Can I get admin", "rights"]), "can i get admin rights", rel="x.json")
    data = {"evaluation": {"goldenSet": [{"id": "admin-rights", "set": "holdout", "query": ESCAPED_HOLDOUT_QUERY}]}}
    (tmp_path / "README.md").write_text(md, encoding="utf-8")
    (tmp_path / "pack.json").write_text(as_json, encoding="utf-8")
    report = validator.check_holdout_isolation(data, tmp_path)
    assert sorted((f.code, f.path) for f in report.errors) == [("holdout.leaked", "README.md"), ("holdout.leaked", "pack.json")]
    assert guide.holdout_leaks(data, "intro\n" + md) == [(2, "holdout case 'admin-rights' query")]


def test_supplements_are_embedded_with_demoted_headings(tmp_path):
    root, data = _copy_pack(tmp_path)
    (root / "guides").mkdir()
    (root / "guides" / "student.md").write_text("# Local tips\n\nBring your badge.\n\n## Parking\n\nLevel 2.\n", encoding="utf-8")
    (root / "guides" / "instructor.md").write_text("# Room setup\n\nTwo projectors.\n", encoding="utf-8")
    data["labs"].update(studentGuideFile="guides/student.md", instructorGuideFile="guides/instructor.md")
    scenario = load_scenario(_write(root, data))
    compiled = compiler.compile_pack(scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    student = (compiled.pack_dir / "labs" / "student-guide.md").read_text(encoding="utf-8")
    assert "## Scenario notes\n\n#### Local tips\n\nBring your badge.\n\n##### Parking" in student
    instructor = (compiled.instructor_dir / "instructor-guide.md").read_text(encoding="utf-8")
    assert "#### Room setup" in instructor and "Two projectors." not in student
    assert set(compiled.files) >= {guide.STUDENT_GUIDE_PATH, guide.INSTRUCTOR_GUIDE_PATH}
    assert "labs.studentGuideFile" in compiled.derivation[guide.STUDENT_GUIDE_PATH]
    assert guide.demote_headings("```\n# not a heading\n```\n# h") == "```\n# not a heading\n```\n#### h"


def test_the_instructor_supplement_is_never_a_student_facing_file(tmp_path):
    """labs.instructorGuideFile is instructor-only: the same file (any spelling) as a knowledge document, a
    skill, a prompt or the student supplement is a cross-reference error, and a compound holdout id in any
    student-facing file fails the output gate (D10 canary)."""
    root, data = _copy_pack(tmp_path)
    (root / "guides").mkdir()
    (root / "guides" / "facilitator.md").write_text("# Facilitator\n\nKeep the answer key closed.\n", encoding="utf-8")
    data["labs"]["guide"] = {"id": "guide-narrative", "provenance": "sa_synthetic", "tagline": "IT help desk, taught with its own questions."}
    data["labs"]["instructorGuideFile"] = "guides/facilitator.md"
    variants = {
        "document": lambda d: d["knowledge"]["documents"].append(
            {"id": "facilitator-notes", "title": "Facilitator notes", "file": "./guides/Facilitator.md", "provenance": "sa_synthetic"}),
        "skill": lambda d: d["skills"][0].update(file="guides/facilitator.md"),
        "prompt": lambda d: d["prompts"].update(baselineFile="guides/facilitator.md"),
        "student supplement": lambda d: d["labs"].update(studentGuideFile="guides/facilitator.md"),
    }
    for name, change in variants.items():
        variant = copy.deepcopy(data)
        change(variant)
        with pytest.raises(CrossReferenceError, match="labs.instructorGuideFile is instructor-only"):
            load_scenario(_write(root, variant))
    load_scenario(_write(root, data))  # its own file is fine

    holdout = next(c for c in data["evaluation"]["goldenSet"] if c["set"] == "holdout" and "-" in c["id"])
    doc = root / data["knowledge"]["documents"][0]["file"]
    doc.write_text(doc.read_text(encoding="utf-8") + f"\nAfter class, ask {holdout['id']} by hand.\n", encoding="utf-8")
    with pytest.raises(PackValidationError) as excinfo:
        compiler.compile_pack(load_scenario(_write(root, data)), tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    leaked = [f for f in excinfo.value.report.errors if f.code == "holdout.id_leaked"]
    assert [(f.path, f.scopes) for f in leaked] == [(f"knowledge-base/docs/{doc.name}", ("knowledge",))]


def test_non_utf8_pack_files_are_findings_not_crashes(tmp_path):
    root, data = _copy_pack(tmp_path)
    candidate = root / data["prompts"]["optimizationCandidateFile"]
    candidate.write_bytes(candidate.read_bytes() + "\nSee the café.\n".encode("latin-1"))
    scenario = load_scenario(_write(root, data))
    assert validate_scenario_policy(scenario.data, root=scenario.root).ok
    compiled = compiler.compile_pack(scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT)  # prompts were never required to be UTF-8
    instructor = (compiled.instructor_dir / "instructor-guide.md").read_text(encoding="utf-8")
    assert "+See the caf�." in instructor  # the prompt diff shows the undecodable byte as U+FFFD
    (root / "guides").mkdir()
    (root / "guides" / "student.md").write_bytes("Bring your badge to the café.\n".encode("latin-1"))
    (root / "guides" / "instructor.md").write_bytes("Two projectors, one café.\n".encode("latin-1"))
    data["labs"].update(studentGuideFile="guides/student.md", instructorGuideFile="guides/instructor.md")
    scenario = load_scenario(_write(root, data))
    assert ("guide.supplement_encoding", "labs.studentGuideFile") in _narrative_findings(scenario.data, scenario.root)
    assert ("guide.supplement_encoding", "labs.instructorGuideFile") in _narrative_findings(scenario.data, scenario.root)
    report = validate_scenario_policy(scenario.data, root=scenario.root)  # validate reports it instead of raising
    assert any(f.code == "guide.supplement_encoding" and f.scopes == ("guides",) for f in report.errors)
    assert "Bring your badge to the caf�." in _guides(scenario).student  # a draft preview still renders
    with pytest.raises(PackValidationError, match="guide.supplement_encoding"):
        compiler.compile_pack(scenario, tmp_path / "out2", template_commit=TEMPLATE_COMMIT)


def test_the_instructor_supplement_may_not_carry_bash_blocks(tmp_path):
    root, data = _copy_pack(tmp_path)
    (root / "guides").mkdir()
    (root / "guides" / "instructor.md").write_text("Reset with:\n\n```bash\n./99-cleanup.sh\n```\n", encoding="utf-8")
    data["labs"]["instructorGuideFile"] = "guides/instructor.md"
    assert ("guide.narrative_command", "labs.instructorGuideFile") in _narrative_findings(data, root)


def test_narrative_gate_and_cross_references(tmp_path):
    root, data = _copy_pack(tmp_path)
    data["labs"]["guide"]["provenance"] = "ai_draft"
    with pytest.raises(ProvenanceGateError, match="guide-narrative"):
        load_scenario(_write(root, data))
    data["labs"]["guide"]["provenance"] = "customer_confirmed"
    with pytest.raises(SchemaViolation):
        load_scenario(_write(root, data))
    data["labs"]["guide"]["provenance"] = "sa_synthetic"
    data["packKind"] = "customer"
    scenario = load_scenario(_write(root, data), enforce_gate=False)
    assert "guide-narrative" not in {v.item_id for v in scenario.gate_violations}  # sa_synthetic passes in every kind
    data["packKind"] = "reference"
    guide_block = data["labs"].pop("guide")
    (root / "guides").mkdir()
    (root / "guides" / "notes.md").write_text("Notes.\n", encoding="utf-8")
    data["labs"]["studentGuideFile"] = "guides/notes.md"
    with pytest.raises(CrossReferenceError, match="guide supplements require labs.guide review"):
        load_scenario(_write(root, data))
    data["labs"]["guide"] = guide_block
    assert load_scenario(_write(root, data)).data["labs"]["studentGuideFile"] == "guides/notes.md"


def test_reference_packs_pass_the_guide_policy():
    for pack in PACKS:
        scenario = _scenario(pack)
        assert guide.check_guide_narrative(scenario.data, scenario.root).findings == []
        assert scenario.data["labs"]["guide"]["provenance"] == "sa_synthetic"


# ---------------------------------------------------------------------------
# Rehearsal evidence and preview
# ---------------------------------------------------------------------------


def test_rehearsal_evidence_is_rendered_defensively():
    assert "No rehearsal evidence" in guide.render_rehearsal_evidence(None, "en")
    assert "No rehearsal evidence" in guide.render_rehearsal_evidence(["not", "a", "dict"], "en")  # type: ignore[arg-type]
    record = {
        "verdict": "ready", "readyForClass": True, "releaseVersion": "it-helpdesk-aaa", "generatedAt": "2026-09-20T10:00:00Z",
        "phenomena": [
            {"id": "prompt-fix", "kind": "prompt_fixable", "verdict": "reproduced", "evidence": "GR 0.4 -> 0.8 | <b>bold</b> `x`"},
            {"id": "gap", "kind": "retrieval_gap", "status": "reproduced", "cases": [{"caseId": "c1", "code": "RG_REPRODUCED"}]},
            "junk", {"id": "x" * 500, "evidence": {"nested": [1, 2, 3]}},
        ],
        "remediation": [{"action": "Rebuild", "because": "stale"}],
    }
    text = guide.render_rehearsal_evidence(record, "en", release_version="it-helpdesk-bbb")
    assert "Verdict: **ready**" in text and "Ready for class: **yes**" in text and "it-helpdesk-aaa" in text
    assert "not the release of this guide" in text  # bound to another release
    assert "&lt;b&gt;bold&lt;/b&gt;" in text and "\\|" in text and "<b>" not in text
    assert "c1: RG_REPRODUCED" in text and "Rebuild (stale)" in text
    assert max(len(line) for line in text.splitlines()) < 1200
    zh = guide.render_rehearsal_evidence({"verdict": "not_ready", "phenomena": []}, "zh-CN")
    assert "结论：**not_ready**" in zh and "彩排记录里没有任何教学现象" in zh


@pytest.mark.parametrize("junk", [
    {"phenomena": 5}, {"phenomena": True}, {"phenomena": "text"}, {"remediation": 3}, {"remediation": {"a": 1}},
    {"phenomena": [{"id": "p", "cases": 7}]}, {"readiness": 5}, {"readiness": {"blockers": 3}}, {"inputs": [1]},
    {"inputs": {"noiseBand": [0.1], "missingEvidence": "optimize: l1"}}, {"verdict": {"x": {1, 2}}},
])
def test_rehearsal_evidence_ignores_values_of_the_wrong_type(junk):
    text = guide.render_rehearsal_evidence(junk, "en")  # never a TypeError: a hand-edited record is data, not code
    assert text.startswith("- Verdict: **")
    assert guide.attach_rehearsal(_guides(_scenario("it-helpdesk")).instructor, junk, "en")


def test_rehearsal_evidence_says_why_a_rehearsal_is_not_ready():
    """A record shaped like P2's run/rehearsal.json: the reason, the blockers and the inputs are shown."""
    record = {
        "schema": "workshop-customizer/rehearsal/v1", "generatedAt": "2026-09-20T10:00:00Z", "releaseVersion": "it-helpdesk-aaa",
        "inputs": {"scenario": "build snapshot", "releaseVerified": True, "missingEvidence": ["optimize: l1", "judge-stability: thelma"],
                   "noiseBand": {"value": 0.31, "source": "judge-stability", "stabilityVerdict": "unstable"}},
        "verdict": "not_ready", "reasonCode": "PF_NOT_REPRODUCED", "reason": "no prompt_fixable phenomenon reproduced",
        "readyForClass": False,
        "readiness": {"verdict": "not_ready", "reportComplete": False, "guidesBuilt": True,
                      "blockers": ["VERDICT_NOT_READY", "REPORT_INCOMPLETE"]},
        "phenomena": [{"id": "prompt-fix", "kind": "prompt_fixable", "verdict": "not_reproduced", "reasonCode": "PF_NO_IMPROVEMENT",
                       "cases": [{"caseId": "p2-response", "status": "not_reproduced", "code": "PF_NO_IMPROVEMENT", "reason": "delta 0.02"}]}],
        "remediation": [{"id": "h01", "code": "PF_NO_IMPROVEMENT", "severity": "blocking", "action": "Strengthen the candidate prompt",
                         "because": "delta 0.02 is inside the noise band"}],
    }
    text = guide.render_rehearsal_evidence(record, "en", release_version="it-helpdesk-aaa")
    for needle in ("Verdict: **not_ready**", "Reason: `PF_NOT_REPRODUCED` — no prompt_fixable phenomenon reproduced",
                   "Ready for class: **no**", "Readiness blockers: `VERDICT_NOT_READY`, `REPORT_INCOMPLETE`",
                   "Guided Run report complete: **no**", "Both guides built: **yes**",
                   "Judge noise band: 0.31 (source `judge-stability`, stability verdict `unstable`)",
                   "Missing evidence: `optimize: l1`, `judge-stability: thelma`", "p2-response: PF_NO_IMPROVEMENT",
                   "Strengthen the candidate prompt (delta 0.02 is inside the noise band)"):
        assert needle in text, needle
    assert "not the release of this guide" not in text
    zh = guide.render_rehearsal_evidence(record, "zh-CN")
    for needle in ("原因：`PF_NOT_REPRODUCED`——no prompt_fixable", "就绪阻塞项：`VERDICT_NOT_READY`、`REPORT_INCOMPLETE`",
                   "Guided Run 报告完整：**否**", "裁判噪声带：0.31（来源 `judge-stability`、稳定性结论 `unstable`）",
                   "缺失的证据：`optimize: l1`、`judge-stability: thelma`"):
        assert needle in zh, needle
    assert not re.search(r"[㐀-鿿]\)|[㐀-鿿`], ", zh)  # Chinese punctuation around the Chinese text
    assert "Judge noise band: 0.2" in guide.render_rehearsal_evidence({"inputs": {"noiseBand": 0.2}}, "en")  # a bare number


def test_rehearsal_evidence_reads_the_guide_language():
    """rehearsal.py writes each text in the pack language with an English twin (<field>En): a Chinese guide reads
    the text, an English guide the twin; a record without twins (older engine) shows its text in both."""
    record = {
        "language": "zh-CN", "verdict": "not_ready", "reasonCode": "PHENOMENON_NOT_REPRODUCED",
        "reason": "最近一次完整运行里，有决定性教学对比没复现", "reasonEn": "a decisive teaching contrast did not reproduce in the latest complete run",
        "readiness": {"blockers": ["VERDICT_NOT_READY"],
                      "blockerTexts": [{"code": "VERDICT_NOT_READY", "text": "有决定性教学对比没复现", "textEn": "a decisive teaching contrast did not reproduce"}]},
        "phenomena": [{"id": "gap", "kind": "retrieval_gap", "verdict": "not_reproduced", "reasonCode": "RG_RETRIEVAL_OK_BASELINE",
                       "cases": [{"caseId": "c1", "code": "RG_RETRIEVAL_OK_BASELINE", "reason": "基线轮检索找到了答案", "reasonEn": "retrieval found the answer"}]}],
        "warnings": [{"code": "PF_OPTIMIZED_NOT_PASS", "message": "优化后的回答有提升，但 THELMA 仍判为 Fail",
                      "messageEn": "the optimized answer improved but THELMA still labels it Fail", "refs": ["prompt-fix", "c2"]},
                     {"code": "PHENOMENON_MIXED", "message": "gap 在 2 次完整运行中复现了 1 次；课前再跑一次",
                      "messageEn": "gap reproduced in 1 of 2 complete runs; run again before class", "refs": ["gap"]}],
        "remediation": [{"id": "h01", "code": "RG_RETRIEVAL_OK_BASELINE", "severity": "blocking", "caseId": "c1",
                         "asset": {"kind": "kb_doc", "file": "knowledge-base/docs/faq.md", "scenarioPath": "knowledge.documents[id=faq]"},
                         "action": "把答案从知识库里删掉。", "actionEn": "Remove the answer from the knowledge base.",
                         "because": "基线轮检索找到了答案", "becauseEn": "retrieval found the answer"},
                        {"id": "h02", "code": "MEMORY_UNCHECKED", "severity": "advisory",
                         "asset": {"kind": "teaching", "file": "scenario.yaml", "scenarioPath": "labs.teaching.firstConversation.mustNotMention"},
                         "action": "声明 firstConversation.mustNotMention。", "actionEn": "Declare firstConversation.mustNotMention.",
                         "because": "", "becauseEn": ""}],
        "scopeWarning": "这不是生产上线审批。", "scopeWarningEn": "Not a production approval.",
    }
    zh = guide.render_rehearsal_evidence(record, "zh-CN")
    for needle in ("原因：`PHENOMENON_NOT_REPRODUCED`——最近一次完整运行里，有决定性教学对比没复现",
                   "就绪阻塞项：`VERDICT_NOT_READY`（有决定性教学对比没复现）",
                   "c1：RG_RETRIEVAL_OK_BASELINE——基线轮检索找到了答案",
                   # A warning names the phenomenon and case it is about, unless its message already does.
                   "提醒：\n\n- `prompt-fix` · `c2`：优化后的回答有提升，但 THELMA 仍判为 Fail\n- gap 在 2 次完整运行中复现了 1 次；课前再跑一次\n",
                   "- **阻断** `knowledge-base/docs/faq.md` · `c1`：把答案从知识库里删掉。原因：基线轮检索找到了答案",
                   "- **建议** `labs.teaching.firstConversation.mustNotMention`：声明 firstConversation.mustNotMention。",
                   "> 这不是生产上线审批。"):
        assert needle in zh, needle
    assert "Remove the answer" not in zh and "decisive teaching contrast" not in zh
    en = guide.render_rehearsal_evidence(record, "en")
    for needle in ("Reason: `PHENOMENON_NOT_REPRODUCED` — a decisive teaching contrast did not reproduce in the latest complete run",
                   "Readiness blockers: `VERDICT_NOT_READY` (a decisive teaching contrast did not reproduce)",
                   "c1: RG_RETRIEVAL_OK_BASELINE — retrieval found the answer",
                   "Warnings:\n\n- `prompt-fix` · `c2`: the optimized answer improved but THELMA still labels it Fail\n"
                   "- gap reproduced in 1 of 2 complete runs; run again before class\n",
                   "- **Blocking** `knowledge-base/docs/faq.md` · `c1`: Remove the answer from the knowledge base. (retrieval found the answer)",
                   "- **Advisory** `labs.teaching.firstConversation.mustNotMention`: Declare firstConversation.mustNotMention.",
                   "> Not a production approval."):
        assert needle in en, needle
    assert not re.search(r"[一-鿿]", en)
    legacy = {k: v for k, v in record.items() if not k.endswith("En")}
    legacy["remediation"] = [{k: v for k, v in h.items() if not k.endswith("En")} for h in record["remediation"]]
    assert "把答案从知识库里删掉。" in guide.render_rehearsal_evidence(legacy, "en")  # no twin: the text as recorded


def test_attach_rehearsal_replaces_only_the_evidence_section():
    instructor = _guides(_scenario("it-helpdesk")).instructor
    attached = guide.attach_rehearsal(instructor, {"verdict": "ready", "phenomena": []}, "en", release_version="v")
    assert "Verdict: **ready**" in attached and "No rehearsal evidence is recorded" not in attached
    before, after = instructor.split(guide.REHEARSAL_BEGIN)[0], instructor.split(guide.REHEARSAL_END)[1]
    assert attached.startswith(before) and attached.endswith(after)
    with pytest.raises(guide.GuideError):
        guide.attach_rehearsal("no markers", None, "en")
    embedded = _guides(_scenario("it-helpdesk"), rehearsal={"verdict": "ready", "phenomena": []}).instructor
    assert "Verdict: **ready**" in embedded


def test_preview_renders_an_unreviewed_draft_with_the_banner(tmp_path):
    root, data = _copy_pack(tmp_path)
    data["labs"]["guide"]["provenance"] = "ai_draft"
    data["facts"][0]["provenance"] = "ai_draft"
    path = _write(root, data)
    markdown, findings = guide.preview(path, "student", template_commit=TEMPLATE_COMMIT)
    assert markdown.splitlines()[0].startswith(f"<!-- {guide.STUDENT_MARKER_TOKEN} ") and guide.DRAFT_MARKER in markdown
    assert "**DRAFT PREVIEW**" in markdown and "[Draft — not for class]" in markdown
    assert [f for f in findings if f.severity == "error"] == []
    instructor, _ = guide.preview(path, "instructor", template_commit=TEMPLATE_COMMIT)
    assert guide.DRAFT_MARKER in instructor and instructor.startswith(f"<!-- {guide.INSTRUCTOR_MARKER_TOKEN}")
    with pytest.raises(guide.GuideError):
        guide.preview(path, "everyone", template_commit=TEMPLATE_COMMIT)
    # A formal build never carries the draft marker.
    assert guide.DRAFT_MARKER not in _guides(_scenario("it-helpdesk")).student


def test_every_step_note_and_facilitator_note_is_rendered_once():
    data = copy.deepcopy(_scenario("maintenance").data)
    data["labs"]["guide"]["stepNotes"] = {sid: f"Student note for {sid}." for sid in guide.GUIDE_STEP_IDS}
    data["labs"]["guide"]["facilitatorNotes"] = {sid: f"Facilitator note for {sid}." for sid in guide.GUIDE_STEP_IDS}
    fake = SimpleNamespace(data=data, root=REPO_ROOT / "scenarios" / "maintenance", path=REPO_ROOT / "scenarios" / "maintenance" / "scenario.yaml")
    g = _guides(fake)
    for sid in guide.GUIDE_STEP_IDS:
        assert g.student.count(f"> **Scenario note:** Student note for {sid}.") == 1, sid
        assert g.instructor.count(f"Facilitator note for {sid}.") == 1, sid
        assert f"Facilitator note for {sid}." not in g.student
    assert guide.extract_bash_commands(g.student) == list(guide.UPSTREAM_GUIDE_COMMANDS)
