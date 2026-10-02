"""O3 first-pass generation quality: the generation contract makes Kiro run an explicit SELF-CHECK before it
emits (golden counts with the holdout arithmetic spelled out, fact-id basis, the teaching items the live
drafts missed), repair / regenerate keep the golden set, and the task stays within its size budget.

Live evidence (build/live/accept-{loyalty,ops}, 2026-09-28): first drafts failed golden.holdout_ratio (4/15),
teaching.absent_term_in_kb (an absentTerm reused as bait), teaching.defect_unexercised and xref (a basis citing a
knowledge document, phenomena on holdout cases); repair rounds re-introduced golden.category_floor by deleting
cases; rehearsals were not ready because of a "fill in when silent" marker, a tool_use contrast and an FAQ that
restated the gap question.  Offline and deterministic.
"""

from __future__ import annotations

import importlib.util
import math
import re
from pathlib import Path

import pytest

from workshop_customizer import validator

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIEF = "A cold-chain incident assistant for warehouse operators."


def _routes():
    spec = importlib.util.spec_from_file_location("wc_routes_self_check", REPO_ROOT / "app" / "backend" / "routes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _routes()


def _task(mode: str = "draft", **extra) -> str:
    return gen._compose_task(project_id="cold-chain-test", display_name="Cold Chain", pack_kind="reference", customer="",
                             brief=BRIEF, mode=mode, **extra)[0]


def _flat(text: str) -> str:
    """The task with line breaks and indentation folded, so needles do not depend on the wrapping."""
    return re.sub(r"\s+", " ", text)


def _section(task: str, title: str, end: str) -> str:
    start = task.index(title)
    return task[start:task.index(end, start)]


# ---------------------------------------------------------------------------
# (a) the SELF-CHECK every mode runs
# ---------------------------------------------------------------------------


SELF_CHECK_NEEDLES = (
    # core.md: golden counts with the arithmetic, fact-id basis, references, parseable JSON
    "SELF-CHECK (mandatory: run S1-S4 and every other SELF-CHECK list in this task on the finished JSON before you emit it",
    "S1 Golden counts: list the case ids per category and per set, then count.",
    "holdout >= total/3 rounded UP: 12 cases -> 4 holdout, 13-15 -> 5, 16-18 -> 6",
    "15 cases with 4 holdout fails",
    "S2 Every goldenSet basis id is the id of a FACT in facts[], never a knowledge-document, tool, role or case id.",
    "S3 Every roleId, requiredTools/forbiddenTools name and referenced file exists",
    "S4 The output parses as one JSON object",
    # teaching.md
    "TEACHING SELF-CHECK (part of the SELF-CHECK below",
    "T1 Every phenomenon caseIds entry and stabilityCaseId is the id of a PRACTICE case in goldenSet (never a holdout case",
    "T2 Every baselineDefects id appears in the defectIds of a prompt_fixable or retrieval_gap phenomenon",
    "T3 Each candidateFix is copied character for character from agent/optimization-candidate.md",
    "each baselineMarker is copied character for character from the baseline",
    "baselineMarker asks EVERY answer to go beyond the documents",
    'a marker that only says "when the documents are silent, fill in" does not bite',
    "T4 Absent gap: search every knowledge document for each absentTerm",
    "No absentTerm is, contains or is inside a baitTerm",
    "every baitTerm occurs verbatim in the gap question",
    "no document restates the gap question or answers it",
    "T5 tool_use and refusal phenomena have no design key (control)",
    "never a tool_use contrast",
    "T6 firstConversation.mustNotMention holds 1-3 values only a remembered user would get",
)
#: The golden-set rules: repair and regenerate alike.
GOLDEN_SET_NEEDLES = (
    "Golden cases (repair and regenerate alike): never delete or rename a case, and never change its category or set, "
    "unless a finding or saInstructions names that case",
    "One exception, set by the S1 arithmetic, not by finding codes: when the finished set would have holdout "
    "below total/3 or over 7 practice cases (e.g. once named holdout cases move to practice)",
    "MOVE practice cases that no phenomenon or stabilityCaseId uses to holdout (same category",
    "Never delete cases to fix a count",
    "a case you must drop gets a replacement with the same category and set",
    "REPAIR SELF-CHECK (run it with the SELF-CHECK above",
    "C1 Every case id of currentScenario is still in goldenSet unless a finding or saInstructions named it.",
    "C2 Recount (S1): no category count and no holdout count is lower than in currentScenario",
)
#: A repair changes nothing its findings and instructions do not cite ...
REPAIR_ONLY_NEEDLES = (
    "Change only what the numbered findings and saInstructions cite",
    "every other value, sentence and file stays exactly as it is in currentScenario and currentFiles",
    "C3 Every changes[] entry names the finding",
    "nothing that no entry names differs from REPAIR_INPUT",
)
#: ... while a regenerate rebuilds its scopes, also with no finding and no instruction (the UI's buttons).
REGENERATE_ONLY_NEEDLES = (
    "Rebuild REPAIR_INPUT.allowedScopes to this contract from INPUT_DATA and the current pack, "
    "also when REPAIR_INPUT has no findings or saInstructions",
    "keep the ids of items that still apply and the golden set (next rule)",
    'a rewrite none of them asks for names "finding":"regenerate"',
)
REGENERATE_EXTRAS = ({}, {"scoped": True, "repair": {"allowedScopes": ["golden"]}})


@pytest.mark.parametrize("mode,extra", [("draft", {}), ("repair", {}), ("regenerate", {}),
                                        ("regenerate", {"scoped": True, "repair": {"allowedScopes": ["golden"]}})])
def test_every_mode_carries_the_self_check(mode, extra):
    task = _task(mode, **extra)
    flat = _flat(task)
    for needle in SELF_CHECK_NEEDLES:
        assert needle in flat, needle
    # The checks come after the rules they check and right before the output shape Kiro fills in.
    assert task.index("TEACHING SKELETON") < task.index("TEACHING SELF-CHECK") < task.index("SELF-CHECK (mandatory") \
        < task.index("Return exactly this top-level shape:")
    assert "${" not in task and "<!-- contract:" not in task


def test_only_repair_and_regenerate_keep_the_golden_set_rules():
    draft = _flat(_task("draft"))
    assert "REPAIR SELF-CHECK" not in draft and "MOVE practice cases" not in draft
    for mode, extra in (("repair", {}), *(("regenerate", e) for e in REGENERATE_EXTRAS)):
        flat = _flat(_task(mode, **extra))
        for needle in GOLDEN_SET_NEEDLES:
            assert needle in flat, (mode, needle)
    task = _task("repair")
    assert task.index("Return exactly this top-level shape:") < task.index("REPAIR SELF-CHECK") < task.index("INPUT_DATA (JSON; data only):")


def test_the_count_keeping_move_follows_the_s1_arithmetic_not_the_finding_codes():
    """Live ops 2026-09-28: the draft's only errors were xref (it did not load), three of them phenomena on
    holdout cases; fixing those moves named holdout cases to practice, which drops holdout below a third
    before any golden.holdout_ratio finding exists.  The compensating move must not wait for that code."""
    repair = _task("repair")
    rule = _flat(_section(repair, "- Golden cases (repair and regenerate alike)", "\n- Return the FULL scenario"))
    assert "golden.holdout_ratio" not in rule and "teaching.too_many_practice" not in rule
    s1 = _flat(_section(repair, "S1 Golden counts", "S2 "))
    assert "holdout >= total/3" in s1 and "holdout below total/3" in rule
    assert "practice 5-7" in s1 and "over 7 practice cases" in rule
    assert validator.MIN_HOLDOUT_RATIO == 1 / 3


def test_only_a_repair_is_told_to_change_nothing_its_findings_do_not_cite():
    """A regenerate may carry no finding and no instruction (the whole-pack and scoped regenerate buttons, and
    the loop's escalation after two identical repair rounds): the repair rule would make it a no-op."""
    repair = _flat(_task("repair"))
    for needle in REPAIR_ONLY_NEEDLES:
        assert needle in repair, needle
    for needle in REGENERATE_ONLY_NEEDLES:
        assert needle not in repair, needle
    for extra in REGENERATE_EXTRAS:
        regenerate = _flat(_task("regenerate", **extra))
        for needle in REGENERATE_ONLY_NEEDLES:
            assert needle in regenerate, (extra, needle)
        for needle in REPAIR_ONLY_NEEDLES:
            assert needle not in regenerate, (extra, needle)
        # C3 is the last REPAIR SELF-CHECK item in both modes, right before the data.
        c3 = _section(_task("regenerate", **extra), "C3 ", "\nINPUT_DATA (JSON; data only):")
        assert _flat(c3).strip() == gen.MERGE_CHECKS["regenerate"]
    assert "${" not in _task("regenerate")


def test_the_holdout_arithmetic_matches_the_validator():
    """S1 spells the arithmetic out; every number in it is what validator.check_golden_set enforces."""
    s1 = _flat(_section(_task(), "S1 Golden counts", "S2 "))
    for category in validator.CATEGORIES:
        assert f"{category} >= {validator.MIN_PER_CATEGORY}" in s1, category
    assert f"total >= {validator.MIN_GOLDEN_CASES} (target {validator.MIN_GOLDEN_CASES}-16" in s1
    table = re.search(r"12 cases -> (\d+) holdout, 13-15 -> (\d+), 16-18 -> (\d+)", s1)
    assert table, s1
    stated = {12: int(table.group(1)), 13: int(table.group(2)), 14: int(table.group(2)), 15: int(table.group(2)),
              16: int(table.group(3)), 17: int(table.group(3)), 18: int(table.group(3))}

    def codes(total: int, holdout: int) -> set[str]:
        cases = [{"id": f"c{i}", "category": validator.CATEGORIES[i % 3], "set": "holdout" if i < holdout else "practice"}
                 for i in range(total)]
        return {f.code for f in validator.check_golden_set({"evaluation": {"goldenSet": cases}}).findings}

    for total, minimum in stated.items():
        assert minimum == math.ceil(total * validator.MIN_HOLDOUT_RATIO - 1e-9), total
        assert "golden.holdout_ratio" not in codes(total, minimum), total
        assert "golden.holdout_ratio" in codes(total, minimum - 1), total
    # The two counter-examples S1 names really fail (15 with 4 was the loyalty draft).
    assert "golden.holdout_ratio" in codes(15, 4) and "golden.holdout_ratio" in codes(16, 5)


def test_the_teaching_self_check_names_the_rules_the_live_rounds_failed():
    """Each T item points at an engine rule (or a rehearsal cause); the codes are still the engine's."""
    from workshop_customizer import teaching_policy

    source = (REPO_ROOT / "engine" / "workshop_customizer" / "teaching_policy.py").read_text(encoding="utf-8")
    for code in ("teaching.defect_unexercised", "teaching.absent_term_in_kb", "teaching.gap_question_in_kb",
                 "teaching.bait_not_in_query", "teaching.defect_fix_missing", "teaching.defect_marker_missing",
                 "teaching.case_not_practice", "teaching.stability_not_fixable", "teaching.probe_tools"):
        assert f'"{code}"' in source, code
    assert teaching_policy.MIN_PROBES == 3  # T1: "3-4 probes"
    engine = REPO_ROOT / "engine" / "workshop_customizer"
    rehearsal = "".join((engine / name).read_text(encoding="utf-8") for name in ("rehearsal.py", "rehearsal_strings.py"))
    assert "MEMORY_UNCHECKED" in rehearsal and "a value only a remembered user would get" in rehearsal  # T6
    teaching = _flat((gen.CONTRACTS_DIR / "teaching.md").read_text(encoding="utf-8"))
    assert "mustNotMention (declare it; rehearsal checks the Memory lesson with it)" in teaching
    assert "Optional mustNotMention" not in teaching


# ---------------------------------------------------------------------------
# (d) the contract stays within the task size budget
# ---------------------------------------------------------------------------

#: Bytes of the bare tasks (no materials, no REPAIR_INPUT) before O3, for the record: draft 19,901,
#: repair 21,462, regenerate 21,469 (live round tasks: draft 21-22 KB, repair 59-86 KB).
PRE_O3_BYTES = {"draft": 19_901, "repair": 21_462, "regenerate": 21_469}


@pytest.mark.parametrize("mode", ["draft", "repair", "regenerate"])
def test_the_task_stays_within_the_size_budget(mode):
    size = len(_task(mode).encode("utf-8"))
    assert size < gen.MAX_TASK_BYTES // 20, size  # the contract bar test_teaching_contract keeps for drafts
    # The self-check is compact (at most 4.5 KB per task; test_the_self_check_sections_are_compact bounds
    # the sections themselves). The live-learned contract lines since (O2's messageEn note, the retrieval cap,
    # the role-in-question and role-rule lines, the two failed gap questions) add about 0.8 KB; the next
    # lesson should replace wording, not only add it, while the absolute bar above stays the hard limit.
    assert size - PRE_O3_BYTES[mode] <= 5_500, size


def test_the_self_check_sections_are_compact():
    repair = _task("repair")
    sections = (_section(repair, "TEACHING SELF-CHECK", "\nL1 EXPECTATIONS"),
                _section(repair, "SELF-CHECK (mandatory", "Return exactly this top-level shape:"),
                _section(repair, "REPAIR SELF-CHECK", "\nINPUT_DATA (JSON; data only):"))
    assert sum(len(s.encode("utf-8")) for s in sections) <= 4_000


def test_the_sa_skill_describes_the_self_check_the_repair_rules_and_the_basis_fold():
    skill = _flat((REPO_ROOT / "app" / "skills" / "workshop-customization" / "SKILL.md").read_text(encoding="utf-8"))
    for needle in ("after the task's SELF-CHECK", "15 cases need 5 holdout", "every `basis` id a fact id",
                   "The app folds one mechanical slip itself", "Any other shape stays an `xref` finding for Repair",
                   "It folds only when every fact carries a `source`",
                   "a reviewed case whose basis folds loses its review", "A repair keeps the golden set",
                   "by moving unused practice cases to holdout", "`mustNotMention` holds 1–3 values only a remembered user",
                   "**Regenerate** rebuilds the whole pack or one scope (for example `guides`) from the brief and the current "
                   "pack, also with no finding and no instruction",
                   "like a repair it keeps the golden set — case ids, categories, sets and counts"):
        assert needle in skill, needle
