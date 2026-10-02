"""Direct mode across packs: rehearse every pack's current release at once and tabulate what still reproduces.

    .venv/bin/python3 tools/direct_fleet.py --data build/aws-e2e/appdata --profile default --account 123456789012

Model drift breaks teaching contrasts without a sound: on 2026-09-27 the generic HR lab's own contrast no longer
reproduced with that day's models. This rehearses each pack's current build (``build/release``) in direct mode,
at most ``--parallel`` packs at a time, each on its own direct resources (``direct.names``, reused or updated
in place), and writes ``fleet.md`` / ``fleet.json`` under ``--out`` (default ``build/fleet/<UTC stamp>``): one
row per pack with its verdict, its phenomena, the time it took and how it compares with the pack's last
recorded class verdict. Each pack's run records go to ``<out>/<project>/``. A pack whose run fails is reported
and the others go on. A direct verdict is not a class verdict. Exit 0 when every pack is ready, 3 when any is
not, 2 on an environment failure.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer.direct.panel import DEFAULT_PANEL  # noqa: E402
from workshop_customizer.direct.run import DirectRun  # noqa: E402

MARK = {"reproduced": "✓", "not_reproduced": "✗", "insufficient_evidence": "?"}
KIND = {"prompt_fixable": "PF", "retrieval_gap": "gap", "tool_use": "tool", "refusal": "refusal", "escalation": "escalation",
        "memory": "memory", "honesty": "honesty"}


def fleet_packs(data: Path, only: list[str]) -> list[Path]:
    """Projects with a built release whose pack declares teaching phenomena (``--project`` narrows them)."""
    found = []
    for pdir in sorted((data / "projects").iterdir()):
        release = pdir / "build" / "release"
        if (only and pdir.name not in only) or not (release / "RELEASE.json").is_file():
            continue
        pack = json.loads((release / "pack" / "pack.json").read_text(encoding="utf-8"))
        if (pack.get("teaching") or {}).get("phenomena"):
            found.append(pdir)
    missing = sorted(set(only) - {p.name for p in found})
    if missing:
        raise SystemExit(f"no built pack with a teaching design: {', '.join(missing)}")
    return found


def last_class_verdict(pdir: Path, version: str) -> dict[str, Any] | None:
    try:
        meta = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    last = meta.get("lastRehearsal") or None
    if not last:
        return None
    return {"verdict": last.get("verdict"), "readyForClass": bool(last.get("readyForClass")), "sameRelease": last.get("releaseVersion") == version,
            "releaseVersion": last.get("releaseVersion")}


def row(project: str, version: str, doc: dict[str, Any] | None, error: str | None, seconds: float, last: dict[str, Any] | None) -> dict[str, Any]:
    phenomena = [{"id": p.get("id"), "kind": p.get("kind"), "verdict": p.get("verdict"), "reasonCode": p.get("reasonCode"),
                  "reproducedIn": (p.get("replication") or {}).get("reproducedIn"), "runs": (p.get("replication") or {}).get("completeRuns")}
                 for p in (doc or {}).get("phenomena") or []]
    verdict = (doc or {}).get("verdict") if doc else "error"
    robust = ((doc or {}).get("direct") or {}).get("robust")
    if verdict == "ready" and robust is False:  # --repeat: ready in the last round, not in every one
        verdict = "not_robust"
    drift = None
    if last and last["readyForClass"] and verdict != "ready":
        drift = "was ready for class on this release" if last["sameRelease"] else "was ready for class on an earlier release"
    return {"project": project, "releaseVersion": version, "verdict": verdict, "reasonCode": (doc or {}).get("reasonCode"), "error": error,
            "seconds": round(seconds, 1), "phenomena": phenomena, "lastClassVerdict": last, "drift": drift,
            "invokeErrors": len(((doc or {}).get("direct") or {}).get("invokeErrors") or [])}


def kinds_summary(phenomena: list[dict[str, Any]]) -> str:
    by_kind: dict[str, list[str]] = {}
    for p in phenomena:
        by_kind.setdefault(KIND.get(str(p["kind"]), str(p["kind"])), []).append(str(p["verdict"]))
    return " · ".join(f"{k} {sum(v == 'reproduced' for v in vs)}/{len(vs)}" for k, vs in by_kind.items())


def render_markdown(doc: dict[str, Any]) -> str:
    lines = [f"# Direct fleet rehearsal, {doc['generatedAt']}", "",
             f"{len(doc['packs'])} packs, {doc['ready']} ready, {doc['parallel']} at a time, {doc['seconds'] / 60:.1f} min in all. "
             "A direct verdict is not a class verdict.", "",
             "| Pack | Verdict | Reproduced by kind | Phenomena | Minutes | Last class verdict |", "|---|---|---|---|---|---|"]
    for r in doc["packs"]:
        marks = " ".join(f"{p['id']} {MARK.get(str(p['verdict']), '·')}" + (f" {p['reproducedIn']}/{p['runs']}" if (p.get("runs") or 0) > 1 else "")
                         for p in r["phenomena"]) or "—"
        last = r["lastClassVerdict"]
        said = "—" if not last else (("ready for class" if last["readyForClass"] else str(last["verdict"]))
                                     + ("" if last["sameRelease"] else f" ({last['releaseVersion']})"))
        verdict = f"**{r['verdict']}**" + (f" `{r['reasonCode']}`" if r.get("reasonCode") else "") + (f" — {r['error']}" if r.get("error") else "")
        lines.append(f"| {r['project']} | {verdict} | {kinds_summary(r['phenomena']) or '—'} | {marks} | {r['seconds'] / 60:.1f} | {said} |")
    drifted = [r for r in doc["packs"] if r["drift"]]
    if drifted:
        lines += ["", "## Check these", ""]
        lines += [f"- **{r['project']}**: {r['drift']}, direct now {r['verdict']}; "
                  "rehearse it once more (judge noise) and, if it holds, repair from its remediation." for r in drifted]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", type=Path, required=True, help="the App data dir (its projects/)")
    parser.add_argument("--project", action="append", default=[], help="only these projects (repeatable; default: every built pack with a teaching design)")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--parallel", type=int, default=3, help="packs rehearsed at once (default 3)")
    parser.add_argument("--workers", type=int, default=4, help="questions asked at once per pack (default 4)")
    parser.add_argument("--compare-model", action="append", default=[], dest="compare_models", help="also ask with the candidate on this model")
    parser.add_argument("--repeat", type=int, default=1, help="rounds per pack on the same release (default 1): reproduction counts")
    parser.add_argument("--panel", action="store_true", help="also score with AgentCore's own evaluators (direct.panel)")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    import boto3

    if boto3.Session(profile_name=args.profile, region_name=args.region).client("sts").get_caller_identity()["Account"] != args.account:
        print("the AWS caller account differs from --account", file=sys.stderr)
        return 2
    packs = fleet_packs(args.data, args.project)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or REPO / "build" / "fleet" / stamp
    out.mkdir(parents=True, exist_ok=True)
    printing = threading.Lock()

    def say(text: str) -> None:
        with printing:
            print(text, flush=True)

    def one(pdir: Path) -> dict[str, Any]:
        version = json.loads((pdir / "build" / "release" / "RELEASE.json").read_text(encoding="utf-8"))["version"]
        log_path = out / f"{pdir.name}.log"
        started = time.monotonic()
        with log_path.open("w", encoding="utf-8") as log:
            def write(line: str) -> None:
                log.write(line + "\n")
                log.flush()

            try:
                session = boto3.Session(profile_name=args.profile, region_name=args.region)  # one per pack: clients are made in its thread
                doc = DirectRun(pdir, session=session, profile=args.profile, account=args.account, region=args.region, out=out / pdir.name,
                                log=write, workers=args.workers, compare_models=args.compare_models, repeat=args.repeat,
                                panel=DEFAULT_PANEL if args.panel else (),
                                on_stage=lambda stage: say(f"  {pdir.name}: {stage}")).run()
                result = row(pdir.name, version, doc, None, time.monotonic() - started, last_class_verdict(pdir, version))
            except Exception as exc:  # noqa: BLE001 - one pack's failure is its row, the others go on
                write(traceback.format_exc())
                result = row(pdir.name, version, None, f"{type(exc).__name__}: {str(exc)[:300]}", time.monotonic() - started,
                             last_class_verdict(pdir, version))
        say(f"{pdir.name}: {result['verdict']} in {result['seconds'] / 60:.1f} min")
        return result

    say(f"{len(packs)} packs, {args.parallel} at a time → {out}")
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        rows = list(pool.map(one, packs))
    doc = {"schema": "workshop-customizer/direct-fleet/1", "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "region": args.region, "parallel": args.parallel, "compareModels": args.compare_models, "seconds": round(time.monotonic() - started, 1),
           "ready": sum(r["verdict"] == "ready" for r in rows), "packs": rows}
    (out / "fleet.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "fleet.md").write_text(render_markdown(doc), encoding="utf-8")
    print((out / "fleet.md").read_text(encoding="utf-8"))
    if any(r["verdict"] == "error" for r in rows):
        return 2
    return 0 if doc["ready"] == len(rows) else 3


if __name__ == "__main__":
    raise SystemExit(main())
