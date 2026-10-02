"""An AgentCore online evaluation for a deployed agent, from the evidence of its direct rehearsal.

The panel (``direct.panel``) says which built-in evaluators see this agent's designed failure modes and which
reward them. This turns its recommendation into a ``CreateOnlineEvaluationConfig`` request for one runtime:
the recommended built-ins, plus the pack's own THELMA evaluator (created by the Workshop's 08) when some
contrast is seen by none of them, over the runtime's log group at a sampling rate, results in a dedicated log
group. The execution role mirrors the one the agentcore SDK creates (trust bedrock-agentcore for evaluators and
online evaluation configs; log queries, the aws/spans index policy, model invocation). Nothing is created here:
:func:`plan` is pure; ``tools/online_eval.py`` applies it on request.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

SAMPLING_PERCENT = 10.0
SESSION_TIMEOUT_MINUTES = 15
#: The Workshop's code-based THELMA evaluator (08-create-evaluators.sh): ``<agent>_thelma_rag_quality-<suffix>``.
THELMA_PREFIX = "{agent}_thelma_rag_quality"


def service_name(runtime_id: str) -> str:
    """CloudWatch's service name of a runtime's DEFAULT endpoint (``<name>.DEFAULT``), as the CLI derives it."""
    return re.sub(r"-[^-]+$", ".DEFAULT", runtime_id)


def config_name(agent: str, kind: str) -> str:
    """``<agent>_<kind>_online_eval`` within the API's name rules (letters, digits, underscores; at most 48)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", f"{agent}_{kind}_online_eval")[:48]


def evaluators(panel: Mapping[str, Any], *, workshop_thelma: str | None) -> tuple[list[str], list[str]]:
    """``(evaluator ids, reasons)``: the panel's recommended built-ins in pick order, and the Workshop's THELMA
    when a designed contrast is seen by none of them."""
    picks = [str(r["evaluator"]) for r in panel.get("recommendation") or [] if r.get("recommended")]
    reasons = [f"{r['evaluator']}: sees {', '.join(r['sees'])}" for r in panel.get("recommendation") or [] if r.get("recommended")]
    unseen = list(panel.get("unseen") or [])
    if unseen and workshop_thelma:
        picks.append(workshop_thelma)
        reasons.append(f"{workshop_thelma}: the Workshop's judge, for {', '.join(unseen)} (no recommended built-in sees it)")
    elif unseen:
        reasons.append(f"not covered online: {', '.join(unseen)} (the Workshop's THELMA evaluator is not in this account)")
    return picks, reasons


def role_documents(account: str, region: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The execution role's trust and permission documents (the agentcore SDK's evaluation role)."""
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account, "aws:ResourceAccount": account},
                      "ArnLike": {"aws:SourceArn": [f"arn:aws:bedrock-agentcore:{region}:{account}:evaluator/*",
                                                    f"arn:aws:bedrock-agentcore:{region}:{account}:online-evaluation-config/*"]}}}]}
    policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "LogQueries", "Effect": "Allow", "Resource": "*",
         "Action": ["logs:DescribeLogGroups", "logs:DescribeLogStreams", "logs:GetQueryResults", "logs:StartQuery",
                    "cloudwatch:GenerateQuery", "cloudwatch:GenerateQueryResultsSummary"]},
        {"Sid": "ResultLogs", "Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:GetLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/evaluations/*"},
        {"Sid": "SpansIndex", "Effect": "Allow", "Action": ["logs:DescribeIndexPolicies", "logs:PutIndexPolicy"],
         "Resource": [f"arn:aws:logs:{region}:{account}:log-group:aws/spans", f"arn:aws:logs:{region}:{account}:log-group:aws/spans:*"]},
        {"Sid": "Judges", "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": "*"},
        {"Sid": "CodeEvaluators", "Effect": "Allow", "Action": ["lambda:GetFunction", "lambda:InvokeFunction"],
         "Resource": f"arn:aws:lambda:{region}:{account}:function:*"},
    ]}
    return trust, policy


def plan(panel: Mapping[str, Any], *, agent: str, kind: str, runtime_id: str, role_arn: str, workshop_thelma: str | None = None,
         sampling: float = SAMPLING_PERCENT, session_timeout: int = SESSION_TIMEOUT_MINUTES,
         tags: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The CreateOnlineEvaluationConfig request (``kind``: ``class`` for the Workshop's runtime, ``direct`` for the
    pack's direct Harness) and the reasons behind each evaluator."""
    if panel.get("references") is not False:  # True, or a panel from before the online-style scoring (no key)
        raise ValueError("this panel did not score as an online evaluation does (it had the golden expectations as reference "
                         "inputs, or predates that choice); an evaluator it recommends may raise false alarms online: rehearse "
                         "with --panel (no --panel-references) first")
    ids, reasons = evaluators(panel, workshop_thelma=workshop_thelma)
    if not ids:
        raise ValueError("the panel recommends no evaluator: run a direct rehearsal with --panel first")
    if not 0 < sampling <= 100:
        raise ValueError("sampling must be a percentage in (0, 100]")
    request = {
        "onlineEvaluationConfigName": config_name(agent, kind),
        "description": f"From the direct rehearsal's evaluator panel: the evaluators that see this agent's failure modes ({kind} runtime)",
        "rule": {"samplingConfig": {"samplingPercentage": float(sampling)}, "sessionConfig": {"sessionTimeoutMinutes": int(session_timeout)}},
        "dataSourceConfig": {"cloudWatchLogs": {"logGroupNames": [f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"],
                                                "serviceNames": [service_name(runtime_id)]}},
        "evaluators": [{"evaluatorId": e} for e in ids],
        "evaluationExecutionRoleArn": role_arn,
        "enableOnCreate": True,
    }
    if tags:
        request["tags"] = dict(tags)
    return {"request": request, "reasons": reasons}


def workshop_thelma_id(evaluator_ids: Sequence[str], agent: str) -> str | None:
    prefix = THELMA_PREFIX.format(agent=agent)
    return next((e for e in evaluator_ids if e.startswith(prefix)), None)


#: A score under this is a failure for the online summary (the built-ins' labels split there: "Somewhat Unhelpful"
#: is 0.33, "Incorrect" 0.0, a THELMA Fail under its 0.7 bar is caught by its own label).
FAIL_BELOW = 0.5
#: Evaluators whose value says what happened rather than how good it was: Builtin.Refusal is 1 for a refusal and
#: 0 for a normal answer, so the summary reports how often the agent refused, never a failure.
RATE_ONLY = frozenset({"Builtin.Refusal"})


def summarize(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The online evaluation's result records (``gen_ai.evaluation.result`` in its results log group) per
    evaluator and per session: counts, means, failure rates, and every failing (session, evaluator)."""
    by_eval: dict[str, list[float]] = {}
    sessions: dict[str, dict[str, Any]] = {}
    failing = []
    for record in results:
        a = record.get("attributes") or {}
        name, value, sid = a.get("gen_ai.evaluation.name"), a.get("gen_ai.evaluation.score.value"), a.get("session.id")
        if not name or not sid:
            continue
        label = str(a.get("gen_ai.evaluation.score.label") or "")
        entry = sessions.setdefault(str(sid), {"sessionId": str(sid), "traceId": record.get("traceId"), "at": record.get("timeUnixNano"),
                                               "scores": {}})
        entry["scores"][str(name)] = {"value": value, "label": label}
        if isinstance(value, (int, float)):
            by_eval.setdefault(str(name), []).append(float(value))
            failed = str(name) not in RATE_ONLY and (value < FAIL_BELOW or label.lower() == "fail")
        else:
            failed = False
        if failed:
            failing.append({"sessionId": str(sid), "traceId": record.get("traceId"), "evaluator": str(name), "value": value, "label": label,
                            "explanation": str(a.get("gen_ai.evaluation.explanation") or "")[:400]})
    evaluators = {name: {"count": len(vals), "mean": round(sum(vals) / len(vals), 3),
                         **({"refusalRate": round(sum(v >= 0.5 for v in vals) / len(vals), 3)} if name in RATE_ONLY else
                            {"failRate": round(sum(v < FAIL_BELOW for v in vals) / len(vals), 3)})} for name, vals in by_eval.items()}
    return {"sessions": len(sessions), "evaluators": evaluators, "failing": failing,
            "failingSessions": sorted({f["sessionId"] for f in failing}), "bySession": list(sessions.values())}


def user_text(records: Sequence[Mapping[str, Any]]) -> str:
    """The first user message among a session's runtime log records (the gen_ai input events), or ''."""
    for record in records:
        body = record.get("body")
        messages = (body.get("input") or {}).get("messages") if isinstance(body, dict) else None
        for message in messages or []:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, dict):
                content = content.get("content") or content.get("message") or content.get("text")
            if isinstance(content, str):
                try:  # the Harness logs the content as a JSON list of blocks
                    import json as _json

                    blocks = _json.loads(content)
                    content = blocks
                except ValueError:
                    return content.strip()
            if isinstance(content, list):
                text = " ".join(str(b.get("text") or "") for b in content if isinstance(b, dict)).strip()
                if text:
                    return text
    return ""


def render_results(summary: Mapping[str, Any], *, config: str, questions: Mapping[str, str]) -> str:
    lines = [f"# Online evaluation results: {config}", "", f"{summary['sessions']} session(s) evaluated.", "",
             "| Evaluator | Scored | Mean | Failing (< 0.5) |", "|---|---|---|---|"]
    lines += [f"| {name} | {e['count']} | {e['mean']} | " + (f"{e['failRate']:.0%}" if "failRate" in e else f"— (refused {e['refusalRate']:.0%})") + " |"
              for name, e in sorted(summary["evaluators"].items())]
    if summary["failingSessions"]:
        lines += ["", "## Failing sessions (candidates for new golden cases, for the SA to confirm)", ""]
        for sid in summary["failingSessions"]:
            said = [f"{f['evaluator']} {f['value']} ({f['label']})" for f in summary["failing"] if f["sessionId"] == sid]
            lines.append(f"- `{sid}`: {questions.get(sid) or '(question not found)'} — {'; '.join(said)}")
    return "\n".join(lines) + "\n"


# -- applying a plan, reading results, deleting (shared by tools/online_eval.py and the console) ----------------------

def existing_role(iam: Any, role: str) -> dict[str, Any] | None:
    """The role's GetRole (with its ARN and path), or None when there is none."""
    try:
        return dict(iam.get_role(RoleName=role)["Role"])
    except Exception as exc:  # noqa: BLE001
        if "NoSuchEntity" in str(exc):
            return None
        raise


#: The tag ``tools/online_eval.py`` gives its role and configuration (``direct`` or ``class``: the runtime evaluated).
ONLINE_EVAL_TAG = "adlc:online-eval"


def _ours(iam: Any, role: str) -> bool:
    """A role this platform made (``adlc:console=1`` or ``adlc:mode=direct``; or ``adlc:online-eval``, which is all
    ``tools/online_eval.py`` tagged its roles with before they carried the direct mode tag): only those are reused or
    deleted."""
    tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=role).get("Tags") or []}
    return tags.get("adlc:console") == "1" or tags.get("adlc:mode") == "direct" or tags.get(ONLINE_EVAL_TAG) in ("direct", "class")


def platform_refusal(iam: Any, role: str, found: Mapping[str, Any]) -> str | None:
    """Why an existing role may not be reused or deleted (None: it may), as ``tools/online_eval.py`` decides: a role
    this platform did not make. The console passes its own rule (``console.agents.refusal``: its IAM path too)."""
    return None if _ours(iam, role) else f"the role {role} exists and was not made by this platform: it is not reused (or changed)"


def apply_plan(session: Any, *, account: str, region: str, planned: Mapping[str, Any], role: str, sleep: Any = None,
               boundary: str | None = None, path: str = "/", refusal: Any = None) -> dict[str, Any]:
    """Create the execution role (or refresh its policy) and the configuration; returns the record to keep. With a
    permissions ``boundary`` (the console's spoke workspace) the role is created with it, and a role this platform made
    before without it gets it first (a spoke role changes only roles that carry it). A new role is created on ``path``
    (the console's roles have their own, ``/adlc-console/``); an existing one is reused only when ``refusal(iam, name,
    role)`` finds nothing against it (:func:`platform_refusal` unless given), and is never changed otherwise. The
    configuration is given the role's own ARN (CreateRole's or GetRole's, with its path), never one built from its name."""
    import json as _json
    import time as _time

    from .aws import client

    sleep = sleep or _time.sleep
    refuse = refusal or platform_refusal
    iam, ctl = client(session, "iam"), client(session, "bedrock-agentcore-control", region)
    trust, policy = role_documents(account, region)
    found = existing_role(iam, role)
    if found is None:
        arn = iam.create_role(RoleName=role, Path=path, AssumeRolePolicyDocument=_json.dumps(trust), Description="AgentCore online evaluation (ADLC)",
                              Tags=[{"Key": k, "Value": v} for k, v in (planned["request"].get("tags") or {}).items()],
                              **({"PermissionsBoundary": boundary} if boundary else {}))["Role"]["Arn"]
    else:
        why = refuse(iam, role, found)
        if why:
            raise ValueError(why)
        if boundary and ((found.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn")) != boundary:
            iam.put_role_permissions_boundary(RoleName=role, PermissionsBoundary=boundary)
        arn = found["Arn"]
    iam.put_role_policy(RoleName=role, PolicyName="online-evaluation", PolicyDocument=_json.dumps(policy))
    request = dict(planned["request"], evaluationExecutionRoleArn=str(arn))
    for attempt in range(8):  # a fresh role is refused for a few seconds
        try:
            created = ctl.create_online_evaluation_config(**request)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 7 or "role" not in str(exc).lower():
                raise
            sleep(10)
    runtime_group = request["dataSourceConfig"]["cloudWatchLogs"]["logGroupNames"][0]
    return {"configId": created.get("onlineEvaluationConfigId"), "arn": created.get("onlineEvaluationConfigArn"), "role": role,
            "roleArn": str(arn), "runtimeGroup": runtime_group, "evaluators": [e["evaluatorId"] for e in request["evaluators"]],
            "sampling": request["rule"]["samplingConfig"]["samplingPercentage"]}


def delete_config(session: Any, *, region: str, config_id: str | None, role: str | None, boundary: str | None = None,
                  refusal: Any = None, left: list[str] | None = None) -> list[str]:
    """Delete the configuration and its role; returns what was removed (with a permissions ``boundary``, a role made
    before it gets it first: a spoke role deletes a role's policies only while it carries it). A role ``refusal`` (as
    in :func:`apply_plan`) finds something against is left as it is, its reason appended to ``left`` when given."""
    from .aws import client

    done = []
    if config_id:
        client(session, "bedrock-agentcore-control", region).delete_online_evaluation_config(onlineEvaluationConfigId=config_id)
        done.append(config_id)
    iam = client(session, "iam")
    found = existing_role(iam, role) if role else None
    why = (refusal or platform_refusal)(iam, role, found) if found is not None else None
    if why and left is not None:
        left.append(why)
    if found is not None and not why:  # a role someone else made is left as it is
        if boundary and ((found.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn")) != boundary:
            iam.put_role_permissions_boundary(RoleName=role, PermissionsBoundary=boundary)
        for name in iam.list_role_policies(RoleName=role).get("PolicyNames") or []:
            iam.delete_role_policy(RoleName=role, PolicyName=name)
        iam.delete_role(RoleName=role)
        done.append(role)
    return done


def _records(logs: Any, group: str, since_ms: int, pattern: str | None = None) -> list[dict[str, Any]]:
    import json as _json

    kwargs: dict[str, Any] = {"logGroupName": group, "startTime": since_ms}
    if pattern:
        kwargs["filterPattern"] = pattern
    out = []
    for page in logs.get_paginator("filter_log_events").paginate(**kwargs):
        for event in page.get("events") or []:
            try:
                record = _json.loads(event.get("message") or "")
            except ValueError:
                continue
            if isinstance(record, dict):
                out.append(record)
    return out


def fetch_results(session: Any, *, region: str, config_id: str, runtime_group: str, hours: float = 24) -> dict[str, Any]:
    """The configuration's results summarized, with the failing sessions' questions from the runtime's logs."""
    import time as _time

    from .aws import client

    logs = client(session, "logs", region)
    since = int((_time.time() - float(hours) * 3600) * 1000)
    try:
        found = _records(logs, f"/aws/bedrock-agentcore/evaluations/results/{config_id}", since)
    except Exception as exc:  # noqa: BLE001 - no result yet: the group is created with the first one
        if "ResourceNotFound" not in str(exc):
            raise
        found = []
    summary = summarize(found)
    questions = {sid: user_text(_records(logs, runtime_group, since, f'"{sid}"')) for sid in summary["failingSessions"]}
    return {**summary, "questions": questions, "configId": config_id}
