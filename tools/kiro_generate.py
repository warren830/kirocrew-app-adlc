"""Run the App's Kiro generation through the logged-in Kiro CLI, headless.

KiroCrew's ``ctx.spawn`` starts the App-registered agent ``workshop-customizer`` through the user's
Kiro CLI.  When the KiroCrew host cannot start App backends, this tool drives the SAME routes
functions against the same registered agent:

    routes.prepare_generation  ->  kiro-cli chat --no-interactive  ->  routes.complete_generation
                                                                   ->  routes.apply_generation (--apply)

It is the single kiro-cli runner in this repo; other drivers import ``run_kiro`` from here.

Single shot (the default).  One generation (``--mode draft|repair|regenerate``) of a project.
Without ``--data-dir`` / ``--project-dir`` it creates a throwaway project under ``<out>/appdata``
(never the KiroCrew data dir).  ``--apply`` writes the draft into the project exactly as the Apply
button does.  ``--out`` receives task.txt, stdout.txt, stderr.txt, bundle.json and report.json.

Acceptance loop (``--loop``; ``tools/e2e_generate.py`` is a thin wrapper).  The headless acceptance
driver for SPEC D11: it creates the project through the process backend's own ``Service`` (in
process, no HTTP, no AWS), optionally uploads customer materials and records their approval, then
runs rounds of

    prepare -> kiro-cli -> complete -> apply (acknowledged) -> Service.validate (draft mode, dry build)

starting with a draft and continuing with ``repair`` rounds while the validation still has
repairable errors or a live-learned teaching warning (``routes.LIVE_LEARNED_WARNING_CODES``: the
reasons the live control contrast did not reproduce), until it has none, ``--max-rounds`` is reached, or two rounds end with the same
findings (one escalation to ``regenerate`` of the suggested scopes, then a no-progress failure).
``--start-mode repair|regenerate`` (with ``--scope`` / ``--instructions`` for that first round) works
on an existing, reviewed project instead of starting over with a draft (``--instructions`` without it
is refused: a draft ignores them); ``--from-rehearsal`` (no argument) starts with a repair of the
blocking remediation of the current release's ``run/rehearsal.json``.
``--review-reference`` then batch-reviews every draft item of a *reference* pack as sa_synthetic
through ``Service.confirm_items`` (the SA's Batch review action, never a file edit; customer and
workshop confirmations stay the SA's job), and ``--build`` builds the release.  Every round keeps
its task, stdout, stderr, record and validation under ``<out>/rounds/NN-<mode>/``; ``report.json``
and ``report.md`` are the evidence log (rounds, findings per round, seconds, task sizes, editLog
with zero manual saves, hand-edited files, orphans).
``--pre-rehearse`` then runs the local pre-rehearsal of the build (tools/pre_rehearsal.py, Bedrock) and,
while it finds something and ``--pre-rounds`` are left, starts a repair (tools, prompts, golden) with its
findings as the instructions (whole findings in order, within ``routes.MAX_INSTRUCTIONS_CHARS``).  That
repair gets the rounds' own handling (invalid-output re-runs, the unsent ``--answers``, further repair
rounds within ``--max-rounds`` while blocking findings remain), then the review, a rebuild and another
pre-rehearsal (under ``<out>/pre-rehearsal/NN/``).
``--direct`` then rehearses the build in direct mode on AgentCore (engine ``direct``: the pack's own
knowledge base, tools, memory and Harness, the Workshop's judges, about 8 minutes) and, while it is not ready
and ``--direct-rounds`` are left, repairs from its remediation: routes reads the current build's
``direct-rehearsal.json`` as rehearsal findings, as it reads a Guided Run's ``run/rehearsal.json``. With
``--direct-repeat N`` each direct rehearsal is N rounds and is ready only when every round is (``direct.robust``):
the failing rounds' hints are then the findings. Each direct rehearsal is copied under ``<out>/direct/NN/``. A
direct verdict is never a class verdict.

Exit codes: 0 ok, 1 content failure (invalid output, needs input, apply refused, no progress,
rounds exhausted), 2 environment (live data dir, engine missing, the wrong AWS account), 3 Kiro,
pre-rehearsal or direct runtime failure (timeout, cannot start, non-zero exit without a usable result; a
pre-rehearsal or direct round that raised: no AWS credentials, an unknown profile, a Bedrock or AgentCore
error), 4 pre-rehearsal findings remain (the last pre-rehearsal still finds something after
``--pre-rounds`` repairs; report.json / report.md list them), 5 the last direct rehearsal is not ready after
``--direct-rounds`` repairs (or names nothing in the pack to repair).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app" / "backend"))

import routes  # noqa: E402

AGENT = routes.AGENT_NAME  # the agent "name" KiroCrew registered from app/agents/workshop-customizer.json
MAX_ARGV_TASK_BYTES = 900_000  # the task travels as one argv element; ARG_MAX is 1,048,576
LIVE_DATA_DIR = Path.home() / ".kiro" / "crew" / "apps" / routes.APP_NAME / "data"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def run_kiro(task: str, *, model: str | None = None, timeout: int, kiro_cli: str = "kiro-cli",
             agent: str = AGENT) -> tuple[str, str, int, float]:
    """One headless Kiro turn: (stdout, stderr, exit code, seconds), ANSI stripped."""
    if len(task.encode("utf-8")) > MAX_ARGV_TASK_BYTES:
        raise ValueError(f"task exceeds {MAX_ARGV_TASK_BYTES} bytes and cannot be passed as an argument")
    cmd = [*shlex.split(kiro_cli), "chat", "--no-interactive", "--agent", agent, "--trust-tools="]
    if model:
        cmd += ["--model", model]
    cmd.append(task)
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return _ANSI.sub("", proc.stdout), _ANSI.sub("", proc.stderr), proc.returncode, time.time() - started


def effective_timeout(requested: int | None, record: dict) -> int:
    """The kiro-cli timeout: ``--timeout`` capped at the per-mode maximum, else the record's own."""
    if requested is None:
        return int(record["timeoutSecs"])
    return max(1, min(routes.TIMEOUT_BOUNDS[1], int(requested)))


def stats(bundle: dict) -> dict:
    scenario = bundle["scenario"]
    ev = scenario.get("evaluation", {})
    retrieval = ev.get("retrievalToolName")
    cases = ev.get("goldenSet", [])
    practice = [c for c in cases if c.get("set") == "practice"]
    return {
        "status": bundle.get("status"),
        "language": scenario.get("language"),
        "namespace": scenario.get("namespace", {}).get("agentName"),
        "facts": len(scenario.get("facts", [])),
        "tools": [t.get("name") for t in scenario.get("tools", [])],
        "documents": len(scenario.get("knowledge", {}).get("documents", [])),
        "noiseDocuments": sum(1 for d in scenario.get("knowledge", {}).get("documents", []) if d.get("noise")),
        "golden": len(cases),
        "practice": len(practice),
        "holdout": len(cases) - len(practice),
        "practiceRetrieval": sum(1 for c in practice if retrieval in (c.get("expected", {}).get("requiredTools") or [])),
        "practiceActors": sorted({c.get("actorId") for c in practice}),
        "labsKeys": sorted((scenario.get("labs") or {}).keys()),
        "files": sorted(bundle.get("files", {})),
        "openQuestions": len(bundle.get("openQuestions", [])),
    }


def ensure_project(data_dir: Path, *, project_id: str, display_name: str, customer: str, pack_kind: str) -> Path:
    """A minimal blank project the routes functions accept; an existing one is reused as is."""
    pdir = data_dir / "projects" / project_id
    if (pdir / "project.json").is_file() and (pdir / "scenario.yaml").is_file():
        return pdir
    pdir.mkdir(parents=True, exist_ok=True)
    scenario = {"schemaVersion": 1, "id": project_id, "displayName": display_name, "packKind": pack_kind}
    (pdir / "scenario.yaml").write_text(json.dumps(scenario, indent=2) + "\n", encoding="utf-8")
    meta = {
        "id": project_id, "displayName": display_name, "customer": customer, "packKind": pack_kind,
        "template": "blank", "status": "intake", "createdAt": routes._utc_now(),
        "target": None, "lastValidation": None, "lastBuild": None, "lastSync": None,
    }
    (pdir / "project.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return pdir


def _finish(out: Path, report: dict, code: int) -> int:
    report["exit"] = code
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--brief", type=Path, help="the customer brief (a draft needs a brief, a stored brief or a source material)")
    parser.add_argument("--project-id", help="the project id (or give --project-dir)")
    parser.add_argument("--project-dir", type=Path, help="an App data project: <data dir>/projects/<project id>")
    parser.add_argument("--display-name", help="display name of a new project (default: the project id)")
    parser.add_argument("--customer", default="")
    parser.add_argument("--pack-kind", default="customer", choices=("customer", "reference", "workshop"))
    parser.add_argument("--data-dir", type=Path, help="existing App data dir holding the project (default: <out>/appdata)")
    parser.add_argument("--mode", default="draft", choices=routes.MODES, help="single shot: the generation mode")
    parser.add_argument("--scope", action="append", choices=routes.SCOPES,
                        help="repair/regenerate scope (repeatable): the single shot, or the loop's first round")
    parser.add_argument("--instructions", help="SA instructions for a repair or regenerate: the single shot, or the loop's first round "
                                               "(refused with a draft, which ignores them)")
    parser.add_argument("--start-mode", choices=routes.MODES, default="draft",
                        help="loop: the first round's mode; repair / regenerate work on the existing project and keep its "
                             "reviewed items (a draft starts the pack over)")
    parser.add_argument("--from-rehearsal", action="store_true",
                        help="loop: start with a repair of the current release's rehearsal remediation (run/rehearsal.json); "
                             "implies --start-mode repair")
    parser.add_argument("--apply", action="store_true", help="single shot: apply the ready generation to the project")
    parser.add_argument("--loop", action="store_true", help="run the acceptance loop (draft, validate, repair rounds)")
    parser.add_argument("--max-rounds", type=int, default=3, help="loop: generation rounds before giving up (default 3)")
    parser.add_argument("--retry-invalid", type=int, default=2,
                        help="loop: re-run a round whose Kiro output is unusable (for example invalid JSON), at most N times (default 2)")
    parser.add_argument("--materials", type=Path,
                        help="loop: a directory of customer materials; optional materials.yaml/json "
                             "{files:{name:{generationUse,author,notes}}, approval:{ref,dataClassification}}")
    parser.add_argument("--answers", type=Path,
                        help="loop: JSON [{question, answer}] sent once when Kiro asks questions (needs-input)")
    parser.add_argument("--review-reference", action="store_true",
                        help="loop: batch-review every draft item of a reference pack as sa_synthetic (Service.confirm_items)")
    parser.add_argument("--build", action="store_true", help="loop: build the release once the validation is ok")
    parser.add_argument("--pre-rehearse", action="store_true",
                        help="loop: after the build, run the local pre-rehearsal (tools/pre_rehearsal.py) and repair what "
                             "it finds (an L1 phenomenon that broke in a run, an empty or looping optimized reply) before any AWS run")
    parser.add_argument("--aws-profile", help="pre-rehearse: the AWS profile for Bedrock (Nova 2 Lite, Titan embeddings)")
    parser.add_argument("--aws-region", default="us-west-2")
    parser.add_argument("--pre-rounds", type=int, default=2,
                        help="pre-rehearse: repairs it may start, each with up to --max-rounds rounds (default 2; 0 only "
                             "reports); findings left after the last one exit 4")
    parser.add_argument("--pre-repeat", type=int, default=2, help="pre-rehearse: runs per question, at least 1 (default 2)")
    parser.add_argument("--direct", action="store_true",
                        help="loop: after the build (and the pre-rehearsal), rehearse the pack in direct mode on AgentCore "
                             "(tools/direct_run.py, about 8 min) and repair what its remediation names; needs --aws-profile "
                             "and --aws-account")
    parser.add_argument("--aws-account", help="direct: the AWS account the profile must resolve to")
    parser.add_argument("--direct-rounds", type=int, default=2,
                        help="direct: repairs a not-ready direct rehearsal may start (default 2); not ready after the last "
                             "one exits 5")
    parser.add_argument("--direct-repeat", type=int, default=1,
                        help="direct: rounds per direct rehearsal on the same release (default 1); with more, every round "
                             "must be ready, and a phenomenon that reproduces in some rounds only is repaired too")
    parser.add_argument("--allow-live-data-dir", action="store_true", help="permit the KiroCrew App data dir (both modes)")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int,
                        help="seconds, at most %d (default: the generation record's per-mode timeout); "
                             "recorded as the record's timeoutSecs" % routes.TIMEOUT_BOUNDS[1])
    parser.add_argument("--kiro-cli", default="kiro-cli", help="kiro-cli command (split like a shell word list)")
    parser.add_argument("--out", type=Path, required=True)
    return parser


def _resolve_project(parser: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[Path | None, str]:
    if args.project_dir is not None:
        pdir = args.project_dir.expanduser().resolve()
        if pdir.parent.name != "projects":
            parser.error("--project-dir must be <data dir>/projects/<project id>")
        if args.project_id and args.project_id != pdir.name:
            parser.error("--project-id differs from the --project-dir name")
        if args.data_dir is not None and args.data_dir.expanduser().resolve() != pdir.parent.parent:
            parser.error("--data-dir differs from the --project-dir data dir")
        return pdir.parent.parent, pdir.name
    if not args.project_id:
        parser.error("give --project-id or --project-dir")
    return args.data_dir, args.project_id


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be a positive number of seconds")
    if args.max_rounds < 1:
        parser.error("--max-rounds must be at least 1")
    if args.pre_rounds < 0:
        parser.error("--pre-rounds must be 0 or more")
    if args.pre_repeat < 1:
        parser.error("--pre-repeat must be at least 1")
    if args.from_rehearsal:
        if args.start_mode not in ("draft", "repair"):
            parser.error("--from-rehearsal starts with a repair")
        args.start_mode = "repair"
    if args.direct and not (args.loop and args.build and args.aws_profile and args.aws_account):
        parser.error("--direct belongs to --loop --build and needs --aws-profile and --aws-account")
    if args.direct and not args.pre_rehearse:
        # Live 2026-10-01 (loyalty): three direct rounds read a gap as reproduced; the Guided Run did not, and the local
        # pre-rehearsal (nine sampled first searches, one minute) had predicted it likely_not_reproduced.
        print("warning: --direct without --pre-rehearse: the local prediction samples a gap's first search nine times; "
              "three direct rounds can miss a gap that fails one class in four", file=sys.stderr)
    if (args.from_rehearsal or args.start_mode != "draft") and not args.loop:
        parser.error("--start-mode / --from-rehearsal belong to --loop (a single shot takes --mode)")
    if args.loop and args.start_mode == "draft" and args.scope:
        parser.error("a draft covers the whole pack; --scope needs --start-mode repair or regenerate")
    # A draft never reads SA instructions (only a repair / regenerate task carries them) and starts the pack
    # over, resetting the SA's reviews: refuse the combination instead of dropping the instructions silently.
    if args.pre_rehearse and not (args.loop and args.build and args.aws_profile):
        parser.error("--pre-rehearse needs --loop, --build and --aws-profile")
    if args.instructions and (args.start_mode if args.loop else args.mode) == "draft":
        parser.error("--instructions steer a repair or regenerate, and a draft ignores them (it starts the pack over "
                     "and resets the SA's reviews); use " + ("--start-mode regenerate --scope ... or --start-mode repair"
                                                              if args.loop else "--mode regenerate --scope ... or --mode repair"))
    data_dir, project_id = _resolve_project(parser, args)
    args.out.mkdir(parents=True, exist_ok=True)
    # SPEC invariant 6: never write into the SA's live KiroCrew App data (a single shot writes a record and
    # the brief, and --apply rewrites the project) unless explicitly allowed. Both modes, exit code 2.
    live = (data_dir or (args.out / "appdata")).expanduser().resolve()
    if not args.allow_live_data_dir and live == LIVE_DATA_DIR.resolve():
        report = {"agent": AGENT, "dataDir": str(live), "projectId": project_id,
                  "error": f"refusing the live KiroCrew App data dir {LIVE_DATA_DIR}; pass --allow-live-data-dir to use it"}
        return _finish(args.out, report, 2)
    if args.loop:
        return run_loop(args, data_dir=data_dir or (args.out / "appdata"), project_id=project_id)
    return run_single(args, data_dir=data_dir, project_id=project_id)


def run_single(args: argparse.Namespace, *, data_dir: Path | None, project_id: str) -> int:
    brief = args.brief.read_text(encoding="utf-8") if args.brief else ""
    if data_dir is None:
        data_dir = args.out / "appdata"
        ensure_project(data_dir, project_id=project_id, display_name=args.display_name or project_id,
                       customer=args.customer, pack_kind=args.pack_kind)
    report: dict = {"agent": AGENT, "dataDir": str(data_dir), "projectId": project_id}
    body: dict = {"projectId": project_id, "mode": args.mode}
    if brief:
        body["brief"] = brief
    if args.scope:
        body["scope"] = args.scope
    if args.instructions:
        body["instructions"] = args.instructions
    try:
        record, task = routes.prepare_generation(data_dir, body, runner="kiro-cli")
    except routes.GenerationError as exc:
        report["error"] = str(exc)
        return _finish(args.out, report, 1)
    (args.out / "task.txt").write_text(task, encoding="utf-8")
    timeout = effective_timeout(args.timeout, record)
    report.update(generationId=record["id"], mode=record["mode"], timeoutSecs=timeout, task=record["task"],
                  contracts=record["contracts"], taskChars=len(task))

    # The record carries the timeout this run really uses, so the app's stale rule (timeoutSecs +
    # grace) never abandons it, and lets a second generation start, while kiro-cli is still inside it.
    routes.mark_running(data_dir, record, timeout_secs=timeout)
    try:
        stdout, stderr, code, elapsed = run_kiro(task, model=args.model, timeout=timeout, kiro_cli=args.kiro_cli)
    except subprocess.TimeoutExpired:
        routes.fail_generation(data_dir, record, f"Kiro CLI timed out after {timeout}s")
        report["error"] = f"Kiro CLI timed out after {timeout}s"
        return _finish(args.out, report, 3)
    except (OSError, ValueError) as exc:
        routes.fail_generation(data_dir, record, f"Kiro CLI could not run: {exc}")
        report["error"] = f"Kiro CLI could not run: {exc}"
        return _finish(args.out, report, 3)
    (args.out / "stdout.txt").write_text(stdout, encoding="utf-8")
    (args.out / "stderr.txt").write_text(stderr, encoding="utf-8")
    report.update(exitCode=code, elapsedSeconds=round(elapsed, 1), stdoutChars=len(stdout))

    record = routes.complete_generation(record, stdout, data_dir=data_dir)
    report["status"] = record["status"]
    if record["status"] == "failed":
        report["error"] = record.get("error")
        return _finish(args.out, report, 3 if code else 1)
    bundle = record["result"]
    (args.out / "bundle.json").write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")
    if record["status"] == "needs-input":
        report["openQuestions"] = bundle.get("openQuestions", [])
        return _finish(args.out, report, 1)
    report["stats"] = stats(bundle)
    report["warnings"] = bundle.get("warnings", [])
    report["provenance"] = (bundle.get("diff") or {}).get("provenance")
    if args.apply:
        try:
            report["apply"] = routes.apply_generation(record["id"], data_dir, {"acknowledged": True})
        except routes.GenerationError as exc:
            report["error"] = f"apply refused: {exc}"
            return _finish(args.out, report, 1)
    return _finish(args.out, report, 0)


# ---------------------------------------------------------------------------
# acceptance loop
# ---------------------------------------------------------------------------


class LoopFailure(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


class InvalidOutput(LoopFailure):
    """Kiro's result could not be used (invalid JSON, no markers, bad bundle): it exited cleanly, or its response
    stream broke off (:data:`STREAM_BROKE`)."""


#: The Kiro CLI's exit when the model's response stream breaks off mid-answer (live 2026-10-01: "Internal error
#: (code -32603): Encountered an error in the response stream: … dispatch failure", exit 1, half a JSON bundle):
#: transient, so the round is re-run like invalid output.
STREAM_BROKE = re.compile(r"error in the response stream|dispatch failure", re.I)


def load_server():
    """The process backend module, loaded by path (stdlib at top level; the engine loads lazily)."""
    spec = importlib.util.spec_from_file_location("workshop_customizer_app_server", REPO / "app" / "backend" / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _refuse_aws(_cfg):
    raise RuntimeError("the headless generation driver never talks to AWS")


def _materials_config(directory: Path) -> dict:
    for name in ("materials.yaml", "materials.yml", "materials.json"):
        path = directory / name
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".json":
                return json.loads(text)
            import yaml

            return yaml.safe_load(text) or {}
    return {}


def _upload_materials(service, server, project_id: str, directory: Path) -> list[dict]:
    """Upload every file of ``directory`` through Service.upload_material (the UI's upload route)."""
    config = _materials_config(directory)
    files = config.get("files") if isinstance(config.get("files"), dict) else {}
    approval = config.get("approval") if isinstance(config.get("approval"), dict) else None
    try:
        if approval:
            service.record_material_approval(project_id, {"approvalRef": approval.get("ref") or approval.get("approvalRef"),
                                                          "dataClassification": approval.get("dataClassification")})
        uploaded = []
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name.startswith(".") or path.name.startswith("materials."):
                continue
            options = files.get(path.name) if isinstance(files.get(path.name), dict) else {}
            body = {"name": path.name, "contentBase64": base64.b64encode(path.read_bytes()).decode("ascii"),
                    "acknowledged": True, "generationUse": options.get("generationUse", "source"),
                    "author": options.get("author", "customer"), "notes": options.get("notes", "")}
            record = service.upload_material(project_id, body)
            uploaded.append({"id": record["id"], "name": record["name"], "use": record["generationUse"],
                             "extraction": record["extraction"]["status"], "chars": record["extraction"]["chars"]})
    except server.HttpError as exc:
        raise LoopFailure(f"materials refused: {exc}", 1) from exc
    return uploaded


def _errors(validation: dict) -> list[dict]:
    return [f for f in (validation.get("repair") or {}).get("repairable", []) if f.get("severity") == "error"]


def _live_learned(validation: dict) -> list[dict]:
    """Repairable warnings the live control run showed break the class contrast (routes.LIVE_LEARNED_WARNING_CODES)."""
    return [f for f in (validation.get("repair") or {}).get("repairable", [])
            if f.get("severity") == "warning" and f.get("code") in routes.LIVE_LEARNED_WARNING_CODES]


def _blocking(validation: dict) -> list[dict]:
    """What the loop repairs before it accepts convergence: repairable errors plus live-learned warnings."""
    return _errors(validation) + _live_learned(validation)


def _signature(findings: list[dict]) -> list[str]:
    return sorted(f"{f.get('code')}|{f.get('path')}" for f in findings)


def _write_round(rdir: Path, name: str, payload) -> None:
    rdir.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    (rdir / name).write_text(text, encoding="utf-8")


def _one_round(args, *, data_dir: Path, project_id: str, service, number: int, mode: str, scope: list[str] | None,
               brief: str, answers: list[dict] | None, out: Path, instructions: str | None = None) -> tuple[dict, dict, dict]:
    """prepare -> kiro-cli -> complete -> apply -> validate; returns (round entry, record, validation)."""
    body: dict = {"projectId": project_id, "mode": mode}
    if mode == "draft" and brief:
        body["brief"] = brief
    if scope:
        body["scope"] = scope
    if instructions and mode != "draft":
        body["instructions"] = instructions
    if answers:
        body["answers"] = answers
    started = time.time()
    try:
        record, task = routes.prepare_generation(data_dir, body, runner="kiro-cli")
    except routes.GenerationError as exc:
        raise LoopFailure(f"round {number} ({mode}) refused: {exc}", 1) from exc
    rdir = out / "rounds" / f"{number:02d}-{mode}"
    _write_round(rdir, "task.txt", task)
    if hashlib.sha256(task.encode("utf-8")).hexdigest() != record["task"]["sha256"]:
        raise LoopFailure("the task sent differs from the recorded task", 2)
    timeout = effective_timeout(args.timeout, record)
    routes.mark_running(data_dir, record, timeout_secs=timeout)
    entry: dict = {"n": number, "mode": mode, "scope": record.get("scope"), "generationId": record["id"],
                   "rehearsalFindings": list((record.get("rehearsalRef") or {}).get("findingIds") or []),
                   "taskBytes": record["task"]["bytes"], "taskChars": record["task"]["chars"],
                   "estTokens": record["task"]["estTokens"], "taskSha256": record["task"]["sha256"],
                   "timeoutSecs": timeout, "materialsUsed": record.get("materialsUsed") or []}
    try:
        stdout, stderr, code, elapsed = run_kiro(task, model=args.model, timeout=timeout, kiro_cli=args.kiro_cli)
    except subprocess.TimeoutExpired as exc:
        routes.fail_generation(data_dir, record, f"Kiro CLI timed out after {timeout}s")
        raise LoopFailure(f"round {number}: Kiro CLI timed out after {timeout}s", 3) from exc
    except (OSError, ValueError) as exc:
        routes.fail_generation(data_dir, record, f"Kiro CLI could not run: {exc}")
        raise LoopFailure(f"round {number}: Kiro CLI could not run: {exc}", 3) from exc
    _write_round(rdir, "stdout.txt", stdout)
    _write_round(rdir, "stderr.txt", stderr)
    entry.update(kiroSeconds=round(elapsed, 1), kiroExitCode=code, stdoutChars=len(stdout))
    record = routes.complete_generation(record, stdout, data_dir=data_dir)
    _write_round(rdir, "record.json", record)
    entry["status"] = record["status"]
    if record["status"] == "failed":
        if code == 0 or STREAM_BROKE.search(stderr or ""):
            raise InvalidOutput(f"round {number}: {record.get('error')}", 1)
        raise LoopFailure(f"round {number}: {record.get('error')}", 3)
    if record["status"] == "needs-input":
        entry["openQuestions"] = (record.get("result") or {}).get("openQuestions", [])
        entry["seconds"] = round(time.time() - started, 1)
        return entry, record, {}
    result = record["result"]
    try:
        applied = routes.apply_generation(record["id"], data_dir, {"acknowledged": True, "acknowledgeConfirmationResets": False})
    except routes.GenerationError as exc:
        raise LoopFailure(f"round {number}: apply refused: {exc}", 1) from exc
    validation = service.validate(project_id, dry_build=True)
    _write_round(rdir, "validation.json", validation)
    errors = _errors(validation)
    live = _live_learned(validation)
    repair = validation.get("repair") or {}
    entry.update(
        filesChanged=len(result.get("changedFiles") or []), filesWritten=len(applied.get("written") or []),
        orphansDeleted=applied.get("deleted") or [], confirmationsReset=applied.get("confirmationsReset") or [],
        ignoredChanges=len(result.get("ignoredChanges") or []), warnings=len(result.get("warnings") or []),
        findings={"repairableErrors": len(errors), "codes": sorted({str(f.get("code")) for f in errors}),
                  "liveLearnedWarnings": sorted({str(f.get("code")) for f in live}),
                  "saOnly": len(repair.get("saOnly") or []), "engine": len(repair.get("engine") or []),
                  "suggestedScopes": repair.get("suggestedScopes") or []},
        validationOk=bool(validation.get("ok")), seconds=round(time.time() - started, 1),
    )
    return entry, record, validation


def _hand_edited(pdir: Path, record: dict | None) -> list[str]:
    """Referenced files whose bytes differ from the last applied generation's result.

    Bytes, not text: apply writes the result exactly (CRLF kept), and read_text() would turn CRLF into
    LF, so every CRLF file would look hand-edited."""
    files = ((record or {}).get("result") or {}).get("files") or {}
    edited = []
    for rel, text in sorted(files.items()):
        path = pdir / rel
        if not path.is_file() or path.read_bytes() != text.encode("utf-8"):
            edited.append(rel)
    return edited


def run_loop(args: argparse.Namespace, *, data_dir: Path, project_id: str) -> int:
    out = args.out
    report: dict = {"agent": AGENT, "dataDir": str(data_dir), "projectId": project_id, "packKind": args.pack_kind,
                    "maxRounds": args.max_rounds, "rounds": [], "simulatedSaReview": False}
    if not args.allow_live_data_dir and data_dir.expanduser().resolve() == LIVE_DATA_DIR.resolve():
        report["error"] = f"refusing the live KiroCrew App data dir {LIVE_DATA_DIR}; pass --allow-live-data-dir to use it"
        return _finish(out, report, 2)
    server = load_server()
    service = server.Service(data_dir, home=REPO, clients_factory=_refuse_aws)
    if not service.engine:
        report["error"] = f"engine unavailable: {service.engine_error}"
        return _finish(out, report, 2)
    brief = args.brief.read_text(encoding="utf-8") if args.brief else ""
    answers = json.loads(args.answers.read_text(encoding="utf-8")) if args.answers else None
    started = time.time()
    code = 0
    try:
        pdir = data_dir / "projects" / project_id
        if not (pdir / "project.json").is_file():
            service.create_project({"id": project_id, "template": "blank", "packKind": args.pack_kind,
                                    "displayName": args.display_name or project_id, "customer": args.customer})
            report["created"] = True
        report["packKind"] = service.store.read_meta(project_id).get("packKind")
        if args.materials:
            report["materials"] = _upload_materials(service, server, project_id, args.materials)

        mode, scope = args.start_mode, (args.scope or None)
        instructions = args.instructions if mode != "draft" else None
        if mode != "draft":
            # A repair / regenerate works on the existing, reviewed project (a draft would reset its reviews)
            # from a fresh validation, and --from-rehearsal from the rehearsal of the current build.
            if report.get("created"):
                raise LoopFailure(f"--start-mode {mode} needs an existing project (give --project-dir)", 1)
            if args.from_rehearsal:
                rehearsal = routes.current_rehearsal(pdir, service.store.read_meta(project_id))
                if rehearsal is None:
                    raise LoopFailure("--from-rehearsal: run/rehearsal.json does not judge the current build; build, sync, "
                                      "run the Guided Run and rehearse this release first", 1)
                if not routes._rehearsal_findings(rehearsal):
                    raise LoopFailure("--from-rehearsal: the rehearsal names no project asset to repair (its blocking hints "
                                      "are environment steps); follow them in the Workshop environment instead", 1)
            _write_round(out / "rounds", "start-validation.json", service.validate(project_id, dry_build=True))
        run = _Run(args=args, data_dir=data_dir, project_id=project_id, service=service, server=server, brief=brief,
                   report=report, answers=answers)
        validation = _converge(run, mode=mode, scope=scope, instructions=instructions)
        report["converged"] = True
        _review_and_build(run, validation, name="review-validation.json")
        if args.pre_rehearse:
            _pre_rehearsal_rounds(run)
        if args.direct:
            _direct_rounds(run)
    except LoopFailure as exc:
        report["error"] = str(exc)
        code = exc.code
    except server.HttpError as exc:
        report["error"] = f"app refused: {exc}"
        code = 1
    report["seconds"] = round(time.time() - started, 1)
    report["final"] = _final(service, project_id, data_dir)
    final = report["final"]
    if code == 0 and (final["editLog"]["manualScenarioSaves"] or final["editLog"]["manualFileSaves"]
                      or final["handEditedFiles"] or final["orphans"]):
        report["error"] = "the project carries manual edits or orphans; this is not a KiroCrew-only result"
        code = 1
    _write_markdown(out, report)
    return _finish(out, report, code)


class _Run:
    """What the rounds of one loop run share: ``number`` is the last round's number and ``answers`` the
    --answers not sent yet (a run sends them once, in whichever round first needs input).

    A plain class: the tests load this module by path, outside sys.modules, where @dataclass cannot resolve it."""

    def __init__(self, *, args: argparse.Namespace, data_dir: Path, project_id: str, service: Any, server: Any, brief: str,
                 report: dict, answers: list[dict] | None):
        self.args, self.data_dir, self.project_id, self.service, self.server = args, data_dir, project_id, service, server
        self.brief, self.report, self.answers, self.number = brief, report, answers, 0


def _generate(run: _Run, *, start: int, mode: str, scope: list[str] | None, instructions: str | None,
              answers: list[dict] | None = None, tag: dict | None = None) -> tuple[dict, dict, dict]:
    """One round (``_one_round``), appended to report["rounds"] with ``tag``; a round whose Kiro output is unusable
    is recorded and re-run, at most --retry-invalid times in the phase (the rounds from ``start``)."""
    rounds = run.report["rounds"]
    while True:
        run.number += 1
        try:
            entry, record, validation = _one_round(run.args, data_dir=run.data_dir, project_id=run.project_id,
                                                   service=run.service, number=run.number, mode=mode, scope=scope,
                                                   brief=run.brief, answers=answers, out=run.args.out,
                                                   instructions=instructions)
        except InvalidOutput as exc:
            # Live 2026-09-28: a draft came back with mismatched brackets; re-run the same round.
            rounds.append({"n": run.number, "mode": mode, "status": "invalid-output", "error": str(exc), **(tag or {})})
            if sum(1 for r in rounds[start:] if r.get("status") == "invalid-output") > run.args.retry_invalid:
                raise
            continue
        entry.update(tag or {})
        rounds.append(entry)
        return entry, record, validation


def _converge(run: _Run, *, mode: str, scope: list[str] | None, instructions: str | None, tag: dict | None = None) -> dict:
    """Rounds from ``mode`` / ``scope`` until the validation has no blocking finding; returns that validation.

    ``instructions`` steer the first round (its re-runs and its answered re-run); later rounds repair the
    validation's findings.  Raises LoopFailure when Kiro needs input and no answers are left, after --max-rounds
    generation rounds of this phase, or when the same findings remain after one escalation to ``regenerate``."""
    args, rounds = run.args, run.report["rounds"]
    start, escalated, previous = len(rounds), False, None
    while True:
        entry, record, validation = _generate(run, start=start, mode=mode, scope=scope, instructions=instructions, tag=tag)
        if record["status"] == "needs-input":
            if not run.answers:
                raise LoopFailure("Kiro needs input: " + "; ".join(entry.get("openQuestions") or []), 1)
            answers, run.answers = run.answers, None  # answers are sent once
            entry, record, validation = _generate(run, start=start, mode=mode, scope=scope, instructions=instructions,
                                                  answers=answers, tag=tag)
            if record["status"] == "needs-input":
                raise LoopFailure("Kiro still needs input after the answers", 1)
        instructions = None  # SA instructions steer the first round only
        # Converged only without repairable errors AND without a live-learned teaching warning: those
        # validate clean but are exactly why a contrast fails at rehearsal, after a live run.
        errors = _blocking(validation)
        if not errors:
            return validation
        generation_rounds = sum(1 for r in rounds[start:] if r.get("status") not in ("needs-input", "invalid-output"))
        if generation_rounds >= args.max_rounds + (1 if escalated else 0):
            raise LoopFailure(f"repairable errors or live-learned teaching warnings remain after {generation_rounds} rounds: "
                              + ", ".join(sorted({str(f.get('code')) for f in errors})), 1)
        signature = _signature(errors)
        if signature == previous:
            if escalated:
                raise LoopFailure("no progress: the same findings remain after an escalation to regenerate: "
                                  + ", ".join(sorted({str(f.get('code')) for f in errors})), 1)
            suggested = (validation.get("repair") or {}).get("suggestedScopes") or []
            blocking_scopes = [s for s in routes.SCOPES if s in suggested or any(s in (f.get("scopes") or []) for f in errors)]
            mode, scope, escalated = "regenerate", blocking_scopes or None, True
        else:
            mode, scope = "repair", None
        previous = signature


def _review_and_build(run: _Run, validation: dict, *, name: str) -> None:
    """--review-reference (then a fresh validation, kept as rounds/<name>) and --build of the converged project."""
    args, report, service, server = run.args, run.report, run.service, run.server
    if args.review_reference:
        report["review"] = _review_reference(service, server, run.project_id)
        validation = service.validate(run.project_id, dry_build=True)
        _write_round(args.out / "rounds", name, validation)
    if args.build:
        if not validation.get("ok"):
            raise LoopFailure("the validation is not ok; build skipped", 1)
        try:
            summary = service.build(run.project_id)
        except server.HttpError as exc:
            raise LoopFailure(f"build refused: {exc}", 1) from exc
        report["build"] = {"version": summary["version"], "files": summary["files"], "contentHash": summary["contentHash"]}


def _pre_rehearsal_instructions(findings: list[str], *, repeat: int) -> tuple[str, int]:
    """(the SA instructions of a pre-rehearsal repair, how many findings they leave out).

    Whole findings in order, as many as fit routes.MAX_INSTRUCTIONS_CHARS (prepare_generation refuses longer
    instructions), then a line saying how many were left out."""
    limit = routes.MAX_INSTRUCTIONS_CHARS

    def text(kept: int) -> str:
        left = len(findings) - kept
        return "\n".join(["A local pre-rehearsal ran every practice question on the Workshop model "
                          f"({repeat} runs each). Fix these before the AWS run:"]
                         + [f"P{i}. {f}" for i, f in enumerate(findings[:kept], 1)]
                         + ([f"... and {left} more finding(s) left out: the instructions are limited to {limit} characters."]
                            if left else []))

    kept = 0
    while kept < len(findings) and len(text(kept + 1)) <= limit:
        kept += 1
    return text(kept), len(findings) - kept


def _pre_rehearse(args, project_dir: Path, *, attempt: int) -> dict:
    """tools/pre_rehearsal.py's pre_rehearse; whatever it raises is this run's runtime failure (exit 3)."""
    tools = str(Path(__file__).resolve().parent)
    try:
        if tools not in sys.path:
            sys.path.insert(0, tools)
        import pre_rehearsal as pre  # tools/pre_rehearsal.py

        return pre.pre_rehearse(project_dir, profile=args.aws_profile, region=args.aws_region, repeat=args.pre_repeat,
                                out=args.out / "pre-rehearsal" / f"{attempt:02d}")
    except Exception as exc:  # botocore: no credentials, an unknown profile, access denied, retries spent; a compile error
        raise LoopFailure(f"pre-rehearsal {attempt} failed: {type(exc).__name__}: {exc}", 3) from exc


def _pre_rehearsal_rounds(run: _Run) -> None:
    """Pre-rehearse the build; while it finds something and --pre-rounds are left, repair with its findings (through
    ``_converge``, so the repair gets the rounds' own handling), review, rebuild and pre-rehearse again.  Findings
    left after the last repair are exit 4."""
    args, report = run.args, run.report
    project_dir = run.data_dir / "projects" / run.project_id
    report["preRehearsals"] = []
    for attempt in range(1, args.pre_rounds + 2):
        doc = _pre_rehearse(args, project_dir, attempt=attempt)
        findings = list(doc["findings"])
        report["preRehearsals"].append({"prediction": doc["prediction"], "findings": findings, "seconds": doc["seconds"],
                                        "usage": doc["usage"], "build": (report.get("build") or {}).get("version")})
        if not findings:
            return
        if attempt > args.pre_rounds:
            raise LoopFailure(f"pre-rehearsal findings remain: pre-rehearsal {attempt} ({doc['prediction']}) still finds "
                              f"{len(findings)} after {args.pre_rounds} repair(s); fix them before the AWS run", 4)
        instructions, omitted = _pre_rehearsal_instructions(findings, repeat=args.pre_repeat)
        report["preRehearsals"][-1]["omittedFindings"] = omitted
        try:
            from workshop_customizer.pre_rehearsal import finding_scopes

            validation = _converge(run, mode="repair", scope=finding_scopes(doc), instructions=instructions,
                                   tag={"preRehearsal": attempt, "preRehearsalFindings": len(findings)})
            _review_and_build(run, validation, name=f"pre-rehearsal-{attempt:02d}-review-validation.json")
        except LoopFailure as exc:
            raise LoopFailure(f"pre-rehearsal {attempt} repair: {exc}", exc.code) from exc


def _direct_rehearse(args, project_dir: Path, *, attempt: int) -> dict:
    """One direct round (engine direct.run) of the current build; whatever it raises is a runtime failure (exit 3)."""
    try:
        import boto3

        from workshop_customizer.direct.run import DirectRun

        session = boto3.Session(profile_name=args.aws_profile, region_name=args.aws_region)
        if session.client("sts").get_caller_identity()["Account"] != args.aws_account:
            raise LoopFailure("direct: the AWS profile resolves to another account than --aws-account", 2)
        version = json.loads((project_dir / "build" / "release" / "RELEASE.json").read_text(encoding="utf-8"))["version"]
        out = project_dir / "build" / "direct" / str(version)
        log_lines: list[str] = []
        doc = DirectRun(project_dir, session=session, profile=args.aws_profile, account=args.aws_account, region=args.aws_region,
                        out=out, log=lambda s: (log_lines.append(s), print(f"  direct {attempt}: {s}", flush=True)),
                        repeat=args.direct_repeat).run()
        copy = args.out / "direct" / f"{attempt:02d}"
        copy.mkdir(parents=True, exist_ok=True)
        for name in ("direct-rehearsal.json", "direct-rehearsal.md"):
            (copy / name).write_bytes((out / name).read_bytes())
        (copy / "log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return doc
    except LoopFailure:
        raise
    except Exception as exc:  # noqa: BLE001 - AWS (credentials, quota, a resource that failed), the judges
        raise LoopFailure(f"direct {attempt} failed: {type(exc).__name__}: {exc}", 3) from exc


def _direct_rounds(run: _Run) -> None:
    """Rehearse the build in direct mode; while it is not ready and --direct-rounds are left, repair from its
    remediation (routes reads the current build's direct-rehearsal.json as rehearsal findings), review, rebuild
    and rehearse again. Not ready after the last repair is exit 5."""
    args, report = run.args, run.report
    project_dir = run.data_dir / "projects" / run.project_id
    report["directRehearsals"] = []
    for attempt in range(1, args.direct_rounds + 2):
        doc = _direct_rehearse(args, project_dir, attempt=attempt)
        blocking = [f"{h['code']} ({h.get('caseId') or '—'})" for h in doc.get("remediation") or []
                    if h.get("severity") == "blocking" and h.get("code") != "RELEASE_NOT_VERIFIED"]
        direct = doc.get("direct") or {}
        report["directRehearsals"].append({"verdict": doc["verdict"], "reasonCode": doc.get("reasonCode"), "blocking": blocking,
                                           "phenomena": {p["id"]: p["verdict"] for p in doc.get("phenomena") or []},
                                           "seconds": direct.get("seconds"), "build": (report.get("build") or {}).get("version"),
                                           **({"robust": direct.get("robust"), "flaky": direct.get("flaky") or []} if "robust" in direct else {})})
        # With --direct-repeat every round must be ready: a phenomenon that reproduces in some rounds only is repaired too.
        if doc["verdict"] == "ready" and direct.get("robust", True):
            return
        if attempt > args.direct_rounds:
            said = doc["verdict"] if doc["verdict"] != "ready" else f"ready but not robust ({', '.join(direct.get('flaky') or [])})"
            raise LoopFailure(f"direct rehearsal {attempt} is {said} ({doc.get('reasonCode')}) after {args.direct_rounds} "
                              f"repair(s): {', '.join(blocking) or 'no blocking remediation'}", 5)
        meta = run.service.store.read_meta(run.project_id)
        if not routes._rehearsal_findings(routes.current_rehearsal(project_dir, meta)):
            raise LoopFailure(f"direct rehearsal {attempt} is {doc['verdict']} and names no project asset to repair "
                              f"({', '.join(blocking) or 'environment hints only'})", 5)
        try:
            validation = _converge(run, mode="repair", scope=None, instructions=None,
                                   tag={"directRehearsal": attempt, "directVerdict": doc["verdict"]})
            _review_and_build(run, validation, name=f"direct-{attempt:02d}-review-validation.json")
        except LoopFailure as exc:
            raise LoopFailure(f"direct {attempt} repair: {exc}", exc.code) from exc


def _review_reference(service, server, project_id: str) -> dict:
    """The SA's Batch review of a reference (fictional) pack: every draft item becomes sa_synthetic."""
    listing = service.list_items(project_id)
    if listing.get("packKind") != "reference":
        return {"skipped": f"packKind {listing.get('packKind')}: customer confirmations are the SA's job"}
    ids = [i["id"] for i in listing["items"] if i["provenance"] in ("ai_draft", "pending") and i["confirmable"]]
    if not ids:
        return {"reviewed": 0}
    try:
        result = service.confirm_items(project_id, {"ids": ids, "acknowledged": True})
    except server.HttpError as exc:
        raise LoopFailure(f"batch review refused: {exc}", 1) from exc
    return {"reviewed": result["confirmed"], "provenance": result["provenance"]}


def _final(service, project_id: str, data_dir: Path) -> dict:
    pdir = data_dir / "projects" / project_id
    if not (pdir / "project.json").is_file():
        return {"validationOk": False, "repairableErrors": 0, "saOnly": 0, "anchors": None, "classes": None,
                "editLog": {k: 0 for k in routes.EDIT_LOG_KEYS}, "handEditedFiles": [], "orphans": [], "lastGeneration": None}
    meta = service.get_project(project_id)
    validation = service.validation(project_id).get("validation") or {}
    last = None
    last_id = (meta.get("lastGeneration") or {}).get("id")
    if last_id:
        try:
            last = routes._read_record(data_dir, str(last_id))
        except routes.GenerationError:
            last = None
    scenario = routes._parse_scenario_text((pdir / "scenario.yaml").read_text(encoding="utf-8"))
    repair = validation.get("repair") or {}
    return {
        "validationOk": bool(validation.get("ok")),
        "repairableErrors": len(_errors(validation)),
        "saOnly": len(repair.get("saOnly") or []),
        "anchors": validation.get("anchors"),
        "classes": validation.get("classes"),
        "editLog": meta.get("editLog") or {k: 0 for k in routes.EDIT_LOG_KEYS},
        "handEditedFiles": _hand_edited(pdir, last),
        "orphans": routes.orphan_files(pdir, scenario)[0],
        "lastGeneration": meta.get("lastGeneration"),
    }


def _write_markdown(out: Path, report: dict) -> None:
    lines = [f"# Headless generation — {report['projectId']}", "",
             f"- Agent: `{report['agent']}`  Pack kind: `{report.get('packKind')}`  Seconds: {report.get('seconds')}",
             f"- Result: {'error: ' + report['error'] if report.get('error') else 'ok'}", "",
             "| round | mode | scope | seconds | task bytes | est. tokens | status | repairable errors | codes | files changed | orphans deleted |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in report["rounds"]:
        findings = r.get("findings") or {}
        lines.append(f"| {r['n']} | {r['mode']} | {', '.join(r.get('scope') or []) or 'all'} | {r.get('seconds')} | {r.get('taskBytes', '')} | "
                     f"{r.get('estTokens', '')} | {r.get('status')} | {findings.get('repairableErrors', '')} | {', '.join(findings.get('codes') or [])} | "
                     f"{r.get('filesChanged', '')} | {len(r.get('orphansDeleted') or [])} |")
    final = report.get("final") or {}
    lines += ["", "## Final", "", f"- Validation ok: {final.get('validationOk')}; repairable errors: {final.get('repairableErrors')}; "
              f"SA-only findings: {final.get('saOnly')}",
              f"- editLog: {json.dumps(final.get('editLog'))}",
              f"- Hand-edited files: {final.get('handEditedFiles')}; orphans: {final.get('orphans')}"]
    if report.get("review"):
        lines.append(f"- Batch review: {json.dumps(report['review'])}")
    if report.get("build"):
        lines.append(f"- Build: `{report['build']['version']}`")
    for i, pre in enumerate(report.get("preRehearsals") or [], 1):
        repaired = [str(r["n"]) for r in report["rounds"] if r.get("preRehearsal") == i]
        lines.append(f"- Pre-rehearsal {i} (`{pre.get('build')}`): {pre['prediction']}, {len(pre['findings'])} finding(s), {pre['seconds']} s"
                     + (f"; repair rounds {', '.join(repaired)}" if repaired else "")
                     + (f" ({pre['omittedFindings']} left out of the instructions: {routes.MAX_INSTRUCTIONS_CHARS} characters)"
                        if pre.get("omittedFindings") else "")
                     + "".join(f"\n  - {f}" for f in pre["findings"]))
    for i, direct in enumerate(report.get("directRehearsals") or [], 1):
        repaired = [str(r["n"]) for r in report["rounds"] if r.get("directRehearsal") == i]
        robust = "" if "robust" not in direct else ("; every round ready" if direct["robust"] else
                                                    f"; not robust ({', '.join(direct.get('flaky') or []) or 'a round failed'})")
        lines.append(f"- Direct rehearsal {i} (`{direct.get('build')}`): {direct['verdict']} ({direct.get('reasonCode')}), "
                     f"{direct.get('seconds')} s{robust}" + (f"; repair rounds {', '.join(repaired)}" if repaired else "")
                     + "".join(f"\n  - {b}" for b in direct.get("blocking") or []))
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
