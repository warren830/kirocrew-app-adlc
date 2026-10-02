"""The console's canary for code agents: a candidate version of a console-deployed AgentCore Runtime measured beside
production through an AgentCore Gateway, the canary ramp, and a promotion that evidence opens, with a rollback.

**Production does not move until the promotion.** ``UpdateAgentRuntime`` mints an immutable version and moves DEFAULT
to it at once, and DEFAULT cannot be pointed anywhere else (``UpdateAgentRuntimeEndpoint`` on DEFAULT: "Default
endpoints are managed through agent updates", live), so a candidate minted as a version of the agent would serve every
caller of DEFAULT before any evidence (Launchpad's canary does that and diverts only its own invoke chain). Here the
candidate runs as a **runtime of its own** beside production (``<name>_c<hex>``), deployed by the deploy module's
stages (validate, upload, build, runtime, ready, smoke) with what makes it production's twin: production's execution
role as it is (nothing is written to it), its network, lifecycle, request headers and file systems, production's
variables under the candidate's own, and its artifact kept where production's are (``deployments/<production>/<job>/``
in the console bucket, production's image repository), so the promotion publishes that very artifact. The **control**
is a named endpoint of production pinned to the version compared; the **treatment** a named endpoint of the candidate
runtime (its DEFAULT keeps the smoke call and the verifications out of the A/B test). DEFAULT and production's own
endpoints stay as they were, for every caller, until the promotion.

**The split** is the console's A/B machinery (``console.experiments``): a Gateway (AWS_IAM) with two ``http.agentcore
Runtime`` targets (runtime ARN and qualifier, signed by the Gateway's role, which needs ``InvokeAgentRuntime`` on both
runtimes and their endpoints), one online evaluation per arm on the arm's endpoint log group
``/aws/bedrock-agentcore/runtimes/<runtimeId>-<endpoint>`` with ``service.name`` ``<runtime name>.<endpoint>`` (made
first: CreateOnlineEvaluationConfig refuses a log group that does not exist yet), and a target-based A/B test on the
control target's path. A client POSTs the runtime's own payload to ``<gateway>/<control target>/invocations`` (SigV4
as ``bedrock-agentcore``) with ``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id``; the Gateway keeps a session on its arm
and answers with the runtime's own body. The console's chat and ``/v1`` send a runtime in a running canary through it
(:func:`route_for`, :func:`invoke_through`; production answers directly if the Gateway fails); :func:`send_traffic`
replays a contract set. A turn falls back to production's DEFAULT only when its request never left (a proxy or TLS
failure before AWS, ``UNSENT``, and before the request's last byte went to the socket, ``experiments.SentBody``:
botocore raises the same SSLError for a TLS record broken mid-response): a timeout or an error answer may come after
the candidate ran, and the turn is not run twice. The ramp is the experiments' 5 → 25 → 50 % (pause, new weights,
resume; an admin's, above 50 % only once the gate holds: ``experiments.split_allowed``); 100 % is the promotion.

**The gate** is ``experiments.gate``: (a) a robust console verification of the candidate (:func:`verify_candidate`:
``direct.verify``'s rounds, L1 and evaluator panel on the candidate runtime's DEFAULT, a console job of kind
``verify``, which records the candidate's version and counts only while that is the version set up), (b) the A/B
result on the canary's own evaluator (declared when it started; deciding on another is an override) not worse than
the control beyond the evaluator's noise band with enough samples in each arm, (c) nothing compared moved:
production still on the version the canary compared, its control endpoint still serving it, the candidate runtime
and its treatment endpoint still on the version set up. The deploy page refuses to move or delete those endpoints or
republish or delete the candidate while the canary holds them (:func:`holder`), and to publish production or move any
of its endpoints while a promotion or rollback runs (:func:`mover`). Every action that moves production or changes a
canary's state — the deploy page's, and a canary's start, ramp, pause, promotion, rollback and cleanup — runs its
checks under the production runtime's own lock (``deploy.moves``) and reads the record again under it: a promotion or
rollback starts only while no deploy job and no other canary's job moves production. An admin may override a failed
gate with a reason; the override, the failed conditions and the evidence are recorded (``promotions``, with the A/B experiments'
releases), and so is a promotion that moved production but did not finish (``incomplete``).

**Promotion** stops the A/B test (its results kept) and publishes the candidate's artifact and variables as
production's next version (the deploy module's update stages without upload or build): DEFAULT serves it, and the
named endpoint the console deploys behind (``live``) moves to it. **Rollback** before a promotion stops the split and
leaves production as it was (the record keeps production's version and endpoints as read then: nothing moved); after
one it moves the named endpoint back to the version before (the deploy module's ``point_endpoint``) and, since DEFAULT
moves only with a new version, publishes that version's own artifact and variables again. **Cleanup** (a job) deletes
the A/B test, the online evaluations, the targets, the Gateway, the control endpoint, the candidate runtime (the deploy
module's teardown: its endpoint, the runtime; production's role, repository and sources are never touched), the
candidate's artifacts — the sources its job uploaded, and an image only when the canary's own build pushed it (a
Dockerfile candidate, tagged with the setup job) — never anything a version of production runs or ran (a rollback
can return to any), the role and the log groups the canary made. Everything is tagged ``adlc:console=1`` and
``adlc:canary=<id>``; cleanup deletes only resources so tagged. A cleanup that leaves something (an AWS refusal) is
not done: what is left is on the record (``cleanup_incomplete``, or ``promoted`` / ``rolled_back`` as it was) and it
runs again. An image candidate is compared by its resolved digest: production's own image with other variables is a
candidate, the same image and variables are nothing to compare.

**Restarts.** A record a job held when the console stopped (``creating``, ``promoting``, ``rolling_back``,
``cleaning``; the job is ``interrupted``) is released when next read (:func:`_settle`): a setup ``failed`` (cleanup
removes what it made, a new canary may start); a promotion ``promoted`` (incomplete, recorded) if a version after the
one compared runs the candidate's artifact and variables, else ``stopped`` (a version a deploy made meanwhile is not
the candidate); a rollback ``rolled_back`` (incomplete, recorded) if a version after the promoted one runs the
version before's again, else ``promoted`` (roll back again); a cleanup incomplete, or ``promoted`` / ``rolled_back``
as before it. A promotion or rollback whose later stage fails is settled the same way. A record saved before one of
its fields existed is read as if it had it (:func:`_filled`).

Live 2026-10-01 (us-west-2, botocore 1.43.90): a Strands agent (Nova 2 Lite; its answer style from an environment
variable) deployed by the deploy module as a zip (requirements bundled by CodeBuild, ``opentelemetry-instrument``) behind
the endpoint ``live``, version 1 terse; two canaries on it.

* **Setup** took 3.0 min: the control endpoint 6 s; the candidate's stages validate 2 s, upload 2 s, build 96 s
  (CodeBuild queued 40 s, built 37 s: a 36.9 MB bundle under production's prefix), runtime 1.5 s (production's role, so
  no new role and no IAM pause), ready 17 s, smoke 8 s; its treatment endpoint 6 s; role, Gateway, two targets, two
  online evaluations and the A/B test about 40 s. Production stayed on DEFAULT → 1, ``live`` → 1 throughout.
* **The split.** 20 replayed sessions at 50/50 went 12 production / 8 candidate (the candidate's answers carry ``[v2]``);
  6 console chat sessions 3 / 3, a second turn staying on its session's arm; ``/v1`` named the canary. ``aws/spans``
  counted 18 / 20 sessions per arm's ``service.name`` (:func:`observed_split`). A first replay lost 9 of 20 sessions to
  this machine's local HTTPS proxy (``ProxyConnectionError``, ``SSLError``) before they reached AWS: such failures are
  retried (``UNSENT``), anything that may have reached the agent is not.
* **The gate.** Before any evidence it refused on (a) and (b). The candidate's verification (10 contracts × 2 rounds on
  its DEFAULT, every trace complete for the evaluator, 46 panel calls) held 10/10 in 237 s, both bands 0.02. The
  online evaluations had scored 21 / 22 traces by 17:10; GetABTest published its first analysis at 17:11:51, 12–17 min
  after the sessions, counting fewer of them: Helpfulness production 0.830 (n=8), candidate 0.966 (n=10), Δ +0.136,
  p = 8.7e-05; Correctness 1.000 (n=8) against 0.950 (n=10), Δ −0.050, p = 0.70. The gate passed on Helpfulness and
  refused the same evidence on Correctness: a band that measures only the judge's noise (0.02) is strict on a handful
  of sessions.
* **Ramp** 50 → 25 → 50 and 5 → 25 → 50: 13–15 s a step (pause, weights, resume).
* **Promotion** took 37 s: the A/B test stopped, UpdateAgentRuntime with the candidate's own bundle (0.6 s), READY in
  18 s, ``live`` moved in 5 s, the smoke call answered ``[v2]``; the chat then reached production directly. **Undoing
  it** took 31 s: ``live`` back to version 1 in 8 s, and version 1's artifact and variables published again as version
  3 (DEFAULT → 3, terse again). A rollback before promotion on the second canary left production on version 3,
  ``live`` → 1, with no version minted.
* **Cleanup** took 41 s and 46 s: DeleteGateway took after 13 s of retries; the candidate runtime's endpoint went in 6.5
  s and the runtime in 6.2 s; a promoted candidate's bundle stays with production, an unpromoted one is deleted.
* An ``http.agentcoreRuntime`` target ``{arn, qualifier}`` with ``credentialProviderConfigurations:
  [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]`` was READY at once; ``POST <gateway>/<target>/invocations``
  answered the runtime's own JSON (``application/json``) and echoed the session header (9.0 s cold, 1.6 s warm); a
  ``/invoke`` path is a 404 (``UnknownOperationException``). DeleteGateway refuses while a target is still going.
* A code runtime with ADOT writes its GenAI events (system, user, choice) to the endpoint's own log group (stream
  ``otel-rt-logs``) under ``service.name`` ``<runtime name>.<endpoint>``, and its spans (``invoke_agent Strands
  Agents``, ``chat``, ``POST /invocations``) with ``session.id`` to ``aws/spans`` a few minutes later, which is what the
  online evaluations read. The deploy smoke call's spans never arrived: it stops its session at once, before the span
  batch is exported; traffic and verifications here leave their sessions to idle out.
* Only traffic sent through the Gateway is split: a caller that invokes production itself stays on production (the
  safe side). A deploy of production during a canary moves DEFAULT; the gate's condition (c) then refuses.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from ..direct import verify
from ..direct.aws import client
from ..direct.run import Asked, new_session_id, out_text
from . import deploy, experiments as ex
from .agents import CONSOLE_TAG

#: Every canary resource is named ``<PREFIX>-<id>`` (``_`` where a name takes no ``-``); its role is on the console's IAM
#: path (``agents.ROLE_PATH``), where a spoke role lets the console make roles. The live probe runs with
#: ``adlc-probe-can`` (:func:`configure`).
PREFIX = "adlc-console-can"
#: The deploy module's names for the candidate's build (its CodeBuild project and role; the probe uses adlc-probe).
NAMES = deploy.NAMES
CANARY_TAG = "adlc:canary"
COLLECTION = "runtime_canaries"
CID = re.compile(r"^can-[0-9a-f]{8}$")
#: A canary in one of these holds its production runtime: no second one starts.
BUSY = ("creating", "running", "paused", "stopped", "promoting", "rolling_back")
#: A status a job holds a canary in, and the record's field naming that job (:func:`_settle`).
HELD = {"creating": "job", "promoting": "promoteJob", "rolling_back": "rollbackJob", "cleaning": "cleanupJob"}
#: A canary in one of these, while its job runs, is publishing production and moving its named endpoint.
MOVING = ("promoting", "rolling_back")
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
USER_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-User-Id"
MAX_ANSWER = 256 * 1024

_sleep: Callable[[float], None] = time.sleep  # the tests make every wait instant
ExperimentError = ex.ExperimentError


def configure(*, prefix: str | None = None, names: deploy.Names | None = None) -> None:
    """Name everything under another prefix (the live probe: ``adlc-probe-can`` and ``deploy.Names("adlc-probe")``)."""
    global PREFIX, NAMES
    if prefix:
        PREFIX = prefix
    if names:
        NAMES = names


def _now() -> str:
    return ex._now()


def _token() -> str:
    return ex._token()


# -- names and requests --------------------------------------------------------------------------------------------------

def names(hexid: str, production: str) -> dict[str, str]:
    under = PREFIX.replace("-", "_")
    return {"copy": f"{production[:38]}_c{hexid}", "control": f"{under}_{hexid}_c", "treatment": f"{under}_{hexid}_t", "role": f"{PREFIX}-{hexid}",
            "gateway": f"{PREFIX}-{hexid}", "C": f"{PREFIX}-{hexid}-c", "T1": f"{PREFIX}-{hexid}-t", "evalC": f"{under}_{hexid}_c",
            "evalT1": f"{under}_{hexid}_t", "abTest": f"{under}_{hexid}"}


def target_request(name: str, runtime_arn: str, qualifier: str) -> dict[str, Any]:
    """A Gateway target that invokes one endpoint of a runtime (InvokeAgentRuntime, signed by the Gateway's role)."""
    return {"name": name, "description": f"InvokeAgentRuntime {runtime_arn.rsplit('/', 1)[-1]} ({qualifier})"[:200],
            "targetConfiguration": {"http": {"agentcoreRuntime": {"arn": runtime_arn, "qualifier": qualifier}}},
            "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}], "clientToken": _token()}


def role_documents(account: str, region: str, runtime_arns: Sequence[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The experiments' role (Gateway, A/B routing, online evaluation) with the two runtimes to invoke: InvokeAgentRuntime,
    and its ForUser form for a caller that names its user (the console's chat does)."""
    trust, policy = ex.role_documents(account, region, runtime_arns)
    for statement in policy["Statement"]:
        if statement.get("Sid") == "InvokeArms":
            statement["Action"] = ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeAgentRuntimeForUser"]
    return trust, policy


def fingerprint(production_id: str, copy_arn: str, version: str, artifact: Mapping[str, Any], environment: Mapping[str, Any]) -> str:
    """Which candidate a verification or a canary is about: this artifact and these variables beside this production."""
    doc = json.dumps({"agent": production_id, "candidate": copy_arn, "version": str(version), "artifact": artifact,
                      "environment": dict(environment)}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(doc.encode("utf-8")).hexdigest()[:16]


def artifact_kind(artifact: Mapping[str, Any]) -> str:
    return "container" if (artifact or {}).get("containerConfiguration") else "code"


def _image_repository(uri: str) -> str | None:
    found = deploy.IMAGE_URI.match(uri or "")
    return found["repo"] if found else None


def publish_request(name: str, artifact: Mapping[str, Any], environment: Mapping[str, Any], *, endpoint: str | None,
                    smoke: bool = True) -> dict[str, Any]:
    """The deploy module's update request for an artifact that exists already (an image by digest, or code in S3)."""
    common = {"mode": "update", "name": name, "protocol": None, "environment": dict(environment), "endpoint": endpoint, "smoke": smoke,
              "prompt": deploy.SMOKE_PROMPT}
    container = ((artifact or {}).get("containerConfiguration") or {}).get("containerUri")
    if container:
        return {**common, "source": "image", "image": str(container)}
    code = (artifact or {}).get("codeConfiguration") or {}
    entry = [str(e) for e in code.get("entryPoint") or ["main.py"]]
    return {**common, "source": "zip", "entry": entry[-1], "runtime": str(code.get("runtime") or deploy.DEFAULT_PYTHON),
            "instrument": entry[0] == "opentelemetry-instrument", "install": False}


# -- the deploy module's stages for the candidate and for a publication ---------------------------------------------------

class _CreateLikeProduction:
    """The control client of a candidate deployment: its CreateAgentRuntime gets production's network, lifecycle, request
    headers and file systems, and the canary's tags; every other call goes through as it is."""

    COPIED = ("networkConfiguration", "lifecycleConfiguration", "requestHeaderConfiguration", "filesystemConfigurations")

    def __init__(self, ctl: Any, production: Mapping[str, Any], tags: Mapping[str, str]):
        self._ctl, self._production, self._tags = ctl, production, dict(tags)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctl, name)

    def create_agent_runtime(self, **request: Any) -> Any:
        for key in self.COPIED:
            if self._production.get(key):
                request[key] = self._production[key]
        request["tags"] = {**(request.get("tags") or {}), **self._tags}
        request["description"] = f"ADLC console canary {self._tags.get(CANARY_TAG)}: the candidate beside {self._production.get('agentRuntimeName')}"[:1200]
        return self._ctl.create_agent_runtime(**request)


class _ProductionRole:
    """A deployment that runs with production's execution role as it is: nothing is created or written for it (the
    deploy module would ensure ``<prefix>-rt-<name>`` and re-put its policy). In a workspace with a permissions
    boundary it is passed only when it is the console's own (on its path), and given the boundary first if it lacks it
    (``agents.passable``)."""

    production: dict[str, Any]

    def _ensure_role(self, name: str, trust: Mapping[str, Any], policy_name: str, policy: Mapping[str, Any], description: str,
                     runtime: str | None = None) -> str:
        if runtime is not None:
            from .agents import passable

            return passable(self.iam, str(self.production["roleArn"]), account=self.account, boundary=self.boundary,  # type: ignore[attr-defined]
                            error=deploy.DeployError, what=f"{self.production.get('agentRuntimeName')}'s role")
        return super()._ensure_role(name, trust, policy_name, policy, description, runtime)  # type: ignore[misc]


class CandidatePipeline(_ProductionRole, deploy.Pipeline):
    """The candidate as a runtime of its own, by the deploy module's stages, as production's twin: production's
    execution role as it is (nothing is written to it), its settings (:class:`_CreateLikeProduction`), and the artifact
    where production's are kept (``deployments/<production>/<job>/``, production's image repository)."""

    def __init__(self, session: Any, *, production: Mapping[str, Any], tags: Mapping[str, str], **kw: Any):
        super().__init__(session, **kw)
        self.production = dict(production)
        self.prefix = f"deployments/{self.production['agentRuntimeName']}/{self.job_id}"
        self.ctl = _CreateLikeProduction(self.ctl, self.production, tags)

    def _ensure_repository(self) -> tuple[str, str]:
        name = str(self.production["agentRuntimeName"])
        repo = self.names.repository(name)
        try:
            found = self.ecr.describe_repositories(repositoryNames=[repo])["repositories"][0]
        except Exception as exc:  # noqa: BLE001
            if not deploy._missing(exc):
                raise
            raise deploy.DeployError(f"{name} has no console image repository {repo}: a Dockerfile candidate is built where production's images are") from exc
        tags = {t["Key"]: t["Value"] for t in self.ecr.list_tags_for_resource(resourceArn=found["repositoryArn"]).get("tags") or []}
        if tags.get("adlc:console") != "1" or tags.get("adlc:runtime") != name:
            raise deploy.DeployError(f"the ECR repository {repo} is not {name}'s (tags {tags})")
        return str(found["repositoryArn"]), str(found["repositoryUri"])


class PublishPipeline(_ProductionRole, deploy.Pipeline):
    """A new version of production made of an artifact that exists already (the candidate the canary measured, or the
    version a rollback returns to): the deploy module's update stages with production's role as it is, upload and
    build skipped."""

    def __init__(self, session: Any, *, artifact: Mapping[str, Any], **kw: Any):
        super().__init__(session, **kw)
        self.production = dict(self.current or {})
        self.artifact = dict(artifact)
        s3 = (((self.artifact.get("codeConfiguration") or {}).get("code") or {}).get("s3") or {})
        if s3:
            self.bucket, self.code_key = str(s3["bucket"]), str(s3["prefix"])

    def _validate(self) -> str:
        status = str(self.ctl.get_agent_runtime(agentRuntimeId=self.runtime_id).get("status"))
        if status in ("CREATING", "UPDATING", "DELETING"):
            raise deploy.DeployError(f"{self.name} is {status}: wait until it settles")
        if self.req["source"] == "image":
            return self._resolve_image()
        return (f"code s3://{self.bucket}/{self.code_key} · {self.req['entry']} on {self.req['runtime']}"
                + (" with opentelemetry-instrument" if self.req.get("instrument") else ""))

    def _upload(self) -> str:
        return deploy.Skip("the artifact exists: nothing to upload")

    def _build(self) -> str:
        return deploy.Skip("the artifact exists: nothing to build")


# -- verification of the candidate ----------------------------------------------------------------------------------------

def ask_runtime(data: Any, runtime_arn: str, rows: Sequence[Asked], *, workers: int = 4, log: Callable[[str], None] = print) -> list[Asked]:
    """Ask every row on a runtime's DEFAULT with the console's payload (``{"prompt", "actorId"}``), ``workers`` at a
    time; the sessions are left to idle out (stopping one at once loses its spans)."""
    def one(asked: Asked) -> Asked:
        started = time.monotonic()
        asked.started_ms = int(time.time() * 1000)
        try:
            response = data.invoke_agent_runtime(agentRuntimeArn=runtime_arn, runtimeSessionId=asked.session_id,
                                                 payload=json.dumps({"prompt": asked.query, "actorId": asked.actor}, ensure_ascii=False).encode("utf-8"))
            body = response.get("response")
            raw = body.read(MAX_ANSWER) if hasattr(body, "read") else (body or b"")
            asked.text = deploy.answer_text(raw if isinstance(raw, bytes) else str(raw).encode(), str(response.get("contentType") or ""))
            if int(response.get("statusCode") or 200) >= 400:
                asked.error = f"HTTP {response.get('statusCode')}: {asked.text[:200]}"
        except Exception as exc:  # noqa: BLE001 - L1 reads it as an invoke error
            asked.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        asked.seconds = round(time.monotonic() - started, 1)
        if asked.error:
            log(f"    {asked.case_id}: {asked.error[:160]}")
        return asked

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, rows))


class RuntimeVerify(verify.Verify):
    """``direct.verify`` on a code runtime: the same rounds, L1 on the answers and spans, and the evaluator panel (noise
    bands), each contract asked with InvokeAgentRuntime on the runtime's DEFAULT (``harness``: the runtime's name, id,
    ARN and runtime id)."""

    def run(self) -> dict[str, Any]:
        from .. import l1
        from ..direct.panel import STABILITY_RUNS, Panel
        from ..direct.traces import TraceStore

        started = time.monotonic()
        scorer = (Panel(client(self.session, "bedrock-agentcore", self.region), client(self.session, "bedrock-agentcore-control", self.region),
                        self.panel, workers=self.workers, log=self.log) if self.panel else None)
        data = client(self.session, "bedrock-agentcore", self.region)
        logs = client(self.session, "logs", self.region)
        group = f"/aws/bedrock-agentcore/runtimes/{self.harness['runtimeId']}-DEFAULT"
        rounds: list[dict[str, Any]] = []
        panel_rows: list[dict[str, Any]] = []
        verdicts: dict[str, str] = {}
        jobs: list[dict[str, Any]] = []
        for number in range(1, self.repeat + 1):
            epoch = int(time.time())
            since = epoch * 1000 - 60_000
            rows = [Asked(index=i, case_id=c["id"], query=c["query"], session_id=new_session_id(epoch), probe=False, started_ms=0,
                          actor=f"{c['actorId']}-verify-r{number}-{epoch}-q{i}") for i, c in enumerate(self.cases, 1)]
            self.log(f"round {number} of {self.repeat}: {len(rows)} contract(s) on {self.harness['name']}")
            asked = ask_runtime(data, self.harness["arn"], rows, workers=self.workers, log=self.log)
            run_dir = self.out / "runs" / f"round{number}-{epoch}"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "sessions.tsv").write_text("".join(f"{r.index}\t{r.case_id}\t{r.session_id}\t{r.actor}\t{r.started_ms}\t0\n" for r in asked),
                                                  encoding="utf-8")
            for r in asked:
                (run_dir / f"q{r.index}.out").write_text(out_text(r), encoding="utf-8")
            store = TraceStore(logs, group, since)
            store.wait_complete({r.session_id: r.text for r in asked if not r.error}, timeout=self.settle_timeout, progress=self.log)
            doc = l1.evaluate_run(pack=self.pack, cases=self.cases, run_dir=run_dir, phase=f"round{number}", since_ms=since,
                                  source=l1.LogsSpanSource(logs, since))
            (run_dir / "l1.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            results = {c["caseId"]: c for c in doc["cases"]}
            rounds.append({"round": number, "cases": {
                cid: {"verdict": c["verdict"], "failed": [k["code"] for k in c["checks"] if k["status"] == "fail"],
                      "tools": [t["tool"] for t in c.get("toolsCalled") or []], "sessionId": c["sessionId"],
                      "answer": c.get("responseExcerpt", "")[:300]} for cid, c in results.items()},
                "invokeErrors": [{"caseId": r.case_id, "error": r.error} for r in asked if r.error]})
            if scorer:
                verdicts.update({c["sessionId"]: c["verdict"] for c in doc["cases"]})
                jobs += [{"evaluator": e, "caseId": r.case_id, "phase": f"round{number}", "sessionId": r.session_id,
                          "records": store.evaluation_records(r.session_id), "references": []} for r in asked for e in self.panel]
        if scorer and jobs:
            jobs += [dict(jobs[i], phase="stability") for i in range(len(self.panel)) for _ in range(STABILITY_RUNS)]
            self.log(f"panel: {len(self.panel)} AgentCore evaluator(s), {len(jobs)} calls")
            panel_rows = scorer.score(jobs)
        return self.document(rounds, panel_rows, verdicts, seconds=time.monotonic() - started)


# -- the record ----------------------------------------------------------------------------------------------------------

def _outcome(rec: Mapping[str, Any]) -> str:
    """What the canary came to before its cleanup, as the record shows it: ``rolled_back``, ``promoted`` or nothing."""
    return "rolled_back" if rec.get("rollback") else "promoted" if rec.get("promotion") else ""


def _filled(rec: dict[str, Any]) -> dict[str, Any]:
    """A record saved before some of its fields existed, read as if it had them (nothing is written): whether the
    canary's own build made the candidate's image (``candidate.built``: a Dockerfile candidate) and what a cleanup the
    upgrade's restart cut off returns to (``cleanupFrom``, as the record shows)."""
    candidate = rec.get("candidate")
    if isinstance(candidate, dict) and "built" not in candidate:
        rec = {**rec, "candidate": {**candidate, "built": candidate.get("source") == "dockerfile"}}
    if rec.get("status") in ("cleaning", "cleanup_incomplete") and not rec.get("cleanupFrom"):
        rec = {**rec, "cleanupFrom": _outcome(rec) or None}
    return rec


def _settle(x: ex.Ctx, rec: dict[str, Any]) -> dict[str, Any]:
    """A canary its job no longer holds (the console restarted while the job ran: it is ``interrupted``; or the job
    failed when what it had done could not be read) released, so it can be cleaned up and a new one started: a setup
    ``failed`` (what it made is on the record), a promotion and a rollback as far as they came (:func:`_promotion_cut`,
    :func:`_rollback_cut`), a cleanup incomplete."""
    rec = _filled(rec)
    field = HELD.get(str(rec.get("status")))
    if not field or ex._holds(x.console, rec, field):
        return rec
    jid = str(rec.get(field) or "(never started)")
    try:
        ran = x.console.jobs.get(jid) if rec.get(field) else {}
    except (KeyError, OSError, ValueError):
        ran = {}
    if ran.get("status") == "failed":  # it ended, but what it did could not be read then (_promotion_cut, _rollback_cut)
        cut = f"the job {jid} failed ({str(ran.get('error') or '')[:300]})"
    else:
        cut = f"the job {jid} did not finish (the console restarted while it ran)"
    status = rec["status"]
    held = {"status": status, field: rec.get(field)}  # written only while the record is still so (_save's expect)
    since = ran.get("finishedAt") or rec.get("updatedAt")
    if status == "creating":
        return _save(x, rec["id"], expect=held, status="failed", error=f"setup: {cut}: clean up what it made")
    if status == "promoting":
        decision = rec.get("decision") or {}
        # production's version when the promotion was decided (the gate's condition (c) read it), else the one compared
        before = next((str(c.get("now")) for c in (decision.get("gate") or {}).get("conditions") or [] if c.get("id") == "agent" and c.get("now")),
                      str(rec["agent"]["version"]))
        return _promotion_cut(x, rec, decision, before, f"promotion: {cut}", jid, expect=held, since=since)
    if status == "rolling_back":
        return _rollback_cut(x, rec, f"rollback: {cut}", jid, expect=held, since=since)
    before = str(rec.get("cleanupFrom") or "")
    return _save(x, rec["id"], expect=held, status=before if before in ("promoted", "rolled_back") else "cleanup_incomplete", cleanedAt=None,
                 cleanupFrom=rec.get("cleanupFrom"), error=f"cleanup: {cut}: clean up again")


def _canaries(x: ex.Ctx) -> list[dict[str, Any]]:
    return [_settle(x, c) for c in x.console.store.read(COLLECTION, {}).values() if c.get("workspace") == x.workspace]


def moving(console: Any, rec: Mapping[str, Any]) -> bool:
    """Whether the canary's promotion or rollback job is publishing production and moving its endpoint now (a record a
    restart left ``promoting`` or ``rolling_back`` is not: its job is gone)."""
    status = str(rec.get("status"))
    return status in MOVING and ex._holds(console, rec, HELD[status])


def mover(console: Any, workspace: str, runtime_id: str, *, but: str | None = None) -> dict[str, Any] | None:
    """The canary whose promotion or rollback job is moving this production runtime now, if any (:func:`moving`;
    ``but``: the canary whose own job asks). While there is one, nothing else publishes production or moves its
    endpoints."""
    for rec in (console.store.read(COLLECTION, {}) or {}).values():
        if (rec.get("workspace") == workspace and (rec.get("agent") or {}).get("id") == runtime_id and not (but and rec.get("id") == but)
                and moving(console, rec)):
            return rec
    return None


def holder(console: Any, workspace: str, runtime_id: str, endpoint: str | None = None, *, but: str | None = None) -> dict[str, Any] | None:
    """The canary that holds a runtime (``endpoint`` None) or one endpoint of it, if any: production while its canary is
    active (:data:`BUSY`), the candidate runtime, production's control endpoint and the candidate's treatment endpoint
    until the canary is cleaned up. The deploy page does not delete, move or republish what a canary holds: the gate's
    evidence is about exactly those versions. ``but``: the canary whose own job asks (its rollback moves production's
    endpoint)."""
    for rec in (console.store.read(COLLECTION, {}) or {}).values():
        if rec.get("workspace") != workspace or rec.get("cleanedAt") or (but and rec.get("id") == but):
            continue
        agent, copy = rec.get("agent") or {}, rec.get("copy") or {}
        if endpoint is None:
            if copy.get("id") == runtime_id or (agent.get("id") == runtime_id and rec.get("status") in BUSY):
                return rec
        elif (agent.get("id") == runtime_id and rec.get("controlEndpoint") == endpoint) or (
                copy.get("id") == runtime_id and rec.get("treatmentEndpoint") == endpoint):
            return rec
    return None


def list_canaries(x: ex.Ctx) -> list[dict[str, Any]]:
    keep = ("id", "name", "status", "agent", "weights", "metric", "createdAt", "createdBy", "promotion", "rollback", "error", "cleanedAt")
    return [{k: c.get(k) for k in keep} for c in sorted(_canaries(x), key=lambda c: str(c.get("createdAt") or ""), reverse=True)]


def _get(x: ex.Ctx, cid: str) -> dict[str, Any]:
    found = x.console.store.read(COLLECTION, {}).get(cid) if CID.match(cid or "") else None
    if not found or found.get("workspace") != x.workspace:
        raise ExperimentError(f"no runtime canary {cid}", status=404)
    return _settle(x, found)


def _save(x: ex.Ctx, cid: str, *, expect: Mapping[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    """The record with ``fields`` saved onto it. With ``expect``, saved only while the stored record still has those
    values: a settle decides on what it read, outside production's lock, and another action may have moved the record
    since (then nothing is written, and the record as it is now is returned)."""
    out: dict[str, Any] = {}

    def change(all_: dict[str, Any]) -> dict[str, Any]:
        current = all_[cid]
        if expect and any(current.get(k) != v for k, v in expect.items()):
            out.update(current)
            return all_
        out.update({**current, **fields, "updatedAt": _now()})
        return {**all_, cid: dict(out)}

    x.console.store.update(COLLECTION, {}, change)
    return out


def _admin(x: ex.Ctx, what: str) -> None:
    if x.caller.get("role") != "admin":
        raise ExperimentError(f"{what} needs an admin", status=403)


def _record(x: ex.Ctx, runtime_id: str) -> dict[str, Any]:
    return deploy._records(x.console.store, x.workspace).get(runtime_id) or {}


def production_state(ctl: Any, runtime_id: str) -> dict[str, Any]:
    """Production as AgentCore has it now: DEFAULT's version and every named endpoint's."""
    got = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
    endpoints = ex._pages(lambda **kw: ctl.list_agent_runtime_endpoints(agentRuntimeId=runtime_id, **kw), "runtimeEndpoints")
    return {"version": str(got.get("agentRuntimeVersion")), "status": got.get("status"),
            "endpoints": {str(e.get("name")): str(e.get("liveVersion") or e.get("targetVersion") or "") for e in endpoints}}


# -- starting ------------------------------------------------------------------------------------------------------------

def _eligible(production: Mapping[str, Any]) -> None:
    name = production.get("agentRuntimeName")
    if production.get("status") != "READY":
        raise ExperimentError(f"{name} is {production.get('status')}: wait until it is READY")
    protocol = (production.get("protocolConfiguration") or {}).get("serverProtocol") or "HTTP"
    if protocol != "HTTP":
        raise ExperimentError(f"{name} speaks {protocol}: a canary splits HTTP agents (the console's payload, a Gateway runtime target)", status=400)
    if production.get("authorizerConfiguration"):
        raise ExperimentError(f"{name} takes JWT bearer tokens: the canary's Gateway invokes it with IAM (SigV4)", status=400)


def _digest(uri: str, resolve: Callable[[str], str | None] | None) -> str | None:
    """An image's digest: the one its URI pins, else what ``resolve`` reads from ECR for its tag."""
    found = deploy.IMAGE_URI.match(uri or "")
    if found and found["digest"]:
        return str(found["digest"])
    return resolve(uri) if resolve and found else None


def candidate_request(production: Mapping[str, Any], body: Mapping[str, Any], copy_name: str,
                      resolve: Callable[[str], str | None] | None = None) -> tuple[dict[str, Any], list[str]]:
    """The candidate's deployment (the deploy module's request, validated) and what it changes. The candidate is built
    like production's artifact (code from a zip, a container from a Dockerfile or an image production's role pulls).
    An image is compared with production's by digest (``resolve`` reads a tag's from ECR; ``req["digest"]``): the
    same image is no image change, and with the same variables nothing to compare."""
    if not isinstance(body, Mapping):
        raise ExperimentError("candidate: the new version, as the deploy page sends it (source, archive or imageUri, environment)", status=400)
    current = dict(production.get("environmentVariables") or {})
    given = body.get("environment")
    try:
        environment = {**current, **deploy.check_environment(given)} if given is not None else dict(current)
        request = {k: v for k, v in body.items() if k not in ("name", "endpoint", "protocol", "smoke", "environment")}
        request.update(name=copy_name, endpoint="", environment=environment, smoke=True,
                       protocol=(production.get("protocolConfiguration") or {}).get("serverProtocol") or "HTTP")
        req = deploy.deploy_request(request, mode="create")
    except deploy.DeployError as exc:
        raise ExperimentError(f"candidate: {exc}", status=400) from exc
    kind = artifact_kind(production.get("agentRuntimeArtifact") or {})
    if kind == "code" and req["source"] != "zip":
        raise ExperimentError(f"{production['agentRuntimeName']} runs code from a zip: the candidate is a zip too", status=400)
    if kind == "container" and req["source"] == "zip":
        raise ExperimentError(f"{production['agentRuntimeName']} runs a container image: the candidate is a Dockerfile build or an image", status=400)
    changed = sorted(k for k in set(current) | set(environment) if current.get(k) != environment.get(k))
    if req["source"] == "image":
        uri = str(((production.get("agentRuntimeArtifact") or {}).get("containerConfiguration") or {}).get("containerUri") or "")
        if _image_repository(req["image"]) != _image_repository(uri):
            raise ExperimentError(f"the candidate runs with production's role, which pulls from {_image_repository(uri)}: push the candidate image "
                                  "there, or give its Dockerfile", status=400)
        mine, theirs = _digest(req["image"], resolve), _digest(uri, resolve)
        same = mine == theirs if mine and theirs else req["image"] == uri
        req["digest"] = mine
        if same and not changed:
            raise ExperimentError("the candidate is production's own image and variables: nothing to compare", status=400)
        what = [] if same else ["image"]
    else:
        what = ["code" if req["source"] == "zip" else "image (Dockerfile)"]
    return req, what + [f"env:{k}" for k in changed]


def start_canary(x: ex.Ctx, body: Mapping[str, Any]) -> dict[str, Any]:
    """Check the request, keep the record, and set the canary up in a console job (kind ``runtime-canary``)."""
    _admin(x, "a runtime canary deploys a candidate runtime: it")
    if body.get("acknowledged") is not True:
        raise ExperimentError("a canary deploys the candidate as a runtime of its own and creates a Gateway, two online evaluations (billed for "
                              "every session they score), an A/B test and an endpoint on production, kept until it is cleaned up: send "
                              "acknowledged: true", status=400)
    ctl = x.ctl()
    runtime_id = str(body.get("runtimeId") or "")
    try:
        production, _tags = deploy._console_runtime(ctl, runtime_id)
        deploy._busy(x.console, x.workspace, str(production["agentRuntimeName"]))
    except deploy.DeployError as exc:
        raise ExperimentError(str(exc), status=400 if "running" not in str(exc) else 409) from exc
    _eligible(production)
    weight = ex._start_weight(body.get("treatmentWeight", ex.CANARY_STEPS[0]))
    evaluators = ex._evaluators(body.get("evaluators"))
    metric = ex._metric(body.get("metric"), evaluators)
    timeout = ex._int(body.get("sessionTimeout"), ex.SESSION_TIMEOUT_MINUTES, "sessionTimeout (minutes)", 1, 60)
    hexid = secrets.token_hex(4)
    cid = f"can-{hexid}"
    n = names(hexid, str(production["agentRuntimeName"]))
    req, changed = candidate_request(production, body.get("candidate") or {}, n["copy"], resolve=lambda uri: _resolve_digest(x, uri))
    record = _record(x, runtime_id)
    rec = {"id": cid, "workspace": x.workspace, "name": str(body.get("name") or f"{production['agentRuntimeName']} 金丝雀")[:80], "status": "creating",
           "agent": {"id": runtime_id, "name": production["agentRuntimeName"], "arn": production["agentRuntimeArn"],
                     "version": str(production["agentRuntimeVersion"]), "endpoint": record.get("endpoint") or None,
                     "artifact": production.get("agentRuntimeArtifact"), "kind": artifact_kind(production.get("agentRuntimeArtifact") or {})},
           "candidate": {**deploy.public_params(req), "changed": changed, "description": str(body.get("description") or "")[:300],
                         "digest": req.get("digest"), "built": req["source"] == "dockerfile"},
           "treatment": {"fingerprint": None}, "names": n, "weights": {"C": 100 - weight, "T1": weight}, "evaluators": evaluators, "metric": metric,
           "sessionTimeout": timeout, "ramp": [{"at": _now(), "by": x.caller.get("username"), "weights": {"C": 100 - weight, "T1": weight}}],
           "createdAt": _now(), "createdBy": x.caller.get("username"), "promotion": None, "rollback": None, "error": None}
    with deploy.moves(runtime_id, busy=_busy_error):
        if any(c.get("status") in BUSY and c["agent"]["id"] == runtime_id for c in _canaries(x)):  # settled: a stuck setup no longer counts
            raise ExperimentError(f"{production['agentRuntimeName']} already has a canary: roll it back or promote it, and clean it up first")
        _production_free(x, rec)
        x.console.store.update(COLLECTION, {}, lambda all_: {**all_, cid: rec})
    job = x.console.jobs.start("runtime-canary", x.workspace, {"canary": cid, "agent": production["agentRuntimeName"], "runtimeId": runtime_id,
                                                               "candidate": rec["candidate"]}, lambda j: _setup(x, cid, req, j),
                               label=f"代码 Agent 金丝雀 {production['agentRuntimeName']}")
    return {"canary": _save(x, cid, job=job["id"]), "job": job}


def _resolve_digest(x: ex.Ctx, uri: str) -> str | None:
    """The digest ECR has for an image URI's tag (a candidate given as ``<repository>:<tag>``)."""
    found = deploy.IMAGE_URI.match(uri or "")
    if not found or not found["tag"]:
        return None
    try:
        details = client(x.session, "ecr", x.region).describe_images(repositoryName=found["repo"], imageIds=[{"imageTag": found["tag"]}]).get(
            "imageDetails") or []
    except Exception as exc:  # noqa: BLE001
        if deploy._code(exc) in ("ImageNotFoundException", "RepositoryNotFoundException"):
            raise ExperimentError(f"candidate: no image {uri} in ECR", status=400) from exc
        raise
    return str(details[0].get("imageDigest") or "") or None if details else None


def _wait_endpoint(ctl: Any, runtime_id: str, name: str, version: str) -> None:
    def probe() -> str:
        got = ctl.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=name)
        status = str(got.get("status"))
        return "LIVE" if status == "READY" and str(got.get("liveVersion") or "") == str(version) else status

    ex._wait(probe, ("LIVE",), what=f"endpoint {name}", attempts=150, pause=4.0)


def _setup(x: ex.Ctx, cid: str, req: Mapping[str, Any], job: Any) -> dict[str, Any]:
    """The canary's AWS resources, each kept on the record as soon as it exists (cleanup removes what a failed setup made)."""
    rec = _get(x, cid)
    n, runtime_id, version = rec["names"], rec["agent"]["id"], rec["agent"]["version"]
    tags = {**CONSOLE_TAG, CANARY_TAG: cid}
    ctl, data = x.ctl(), x.data()
    try:
        production = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
        if str(production.get("agentRuntimeVersion")) != version:
            raise ExperimentError(f"{rec['agent']['name']} moved to version {production.get('agentRuntimeVersion')} while the canary was being set up")
        job.log(f"control endpoint {n['control']} on {rec['agent']['name']}: version {version} (DEFAULT and its own endpoints stay as they are)")
        ctl.create_agent_runtime_endpoint(agentRuntimeId=runtime_id, name=n["control"], agentRuntimeVersion=version, tags=tags, clientToken=_token(),
                                          description=f"ADLC console canary {cid}: the control arm, pinned to version {version}")
        rec = _save(x, cid, controlEndpoint=n["control"])
        _wait_endpoint(ctl, runtime_id, n["control"], version)

        job.log(f"candidate runtime {n['copy']}: the deploy module's stages, with {rec['agent']['name']}'s role and settings")

        def keep(fields: Mapping[str, Any]) -> None:
            _save(x, cid, copy={"id": fields["runtimeId"], "arn": fields["runtimeArn"], "name": fields["name"], "version": fields.get("version"),
                                "job": fields.get("jobId")})

        out = CandidatePipeline(x.session, account=x.account, region=x.region, request=req, job=job, names=NAMES, production=production, tags=tags,
                                on_runtime=keep, sleep=_sleep, boundary=x.boundary).run()
        copy = ctl.get_agent_runtime(agentRuntimeId=out["runtimeId"])
        env = dict(copy.get("environmentVariables") or {})
        copied = {"id": out["runtimeId"], "arn": out["runtimeArn"], "name": out["name"], "version": str(out["version"]), "job": job.job["id"],
                  "artifact": copy.get("agentRuntimeArtifact"), "environment": sorted(env), "smoke": out.get("smoke"),
                  "stages": [{k: s.get(k) for k in ("name", "status", "seconds", "detail")} for s in out.get("stages") or []]}
        rec = _save(x, cid, copy=copied, treatment={"fingerprint": fingerprint(runtime_id, copied["arn"], copied["version"], copied["artifact"] or {}, env)})

        job.log(f"treatment endpoint {n['treatment']} on {n['copy']}: version {copied['version']} (its DEFAULT keeps the smoke call and verifications)")
        ctl.create_agent_runtime_endpoint(agentRuntimeId=copied["id"], name=n["treatment"], agentRuntimeVersion=copied["version"], tags=tags,
                                          clientToken=_token(), description=f"ADLC console canary {cid}: the treatment arm")
        rec = _save(x, cid, treatmentEndpoint=n["treatment"])
        _wait_endpoint(ctl, copied["id"], n["treatment"], copied["version"])

        job.log(f"role {n['role']}: the Gateway (InvokeAgentRuntime of both arms), the A/B test and the online evaluations")
        trust, policy = role_documents(x.account, x.region, [rec["agent"]["arn"], copied["arn"]])
        role_arn = ex._ensure_role(x.iam(), n["role"], trust, policy, tags, x.boundary)
        rec = _save(x, cid, role={"name": n["role"], "arn": role_arn})
        _sleep(10)  # IAM propagation
        job.log(f"Gateway {n['gateway']} (AWS_IAM)")
        request = {**ex.gateway_request(n["gateway"], role_arn, tags), "description": "ADLC console runtime canary: sessions split between production and the candidate"}
        gw = ex._retry(lambda: ctl.create_gateway(**request))
        rec = _save(x, cid, gateway={"id": gw["gatewayId"], "arn": gw["gatewayArn"], "url": gw.get("gatewayUrl")})
        ex._wait(lambda: ctl.get_gateway(gatewayIdentifier=gw["gatewayId"])["status"], ("READY",), what=f"Gateway {n['gateway']}")
        url = rec["gateway"]["url"] or ctl.get_gateway(gatewayIdentifier=gw["gatewayId"]).get("gatewayUrl")
        rec = _save(x, cid, gateway={**rec["gateway"], "url": url})
        targets: dict[str, dict[str, str]] = {}
        for variant, arn, qualifier in (("C", rec["agent"]["arn"], n["control"]), ("T1", copied["arn"], n["treatment"])):
            job.log(f"target {n[variant]}: {arn.rsplit('/', 1)[-1]} ({qualifier})")
            made = ex._retry(lambda a=arn, q=qualifier, v=variant: ctl.create_gateway_target(gatewayIdentifier=gw["gatewayId"], **target_request(n[v], a, q)))
            targets[variant] = {"name": n[variant], "id": made["targetId"]}
            rec = _save(x, cid, targets=dict(targets))
        for variant in ("C", "T1"):
            ex._wait(lambda v=variant: ctl.get_gateway_target(gatewayIdentifier=gw["gatewayId"], targetId=targets[v]["id"])["status"], ("READY",),
                     what=f"target {n[variant]}")
        logs = x.logs()
        made_groups: list[str] = list(rec.get("logGroups") or [])
        evals: dict[str, dict[str, str]] = {}
        for variant, arm_id, endpoint, name in (("C", runtime_id, n["control"], n["evalC"]), ("T1", copied["id"], n["treatment"], n["evalT1"])):
            group = f"/aws/bedrock-agentcore/runtimes/{arm_id}-{endpoint}"
            try:  # an online evaluation refuses a log group that does not exist yet; an endpoint's appears with its first session
                logs.create_log_group(logGroupName=group, tags=tags)
                made_groups.append(group)
            except Exception as exc:  # noqa: BLE001
                if "AlreadyExists" not in ex._code(exc):
                    raise
            rec = _save(x, cid, logGroups=list(dict.fromkeys(made_groups)))
            job.log(f"online evaluation {name}: {', '.join(rec['evaluators'])} on every session of {ex.runtime_name(arm_id)} ({endpoint})")
            made = ex._retry(lambda r=arm_id, e=endpoint, m=name: ctl.create_online_evaluation_config(
                **ex.online_eval_request(m, r, e, rec["evaluators"], role_arn, tags, rec["sessionTimeout"])))
            evals[variant] = {"id": made["onlineEvaluationConfigId"], "arn": made["onlineEvaluationConfigArn"], "name": name}
            rec = _save(x, cid, onlineEvaluations=dict(evals))
        for variant in ("C", "T1"):
            ex._wait(lambda v=variant: ctl.get_online_evaluation_config(onlineEvaluationConfigId=evals[v]["id"])["status"], ("ACTIVE",),
                     what=f"online evaluation {evals[variant]['name']}")
        job.log(f"A/B test {n['abTest']}: {rec['weights']['C']} % production, {rec['weights']['T1']} % candidate")
        ab_request = {**ex.ab_test_request(n["abTest"], gw["gatewayArn"], {v: t["name"] for v, t in targets.items()}, rec["weights"]["T1"],
                                           {v: e["arn"] for v, e in evals.items()}, role_arn, tags),
                      "description": "ADLC console runtime canary: production's pinned version against the candidate runtime"}
        made = ex._retry(lambda: data.create_ab_test(**ab_request))
        rec = _save(x, cid, abTest={"id": made["abTestId"], "arn": made["abTestArn"]})

        def started() -> str:
            ab = data.get_ab_test(abTestId=made["abTestId"])
            return str(ab["status"]) if "FAIL" in str(ab.get("status")) else str(ab.get("executionStatus"))

        ex._wait(started, ("RUNNING",), what=f"A/B test {n['abTest']}")
        rec = _save(x, cid, status="running", invokeUrl=f"{url.rstrip('/')}/{targets['C']['name']}/invocations")
        job.log(f"running: the console's chat and /v1 go through {rec['invokeUrl']}")
        return {"canary": cid, "invokeUrl": rec["invokeUrl"], "copy": copied["name"]}
    except Exception as exc:
        _save(x, cid, status="failed", error=f"{type(exc).__name__}: {str(exc)[:500]}")
        raise


# -- reading, the split, traffic -------------------------------------------------------------------------------------------

def _endpoint_arm(ctl: Any, runtime_id: str, name: str, expected: Any, what: str) -> dict[str, Any]:
    try:
        got = ctl.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=name)
        live, target = got.get("liveVersion"), got.get("targetVersion")
    except Exception as exc:  # noqa: BLE001
        if not ex._gone(exc):
            raise
        live = target = None
    return {"what": what, "expected": str(expected), "now": None if live in (None, "") else str(live), "target": None if target in (None, "") else str(target)}


def _arms(ctl: Any, rec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What the A/B test compares besides production's DEFAULT, as AgentCore has it now: production's control endpoint
    (on the version compared), the candidate runtime and its treatment endpoint (on the version set up)."""
    arms = []
    if rec.get("controlEndpoint"):
        arms.append(_endpoint_arm(ctl, rec["agent"]["id"], rec["controlEndpoint"], rec["agent"]["version"],
                                  f"production's control endpoint {rec['controlEndpoint']}"))
    copy = rec.get("copy") or {}
    if copy.get("id") and copy.get("version"):
        try:
            now: str | None = str(ctl.get_agent_runtime(agentRuntimeId=copy["id"]).get("agentRuntimeVersion"))
        except Exception as exc:  # noqa: BLE001
            if not ex._gone(exc):
                raise
            now = None
        arms.append({"what": f"the candidate {copy.get('name')}", "expected": str(copy["version"]), "now": now, "target": None})
        if rec.get("treatmentEndpoint"):
            arms.append(_endpoint_arm(ctl, copy["id"], rec["treatmentEndpoint"], copy["version"], f"the treatment endpoint {rec['treatmentEndpoint']}"))
    return arms


def _gate(x: ex.Ctx, rec: Mapping[str, Any], metric: str | None = None) -> dict[str, Any]:
    live, metrics = ex._live(x, rec)
    verification, verifying = ex.find_verification(x, rec) if (rec.get("treatment") or {}).get("fingerprint") else (None, False)
    ctl = x.ctl()
    try:
        state: dict[str, Any] | None = production_state(ctl, rec["agent"]["id"])
    except Exception as exc:  # noqa: BLE001
        if not ex._gone(exc):
            raise
        state = None
    chosen = metric or rec["metric"]
    for m in metrics:
        m["band"], m["bandSource"] = ex.band_for(verification, m["evaluator"])
    decided = ex.gate(rec, verification=verification, metrics=metrics, metric=chosen, agent_version=state["version"] if state else None,
                      verifying=verifying, arms=_arms(ctl, rec))
    first = decided["conditions"][0]
    if verification is None and not verifying:
        first["evidence"] = "no console verification of the candidate: verify the candidate runtime against a contract set"
    return {"live": live, "metrics": metrics, "verification": verification, "verifying": verifying, "production": state, "gate": decided}


def canary_detail(x: ex.Ctx, cid: str) -> dict[str, Any]:
    """The record, the A/B test as AgentCore has it now, its metrics, production as it is now and the gate; once
    promoted, the gate as it was decided."""
    rec = _get(x, cid)
    if rec["status"] in ("creating", "failed", "cleanup_incomplete") or rec.get("cleanedAt"):
        production = None
        if rec["status"] != "creating":
            try:
                production = production_state(x.ctl(), rec["agent"]["id"])
            except Exception as exc:  # noqa: BLE001
                if not ex._gone(exc):
                    raise
        final = rec.get("finalResults") or {}
        verification = ex.find_verification(x, rec)[0] if (rec.get("treatment") or {}).get("fingerprint") else None
        metrics = [dict(m) for m in final.get("metrics") or []]
        for m in metrics:
            m["band"], m["bandSource"] = ex.band_for(verification, m["evaluator"])
        live = {"analysisTimestamp": final.get("analysisTimestamp"), "executionStatus": None, "final": True} if final else None
        decided = (rec.get("promotion") or {}).get("gate")
        return {**rec, "live": live, "metrics": metrics, "verification": verification, "production": production,
                "gate": {**decided, "decidedAt": rec["promotion"]["at"]} if decided else None}
    out = {**rec, **_gate(x, rec)}
    if rec.get("promotion") and rec["promotion"].get("gate"):
        out["gate"] = {**rec["promotion"]["gate"], "decidedAt": rec["promotion"]["at"]}
    return out


def set_split(x: ex.Ctx, cid: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """The canary ramp: a new candidate share (pause, change the weights, resume), an admin's
    (``experiments.split_allowed``: above 50 % only once the gate holds; raising refused while the A/B result shows the
    candidate worse beyond the noise band, unless ``acknowledged: true``)."""
    with deploy.moves(_get(x, cid)["agent"]["id"], busy=_busy_error):
        return _set_split(x, cid, body)


def _set_split(x: ex.Ctx, cid: str, body: Mapping[str, Any]) -> dict[str, Any]:
    rec = _get(x, cid)  # under production's lock: as another admin's action left it
    if rec["status"] not in ("running", "paused") or not rec.get("abTest"):
        raise ExperimentError(f"canary {cid} is {rec['status']}: only a running or paused A/B test takes a new split")
    weight = ex._weight(body.get("treatmentWeight"))
    before = int(rec["weights"]["T1"])
    ex.split_allowed(x, rec, weight, before, body.get("acknowledged") is True, lambda: _gate(x, rec)["gate"], arm="candidate", base="production")
    data = x.data()
    ab_id = rec["abTest"]["id"]
    was = data.get_ab_test(abTestId=ab_id).get("executionStatus")
    if was == "STOPPED":
        raise ExperimentError(f"A/B test {ab_id} is stopped")
    if was == "RUNNING":
        data.update_ab_test(abTestId=ab_id, executionStatus="PAUSED", clientToken=_token())
        ex._ab_wait(data, ab_id, execution="PAUSED")
    data.update_ab_test(abTestId=ab_id, variants=ex.variants({v: t["name"] for v, t in rec["targets"].items()}, weight), clientToken=_token())
    ex._ab_wait(data, ab_id, execution=str(was if was != "RUNNING" else "PAUSED"))
    if was == "RUNNING":
        data.update_ab_test(abTestId=ab_id, executionStatus="RUNNING", clientToken=_token())
        ex._ab_wait(data, ab_id, execution="RUNNING")
    weights = {"C": 100 - weight, "T1": weight}
    step = {"at": _now(), "by": x.caller.get("username"), "weights": weights,
            **({"acknowledged": True} if weight > before and body.get("acknowledged") is True else {})}
    return _save(x, cid, weights=weights, ramp=[*rec.get("ramp", []), step])


def _stop(x: ex.Ctx, rec: Mapping[str, Any], *, status: str) -> dict[str, Any]:
    data = x.data()
    ab = data.get_ab_test(abTestId=rec["abTest"]["id"])
    if ab.get("executionStatus") != "STOPPED":
        data.update_ab_test(abTestId=rec["abTest"]["id"], executionStatus="STOPPED", clientToken=_token())
        ab = ex._ab_wait(data, rec["abTest"]["id"], execution="STOPPED")
    final = {"at": _now(), "metrics": ex.ab_metrics(ab) or list((rec.get("finalResults") or {}).get("metrics") or []),
             "analysisTimestamp": str((ab.get("results") or {}).get("analysisTimestamp") or "") or None}
    return _save(x, rec["id"], status=status, finalResults=final)


def set_state(x: ex.Ctx, cid: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Pause (every session goes to production's pinned version), resume, or stop the A/B test (its results kept)."""
    with deploy.moves(_get(x, cid)["agent"]["id"], busy=_busy_error):
        return _set_state(x, cid, body)


def _set_state(x: ex.Ctx, cid: str, body: Mapping[str, Any]) -> dict[str, Any]:
    rec = _get(x, cid)  # under production's lock: as another admin's action left it
    state = str(body.get("executionStatus") or "")
    if state not in ("PAUSED", "RUNNING", "STOPPED"):
        raise ExperimentError("executionStatus: PAUSED, RUNNING or STOPPED", status=400)
    if rec["status"] not in ("running", "paused") or not rec.get("abTest"):
        raise ExperimentError(f"canary {cid} is {rec['status']}")
    if state == "STOPPED":
        return _stop(x, rec, status="stopped")
    data = x.data()
    data.update_ab_test(abTestId=rec["abTest"]["id"], executionStatus=state, clientToken=_token())
    ex._ab_wait(data, rec["abTest"]["id"], execution=state)
    return _save(x, cid, status="paused" if state == "PAUSED" else "running")


def observed_split(x: ex.Ctx, cid: str, *, hours: int = 24) -> dict[str, Any]:
    """The sessions each arm served, as ``aws/spans`` has them (a few minutes behind): distinct ``session.id`` per arm's
    ``service.name``."""
    rec = _get(x, cid)
    if not rec.get("copy") or not rec.get("treatmentEndpoint"):
        raise ExperimentError(f"canary {cid} has no candidate runtime yet")
    services = {"C": f"{ex.runtime_name(rec['agent']['id'])}.{rec['controlEndpoint']}", "T1": f"{ex.runtime_name(rec['copy']['id'])}.{rec['treatmentEndpoint']}"}
    logs = x.logs()
    end = int(time.time())
    query = ("fields `resource.attributes.service.name` as service, `attributes.session.id` as sid | filter ispresent(sid) and service in ["
             + ", ".join(json.dumps(s) for s in services.values()) + "] | stats count_distinct(sid) as sessions by service")
    started = logs.start_query(logGroupName="aws/spans", startTime=end - int(hours) * 3600, endTime=end, queryString=query)
    result: dict[str, Any] = {"status": "Running", "results": []}
    for _ in range(60):
        result = logs.get_query_results(queryId=started["queryId"])
        if result.get("status") in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        _sleep(1.0)
    counts = {str(next((c["value"] for c in row if c["field"] == "service"), "")): int(float(next((c["value"] for c in row if c["field"] == "sessions"), 0)))
              for row in result.get("results") or []}
    arms = {v: {"service": s, "sessions": counts.get(s, 0)} for v, s in services.items()}
    total = sum(a["sessions"] for a in arms.values())
    return {"arms": arms, "total": total, "share": {v: (round(a["sessions"] / total, 3) if total else None) for v, a in arms.items()},
            "weights": rec["weights"], "status": result.get("status"), "hours": hours}


#: Failures before the request reaches AWS (the proxy refused the connection, the TLS handshake broke, the connection
#: closed before the request was out): a retry cannot send a turn twice. Live 2026-10-01, 9 of 20 parallel sessions
#: through a local HTTPS proxy failed so on the first try. Only while the request's last byte has not gone to the
#: socket (``experiments.SentBody``): botocore raises the same SSLError for a TLS record broken while the answer is
#: read, after the arm got the whole turn (and ConnectionClosedError for a connection dropped then).
UNSENT = ("ProxyConnectionError", "SSLError", "EndpointConnectionError", "ConnectTimeoutError", "ConnectionClosedError")
POST_ATTEMPTS = 4


class SentError(Exception):
    """A failure :data:`UNSENT` names that came after the whole request went to the socket: the arm may have run the
    turn, so it is neither sent again nor answered by production."""

    def __init__(self, cause: BaseException):
        super().__init__(f"{type(cause).__name__} after the whole request was sent: {str(cause)[:300]}")


def post_signed(session: Any, region: str, url: str, data: bytes, headers: Mapping[str, str], *, timeout: float = 180.0) -> tuple[int, str, bytes]:
    """The experiments' SigV4 POST (service ``bedrock-agentcore``), sent again only while it never left (``UNSENT``,
    before its last byte went out); such a failure after that is a :class:`SentError`."""
    for attempt in range(1, POST_ATTEMPTS + 1):
        body = ex.SentBody(data)
        try:
            return ex.post_signed(session, region, url, body, headers, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ not in UNSENT:
                raise
            if body.sent:
                raise SentError(exc) from exc
            if attempt == POST_ATTEMPTS:
                raise
            _sleep(1.5 * attempt)
    raise AssertionError("unreachable")


def _headers(sid: str, user: str) -> dict[str, str]:
    return {"Content-Type": "application/json", SESSION_HEADER: sid, USER_HEADER: re.sub(r"[^A-Za-z0-9_.:@-]", "-", user)[:128] or "console"}


def route_for(console: Any, workspace: str, runtime_id: str) -> dict[str, Any] | None:
    """The running canary of a runtime, if it has one: its traffic should go through the canary's Gateway."""
    for rec in (console.store.read(COLLECTION, {}) or {}).values():
        if (rec.get("workspace") == workspace and rec.get("status") == "running" and rec.get("invokeUrl")
                and (rec.get("agent") or {}).get("id") == runtime_id):
            return rec
    return None


def invoke_through(session: Any, region: str, rec: Mapping[str, Any], *, message: str, session_id: str | None, actor: str) -> Iterator[dict[str, Any]]:
    """One turn of the console chat or the public API through a running canary's Gateway, as the events of
    ``agents.invoke`` (the runtime's answer as it came) plus ``{"type": "experiment"}``: the Gateway decides the
    session's arm and keeps it there. Production answers directly (its DEFAULT has not moved) only when the request
    never left (:data:`UNSENT`, after ``post_signed``'s retries); a timeout or an error answer may come after an arm ran
    the turn, so it is an ``error``, never a second run."""
    from . import agents

    if not message.strip() or len(message) > 20_000:
        raise agents.AgentError("message: 1-20000 characters")
    sid = session_id or agents.new_session()
    if not agents.SESSION.match(sid):
        raise agents.AgentError("sessionId: 33-100 letters, digits, '-' or '_'")
    yield {"type": "session", "sessionId": sid}
    yield {"type": "experiment", "kind": "runtime-canary", "id": rec["id"], "name": rec.get("name"), "weights": rec.get("weights")}
    started = time.monotonic()
    payload = json.dumps({"prompt": message, "actorId": actor}, ensure_ascii=False).encode("utf-8")
    try:
        status, _kind, raw = post_signed(session, region, rec["invokeUrl"], payload, _headers(sid, actor))
    except Exception as exc:  # noqa: BLE001
        problem = f"{type(exc).__name__}: {str(exc)[:200]}"
        if type(exc).__name__ not in UNSENT:  # it may have reached an arm: the turn is not run again
            yield {"type": "error", "error": f"the canary's Gateway did not answer ({problem}); the turn may have run, so it is not sent again"}
            return
        yield {"type": "fallback", "error": problem, "via": "production (DEFAULT)"}  # it never left: production answers instead
        for event in agents.invoke(session, region=region, agent={"kind": "runtime", "arn": rec["agent"]["arn"]}, message=message, session_id=sid,
                                   actor=actor):
            if event["type"] != "session":
                yield event
        return
    if status != 200:
        yield {"type": "error", "error": f"the canary's Gateway answered {status}: {raw.decode('utf-8', 'replace')[:200]}"}
        return
    yield {"type": "text", "text": raw.decode("utf-8", "replace")}
    yield {"type": "stop", "seconds": round(time.monotonic() - started, 1)}


def send_traffic(x: ex.Ctx, cid: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Replay prompts (a contract set's questions, or ``prompts``) through the canary's Gateway, one new session each,
    ``repeat`` times (console job ``runtime-canary-traffic``)."""
    rec = _get(x, cid)
    if rec["status"] not in ("running", "paused") or not rec.get("invokeUrl"):
        raise ExperimentError(f"canary {cid} is {rec['status']}: traffic goes to a running A/B test")
    prompts = ex.contract_queries(x, str(body["contractSet"])) if body.get("contractSet") else [str(p).strip() for p in body.get("prompts") or [] if str(p).strip()]
    repeat = ex._int(body.get("repeat"), 1, "repeat", 1, 5)
    if not prompts or len(prompts) * repeat > ex.MAX_TRAFFIC:
        raise ExperimentError(f"give a contractSet or prompts, at most {ex.MAX_TRAFFIC} sessions in all", status=400)
    hexid, url, session, region = cid[4:], rec["invokeUrl"], x.session, x.region
    items = list(enumerate([q for _ in range(repeat) for q in prompts], 1))

    def work(job: Any) -> dict[str, Any]:
        def one(item: tuple[int, str]) -> dict[str, Any]:
            index, query = item
            sid = f"can-{hexid}-{secrets.token_hex(16)}"
            payload = json.dumps({"prompt": query, "actorId": f"can-{hexid}-{index}"}, ensure_ascii=False).encode("utf-8")
            try:
                status, kind, raw = post_signed(session, region, url, payload, _headers(sid, f"can-{hexid}-traffic"))
                text, error = (deploy.answer_text(raw, kind), None) if status == 200 else ("", raw.decode("utf-8", "replace")[:300])
            except Exception as exc:  # noqa: BLE001 - counted, shown on the job
                status, text, error = 0, "", f"{type(exc).__name__}: {str(exc)[:200]}"
            return {"sessionId": sid, "query": query[:200], "status": status, "answer": text[:300], "error": error}

        job.log(f"{len(items)} session(s) through {url}")
        with ThreadPoolExecutor(max_workers=ex.TRAFFIC_WORKERS) as pool:
            done = list(pool.map(one, items))
        failed = [d for d in done if d["status"] != 200 or d["error"]]
        statuses: dict[str, int] = {}
        for d in done:
            statuses[str(d["status"])] = statuses.get(str(d["status"]), 0) + 1
        job.progress(sent=len(done) - len(failed), failed=len(failed))
        for d in failed[:3]:
            job.log(f"failed {d['status']}: {d['error']}")
        return {"sent": len(done) - len(failed), "failed": len(failed), "statuses": statuses, "samples": done[:20],
                "sessions": [d["sessionId"] for d in done if d["status"] == 200]}

    return x.console.jobs.start("runtime-canary-traffic", x.workspace, {"canary": cid, "sessions": len(items)}, work,
                                label=f"金丝雀流量 {rec['agent']['name']}")


def verify_candidate(x: ex.Ctx, cid: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """A console verification (job kind ``verify``) of the candidate runtime: N rounds of a contract set on its DEFAULT
    (never the treatment endpoint, so these sessions stay out of the A/B test), L1, and the canary's evaluators as the
    panel that measures their noise bands."""
    from .evaluation import EvaluationError, contract_set

    rec = _get(x, cid)
    copy = rec.get("copy") or {}
    if rec["status"] not in ("running", "paused", "stopped") or not copy.get("arn") or not rec["treatment"].get("fingerprint"):
        raise ExperimentError(f"canary {cid} is {rec['status']}: a candidate runtime is verified while its canary runs")
    try:
        cs = contract_set(x.console.store, x.workspace, str(body.get("contractSet") or ""))
    except EvaluationError as exc:
        raise ExperimentError(str(exc), status=404) from exc
    repeat = ex._int(body.get("repeat"), 3, "repeat (rounds)", 1, 5)
    panel = tuple(e for e in rec["evaluators"] if e not in ex.RATE_ONLY) if body.get("panel", True) else ()
    ctl = x.ctl()
    version = production_state(ctl, rec["agent"]["id"])["version"]
    candidate = str(ctl.get_agent_runtime(agentRuntimeId=copy["id"]).get("agentRuntimeVersion"))
    if copy.get("version") and candidate != str(copy["version"]):  # its DEFAULT is what is verified: it must be what the canary measures
        raise ExperimentError(f"the candidate {copy['name']} is version {candidate} now, not version {copy['version']} the canary measures: "
                              "a verification of it would not be about this canary's candidate (start a new canary)")
    marker = {"fingerprint": rec["treatment"]["fingerprint"], "agentVersion": version, "canary": cid, "runtime": copy["name"],
              "candidateVersion": candidate}
    info = {"name": copy["name"], "id": copy["id"], "arn": copy["arn"], "runtimeId": copy["id"], "model": None, "targets": []}
    out_dir = x.console.data_dir / "console" / "verifications"
    session, region = x.session, x.region

    def work(job: Any) -> dict[str, Any]:
        run = RuntimeVerify(session, info, cs["contracts"], cs["l1"], region=region, out=Path(out_dir) / job.job["id"], repeat=repeat, panel=panel,
                            log=job.log)
        doc = run.run()
        job.progress(robust=doc["robust"], holding=doc["holding"], contracts=len(doc["contracts"]))
        return {**doc, "markdown": verify.render_markdown(doc), "contractSet": cs["id"], "treatment": marker}

    return x.console.jobs.start("verify", x.workspace, {"agent": rec["agent"]["name"], "agentId": rec["agent"]["id"], "contractSet": cs["id"],
                                                         "repeat": repeat, "panel": bool(panel), "treatment": marker, "runtime": copy["name"]},
                                work, label=f"验证 {rec['agent']['name']} 的候选版本（{copy['name']}）")


# -- promotion and rollback --------------------------------------------------------------------------------------------------

def promote(x: ex.Ctx, cid: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Publish the candidate as production's next version only when the gate holds; an admin's ``acknowledged: true``
    (with a reason) overrides a failed gate, recorded with the evidence it overrode (console job
    ``runtime-canary-promote``)."""
    _admin(x, "promotion")
    rec = _get(x, cid)
    if rec["status"] not in ("running", "paused", "stopped") or not (rec.get("copy") or {}).get("artifact"):
        raise ExperimentError(f"canary {cid} is {rec['status']}: nothing to promote")
    metric = rec["metric"]  # the canary's own, declared when it started: another one is an override
    switched = ex.switched_metric(rec, body.get("metric"))
    decided = _gate(x, rec, metric)["gate"]
    if switched:
        decided = {**decided, "ok": False, "conditions": [*decided["conditions"], ex.metric_condition(metric, switched, _gate(x, rec, switched)["gate"])]}
    failed = [c for c in decided["conditions"] if not c["ok"]]
    override = bool(failed) and body.get("acknowledged") is True
    reason = str(body.get("reason") or "").strip()[:500]
    if failed and not override:
        raise ExperimentError("promotion refused: " + "; ".join(f"{c['id']}: {c['evidence']}" for c in failed), gate=decided)
    if override and len(reason) < 5:
        raise ExperimentError("an override of the gate needs a reason (why promote against the evidence)", status=400, gate=decided)
    decision = {"at": _now(), "by": x.caller.get("username"), "metric": metric, "requestedMetric": switched, "gate": decided, "override": override,
                "failed": [c["id"] for c in failed], "reason": reason if override else None}
    with deploy.moves(rec["agent"]["id"], busy=_busy_error):
        rec = _get(x, cid)  # under production's lock: another admin's promotion, rollback or stop may have come first
        if rec["status"] not in ("running", "paused", "stopped"):
            raise ExperimentError(f"canary {cid} is {rec['status']} now: nothing to promote")
        _production_free(x, rec)
        rec = _save(x, cid, status="promoting", decision=decision, promoteJob=None, error=None)
    job = x.console.jobs.start("runtime-canary-promote", x.workspace, {"canary": cid, "agent": rec["agent"]["name"], "override": override},
                               lambda j: _promote(x, cid, decision, j), label=f"发布金丝雀候选 {rec['agent']['name']}")
    return {"canary": _save(x, cid, promoteJob=job["id"]), "job": job, "gate": decided}


def _busy_error(message: str) -> ExperimentError:
    return ExperimentError(message, status=409)


def _production_free(x: ex.Ctx, rec: Mapping[str, Any]) -> None:
    """Refused (409) while a deploy page job (a new version, or a delete) of production runs, or another canary's
    promotion or rollback moves it: production moves under one job at a time. Called under production's lock
    (``deploy.moves``), which the deploy page's checks take too."""
    try:
        deploy._busy(x.console, x.workspace, str(rec["agent"]["name"]))
    except deploy.DeployError as exc:
        raise ExperimentError(f"{exc}: production moves under one job at a time", status=409) from exc
    other = mover(x.console, x.workspace, rec["agent"]["id"], but=rec["id"])
    if other:
        raise ExperimentError(f"the canary {other['id']} of {rec['agent']['name']} is {other['status']}, and its job is moving production now: "
                              "wait for it", status=409)


def _runs(runtime: Mapping[str, Any]) -> tuple[str, str]:
    """What a version of a runtime runs, to tell versions apart: its artifact (an image by digest — the deploy module
    pins one so — or code by its S3 key, under the job that uploaded it) and its variables."""
    artifact = runtime.get("agentRuntimeArtifact") or {}
    uri = str((artifact.get("containerConfiguration") or {}).get("containerUri") or "")
    s3 = (((artifact.get("codeConfiguration") or {}).get("code") or {}).get("s3") or {})
    what = uri.split("@", 1)[-1] if uri else f"s3://{s3.get('bucket')}/{s3.get('prefix')}"
    return what, json.dumps(dict(runtime.get("environmentVariables") or {}), sort_keys=True)


def _published(ctl: Any, runtime_id: str, after: str, like: Mapping[str, Any]) -> tuple[str | None, str]:
    """The first version of a runtime after ``after`` that runs what ``like`` runs (:func:`_runs`), if any, and the
    version DEFAULT is on now: the version a promotion or rollback published, told apart from one a deploy made."""
    now = str(ctl.get_agent_runtime(agentRuntimeId=runtime_id).get("agentRuntimeVersion"))
    want = _runs(like)
    if not (after.isdigit() and now.isdigit()):
        return None, now
    for version in range(int(after) + 1, int(now) + 1):
        try:
            got = ctl.get_agent_runtime(agentRuntimeId=runtime_id, agentRuntimeVersion=str(version))
        except Exception as exc:  # noqa: BLE001
            if ex._gone(exc):
                continue
            raise
        if _runs(got) == want:
            return str(version), now
    return None, now


def _since(version: str, now: str) -> str:
    return "" if version == now else f"; production has moved on to version {now} since (not by this canary)"


#: How long a cut-off promotion or rollback whose check cannot read AWS stays undecided (read again with the record)
#: before it is settled as not known, so it can be cleaned up.
UNDECIDED_LIMIT = 15 * 60.0


def _promotion_cut(x: ex.Ctx, rec: Mapping[str, Any], decision: Mapping[str, Any], before: str, error: str, job_id: str, *,
                   expect: Mapping[str, Any] | None = None, since: Any = None) -> dict[str, Any]:
    """A promotion that did not finish: ``promoted`` (incomplete, recorded) if a version after ``before`` runs the
    candidate (its artifact and variables, read from the candidate runtime), else ``stopped``; left ``promoting`` while
    that cannot be read, up to :data:`UNDECIDED_LIMIT` after its job ended (``since``), then ``stopped`` as not known.
    ``expect``: what the record must still be for anything to be written (:func:`_save`)."""
    try:
        ctl = x.ctl()
        copy = rec.get("copy") or {}
        candidate = ctl.get_agent_runtime(agentRuntimeId=copy["id"], agentRuntimeVersion=str(copy["version"]))
        to, now = _published(ctl, rec["agent"]["id"], before, candidate)
    except Exception as exc:  # noqa: BLE001
        if ex._gone(exc):
            return _save(x, rec["id"], expect=expect, status="stopped", error=f"{error}; production or its candidate is gone ({ex._code(exc)}): check "
                                                                             "production on the deploy page")
        if since and ex._age(since) > UNDECIDED_LIMIT:
            return _save(x, rec["id"], expect=expect, status="stopped", error=(
                f"{error}; whether production runs the candidate could not be read for {int(UNDECIDED_LIMIT // 60)} min ({type(exc).__name__}): "
                "not known, so no promotion is recorded; check production on the deploy page"))
        # not known yet: left promoting (no job holds it, so nothing waits on it) and checked again when next read
        return {**rec, "error": f"{error}; whether production runs the candidate could not be read ({type(exc).__name__}): checked again when "
                                "the canary is next read"}
    if to:
        return _incomplete(x, rec, decision, before, to, error + _since(to, now), job_id, expect=expect)
    moved = f"production is version {now}, which does not run the candidate (a deploy outside the canary)" if now != before else "production did not move"
    return _save(x, rec["id"], expect=expect, status="stopped", error=f"{error}; {moved}")


def _incomplete(x: ex.Ctx, rec: Mapping[str, Any], decision: Mapping[str, Any], from_version: str, to_version: str, error: str,
                job_id: str, *, expect: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A promotion that published the candidate as ``to_version`` (:func:`_promotion_cut` found it) but did not
    finish: ``promoted`` (a rollback undoes it), and recorded in the release log like any promotion — override and
    all — marked ``incomplete``."""
    promotion = {"experiment": rec["id"], "kind": "runtime-canary", "workspace": x.workspace, "agent": rec["agent"]["name"],
                 "runtimeId": rec["agent"]["id"], "fromVersion": str(from_version), "toVersion": str(to_version),
                 "endpoint": rec["agent"].get("endpoint"), "changed": rec["candidate"].get("changed") or [], "candidate": (rec.get("copy") or {}).get("name"),
                 **decision, "incomplete": error, "job": job_id}

    def change(all_: list[dict[str, Any]]) -> list[dict[str, Any]]:  # decided inside the update: two settles at once log it once
        if any(p.get("experiment") == rec["id"] and p.get("kind") == "runtime-canary" and p.get("job") == job_id for p in all_):
            return all_
        return [*all_, promotion]

    saved = _save(x, rec["id"], expect=expect, status="promoted", error=error, promotion=promotion)
    if saved.get("status") == "promoted" and (saved.get("promotion") or {}).get("job") == job_id:  # not when another action moved it first
        x.console.store.update("promotions", [], change)
    return saved


def _promote(x: ex.Ctx, cid: str, decision: Mapping[str, Any], job: Any) -> dict[str, Any]:
    rec = _get(x, cid)
    production: Mapping[str, Any] = {}
    try:
        if rec.get("abTest"):
            job.log("stopping the A/B test (its results are kept with the canary)")
            rec = _stop(x, rec, status="promoting")
        ctl = x.ctl()
        production = ctl.get_agent_runtime(agentRuntimeId=rec["agent"]["id"])
        copy = ctl.get_agent_runtime(agentRuntimeId=rec["copy"]["id"])
        artifact, env = copy["agentRuntimeArtifact"], dict(copy.get("environmentVariables") or {})
        job.log(f"publishing the candidate's artifact and variables as {rec['agent']['name']}'s next version (DEFAULT moves to it"
                + (f", and the endpoint {rec['agent']['endpoint']})" if rec["agent"].get("endpoint") else ")"))
        req = publish_request(rec["agent"]["name"], artifact, env, endpoint=None)
        out = PublishPipeline(x.session, account=x.account, region=x.region, request=req, job=job, names=NAMES, current=production,
                              record=_record(x, rec["agent"]["id"]), artifact=artifact, sleep=_sleep, boundary=x.boundary,
                              on_runtime=lambda fields: deploy._remember(x.console.store, x.workspace, fields)).run()
        promotion = {"experiment": cid, "kind": "runtime-canary", "workspace": x.workspace, "agent": rec["agent"]["name"], "runtimeId": rec["agent"]["id"],
                     "fromVersion": str(production.get("agentRuntimeVersion")), "toVersion": str(out["version"]), "endpoint": out.get("endpoint") or None,
                     "changed": rec["candidate"].get("changed") or [], "candidate": rec["copy"]["name"], **decision, "artifact": artifact,
                     "smoke": out.get("smoke"), "job": job.job["id"]}
        x.console.store.update("promotions", [], lambda all_: [*all_, {k: v for k, v in promotion.items() if k not in ("artifact", "smoke")}])
        _save(x, cid, status="promoted", promotion=promotion, error=None)
        return promotion
    except Exception as exc:
        error = f"promotion: {type(exc).__name__}: {str(exc)[:400]}"
        if production:  # the candidate's version may exist (a later stage failed): production runs it, which a rollback undoes
            _promotion_cut(x, rec, decision, str(production.get("agentRuntimeVersion")), error, job.job["id"], expect={"status": "promoting"})
        else:
            _save(x, cid, status="stopped", error=error)
        raise


def rollback(x: ex.Ctx, cid: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Before a promotion: stop the split; production stays as it was (and the record shows it, read now). After one:
    production back to the version before (console job ``runtime-canary-rollback``)."""
    _admin(x, "a rollback")
    reason = str(body.get("reason") or "").strip()[:500]
    with deploy.moves(_get(x, cid)["agent"]["id"], busy=_busy_error):
        rec = _get(x, cid)  # under production's lock: another admin's promotion or rollback may have come first
        if rec["status"] in ("running", "paused", "stopped"):
            if rec.get("abTest"):
                rec = _stop(x, rec, status=rec["status"])
            now = production_state(x.ctl(), rec["agent"]["id"])
            moved = now["version"] != rec["agent"]["version"]
            done = {"at": _now(), "by": x.caller.get("username"), "reason": reason or None, "of": "the canary", "production": now, "moved": moved,
                    "evidence": (f"production is still version {now['version']}" + "".join(f", {k} → {v}" for k, v in sorted(now["endpoints"].items())
                                                                                  if not k.startswith(PREFIX.replace('-', '_')))
                                 + ": nothing to move back" if not moved else
                                 f"production moved from version {rec['agent']['version']} to {now['version']} during the canary, outside it")}
            return {"canary": _save(x, cid, status="rolled_back", rollback=done)}
        if rec["status"] != "promoted":
            raise ExperimentError(f"canary {cid} is {rec['status']}: nothing to roll back")
        _production_free(x, rec)
        _save(x, cid, status="rolling_back", rollbackJob=None, error=None, rollbackRequest={"at": _now(), "by": x.caller.get("username"),
                                                                                             "reason": reason or None})
    job = x.console.jobs.start("runtime-canary-rollback", x.workspace, {"canary": cid, "agent": rec["agent"]["name"]},
                               lambda j: _undo_promotion(x, cid, reason, j), label=f"回滚 {rec['agent']['name']} 的发布")
    return {"canary": _save(x, cid, rollbackJob=job["id"]), "job": job}


def _rolled_back(x: ex.Ctx, rec: Mapping[str, Any], done: Mapping[str, Any], *, expect: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The canary rolled back (``done``: the rollback as it went), and the rollback in the release log, once per job;
    nothing when the record is no longer as ``expect`` says."""
    entry = {"experiment": rec["id"], "kind": "runtime-canary-rollback", "workspace": x.workspace, "agent": rec["agent"]["name"], "at": done["at"],
             "by": done.get("by"), "fromVersion": rec["promotion"]["toVersion"], "toVersion": done["defaultVersion"],
             "changed": [f"rollback to version {done['restoredFrom']}"], "override": False, "metric": rec["metric"], "reason": done.get("reason"),
             "job": done.get("job"), **({"incomplete": done["incomplete"]} if done.get("incomplete") else {})}

    def change(all_: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if any(p.get("kind") == entry["kind"] and p.get("experiment") == entry["experiment"] and p.get("job") == entry["job"] for p in all_):
            return all_
        return [*all_, entry]

    saved = _save(x, rec["id"], expect=expect, status="rolled_back", rollback=dict(done), error=done.get("incomplete"))
    if saved.get("status") == "rolled_back" and (saved.get("rollback") or {}).get("job") == done.get("job"):
        x.console.store.update("promotions", [], change)
    return saved


def _rollback_cut(x: ex.Ctx, rec: Mapping[str, Any], error: str, job_id: str, *, otherwise: str | None = None,
                  expect: Mapping[str, Any] | None = None, since: Any = None) -> dict[str, Any]:
    """A rollback that did not finish: ``rolled_back`` (incomplete, recorded) if a version after the promoted one runs
    the version before's artifact and variables again — production already runs it — else ``promoted`` (with
    ``otherwise``, or: roll back again); left ``rolling_back`` while that cannot be read, up to :data:`UNDECIDED_LIMIT`
    after its job ended (``since``), then ``promoted`` as not known. ``expect``: as for :func:`_promotion_cut`."""
    promotion = rec.get("promotion") or {}
    runtime_id, back = rec["agent"]["id"], str(promotion.get("fromVersion") or "")
    try:
        ctl = x.ctl()
        before = ctl.get_agent_runtime(agentRuntimeId=runtime_id, agentRuntimeVersion=back)
        again, now = _published(ctl, runtime_id, str(promotion.get("toVersion") or ""), before)
        state = production_state(ctl, runtime_id) if again else None
    except Exception as exc:  # noqa: BLE001
        if ex._gone(exc):
            return _save(x, rec["id"], expect=expect, status="promoted", error=f"{error}; production is gone ({ex._code(exc)})")
        if since and ex._age(since) > UNDECIDED_LIMIT:
            return _save(x, rec["id"], expect=expect, status="promoted", error=(
                f"{error}; how far the rollback came could not be read for {int(UNDECIDED_LIMIT // 60)} min ({type(exc).__name__}): check "
                "production on the deploy page before rolling back again"))
        # not known yet: left rolling_back (no job holds it, so nothing waits on it) and checked again when next read
        return {**rec, "error": f"{error}; how far the rollback came could not be read ({type(exc).__name__}): checked again when the canary is "
                                "next read"}
    if not again:
        return _save(x, rec["id"], expect=expect, status="promoted", error=otherwise or f"{error}: roll back again")
    asked = rec.get("rollbackRequest") or {}
    endpoint = _record(x, runtime_id).get("endpoint") or promotion.get("endpoint")
    return _rolled_back(x, rec, {"at": _now(), "by": asked.get("by"), "reason": asked.get("reason"), "of": "the promotion", "endpoint": endpoint,
                                 "endpointVersion": (state or {}).get("endpoints", {}).get(endpoint) if endpoint else None, "pointed": None,
                                 "defaultVersion": again, "restoredFrom": back, "production": state, "job": job_id,
                                 "incomplete": error + _since(again, now)}, expect=expect)


def _undo_promotion(x: ex.Ctx, cid: str, reason: str, job: Any) -> dict[str, Any]:
    rec = _get(x, cid)
    promotion = rec["promotion"]
    back, runtime_id = str(promotion["fromVersion"]), rec["agent"]["id"]
    try:
        ctl = x.ctl()
        production = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
    except Exception as exc:
        _save(x, cid, status="promoted", error=f"rollback: {type(exc).__name__}: {str(exc)[:400]}: roll back again")
        raise
    if str(production.get("agentRuntimeVersion")) != str(promotion["toVersion"]):
        refused = ExperimentError(f"{rec['agent']['name']} is version {production.get('agentRuntimeVersion')} now, not the promoted "
                                  f"{promotion['toVersion']}: roll back from the deploy page")
        # an earlier rollback of this promotion may have published the version before again (its job cut off)
        settled = _rollback_cut(x, rec, f"rollback: an earlier rollback had published version {promotion['fromVersion']} again", job.job["id"],
                                otherwise=f"rollback: {refused}", expect={"status": "rolling_back"})
        if settled.get("status") == "rolled_back":
            job.log(f"version {settled['rollback']['defaultVersion']} already runs version {back}'s artifact and variables: rolled back")
            return settled["rollback"]
        raise refused
    try:
        endpoint = _record(x, runtime_id).get("endpoint") or promotion.get("endpoint")
        moved = None
        if endpoint:
            job.log(f"endpoint {endpoint} → version {back} (the deploy module's point_endpoint)")
            moved = deploy.point_endpoint(x.console, x.workspace, runtime_id, {"name": endpoint, "version": back}, but=cid)
            _wait_endpoint(ctl, runtime_id, endpoint, back)
        old = ctl.get_agent_runtime(agentRuntimeId=runtime_id, agentRuntimeVersion=back)
        job.log(f"DEFAULT moves only with a new version: version {back}'s own artifact and variables are published again")
        req = publish_request(rec["agent"]["name"], old["agentRuntimeArtifact"], dict(old.get("environmentVariables") or {}), endpoint="")
        out = PublishPipeline(x.session, account=x.account, region=x.region, request=req, job=job, names=NAMES, current=production,
                              record=_record(x, runtime_id), artifact=old["agentRuntimeArtifact"], sleep=_sleep, boundary=x.boundary,
                              on_runtime=lambda fields: deploy._remember(x.console.store, x.workspace, fields)).run()
        done = {"at": _now(), "by": x.caller.get("username"), "reason": reason or None, "of": "the promotion", "endpoint": endpoint,
                "endpointVersion": back if endpoint else None, "pointed": moved, "defaultVersion": str(out["version"]), "restoredFrom": back,
                "production": production_state(ctl, runtime_id), "job": job.job["id"]}
        _rolled_back(x, rec, done, expect={"status": "rolling_back"})
        return done
    except Exception as exc:  # rolled back if its new version exists
        _rollback_cut(x, rec, f"rollback: {type(exc).__name__}: {str(exc)[:400]}", job.job["id"], expect={"status": "rolling_back"})
        raise


# -- cleanup -----------------------------------------------------------------------------------------------------------------

def cleanup_canary(x: ex.Ctx, cid: str) -> dict[str, Any]:
    """Delete everything the canary created (console job ``runtime-canary-cleanup``); the record, its final results, any
    promotion and rollback stay. A cleanup that left something runs again."""
    _admin(x, "cleaning up a canary (it deletes the candidate runtime)")
    with deploy.moves(_get(x, cid)["agent"]["id"], busy=_busy_error):
        rec = _get(x, cid)  # settled (a job the console's restart cut off no longer holds it), under production's lock
        if rec["status"] in ("creating", "promoting", "rolling_back", "cleaning"):
            raise ExperimentError(f"canary {cid} is {rec['status']}: wait for its job")
        if rec.get("cleanedAt"):
            raise ExperimentError(f"canary {cid} is already cleaned up")
        before = str(rec.get("cleanupFrom") or rec["status"]) if rec["status"] == "cleanup_incomplete" else rec["status"]
        after = before if before in ("promoted", "rolled_back") else "cleaned"
        _save(x, cid, status="cleaning", cleanupFrom=before, cleanupJob=None)
    job = x.console.jobs.start("runtime-canary-cleanup", x.workspace, {"canary": cid}, lambda job: _cleanup(x, cid, job, after),
                               label=f"清理金丝雀 {rec['agent']['name']}")
    _save(x, cid, cleanupJob=job["id"])
    return job


def _tagged(ctl: Any, arn: str, cid: str) -> bool:
    tags = ctl.list_tags_for_resource(resourceArn=arn).get("tags") or {}
    return tags.get("adlc:console") == "1" and tags.get(CANARY_TAG) == cid


def production_artifacts(ctl: Any, runtime_id: str) -> tuple[set[str], set[str], bool]:
    """The image digests and code keys of every version of production (a rollback can return to any of them): what a
    canary's cleanup never deletes. The flag is False when a version could not be read: then nothing is deleted."""
    digests: set[str] = set()
    keys: set[str] = set()
    try:
        found, complete = deploy.runtime_versions(ctl, runtime_id)  # more than deploy.MAX_VERSIONS are not all read: nothing is deleted
    except Exception as exc:  # noqa: BLE001
        if ex._gone(exc):
            return digests, keys, True  # production is gone: no version of it runs anything
        return digests, keys, False
    for got in found:
        artifact = got.get("agentRuntimeArtifact") or {}
        uri = str((artifact.get("containerConfiguration") or {}).get("containerUri") or "")
        if "@" in uri:
            digests.add(uri.split("@", 1)[1])
        key = str((((artifact.get("codeConfiguration") or {}).get("code") or {}).get("s3") or {}).get("prefix") or "")
        if key:
            keys.add(key)
    return digests, keys, complete


def _cleanup(x: ex.Ctx, cid: str, job: Any, after: str) -> dict[str, Any]:
    rec = _get(x, cid)
    ctl, data, iam, logs = x.ctl(), x.data(), x.iam(), x.logs()
    done: list[dict[str, Any]] = []

    def step(label: str, fn: Callable[[], Any]) -> None:
        ex.cleanup_step(done, job, label, fn)

    ab = rec.get("abTest")
    if ab:
        def delete_ab() -> None:
            if data.get_ab_test(abTestId=ab["id"]).get("executionStatus") != "STOPPED":
                _stop(x, _get(x, cid), status="cleaning")
            data.delete_ab_test(abTestId=ab["id"])
            for _ in range(60):  # deleting is asynchronous; the online evaluations it references refuse deletion until it is gone
                try:
                    data.get_ab_test(abTestId=ab["id"])
                except Exception as exc:  # noqa: BLE001
                    if ex._gone(exc):
                        return
                    raise
                _sleep(3)

        step(f"A/B test {ab['id']}", delete_ab)
    for ev in (rec.get("onlineEvaluations") or {}).values():
        step(f"online evaluation {ev['id']}", lambda e=ev: ex._retry(lambda: ctl.delete_online_evaluation_config(onlineEvaluationConfigId=e["id"]),
                                                                    when=ex._busy, attempts=10, pause=6.0))
    gw = rec.get("gateway")
    for target in (rec.get("targets") or {}).values():
        step(f"target {target['name']}", lambda t=target: ex._retry(lambda: ctl.delete_gateway_target(gatewayIdentifier=gw["id"], targetId=t["id"]),
                                                                    when=ex._busy, attempts=10, pause=6.0))
    if gw:
        # DeleteGateway is refused until its A/B test and targets are gone (live: seconds to minutes after they are deleted).
        step(f"Gateway {gw['id']}", lambda: ex._retry(lambda: ctl.delete_gateway(gatewayIdentifier=gw["id"]), when=ex._busy, attempts=50, pause=6.0))
    if rec.get("controlEndpoint"):
        def delete_control() -> str | None:
            got = ctl.get_agent_runtime_endpoint(agentRuntimeId=rec["agent"]["id"], endpointName=rec["controlEndpoint"])
            if not _tagged(ctl, str(got.get("agentRuntimeEndpointArn")), cid):
                return "left: not this canary's endpoint"
            ctl.delete_agent_runtime_endpoint(agentRuntimeId=rec["agent"]["id"], endpointName=rec["controlEndpoint"])
            return None

        step(f"endpoint {rec['controlEndpoint']} of {rec['agent']['name']}", delete_control)
    copy = rec.get("copy") or {}
    if copy.get("id"):
        def delete_copy() -> str | None:
            try:
                runtime = ctl.get_agent_runtime(agentRuntimeId=copy["id"])
            except Exception as exc:  # noqa: BLE001
                if not ex._gone(exc):
                    raise
                return "already gone"
            if not _tagged(ctl, str(runtime["agentRuntimeArn"]), cid):
                return f"left: {copy['name']} is not this canary's candidate"
            deploy.Teardown(x.session, account=x.account, region=x.region, runtime_id=copy["id"], name=copy["name"], runtime=runtime, job=job,
                            record={"name": copy["name"]}, names=NAMES, sleep=_sleep, boundary=x.boundary).run()
            return None

        step(f"candidate runtime {copy['name']}", delete_copy)
        promoted = bool(rec.get("promotion"))
        if copy.get("job") and promoted:
            done.append({"resource": f"candidate artifact under deployments/{rec['agent']['name']}/{copy['job']}/",
                         "result": f"kept: production's version {rec['promotion'].get('toVersion')} runs it (it goes with production)"})
        if copy.get("job") and not promoted:  # the artifact under production's own name, unless a version of production runs it
            digests, keys, complete = production_artifacts(ctl, rec["agent"]["id"])
            prefix = f"deployments/{rec['agent']['name']}/{copy['job']}/"
            bucket = deploy.console_bucket(x.account, x.region)

            def delete_sources() -> str:
                if not complete or any(k.startswith(prefix) for k in keys):
                    raise ex.NotOurs("a version of production runs it (or a version could not be read): kept")
                return f"{deploy.delete_versions(client(x.session, 's3', x.region), bucket, x.account, prefix)} object version(s) deleted"

            step(f"candidate sources s3://{bucket}/{prefix}", delete_sources)
            uri = str(((copy.get("artifact") or {}).get("containerConfiguration") or {}).get("containerUri") or "")
            repo = _image_repository(uri)
            if repo and "@" in uri:
                digest = uri.split("@", 1)[1]

                def delete_image() -> None:
                    if not (rec.get("candidate") or {}).get("built"):
                        raise ex.NotOurs("an image the canary did not build (given as the candidate): kept")
                    if not complete or digest in digests:
                        raise ex.NotOurs("a version of production runs it (or a version could not be read): kept")
                    ecr = client(x.session, "ecr", x.region)
                    details = (ecr.describe_images(repositoryName=repo, imageIds=[{"imageDigest": digest}]).get("imageDetails") or [{}])[0]
                    if "imageTags" in details and copy["job"] not in (details.get("imageTags") or []):
                        raise ex.NotOurs(f"not the image this canary's build pushed ({repo}:{copy['job']}): kept")
                    ecr.batch_delete_image(repositoryName=repo, imageIds=[{"imageDigest": digest}])

                step(f"candidate image {uri.rsplit('/', 1)[-1]}", delete_image)
    role = rec.get("role")
    if role:
        def delete_role() -> None:
            from .agents import ensure_boundary, refusal

            tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=role["name"]).get("Tags") or []}
            if tags.get("adlc:console") != "1" or tags.get(CANARY_TAG) != cid:
                raise ex.NotOurs(f"role {role['name']} is not this canary's: left as it is")
            found = iam.get_role(RoleName=role["name"])["Role"]
            why = refusal(role["name"], found, tags, x.boundary)  # off the console's path: never deleted from here
            if why:
                raise ex.NotOurs(why)
            if x.boundary:  # a role made before the workspace had its boundary: given it first (a spoke role deletes policies only so)
                ensure_boundary(iam, role["name"], x.boundary, found, tags)
            for policy in iam.list_role_policies(RoleName=role["name"]).get("PolicyNames") or []:
                iam.delete_role_policy(RoleName=role["name"], PolicyName=policy)
            iam.delete_role(RoleName=role["name"])

        step(f"role {role['name']}", delete_role)
    groups = [f"/aws/bedrock-agentcore/evaluations/results/{ev['id']}" for ev in (rec.get("onlineEvaluations") or {}).values()]
    if rec.get("controlEndpoint"):
        groups.append(f"/aws/bedrock-agentcore/runtimes/{rec['agent']['id']}-{rec['controlEndpoint']}")
    if copy.get("id"):
        groups += [f"/aws/bedrock-agentcore/runtimes/{copy['id']}-DEFAULT"]
        if rec.get("treatmentEndpoint"):
            groups.append(f"/aws/bedrock-agentcore/runtimes/{copy['id']}-{rec['treatmentEndpoint']}")
    for group in dict.fromkeys(groups):  # never production's DEFAULT or its own endpoints' groups
        step(f"log group {group}", lambda g=group: logs.delete_log_group(logGroupName=g))
    return ex.finish_cleanup(x, COLLECTION, cid, done, after)


# -- routes ----------------------------------------------------------------------------------------------------------------

def register(router: Any) -> None:
    def ctx(r: Any) -> ex.Ctx:
        wid = r.workspace()
        session = r.session()
        ws = r.console.workspaces.get(wid)
        return ex.Ctx(r.console, wid, session, ws["region"], ws["accountId"], r.caller, boundary=ws.get("permissionsBoundaryArn"))

    def handle(fn: Callable[[ex.Ctx, Any], Any], status: int = 200) -> Callable[[Any], tuple[int, Any]]:
        def route(r: Any) -> tuple[int, Any]:
            try:
                return status, fn(ctx(r), r)
            except ExperimentError as exc:
                return exc.status, {"error": str(exc), **exc.extra}
            except Exception as exc:  # noqa: BLE001 - an AWS refusal is the caller's to read, not an internal error
                code = ex._client_status(exc) if hasattr(exc, "response") else None
                if code is None:
                    raise
                return code, {"error": f"{ex._code(exc)}: {str(exc)[:400]}"}

        return route

    add, base = router.add, "/workspaces/{wid}/experiments/runtime-canaries"
    add("GET", base, handle(lambda x, r: {"canaries": list_canaries(x)}))
    add("POST", base, handle(lambda x, r: start_canary(x, r.body), 202), admin=True)
    add("GET", base + "/{cid}", handle(lambda x, r: canary_detail(x, r.params["cid"])))
    add("GET", base + "/{cid}/observed", handle(lambda x, r: observed_split(x, r.params["cid"], hours=ex._int(r.query.get("hours"), 24, "hours", 1, 168))))
    add("POST", base + "/{cid}/split", handle(lambda x, r: set_split(x, r.params["cid"], r.body)), admin=True)
    add("POST", base + "/{cid}/state", handle(lambda x, r: set_state(x, r.params["cid"], r.body)))
    add("POST", base + "/{cid}/traffic", handle(lambda x, r: send_traffic(x, r.params["cid"], r.body), 202))
    add("POST", base + "/{cid}/verifications", handle(lambda x, r: verify_candidate(x, r.params["cid"], r.body), 202))
    add("POST", base + "/{cid}/promote", handle(lambda x, r: promote(x, r.params["cid"], r.body), 202), admin=True)
    add("POST", base + "/{cid}/rollback", handle(lambda x, r: rollback(x, r.params["cid"], r.body)), admin=True)
    add("DELETE", base + "/{cid}", handle(lambda x, r: cleanup_canary(x, r.params["cid"]), 202), admin=True)
