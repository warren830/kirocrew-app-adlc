"""O3 (c): the canonicalizer folds a golden ``basis`` entry that names a knowledge document onto facts, only
where that has one right answer; every other shape stays an ``xref`` finding for Kiro.

Live 2026-09-27 (build/live/gen-baseline-ops): ``gc-normal-error-code-meaning`` cited
``["fact-error-e217", "kb-error-code-handbook"]``; the pack did not load (xref), so the draft's teaching findings
only surfaced a round later.  Offline and deterministic.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest
import yaml

from workshop_customizer.scenario import CrossReferenceError, SchemaViolation, load_scenario

REPO = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("wc_routes_basis", REPO / "app" / "backend" / "routes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


gen = _load()

#: Every fact carries a ``source`` (the fold needs that); fact-e217's names no knowledge document.
FACTS = [
    {"id": "fact-e217", "statement": "E-217 means an incompatible scan file.", "source": "support call 2026-09-02 (brief)"},
    {"id": "fact-e503", "statement": "E-503 is a design-engine crash.", "source": "knowledge-base/docs/errors.md (Codes)"},
    {"id": "fact-p1", "statement": "P1 needs both conditions.", "source": "kb-escalation"},
    {"id": "fact-p2", "statement": "P2 is a single E-503.", "source": "knowledge-base/docs/escalation.md"},
]
DOCUMENTS = [
    {"id": "kb-errors", "title": "Error codes", "file": "knowledge-base/docs/errors.md"},
    {"id": "kb-escalation", "title": "Escalation", "file": "knowledge-base/docs/escalation.md"},
    {"id": "kb-faq", "title": "FAQ", "file": "knowledge-base/docs/faq.md"},
]


def _fold(basis: list[str], facts=FACTS, documents=DOCUMENTS) -> tuple[list[str], list[str]]:
    canon = gen._Canon()
    out = gen._normalise_basis(list(basis), where="evaluation.goldenSet[c].basis",
                               fact_ids=frozenset(f["id"] for f in facts),
                               document_facts=gen._document_facts(facts, documents), canon=canon)
    return out, canon.warnings


# ---------------------------------------------------------------------------
# positive: one right answer
# ---------------------------------------------------------------------------


def test_a_document_next_to_a_fact_is_dropped_when_no_fact_comes_from_it():
    """The live shape: the case already cites its fact; the document names no fact of its own."""
    out, warnings = _fold(["fact-e217", "kb-faq"])
    assert out == ["fact-e217"]
    assert len(warnings) == 1 and "dropped knowledge document 'kb-faq'" in warnings[0]


def test_a_document_that_is_the_source_of_exactly_one_fact_becomes_that_fact():
    out, warnings = _fold(["kb-errors"])  # fact-e503's source is the document's file (with a section note)
    assert out == ["fact-e503"] and "replaced by fact 'fact-e503'" in warnings[0]
    assert _fold(["knowledge-base/docs/errors.md"])[0] == ["fact-e503"]  # the file path names it too
    assert _fold(["fact-e217", "kb-errors"])[0] == ["fact-e217", "fact-e503"]  # in place, order kept


def test_a_document_whose_one_fact_is_already_cited_is_dropped_without_a_duplicate():
    out, warnings = _fold(["fact-e503", "kb-errors"])
    assert out == ["fact-e503"] and "already cites its fact 'fact-e503'" in warnings[0]
    assert _fold(["kb-errors", "knowledge-base/docs/errors.md"])[0] == ["fact-e503"]


def test_a_folded_fact_lets_a_second_factless_document_go():
    """[doc with one fact, doc with none]: after the first fold the basis cites a fact, so the second goes."""
    assert _fold(["kb-errors", "kb-faq"])[0] == ["fact-e503"]
    assert _fold(["kb-faq", "kb-errors"])[0] == ["fact-e503"]  # order-independent


# ---------------------------------------------------------------------------
# negative: left to Kiro (the xref finding asks for a judgement)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("basis", [
    ["fact-e217"],                   # already fact ids: untouched
    ["fact-e217", "fact-p1"],
    ["kb-faq"],                      # a document with no fact and no fact cited: which fact? Kiro decides
    ["kb-escalation"],               # two facts come from it (by id and by file): ambiguous
    ["fact-p1", "kb-escalation"],    # one of its two facts is cited: the other may be meant too
    ["fact-e217", "kb-escalation"],
    ["fact-e2l7"],                   # a mistyped fact id is not a document
    ["fact-e217", "no-such-thing"],
    ["knowledge-base/docs/faq"],     # not a document id or file
])
def test_ambiguous_or_unknown_entries_are_left_alone(basis):
    out, warnings = _fold(basis)
    assert out == basis and warnings == []


def test_a_key_that_names_two_documents_is_not_one_document():
    documents = DOCUMENTS + [{"id": "kb-errors", "title": "Duplicate id", "file": "knowledge-base/docs/errors-2.md"}]
    assert _fold(["kb-errors"], documents=documents) == (["kb-errors"], [])
    assert _fold(["knowledge-base/docs/errors.md"], documents=documents)[0] == ["fact-e503"]  # the file is still unique


def test_an_id_shared_by_a_fact_and_a_document_stays_a_fact():
    facts = FACTS + [{"id": "kb-faq", "statement": "A fact that reuses a document id (an xref error of its own)."}]
    assert _fold(["kb-faq"], facts=facts) == (["kb-faq"], [])


def _without_source(facts: list[dict], *ids: str) -> list[dict]:
    return [{k: v for k, v in f.items() if k != "source" or (ids and f["id"] not in ids)} for f in facts]


@pytest.mark.parametrize("facts", [
    _without_source(FACTS),                    # the live shape: Kiro drafts of 2026-09-28 wrote no source at all
    _without_source(FACTS, "fact-e217"),       # one fact without a source may come from any document
    [{**f, "source": ["knowledge-base/docs/errors.md"]} if f["id"] == "fact-e217" else f for f in FACTS],  # not a string
    [{**f, "source": "  "} if f["id"] == "fact-e217" else f for f in FACTS],                                # blank
], ids=["no-source", "one-missing", "not-a-string", "blank"])
@pytest.mark.parametrize("basis", [["fact-e217", "kb-faq"], ["kb-errors"], ["fact-e217", "kb-errors"], ["fact-p1", "kb-faq"]])
def test_nothing_folds_unless_every_fact_carries_a_string_source(facts, basis):
    """Without a source on every fact, 'no fact comes from this document' (the drop) and 'exactly one does' (the
    replacement) are guesses: the handbook may state fact-e217 and more.  The entry stays for the xref finding."""
    assert gen._document_facts(facts, DOCUMENTS) == {}
    assert _fold(basis, facts=facts) == (basis, [])


def test_the_reviewers_live_shape_keeps_every_fact_a_handbook_may_state():
    """Three facts from one handbook, none with a source: the basis is not trimmed to the one cited fact."""
    facts = [{"id": i, "statement": i} for i in ("fact-e217", "fact-e503", "fact-e118")]
    assert _fold(["fact-e217", "kb-errors"], facts=facts) == (["fact-e217", "kb-errors"], [])


@pytest.mark.parametrize("source", [
    "knowledge-base/docs/errors.md（错误码）", "knowledge-base/docs/errors.md (错误码)", "knowledge-base/docs/errors.md#codes",
    "knowledge-base/docs/errors.md", "(knowledge-base/docs/errors.md)", "见 knowledge-base/docs/errors.md，第 2 节",
    "errors.md（错误码）", "docs/errors.md; handbook", "kb-errors, section 2", "Error handbook: knowledge-base/docs/errors.md",
])
def test_a_source_names_its_document_up_to_any_non_path_character(source):
    facts = [{"id": "fact-a", "statement": "A", "source": source}, {"id": "fact-b", "statement": "B", "source": source},
             {"id": "fact-c", "statement": "C", "source": "customer interview"}]
    assert gen._document_facts(facts, DOCUMENTS)["kb-errors"] == ["fact-a", "fact-b"]
    # Two facts come from the document: the entry is ambiguous, never dropped (fact-b is not lost).
    assert _fold(["fact-a", "kb-errors"], facts=facts) == (["fact-a", "kb-errors"], [])
    assert _fold(["fact-c", "kb-errors"], facts=facts) == (["fact-c", "kb-errors"], [])
    one = [facts[0], {**facts[1], "source": "customer interview"}, facts[2]]
    assert _fold(["kb-errors"], facts=one)[0] == ["fact-a"]  # exactly one: replaced


@pytest.mark.parametrize("source", [
    "knowledge-base/docs/errors.md.bak", "knowledge-base/docs/errors.mdx", "knowledge-base/docs/errors-2.md",
    "kb-errors-2", "xkb-errors", "old-errors.md", "knowledge-base/docs/errors_md", "",
])
def test_a_longer_path_or_id_does_not_name_the_document(source):
    facts = [{"id": "fact-a", "statement": "A", "source": source or "customer interview"},
             {"id": "fact-b", "statement": "B", "source": "customer interview"}]
    assert gen._document_facts(facts, DOCUMENTS)["kb-errors"] == []
    assert _fold(["fact-b", "kb-errors"], facts=facts)[0] == ["fact-b"]  # every source known, none names it: dropped


def test_a_file_name_shared_by_two_documents_is_not_one_document():
    documents = DOCUMENTS + [{"id": "kb-errors-old", "title": "Old", "file": "knowledge-base/docs/archive/errors.md"}]
    document_facts = gen._document_facts(FACTS, documents)
    assert "errors.md" not in document_facts and document_facts["kb-errors"] == ["fact-e503"]
    assert _fold(["errors.md"], documents=documents) == (["errors.md"], [])


def test_the_contract_asks_for_the_fact_source_the_fold_reads():
    task = " ".join(gen._compose_task(project_id="basis-pack", display_name="Basis", pack_kind="reference", customer="",
                                      brief="A cold-chain incident assistant for warehouse operators.")[0].split())
    assert ('facts[]: id, statement, criticality=blocking|advisory, provenance, origin, source (the knowledge document '
            'file that states it, e.g. "knowledge-base/docs/x.md (Section)"') in task
    assert "A case that rests on a document cites the facts whose source is that document" in task


# ---------------------------------------------------------------------------
# through the bundle normaliser and the engine's cross-references
# ---------------------------------------------------------------------------


def _maintenance_payload() -> dict:
    root = REPO / "scenarios" / "maintenance"
    scenario = yaml.safe_load((root / "scenario.yaml").read_text(encoding="utf-8"))
    scenario.update(id="basis-pack", displayName="Basis Pack", packKind="reference")
    refs = {d["file"] for d in scenario["knowledge"]["documents"]} | {s["file"] for s in scenario["skills"]}
    refs |= {scenario["prompts"]["baselineFile"], scenario["prompts"]["optimizationCandidateFile"]}
    files = {rel: (root / rel).read_text(encoding="utf-8") for rel in refs}
    return {"status": "ready", "summary": "x", "openQuestions": [], "truthLedger": [], "scenario": scenario, "files": files}


def _cases(payload: dict) -> dict[str, dict]:
    return {c["id"]: c for c in payload["scenario"]["evaluation"]["goldenSet"]}


def _write_pack(tmp_path: Path, clean: dict) -> Path:
    root = tmp_path / "pack"
    for rel, content in clean["files"].items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content, encoding="utf-8")
    (root / "scenario.yaml").write_text(yaml.safe_dump(clean["scenario"], sort_keys=False, allow_unicode=True), encoding="utf-8")
    return root / "scenario.yaml"


def test_the_bundle_normaliser_folds_the_slip_and_the_pack_loads(tmp_path):
    payload = _maintenance_payload()
    cases = _cases(payload)
    cases["t301-first-response"]["basis"] = ["fault-codes"]  # the only fact from fault_codes.md
    cases["custom-part-lead-time"]["basis"] = ["spare-parts-rules", "knowledge-base/docs/spare_parts.md"]
    clean = gen._normalise_bundle(payload, project_id="basis-pack", pack_kind="reference")
    folded = {c["id"]: c["basis"] for c in clean["scenario"]["evaluation"]["goldenSet"]}
    assert folded["t301-first-response"] == ["fault-code-first-response"]
    assert folded["custom-part-lead-time"] == ["spare-parts-rules"]
    assert sum("knowledge document" in w for w in clean["warnings"]) == 2
    loaded = load_scenario(_write_pack(tmp_path, clean), enforce_gate=False)  # no CrossReferenceError
    assert loaded.data["evaluation"]["goldenSet"]


def test_a_draft_whose_facts_have_no_source_folds_nothing(tmp_path):
    """The live shape (loyalty-points, ops-support, gen-baseline-ops: no fact has a source): the slips the
    sourced template folds all stay, so the xref finding asks Kiro, and no document entry is silently dropped."""
    payload = _maintenance_payload()
    for fact in payload["scenario"]["facts"]:
        fact.pop("source")
    cases = _cases(payload)
    cases["t301-first-response"]["basis"] = ["fault-codes"]
    cases["custom-part-lead-time"]["basis"] = ["spare-parts-rules", "knowledge-base/docs/spare_parts.md"]
    cases["filler-lubrication-interval"]["basis"] = ["pm-intervals-filler", "preventive-maintenance"]
    clean = gen._normalise_bundle(payload, project_id="basis-pack", pack_kind="reference")
    folded = {c["id"]: c["basis"] for c in clean["scenario"]["evaluation"]["goldenSet"]}
    assert folded["t301-first-response"] == ["fault-codes"]
    assert folded["custom-part-lead-time"] == ["spare-parts-rules", "knowledge-base/docs/spare_parts.md"]
    assert folded["filler-lubrication-interval"] == ["pm-intervals-filler", "preventive-maintenance"]
    assert not any("knowledge document" in w for w in clean["warnings"])
    # The pack does not load, so Kiro gets the finding: the path fails the basis id pattern first ...
    with pytest.raises(SchemaViolation, match="knowledge-base/docs/spare_parts.md"):
        load_scenario(_write_pack(tmp_path / "a", clean), enforce_gate=False)
    # ... and the document ids fail the cross-references.
    cases = _cases(clean)
    cases["custom-part-lead-time"]["basis"] = ["spare-parts-rules"]
    with pytest.raises(CrossReferenceError, match="cites unknown fact '(fault-codes|preventive-maintenance)'"):
        load_scenario(_write_pack(tmp_path / "b", clean), enforce_gate=False)


def test_an_ambiguous_document_basis_still_fails_the_cross_references(tmp_path):
    payload = _maintenance_payload()
    _cases(payload)["loto-steps-question"]["basis"] = ["lockout-tagout"]  # three facts come from lockout_tagout.md
    clean = gen._normalise_bundle(payload, project_id="basis-pack", pack_kind="reference")
    assert _cases(clean)["loto-steps-question"]["basis"] == ["lockout-tagout"]
    assert not any("knowledge document" in w for w in clean["warnings"])
    with pytest.raises(CrossReferenceError, match="cites unknown fact 'lockout-tagout'"):
        load_scenario(_write_pack(tmp_path, clean), enforce_gate=False)


def test_a_reviewed_case_whose_basis_is_folded_loses_its_review():
    """Fail-safe: the fold is a content change, so an SA review never survives on a basis it did not see."""
    payload = _maintenance_payload()
    _cases(payload)["t301-first-response"]["basis"] = ["fact-that-is-not", "fault-codes"]
    current = copy.deepcopy(payload["scenario"])
    for case in current["evaluation"]["goldenSet"]:
        case["provenance"] = "sa_synthetic"
    unchanged = copy.deepcopy(_cases(payload)["filler-lubrication-interval"])
    clean = gen._normalise_bundle(payload, project_id="basis-pack", pack_kind="reference", current=current)
    reset = {row["id"] for row in clean["diff"]["provenance"]["reset"] if row["type"] == "golden"}
    assert "t301-first-response" in reset and _cases(clean)["t301-first-response"]["provenance"] == "ai_draft"
    assert _cases(clean)["t301-first-response"]["basis"] == ["fact-that-is-not", "fault-code-first-response"]
    assert unchanged["basis"] == _cases(clean)["filler-lubrication-interval"]["basis"]
    assert "filler-lubrication-interval" not in reset  # an untouched reviewed case keeps its review
