"""engine console.governance: engines, Cedar policies, generation, attaching to a Gateway, rules, rate limits, the
resource policy and the decision log, on fakes that record every request (no AWS). The shapes are the live ones of
2026-10-01 (us-west-2)."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, ParamValidationError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer.console import governance as gov  # noqa: E402

ACCOUNT, REGION = "111122223333", "us-west-2"
ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}"
MINE, THEIRS = "shop_pe-abcdefghij", "team_pe-abcdefghij"
SHOP, TEAM = "shop-gw-abcdefghij", "team-gw-klmnopqrst"
WHEN = datetime(2026, 10, 1, 13, 50, tzinfo=timezone.utc)
CEDAR = f'forbid(principal, action == AgentCore::Action::"tools___issue_refund", resource == AgentCore::Gateway::"{ARN}:gateway/{SHOP}") when {{ context.input.amount > 100 }};'
PERMIT = f'permit(principal, action == AgentCore::Action::"tools___get_balance", resource == AgentCore::Gateway::"{ARN}:gateway/{SHOP}");'


def aws_error(code: str, message: str, status: int = 400, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": status}}, op)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(gov, "_pause", lambda seconds: None)


class Control:
    """bedrock-agentcore-control: two engines (the console's, a team's), two Gateways (likewise); every call recorded."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.engines = {eid: {"policyEngineId": eid, "name": eid.split("-")[0], "policyEngineArn": f"{ARN}:policy-engine/{eid}", "status": "ACTIVE",
                              "statusReasons": [], "createdAt": WHEN, "updatedAt": WHEN} for eid in (MINE, THEIRS)}
        self.policies: dict[str, dict[str, dict]] = {MINE: {}, THEIRS: {"team_rule-abcdefghij": self._policy(THEIRS, "team_rule-abcdefghij", PERMIT, "ACTIVE")}}
        self.gateways = {gid: {"gatewayId": gid, "gatewayArn": f"{ARN}:gateway/{gid}", "gatewayUrl": f"https://{gid}.example/mcp", "name": gid.rsplit("-", 1)[0],
                               "description": "tools", "roleArn": f"arn:aws:iam::{ACCOUNT}:role/{gid}-role", "protocolType": "MCP",
                               "protocolConfiguration": {"mcp": {"searchType": "SEMANTIC"}}, "authorizerType": "AWS_IAM", "exceptionLevel": "DEBUG",
                               "status": "READY", "createdAt": WHEN, "updatedAt": WHEN, "webAclArn": "arn:aws:wafv2:x",
                               "workloadIdentityDetails": {"workloadIdentityArn": "arn:wi"}} for gid in (SHOP, TEAM)}
        self.tags = {f"{ARN}:policy-engine/{MINE}": {"adlc:console": "1"}, f"{ARN}:gateway/{SHOP}": {"adlc:console": "1"}}
        self.settles = "ACTIVE"  # what the next created / updated policy settles to
        self.reasons: list[str] = []
        self.update_errors: list[Exception] = []
        self.rules = {SHOP: [{"ruleId": "11111111-2222-3333-4444-555555555555", "gatewayArn": f"{ARN}:gateway/{SHOP}", "priority": 5,
                              "actions": [{"routeToTarget": {"staticRoute": {"targetName": "web"}}}], "status": "ACTIVE", "createdAt": WHEN,
                              "system": {"managedBy": "experiments"}}]}
        self.resource_policies: dict[str, str] = {}

    def _rec(self, op, **kw):
        self.calls.append((op, kw))

    def ops(self):
        return [op for op, _ in self.calls]

    def last(self, op):
        return next(kw for o, kw in reversed(self.calls) if o == op)

    @staticmethod
    def _policy(engine, pid, statement, status, kind="cedar", mode="ACTIVE"):
        return {"policyId": pid, "name": pid.split("-")[0], "policyEngineId": engine, "policyArn": f"{ARN}:policy-engine/{engine}/policy/{pid}",
                "status": status, "enforcementMode": mode, "definition": {kind: {"statement": statement}}, "statusReasons": [], "createdAt": WHEN, "updatedAt": WHEN}

    # tags
    def list_tags_for_resource(self, resourceArn):
        return {"tags": dict(self.tags.get(resourceArn, {}))}

    # engines
    def list_policy_engines(self, **kw):
        return {"policyEngines": list(self.engines.values())}

    def get_policy_engine(self, policyEngineId):
        return dict(self.engines[policyEngineId])

    def create_policy_engine(self, **kw):
        self._rec("create_policy_engine", **kw)
        return {"policyEngineId": f"{kw['name']}-zzzzzzzzzz", "policyEngineArn": f"{ARN}:policy-engine/{kw['name']}-zzzzzzzzzz", "status": "CREATING"}

    def delete_policy_engine(self, policyEngineId):
        self._rec("delete_policy_engine", policyEngineId=policyEngineId)
        return {"status": "DELETING"}

    # policies
    def list_policies(self, policyEngineId, **kw):
        return {"policies": [dict(p) for p in self.policies[policyEngineId].values()]}

    def get_policy(self, policyEngineId, policyId):
        if policyId not in self.policies[policyEngineId]:
            raise aws_error("ResourceNotFoundException", "no such policy", 404, "GetPolicy")
        p = self.policies[policyEngineId][policyId]
        if p["status"] in ("CREATING", "UPDATING"):  # settles on the next read
            p["status"], p["statusReasons"] = self.settles, list(self.reasons)
            if p.get("pending") and self.settles == "ACTIVE":
                p["definition"] = p.pop("pending")
            p.pop("pending", None)
            return {**p, "status": "CREATING"}
        return dict(p)

    def create_policy(self, **kw):
        self._rec("create_policy", **kw)
        statement = gov.statement_of(kw["definition"]) or "generated"
        if "PARSE" in statement:
            raise aws_error("ValidationException", "When parsing the policy statement, the following errors occurred:\n* unexpected token `,`", 400, "CreatePolicy")
        pid = f"{kw['name']}-{len(self.calls):010d}"
        kind = "policy" if "policyGeneration" in kw["definition"] else "cedar"
        self.policies[kw["policyEngineId"]][pid] = self._policy(kw["policyEngineId"], pid, statement, "CREATING", kind, kw.get("enforcementMode", "ACTIVE"))
        return {"policyId": pid, "status": "CREATING"}

    def update_policy(self, **kw):
        self._rec("update_policy", **kw)
        p = self.policies[kw["policyEngineId"]][kw["policyId"]]
        p["status"] = "UPDATING"
        if "definition" in kw:
            p["pending"] = kw["definition"]
        if "enforcementMode" in kw:
            p["enforcementMode"] = kw["enforcementMode"]
        return {"status": "UPDATING"}

    def delete_policy(self, policyEngineId, policyId):
        self._rec("delete_policy", policyEngineId=policyEngineId, policyId=policyId)
        self.policies[policyEngineId].pop(policyId, None)
        return {"status": "DELETING"}

    # generation
    def start_policy_generation(self, **kw):
        self._rec("start_policy_generation", **kw)
        return {"policyGenerationId": f"{kw['name']}-gggggggggg", "name": kw["name"], "status": "GENERATING"}

    def get_policy_generation(self, policyEngineId, policyGenerationId):
        return {"policyGenerationId": policyGenerationId, "name": "g", "status": "GENERATED", "resource": {"arn": f"{ARN}:gateway/{SHOP}"},
                "statusReasons": [], "createdAt": WHEN}

    def list_policy_generation_assets(self, **kw):
        return {"policyGenerationAssets": [
            {"policyGenerationAssetId": "g-aaaaaaaaaa", "definition": {"policy": {"statement": CEDAR}}, "rawTextFragment": "No refund above 100.", "findings": []},
            {"policyGenerationAssetId": "g-bbbbbbbbbb", "definition": {"policy": {"statement": PERMIT}}, "rawTextFragment": "Anyone may look up a balance.",
             "findings": [{"type": "ALLOW_ALL", "description": "Overly Permissive"}]},
            {"policyGenerationAssetId": "g-cccccccccc", "rawTextFragment": "Only managers on weekends.",
             "findings": [{"type": "INVALID", "description": "Non-translatable: cannot be expressed in Dogwood"}]}]}

    def list_policy_generations(self, **kw):
        return {"policyGenerations": [{"policyGenerationId": "g-gggggggggg", "name": "g", "status": "GENERATED", "resource": {"arn": "x"}, "createdAt": WHEN}]}

    # gateways
    def list_gateways(self, **kw):
        return {"items": [{"gatewayId": g["gatewayId"], "name": g["name"]} for g in self.gateways.values()]}

    def get_gateway(self, gatewayIdentifier):
        if gatewayIdentifier not in self.gateways:
            raise aws_error("ResourceNotFoundException", "no such gateway", 404, "GetGateway")
        return json.loads(json.dumps(self.gateways[gatewayIdentifier], default=str)) | {"createdAt": WHEN, "updatedAt": WHEN}

    def update_gateway(self, **kw):
        self._rec("update_gateway", **kw)
        if self.update_errors:
            raise self.update_errors.pop(0)
        g = self.gateways[kw["gatewayIdentifier"]]
        g.pop("policyEngineConfiguration", None)
        if kw.get("policyEngineConfiguration"):
            g["policyEngineConfiguration"] = dict(kw["policyEngineConfiguration"])
        return {"status": "UPDATING"}

    # rules and rate limits
    def list_gateway_rules(self, gatewayIdentifier, **kw):
        return {"gatewayRules": list(self.rules.get(gatewayIdentifier, []))}

    def get_gateway_rule(self, gatewayIdentifier, ruleId):
        return next(r for r in self.rules.get(gatewayIdentifier, []) if r["ruleId"] == ruleId)

    def create_gateway_rule(self, **kw):
        self._rec("create_gateway_rule", **kw)
        return {"ruleId": "99999999-2222-3333-4444-555555555555", **{k: v for k, v in kw.items() if k != "gatewayIdentifier"}, "status": "CREATING", "createdAt": WHEN}

    def delete_gateway_rule(self, **kw):
        self._rec("delete_gateway_rule", **kw)
        return {"ruleId": kw["ruleId"], "status": "DELETING"}

    def list_gateway_rate_limits(self, gatewayIdentifier, **kw):
        return {"rateLimits": []}

    def create_gateway_rate_limit(self, **kw):
        self._rec("create_gateway_rate_limit", **kw)
        return {"rateLimitId": kw.get("rateLimitId", "rl-1"), **kw, "status": "CREATING", "createdAt": WHEN, "updatedAt": WHEN}

    def delete_gateway_rate_limit(self, **kw):
        self._rec("delete_gateway_rate_limit", **kw)
        return {"rateLimitId": kw["rateLimitId"], "status": "DELETING"}

    # resource policy
    def get_resource_policy(self, resourceArn):
        return {"policy": self.resource_policies[resourceArn]} if resourceArn in self.resource_policies else {}

    def put_resource_policy(self, resourceArn, policy):
        self._rec("put_resource_policy", resourceArn=resourceArn, policy=policy)
        self.resource_policies[resourceArn] = policy
        return {"policy": policy}

    def delete_resource_policy(self, resourceArn):
        self._rec("delete_resource_policy", resourceArn=resourceArn)
        if resourceArn not in self.resource_policies:
            raise aws_error("ResourceNotFoundException", "Resource Policy doesn't exist for requested arn", 404, "DeleteResourcePolicy")
        del self.resource_policies[resourceArn]
        return {}


class Iam:
    def __init__(self, console_roles=()):
        self.console_roles, self.calls = set(console_roles), []

    def list_role_tags(self, RoleName):
        return {"Tags": [{"Key": "adlc:console", "Value": "1"}] if RoleName in self.console_roles else []}

    def put_role_policy(self, **kw):
        self.calls.append(("put_role_policy", kw))


class Logs:
    """CloudWatch Logs: deliveries, the Insights queries and the delivery resources the decision log creates."""

    def __init__(self, deliveries=(), records=(), spans=()):
        self.calls: list[tuple[str, dict]] = []
        self.sources, self.deliveries, self.destinations = [], [], []
        self.tags: dict[str, dict] = {}
        self.records, self.spans = list(records), list(spans)
        for kind, dest_type, group, tagged in deliveries:
            name = f"{SHOP}-{'logs' if kind == 'APPLICATION_LOGS' else 'traces'}-source"
            dest = f"arn:aws:logs:{REGION}:{ACCOUNT}:delivery-destination:{name.replace('source', 'destination')}"
            self.sources.append({"name": name, "arn": f"arn:aws:logs:{REGION}:{ACCOUNT}:delivery-source:{name}", "resourceArns": [f"{ARN}:gateway/{SHOP}"], "logType": kind})
            self.deliveries.append({"id": f"d-{kind}", "arn": f"arn:aws:logs:{REGION}:{ACCOUNT}:delivery:d-{kind}", "deliverySourceName": name,
                                    "deliveryDestinationArn": dest, "deliveryDestinationType": dest_type})
            self.destinations.append({"name": dest.rsplit(":", 1)[-1], "arn": dest, "deliveryDestinationType": dest_type,
                                      "deliveryDestinationConfiguration": {"destinationResourceArn": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{group}" if group else ""}})
            if tagged:
                for arn in (self.sources[-1]["arn"], self.deliveries[-1]["arn"], dest):
                    self.tags[arn] = {"adlc:console": "1"}

    def _rec(self, op, **kw):
        self.calls.append((op, kw))

    def describe_delivery_sources(self, **kw):
        return {"deliverySources": list(self.sources)}

    def describe_deliveries(self, **kw):
        return {"deliveries": list(self.deliveries)}

    def describe_delivery_destinations(self, **kw):
        return {"deliveryDestinations": list(self.destinations)}

    def list_tags_for_resource(self, resourceArn):
        return {"tags": self.tags.get(resourceArn, {})}

    def start_query(self, **kw):
        self._rec("start_query", **kw)
        return {"queryId": "q-1"}

    def get_query_results(self, queryId):
        group = self.last("start_query")["logGroupName"]
        rows = self.spans if group == "aws/spans" else self.records
        return {"status": "Complete", "results": [[{"field": "@message", "value": json.dumps(r)}] for r in rows]}

    def last(self, op):
        return next(kw for o, kw in reversed(self.calls) if o == op)

    def create_log_group(self, **kw):
        self._rec("create_log_group", **kw)

    def put_retention_policy(self, **kw):
        self._rec("put_retention_policy", **kw)

    def put_delivery_source(self, **kw):
        self._rec("put_delivery_source", **kw)
        return {"deliverySource": {"name": kw["name"], "arn": f"arn:aws:logs:{REGION}:{ACCOUNT}:delivery-source:{kw['name']}"}}

    def put_delivery_destination(self, **kw):
        self._rec("put_delivery_destination", **kw)
        return {"deliveryDestination": {"name": kw["name"], "arn": f"arn:aws:logs:{REGION}:{ACCOUNT}:delivery-destination:{kw['name']}"}}

    def create_delivery(self, **kw):
        self._rec("create_delivery", **kw)
        return {"delivery": {"id": "new-delivery"}}

    def delete_delivery(self, **kw):
        self._rec("delete_delivery", **kw)

    def delete_delivery_source(self, **kw):
        self._rec("delete_delivery_source", **kw)

    def delete_delivery_destination(self, **kw):
        self._rec("delete_delivery_destination", **kw)


class CloudWatch:
    """AWS/Bedrock-AgentCore: the same decisions under overlapping dimension sets (as published live)."""

    def __init__(self, streams):
        self.streams, self.queries = streams, []  # [(metric name, {dimensions}, value)]

    def get_paginator(self, name):
        def paginate(Namespace, MetricName, Dimensions):
            want = {d["Name"]: d["Value"] for d in Dimensions}
            found = [{"Namespace": Namespace, "MetricName": m, "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()]}
                     for m, dims, _ in self.streams if m == MetricName and all(dims.get(k) == v for k, v in want.items())]
            return [{"Metrics": found}]
        return type("P", (), {"paginate": staticmethod(paginate)})()

    def get_metric_data(self, MetricDataQueries, StartTime, EndTime, **kw):
        self.queries += MetricDataQueries
        out = []
        for q in MetricDataQueries:
            metric = q["MetricStat"]["Metric"]
            dims = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
            value = next(v for m, d, v in self.streams if m == metric["MetricName"] and d == dims)
            out.append({"Id": q["Id"], "Values": [value - 1, 1] if value > 1 else [value]})
        return {"MetricDataResults": out}


class Session:
    def __init__(self, ctl=None, logs=None, cw=None, iam=None):
        self.ctl, self.logs, self.cw, self.iam = ctl or Control(), logs or Logs(), cw or CloudWatch([]), iam or Iam()

    def client(self, name, region_name=None, **kw):
        if name == "sts":
            return type("Sts", (), {"get_caller_identity": lambda _s: {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/sa"}})()
        return {"bedrock-agentcore-control": self.ctl, "logs": self.logs, "cloudwatch": self.cw, "iam": self.iam}[name]


MUTATIONS = {"create_policy_engine", "delete_policy_engine", "create_policy", "update_policy", "delete_policy", "start_policy_generation", "update_gateway",
             "create_gateway_rule", "delete_gateway_rule", "create_gateway_rate_limit", "delete_gateway_rate_limit", "put_resource_policy", "delete_resource_policy"}


# -- engines and policies -------------------------------------------------------------------------------------------

def test_engines_are_created_tagged_and_only_the_consoles_are_changed():
    s = Session()
    out = gov.create_engine(s, REGION, {"name": "refund_rules", "description": "Refund limits"})
    assert s.ctl.last("create_policy_engine") == {"name": "refund_rules", "tags": {"adlc:console": "1"}, "description": "Refund limits"}
    assert out["status"] == "CREATING"
    with pytest.raises(gov.GovernanceError):
        gov.create_engine(s, REGION, {"name": "1-bad"})
    before = len(s.ctl.calls)
    for call in (lambda: gov.create_policy(s, REGION, THEIRS, {"name": "p", "statement": CEDAR}),
                 lambda: gov.update_policy(s, REGION, THEIRS, "team_rule-abcdefghij", {"enforcementMode": "LOG_ONLY"}),
                 lambda: gov.delete_policy(s, REGION, THEIRS, "team_rule-abcdefghij"),
                 lambda: gov.validate_policy(s, REGION, THEIRS, {"statement": CEDAR}),
                 lambda: gov.start_generation(s, REGION, THEIRS, {"gatewayId": SHOP, "text": "No refunds above 100."}),
                 lambda: gov.delete_engine(s, REGION, THEIRS, {"deletePolicies": True})):
        with pytest.raises(gov.GovernanceError) as refused:
            call()
        assert refused.value.status == 403
    assert len(s.ctl.calls) == before  # nothing was sent for someone else's engine
    assert [p["name"] for p in gov.policies(s, REGION, THEIRS)] == ["team_rule"]  # reading is fine


def test_a_policy_is_cedar_validated_by_the_service_and_only_logged_until_made_active():
    req = gov.policy_request({"name": "refund_cap", "statement": CEDAR, "description": "refunds above 100 need a person"})
    assert req == {"name": "refund_cap", "validationMode": "FAIL_ON_ANY_FINDINGS", "enforcementMode": "LOG_ONLY",
                   "definition": {"cedar": {"statement": CEDAR}}, "description": "refunds above 100 need a person"}
    assert gov.policy_request({"name": "p", "statement": PERMIT, "ignoreFindings": True, "enforcementMode": "ACTIVE"})["validationMode"] == "IGNORE_ALL_FINDINGS"
    assert gov.policy_request({"name": "p", "generationId": "g-gggggggggg", "assetId": "g-aaaaaaaaaa"})["definition"] == {
        "policyGeneration": {"policyGenerationId": "g-gggggggggg", "policyGenerationAssetId": "g-aaaaaaaaaa"}}
    for bad in ({"name": "p", "statement": "permit(principal, action, x);"}, {"name": "p", "statement": "x" * 60},
                {"name": "p", "statement": CEDAR, "enforcementMode": "ENFORCE"}, {"name": "has-dash", "statement": CEDAR},
                {"name": "p", "generationId": "g-gggggggggg"}):
        with pytest.raises(gov.GovernanceError):
            gov.policy_request(bad)
    s = Session()
    s.ctl.settles, s.ctl.reasons = "CREATE_FAILED", ["attribute `input.nope` in context ... not found"]
    out = gov.create_policy(s, REGION, MINE, {"name": "refund_cap", "statement": CEDAR})
    assert s.ctl.last("create_policy")["policyEngineId"] == MINE and s.ctl.last("create_policy")["enforcementMode"] == "LOG_ONLY"
    assert out["status"] == "CREATE_FAILED" and out["statusReasons"] == s.ctl.reasons and out["statement"] == CEDAR
    assert isinstance(out["createdAt"], str)


def test_an_update_keeps_the_definition_kind_and_always_sends_the_validation_mode():
    s = Session()
    s.ctl.policies[MINE]["gen_rule-abcdefghij"] = Control._policy(MINE, "gen_rule-abcdefghij", PERMIT, "ACTIVE", kind="policy")
    gov.update_policy(s, REGION, MINE, "gen_rule-abcdefghij", {"statement": CEDAR})
    assert s.ctl.last("update_policy") == {"policyEngineId": MINE, "policyId": "gen_rule-abcdefghij", "validationMode": "FAIL_ON_ANY_FINDINGS",
                                           "definition": {"policy": {"statement": CEDAR}}}  # a generated policy stays a `policy`
    gov.update_policy(s, REGION, MINE, "gen_rule-abcdefghij", {"enforcementMode": "ACTIVE", "ignoreFindings": True, "description": "now decides"})
    assert s.ctl.last("update_policy") == {"policyEngineId": MINE, "policyId": "gen_rule-abcdefghij", "validationMode": "IGNORE_ALL_FINDINGS",
                                           "enforcementMode": "ACTIVE", "description": {"optionalValue": "now decides"}}
    with pytest.raises(gov.GovernanceError):
        gov.update_policy(s, REGION, MINE, "gen_rule-abcdefghij", {"ignoreFindings": True})  # nothing to change
    s.ctl.settles = "UPDATE_FAILED"
    out = gov.update_policy(s, REGION, MINE, "gen_rule-abcdefghij", {"statement": CEDAR.replace("amount", "nope")})
    assert out["status"] == "UPDATE_FAILED" and "previous definition" in out["kept"] and out["statement"] == CEDAR  # the old one stays in force


def test_validation_uses_a_log_only_policy_that_is_always_deleted():
    s = Session()
    out = gov.validate_policy(s, REGION, MINE, {"statement": CEDAR})
    created = s.ctl.last("create_policy")
    assert out == {"valid": True, "stage": "schema", "status": "ACTIVE", "reasons": []}
    assert created["enforcementMode"] == "LOG_ONLY" and created["name"].startswith("adlc_check_") and created["validationMode"] == "FAIL_ON_ANY_FINDINGS"
    assert s.ctl.ops()[-1] == "delete_policy" and s.ctl.policies[MINE] == {}
    s.ctl.settles, s.ctl.reasons = "CREATE_FAILED", ["Overly Permissive: Policy Engine will allow every request ..."]
    out = gov.validate_policy(s, REGION, MINE, {"statement": PERMIT})
    assert out["valid"] is False and out["reasons"] == s.ctl.reasons and s.ctl.policies[MINE] == {}
    out = gov.validate_policy(s, REGION, MINE, {"statement": CEDAR.replace("principal,", "principal,, PARSE")})
    assert out == {"valid": False, "stage": "parse", "reasons": ["When parsing the policy statement, the following errors occurred:\n* unexpected token `,`"]}


def test_generation_reads_the_gateway_and_returns_each_assets_cedar():
    s = Session()
    out = gov.start_generation(s, REGION, MINE, {"gatewayId": SHOP, "text": "No refund above 100 dollars. Anyone may look up a balance.", "name": "refunds"})
    assert s.ctl.last("start_policy_generation") == {"policyEngineId": MINE, "resource": {"arn": f"{ARN}:gateway/{SHOP}"},
                                                     "content": {"rawText": "No refund above 100 dollars. Anyone may look up a balance."}, "name": "refunds"}
    assert out["status"] == "GENERATING" and out["gatewayId"] == SHOP
    got = gov.generation(s, REGION, MINE, out["id"])
    assert [(a["statement"], a["usable"]) for a in got["assets"]] == [(CEDAR, True), (PERMIT, True), ("", False)]  # the Cedar is under definition.policy
    assert got["assets"][1]["findings"] == [{"type": "ALLOW_ALL", "description": "Overly Permissive"}] and got["gatewayArn"].endswith(SHOP)
    for bad in ({"gatewayId": SHOP, "text": ""}, {"gatewayId": SHOP, "text": "x" * 2001}, {"gatewayId": "Not A Gateway", "text": "x"}):
        with pytest.raises(gov.GovernanceError):
            gov.start_generation(s, REGION, MINE, bad)
    gov.create_policy(s, REGION, MINE, {"name": "refund_cap", "generationId": out["id"], "assetId": "g-aaaaaaaaaa"})
    assert s.ctl.last("create_policy")["definition"] == {"policyGeneration": {"policyGenerationId": out["id"], "policyGenerationAssetId": "g-aaaaaaaaaa"}}


def test_an_engine_a_gateway_uses_or_that_holds_policies_is_not_deleted_silently():
    s = Session()
    s.ctl.policies[MINE]["refund_cap-abcdefghij"] = Control._policy(MINE, "refund_cap-abcdefghij", CEDAR, "ACTIVE")
    s.ctl.gateways[TEAM]["policyEngineConfiguration"] = {"arn": f"{ARN}:policy-engine/{MINE}", "mode": "ENFORCE"}
    with pytest.raises(gov.GovernanceError) as refused:
        gov.delete_engine(s, REGION, MINE, {"deletePolicies": True})
    assert refused.value.status == 409 and refused.value.extra["gateways"] == ["team-gw"]
    del s.ctl.gateways[TEAM]["policyEngineConfiguration"]
    with pytest.raises(gov.GovernanceError) as refused:
        gov.delete_engine(s, REGION, MINE, {})
    assert refused.value.extra["policies"] == 1 and not {"delete_policy", "delete_policy_engine"} & set(s.ctl.ops())
    out = gov.delete_engine(s, REGION, MINE, {"deletePolicies": True})
    assert s.ctl.ops()[-2:] == ["delete_policy", "delete_policy_engine"] and out["deletedPolicies"] == 1


# -- Gateways ---------------------------------------------------------------------------------------------------------

def test_attaching_echoes_the_whole_gateway_and_changes_only_its_engine():
    s = Session()
    gateway = s.ctl.get_gateway(SHOP)
    request = gov.gateway_update(gateway, {"arn": f"{ARN}:policy-engine/{MINE}", "mode": "LOG_ONLY"})
    assert request == {"gatewayIdentifier": SHOP, "name": "shop-gw", "description": "tools", "roleArn": f"arn:aws:iam::{ACCOUNT}:role/{SHOP}-role",
                       "protocolType": "MCP", "protocolConfiguration": {"mcp": {"searchType": "SEMANTIC"}}, "authorizerType": "AWS_IAM",
                       "exceptionLevel": "DEBUG", "policyEngineConfiguration": {"arn": f"{ARN}:policy-engine/{MINE}", "mode": "LOG_ONLY"}}
    assert "policyEngineConfiguration" not in gov.gateway_update({**gateway, "policyEngineConfiguration": {"arn": "x", "mode": "ENFORCE"}}, None)  # detach
    import botocore.session
    accepted = set(botocore.session.get_session().get_service_model("bedrock-agentcore-control").operation_model("UpdateGateway").input_shape.members)
    assert set(request) <= accepted and {"webAclArn", "workloadIdentityDetails", "status", "gatewayUrl"}.isdisjoint(request)
    out = gov.attach(s, REGION, SHOP, {"engineId": MINE, "mode": "ENFORCE"})
    assert s.ctl.last("update_gateway")["policyEngineConfiguration"] == {"arn": f"{ARN}:policy-engine/{MINE}", "mode": "ENFORCE"}
    assert out["engine"] == {"arn": f"{ARN}:policy-engine/{MINE}", "mode": "ENFORCE"} and "every tool call is denied" in out["warning"]  # no permit yet
    assert gov.attach(s, REGION, SHOP, {"engineId": MINE, "mode": "ENFORCE"})["unchanged"] is True and s.ctl.ops().count("update_gateway") == 1
    for bad in ({"engineId": MINE, "mode": "ON"}, {"engineId": "nope", "mode": "LOG_ONLY"}):
        with pytest.raises(gov.GovernanceError):
            gov.attach(s, REGION, SHOP, bad)
    out = gov.detach(s, REGION, SHOP, {})
    assert "policyEngineConfiguration" not in s.ctl.last("update_gateway") and out["detached"].endswith(MINE)


def test_a_gateway_the_console_did_not_create_is_changed_only_when_acknowledged():
    s = Session()
    s.ctl.rules[TEAM] = [{"ruleId": "22222222-2222-3333-4444-555555555555", "priority": 1, "actions": [], "status": "ACTIVE"}]
    rule = {"priority": 10, "actions": [{"routeToTarget": {"staticRoute": {"targetName": "web"}}}]}
    limit = {"dimensionKeys": ["toolName"], "entries": [{"dimensions": {"toolName": "*"}, "requests": [{"rate": 100, "period": "minute"}]}]}
    changes = [lambda b: gov.attach(s, REGION, TEAM, {"engineId": MINE, "mode": "LOG_ONLY", **b}), lambda b: gov.detach(s, REGION, TEAM, b),
               lambda b: gov.create_rule(s, REGION, TEAM, {**rule, **b}), lambda b: gov.delete_rule(s, REGION, TEAM, "22222222-2222-3333-4444-555555555555", b),
               lambda b: gov.create_rate_limit(s, REGION, TEAM, {**limit, **b}), lambda b: gov.delete_rate_limit(s, REGION, TEAM, "rl-1", b),
               lambda b: gov.put_resource_policy(s, REGION, TEAM, {"policy": {"Statement": [{"Effect": "Allow"}]}, **b}),
               lambda b: gov.delete_resource_policy(s, REGION, TEAM, b), lambda b: gov.enable_decision_log(s, REGION, TEAM, b),
               lambda b: gov.disable_decision_log(s, REGION, TEAM, b)]
    for change in changes:
        with pytest.raises(gov.GovernanceError) as refused:
            change({})
        assert refused.value.status == 409 and refused.value.extra["needsAcknowledgement"] is True
        with pytest.raises(gov.GovernanceError):
            change({"acknowledged": "yes"})  # only a real true
    assert not MUTATIONS & set(s.ctl.ops()) and s.logs.calls == []
    gov.attach(s, REGION, TEAM, {"engineId": MINE, "mode": "LOG_ONLY", "acknowledged": True})
    assert s.ctl.last("update_gateway")["gatewayIdentifier"] == TEAM


def test_the_gateway_role_gets_the_policy_permissions_only_when_the_console_made_it():
    refusal = aws_error("AccessDeniedException", f"Policy Engine '{MINE}' does not have the required permissions. User: arn:aws:sts::{ACCOUNT}:assumed-role/r/"
                        "GenesisPolicyEngineCheck is not authorized to perform: bedrock-agentcore:AuthorizeAction on resource", 403, "UpdateGateway")
    s = Session(iam=Iam(console_roles=[f"{SHOP}-role"]))
    s.ctl.update_errors = [refusal]
    with pytest.raises(gov.GovernanceError) as refused:
        gov.attach(s, REGION, SHOP, {"engineId": MINE})
    statement = gov.role_statement(f"{ARN}:policy-engine/{MINE}", f"{ARN}:gateway/{SHOP}")
    assert refused.value.status == 409 and refused.value.extra["roleStatement"] == statement and refused.value.extra["canGrant"] is True
    assert statement["Statement"][1]["Action"] == ["bedrock-agentcore:AuthorizeAction", "bedrock-agentcore:PartiallyAuthorizeActions"]
    s.ctl.update_errors = [refusal, refusal]  # still refused once while IAM propagates
    out = gov.attach(s, REGION, SHOP, {"engineId": MINE, "grantRole": True})
    assert s.iam.calls == [("put_role_policy", {"RoleName": f"{SHOP}-role", "PolicyName": "adlc-console-policy-engine", "PolicyDocument": json.dumps(statement)})]
    assert out["roleGranted"] is True and s.ctl.ops().count("update_gateway") == 4
    other = Session()
    other.ctl.update_errors = [aws_error("ValidationException", "Access denied while calling GetPolicyEngine on Policy Engine: x with Gateway role: y")]
    with pytest.raises(gov.GovernanceError) as refused:
        gov.attach(other, REGION, SHOP, {"engineId": MINE, "grantRole": True})
    assert refused.value.extra["canGrant"] is False and other.iam.calls == []  # someone else's role is never edited
    other.ctl.update_errors = [aws_error("AccessDeniedException", "User: arn:aws:iam::1:user/sa is not authorized to perform: bedrock-agentcore:UpdateGateway")]
    with pytest.raises(ClientError):
        gov.attach(other, REGION, SHOP, {"engineId": MINE, "grantRole": True})  # the caller's own denial is not the role's


def test_in_a_spoke_workspace_the_gateway_role_is_bounded_before_it_is_passed_and_a_grant_it_may_not_make_is_a_409():
    """review: the policy-engine grant called PutRolePolicy directly: a console Gateway role made before the workspace had
    its boundary was denied by the spoke role and the attach answered 500. Now the console's own role (on its path) gets
    the boundary before UpdateGateway passes it and before the grant; another team's role is never bounded, and a grant
    the spoke may not make is a 409 with the statement for its owner."""
    boundary = f"arn:aws:iam::{ACCOUNT}:policy/adlc-console-spoke-boundary"

    class SpokeIam:
        def __init__(self, roles, updates):
            self.roles, self.updates, self.calls = roles, updates, []

        def get_role(self, RoleName):
            r = self.roles[RoleName]
            out = {"RoleName": RoleName, "Path": r["path"], "Arn": f"arn:aws:iam::{ACCOUNT}:role{r['path']}{RoleName}"}
            if r.get("boundary"):
                out["PermissionsBoundary"] = {"PermissionsBoundaryType": "Policy", "PermissionsBoundaryArn": r["boundary"]}
            return {"Role": out}

        def list_role_tags(self, RoleName):
            return {"Tags": [{"Key": "adlc:console", "Value": "1"}]}

        def put_role_permissions_boundary(self, RoleName, PermissionsBoundary):
            self.calls.append(("put_role_permissions_boundary", RoleName, self.updates()))  # how many UpdateGateway calls came before
            self.roles[RoleName]["boundary"] = PermissionsBoundary

        def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
            self.calls.append(("put_role_policy", RoleName, self.updates()))
            if self.roles[RoleName].get("boundary") != boundary:  # the spoke role changes only bounded roles
                raise aws_error("AccessDenied", f"User: arn:aws:sts::{ACCOUNT}:assumed-role/adlc-console-spoke/adlc-console is not authorized to perform: "
                                "iam:PutRolePolicy", 403, "PutRolePolicy")

    refusal = aws_error("AccessDeniedException", f"Policy Engine '{MINE}' does not have the required permissions. User: arn:aws:sts::{ACCOUNT}:assumed-role/r/"
                        "GenesisPolicyEngineCheck is not authorized to perform: bedrock-agentcore:AuthorizeAction on resource", 403, "UpdateGateway")
    s = Session()
    s.iam = SpokeIam({f"{SHOP}-role": {"path": "/adlc-console/"}, f"{TEAM}-role": {"path": "/"}}, lambda: s.ctl.ops().count("update_gateway"))
    s.ctl.gateways[SHOP]["roleArn"] = f"arn:aws:iam::{ACCOUNT}:role/adlc-console/{SHOP}-role"
    s.ctl.update_errors = [refusal]
    out = gov.attach(s, REGION, SHOP, {"engineId": MINE, "grantRole": True}, boundary=boundary)
    assert out["roleGranted"] is True and s.iam.calls == [("put_role_permissions_boundary", f"{SHOP}-role", 0), ("put_role_policy", f"{SHOP}-role", 1)]
    s.ctl.update_errors = [refusal]
    with pytest.raises(gov.GovernanceError) as refused:  # another team's tagged role, on /: not bounded, and the grant is refused cleanly
        gov.attach(s, REGION, TEAM, {"engineId": MINE, "grantRole": True, "acknowledged": True}, boundary=boundary)
    assert refused.value.status == 409 and refused.value.extra["roleStatement"]["Statement"][0]["Sid"] == "ReadPolicyEngine"
    assert "tag it adlc:console=1" in str(refused.value) and s.iam.roles[f"{TEAM}-role"].get("boundary") is None
    assert [c[0] for c in s.iam.calls if c[1] == f"{TEAM}-role"] == ["put_role_policy"]


def test_rules_are_checked_and_a_system_managed_rule_is_kept():
    principal = {"matchPrincipals": {"anyOf": [{"iamPrincipal": {"arn": f"arn:aws:iam::{ACCOUNT}:role/Support", "operator": "StringEquals"}}]}}
    bundle = {"configurationBundle": {"staticOverride": {"bundleArn": f"{ARN}:configuration-bundle/b-abcdefghij", "bundleVersion": "15254c40-1681-435d-95f2-ef97f69bd625"}}}
    assert gov.rule_request({"priority": "10", "conditions": [principal, {"matchPaths": {"anyOf": ["/web/*"]}}], "actions": [bundle], "description": " A/B "}) == {
        "priority": 10, "conditions": [principal, {"matchPaths": {"anyOf": ["/web/*"]}}], "actions": [bundle], "description": "A/B"}
    split = {"routeToTarget": {"weightedRoute": {"trafficSplit": [{"name": "a", "weight": 90, "targetName": "web"}, {"name": "b", "weight": 10, "targetName": "web2"}]}}}
    assert gov.rule_request({"priority": 1, "actions": [split]}) == {"priority": 1, "actions": [split]}
    for bad in ({"priority": 0, "actions": [bundle]}, {"priority": 1, "actions": []}, {"priority": 1, "conditions": [principal] * 3, "actions": [bundle]},
                {"priority": 1, "conditions": [{"matchHeaders": {}}], "actions": [bundle]}, {"priority": 1, "conditions": [{"matchPaths": {"anyOf": ["web"]}}], "actions": [bundle]},
                {"priority": 1, "conditions": [{"matchPrincipals": {"anyOf": [{"iamPrincipal": {"arn": "alice"}}]}}], "actions": [bundle]},
                {"priority": 1, "actions": [{"routeToTarget": {"weightedRoute": {"trafficSplit": [{"name": "a", "weight": 100, "targetName": "web"}]}}}]},
                {"priority": "high", "actions": [bundle]}):
        with pytest.raises(gov.GovernanceError):
            gov.rule_request(bad)
    s = Session()
    out = gov.create_rule(s, REGION, SHOP, {"priority": 10, "conditions": [principal], "actions": [bundle]})
    assert s.ctl.last("create_gateway_rule") == {"gatewayIdentifier": SHOP, "priority": 10, "conditions": [principal], "actions": [bundle]}
    assert out["status"] == "CREATING" and out["createdAt"] == "2026-10-01T13:50:00Z"
    assert gov.rules(s, REGION, SHOP)[0]["managedBy"] == "experiments"
    with pytest.raises(gov.GovernanceError) as refused:
        gov.delete_rule(s, REGION, SHOP, "11111111-2222-3333-4444-555555555555", {})
    assert refused.value.status == 409 and "delete_gateway_rule" not in s.ctl.ops()


def test_rate_limits_follow_the_services_rules():
    req = gov.rate_limit_request({"rateLimitId": "refunds-per-minute", "description": "2 refunds a minute", "dimensionKeys": ["toolName"],
                                  "entries": [{"dimensions": {"toolName": "tools___issue_refund"}, "requests": {"rate": 2, "period": "minute"}}]})
    assert req == {"dimensionKeys": ["toolName"], "entries": [{"dimensions": {"toolName": "tools___issue_refund"}, "requests": [{"rate": 2.0, "period": "minute"}]}],
                   "rateLimitId": "refunds-per-minute", "description": "2 refunds a minute"}
    two = gov.rate_limit_request({"dimensionKeys": ["$.context.iam.principal", "toolName"], "entries": [
        {"dimensions": {"$.context.iam.principal": "arn:aws:iam::1:role/a", "toolName": "*"}, "tokens": [{"rate": 1000, "period": "minute"}],
         "connections": {"rate": 3, "period": "second"}}]})
    assert two["entries"][0]["tokens"] == [{"rate": 1000.0, "period": "minute"}] and two["entries"][0]["connections"] == [{"rate": 3.0, "period": "second"}]
    for bad in ({"dimensionKeys": ["toolName"], "entries": [{"dimensions": {"toolName": "*"}, "tokens": {"rate": 1, "period": "second"}}]},  # tokens: minute only
                {"dimensionKeys": ["toolName"], "entries": [{"dimensions": {"toolName": "*"}, "connections": {"rate": 1, "period": "minute"}}]},  # connections: second only
                {"dimensionKeys": ["targetName", "toolName"], "entries": [{"dimensions": {"targetName": "*", "toolName": "t___x"}, "requests": {"rate": 1, "period": "second"}}]},
                {"dimensionKeys": ["toolName"], "entries": [{"dimensions": {"targetName": "web"}, "requests": {"rate": 1, "period": "second"}}]},
                {"dimensionKeys": ["user"], "entries": [{"dimensions": {"user": "a"}, "requests": {"rate": 1, "period": "second"}}]},
                {"dimensionKeys": ["toolName"], "entries": [{"dimensions": {"toolName": "*"}}]},
                {"dimensionKeys": ["toolName"], "entries": [{"dimensions": {"toolName": "*"}, "requests": {"rate": -1, "period": "second"}}]},
                {"dimensionKeys": ["toolName", "toolName"], "entries": []}, {"dimensionKeys": ["toolName"], "entries": []}):
        with pytest.raises(gov.GovernanceError):
            gov.rate_limit_request(bad)
    s = Session()
    gov.create_rate_limit(s, REGION, SHOP, {"dimensionKeys": ["toolName"], "entries": [{"dimensions": {"toolName": "*"}, "requests": {"rate": 50, "period": "second"}}]})
    assert s.ctl.last("create_gateway_rate_limit") == {"gatewayIdentifier": SHOP, "dimensionKeys": ["toolName"],
                                                       "entries": [{"dimensions": {"toolName": "*"}, "requests": [{"rate": 50.0, "period": "second"}]}]}
    assert gov.delete_rate_limit(s, REGION, SHOP, "rl-1", {}) == {"id": "rl-1", "status": "DELETING"}


def test_the_resource_policy_needs_a_document():
    s = Session()
    doc = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:root"}, "Action": "bedrock-agentcore:InvokeGateway",
                                                   "Resource": f"{ARN}:gateway/{SHOP}"}]}
    assert gov.resource_policy(s, REGION, SHOP)["policy"] is None  # live: none is {}
    assert gov.put_resource_policy(s, REGION, SHOP, {"policy": json.dumps(doc)})["policy"] == doc
    assert s.ctl.last("put_resource_policy") == {"resourceArn": f"{ARN}:gateway/{SHOP}", "policy": json.dumps(doc)}
    for bad in ({"policy": "{not json"}, {"policy": {"Version": "2012-10-17"}}, {}):
        with pytest.raises(gov.GovernanceError):
            gov.put_resource_policy(s, REGION, SHOP, bad)
    assert gov.delete_resource_policy(s, REGION, SHOP, {})["deleted"] is True and gov.delete_resource_policy(s, REGION, SHOP, {})["deleted"] is False


# -- the decision log -----------------------------------------------------------------------------------------------

def record(rid, ms, log, **body):
    return {"resource_arn": f"{ARN}:gateway/{SHOP}", "event_timestamp": ms, "request_id": rid, "trace_id": f"t-{rid}", "span_id": "s",
            "body": {"isError": False, "log": log, "id": "1", **body}}


def policy(decision, policies, reason=None):
    return {"decision": decision, "policyEngineArn": f"{ARN}:policy-engine/{MINE}", "determiningPolicies": policies, "latencyMs": 50,
            "principal": {"entityType": "AgentCore::IamEntity", "entityId": f"arn:aws:iam::{ACCOUNT}:user/sa"}, **({"reason": reason} if reason else {})}


#: The Gateway's application log as delivered live: newest first, as the query sorts it.
RECORDS = [
    record("r3", 1790862619831, "Tool Execution Denied: Tool call not allowed due to policy enforcement [Policy evaluation denied due to refund_cap-abcdefghij]", isError=True),
    record("r3", 1790862619831, "Policy evaluation denied request", isError=True,
           policy=policy("DENY", ["refund_cap-abcdefghij"], "Policy evaluation denied due to refund_cap-abcdefghij")),
    record("r3", 1790862619726, "Started processing request",
           requestBody="{id=13, jsonrpc=2.0, method=tools/call, params={name=tools___issue_refund, arguments={amount=500, account_id=A-1}}}"),
    record("r2", 1790862605945, "Policy evaluation denied request", isError=True,
           policy=policy("DENY", ["refund_cap-abcdefghij"], "Policy evaluation denied due to refund_cap-abcdefghij")),
    record("r2", 1790862605822, "Started processing request",
           requestBody="{id=13, jsonrpc=2.0, method=tools/call, params={name=tools___issue_refund, arguments={amount=500, account_id=A-1}}}"),
    record("r1", 1790862601954, "Policy evaluation completed", policy=policy("ALLOW", ["balance_ok-abcdefghij", "allow_all-abcdefghij"])),
    record("r1", 1790862601818, "Started processing request",
           requestBody="{id=10, jsonrpc=2.0, method=tools/call, params={name=tools___get_balance, arguments={account_id=A-1}}}"),
]

SPANS = [
    {"name": "AgentCore.Gateway.InvokeTool", "traceId": "t1", "spanId": "call1", "startTimeUnixNano": 1790862619772000000,
     "attributes": {"tool.name": "tools___issue_refund", "aws.agentcore.policy.authorization_decision": "DENY", "aws.agentcore.gateway.policy.mode": "ENFORCE"}},
    {"name": "AgentCore.Policy.AuthorizeAction", "traceId": "t1", "spanId": "auth1", "parentSpanId": "call1", "startTimeUnixNano": 1790862619780000000,
     "attributes": {"aws.agentcore.policy.authorization_decision": "DENY", "aws.agentcore.policy.determining_policies": ["refund_cap-abcdefghij"],
                    "aws.agentcore.policy.authorization_reason": "Policy evaluation denied due to refund_cap-abcdefghij", "aws.agentcore.gateway.policy.mode": "ENFORCE",
                    "aws.agentcore.policy.log_only_matched_policies": ["allow_all-abcdefghij"], "aws.request.id": "req-1"}},
    {"name": "AgentCore.Policy.AuthorizeAction", "traceId": "t2", "spanId": "auth2", "parentSpanId": "call2", "startTimeUnixNano": 1790862695235000000,
     "attributes": {"aws.agentcore.policy.authorization_decision": "DENY", "aws.agentcore.policy.determining_policies": [],
                    "aws.agentcore.policy.authorization_reason": "No policy applies to the request (denied by default).", "aws.agentcore.gateway.policy.mode": "ENFORCE",
                    "aws.agentcore.policy.log_only_decision_flipping_policies": ["allow_all-abcdefghij"]}},
    {"name": "AgentCore.Gateway.InvokeTool", "traceId": "t2", "spanId": "call2", "startTimeUnixNano": 1790862695200000000, "attributes": {"tool.name": "tools___get_balance"}},
]


def test_each_decision_is_joined_to_its_tool_and_caller():
    rows = gov.log_rows(RECORDS)
    assert [(r["tool"], r["decision"], r["blocked"]) for r in rows] == [("tools___issue_refund", "DENY", True),  # ENFORCE: refused
                                                                         ("tools___issue_refund", "DENY", False),  # LOG_ONLY: the call ran
                                                                         ("tools___get_balance", "ALLOW", False)]
    assert rows[0]["policies"] == ["refund_cap-abcdefghij"] and rows[0]["reason"].endswith("refund_cap-abcdefghij")
    assert rows[2]["principal"] == f"arn:aws:iam::{ACCOUNT}:user/sa" and rows[2]["arguments"] == "{account_id=A-1}" and rows[2]["at"] == "2026-10-01T13:50:01Z"
    spans = gov.span_rows(SPANS)
    assert [(r["tool"], r["decision"], r["policies"], r["blocked"]) for r in spans] == [("tools___get_balance", "DENY", [], True),
                                                                                         ("tools___issue_refund", "DENY", ["refund_cap-abcdefghij"], True)]
    assert spans[0]["reason"].endswith("(denied by default).") and spans[0]["flips"] == ["allow_all-abcdefghij"] and spans[1]["logOnly"] == ["allow_all-abcdefghij"]
    assert gov.tool_call("{jsonrpc=2.0, method=tools/call, params={arguments={q={deep=1}}, name=t___x}, id=3}") == ("t___x", "{q={deep=1}}")
    assert gov.tool_call("{jsonrpc=2.0, method=tools/list, id=1}") == (None, None)


def test_decision_counts_sum_exact_dimension_sets_only():
    base = {"OperationName": "AuthorizeAction", "TargetResource": SHOP}
    cw = CloudWatch([("AllowDecisions", base, 20), ("AllowDecisions", {**base, "PolicyEngine": MINE}, 20),  # the same decisions again
                     ("AllowDecisions", {**base, "Policy": "allow_all-abcdefghij"}, 12), ("AllowDecisions", {**base, "Policy": "allow_all-abcdefghij", "PolicyEngine": MINE}, 12),
                     ("AllowDecisions", {**base, "Mode": "ENFORCE"}, 17), ("AllowDecisions", {**base, "Mode": "LOG_ONLY"}, 3),
                     ("AllowDecisions", {"OperationName": "PartiallyAuthorizeActions", "TargetResource": SHOP, "ToolName": "tools___get_balance"}, 40),
                     ("DenyDecisions", base, 5), ("DenyDecisions", {**base, "Policy": "refund_cap-abcdefghij"}, 4), ("DenyDecisions", {**base, "Mode": "ENFORCE"}, 5),
                     ("NoDeterminingPolicies", base, 1), ("LogOnlyMatches", {**base, "Policy": "allow_all-abcdefghij"}, 13),
                     ("LogOnlyDecisionFlips", {**base, "Policy": "allow_all-abcdefghij"}, 1), ("AllowDecisions", {**base, "TargetResource": TEAM}, 99)])
    end = datetime(2026, 10, 2, tzinfo=timezone.utc)
    out = gov.decision_metrics(cw, SHOP, datetime(2026, 10, 1, tzinfo=timezone.utc), end)
    assert (out["allow"], out["deny"], out["defaultDeny"]) == (20, 5, 1)
    assert out["byPolicy"] == [{"policy": "allow_all-abcdefghij", "allow": 12, "deny": 0, "defaultDeny": 0, "logOnlyMatches": 13, "flips": 1},
                               {"policy": "refund_cap-abcdefghij", "allow": 0, "deny": 4, "defaultDeny": 0, "logOnlyMatches": 0, "flips": 0}]
    assert out["byMode"] == {"ENFORCE": {"allow": 17, "deny": 5}, "LOG_ONLY": {"allow": 3, "deny": 0}}
    assert {q["MetricStat"]["Stat"] for q in cw.queries} == {"Sum"} and all(q["MetricStat"]["Period"] % 60 == 0 for q in cw.queries)


def test_decisions_come_from_the_delivered_log_group_else_the_spans_and_always_the_metrics():
    group = f"/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/{SHOP}"
    s = Session(logs=Logs(deliveries=[("APPLICATION_LOGS", "CWL", group, True), ("TRACES", "XRAY", None, True)], records=RECORDS, spans=SPANS),
                cw=CloudWatch([("AllowDecisions", {"OperationName": "AuthorizeAction", "TargetResource": SHOP}, 2)]))
    found = gov.channels(s.logs, f"{ARN}:gateway/{SHOP}")
    assert found["logGroup"] == group and found["traces"] is True and [d["logType"] for d in found["deliveries"]] == ["APPLICATION_LOGS", "TRACES"]
    out = gov.decisions(s, REGION, SHOP, hours="6", limit=2)
    assert out["source"] == "logs" and s.logs.last("start_query")["logGroupName"] == group and out["total"] == 3 and len(out["rows"]) == 2
    assert "Policy evaluation" in s.logs.last("start_query")["queryString"] and out["summary"]["allow"] == 2 and out["hours"] == 6.0
    json.dumps(out)  # the payload is plain JSON
    spans_only = Session(logs=Logs(deliveries=[("TRACES", "XRAY", None, True)], spans=SPANS))
    out = gov.decisions(spans_only, REGION, SHOP, hours=1000)
    assert out["source"] == "spans" and spans_only.logs.last("start_query")["logGroupName"] == "aws/spans" and out["hours"] == 24 * 14
    assert f'`attributes.aws.resource.arn` = "{ARN}:gateway/{SHOP}"' in spans_only.logs.last("start_query")["queryString"]
    nothing = Session()
    out = gov.decisions(nothing, REGION, SHOP)
    assert (out["source"], out["rows"], out["logGroup"]) == (None, [], None) and nothing.logs.calls == []  # counts only


def test_the_decision_log_is_turned_on_with_tagged_deliveries_and_off_for_the_consoles_only():
    s = Session()
    out = gov.enable_decision_log(s, REGION, SHOP, {})
    group = f"/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/{SHOP}"
    tag = {"adlc:console": "1"}
    assert s.logs.calls == [
        ("create_log_group", {"logGroupName": group, "tags": tag}), ("put_retention_policy", {"logGroupName": group, "retentionInDays": 30}),
        ("put_delivery_source", {"name": f"{SHOP}-logs-source", "logType": "APPLICATION_LOGS", "resourceArn": f"{ARN}:gateway/{SHOP}", "tags": tag}),
        ("put_delivery_destination", {"name": f"{SHOP}-logs-destination", "deliveryDestinationType": "CWL", "tags": tag,
                                      "deliveryDestinationConfiguration": {"destinationResourceArn": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{group}"}}),
        ("create_delivery", {"deliverySourceName": f"{SHOP}-logs-source", "deliveryDestinationArn": f"arn:aws:logs:{REGION}:{ACCOUNT}:delivery-destination:{SHOP}-logs-destination",
                             "tags": tag})]
    assert out == {"gatewayId": SHOP, "logGroup": group, "created": True, "deliveryId": "new-delivery"}
    on = Session(logs=Logs(deliveries=[("APPLICATION_LOGS", "CWL", group, True)]))
    assert gov.enable_decision_log(on, REGION, SHOP, {})["created"] is False and on.logs.calls == []
    out = gov.disable_decision_log(on, REGION, SHOP, {})
    assert [op for op, _ in on.logs.calls] == ["delete_delivery", "delete_delivery_source", "delete_delivery_destination"] and out["logGroup"] == group
    theirs = Session(logs=Logs(deliveries=[("APPLICATION_LOGS", "CWL", group, False)]))
    assert gov.disable_decision_log(theirs, REGION, SHOP, {})["kept"] == ["d-APPLICATION_LOGS"] and theirs.logs.calls == []


# -- routes -----------------------------------------------------------------------------------------------------------

def test_routes_read_for_members_and_write_for_admins_and_answer_refusals():
    added = []
    gov.register(type("Router", (), {"add": lambda self, method, pattern, fn, admin=False: added.append((method, pattern, admin))})())
    assert added and all(p.startswith("/workspaces/{wid}/governance") for _, p, _ in added)
    assert all(admin == (method != "GET") for method, _, admin in added)  # every write is admin-only
    assert len({(m, p) for m, p, _ in added}) == len(added)
    answer = gov.guarded(lambda r: (_ for _ in ()).throw(gov.GovernanceError("ack first", 409, needsAcknowledgement=True)))
    assert answer(None) == (409, {"error": "ack first", "needsAcknowledgement": True})
    assert gov.guarded(lambda r: (_ for _ in ()).throw(aws_error("ConflictException", "busy", 409)))(None) == (409, {"error": "ConflictException: busy"})
    assert gov.guarded(lambda r: (_ for _ in ()).throw(aws_error("InternalServerException", "oops", 500)))(None)[0] == 502
    assert gov.guarded(lambda r: (_ for _ in ()).throw(ParamValidationError(report="bad length")))(None)[0] == 400
    with pytest.raises(KeyError):
        gov.guarded(lambda r: {}["x"])(None)  # a bug stays a 500


def test_the_console_serves_governance_for_its_workspace(tmp_path):
    spec = importlib.util.spec_from_file_location("adlc_console_server_gov", REPO / "app" / "console" / "server.py")
    server_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server_mod)  # type: ignore[union-attr]
    fake = Session()
    console = server_mod.Console(tmp_path / "data", session_factory=lambda **kw: fake, clients_factory=lambda cfg: None)
    server = server_mod.create_server(console)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base, cookie = f"http://127.0.0.1:{server.server_address[1]}/api/console", {}

    def call(method, path, body=None):
        headers = {"Content-Type": "application/json", **({"Cookie": cookie["v"]} if cookie else {})}
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.headers.get("Set-Cookie"):
                    cookie["v"] = resp.headers["Set-Cookie"].split(";")[0]
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    try:
        assert call("POST", "/workspaces", {"id": "dev", "accountId": ACCOUNT, "region": REGION, "profile": "default"})[0] == 201
        status, body = call("GET", "/workspaces/dev/governance")
        assert status == 200 and [e["console"] for e in body["engines"]] == [True, False] and body["engines"][0]["createdAt"] == "2026-10-01T13:50:00Z"
        assert {g["id"]: g["console"] for g in body["gateways"]} == {SHOP: True, TEAM: False}
        status, body = call("PUT", f"/workspaces/dev/governance/gateways/{TEAM}/engine", {"engineId": MINE, "mode": "LOG_ONLY"})
        assert status == 409 and body["needsAcknowledgement"] is True and "update_gateway" not in fake.ctl.ops()
        assert call("POST", f"/workspaces/dev/governance/engines/{THEIRS}/policies", {"name": "p", "statement": CEDAR})[0] == 403
        status, body = call("POST", f"/workspaces/dev/governance/engines/{MINE}/policies", {"name": "refund_cap", "statement": CEDAR})
        assert status == 201 and body["enforcementMode"] == "LOG_ONLY"
        assert call("GET", f"/workspaces/dev/governance/gateways/{SHOP}/decisions?hours=3")[1]["rows"] == []
        status, body = call("GET", "/workspaces/dev/governance/gateways/gone-gw-abcdefghij/resource-policy")
        assert status == 404 and body["error"].startswith("ResourceNotFoundException")  # AWS's answer, not a 500
        assert call("POST", "/users", {"username": "admin", "password": "0123456789", "role": "admin"})[0] == 201
        assert call("POST", "/login", {"username": "admin", "password": "0123456789"})[0] == 200
        assert call("POST", "/users", {"username": "ann", "password": "0123456789", "role": "member", "workspaces": ["dev"]})[0] == 201
        cookie.clear()
        assert call("POST", "/login", {"username": "ann", "password": "0123456789"})[0] == 200
        assert call("GET", "/workspaces/dev/governance")[0] == 200  # a member reads
        assert call("POST", "/workspaces/dev/governance/engines", {"name": "mine_too"})[0] == 403  # but does not write
        assert call("DELETE", f"/workspaces/dev/governance/gateways/{SHOP}/engine", {})[0] == 403
    finally:
        server.shutdown()
        server.server_close()
