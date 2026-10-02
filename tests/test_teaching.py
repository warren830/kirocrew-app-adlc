"""teaching.py derivations: probes, eval order, stability case, lexical matching, answer/teaching
documents and the THRESHOLDS comparisons (offline, deterministic)."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
import yaml

from workshop_customizer import teaching
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_PACKS = ("hr-default", "it-helpdesk", "maintenance")


def _case(cid: str, *, set_: str = "practice", category: str = "normal", actor: str = "user-001", tools=(), must=()):
    return {
        "id": cid, "label": cid.replace("-", " "), "query": f"question {cid}?", "category": category, "set": set_,
        "actorId": actor, "expected": {"requiredTools": list(tools), "mustMention": list(must)}, "basis": ["f1"],
        "provenance": "sa_synthetic",
    }


def _data(phenomena=None, *, stability=None, first=None):
    cases = [
        _case("a-normal"),
        _case("b-tool", tools=["lookup_thing"]),
        _case("h-holdout", set_="holdout"),
        _case("c-fix", tools=["retrieve_docs"], must=["alpha"], actor="u-c"),
        _case("d-gap", tools=["retrieve_docs"], must=["beta"], actor="u-d", category="boundary"),
        _case("e-fix", tools=["retrieve_docs"], must=["alpha", "gamma"], actor="u-e"),
        _case("f-refuse", category="prohibited"),
    ]
    data = {
        "id": "demo",
        "namespace": {},
        "evaluation": {"retrievalToolName": "retrieve_docs", "goldenSet": cases},
        "knowledge": {"documents": [
            {"id": "doc-answer", "title": "Answer", "file": "docs/answer.md", "provenance": "sa_synthetic"},
            {"id": "doc-noise", "title": "Noise", "file": "docs/noise.md", "provenance": "sa_synthetic", "noise": True},
            {"id": "doc-other", "title": "Other", "file": "docs/other.md", "provenance": "sa_synthetic", "noise": True},
        ]},
        "labs": {},
    }
    if phenomena is not None:
        block = {
            "firstConversation": first or {"query": "What applies to me?", "actorId": "first-001", "unknownContext": ["site"]},
            "baselineDefects": [{"id": "d1", "description": "no grounding", "candidateFix": "Only answer from documents"}],
            "phenomena": phenomena,
        }
        if stability is not None:
            block["stabilityCaseId"] = stability
        data["labs"]["teaching"] = block
    return data


PHENOMENA = [
    {"id": "pf", "kind": "prompt_fixable", "caseIds": ["e-fix", "c-fix"], "defectIds": ["d1"], "teachingPoint": "t"},
    {"id": "gap", "kind": "retrieval_gap", "caseIds": ["d-gap"], "mechanism": "buried", "baitTerms": ["sale price", "Beta Plan"], "teachingPoint": "t"},
    {"id": "noise", "kind": "noise_grounding", "documentIds": ["doc-other"], "teachingPoint": "t"},
    {"id": "tool", "kind": "tool_use", "caseIds": ["b-tool"], "teachingPoint": "t"},
    {"id": "refuse", "kind": "refusal", "caseIds": ["f-refuse"], "design": "contrast", "teachingPoint": "t"},
]

TEXTS = {
    "doc-answer": "The Alpha rule is here.\n\nGamma applies too; beta is buried in paragraph two.",
    "doc-noise": "FAQ: what is the sale price? Nothing useful.\n\nBETA PLAN pricing is off-topic.",
    "doc-other": "Unrelated gamma notes.",
}


def test_teaching_module_imports_only_the_standard_library():
    """scenario.py / validator.py may import teaching without an import cycle."""
    import ast
    import sys

    tree = ast.parse(Path(teaching.__file__).read_text(encoding="utf-8"))
    modules = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    modules |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module}
    relative = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level > 0]
    assert not relative
    assert modules - {"__future__"} <= set(sys.stdlib_module_names)


# ---------------------------------------------------------------------------
# Undeclared packs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pack_id", REFERENCE_PACKS)
def test_reference_packs_without_teaching_derive_empty_teaching_and_practice_order(pack_id):
    data = yaml.safe_load((REPO_ROOT / "scenarios" / pack_id / "scenario.yaml").read_text(encoding="utf-8"))
    del data["labs"]["teaching"]  # the reference packs declare one since P1
    practice_ids = [c["id"] for c in data["evaluation"]["goldenSet"] if c["set"] == "practice"]
    assert teaching.block(data) is None and not teaching.declared(data)
    assert teaching.phenomena(data) == [] and teaching.case_kinds(data) == {}
    assert teaching.probe_case_ids(data) == ()
    assert teaching.stability_case_id(data) is None
    assert list(teaching.eval_order_ids(data)) == practice_ids
    assert teaching.probe_start_index(data) == len(practice_ids)
    assert teaching.first_conversation(data) is None
    assert teaching.teaching_document_ids(data) == ()


# ---------------------------------------------------------------------------
# Probes, stability case, eval order
# ---------------------------------------------------------------------------


def test_probes_are_prompt_fixable_and_retrieval_gap_practice_cases_in_golden_order():
    data = _data(PHENOMENA)
    assert teaching.probe_case_ids(data) == ("c-fix", "d-gap", "e-fix")


def test_eval_order_puts_non_probes_first_and_the_default_stability_case_last():
    data = _data(PHENOMENA)
    assert teaching.stability_case_id(data) == "e-fix"  # last prompt_fixable probe in goldenSet order
    assert teaching.eval_order_ids(data) == ("a-normal", "b-tool", "f-refuse", "c-fix", "d-gap", "e-fix")
    assert teaching.probe_start_index(data) == 3


def test_declared_stability_case_is_asked_last():
    data = _data(PHENOMENA, stability="c-fix")
    assert teaching.stability_case_id(data) == "c-fix"
    assert teaching.eval_order_ids(data) == ("a-normal", "b-tool", "f-refuse", "d-gap", "e-fix", "c-fix")


def test_a_non_probe_stability_case_falls_back_to_the_default():
    data = _data(PHENOMENA, stability="b-tool")
    assert teaching.stability_case_id(data) == "e-fix"
    assert teaching.eval_order_ids(data)[-1] == "e-fix"
    assert teaching.eval_order_ids(data).count("b-tool") == 1


def test_without_a_prompt_fixable_probe_there_is_no_default_stability_case():
    data = _data([p for p in PHENOMENA if p["kind"] != "prompt_fixable"])
    assert teaching.probe_case_ids(data) == ("d-gap",)
    assert teaching.stability_case_id(data) is None
    assert teaching.eval_order_ids(data)[-1] == "d-gap"


def test_probe_ids_are_deduplicated_and_never_include_holdout_cases():
    phenomena = copy.deepcopy(PHENOMENA)
    phenomena[0]["caseIds"] = ["c-fix", "c-fix", "h-holdout"]
    phenomena[1]["caseIds"] = ["c-fix", "d-gap"]  # double role: still one probe
    data = _data(phenomena)
    assert teaching.probe_case_ids(data) == ("c-fix", "d-gap")
    assert "h-holdout" not in teaching.eval_order_ids(data)


@pytest.mark.parametrize("stability", [None, "c-fix", "d-gap", "e-fix", "a-normal", "missing"])
def test_eval_order_is_always_a_permutation_of_the_practice_set(stability):
    data = _data(PHENOMENA, stability=stability)
    practice = [c["id"] for c in teaching.practice_cases(data)]
    order = teaching.eval_order_ids(data)
    assert sorted(order) == sorted(practice) and len(order) == len(practice)
    probes = set(teaching.probe_case_ids(data))
    k = teaching.probe_start_index(data)
    assert all(cid not in probes for cid in order[:k]) and all(cid in probes for cid in order[k:])


def test_kind_helpers_and_design_default():
    data = _data(PHENOMENA)
    assert [p["id"] for p in teaching.phenomena(data, "prompt_fixable")] == ["pf"]
    assert [p["id"] for p in teaching.phenomena(data, teaching.L1_KINDS)] == ["tool", "refuse"]
    assert teaching.case_kinds(data)["e-fix"] == ("prompt_fixable",)
    assert teaching.case_kinds(data)["f-refuse"] == ("refusal",)
    designs = {p["id"]: teaching.design(p) for p in teaching.phenomena(data)}
    assert designs == {"pf": None, "gap": None, "noise": None, "tool": "control", "refuse": "contrast"}
    assert set(teaching.KINDS) == set(teaching.PROBE_KINDS) | set(teaching.L1_KINDS) | set(teaching.ADVISORY_KINDS)


def test_first_conversation_is_the_declared_block():
    first = {"query": "What applies to me?", "actorId": "first-001", "unknownContext": ["site", "shift"], "label": "my shift"}
    assert teaching.first_conversation(_data(PHENOMENA, first=first)) == first


def test_derivations_hold_on_a_schema_valid_reference_pack_with_teaching(tmp_path: Path):
    """The same derivations on a real pack whose labs.teaching passes schema and cross-references."""
    root = tmp_path / "maintenance"
    shutil.copytree(REPO_ROOT / "scenarios" / "maintenance", root)
    data = yaml.safe_load((root / "scenario.yaml").read_text(encoding="utf-8"))
    data.setdefault("labs", {})["teaching"] = {
        "firstConversation": {"query": "What should I check first on my line today?", "actorId": "first-001",
                              "unknownContext": ["assigned line", "shift"], "label": "your shift"},
        "baselineDefects": [{"id": "no-grounding", "description": "No grounding rule.", "candidateFix": "Only answer from retrieved documents"}],
        "phenomena": [
            {"id": "pf-lube", "kind": "prompt_fixable", "caseIds": ["filler-lubrication-interval", "overdue-lubrication-check"],
             "defectIds": ["no-grounding"], "teachingPoint": "Grounding rules raise GR."},
            {"id": "gap-p1", "kind": "retrieval_gap", "caseIds": ["p1-response-target"], "mechanism": "absent",
             "absentTerms": ["escalation matrix"], "teachingPoint": "The KB lacks it."},
            {"id": "noise", "kind": "noise_grounding", "documentIds": ["spare-parts"], "teachingPoint": "Noise lowers SP2."},
            {"id": "tool", "kind": "tool_use", "caseIds": ["own-work-order-status"], "teachingPoint": "Tools answer own data."},
            {"id": "refuse", "kind": "refusal", "caseIds": ["interlock-bypass-request"], "design": "contrast", "teachingPoint": "Refuse bypasses."},
        ],
    }
    (root / "scenario.yaml").unlink()
    (root / "scenario.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    scenario = load_scenario(root / "scenario.json")
    d = scenario.data
    assert teaching.probe_case_ids(d) == ("filler-lubrication-interval", "p1-response-target", "overdue-lubrication-check")
    assert teaching.stability_case_id(d) == "overdue-lubrication-check"
    assert teaching.eval_order_ids(d) == (
        "cap-steriliser-uv-lamps", "own-work-order-status", "interlock-bypass-request",
        "filler-lubrication-interval", "p1-response-target", "overdue-lubrication-check",
    )
    assert teaching.probe_start_index(d) == 3
    assert teaching.teaching_document_ids(d, scenario.root) == ("spare-parts",)
    answer = teaching.answer_documents(d, "filler-lubrication-interval", scenario.root)
    assert answer.terms == ("250",) and answer.answer == ("preventive-maintenance",) and answer.missing_terms == ()


# ---------------------------------------------------------------------------
# Lexical matching
# ---------------------------------------------------------------------------


def test_norm_folds_width_case_markdown_and_whitespace():
    assert teaching.norm("  **ＶＰＮ**  `Reset`\n\tNow ") == "vpn reset now"


@pytest.mark.parametrize(
    "text,term,expected",
    [
        ("within 14 days", "4", False),
        ("within 4 days", "4", True),
        ("environment", "nm", False),
        ("25 nm torque", "NM", True),
        ("请病假需要医疗证明", "病假", True),
        ("Use the **Beta  Plan** now", "beta plan", True),
        ("SLA-4h", "4h", True),
        ("40% weight", "40%", True),
        ("140% weight", "40%", False),
        ("anything", "", False),
        ("anything", "  ", False),
    ],
)
def test_contains_term_word_boundaries_for_ascii_substrings_for_cjk(text, term, expected):
    assert teaching.contains_term(text, term) is expected


def test_count_term():
    assert teaching.count_term("Sale price? The SALE price! resale price", "sale price") == 2
    assert teaching.count_term("病假 病假", "病假") == 2
    assert teaching.count_term("x", "") == 0


def test_document_texts_reads_only_listed_documents(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "answer.md").write_text("alpha", encoding="utf-8")
    (tmp_path / "docs" / "noise.md").write_text("sale price", encoding="utf-8")
    (tmp_path / "docs" / "stray.md").write_text("beta", encoding="utf-8")  # not listed: ignored
    data = _data(PHENOMENA)
    data["knowledge"]["documents"].append({"id": "escape", "title": "x", "file": "../outside.md", "provenance": "sa_synthetic"})
    texts = teaching.document_texts(data, tmp_path)
    assert texts == {"doc-answer": "alpha", "doc-noise": "sale price"}  # doc-other missing, escape skipped


# ---------------------------------------------------------------------------
# Answer and teaching documents
# ---------------------------------------------------------------------------


def test_answer_documents_for_a_prompt_fixable_case():
    data = _data(PHENOMENA)
    found = teaching.answer_documents(data, "e-fix", texts=TEXTS)
    assert found == teaching.AnswerDocuments(
        case_id="e-fix", terms=("alpha", "gamma"), answer=("doc-answer", "doc-other"), complete=("doc-answer",),
        missing_terms=(), bait_terms=(), bait=(),
    )


def test_answer_documents_for_a_buried_gap_reports_bait_in_noise_documents_only():
    data = _data(PHENOMENA)
    texts = dict(TEXTS, **{"doc-answer": TEXTS["doc-answer"] + " Also the sale price."})
    found = teaching.answer_documents(data, data["evaluation"]["goldenSet"][4], texts=texts)
    assert found.case_id == "d-gap" and found.terms == ("beta",)
    # 'beta' is a whole word inside the noise document's 'BETA PLAN' too: both carry the answer term.
    assert found.answer == ("doc-answer", "doc-noise")
    assert found.bait_terms == ("sale price", "Beta Plan")
    assert found.bait == ("doc-noise",)  # doc-answer mentions the bait but is not a noise document


def test_answer_documents_reports_missing_terms_and_rejects_unknown_input():
    data = _data(PHENOMENA)
    data["evaluation"]["goldenSet"][3]["expected"]["mustMention"] = ["alpha", "omega"]
    found = teaching.answer_documents(data, "c-fix", texts=TEXTS)
    assert found.answer == ("doc-answer",) and found.complete == () and found.missing_terms == ("omega",)
    with pytest.raises(KeyError):
        teaching.answer_documents(data, "nope", texts=TEXTS)
    with pytest.raises(ValueError):
        teaching.answer_documents(data, "c-fix")


def test_teaching_document_ids_union_of_noise_grounding_and_bait_documents():
    data = _data(PHENOMENA)
    assert teaching.teaching_document_ids(data, texts=TEXTS) == ("doc-noise", "doc-other")
    # Without document text only the declared noise_grounding documents are known.
    assert teaching.teaching_document_ids(data) == ("doc-other",)
    no_bait = _data([p for p in PHENOMENA if p["kind"] != "retrieval_gap"])
    assert teaching.teaching_document_ids(no_bait, texts=TEXTS) == ("doc-other",)


# ---------------------------------------------------------------------------
# THRESHOLDS
# ---------------------------------------------------------------------------


def test_thresholds_match_spec_d2_and_are_read_only():
    assert dict(teaching.THRESHOLDS) == {
        "grPass": 0.7, "retrievalFailedSp2": 0.2, "retrievalFailedSqc": 0.3, "retrievalOkSqc": 0.5,
        "minImprovement": 0.05, "maxUsableBand": 0.25,
    }
    with pytest.raises(TypeError):
        teaching.THRESHOLDS["grPass"] = 0.5  # type: ignore[index]


@pytest.mark.parametrize(
    "metrics,expected",
    [
        ({"SP2": 0.2, "SQC": 0.9}, True),  # SP2 bound is inclusive
        ({"SP2": 0.21, "SQC": 0.3}, False),  # SQC bound is strict
        ({"SP2": 0.9, "SQC": 0.29}, True),
        ({"SQC": 0.1}, True),
        ({"SP2": 0.5}, False),
        ({"GR": 0.1}, None),
        ({"SP2": True, "SQC": None}, None),  # booleans are not scores
        (None, None),
    ],
)
def test_retrieval_failed(metrics, expected):
    assert teaching.retrieval_failed(metrics) is expected


def test_retrieval_ok_improvement_and_noise_band():
    assert teaching.retrieval_ok({"SQC": 0.2}, {"SQC": 0.5}) is True
    assert teaching.retrieval_ok({"SQC": 0.49}, {}) is False
    assert teaching.retrieval_ok({}, None) is None
    assert teaching.improvement_bar(None) == 0.05 and teaching.improvement_bar(0.02) == 0.05
    assert teaching.improvement_bar(0.1) == 0.1
    assert not teaching.is_improvement(0.05, None) and teaching.is_improvement(0.051, None)
    assert not teaching.is_improvement(0.1, 0.1) and teaching.is_improvement(0.11, 0.1)
    assert not teaching.judge_too_noisy(0.25) and teaching.judge_too_noisy(0.26) and not teaching.judge_too_noisy(None)
    assert teaching.gr_pass(0.7) and not teaching.gr_pass(0.69)
    assert teaching.JUDGE_TOO_NOISY == "JUDGE_TOO_NOISY"
