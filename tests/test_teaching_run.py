"""The RunStep step-output parser (SPEC D7) and the report views over it (SPEC D8), on the canonical fixture.

tests/fixtures/teaching_run/ holds the raw host output of real rendered 06/09/10 runs (fake AgentCore,
real L1) plus the parsed step records; see its manifest.json. Everything here is offline.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import teaching_run as tr
from workshop_customizer import guided_report, teaching
from workshop_customizer.guided_execution import MALFORMED_OUTPUT, parse_invocation

SSM_OUTPUT_CAP = 24000
#: SPEC D7: the one budget the printed step output stays within (bytes, so characters too).
STEP_OUTPUT_BUDGET = 22000
THELMA_METRICS = {"GR", "SP1", "SP2", "SQC", "RP", "RQC", "SD"}


def _payload(line: str) -> dict:
    return json.loads(line)


def _sessions(run: str, step: str) -> dict[str, str]:
    rows = [l.split("\t") for l in tr.raw(run, f"{step}.sessions.tsv").splitlines()]
    return {r[1]: r[2] for r in rows}


# ---------------------------------------------------------------------------
# The committed fixture
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("run", tr.RUNS)
def test_parsed_fixture_steps_are_fresh(run, tmp_path):
    steps = tr.parse_run(run, tmp_path)
    if os.environ.get("WSC_UPDATE_TEACHING_RUN") == "1":
        tr.write_steps(run, steps)
    assert steps == tr.load_steps(run), (
        f"tests/fixtures/teaching_run/{run}/steps.json is stale: rerun with WSC_UPDATE_TEACHING_RUN=1"
    )


@pytest.mark.parametrize("run", tr.RUNS)
def test_every_fixture_score_is_attributed_to_its_case_and_session(run):
    steps = tr.load_steps(run)
    data = tr.scenario(run)
    practice = [c["id"] for c in teaching.practice_cases(data)]
    probes = set(teaching.probe_case_ids(data))
    for step in ("baseline", "optimize"):
        outputs = steps[step]["outputs"]
        sessions = _sessions(run, step)
        assert set(sessions) == set(practice)
        for row in outputs["scores"]:
            assert row["caseId"] in practice and row["sessionId"] == sessions[row["caseId"]], row
            if row["evaluator"] == "thelma_rag_quality":
                assert row["caseId"] in probes and set(row["metrics"]) == THELMA_METRICS
            else:
                assert row["evaluator"] == "mtg_goal_success" and "metrics" not in row
        assert {r["caseId"] for r in outputs["scores"] if r["evaluator"] == "mtg_goal_success"} == set(practice)
        compact = json.loads(tr.raw(run, f"{step}.l1-compact.json"))
        assert outputs["l1"] == compact and compact["phase"] == ("baseline" if step == "baseline" else "optimized")
        assert outputs["payloadChars"] < SSM_OUTPUT_CAP and outputs["payloadBytes"] < SSM_OUTPUT_CAP
        assert steps[step]["documentVersion"] == tr.DOCUMENT_VERSION
    conversation = steps["conversation"]["outputs"]["conversation"]
    assert conversation["schema"] == "workshop-customizer/conversation/1"
    assert conversation["sessionId"].startswith("session-") and conversation["response"] and conversation["truncated"] is False


def test_it_fixture_keeps_the_2026_09_13_numbers():
    rows = {(s["caseId"], s["evaluator"]): s for s in tr.load_steps("it-2026-09-13")["baseline"]["outputs"]["scores"]}
    vpn = rows[("personal-device-vpn", "thelma_rag_quality")]
    assert (vpn["value"], vpn["metrics"]["SP2"], vpn["metrics"]["SQC"]) == (0.941, 0.46, 0.6)
    p2 = rows[("p2-response-time", "thelma_rag_quality")]
    assert (p2["value"], p2["metrics"]["SP2"], p2["metrics"]["SQC"]) == (0.5, 0.45, 0.5)
    assert ("lockout-duration", "thelma_rag_quality") not in rows  # answered without retrieving
    optimized = {s["caseId"]: s for s in tr.load_steps("it-2026-09-13")["optimize"]["outputs"]["scores"] if s["evaluator"] == "thelma_rag_quality"}
    assert optimized["p2-response-time"]["value"] == 0.286 and optimized["p2-response-time"]["traceId"] == "6aa63df57bee9ea6"


@pytest.mark.parametrize("run", tr.RUNS)
def test_fixture_report_and_teaching_contrast(run):
    report = guided_report.build_report(tr.run_state(run), tr.scenario(run), generated_at="2026-09-27T00:00:00Z")
    expect = tr.MANIFEST["runs"][run]["expect"]
    assert report["status"] == "complete" and report["completion"]["missingEvidence"] == []
    contrast = report["teachingContrast"]
    assert contrast["currentRun"] == expect["currentRun"] and contrast["groups"] == expect["groups"]
    assert contrast["thresholds"] == dict(teaching.THRESHOLDS)

    def no_booleans(value):
        if isinstance(value, bool):
            return False
        if isinstance(value, dict):
            return all(no_booleans(v) for v in value.values())
        if isinstance(value, list):
            return all(no_booleans(v) for v in value)
        return True

    assert no_booleans(contrast), "teachingContrast is a view: readiness booleans live only in run/rehearsal.json"
    assert "readyForClass" not in json.dumps(report)
    assert [s["documentVersion"] for s in report["stepEvidence"]] == [tr.DOCUMENT_VERSION] * 15


def test_hr_reproduced_contrast_details():
    report = guided_report.build_report(tr.run_state("hr-reproduced"), tr.scenario("hr-reproduced"), generated_at="x")
    phenomena = {p["id"]: p for p in report["teachingContrast"]["phenomena"]}
    perf = next(c for c in phenomena["prompt-fix-where-retrieval-good"]["cases"] if c["caseId"] == "perf-review-process")
    assert perf["code"] == "PF_REPRODUCED" and perf["delta"] == 0.4 and perf["baseline"]["GR"] == 0.45
    gap = phenomena["sick-leave-retrieval-gap"]
    assert gap["mechanism"] == "buried" and gap["cases"][0]["code"] == "RG_REPRODUCED"
    assert gap["cases"][0]["optimized"]["label"] == "Fail"
    table = {row["caseId"]: row for row in report["caseTable"]["cases"]}
    assert [row["caseId"] for row in report["caseTable"]["cases"]] == [c["id"] for c in teaching.practice_cases(tr.scenario("hr-default"))]
    assert table["sick-leave-certificate"]["evalIndex"] == 4 and table["sick-leave-certificate"]["probe"] is True
    perf_row = table["perf-review-process"]
    assert perf_row["baseline"]["l1"]["verdict"] == "fail" and perf_row["baseline"]["l1"]["fail"] == ["mustMention"]
    assert perf_row["optimized"]["combined"] == "pass" and perf_row["baseline"]["combined"] == "fail"
    assert perf_row["baseline"]["thelma"]["metrics"]["SP2"] == 0.62 and perf_row["baseline"]["mtg"]["label"] == "Pass"
    assert table["own-annual-balance"]["baseline"]["thelma"] is None and table["own-annual-balance"]["kinds"] == ["tool_use"]
    assert report["caseTable"]["phases"]["comparison"]["l1"] == "missing"
    assert report["quality"]["l1"]["baseline"]["total"] == 6
    absent = phenomena["volunteer-days-gap"]["cases"][0]
    assert phenomena["volunteer-days-gap"]["mechanism"] == "absent" and absent["code"] == "RG_REPRODUCED"
    assert absent["honesty"] == "admits the gap"
    memory = next(a for a in report["teachingContrast"]["advisory"] if a["kind"] == "memory")
    assert memory["evidence"] == "present" and memory["reading"] == "unknown"  # hr-default declares no mustNotMention


def test_it_fixture_explains_why_nothing_reproduced():
    report = guided_report.build_report(tr.run_state("it-2026-09-13"), tr.scenario("it-2026-09-13"), generated_at="x")
    cases = {c["caseId"]: c for p in report["teachingContrast"]["phenomena"] for c in p["cases"]}
    assert cases["personal-device-vpn"]["code"] == "PF_BASELINE_ALREADY_PASSES"
    assert cases["p2-response-time"]["code"] == "PF_NO_GAIN" and cases["p2-response-time"]["delta"] == -0.214
    assert cases["lockout-duration"]["code"] == "PF_NOT_SCORED"
    assert cases["vpn-session-timeout"]["code"] == "RG_NOT_SCORED" and cases["vpn-session-timeout"]["honesty"] == "admits the gap"
    memory = next(a for a in report["teachingContrast"]["advisory"] if a["kind"] == "memory")
    assert memory["mustNotMention"] == ["NWR-4471"] and memory["reading"] == "generic"


def test_boundary_sp2_just_above_the_bound_is_not_a_retrieval_failure():
    report = guided_report.build_report(tr.run_state("hr-boundary-sp2"), tr.scenario("hr-default"), generated_at="x")
    gap = next(p for p in report["teachingContrast"]["phenomena"] if p["kind"] == "retrieval_gap")
    case = gap["cases"][0]
    assert case["code"] == "RG_RETRIEVAL_OK_BASELINE" and (case["baseline"]["SP2"], case["baseline"]["SQC"]) == (0.25, 0.35)
    assert "SP2 0.25" in case["reason"] and "SP2 ≤ 0.2" in case["reason"]  # hr-default is zh-CN
    assert "SP2 0.25" in case["reasonEn"] and "SP2 <= 0.2" in case["reasonEn"]


@pytest.mark.parametrize("sp2, sqc, failed", [(0.2, 0.9, True), (0.25, 0.35, False), (0.25, 0.3, False), (0.25, 0.29, True)])
def test_retrieval_gap_bounds_follow_the_threshold_table(sp2, sqc, failed):
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    for row in steps["baseline"]["outputs"]["scores"]:
        if row["caseId"] == "sick-leave-certificate" and row["evaluator"] == "thelma_rag_quality":
            row["metrics"].update(SP2=sp2, SQC=sqc)
    report = guided_report.build_report(tr.run_state("hr-reproduced", steps), tr.scenario("hr-default"), generated_at="x")
    gap = next(p for p in report["teachingContrast"]["phenomena"] if p["kind"] == "retrieval_gap")
    assert gap["status"] == ("reproduced" if failed else "not_reproduced")


def test_a_noisy_judge_makes_thelma_phenomena_insufficient():
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    steps["judge-stability"]["outputs"]["judgeStability"].update(std=0.2, spread=0.3)
    report = guided_report.build_report(tr.run_state("hr-reproduced", steps), tr.scenario("hr-default"), generated_at="x")
    contrast = report["teachingContrast"]
    assert contrast["noiseBand"] == 0.4 and contrast["currentRun"] == "insufficient_evidence"
    codes = {c["code"] for p in contrast["phenomena"] if p["kind"] in ("prompt_fixable", "retrieval_gap") for c in p["cases"]}
    assert codes == {teaching.JUDGE_TOO_NOISY}


def test_improvement_must_clear_the_noise_band():
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    steps["judge-stability"]["outputs"]["judgeStability"].update(std=0.1, spread=0.2)  # band 0.2
    for row in steps["optimize"]["outputs"]["scores"]:
        if row["evaluator"] == "thelma_rag_quality" and row["caseId"] in ("perf-review-process", "benefits-enrollment-process"):
            row["value"] = {"perf-review-process": 0.65, "benefits-enrollment-process": 0.7}[row["caseId"]]  # +0.20 exactly
    report = guided_report.build_report(tr.run_state("hr-reproduced", steps), tr.scenario("hr-default"), generated_at="x")
    pf = next(p for p in report["teachingContrast"]["phenomena"] if p["kind"] == "prompt_fixable")
    assert pf["status"] == "not_reproduced" and {c["code"] for c in pf["cases"]} == {"PF_NO_GAIN"}


def test_l1_contrast_design_and_mind_the_goal_resolution():
    data = copy.deepcopy(tr.scenario("hr-default"))
    for p in data["labs"]["teaching"]["phenomena"]:
        if p["kind"] == "refusal":
            p["design"] = "contrast"
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    report = guided_report.build_report(tr.run_state("hr-reproduced", steps), data, generated_at="x")
    refusal = next(p for p in report["teachingContrast"]["phenomena"] if p["kind"] == "refusal")
    assert refusal["design"] == "contrast" and refusal["cases"][0]["code"] == "L1_BASELINE_ALREADY_PASSES"

    for row in steps["baseline"]["outputs"]["l1"]["cases"]:
        if row["id"] == "colleague-salary":
            row.update(v="fail", fail=["forbiddenTools"])
    for row in steps["optimize"]["outputs"]["l1"]["cases"]:
        if row["id"] == "colleague-salary":
            row.update(v="defer", defer=["shouldRefuse"])  # resolved by the goal judge (Pass)
    report = guided_report.build_report(tr.run_state("hr-reproduced", steps), data, generated_at="x")
    case = next(p for p in report["teachingContrast"]["phenomena"] if p["kind"] == "refusal")["cases"][0]
    assert case["code"] == "L1_CONTRAST_REPRODUCED" and case["optimized"]["focus"] == "pass"

    for row in steps["optimize"]["outputs"]["scores"]:
        if row["caseId"] == "colleague-salary" and row["evaluator"] == "mtg_goal_success":
            row["label"] = "Undetermined"
    report = guided_report.build_report(tr.run_state("hr-reproduced", steps), data, generated_at="x")
    case = next(p for p in report["teachingContrast"]["phenomena"] if p["kind"] == "refusal")["cases"][0]
    assert case["code"] == "L1_UNRESOLVED" and report["teachingContrast"]["groups"]["l1"] == "insufficient_evidence"


def test_undeclared_scenarios_get_an_undeclared_view_and_no_per_case_gate():
    data = copy.deepcopy(tr.scenario("hr-default"))
    del data["labs"]["teaching"]
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    del steps["baseline"]["outputs"]["l1"]
    report = guided_report.build_report(tr.run_state("hr-reproduced", steps), data, generated_at="x")
    assert report["teachingContrast"]["currentRun"] == "undeclared" and report["status"] == "complete"
    assert all(row["probe"] is False for row in report["caseTable"]["cases"])


# ---------------------------------------------------------------------------
# Evidence gates (step_evidence_errors with the build scenario)
# ---------------------------------------------------------------------------


def test_teaching_packs_need_l1_and_mind_the_goal_for_every_practice_case():
    data = tr.scenario("hr-default")
    good = tr.load_steps("hr-reproduced")["baseline"]["outputs"]
    assert guided_report.step_evidence_errors("baseline", good, data) == []
    assert guided_report.step_evidence_errors("baseline", good) == []  # no scenario: legacy rule only

    no_l1 = {k: v for k, v in good.items() if k != "l1"}
    [message] = guided_report.step_evidence_errors("baseline", no_l1, data)
    assert message.startswith("L1 scenario assertions (every practice case) from 09-run-eval.sh") and "stale" not in message

    legacy = {"scores": [{k: v for k, v in s.items() if k not in ("caseId", "sessionId", "metrics")} for s in good["scores"]]}
    errors = guided_report.step_evidence_errors("baseline", legacy, data)
    assert any("RunStep document is probably stale" in e for e in errors)
    assert any(e.startswith("Mind the Goal verdicts for every practice case (baseline; missing: ") for e in errors)

    partial = copy.deepcopy(good)
    partial["l1"]["cases"] = [c for c in partial["l1"]["cases"] if c["id"] != "colleague-salary"]
    partial["scores"] = [s for s in partial["scores"] if not (s["caseId"] == "perf-review-process" and s["evaluator"] == "mtg_goal_success")]
    assert guided_report.step_evidence_errors("baseline", partial, data) == [
        "L1 verdicts for every practice case (baseline; missing: colleague-salary)",
        "Mind the Goal verdicts for every practice case (baseline; missing: perf-review-process)",
    ]
    truncated = copy.deepcopy(good)
    truncated["l1"].update(cases=[], truncated=True)
    assert "truncated" in guided_report.step_evidence_errors("baseline", truncated, data)[0]
    wrong_phase = tr.load_steps("hr-reproduced")["optimize"]["outputs"]
    assert "the L1 result is for phase optimized" in " ".join(guided_report.step_evidence_errors("baseline", wrong_phase, data))
    errored = {**no_l1, "l1Error": "unexpected l1 schema"}
    assert "(unexpected l1 schema)" in guided_report.step_evidence_errors("optimize", errored, data)[0]


def test_an_incomplete_teaching_run_is_reported_incomplete():
    steps = copy.deepcopy(tr.load_steps("hr-reproduced"))
    steps["optimize"]["outputs"].pop("l1")
    report = guided_report.build_report(tr.run_state("hr-reproduced", steps), tr.scenario("hr-default"), generated_at="x")
    assert report["status"] == "incomplete"
    assert report["completion"]["missingEvidence"] == ["L1 scenario assertions (every practice case) from 10-optimize-prompt.sh"]
    assert report["caseTable"]["phases"]["optimized"]["l1"] == "missing"
    row = next(r for r in report["caseTable"]["cases"] if r["caseId"] == "colleague-salary")
    assert row["optimized"]["l1"] is None and row["optimized"]["combined"] == "undetermined"


# ---------------------------------------------------------------------------
# The embedded parser itself
# ---------------------------------------------------------------------------


def test_run_record_sessions_tsv_inside_the_eval_root_is_preferred(tmp_path):
    stdout = tr.raw("hr-reproduced", "baseline.stdout.log")
    host_dir = re.search(r"^📁 Run record: (\S+)$", stdout, re.M).group(1)
    eval_root = tmp_path / "eval-runs"
    run_dir = eval_root / Path(host_dir).relative_to(tr.HOST_EVAL_ROOT)
    run_dir.mkdir(parents=True)
    rows = [l.split("\t") for l in tr.raw("hr-reproduced", "baseline.sessions.tsv").splitlines()]
    rows[2][2] = "session-from-the-run-record"  # perf-review-process
    (run_dir / "sessions.tsv").write_text("".join("\t".join(r) + "\n" for r in rows), encoding="utf-8")
    local = stdout.replace(host_dir, str(run_dir))
    scores = _payload(tr.run_parser(tmp_path / "a", local, step_id="baseline", eval_root=eval_root))["outputs"]["scores"]
    assert {s["sessionId"] for s in scores if s["caseId"] == "perf-review-process"} == {"session-from-the-run-record"}

    # Outside the eval-runs root, or a symlinked sessions.tsv: ignored, the stdout mirror decides.
    mirror = _sessions("hr-reproduced", "baseline")
    outside = tmp_path / "elsewhere"
    shutil.copytree(run_dir, outside)
    for text, root in ((stdout.replace(host_dir, str(outside)), eval_root), (local, tmp_path / "other-root")):
        scores = _payload(tr.run_parser(tmp_path / "b", text, step_id="baseline", eval_root=root))["outputs"]["scores"]
        assert all(s["sessionId"] == mirror[s["caseId"]] for s in scores)
    (run_dir / "sessions.tsv").unlink()
    (run_dir / "sessions.tsv").symlink_to(outside / "sessions.tsv")
    scores = _payload(tr.run_parser(tmp_path / "c", local, step_id="baseline", eval_root=eval_root))["outputs"]["scores"]
    assert all(s["sessionId"] == mirror[s["caseId"]] for s in scores)


def test_score_lines_without_case_tags_still_parse(tmp_path):
    stdout = re.sub(r" case=[a-z0-9-]+$", "", tr.raw("it-2026-09-13", "baseline.stdout.log"), flags=re.M)
    outputs = _payload(tr.run_parser(tmp_path, stdout, step_id="baseline"))["outputs"]
    assert len(outputs["scores"]) == 9 and not any("caseId" in s for s in outputs["scores"])
    assert outputs["scores"][0]["metrics"]["SP2"] == 0.46  # THELMA metrics do not depend on the tag


def test_compact_l1_file_is_read_only_when_valid(tmp_path):
    stdout = tr.raw("hr-reproduced", "baseline.stdout.log")
    compact = json.loads(tr.raw("hr-reproduced", "baseline.l1-compact.json"))
    cases = [
        (None, {}),
        ("{not json", {"l1Error": "unreadable l1-compact.json"}),
        ({**compact, "schema": "other/1"}, {"l1Error": "unexpected l1 schema"}),
        ({**compact, "cases": "x"}, {"l1Error": "unexpected l1 schema"}),
        (compact, {"l1": compact}),
    ]
    for i, (given, expected) in enumerate(cases):
        outputs = _payload(tr.run_parser(tmp_path / str(i), stdout, step_id="baseline", compact=given))["outputs"]
        assert {k: outputs[k] for k in ("l1", "l1Error") if k in outputs} == expected, given
    target = tmp_path / "secret.json"
    target.write_text(json.dumps(compact), encoding="utf-8")
    log_dir = tmp_path / "link"
    log_dir.mkdir()
    (log_dir / "l1-compact.json").symlink_to(target)
    outputs = _payload(tr.run_parser(log_dir, stdout, step_id="baseline"))["outputs"]
    assert outputs.get("l1Error") == "unreadable l1-compact.json" and "l1" not in outputs


def test_conversation_excerpt_strips_ansi_and_caps_the_response(tmp_path):
    stdout = tr.raw("it-2026-09-13", "conversation.stdout.log")
    long = "\x1b[32m" + "The agent answers. " * 150 + "\x1b[0m"
    text = stdout.replace(json.loads(json.dumps(tr.load_steps("it-2026-09-13")["conversation"]["outputs"]["conversation"]["response"])), long)
    conversation = _payload(tr.run_parser(tmp_path, text, step_id="conversation"))["outputs"]["conversation"]
    assert "\x1b" not in conversation["response"] and len(conversation["response"]) == 2000
    assert conversation["responseChars"] == len(long.replace("\x1b[32m", "").replace("\x1b[0m", "").strip())
    assert conversation["truncated"] is True
    outputs = _payload(tr.run_parser(tmp_path / "b", tr.raw("it-2026-09-13", "baseline.stdout.log"), step_id="baseline"))["outputs"]
    assert "conversation" not in outputs


def _cjk_stdout_with_12_cases() -> tuple[str, dict, list[str]]:
    ids = [f"case-{i:02d}-{'x' * 20}" for i in range(12)]
    lines = [("绩效福利病假知识库检索回答" * 40)[:500] for _ in range(120)]  # 60,000 CJK characters
    for i, cid in enumerate(ids):
        lines += [f"─── Q{i + 1} [标签 {i}] ───", f'    "问题 {i}"   (session: session-{i:02d}-aaaaaaaa-1789279227)',
                  f"    case: {cid}   persona: employee-001   fresh memory actor: employee-001-baseline-1-q{i + 1}"]
    lines.append("═══ THELMA (RAG quality) — 12 条 trace ═══")
    for i, cid in enumerate(ids):
        lines += [f"🔬 THELMA — trace #{i + 1} {i:016x}", f"  Score:    trace={i:016x} value=0.5 [Fail] case={cid}",
                  "     THELMA 7 维 (query: 问题): GR(接地/防幻觉)=0.5 | SP1(块级检索精度)=1.00 | SP2(事实级检索精度)=0.05 | "
                  "SQC(源覆盖)=0.20 | RP(响应精度)=0.4 | RQC(响应覆盖)=0.5 | SD(去重)=0.7. 诊断: 无"]
    lines.append("═══ Mind the Goal (GSR) — 对应 session（去重）═══")
    for i, cid in enumerate(ids):
        lines += [f"🔬 Mind the Goal — session #{i + 1} session-{i:02d}", f"  Score:    trace=? value=1 [Pass] case={cid}",
                  "     Mind the Goal: GSR=100.0% (1/1 目标达成), 轮次数=1. 失败归因: 无失败"]
    compact = {"schema": tr.L1_SCHEMA, "phase": "baseline", "runId": "baseline-1789279227", "releaseVersion": "hr-default-0123456789ab",
               "summary": {"total": 12, "pass": 6, "fail": 6, "defer": 0, "unverified": 0, "error": 0, "byCategory": {}},
               "cases": [{"id": cid, "v": "fail" if i % 2 else "pass", "fail": ["mustMention"] if i % 2 else [], "defer": [],
                          "unverified": [], "tools": ["retrieve_hr_policy"], "session": f"session-{i:02d}-aaaaaaaa-1789279227"}
                         for i, cid in enumerate(ids)], "truncated": False}
    return "\n".join(lines) + "\n", compact, ids


@pytest.mark.parametrize("rc", [0, 1])
def test_budget_keeps_60000_cjk_chars_and_12_case_evidence_under_the_ssm_cap(tmp_path, rc):
    stdout, compact, ids = _cjk_stdout_with_12_cases()
    assert sum(1 for ch in stdout if "一" <= ch <= "鿿") >= 60000
    stderr = "错误输出" * 5000
    line = tr.run_parser(tmp_path, stdout, step_id="baseline", rc=rc, stderr=stderr, compact=compact)
    assert len(line.rstrip("\n").encode("utf-8")) <= STEP_OUTPUT_BUDGET < SSM_OUTPUT_CAP
    payload = json.loads(line)
    outputs = payload["outputs"]
    assert [s["caseId"] for s in outputs["scores"]] == ids + ids
    assert all(s["sessionId"] == f"session-{i % 12:02d}-aaaaaaaa-1789279227" for i, s in enumerate(outputs["scores"]))
    assert outputs["l1"] == compact  # the evidence is never trimmed, only the tails are
    assert outputs["payloadTrimmed"] is True and outputs["payloadChars"] == len(line.rstrip("\n"))
    assert len(outputs["stdoutTail"]) <= 2000 and len(payload["summary"]) <= 2000
    full = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert len(full["outputs"]["stdoutTail"]) == 4000 and full["outputs"]["scores"] == outputs["scores"]
    result = parse_invocation(tr.invocation(line, status="Success" if rc == 0 else "Failed"))
    assert result["passed"] is (rc == 0) and len(result["outputs"]["scores"]) == 24


def test_budget_drops_l1_cases_before_giving_up_and_fails_closed_last(tmp_path):
    stdout, compact, ids = _cjk_stdout_with_12_cases()
    fat = copy.deepcopy(compact)
    for row in fat["cases"]:
        row["tools"] = ["t" * 60] * 25
    payload = _payload(tr.run_parser(tmp_path / "a", stdout, step_id="baseline", compact=fat))
    l1 = payload["outputs"]["l1"]
    assert payload["status"] == "passed" and l1["cases"] == [] and l1["truncated"] is True and l1["summary"] == fat["summary"]

    many = stdout + "\n".join(f"  Score:    trace={i:016x} value=0.5 [Fail] case=case-{i:04d}" for i in range(600)) + "\n"
    line = tr.run_parser(tmp_path / "b", many, step_id="baseline", compact=compact)
    payload = _payload(line)
    assert len(line.rstrip("\n").encode("utf-8")) <= STEP_OUTPUT_BUDGET
    assert payload["status"] == "failed" and payload["outputs"]["budgetExceeded"] is True
    assert "result.json" in payload["error"] and (tmp_path / "b" / "result.json").is_file()
    assert parse_invocation(tr.invocation(line))["passed"] is False


def _thelma_stdout(n: int, suffix: str = "") -> str:
    lines = [f"═══ THELMA (RAG quality) — {n} 条 trace ═══"]
    for i in range(n):
        lines += [f"🔬 THELMA — trace #{i + 1} {i:016x}", f"  Score:    trace={i:016x} value=0.5 [Fail] case=case-{i:03d}{suffix}",
                  "     THELMA 7 维 (query: q): GR(接地/防幻觉)=0.5 | SP1(块级检索精度)=1.00 | SP2(事实级检索精度)=0.05 | "
                  "SQC(源覆盖)=0.20 | RP(响应精度)=0.4 | RQC(响应覆盖)=0.5 | SD(去重)=0.7. 诊断: 无"]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(("n", "suffix", "fits"), [(52, "", True), (64, "", True), (91, "", True), (91, "x" * 9, True),
                                                  (97, "xxx", False)])
def test_bookkeeping_fields_count_against_the_budget(tmp_path, n, suffix, fits):
    """Reviewer regression: payloadChars/payloadBytes/payloadTrimmed were added after the budget check, so a payload
    just under 22,000 bytes was printed at 22,029 (91 rows) or 22,054 bytes (97 rows, which cannot fit at all and
    now fails closed)."""
    line = tr.run_parser(tmp_path, _thelma_stdout(n, suffix), step_id="baseline").rstrip("\n")
    payload = json.loads(line)
    outputs = payload["outputs"]
    assert len(line.encode("utf-8")) <= STEP_OUTPUT_BUDGET
    assert (outputs["payloadChars"], outputs["payloadBytes"]) == (len(line), len(line.encode("utf-8")))
    if fits:
        assert "budgetExceeded" not in outputs and len(outputs["scores"]) == n and payload["status"] == "passed"
    else:
        assert outputs["budgetExceeded"] is True and payload["status"] == "failed" and "scores" not in outputs


def test_result_json_mode_is_set_on_the_descriptor_never_by_path(tmp_path):
    """Root writes LOG_DIR/result.json into a directory the workshop user owns: a chmod by path after the file is
    closed would follow a symlink swapped in by that user."""
    source = tr.parser_source()
    assert "os.fchmod(fd, 0o644)" in source and "os.chmod(" not in source
    assert source.index("os.fchmod(fd, 0o644)") < source.index("with os.fdopen(fd, 'w'")
    tr.run_parser(tmp_path, _thelma_stdout(3), step_id="baseline")
    assert stat.S_IMODE((tmp_path / "result.json").stat().st_mode) == 0o644
    assert not [p.name for p in tmp_path.iterdir() if p.name.startswith(".result-")]


def test_output_cut_at_the_ssm_cap_is_a_failed_step(tmp_path):
    stdout, compact, _ids = _cjk_stdout_with_12_cases()
    line = tr.run_parser(tmp_path, stdout, step_id="baseline", compact=compact)
    for broken in (line[: len(line) // 2], line + "\n---Output truncated---", "[1, 2]", "plain text"):
        result = parse_invocation(tr.invocation(broken))
        assert result["passed"] is False and result["summary"] == MALFORMED_OUTPUT
        assert result["error"].startswith(MALFORMED_OUTPUT) and result["documentVersion"] == tr.DOCUMENT_VERSION


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system python3")
def test_the_parser_runs_under_the_hosts_system_python(tmp_path):
    version = subprocess.run(["/usr/bin/python3", "-c", "import sys; print(sys.version_info[:2] >= (3, 9))"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
    if version != "True":
        pytest.skip("system python3 is older than the Workshop host's 3.9")
    stdout = tr.raw("hr-reproduced", "optimize.stdout.log")
    compact = tr.raw("hr-reproduced", "optimize.l1-compact.json")
    ours = tr.run_parser(tmp_path / "a", stdout, step_id="optimize", compact=compact)
    theirs = tr.run_parser(tmp_path / "b", stdout, step_id="optimize", compact=compact, python="/usr/bin/python3")
    assert ours == theirs


def test_document_exports_the_eval_out_dir_and_clears_stale_evidence():
    commands = json.loads(tr.RUN_DOC.read_text(encoding="utf-8"))["mainSteps"][0]["inputs"]["runCommand"]
    install = next(i for i, c in enumerate(commands) if c.startswith('install -d -m 0755 -o "$TARGET_USER"'))
    assert commands[install + 1] == 'rm -f "$LOG_DIR/l1-compact.json" "$LOG_DIR/result.json"'
    [runner] = [c for c in commands if c.startswith("runuser -u \"$TARGET_USER\" -- env HOME=\"$HOME_DIR\" AWS_DEFAULT_REGION")]
    assert 'WORKSHOP_EVAL_OUT_DIR="$LOG_DIR"' in runner and "WORKSHOP_NONINTERACTIVE=1" in runner
    parser_line = next(c for c in commands if c.startswith('python3 - "$RC"'))
    assert parser_line.endswith('\'{{ StepId }}\' "$HOME_DIR/workshop/eval-runs" <<\'PY\'')
    source = tr.parser_source()
    assert "LIMIT = 22000" in source and "result.json" in source
    assert len(json.dumps(json.loads(tr.RUN_DOC.read_text(encoding="utf-8")))) < 64 * 1024


def test_the_stability_verdict_is_the_scripts_verdict_line_not_its_closing_line(tmp_path):
    """13 always ends with "✅ 稳定性检验完成"; live 2026-10-01 an unstable run ([0.0, 0.611, 0.611]) was read as stable."""
    stdout = ("  分数: [0.0, 0.611, 0.611]\n  均值=0.407  标准差=0.288  极差=0.611\n\n"
              "  ❌ 不稳：抖动明显。小模型(如 Nova 2 Lite)做 judge 容易这样——\n\n✅ 稳定性检验完成\n")
    line = tr.run_parser(tmp_path / "js", stdout, step_id="judge-stability", script="13-judge-stability.sh")
    outputs = json.loads(line)["outputs"]
    assert outputs["judgeStability"]["verdict"] == "unstable" and outputs["judgeStability"]["spread"] == 0.611
    stable = stdout.replace("❌ 不稳：抖动明显。小模型(如 Nova 2 Lite)做 judge 容易这样——", "✅ 稳定：多次打分高度一致，这个裁判在重复性上可信。")
    line = tr.run_parser(tmp_path / "js2", stable, step_id="judge-stability", script="13-judge-stability.sh")
    assert json.loads(line)["outputs"]["judgeStability"]["verdict"] == "stable"
