"""Generic scenario tools handler for AgentCore Gateway (generated release artifact).

Replaces the upstream ``hr_tools_handler.py``. Behaviour is driven entirely by the
``fixtures.json`` shipped next to this file (compiled from ``scenario.yaml``):

* ``kind: retrieval`` tools query the Bedrock Knowledge Base whose id is stored in the SSM
  parameter named by ``kbIdSsmParameter`` (fallback: ``KNOWLEDGE_BASE_ID`` env var).
* ``kind: mock`` tools return deterministic fixtures: the first matching ``errors`` rule wins,
  then the first matching ``cases`` rule, then ``default``. Matching is equality on every key of
  the rule's ``when`` mapping. Values may contain placeholders:
  ``{{arg:<name>}}`` (call argument, ``unknown`` when absent), ``{{year}}``, ``{{uuid4}}``
  (4 upper-case hex chars, like the upstream mock) and ``{{now}}`` (ISO-8601 UTC).

No customer write API is ever called from here: mock tools are the only side-effect-free stand-in
the workshop uses.
"""

import ast
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES_PATH = os.environ.get("TOOL_FIXTURES_PATH", os.path.join(HERE, "fixtures.json"))
with open(FIXTURES_PATH, encoding="utf-8") as _fh:
    FIXTURES = json.load(_fh)

REGION = os.environ.get("AWS_REGION", "us-west-2")
KB_ID_SSM_PARAM = os.environ.get("KB_ID_SSM_PARAM", FIXTURES.get("kbIdSsmParameter", ""))
RETRIEVAL_RESULTS = int(os.environ.get("KB_NUMBER_OF_RESULTS", "3"))

_PLACEHOLDER = re.compile(r"\{\{(arg:[A-Za-z0-9_]+|year|uuid4|now)\}\}")
_kb_id_cache = None


# ---------------------------------------------------------------------------
# Knowledge Base
# ---------------------------------------------------------------------------


def get_kb_id():
    """Resolve the Knowledge Base id from SSM (cached only on success)."""
    global _kb_id_cache
    if _kb_id_cache:
        return _kb_id_cache
    kb_id = ""
    if KB_ID_SSM_PARAM:
        try:
            ssm = boto3.client("ssm", region_name=REGION)
            kb_id = ssm.get_parameter(Name=KB_ID_SSM_PARAM)["Parameter"]["Value"]
        except Exception as exc:  # noqa: BLE001 - surfaced through the fallback below
            logger.warning("Could not read KB ID from SSM %s: %s", KB_ID_SSM_PARAM, exc)
    if not kb_id:
        kb_id = os.environ.get("KNOWLEDGE_BASE_ID", "")
    if kb_id:
        _kb_id_cache = kb_id
    return kb_id


def run_retrieval(args):
    query = str(args.get("query", ""))
    kb_id = get_kb_id()
    if not kb_id:
        return {"answer": "Knowledge base is not configured.", "sources": []}
    client = boto3.client("bedrock-agent-runtime", region_name=REGION)
    response = client.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": RETRIEVAL_RESULTS}},
    )
    results = []
    for item in response.get("retrievalResults", []):
        results.append(
            {
                "content": item.get("content", {}).get("text", ""),
                "source": item.get("location", {}).get("s3Location", {}).get("uri", "unknown"),
                "score": item.get("score", 0),
            }
        )
    if not results:
        return {"answer": "No relevant policy documents found for your query.", "sources": []}
    return {"answer": "\n\n".join(r["content"] for r in results), "sources": [r["source"] for r in results]}


# ---------------------------------------------------------------------------
# Mock tools
# ---------------------------------------------------------------------------


def render_placeholders(value, args):
    if isinstance(value, str):
        def sub(match):
            key = match.group(1)
            if key.startswith("arg:"):
                arg = args.get(key[4:])
                return "unknown" if arg is None or arg == "" else str(arg)
            if key == "year":
                return datetime.now(timezone.utc).strftime("%Y")
            if key == "uuid4":
                return uuid.uuid4().hex[:4].upper()
            if key == "now":
                return datetime.now(timezone.utc).isoformat()
            return match.group(0)

        return _PLACEHOLDER.sub(sub, value)
    if isinstance(value, list):
        return [render_placeholders(v, args) for v in value]
    if isinstance(value, dict):
        return {k: render_placeholders(v, args) for k, v in value.items()}
    return value


def rule_matches(when, args):
    return all(str(args.get(key)) == str(expected) for key, expected in when.items())


def run_mock(tool_name, spec, args):
    fixtures = spec.get("fixtures", {})
    for rule in fixtures.get("errors", []):
        if rule_matches(rule.get("when", {}), args):
            return {"error": render_placeholders(rule["error"], args)}
    for rule in fixtures.get("cases", []):
        if rule_matches(rule.get("when", {}), args):
            return render_placeholders(rule["return"], args)
    if "default" not in fixtures:
        return {"error": f"Tool {tool_name} has no default fixture"}
    return render_placeholders(fixtures["default"], args)


# ---------------------------------------------------------------------------
# Gateway plumbing
# ---------------------------------------------------------------------------


def resolve_tool_name(event, context):
    """Tool name from the Gateway client context, falling back to the event body."""
    tool_name = ""
    client_context = getattr(context, "client_context", None) if context else None
    if client_context is not None:
        custom = getattr(client_context, "custom", None) or {}
        if isinstance(custom, str):
            try:
                custom = ast.literal_eval(custom)
            except Exception:  # noqa: BLE001
                custom = json.loads(custom) if custom else {}
        tool_name = custom.get("bedrockAgentCoreToolName", custom.get("bedrockagentcoreToolName", ""))
    if not tool_name:
        body = event if isinstance(event, dict) else json.loads(event or "{}")
        tool_name = body.get("name", body.get("tool_name", ""))
    if "___" in tool_name:
        tool_name = tool_name.split("___", 1)[1]
    return tool_name


def lambda_handler(event, context):
    logger.info("EVENT: %s", json.dumps(event, default=str))
    tool_name = resolve_tool_name(event, context)
    tools = FIXTURES.get("tools", {})
    spec = tools.get(tool_name)
    if spec is None:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": f"Unknown tool: {tool_name}. Available: {sorted(tools)}"}),
        }
    args = event if isinstance(event, dict) else {}
    try:
        if spec.get("kind") == "retrieval":
            result = run_retrieval(args)
        else:
            result = run_mock(tool_name, spec, args)
        return {"statusCode": 200, "body": json.dumps(result, ensure_ascii=False)}
    except Exception as exc:  # noqa: BLE001 - Gateway expects a JSON error body
        logger.exception("tool %s failed", tool_name)
        return {"statusCode": 500, "body": json.dumps({"error": str(exc)})}
