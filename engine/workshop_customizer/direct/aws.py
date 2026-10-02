"""Create or update a pack's direct-mode resources (idempotent, by name) and drive its Harness.

Every request mirrors what the Workshop's own steps send, minus the Workshop instance:
01 (``knowledge-base/create_kb.py``: S3 Vectors, FIXED_SIZE 128 tokens / 20 %), 02 (the tools Lambda from the
release's ``lambda/`` and an MCP Gateway with an AWS_IAM authorizer and a GATEWAY_IAM_ROLE target), 04 / 05 (a
memory with the SEMANTIC and USER_PREFERENCE strategies under ``/users/{actorId}/…``, the Harness limits
30 iterations / 8192 tokens / 300 s, split telemetry) and the execution role's statements of the Workshop's
CDK role. Two deliberate differences: the Harness runs on the default (PUBLIC) network, so no VPC is needed,
and its skills come from S3 (``{"s3": {"uri": <prefix>}}``) instead of an S3 Files mount.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .names import DirectNames

#: 04-deploy.sh: --max-iterations 30 --max-tokens 8192; the Harness default timeout of the Workshop.
HARNESS_LIMITS = {"maxIterations": 30, "maxTokens": 8192, "timeoutSeconds": 300}
#: The render's split telemetry (spans in aws/spans, events in the runtime log group), which THELMA and L1 read.
HARNESS_ENVIRONMENT = {"UNIFIED_TRACES_DESTINATION_ENABLED": "false"}
#: 05-setup-memory.sh: the retrieval namespaces and their topK.
MEMORY_NAMESPACES = {"USER_PREFERENCE": ("/users/{actorId}/preferences", 20), "SEMANTIC": ("/users/{actorId}/facts", 10)}
#: The account that publishes the Harness container image (per region).
HARNESS_IMAGE_ACCOUNT = "796669927364"


#: Live 2026-10-01 three packs at once through a local HTTPS proxy saw TLS connections dropped on CloudWatch
#: Logs calls, five legacy-mode attempts in a row, which ended a pack's run in its judge stage. Every direct
#: client retries in standard mode with backoff; subprocesses (create_kb.py, the judges) get the same through
#: the environment variables boto3 reads.
RETRIES = {"total_max_attempts": 10, "mode": "standard"}
RETRY_ENV = {"AWS_RETRY_MODE": "standard", "AWS_MAX_ATTEMPTS": "10"}
#: The checkout's own interpreter. It has what the release's scripts (create_kb.py: retrying, boto3) and the judges
#: import. Inside KiroCrew this process runs on the gateway's bundled Python, which lacks them (live 2026-10-02: an
#: Autopilot run in KiroCrew failed its first direct round on ``No module named 'retrying'``).
CHECKOUT_PYTHON = Path(__file__).resolve().parents[3] / ".venv" / "bin" / "python3"


def tools_python() -> str:
    """The interpreter for the release's scripts, the judges and the Autopilot loop: the checkout's ``.venv`` when it is
    there, else this process's own (the standalone console and the tests already run on it)."""
    return str(CHECKOUT_PYTHON) if CHECKOUT_PYTHON.is_file() and os.access(CHECKOUT_PYTHON, os.X_OK) else sys.executable


def client(session: Any, name: str, region: str | None = None) -> Any:
    """A boto3 client for direct mode: standard retries, and a read timeout a Harness stream fits in (300 s)."""
    from botocore.config import Config

    return session.client(name, region_name=region, config=Config(retries=dict(RETRIES), connect_timeout=20, read_timeout=360))


class ProvisionError(RuntimeError):
    pass


def _wait(probe: Callable[[], str], ready: Iterable[str], *, what: str, attempts: int = 120, pause: float = 5.0,
          sleep: Callable[[float], None] = time.sleep) -> str:
    ready = set(ready)
    status = "UNKNOWN"
    for _ in range(attempts):
        status = probe()
        if status in ready:
            return status
        if "FAIL" in status:
            raise ProvisionError(f"{what} is {status}")
        sleep(pause)
    raise ProvisionError(f"{what} is still {status} after {int(attempts * pause)} s")


def _code_error(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    return str(((response or {}).get("Error") or {}).get("Code") or "") if isinstance(response, dict) else ""


def tool_zip(release_dir: Path) -> bytes:
    """The tools Lambda package 02 builds: the scenario handler and its fixtures, at the archive root."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("scenario_tools_handler.py", "fixtures.json"):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, (release_dir / "lambda" / name).read_bytes())
    return buffer.getvalue()


def docs_digest(docs_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(docs_dir.glob("*")):
        if path.is_file():
            digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def harness_role_policy(*, account: str, region: str, names: DirectNames, gateway_arn: str, memory_arn: str) -> dict:
    """The Workshop CDK role's statements (model calls, telemetry, workload identity, the Harness image),
    scoped to this pack's Gateway, memory and skills prefix."""
    runtimes = f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": ["arn:aws:bedrock:*::foundation-model/*", f"arn:aws:bedrock:{region}:{account}:*", "arn:aws:bedrock:*:*:inference-profile/*"]},
        {"Effect": "Allow", "Action": ["ecr-public:GetAuthorizationToken", "sts:GetServiceBearerToken", "ecr:GetAuthorizationToken",
                                       "cloudwatch:PutMetricData", "xray:GetSamplingRules", "xray:GetSamplingTargets",
                                       "xray:PutTelemetryRecords", "xray:PutTraceSegments"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
         "Resource": f"arn:aws:ecr:{region}:{HARNESS_IMAGE_ACCOUNT}:repository/harness-{region}"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:DescribeLogStreams"], "Resource": runtimes},
        {"Effect": "Allow", "Action": ["logs:DescribeLogGroups"], "Resource": f"arn:aws:logs:{region}:{account}:log-group:*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Resource": f"{runtimes}:log-stream:*"},
        {"Effect": "Allow", "Action": ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                                       "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
         "Resource": [f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default",
                      f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/*"]},
        {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway", "Resource": gateway_arn},
        {"Effect": "Allow", "Action": ["bedrock-agentcore:CreateEvent", "bedrock-agentcore:DeleteEvent", "bedrock-agentcore:GetEvent",
                                       "bedrock-agentcore:GetMemory", "bedrock-agentcore:GetMemoryRecord", "bedrock-agentcore:ListActors",
                                       "bedrock-agentcore:ListEvents", "bedrock-agentcore:ListMemoryRecords", "bedrock-agentcore:ListSessions",
                                       "bedrock-agentcore:RetrieveMemoryRecords", "bedrock-agentcore:DeleteMemoryRecord"],
         "Resource": memory_arn},
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{names.bucket}/{names.skills_prefix}*"},
        {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{names.bucket}",
         "Condition": {"StringLike": {"s3:prefix": [f"{names.skills_prefix}*"]}}},
    ]}


class Provisioner:
    """The pack's direct-mode resources, created or updated by name (rerunning it changes only what changed)."""

    def __init__(self, session: Any, names: DirectNames, *, release_dir: Path, pack: Mapping[str, Any], account: str, region: str,
                 state_path: Path, log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep):
        self.session, self.names, self.release_dir, self.pack = session, names, release_dir, pack
        self.account, self.region, self.log, self.sleep = account, region, log, sleep
        self.skipped_skills: list[str] = []
        self.state_path = state_path
        self.state: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        self.tags = names.tags(str(pack.get("packId") or names.agent))
        self.ctl = client(session, "bedrock-agentcore-control", region)
        self.iam = client(session, "iam")
        self.s3 = client(session, "s3", region)
        self.lam = client(session, "lambda", region)

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------ storage
    def ensure_bucket(self) -> str:
        bucket = self.names.bucket
        try:
            self.s3.head_bucket(Bucket=bucket)
        except Exception:  # noqa: BLE001 - 404 / 403 both mean "create it" (a foreign owner fails below)
            kwargs = {} if self.region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": self.region}}
            self.s3.create_bucket(Bucket=bucket, **kwargs)
            self.s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
                "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
            self.s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
            self.log(f"  bucket {bucket} created")
        return bucket

    # ------------------------------------------------------------------ knowledge base
    def ensure_kb(self, *, profile: str | None) -> str:
        """01's knowledge base, through the release's own create_kb.py (run in its own process: it keeps
        module globals and a default boto3 session); documents are re-ingested only when they changed."""
        docs = self.release_dir / "pack" / "knowledge-base" / "docs"
        digest = docs_digest(docs)
        kb = self.state.get("knowledgeBase") or {}
        if kb.get("id") and kb.get("docsDigest") == digest:
            return str(kb["id"])
        script = (
            "import importlib.util, json, sys\n"
            "spec = importlib.util.spec_from_file_location('create_kb', sys.argv[1]); m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "kb = m.KnowledgeBasesForAmazonBedrock(); kb.inclusion_prefix = sys.argv[4]\n"
            "kb_id, ds_id = kb.create_or_retrieve_knowledge_base(sys.argv[2], 'ADLC direct mode knowledge base', data_bucket_name=sys.argv[3],"
            " embedding_model=m.EMBEDDING_MODEL)\n"
            "m.prune_stale_documents(kb.s3_client, sys.argv[3], sys.argv[4], sys.argv[5])\n"
            "kb.upload_directory(sys.argv[5], sys.argv[3], prefix=sys.argv[4])\n"
            "kb.synchronize_data(kb_id, ds_id)\n"
            "print('DIRECT_KB ' + json.dumps({'id': kb_id, 'dataSource': ds_id}))\n"
        )
        env = {**os.environ, **RETRY_ENV, "AWS_DEFAULT_REGION": self.region, "AWS_REGION": self.region, "PYTHONDONTWRITEBYTECODE": "1"}
        if profile:
            env["AWS_PROFILE"] = profile
        done = subprocess.run([tools_python(), "-c", script, str(self.release_dir / "knowledge-base" / "create_kb.py"), self.names.knowledge_base,
                               self.names.bucket, self.names.kb_prefix, str(docs)], capture_output=True, text=True, env=env, timeout=1800)
        line = next((ln for ln in reversed(done.stdout.splitlines()) if ln.startswith("DIRECT_KB ")), None)
        if done.returncode != 0 or line is None:
            raise ProvisionError(f"knowledge base: {(done.stderr or done.stdout).strip()[-600:]}")
        found = json.loads(line[len("DIRECT_KB "):])
        self.state["knowledgeBase"] = {**found, "name": self.names.knowledge_base, "docsDigest": digest}
        self.save()
        self.log(f"  knowledge base {found['id']} ingested ({len(list(docs.glob('*')))} documents)")
        return str(found["id"])

    # ------------------------------------------------------------------ roles
    def _ensure_role(self, name: str, trust: dict, policy_name: str, policy: dict, managed: Iterable[str] = ()) -> str:
        try:
            role = self.iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                                        Description="ADLC direct mode", Tags=[{"Key": k, "Value": v} for k, v in self.tags.items()])["Role"]
            created = True
        except Exception as exc:  # noqa: BLE001
            if _code_error(exc) != "EntityAlreadyExists":
                raise
            self.iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
            role = self.iam.get_role(RoleName=name)["Role"]
            created = False
        self.iam.put_role_policy(RoleName=name, PolicyName=policy_name, PolicyDocument=json.dumps(policy))
        for arn in managed:
            self.iam.attach_role_policy(RoleName=name, PolicyArn=arn)
        if created:
            self.sleep(12)  # a new role is not assumable everywhere at once
        return str(role["Arn"])

    # ------------------------------------------------------------------ tools
    def ensure_tools(self, kb_id: str) -> dict[str, str]:
        """02: the tools Lambda (KB id in its environment, no SSM) and the MCP Gateway with the pack's schema."""
        n = self.names
        lambda_trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                                                                 "Action": "sts:AssumeRole"}]}
        lambda_policy = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["bedrock:Retrieve"], "Resource": f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/{kb_id}"}]}
        role_arn = self._ensure_role(n.lambda_role, lambda_trust, "retrieve-knowledge-base", lambda_policy,
                                     ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"])
        code = tool_zip(self.release_dir)
        sha = base64.b64encode(hashlib.sha256(code).digest()).decode()
        env = {"Variables": {"KNOWLEDGE_BASE_ID": kb_id, "KB_ID_SSM_PARAM": ""}}
        try:
            current = self.lam.get_function(FunctionName=n.lambda_function)["Configuration"]
            if current.get("CodeSha256") != sha:
                self.lam.update_function_code(FunctionName=n.lambda_function, ZipFile=code)
                self.lam.get_waiter("function_updated_v2").wait(FunctionName=n.lambda_function)
            if (current.get("Environment") or {}).get("Variables") != env["Variables"]:
                self.lam.update_function_configuration(FunctionName=n.lambda_function, Environment=env)
                self.lam.get_waiter("function_updated_v2").wait(FunctionName=n.lambda_function)
            lambda_arn = str(current["FunctionArn"])
        except self.lam.exceptions.ResourceNotFoundException:
            for attempt in range(6):  # the new role may not be assumable by Lambda yet
                try:
                    created = self.lam.create_function(FunctionName=n.lambda_function, Runtime="python3.12", Role=role_arn,
                                                       Handler="scenario_tools_handler.lambda_handler", Code={"ZipFile": code},
                                                       Timeout=30, MemorySize=256, Environment=env, Tags=self.tags)
                    break
                except Exception as exc:  # noqa: BLE001
                    if _code_error(exc) != "InvalidParameterValueException" or attempt == 5:
                        raise
                    self.sleep(10)
            self.lam.get_waiter("function_active_v2").wait(FunctionName=n.lambda_function)
            lambda_arn = str(created["FunctionArn"])
        gateway_trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                                                                  "Action": "sts:AssumeRole",
                                                                  "Condition": {"StringEquals": {"aws:SourceAccount": self.account}}}]}
        gateway_policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": lambda_arn}]}
        gateway_role = self._ensure_role(n.gateway_role, gateway_trust, "invoke-tools-lambda", gateway_policy)
        gateway = next((g for g in self._paged("list_gateways", "items") if g.get("name") == n.gateway), None)
        if gateway is None:
            gateway_id = self.ctl.create_gateway(name=n.gateway, description="ADLC direct mode tools", roleArn=gateway_role,
                                                 protocolType="MCP", authorizerType="AWS_IAM", tags=self.tags)["gatewayId"]
        else:
            gateway_id = gateway["gatewayId"]
        _wait(lambda: self.ctl.get_gateway(gatewayIdentifier=gateway_id)["status"], ("READY",), what=f"Gateway {n.gateway}", sleep=self.sleep)
        gateway_arn = str(self.ctl.get_gateway(gatewayIdentifier=gateway_id)["gatewayArn"])
        schema = json.loads((self.release_dir / "pack" / "tools" / "schema.json").read_text(encoding="utf-8"))
        target_config = {"mcp": {"lambda": {"lambdaArn": lambda_arn, "toolSchema": {"inlinePayload": schema}}}}
        credentials = [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
        target = next((t for t in self._paged("list_gateway_targets", "items", gatewayIdentifier=gateway_id) if t.get("name") == n.target), None)
        if target is None:
            target_id = self.ctl.create_gateway_target(gatewayIdentifier=gateway_id, name=n.target, targetConfiguration=target_config,
                                                       credentialProviderConfigurations=credentials)["targetId"]
        else:
            target_id = target["targetId"]
            self.ctl.update_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id, name=n.target,
                                           targetConfiguration=target_config, credentialProviderConfigurations=credentials)
        _wait(lambda: self.ctl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)["status"], ("READY",),
              what=f"Gateway target {n.target}", sleep=self.sleep)
        self.state["tools"] = {"lambdaArn": lambda_arn, "gatewayId": gateway_id, "gatewayArn": gateway_arn, "targetId": target_id}
        self.save()
        return self.state["tools"]

    def _paged(self, op: str, key: str, **kwargs: Any) -> list[dict[str, Any]]:
        items, token = [], None
        while True:
            page = getattr(self.ctl, op)(**kwargs, **({"nextToken": token} if token else {}))
            items += page.get(key) or []
            token = page.get("nextToken")
            if not token:
                return items

    # ------------------------------------------------------------------ memory
    def ensure_memory(self) -> dict[str, Any]:
        """04 / 05: a memory with the Workshop's SEMANTIC and USER_PREFERENCE strategies and their namespaces."""
        found = next((m for m in self._paged("list_memories", "memories") if str(m.get("id", "")).startswith(self.names.memory + "-")), None)
        if found is None:
            strategies = [{"semanticMemoryStrategy": {"name": "Semantic", "namespaces": [MEMORY_NAMESPACES["SEMANTIC"][0]]}},
                          {"userPreferenceMemoryStrategy": {"name": "Userpreference", "namespaces": [MEMORY_NAMESPACES["USER_PREFERENCE"][0]]}}]
            memory_id = self.ctl.create_memory(name=self.names.memory, description="ADLC direct mode memory", eventExpiryDuration=30,
                                               memoryStrategies=strategies, tags=self.tags)["memory"]["id"]
        else:
            memory_id = found["id"]
        _wait(lambda: self.ctl.get_memory(memoryId=memory_id)["memory"]["status"], ("ACTIVE",), what=f"memory {self.names.memory}",
              attempts=120, sleep=self.sleep)
        memory = self.ctl.get_memory(memoryId=memory_id)["memory"]
        retrieval = {}
        for strategy in memory.get("strategies") or []:
            namespace, top_k = MEMORY_NAMESPACES.get(str(strategy.get("type")), (None, None))
            if namespace:
                retrieval[namespace] = {"topK": top_k, "strategyId": strategy["strategyId"]}
        self.state["memory"] = {"id": memory_id, "arn": memory["arn"], "retrievalConfig": retrieval}
        self.save()
        return self.state["memory"]

    # ------------------------------------------------------------------ skills
    def ensure_skills(self) -> list[dict[str, Any]]:
        """03's skills, uploaded under the pack's prefix; the Harness reads each from its S3 prefix.

        A SKILL.md without YAML frontmatter (releases built before 2026-10-01) is left out, as the class got it:
        the Workshop's runtime skipped it ("no skills were loaded"), while a Harness given it from S3 fails
        every call ("SKILL.md … has no YAML frontmatter"). The names left out are in ``skipped_skills``."""
        sources = []
        self.skipped_skills = []
        for path in sorted((self.release_dir / "pack" / "skills").glob("*/SKILL.md")):
            body = path.read_bytes()
            if not body.lstrip(b"\xef\xbb\xbf").startswith(b"---"):
                self.skipped_skills.append(path.parent.name)
                self.log(f"  skill {path.parent.name}: no frontmatter, left out (the Workshop's runtime did not load it either)")
                continue
            key = f"{self.names.skills_prefix}{path.parent.name}/SKILL.md"
            self.s3.put_object(Bucket=self.names.bucket, Key=key, Body=body, ContentType="text/markdown")
            sources.append({"s3": {"uri": f"s3://{self.names.bucket}/{self.names.skills_prefix}{path.parent.name}/"}})
        return sources

    # ------------------------------------------------------------------ harness
    def ensure_harness_role(self, gateway_arn: str, memory_arn: str) -> str:
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                                                          "Action": "sts:AssumeRole", "Condition": {
                                                              "StringEquals": {"aws:SourceAccount": self.account},
                                                              "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"}}}]}
        policy = harness_role_policy(account=self.account, region=self.region, names=self.names, gateway_arn=gateway_arn, memory_arn=memory_arn)
        return self._ensure_role(self.names.harness_role, trust, "harness-execution", policy)

    def harness_config(self, *, prompt: str, role_arn: str, tools: Mapping[str, str], memory: Mapping[str, Any],
                       skills: list[dict[str, Any]], model: str) -> dict[str, Any]:
        return {
            "executionRoleArn": role_arn,
            "model": {"bedrockModelConfig": {"modelId": model}},
            "systemPrompt": [{"text": prompt}],
            "tools": [{"type": "agentcore_gateway", "name": self.names.target, "config": {"agentCoreGateway": {"gatewayArn": tools["gatewayArn"]}}}],
            "allowedTools": [f"@{self.names.target}/*"],
            "skills": skills,
            "memory": {"agentCoreMemoryConfiguration": {"arn": memory["arn"], "actorId": "{actorId}", "retrievalConfig": memory["retrievalConfig"]}},
            "environmentVariables": dict(HARNESS_ENVIRONMENT),
            **HARNESS_LIMITS,
        }

    def ensure_harness(self, config: Mapping[str, Any]) -> dict[str, str]:
        found = next((h for h in self._paged("list_harnesses", "harnesses") if h.get("harnessName") == self.names.harness), None)
        if found is None:
            for attempt in range(6):  # a fresh role can be refused for a few seconds
                try:
                    harness = self.ctl.create_harness(harnessName=self.names.harness, tags=self.tags, **config)["harness"]
                    break
                except Exception as exc:  # noqa: BLE001
                    if _code_error(exc) not in ("ValidationException", "AccessDeniedException") or attempt == 5:
                        raise
                    self.sleep(10)
            harness_id = harness["harnessId"]
        else:
            harness_id = found["harnessId"]
            self.update_harness(harness_id, {k: v for k, v in config.items() if k != "memory"})
        self.wait_harness(harness_id)
        harness = self.ctl.get_harness(harnessId=harness_id)["harness"]
        runtime = ((harness.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {})
        self.state["harness"] = {"id": harness_id, "arn": harness["arn"], "runtimeId": runtime.get("agentRuntimeId"),
                                 "runtimeArn": runtime.get("agentRuntimeArn")}
        self.save()
        return self.state["harness"]

    def update_harness(self, harness_id: str, fields: Mapping[str, Any]) -> None:
        """UpdateHarness with only ``fields`` (the others stay): the in-place update 10 and 12 use."""
        self.ctl.update_harness(harnessId=harness_id, **fields)
        self.wait_harness(harness_id)

    def wait_harness(self, harness_id: str) -> None:
        _wait(lambda: self.ctl.get_harness(harnessId=harness_id)["harness"]["status"], ("READY",), what=f"Harness {harness_id}",
              attempts=90, sleep=self.sleep)
