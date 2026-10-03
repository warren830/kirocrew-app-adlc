"""Knowledge bases: Bedrock managed knowledge bases, their documents, a retrieval playground, and attaching one to an
agent.

A console KB is ``type: MANAGED`` (the service owns the vector store, the embeddings and the reranking; Launchpad's
choice): ``CreateKnowledgeBase`` with the console's KB role (``adlc-console-kb-role``, assumed by bedrock.amazonaws.com
to read the sources), then an S3 data source through the managed connector — the console bucket's
``kb/<kb id>/`` prefix for uploaded files, or a bucket and prefix of your own (the role gets a per-KB read grant).
KB creation takes minutes, so the tail (wait ACTIVE → data source → first sync) runs as a console job. Every
KB in the workspace is listed (managed or vector); only console-created ones can be deleted.

Attaching a KB to a Harness goes through the console's KB Gateway (``adlc-console-kb-gateway``) and its native
knowledge-base connector, so no code of ours sits in the retrieval path: one ``Retrieve`` target per KB
(``<kb slug>-<kb id>``) and, per agent, one ``AgenticRetrieveStream`` target over all of its KBs (``agentic-<agent>``:
the service plans sub-queries, retrieves and reranks). The Harness gets the Gateway as its tool ``adlckb`` with
allowedTools naming only its own targets, so an agent never sees another agent's KBs. Which KBs each Harness has is
kept in the console store (``kb_attachments_<workspace>``). A Harness the console did not create is changed only when
the caller confirms (``acknowledged``).

In a workspace with a permissions boundary (a spoke workspace) the KB and KB Gateway roles are created with it (a
role made before gets it first), and the boundary lets them read S3 only in the console bucket: a data source in
another bucket is ingested only once the account's owner has added that bucket to the boundary. Both roles are on the
console's IAM path (``agents.ROLE_PATH``); an existing role of their name is used only when the console may adopt it
(``agents.adopt``: a console role made before roles had a path, on ``/``, only in a workspace without a boundary).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Any, Mapping, Sequence

from ..direct.aws import client
from .agents import CONSOLE_TAG, adopt, ensure_boundary, ensure_role, get_agent, put_inline_policy
from .common import pages as _pages
from .workspaces import boundary_of

KB_ROLE = "adlc-console-kb-role"
GATEWAY = "adlc-console-kb-gateway"
GATEWAY_ROLE = "adlc-console-kb-gateway"
#: The Harness's name for the KB Gateway tool (its tools reach the model as ``<target>___<operation>``).
TOOL = "adlckb"
CONNECTOR = "bedrock-knowledge-bases"
NAME = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,99}$")
MAX_FILES_BYTES = 18 * 1024 * 1024


class KnowledgeError(ValueError):
    pass


def console_bucket(account: str, region: str) -> str:
    return f"adlc-console-{account}-{region}"


def ensure_bucket(session: Any, account: str, region: str) -> str:
    """The console's bucket (KB sources and uploads, skills, deployment sources): created once, versioned, private,
    encrypted, tagged; always checked to be this account's (``ExpectedBucketOwner``: the name is guessable)."""
    s3 = client(session, "s3", region)
    name = console_bucket(account, region)
    try:
        s3.head_bucket(Bucket=name, ExpectedBucketOwner=account)
        return name
    except Exception as exc:  # noqa: BLE001
        if "404" not in str(exc) and "NoSuchBucket" not in str(exc) and "Not Found" not in str(exc):
            raise
    kwargs: dict[str, Any] = {"Bucket": name}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**kwargs)
    owner = {"Bucket": name, "ExpectedBucketOwner": account}
    s3.put_public_access_block(**owner, PublicAccessBlockConfiguration={"BlockPublicAcls": True, "IgnorePublicAcls": True,
                                                                         "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_encryption(**owner, ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    s3.put_bucket_versioning(**owner, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_tagging(**owner, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in CONSOLE_TAG.items()]})
    return name


def _ensure_role(session: Any, name: str, trust: Mapping[str, Any], policy_name: str, policy: Mapping[str, Any], boundary: str | None = None) -> str:
    """The KB's or the KB Gateway's role (``agents.ensure_role``; one the console may not adopt is a KnowledgeError)."""
    return ensure_role(session, name, trust, policy_name, policy, description="ADLC console", boundary=boundary, error=KnowledgeError)


def _s3_read(bucket: str, prefix: str) -> dict[str, Any]:
    listing: dict[str, Any] = {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}"}
    if prefix:
        listing["Condition"] = {"StringLike": {"s3:prefix": [f"{prefix}*"]}}
    return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/{prefix}*"}, listing]}


def ensure_kb_role(session: Any, account: str, region: str, boundary: str | None = None) -> str:
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock.amazonaws.com"}, "Action": "sts:AssumeRole",
                                                      "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}
    return _ensure_role(session, KB_ROLE, trust, "console-bucket-kb", _s3_read(console_bucket(account, region), "kb/"), boundary=boundary)


def _kb(session: Any, region: str) -> Any:
    return client(session, "bedrock-agent", region)


def list_kbs(session: Any, region: str) -> list[dict[str, Any]]:
    agent = _kb(session, region)
    return [{"id": k.get("knowledgeBaseId"), "name": k.get("name"), "status": k.get("status"), "description": k.get("description"),
             "updatedAt": str(k.get("updatedAt") or "")} for k in _pages(agent.list_knowledge_bases, "knowledgeBaseSummaries")]


def _location(ds: Mapping[str, Any]) -> tuple[str | None, str]:
    conf = ds.get("dataSourceConfiguration") or {}
    params = (((conf.get("managedKnowledgeBaseConnectorConfiguration") or {}).get("connectorParameters")) or {})
    if isinstance(params, str):  # sent as a document, read back as a JSON string (live 2026-10-01)
        try:
            params = json.loads(params)
        except ValueError:
            params = {}
    if params:
        conn = params.get("connectionConfiguration") or {}
        prefixes = (params.get("filterConfiguration") or {}).get("inclusionPrefixes") or [""]
        return conn.get("bucketName"), prefixes[0]
    s3 = conf.get("s3Configuration") or {}
    arn = str(s3.get("bucketArn") or "")
    return (arn.rsplit(":", 1)[-1] or None), ((s3.get("inclusionPrefixes") or [""])[0])


def detail(session: Any, region: str, kb_id: str) -> dict[str, Any]:
    agent = _kb(session, region)
    kb = agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
    tags = agent.list_tags_for_resource(resourceArn=kb["knowledgeBaseArn"]).get("tags") or {}
    sources = []
    for summary in agent.list_data_sources(knowledgeBaseId=kb_id).get("dataSourceSummaries") or []:
        ds = agent.get_data_source(knowledgeBaseId=kb_id, dataSourceId=summary["dataSourceId"])["dataSource"]
        bucket, prefix = _location(ds)
        jobs = agent.list_ingestion_jobs(knowledgeBaseId=kb_id, dataSourceId=ds["dataSourceId"], maxResults=5,
                                         sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"}).get("ingestionJobSummaries") or []
        documents = []
        try:
            for doc in agent.list_knowledge_base_documents(knowledgeBaseId=kb_id, dataSourceId=ds["dataSourceId"], maxResults=100).get("documentDetails") or []:
                ident = doc.get("identifier") or {}
                documents.append({"uri": ((ident.get("s3") or {}).get("uri")) or ident.get("custom", {}).get("id"), "status": doc.get("status"),
                                  "reason": doc.get("statusReason"), "updatedAt": str(doc.get("updatedAt") or "")})
        except Exception:  # noqa: BLE001 - not every data source type lists documents
            documents = None
        sources.append({"id": ds["dataSourceId"], "name": ds.get("name"), "status": ds.get("status"), "bucket": bucket, "prefix": prefix,
                        "ingestion": [{"id": j.get("ingestionJobId"), "status": j.get("status"), "startedAt": str(j.get("startedAt") or ""),
                                       "statistics": j.get("statistics")} for j in jobs], "documents": documents})
    conf = kb.get("knowledgeBaseConfiguration") or {}
    return {"id": kb["knowledgeBaseId"], "name": kb.get("name"), "status": kb.get("status"), "type": conf.get("type"), "arn": kb["knowledgeBaseArn"],
            "description": kb.get("description"), "failureReasons": kb.get("failureReasons") or [], "sources": sources,
            "console": tags.get("adlc:console") == "1"}


def _source(account: str, region: str, kb_id: str, source: Mapping[str, Any]) -> tuple[str, str]:
    mode = str(source.get("mode") or "upload")
    if mode == "upload":
        return console_bucket(account, region), f"kb/{kb_id}/"
    if mode == "s3":
        bucket, prefix = str(source.get("bucket") or ""), str(source.get("prefix") or "")
        if not re.match(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", bucket):
            raise KnowledgeError("source.bucket: an S3 bucket name")
        return bucket, prefix.lstrip("/")
    raise KnowledgeError("source.mode: upload or s3")


def create_data_source(session: Any, account: str, region: str, kb_id: str, source: Mapping[str, Any], boundary: str | None = None) -> str:
    """A source in another bucket gets its own read grant on the KB role. With a permissions boundary (a spoke
    workspace) the role reads only what the boundary lets it: the console bucket, unless the account's owner widened it."""
    bucket, prefix = _source(account, region, kb_id, source)
    agent = _kb(session, region)
    for summary in agent.list_data_sources(knowledgeBaseId=kb_id).get("dataSourceSummaries") or []:
        ds = agent.get_data_source(knowledgeBaseId=kb_id, dataSourceId=summary["dataSourceId"])["dataSource"]
        if _location(ds) == (bucket, prefix):
            return ds["dataSourceId"]  # idempotent: one connector per location
    if bucket != console_bucket(account, region):
        iam = client(session, "iam")
        role, tags = adopt(iam, KB_ROLE, boundary, error=KnowledgeError)  # the console's KB role, never a same-name role of another
        ensure_boundary(iam, KB_ROLE, boundary, role, tags)
        iam.put_role_policy(RoleName=KB_ROLE, PolicyName=f"adlc-kb-{kb_id}", PolicyDocument=json.dumps(_s3_read(bucket, prefix)))
    conn: dict[str, Any] = {"type": "S3", "version": "1", "connectionConfiguration": {"bucketName": bucket, "bucketOwnerAccountId": account}}
    if prefix:
        conn["filterConfiguration"] = {"inclusionPrefixes": [prefix]}
    created = agent.create_data_source(knowledgeBaseId=kb_id, name=re.sub(r"[^0-9A-Za-z_-]", "-", f"{bucket}-{prefix}")[:60].strip("-") or "source",
                                       dataSourceConfiguration={"type": "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
                                                                "managedKnowledgeBaseConnectorConfiguration": {"connectorParameters": conn}},
                                       vectorIngestionConfiguration={"parsingConfiguration": {"parsingStrategy": "SMART_PARSING"}})
    return created["dataSource"]["dataSourceId"]


def sync(session: Any, region: str, kb_id: str) -> list[dict[str, Any]]:
    agent = _kb(session, region)
    started = []
    for summary in agent.list_data_sources(knowledgeBaseId=kb_id).get("dataSourceSummaries") or []:
        job = agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=summary["dataSourceId"])["ingestionJob"]
        started.append({"dataSourceId": summary["dataSourceId"], "ingestionJobId": job.get("ingestionJobId"), "status": job.get("status")})
    return started


def create_kb(console: Any, workspace: str, body: Mapping[str, Any]) -> dict[str, Any]:
    name = str(body.get("name") or "")
    if not NAME.match(name):
        raise KnowledgeError("name: letters, digits, '-' or '_' (at most 100)")
    ws = console.workspaces.get(workspace)
    console.workspaces.verify(workspace)
    session = console.workspaces.session(workspace)
    account, region = ws["accountId"], ws["region"]
    source = dict(body.get("source") or {"mode": "upload"})
    _source(account, region, "x", source)  # validate before creating anything
    ensure_bucket(session, account, region)
    boundary = boundary_of(ws)
    role = ensure_kb_role(session, account, region, boundary)
    kwargs: dict[str, Any] = {"name": name, "roleArn": role, "tags": dict(CONSOLE_TAG),
                              "knowledgeBaseConfiguration": {"type": "MANAGED", "managedKnowledgeBaseConfiguration": {"embeddingModelType": "MANAGED"}}}
    if body.get("description"):
        kwargs["description"] = str(body["description"])[:200]
    for attempt in range(6):  # a fresh role is refused for a few seconds
        try:
            kb = _kb(session, region).create_knowledge_base(**kwargs)["knowledgeBase"]
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 5 or "role" not in str(exc).lower():
                raise
            time.sleep(10)
    kb_id = kb["knowledgeBaseId"]

    def tail(job: Any) -> dict[str, Any]:  # KB creation takes minutes: wait it out here, not on the request
        agent = _kb(session, region)
        for _ in range(90):
            status = agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]["status"]
            job.progress(status=status)
            if status in ("ACTIVE", "FAILED"):
                break
            time.sleep(10)
        if status != "ACTIVE":
            raise KnowledgeError(f"knowledge base {kb_id} is {status}")
        ds = create_data_source(session, account, region, kb_id, source, boundary)
        job.log(f"data source {ds}")
        return {"knowledgeBaseId": kb_id, "dataSourceId": ds, "sync": sync(session, region, kb_id)}

    job = console.jobs.start("kb", workspace, {"knowledgeBaseId": kb_id, "name": name}, tail, label=f"知识库 {name}")
    return {"id": kb_id, "name": name, "status": kb.get("status"), "job": job["id"]}


def file_name(raw: str) -> str:
    """A document's name as it was given (Chinese included), without path parts or control characters."""
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", str(raw or "").strip()).strip(". ")[:120]
    if not name:
        raise KnowledgeError("every file needs a name")
    return name


def owned(session: Any, region: str, kb_id: str, acknowledged: Any = False) -> dict[str, Any]:
    """The KB, if the console created it (``adlc:console=1``) or the caller confirmed changing another team's."""
    agent = _kb(session, region)
    kb = agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
    tags = agent.list_tags_for_resource(resourceArn=kb["knowledgeBaseArn"]).get("tags") or {}
    if tags.get("adlc:console") != "1" and acknowledged is not True:
        raise KnowledgeError(f"{kb.get('name')} was not created from this console: send acknowledged: true to change it")
    return kb


def upload(console: Any, workspace: str, kb_id: str, files: Sequence[Mapping[str, Any]], *, start_sync: bool = True,
           acknowledged: Any = False) -> dict[str, Any]:
    """Files into the console bucket's ``kb/<id>/``, which only a source reading that prefix ingests (a KB built on
    another S3 location is fed there, not here)."""
    ws = console.workspaces.get(workspace)
    session = console.workspaces.session(workspace)
    owned(session, ws["region"], kb_id, acknowledged)
    bucket = ensure_bucket(session, ws["accountId"], ws["region"])
    reads = [_location(_kb(session, ws["region"]).get_data_source(knowledgeBaseId=kb_id, dataSourceId=d["dataSourceId"])["dataSource"])
             for d in _kb(session, ws["region"]).list_data_sources(knowledgeBaseId=kb_id).get("dataSourceSummaries") or []]
    if (bucket, f"kb/{kb_id}/") not in reads:
        raise KnowledgeError("no data source of this KB reads the console's upload folder: add the files where its source reads "
                             f"({', '.join(f's3://{b}/{p}' for b, p in reads) or 'it has none'})")
    s3 = client(session, "s3", ws["region"])
    total, written, names = 0, [], set()
    for f in files:
        name = file_name(f.get("name"))
        if name in names:  # two files of one upload with one name: the second keeps a suffix instead of replacing the first
            stem, dot, ext = name.rpartition(".")
            name = f"{stem or ext}-{len(names) + 1}{dot}{ext if stem else ''}"
        names.add(name)
        data = base64.b64decode(str(f.get("contentBase64") or ""), validate=True)
        total += len(data)
        if total > MAX_FILES_BYTES:
            raise KnowledgeError("upload at most 18 MB at a time")
        s3.put_object(Bucket=bucket, Key=f"kb/{kb_id}/{name}", Body=data)
        written.append(name)
    started = sync(session, ws["region"], kb_id) if start_sync else []
    return {"uploaded": written, "sync": started}


def query(session: Any, region: str, kb_id: str, text: str, results: int = 5) -> list[dict[str, Any]]:
    if not text.strip():
        raise KnowledgeError("query is empty")
    kb = _kb(session, region).get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
    mode = "managedSearchConfiguration" if (kb.get("knowledgeBaseConfiguration") or {}).get("type") == "MANAGED" else "vectorSearchConfiguration"
    out = client(session, "bedrock-agent-runtime", region).retrieve(knowledgeBaseId=kb_id, retrievalQuery={"text": text},
                                                                    retrievalConfiguration={mode: {"numberOfResults": max(1, min(int(results), 20))}})
    return [{"text": (r.get("content") or {}).get("text", ""), "score": r.get("score"), "location": r.get("location"), "metadata": r.get("metadata")}
            for r in out.get("retrievalResults") or []]


def delete_kb(console: Any, workspace: str, kb_id: str) -> dict[str, Any]:
    """Delete a console KB: first off every agent that has it and its Retrieve target, then its sources and itself."""
    ws = console.workspaces.get(workspace)
    console.workspaces.verify(workspace)
    session, region = console.workspaces.session(workspace), ws["region"]
    agent = _kb(session, region)
    kb = agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
    if (agent.list_tags_for_resource(resourceArn=kb["knowledgeBaseArn"]).get("tags") or {}).get("adlc:console") != "1":
        raise KnowledgeError("only knowledge bases created from this console can be deleted here")
    detached = [detach(console, workspace, kb_id, {"harnessId": hid, "acknowledged": True})["harness"] for hid in attached(console, workspace, kb_id)]
    ctl = client(session, "bedrock-agentcore-control", region)
    gateway = _find_gateway(ctl)
    if gateway:
        own = _targets(ctl, gateway["gatewayId"]).get(retrieve_target(kb_id, kb["name"]))
        if own:
            ctl.delete_gateway_target(gatewayIdentifier=gateway["gatewayId"], targetId=own["targetId"])
    for summary in agent.list_data_sources(knowledgeBaseId=kb_id).get("dataSourceSummaries") or []:
        agent.delete_data_source(knowledgeBaseId=kb_id, dataSourceId=summary["dataSourceId"])
    agent.delete_knowledge_base(knowledgeBaseId=kb_id)
    try:
        iam = client(session, "iam")
        adopt(iam, KB_ROLE, boundary_of(ws), error=KnowledgeError)  # only the console's own KB role is changed
        iam.delete_role_policy(RoleName=KB_ROLE, PolicyName=f"adlc-kb-{kb_id}")
    except Exception:  # noqa: BLE001 - only BYO-bucket KBs have one
        pass
    return {"deleted": kb_id, "detachedFrom": detached}


# -- attaching to an agent ----------------------------------------------------------------------------------------------

def _slug(text: str, limit: int) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")


def retrieve_target(kb_id: str, kb_name: str) -> str:
    """A KB's Retrieve target on the Gateway, e.g. ``loyalty-rules-kbexample1`` (found again by its ``-<kb id>``)."""
    return f"{_slug(kb_name, 30) or 'kb'}-{kb_id.lower()}"


def agentic_target(harness_name: str) -> str:
    """The agent's deep-search target. UpdateHarness refuses an allowedTools entry over 64 characters (live): with
    ``@adlckb/`` and ``___AgenticRetrieveStream`` around it the name keeps to 32, a long agent name shortened with a hash."""
    slug = _slug(harness_name, 60) or "agent"
    if len(slug) <= 24:
        return "agentic-" + slug
    return f"agentic-{slug[:15].strip('-')}-{hashlib.sha256(harness_name.encode('utf-8')).hexdigest()[:8]}"


def tool_names(harness_name: str, kbs: Sequence[Mapping[str, Any]]) -> list[str]:
    """The tools an agent gets for its KBs, as the model sees them: one Retrieve per KB, one deep search over all."""
    names = [f"{retrieve_target(k['id'], k['name'])}___Retrieve" for k in kbs]
    return names + [f"{agentic_target(harness_name)}___AgenticRetrieveStream"] if kbs else []


def _find_gateway(ctl: Any) -> dict[str, Any] | None:
    return next((g for g in _pages(ctl.list_gateways, "items") if g.get("name") == GATEWAY), None)


def _targets(ctl: Any, gateway_id: str) -> dict[str, dict[str, Any]]:
    return {t["name"]: t for t in _pages(ctl.list_gateway_targets, "items", gatewayIdentifier=gateway_id)}


def _wait_target(ctl: Any, gateway_id: str, target_id: str) -> None:
    for _ in range(60):
        got = ctl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        if got.get("status") == "READY":
            return
        if got.get("status") in ("FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"):
            raise KnowledgeError(f"Gateway target {got.get('name')}: {got.get('status')} {got.get('statusReasons') or ''}")
        time.sleep(5)
    raise KnowledgeError(f"Gateway target {target_id} was not READY after 5 minutes")


def live_kb_ids(ctl: Any, gateway_id: str) -> set[str]:
    """The KBs the Gateway's targets read, whichever console attached them: the role's grant follows the Gateway, not
    one console's store (a console with a fresh data dir would otherwise revoke another's agents' knowledge bases)."""
    found: set[str] = set()
    for target in _targets(ctl, gateway_id).values():
        got = ctl.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target["targetId"])
        for conf in (((got.get("targetConfiguration") or {}).get("mcp") or {}).get("connector") or {}).get("configurations") or []:
            values = conf.get("parameterValues") or {}
            if values.get("knowledgeBaseId"):
                found.add(str(values["knowledgeBaseId"]))
            for retriever in values.get("retrievers") or []:
                kid = ((retriever.get("configuration") or {}).get("knowledgeBase") or {}).get("knowledgeBaseId")
                if kid:
                    found.add(str(kid))
    return found


def gateway_role_policy(account: str, region: str, kb_ids: Sequence[str]) -> dict[str, Any]:
    """Retrieve on the attached KBs only; AgenticRetrieveStream cannot be scoped to a resource."""
    kbs = [f"arn:aws:bedrock:{region}:{account}:knowledge-base/{k}" for k in sorted(set(kb_ids))]
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "Retrieve", "Effect": "Allow", "Action": ["bedrock:Retrieve", "bedrock:GetKnowledgeBase"],
         "Resource": kbs or [f"arn:aws:bedrock:{region}:{account}:knowledge-base/none"]},
        {"Sid": "AgenticRetrieve", "Effect": "Allow", "Action": "bedrock:AgenticRetrieveStream", "Resource": "*"}]}


def ensure_gateway(session: Any, account: str, region: str, kb_ids: Sequence[str], boundary: str | None = None) -> dict[str, str]:
    """The KB Gateway (IAM-authorized, MCP) and its role, allowed to retrieve from ``kb_ids``."""
    ctl = client(session, "bedrock-agentcore-control", region)
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                                                      "Action": "sts:AssumeRole", "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}
    role = _ensure_role(session, GATEWAY_ROLE, trust, "kb-retrieve", gateway_role_policy(account, region, kb_ids), boundary=boundary)
    gateway = _find_gateway(ctl)
    gateway_id = gateway["gatewayId"] if gateway else ctl.create_gateway(
        name=GATEWAY, description="ADLC console knowledge base retrieval", roleArn=role, protocolType="MCP", authorizerType="AWS_IAM",
        tags=dict(CONSOLE_TAG))["gatewayId"]
    for _ in range(60):
        got = ctl.get_gateway(gatewayIdentifier=gateway_id)
        if got.get("status") == "READY":
            return {"gatewayId": gateway_id, "gatewayArn": got["gatewayArn"]}
        time.sleep(5)
    raise KnowledgeError(f"Gateway {GATEWAY} was not READY after 5 minutes")


def sync_targets(ctl: Any, gateway_id: str, harness_name: str, kbs: Sequence[Mapping[str, Any]]) -> None:
    """Every KB in ``kbs`` has its Retrieve target, and the agent's deep-search target covers exactly ``kbs``."""
    targets = _targets(ctl, gateway_id)
    creds = [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
    for kb in kbs:
        name = retrieve_target(kb["id"], kb["name"])
        if name not in targets:
            created = ctl.create_gateway_target(
                gatewayIdentifier=gateway_id, name=name, description=f"Knowledge base {kb['name']}: Retrieve", credentialProviderConfigurations=creds,
                targetConfiguration={"mcp": {"connector": {"source": {"connectorId": CONNECTOR}, "configurations": [{
                    "name": "Retrieve", "description": (kb.get("description") or f"Search the knowledge base {kb['name']}")[:200],
                    "parameterValues": {"knowledgeBaseId": kb["id"]}}]}}})
            _wait_target(ctl, gateway_id, created["targetId"])
    name = agentic_target(harness_name)
    existing = targets.get(name)
    for _ in range(20):  # a target still DELETING cannot be updated
        if not existing or existing.get("status") != "DELETING":
            break
        time.sleep(3)
        existing = _targets(ctl, gateway_id).get(name)
    if not kbs:
        if existing:
            ctl.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=existing["targetId"])
        return
    config = {"mcp": {"connector": {"source": {"connectorId": CONNECTOR}, "configurations": [{"name": "AgenticRetrieveStream", "parameterValues": {
        "retrievers": [{"description": (k.get("description") or k["name"])[:200], "configuration": {"knowledgeBase": {"knowledgeBaseId": k["id"]}}}
                       for k in kbs],
        "agenticRetrieveConfiguration": {"foundationModelType": "MANAGED", "rerankingModelType": "MANAGED"}}}]}}}
    request = {"gatewayIdentifier": gateway_id, "name": name, "description": f"Deep search over the knowledge bases of {harness_name}",
               "targetConfiguration": config, "credentialProviderConfigurations": creds}
    target_id = existing["targetId"] if existing else None
    if target_id:
        ctl.update_gateway_target(targetId=target_id, **request)
    else:
        target_id = ctl.create_gateway_target(**request)["targetId"]
    _wait_target(ctl, gateway_id, target_id)


def attachments(console: Any, workspace: str) -> dict[str, Any]:
    """``{harness id: {"name", "kbs": [{"id", "name", "description"}]}}``."""
    return dict(console.store.read(f"kb_attachments_{workspace}", {}))


def attached(console: Any, workspace: str, kb_id: str) -> list[str]:
    return sorted(hid for hid, a in attachments(console, workspace).items() if any(k["id"] == kb_id for k in a.get("kbs") or []))


def _apply(console: Any, workspace: str, harness: Mapping[str, Any], kbs: list[dict[str, Any]]) -> dict[str, Any]:
    """Make the Harness's KB tools exactly ``kbs``: the Gateway role, the targets, the Harness's tool and allowedTools."""
    ws = console.workspaces.get(workspace)
    session = console.workspaces.session(workspace)
    planned = attachments(console, workspace)
    if kbs:
        planned[harness["id"]] = {"name": harness["name"], "kbs": kbs}
    else:
        planned.pop(harness["id"], None)
    ctl = client(session, "bedrock-agentcore-control", ws["region"])
    found = _find_gateway(ctl)
    wanted = {k["id"] for a in planned.values() for k in a["kbs"]} | (live_kb_ids(ctl, found["gatewayId"]) if found else set())
    gw = ensure_gateway(session, ws["accountId"], ws["region"], sorted(wanted), boundary_of(ws))
    current = ctl.get_harness(harnessId=harness["id"])["harness"]
    role = str(current.get("executionRoleArn") or "").rsplit("/", 1)[-1]
    if role and kbs:  # first, before any target changes: the Harness's role must be allowed to call the Gateway
        put_inline_policy(session, role, "adlc-console-kb-gateway", {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway", "Resource": gw["gatewayArn"]}]}, what="the knowledge-base Gateway",
            boundary=boundary_of(ws))
    before = (attachments(console, workspace).get(harness["id"]) or {}).get("kbs") or []
    tools = [t for t in current.get("tools") or [] if not (t.get("type") == "agentcore_gateway" and t.get("name") == TOOL)]
    allowed = [a for a in current.get("allowedTools") or [] if a != f"@{TOOL}" and not a.startswith(f"@{TOOL}/")]
    names = tool_names(harness["name"], kbs)
    if kbs:
        tools.append({"type": "agentcore_gateway", "name": TOOL, "config": {"agentCoreGateway": {"gatewayArn": gw["gatewayArn"]}}})
        allowed += [f"@{TOOL}/{n}" for n in names]
    sync_targets(ctl, gw["gatewayId"], harness["name"], kbs)
    try:
        ctl.update_harness(harnessId=harness["id"], tools=tools, allowedTools=allowed)  # plain lists (only memory is optionalValue)
    except Exception:  # the Harness refused: put its deep-search target back as it was (live: a refused attach left it)
        try:
            sync_targets(ctl, gw["gatewayId"], harness["name"], before)
        except Exception:  # noqa: BLE001 - the refusal is what the caller needs to see
            pass
        raise
    if role and not kbs:
        try:
            client(session, "iam").delete_role_policy(RoleName=role, PolicyName="adlc-console-kb-gateway")
        except Exception as exc:  # noqa: BLE001 - absent, or a role this workspace may not change (the grant is then only unused)
            if "NoSuchEntity" not in str(exc) and "AccessDenied" not in str(exc):
                raise
    def merge(all_: dict[str, Any]) -> dict[str, Any]:  # only this Harness's entry: another attach may have finished meanwhile
        if kbs:
            all_[harness["id"]] = {"name": harness["name"], "kbs": kbs}
        else:
            all_.pop(harness["id"], None)
        return all_

    console.store.update(f"kb_attachments_{workspace}", {}, merge)
    return {"harness": harness["name"], "harnessId": harness["id"], "knowledgeBases": [k["id"] for k in kbs], "tools": names,
            "gatewayArn": gw["gatewayArn"]}


def _harness(console: Any, workspace: str, body: Mapping[str, Any], verb: str) -> dict[str, Any]:
    ws = console.workspaces.get(workspace)
    console.workspaces.verify(workspace)
    harness = get_agent(console.workspaces.session(workspace), ws["region"], "harness", str(body.get("harnessId") or ""))
    if not harness.get("console") and body.get("acknowledged") is not True:
        raise KnowledgeError(f"{harness['name']} was not created from this console: send acknowledged: true to {verb} it")
    return harness


def attach(console: Any, workspace: str, kb_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    harness = _harness(console, workspace, body, "change")
    kb = detail(console.workspaces.session(workspace), console.workspaces.get(workspace)["region"], kb_id)
    have = (attachments(console, workspace).get(harness["id"]) or {}).get("kbs") or []
    kbs = [k for k in have if k["id"] != kb_id] + [{"id": kb_id, "name": kb["name"], "description": kb.get("description") or ""}]
    return _apply(console, workspace, harness, kbs)


def detach(console: Any, workspace: str, kb_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    hid = str(body.get("harnessId") or "")
    try:
        harness = _harness(console, workspace, body, "change")
    except Exception as exc:  # noqa: BLE001 - a Harness deleted since: only forget it
        if "ResourceNotFound" not in str(exc):
            raise
        console.store.update(f"kb_attachments_{workspace}", {}, lambda all_: {k: v for k, v in all_.items() if k != hid})
        return {"harness": hid, "harnessId": hid, "knowledgeBases": [], "tools": [], "forgotten": True}
    have = (attachments(console, workspace).get(harness["id"]) or {}).get("kbs") or []
    return _apply(console, workspace, harness, [k for k in have if k["id"] != kb_id])


def register(router: Any) -> None:
    def sess(r: Any) -> tuple[Any, str]:
        session = r.session()
        return session, r.console.workspaces.get(r.workspace())["region"]

    add = router.add
    add("GET", "/workspaces/{wid}/knowledge-bases", lambda r: (200, {"knowledgeBases": list_kbs(*sess(r))}))
    add("POST", "/workspaces/{wid}/knowledge-bases", lambda r: (202, create_kb(r.console, r.workspace(), r.body)))
    add("GET", "/workspaces/{wid}/knowledge-bases/{kid}", lambda r: (200, detail(*sess(r), r.params["kid"])))
    add("DELETE", "/workspaces/{wid}/knowledge-bases/{kid}", lambda r: (200, delete_kb(r.console, r.workspace(), r.params["kid"])))
    add("POST", "/workspaces/{wid}/knowledge-bases/{kid}/files", lambda r: (201, upload(r.console, r.workspace(), r.params["kid"], r.body.get("files") or [],
                                                                                       acknowledged=r.body.get("acknowledged"))))

    def start_sync(r: Any):
        session, region = sess(r)
        owned(session, region, r.params["kid"], r.body.get("acknowledged"))
        return 202, {"started": sync(session, region, r.params["kid"])}

    add("POST", "/workspaces/{wid}/knowledge-bases/{kid}/sync", start_sync)
    add("POST", "/workspaces/{wid}/knowledge-bases/{kid}/query", lambda r: (200, {"results": query(*sess(r), r.params["kid"], str(r.body.get("query") or ""),
                                                                                                    int(r.body.get("results") or 5))}))
    add("POST", "/workspaces/{wid}/knowledge-bases/{kid}/attach", lambda r: (200, attach(r.console, r.workspace(), r.params["kid"], r.body)))
    add("POST", "/workspaces/{wid}/knowledge-bases/{kid}/detach", lambda r: (200, detach(r.console, r.workspace(), r.params["kid"], r.body)))
    add("GET", "/workspaces/{wid}/knowledge-bases/{kid}/agents", lambda r: (200, {"agents": [
        {"harnessId": hid, "name": attachments(r.console, r.workspace())[hid]["name"]} for hid in attached(r.console, r.workspace(), r.params["kid"])]}))
