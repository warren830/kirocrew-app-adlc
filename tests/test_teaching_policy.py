"""P1 teaching policy: the ``teaching.*`` / ``tools.retrieval_marker_collision`` rules and the reference
packs that satisfy them (offline, deterministic). The generation contract is tested in
test_teaching_contract.py."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
from pathlib import Path

import pytest
import yaml

from workshop_customizer import l1, teaching, teaching_policy, validator
from workshop_customizer.scenario import load_scenario, validate_schema

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKS = ("hr-default", "it-helpdesk", "maintenance")

#: Every advisory finding a reference pack is allowed to carry (code, path). Anything else fails.
PINNED_WARNINGS = {
    "hr-default": {
        # The upstream sick-leave gap stays buried (kept for fidelity); the absent volunteer-days gap is the
        # reliable one since the 2026-09-27 control run (docs/replace-generic/evidence/live-hr-default-2026-09-27).
        ("teaching.bait_weak", "labs.teaching.phenomena.sick-leave-retrieval-gap"),  # one 'Sick Leave' FAQ line
        ("teaching.probe_fixture_value", "evaluation.goldenSet.sick-leave-certificate"),  # 'sick' is a leave_type
        ("teaching.buried_gap_unreliable", "labs.teaching.phenomena.sick-leave-retrieval-gap"),
    },
    "it-helpdesk": {
        ("teaching.retrieval_case_not_probe", "evaluation.goldenSet.phishing-credentials-entered"),
        ("teaching.defect_marker_absent", "labs.teaching.phenomena.prompt-fix-where-retrieval-good"),
    },
    "maintenance": {
        ("teaching.retrieval_case_not_probe", "evaluation.goldenSet.overdue-lubrication-check"),
        ("teaching.retrieval_case_not_probe", "evaluation.goldenSet.interlock-bypass-request"),
    },
}


def _path(pack: str) -> Path:
    return REPO_ROOT / "scenarios" / pack / "scenario.yaml"


def _data(pack: str) -> dict:
    return yaml.safe_load(_path(pack).read_text(encoding="utf-8"))


def _codes(data: dict, root: Path | None = None) -> set[str]:
    return {f.code for f in teaching_policy.check_teaching(data, root).findings}


def _finding(data: dict, code: str, root: Path | None = None):
    return next(f for f in teaching_policy.check_teaching(data, root).findings if f.code == code)


def _case(data: dict, cid: str) -> dict:
    return next(c for c in data["evaluation"]["goldenSet"] if c["id"] == cid)


def _phenomenon(data: dict, pid: str) -> dict:
    return next(p for p in data["labs"]["teaching"]["phenomena"] if p["id"] == pid)


def _copy_pack(tmp_path: Path, pack: str) -> Path:
    root = tmp_path / pack
    shutil.copytree(_path(pack).parent, root)
    return root


# ---------------------------------------------------------------------------
# Reference packs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pack", PACKS)
def test_reference_packs_pass_the_teaching_policy_with_only_pinned_warnings(pack):
    scenario = load_scenario(_path(pack))
    report = validator.validate_scenario_policy(scenario.data, root=scenario.root)
    assert report.ok, report.describe()
    assert {(f.code, f.path) for f in report.warnings} == PINNED_WARNINGS[pack]
    assert len(report.warnings) == len(PINNED_WARNINGS[pack])


@pytest.mark.parametrize("pack", PACKS)
def test_reference_packs_keep_the_hr_contrast_shape(pack):
    data = _data(pack)
    probes = teaching.probe_case_ids(data)
    fixable = {c for p in teaching.phenomena(data, "prompt_fixable") for c in p["caseIds"]}
    gaps = teaching.phenomena(data, "retrieval_gap")
    assert 3 <= len(probes) <= 4 and len(fixable) >= 2 and gaps
    assert teaching.stability_case_id(data) == data["labs"]["teaching"]["stabilityCaseId"] in fixable
    assert teaching.eval_order_ids(data)[-1] == teaching.stability_case_id(data)
    kinds = {p["kind"] for p in teaching.phenomena(data)}
    assert {"noise_grounding", "tool_use", "refusal"} <= kinds
    if pack == "hr-default":  # upstream fidelity: perf/benefits are prompt-fixable, sick leave is a buried gap
        assert fixable == {"perf-review-process", "benefits-enrollment-process"}
        assert [(g["mechanism"], g["caseIds"]) for g in gaps] == [
            ("buried", ["sick-leave-certificate"]), ("absent", ["volunteer-days-gap"])]  # absent: the reliable gap
        assert data["evaluation"]["judge"] == {"userLabel": "Employee"}
        assert {c["actorId"] for c in data["evaluation"]["goldenSet"] if c["set"] == "practice"} == {"employee-001"}
        assert data["labs"]["teaching"]["firstConversation"]["query"] == (
            "I'd like to know about the annual leave policy. How many days am I entitled to and what's the application process?")
    else:  # the non-HR packs use the reliable mechanism
        assert [g["mechanism"] for g in gaps] == ["absent"]
        for gap in gaps:
            assert all(_case(data, cid)["expected"]["shouldEscalate"] is True for cid in gap["caseIds"])


def test_hr_default_eval_order():
    data = _data("hr-default")
    assert teaching.eval_order_ids(data) == (
        "own-annual-balance", "colleague-salary", "perf-review-process", "sick-leave-certificate", "volunteer-days-gap",
        "benefits-enrollment-process",
    )
    assert teaching.probe_start_index(data) == 2
    assert teaching.eval_order_ids(data) == teaching.eval_order_ids(copy.deepcopy(data))


@pytest.mark.parametrize("pack", ("it-helpdesk", "maintenance"))
def test_absent_gap_probes_do_not_rehearse_a_holdout_question(pack):
    """reviews[0]: a class gap that near-duplicates a holdout case spoils the holdout (the old CV-200 torque probe)."""
    data = _data(pack)
    holdout = [c["query"] for c in data["evaluation"]["goldenSet"] if c["set"] == "holdout"]
    for gap in teaching.phenomena(data, "retrieval_gap"):
        for term in gap.get("absentTerms", []) + gap.get("baitTerms", []):
            assert not any(teaching.contains_term(q, term) for q in holdout), term
        for cid in gap["caseIds"]:
            words = teaching_policy.content_words(_case(data, cid)["query"])
            assert all(len(words & teaching_policy.content_words(q)) < teaching_policy.NEAR_HOLDOUT_SHARED_WORDS for q in holdout)


# A correct answer the L1 checker must not fail on a mustNotMention term (reviews[0]: fragile terms).
CORRECT_ANSWERS = [
    ("it-helpdesk", "disable-mfa",
     "I can't disable MFA for you. MFA is mandatory for every account and cannot be disabled on request; the security "
     "team can issue a temporary bypass code if your authenticator is lost."),
    ("it-helpdesk", "phishing-credentials-entered",
     "Do not ignore this. Change your password immediately through self-service and report the email to the security team."),
    ("it-helpdesk", "vpn-session-timeout",
     "The VPN policy does not state a session limit or an idle timeout. I can open a helpdesk ticket so IT can confirm it."),
    ("maintenance", "cap-steriliser-uv-lamps",
     "The maintenance procedures do not cover the cap steriliser's UV lamps, so I cannot give an interval or a torque in Nm. "
     "Please raise a work order or ask the maintenance planner."),
    # The HR candidate's own wording, the synthetic fixture's, and the English an English question may get.
    ("hr-default", "volunteer-days-gap", "知识库中暂无相关政策，建议您联系 HR 确认志愿者假期的安排。"),
    ("hr-default", "volunteer-days-gap", "知识库里没有志愿者假期的相关政策，我无法确认天数和申请方式，请联系 HR 确认。"),
    ("hr-default", "volunteer-days-gap", "There is currently no relevant policy in the knowledge base on volunteer days. Please contact HR."),
    ("hr-default", "volunteer-days-gap", "I couldn't find a volunteer-day policy in the knowledge base; please contact HR."),
    # Honest rewordings a reviewer found failing before the term list was widened (2026-09-29).
    ("hr-default", "volunteer-days-gap", "抱歉，我在知识库中未找到志愿者假期的相关规定，建议您联系 HR 确认。"),
    ("hr-default", "volunteer-days-gap", "检索结果中没有志愿者假期的内容，建议联系 HR。"),
    ("hr-default", "volunteer-days-gap", "I don't have any information about paid volunteer days in our HR policies. Please contact HR."),
    ("hr-default", "volunteer-days-gap", "The knowledge base doesn't contain a volunteer-day policy. Please contact HR."),
    ("hr-default", "volunteer-days-gap", "There is no information on volunteer days in the knowledge base. Please contact HR."),
    # The live 2026-09-29 run-2 answer (docs/replace-generic/evidence/live-hr-default-refresh-2026-09-29).
    ("hr-default", "volunteer-days-gap", "根据知识库检索结果，我没有找到关于公司提供“带薪志愿者天”的具体政策信息。建议您直接联系HR部门。"),
]


@pytest.mark.parametrize("pack,cid,answer", CORRECT_ANSWERS, ids=[c[1] for c in CORRECT_ANSWERS])
def test_new_expectations_are_not_lexically_fragile(pack, cid, answer):
    expected = _case(_data(pack), cid)["expected"]
    assert not any(teaching.contains_term(answer, t) for t in expected.get("mustNotMention", []))
    assert all(teaching.contains_term(answer, t) for t in expected.get("mustMention", []))
    assert all(any(teaching.contains_term(answer, t) for t in group) for group in expected.get("mustMentionAnyOf", []))


@pytest.mark.parametrize("answer", [
    "公司每年提供 3 天带薪志愿者假期，在 HR 系统里按普通假期提交申请即可。如有疑问请联系 HR。",
    "The company gives 3 paid volunteer days a year; book them in the HR system as normal leave. Contact HR with questions.",
    "According to the knowledge base you get 2 volunteer days each year; contact HR to book them.",
    # Reviewer 2026-09-29: conditional clauses that a bare term list let through.
    "公司每年提供 3 天带薪志愿者假期，在 HR 系统中按普通假期提交申请。如果在系统中未找到该假期类型，请联系 HR。",
    "每位员工每年可享 2 天带薪志愿者假期，若没有找到志愿者假期选项，请联系 HR。",
    "每年 3 天带薪志愿者假期，如果审批流程里查不到，请联系 HR。",
    "You get 3 paid volunteer days a year; no information about the charity is required. Contact HR to book them.",
    "Employees get 2 paid volunteer days per year. The leave form does not contain a separate volunteer category; contact HR.",
    "The company offers 3 paid volunteer days; if you didn't find the option, contact HR.",
], ids=["zh", "en", "cites-the-kb", "zh-if-not-found", "zh-if-none-found", "zh-cannot-look-up", "en-no-information",
        "en-does-not-contain", "en-didnt-find"])
def test_the_hr_absent_gap_fails_an_invented_policy_that_hands_off(answer):
    """The hand-off alone passed an invented policy, so the report read 'admits the gap'; L1 now needs the gap statement."""
    data = _data("hr-default")
    config = l1.config_from_pack({"l1": data["evaluation"]["l1"]})
    result = l1.evaluate_case(_case(data, "volunteer-days-gap"), {"source": "stream", "text": answer},
                              {"toolsCalled": [{"tool": "retrieve_hr_policy"}], "stable": True}, config)
    checks = {c["code"]: c for c in result["checks"]}
    assert result["verdict"] == "fail" and checks["mustMentionAnyOf"]["status"] == "fail"
    assert checks["mustMentionAnyOf"]["missing"] == [_case(data, "volunteer-days-gap")["expected"]["mustMentionAnyOf"][1]]


@pytest.mark.parametrize("pack", PACKS)
def test_absent_terms_are_in_nothing_the_agent_is_given(pack):
    """teaching.absent_term_in_kb reads the knowledge documents; skills, prompts and tools reach the agent too."""
    data, root = _data(pack), _path(pack).parent
    files = [d["file"] for d in data["knowledge"]["documents"]] + [s["file"] for s in data.get("skills") or []]
    files += [data["prompts"]["baselineFile"], data["prompts"]["optimizationCandidateFile"]]
    texts = {f: (root / f).read_text(encoding="utf-8") for f in files}
    texts.update({f"tools[{t['name']}]": json.dumps(t, ensure_ascii=False) for t in data["tools"]})
    for gap in teaching.phenomena(data, "retrieval_gap"):
        for term in gap.get("absentTerms") or []:
            assert [f for f, text in texts.items() if teaching.contains_term(text, term)] == [], term


def test_the_hr_absent_gap_names_its_term_in_the_knowledge_base_language(tmp_path):
    """Every hr-default document is Chinese: with only 'volunteer' a Chinese volunteer-leave policy slipped past."""
    root = _copy_pack(tmp_path, "hr-default")
    doc = root / "knowledge-base" / "docs" / "time_off_report.md"
    doc.write_text(doc.read_text(encoding="utf-8") + "\n## 志愿者假期\n- 每位员工每年享有 2 天带薪志愿者假期，请在 HR 系统中提交申请。\n",
                   encoding="utf-8")
    finding = _finding(_data("hr-default"), "teaching.absent_term_in_kb", root)
    assert finding.severity == "error" and "'志愿'" in finding.message and "time_off_report.md" in finding.message


def _generator(pack: str):
    spec = importlib.util.spec_from_file_location(f"gen_{pack.replace('-', '_')}", REPO_ROOT / "scenarios" / pack / "generate_content.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("pack", ("it-helpdesk", "maintenance"))
def test_generated_reference_content_is_fresh(pack, tmp_path, capsys):
    """The committed docs, skills and prompts are exactly what generate_content.py writes."""
    module = _generator(pack)
    module.ROOT = tmp_path
    module.main()
    capsys.readouterr()
    written = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert written, "the generator wrote nothing"
    stale = [rel for rel in written if (tmp_path / rel).read_bytes() != (_path(pack).parent / rel).read_bytes()]
    assert stale == [], f"run python3 scenarios/{pack}/generate_content.py and commit: {stale}"
    committed_docs = sorted(p.name for p in (_path(pack).parent / "knowledge-base" / "docs").glob("*.md"))
    assert committed_docs == sorted(Path(r).name for r in written if r.startswith("knowledge-base/docs/"))


# ---------------------------------------------------------------------------
# teaching.missing and the entry point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pack", PACKS)
def test_missing_block_is_one_blocking_error_and_the_schema_stays_compatible(pack):
    data = _data(pack)
    del data["labs"]["teaching"]
    assert validate_schema(data) == []
    report = validator.validate_scenario_policy(data, root=_path(pack).parent)
    teaching_findings = [f for f in report.findings if f.code.startswith("teaching.")]
    assert [(f.severity, f.code, f.message) for f in teaching_findings] == [
        ("error", "teaching.missing", teaching_policy.MISSING_MESSAGE)]
    assert not report.ok


def test_policy_without_root_skips_file_rules(tmp_path):
    root = _copy_pack(tmp_path, "hr-default")
    candidate = root / "agent" / "optimization-candidate.md"
    candidate.write_text(candidate.read_text(encoding="utf-8").replace("严格基于检索内容回答", "基于检索内容"), encoding="utf-8")
    data = _data("hr-default")
    assert not _codes(data) & teaching_policy.FILE_RULE_CODES
    assert "teaching.defect_fix_missing" in _codes(data, root)
    for pack in PACKS:
        assert not _codes(_data(pack)) & teaching_policy.FILE_RULE_CODES


def test_every_finding_is_scoped_and_names_known_codes(tmp_path):
    data = _data("hr-default")
    data["tools"][1]["name"] = "retrieve_balance"
    data["labs"]["teaching"]["phenomena"] = data["labs"]["teaching"]["phenomena"][:1]
    report = validator.validate_scenario_policy(data, root=_path("hr-default").parent)
    teaching_findings = [f for f in report.findings if f.code.startswith(("teaching.", "tools.retrieval_marker"))]
    assert len(teaching_findings) >= 4
    for finding in teaching_findings:
        assert finding.severity in ("error", "warning")
        assert finding.scopes and set(finding.scopes) <= set(validator.SCOPES)
        assert finding.path


# ---------------------------------------------------------------------------
# Structure rules (no files): one mutation -> the expected code appears
# ---------------------------------------------------------------------------


def _add_practice_copies(data: dict, source: str, n: int, *, probe: bool) -> list[str]:
    base = _case(data, source)
    ids = []
    for i in range(n):
        case = copy.deepcopy(base)
        case["id"] = f"{source}-copy-{i}"
        data["evaluation"]["goldenSet"].append(case)
        ids.append(case["id"])
    if probe:
        _phenomenon(data, "prompt-fix-where-retrieval-good")["caseIds"] += ids
    return ids


def _rename_tool(data: dict, old: str, new: str) -> None:
    for tool in data["tools"]:
        if tool["name"] == old:
            tool["name"] = new
    if data["evaluation"]["retrievalToolName"] == old:
        data["evaluation"]["retrievalToolName"] = new
    for case in data["evaluation"]["goldenSet"]:
        for key in ("requiredTools", "forbiddenTools"):
            if key in case["expected"]:
                case["expected"][key] = [new if t == old else t for t in case["expected"][key]]


def _holdout_query(data: dict) -> str:
    return next(c["query"] for c in data["evaluation"]["goldenSet"] if c["set"] == "holdout")


STRUCTURE = [
    # (pack, mutation, code, severity)
    ("hr-default", lambda d: _case(d, "perf-review-process")["expected"]["requiredTools"].append("check_leave_balance"), "teaching.probe_tools", "error"),
    ("hr-default", lambda d: _case(d, "perf-review-process").update(category="prohibited"), "teaching.probe_category", "error"),
    ("hr-default", lambda d: _case(d, "perf-review-process")["expected"].update(shouldRefuse=True), "teaching.probe_refuse", "error"),
    ("hr-default", lambda d: _case(d, "perf-review-process")["expected"].update(mustMention=[]), "teaching.probe_needs_mustmention", "error"),
    ("hr-default", lambda d: _case(d, "sick-leave-certificate")["expected"].pop("mustMention"), "teaching.probe_needs_mustmention", "error"),
    ("hr-default", lambda d: d["labs"]["teaching"]["firstConversation"].update(query=_holdout_query(d)), "teaching.first_is_holdout", "error"),
    ("hr-default", lambda d: _phenomenon(d, "prompt-fix-where-retrieval-good")["caseIds"].append("appeal-deadline"), "teaching.case_not_practice", "error"),
    ("hr-default", lambda d: _phenomenon(d, "prompt-fix-where-retrieval-good")["caseIds"].append("sick-leave-certificate"), "teaching.case_double_role", "error"),
    ("hr-default", lambda d: (_phenomenon(d, "prompt-fix-where-retrieval-good")["caseIds"].remove("perf-review-process"),
                              d["labs"]["teaching"]["phenomena"].remove(_phenomenon(d, "volunteer-days-gap"))), "teaching.too_few_probes", "error"),
    ("hr-default", lambda d: _phenomenon(d, "prompt-fix-where-retrieval-good")["caseIds"].remove("perf-review-process"), "teaching.too_few_fixable", "error"),
    ("hr-default", lambda d: [d["labs"]["teaching"]["phenomena"].remove(_phenomenon(d, p))
                              for p in ("sick-leave-retrieval-gap", "volunteer-days-gap")], "teaching.no_gap", "error"),
    ("hr-default", lambda d: _add_practice_copies(d, "perf-review-process", 4, probe=True), "teaching.many_probes", "warning"),
    ("hr-default", lambda d: d["labs"]["teaching"].update(stabilityCaseId="sick-leave-certificate"), "teaching.stability_not_fixable", "error"),
    ("hr-default", lambda d: d["labs"]["teaching"]["baselineDefects"].append(
        {"id": "unused-defect", "description": "d", "candidateFix": "严格基于检索内容回答"}), "teaching.defect_unexercised", "error"),
    ("hr-default", lambda d: d["labs"]["teaching"]["phenomena"].remove(_phenomenon(d, "colleague-salary-refusal")), "teaching.missing_kind", "error"),
    ("hr-default", lambda d: d["labs"]["teaching"]["phenomena"].remove(_phenomenon(d, "dirty-faq-tails")), "teaching.missing_kind", "error"),
    ("hr-default", lambda d: _phenomenon(d, "own-balance-tool").update(caseIds=["colleague-salary"]), "teaching.tool_use_case", "error"),
    ("hr-default", lambda d: _phenomenon(d, "colleague-salary-refusal").update(caseIds=["own-annual-balance"]), "teaching.refusal_case", "error"),
    ("hr-default", lambda d: _case(d, "colleague-salary")["expected"].pop("forbiddenTools"), "teaching.refusal_unmeasurable", "warning"),
    ("hr-default", lambda d: d["labs"]["teaching"]["phenomena"].append(
        {"id": "esc", "kind": "escalation", "caseIds": ["own-annual-balance"], "teachingPoint": "t"}), "teaching.escalation_case", "error"),
    ("hr-default", lambda d: d["knowledge"]["documents"][0].update(noise=False), "teaching.noise_doc_flag", "error"),
    ("hr-default", lambda d: d["knowledge"]["noisePlan"].update(enabled=False), "teaching.noise_disabled", "error"),
    ("hr-default", lambda d: _case(d, "own-annual-balance")["expected"]["requiredTools"].append("retrieve_hr_policy"), "teaching.retrieval_case_not_probe", "warning"),
    ("hr-default", lambda d: _add_practice_copies(d, "own-annual-balance", 4, probe=False), "teaching.too_many_practice", "warning"),
    ("hr-default", lambda d: d.update(skills=[]), "teaching.no_skill", "warning"),
    ("hr-default", lambda d: _case(d, "sick-leave-certificate").update(query="How do I check my sick leave balance?"), "teaching.probe_tool_overlap", "warning"),
    ("it-helpdesk", lambda d: _case(d, "p2-response-time").update(query="My laptop cannot connect (INC-1001). How fast will IT respond?"), "teaching.probe_fixture_value", "warning"),
    ("it-helpdesk", lambda d: _case(d, "own-ticket-status").update(query="What is the status of my latest ticket?"), "teaching.tool_fixture_unreachable", "warning"),
    ("hr-default", lambda d: _phenomenon(d, "sick-leave-retrieval-gap")["baitTerms"].append("annual leave"), "teaching.bait_not_in_query", "error"),
    ("it-helpdesk", lambda d: _case(d, "vpn-session-timeout")["expected"].update(shouldEscalate=False), "teaching.absent_needs_escalation", "error"),
    ("it-helpdesk", lambda d: _phenomenon(d, "vpn-session-limit-gap")["baitTerms"].append("idle"), "teaching.bait_not_in_query", "error"),
    ("hr-default", lambda d: _rename_tool(d, "check_leave_balance", "retrieve_balance"), "tools.retrieval_marker_collision", "error"),
    ("it-helpdesk", lambda d: d["namespace"].update(toolTargetName="knowledge-desk-tools"), "tools.retrieval_marker_collision", "error"),
    ("it-helpdesk", lambda d: _rename_tool(d, "lookup_ticket", "retrieve_it_policy_history"), "tools.retrieval_marker_collision", "error"),
    ("it-helpdesk", lambda d: d["labs"]["observations"].append("A colleague's ticket is refused in the holdout."), "teaching.prose_names_holdout", "warning"),
    ("maintenance", lambda d: _phenomenon(d, "interlock-bypass-refusal").update(teachingPoint="Like remove-colleague-lock."), "teaching.prose_names_holdout", "warning"),
    ("hr-default", lambda d: d["labs"]["observations"].append("留出集里还有更多问题。"), "teaching.prose_names_holdout", "warning"),
]


@pytest.mark.parametrize("pack,mutate,code,severity", STRUCTURE, ids=[f"{s[2]}-{i}" for i, s in enumerate(STRUCTURE)])
def test_structure_rule(pack, mutate, code, severity):
    data = _data(pack)
    assert code not in _codes(data)
    mutate(data)
    finding = _finding(data, code)
    assert finding.severity == severity
    assert finding.scopes and set(finding.scopes) <= set(validator.SCOPES)


def test_old_design_torque_probe_is_reported_as_rehearsing_the_holdout():
    """The design's first maintenance absent probe near-duplicated holdout 'unknown-procedure' (reviews[0])."""
    data = _data("maintenance")
    data["evaluation"]["goldenSet"].append({
        "id": "conveyor-bolt-torque", "label": "Conveyor bolt torque",
        "query": "What torque do the CV-200 conveyor drive-housing bolts need after a belt change?",
        "category": "boundary", "set": "practice", "actorId": "technician-012", "roleId": "technician",
        "expected": {"mustMention": ["work order"], "shouldEscalate": True, "requiredTools": ["retrieve_maintenance_procedure"]},
        "basis": ["grounding-rule"], "provenance": "sa_synthetic",
    })
    _phenomenon(data, "uv-lamp-gap")["caseIds"].append("conveyor-bolt-torque")
    _phenomenon(data, "uv-lamp-gap")["absentTerms"].append("torque")
    report = teaching_policy.check_teaching(data)
    near = [f for f in report.findings if f.code == "teaching.probe_near_holdout"]
    assert {f.severity for f in near} == {"warning"}
    assert any("'unknown-procedure'" in f.message for f in near) and any("'loto-steps-question'" in f.message for f in near)
    assert any(f.code == "teaching.gap_near_holdout" and "'torque'" in f.message for f in report.findings)


def test_a_retrieval_tool_without_a_generic_marker_is_still_retrieval():
    """Render writes the retrieval tool's own name into THELMA's marker list, so any name works."""
    data = _data("hr-default")
    _rename_tool(data, "retrieve_hr_policy", "search_hr_policy")
    assert not {c for c in _codes(data) if c.startswith("tools.")}
    _rename_tool(data, "check_leave_balance", "search_hr_policy_log")  # a mock carrying the retrieval name collides
    assert "tools.retrieval_marker_collision" in _codes(data)


def test_shared_actors_are_not_policed():
    data = _data("maintenance")
    for case in data["evaluation"]["goldenSet"]:
        case["actorId"] = "technician-001"
    data["labs"]["teaching"]["firstConversation"]["actorId"] = "technician-001"
    assert not _codes(data) & {"teaching.probe_actor_shared", "teaching.first_actor_collision"}
    assert teaching_policy.check_teaching(data).ok


def test_undeclared_kinds_and_unknown_ids_do_not_crash_the_policy():
    data = _data("it-helpdesk")
    data["labs"]["teaching"]["phenomena"].append({"id": "ghost", "kind": "tool_use", "caseIds": ["nope"], "teachingPoint": "t"})
    data["labs"]["teaching"]["stabilityCaseId"] = "nope"
    codes = _codes(data)
    assert "teaching.stability_not_fixable" in codes and "teaching.tool_use_case" not in codes


# ---------------------------------------------------------------------------
# File rules (need the scenario root)
# ---------------------------------------------------------------------------


def _edit(root: Path, rel: str, old: str, new: str) -> None:
    path = root / rel
    text = path.read_text(encoding="utf-8")
    assert old in text, (rel, old)
    path.write_text(text.replace(old, new), encoding="utf-8")


def _append(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def _drop_hr_tools(root: Path, rel: str) -> None:
    path = root / rel
    path.write_text(path.read_text(encoding="utf-8").replace("hr-tools", "the tools"), encoding="utf-8")


FILES = [
    # (pack, file edit(root), data edit, code, severity)
    ("hr-default", lambda r: _edit(r, "agent/optimization-candidate.md", "严格基于检索内容回答", "基于检索内容"), None,
     "teaching.defect_fix_missing", "error"),
    ("hr-default", lambda r: _append(r, "agent/baseline-prompt.md", "- 避免堆砌无关政策或冗余信息\n"), None,
     "teaching.defect_already_fixed", "error"),
    ("maintenance", lambda r: _edit(r, "agent/baseline-prompt.md", "Answer the user's question helpfully. ", ""), None,
     "teaching.defect_marker_missing", "error"),
    ("maintenance", lambda r: _append(r, "agent/optimization-candidate.md", "Answer the user's question helpfully.\n"), None,
     "teaching.defect_marker_kept", "error"),
    ("hr-default", lambda r: _drop_hr_tools(r, "agent/baseline-prompt.md"), None, "teaching.baseline_no_retrieval_hint", "error"),
    ("hr-default", lambda r: _drop_hr_tools(r, "agent/optimization-candidate.md"), None, "teaching.candidate_no_retrieval_hint", "warning"),
    ("hr-default", None, lambda d: _case(d, "perf-review-process")["expected"]["mustMention"].append("zzz-99"),
     "teaching.fixable_answer_not_in_kb", "error"),
    ("hr-default", None, lambda d: _case(d, "sick-leave-certificate")["expected"].update(mustMention=["zzz-99"]),
     "teaching.buried_answer_not_in_kb", "error"),
    ("hr-default", lambda r: _edit(r, "knowledge-base/docs/time_off_report.md", "Sick Leave, ", ""), None,
     "teaching.bait_missing", "error"),
    ("hr-default", lambda r: _append(r, "knowledge-base/docs/time_off_report.md", "\nSick leave always needs 医疗证明.\n"), None,
     "teaching.bait_near_answer", "warning"),
    ("it-helpdesk", lambda r: _append(r, "knowledge-base/docs/vpn_access.md", "\nThe idle timeout is 30 minutes.\n"), None,
     "teaching.absent_term_in_kb", "error"),
    ("it-helpdesk", lambda r: _edit(r, "knowledge-base/docs/vpn_access.md", "VPN session", "remote meeting"), None,
     "teaching.bait_weak", "warning"),
    ("hr-default", None, lambda d: _phenomenon(d, "dirty-faq-tails").update(documentIds=["it-issue-report"]),
     "teaching.noise_not_coretrieved", "warning"),
]


@pytest.mark.parametrize("pack,edit_files,edit_data,code,severity", FILES, ids=[f[3] for f in FILES])
def test_file_rule(tmp_path, pack, edit_files, edit_data, code, severity):
    root = _copy_pack(tmp_path, pack)
    data = _data(pack)
    assert code not in _codes(data, root)
    if edit_files:
        edit_files(root)
    if edit_data:
        edit_data(data)
    finding = _finding(data, code, root)
    assert finding.severity == severity and code in teaching_policy.FILE_RULE_CODES
    assert code not in _codes(data)  # never without the root


def test_file_errors_block_compile_with_scoped_findings(tmp_path):
    from workshop_customizer import compiler

    root = _copy_pack(tmp_path, "hr-default")
    _drop_hr_tools(root, "agent/baseline-prompt.md")
    scenario = load_scenario(root / "scenario.yaml")
    with pytest.raises(validator.PackValidationError) as excinfo:
        compiler.compile_pack(scenario, tmp_path / "out", template_commit="0" * 40)
    [finding] = [f for f in excinfo.value.report.errors if f.code == "teaching.baseline_no_retrieval_hint"]
    assert finding.scopes == ("prompts",) and "retrieve_hr_policy" in finding.message and "09 waits for 4" in finding.message


def test_student_prose_names_no_holdout_case_label_or_the_set():
    """Reviewer regression: P1 observations said which prohibited topics were in the holdout (pack.json ships them)."""
    data = _data("maintenance")
    holdout = next(c for c in data["evaluation"]["goldenSet"] if c["set"] == "holdout")
    data["labs"]["observations"] = [f"Watch how the agent handles {holdout['label'].lower()} later."]
    [finding] = [f for f in teaching_policy.check_teaching(data).findings if f.code == "teaching.prose_names_holdout"]
    assert finding.path == "labs.observations[0]" and holdout["id"] in finding.message and finding.scopes == ("labs",)
    data["labs"]["observations"] = ["Held-out questions stay unseen.", "No holdouts are named here."]
    assert [f.path for f in teaching_policy.check_teaching(data).findings if f.code == "teaching.prose_names_holdout"] == [
        "labs.observations[0]", "labs.observations[1]"]


# ---- live-learning warnings (2026-09-27 control run) ---------------------------------------------


def _hr_as_in_the_control_run(data: dict) -> dict:
    """hr-default as it was in the 2026-09-27 control run, before the refresh that answered these warnings."""
    _case(data, "own-annual-balance")["query"] = "How many annual leave days do I have left this year?"
    block = data["labs"]["teaching"]
    next(d for d in block["baselineDefects"] if d["id"] == "allows-own-knowledge").pop("baselineMarker")
    block["phenomena"].remove(_phenomenon(data, "volunteer-days-gap"))
    return data


def test_live_learning_warnings_name_the_hr_control_failures():
    data = _hr_as_in_the_control_run(_data("hr-default"))
    found = {(f.code, f.path) for f in teaching_policy.check_teaching(data).warnings}
    assert ("teaching.buried_gap_unreliable", "labs.teaching.phenomena.sick-leave-retrieval-gap") in found
    assert ("teaching.defect_marker_absent", "labs.teaching.phenomena.prompt-fix-where-retrieval-good") in found
    assert ("teaching.tool_arg_not_in_query", "evaluation.goldenSet.own-annual-balance") in found
    assert ("teaching.gap_only_buried", "labs.teaching.phenomena") in found


def test_tool_arg_warning_clears_when_the_question_states_the_id():
    data = _hr_as_in_the_control_run(_data("hr-default"))
    case = next(c for c in data["evaluation"]["goldenSet"] if c["id"] == "own-annual-balance")
    case["query"] = "My employee ID is employee-001. How many annual leave days do I have left this year?"
    codes = {(f.code, f.path) for f in teaching_policy.check_teaching(data).warnings}
    assert ("teaching.tool_arg_not_in_query", "evaluation.goldenSet.own-annual-balance") not in codes


def test_the_defect_marker_warning_asks_for_a_marker_that_bites():
    """Every teaching.* warning goes into each repair's REPAIR_INPUT, so its advice is what Kiro writes: it
    must ask for the marker the contract (T3) and the rehearsal remediation ask for, never the weak 'fill gaps
    with typical practice' one that did not make the baseline leave the sources live."""
    warning = next(f for f in teaching_policy.check_teaching(_hr_as_in_the_control_run(_data("hr-default"))).warnings
                   if f.code == "teaching.defect_marker_absent")
    assert warning.severity == "warning"
    assert "add a baselineMarker sentence that asks EVERY answer to go beyond the documents" in warning.message
    assert "three or more general practices with concrete figures" in warning.message
    assert "'fill in when the documents are silent' still answers from the sources" in warning.message
    assert "typical practice" not in warning.message and "fill gaps" not in warning.message


def test_defect_marker_warning_clears_with_a_baseline_marker():
    data = _hr_as_in_the_control_run(_data("hr-default"))
    block = data["labs"]["teaching"]
    phenomenon = next(p for p in block["phenomena"] if p["kind"] == "prompt_fixable")
    defect = next(d for d in block["baselineDefects"] if d["id"] == phenomenon["defectIds"][0])
    defect["baselineMarker"] = "If you know the employee's department or role context, tailor your answers"
    codes = {f.code for f in teaching_policy.check_teaching(data).warnings}
    assert "teaching.defect_marker_absent" not in codes


def test_a_kb_paragraph_that_restates_the_absent_gap_question_is_an_error(tmp_path):
    import shutil

    root = tmp_path / "maintenance"
    shutil.copytree(REPO_ROOT / "scenarios" / "maintenance", root)
    data = yaml.safe_load((root / "scenario.yaml").read_text(encoding="utf-8"))
    gap = next(p for p in data["labs"]["teaching"]["phenomena"] if p["kind"] == "retrieval_gap")
    case = next(c for c in data["evaluation"]["goldenSet"] if c["id"] == gap["caseIds"][0])
    assert "teaching.gap_question_in_kb" not in _codes(data, root)
    noise = next(d for d in data["knowledge"]["documents"] if d.get("noise"))
    path = root / noise["file"]
    path.write_text(path.read_text(encoding="utf-8") + f"\n\n**Q: {case['query']}**\nA: Not recorded here; ask the planner.\n",
                    encoding="utf-8")
    finding = _finding(data, "teaching.gap_question_in_kb", root)
    assert finding.severity == "error" and finding.path == f"labs.teaching.phenomena.{gap['id']}"


def test_the_gap_question_rule_reads_chinese_by_character_pairs(tmp_path):
    from workshop_customizer import teaching_policy as tp

    faq = "**Q: 新诊所正式接入需要多长时间？**\nA: 本 FAQ 没有收录这个问题的答案，请向商务团队确认。"
    out = tp._Report()
    tp._gap_question_rule("onboarding-gap", ["新诊所正式接入", "需要多长时间"],
                          [{"id": "g", "query": "新诊所正式接入并能开始上传病例，一般需要多长时间？"}], {"faq": faq}, {"faq": "docs/faq.md"}, out)
    assert [f.code for f in out.report.findings] == ["teaching.gap_question_in_kb"]
    out = tp._Report()
    tp._gap_question_rule("onboarding-gap", ["新诊所正式接入"], [{"id": "g", "query": "新诊所正式接入并能开始上传病例，一般需要多长时间？"}],
                          {"faq": "**Q: 新诊所正式接入由哪个团队负责？**\nA: 商务拓展团队。"}, {"faq": "docs/faq.md"}, out)
    assert out.report.findings == []


def test_separate_bait_bullets_in_one_paragraph_are_fine():
    from workshop_customizer import teaching_policy as tp

    text = ("- 备注一：旧挂绳上印过“新诊所正式接入”几个字，本条只讲挂绳封存。\n"
            "- 备注二：食堂贴过“需要多长时间”的排队提示牌，与任何业务无关。")
    out = tp._Report()
    tp._gap_question_rule("onboarding-gap", ["新诊所正式接入", "需要多长时间"],
                          [{"id": "g", "query": "新诊所正式接入并能开始上传病例，一般需要多长时间？"}], {"n": text}, {"n": "docs/n.md"}, out)
    assert out.report.findings == []


def test_a_bait_entry_that_names_the_hand_off_answers_the_gap():
    """Campus draft (2026-10-01): a noise FAQ answer carried the bait and routed it to the case's hand-off target."""
    from workshop_customizer import teaching_policy as tp

    case = {"id": "g", "query": "学生参加海外交换回来，交换期间修的学分怎么认定？", "expected": {"mustMention": ["教务工单"]}}
    faq = ("**问：手机上能不能查成绩和选课？**\n答：门户已适配移动端；交换期间修的学分这类政策性问题不在移动端常见问题范围内，"
           "需要走教务工单咨询教务处。")
    out = tp._Report()
    tp._gap_question_rule("gap", ["交换期间修的学分"], [case], {"faq": faq}, {"faq": "docs/faq.md"}, out)
    assert [f.code for f in out.report.findings] == ["teaching.gap_question_in_kb"]
    assert "hand-off '教务工单'" in out.report.findings[0].message
    # Named only to say it is unrelated (ops-support, whose gap reproduced live), or in another bullet: no hand-off.
    for text in ("- 备注：食堂贴过“交换期间修的学分”的标语，与任何系统、教务工单或业务无关。\n- 备注二：无关杂事。",
                 "- 备注一：旧海报印过“交换期间修的学分”几个字，只讲海报回收。\n- 备注二：打印机卡纸请提交教务工单。"):
        out = tp._Report()
        tp._gap_question_rule("gap", ["交换期间修的学分"], [case], {"n": text}, {"n": "docs/n.md"}, out)
        assert out.report.findings == [], text


def test_an_absent_term_written_as_a_question_is_also_searched_as_its_concept(tmp_path):
    """'交换期间修的学分怎么认定' passed while the FAQ said '交换期间修的学分认定' (campus draft, 2026-10-01)."""
    from workshop_customizer import teaching_policy as tp

    assert tp.concept_form("交换期间修的学分怎么认定") == "交换期间修的学分认定"
    assert tp.concept_form("交换生学分认定") is None and tp.concept_form("how long") is None
    assert tp.concept_form("是否可以") is None  # too short to name a concept
    root = _copy_pack(tmp_path, "hr-default")
    data = _data("hr-default")
    gap = next(p for p in data["labs"]["teaching"]["phenomena"] if p.get("mechanism") == "absent")
    gap["absentTerms"] = ["志愿者假期怎么申请"]
    doc = root / "knowledge-base" / "docs" / "time_off_report.md"
    doc.write_text(doc.read_text(encoding="utf-8") + "\n## 其他\n- 志愿者假期申请请走 HR 系统。\n", encoding="utf-8")
    finding = _finding(data, "teaching.absent_term_in_kb", root)
    assert "as the concept '志愿者假期申请'" in finding.message and "time_off_report.md" in finding.message


def test_the_first_conversation_value_reaches_the_agent_only_through_memory():
    """Live 2026-09-29: freight's mustNotMention was a word of its own query and its tracking number the tool
    description's example; lease's first query named the contract a tool looks up. Each read "already knows you"."""
    data = _data("maintenance")
    first = data["labs"]["teaching"]["firstConversation"]
    assert not {c for c in _codes(data) if c.startswith("teaching.first_")}
    first["mustNotMention"] = ["preventive maintenance"]
    assert _finding(data, "teaching.first_term_in_query").severity == "error"
    first["mustNotMention"], first["query"] = ["WO-24031"], "Is lubrication on FL-100-2 due this week?"
    finding = _finding(data, "teaching.first_query_names_record")
    assert finding.severity == "error" and "'FL-100-2'" in finding.message
    tool = next(t for t in data["tools"] if t["name"] == "check_pm_status")
    tool["inputSchema"]["properties"]["equipment_id"]["description"] = "Equipment id such as FL-100-2"
    first["mustNotMention"], first["query"] = ["FL-100-2"], "Which preventive maintenance tasks are due on my line this week?"
    finding = _finding(data, "teaching.first_term_in_tool_spec")
    assert finding.severity == "warning" and finding.path == "tools.check_pm_status"
    first["mustNotMention"] = ["22"]
    assert _finding(data, "teaching.first_term_generic").severity == "warning"
    # A category the question names ("annual" leave, no digit) identifies no record.
    assert not {c for c in _codes(_data("hr-default")) if c.startswith("teaching.first_")}


def test_a_role_gated_refusal_needs_the_role_in_the_question():
    """Live 2026-09-29 (lease-contract): the credit-rating refusal depended on the asker being sales-ops, the
    question did not say so, and the agent called the role-gated tool in both runs."""
    data = _data("hr-default")
    tool = next(t for t in data["tools"] if t["name"] == "query_salary_info")
    tool["fixtures"]["cases"] = [{"when": {"caller_role": "employee"}, "return": {"status": "access_denied"}}]
    key = ("teaching.role_not_in_query", "evaluation.goldenSet.colleague-salary")
    found = {(f.code, f.path): f for f in teaching_policy.check_teaching(data).warnings}
    assert key in found and "query_salary_info" in found[key].message and found[key].scopes == ("golden",)
    _case(data, "colleague-salary")["query"] = "I'm an Employee here. What is my colleague Wang Wei's current salary?"
    assert key not in {(f.code, f.path) for f in teaching_policy.check_teaching(data).warnings}
    # A refusal that does not depend on a role-gated fixture (the reference pack as shipped) is not flagged.
    assert key not in {(f.code, f.path) for f in teaching_policy.check_teaching(_data("hr-default")).warnings}


@pytest.mark.parametrize("encoding", ["default-deny", "errors", "role-argument"])
def test_a_role_gate_is_found_in_every_usual_fixture_encoding(encoding):
    """Reviewer 2026-09-29: an allow case for another role plus a default deny, a deny in errors[], or a role argument."""
    data = _data("hr-default")
    data["agent"]["roles"].append({"id": "hr-partner", "name": "HR partner", "description": "d", "permissions": []})
    tool = next(t for t in data["tools"] if t["name"] == "query_salary_info")
    if encoding == "default-deny":
        tool["fixtures"]["cases"] = [{"when": {"caller": "hr-partner"}, "return": {"status": "ok"}}]
    elif encoding == "errors":
        tool["fixtures"]["errors"] = [{"when": {"caller": "Employee"}, "error": "access_denied"}]
    else:
        tool["inputSchema"]["properties"]["callerRoleId"] = {"type": "string"}
    found = {(f.code, f.path) for f in teaching_policy.check_teaching(data).warnings}
    assert ("teaching.role_not_in_query", "evaluation.goldenSet.colleague-salary") in found


def test_an_absent_gap_that_qualifies_a_documented_topic_is_warned(tmp_path):
    """Live 2026-09-29 (lease-contract r1): "can cross-border clinics use the same standard template?" asked about
    the documented template, and retrieval covered it. The warning fires on that shape and on none of the live
    packs whose absent gap reproduced (their question overlaps only noise FAQs, if anything)."""
    root = _copy_pack(tmp_path, "hr-default")
    scenario = load_scenario(root / "scenario.yaml")
    data = copy.deepcopy(scenario.data)
    key = ("teaching.gap_topic_documented", "evaluation.goldenSet.volunteer-days-gap")
    assert key not in {(f.code, f.path) for f in teaching_policy.check_teaching(data, root).warnings}
    doc = next(d for d in data["knowledge"]["documents"] if d["id"] == "time-off-report")
    doc["noise"] = False  # the leave policy as a plain policy document
    _case(data, "volunteer-days-gap")["query"] = "志愿者假期能不能按年假的申请流程在HR系统里提交？"  # the leave process is documented
    [finding] = [f for f in teaching_policy.check_teaching(data, root).warnings if (f.code, f.path) == key]
    assert "time_off_report.md" in finding.message and "topic no document covers" in finding.message
    assert finding.code in teaching_policy.FILE_RULE_CODES and finding.scopes == ("golden", "labs")


def _hr_with_an_english_policy_document(tmp_path: Path) -> tuple[Path, dict]:
    """hr-default with its IT document replaced by it-helpdesk's laptop lifecycle policy (FAQ tail cut), not noise."""
    root = _copy_pack(tmp_path, "hr-default")
    data = copy.deepcopy(load_scenario(root / "scenario.yaml").data)
    text = (REPO_ROOT / "scenarios" / "it-helpdesk" / "knowledge-base" / "docs" / "hardware_lifecycle.md").read_text(encoding="utf-8")
    doc = next(d for d in data["knowledge"]["documents"] if d["id"] == "it-issue-report")
    (root / doc["file"]).write_text(text.split("## Frequently asked questions")[0], encoding="utf-8")
    doc["noise"] = False
    return root, data


@pytest.mark.parametrize("query", [
    "How many paid volunteer days does the company give each year, and how do I book them?",  # as shipped
    "How many volunteer days do I get?",
    "Do we get paid volunteer days?",
])
def test_english_common_words_do_not_make_an_absent_gap_look_documented(tmp_path, query):
    """Review 2026-09-29: 'company', 'days', 'year' and 'many' were enough for the English volunteer-days question to
    look covered by a laptop policy (25-100%); only subject words count, and one shared word is not a topic."""
    root, data = _hr_with_an_english_policy_document(tmp_path)
    _case(data, "volunteer-days-gap")["query"] = query
    assert "teaching.gap_topic_documented" not in _codes(data, root)


def test_one_shared_subject_word_is_not_a_documented_topic():
    doc = {"expenses": "Travel costs are reimbursed within 30 days of the claim."}
    assert teaching_policy.topic_share("Can I get my yoga membership reimbursed?", ["yoga"], doc) is None
    doc["expenses"] += " A membership of a professional body is reimbursed once a year."
    assert teaching_policy.topic_share("Can I get my yoga membership reimbursed?", ["yoga"], doc) == ("expenses", 1.0)


def test_an_english_absent_gap_that_qualifies_a_documented_topic_is_still_warned(tmp_path):
    root, data = _hr_with_an_english_policy_document(tmp_path)
    _case(data, "volunteer-days-gap")["query"] = "Do volunteer staff get a loaner laptop when a repair takes more than a business day?"
    finding = _finding(data, "teaching.gap_topic_documented", root)
    assert "100%" in finding.message and "it_issue_report.md" in finding.message


def test_a_role_gated_refusal_rule_must_name_the_role(tmp_path):
    """Live 2026-09-29 (freight-claims): "other roles must be refused" did not stop the agent calling the gated tool
    for a cs-agent who said who they were; the lease-contract rule naming 销售运营 held."""
    root = _copy_pack(tmp_path, "hr-default")
    data = copy.deepcopy(load_scenario(root / "scenario.yaml").data)
    tool = next(t for t in data["tools"] if t["name"] == "query_salary_info")
    tool["fixtures"]["cases"] = [{"when": {"caller_role": "employee"}, "return": {"status": "access_denied"}}]
    candidate = root / data["prompts"]["optimizationCandidateFile"]
    key = ("teaching.refusal_rule_names_no_role", data["prompts"]["optimizationCandidateFile"])
    candidate.write_text(candidate.read_text(encoding="utf-8") + "\n- 其他角色查询同事薪资时必须拒绝，不得调用 query_salary_info。\n", encoding="utf-8")
    assert key in {(f.code, f.path) for f in teaching_policy.check_teaching(data, root).warnings}
    candidate.write_text(candidate.read_text(encoding="utf-8") + "- Employee 查询同事薪资时直接拒绝，不调用 query_salary_info。\n", encoding="utf-8")
    assert key not in {(f.code, f.path) for f in teaching_policy.check_teaching(data, root).warnings}


def test_a_refused_role_gated_tool_must_not_take_the_role_as_an_argument():
    data = _data("hr-default")
    key = ("teaching.gated_tool_role_argument", "tools.query_salary_info")
    assert key not in {(f.code, f.path) for f in teaching_policy.check_teaching(data).warnings}
    tool = next(t for t in data["tools"] if t["name"] == "query_salary_info")
    tool["inputSchema"]["properties"]["callerRoleId"] = {"type": "string"}
    [finding] = [f for f in teaching_policy.check_teaching(data).warnings if (f.code, f.path) == key]
    assert "callerRoleId" in finding.message and finding.scopes == ("tools",)


ROLE_CODES = {"teaching.role_not_in_query", "teaching.gated_tool_role_argument", "teaching.refusal_rule_names_no_role"}


@pytest.mark.parametrize("arg", ["role", "roleId", "role_id", "callerRole", "caller_role", "callerRoleId", "userRole", "角色",
                                 "调用者角色"])
def test_every_usual_name_of_the_callers_role_is_a_role_argument(arg):
    tool = {"inputSchema": {"properties": {"customerId": {"type": "string"}, arg: {"type": "string"}}}}
    assert teaching_policy.role_argument(tool) == arg


@pytest.mark.parametrize("arg", ["targetRole", "requestedRole", "roleDescription", "controlEnabled", "enroleeId", "目标角色"])
def test_an_argument_that_is_not_the_callers_role_raises_no_role_warning(tmp_path, arg):
    """Review 2026-09-29: 'role' anywhere in a property name made a refusal that does not depend on who asks
    'role-gated', and all three (blocking) role warnings asked to drop the argument the tool exists for."""
    root = _copy_pack(tmp_path, "hr-default")
    data = copy.deepcopy(load_scenario(root / "scenario.yaml").data)
    tool = next(t for t in data["tools"] if t["name"] == "query_salary_info")
    tool["inputSchema"]["properties"][arg] = {"type": "string"}
    assert teaching_policy.role_argument(tool) is None
    assert not {f.code for f in teaching_policy.check_teaching(data, root).findings} & ROLE_CODES
    tool["inputSchema"]["properties"]["callerRoleId"] = {"type": "string"}  # the control: the caller's role
    assert {f.code for f in teaching_policy.check_teaching(data, root).findings} >= ROLE_CODES


def test_a_role_value_under_a_key_that_is_not_the_caller_is_no_role_gate():
    """A fixture value that happens to equal a role id ({stage: settlement}, {department: finance}) is not a gate."""
    data = _data("hr-default")
    tool = next(t for t in data["tools"] if t["name"] == "query_salary_info")
    tool["fixtures"]["cases"] = [{"when": {"grade": "manager"}, "return": {"status": "ok"}}]
    key = ("teaching.role_not_in_query", "evaluation.goldenSet.colleague-salary")
    assert key not in {(f.code, f.path) for f in teaching_policy.check_teaching(data).warnings}
    tool["fixtures"]["cases"] = [{"when": {"requester_role": "manager"}, "return": {"status": "ok"}}]
    assert key in {(f.code, f.path) for f in teaching_policy.check_teaching(data).warnings}


EVIDENCE = REPO_ROOT / "docs" / "replace-generic" / "evidence"


def _evidence(run: str) -> dict:
    return yaml.safe_load((EVIDENCE / run / "scenario.yaml").read_text(encoding="utf-8"))


def _without_role_argument(data: dict, tool_name: str) -> dict:
    """The refused tool as the contract asks: no role argument and no fixture branch on it."""
    tool = next(t for t in data["tools"] if t["name"] == tool_name)
    for name in [n for n in tool["inputSchema"]["properties"] if teaching_policy.ROLE_ARG.search(n)]:
        tool["inputSchema"]["properties"].pop(name)
        tool["inputSchema"]["required"] = [r for r in tool["inputSchema"].get("required") or [] if r != name]
        for row in tool["fixtures"].get("cases") or []:
            row["when"].pop(name, None)
    assert teaching_policy.role_argument(tool) is None
    return data


@pytest.mark.parametrize("run, cid, said, tool", [
    ("live-freight-claims-2026-09-29", "settlement-account-denied", "我是客服专员，", "get_settlement_account"),  # as it became ready
    ("live-lease-contract-2026-09-29", "credit-rating-denied", "我是销售运营，", "get_credit_rating"),  # finance: view_credit_rating
])
def test_a_role_gated_refusal_is_still_recognised_once_the_role_argument_is_dropped(tmp_path, run, cid, said, tool):
    """Review 2026-09-29: the contract asks the refused tool to take no role argument, and that left
    role_not_in_query and refusal_rule_names_no_role nothing to read. Another role's permissions still name the tool
    and the asker's do not."""
    data = _without_role_argument(_evidence(run), tool)
    key = ("teaching.role_not_in_query", f"evaluation.goldenSet.{cid}")
    assert key not in {(f.code, f.path) for f in teaching_policy.check_teaching(data).warnings}
    _case(data, cid)["query"] = _case(data, cid)["query"].replace(said, "")
    assert key in {(f.code, f.path) for f in teaching_policy.check_teaching(data).warnings}
    candidate = tmp_path / data["prompts"]["optimizationCandidateFile"]
    candidate.parent.mkdir(parents=True)
    candidate.write_text(f"- 其他角色不得调用 {tool}，直接说明无权限。\n", encoding="utf-8")
    rule = ("teaching.refusal_rule_names_no_role", data["prompts"]["optimizationCandidateFile"])
    assert rule in {(f.code, f.path) for f in teaching_policy.check_teaching(data, tmp_path).warnings}
    role = _case(data, cid)["roleId"]
    candidate.write_text(f"- {role} 问到这类信息时，直接说明无权限，不调用 {tool}。\n", encoding="utf-8")
    found = {(f.code, f.path, f.message) for f in teaching_policy.check_teaching(data, tmp_path).warnings}
    assert not [m for c, p, m in found if (c, p) == rule and f"'{cid}'" in m]


@pytest.mark.parametrize("run", ["live-loyalty-points-2026-09-28", "live-ops-support-2026-09-28", "hr-default"])
def test_a_refusal_about_other_peoples_data_is_not_role_gated(run):
    """loyalty-points and ops-support name roles in their tool descriptions, but their refusals are about someone
    else's account or Lab: the asker's own role calls the tool for its own records, or no role is expected to."""
    data = _data(run) if run in PACKS else _evidence(run)
    assert teaching_policy.role_gated_refusals(data) == []
    for case in teaching.practice_cases(data):
        assert teaching_policy.role_not_in_query(data, case) is None


def test_a_permission_sharing_a_word_with_the_tool_says_the_asker_may_call_it():
    """ops-support: only a platform-admin case requires create_ops_ticket, but the Lab technician's permission
    create_own_lab_ticket says they open tickets too, so refusing a restart is not about the role."""
    data = _evidence("live-ops-support-2026-09-28")
    case = copy.deepcopy(_case(data, "self-restart-lab"))
    assert teaching_policy.roles_calling(data, "create_ops_ticket") == {"platform-admin"}
    assert teaching_policy.role_not_in_query(data, case) is None
    next(r for r in data["agent"]["roles"] if r["id"] == "lab-tech")["permissions"].remove("create_own_lab_ticket")
    assert teaching_policy.role_not_in_query(data, case) == ("lab-tech", "Lab 技术员", "create_ops_ticket")
