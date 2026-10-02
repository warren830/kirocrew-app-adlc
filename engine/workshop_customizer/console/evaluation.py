"""The console's evaluation hub: contract sets, verifications, AgentCore datasets, custom judges, batch and online
evaluation.

* **Contract sets** — behaviour contracts (``direct.verify``) kept by the console, written by hand or imported from a
  workshop pack's practice set; one becomes an AgentCore **dataset** on request (``CreateDataset``, one example per
  contract: the question as the turn's input, the expectations as assertions and the expected tools).
* **Verifications** — ``direct.verify.Verify`` as a console job: N rounds of a contract set on an agent, L1, and
  AgentCore's evaluators read against L1; its reading feeds an **online evaluation** (plan, create, results,
  delete; ``direct.online``).
* **Custom judges** — LLM-as-a-judge evaluators (``CreateEvaluator``: instructions, a rating scale, the judge model).
* **Batch evaluation** — ``StartBatchEvaluation`` over an agent's recent sessions with chosen evaluators and the
  failure-analysis insights (``Builtin.Insight.*``): per-evaluator averages, failure clusters with root causes, user
  intents and execution summaries.
* **Recommendations** — AgentCore's ``StartRecommendation``: a rewritten system prompt (or tool descriptions) from the
  agent's own traces, scored by the evaluators named, read from its logs, an online evaluation or a batch evaluation.
  Launchpad applies one with an accept click; here **adopting** makes the recommended prompt a configuration bundle's
  treatment and verifies it on the agent per call against a contract set, so it reaches an A/B test (and the gated
  promotion) only with evidence. Live 2026-10-01 its prompt-attack protection refused two benign Chinese system
  prompts within 9 s ("detected as unsafe by prompt attack protection"), while the same agent's traces with the prompt
  in English gave a recommendation in 6.5 minutes; so a CJK prompt is translated to English for the analysis, the
  recommendation is translated back, and the translation back is what is adopted (and then verified).

Everything the console creates is tagged ``adlc:console=1``; only those can be deleted here.
"""
from __future__ import annotations

import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..direct import online, verify
from ..direct.aws import client
from ..direct.panel import DEFAULT_PANEL
from .agents import CONSOLE_TAG, ROLE_PATH, get_agent, refusal, role_tags

NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
#: The insights of a batch (Launchpad's agentcore_eval.INSIGHT_TYPES): failure clusters with root causes, user intents,
#: execution summaries. A bare "Builtin.Insight" fails every session.
INSIGHTS = ("Builtin.Insight.FailureAnalysis", "Builtin.Insight.UserIntent", "Builtin.Insight.ExecutionSummary")


class EvaluationError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- contract sets ------------------------------------------------------------------------------------------------

def put_contract_set(store: Any, workspace: str, body: Mapping[str, Any]) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 80:
        raise EvaluationError("name: 1-80 characters")
    contracts = verify.check_contracts(body.get("contracts") or [])
    sid = str(body.get("id") or f"cs-{secrets.token_hex(4)}")
    if not re.match(r"^cs-[a-z0-9-]{1,60}$", sid):
        raise EvaluationError("id: cs- followed by lowercase letters, digits or hyphens")
    record = {"id": sid, "workspace": workspace, "name": name, "contracts": contracts, "l1": dict(body.get("l1") or {}),
              "source": str(body.get("source") or "manual"), "updatedAt": _now()}

    def change(all_: dict[str, Any]) -> dict[str, Any]:
        prior = all_.get(sid)
        if prior and prior.get("workspace") != workspace:  # ids are global: never write over another workspace's set
            raise EvaluationError(f"the contract set id {sid} belongs to another workspace")
        all_[sid] = {**record, "createdAt": (prior or {}).get("createdAt") or _now()}
        return all_

    store.update("contract_sets", {}, change)
    return record


def contract_sets(store: Any, workspace: str) -> list[dict[str, Any]]:
    return [{**{k: v for k, v in s.items() if k != "contracts"}, "count": len(s["contracts"])}
            for s in sorted(store.read("contract_sets", {}).values(), key=lambda s: s["updatedAt"], reverse=True) if s["workspace"] == workspace]


def contract_set(store: Any, workspace: str, sid: str) -> dict[str, Any]:
    found = store.read("contract_sets", {}).get(sid)
    if not found or found["workspace"] != workspace:
        raise EvaluationError(f"no contract set {sid}")
    return found


def packs(workshop_data: Path) -> list[dict[str, Any]]:
    """The Workshop projects with a built release, whose practice set can become a contract set: read here, so a member
    sees them without the Workshop App (which acts with the host's AWS profiles and is an admin's)."""
    root = Path(workshop_data) / "projects"
    found = []
    for pdir in sorted(root.iterdir()) if root.is_dir() else []:
        if (pdir / "build" / "release" / "pack" / "golden" / "practice.json").is_file() and re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", pdir.name):
            found.append({"id": pdir.name, "name": pdir.name})
    return found


def import_from_pack(store: Any, workspace: str, workshop_data: Path, project: str) -> dict[str, Any]:
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", project or ""):
        raise EvaluationError("project: a Workshop project id")
    pdir = Path(workshop_data) / "projects" / project
    if not (pdir / "build" / "release" / "pack" / "golden" / "practice.json").is_file():
        raise EvaluationError(f"workshop project {project} has no built release")
    cases, l1cfg = verify.contracts_from_pack(pdir)
    sets = store.read("contract_sets", {})
    legacy = f"cs-pack-{project}"[:40]  # the id before it could be 63 characters long: the same import updates that set
    sid = legacy if (sets.get(legacy) or {}).get("workspace") == workspace else f"cs-pack-{project}"[:63]
    taken = (sets.get(sid) or {}).get("workspace")
    if taken and taken != workspace:  # the same pack imported in another workspace keeps its own set
        sid = f"cs-pack-{workspace}-{project}"[:63].rstrip("-")
    return put_contract_set(store, workspace, {"name": f"{project} practice", "contracts": cases, "l1": l1cfg, "source": f"pack:{project}",
                                               "id": sid})


def dataset_examples(contracts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One AgentCore dataset example per contract (the CLI's dataset scenario: id, turns, assertions, expected tools)."""
    examples = []
    for c in contracts:
        refs = online_free_references(c)
        example: dict[str, Any] = {"scenario_id": c["id"], "turns": [{"input": c["query"]}]}  # the service's schema is snake_case
        if refs.get("assertions"):
            example["assertions"] = refs["assertions"]
        if refs.get("expectedTrajectory"):
            example["expected_trajectory"] = refs["expectedTrajectory"]
        examples.append(example)
    return examples


def online_free_references(contract: Mapping[str, Any]) -> dict[str, Any]:
    """The contract's expectations as text assertions and an expected tool list (no gateway prefix: a dataset is
    agent-agnostic)."""
    from ..direct.panel import references

    refs = references({"expected": contract["expected"]}, "dataset", "")
    if not refs:
        return {}
    ref = refs[0]
    return {"assertions": [a["text"] for a in ref.get("assertions") or []],
            "expectedTrajectory": list((ref.get("expectedTrajectory") or {}).get("toolNames") or [])}


def create_dataset(session: Any, region: str, record: Mapping[str, Any], name: str | None = None) -> dict[str, Any]:
    dname = name or re.sub(r"[^A-Za-z0-9_]", "_", f"adlc_{record['name']}")[:48]
    if not NAME.match(dname):
        raise EvaluationError("dataset name: a letter, then letters, digits or underscores")
    ctl = client(session, "bedrock-agentcore-control", region)
    out = ctl.create_dataset(datasetName=dname, description=f"From the console contract set {record['id']}"[:200],
                             schemaType="AGENTCORE_EVALUATION_PREDEFINED_V1", source={"inlineExamples": {"examples": dataset_examples(record["contracts"])}},
                             tags=dict(CONSOLE_TAG))
    return {"datasetId": out.get("datasetId"), "datasetArn": out.get("datasetArn"), "name": dname, "status": out.get("status")}


# -- verifications ----------------------------------------------------------------------------------------------------

def start_verification(console: Any, workspace: str, body: Mapping[str, Any]) -> dict[str, Any]:
    record = contract_set(console.store, workspace, str(body.get("contractSet") or ""))
    kind, ident = str(body.get("agentKind") or "harness"), str(body.get("agentId") or "")
    if kind != "harness":
        raise EvaluationError("verification runs on a Harness (a Runtime takes its own payload)")
    repeat = int(body.get("repeat") or 3)
    if not 1 <= repeat <= 5:
        raise EvaluationError("repeat: 1-5 rounds")
    panel = DEFAULT_PANEL if body.get("panel") else ()
    ws = console.workspaces.get(workspace)
    console.workspaces.verify(workspace)
    session = console.workspaces.session(workspace)
    ctl = client(session, "bedrock-agentcore-control", ws["region"])
    info = verify.harness_info(ctl, ident)
    # which version ran: an A/B experiment credits a verification of its treatment Harness only on the version it set up
    version = str(ctl.get_harness(harnessId=info["id"])["harness"].get("harnessVersion") or "") or None
    out = console.data_dir / "console" / "verifications"

    def work(job: Any) -> dict[str, Any]:
        run = verify.Verify(session, info, record["contracts"], record["l1"], region=ws["region"], out=out / job.job["id"], repeat=repeat,
                            panel=panel, log=job.log)
        doc = run.run()
        job.progress(robust=doc["robust"], holding=doc["holding"], contracts=len(doc["contracts"]))
        return {**doc, "markdown": verify.render_markdown(doc), "contractSet": record["id"]}

    return console.jobs.start("verify", workspace, {"agent": info["name"], "agentId": info["id"], "agentVersion": version, "contractSet": record["id"],
                                                     "repeat": repeat, "panel": bool(panel)}, work, label=f"验证 {info['name']}")


# -- online evaluation ---------------------------------------------------------------------------------------------------

def _console_rule(boundary: str | None) -> Any:
    """``direct.online``'s refusal hook with the console's rule (``agents.refusal``: its IAM path and tag)."""
    return lambda iam, name, found: refusal(name, found, role_tags(iam, name), boundary)


def online_role_arn(session: Any, account: str, role: str, boundary: str | None) -> str:
    """The online evaluation role's ARN as IAM has it (its path included) when the role exists, else the one it gets
    when the console creates it on its path. An existing role the console may not adopt is refused here, before
    anything is planned on it."""
    iam = client(session, "iam")
    found = online.existing_role(iam, role)
    if found is None:
        return f"arn:aws:iam::{account}:role{ROLE_PATH}{role}"
    why = refusal(role, found, role_tags(iam, role), boundary)
    if why:
        raise EvaluationError(why)
    return str(found["Arn"])


def plan_online(console: Any, workspace: str, job_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    job = console.jobs.get(job_id)
    doc = job.get("result") or {}
    if job.get("workspace") != workspace or job.get("kind") != "verify" or not doc.get("panel"):
        raise EvaluationError("plan from a finished verification that ran the evaluator panel")
    ws = console.workspaces.get(workspace)
    harness = doc["harness"]
    session = console.workspaces.session(workspace)
    ids = [e["evaluatorId"] for e in client(session, "bedrock-agentcore-control", ws["region"]).list_evaluators().get("evaluators") or []]
    role = f"{harness['name'][:40]}-online-eval"
    role_arn = online_role_arn(session, ws["accountId"], role, ws.get("permissionsBoundaryArn"))
    return online.plan(doc["panel"]["online"], agent=str(harness["name"]), kind="console", runtime_id=str(harness["runtimeId"]),
                       role_arn=role_arn, workshop_thelma=online.workshop_thelma_id(ids, str(harness["name"]).split("_")[0]),
                       sampling=float(body.get("sampling") or online.SAMPLING_PERCENT),
                       session_timeout=int(body.get("sessionTimeout") or online.SESSION_TIMEOUT_MINUTES), tags=dict(CONSOLE_TAG)) | {"role": role}


def create_online(console: Any, workspace: str, job_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """The plan applied: the role on the console's path (or the console's own existing one) and the configuration,
    which gets the role's ARN as IAM has it."""
    if body.get("acknowledged") is not True:
        raise EvaluationError("an online evaluation runs, and is billed, for every sampled session until deleted: send acknowledged: true")
    planned = plan_online(console, workspace, job_id, body)
    ws = console.workspaces.get(workspace)
    boundary = ws.get("permissionsBoundaryArn")
    record = online.apply_plan(console.workspaces.session(workspace), account=ws["accountId"], region=ws["region"], planned=planned,
                               role=planned["role"], boundary=boundary, path=ROLE_PATH, refusal=_console_rule(boundary))
    record.update(workspace=workspace, verification=job_id, createdAt=_now(), agent=console.jobs.get(job_id)["result"]["harness"]["name"])
    console.store.update("online_evals", {}, lambda all_: {**all_, record["configId"]: record})
    return record


def online_evaluations(console: Any, workspace: str) -> list[dict[str, Any]]:
    ws = console.workspaces.get(workspace)
    ctl = client(console.workspaces.session(workspace), "bedrock-agentcore-control", ws["region"])
    mine = console.store.read("online_evals", {})
    out = []
    for c in ctl.list_online_evaluation_configs().get("onlineEvaluationConfigs") or []:
        cid = c.get("onlineEvaluationConfigId")
        out.append({"configId": cid, "name": c.get("onlineEvaluationConfigName"), "status": c.get("status"), "execution": c.get("executionStatus"),
                    "console": cid in mine, **({k: mine[cid][k] for k in ("agent", "evaluators", "sampling", "createdAt") if k in mine[cid]} if cid in mine else {})})
    return out


def online_results(console: Any, workspace: str, config_id: str, hours: float) -> dict[str, Any]:
    ws = console.workspaces.get(workspace)
    session = console.workspaces.session(workspace)
    mine = console.store.read("online_evals", {}).get(config_id)
    if mine:
        group = mine["runtimeGroup"]
    else:
        got = client(session, "bedrock-agentcore-control", ws["region"]).get_online_evaluation_config(onlineEvaluationConfigId=config_id)
        group = ((got.get("dataSourceConfig") or {}).get("cloudWatchLogs") or {}).get("logGroupNames", [""])[0]
    return online.fetch_results(session, region=ws["region"], config_id=config_id, runtime_group=group, hours=hours)


def delete_online(console: Any, workspace: str, config_id: str) -> dict[str, Any]:
    mine = console.store.read("online_evals", {}).get(config_id)
    if not mine or mine.get("workspace") != workspace:
        raise EvaluationError("only online evaluations created from this console can be deleted here")
    ws = console.workspaces.get(workspace)
    boundary, left = ws.get("permissionsBoundaryArn"), []
    done = online.delete_config(console.workspaces.session(workspace), region=ws["region"], config_id=config_id, role=mine.get("role"),
                                boundary=boundary, refusal=_console_rule(boundary), left=left)
    console.store.update("online_evals", {}, lambda all_: {k: v for k, v in all_.items() if k != config_id})
    return {"deleted": done, **({"left": left} if left else {})}


# -- custom judges --------------------------------------------------------------------------------------------------------

def judge_request(body: Mapping[str, Any]) -> dict[str, Any]:
    name = str(body.get("name") or "")
    if not NAME.match(name):
        raise EvaluationError("name: a letter, then letters, digits or underscores (at most 48)")
    level = str(body.get("level") or "TRACE")
    if level not in ("TRACE", "SESSION", "TOOL_CALL"):
        raise EvaluationError("level: TRACE, SESSION or TOOL_CALL")
    instructions = str(body.get("instructions") or "").strip()
    if len(instructions) < 20:
        raise EvaluationError("instructions: say what to judge and how (at least 20 characters)")
    scale = body.get("scale") or [{"value": 0.0, "label": "No", "definition": "The response does not meet the criterion."},
                                  {"value": 1.0, "label": "Yes", "definition": "The response meets the criterion."}]
    numerical = [{"value": float(s["value"]), "label": str(s["label"])[:40], "definition": str(s["definition"])[:500]} for s in scale]
    if len(numerical) < 2:
        raise EvaluationError("scale: at least two levels")
    model = str(body.get("model") or "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    return {"evaluatorName": name, "description": str(body.get("description") or "")[:200] or None, "level": level,
            "evaluatorConfig": {"llmAsAJudge": {"instructions": instructions, "ratingScale": {"numerical": numerical},
                                                "modelConfig": {"bedrockEvaluatorModelConfig": {"modelId": model, "inferenceConfig": {"temperature": 0.0}}}}},
            "tags": dict(CONSOLE_TAG)}


def evaluators(session: Any, region: str) -> list[dict[str, Any]]:
    ctl = client(session, "bedrock-agentcore-control", region)
    out, kwargs = [], {}
    while True:
        page = ctl.list_evaluators(**kwargs)
        out += [{"id": e.get("evaluatorId"), "name": e.get("evaluatorName"), "type": e.get("evaluatorType"), "level": e.get("level"),
                 "arn": e.get("evaluatorArn"), "description": e.get("description")} for e in page.get("evaluators") or []]
        if not page.get("nextToken"):
            return out
        kwargs["nextToken"] = page["nextToken"]


def create_judge(session: Any, region: str, body: Mapping[str, Any]) -> dict[str, Any]:
    request = {k: v for k, v in judge_request(body).items() if v is not None}
    out = client(session, "bedrock-agentcore-control", region).create_evaluator(**request)
    return {"id": out.get("evaluatorId"), "arn": out.get("evaluatorArn"), "name": request["evaluatorName"], "status": out.get("status")}


def delete_judge(session: Any, region: str, evaluator_id: str) -> dict[str, Any]:
    ctl = client(session, "bedrock-agentcore-control", region)
    got = ctl.get_evaluator(evaluatorId=evaluator_id)
    tags = ctl.list_tags_for_resource(resourceArn=got["evaluatorArn"]).get("tags") or {}
    if tags.get("adlc:console") != "1":
        raise EvaluationError("only judges created from this console can be deleted here")
    ctl.delete_evaluator(evaluatorId=evaluator_id)
    return {"deleted": evaluator_id}


# -- batch evaluation -----------------------------------------------------------------------------------------------------

def start_batch(session: Any, region: str, agent: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    if not agent.get("runtimeId"):
        raise EvaluationError("the agent has no runtime to read sessions from")
    chosen = [str(e) for e in body.get("evaluators") or ["Builtin.Correctness", "Builtin.Faithfulness"]][:10]
    hours = min(max(float(body.get("hours") or 24), 1.0), 24 * 30)
    end = datetime.now(timezone.utc)
    filters: dict[str, Any] = {"timeRange": {"startTime": end - timedelta(hours=hours), "endTime": end}}
    if body.get("sessionIds"):
        filters["sessionIds"] = [str(s) for s in body["sessionIds"]][:100]
    service = re.sub(r"-[^-]+$", ".DEFAULT", str(agent["runtimeId"]))
    name = re.sub(r"[^A-Za-z0-9_]", "_", f"adlc_{agent['name']}_{end.strftime('%m%d%H%M%S')}")[:48]
    request: dict[str, Any] = {"batchEvaluationName": name, "tags": dict(CONSOLE_TAG),
                               "dataSourceConfig": {"cloudWatchLogs": {"serviceNames": [service],
                                                                       "logGroupNames": [f"/aws/bedrock-agentcore/runtimes/{agent['runtimeId']}-DEFAULT"],
                                                                       "filterConfig": filters}}}
    # The service takes evaluators or the insight, never both (live 2026-10-01): two kinds of batch.
    if body.get("mode") == "insight":
        request["insights"], chosen = [{"insightId": i} for i in INSIGHTS], []
    else:
        request["evaluators"] = [{"evaluatorId": e} for e in chosen]
    out = client(session, "bedrock-agentcore", region).start_batch_evaluation(**request)
    return {"id": out.get("batchEvaluationId"), "name": name, "status": out.get("status"), "evaluators": chosen,
            "mode": "insight" if "insights" in request else "evaluators"}


def batches(session: Any, region: str) -> list[dict[str, Any]]:
    got = client(session, "bedrock-agentcore", region).list_batch_evaluations()
    return [{"id": b.get("batchEvaluationId"), "name": b.get("batchEvaluationName"), "status": b.get("status"), "createdAt": str(b.get("createdAt") or "")}
            for b in got.get("batchEvaluations") or got.get("batchEvaluationSummaries") or []]


def batch(session: Any, region: str, batch_id: str) -> dict[str, Any]:
    got = client(session, "bedrock-agentcore", region).get_batch_evaluation(batchEvaluationId=batch_id)
    keep = ("batchEvaluationId", "batchEvaluationName", "status", "createdAt", "updatedAt", "evaluators", "evaluationResults", "failureAnalysisResult",
            "userIntentResult", "executionSummaryResult", "errorDetails")
    return {k: got.get(k) for k in keep if got.get(k) is not None}


# -- recommendations ------------------------------------------------------------------------------------------------

REC_TYPES = {"systemPrompt": "SYSTEM_PROMPT_RECOMMENDATION", "toolDescription": "TOOL_DESCRIPTION_RECOMMENDATION"}
TRANSLATOR = "us.anthropic.claude-sonnet-5-5"
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def translate(session: Any, region: str, text: str, into: str) -> str:
    """A faithful translation of a system prompt (tool names, placeholders and formatting kept), by Converse."""
    out = client(session, "bedrock-runtime", region).converse(
        modelId=TRANSLATOR, system=[{"text": f"Translate the system prompt the user gives into {into}, faithfully: every rule, the same structure and "
                                             "formatting; tool names, identifiers, numbers and placeholders unchanged. Output only the translation."}],
        messages=[{"role": "user", "content": [{"text": text}]}], inferenceConfig={"maxTokens": 8000})  # Sonnet 5.5 refuses temperature
    if out.get("stopReason") == "max_tokens":  # a cut translation would be analysed and adopted as if it were the prompt
        raise EvaluationError("the prompt is too long to translate in one piece for AgentCore's analysis: shorten it, or send translate: false")
    return "".join(b.get("text") or "" for b in out["output"]["message"]["content"]).strip()
REC_SOURCES = ("logs", "online", "batch")
REC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
EVALUATOR_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,127}$")


def _runtime(agent: Mapping[str, Any]) -> tuple[str, str]:
    """The agent's runtime log group and service name, as AgentCore's evaluation reads them."""
    rid = str(agent.get("runtimeId") or "")
    if not rid:
        raise EvaluationError(f"{agent.get('name')} has no runtime to read traces from")
    return f"/aws/bedrock-agentcore/runtimes/{rid}-DEFAULT", f"{rid.rsplit('-', 1)[0]}.DEFAULT"


def gateway_tools(session: Any, region: str, harness_id: str) -> list[dict[str, str]]:
    """A Harness's Gateway tools as the model sees them (``<target>___<tool>``) with their descriptions: the inline
    schemas of Lambda targets (connector targets, a knowledge base's say, declare none)."""
    ctl = client(session, "bedrock-agentcore-control", region)
    out = []
    for tool in ctl.get_harness(harnessId=harness_id)["harness"].get("tools") or []:
        arn = ((tool.get("config") or {}).get("agentCoreGateway") or {}).get("gatewayArn")
        if tool.get("type") != "agentcore_gateway" or not arn:
            continue
        gid, token = arn.rsplit("/", 1)[-1], None
        while True:
            page = ctl.list_gateway_targets(gatewayIdentifier=gid, **({"nextToken": token} if token else {}))
            for target in page.get("items") or []:
                got = ctl.get_gateway_target(gatewayIdentifier=gid, targetId=target["targetId"])
                schema = (((got.get("targetConfiguration") or {}).get("mcp") or {}).get("lambda") or {}).get("toolSchema") or {}
                for t in schema.get("inlinePayload") or []:
                    out.append({"toolName": f"{target['name']}___{t['name']}", "description": str(t.get("description") or "")})
            token = page.get("nextToken")
            if not token:
                break
    return out


def recommendation_request(agent: Mapping[str, Any], body: Mapping[str, Any], *, account: str, region: str, prompt: str | None,
                           tools: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    kind = str(body.get("type") or "systemPrompt")
    if kind not in REC_TYPES:
        raise EvaluationError("type: systemPrompt or toolDescription")
    source = str(body.get("source") or "logs")
    if source not in REC_SOURCES:
        raise EvaluationError("source: logs, online or batch")
    days = max(1, min(int(body.get("days") or 7), 30))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    if source == "logs":
        group, service = _runtime(agent)
        traces: dict[str, Any] = {"cloudwatchLogs": {"logGroupArns": [f"arn:aws:logs:{region}:{account}:log-group:{group}",
                                                                      f"arn:aws:logs:{region}:{account}:log-group:aws/spans"],
                                                     "serviceNames": [service], "startTime": start, "endTime": end}}
    elif source == "online":
        arn = str(body.get("onlineEvaluationConfigArn") or "")
        if not arn.startswith("arn:aws:bedrock-agentcore:"):
            raise EvaluationError("onlineEvaluationConfigArn: the online evaluation whose scored sessions to learn from")
        traces = {"onlineEvaluation": {"onlineEvaluationConfigArn": arn, "startTime": start, "endTime": end}}
    else:
        arn = str(body.get("batchEvaluationArn") or "")
        if not arn.startswith("arn:aws:bedrock-agentcore:"):
            raise EvaluationError("batchEvaluationArn: the batch evaluation whose sessions to learn from")
        if kind == "toolDescription":
            raise EvaluationError("AgentCore refuses a batch evaluation as the traces of a tool-description recommendation")
        traces = {"batchEvaluation": {"batchEvaluationArn": arn}}
    if kind == "systemPrompt":
        if not prompt:
            raise EvaluationError(f"{agent.get('name')} has no system prompt the console can read: give systemPrompt")
        evaluators = [str(e) for e in body.get("evaluators") or ["Builtin.GoalSuccessRate"]]
        if not 1 <= len(evaluators) <= 5 or not all(EVALUATOR_ID.match(e) for e in evaluators):
            raise EvaluationError("evaluators: 1 to 5 evaluator ids (Builtin.GoalSuccessRate by default)")
        config: dict[str, Any] = {"systemPromptRecommendationConfig": {"systemPrompt": {"text": prompt}, "agentTraces": traces, "evaluationConfig": {
            "evaluators": [{"evaluatorArn": e if e.startswith("arn:") else f"arn:aws:bedrock-agentcore:::evaluator/{e}"} for e in evaluators]}}}
    else:
        if not tools:
            raise EvaluationError(f"{agent.get('name')} has no tool descriptions the console can read (Lambda-target Gateway tools)")
        config = {"toolDescriptionRecommendationConfig": {"toolDescription": {"toolDescriptionText": {"tools": [
            {"toolName": t["toolName"], "toolDescription": {"text": t["description"] or t["toolName"]}} for t in tools]}}, "agentTraces": traces}}
    return {"name": f"adlc_{re.sub(r'[^A-Za-z0-9_]', '_', str(agent.get('name') or 'agent'))[:40]}_{secrets.token_hex(3)}",
            "description": f"ADLC console: {agent.get('name')}, {kind} from {source}"[:200], "type": REC_TYPES[kind], "recommendationConfig": config,
            "clientToken": str(uuid.uuid4()), "tags": dict(CONSOLE_TAG)}


def start_recommendation(console: Any, workspace: str, body: Mapping[str, Any]) -> dict[str, Any]:
    ws = console.workspaces.get(workspace)
    console.workspaces.verify(workspace)
    session = console.workspaces.session(workspace)
    kind = str(body.get("agentKind") or "harness")
    agent = get_agent(session, ws["region"], kind, str(body.get("agentId") or ""))
    tools = gateway_tools(session, ws["region"], agent["id"]) if body.get("type") == "toolDescription" and kind == "harness" else []
    prompt = str(body.get("systemPrompt") or agent.get("systemPrompt") or "") or None
    original = None
    if prompt and CJK.search(prompt) and body.get("translate", True) is not False and str(body.get("type") or "systemPrompt") == "systemPrompt":
        original, prompt = prompt, translate(session, ws["region"], prompt, "English")
    request = recommendation_request(agent, body, account=ws["accountId"], region=ws["region"], prompt=prompt, tools=tools)
    got = client(session, "bedrock-agentcore", ws["region"]).start_recommendation(**request)
    rid = got["recommendationId"]
    record = {"workspace": workspace, "agent": {"kind": kind, "id": agent["id"], "name": agent["name"]}, "type": request["type"],
              "source": str(body.get("source") or "logs"), "createdAt": _now(), **({"original": original} if original else {})}
    console.store.update("recommendations", {}, lambda all_: {**all_, rid: record})
    return {"id": rid, "status": got.get("status"), "name": request["name"]}


def recommendation(console: Any, workspace: str, rid: str) -> dict[str, Any]:
    if not REC_ID.match(rid):
        raise EvaluationError("no such recommendation")
    ws = console.workspaces.get(workspace)
    got = client(console.workspaces.session(workspace), "bedrock-agentcore", ws["region"]).get_recommendation(recommendationId=rid)
    config = got.get("recommendationConfig") or {}
    result = got.get("recommendationResult") or {}
    prompt_result, tool_result = result.get("systemPromptRecommendationResult") or {}, result.get("toolDescriptionRecommendationResult") or {}
    current_tools = {t["toolName"]: (t.get("toolDescription") or {}).get("text") for t in
                     (((config.get("toolDescriptionRecommendationConfig") or {}).get("toolDescription") or {}).get("toolDescriptionText") or {}).get("tools") or []}
    mine = console.store.read("recommendations", {}).get(rid) or {}
    recommended = prompt_result.get("recommendedSystemPrompt")
    back = mine.get("translatedBack")
    if mine.get("original") and recommended and not back:  # once: the recommendation in the agent's own language
        back = translate(console.workspaces.session(workspace), ws["region"], recommended, "the language of this sample: " + mine["original"][:200])
        console.store.update("recommendations", {}, lambda all_: {**all_, rid: {**all_.get(rid, mine), "translatedBack": back}})
    return {"id": rid, "name": got.get("name"), "type": got.get("type"), "status": got.get("status"), "createdAt": str(got.get("createdAt") or ""),
            "updatedAt": str(got.get("updatedAt") or ""), "agent": mine.get("agent"), "console": bool(mine),
            "current": mine.get("original") or ((config.get("systemPromptRecommendationConfig") or {}).get("systemPrompt") or {}).get("text"),
            "analyzedAs": ((config.get("systemPromptRecommendationConfig") or {}).get("systemPrompt") or {}).get("text") if mine.get("original") else None,
            "recommended": back or recommended, "recommendedAsAnalyzed": recommended if back else None, "explanation": prompt_result.get("explanation"),
            "tools": [{"toolName": t.get("toolName"), "current": current_tools.get(t.get("toolName")), "recommended": t.get("recommendedToolDescription"),
                       "explanation": t.get("explanation")} for t in tool_result.get("tools") or []],
            "error": " ".join(str(x) for x in (prompt_result.get("errorCode"), prompt_result.get("errorMessage"), tool_result.get("errorCode"),
                                               tool_result.get("errorMessage")) if x) or None}


def recommendations(console: Any, workspace: str) -> list[dict[str, Any]]:
    ws = console.workspaces.get(workspace)
    data = client(console.workspaces.session(workspace), "bedrock-agentcore", ws["region"])
    mine = console.store.read("recommendations", {})
    out, token = [], None
    while True:
        page = data.list_recommendations(**({"nextToken": token} if token else {}))
        for r in page.get("recommendationSummaries") or page.get("recommendations") or []:
            rid = r.get("recommendationId")
            out.append({"id": rid, "name": r.get("name"), "type": r.get("type"), "status": r.get("status"), "createdAt": str(r.get("createdAt") or ""),
                        "agent": (mine.get(rid) or {}).get("agent"), "console": rid in mine})
        token = page.get("nextToken")
        if not token:
            return sorted(out, key=lambda r: r["createdAt"], reverse=True)


def delete_recommendation(console: Any, workspace: str, rid: str) -> dict[str, Any]:
    mine = console.store.read("recommendations", {}).get(rid)
    if not mine or mine.get("workspace") != workspace:
        raise EvaluationError("only recommendations started from this console can be deleted here")
    ws = console.workspaces.get(workspace)
    client(console.workspaces.session(workspace), "bedrock-agentcore", ws["region"]).delete_recommendation(recommendationId=rid)
    console.store.update("recommendations", {}, lambda all_: {k: v for k, v in all_.items() if k != rid})
    return {"deleted": rid}


def adopt_recommendation(console: Any, workspace: str, rid: str, body: Mapping[str, Any], caller: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The recommended prompt as a configuration bundle's treatment (the agent's current configuration its control),
    then, with a contract set, a verification of it on the agent per call: the agent itself is not changed."""
    from . import experiments

    rec = recommendation(console, workspace, rid)
    if rec["status"] != "COMPLETED" or not rec.get("recommended"):
        raise EvaluationError(f"the recommendation is {rec['status']}{': ' + rec['error'] if rec.get('error') else ''}: nothing to adopt yet")
    agent = rec.get("agent") or {}
    if agent.get("kind") != "harness":
        raise EvaluationError("adopting needs a Harness started from this console (a configuration bundle carries its prompt)")
    ws = console.workspaces.get(workspace)
    x = experiments.Ctx(console, workspace, console.workspaces.session(workspace), ws["region"], ws["accountId"], caller)
    bundle = experiments.create_bundle(x, {"agentId": agent["id"], "systemPrompt": rec["recommended"],
                                           "commitMessage": f"AgentCore recommendation {rid}: {str(rec.get('explanation') or '')[:300]}"})
    out: dict[str, Any] = {"bundle": bundle}
    if body.get("contractSet"):
        out["verification"] = experiments.verify_treatment(x, {"agentId": agent["id"], "bundleId": bundle["id"], "versionId": bundle["treatmentVersion"],
                                                               "contractSet": body["contractSet"], "repeat": body.get("repeat", 3)})
    return out


def register(router: Any) -> None:
    def ws(r: Any) -> tuple[Any, str]:
        w = r.workspace()
        return r.console.workspaces.get(w), w

    def sess(r: Any) -> tuple[Any, str]:
        session = r.session()
        return session, r.console.workspaces.get(r.workspace())["region"]

    add = router.add
    add("GET", "/workspaces/{wid}/contract-sets", lambda r: (200, {"contractSets": contract_sets(r.console.store, r.workspace())}))
    add("POST", "/workspaces/{wid}/contract-sets", lambda r: (201, put_contract_set(r.console.store, r.workspace(), r.body)))
    add("GET", "/workspaces/{wid}/contract-sets/packs", lambda r: (r.workspace(), (200, {"projects": packs(r.console.workshop_data)}))[1])
    add("POST", "/workspaces/{wid}/contract-sets/import", lambda r: (201, import_from_pack(r.console.store, r.workspace(), r.console.workshop_data,
                                                                                            str(r.body.get("project") or ""))))
    add("GET", "/workspaces/{wid}/contract-sets/{sid}", lambda r: (200, contract_set(r.console.store, r.workspace(), r.params["sid"])))
    add("DELETE", "/workspaces/{wid}/contract-sets/{sid}", lambda r: (r.console.store.update("contract_sets", {}, lambda a: {
        k: v for k, v in a.items() if not (k == r.params["sid"] and v["workspace"] == r.workspace())}), (200, {"deleted": r.params["sid"]}))[1])
    add("POST", "/workspaces/{wid}/contract-sets/{sid}/dataset", lambda r: (201, create_dataset(*sess(r), contract_set(r.console.store, r.workspace(),
                                                                                                                       r.params["sid"]), r.body.get("name"))))
    add("POST", "/workspaces/{wid}/verifications", lambda r: (202, start_verification(r.console, r.workspace(), r.body)))
    add("GET", "/workspaces/{wid}/verifications", lambda r: (200, {"jobs": r.console.jobs.list(workspace=r.workspace(), kind="verify")}))
    add("POST", "/workspaces/{wid}/verifications/{jid}/online-plan", lambda r: (200, plan_online(r.console, r.workspace(), r.params["jid"], r.body)))
    add("POST", "/workspaces/{wid}/verifications/{jid}/online", lambda r: (201, create_online(r.console, r.workspace(), r.params["jid"], r.body)))
    add("GET", "/workspaces/{wid}/online-evaluations", lambda r: (200, {"configs": online_evaluations(r.console, r.workspace())}))
    add("GET", "/workspaces/{wid}/online-evaluations/{cid}/results", lambda r: (200, online_results(r.console, r.workspace(), r.params["cid"],
                                                                                                     float(r.query.get("hours") or 24))))
    add("DELETE", "/workspaces/{wid}/online-evaluations/{cid}", lambda r: (200, delete_online(r.console, r.workspace(), r.params["cid"])))
    add("GET", "/workspaces/{wid}/evaluators", lambda r: (200, {"evaluators": evaluators(*sess(r))}))
    add("POST", "/workspaces/{wid}/evaluators", lambda r: (201, create_judge(*sess(r), r.body)))
    add("DELETE", "/workspaces/{wid}/evaluators/{eid}", lambda r: (200, delete_judge(*sess(r), r.params["eid"])))

    def start(r: Any):
        session, region = sess(r)
        agent = get_agent(session, region, str(r.body.get("agentKind") or "harness"), str(r.body.get("agentId") or ""))
        return 202, start_batch(session, region, agent, r.body)

    add("POST", "/workspaces/{wid}/recommendations", lambda r: (202, start_recommendation(r.console, r.workspace(), r.body)))
    add("GET", "/workspaces/{wid}/recommendations", lambda r: (200, {"recommendations": recommendations(r.console, r.workspace())}))
    add("GET", "/workspaces/{wid}/recommendations/{rid}", lambda r: (200, recommendation(r.console, r.workspace(), r.params["rid"])))
    add("DELETE", "/workspaces/{wid}/recommendations/{rid}", lambda r: (200, delete_recommendation(r.console, r.workspace(), r.params["rid"])))
    add("POST", "/workspaces/{wid}/recommendations/{rid}/adopt", lambda r: (201, adopt_recommendation(r.console, r.workspace(), r.params["rid"], r.body,
                                                                                                     r.caller)))
    add("POST", "/workspaces/{wid}/batch-evaluations", start)
    add("GET", "/workspaces/{wid}/batch-evaluations", lambda r: (200, {"batches": batches(*sess(r))}))
    add("GET", "/workspaces/{wid}/batch-evaluations/{bid}", lambda r: (200, batch(*sess(r), r.params["bid"])))
