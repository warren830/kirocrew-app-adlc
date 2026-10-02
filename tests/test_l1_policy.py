"""L1 declaration advisories (designs[2] C3): the ``l1.*`` warnings of ``validator.check_l1_policy``.

l1.no_assertions, l1.escalation_deferred, l1.long_phrase and l1.tool_name_separator; warnings only,
scoped for the repair loop. The hand-off evidence rule agrees with l1.py (what makes shouldEscalate
decidable) and with the report's honesty reading of an absent gap (guided_report.HONESTY_CHECKS).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from workshop_customizer import guided_report, l1, validator

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKS = ("hr-default", "it-helpdesk", "maintenance")


def _data(pack: str = "it-helpdesk") -> dict:
    return yaml.safe_load((REPO_ROOT / "scenarios" / pack / "scenario.yaml").read_text(encoding="utf-8"))


def _case(data: dict, cid: str) -> dict:
    return next(c for c in data["evaluation"]["goldenSet"] if c["id"] == cid)


def _findings(data: dict, code: str) -> list[validator.Finding]:
    return [f for f in validator.check_l1_policy(data).findings if f.code == code]


@pytest.mark.parametrize("pack", PACKS)
def test_reference_packs_raise_no_l1_advisories(pack):
    assert validator.check_l1_policy(_data(pack)).findings == []


def test_the_composed_policy_carries_l1_warnings_that_never_block():
    data = _data()
    _case(data, "p2-response-time")["expected"] = {}
    report = validator.validate_scenario_policy(data)
    [finding] = [f for f in report.findings if f.code.startswith("l1.")]
    assert (finding.severity, finding.code, finding.scopes) == ("warning", "l1.no_assertions", ("golden",))
    assert not [f for f in report.errors if f.code.startswith("l1.")]


@pytest.mark.parametrize(("expected", "fires"), [
    ({}, True),
    ({"shouldRefuse": False, "shouldEscalate": False}, True),  # false is n/a in L1
    ({"mustMention": []}, True),
    ({"mustMentionAnyOf": [[]]}, True),
    ({"mustMention": ["4"]}, False),
    ({"mustMentionAnyOf": [["ticket", "helpdesk"]]}, False),
    ({"mustNotMention": ["x"]}, False),
    ({"requiredTools": ["retrieve_it_policy"]}, False),
    ({"shouldRefuse": True}, False),
])
def test_no_assertions_on_practice_cases_only(expected, fires):
    data = _data()
    practice = _case(data, "p2-response-time")
    practice["expected"] = expected
    found = _findings(data, "l1.no_assertions")
    assert bool(found) is fires
    if fires:
        assert found[0].path == "evaluation.goldenSet.p2-response-time.expected" and "p2-response-time" in found[0].message
    holdout = next(c for c in data["evaluation"]["goldenSet"] if c["set"] == "holdout")
    holdout["expected"] = {}
    assert [f.path for f in _findings(data, "l1.no_assertions")] == [f.path for f in found]


def test_escalation_deferred_without_any_hand_off_evidence():
    data = _data()
    data["evaluation"].pop("l1")
    case = _case(data, "vpn-session-timeout")  # the absent retrieval gap: shouldEscalate + mustMention
    assert case["expected"]["shouldEscalate"] is True and case["expected"].get("mustMention")
    assert _findings(data, "l1.escalation_deferred") == []  # mustMention names the hand-off target
    case["expected"].pop("mustMention")
    case["expected"].pop("mustMentionAnyOf", None)
    [finding] = _findings(data, "l1.escalation_deferred")
    assert finding.scopes == ("golden",) and "vpn-session-timeout" in finding.message
    case["expected"]["mustMentionAnyOf"] = [["ticket", "helpdesk"]]
    assert _findings(data, "l1.escalation_deferred") == []
    case["expected"].pop("mustMentionAnyOf")
    data["evaluation"]["l1"] = {"escalationMarkers": ["open a ticket"]}
    assert _findings(data, "l1.escalation_deferred") == []
    data["evaluation"]["l1"] = {"escalationTools": ["create_ticket"]}
    assert _findings(data, "l1.escalation_deferred") == []


def test_hand_off_evidence_agrees_with_l1_and_the_report():
    """Markers/tools make L1's shouldEscalate decidable; without them it defers (the warning's premise)."""
    answer = {"source": "stream", "text": "I could not find that; please open a ticket with the helpdesk."}
    case = {"expected": {"shouldEscalate": True}}
    undeclared = l1.evaluate_case(case, answer, {}, l1.config_from_pack({}))
    assert validator.handoff_evidence(case["expected"], {}) == ()
    assert next(c for c in undeclared["checks"] if c["code"] == "shouldEscalate")["status"] == "defer"
    cfg = {"escalationMarkers": ["open a ticket"]}
    decided = l1.evaluate_case(case, answer, {}, l1.config_from_pack({"l1": cfg}))
    assert validator.handoff_evidence(case["expected"], cfg) == ("evaluation.l1.escalationMarkers",)
    assert next(c for c in decided["checks"] if c["code"] == "shouldEscalate")["status"] == "pass"
    # The case-level sources are exactly the report's honesty checks besides shouldEscalate itself.
    both = validator.handoff_evidence({"mustMention": ["ticket"], "mustMentionAnyOf": [["a", "b"]]}, None)
    assert set(both) == set(guided_report.HONESTY_CHECKS) - {"shouldEscalate"}


def test_long_phrases_in_any_lexical_list():
    data = _data()
    long = "please reset the password from the self-service portal now"
    assert len(long) > validator.L1_MAX_PHRASE
    expected = _case(data, "p2-response-time")["expected"]
    expected["mustMention"] = ["4", "x" * validator.L1_MAX_PHRASE]  # exactly 40: fine
    assert _findings(data, "l1.long_phrase") == []
    expected["mustNotMention"] = [long]
    expected["mustMentionAnyOf"] = [["short", long + "!"]]
    found = _findings(data, "l1.long_phrase")
    assert len(found) == 2 and all(f.scopes == ("golden",) and f.severity == "warning" for f in found)


def test_tool_name_separator():
    data = _data()
    mock = next(t for t in data["tools"] if t["kind"] == "mock")
    assert _findings(data, "l1.tool_name_separator") == []
    mock["name"] = "lookup___ticket"
    [finding] = _findings(data, "l1.tool_name_separator")
    assert finding.scopes == ("tools",) and finding.path == "tools.lookup___ticket"
