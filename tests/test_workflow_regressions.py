"""Behavioral regressions found while exercising the complete SA workflow."""
import copy
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from test_app_backend import REPO_ROOT, server_mod
from test_guided_report import _scenario, _state
from workshop_customizer.guided_report import build_report
from workshop_customizer.guided_run_store import GuidedRunStore
from workshop_customizer.validator import validate_scenario_policy


@pytest.fixture
def service(tmp_path):
    svc = server_mod.Service(tmp_path, home=REPO_ROOT)
    svc.create_project({"id": "workflow-audit", "template": "it-helpdesk", "packKind": "reference"})
    return svc


def test_tool_confirmation_accepts_schema_tool_name(service):
    result = service.confirm_item("workflow-audit", "retrieve_it_policy", {
        "provenance": "customer_confirmed", "confirmedBy": "Workshop reviewer",
        "confirmationRef": "fictional test fixture",
    })
    assert result["provenance"] == "customer_confirmed"
    assert service.validate("workflow-audit")["ok"]

def test_gateway_schema_rejects_unquoted_yaml_comma_before_build(service):
    data = yaml.safe_load(service.get_project("workflow-audit")["scenarioYaml"])
    data["tools"][-1]["inputSchema"]["properties"]["asset_tag"]["e.g. NWR-4471"] = None
    report = validate_scenario_policy(data)
    assert not report.ok
    assert any(item.code == "tools.gateway_schema" and "e.g. NWR-4471" in item.message
               for item in report.errors)


@pytest.mark.parametrize("edit", ["scenario", "prompt"])
def test_source_edits_refuse_export_and_preflight_until_rebuilt(service, edit):
    pid = "workflow-audit"
    service.build(pid)
    if edit == "scenario":
        data = yaml.safe_load(service.get_project(pid)["scenarioYaml"])
        data["description"] += " Updated for the workshop."
        service.put_scenario(pid, {"yaml": yaml.safe_dump(data, sort_keys=False)})
    else:
        baseline = (service.store.project_dir(pid) / "agent/baseline-prompt.md").read_text(encoding="utf-8")
        # Keep the retrieval hint: the teaching policy blocks a baseline that never names the retrieval tool.
        service.put_file(pid, "agent/baseline-prompt.md", baseline + "\nUpdated for the workshop.\n")
    assert service.get_project(pid)["lastBuild"] is None
    with pytest.raises(server_mod.HttpError, match="build|rebuild"):
        service.export_zip(pid)
    with pytest.raises(server_mod.HttpError, match="build|rebuild"):
        service.release_info(pid)
    with pytest.raises(server_mod.HttpError, match="build|rebuild"):
        service.sync_preflight(pid)
    service.build(pid)
    assert service.export_zip(pid)[0].startswith(b"PK")


def test_direct_source_change_is_detected_before_export(service):
    service.build("workflow-audit")
    prompt = service.store.project_dir("workflow-audit") / "agent/baseline-prompt.md"
    prompt.write_text("Changed outside the app.\n")
    with pytest.raises(server_mod.HttpError, match="rebuild"):
        service.export_zip("workflow-audit")


def test_reset_archives_completed_state_and_refuses_running_step(tmp_path):
    store = GuidedRunStore(tmp_path)
    original = store.create(project_id="demo", release_version="v1", template_commit="c1")
    original["steps"]["setup"]["status"] = "passed"
    store.save(original)
    reset = store.reset(project_id="demo", release_version="v2", template_commit="c1")
    assert reset["releaseVersion"] == "v2"
    assert reset["steps"]["setup"]["status"] == "not_started"
    archived = list((tmp_path / "run/history").glob("*/state.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text())["steps"]["setup"]["status"] == "passed"
    reset["steps"]["setup"]["status"] = "running"
    store.save(reset)
    with pytest.raises(ValueError, match="running"):
        store.reset(project_id="demo", release_version="v3", template_commit="c1")


@pytest.mark.parametrize("step,outputs", [
    ("cost-latency", {"costLatency": {"totalCostUsd": 0.01}}),
    ("models", {"models": {"baseline": None, "comparison": "model-b"},
                "scores": [{"evaluator": "thelma_rag_quality", "value": 0.8}]}),
    ("judge-stability", {"judgeStability": {"verdict": "stable"}}),
])
def test_partial_evidence_cannot_complete_a_report(step, outputs):
    state = copy.deepcopy(_state())
    state["steps"][step]["outputs"] = outputs
    report = build_report(state, _scenario(), generated_at="2026-09-13T00:00:00Z")
    assert report["status"] == "incomplete"
    assert report["completion"]["missingEvidence"]


def test_models_script_supports_explicit_noninteractive_opt_in(service):
    build = service.build("workflow-audit")
    text = (Path(build["releaseDir"]) / "12-compare-models.sh").read_text()
    start = text.index('if [ "${WORKSHOP_NONINTERACTIVE')
    end = text.index('\ncd "$WORKDIR"', start)
    confirmation = text[start:end]
    accepted = subprocess.run(
        ["bash", "-c", 'set -e\nWORKSHOP_NONINTERACTIVE=1\n' + confirmation],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    assert accepted.returncode == 0, accepted.stderr
    refused = subprocess.run(
        ["bash", "-c", 'set -e\nunset WORKSHOP_NONINTERACTIVE\n' + confirmation],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    assert refused.returncode != 0
    assert 'WORKSHOP_MODEL_ID="$COMPARE_MODEL"' in text


def test_cost_estimate_selects_model_rate_and_refuses_unknown_model(service):
    build = service.build("workflow-audit")
    text = (Path(build["releaseDir"]) / "11-cost-latency.sh").read_text()
    pricing = text.split("# WORKSHOP_PRICING_BEGIN\n")[1].split("# WORKSHOP_PRICING_END")[0]
    for model, expected in (("us.amazon.nova-2-lite-v1:0", "0.33/2.75"),
                            ("us.amazon.nova-pro-v1:0", "0.80/3.20")):
        result = subprocess.run(
            ["bash", "-c", f"set -e\nunset PRICE_IN PRICE_OUT\nREGION=us-west-2\nMODEL_ID={model}\n"
             + pricing + '\nprintf "%s/%s" "$PRICE_IN" "$PRICE_OUT"'],
            capture_output=True, text=True,
        )
        assert result.returncode == 0 and result.stdout == expected
    unknown = subprocess.run(
        ["bash", "-c", "set -e\nunset PRICE_IN PRICE_OUT\nREGION=us-west-2\nMODEL_ID=unknown\n" + pricing],
        capture_output=True, text=True,
    )
    assert unknown.returncode != 0
