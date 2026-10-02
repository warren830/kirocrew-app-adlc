"""Agents in a workspace: every Harness and AgentCore Runtime, a declarative Harness created from the console, and
the invoke chain the chat and the public API share.

Listing never changes anything. :func:`create_harness` creates the Harness and, unless one is given, its execution
role (``<name>-console-harness``: model invocation, the named Gateways, the memory, skills read from S3, the image
pull and logs, as the Workshop's CDK role grants). Everything the console creates is tagged ``adlc:console=1`` and
only those may be deleted from it. In a workspace with a permissions boundary (``permissionsBoundaryArn``, the spoke
stack's) every role the console creates carries it, and a console role made before gets it before its policy changes
(:func:`ensure_boundary`, used by every module that creates or changes a console role). :func:`invoke` streams one
turn as events — ``text`` deltas, ``tool`` calls, ``stop`` with the usage — for a Harness (``InvokeHarness``, the
session kept by ``runtimeSessionId``) or a Runtime (``InvokeAgentRuntime``, its payload passed through).

Every role the console creates is on its own IAM path, :data:`ROLE_PATH` (``/adlc-console/``; the names are as they
were: a role name is unique in its account whatever the path), and the spoke role creates, changes, tags, passes and
deletes roles on that path only. A role of the console's name that exists already is adopted only when
:func:`refusal` finds nothing against it (:func:`adopt`, used by every module before it changes, passes or deletes one
of its roles): on the path and tagged ``adlc:console=1``, or — in a workspace without a permissions boundary — a
console role made before roles had a path (on ``/``, tagged). Anything else is refused and left as it is. The ARN
given to AgentCore is always the role's own (CreateRole's or GetRole's, with its path), never one built from a name.
"""
from __future__ import annotations

import codecs
import json
import re
import time
import uuid
from typing import Any, Iterator, Mapping, Sequence

from ..direct.aws import HARNESS_ENVIRONMENT, HARNESS_IMAGE_ACCOUNT, HARNESS_LIMITS, client

CONSOLE_TAG = {"adlc:console": "1"}
#: The IAM path of every role the console creates (``arn:aws:iam::<account>:role/adlc-console/<name>``): the spoke role
#: (``app/console/spoke-role.yaml``) creates, changes, tags, passes and deletes roles on this path only, so a role
#: someone else made is out of its reach whatever its name.
ROLE_PATH = "/adlc-console/"
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")  # Harness names: letters, digits, underscores
SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{32,99}$")


class AgentError(ValueError):
    pass


def _pages(call: Any, key: str, **kwargs: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while True:
        page = call(**kwargs)
        out += page.get(key) or []
        if not page.get("nextToken"):
            return out
        kwargs["nextToken"] = page["nextToken"]


def list_agents(session: Any, region: str) -> list[dict[str, Any]]:
    """Harnesses first (each with the runtime that serves it), then the Runtimes no Harness owns."""
    ctl = client(session, "bedrock-agentcore-control", region)
    harnesses = _pages(ctl.list_harnesses, "harnesses")
    runtimes = _pages(ctl.list_agent_runtimes, "agentRuntimes")
    owned = {f"harness_{h.get('harnessName')}" for h in harnesses}
    out = [{"kind": "harness", "name": h.get("harnessName"), "id": h.get("harnessId"), "arn": h.get("arn"), "status": h.get("status"),
            "updatedAt": str(h.get("updatedAt") or h.get("createdAt") or "")} for h in harnesses]
    out += [{"kind": "runtime", "name": r.get("agentRuntimeName"), "id": r.get("agentRuntimeId"), "arn": r.get("agentRuntimeArn"),
             "status": r.get("status"), "updatedAt": str(r.get("lastUpdatedAt") or "")} for r in runtimes if r.get("agentRuntimeName") not in owned]
    return out


def get_agent(session: Any, region: str, kind: str, ident: str) -> dict[str, Any]:
    ctl = client(session, "bedrock-agentcore-control", region)
    if kind == "harness":
        h = ctl.get_harness(harnessId=ident)["harness"]
        runtime = (h.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {}
        prompt = " ".join(str(b.get("text") or "") for b in h.get("systemPrompt") or [])
        tags = ctl.list_tags_for_resource(resourceArn=h["arn"]).get("tags") or {}
        return {"kind": "harness", "name": h.get("harnessName"), "id": h.get("harnessId"), "arn": h.get("arn"), "status": h.get("status"),
                "model": ((h.get("model") or {}).get("bedrockModelConfig") or {}).get("modelId"), "systemPrompt": prompt,
                "tools": [{"type": t.get("type"), "name": t.get("name")} for t in h.get("tools") or []], "allowedTools": h.get("allowedTools") or [],
                "skills": h.get("skills") or [], "memory": (h.get("memory") or {}).get("agentCoreMemoryConfiguration"),
                "limits": {k: h.get(k) for k in ("maxIterations", "maxTokens", "timeoutSeconds")}, "runtimeId": runtime.get("agentRuntimeId"),
                "runtimeArn": runtime.get("agentRuntimeArn"), "executionRoleArn": h.get("executionRoleArn"), "tags": tags,
                "console": tags.get("adlc:console") == "1"}
    if kind == "runtime":
        r = ctl.get_agent_runtime(agentRuntimeId=ident)
        return {"kind": "runtime", "name": r.get("agentRuntimeName"), "id": r.get("agentRuntimeId"), "arn": r.get("agentRuntimeArn"),
                "status": r.get("status"), "version": r.get("agentRuntimeVersion"), "artifact": r.get("agentRuntimeArtifact"),
                "network": r.get("networkConfiguration"), "protocol": r.get("protocolConfiguration"), "roleArn": r.get("roleArn"),
                "runtimeId": r.get("agentRuntimeId"), "runtimeArn": r.get("agentRuntimeArn"), "console": False}
    raise AgentError("kind must be harness or runtime")


def role_policy(*, account: str, region: str, gateways: Sequence[str], memory_arn: str | None, skill_buckets: Sequence[str],
                harness_name: str = "") -> dict[str, Any]:
    """A Harness with no memory of its own gets one the service manages (``memory/<harness name>-<suffix>``), created
    with the Harness, so its ARN is unknown here: the memory grant covers that name as well as any memory given."""
    statements: list[dict[str, Any]] = [
        {"Sid": "Models", "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": ["arn:aws:bedrock:*::foundation-model/*", f"arn:aws:bedrock:{region}:{account}:*", "arn:aws:bedrock:*:*:inference-profile/*"]},
        {"Sid": "Image", "Effect": "Allow", "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
         "Resource": f"arn:aws:ecr:{region}:{HARNESS_IMAGE_ACCOUNT}:repository/*"},
        {"Sid": "ImageToken", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
        {"Sid": "Logs", "Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/*"},
        {"Sid": "Telemetry", "Effect": "Allow", "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "cloudwatch:PutMetricData"], "Resource": "*"},
    ]
    if gateways:
        statements.append({"Sid": "Gateways", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway", "Resource": list(gateways)})
    memories = ([memory_arn] if memory_arn else []) + ([f"arn:aws:bedrock-agentcore:{region}:{account}:memory/{harness_name}-*"] if harness_name else [])
    if memories:
        statements.append({"Sid": "Memory", "Effect": "Allow", "Resource": memories, "Action": [
            "bedrock-agentcore:CreateEvent", "bedrock-agentcore:DeleteEvent", "bedrock-agentcore:GetEvent", "bedrock-agentcore:GetMemory",
            "bedrock-agentcore:GetMemoryRecord", "bedrock-agentcore:ListActors", "bedrock-agentcore:ListEvents", "bedrock-agentcore:ListMemoryRecords",
            "bedrock-agentcore:ListSessions", "bedrock-agentcore:RetrieveMemoryRecords", "bedrock-agentcore:DeleteMemoryRecord"]})
    statements.append({"Sid": "WorkloadIdentity", "Effect": "Allow", "Action": [
        "bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT", "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
        "Resource": [f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default",
                     f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/*"]})
    if skill_buckets:
        statements.append({"Sid": "Skills", "Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"],
                           "Resource": [arn for b in skill_buckets for arn in (f"arn:aws:s3:::{b}", f"arn:aws:s3:::{b}/*")]})
    return {"Version": "2012-10-17", "Statement": statements}


def boundary_args(boundary: str | None) -> dict[str, Any]:
    """CreateRole's ``PermissionsBoundary`` when the workspace has one."""
    return {"PermissionsBoundary": boundary} if boundary else {}


def role_args(boundary: str | None) -> dict[str, Any]:
    """CreateRole's ``Path`` (the console's own, :data:`ROLE_PATH`) and, when the workspace has one, its
    ``PermissionsBoundary``: every module that creates a role passes these."""
    return {"Path": ROLE_PATH, **boundary_args(boundary)}


def role_tags(iam: Any, name: str) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=name).get("Tags") or []}


def refusal(name: str, role: Mapping[str, Any], tags: Mapping[str, str], boundary: str | None) -> str | None:
    """Why the console may not adopt the existing role ``name`` — change its policies, trust or boundary, pass it to a
    service, delete it — or None when it may: a role on the console's path tagged ``adlc:console=1``, or, in a
    workspace without a permissions boundary (a local profile in the same account), a console role made before the
    console's roles had a path (on ``/``, tagged ``adlc:console=1``). A workspace with a boundary (a spoke role's)
    takes over nothing off the path: the spoke role may not change such a role, and a role someone else made whose
    name happens to be the console's must never become the console's."""
    path = str(role.get("Path") or "/")
    if tags.get("adlc:console") != "1":
        return (f"the role {name} exists and is not the console's (it is not tagged adlc:console=1): it is left as it is. A role name is "
                "unique in its account: the account's owner can remove that role, or the console's resource needs another name")
    if path == ROLE_PATH or (path == "/" and not boundary):
        return None
    if path == "/":
        return (f"the role {name} is tagged adlc:console=1 but is on the path /, not the console's {ROLE_PATH} (a console role made before "
                f"the console's roles had their own path): a workspace with a permissions boundary adopts only roles on {ROLE_PATH}, so it is "
                f"left as it is. The account's owner can delete it; the console then creates it again on {ROLE_PATH}")
    return f"the role {name} is on the path {path}, not the console's {ROLE_PATH}: it is left as it is"


def adopt(iam: Any, name: str, boundary: str | None, role: Mapping[str, Any] | None = None, *,
          error: type[Exception] = AgentError) -> tuple[dict[str, Any], dict[str, str]]:
    """The existing role ``name`` (its GetRole, or ``role``) and its tags, when the console may adopt it
    (:func:`refusal`); otherwise ``error`` with the reason, raised before anything is changed."""
    found = dict(role) if role is not None else iam.get_role(RoleName=name)["Role"]
    tags = role_tags(iam, name)
    why = refusal(name, found, tags, boundary)
    if why:
        raise error(why)
    return found, tags


def ensure_boundary(iam: Any, name: str, boundary: str | None, role: Mapping[str, Any] | None = None,
                    tags: Mapping[str, str] | None = None) -> bool:
    """A console role — on the console's path and tagged ``adlc:console=1`` — without the workspace's permissions
    boundary gets it (the spoke role changes only roles that carry it, and lets this boundary, no other, be put on one):
    a role made before the workspace had a boundary keeps working. Any other role is left as it is, even one its owner
    tagged ``adlc:console=1`` so the console may add a grant to it: the boundary would cap it for good. True when the
    boundary was set now."""
    if not boundary:
        return False
    found = dict(role) if role is not None else iam.get_role(RoleName=name)["Role"]
    if ((found.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn")) == boundary:
        return False
    if str(found.get("Path") or "/") != ROLE_PATH:
        return False
    if (dict(tags) if tags is not None else role_tags(iam, name)).get("adlc:console") != "1":
        return False
    iam.put_role_permissions_boundary(RoleName=name, PermissionsBoundary=boundary)
    return True


def passable(iam: Any, arn: str, *, account: str, boundary: str | None, error: type[Exception] = AgentError,
             what: str = "executionRoleArn") -> str:
    """An existing role about to be passed to a service (a Harness's ``executionRoleArn``, an agent's role for its A/B
    copy, production's for a canary), checked before anything is created. In a workspace with a permissions boundary the
    spoke role passes only roles on the console's path tagged ``adlc:console=1`` (PassRole takes no boundary key): such
    a role gets the workspace's boundary first when it lacks it, and is passed by its own ARN (GetRole's, with its path);
    anything else is refused here with ``error``. Elsewhere the ARN is passed as given."""
    if not boundary:
        return arn
    parts = str(arn or "").strip().split(":")
    if len(parts) != 6 or parts[2] != "iam" or parts[4] != account or not parts[5].startswith("role/"):
        raise error(f"{what}: {arn or '(none)'} is not a role of this workspace's account {account}")
    name = parts[5].rsplit("/", 1)[-1]
    try:
        found = iam.get_role(RoleName=name)["Role"]
    except Exception as exc:  # noqa: BLE001
        if "NoSuchEntity" in str(exc):
            raise error(f"{what}: no role {name} in this account") from exc
        raise
    tags = role_tags(iam, name)
    if str(found.get("Path") or "/") != ROLE_PATH or tags.get("adlc:console") != "1":
        raise error(f"{what}: a workspace with a permissions boundary passes only the console's own roles, on its path {ROLE_PATH} and tagged "
                    f"adlc:console=1 (the spoke role passes no other): {found.get('Arn') or name} is on {found.get('Path') or '/'}, tagged "
                    f"adlc:console={tags.get('adlc:console', '(none)')}")
    try:
        ensure_boundary(iam, name, boundary, found, tags)  # never passed unbounded: a console role made before gets the boundary now
    except Exception as exc:  # noqa: BLE001
        if "AccessDenied" not in str(exc):
            raise
        raise error(f"{what}: the role {name} does not carry the workspace's permissions boundary and could not be given it: {str(exc)[:200]}") from exc
    return str(found["Arn"])


def _ensure_role(session: Any, name: str, account: str, region: str, policy: Mapping[str, Any], *, boundary: str | None = None) -> str:
    """The Harness's role, created on the console's path (with the workspace's boundary) or adopted (:func:`adopt`);
    its ARN as IAM gives it."""
    iam = client(session, "iam")
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                                                      "Action": "sts:AssumeRole", "Condition": {
                                                          "StringEquals": {"aws:SourceAccount": account},
                                                          "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:*"}}}]}
    try:
        role = iam.get_role(RoleName=name)["Role"]
    except Exception as exc:  # noqa: BLE001
        if "NoSuchEntity" not in str(exc):
            raise
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Description="ADLC console Harness role",
                              Tags=[{"Key": k, "Value": v} for k, v in CONSOLE_TAG.items()], **role_args(boundary))["Role"]["Arn"]
        time.sleep(10)  # IAM propagation: a fresh role is refused by CreateHarness for a few seconds
    else:
        role, tags = adopt(iam, name, boundary, role)
        arn = role["Arn"]
        ensure_boundary(iam, name, boundary, role, tags)
    iam.put_role_policy(RoleName=name, PolicyName="harness-execution", PolicyDocument=json.dumps(policy))
    return arn


def put_inline_policy(session: Any, role: str, name: str, document: Mapping[str, Any], *, what: str, boundary: str | None = None) -> None:
    """Add a console grant to an agent's role. Through a spoke role only roles tagged ``adlc:console=1`` that carry
    the workspace's permissions boundary may be changed (``app/console/spoke-role.yaml``; a console role without it
    gets it first when ``boundary`` is given): another role's refusal says exactly what its owner should add."""
    role = str(role).rsplit("/", 1)[-1]
    iam = client(session, "iam")
    try:
        if boundary:
            ensure_boundary(iam, role, boundary)
        iam.put_role_policy(RoleName=role, PolicyName=name, PolicyDocument=json.dumps(document))
    except Exception as exc:  # noqa: BLE001
        if "AccessDenied" not in str(exc):
            raise
        raise AgentError(f"this workspace may not change the role {role} ({what}): a spoke role changes only roles tagged adlc:console=1 "
                         "that carry the console's permissions boundary; ask the account's owner to tag it adlc:console=1 and give it the "
                         f"boundary, or to add this inline policy {name} to it: {json.dumps(document, ensure_ascii=False)}") from exc


def role_name(harness_name: str) -> str:
    """``<name>-console-harness`` (at most 64): a name over 44 characters keeps 35 and a hash of the whole, so two
    long names never share one role (the second create would overwrite the first's policy)."""
    if len(harness_name) <= 44:
        return f"{harness_name}-console-harness"
    import hashlib

    return f"{harness_name[:35]}-{hashlib.sha256(harness_name.encode('utf-8')).hexdigest()[:8]}-console-harness"


def check_turn(message: str, session_id: str | None) -> str:
    """A turn's message and session id, checked before any stream starts (a refusal inside a started stream would
    reach the browser as a 200 with an error page in its body)."""
    if not message.strip() or len(message) > 20_000:
        raise AgentError("message: 1-20000 characters")
    sid = session_id or new_session()
    if not SESSION.match(sid):
        raise AgentError("sessionId: 33-100 letters, digits, '-' or '_'")
    return sid


def harness_request(body: Mapping[str, Any]) -> dict[str, Any]:
    """The CreateHarness fields from the console's form (validated): name, model, systemPrompt, gateways, skills, memory."""
    name = str(body.get("name") or "")
    if not NAME.match(name):
        raise AgentError("name: a letter, then letters, digits or underscores (at most 48)")
    model = str(body.get("model") or "").strip()
    if not re.match(r"^[a-z0-9][a-z0-9.:_\-/]{2,199}$", model):
        raise AgentError("model must be a Bedrock model or inference profile id")
    prompt = str(body.get("systemPrompt") or "").strip()
    if not prompt or len(prompt) > 100_000:
        raise AgentError("systemPrompt is required (at most 100 000 characters)")
    tools = []
    for g in body.get("gateways") or []:
        arn, target = str((g or {}).get("arn") or ""), str((g or {}).get("name") or "")
        if not arn.startswith("arn:aws:bedrock-agentcore:") or not re.match(r"^[A-Za-z0-9]{1,64}$", target):
            raise AgentError("each gateway needs its ARN and a tool name (letters and digits)")
        tools.append({"type": "agentcore_gateway", "name": target, "config": {"agentCoreGateway": {"gatewayArn": arn}}})
    skills = []
    for uri in body.get("skills") or []:
        if not (isinstance(uri, str) and uri.startswith("s3://") and uri.endswith("/")):
            raise AgentError("each skill is an s3://bucket/prefix/ holding its SKILL.md")
        skills.append({"s3": {"uri": uri}})
    request: dict[str, Any] = {"harnessName": name, "model": {"bedrockModelConfig": {"modelId": model}}, "systemPrompt": [{"text": prompt}],
                               "tools": tools, "allowedTools": [f"@{t['name']}/*" for t in tools], "skills": skills,
                               "environmentVariables": dict(HARNESS_ENVIRONMENT), **HARNESS_LIMITS}
    memory = str(body.get("memoryArn") or "").strip()
    if memory:
        request["memory"] = {"agentCoreMemoryConfiguration": {"arn": memory, "actorId": "{actorId}"}}
    return request


def create_harness(session: Any, *, account: str, region: str, body: Mapping[str, Any], boundary: str | None = None) -> dict[str, Any]:
    """The Harness and, unless ``executionRoleArn`` is given, its role (on the console's path, with the workspace's
    permissions boundary). A given role is checked first in a workspace with a boundary (:func:`passable`)."""
    request = harness_request(body)
    role = str(body.get("executionRoleArn") or "").strip()
    if role and boundary:
        role = passable(client(session, "iam"), role, account=account, boundary=boundary)
    elif not role:
        gateways = [t["config"]["agentCoreGateway"]["gatewayArn"] for t in request["tools"]]
        buckets = sorted({s["s3"]["uri"][5:].split("/", 1)[0] for s in request["skills"]})
        memory = (request.get("memory") or {}).get("agentCoreMemoryConfiguration", {}).get("arn")
        role = _ensure_role(session, role_name(request["harnessName"]), account, region,
                            role_policy(account=account, region=region, gateways=gateways, memory_arn=memory, skill_buckets=buckets,
                                        harness_name=request["harnessName"]), boundary=boundary)
    ctl = client(session, "bedrock-agentcore-control", region)
    for attempt in range(6):
        try:
            created = ctl.create_harness(executionRoleArn=role, tags=dict(CONSOLE_TAG), **request)["harness"]
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 5 or not any(code in str(exc) for code in ("AccessDenied", "ValidationException")):
                raise
            time.sleep(10)
    return {"id": created["harnessId"], "arn": created["arn"], "name": request["harnessName"], "status": created.get("status"), "role": role}


def update_harness(session: Any, *, region: str, ident: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Change a Harness's prompt and/or model. A Harness the console did not create changes only with
    ``acknowledged: true``; a new model keeps the rest of the model configuration (max tokens, temperature, …)."""
    if not body.get("systemPrompt") and not body.get("model"):
        raise AgentError("give systemPrompt and/or model")
    ctl = client(session, "bedrock-agentcore-control", region)
    current = ctl.get_harness(harnessId=ident)["harness"]
    tags = ctl.list_tags_for_resource(resourceArn=current["arn"]).get("tags") or {}
    if tags.get("adlc:console") != "1" and body.get("acknowledged") is not True:
        raise AgentError(f"{current.get('harnessName')} was not created from this console: send acknowledged: true to change it")
    changes: dict[str, Any] = {}  # UpdateHarness: lists and the model structure as they are; only memory is wrapped in optionalValue
    if body.get("systemPrompt"):
        changes["systemPrompt"] = [{"text": str(body["systemPrompt"])}]
    if body.get("model"):
        if not re.match(r"^[a-z0-9][a-z0-9.:_\-/]{2,199}$", str(body["model"])):
            raise AgentError("model must be a Bedrock model or inference profile id")
        kept = dict(((current.get("model") or {}).get("bedrockModelConfig") or {}))
        changes["model"] = {"bedrockModelConfig": {**kept, "modelId": str(body["model"])}}
    out = ctl.update_harness(harnessId=ident, **changes)
    return {"id": ident, "status": (out.get("harness") or {}).get("status")}


def delete_agent(session: Any, *, region: str, ident: str) -> dict[str, Any]:
    """Delete a Harness the console created (tag ``adlc:console=1``); anything else is refused."""
    ctl = client(session, "bedrock-agentcore-control", region)
    h = ctl.get_harness(harnessId=ident)["harness"]
    tags = ctl.list_tags_for_resource(resourceArn=h["arn"]).get("tags") or {}
    if tags.get("adlc:console") != "1":
        raise AgentError("only agents created from this console can be deleted here")
    for attempt in range(13):  # a Harness still UPDATING refuses deletion (ConflictException, live): wait it out
        try:
            ctl.delete_harness(harnessId=ident)
            return {"id": ident, "deleted": True}
        except Exception as exc:  # noqa: BLE001
            if "Conflict" not in str(exc) or attempt == 12:
                raise
            time.sleep(10)
    raise AgentError(f"{ident} is still changing: delete it again in a minute")


def new_session() -> str:
    return f"console-{uuid.uuid4().hex}-{int(time.time())}"


def invoke(session: Any, *, region: str, agent: Mapping[str, Any], message: str, session_id: str | None, actor: str,
           prompt: str | None = None, model: str | None = None, skills: Sequence[Mapping[str, Any]] | None = None,
           data: Any = None) -> Iterator[dict[str, Any]]:
    """One turn as events: ``{"type": "session"}`` first, then ``text`` / ``tool`` / ``toolInput`` / ``stop`` / ``error``.

    ``prompt``, ``model`` and ``skills`` override the Harness's own for this turn only. ``data`` is a data-plane client
    to reuse (a boto3 session must not make clients from several threads at once)."""
    sid = check_turn(message, session_id)
    yield {"type": "session", "sessionId": sid}
    data = data or client(session, "bedrock-agentcore", region)
    started = time.monotonic()
    try:
        if agent["kind"] == "harness":
            extra: dict[str, Any] = {}
            if prompt:
                extra["systemPrompt"] = [{"text": prompt}]
            if model:
                extra["model"] = {"bedrockModelConfig": {"modelId": model}}
            if skills is not None:
                extra["skills"] = [dict(s) for s in skills]
            response = data.invoke_harness(harnessArn=agent["arn"], runtimeSessionId=sid, actorId=actor,
                                           messages=[{"role": "user", "content": [{"text": message}]}], **extra)
            usage = {"inputTokens": 0, "outputTokens": 0}
            pending: dict[int, list[str]] = {}  # a tool call's input, streamed in pieces by content block
            for event in response["stream"]:
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"].get("delta") or {}
                    if delta.get("text"):
                        yield {"type": "text", "text": delta["text"]}
                    elif "toolUse" in delta:
                        index = int(event["contentBlockDelta"].get("contentBlockIndex") or 0)
                        if index in pending:
                            pending[index][1] += str((delta.get("toolUse") or {}).get("input") or "")
                elif "contentBlockStart" in event:
                    tool = ((event["contentBlockStart"].get("start") or {}).get("toolUse") or {}).get("name")
                    if tool:
                        pending[int(event["contentBlockStart"].get("contentBlockIndex") or 0)] = [tool, ""]
                        yield {"type": "tool", "name": tool}
                elif "contentBlockStop" in event:
                    done = pending.pop(int(event["contentBlockStop"].get("contentBlockIndex") or 0), None)
                    if done:
                        try:
                            arguments = json.loads(done[1]) if done[1] else {}
                        except ValueError:
                            arguments = done[1]
                        yield {"type": "toolInput", "name": done[0], "input": arguments}
                elif "metadata" in event:
                    u = (event["metadata"].get("usage") or {})
                    usage = {k: usage[k] + int(u.get(k) or 0) for k in usage}
                elif "messageStop" in event:
                    yield {"type": "turn", "stopReason": event["messageStop"].get("stopReason")}
                else:
                    for key, value in event.items():
                        if key.endswith("Exception") or key == "error":
                            yield {"type": "error", "error": f"{key}: {str((value or {}).get('message') or value)[:300]}"}
            yield {"type": "stop", "seconds": round(time.monotonic() - started, 1), **usage}
        else:
            response = data.invoke_agent_runtime(agentRuntimeArn=agent["arn"], runtimeSessionId=sid,
                                                 payload=json.dumps({"prompt": message, "actorId": actor}).encode("utf-8"))
            body = response.get("response")
            chunks = body.iter_chunks() if hasattr(body, "iter_chunks") else [body.read()] if hasattr(body, "read") else [body or b""]
            decoder = codecs.getincrementaldecoder("utf-8")("replace")  # a character may be split between two chunks
            for chunk in chunks:
                text = decoder.decode(chunk) if isinstance(chunk, bytes) else str(chunk or "")
                if text:
                    yield {"type": "text", "text": text}
            tail = decoder.decode(b"", final=True)
            if tail:
                yield {"type": "text", "text": tail}
            yield {"type": "stop", "seconds": round(time.monotonic() - started, 1)}
    except Exception as exc:  # noqa: BLE001 - shown in the chat
        yield {"type": "error", "error": f"{type(exc).__name__}: {str(exc)[:400]}"}
