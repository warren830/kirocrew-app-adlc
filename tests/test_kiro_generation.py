"""Regression tests for the KiroCrew-backed generation seam."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
ROUTES_PY = REPO / "app" / "backend" / "routes.py"


def _load():
    spec = importlib.util.spec_from_file_location("wc_generation_routes", ROUTES_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


gen = _load()


def _maintenance_bundle(project_id: str = "cold-chain-test", pack_kind: str = "reference") -> dict:
    root = REPO / "scenarios" / "maintenance"
    scenario = yaml.safe_load((root / "scenario.yaml").read_text(encoding="utf-8"))
    scenario["id"] = project_id
    scenario["displayName"] = "Cold Chain Test"
    scenario["packKind"] = pack_kind
    refs = set()
    refs.update(d["file"] for d in scenario["knowledge"]["documents"])
    refs.update(s["file"] for s in scenario["skills"])
    refs.add(scenario["prompts"]["baselineFile"])
    refs.add(scenario["prompts"]["optimizationCandidateFile"])
    files = {rel: (root / rel).read_text(encoding="utf-8") for rel in refs}
    return {
        "status": "ready",
        "summary": "Kiro generated a complete fictional maintenance pack.",
        "openQuestions": ["Who confirms the response targets?"],
        "truthLedger": [{"id": "pm-intervals-filler", "statement": "250 hours", "provenance": "sa_synthetic"}],
        "scenario": scenario,
        "files": files,
    }


def _project(data_dir: Path, project_id: str = "cold-chain-test", pack_kind: str = "reference") -> tuple[Path, str]:
    pdir = data_dir / "projects" / project_id
    pdir.mkdir(parents=True)
    original = json.dumps({"schemaVersion": 1, "id": project_id}) + "\n"
    (pdir / "scenario.yaml").write_text(original, encoding="utf-8")
    (pdir / "project.json").write_text(
        json.dumps({"id": project_id, "displayName": "Cold Chain Test", "packKind": pack_kind, "status": "intake", "template": "blank"}),
        encoding="utf-8",
    )
    return pdir, original


def test_generation_task_uses_kirocrew_not_bedrock_generation():
    task = gen._build_task(project_id="cold-chain-test", display_name="Cold Chain Test", pack_kind="customer", customer="Fictional Foods", brief="A cold-chain incident assistant for warehouse operators.")
    assert "Kiro reasoning only" in task
    assert "Do not call tools, Bedrock, AWS" in task
    assert "WORKSHOP_PACK_JSON_BEGIN" in task
    assert '"projectId": "cold-chain-test"' in task


def test_extract_and_normalise_bounded_result():
    payload = _maintenance_bundle()
    raw = "WORKSHOP_PACK_JSON_BEGIN\n" + json.dumps(payload) + "\nWORKSHOP_PACK_JSON_END"
    parsed = gen._extract_json(raw)
    clean = gen._normalise_bundle(parsed, project_id="cold-chain-test", pack_kind="reference")
    assert clean["status"] == "ready"
    assert len(clean["scenario"]["evaluation"]["goldenSet"]) == 16
    assert clean["scenario"]["labs"]["teaching"] == payload["scenario"]["labs"]["teaching"]  # kept, not dropped
    assert clean["scenario"]["evaluation"]["judgeModel"] == gen.FIXED_JUDGE_MODEL
    assert all(path.startswith(gen.ALLOWED_FILE_PREFIXES) for path in clean["files"])


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda p: p["files"].update({"../escape.md": "x"}), "path is not allowed"),
        (lambda p: p["files"].update({"knowledge-base/docs/leak.md": "key " + "AKIA" + "ABCDEFGHIJKLMNOP"}), "credential-shaped"),
    ],
)
def test_generated_bundle_fails_closed(mutate, message):
    payload = _maintenance_bundle()
    mutate(payload)
    with pytest.raises(gen.GenerationError, match=message):
        gen._normalise_bundle(payload, project_id="cold-chain-test", pack_kind="reference")


def test_settle_recovers_structured_agent_result(tmp_path):
    data = tmp_path / "data"
    pdir, original = _project(data)
    generation_id = "gen-0123456789abcdef"
    record = {
        "id": generation_id,
        "projectId": "cold-chain-test",
        "packKind": "reference",
        "status": "running",
        "sourceScenarioSha256": gen._sha256_text(original),
        "createdAt": gen._utc_now(),
        "spawnId": "spawn-1",
    }
    gen._write_record(data, record)
    answer = "WORKSHOP_PACK_JSON_BEGIN\n" + json.dumps(_maintenance_bundle()) + "\nWORKSHOP_PACK_JSON_END"
    info = SimpleNamespace(done=True, result=answer, result_truncated=False, error="", app="workshop-customizer")
    manager = SimpleNamespace(get=lambda _spawn_id: info)
    request = SimpleNamespace(app={"state": SimpleNamespace(subagents=manager)})
    settled = gen._settle(record, request=request, data_dir=data)
    assert settled["status"] == "ready"
    assert settled["result"]["summary"].startswith("Kiro generated")
    assert gen._public_record(settled)["generatedFiles"]
    assert pdir.is_dir()


def test_apply_is_explicit_atomic_and_invalidates_old_build(tmp_path):
    data = tmp_path / "data"
    pdir, original = _project(data)
    generation_id = "gen-fedcba9876543210"
    record = {
        "id": generation_id,
        "projectId": "cold-chain-test",
        "packKind": "reference",
        "status": "ready",
        "sourceScenarioSha256": gen._sha256_text(original),
        "createdAt": gen._utc_now(),
        "result": gen._normalise_bundle(_maintenance_bundle(), project_id="cold-chain-test", pack_kind="reference"),
    }
    gen._write_record(data, record)

    result = gen._apply_record(record, data_dir=data, body={"acknowledged": True})
    assert result["applied"] is True and result["files"] >= 8
    scenario = yaml.safe_load((pdir / "scenario.yaml").read_text(encoding="utf-8"))
    assert scenario["id"] == "cold-chain-test" and len(scenario["evaluation"]["goldenSet"]) == 16
    meta = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    assert meta["status"] == "truth-review" and meta["template"] == "kiro-generated"
    assert meta["lastValidation"] is None and meta["lastBuild"] is None
    assert json.loads((data / "generations" / f"{generation_id}.json").read_text())["status"] == "applied"


def test_apply_refuses_when_project_changed_after_generation(tmp_path):
    data = tmp_path / "data"
    pdir, original = _project(data)
    record = {
        "id": "gen-1111111111111111",
        "projectId": "cold-chain-test",
        "packKind": "reference",
        "status": "ready",
        "sourceScenarioSha256": gen._sha256_text(original),
        "createdAt": gen._utc_now(),
        "result": gen._normalise_bundle(_maintenance_bundle(), project_id="cold-chain-test", pack_kind="reference"),
    }
    gen._write_record(data, record)  # apply reads the record from disk
    (pdir / "scenario.yaml").write_text(original + "# user edit\n", encoding="utf-8")
    with pytest.raises(gen.GenerationError, match="project changed"):
        gen._apply_record(record, data_dir=data, body={"acknowledged": True})


def test_common_kiro_variants_canonicalize_to_schema_v1(tmp_path):
    from workshop_customizer.scenario import load_scenario
    from workshop_customizer.validator import validate_scenario_policy

    payload = _maintenance_bundle(project_id="variant-pack", pack_kind="customer")
    scenario = payload["scenario"]
    scenario["namespace"] = "variant-pack"
    scenario["knowledge"]["noisePlan"] = {
        "description": "Teaching noise for retrieval diagnosis.",
        "offTopicTails": ["cafeteria"],
        "smallChunkSize": 120,
    }
    scenario["agent"]["roles"] = [
        {"role": role["id"], "name": role["name"], "description": role["description"], "permissions": role.get("permissions", [])}
        for role in scenario["agent"]["roles"]
    ]
    for tool in scenario["tools"]:
        tool["kind"] = "action" if tool["name"].startswith("request_") else "lookup"
        fixtures = tool.get("fixtures") or {}
        fixtures["cases"] = [
            {"input": row["when"], "output": row["return"]} for row in fixtures.get("cases", [])
        ]
        fixtures["errors"] = [
            {"when": row["when"], "error": {"code": "SYNTHETIC", "message": row["error"]}}
            for row in fixtures.get("errors", [])
        ]
    scenario["skills"] = [
        {"id": row["name"], "title": row["name"], "path": row["file"]}
        for row in scenario["skills"]
    ]
    scenario["evaluation"]["retrievalToolName"] = "made-up-kb-name"

    clean = gen._normalise_bundle(payload, project_id="variant-pack", pack_kind="customer")
    root = tmp_path / "pack"
    root.mkdir()
    for rel, content in clean["files"].items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (root / "scenario.yaml").write_text(yaml.safe_dump(clean["scenario"], sort_keys=False), encoding="utf-8")

    loaded = load_scenario(root / "scenario.yaml", enforce_gate=False)
    assert loaded.gate_violations  # ai_draft remains fail-closed; adapter never confirms truth
    # The variant mis-kinded the retrieval tool as a lookup: the canonicalizer remaps the one tool whose
    # name carries a retrieval marker (and has no mock fixtures) back to kind retrieval instead of adding
    # a shell next to it, so the probes keep requiring the real retrieval tool and the policy passes.
    errors = {f.code for f in validate_scenario_policy(loaded.data).errors}
    assert errors == set(), errors
    assert loaded.data["namespace"]["ssmParameterPrefix"].startswith("/app/")
    assert loaded.data["evaluation"]["retrievalToolName"] == "retrieve_maintenance_procedure"
    assert [t["name"] for t in loaded.data["tools"] if t["kind"] == "retrieval"] == ["retrieve_maintenance_procedure"]
    assert all(tool["kind"] in ("retrieval", "mock") for tool in loaded.data["tools"])
    assert any("remapped to retrieval" in w for w in clean["warnings"])


UNSUPPORTED_SCHEMA_KEYS = {"additionalProperties", "enum", "minimum", "maximum", "default"}


def _schema_keys(schema: dict) -> set:
    keys = set(schema)
    for sub in (schema.get("properties") or {}).values():
        keys |= _schema_keys(sub)
    if isinstance(schema.get("items"), dict):
        keys |= _schema_keys(schema["items"])
    return keys


def test_generated_tool_schemas_are_folded_to_the_gateway_subset(tmp_path):
    from workshop_customizer.scenario import load_scenario
    from workshop_customizer.validator import validate_scenario_policy

    payload = _maintenance_bundle(project_id="schema-pack")
    # A mock tool: the retrieval tool's schema is forced to {query: string} (SPEC D6b).
    tool = next(t for t in payload["scenario"]["tools"] if t["kind"] == "mock")
    tool["inputSchema"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "priority": {"type": "string", "description": "Ticket priority", "enum": ["P1", "P2"]},
            "topK": {"type": "integer", "minimum": 1, "maximum": 8, "default": 3},
            "tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
        },
        "required": ["priority"],
    }
    clean = gen._normalise_bundle(payload, project_id="schema-pack", pack_kind="reference")
    schema = next(t for t in clean["scenario"]["tools"] if t["name"] == tool["name"])["inputSchema"]
    assert not (_schema_keys(schema) & UNSUPPORTED_SCHEMA_KEYS), schema
    props = schema["properties"]
    assert props["priority"]["description"] == "Ticket priority (allowed: P1 | P2)"
    assert props["topK"]["description"] == "range 1–8; default 3"
    assert props["tags"]["items"]["description"] == "allowed: a | b"
    assert schema["type"] == "object" and schema["required"] == ["priority"]

    root = tmp_path / "pack"
    root.mkdir()
    for rel, content in clean["files"].items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (root / "scenario.yaml").write_text(yaml.safe_dump(clean["scenario"], sort_keys=False), encoding="utf-8")
    data = load_scenario(root / "scenario.yaml", enforce_gate=False).data

    def gateway_errors(scenario: dict) -> list:
        report = validate_scenario_policy(scenario)
        return [f for f in report.findings if getattr(f, "code", None) == "tools.gateway_schema"]

    assert gateway_errors(data) == []
    raw = json.loads(json.dumps(data))
    raw["tools"][0]["inputSchema"]["additionalProperties"] = False  # negative control: the engine check is live
    assert gateway_errors(raw), "tools.gateway_schema must reject unsupported inputSchema keys"
