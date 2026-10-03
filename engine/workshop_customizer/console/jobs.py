"""Background jobs of the console: long operations (a verification, an ingestion, a build) run on a thread and the
page polls ``GET /api/console/jobs/{id}``. Each job is a JSON file (``<console>/jobs/<id>.json``) rewritten on every
log line and at the end; a job still marked running when the console starts was cut off and is marked so
(``interrupted``). A record such a job held (an experiment or canary ``creating``, ``promoting``, ``rolling_back``,
``cleaning``) is released by its own module when next read: the module checks whether the job it names still runs.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from .common import now as _now

JOB_ID = re.compile(r"^[a-z][a-z0-9-]{1,40}-[0-9a-f]{10}$")
MAX_LOG = 400


class JobLog:
    """What a job's work function gets: ``log(line)`` and ``progress(**fields)`` (both persisted at once)."""

    def __init__(self, jobs: "Jobs", job: dict[str, Any]):
        self.jobs, self.job = jobs, job

    def log(self, line: str) -> None:
        self.job["log"] = (self.job.get("log") or [])[-MAX_LOG + 1:] + [f"{_now()[11:19]} {line}"]
        self.jobs._save(self.job)

    def progress(self, **fields: Any) -> None:
        self.job.setdefault("progress", {}).update(fields)
        self.jobs._save(self.job)


class Jobs:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        for path in self.root.glob("*.json"):  # cut off by a restart
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if job.get("status") == "running":
                job.update(status="interrupted", finishedAt=_now(), error="the console restarted while this job ran")
                self._save(job)

    def _save(self, job: dict[str, Any]) -> None:
        with self._lock:
            path = self.root / f"{job['id']}.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
            os.replace(tmp, path)

    def start(self, kind: str, workspace: str, params: dict[str, Any], work: Callable[[JobLog], Any], *, label: str = "") -> dict[str, Any]:
        job = {"id": f"{kind}-{secrets.token_hex(5)}", "kind": kind, "workspace": workspace, "label": label or kind, "params": params,
               "status": "running", "createdAt": _now(), "finishedAt": None, "log": [], "progress": {}, "result": None, "error": None}
        self._save(job)
        snapshot = json.loads(json.dumps(job, default=str))

        def run() -> None:
            ctx = JobLog(self, job)
            try:
                job["result"] = work(ctx)
                job["status"] = "succeeded"
            except Exception as exc:  # noqa: BLE001 - recorded on the job
                job.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:600]}")
                ctx.log(traceback.format_exc().strip().splitlines()[-1])
            job["finishedAt"] = _now()
            self._save(job)

        threading.Thread(target=run, name=f"job-{job['id']}", daemon=True).start()
        return snapshot

    def get(self, job_id: str) -> dict[str, Any]:
        if not JOB_ID.match(job_id):
            raise KeyError(job_id)
        return json.loads((self.root / f"{job_id}.json").read_text(encoding="utf-8"))

    def list(self, *, workspace: str | None = None, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (workspace and job.get("workspace") != workspace) or (kind and job.get("kind") != kind):
                continue
            out.append({k: v for k, v in job.items() if k not in ("log", "result")})
            if len(out) >= limit:
                break
        return out
