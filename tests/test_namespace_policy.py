"""SPEC D6a/b policy: AWS name limits (``namespace.*``) and the one retrieval tool (``tools.retrieval_*``).

Boundary values: agentName 19/20 characters, knowledgeBaseName 50/51, the THELMA evaluator name
48/49; a second retrieval tool, and retrieval inputSchemas without a required string ``query``.
Offline: the checks read only the scenario dict (and the pinned botocore model for tool schemas).
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
import yaml

from workshop_customizer import compiler, validator
from workshop_customizer.scenario import ScenarioError, load_scenario

from test_app_backend import server_mod  # noqa: E402
from test_kiro_generation import gen  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKS = ("hr-default", "it-helpdesk", "maintenance")


def _data(pack: str = "maintenance") -> dict:
    return yaml.safe_load((REPO_ROOT / "scenarios" / pack / "scenario.yaml").read_text(encoding="utf-8"))


def _codes(report: validator.ValidationReport) -> list[str]:
    return [f.code for f in report.errors]


def _retrieval(data: dict) -> dict:
    return next(t for t in data["tools"] if t["kind"] == "retrieval")


@pytest.mark.parametrize("pack", PACKS)
def test_reference_packs_fit_the_limits_and_have_one_query_retrieval_tool(pack):
    data = _data(pack)
    assert validator.check_namespace(data).findings == []
    assert not [f for f in validator.check_gateway_tools(data).findings if f.code.startswith("tools.retrieval")]


@pytest.mark.parametrize(("length", "codes"), [
    (19, []),
    (20, ["namespace.agent_name_too_long"]),
    (29, ["namespace.agent_name_too_long"]),  # '<29>_thelma_rag_quality' is exactly 48
    (30, ["namespace.agent_name_too_long", "namespace.evaluator_name_too_long"]),
])
def test_agent_name_limit_and_the_evaluator_name(length, codes):
    data = _data()
    data["namespace"]["agentName"] = "m" + "a" * (length - 1)
    report = validator.check_namespace(data)
    assert _codes(report) == codes
    assert all(f.scopes == ("agent",) and f.path == "namespace.agentName" for f in report.findings)


@pytest.mark.parametrize(("length", "codes"), [(50, []), (51, ["namespace.kb_name_too_long"])])
def test_knowledge_base_name_limit(length, codes):
    data = _data()
    data["namespace"]["knowledgeBaseName"] = "k" + "b" * (length - 1)
    report = validator.check_namespace(data)
    assert _codes(report) == codes
    assert all(f.scopes == ("agent",) for f in report.findings)


@pytest.mark.parametrize("prefix", ["customizer-releases/", "skills/", "multimodal/"])
def test_a_kb_prefix_may_not_be_a_reserved_bucket_prefix(prefix):
    """01-create-kb deletes every object under kbPrefix that is not this pack's document; the bucket's own
    layout prefixes (release bundles, skills, multimodal staging) are refused before any build."""
    data = _data()
    data["namespace"]["kbPrefix"] = prefix
    report = validator.validate_scenario_policy(data, root=REPO_ROOT / "scenarios" / "maintenance")
    found = [(f.code, f.path, f.scopes) for f in report.errors if f.code.startswith("namespace.")]
    assert found == [("namespace.kb_prefix_reserved", "namespace.kbPrefix", ("agent",))]
    data["namespace"]["kbPrefix"] = prefix[:-1] + "-kb/"
    assert validator.check_namespace(data).findings == []


@pytest.mark.parametrize("agent", ["current", "previous", "releases", "run", "skills"])
def test_an_agent_name_may_not_name_an_entry_of_the_workshop_root(agent, tmp_path):
    """04-deploy runs rm -rf ~/workshop/<agentName> and 99-cleanup.sh removes it: agentName releases would
    delete every release on the Workshop instance, run the step evidence, skills the skills tree (review
    finding). Refused by the policy, so no such release is ever built."""
    assert agent in validator.RESERVED_AGENT_NAMES
    data = _data()
    data["namespace"]["agentName"] = agent
    report = validator.validate_scenario_policy(data, root=REPO_ROOT / "scenarios" / "maintenance")
    found = [(f.code, f.path, f.scopes) for f in report.errors if f.code.startswith("namespace.")]
    assert found == [("namespace.agent_name_reserved", "namespace.agentName", ("agent",))]
    data["namespace"]["agentName"] = agent + "agent"
    assert validator.check_namespace(data).findings == []
    root = tmp_path / "maintenance"
    shutil.copytree(REPO_ROOT / "scenarios" / "maintenance", root)
    path = root / "scenario.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("agentName: maintassistant", f"agentName: {agent}"),
                    encoding="utf-8")
    with pytest.raises(validator.PackValidationError) as excinfo:
        compiler.compile_pack(load_scenario(path), tmp_path / "build", template_commit="0" * 40)
    assert "namespace.agent_name_reserved" in str(excinfo.value)


def test_the_composed_policy_blocks_long_names_with_repair_scopes():
    data = _data()
    data["namespace"]["agentName"] = "maintassistantaverylongname123"  # 30 characters, evaluator 49
    data["namespace"]["knowledgeBaseName"] = "m" * 59
    report = validator.validate_scenario_policy(data, root=REPO_ROOT / "scenarios" / "maintenance")
    found = {f.code: f.scopes for f in report.errors if f.code.startswith("namespace.")}
    assert found == {"namespace.agent_name_too_long": ("agent",), "namespace.evaluator_name_too_long": ("agent",),
                     "namespace.kb_name_too_long": ("agent",)}
    assert not report.ok


def test_the_schema_mirrors_the_name_limits(tmp_path):
    root = tmp_path / "maintenance"
    shutil.copytree(REPO_ROOT / "scenarios" / "maintenance", root)
    path = root / "scenario.yaml"
    text = path.read_text(encoding="utf-8")
    ok_agent, long_agent = "m" + "a" * 18, "m" + "a" * 19
    path.write_text(text.replace("agentName: maintassistant", f"agentName: {ok_agent}"), encoding="utf-8")
    assert load_scenario(path).data["namespace"]["agentName"] == ok_agent
    path.write_text(text.replace("agentName: maintassistant", f"agentName: {long_agent}"), encoding="utf-8")
    with pytest.raises(ScenarioError) as excinfo:
        load_scenario(path)
    assert any(e.startswith("namespace/agentName") for e in excinfo.value.errors)
    path.write_text(text.replace("knowledgeBaseName: maint-knowledge-base", "knowledgeBaseName: " + "k" * 51), encoding="utf-8")
    with pytest.raises(ScenarioError) as excinfo:
        load_scenario(path)
    assert any(e.startswith("namespace/knowledgeBaseName") for e in excinfo.value.errors)


def test_generation_namespaces_satisfy_the_policy_and_share_its_limits():
    assert (gen.NAMESPACE_AGENT_NAME_MAX, gen.NAMESPACE_KB_NAME_MAX) == (validator.AGENT_NAME_MAX, validator.KNOWLEDGE_BASE_NAME_MAX)
    for project_id in ("northstar-cold-chain-logistics-operation", "insurance-claims-intake", "a" + "b" * 40, "abc",
                       "customizer-releases", "skills", "multimodal", "releases", "run", "current", "previous"):
        data = {"namespace": gen.derive_namespace(project_id)}
        assert validator.check_namespace(data).findings == [], project_id


def test_blank_projects_get_an_agent_name_within_the_limit(tmp_path):
    srv, svc = server_mod.create_server(tmp_path / "data", home=REPO_ROOT)
    try:
        for project_id in ("northstar-cold-chain-logistics-ops", "globex-hr"):
            svc.create_project({"id": project_id, "template": "blank", "packKind": "customer"})
            text = (tmp_path / "data" / "projects" / project_id / "scenario.yaml").read_text(encoding="utf-8")
            agent = yaml.safe_load(text)["namespace"]["agentName"]
            assert agent == gen.derive_namespace(project_id)["agentName"] and len(agent) <= validator.AGENT_NAME_MAX
        assert agent == "globexhr"  # short ids keep today's name
        svc.create_project({"id": "customizer-releases", "template": "blank"})
        text = (tmp_path / "data" / "projects" / "customizer-releases" / "scenario.yaml").read_text(encoding="utf-8")
        assert yaml.safe_load(text)["namespace"]["kbPrefix"] == "customizer-releases-kb/"  # never the release-bundle prefix
        svc.create_project({"id": "releases", "template": "blank"})
        text = (tmp_path / "data" / "projects" / "releases" / "scenario.yaml").read_text(encoding="utf-8")
        assert yaml.safe_load(text)["namespace"]["agentName"] == "releasesagent"  # never ~/workshop/releases
    finally:
        srv.server_close()


# ---------------------------------------------------------------------------
# D6b: exactly one retrieval tool with a required string query
# ---------------------------------------------------------------------------


def test_a_second_retrieval_tool_is_an_error():
    data = _data()
    second = copy.deepcopy(_retrieval(data))
    second["name"] = "retrieve_second_source"
    data["tools"].append(second)
    report = validator.check_gateway_tools(data)
    [finding] = [f for f in report.errors if f.code == "tools.retrieval_count"]
    assert "retrieve_second_source" in finding.message and finding.scopes == ("tools",)


def test_no_retrieval_tool_is_an_error():
    data = _data()
    for tool in data["tools"]:
        if tool["kind"] == "retrieval":
            tool["kind"] = "mock"
            tool["fixtures"] = {"default": {}}
    assert "tools.retrieval_count" in _codes(validator.check_gateway_tools(data))


@pytest.mark.parametrize("schema", [
    {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
    {"type": "object", "properties": {"query": {"type": "integer"}}, "required": ["query"]},
    {"type": "object", "properties": {"query": {"type": "string"}}},
    {"type": "object", "properties": {"query": {"type": "string"}}, "required": []},
    {"type": "object"},
])
def test_the_retrieval_tool_must_require_a_string_query(schema):
    data = _data()
    _retrieval(data)["inputSchema"] = schema
    report = validator.check_gateway_tools(data)
    [finding] = [f for f in report.errors if f.code == "tools.retrieval_query_schema"]
    assert finding.scopes == ("tools",) and finding.path == f"tools.{_retrieval(data)['name']}.inputSchema"


def test_extra_retrieval_properties_are_allowed():
    data = _data()
    _retrieval(data)["inputSchema"] = {"type": "object", "required": ["query"], "properties": {
        "query": {"type": "string"}, "topK": {"type": "integer"}}}
    assert not [f for f in validator.check_gateway_tools(data).findings if f.code.startswith("tools.retrieval")]


def test_prompts_and_skills_name_only_the_packs_own_tool_target(tmp_path):
    import shutil

    from workshop_customizer import validator
    from workshop_customizer.scenario import load_scenario

    root = tmp_path / "it"
    shutil.copytree(REPO_ROOT / "scenarios" / "it-helpdesk", root)
    scenario = load_scenario(root / "scenario.yaml")
    assert not [f for f in validator.validate_scenario_policy(scenario.data, root).findings if f.code == "namespace.foreign_tool_target"]
    baseline = root / scenario.data["prompts"]["baselineFile"]
    baseline.write_text(baseline.read_text(encoding="utf-8") + "\nUse hr-tools to query the policy.\n", encoding="utf-8")
    found = [f for f in validator.validate_scenario_policy(scenario.data, root).findings if f.code == "namespace.foreign_tool_target"]
    assert [(f.severity, f.path, f.scopes) for f in found] == [("error", scenario.data["prompts"]["baselineFile"], ("prompts",))]
    assert "'hr-tools'" in found[0].message and "'it-tools'" in found[0].message
