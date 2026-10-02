"""Skill Lab: what a skill really adds to an agent, and a loop that improves it from its failures.

A skill is an Agent Skills folder: ``SKILL.md`` with ``name`` and ``description`` frontmatter. The console keeps a
library of them in its bucket, every saved version under its own prefix ``skills/<name>/v<NNNN>/`` (a Harness fetches
a skill by prefix, into ``.agents/skills/s3/<name>``), with the version that is current and the agents using it.

Evaluation runs on the AgentCore Harness the skill is for, through InvokeHarness's per-call ``skills`` override: each
task is answered by the same agent with the skill and without it (each answer in a fresh session under its own actor,
so memory cannot carry one arm into the other), and an LLM judge scores both answers against the task's rubric. The
report gives both arms' pass rate and mean score, the lift, the tasks the skill wins and loses with a sign test on
them, and the trigger rate: how often the model loaded the skill at all (it calls the ``skills`` tool with
``{"skill_name": …}``). The model and the skill's description decide that, not ``allowedTools``: live on 2026-10-01
Nova 2 Lite skipped a skill described as "How every reply to a customer must end" on every call and loaded one
described as "回答会员关于积分…的任何问题时使用" on every call; Claude Sonnet 5.5 and Haiku 4.5 loaded both. (Launchpad's
Skill Lab runs a skill in a coding-agent worker, with no without-skill baseline and no trigger rate.)

Training is a reflection loop: answer a train batch with the current skill, have the optimizer model rewrite SKILL.md
from the failures within an edit budget that decays over the steps, and keep a candidate only when it beats the current
skill on val, both answering the same val tasks at the same time, by a margin (one-shot val scores are noisy: live on
2026-10-01 a candidate 0.21 ahead on 4 val tasks was 0.14 behind on 6 test tasks). ``repeats`` answers every val and test
task up to three times. Candidates live under ``skills/<name>/candidates/<job>/``. At the end the seed, the best version
and no skill are all measured on test. Publishing saves the best as the skill's new current version and can switch an agent
to it. Task sets are ``{id, question, rubric}`` items (and ``files``, below), in one list or split into train / val / test.

**A skill whose output is a file** is evaluated on a Claude Agent SDK runtime (``runtimeId``: one deployed from the
console's template, found in claude_sdk's records — its newest successful deployment says what the image holds: its
skills, its model, template 2 or later). Each answer is one InvokeAgentRuntime ``{"prompt", "format": "json", "skills",
"skillOverrides"}`` in a session of its own (its own microVM, stopped in the background afterwards): the arm with the
skill names it and brings the version under test (or a training's candidate) as an override read from S3 for that call,
the arm without it does not — the image's other skills are in both, as a Harness's are. The trigger is the turn's
``Skill`` call naming the skill. The answer brings the files the turn created or changed in its own directory, each with
a view extracted from the file (a spreadsheet's cells and formulas, a document's text); the judge reads them after the
answer and is told to judge a file from them, never from what the answer says (the Harness's judge prompt is unchanged),
the optimizer sees a failed answer's files, the results keep them (a small file's bytes too, for the page's download).
Publishing to such a runtime is a new version of it: its image holds its skills (the image the scan gate passed;
agent_spec.json records the versions), so claude_sdk's redeploy (``runtimeId``) builds it again with the published
version — a deployment, an admin's. Launchpad's Skill Lab rolls a skill out in a coding-agent worker with no
without-skill arm and judges binary artifacts with an agentic judge (artifact_inventory / inspect / render / extract
over an MCP server, LibreOffice rendering documents); here the agent's own runtime turns its files into text and
pictures, and one chat judge reads the answer and the files and looks at the pictures (a document is not rendered:
its text and its pictures are what the judge gets).

**A task's input files** (Launchpad's task assets): a task item may carry ``files`` — ``[{"path", "asset":
"sha256:<hex>"}]`` (or Launchpad's ``{path: {"asset": …}}``), ``path`` where the file lands in the agent's directory —
naming files uploaded first (``POST …/skill-lab/assets``, base64 in a JSON body as the console's other uploads; that
route alone takes a body of 134 MiB so a 25 MiB file fits). Each content is one object ``skill-lab/assets/<sha256>`` of
the console bucket, written only when it is not there yet, and an entry of the workspace's catalog (the names it was
uploaded as, which sets use it); Launchpad's types (xlsx, pdf, png, jpg, webp, md, txt, csv, the content checked
against the extension: a text file UTF-8 without NUL, a binary its signature, an xlsx a workbook without macros or a
zip bomb) and limits (25 MiB a file; 32 files and 100 MiB an upload or a task; 256 references and 200 MiB of distinct
files a set). A file a set names is not deleted; deleting one takes every version of its object. On a Claude Agent SDK
runtime (template 3: an older one is refused tasks with files, and a call it answers without ``call.inputs`` stops the
job) both arms' calls carry the task's ``files`` as S3 URIs with their SHA-256 and size; the runtime puts them in the
call's directory before the prompt, tells the model they are there, and lists only the files the turn created or
changed — an input it rewrites is marked an input. Its role's grant covers ``skill-lab/assets/*`` too. **A Harness is
refused** a job whose tasks bring files, before anything of it is read or changed: InvokeHarness has nowhere to put a
file, and inlining small text files into the question would evaluate a different task than the one the skill is for
(a skill that says how to read a file would be tested on text it never opens) — the same set's file-less splits still
run on a Harness. The judge is told which files were the task's inputs, and **sees the pictures**: an image the turn
made, and each picture inside a document it made (a Word document's or a workbook's media, a slide's pictures), comes
after the text as a Converse image block — the runtime's preview of it (at most 1568 px, 1 MB) or, from a template-2
runtime, a small image's own bytes — at most 6 a judgment, 3.75 MB each, 10 MB in all, each left out named with why;
a presentation's slides come as text (titles, text, tables, a chart's data, notes). Everything else stays text, as
before. A result keeps each picture's thumbnail (320 px) for the page, not the judge's preview.

Live 2026-10-02 on ``adlc_probe_inputs_bf820b`` (template 3, Claude Sonnet 5.5 with Read / Write / Edit / Bash / Glob,
the sample ``points-chart-png``: each task gives the agent a member file — a CSV, two CSVs or an xlsx with text IDs —
and its rubric checks the chart picture itself; the judge Claude Sonnet 5.5):

* **Importing the sample** uploaded its 12 files (13 references on 11 tasks) as 12 objects; attaching one of them again
  from the page answered "already there" and wrote nothing.
* **Evaluation** on the 4 test tasks, 8 calls in 50 s (the grant's 10 s first, no 503): with the skill 4 of 4 passed
  (mean 1.00, loaded every time, ``points-chart.png`` each time, 7.6–9.3 s, $0.015–0.017); without it 0 of 4 (mean 0.28:
  ``members.png``, ``points_bar.png``, ``points_rank.png``, ``points.png``, 15.8–21.4 s, $0.024–0.037) — wins 4, losses
  0, p = 0.125: ``maybe`` with four tasks. Every judgment was shown the chart (``judgedImages``) and its reasons are
  about the picture: with the skill "3根横向柱、按2450/1200/800自上而下排列，M1002为橙色其余为蓝色，右端有积分标注，标题和横轴标签正确，
  members.csv未改动"; without it "图是纵向柱状图，并非横向；柱子按会员号排序；M1002 不是橙色；标题是「本期会员积分」". The inputs the
  agent left alone were in no answer's files, and the judge said so from the list of inputs.
* **A presentation** (one more call, no skill): ``chart.png`` and a two-slide ``report.pptx`` with a table and the chart
  as a picture, 17 s; the slides came as text, the slide's picture as a preview, and the judge was shown both pictures
  ("图为横向柱状图，M1002 为橙色"), pass 1.0. A Harness asked to evaluate the set was refused with a 400 before any call.
  A job's record with 8 answers and their thumbnails: 354 KB.

Live 2026-10-02 on ``adlc_probe_artifacts_dc2877`` (Claude Sonnet 5.5 with Bash, the sample ``points-report-xlsx`` and
its task set, the judge Claude Sonnet 5.5):

* **Evaluation** of v0001 on the 6 test tasks: with the skill 6 of 6 passed (mean 1.00), the skill loaded every time;
  without it 0 of 6 (mean 0.14) — ``helps``: 6 wins, 0 losses, sign test p = 0.031; 60 s for 12 answers, 4 at a time
  (the grant's 10 s wait first). The judge read the files: for ``test-zero-ids`` the arm without the skill wrote
  ``会员积分报表.xlsx`` (sheet 积分报表, a fourth column ``扣除到期后积分``, rows 00127, 00031, 00450) and the judge's reason
  named exactly that, crediting the IDs kept as text ("00127" in the view) and the SUM formulas it saw.
* **One training step** from a deliberately weak seed v0002 (three rules: the file name, a row each, say the file):
  seed val 0.23; every train answer failed, Claude Opus 5.5 rewrote the skill with 4 edits (the sheet 积分明细, the
  header, sorted rows with merged duplicates and IDs as text, the SUM total, the reply's count and total), the
  candidate — read by the runtime from ``candidates/<job>/s001/`` — scored 1.00 against the seed's 0.23 re-measured
  alongside it (3 better, 0 worse) and was accepted; on test best 6/6 (1.00), seed 0/6 (0.37), no skill 0/6 (0.13):
  ``improved``, 2 min 8 s in all. Publishing it to the runtime saved v0003 and built version 2 of the runtime with it
  (2 min 15 s, the scan gate and the smoke call included).
"""
from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
import io
import json
import math
import re
import secrets
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..direct.aws import client
from . import agents
from .auth import Auth
from .kb import ensure_bucket
from .workspaces import boundary_of

JUDGE_MODEL = "us.anthropic.claude-sonnet-5-5"
OPTIMIZER_MODEL = "us.anthropic.claude-opus-5-5"
SKILL_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9.:_\-/]{2,199}$")
SPLITS = ("train", "val", "test")
MAX_TASKS = 500
SKILLS_POLICY = "adlc-console-skills"
GATES = ("soft", "hard", "mixed")
SAMPLES = Path(__file__).resolve().parents[3] / "app" / "console" / "samples" / "skill-lab"
DEFAULT_SAMPLE = "loyalty-reply-style"
#: The Claude Agent SDK template that lets a call bring its own skills and lists the files a turn made (claude_sdk.TEMPLATE).
RUNTIME_TEMPLATE = 2
#: The template whose calls also bring their input files (``files``) and whose answers carry pictures for the judge.
FILES_TEMPLATE = 3
#: A runtime role just granted the library is given this long before the first call reads a skill with it (IAM).
GRANT_WAIT = 10.0

#: Task input files (Launchpad's task assets): one object per content in the console bucket, under this prefix.
ASSET_PREFIX = "skill-lab/assets/"
#: Launchpad's task-asset types (``task_assets.py``) and the media type each is stored with.
ASSET_TYPES = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "pdf": "application/pdf", "png": "image/png",
               "jpg": "image/jpeg", "webp": "image/webp", "md": "text/markdown", "txt": "text/plain", "csv": "text/csv"}
ASSET_EXTENSIONS = {".xlsx": "xlsx", ".pdf": "pdf", ".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".webp": "webp", ".md": "md", ".txt": "txt",
                    ".csv": "csv"}
#: Launchpad's limits: 25 MiB a file; 32 files and 100 MiB an upload; 32 files and 100 MiB a task; 256 file references
#: and 200 MiB of distinct files a task set.
MAX_ASSET_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_FILES, MAX_UPLOAD_BYTES = 32, 100 * 1024 * 1024
MAX_TASK_FILES, MAX_TASK_BYTES = 32, 100 * 1024 * 1024
MAX_SET_FILES, MAX_SET_BYTES = 256, 200 * 1024 * 1024
#: An upload is base64 in a JSON body, as the console's other uploads are: its route takes a body this large (the
#: console's other routes 25 MB), so a 25 MiB file fits.
MAX_UPLOAD_BODY = (MAX_UPLOAD_BYTES + 2) // 3 * 4 + 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
_XLSX_MAX_MEMBERS, _XLSX_MAX_UNPACKED, _XLSX_MAX_RATIO = 10_000, 200 * 1024 * 1024, 100
_BINARY_SIGNATURES = (b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"PK\x03\x04")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SkillLabError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- SKILL.md -----------------------------------------------------------------------------------------------------------

def frontmatter(text: str) -> tuple[dict[str, str], str]:
    """SKILL.md's frontmatter (top-level ``key: value`` lines, folded or literal blocks joined) and its body."""
    t = text.lstrip("﻿")
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", t, re.S)
    if not m:
        raise SkillLabError("SKILL.md must start with YAML frontmatter (--- name / description ---): a Harness refuses a skill without it")
    meta: dict[str, str] = {}
    key = None
    for line in m.group(1).splitlines():
        if line[:1] in (" ", "\t"):
            if key:
                meta[key] = (meta[key] + " " + line.strip()).strip()
            continue
        k, sep, v = line.partition(":")
        if not sep or not k.strip():
            key = None
            continue
        key, value = k.strip(), v.strip()
        meta[key] = "" if value in (">", "|", ">-", "|-") else value.strip('"').strip("'")
    return meta, t[m.end():]


def check_skill(name: str, text: str) -> dict[str, str]:
    if not SKILL_NAME.match(name or ""):
        raise SkillLabError("skill name: lowercase letters, digits and hyphens (at most 64)")
    if len(text.encode("utf-8")) > 200_000:
        raise SkillLabError("SKILL.md: at most 200 KB")
    meta, body = frontmatter(text)
    if meta.get("name") != name:
        raise SkillLabError(f"the frontmatter name must be {name}")
    if not meta.get("description"):
        raise SkillLabError("the frontmatter needs a description: it is what the model reads to decide whether to load the skill")
    if len(meta["description"]) > 1024:
        raise SkillLabError("description: at most 1024 characters")
    if not body.strip():
        raise SkillLabError("SKILL.md has no instructions after the frontmatter")
    return meta


# -- the library --------------------------------------------------------------------------------------------------------

def _key(workspace: str) -> str:
    return f"skills_{workspace}"


def library(console: Any, workspace: str) -> dict[str, Any]:
    return dict(console.store.read(_key(workspace), {}))


def _where(console: Any, workspace: str) -> tuple[Any, dict[str, Any], str]:
    ws = console.workspaces.get(workspace)
    console.workspaces.verify(workspace)
    session = console.workspaces.session(workspace)
    return session, ws, ensure_bucket(session, ws["accountId"], ws["region"])


def skill_uri(bucket: str, name: str, version: str) -> str:
    return f"s3://{bucket}/skills/{name}/{version}/"


def list_skills(console: Any, workspace: str) -> list[dict[str, Any]]:
    return [{"name": n, "description": e.get("description"), "current": e.get("current"), "versions": len(e.get("versions") or []),
             "agents": sorted((e.get("agents") or {}).values(), key=lambda a: a["name"]), "updatedAt": e.get("updatedAt")}
            for n, e in sorted(library(console, workspace).items())]


def save_version(console: Any, workspace: str, name: str, text: str, *, source: str, note: str = "", make_current: bool = True) -> dict[str, Any]:
    """A new version of ``name`` (the skill is created by its first); the same text again returns its version."""
    meta = check_skill(name, text)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    session, ws, bucket = _where(console, workspace)
    made: dict[str, Any] = {}

    def reserve(all_: dict[str, Any]) -> dict[str, Any]:
        entry = all_.get(name) or {"name": name, "versions": [], "agents": {}, "createdAt": _now()}
        same = next((v for v in entry["versions"] if v["sha"] == sha), None)
        if same:
            made.update(version=same["version"], created=False)
        else:  # after the highest number held: a clash with another library leaves gaps, never a reuse
            version = f"v{max([int(v['version'][1:]) for v in entry['versions']] or [0]) + 1:04d}"
            entry["versions"].append({"version": version, "sha": sha, "createdAt": _now(), "source": source[:120], "note": note[:500]})
            made.update(version=version, created=True)
        if make_current or not entry.get("current"):
            entry["current"] = made["version"]
        entry.update(description=meta["description"], updatedAt=_now())
        all_[name] = entry
        return all_

    console.store.update(_key(workspace), {}, reserve)
    if made["created"]:
        try:
            # The library index is per workspace, the bucket per account and region: another workspace (or console) may
            # already hold this number. A conditional write never replaces it; the next free number is taken instead.
            s3 = client(session, "s3", ws["region"])
            for _ in range(200):
                try:
                    s3.put_object(Bucket=bucket, Key=f"skills/{name}/{made['version']}/SKILL.md", Body=text.encode("utf-8"),
                                  ContentType="text/markdown", IfNoneMatch="*")
                    break
                except Exception as exc:  # noqa: BLE001
                    if "PreconditionFailed" not in str(exc) and "(412)" not in str(exc):
                        raise
                    taken, nxt = made["version"], f"v{int(made['version'][1:]) + 1:04d}"

                    def renumber(all_: dict[str, Any], taken: str = taken, nxt: str = nxt) -> dict[str, Any]:
                        entry = all_[name]
                        mine = next(v for v in entry["versions"] if v["version"] == taken and v["sha"] == sha)  # only this save
                        while any(v["version"] == nxt for v in entry["versions"]):
                            nxt = f"v{int(nxt[1:]) + 1:04d}"
                        mine["version"] = nxt
                        if entry.get("current") == taken:
                            entry["current"] = nxt
                        made["version"] = nxt
                        return all_

                    console.store.update(_key(workspace), {}, renumber)
            else:
                raise SkillLabError(f"no free version number for {name}")
        except Exception:
            def undo(all_: dict[str, Any]) -> dict[str, Any]:
                entry = all_[name]
                entry["versions"] = [v for v in entry["versions"] if not (v["version"] == made["version"] and v["sha"] == sha)]
                if not entry["versions"]:
                    all_.pop(name)
                elif entry.get("current") == made["version"]:
                    entry["current"] = entry["versions"][-1]["version"]
                return all_

            console.store.update(_key(workspace), {}, undo)
            raise
    return {"name": name, "version": made["version"], "created": made["created"], "uri": skill_uri(bucket, name, made["version"])}


def _entry(console: Any, workspace: str, name: str) -> dict[str, Any]:
    entry = library(console, workspace).get(name)
    if not entry:
        raise SkillLabError(f"no skill {name} in this workspace's library")
    return entry


def get_skill(console: Any, workspace: str, name: str, version: str | None = None) -> dict[str, Any]:
    entry = _entry(console, workspace, name)
    version = version or entry["current"]
    if not any(v["version"] == version for v in entry["versions"]):
        raise SkillLabError(f"{name} has no version {version}")
    session, ws, bucket = _where(console, workspace)
    body = client(session, "s3", ws["region"]).get_object(Bucket=bucket, Key=f"skills/{name}/{version}/SKILL.md")["Body"].read()
    return {"name": name, "version": version, "current": entry["current"], "text": body.decode("utf-8"), "uri": skill_uri(bucket, name, version),
            "description": entry.get("description"), "versions": entry["versions"], "agents": sorted((entry.get("agents") or {}).values(),
                                                                                                     key=lambda a: a["name"])}


def set_current(console: Any, workspace: str, name: str, version: str) -> dict[str, Any]:
    def change(all_: dict[str, Any]) -> dict[str, Any]:
        entry = all_.get(name)
        if not entry or not any(v["version"] == version for v in entry["versions"]):
            raise SkillLabError(f"{name} has no version {version}")
        entry.update(current=version, updatedAt=_now())
        return all_

    console.store.update(_key(workspace), {}, change)
    return {"name": name, "current": version}


def delete_skill(console: Any, workspace: str, name: str) -> dict[str, Any]:
    """Forget a skill and delete its versions; refused while an agent uses it (take it off first)."""
    entry = _entry(console, workspace, name)
    if entry.get("agents"):
        raise SkillLabError(f"{name} is used by {', '.join(a['name'] for a in entry['agents'].values())}: take it off those agents first")
    session, ws, bucket = _where(console, workspace)
    s3 = client(session, "s3", ws["region"])
    # Only this workspace's own versions and its trainings' candidates: another workspace on the same account and region
    # may keep versions of a skill with the same name under the same prefix. Every object version goes (the bucket is
    # versioned: a plain delete would only add a delete marker).
    jobs = [j["id"] for j in console.jobs.list(workspace=workspace, kind="skill-train", limit=500)]
    prefixes = [f"skills/{name}/{v['version']}/" for v in entry["versions"]] + [f"skills/{name}/candidates/{j}/" for j in jobs]
    doomed = []
    for prefix in prefixes:
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix):
            doomed += [{"Key": o["Key"], "VersionId": o["VersionId"]} for o in (page.get("Versions") or []) + (page.get("DeleteMarkers") or [])]
    for i in range(0, len(doomed), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": doomed[i:i + 1000], "Quiet": True})
    console.store.update(_key(workspace), {}, lambda all_: {k: v for k, v in all_.items() if k != name})
    return {"deleted": name, "objects": len(doomed)}


def import_uri(console: Any, workspace: str, uri: str, name: str | None = None) -> dict[str, Any]:
    """Copy the SKILL.md under an S3 prefix (another stack's skill, say) into the library."""
    m = re.match(r"^s3://([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])/(.*)$", uri or "")
    if not m:
        raise SkillLabError("uri: s3://bucket/prefix/ holding a SKILL.md")
    bucket, prefix = m.group(1), m.group(2)
    key = prefix if prefix.endswith("SKILL.md") else prefix.rstrip("/") + "/SKILL.md"
    session, ws, _ = _where(console, workspace)
    text = client(session, "s3", ws["region"]).get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    meta, _body = frontmatter(text)
    return save_version(console, workspace, name or meta.get("name") or "", text, source=f"import:{uri}"[:120])


def import_pack(console: Any, workspace: str, project: str) -> list[dict[str, Any]]:
    """Every skill with frontmatter in a Workshop project's release pack."""
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", project or ""):
        raise SkillLabError("project: a Workshop project id")
    found = sorted((Path(console.workshop_data) / "projects" / project / "build" / "release" / "pack" / "skills").glob("*/SKILL.md"))
    out = []
    for path in found:
        text = path.read_text(encoding="utf-8")
        try:
            out.append(save_version(console, workspace, path.parent.name, text, source=f"pack:{project}"))
        except SkillLabError as exc:
            out.append({"name": path.parent.name, "error": str(exc)})
    if not found:
        raise SkillLabError(f"no skills in {project}'s release pack (generate and release it first)")
    return out


# -- agents -------------------------------------------------------------------------------------------------------------

def _harness(console: Any, workspace: str, harness_id: str, acknowledged: Any, verb: str) -> dict[str, Any]:
    ws = console.workspaces.get(workspace)
    harness = agents.get_agent(console.workspaces.session(workspace), ws["region"], "harness", str(harness_id or ""))
    if not harness.get("console") and acknowledged is not True:
        raise SkillLabError(f"{harness['name']} was not created from this console: send acknowledged: true to {verb}")
    return harness


def read_policy(bucket: str) -> dict[str, Any]:
    """Reading the library: every version and candidate under ``skills/`` of the console bucket, nothing else."""
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/skills/*"},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}", "Condition": {"StringLike": {"s3:prefix": ["skills/*"]}}}]}


def grant_read(session: Any, harness: Mapping[str, Any], bucket: str, *, boundary: str | None = None) -> None:
    """The Harness's role may read the library (the runtime fetches a skill with the agent's own role). In a workspace
    with a permissions boundary the role gets it first when it is the console's (the spoke changes only such roles)."""
    role = str(harness.get("executionRoleArn") or "").rsplit("/", 1)[-1]
    if not role:
        raise SkillLabError(f"{harness.get('name')} has no execution role to grant")
    agents.put_inline_policy(session, role, SKILLS_POLICY, read_policy(bucket), what="reading the skill library", boundary=boundary)


def runtime_read_policy(bucket: str) -> dict[str, Any]:
    """A Claude Agent SDK runtime's reading: the library (a call's skill versions and candidates) and the task files
    under ``skill-lab/assets/`` (a call's inputs), nothing else of the console bucket."""
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": [f"arn:aws:s3:::{bucket}/skills/*", f"arn:aws:s3:::{bucket}/{ASSET_PREFIX}*"]},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}",
         "Condition": {"StringLike": {"s3:prefix": ["skills/*", f"{ASSET_PREFIX}*"]}}}]}


def grant_runtime_read(session: Any, role: str, bucket: str, *, boundary: str | None = None) -> bool:
    """A Claude Agent SDK runtime's role may read the library and the task files (its calls fetch a skill version and
    their input files from S3 with it). True when the grant is new or changed: IAM takes a few seconds to apply it."""
    document = runtime_read_policy(bucket)
    try:
        current = client(session, "iam").get_role_policy(RoleName=role, PolicyName=SKILLS_POLICY).get("PolicyDocument")
        if isinstance(current, str):
            from urllib.parse import unquote

            current = json.loads(unquote(current))
        if current == document:
            return False
    except Exception as exc:  # noqa: BLE001 - not granted yet (NoSuchEntity), or not readable: granted below
        if "NoSuchEntity" not in str(exc) and "AccessDenied" not in str(exc):
            raise
    agents.put_inline_policy(session, role, SKILLS_POLICY, document, what="reading the skill library", boundary=boundary)
    return True


def base_skills(skills: Sequence[Mapping[str, Any]] | None, bucket: str, name: str) -> list[dict[str, Any]]:
    """An agent's configured skills without any library version of ``name``: the without-skill arm."""
    prefix = f"s3://{bucket}/skills/{name}/"
    return [dict(s) for s in skills or [] if not str(((s.get("s3") or {}).get("uri")) or "").startswith(prefix)]


def apply_to_agent(console: Any, workspace: str, name: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Point a Harness at a version of the skill (the current one by default), replacing any other version of it."""
    entry = _entry(console, workspace, name)
    version = str(body.get("version") or entry["current"])
    if not any(v["version"] == version for v in entry["versions"]):
        raise SkillLabError(f"{name} has no version {version}")
    harness = _harness(console, workspace, str(body.get("harnessId") or ""), body.get("acknowledged"), "change its skills")
    session, ws, bucket = _where(console, workspace)
    ctl = client(session, "bedrock-agentcore-control", ws["region"])
    current = ctl.get_harness(harnessId=harness["id"])["harness"]
    skills = base_skills(current.get("skills"), bucket, name) + [{"s3": {"uri": skill_uri(bucket, name, version)}}]
    grant_read(session, current, bucket, boundary=boundary_of(ws))
    ctl.update_harness(harnessId=harness["id"], skills=skills)  # a plain list (only memory is optionalValue)

    def record(all_: dict[str, Any]) -> dict[str, Any]:
        all_[name].setdefault("agents", {})[harness["id"]] = {"harnessId": harness["id"], "name": harness["name"], "version": version, "at": _now()}
        return all_

    console.store.update(_key(workspace), {}, record)
    return {"harness": harness["name"], "skill": name, "version": version, "skills": [s.get("s3", {}).get("uri") for s in skills]}


def remove_from_agent(console: Any, workspace: str, name: str, body: Mapping[str, Any]) -> dict[str, Any]:
    _entry(console, workspace, name)
    harness = _harness(console, workspace, str(body.get("harnessId") or ""), body.get("acknowledged"), "change its skills")
    session, ws, bucket = _where(console, workspace)
    ctl = client(session, "bedrock-agentcore-control", ws["region"])
    current = ctl.get_harness(harnessId=harness["id"])["harness"]
    skills = base_skills(current.get("skills"), bucket, name)
    ctl.update_harness(harnessId=harness["id"], skills=skills)
    role = str(current.get("executionRoleArn") or "").rsplit("/", 1)[-1]
    if role and not any(str(((s.get("s3") or {}).get("uri")) or "").startswith(f"s3://{bucket}/skills/") for s in skills):
        try:  # no library skill left on the agent: its read grant goes too
            client(session, "iam").delete_role_policy(RoleName=role, PolicyName=SKILLS_POLICY)
        except Exception as exc:  # noqa: BLE001 - absent, or a role this workspace may not change
            if "NoSuchEntity" not in str(exc) and "AccessDenied" not in str(exc):
                raise

    def forget(all_: dict[str, Any]) -> dict[str, Any]:
        (all_[name].get("agents") or {}).pop(harness["id"], None)
        return all_

    console.store.update(_key(workspace), {}, forget)
    return {"harness": harness["name"], "skill": name, "removed": True}


# -- Claude Agent SDK runtimes ------------------------------------------------------------------------------------------

def claude_runtime(console: Any, workspace: str, runtime_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The Claude Agent SDK agent (claude_sdk's record) a runtime was deployed from, and its newest successful
    deployment to that runtime (None while there is none): what the runtime's image holds (its skills, model, template)."""
    from . import claude_sdk  # claude_sdk imports this module

    rid = str(runtime_id or "")
    for agent in claude_sdk.list_agents(console, workspace):
        mine = [d for d in agent.get("deployments") or [] if d.get("runtimeId") == rid]
        if mine:
            return agent, next((d for d in mine if d.get("jobStatus") == "succeeded"), None)
    raise SkillLabError(f"{rid or 'runtimeId'} is not a runtime deployed from the Claude Agent SDK template in this workspace "
                        "(部署代码 → 从模板生成: Claude Agent SDK)")


def targets(console: Any, workspace: str) -> list[dict[str, Any]]:
    """What a skill can be evaluated on: the workspace's Harnesses and its runtimes deployed from the Claude Agent SDK
    template (each with what its image holds, why it cannot be used yet when it cannot, and — ``inputFiles`` — whether
    its calls take a task's input files)."""
    from . import claude_sdk

    ws = console.workspaces.get(workspace)
    listed = agents.list_agents(console.workspaces.session(workspace), ws["region"])
    status = {a["id"]: a.get("status") for a in listed}
    out = [{"kind": "harness", "id": a["id"], "name": a["name"], "status": a.get("status")} for a in listed if a["kind"] == "harness"]
    for agent in claude_sdk.list_agents(console, workspace):
        live = next((d for d in agent.get("deployments") or [] if d.get("jobStatus") == "succeeded" and d.get("runtimeId")), None)
        if not live:
            continue
        rid = live["runtimeId"]
        note = None
        if int(live.get("template") or 1) < RUNTIME_TEMPLATE:
            note = "旧模板部署的：不能按次指定技能、不报告产出的文件。先在「部署代码」给它部署一个新版本"
        elif status.get(rid) not in ("READY", None):
            note = f"现在是 {status.get(rid)}"
        elif rid not in status:
            note = "这个 Runtime 已经不在了"
        out.append({"kind": "runtime", "id": rid, "name": agent["name"], "status": status.get(rid) or "MISSING", "template": live.get("template") or 1,
                    "skills": sorted(live.get("skills") or {}), "model": live.get("model") or (agent.get("spec") or {}).get("model"), "note": note,
                    "inputFiles": int(live.get("template") or 1) >= FILES_TEMPLATE})
    return out


def redeploy_runtime(console: Any, workspace: str, name: str, version: str, runtime_id: str, caller: Mapping[str, Any] | None) -> dict[str, Any]:
    """Publish a skill version to a Claude Agent SDK runtime: its image holds its skills (the scan gate passed that
    image, agent_spec.json records the versions), so the runtime gets a new version built with this one —
    claude_sdk's redeploy (``runtimeId``), the scan gate and the smoke call included. A skill the spec pins moves to
    this version; one it does not pin follows the library's current version, which this now is."""
    from . import claude_sdk

    agent, _live = claude_runtime(console, workspace, runtime_id)
    spec = dict(agent.get("spec") or {})
    skills = [dict(s) for s in spec.get("skills") or []]
    pinned = any(s.get("name") == name and s.get("version") for s in skills)
    if any(s.get("name") == name for s in skills):
        skills = [{**s, "version": version} if s.get("name") == name and s.get("version") else s for s in skills]
    else:
        skills.append({"name": name, "version": None})
    job = claude_sdk.deploy_agent(console, workspace, {**spec, "skills": skills, "runtimeId": runtime_id}, caller or {"username": "skill-lab"})
    return {"jobId": job["id"], "runtimeId": runtime_id, "runtime": agent["name"], "skill": name, "version": version, "pinned": pinned}


# -- task input files (Launchpad's task assets) -------------------------------------------------------------------------

def asset_key(sha: str) -> str:
    return ASSET_PREFIX + sha


def asset_uri(bucket: str, sha: str) -> str:
    return f"s3://{bucket}/{ASSET_PREFIX}{sha}"


def _assets_key(workspace: str) -> str:
    return f"skill_assets_{workspace}"


def assets_catalog(store: Any, workspace: str) -> dict[str, dict[str, Any]]:
    """The workspace's uploaded task files by SHA-256: type, media type, bytes, the names they were uploaded as."""
    return dict(store.read(_assets_key(workspace), {}))


def _check_xlsx(name: str, data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            names = {i.filename for i in infos}
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise ValueError("the workbook's members are missing")
            if len(infos) > _XLSX_MAX_MEMBERS:
                raise ValueError("too many members")
            if sum(i.file_size for i in infos) > _XLSX_MAX_UNPACKED:
                raise ValueError("it unpacks to more than 200 MiB")
            if any(i.file_size > max(1, i.compress_size) * _XLSX_MAX_RATIO for i in infos):
                raise ValueError("a member is compressed more than 100 times (a zip bomb?)")
            if any(i.filename.lower().endswith(("vbaproject.bin", ".xlsm")) for i in infos):
                raise ValueError("a workbook with macros is not taken")
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        raise SkillLabError(f"{name} is not a usable xlsx: {exc}") from exc


def sniff_asset(name: str, data: bytes) -> str:
    """A task file's type, from its extension checked against its content as Launchpad checks a task asset: a text type
    is UTF-8 without NUL and carries no binary signature; a binary one starts with its own (an xlsx is a workbook: its
    members there, no macros, no zip bomb)."""
    kind = ASSET_EXTENSIONS.get(Path(name).suffix.lower())
    if kind is None:
        raise SkillLabError(f"{name}: a task file is {', '.join(sorted({k.lstrip('.') for k in ASSET_EXTENSIONS}))}")
    if kind in ("md", "txt", "csv"):
        if any(data.startswith(s) for s in _BINARY_SIGNATURES):
            raise SkillLabError(f"{name} is not a {kind} file: its content is binary")
        if b"\x00" in data:
            raise SkillLabError(f"{name} holds NUL bytes: not a {kind} file")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillLabError(f"{name} is not UTF-8 text ({exc.reason} at byte {exc.start}): save it as UTF-8") from exc
        return kind
    head = data[:16]
    found = ("pdf" if head.startswith(b"%PDF-") else "png" if head.startswith(b"\x89PNG\r\n\x1a\n") else "jpg" if head.startswith(b"\xff\xd8\xff")
             else "webp" if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP" else "xlsx" if head.startswith(b"PK\x03\x04") else None)
    if found != kind:
        raise SkillLabError(f"{name} is not a {kind} file (its content does not match its extension)")
    if kind == "xlsx":
        _check_xlsx(name, data)
    return kind


def _missing_object(exc: BaseException) -> bool:
    code = str(((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code") or "")
    return code in ("404", "NoSuchKey", "NotFound") or "Not Found" in str(exc) or "NoSuchKey" in str(exc)


def _has_object(s3: Any, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception as exc:  # noqa: BLE001
        if _missing_object(exc):
            return False
        raise


def upload_assets(console: Any, workspace: str, files: Any) -> dict[str, Any]:
    """Task files, base64 in the body (``[{"name", "contentBase64"}]``), each kept once by its content: the object
    ``skill-lab/assets/<sha256>`` of the console bucket (written only when it is not there yet) and an entry of the
    workspace's catalog. A task names one as ``{"path", "asset": "sha256:<hex>"}``."""
    if not isinstance(files, list) or not files:
        raise SkillLabError('files: at least one {"name", "contentBase64"}')
    if len(files) > MAX_UPLOAD_FILES:
        raise SkillLabError(f"files: at most {MAX_UPLOAD_FILES} an upload")
    decoded: list[tuple[str, str, bytes]] = []
    total = 0
    for i, f in enumerate(files):
        name = str(f.get("name") or "") if isinstance(f, Mapping) else ""
        if (not name or len(name) > 120 or "/" in name or "\\" in name or name.startswith(".") or _CONTROL.search(name)
                or name.strip() != name):
            raise SkillLabError(f"files[{i}].name: a plain file name (no folder, not hidden, at most 120 characters)")
        try:
            data = base64.b64decode(str(f.get("contentBase64") or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SkillLabError(f"{name}: contentBase64 is not base64") from exc
        if not data:
            raise SkillLabError(f"{name} is empty")
        if len(data) > MAX_ASSET_BYTES:
            raise SkillLabError(f"{name} is {len(data) / 1024 / 1024:.1f} MiB: at most {MAX_ASSET_BYTES // 1024 // 1024} MiB a file")
        total += len(data)
        if total > MAX_UPLOAD_BYTES:
            raise SkillLabError(f"an upload is at most {MAX_UPLOAD_BYTES // 1024 // 1024} MiB")
        decoded.append((name, sniff_asset(name, data), data))
    folded = [n.casefold() for n, _k, _d in decoded]
    if len(set(folded)) != len(folded):
        raise SkillLabError("files: two of them have the same name")
    session, ws, bucket = _where(console, workspace)
    s3 = client(session, "s3", ws["region"])
    out = []
    for name, kind, data in decoded:
        sha = hashlib.sha256(data).hexdigest()
        created = not _has_object(s3, bucket, asset_key(sha))
        if created:
            s3.put_object(Bucket=bucket, Key=asset_key(sha), Body=data, ContentType=ASSET_TYPES[kind])

        def record(all_: dict[str, Any], sha: str = sha, name: str = name, kind: str = kind, size: int = len(data)) -> dict[str, Any]:
            prior = all_.get(sha) or {"sha256": sha, "type": kind, "mediaType": ASSET_TYPES[kind], "bytes": size, "names": [], "uploadedAt": _now()}
            prior["names"] = (list(prior.get("names") or []) + ([name] if name not in (prior.get("names") or []) else []))[-20:]
            all_[sha] = {**prior, "updatedAt": _now()}
            return all_

        console.store.update(_assets_key(workspace), {}, record)
        out.append({"asset": f"sha256:{sha}", "sha256": sha, "name": name, "path": name, "type": kind, "mediaType": ASSET_TYPES[kind],
                    "bytes": len(data), "created": created})
    return {"assets": out}


def _task_lists(ts: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [t for k in ("tasks",) + SPLITS for t in ts.get(k) or []]


def _asset_users(store: Any, workspace: str) -> dict[str, list[dict[str, Any]]]:
    """Which task sets name each file (by SHA-256), and on how many of their tasks."""
    out: dict[str, list[dict[str, Any]]] = {}
    for ts in store.read(_sets_key(workspace), {}).values():
        counted: dict[str, int] = {}
        for t in _task_lists(ts):
            for f in t.get("files") or []:
                sha = str(f.get("asset") or "").removeprefix("sha256:")
                counted[sha] = counted.get(sha, 0) + 1
        for sha, n in counted.items():
            out.setdefault(sha, []).append({"id": ts["id"], "name": ts["name"], "tasks": n})
    return out


def list_assets(console: Any, workspace: str) -> list[dict[str, Any]]:
    users = _asset_users(console.store, workspace)
    return [{**rec, "asset": f"sha256:{sha}", "usedBy": users.get(sha, [])}
            for sha, rec in sorted(assets_catalog(console.store, workspace).items(), key=lambda kv: str(kv[1].get("updatedAt") or ""), reverse=True)]


def delete_asset(console: Any, workspace: str, asset: str) -> dict[str, Any]:
    """Forget an uploaded file; refused while a task set names it. Its object stays: it is stored by its content, so
    another console on the same account and region (whose catalog this one cannot see) may hold the same file."""
    sha = str(asset or "").removeprefix("sha256:")
    if not SHA256.match(sha):
        raise SkillLabError("not a task file id (sha256:<64 hex>)")
    rec = assets_catalog(console.store, workspace).get(sha)
    if not rec:
        raise SkillLabError(f"no task file sha256:{sha[:12]}… in this workspace")
    users = _asset_users(console.store, workspace).get(sha)
    if users:
        raise SkillLabError(f"{', '.join(rec.get('names') or [sha[:12]])} is a file of the task set {', '.join(u['name'] for u in users)}: "
                            "delete the set or take the file off its tasks first")
    console.store.update(_assets_key(workspace), {}, lambda all_: {k: v for k, v in all_.items() if k != sha})
    return {"deleted": f"sha256:{sha}", "objects": 0, "kept": "the object is stored by its content and may be another console's too"}


def check_file_path(path: str, where: str) -> str:
    """Where a task file lands in the agent's working directory: a plain relative path of at most 4 levels, no hidden
    names (``.claude`` is Claude Code's), its extension one of the task file types."""
    parts = path.split("/")
    if (not path or len(path) > 240 or len(parts) > 4 or path.startswith(("/", "~")) or "\\" in path or _CONTROL.search(path)
            or any(p in ("", ".", "..") or p.startswith(".") or p.strip() != p for p in parts)):
        raise SkillLabError(f"{where}: {path!r} is not a plain relative path (at most 4 levels, no '..', no hidden names)")
    return path


def _task_files(raw: Any, where: str, assets: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A task's ``files`` as stored: ``[{"path", "asset": "sha256:<hex>", "bytes", "type"}]``, from a list of
    ``{"path" (or "name"), "asset"}`` or Launchpad's ``{path: {"asset": "sha256:…", …}}``; each an uploaded file."""
    if raw in (None, [], {}):
        return []
    if isinstance(raw, Mapping):
        pairs = [(str(k), v) for k, v in raw.items()]
    elif isinstance(raw, list):
        pairs = [(str(v.get("path") or v.get("name") or "") if isinstance(v, Mapping) else "", v) for v in raw]
    else:
        raise SkillLabError(f'{where}.files: a list of {{"path", "asset"}}')
    if len(pairs) > MAX_TASK_FILES:
        raise SkillLabError(f"{where}.files: at most {MAX_TASK_FILES} a task")
    out, seen, total = [], set(), 0
    for path, ref in pairs:
        if isinstance(ref, str):
            raise SkillLabError(f"{where}.files: {path} is text written inline: upload it as a file (Skill Lab → 输入文件) and name it here")
        if not isinstance(ref, Mapping):
            raise SkillLabError(f'{where}.files: each one {{"path", "asset": "sha256:…"}}')
        sha = str(ref.get("asset") or ref.get("sha256") or "").removeprefix("sha256:")
        if not SHA256.match(sha):
            raise SkillLabError(f"{where}.files: {path or 'a file'} needs its asset (the sha256:… its upload answered)")
        rec = assets.get(sha)
        if not rec:
            raise SkillLabError(f"{where}.files: sha256:{sha[:12]}… is not a file uploaded to this workspace: upload it first")
        path = check_file_path(path or str((rec.get("names") or [""])[0]), f"{where}.files")
        if ASSET_EXTENSIONS.get(Path(path).suffix.lower()) != rec["type"]:
            raise SkillLabError(f"{where}.files: {path} must end in the file's own extension (it is a {rec['type']} file)")
        if path.casefold() in seen:
            raise SkillLabError(f"{where}.files: {path} twice")
        seen.add(path.casefold())
        total += int(rec["bytes"])
        if total > MAX_TASK_BYTES:
            raise SkillLabError(f"{where}.files: at most {MAX_TASK_BYTES // 1024 // 1024} MiB a task")
        out.append({"path": path, "asset": f"sha256:{sha}", "bytes": int(rec["bytes"]), "type": rec["type"]})
    return out


def inputs_of(tasks: Sequence[Mapping[str, Any]]) -> list[str]:
    """The ids of the tasks that give the agent input files."""
    return [t["id"] for t in tasks if t.get("files")]


# -- task sets ----------------------------------------------------------------------------------------------------------

def _items(raw: Any, label: str, *, required: bool = True, assets: Mapping[str, Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    if not raw:
        if required:
            raise SkillLabError(f"{label}: at least one task")
        return []
    if not isinstance(raw, list) or len(raw) > MAX_TASKS:
        raise SkillLabError(f"{label}: a list of at most {MAX_TASKS} tasks")
    out = []
    for i, item in enumerate(raw):
        item = item if isinstance(item, Mapping) else {}
        tid = str(item.get("id") or f"{label}-{i + 1}")
        question, rubric = str(item.get("question") or "").strip(), str(item.get("rubric") or "").strip()
        if not TASK_ID.match(tid):
            raise SkillLabError(f"{label}[{i}].id: letters, digits, '.', '_' or '-'")
        if not question or len(question) > 4000:
            raise SkillLabError(f"{label}[{i}] ({tid}): question is required (at most 4000 characters)")
        if not rubric or len(rubric) > 4000:
            raise SkillLabError(f"{label}[{i}] ({tid}): rubric is required (at most 4000 characters): what a passing answer must do")
        files = _task_files(item.get("files"), f"{label}[{i}] ({tid})", assets or {})
        out.append({"id": tid, "question": question, "rubric": rubric, **({"files": files} if files else {})})
    return out


def check_taskset(body: Mapping[str, Any], assets: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """A task set as stored; ``assets`` (the workspace's uploaded files) resolves the tasks' ``files``."""
    name = str(body.get("name") or "").strip()[:80]
    if not name:
        raise SkillLabError("name is required")
    mode = str(body.get("mode") or ("split" if any(body.get(k) for k in SPLITS) else "single"))
    if mode == "single":
        parts = {"tasks": _items(body.get("tasks"), "tasks", assets=assets)}
    elif mode == "split":
        parts = {k: _items(body.get(k), k, required=k != "test", assets=assets) for k in SPLITS}
    else:
        raise SkillLabError("mode: single or split")
    ids = [t["id"] for items in parts.values() for t in items]
    if len(ids) != len(set(ids)):
        raise SkillLabError("task ids must be unique across the set")
    refs = [f for items in parts.values() for t in items for f in t.get("files") or []]
    if len(refs) > MAX_SET_FILES:
        raise SkillLabError(f"a task set names at most {MAX_SET_FILES} files")
    if sum({f["asset"]: f["bytes"] for f in refs}.values()) > MAX_SET_BYTES:
        raise SkillLabError(f"a task set's files are at most {MAX_SET_BYTES // 1024 // 1024} MiB")
    return {"name": name, "description": str(body.get("description") or "")[:500], "mode": mode, **parts}


def _sets_key(workspace: str) -> str:
    return f"skill_tasksets_{workspace}"


def put_taskset(store: Any, workspace: str, body: Mapping[str, Any]) -> dict[str, Any]:
    ts = check_taskset(body, assets_catalog(store, workspace))
    tid = str(body.get("id") or f"ts-{secrets.token_hex(5)}")
    if not re.match(r"^ts-[0-9a-f]{10}$", tid):
        raise SkillLabError("id: a task set id (ts-…)")

    def change(all_: dict[str, Any]) -> dict[str, Any]:
        prior = all_.get(tid) or {}
        all_[tid] = {**ts, "id": tid, "createdAt": prior.get("createdAt") or _now(), "updatedAt": _now()}
        return all_

    return store.update(_sets_key(workspace), {}, change)[tid]


def _counts(ts: Mapping[str, Any]) -> dict[str, int]:
    return {"tasks": len(ts.get("tasks") or [])} if ts["mode"] == "single" else {k: len(ts.get(k) or []) for k in SPLITS}


def tasksets(store: Any, workspace: str) -> list[dict[str, Any]]:
    """The workspace's task sets; ``files``: how many tasks give the agent input files."""
    return [{"id": t["id"], "name": t["name"], "description": t.get("description"), "mode": t["mode"], "counts": _counts(t),
             "files": len(inputs_of(_task_lists(t))), "updatedAt": t.get("updatedAt")}
            for t in sorted(store.read(_sets_key(workspace), {}).values(), key=lambda t: t.get("updatedAt") or "", reverse=True)]


def taskset(store: Any, workspace: str, tid: str) -> dict[str, Any]:
    found = store.read(_sets_key(workspace), {}).get(tid)
    if not found:
        raise SkillLabError(f"no task set {tid} in this workspace")
    return found


def delete_taskset(store: Any, workspace: str, tid: str) -> dict[str, Any]:
    taskset(store, workspace, tid)
    store.update(_sets_key(workspace), {}, lambda all_: {k: v for k, v in all_.items() if k != tid})
    return {"deleted": tid}


def split(ts: Mapping[str, Any], which: str) -> list[dict[str, str]]:
    """A split's tasks. A single list splits 4:3:3 by a hash of each id, so a task always lands in the same split."""
    if which not in SPLITS + ("all",):
        raise SkillLabError("split: train, val, test or all")
    if ts["mode"] == "split":
        return [t for k in SPLITS for t in ts.get(k) or []] if which == "all" else list(ts.get(which) or [])
    tasks = list(ts.get("tasks") or [])
    if which == "all":
        return tasks
    ordered = sorted(tasks, key=lambda t: hashlib.sha256(t["id"].encode("utf-8")).hexdigest())
    a = round(len(ordered) * 0.4)
    b = a + round(len(ordered) * 0.3)
    return {"train": ordered[:a], "val": ordered[a:b], "test": ordered[b:]}[which]


# -- models -------------------------------------------------------------------------------------------------------------

def converse(rt: Any, model: str, system: str, user: str, *, max_tokens: int = 4096, temperature: float | None = 0.0,
             images: Sequence[Mapping[str, Any]] = ()) -> str:
    """One Converse turn; ``images`` (``{"label", "format", "bytes"}``) follow the text, each after a line naming it."""
    content: list[dict[str, Any]] = [{"text": user}]
    for number, image in enumerate(images, 1):
        content += [{"text": f"Image {number}: {image['label']}"}, {"image": {"format": image["format"], "source": {"bytes": image["bytes"]}}}]
    request: dict[str, Any] = {"modelId": model, "system": [{"text": system}], "messages": [{"role": "user", "content": content}],
                               "inferenceConfig": {"maxTokens": max_tokens}}
    if temperature is not None:
        request["inferenceConfig"]["temperature"] = temperature
    try:
        out = rt.converse(**request)
    except Exception as exc:  # noqa: BLE001 - a model that takes no temperature: once more without it
        if temperature is None or "temperature" not in str(exc):
            raise
        request["inferenceConfig"].pop("temperature")
        out = rt.converse(**request)
    return "".join(b.get("text") or "" for b in (out.get("output") or {}).get("message", {}).get("content") or [])


def _json(text: str) -> Any:
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    for candidate in fenced + [text]:
        candidate = candidate.strip()
        for opener, closer in (("{", "}"), ("[", "]")):
            i, j = candidate.find(opener), candidate.rfind(closer)
            if i != -1 and j > i:
                try:
                    return json.loads(candidate[i:j + 1])
                except ValueError:
                    continue
    raise SkillLabError("the model's reply held no JSON")


JUDGE_SYSTEM = """You grade one answer of an AI agent against the task's rubric.
The rubric lists what a passing answer must do. Judge only against the rubric: not your own knowledge, not style the rubric does not ask for.
Reply with JSON only: {"pass": true or false, "score": 0.0 to 1.0, "reason": "one or two sentences, in the rubric's language"}.
"score" is the fraction of the rubric's requirements the answer meets; "pass" is true only when it meets every one."""


#: For an agent that works on files (a Claude Agent SDK runtime): the judge reads the files themselves.
JUDGE_FILES = """
The agent works in a directory of its own. After its answer come the files it created or changed there, each with a view extracted from the file itself: a spreadsheet as its sheets' cells (formulas as written; frozen panes and a bold header row noted; a text cell that looks like a number shown in quotes), a CSV or text file as it is, a PDF's or a Word document's text, a presentation's slides (titles, text, tables, chart data, notes, pictures).
When the rubric asks for a file or for what it holds, judge from that view, never from what the answer says about the file. A file that is not listed was not produced."""
#: The task gave the agent files: they are not its work.
JUDGE_INPUTS = """
The task gave the agent input files, listed before its files: they were in its directory before it began and are not its work. An input file it changed is listed among its files as changed; one that is not listed there was left as it was."""
#: Pictures come as images after the text.
JUDGE_IMAGES = """
An image the agent made, and a picture inside a document it made, comes after the text as the image itself, labelled with its file. Judge what the rubric asks of a picture (a chart's kind, bars, order, colours, labels, title; what an image shows) from the image, never from what the answer says about it."""
#: How much of the files' views the judge reads.
JUDGE_FILES_CHARS = 16_000
#: Pictures a judgment shows: at most this many, each within Bedrock's 3.75 MB for an image, this much in all.
JUDGE_IMAGES_MAX, JUDGE_IMAGE_BYTES, JUDGE_IMAGES_BYTES = 6, 3_750_000, 10_000_000


def image_format(data: bytes) -> str | None:
    """An image's Converse format from its signature (png, jpeg, gif, webp), None for anything else."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _dims(pic: Mapping[str, Any]) -> str:
    return f", {pic['width']}×{pic['height']} px" if pic.get("width") and pic.get("height") else ""


def judge_images(artifacts: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[tuple[str, str | None], str]]:
    """The pictures a judgment shows, in the files' order: each image the turn made (the runtime's preview of it, or
    — an older template's answer — a small image's own bytes) and each picture inside a document it made, within
    the caps. Also why each one left out was, by ``(file, picture)``."""
    shown: list[dict[str, Any]] = []
    left_out: dict[tuple[str, str | None], str] = {}
    total = 0
    for a in artifacts:
        path = str(a.get("path") or "")
        candidates: list[tuple[str | None, Mapping[str, Any], Any]] = []
        if a.get("type") == "image":
            pic = a.get("image") or {}
            candidates.append((None, pic, (pic.get("preview") or {}).get("base64") or a.get("base64")))
        for pic in a.get("images") or []:
            candidates.append((str(pic.get("name") or "?"), pic, (pic.get("preview") or {}).get("base64")))
        for name, pic, raw in candidates:
            why = None
            data = b""
            if not raw:
                why = pic.get("error") or pic.get("note") or "the runtime sent no preview of it"
            else:
                try:
                    data = base64.b64decode(str(raw), validate=True)
                except (binascii.Error, ValueError):
                    why = "its preview is not base64"
            fmt = image_format(data) if data else None
            if why is None and fmt is None:
                why = "not an image format the judge model takes"
            elif why is None and len(data) > JUDGE_IMAGE_BYTES:
                why = f"its preview is {len(data) / 1e6:.1f} MB (at most {JUDGE_IMAGE_BYTES / 1e6:.2f} MB an image)"
            elif why is None and (len(shown) >= JUDGE_IMAGES_MAX or total + len(data) > JUDGE_IMAGES_BYTES):
                why = f"a judgment shows at most {JUDGE_IMAGES_MAX} pictures"
            if why:
                left_out[(path, name)] = str(why)[:200]
                continue
            label = f"{path}{f' → {name}' if name else ''}{_dims(pic)}"
            shown.append({"path": path, "name": name, "label": label, "format": fmt, "bytes": data})
            total += len(data)
    return shown, left_out


def inputs_view(files: Sequence[Mapping[str, Any]]) -> str:
    return (f"Input files the task gave the agent ({len(files)}; in its directory before it began, not its work):\n"
            + "\n".join(f"- {f.get('path')} · {f.get('type')} · {f.get('bytes')} bytes" for f in files))


def files_view(artifacts: Sequence[Mapping[str, Any]], limit: int = JUDGE_FILES_CHARS, images: Sequence[Mapping[str, Any]] = (),
               left_out: Mapping[tuple[str, str | None], str] | None = None) -> str:
    """The files a turn created or changed as the judge reads them: a header line each, then its text view (and
    which attached image each picture is)."""
    if not artifacts:
        return "Files the agent created or changed in its working directory: none."
    number = {(i["path"], i["name"]): n for n, i in enumerate(images, 1)}
    left_out = left_out or {}

    def picture(path: str, name: str | None, pic: Mapping[str, Any]) -> str:
        if (path, name) in number:
            return f"attached below as Image {number[(path, name)]}"
        return f"not shown: {left_out.get((path, name)) or 'no preview'}"

    parts, left = [], limit
    for a in artifacts:
        path = str(a.get("path") or "")
        if a.get("error"):
            body = f"(could not be read: {a['error']})"
        elif a.get("type") == "image":
            body = f"(an image{_dims(a.get('image') or {})}: {picture(path, None, a.get('image') or {})})"
        else:
            body = str(a.get("text") or "") or "(no text view for this type of file)"
        pictures = [f"- {p.get('name')}{_dims(p)}: {picture(path, str(p.get('name') or '?'), p)}" for p in a.get("images") or []]
        if pictures:
            more = f" (of {a['imagesTotal']})" if a.get("imagesTotal") else ""
            body += f"\nPictures in this file{more}:\n" + "\n".join(pictures)
        shown = body[:max(0, left)]
        cut = len(shown) < len(body) or bool(a.get("truncated"))
        status = a.get("status") or "created"
        parts.append(f"--- {path} · {a.get('type')} · {a.get('bytes')} bytes · {status}{' (an input file)' if a.get('input') else ''}"
                     f"{' · truncated' if cut else ''} ---\n{shown}")
        left -= len(shown)
    return f"Files the agent created or changed in its working directory ({len(artifacts)}):\n" + "\n\n".join(parts)


def judge(rt: Any, model: str, task: Mapping[str, Any], answer: str, artifacts: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """The answer against the task's rubric; with ``artifacts`` (a runtime's answer) the judge reads the files too —
    the task's input files named, each picture the turn made shown as an image."""
    user = f"Question:\n{task['question']}\n\nRubric:\n{task['rubric']}\n\nAnswer:\n{answer.strip() or '(no answer)'}"
    system = JUDGE_SYSTEM
    images: list[dict[str, Any]] = []
    if artifacts is not None:
        images, left_out = judge_images(artifacts)
        inputs = list(task.get("files") or [])
        user += "\n\n" + (inputs_view(inputs) + "\n\n" if inputs else "") + files_view(artifacts, images=images, left_out=left_out)
        system = JUDGE_SYSTEM + JUDGE_FILES + (JUDGE_INPUTS if inputs else "") + (JUDGE_IMAGES if images else "")
    last: Exception | None = None
    for _ in range(2):
        try:
            got = _json(converse(rt, model, system, user, max_tokens=800, images=images))
            score = max(0.0, min(1.0, float(got.get("score"))))
            out = {"pass": bool(got.get("pass")) and score > 0, "score": round(score, 3), "reason": str(got.get("reason") or "")[:600]}
            if images:  # what the judge looked at besides text, for the results
                out["judgedImages"] = [i["label"] for i in images]
            return out
        except (SkillLabError, ValueError, TypeError, AttributeError) as exc:
            last = exc
    return {"pass": False, "score": 0.0, "reason": f"the judge's reply could not be read: {last}", "invalid": True}


# -- evaluation ---------------------------------------------------------------------------------------------------------

#: Runtime failures that are the platform's, not the agent's (live 2026-10-01: three 502s in one batch of 18 calls).
TRANSIENT = re.compile(r"\((?:502|503|504)\)|ThrottlingException|ServiceUnavailable|InternalServerException|EndpointConnectionError|"
                       r"ReadTimeoutError|ConnectionClosedError|SSLError")
ATTEMPTS = 3


def rollout(session: Any, region: str, data: Any, harness: Mapping[str, Any], task: Mapping[str, Any], skills: list[dict[str, Any]],
            skill_name: str, model: str | None) -> dict[str, Any]:
    """One answer: fresh session, own actor; whether the model loaded ``skill_name`` (the skills tool's input names it).

    A platform failure with no answer is tried again (a new session each time); one that persists is marked ``infra``
    and is left out of the rates rather than counted against the skill."""
    for attempt in range(ATTEMPTS):
        out = _rollout_once(session, region, data, harness, task, skills, skill_name, model)
        if out["text"] or not out["error"] or not TRANSIENT.search(str(out["error"])):
            break
        out["infra"] = True
        time.sleep(3 * (attempt + 1))
    out["attempts"] = attempt + 1
    return out


def _rollout_once(session: Any, region: str, data: Any, harness: Mapping[str, Any], task: Mapping[str, Any], skills: Any,
                  skill_name: str, model: str | None) -> dict[str, Any]:
    if harness.get("kind") == "runtime":
        return _runtime_once(data, harness, task, skills, skill_name, model)
    out: dict[str, Any] = {"text": "", "tools": [], "loaded": [], "error": None, "seconds": None}
    actor = f"skilllab-{uuid.uuid4().hex[:16]}"
    for event in agents.invoke(session, region=region, agent=harness, message=task["question"], session_id=None, actor=actor, model=model,
                               skills=skills, data=data):
        kind = event["type"]
        if kind == "session":
            out["sessionId"] = event["sessionId"]
        elif kind == "text":
            out["text"] += event["text"]
        elif kind == "tool":
            out["tools"].append(event["name"])
        elif kind == "toolInput" and event["name"] == "skills":
            got = event.get("input")
            out["loaded"].append(str(got.get("skill_name") if isinstance(got, Mapping) else got))
        elif kind == "error":
            out["error"] = event["error"]
        elif kind == "stop":
            out["seconds"] = event.get("seconds")
    out["skillLoaded"] = skill_name in out["loaded"]
    return out


#: What a result keeps of each file a runtime's turn made (the runtime already caps the views and the inline bytes):
#: ``image`` / ``images`` (template 3) are an image's, or a document's pictures', size and thumbnail — their larger
#: preview goes to the judge and is not kept.
ARTIFACT_KEYS = ("path", "bytes", "type", "status", "text", "truncated", "table", "sheets", "pages", "slides", "error", "base64", "sha256", "image",
                 "images", "imagesTotal")


def call_files(task: Mapping[str, Any], bucket: str) -> list[dict[str, Any]]:
    """A task's input files as its runtime call names them: where each goes, its object, its SHA-256 and size."""
    out = []
    for f in task.get("files") or []:
        sha = str(f["asset"]).removeprefix("sha256:")
        out.append({"path": f["path"], "uri": asset_uri(bucket, sha), "sha256": sha, "bytes": int(f["bytes"])})
    return out


def without_previews(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """A file as a result keeps it: its pictures' previews (the judge's) dropped, their thumbnails kept."""
    out = dict(artifact)
    if isinstance(out.get("image"), Mapping):
        out["image"] = {k: v for k, v in out["image"].items() if k != "preview"}
    if isinstance(out.get("images"), list):
        out["images"] = [{k: v for k, v in p.items() if k != "preview"} if isinstance(p, Mapping) else p for p in out["images"]]
    return out


def runtime_arm(others: Sequence[str], name: str, uri: str | None) -> dict[str, Any]:
    """A Claude Agent SDK runtime's arm, as its call's payload: the agent's other skills in both arms, plus — with
    ``uri`` — this skill, that version read from S3 for the call (``skills: []`` when there is nothing else)."""
    return {"skills": list(others) + ([name] if uri else []), "skillOverrides": [{"name": name, "uri": uri}] if uri else []}


def _stop_session(data: Any, arn: str, session_id: str) -> None:
    """End a rollout's runtime session (its microVM) in the background: it would idle out anyway."""
    def stop() -> None:
        try:
            data.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=session_id)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=stop, name=f"stop-{session_id[:24]}", daemon=True).start()


def _runtime_once(data: Any, agent: Mapping[str, Any], task: Mapping[str, Any], arm: Mapping[str, Any], skill_name: str,
                  model: str | None) -> dict[str, Any]:
    """One answer of a Claude Agent SDK runtime: InvokeAgentRuntime ``{"prompt", "format": "json", "skills",
    "skillOverrides"}`` — and the task's input files, ``files`` — in a session of its own (its own microVM); the
    trigger is the turn's ``Skill`` call naming the skill, the files the turn made come back as ``artifacts`` (each
    with a text view the judge reads, an image or a document's pictures with a preview it sees)."""
    session_id = f"skilllab-{uuid.uuid4().hex}-{int(time.time())}"
    out: dict[str, Any] = {"text": "", "tools": [], "loaded": [], "error": None, "seconds": None, "artifacts": [], "sessionId": session_id}
    payload: dict[str, Any] = {"prompt": task["question"], "format": "json", "skills": list(arm.get("skills") or []),
                               "skillOverrides": list(arm.get("skillOverrides") or [])}
    files = call_files(task, agent["bucket"]) if task.get("files") else []
    if files:
        payload["files"] = files
    if model:
        payload["model"] = model
    started = time.monotonic()
    try:
        response = data.invoke_agent_runtime(agentRuntimeArn=agent["arn"], runtimeSessionId=session_id,
                                             payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        body = response.get("response")
        try:
            raw = body.read() if hasattr(body, "read") else (body or b"")
        finally:
            if hasattr(body, "close"):
                body.close()
        status = int(response.get("statusCode") or 200)
    except Exception as exc:  # noqa: BLE001 - a platform failure is retried by rollout(); the rest is the agent's
        out.update(error=f"{type(exc).__name__}: {str(exc)[:400]}", seconds=round(time.monotonic() - started, 1))
        return out
    finally:
        _stop_session(data, agent["arn"], session_id)
    out["seconds"] = round(time.monotonic() - started, 1)
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    try:
        got = json.loads(text)
    except ValueError:
        got = {"error": f"not JSON: {text[:300]}"}
    if not isinstance(got, Mapping):
        got = {"error": f"not a JSON object: {text[:300]}"}
    if status >= 400 or got.get("error"):
        out["error"] = f"the runtime answered ({status}): {str(got.get('error') or text)[:400]}"
        return out
    if "call" not in got:  # an image from before calls could bring their own skills: both arms would be the same agent
        raise SkillLabError(f"{agent.get('name')} ignored the call's skills (its image is from an older template): deploy a new version of it "
                            "(部署代码) and evaluate again")
    if files and not isinstance((got.get("call") or {}).get("inputs"), list):  # an image from before calls could bring files
        raise SkillLabError(f"{agent.get('name')} ignored the call's input files (its image is from an older template): deploy a new version "
                            "of it (部署代码) and evaluate again")
    given = {f["path"] for f in files}
    out["text"] = str(got.get("result") or "")
    out["tools"] = [str((c or {}).get("tool")) for c in got.get("tools") or []]
    out["loaded"] = [str(n) for n in (got.get("skillsLoaded") if got.get("skillsLoaded") is not None else got.get("skills")) or []]
    out["artifacts"] = [{**{k: a[k] for k in ARTIFACT_KEYS if k in a}, **({"input": True} if a.get("path") in given else {})}
                        for a in got.get("artifacts") or [] if isinstance(a, Mapping)]
    if files:
        out["inputs"] = [{k: i.get(k) for k in ("path", "bytes", "sha256")} for i in (got.get("call") or {}).get("inputs") or [] if isinstance(i, Mapping)]
    if got.get("artifactsTotal"):
        out["artifactsTotal"] = got["artifactsTotal"]
    out.update(seconds=got.get("seconds") or out["seconds"], turns=got.get("turns"), costUsd=got.get("costUsd"), workdir=(got.get("call") or {}).get("workdir"))
    out["skillLoaded"] = skill_name in out["loaded"]
    return out


def _grade(rt: Any, judge_model: str, task: Mapping[str, Any], answer: Mapping[str, Any]) -> dict[str, Any]:
    if answer.get("error") and not answer.get("text") and answer.get("infra"):
        graded = {"pass": False, "score": 0.0, "reason": f"not graded: the platform failed {answer.get('attempts')} times ({answer['error']})", "invalid": True}
    elif answer.get("error") and not answer.get("text"):
        graded = {"pass": False, "score": 0.0, "reason": f"the agent failed: {answer['error']}"}
    else:  # a runtime's answer carries the files its turn made: the judge reads them with it (and sees their pictures)
        graded = judge(rt, judge_model, task, answer.get("text") or "", answer.get("artifacts") if "artifacts" in answer else None)
    out = {**answer, **graded}
    if "artifacts" in out:  # the pictures' previews were the judge's: a result keeps their thumbnails
        out["artifacts"] = [without_previews(a) for a in out["artifacts"]]
    return out


def run_arm(session: Any, region: str, harness: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]], skills: list[dict[str, Any]], *,
            skill_name: str, judge_model: str, workers: int, model: str | None, data: Any, rt: Any, repeats: int = 1,
            on_done: Callable[[Mapping[str, Any], Mapping[str, Any]], None] | None = None) -> dict[str, dict[str, Any]]:
    """Every task answered ``repeats`` times with ``skills`` and graded: ``{task id: result}`` (see :func:`merge_runs`)."""
    def one(item: tuple[Mapping[str, Any], int]) -> tuple[str, dict[str, Any]]:
        task = item[0]
        result = _grade(rt, judge_model, task, rollout(session, region, data, harness, task, skills, skill_name, model))
        if on_done:
            on_done(task, result)
        return task["id"], result

    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 8))) as pool:
        pairs = list(pool.map(one, [(t, r) for t in tasks for r in range(max(1, int(repeats)))]))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for tid, result in pairs:
        grouped.setdefault(tid, []).append(result)
    return {tid: merge_runs(runs) for tid, runs in grouped.items()}


def _brief(run: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: run.get(k) for k in ("pass", "score", "skillLoaded", "reason", "seconds", "error", "invalid")}
    if "artifacts" in run:
        out["files"] = [a.get("path") for a in run.get("artifacts") or []]
    return out


def merge_runs(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One task's repeated answers as one result: the mean score, pass by a strict majority (``passShare`` the
    fraction that passed), the share that loaded the skill; the first graded answer is the one shown. A task is
    invalid only when every answer was."""
    if len(runs) == 1:
        return dict(runs[0])
    valid = [r for r in runs if not r.get("invalid")]
    if not valid:
        return {**runs[0], "runs": [_brief(r) for r in runs]}
    passes = sum(1 for r in valid if r["pass"])
    return {**valid[0], "pass": passes * 2 > len(valid), "passShare": round(passes / len(valid), 3),
            "score": round(sum(float(r["score"]) for r in valid) / len(valid), 3), "skillLoaded": any(r.get("skillLoaded") for r in valid),
            "loadedShare": round(sum(1 for r in valid if r.get("skillLoaded")) / len(valid), 3), "runs": [_brief(r) for r in runs]}


def summarize(results: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    graded = [r for r in results.values() if not r.get("invalid")]
    n = len(graded)

    def rate(values: list[float]) -> float | None:
        return round(sum(values) / n, 3) if n else None

    return {"n": n, "invalid": len(results) - n, "passRate": rate([float(r.get("passShare", 1.0 if r["pass"] else 0.0)) for r in graded]),
            "softMean": rate([float(r["score"]) for r in graded]),
            "triggerRate": rate([float(r.get("loadedShare", 1.0 if r.get("skillLoaded") else 0.0)) for r in graded]),
            "errors": sum(1 for r in graded if r.get("error"))}


def sign_test(wins: int, losses: int) -> float | None:
    """Two-sided sign test on the tasks the arms disagree on (ties dropped)."""
    n = wins + losses
    if not n:
        return None
    k = max(wins, losses)
    return round(min(1.0, 2 * sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n), 4)


#: A task's score must move this much for it to count as better or worse on the score (the judge's own wobble).
SOFT_MARGIN = 0.05


def compare(with_: Mapping[str, Mapping[str, Any]], without: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Paired by task: wins and losses on pass (sign test ``p``) and on the score (``softWins`` … ``pSoft``)."""
    both = [t for t in with_ if t in without and not with_[t].get("invalid") and not without[t].get("invalid")]
    wins = [t for t in both if with_[t]["pass"] and not without[t]["pass"]]
    losses = [t for t in both if without[t]["pass"] and not with_[t]["pass"]]
    soft_wins = [t for t in both if with_[t]["score"] > without[t]["score"] + SOFT_MARGIN]
    soft_losses = [t for t in both if without[t]["score"] > with_[t]["score"] + SOFT_MARGIN]
    a, b = summarize(with_), summarize(without)
    lift = {k: (round(a[k] - b[k], 3) if a[k] is not None and b[k] is not None else None) for k in ("passRate", "softMean")}
    return {"with": a, "without": b, "lift": lift, "wins": wins, "losses": losses, "ties": len(both) - len(wins) - len(losses),
            "p": sign_test(len(wins), len(losses)), "softWins": soft_wins, "softLosses": soft_losses,
            "pSoft": sign_test(len(soft_wins), len(soft_losses))}


def verdict(cmp: Mapping[str, Any], model: str) -> dict[str, str]:
    """What the comparison says, for the page: not loaded, helps, maybe, partial, hurts or no effect.

    Pass decides when the arms disagree on it; when they agree on every task, the scores decide."""
    a, lift, wins, losses, p = cmp["with"], cmp["lift"], len(cmp["wins"]), len(cmp["losses"]), cmp["p"]
    if a["n"] and not a["triggerRate"]:
        return {"key": "not_loaded", "text": f"模型 {model} 一次都没有加载这个技能（skills 工具没被调用过），技能没有机会起作用。先把 description 写得更贴近用户会问的话"
                                             "（2026-10-01 实测 Nova 2 Lite 会跳过泛泛描述的技能），或换一个更会用技能的模型（Claude Sonnet 5.5 / Haiku 4.5 实测都会加载）。"}
    if lift["passRate"] is None:
        return {"key": "no_data", "text": "没有可比较的结果。"}
    pts = round(lift["passRate"] * 100)
    soft, sw, sl, ps = lift["softMean"] or 0.0, len(cmp.get("softWins") or []), len(cmp.get("softLosses") or []), cmp.get("pSoft")
    if wins + losses:
        if lift["passRate"] > 0 and wins > losses:
            if p is not None and p < 0.1:
                return {"key": "helps", "text": f"技能有效：通过率提升 {pts} 个百分点（赢 {wins} 题、输 {losses} 题，符号检验 p={p}）。"}
            same_way = f"得分上也是 {sw} 题更好、{sl} 题更差（p={ps}），方向一致；" if soft > 0 and sw > sl else ""
            return {"key": "maybe", "text": f"通过率提升 {pts} 个百分点，但赢输的题太少（赢 {wins}、输 {losses}，p={p}），还不能排除是噪声。{same_way}加题再测。"}
        if losses > wins:
            return {"key": "hurts", "text": f"技能让结果变差：输 {losses} 题、赢 {wins} 题（通过率 {pts:+d} 个百分点）。先看输掉的题。"}
    if soft >= SOFT_MARGIN and sw > sl:
        return {"key": "partial", "text": f"通过率没变，但平均得分提升 {soft:.2f}（{sw} 题更好、{sl} 题更差，p={ps}）：技能在起作用，只是还没让答案全部达标。"
                                          "看每题没达标的那一条，或用训练改进它。"}
    if soft <= -SOFT_MARGIN and sl > sw:
        return {"key": "hurts", "text": f"通过率没变，但平均得分下降 {-soft:.2f}（{sl} 题更差、{sw} 题更好）：先看变差的题。"}
    return {"key": "no_effect", "text": f"看不出技能的作用（通过 赢 {wins}、输 {losses}；得分 {soft:+.2f}）。"}


def _model(body: Mapping[str, Any], key: str, default: str | None) -> str | None:
    value = str(body.get(key) or "").strip() or default
    if value is not None and not MODEL_ID.match(value):
        raise SkillLabError(f"{key} must be a Bedrock model or inference profile id")
    return value


def _workers(body: Mapping[str, Any]) -> int:
    return max(1, min(int(body.get("workers") or 4), 8))


def _repeats(body: Mapping[str, Any]) -> int:
    """How many times each task is answered (averaged): the judge and the agent both wobble from call to call."""
    return max(1, min(int(body.get("repeats") or 1), 3))


def _listed(ids: Sequence[str]) -> str:
    return f"{', '.join(ids[:5])}{' …' if len(ids) > 5 else ''}"


def _setup(console: Any, workspace: str, body: Mapping[str, Any], verb: str,
           used: Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    """What an evaluation or a training needs: the agent (a Harness, or — ``runtimeId`` — a Claude Agent SDK runtime;
    its role allowed to read the library and the task files), the skill, the set, and ``arm(uri)``: the skills of one
    arm (``uri`` the version under test, None for the arm without it). ``used(ts)``: the tasks the job answers — when
    some give the agent input files, a Harness is refused (nothing can be put in its sandbox) before anything of it
    is read or changed, and so is a runtime of a template whose calls cannot bring files."""
    name = str(body.get("skill") or "")
    entry = _entry(console, workspace, name)
    version = str(body.get("version") or entry["current"])
    if not any(v["version"] == version for v in entry["versions"]):
        raise SkillLabError(f"{name} has no version {version}")
    ts = taskset(console.store, workspace, str(body.get("tasksetId") or ""))
    tasks = list(used(ts))
    common = {"name": name, "version": version, "ts": ts, "model": _model(body, "model", None), "judgeModel": _model(body, "judgeModel", JUDGE_MODEL),
              "workers": _workers(body), "repeats": _repeats(body)}
    with_files = inputs_of(tasks)
    if body.get("runtimeId"):
        return {**common, **_runtime_setup(console, workspace, str(body["runtimeId"]), name, common["model"], tasks)}
    if with_files:
        raise SkillLabError(f"{len(with_files)} of these tasks give the agent input files ({_listed(with_files)}), and a Harness answers in a "
                            "sandbox nothing can be put into: evaluate on a Claude Agent SDK runtime (部署代码 → 从模板生成: Claude Agent SDK), "
                            "or with tasks that bring no files")
    harness = _harness(console, workspace, str(body.get("harnessId") or ""), body.get("acknowledged"), verb)
    session, ws, bucket = _where(console, workspace)
    full = client(session, "bedrock-agentcore-control", ws["region"]).get_harness(harnessId=harness["id"])["harness"]
    if full.get("status") != "READY":
        raise SkillLabError(f"{harness['name']} is {full.get('status')}, not READY")
    grant_read(session, full, bucket, boundary=boundary_of(ws))
    agent_model = ((full.get("model") or {}).get("bedrockModelConfig") or {}).get("modelId")
    base = base_skills(full.get("skills"), bucket, name)
    return {**common, "harness": harness, "session": session, "region": ws["region"], "bucket": bucket, "base": base, "agentModel": agent_model,
            "modelUsed": common["model"] or agent_model, "arm": lambda uri: base + ([{"s3": {"uri": uri}}] if uri else []),
            "target": {"targetKind": "harness", "harnessId": harness["id"], "harness": harness["name"]}}


def _runtime_setup(console: Any, workspace: str, runtime_id: str, name: str, model: str | None,
                   tasks: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """A Claude Agent SDK runtime as the agent under test: deployed from the template (a call may bring its own skills
    and — template 3 — its input files), READY, its role allowed to read the library and the task files, each file the
    tasks name still in the bucket. Its image's other skills stay in both arms, as a Harness's do."""
    agent, live = claude_runtime(console, workspace, runtime_id)
    if not live:
        raise SkillLabError(f"{agent['name']} has no successful deployment to {runtime_id} yet")
    template = int(live.get("template") or 1)
    if template < RUNTIME_TEMPLATE:
        raise SkillLabError(f"{agent['name']} was deployed from the Claude Agent SDK template before a call could bring its own skills: "
                            "deploy a new version of it (部署代码) and evaluate again")
    with_files = inputs_of(tasks)
    if with_files and template < FILES_TEMPLATE:
        raise SkillLabError(f"{agent['name']} was deployed from the Claude Agent SDK template before a call could bring its input files, and "
                            f"{len(with_files)} of these tasks give some ({_listed(with_files)}): deploy a new version of it (部署代码) and "
                            "evaluate again")
    session, ws, bucket = _where(console, workspace)
    if with_files:
        s3 = client(session, "s3", ws["region"])
        named = {str(f["asset"]).removeprefix("sha256:"): f["path"] for t in tasks for f in t.get("files") or []}
        gone = sorted(path for sha, path in named.items() if not _has_object(s3, bucket, asset_key(sha)))
        if gone:
            raise SkillLabError(f"the task files {_listed(gone)} are no longer in the console bucket: upload them again and save the task set")
    runtime = agents.get_agent(session, ws["region"], "runtime", runtime_id)
    if runtime.get("status") != "READY":
        raise SkillLabError(f"{runtime['name']} is {runtime.get('status')}, not READY")
    role = str(runtime.get("roleArn") or "").rsplit("/", 1)[-1]
    if not role:
        raise SkillLabError(f"{runtime['name']} has no execution role to grant")
    granted = grant_runtime_read(session, role, bucket, boundary=boundary_of(ws))
    others = [n for n in sorted(live.get("skills") or {}) if n != name]
    agent_model = live.get("model") or (agent.get("spec") or {}).get("model")
    target = {"kind": "runtime", "id": runtime_id, "name": runtime["name"], "arn": runtime["arn"], "role": role, "bucket": bucket, "template": template}
    return {"harness": target, "session": session, "region": ws["region"], "bucket": bucket, "base": others, "agentModel": agent_model,
            "modelUsed": model or agent_model, "arm": lambda uri: runtime_arm(others, name, uri), "granted": granted,
            "target": {"targetKind": "runtime", "runtimeId": runtime_id, "runtime": runtime["name"]}}


def _wait_for_grant(s: Mapping[str, Any], ctx: Any) -> None:
    if s.get("granted"):
        ctx.log(f"{s['harness']['name']}'s role was just allowed to read the skill library: {GRANT_WAIT:.0f} s for IAM")
        time.sleep(GRANT_WAIT)


def _eval_tasks(ts: Mapping[str, Any], body: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """The split an evaluation answers (test unless named; val when there is no test) and its tasks, up to ``limit``."""
    which = str(body.get("split") or ("all" if ts["mode"] == "single" else "test"))
    tasks = split(ts, which)
    if not tasks and which == "test":
        which, tasks = "val", split(ts, "val")
    limit = int(body.get("limit") or 0)
    tasks = tasks[:limit] if limit > 0 else tasks
    if not tasks:
        raise SkillLabError(f"the {which} split has no tasks")
    return which, tasks


def _row(t: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": t["id"], "question": t["question"], "rubric": t["rubric"], **({"files": t["files"]} if t.get("files") else {})}


def start_evaluation(console: Any, workspace: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """A job: the skill version against no skill, on one split of a task set."""
    picked: dict[str, Any] = {}

    def used(ts: Mapping[str, Any]) -> list[dict[str, Any]]:
        picked["which"], picked["tasks"] = _eval_tasks(ts, body)
        return picked["tasks"]

    s = _setup(console, workspace, body, "evaluate a skill on it (its role gets read access to the skill library)", used)
    which, tasks = picked["which"], picked["tasks"]
    arms = [a for a in (body.get("arms") or ["with", "without"]) if a in ("with", "without")] or ["with", "without"]
    params = {**s["target"], "skill": s["name"], "version": s["version"],
              "tasksetId": s["ts"]["id"], "taskset": s["ts"]["name"], "split": which, "tasks": len(tasks), "arms": arms,
              "model": s["modelUsed"], "modelOverride": s["model"], "judgeModel": s["judgeModel"], "workers": s["workers"], "repeats": s["repeats"],
              "inputFiles": len(inputs_of(tasks))}

    def work(ctx: Any) -> dict[str, Any]:
        data, rt = client(s["session"], "bedrock-agentcore", s["region"]), client(s["session"], "bedrock-runtime", s["region"])
        done = {"n": 0}
        total = len(tasks) * len(arms) * s["repeats"]
        _wait_for_grant(s, ctx)

        def tick(arm: str) -> Callable[[Mapping[str, Any], Mapping[str, Any]], None]:
            def on_done(task: Mapping[str, Any], result: Mapping[str, Any]) -> None:
                done["n"] += 1
                ctx.progress(done=done["n"], total=total)
                files = f" · files {', '.join(a['path'] for a in result['artifacts']) or 'none'}" if "artifacts" in result else ""
                ctx.log(f"{arm:7s} {task['id']}: {'pass' if result['pass'] else 'fail'} {result['score']}"
                        f"{' · skill loaded' if result.get('skillLoaded') else ''}{files}{' · ' + str(result['error'])[:80] if result.get('error') else ''}")
            return on_done

        uri = skill_uri(s["bucket"], s["name"], s["version"])
        arm_skills = {"with": s["arm"](uri), "without": s["arm"](None)}
        results = {arm: run_arm(s["session"], s["region"], s["harness"], tasks, arm_skills[arm], skill_name=s["name"], judge_model=s["judgeModel"],
                                workers=s["workers"], model=s["model"], data=data, rt=rt, repeats=s["repeats"], on_done=tick(arm)) for arm in arms}
        rows = [{**_row(t), **{arm: results[arm].get(t["id"]) for arm in arms}} for t in tasks]
        out: dict[str, Any] = {"rows": rows, "arms": {arm: summarize(results[arm]) for arm in arms}, "model": s["modelUsed"]}
        if len(arms) == 2:
            out["comparison"] = compare(results["with"], results["without"])
            out["verdict"] = verdict(out["comparison"], s["modelUsed"] or "?")
        elif "with" in arms and out["arms"]["with"]["n"] and not out["arms"]["with"]["triggerRate"]:
            out["verdict"] = verdict({"with": out["arms"]["with"], "lift": {"passRate": None, "softMean": None}, "wins": [], "losses": [], "p": None},
                                     s["modelUsed"] or "?")
        return out

    return {"job": console.jobs.start("skill-eval", workspace, params, work, label=f"评估 {s['name']} {s['version']} · {s['harness']['name']}"
                                                                              f"{'（Claude Agent SDK）' if s['harness'].get('kind') == 'runtime' else ''}")}


# -- training -----------------------------------------------------------------------------------------------------------

OPTIMIZER_SYSTEM = """You improve an Agent Skill: a SKILL.md an AI agent loads when the skill's description matches the request, then follows.
You get the current SKILL.md, tasks the agent failed with it (the question, the rubric, the agent's answer, the grader's reason, and whether the agent loaded the skill at all), and some it passed.
Rewrite SKILL.md so the agent would pass the failed tasks without breaking the passed ones. Rules:
- Change at most {budget} things (an added, removed or reworded rule counts as one).
- Keep the frontmatter and its "name: {name}" exactly; you may reword the description. When the agent did not load the skill, the description is the likely cause: say plainly what requests the skill is for.
- Write rules for the kind of request, never answers to these particular tasks. Do not copy facts from the rubrics (numbers, names, policies) that the agent should get from its own tools.
- Keep it short and concrete; keep the skill's language.
Reply in exactly this format:
<rationale>one or two sentences</rationale>
<edits>
one line per change
</edits>
<skill>
the full new SKILL.md
</skill>"""


def edit_budget(step: int, steps: int, start: int) -> int:
    """Cosine decay from ``start`` edits at the first step to 2 at the last."""
    if steps <= 1 or start <= 2:
        return max(1, start)
    return round(2 + (start - 2) * (1 + math.cos(math.pi * (step - 1) / (steps - 1))) / 2)


def _tag(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.S)
    return m.group(1) if m else None


def optimize(rt: Any, model: str, *, name: str, skill: str, failures: Sequence[Mapping[str, Any]], passes: Sequence[Mapping[str, Any]],
             budget: int) -> dict[str, Any]:
    """A rewritten SKILL.md from the failures: ``{"skill", "edits", "rationale"}`` or ``{"error"}``."""
    def show(row: Mapping[str, Any]) -> str:
        return (f"### {row['id']}\nQuestion: {row['question']}\nRubric: {row['rubric']}\nLoaded the skill: {'yes' if row.get('skillLoaded') else 'no'}\n"
                f"Answer: {str(row.get('text') or '')[:2500]}\n{files(row)}Grader: {'pass' if row.get('pass') else 'fail'} ({row.get('score')}): {row.get('reason')}")

    def files(row: Mapping[str, Any]) -> str:  # an agent that works on files (a runtime): what it was given, what it produced as the judge read it
        if "artifacts" not in row:
            return ""
        made = row.get("artifacts") or []
        given = f"Input files the task gave it: {', '.join(str(f.get('path')) for f in row.get('files') or [])}\n" if row.get("files") else ""
        return (given + f"Files it produced: {', '.join(str(a.get('path')) for a in made) or 'none'}\n"
                + "".join(f"--- {a.get('path')} ---\n{str(a.get('text') or a.get('error') or '')[:1500]}\n" for a in made[:3]))

    user = (f"## Current SKILL.md\n{skill}\n\n## Failed ({len(failures)})\n" + "\n\n".join(show(r) for r in failures)
            + (f"\n\n## Passed ({len(passes)})\n" + "\n\n".join(show(r) for r in passes[:3]) if passes else ""))
    text = converse(rt, model, OPTIMIZER_SYSTEM.replace("{budget}", str(budget)).replace("{name}", name), user, max_tokens=8000, temperature=None)
    new = (_tag(text, "skill") or "").strip()
    if not new:
        return {"error": "the optimizer's reply had no <skill> block"}
    new = re.sub(r"^```(?:markdown|md)?\s*\n(.*?)\n```$", r"\1", new, flags=re.S).strip() + "\n"
    try:
        check_skill(name, new)
    except SkillLabError as exc:
        return {"error": f"the optimizer's SKILL.md is not valid: {exc}"}
    edits = [line.strip("-• \t") for line in (_tag(text, "edits") or "").splitlines() if line.strip()]
    return {"skill": new, "edits": edits[:20], "rationale": (_tag(text, "rationale") or "").strip()[:600]}


#: A candidate must beat the current skill by this much on val (on the gate metric) to be accepted.
GATE_MARGIN = 0.05


def gate_score(summary: Mapping[str, Any], metric: str) -> float:
    hard, soft = summary.get("passRate") or 0.0, summary.get("softMean") or 0.0
    return round(hard if metric == "hard" else soft if metric == "soft" else (hard + soft) / 2, 4)


def start_training(console: Any, workspace: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """A job: the reflection loop on train, gated on val, the seed / best / no skill measured on test."""
    s = _setup(console, workspace, body, "train a skill on it (its role gets read access to the skill library)",
               lambda ts: [t for k in SPLITS for t in split(ts, k)])
    train, val, test = (split(s["ts"], k) for k in SPLITS)
    if not train or not val:
        raise SkillLabError("training needs train and val tasks (a single list needs at least 4 tasks to split)")
    epochs = max(1, min(int(body.get("epochs") or 1), 5))
    batch = max(1, min(int(body.get("batchSize") or 4), 16))
    start_budget = max(1, min(int(body.get("editBudget") or 4), 16))
    metric = str(body.get("gateMetric") or "soft")
    if metric not in GATES:
        raise SkillLabError("gateMetric: soft, hard or mixed")
    margin = max(0.0, min(float(body.get("gateMargin") if body.get("gateMargin") is not None else GATE_MARGIN), 1.0))
    optimizer = _model(body, "optimizerModel", OPTIMIZER_MODEL)
    seed_text = get_skill(console, workspace, s["name"], s["version"])["text"]
    steps = epochs * math.ceil(len(train) / batch)
    params = {**s["target"], "skill": s["name"], "version": s["version"],
              "tasksetId": s["ts"]["id"], "taskset": s["ts"]["name"], "counts": {"train": len(train), "val": len(val), "test": len(test)},
              "epochs": epochs, "batchSize": batch, "editBudget": start_budget, "gateMetric": metric, "gateMargin": margin, "steps": steps,
              "model": s["modelUsed"], "modelOverride": s["model"], "judgeModel": s["judgeModel"], "optimizerModel": optimizer, "workers": s["workers"],
              "repeats": s["repeats"]}

    def work(ctx: Any) -> dict[str, Any]:
        job_id = ctx.job["id"]
        data, rt = client(s["session"], "bedrock-agentcore", s["region"]), client(s["session"], "bedrock-runtime", s["region"])
        s3 = client(s["session"], "s3", s["region"])
        _wait_for_grant(s, ctx)

        def arm(tasks: Sequence[Mapping[str, Any]], uri: str | None, repeats: int = 1) -> dict[str, dict[str, Any]]:
            return run_arm(s["session"], s["region"], s["harness"], tasks, s["arm"](uri), skill_name=s["name"], judge_model=s["judgeModel"],
                           workers=s["workers"], model=s["model"], data=data, rt=rt, repeats=repeats)

        seed_uri = skill_uri(s["bucket"], s["name"], s["version"])
        ctx.log(f"seed {s['version']} on val ({len(val)} tasks)")
        seed_val = summarize(arm(val, seed_uri, s["repeats"]))
        current = {"step": 0, "text": seed_text, "uri": seed_uri, "score": gate_score(seed_val, metric), "val": seed_val}
        history: list[dict[str, Any]] = [{"step": 0, "kind": "seed", "valScore": current["score"], "val": seed_val, "accepted": True, "best": True}]
        ctx.progress(step=0, steps=steps, current=current["score"], best=current["score"], history=history)
        ctx.log(f"seed val {metric} score {current['score']} (trigger rate {seed_val['triggerRate']})")
        step = 0
        for epoch in range(epochs):
            order = sorted(train, key=lambda t: hashlib.sha256(f"{epoch}:{t['id']}".encode("utf-8")).hexdigest())
            for start in range(0, len(order), batch):
                step += 1
                tasks = order[start:start + batch]
                budget = edit_budget(step, steps, start_budget)
                results = arm(tasks, current["uri"])
                rows = [{**_row(t), **results[t["id"]]} for t in tasks]
                failures = [r for r in rows if not r.get("pass") and not r.get("invalid")]
                entry: dict[str, Any] = {"step": step, "epoch": epoch + 1, "batch": [t["id"] for t in tasks], "budget": budget,
                                         "train": summarize(results), "failures": [r["id"] for r in failures]}
                if not failures:
                    entry.update(kind="skip", note="every task in the batch passed")
                else:
                    proposal = optimize(rt, optimizer, name=s["name"], skill=current["text"], failures=failures,
                                        passes=[r for r in rows if r.get("pass")], budget=budget)
                    if proposal.get("error") or proposal["skill"].strip() == current["text"].strip():
                        entry.update(kind="skip", note=proposal.get("error") or "the optimizer changed nothing")
                    else:
                        key = f"skills/{s['name']}/candidates/{job_id}/s{step:03d}/SKILL.md"
                        s3.put_object(Bucket=s["bucket"], Key=key, Body=proposal["skill"].encode("utf-8"), ContentType="text/markdown")
                        uri = f"s3://{s['bucket']}/{key.rsplit('/', 1)[0]}/"
                        # paired: the current skill answers the same val tasks again now, so the two are compared under
                        # the same conditions (live 2026-10-01 the seed's val score moved 0.66-0.69 between runs)
                        cand_res, cur_res = arm(val, uri, s["repeats"]), arm(val, current["uri"], s["repeats"])
                        cand_val, cur_val = summarize(cand_res), summarize(cur_res)
                        score, against = gate_score(cand_val, metric), gate_score(cur_val, metric)
                        paired = compare(cand_res, cur_res)
                        accepted = score - against > 0 and score - against >= margin and len(paired["softWins"]) >= len(paired["softLosses"])
                        entry.update(kind="candidate", uri=uri, valScore=score, currentScore=against, delta=round(score - against, 4), val=cand_val,
                                     better=paired["softWins"], worse=paired["softLosses"], accepted=accepted, best=accepted,
                                     edits=proposal["edits"], rationale=proposal["rationale"], text=proposal["skill"])
                        if accepted:
                            current = {"step": step, "text": proposal["skill"], "uri": uri, "score": score, "val": cand_val}
                history.append(entry)
                ctx.progress(step=step, steps=steps, current=current["score"], best=current["score"], history=[
                    {k: v for k, v in h.items() if k != "text"} for h in history])
                ctx.log(f"step {step}/{steps}: {entry.get('kind')} {entry.get('valScore', '')} vs {entry.get('currentScore', '')} "
                        f"{'accepted' if entry.get('accepted') else entry.get('note', 'rejected')}")
        final = test or val
        which = "test" if test else "val"
        ctx.log(f"seed, best and no skill on {which} ({len(final)} tasks)")
        best = current  # every accepted step beat the one before it on the same val tasks, by the margin
        seed_final = arm(final, seed_uri, s["repeats"])
        best_final = seed_final if best["step"] == 0 else arm(final, best["uri"], s["repeats"])
        none_final = arm(final, None, s["repeats"])
        rows = [{**_row(t), "seed": seed_final.get(t["id"]), "best": best_final.get(t["id"]),
                 "without": none_final.get(t["id"])} for t in final]
        diff = "".join(difflib.unified_diff(seed_text.splitlines(keepends=True), best["text"].splitlines(keepends=True),
                                            fromfile=f"{s['name']} {s['version']} (seed)", tofile=f"{s['name']} best (step {best['step']})"))
        seed_sum, best_sum = summarize(seed_final), summarize(best_final)
        return {"history": history, "final": {"split": which, "seed": seed_sum, "best": best_sum, "without": summarize(none_final), "rows": rows,
                                               "bestVsSeed": compare(best_final, seed_final), "bestVsNone": compare(best_final, none_final)},
                "seed": {"version": s["version"], "valScore": history[0]["valScore"]},
                "best": {"step": best["step"], "uri": best["uri"], "valScore": best["score"], "text": best["text"]},
                "improved": best["step"] != 0 and 0 < gate_score(best_sum, metric) - gate_score(seed_sum, metric) >= margin, "diff": diff,
                "model": s["modelUsed"]}

    return {"job": console.jobs.start("skill-train", workspace, params, work, label=f"训练 {s['name']} {s['version']} · {s['harness']['name']}"
                                                                               f"{'（Claude Agent SDK）' if s['harness'].get('kind') == 'runtime' else ''}")}


def publish(console: Any, workspace: str, job_id: str, body: Mapping[str, Any], caller: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The best version of a finished training becomes the skill's current version and, if asked, an agent's: a
    Harness is switched to it (``harnessId``), a Claude Agent SDK runtime gets a new version built with it
    (``runtimeId``: a deployment, so an admin's)."""
    job = console.jobs.get(job_id)
    if job.get("kind") != "skill-train" or job.get("workspace") != workspace:
        raise SkillLabError("not a training of this workspace")
    if job.get("status") != "succeeded":
        raise SkillLabError(f"the training is {job.get('status')}")
    result = job.get("result") or {}
    if (result.get("best") or {}).get("step", 0) == 0:
        raise SkillLabError("the training kept the seed: there is nothing new to publish")
    if not result.get("improved") and body.get("force") is not True:
        raise SkillLabError("the best version did not beat the seed on the held-out split: send force: true to publish it anyway")
    runtime_id = str(body.get("runtimeId") or "").strip()
    if runtime_id:  # checked before anything is saved: a member cannot half-publish
        if caller is not None:
            Auth.may(dict(caller), admin=True)
        claude_runtime(console, workspace, runtime_id)
    if body.get("harnessId"):  # likewise: another team's Harness asks first, before the library's current version moves
        _harness(console, workspace, str(body["harnessId"]), body.get("acknowledged"), "change its skills")
    name = job["params"]["skill"]
    saved = save_version(console, workspace, name, result["best"]["text"], source=f"train:{job_id}",
                         note=f"step {result['best']['step']}, val {result['best']['valScore']} (seed {result['seed']['valScore']})")
    out: dict[str, Any] = {"saved": saved}
    if body.get("harnessId"):
        out["applied"] = apply_to_agent(console, workspace, name, {"harnessId": body["harnessId"], "version": saved["version"],
                                                                    "acknowledged": body.get("acknowledged")})
    elif runtime_id:
        out["redeploy"] = redeploy_runtime(console, workspace, name, saved["version"], runtime_id, caller)
    return out


GENERATE_SYSTEM = """You write evaluation tasks for an Agent Skill: realistic requests an agent with this skill will get, and for each a rubric.
The rubric lists the concrete, checkable things a passing answer must do for the skill's rules to have been followed, one per line. Prefer requirements the skill itself states; a requirement about facts the agent must look up is fine when the guidance says what is true.
Cover the skill's rules evenly, including the edge cases and the things it forbids. Write in the skill's language.
Reply with JSON only: {"tasks": [{"id": "short-kebab-id", "question": "...", "rubric": "..."}]}"""


def generate_tasks(console: Any, workspace: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Proposed tasks for a skill (for review; nothing is saved)."""
    skill = get_skill(console, workspace, str(body.get("skill") or ""), str(body.get("version") or "") or None)
    count = max(1, min(int(body.get("count") or 8), 30))
    guidance = str(body.get("guidance") or "")[:4000]
    model = _model(body, "model", JUDGE_MODEL)
    session, ws, _ = _where(console, workspace)
    user = f"## SKILL.md\n{skill['text']}\n\n## Guidance\n{guidance or '(none)'}\n\nWrite {count} tasks."
    got = _json(converse(client(session, "bedrock-runtime", ws["region"]), model, GENERATE_SYSTEM, user, max_tokens=8000, temperature=None))
    tasks = got.get("tasks") if isinstance(got, Mapping) else got
    return {"tasks": _items(tasks, "tasks")[:count], "model": model}


def samples() -> list[dict[str, Any]]:
    """The bundled samples, each a skill and its task set: loyalty-reply-style (a reply style, for a Harness on the
    loyalty KB), points-report-xlsx (a file the agent writes, for a Claude Agent SDK runtime with Bash) and
    points-chart-png (a chart drawn from the member file each task gives, judged from the picture)."""
    out = []
    for folder in sorted(p for p in SAMPLES.iterdir() if (p / "SKILL.md").is_file() and (p / "taskset.json").is_file()):
        body = json.loads((folder / "taskset.json").read_text(encoding="utf-8"))
        meta, _ = frontmatter((folder / "SKILL.md").read_text(encoding="utf-8"))
        out.append({"name": folder.name, "description": meta.get("description"), "taskset": body.get("name"), "about": body.get("description"),
                    "counts": {k: len(body.get(k) or []) for k in SPLITS}, "files": len(inputs_of(_task_lists(body)))})
    return sorted(out, key=lambda s: s["name"] != DEFAULT_SAMPLE)


def _sample_files(console: Any, workspace: str, folder: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    """A sample's task set with its tasks' files uploaded: ``{"path", "source"}`` (a file of the sample's folder)
    becomes ``{"path", "asset"}``."""
    sources = sorted({str(f["source"]) for t in _task_lists(body) for f in t.get("files") or [] if isinstance(f, Mapping) and f.get("source")})
    if not sources:
        return dict(body)
    root = folder.resolve()
    sha: dict[str, str] = {}
    for start in range(0, len(sources), MAX_UPLOAD_FILES):
        batch = sources[start:start + MAX_UPLOAD_FILES]
        paths = [(folder / s).resolve() for s in batch]
        if any(root not in p.parents or not p.is_file() for p in paths):
            raise SkillLabError(f"sample {folder.name}: a task file is not in its folder")
        got = upload_assets(console, workspace, [{"name": p.name, "contentBase64": base64.b64encode(p.read_bytes()).decode("ascii")} for p in paths])
        sha.update({s: a["asset"] for s, a in zip(batch, got["assets"])})

    def resolved(t: Mapping[str, Any]) -> dict[str, Any]:
        return {**t, "files": [{"path": f["path"], "asset": sha[f["source"]]} if isinstance(f, Mapping) and f.get("source") else f
                               for f in t.get("files") or []]} if t.get("files") else dict(t)

    return {**body, **{k: [resolved(t) for t in body.get(k) or []] for k in ("tasks",) + SPLITS if body.get(k)}}


def import_sample(console: Any, workspace: str, name: str | None = None) -> dict[str, Any]:
    """A bundled sample's skill and task set (``loyalty-reply-style`` unless named; see :func:`samples`), its tasks'
    input files uploaded."""
    name = name or DEFAULT_SAMPLE
    if name not in [s["name"] for s in samples()]:
        raise SkillLabError(f"no sample {name} (samples: {', '.join(s['name'] for s in samples())})")
    folder = SAMPLES / name
    saved = save_version(console, workspace, name, (folder / "SKILL.md").read_text(encoding="utf-8"), source="sample")
    body = _sample_files(console, workspace, folder, json.loads((folder / "taskset.json").read_text(encoding="utf-8")))
    existing = next((t for t in tasksets(console.store, workspace) if t["name"] == body["name"]), None)
    ts = put_taskset(console.store, workspace, {**body, "id": existing["id"]} if existing else body)
    return {"skill": saved, "taskset": {"id": ts["id"], "name": ts["name"], "counts": _counts(ts), "files": len(inputs_of(_task_lists(ts)))}}


def jobs(console: Any, workspace: str) -> list[dict[str, Any]]:
    return [j for kind in ("skill-eval", "skill-train") for j in console.jobs.list(workspace=workspace, kind=kind, limit=50)]


def register(router: Any) -> None:
    add = router.add
    base = "/workspaces/{wid}/skill-lab"
    add("GET", f"{base}/skills", lambda r: (200, {"skills": list_skills(r.console, r.workspace())}))
    add("POST", f"{base}/skills", lambda r: (201, save_version(r.console, r.workspace(), str(r.body.get("name") or ""), str(r.body.get("text") or ""),
                                                               source="edit", note=str(r.body.get("note") or ""))))
    add("POST", f"{base}/skills/import", lambda r: (201, {"imported": import_pack(r.console, r.workspace(), str(r.body["project"]))}
                                                    if r.body.get("project") else import_uri(r.console, r.workspace(), str(r.body.get("uri") or ""),
                                                                                             r.body.get("name"))))
    add("GET", f"{base}/samples", lambda r: (r.workspace(), (200, {"samples": samples()}))[1])
    add("POST", f"{base}/samples", lambda r: (201, import_sample(r.console, r.workspace(), str(r.body.get("sample") or "") or None)))
    add("GET", f"{base}/targets", lambda r: (200, {"targets": targets(r.console, r.workspace())}))
    add("GET", f"{base}/skills/{{name}}", lambda r: (200, get_skill(r.console, r.workspace(), r.params["name"], r.query.get("version") or None)))
    add("DELETE", f"{base}/skills/{{name}}", lambda r: (200, delete_skill(r.console, r.workspace(), r.params["name"])))
    add("POST", f"{base}/skills/{{name}}/current", lambda r: (200, set_current(r.console, r.workspace(), r.params["name"], str(r.body.get("version") or ""))))
    add("POST", f"{base}/skills/{{name}}/apply", lambda r: (200, apply_to_agent(r.console, r.workspace(), r.params["name"], r.body)))
    add("POST", f"{base}/skills/{{name}}/remove", lambda r: (200, remove_from_agent(r.console, r.workspace(), r.params["name"], r.body)))
    add("GET", f"{base}/assets", lambda r: (200, {"assets": list_assets(r.console, r.workspace())}))
    add("POST", f"{base}/assets", lambda r: (201, upload_assets(r.console, r.workspace(), r.body.get("files"))))
    add("DELETE", f"{base}/assets/{{asset}}", lambda r: (200, delete_asset(r.console, r.workspace(), r.params["asset"])))
    add("GET", f"{base}/tasksets", lambda r: (200, {"tasksets": tasksets(r.console.store, r.workspace())}))
    add("POST", f"{base}/tasksets", lambda r: (201, put_taskset(r.console.store, r.workspace(), r.body)))
    add("POST", f"{base}/tasksets/generate", lambda r: (200, generate_tasks(r.console, r.workspace(), r.body)))
    add("GET", f"{base}/tasksets/{{tid}}", lambda r: (200, taskset(r.console.store, r.workspace(), r.params["tid"])))
    add("DELETE", f"{base}/tasksets/{{tid}}", lambda r: (200, delete_taskset(r.console.store, r.workspace(), r.params["tid"])))
    add("POST", f"{base}/evaluations", lambda r: (202, start_evaluation(r.console, r.workspace(), r.body)))
    add("POST", f"{base}/trainings", lambda r: (202, start_training(r.console, r.workspace(), r.body)))
    add("GET", f"{base}/jobs", lambda r: (200, {"jobs": jobs(r.console, r.workspace())}))
    add("POST", f"{base}/jobs/{{jid}}/publish", lambda r: (200, publish(r.console, r.workspace(), r.params["jid"], r.body, r.caller)))
