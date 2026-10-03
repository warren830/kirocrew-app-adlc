"""Workshop Customizer — KiroCrew app backend (separate process, stdlib only).

Launched by the KiroCrew gateway with ``PORT`` / ``KIROCREW_APP_NAME`` / ``KIROCREW_PROXY_SECRET``
in the environment; requests reach us through the gateway proxy under
``/api/apps/workshop-customizer``. Binds 127.0.0.1 only.

It also serves the ADLC console under ``/api/console`` (``app/console/server.py``'s ``Mount``, loaded on
its first request): the App's page frames it from ``/apps/workshop-customizer/api/console/``. Those
requests are served only with the gateway's signature (KiroCrew's sign-in is the console's there) and
never without a proxy secret; the console keeps its state in ``<data>/console``. The console's public
API (``/v1``, behind its own ``X-Api-Key``) has a port of its own on 127.0.0.1 (:func:`start_public_api`,
``ADLC_PUBLIC_API_PORT``, 8772 unless set; 0 turns it off): the gateway admits only a signed-in browser
and cuts every request at 30 s, so other programs on this machine call the agents there, streams included.

Security posture
- If ``KIROCREW_PROXY_SECRET`` is present every request must carry a fresh, valid
  ``X-KiroCrew-Proxy`` signature (fail closed). Without the secret (local dev/tests) the server
  still only listens on loopback.
- No credential values are ever stored or logged: a project's *target* holds only the AWS CLI
  profile name, region, expected account id and stack names. boto3 resolves credentials at call time.
- Sync is two-phase: ``POST .../sync/preflight`` returns the plan + a one-time confirm token bound to
  the plan digest; ``POST .../sync/apply`` requires that token. Only ``engine.sync`` talks to AWS and
  it only ever invokes the custom SSM document.

The engine lives in the repository checkout (``WORKSHOP_CUSTOMIZER_HOME``), resolved in order:
env var → ``<app>/../`` when it holds ``template-lock.json`` → ``data/config.json["homeDir"]``.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
import zipfile
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

APP_NAME = os.environ.get("KIROCREW_APP_NAME", "workshop-customizer")
ROUTE_PREFIX = f"/api/apps/{APP_NAME}"
ITEM_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,60}$")
SIGNATURE_MAX_SKEW = 300
CONFIRM_TOKEN_TTL = 900
TEMPLATES = ("hr-default", "it-helpdesk", "maintenance", "blank")
#: scenario.packKind values (SPEC D12: reference, strict customer, and workshop = origin-labeled
#: synthetic settings plus customer-confirmed anchors).
PACK_KINDS = ("customer", "reference", "workshop")
APP_DIR = Path(__file__).resolve().parents[1]
#: The generation routes module (stdlib at top level).  The helpers both processes need (the pack
#: digest and its APP_MANAGED_DIRS today) have exactly one implementation there; see ``_gen()``.
ROUTES_PY = Path(__file__).resolve().with_name("routes.py")
_ROUTES_LOCK = threading.Lock()
#: The modules :func:`_load_once` loaded, by path.
_LOADED: dict[Path, Any] = {}


def utc_now() -> str:
    """Now, as every record of both processes writes it (``routes._utc_now``)."""
    return _gen()._utc_now()


# ---------------------------------------------------------------------------
# Engine + home resolution
# ---------------------------------------------------------------------------


def installed_homes(app_dir: Path | None = None) -> list[Path]:
    """Where KiroCrew installed this App from (its ``installed.json``): an Install-from-Path source's parent (the
    checkout around ``app/``), or, for a registry install (``registry:<name>``, the registry entry's ``subdirectory``
    being ``app``), KiroCrew's persistent clone of the repository (``~/.kiro/crew/app-sources/<name>``)."""
    app_dir = app_dir or APP_DIR
    try:
        source = str(json.loads((app_dir / "installed.json").read_text(encoding="utf-8")).get("source") or "")
    except (OSError, ValueError, AttributeError):
        return []
    if source.startswith("registry:"):
        return [app_dir.parent.parent / "app-sources" / source[len("registry:"):]]
    return [Path(source).expanduser().parent] if source else []


def resolve_home(data_dir: Path) -> Path | None:
    env = os.environ.get("WORKSHOP_CUSTOMIZER_HOME")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env))
    candidates.append(APP_DIR.parent)
    candidates += installed_homes()
    cfg = data_dir / "config.json"
    if cfg.is_file():
        try:
            home = json.loads(cfg.read_text(encoding="utf-8")).get("homeDir")
            if home:
                candidates.append(Path(home).expanduser())
        except (OSError, json.JSONDecodeError):
            pass
    for cand in candidates:
        if (cand / "template-lock.json").is_file() and (cand / "engine" / "workshop_customizer").is_dir():
            return cand.resolve()
    return None


def load_engine(home: Path):
    engine_dir = str(home / "engine")
    if engine_dir not in sys.path:
        sys.path.insert(0, engine_dir)
    import workshop_customizer.calibration as calibration  # noqa: WPS433
    import workshop_customizer.compiler as compiler
    import workshop_customizer.guide as guide
    import workshop_customizer.materials as materials
    import workshop_customizer.render as render
    import workshop_customizer.scenario as scenario
    import workshop_customizer.sync as sync
    import workshop_customizer.validator as validator

    return {"calibration": calibration, "compiler": compiler, "guide": guide, "materials": materials,
            "render": render, "scenario": scenario, "sync": sync, "validator": validator}


def _jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


# ---------------------------------------------------------------------------
# Signature verification (gateway → app)
# ---------------------------------------------------------------------------


def verify_proxy_signature(secret: str, header_value: str, method: str, targets: list[str], body: bytes, *, now: float | None = None) -> bool:
    if not secret or not header_value or ":" not in header_value:
        return False
    ts_str, _, sig = header_value.partition(":")
    if not ts_str.isdigit():
        return False
    now = now if now is not None else time.time()
    if abs(now - int(ts_str)) > SIGNATURE_MAX_SKEW:
        return False
    body_hash = hashlib.sha256(body).hexdigest()
    for target in targets:
        msg = f"{ts_str}:{method.upper()}:{target}:{body_hash}"
        expected = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, sig):
            return True
    return False


def sign_for_tests(secret: str, method: str, target: str, body: bytes, ts: int | None = None) -> str:
    ts = ts if ts is not None else int(time.time())
    msg = f"{ts}:{method.upper()}:{target}:{hashlib.sha256(body).hexdigest()}"
    return f"{ts}:{hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()}"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message, **extra}


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.projects_dir = data_dir / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def project_dir(self, project_id: str, *, must_exist: bool = True) -> Path:
        if not _gen().PROJECT_RE.match(project_id):
            raise HttpError(400, "project id must be kebab-case (3-41 chars)")
        path = self.projects_dir / project_id
        if must_exist and not (path / "project.json").is_file():
            raise HttpError(404, f"project {project_id} not found")
        return path

    def list_projects(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.projects_dir.iterdir()) if self.projects_dir.exists() else []:
            meta = p / "project.json"
            if meta.is_file():
                out.append(json.loads(meta.read_text(encoding="utf-8")))
        return out

    def read_meta(self, project_id: str) -> dict[str, Any]:
        return json.loads((self.project_dir(project_id) / "project.json").read_text(encoding="utf-8"))

    def write_meta(self, project_id: str, meta: dict[str, Any]) -> None:
        with self._lock:
            meta["updatedAt"] = utc_now()
            self.write_json(self.project_dir(project_id, must_exist=False) / "project.json", meta)

    def write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(_jsonable(payload), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def append_history(self, project_id: str, event: dict[str, Any]) -> None:
        path = self.project_dir(project_id) / "sync" / "history.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": utc_now(), **_jsonable(event)}, ensure_ascii=False, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

JOB_ID_RE = re.compile(r"^[a-z]+(?:-[a-z]+)?-[0-9]{8}t[0-9]{12}z-[0-9a-f]{6}$")
ONECLICK_STAGES = ("validate", "build", "preflight", "apply", "verify", "confirm", "guided")


class JobStageFailed(Exception):
    """A pipeline stage refused to continue; ``detail`` is shown next to that stage."""

    def __init__(self, message: str, detail: Any = None):
        super().__init__(message)
        self.detail = detail


class JobContext:
    """Stage bookkeeping for one running job; every transition is persisted immediately."""

    def __init__(self, runner: "JobRunner", project_id: str, job: dict[str, Any]):
        self.runner = runner
        self.project_id = project_id
        self.job = job
        self.path = runner.jobs_dir(project_id) / f"{job['id']}.json"

    def save(self) -> None:
        self.runner.store.write_json(self.path, self.job)

    def _stage(self, stage_id: str) -> dict[str, Any]:
        for stage in self.job["stages"]:
            if stage["id"] == stage_id:
                return stage
        raise KeyError(stage_id)

    def begin(self, stage_id: str) -> None:
        self._stage(stage_id).update(status="running", startedAt=utc_now())
        self.job["currentStage"] = stage_id
        self.save()

    def done(self, stage_id: str, detail: Any = None, *, note: str | None = None) -> None:
        stage = self._stage(stage_id)
        stage.update(status="succeeded", finishedAt=utc_now())
        if detail is not None:
            stage["detail"] = _jsonable(detail)
        if note:
            stage["note"] = note
        self.save()

    def skip(self, stage_id: str, note: str) -> None:
        self._stage(stage_id).update(status="skipped", finishedAt=utc_now(), note=note)
        self.save()

    def fail_running(self, error: str, detail: Any = None) -> None:
        for stage in self.job["stages"]:
            if stage["status"] == "running":
                stage.update(status="failed", finishedAt=utc_now(), error=error)
                if detail is not None:
                    stage["detail"] = _jsonable(detail)
        self.save()

    def finish(self, status: str, *, result: Any = None, error: str | None = None) -> None:
        if status != "succeeded":
            for stage in self.job["stages"]:
                if stage["status"] == "pending":
                    stage["status"] = "not-run"
        self.job.update(status=status, finishedAt=utc_now(), result=_jsonable(result), error=error, currentStage=None)
        self.save()


class JobRunner:
    """Background jobs for operations that outlive the gateway proxy timeout.

    The KiroCrew gateway gives a proxied request 30 seconds, while build + preflight + S3 upload +
    SSM apply/commit take minutes. Those run here on a daemon thread and the UI polls the job file
    ``<project>/jobs/<id>.json``, rewritten atomically at every stage transition. At most one job
    runs per project. A job still marked running when the backend starts was cut off by a restart;
    it is recorded as ``interrupted`` and never silently resumed, because the remote side may or
    may not have acted.
    """

    def __init__(self, store: Store):
        self.store = store
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}
        self._recover_interrupted()

    def jobs_dir(self, project_id: str) -> Path:
        return self.store.project_dir(project_id) / "jobs"

    def _recover_interrupted(self) -> None:
        if not self.store.projects_dir.exists():
            return
        for path in sorted(self.store.projects_dir.glob("*/jobs/*.json")):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("status") != "running":
                continue
            for stage in job.get("stages", []):
                if stage.get("status") == "running":
                    stage["status"] = "interrupted"
                elif stage.get("status") == "pending":
                    stage["status"] = "not-run"
            job.update(status="interrupted", finishedAt=utc_now(), currentStage=None,
                       error="the app backend restarted while this job was running; check the Workshop target status, then run it again")
            self.store.write_json(path, job)

    def active(self, project_id: str) -> str | None:
        with self._lock:
            return self._active.get(project_id)

    def get(self, project_id: str, job_id: str) -> dict[str, Any]:
        if not JOB_ID_RE.match(job_id):
            raise HttpError(400, "invalid job id")
        path = self.jobs_dir(project_id) / f"{job_id}.json"
        if not path.is_file():
            raise HttpError(404, f"job {job_id} not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def latest(self, project_id: str, kind: str) -> dict[str, Any] | None:
        jobs = sorted(self.jobs_dir(project_id).glob(f"{kind}-[0-9]*.json"))  # "direct" is not "direct-cleanup"
        return json.loads(jobs[-1].read_text(encoding="utf-8")) if jobs else None

    def start(self, project_id: str, kind: str, stages: tuple[str, ...], work: Callable[[JobContext], Any], *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.store.project_dir(project_id)  # 404 before anything is written
        with self._lock:
            running = self._active.get(project_id)
            if running:
                raise HttpError(409, f"job {running} is still running for this project; wait for it to finish", jobId=running)
            job_id = f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dt%H%M%S%fz')}-{secrets.token_hex(3)}"
            job = {"id": job_id, "project": project_id, "kind": kind, "status": "running", "createdAt": utc_now(),
                   "finishedAt": None, "currentStage": None, "params": params or {}, "result": None, "error": None,
                   "stages": [{"id": stage, "status": "pending"} for stage in stages]}
            ctx = JobContext(self, project_id, job)
            ctx.save()
            snapshot = json.loads(json.dumps(job))
            self._active[project_id] = job_id
            threading.Thread(target=self._run, args=(ctx, work), name=f"wc-{job_id}", daemon=True).start()
        return snapshot

    def _run(self, ctx: JobContext, work: Callable[[JobContext], Any]) -> None:
        try:
            result = work(ctx)
        except JobStageFailed as exc:
            ctx.fail_running(str(exc), exc.detail)
            ctx.finish("failed", error=str(exc))
        except HttpError as exc:
            ctx.fail_running(str(exc), {k: v for k, v in exc.payload.items() if k != "error"} or None)
            ctx.finish("failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - recorded on the job, traceback logged locally only
            sys.stderr.write(traceback.format_exc())
            message = f"internal error: {type(exc).__name__}: {exc}"
            ctx.fail_running(message)
            ctx.finish("failed", error=message)
        else:
            ctx.finish("succeeded", result=result)
        finally:
            with self._lock:
                self._active.pop(ctx.project_id, None)


BLANK_SCENARIO = """\
# Scenario Pack — the single source of business truth for one customer scenario.
# Every fact, knowledge document, tool and golden case carries provenance. A customer pack needs
# customer_confirmed (+ confirmedBy/confirmedAt/confirmationRef) on blocking items before a formal build;
# sa_synthetic is for SA-supplied safe synthetic data; ai_draft/pending never enter a formal pack.
schemaVersion: 1
id: {id}
displayName: "{display_name}"
description: >
  TODO: one paragraph the SA writes after the intake conversation (who the agent serves, what it must do,
  what it must never do).
packKind: {pack_kind}
language: en

namespace:
  agentName: {agentName}
  toolTargetName: {toolTargetName}
  gatewayName: {gatewayName}
  knowledgeBaseName: {knowledgeBaseName}
  kbPrefix: {kbPrefix}
  ssmParameterPrefix: {ssmParameterPrefix}
  lambdaFunctionName: {lambdaFunctionName}

agent:
  audience: TODO who asks the questions
  purpose: TODO what the agent does for them
  scope: []
  outOfScope: []
  roles: []
  handoffConditions: []
  prohibitedBehaviors: []

facts: []

knowledge:
  documents: []
  noisePlan:
    enabled: true
    rationale: TODO why dirty passages should co-retrieve with policy text in this scenario.

tools: []

skills: []

prompts:
  baselineFile: agent/baseline-prompt.md
  optimizationCandidateFile: agent/optimization-candidate.md

evaluation:
  retrievalToolName: retrieve_policy
  judgeModel: us.amazon.nova-2-lite-v1:0
  goldenSet: []

labs:
  observations: []
"""


BUILD_OUTPUTS = ("pack", "instructor", "release", "checksums.json", "derivation.json")


def _promote_build(out: Path, staging: Path) -> None:
    """Swap a complete, verified staging build into ``out``; put the previous build back on failure."""
    retired = out / f".retired-{secrets.token_hex(4)}"
    retired.mkdir()
    try:
        for name in BUILD_OUTPUTS:
            if (out / name).exists():
                os.replace(out / name, retired / name)
        for name in BUILD_OUTPUTS:
            if (staging / name).exists():
                os.replace(staging / name, out / name)
    except BaseException:
        for name in BUILD_OUTPUTS:
            if (retired / name).exists():
                target = out / name
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                os.replace(retired / name, target)
        raise
    finally:
        shutil.rmtree(retired, ignore_errors=True)


def _golden_set(data: dict[str, Any]) -> list[dict[str, Any]]:
    return list((data.get("evaluation") or {}).get("goldenSet", []) or [])


def _gen() -> Any:
    """``routes.py`` loaded by path, once (it needs no aiohttp/KiroCrew until ``register_routes``).

    The build ``sourceHash``, draft validation's ``packDigest`` and the generation binding
    (``sourcePackDigest``) must agree byte for byte, so this process reuses the routes
    implementation instead of keeping a copy.
    """
    return _load_once("workshop_customizer_app_routes", ROUTES_PY, _ROUTES_LOCK)


def _load_once(name: str, path: Path, lock: threading.Lock) -> Any:
    """``path`` loaded as module ``name``, once, under ``lock`` (one lock a module, so a slow load — the console's —
    holds up only its own callers)."""
    with lock:
        if path not in _LOADED:
            import importlib.util

            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _LOADED[path] = module
        return _LOADED[path]


def pack_digest(pdir: Path) -> str:
    """Digest of every pack source file of a project (scenario + referenced and stray files).

    App-managed directories (``routes.APP_MANAGED_DIRS``), project.json and Python caches are
    excluded. Builds record it as ``sourceHash``; draft validation binds build/validate.json to it
    as ``packDigest``; generation records bind to it as ``sourcePackDigest``. One implementation:
    ``routes.pack_digest``.
    """
    return _gen().pack_digest(pdir)


def _file_sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _all_items(data: dict[str, Any]):
    """Every provenance-bearing item: facts, tools, knowledge documents, golden cases, the guide narrative."""
    for item in data.get("facts", []) or []:
        if isinstance(item, dict):
            yield item
    for item in data.get("tools", []) or []:
        if isinstance(item, dict):
            yield item
    for item in (data.get("knowledge") or {}).get("documents", []) or []:
        if isinstance(item, dict):
            yield item
    for item in _golden_set(data):
        if isinstance(item, dict):
            yield item
    narrative = _guide_narrative(data)
    if narrative is not None:
        yield narrative


GUIDE_AUDIENCES = ("student", "instructor")
INSTRUCTOR_BUNDLE_WARNING = "contains holdout cases and expected answers; never distribute it to participants"


def _guide_narrative(data: dict[str, Any]) -> dict[str, Any] | None:
    labs = data.get("labs") if isinstance(data.get("labs"), dict) else {}
    narrative = labs.get("guide")
    return narrative if isinstance(narrative, dict) else None


# ---------------------------------------------------------------------------
# Service layer (engine calls)
# ---------------------------------------------------------------------------


class Service:
    def __init__(self, data_dir: Path, *, home: Path | None = None, clients_factory: Callable[[Any], Any] | None = None):
        self.store = Store(data_dir)
        self.home = home or resolve_home(data_dir)
        self.engine = None
        self.engine_error = "" if self.home else "checkout not found (WORKSHOP_CUSTOMIZER_HOME / data/config.json homeDir)"
        if self.home:
            try:
                self.engine = load_engine(self.home)
            except ImportError as exc:  # gateway interpreter without pyyaml/jsonschema/boto3
                self.engine_error = f"engine dependency missing in {sys.executable}: {exc}"
        self._clients_factory = clients_factory
        self._lock = threading.Lock()
        self.jobs = JobRunner(self.store)

    # -- helpers -----------------------------------------------------------
    def _require_engine(self):
        if not self.engine:
            raise HttpError(503, "engine unavailable: " + (self.engine_error or "set WORKSHOP_CUSTOMIZER_HOME to the workshop-customizer checkout"), hint="data/config.json {\"homeDir\": \"...\"} also works; the checkout's .venv must have pyyaml, jsonschema, boto3")
        return self.engine

    def template_lock(self) -> dict[str, Any]:
        return json.loads((self.home / "template-lock.json").read_text(encoding="utf-8"))  # type: ignore[union-attr]

    def _clients(self, cfg):
        if self._clients_factory:
            return self._clients_factory(cfg)
        try:
            return self._require_engine()["sync"].make_clients(cfg)
        except HttpError:
            raise
        except Exception as exc:  # noqa: BLE001 - botocore ProfileNotFound & friends: refuse, never guess
            raise HttpError(409, f"AWS profile '{cfg.profile}' is not usable from this app: {type(exc).__name__}: {exc}", hint="log in first (aws sso login / assume the temporary role) and name that profile in the target") from exc

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "app": APP_NAME, "engine": bool(self.engine), "engineError": self.engine_error or None, "python": sys.executable, "home": str(self.home) if self.home else None, "dataDir": str(self.store.data_dir), "projects": len(self.store.list_projects())}

    def _source_hash(self, project_id: str) -> str:
        return pack_digest(self.store.project_dir(project_id))

    @contextlib.contextmanager
    def _locked(self, project_id: str):
        """Hold the project's generation/.lock (shared with the in-gateway generation routes and the
        headless tool) around a write; 409 when another writer holds it for more than 10 s."""
        gen = _gen()
        pdir = self.store.project_dir(project_id)
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(gen.project_lock(pdir))
            except gen.ProjectBusy as exc:
                raise HttpError(409, str(exc)) from exc
            yield pdir

    def _count_edit(self, project_id: str, key: str) -> None:
        meta = self.store.read_meta(project_id)
        _gen().bump_edit_log(meta, key)
        self.store.write_meta(project_id, meta)

    def _invalidate_build(self, project_id: str) -> None:
        meta = self.store.read_meta(project_id)
        meta.update(lastValidation=None, lastBuild=None, status="truth-review")
        self.store.write_meta(project_id, meta)
        (self.store.project_dir(project_id) / "sync" / "pending-confirm.json").unlink(missing_ok=True)

    def _current_build(self, project_id: str) -> dict[str, Any]:
        meta = self.store.read_meta(project_id)
        path = self.store.project_dir(project_id) / "build" / "build.json"
        if not meta.get("lastBuild") or not path.is_file():
            raise HttpError(409, "build or rebuild the current scenario before using its release")
        summary = json.loads(path.read_text(encoding="utf-8"))
        if (summary.get("sourceHash") != self._source_hash(project_id)
                or summary.get("version") != meta["lastBuild"].get("version")):
            raise HttpError(409, "source files changed after the last build; rebuild the release")
        return summary

    # -- projects ----------------------------------------------------------
    def create_project(self, body: dict[str, Any]) -> dict[str, Any]:
        project_id = str(body.get("id", "")).strip()
        if not _gen().PROJECT_RE.match(project_id):
            raise HttpError(400, "id must be kebab-case (3-41 chars)")
        template = str(body.get("template", "blank"))
        if template not in TEMPLATES:
            raise HttpError(400, f"template must be one of {TEMPLATES}")
        pack_kind = str(body.get("packKind", "customer"))
        if pack_kind not in PACK_KINDS:
            raise HttpError(400, "packKind must be customer, reference or workshop")
        pdir = self.store.project_dir(project_id, must_exist=False)
        if pdir.exists():
            raise HttpError(409, f"project {project_id} already exists")
        pdir.mkdir(parents=True)
        display_name = str(body.get("displayName") or project_id).replace('"', "'")
        if template == "blank":
            # The generation loop's namespace, exactly (SPEC D6a: AWS name limits, no THELMA markers).
            namespace = _gen().derive_namespace(project_id)
            (pdir / "scenario.yaml").write_text(BLANK_SCENARIO.format(id=project_id, display_name=display_name, pack_kind=pack_kind, **namespace), encoding="utf-8")
            (pdir / "agent").mkdir()
            (pdir / "agent" / "baseline-prompt.md").write_text("# Baseline prompt\n\nTODO: the intentionally weak first prompt students start from.\n", encoding="utf-8")
            (pdir / "agent" / "optimization-candidate.md").write_text("# Optimization candidate\n\nTODO: the hardened prompt students compare against the baseline.\n", encoding="utf-8")
        else:
            src = self._require_engine() and (self.home / "scenarios" / template)  # type: ignore[operator]
            # scenario.yaml plus the files it references, never the template's extras (its generator
            # script, unreferenced schemas): a new project starts without orphans.
            import yaml

            shutil.copy2(src / "scenario.yaml", pdir / "scenario.yaml")
            template_data = yaml.safe_load((src / "scenario.yaml").read_text(encoding="utf-8")) or {}
            root = src.resolve()
            for rel in sorted(_gen()._referenced_files(template_data)):
                source = (src / rel).resolve()
                if root in source.parents and source.is_file():
                    (pdir / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, pdir / rel)
            text = (pdir / "scenario.yaml").read_text(encoding="utf-8")
            text = re.sub(r"^id: .*$", f"id: {project_id}", text, count=1, flags=re.M)
            text = re.sub(r"^packKind: .*$", f"packKind: {pack_kind}", text, count=1, flags=re.M)
            (pdir / "scenario.yaml").write_text(text, encoding="utf-8")
        meta = {
            "id": project_id,
            "displayName": display_name,
            "customer": str(body.get("customer", "")),
            "packKind": pack_kind,
            "template": template,
            "status": "intake",
            "createdAt": utc_now(),
            "target": None,
            "lastValidation": None,
            "lastBuild": None,
            "lastSync": None,
        }
        self.store.write_meta(project_id, meta)
        return meta

    def get_project(self, project_id: str) -> dict[str, Any]:
        meta = self.store.read_meta(project_id)
        pdir = self.store.project_dir(project_id)
        meta["scenarioYaml"] = (pdir / "scenario.yaml").read_text(encoding="utf-8")
        gen = _gen()
        meta["brief"] = gen.stored_brief(pdir)
        log = meta.get("editLog") if isinstance(meta.get("editLog"), dict) else {}
        meta["editLog"] = {key: int(log.get(key) or 0) for key in gen.EDIT_LOG_KEYS}
        meta.setdefault("materialsPolicy", None)
        return meta

    def put_scenario(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        text = body.get("yaml")
        if not isinstance(text, str) or not text.strip():
            raise HttpError(400, "body.yaml must be a non-empty string")
        import yaml  # engine dependency; present in the app venv

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise HttpError(400, f"YAML does not parse: {exc}") from exc
        if not isinstance(data, dict) or data.get("id") != project_id:
            raise HttpError(400, "scenario.id must equal the project id")
        with self._locked(project_id) as pdir:
            (pdir / "scenario.yaml").write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
            self._invalidate_build(project_id)
            meta = self.store.read_meta(project_id)
            pack_kind = data.get("packKind")
            if pack_kind in PACK_KINDS:
                # The engine gates read scenario.packKind, but the project list and Kiro regeneration read
                # meta.packKind; keep them in step when the SA edits packKind in the scenario editor.
                # The current UI tells the SA to switch kinds that way, so it is still accepted (P5 moves
                # it to POST /pack-kind), but never silently: every change lands in packKindHistory.
                if pack_kind != meta.get("packKind"):
                    self._record_pack_kind(meta, str(meta.get("packKind") or ""), pack_kind, None, via="scenario-editor")
                meta["packKind"] = pack_kind
            _gen().bump_edit_log(meta, "manualScenarioSaves")  # the zero-hand-edit evidence counts these
            self.store.write_meta(project_id, meta)
        return {"saved": True, "bytes": len(text.encode("utf-8"))}

    def scenario_view(self, project_id: str) -> dict[str, Any]:
        """The live scenario.yaml as JSON, plus the project's pack files (read-only, for the UI).

        GET /projects/{pid} already returns the YAML text; this is the same content parsed, so the
        UI can show teaching, prompts, tools and golden cases without a YAML parser. A file that
        does not parse is reported (``error``), never a 500.
        """
        import yaml

        pdir = self.store.project_dir(project_id)
        raw = (pdir / "scenario.yaml").read_bytes()
        data: Any = None
        error = None
        try:
            data = yaml.safe_load(raw.decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            error = f"scenario.yaml does not parse: {exc}"
        if error is None and not isinstance(data, dict):
            data, error = None, "scenario.yaml must be a mapping"
        gen = _gen()
        referenced = sorted(gen._referenced_files(data)) if isinstance(data, dict) else []
        orphans, untracked = gen.orphan_files(pdir, data) if isinstance(data, dict) else ([], [])
        return {"scenario": _jsonable(data), "error": error, "sha256": hashlib.sha256(raw).hexdigest(),
                "files": gen._project_files(pdir), "referenced": referenced, "orphans": orphans, "untracked": untracked}

    def _pack_file(self, project_id: str, rel: str) -> tuple[Path, Path]:
        """(project dir, target) of a pack-side file the SA may read or write.

        App-managed state (project.json, scenario.yaml, materials/, run/, generation/, build/, ...) is refused
        however the path spells it: letter case never matters (on a case-insensitive filesystem such as macOS
        APFS 'Materials/' IS 'materials/') and the check runs on the resolved path too, so a symlink inside the
        project cannot reach it either."""
        pdir = self.store.project_dir(project_id)
        if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel or ".." in Path(rel).parts:
            raise HttpError(400, "path must stay inside the project")
        gen = _gen()
        try:
            target = gen.pack_path(pdir, rel)  # the one confinement rule, shared with generation apply/revert
        except gen.GenerationError as exc:
            raise HttpError(400, str(exc)) from None
        return pdir, target

    MAX_FILE_VIEW_BYTES = 1_000_000

    def get_file(self, project_id: str, rel: str) -> dict[str, Any]:
        """One pack file's text (prompts, knowledge documents, skills), for review and editing."""
        _pdir, target = self._pack_file(project_id, rel)
        if not target.is_file():
            raise HttpError(404, f"file {rel} not found")
        raw = target.read_bytes()
        if len(raw) > self.MAX_FILE_VIEW_BYTES:
            raise HttpError(413, f"file {rel} is larger than {self.MAX_FILE_VIEW_BYTES} bytes")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HttpError(415, f"file {rel} is not UTF-8 text") from exc
        return {"path": rel, "content": content, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    def put_file(self, project_id: str, rel: str, content: str) -> dict[str, Any]:
        _pdir, target = self._pack_file(project_id, rel)
        with self._locked(project_id):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            self._invalidate_build(project_id)
            self._count_edit(project_id, "manualFileSaves")
        return {"saved": rel, "bytes": len(content.encode("utf-8"))}

    @staticmethod
    def _record_pack_kind(meta: dict[str, Any], old: str, new: str, reason: str | None, *, via: str) -> None:
        history = meta.get("packKindHistory") if isinstance(meta.get("packKindHistory"), list) else []
        meta["packKindHistory"] = [*history, {"from": old or None, "to": new, "at": utc_now(), "reason": reason, "via": via}][-50:]

    def switch_pack_kind(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """The audited packKind switch (designs[4]): acknowledged, with a 10-500 character reason kept
        in packKindHistory; rewrites scenario.packKind and invalidates the build."""
        import yaml

        pack_kind = body.get("packKind")
        if pack_kind not in PACK_KINDS:
            raise HttpError(400, "packKind must be customer, reference or workshop")
        if body.get("acknowledged") is not True:
            raise HttpError(400, "acknowledged must be true: a pack kind changes which provenance gate the release passes")
        reason = body.get("reason")
        if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 500:
            raise HttpError(400, "reason must explain the switch in 10-500 characters")
        with self._locked(project_id) as pdir:
            path = pdir / "scenario.yaml"
            text = path.read_text(encoding="utf-8")
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise HttpError(409, f"scenario.yaml does not parse: {exc}") from exc
            if not isinstance(data, dict):
                raise HttpError(409, "scenario.yaml must be a mapping")
            meta = self.store.read_meta(project_id)
            old = str(data.get("packKind") or meta.get("packKind") or "")
            if old == pack_kind and meta.get("packKind") == pack_kind:
                raise HttpError(409, f"the pack kind is already {pack_kind}")
            # Rewrite only the packKind line when possible, so the SA's YAML layout and comments survive.
            patched = re.sub(r"^packKind:.*$", f"packKind: {pack_kind}", text, count=1, flags=re.M)
            data["packKind"] = pack_kind
            if patched != text and yaml.safe_load(patched) == data:
                tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
                try:
                    tmp.write_text(patched, encoding="utf-8")
                    tmp.replace(path)
                finally:
                    tmp.unlink(missing_ok=True)
            else:
                self._write_scenario_data(path, data)
            self._invalidate_build(project_id)
            meta = self.store.read_meta(project_id)
            self._record_pack_kind(meta, old, pack_kind, reason.strip(), via="pack-kind")
            meta["packKind"] = pack_kind
            self.store.write_meta(project_id, meta)
        return {"packKind": pack_kind, "previous": old or None, "packKindHistory": meta["packKindHistory"]}

    PRUNE_KEEP = 5

    def prune_files(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Orphan files of the project (unreferenced pack sources, as apply computes them).

        ``dryRun`` (the default) only lists them; ``{"dryRun": false, "acknowledged": true}`` removes
        them from the pack into ``generation/pruned/<stamp>/`` (app-managed, outside the digest; the
        newest five batches are kept) so a mistaken prune stays recoverable. Untracked files are
        listed, never touched.
        """
        import yaml

        dry_run = body.get("dryRun", True)
        if not isinstance(dry_run, bool):
            raise HttpError(400, "dryRun must be a boolean")
        if not dry_run and body.get("acknowledged") is not True:
            raise HttpError(400, "acknowledged must be true: confirm you reviewed the orphan files that will be removed")
        gen = _gen()
        with self._locked(project_id) as pdir:
            try:
                data = yaml.safe_load((pdir / "scenario.yaml").read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise HttpError(409, f"scenario.yaml does not parse: {exc}") from exc
            if not isinstance(data, dict):
                raise HttpError(409, "scenario.yaml must be a mapping")
            orphans, untracked = gen.orphan_files(pdir, data)
            if dry_run or not orphans:
                return {"dryRun": dry_run, "orphans": orphans, "untracked": untracked, "deleted": [], "trash": None}
            job = gen._running_job(pdir)
            if job:
                raise HttpError(409, f"background job {job} is running for this project; wait for it to finish")
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3)
            trash = pdir / "generation" / "pruned" / stamp
            moved: list[tuple[Path, Path]] = []
            try:
                for rel in orphans:
                    source = pdir / rel
                    target = trash / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, target)
                    moved.append((source, target))
            except OSError:
                for source, target in reversed(moved):
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, source)
                raise
            gen._remove_empty_dirs(pdir, orphans)
            batches = sorted((p for p in trash.parent.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
            for old_batch in batches[self.PRUNE_KEEP:]:
                shutil.rmtree(old_batch, ignore_errors=True)
            self._invalidate_build(project_id)
        return {"dryRun": False, "orphans": orphans, "untracked": untracked, "deleted": orphans,
                "trash": trash.relative_to(pdir).as_posix()}

    def _confirmation_fields(self, body: dict[str, Any]) -> tuple[str, str, str]:
        provenance = str(body.get("provenance", "customer_confirmed"))
        if provenance not in ("customer_confirmed", "sa_synthetic"):
            raise HttpError(400, "provenance must be customer_confirmed or sa_synthetic")
        confirmed_by = str(body.get("confirmedBy", "")).strip()
        confirmed_ref = str(body.get("confirmationRef") or body.get("confirmedRef") or "").strip()
        if provenance == "customer_confirmed" and not (confirmed_by and confirmed_ref):
            raise HttpError(400, "customer_confirmed needs confirmedBy and confirmationRef (meeting note / email / ticket)")
        simulated = self._require_engine()["scenario"].SIMULATED_CONFIRMATION_RE
        if provenance == "customer_confirmed" and (simulated.search(confirmed_by) or simulated.search(confirmed_ref)):
            # SPEC D12: nothing may fabricate a customer confirmation (the engine gate refuses it too).
            raise HttpError(400, "a simulated confirmation is never recorded as customer_confirmed; "
                                 "confirm with the customer's own reference, or review the item as sa_synthetic")
        return provenance, confirmed_by, confirmed_ref

    def _material_ids(self, project_id: str) -> set[str]:
        return {str(r.get("id")) for r in _gen().read_materials(self.store.project_dir(project_id))}

    def _origin_from_body(self, project_id: str, value: Any) -> dict[str, Any]:
        """A validated origin label; cited material ids must exist in the project's materials."""
        kinds = self._require_engine()["scenario"].ORIGIN_KINDS
        if not isinstance(value, dict) or value.get("kind") not in kinds:
            raise HttpError(400, f"origin must be an object whose kind is one of {', '.join(kinds)}")
        origin: dict[str, Any] = {"kind": value["kind"]}
        materials = value.get("materials")
        if materials is not None:
            if (not isinstance(materials, list) or len(materials) > 20 or len(set(materials)) != len(materials)
                    or not all(isinstance(m, str) and _gen().MATERIAL_ID_RE.fullmatch(m) for m in materials)):
                raise HttpError(400, "origin.materials must be a list of at most 20 unique material ids")
            unknown = sorted(set(materials) - self._material_ids(project_id))
            if unknown:
                raise HttpError(400, "origin cites unknown materials: " + ", ".join(unknown))
            if materials:
                origin["materials"] = list(materials)
        if origin["kind"] == "customer_material" and not origin.get("materials"):
            raise HttpError(400, "origin kind customer_material must cite at least one uploaded material")
        note = value.get("note")
        if note is not None:
            if not isinstance(note, str) or not 1 <= len(note.strip()) <= 500:
                raise HttpError(400, "origin.note must be 1-500 characters")
            origin["note"] = note.strip()
        return origin

    @staticmethod
    def _has_origin(item: dict[str, Any]) -> bool:
        origin = item.get("origin")
        return isinstance(origin, dict) and origin.get("kind") in ("customer_material", "sa_authored", "teaching_design")

    @staticmethod
    def _find_items(data: dict[str, Any], item_ids: list[str]) -> dict[str, dict[str, Any]]:
        wanted = set(item_ids)
        found: dict[str, dict[str, Any]] = {}
        for item in _all_items(data):
            key = item.get("id") or item.get("name")
            if key in wanted:
                found[key] = item  # last match wins, as the single-item confirm always did
        return found

    @staticmethod
    def _stamp_confirmation(data: dict[str, Any], hit: dict[str, Any], provenance: str, confirmed_by: str, confirmed_ref: str) -> None:
        hit["provenance"] = provenance
        is_document = any(hit is d for d in (data.get("knowledge") or {}).get("documents", []) or [])
        for key in ("confirmedBy", "confirmedAt", "confirmationRef", "confirmedRef"):
            hit.pop(key, None)
        if provenance == "customer_confirmed" and not is_document:  # documents carry provenance only (schema)
            hit["confirmedBy"] = confirmed_by
            hit["confirmedAt"] = utc_now()
            hit["confirmationRef"] = confirmed_ref

    @staticmethod
    def _write_scenario_data(path: Path, data: dict[str, Any]) -> None:
        """Write the scenario through a temp file + rename so a failed write never leaves it half-written."""
        import yaml

        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    def confirm_item(self, project_id: str, item_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Mark a fact/tool/document/golden case with confirmed provenance (customer_confirmed or sa_synthetic).

        Optional ``origin`` labels the item; optional ``materialId`` (customer_confirmed only) names the
        uploaded material the customer confirmed it from and is appended to the confirmationRef. In a
        workshop pack an sa_synthetic item must carry an origin (its own or ``origin``), except the guide
        narrative: teaching prose whose schema has no origin, so it never takes one.
        """
        if not ITEM_ID_RE.match(item_id):
            raise HttpError(400, "invalid item id")
        provenance, confirmed_by, confirmed_ref = self._confirmation_fields(body)
        origin = self._origin_from_body(project_id, body["origin"]) if body.get("origin") is not None else None
        material_id = body.get("materialId")
        if material_id is not None:
            if provenance != "customer_confirmed":
                raise HttpError(400, "materialId records the source of a customer confirmation; use origin for sa_synthetic")
            if not isinstance(material_id, str) or material_id not in self._material_ids(project_id):
                raise HttpError(400, "materialId must name an uploaded material of this project")
            confirmed_ref = f"{confirmed_ref} (material {material_id})"
        import yaml

        with self._locked(project_id) as pdir:
            path = pdir / "scenario.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            hit = self._find_items(data, [item_id]).get(item_id)
            if hit is None:
                raise HttpError(404, f"no fact/tool/golden case with id {item_id}")
            narrative = item_id == _gen().GUIDE_NARRATIVE_ID  # labs.guide's fixed id: teaching prose, sa_synthetic only
            if narrative and provenance != "sa_synthetic":
                raise HttpError(400, "guide narrative is teaching prose; review it as sa_synthetic")
            if narrative and origin is not None:
                # labs.guide has no origin field (schema): it is teaching prose in every pack kind.
                raise HttpError(400, "the guide narrative is teaching prose and carries no origin; review it without one")
            if origin is not None:
                hit["origin"] = origin
            if (provenance == "sa_synthetic" and data.get("packKind") == "workshop" and not narrative
                    and not self._has_origin(hit)):
                raise HttpError(400, "a workshop pack labels every synthetic setting with its origin "
                                     "(customer_material, sa_authored or teaching_design); pass origin")
            self._stamp_confirmation(data, hit, provenance, confirmed_by, confirmed_ref)
            self._write_scenario_data(path, data)
            self._invalidate_build(project_id)
        return {"itemId": item_id, "provenance": provenance}

    def confirm_items(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Batch SA review: mark many items sa_synthetic in one all-or-nothing write.

        Only sa_synthetic is accepted, and never over a customer confirmation (that would downgrade
        it). A customer pack still needs customer_confirmed (with its own confirmedBy/confirmationRef)
        on blocking items; in a workshop pack every item must end up labeled with an origin (its own
        or ``origin``, which fills items that have none) — except the guide narrative, which has no
        origin field and never gets one.
        """
        if body.get("acknowledged") is not True:
            raise HttpError(400, "acknowledged must be true: confirm you reviewed every selected item")
        if str(body.get("provenance", "sa_synthetic")) != "sa_synthetic":
            raise HttpError(400, "batch review only records sa_synthetic; confirm customer items one by one with their reference")
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > 500:
            raise HttpError(400, "ids must be a list of 1-500 item ids")
        if any(not isinstance(i, str) or not ITEM_ID_RE.match(i) for i in ids) or len(set(ids)) != len(ids):
            raise HttpError(400, "ids must be unique, valid item ids")
        origin = self._origin_from_body(project_id, body["origin"]) if body.get("origin") is not None else None
        import yaml

        with self._locked(project_id) as pdir:
            path = pdir / "scenario.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            found = self._find_items(data, ids)
            missing = [i for i in ids if i not in found]
            if missing:
                raise HttpError(404, f"no fact/tool/golden case with id: {', '.join(missing)}")
            confirmed = [i for i in ids if found[i].get("provenance") == "customer_confirmed"]
            if confirmed:
                raise HttpError(409, "batch review would downgrade a customer confirmation: " + ", ".join(confirmed), itemIds=confirmed)
            # The guide narrative is teaching prose with no origin field (schema): never required, never written.
            guide_id = _gen().GUIDE_NARRATIVE_ID
            if data.get("packKind") == "workshop" and origin is None:
                unlabeled = [i for i in ids if i != guide_id and not self._has_origin(found[i])]
                if unlabeled:
                    raise HttpError(400, "a workshop pack labels every synthetic setting with its origin; pass origin "
                                         "or review these items one by one: " + ", ".join(unlabeled), itemIds=unlabeled)
            for item_id in ids:
                if origin is not None and item_id != guide_id and not self._has_origin(found[item_id]):
                    found[item_id]["origin"] = dict(origin)
                self._stamp_confirmation(data, found[item_id], "sa_synthetic", "", "")
            self._write_scenario_data(path, data)
            self._invalidate_build(project_id)
        return {"confirmed": len(ids), "provenance": "sa_synthetic", "itemIds": list(ids)}

    def list_items(self, project_id: str) -> dict[str, Any]:
        """Every provenance-bearing item with its kind, class and origin, for the review UI (read-only)."""
        import yaml

        sc = self._require_engine()["scenario"]
        path = self.store.project_dir(project_id) / "scenario.yaml"
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise HttpError(409, f"scenario.yaml does not parse: {exc}") from exc
        if not isinstance(data, dict):
            raise HttpError(409, "scenario.yaml must be a mapping")
        groups = (
            ("fact", data.get("facts", []) or []),
            ("tool", data.get("tools", []) or []),
            ("document", (data.get("knowledge") or {}).get("documents", []) or []),
            ("golden", list(_golden_set(data))),
            ("narrative", [n for n in [_guide_narrative(data)] if n is not None]),
        )
        label_keys = ("title", "statement", "claim", "text", "question", "query", "prompt", "input", "description", "summary", "path")
        items: list[dict[str, Any]] = []
        for kind, group in groups:
            for item in group:
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id") or item.get("name")
                if not isinstance(item_id, str):
                    continue
                keys = ("tagline", "scenarioIntro", "memoryLesson", "retrievalContrast") if kind == "narrative" else label_keys
                label = next((str(item[k]).strip() for k in keys if isinstance(item.get(k), (str, int, float)) and str(item[k]).strip()),
                             "guide narrative" if kind == "narrative" else "")
                row = {
                    "id": item_id,
                    "kind": kind,
                    "provenance": str(item.get("provenance") or ""),
                    "label": label[:200],
                    "confirmable": bool(ITEM_ID_RE.match(item_id)),
                    "class": sc.item_class(item),
                    "origin": item.get("origin") if isinstance(item.get("origin"), dict) else None,
                    "criticality": ("blocking" if kind == "tool" or (kind == "document" and data.get("packKind") in ("customer", "workshop"))
                                    else "advisory" if kind in ("document", "narrative") else item.get("criticality", "blocking")),
                }
                if kind == "golden":
                    row.update(category=item.get("category"), set=item.get("set"))
                if kind == "document":
                    row["noise"] = bool(item.get("noise"))
                items.append(row)
        try:
            classes, anchors = sc.class_summary(data), sc.customer_anchors(data)
        except Exception:  # noqa: BLE001 - a half-edited scenario still lists its items
            classes, anchors = {}, {}
        return {"packKind": data.get("packKind"), "items": items, "classes": classes,
                "anchors": {"required": data.get("packKind") == "workshop", **anchors,
                            "missing": [c for c in ("normal", "boundary", "prohibited") if not anchors.get(c)]},
                "anchorCandidates": self._anchor_candidates(project_id)}

    def _anchor_candidates(self, project_id: str) -> dict[str, list[str]] | None:
        """Kiro's anchor suggestions from the last applied generation (the SA confirms those first)."""
        last = self.store.read_meta(project_id).get("lastGeneration") or {}
        gid = str(last.get("id") or "")
        path = self.store.data_dir / "generations" / f"{gid}.json"
        if not _gen().GENERATION_RE.fullmatch(gid) or not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        result = record.get("result") if isinstance(record, dict) else None
        return result.get("anchorCandidates") if isinstance(result, dict) else None

    # -- customer materials ----------------------------------------------
    def _materials_index(self, pdir: Path) -> list[dict[str, Any]]:
        return _gen().read_materials(pdir)

    def _write_materials_index(self, pdir: Path, records: list[dict[str, Any]]) -> None:
        self.store.write_json(pdir / "materials" / "index.json", {"schema": _gen().MATERIALS_INDEX_SCHEMA, "materials": records})

    def list_materials(self, project_id: str) -> dict[str, Any]:
        mat = self._require_engine()["materials"]
        pdir = self.store.project_dir(project_id)
        gen = _gen()
        records = self._materials_index(pdir)
        allocated = gen.allocate_materials(pdir, gen.material_budget("draft"), records=records)
        sent = "".join(m["text"] for m in allocated)
        meta = self.store.read_meta(project_id)
        return {
            "materials": records,
            "policy": meta.get("materialsPolicy"),
            "limits": {"maxFiles": mat.MAX_FILES, "maxBytes": mat.MAX_FILE_BYTES, "maxTotalBytes": mat.MAX_TOTAL_BYTES,
                       "types": sorted(mat.SUPPORTED)},
            "budget": {"mode": "draft", "totalChars": sum(m["totalChars"] for m in allocated),
                       "sentChars": sum(m["sentChars"] for m in allocated), "estTokens": gen.est_tokens(sent),
                       "perMaterial": {m["id"]: {"sentChars": m["sentChars"], "truncated": m["truncated"]} for m in allocated}},
        }

    def upload_material(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Store one customer material (JSON base64) with its extracted text; never a pack source.

        The upload must be acknowledged, of a supported type (magic bytes checked), within the size
        and count limits, and free of credential or national-id shapes (422, content never echoed).
        """
        mat = self._require_engine()["materials"]
        if body.get("acknowledged") is not True:
            raise HttpError(400, "acknowledged must be true: confirm you may store this material and send it to the Kiro model")
        use = str(body.get("generationUse") or "source")
        author = str(body.get("author") or "customer")
        notes = str(body.get("notes") or "")
        if use not in mat.GENERATION_USES:
            raise HttpError(400, f"generationUse must be one of {', '.join(mat.GENERATION_USES)}")
        if author not in mat.AUTHORS:
            raise HttpError(400, f"author must be one of {', '.join(mat.AUTHORS)}")
        if len(notes) > mat.MAX_NOTES_CHARS:
            raise HttpError(400, f"notes are limited to {mat.MAX_NOTES_CHARS} characters")
        name = mat.sanitize_name(body.get("name"))
        try:
            media = mat.media_type(name)
        except mat.MaterialError as exc:
            raise HttpError(exc.status, str(exc)) from exc
        content = body.get("contentBase64")
        if not isinstance(content, str) or not content:
            raise HttpError(400, "contentBase64 must carry the file content")
        if len(content) > (mat.MAX_FILE_BYTES * 4) // 3 + 8:
            raise HttpError(413, f"a material is limited to {mat.MAX_FILE_BYTES} bytes")
        try:
            raw = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HttpError(400, "contentBase64 is not valid base64") from exc
        if len(raw) > mat.MAX_FILE_BYTES:
            raise HttpError(413, f"a material is limited to {mat.MAX_FILE_BYTES} bytes")
        pdir = self.store.project_dir(project_id)
        self._material_capacity(pdir, raw)
        try:
            extraction = mat.extract(name, raw)  # outside the project lock: a PDF may take seconds
        except mat.MaterialError as exc:
            raise HttpError(exc.status, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - a malformed customer file is the SA's 400, never a 500
            raise HttpError(400, f"the material could not be read ({type(exc).__name__}); save it again or upload "
                                 "a DOCX/TXT export") from exc
        hits = mat.scan_sensitive(extraction.text)
        if hits:
            raise HttpError(422, "the material contains credential or personal-identifier shapes; remove them and upload again",
                            findings=[{"code": code, "line": line} for code, line in hits])
        contacts = mat.count_contacts(extraction.text)
        if contacts:
            extraction.warnings.append(f"{contacts} email/phone patterns; replace people with roles before generating")
        mid = "mat-" + hashlib.sha256(raw).hexdigest()[:12]
        record = {"id": mid, "name": name, "mediaType": media, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                  "uploadedAt": utc_now(), "author": author, "generationUse": use, "extraction": extraction.as_record(),
                  "notes": notes}
        with self._locked(project_id):
            records = self._material_capacity(pdir, raw)
            if any(r.get("id") == mid for r in records):
                raise HttpError(409, f"this material is already uploaded as {mid}", existingId=mid)
            raw_path = pdir / "materials" / "raw" / f"{mid}{mat.extension(name)}"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(raw)
            text_path = pdir / "materials" / "text" / f"{mid}.txt"
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(extraction.text, encoding="utf-8")
            self._write_materials_index(pdir, [*records, record])
        return record

    def _material_capacity(self, pdir: Path, raw: bytes) -> list[dict[str, Any]]:
        mat = self.engine["materials"]  # type: ignore[index]
        records = self._materials_index(pdir)
        if len(records) >= mat.MAX_FILES:
            raise HttpError(409, f"a project holds at most {mat.MAX_FILES} materials; delete one first")
        if sum(int(r.get("bytes") or 0) for r in records) + len(raw) > mat.MAX_TOTAL_BYTES:
            raise HttpError(409, f"a project's materials are limited to {mat.MAX_TOTAL_BYTES} bytes in total")
        return records

    def _material(self, pdir: Path, material_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not _gen().MATERIAL_ID_RE.fullmatch(material_id):
            raise HttpError(400, "invalid material id")
        records = self._materials_index(pdir)
        record = next((r for r in records if r.get("id") == material_id), None)
        if record is None:
            raise HttpError(404, f"material {material_id} not found")
        return records, record

    def update_material(self, project_id: str, material_id: str, body: dict[str, Any]) -> dict[str, Any]:
        mat = self._require_engine()["materials"]
        with self._locked(project_id) as pdir:
            records, record = self._material(pdir, material_id)
            if "generationUse" in body:
                if body["generationUse"] not in mat.GENERATION_USES:
                    raise HttpError(400, f"generationUse must be one of {', '.join(mat.GENERATION_USES)}")
                record["generationUse"] = body["generationUse"]
            if "author" in body:
                if body["author"] not in mat.AUTHORS:
                    raise HttpError(400, f"author must be one of {', '.join(mat.AUTHORS)}")
                record["author"] = body["author"]
            if "notes" in body:
                if not isinstance(body["notes"], str) or len(body["notes"]) > mat.MAX_NOTES_CHARS:
                    raise HttpError(400, f"notes are limited to {mat.MAX_NOTES_CHARS} characters")
                record["notes"] = body["notes"]
            self._write_materials_index(pdir, records)
        return record

    def delete_material(self, project_id: str, material_id: str) -> dict[str, Any]:
        import yaml

        with self._locked(project_id) as pdir:
            records, record = self._material(pdir, material_id)
            try:
                data = yaml.safe_load((pdir / "scenario.yaml").read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                data = {}
            referenced = sorted(key for _t, key, item in _gen()._items(data if isinstance(data, dict) else {})
                                if material_id in ((item.get("origin") or {}).get("materials") or []))
            for path in (pdir / "materials" / "raw").glob(f"{material_id}.*"):
                path.unlink(missing_ok=True)
            (pdir / "materials" / "text" / f"{material_id}.txt").unlink(missing_ok=True)
            self._write_materials_index(pdir, [r for r in records if r is not record])
        return {"deleted": material_id, "referencedBy": referenced}

    def material_text(self, project_id: str, material_id: str, query: dict[str, str]) -> dict[str, Any]:
        pdir = self.store.project_dir(project_id)
        self._material(pdir, material_id)
        try:
            offset = max(0, int(query.get("offset", 0)))
            limit = min(20_000, max(1, int(query.get("limit", 20_000))))
        except ValueError as exc:
            raise HttpError(400, "offset and limit must be integers") from exc
        text = _gen().material_text(pdir, material_id)
        return {"id": material_id, "offset": offset, "limit": limit, "chars": len(text), "text": text[offset:offset + limit]}

    def record_material_approval(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Who approved sending this project's customer materials to the Kiro model, and their class."""
        ref = str(body.get("approvalRef") or "").strip()
        classification = str(body.get("dataClassification") or "")
        if not 3 <= len(ref) <= 500:
            raise HttpError(400, "approvalRef must name who approved it and where (3-500 characters)")
        if classification not in ("synthetic", "internal", "confidential"):
            raise HttpError(400, "dataClassification must be synthetic, internal or confidential")
        with self._locked(project_id):
            meta = self.store.read_meta(project_id)
            meta["materialsPolicy"] = {"approvalRef": ref, "dataClassification": classification, "recordedAt": utc_now()}
            self.store.write_meta(project_id, meta)
        return meta["materialsPolicy"]

    def calibrate(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Judge-noise calibration: paste the 13-judge-stability.sh output → evaluation.noiseBand[metric]."""
        eng = self._require_engine()
        cal = eng["calibration"]
        log = body.get("log")
        if not isinstance(log, str) or not log.strip():
            raise HttpError(400, "body.log must contain the judge-stability output (or scores, one per line)")
        metric = str(body.get("metric") or cal.DEFAULT_METRIC)
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,60}", metric):
            raise HttpError(400, "metric must be snake_case")
        try:
            stability = cal.calibrate(log, metric=metric)
        except cal.CalibrationError as exc:
            raise HttpError(400, str(exc)) from exc
        result = {"metric": metric, "runs": stability.runs, "mean": stability.mean, "std": stability.std, "spread": stability.spread, "noiseBand": stability.band, "verdict": stability.verdict, "applied": False}
        if not body.get("dryRun"):
            with self._locked(project_id) as pdir:
                try:
                    cal.apply_to_scenario(pdir / "scenario.yaml", stability, source=str(body.get("source") or "13-judge-stability.sh"))
                except cal.CalibrationError as exc:
                    raise HttpError(409, str(exc)) from exc
                result["applied"] = True
                self._invalidate_build(project_id)
                meta = self.store.read_meta(project_id)
                meta["lastValidation"] = None
                meta["lastBuild"] = None
                meta["lastCalibration"] = {k: result[k] for k in ("metric", "runs", "noiseBand", "verdict")} | {"at": utc_now()}
                self.store.write_meta(project_id, meta)
        return result

    def guided_run(self, project_id: str) -> dict[str, Any]:
        """Create or read the durable 00→13 run state bound to the current release."""
        self._require_engine()
        build = self._current_build(project_id)
        from workshop_customizer.guided_run_store import GuidedRunStore

        store = GuidedRunStore(self.store.project_dir(project_id))
        try:
            state = store.create(
                project_id=project_id,
                release_version=str(build["version"]),
                template_commit=str(build.get("templateCommit") or self.template_lock()["template"]["commit"]),
            )
        except ValueError as exc:
            raise HttpError(409, str(exc)) from exc
        if state["releaseVersion"] != str(build["version"]):
            raise HttpError(409, "the Guided Run is bound to a different release; reset it before running the rebuilt release")
        return state

    def reset_guided_run(self, project_id: str, *, from_step: str | None = None) -> dict[str, Any]:
        from workshop_customizer.guided_run_store import GuidedRunStore

        with self._lock:
            build = self._current_build(project_id)
            try:
                state = GuidedRunStore(self.store.project_dir(project_id)).reset(
                    project_id=project_id, release_version=build["version"],
                    template_commit=build["templateCommit"], from_step=from_step,
                )
            except ValueError as exc:
                raise HttpError(409, str(exc)) from exc
            # The reset archived run/rehearsal.json (kept in run/history/<id>/): its meta mirror goes with it,
            # so project.json never reports a readyForClass the current run no longer has.
            meta = self.store.read_meta(project_id)
            if meta.get("lastRehearsal") is not None:
                meta["lastRehearsal"] = None
                self.store.write_meta(project_id, meta)
            return state

    def _guided_execution_context(self, project_id: str):
        """Return state plus a verified, synced EC2 target; never infer a remote target."""
        from workshop_customizer.guided_execution import DOCUMENT_NAME
        from workshop_customizer.guided_run_store import GuidedRunStore

        state = self.guided_run(project_id)
        cfg, _release_project, meta = self._target_cfg(project_id)
        if meta.get("status") != "synced":
            raise HttpError(409, "sync and commit the verified release before running Workshop steps")
        preflight_path = self.store.project_dir(project_id) / "sync" / "last-preflight.json"
        if not preflight_path.is_file():
            raise HttpError(409, "run sync preflight first so the Workshop instance is verified")
        plan = json.loads(preflight_path.read_text(encoding="utf-8")).get("plan") or {}
        if (
            plan.get("version") != state["releaseVersion"]
            or plan.get("templateCommit") != state["templateCommit"]
            or plan.get("runDocumentName") != DOCUMENT_NAME
        ):
            raise HttpError(409, "the last preflight is not bound to this Guided Run release and fixed document")
        return GuidedRunStore(self.store.project_dir(project_id)), state, cfg, plan

    def _require_current_run_document(self, plan: dict[str, Any]) -> None:
        """Hard gate before dispatch: the add-ons stack runs exactly the checked-in RunStep document.

        An older document parses step output without case attribution, THELMA metrics or L1, so the
        evidence gates could never pass. Polling an already dispatched step is not gated (its result
        records the SSM DocumentVersion that produced it).
        """
        sync_mod = self._require_engine()["sync"]
        try:
            local_sha = sync_mod.run_document_sha256()
        except sync_mod.SyncError as exc:
            raise HttpError(409, str(exc)) from exc
        if plan.get("runDocumentSha256") != local_sha:
            raise HttpError(409, "the add-ons RunStep document recorded at the last preflight does not match "
                                 "sync/ssm/WorkshopCustomizerRunStep.json; redeploy sync/cfn/customizer-addons.json "
                                 "(tools/deploy_addons.py), then run preflight again")

    def start_guided_step(self, project_id: str, step_id: str | None = None) -> dict[str, Any]:
        """Dispatch exactly one runnable allow-listed guide step through the fixed SSM document."""
        from workshop_customizer.guided_execution import send_step
        from workshop_customizer.guided_run import STEP_BY_ID, next_runnable, runnable, start

        with self._lock:
            store, state, cfg, plan = self._guided_execution_context(project_id)
            self._require_current_run_document(plan)
            running = [sid for sid, step in state["steps"].items() if step["status"] == "running"]
            if running:
                raise HttpError(409, f"step {running[0]} is already running; poll it before starting another")
            selected = step_id or next_runnable(state["steps"])
            if selected is None:
                raise HttpError(409, "no runnable Workshop step remains")
            if selected not in STEP_BY_ID:
                raise HttpError(400, "step is not in the Workshop Guide allowlist")
            if not runnable(selected, state["steps"]):
                raise HttpError(409, f"step {selected} is not runnable; its dependency has not passed")
            try:
                command_id = send_step(
                    self._clients(cfg).ssm,
                    step_id=selected,
                    release_version=state["releaseVersion"],
                    instance_id=str(plan.get("instanceId") or ""),
                )
            except Exception as exc:  # noqa: BLE001 - SDK failures become fail-closed API errors
                raise HttpError(409, f"could not dispatch step {selected}: {type(exc).__name__}: {exc}") from exc
            start(selected, state["steps"], utc_now())
            state["steps"][selected].update(commandId=command_id, ssmStatus="Pending")
            state["currentStepId"] = selected
            state["status"] = "running"
            store.save(state)
            return state

    def _build_scenario(self, project_id: str) -> dict[str, Any]:
        """The scenario exactly as the current release was built (build/instructor/scenario-snapshot.*).

        The report and the evidence gates read this snapshot, never the live scenario.yaml, so they
        judge the run against the pack the Workshop actually ran (SPEC D8).
        """
        import yaml

        instructor = self.store.project_dir(project_id) / "build" / "instructor"
        for path in sorted(instructor.glob("scenario-snapshot.*")):
            if path.is_file():
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        raise HttpError(409, "the build has no scenario snapshot (build/instructor/scenario-snapshot.*); rebuild the release")

    def poll_guided_step(self, project_id: str) -> dict[str, Any]:
        """Poll the sole running command and atomically persist an intermediate or terminal result."""
        from workshop_customizer.guided_execution import poll_step
        from workshop_customizer.guided_report import build_report, step_evidence_errors
        from workshop_customizer.guided_run import finish

        with self._lock:
            store, state, cfg, plan = self._guided_execution_context(project_id)
            scenario = self._build_scenario(project_id)
            running = [sid for sid, step in state["steps"].items() if step["status"] == "running"]
            if len(running) != 1:
                raise HttpError(409, "exactly one Workshop step must be running before poll")
            step_id = running[0]
            step = state["steps"][step_id]
            command_id = str(step.get("commandId") or "")
            if not command_id:
                raise HttpError(409, f"running step {step_id} has no command id")
            try:
                result = poll_step(self._clients(cfg).ssm, command_id=command_id, instance_id=str(plan.get("instanceId") or ""))
            except Exception as exc:  # noqa: BLE001 - SDK failures become fail-closed API errors
                raise HttpError(409, f"could not poll step {step_id}: {type(exc).__name__}: {exc}") from exc
            step["ssmStatus"] = result["status"]
            if not result["terminal"]:
                store.save(state)
                return state
            # Which version of the fixed SSM document produced this result (live evidence, D7).
            step["documentVersion"] = result.get("documentVersion")
            if result["passed"]:
                missing = step_evidence_errors(step_id, result["outputs"], scenario)
                if missing:
                    result["passed"] = False
                    result["error"] = "Script exited successfully but required evidence is missing: " + "; ".join(missing)
            finished_at = utc_now()
            try:
                started = datetime.strptime(step["startedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                finished = datetime.strptime(finished_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                duration = (finished - started).total_seconds()
            except (TypeError, ValueError):
                duration = 0.0
            finish(
                step_id,
                state["steps"],
                passed=bool(result["passed"]),
                at=finished_at,
                duration=duration,
                summary=result["summary"],
                error=result["error"],
                outputs=result["outputs"],
            )
            state["currentStepId"] = None
            if not result["passed"]:
                state["status"] = "failed"
            elif all(item["status"] == "passed" for item in state["steps"].values()):
                state["status"] = "passed"
                state["report"] = build_report(state, scenario, generated_at=finished_at)
                self.store.write_json(self.store.project_dir(project_id) / "run" / "report.json", state["report"])
            else:
                state["status"] = "in_progress"
            store.save(state)
            return state

    def guided_report(self, project_id: str) -> dict[str, Any]:
        state = self.guided_run(project_id)
        if not state.get("report"):
            raise HttpError(409, "the final report is available only after all 15 Workshop steps pass")
        return state["report"]

    def rehearsal(self, project_id: str, *, persist: bool) -> dict[str, Any]:
        """The class verdict over this release's Guided Runs (engine ``rehearsal.build_rehearsal``).

        Read-only unless ``persist`` (POST): GET never creates the run state or writes a file, so it
        cannot race a poll; POST computes under the Guided Run lock and writes ``run/rehearsal.json``
        and ``meta.lastRehearsal`` (its mirror; every Guided Run reset archives the file and clears
        the mirror). Both read the build snapshot, the current run and the archived runs of the
        same release.
        """
        eng = self._require_engine()  # 503 before the engine import below can fail
        from workshop_customizer import rehearsal as rehearsal_mod
        from workshop_customizer.guided_run_store import GuidedRunStore

        def compute() -> dict[str, Any]:
            build = self._current_build(project_id)
            pdir = self.store.project_dir(project_id)
            store = GuidedRunStore(pdir)
            if not store.path.is_file():
                raise HttpError(409, "no Guided Run exists for this release yet; start it first")
            try:
                state = store.load()
            except (OSError, ValueError) as exc:
                raise HttpError(409, f"the Guided Run state cannot be read: {exc}") from exc
            if state["releaseVersion"] != str(build["version"]):
                raise HttpError(409, "the Guided Run is bound to a different release; reset it before rehearsing the rebuilt release")
            scenario = self._build_scenario(project_id)
            history = store.history_states(release_version=state["releaseVersion"], template_commit=state["templateCommit"])
            # readyForClass needs the release the run executed, intact: recompute its hashes (RELEASE.json).
            try:
                manifest = eng["render"].verify_release(Path(build["releaseDir"]))
                release_verified = str(manifest.get("version")) == str(state["releaseVersion"])
            except eng["render"].RenderError:
                release_verified = False
            return rehearsal_mod.build_rehearsal(
                state, scenario, history=history,
                documents=rehearsal_mod.build_documents(scenario, pdir / "build"),
                guides=rehearsal_mod.guide_status(pdir / "build"),
                release_verified=release_verified,
            )

        if not persist:
            return compute()
        with self._lock:
            result = compute()
            self.store.write_json(self.store.project_dir(project_id) / "run" / "rehearsal.json", result)
            meta = self.store.read_meta(project_id)
            meta["lastRehearsal"] = {"at": utc_now(), "generatedAt": result["generatedAt"], "releaseVersion": result["releaseVersion"],
                                     "verdict": result["verdict"], "readyForClass": result["readyForClass"]}
            self.store.write_meta(project_id, meta)
            return result

    # -- validate / build --------------------------------------------------
    def validate(self, project_id: str, *, dry_build: bool = True) -> dict[str, Any]:
        """Draft-mode validation: every finding is visible, even for an all-ai_draft draft.

        The scenario loads with ``enforce_gate=False``: schema and cross-reference errors stop here,
        but gate violations are reported separately (``gate``; unwaived ones make ``ok`` false) while
        the composed policy (``validate_scenario_policy(data, root)``) always runs. When requested and
        the policy passes, a dry build compiles and renders into a throwaway directory so output
        findings (holdout, residue, secrets) and render errors show up before a real build. The result
        is persisted to build/validate.json, bound to the scenario sha256 and the pack digest.
        """
        eng = self._require_engine()
        sc = eng["scenario"]
        pdir = self.store.project_dir(project_id)
        scenario_path = pdir / "scenario.yaml"
        result: dict[str, Any] = {
            "project": project_id, "at": utc_now(), "ok": False, "mode": "draft",
            "scenarioSha256": _file_sha256(scenario_path), "packDigest": pack_digest(pdir),
            "gate": [], "gateWaived": [], "schema": [], "schemaKind": None, "policy": [], "output": [], "render": [],
            "dryBuild": {"requested": bool(dry_build), "ran": False, "skipped": None, "seconds": 0.0},
        }
        scenario = None
        try:
            scenario = sc.load_scenario(scenario_path, enforce_gate=False)
        except sc.SchemaViolation as exc:
            result["schema"], result["schemaKind"] = [str(e) for e in exc.errors], "schema"
        except sc.CrossReferenceError as exc:
            result["schema"], result["schemaKind"] = [str(e) for e in exc.errors], "xref"
        except sc.ScenarioError as exc:
            result["schema"], result["schemaKind"] = [str(exc)], "load"
        except Exception as exc:  # noqa: BLE001 - surface loader crashes (YAML syntax, I/O) as findings
            result["schema"], result["schemaKind"] = [f"loader error: {exc}"], "load"

        blocking_gate = True
        policy_ok = False
        if scenario is None:
            result["dryBuild"]["skipped"] = "the scenario did not load"
        else:
            data = scenario.data
            violations = list(scenario.gate_violations)
            blocking_gate = any(not v.waived for v in violations)
            # `gate` keeps its historical meaning: every violation (waived ones flagged) when the gate blocks.
            result["gate"] = [_jsonable(v) for v in violations] if blocking_gate else []
            result["gateWaived"] = [_jsonable(v) for v in violations if v.waived]
            report = eng["validator"].validate_scenario_policy(data, root=scenario.root)
            result["policy"] = [_jsonable(f) for f in report.findings]
            policy_ok = report.ok
            golden = _golden_set(data)
            result["summary"] = {
                "facts": len(data.get("facts", [])),
                "tools": len(data.get("tools", [])),
                "documents": len((data.get("knowledge") or {}).get("documents", [])),
                "goldenCases": len(golden),
                "holdout": sum(1 for c in golden if c.get("set") == "holdout"),
                "pending": sum(1 for i in _all_items(data) if i.get("provenance") in ("pending", "ai_draft")),
            }
            if not dry_build:
                result["dryBuild"]["skipped"] = "not requested"
            elif not policy_ok:
                result["dryBuild"]["skipped"] = "policy errors"
            else:
                self._dry_build(pdir, scenario, result)
        result["workspace"] = self._workspace_findings(project_id, pdir, scenario.data) if scenario is not None else []
        if scenario is not None:
            sc_mod = eng["scenario"]
            result["classes"] = sc_mod.class_summary(scenario.data)
            result["anchors"] = sc_mod.provenance_policy(scenario.data)
        output_errors = sum(1 for f in result["output"] if f.get("severity") == "error")
        workspace_errors = sum(1 for f in result["workspace"] if f.get("severity") == "error")
        result["ok"] = bool(scenario is not None and not blocking_gate and policy_ok and not output_errors
                            and not result["render"] and not workspace_errors)
        result["repair"] = _gen().classify_findings(result)
        with self._locked(project_id):
            if _file_sha256(scenario_path) != result["scenarioSha256"] or pack_digest(pdir) != result["packDigest"]:
                raise HttpError(409, "the project changed while it was being validated; validate again")
            self.store.write_json(pdir / "build" / "validate.json", result)
            meta = self.store.read_meta(project_id)
            anchors = result.get("anchors") or {}
            meta["lastValidation"] = {
                "at": result["at"], "ok": result["ok"], "gate": len(result["gate"]), "schema": len(result["schema"]),
                "policyErrors": sum(1 for f in result["policy"] if f.get("severity") == "error"),
                "outputErrors": output_errors, "renderErrors": len(result["render"]), "dryBuild": result["dryBuild"]["ran"],
                "workspaceErrors": workspace_errors,
                "repairable": sum(1 for f in result["repair"]["repairable"] if f["severity"] == "error"),
                "saOnly": len(result["repair"]["saOnly"]), "anchorsMissing": anchors.get("anchorsMissing", []),
            }
            if result["ok"] and meta.get("status") in ("intake", "truth-review"):
                meta["status"] = "validated"
            self.store.write_meta(project_id, meta)
        return result

    def _workspace_findings(self, project_id: str, pdir: Path, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Project-level findings: orphan files (apply prunes them), origins citing unknown materials,
        and customer source materials in a reference pack."""
        gen = _gen()
        findings: list[dict[str, Any]] = []
        orphans, _untracked = gen.orphan_files(pdir, data)
        for rel in orphans:
            findings.append({"severity": "warning", "code": "project.orphan_file", "path": rel, "line": None, "scopes": [],
                             "message": f"{rel} is not referenced by the scenario; the next Kiro apply deletes it"})
        materials = gen.read_materials(pdir)
        known = {str(r.get("id")) for r in materials}
        scope_of = {"fact": "facts", "tool": "tools", "document": "knowledge", "golden": "golden"}
        for item_type, key, item in gen._items(data):
            origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
            unknown = [m for m in origin.get("materials") or [] if m not in known]
            if unknown and item_type in scope_of:
                message = f"{item_type} {key} cites materials that are not uploaded: {', '.join(unknown)}"
                if item.get("provenance") in gen.FORMAL_PROVENANCE:
                    # A reviewed item's origin is the SA's label (generation preserves it verbatim), so
                    # only the SA can fix it: re-label it through review.
                    findings.append({"severity": "error", "code": "provenance.material_unknown", "path": f"{item_type}.{key}.origin",
                                     "line": None, "scopes": [], "saOnly": True,
                                     "message": message + "; re-label its origin in Review (confirm it again with an "
                                                          "origin that cites uploaded materials, or another kind)"})
                    continue
                findings.append({"severity": "error", "code": "provenance.material_unknown", "path": f"{item_type}.{key}.origin",
                                 "line": None, "scopes": [scope_of[item_type]], "message": message})
        if data.get("packKind") == "reference" and any(r.get("author") == "customer" and r.get("generationUse") == "source" for r in materials):
            findings.append({"severity": "warning", "code": "materials.reference_pack_customer_source", "path": "packKind",
                             "line": None, "scopes": [],
                             "message": "customer materials are a generation source of a reference (fictional) pack; "
                                        "use packKind workshop or customer when the pack teaches this customer's truth"})
        return findings

    def _dry_build(self, pdir: Path, scenario: Any, result: dict[str, Any]) -> None:
        """Compile + render into build/.dry-<hex> (never promoted), recording findings; always cleaned up."""
        eng = self.engine
        tmp = pdir / "build" / f".dry-{secrets.token_hex(4)}"
        started = time.monotonic()
        stage = "compile"
        try:
            lock = self.template_lock()
            commit = lock["template"]["commit"]
            upstream = self.home / lock["template"].get("localPath", "upstream/")  # type: ignore[operator]
            pack = eng["compiler"].compile_pack(scenario, tmp, template_commit=commit)
            result["output"] += [{**_jsonable(f), "stage": stage} for f in pack.report.findings]
            stage = "render"
            eng["render"].render_release(pack, upstream, tmp / "release", template_commit=commit)
        except eng["validator"].PackValidationError as exc:
            result["output"] += [{**_jsonable(f), "stage": stage} for f in exc.report.findings]
        except eng["render"].RenderError as exc:
            result["render"].append(str(exc))
        except Exception as exc:  # noqa: BLE001 - an engine crash is a failed dry build, never a 500
            sys.stderr.write(traceback.format_exc())
            result["render"].append(f"dry build error: {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            result["dryBuild"].update(ran=True, seconds=round(time.monotonic() - started, 3))

    def validation(self, project_id: str) -> dict[str, Any]:
        """The last persisted validation and whether it still matches the project's sources, plus
        ``rehearsalFindings``: the blocking remediation of the current release's rehearsal that a repair
        or regenerate adds as findings R1..Rn (``routes._rehearsal_findings``, before any scope narrows
        it), so the UI shows what Kiro will really be asked to fix."""
        pdir = self.store.project_dir(project_id)
        gen = _gen()
        rehearsal_findings = gen._rehearsal_findings(gen.current_rehearsal(pdir, self.store.read_meta(project_id)))
        path = pdir / "build" / "validate.json"
        if not path.is_file():
            return {"validation": None, "fresh": False, "rehearsalFindings": rehearsal_findings}
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"validation": None, "fresh": False, "rehearsalFindings": rehearsal_findings}
        fresh = bool(
            record.get("scenarioSha256") and record.get("packDigest")
            and record["scenarioSha256"] == _file_sha256(pdir / "scenario.yaml")
            and record["packDigest"] == pack_digest(pdir)
        )
        return {"validation": record, "fresh": fresh, "rehearsalFindings": rehearsal_findings}

    def build(self, project_id: str, *, strict_prose: bool = False) -> dict[str, Any]:
        eng = self._require_engine()
        validation = self.validate(project_id, dry_build=False)
        if not validation["ok"]:
            raise HttpError(409, "validation must pass before a build", validation=validation)
        pdir = self.store.project_dir(project_id)
        lock = self.template_lock()
        commit = lock["template"]["commit"]
        upstream = self.home / lock["template"].get("localPath", "upstream/")  # type: ignore[operator]
        out = pdir / "build"
        out.mkdir(parents=True, exist_ok=True)
        # Build into a private staging tree and swap it in only after it verifies. A failed rebuild
        # must leave the previous release, and build.json pointing at it, intact: status, rollback
        # and export still read that release.
        staging = out / f".staging-{secrets.token_hex(4)}"
        try:
            with self._lock:
                scenario = eng["scenario"].load_scenario(pdir / "scenario.yaml")
                pack = eng["compiler"].compile_pack(scenario, staging, template_commit=commit, strict_prose=strict_prose)
                release = eng["render"].render_release(pack, upstream, staging / "release", template_commit=commit)
                eng["render"].verify_release(release.release_dir)
                _promote_build(out, staging)
        except eng["validator"].PackValidationError as exc:
            # Output checks (holdout isolation, residue, secrets) refused the pack: a fixable input
            # problem, reported like validation (409 + findings), never as an internal error.
            raise HttpError(409, "the build failed its output checks; fix the findings, then build again",
                            findings=[_jsonable(f) for f in exc.report.findings]) from exc
        except eng["render"].RenderError as exc:
            raise HttpError(409, f"the release could not be rendered: {exc}", render=[str(exc)]) from exc
        except eng["guide"].GuideError as exc:
            # The Workshop Guides could not be generated from this scenario: an input problem, like a render error.
            raise HttpError(409, f"the Workshop Guides could not be generated: {exc}", render=[str(exc)]) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        release_dir = out / "release"
        manifest = eng["render"].verify_release(release_dir)
        summary = {
            "project": project_id,
            "at": utc_now(),
            "version": release.version,
            "templateCommit": commit,
            "releaseDir": str(release_dir),
            "files": len(manifest.get("files", {})),
            "contentHash": manifest.get("contentHash"),
            "sourceHash": self._source_hash(project_id),
            "patches": len(getattr(release, "patches", []) or []),
        }
        self.store.write_json(out / "build.json", summary)
        meta = self.store.read_meta(project_id)
        meta["lastBuild"] = {k: summary[k] for k in ("at", "version", "files", "contentHash", "sourceHash")}
        meta["status"] = "built"
        self.store.write_meta(project_id, meta)
        return summary

    def release_info(self, project_id: str) -> dict[str, Any]:
        eng = self._require_engine()
        summary = self._current_build(project_id)
        manifest = eng["render"].verify_release(Path(summary["releaseDir"]))
        return {"build": summary, "manifest": {k: v for k, v in manifest.items() if k != "files"}, "fileCount": len(manifest.get("files", {}))}

    def golden(self, project_id: str, *, include_holdout: bool = False) -> dict[str, Any]:
        import yaml

        pdir = self.store.project_dir(project_id)
        data = yaml.safe_load((pdir / "scenario.yaml").read_text(encoding="utf-8")) or {}
        cases = _golden_set(data)
        practice = [c for c in cases if c.get("set") == "practice"]
        holdout = [c for c in cases if c.get("set") == "holdout"]
        out = {"practice": practice, "holdoutCount": len(holdout), "categories": {}}
        for c in cases:
            out["categories"][c.get("category", "?")] = out["categories"].get(c.get("category", "?"), 0) + 1
        if include_holdout:
            out["holdout"] = holdout
            out["warning"] = "holdout cases are instructor-only; never paste them into student material"
        return out

    # -- Workshop Guide (SPEC D10) ------------------------------------------------
    def _verified_build_file(self, project_id: str, rel: str) -> Path:
        """A compiled file of the current build, verified against build/checksums.json (409 otherwise)."""
        build = self.store.project_dir(project_id) / "build"
        try:
            expected = json.loads((build / "checksums.json").read_text(encoding="utf-8"))["files"].get(rel)
        except (OSError, ValueError, KeyError, AttributeError):
            expected = None
        path = build / rel
        if not expected or not path.is_file() or _file_sha256(path) != expected:
            raise HttpError(409, f"build file {rel} is missing or does not match build/checksums.json; rebuild the release")
        return path

    def _rehearsal_record(self, project_id: str) -> dict[str, Any] | None:
        """run/rehearsal.json when present (rendered defensively by the engine), else None."""
        path = self.store.project_dir(project_id) / "run" / "rehearsal.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _guide_text(self, project_id: str, audience: str) -> tuple[str, str, dict[str, Any], bool]:
        eng = self._require_engine()
        if audience not in GUIDE_AUDIENCES:
            raise HttpError(400, "audience must be student or instructor")
        summary = self._current_build(project_id)
        g = eng["guide"]
        path = self._verified_build_file(project_id, g.STUDENT_GUIDE_PATH if audience == "student" else g.INSTRUCTOR_GUIDE_PATH)
        text = path.read_text(encoding="utf-8")
        rehearsal = self._rehearsal_record(project_id) if audience == "instructor" else None
        if rehearsal is not None:
            language = str(self._build_scenario(project_id).get("language") or "en")
            text = g.attach_rehearsal(text, rehearsal, language, release_version=summary["version"])
        return text, _file_sha256(path) or "", summary, rehearsal is not None

    def guide(self, project_id: str, audience: str) -> dict[str, Any]:
        """The built guide of the current release: student = release README.md, instructor = instructor-guide.md."""
        text, sha, summary, with_rehearsal = self._guide_text(project_id, audience)
        out = {"audience": audience, "version": summary["version"], "markdown": text, "builtSha256": sha,
               "fileName": "README.md" if audience == "student" else f"{summary['version']}-instructor-guide.md",
               "rehearsalEvidence": with_rehearsal}
        if audience == "instructor":
            out["warning"] = INSTRUCTOR_BUNDLE_WARNING
        return out

    def guide_download(self, project_id: str, audience: str) -> tuple[bytes, str]:
        text, _sha, summary, _r = self._guide_text(project_id, audience)
        return text.encode("utf-8"), ("README.md" if audience == "student" else f"{summary['version']}-instructor-guide.md")

    def guide_preview(self, project_id: str, audience: str) -> dict[str, Any]:
        """A draft guide from the live scenario.yaml: no build, provenance gate not enforced, draft banner."""
        import yaml

        eng = self._require_engine()
        if audience not in GUIDE_AUDIENCES:
            raise HttpError(400, "audience must be student or instructor")
        path = self.store.project_dir(project_id) / "scenario.yaml"
        commit = self.template_lock()["template"]["commit"]
        try:
            markdown, findings = eng["guide"].preview(path, audience, template_commit=commit,
                                                      rehearsal=self._rehearsal_record(project_id) if audience == "instructor" else None)
        except (eng["scenario"].ScenarioError, yaml.YAMLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            # A schema/xref error, or a scenario file that does not parse (YAML syntax, bad encoding).
            raise HttpError(409, f"the scenario does not load; fix it before previewing the guide: {exc}") from exc
        except eng["guide"].GuideError as exc:
            raise HttpError(409, f"the guide could not be rendered: {exc}") from exc
        out = {"audience": audience, "draft": True, "markdown": markdown, "findings": [_jsonable(f) for f in findings]}
        if audience == "instructor":
            out["warning"] = INSTRUCTOR_BUNDLE_WARNING
        return out

    def _instructor_bundle(self, project_id: str, summary: dict[str, Any]) -> tuple[bytes, str]:
        """'{version}-instructor.zip': every instructor/ file of the build (hash-verified), the student guide
        and, when a rehearsal record exists, rehearsal-evidence.md. Never any release file."""
        eng = self._require_engine()
        g = eng["guide"]
        build = self.store.project_dir(project_id) / "build"
        try:
            listed = json.loads((build / "checksums.json").read_text(encoding="utf-8"))["files"]
        except (OSError, ValueError, KeyError) as exc:
            raise HttpError(409, "build/checksums.json is unreadable; rebuild the release") from exc
        entries: dict[str, bytes] = {}
        for rel in sorted(listed):
            if rel.startswith("instructor/"):
                entries[rel[len("instructor/"):]] = self._verified_build_file(project_id, rel).read_bytes()
        if "instructor-guide.md" not in entries:
            raise HttpError(409, "the build has no instructor guide; rebuild the release")
        entries["student-guide.md"] = self._verified_build_file(project_id, g.STUDENT_GUIDE_PATH).read_bytes()
        rehearsal = self._rehearsal_record(project_id)
        if rehearsal is not None:
            snapshot = self._build_scenario(project_id)
            entries["rehearsal-evidence.md"] = g.rehearsal_evidence_file(
                rehearsal, snapshot.get("language"), release_version=summary["version"], pack_id=snapshot.get("id", project_id)).encode("utf-8")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(entries):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, entries[name])
        return buf.getvalue(), f"{summary['version']}-instructor.zip"

    def export_zip(self, project_id: str, bundle: str = "release") -> tuple[bytes, str]:
        if bundle not in ("release", "instructor"):
            raise HttpError(400, "bundle must be release or instructor")
        eng = self._require_engine()
        summary = self._current_build(project_id)
        if bundle == "instructor":
            return self._instructor_bundle(project_id, summary)
        release_dir = Path(summary["releaseDir"])
        try:
            eng["render"].verify_release(release_dir)
        except eng["render"].RenderError as exc:
            raise HttpError(409, f"the built release does not verify; rebuild it before exporting: {exc}") from exc
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(p for p in release_dir.rglob("*") if p.is_file()):
                info = zipfile.ZipInfo(path.relative_to(release_dir).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o755 if path.suffix == ".sh" else 0o644) << 16
                archive.writestr(info, path.read_bytes())
        return buf.getvalue(), f"{summary['version']}.zip"

    # -- sync ----------------------------------------------------------------
    def put_target(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        eng = self._require_engine()
        for forbidden in ("accessKeyId", "secretAccessKey", "sessionToken", "aws_access_key_id", "aws_secret_access_key"):
            if forbidden in body:
                raise HttpError(400, "the app never stores credential material; configure an AWS CLI profile and name it here")
        try:
            cfg = eng["sync"].TargetConfig(
                profile=str(body.get("profile", "")),
                region=str(body.get("region", "")),
                expected_account_id=str(body.get("expectedAccountId", "")),
                workshop_stack=str(body.get("workshopStack") or "workshop-infra"),
                addons_stack=str(body.get("addonsStack") or "workshop-customizer-addons"),
            )
        except eng["sync"].SyncError as exc:
            raise HttpError(400, str(exc)) from exc
        project = str(body.get("releaseProject") or project_id)
        if not _gen().PROJECT_RE.match(project):
            raise HttpError(400, "releaseProject must be kebab-case")
        target = {**asdict(cfg), "releaseProject": project}
        meta = self.store.read_meta(project_id)
        meta["target"] = target
        self.store.write_meta(project_id, meta)
        return target

    def _target_cfg(self, project_id: str):
        eng = self._require_engine()
        meta = self.store.read_meta(project_id)
        target = meta.get("target")
        if not target:
            raise HttpError(409, "set the workshop target first (profile, region, expected account, stacks)")
        cfg = eng["sync"].TargetConfig(**{k: v for k, v in target.items() if k != "releaseProject"})
        return cfg, target["releaseProject"], meta

    def sync_preflight(self, project_id: str) -> dict[str, Any]:
        eng = self._require_engine()
        summary = self._current_build(project_id)
        cfg, release_project, meta = self._target_cfg(project_id)
        pdir = self.store.project_dir(project_id)
        manifest = eng["render"].verify_release(Path(summary["releaseDir"]))
        previous = [ns for ns in meta.get("syncedNamespaces") or [] if isinstance(ns, dict)]
        pf = eng["sync"].preflight(cfg, self._clients(cfg), expected_template_commit=manifest["templateCommit"],
                                   pack_namespace=manifest["namespace"], previous_namespaces=previous)
        plan = {
            "project": project_id,
            "releaseProject": release_project,
            "version": manifest["version"],
            "packId": manifest["packId"],
            "accountId": pf.account_id,
            "identityArn": pf.identity_arn,
            "region": cfg.region,
            "instanceId": pf.instance_id,
            "bucket": pf.bucket,
            "documentName": pf.document_name,
            "runDocumentName": pf.run_document_name,
            "runDocumentSha256": pf.run_document_sha256,
            "sourceUri": f"s3://{pf.bucket}/customizer-releases/{release_project}/{manifest['version']}/release.zip" if pf.bucket else None,
            "templateCommit": manifest["templateCommit"],
        }
        result: dict[str, Any] = {"ok": pf.ok, "checks": _jsonable(pf.checks), "plan": plan, "at": utc_now()}
        if pf.ok:
            token = secrets.token_urlsafe(24)
            digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode("utf-8")).hexdigest()
            self.store.write_json(pdir / "sync" / "pending-confirm.json", {"token": token, "planDigest": digest, "issuedAt": time.time(), "plan": plan})
            result["confirmToken"] = token
            result["confirmExpiresIn"] = CONFIRM_TOKEN_TTL
        self.store.write_json(pdir / "sync" / "last-preflight.json", {k: v for k, v in result.items() if k != "confirmToken"})
        self.store.append_history(project_id, {"event": "preflight", "ok": pf.ok, "version": plan["version"], "failures": pf.failures()})
        return result

    def sync_apply(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        eng = self._require_engine()
        summary = self._current_build(project_id)
        cfg, release_project, meta = self._target_cfg(project_id)
        pdir = self.store.project_dir(project_id)
        pending_path = pdir / "sync" / "pending-confirm.json"
        if not pending_path.is_file():
            raise HttpError(409, "run preflight first; apply needs a fresh confirm token")
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        token = str(body.get("confirmToken", ""))
        if not token or not hmac.compare_digest(token, pending["token"]):
            raise HttpError(403, "confirm token does not match the last preflight")
        if time.time() - pending["issuedAt"] > CONFIRM_TOKEN_TTL:
            pending_path.unlink()
            raise HttpError(409, "confirm token expired; run preflight again")
        pending_path.unlink()  # one-time use
        release_dir = Path(summary["releaseDir"])
        expected_digest = pending["planDigest"]

        def confirm(plan) -> bool:
            live = {
                "project": project_id,
                "releaseProject": release_project,
                "version": plan.version,
                "packId": plan.pack_id,
                "accountId": plan.account_id,
                "identityArn": plan.preflight.identity_arn,
                "region": plan.region,
                "instanceId": plan.instance_id,
                "bucket": plan.bucket,
                "documentName": plan.document_name,
                "runDocumentName": plan.preflight.run_document_name,
                "runDocumentSha256": plan.preflight.run_document_sha256,
                "sourceUri": plan.source_uri,
                "templateCommit": plan.template_commit,
            }
            return hashlib.sha256(json.dumps(live, sort_keys=True).encode("utf-8")).hexdigest() == expected_digest

        try:
            outcome = eng["sync"].sync_release(cfg, self._clients(cfg), release_dir, project=release_project, work_dir=pdir / "sync" / "bundles", confirm=confirm, watchdog_minutes=int(body.get("watchdogMinutes", 20)))
        except eng["sync"].SyncError as exc:
            self.store.append_history(project_id, {"event": "apply", "status": "refused", "reason": str(exc)})
            raise HttpError(409, str(exc)) from exc
        record = {
            "status": outcome["status"],
            "commandId": outcome.get("commandId"),
            "ssmStatus": outcome.get("ssmStatus"),
            "applier": outcome.get("applier"),
            "version": summary["version"],
            "at": utc_now(),
        }
        if outcome["status"] == "not-confirmed":
            record["reason"] = "live plan differed from the confirmed preflight plan"
        self.store.append_history(project_id, {"event": "apply", **record})
        meta["lastSync"] = record
        if outcome["status"] == "pending-commit":
            meta["status"] = "synced-pending-commit"
            # The namespaces this project has synced into the environment: a later release with another
            # namespace must find their resources gone too (preflight guard.previous.*).
            namespace = json.loads((release_dir / "RELEASE.json").read_text(encoding="utf-8")).get("namespace")
            synced = [ns for ns in meta.get("syncedNamespaces") or [] if isinstance(ns, dict) and ns != namespace]
            meta["syncedNamespaces"] = ([*synced, namespace] if isinstance(namespace, dict) else synced)[-5:]
        self.store.write_meta(project_id, meta)
        return record

    def sync_action(self, project_id: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        eng = self._require_engine()
        cfg, release_project, meta = self._target_cfg(project_id)
        pf_path = self.store.project_dir(project_id) / "sync" / "last-preflight.json"
        if not pf_path.is_file():
            raise HttpError(409, "run preflight first so the instance and document are known")
        plan = json.loads(pf_path.read_text(encoding="utf-8"))["plan"]
        clients = self._clients(cfg)
        try:
            command_id = eng["sync"].send_action(clients, instance_id=plan["instanceId"], document_name=plan["documentName"], action=action, version=str(body.get("version") or plan["version"]), reason=str(body.get("reason") or "operator request"))
            outcome = eng["sync"].wait_for_command(clients, command_id, plan["instanceId"], timeout=float(body.get("timeout", 600)))
        except eng["sync"].SyncError as exc:
            raise HttpError(409, str(exc)) from exc
        record = {"action": action, "commandId": command_id, "ssmStatus": outcome["ssmStatus"], "applier": outcome["result"], "at": utc_now()}
        if action in ("rollback", "status"):
            # Files restored ≠ scenario rolled back: the KB / Gateway / runtime may still be the new version.
            manifest = eng["render"].verify_release(Path(json.loads((self.store.project_dir(project_id) / "build" / "build.json").read_text(encoding="utf-8"))["releaseDir"]))
            guard = eng["sync"].resource_guard(clients, manifest["namespace"])
            record["resourceGuard"] = _jsonable(guard)
            record["awsResourcesPresent"] = eng["sync"].resources_present(guard)
        self.store.append_history(project_id, {"event": action, **record})
        if action == "commit" and outcome["result"].get("status") == "committed":
            meta["status"] = "synced"
        if action == "rollback" and outcome["result"].get("status") in ("rolled-back", "rollback"):
            if record["awsResourcesPresent"]:
                record["status"] = "rolled-back-files-only"
                record["warning"] = "files were restored but scenario AWS resources still exist; this is NOT a rollback of the scenario — rebuild the workshop environment"
                meta["status"] = "rollback-incomplete"
            else:
                record["status"] = "rolled-back"
                meta["status"] = "rolled-back"
        meta["lastSync"] = record
        self.store.write_meta(project_id, meta)
        return record

    SYNC_JOB_ACTIONS = ("status", "commit", "rollback")

    def start_sync_action_job(self, project_id: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        """Run Status / Confirm release / Rollback as a background job.

        The synchronous ``sync/<action>`` route waits for SSM (up to 10 minutes); behind the host's
        30-second proxy that becomes a 504 while the command keeps running on the EC2. This variant
        answers 202 at once and the page polls ``jobs/<id>``; the job result is the record
        ``sync_action`` returns, so history and project status are written exactly as before.
        """
        if action not in self.SYNC_JOB_ACTIONS:
            raise HttpError(400, f"unsupported sync action {action!r}; expected one of {', '.join(self.SYNC_JOB_ACTIONS)}")
        self._require_engine()
        self._target_cfg(project_id)  # refuse before a job exists when no Workshop target is set
        if not (self.store.project_dir(project_id) / "sync" / "last-preflight.json").is_file():
            raise HttpError(409, "run preflight first so the instance and document are known")
        params = {k: body[k] for k in ("version", "reason") if isinstance(body.get(k), str) and body[k]}

        def work(ctx: JobContext) -> dict[str, Any]:
            ctx.begin(action)
            record = self.sync_action(project_id, action, params)
            applier = record.get("applier") or {}
            ctx.done(action, {"status": record.get("status") or applier.get("status"), "commandId": record.get("commandId")})
            return record

        return self.jobs.start(project_id, f"sync-{action}", (action,), work, params=params)

    def sync_history(self, project_id: str) -> list[dict[str, Any]]:
        path = self.store.project_dir(project_id) / "sync" / "history.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # -- direct mode (AgentCore, no Workshop instance) -----------------------
    DIRECT_STAGES = ("provision", "ask", "settle", "judge", "rehearse")
    #: InvokeHarness answers of a chat are kept here; one question at a time per chat id.
    CHAT_ID_RE = re.compile(r"^chat-[0-9a-f]{12}$")
    MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.:_\-/]{2,199}$")

    def _direct_session(self, project_id: str):
        """(boto3 session, account, target config) from the project's Workshop target, the account checked."""
        cfg, _release_project, _meta = self._target_cfg(project_id)
        import boto3

        try:
            session = boto3.Session(profile_name=cfg.profile, region_name=cfg.region)
            account = session.client("sts").get_caller_identity()["Account"]
        except Exception as exc:  # noqa: BLE001 - an expired or unknown profile: refuse, never guess
            raise HttpError(409, f"AWS profile '{cfg.profile}' is not usable from this app: {type(exc).__name__}: {exc}") from exc
        if account != cfg.expected_account_id:
            raise HttpError(409, f"the AWS profile resolves to account {account}, not the target's {cfg.expected_account_id}")
        return session, account, cfg

    @staticmethod
    def _direct_run_class():
        from workshop_customizer.direct.run import DirectRun  # the engine dir is on sys.path once load_engine ran

        return DirectRun

    def start_direct(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Rehearse the current build in direct mode (about 8 min) in the background; the page polls the job."""
        self._require_engine()
        summary = self._current_build(project_id)
        compare = [m.strip() for m in body.get("compareModels") or [] if isinstance(m, str) and m.strip()][:3]
        bad = [m for m in compare if not self.MODEL_ID_RE.match(m)]
        if bad:
            raise HttpError(400, f"not a Bedrock model or inference profile id: {', '.join(bad)}")
        repeat = body.get("repeat", 1)
        if repeat not in (1, 2, 3):
            raise HttpError(400, "repeat must be 1, 2 or 3 rounds")
        panel = body.get("panel") is True
        session, account, cfg = self._direct_session(project_id)
        pdir = self.store.project_dir(project_id)

        def work(ctx: JobContext) -> dict[str, Any]:
            current = {"stage": None}

            def on_stage(name: str) -> None:
                if current["stage"]:
                    ctx.done(current["stage"])
                ctx.begin(name)
                current["stage"] = name

            out = pdir / "build" / "direct" / str(summary["version"])
            from workshop_customizer.direct.panel import DEFAULT_PANEL

            doc = self._direct_run_class()(pdir, session=session, profile=cfg.profile, account=account, region=cfg.region, out=out,
                                           log=lambda _line: None, on_stage=on_stage, compare_models=compare, repeat=repeat,
                                           panel=DEFAULT_PANEL if panel else ()).run()
            if current["stage"]:
                ctx.done(current["stage"], {"verdict": doc["verdict"], "reasonCode": doc.get("reasonCode")})
            return {"version": summary["version"], "verdict": doc["verdict"], "reasonCode": doc.get("reasonCode"),
                    "seconds": (doc.get("direct") or {}).get("seconds")}

        return self.jobs.start(project_id, "direct", self.DIRECT_STAGES, work, params={"compareModels": compare, "repeat": repeat, "panel": panel})

    def direct_status(self, project_id: str) -> dict[str, Any]:
        """The direct rehearsal of the current build (if any), the pack's direct resources and the latest job."""
        pdir = self.store.project_dir(project_id)
        build = None
        try:
            build = json.loads((pdir / "build" / "build.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        version = (build or {}).get("version")
        doc = None
        if version:
            try:
                doc = json.loads((pdir / "build" / "direct" / str(version) / "direct-rehearsal.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = None
        try:
            resources = json.loads((pdir / "build" / "direct" / "resources.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            resources = None
        rehearsal = None
        if doc:
            rehearsal = {k: doc.get(k) for k in ("releaseVersion", "verdict", "reasonCode", "reason", "reasonEn", "generatedAt", "language")}
            rehearsal["phenomena"] = [{**{k: p.get(k) for k in ("id", "kind", "mechanism", "verdict", "reasonCode", "teachingPoint", "replication")},
                                       "cases": [{k: c.get(k) for k in ("caseId", "status", "reason", "reasonEn", "baseline", "optimized")}
                                                 for c in p.get("cases") or []]}
                                      for p in doc.get("phenomena") or []]
            # Whole hints: the page's "让 Kiro 修" sends one as a repair, as from the Guided Run's rehearsal.
            rehearsal["remediation"] = [h for h in doc.get("remediation") or [] if h.get("code") != "RELEASE_NOT_VERIFIED"]
            direct = doc.get("direct") or {}
            rehearsal["direct"] = {k: direct.get(k) for k in ("seconds", "timings", "sweep", "invokeErrors", "harness", "rounds", "robust",
                                                              "flaky", "readyRounds", "skippedSkills")}
            if direct.get("panel"):  # the matrix and the recommendation (the per-call rows stay in the file)
                rehearsal["direct"]["panel"] = {k: direct["panel"].get(k) for k in ("evaluators", "bands", "matrix", "recommendation", "unseen",
                                                                                     "references", "error")}
        return {"version": version, "rehearsal": rehearsal, "resources": resources, "job": self.jobs.latest(project_id, "direct"),
                "cleanupJob": self.jobs.latest(project_id, "direct-cleanup")}

    def start_direct_chat(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Ask the pack's direct Harness one question (in the background; the page polls the chat)."""
        question = body.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise HttpError(400, "question must be 1-2000 characters")
        prompt_kind = body.get("prompt", "candidate")
        if prompt_kind not in ("baseline", "candidate"):
            raise HttpError(400, "prompt must be baseline or candidate")
        model = body.get("model") or None
        if model is not None and not (isinstance(model, str) and self.MODEL_ID_RE.match(model)):
            raise HttpError(400, "model must be a Bedrock model or inference profile id")
        pdir = self.store.project_dir(project_id)
        try:
            harness = json.loads((pdir / "build" / "direct" / "resources.json").read_text(encoding="utf-8"))["harness"]
        except (OSError, ValueError, KeyError) as exc:
            raise HttpError(409, "no direct Harness yet: run a direct rehearsal first") from exc
        summary = self._current_build(project_id)
        prompt = (Path(summary["releaseDir"]) / "pack" / "prompts" / ("baseline.md" if prompt_kind == "baseline" else "optimization-candidate.md")
                  ).read_text(encoding="utf-8")
        session, _account, cfg = self._direct_session(project_id)
        chat_id = f"chat-{secrets.token_hex(6)}"
        chats = pdir / "build" / "direct" / "chats"
        record = {"id": chat_id, "status": "running", "question": question, "prompt": prompt_kind, "model": model, "answer": "",
                  "tools": [], "sessionId": None, "error": None, "seconds": None, "createdAt": utc_now()}
        self.store.write_json(chats / f"{chat_id}.json", record)

        def ask() -> None:
            from workshop_customizer.direct.run import Asked, new_session_id, stream_result

            asked = Asked(index=0, case_id="chat", query=question, session_id=new_session_id(int(time.time())),
                          actor=f"console-{chat_id}", started_ms=0, probe=False)
            started = time.monotonic()
            try:
                from workshop_customizer.direct.aws import client as direct_client

                client = direct_client(session, "bedrock-agentcore", cfg.region)
                override = {"model": {"bedrockModelConfig": {"modelId": model}}} if model else {}
                response = client.invoke_harness(harnessArn=harness["arn"], runtimeSessionId=asked.session_id, actorId=asked.actor,
                                                 messages=[{"role": "user", "content": [{"text": question}]}], systemPrompt=[{"text": prompt}],
                                                 **override)
                stream_result(response["stream"], asked)
            except Exception as exc:  # noqa: BLE001 - shown in the chat
                asked.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            record.update(status="failed" if asked.error and not asked.text else "done", answer=asked.text, tools=asked.tools,
                          sessionId=asked.session_id, error=asked.error, seconds=round(time.monotonic() - started, 1))
            self.store.write_json(chats / f"{chat_id}.json", record)

        snapshot = dict(record)  # the thread updates record; the response is the state at the start
        threading.Thread(target=ask, name=f"wc-{chat_id}", daemon=True).start()
        return snapshot

    def direct_chat(self, project_id: str, chat_id: str) -> dict[str, Any]:
        if not self.CHAT_ID_RE.match(chat_id):
            raise HttpError(400, "invalid chat id")
        path = self.store.project_dir(project_id) / "build" / "direct" / "chats" / f"{chat_id}.json"
        if not path.is_file():
            raise HttpError(404, f"chat {chat_id} not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def start_direct_cleanup(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Remove the pack's direct-mode resources (never the Workshop's) in the background."""
        if body.get("acknowledged") is not True:
            raise HttpError(400, "acknowledged must be true: confirm removing this pack's direct resources")
        summary = self._current_build(project_id)
        session, account, cfg = self._direct_session(project_id)
        pdir = self.store.project_dir(project_id)

        def work(ctx: JobContext) -> dict[str, Any]:
            from workshop_customizer.direct.cleanup import cleanup
            from workshop_customizer.direct.names import DirectNames

            ctx.begin("cleanup")
            release = Path(summary["releaseDir"])
            pack = json.loads((release / "pack" / "pack.json").read_text(encoding="utf-8"))
            done = cleanup(session, DirectNames.of(pack["namespace"], account=account, region=cfg.region), release_dir=release,
                           region=cfg.region, profile=cfg.profile, state_path=pdir / "build" / "direct" / "resources.json", log=lambda _l: None)
            failed = {k: v for k, v in done.items() if v.startswith("failed")}
            if failed:
                raise JobStageFailed("some direct resources were not removed", failed)
            ctx.done("cleanup", done)
            return done

        return self.jobs.start(project_id, "direct-cleanup", ("cleanup",), work)

    # -- one-click delivery ------------------------------------------------
    def start_oneclick(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Validate → build → preflight → apply → verify → confirm → bind the Guided Run, in the background.

        Every stage reuses the same guarded method the step-by-step buttons call, so one-click adds
        no new trust path: the preflight token, the plan-digest confirmation, the fixed SSM documents
        and the EC2 smoke check all still apply. Re-running it for an unchanged scenario is safe: the
        build is reused, the EC2 applier answers ``no-op`` and commit is idempotent.
        """
        self._require_engine()
        self._target_cfg(project_id)  # refuse before a job exists when no Workshop target is set
        if body.get("acknowledged") is not True:
            # Same explicit-intent gate as the manual Apply checkbox: one-click writes to the Workshop
            # account, so a bare POST (a stray script, a replayed request) must not start it.
            raise HttpError(400, "acknowledged must be true: confirm the Workshop account is correct before one-click overwrite")
        try:
            watchdog = int(body.get("watchdogMinutes", 20))
        except (TypeError, ValueError) as exc:
            raise HttpError(400, "watchdogMinutes must be an integer") from exc
        if not 5 <= watchdog <= 240:
            raise HttpError(400, "watchdogMinutes must be between 5 and 240")
        params = {"watchdogMinutes": watchdog, "confirm": bool(body.get("confirm", True))}
        return self.jobs.start(project_id, "oneclick", ONECLICK_STAGES, lambda ctx: self._oneclick(ctx, params), params=params)

    def _oneclick(self, ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
        from workshop_customizer.guided_run import next_runnable

        pid = ctx.project_id

        ctx.begin("validate")
        validation = self.validate(pid, dry_build=False)
        if not validation["ok"]:
            errors = [f for f in validation["policy"] if f.get("severity") == "error"]
            raise JobStageFailed("validation failed; fix the findings, then run one-click again",
                                 {"gate": validation["gate"], "schema": validation["schema"], "policy": errors[:50]})
        ctx.done("validate", validation.get("summary"))

        ctx.begin("build")
        try:
            summary, reused = self._current_build(pid), True
        except HttpError:
            summary, reused = self.build(pid), False
        version = str(summary["version"])
        ctx.done("build", {"version": version, "files": summary.get("files"), "contentHash": summary.get("contentHash"), "reused": reused},
                 note="reused the release already built from these exact sources" if reused else None)

        ctx.begin("preflight")
        pf = self.sync_preflight(pid)
        if not pf["ok"]:
            failed = [c for c in pf["checks"] if c.get("status") == "fail"]
            raise JobStageFailed("preflight refused the Workshop target; fix the failed checks, then run one-click again", {"failed": failed})
        plan = pf["plan"]
        ctx.done("preflight", {**{k: plan.get(k) for k in ("accountId", "region", "instanceId", "documentName", "runDocumentName")}, "checks": len(pf["checks"])})

        ctx.begin("apply")
        applied = self.sync_apply(pid, {"confirmToken": pf["confirmToken"], "watchdogMinutes": params["watchdogMinutes"]})
        if applied.get("status") not in ("pending-commit", "no-op"):
            raise JobStageFailed(f"the Workshop EC2 did not accept the release (status {applied.get('status')})", applied)
        ctx.done("apply", applied, note="this release was already the active workshop tree" if applied.get("status") == "no-op" else None)

        ctx.begin("verify")
        status = self.sync_action(pid, "status", {"version": version})
        host = status.get("applier") or {}
        if (host.get("active") != version or host.get("currentLink") != f"releases/{version}"
                or host.get("status") not in ("pending-commit", "committed")):
            raise JobStageFailed("the Workshop EC2 does not report this release as its active workshop tree",
                                 {"expected": version, "active": host.get("active"), "currentLink": host.get("currentLink"), "hostStatus": host.get("status")})
        ctx.done("verify", {"active": host.get("active"), "currentLink": host.get("currentLink"), "hostStatus": host.get("status"), "releases": host.get("releases")})

        if not params["confirm"]:
            ctx.skip("confirm", "left pending-commit on request: the watchdog rolls the Workshop back unless you confirm")
            ctx.skip("guided", "the Guided Run needs a confirmed release")
            return {"version": version, "live": False, "hostStatus": host.get("status")}

        ctx.begin("confirm")
        committed = self.sync_action(pid, "commit", {"version": version})
        if (committed.get("applier") or {}).get("status") != "committed":
            raise JobStageFailed("the Workshop EC2 did not acknowledge the confirmation", committed)
        ctx.done("confirm", {"active": (committed.get("applier") or {}).get("active"), "commandId": committed.get("commandId")})

        ctx.begin("guided")
        try:
            state, reset = self.guided_run(pid), False
        except HttpError:  # bound to an earlier release: archive it and start fresh on this one
            state, reset = self.reset_guided_run(pid), True
        next_step = next_runnable(state["steps"])
        passed = sum(1 for step in state["steps"].values() if step["status"] == "passed")
        ctx.done("guided", {"releaseVersion": state["releaseVersion"], "steps": len(state["steps"]), "passed": passed, "nextStep": next_step, "reset": reset})
        return {"version": version, "live": True, "nextStep": next_step}


# ---------------------------------------------------------------------------
# The ADLC console, mounted under /api/console (app/console/server.py's Mount)
# ---------------------------------------------------------------------------

#: The console's routes in this process: the browser's /apps/<app>/api/console/... through the gateway's signed proxy.
CONSOLE_PREFIX = "/api/console"
CONSOLE_PY = APP_DIR / "console" / "server.py"
BODY_LIMIT = 8 * 1024 * 1024
_CONSOLE_LOCK = threading.Lock()


def console_module() -> Any:
    """``app/console/server.py``, loaded by path once: it brings the platform's engine modules (and boto3) along, so
    it is loaded on the console's first request, never at start-up."""
    return _load_once("adlc_console_server_mount", CONSOLE_PY, _CONSOLE_LOCK)


class ConsoleMount:
    """The console of one :class:`Service`, in its data directory (``<data>/console``), made on first use.
    ``session_factory`` stands in for boto3 sessions (tests); ``public_api`` is where the public API's port listens
    (:func:`start_public_api` fills it in)."""

    def __init__(self, service: Service, *, session_factory: Callable[..., Any] | None = None):
        self.service = service
        self._factory = session_factory
        self._mount: Any = None
        self._lock = threading.Lock()
        self.public_api: dict[str, Any] = {}

    def get(self) -> Any:
        with self._lock:
            if self._mount is None:
                self._mount = console_module().Mount(self.service.store.data_dir, app=APP_NAME, session_factory=self._factory,
                                                     public_api=self.public_api)
            return self._mount


#: The public API's own port on 127.0.0.1 (``ADLC_PUBLIC_API_PORT``; 0 turns it off).
PUBLIC_API_PORT = 8772
PUBLIC_BODY_LIMIT = 1024 * 1024


def write_reply(handler: BaseHTTPRequestHandler, reply: Any) -> None:
    """A console :class:`Reply`: whole, or its ``chunks`` sent chunked as they come (a stream, through the gateway or
    on the public API's port)."""
    handler.send_response(reply.status)
    handler.send_header("Content-Type", reply.content_type)
    if reply.chunks is None:
        handler.send_header("Content-Length", str(len(reply.body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    if reply.chunks is not None:
        handler.send_header("Transfer-Encoding", "chunked")
    for k, v in reply.headers.items():
        handler.send_header(k, v)
    handler.end_headers()
    if reply.chunks is None:
        handler.wfile.write(reply.body)
        return
    try:
        for chunk in reply.chunks:
            if chunk:
                handler.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                handler.wfile.flush()
        handler.wfile.write(b"0\r\n\r\n")
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True
    except Exception:  # noqa: BLE001 - the status line is gone: end the response by closing, log locally
        sys.stderr.write(traceback.format_exc())
        handler.close_connection = True
    finally:
        close = getattr(reply.chunks, "close", None)
        if close:
            close()


def make_public_handler(console: ConsoleMount):
    """The public API's port: ``/v1/...`` only, a localhost Host only (no DNS rebinding), the console's own
    ``X-Api-Key`` for every call (no CORS: a web page cannot send that header here without a preflight it fails)."""

    class PublicHandler(BaseHTTPRequestHandler):
        server_version = "AdlcPublicApi/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # no bodies or keys ever logged
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _refuse(self, status: int, error: str) -> None:
            self.close_connection = True
            body = json.dumps({"error": error}).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self) -> None:
            host = (self.headers.get("Host") or "").strip().lower().rsplit(":", 1)[0].strip("[]")
            if host not in ("127.0.0.1", "localhost", "::1"):
                return self._refuse(403, "this port answers only to localhost")
            if not urlsplit(self.path).path.startswith("/v1/"):
                return self._refuse(404, "this port serves only the console's public API, under /v1")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > PUBLIC_BODY_LIMIT:
                return self._refuse(400 if length < 0 else 413, "bad Content-Length" if length < 0 else "body too large")
            raw = self.rfile.read(length) if length else b""
            try:
                reply = console.get().public(self.command, self.path, raw, self.headers)
            except Exception as exc:  # noqa: BLE001 - the console could not load: say so, never a traceback
                return self._refuse(503, f"the console is not available: {type(exc).__name__}: {str(exc)[:300]}")
            write_reply(self, reply)

        do_GET = _dispatch
        do_POST = _dispatch

    return PublicHandler


def start_public_api(console: ConsoleMount, port: int) -> ThreadingHTTPServer | None:
    """The public API's port on 127.0.0.1 (0 picks a free one; below 0 it is off), served on a thread. What came of
    it goes on ``console.public_api``, which the console's 运行环境 and API 密钥 pages show."""
    if port < 0:
        console.public_api.update(listening=False, error="turned off (ADLC_PUBLIC_API_PORT=0)")
        return None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_public_handler(console))
    except OSError as exc:
        console.public_api.update(listening=False, port=port, error=f"{type(exc).__name__}: {exc}")
        sys.stderr.write(f"{APP_NAME}: the public API's port 127.0.0.1:{port} did not open: {exc}\n")
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="adlc-public-api", daemon=True).start()
    actual = server.server_address[1]
    console.public_api.update(listening=True, port=actual, url=f"http://127.0.0.1:{actual}/v1", error=None)
    return server


def is_console(path: str) -> bool:
    return path == CONSOLE_PREFIX or path.startswith(CONSOLE_PREFIX + "/")


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def make_handler(service: Service, proxy_secret: str, console: ConsoleMount | None = None):
    console = console or ConsoleMount(service)

    class Handler(BaseHTTPRequestHandler):
        server_version = "WorkshopCustomizer/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter, no bodies ever logged
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        # -- plumbing ------------------------------------------------------
        def _send(self, status: int, payload: Any = None, *, raw: bytes | None = None, content_type: str = "application/json; charset=utf-8", extra: dict[str, str] | None = None) -> None:
            body = raw if raw is not None else json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self, limit: int = BODY_LIMIT) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > limit:
                self.close_connection = True  # the body is left unread: this connection cannot carry another request
                raise HttpError(400 if length < 0 else 413, "bad Content-Length" if length < 0 else "body too large")
            return self.rfile.read(length) if length else b""

        def _console(self, split) -> None:
            """``/api/console/...``: only through the gateway, whose signature covers the body (so it is read first,
            bounded by the console's own limit, once the header is there)."""
            if not proxy_secret:
                self.close_connection = True
                raise HttpError(403, "the console answers here only through KiroCrew's gateway (no KIROCREW_PROXY_SECRET); "
                                     "run app/console/server.py for the standalone console")
            if not self.headers.get("X-KiroCrew-Proxy"):
                self.close_connection = True
                raise HttpError(401, "missing or invalid gateway signature")
            mount = console.get()
            body = self._read_body(mount.body_limit(split.path))
            if not self._authorised(body):
                raise HttpError(401, "missing or invalid gateway signature")
            write_reply(self, mount.handle(self.command, self.path, body, self.headers))

        def _authorised(self, body: bytes) -> bool:
            if not proxy_secret:
                return True
            split = urlsplit(self.path)
            # The gateway's liveness probe is an UNSIGNED `GET <healthCheck>` and demands status < 400,
            # otherwise the backend is marked unhealthy and never proxied. /health carries no project
            # data (status, engine flag, paths) and is loopback-only, so it is the one unsigned route.
            if self.command == "GET" and not body and split.path in ("/health", f"{ROUTE_PREFIX}/health"):
                return True
            header = self.headers.get("X-KiroCrew-Proxy", "")
            targets = [self.path, split.path]
            stripped = split.path[len(ROUTE_PREFIX):] if split.path.startswith(ROUTE_PREFIX) else None
            if stripped:
                targets += [stripped, stripped + (f"?{split.query}" if split.query else "")]
            return verify_proxy_signature(proxy_secret, header, self.command, targets, body)

        def _dispatch(self) -> None:
            try:
                split = urlsplit(self.path)
                if is_console(split.path):
                    self._console(split)
                    return
                body = self._read_body()
                if not self._authorised(body):
                    raise HttpError(401, "missing or invalid gateway signature")
                path = split.path
                if path.startswith(ROUTE_PREFIX):
                    path = path[len(ROUTE_PREFIX):] or "/"
                query = {k: v[-1] for k, v in parse_qs(split.query).items()}
                payload: dict[str, Any] = {}
                if body:
                    try:
                        payload = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise HttpError(400, f"body must be JSON: {exc}") from exc
                    if not isinstance(payload, dict):
                        raise HttpError(400, "body must be a JSON object")
                status, result, raw, headers = route(service, self.command, path, query, payload)
                if raw is not None:
                    self._send(status, raw=raw, content_type=headers.pop("Content-Type", "application/octet-stream"), extra=headers)
                else:
                    self._send(status, result)
            except HttpError as exc:
                self._send(exc.status, exc.payload)
            except Exception as exc:  # noqa: BLE001 - never leak tracebacks to the UI, log locally
                sys.stderr.write(traceback.format_exc())
                self._send(500, {"error": f"internal error: {type(exc).__name__}: {exc}"})

        def do_GET(self):  # noqa: N802
            self._dispatch()

        def do_POST(self):  # noqa: N802
            self._dispatch()

        def do_PUT(self):  # noqa: N802
            self._dispatch()

        def do_DELETE(self):  # noqa: N802
            self._dispatch()

    return Handler


def route(service: Service, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> tuple[int, Any, bytes | None, dict[str, str]]:
    parts = [p for p in path.split("/") if p]
    ok = lambda payload, status=200: (status, payload, None, {})  # noqa: E731

    if method == "GET" and parts in ([], ["health"]):
        return ok(service.health())
    if parts[:1] == ["templates"] and method == "GET":
        return ok({"templates": list(TEMPLATES)})
    if parts[:1] == ["projects"]:
        if len(parts) == 1:
            if method == "GET":
                return ok({"projects": service.store.list_projects()})
            if method == "POST":
                return ok(service.create_project(body), 201)
        elif len(parts) >= 2:
            pid = parts[1]
            rest = parts[2:]
            if method != "GET":
                busy = service.jobs.active(pid)
                # A direct job touches only build/direct and its own AWS resources: the Guided Run (run/*) and the chat
                # go on beside it; anything that rewrites the sources or the release waits.
                if busy and not (busy.startswith("direct") and (rest[:1] == ["run"] or rest[:2] == ["direct", "chat"])):
                    raise HttpError(409, f"job {busy} is still running for this project; wait for it to finish", jobId=busy)
            if not rest:
                if method == "GET":
                    return ok(service.get_project(pid))
            elif rest == ["scenario"] and method == "PUT":
                return ok(service.put_scenario(pid, body))
            elif rest == ["scenario"] and method == "GET":
                return ok(service.scenario_view(pid))
            elif rest == ["files", "prune"] and method == "POST":
                return ok(service.prune_files(pid, body))
            elif rest[:1] == ["files"] and method == "PUT":
                return ok(service.put_file(pid, "/".join(rest[1:]), str(body.get("content", ""))))
            elif len(rest) >= 2 and rest[0] == "files" and method == "GET":
                return ok(service.get_file(pid, "/".join(rest[1:])))
            elif rest == ["pack-kind"] and method == "POST":
                return ok(service.switch_pack_kind(pid, body))
            elif rest == ["items"] and method == "GET":
                return ok(service.list_items(pid))
            elif rest == ["materials"] and method == "GET":
                return ok(service.list_materials(pid))
            elif rest == ["materials"] and method == "POST":
                return ok(service.upload_material(pid, body), 201)
            elif rest == ["materials", "approval"] and method == "POST":
                return ok(service.record_material_approval(pid, body))
            elif len(rest) == 2 and rest[0] == "materials" and method == "PUT":
                return ok(service.update_material(pid, rest[1], body))
            elif len(rest) == 2 and rest[0] == "materials" and method == "DELETE":
                return ok(service.delete_material(pid, rest[1]))
            elif len(rest) == 3 and rest[0] == "materials" and rest[2] == "text" and method == "GET":
                return ok(service.material_text(pid, rest[1], query))
            elif rest == ["items", "confirm-batch"] and method == "POST":
                return ok(service.confirm_items(pid, body))
            elif len(rest) == 3 and rest[0] == "items" and rest[2] == "confirm" and method == "POST":
                return ok(service.confirm_item(pid, rest[1], body))
            elif rest == ["run"] and method == "GET":
                return ok(service.guided_run(pid))
            elif rest == ["run", "report"] and method == "GET":
                return ok(service.guided_report(pid))
            elif rest == ["run", "rehearsal"] and method in ("GET", "POST"):
                return ok(service.rehearsal(pid, persist=method == "POST"))
            elif rest == ["run", "next"] and method == "POST":
                return ok(service.start_guided_step(pid))
            elif len(rest) == 4 and rest[:2] == ["run", "steps"] and rest[3] == "start" and method == "POST":
                return ok(service.start_guided_step(pid, rest[2]))
            elif rest == ["run", "poll"] and method == "POST":
                return ok(service.poll_guided_step(pid))
            elif rest == ["run", "reset"] and method == "POST":
                return ok(service.reset_guided_run(pid, from_step=body.get("fromStep")))
            elif rest == ["validate"] and method == "POST":
                dry_build = body.get("dryBuild", True)
                if not isinstance(dry_build, bool):
                    raise HttpError(400, "dryBuild must be a boolean")
                return ok(service.validate(pid, dry_build=dry_build))
            elif rest == ["validation"] and method == "GET":
                return ok(service.validation(pid))
            elif rest == ["calibrate"] and method == "POST":
                return ok(service.calibrate(pid, body))
            elif rest == ["build"] and method == "POST":
                return ok(service.build(pid, strict_prose=bool(body.get("strictProse", False))))
            elif rest == ["release"] and method == "GET":
                return ok(service.release_info(pid))
            elif rest == ["golden"] and method == "GET":
                return ok(service.golden(pid, include_holdout=query.get("instructor") == "1"))
            elif rest == ["export"] and method == "GET":
                data, filename = service.export_zip(pid, bundle=query.get("bundle", "release"))
                return 200, None, data, {"Content-Type": "application/zip", "Content-Disposition": f'attachment; filename="{filename}"'}
            elif rest == ["guide"] and method == "GET":
                audience = query.get("audience", "student")
                if query.get("download") == "1":
                    data, filename = service.guide_download(pid, audience)
                    return 200, None, data, {"Content-Type": "text/markdown; charset=utf-8", "Content-Disposition": f'attachment; filename="{filename}"'}
                return ok(service.guide(pid, audience))
            elif rest == ["guide", "preview"] and method == "GET":
                return ok(service.guide_preview(pid, query.get("audience", "student")))
            elif rest == ["target"] and method == "PUT":
                return ok(service.put_target(pid, body))
            elif rest == ["sync", "preflight"] and method == "POST":
                return ok(service.sync_preflight(pid))
            elif rest == ["sync", "apply"] and method == "POST":
                return ok(service.sync_apply(pid, body))
            elif len(rest) == 2 and rest[0] == "sync" and rest[1] in ("commit", "rollback", "status") and method == "POST":
                return ok(service.sync_action(pid, rest[1], body))
            elif len(rest) == 3 and rest[0] == "sync" and rest[2] == "job" and method == "POST":
                return ok(service.start_sync_action_job(pid, rest[1], body), 202)
            elif len(rest) == 3 and rest[0] == "sync" and rest[1] in service.SYNC_JOB_ACTIONS and rest[2] == "job" and method == "GET":
                return ok({"job": service.jobs.latest(pid, f"sync-{rest[1]}")})
            elif rest == ["sync", "history"] and method == "GET":
                return ok({"history": service.sync_history(pid)})
            elif rest == ["direct"] and method == "POST":
                return ok(service.start_direct(pid, body), 202)
            elif rest == ["direct"] and method == "GET":
                return ok(service.direct_status(pid))
            elif rest == ["direct", "chat"] and method == "POST":
                return ok(service.start_direct_chat(pid, body), 202)
            elif len(rest) == 3 and rest[:2] == ["direct", "chat"] and method == "GET":
                return ok(service.direct_chat(pid, rest[2]))
            elif rest == ["direct", "cleanup"] and method == "POST":
                return ok(service.start_direct_cleanup(pid, body), 202)
            elif rest == ["oneclick"] and method == "POST":
                return ok(service.start_oneclick(pid, body), 202)
            elif rest == ["oneclick"] and method == "GET":
                return ok({"job": service.jobs.latest(pid, "oneclick")})
            elif len(rest) == 2 and rest[0] == "jobs" and method == "GET":
                return ok({"job": service.jobs.get(pid, rest[1])})
    raise HttpError(404, f"no route for {method} {path}")


def create_server(data_dir: Path, *, port: int = 0, home: Path | None = None, proxy_secret: str = "", clients_factory: Callable[[Any], Any] | None = None,
                  session_factory: Callable[..., Any] | None = None) -> tuple[ThreadingHTTPServer, Service]:
    """The App's backend; ``server.console`` is its console mount (``session_factory``: the console's boto3 sessions)."""
    service = Service(data_dir, home=home, clients_factory=clients_factory)
    console = ConsoleMount(service, session_factory=session_factory)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(service, proxy_secret, console))
    server.daemon_threads = True
    server.console = console  # type: ignore[attr-defined]
    return server, service


def default_data_dir() -> Path:
    env = os.environ.get("WORKSHOP_CUSTOMIZER_DATA")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".kiro" / "crew" / "apps" / APP_NAME / "data"


def _reexec_into_checkout_venv(data_dir: Path) -> None:
    """The gateway's bundled Python lacks jsonschema; the checkout's .venv has the engine deps."""
    try:
        import jsonschema  # noqa: F401
        import yaml  # noqa: F401
        return
    except ImportError:
        pass
    home = resolve_home(data_dir)
    if not home:
        return
    venv_py = home / ".venv" / "bin" / "python3"
    if venv_py.is_file() and os.access(venv_py, os.X_OK) and Path(sys.executable).resolve() != venv_py.resolve() and os.environ.get("WORKSHOP_CUSTOMIZER_REEXEC") != "1":
        env = {**os.environ, "WORKSHOP_CUSTOMIZER_REEXEC": "1"}
        sys.stderr.write(f"{APP_NAME}: re-executing under {venv_py} (engine dependencies)\n")
        os.execve(str(venv_py), [str(venv_py), str(Path(__file__).resolve())], env)


def main() -> None:  # pragma: no cover - process entry point
    port = int(os.environ.get("PORT", "9100"))
    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    _reexec_into_checkout_venv(data_dir)
    server, service = create_server(data_dir, port=port, proxy_secret=os.environ.get("KIROCREW_PROXY_SECRET", ""))
    raw_port = os.environ.get("ADLC_PUBLIC_API_PORT", str(PUBLIC_API_PORT)).strip()
    start_public_api(server.console, -1 if raw_port in ("0", "off", "") else int(raw_port))  # type: ignore[attr-defined]
    sys.stderr.write(f"{APP_NAME} backend on 127.0.0.1:{port} (engine={'ok' if service.engine else 'MISSING: ' + service.engine_error}, home={service.home}, python={sys.executable})\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
