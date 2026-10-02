"""The console's experiments and release: configuration bundles, A/B tests with a canary ramp, and a promotion that
evidence opens, not a click.

How AgentCore does it, live 2026-10-01 (us-west-2, botocore 1.43.90):

* A **configuration bundle** is a versioned document ``{component ARN: {"configuration": <free JSON>}}``; every
  UpdateConfigurationBundle is a new version (a UUID, its parents in ``lineageMetadata``; the whole component map is
  replaced; ListConfigurationBundleVersions orders by version id, not by time). The console writes
  ``{"systemPrompt", "modelId"}`` under the Harness ARN (the agentcore CLI's runtimes read ``systemPrompt``).
* A managed **Harness never reads a bundle**: InvokeHarness with the bundle as W3C baggage
  (``aws.agentcore.configbundle_arn`` / ``_version``, what the agentcore CLI and a Gateway send) answered with the
  Harness's own prompt in every configuration shape tried. Bundles are read by agent code
  (``BedrockAgentCoreContext.get_config_bundle()``), which a Harness does not run.
* An **A/B test splits traffic at a Gateway**. CreateABTest installs a system-managed gateway rule on its
  ``gatewayFilter`` path (``routeToTarget.weightedRoute`` between two targets, or
  ``configurationBundle.weightedOverride``); a session sticks to its variant (12 sessions at 50/50 went 3/9, 16 at 75/25
  went 15/1); only traffic sent through the Gateway is split. Weights change only while PAUSED or NOT_STARTED (pausing
  removes the rule: every session goes to the control) and must sum to 100. The results (``results.evaluatorMetrics``:
  each arm's mean and sample size, the change, its p-value and confidence interval) come from one online evaluation per
  variant, about 15 minutes after the sessions; a trace-level evaluator counts every turn.
* A Harness is reachable from a Gateway only through an http **passthrough target** to its InvokeHarness URL (static
  query parameters ``harnessArn`` and ``qualifier``, SigV4 by the gateway's role as ``bedrock-agentcore``, which needs
  ``bedrock-agentcore:InvokeAgentRuntime`` on the Harness ARN); the client POSTs an InvokeHarness body to
  ``<gateway>/<target>/invoke``. An ``agentcoreRuntime`` target cannot work: a Harness's runtime "is managed by a harness
  and cannot be invoked directly".
* UpdateHarness mints an immutable version and moves DEFAULT to it at once ("Cannot update DEFAULT endpoint directly"),
  so a treatment minted on the agent itself would serve its production before any evidence. The **treatment** therefore
  runs as a copy of the agent (its role, tools, skills, limits and memory, the treatment's prompt / model), and the
  **control** on a named endpoint of the agent pinned to the version compared, so each arm's online evaluation sees only
  the experiment's sessions (a named endpoint has its own log group and ``service.name``) and production is untouched.

An experiment is that: the bundle's control and treatment versions, the treatment Harness, the control endpoint, a
Gateway with two passthrough targets, one online evaluation per variant, a target-based A/B test, and traffic sent
through the Gateway (:func:`send_traffic` replays a contract set; any SigV4 client can POST to its ``invokeUrl``). The
canary ramp moves the treatment's share 5 → 25 → 50 %; 100 % is the promotion. Changing the split is an admin's, and
a share above 50 % (:data:`MAX_UNPROVEN`), at the start or later, needs the gate to hold first: past half the
sessions the treatment is production in all but name.

**Promotion is gated on evidence** (:func:`gate`): (a) a console verification of the treatment (the bundle's prompt /
model on the agent, per call; or the treatment Harness as it is) exists and is robust; (b) the A/B result on the
chosen evaluator shows the treatment not worse than the control beyond that evaluator's noise band (the verification
panel's band; the minimum band, the strictest, when the panel did not measure it), with enough samples in each arm;
(c) nothing the A/B test compared moved: the agent is still the version compared, the control endpoint still serves
it, and the treatment Harness is still the version set up (pinned then: a verification of the treatment Harness
counts only if it ran on that version, so a changed copy is never credited). The gate decides on the experiment's own
metric, declared when it started; deciding on another is an override. Otherwise :func:`promote` refuses, naming the
failed condition; an admin may override with ``acknowledged: true`` and a reason, and the override, the failed
conditions and the evidence of that moment are recorded. Promoting stops the A/B test (its results kept) and applies
the treatment with UpdateHarness (a new version, which DEFAULT then serves). ``agents.update_harness`` wraps ``systemPrompt`` /
``model`` in ``optionalValue``, which botocore 1.43.90 refuses (ParamValidationError), so the update is sent here in
the shape the API takes.

Everything the console creates is tagged ``adlc:console=1`` (the experiment's resources also ``adlc:experiment``) and
named ``adlc-console-exp-<id>`` (``_`` where a name takes no ``-``); its role is on the console's IAM path
(``/adlc-console/``, the only roles a spoke role creates and passes) with the workspace's permissions boundary, and is
given to the Gateway, the online evaluations and the A/B test by its own ARN. Cleanup deletes only those, and only
bundles so tagged can be deleted or given a new version here (another team's bundle: ``acknowledged``). A cleanup
that leaves something (an AWS refusal, not a resource that is not the experiment's) is not ``cleaned``: the record
says what is left (``cleanup_incomplete``, or ``promoted`` still) and cleanup runs again. A record a job held when the
console stopped (``creating``, ``cleaning``: the job is ``interrupted`` then) is released when next read: a setup is
``failed`` (cleanup removes what it made), a cleanup incomplete.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping, Sequence

from ..direct import verify
from ..direct.aws import client
from ..direct.online import role_documents as evaluation_role_documents
from ..direct.panel import DEFAULT_PANEL, MIN_BAND
from .agents import CONSOLE_TAG

#: The canary ramp: the treatment's share of the Gateway's sessions, step by step; 100 % is the promotion itself.
CANARY_STEPS = (5, 25, 50)
#: The largest share a treatment gets before the gate holds (at the start too): above it, it serves most sessions.
MAX_UNPROVEN = 50
#: Scored sessions (turns, for a trace-level evaluator) each arm needs before its mean is evidence.
MIN_SAMPLES = 3
DEFAULT_EVALUATORS = ("Builtin.Correctness", "Builtin.Helpfulness")
MAX_EVALUATORS = 5
#: The better arm scores lower on these (Launchpad's agentcore_eval.LOWER_IS_BETTER_EVALUATORS, less Refusal).
LOWER_IS_BETTER = frozenset({"Builtin.Harmfulness", "Builtin.Stereotyping", "ThirdParty.DeepEval.Bias", "ThirdParty.DeepEval.Toxicity"})
#: Says what happened (1 = it refused), not how good it was: never the metric a promotion is decided on.
RATE_ONLY = frozenset({"Builtin.Refusal"})
HARNESS_INVOKE = "https://bedrock-agentcore.{region}.amazonaws.com/harnesses"
EXPERIMENT_TAG = "adlc:experiment"
#: Every experiment resource is named ``<PREFIX>-<id>`` (``_`` where a name takes no ``-``); its role is on the console's
#: IAM path (``agents.ROLE_PATH``), where a spoke role lets the console make roles.
PREFIX = "adlc-console-exp"
SESSION_TIMEOUT_MINUTES = 5
TRAFFIC_WORKERS = 4
MAX_TRAFFIC = 200
MAX_VERSIONS = 20
ACTIVE = ("creating", "running", "paused")
#: A status a job holds a record in, and the record's field naming that job: a record whose job is no longer running
#: (``interrupted`` by a restart) is released when next read (:func:`_settle`).
HELD = {"creating": "job", "cleaning": "cleanupJob"}
#: How long a record may say a job holds it before it names that job (the moment between saving it and starting the job).
HOLD_GRACE = 120.0
EID = re.compile(r"^exp-[0-9a-f]{8}$")
BUNDLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
EVALUATOR = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,127}$")
MODEL = re.compile(r"^[a-z0-9][a-z0-9.:_\-/]{2,199}$")
GONE = ("ResourceNotFoundException", "NotFoundException", "NoSuchEntity")

_sleep: Callable[[float], None] = time.sleep  # the tests make every wait instant


class ExperimentError(ValueError):
    """Refused, with the HTTP status the route answers and any structured detail (the gate, for a promotion)."""

    def __init__(self, message: str, *, status: int = 409, **extra: Any):
        super().__init__(message)
        self.status, self.extra = status, extra


class NotOurs(ExperimentError):
    """A resource a record names that is not this experiment's or canary's: cleanup leaves it, by design."""


class Ctx:
    """What every function works in: the console (store, jobs), the verified workspace's session, the caller, and the
    workspace's permissions boundary (every role made here carries it)."""

    def __init__(self, console: Any, workspace: str, session: Any, region: str, account: str, caller: Mapping[str, Any] | None = None,
                 boundary: str | None = None):
        self.console, self.workspace, self.session, self.region, self.account = console, workspace, session, region, account
        self.caller = dict(caller or {"username": "local", "role": "admin", "workspaces": ["*"]})
        self.boundary = boundary

    def ctl(self) -> Any:
        return client(self.session, "bedrock-agentcore-control", self.region)

    def data(self) -> Any:
        return client(self.session, "bedrock-agentcore", self.region)

    def iam(self) -> Any:
        return client(self.session, "iam")

    def logs(self) -> Any:
        return client(self.session, "logs", self.region)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(stamp: Any) -> float:
    """Seconds since a record's ``_now()`` stamp (very old when it has none)."""
    try:
        return (datetime.now(timezone.utc) - datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds()
    except ValueError:
        return float("inf")


def _holds(console: Any, rec: Mapping[str, Any], field: str) -> bool:
    """Whether the job a record's ``field`` names is still running (a record that names none yet: whether it was saved
    a moment ago, its job about to start)."""
    jid = rec.get(field)
    if not jid:
        return _age(rec.get("updatedAt") or rec.get("createdAt")) < HOLD_GRACE
    try:
        return console.jobs.get(str(jid)).get("status") == "running"
    except (KeyError, OSError, ValueError):
        return False


def _token() -> str:
    return str(uuid.uuid4())


def _code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    code = str(((response or {}).get("Error") or {}).get("Code") or "") if isinstance(response, dict) else ""
    return code or type(exc).__name__


def _gone(exc: BaseException) -> bool:
    return any(g in _code(exc) or g in str(exc) for g in GONE)


def _pages(call: Callable[..., Any], key: str, **kwargs: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while True:
        page = call(**kwargs)
        out += page.get(key) or []
        if not page.get("nextToken"):
            return out
        kwargs["nextToken"] = page["nextToken"]


def _wait(probe: Callable[[], str], ready: Sequence[str], *, what: str, attempts: int = 90, pause: float = 4.0) -> str:
    status = "UNKNOWN"
    for _ in range(attempts):
        status = probe()
        if status in ready:
            return status
        if "FAIL" in status:
            raise ExperimentError(f"{what} is {status}")
        _sleep(pause)
    raise ExperimentError(f"{what} is still {status} after {int(attempts * pause)} s")


def _fresh_role(exc: BaseException) -> bool:
    """A role created a moment ago is refused for a few seconds (AccessDenied, or a validation error about the role)."""
    text = str(exc).lower()
    return "AccessDenied" in _code(exc) or ("Validation" in _code(exc) and ("role" in text or "assume" in text))


def _busy(exc: BaseException) -> bool:
    """A resource still referenced by another one being deleted refuses its own deletion for a while."""
    return any(c in _code(exc) for c in ("ConflictException", "ValidationException"))


def _retry(call: Callable[[], Any], *, when: Callable[[BaseException], bool] = _fresh_role, attempts: int = 6, pause: float = 10.0) -> Any:
    for attempt in range(attempts):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001
            if attempt == attempts - 1 or not when(exc):
                raise
            _sleep(pause)
    return None


# -- configurations ----------------------------------------------------------------------------------------------------

def prompt_of(harness: Mapping[str, Any]) -> str:
    return "\n\n".join(str(b.get("text") or "") for b in harness.get("systemPrompt") or [] if isinstance(b, dict) and b.get("text"))


def model_of(harness: Mapping[str, Any]) -> str | None:
    return ((harness.get("model") or {}).get("bedrockModelConfig") or {}).get("modelId")


def configuration(prompt: str, model: str | None) -> dict[str, Any]:
    """What a bundle version holds for a Harness (the agentcore CLI's ``systemPrompt``, and the model id)."""
    out: dict[str, Any] = {"systemPrompt": prompt}
    if model:
        out["modelId"] = model
    return out


def read_configuration(conf: Mapping[str, Any]) -> dict[str, Any]:
    """``{"systemPrompt", "model"}`` from a bundle configuration (also the CLI's and Launchpad's spellings)."""
    prompt = conf.get("systemPrompt", conf.get("system_prompt"))
    if isinstance(prompt, list):
        prompt = "\n\n".join(str(b.get("text") or "") for b in prompt if isinstance(b, dict) and b.get("text"))
    model = conf.get("modelId") or conf.get("model")
    if isinstance(model, dict):
        model = (model.get("bedrockModelConfig") or {}).get("modelId")
    return {"systemPrompt": None if prompt is None else str(prompt), "model": str(model) if model else None}


def fingerprint(agent_id: str, prompt: str, model: str | None) -> str:
    """Which treatment a verification or an experiment is about: this prompt and model on this agent."""
    doc = json.dumps({"agent": agent_id, "systemPrompt": prompt, "model": model}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(doc.encode("utf-8")).hexdigest()[:16]


def _harness(ctl: Any, ident: str) -> dict[str, Any]:
    if not ident:
        raise ExperimentError("agentId: the Harness's id", status=400)
    return ctl.get_harness(harnessId=ident)["harness"]


def _changed(current: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any] | None:
    """The treatment configuration a form asks for (``systemPrompt`` / ``model``), or ``None`` when nothing changes."""
    prompt = str(current.get("systemPrompt") or "")
    if body.get("systemPrompt") not in (None, ""):
        prompt = str(body["systemPrompt"]).strip()
        if not prompt or len(prompt) > 100_000:
            raise ExperimentError("systemPrompt: 1-100 000 characters", status=400)
    model = current.get("modelId") or current.get("model")
    if body.get("model") not in (None, ""):
        model = str(body["model"]).strip()
        if not MODEL.match(model):
            raise ExperimentError("model must be a Bedrock model or inference profile id", status=400)
    changed = configuration(prompt, model)
    return None if changed == configuration(str(current.get("systemPrompt") or ""), current.get("modelId") or current.get("model")) else changed


def describe_change(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    parts = []
    if before.get("systemPrompt") != after.get("systemPrompt"):
        parts.append(f"system prompt changed ({len(str(before.get('systemPrompt') or ''))} → {len(str(after.get('systemPrompt') or ''))} characters)")
    if (before.get("modelId") or before.get("model")) != (after.get("modelId") or after.get("model")):
        parts.append(f"model {before.get('modelId') or before.get('model')} → {after.get('modelId') or after.get('model')}")
    return "treatment: " + ("; ".join(parts) or "unchanged")


# -- configuration bundles -----------------------------------------------------------------------------------------------

def list_bundles(x: Ctx) -> list[dict[str, Any]]:
    """Every bundle in the account and region; the console's own carry the agent they were made from."""
    mine = x.console.store.read("bundles", {})
    out = []
    for b in _pages(x.ctl().list_configuration_bundles, "bundles"):
        rec = mine.get(b.get("bundleId")) or {}
        out.append({"id": b.get("bundleId"), "arn": b.get("bundleArn"), "name": b.get("bundleName"), "description": b.get("description"),
                    "createdAt": str(b.get("createdAt") or ""), "console": bool(rec), "agent": rec.get("agent"),
                    "controlVersion": rec.get("controlVersion")})
    return sorted(out, key=lambda b: b["createdAt"], reverse=True)


def create_bundle(x: Ctx, body: Mapping[str, Any]) -> dict[str, Any]:
    """A bundle from an agent's current configuration (its first version: the control) and, when the form changes the
    prompt or the model, a second version on top of it (the treatment)."""
    ctl = x.ctl()
    agent = _harness(ctl, str(body.get("agentId") or ""))
    name = str(body.get("name") or f"{agent['harnessName'][:80]}_{secrets.token_hex(3)}")
    if not BUNDLE_NAME.match(name):
        raise ExperimentError("name: a letter, then letters, digits or underscores (at most 100)", status=400)
    current = configuration(prompt_of(agent), model_of(agent))
    changed = _changed(current, body)
    arn = agent["arn"]
    created = ctl.create_configuration_bundle(
        bundleName=name, description=f"{agent['harnessName']}: control and treatment configurations (ADLC console)"[:500],
        components={arn: {"configuration": current}}, commitMessage=f"control: {agent['harnessName']} version {agent.get('harnessVersion')} as deployed"[:500],
        tags=dict(CONSOLE_TAG), clientToken=_token())
    out = {"id": created["bundleId"], "arn": created["bundleArn"], "name": name, "controlVersion": created["versionId"], "treatmentVersion": None}
    if changed:
        updated = ctl.update_configuration_bundle(bundleId=created["bundleId"], components={arn: {"configuration": changed}},
                                                  parentVersionIds=[created["versionId"]], clientToken=_token(),
                                                  commitMessage=str(body.get("commitMessage") or describe_change(current, changed))[:500])
        out["treatmentVersion"] = updated["versionId"]
    record = {"workspace": x.workspace, "name": name, "agent": {"id": agent["harnessId"], "name": agent["harnessName"], "arn": arn},
              "controlVersion": created["versionId"], "createdAt": _now(), "createdBy": x.caller.get("username")}
    x.console.store.update("bundles", {}, lambda all_: {**all_, created["bundleId"]: record})
    return out


def _version(ctl: Any, bundle_id: str, version_id: str) -> dict[str, Any]:
    return ctl.get_configuration_bundle_version(bundleId=bundle_id, versionId=version_id)


def bundle_detail(x: Ctx, bundle_id: str) -> dict[str, Any]:
    """The bundle and its versions, oldest first, each with what it sets for every component."""
    ctl = x.ctl()
    got = ctl.get_configuration_bundle(bundleId=bundle_id)
    listed = _pages(lambda **kw: ctl.list_configuration_bundle_versions(bundleId=bundle_id, **kw), "versions")
    listed.sort(key=lambda v: str(v.get("versionCreatedAt") or ""))
    versions = []
    for v in listed[-MAX_VERSIONS:]:
        full = _version(ctl, bundle_id, v["versionId"])
        lineage = full.get("lineageMetadata") or v.get("lineageMetadata") or {}
        versions.append({"versionId": v["versionId"], "createdAt": str(full.get("versionCreatedAt") or v.get("versionCreatedAt") or ""),
                         "commitMessage": lineage.get("commitMessage"), "parents": lineage.get("parentVersionIds") or [],
                         "branch": lineage.get("branchName"), "latest": v["versionId"] == got.get("versionId"),
                         "components": {arn: read_configuration((c or {}).get("configuration") or {}) for arn, c in (full.get("components") or {}).items()}})
    rec = x.console.store.read("bundles", {}).get(bundle_id) or {}
    return {"id": got["bundleId"], "arn": got["bundleArn"], "name": got.get("bundleName"), "description": got.get("description"),
            "latestVersion": got.get("versionId"), "console": bool(rec), "agent": rec.get("agent"), "controlVersion": rec.get("controlVersion"),
            "versions": versions, "truncated": len(listed) > MAX_VERSIONS}


def add_version(x: Ctx, bundle_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """A new version on top of the latest: the given component (or the only one) with a changed prompt / model. Only a
    bundle the console created (tag ``adlc:console=1``), or another team's with ``acknowledged: true``: agent code that
    reads its bundle's latest version would run the new one."""
    ctl = x.ctl()
    got = ctl.get_configuration_bundle(bundleId=bundle_id)
    tags = ctl.list_tags_for_resource(resourceArn=got["bundleArn"]).get("tags") or {}
    if tags.get("adlc:console") != "1" and body.get("acknowledged") is not True:
        raise ExperimentError(f"{got.get('bundleName') or bundle_id} was not created from this console (agent code reading it would run the new "
                              "version): send acknowledged: true to add a version to it", status=403)
    components = dict(got.get("components") or {})
    arn = str(body.get("componentArn") or (next(iter(components)) if len(components) == 1 else ""))
    if arn not in components:
        raise ExperimentError("componentArn: which of the bundle's components to change", status=400)
    base = read_configuration(components[arn].get("configuration") or {})
    before = configuration(str(base["systemPrompt"] or ""), base["model"])
    changed = _changed(before, body)
    if not changed:
        raise ExperimentError("nothing changed: give a different systemPrompt or model", status=400)
    updated = ctl.update_configuration_bundle(bundleId=bundle_id, components={**components, arn: {"configuration": {**components[arn].get("configuration", {}), **changed}}},
                                              parentVersionIds=[got["versionId"]], clientToken=_token(),
                                              commitMessage=str(body.get("commitMessage") or describe_change(before, changed))[:500])
    return {"id": bundle_id, "versionId": updated["versionId"], "parent": got["versionId"]}


def delete_bundle(x: Ctx, bundle_id: str) -> dict[str, Any]:
    ctl = x.ctl()
    got = ctl.get_configuration_bundle(bundleId=bundle_id)
    tags = ctl.list_tags_for_resource(resourceArn=got["bundleArn"]).get("tags") or {}
    if tags.get("adlc:console") != "1":
        raise ExperimentError("only bundles created from this console can be deleted here", status=403)
    using = [e["id"] for e in _experiments(x) if e.get("status") in ACTIVE and (e.get("bundle") or {}).get("id") == bundle_id]
    if using:
        raise ExperimentError(f"experiment {', '.join(using)} uses this bundle: clean it up first")
    ctl.delete_configuration_bundle(bundleId=bundle_id)
    x.console.store.update("bundles", {}, lambda all_: {k: v for k, v in all_.items() if k != bundle_id})
    return {"deleted": bundle_id}


def _treatment_from(ctl: Any, agent: Mapping[str, Any], bundle_id: str, version_id: str) -> dict[str, Any]:
    """A bundle version's configuration for this agent, the model defaulting to the agent's."""
    if not bundle_id or not version_id:
        raise ExperimentError("bundleId and versionId: the bundle version to use", status=400)
    comp = (_version(ctl, bundle_id, version_id).get("components") or {}).get(agent["arn"])
    if not comp:
        raise ExperimentError(f"bundle {bundle_id} version {version_id} has no configuration for {agent['harnessName']}")
    conf = read_configuration(comp.get("configuration") or {})
    if not conf["systemPrompt"]:
        raise ExperimentError(f"bundle {bundle_id} version {version_id} sets no systemPrompt")
    return {"systemPrompt": conf["systemPrompt"], "model": conf["model"] or model_of(agent), "bundleId": bundle_id, "versionId": version_id}


# -- verification of a treatment -------------------------------------------------------------------------------------

def verify_treatment(x: Ctx, body: Mapping[str, Any]) -> dict[str, Any]:
    """A console verification (job kind ``verify``) of a treatment: the bundle version's prompt / model on the agent,
    per call (the agent is never changed), N rounds of a contract set, with the evaluator panel for its noise bands."""
    from .evaluation import EvaluationError, contract_set

    try:
        cs = contract_set(x.console.store, x.workspace, str(body.get("contractSet") or ""))
    except EvaluationError as exc:
        raise ExperimentError(str(exc), status=404) from exc
    ctl = x.ctl()
    eid = str(body.get("experiment") or "")
    if eid:
        rec = _get(x, eid)
        agent = _harness(ctl, rec["agent"]["id"])
        treatment = {**rec["treatment"], "bundleId": rec["bundle"]["id"], "versionId": rec["bundle"]["treatment"]}
    else:
        agent = _harness(ctl, str(body.get("agentId") or ""))
        treatment = _treatment_from(ctl, agent, str(body.get("bundleId") or ""), str(body.get("versionId") or ""))
    prompt = treatment["systemPrompt"] if treatment["systemPrompt"] != prompt_of(agent) else None
    model = treatment["model"] if treatment["model"] and treatment["model"] != model_of(agent) else None
    if prompt is None and model is None:
        raise ExperimentError("this treatment is the agent's current configuration: verify the agent itself on the evaluation page", status=400)
    repeat = _int(body.get("repeat"), 3, "repeat (rounds)", 1, 5)
    panel = DEFAULT_PANEL if body.get("panel", True) else ()
    info = verify.harness_info(ctl, agent["harnessId"])
    marker = {"fingerprint": fingerprint(agent["harnessId"], treatment["systemPrompt"], treatment["model"]), "bundleId": treatment.get("bundleId"),
              "versionId": treatment.get("versionId"), "model": treatment["model"], "agentVersion": str(agent.get("harnessVersion")),
              "experiment": eid or None}
    out_dir = x.console.data_dir / "console" / "verifications"
    session, region = x.session, x.region

    def work(job: Any) -> dict[str, Any]:
        run = verify.Verify(session, info, cs["contracts"], cs["l1"], region=region, out=out_dir / job.job["id"], repeat=repeat, panel=panel,
                            prompt=prompt, model=model, log=job.log)
        doc = run.run()
        job.progress(robust=doc["robust"], holding=doc["holding"], contracts=len(doc["contracts"]))
        return {**doc, "markdown": verify.render_markdown(doc), "contractSet": cs["id"], "treatment": marker}

    return x.console.jobs.start("verify", x.workspace, {"agent": info["name"], "agentId": info["id"], "contractSet": cs["id"], "repeat": repeat,
                                                         "panel": bool(panel), "treatment": marker}, work, label=f"验证 {info['name']} 的候选配置")


def _pinned(x: Ctx, rec: Mapping[str, Any]) -> Mapping[str, Any]:
    """A record set up before the treatment Harness's version was pinned (``treatmentHarness.version``), pinned to the
    copy's first version: the setup made the copy (CreateHarness, its first version) and the console never changes
    it, so that is the version set up; a later one is a change made outside, which the gate's condition (c) shows."""
    twin = rec.get("treatmentHarness") or {}
    if not twin.get("id") or twin.get("version"):
        return rec
    try:
        listed = _pages(lambda **kw: x.ctl().list_harness_versions(harnessId=twin["id"], **kw), "harnessVersions")
    except Exception as exc:  # noqa: BLE001
        if _gone(exc):
            return rec
        raise
    first = min((int(v["harnessVersion"]) for v in listed if str(v.get("harnessVersion") or "").isdigit()), default=None)
    return rec if first is None else _save(x, rec["id"], treatmentHarness={**twin, "version": str(first)})


def find_verification(x: Ctx, rec: Mapping[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    """The newest finished console verification of this experiment's treatment, and whether one is still running.

    A verification of the treatment Harness itself counts only if it ran on the version set up (pinned in
    ``treatmentHarness.version``; a changed copy is not the treatment), and a canary's verification only if it ran on
    the candidate's pinned version (``copy.version``, recorded on the job as ``candidateVersion``). A canary's job from
    before that was recorded ran on the candidate's DEFAULT, which moves only to a new version: while the gate's
    condition (c) finds the candidate still on the pinned version, it was on it then too."""
    fp, agent_id = rec["treatment"]["fingerprint"], rec["agent"]["id"]
    twin_rec = rec.get("treatmentHarness") or {}
    twin, pinned = twin_rec.get("id"), str(twin_rec.get("version") or "")
    candidate = str((rec.get("copy") or {}).get("version") or "") if (rec.get("copy") or {}).get("id") else ""
    running = False
    for job in x.console.jobs.list(workspace=x.workspace, kind="verify", limit=200):
        params = job.get("params") or {}
        marker = params.get("treatment") or {}
        of_treatment = marker.get("fingerprint") == fp and params.get("agentId") == agent_id
        of_twin = bool(twin) and params.get("agentId") == twin and not params.get("treatment")
        if not (of_treatment or of_twin):
            continue
        if of_twin and (not pinned or str(params.get("agentVersion") or "") != pinned):
            continue  # the treatment Harness changed since the setup (or the job does not say which version it ran on)
        if of_treatment and marker.get("canary") and candidate and str(marker.get("candidateVersion") or candidate) != candidate:
            continue  # the candidate runtime is another version than the one the canary measures
        if job.get("status") == "running":
            running = True
            continue
        if job.get("status") != "succeeded":
            continue
        doc = (x.console.jobs.get(job["id"]) or {}).get("result") or {}
        if of_twin and (doc.get("promptOverride") or doc.get("modelOverride")):
            continue  # the treatment Harness run with another prompt or model is not the treatment
        panel = doc.get("panel") or {}
        return {"id": job["id"], "finishedAt": job.get("finishedAt"), "robust": bool(doc.get("robust")), "holding": doc.get("holding"),
                "contracts": len(doc.get("contracts") or []), "repeat": doc.get("repeat"), "panel": bool(panel), "bands": dict(panel.get("bands") or {}),
                "agentVersion": marker.get("agentVersion") or (rec["agent"]["version"] if of_twin else None),
                "treatmentVersion": pinned if of_twin else (marker.get("candidateVersion") or (candidate if marker.get("canary") else None) or None),
                "of": "treatment Harness" if of_twin else "the treatment on the agent"}, running
    return None, running


# -- the evidence gate -------------------------------------------------------------------------------------------------

def polarity(evaluator: str) -> int:
    return -1 if evaluator.rsplit("/", 1)[-1] in LOWER_IS_BETTER else 1


def ab_metrics(ab: Mapping[str, Any]) -> list[dict[str, Any]]:
    """GetABTest's ``results`` per evaluator: each arm's mean and sample size, AgentCore's change and statistics."""
    out = []
    for m in (ab.get("results") or {}).get("evaluatorMetrics") or []:
        evaluator = str(m.get("evaluatorArn") or "").rsplit("/", 1)[-1]
        control = m.get("controlStats") or {}
        variants = m.get("variantResults") or []
        t1 = next((v for v in variants if v.get("variantName") == "T1"), variants[0] if variants else {})
        ci = t1.get("confidenceInterval") or {}
        out.append({"evaluator": evaluator, "polarity": polarity(evaluator),
                    "control": {"n": int(control.get("sampleSize") or 0), "mean": control.get("mean")},
                    "treatment": {"n": int(t1.get("sampleSize") or 0), "mean": t1.get("mean"), "change": t1.get("absoluteChange"),
                                  "percent": t1.get("percentChange"), "pValue": t1.get("pValue"), "significant": t1.get("isSignificant"),
                                  "ci": [ci.get("lower"), ci.get("upper")] if ci else None}})
    return out


def band_for(verification: Mapping[str, Any] | None, metric: str) -> tuple[float, str]:
    bands = (verification or {}).get("bands") or {}
    if metric in bands:
        return float(bands[metric]), f"measured by verification {verification['id']}"  # type: ignore[index]
    return MIN_BAND, "the minimum band: the verification's panel did not score this evaluator"


def arm_ok(arm: Mapping[str, Any]) -> bool:
    """An arm of the A/B test (``{"what", "expected", "now", "target"}``) still serves the version it was set up on."""
    now, target = arm.get("now"), arm.get("target")
    return now is not None and str(now) == str(arm["expected"]) and target in (None, "", str(arm["expected"]))


def arm_evidence(arm: Mapping[str, Any]) -> str:
    if arm.get("now") is None:
        return f"{arm['what']} is gone: the A/B test compared version {arm['expected']}"
    if str(arm["now"]) == str(arm["expected"]):  # still live on it, a move under way
        return f"{arm['what']} is moving from version {arm['expected']}, the one the A/B test compared, to version {arm['target']}"
    moving = f" (moving to {arm['target']})" if arm.get("target") not in (None, "", arm.get("now")) else ""
    return (f"{arm['what']} serves version {arm['now']}{moving}, not version {arm['expected']}, the one the A/B test compared: "
            "the evidence is about another version")


def gate(rec: Mapping[str, Any], *, verification: Mapping[str, Any] | None, metrics: Sequence[Mapping[str, Any]], metric: str,
         agent_version: str | None, verifying: bool = False, arms: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """The promotion's conditions, each ``{"id", "ok", "evidence", ...}``; ``ok`` only when every one holds. ``arms``
    are what else the A/B test compared, as AgentCore has it now (the control endpoint, the treatment copy): condition
    (c) holds only while each still serves the version it was set up on (:func:`arm_ok`)."""
    compared = rec["agent"]["version"]
    v = verification
    if v is None:
        ok, said = False, ("a verification of this treatment is still running" if verifying else
                          "no console verification of this treatment: verify its prompt and model on the agent against a contract set")
    elif v.get("agentVersion") not in (None, compared):
        ok, said = False, f"verification {v['id']} ran on version {v['agentVersion']}; the experiment compares version {compared}: verify again"
    elif not v.get("robust"):
        ok, said = False, f"verification {v['id']}: {v.get('holding')}/{v.get('contracts')} contract(s) hold in every one of {v.get('repeat')} round(s) (not robust)"
    else:
        ok, said = True, f"verification {v['id']}: all {v.get('contracts')} contract(s) hold in every one of {v.get('repeat')} round(s)"
    conditions: list[dict[str, Any]] = [{"id": "verification", "ok": ok, "evidence": said, "verification": v}]

    band, source = band_for(v, metric)
    row = next((m for m in metrics if m.get("evaluator") == metric), None)
    ab: dict[str, Any] = {"id": "ab", "metric": metric, "band": band, "bandSource": source, "worse": False}
    if row is None:
        ab.update(ok=False, evidence=f"no A/B result for {metric} yet (AgentCore publishes it about 15 minutes after the sessions)")
    else:
        c, t = row["control"], row["treatment"]
        ab.update(control=c, treatment=t)
        if min(c["n"], t["n"]) < MIN_SAMPLES or c.get("mean") is None or t.get("mean") is None:
            ab.update(ok=False, evidence=f"too few scored sessions: control {c['n']}, treatment {t['n']} (at least {MIN_SAMPLES} in each arm)")
        else:
            delta = round(polarity(metric) * (float(t["mean"]) - float(c["mean"])), 4)
            worse = delta < -band
            stats = f"; AgentCore p={t['pValue']:.3g}" if isinstance(t.get("pValue"), (int, float)) else ""
            ab.update(ok=not worse, worse=worse, delta=delta,
                      evidence=f"{metric}: control {float(c['mean']):.3f} (n={c['n']}), treatment {float(t['mean']):.3f} (n={t['n']}), "
                               f"Δ {delta:+.3f} {'<' if worse else '≥'} −{band:g} (the noise band, {source}){stats}"
                               + (": worse beyond the noise band" if worse else ""))
    conditions.append(ab)

    same = agent_version is not None and str(agent_version) == str(compared)
    moved = [dict(a) for a in arms or () if not arm_ok(a)]
    agent: dict[str, Any] = {"id": "agent", "ok": same and not moved, "compared": compared, "now": agent_version}
    if not same:
        agent["evidence"] = f"the agent moved from version {compared} to {agent_version} during the experiment: the A/B test compared another configuration"
    elif moved:
        agent.update(arm=moved[0], evidence=arm_evidence(moved[0]))
    else:
        also = f" ({', '.join(str(a['what']) for a in arms)} too)" if arms else ""
        agent["evidence"] = f"the agent is still version {compared}, the one the A/B test compared{also}"
    conditions.append(agent)
    return {"ok": all(c["ok"] for c in conditions), "conditions": conditions, "metric": metric}


def switched_metric(rec: Mapping[str, Any], asked: Any) -> str | None:
    """Another metric than the experiment's own, asked for at promotion (validated), or None."""
    asked = str(asked or "").strip()
    return _metric(asked, rec["evaluators"]) if asked and asked != rec["metric"] else None


def metric_condition(declared: str, switched: str, other: Mapping[str, Any]) -> dict[str, Any]:
    """The gate's extra condition when a promotion decides on another metric than the declared one: never met (an
    override), with that metric's own A/B condition beside it for the record."""
    ab = next((c for c in other.get("conditions") or [] if c.get("id") == "ab"), {})
    return {"id": "metric", "ok": False, "declared": declared, "metric": switched, "ab": ab,
            "evidence": f"the experiment decides on {declared}, declared when it started; deciding on {switched} instead is an override "
                        f"({ab.get('evidence') or 'no result'})"}


# -- experiments: requests -----------------------------------------------------------------------------------------------

def names(hexid: str, agent_name: str) -> dict[str, str]:
    under = PREFIX.replace("-", "_")
    return {"harness": f"{agent_name[:27]}_x{hexid}", "endpoint": f"{under}_{hexid}", "role": f"{PREFIX}-{hexid}", "gateway": f"{PREFIX}-{hexid}",
            "C": f"{PREFIX}-{hexid}-c", "T1": f"{PREFIX}-{hexid}-t", "evalC": f"{under}_{hexid}_c", "evalT1": f"{under}_{hexid}_t",
            "abTest": f"{under}_{hexid}"}


def clone_request(agent: Mapping[str, Any], treatment: Mapping[str, Any], name: str, tags: Mapping[str, str]) -> dict[str, Any]:
    """CreateHarness for the treatment Harness: the agent's role, tools, skills, limits, environment and memory (an
    explicit memory as it is, a managed one by its ARN), with the treatment's prompt and model."""
    model = dict(agent.get("model") or {})
    if treatment.get("model") and treatment["model"] != model_of(agent):
        if "bedrockModelConfig" not in model:
            raise ExperimentError("the agent's model is not a Bedrock model configuration: a treatment can change its prompt only")
        model = {"bedrockModelConfig": {**model["bedrockModelConfig"], "modelId": treatment["model"]}}
    memory = agent.get("memory") or {}
    if memory.get("agentCoreMemoryConfiguration"):
        mem: dict[str, Any] = {"agentCoreMemoryConfiguration": dict(memory["agentCoreMemoryConfiguration"])}
    elif (memory.get("managedMemoryConfiguration") or {}).get("arn"):
        mem = {"agentCoreMemoryConfiguration": {"arn": memory["managedMemoryConfiguration"]["arn"], "actorId": "{actorId}"}}
    else:
        mem = {"disabled": {}}
    request: dict[str, Any] = {"harnessName": name, "executionRoleArn": agent["executionRoleArn"], "model": model,
                               "systemPrompt": [{"text": treatment["systemPrompt"]}], "tools": list(agent.get("tools") or []),
                               "allowedTools": list(agent.get("allowedTools") or []), "skills": list(agent.get("skills") or []), "memory": mem,
                               "environmentVariables": dict(agent.get("environmentVariables") or {}), "tags": dict(tags), "clientToken": _token()}
    runtime = (agent.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {}
    environment = {k: runtime[k] for k in ("lifecycleConfiguration", "networkConfiguration", "filesystemConfigurations") if runtime.get(k)}
    if environment:
        request["environment"] = {"agentCoreRuntimeEnvironment": environment}
    for key in ("environmentArtifact", "authorizerConfiguration", "truncation", "maxIterations", "maxTokens", "timeoutSeconds"):
        if agent.get(key) is not None:
            request[key] = agent[key]
    return request


def role_documents(account: str, region: str, harness_arns: Sequence[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The experiment's one role, assumed by the Gateway (InvokeHarness of the two arms, which IAM authorizes as
    InvokeAgentRuntime), the A/B test (its routing rule on the Gateway) and the two online evaluations."""
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                                                      "Action": "sts:AssumeRole", "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}
    _, evaluation = evaluation_role_documents(account, region)
    statements = [
        {"Sid": "InvokeArms", "Effect": "Allow", "Action": ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeHarness"],
         "Resource": [r for arn in harness_arns for r in (arn, f"{arn}/*")]},
        {"Sid": "ABTestRouting", "Effect": "Allow", "Resource": "*", "Action": [
            "bedrock-agentcore:GetGateway", "bedrock-agentcore:GetGatewayTarget", "bedrock-agentcore:ListGatewayTargets", "bedrock-agentcore:UpdateGateway",
            "bedrock-agentcore:CreateGatewayRule", "bedrock-agentcore:GetGatewayRule", "bedrock-agentcore:UpdateGatewayRule",
            "bedrock-agentcore:DeleteGatewayRule", "bedrock-agentcore:ListGatewayRules", "bedrock-agentcore:GetOnlineEvaluationConfig",
            "bedrock-agentcore:GetEvaluator", "bedrock-agentcore:GetABTest"]},
        *evaluation["Statement"],
    ]
    return trust, {"Version": "2012-10-17", "Statement": statements}


def gateway_request(name: str, role_arn: str, tags: Mapping[str, str]) -> dict[str, Any]:
    return {"name": name, "description": "ADLC console A/B test: sessions split between the control and the treatment Harness",
            "roleArn": role_arn, "authorizerType": "AWS_IAM", "tags": dict(tags), "clientToken": _token()}


def target_request(region: str, name: str, harness_arn: str, qualifier: str = "DEFAULT") -> dict[str, Any]:
    """An http passthrough target to InvokeHarness of one Harness endpoint, signed by the Gateway's role."""
    return {"name": name, "description": f"InvokeHarness {harness_arn.rsplit('/', 1)[-1]} ({qualifier})"[:200],
            "targetConfiguration": {"http": {"passthrough": {"endpoint": HARNESS_INVOKE.format(region=region), "protocolType": "CUSTOM",
                                                             "staticQueryParameters": {"harnessArn": harness_arn, "qualifier": qualifier},
                                                             "staticQueryParameterConflictResolution": "STATIC_OVERRIDE"}}},
            "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE",
                                                  "credentialProvider": {"iamCredentialProvider": {"service": "bedrock-agentcore", "region": region}}}],
            "clientToken": _token()}


def runtime_name(runtime_id: str) -> str:
    return re.sub(r"-[^-]+$", "", runtime_id)


def online_eval_request(name: str, runtime_id: str, endpoint: str, evaluators: Sequence[str], role_arn: str, tags: Mapping[str, str],
                        session_timeout: int = SESSION_TIMEOUT_MINUTES) -> dict[str, Any]:
    """One arm's online evaluation: every session of that Harness endpoint (its own log group and service name)."""
    return {"onlineEvaluationConfigName": name, "description": f"ADLC console A/B test arm: {runtime_name(runtime_id)} ({endpoint})"[:200],
            "rule": {"samplingConfig": {"samplingPercentage": 100.0}, "sessionConfig": {"sessionTimeoutMinutes": int(session_timeout)}},
            "dataSourceConfig": {"cloudWatchLogs": {"logGroupNames": [f"/aws/bedrock-agentcore/runtimes/{runtime_id}-{endpoint}"],
                                                    "serviceNames": [f"{runtime_name(runtime_id)}.{endpoint}"]}},
            "evaluators": [{"evaluatorId": e} for e in evaluators], "evaluationExecutionRoleArn": role_arn, "enableOnCreate": True,
            "tags": dict(tags), "clientToken": _token()}


def variants(targets: Mapping[str, str], treatment_weight: int) -> list[dict[str, Any]]:
    return [{"name": "C", "weight": 100 - int(treatment_weight), "variantConfiguration": {"target": {"name": targets["C"]}}},
            {"name": "T1", "weight": int(treatment_weight), "variantConfiguration": {"target": {"name": targets["T1"]}}}]


def ab_test_request(name: str, gateway_arn: str, targets: Mapping[str, str], treatment_weight: int, evals: Mapping[str, str], role_arn: str,
                    tags: Mapping[str, str]) -> dict[str, Any]:
    """A target-based A/B test on the control target's path; each arm scored by its own online evaluation."""
    return {"name": name, "description": "ADLC console: control vs treatment Harness, one online evaluation per arm", "gatewayArn": gateway_arn,
            "variants": variants(targets, treatment_weight), "gatewayFilter": {"targetPaths": [f"/{targets['C']}/*"]},
            "evaluationConfig": {"perVariantOnlineEvaluationConfig": [{"name": v, "onlineEvaluationConfigArn": evals[v]} for v in ("C", "T1")]},
            "roleArn": role_arn, "enableOnCreate": True, "tags": dict(tags), "clientToken": _token()}


# -- experiments: the record ---------------------------------------------------------------------------------------------

def _settle(x: Ctx, rec: dict[str, Any]) -> dict[str, Any]:
    """A record its job no longer holds (the console restarted while the job ran: it is ``interrupted``) released: a
    setup ``failed`` (what it made is on the record for cleanup), a cleanup incomplete (it runs again)."""
    field = HELD.get(str(rec.get("status")))
    if not field or _holds(x.console, rec, field):
        return rec
    jid = rec.get(field) or "(never started)"
    held = {"status": rec["status"], field: rec.get(field)}  # written only while the record is still so
    if rec["status"] == "creating":
        return _save(x, rec["id"], expect=held, status="failed", error=f"the setup job {jid} did not finish (the console restarted while it ran): "
                                                                      "clean up what it made")
    promoted = rec.get("cleanupFrom") == "promoted" or bool(rec.get("promotion"))
    return _save(x, rec["id"], expect=held, status="promoted" if promoted else "cleanup_incomplete", cleanedAt=None,
                 error=f"the cleanup job {jid} did not finish (the console restarted while it ran): clean up again")


def _experiments(x: Ctx) -> list[dict[str, Any]]:
    return [_settle(x, e) for e in x.console.store.read("experiments", {}).values() if e.get("workspace") == x.workspace]


def list_experiments(x: Ctx) -> list[dict[str, Any]]:
    keep = ("id", "name", "status", "agent", "weights", "metric", "createdAt", "createdBy", "promotion", "error")
    return [{k: e.get(k) for k in keep} for e in sorted(_experiments(x), key=lambda e: e["createdAt"], reverse=True)]


def _get(x: Ctx, eid: str) -> dict[str, Any]:
    found = x.console.store.read("experiments", {}).get(eid) if EID.match(eid or "") else None
    if not found or found.get("workspace") != x.workspace:
        raise ExperimentError(f"no experiment {eid}", status=404)
    return _settle(x, found)


def _save(x: Ctx, eid: str, *, expect: Mapping[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    """The record with ``fields`` saved onto it; with ``expect``, only while the stored record still has those values
    (a settle decides on what it read, and another action may have moved the record since: then nothing is written,
    and the record as it is now is returned)."""
    out: dict[str, Any] = {}

    def change(all_: dict[str, Any]) -> dict[str, Any]:
        current = all_[eid]
        if expect and any(current.get(k) != v for k, v in expect.items()):
            out.update(current)
            return all_
        out.update({**current, **fields, "updatedAt": _now()})
        return {**all_, eid: dict(out)}

    x.console.store.update("experiments", {}, change)
    return out


@contextmanager
def one_at_a_time(agent_id: str, name: str | None = None) -> Iterator[None]:
    """An agent's experiment actions (start, ramp, pause, promotion, cleanup) and its direct edits, one at a time: the
    deploy module's per-key lock (``deploy.moves``), refused with a 409 after a minute's wait. Each action reads the
    record again under it."""
    from .deploy import moves

    with moves(f"harness:{agent_id}", busy=lambda message: ExperimentError(message, status=409), what=f"the agent {name or agent_id}"):
        yield


def _int(value: Any, default: int, name: str, low: int, high: int) -> int:
    try:
        number = int(default if value in (None, "") else value)
    except (TypeError, ValueError):
        number = low - 1
    if not low <= number <= high:
        raise ExperimentError(f"{name}: {low}-{high}", status=400)
    return number


def _weight(value: Any) -> int:
    try:
        weight = int(value)
    except (TypeError, ValueError):
        raise ExperimentError("treatmentWeight: the treatment's share, 1-99 %", status=400) from None
    if not 1 <= weight <= 99:
        raise ExperimentError("treatmentWeight: 1-99 % (100 % is the promotion)", status=400)
    return weight


def _start_weight(value: Any) -> int:
    """The treatment's share at the start: at most :data:`MAX_UNPROVEN` (no gate can hold before any evidence)."""
    weight = _weight(value)
    if weight > MAX_UNPROVEN:
        raise ExperimentError(f"treatmentWeight: at most {MAX_UNPROVEN} % to start; a larger share only once the gate holds", status=400)
    return weight


def _evaluators(value: Any) -> list[str]:
    chosen = [str(e).strip() for e in (value or DEFAULT_EVALUATORS) if str(e).strip()]
    if not chosen or len(chosen) > MAX_EVALUATORS or not all(EVALUATOR.match(e) for e in chosen):
        raise ExperimentError(f"evaluators: 1-{MAX_EVALUATORS} evaluator ids", status=400)
    return list(dict.fromkeys(chosen))


def _metric(value: Any, evaluators: Sequence[str]) -> str:
    metric = str(value or evaluators[0])
    if metric not in evaluators:
        raise ExperimentError(f"metric must be one of the experiment's evaluators ({', '.join(evaluators)})", status=400)
    if metric in RATE_ONLY:
        raise ExperimentError(f"{metric} says how often the agent refused, not how well it answered: choose a quality evaluator", status=400)
    return metric


def start_experiment(x: Ctx, body: Mapping[str, Any]) -> dict[str, Any]:
    """Check the request, keep the record, and set the experiment up in a console job (kind ``experiment``)."""
    if body.get("acknowledged") is not True:
        raise ExperimentError("an experiment creates a treatment Harness, a Gateway, two online evaluations (billed for every session they score) "
                              "and an A/B test, kept until it is cleaned up: send acknowledged: true", status=400)
    ctl = x.ctl()
    agent = _harness(ctl, str(body.get("agentId") or ""))
    if agent.get("status") != "READY":
        raise ExperimentError(f"{agent['harnessName']} is {agent.get('status')}: wait until it is READY")
    bundle_id = str(body.get("bundleId") or "")
    control = _treatment_from(ctl, agent, bundle_id, str(body.get("controlVersion") or ""))
    treatment = _treatment_from(ctl, agent, bundle_id, str(body.get("treatmentVersion") or ""))
    current = {"systemPrompt": prompt_of(agent), "model": model_of(agent)}
    if (control["systemPrompt"], control["model"]) != (current["systemPrompt"], current["model"]):
        raise ExperimentError(f"{agent['harnessName']} no longer runs this bundle's control version (prompt or model changed since): "
                              "make a new bundle from its current configuration")
    if (treatment["systemPrompt"], treatment["model"]) == (current["systemPrompt"], current["model"]):
        raise ExperimentError("the treatment version is the agent's current configuration: nothing to compare", status=400)
    weight = _start_weight(body.get("treatmentWeight", CANARY_STEPS[0]))
    evaluators = _evaluators(body.get("evaluators"))
    metric = _metric(body.get("metric"), evaluators)
    timeout = _int(body.get("sessionTimeout"), SESSION_TIMEOUT_MINUTES, "sessionTimeout (minutes)", 1, 60)
    hexid = secrets.token_hex(4)
    eid = f"exp-{hexid}"
    runtime = (agent.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {}
    rec = {"id": eid, "workspace": x.workspace, "name": str(body.get("name") or f"{agent['harnessName']} A/B")[:80], "status": "creating",
           "agent": {"id": agent["harnessId"], "name": agent["harnessName"], "arn": agent["arn"], "runtimeId": runtime.get("agentRuntimeId"),
                     "version": str(agent.get("harnessVersion"))},
           "bundle": {"id": bundle_id, "control": control["versionId"], "treatment": treatment["versionId"]},
           "control": {"systemPrompt": current["systemPrompt"], "model": current["model"]},
           "treatment": {"systemPrompt": treatment["systemPrompt"], "model": treatment["model"],
                         "fingerprint": fingerprint(agent["harnessId"], treatment["systemPrompt"], treatment["model"])},
           "names": names(hexid, agent["harnessName"]), "weights": {"C": 100 - weight, "T1": weight}, "evaluators": evaluators, "metric": metric,
           "sessionTimeout": timeout, "ramp": [{"at": _now(), "by": x.caller.get("username"), "weights": {"C": 100 - weight, "T1": weight}}],
           "createdAt": _now(), "createdBy": x.caller.get("username"), "promotion": None, "error": None}
    with one_at_a_time(agent["harnessId"], agent["harnessName"]):
        if any(e.get("status") in ACTIVE + ("promoting",) and e["agent"]["id"] == agent["harnessId"] for e in _experiments(x)):  # settled first
            raise ExperimentError(f"{agent['harnessName']} already has an experiment running: stop and clean it up first")
        x.console.store.update("experiments", {}, lambda all_: {**all_, eid: rec})
    job = x.console.jobs.start("experiment", x.workspace, {"experiment": eid, "agent": agent["harnessName"]}, lambda j: _setup(x, eid, j),
                               label=f"A/B 实验 {agent['harnessName']}")
    return {"experiment": _save(x, eid, job=job["id"]), "job": job}


def _ensure_role(iam: Any, name: str, trust: Mapping[str, Any], policy: Mapping[str, Any], tags: Mapping[str, str],
                 boundary: str | None = None) -> str:
    """The A/B test's (or canary's) role, on the console's path with the workspace's permissions boundary (an existing
    one only when the console may adopt it, ``agents.adopt``); its ARN as IAM gives it, which the Gateway, the online
    evaluations and the A/B test are given."""
    from .agents import adopt, ensure_boundary, role_args

    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Description="ADLC console A/B test (gateway, routing, evaluation)",
                              Tags=[{"Key": k, "Value": v} for k, v in tags.items()], **role_args(boundary))["Role"]["Arn"]
    except Exception as exc:  # noqa: BLE001
        if "EntityAlreadyExists" not in _code(exc) and "EntityAlreadyExists" not in str(exc):
            raise
        role, found = adopt(iam, name, boundary, error=ExperimentError)
        arn = role["Arn"]
        ensure_boundary(iam, name, boundary, role, found)
    iam.put_role_policy(RoleName=name, PolicyName="experiment", PolicyDocument=json.dumps(policy))
    return arn


def _setup(x: Ctx, eid: str, job: Any) -> dict[str, Any]:
    """The experiment's AWS resources, each kept on the record as soon as it exists (cleanup removes what a failed
    setup made)."""
    rec = _get(x, eid)
    n, agent_id, tags = rec["names"], rec["agent"]["id"], {**CONSOLE_TAG, EXPERIMENT_TAG: eid}
    ctl, data = x.ctl(), x.data()
    try:
        agent = ctl.get_harness(harnessId=agent_id)["harness"]
        if str(agent.get("harnessVersion")) != rec["agent"]["version"]:
            raise ExperimentError(f"{rec['agent']['name']} changed while the experiment was being set up")
        job.log(f"treatment Harness {n['harness']}: {rec['agent']['name']}'s configuration with the treatment's prompt / model")
        if x.boundary:  # the copy runs with the agent's own role: the console's (on its path), given the boundary first
            from .agents import passable

            agent = {**agent, "executionRoleArn": passable(x.iam(), str(agent.get("executionRoleArn") or ""), account=x.account, boundary=x.boundary,
                                                           error=ExperimentError, what=f"{rec['agent']['name']}'s role")}
        twin = ctl.create_harness(**clone_request(agent, rec["treatment"], n["harness"], tags))["harness"]
        twin_runtime = ((twin.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {}).get("agentRuntimeId")
        rec = _save(x, eid, treatmentHarness={"id": twin["harnessId"], "arn": twin["arn"], "name": n["harness"], "runtimeId": twin_runtime})
        job.log(f"control endpoint {n['endpoint']} on {rec['agent']['name']}: version {rec['agent']['version']} (DEFAULT is untouched)")
        ctl.create_harness_endpoint(harnessId=agent_id, endpointName=n["endpoint"], targetVersion=rec["agent"]["version"], tags=tags,
                                    description=f"ADLC console experiment {eid}: the control arm", clientToken=_token())
        rec = _save(x, eid, controlEndpoint=n["endpoint"])
        _wait(lambda: ctl.get_harness(harnessId=twin["harnessId"])["harness"]["status"], ("READY",), what=f"Harness {n['harness']}")
        got = ctl.get_harness(harnessId=twin["harnessId"])["harness"]
        twin_runtime = twin_runtime or ((got.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {}).get("agentRuntimeId")
        # the treatment as the A/B test runs it: pinned, so a verification of a changed copy is never credited (find_verification)
        rec = _save(x, eid, treatmentHarness={**rec["treatmentHarness"], "runtimeId": twin_runtime, "version": str(got.get("harnessVersion") or "")})
        _wait(lambda: ctl.get_harness_endpoint(harnessId=agent_id, endpointName=n["endpoint"])["endpoint"]["status"], ("READY",),
              what=f"endpoint {n['endpoint']}")
        job.log(f"role {n['role']}: the Gateway, the A/B test and the online evaluations")
        trust, policy = role_documents(x.account, x.region, [rec["agent"]["arn"], twin["arn"]])
        role_arn = _ensure_role(x.iam(), n["role"], trust, policy, tags, x.boundary)
        rec = _save(x, eid, role={"name": n["role"], "arn": role_arn})
        _sleep(10)  # IAM propagation
        job.log(f"Gateway {n['gateway']} (AWS_IAM)")
        gw = _retry(lambda: ctl.create_gateway(**gateway_request(n["gateway"], role_arn, tags)))
        rec = _save(x, eid, gateway={"id": gw["gatewayId"], "arn": gw["gatewayArn"], "url": gw.get("gatewayUrl")})
        _wait(lambda: ctl.get_gateway(gatewayIdentifier=gw["gatewayId"])["status"], ("READY",), what=f"Gateway {n['gateway']}")
        url = rec["gateway"]["url"] or ctl.get_gateway(gatewayIdentifier=gw["gatewayId"]).get("gatewayUrl")
        rec = _save(x, eid, gateway={**rec["gateway"], "url": url})
        targets: dict[str, dict[str, str]] = {}
        for variant, arn, qualifier in (("C", rec["agent"]["arn"], n["endpoint"]), ("T1", twin["arn"], "DEFAULT")):
            job.log(f"target {n[variant]}: InvokeHarness {arn.rsplit('/', 1)[-1]} ({qualifier})")
            made = ctl.create_gateway_target(gatewayIdentifier=gw["gatewayId"], **target_request(x.region, n[variant], arn, qualifier))
            targets[variant] = {"name": n[variant], "id": made["targetId"]}
            rec = _save(x, eid, targets=dict(targets))
        for variant in ("C", "T1"):
            _wait(lambda v=variant: ctl.get_gateway_target(gatewayIdentifier=gw["gatewayId"], targetId=targets[v]["id"])["status"], ("READY",),
                  what=f"target {n[variant]}")
        evals: dict[str, dict[str, str]] = {}
        arms = (("C", rec["agent"]["runtimeId"], n["endpoint"], n["evalC"]), ("T1", twin_runtime, "DEFAULT", n["evalT1"]))
        logs = x.logs()
        for variant, runtime_id, endpoint, name in arms:
            # An online evaluation refuses a log group that does not exist yet ("One or more specified log groups do not
            # exist", live), and a new endpoint's appears with its first session: make it now (the runtime writes into it).
            try:
                logs.create_log_group(logGroupName=f"/aws/bedrock-agentcore/runtimes/{runtime_id}-{endpoint}", tags=tags)
            except Exception as exc:  # noqa: BLE001
                if "AlreadyExists" not in _code(exc):
                    raise
            job.log(f"online evaluation {name}: {', '.join(rec['evaluators'])} on every session of {runtime_name(runtime_id)} ({endpoint})")
            made = _retry(lambda r=runtime_id, e=endpoint, m=name: ctl.create_online_evaluation_config(
                **online_eval_request(m, r, e, rec["evaluators"], role_arn, tags, rec["sessionTimeout"])))
            evals[variant] = {"id": made["onlineEvaluationConfigId"], "arn": made["onlineEvaluationConfigArn"], "name": name}
            rec = _save(x, eid, onlineEvaluations=dict(evals))
        for variant in ("C", "T1"):
            _wait(lambda v=variant: ctl.get_online_evaluation_config(onlineEvaluationConfigId=evals[v]["id"])["status"], ("ACTIVE",),
                  what=f"online evaluation {evals[variant]['name']}")
        job.log(f"A/B test {n['abTest']}: {rec['weights']['C']} % control, {rec['weights']['T1']} % treatment")
        made = _retry(lambda: data.create_ab_test(**ab_test_request(n["abTest"], gw["gatewayArn"], {v: t["name"] for v, t in targets.items()},
                                                                    rec["weights"]["T1"], {v: e["arn"] for v, e in evals.items()}, role_arn, tags)))
        rec = _save(x, eid, abTest={"id": made["abTestId"], "arn": made["abTestArn"]})

        def started() -> str:
            ab = data.get_ab_test(abTestId=made["abTestId"])
            return str(ab["status"]) if "FAIL" in str(ab.get("status")) else str(ab.get("executionStatus"))

        _wait(started, ("RUNNING",), what=f"A/B test {n['abTest']}")
        rec = _save(x, eid, status="running", invokeUrl=f"{url.rstrip('/')}/{targets['C']['name']}/invoke")
        job.log(f"running: send sessions to {rec['invokeUrl']}")
        return {"experiment": eid, "invokeUrl": rec["invokeUrl"]}
    except Exception as exc:
        _save(x, eid, status="failed", error=f"{type(exc).__name__}: {str(exc)[:500]}")
        raise


# -- experiments: reading, the split, traffic --------------------------------------------------------------------------

def _live(x: Ctx, rec: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The A/B test as AgentCore has it now and its metrics, or the snapshot kept when it was stopped."""
    snapshot = list((rec.get("finalResults") or {}).get("metrics") or [])
    if not rec.get("abTest") or rec.get("cleanedAt") or rec.get("status") in ("cleaned", "cleaning"):
        return None, snapshot
    try:
        ab = x.data().get_ab_test(abTestId=rec["abTest"]["id"])
    except Exception as exc:  # noqa: BLE001
        if _gone(exc):
            return {"gone": True}, snapshot
        raise
    live = {"status": ab.get("status"), "executionStatus": ab.get("executionStatus"), "weights": {v["name"]: v["weight"] for v in ab.get("variants") or []},
            "analysisTimestamp": str((ab.get("results") or {}).get("analysisTimestamp") or "") or None, "startedAt": str(ab.get("startedAt") or "") or None,
            "stoppedAt": str(ab.get("stoppedAt") or "") or None, "expiresAt": str(ab.get("maxDurationExpiresAt") or "") or None,
            "errors": ab.get("errorDetails") or []}
    return live, ab_metrics(ab) or snapshot


def _arms(ctl: Any, rec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What else the A/B test compared, as AgentCore has it now: the agent's control endpoint (on the version compared)
    and the treatment Harness (on the version set up), for the gate's condition (c)."""
    arms: list[dict[str, Any]] = []
    if rec.get("controlEndpoint"):
        try:
            found = ctl.get_harness_endpoint(harnessId=rec["agent"]["id"], endpointName=rec["controlEndpoint"]).get("endpoint") or {}
            now, target = found.get("liveVersion") or found.get("targetVersion"), found.get("targetVersion")
        except Exception as exc:  # noqa: BLE001
            if not _gone(exc):
                raise
            now = target = None
        arms.append({"what": f"the control endpoint {rec['controlEndpoint']}", "expected": str(rec["agent"]["version"]),
                     "now": None if now is None else str(now), "target": None if target is None else str(target)})
    twin = rec.get("treatmentHarness") or {}
    if twin.get("id") and twin.get("version"):
        try:
            now = str(ctl.get_harness(harnessId=twin["id"])["harness"].get("harnessVersion"))
        except Exception as exc:  # noqa: BLE001
            if not _gone(exc):
                raise
            now = None
        arms.append({"what": f"the treatment Harness {twin.get('name')}", "expected": str(twin["version"]), "now": now, "target": None})
    return arms


def _evidence(x: Ctx, rec: Mapping[str, Any], metric: str | None = None) -> dict[str, Any]:
    rec = _pinned(x, rec)
    live, metrics = _live(x, rec)
    verification, verifying = find_verification(x, rec)
    ctl = x.ctl()
    try:
        version = str(ctl.get_harness(harnessId=rec["agent"]["id"])["harness"].get("harnessVersion"))
    except Exception as exc:  # noqa: BLE001
        if not _gone(exc):
            raise
        version = None
    chosen = metric or rec["metric"]
    for m in metrics:
        m["band"], m["bandSource"] = band_for(verification, m["evaluator"])
    return {"live": live, "metrics": metrics, "verification": verification, "verifying": verifying, "agentVersion": version,
            "gate": gate(rec, verification=verification, metrics=metrics, metric=chosen, agent_version=version, verifying=verifying,
                         arms=_arms(ctl, rec))}


def experiment_detail(x: Ctx, eid: str) -> dict[str, Any]:
    """The record, the A/B test as AgentCore has it now, its metrics and the gate; once promoted, the gate as it was
    decided (the promotion itself moved the agent to a new version)."""
    rec = _get(x, eid)
    if rec["status"] in ("creating", "failed"):
        return {**rec, "live": None, "metrics": [], "verification": None, "gate": None}
    out = {**rec, **_evidence(x, rec)}
    if rec.get("promotion"):
        out["gate"] = {**rec["promotion"]["gate"], "decidedAt": rec["promotion"]["at"]}
    return out


def _ab_wait(data: Any, ab_id: str, *, execution: str) -> dict[str, Any]:
    got: dict[str, Any] = {}

    def probe() -> str:
        got.update(data.get_ab_test(abTestId=ab_id))
        return "READY" if got.get("status") == "ACTIVE" and got.get("executionStatus") == execution else str(got.get("status"))

    _wait(probe, ("READY",), what=f"A/B test {ab_id}", attempts=60, pause=2.0)
    return got


def split_allowed(x: Ctx, rec: Mapping[str, Any], weight: int, before: int, acknowledged: bool, decide: Callable[[], Mapping[str, Any]],
                  *, arm: str = "treatment", base: str = "the control") -> None:
    """The ramp's rules (experiments and runtime canaries alike): an admin's; above :data:`MAX_UNPROVEN` only once the
    gate holds (no acknowledgement opens it: past half the sessions the treatment is production without the evidence);
    up to it, raising refused while the A/B result shows the treatment worse beyond the noise band unless acknowledged.
    ``decide()`` gives the gate as it stands."""
    if x.caller.get("role") != "admin":
        raise ExperimentError("changing the split needs an admin (it decides how many sessions the treatment serves)", status=403)
    if weight > MAX_UNPROVEN:
        decided = decide()
        if not decided["ok"]:
            failed = [c for c in decided["conditions"] if not c["ok"]]
            raise ExperimentError(f"above {MAX_UNPROVEN} % only once the gate holds (the {arm} would serve most sessions without the evidence a "
                                  "promotion needs): " + "; ".join(f"{c['id']}: {c['evidence']}" for c in failed), gate=decided)
        return
    if weight > before and not acknowledged:
        ab = next(c for c in decide()["conditions"] if c["id"] == "ab")
        if ab.get("worse"):
            raise ExperimentError(f"the {arm} is worse than {base} beyond the noise band ({ab['evidence']}): ramping it up needs acknowledged: true",
                                  condition=ab)


def set_split(x: Ctx, eid: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """The canary ramp: a new treatment share (pause, change the weights, resume: weights only change while paused),
    an admin's (:func:`split_allowed`: above 50 % only once the gate holds; raising refused while the A/B result shows
    the treatment worse beyond the noise band, unless ``acknowledged: true``)."""
    agent = _get(x, eid)["agent"]
    with one_at_a_time(agent["id"], agent.get("name")):
        return _set_split(x, eid, body)


def _set_split(x: Ctx, eid: str, body: Mapping[str, Any]) -> dict[str, Any]:
    rec = _get(x, eid)  # under the agent's lock: as another admin's action left it
    if rec["status"] not in ("running", "paused") or not rec.get("abTest"):
        raise ExperimentError(f"experiment {eid} is {rec['status']}: only a running or paused A/B test takes a new split")
    weight = _weight(body.get("treatmentWeight"))
    before = int(rec["weights"]["T1"])
    split_allowed(x, rec, weight, before, body.get("acknowledged") is True, lambda: _evidence(x, rec)["gate"])
    data = x.data()
    ab_id = rec["abTest"]["id"]
    was = data.get_ab_test(abTestId=ab_id).get("executionStatus")
    if was == "STOPPED":
        raise ExperimentError(f"A/B test {ab_id} is stopped")
    if was == "RUNNING":
        data.update_ab_test(abTestId=ab_id, executionStatus="PAUSED", clientToken=_token())
        _ab_wait(data, ab_id, execution="PAUSED")
    data.update_ab_test(abTestId=ab_id, variants=variants({v: t["name"] for v, t in rec["targets"].items()}, weight), clientToken=_token())
    _ab_wait(data, ab_id, execution=str(was if was != "RUNNING" else "PAUSED"))
    if was == "RUNNING":
        data.update_ab_test(abTestId=ab_id, executionStatus="RUNNING", clientToken=_token())
        _ab_wait(data, ab_id, execution="RUNNING")
    weights = {"C": 100 - weight, "T1": weight}
    step = {"at": _now(), "by": x.caller.get("username"), "weights": weights, **({"acknowledged": True} if weight > before and body.get("acknowledged") is True else {})}
    return _save(x, eid, weights=weights, ramp=[*rec.get("ramp", []), step])


def set_state(x: Ctx, eid: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Pause (every session goes to the control), resume, or stop the A/B test; stopping keeps its results."""
    agent = _get(x, eid)["agent"]
    with one_at_a_time(agent["id"], agent.get("name")):
        return _set_state(x, eid, body)


def _set_state(x: Ctx, eid: str, body: Mapping[str, Any]) -> dict[str, Any]:
    rec = _get(x, eid)  # under the agent's lock: as another admin's action left it
    state = str(body.get("executionStatus") or "")
    if state not in ("PAUSED", "RUNNING", "STOPPED"):
        raise ExperimentError("executionStatus: PAUSED, RUNNING or STOPPED", status=400)
    if rec["status"] not in ("running", "paused") or not rec.get("abTest"):
        raise ExperimentError(f"experiment {eid} is {rec['status']}")
    return _stop(x, rec) if state == "STOPPED" else _pause_resume(x, rec, state)


def _pause_resume(x: Ctx, rec: Mapping[str, Any], state: str) -> dict[str, Any]:
    data = x.data()
    data.update_ab_test(abTestId=rec["abTest"]["id"], executionStatus=state, clientToken=_token())
    _ab_wait(data, rec["abTest"]["id"], execution=state)
    return _save(x, rec["id"], status="paused" if state == "PAUSED" else "running")


def _stop(x: Ctx, rec: Mapping[str, Any], *, status: str = "stopped") -> dict[str, Any]:
    data = x.data()
    ab = data.get_ab_test(abTestId=rec["abTest"]["id"])
    if ab.get("executionStatus") != "STOPPED":
        data.update_ab_test(abTestId=rec["abTest"]["id"], executionStatus="STOPPED", clientToken=_token())
        ab = _ab_wait(data, rec["abTest"]["id"], execution="STOPPED")
    final = {"at": _now(), "metrics": ab_metrics(ab) or list((rec.get("finalResults") or {}).get("metrics") or []),
             "analysisTimestamp": str((ab.get("results") or {}).get("analysisTimestamp") or "") or None}
    return _save(x, rec["id"], status=status, finalResults=final)


def contract_queries(x: Ctx, sid: str) -> list[str]:
    from .evaluation import EvaluationError, contract_set

    try:
        return [c["query"] for c in contract_set(x.console.store, x.workspace, sid)["contracts"]]
    except EvaluationError as exc:
        raise ExperimentError(str(exc), status=404) from exc


class SentBody(io.BytesIO):
    """A request body that knows when all of it went to the socket (``sent``). urllib3 reads a body block by block
    and sends each before reading the next, until a read comes back empty: that read comes only after the last block
    was sent. The request carries a Content-Length, so before then the server cannot have the whole request."""

    sent = False

    def read(self, size: int | None = -1) -> bytes:
        block = super().read(size)
        if not block and size != 0:
            self.sent = True
        return block


def post_signed(session: Any, region: str, url: str, data: bytes | SentBody, headers: Mapping[str, str], *,
                timeout: float = 180.0) -> tuple[int, str, bytes]:
    """A SigV4 POST (service ``bedrock-agentcore``) through the same HTTP stack and proxies boto3 uses. ``data`` given
    as a :class:`SentBody` says afterwards whether the whole request went out."""
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.httpsession import URLLib3Session
    from botocore.utils import get_environ_proxies

    request = AWSRequest(method="POST", url=url, data=data.getvalue() if isinstance(data, SentBody) else data, headers=dict(headers))
    SigV4Auth(session.get_credentials().get_frozen_credentials(), "bedrock-agentcore", region).add_auth(request)
    prepared = request.prepare()
    if isinstance(data, SentBody):
        prepared.body = data  # the bytes just signed, sent from the object that marks when the last of them went out
    http = URLLib3Session(proxies=get_environ_proxies(url), timeout=timeout)
    try:
        response = http.send(prepared)
        return int(response.status_code), str(response.headers.get("Content-Type") or ""), bytes(response.content or b"")
    finally:
        http.close()


def stream_events(content_type: str, raw: bytes) -> list[tuple[str, dict[str, Any]]]:
    """An InvokeHarness event stream as ``(event type, payload)``; an exception as ``("exception", {"error"})``; a body
    that is no stream (a JSON error) as one exception."""
    if "eventstream" not in content_type:
        return [("exception", {"error": raw.decode("utf-8", "replace")[:300] or "empty answer"})]
    from botocore.eventstream import EventStreamBuffer

    buffer = EventStreamBuffer()
    buffer.add_data(raw)
    out: list[tuple[str, dict[str, Any]]] = []
    for message in buffer:
        headers = message.headers
        try:
            payload = json.loads(message.payload or b"{}")
        except ValueError:
            payload = {}
        if headers.get(":message-type") == "exception":
            out.append(("exception", {"error": f"{headers.get(':exception-type')}: {str(payload.get('message') or payload)[:200]}"}))
        else:
            out.append((str(headers.get(":event-type") or ""), payload if isinstance(payload, dict) else {}))
    return out


def answer_text(content_type: str, raw: bytes) -> tuple[str, str | None]:
    """The text of an InvokeHarness event stream (or of a JSON error), and the stream's error if any."""
    text, error = [], None
    for kind, payload in stream_events(content_type, raw):
        if kind == "exception":
            error = payload["error"]
        elif kind == "contentBlockDelta":
            text.append(str((payload.get("delta") or {}).get("text") or ""))
    return "".join(text), error


def twin_of(console: Any, workspace: str, harness_id: str) -> dict[str, Any] | None:
    """The experiment whose treatment Harness this is, until it is cleaned up: its evidence is about the version set up."""
    for rec in (console.store.read("experiments", {}) or {}).values():
        if (rec.get("workspace") == workspace and not rec.get("cleanedAt") and rec.get("status") not in ("cleaned", "failed")
                and (rec.get("treatmentHarness") or {}).get("id") == harness_id):
            return rec
    return None


def route_for(console: Any, workspace: str, harness_id: str) -> dict[str, Any] | None:
    """The running experiment of a Harness, if it has one: its traffic should go through the experiment's Gateway."""
    for rec in (console.store.read("experiments", {}) or {}).values():
        if (rec.get("workspace") == workspace and rec.get("status") == "running" and rec.get("invokeUrl")
                and (rec.get("agent") or {}).get("id") == harness_id):
            return rec
    return None


def invoke_through(session: Any, region: str, rec: Mapping[str, Any], *, message: str, session_id: str | None, actor: str) -> Iterator[dict[str, Any]]:
    """One turn of the console chat or the public API through a running experiment's Gateway, as the events of
    ``agents.invoke`` plus ``{"type": "experiment"}``: the Gateway decides the session's arm and keeps it for the
    session. The passthrough target answers with the whole InvokeHarness stream at once, so the text comes in one piece."""
    from . import agents

    if not message.strip() or len(message) > 20_000:
        raise agents.AgentError("message: 1-20000 characters")
    sid = session_id or agents.new_session()
    if not agents.SESSION.match(sid):
        raise agents.AgentError("sessionId: 33-100 letters, digits, '-' or '_'")
    yield {"type": "session", "sessionId": sid}
    yield {"type": "experiment", "id": rec["id"], "name": rec.get("name"), "weights": rec.get("weights")}
    started = time.monotonic()
    payload = json.dumps({"messages": [{"role": "user", "content": [{"text": message}]}], "actorId": actor}).encode("utf-8")
    try:
        status, kind, raw = post_signed(session, region, rec["invokeUrl"], payload, {"Content-Type": "application/json",
                                                                                     "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sid})
    except Exception as exc:  # noqa: BLE001 - shown in the chat
        yield {"type": "error", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        return
    if status != 200:
        yield {"type": "error", "error": f"the experiment's Gateway answered {status}: {raw.decode('utf-8', 'replace')[:300]}"}
        return
    usage = {"inputTokens": 0, "outputTokens": 0}
    pending: dict[int, list[str]] = {}
    for event, body in stream_events(kind, raw):
        if event == "contentBlockDelta":
            delta = body.get("delta") or {}
            if delta.get("text"):
                yield {"type": "text", "text": delta["text"]}
            elif "toolUse" in delta and int(body.get("contentBlockIndex") or 0) in pending:
                pending[int(body.get("contentBlockIndex") or 0)][1] += str((delta.get("toolUse") or {}).get("input") or "")
        elif event == "contentBlockStart":
            tool = ((body.get("start") or {}).get("toolUse") or {}).get("name")
            if tool:
                pending[int(body.get("contentBlockIndex") or 0)] = [tool, ""]
                yield {"type": "tool", "name": tool}
        elif event == "contentBlockStop":
            done = pending.pop(int(body.get("contentBlockIndex") or 0), None)
            if done:
                try:
                    arguments: Any = json.loads(done[1]) if done[1] else {}
                except ValueError:
                    arguments = done[1]
                yield {"type": "toolInput", "name": done[0], "input": arguments}
        elif event == "metadata":
            got = body.get("usage") or {}
            usage = {k: usage[k] + int(got.get(k) or 0) for k in usage}
        elif event == "messageStop":
            yield {"type": "turn", "stopReason": body.get("stopReason")}
        elif event == "exception":
            yield {"type": "error", "error": body["error"]}
    yield {"type": "stop", "seconds": round(time.monotonic() - started, 1), **usage}


def send_traffic(x: Ctx, eid: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Replay prompts (a contract set's questions, or ``prompts``) through the experiment's Gateway, one new session
    each, ``repeat`` times: the Gateway decides each session's arm (console job ``experiment-traffic``)."""
    rec = _get(x, eid)
    if rec["status"] not in ("running", "paused") or not rec.get("invokeUrl"):
        raise ExperimentError(f"experiment {eid} is {rec['status']}: traffic goes to a running A/B test")
    prompts = contract_queries(x, str(body["contractSet"])) if body.get("contractSet") else [str(p).strip() for p in body.get("prompts") or [] if str(p).strip()]
    repeat = _int(body.get("repeat"), 1, "repeat", 1, 5)
    if not prompts or len(prompts) * repeat > MAX_TRAFFIC:
        raise ExperimentError(f"give a contractSet or prompts, at most {MAX_TRAFFIC} sessions in all", status=400)
    hexid, url, session, region = eid[4:], rec["invokeUrl"], x.session, x.region
    items = [(i, q) for i, q in enumerate([q for _ in range(repeat) for q in prompts], 1)]

    def work(job: Any) -> dict[str, Any]:
        def one(item: tuple[int, str]) -> dict[str, Any]:
            index, query = item
            sid = f"exp-{hexid}-{uuid.uuid4().hex}"
            payload = json.dumps({"messages": [{"role": "user", "content": [{"text": query}]}], "actorId": f"exp-{hexid}-{index}"}).encode("utf-8")
            try:
                status, kind, raw = post_signed(session, region, url, payload, {"Content-Type": "application/json",
                                                                                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sid})
                text, error = answer_text(kind, raw) if status == 200 else ("", raw.decode("utf-8", "replace")[:300])
            except Exception as exc:  # noqa: BLE001 - counted, shown on the job
                status, text, error = 0, "", f"{type(exc).__name__}: {str(exc)[:200]}"
            return {"sessionId": sid, "query": query[:200], "status": status, "answer": text[:300], "error": error}

        job.log(f"{len(items)} session(s) through {url}")
        with ThreadPoolExecutor(max_workers=TRAFFIC_WORKERS) as pool:
            done = list(pool.map(one, items))
        failed = [d for d in done if d["status"] != 200 or d["error"]]
        statuses: dict[str, int] = {}
        for d in done:
            statuses[str(d["status"])] = statuses.get(str(d["status"]), 0) + 1
        job.progress(sent=len(done) - len(failed), failed=len(failed))
        for d in failed[:3]:
            job.log(f"failed {d['status']}: {d['error']}")
        return {"sent": len(done) - len(failed), "failed": len(failed), "statuses": statuses, "samples": done[:10],
                "sessions": [d["sessionId"] for d in done if d["status"] == 200]}

    return x.console.jobs.start("experiment-traffic", x.workspace, {"experiment": eid, "sessions": len(items)}, work,
                                label=f"A/B 流量 {rec['agent']['name']}")


# -- promotion ---------------------------------------------------------------------------------------------------------

def apply_treatment(ctl: Any, agent: Mapping[str, Any], treatment: Mapping[str, Any]) -> dict[str, Any]:
    """UpdateHarness with the treatment's prompt and / or model (in the shape the API takes): a new version, which
    DEFAULT serves at once."""
    changes: dict[str, Any] = {}
    if treatment["systemPrompt"] != prompt_of(agent):
        changes["systemPrompt"] = [{"text": treatment["systemPrompt"]}]
    if treatment.get("model") and treatment["model"] != model_of(agent):
        current = (agent.get("model") or {}).get("bedrockModelConfig")
        if current is None:
            raise ExperimentError("the agent's model is not a Bedrock model configuration")
        changes["model"] = {"bedrockModelConfig": {**current, "modelId": treatment["model"]}}
    if not changes:
        raise ExperimentError("the agent already runs the treatment")
    ctl.update_harness(harnessId=agent["harnessId"], clientToken=_token(), **changes)
    _wait(lambda: ctl.get_harness(harnessId=agent["harnessId"])["harness"]["status"], ("READY",), what=f"Harness {agent['harnessName']}")
    after = ctl.get_harness(harnessId=agent["harnessId"])["harness"]
    return {"version": str(after.get("harnessVersion")), "changed": sorted(changes)}


def promote(x: Ctx, eid: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the treatment to the agent only when the gate holds; an admin's ``acknowledged: true`` (with a reason)
    overrides a failed gate, and the override is recorded with the evidence it overrode."""
    if x.caller.get("role") != "admin":
        raise ExperimentError("promotion needs an admin", status=403)
    agent = _get(x, eid)["agent"]
    with one_at_a_time(agent["id"], agent.get("name")):  # two promotions at once would apply the treatment twice
        return _promote(x, eid, body)


def _promote(x: Ctx, eid: str, body: Mapping[str, Any]) -> dict[str, Any]:
    rec = _get(x, eid)  # under the agent's lock: another admin's promotion or cleanup may have come first
    if rec["status"] in ("creating", "failed", "promoted", "cleaning", "cleanup_incomplete") or rec.get("cleanedAt"):
        raise ExperimentError(f"experiment {eid} is {rec['status']}: nothing to promote")
    metric = rec["metric"]  # the experiment's own, declared when it started: another one is an override
    switched = switched_metric(rec, body.get("metric"))
    evidence = _evidence(x, rec, metric)
    decided = evidence["gate"]
    if switched:
        decided = {**decided, "ok": False, "conditions": [*decided["conditions"], metric_condition(metric, switched, _evidence(x, rec, switched)["gate"])]}
    failed = [c for c in decided["conditions"] if not c["ok"]]
    override = bool(failed) and body.get("acknowledged") is True
    reason = str(body.get("reason") or "").strip()[:500]
    if failed and not override:
        raise ExperimentError("promotion refused: " + "; ".join(f"{c['id']}: {c['evidence']}" for c in failed), gate=decided)
    if override and len(reason) < 5:
        raise ExperimentError("an override of the gate needs a reason (why promote against the evidence)", status=400, gate=decided)
    if rec.get("abTest") and rec["status"] in ("running", "paused", "stopped"):
        rec = _stop(x, rec, status=rec["status"])  # the split ends; its results are kept with the record
    ctl = x.ctl()
    agent = ctl.get_harness(harnessId=rec["agent"]["id"])["harness"]
    applied = apply_treatment(ctl, agent, rec["treatment"])
    promotion = {"experiment": eid, "workspace": x.workspace, "agent": rec["agent"]["name"], "at": _now(), "by": x.caller.get("username"),
                 "fromVersion": str(agent.get("harnessVersion")), "toVersion": applied["version"], "changed": applied["changed"], "metric": metric,
                 "requestedMetric": switched, "gate": decided, "override": override, "failed": [c["id"] for c in failed],
                 "reason": reason if override else None, "bundle": rec["bundle"], "fingerprint": rec["treatment"]["fingerprint"]}
    x.console.store.update("promotions", [], lambda all_: [*all_, promotion])
    _save(x, eid, status="promoted", promotion=promotion)
    return promotion


def promotions(x: Ctx) -> list[dict[str, Any]]:
    return [p for p in reversed(x.console.store.read("promotions", [])) if p.get("workspace") == x.workspace]


# -- cleanup -----------------------------------------------------------------------------------------------------------

def cleanup_experiment(x: Ctx, eid: str) -> dict[str, Any]:
    """Delete everything the experiment created (console job ``experiment-cleanup``); the record, its final results
    and any promotion stay. A cleanup that left something runs again."""
    agent = _get(x, eid)["agent"]
    with one_at_a_time(agent["id"], agent.get("name")):
        rec = _get(x, eid)  # settled (a setup or cleanup the console's restart cut off no longer holds it), under the agent's lock
        if rec["status"] == "creating":
            raise ExperimentError(f"experiment {eid} is still being set up: wait for its job")
        if rec["status"] == "cleaning":
            raise ExperimentError(f"experiment {eid} is being cleaned up: wait for its job")
        if rec.get("cleanedAt"):
            raise ExperimentError(f"experiment {eid} is already cleaned up")
        before = str(rec.get("cleanupFrom") or rec["status"]) if rec["status"] == "cleanup_incomplete" else rec["status"]
        after = "promoted" if before == "promoted" or rec.get("promotion") else "cleaned"
        _save(x, eid, status="cleaning", cleanupFrom=before, cleanupJob=None)
    job = x.console.jobs.start("experiment-cleanup", x.workspace, {"experiment": eid}, lambda job: _cleanup(x, eid, job, after),
                               label=f"清理 A/B 实验 {rec['agent']['name']}")
    _save(x, eid, cleanupJob=job["id"])
    return job


def cleanup_step(done: list[dict[str, Any]], job: Any, label: str, fn: Callable[[], Any]) -> None:
    """One resource of a cleanup (experiments and canaries alike): every one is tried and its result recorded —
    ``deleted`` (or the step's own note), ``already gone``, ``left: …`` for one that is not this record's (by design),
    and ``left: <AWS error>`` marked ``failed`` (the cleanup is then incomplete and runs again)."""
    try:
        note = fn()
        done.append({"resource": label, "result": str(note) if isinstance(note, str) else "deleted"})
    except NotOurs as exc:
        done.append({"resource": label, "result": f"left: {exc}"})
    except Exception as exc:  # noqa: BLE001 - every resource is tried; what stays is reported
        if _gone(exc):
            done.append({"resource": label, "result": "already gone"})
        else:
            done.append({"resource": label, "result": f"left: {_code(exc)}: {str(exc)[:200]}", "failed": True})
    job.log(f"{label}: {done[-1]['result']}")


def finish_cleanup(x: Ctx, collection: str, rid: str, done: list[dict[str, Any]], after: str, kept: Sequence[str] = ("promoted", "rolled_back")) -> dict[str, Any]:
    """The record after its cleanup: ``after`` and ``cleanedAt`` when nothing failed; otherwise not cleaned — what was
    left is on the record (``cleanupLeft``), its status ``cleanup_incomplete`` (or ``kept`` as it was: a promotion can
    still be rolled back), and cleanup runs again."""
    left = [d["resource"] for d in done if d.get("failed")]

    def change(all_: dict[str, Any]) -> dict[str, Any]:
        rec = dict(all_[rid])
        if left:
            rec.update(status=after if after in kept else "cleanup_incomplete", cleanup=done, cleanupLeft=left, cleanedAt=None,
                       error=f"cleanup left {len(left)} resource(s) ({', '.join(left[:3])}{'…' if len(left) > 3 else ''}): clean up again")
        else:
            rec.update(status=after, cleanup=done, cleanupLeft=None, cleanedAt=_now())
        rec["updatedAt"] = _now()
        return {**all_, rid: rec}

    x.console.store.update(collection, {}, change)
    return {"cleanup": done, "left": left}


def _cleanup(x: Ctx, eid: str, job: Any, after: str) -> dict[str, Any]:
    rec = _get(x, eid)
    ctl, data, iam, logs = x.ctl(), x.data(), x.iam(), x.logs()
    done: list[dict[str, Any]] = []

    def step(label: str, fn: Callable[[], Any]) -> None:
        cleanup_step(done, job, label, fn)

    ab = rec.get("abTest")
    if ab:
        def delete_ab() -> None:
            if data.get_ab_test(abTestId=ab["id"]).get("executionStatus") != "STOPPED":
                _stop(x, _get(x, eid), status="cleaning")
            data.delete_ab_test(abTestId=ab["id"])
            for _ in range(60):  # deleting is asynchronous; the online evaluations it references refuse deletion until it is gone
                try:
                    data.get_ab_test(abTestId=ab["id"])
                except Exception as exc:  # noqa: BLE001
                    if _gone(exc):
                        return
                    raise
                _sleep(3)

        step(f"A/B test {ab['id']}", delete_ab)
    for ev in (rec.get("onlineEvaluations") or {}).values():
        step(f"online evaluation {ev['id']}", lambda e=ev: _retry(lambda: ctl.delete_online_evaluation_config(onlineEvaluationConfigId=e["id"]),
                                                                when=_busy, attempts=10, pause=6.0))
    gw = rec.get("gateway")
    for target in (rec.get("targets") or {}).values():
        step(f"target {target['name']}", lambda t=target: _retry(lambda: ctl.delete_gateway_target(gatewayIdentifier=gw["id"], targetId=t["id"]),
                                                                 when=_busy, attempts=10, pause=6.0))
    if gw:
        # DeleteGateway is refused until its A/B test and targets are gone (minutes after the lists stop showing them).
        step(f"Gateway {gw['id']}", lambda: _retry(lambda: ctl.delete_gateway(gatewayIdentifier=gw["id"]), when=_busy, attempts=50, pause=6.0))
    if rec.get("controlEndpoint"):
        step(f"endpoint {rec['controlEndpoint']} of {rec['agent']['name']}",
             lambda: ctl.delete_harness_endpoint(harnessId=rec["agent"]["id"], endpointName=rec["controlEndpoint"], clientToken=_token()))
    twin = rec.get("treatmentHarness")
    if twin:
        def delete_twin() -> None:
            tags = ctl.list_tags_for_resource(resourceArn=twin["arn"]).get("tags") or {}
            if tags.get("adlc:console") != "1" or tags.get(EXPERIMENT_TAG) != eid:
                raise NotOurs(f"{twin['name']} is not this experiment's treatment Harness: left as it is")
            ctl.delete_harness(harnessId=twin["id"])

        step(f"treatment Harness {twin['name']}", delete_twin)
    role = rec.get("role")
    if role:
        def delete_role() -> None:
            from .agents import ensure_boundary, refusal

            tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=role["name"]).get("Tags") or []}
            if tags.get("adlc:console") != "1":
                raise NotOurs(f"role {role['name']} is not the console's: left as it is")
            found = iam.get_role(RoleName=role["name"])["Role"]
            why = refusal(role["name"], found, tags, x.boundary)  # off the console's path: never deleted from here
            if why:
                raise NotOurs(why)
            if x.boundary:  # a role made before the workspace had its boundary: given it first (a spoke role deletes policies only so)
                ensure_boundary(iam, role["name"], x.boundary, found, tags)
            for policy in iam.list_role_policies(RoleName=role["name"]).get("PolicyNames") or []:
                iam.delete_role_policy(RoleName=role["name"], PolicyName=policy)
            iam.delete_role(RoleName=role["name"])

        step(f"role {role['name']}", delete_role)
    groups = [f"/aws/bedrock-agentcore/evaluations/results/{ev['id']}" for ev in (rec.get("onlineEvaluations") or {}).values()]
    if rec.get("controlEndpoint") and rec["agent"].get("runtimeId"):
        groups.append(f"/aws/bedrock-agentcore/runtimes/{rec['agent']['runtimeId']}-{rec['controlEndpoint']}")
    if twin and twin.get("runtimeId"):
        groups.append(f"/aws/bedrock-agentcore/runtimes/{twin['runtimeId']}-DEFAULT")
    for group in groups:
        step(f"log group {group}", lambda g=group: logs.delete_log_group(logGroupName=g))
    return finish_cleanup(x, "experiments", eid, done, after, kept=("promoted",))


# -- routes ------------------------------------------------------------------------------------------------------------

def _client_status(exc: BaseException) -> int | None:
    code = _code(exc)
    if any(g in code for g in GONE):
        return 404
    if code in ("ValidationException", "ConflictException", "ServiceQuotaExceededException"):
        return 409
    if "AccessDenied" in code:
        return 403
    return None


def register(router: Any) -> None:
    def ctx(r: Any) -> Ctx:
        wid = r.workspace()
        session = r.session()
        ws = r.console.workspaces.get(wid)
        return Ctx(r.console, wid, session, ws["region"], ws["accountId"], r.caller, boundary=ws.get("permissionsBoundaryArn"))

    def handle(fn: Callable[[Ctx, Any], Any], status: int = 200) -> Callable[[Any], tuple[int, Any]]:
        def route(r: Any) -> tuple[int, Any]:
            try:
                return status, fn(ctx(r), r)
            except ExperimentError as exc:
                return exc.status, {"error": str(exc), **exc.extra}
            except Exception as exc:  # noqa: BLE001 - an AWS refusal is the caller's to read, not an internal error
                code = _client_status(exc) if hasattr(exc, "response") else None
                if code is None:
                    raise
                return code, {"error": f"{_code(exc)}: {str(exc)[:400]}"}

        return route

    add, base = router.add, "/workspaces/{wid}/experiments"
    add("GET", base + "/bundles", handle(lambda x, r: {"bundles": list_bundles(x)}))
    add("POST", base + "/bundles", handle(lambda x, r: create_bundle(x, r.body), 201))
    add("GET", base + "/bundles/{bid}", handle(lambda x, r: bundle_detail(x, r.params["bid"])))
    add("POST", base + "/bundles/{bid}/versions", handle(lambda x, r: add_version(x, r.params["bid"], r.body), 201))
    add("DELETE", base + "/bundles/{bid}", handle(lambda x, r: delete_bundle(x, r.params["bid"])))
    add("POST", base + "/verifications", handle(lambda x, r: verify_treatment(x, r.body), 202))
    add("GET", base + "/ab-tests", handle(lambda x, r: {"experiments": list_experiments(x)}))
    add("POST", base + "/ab-tests", handle(lambda x, r: start_experiment(x, r.body), 202))
    add("GET", base + "/ab-tests/{eid}", handle(lambda x, r: experiment_detail(x, r.params["eid"])))
    add("POST", base + "/ab-tests/{eid}/split", handle(lambda x, r: set_split(x, r.params["eid"], r.body)), admin=True)
    add("POST", base + "/ab-tests/{eid}/state", handle(lambda x, r: set_state(x, r.params["eid"], r.body)))
    add("POST", base + "/ab-tests/{eid}/traffic", handle(lambda x, r: send_traffic(x, r.params["eid"], r.body), 202))
    add("POST", base + "/ab-tests/{eid}/promote", handle(lambda x, r: promote(x, r.params["eid"], r.body)), admin=True)
    add("DELETE", base + "/ab-tests/{eid}", handle(lambda x, r: cleanup_experiment(x, r.params["eid"]), 202))
    add("GET", base + "/promotions", handle(lambda x, r: {"promotions": promotions(x)}))
