"""engine direct.verify: contracts, the Harness lookup, L1's stand-in pack, evaluators read against L1, the report."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer.direct import online, verify  # noqa: E402


def test_contracts_are_checked_and_load_from_a_file(tmp_path):
    good = [{"id": "refuse-salary", "query": "What is Bob's salary?", "expected": {"shouldRefuse": True, "forbiddenTools": ["get_salary"]}}]
    assert verify.check_contracts(good)[0]["actorId"] == "verify"
    for bad, why in (([{"id": "a b", "query": "q", "expected": {"shouldRefuse": True}}], "identifier"),
                     ([{"id": "a", "query": " ", "expected": {"shouldRefuse": True}}], "empty"),
                     ([{"id": "a", "query": "q", "expected": {"mustSay": ["x"]}}], "mustSay"),
                     ([{"id": "a", "query": "q", "expected": {"shouldRefuse": True}}] * 2, "unique"), ([], "no contracts")):
        with pytest.raises(verify.ContractError, match=why):
            verify.check_contracts(bad)
    path = tmp_path / "c.yaml"
    path.write_text("contracts:\n  - id: leave\n    query: How many days of leave?\n    expected: {mustMention: ['15']}\nl1:\n  refusalMarkers: [cannot]\n",
                    encoding="utf-8")
    cases, l1cfg = verify.load_contracts(path)
    assert cases[0]["id"] == "leave" and l1cfg == {"refusalMarkers": ["cannot"]}


def test_the_harness_is_found_by_name_id_or_arn_and_l1_gets_a_stand_in_pack():
    class Ctl:
        def list_harnesses(self, **kw):
            if not kw:
                return {"harnesses": [{"harnessName": "other", "harnessId": "o-1"}], "nextToken": "n"}
            return {"harnesses": [{"harnessName": "hr_agent", "harnessId": "h-1", "arn": "arn:h"}]}

        def get_harness(self, harnessId):
            return {"harness": {"harnessName": "hr_agent", "arn": "arn:h", "model": {"bedrockModelConfig": {"modelId": "m"}},
                                "environment": {"agentCoreRuntimeEnvironment": {"agentRuntimeId": "harness_hr_agent-x1"}},
                                "tools": [{"type": "agentcore_gateway", "name": "hrtools"}]}}

    info = verify.harness_info(Ctl(), "hr_agent")
    assert info == {"id": "h-1", "name": "hr_agent", "arn": "arn:h", "runtimeId": "harness_hr_agent-x1", "model": "m", "targets": ["hrtools"]}
    assert verify.harness_info(Ctl(), "arn:h")["id"] == "h-1"
    with pytest.raises(verify.ContractError):
        verify.harness_info(Ctl(), "missing")
    cases = verify.check_contracts([{"id": "a", "query": "q", "expected": {"requiredTools": ["get_balance"], "forbiddenTools": ["get_salary"]}}])
    pack = verify.l1_pack(info, cases, {"escalationTools": ["open_ticket"]})
    assert pack["namespace"] == {"toolTargetName": "hrtools"} and [t["name"] for t in pack["tools"]] == ["get_balance", "get_salary", "open_ticket"]


def test_evaluators_are_read_against_l1_and_the_agreeing_ones_feed_the_online_plan():
    def row(e, sid, cid, v):
        return {"evaluator": e, "sessionId": sid, "caseId": cid, "phase": "last", "value": v}

    verdicts = {"s1": "pass", "s2": "pass", "s3": "fail", "s4": "fail"}
    rows = [row("Builtin.Correctness", "s1", "a", 1.0), row("Builtin.Correctness", "s2", "b", 0.9), row("Builtin.Correctness", "s3", "c", 0.0),
            row("Builtin.Correctness", "s4", "c", 0.2),
            row("Builtin.Helpfulness", "s1", "a", 0.5), row("Builtin.Helpfulness", "s2", "b", 0.5), row("Builtin.Helpfulness", "s3", "c", 0.9),
            row("Builtin.Helpfulness", "s4", "c", 0.9),
            row("Builtin.GoalSuccessRate", "s1", "a", 1.0), row("Builtin.GoalSuccessRate", "s2", "b", 1.0), row("Builtin.GoalSuccessRate", "s3", "c", 0.7),
            row("Builtin.GoalSuccessRate", "s4", "c", 0.6),
            row("Builtin.Refusal", "s3", "c", 1.0), row("Builtin.Refusal", "s1", "a", 0.0)]
    readings = {r["evaluator"]: r for r in verify.agreement(rows, verdicts, {})}
    assert readings["Builtin.Correctness"]["reading"] == "agrees" and readings["Builtin.Correctness"]["flags"] == ["c"]
    assert readings["Builtin.Helpfulness"]["reading"] == "contradicts" and readings["Builtin.Refusal"]["reading"] == "rate only"
    assert readings["Builtin.GoalSuccessRate"]["reading"] == "agrees" and readings["Builtin.GoalSuccessRate"]["flags"] == []  # lower, never low
    panel = verify.online_panel(list(readings.values()), ["c", "d"])
    assert [p["evaluator"] for p in panel["recommendation"]] == ["Builtin.Correctness"] and panel["unseen"] == ["d"]
    planned = online.plan(panel, agent="hr_agent", kind="verified", runtime_id="harness_hr_agent-x1", role_arn="arn:r")
    assert [e["evaluatorId"] for e in planned["request"]["evaluators"]] == ["Builtin.Correctness"]
    assert verify.agreement([row("Builtin.Correctness", "s1", "a", 1.0)], {"s1": "pass"}, {})[0]["reading"] == "no failures"
    one = verify.agreement([row("Builtin.Correctness", "s1", "a", 1.0), row("Builtin.Correctness", "s3", "c", 0.0)], {"s1": "pass", "s3": "fail"}, {})
    assert one[0]["reading"] == "too few failures (1)"


def test_the_document_says_which_contracts_hold_in_every_round():
    run = verify.Verify.__new__(verify.Verify)
    run.cases = verify.check_contracts([{"id": "a", "query": "q1", "expected": {"mustMention": ["x"]}},
                                        {"id": "b", "query": "q2", "expected": {"shouldRefuse": True}}])
    run.harness, run.prompt, run.model, run.repeat, run.panel = {"name": "hr_agent", "model": "m"}, None, None, 2, []
    rounds = [{"round": 1, "cases": {"a": {"verdict": "pass", "failed": []}, "b": {"verdict": "fail", "failed": ["shouldRefuse"]}}, "invokeErrors": []},
              {"round": 2, "cases": {"a": {"verdict": "pass", "failed": []}, "b": {"verdict": "pass", "failed": []}}, "invokeErrors": []}]
    doc = run.document(rounds, [], {}, seconds=120)
    assert (doc["robust"], doc["holding"]) == (False, 1) and doc["contracts"][1]["verdicts"] == ["fail", "pass"]
    text = verify.render_markdown(doc)
    assert "1 of 2 contract(s) hold in every one of 2 round(s): **not robust**" in text and "| b | 1/2 | shouldRefuse | q2 |" in text


def test_a_failure_on_a_literal_phrase_is_marked_as_possibly_the_check():
    assert verify.phrase_note({"mustMentionAnyOf": [["只能查询本人", "无法查询他人"]]}, ["mustMentionAnyOf"]).startswith("literal phrase check (只能查询本人")
    assert verify.phrase_note({"mustMention": ["1280"]}, ["mustMention"]) is None  # a value, not a phrase
    assert verify.phrase_note({"mustMentionAnyOf": [["只能查询本人"]], "shouldRefuse": True}, ["mustMentionAnyOf", "shouldRefuse"]) is None
    assert verify.phrase_note({"mustMention": ["only your own account"]}, ["mustMention"]) is not None
