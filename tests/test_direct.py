"""Tests for engine/workshop_customizer/direct (no AWS: fakes for the stream, CloudWatch and the judges)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer import compiler, guided_report, l1, render  # noqa: E402
from workshop_customizer.direct import aws, judges, run, traces  # noqa: E402
from workshop_customizer.direct.names import DirectNames  # noqa: E402
from workshop_customizer.scenario import load_scenario  # noqa: E402

TEMPLATE_COMMIT = json.loads((REPO / "template-lock.json").read_text())["template"]["commit"]
NAMESPACE = {"agentName": "campusregistrar", "gatewayName": "campus-registrar-gateway", "toolTargetName": "campus-registrar-tools",
             "lambdaFunctionName": "campus-registrar-tools-handler", "knowledgeBaseName": "campus-registrar-knowledge-base"}


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    base = tmp_path_factory.mktemp("direct")
    pack = compiler.compile_pack(load_scenario(REPO / "scenarios" / "hr-default" / "scenario.yaml"), base / "build", template_commit=TEMPLATE_COMMIT)
    return render.render_release(pack, REPO / "upstream", base / "release", template_commit=TEMPLATE_COMMIT).release_dir


def test_direct_names_never_reuse_the_workshop_names():
    n = DirectNames.of(NAMESPACE, account="111122223333", region="us-west-2")
    assert n.harness == "direct_campusregistrar" and "-" not in n.harness and n.memory == "campusregistrar_direct_memory"
    # The Workshop's selectors (99-cleanup, 09, 13) never match the direct Harness or its runtime.
    assert not n.harness.startswith("campusregistrar_") and not f"harness_{n.harness}".startswith("harness_campusregistrar_")
    with pytest.raises(ValueError):
        DirectNames.of({**NAMESPACE, "agentName": "direct"}, account="111122223333", region="us-west-2")
    assert n.gateway == "campus-registrar-gateway-direct" and n.lambda_function == "campus-registrar-tools-handler-direct"
    assert n.target == "campusregistrartools"  # the Workshop's target name: the tools the model sees are identical
    assert n.bucket == "adlc-direct-111122223333-us-west-2" and n.kb_prefix == "direct/campusregistrar/kb/"
    assert n.tags("campus-registrar") == {"adlc:mode": "direct", "adlc:pack": "campus-registrar"}
    assert all(len(name) <= 64 for name in (n.harness_role, n.lambda_role, n.gateway_role, n.lambda_function))


def test_the_stream_gives_what_the_cli_prints_and_l1_reads_it_back(tmp_path):
    asked = run.Asked(index=1, case_id="c", query="q", session_id="direct-" + "a" * 40, actor="a", started_ms=0, probe=True)
    stream = [{"messageStart": {}}, {"contentBlockDelta": {"delta": {"text": "Let me check. "}}},
              {"contentBlockStart": {"start": {"toolUse": {"name": "hrtools___retrieve_hr_policy"}}}},
              {"messageStop": {"stopReason": "tool_use"}}, {"metadata": {"usage": {"inputTokens": 100, "outputTokens": 10}}},
              {"contentBlockDelta": {"delta": {"text": "Ten days."}}}, {"messageStop": {"stopReason": "end_turn"}},
              {"metadata": {"usage": {"inputTokens": 150, "outputTokens": 20}}}]
    run.stream_result(stream, asked)
    assert (asked.text, asked.tools, asked.stop_reason) == ("Let me check. Ten days.", ["hrtools___retrieve_hr_policy"], "end_turn")
    assert (asked.input_tokens, asked.output_tokens) == (250, 30)
    (tmp_path / "q1.out").write_text(run.out_text(asked), encoding="utf-8")
    row = {"index": 1, "sessionId": asked.session_id}
    assert l1.response_for(row, tmp_path, {}) == {"source": "stream", "text": "Let me check. Ten days.", "error": None, "log": None}

    cut = run.Asked(index=2, case_id="c", query="q", session_id="s", actor="a", started_ms=0, probe=False, text="Partial", stop_reason="max_tokens")
    (tmp_path / "q2.out").write_text(run.out_text(cut), encoding="utf-8")
    assert l1.response_for({"index": 2, "sessionId": "s"}, tmp_path, {})["truncated"] is True
    failed = run.Asked(index=3, case_id="c", query="q", session_id="s", actor="a", started_ms=0, probe=False)
    run.stream_result([{"throttlingException": {"message": "Rate exceeded"}}], failed)
    (tmp_path / "q3.out").write_text(run.out_text(failed), encoding="utf-8")
    assert l1.response_for({"index": 3, "sessionId": "s"}, tmp_path, {})["source"] == "error"


class FakeLogs:
    """``filter_log_events`` over in-memory groups (a substring filter, like CloudWatch's quoted pattern)."""

    def __init__(self, groups):
        self.groups = groups

    def get_paginator(self, _name):
        logs = self

        class Paginator:
            def paginate(self, logGroupName, startTime, filterPattern):
                needle = filterPattern.strip('"')
                yield {"events": [{"message": json.dumps(r)} for r in logs.groups.get(logGroupName, []) if needle in json.dumps(r)]}

        return Paginator()


def test_session_spans_carry_their_events_and_wait_until_the_answer_is_there():
    span = {"traceId": "t1", "spanId": "s1", "name": "chat", "startTimeUnixNano": "2", "attributes": {"session.id": "sess-1"}}
    tool = {"traceId": "t1", "spanId": "s2", "name": "execute_tool hrtools___retrieve_hr_policy", "startTimeUnixNano": "1", "attributes": {}}
    events = [{"traceId": "t1", "spanId": "s1", "body": {"output": {"messages": [{"content": {"message": "[{\"text\": \"年假有 **10 天**\"}]"}}]}}},
              {"traceId": "t1", "spanId": "s2", "body": json.dumps({"level": "INFO"})}]
    store = traces.TraceStore(FakeLogs({"aws/spans": [span, tool], "/rt": events}), "/rt", 0, pause=0, sleep=lambda s: None)
    spans = store.session_spans("sess-1")
    assert [s["spanId"] for s in spans] == ["s2", "s1"] and spans[0]["span_events"] == [{"body": {"level": "INFO"}}]
    assert traces.retrieval_trace(spans, "retrieve_hr_policy") == "t1"
    assert traces.TraceStore.has_answer(spans, "第一行\n年假有 **10 天**") and not traces.TraceStore.has_answer(spans, "别的答案")
    assert traces.answer_marker("依据“申请时限”和“审批流程”章节。") == "“申请时限”和“审批流程”章节。"
    waited = []
    done = store.wait_complete({"sess-1": "年假有 **10 天**", "sess-2": "nothing"}, timeout=0, poll=1, progress=waited.append)
    assert set(done) == {"sess-1", "sess-2"} and done["sess-2"] == [] and waited == ["traces complete for the evaluator: 1/2"]


def test_score_rows_read_back_as_the_parser_and_l1_read_them(tmp_path):
    rag = judges.score_row("thelma", "probe-1", "sess-1", "t1", {"value": 0.4, "label": "Fail", "metrics": {"GR": 0.4, "SQC": 1.0, "SP2": 0.5}})
    goal = judges.score_row("mtg", "probe-1", "sess-1", None, {"value": 1.0, "label": "Pass"})
    skipped = judges.score_row("thelma", "probe-2", "sess-2", None, {"value": None, "label": "Skipped"})
    assert guided_report.case_scores({"scores": [rag, goal, skipped]})["probe-1"]["thelma_rag_quality"]["value"] == 0.4
    (tmp_path / "scores.tsv").write_text("".join(judges.tsv_line(r) + "\n" for r in (rag, goal, skipped)), encoding="utf-8")
    read = l1.read_scores(tmp_path / "scores.tsv")
    assert read["sess-1"]["rag"]["metrics"]["SQC"] == 1.0 and read["sess-1"]["goal"]["label"] == "Pass" and "sess-2" not in read


def test_the_release_pins_the_model_and_its_price(release):
    model = run.release_model(release)
    assert model == "us.amazon.nova-2-lite-v1:0" and run.prices(release, model, "us-west-2") == (0.33, 2.75)
    assert run.prices(release, "unknown-model", "us-west-2") is None
    assert run.stability_verdict([0.6, 0.6, 0.6])["verdict"] == "stable"
    assert run.stability_verdict([0.0, 0.611, 0.611]) == {"values": [0.0, 0.611, 0.611], "mean": 0.407, "std": 0.288, "spread": 0.611,
                                                          "verdict": "unstable"}


def test_the_tools_package_and_the_role_mirror_the_workshop(release):
    import io
    import zipfile

    names = zipfile.ZipFile(io.BytesIO(aws.tool_zip(release))).namelist()
    assert names == ["scenario_tools_handler.py", "fixtures.json"]
    assert aws.tool_zip(release) == aws.tool_zip(release)  # deterministic: an unchanged pack never re-uploads the Lambda
    n = DirectNames.of(NAMESPACE, account="111122223333", region="us-west-2")
    policy = aws.harness_role_policy(account="111122223333", region="us-west-2", names=n, gateway_arn="arn:gw", memory_arn="arn:mem")
    actions = {a for st in policy["Statement"] for a in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"])}
    assert {"bedrock:InvokeModel", "bedrock-agentcore:InvokeGateway", "bedrock-agentcore:RetrieveMemoryRecords", "s3:GetObject",
            "ecr:BatchGetImage"} <= actions
    gateway = next(st for st in policy["Statement"] if st["Action"] == "bedrock-agentcore:InvokeGateway")
    assert gateway["Resource"] == "arn:gw"  # this pack's Gateway only
    prov = aws.Provisioner.__new__(aws.Provisioner)
    prov.names = n
    config = prov.harness_config(prompt="P", role_arn="arn:role", tools={"gatewayArn": "arn:gw"}, memory={"arn": "arn:mem", "retrievalConfig": {}},
                                 skills=[{"s3": {"uri": "s3://b/p/"}}], model="m")
    assert config["allowedTools"] == ["@campusregistrartools/*"] and config["maxTokens"] == 8192 and "environment" not in config
    assert config["environmentVariables"] == {"UNIFIED_TRACES_DESTINATION_ENABLED": "false"}  # split telemetry, as the render sets


def test_a_direct_report_is_complete_without_a_model_comparison():
    import teaching_run as tr

    state = json.loads(json.dumps(tr.run_state("hr-reproduced")))
    state["steps"].pop("models")
    state["stepOrder"] = [s for s in state["stepOrder"] if s != "models"]
    scenario = tr.scenario("hr-reproduced")
    guided = guided_report.build_report(state, scenario, generated_at="x")  # a Guided Run needs 12's comparison
    assert guided["status"] == "incomplete" and any("12-compare-models.sh" in m for m in guided["completion"]["missingEvidence"])
    state["mode"] = "direct"
    report = guided_report.build_report(state, scenario, generated_at="x")
    assert report["completion"]["missingEvidence"] == [] and report["modelComparison"]["models"] == {}


def test_the_judge_runner_scores_like_both_lambdas(tmp_path):
    """templates/trace_judge_runner.py on stand-in THELMA and Mind the Goal packages: the handlers' logic and bars."""
    for kind, lam in (("thelma", 'GR_PASS_THRESHOLD = float(os.environ.get("THELMA_GR_THRESHOLD", "0.7"))\n'),
                      ("mtg", 'GSR_PASS_THRESHOLD = float(os.environ.get("MTG_GSR_THRESHOLD", "80"))\n')):
        root = tmp_path / f"{kind}_eval"
        for d in ("shared", "evaluators", "evaluators/thelma", "evaluators/mind_the_goal"):
            (root / d).mkdir(parents=True, exist_ok=True)
            (root / d / "__init__.py").write_text("")
        (root / "lambda_function.py").write_text(lam)
        (root / "shared" / "llm_client.py").write_text("class LLMClient:\n    def __init__(self, model_id, region):\n        self.model_id = model_id\n")
        (root / "span_adapter.py").write_text(
            "def extract_turns_for_trace(spans, target):\n"
            "    return [{'query': s['q'], 'sources': [s['src']], 'response': s['r']} for s in spans if s.get('traceId') == target]\n")
        (root / "evaluators" / "thelma" / "metrics.py").write_text(
            "from types import SimpleNamespace\n"
            "def evaluate_turn_detailed(query, sources, response, llm, embed_fn=None, skip_sp2=False):\n"
            "    return SimpleNamespace(groundedness=0.6666, source_precision_chunk=1, source_precision_fact=0.5, source_query_coverage=0.0,"
            " response_precision=1, response_query_coverage=1), {}\n")
        (root / "session_adapter.py").write_text(
            "from types import SimpleNamespace\ndef rebuild_session(spans):\n    return SimpleNamespace(turns=list(spans))\n")
        for mod, body in (("turn_quality", "def evaluate_session(session, llm):\n    return session.turns, [], None\n"),
                          ("segmentation", "def segment_goals(turns, qualities):\n    return turns\n"),
                          ("gsr", "def build_summary(goals):\n    return {'gsr': 85.0, 'total_goals': 1, 'successful_goals': 1}\n")):
            (root / "evaluators" / "mind_the_goal" / f"{mod}.py").write_text(body)
    jobs = [{"sessionSpans": [{"traceId": "t1", "q": "q", "src": "s", "r": "r"}], "targetTraceId": "t1"},
            {"sessionSpans": [], "targetTraceId": "t9"}]
    env = {"PATH": "/usr/bin:/bin", "THELMA_MODEL": "judge", "MTG_MODEL": "judge"}
    done = subprocess.run([sys.executable, str(judges.RUNNER), str(tmp_path / "thelma_eval"), "thelma"], input=json.dumps(jobs),
                          capture_output=True, text=True, env=env, check=True)
    scored, skipped = json.loads(done.stdout)
    assert scored["value"] == 0.667 and scored["label"] == "Fail" and scored["metrics"]["SQC"] == 0.0 and skipped["label"] == "Skipped"
    done = subprocess.run([sys.executable, str(judges.RUNNER), str(tmp_path / "mtg_eval"), "mtg"], input=json.dumps(jobs),
                          capture_output=True, text=True, env=env, check=True)
    passed, empty = json.loads(done.stdout)
    assert (passed["value"], passed["label"]) == (0.85, "Pass") and empty["label"] == "Skipped"


def test_cleanup_removes_only_the_direct_resources_in_order_and_tolerates_missing_ones(tmp_path, monkeypatch):
    from botocore.exceptions import ClientError

    from workshop_customizer.direct import cleanup as cleanup_mod

    n = DirectNames.of(NAMESPACE, account="111122223333", region="us-west-2")
    calls = []

    class Ctl:
        def __init__(self):
            self.harnesses = [{"harnessName": n.harness, "harnessId": "h-1"}, {"harnessName": "campusregistrar_campusregistrar", "harnessId": "w"},
                              {"harnessName": "campusregistrar_direct", "harnessId": "old-1"}]  # the name before 2026-10-01

        def list_harnesses(self):
            return {"harnesses": list(self.harnesses)}

        def delete_harness(self, harnessId):
            calls.append(("harness", harnessId))
            self.harnesses = [h for h in self.harnesses if h["harnessId"] != harnessId]

        def list_memories(self):
            return {"memories": [{"id": f"{n.memory}-abc"}, {"id": "campusregistrar_campusregistrarmemory-x"}]}

        def delete_memory(self, memoryId):
            calls.append(("memory", memoryId))

        def list_gateways(self):
            return {"items": [{"name": n.gateway, "gatewayId": "g-1"}, {"name": "campus-registrar-gateway", "gatewayId": "w"}]}

        def list_gateway_targets(self, gatewayIdentifier):
            return {"items": [] if ("target", gatewayIdentifier) in calls else [{"targetId": "t-1"}]}

        def delete_gateway_target(self, gatewayIdentifier, targetId):
            calls.append(("target", gatewayIdentifier))

        def delete_gateway(self, gatewayIdentifier):
            calls.append(("gateway", gatewayIdentifier))

    class Other:
        def delete_function(self, FunctionName):
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "DeleteFunction")

        def get_paginator(self, _):
            return type("P", (), {"paginate": staticmethod(lambda **kw: [{"Contents": [{"Key": kw["Prefix"] + "a.md"}]}])})()

        def delete_objects(self, Bucket, Delete):
            calls.append(("objects", len(Delete["Objects"])))

        def list_role_policies(self, RoleName):
            return {"PolicyNames": ["p"]}

        def list_attached_role_policies(self, RoleName):
            return {"AttachedPolicies": []}

        def delete_role_policy(self, RoleName, PolicyName):
            pass

        def delete_role(self, RoleName):
            calls.append(("role", RoleName))

    ctl, other = Ctl(), Other()
    session = type("S", (), {"client": lambda self, name, region_name=None, **kw: ctl if name == "bedrock-agentcore-control" else other})()
    monkeypatch.setattr(cleanup_mod.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    state = tmp_path / "resources.json"
    state.write_text("{}", encoding="utf-8")
    done = cleanup_mod.cleanup(session, n, release_dir=tmp_path, region="us-west-2", profile=None, state_path=state, log=lambda s: None,
                               sleep=lambda s: None)
    assert [c for c in calls if c[0] in ("harness", "memory", "gateway")] == [("harness", "h-1"), ("harness", "old-1"), ("memory", f"{n.memory}-abc"),
                                                                             ("gateway", "g-1")]
    assert ("harness", "w") not in calls  # the Workshop's Harness is never touched
    assert done["tools lambda"] == "already gone" and done["knowledge base"] == "deleted" and ("objects", 1) in calls
    assert [c[1] for c in calls if c[0] == "role"] == [n.harness_role, n.gateway_role, n.lambda_role] and not state.exists()


def test_a_model_comparison_is_summarized_next_to_the_candidate():
    entry = {"model": "us.amazon.nova-pro-v1:0", "invokeErrors": [{"caseId": "x", "error": "ValidationException"}],
             "scores": [{"evaluator": "thelma_rag_quality", "value": 0.5, "metrics": {"SQC": 1.0}},
                        {"evaluator": "thelma_rag_quality", "value": None, "label": "Skipped"}],
             "costLatency": {"averageLatencySeconds": 3.2, "averageCostUsd": 0.004, "priced": True}}
    steps = {"optimize": {"outputs": {"scores": [{"evaluator": "thelma_rag_quality", "value": 0.9, "metrics": {"SQC": 0.5}},
                                                  {"evaluator": "mtg_goal_success", "value": 1.0}]}}}
    summary = run.sweep_summary(entry, "us.amazon.nova-2-lite-v1:0", steps)
    assert (summary["meanGR"], summary["scored"], summary["candidateMeanGR"], summary["candidateMeanSQC"]) == (0.5, 1, 0.9, 0.5)
    assert summary["invokeErrors"] == 1 and summary["averageCostUsd"] == 0.004
    doc = {"verdict": "ready", "reasonCode": "CONTRASTS_REPRODUCED", "releaseVersion": "v", "phenomena": [], "remediation": [],
           "direct": {"seconds": 1, "timings": {}, "harness": {"id": "h"}, "sweep": [summary]}}
    assert "| us.amazon.nova-pro-v1:0 | 0.5 | 1.0 | 3.2 | 0.004 | 1 |" in run.render_markdown(doc)


def test_direct_clients_and_subprocesses_retry_in_standard_mode():
    """Live 2026-10-01: a local proxy dropped TLS connections five attempts in a row and ended a pack's run."""
    seen = {}
    session = type("S", (), {"client": lambda self, name, region_name=None, config=None: seen.update(name=name, region=region_name, config=config)})()
    aws.client(session, "logs", "us-west-2")
    assert seen["config"].retries == {"total_max_attempts": 10, "mode": "standard"} and seen["config"].read_timeout >= 300
    assert aws.RETRY_ENV == {"AWS_RETRY_MODE": "standard", "AWS_MAX_ATTEMPTS": "10"}


def test_the_panel_reads_which_agentcore_evaluators_see_each_phenomenon():
    """direct.panel: reference inputs from the golden case, the evaluator levels from the service, the readings."""
    from workshop_customizer.direct import panel

    case = {"expected": {"requiredTools": ["query_account"], "mustMention": ["186.4"], "mustNotMention": ["身份证号是"], "shouldRefuse": True}}
    [ref] = panel.references(case, "sess-1", "gasutilitytools___")
    assert ref["context"] == {"spanContext": {"sessionId": "sess-1"}} and ref["expectedTrajectory"] == {"toolNames": ["gasutilitytools___query_account"]}
    assert [a["text"] for a in ref["assertions"]][:2] == ["The response mentions 186.4.", "The response does not say 身份证号是."]
    assert panel.references({"expected": {}}, "s", "t___") == []
    records = [{"traceId": "t1", "spanId": "a", "name": "chat"}, {"traceId": "t1", "spanId": "b", "name": "execute_tool x"},
               {"traceId": "t1", "spanId": "a", "eventName": "gen_ai.input"}]
    assert panel.target("TRACE", records) == {"traceIds": ["t1"]} and panel.target("SESSION", records) == {}
    assert panel.target("TOOL_CALL", records) == {"spanIds": ["b"]} and panel.target("TOOL_CALL", records[:1]) is None

    calls = []

    class Runtime:
        def evaluate(self, **request):
            calls.append(request)
            value = {"sess-b": 0.0, "sess-o": 1.0}.get(request["evaluationInput"]["sessionSpans"][0]["traceId"], 0.5)
            return {"evaluationResults": [{"value": value, "label": "x", "explanation": "e", "ignoredReferenceInputFields": ["expectedTrajectory"]}]}

    class Control:
        def list_evaluators(self, **kw):
            if not kw:
                return {"evaluators": [{"evaluatorId": "Builtin.Faithfulness", "level": "TRACE"}], "nextToken": "n"}
            return {"evaluators": [{"evaluatorId": "Builtin.Refusal", "level": "TRACE"}]}

    scorer = panel.Panel(Runtime(), Control(), ["Builtin.Faithfulness", "Builtin.Refusal"], workers=2, log=lambda s: None, sleep=lambda s: None)
    assert scorer.levels == {"Builtin.Faithfulness": "TRACE", "Builtin.Refusal": "TRACE"}
    with pytest.raises(ValueError, match="Builtin.Nope"):
        panel.Panel(Runtime(), Control(), ["Builtin.Nope"], log=lambda s: None)
    jobs = [{"evaluator": e, "caseId": "gap-case", "phase": phase, "sessionId": sid, "records": [{"traceId": sid, "spanId": "s", "name": "chat"}],
             "references": [ref]} for e in scorer.evaluators for phase, sid in (("baseline", "sess-b"), ("optimized", "sess-o"))]
    rows = scorer.score(jobs)
    assert [r["value"] for r in rows] == [0.0, 1.0, 0.0, 1.0] and rows[0]["ignored"] == ["expectedTrajectory"]
    assert calls[0]["evaluationTarget"] == {"traceIds": ["sess-b"]} and calls[0]["evaluationReferenceInputs"] == [ref]
    phenomena = [{"id": "gap", "kind": "retrieval_gap", "caseIds": ["gap-case"]}, {"id": "pf", "kind": "prompt_fixable", "caseIds": ["gap-case"]},
                 {"id": "tool", "kind": "tool_use", "caseIds": ["gap-case"]}]
    cells = {(c["evaluator"], c["phenomenonId"]): c["reading"] for c in panel.matrix(rows, phenomena, {"Builtin.Faithfulness": 0.02})}
    assert cells[("Builtin.Faithfulness", "gap")] == "sees" and cells[("Builtin.Refusal", "gap")] == "sees"  # declining the gap is right
    assert cells[("Builtin.Refusal", "pf")] == "contradicts"  # refusing more on a fixable question is not
    assert cells[("Builtin.Faithfulness", "tool")] == "agrees" and cells[("Builtin.Refusal", "tool")] == "disagrees"
    picks = panel.recommend(panel.matrix(rows, phenomena, {}))
    assert [(r["evaluator"], r["recommended"]) for r in picks] == [("Builtin.Faithfulness", True), ("Builtin.Refusal", False)]
    assert panel.band([0.5, 0.5, 0.5]) == 0.02 and panel.band([0.4, 0.6, 0.5]) == 0.2
    scorer.levels["Builtin.TrajectoryAnyOrderMatch"] = "SESSION"
    assert scorer.evaluate("Builtin.TrajectoryAnyOrderMatch", jobs[0]["records"], [])["label"] == "Skipped"  # a refusal case names no tools

    broken = panel.Panel(type("R", (), {"evaluate": lambda self, **kw: (_ for _ in ()).throw(RuntimeError("throttled"))})(), Control(),
                         ["Builtin.Faithfulness"], log=lambda s: None, sleep=lambda s: None)
    assert broken.score(jobs[:1])[0] | {"sessionId": None} == {"evaluator": "Builtin.Faithfulness", "caseId": "gap-case", "phase": "baseline",
                                                               "sessionId": None, "value": None, "label": "Error", "error": "RuntimeError: throttled"}
    text = "\n".join(panel.render_markdown({"evaluators": ["Builtin.Faithfulness", "Builtin.Refusal"], "matrix": panel.matrix(rows, phenomena, {}),
                                            "bands": {}, "recommendation": picks, "rows": rows},
                                           [{"id": "gap", "kind": "retrieval_gap", "verdict": "reproduced",
                                             "cases": [{"baseline": {"GR": 0.08}, "optimized": {"GR": 0.714}}]}]))
    assert "| gap (retrieval_gap) ✓ | 0.08 → 0.714 | 0 → 1 ✓ | 0 → 1 ✓ |" in text and "**Builtin.Faithfulness** (sees gap, pf)" in text
    assert "Builtin.Refusal on pf" in text


def test_direct_rounds_are_distinct_runs_of_one_release_and_the_rehearsal_counts_them():
    """--repeat: each round's baseline / optimize records carry direct-<epoch>-… ids, so the rehearsal reads the
    earlier rounds as same-release history and reports how often each phenomenon reproduced."""
    import copy

    import teaching_run as tr
    from workshop_customizer import rehearsal

    def round_state(epoch: int) -> dict:
        state = copy.deepcopy(tr.run_state("hr-reproduced"))
        state["mode"] = "direct"
        state["steps"].pop("models")
        state["stepOrder"] = [s for s in state["stepOrder"] if s != "models"]
        for step in ("baseline", "optimize"):
            state["steps"][step].update(commandId=f"direct-{epoch}-{step}", finishedAt=f"2026-10-01T0{epoch}:00:00Z")
        state["updatedAt"] = f"2026-10-01T0{epoch}:00:00Z"
        return state

    scenario = tr.scenario("hr-reproduced")
    doc = rehearsal.build_rehearsal(round_state(2), scenario, history=[("direct round 1", round_state(1))], release_verified=False)
    assert doc["inputs"]["completeRuns"] == 2 and all(p["replication"]["completeRuns"] == 2 for p in doc["phenomena"])
    assert doc["readyForClass"] is False  # never a class verdict
    pid = doc["phenomena"][0]["id"]
    text = run.render_markdown({"verdict": "ready", "reasonCode": "CONTRASTS_REPRODUCED", "releaseVersion": "v", "remediation": [],
                                "phenomena": doc["phenomena"],
                                "direct": {"seconds": 1, "timings": {}, "harness": {"id": "h"},
                                           "rounds": [{"round": 1, "phenomena": {pid: "not_reproduced"}}, {"round": 2, "phenomena": {pid: "reproduced"}}]}})
    assert f"| {pid} | not_reproduced | reproduced | 1/2 |" in text


def test_old_skills_are_left_out_as_the_class_got_them_and_a_missing_13_is_incomplete_not_a_crash(release, tmp_path):
    import shutil as sh

    import teaching_run as tr

    copy = tmp_path / "release"
    sh.copytree(release / "pack" / "skills", copy / "pack" / "skills")
    first = sorted((copy / "pack" / "skills").glob("*/SKILL.md"))[0]
    first.write_text("# No frontmatter\n\nOld release.\n", encoding="utf-8")
    put = []
    prov = aws.Provisioner.__new__(aws.Provisioner)
    prov.release_dir, prov.names, prov.log, prov.skipped_skills = copy, DirectNames.of(NAMESPACE, account="111122223333", region="us-west-2"), lambda s: None, []
    prov.s3 = type("S3", (), {"put_object": lambda self, **kw: put.append(kw["Key"])})()
    sources = prov.ensure_skills()
    assert prov.skipped_skills == [first.parent.name] and all(first.parent.name not in k for k in put) and len(sources) == len(put) >= 1

    state = json.loads(json.dumps(tr.run_state("hr-reproduced")))
    state["mode"] = "direct"
    state["steps"].pop("judge-stability")
    state["stepOrder"] = [s for s in state["stepOrder"] if s != "judge-stability"]
    report = guided_report.build_report(state, tr.scenario("hr-reproduced"), generated_at="x")
    assert report["status"] == "incomplete" and any("13-judge-stability" in m for m in report["completion"]["missingEvidence"])


def test_a_release_ready_in_the_last_round_only_is_not_robust_and_takes_the_failing_rounds_hints():
    hint = {"id": "h03", "code": "RG_REDECLARE", "severity": "blocking", "caseId": "gap-case"}
    rounds = [{"verdict": "not_ready", "phenomena": {"gap": "not_reproduced", "pf": "reproduced"}, "hints": [hint]},
              {"verdict": "ready", "phenomena": {"gap": "reproduced", "pf": "reproduced"}, "hints": []}]
    doc = {"verdict": "ready", "remediation": [{"id": "h01", "code": "PF_BASELINE_ALREADY_PASSES", "severity": "advisory", "caseId": "x"}],
           "direct": {}}
    run.robust(doc, rounds)
    assert doc["direct"]["robust"] is False and doc["direct"]["flaky"] == ["gap"]
    assert doc["remediation"][-1] == {**hint, "id": "round1-h03", "round": 1}
    steady = {"verdict": "ready", "remediation": [], "direct": {}}
    run.robust(steady, [rounds[1], rounds[1]])
    assert steady["direct"] == {"readyRounds": 2, "robust": True, "flaky": []} and steady["remediation"] == []
    assert doc["direct"]["readyRounds"] == 1
    failed = {"verdict": "not_ready", "remediation": [], "direct": {}}
    run.robust(failed, rounds[::-1])  # not ready in the last round: its own remediation already says why
    assert failed["direct"]["robust"] is False and failed["remediation"] == []


def test_the_recommended_evaluators_are_the_fewest_that_see_the_most_none_rewarding_a_failure_first():
    """Live gas 2026-10-01: Faithfulness saw four contrasts but scored one fixable case backwards; Correctness,
    Helpfulness and GoalSuccessRate together saw the same four without contradicting any; nothing saw late-fee."""
    from workshop_customizer.direct import panel

    def cell(e, pid, reading, kind="prompt_fixable"):
        return {"evaluator": e, "phenomenonId": pid, "kind": kind, "reading": reading}

    cells = [cell("F", "pricing", "sees"), cell("F", "late-fee", "contradicts"), cell("F", "transfer", "sees"), cell("F", "gap", "sees", "retrieval_gap"),
             cell("F", "refusal", "sees", "refusal"), cell("C", "pricing", "sees"), cell("C", "gap", "sees", "retrieval_gap"),
             cell("C", "late-fee", "misses"), cell("H", "transfer", "sees"), cell("H", "gap", "sees", "retrieval_gap"),
             cell("G", "refusal", "sees", "refusal"), cell("G", "late-fee", "misses"), cell("G", "tool", "agrees", "tool_use")]
    picks = panel.recommend(cells)
    assert [r["evaluator"] for r in picks if r["recommended"]] == ["C", "H", "G"]
    assert picks[-1] == {"evaluator": "F", "sees": ["pricing", "transfer", "gap", "refusal"], "contradicts": ["late-fee"], "of": 5,
                         "recommended": False}
    assert panel.unseen(cells, picks) == ["late-fee"]
    noisy = [cell("F", "pf", "sees"), cell("H", "gap", "sees", "retrieval_gap"), cell("H", "pf", "contradicts"), cell("H", "x", "contradicts")]
    picks = panel.recommend(noisy)  # H sees the gap but contradicts more than it sees: never recommended
    assert [r["evaluator"] for r in picks if r["recommended"]] == ["F"] and panel.unseen(noisy, picks) == ["gap", "x"]


def test_an_online_evaluation_plan_takes_the_panels_recommendation_and_the_workshop_judge_for_the_rest():
    from workshop_customizer.direct import online

    panel = {"recommendation": [{"evaluator": "Builtin.Correctness", "sees": ["pf", "gap"], "recommended": True},
                                {"evaluator": "Builtin.GoalSuccessRate", "sees": ["refusal"], "recommended": True},
                                {"evaluator": "Builtin.Helpfulness", "sees": ["gap"], "contradicts": ["refusal"], "recommended": False}],
             "unseen": ["late-fee"], "references": False}
    with pytest.raises(ValueError, match="predates"):
        online.plan({k: v for k, v in panel.items() if k != "references"}, agent="a", kind="direct", runtime_id="r-1", role_arn="arn:r")
    planned = online.plan(panel, agent="gasutility", kind="class", runtime_id="harness_gasutility_gasutility-AbC123",
                          role_arn="arn:aws:iam::111122223333:role/gasutility-online-eval",
                          workshop_thelma=online.workshop_thelma_id(["x", "gasutility_thelma_rag_quality-Zz9"], "gasutility"))
    request = planned["request"]
    assert [e["evaluatorId"] for e in request["evaluators"]] == ["Builtin.Correctness", "Builtin.GoalSuccessRate", "gasutility_thelma_rag_quality-Zz9"]
    assert request["dataSourceConfig"] == {"cloudWatchLogs": {"logGroupNames": ["/aws/bedrock-agentcore/runtimes/harness_gasutility_gasutility-AbC123-DEFAULT"],
                                                              "serviceNames": ["harness_gasutility_gasutility.DEFAULT"]}}
    assert request["rule"]["samplingConfig"] == {"samplingPercentage": 10.0} and request["onlineEvaluationConfigName"] == "gasutility_class_online_eval"
    assert any("late-fee" in r for r in planned["reasons"]) and request["enableOnCreate"] is True
    without = online.plan(panel, agent="gasutility", kind="direct", runtime_id="r-1", role_arn="arn:r", workshop_thelma=None)
    assert len(without["request"]["evaluators"]) == 2 and any("not covered online: late-fee" in r for r in without["reasons"])
    with pytest.raises(ValueError):
        online.plan({"recommendation": []}, agent="a", kind="direct", runtime_id="r-1", role_arn="arn:r")
    with pytest.raises(ValueError, match="reference inputs"):  # online, no golden expectations go along: GoalSuccessRate failed refusals
        online.plan({**panel, "references": True}, agent="a", kind="direct", runtime_id="r-1", role_arn="arn:r")
    trust, policy = online.role_documents("111122223333", "us-west-2")
    assert trust["Statement"][0]["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}
    assert {"logs:StartQuery", "bedrock:InvokeModel", "lambda:InvokeFunction"} <= {a for st in policy["Statement"] for a in st["Action"]}


def test_online_results_are_summarized_with_the_failing_sessions_questions():
    from workshop_customizer.direct import online

    def result(sid, name, value, label):
        return {"traceId": "t-" + sid, "timeUnixNano": 1, "attributes": {"session.id": sid, "gen_ai.evaluation.name": name,
                                                                        "gen_ai.evaluation.score.value": value, "gen_ai.evaluation.score.label": label,
                                                                        "gen_ai.evaluation.explanation": "why"}}

    rows = [result("s1", "Builtin.Correctness", 0.0, "Incorrect"), result("s1", "agent_thelma_rag_quality", 0.111, "Fail"),
            result("s2", "Builtin.Refusal", 0.0, "No"), result("s3", "Builtin.Refusal", 1.0, "Yes"),
            result("s2", "Builtin.Correctness", 1.0, "Perfectly Correct"), result("s2", "agent_thelma_rag_quality", None, "Skipped"),
            result("s3", "agent_thelma_rag_quality", 0.75, "Fail")]
    summary = online.summarize(rows)
    assert summary["sessions"] == 3 and summary["failingSessions"] == ["s1", "s3"]  # a THELMA Fail over 0.5 still fails its own bar
    assert summary["evaluators"]["Builtin.Correctness"] == {"count": 2, "mean": 0.5, "failRate": 0.5}
    assert summary["evaluators"]["Builtin.Refusal"] == {"count": 2, "mean": 0.5, "refusalRate": 0.5}  # what happened, never a failure
    assert not [f for f in summary["failing"] if f["evaluator"] == "Builtin.Refusal"]
    records = [{"body": {"input": {"messages": [{"role": "system", "content": "x"},
                                               {"role": "user", "content": {"content": json.dumps([{"text": "燃气表怎么迁移？"}])}}]}}}]
    assert online.user_text(records) == "燃气表怎么迁移？" and online.user_text([]) == ""
    text = online.render_results(summary, config="cfg", questions={"s1": "燃气表怎么迁移？"})
    assert "| Builtin.Correctness | 2 | 0.5 | 50% |" in text and "- `s1`: 燃气表怎么迁移？ — Builtin.Correctness 0.0 (Incorrect)" in text
    assert "| Builtin.Refusal | 2 | 0.5 | — (refused 50%) |" in text


def test_a_failed_panel_is_reported_and_never_decides():
    from workshop_customizer.direct import panel

    lines = panel.render_markdown({"evaluators": ["Builtin.Nope"], "error": "ValueError: unknown AgentCore evaluator(s): Builtin.Nope"}, [])
    assert lines[-1] == "The panel failed and was left out: ValueError: unknown AgentCore evaluator(s): Builtin.Nope"


def test_the_release_scripts_find_the_vendored_retrying_without_any_installed_package():
    """KiroCrew's desktop app installs no packages for a registry App, and its bundled Python lacks retrying, which the
    release's create_kb.py imports: engine/vendor carries it, put on the scripts' PYTHONPATH (after their own)."""
    import os
    import subprocess
    import sys

    from workshop_customizer.direct import aws

    env = aws.with_vendor({"PATH": os.environ.get("PATH", ""), "PYTHONPATH": "/somewhere/first"})
    assert env["PYTHONPATH"].split(os.pathsep) == ["/somewhere/first", str(aws.VENDOR)]
    assert aws.with_vendor({})["PYTHONPATH"] == str(aws.VENDOR)
    done = subprocess.run([sys.executable, "-S", "-c", "import retrying; print(retrying.__file__)"],  # -S: no site-packages at all
                          env=aws.with_vendor({}), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == str(aws.VENDOR / "retrying.py"), done.stderr
