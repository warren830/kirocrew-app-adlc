"""Rehearsal (SPEC D2, D8): the single owner of the verdict rules, the class verdict and readyForClass.

Offline and deterministic: every run is the canonical fixture of tests/fixtures/teaching_run (the 2026-09-13
IT numbers, a synthetic HR run that reproduces, and the SP2 = 0.25 boundary), sometimes edited in memory.
"""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

import teaching_run as tr
from workshop_customizer import guided_report, rehearsal, script_facts, teaching
from workshop_customizer.guided_run_store import GuidedRunStore, read_history

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE = "hr-default-0123456789ab"
EXPECTED_VERDICT = {"it-2026-09-13": "insufficient_evidence", "hr-reproduced": "ready", "hr-boundary-sp2": "not_ready"}
BOTH_GUIDES = {"student": True, "instructor": True}


def _documents(run: str) -> dict[str, str]:
    pack = tr.run_pack(run)
    return teaching.document_texts(tr.scenario(pack), REPO_ROOT / "scenarios" / pack)


def _state(run: str, steps=None, *, updated: str = "2026-09-20T10:00:00Z", tag: str | None = None, release: str | None = None) -> dict:
    """A stored Guided Run state around the fixture steps (the shape GuidedRunStore persists)."""
    state = tr.run_state(run, copy.deepcopy(steps if steps is not None else tr.load_steps(run)))
    state.update(schemaVersion=1, status="passed", createdAt=updated, updatedAt=updated, currentStepId=None, report=None)
    if release:
        state["releaseVersion"] = release
    for step in state["steps"].values():
        step["finishedAt"] = updated
        if tag and step.get("commandId"):
            step["commandId"] = f"{step['commandId']}-{tag}"
    return state


def _rehearse(run: str, state=None, *, history=(), guides=None, scenario=None) -> dict:
    doc = rehearsal.build_rehearsal(state or _state(run), scenario or tr.scenario(run), history=history,
                                    documents=_documents(run), guides=guides)
    assert rehearsal.validate_document(doc) == []
    return doc


def _hints(doc: dict, code: str) -> list[dict]:
    return [h for h in doc["remediation"] if h["code"] == code]


def _phenomenon(doc: dict, pid: str) -> dict:
    return next(p for p in doc["phenomena"] if p["id"] == pid)


# ---------------------------------------------------------------------------
# One owner for the rules
# ---------------------------------------------------------------------------


def test_the_verdict_rules_live_only_in_rehearsal():
    assert guided_report.teaching_contrast is rehearsal.teaching_contrast
    assert guided_report.FOCUS_CHECKS is rehearsal.FOCUS_CHECKS and guided_report.HONESTY_CHECKS is rehearsal.HONESTY_CHECKS
    report_tree = ast.parse((REPO_ROOT / "engine/workshop_customizer/guided_report.py").read_text(encoding="utf-8"))
    defined = {node.name for node in ast.walk(report_tree) if isinstance(node, ast.FunctionDef)}
    rules = {"teaching_contrast", "_prompt_fixable_case", "_retrieval_gap_case", "_l1_case", "_aggregate", "_group", "_focus"}
    assert not defined & rules, "guided_report must not re-implement a verdict rule"
    # No threshold is re-encoded: rehearsal reads teaching.THRESHOLDS through its helpers only.
    tree = ast.parse((REPO_ROOT / "engine/workshop_customizer/rehearsal.py").read_text(encoding="utf-8"))
    floats = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, float)}
    assert not floats & set(teaching.THRESHOLDS.values()), floats


def test_rehearsal_imports_no_report_module_at_import_time():
    tree = ast.parse((REPO_ROOT / "engine/workshop_customizer/rehearsal.py").read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {alias.name for n in top for alias in n.names} | {n.module for n in top if isinstance(n, ast.ImportFrom) and n.module}
    assert "guided_report" not in names, "guided_report imports the rules from rehearsal; rehearsal imports it lazily"


# ---------------------------------------------------------------------------
# The fixture runs: rehearsal and report.teachingContrast agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("run", tr.RUNS)
def test_rehearsal_verdicts_equal_the_report_teaching_contrast(run):
    state = _state(run)
    report = guided_report.build_report(state, tr.scenario(run), generated_at="x")
    contrast = report["teachingContrast"]
    doc = _rehearse(run, state)
    assert doc["verdict"] == rehearsal.RUN_VERDICT[contrast["currentRun"]] == EXPECTED_VERDICT[run]
    assert doc["groups"] == contrast["groups"] == tr.MANIFEST["runs"][run]["expect"]["groups"]
    assert {p["id"]: p["verdict"] for p in doc["phenomena"]} == {p["id"]: p["status"] for p in contrast["phenomena"]}
    assert all(p["cases"] == q["cases"] for p, q in zip(doc["phenomena"], contrast["phenomena"]))
    assert doc["inputs"]["decisiveRun"] == "current" and doc["inputs"]["reportStatus"] == report["status"] == "complete"
    assert doc["runs"][0]["verdict"] == doc["verdict"] and doc["consistency"] == "single_run"
    assert doc["readyForClass"] is False  # no guide is built before P3
    assert "readyForClass" not in json.dumps(report)


def test_hr_reproduced_is_ready_for_class_only_with_both_guides():
    doc = _rehearse("hr-reproduced")
    assert (doc["verdict"], doc["reasonCode"]) == ("ready", "CONTRASTS_REPRODUCED")
    assert doc["readiness"]["blockers"] == ["GUIDES_MISSING"] and rehearsal.exit_code(doc) == rehearsal.EXIT_READY_NOT_FOR_CLASS
    [guide] = _hints(doc, "GUIDE_MISSING")
    assert guide["asset"] == {"kind": "guide", "file": None, "scenarioPath": "labs.guide", "scopes": ["guides"]}
    assert "build/release/README.md" in guide["because"] and "build/instructor/instructor-guide.md" in guide["because"]

    half = _rehearse("hr-reproduced", guides={"student": True, "instructor": False})
    assert half["readyForClass"] is False and half["readiness"]["guides"]["instructor"] == {"path": "build/instructor/instructor-guide.md", "present": False}
    ready = _rehearse("hr-reproduced", guides=BOTH_GUIDES)
    assert ready["readyForClass"] is True and ready["readiness"]["blockers"] == [] and rehearsal.exit_code(ready) == 0
    assert not [h for h in ready["remediation"] if h["severity"] == "blocking"]


def test_only_the_build_snapshot_of_a_verified_release_can_be_ready_for_class():
    """SPEC D8: the verdict is computed for any input, but readyForClass needs the build snapshot of the run's release."""
    ready = _rehearse("hr-reproduced", guides=BOTH_GUIDES)
    assert (ready["inputs"]["scenario"], ready["inputs"]["releaseVerified"], ready["readyForClass"]) == ("build snapshot", True, True)
    file_doc = rehearsal.build_rehearsal(_state("hr-reproduced"), tr.scenario("hr-reproduced"), documents=_documents("hr-reproduced"),
                                         guides=BOTH_GUIDES, scenario_source="scenario file", release_verified=False)
    assert rehearsal.validate_document(file_doc) == []
    assert (file_doc["verdict"], file_doc["readyForClass"]) == ("ready", False)
    assert file_doc["readiness"]["blockers"] == ["SCENARIO_NOT_BUILD_SNAPSHOT", "RELEASE_NOT_VERIFIED"]
    assert file_doc["inputs"]["scenario"] == "scenario file" and rehearsal.exit_code(file_doc) == rehearsal.EXIT_READY_NOT_FOR_CLASS
    assert [h["code"] for h in file_doc["remediation"] if h["severity"] == "blocking"] == ["SCENARIO_NOT_BUILD_SNAPSHOT", "RELEASE_NOT_VERIFIED"]
    unverified = rehearsal.build_rehearsal(_state("hr-reproduced"), tr.scenario("hr-reproduced"), guides=BOTH_GUIDES, release_verified=False)
    assert unverified["readiness"]["blockers"] == ["RELEASE_NOT_VERIFIED"] and unverified["readyForClass"] is False
    with pytest.raises(ValueError, match="scenario_source"):
        rehearsal.build_rehearsal(_state("hr-reproduced"), tr.scenario("hr-reproduced"), scenario_source="scenario.yaml")


def test_guides_built_needs_the_verified_student_readme_and_instructor_guide(tmp_path):
    """Both guides must exist and match the build's records (build/checksums.json, release RELEASE.json)."""
    import hashlib

    from workshop_customizer import render

    assert rehearsal.RELEASE_MANIFEST == render.MANIFEST_NAME
    assert rehearsal.guide_status(None) == {"student": False, "instructor": False}
    assert rehearsal.guide_status(tmp_path) == {"student": False, "instructor": False}
    student, instructor = b"# Student guide\n", b"# Instructor guide\n"
    digest = {k: hashlib.sha256(v).hexdigest() for k, v in (("s", student), ("i", instructor))}
    (tmp_path / "release").mkdir()
    (tmp_path / "release" / "README.md").write_bytes(student)
    (tmp_path / "instructor").mkdir()
    (tmp_path / "instructor" / "instructor-guide.md").write_bytes(instructor)
    assert rehearsal.guide_status(tmp_path) == {"student": False, "instructor": False}  # files alone never count
    (tmp_path / "checksums.json").write_text(json.dumps({"files": {"pack/labs/student-guide.md": digest["s"],
                                                                   "instructor/instructor-guide.md": digest["i"]}}))
    assert rehearsal.guide_status(tmp_path) == {"student": False, "instructor": True}  # README.md is not in RELEASE.json yet
    (tmp_path / "release" / "RELEASE.json").write_text(json.dumps({"files": {"README.md": digest["s"]}}))
    assert rehearsal.guide_status(tmp_path) == {"student": True, "instructor": True}
    (tmp_path / "release" / "README.md").write_text("# Another guide\n", encoding="utf-8")
    assert rehearsal.guide_status(tmp_path) == {"student": False, "instructor": True}


def test_exit_codes_follow_the_verdict():
    assert rehearsal.exit_code({"readyForClass": True, "verdict": "ready"}) == 0
    assert rehearsal.exit_code({"readyForClass": False, "verdict": "ready"}) == 4
    assert rehearsal.exit_code({"readyForClass": False, "verdict": "insufficient_evidence"}) == 2
    assert rehearsal.exit_code({"readyForClass": False, "verdict": "not_ready"}) == 3


def test_deterministic_and_generated_at_comes_from_the_inputs():
    history = [("history/a", _state("hr-boundary-sp2", updated="2026-09-18T08:00:00Z", release=RELEASE))]
    state = _state("hr-reproduced", updated="2026-09-20T10:00:00Z", release=RELEASE)
    first = _rehearse("hr-reproduced", state, history=history)
    second = _rehearse("hr-reproduced", copy.deepcopy(state), history=copy.deepcopy(history))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["generatedAt"] == "2026-09-20T10:00:00Z" and first["schema"] == rehearsal.SCHEMA


# ---------------------------------------------------------------------------
# Remediation on concrete assets
# ---------------------------------------------------------------------------


def test_sp2_boundary_points_to_the_document_carrying_the_answer():
    doc = _rehearse("hr-boundary-sp2")
    gap = _phenomenon(doc, "sick-leave-retrieval-gap")
    assert (gap["verdict"], gap["reasonCode"]) == ("not_reproduced", "RG_RETRIEVAL_OK_BASELINE")
    [kb] = [h for h in _hints(doc, "RG_RETRIEVAL_OK_BASELINE") if h["caseId"] == "sick-leave-certificate"]
    assert kb["asset"] == {"kind": "kb_doc", "file": "knowledge-base/docs/time_off_report.md",
                           "scenarioPath": "knowledge.documents[id=time-off-report]", "scopes": ["knowledge"]}
    assert kb["severity"] == "blocking" and kb["caseId"] == "sick-leave-certificate" and "SP2 0.25" in kb["because"]
    [redeclare] = [h for h in _hints(doc, "RG_REDECLARE") if "sick-leave" in h["asset"]["scenarioPath"]]
    assert redeclare["asset"]["scenarioPath"] == "labs.teaching.phenomena[id=sick-leave-retrieval-gap]"
    assert gap["hintIds"] == [kb["id"], redeclare["id"]]
    assert [h["id"] for h in doc["remediation"]] == [f"h{i:02d}" for i in range(1, len(doc["remediation"]) + 1)]


def test_it_2026_09_13_names_the_prompts_and_the_case_to_fix():
    doc = _rehearse("it-2026-09-13")
    assert (doc["verdict"], doc["reasonCode"]) == ("insufficient_evidence", "PHENOMENON_INSUFFICIENT")
    fixable = _phenomenon(doc, "prompt-fix-where-retrieval-good")
    assert (fixable["verdict"], fixable["reasonCode"]) == ("insufficient_evidence", "PF_NOT_SCORED")
    [gain] = _hints(doc, "PF_NO_GAIN")
    assert gain["asset"]["file"] == "agent/optimization-candidate.md" and gain["caseId"] == "p2-response-time"
    assert "-0.214" in gain["because"]
    passes = {h["asset"]["kind"]: h for h in _hints(doc, "PF_BASELINE_ALREADY_PASSES")}
    assert passes["baseline_prompt"]["asset"]["file"] == "agent/baseline-prompt.md" and "0.941" in passes["baseline_prompt"]["because"]
    assert passes["teaching"]["asset"]["scenarioPath"] == "labs.teaching.phenomena[id=prompt-fix-where-retrieval-good]"
    unscored = {(h["caseId"], h["asset"]["kind"]) for h in _hints(doc, "PF_NOT_SCORED")}
    assert unscored == {("lockout-duration", "golden_case"), ("lockout-duration", "baseline_prompt")}
    assert _hints(doc, "PF_NOT_SCORED")[0]["asset"]["scenarioPath"] == "evaluation.goldenSet[id=lockout-duration].query"
    assert [h["caseId"] for h in _hints(doc, "RG_ABSENT_NOT_SCORED")] == ["vpn-session-timeout"]
    assert not _hints(doc, "MEMORY_PERSONALIZED")  # the 06 answer is generic
    assert {a["kind"]: a["reading"] for a in doc["advisory"]} == {"noise_grounding": "observed", "memory": "generic"}


def test_l1_failures_map_to_the_candidate_prompt_the_tool_fixture_and_the_case():
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for row in steps["optimize"]["outputs"]["l1"]["cases"]:
        if row["id"] == "own-annual-balance":
            row.update(v="fail", fail=["requiredTools"])
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps))
    tool = _phenomenon(doc, "own-balance-tool")
    assert (doc["verdict"], tool["verdict"], tool["reasonCode"]) == ("not_ready", "not_reproduced", "L1_OPTIMIZED_FAILS")
    [prompt] = _hints(doc, "L1_OPTIMIZED_FAILS")
    assert prompt["asset"]["kind"] == "candidate_prompt" and "check_leave_balance" in prompt["action"]
    # hr-default is a zh-CN pack: the text is Chinese, with the English original next to it.
    assert "优化轮 L1 没通过：requiredTools" in prompt["because"] and "optimized L1 failed: requiredTools" in prompt["becauseEn"]
    assert "check_leave_balance" in prompt["actionEn"] and prompt["action"].startswith("在候选提示词里加一条明确的规则")
    [fixture] = _hints(doc, "L1_TOOL_FIXTURE")
    assert fixture["asset"] == {"kind": "tool_fixture", "file": "scenario.yaml", "scenarioPath": "tools[name=check_leave_balance].fixtures",
                                "scopes": ["tools"]}
    [expected] = _hints(doc, "L1_EXPECTED_CHECK")
    assert expected["asset"]["scenarioPath"] == "evaluation.goldenSet[id=own-annual-balance].expected"


def test_contrast_design_with_a_safe_baseline_points_at_the_baseline_prompt():
    data = copy.deepcopy(tr.scenario("hr-default"))
    for p in data["labs"]["teaching"]["phenomena"]:
        if p["kind"] == "refusal":
            p["design"] = "contrast"
    doc = _rehearse("hr-reproduced", scenario=data)
    refusal = _phenomenon(doc, "colleague-salary-refusal")
    assert (refusal["design"], refusal["reasonCode"]) == ("contrast", "L1_BASELINE_ALREADY_PASSES")
    kinds = {h["asset"]["kind"]: h for h in _hints(doc, "L1_BASELINE_ALREADY_PASSES")}
    assert set(kinds) == {"baseline_prompt", "teaching"} and "design: control" in kinds["teaching"]["action"]


def test_mind_the_goal_failing_a_correct_refusal_is_a_warning_only():
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for row in steps["optimize"]["outputs"]["scores"]:
        if row["evaluator"] == "mtg_goal_success" and row["caseId"] == "colleague-salary":
            row["label"] = "Fail"
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps))
    assert doc["verdict"] == "ready" and _phenomenon(doc, "colleague-salary-refusal")["verdict"] == "reproduced"
    [warning] = [w for w in doc["warnings"] if w["code"] == "MTG_REFUSAL_CONFLICT"]
    assert warning["refs"] == ["colleague-salary-refusal", "colleague-salary"]


def test_a_noisy_judge_gives_one_judge_hint():
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    steps["judge-stability"]["outputs"]["judgeStability"].update(std=0.2, spread=0.3)
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps))
    assert doc["verdict"] == "insufficient_evidence" and doc["inputs"]["noiseBand"]["value"] == 0.4
    [judge] = _hints(doc, teaching.JUDGE_TOO_NOISY)
    assert judge["caseId"] is None and judge["asset"]["kind"] == "judge" and judge["asset"]["scenarioPath"] == "evaluation.noiseBand"
    assert "choose a steadier" not in judge["action"] and "13-judge-stability.sh" in judge["action"]  # the judge model is fixed


def test_a_failing_case_of_a_reproduced_group_is_advisory():
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for row in steps["optimize"]["outputs"]["scores"]:
        if row["evaluator"] == "thelma_rag_quality" and row["caseId"] == "benefits-enrollment-process":
            row["value"] = 0.52  # +0.02: no gain, the other fixable case still reproduces
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps))
    assert doc["verdict"] == "ready" and _phenomenon(doc, "prompt-fix-where-retrieval-good")["verdict"] == "reproduced"
    [hint] = _hints(doc, "PF_NO_GAIN")
    assert hint["severity"] == "advisory" and hint["caseId"] == "benefits-enrollment-process"


def test_a_shared_case_less_hint_blocks_when_any_phenomenon_blocks():
    """METRICS_MISSING has no case id: first advisory (its PF group reproduces), then blocking for the gap."""
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for sid in ("baseline", "optimize"):
        for row in steps[sid]["outputs"]["scores"]:
            if row["evaluator"] != "thelma_rag_quality":
                continue
            if row["caseId"] == "benefits-enrollment-process":
                row["metrics"].pop("SQC")
            if row["caseId"] in ("sick-leave-certificate", "volunteer-days-gap") and sid == "baseline":
                row["metrics"].pop("SQC")
                row["metrics"].pop("SP2")
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps), guides=BOTH_GUIDES)
    assert (doc["verdict"], doc["reasonCode"]) == ("insufficient_evidence", "PHENOMENON_INSUFFICIENT")
    assert (doc["groups"]["prompt_fixable"], doc["groups"]["retrieval_gap"]) == ("reproduced", "insufficient_evidence")
    [metrics] = _hints(doc, "METRICS_MISSING")
    assert metrics["severity"] == "blocking" and metrics["caseId"] is None
    assert metrics["phenomenonId"] == "sick-leave-retrieval-gap"
    assert metrics["phenomenonIds"] == ["prompt-fix-where-retrieval-good", "sick-leave-retrieval-gap", "volunteer-days-gap"]
    assert "neither SP2 nor SQC in the baseline run" in metrics["becauseEn"] and "基线轮 THELMA 既没返回 SP2" in metrics["because"]
    assert metrics["id"] in _phenomenon(doc, "prompt-fix-where-retrieval-good")["hintIds"]
    assert metrics["id"] in _phenomenon(doc, "sick-leave-retrieval-gap")["hintIds"]
    assert [h["id"] for h in doc["remediation"] if h["severity"] == "blocking"] == [metrics["id"]]


def test_hint_severity_only_rises_and_every_phenomenon_is_recorded():
    hints = rehearsal._Hints()
    asset = rehearsal._asset("ops")
    first = hints.add("X", asset, "act", "blocks here", severity="blocking", phenomenon_id="a")
    assert hints.add("X", asset, "act", "advisory there", severity="advisory", phenomenon_id="b") == first
    assert hints.add("X", asset, "act", "again", severity="advisory", phenomenon_id="b") == first
    [hint] = hints.items
    assert (hint["severity"], hint["phenomenonId"], hint["because"], hint["phenomenonIds"]) == ("blocking", "a", "blocks here", ["a", "b"])


def test_undeclared_teaching_and_missing_parity_are_not_ready():
    data = copy.deepcopy(tr.scenario("hr-default"))
    del data["labs"]["teaching"]
    doc = _rehearse("hr-reproduced", scenario=data)
    assert (doc["verdict"], doc["reasonCode"], doc["phenomena"]) == ("not_ready", "TEACHING_UNDECLARED", [])
    assert _hints(doc, "TEACHING_UNDECLARED")[0]["asset"]["scenarioPath"] == "labs.teaching"

    data = copy.deepcopy(tr.scenario("hr-default"))
    data["labs"]["teaching"]["phenomena"] = [p for p in data["labs"]["teaching"]["phenomena"] if p["kind"] != "retrieval_gap"]
    doc = _rehearse("hr-reproduced", scenario=data)
    assert (doc["verdict"], doc["reasonCode"], doc["groups"]["retrieval_gap"]) == ("not_ready", "PARITY_MISSING", "missing")
    [parity] = _hints(doc, "PARITY_MISSING")
    assert parity["asset"]["scenarioPath"] == "labs.teaching.phenomena" and "retrieval_gap" in parity["action"]


def test_advisory_memory_and_noise_readings_never_change_the_verdict():
    steps = copy.deepcopy(tr.load_steps("it-2026-09-13"))
    steps["conversation"]["outputs"]["conversation"]["response"] = "Your laptop NWR-4471 was bought in 2022, so you are due."
    doc = _rehearse("it-2026-09-13", _state("it-2026-09-13", steps))
    assert doc["verdict"] == "insufficient_evidence"
    [memory] = _hints(doc, "MEMORY_PERSONALIZED")
    assert memory["severity"] == "advisory" and memory["asset"]["scenarioPath"] == "labs.teaching.firstConversation.actorId"
    assert "NWR-4471" in memory["because"]

    # The value is in the tool description (freight-claims, live 2026-09-29): not memory, so no actorId advice.
    data = copy.deepcopy(tr.scenario("it-2026-09-13"))
    tool = next(t for t in data["tools"] if t["name"] == "check_device_warranty")
    tool["inputSchema"]["properties"]["asset_tag"]["description"] = "Asset tag printed on the device, e.g. NWR-4471"
    doc = _rehearse("it-2026-09-13", _state("it-2026-09-13", steps), scenario=data)
    assert not _hints(doc, "MEMORY_PERSONALIZED")
    [given] = _hints(doc, "MEMORY_TERM_GIVEN")
    assert given["asset"]["scenarioPath"] == "labs.teaching.firstConversation.mustNotMention"
    assert "check_device_warranty" in given["because"] and doc["verdict"] == "insufficient_evidence"

    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for row in steps["baseline"]["outputs"]["scores"]:
        if row["evaluator"] == "thelma_rag_quality":
            row["metrics"]["SP1"] = row["metrics"]["SP2"]  # no chunk/fact precision gap anywhere
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps))
    assert doc["verdict"] == "ready"
    noise = next(a for a in doc["advisory"] if a["kind"] == "noise_grounding")
    assert noise["reading"] == "not_observed" and noise["source"] == "current"
    files = [h["asset"]["file"] for h in _hints(doc, "NOISE_NOT_OBSERVED")]
    assert files == ["knowledge-base/docs/time_off_report.md", "knowledge-base/docs/benefits_enrollment.md",
                     "knowledge-base/docs/performance_review.md"]
    assert _hints(doc, "MEMORY_UNCHECKED")  # hr-default declares no firstConversation.mustNotMention


def test_an_absent_gap_that_retrieves_names_the_bait_documents_not_the_hand_off_wording():
    steps = copy.deepcopy(tr.load_steps("it-2026-09-13"))
    for step in ("baseline", "optimize"):
        steps[step]["outputs"]["scores"].append({
            "caseId": "vpn-session-timeout", "evaluator": "thelma_rag_quality", "label": "Fail", "traceId": "0" * 16, "value": 0.4,
            "sessionId": f"session-{step}-vpn", "metrics": {"GR": 0.4, "SP1": 1.0, "SP2": 0.6, "SQC": 0.8, "RP": 0.5, "RQC": 0.5, "SD": 0.7}})
    doc = _rehearse("it-2026-09-13", _state("it-2026-09-13", steps))
    gap = _phenomenon(doc, "vpn-session-limit-gap")
    assert (gap["mechanism"], gap["reasonCode"]) == ("absent", "RG_RETRIEVAL_OK_BASELINE")
    hints = _hints(doc, "RG_RETRIEVAL_OK_BASELINE")
    # expected.mustMention is the hand-off wording ("ticket"); no document states an absentTerm, so the
    # noise documents carrying the baitTerm ("VPN session") are what retrieval found.
    assert [h["asset"]["file"] for h in hints] == ["knowledge-base/docs/vpn_access.md", "knowledge-base/docs/incident_priority_sla.md"]
    assert "bait lines" in hints[0]["actionEn"] and all(h["severity"] == "blocking" for h in hints)
    # No document states the absent term, so the question itself overlaps a documented topic (VPN access):
    # the first hint asks for a question about a topic no document covers (live 2026-09-29, lease-contract).
    [topic] = _hints(doc, "RG_ABSENT_TOPIC_COVERED")
    assert topic["asset"]["scenarioPath"] == "evaluation.goldenSet[id=vpn-session-timeout].query" and topic["severity"] == "blocking"
    assert "session timeout" in topic["actionEn"] and "topic no document covers" in topic["actionEn"]


def test_an_empty_optimized_reply_on_an_absent_gap_admits_nothing_and_points_at_the_candidate():
    """Live 2026-09-29: the optimized agent retrieved ~30 times and ended its turn with no text; Mind the Goal
    still passed the trace. The gap reproduces (retrieval found nothing twice), but honesty is 'no answer'."""
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for row in steps["optimize"]["outputs"]["l1"]["cases"]:
        if row["id"] == "volunteer-days-gap":
            row.update(v="fail", fail=["response"], unverified=["mustMentionAnyOf", "shouldEscalate"])
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps), guides=BOTH_GUIDES)
    gap = _phenomenon(doc, "volunteer-days-gap")
    [case] = gap["cases"]
    assert gap["verdict"] == "reproduced" and case["honesty"] == rehearsal.NO_ANSWER
    assert doc["verdict"] == "ready" and doc["readyForClass"] is True
    # Further from the lesson than a reply that does not admit the gap, so it is a warning too (not only an advisory hint).
    [warning] = doc["warnings"]
    assert (warning["code"], warning["refs"]) == ("ABSENT_GAP_NO_ANSWER", ["volunteer-days-gap", "volunteer-days-gap"])
    assert "empty reply" in warning["messageEn"] and "空回答" in warning["message"]
    assert "hands off" not in warning["messageEn"]  # an empty reply may still have called an escalation tool
    [hint] = _hints(doc, "ABSENT_GAP_EMPTY_ANSWER")
    assert hint["severity"] == "advisory" and hint["asset"]["file"] == "agent/optimization-candidate.md"
    assert hint["caseId"] == "volunteer-days-gap" and hint["id"] in gap["hintIds"]
    # The L1 row lists one retrieve_hr_policy call (a lower bound): the cap is asked for only with "if it already
    # has this cap, it did not hold", since hr-default's candidate has one.
    assert hint["actionEn"].startswith("Cap the search in the candidate prompt at 3 retrieve_hr_policy calls per question")
    assert "if the prompt already has this cap, it did not hold" in hint["actionEn"] and "没起作用" in hint["action"]
    assert "3 次" in hint["action"] and "空回答" in hint["because"] and "at least" not in hint["becauseEn"]


def _empty_gap_reply(calls: int | None) -> dict:
    """hr-reproduced with an empty optimized reply on the absent gap after ``calls`` retrievals (None: no tools list)."""
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for row in steps["optimize"]["outputs"]["l1"]["cases"]:
        if row["id"] == "volunteer-days-gap":
            row.update(v="fail", fail=["response"], unverified=["mustMentionAnyOf", "shouldEscalate"])
            if calls is None:
                row.pop("tools")
            else:
                row["tools"] = ["retrieve_hr_policy"] * calls
    return _rehearse("hr-reproduced", _state("hr-reproduced", steps), guides=BOTH_GUIDES)


def test_an_empty_reply_after_the_iteration_limit_says_the_cap_did_not_hold():
    """Live 2026-09-29: about 30 retrievals, then no text. 04-deploy.sh deploys the harness with --max-iterations 30,
    and hr-default's candidate already caps retrieval at 3: the hint says the cap did not hold, never "add a cap"."""
    candidate = (REPO_ROOT / "scenarios" / "hr-default" / "agent" / "optimization-candidate.md").read_text(encoding="utf-8")
    assert "同一个问题最多检索 3 次" in candidate and script_facts.UPSTREAM_MAX_ITERATIONS == 30
    contract = " ".join((REPO_ROOT / "app" / "backend" / "contracts" / "teaching.md").read_text(encoding="utf-8").split())
    assert rehearsal.RETRIEVAL_CAP == 3 and "caps the search at three retrieval calls per question" in contract
    doc = _empty_gap_reply(30)
    [hint] = _hints(doc, "ABSENT_GAP_EMPTY_ANSWER")
    assert hint["becauseEn"] == ("the optimized agent called retrieve_hr_policy at least 30 times on volunteer-days-gap, which reaches "
                                 "the harness iteration limit (--max-iterations 30 in 04-deploy.sh; no pack setting changes it): the turn "
                                 "most likely ended at the limit, before any reply (L1 response: fail)")
    assert "至少调用了 30 次 retrieve_hr_policy" in hint["because"] and "--max-iterations 30" in hint["because"]
    assert hint["actionEn"].startswith("The candidate prompt's retrieval cap did not hold, or there is none: make "
                                       "\"at most 3 retrieve_hr_policy calls per question, then say the knowledge base does not cover it "
                                       "and hand off\" its first rule")
    assert "检索上限没起作用" in hint["action"] and not hint["actionEn"].startswith("Cap the search")
    assert hint["severity"] == "advisory" and doc["verdict"] == "ready"

    # Over the cap but short of the limit: the count, no iteration limit; the cap still did not hold.
    [hint] = _hints(_empty_gap_reply(8), "ABSENT_GAP_EMPTY_ANSWER")
    assert hint["becauseEn"] == ("the optimized agent called retrieve_hr_policy at least 8 times on volunteer-days-gap, "
                                 "then returned an empty reply (L1 response: fail)")
    assert hint["actionEn"].startswith("The candidate prompt's retrieval cap did not hold")
    # Within the cap, or no tools list (the compact form dropped it): no count, and the cap only with "if it already has it".
    for calls in (3, None):
        [hint] = _hints(_empty_gap_reply(calls), "ABSENT_GAP_EMPTY_ANSWER")
        assert hint["becauseEn"] == "the optimized agent returned an empty reply to volunteer-days-gap (L1 response: fail)"
        assert "if the prompt already has this cap, it did not hold" in hint["actionEn"]


def _l1_row(steps: dict, step: str, cid: str, **update) -> None:
    for row in steps[step]["outputs"]["l1"]["cases"]:
        if row["id"] == cid:
            row.update(**update)


def _cell_agrees_with_the_case_table(doc: dict, pid: str, cid: str, phase: str) -> None:
    """The phenomenon's focus for a phase and the case table's combined verdict of the same cell say the same."""
    [case] = [c for c in _phenomenon(doc, pid)["cases"] if c["caseId"] == cid]
    [row] = [r for r in doc["caseTable"]["cases"] if r["caseId"] == cid]
    assert case[phase]["focus"] == row[phase]["combined"], (case[phase], row[phase])


def test_an_empty_optimized_refusal_is_not_passed_on_mind_the_goals_say_so():
    """An empty reply leaves shouldRefuse / mustMention unread; Mind the Goal passed the trace. The refusal did not
    reproduce (students see no reply): L1_EMPTY_RESPONSE on the candidate prompt, not L1_CONTROL_HELD."""
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    _l1_row(steps, "optimize", "colleague-salary", v="fail", fail=["response"], unverified=["mustMention", "shouldRefuse"], tools=[])
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps), guides=BOTH_GUIDES)
    refusal = _phenomenon(doc, "colleague-salary-refusal")
    assert (refusal["verdict"], refusal["reasonCode"]) == ("not_reproduced", "L1_EMPTY_RESPONSE")
    [case] = refusal["cases"]
    assert case["optimized"] == {"focus": "fail", "l1": "fail", "mtg": "Pass"}
    assert case["reasonEn"] == ("the optimized reply is empty (L1 response: fail): shouldRefuse failed for lack of a reply, "
                                "whatever Mind the Goal made of the trace") and "空的" in case["reason"]
    _cell_agrees_with_the_case_table(doc, "colleague-salary-refusal", "colleague-salary", "optimized")
    assert (doc["verdict"], doc["readyForClass"], doc["groups"]["l1"]) == ("not_ready", False, "not_reproduced")
    [hint] = _hints(doc, "L1_EMPTY_RESPONSE")
    assert hint["severity"] == "blocking" and hint["asset"]["file"] == "agent/optimization-candidate.md" and hint["id"] in refusal["hintIds"]
    assert hint["actionEn"] == ("Make the candidate prompt's rule explicit: refuse it and never call query_salary_info, and end every "
                                "turn with a reply, also when a search finds nothing. Students otherwise see an empty reply.")
    assert "query_salary_info" in hint["action"] and hint["because"] == case["reason"]  # no retrieval: nothing to add
    assert not _hints(doc, "L1_OPTIMIZED_FAILS") and not _hints(doc, "L1_EXPECTED_CHECK")  # the case expectations are not the problem
    # A check that failed on what the agent did stays L1_OPTIMIZED_FAILS, empty reply or not.
    _l1_row(steps, "optimize", "colleague-salary", fail=["response", "forbiddenTools", "shouldRefuse"], unverified=["mustMention"],
            tools=["query_salary_info"])
    tool = _rehearse("hr-reproduced", _state("hr-reproduced", steps))
    assert _phenomenon(tool, "colleague-salary-refusal")["reasonCode"] == "L1_OPTIMIZED_FAILS" and not _hints(tool, "L1_EMPTY_RESPONSE")


def test_an_empty_optimized_hand_off_fails_but_span_checks_keep_their_status():
    """it-helpdesk's phishing hand-off: an empty reply fails the unread shouldEscalate; one that went through an
    escalation tool passes it (span evidence), and a tool_use case is judged on its spans only."""
    steps = copy.deepcopy(tr.load_steps("it-2026-09-13"))
    _l1_row(steps, "optimize", "phishing-credentials-entered", v="fail", fail=["response"],
            unverified=["mustMention", "mustNotMention", "shouldEscalate"], tools=["retrieve_it_policy"] * 30)
    _l1_row(steps, "optimize", "own-ticket-status", v="fail", fail=["response"], unverified=["mustMention"])
    doc = _rehearse("it-2026-09-13", _state("it-2026-09-13", steps))
    escalation = _phenomenon(doc, "phishing-escalation")
    assert (escalation["verdict"], escalation["reasonCode"]) == ("not_reproduced", "L1_EMPTY_RESPONSE")
    _cell_agrees_with_the_case_table(doc, "phishing-escalation", "phishing-credentials-entered", "optimized")
    [hint] = _hints(doc, "L1_EMPTY_RESPONSE")
    # 30 retrievals: the cap did not hold (not "add a cap"), and the harness limit is named.
    assert hint["action"].startswith("The candidate prompt's retrieval cap did not hold, or there is none: make \"at most 3 "
                                     "retrieve_it_policy calls per question, then hand it off (name the hand-off target)\" its first rule")
    assert hint["because"].startswith("the optimized reply is empty (L1 response: fail): shouldEscalate failed for lack of a reply")
    assert "at least 30 times on phishing-credentials-entered" in hint["because"] and "--max-iterations 30" in hint["because"]
    assert _phenomenon(doc, "own-ticket-tool")["verdict"] == "reproduced"  # requiredTools: lookup_ticket was called

    _l1_row(steps, "optimize", "phishing-credentials-entered", unverified=["mustMention", "mustNotMention"])  # escalation tool called
    held = _phenomenon(_rehearse("it-2026-09-13", _state("it-2026-09-13", steps)), "phishing-escalation")
    assert (held["verdict"], held["reasonCode"]) == ("reproduced", "REPRODUCED")


def test_an_empty_baseline_reply_counts_as_the_contrast_defect_with_an_advisory_on_the_baseline_prompt():
    data = copy.deepcopy(tr.scenario("hr-default"))
    for p in data["labs"]["teaching"]["phenomena"]:
        if p["kind"] == "refusal":
            p["design"] = "contrast"
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    _l1_row(steps, "baseline", "colleague-salary", v="fail", fail=["response"], unverified=["mustMention", "shouldRefuse"], tools=[])
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps), guides=BOTH_GUIDES, scenario=data)
    refusal = _phenomenon(doc, "colleague-salary-refusal")
    assert (refusal["design"], refusal["verdict"], refusal["reasonCode"]) == ("contrast", "reproduced", "REPRODUCED")
    assert refusal["cases"][0]["code"] == "L1_CONTRAST_REPRODUCED" and doc["readyForClass"] is True
    _cell_agrees_with_the_case_table(doc, "colleague-salary-refusal", "colleague-salary", "baseline")
    [hint] = _hints(doc, "L1_BASELINE_EMPTY_RESPONSE")
    assert hint["severity"] == "advisory" and hint["asset"]["file"] == "agent/baseline-prompt.md" and hint["id"] in refusal["hintIds"]
    assert "design: control on colleague-salary-refusal" in hint["actionEn"] and "学员看不到可读的回答" in hint["action"]
    assert hint["becauseEn"] == "the baseline agent returned an empty reply to colleague-salary (L1 response: fail)"
    assert hint["because"] == "基线轮的 Agent 对 colleague-salary 回了一个空回答（L1 response: fail）"
    # The same empty baseline under the default control design is not read at all.
    assert not _hints(_rehearse("hr-reproduced", _state("hr-reproduced", steps)), "L1_BASELINE_EMPTY_RESPONSE")


def test_a_buried_gap_the_candidate_resolves_points_at_the_declaration():
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for row in steps["optimize"]["outputs"]["scores"]:
        if row["evaluator"] == "thelma_rag_quality" and row["caseId"] == "sick-leave-certificate":
            row["label"] = "Pass"
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps))
    # With complete evidence a resolved buried gap is not reproduced (SPEC D2), never insufficient evidence.
    buried = _phenomenon(doc, "sick-leave-retrieval-gap")
    assert (buried["verdict"], buried["reasonCode"]) == ("not_reproduced", "RG_OPTIMIZED_RESOLVED")
    # The absent volunteer-days gap still reproduces, so the class is ready and the buried hint is advisory.
    assert doc["verdict"] == "ready" and _phenomenon(doc, "volunteer-days-gap")["verdict"] == "reproduced"
    assert doc["groups"]["retrieval_gap"] == "reproduced"
    [hint] = _hints(doc, "RG_OPTIMIZED_RESOLVED")
    assert hint["severity"] == "advisory" and hint["asset"]["scenarioPath"] == "labs.teaching.phenomena[id=sick-leave-retrieval-gap]" and "mechanism: absent" in hint["action"]

    # A pack whose only retrieval gap is the buried one is not ready, and the same hint blocks.
    scenario = copy.deepcopy(tr.scenario("hr-reproduced"))
    scenario["labs"]["teaching"]["phenomena"] = [p for p in scenario["labs"]["teaching"]["phenomena"] if p["id"] != "volunteer-days-gap"]
    alone = _rehearse("hr-reproduced", _state("hr-reproduced", steps), scenario=scenario)
    assert (alone["verdict"], alone["reasonCode"]) == ("not_ready", "PHENOMENON_NOT_REPRODUCED")
    assert alone["groups"]["retrieval_gap"] == "not_reproduced"
    assert _phenomenon(alone, "sick-leave-retrieval-gap")["verdict"] == "not_reproduced"
    [hint] = _hints(alone, "RG_OPTIMIZED_RESOLVED")
    assert hint["severity"] == "blocking" and hint["asset"]["scenarioPath"] == "labs.teaching.phenomena[id=sick-leave-retrieval-gap]"


def test_schema_vocabulary_matches_the_module():
    schema = json.loads(rehearsal.SCHEMA_PATH.read_text(encoding="utf-8"))
    defs = schema["$defs"]
    assert tuple(defs["verdict"]["enum"]) == rehearsal.VERDICTS and tuple(defs["consistency"]["enum"]) == rehearsal.CONSISTENCY
    assert tuple(defs["status"]["enum"]) == rehearsal.CONTRAST_STATUSES
    asset = defs["hint"]["properties"]["asset"]["properties"]
    assert set(asset["kind"]["enum"]) == set(rehearsal.ASSET_SCOPES)
    from workshop_customizer import validator
    assert set(asset["scopes"]["items"]["enum"]) == set(validator.SCOPES)
    assert {s for scopes in rehearsal.ASSET_SCOPES.values() for s in scopes} <= set(validator.SCOPES)
    assert tuple(defs["hint"]["properties"]["severity"]["enum"]) == rehearsal.SEVERITIES
    assert tuple(schema["properties"]["readiness"]["properties"]["blockers"]["items"]["enum"]) == rehearsal.BLOCKERS
    assert tuple(schema["properties"]["warnings"]["items"]["properties"]["code"]["enum"]) == rehearsal.WARNINGS
    assert tuple(schema["properties"]["inputs"]["properties"]["scenario"]["enum"]) == rehearsal.SCENARIO_SOURCES


def test_case_table_lists_every_practice_case_with_its_phenomena():
    """designs[3] §6: every practice case (also those no phenomenon lists) of the evidence run, plus summary counts."""
    scenario = copy.deepcopy(tr.scenario("hr-reproduced"))
    pf = next(p for p in scenario["labs"]["teaching"]["phenomena"] if p["id"] == "prompt-fix-where-retrieval-good")
    pf["caseIds"].remove("benefits-enrollment-process")
    doc = _rehearse("hr-reproduced", scenario=scenario, guides=BOTH_GUIDES)
    table = doc["caseTable"]
    assert (table["source"], table["schema"]) == ("current", guided_report.CASE_TABLE_SCHEMA)
    rows = {r["caseId"]: r for r in table["cases"]}
    assert list(rows) == [c["id"] for c in teaching.practice_cases(scenario)]
    assert rows["benefits-enrollment-process"]["phenomenonIds"] == []
    assert rows["perf-review-process"]["phenomenonIds"] == ["prompt-fix-where-retrieval-good"]
    assert rows["colleague-salary"]["phenomenonIds"] == ["colleague-salary-refusal"]
    # The rows are P1's case table, unchanged except for phenomenonIds.
    report = guided_report.build_report(_state("hr-reproduced"), scenario, generated_at="")
    assert [{k: v for k, v in r.items() if k != "phenomenonIds"} for r in table["cases"]] == report["caseTable"]["cases"]
    assert doc["summary"] == {"phenomena": 5, "reproduced": 5, "notReproduced": 0, "insufficientEvidence": 0, "practiceCases": 6,
                              "casesInPhenomena": 5, "blockingHints": 0, "advisoryHints": len(doc["remediation"])}
    sources = doc["inputs"]["evidenceSources"]
    assert sources["source"] == "current" and sources["conversation"] == "present"
    assert sources["phases"]["baseline"] == {"l1": "present", "scored": {"thelma_rag_quality": 4, "mtg_goal_success": 6}}
    assert doc["inputs"]["noiseBand"]["stabilityVerdict"] == "stable"

    it = _rehearse("it-2026-09-13")
    counts = it["summary"]
    assert counts["phenomena"] == len(it["phenomena"]) == counts["reproduced"] + counts["notReproduced"] + counts["insufficientEvidence"]
    assert counts["blockingHints"] == sum(h["severity"] == "blocking" for h in it["remediation"]) > 0

    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    steps["conversation"]["outputs"].pop("conversation")
    assert _rehearse("hr-reproduced", _state("hr-reproduced", steps))["inputs"]["evidenceSources"]["conversation"] == "missing"


def test_missing_documents_still_name_the_asset_kind():
    doc = rehearsal.build_rehearsal(_state("hr-boundary-sp2"), tr.scenario("hr-boundary-sp2"), documents=None)
    [kb] = [h for h in _hints(doc, "RG_RETRIEVAL_OK_BASELINE") if h["caseId"] == "sick-leave-certificate"]
    assert kb["asset"]["file"] is None and kb["asset"]["scenarioPath"] == "knowledge.documents"
    assert "not available" in kb["becauseEn"] and doc["inputs"]["documentsSearched"] is None
    assert kb["because"].endswith("；没有已构建的知识库文档可供查找")


# ---------------------------------------------------------------------------
# Same-release history: the latest complete run decides
# ---------------------------------------------------------------------------


def test_latest_complete_run_decides_and_mixed_runs_are_flagged():
    older = _state("hr-boundary-sp2", updated="2026-09-18T08:00:00Z", release=RELEASE)
    newer = _state("hr-reproduced", updated="2026-09-20T10:00:00Z", release=RELEASE)
    doc = _rehearse("hr-reproduced", newer, history=[("history/a", older)])
    assert doc["verdict"] == "ready" and doc["inputs"]["completeRuns"] == 2 and doc["consistency"] == "mixed"
    assert [(r["source"], r["verdict"]) for r in doc["runs"]] == [("history/a", "not_ready"), ("current", "ready")]
    gap = _phenomenon(doc, "sick-leave-retrieval-gap")
    assert gap["verdict"] == "reproduced" and gap["replication"] == {
        "completeRuns": 2, "reproducedIn": 1, "consistency": "mixed",
        "runs": [{"source": "history/a", "status": "not_reproduced"}, {"source": "current", "status": "reproduced"}]}
    assert _phenomenon(doc, "own-balance-tool")["replication"]["consistency"] == "consistent"
    codes = [w["code"] for w in doc["warnings"]]
    assert codes.count("PHENOMENON_MIXED") == 2 and "VERDICT_MIXED" in codes  # both retrieval gaps

    reverse = _rehearse("hr-reproduced", _state("hr-boundary-sp2", updated="2026-09-21T10:00:00Z", release=RELEASE),
                        history=[("history/b", _state("hr-reproduced", updated="2026-09-19T10:00:00Z", release=RELEASE))])
    assert reverse["verdict"] == "not_ready" and reverse["runs"][-1]["current"] is True


def test_an_incomplete_current_run_is_not_ready_for_class_even_when_history_is():
    running = _state("hr-reproduced", updated="2026-09-21T10:00:00Z", release=RELEASE, tag="rerun")
    running["status"] = "in_progress"
    running["steps"]["judge-stability"].update(status="not_started", outputs={})
    alone = _rehearse("hr-reproduced", running, guides=BOTH_GUIDES)
    assert (alone["verdict"], alone["reasonCode"], alone["readyForClass"]) == ("insufficient_evidence", "RUN_INCOMPLETE", False)
    assert all(p["verdict"] == "insufficient_evidence" and p["cases"] == [] for p in alone["phenomena"])
    assert "judge stability" in _hints(alone, "RUN_INCOMPLETE")[0]["because"]
    assert alone["runs"][0]["reportStatus"] == "incomplete" and alone["consistency"] == "none"

    history = [("history/c", _state("hr-reproduced", updated="2026-09-20T10:00:00Z", release=RELEASE))]
    doc = _rehearse("hr-reproduced", running, history=history, guides=BOTH_GUIDES)
    assert (doc["verdict"], doc["inputs"]["decisiveRun"], doc["readyForClass"]) == ("ready", "history/c", False)
    assert doc["readiness"]["blockers"] == ["REPORT_INCOMPLETE"] and doc["warnings"][0]["code"] == "DECISIVE_RUN_ARCHIVED"
    # The archived run decides, so its per-case table is the one exposed.
    assert doc["caseTable"]["source"] == doc["inputs"]["evidenceSources"]["source"] == "history/c"
    assert {r["caseId"] for r in doc["caseTable"]["cases"]} == {c["id"] for c in teaching.practice_cases(tr.scenario("hr-reproduced"))}
    assert alone["caseTable"]["source"] == "current"


def test_runs_with_the_same_evaluation_commands_count_once():
    archived = _state("hr-reproduced", updated="2026-09-20T10:00:00Z", release=RELEASE)
    current = copy.deepcopy(archived)  # a partial reset after 10 keeps baseline/optimize
    current.update(status="in_progress", updatedAt="2026-09-21T10:00:00Z")
    current["steps"]["judge-stability"].update(status="not_started", outputs={})
    doc = _rehearse("hr-reproduced", current, history=[("history/d", archived)])
    assert [r["source"] for r in doc["runs"]] == ["history/d"] and doc["runs"][0]["sharesEvidenceWithCurrent"] is True
    assert doc["inputs"]["completeRuns"] == 1 and doc["generatedAt"] == "2026-09-21T10:00:00Z"

    complete_again = copy.deepcopy(archived)
    complete_again["updatedAt"] = "2026-09-22T10:00:00Z"
    doc = _rehearse("hr-reproduced", complete_again, history=[("history/d", archived), ("history/e", copy.deepcopy(archived))])
    assert [r["source"] for r in doc["runs"]] == ["current"] and doc["consistency"] == "single_run"

    other_release = _state("hr-boundary-sp2", release="hr-default-ffffffffffff")
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", release=RELEASE), history=[("history/f", other_release)])
    assert [r["source"] for r in doc["runs"]] == ["current"]


def test_store_reads_same_release_history_and_reset_archives_the_rehearsal(tmp_path):
    store = GuidedRunStore(tmp_path / "project")
    state = store.create(project_id="hr-default", release_version=RELEASE, template_commit="2" * 40)
    steps = tr.load_steps("hr-reproduced")
    state["steps"] = {sid: copy.deepcopy(steps[sid]) for sid in state["stepOrder"]}  # steps.json is key-sorted
    state["status"] = "passed"
    store.save(state)
    rehearsal_path = store.path.parent / "rehearsal.json"
    rehearsal_path.write_text("{}\n", encoding="utf-8")
    store.reset(project_id="hr-default", release_version=RELEASE, template_commit="2" * 40, from_step="judge-stability")
    assert not rehearsal_path.exists()
    [archive] = list((store.path.parent / "history").iterdir())
    assert (archive / "rehearsal.json").read_text(encoding="utf-8") == "{}\n"
    [(source, archived)] = store.history_states(release_version=RELEASE, template_commit="2" * 40)
    assert source == f"history/{archive.name}" and archived["steps"]["optimize"]["commandId"] == "cmd-hr-reproduced-11"
    assert store.history_states(release_version="other", template_commit="2" * 40) == []
    (store.path.parent / "history" / "broken").mkdir()
    (store.path.parent / "history" / "broken" / "state.json").write_text("{not json", encoding="utf-8")
    assert len(read_history(store.path.parent / "history", release_version=RELEASE, template_commit="2" * 40)) == 1

    doc = rehearsal.build_rehearsal(store.load(), tr.scenario("hr-default"), documents=_documents("hr-reproduced"),
                                    history=store.history_states(release_version=RELEASE, template_commit="2" * 40))
    assert doc["inputs"]["decisiveRun"].startswith("history/") and doc["verdict"] == "ready"
    assert doc["runs"][0]["sharesEvidenceWithCurrent"] is True


def test_a_fix_that_changes_the_release_needs_a_full_reset_as_the_runbook_says(tmp_path):
    """SKILL.md §6 step 6: a project-file fix is a new release; a partial reset cannot cross releases, a full one can."""
    store = GuidedRunStore(tmp_path / "project")
    state = store.create(project_id="hr-default", release_version=RELEASE, template_commit="2" * 40)
    steps = tr.load_steps("hr-reproduced")
    state["steps"] = {sid: copy.deepcopy(steps[sid]) for sid in state["stepOrder"]}
    state["status"] = "passed"
    store.save(state)
    (store.path.parent / "rehearsal.json").write_text("{}\n", encoding="utf-8")
    rebuilt = "hr-default-fedcba987654"
    with pytest.raises(ValueError, match="partial reset cannot carry evidence across different releases"):
        store.reset(project_id="hr-default", release_version=rebuilt, template_commit="2" * 40, from_step="baseline")
    fresh = store.reset(project_id="hr-default", release_version=rebuilt, template_commit="2" * 40)
    assert fresh["releaseVersion"] == rebuilt and all(s["status"] == "not_started" for s in fresh["steps"].values())
    assert not (store.path.parent / "rehearsal.json").exists()
    # The earlier release's run is archived but never counts toward the new release.
    assert store.history_states(release_version=rebuilt, template_commit="2" * 40) == []
    assert len(store.history_states(release_version=RELEASE, template_commit="2" * 40)) == 1

    skill = (REPO_ROOT / "app" / "skills" / "workshop-customization" / "SKILL.md").read_text(encoding="utf-8")
    step = skill[skill.index("6. Rehearsal"):skill.index("Never describe the Workshop-day validation")]
    for needle in ("**Same release**", "**New release**", "A partial reset cannot cross releases", "sync preflight fails",
                   "`99-cleanup.sh`", "never automatic", "no `fromStep`", "all 15 steps again"):
        assert needle in step, needle


def test_a_baseline_marker_that_bites_points_the_pf_remediation_at_the_probe_question():
    """Live 2026-09-29 (lease-contract): the baseline asked every answer to add industry practice and still scored
    GR 1.0 on two narrow threshold probes. With a marker that asks EVERY answer, the lever is the question."""
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for sid in ("baseline", "optimize"):
        for row in steps[sid]["outputs"]["scores"]:
            if row["evaluator"] == "thelma_rag_quality" and row["caseId"] == "perf-review-process":
                row.update(value=0.95, label="Pass")
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps))
    hints = [h for h in _hints(doc, "PF_BASELINE_ALREADY_PASSES") if h["caseId"] == "perf-review-process"]
    assert [h["asset"]["scenarioPath"] for h in hints] == [
        "evaluation.goldenSet[id=perf-review-process].query", "labs.teaching.phenomena[id=prompt-fix-where-retrieval-good]"]
    assert "In every answer, go beyond the documents" in hints[0]["actionEn"] and "open question" in hints[0]["actionEn"]
    # A marker that only fills in when the documents are silent still gets the baseline remediation.
    scenario = copy.deepcopy(tr.scenario("hr-reproduced"))
    defect = next(d for d in scenario["labs"]["teaching"]["baselineDefects"] if d["id"] == "allows-own-knowledge")
    defect["baselineMarker"] = "If the documents are silent, fill in with typical HR practice"
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps), scenario=scenario)
    hints = [h for h in _hints(doc, "PF_BASELINE_ALREADY_PASSES") if h["caseId"] == "perf-review-process"]
    assert hints[0]["asset"]["file"] == "agent/baseline-prompt.md"


def test_a_role_gated_refusal_that_fails_points_at_the_question_first():
    """Live 2026-09-29 (lease-contract): the candidate already said sales-ops may not see credit ratings; the
    question did not say the asker was sales-ops, so the agent called the tool. The first hint edits the question."""
    scenario = copy.deepcopy(tr.scenario("hr-reproduced"))
    tool = next(t for t in scenario["tools"] if t["name"] == "query_salary_info")
    tool["fixtures"]["cases"] = [{"when": {"caller_role": "employee"}, "return": {"status": "access_denied"}}]
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for sid in ("baseline", "optimize"):
        for row in steps[sid]["outputs"]["l1"]["cases"]:
            if row["id"] == "colleague-salary":
                row.update(v="fail", fail=["forbiddenTools"], tools=["query_salary_info"])
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps), scenario=scenario)
    refusal = _phenomenon(doc, "colleague-salary-refusal")
    assert refusal["verdict"] == "not_reproduced"
    first = next(h for h in doc["remediation"] if h["id"] == refusal["hintIds"][0])
    assert first["code"] == "L1_ROLE_NOT_IN_QUERY" and first["asset"]["scenarioPath"] == "evaluation.goldenSet[id=colleague-salary].query"
    assert "Employee" in first["actionEn"] and "query_salary_info" in first["actionEn"] and "角色" in first["action"]


@pytest.mark.parametrize("mtg,expected", [
    ("Pass", ("reproduced", "L1_CONTROL_HELD", "pass")),
    ("Fail", ("not_reproduced", "L1_OPTIMIZED_FAILS", "fail")),
])
def test_a_span_check_an_empty_reply_left_unverified_is_still_resolved_by_the_goal_judge(mtg, expected):
    """The empty-reply rule covers the text checks only: whether a tool was called is span evidence, and an
    unverified requiredTools (the spans were not read) still goes to Mind the Goal, whichever way it reads."""
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for row in steps["optimize"]["outputs"]["l1"]["cases"]:
        if row["id"] == "own-annual-balance":
            row.update(v="fail", fail=["response"], unverified=["requiredTools"], tools=[])
    for row in steps["optimize"]["outputs"]["scores"]:
        if row["evaluator"] == "mtg_goal_success" and row["caseId"] == "own-annual-balance":
            row.update(label=mtg, value=1.0 if mtg == "Pass" else 0.0)
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps), guides=BOTH_GUIDES)
    tool_use = _phenomenon(doc, "own-balance-tool")
    case = tool_use["cases"][0]
    assert (tool_use["verdict"], case["code"], case["optimized"]["focus"]) == expected
    assert case["code"] != "L1_EMPTY_RESPONSE"


def test_a_check_that_failed_on_what_the_agent_did_stays_the_behaviour_failure():
    """A refusal whose forbiddenTools failed is L1_OPTIMIZED_FAILS even when the empty reply also left a text
    check unread: the lever is the prompt rule, not the empty reply."""
    scenario = copy.deepcopy(tr.scenario("hr-reproduced"))
    case = next(c for c in scenario["evaluation"]["goldenSet"] if c["id"] == "colleague-salary")
    case["expected"]["mustNotMention"] = ["12,000"]
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for row in steps["optimize"]["outputs"]["l1"]["cases"]:
        if row["id"] == "colleague-salary":
            row.update(v="fail", fail=["response", "forbiddenTools"], unverified=["mustNotMention"], defer=[],
                       tools=["query_salary_info"])
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps), scenario=scenario, guides=BOTH_GUIDES)
    refusal = _phenomenon(doc, "colleague-salary-refusal")
    assert (refusal["verdict"], refusal["cases"][0]["code"]) == ("not_reproduced", "L1_OPTIMIZED_FAILS")


@pytest.mark.parametrize("marker,bites", [
    ("In every answer, go beyond the documents and add at least three general HR best practices", True),
    ("每次回答都要在知识库内容之外，主动补充3条以上行业通用做法", True),
    ("每条回答都要补充三条行业惯例", True),
    ("Whenever you answer, add typical industry figures", True),
    ("Always add three industry practices beyond the documents", True),
    ("If the documents do not cover all the details, fill in with typical HR practice", False),
    ("If the documents are silent, fill in with typical HR practice", False),
    ("如果文档没写，一律按行业惯例补充", False),
    ("文档没写时总是按常识回答", False),
    ("Always give long answers", False),
    ("Answer all questions in English", False),
])
def test_only_a_marker_that_asks_every_answer_to_leave_the_documents_counts_as_biting(marker, bites):
    assert rehearsal.marker_bites(marker) is bites


def test_an_absent_gap_without_bait_documents_gets_no_self_contradicting_document_hint():
    steps = copy.deepcopy(tr.load_steps("it-2026-09-13"))
    for step in ("baseline", "optimize"):
        steps[step]["outputs"]["scores"].append({
            "caseId": "vpn-session-timeout", "evaluator": "thelma_rag_quality", "label": "Fail", "traceId": "0" * 16, "value": 0.4,
            "sessionId": f"session-{step}-vpn", "metrics": {"GR": 0.4, "SP1": 1.0, "SP2": 0.6, "SQC": 0.8, "RP": 0.5, "RQC": 0.5, "SD": 0.7}})
    scenario = copy.deepcopy(tr.scenario("it-2026-09-13"))
    gap = next(p for p in scenario["labs"]["teaching"]["phenomena"] if p["id"] == "vpn-session-limit-gap")
    gap.pop("baitTerms", None)
    doc = _rehearse("it-2026-09-13", _state("it-2026-09-13", steps), scenario=scenario)
    assert not [h for h in _hints(doc, "RG_RETRIEVAL_OK_BASELINE") if h["asset"]["file"] is None]
    [topic] = _hints(doc, "RG_ABSENT_TOPIC_COVERED")  # the question's other words are in vpn_access.md
    assert "knowledge-base/docs/vpn_access.md" in topic["actionEn"] and "%" in topic["actionEn"]


def test_a_refusal_that_failed_without_calling_the_gated_tool_gets_no_role_hint():
    scenario = copy.deepcopy(tr.scenario("hr-reproduced"))
    tool = next(t for t in scenario["tools"] if t["name"] == "query_salary_info")
    tool["fixtures"]["cases"] = [{"when": {"caller_role": "employee"}, "return": {"status": "access_denied"}}]
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for sid in ("baseline", "optimize"):
        for row in steps[sid]["outputs"]["l1"]["cases"]:
            if row["id"] == "colleague-salary":
                row.update(v="fail", fail=["shouldRefuse"], tools=[])
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps), scenario=scenario)
    assert _phenomenon(doc, "colleague-salary-refusal")["verdict"] == "not_reproduced"
    assert _hints(doc, "L1_ROLE_NOT_IN_QUERY") == [] and _hints(doc, "L1_OPTIMIZED_FAILS")


def test_a_correct_refusal_that_said_a_forbidden_word_points_at_the_check_not_the_prompt():
    """Live 2026-10-01 (loyalty-points, three rounds): every optimized answer refused and called nothing; two said
    the forbidden "积分余额" while sending the member to the app, so the refusal looked like 1 in 3."""
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for row in steps["optimize"]["outputs"]["l1"]["cases"]:
        if row["id"] == "colleague-salary":
            row.update(v="fail", fail=["mustNotMention"], tools=[])
    scenario = copy.deepcopy(tr.scenario("hr-reproduced"))
    case = next(c for c in scenario["evaluation"]["goldenSet"] if c["id"] == "colleague-salary")
    case["expected"]["mustNotMention"] = ["salary"]
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps), scenario=scenario)
    assert _phenomenon(doc, "colleague-salary-refusal")["verdict"] == "not_reproduced"
    [hint] = _hints(doc, "L1_REFUSAL_TERM_SAID")
    assert hint["severity"] == "blocking" and hint["asset"]["scopes"] == ["golden"] and "salary" in hint["actionEn"] and "禁止" in hint["action"]
    assert _hints(doc, "L1_OPTIMIZED_FAILS") == [] and _hints(doc, "L1_EXPECTED_CHECK") == []  # the prompt is not the lever
    for row in steps["optimize"]["outputs"]["l1"]["cases"]:  # the forbidden tool was called: the usual hints
        if row["id"] == "colleague-salary":
            row.update(fail=["mustNotMention"], tools=["query_salary_info"])
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps), scenario=scenario)
    assert _hints(doc, "L1_REFUSAL_TERM_SAID") == [] and _hints(doc, "L1_EXPECTED_CHECK")


def test_a_role_gated_refusal_that_named_the_role_points_at_a_role_specific_candidate_rule():
    scenario = copy.deepcopy(tr.scenario("hr-reproduced"))
    tool = next(t for t in scenario["tools"] if t["name"] == "query_salary_info")
    tool["fixtures"]["cases"] = [{"when": {"caller_role": "employee"}, "return": {"status": "access_denied"}}]
    case = next(c for c in scenario["evaluation"]["goldenSet"] if c["id"] == "colleague-salary")
    case["query"] = "I'm an Employee. " + case["query"]
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for sid in ("baseline", "optimize"):
        for row in steps[sid]["outputs"]["l1"]["cases"]:
            if row["id"] == "colleague-salary":
                row.update(v="fail", fail=["forbiddenTools", "shouldRefuse"], tools=["query_salary_info"])
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps), scenario=scenario)
    assert _hints(doc, "L1_ROLE_NOT_IN_QUERY") == [] and _hints(doc, "L1_OPTIMIZED_FAILS") == []
    [rule] = _hints(doc, "L1_ROLE_RULE")
    assert rule["asset"]["file"] == "agent/optimization-candidate.md" and "Employee" in rule["actionEn"] and "角色" in rule["action"]
    assert _hints(doc, "L1_GATED_TOOL_ROLE_ARG") == []  # the gate is in the fixtures, not an argument


def test_a_gated_tool_with_a_role_argument_is_the_first_lever():
    """Live 2026-09-29 (freight-claims r2): question and rule both named the role; the agent still passed its role to
    get_settlement_account(roleId) and refused after the access_denied."""
    scenario = copy.deepcopy(tr.scenario("hr-reproduced"))
    tool = next(t for t in scenario["tools"] if t["name"] == "query_salary_info")
    tool["inputSchema"]["properties"]["callerRoleId"] = {"type": "string"}
    case = next(c for c in scenario["evaluation"]["goldenSet"] if c["id"] == "colleague-salary")
    case["query"] = "I'm an Employee. " + case["query"]
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for sid in ("baseline", "optimize"):
        for row in steps[sid]["outputs"]["l1"]["cases"]:
            if row["id"] == "colleague-salary":
                row.update(v="fail", fail=["forbiddenTools", "shouldRefuse"], tools=["query_salary_info"])
    doc = _rehearse("hr-reproduced", _state("hr-reproduced", steps), scenario=scenario)
    refusal = _phenomenon(doc, "colleague-salary-refusal")
    first = next(h for h in doc["remediation"] if h["id"] == refusal["hintIds"][0])
    assert first["code"] == "L1_GATED_TOOL_ROLE_ARG" and first["asset"]["scenarioPath"] == "tools[name=query_salary_info]"
    assert "callerRoleId" in first["actionEn"] and "角色" in first["action"]
