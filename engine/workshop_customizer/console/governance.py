"""The console's governance: Cedar policies enforced at an AgentCore Gateway, the policy engines that hold them, policies
written from a sentence, the Gateway's rules, rate limits and resource policy, and the decision log.

Live findings (us-west-2, 2026-10-01) the requests below rely on:

* **Engines** take tags; **policies** do not (TagResource / ListTagsForResource on a policy ARN is refused), so a policy is
  the console's when its engine is. An engine with policies is not deletable (ConflictException), and the service
  does not protect an attached one: the console refuses to delete an engine any Gateway still points at.
* **Policies** are validated against the Gateway's tool schema (one action ``<target>___<tool>`` per tool, the
  arguments under ``context.input``), even before the engine is attached. A parse error is refused at once
  (ValidationException); an unknown action or attribute, or a finding such as "Overly Permissive", ends
  ``CREATE_FAILED`` / ``UPDATE_FAILED`` with ``statusReasons``. ``validationMode`` defaults to FAIL_ON_ANY_FINDINGS on
  create *and* update, so a broad permit needs ``ignoreFindings`` on every change; a failed update keeps the previous
  definition in force. A new policy is LOG_ONLY here until made ACTIVE (the service default is ACTIVE).
* **Generation** reads the Gateway's tools (``resource.arn``), GENERATING -> GENERATED in about 15 s; each asset's Cedar
  is under ``definition.policy`` (not ``cedar``), with findings (``ALLOW_ALL`` …); a sentence the tools cannot express
  still ends GENERATED with an asset that has no definition and an ``INVALID`` finding. A policy created from an
  asset keeps ``definition.policy``.
* **Attaching** is UpdateGateway, which replaces the Gateway: every field it accepts is echoed from GetGateway and only
  ``policyEngineConfiguration`` changes (omitting it detaches). The service checks the Gateway role first
  (GetPolicyEngine, then AuthorizeAction) and refuses the update when either is missing. A denied call answers
  JSON-RPC ``-32002`` "Tool Execution Denied … [Policy evaluation denied due to <policy id>]" or "[No policy applies to
  the request (denied by default).]"; in LOG_ONLY the tool still runs.
* **Rules** route by caller (``matchPrincipals``) or path (``matchPaths``) to an HTTP target, or override tool
  descriptions with a configuration bundle (the Gateway role then needs GetConfigurationBundleVersion, or tools/list
  fails). **Rate limits** answer HTTP 429 / JSON-RPC ``-32003`` with ``{metric, retryAfter, limitKey}``; tokens are per
  minute only, connections per second only, ``*`` only in trailing dimensions. Neither takes tags: they are the
  Gateway's, and the Gateway rule below applies.
* **Decisions** are written three ways: metrics in ``AWS/Bedrock-AgentCore`` always (``AllowDecisions`` /
  ``DenyDecisions`` … under overlapping dimension sets, so exact sets are summed); one record per decision in the
  Gateway's APPLICATION_LOGS once a log delivery exists ("Policy evaluation completed" / "Policy evaluation denied
  request" with the decision, the determining policies, the reason and the caller principal, joined by
  ``request_id`` to the request's tool and to "Tool Execution Denied"); and spans in ``aws/spans`` once trace delivery
  exists (``AgentCore.Policy.AuthorizeAction`` under the ``AgentCore.Gateway.InvokeTool`` span that names the tool).

Safety: everything the console creates is tagged ``adlc:console=1``. Only console engines (and their policies) are
changed here. A Gateway is changed (engine, rules, rate limits, resource policy, decision log) only when the console
created it, or when the caller sends ``acknowledged: true``; the Gateway role gets the policy permissions only when
the console created that role too.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable, Iterable, Mapping

from ..direct.aws import client
from .agents import CONSOLE_TAG, AgentError, ensure_boundary, put_inline_policy
from .common import error_code as _code, guarded as common_guarded, pages as _pages
from .workspaces import boundary_of

NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")  # engine, policy and generation names
RESOURCE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}-[a-z0-9_]{10}$")  # engine, policy, generation and asset ids
GATEWAY_ID = re.compile(r"^[0-9a-z][0-9a-z-]{0,200}-[0-9a-z]{10}$")
RULE_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
RATE_LIMIT_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}[a-zA-Z0-9]$")
EFFECT = re.compile(r"\b(permit|forbid)\s*\(")
STATEMENT = (35, 10_000)
MODES = ("LOG_ONLY", "ENFORCE")  # a Gateway's attachment
ENFORCEMENT = ("LOG_ONLY", "ACTIVE")  # a policy's own
POLICY_ACTIONS = ("bedrock-agentcore:GetPolicyEngine", "bedrock-agentcore:AuthorizeAction", "bedrock-agentcore:PartiallyAuthorizeActions")
ROLE_POLICY = "adlc-console-policy-engine"
NAMESPACE = "AWS/Bedrock-AgentCore"
DECISION_METRICS = ("AllowDecisions", "DenyDecisions", "NoDeterminingPolicies", "LogOnlyMatches", "LogOnlyDecisionFlips")
LOG_GROUP = "/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/{gateway}"
MAX_HOURS = 24 * 14
#: Rate-limit dimension keys (botocore DimensionKey) and the periods each metric takes (live: tokens per minute only,
#: connections per second only).
DIMENSION_KEY = re.compile(r"^(targetName|toolName|qualifiedModelId|\$\.context\.iam\.principal|\$\.context\.iam\.sourceIdentity|"
                           r"\$\.context\.jwt\.[a-zA-Z_][a-zA-Z0-9_\-.]{0,61}[a-zA-Z0-9_])$")
PERIODS = {"requests": ("second", "minute"), "tokens": ("minute",), "connections": ("second",)}
PATH = re.compile(r"^/[\w\-.]+/\*$")
IAM_PRINCIPAL = re.compile(r"^(arn:aws[a-zA-Z-]*:iam::(\d{12}|\*):(user|role)/[\w+=,.@*?/-]+|arn:aws[a-zA-Z-]*:sts::(\d{12}|\*):assumed-role/[\w+=,.@*?/-]+)$")


class GovernanceError(ValueError):
    """A refusal the console answers itself: ``status`` and extra fields (``needsAcknowledgement``, ``roleStatement`` …)."""

    def __init__(self, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.status, self.extra = status, extra


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _ctl(session: Any, region: str) -> Any:
    return client(session, "bedrock-agentcore-control", region)


def _message(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    found = ((response or {}).get("Error") or {}).get("Message") if isinstance(response, dict) else None
    return str(found or exc)


def _check(value: Any, pattern: re.Pattern[str], what: str) -> str:
    text = str(value or "")
    if not pattern.match(text):
        raise GovernanceError(f"{what}: not a valid id ({text[:80]!r})")
    return text


def _console(tags: Mapping[str, str]) -> bool:
    return all(tags.get(k) == v for k, v in CONSOLE_TAG.items())


def _tags(ctl: Any, arn: str) -> dict[str, str]:
    return dict(ctl.list_tags_for_resource(resourceArn=arn).get("tags") or {})


def statement_of(definition: Mapping[str, Any]) -> str:
    """The Cedar of a policy or a generated asset: ``definition.cedar`` when written, ``definition.policy`` when generated."""
    return str((definition.get("cedar") or definition.get("policy") or {}).get("statement") or "")


def check_statement(value: Any) -> str:
    text = str(value or "").strip()
    if not STATEMENT[0] <= len(text) <= STATEMENT[1]:
        raise GovernanceError(f"statement: {STATEMENT[0]}-{STATEMENT[1]} characters of Cedar")
    if not EFFECT.search(text):
        raise GovernanceError("statement: Cedar needs a permit(…) or forbid(…) policy")
    return text


# -- engines -------------------------------------------------------------------------------------------------------

def _engine_view(e: Mapping[str, Any], tags: Mapping[str, str], gateways: list[dict[str, Any]], policies: int | None) -> dict[str, Any]:
    return {"id": e.get("policyEngineId"), "name": e.get("name"), "arn": e.get("policyEngineArn"), "status": e.get("status"),
            "description": e.get("description"), "statusReasons": list(e.get("statusReasons") or []), "console": _console(tags),
            "gateways": gateways, "policies": policies, "createdAt": _iso(e.get("createdAt")), "updatedAt": _iso(e.get("updatedAt"))}


def _gateway_view(g: Mapping[str, Any], tags: Mapping[str, str]) -> dict[str, Any]:
    cfg = g.get("policyEngineConfiguration") or {}
    engine = {"arn": cfg.get("arn"), "id": str(cfg.get("arn") or "").rsplit("/", 1)[-1], "mode": cfg.get("mode")} if cfg.get("arn") else None
    return {"id": g.get("gatewayId"), "name": g.get("name"), "arn": g.get("gatewayArn"), "status": g.get("status"), "authorizerType": g.get("authorizerType"),
            "roleArn": g.get("roleArn"), "console": _console(tags), "engine": engine, "updatedAt": _iso(g.get("updatedAt"))}


def overview(session: Any, region: str) -> dict[str, Any]:
    """Every policy engine (with the Gateways attached to it and its policy count) and every Gateway (with its engine)."""
    ctl = _ctl(session, region)
    gateways = []
    for summary in _pages(ctl.list_gateways, "items"):
        g = ctl.get_gateway(gatewayIdentifier=summary["gatewayId"])
        gateways.append(_gateway_view(g, _tags(ctl, g["gatewayArn"])))
    attached: dict[str, list[dict[str, Any]]] = {}
    for g in gateways:
        if g["engine"]:
            attached.setdefault(str(g["engine"]["arn"]), []).append({"id": g["id"], "name": g["name"], "mode": g["engine"]["mode"]})
    engines = []
    for e in _pages(ctl.list_policy_engines, "policyEngines"):
        count = len(_pages(ctl.list_policies, "policies", policyEngineId=e["policyEngineId"]))
        engines.append(_engine_view(e, _tags(ctl, e["policyEngineArn"]), attached.get(str(e["policyEngineArn"]), []), count))
    return {"engines": engines, "gateways": gateways}


def create_engine(session: Any, region: str, body: Mapping[str, Any]) -> dict[str, Any]:
    name = str(body.get("name") or "")
    if not NAME.match(name):
        raise GovernanceError("name: a letter, then letters, digits or underscores (at most 48)")
    request: dict[str, Any] = {"name": name, "tags": dict(CONSOLE_TAG)}
    description = str(body.get("description") or "").strip()
    if description:
        request["description"] = description[:4096]
    out = _ctl(session, region).create_policy_engine(**request)
    return {"id": out.get("policyEngineId"), "arn": out.get("policyEngineArn"), "name": name, "status": out.get("status")}


def _console_engine(ctl: Any, engine_id: str) -> dict[str, Any]:
    engine = ctl.get_policy_engine(policyEngineId=_check(engine_id, RESOURCE_ID, "policy engine"))
    if not _console(_tags(ctl, engine["policyEngineArn"])):
        raise GovernanceError("only policy engines created from this console (and their policies) can be changed here", 403)
    return engine


def _attached_to(ctl: Any, engine_arn: str) -> list[str]:
    names = []
    for summary in _pages(ctl.list_gateways, "items"):
        g = ctl.get_gateway(gatewayIdentifier=summary["gatewayId"])
        if (g.get("policyEngineConfiguration") or {}).get("arn") == engine_arn:
            names.append(str(g.get("name") or g.get("gatewayId")))
    return names


def delete_engine(session: Any, region: str, engine_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Delete a console engine. Refused while a Gateway points at it; its policies go first only with ``deletePolicies``."""
    ctl = _ctl(session, region)
    engine = _console_engine(ctl, engine_id)
    gateways = _attached_to(ctl, engine["policyEngineArn"])
    if gateways:
        raise GovernanceError(f"detach the engine from {', '.join(gateways)} first: a Gateway keeps pointing at a deleted engine", 409,
                              gateways=gateways)
    policies = _pages(ctl.list_policies, "policies", policyEngineId=engine_id)
    if policies and body.get("deletePolicies") is not True:
        raise GovernanceError(f"the engine still holds {len(policies)} policies: send deletePolicies: true to delete them with it", 409,
                              policies=len(policies))
    for p in policies:
        if p.get("status") != "DELETING":
            ctl.delete_policy(policyEngineId=engine_id, policyId=p["policyId"])
    for _ in range(40):
        if not _pages(ctl.list_policies, "policies", policyEngineId=engine_id):
            break
        _pause(3)
    out = ctl.delete_policy_engine(policyEngineId=engine_id)
    return {"id": engine_id, "status": out.get("status"), "deletedPolicies": len(policies)}


# -- policies ------------------------------------------------------------------------------------------------------

def _policy_view(p: Mapping[str, Any]) -> dict[str, Any]:
    definition = p.get("definition") or {}
    return {"id": p.get("policyId"), "name": p.get("name"), "engineId": p.get("policyEngineId"), "arn": p.get("policyArn"), "status": p.get("status"),
            "enforcementMode": p.get("enforcementMode"), "statement": statement_of(definition), "description": p.get("description"),
            "statusReasons": list(p.get("statusReasons") or []), "createdAt": _iso(p.get("createdAt")), "updatedAt": _iso(p.get("updatedAt"))}


def policies(session: Any, region: str, engine_id: str) -> list[dict[str, Any]]:
    ctl = _ctl(session, region)
    found = _pages(ctl.list_policies, "policies", policyEngineId=_check(engine_id, RESOURCE_ID, "policy engine"))
    return [_policy_view(p) for p in sorted(found, key=lambda p: str(p.get("name") or ""))]


def get_policy(session: Any, region: str, engine_id: str, policy_id: str) -> dict[str, Any]:
    return _policy_view(_ctl(session, region).get_policy(policyEngineId=_check(engine_id, RESOURCE_ID, "policy engine"),
                                                         policyId=_check(policy_id, RESOURCE_ID, "policy")))


def _enforcement(value: Any) -> str:
    mode = str(value or "LOG_ONLY")
    if mode not in ENFORCEMENT:
        raise GovernanceError("enforcementMode: LOG_ONLY (only logged) or ACTIVE (decides)")
    return mode


def _validation(body: Mapping[str, Any]) -> str:
    return "IGNORE_ALL_FINDINGS" if body.get("ignoreFindings") is True else "FAIL_ON_ANY_FINDINGS"


def policy_request(body: Mapping[str, Any]) -> dict[str, Any]:
    """CreatePolicy's fields: a written statement (``definition.cedar``) or a generated asset (``definition.policyGeneration``)."""
    name = str(body.get("name") or "")
    if not NAME.match(name):
        raise GovernanceError("name: a letter, then letters, digits or underscores (at most 48)")
    request: dict[str, Any] = {"name": name, "validationMode": _validation(body), "enforcementMode": _enforcement(body.get("enforcementMode"))}
    if body.get("generationId") or body.get("assetId"):
        request["definition"] = {"policyGeneration": {"policyGenerationId": _check(body.get("generationId"), RESOURCE_ID, "generationId"),
                                                      "policyGenerationAssetId": _check(body.get("assetId"), RESOURCE_ID, "assetId")}}
    else:
        request["definition"] = {"cedar": {"statement": check_statement(body.get("statement"))}}
    description = str(body.get("description") or "").strip()
    if description:
        request["description"] = description[:4096]
    return request


def _settle(ctl: Any, engine_id: str, policy_id: str, *, attempts: int = 25, pause: float = 2.0) -> dict[str, Any]:
    policy: dict[str, Any] = {}
    for _ in range(attempts):
        policy = ctl.get_policy(policyEngineId=engine_id, policyId=policy_id)
        if policy.get("status") not in ("CREATING", "UPDATING"):
            break
        _pause(pause)
    return policy


def create_policy(session: Any, region: str, engine_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Create a policy in a console engine and wait for the service's verdict (ACTIVE, or CREATE_FAILED with reasons)."""
    request = policy_request(body)
    ctl = _ctl(session, region)
    _console_engine(ctl, engine_id)
    out = ctl.create_policy(policyEngineId=engine_id, **request)
    return _policy_view(_settle(ctl, engine_id, out["policyId"]))


def update_request(body: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """UpdatePolicy's changes. The definition keeps its kind (``cedar`` or ``policy``); ``validationMode`` is always sent
    (live: it defaults to FAIL_ON_ANY_FINDINGS on every update, even one that only changes the mode)."""
    changes: dict[str, Any] = {"validationMode": _validation(body)}
    if body.get("statement") is not None:
        kind = "policy" if "policy" in (current.get("definition") or {}) else "cedar"
        changes["definition"] = {kind: {"statement": check_statement(body.get("statement"))}}
    if body.get("enforcementMode"):
        changes["enforcementMode"] = _enforcement(body.get("enforcementMode"))
    description = str(body.get("description") or "").strip()
    if description:
        changes["description"] = {"optionalValue": description[:4096]}
    if len(changes) == 1:
        raise GovernanceError("give a statement, an enforcementMode and/or a description")
    return changes


def update_policy(session: Any, region: str, engine_id: str, policy_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    ctl = _ctl(session, region)
    _console_engine(ctl, engine_id)
    current = ctl.get_policy(policyEngineId=engine_id, policyId=_check(policy_id, RESOURCE_ID, "policy"))
    ctl.update_policy(policyEngineId=engine_id, policyId=policy_id, **update_request(body, current))
    view = _policy_view(_settle(ctl, engine_id, policy_id))
    if view["status"] == "UPDATE_FAILED":
        view["kept"] = "the previous definition stays in force"
    return view


def delete_policy(session: Any, region: str, engine_id: str, policy_id: str) -> dict[str, Any]:
    ctl = _ctl(session, region)
    _console_engine(ctl, engine_id)
    out = ctl.delete_policy(policyEngineId=engine_id, policyId=_check(policy_id, RESOURCE_ID, "policy"))
    return {"id": policy_id, "status": out.get("status")}


def validate_policy(session: Any, region: str, engine_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a statement the way the service does, without keeping it: a parse error comes back at once; anything
    else needs a policy, so a LOG_ONLY one is created, its verdict read, and it is deleted again."""
    statement = check_statement(body.get("statement"))
    ctl = _ctl(session, region)
    _console_engine(ctl, engine_id)
    name = f"adlc_check_{secrets.token_hex(4)}"
    try:
        created = ctl.create_policy(policyEngineId=engine_id, name=name, definition={"cedar": {"statement": statement}},
                                    validationMode=_validation(body), enforcementMode="LOG_ONLY", description="console validation, deleted at once")
    except Exception as exc:  # noqa: BLE001 - a parse error is the answer
        if _code(exc) != "ValidationException":
            raise
        return {"valid": False, "stage": "parse", "reasons": [_message(exc)]}
    policy = _settle(ctl, engine_id, created["policyId"], attempts=30)
    out = {"valid": policy.get("status") == "ACTIVE", "stage": "schema", "status": policy.get("status"), "reasons": list(policy.get("statusReasons") or [])}
    try:
        ctl.delete_policy(policyEngineId=engine_id, policyId=created["policyId"])
        for _ in range(8):  # gone before the list is read again (DELETING lasts a few seconds)
            ctl.get_policy(policyEngineId=engine_id, policyId=created["policyId"])
            _pause(2)
    except Exception as exc:  # noqa: BLE001 - ResourceNotFound is the clean end; anything else names the policy to delete
        if _code(exc) != "ResourceNotFoundException":
            out["leftover"] = {"id": created["policyId"], "name": name, "error": _message(exc)[:200]}
    return out


# -- generation ----------------------------------------------------------------------------------------------------

def _asset_view(a: Mapping[str, Any]) -> dict[str, Any]:
    findings = [{"type": f.get("type"), "description": f.get("description")} for f in a.get("findings") or []]
    statement = statement_of(a.get("definition") or {})
    return {"id": a.get("policyGenerationAssetId"), "statement": statement, "fragment": a.get("rawTextFragment"), "findings": findings,
            "usable": bool(statement) and not any(f["type"] in ("INVALID", "NOT_TRANSLATABLE") for f in findings)}


def start_generation(session: Any, region: str, engine_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Cedar from a sentence, for one Gateway's tools (the Gateway is only read)."""
    text = str(body.get("text") or "").strip()
    if not 1 <= len(text) <= 2000:
        raise GovernanceError("text: say in 1-2000 characters who may or may not do what")
    name = str(body.get("name") or f"adlc_gen_{datetime.now(timezone.utc).strftime('%m%d%H%M%S')}")
    if not NAME.match(name):
        raise GovernanceError("name: a letter, then letters, digits or underscores (at most 48)")
    ctl = _ctl(session, region)
    _console_engine(ctl, engine_id)
    gateway = ctl.get_gateway(gatewayIdentifier=_check(body.get("gatewayId"), GATEWAY_ID, "gatewayId"))
    out = ctl.start_policy_generation(policyEngineId=engine_id, resource={"arn": gateway["gatewayArn"]}, content={"rawText": text}, name=name)
    return {"id": out.get("policyGenerationId"), "name": out.get("name"), "status": out.get("status"), "gatewayId": gateway["gatewayId"]}


def generation(session: Any, region: str, engine_id: str, generation_id: str) -> dict[str, Any]:
    ctl = _ctl(session, region)
    engine_id, generation_id = _check(engine_id, RESOURCE_ID, "policy engine"), _check(generation_id, RESOURCE_ID, "generation")
    g = ctl.get_policy_generation(policyEngineId=engine_id, policyGenerationId=generation_id)
    assets = []
    if g.get("status") == "GENERATED":
        assets = [_asset_view(a) for a in _pages(ctl.list_policy_generation_assets, "policyGenerationAssets", policyEngineId=engine_id,
                                               policyGenerationId=generation_id)]
    return {"id": g.get("policyGenerationId"), "name": g.get("name"), "status": g.get("status"), "statusReasons": list(g.get("statusReasons") or []),
            "findings": g.get("findings"), "gatewayArn": (g.get("resource") or {}).get("arn"), "createdAt": _iso(g.get("createdAt")), "assets": assets}


# -- Gateways: the engine, rules, rate limits, resource policy -----------------------------------------------------

def _changeable(ctl: Any, gateway_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """The Gateway, when the console created it or the caller acknowledged changing someone else's."""
    gateway = ctl.get_gateway(gatewayIdentifier=_check(gateway_id, GATEWAY_ID, "gateway"))
    console = _console(_tags(ctl, gateway["gatewayArn"]))
    if not console and body.get("acknowledged") is not True:
        raise GovernanceError(f"Gateway {gateway.get('name')} was not created from this console and every agent using it is affected: "
                              "send acknowledged: true to change it", 409, needsAcknowledgement=True, gateway=gateway.get("name"))
    return gateway


@lru_cache(maxsize=1)
def _update_fields() -> tuple[str, ...]:
    import botocore.session

    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    return tuple(model.operation_model("UpdateGateway").input_shape.members)


def gateway_update(gateway: Mapping[str, Any], engine: Mapping[str, str] | None) -> dict[str, Any]:
    """UpdateGateway replaces the Gateway: every field it accepts is echoed from GetGateway; only the engine changes
    (``None`` detaches)."""
    request = {k: gateway[k] for k in _update_fields() if k in gateway and k not in ("gatewayIdentifier", "policyEngineConfiguration")}
    request["gatewayIdentifier"] = gateway["gatewayId"]
    if engine:
        request["policyEngineConfiguration"] = {"arn": engine["arn"], "mode": engine["mode"]}
    return request


def role_statement(engine_arn: str, gateway_arn: str) -> dict[str, Any]:
    """What the Gateway role needs to evaluate the engine's policies (the service checks it before it attaches)."""
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "ReadPolicyEngine", "Effect": "Allow", "Action": POLICY_ACTIONS[0], "Resource": engine_arn},
        {"Sid": "Authorize", "Effect": "Allow", "Action": list(POLICY_ACTIONS[1:]), "Resource": [engine_arn, gateway_arn]}]}


def _role_refusal(exc: BaseException) -> bool:
    """The service's refusal of a Gateway role without the policy permissions (not the caller's own AccessDenied)."""
    return _code(exc) in ("ValidationException", "AccessDeniedException") and any(a.split(":")[1] in _message(exc) for a in POLICY_ACTIONS)


def _console_role(iam: Any, role_arn: str) -> bool:
    try:
        tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=role_arn.rsplit("/", 1)[-1]).get("Tags") or []}
    except Exception:  # noqa: BLE001 - unreadable is not the console's
        return False
    return _console(tags)


def _bound_role(session: Any, role_arn: str, boundary: str | None) -> None:
    """UpdateGateway passes the Gateway's role again: in a workspace with a permissions boundary the console's own role
    (on its path) gets the boundary first when it lacks it (``agents.ensure_boundary``); another team's is left as it is."""
    if not boundary or not role_arn:
        return
    try:
        ensure_boundary(client(session, "iam"), role_arn.rsplit("/", 1)[-1], boundary)
    except Exception as exc:  # noqa: BLE001 - a role gone since: UpdateGateway answers for it
        if "NoSuchEntity" not in str(exc):
            raise


def _wait_gateway(ctl: Any, gateway_id: str, done: Callable[[Mapping[str, Any]], bool], *, attempts: int = 40, pause: float = 3.0) -> dict[str, Any]:
    gateway: dict[str, Any] = {}
    for _ in range(attempts):
        gateway = ctl.get_gateway(gatewayIdentifier=gateway_id)
        if gateway.get("status") in ("FAILED", "UPDATE_UNSUCCESSFUL"):
            raise GovernanceError(f"the Gateway is {gateway.get('status')}: {'; '.join(gateway.get('statusReasons') or [])[:400]}", 502)
        if gateway.get("status") == "READY" and done(gateway):
            break
        _pause(pause)
    return gateway


def attach(session: Any, region: str, gateway_id: str, body: Mapping[str, Any], *, boundary: str | None = None) -> dict[str, Any]:
    """Attach an engine to a Gateway (LOG_ONLY: decisions are only logged; ENFORCE: denied calls are refused). With the
    workspace's permissions ``boundary`` the console's own Gateway role is bounded before it is passed again, and the
    policy-engine grant goes through ``agents.put_inline_policy`` (bounded first, refused with the statement to add)."""
    mode = str(body.get("mode") or "LOG_ONLY")
    if mode not in MODES:
        raise GovernanceError("mode: LOG_ONLY or ENFORCE")
    ctl = _ctl(session, region)
    gateway = _changeable(ctl, gateway_id, body)
    engine = ctl.get_policy_engine(policyEngineId=_check(body.get("engineId"), RESOURCE_ID, "engineId"))
    if engine.get("status") != "ACTIVE":
        raise GovernanceError(f"the policy engine is {engine.get('status')}", 409)
    if gateway.get("status") != "READY":
        raise GovernanceError(f"the Gateway is {gateway.get('status')}: wait until it is READY", 409)
    wanted = {"arn": str(engine["policyEngineArn"]), "mode": mode}
    before = gateway.get("policyEngineConfiguration") or None
    out: dict[str, Any] = {"gatewayId": gateway["gatewayId"], "engineId": engine["policyEngineId"], "mode": mode,
                           "replaced": before["arn"] if before and before.get("arn") != wanted["arn"] else None, "roleGranted": False}
    if mode == "ENFORCE":
        live = [p for p in _pages(ctl.list_policies, "policies", policyEngineId=engine["policyEngineId"])
                if p.get("status") == "ACTIVE" and p.get("enforcementMode") == "ACTIVE" and re.search(r"\bpermit\s*\(", statement_of(p.get("definition") or {}))]
        if not live:
            out["warning"] = "no ACTIVE permit policy in this engine: under ENFORCE every tool call is denied"
    if before == wanted:
        return {**out, "status": gateway.get("status"), "unchanged": True}
    role = str(gateway.get("roleArn") or "")
    statement = role_statement(wanted["arn"], gateway["gatewayArn"])
    _bound_role(session, role, boundary)
    for attempt in range(6):
        try:
            ctl.update_gateway(**gateway_update(gateway, wanted))
            break
        except Exception as exc:  # noqa: BLE001 - the role refusal is answered with what the role needs
            if not _role_refusal(exc):
                raise
            if out["roleGranted"]:
                if attempt < 5:
                    _pause(10)  # IAM propagation
                    continue
                raise GovernanceError("the Gateway's role has the policy permissions now, but the service still refuses: try again in a minute", 409,
                                      roleArn=role, roleStatement=statement) from exc
            mine = _console_role(client(session, "iam"), role)
            if body.get("grantRole") is True and mine:
                try:
                    put_inline_policy(session, role, ROLE_POLICY, statement, what="the policy engine", boundary=boundary)
                except AgentError as refused:  # a role this workspace may not change: the statement for its owner to add
                    raise GovernanceError(str(refused), 409, roleArn=role, roleStatement=statement, canGrant=False) from exc
                out["roleGranted"] = True
                _pause(10)
                continue
            raise GovernanceError("the Gateway's role may not use this policy engine" + ("" if mine else
                                  " (the role was not created from this console: add the statement to it yourself)"), 409,
                                  roleArn=role, roleStatement=statement, canGrant=mine, detail=_message(exc)[:400]) from exc
    final = _wait_gateway(ctl, gateway["gatewayId"], lambda g: (g.get("policyEngineConfiguration") or None) == wanted)
    return {**out, "status": final.get("status"), "engine": final.get("policyEngineConfiguration")}


def detach(session: Any, region: str, gateway_id: str, body: Mapping[str, Any], *, boundary: str | None = None) -> dict[str, Any]:
    ctl = _ctl(session, region)
    gateway = _changeable(ctl, gateway_id, body)
    before = gateway.get("policyEngineConfiguration") or None
    if not before:
        return {"gatewayId": gateway["gatewayId"], "detached": None, "status": gateway.get("status")}
    if gateway.get("status") != "READY":
        raise GovernanceError(f"the Gateway is {gateway.get('status')}: wait until it is READY", 409)
    _bound_role(session, str(gateway.get("roleArn") or ""), boundary)
    ctl.update_gateway(**gateway_update(gateway, None))
    final = _wait_gateway(ctl, gateway["gatewayId"], lambda g: not g.get("policyEngineConfiguration"))
    return {"gatewayId": gateway["gatewayId"], "detached": before.get("arn"), "status": final.get("status")}


def _rule_view(r: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": r.get("ruleId"), "priority": r.get("priority"), "description": r.get("description"), "conditions": list(r.get("conditions") or []),
            "actions": list(r.get("actions") or []), "status": r.get("status"), "managedBy": (r.get("system") or {}).get("managedBy"),
            "createdAt": _iso(r.get("createdAt")), "updatedAt": _iso(r.get("updatedAt"))}


def rules(session: Any, region: str, gateway_id: str) -> list[dict[str, Any]]:
    found = _pages(_ctl(session, region).list_gateway_rules, "gatewayRules", gatewayIdentifier=_check(gateway_id, GATEWAY_ID, "gateway"))
    return [_rule_view(r) for r in sorted(found, key=lambda r: int(r.get("priority") or 0))]


def _one_key(item: Any, allowed: Iterable[str], what: str) -> tuple[str, Any]:
    if not isinstance(item, dict) or len(item) != 1 or next(iter(item)) not in allowed:
        raise GovernanceError(f"{what}: one of {', '.join(allowed)}")
    key = next(iter(item))
    return key, item[key]


def _split(entries: Any, what: str, target: str) -> None:
    if not isinstance(entries, list) or len(entries) != 2:
        raise GovernanceError(f"{what}: exactly two weighted entries")
    for e in entries:
        weight = e.get("weight") if isinstance(e, dict) else None
        if not isinstance(e, dict) or not str(e.get("name") or "") or not isinstance(weight, int) or not 1 <= weight <= 99 or not e.get(target):
            raise GovernanceError(f"{what}: each entry has a name, a whole weight 1-99 and its {target}")


def rule_request(body: Mapping[str, Any]) -> dict[str, Any]:
    """CreateGatewayRule's fields, checked: a priority, at most two conditions (callers or paths), one or two actions
    (route to an HTTP target, or a configuration bundle), each as the API spells it."""
    try:
        priority = int(body.get("priority"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise GovernanceError("priority: a whole number 1-1000000") from None
    if not 1 <= priority <= 1_000_000:
        raise GovernanceError("priority: a whole number 1-1000000")
    conditions, actions = body.get("conditions") or [], body.get("actions") or []
    if not isinstance(conditions, list) or len(conditions) > 2:
        raise GovernanceError("conditions: at most two")
    for c in conditions:
        kind, value = _one_key(c, ("matchPrincipals", "matchPaths"), "each condition")
        anyof = (value or {}).get("anyOf") if isinstance(value, dict) else None
        if not isinstance(anyof, list) or not anyof:
            raise GovernanceError(f"{kind}: anyOf needs at least one entry")
        for entry in anyof:
            if kind == "matchPaths" and not (isinstance(entry, str) and PATH.match(entry)):
                raise GovernanceError(f"matchPaths: each path is /<target>/* (got {str(entry)[:80]!r})")
            if kind == "matchPrincipals":
                principal = (entry or {}).get("iamPrincipal") if isinstance(entry, dict) else None
                if not isinstance(principal, dict) or not IAM_PRINCIPAL.match(str(principal.get("arn") or "")) \
                        or principal.get("operator", "StringEquals") not in ("StringEquals", "StringLike"):
                    raise GovernanceError("matchPrincipals: each entry is {iamPrincipal: {arn: <IAM user/role or assumed-role ARN>, operator: StringEquals|StringLike}}")
    if not isinstance(actions, list) or not 1 <= len(actions) <= 2:
        raise GovernanceError("actions: one or two")
    for a in actions:
        kind, value = _one_key(a, ("routeToTarget", "configurationBundle"), "each action")
        if kind == "routeToTarget":
            how, spec = _one_key(value, ("staticRoute", "weightedRoute"), "routeToTarget")
            if how == "staticRoute" and not str((spec or {}).get("targetName") or ""):
                raise GovernanceError("staticRoute: targetName")
            if how == "weightedRoute":
                _split((spec or {}).get("trafficSplit"), "weightedRoute.trafficSplit", "targetName")
        else:
            how, spec = _one_key(value, ("staticOverride", "weightedOverride"), "configurationBundle")
            if how == "staticOverride" and not ((spec or {}).get("bundleArn") and (spec or {}).get("bundleVersion")):
                raise GovernanceError("staticOverride: bundleArn and bundleVersion")
            if how == "weightedOverride":
                _split((spec or {}).get("trafficSplit"), "weightedOverride.trafficSplit", "configurationBundle")
    request: dict[str, Any] = {"priority": priority, "actions": actions}
    if conditions:
        request["conditions"] = conditions
    description = str(body.get("description") or "").strip()
    if description:
        request["description"] = description[:256]
    return request


def create_rule(session: Any, region: str, gateway_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    request = rule_request(body)
    ctl = _ctl(session, region)
    gateway = _changeable(ctl, gateway_id, body)
    return _rule_view(ctl.create_gateway_rule(gatewayIdentifier=gateway["gatewayId"], **request))


def delete_rule(session: Any, region: str, gateway_id: str, rule_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    ctl = _ctl(session, region)
    gateway = _changeable(ctl, gateway_id, body)
    rule = ctl.get_gateway_rule(gatewayIdentifier=gateway["gatewayId"], ruleId=_check(rule_id, RULE_ID, "rule"))
    managed = (rule.get("system") or {}).get("managedBy")
    if managed:
        raise GovernanceError(f"this rule is managed by {managed}: change it there", 409)
    out = ctl.delete_gateway_rule(gatewayIdentifier=gateway["gatewayId"], ruleId=rule_id)
    return {"id": rule_id, "status": out.get("status")}


def _rate_limit_view(r: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": r.get("rateLimitId"), "description": r.get("description"), "dimensionKeys": list(r.get("dimensionKeys") or []),
            "entries": list(r.get("entries") or []), "status": r.get("status"), "createdAt": _iso(r.get("createdAt")), "updatedAt": _iso(r.get("updatedAt"))}


def rate_limits(session: Any, region: str, gateway_id: str) -> list[dict[str, Any]]:
    found = _pages(_ctl(session, region).list_gateway_rate_limits, "rateLimits", gatewayIdentifier=_check(gateway_id, GATEWAY_ID, "gateway"))
    return [_rate_limit_view(r) for r in found]


def rate_limit_request(body: Mapping[str, Any]) -> dict[str, Any]:
    """CreateGatewayRateLimit's fields, checked the way the service checks them (live 2026-10-01)."""
    keys = body.get("dimensionKeys")
    if not isinstance(keys, list) or not 1 <= len(keys) <= 10 or len(set(map(str, keys))) != len(keys):
        raise GovernanceError("dimensionKeys: 1-10 different keys")
    for key in keys:
        if not DIMENSION_KEY.match(str(key)):
            raise GovernanceError(f"dimensionKeys: {str(key)[:80]!r} is not one of toolName, targetName, qualifiedModelId, "
                                  "$.context.iam.principal, $.context.iam.sourceIdentity or $.context.jwt.<claim>")
    entries = body.get("entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 1000:
        raise GovernanceError("entries: 1-1000")
    wire = []
    for i, entry in enumerate(entries):
        dims = (entry or {}).get("dimensions") if isinstance(entry, dict) else None
        if not isinstance(dims, dict) or set(dims) != set(keys) or not all(str(v) for v in dims.values()):
            raise GovernanceError(f"entry {i}: dimensions give a value for exactly the keys {', '.join(map(str, keys))}")
        values = [str(dims[k]) for k in keys]
        if "*" in values and any(v != "*" for v in values[values.index("*"):]):
            raise GovernanceError(f"entry {i}: '*' only in trailing positions (after a '*' every later key is '*' too)")
        out: dict[str, Any] = {"dimensions": {str(k): str(dims[k]) for k in keys}}
        for metric, periods in PERIODS.items():
            config = entry.get(metric)
            if config is None:
                continue
            config = config[0] if isinstance(config, list) and len(config) == 1 else config
            if not isinstance(config, dict):
                raise GovernanceError(f"entry {i}: {metric} is one {{rate, period}}")
            try:
                rate = float(config.get("rate"))
            except (TypeError, ValueError):
                raise GovernanceError(f"entry {i}: {metric} rate is a number") from None
            if not 0 <= rate <= 10_000_000:
                raise GovernanceError(f"entry {i}: {metric} rate 0-10000000")
            if config.get("period") not in periods:
                raise GovernanceError(f"entry {i}: {metric} are counted per {' or '.join(periods)}")
            out[metric] = [{"rate": rate, "period": config["period"]}]
        if len(out) == 1:
            raise GovernanceError(f"entry {i}: limit requests, tokens and/or connections")
        wire.append(out)
    request: dict[str, Any] = {"dimensionKeys": [str(k) for k in keys], "entries": wire}
    if body.get("rateLimitId"):
        request["rateLimitId"] = _check(body.get("rateLimitId"), RATE_LIMIT_ID, "rateLimitId")
    description = str(body.get("description") or "").strip()
    if description:
        request["description"] = description[:512]
    return request


def create_rate_limit(session: Any, region: str, gateway_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    request = rate_limit_request(body)
    ctl = _ctl(session, region)
    gateway = _changeable(ctl, gateway_id, body)
    return _rate_limit_view(ctl.create_gateway_rate_limit(gatewayIdentifier=gateway["gatewayId"], **request))


def delete_rate_limit(session: Any, region: str, gateway_id: str, rate_limit_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    ctl = _ctl(session, region)
    gateway = _changeable(ctl, gateway_id, body)
    out = ctl.delete_gateway_rate_limit(gatewayIdentifier=gateway["gatewayId"], rateLimitId=_check(rate_limit_id, RATE_LIMIT_ID, "rate limit"))
    return {"id": rate_limit_id, "status": out.get("status")}


def resource_policy(session: Any, region: str, gateway_id: str) -> dict[str, Any]:
    """The Gateway's resource-based policy (who may invoke it); live: none answers ``{}``."""
    ctl = _ctl(session, region)
    gateway = ctl.get_gateway(gatewayIdentifier=_check(gateway_id, GATEWAY_ID, "gateway"))
    text = ctl.get_resource_policy(resourceArn=gateway["gatewayArn"]).get("policy")
    return {"gatewayId": gateway["gatewayId"], "policy": json.loads(text) if text else None}


def put_resource_policy(session: Any, region: str, gateway_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    document = body.get("policy")
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except ValueError as exc:
            raise GovernanceError(f"policy: not JSON ({exc})") from None
    if not isinstance(document, dict) or not isinstance(document.get("Statement"), list) or not document["Statement"]:
        raise GovernanceError("policy: an IAM policy document with a Statement list")
    ctl = _ctl(session, region)
    gateway = _changeable(ctl, gateway_id, body)
    out = ctl.put_resource_policy(resourceArn=gateway["gatewayArn"], policy=json.dumps(document))
    return {"gatewayId": gateway["gatewayId"], "policy": json.loads(out.get("policy") or "null")}


def delete_resource_policy(session: Any, region: str, gateway_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    ctl = _ctl(session, region)
    gateway = _changeable(ctl, gateway_id, body)
    try:
        ctl.delete_resource_policy(resourceArn=gateway["gatewayArn"])
    except Exception as exc:  # noqa: BLE001 - nothing to delete
        if _code(exc) != "ResourceNotFoundException":
            raise
        return {"gatewayId": gateway["gatewayId"], "deleted": False}
    return {"gatewayId": gateway["gatewayId"], "deleted": True}


# -- the decision log ----------------------------------------------------------------------------------------------

def _window(hours: Any) -> tuple[float, datetime, datetime]:
    try:
        h = max(1.0, min(float(hours), MAX_HOURS))
    except (TypeError, ValueError):
        h = 24.0
    end = datetime.now(timezone.utc)
    return h, end - timedelta(hours=h), end


def channels(logs: Any, gateway_arn: str) -> dict[str, Any]:
    """Where this Gateway's decisions are delivered: its APPLICATION_LOGS log group, and whether traces reach aws/spans."""
    sources = {s["name"]: s for s in _pages(logs.describe_delivery_sources, "deliverySources") if gateway_arn in (s.get("resourceArns") or [])}
    out: dict[str, Any] = {"logGroup": None, "traces": False, "deliveries": []}
    deliveries = [d for d in _pages(logs.describe_deliveries, "deliveries") if d.get("deliverySourceName") in sources] if sources else []
    destinations = {d["arn"]: d for d in _pages(logs.describe_delivery_destinations, "deliveryDestinations")} if deliveries else {}
    for d in deliveries:
        kind = sources[d["deliverySourceName"]].get("logType")
        out["deliveries"].append({"id": d.get("id"), "arn": d.get("arn"), "source": d["deliverySourceName"], "logType": kind,
                                  "destination": d.get("deliveryDestinationArn"), "type": d.get("deliveryDestinationType")})
        if kind == "APPLICATION_LOGS" and d.get("deliveryDestinationType") == "CWL":
            arn = str(((destinations.get(d.get("deliveryDestinationArn")) or {}).get("deliveryDestinationConfiguration") or {}).get("destinationResourceArn") or "")
            if ":log-group:" in arn:
                out["logGroup"] = arn.split(":log-group:", 1)[1].removesuffix(":*")
        if kind == "TRACES" and d.get("deliveryDestinationType") == "XRAY":
            out["traces"] = True
    return out


def _query(logs: Any, group: str, query: str, start: datetime, end: datetime, *, timeout: float = 60) -> list[dict[str, str]]:
    qid = logs.start_query(logGroupName=group, startTime=int(start.timestamp()), endTime=int(end.timestamp()), queryString=query, limit=5000)["queryId"]
    deadline = time.monotonic() + timeout
    while True:
        out = logs.get_query_results(queryId=qid)
        if out.get("status") in ("Complete", "Failed", "Cancelled", "Timeout") or time.monotonic() > deadline:
            return [{f["field"]: f["value"] for f in row} for row in out.get("results") or []]
        _pause(1)


def _messages(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        try:
            out.append(json.loads(row.get("@message") or ""))
        except ValueError:
            continue
    return out


def tool_call(request_body: Any) -> tuple[str | None, str | None]:
    """The tool and its arguments from the log's request text ``{…, method=tools/call, params={name=…, arguments={…}}}``."""
    text = str(request_body or "")
    args, rest, at = None, text, text.find("arguments={")
    if at >= 0:
        depth, start = 0, at + len("arguments=")
        for i in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                args, rest = text[start:i + 1][:300], text[:at] + text[i + 1:]
                break
    params = rest.find("params={")
    name = re.search(r"\bname=([^,}\s]+)", rest[params:]) if params >= 0 else None
    return (name.group(1) if name else None), args


LOG_QUERY = ("fields @timestamp, @message | filter body.log like /^Policy evaluation/ or body.log like /^Tool Execution Denied/ "
             "or (body.log = \"Started processing request\" and body.requestBody like /method=tools\\/call/) | sort @timestamp desc | limit 5000")


def log_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per policy decision of the Gateway's application log, joined by ``request_id`` to its request (the tool)
    and to "Tool Execution Denied" (an enforced denial; a LOG_ONLY denial lets the call run)."""
    by_request: dict[str, dict[str, Any]] = {}
    for rec in records:
        body = rec.get("body") or {}
        row = by_request.setdefault(str(rec.get("request_id") or rec.get("trace_id") or len(by_request)),
                                    {"tool": None, "arguments": None, "denied": False, "mode": None, "logOnly": [], "flips": []})
        text = str(body.get("log") or "")
        if isinstance(body.get("policy"), dict):
            p = body["policy"]
            principal = p.get("principal") or {}
            row.update(at=_iso(datetime.fromtimestamp(int(rec.get("event_timestamp") or 0) / 1000, timezone.utc)), decision=p.get("decision"),
                       policies=list(p.get("determiningPolicies") or []), reason=p.get("reason"), principal=principal.get("entityId"),
                       principalType=principal.get("entityType"), engineArn=p.get("policyEngineArn"), latencyMs=p.get("latencyMs"),
                       requestId=rec.get("request_id"), traceId=rec.get("trace_id"), source="logs")
        elif text.startswith("Tool Execution Denied"):
            row["denied"] = True
        elif text == "Started processing request":
            row["tool"], row["arguments"] = tool_call(body.get("requestBody"))
    rows = []
    for row in by_request.values():
        if not row.get("decision"):
            continue
        denied = row.pop("denied")
        row["blocked"] = bool(row["decision"] == "DENY" and denied)
        rows.append(row)
    return sorted(rows, key=lambda r: str(r.get("at") or ""), reverse=True)


def span_rows(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per ``AgentCore.Policy.AuthorizeAction`` span, its tool read from the parent ``AgentCore.Gateway.InvokeTool``."""
    calls = {s.get("spanId"): s for s in spans if s.get("name") == "AgentCore.Gateway.InvokeTool"}
    rows = []
    for s in spans:
        if s.get("name") != "AgentCore.Policy.AuthorizeAction":
            continue
        a = s.get("attributes") or {}
        call = (calls.get(s.get("parentSpanId")) or {}).get("attributes") or {}
        decision, mode = a.get("aws.agentcore.policy.authorization_decision"), a.get("aws.agentcore.gateway.policy.mode")
        rows.append({"at": _iso(datetime.fromtimestamp(int(s.get("startTimeUnixNano") or 0) / 1e9, timezone.utc)), "decision": decision,
                     "tool": call.get("tool.name"), "arguments": None, "policies": list(a.get("aws.agentcore.policy.determining_policies") or []),
                     "reason": a.get("aws.agentcore.policy.authorization_reason"), "principal": None, "principalType": None,
                     "engineArn": a.get("aws.agentcore.gateway.policy.arn"), "mode": mode, "blocked": decision == "DENY" and mode == "ENFORCE",
                     "logOnly": list(a.get("aws.agentcore.policy.log_only_matched_policies") or []),
                     "flips": list(a.get("aws.agentcore.policy.log_only_decision_flipping_policies") or []),
                     "requestId": a.get("aws.request.id"), "traceId": s.get("traceId"), "source": "spans"})
    return sorted(rows, key=lambda r: str(r.get("at") or ""), reverse=True)


def decision_metrics(cw: Any, gateway_id: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Counts from ``AWS/Bedrock-AgentCore``: the same decision is published under many dimension sets, so only exact
    sets are summed: the Gateway's total, per policy (the determining one) and per attachment mode."""
    picks: list[tuple[tuple[str, str, str | None], dict[str, Any]]] = []
    for name in DECISION_METRICS:
        for page in cw.get_paginator("list_metrics").paginate(Namespace=NAMESPACE, MetricName=name, Dimensions=[{"Name": "TargetResource", "Value": gateway_id}]):
            for m in page.get("Metrics") or []:
                dims = {d["Name"]: d["Value"] for d in m.get("Dimensions") or []}
                if dims.get("OperationName") != "AuthorizeAction" or dims.get("TargetResource") != gateway_id:
                    continue
                extra = set(dims) - {"OperationName", "TargetResource"}
                if not extra:
                    picks.append(((name, "total", None), m))
                elif extra == {"Policy"}:
                    picks.append(((name, "policy", dims["Policy"]), m))
                elif extra == {"Mode"}:
                    picks.append(((name, "mode", dims["Mode"]), m))
    seconds = max(300, int((end - start).total_seconds() / 50) // 60 * 60)
    sums = [0.0] * len(picks)
    for at in range(0, len(picks), 500):
        queries = [{"Id": f"m{at + i}", "MetricStat": {"Metric": m, "Period": seconds, "Stat": "Sum"}, "ReturnData": True} for i, (_, m) in enumerate(picks[at:at + 500])]
        kwargs: dict[str, Any] = {"MetricDataQueries": queries, "StartTime": start, "EndTime": end}
        while True:
            got = cw.get_metric_data(**kwargs)
            for result in got.get("MetricDataResults") or []:
                sums[int(result["Id"][1:])] += sum(float(v) for v in result.get("Values") or [])
            if not got.get("NextToken"):
                break
            kwargs["NextToken"] = got["NextToken"]
    label = {"AllowDecisions": "allow", "DenyDecisions": "deny", "NoDeterminingPolicies": "defaultDeny", "LogOnlyMatches": "logOnlyMatches",
             "LogOnlyDecisionFlips": "flips"}
    out: dict[str, Any] = {v: 0 for v in label.values()}
    by_policy: dict[str, dict[str, int]] = {}
    by_mode: dict[str, dict[str, int]] = {}
    for ((name, kind, key), _m), total in zip(picks, sums):
        n = int(round(total))
        if kind == "total":
            out[label[name]] += n
        elif kind == "policy":
            by_policy.setdefault(str(key), {v: 0 for v in label.values()})[label[name]] += n
        elif name in ("AllowDecisions", "DenyDecisions"):
            by_mode.setdefault(str(key), {"allow": 0, "deny": 0})[label[name]] += n
    out["byPolicy"] = [{"policy": k, **v} for k, v in sorted(by_policy.items(), key=lambda kv: -(kv[1]["allow"] + kv[1]["deny"]))]
    out["byMode"] = by_mode
    return out


def decisions(session: Any, region: str, gateway_id: str, hours: Any = 24, limit: int = 200) -> dict[str, Any]:
    """The Gateway's policy decisions over the last ``hours``: one row per decision from its application log (or its
    spans), and the counts from the metrics, which exist without any delivery."""
    ctl = _ctl(session, region)
    gateway = ctl.get_gateway(gatewayIdentifier=_check(gateway_id, GATEWAY_ID, "gateway"))
    h, start, end = _window(hours)
    logs = client(session, "logs", region)
    found = channels(logs, gateway["gatewayArn"])
    rows: list[dict[str, Any]] = []
    source, error = None, None
    try:
        if found["logGroup"]:
            source = "logs"
            rows = log_rows(_messages(_query(logs, found["logGroup"], LOG_QUERY, start, end)))
        elif found["traces"]:
            source = "spans"
            query = ("fields @timestamp, @message | filter (name = \"AgentCore.Policy.AuthorizeAction\" or name = \"AgentCore.Gateway.InvokeTool\") "
                     f"and `attributes.aws.resource.arn` = \"{gateway['gatewayArn']}\" | sort @timestamp desc | limit 5000")
            rows = span_rows(_messages(_query(logs, "aws/spans", query, start, end)))
    except Exception as exc:  # noqa: BLE001 - a log group nothing reached yet; the counts still answer
        if _code(exc) != "ResourceNotFoundException":
            error = f"{_code(exc) or type(exc).__name__}: {_message(exc)[:300]}"
    summary = decision_metrics(client(session, "cloudwatch", region), gateway["gatewayId"], start, end)
    return {"gatewayId": gateway["gatewayId"], "gatewayName": gateway.get("name"), "hours": h, "engine": gateway.get("policyEngineConfiguration"),
            "source": source, "logGroup": found["logGroup"], "traces": found["traces"], "rows": rows[:max(1, min(int(limit), 1000))],
            "total": len(rows), "summary": summary, "error": error}


def enable_decision_log(session: Any, region: str, gateway_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Deliver the Gateway's application log (one record per decision, with the caller) to CloudWatch Logs: the log group
    (30 days), the delivery source and destination and the delivery, all tagged."""
    ctl = _ctl(session, region)
    gateway = _changeable(ctl, gateway_id, body)
    logs = client(session, "logs", region)
    found = channels(logs, gateway["gatewayArn"])
    if found["logGroup"]:
        return {"gatewayId": gateway["gatewayId"], "logGroup": found["logGroup"], "created": False}
    gid, account = gateway["gatewayId"], gateway["gatewayArn"].split(":")[4]
    group = LOG_GROUP.format(gateway=gid)
    try:
        logs.create_log_group(logGroupName=group, tags=dict(CONSOLE_TAG))
        logs.put_retention_policy(logGroupName=group, retentionInDays=30)
    except Exception as exc:  # noqa: BLE001 - an existing group is reused as it is
        if _code(exc) != "ResourceAlreadyExistsException":
            raise
    source = logs.put_delivery_source(name=f"{gid}-logs-source", logType="APPLICATION_LOGS", resourceArn=gateway["gatewayArn"],
                                      tags=dict(CONSOLE_TAG))["deliverySource"]
    destination = logs.put_delivery_destination(name=f"{gid}-logs-destination", deliveryDestinationType="CWL", tags=dict(CONSOLE_TAG),
                                                deliveryDestinationConfiguration={"destinationResourceArn": f"arn:aws:logs:{region}:{account}:log-group:{group}"}
                                                )["deliveryDestination"]
    delivery = logs.create_delivery(deliverySourceName=source["name"], deliveryDestinationArn=destination["arn"], tags=dict(CONSOLE_TAG))["delivery"]
    return {"gatewayId": gid, "logGroup": group, "created": True, "deliveryId": delivery.get("id")}


def disable_decision_log(session: Any, region: str, gateway_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Stop the application-log deliveries the console created (the log group and what it holds stay)."""
    ctl = _ctl(session, region)
    gateway = _changeable(ctl, gateway_id, body)
    logs = client(session, "logs", region)
    found = channels(logs, gateway["gatewayArn"])

    def mine(arn: str) -> bool:
        return _console(logs.list_tags_for_resource(resourceArn=arn).get("tags") or {})

    deleted, kept = [], []
    for d in found["deliveries"]:
        if d["logType"] != "APPLICATION_LOGS":
            continue
        if not mine(str(d["arn"])):
            kept.append(d["id"])
            continue
        logs.delete_delivery(id=d["id"])
        deleted.append(d["id"])
        source_arn = str(d["arn"]).split(":delivery:")[0] + f":delivery-source:{d['source']}"
        if mine(source_arn):
            logs.delete_delivery_source(name=d["source"])
        if d.get("destination") and mine(str(d["destination"])):
            logs.delete_delivery_destination(name=str(d["destination"]).rsplit(":", 1)[-1])
    return {"gatewayId": gateway["gatewayId"], "deleted": deleted, "kept": kept, "logGroup": found["logGroup"]}


# -- routes --------------------------------------------------------------------------------------------------------

def guarded(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """A route whose refusals are answers: the console's own with their status, AWS's with theirs (502 for a 5xx)."""
    return common_guarded(fn, (GovernanceError,), limit=1500)


def register(router: Any) -> None:
    """Add this module's /api/console routes: reads for every member of the workspace, writes for admins."""
    def sess(r: Any) -> tuple[Any, str]:
        session = r.session()
        return session, r.console.workspaces.get(r.workspace())["region"]

    def limit(r: Any) -> int:
        text = str(r.query.get("limit") or "200")
        return int(text) if text.isdigit() else 200

    def bound(r: Any) -> str | None:  # the workspace's permissions boundary (a spoke workspace's)
        return boundary_of(r.console.workspaces.get(r.workspace()))

    base = "/workspaces/{wid}/governance"
    engine, gateway = f"{base}/engines/{{eid}}", f"{base}/gateways/{{gw}}"
    routes: list[tuple[str, str, Callable[[Any], Any], bool]] = [
        ("GET", base, lambda r: (200, overview(*sess(r))), False),
        ("POST", f"{base}/engines", lambda r: (201, create_engine(*sess(r), r.body)), True),
        ("DELETE", engine, lambda r: (200, delete_engine(*sess(r), r.params["eid"], r.body)), True),
        ("GET", f"{engine}/policies", lambda r: (200, {"policies": policies(*sess(r), r.params["eid"])}), False),
        ("POST", f"{engine}/policies", lambda r: (201, create_policy(*sess(r), r.params["eid"], r.body)), True),
        ("POST", f"{engine}/validate", lambda r: (200, validate_policy(*sess(r), r.params["eid"], r.body)), True),
        ("GET", f"{engine}/policies/{{pid}}", lambda r: (200, get_policy(*sess(r), r.params["eid"], r.params["pid"])), False),
        ("PUT", f"{engine}/policies/{{pid}}", lambda r: (200, update_policy(*sess(r), r.params["eid"], r.params["pid"], r.body)), True),
        ("DELETE", f"{engine}/policies/{{pid}}", lambda r: (200, delete_policy(*sess(r), r.params["eid"], r.params["pid"])), True),
        ("POST", f"{engine}/generations", lambda r: (202, start_generation(*sess(r), r.params["eid"], r.body)), True),
        ("GET", f"{engine}/generations/{{gen}}", lambda r: (200, generation(*sess(r), r.params["eid"], r.params["gen"])), False),
        ("PUT", f"{gateway}/engine", lambda r: (200, attach(*sess(r), r.params["gw"], r.body, boundary=bound(r))), True),
        ("DELETE", f"{gateway}/engine", lambda r: (200, detach(*sess(r), r.params["gw"], r.body, boundary=bound(r))), True),
        ("GET", f"{gateway}/rules", lambda r: (200, {"rules": rules(*sess(r), r.params["gw"])}), False),
        ("POST", f"{gateway}/rules", lambda r: (201, create_rule(*sess(r), r.params["gw"], r.body)), True),
        ("DELETE", f"{gateway}/rules/{{rid}}", lambda r: (200, delete_rule(*sess(r), r.params["gw"], r.params["rid"], r.body)), True),
        ("GET", f"{gateway}/rate-limits", lambda r: (200, {"rateLimits": rate_limits(*sess(r), r.params["gw"])}), False),
        ("POST", f"{gateway}/rate-limits", lambda r: (201, create_rate_limit(*sess(r), r.params["gw"], r.body)), True),
        ("DELETE", f"{gateway}/rate-limits/{{rl}}", lambda r: (200, delete_rate_limit(*sess(r), r.params["gw"], r.params["rl"], r.body)), True),
        ("GET", f"{gateway}/resource-policy", lambda r: (200, resource_policy(*sess(r), r.params["gw"])), False),
        ("PUT", f"{gateway}/resource-policy", lambda r: (200, put_resource_policy(*sess(r), r.params["gw"], r.body)), True),
        ("DELETE", f"{gateway}/resource-policy", lambda r: (200, delete_resource_policy(*sess(r), r.params["gw"], r.body)), True),
        ("GET", f"{gateway}/decisions", lambda r: (200, decisions(*sess(r), r.params["gw"], r.query.get("hours") or 24, limit(r))), False),
        ("PUT", f"{gateway}/decision-log", lambda r: (200, enable_decision_log(*sess(r), r.params["gw"], r.body)), True),
        ("DELETE", f"{gateway}/decision-log", lambda r: (200, disable_decision_log(*sess(r), r.params["gw"], r.body)), True),
    ]
    for method, pattern, fn, admin in routes:
        router.add(method, pattern, guarded(fn), admin=admin)
