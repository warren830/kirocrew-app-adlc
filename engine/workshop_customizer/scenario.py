"""Scenario loading, schema validation, cross-reference checks and the provenance gate.

The scenario document (``scenario.yaml`` / ``scenario.json``) is the single business
truth source of a pack. Three independent checks run before anything is generated:

1. **Schema** — structural validation against ``schemas/scenario.schema.json`` (v1).
2. **Cross-references** — ids are unique, golden cases cite existing facts, tool names
   referenced by cases exist, referenced files exist under the scenario root and stay
   inside it.
3. **Provenance gate** — fail closed on unconfirmed truth (SPEC D12):

   * ``ai_draft`` / ``pending`` items never enter a formal pack;
   * ``reference`` packs (internal / fictional, no customer) accept ``sa_synthetic`` for
     blocking items;
   * ``customer`` packs (the handoff / launch-grade kind) require ``customer_confirmed`` for
     every *blocking* fact, tool, golden case and knowledge document;
   * ``workshop`` packs (a real customer scenario taught in class) accept a blocking
     ``sa_synthetic`` item only when it is labeled with its ``origin`` (customer_material,
     sa_authored or teaching_design), and need at least one customer-confirmed *anchor* golden
     case per category (normal / boundary / prohibited) whose basis facts are all
     ``customer_confirmed``; the anchor rule cannot be waived;
   * noise documents are teaching devices (``noise: true`` + ``sa_synthetic`` + origin
     ``teaching_design``) and never need a customer confirmation, in any kind;
   * nothing may fabricate a confirmation: a ``customer_confirmed`` item whose ``confirmedBy``
     or ``confirmationRef`` looks simulated (``simulated``, ``e2e_generate``) is rejected in
     every kind and cannot be waived;
   * an exception waives one item only, must name two approvers, and is always reported.

Cross-references also resolve the optional teaching skeleton (``labs.teaching``), the L1
escalation tools (``evaluation.l1.escalationTools``) and the reserved ``guide-narrative`` id of
the optional guide narrative (``labs.guide``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

SCHEMA_PATH = Path(__file__).parent / "schemas" / "scenario.schema.json"
SUPPORTED_SCHEMA_VERSION = 1
FORMAL_PROVENANCE = frozenset({"customer_confirmed", "sa_synthetic"})
DRAFT_PROVENANCE = frozenset({"ai_draft", "pending"})
PACK_KINDS = ("reference", "customer", "workshop")
#: Pack kinds whose knowledge documents are blocking (the customer owns the source material).
STRICT_PACK_KINDS = frozenset({"customer", "workshop"})
#: Item id owned by ``labs.guide``; no fact, tool, document, role, skill or golden case may use it.
GUIDE_NARRATIVE_ID = "guide-narrative"
ORIGIN_KINDS = ("customer_material", "sa_authored", "teaching_design")
ANCHOR_CATEGORIES = ("normal", "boundary", "prohibited")
#: The gate item id of the (never waivable) workshop anchor rule.
ANCHOR_GATE_ID = "customer-anchors"
#: Derived item classes (never stored): how a guide or report may present an item.
ITEM_CLASSES = ("customer-fact", "derived-setting", "teaching-device", "synthetic-setting", "draft")
ITEM_CLASS_LABELS = {
    "customer-fact": "customer fact",
    "derived-setting": "synthetic teaching setting derived from customer materials",
    "teaching-device": "teaching device (synthetic teaching setting)",
    "synthetic-setting": "synthetic teaching setting",
    "draft": "draft (not reviewed)",
}
#: The same labels per pack language (SPEC D12: guides label non-customer items
#: "合成教学设定 / synthetic teaching setting", one per pack language); :func:`item_class_label` picks
#: one. The instructor truth sheet prints it; guide tags follow :func:`item_class` too.
ITEM_CLASS_LABELS_BY_LANGUAGE = {
    "en": ITEM_CLASS_LABELS,
    "zh-CN": {
        "customer-fact": "客户事实",
        "derived-setting": "合成教学设定（源自客户材料）",
        "teaching-device": "教学设计（合成教学设定）",
        "synthetic-setting": "合成教学设定",
        "draft": "草稿（未审核）",
    },
}
#: Confirmation text that marks a simulated review (a test or acceptance driver), never a customer's.
SIMULATED_CONFIRMATION_RE = re.compile(r"simulated|e2e_generate", re.IGNORECASE)
PROVENANCE_POLICY_SCHEMA = "workshop-customizer/provenance-policy/1"
PROVENANCE_POLICIES = {
    "reference": "internal or fictional pack: sa_synthetic is allowed for blocking items",
    "customer": "every blocking fact, tool, golden case and knowledge document is customer_confirmed",
    "workshop": ("blocking items are customer_confirmed or origin-labeled sa_synthetic settings; at least one "
                 "customer-confirmed anchor golden case per category with customer-confirmed basis facts"),
}


class ScenarioError(Exception):
    """Base class for scenario problems."""


class SchemaViolation(ScenarioError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("scenario schema violation:\n- " + "\n- ".join(errors))


class CrossReferenceError(ScenarioError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("scenario cross-reference errors:\n- " + "\n- ".join(errors))


@dataclass(frozen=True)
class GateViolation:
    item_type: str
    item_id: str
    reason: str
    waived: bool = False
    exception: dict[str, Any] | None = None

    def describe(self) -> str:
        prefix = "WAIVED " if self.waived else ""
        return f"{prefix}{self.item_type} '{self.item_id}': {self.reason}"


class ProvenanceGateError(ScenarioError):
    def __init__(self, violations: list[GateViolation]):
        self.violations = violations
        blocking = [v.describe() for v in violations if not v.waived]
        super().__init__("provenance gate blocked the pack:\n- " + "\n- ".join(blocking))


@dataclass
class Scenario:
    """A loaded, validated scenario."""

    path: Path
    root: Path
    data: dict[str, Any]
    gate_violations: list[GateViolation] = field(default_factory=list)

    @property
    def id(self) -> str:
        return str(self.data["id"])

    @property
    def pack_kind(self) -> str:
        return str(self.data["packKind"])

    @property
    def namespace(self) -> dict[str, str]:
        return dict(self.data["namespace"])

    @property
    def facts(self) -> list[dict[str, Any]]:
        return list(self.data["facts"])

    @property
    def tools(self) -> list[dict[str, Any]]:
        return list(self.data["tools"])

    @property
    def golden_set(self) -> list[dict[str, Any]]:
        return list(self.data["evaluation"]["goldenSet"])

    @property
    def waived_items(self) -> list[GateViolation]:
        return [v for v in self.gate_violations if v.waived]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def read_structured_file(path: Path) -> dict[str, Any]:
    """Read a YAML or JSON scenario document into a plain dict."""
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix in {".yaml", ".yml"}:
        import yaml  # local import keeps JSON-only environments working

        data = yaml.safe_load(text)
    elif suffix == ".json":
        data = json.loads(text)
    else:
        raise ScenarioError(f"unsupported scenario file type: {path.name} (use .yaml, .yml or .json)")
    if not isinstance(data, dict):
        raise ScenarioError(f"scenario document must be a mapping at the top level: {path}")
    return data


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _format_error(err: jsonschema.ValidationError) -> str:
    location = "/".join(str(p) for p in err.absolute_path) or "<root>"
    return f"{location}: {err.message}"


def validate_schema(data: dict[str, Any]) -> list[str]:
    """Return schema error strings (empty when valid)."""
    schema = load_schema()
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema, format_checker=validator_cls.FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    return [_format_error(e) for e in errors]


# ---------------------------------------------------------------------------
# Cross references
# ---------------------------------------------------------------------------


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for v in values:
        if v in seen and v not in dupes:
            dupes.append(v)
        seen.add(v)
    return dupes


def resolve_inside(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root`` and refuse anything that escapes the root."""
    root_resolved = root.resolve()
    candidate = (root_resolved / rel).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:  # pragma: no cover - defensive
        raise CrossReferenceError([f"path escapes scenario root: {rel}"]) from exc
    return candidate


def cross_reference_errors(
    data: dict[str, Any], root: Path | None = None, *, check_files: bool = True
) -> list[str]:
    """Semantic checks the JSON schema cannot express."""
    errors: list[str] = []

    fact_ids = [f["id"] for f in data.get("facts", [])]
    for dup in _duplicates(fact_ids):
        errors.append(f"duplicate fact id: {dup}")

    tool_names = [t["name"] for t in data.get("tools", [])]
    for dup in _duplicates(tool_names):
        errors.append(f"duplicate tool name: {dup}")

    doc_ids = [d["id"] for d in data.get("knowledge", {}).get("documents", [])]
    for dup in _duplicates(doc_ids):
        errors.append(f"duplicate knowledge document id: {dup}")

    skill_names = [s["name"] for s in data.get("skills", [])]
    for dup in _duplicates(skill_names):
        errors.append(f"duplicate skill name: {dup}")

    role_ids = [r["id"] for r in data.get("agent", {}).get("roles", [])]
    for dup in _duplicates(role_ids):
        errors.append(f"duplicate role id: {dup}")

    evaluation = data.get("evaluation", {})
    cases = evaluation.get("goldenSet", [])
    case_ids = [c["id"] for c in cases]
    for dup in _duplicates(case_ids):
        errors.append(f"duplicate golden case id: {dup}")

    all_ids = fact_ids + tool_names + doc_ids + skill_names + role_ids + case_ids
    for dup in _duplicates(all_ids):
        if dup not in _duplicates(fact_ids) + _duplicates(tool_names) + _duplicates(doc_ids) + _duplicates(
            skill_names
        ) + _duplicates(role_ids) + _duplicates(case_ids):
            errors.append(f"id used by more than one item type: {dup}")

    retrieval_tool = evaluation.get("retrievalToolName")
    retrieval_tools = {t["name"] for t in data.get("tools", []) if t.get("kind") == "retrieval"}
    if retrieval_tool and retrieval_tool not in retrieval_tools:
        errors.append(
            f"evaluation.retrievalToolName '{retrieval_tool}' is not a tool of kind 'retrieval'"
        )

    fact_set = set(fact_ids)
    tool_set = set(tool_names)
    role_set = set(role_ids)
    for case in cases:
        for fact_id in case.get("basis", []):
            if fact_id not in fact_set:
                errors.append(f"golden case '{case['id']}' cites unknown fact '{fact_id}'")
        expected = case.get("expected", {})
        for key in ("requiredTools", "forbiddenTools"):
            for name in expected.get(key, []):
                if name not in tool_set:
                    errors.append(f"golden case '{case['id']}' {key} references unknown tool '{name}'")
        role_id = case.get("roleId")
        if role_id and role_id not in role_set:
            errors.append(f"golden case '{case['id']}' references unknown role '{role_id}'")

    for item_id in sorted(set(all_ids)):
        if item_id == GUIDE_NARRATIVE_ID:
            errors.append(f"item id '{GUIDE_NARRATIVE_ID}' is reserved for the labs.guide narrative; rename the item")
    labs = data.get("labs") or {}
    for key in ("studentGuideFile", "instructorGuideFile"):
        if key in labs and not isinstance(labs.get("guide"), dict):
            errors.append(f"labs.{key}: guide supplements require labs.guide review (they are embedded in the generated guide)")
    errors.extend(_instructor_supplement_errors(data))

    l1 = evaluation.get("l1") or {}
    for name in l1.get("escalationTools", []) or []:
        if name not in tool_set:
            errors.append(f"evaluation.l1.escalationTools references unknown tool '{name}'")

    errors.extend(_teaching_reference_errors(data, set(doc_ids)))

    exception_ids = [e["itemId"] for e in data.get("governance", {}).get("exceptions", [])]
    for dup in _duplicates(exception_ids):
        errors.append(f"duplicate governance exception for item: {dup}")
    for item_id in exception_ids:
        if item_id not in set(all_ids):
            errors.append(f"governance exception references unknown item '{item_id}'")

    if check_files:
        if root is None:
            errors.append("check_files requested without a scenario root")
        else:
            referenced: list[tuple[str, str]] = []
            for doc in data.get("knowledge", {}).get("documents", []):
                referenced.append((f"knowledge document '{doc['id']}'", doc["file"]))
            for skill in data.get("skills", []):
                referenced.append((f"skill '{skill['name']}'", skill["file"]))
            prompts = data.get("prompts", {})
            for key in ("baselineFile", "optimizationCandidateFile"):
                if key in prompts:
                    referenced.append((f"prompts.{key}", prompts[key]))
            labs = data.get("labs", {})
            for key in ("studentGuideFile", "instructorGuideFile"):
                if key in labs:
                    referenced.append((f"labs.{key}", labs[key]))
            for label, rel in referenced:
                try:
                    target = resolve_inside(root, rel)
                except CrossReferenceError as exc:
                    errors.extend(exc.errors)
                    continue
                if not target.is_file():
                    errors.append(f"{label} file not found: {rel}")

    return errors


def _same_file(a: str, b: str) -> bool:
    """Two scenario-relative paths name the same file (normalised; letter case ignored, as on macOS APFS)."""
    import posixpath

    def norm(rel: str) -> str:
        return posixpath.normpath(rel.replace("\\", "/")).casefold()

    return norm(a) == norm(b)


def _instructor_supplement_errors(data: dict[str, Any]) -> list[str]:
    """labs.instructorGuideFile is instructor-only (holdout ids, answers): it may never also be a file the
    compiler ships to participants — a knowledge document, a skill, a prompt or the student supplement."""
    labs = data.get("labs") or {}
    supplement = labs.get("instructorGuideFile")
    if not isinstance(supplement, str):
        return []
    others: list[tuple[str, Any]] = [(f"the file of document '{d.get('id')}'", d.get("file"))
                                     for d in (data.get("knowledge") or {}).get("documents", []) or [] if isinstance(d, dict)]
    others += [(f"the file of skill {s.get('name')!r}", s.get("file")) for s in data.get("skills", []) or [] if isinstance(s, dict)]
    others += [(f"the prompt file {key}", value) for key, value in (data.get("prompts") or {}).items()]
    others.append(("labs.studentGuideFile", labs.get("studentGuideFile")))
    return [f"labs.instructorGuideFile is instructor-only and cannot also be {label} ({rel})"
            for label, rel in others if isinstance(rel, str) and _same_file(rel, supplement)]


def _teaching_reference_errors(data: dict[str, Any], doc_ids: set[str]) -> list[str]:
    """Id resolution for ``labs.teaching`` (its phenomenon and defect ids are their own namespaces)."""
    teaching = (data.get("labs") or {}).get("teaching")
    if not isinstance(teaching, dict):
        return []
    errors: list[str] = []
    cases = {c["id"]: c for c in data.get("evaluation", {}).get("goldenSet", []) if isinstance(c, dict) and "id" in c}
    defects = [d.get("id") for d in teaching.get("baselineDefects", []) or [] if isinstance(d, dict)]
    phenomena = [p for p in teaching.get("phenomena", []) or [] if isinstance(p, dict)]
    for dup in _duplicates([str(p.get("id")) for p in phenomena]):
        errors.append(f"duplicate labs.teaching phenomenon id: {dup}")
    for dup in _duplicates([str(d) for d in defects]):
        errors.append(f"duplicate labs.teaching baseline defect id: {dup}")
    defect_set = set(defects)
    for phenomenon in phenomena:
        pid = phenomenon.get("id")
        for cid in phenomenon.get("caseIds", []) or []:
            case = cases.get(cid)
            if case is None:
                errors.append(f"labs.teaching phenomenon '{pid}' references unknown golden case '{cid}'")
            elif case.get("set") != "practice":
                errors.append(
                    f"labs.teaching phenomenon '{pid}' uses holdout case '{cid}'; teaching phenomena are "
                    "demonstrated in class by 09/10 and must use practice cases"
                )
        for did in phenomenon.get("documentIds", []) or []:
            if did not in doc_ids:
                errors.append(f"labs.teaching phenomenon '{pid}' references unknown knowledge document '{did}'")
        for dfid in phenomenon.get("defectIds", []) or []:
            if dfid not in defect_set:
                errors.append(f"labs.teaching phenomenon '{pid}' references unknown baseline defect '{dfid}'")
    stability = teaching.get("stabilityCaseId")
    if stability is not None:
        case = cases.get(stability)
        if case is None:
            errors.append(f"labs.teaching.stabilityCaseId references unknown golden case '{stability}'")
        elif case.get("set") != "practice":
            errors.append(
                f"labs.teaching.stabilityCaseId '{stability}' is a holdout case; the stability case is asked "
                "in class and must be a practice case"
            )
    return errors


# ---------------------------------------------------------------------------
# Provenance gate
# ---------------------------------------------------------------------------


def _exceptions_by_item(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["itemId"]: e for e in data.get("governance", {}).get("exceptions", [])}


def _origin_kind(item: dict[str, Any]) -> str | None:
    origin = item.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    return kind if kind in ORIGIN_KINDS else None


def is_simulated_confirmation(item: dict[str, Any]) -> bool:
    """True when a ``customer_confirmed`` item's confirmation text marks a simulated review."""
    if item.get("provenance") != "customer_confirmed":
        return False
    return any(isinstance(item.get(key), str) and SIMULATED_CONFIRMATION_RE.search(item[key])
               for key in ("confirmedBy", "confirmationRef"))


def is_customer_confirmed(item: dict[str, Any]) -> bool:
    """A real customer confirmation (never a simulated one)."""
    return item.get("provenance") == "customer_confirmed" and not is_simulated_confirmation(item)


def is_noise_device(doc: dict[str, Any]) -> bool:
    """A noise document labeled as a teaching device: never needs a customer confirmation (D12)."""
    return doc.get("noise") is True and doc.get("provenance") == "sa_synthetic" and _origin_kind(doc) == "teaching_design"


def item_class(item: dict[str, Any]) -> str:
    """The derived class of a provenance-bearing item (see :data:`ITEM_CLASSES`)."""
    provenance = item.get("provenance")
    if provenance == "customer_confirmed":
        return "customer-fact" if not is_simulated_confirmation(item) else "draft"
    if provenance != "sa_synthetic":
        return "draft"
    kind = _origin_kind(item)
    if kind == "customer_material":
        return "derived-setting"
    if kind == "teaching_design":
        return "teaching-device"
    return "synthetic-setting"


def item_class_label(value: dict[str, Any] | str, language: str = "en", *, bilingual: bool = False) -> str:
    """The display label of an item (or a class name) in the pack language (en or zh-CN; others fall
    back to en). ``bilingual`` gives the D12 form "合成教学设定 / synthetic teaching setting"."""
    cls = value if isinstance(value, str) else item_class(value)
    if cls not in ITEM_CLASS_LABELS:
        raise ValueError(f"unknown item class {cls!r}")
    zh, en = ITEM_CLASS_LABELS_BY_LANGUAGE["zh-CN"][cls], ITEM_CLASS_LABELS[cls]
    if bilingual:
        return f"{zh} / {en}"
    return zh if language == "zh-CN" else en


def _provenance_items(data: dict[str, Any]):
    for item in data.get("facts", []) or []:
        if isinstance(item, dict):
            yield "fact", item
    for item in data.get("tools", []) or []:
        if isinstance(item, dict):
            yield "tool", item
    for item in (data.get("knowledge") or {}).get("documents", []) or []:
        if isinstance(item, dict):
            yield "document", item
    for item in (data.get("evaluation") or {}).get("goldenSet", []) or []:
        if isinstance(item, dict):
            yield "golden", item


def class_summary(data: dict[str, Any]) -> dict[str, int]:
    """Item count per class (every class listed, zero included)."""
    counts = {name: 0 for name in ITEM_CLASSES}
    for _kind, item in _provenance_items(data):
        counts[item_class(item)] += 1
    return counts


def customer_anchors(data: dict[str, Any]) -> dict[str, list[str]]:
    """Golden cases per category that anchor a class to customer truth.

    An anchor is a golden case that is (really) ``customer_confirmed`` and whose basis facts all
    exist and are (really) ``customer_confirmed``.
    """
    facts = {f.get("id"): f for f in data.get("facts", []) or [] if isinstance(f, dict)}
    anchors: dict[str, list[str]] = {category: [] for category in ANCHOR_CATEGORIES}
    for case in (data.get("evaluation") or {}).get("goldenSet", []) or []:
        if not isinstance(case, dict) or case.get("category") not in anchors or not is_customer_confirmed(case):
            continue
        basis = case.get("basis") or []
        if basis and all(isinstance(facts.get(fid), dict) and is_customer_confirmed(facts[fid]) for fid in basis):
            anchors[case["category"]].append(str(case.get("id")))
    return anchors


def provenance_policy(data: dict[str, Any], *, student_safe: bool = False) -> dict[str, Any]:
    """The provenance policy a pack is gated under, with its customer anchors and item classes.

    ``student_safe`` (RELEASE.json) lists only practice anchor ids; holdout ids are instructor-only,
    so holdout anchors appear as counts alone.
    """
    kind = str(data.get("packKind") or "customer")
    anchors = customer_anchors(data)
    sets = {c.get("id"): c.get("set") for c in (data.get("evaluation") or {}).get("goldenSet", []) or [] if isinstance(c, dict)}
    view: dict[str, Any] = {
        "schema": PROVENANCE_POLICY_SCHEMA,
        "packKind": kind,
        "rule": PROVENANCE_POLICIES.get(kind, PROVENANCE_POLICIES["customer"]),
        "anchorsRequired": kind == "workshop",
        "anchorCounts": {category: len(ids) for category, ids in anchors.items()},
        "classes": class_summary(data),
    }
    if student_safe:
        view["practiceAnchors"] = {category: [cid for cid in ids if sets.get(cid) == "practice"] for category, ids in anchors.items()}
    else:
        view["anchors"] = anchors
        view["anchorsMissing"] = [category for category, ids in anchors.items() if not ids] if kind == "workshop" else []
    return view


def provenance_gate(data: dict[str, Any]) -> list[GateViolation]:
    """Return every gate violation (waived ones included, flagged)."""
    pack_kind = data.get("packKind", "customer")
    exceptions = _exceptions_by_item(data)
    violations: list[GateViolation] = []

    def add(item_type: str, item_id: str, reason: str, *, waivable: bool = True) -> None:
        exc = exceptions.get(item_id) if waivable else None
        violations.append(
            GateViolation(item_type=item_type, item_id=item_id, reason=reason, waived=exc is not None, exception=exc)
        )

    def check_item(item_type: str, item: dict[str, Any], item_id: str, criticality: str, *, device: bool = False) -> None:
        provenance = item["provenance"]
        if provenance in DRAFT_PROVENANCE:
            add(item_type, item_id, f"provenance '{provenance}' cannot enter a formal pack")
            return
        if is_simulated_confirmation(item):
            add(item_type, item_id, "customer_confirmed carries a simulated confirmation (confirmedBy/confirmationRef); "
                                    "only a real customer confirmation may be recorded", waivable=False)
            return
        if criticality != "blocking" or device:
            return
        if provenance == "customer_confirmed":
            return
        if pack_kind == "workshop":
            if provenance == "sa_synthetic" and _origin_kind(item) is None:
                add(item_type, item_id, "blocking sa_synthetic item in a workshop pack must carry origin "
                                        "(customer_material | sa_authored | teaching_design) or be customer_confirmed")
        elif pack_kind in STRICT_PACK_KINDS:
            add(item_type, item_id, f"blocking item in a {pack_kind} pack requires provenance 'customer_confirmed'")
        elif provenance not in FORMAL_PROVENANCE:  # pragma: no cover - the schema enum already refuses it
            add(item_type, item_id, f"blocking item has unsupported provenance '{provenance}'")

    for fact in data.get("facts", []):
        check_item("fact", fact, fact["id"], fact.get("criticality", "blocking"))

    for tool in data.get("tools", []):
        # Tool contracts define what the agent can do; a mock's behavior is blocking truth.
        check_item("tool", tool, tool["name"], "blocking")

    for doc in data.get("knowledge", {}).get("documents", []):
        # Knowledge documents are the answers' evidence; advisory unless the pack is customer- or
        # workshop-kind, where the customer must own the source material. A labeled noise document
        # is a teaching device in every kind (D12).
        criticality = "blocking" if pack_kind in STRICT_PACK_KINDS else "advisory"
        check_item("knowledge document", doc, doc["id"], criticality, device=is_noise_device(doc))

    for case in data.get("evaluation", {}).get("goldenSet", []):
        check_item("golden case", case, case["id"], case.get("criticality", "blocking"))

    if pack_kind == "workshop":
        missing = [category for category, ids in customer_anchors(data).items() if not ids]
        if missing:
            add("golden set", ANCHOR_GATE_ID,
                "a workshop pack needs at least one customer-confirmed anchor golden case per category "
                "(normal, boundary, prohibited) whose basis facts are all customer_confirmed; missing: "
                + ", ".join(missing), waivable=False)

    guide = (data.get("labs") or {}).get("guide")
    if isinstance(guide, dict):
        # Teaching prose, never business truth: an unreviewed narrative blocks; sa_synthetic passes in every kind.
        check_item("guide narrative", {"provenance": str(guide.get("provenance", ""))}, GUIDE_NARRATIVE_ID, "advisory")

    return violations


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def load_scenario(path: str | Path, *, check_files: bool = True, enforce_gate: bool = True) -> Scenario:
    """Load and fully validate a scenario file.

    Raises ``SchemaViolation``, ``CrossReferenceError`` or ``ProvenanceGateError``.
    With ``enforce_gate=False`` the gate still runs and its findings are attached to the
    returned scenario (used for draft previews), but nothing is raised for them.
    """
    scenario_path = Path(path)
    if not scenario_path.is_file():
        raise ScenarioError(f"scenario file not found: {scenario_path}")
    data = read_structured_file(scenario_path)

    if data.get("schemaVersion") != SUPPORTED_SCHEMA_VERSION:
        raise SchemaViolation([f"schemaVersion: expected {SUPPORTED_SCHEMA_VERSION}, got {data.get('schemaVersion')!r}"])

    schema_errors = validate_schema(data)
    if schema_errors:
        raise SchemaViolation(schema_errors)

    root = scenario_path.parent
    ref_errors = cross_reference_errors(data, root, check_files=check_files)
    if ref_errors:
        raise CrossReferenceError(ref_errors)

    violations = provenance_gate(data)
    if enforce_gate and any(not v.waived for v in violations):
        raise ProvenanceGateError(violations)

    return Scenario(path=scenario_path, root=root, data=data, gate_violations=violations)
