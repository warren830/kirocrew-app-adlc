"""engine console.evaluation: contract sets, datasets, judges, batch evaluation, the online guard (no AWS)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer.console import evaluation as ev  # noqa: E402
from workshop_customizer.console.store import Store  # noqa: E402


def test_contract_sets_are_kept_per_workspace_and_imported_from_a_pack(tmp_path):
    store = Store(tmp_path / "console")
    s = ev.put_contract_set(store, "dev", {"name": "HR", "contracts": [{"id": "a", "query": "q", "expected": {"mustMention": ["15"]}}]})
    assert ev.contract_sets(store, "dev")[0]["count"] == 1 and ev.contract_sets(store, "prod") == []
    with pytest.raises(ev.EvaluationError):
        ev.contract_set(store, "prod", s["id"])
    pack = tmp_path / "workshop" / "projects" / "hr" / "build" / "release" / "pack"
    (pack / "golden").mkdir(parents=True)
    (pack / "pack.json").write_text(json.dumps({"l1": {"refusalMarkers": ["cannot"]}}))
    (pack / "golden" / "practice.json").write_text(json.dumps([{"id": "leave", "query": "Leave?", "expected": {"mustMention": ["15"]}, "actorId": "emp"},
                                                              {"id": "no-checks", "query": "Hi", "expected": {}}]))
    got = ev.import_from_pack(store, "dev", tmp_path / "workshop", "hr")
    assert [c["id"] for c in got["contracts"]] == ["leave"] and got["l1"] == {"refusalMarkers": ["cannot"]} and got["source"] == "pack:hr"
    with pytest.raises(ev.EvaluationError):
        ev.import_from_pack(store, "dev", tmp_path / "workshop", "missing")


def test_a_contract_becomes_a_dataset_example_in_the_services_snake_case_schema():
    [example] = ev.dataset_examples([{"id": "leave", "query": "Leave?", "expected": {"mustMention": ["15"], "requiredTools": ["retrieve_policy"]}}])
    assert example == {"scenario_id": "leave", "turns": [{"input": "Leave?"}], "assertions": ["The response mentions 15."],
                       "expected_trajectory": ["retrieve_policy"]}  # live 2026-10-01: camelCase is refused ("scenario_id" missing)


def test_judges_and_batches_send_what_the_service_takes():
    req = ev.judge_request({"name": "no_invented_rules", "instructions": "Answer Yes when nothing is invented beyond the context."})
    assert req["evaluatorConfig"]["llmAsAJudge"]["ratingScale"]["numerical"][1]["value"] == 1.0 and req["tags"] == {"adlc:console": "1"}
    for bad in ({"name": "1x", "instructions": "x" * 30}, {"name": "ok", "instructions": "short"}, {"name": "ok", "instructions": "x" * 30, "level": "TURN"}):
        with pytest.raises(ev.EvaluationError):
            ev.judge_request(bad)
    sent = []
    session = type("S", (), {"client": lambda self, name, region_name=None, **kw: type("C", (), {
        "start_batch_evaluation": lambda _s, **r: (sent.append(r), {"batchEvaluationId": "b-1", "status": "PENDING"})[1]})()})()
    agent = {"name": "hr_agent", "runtimeId": "harness_hr_agent-x1"}
    scored = ev.start_batch(session, "us-west-2", agent, {"evaluators": ["Builtin.Correctness"], "hours": 6})
    insight = ev.start_batch(session, "us-west-2", agent, {"mode": "insight"})
    assert "insights" not in sent[0] and sent[0]["evaluators"] == [{"evaluatorId": "Builtin.Correctness"}]
    assert "evaluators" not in sent[1] and [i["insightId"] for i in sent[1]["insights"]] == list(ev.INSIGHTS)  # never both (live)
    assert sent[0]["dataSourceConfig"]["cloudWatchLogs"]["serviceNames"] == ["harness_hr_agent.DEFAULT"]
    assert (scored["mode"], insight["mode"]) == ("evaluators", "insight")


def test_only_console_judges_are_deleted_and_online_needs_an_acknowledgement(tmp_path):
    class Ctl:
        def get_evaluator(self, evaluatorId):
            return {"evaluatorArn": f"arn:{evaluatorId}"}

        def list_tags_for_resource(self, resourceArn):
            return {"tags": {"adlc:console": "1"} if "mine" in resourceArn else {}}

        def delete_evaluator(self, evaluatorId):
            self.deleted = evaluatorId

    ctl = Ctl()
    session = type("S", (), {"client": lambda self, name, region_name=None, **kw: ctl})()
    with pytest.raises(ev.EvaluationError):
        ev.delete_judge(session, "us-west-2", "someone_elses")
    assert ev.delete_judge(session, "us-west-2", "mine_judge") == {"deleted": "mine_judge"}
    with pytest.raises(ev.EvaluationError, match="acknowledged"):
        ev.create_online(None, "dev", "verify-0123456789", {})


def test_a_recommendation_reads_the_agents_traces_and_its_evaluators():
    agent = {"name": "hr_agent", "runtimeId": "harness_hr_agent-x1"}
    req = ev.recommendation_request(agent, {"days": 3}, account="111122223333", region="us-west-2", prompt="Be helpful.", tools=[])
    config = req["recommendationConfig"]["systemPromptRecommendationConfig"]
    logs = config["agentTraces"]["cloudwatchLogs"]
    assert req["type"] == "SYSTEM_PROMPT_RECOMMENDATION" and config["systemPrompt"] == {"text": "Be helpful."} and req["tags"] == {"adlc:console": "1"}
    assert logs["logGroupArns"] == ["arn:aws:logs:us-west-2:111122223333:log-group:/aws/bedrock-agentcore/runtimes/harness_hr_agent-x1-DEFAULT",
                                    "arn:aws:logs:us-west-2:111122223333:log-group:aws/spans"] and logs["serviceNames"] == ["harness_hr_agent.DEFAULT"]
    assert (logs["endTime"] - logs["startTime"]).days == 3
    assert config["evaluationConfig"]["evaluators"] == [{"evaluatorArn": "arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate"}]
    tools = [{"toolName": "hrtools___retrieve_policy", "description": "Search the HR policies."}]
    tool_req = ev.recommendation_request(agent, {"type": "toolDescription"}, account="1", region="us-west-2", prompt=None, tools=tools)
    assert tool_req["recommendationConfig"]["toolDescriptionRecommendationConfig"]["toolDescription"]["toolDescriptionText"]["tools"] == [
        {"toolName": "hrtools___retrieve_policy", "toolDescription": {"text": "Search the HR policies."}}]
    for body, match in (({"type": "toolDescription", "source": "batch", "batchEvaluationArn": "arn:aws:bedrock-agentcore:x"}, "refuses a batch"),
                        ({"type": "toolDescription"}, "no tool descriptions"), ({"source": "online"}, "onlineEvaluationConfigArn"),
                        ({"evaluators": ["x" * 2]}, "evaluators")):
        with pytest.raises(ev.EvaluationError, match=match):
            ev.recommendation_request(agent, body, account="1", region="us-west-2", prompt="p", tools=[] if "toolDescription" in str(body) else tools)


def test_a_chinese_prompt_is_analysed_in_english_and_the_recommendation_comes_back_in_chinese(tmp_path):
    calls = {"converse": [], "start": []}

    class Runtime:
        def converse(self, modelId, system, messages, inferenceConfig):
            text = messages[0]["content"][0]["text"]
            calls["converse"].append((system[0]["text"], text))
            out = "You are the HR assistant." if "English" in system[0]["text"] else "你是人事助手，先检索再回答。"
            return {"output": {"message": {"content": [{"text": out}]}}}

    class Data:
        def start_recommendation(self, **request):
            calls["start"].append(request)
            return {"recommendationId": "rec-1", "status": "PENDING"}

        def get_recommendation(self, recommendationId):
            return {"name": "n", "type": "SYSTEM_PROMPT_RECOMMENDATION", "status": "COMPLETED", "recommendationConfig": calls["start"][0]["recommendationConfig"],
                    "recommendationResult": {"systemPromptRecommendationResult": {"recommendedSystemPrompt": "Search first, then answer.", "explanation": "e"}}}

    class Ctl:
        def get_harness(self, harnessId):
            return {"harness": {"harnessName": "hr_agent", "harnessId": "h-1", "arn": "arn:h", "status": "READY", "systemPrompt": [{"text": "你是人事助手。"}],
                                "environment": {"agentCoreRuntimeEnvironment": {"agentRuntimeId": "harness_hr_agent-x1"}}}}

        def list_tags_for_resource(self, resourceArn):
            return {"tags": {}}

    clients = {"bedrock-runtime": Runtime(), "bedrock-agentcore": Data(), "bedrock-agentcore-control": Ctl()}
    session = type("S", (), {"client": lambda self, name, region_name=None, **kw: clients[name]})()
    ws = {"id": "dev", "accountId": "111122223333", "region": "us-west-2"}
    console = type("C", (), {"store": Store(tmp_path), "workspaces": type("W", (), {"get": lambda _s, w: ws, "verify": lambda _s, w: {},
                                                                                      "session": lambda _s, w: session})()})()
    ev.start_recommendation(console, "dev", {"agentId": "h-1"})
    sent = calls["start"][0]["recommendationConfig"]["systemPromptRecommendationConfig"]["systemPrompt"]["text"]
    assert sent == "You are the HR assistant."  # live 2026-10-01: AgentCore refuses Chinese prompts as a "prompt attack"
    got = ev.recommendation(console, "dev", "rec-1")
    assert got["current"] == "你是人事助手。" and got["analyzedAs"] == "You are the HR assistant."
    assert got["recommended"] == "你是人事助手，先检索再回答。" and got["recommendedAsAnalyzed"] == "Search first, then answer."
    ev.recommendation(console, "dev", "rec-1")
    assert len(calls["converse"]) == 2  # translated back once, then kept
    ev.start_recommendation(console, "dev", {"agentId": "h-1", "translate": False})
    assert calls["start"][1]["recommendationConfig"]["systemPromptRecommendationConfig"]["systemPrompt"]["text"] == "你是人事助手。"
