"""Durable per-project Guided Run state."""
from __future__ import annotations
import json, os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .guided_run import GUIDE_STEPS, initial_state


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_state(data: Any) -> dict[str, Any]:
    """A loaded state.json with the current step defaults; ``ValueError`` when it does not fit the guide."""
    expected = [s.id for s in GUIDE_STEPS]
    if not isinstance(data, dict) or data.get("schemaVersion") != 1 or list((data.get("steps") or {}).keys()) != expected:
        raise ValueError("guided run state is incompatible with the current guide")
    defaults = initial_state()
    for step_id in expected:
        for key, value in defaults[step_id].items():
            data["steps"][step_id].setdefault(key, value)
    data.setdefault("currentStepId", None)
    return data


class GuidedRunStore:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir.resolve()
        self.path = self.project_dir / "run" / "state.json"

    def create(self, *, project_id: str, release_version: str, template_commit: str) -> dict[str, Any]:
        if self.path.exists():
            return self.load()
        state = {"schemaVersion": 1, "projectId": project_id, "releaseVersion": release_version,
                 "templateCommit": template_commit, "status": "not_started", "createdAt": utc_now(),
                 "updatedAt": utc_now(), "stepOrder": [step.id for step in GUIDE_STEPS],
                 "steps": initial_state(), "report": None}
        self.save(state)
        return state

    def load(self) -> dict[str, Any]:
        return normalize_state(json.loads(self.path.read_text(encoding="utf-8")))

    def history_states(self, *, release_version: str, template_commit: str) -> list[tuple[str, dict[str, Any]]]:
        """Archived runs (``run/history/*/state.json``) of the same release and template; see :func:`read_history`."""
        return read_history(self.path.parent / "history", release_version=release_version, template_commit=template_commit)

    def reset(self, *, project_id: str, release_version: str, template_commit: str, from_step: str | None = None) -> dict[str, Any]:
        previous = None
        if from_step is not None and not self.path.exists():
            raise ValueError("no run exists to partially reset")
        if self.path.exists():
            state = self.load()
            if any(step["status"] == "running" for step in state["steps"].values()):
                raise ValueError("cannot reset while a Workshop step is running; poll it to completion first")
            if from_step is not None:
                if from_step not in state["stepOrder"]:
                    raise ValueError("reset step is not in the Workshop Guide")
                if state["releaseVersion"] != release_version or state["templateCommit"] != template_commit:
                    raise ValueError("partial reset cannot carry evidence across different releases")
                boundary = state["stepOrder"].index(from_step)
                if any(state["steps"][sid]["status"] != "passed" for sid in state["stepOrder"][:boundary]):
                    raise ValueError("all earlier steps must have passed before a partial reset")
                previous = state
            archive = self.path.parent / "history" / uuid.uuid4().hex
            archive.mkdir(parents=True)
            shutil.copy2(self.path, archive / "state.json")
            for name in ("report.json", "rehearsal.json"):
                derived = self.path.parent / name
                if derived.exists():
                    shutil.copy2(derived, archive / name)
                    derived.unlink()
            self.path.unlink()
        if previous is not None:
            defaults = initial_state()
            boundary = previous["stepOrder"].index(from_step)
            for sid in previous["stepOrder"][boundary:]:
                previous["steps"][sid] = defaults[sid]
            previous.update(status="in_progress" if boundary else "not_started", currentStepId=None, report=None)
            self.save(previous)
            return previous
        return self.create(project_id=project_id, release_version=release_version, template_commit=template_commit)

    def save(self, state: dict[str, Any]) -> None:
        state["updatedAt"] = utc_now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


def read_history(history_dir: Path, *, release_version: str, template_commit: str) -> list[tuple[str, dict[str, Any]]]:
    """``[("history/<id>", state)]`` for the archived runs of one release and template, in archive-name order.

    Read-only. Unreadable or incompatible archives are skipped; rehearsal orders and de-duplicates the
    runs by their evaluation commands.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(Path(history_dir).glob("*/state.json")):
        try:
            state = normalize_state(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):  # json.JSONDecodeError is a ValueError
            continue
        if state.get("releaseVersion") == release_version and state.get("templateCommit") == template_commit:
            out.append((f"history/{path.parent.name}", state))
    return out
