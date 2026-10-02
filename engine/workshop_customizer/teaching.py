"""Teaching-skeleton derivations over ``labs.teaching`` (pure, deterministic, no network).

``labs.teaching`` declares the moments a customized Workshop must reproduce: the first
conversation (the Memory lesson), the baseline defects the optimization candidate fixes, and the
teaching phenomena tied to practice cases or knowledge documents. This module derives everything
the engine needs from that declaration, in ONE place:

* **Probes** ``P`` — the practice cases listed by ``prompt_fixable`` and ``retrieval_gap``
  phenomena, in goldenSet order (:func:`probe_case_ids`). They are the questions whose retrieval
  traces 09/10 wait for and THELMA scores.
* **Eval order** — non-probe practice cases first, then the probes, with the stability case last
  so ``13-judge-stability.sh`` re-scores it (:func:`eval_order`).
* **Answer documents** — which knowledge documents carry a case's answer (and bait), found by
  lexical search on ``expected.mustMention`` and the declared ``baitTerms``
  (:func:`answer_documents`). Documents are never declared as answers.
* **Teaching documents** — the documents that are deliberate teaching devices (noise, bait)
  (:func:`teaching_document_ids`).
* **THRESHOLDS** — the single set of verdict constants (SPEC D2), with the comparison helpers that
  fix which bounds are inclusive. Rehearsal and the run report read them; packs cannot override.
* **Views** — what the build records: the student-safe ``teaching`` object of ``pack/pack.json``
  (:func:`student_view`), ``instructor/teaching.json`` (:func:`instructor_view`) and RELEASE.json
  ``teaching`` (:func:`release_view`), all over :func:`derived_order`.

Validation rules over the declaration (``teaching.*`` findings) live in :mod:`teaching_policy`.
Every function tolerates an undeclared block (it returns empty results or ``None``).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

# Stdlib only and no engine imports: scenario.py (provenance) and validator.py (policy) may import
# this module without an import cycle.

KINDS: tuple[str, ...] = ("prompt_fixable", "retrieval_gap", "noise_grounding", "tool_use", "refusal", "escalation")
#: Kinds whose cases are retrieval probes (THELMA-scored in 09/10).
PROBE_KINDS: tuple[str, ...] = ("prompt_fixable", "retrieval_gap")
#: Kinds judged by L1 focus checks; they may carry ``design`` (contrast|control).
L1_KINDS: tuple[str, ...] = ("tool_use", "refusal", "escalation")
#: Kinds that never decide the class verdict.
ADVISORY_KINDS: tuple[str, ...] = ("noise_grounding",)
MECHANISMS: tuple[str, ...] = ("buried", "absent")
DESIGNS: tuple[str, ...] = ("contrast", "control")
DEFAULT_DESIGN = "control"

#: Unified verdict constants (SPEC D2 / reviews[0].notes §7). Read-only; packs cannot override.
#:
#: * GR pass: ``GR >= grPass``.
#: * retrieval failed: ``SP2 <= retrievalFailedSp2`` or ``SQC < retrievalFailedSqc``.
#: * retrieval ok (prompt_fixable): ``max(baseline SQC, optimized SQC) >= retrievalOkSqc``.
#: * improvement: ``optimized GR - baseline GR > max(noise band, minImprovement)``.
#: * judge too noisy: ``noise band > maxUsableBand`` makes the evidence insufficient.
THRESHOLDS: Mapping[str, float] = MappingProxyType(
    {
        "grPass": 0.7,
        "retrievalFailedSp2": 0.2,
        "retrievalFailedSqc": 0.3,
        "retrievalOkSqc": 0.5,
        "minImprovement": 0.05,
        "maxUsableBand": 0.25,
    }
)
#: Reason code when the judge noise band is above ``THRESHOLDS['maxUsableBand']``.
JUDGE_TOO_NOISY = "JUDGE_TOO_NOISY"


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def block(data: dict[str, Any]) -> dict[str, Any] | None:
    """The ``labs.teaching`` object, or ``None`` when the pack does not declare one."""
    teaching = (data.get("labs") or {}).get("teaching")
    return teaching if isinstance(teaching, dict) else None


def declared(data: dict[str, Any]) -> bool:
    return block(data) is not None


def phenomena(data: dict[str, Any], kind: str | Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Declared phenomena in declaration order, optionally filtered by one kind or several."""
    teaching = block(data)
    if teaching is None:
        return []
    items = [p for p in teaching.get("phenomena") or [] if isinstance(p, dict)]
    if kind is None:
        return items
    kinds = {kind} if isinstance(kind, str) else set(kind)
    return [p for p in items if p.get("kind") in kinds]


def design(phenomenon: dict[str, Any]) -> str | None:
    """``contrast`` or ``control`` for tool_use/refusal/escalation (default control); ``None`` otherwise."""
    if phenomenon.get("kind") not in L1_KINDS:
        return None
    value = phenomenon.get("design")
    return value if value in DESIGNS else DEFAULT_DESIGN


def first_conversation(data: dict[str, Any]) -> dict[str, Any] | None:
    """The declared first conversation (06; not a golden case), or ``None``."""
    teaching = block(data)
    value = teaching.get("firstConversation") if teaching else None
    return value if isinstance(value, dict) else None


def practice_cases(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Practice golden cases in scenario order."""
    return [c for c in data.get("evaluation", {}).get("goldenSet", []) if isinstance(c, dict) and c.get("set") == "practice"]


def _case_ids(items: Iterable[dict[str, Any]]) -> set[str]:
    return {cid for p in items for cid in p.get("caseIds") or [] if isinstance(cid, str)}


def case_kinds(data: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """Case id → the kinds of the phenomena that list it (declaration order, de-duplicated)."""
    out: dict[str, list[str]] = {}
    for phenomenon in phenomena(data):
        kind = phenomenon.get("kind")
        for cid in phenomenon.get("caseIds") or []:
            kinds = out.setdefault(cid, [])
            if kind not in kinds:
                kinds.append(kind)
    return {cid: tuple(kinds) for cid, kinds in out.items()}


# ---------------------------------------------------------------------------
# Derived sets and order
# ---------------------------------------------------------------------------


def probe_case_ids(data: dict[str, Any]) -> tuple[str, ...]:
    """P: practice cases listed by prompt_fixable ∪ retrieval_gap, de-duplicated, in goldenSet order.

    Holdout ids never enter P (the scenario cross-references already reject them).
    """
    listed = _case_ids(phenomena(data, PROBE_KINDS))
    return tuple(c["id"] for c in practice_cases(data) if c["id"] in listed)


def stability_case_id(data: dict[str, Any]) -> str | None:
    """The probe asked last so 13-judge-stability re-scores it.

    The declared ``stabilityCaseId`` when it is a probe; otherwise the last prompt_fixable probe in
    goldenSet order; ``None`` when there is no such probe.
    """
    probes = probe_case_ids(data)
    requested = (block(data) or {}).get("stabilityCaseId")
    if requested in probes:
        return requested
    fixable = _case_ids(phenomena(data, "prompt_fixable"))
    for cid in reversed(probes):
        if cid in fixable:
            return cid
    return None


def eval_order(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Practice cases in the order 09/10/12 ask them: non-probes, probes, stability case last.

    Every practice case appears exactly once. Without a teaching block this is the practice order.
    """
    practice = practice_cases(data)
    probes = set(probe_case_ids(data))
    stability = stability_case_id(data)
    non_probes = [c for c in practice if c["id"] not in probes]
    ordered = [c for c in practice if c["id"] in probes and c["id"] != stability]
    ordered += [c for c in practice if c["id"] == stability]
    return non_probes + ordered


def eval_order_ids(data: dict[str, Any]) -> tuple[str, ...]:
    return tuple(c["id"] for c in eval_order(data))


def probe_start_index(data: dict[str, Any]) -> int:
    """Index of the first probe in :func:`eval_order` (= the number of non-probe practice cases)."""
    return len(practice_cases(data)) - len(probe_case_ids(data))


def derived_order(data: dict[str, Any]) -> dict[str, Any]:
    """The derived order every release artifact records (pack.json, RELEASE.json, instructor/teaching.json)."""
    return {
        "evalOrder": list(eval_order_ids(data)),
        "probeCaseIds": list(probe_case_ids(data)),
        "probeStartIndex": probe_start_index(data),
        "stabilityCaseId": stability_case_id(data),
    }


#: labs.teaching.firstConversation keys that ship to students (06 prints them).
STUDENT_FIRST_CONVERSATION_KEYS: tuple[str, ...] = ("query", "actorId", "unknownContext", "label")


def student_view(data: dict[str, Any]) -> dict[str, Any] | None:
    """The student-safe teaching object of pack/pack.json (designs[0] derived artifacts), or ``None``.

    The first conversation 06 asks, the derived order, and each phenomenon's id, kind, practice
    caseIds (documentIds for noise_grounding) and teachingPoint. Baseline defects, baitTerms,
    absentTerms, mechanisms, designs and the first conversation's teachingPoint / mustNotMention stay
    instructor-only (instructor/teaching.json).
    """
    if block(data) is None:
        return None
    first = first_conversation(data) or {}
    practice = {c["id"] for c in practice_cases(data)}
    items: list[dict[str, Any]] = []
    for phenomenon in phenomena(data):
        item: dict[str, Any] = {"id": phenomenon.get("id"), "kind": phenomenon.get("kind")}
        if phenomenon.get("kind") == "noise_grounding":
            item["documentIds"] = [d for d in phenomenon.get("documentIds") or [] if isinstance(d, str)]
        else:
            item["caseIds"] = [c for c in phenomenon.get("caseIds") or [] if c in practice]
        if isinstance(phenomenon.get("teachingPoint"), str):
            item["teachingPoint"] = phenomenon["teachingPoint"]
        items.append(item)
    view: dict[str, Any] = {"firstConversation": {k: first[k] for k in STUDENT_FIRST_CONVERSATION_KEYS if k in first}}
    view.update(derived_order(data))
    view["phenomena"] = items
    return view


def instructor_view(data: dict[str, Any]) -> dict[str, Any] | None:
    """instructor/teaching.json: the full declared block, the derived order and the verdict thresholds."""
    teaching = block(data)
    if teaching is None:
        return None
    return {
        "schema": "workshop-customizer/teaching/1",
        "labsTeaching": teaching,
        **derived_order(data),
        "designs": {p["id"]: design(p) for p in phenomena(data) if design(p) is not None and isinstance(p.get("id"), str)},
        "thresholds": dict(THRESHOLDS),
    }


def release_view(data: dict[str, Any]) -> dict[str, Any] | None:
    """RELEASE.json ``teaching``: the derived order and the first conversation's actor."""
    if block(data) is None:
        return None
    view = derived_order(data)
    view["firstConversationActorId"] = (first_conversation(data) or {}).get("actorId")
    return view


# ---------------------------------------------------------------------------
# Lexical matching (shared by every term check)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=256)
def norm(text: str) -> str:
    """NFKC, casefolded, markdown emphasis/code marks dropped, whitespace collapsed."""
    folded = unicodedata.normalize("NFKC", text).casefold().replace("*", "").replace("`", "")
    return re.sub(r"\s+", " ", folded).strip()


def _is_ascii_alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


@lru_cache(maxsize=1024)
def _term_pattern(term: str) -> re.Pattern[str] | None:
    t = norm(term)
    if not t:
        return None
    pattern = re.escape(t)
    if _is_ascii_alnum(t[0]):
        pattern = r"(?<![0-9a-z])" + pattern
    if _is_ascii_alnum(t[-1]):
        pattern += r"(?![0-9a-z])"
    return re.compile(pattern)


def count_term(text: str, term: str) -> int:
    """Occurrences of ``term`` in ``text`` after :func:`norm`.

    ASCII alphanumeric edges match at word boundaries ('4' is not in '14', 'nm' is not in
    'environment'); CJK terms match as substrings.
    """
    pattern = _term_pattern(term)
    return len(pattern.findall(norm(text))) if pattern else 0


def contains_term(text: str, term: str) -> bool:
    pattern = _term_pattern(term)
    return bool(pattern and pattern.search(norm(text)))


def document_texts(data: dict[str, Any], root: Path | str) -> dict[str, str]:
    """Knowledge document id → file text, for the documents in ``knowledge.documents`` only.

    Stray files under the docs directory are ignored; unreadable or escaping paths are skipped
    (the scenario loader reports those).
    """
    base = Path(root).resolve()
    texts: dict[str, str] = {}
    for doc in data.get("knowledge", {}).get("documents", []):
        path = (base / doc["file"]).resolve()
        if path != base and base not in path.parents:
            continue  # escapes the scenario root
        if path.is_file():
            texts[doc["id"]] = path.read_text(encoding="utf-8", errors="replace")
    return texts


def documents_containing(texts: Mapping[str, str], term: str, *, order: Iterable[str] | None = None) -> tuple[str, ...]:
    """Document ids (in ``order``, default the mapping order) whose text contains ``term``."""
    ids = list(order) if order is not None else list(texts)
    return tuple(did for did in ids if did in texts and contains_term(texts[did], term))


# ---------------------------------------------------------------------------
# Answer and teaching documents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnswerDocuments:
    """Where a case's answer (and bait) lives in the knowledge base, by lexical search."""

    case_id: str
    terms: tuple[str, ...]  # answer terms searched: expected.mustMention
    answer: tuple[str, ...]  # documents containing at least one answer term
    complete: tuple[str, ...]  # documents containing every answer term
    missing_terms: tuple[str, ...]  # answer terms that appear in no document
    bait_terms: tuple[str, ...]  # baitTerms of the retrieval_gap phenomena listing this case
    bait: tuple[str, ...]  # noise documents (noise: true) containing at least one bait term


def _texts(data: dict[str, Any], root: Path | str | None, texts: Mapping[str, str] | None) -> Mapping[str, str]:
    if texts is not None:
        return texts
    if root is None:
        raise ValueError("document text lookups need the scenario root or pre-read document texts")
    return document_texts(data, root)


def _doc_order(data: dict[str, Any]) -> list[str]:
    return [d["id"] for d in data.get("knowledge", {}).get("documents", [])]


def _noise_doc_ids(data: dict[str, Any]) -> list[str]:
    return [d["id"] for d in data.get("knowledge", {}).get("documents", []) if d.get("noise") is True]


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return tuple(out)


def answer_documents(
    data: dict[str, Any],
    case: dict[str, Any] | str,
    root: Path | str | None = None,
    *,
    texts: Mapping[str, str] | None = None,
) -> AnswerDocuments:
    """The documents that carry ``case``'s answer and its declared bait (knowledge-document order).

    ``case`` is a golden case or its id. Pass the scenario ``root`` or pre-read ``texts``
    (see :func:`document_texts`).
    """
    if isinstance(case, str):
        found = next((c for c in data.get("evaluation", {}).get("goldenSet", []) if c.get("id") == case), None)
        if found is None:
            raise KeyError(f"unknown golden case '{case}'")
        case = found
    cid = case["id"]
    docs = _texts(data, root, texts)
    order = _doc_order(data)
    terms = _unique(t for t in (case.get("expected") or {}).get("mustMention", []) or [] if isinstance(t, str))
    hits = {term: set(documents_containing(docs, term, order=order)) for term in terms}
    answer = tuple(did for did in order if any(did in ids for ids in hits.values()))
    complete = tuple(did for did in order if terms and all(did in ids for ids in hits.values()))
    missing = tuple(term for term in terms if not hits[term])
    bait_terms = _unique(
        t
        for p in phenomena(data, "retrieval_gap")
        if cid in (p.get("caseIds") or [])
        for t in p.get("baitTerms") or []
        if isinstance(t, str)
    )
    noise = _noise_doc_ids(data)
    bait = tuple(did for did in noise if did in docs and any(contains_term(docs[did], t) for t in bait_terms))
    return AnswerDocuments(
        case_id=cid, terms=terms, answer=answer, complete=complete, missing_terms=missing, bait_terms=bait_terms, bait=bait
    )


def teaching_document_ids(
    data: dict[str, Any], root: Path | str | None = None, *, texts: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Documents that are deliberate teaching devices, sorted.

    The union of every noise_grounding ``documentIds`` and the noise documents (``noise: true``)
    containing any declared ``baitTerm``. The bait part needs document text: without ``root`` and
    ``texts`` only the declared noise_grounding documents are returned.
    """
    ids = {did for p in phenomena(data, "noise_grounding") for did in p.get("documentIds") or []}
    bait_terms = [t for p in phenomena(data, "retrieval_gap") for t in p.get("baitTerms") or [] if isinstance(t, str)]
    if bait_terms and (root is not None or texts is not None):
        docs = _texts(data, root, texts)
        for did in _noise_doc_ids(data):
            if did in docs and any(contains_term(docs[did], t) for t in bait_terms):
                ids.add(did)
    return tuple(sorted(ids))


# ---------------------------------------------------------------------------
# Threshold comparisons (the only encoding of the THRESHOLDS bounds)
# ---------------------------------------------------------------------------


def _metric(metrics: Mapping[str, Any] | None, key: str) -> float | None:
    value = metrics.get(key) if metrics else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def gr_pass(value: float) -> bool:
    return value >= THRESHOLDS["grPass"]


def retrieval_failed(metrics: Mapping[str, Any] | None) -> bool | None:
    """``SP2 <= 0.2`` or ``SQC < 0.3``; ``None`` when neither metric is present (unknown)."""
    sp2, sqc = _metric(metrics, "SP2"), _metric(metrics, "SQC")
    if sp2 is None and sqc is None:
        return None
    return (sp2 is not None and sp2 <= THRESHOLDS["retrievalFailedSp2"]) or (
        sqc is not None and sqc < THRESHOLDS["retrievalFailedSqc"]
    )


def retrieval_ok(baseline: Mapping[str, Any] | None, optimized: Mapping[str, Any] | None) -> bool | None:
    """``max(baseline SQC, optimized SQC) >= 0.5``; ``None`` when neither run has SQC."""
    values = [v for v in (_metric(baseline, "SQC"), _metric(optimized, "SQC")) if v is not None]
    return max(values) >= THRESHOLDS["retrievalOkSqc"] if values else None


def improvement_bar(band: float | None) -> float:
    """The GR delta an improvement must exceed: ``max(noise band, minImprovement)``."""
    return max(float(band) if band is not None else 0.0, THRESHOLDS["minImprovement"])


def is_improvement(delta: float, band: float | None) -> bool:
    return delta > improvement_bar(band)


def judge_too_noisy(band: float | None) -> bool:
    return band is not None and band > THRESHOLDS["maxUsableBand"]
