from workshop_customizer.guided_report import build_report
from workshop_customizer.guided_report import step_evidence_errors
from workshop_customizer.guided_run import GUIDE_STEPS, initial_state


def _state():
    steps = initial_state()
    for step in steps.values():
        step.update(status="passed", commandId="cmd-1", ssmStatus="Success", durationSeconds=1.0)
    steps["baseline"]["outputs"] = {"scores": [
        {"evaluator": "thelma_rag_quality", "traceId": "a", "value": 0.8, "label": "Pass"},
        {"evaluator": "thelma_rag_quality", "traceId": "b", "value": 0.6, "label": "Fail"},
        {"evaluator": "mtg_goal_success", "traceId": "c", "value": 0.9, "label": "Pass"},
    ]}
    steps["optimize"]["outputs"] = {"scores": [
        {"evaluator": "thelma_rag_quality", "traceId": "d", "value": 0.85, "label": "Pass"},
        {"evaluator": "thelma_rag_quality", "traceId": "e", "value": 0.75, "label": "Pass"},
        {"evaluator": "mtg_goal_success", "traceId": "f", "value": 0.7, "label": "Pass"},
    ]}
    steps["cost-latency"]["outputs"] = {"costLatency": {"averageLatencySeconds": 1.2, "averageInputTokens": 100, "averageOutputTokens": 40, "totalCostUsd": 0.001}}
    steps["models"]["outputs"] = {
        "models": {"baseline": "model-a", "comparison": "model-b"},
        "scores": [{"evaluator": "thelma_rag_quality", "traceId": "m", "value": 0.78, "label": "Pass"}],
        "costLatency": {"averageLatencySeconds": 0.8, "totalCostUsd": 0.0007},
    }
    steps["judge-stability"]["outputs"] = {"judgeStability": {"values": [0.8, 0.82, 0.79], "mean": 0.803, "std": 0.012, "spread": 0.03, "verdict": "stable"}}
    return {
        "projectId": "northstar",
        "releaseVersion": "northstar-abcdef123456",
        "templateCommit": "2" * 40,
        "stepOrder": [step.id for step in GUIDE_STEPS],
        "steps": steps,
    }


def _scenario():
    return {
        "tools": [{"name": "retrieve_policy", "description": "Retrieve policy"}, {"name": "lookup_order", "description": "Lookup order"}],
        "evaluation": {
            "retrievalToolName": "retrieve_policy",
            "judgeModel": "judge-model",
            "noiseBand": {"thelma_rag_quality": 0.05},
            "goldenSet": [
                {"set": "practice", "category": "normal"},
                {"set": "practice", "category": "boundary"},
                {"set": "holdout", "category": "prohibited"},
            ],
        },
    }


def test_complete_report_contains_decision_evidence_and_regressions():
    report = build_report(_state(), _scenario(), generated_at="2026-09-09T00:00:00Z")
    assert report["status"] == "complete"
    assert report["completion"] == {"allStepsPassed": True, "passed": 15, "total": 15, "missingEvidence": []}
    assert report["quality"]["delta"]["thelma_rag_quality"]["delta"] == 0.1
    assert report["quality"]["regressions"][0]["evaluator"] == "mtg_goal_success"
    assert report["operations"]["costLatency"]["averageInputTokens"] == 100
    assert report["modelComparison"]["models"] == {"baseline": "model-a", "comparison": "model-b"}
    assert report["judgeStability"]["noiseBand"] == 0.05
    assert report["agentEvidence"]["retrievalTool"] == "retrieve_policy"
    assert report["agentEvidence"]["goldenSet"]["holdout"] == 1
    assert "not a production launch" in report["scopeWarning"]


def test_report_is_incomplete_when_a_passed_script_lacks_required_metrics():
    state = _state()
    state["steps"]["baseline"]["outputs"] = {}
    state["steps"]["models"]["outputs"] = {"models": {"baseline": "a", "comparison": "b"}}
    report = build_report(state, _scenario(), generated_at="2026-09-09T00:00:00Z")
    assert report["status"] == "incomplete"
    assert any("baseline evaluation scores" in item for item in report["completion"]["missingEvidence"])
    assert any("model identity and comparison scores" in item for item in report["completion"]["missingEvidence"])


def test_success_without_scores_and_zero_token_placeholders_are_not_evidence():
    assert step_evidence_errors("baseline", {"exitCode": 0, "scores": []})
    assert step_evidence_errors("baseline", {"scores": [
        {"evaluator": "thelma_rag_quality", "value": 0, "label": "Skipped"},
    ]})
    assert step_evidence_errors("cost-latency", {"costLatency": {
        "averageLatencySeconds": 1, "averageInputTokens": 0, "averageOutputTokens": 0, "totalCostUsd": 0,
    }})


def test_unequal_score_counts_do_not_claim_a_meaningful_regression():
    state = _state()
    state["steps"]["optimize"]["outputs"]["scores"] = [
        {"evaluator": "thelma_rag_quality", "value": 0.286, "label": "Fail"},
    ]
    report = build_report(state, _scenario(), generated_at="2026-09-13T00:00:00Z")
    delta = report["quality"]["delta"]["thelma_rag_quality"]
    assert delta["baselineCount"] == 2 and delta["optimizedCount"] == 1
    assert delta["clearsNoiseBand"] is False
    assert report["quality"]["regressions"] == []
    assert report["quality"]["comparisonWarnings"]
