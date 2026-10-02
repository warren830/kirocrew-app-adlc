"""SPEC D12 provenance policy: workshop packs (origin-labeled synthetic settings + customer anchors),
noise documents as teaching devices in every kind, simulated confirmations rejected, derived item
classes, and the policy view recorded in RELEASE.json, the provenance report and the run report."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from workshop_customizer import compiler, render
from workshop_customizer import scenario as sc
from test_scenario import CONFIRMATION, minimal_scenario, write_scenario

REPO = Path(__file__).resolve().parents[1]
LOCK = json.loads((REPO / "template-lock.json").read_text(encoding="utf-8"))
SIMULATED = {"confirmedBy": "SIMULATED SA REVIEW (e2e_generate)", "confirmedAt": "2026-09-01T10:00:00+08:00",
             "confirmationRef": "e2e_generate:run-1"}


def _confirm(item: dict, confirmation: dict | None = None) -> dict:
    item["provenance"] = "customer_confirmed"
    item.update(confirmation or CONFIRMATION)
    return item


def _synthetic(item: dict, origin: str | None = "sa_authored") -> dict:
    item["provenance"] = "sa_synthetic"
    for key in ("confirmedBy", "confirmedAt", "confirmationRef"):
        item.pop(key, None)
    if origin:
        item["origin"] = {"kind": origin}
    else:
        item.pop("origin", None)
    return item


def workshop_scenario(*, anchors: bool = True, origin: str | None = "sa_authored") -> dict:
    """A workshop pack: labeled synthetic settings plus (optionally) one anchor per category."""
    data = minimal_scenario(pack_kind="workshop", provenance="sa_synthetic")
    for item in data["facts"] + data["tools"] + data["knowledge"]["documents"] + data["evaluation"]["goldenSet"]:
        _synthetic(item, origin)
    base = data["evaluation"]["goldenSet"][0]
    for category in ("boundary", "prohibited"):
        case = copy.deepcopy(base)
        case.update(id=f"vpn-{category}", label=category, query=f"A {category} VPN question?", category=category)
        data["evaluation"]["goldenSet"].append(case)
    if anchors:
        _confirm(data["facts"][0])  # vpn-reset-window: the basis of every case
        for case in data["evaluation"]["goldenSet"]:
            _confirm(case)
    return data


def _blocking(data: dict) -> dict[tuple[str, str], str]:
    return {(v.item_type, v.item_id): v.reason for v in sc.provenance_gate(data) if not v.waived}


# ---------------------------------------------------------------------------
# workshop kind
# ---------------------------------------------------------------------------


def test_workshop_pack_with_labeled_settings_and_anchors_loads(tmp_path):
    data = workshop_scenario()
    assert _blocking(data) == {}
    loaded = sc.load_scenario(write_scenario(tmp_path, data))
    assert loaded.pack_kind == "workshop" and not loaded.gate_violations
    assert sc.customer_anchors(data) == {"normal": ["vpn-reset-normal"], "boundary": ["vpn-boundary"], "prohibited": ["vpn-prohibited"]}


def test_workshop_synthetic_blocking_item_needs_an_origin():
    data = workshop_scenario()
    _synthetic(data["tools"][1], origin=None)
    _synthetic(data["knowledge"]["documents"][0], origin=None)
    blocked = _blocking(data)
    assert set(blocked) == {("tool", "lookup_ticket"), ("knowledge document", "vpn-policy")}
    assert all("must carry origin" in reason for reason in blocked.values())
    for kind in sc.ORIGIN_KINDS:
        labeled = workshop_scenario(origin=kind if kind != "customer_material" else "sa_authored")
        assert _blocking(labeled) == {}


def test_workshop_pack_needs_one_anchor_per_category():
    data = workshop_scenario(anchors=False)
    blocked = _blocking(data)
    assert list(blocked) == [("golden set", sc.ANCHOR_GATE_ID)]
    assert "missing: normal, boundary, prohibited" in blocked[("golden set", sc.ANCHOR_GATE_ID)]

    data = workshop_scenario()
    _synthetic(next(c for c in data["evaluation"]["goldenSet"] if c["category"] == "boundary"))
    assert "missing: boundary" in _blocking(data)[("golden set", sc.ANCHOR_GATE_ID)]


def test_an_anchor_needs_customer_confirmed_basis_facts():
    data = workshop_scenario()
    _synthetic(data["facts"][0])  # the basis of every case is now a labeled synthetic setting
    assert sc.customer_anchors(data) == {"normal": [], "boundary": [], "prohibited": []}
    assert ("golden set", sc.ANCHOR_GATE_ID) in _blocking(data)
    data = workshop_scenario()
    data["evaluation"]["goldenSet"][0]["basis"] = ["vpn-reset-window", "ticket-sla"]  # ticket-sla is sa_synthetic
    assert sc.customer_anchors(data)["normal"] == []


def test_the_anchor_rule_can_never_be_waived(tmp_path):
    data = workshop_scenario(anchors=False)
    exception = {"itemId": sc.ANCHOR_GATE_ID, "reason": "class starts tomorrow", "approvedBy": ["a", "b"],
                 "approvedAt": "2026-09-01T10:00:00+08:00"}
    data["governance"] = {"exceptions": [exception]}
    violation = next(v for v in sc.provenance_gate(data) if v.item_id == sc.ANCHOR_GATE_ID)
    assert violation.waived is False and violation.exception is None
    assert f"governance exception references unknown item '{sc.ANCHOR_GATE_ID}'" in sc.cross_reference_errors(data, check_files=False)
    # even a real item named like the gate id cannot waive it
    data["facts"].append({"id": sc.ANCHOR_GATE_ID, "statement": "x", "criticality": "advisory", "provenance": "sa_synthetic"})
    assert not next(v for v in sc.provenance_gate(data) if v.item_id == sc.ANCHOR_GATE_ID and v.item_type == "golden set").waived


# ---------------------------------------------------------------------------
# customer and reference kinds
# ---------------------------------------------------------------------------


def test_customer_pack_stays_strict_for_labeled_settings():
    data = workshop_scenario()
    data["packKind"] = "customer"
    blocked = _blocking(data)
    assert ("tool", "lookup_ticket") in blocked and ("knowledge document", "vpn-policy") in blocked
    assert blocked[("tool", "lookup_ticket")] == "blocking item in a customer pack requires provenance 'customer_confirmed'"
    assert ("golden set", sc.ANCHOR_GATE_ID) not in blocked  # anchors are the workshop rule


def _noise_doc(provenance: str = "sa_synthetic", origin: str | None = "teaching_design", noise: bool = True) -> dict:
    doc = {"id": "canteen-faq", "title": "Canteen FAQ", "file": "knowledge-base/docs/vpn-policy.md",
           "provenance": provenance, "noise": noise}
    if origin:
        doc["origin"] = {"kind": origin}
    return doc


@pytest.mark.parametrize("kind", sc.PACK_KINDS)
def test_noise_documents_are_teaching_devices_in_every_kind(kind):
    data = minimal_scenario(pack_kind=kind, provenance="customer_confirmed")
    data["knowledge"]["documents"].append(_noise_doc())
    if kind == "workshop":
        data = workshop_scenario()
        data["knowledge"]["documents"].append(_noise_doc())
    assert ("knowledge document", "canteen-faq") not in _blocking(data)
    assert sc.item_class(_noise_doc()) == "teaching-device"


@pytest.mark.parametrize("doc, blocked", [
    (_noise_doc(origin=None), True),  # an unlabeled distractor is not a declared teaching device
    (_noise_doc(origin="sa_authored"), True),
    (_noise_doc(noise=False), True),  # teaching_design on a policy document does not exempt it
    (_noise_doc(provenance="ai_draft"), True),  # drafts never enter a formal pack
])
def test_customer_pack_only_exempts_labeled_noise_documents(doc, blocked):
    data = minimal_scenario(pack_kind="customer", provenance="customer_confirmed")
    data["knowledge"]["documents"].append(doc)
    assert (("knowledge document", "canteen-faq") in _blocking(data)) is blocked


def test_reference_packs_are_unaffected(tmp_path):
    for pack in ("hr-default", "it-helpdesk", "maintenance"):
        loaded = sc.load_scenario(REPO / "scenarios" / pack / "scenario.yaml")
        assert loaded.gate_violations == [], pack
        policy = sc.provenance_policy(loaded.data)
        assert policy["packKind"] == "reference" and policy["anchorsRequired"] is False and policy["anchorsMissing"] == []


# ---------------------------------------------------------------------------
# simulated confirmations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sc.PACK_KINDS)
@pytest.mark.parametrize("field, value", [
    ("confirmedBy", "SIMULATED SA REVIEW (e2e_generate)"),
    ("confirmedBy", "simulated customer"),
    ("confirmationRef", "e2e_generate:run-7"),
    ("confirmationRef", "E2E_GENERATE-42"),
])
def test_simulated_confirmations_are_rejected_and_never_waived(kind, field, value):
    data = minimal_scenario(pack_kind=kind, provenance="customer_confirmed")
    data["facts"][0][field] = value
    data["governance"] = {"exceptions": [{"itemId": "vpn-reset-window", "reason": "prep", "approvedBy": ["a", "b"],
                                          "approvedAt": "2026-09-01T10:00:00+08:00"}]}
    violation = next(v for v in sc.provenance_gate(data) if v.item_id == "vpn-reset-window")
    assert "simulated confirmation" in violation.reason and violation.waived is False
    assert sc.is_simulated_confirmation(data["facts"][0]) and sc.item_class(data["facts"][0]) == "draft"


def test_simulated_confirmations_never_count_as_anchors():
    data = workshop_scenario()
    _confirm(data["facts"][0], SIMULATED)
    assert sc.customer_anchors(data) == {"normal": [], "boundary": [], "prohibited": []}
    blocked = _blocking(data)
    assert ("golden set", sc.ANCHOR_GATE_ID) in blocked and ("fact", "vpn-reset-window") in blocked


# ---------------------------------------------------------------------------
# classes and the recorded policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("item, expected", [
    ({"provenance": "customer_confirmed", **CONFIRMATION}, "customer-fact"),
    ({"provenance": "sa_synthetic", "origin": {"kind": "customer_material", "materials": ["mat-0123456789ab"]}}, "derived-setting"),
    ({"provenance": "sa_synthetic", "origin": {"kind": "teaching_design"}}, "teaching-device"),
    ({"provenance": "sa_synthetic", "origin": {"kind": "sa_authored"}}, "synthetic-setting"),
    ({"provenance": "sa_synthetic"}, "synthetic-setting"),
    ({"provenance": "ai_draft", "origin": {"kind": "teaching_design"}}, "draft"),
    ({"provenance": "pending"}, "draft"),
])
def test_item_class(item, expected):
    assert sc.item_class(item) == expected and expected in sc.ITEM_CLASSES


def test_class_summary_and_policy_views():
    data = workshop_scenario()
    data["evaluation"]["goldenSet"][1]["set"] = "holdout"  # the boundary anchor is a holdout case
    summary = sc.class_summary(data)
    assert set(summary) == set(sc.ITEM_CLASSES) and sum(summary.values()) == 2 + 2 + 1 + 3
    assert summary["customer-fact"] == 4 and summary["synthetic-setting"] == 4
    full = sc.provenance_policy(data)
    assert full["anchors"]["boundary"] == ["vpn-boundary"] and full["anchorsRequired"] is True and full["anchorsMissing"] == []
    safe = sc.provenance_policy(data, student_safe=True)
    assert "anchors" not in safe and safe["practiceAnchors"]["boundary"] == [] and safe["anchorCounts"]["boundary"] == 1
    assert "vpn-boundary" not in json.dumps(safe)  # holdout ids never reach a student-facing file


def test_release_manifest_and_provenance_report_record_the_policy(tmp_path):
    scenario = sc.load_scenario(REPO / "scenarios" / "it-helpdesk" / "scenario.yaml")
    pack = compiler.compile_pack(scenario, tmp_path / "out", template_commit=LOCK["template"]["commit"])
    report = (pack.instructor_dir / "provenance-report.md").read_text(encoding="utf-8")
    assert "Fictional reference pack — no statement is a customer fact." in report
    assert "## Provenance policy" in report and "| class | origin |" in report
    release = render.render_release(pack, REPO / LOCK["template"]["localPath"], tmp_path / "out" / "release",
                                    template_commit=LOCK["template"]["commit"])
    manifest = json.loads((release.release_dir / render.MANIFEST_NAME).read_text(encoding="utf-8"))
    policy = manifest["provenancePolicy"]
    assert policy == sc.provenance_policy(scenario.data, student_safe=True)
    assert policy["packKind"] == "reference" and policy["schema"] == sc.PROVENANCE_POLICY_SCHEMA
    holdout_ids = {c["id"] for c in scenario.data["evaluation"]["goldenSet"] if c["set"] == "holdout"}
    assert not holdout_ids & {cid for ids in policy["practiceAnchors"].values() for cid in ids}
    render.verify_release(release.release_dir)  # the manifest key is outside the hashed file set


def test_run_report_records_the_policy_of_the_build_snapshot():
    import teaching_run as tr
    from workshop_customizer import guided_report

    report = guided_report.build_report(tr.run_state("hr-reproduced"), tr.scenario("hr-reproduced"), generated_at="x")
    assert report["provenancePolicy"] == sc.provenance_policy(tr.scenario("hr-reproduced"))
    assert report["provenancePolicy"]["packKind"] == "reference"


def test_item_class_labels_are_bilingual():
    """SPEC D12: guides label non-customer items "合成教学设定 / synthetic teaching setting"."""
    assert set(sc.ITEM_CLASS_LABELS_BY_LANGUAGE["zh-CN"]) == set(sc.ITEM_CLASSES) == set(sc.ITEM_CLASS_LABELS)
    setting = {"provenance": "sa_synthetic", "origin": {"kind": "sa_authored"}}
    assert sc.item_class_label(setting) == "synthetic teaching setting"
    assert sc.item_class_label(setting, "zh-CN") == "合成教学设定"
    assert sc.item_class_label("synthetic-setting", bilingual=True) == "合成教学设定 / synthetic teaching setting"
    derived = {"provenance": "sa_synthetic", "origin": {"kind": "customer_material", "materials": ["mat-000000000001"]}}
    assert "合成教学设定" in sc.item_class_label(derived, "zh-CN")
    assert sc.item_class_label({"provenance": "customer_confirmed"}, "fr") == "customer fact"  # unknown language: en
    with pytest.raises(ValueError):
        sc.item_class_label("nonsense")
