"""App-side controlled sync: preflight → bundle → upload → custom SSM document → result.

Everything here runs on the SA's machine with the SA's *own* AWS CLI profile (SSO, role, or a
workshop-issued temporary profile). The engine never reads, stores or prints credential values;
boto3 resolves them through the standard chain. The App only persists the profile name, region,
expected account id and stack names.

Fail-closed preflight (every check must pass before anything is uploaded or sent):

1. identity: ``sts:GetCallerIdentity`` account == expected account id
2. credential TTL (when the session exposes an expiry) ≥ required margin
3. workshop stack exists and is complete; outputs carry ``InstanceId`` and a bucket
4. add-ons stack exists and is complete; outputs name the custom document and contract, and its
   ``RunStepDocumentSha256`` output equals the local ``sync/ssm/WorkshopCustomizerRunStep.json`` (a
   stale stack runs an older step-output parser, so per-case evidence would never reach the report)
5. target contract (SSM Parameter) parses, matches the pinned template commit, the instance
   and the bucket, and names the custom document
6. the instance is an online, Linux SSM managed instance
7. pre-execution guard: the pack's scenario AWS resources do not exist yet (no KB id / gateway
   ARN parameters under the pack's SSM prefix, no agent runtime with the pack's agent name)

Only ``ssm:SendCommand`` on the custom document is ever used — never ``AWS-RunShellScript``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .direct.names import direct_runtime
from .render import MANIFEST_NAME, verify_release

PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{2,40}$")
RELEASE_PREFIX = "customizer-releases/"
RUN_DOCUMENT_NAME = "WorkshopCustomizerRunStep"
#: The checked-in RunStep document the add-ons stack must be running (the engine lives in the checkout).
RUN_DOCUMENT_PATH = Path(__file__).resolve().parents[2] / "sync" / "ssm" / f"{RUN_DOCUMENT_NAME}.json"
RUN_DOCUMENT_SHA_OUTPUT = "RunStepDocumentSha256"
COMPLETE_STACK_STATUSES = {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "IMPORT_COMPLETE"}
TERMINAL_COMMAND_STATUSES = {"Success", "Cancelled", "TimedOut", "Failed", "Cancelling"}
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class SyncError(Exception):
    """Fail-closed: raised when preflight, upload or command dispatch cannot be trusted."""


def document_sha256(document: Any) -> str:
    """Canonical sha256 of an SSM document's content: sorted keys, compact separators.

    ``sync/cfn/build_addons_template.py`` publishes the same digest as the add-ons output
    ``RunStepDocumentSha256`` (a test pins that the two agree).
    """
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def run_document_sha256(path: Path | str | None = None) -> str:
    """The canonical sha256 of the local RunStep document; ``SyncError`` when it cannot be read."""
    source = Path(path) if path is not None else RUN_DOCUMENT_PATH
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SyncError(f"cannot read the local RunStep document {source}: {exc}") from exc
    return document_sha256(document)


@dataclass
class TargetConfig:
    profile: str
    region: str
    expected_account_id: str
    workshop_stack: str = "workshop-infra"
    addons_stack: str = "workshop-customizer-addons"
    contract_parameter: str = "/workshop-customizer/target-contract"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"\d{12}", self.expected_account_id):
            raise SyncError("expected account id must be 12 digits")
        if not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", self.region):
            raise SyncError("region has an unexpected shape")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.profile):
            raise SyncError("profile name has an unexpected shape")


class AwsClients(Protocol):
    """The subset of boto3 clients the sync needs (duck-typed so tests can stub it)."""

    sts: Any
    cloudformation: Any
    ssm: Any
    s3: Any
    agentcore: Any

    def credential_expiry(self) -> datetime | None: ...


def make_clients(cfg: TargetConfig) -> AwsClients:  # pragma: no cover - needs real AWS
    import boto3

    session = boto3.Session(profile_name=cfg.profile, region_name=cfg.region)

    class _Clients:
        sts = session.client("sts")
        cloudformation = session.client("cloudformation")
        ssm = session.client("ssm")
        s3 = session.client("s3")
        agentcore = session.client("bedrock-agentcore-control")

        @staticmethod
        def credential_expiry() -> datetime | None:
            creds = session.get_credentials()
            expiry = getattr(creds, "_expiry_time", None)
            return expiry if isinstance(expiry, datetime) else None

    return _Clients()


@dataclass
class Check:
    name: str
    status: str  # ok | fail | skip
    detail: str = ""


@dataclass
class Preflight:
    checks: list[Check] = field(default_factory=list)
    account_id: str = ""
    identity_arn: str = ""
    instance_id: str = ""
    bucket: str = ""
    document_name: str = ""
    run_document_name: str = ""
    run_document_sha256: str = ""
    contract: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(c.status != "fail" for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, "ok" if ok else "fail", detail))

    def skip(self, name: str, detail: str) -> None:
        self.checks.append(Check(name, "skip", detail))

    def failures(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if c.status == "fail"]


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None) or {}
    return str(response.get("Error", {}).get("Code", ""))


def _outputs(stack: dict[str, Any]) -> dict[str, str]:
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


#: add-ons stack parameter -> pack namespace key. The add-ons' WorkshopExecutionPolicy grants the Workshop
#: EC2 its AWS permissions for exactly these names, so the stack must be deployed for the release's
#: namespace (tools/deploy_addons.py --scenario <that pack's scenario.yaml>): one namespace per environment.
ADDONS_NAMESPACE_PARAMETERS: tuple[tuple[str, str], ...] = (
    ("AgentName", "agentName"), ("ToolTargetName", "toolTargetName"), ("GatewayName", "gatewayName"),
    ("ToolFunctionName", "lambdaFunctionName"), ("KnowledgeBaseName", "knowledgeBaseName"),
    ("SsmParameterPrefix", "ssmParameterPrefix"),
)


def addons_namespace_mismatches(addons: dict[str, Any], pack_namespace: dict[str, str]) -> list[str]:
    """Where the deployed add-ons stack's namespace parameters differ from the pack namespace."""
    params = {p.get("ParameterKey"): p.get("ParameterValue") for p in addons.get("Parameters") or [] if isinstance(p, dict)}
    return [f"{param} {params.get(param)!r} vs pack {pack_namespace.get(key)!r}"
            for param, key in ADDONS_NAMESPACE_PARAMETERS
            if key in pack_namespace and params.get(param) != pack_namespace.get(key)]


def _describe_stack(clients: AwsClients, name: str) -> dict[str, Any] | None:
    try:
        stacks = clients.cloudformation.describe_stacks(StackName=name)["Stacks"]
    except Exception as exc:  # noqa: BLE001 - boto raises ClientError subclasses
        if "does not exist" in str(exc) or _error_code(exc) == "ValidationError":
            return None
        raise
    return stacks[0] if stacks else None


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def preflight(
    cfg: TargetConfig,
    clients: AwsClients,
    *,
    expected_template_commit: str,
    pack_namespace: dict[str, str] | None = None,
    required_ttl: timedelta = timedelta(minutes=30),
    now: datetime | None = None,
    local_run_document_sha256: str | None = None,
    previous_namespaces: list[dict[str, str]] | tuple[dict[str, str], ...] = (),
) -> Preflight:
    """Every sync precondition, fail-closed. ``previous_namespaces`` are namespaces an earlier release of
    this project synced into the environment: when the pack's namespace changed, their resources must be
    gone too (otherwise the old KB, Gateway and runtime linger unnoticed)."""
    result = Preflight()
    now = now or datetime.now(timezone.utc)

    # 1. identity
    try:
        ident = clients.sts.get_caller_identity()
        result.account_id = str(ident.get("Account", ""))
        result.identity_arn = str(ident.get("Arn", ""))
        result.add("identity.account", result.account_id == cfg.expected_account_id, f"caller account {result.account_id} vs expected {cfg.expected_account_id}")
    except Exception as exc:  # noqa: BLE001
        result.add("identity.account", False, f"sts:GetCallerIdentity failed: {exc}")
        return result

    # 2. credential TTL
    expiry = clients.credential_expiry()
    if expiry is None:
        result.skip("credentials.ttl", "session exposes no expiry (long-lived or role-provided credentials)")
    else:
        remaining = expiry - now
        result.add("credentials.ttl", remaining >= required_ttl, f"{int(remaining.total_seconds() // 60)} min remaining; need ≥ {int(required_ttl.total_seconds() // 60)}")

    # 3. workshop stack
    workshop = _describe_stack(clients, cfg.workshop_stack)
    if workshop is None:
        result.add("stack.workshop", False, f"stack {cfg.workshop_stack} not found in {cfg.region}")
        return result
    result.add("stack.workshop", workshop.get("StackStatus") in COMPLETE_STACK_STATUSES, f"status {workshop.get('StackStatus')}")
    outputs = _outputs(workshop)
    result.instance_id = outputs.get("InstanceId", "")
    result.bucket = outputs.get("SkillsBucketName") or outputs.get("DataBucketName", "")
    result.add("stack.workshop.outputs", bool(result.instance_id and result.bucket), "needs InstanceId and SkillsBucketName/DataBucketName outputs")

    # 4. add-ons stack
    addons = _describe_stack(clients, cfg.addons_stack)
    if addons is None:
        result.add("stack.addons", False, f"stack {cfg.addons_stack} not found; deploy sync/cfn/customizer-addons.json first")
        return result
    result.add("stack.addons", addons.get("StackStatus") in COMPLETE_STACK_STATUSES, f"status {addons.get('StackStatus')}")
    addon_outputs = _outputs(addons)
    result.document_name = addon_outputs.get("ApplyDocumentName", "")
    result.run_document_name = addon_outputs.get("RunStepDocumentName", "")
    result.add(
        "stack.addons.outputs",
        bool(result.document_name)
        and result.run_document_name == RUN_DOCUMENT_NAME
        and addon_outputs.get("TargetContractParameter") == cfg.contract_parameter,
        "needs ApplyDocumentName, RunStepDocumentName and TargetContractParameter outputs",
    )
    result.run_document_sha256 = addon_outputs.get(RUN_DOCUMENT_SHA_OUTPUT, "")
    try:
        local_sha = local_run_document_sha256 or run_document_sha256()
    except SyncError as exc:
        result.add("stack.addons.runDocument", False, str(exc))
    else:
        deployed = result.run_document_sha256
        if deployed == local_sha:
            detail = f"add-ons RunStep document {local_sha[:12]} matches sync/ssm/{RUN_DOCUMENT_NAME}.json"
        else:
            seen = f"stack {deployed[:12]}" if deployed else f"the stack has no {RUN_DOCUMENT_SHA_OUTPUT} output"
            detail = (f"add-ons RunStep document is stale ({seen}; local {local_sha[:12]}): redeploy "
                      "sync/cfn/customizer-addons.json (tools/deploy_addons.py) so per-case evidence reaches the report")
        result.add("stack.addons.runDocument", deployed == local_sha, detail)
    if pack_namespace:
        mismatches = addons_namespace_mismatches(addons, pack_namespace)
        detail = (f"add-ons execution permissions are bound to this pack's namespace ({pack_namespace.get('agentName')})"
                  if not mismatches else
                  "the add-ons stack grants the Workshop EC2 its permissions for another pack namespace ("
                  + "; ".join(mismatches) + "): steps 01/02 would be denied. Redeploy it for this pack with "
                  "python tools/deploy_addons.py --scenario <this project's scenario.yaml> (one pack namespace per environment)")
        result.add("stack.addons.namespace", not mismatches, detail)

    # 5. target contract
    try:
        raw = clients.ssm.get_parameter(Name=cfg.contract_parameter)["Parameter"]["Value"]
        contract = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        result.add("contract.readable", False, f"cannot read {cfg.contract_parameter}: {exc}")
        return result
    result.contract = contract
    result.add("contract.schema", contract.get("schema") == "workshop-customizer/target-contract/1", str(contract.get("schema")))
    result.add("contract.templateCommit", contract.get("templateCommit") == expected_template_commit, f"contract {str(contract.get('templateCommit'))[:12]} vs pack {expected_template_commit[:12]}")
    result.add("contract.instance", contract.get("instanceId") == result.instance_id, f"contract {contract.get('instanceId')} vs stack {result.instance_id}")
    expected_prefix = f"s3://{result.bucket}/{RELEASE_PREFIX}"
    result.add("contract.releasePrefix", expected_prefix in contract.get("allowedReleasePrefixes", []), expected_prefix)
    result.add("contract.document", contract.get("documentName") == result.document_name, f"contract {contract.get('documentName')} vs stack {result.document_name}")
    result.add("contract.runDocument", contract.get("runDocumentName") == result.run_document_name == RUN_DOCUMENT_NAME, f"contract {contract.get('runDocumentName')} vs stack {result.run_document_name}")

    # 6. managed instance online
    try:
        info = clients.ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [result.instance_id]}])
        entries = info.get("InstanceInformationList", [])
        online = bool(entries) and entries[0].get("PingStatus") == "Online" and entries[0].get("PlatformType") == "Linux"
        result.add("instance.online", online, f"{entries[0].get('PingStatus') if entries else 'not managed'}")
    except Exception as exc:  # noqa: BLE001
        result.add("instance.online", False, f"describe_instance_information failed: {exc}")

    # 7. pre-execution guard: scenario AWS resources must not exist yet (nor those of a namespace this
    #    project synced before, when the pack's namespace changed since)
    if pack_namespace:
        result.checks.extend(resource_guard(clients, pack_namespace))
        seen: list[dict[str, str]] = []
        for previous in previous_namespaces:
            if not isinstance(previous, dict) or previous == pack_namespace or previous in seen:
                continue
            seen.append(previous)
            if not previous.get("ssmParameterPrefix") or not previous.get("agentName"):
                continue
            for check in resource_guard(clients, previous):
                detail = (f"previous namespace {previous['agentName']}: {check.detail}" + (
                          "; remove the earlier release's resources first: with the add-ons bound to that namespace "
                          "(tools/deploy_addons.py --scenario <its scenario.yaml>), run its 99-cleanup.sh "
                          "--scenario-only on the Workshop instance" if check.status == "fail" else ""))
                result.checks.append(Check("guard.previous." + check.name[len("guard."):], check.status, detail))

    return result


def resource_guard(clients: AwsClients, pack_namespace: dict[str, str]) -> list[Check]:
    """Checks that the scenario's AWS resources do NOT exist (KB id / gateway ARN parameters, agent runtime).

    Used before a sync (MVP sync is pre-execution only) and after a file rollback: files restored while
    these resources still exist is *not* a rollback of the scenario — the caller must say so.
    Fails closed: anything that cannot be verified counts as a failure.
    """
    checks: list[Check] = []
    prefix = pack_namespace["ssmParameterPrefix"]
    for suffix in ("knowledge_base_id", "gateway_arn"):
        name = f"{prefix}/{suffix}"
        try:
            clients.ssm.get_parameter(Name=name)
            checks.append(Check(f"guard.{suffix}", "fail", f"{name} exists: scenario resources already created"))
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) == "ParameterNotFound":
                checks.append(Check(f"guard.{suffix}", "ok", f"{name} absent"))
            else:
                checks.append(Check(f"guard.{suffix}", "fail", f"cannot verify {name}: {exc}"))
    try:
        runtimes = clients.agentcore.list_agent_runtimes().get("agentRuntimes", [])
        agent = pack_namespace["agentName"]
        # A pack's direct-mode Harness (direct.names) is not a Workshop resource and no Workshop script selects it.
        clash = [r.get("agentRuntimeName") for r in runtimes
                 if agent in str(r.get("agentRuntimeName", "")) and not direct_runtime(str(r.get("agentRuntimeName")), agent)]
        checks.append(Check("guard.agentRuntime", "ok" if not clash else "fail", f"runtimes named like {pack_namespace['agentName']}: {clash or 'none'}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("guard.agentRuntime", "fail", f"cannot verify agent runtimes: {exc}"))
    return checks


def resources_present(checks: list[Check]) -> bool:
    """True when any guard check failed (resources exist or could not be verified)."""
    return any(c.name.startswith("guard.") and c.status == "fail" for c in checks)


# ---------------------------------------------------------------------------
# Bundle, upload, dispatch
# ---------------------------------------------------------------------------


def build_bundle(release_dir: Path, out_zip: Path) -> tuple[Path, str]:
    """Zip a verified release deterministically (fixed timestamps, sorted entries)."""
    verify_release(release_dir)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in release_dir.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(path.relative_to(release_dir).as_posix(), date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if path.suffix == ".sh" else 0o644) << 16
            archive.writestr(info, path.read_bytes())
    digest = hashlib.sha256(out_zip.read_bytes()).hexdigest()
    (out_zip.with_suffix(".sha256")).write_text(f"{digest}  release.zip\n", encoding="utf-8")
    return out_zip, digest


def release_key(project: str, version: str) -> str:
    if not PROJECT_RE.match(project):
        raise SyncError("project id must be kebab-case (3-41 chars)")
    return f"{RELEASE_PREFIX}{project}/{version}/release.zip"


def upload_bundle(clients: AwsClients, bucket: str, project: str, version: str, bundle: Path, sha256: str, release_dir: Path) -> str:
    key = release_key(project, version)
    base = key[: -len("release.zip")]
    with bundle.open("rb") as body:
        clients.s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/zip", Metadata={"sha256": sha256, "version": version})
    clients.s3.put_object(Bucket=bucket, Key=base + "release.sha256", Body=f"{sha256}  release.zip\n".encode("utf-8"), ContentType="text/plain")
    clients.s3.put_object(Bucket=bucket, Key=base + MANIFEST_NAME, Body=(release_dir / MANIFEST_NAME).read_bytes(), ContentType="application/json")
    return f"s3://{bucket}/{key}"


def send_apply(clients: AwsClients, *, instance_id: str, document_name: str, source_uri: str, sha256: str, version: str, watchdog_minutes: int = 20) -> str:
    if not source_uri.startswith("s3://") or f"/{RELEASE_PREFIX}" not in source_uri:
        raise SyncError("refusing to dispatch: source is not inside the release prefix")
    response = clients.ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName=document_name,
        Comment=f"workshop-customizer apply {version}"[:100],
        TimeoutSeconds=1200,
        Parameters={
            "Action": ["apply"],
            "SourceUri": [source_uri],
            "ExpectedSha256": [sha256],
            "ExpectedVersion": [version],
            "WatchdogMinutes": [str(int(watchdog_minutes))],
        },
    )
    return response["Command"]["CommandId"]


def send_action(clients: AwsClients, *, instance_id: str, document_name: str, action: str, version: str = "", reason: str = "operator request") -> str:
    if action not in {"commit", "rollback", "status"}:
        raise SyncError("action must be commit, rollback or status")
    params: dict[str, list[str]] = {"Action": [action]}
    if version:
        params["ExpectedVersion"] = [version]
    if action == "rollback":
        params["Reason"] = [re.sub(r"[^A-Za-z0-9 ._:-]", " ", reason)[:120] or "operator request"]
    response = clients.ssm.send_command(InstanceIds=[instance_id], DocumentName=document_name, Comment=f"workshop-customizer {action}"[:100], TimeoutSeconds=600, Parameters=params)
    return response["Command"]["CommandId"]


def wait_for_command(clients: AwsClients, command_id: str, instance_id: str, *, timeout: float = 1500.0, poll: float = 5.0, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Poll GetCommandInvocation; return the applier's JSON result plus SSM status."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            inv = clients.ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) == "InvocationDoesNotExist":
                inv = {"Status": "Pending"}
            else:
                raise
        status = inv.get("Status", "Pending")
        if status in TERMINAL_COMMAND_STATUSES:
            stdout = inv.get("StandardOutputContent", "") or ""
            try:
                payload = json.loads(stdout[stdout.index("{"):]) if "{" in stdout else {}
            except json.JSONDecodeError:
                payload = {}
            return {"commandId": command_id, "ssmStatus": status, "result": payload, "stderr": (inv.get("StandardErrorContent") or "")[-2000:]}
        if time.monotonic() >= deadline:
            raise SyncError(f"command {command_id} did not finish within {int(timeout)}s (status {status})")
        sleep(poll)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class SyncPlan:
    project: str
    version: str
    pack_id: str
    account_id: str
    region: str
    instance_id: str
    bucket: str
    document_name: str
    source_uri: str
    sha256: str
    template_commit: str
    preflight: Preflight


def sync_release(
    cfg: TargetConfig,
    clients: AwsClients,
    release_dir: Path,
    *,
    project: str,
    work_dir: Path,
    confirm: Callable[[SyncPlan], bool],
    watchdog_minutes: int = 20,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Preflight → confirm → upload → apply → wait. Returns the applier result and evidence."""
    manifest = verify_release(release_dir)
    namespace = manifest["namespace"]
    pf = preflight(cfg, clients, expected_template_commit=manifest["templateCommit"], pack_namespace=namespace)
    if not pf.ok:
        raise SyncError("preflight failed:\n- " + "\n- ".join(pf.failures()))

    bundle, sha256 = build_bundle(release_dir, work_dir / f"{manifest['version']}.zip")
    plan = SyncPlan(
        project=project,
        version=manifest["version"],
        pack_id=manifest["packId"],
        account_id=pf.account_id,
        region=cfg.region,
        instance_id=pf.instance_id,
        bucket=pf.bucket,
        document_name=pf.document_name,
        source_uri=f"s3://{pf.bucket}/{release_key(project, manifest['version'])}",
        sha256=sha256,
        template_commit=manifest["templateCommit"],
        preflight=pf,
    )
    if not confirm(plan):
        return {"status": "not-confirmed", "plan": plan}

    source_uri = upload_bundle(clients, pf.bucket, project, manifest["version"], bundle, sha256, release_dir)
    command_id = send_apply(clients, instance_id=pf.instance_id, document_name=pf.document_name, source_uri=source_uri, sha256=sha256, version=manifest["version"], watchdog_minutes=watchdog_minutes)
    outcome = wait_for_command(clients, command_id, pf.instance_id, sleep=sleep)
    return {
        "status": outcome["result"].get("status", outcome["ssmStatus"].lower()),
        "plan": plan,
        "commandId": command_id,
        "ssmStatus": outcome["ssmStatus"],
        "applier": outcome["result"],
        "stderr": outcome["stderr"],
    }
