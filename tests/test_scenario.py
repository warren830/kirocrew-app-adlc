"""Tests for scenario loading, schema validation and the provenance gate."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from workshop_customizer import scenario as sc

CONFIRMATION = {
    "confirmedBy": "Customer SME (Jane Doe)",
    "confirmedAt": "2026-09-01T10:00:00+08:00",
    "confirmationRef": "workshop-prep-meeting-2026-09-01",
}


def minimal_scenario(pack_kind: str = "reference", provenance: str = "sa_synthetic") -> dict:
    extra = CONFIRMATION if provenance == "customer_confirmed" else {}
    return {
        "schemaVersion": 1,
        "id": "it-helpdesk",
        "displayName": "IT Helpdesk Assistant",
        "packKind": pack_kind,
        "language": "en",
        "namespace": {
            "agentName": "itassistant",
            "toolTargetName": "it-tools",
            "gatewayName": "itgateway",
            "knowledgeBaseName": "it-knowledge-base",
            "kbPrefix": "it/",
            "ssmParameterPrefix": "/app/it",
            "lambdaFunctionName": "it-tools-handler",
        },
        "agent": {
            "audience": "Employees opening IT tickets",
            "purpose": "Diagnose common IT issues and route tickets",
            "scope": ["VPN access", "password reset"],
            "outOfScope": ["Hardware procurement"],
            "roles": [{"id": "employee", "name": "Employee", "description": "Regular staff"}],
            "handoffConditions": ["Security incident suspected"],
            "prohibitedBehaviors": ["Never reveal another employee's ticket"],
        },
        "facts": [
            {
                "id": "vpn-reset-window",
                "statement": "VPN credentials can be reset by the employee once per 24 hours.",
                "criticality": "blocking",
                "provenance": provenance,
                "source": "IT policy 4.2",
                **extra,
            },
            {
                "id": "ticket-sla",
                "statement": "Priority-2 tickets are acknowledged within 4 business hours.",
                "criticality": "advisory",
                "provenance": "sa_synthetic",
            },
        ],
        "knowledge": {
            "documents": [
                {
                    "id": "vpn-policy",
                    "title": "VPN Access Policy",
                    "file": "knowledge-base/docs/vpn-policy.md",
                    "provenance": provenance,
                    "noise": False,
                    **({} if provenance != "customer_confirmed" else {}),
                }
            ],
            "noisePlan": {"enabled": True, "rationale": "Surface retrieval precision issues in THELMA SP2."},
        },
        "tools": [
            {
                "name": "retrieve_it_policy",
                "description": "Search the IT knowledge base",
                "kind": "retrieval",
                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                "provenance": provenance,
                **extra,
            },
            {
                "name": "lookup_ticket",
                "description": "Look up a ticket by id",
                "kind": "mock",
                "inputSchema": {"type": "object", "properties": {"ticket_id": {"type": "string"}}, "required": ["ticket_id"]},
                "provenance": provenance,
                "fixtures": {
                    "default": {"error": "ticket not found"},
                    "cases": [{"when": {"ticket_id": "INC-1001"}, "return": {"status": "open", "priority": "P2"}}],
                },
                **extra,
            },
        ],
        "skills": [{"name": "ticket-triage", "file": "skills/ticket-triage/SKILL.md"}],
        "prompts": {
            "baselineFile": "agent/baseline-prompt.md",
            "optimizationCandidateFile": "agent/optimization-candidate.md",
        },
        "evaluation": {
            "retrievalToolName": "retrieve_it_policy",
            "judgeModel": "us.amazon.nova-2-lite-v1:0",
            "goldenSet": [
                {
                    "id": "vpn-reset-normal",
                    "label": "VPN reset",
                    "query": "How do I reset my VPN credentials?",
                    "category": "normal",
                    "set": "practice",
                    "actorId": "employee-001",
                    "roleId": "employee",
                    "expected": {"mustMention": ["24 hours"], "requiredTools": ["retrieve_it_policy"]},
                    "basis": ["vpn-reset-window"],
                    "provenance": provenance,
                    **extra,
                }
            ],
        },
    }


def write_scenario(tmp_path: Path, data: dict, fmt: str = "yaml") -> Path:
    root = tmp_path / data["id"]
    for rel in [
        "knowledge-base/docs/vpn-policy.md",
        "skills/ticket-triage/SKILL.md",
        "agent/baseline-prompt.md",
        "agent/optimization-candidate.md",
    ]:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {rel}\n", encoding="utf-8")
    if fmt == "yaml":
        path = root / "scenario.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    else:
        path = root / "scenario.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def test_schema_file_is_a_valid_draft_2020_schema():
    schema = sc.load_schema()
    assert schema["$schema"].endswith("draft/2020-12/schema")
    assert sc.validate_schema(minimal_scenario()) == []


@pytest.mark.parametrize("fmt", ["yaml", "json"])
def test_reference_pack_loads(tmp_path: Path, fmt: str):
    path = write_scenario(tmp_path, minimal_scenario(), fmt=fmt)
    scenario = sc.load_scenario(path)
    assert scenario.id == "it-helpdesk"
    assert scenario.pack_kind == "reference"
    assert scenario.namespace["agentName"] == "itassistant"
    assert scenario.gate_violations == []


def test_schema_violation_reports_location(tmp_path: Path):
    data = minimal_scenario()
    data["namespace"]["agentName"] = "Bad-Name!"
    path = write_scenario(tmp_path, data)
    with pytest.raises(sc.SchemaViolation) as excinfo:
        sc.load_scenario(path)
    assert any(err.startswith("namespace/agentName") for err in excinfo.value.errors)


def test_unknown_schema_version_is_rejected(tmp_path: Path):
    data = minimal_scenario()
    data["schemaVersion"] = 2
    path = write_scenario(tmp_path, data)
    with pytest.raises(sc.SchemaViolation):
        sc.load_scenario(path)


def test_golden_case_must_cite_existing_fact(tmp_path: Path):
    data = minimal_scenario()
    data["evaluation"]["goldenSet"][0]["basis"] = ["does-not-exist"]
    path = write_scenario(tmp_path, data)
    with pytest.raises(sc.CrossReferenceError) as excinfo:
        sc.load_scenario(path)
    assert any("unknown fact 'does-not-exist'" in err for err in excinfo.value.errors)


def test_missing_referenced_file_is_reported(tmp_path: Path):
    data = minimal_scenario()
    path = write_scenario(tmp_path, data)
    (path.parent / "agent" / "baseline-prompt.md").unlink()
    with pytest.raises(sc.CrossReferenceError) as excinfo:
        sc.load_scenario(path)
    assert any("baselineFile file not found" in err for err in excinfo.value.errors)


def test_relative_path_may_not_escape_root(tmp_path: Path):
    data = minimal_scenario()
    data["prompts"]["baselineFile"] = "../outside.md"
    assert any("prompts/baselineFile" in err for err in sc.validate_schema(data))


def test_retrieval_tool_must_exist():
    data = minimal_scenario()
    data["evaluation"]["retrievalToolName"] = "retrieve_hr_policy"
    errors = sc.cross_reference_errors(data, None, check_files=False)
    assert any("retrievalToolName" in err for err in errors)


def test_customer_pack_blocks_unconfirmed_blocking_fact(tmp_path: Path):
    data = minimal_scenario(pack_kind="customer", provenance="sa_synthetic")
    path = write_scenario(tmp_path, data)
    with pytest.raises(sc.ProvenanceGateError) as excinfo:
        sc.load_scenario(path)
    blocked = {(v.item_type, v.item_id) for v in excinfo.value.violations if not v.waived}
    assert ("fact", "vpn-reset-window") in blocked
    assert ("golden case", "vpn-reset-normal") in blocked
    # advisory facts never block
    assert ("fact", "ticket-sla") not in blocked


def test_customer_pack_loads_when_blocking_items_are_confirmed(tmp_path: Path):
    data = minimal_scenario(pack_kind="customer", provenance="customer_confirmed")
    path = write_scenario(tmp_path, data)
    scenario = sc.load_scenario(path)
    assert scenario.pack_kind == "customer"
    assert not [v for v in scenario.gate_violations if not v.waived]


def test_customer_confirmed_requires_confirmation_evidence():
    data = minimal_scenario(pack_kind="customer", provenance="customer_confirmed")
    del data["facts"][0]["confirmedBy"]
    errors = sc.validate_schema(data)
    assert any("confirmedBy" in err for err in errors)


def test_draft_provenance_never_enters_a_formal_pack(tmp_path: Path):
    data = minimal_scenario()
    data["facts"][1]["provenance"] = "ai_draft"  # even an advisory fact
    path = write_scenario(tmp_path, data)
    with pytest.raises(sc.ProvenanceGateError) as excinfo:
        sc.load_scenario(path)
    assert any(v.item_id == "ticket-sla" and "ai_draft" in v.reason for v in excinfo.value.violations)


def test_exception_waives_one_item_and_is_reported(tmp_path: Path):
    data = minimal_scenario(pack_kind="customer", provenance="customer_confirmed")
    data["facts"][0]["provenance"] = "sa_synthetic"
    for key in ("confirmedBy", "confirmedAt", "confirmationRef"):
        data["facts"][0].pop(key)
    data["governance"] = {
        "exceptions": [
            {
                "itemId": "vpn-reset-window",
                "reason": "Customer policy text pending legal review; synthetic value agreed for the lab.",
                "approvedBy": ["Product/Workshop Owner", "SA Pilot Owner"],
                "approvedAt": "2026-09-05T09:00:00+08:00",
            }
        ]
    }
    path = write_scenario(tmp_path, data)
    scenario = sc.load_scenario(path)
    assert [v.item_id for v in scenario.waived_items] == ["vpn-reset-window"]


def test_exception_requires_two_approvers():
    data = minimal_scenario()
    data["governance"] = {
        "exceptions": [
            {
                "itemId": "vpn-reset-window",
                "reason": "x",
                "approvedBy": ["only-one"],
                "approvedAt": "2026-09-05T09:00:00+08:00",
            }
        ]
    }
    assert any("approvedBy" in err for err in sc.validate_schema(data))


def test_gate_findings_are_attached_when_not_enforced(tmp_path: Path):
    data = minimal_scenario(pack_kind="customer", provenance="sa_synthetic")
    path = write_scenario(tmp_path, data)
    scenario = sc.load_scenario(path, enforce_gate=False)
    assert any(not v.waived for v in scenario.gate_violations)


def test_scenario_is_not_mutated_by_loading(tmp_path: Path):
    data = minimal_scenario()
    snapshot = copy.deepcopy(data)
    path = write_scenario(tmp_path, data)
    sc.load_scenario(path)
    assert data == snapshot
