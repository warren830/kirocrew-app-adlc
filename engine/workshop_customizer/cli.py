"""Command-line interface for the Workshop Customizer engine.

    python -m workshop_customizer validate       scenarios/<id>/scenario.yaml
    python -m workshop_customizer compile        scenarios/<id>/scenario.yaml --out build/<id>
    python -m workshop_customizer build-release  scenarios/<id>/scenario.yaml --out build/<id>
    python -m workshop_customizer verify-release build/<id>/release
    python -m workshop_customizer rehearsal      <project-dir | scenario.yaml --run-state state.json>
    python -m workshop_customizer guide          scenarios/<id>/scenario.yaml --audience student [--out -] [--draft]

Every command exits non-zero on any error or blocking finding; ``--json`` prints a machine
readable summary (used by the KiroCrew app backend and CI).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from . import __version__, compiler, guide, rehearsal, render, teaching
from .scenario import ScenarioError, load_scenario
from .validator import PackValidationError, validate_scenario_policy


def repo_root() -> Path:
    """Root of the workshop-customizer checkout (overridable for installed copies)."""
    env = os.environ.get("WORKSHOP_CUSTOMIZER_HOME")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2]


def read_template_lock(path: Path | None) -> dict[str, Any]:
    lock_path = path or repo_root() / "template-lock.json"
    if not lock_path.is_file():
        raise ScenarioError(f"template-lock.json not found: {lock_path}")
    return json.loads(lock_path.read_text(encoding="utf-8"))


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            print(f"{key}:")
            text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
            print("\n".join("  " + line for line in text.splitlines()))
        else:
            print(f"{key}: {value}")


def _scenario_summary(scenario) -> dict[str, Any]:
    cases = scenario.golden_set
    summary = {
        "id": scenario.id,
        "packKind": scenario.pack_kind,
        "facts": len(scenario.facts),
        "tools": len(scenario.tools),
        "documents": len(scenario.data["knowledge"]["documents"]),
        "goldenCases": len(cases),
        "byCategory": dict(Counter(c["category"] for c in cases)),
        "bySet": dict(Counter(c["set"] for c in cases)),
        "waived": [v.describe() for v in scenario.waived_items],
    }
    if teaching.declared(scenario.data):
        summary["probes"] = len(teaching.probe_case_ids(scenario.data))
    return summary


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.scenario, enforce_gate=not args.allow_gate_findings)
    policy = validate_scenario_policy(scenario.data, root=scenario.root)
    payload = {
        "command": "validate",
        "status": "ok" if policy.ok and not [v for v in scenario.gate_violations if not v.waived] else "blocked",
        "scenario": _scenario_summary(scenario),
        "gateViolations": [v.describe() for v in scenario.gate_violations],
        "policyFindings": [f.describe() for f in policy.findings],
    }
    _emit(payload, args.json)
    return 0 if payload["status"] == "ok" else 1


def cmd_compile(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.scenario)
    lock = read_template_lock(args.template_lock)
    result = compiler.compile_pack(scenario, Path(args.out), template_commit=lock["template"]["commit"], strict_prose=args.strict_prose)
    payload = {
        "command": "compile",
        "status": "ok",
        "scenario": _scenario_summary(scenario),
        "outDir": str(result.out_dir),
        "files": len(result.files),
        "warnings": [f.describe() for f in result.report.warnings],
    }
    _emit(payload, args.json)
    return 0


def cmd_build_release(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.scenario)
    lock = read_template_lock(args.template_lock)
    commit = lock["template"]["commit"]
    upstream = Path(args.upstream) if args.upstream else repo_root() / lock["template"].get("localPath", "upstream/")
    out = Path(args.out)
    pack = compiler.compile_pack(scenario, out, template_commit=commit, strict_prose=args.strict_prose)
    release = render.render_release(pack, upstream, out / "release", template_commit=commit)
    payload = {
        "command": "build-release",
        "status": "ok",
        "scenario": _scenario_summary(scenario),
        "version": release.version,
        "templateCommit": commit,
        "releaseDir": str(release.release_dir),
        "manifest": str(release.manifest_path),
        "files": len(release.files),
        "patches": len(release.patches),
        "tokenReplacedFiles": len(release.token_counts),
        "warnings": [f.describe() for f in pack.report.warnings],
    }
    _emit(payload, args.json)
    return 0


def cmd_verify_release(args: argparse.Namespace) -> int:
    manifest = render.verify_release(Path(args.release_dir))
    payload = {
        "command": "verify-release",
        "status": "ok",
        "packId": manifest["packId"],
        "version": manifest["version"],
        "templateCommit": manifest["templateCommit"],
        "files": len(manifest["files"]),
    }
    _emit(payload, args.json)
    return 0


def _load_mapping(path: Path, what: str) -> dict[str, Any]:
    import yaml

    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ScenarioError(f"cannot read {what} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ScenarioError(f"{what} {path} is not a mapping")
    return data


def _rehearsal_inputs(args: argparse.Namespace) -> tuple[dict[str, Any], Path, Path | None, dict[str, str] | None, str]:
    """(scenario, run-state path, build dir, document texts, scenario source) for ``rehearsal <target>``.

    With a build (a project directory, or ``--build-dir``) the build snapshot
    (``build/instructor/scenario-snapshot.*``) and the built documents are judged (SPEC D8); a
    scenario file given with ``--build-dir`` must be byte-identical to that snapshot. A scenario file
    on its own (``--run-state`` required) is judged as ``scenario file``: never ready for class.
    """
    target = Path(args.target)
    build_dir = Path(args.build_dir) if args.build_dir else None
    if target.is_dir():
        build_dir = build_dir or target / "build"
        run_state = Path(args.run_state) if args.run_state else target / "run" / "state.json"
    elif not args.run_state:
        raise ScenarioError("--run-state is required when the target is a scenario file")
    else:
        run_state = Path(args.run_state)
    if build_dir is None:
        scenario = _load_mapping(target, "scenario")
        return scenario, run_state, None, teaching.document_texts(scenario, target.parent), "scenario file"
    snapshot = next((p for p in sorted((build_dir / "instructor").glob("scenario-snapshot.*")) if p.is_file()), None)
    if snapshot is None:
        raise ScenarioError(f"{build_dir} has no build snapshot ({build_dir}/instructor/scenario-snapshot.*); build the release first")
    if not target.is_dir():
        try:
            same = target.read_bytes() == snapshot.read_bytes()
        except OSError as exc:
            raise ScenarioError(f"cannot read scenario {target}: {exc}") from exc
        if not same:
            raise ScenarioError(f"{target} differs from the build snapshot {snapshot}: rehearsal judges the pack as the "
                                "release was built; rebuild, or rehearse the project directory")
    scenario = _load_mapping(snapshot, "scenario snapshot")
    return scenario, run_state, build_dir, rehearsal.build_documents(scenario, build_dir), "build snapshot"


def cmd_rehearsal(args: argparse.Namespace) -> int:
    from .guided_run_store import normalize_state, read_history

    scenario, run_state, build_dir, documents, source = _rehearsal_inputs(args)
    try:
        state = normalize_state(_load_mapping(run_state, "run state"))
    except ValueError as exc:
        raise ScenarioError(f"{run_state}: {exc}") from exc
    # The run must be bound to the release this build holds; without a manifest that is unverified
    # (the verdict is still computed, readyForClass stays false).
    release_verified = False
    manifest = build_dir / "release" / render.MANIFEST_NAME if build_dir else None
    if manifest is not None and manifest.is_file():
        built = _load_mapping(manifest, "release manifest").get("version")
        if built != state["releaseVersion"]:
            raise ScenarioError(f"the run state is bound to release {state['releaseVersion']}, but {build_dir} holds {built}; "
                                "rehearse the run against the build it executed")
        try:  # the release must also be intact (per-file sha256 + content hash), as the app requires
            render.verify_release(manifest.parent)
            release_verified = True
        except render.RenderError:
            release_verified = False
    history_dir = Path(args.history) if args.history else run_state.parent / "history"
    history = read_history(history_dir, release_version=state["releaseVersion"], template_commit=state["templateCommit"])
    result = rehearsal.build_rehearsal(state, scenario, history=history, documents=documents, guides=rehearsal.guide_status(build_dir),
                                       scenario_source=source, release_verified=release_verified)
    errors = rehearsal.validate_document(result)
    if errors:
        raise ScenarioError("rehearsal output does not match schemas/rehearsal.schema.json: " + "; ".join(errors[:5]))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        _emit({
            "command": "rehearsal",
            "verdict": result["verdict"],
            "reason": f"{result['reasonCode']}: {result['reason']}",
            "readyForClass": result["readyForClass"],
            "blockers": result["readiness"]["blockers"],
            "scenario": result["inputs"]["scenario"] + ("" if result["inputs"]["releaseVerified"] else " (release not verified)"),
            "decisiveRun": result["inputs"]["decisiveRun"],
            "phenomena": [f"{p['id']} ({p['kind']}): {p['verdict']} [{p['reasonCode']}]" for p in result["phenomena"]],
            "remediation": [f"{h['id']} {h['severity']} {h['code']}: {h['asset']['file'] or h['asset']['scenarioPath'] or h['asset']['kind']} — {h['action']}"
                            for h in result["remediation"]],
        }, False)
    return rehearsal.exit_code(result)


def cmd_guide(args: argparse.Namespace) -> int:
    """Render one Workshop Guide without building a release (SA / Kiro repair loops, golden regeneration).

    Without ``--draft`` the scenario must pass the provenance gate and the text equals the compiled
    guide; ``--draft`` renders an unreviewed scenario with the draft banner. The markdown goes to
    ``--out`` (``-`` = stdout); findings go to stderr, and any guide error exits 1.
    """
    lock = read_template_lock(args.template_lock)
    commit = lock["template"]["commit"]
    rehearsal = json.loads(Path(args.rehearsal).read_text(encoding="utf-8")) if args.rehearsal else None
    if args.draft:
        markdown, findings = guide.preview(args.scenario, args.audience, template_commit=commit, rehearsal=rehearsal)
    else:
        scenario = load_scenario(args.scenario)
        policy = validate_scenario_policy(scenario.data, root=scenario.root)
        guides = guide.build_guides(scenario, template_commit=commit, generator_version=__version__,
                                    warnings=policy.warnings, rehearsal=rehearsal)
        findings = [f for f in policy.findings if f.code.startswith("guide.")] + list(guides.findings)
        findings += guide.check_guides(scenario.data, guides, sources_text=guide.pack_sources_text(scenario),
                                       root=scenario.root).findings
        markdown = guides.student if args.audience == "student" else guides.instructor
    if args.out in (None, "-"):
        sys.stdout.write(markdown)
    else:
        Path(args.out).write_text(markdown, encoding="utf-8")
    for finding in findings:
        print(finding.describe(), file=sys.stderr)
    return 1 if any(f.severity == "error" for f in findings) else 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from . import calibration

    text = Path(args.log).read_text(encoding="utf-8") if args.log != "-" else sys.stdin.read()
    stability = calibration.calibrate(text, metric=args.metric)
    payload: dict[str, Any] = {
        "command": "calibrate",
        "status": "ok",
        "metric": stability.metric,
        "runs": stability.runs,
        "mean": stability.mean,
        "std": stability.std,
        "spread": stability.spread,
        "noiseBand": stability.band,
        "verdict": stability.verdict,
        "applied": False,
    }
    if not args.dry_run:
        calibration.apply_to_scenario(Path(args.scenario), stability, source=args.source)
        payload["applied"] = True
    _emit(payload, args.json)
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workshop_customizer", description="Eval-First workshop scenario pack engine")
    parser.add_argument("--version", action="version", version=f"workshop-customizer engine {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, *, out: bool) -> None:
        p.add_argument("--json", action="store_true", help="print a machine-readable summary")
        if out:
            p.add_argument("--out", required=True, help="output directory")
            p.add_argument("--template-lock", type=Path, default=None, help="path to template-lock.json")
            p.add_argument("--strict-prose", action="store_true", help="treat prose-residue warnings as errors")

    p = sub.add_parser("validate", help="schema, cross-reference, provenance gate and golden-set policy")
    p.add_argument("scenario")
    p.add_argument("--allow-gate-findings", action="store_true", help="report gate findings instead of failing on load")
    common(p, out=False)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("compile", help="compile the student pack and instructor bundle")
    p.add_argument("scenario")
    common(p, out=True)
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser("build-release", help="compile the pack and render the full runnable release")
    p.add_argument("scenario")
    p.add_argument("--upstream", default=None, help="pinned upstream checkout (default: <repo>/upstream)")
    common(p, out=True)
    p.set_defaults(func=cmd_build_release)

    p = sub.add_parser("verify-release", help="recompute hashes and compare with RELEASE.json")
    p.add_argument("release_dir")
    common(p, out=False)
    p.set_defaults(func=cmd_verify_release)

    p = sub.add_parser("guide", help="render the student or instructor Workshop Guide without building a release")
    p.add_argument("scenario")
    p.add_argument("--audience", choices=("student", "instructor"), default="student")
    p.add_argument("--out", default="-", help="output file, or '-' for stdout (default)")
    p.add_argument("--draft", action="store_true", help="render an unreviewed scenario (gate not enforced; draft banner)")
    p.add_argument("--rehearsal", default=None, help="rehearsal record JSON to show in the instructor guide")
    p.add_argument("--template-lock", type=Path, default=None, help="path to template-lock.json")
    p.set_defaults(func=cmd_guide)

    p = sub.add_parser("calibrate", help="turn a 13-judge-stability.sh log into evaluation.noiseBand")
    p.add_argument("scenario")
    p.add_argument("log", help="stability script output file, a JSON array, one score per line, or '-' for stdin")
    p.add_argument("--metric", default="thelma_rag_quality", help="metric key under evaluation.noiseBand")
    p.add_argument("--source", default="13-judge-stability.sh", help="evidence label written to labs.observations")
    p.add_argument("--dry-run", action="store_true", help="compute the band without editing the scenario")
    common(p, out=False)
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("rehearsal", help="class verdict and readyForClass over a Guided Run (exit 0 ready for class, "
                                         "2 insufficient evidence, 3 not ready, 4 verdict ready but not ready for class)")
    p.add_argument("target", help="a project directory (build snapshot + run/state.json) or a scenario file")
    p.add_argument("--run-state", help="Guided Run state.json (default: <project>/run/state.json)")
    p.add_argument("--history", help="archived runs directory (default: <run-state dir>/history)")
    p.add_argument("--build-dir", help="build directory: its scenario snapshot (a scenario file target must match it), release "
                                       "manifest, guides and built documents (default: <project>/build)")
    p.add_argument("--out", help="also write the rehearsal JSON to this file")
    common(p, out=False)
    p.set_defaults(func=cmd_rehearsal)
    return parser


def main(argv: list[str] | None = None) -> int:
    from .calibration import CalibrationError

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except PackValidationError as exc:
        payload = {"command": args.command, "status": "blocked", "findings": [f.describe() for f in exc.report.findings]}
        _emit(payload, getattr(args, "json", False))
        return 1
    except (ScenarioError, render.RenderError, CalibrationError, guide.GuideError) as exc:
        payload = {"command": args.command, "status": "error", "error": str(exc)}
        _emit(payload, getattr(args, "json", False))
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
