"""Harness-only: seed a built project's Guided Run with a committed fixture run, for UI evidence.

    .venv/bin/python3 tools/ui_harness/seed_run.py DATA_DIR PROJECT_ID [RUN] [--upto STEP] [--fail STEP]

RUN is a run of tests/fixtures/teaching_run (default ``it-2026-09-13``, an it-helpdesk run; use it on
a project created from the it-helpdesk template).  The step records are the fixture's parsed outputs,
bound to the project's current build (release version and template commit), so the real backend
computes the report and the rehearsal from them exactly as after a live run.  ``--upto`` keeps only
the steps up to STEP; ``--fail`` marks STEP failed.  Never used by tests or the app; the UI has no
demo data of its own.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "engine"))
from workshop_customizer.guided_report import build_report  # noqa: E402
from workshop_customizer.guided_run import GUIDE_STEPS, initial_state  # noqa: E402
from workshop_customizer.guided_run_store import GuidedRunStore  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures" / "teaching_run"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("project_id")
    parser.add_argument("run", nargs="?", default="it-2026-09-13")
    parser.add_argument("--upto", help="keep the fixture steps up to this step id; later steps are not started")
    parser.add_argument("--fail", help="mark this step failed")
    args = parser.parse_args()

    pdir = args.data_dir / "projects" / args.project_id
    build = json.loads((pdir / "build" / "build.json").read_text(encoding="utf-8"))
    snapshot = next(p for p in sorted((pdir / "build" / "instructor").glob("scenario-snapshot.*")) if p.is_file())
    scenario = yaml.safe_load(snapshot.read_text(encoding="utf-8"))
    fixture = json.loads((FIXTURES / args.run / "steps.json").read_text(encoding="utf-8"))["steps"]
    order = [step.id for step in GUIDE_STEPS]
    keep = order[: order.index(args.upto) + 1] if args.upto else order
    defaults = initial_state()
    steps = {sid: (fixture[sid] if sid in keep else defaults[sid]) for sid in order}
    if args.fail:
        steps[args.fail].update(status="failed", error="(fixture) the step exited non-zero; see the output summary", outputs={})
        for sid in order[order.index(args.fail) + 1:]:
            steps[sid] = {**defaults[sid], "status": "blocked"}
    passed = all(steps[sid]["status"] == "passed" for sid in order)
    state = {"schemaVersion": 1, "projectId": args.project_id, "releaseVersion": build["version"],
             "templateCommit": build["templateCommit"], "status": "passed" if passed else ("failed" if args.fail else "in_progress"),
             "createdAt": build["at"], "updatedAt": build["at"], "stepOrder": order, "steps": steps, "report": None,
             "currentStepId": None}
    if passed:
        state["report"] = build_report(state, scenario, generated_at=build["at"])
    store = GuidedRunStore(pdir)
    store.save(state)
    if passed:
        (pdir / "run" / "report.json").write_text(json.dumps(state["report"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"seeded {store.path} from fixture {args.run}: {sum(s['status'] == 'passed' for s in steps.values())}/15 passed")


if __name__ == "__main__":
    main()
