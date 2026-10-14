"""In-gateway Kiro generation routes for Workshop Customizer.

The deterministic Scenario Pack engine remains in ``server.py``'s isolated process.  These
routes exist only for the model-backed drafting step because the gateway injects the app-scoped
``ctx.spawn`` capability here.  That capability starts this app's own restricted agent through
KiroCrew/ACP (backed by the user's logged-in Kiro CLI); this module never starts a CLI subprocess
and never calls Bedrock.

Routes (under /api/apps/workshop-customizer):
- POST /generations                     start one Kiro generation (draft | repair | regenerate)
- GET  /generations/latest/{project_id} the newest generation of a project (settled)
- GET  /generations/{generation_id}     settle/poll the Kiro result
- POST /generations/{generation_id}/apply
                                         apply the reviewed draft ({"acknowledged": true}); orphan
                                         files are deleted behind a snapshot
- POST /generations/{generation_id}/revert
                                         restore the pre-apply snapshot ({"acknowledged": true})

Generation is plain functions that the aiohttp handlers, ``tools/kiro_generate.py`` (the headless
kiro-cli runner and acceptance driver) and the tests all share:

- ``prepare_generation(data_dir, body, *, runner, settle=None) -> (record, task)`` validates the
  request, refuses a second live generation, assembles the task from ``contracts/*.md`` and writes
  the queued record (bound to the scenario sha256 and the whole-pack digest).  Modes: ``draft`` (a
  complete pack from the brief, answers and customer materials), ``repair`` (the numbered findings
  of a fresh build/validate.json plus SA instructions, inside the allowed scopes) and
  ``regenerate`` (the whole pack, or only ``scope``).  Repair and regenerate receive REPAIR_INPUT
  (current scenario, the files of the allowed scopes, locked reviewed items) and answer with a FULL
  scenario plus only new or changed files.
- ``complete_generation(record, result_text, *, data_dir) -> record`` parses Kiro's answer,
  canonicalizes it (passthrough table, app-owned fields, forced ai_draft provenance with
  fingerprint-based preservation of SA-reviewed items whose content equals the item on disk), masks
  every section outside the allowed scopes back to disk, merges unchanged files from disk, computes
  the orphan files and the diff, and stores the result.
- ``apply_generation(record, data_dir, body) -> dict`` needs ``acknowledged: true`` (and
  ``acknowledgeConfirmationResets: true`` when customer confirmations reset), re-reads the record
  from disk, refuses a draft superseded by a newer generation, re-checks the bindings, verifies the
  stored bundle at the write boundary against the project's own meta (``_verify_bundle``), takes a
  snapshot (``generation/snapshots/<id>``, the newest five kept), writes the changed files and
  scenario.yaml (YAML), deletes the orphans, and rolls everything back on failure.
- ``revert_generation(record, data_dir, body) -> dict`` restores that snapshot byte for byte while
  the project is still exactly as the apply left it.

Every project write (these functions and the process backend's) holds ``generation/.lock``
(``project_lock``: an advisory flock, retried for 10 s, then 409).

Record states: queued -> running -> ready | needs-input | failed; ready -> applied | superseded (a
newer generation of the project was prepared); applied -> reverted.  A record settled on disk is
never revived by a late writer holding a stale in-memory copy (``_update_live``).

Contracts.  ``contracts/core.md`` is the task template.  A line that is exactly
``<!-- contract:NAME -->`` is replaced by ``contracts/NAME.md`` (dropped when that optional file is
absent); ``${name}`` placeholders are then filled by ``string.Template`` (write ``$$`` for a literal
dollar sign).  Untrusted INPUT_DATA is appended after substitution, as JSON, and is never templated.

Generated content is untrusted model output.  It is bounded, path-confined, credential-scanned,
bound to the source scenario hash and pack digest, and written only after an explicit Apply.  The
normal Validate and Build routes remain the authority; applying a draft never claims validation.
"""

from __future__ import annotations

import contextlib
import copy
import errno
import fcntl
import hashlib
import importlib
import json
import math
import os
import posixpath
import re
import secrets
import shutil
import string
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

APP_NAME = "workshop-customizer"
AGENT_NAME = "workshop-customizer"
GENERATION_RE = re.compile(r"^gen-[0-9a-f]{16}$")
PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{2,40}$")
MATERIAL_ID_RE = re.compile(r"^mat-[0-9a-f]{12}$")
MAX_BRIEF_CHARS = 20_000
MAX_REQUEST_BYTES = 131_072  # route body cap; materials never travel in it
MAX_ANSWER_ENTRIES = 20
MAX_QUESTION_CHARS = 500
MAX_ANSWER_CHARS = 2_000
MAX_ANSWERS_CHARS = 8_000
MAX_INSTRUCTIONS_CHARS = 4_000
MAX_RESULT_BYTES = 2_000_000
MAX_TASK_BYTES = 600_000
MAX_FILES = 64
MAX_FILE_CHARS = 120_000
MAX_TOTAL_FILE_CHARS = 1_000_000
GENERATION_TIMEOUT_SECS = 600  # records written before per-mode timeouts existed
TIMEOUTS = {"draft": 1800, "repair": 900, "regenerate": 900}  # a live draft has taken 21 minutes
TIMEOUT_ENV = "WORKSHOP_CUSTOMIZER_GENERATION_TIMEOUT_SECS"
TIMEOUT_BOUNDS = (300, 3600)
KIRO_CLI_STALE_GRACE_SECS = 120
#: How often a spawned generation with no gateway run task to wait on is checked again (see ``register_routes``).
WATCH_INTERVAL_SECS = 10
#: A draft is written in stages, one Kiro reply each (``staged``; live 2026-10-12 the whole pack in one reply, 29 305
#: characters of it after 35 526 of thinking, hit the reply's output limit four documents into ``files``).  Stage ->
#: the stages it needs.  The structure comes first, the golden set and teaching design are written against it, and
#: the files are written to both, in parallel: ``knowledge-N`` (a batch of documents) and ``guides`` are added when
#: ``evaluation`` is done, since its teaching design is what the documents must carry.
STAGE_DEPS: dict[str, tuple[str, ...]] = {
    "structure": (), "evaluation": ("structure",), "skills": ("structure",), "prompts": ("evaluation",),
    "knowledge": ("evaluation",), "guides": ("evaluation",)}
INITIAL_STAGES = ("structure", "evaluation", "skills", "prompts")
STAGE_TIMEOUTS = {"structure": 1200, "evaluation": 1200, "knowledge": 900, "prompts": 900, "skills": 900, "guides": 900}
STAGE_RE = re.compile(r"^(structure|evaluation|skills|prompts|guides|knowledge-[1-9][0-9]?)$")
#: A stage whose reply is unusable or fails its checks is asked once more, with the problems and its last reply.
STAGE_OUTPUT_RETRIES = 1
#: A stage whose run failed (the response stream broke off, a timeout, no output, a declined spawn) runs again, on its
#: own budget: live 2026-10-12 five of six stages' first runs broke off ("dispatch failure") within half a minute.
STAGE_RUN_RETRIES = 3
KNOWLEDGE_BATCH = 4  # documents per knowledge reply
#: A stage claimed for a spawn that never recorded its id (the gateway restarted mid-start) is run again after this.
STAGE_CLAIM_GRACE_SECS = 120
#: The stages whose task carries the source materials; the others work from the structure.
MATERIAL_STAGES = frozenset({"structure", "knowledge"})
FILE_STAGES = frozenset({"knowledge", "prompts", "skills", "guides"})
MODES = ("draft", "repair", "regenerate")
#: Modes whose output is a FULL scenario plus only new or changed files (unchanged files are carried
#: over from disk); a draft must return every referenced file itself.
MERGE_MODES = frozenset({"repair", "regenerate"})
RUNNERS = ("kirocrew-spawn", "kiro-cli")
LIVE_STATUSES = ("queued", "running")
ALLOWED_FILE_PREFIXES = ("knowledge-base/docs/", "agent/", "skills/", "guides/")
#: Unreferenced files under these prefixes (plus the template root extras) are pack-source orphans:
#: apply deletes them (with a snapshot) so the project tree equals what the scenario references.
PACK_SOURCE_PREFIXES = ALLOWED_FILE_PREFIXES + ("tools/",)
TEMPLATE_ROOT_EXTRAS = ("generate_content.py",)
FIXED_JUDGE_MODEL = "us.amazon.nova-2-lite-v1:0"  # metadata for the later Workshop, never invoked here
# Project subdirectories owned by the app, never pack sources (excluded from pack_digest).  The one
# definition: server.py's pack_digest (the build sourceHash) delegates to this module.
APP_MANAGED_DIRS = frozenset({"build", "sync", "run", "jobs", "materials", "generation"})
#: Scenario sections a repair may touch (equal to validator.SCOPES; a test pins it).
SCOPES = ("agent", "facts", "knowledge", "tools", "skills", "prompts", "golden", "labs", "guides")
#: scope -> the scenario section paths it owns (the scope mask copies every other path from disk).
SCOPE_SECTIONS: dict[str, tuple[str, ...]] = {
    "agent": ("agent", "description", "language"),
    "facts": ("facts",),
    "knowledge": ("knowledge",),
    "tools": ("tools", "evaluation.retrievalToolName"),
    "skills": ("skills",),
    "prompts": ("prompts",),
    "golden": ("evaluation.goldenSet", "evaluation.l1", "evaluation.judge"),
    "labs": ("labs.observations", "labs.teaching"),
    "guides": ("labs.guide", "labs.studentGuideFile", "labs.instructorGuideFile"),
}
#: Material budgets per task kind: characters, estimated tokens, per-file characters.
MATERIAL_BUDGETS: dict[str, dict[str, Any]] = {
    "full": {"chars": 90_000, "tokens": 36_000, "perFile": 40_000, "sourceOnly": False},
    "scoped": {"chars": 60_000, "tokens": 24_000, "perFile": 30_000, "sourceOnly": False},
    "repair": {"chars": 30_000, "tokens": 12_000, "perFile": 15_000, "sourceOnly": True},
}
MATERIAL_BACKGROUND_SHARE = 0.25
MATERIALS_INDEX_SCHEMA = "workshop-customizer/materials/1"
#: REPAIR_INPUT.currentFiles caps; a larger file is listed read-only in omittedFiles.
REPAIR_FILE_CHARS = 30_000
REPAIR_FILES_TOTAL_CHARS = 150_000
TASK_SHRINK_STEPS = 5
SNAPSHOT_KEEP = 5
LOCK_TIMEOUT_SECS = 10.0
FORMAL_PROVENANCE = frozenset({"customer_confirmed", "sa_synthetic"})
CONFIRMATION_FIELDS = ("confirmedBy", "confirmedAt", "confirmationRef")
GUIDE_NARRATIVE_ID = "guide-narrative"
ORIGIN_KINDS = ("customer_material", "sa_authored", "teaching_design")
# AWS name limits (SPEC D6a): the harness is named <agentName>_<agentName> (<= 40 chars) and the
# S3 Vectors bucket <knowledgeBaseName>-vectors-xxxx (<= 63 chars).
NAMESPACE_AGENT_NAME_MAX = 19
NAMESPACE_KB_NAME_MAX = 50
# THELMA's span adapter treats any tool span whose name contains one of these as retrieval; the
# span name is <toolTargetName without dashes>___<tool>, so the target must not contain them.
RETRIEVAL_SPAN_MARKERS = ("retrieve", "knowledge", "kb_")
CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"
CONTRACT_NAMES = ("core", "provenance", "teaching", "l1", "guides", "repair")
REQUIRED_CONTRACTS = frozenset({"core", "provenance"})
_INCLUDE_RE = re.compile(r"<!-- contract:([a-z0-9_-]+) -->")
_LOCK = threading.RLock()

#: Credential and personal-identifier shapes refused on every path to and from the Kiro model (the brief,
#: the composed task, generated files). The same set as the engine's materials.SENSITIVE_PATTERNS (the
#: upload scan) and the release gate (validator.SECRET_PATTERNS); a test keeps them in step. The word
#: boundaries are ASCII-only: ``\b`` finds none between a CJK character and a digit (``身份证号110101…``).
_ASCII_START, _ASCII_END = r"(?<![0-9A-Za-z])", r"(?![0-9A-Za-z])"
_SECRET_PATTERNS = (
    re.compile(_ASCII_START + r"(?:AKIA|ASIA)[A-Z0-9]{16}" + _ASCII_END),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(_ASCII_START + r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}", re.I),
    re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{20,}"),
    re.compile(r"(?i)(?:aws_session_token|\"SessionToken\")\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40,}"),
    re.compile(_ASCII_START + r"[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]" + _ASCII_END),  # cn-national-id
    re.compile(_ASCII_START + r"(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}" + _ASCII_END),  # us-ssn
)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


class GenerationError(ValueError):
    """A safe, user-facing generation refusal."""


class GenerationConflict(GenerationError):
    """Another generation (or job) is live for the project; HTTP 409."""

    def __init__(self, message: str, record: dict[str, Any] | None = None):
        super().__init__(message)
        self.record = record


class GenerationTooLarge(GenerationError):
    """The assembled task exceeds MAX_TASK_BYTES; HTTP 413."""


class ProjectBusy(GenerationConflict):
    """Another writer holds the project's generation/.lock for longer than LOCK_TIMEOUT_SECS; HTTP 409."""


@contextlib.contextmanager
def project_lock(pdir: Path | str, *, timeout: float | None = None) -> Iterator[None]:
    """Hold ``<project>/generation/.lock`` (an advisory ``fcntl.flock``) around a project write.

    Shared by these in-gateway routes, the process backend (``server.py``) and the headless tool, so
    an apply, a revert, an SA save and a materials upload never interleave.  The lock is retried
    for ``LOCK_TIMEOUT_SECS`` (then :class:`ProjectBusy`, HTTP 409) and is reentrant per thread (the
    registry lives on the thread object, so two loaded copies of this module share it).
    """
    pdir = Path(pdir)
    key = str(pdir.resolve())
    thread = threading.current_thread()
    held: dict[str, list[int]] = thread.__dict__.setdefault("_workshop_customizer_project_locks", {})
    if key in held:
        held[key][1] += 1
        try:
            yield
        finally:
            held[key][1] -= 1
        return
    path = pdir / "generation" / ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + (LOCK_TIMEOUT_SECS if timeout is None else timeout)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise ProjectBusy("project busy: another change to this project is in progress; try again") from exc
                time.sleep(0.05)
    except BaseException:
        os.close(fd)
        raise
    held[key] = [fd, 1]
    try:
        yield
    finally:
        held.pop(key, None)
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def est_tokens(text: str) -> int:
    """Conservative token estimate: ASCII/4 plus one token per non-ASCII character."""
    ascii_count = sum(1 for ch in text if ord(ch) < 128)
    return math.ceil(ascii_count / 4) + (len(text) - ascii_count)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def _generation_path(data_dir: Path, generation_id: str) -> Path:
    if not GENERATION_RE.fullmatch(generation_id):
        raise GenerationError("invalid generation id")
    return data_dir / "generations" / f"{generation_id}.json"


def _project_dir(data_dir: Path, project_id: str) -> Path:
    if not PROJECT_RE.fullmatch(project_id):
        raise GenerationError("project id must be kebab-case")
    path = data_dir / "projects" / project_id
    if not (path / "project.json").is_file() or not (path / "scenario.yaml").is_file():
        raise GenerationError(f"project {project_id} not found")
    return path


def _read_meta(pdir: Path) -> dict[str, Any]:
    meta = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise GenerationError("project metadata is invalid")
    return meta


def _read_record(data_dir: Path, generation_id: str) -> dict[str, Any]:
    path = _generation_path(data_dir, generation_id)
    if not path.is_file():
        raise GenerationError("generation not found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GenerationError("generation record is unreadable") from exc
    if not isinstance(payload, dict):
        raise GenerationError("generation record is invalid")
    return payload


def _write_record(data_dir: Path, record: dict[str, Any]) -> None:
    record["updatedAt"] = _utc_now()
    _atomic_json(_generation_path(data_dir, str(record["id"])), record)


def _project_records(data_dir: Path, project_id: str) -> list[dict[str, Any]]:
    gen_dir = data_dir / "generations"
    records: list[dict[str, Any]] = []
    for path in sorted(gen_dir.glob("gen-*.json")) if gen_dir.is_dir() else ():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("projectId") == project_id:
            records.append(record)
    return records


def _running_job(pdir: Path) -> str | None:
    """Id of a process-backend job (one-click, sync, …) still marked running, if any."""
    jobs = pdir / "jobs"
    for path in sorted(jobs.glob("*.json")) if jobs.is_dir() else ():
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(job, dict) and job.get("status") == "running":
            return str(job.get("id") or path.stem)
    return None


def pack_digest(pdir: Path) -> str:
    """Digest of every pack source file of a project: the build sourceHash, validate.json packDigest
    and generation sourcePackDigest (``server.pack_digest`` delegates here)."""
    digest = hashlib.sha256()
    for path in sorted(pdir.rglob("*")):
        rel = path.relative_to(pdir)
        if (rel.parts[0] in APP_MANAGED_DIRS or path.name == "project.json"
                or "__pycache__" in rel.parts or path.suffix == ".pyc" or not path.is_file()):
            continue
        digest.update(rel.as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _parse_scenario_text(text: str) -> dict[str, Any]:
    """The project's current scenario (JSON or YAML); {} when it cannot be read."""
    try:
        data = json.loads(text)
    except ValueError:
        try:
            import yaml  # present in the gateway and the app venv; optional here
        except ImportError:
            return {}
        try:
            data = yaml.safe_load(text)
        except Exception:  # noqa: BLE001 - a broken scenario means "nothing to preserve"
            return {}
    return data if isinstance(data, dict) else {}


def _secret_hit(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _task_secret_sources(materials: list[dict[str, Any]], repair: dict[str, Any] | None) -> list[str]:
    """Where credential or personal-identifier shapes sit in a task's data: material ids, project files and
    the scenario (never the matched text itself)."""
    where = [f"material {m.get('id')}" for m in materials if isinstance(m.get("text"), str) and _secret_hit(m["text"])]
    if repair is not None:
        where += [rel for rel, text in sorted((repair.get("currentFiles") or {}).items()) if _secret_hit(text)]
        where += [row["path"] for row in repair.get("omittedFiles") or [] if _secret_hit(str(row.get("head") or ""))]
        if _secret_hit(json.dumps(repair.get("currentScenario"), ensure_ascii=False)):
            where.append("scenario.yaml")
        if _secret_hit(json.dumps([repair.get("findings"), repair.get("saInstructions")], ensure_ascii=False)):
            where.append("the findings or instructions")
    return where


def _safe_relative_file(raw: object) -> str:
    rel = str(raw or "").strip().replace("\\", "/")
    path = Path(rel)
    if not rel or path.is_absolute() or ".." in path.parts or rel.startswith("."):
        raise GenerationError(f"generated file path is not allowed: {rel or '<empty>'}")
    rel = posixpath.normpath(rel)
    if not rel.startswith(ALLOWED_FILE_PREFIXES):
        raise GenerationError(f"generated file must be under one of {ALLOWED_FILE_PREFIXES}: {rel}")
    return rel


def _unclosed(text: str) -> bool:
    """Whether JSON text ends inside a string or with an object or array still open (it was cut off)."""
    depth, in_string, escaped = 0, False, False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return in_string or depth > 0


def _extract_json(text: str) -> dict[str, Any]:
    """Extract the one JSON object required by the generation contract."""
    raw = text.strip()
    begin = raw.find("WORKSHOP_PACK_JSON_BEGIN")
    end = raw.rfind("WORKSHOP_PACK_JSON_END")
    if begin >= 0 and end > begin:
        raw = raw[begin + len("WORKSHOP_PACK_JSON_BEGIN") : end].strip()
    elif begin >= 0 and _unclosed(raw[begin + len("WORKSHOP_PACK_JSON_BEGIN"):]):
        # Live 2026-10-12: the reply stopped mid-JSON with no end marker (Kiro's per-reply output limit). Say so,
        # rather than report the JSON error at the cut.
        raise GenerationError(f"Kiro's reply was cut off after {len(raw)} characters, before WORKSHOP_PACK_JSON_END "
                              "(it reached Kiro's output limit, or its response stream broke off)")
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    first, last = raw.find("{"), raw.rfind("}")
    if first < 0 or last <= first:
        raise GenerationError("Kiro returned no JSON generation object")
    try:
        payload = json.loads(raw[first : last + 1])
    except json.JSONDecodeError as exc:
        raise GenerationError(f"Kiro returned invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise GenerationError("Kiro generation output must be a JSON object")
    return payload


def _referenced_owners(scenario: dict[str, Any]) -> dict[str, str]:
    """Referenced file -> the scope that owns it (knowledge, skills, prompts, guides)."""
    owners: dict[str, str] = {}

    def add(rel: Any, scope: str) -> None:
        if rel:
            owners.setdefault(posixpath.normpath(str(rel)), scope)

    knowledge = scenario.get("knowledge") if isinstance(scenario.get("knowledge"), dict) else {}
    for doc in knowledge.get("documents", []) or []:
        if isinstance(doc, dict):
            add(doc.get("file"), "knowledge")
    for skill in scenario.get("skills", []) or []:
        if isinstance(skill, dict):
            add(skill.get("file"), "skills")
    prompts = scenario.get("prompts") if isinstance(scenario.get("prompts"), dict) else {}
    for key in ("baselineFile", "optimizationCandidateFile"):
        add(prompts.get(key), "prompts")
    labs = scenario.get("labs") if isinstance(scenario.get("labs"), dict) else {}
    for key in ("studentGuideFile", "instructorGuideFile"):
        add(labs.get(key), "guides")
    return owners


def _referenced_files(scenario: dict[str, Any]) -> set[str]:
    return set(_referenced_owners(scenario))


def _project_files(pdir: Path) -> list[str]:
    """Every regular pack-side file of a project (app-managed dirs, metadata, caches and dot/temp
    files excluded), as sorted relative POSIX paths."""
    out: list[str] = []
    for path in sorted(pdir.rglob("*")):
        rel = path.relative_to(pdir)
        parts = rel.parts
        if (not path.is_file() or parts[0] in APP_MANAGED_DIRS or rel.as_posix() in ("project.json", "scenario.yaml")
                or "__pycache__" in parts or path.suffix in (".pyc", ".tmp", ".rollback")
                or any(part.startswith(".") for part in parts)):
            continue
        out.append(rel.as_posix())
    return out


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _case_clashes(rels: Any) -> list[str]:
    """Referenced paths that differ only by letter case (one file on a case-insensitive filesystem)."""
    folded: dict[str, list[str]] = {}
    for rel in rels:
        folded.setdefault(str(rel).casefold(), []).append(str(rel))
    return sorted(rel for group in folded.values() if len(group) > 1 for rel in group)


def orphan_files(pdir: Path | str | None, scenario: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(orphans, untracked) of a project against a scenario.

    Orphans are unreferenced files under a pack-source prefix (knowledge-base/docs/, agent/, skills/,
    guides/, tools/) or template root extras (generate_content.py): apply deletes them.  Untracked
    files are any other unreferenced files: reported, never deleted.

    A file whose path equals a referenced path except for letter case is never an orphan: on a
    case-insensitive filesystem (macOS APFS, the SA's default) it IS the referenced file, so deleting
    it would delete the file apply just wrote.  It is skipped when it is the same file, and reported
    as untracked otherwise (a case-sensitive filesystem).
    """
    if pdir is None:
        return [], []
    pdir = Path(pdir)
    if not pdir.is_dir():
        return [], []
    refs = _referenced_files(scenario)
    folded: dict[str, list[str]] = {}
    for ref in refs:
        folded.setdefault(ref.casefold(), []).append(ref)
    orphans: list[str] = []
    untracked: list[str] = []
    for rel in _project_files(pdir):
        if rel in refs:
            continue
        variants = folded.get(rel.casefold())
        if variants:
            if not any(_same_file(pdir / rel, pdir / ref) for ref in variants):
                untracked.append(rel)
            continue
        if rel.startswith(PACK_SOURCE_PREFIXES) or rel in TEMPLATE_ROOT_EXTRAS:
            orphans.append(rel)
        else:
            untracked.append(rel)
    return orphans, untracked


def _dump_scenario(scenario: dict[str, Any]) -> str:
    """The scenario as YAML (the SA's editor format); JSON (valid YAML) when PyYAML is absent."""
    try:
        import yaml  # present in the gateway and the app venv
    except ImportError:
        return json.dumps(scenario, indent=2, ensure_ascii=False) + "\n"
    return yaml.safe_dump(scenario, sort_keys=False, allow_unicode=True, width=100)


EDIT_LOG_KEYS = ("manualScenarioSaves", "manualFileSaves", "kiroApplies", "kiroReverts")


def bump_edit_log(meta: dict[str, Any], key: str) -> dict[str, Any]:
    """Count one edit in ``meta.editLog`` (manual SA saves vs. Kiro applies and reverts)."""
    log = meta.get("editLog") if isinstance(meta.get("editLog"), dict) else {}
    log = {name: int(log.get(name) or 0) for name in EDIT_LOG_KEYS}
    log[key] += 1
    meta["editLog"] = log
    return log


# ---------------------------------------------------------------------------
# Customer materials (stored by server.py; read here to build the task)
# ---------------------------------------------------------------------------


def read_materials(pdir: Path) -> list[dict[str, Any]]:
    """The project's material records (``materials/index.json``); [] when none were uploaded."""
    path = Path(pdir) / "materials" / "index.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = payload.get("materials") if isinstance(payload, dict) else None
    return [r for r in records if isinstance(r, dict) and MATERIAL_ID_RE.fullmatch(str(r.get("id")))] if isinstance(records, list) else []


def material_text(pdir: Path, material_id: str) -> str:
    if not MATERIAL_ID_RE.fullmatch(material_id):
        raise GenerationError(f"invalid material id {material_id!r}")
    path = Path(pdir) / "materials" / "text" / f"{material_id}.txt"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _usable(record: dict[str, Any], *, source_only: bool = False) -> bool:
    use = record.get("generationUse") or "source"
    status = (record.get("extraction") or {}).get("status") if isinstance(record.get("extraction"), dict) else None
    return use != "exclude" and (use == "source" or not source_only) and status in ("ok", "partial")


def material_budget(mode: str, *, scoped: bool = False) -> dict[str, Any]:
    if mode == "repair":
        return dict(MATERIAL_BUDGETS["repair"])
    return dict(MATERIAL_BUDGETS["scoped" if scoped else "full"])


def _excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n\n", int(limit * 0.8), limit)
    head = text[: cut if cut > 0 else limit]
    return head + f"\n[… truncated by Workshop Customizer: sent {len(head)} of {len(text)} characters]"


def allocate_materials(pdir: Path | str, budget: dict[str, Any], *, scale: float = 1.0,
                       records: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Deterministic, token-aware water-fill of material text into one task.

    Candidates are usable, non-excluded materials (source only for repair), source before
    background, then upload time, then id.  Source may use the whole budget, background at most 25%
    of it.  Within a group the smallest files are served first; every file gets at most its fair
    share of what is left and the per-file cap.  Returns the candidates in order with
    ``{id, name, use, author, mediaType, totalChars, sentChars, truncated, text}``.
    """
    pdir = Path(pdir)
    records = read_materials(pdir) if records is None else records
    candidates = [r for r in records if _usable(r, source_only=bool(budget.get("sourceOnly")))]
    candidates.sort(key=lambda r: (0 if (r.get("generationUse") or "source") == "source" else 1,
                                   str(r.get("uploadedAt") or ""), str(r.get("id"))))
    chars_left = int(budget["chars"] * scale)
    tokens_left = int(budget["tokens"] * scale)
    per_file = int(budget["perFile"] * scale)
    texts = {str(r["id"]): material_text(pdir, str(r["id"])) for r in candidates}
    sent: dict[str, str] = {}
    for group in ("source", "background"):
        members = [r for r in candidates if (r.get("generationUse") or "source") == group and texts[str(r["id"])]]
        cap = chars_left if group == "source" else min(chars_left, int(budget["chars"] * scale * MATERIAL_BACKGROUND_SHARE))
        members.sort(key=lambda r: (min(len(texts[str(r["id"])]), per_file), str(r["id"])))
        remaining = len(members)
        group_left = cap
        for record in members:
            mid = str(record["id"])
            full = texts[mid]
            fair = group_left // remaining if remaining else 0
            give = min(len(full), per_file, fair)
            text = _excerpt(full, give) if give > 0 else ""
            while text and est_tokens(text) > tokens_left:
                give = int(give * 0.8)
                text = _excerpt(full, give) if give > 0 else ""
            remaining -= 1
            if text:
                sent[mid] = text
                group_left -= len(text)
                chars_left -= len(text)
                tokens_left -= est_tokens(text)
    out = []
    for record in candidates:
        mid = str(record["id"])
        full = texts[mid]
        text = sent.get(mid, "")
        body = text.split("\n[… truncated by Workshop Customizer", 1)[0]
        out.append({
            "id": mid,
            "name": str(record.get("name") or mid),
            "use": str(record.get("generationUse") or "source"),
            "author": str(record.get("author") or "customer"),
            "mediaType": str(record.get("mediaType") or ""),
            "totalChars": len(full),
            "sentChars": len(body),
            "truncated": len(body) < len(full),
            "text": text,
        })
    return out


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------


def timeout_for(mode: str) -> int:
    """Per-mode generation timeout; the env override applies to every mode, clamped to 300..3600."""
    return _env_timeout(TIMEOUTS.get(mode, TIMEOUTS["draft"]))


def stage_timeout(name: str) -> int:
    """A staged draft's per-stage timeout (one Kiro reply); the same env override applies."""
    return _env_timeout(STAGE_TIMEOUTS[_stage_base(name)])


def _env_timeout(default: int) -> int:
    raw = os.environ.get(TIMEOUT_ENV, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    low, high = TIMEOUT_BOUNDS
    return max(low, min(high, value))


def _record_timeout(record: dict[str, Any]) -> int:
    try:
        value = int(record.get("timeoutSecs") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else GENERATION_TIMEOUT_SECS


# ---------------------------------------------------------------------------
# Language and namespace derivation
# ---------------------------------------------------------------------------


def _language_signal(text: str) -> tuple[int, int]:
    return len(_CJK_RE.findall(text)), len(_LATIN_RE.findall(text))


def detect_language(text: str) -> str:
    """'zh-CN' when the text is mostly Chinese, else 'en' (one CJK character ~ one 5-letter word)."""
    cjk, latin = _language_signal(text)
    return "zh-CN" if cjk and cjk * 5 >= latin else "en"


def _normalise_language(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace("_", "-")
    if key.startswith("zh") or key in ("chinese", "cn", "中文"):
        return "zh-CN"
    if key.startswith("en") or key == "english":
        return "en"
    return None


def _content_sample(raw: dict[str, Any]) -> str:
    parts: list[Any] = [raw.get("description")]
    agent = raw.get("agent") if isinstance(raw.get("agent"), dict) else {}
    parts += [agent.get("audience"), agent.get("purpose")]
    parts += list(agent.get("scope") or []) if isinstance(agent.get("scope"), list) else []
    for item in raw.get("facts", []) or []:
        if isinstance(item, dict):
            parts.append(item.get("statement"))
    evaluation = raw.get("evaluation") if isinstance(raw.get("evaluation"), dict) else {}
    for item in evaluation.get("goldenSet", []) or []:
        if isinstance(item, dict):
            parts += [item.get("label"), item.get("query")]
    return "\n".join(str(p) for p in parts if isinstance(p, str))


def _short_hash(value: str, length: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _has_span_marker(target: str) -> bool:
    span_prefix = target.replace("-", "") + "___"
    return any(marker in span_prefix for marker in RETRIEVAL_SPAN_MARKERS)


def _strip_span_markers(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        for marker in ("retrieve", "knowledge"):
            text = text.replace(marker, "")
    return text


#: Key prefixes of the workshop bucket that are not a knowledge base (the engine's
#: validator.RESERVED_BUCKET_PREFIXES; a test keeps them in step). 01-create-kb prunes everything
#: under kbPrefix, so a project named "customizer-releases" must not get that prefix.
RESERVED_KB_PREFIXES = ("skills/", "customizer-releases/", "multimodal/")
#: Entries of the Workshop instance's ~/workshop an agentName could name (the engine's
#: validator.RESERVED_AGENT_NAMES; a test keeps them in step): 04-deploy removes ~/workshop/<agentName>,
#: so a project named "releases" must not get that agentName.
RESERVED_AGENT_NAMES = ("current", "previous", "releases", "run", "skills")


def derive_namespace(project_id: str) -> dict[str, str]:
    """App-owned AWS resource names for a project (never taken from the model).

    Short ids keep their readable names; long ids are truncated with a short hash so the agent
    name stays <= 19 chars and the knowledge-base name <= 50 chars (SPEC D6a). A kbPrefix that
    would be a reserved bucket prefix (:data:`RESERVED_KB_PREFIXES`) gets a ``-kb`` suffix, an
    agentName that would name an entry of the Workshop root (:data:`RESERVED_AGENT_NAMES`) an
    ``agent`` suffix.
    """
    pid = project_id.lower()
    compact = re.sub(r"[^a-z0-9]", "", pid) or "scenario"
    if len(compact) > NAMESPACE_AGENT_NAME_MAX:
        compact = compact[: NAMESPACE_AGENT_NAME_MAX - 5] + _short_hash(pid, 5)
    elif len(compact) < 3:
        compact = compact + _short_hash(pid, 4)
    if compact in RESERVED_AGENT_NAMES:
        compact += "agent"
    dashed = re.sub(r"[^a-z0-9-]", "-", pid).strip("-")[:36] or "scenario"

    target = (dashed + "-tools")[:40].rstrip("-")
    if _has_span_marker(target):
        base = re.sub(r"^[^a-z]+", "", _strip_span_markers(dashed.replace("-", "")))[:28] or "agent"
        target = f"{base}-{_short_hash(pid, 4)}-tools"
        if _has_span_marker(target):
            target = f"t{_short_hash(pid, 8)}-tools"

    knowledge_base = (dashed + "-knowledge-base")[:60].rstrip("-")
    if len(knowledge_base) > NAMESPACE_KB_NAME_MAX:
        head = dashed[: NAMESPACE_KB_NAME_MAX - 20].rstrip("-")
        knowledge_base = f"{head}-{_short_hash(pid, 4)}-knowledge-base"
    kb_prefix = dashed[:40].rstrip("-") + "/"
    if kb_prefix in RESERVED_KB_PREFIXES:
        kb_prefix = kb_prefix[:-1] + "-kb/"
    return {
        "agentName": compact,
        "toolTargetName": target,
        "gatewayName": (dashed + "-gateway")[:40].rstrip("-"),
        "knowledgeBaseName": knowledge_base,
        "kbPrefix": kb_prefix,
        "ssmParameterPrefix": "/app/" + dashed[:40].rstrip("-"),
        "lambdaFunctionName": (dashed + "-tools-handler")[:60].rstrip("-"),
    }


#: The scenario schema's namespace patterns (engine/.../scenario.schema.json), for the app-owned check.
NAMESPACE_PATTERNS = {
    "agentName": re.compile(r"^[a-z][a-z0-9]{2,18}$"),
    "toolTargetName": re.compile(r"^[a-z][a-z0-9-]{2,40}$"),
    "gatewayName": re.compile(r"^[a-z][a-z0-9-]{2,40}$"),
    "knowledgeBaseName": re.compile(r"^[a-z][a-z0-9-]{2,49}$"),
    "kbPrefix": re.compile(r"^[a-z][a-z0-9-]{0,40}/$"),
    "ssmParameterPrefix": re.compile(r"^/app/[a-z][a-z0-9-]{1,40}$"),
    "lambdaFunctionName": re.compile(r"^[a-z][a-z0-9-]{2,60}$"),
}


def namespace_ok(namespace: Any) -> bool:
    """A complete namespace within the schema patterns and the SPEC D6a limits (no THELMA span marker)."""
    return (isinstance(namespace, dict) and set(namespace) == set(NAMESPACE_PATTERNS)
            and all(isinstance(namespace[key], str) and pattern.fullmatch(namespace[key])
                    for key, pattern in NAMESPACE_PATTERNS.items())
            and not _has_span_marker(namespace["toolTargetName"])
            and namespace["kbPrefix"] not in RESERVED_KB_PREFIXES
            and namespace["agentName"] not in RESERVED_AGENT_NAMES)


def app_namespace(project_id: str, *, current: dict[str, Any] | None = None, mode: str = "draft") -> dict[str, str]:
    """The namespace a generation result must carry (never the model's).

    A repair or regenerate keeps the project's current namespace while it is valid: the pack's AWS
    resources may already be deployed under those names (a template project keeps its template
    names), and renaming them would orphan the deployed ones.  A draft, or an invalid current
    namespace, takes :func:`derive_namespace`.
    """
    namespace = current.get("namespace") if isinstance(current, dict) else None
    if mode in MERGE_MODES and namespace_ok(namespace):
        return {key: str(namespace[key]) for key in NAMESPACE_PATTERNS}
    return derive_namespace(project_id)


_NAME_TOKEN_RE = re.compile(r"[a-z][a-z0-9-]{2,}")


def _rename_model_targets(files: dict[str, Any], scenario: dict[str, Any], model_namespace: Any,
                          canon: "_Canon") -> dict[str, Any]:
    """Prompts and skills name the Gateway target and the gateway as the APP namespace does.

    The app overwrites the model's namespace, so a prompt written for the model's own names ("Use
    it-tools to ...") would point the agent at a target that does not exist. Every whole-word use of the
    model's toolTargetName / gatewayName in a returned prompt or skill file becomes the app's name.
    """
    app_ns = scenario.get("namespace") if isinstance(scenario.get("namespace"), dict) else {}
    model_ns = model_namespace if isinstance(model_namespace, dict) else {}
    renames = []
    for key in ("toolTargetName", "gatewayName"):
        old, new = model_ns.get(key), app_ns.get(key)
        if isinstance(old, str) and isinstance(new, str) and _NAME_TOKEN_RE.fullmatch(old) and old != new:
            renames.append((re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(old)}(?![A-Za-z0-9_-])"), new, old))
    if not renames:
        return files
    owners = _referenced_owners(scenario)
    out = dict(files)
    for rel, text in files.items():
        if not isinstance(text, str) or owners.get(posixpath.normpath(str(rel))) not in ("prompts", "skills"):
            continue
        renamed = text
        for pattern, new, _old in renames:
            renamed = pattern.sub(new, renamed)
        if renamed != text:
            out[rel] = renamed
            canon.warnings.append(f"{rel}: the model's names ({', '.join(o for _p, _n, o in renames)}) were replaced "
                                  "with the app-assigned namespace")
    return out


# ---------------------------------------------------------------------------
# Canonicalizer passthrough table
# ---------------------------------------------------------------------------


class _Canon:
    """Per-canonicalization context: warnings for the SA, ids of materials actually sent.

    A plain class (not a dataclass) because tests and tools load this file by path without
    registering it in sys.modules.
    """

    def __init__(self, *, material_ids: frozenset[str] = frozenset(), first_role: str = "user"):
        self.warnings: list[str] = []
        self.material_ids = frozenset(material_ids)
        self.first_role = first_role


def _scalar_text(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return str(value).strip()


def _string_list(value: Any) -> list[str] | None:
    """A list of non-empty strings; a bare string becomes a one-item list; None when not a list."""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list):
        return None
    return [text for text in (_scalar_text(v) for v in value) if text]


def _pt_origin(value: Any, canon: _Canon, where: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        canon.warnings.append(f"{where}: dropped (not an object)")
        return None
    kind = re.sub(r"[\s-]+", "_", _scalar_text(value.get("kind")).lower())
    if kind not in ORIGIN_KINDS:
        canon.warnings.append(f"{where}: dropped unknown kind {kind or '<empty>'!r}")
        return None
    cited = list(dict.fromkeys(_string_list(value.get("materials")) or []))
    kept = [mid for mid in cited if MATERIAL_ID_RE.fullmatch(mid) and mid in canon.material_ids]
    if len(kept) != len(cited):
        canon.warnings.append(f"{where}: dropped material ids that were not sent to Kiro")
    out: dict[str, Any] = {"kind": kind}
    if kind == "customer_material" and not kept:
        out["kind"] = "sa_authored"
        canon.warnings.append(f"{where}: customer_material cites no sent material; recorded as sa_authored")
    if kept:
        out["materials"] = kept[:20]
    note = _scalar_text(value.get("note"))[:500]
    if note:
        out["note"] = note
    return out


def _pt_any_of(value: Any, canon: _Canon, where: str) -> list[list[str]] | None:
    if not isinstance(value, list):
        return None
    groups = [members for members in (_string_list(group) for group in value) if members]
    return groups or None


def _pt_l1(value: Any, canon: _Canon, where: str) -> dict[str, list[str]] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, list[str]] = {}
    for key in ("refusalMarkers", "escalationMarkers", "escalationTools"):
        items = _string_list(value.get(key)) if key in value else None
        if items is not None:
            out[key] = items
    return out or None


def _pt_judge(value: Any, canon: _Canon, where: str) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    lines = _scalar_text(value.get("userLabel")).splitlines()
    label = lines[0].strip()[:40] if lines else ""
    return {"userLabel": label} if label else None


TEACHING_KIND_ALIASES = {
    "promptfixable": "prompt_fixable",
    "retrievalgap": "retrieval_gap",
    "noisegrounding": "noise_grounding",
    "tooluse": "tool_use",
    "refusal": "refusal",
    "escalation": "escalation",
}


def _teaching_kind(value: Any) -> str | None:
    key = re.sub(r"[\s-]+", "_", _scalar_text(value).lower())
    if not key:
        return None
    return TEACHING_KIND_ALIASES.get(key.replace("_", ""), key)


def _dict_items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _pt_teaching(value: Any, canon: _Canon, where: str) -> dict[str, Any] | None:
    """C1 normalization: keep the declared skeleton, coerce shapes, drop unknown keys, invent nothing."""
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    first = value.get("firstConversation")
    if isinstance(first, dict):
        row: dict[str, Any] = {}
        for key in ("query", "actorId", "unknownContext", "label", "teachingPoint", "mustNotMention"):
            if key in ("unknownContext", "mustNotMention"):
                items = _string_list(first.get(key))
                if items:
                    row[key] = items
            elif _scalar_text(first.get(key)):
                row[key] = _scalar_text(first.get(key))
        if row:
            row.setdefault("actorId", f"{canon.first_role}-first")
            out["firstConversation"] = row
    defects = []
    for item in _dict_items(value.get("baselineDefects")):
        row = {k: _scalar_text(item.get(k)) for k in ("id", "description", "candidateFix", "baselineMarker") if _scalar_text(item.get(k))}
        if row:
            defects.append(row)
    if defects:
        out["baselineDefects"] = defects
    phenomena = []
    for item in _dict_items(value.get("phenomena")):
        row = {}
        for key in ("id", "kind", "caseIds", "defectIds", "documentIds", "mechanism", "baitTerms", "absentTerms", "design", "teachingPoint"):
            if key == "kind":
                kind = _teaching_kind(item.get(key))
                if kind:
                    row[key] = kind
            elif key in ("caseIds", "defectIds", "documentIds", "baitTerms", "absentTerms"):
                items = _string_list(item.get(key)) or []
                short = [t for t in items if len(t.strip()) < 2] if key in ("baitTerms", "absentTerms") else []
                if short and len(short) < len(items):  # live 2026-10-12: '猫' broke the schema (2+ characters)
                    items = [t for t in items if len(t.strip()) >= 2]
                    canon.warnings.append(f"{where}.phenomena[{_scalar_text(item.get('id')) or '?'}].{key}: dropped the one-character "
                                          f"term {', '.join(map(repr, short))} (the schema needs 2+ characters; the other terms stay)")
                if items:
                    row[key] = items
            elif key in ("mechanism", "design"):
                if _scalar_text(item.get(key)):
                    row[key] = _scalar_text(item.get(key)).lower()
            elif _scalar_text(item.get(key)):
                row[key] = _scalar_text(item.get(key))
        if row:
            phenomena.append(row)
    if phenomena:
        out["phenomena"] = phenomena
    if _scalar_text(value.get("stabilityCaseId")):
        out["stabilityCaseId"] = _scalar_text(value.get("stabilityCaseId"))
    return out or None


GUIDE_STEP_IDS = frozenset({
    "prerequisites", "setup", "infra", "knowledge-base", "gateway", "skills", "agent", "memory",
    "conversation", "eval-env", "conversation-rerun", "evaluators", "baseline", "optimize",
    "cost-latency", "models", "judge-stability", "cleanup",
})
# (field, shape, limit) in schema order; limits are the schema maxima.
GUIDE_FIELDS = (
    ("tagline", "text", 300),
    ("scenarioIntro", "text", 2000),
    ("memoryLesson", "text", 600),
    ("retrievalContrast", "text", 1200),
    ("stepNotes", "notes", 1200),
    ("facilitatorNotes", "notes", 2000),
    ("designRationale", "text", 3000),
    ("experiments", "experiments", 6),
    ("attribution", "text", 2000),
)


def _guide_text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _pt_guide(value: Any, canon: _Canon, where: str) -> dict[str, Any] | None:
    """C2: the narrative is prose the SA reviews; Kiro can never self-review it (always ai_draft)."""
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {"id": GUIDE_NARRATIVE_ID, "provenance": "ai_draft"}
    for key, shape, limit in GUIDE_FIELDS:
        raw = value.get(key)
        if shape == "text":
            text = _guide_text(raw, limit)
            if text:
                out[key] = text
        elif shape == "notes" and isinstance(raw, dict):
            notes = {sid: _guide_text(v, limit) for sid, v in raw.items() if sid in GUIDE_STEP_IDS and _guide_text(v, limit)}
            if set(raw) - GUIDE_STEP_IDS:
                canon.warnings.append(f"{where}.{key}: dropped notes for unknown guide steps")
            if notes:
                out[key] = notes
        elif shape == "experiments" and isinstance(raw, list):
            experiments = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                audience = _scalar_text(item.get("audience")).lower()
                title, body = _guide_text(item.get("title"), 120), _guide_text(item.get("body"), 1500)
                if audience in ("student", "instructor") and title and body:
                    experiments.append({"audience": audience, "title": title, "body": body})
            if experiments:
                out[key] = experiments[:limit]
    return out if len(out) > 2 else None


# Declarations the canonicalizer keeps instead of dropping: (container, key, normaliser).  A
# normaliser returns the cleaned value, or None to drop it.  Later phases add rows here, and add the
# field to FINGERPRINT_FIELDS when it is reviewable content.
PASSTHROUGH: tuple[tuple[str, str, Callable[[Any, _Canon, str], Any]], ...] = (
    ("labs", "teaching", _pt_teaching),
    ("labs", "guide", _pt_guide),
    ("evaluation", "l1", _pt_l1),
    ("evaluation", "judge", _pt_judge),
    ("expected", "mustMentionAnyOf", _pt_any_of),
    ("fact", "origin", _pt_origin),
    ("tool", "origin", _pt_origin),
    ("document", "origin", _pt_origin),
    ("golden", "origin", _pt_origin),
)


def _passthrough(container: str, source: Any, target: dict[str, Any], canon: _Canon, where: str) -> None:
    if not isinstance(source, dict):
        return
    for name, key, normalise in PASSTHROUGH:
        if name == container and key in source:
            value = normalise(source[key], canon, f"{where}.{key}")
            if value is not None:
                target[key] = value


def _force_query_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """SPEC D6b: the retrieval handler and THELMA read only args['query'] (a required string)."""
    out = dict(schema)
    out["type"] = "object"
    props = out.get("properties") if isinstance(out.get("properties"), dict) else {}
    old = props.get("query") if isinstance(props.get("query"), dict) else {}
    query: dict[str, Any] = {"type": "string"}
    if isinstance(old.get("description"), str) and old["description"].strip():
        query["description"] = old["description"]
    fixed = {} if "query" in props else {"query": query}
    for name, spec in props.items():
        fixed[name] = query if name == "query" else spec
    out["properties"] = fixed
    out["required"] = ["query"]
    return out


def _has_query_property(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    query = (schema.get("properties") or {}).get("query") if isinstance(schema.get("properties"), dict) else None
    return isinstance(query, dict) and query.get("type") == "string" and "query" in (schema.get("required") or [])


RETRIEVAL_KIND_ALIASES = frozenset({
    "retrieval", "retriever", "retrieve", "rag", "kb", "kbsearch", "knowledge", "knowledgebase",
    "knowledgesearch", "search", "vectorsearch", "semanticsearch", "documentsearch",
})


def _retrieval_remap_target(raw: dict[str, Any]) -> str | None:
    """The tool Kiro meant as the knowledge-base retrieval tool when none has kind=retrieval.

    In order: the tool ``evaluation.retrievalToolName`` names; else the one tool whose kind spells
    retrieval differently (``rag``, ``knowledge_base``, ``search`` ...); else the one tool whose name
    carries a THELMA retrieval marker (retrieve / knowledge / kb_).  A tool with mock fixture cases
    is a real mock (``search_orders`` with kind ``search``) and is never a candidate: remapping it
    would silently drop its fixtures.  None means the standard retrieval shell is added instead.
    """
    tools = [t for t in raw.get("tools", []) or [] if isinstance(t, dict) and t.get("name")]
    if any(t.get("kind") == "retrieval" for t in tools):
        return None

    def has_cases(tool: dict[str, Any]) -> bool:
        fixtures = tool.get("fixtures") if isinstance(tool.get("fixtures"), dict) else {}
        return bool(fixtures.get("cases"))

    candidates = [t for t in tools if not has_cases(t)]
    evaluation = raw.get("evaluation") if isinstance(raw.get("evaluation"), dict) else {}
    named = str(evaluation.get("retrievalToolName") or "")
    if named and any(str(t["name"]) == named for t in candidates):
        return named
    by_kind = [str(t["name"]) for t in candidates
               if re.sub(r"[^a-z]", "", str(t.get("kind") or "").lower()) in RETRIEVAL_KIND_ALIASES]
    if len(by_kind) == 1:
        return by_kind[0]
    by_name = [str(t["name"]) for t in candidates if any(marker in str(t["name"]).lower() for marker in RETRIEVAL_SPAN_MARKERS)]
    return by_name[0] if len(by_name) == 1 else None


# Recurring first-pass slips of the 2026-09-28 acceptance loops, and why only the document-basis one is folded
# here: every other one has more than one right fix, so it stays a finding Kiro repairs (the contract's
# SELF-CHECK asks Kiro to avoid them):
# - golden.holdout_ratio / golden.category_floor: which case moves or is added is a teaching-design choice
#   (phenomena, probes and holdout isolation depend on it);
# - teaching.defect_unexercised: listing the defect under a phenomenon would claim a contrast nobody designed,
#   dropping it loses a prompt rule — and either would make the check vacuous;
# - xref "phenomenon uses holdout case" / "unknown golden case": flip the set, pick another case or fix a typo;
# - teaching.absent_term_in_kb / gap_question_in_kb / bait_not_in_query: rewriting documents or terms is content.
#: Characters a path or id continues with; any other character ends a document name inside a fact ``source``
#: (``knowledge-base/docs/x.md (Section)``, ``x.md（章节）``, ``x.md#codes``).
_PATH_CHARS = frozenset(string.ascii_letters + string.digits + "._-/")


def _names_document(source: str, name: str) -> bool:
    """Whether ``source`` names ``name`` (a document id, file or file name) as a whole path: it starts the
    source or follows a ``/`` or a non-path character, and the source ends or a non-path character follows."""
    start = source.find(name)
    while start >= 0:
        before = source[start - 1] if start else " "
        end = start + len(name)
        after = source[end] if end < len(source) else " "
        if (before == "/" or before not in _PATH_CHARS) and after not in _PATH_CHARS:
            return True
        start = source.find(name, start + 1)
    return False


def _document_facts(facts: list[dict[str, Any]], documents: list[dict[str, Any]]) -> dict[str, list[str]]:
    """{knowledge document id, file or file name: ids of the facts whose ``source`` names that document}.

    Empty unless every fact carries a string ``source``: only then is it known which facts a document
    states (the live Kiro drafts of 2026-09-28 wrote no source at all, and a fact without one may come from
    any document).  A name that belongs to more than one document (a duplicate id, file or file name) is
    no key, since it is not one document; a source that names it still counts for each of them.
    """
    if not facts or not all(isinstance(f.get("source"), str) and f["source"].strip() for f in facts):
        return {}
    names = [{_scalar_text(d.get("id")), _scalar_text(d.get("file")), posixpath.basename(_scalar_text(d.get("file")))} - {""}
             for d in documents]
    owners: dict[str, list[int]] = {}
    for index, doc_names in enumerate(names):
        for name in doc_names:
            owners.setdefault(name, []).append(index)
    stated = [[str(f["id"]) for f in facts if f.get("id") and any(_names_document(f["source"], n) for n in doc_names)]
              for doc_names in names]
    return {name: stated[docs[0]] for name, docs in owners.items() if len(docs) == 1}


def _normalise_basis(basis: list[str], *, where: str, fact_ids: frozenset[str], document_facts: dict[str, list[str]],
                     canon: _Canon) -> list[str]:
    """Fold a golden ``basis`` entry that names a knowledge document (by id, file or file name) instead of a fact.

    Live drafts cite the document a case rests on next to its facts (2026-09-27: ``["fact-error-e217",
    "kb-error-code-handbook"]``) and the ``xref`` gate blocks the pack for a slip with one right answer.
    Only the unambiguous shapes fold, each with a warning for the SA, and only when every fact carries a
    ``source`` (``_document_facts``; otherwise which facts a document states is unknown and nothing folds):

    * exactly one fact's ``source`` names the document: the entry becomes that fact (and is dropped when
      the basis already cites it);
    * no fact's ``source`` names the document and the basis cites a fact: the entry is dropped (it names
      no fact, and every fact the case cites stays).

    Everything else stays for Kiro and the ``xref`` finding: a fact without a source, several facts come
    from the document (cited or not), the basis cites no fact at all, or the id is unknown (a mistyped
    fact id).
    Which fact justifies a case is then a judgement, and the anchor rule (every basis fact confirmed)
    must never lose a fact the case really rests on.  Ids are unique across item types (an ``xref``
    rule), so an entry that equals a document id or file is never also a fact.
    """
    named = [entry for entry in basis if entry not in fact_ids and entry in document_facts]
    if not named:
        return basis
    unique = {entry: document_facts[entry][0] for entry in named if len(document_facts[entry]) == 1}
    cites_fact = bool(unique) or any(entry in fact_ids for entry in basis)
    out: list[str] = []
    for entry in basis:
        if entry in unique:
            fact = unique[entry]
            if fact in basis or fact in out:
                canon.warnings.append(f"{where}: dropped knowledge document {entry!r}; the case already cites its fact {fact!r}")
            else:
                out.append(fact)
                canon.warnings.append(f"{where}: knowledge document {entry!r} replaced by fact {fact!r}, the only fact "
                                      "whose source is that document")
        elif entry in named and not document_facts[entry] and cites_fact:
            canon.warnings.append(f"{where}: dropped knowledge document {entry!r}; a basis lists fact ids, and no fact "
                                  "names that document as its source")
        else:
            out.append(entry)
    return out


def _canonicalize_scenario(raw: dict[str, Any], *, project_id: str, pack_kind: str, canon: _Canon | None = None,
                           content_text: str = "") -> dict[str, Any]:
    """Fold common model spellings onto schema v1 without inventing business truth.

    The adapter derives resource names, renames structural keys, adds the standard retrieval-tool
    shell and folds a golden basis entry that names a knowledge document onto facts when that is
    unambiguous (``_normalise_basis``).  It never changes a fact statement, threshold, permission or
    expected answer.  Every item comes out ai_draft without confirmation fields: provenance is never
    taken from the model (``_preserve_provenance`` restores SA-reviewed items whose content is
    unchanged).  Model-supplied ``customer`` and ``governance`` are dropped; ``_apply_app_owned`` sets
    the app's values.
    ``content_text`` (the generated knowledge documents) informs language detection.
    """
    canon = canon if canon is not None else _Canon()
    namespace = derive_namespace(project_id)

    raw_agent = raw.get("agent") if isinstance(raw.get("agent"), dict) else {}
    roles: list[dict[str, Any]] = []
    for index, item in enumerate(raw_agent.get("roles", []) or [], start=1):
        if not isinstance(item, dict):
            continue
        role_id = str(item.get("id") or item.get("role") or f"role-{index}").lower().replace("_", "-")
        role_id = re.sub(r"[^a-z0-9-]", "-", role_id).strip("-") or f"role-{index}"
        if not role_id[0].isalpha():
            role_id = "role-" + role_id
        roles.append({
            "id": role_id[:60],
            "name": str(item.get("name") or item.get("title") or role_id).strip() or role_id,
            "description": str(item.get("description") or f"Role {role_id} in this draft.").strip(),
            "permissions": [str(v) for v in (item.get("permissions") or []) if str(v).strip()],
        })
    if not roles:
        roles = [{"id": "user", "name": "User", "description": "Primary user of this draft.", "permissions": []}]
    role_ids = {r["id"] for r in roles}
    canon.first_role = roles[0]["id"]

    facts: list[dict[str, Any]] = []
    for item in raw.get("facts", []) or []:
        if not isinstance(item, dict):
            continue
        fact = {k: item[k] for k in ("id", "statement", "criticality", "provenance", "source", "tags") if k in item}
        fact.setdefault("criticality", "blocking")
        fact["provenance"] = "ai_draft"
        _passthrough("fact", item, fact, canon, f"facts[{fact.get('id')}]")
        facts.append(fact)
    fact_ids = [str(f.get("id")) for f in facts if f.get("id")]

    raw_knowledge = raw.get("knowledge") if isinstance(raw.get("knowledge"), dict) else {}
    documents: list[dict[str, Any]] = []
    for item in raw_knowledge.get("documents", []) or []:
        if not isinstance(item, dict):
            continue
        doc = {k: item[k] for k in ("id", "title", "file", "provenance", "noise") if k in item}
        doc["provenance"] = "ai_draft"
        _passthrough("document", item, doc, canon, f"knowledge.documents[{doc.get('id')}]")
        documents.append(doc)
    old_noise = raw_knowledge.get("noisePlan") if isinstance(raw_knowledge.get("noisePlan"), dict) else {}
    rationale = str(old_noise.get("rationale") or old_noise.get("description") or "Teaching noise lets retrieval metrics expose dirty or distracting passages.")
    knowledge = {"documents": documents, "noisePlan": {"enabled": bool(old_noise.get("enabled", True)), "rationale": rationale}}

    # AgentCore Gateway accepts only these keys in a tool inputSchema; the engine's build-time
    # tools.gateway_schema check rejects anything else. Kiro drafts often add enum/min/max/default/
    # additionalProperties, so fold those constraints into the description instead of failing the build.
    gateway_keys = {"type", "properties", "required", "items", "description"}

    def gateway_schema(schema: Any, root: bool = False) -> Any:
        if not isinstance(schema, dict):
            return {"type": "object"} if root else schema
        out = {k: v for k, v in schema.items() if k in gateway_keys}
        notes = []
        if isinstance(schema.get("enum"), list):
            notes.append("allowed: " + " | ".join(str(v) for v in schema["enum"]))
        if "minimum" in schema and "maximum" in schema:
            notes.append(f"range {schema['minimum']}–{schema['maximum']}")
        elif "minimum" in schema:
            notes.append(f"min {schema['minimum']}")
        elif "maximum" in schema:
            notes.append(f"max {schema['maximum']}")
        if "default" in schema:
            notes.append(f"default {schema['default']}")
        if notes:
            base = str(out.get("description") or "").strip()
            out["description"] = f"{base} ({'; '.join(notes)})" if base else "; ".join(notes)
        if isinstance(out.get("properties"), dict):
            out["properties"] = {n: gateway_schema(s) for n, s in out["properties"].items()}
        if isinstance(out.get("items"), dict):
            out["items"] = gateway_schema(out["items"])
        if root:
            out["type"] = "object"
        return out

    remap = _retrieval_remap_target(raw)
    tools: list[dict[str, Any]] = []
    for item in raw.get("tools", []) or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"])
        kind = "retrieval" if item.get("kind") == "retrieval" or name == remap else "mock"
        if name == remap:
            canon.warnings.append(f"tools[{name}]: kind {item.get('kind')!r} remapped to retrieval (no tool had kind=retrieval)")
            if isinstance(item.get("fixtures"), dict) and item["fixtures"]:
                canon.warnings.append(f"tools[{name}]: fixtures dropped (the retrieval tool reads the knowledge base)")
        schema = gateway_schema(item.get("inputSchema"), root=True)
        if kind == "retrieval":
            forced = _force_query_schema(schema)
            if _canonical_json(forced) != _canonical_json(schema):
                canon.warnings.append(f"tools[{name}]: retrieval inputSchema now requires the string property 'query'")
            schema = forced
        tool: dict[str, Any] = {
            "name": name,
            "description": str(item.get("description") or f"Draft tool {name}."),
            "kind": kind,
            "inputSchema": schema,
            "provenance": "ai_draft",
        }
        if kind == "mock":
            source = item.get("fixtures") if isinstance(item.get("fixtures"), dict) else {}
            cases = []
            for case in source.get("cases", []) or []:
                if not isinstance(case, dict):
                    continue
                when = case.get("when") if isinstance(case.get("when"), dict) else case.get("input")
                returned = case.get("return") if isinstance(case.get("return"), dict) else case.get("output")
                if isinstance(when, dict) and when and isinstance(returned, dict):
                    row = {"when": when, "return": returned}
                    if case.get("note"):
                        row["note"] = str(case["note"])
                    cases.append(row)
            errors = []
            for err in source.get("errors", []) or []:
                if not isinstance(err, dict):
                    continue
                when = err.get("when") if isinstance(err.get("when"), dict) else err.get("input")
                value = err.get("error")
                if isinstance(value, dict):
                    note = str(value.get("code") or "")
                    value = str(value.get("message") or json.dumps(value, ensure_ascii=False))
                else:
                    note = ""
                if isinstance(when, dict) and when and value:
                    row = {"when": when, "error": str(value)}
                    if err.get("note") or note:
                        row["note"] = str(err.get("note") or note)
                    errors.append(row)
            # Optional lists are emitted only when the source has them (the handler reads a missing
            # list as empty), so an echoed reviewed tool is not changed by the canonicalizer.
            fixtures: dict[str, Any] = {}
            if "cases" in source or cases:
                fixtures["cases"] = cases
            fixtures["default"] = source.get("default") if isinstance(source.get("default"), dict) else {}
            if "errors" in source or errors:
                fixtures["errors"] = errors
            tool["fixtures"] = fixtures
        _passthrough("tool", item, tool, canon, f"tools[{name}]")
        tools.append(tool)

    # Workshop evaluation always needs one KB retrieval tool. This is a technical adapter shell, not
    # business truth; the real KB id is resolved at Workshop runtime.
    retrievals = [t for t in tools if t["kind"] == "retrieval"]
    if len(retrievals) > 1:
        canon.warnings.append(
            "more than one tool has kind=retrieval (" + ", ".join(t["name"] for t in retrievals)
            + f"); evaluation.retrievalToolName uses {retrievals[0]['name']}"
        )
    retrieval = retrievals[0] if retrievals else None
    if retrieval is None:
        retrieval = {
            "name": "retrieve_policy",
            "description": "Retrieve grounded policy passages from this Scenario Pack knowledge base.",
            "kind": "retrieval",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            "provenance": "ai_draft",
            "origin": {"kind": "sa_authored", "note": "Standard Workshop knowledge-base retrieval tool added by Workshop Customizer."},
        }
        tools.insert(0, retrieval)

    skills: list[dict[str, str]] = []
    for index, item in enumerate(raw.get("skills", []) or [], start=1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("id") or f"skill-{index}").lower().replace("_", "-")
        name = re.sub(r"[^a-z0-9-]", "-", name).strip("-") or f"skill-{index}"
        file = str(item.get("file") or item.get("path") or f"skills/{name}/SKILL.md")
        skills.append({"name": name[:60], "file": file})

    raw_eval = raw.get("evaluation") if isinstance(raw.get("evaluation"), dict) else {}
    cases: list[dict[str, Any]] = []
    default_role = roles[0]["id"]
    default_fact = fact_ids[0] if fact_ids else "missing-fact"
    fact_set = frozenset(fact_ids)
    document_facts = _document_facts(facts, documents)
    expected_keys = ("mustMention", "mustNotMention", "requiredTools", "forbiddenTools", "shouldEscalate", "shouldRefuse")
    for index, item in enumerate(raw_eval.get("goldenSet", []) or [], start=1):
        if not isinstance(item, dict):
            continue
        expected = item.get("expected") if isinstance(item.get("expected"), dict) else {}
        role_id = str(item.get("roleId") or default_role)
        case_id = str(item.get("id") or f"case-{index}")
        if role_id not in role_ids:
            canon.warnings.append(f"evaluation.goldenSet[{case_id}].roleId: unknown role {role_id!r} replaced by {default_role!r}; "
                                  "check who asks this question")
            role_id = default_role
        if not item.get("basis"):
            canon.warnings.append(f"evaluation.goldenSet[{case_id}].basis: none given; set to {default_fact!r}, check it")
        basis = _normalise_basis([str(v) for v in (item.get("basis") or [default_fact])],
                                 where=f"evaluation.goldenSet[{case_id}].basis", fact_ids=fact_set,
                                 document_facts=document_facts, canon=canon)
        row: dict[str, Any] = {
            "id": case_id,
            "label": str(item.get("label") or item.get("id") or f"Case {index}"),
            "query": str(item.get("query") or ""),
            "category": item.get("category") if item.get("category") in ("normal", "boundary", "prohibited") else "normal",
            "set": "holdout" if item.get("set") == "holdout" else "practice",
            "actorId": str(item.get("actorId") or f"actor-{index:03d}"),
            "roleId": role_id,
            "expected": {k: expected[k] for k in expected_keys if k in expected},
            "basis": basis,
            "provenance": "ai_draft",
        }
        _passthrough("expected", expected, row["expected"], canon, f"evaluation.goldenSet[{row['id']}].expected")
        if item.get("criticality") in ("blocking", "advisory"):
            row["criticality"] = item["criticality"]
        if row["category"] == "prohibited":
            row["expected"].setdefault("shouldRefuse", True)
        _passthrough("golden", item, row, canon, f"evaluation.goldenSet[{row['id']}]")
        cases.append(row)
    evaluation: dict[str, Any] = {"retrievalToolName": retrieval["name"], "judgeModel": FIXED_JUDGE_MODEL, "goldenSet": cases}
    _passthrough("evaluation", raw_eval, evaluation, canon, "evaluation")

    raw_prompts = raw.get("prompts") if isinstance(raw.get("prompts"), dict) else {}
    prompts = {
        "baselineFile": str(raw_prompts.get("baselineFile") or "agent/baseline-prompt.md"),
        "optimizationCandidateFile": str(raw_prompts.get("optimizationCandidateFile") or "agent/optimization-candidate.md"),
    }
    raw_labs = raw.get("labs") if isinstance(raw.get("labs"), dict) else {}
    labs = {k: raw_labs[k] for k in ("observations", "studentGuideFile", "instructorGuideFile") if k in raw_labs}
    labs.setdefault("observations", [])
    # Fold the common off-schema spellings of the teaching skeleton into labs.teaching.
    labs_source = dict(raw_labs)
    teaching = raw_labs.get("teaching") if isinstance(raw_labs.get("teaching"), dict) else (
        raw.get("teaching") if isinstance(raw.get("teaching"), dict) else None)
    stray = {k: raw_labs[k] for k in ("firstConversation", "baselineDefects", "phenomena", "stabilityCaseId") if k in raw_labs}
    if stray:
        teaching = {**stray, **(teaching or {})}
    if teaching is not None:
        labs_source["teaching"] = teaching
    _passthrough("labs", labs_source, labs, canon, "labs")

    declared = _normalise_language(raw.get("language"))
    sample = _content_sample(raw) + "\n" + content_text
    detected = detect_language(sample)
    if declared is None:
        declared = detected
    elif declared != detected and sum(_language_signal(sample)) >= 100:
        canon.warnings.append(f"language is {declared!r} but the generated content looks {detected!r}; check scenario.language")

    scenario: dict[str, Any] = {
        "schemaVersion": 1,
        "id": project_id,
        "displayName": str(raw.get("displayName") or project_id),
        "description": str(raw.get("description") or "Kiro-generated Scenario Pack draft."),
        "packKind": pack_kind,
        "language": declared,
        "namespace": namespace,
        "agent": {
            "audience": str(raw_agent.get("audience") or "Scenario users"),
            "purpose": str(raw_agent.get("purpose") or "Assist users within the confirmed scenario scope."),
            "scope": [str(v) for v in (raw_agent.get("scope") or ["Answer in-scope scenario questions"])],
            "outOfScope": [str(v) for v in (raw_agent.get("outOfScope") or [])],
            "roles": roles,
            "handoffConditions": [str(v) for v in (raw_agent.get("handoffConditions") or ["Escalate when business truth is missing."])],
            "prohibitedBehaviors": [str(v) for v in (raw_agent.get("prohibitedBehaviors") or ["Do not invent business truth."])],
        },
        "facts": facts,
        "knowledge": knowledge,
        "tools": tools,
        "skills": skills,
        "prompts": prompts,
        "evaluation": evaluation,
        "labs": labs,
    }
    if isinstance(raw.get("governance"), dict) or isinstance(raw.get("customer"), dict):
        canon.warnings.append("model-supplied customer/governance ignored; the app owns them")
    return scenario


# ---------------------------------------------------------------------------
# App-owned fields and provenance preservation
# ---------------------------------------------------------------------------


MATERIAL_CLASSIFICATIONS = ("synthetic", "internal", "confidential")


def _app_customer(current: dict[str, Any] | None, meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """designs[4]: the customer block comes from meta.customer plus meta.materialsPolicy.

    The project's block (or ``{name: meta.customer}``) is the base; the recorded materials policy
    always sets ``dataClassification`` and ``materialApproval`` (its approvalRef), so the guides show
    the classification banner and a later approval change reaches the next generation.
    """
    customer: dict[str, Any] | None = None
    if isinstance(current, dict) and isinstance(current.get("customer"), dict):
        customer = copy.deepcopy(current["customer"])
    else:
        name = str((meta or {}).get("customer") or "").strip()
        customer = {"name": name[:200]} if name else None
    policy = (meta or {}).get("materialsPolicy")
    if isinstance(policy, dict):
        classification = policy.get("dataClassification")
        approval = str(policy.get("approvalRef") or "").strip()
        if classification in MATERIAL_CLASSIFICATIONS:
            customer = {**(customer or {}), "dataClassification": classification}
        if approval:
            customer = {**(customer or {}), "materialApproval": approval[:500]}
    return customer


def _app_governance(current: dict[str, Any] | None) -> dict[str, Any] | None:
    governance = current.get("governance") if isinstance(current, dict) else None
    return copy.deepcopy(governance) if isinstance(governance, dict) else None


def _app_noise_band(current: dict[str, Any] | None) -> dict[str, Any] | None:
    evaluation = current.get("evaluation") if isinstance(current, dict) else None
    band = evaluation.get("noiseBand") if isinstance(evaluation, dict) else None
    return copy.deepcopy(band) if isinstance(band, dict) else None


def _apply_app_owned(scenario: dict[str, Any], *, current: dict[str, Any] | None, meta: dict[str, Any] | None) -> None:
    if meta is not None:
        scenario["displayName"] = str(meta.get("displayName") or scenario["id"])
    customer = _app_customer(current, meta)
    if customer is not None:
        scenario["customer"] = customer
    governance = _app_governance(current)
    if governance is not None:
        scenario["governance"] = governance
    band = _app_noise_band(current)
    if band is not None:
        scenario["evaluation"]["noiseBand"] = band


# Content that a confirmation vouches for.  An SA-reviewed item keeps its provenance only while
# these fields (for documents: plus the file bytes) are unchanged.
FINGERPRINT_FIELDS: dict[str, tuple[str, ...]] = {
    "fact": ("statement", "criticality"),
    "tool": ("description", "kind", "inputSchema", "fixtures"),
    "document": ("title", "noise"),
    "golden": ("query", "category", "set", "actorId", "roleId", "expected", "basis", "criticality"),
    "guide": tuple(name for name, _shape, _limit in GUIDE_FIELDS),
}


def item_fingerprint(item_type: str, item: dict[str, Any], file_sha: Callable[[str], str | None] | None = None) -> str:
    """sha256 over the canonical JSON of the fields a confirmation vouches for."""
    payload: dict[str, Any] = {"type": item_type}
    for key in FINGERPRINT_FIELDS[item_type]:
        payload[key] = item.get(key)
    if item_type == "document":
        rel = str(item.get("file") or "")
        payload["fileSha256"] = file_sha(rel) if (file_sha is not None and rel) else None
    return _sha256_text(_canonical_json(payload))


def _items(scenario: dict[str, Any] | None) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """(type, key, item) for every provenance-bearing item; tools are keyed by name."""
    if not isinstance(scenario, dict):
        return
    for item in _dict_items(scenario.get("facts")):
        if item.get("id"):
            yield "fact", str(item["id"]), item
    for item in _dict_items(scenario.get("tools")):
        if item.get("name"):
            yield "tool", str(item["name"]), item
    knowledge = scenario.get("knowledge") if isinstance(scenario.get("knowledge"), dict) else {}
    for item in _dict_items(knowledge.get("documents")):
        if item.get("id"):
            yield "document", str(item["id"]), item
    evaluation = scenario.get("evaluation") if isinstance(scenario.get("evaluation"), dict) else {}
    for item in _dict_items(evaluation.get("goldenSet")):
        if item.get("id"):
            yield "golden", str(item["id"]), item
    labs = scenario.get("labs") if isinstance(scenario.get("labs"), dict) else {}
    if isinstance(labs.get("guide"), dict):
        yield "guide", GUIDE_NARRATIVE_ID, labs["guide"]


def _text_sha(files: dict[str, str]) -> Callable[[str], str | None]:
    return lambda rel: _sha256_text(files[rel]) if rel in files else None


def _disk_sha(pdir: Path | None) -> Callable[[str], str | None]:
    def sha(rel: str) -> str | None:
        if pdir is None:
            return None
        try:
            target = (pdir / rel).resolve()
            if pdir.resolve() not in target.parents or not target.is_file():
                return None
            return hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            return None
    return sha


def _preserve_provenance(candidate: dict[str, Any], files: dict[str, str], *, current: dict[str, Any] | None,
                         pdir: Path | None) -> dict[str, Any]:
    """Restore SA review (sa_synthetic / customer_confirmed) on items whose fingerprint is unchanged.

    ``reset`` lists every reviewed item that loses its review: changed ones, and removed ones
    (``removed: true``).

    The comparison is against the item exactly as it is on disk (the content the SA reviewed), never
    against a canonicalized view of it: anything the canonicalizer adds or rewrites in reviewed content
    (a prohibited case's default ``shouldRefuse``, the forced retrieval ``query`` schema, ...) is a
    change, so the item resets to ai_draft and is listed in ``reset``.
    """
    prior_index = {(t, k): item for t, k, item in _items(current)}
    candidate_sha, current_sha = _text_sha(files), _disk_sha(pdir)
    preserved = 0
    reset: list[dict[str, str]] = []
    for item_type, key, item in _items(candidate):
        prior = prior_index.get((item_type, key))
        if not isinstance(prior, dict) or prior.get("provenance") not in FORMAL_PROVENANCE:
            continue
        if item_fingerprint(item_type, item, candidate_sha) == item_fingerprint(item_type, prior, current_sha):
            item["provenance"] = prior["provenance"]
            for name in CONFIRMATION_FIELDS:
                if name in prior:
                    item[name] = copy.deepcopy(prior[name])
                else:
                    item.pop(name, None)
            # The SA's labels (or their absence) travel with the review: Kiro can neither relabel a
            # reviewed item nor give it a class the SA never assigned (confirm / confirm-batch do that).
            # The guide narrative has no origin field (schema $defs/guide): it never carries one.
            if item_type != "guide" and isinstance(prior.get("origin"), dict):
                item["origin"] = copy.deepcopy(prior["origin"])
            else:
                item.pop("origin", None)
            preserved += 1
        else:
            reset.append({"type": item_type, "id": key, "was": str(prior["provenance"])})
    # Deleting a reviewed item loses its review just like changing it does: list it (removed) so a
    # lost customer confirmation still needs acknowledgeConfirmationResets at apply.
    kept = {(item_type, key) for item_type, key, _item in _items(candidate)}
    for (item_type, key), prior in prior_index.items():
        if (item_type, key) not in kept and prior.get("provenance") in FORMAL_PROVENANCE:
            reset.append({"type": item_type, "id": key, "was": str(prior["provenance"]), "removed": True})
    return {"preserved": preserved, "reset": reset}


# ---------------------------------------------------------------------------
# Bundle normalisation and the write-boundary verification
# ---------------------------------------------------------------------------


def _clean_files(files: dict[str, Any], *, normalise: bool = True) -> dict[str, str]:
    """Path-confined, bounded, credential-scanned files.  ``normalise`` adds a final newline to model
    output; stored results and disk-carried files are checked verbatim (what apply writes)."""
    if len(files) > MAX_FILES:
        raise GenerationError(f"generated file count exceeds {MAX_FILES}")
    clean_files: dict[str, str] = {}
    total = 0
    for raw_path, raw_content in files.items():
        rel = _safe_relative_file(raw_path)
        if not isinstance(raw_content, str) or not raw_content.strip():
            raise GenerationError(f"generated file is empty: {rel}")
        if len(raw_content) > MAX_FILE_CHARS:
            raise GenerationError(f"generated file is too large: {rel}")
        if _secret_hit(raw_content):
            raise GenerationError(f"generated file contains credential-shaped material: {rel}")
        total += len(raw_content)
        clean_files[rel] = raw_content if (raw_content.endswith("\n") or not normalise) else raw_content + "\n"
    if total > MAX_TOTAL_FILE_CHARS:
        raise GenerationError("generated files are too large in total")
    return clean_files


def _materialize_files(files: dict[str, Any], *, pdir: Path | None, sent_ids: frozenset[str],
                       meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve ``{"fromMaterial": "mat-…", "prepend": "…"}`` file values into text.

    Kiro can reference a material sent in THIS task instead of echoing it: the file becomes the
    SA-visible prepend plus the material's full extracted text (still bounded and credential-scanned).
    Only ``sent_ids`` (the record's ``materialIds``) qualify, never a material the pack merely cites;
    a material excluded since, or a project now classified confidential, is refused as well.
    """
    out: dict[str, Any] = {}
    index = {str(r.get("id")): r for r in read_materials(pdir)} if pdir is not None and any(isinstance(v, dict) for v in files.values()) else {}
    policy = (meta or {}).get("materialsPolicy") if isinstance((meta or {}).get("materialsPolicy"), dict) else {}
    for rel, value in files.items():
        if not isinstance(value, dict):
            out[rel] = value
            continue
        mid = str(value.get("fromMaterial") or "")
        if not MATERIAL_ID_RE.fullmatch(mid) or mid not in sent_ids or pdir is None:
            raise GenerationError(f"file {rel} references material {mid or '<none>'!r}, which was not sent to Kiro")
        if mid not in index or not _usable(index[mid]):
            raise GenerationError(f"file {rel}: material {mid} is excluded from generation or no longer uploaded")
        if policy.get("dataClassification") == "confidential":
            raise GenerationError(f"file {rel}: the project's materials are classified confidential; no material text is copied into the pack")
        prepend = value.get("prepend") if isinstance(value.get("prepend"), str) else ""
        if len(prepend) > 2000:
            raise GenerationError(f"file {rel}: prepend is limited to 2000 characters")
        text = material_text(pdir, mid)
        if not text.strip():
            raise GenerationError(f"file {rel}: material {mid} has no extracted text")
        content = (prepend.rstrip() + "\n\n" + text) if prepend.strip() else text
        if len(content) > MAX_FILE_CHARS:
            raise GenerationError(f"file {rel} built from material {mid} exceeds {MAX_FILE_CHARS} characters")
        out[rel] = content
    return out


def _get_path(data: dict[str, Any], path: str) -> tuple[bool, Any]:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _set_path(data: dict[str, Any], path: str, present: bool, value: Any) -> None:
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            if not present:
                return
            node[part] = {}
        node = node[part]
    if present:
        node[parts[-1]] = copy.deepcopy(value)
    else:
        node.pop(parts[-1], None)


def normalise_scopes(value: Any) -> list[str] | None:
    """A scope list in SCOPES order; None when absent; GenerationError for unknown scopes."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        raise GenerationError("scope must be a list of scope names")
    unknown = sorted(set(value) - set(SCOPES))
    if unknown:
        raise GenerationError("unknown scope: " + ", ".join(unknown) + f"; use {', '.join(SCOPES)}")
    return [s for s in SCOPES if s in value]


def _scope_mask(candidate: dict[str, Any], current: dict[str, Any], scopes: list[str], ignored: list[dict[str, str]]) -> None:
    """Copy every section outside ``scopes`` from the current scenario (as it is on disk)."""
    allowed = ", ".join(scopes) or "none"
    for scope, paths in SCOPE_SECTIONS.items():
        if scope in scopes:
            continue
        for path in paths:
            had, value = _get_path(current, path)
            has, proposed = _get_path(candidate, path)
            if (had, value) != (has, proposed):
                ignored.append({"target": path, "reason": f"outside the allowed scopes ({allowed})"})
            _set_path(candidate, path, had, value)


def _disk_text(pdir: Path | None, rel: str) -> str | None:
    """A project file's exact text: decoded from its bytes, so CRLF line endings survive (``read_text``
    would turn them into LF, and a carried-over file would then no longer match its own sha256)."""
    if pdir is None:
        return None
    try:
        target = (pdir / rel).resolve()
        if pdir.resolve() not in target.parents or not target.is_file():
            return None
        return target.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _merge_files(candidate: dict[str, Any], returned: dict[str, str], *, mode: str, scopes: list[str] | None,
                 pdir: Path | None, read_only: frozenset[str], ignored: list[dict[str, str]]) -> dict[str, str]:
    """The COMPLETE referenced file set of the candidate.

    A draft must return every referenced file itself.  Repair and regenerate return only new or
    changed files; every other referenced file is carried over from disk verbatim.  Returned files
    outside the allowed scopes, read-only (omitted) files and unreferenced files are ignored.
    """
    owners = _referenced_owners(candidate)
    accepted: dict[str, str] = {}
    for rel, text in returned.items():
        if rel in read_only:
            ignored.append({"target": rel, "reason": "read-only file (too large to send); the disk version is kept"})
        elif rel not in owners:
            ignored.append({"target": rel, "reason": "not referenced by the scenario"})
        elif scopes is not None and owners[rel] not in scopes:
            ignored.append({"target": rel, "reason": f"file of scope {owners[rel]}, outside the allowed scopes ({', '.join(scopes) or 'none'})"})
        else:
            accepted[rel] = text
    merged: dict[str, str] = {}
    missing: list[str] = []
    for rel in sorted(owners):
        if rel in accepted:
            merged[rel] = accepted[rel]
            continue
        disk = _disk_text(pdir, rel) if mode in MERGE_MODES else None
        if disk is None:
            missing.append(rel)
        else:
            merged[rel] = disk
    if missing:
        raise GenerationError("generated scenario references missing files: " + ", ".join(missing))
    return merged


DIFF_SECTIONS = ("namespace", "description", "language", "agent", "prompts", "knowledge.noisePlan", "evaluation.retrievalToolName",
                 "evaluation.l1", "evaluation.judge", "labs.observations", "labs.teaching", "labs.studentGuideFile",
                 "labs.instructorGuideFile")


def _diff(current: dict[str, Any] | None, candidate: dict[str, Any], files: dict[str, str], *,
          pdir: Path | None, delete: list[str], provenance: dict[str, Any]) -> dict[str, Any]:
    before = {(t, k): item for t, k, item in _items(current)}
    after = {(t, k): item for t, k, item in _items(candidate)}
    added = [{"type": t, "id": k} for (t, k) in after if (t, k) not in before]
    removed = [{"type": t, "id": k} for (t, k) in before if (t, k) not in after]
    modified = []
    for key, item in after.items():
        prior = before.get(key)
        if prior is None:
            continue
        fields = sorted(name for name in set(prior) | set(item) if prior.get(name) != item.get(name))
        if fields:
            modified.append({"type": key[0], "id": key[1], "fields": fields})
    sections = [path for path in DIFF_SECTIONS if _get_path(current or {}, path) != _get_path(candidate, path)]
    file_added, file_modified, unchanged = [], [], 0
    for rel, text in sorted(files.items()):
        disk = _disk_text(pdir, rel)
        if disk is None:
            file_added.append(rel)
        elif disk != text:
            file_modified.append(rel)
        else:
            unchanged += 1
    return {
        "items": {"added": added, "removed": removed, "modified": modified},
        "sections": sections,
        "files": {"added": file_added, "modified": file_modified, "deleted": sorted(delete), "unchanged": unchanged},
        "provenance": provenance,
    }


def _anchor_candidates(value: Any, scenario: dict[str, Any]) -> dict[str, list[str]]:
    cases = {c.get("id"): c for c in _dict_items((scenario.get("evaluation") or {}).get("goldenSet"))}
    out: dict[str, list[str]] = {"normal": [], "boundary": [], "prohibited": []}
    if isinstance(value, dict):
        for category in out:
            for cid in _string_list(value.get(category)) or []:
                if cid in cases and cases[cid].get("category") == category and cid not in out[category]:
                    out[category].append(cid)
    return out


def _changes(value: Any) -> list[dict[str, str]]:
    out = []
    for item in _dict_items(value)[:100]:
        row = {key: _scalar_text(item.get(key))[:300] for key in ("finding", "target", "action") if _scalar_text(item.get(key))}
        if row:
            out.append(row)
    return out


def _normalise_bundle(payload: dict[str, Any], *, project_id: str, pack_kind: str,
                      current: dict[str, Any] | None = None, meta: dict[str, Any] | None = None,
                      pdir: Path | None = None, material_ids: frozenset[str] = frozenset(), mode: str = "draft",
                      scopes: list[str] | None = None, read_only: frozenset[str] = frozenset(),
                      sent_material_ids: frozenset[str] | None = None) -> dict[str, Any]:
    """Model payload -> stored result.  ``current``/``meta``/``pdir`` supply app-owned values, the
    reviewed items whose provenance may be preserved and (repair / regenerate) the files carried over
    from disk; ``scopes`` (repair / scoped regenerate) masks every other section back to disk.
    ``material_ids`` are the ids an ``origin`` may cite; ``sent_material_ids`` (default: the same)
    are the materials whose text this task carried, the only ones a ``fromMaterial`` file may copy."""
    status = str(payload.get("status") or "ready")
    questions = payload.get("openQuestions") or []
    if not isinstance(questions, list) or not all(isinstance(q, str) for q in questions):
        raise GenerationError("openQuestions must be a list of strings")
    if status == "needs_input":
        if not questions:
            raise GenerationError("needs_input must include openQuestions")
        return {"status": "needs_input", "openQuestions": questions[:20]}
    if status != "ready":
        raise GenerationError("Kiro generation status must be ready or needs_input")

    scenario = payload.get("scenario")
    files = payload.get("files") if "files" in payload or mode not in MERGE_MODES else {}
    if not isinstance(scenario, dict):
        if mode in MERGE_MODES and isinstance(payload.get("scenarioPatch"), dict):
            raise GenerationError(f"{mode} output must carry the FULL scenario (scenarioPatch is not accepted)")
        raise GenerationError("ready output needs scenario and files objects")
    if not isinstance(files, dict):
        raise GenerationError("ready output needs scenario and files objects")
    sent = frozenset(material_ids if sent_material_ids is None else sent_material_ids)
    files = _materialize_files(files, pdir=pdir, sent_ids=sent, meta=meta)
    model_namespace = scenario.get("namespace")
    canon = _Canon(material_ids=frozenset(material_ids))
    ignored: list[dict[str, str]] = []
    documents_text = "\n".join(str(text) for rel, text in files.items() if str(rel).startswith("knowledge-base/docs/"))
    if mode in MERGE_MODES and current:
        documents_text += "\n" + "\n".join(
            _disk_text(pdir, rel) or "" for rel, owner in _referenced_owners(current).items() if owner == "knowledge")
    scenario = _canonicalize_scenario(scenario, project_id=project_id, pack_kind=pack_kind, canon=canon,
                                      content_text=documents_text)
    _apply_app_owned(scenario, current=current, meta=meta)
    scenario["namespace"] = app_namespace(project_id, current=current, mode=mode)
    files = _rename_model_targets(files, scenario, model_namespace, canon)
    if mode in MERGE_MODES and isinstance(current, dict) and "namespace" in current and current.get("namespace") != scenario["namespace"]:
        canon.warnings.append("namespace: the project's namespace breaks the AWS name limits; replaced with the app-derived "
                              "names (resources deployed under the old names are not cleaned up automatically)")
    if mode in MERGE_MODES and scopes is not None and current is not None:
        _scope_mask(scenario, current, scopes, ignored)
    if scenario.get("schemaVersion") != 1:
        raise GenerationError("generated scenario.schemaVersion must be 1")
    if scenario.get("id") != project_id:
        raise GenerationError("generated scenario id does not match the project")
    if scenario.get("packKind") != pack_kind:
        raise GenerationError("generated scenario packKind does not match the project")
    required = {"displayName", "description", "namespace", "agent", "facts", "knowledge", "tools", "skills", "prompts", "evaluation", "labs"}
    missing = sorted(required - set(scenario))
    if missing:
        raise GenerationError("generated scenario is missing: " + ", ".join(missing))

    evaluation = scenario.get("evaluation")
    if not isinstance(evaluation, dict):
        raise GenerationError("generated scenario.evaluation must be an object")
    # Model choice is a pinned Workshop-template concern, not a drafting decision.
    evaluation["judgeModel"] = FIXED_JUDGE_MODEL

    clashes = _case_clashes(_referenced_files(scenario))
    if clashes:
        raise GenerationError("generated scenario references files that differ only by letter case: " + ", ".join(clashes))
    returned = _clean_files(files)
    merged = _merge_files(scenario, returned, mode=mode, scopes=scopes if mode in MERGE_MODES else None,
                          pdir=pdir, read_only=frozenset(read_only), ignored=ignored)
    carried = _clean_files({rel: text for rel, text in merged.items() if rel not in returned}, normalise=False)
    merged = {**carried, **{rel: text for rel, text in merged.items() if rel in returned}}
    if len(merged) > MAX_FILES:
        raise GenerationError(f"generated file count exceeds {MAX_FILES}")
    if sum(len(text) for text in merged.values()) > MAX_TOTAL_FILE_CHARS:
        raise GenerationError("generated files are too large in total")
    if _secret_hit(json.dumps(scenario, ensure_ascii=False)):
        raise GenerationError("generated scenario contains credential-shaped material")
    provenance = _preserve_provenance(scenario, merged, current=current, pdir=pdir)
    delete, untracked = orphan_files(pdir, scenario)
    diff = _diff(current, scenario, merged, pdir=pdir, delete=delete, provenance=provenance)

    ledger = payload.get("truthLedger") or []
    if not isinstance(ledger, list):
        raise GenerationError("truthLedger must be a list")
    summary = str(payload.get("summary") or "Kiro generated a Scenario Pack draft.")[:2000]
    return {
        "status": "ready",
        "mode": mode,
        "scenario": scenario,
        "files": dict(sorted(merged.items())),
        "changedFiles": diff["files"]["added"] + diff["files"]["modified"],
        "truthLedger": ledger[:100],
        "openQuestions": questions[:20],
        "summary": summary,
        "changes": _changes(payload.get("changes")),
        "anchorCandidates": _anchor_candidates(payload.get("anchorCandidates"), scenario),
        "deleteOrphans": delete,
        "untracked": untracked,
        "ignoredChanges": ignored[:200],
        "warnings": canon.warnings[:100],
        "diff": diff,
    }


def _verify_bundle(record: dict[str, Any], result: dict[str, Any], *, pdir: Path,
                   current: dict[str, Any], meta: dict[str, Any]) -> dict[str, str]:
    """Write-boundary check of a stored result (the record is on disk and is not trusted).

    Returns the files to write.  Refuses anything the canonicalizer would never have produced:
    foreign app-owned fields, provenance the SA did not record on identical content, unsafe files,
    and an orphan list that differs from the project's files.
    """
    scenario, files = result.get("scenario"), result.get("files")
    if not isinstance(scenario, dict) or not isinstance(files, dict):
        raise GenerationError("generation bundle is incomplete")
    # The expected values come from the project being written, never from the record itself.
    project_id = pdir.name
    pack_kind = str(meta.get("packKind") or "customer")  # the same default prepare_generation uses
    problems: list[str] = []

    def expect(ok: bool, message: str) -> None:
        if not ok:
            problems.append(message)

    expect(str(record.get("projectId")) == project_id, "generation record belongs to another project")
    expect(str(meta.get("id") or project_id) == project_id, "project metadata id does not match the project directory")
    expect(str(record.get("packKind")) == pack_kind, "generation record packKind differs from the project's packKind")
    evaluation = scenario.get("evaluation") if isinstance(scenario.get("evaluation"), dict) else {}
    expect(scenario.get("schemaVersion") == 1, "schemaVersion must be 1")
    expect(scenario.get("id") == project_id, "scenario id does not match the project")
    expect(scenario.get("packKind") == pack_kind, "packKind does not match the project")
    expect(scenario.get("namespace") == app_namespace(project_id, current=current, mode=str(record.get("mode") or "draft")),
           "namespace is not the app-owned namespace")
    expect(evaluation.get("judgeModel") == FIXED_JUDGE_MODEL, "evaluation.judgeModel is not the fixed Workshop judge")
    expect(scenario.get("governance") == _app_governance(current), "governance differs from the project's")
    expect(scenario.get("customer") == _app_customer(current, meta), "customer differs from the project's")
    expect(evaluation.get("noiseBand") == _app_noise_band(current), "evaluation.noiseBand differs from the project's calibration")
    for tool in _dict_items(scenario.get("tools")):
        if tool.get("kind") == "retrieval":
            expect(_has_query_property(tool.get("inputSchema")), f"retrieval tool {tool.get('name')} lacks the required string property query")
    guide = (scenario.get("labs") or {}).get("guide") if isinstance(scenario.get("labs"), dict) else None
    if isinstance(guide, dict):
        expect(guide.get("id") == GUIDE_NARRATIVE_ID, "labs.guide id must be guide-narrative")

    clean: dict[str, str] = {}
    try:
        clean = _clean_files(files, normalise=False)
        missing = sorted(_referenced_files(scenario) - set(clean))
        expect(not missing, "referenced files are missing: " + ", ".join(missing))
    except GenerationError as exc:
        problems.append(str(exc))
    if "deleteOrphans" in result:
        delete = result.get("deleteOrphans")
        expect(isinstance(delete, list) and sorted(delete) == orphan_files(pdir, scenario)[0],
               "the orphan list differs from the project's files")

    # Formal provenance is accepted only on content identical to the item as it is on disk now.
    prior_index = {(t, k): item for t, k, item in _items(current)}
    candidate_sha, current_sha = _text_sha(clean), _disk_sha(pdir)
    for item_type, key, item in _items(scenario):
        provenance = item.get("provenance")
        if provenance == "ai_draft":
            expect(not any(name in item for name in CONFIRMATION_FIELDS), f"{item_type} {key}: ai_draft item carries confirmation fields")
            continue
        if provenance not in FORMAL_PROVENANCE:
            problems.append(f"{item_type} {key}: provenance {provenance!r} was not produced by the app")
            continue
        prior = prior_index.get((item_type, key))
        same = (
            isinstance(prior, dict) and prior.get("provenance") == provenance
            and all(prior.get(name) == item.get(name) for name in CONFIRMATION_FIELDS)
            and (prior.get("origin") if item_type != "guide" and isinstance(prior.get("origin"), dict) else None) == item.get("origin")
            and item_fingerprint(item_type, item, candidate_sha) == item_fingerprint(item_type, prior, current_sha)
        )
        expect(same, f"{item_type} {key}: {provenance} provenance does not match the reviewed project item")
    expect(not _secret_hit(json.dumps(scenario, ensure_ascii=False)), "scenario contains credential-shaped material")
    if problems:
        more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
        raise GenerationError("generation record failed verification; generate again: " + "; ".join(problems[:5]) + more)
    return clean


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    files = result.get("files") if isinstance(result, dict) else {}
    changed = result.get("changedFiles") if isinstance(result.get("changedFiles"), list) else None
    previews = []
    if isinstance(files, dict):
        for path, content in sorted(files.items()):
            if changed is not None and path not in changed:
                continue  # unchanged files carried over from disk are not previewed
            text = str(content)
            previews.append({"path": path, "preview": text[:4000], "truncated": len(text) > 4000})
    diff = result.get("diff") if isinstance(result.get("diff"), dict) else {}
    return {
        "id": record.get("id"),
        "projectId": record.get("projectId"),
        "status": record.get("status"),
        "mode": record.get("mode") or "draft",
        "scope": record.get("scope"),
        "runner": record.get("runner") or "kirocrew-spawn",
        "round": record.get("round") or 1,
        "parentGenerationId": record.get("parentGenerationId"),
        "timeoutSecs": _record_timeout(record),
        "task": record.get("task"),
        "staged": bool(record.get("staged")),
        "stages": [{k: s.get(k) for k in ("name", "status", "attempts", "startedAt", "completedAt", "error", "problems", "history", "write", "kept", "keptAttempt")}
                   | {"estTokens": (s.get("task") or {}).get("estTokens")} for s in record.get("stages") or []],
        "materialsUsed": record.get("materialsUsed") or [],
        "validationRef": record.get("validationRef"),
        "createdAt": record.get("createdAt"),
        "updatedAt": record.get("updatedAt"),
        "completedAt": record.get("completedAt"),
        "summary": result.get("summary") if isinstance(result, dict) else None,
        "openQuestions": result.get("openQuestions", []) if isinstance(result, dict) else [],
        "truthLedger": result.get("truthLedger", []) if isinstance(result, dict) else [],
        "warnings": result.get("warnings", []) if isinstance(result, dict) else [],
        "precheck": result.get("precheck"),
        "changes": result.get("changes", []),
        "anchorCandidates": result.get("anchorCandidates"),
        "deleteOrphans": result.get("deleteOrphans", []),
        "untracked": result.get("untracked", []),
        "ignoredChanges": result.get("ignoredChanges", []),
        "diff": diff or None,
        "provenance": diff.get("provenance"),
        "generatedFiles": sorted(files) if isinstance(files, dict) else [],
        "changedFiles": changed if changed is not None else (sorted(files) if isinstance(files, dict) else []),
        "scenarioPreview": result.get("scenario") if isinstance(result, dict) else None,
        "filePreviews": previews,
        "error": record.get("error"),
        "appliedAt": record.get("appliedAt"),
        "snapshot": record.get("snapshot"),
        "revertedAt": record.get("revertedAt"),
        "supersededBy": record.get("supersededBy"),
    }


# ---------------------------------------------------------------------------
# Validation findings -> repair classification (server.validate persists it in build/validate.json)
# ---------------------------------------------------------------------------

REPAIR_SOURCES = ("schema", "xref", "policy", "output", "render", "workspace", "gate")
_SCHEMA_SEGMENT_SCOPES = {
    "facts": ("facts",), "knowledge": ("knowledge",), "tools": ("tools",), "skills": ("skills",),
    "prompts": ("prompts",), "agent": ("agent",), "description": ("agent",), "language": ("agent",),
}
_XREF_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("labs.instructorGuideFile is instructor-only", ("guides",)),  # point the supplement at its own guides/ file
    ("cites unknown fact", ("facts", "golden")),
    ("references unknown tool", ("tools", "golden")),
    ("references unknown role", ("agent", "golden")),
    ("knowledge document", ("knowledge",)),
    ("skill '", ("skills",)),
    ("prompts.", ("prompts",)),
    ("labs.studentGuideFile", ("guides",)),
    ("labs.instructorGuideFile", ("guides",)),
    ("labs.teaching", ("labs", "golden")),
    ("evaluation.l1.escalationTools", ("golden", "tools")),
    ("retrievalToolName", ("tools",)),
    ("duplicate fact id", ("facts",)),
    ("duplicate tool name", ("tools",)),
    ("duplicate skill name", ("skills",)),
    ("duplicate role id", ("agent",)),
    ("duplicate golden case id", ("golden",)),
    ("id used by more than one item type", ("facts", "knowledge", "tools", "skills", "golden")),
    ("reserved for the labs.guide", ("facts", "knowledge", "tools", "skills", "golden")),
)
_RENDER_GOLDEN = ("pack has no practice golden cases", "no retrieval probes", "|P| = 0")


def _ordered(scopes: Any) -> list[str]:
    values = set(scopes or ())
    return [s for s in SCOPES if s in values]


def _schema_scopes(message: str) -> list[str]:
    path = message.split(":", 1)[0].strip()
    parts = path.split("/")
    first = parts[0]
    if first == "evaluation":
        second = parts[1] if len(parts) > 1 else ""
        return ["tools"] if second == "retrievalToolName" else ["golden"]
    if first == "labs":
        second = parts[1] if len(parts) > 1 else ""
        return ["guides"] if second in ("guide", "studentGuideFile", "instructorGuideFile") else ["labs"]
    return _ordered(_SCHEMA_SEGMENT_SCOPES.get(first, ()))


def _xref_scopes(message: str) -> list[str]:
    for needle, scopes in _XREF_RULES:
        if needle in message:
            return _ordered(scopes)
    return []


def classify_findings(validation: dict[str, Any]) -> dict[str, Any]:
    """Every validation finding as a numbered RepairFinding, split into ``repairable`` (Kiro can fix
    it inside the listed scopes), ``saOnly`` (a review or policy decision: gate violations, material
    advisories, and any finding marked ``saOnly``, such as a reviewed item citing a deleted material)
    and ``engine`` (no scenario scope: a template or app problem).  ``suggestedScopes``
    is the union of the repairable errors' scopes.  Orphan-file warnings are left out: apply prunes
    orphans.
    """
    rows: list[dict[str, Any]] = []
    sa_decisions: set[int] = set()  # id() of rows only the SA can resolve (a finding marked saOnly)

    def add(source: str, code: str, severity: str, message: str, path: Any, scopes: list[str], item_id: Any = None,
            *, sa_only: bool = False) -> None:
        row = {"source": source, "code": code, "severity": severity, "message": str(message)[:2000],
               "path": None if path is None else str(path), "scopes": scopes}
        if item_id is not None:
            row["itemId"] = str(item_id)
        rows.append(row)
        if sa_only:
            sa_decisions.add(id(row))

    kind = validation.get("schemaKind")
    for message in validation.get("schema") or []:
        source = "xref" if kind == "xref" else "schema"
        scopes = _xref_scopes(str(message)) if source == "xref" else _schema_scopes(str(message))
        add(source, source, "error", str(message), str(message).split(":", 1)[0] if source == "schema" else None, scopes)
    for source in ("policy", "output"):
        for finding in _dict_items(validation.get(source)):
            add(source, str(finding.get("code") or source), str(finding.get("severity") or "error"),
                str(finding.get("message") or ""), finding.get("path"), _ordered(finding.get("scopes")))
    for message in validation.get("render") or []:
        scopes = ["golden"] if any(needle in str(message) for needle in _RENDER_GOLDEN) else []
        add("render", "render", "error", str(message), None, scopes)
    for finding in _dict_items(validation.get("workspace")):
        if finding.get("code") == "project.orphan_file":
            continue
        add("workspace", str(finding.get("code")), str(finding.get("severity") or "warning"),
            str(finding.get("message") or ""), finding.get("path"), _ordered(finding.get("scopes")),
            sa_only=finding.get("saOnly") is True)
    for violation in _dict_items(validation.get("gate")):
        if violation.get("waived"):
            continue
        add("gate", "gate." + re.sub(r"\s+", "_", str(violation.get("item_type") or "item")), "error",
            str(violation.get("reason") or ""), violation.get("item_id"), [], item_id=violation.get("item_id"))

    order = {name: index for index, name in enumerate(REPAIR_SOURCES)}
    rows.sort(key=lambda r: (order[r["source"]], r["path"] or "", r["message"]))
    repairable, sa_only, engine = [], [], []
    for number, source_row in enumerate(rows, start=1):
        row = {"id": f"F{number}", **source_row}
        if row["source"] == "gate" or row["code"].startswith("materials.") or id(source_row) in sa_decisions:
            sa_only.append(row)
        elif row["scopes"]:
            repairable.append(row)
        else:
            engine.append(row)
    suggested = _ordered({s for row in repairable if row["severity"] == "error" for s in row["scopes"]})
    return {"repairable": repairable, "saOnly": sa_only, "engine": engine, "suggestedScopes": suggested}


# ---------------------------------------------------------------------------
# Contracts and task assembly
# ---------------------------------------------------------------------------

#: The first line of every task body, per mode (``${mode_directive}`` in contracts/core.md).
MODE_DIRECTIVES = {
    "draft": "Create a complete Scenario Pack draft for Workshop Customizer.",
    "repair": ("Repair the current Scenario Pack for Workshop Customizer: fix exactly the numbered findings and "
               "SA instructions in REPAIR_INPUT, inside REPAIR_INPUT.allowedScopes."),
    "regenerate": ("Regenerate the complete Scenario Pack for Workshop Customizer from INPUT_DATA and the current "
                   "pack in REPAIR_INPUT, keeping the ids of items that still apply."),
    "regenerate-scoped": ("Regenerate only REPAIR_INPUT.allowedScopes of the current Scenario Pack for Workshop "
                          "Customizer; keep everything else exactly as it is."),
}
#: Contract sections that apply only to some modes (dropped, but still counted as included, otherwise).
MODE_CONTRACTS = {"repair": frozenset({"repair", "regenerate"})}
#: contracts/repair.md's mode-specific merge rule (``${merge_rule}``) and last REPAIR SELF-CHECK item
#: (``${merge_check}``).  A repair changes only what its findings and instructions cite; a regenerate rebuilds
#: its scopes, also with no finding and no instruction (the UI's regenerate buttons send none, and the loop
#: escalates to one when repairs stop making progress).  Both keep the golden set (repair.md's shared rules).
MERGE_RULES = {
    "repair": ("Change only what the numbered findings and saInstructions cite, inside REPAIR_INPUT.allowedScopes: "
               "every other value, sentence and file stays exactly as it is in currentScenario and currentFiles."),
    "regenerate": ("Rebuild REPAIR_INPUT.allowedScopes to this contract from INPUT_DATA and the current pack, also "
                   "when REPAIR_INPUT has no findings or saInstructions (fix and follow any it has); keep the ids of "
                   "items that still apply and the golden set (next rule)."),
}
MERGE_CHECKS = {
    "repair": ("C3 Every changes[] entry names the finding (F1, R2 ...) or sa-instruction it fixes, and nothing that no "
               "entry names differs from REPAIR_INPUT."),
    "regenerate": ("C3 Every finding (F1, R2 ...) and the saInstructions, when given, have a changes[] entry; a rewrite "
                   'none of them asks for names "finding":"regenerate".'),
}


def load_contracts(directory: Path | None = None) -> dict[str, str | None]:
    """Every contract section by name; optional sections that are not installed are None."""
    directory = directory if directory is not None else CONTRACTS_DIR
    contracts: dict[str, str | None] = {}
    for name in CONTRACT_NAMES:
        path = directory / f"{name}.md"
        if path.is_file():
            contracts[name] = path.read_text(encoding="utf-8")
        elif name in REQUIRED_CONTRACTS:
            raise GenerationError(f"generation contract {name}.md is missing from the app")
        else:
            contracts[name] = None
    return contracts


def _render_contract(contracts: dict[str, str | None], values: dict[str, str], *, omit: frozenset[str] = frozenset()) -> str:
    core = contracts.get("core")
    if core is None:
        raise GenerationError("generation contract core.md is missing from the app")
    lines: list[str] = []
    included: set[str] = set()
    for line in core.splitlines(keepends=True):
        match = _INCLUDE_RE.fullmatch(line.rstrip("\n"))
        if match is None:
            lines.append(line)
            continue
        name = match.group(1)
        if name == "core" or name not in contracts:
            raise GenerationError(f"generation contract includes an unknown section: {name}")
        included.add(name)
        text = contracts[name]
        if text is not None and name not in omit:
            lines.append(text if text.endswith("\n") else text + "\n")
    orphaned = sorted(n for n, t in contracts.items() if t is not None and n != "core" and n not in included)
    if orphaned:
        raise GenerationError("generation contract sections are installed but never included: " + ", ".join(orphaned))
    try:
        return string.Template("".join(lines)).substitute(values)
    except (KeyError, ValueError) as exc:
        raise GenerationError(f"generation contract has an invalid placeholder: {exc}") from exc


def _data_json(value: Any) -> str:
    """Untrusted data as JSON.  The output markers are written with an escaped underscore (valid JSON,
    the same text once parsed), so data can never spell the literal marker lines in the task."""
    return json.dumps(value, ensure_ascii=False, indent=2).replace("WORKSHOP_PACK_JSON", "WORKSHOP\\u005fPACK\\u005fJSON")


def task_header(mode: str, round_: int, project_id: str) -> str:
    return f"WORKSHOP_CUSTOMIZER_TASK mode={mode} round={round_} project={project_id}"


def _compose_task(*, project_id: str, display_name: str, pack_kind: str, customer: str, brief: str,
                  contracts: dict[str, str | None] | None = None, mode: str = "draft", round_: int = 1,
                  scoped: bool = False, answers: list[dict[str, str]] | None = None,
                  materials: list[dict[str, Any]] | None = None, repair: dict[str, Any] | None = None,
                  language: str | None = None, namespace: dict[str, str] | None = None) -> tuple[str, dict[str, str | None]]:
    """(task text, {contract name: sha256 | None}) for one bounded, data-delimited Kiro turn.

    Layout: the header line, the contract body (``contracts/core.md`` with its includes), then the
    untrusted data as JSON: INPUT_DATA (brief, answers, materials) and, for repair and regenerate,
    REPAIR_INPUT (findings, SA instructions, scopes, the current scenario and files).
    """
    contracts = contracts if contracts is not None else load_contracts()
    directive = MODE_DIRECTIVES["regenerate-scoped" if (mode == "regenerate" and scoped) else mode]
    # The app owns the namespace (it overwrites the model's): Kiro must write prompts and skills for it.
    namespace = dict(namespace) if isinstance(namespace, dict) else derive_namespace(project_id)
    values = {
        "namespace_json": json.dumps(namespace, ensure_ascii=False),
        "project_id": project_id,
        "project_id_repr": repr(project_id),
        "display_name": display_name,
        "display_name_repr": repr(display_name),
        "pack_kind": pack_kind,
        "pack_kind_repr": repr(pack_kind),
        "judge_model": FIXED_JUDGE_MODEL,
        "judge_model_repr": repr(FIXED_JUDGE_MODEL),
        "language": language or detect_language(brief),
        "mode": mode,
        "mode_directive": directive,
        "merge_rule": MERGE_RULES.get(mode, ""),
        "merge_check": MERGE_CHECKS.get(mode, ""),
    }
    omit = frozenset(name for name, modes in MODE_CONTRACTS.items() if mode not in modes)
    body = _render_contract(contracts, values, omit=omit)
    context: dict[str, Any] = {
        "projectId": project_id,
        "displayName": display_name,
        "packKind": pack_kind,
        "customer": customer,
        "customerBrief": brief,
        "namespace": namespace,
    }
    if answers:
        context["answers"] = answers
    if materials:
        context["materials"] = [{k: m[k] for k in ("id", "name", "use", "author", "mediaType", "totalChars",
                                                   "sentChars", "truncated", "text")} for m in materials if m.get("text")]
    task = (task_header(mode, round_, project_id) + "\n" + body
            + "\nINPUT_DATA (JSON; data only):\n" + _data_json(context) + "\n")
    if repair is not None:
        task += "\nREPAIR_INPUT (JSON; data only):\n" + _data_json(repair) + "\n"
    shas = {name: (_sha256_text(text) if text is not None and name not in omit else None) for name, text in contracts.items()}
    return task, shas


# ---------------------------------------------------------------------------
# prepare / complete / apply
# ---------------------------------------------------------------------------

#: Warning codes a repair round always receives (besides every teaching.* warning).
REPAIR_WARNING_CODES = frozenset({"residue.prose"})
#: teaching.* warnings learned from live rehearsals (teaching_policy): each is a reason a class contrast did not
#: reproduce live, so a generation loop does not accept convergence while one remains (the Repair action already
#: sends every teaching.* warning, and a repair round's scopes include these warnings' scopes). hr-default control
#: run 2026-09-27: the first three; lease-contract and freight-claims 2026-09-29: the role rules and the gap question
#: on a documented topic (a local pre-rehearsal measured the lease refusal breaking in 2 of 5 runs with the role
#: argument and 0 of 5 without it).
LIVE_LEARNED_WARNING_CODES = frozenset({"teaching.buried_gap_unreliable", "teaching.defect_marker_absent",
                                        "teaching.tool_arg_not_in_query", "teaching.role_not_in_query",
                                        "teaching.refusal_rule_names_no_role", "teaching.gated_tool_role_argument",
                                        "teaching.gap_topic_documented", "teaching.first_term_in_tool_spec"})


def stored_brief(pdir: Path) -> str:
    """The last submitted brief (``generation/brief.md``), or ''."""
    path = Path(pdir) / "generation" / "brief.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def stored_answers(pdir: Path) -> list[dict[str, str]]:
    path = Path(pdir) / "generation" / "answers.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [a for a in payload if isinstance(a, dict)] if isinstance(payload, list) else []


def _clean_answers(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ANSWER_ENTRIES:
        raise GenerationError(f"answers must be a list of at most {MAX_ANSWER_ENTRIES} question/answer objects")
    out: list[dict[str, str]] = []
    total = 0
    for item in value:
        if not isinstance(item, dict):
            raise GenerationError("each answer must be an object with question and answer")
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if not question or not answer:
            raise GenerationError("each answer needs a question and an answer")
        if len(question) > MAX_QUESTION_CHARS or len(answer) > MAX_ANSWER_CHARS:
            raise GenerationError(f"a question is limited to {MAX_QUESTION_CHARS} and an answer to {MAX_ANSWER_CHARS} characters")
        total += len(question) + len(answer)
        out.append({"question": question, "answer": answer})
    if total > MAX_ANSWERS_CHARS:
        raise GenerationError(f"answers are limited to {MAX_ANSWERS_CHARS} characters in total")
    return out


def _answers_to_send(stored: list[dict[str, str]], new: list[dict[str, str]], now: str) -> list[dict[str, str]]:
    """The last answers within the entry and character caps (newest kept)."""
    rows = [*stored, *({**a, "at": now} for a in new)][-MAX_ANSWER_ENTRIES:]
    while rows and sum(len(r.get("question", "")) + len(r.get("answer", "")) for r in rows) > MAX_ANSWERS_CHARS:
        rows = rows[1:]
    return rows


def _fresh_validation(pdir: Path) -> dict[str, Any]:
    """build/validate.json, which must still match the scenario bytes and the pack digest."""
    path = pdir / "build" / "validate.json"
    try:
        validation = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GenerationConflict("run Validate first: a repair works from the findings of build/validate.json") from exc
    scenario_sha = hashlib.sha256((pdir / "scenario.yaml").read_bytes()).hexdigest()
    if (not isinstance(validation, dict) or validation.get("scenarioSha256") != scenario_sha
            or validation.get("packDigest") != pack_digest(pdir)):
        raise GenerationConflict("validation is stale; run Validate again before repairing")
    return validation


def current_rehearsal(pdir: Path, meta: dict[str, Any]) -> dict[str, Any] | None:
    """The newest rehearsal of the project's current release, for a repair: run/rehearsal.json (the Guided Run)
    or the direct run's ``build/direct/<version>/direct-rehearsal.json``, whichever was generated last (the Guided
    Run's on a tie). Current means the build it names is the last build (meta.lastBuild and build/build.json) and
    no pack source changed since (sourceHash). Live 2026-10-01: loyalty's class-ready Guided Run hid a newer
    three-round direct rehearsal whose refusal reproduced once in three."""
    try:
        build = json.loads((pdir / "build" / "build.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(build, dict):
        return None
    last = meta.get("lastBuild") if isinstance(meta.get("lastBuild"), dict) else {}
    found = []
    for path in (pdir / "run" / "rehearsal.json", pdir / "build" / "direct" / str(build.get("version")) / "direct-rehearsal.json"):
        try:
            rehearsal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        version = rehearsal.get("releaseVersion") if isinstance(rehearsal, dict) else None
        if version and build.get("version") == version and last.get("version") == version and build.get("sourceHash") == pack_digest(pdir):
            found.append(rehearsal)
    return max(found, key=lambda r: str(r.get("generatedAt") or "")) if found else None


#: How a rehearsal hint's action and because join into one finding message, per rehearsal ``language``
#: (the engine's rehearsal_strings.py writes the hint texts in the pack language plus an English twin).
REHEARSAL_BECAUSE = {"en": "{action} Because: {because}", "zh-CN": "{action}原因：{because}"}


def _hint_message(action: str, because: str, language: str) -> str:
    action, because = action.strip(), because.strip()
    return REHEARSAL_BECAUSE[language].format(action=action, because=because) if because else action


def _rehearsal_findings(rehearsal: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The blocking remediation of a current rehearsal that names a project asset (asset.scopes), as
    numbered repair findings R1..Rn. Environment hints (ops, run, judge: no scopes) stay the SA's.

    ``message`` is in the rehearsal's language (what the SA reads in the repair panel); ``messageEn`` is
    the same instruction in English (a record without English twins repeats ``message``)."""
    out: list[dict[str, Any]] = []
    language = "zh-CN" if (rehearsal or {}).get("language") == "zh-CN" else "en"
    for hint in _dict_items((rehearsal or {}).get("remediation")):
        asset = hint.get("asset") if isinstance(hint.get("asset"), dict) else {}
        scopes = _ordered(asset.get("scopes"))
        if hint.get("severity") != "blocking" or not scopes:
            continue
        action, because = str(hint.get("action") or ""), str(hint.get("because") or "")
        message = _hint_message(action, because, language)
        message_en = _hint_message(str(hint.get("actionEn") or action), str(hint.get("becauseEn") or because), "en") \
            if "actionEn" in hint else message
        row = {"id": f"R{len(out) + 1}", "source": "rehearsal", "code": f"rehearsal.{hint.get('code')}", "severity": "error",
               "message": message[:2000], "messageEn": message_en[:2000], "path": asset.get("file") or asset.get("scenarioPath"),
               "scopes": scopes}
        if hint.get("caseId"):
            row["caseId"] = str(hint["caseId"])
        out.append(row)
    return out


def _selected_findings(validation: dict[str, Any], include_warnings: bool) -> list[dict[str, Any]]:
    repair = validation.get("repair") if isinstance(validation.get("repair"), dict) else {}
    selected = []
    for finding in _dict_items(repair.get("repairable")):
        code = str(finding.get("code") or "")
        if (finding.get("severity") == "error" or include_warnings or code in REPAIR_WARNING_CODES
                or code.startswith("teaching.")):
            selected.append(finding)
    return selected


def _repair_input(*, mode: str, round_: int, findings: list[dict[str, Any]], instructions: str,
                  scopes: list[str], current: dict[str, Any], pdir: Path) -> dict[str, Any]:
    files: dict[str, str] = {}
    omitted: list[dict[str, Any]] = []
    total = 0
    owned = [(rel, text) for rel, owner in _referenced_owners(current).items()
             if owner in scopes and (text := _disk_text(pdir, rel)) is not None]
    for rel, text in sorted(owned, key=lambda pair: (len(pair[1]), pair[0])):
        if len(text) > REPAIR_FILE_CHARS or total + len(text) > REPAIR_FILES_TOTAL_CHARS:
            omitted.append({"path": rel, "sha256": _sha256_text(text), "chars": len(text), "head": text[:300], "readOnly": True})
        else:
            files[rel] = text
            total += len(text)
    locked = [{"type": item_type, "id": key, "provenance": item.get("provenance")}
              for item_type, key, item in _items(current) if item.get("provenance") in FORMAL_PROVENANCE]
    return {
        "round": round_,
        "mode": mode,
        "findings": findings,
        "saInstructions": instructions,
        "allowedScopes": scopes,
        "currentScenario": current,
        "currentFiles": dict(sorted(files.items())),
        "omittedFiles": sorted(omitted, key=lambda row: row["path"]),
        "lockedItems": locked,
    }


def _shrink_repair_files(repair: dict[str, Any]) -> bool:
    """Move the largest current file to omittedFiles (read-only); False when none is left."""
    files = repair["currentFiles"]
    if not files:
        return False
    rel = max(files, key=lambda r: (len(files[r]), r))
    text = files.pop(rel)
    repair["omittedFiles"] = sorted([*repair["omittedFiles"], {"path": rel, "sha256": _sha256_text(text), "chars": len(text),
                                                               "head": text[:300], "readOnly": True}], key=lambda row: row["path"])
    return True


def _check_material_policy(meta: dict[str, Any], materials: list[dict[str, Any]]) -> None:
    """designs[4] D.4: any usable material reaches the model only under a recorded materials policy
    whose data classification is synthetic or internal (never confidential), whoever wrote it (an
    SA-authored file can still carry customer data); customer-authored materials also need the
    approval reference."""
    if not materials:
        return
    policy = meta.get("materialsPolicy") if isinstance(meta.get("materialsPolicy"), dict) else None
    if policy and policy.get("dataClassification") == "confidential":
        raise GenerationConflict("confidential material must not be sent to the Kiro model; upload a sanitized "
                                 "version or set the materials to exclude")
    if not policy or policy.get("dataClassification") not in ("synthetic", "internal"):
        raise GenerationConflict("record the materials approval (who approved sending these materials to the Kiro "
                                 "model, and their data classification: synthetic or internal) before generating")
    if any(m.get("author") != "sa" for m in materials) and not str(policy.get("approvalRef") or "").strip():
        raise GenerationConflict("record the materials approval (who approved sending these customer materials to "
                                 "the Kiro model) before generating")


def _latest_applied(data_dir: Path, project_id: str, *, exclude: str | None = None,
                    meta: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The most recently applied (and not reverted) generation of the project: the one whose state
    the project is in.  Ordered by apply time, then the per-project apply sequence (same-second
    applies), never by round (a fresh draft is round 1 again).  With ``meta``, records of an earlier
    project of the same id (applied before this project was created) are ignored."""
    created = str((meta or {}).get("createdAt") or "")
    applied = [r for r in _project_records(data_dir, project_id)
               if r.get("status") == "applied" and r.get("id") != exclude and str(r.get("appliedAt") or "") >= created]
    return max(applied, key=lambda r: (str(r.get("appliedAt") or ""), int(r.get("applySeq") or 0), str(r.get("id")))) if applied else None


def prepare_generation(data_dir: Path | str, body: Any, *, runner: str = "kirocrew-spawn",
                       settle: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> tuple[dict[str, Any], str]:
    """Validate a generation request and write its queued record; returns (record, task).

    Modes: ``draft`` (a complete pack from the brief, answers and materials), ``repair`` (fix the
    findings of a fresh build/validate.json plus SA instructions, inside the allowed scopes) and
    ``regenerate`` (the whole pack, or only ``scope``).  ``settle`` refreshes a live prior record
    before the one-live-generation check (the route passes the host-aware ``_settle``).
    """
    data_dir = Path(data_dir)
    if not isinstance(body, dict):
        raise GenerationError("request body must be a JSON object")
    if runner not in RUNNERS:
        raise GenerationError(f"unknown generation runner: {runner}")
    mode = str(body.get("mode") or "draft").strip()
    if mode not in MODES:
        raise GenerationError(f"unknown generation mode {mode!r}; use one of {', '.join(MODES)}")
    project_id = str(body.get("projectId") or "").strip()
    brief = str(body.get("brief") or "").strip()
    if len(brief) > MAX_BRIEF_CHARS:
        raise GenerationError(f"customer scenario is limited to {MAX_BRIEF_CHARS} characters")
    answers = _clean_answers(body.get("answers"))
    instructions = str(body.get("instructions") or "").strip()
    if len(instructions) > MAX_INSTRUCTIONS_CHARS:
        raise GenerationError(f"instructions are limited to {MAX_INSTRUCTIONS_CHARS} characters")
    if any(_secret_hit(text) for text in [brief, instructions, *(a["question"] + "\n" + a["answer"] for a in answers)]):
        raise GenerationError("remove credential-shaped material before sending the scenario to Kiro")
    scope = normalise_scopes(body.get("scope"))
    include_warnings = body.get("includeWarnings", False)
    if not isinstance(include_warnings, bool):
        raise GenerationError("includeWarnings must be a boolean")
    include_rehearsal = body.get("includeRehearsal", True)
    if not isinstance(include_rehearsal, bool):
        raise GenerationError("includeRehearsal must be a boolean")
    parent = body.get("parentGenerationId")
    if parent is not None and (not isinstance(parent, str) or not GENERATION_RE.fullmatch(parent)):
        raise GenerationError("parentGenerationId must be a generation id")
    if mode == "draft" and scope is not None:
        raise GenerationError("a draft covers the whole pack; use regenerate with a scope instead")
    staged = body.get("staged", True)
    if not isinstance(staged, bool):
        raise GenerationError("staged must be a boolean")
    staged = staged and mode == "draft"
    pdir = _project_dir(data_dir, project_id)

    with project_lock(pdir):
        meta = _read_meta(pdir)
        effective_brief = brief or stored_brief(pdir).strip()
        scoped = mode == "regenerate" and scope is not None and set(scope) != set(SCOPES)
        budget = material_budget(mode, scoped=scoped)
        records = read_materials(pdir)
        if mode == "draft" and len(effective_brief) < 20 and not any(_usable(r, source_only=True) for r in records):
            raise GenerationError("describe the customer scenario in at least 20 characters, or upload a source material")
        # One live generation per project prevents duplicate token spend and last-writer races.  Stale
        # records are settled first so an unpolled, long-dead record does not block forever.
        for prior in _project_records(data_dir, project_id):
            if prior.get("status") not in LIVE_STATUSES:
                continue
            prior = settle(prior) if settle is not None else _settle(prior, request=None, data_dir=data_dir)
            if prior.get("status") in LIVE_STATUSES:
                raise GenerationConflict("a Kiro generation is already running for this project", prior)
        job = _running_job(pdir)
        if job:  # a one-click / sync job is changing the project: never spend tokens against it
            raise GenerationConflict(f"background job {job} is running for this project; wait for it to finish, then generate")
        scenario_text = (pdir / "scenario.yaml").read_text(encoding="utf-8")
        current = _parse_scenario_text(scenario_text)
        pack_kind = str(meta.get("packKind") or "customer")
        latest = _latest_applied(data_dir, project_id, meta=meta)
        round_ = 1 if mode == "draft" else int((latest or {}).get("round") or 0) + 1

        repair: dict[str, Any] | None = None
        validation_ref = None
        rehearsal_ref = None
        allowed: list[str] | None = None
        if mode in MERGE_MODES:
            findings: list[dict[str, Any]] = []
            validation = None
            if mode == "repair":
                validation = _fresh_validation(pdir)
            else:
                try:
                    validation = _fresh_validation(pdir)
                except GenerationConflict:
                    validation = None  # regenerate does not need a validation; it uses one when fresh
            if validation is not None:
                findings = _selected_findings(validation, include_warnings)
                validation_ref = {"at": validation.get("at"), "scenarioSha256": validation.get("scenarioSha256"),
                                  "packDigest": validation.get("packDigest"), "findingIds": [f.get("id") for f in findings]}
            # A rehearsal of the current release is the live evidence about the pack: its blocking
            # remediation that names a project asset is a repair finding too (includeRehearsal: false opts out).
            rehearsal = current_rehearsal(pdir, meta) if include_rehearsal else None
            from_rehearsal = _rehearsal_findings(rehearsal)
            if scope is not None:
                from_rehearsal = [f for f in from_rehearsal if set(f["scopes"]) & set(scope)]
            if from_rehearsal:
                rehearsal_ref = {"releaseVersion": rehearsal.get("releaseVersion"), "generatedAt": rehearsal.get("generatedAt"),
                                 "verdict": rehearsal.get("verdict"), "findingIds": [f["id"] for f in from_rehearsal]}
            if mode == "repair":
                if not findings and not from_rehearsal and not instructions:
                    raise GenerationConflict("nothing to repair: the validation has no repairable findings, no rehearsal "
                                             "remediation names a project asset, and no instructions were given")
                if instructions and not findings and not from_rehearsal and scope is None:
                    raise GenerationError("instructions without findings need an explicit scope")
                suggested = normalise_scopes((validation.get("repair") or {}).get("suggestedScopes") or []) if validation else []
                union = [s for s in SCOPES if any(s in (f.get("scopes") or []) for f in findings)]
                rehearsal_scopes = {s for f in from_rehearsal for s in f["scopes"]}
                # A live-learned warning blocks the loop's convergence as an error does: a round that leaves its
                # scopes out sends the finding but masks the fix away (review 2026-09-29).
                live_scopes = {s for f in findings if f.get("code") in LIVE_LEARNED_WARNING_CODES for s in f.get("scopes") or []}
                allowed = scope or _ordered(set(suggested or union) | live_scopes | rehearsal_scopes)
                if not allowed:
                    raise GenerationConflict("the findings name no repairable scope; give an explicit scope")
            else:
                allowed = scope if scoped else list(SCOPES)
            findings = findings + from_rehearsal
            repair = _repair_input(mode=mode, round_=round_, findings=findings, instructions=instructions,
                                   scopes=list(allowed), current=current, pdir=pdir)

        common = dict(project_id=project_id, display_name=str(meta.get("displayName") or project_id), pack_kind=pack_kind,
                      customer=str(meta.get("customer") or ""), brief=effective_brief, mode=mode, round_=round_,
                      scoped=scoped, answers=[{"question": a["question"], "answer": a["answer"]} for a in
                                              _answers_to_send(stored_answers(pdir), answers, _utc_now())] or None,
                      language=_normalise_language(current.get("language")) if mode in MERGE_MODES else None,
                      namespace=app_namespace(project_id, current=current, mode=mode))
        scale = 1.0
        for _attempt in range(TASK_SHRINK_STEPS + 1):
            materials = allocate_materials(pdir, budget, scale=scale, records=records)
            if staged:
                task, contracts = _compose_stage_task("structure", context={"common": common, "materials": materials}, outputs={})
            else:
                task, contracts = _compose_task(materials=materials, repair=repair, **common)
            if len(task.encode("utf-8")) <= MAX_TASK_BYTES:
                break
            scale *= 0.8
        while len(task.encode("utf-8")) > MAX_TASK_BYTES and repair is not None and _shrink_repair_files(repair):
            task, contracts = _compose_task(materials=materials, repair=repair, **common)
        task_bytes = len(task.encode("utf-8"))
        if task_bytes > MAX_TASK_BYTES:
            raise GenerationTooLarge(f"generation task is {task_bytes} bytes; the limit is {MAX_TASK_BYTES}")
        # The whole task is data sent to the model: material texts (their upload scan can be stale) and,
        # for repair / regenerate, the project's scenario and files verbatim. Refuse any credential or
        # personal-identifier shape anywhere in it, naming where it sits but never echoing it.
        leaks = _task_secret_sources(materials, repair)
        if leaks or _secret_hit(task):
            raise GenerationError("remove credential-shaped material before sending the scenario to Kiro (a credential or "
                                  "personal identifier is in " + ", ".join(leaks or ["the task"]) + ")")
        _check_material_policy(meta, materials)

        now = _utc_now()
        record = {
            "id": "gen-" + secrets.token_hex(8),
            "projectId": project_id,
            "packKind": pack_kind,
            "mode": mode,
            "scope": list(allowed) if (mode == "repair" or scoped) else None,
            "runner": runner,
            "round": round_,
            "parentGenerationId": parent or ((latest or {}).get("id") if mode in MERGE_MODES else None),
            "status": "queued",
            "sourceScenarioSha256": _sha256_text(scenario_text),
            "sourcePackDigest": pack_digest(pdir),
            "validationRef": validation_ref,
            "rehearsalRef": rehearsal_ref,
            "saInstructions": instructions or None,
            "materialsUsed": [{k: m[k] for k in ("id", "name", "use", "totalChars", "sentChars", "truncated")} for m in materials],
            "materialIds": sorted(m["id"] for m in materials if m["sentChars"] > 0),
            "omittedFiles": [row["path"] for row in (repair or {}).get("omittedFiles", [])],
            "task": {"chars": len(task), "bytes": task_bytes, "estTokens": est_tokens(task), "sha256": _sha256_text(task)},
            "contracts": contracts,
            "timeoutSecs": stage_timeout("structure") if staged else timeout_for(mode),
            "createdAt": now,
            "updatedAt": now,
            "spawnId": None,
            "result": None,
            "error": None,
        }
        if staged:
            record["staged"] = True
            record["stages"] = [_new_stage(name) for name in INITIAL_STAGES]
            record["stages"][0]["task"] = dict(record["task"])
        if brief:
            _atomic_text(pdir / "generation" / "brief.md", brief + "\n")
        if answers:
            _atomic_text(pdir / "generation" / "answers.json",
                         json.dumps(_answers_to_send(stored_answers(pdir), answers, now), indent=2, ensure_ascii=False) + "\n")
        with _LOCK:
            # The new generation supersedes every older ready draft: only the newest draft of a project
            # can be applied, so a stale draft can never overwrite the project under a newer one.
            for prior in _project_records(data_dir, project_id):
                if prior.get("status") == "ready":
                    prior.update(status="superseded", supersededBy=record["id"], supersededAt=now)
                    _write_record(data_dir, prior)
            if staged:
                # What every later stage's task is composed from, so all of them see the brief, answers and
                # materials of this moment (an upload between two stages changes none of them).
                _write_stage_json(_stage_dir(data_dir, record["id"]) / "input.json", {"common": common, "materials": materials})
            _write_record(data_dir, record)
    return record, task


def _stored_record(data_dir: Path, generation_id: str) -> dict[str, Any] | None:
    """The on-disk copy of a record, or None when it was never written or cannot be read."""
    try:
        return _read_record(data_dir, generation_id)
    except GenerationError:
        return None


def _update_live(data_dir: Path, record: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Write ``changes`` into a still-live record.

    A caller may hold a stale in-memory copy (the kiro-cli runner holds its record for the whole
    run).  When another writer already settled the record on disk (abandoned, failed, completed),
    the on-disk record wins: it is returned unchanged and the caller's dict is synced to it.
    """
    with _LOCK:
        stored = _stored_record(data_dir, str(record.get("id")))
        if stored is not None and stored.get("status") not in LIVE_STATUSES:
            record.clear()
            record.update(stored)
            return record
        record.update(changes)
        if record.get("status") not in LIVE_STATUSES and not record.get("completedAt"):
            # The moment Kiro's run ended (ready, needs-input or failed); apply, revert and supersede
            # bump updatedAt later, so elapsed time is measured to this, never to updatedAt.
            record["completedAt"] = _utc_now()
        _write_record(data_dir, record)
        return record


def mark_running(data_dir: Path | str, record: dict[str, Any], *, spawn_id: Any = None,
                 timeout_secs: int | None = None) -> dict[str, Any]:
    """Mark a queued record running; ``timeout_secs`` records the runner's effective timeout so the
    stale rule in ``_settle`` never abandons a run that is still inside its own timeout."""
    changes: dict[str, Any] = {"status": "running", "spawnId": None if spawn_id is None else str(spawn_id)}
    if timeout_secs is not None:
        changes["timeoutSecs"] = int(timeout_secs)
    record = _update_live(Path(data_dir), record, changes)
    if record.get("staged") and spawn_id is not None and record.get("status") in LIVE_STATUSES:
        # The spawn that was started with the task prepare_generation returned runs the structure stage.
        claim_stage(data_dir, str(record["id"]), "structure", spawn_id=spawn_id)
        record.update(_read_record(Path(data_dir), str(record["id"])))
    return record


def fail_generation(data_dir: Path | str, record: dict[str, Any], error: str) -> dict[str, Any]:
    return _update_live(Path(data_dir), record, {"status": "failed", "error": str(error)[:1000]})


def _citable_materials(record: dict[str, Any], current: dict[str, Any], pdir: Path) -> frozenset[str]:
    """Material ids an origin may cite: those sent in this task, plus ids the current pack already
    cites that still exist in the materials index (a repair round keeps earlier citations)."""
    indexed = {str(r.get("id")) for r in read_materials(pdir)}
    cited = {mid for _t, _k, item in _items(current) if isinstance(item.get("origin"), dict)
             for mid in (item["origin"].get("materials") or []) if isinstance(mid, str)}
    return frozenset(set(record.get("materialIds") or []) | (cited & indexed))


def complete_generation(record: dict[str, Any] | str, result_text: str, *, data_dir: Path | str) -> dict[str, Any]:
    """Store Kiro's answer for a live record: ready, needs-input or failed (never raises for bad output).

    If the record was settled on disk meanwhile (for example failed as abandoned by ``_settle``),
    that on-disk record is returned unchanged: a late result never revives it.
    """
    data_dir = Path(data_dir)
    if isinstance(record, str):
        record = _read_record(data_dir, record)
    if record.get("status") not in LIVE_STATUSES:
        raise GenerationError(f"generation {record.get('id')} is {record.get('status')}; it no longer accepts a result")
    stored = _stored_record(data_dir, str(record.get("id")))
    if stored is not None and stored.get("status") not in LIVE_STATUSES:
        return _update_live(data_dir, record, {})
    if (stored or record).get("staged"):
        # One reply of a staged draft: the stage still open (a runner that names none answers the structure).
        settled = finish_stage(data_dir, str(record["id"]), _answered_stage(stored or record), result_text, retries=False)
        record.clear()
        record.update(settled)
        return record
    try:
        text = str(result_text or "")
        if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
            raise GenerationError("Kiro generation result is too large")
        payload = _extract_json(text)
    except GenerationError as exc:
        return _update_live(data_dir, record, {"status": "failed", "error": str(exc)})
    return _update_live(data_dir, record, _bundle_changes(record, payload, data_dir=data_dir))


def _bundle_changes(record: dict[str, Any], payload: dict[str, Any], *, data_dir: Path) -> dict[str, Any]:
    """The record changes for Kiro's parsed answer: ready (with the result), needs-input or failed."""
    changes: dict[str, Any]
    try:
        project_id = str(record["projectId"])
        pdir = _project_dir(data_dir, project_id)
        meta = _read_meta(pdir)
        current = _parse_scenario_text((pdir / "scenario.yaml").read_text(encoding="utf-8"))
        mode = str(record.get("mode") or "draft")
        result = _normalise_bundle(payload, project_id=project_id, pack_kind=str(record.get("packKind")),
                                   current=current, meta=meta, pdir=pdir, mode=mode,
                                   material_ids=_citable_materials(record, current, pdir),
                                   sent_material_ids=frozenset(record.get("materialIds") or []),
                                   scopes=record.get("scope") if isinstance(record.get("scope"), list) else None,
                                   read_only=frozenset(record.get("omittedFiles") or []))
        if result.get("status") == "ready" and payload.get("mode") not in (None, mode):
            result["warnings"].append(f"Kiro answered as mode {payload.get('mode')!r}; the task was {mode!r}")
        if result.get("status") == "ready":
            result["precheck"] = _precheck(result, pdir)
    except GenerationError as exc:
        changes = {"status": "failed", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - untrusted output must fail the record, not the route
        changes = {"status": "failed", "error": f"could not process the Kiro result: {type(exc).__name__}"}
    else:
        result["mode"] = str(record.get("mode") or "draft")
        changes = {"result": result, "error": None,
                   "status": "needs-input" if result["status"] == "needs_input" else "ready"}
    return changes


#: The provenance gate's reason for an item nobody has reviewed yet (engine scenario.provenance_gate): the SA's review.
UNREVIEWED_GATE = re.compile(r"^provenance '(ai_draft|pending)' cannot enter a formal pack")


def _engine_template_commit(module: Any) -> str | None:
    """The pinned Workshop template commit of the checkout the engine was loaded from (template-lock.json)."""
    try:
        lock = _read_json_file(Path(module.__file__).resolve().parents[2] / "template-lock.json") or {}
        return str(lock["template"]["commit"])
    except (KeyError, TypeError, OSError):
        return None


def _precheck(result: dict[str, Any], pdir: Path) -> dict[str, Any] | None:
    """What validation will report once this result is applied, shown before the SA applies it: the scenario loads
    (schema, cross-references), the engine policy runs on it with its files, and the pack compiles (holdout leaks,
    guides, residue), as server.validate does short of rendering the release.  ``review`` counts the items the
    provenance gate holds until the SA reviews them; ``anchorsMissing`` the customer anchors a workshop pack lacks.
    ``partial``: a schema or reference error stopped the later checks."""
    if not isinstance(result.get("scenario"), dict):
        return None
    scenario_mod, validator, compiler = _engine_module("scenario"), _engine_module("validator"), _engine_module("compiler")
    if scenario_mod is None or validator is None:
        return {"ran": False, "error": "engine not found"}

    def summary(findings: list[dict[str, Any]], *, partial: bool = False, warnings: int = 0, loaded: Any = None) -> dict[str, Any]:
        errors = [f for f in findings if f["severity"] == "error"]
        gate = list(getattr(loaded, "gate_violations", None) or [])
        anchors = scenario_mod.provenance_policy(loaded.data).get("anchorsMissing") if loaded is not None else None
        hints = [f for f in findings if f["severity"] != "error"]
        return {"ran": True, "partial": partial, "errorCount": len(errors), "warningCount": warnings,
                "errors": [f"{f['code']}: {f['message']}" for f in errors][:30], "findings": errors[:30], "warnings": hints[:30],
                "review": sum(1 for v in gate if not v.waived and UNREVIEWED_GATE.match(str(v.reason))) if loaded is not None else None,
                "anchorsMissing": anchors or []}

    def rows(source: str, items: list[Any]) -> list[dict[str, Any]]:
        out = []
        for item in items:
            if isinstance(item, str):
                code = "schema" if source == "schema" else source
                out.append({"severity": "error", "code": code, "message": item, "path": item.split(":", 1)[0], "source": source})
            else:
                out.append({"severity": str(item.severity), "code": str(item.code), "message": str(item.message),
                            "path": item.path, "source": source, "scopes": list(getattr(item, "scopes", ()) or ())})
        return out
    try:
        files = dict(result.get("files") or {})
        for rel in _referenced_files(result["scenario"]):
            if rel not in files and _disk_text(pdir, rel) is not None:
                files[rel] = _disk_text(pdir, rel)  # a repair or regenerate carries the project's other files over
        with tempfile.TemporaryDirectory(prefix="wc-precheck-") as tmp:
            root = Path(tmp) / "pack"
            for rel, text in files.items():
                target = root / _safe_relative_file(rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(text), encoding="utf-8")
            root.mkdir(parents=True, exist_ok=True)
            (root / "scenario.yaml").write_text(_dump_scenario(result["scenario"]), encoding="utf-8")
            try:
                loaded = scenario_mod.load_scenario(root / "scenario.yaml", enforce_gate=False)
            except scenario_mod.SchemaViolation as exc:
                return summary(rows("schema", list(exc.errors)), partial=True)
            except scenario_mod.CrossReferenceError as exc:
                return summary(rows("xref", list(exc.errors)), partial=True)
            report = validator.validate_scenario_policy(loaded.data, root=loaded.root)
            findings = rows("policy", list(report.findings))
            commit = _engine_template_commit(scenario_mod)
            if report.ok and compiler is not None and commit:
                try:
                    pack = compiler.compile_pack(loaded, Path(tmp) / "dry", template_commit=commit)
                    findings += rows("output", list(pack.report.findings))
                except validator.PackValidationError as exc:
                    findings += rows("output", list(exc.report.findings))
            warnings = sum(1 for f in findings if f["severity"] != "error")
            return summary(findings, warnings=warnings, loaded=loaded)
    except Exception as exc:  # noqa: BLE001 - the precheck informs; it never fails a result
        return {"ran": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


def _result_text(info: Any) -> str:
    if bool(getattr(info, "result_truncated", False)):
        raw = str(getattr(info, "result_path", "") or "")
        if not raw:
            raise GenerationError("Kiro result was truncated and no full result is available")
        path = Path(raw)
        try:
            if path.stat().st_size > MAX_RESULT_BYTES:
                raise GenerationError("Kiro generation result is too large")
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GenerationError("Kiro full result could not be read") from exc
    text = str(getattr(info, "result", "") or "")
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        raise GenerationError("Kiro generation result is too large")
    return text


# ---------------------------------------------------------------------------
# Staged draft: one Kiro reply per part of the pack
# ---------------------------------------------------------------------------

STAGE_DIRECTIVES = {
    "structure": "Create the structure of a Scenario Pack draft for Workshop Customizer (staged draft, stage 1).",
    "evaluation": "Write the golden set and teaching design of a Scenario Pack draft for Workshop Customizer (staged draft, stage 2).",
    "knowledge": "Write knowledge documents of a Scenario Pack draft for Workshop Customizer (staged draft, stage 3).",
    "prompts": "Write the two system prompts of a Scenario Pack draft for Workshop Customizer (staged draft, stage 3).",
    "skills": "Write the skills of a Scenario Pack draft for Workshop Customizer (staged draft, stage 3).",
    "guides": "Write the guide supplements of a Scenario Pack draft for Workshop Customizer (staged draft, stage 3).",
}
#: The shared contract sections a stage contract may include (the one-shot core includes them all).
STAGE_INCLUDES = frozenset({"provenance", "teaching", "l1", "guides"})
RETRY_NOTE = ("Your previous reply to this stage failed the app's checks in RETRY_INPUT.problems "
              "(RETRY_INPUT.previousOutput is that reply, when it parsed). Fix exactly those problems and return the "
              "whole stage again in the same shape.")


def _stage_base(name: str) -> str:
    return name.split("-", 1)[0]


def _stage_dir(data_dir: Path, generation_id: str) -> Path:
    """``generations/<id>/``: a staged draft's task input, each stage's parsed reply and retry input."""
    return _generation_path(data_dir, generation_id).with_suffix("")


def _stage_path(data_dir: Path, generation_id: str, name: str, kind: str = "output") -> Path:
    if not STAGE_RE.fullmatch(name):
        raise GenerationError(f"unknown draft stage {name!r}")
    return _stage_dir(data_dir, generation_id) / f"{name}.{kind}.json"


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _stage_outputs(data_dir: Path, record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    outputs: dict[str, dict[str, Any]] = {}
    for stage in record.get("stages") or []:
        if stage.get("status") == "done":
            value = _read_json_file(_stage_path(data_dir, str(record["id"]), stage["name"]))
            if value is not None:
                outputs[stage["name"]] = value
    return outputs


def _new_stage(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "deps": list(STAGE_DEPS[_stage_base(name)]), "status": "pending", "attempts": 0,
            "runFailures": 0, "outputRetries": 0, "spawnId": None, "startedAt": None, "completedAt": None, "error": None,
            "problems": [], "task": None, **extra}


def _stage(record: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((s for s in record.get("stages") or [] if s.get("name") == name), None)


def _answered_stage(record: dict[str, Any]) -> str:
    """The stage a runner's reply answers when it names none: the structure until it is done (an older runner or
    the UI harness answers the draft in one reply), then the one stage that is running."""
    structure = _stage(record, "structure")
    if structure is None or structure.get("status") != "done":
        return "structure"
    running = [s["name"] for s in record.get("stages") or [] if s.get("status") == "running"]
    if len(running) != 1:
        raise GenerationError("this staged draft has several stages open; complete one by name")
    return running[0]


def runnable_stages(record: dict[str, Any]) -> list[str]:
    """Stages that can start now: pending, with every stage they need done."""
    if not record.get("staged") or record.get("status") not in LIVE_STATUSES:
        return []
    done = {s["name"] for s in record.get("stages") or [] if s.get("status") == "done"}
    return [s["name"] for s in record.get("stages") or [] if s.get("status") == "pending" and set(s.get("deps") or []) <= done]


def _mutate_staged(data_dir: Path, generation_id: str, change: Callable[[dict[str, Any]], Any]) -> tuple[dict[str, Any], Any]:
    """Read-modify-write a live staged record under the app lock: parallel stages finish into one record."""
    with _LOCK:
        record = _read_record(data_dir, generation_id)
        if record.get("status") not in LIVE_STATUSES:
            return record, None
        value = change(record)
        if record.get("status") not in LIVE_STATUSES and not record.get("completedAt"):
            record["completedAt"] = _utc_now()
        _write_record(data_dir, record)
        return record, value


def claim_stage(data_dir: Path | str, generation_id: str, name: str, *, spawn_id: Any = None) -> dict[str, Any] | None:
    """Mark a runnable stage running for one runner; None when it is not runnable (another runner has it)."""
    def change(record: dict[str, Any]) -> dict[str, Any] | None:
        stage = _stage(record, name)
        if stage is None or name not in runnable_stages(record):
            return None
        stage.update(status="running", attempts=int(stage.get("attempts") or 0) + 1, startedAt=_utc_now(),
                     spawnId=None if spawn_id is None else str(spawn_id), error=None)
        if record.get("status") == "queued":
            record["status"] = "running"
        return dict(stage)
    return _mutate_staged(Path(data_dir), generation_id, change)[1]


def stage_spawned(data_dir: Path | str, generation_id: str, name: str, spawn_id: Any) -> dict[str, Any]:
    """Record the spawn that runs a claimed stage."""
    def change(record: dict[str, Any]) -> None:
        stage = _stage(record, name)
        if stage is not None and stage.get("status") == "running" and not stage.get("spawnId"):
            stage["spawnId"] = str(spawn_id)
    return _mutate_staged(Path(data_dir), generation_id, change)[0]


def stage_task(data_dir: Path | str, record: dict[str, Any], name: str) -> str:
    """The task of one stage, composed from the draft's input and the replies of the stages it needs."""
    data_dir = Path(data_dir)
    generation_id = str(record["id"])
    context = _read_json_file(_stage_dir(data_dir, generation_id) / "input.json")
    if context is None:
        raise GenerationError("this staged draft's input is missing; generate again")
    stage = _stage(record, name)
    if stage is None:
        raise GenerationError(f"this draft has no stage {name!r}")
    retry = _read_json_file(_stage_path(data_dir, generation_id, name, "retry"))
    task, shas = _compose_stage_task(name, context=context, outputs=_stage_outputs(data_dir, record), stage=stage, retry=retry)
    stats = {"chars": len(task), "bytes": len(task.encode("utf-8")), "estTokens": est_tokens(task), "sha256": _sha256_text(task)}
    if stats["bytes"] > MAX_TASK_BYTES:
        raise GenerationTooLarge(f"stage {name} task is {stats['bytes']} bytes; the limit is {MAX_TASK_BYTES}")

    def change(current: dict[str, Any]) -> None:
        live = _stage(current, name)
        if live is not None:
            live["task"] = stats
        current["contracts"] = {**(current.get("contracts") or {}), **{k: v for k, v in shas.items() if v}}
    _mutate_staged(data_dir, generation_id, change)
    return task


def _render_stage_contract(base: str, values: dict[str, str], shared: dict[str, str | None]) -> str:
    path = CONTRACTS_DIR / f"stage-{base}.md"
    if not path.is_file():
        raise GenerationError(f"generation contract stage-{base}.md is missing from the app")
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        match = _INCLUDE_RE.fullmatch(line.rstrip("\n"))
        if match is None:
            lines.append(line)
            continue
        name = match.group(1)
        if name not in STAGE_INCLUDES:
            raise GenerationError(f"stage contract {base} includes an unknown section: {name}")
        text = shared.get(name)
        if text is not None:
            lines.append(text if text.endswith("\n") else text + "\n")
    try:
        return string.Template("".join(lines)).substitute(values)
    except (KeyError, ValueError) as exc:
        raise GenerationError(f"stage contract {base} has an invalid placeholder: {exc}") from exc


def _compose_stage_task(name: str, *, context: dict[str, Any], outputs: dict[str, dict[str, Any]],
                        stage: dict[str, Any] | None = None, retry: dict[str, Any] | None = None,
                        contracts: dict[str, str | None] | None = None) -> tuple[str, dict[str, str | None]]:
    """(task text, {contract name: sha256 | None}) for one stage: the header with the stage, the stage contract
    (``contracts/stage-<stage>.md`` with its shared includes), INPUT_DATA (the brief; the materials only for the
    stages that write from them), STAGE_INPUT (what the earlier stages decided) and, for a second attempt,
    RETRY_INPUT (the app's findings on the first)."""
    base = _stage_base(name)
    common = context.get("common") or {}
    shared = contracts if contracts is not None else load_contracts()
    project_id = str(common.get("project_id") or "")
    display_name = str(common.get("display_name") or project_id)
    pack_kind = str(common.get("pack_kind") or "customer")
    namespace = dict(common["namespace"]) if isinstance(common.get("namespace"), dict) else derive_namespace(project_id)
    structure = (outputs.get("structure") or {}).get("scenario") or {}
    language = (_normalise_language(structure.get("language")) if isinstance(structure, dict) else None) \
        or common.get("language") or detect_language(str(common.get("brief") or ""))
    values = {
        "namespace_json": json.dumps(namespace, ensure_ascii=False),
        "project_id": project_id,
        "project_id_repr": repr(project_id),
        "display_name": display_name,
        "display_name_repr": repr(display_name),
        "pack_kind": pack_kind,
        "pack_kind_repr": repr(pack_kind),
        "judge_model": FIXED_JUDGE_MODEL,
        "judge_model_repr": repr(FIXED_JUDGE_MODEL),
        "language": language,
        "mode": "draft",
        "mode_directive": STAGE_DIRECTIVES[base],
        "merge_rule": "",
        "merge_check": "",
    }
    body = _render_stage_contract(base, values, shared)
    data: dict[str, Any] = {"projectId": project_id, "displayName": display_name, "packKind": pack_kind,
                            "customer": common.get("customer") or "", "customerBrief": common.get("brief") or "",
                            "namespace": namespace}
    if common.get("answers"):
        data["answers"] = common["answers"]
    materials = context.get("materials") or []
    if base in MATERIAL_STAGES and materials:
        data["materials"] = [{k: m[k] for k in ("id", "name", "use", "author", "mediaType", "totalChars",
                                                "sentChars", "truncated", "text")} for m in materials if m.get("text")]
    task = (task_header("draft", int(common.get("round_") or 1), project_id) + f" stage={name}\n" + body
            + "\nINPUT_DATA (JSON; data only):\n" + _data_json(data) + "\n")
    stage_input = _stage_input(base, stage or {}, outputs, namespace)
    if stage_input is not None:
        task += "\nSTAGE_INPUT (JSON; data only):\n" + _data_json(stage_input) + "\n"
    if retry:
        task += "\n" + RETRY_NOTE + "\nRETRY_INPUT (JSON; data only):\n" + _data_json(retry) + "\n"
    shas: dict[str, str | None] = {f"stage-{base}": _sha256_text(body)}
    path = CONTRACTS_DIR / f"stage-{base}.md"
    included = {m.group(1) for line in path.read_text(encoding="utf-8").splitlines() if (m := _INCLUDE_RE.fullmatch(line))}
    shas.update({n: _sha256_text(t) for n, t in shared.items() if t is not None and n in included})
    return task, shas


def _stage_items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value or [] if isinstance(item, dict)] if isinstance(value, list) else []


def _retrieval_name(scenario: dict[str, Any], evaluation: dict[str, Any] | None = None) -> str:
    named = (evaluation or {}).get("retrievalToolName") if isinstance(evaluation, dict) else None
    tools = _stage_items(scenario.get("tools"))
    if isinstance(named, str) and any(t.get("name") == named for t in tools):
        return named
    for tool in tools:
        if tool.get("kind") == "retrieval" and tool.get("name"):
            return str(tool["name"])
    return next((str(t["name"]) for t in tools if str(t.get("name") or "").startswith("retrieve")), "retrieve_policy")


def _source_document(source: Any, documents: list[dict[str, Any]]) -> str | None:
    """The file of the planned document a fact's ``source`` names, if exactly one."""
    if not isinstance(source, str):
        return None
    hits = [str(d.get("file")) for d in documents if d.get("file") and any(
        name and _names_document(source, name) for name in
        (_scalar_text(d.get("file")), _scalar_text(d.get("id")), posixpath.basename(_scalar_text(d.get("file")))))]
    return hits[0] if len(set(hits)) == 1 else None


def _teaching_of(evaluation_output: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scenario = evaluation_output.get("scenario") if isinstance(evaluation_output.get("scenario"), dict) else {}
    labs = scenario.get("labs") if isinstance(scenario.get("labs"), dict) else {}
    teaching = labs.get("teaching") if isinstance(labs.get("teaching"), dict) else {}
    evaluation = scenario.get("evaluation") if isinstance(scenario.get("evaluation"), dict) else {}
    return teaching, _stage_items(evaluation.get("goldenSet"))


def knowledge_requirements(structure: dict[str, Any], evaluation_output: dict[str, Any]) -> dict[str, Any]:
    """What the teaching design puts on the documents: per file, the literal answer terms of its prompt_fixable
    probes and its bait phrases; overall, the absent gap's terms (in no document) and topic."""
    documents = _stage_items((structure.get("knowledge") or {}).get("documents") if isinstance(structure.get("knowledge"), dict) else [])
    noise = [str(d["file"]) for d in documents if d.get("noise") and d.get("file")]
    facts = {str(f.get("id")): f for f in _stage_items(structure.get("facts"))}
    teaching, cases = _teaching_of(evaluation_output)
    by_id = {str(c.get("id")): c for c in cases}
    files: dict[str, dict[str, Any]] = {str(d["file"]): {"noise": bool(d.get("noise")), "mustContain": [], "baitPhrases": []}
                                        for d in documents if d.get("file")}
    absent: list[str] = []
    for phenomenon in _stage_items(teaching.get("phenomena")):
        kind = phenomenon.get("kind")
        if kind == "prompt_fixable":
            for case_id in phenomenon.get("caseIds") or []:
                case = by_id.get(str(case_id)) or {}
                terms = [t for t in ((case.get("expected") or {}).get("mustMention") or []) if isinstance(t, str) and t.strip()]
                sources = [_source_document(facts.get(str(b), {}).get("source"), documents) for b in case.get("basis") or []]
                sources = [s for s in sources if s in files]
                target = next((s for s in sources if s in noise), None) or (sources[0] if sources else (noise[0] if noise else None))
                if target is not None:
                    files[target]["mustContain"] += [t for t in terms if t not in files[target]["mustContain"]]
        elif kind == "retrieval_gap":
            absent += [t for t in phenomenon.get("absentTerms") or [] if isinstance(t, str) and len(t.strip()) >= 2 and t not in absent]
            bait = [t for t in phenomenon.get("baitTerms") or [] if isinstance(t, str) and len(t.strip()) >= 2]
            if bait and noise:
                files[noise[-1]]["baitPhrases"] += [t for t in bait if t not in files[noise[-1]]["baitPhrases"]]
    return {"files": files, "absentTerms": absent}


def _stage_input(base: str, stage: dict[str, Any], outputs: dict[str, dict[str, Any]], namespace: dict[str, str]) -> dict[str, Any] | None:
    """STAGE_INPUT: the slice of the earlier stages' replies a stage writes from.  The file stages never see a
    holdout case: no document, prompt, skill or guide can repeat one."""
    if base == "structure":
        return None
    first = outputs.get("structure") or {}
    scenario = first.get("scenario") if isinstance(first.get("scenario"), dict) else {}
    plan = first.get("plan") if isinstance(first.get("plan"), dict) else {}
    agent = scenario.get("agent") if isinstance(scenario.get("agent"), dict) else {}
    tools = [{k: t[k] for k in ("name", "description", "kind", "inputSchema") if k in t} for t in _stage_items(scenario.get("tools"))]
    if base == "evaluation":
        terms = [t for t in plan.get("absentTerms") or [] if isinstance(t, str) and len(t.strip()) >= 2]
        return {"scenario": scenario, "plan": {**plan, "absentTerms": terms} if "absentTerms" in plan else plan,
                "openQuestions": [q for q in first.get("openQuestions") or [] if isinstance(q, str)]}
    if base == "skills":
        return {"agent": agent, "tools": tools, "retrievalToolName": _retrieval_name(scenario),
                "facts": [{"id": f.get("id"), "statement": f.get("statement")} for f in _stage_items(scenario.get("facts"))],
                "skills": _stage_items(scenario.get("skills"))}
    second = outputs.get("evaluation") or {}
    teaching, cases = _teaching_of(second)
    evaluation = (second.get("scenario") or {}).get("evaluation") if isinstance(second.get("scenario"), dict) else {}
    practice = [c for c in cases if c.get("set") != "holdout"]
    if base == "knowledge":
        requirements = knowledge_requirements(scenario, second)
        write = list(stage.get("write") or [])
        return {"write": write, "agent": {k: agent.get(k) for k in ("audience", "purpose", "scope", "outOfScope")},
                "facts": _stage_items(scenario.get("facts")), "documents": _stage_items((scenario.get("knowledge") or {}).get("documents")),
                "requirements": {f: requirements["files"].get(f, {}) for f in write},
                "absentTopic": plan.get("absentTopic"),
                "absentTerms": requirements["absentTerms"] or [t for t in plan.get("absentTerms") or [] if isinstance(t, str) and len(t.strip()) >= 2]}
    if base == "prompts":
        roles = {str(r.get("id")): r.get("name") for r in _stage_items(agent.get("roles"))}
        designs = {str(c): p.get("design") for p in _stage_items(teaching.get("phenomena")) if p.get("kind") == "refusal" for c in p.get("caseIds") or []}
        refusals = [{"query": c.get("query"), "roleId": c.get("roleId"), "role": roles.get(str(c.get("roleId"))),
                     "forbiddenTools": (c.get("expected") or {}).get("forbiddenTools") or [], "design": designs.get(str(c.get("id")))}
                    for c in practice if (c.get("expected") or {}).get("shouldRefuse")]
        return {"agent": agent, "tools": tools, "toolTargetName": namespace.get("toolTargetName"),
                "retrievalToolName": _retrieval_name(scenario, evaluation), "baselineDefects": _stage_items(teaching.get("baselineDefects")),
                "refusals": refusals, "skills": [s.get("name") for s in _stage_items(scenario.get("skills"))]}
    if base == "guides":
        labs = (second.get("scenario") or {}).get("labs") if isinstance(second.get("scenario"), dict) else {}
        return {"write": list(stage.get("write") or []), "agent": agent, "guide": (labs or {}).get("guide"),
                "teaching": {k: teaching.get(k) for k in ("firstConversation", "baselineDefects", "phenomena")},
                "practiceCases": [{k: c.get(k) for k in ("id", "label", "category")} for c in practice],
                "stepIds": sorted(GUIDE_STEP_IDS)}
    raise GenerationError(f"unknown draft stage {base!r}")


def _file_stages(structure: dict[str, Any], evaluation_output: dict[str, Any]) -> list[dict[str, Any]]:
    """The stages added once the golden set is written: the documents in balanced batches, and the guides when
    labs declares guide files."""
    documents = [str(d["file"]) for d in _stage_items((structure.get("knowledge") or {}).get("documents")) if d.get("file")]
    documents = list(dict.fromkeys(documents))
    count = max(1, math.ceil(len(documents) / KNOWLEDGE_BATCH)) if documents else 0
    stages = [_new_stage(f"knowledge-{i + 1}", write=documents[i::count]) for i in range(count)]
    labs = (evaluation_output.get("scenario") or {}).get("labs") if isinstance(evaluation_output.get("scenario"), dict) else {}
    guides = [str(labs[k]) for k in ("studentGuideFile", "instructorGuideFile") if isinstance(labs, dict) and labs.get(k)]
    if guides:
        stages.append(_new_stage("guides", write=guides))
    return stages


def _whole_pack(payload: dict[str, Any]) -> bool:
    """A reply that is the whole pack at once (an agent or harness answering a draft in one reply)."""
    scenario = payload.get("scenario") if isinstance(payload.get("scenario"), dict) else {}
    evaluation = scenario.get("evaluation") if isinstance(scenario.get("evaluation"), dict) else {}
    return bool(_stage_items(evaluation.get("goldenSet"))) and isinstance(payload.get("files"), dict) and bool(payload["files"])


def _present(term: str, text: str) -> bool:
    return term.casefold() in text.casefold()


#: The engine's live-learned teaching warnings the evaluation stage can fix itself (the role argument of a tool is the
#: structure's, checked there), plus too many practice cases; with every teaching error they ask the stage again.
EVALUATION_TEACHING_CODES = (LIVE_LEARNED_WARNING_CODES - {"teaching.gated_tool_role_argument"}) | {"teaching.too_many_practice"}
MIN_DOCUMENT_CHARS = 500  # the reference packs' documents are 600-1 800 characters; a live staged one was 240
_ENGINE: dict[str, Any] = {}


def _engine_homes() -> list[Path]:
    """Checkouts that may hold the engine, as the process backend finds its home (server.resolve_home)."""
    here = Path(__file__).resolve()
    homes = [Path(os.environ["WORKSHOP_CUSTOMIZER_HOME"])] if os.environ.get("WORKSHOP_CUSTOMIZER_HOME") else []
    homes.append(here.parents[2])  # app/backend/routes.py in the checkout itself
    installed = _read_json_file(here.parents[1] / "installed.json") or {}
    source = str(installed.get("source") or "")
    if source.startswith("registry:"):
        homes.append(here.parents[3] / "app-sources" / source[len("registry:"):])  # ~/.kiro/crew/app-sources/<name>
    elif source:
        homes.append(Path(source).expanduser().parent)
    return homes


def _teaching_policy() -> Any | None:
    """The engine's teaching rules (stdlib only) for the stage checks, or None when no engine is found (the final
    validation still runs them all)."""
    if "policy" in _ENGINE:
        return _ENGINE["policy"]
    policy = None
    try:
        from workshop_customizer import teaching_policy as policy  # type: ignore[no-redef]
    except ImportError:
        for home in _engine_homes():
            engine = home / "engine"
            if (engine / "workshop_customizer" / "teaching_policy.py").is_file():
                sys.path.append(str(engine))
                try:
                    from workshop_customizer import teaching_policy as policy  # type: ignore[no-redef]
                    break
                except ImportError:
                    sys.path.remove(str(engine))
    _ENGINE["policy"] = policy
    return policy


def _engine_module(name: str) -> Any | None:
    """An engine module (``scenario`` needs jsonschema, which the KiroCrew Python has), loaded like the teaching rules."""
    if _teaching_policy() is None:
        return None
    try:
        return importlib.import_module(f"workshop_customizer.{name}")
    except ImportError:
        return None


#: The scenario sections each stage writes: a schema error under another section is not that stage's to fix.
STAGE_SCHEMA_SECTIONS = {"structure": ("description", "language", "agent", "facts", "knowledge", "tools", "skills", "prompts"),
                         "evaluation": ("evaluation", "labs")}
#: The engine's cross-reference errors the golden set or the teaching design causes (the structure's own ids were
#: checked with its reply): the evaluation stage is asked again with them.
EVALUATION_XREF = re.compile(r"golden case|labs\.|evaluation\.|phenomenon|baseline defect|id used by more than one item type")


def _engine_schema_problems(raw: dict[str, Any], base: str, *, project_id: str, pack_kind: str) -> list[str]:
    """The schema errors the final validation would report in the sections this stage wrote (live 2026-10-12: an
    absentTerm of one character, '猫', passed every stage check and failed the schema after Apply). The raw reply is
    canonicalized first, exactly as the assembled draft will be."""
    module = _engine_module("scenario")
    if module is None:
        return []
    try:
        scenario = _canonicalize_scenario(copy.deepcopy(raw), project_id=project_id, pack_kind=pack_kind, canon=_Canon())
        errors = module.validate_schema(scenario)
    except Exception:  # noqa: BLE001 - an odd draft shape is the final validation's to report
        return []
    sections = STAGE_SCHEMA_SECTIONS[base]
    problems = [f"schema: {e}" for e in errors if e.split("/", 1)[0].split(":", 1)[0] in sections]
    if base == "evaluation" and not problems:
        try:
            xref = module.cross_reference_errors(scenario, None, check_files=False)
        except Exception:  # noqa: BLE001
            xref = []
        problems += [f"xref: {e}" for e in xref if EVALUATION_XREF.search(e)]
    return problems


def _engine_teaching_problems(scenario: dict[str, Any]) -> list[str]:
    """The engine's structure-only teaching rules on the structure plus the golden set (no files yet)."""
    policy = _teaching_policy()
    if policy is None:
        return []
    try:
        report = policy.check_teaching(scenario)
    except Exception:  # noqa: BLE001 - an odd draft shape is the final validation's to report
        return []
    return list(dict.fromkeys(f"{f.code}: {f.message}" for f in report.findings
                              if f.code.startswith("teaching.") and (f.severity == "error" or f.code in EVALUATION_TEACHING_CODES)))


def check_stage(name: str, payload: dict[str, Any], *, stage: dict[str, Any], outputs: dict[str, dict[str, Any]],
                project_id: str = "draft", pack_kind: str = "customer") -> list[str]:
    """The app's deterministic checks of one stage's reply (problems for a second attempt; the final validation
    still decides).  They cover what a later stage cannot repair: counts, ids, and the literal links between the
    teaching design and the files."""
    base = _stage_base(name)
    problems: list[str] = []
    scenario = payload.get("scenario") if isinstance(payload.get("scenario"), dict) else None
    if base == "structure":
        if scenario is None:
            return ["the reply has no scenario object"]
        facts, roles = _stage_items(scenario.get("facts")), _stage_items((scenario.get("agent") or {}).get("roles"))
        documents = _stage_items((scenario.get("knowledge") or {}).get("documents"))
        tools, skills = _stage_items(scenario.get("tools")), _stage_items(scenario.get("skills"))
        if not 5 <= len(facts) <= 25:  # the contract asks 8-20; only a clear miss is worth another reply
            problems.append(f"facts has {len(facts)} entries; write 8-20")
        if not roles:
            problems.append("agent.roles is empty")
        if not 3 <= len(documents) <= 12:  # the contract asks 5-9 (the HR reference has 11)
            problems.append(f"knowledge.documents plans {len(documents)} documents; plan 5-9")
        if not any(d.get("noise") for d in documents):
            problems.append("no knowledge document has noise:true; plan 2-3")
        files = [str(d.get("file") or "") for d in documents]
        bad = [f for f in files if not (f.startswith("knowledge-base/docs/") and f.endswith(".md"))]
        if bad:
            problems.append("knowledge document files must be knowledge-base/docs/<name>.md: " + ", ".join(bad or ["<empty>"]))
        retrieval = [t.get("name") for t in tools if t.get("kind") == "retrieval"]
        if len(retrieval) != 1:
            problems.append(f"{len(retrieval)} tools have kind=retrieval; exactly one must")
        for tool in tools:
            name_ = str(tool.get("name") or "")
            if tool.get("kind") != "retrieval" and any(m in name_ for m in RETRIEVAL_SPAN_MARKERS + ("___",)):
                problems.append(f"mock tool name {name_!r} contains retrieve/knowledge/kb_/___")
        for skill in skills:
            if not str(skill.get("file") or "").startswith("skills/"):
                problems.append(f"skill {skill.get('name')!r}: file must be under skills/")
        policy = _teaching_policy()
        for tool in tools if policy is not None else []:
            argument = policy.role_argument(tool) if tool.get("kind") != "retrieval" else None
            if argument:
                problems.append(f"teaching.gated_tool_role_argument: mock tool {tool.get('name')!r} takes the caller's role as "
                                f"argument {argument!r}: the agent fills it in and calls the tool before refusing; drop the "
                                "argument and the fixture branches on it (the access rule goes into the prompts)")
        ids = [str(i.get("id")) for i in facts + documents + roles] + [str(t.get("name")) for t in tools] + [str(s.get("name")) for s in skills]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            problems.append("ids repeat across facts, documents, roles, tools and skills: " + ", ".join(dupes))
        unsourced = [str(f.get("id")) for f in facts if _source_document(f.get("source"), documents) is None]
        if unsourced and len(unsourced) == len(facts):
            problems.append("no fact's source names a planned document; give each fact the document that will state it")
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
        terms = plan.get("absentTerms") if isinstance(plan.get("absentTerms"), list) else []
        usable = [t for t in terms if isinstance(t, str) and len(t.strip()) >= 2]
        if terms and not usable:  # the gap's absentTerms are schema strings of 2+ characters; a shorter one is dropped
            problems.append(f"plan.absentTerms {terms}: write 1-3 concept words of at least 2 characters each "
                            "(a single character breaks the schema and matches inside unrelated words)")
        problems += [p for p in _engine_schema_problems(scenario, "structure", project_id=project_id, pack_kind=pack_kind)
                     if p not in problems]
        return problems
    if base == "evaluation":
        if scenario is None:
            return ["the reply has no scenario object"]
        first = (outputs.get("structure") or {}).get("scenario") or {}
        evaluation = scenario.get("evaluation") if isinstance(scenario.get("evaluation"), dict) else {}
        labs = scenario.get("labs") if isinstance(scenario.get("labs"), dict) else {}
        teaching = labs.get("teaching") if isinstance(labs.get("teaching"), dict) else None
        cases = _stage_items(evaluation.get("goldenSet"))
        counts = {c: sum(1 for x in cases if x.get("category") == c) for c in ("normal", "boundary", "prohibited")}
        holdout = sum(1 for x in cases if x.get("set") == "holdout")
        if len(cases) < 12 or min(counts.values()) < 3:
            problems.append(f"S1: {len(cases)} golden cases ({counts}); write >= 12 with >= 3 per category")
        if holdout < math.ceil(len(cases) / 3):
            problems.append(f"S1: {holdout} holdout of {len(cases)} cases; holdout must be >= {math.ceil(len(cases) / 3)}")
        fact_ids = {str(f.get("id")) for f in _stage_items(first.get("facts"))}
        role_ids = {str(r.get("id")) for r in _stage_items((first.get("agent") or {}).get("roles"))}
        tool_names = {str(t.get("name")) for t in _stage_items(first.get("tools"))}
        for case in cases:
            cid = case.get("id")
            wrong = [b for b in case.get("basis") or [] if str(b) not in fact_ids]
            if wrong or not case.get("basis"):
                problems.append(f"S2: case {cid}: basis {wrong or '[]'} must be fact ids of STAGE_INPUT")
            if str(case.get("roleId")) not in role_ids:
                problems.append(f"S3: case {cid}: roleId {case.get('roleId')!r} is not a role id")
            expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
            unknown = [t for t in (expected.get("requiredTools") or []) + (expected.get("forbiddenTools") or []) if str(t) not in tool_names]
            if unknown:
                problems.append(f"S3: case {cid}: tools {unknown} are not tools of STAGE_INPUT")
        if teaching is None:
            problems.append("labs.teaching is missing")
            return problems
        practice = {str(c.get("id")) for c in cases if c.get("set") != "holdout"}
        retrieval = _retrieval_name(first, evaluation)
        by_id = {str(c.get("id")): c for c in cases}
        phenomena = _stage_items(teaching.get("phenomena"))
        probes: set[str] = set()
        for p in phenomena:
            for cid in p.get("caseIds") or []:
                if str(cid) not in practice:
                    problems.append(f"T1: phenomenon {p.get('id')}: case {cid} is not a practice case")
            if p.get("kind") in ("prompt_fixable", "retrieval_gap"):
                probes.update(str(c) for c in p.get("caseIds") or [])
        for cid in sorted(probes & practice):
            tools = ((by_id.get(cid) or {}).get("expected") or {}).get("requiredTools")
            if tools != [retrieval]:
                problems.append(f"T1: probe {cid}: requiredTools must be exactly [{retrieval!r}]")
        gaps = [p for p in phenomena if p.get("kind") == "retrieval_gap" and p.get("mechanism") == "absent"]
        if len(gaps) != 1:
            problems.append(f"{len(gaps)} retrieval_gap phenomena with mechanism absent; declare exactly 1")
        fixable = {str(c) for p in phenomena if p.get("kind") == "prompt_fixable" for c in p.get("caseIds") or []}
        if str(teaching.get("stabilityCaseId")) not in fixable:
            problems.append("T1: stabilityCaseId must be a prompt_fixable probe")
        used = {str(d) for p in phenomena if p.get("kind") in ("prompt_fixable", "retrieval_gap") for d in p.get("defectIds") or []}
        for defect in _stage_items(teaching.get("baselineDefects")):
            if str(defect.get("id")) not in used:
                problems.append(f"T2: baselineDefect {defect.get('id')} is in no prompt_fixable or retrieval_gap defectIds")
            if not str(defect.get("candidateFix") or "").strip():
                problems.append(f"baselineDefect {defect.get('id')}: candidateFix is empty")
            elif defect.get("candidateFix") == defect.get("baselineMarker"):
                problems.append(f"T3: baselineDefect {defect.get('id')}: candidateFix and baselineMarker are the same sentence")
        for key in ("studentGuideFile", "instructorGuideFile"):
            if labs.get(key) and not str(labs[key]).startswith("guides/"):
                problems.append(f"labs.{key} must be under guides/")
        merged = {**copy.deepcopy(first), "evaluation": evaluation, "labs": labs}
        schema = _engine_schema_problems(merged, "evaluation", project_id=project_id, pack_kind=pack_kind)
        problems += [p for p in schema + _engine_teaching_problems(merged) if p not in problems]
        return problems
    files = payload.get("files")
    if not isinstance(files, dict):
        return ["the reply has no files object"]
    first = (outputs.get("structure") or {}).get("scenario") or {}
    if base in ("knowledge", "guides"):
        expected_files = list(stage.get("write") or [])
    elif base == "prompts":
        prompts = first.get("prompts") if isinstance(first.get("prompts"), dict) else {}
        expected_files = [str(prompts.get("baselineFile") or "agent/baseline-prompt.md"),
                          str(prompts.get("optimizationCandidateFile") or "agent/optimization-candidate.md")]
    else:
        expected_files = [str(s.get("file")) for s in _stage_items(first.get("skills")) if s.get("file")]
    missing = [f for f in expected_files if f not in files]
    extra = [f for f in files if f not in expected_files]
    if missing:
        problems.append("files is missing: " + ", ".join(missing))
    if extra:
        problems.append("files has paths this stage does not write: " + ", ".join(extra))
    texts = {path: value for path, value in files.items() if isinstance(value, str)}
    for path, value in files.items():
        if not (isinstance(value, str) and value.strip()) and not (base == "knowledge" and isinstance(value, dict) and value.get("fromMaterial")):
            problems.append(f"{path}: the content is empty")
    if base == "knowledge":
        requirements = knowledge_requirements(first, outputs.get("evaluation") or {})
        for path in expected_files:
            text = texts.get(path)
            if text is None:
                continue
            lacking = [t for t in requirements["files"].get(path, {}).get("mustContain", []) if not _present(t, text)]
            if lacking:
                problems.append(f"K3: {path} lacks the literal probe answer terms {lacking}")
            leaked = [t for t in requirements["absentTerms"] if _present(t, text)]
            if leaked:
                problems.append(f"K3: {path} contains absentTerms {leaked}; no document may contain them")
            if len(text.strip()) < MIN_DOCUMENT_CHARS:
                problems.append(f"K1: {path} is {len(text.strip())} characters; write a complete document (900-2000 characters)")
    elif base == "prompts" and len(expected_files) == 2:
        baseline, candidate = texts.get(expected_files[0], ""), texts.get(expected_files[1], "")
        teaching, _cases = _teaching_of(outputs.get("evaluation") or {})
        for defect in _stage_items(teaching.get("baselineDefects")):
            fix, marker = str(defect.get("candidateFix") or ""), str(defect.get("baselineMarker") or "")
            if fix and fix not in candidate:
                problems.append(f"P1: candidateFix of {defect.get('id')} is not in the candidate verbatim: {fix!r}")
            if fix and fix in baseline:
                problems.append(f"P1: candidateFix of {defect.get('id')} also occurs in the baseline")
            if marker and marker not in baseline:
                problems.append(f"P1: baselineMarker of {defect.get('id')} is not in the baseline verbatim: {marker!r}")
            if marker and marker in candidate:
                problems.append(f"P1: baselineMarker of {defect.get('id')} also occurs in the candidate")
        retrieval = _retrieval_name(first, ((outputs.get("evaluation") or {}).get("scenario") or {}).get("evaluation"))
        target = str((first.get("namespace") or {}).get("toolTargetName") or "") if isinstance(first.get("namespace"), dict) else ""
        for label, text in (("baseline", baseline), ("candidate", candidate)):
            if text and retrieval not in text and not (target and target in text):
                problems.append(f"P2: the {label} names neither the retrieval tool {retrieval} nor the tool target {target}")
    return problems


def _write_stage_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")


def stage_failed(data_dir: Path | str, generation_id: str, name: str, error: str, *, kind: str = "run",
                 retry: dict[str, Any] | None = None) -> dict[str, Any]:
    """A stage's run (``kind="run"``: Kiro did not answer) or reply (``"output"``) failed: it runs again while that
    kind has retries left (``retry`` is what the next task tells Kiro about the last reply), otherwise the draft
    fails."""
    data_dir = Path(data_dir)
    earlier = _read_json_file(_stage_path(data_dir, generation_id, name, "retry")) or {}
    rerun = earlier.get("kind") == "precheck" and isinstance(earlier.get("previousOutput"), dict)
    # a rewrite of a usable first reply (it failed only the stage's checks) whose runs keep failing keeps that reply
    first = (kind == "run" and earlier.get("kind", "output") == "output" and isinstance(earlier.get("previousOutput"), dict)
             and isinstance(earlier.get("problems"), list))
    if retry is not None and not rerun:
        _write_stage_json(_stage_path(data_dir, generation_id, name, "retry"), retry)
    counter, budget = ("outputRetries", STAGE_OUTPUT_RETRIES) if kind == "output" else ("runFailures", STAGE_RUN_RETRIES)
    fell_back: list[str] = []

    def change(record: dict[str, Any]) -> None:
        stage = _stage(record, name)
        if stage is None or stage.get("status") not in ("pending", "running"):
            return
        stage[counter] = int(stage.get(counter) or 0) + 1
        # Why each earlier attempt was not kept: the page shows it next to the stage after it is done.
        entry: dict[str, Any] = {"kind": kind, "error": str(error)[:600], "at": _utc_now(), "attempt": int(stage.get("attempts") or 0)}
        if isinstance((retry or {}).get("problems"), list):
            entry["problems"] = [str(p)[:600] for p in retry["problems"][:12]]  # one per check (a message may hold "; ")
        stage["history"] = (list(stage.get("history") or []) + [entry])[-6:]
        if stage[counter] <= budget:
            stage.update(status="pending", error=str(error)[:1000], spawnId=None)
        elif rerun:  # its output file still holds the reply the precheck rerun was asked to redo
            stage.update(status="done", completedAt=_utc_now(), error=None, kept="before-precheck",
                         problems=[str(p) for p in earlier.get("keptProblems") or []][:20])
            fell_back.append("before-precheck")
        elif first and int(stage.get("outputRetries") or 0) >= STAGE_OUTPUT_RETRIES:
            stage.update(status="done", completedAt=_utc_now(), error=None, kept="first", keptAttempt=1,
                         problems=[str(p) for p in earlier["problems"]][:20])
            fell_back.append("first")
        else:
            stage.update(status="failed", error=str(error)[:1000], completedAt=_utc_now())
            record.update(status="failed", error=f"draft stage {name} failed: {error}"[:1000])
    record = _mutate_staged(data_dir, generation_id, change)[0]
    if fell_back == ["first"]:
        _write_stage_json(_stage_path(data_dir, generation_id, name), earlier["previousOutput"])
        if name == "evaluation":  # the file stages come with the golden set, as when the reply is accepted
            added = _file_stages((_stage_outputs(data_dir, record).get("structure") or {}).get("scenario") or {}, earlier["previousOutput"])

            def add(current: dict[str, Any]) -> None:
                names = {x["name"] for x in current["stages"]}
                current["stages"] += [x for x in added if x["name"] not in names]
            record = _mutate_staged(data_dir, generation_id, add)[0]
    if fell_back and record.get("status") in LIVE_STATUSES and all(s.get("status") == "done" for s in record.get("stages") or []):
        return _complete_staged(data_dir, record)
    return record


def _problem_rank(problems: list[str]) -> tuple[int, int, int]:
    """How bad a reply's problems are: those that block the build (the scenario does not load, an engine teaching
    error), then the stage's own checks, then the engine's teaching warnings and the slips the app fixes itself (an
    unknown roleId becomes the first role).  A count alone kept a schema error over two id slips."""
    def tier(problem: str) -> int:
        if problem.startswith(("schema:", "xref:", "the reply has no", "the reply could not", "files is missing")):
            return 0  # the scenario does not load, or the assembly misses a file (live: a rewrite dropped a document)
        if problem.startswith("teaching."):
            return 2 if problem.split(":", 1)[0] in EVALUATION_TEACHING_CODES else 0
        return 2 if re.match(r"S3: case \S+: roleId ", problem) else 1
    tiers = [tier(str(p)) for p in problems]
    return tiers.count(0), tiers.count(1), tiers.count(2)


def _kept_attempt(data_dir: Path, generation_id: str, name: str, stage: dict[str, Any]) -> tuple[dict[str, Any], list[str], str] | None:
    """The usable earlier reply of a stage whose last reply is unusable: the one the precheck rerun was asked to
    redo, or the first reply once the stage's quality retry is spent."""
    earlier = _read_json_file(_stage_path(data_dir, generation_id, name, "retry")) or {}
    previous = earlier.get("previousOutput")
    if not isinstance(previous, dict):
        return None
    if earlier.get("kind") == "precheck":
        return previous, [str(p) for p in earlier.get("keptProblems") or []], "before-precheck"
    if int(stage.get("outputRetries") or 0) >= STAGE_OUTPUT_RETRIES:
        return previous, [str(p) for p in earlier.get("problems") or []], "first"
    return None


def finish_stage(data_dir: Path | str, generation_id: str, name: str, result_text: str, *,
                 retries: bool = True) -> dict[str, Any]:
    """Store one stage's reply.  A reply that fails the stage's checks is asked once more with its problems; of the
    two replies the less bad one is kept, and an unusable rewrite never discards a usable first reply.  The last
    stage to finish assembles the draft, which then completes like a one-reply draft.  ``retries=False`` (a runner
    that answers through ``complete_generation`` and never runs a stage again) fails the draft instead."""
    data_dir = Path(data_dir)
    record = _read_record(data_dir, generation_id)
    stage = _stage(record, name)
    if record.get("status") not in LIVE_STATUSES or stage is None or stage.get("status") not in ("pending", "running"):
        return record
    text = str(result_text or "")
    try:
        if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
            raise GenerationError("Kiro generation result is too large")
        payload = _extract_json(text)
    except GenerationError as exc:
        if not retries:
            return fail_generation(data_dir, record, str(exc))
        kept = _kept_attempt(data_dir, generation_id, name, stage)
        if kept is not None:  # the rewrite is unusable (cut off, no JSON): the earlier usable reply stands
            return _accept_stage(data_dir, generation_id, name, kept[0], kept[1], kept=kept[2])
        return stage_failed(data_dir, generation_id, name, str(exc), kind="output", retry={"kind": "output", "problems": [str(exc)]})
    if name == "structure" and (_whole_pack(payload) or payload.get("status") == "needs_input"):
        return _finish_staged(data_dir, generation_id, payload, [])
    if not retries:
        return fail_generation(data_dir, record, "Kiro answered one stage of a staged draft, and this runner runs no "
                                                 "further stages: start the draft from the app, or run it with routes.drive_stages")
    outputs = _stage_outputs(data_dir, record)
    try:
        problems = check_stage(name, payload, stage=stage, outputs=outputs, project_id=str(record.get("projectId")),
                               pack_kind=str(record.get("packKind") or "customer"))
    except Exception as exc:  # noqa: BLE001 - an odd reply must reach the retry, never break the runner
        problems = [f"the reply could not be checked: {type(exc).__name__}"]
    earlier = _read_json_file(_stage_path(data_dir, generation_id, name, "retry")) or {}
    if earlier.get("kind") == "precheck" and isinstance(earlier.get("previousOutput"), dict):
        if problems:  # the once-per-draft precheck rerun is best effort: a reply its own checks refuse keeps the earlier one
            return _accept_stage(data_dir, generation_id, name, earlier["previousOutput"],
                                 [str(p) for p in earlier.get("keptProblems") or []], kept="before-precheck")
        return _accept_stage(data_dir, generation_id, name, payload, [])
    if problems and int(stage.get("outputRetries") or 0) < STAGE_OUTPUT_RETRIES:
        return stage_failed(data_dir, generation_id, name, "; ".join(problems)[:1000], kind="output",
                            retry={"kind": "output", "problems": problems, "previousOutput": payload})
    kept = None
    if (problems and earlier.get("kind", "output") == "output" and isinstance(earlier.get("previousOutput"), dict)
            and isinstance(earlier.get("problems"), list) and _problem_rank([str(p) for p in earlier["problems"]]) < _problem_rank(problems)):
        payload, problems, kept = earlier["previousOutput"], [str(p) for p in earlier["problems"]], "first"
    return _accept_stage(data_dir, generation_id, name, payload, problems, kept=kept)


def _accept_stage(data_dir: Path, generation_id: str, name: str, payload: dict[str, Any], problems: list[str], *,
                  kept: str | None = None) -> dict[str, Any]:
    """A stage's reply is final (``kept`` says when it is an earlier reply than the last one): store it, add the file
    stages once the golden set is written, and assemble the draft when every stage is done."""
    record = _read_record(data_dir, generation_id)
    outputs = _stage_outputs(data_dir, record)
    _write_stage_json(_stage_path(data_dir, generation_id, name), payload)
    added = _file_stages((outputs.get("structure") or {}).get("scenario") or {}, payload) if name == "evaluation" else []

    def change(current: dict[str, Any]) -> bool:
        live = _stage(current, name)
        if live is None or live.get("status") not in ("pending", "running"):
            return False
        attempt = int(live.get("attempts") or 0)
        kept_attempt = 1 if kept == "first" else live.get("keptAttempt") if kept == "before-precheck" else attempt
        live.update(status="done", completedAt=_utc_now(), problems=problems[:20], error=None, kept=kept, keptAttempt=kept_attempt)
        names = {s["name"] for s in current["stages"]}
        current["stages"] += [s for s in added if s["name"] not in names]
        return all(s.get("status") == "done" for s in current["stages"])
    record, complete = _mutate_staged(data_dir, generation_id, change)
    if not complete:
        return record
    return _complete_staged(data_dir, record)


def _complete_staged(data_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Every stage is done: the assembled draft, after at most one precheck rerun of the file stages it blames.  A
    rerun that leaves the precheck with more errors than before is undone (its stages keep the replies it reran)."""
    payload, warnings = _assemble_staged(data_dir, record)
    changes = _bundle_changes(record, payload, data_dir=data_dir)
    after = int(((changes.get("result") or {}).get("precheck") or {}).get("errorCount") or 0)
    if record.get("precheckRedo") and after > int(record.get("precheckBefore") or 0):
        gid = str(record["id"])
        kept: dict[str, tuple[list[str], Any]] = {}
        for name in record["precheckRedo"]:
            earlier = _read_json_file(_stage_path(data_dir, gid, name, "retry")) or {}
            if earlier.get("kind") == "precheck" and isinstance(earlier.get("previousOutput"), dict):
                _write_stage_json(_stage_path(data_dir, gid, name), earlier["previousOutput"])
                kept[name] = ([str(p) for p in earlier.get("keptProblems") or []], earlier.get("keptAttempt"))

        def undo(current: dict[str, Any]) -> None:
            current["precheckUndone"] = True
            for stage in current.get("stages") or []:
                if stage.get("name") in kept:
                    problems, attempt = kept[stage["name"]]
                    stage.update(kept="before-precheck", problems=problems[:20], keptAttempt=attempt or stage.get("keptAttempt"))
        record = _mutate_staged(data_dir, gid, undo)[0]
        payload, warnings = _assemble_staged(data_dir, record)
        changes = _bundle_changes(record, payload, data_dir=data_dir)
        return _finish_staged(data_dir, gid, payload, warnings, changes=changes)
    redo = _precheck_redo(data_dir, record, changes)
    return redo if redo is not None else _finish_staged(data_dir, str(record["id"]), payload, warnings, changes=changes)


#: Precheck errors without a file in their message that a file stage owns: the prompts', and the noise documents'.
PRECHECK_PROMPT_CODES = frozenset({"teaching.defect_fix_missing", "teaching.defect_already_fixed", "teaching.defect_marker_missing",
                                   "teaching.defect_marker_kept"})
PRECHECK_NOISE_CODES = frozenset({"teaching.bait_missing", "teaching.fixable_answer_not_in_kb"})
#: ... and the ones the structure or the golden set owns even when they name a document (no file stage can fix them).
PRECHECK_STRUCTURE_CODES = frozenset({"teaching.noise_doc_flag", "teaching.noise_disabled", "teaching.missing_kind"})


def _precheck_redo(data_dir: Path, record: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any] | None:
    """Once per draft: a precheck error in a file one file stage wrote (a document, a prompt, a skill, a guide) asks
    that stage again with the error.  An error in the structure or the golden set stays on the draft for the SA."""
    if record.get("precheckRedo"):
        return None
    findings = [f for f in ((changes.get("result") or {}).get("precheck") or {}).get("findings") or [] if isinstance(f, dict)]
    outputs = _stage_outputs(data_dir, record)
    owner = {str(path): s["name"] for s in record.get("stages") or [] if _stage_base(s["name"]) in FILE_STAGES
             for path in ((outputs.get(s["name"]) or {}).get("files") or {})}
    structure = (outputs.get("structure") or {}).get("scenario") or {}
    documents = _stage_items((structure.get("knowledge") or {}).get("documents"))
    noise_stages = [owner[str(d.get("file"))] for d in documents if d.get("noise") and str(d.get("file")) in owner]
    redo: dict[str, list[str]] = {}
    for finding in findings:
        text = f"{finding.get('path') or ''} {finding.get('message') or ''}"
        named = [] if str(finding.get("code")) in PRECHECK_STRUCTURE_CODES else [
            owner[str(d.get("file"))] for d in documents if str(d.get("file")) in owner and d.get("id") and f"'{d.get('id')}'" in text]
        stages = [name for path, name in owner.items() if path in text] + named
        if not stages and str(finding.get("code")) in PRECHECK_PROMPT_CODES:
            stages = [name for name in set(owner.values()) if _stage_base(name) == "prompts"]
        elif not stages and str(finding.get("code")) in PRECHECK_NOISE_CODES:
            stages = noise_stages  # the bait phrases and the probe answers live in the noise documents
        for name in dict.fromkeys(stages):
            redo.setdefault(name, []).append(f"precheck {finding.get('code')}: {finding.get('message')}"[:600])
    if not redo:
        return None
    kept = {s["name"]: (list(s.get("problems") or []), s.get("keptAttempt")) for s in record.get("stages") or []}
    for name, problems in redo.items():
        _write_stage_json(_stage_path(data_dir, str(record["id"]), name, "retry"),
                          {"kind": "precheck", "problems": problems, "previousOutput": outputs.get(name),
                           "keptProblems": kept.get(name, ([], None))[0], "keptAttempt": kept.get(name, ([], None))[1]})

    before = int(((changes.get("result") or {}).get("precheck") or {}).get("errorCount") or 0)

    def change(current: dict[str, Any]) -> None:
        current["precheckRedo"] = sorted(redo)
        current["precheckBefore"] = before
        for stage in current.get("stages") or []:
            if stage.get("name") in redo and stage.get("status") == "done":
                entry = {"kind": "precheck", "error": "; ".join(redo[stage["name"]])[:600], "problems": redo[stage["name"]][:12], "at": _utc_now(),
                         "attempt": int(stage.get("attempts") or 0)}
                stage["history"] = (list(stage.get("history") or []) + [entry])[-6:]
                stage.update(status="pending", spawnId=None, completedAt=None, error=None, problems=[])
    return _mutate_staged(data_dir, str(record["id"]), change)[0]


def _assemble_staged(data_dir: Path, record: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The draft the stages wrote, in the one-reply shape ``_normalise_bundle`` takes."""
    outputs = _stage_outputs(data_dir, record)
    first, second = outputs.get("structure") or {}, outputs.get("evaluation") or {}
    scenario = copy.deepcopy(first.get("scenario") or {})
    written = second.get("scenario") if isinstance(second.get("scenario"), dict) else {}
    for key in ("evaluation", "labs"):
        if key in written:
            scenario[key] = written[key]
    files: dict[str, Any] = {}
    for stage in record.get("stages") or []:
        if _stage_base(stage["name"]) in FILE_STAGES and isinstance((outputs.get(stage["name"]) or {}).get("files"), dict):
            files.update(outputs[stage["name"]]["files"])
    questions = [q for q in (first.get("openQuestions") or []) + (second.get("openQuestions") or []) if isinstance(q, str)]
    payload = {"status": "ready", "summary": first.get("summary"), "openQuestions": list(dict.fromkeys(questions)),
               "truthLedger": first.get("truthLedger") or [], "anchorCandidates": second.get("anchorCandidates"),
               "scenario": scenario, "files": files}
    warnings = [f"draft stage {s['name']}: {p}" for s in record.get("stages") or [] for p in s.get("problems") or []]
    return payload, warnings


def _finish_staged(data_dir: Path, generation_id: str, payload: dict[str, Any], warnings: list[str], *,
                   changes: dict[str, Any] | None = None) -> dict[str, Any]:
    record = _read_record(data_dir, generation_id)
    if record.get("status") not in LIVE_STATUSES:
        return record
    changes = copy.deepcopy(changes) if changes is not None else _bundle_changes(record, payload, data_dir=data_dir)
    if warnings and isinstance(changes.get("result"), dict):
        changes["result"]["warnings"] = (warnings + list(changes["result"].get("warnings") or []))[:100]
    stages = copy.deepcopy(record.get("stages") or [])
    for stage in stages:
        if stage.get("status") in ("pending", "running"):
            stage["status"] = "skipped"  # the structure reply was the whole pack, or asked for input
    changes["stages"] = stages
    return _update_live(data_dir, record, changes)


def drive_stages(data_dir: Path | str, record: dict[str, Any], run: Callable[[str, str], str], *,
                 workers: int = 4) -> dict[str, Any]:
    """Run a staged draft to its end with ``run(stage, task) -> reply text`` (the local kiro-cli), stages that do
    not need each other in parallel.  ``run`` raises when Kiro could not answer (timeout, no output); that is a
    failed attempt like an unusable reply.  Returns the settled record."""
    import concurrent.futures

    data_dir = Path(data_dir)
    generation_id = str(record["id"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"stage-{generation_id}") as pool:
        running: dict[Any, str] = {}
        while True:
            current = _read_record(data_dir, generation_id)
            if current.get("status") not in LIVE_STATUSES:
                break
            for name in runnable_stages(current):
                if claim_stage(data_dir, generation_id, name) is None:
                    continue
                try:
                    task = stage_task(data_dir, _read_record(data_dir, generation_id), name)
                except GenerationError as exc:
                    fail_generation(data_dir, _read_record(data_dir, generation_id), f"draft stage {name}: {exc}")
                    break
                running[pool.submit(run, name, task)] = name
            if not running:
                break
            done, _pending = concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                name = running.pop(future)
                try:
                    reply = future.result()
                except Exception as exc:  # noqa: BLE001 - recorded on the stage; the runner's own message
                    stage_failed(data_dir, generation_id, name, f"{type(exc).__name__}: {exc}"[:1000])
                    continue
                finish_stage(data_dir, generation_id, name, reply)
    settled = _read_record(data_dir, generation_id)
    if settled.get("status") in LIVE_STATUSES:  # nothing left to run, yet not finished: never leave it live
        open_ = ", ".join(s["name"] for s in settled.get("stages") or [] if s.get("status") != "done")
        settled = fail_generation(data_dir, settled, f"the staged draft stopped with stages open: {open_}")
    record.clear()
    record.update(settled)
    return record


def _settle_staged(record: dict[str, Any], *, request: Any, data_dir: Path) -> dict[str, Any]:
    """Settle each running stage of a spawned staged draft from the gateway's spawns, as ``_settle`` does a
    one-reply draft: done -> its reply, failed or timed out -> a failed attempt."""
    manager = _spawn_manager(request)
    generation_id = str(record["id"])
    now = time.time()
    for stage in list(record.get("stages") or []):
        if stage.get("status") != "running":
            continue
        name = stage["name"]
        elapsed = now - _epoch(str(stage.get("startedAt") or record.get("createdAt") or ""))
        spawn_id = str(stage.get("spawnId") or "")
        if not spawn_id:
            if elapsed > STAGE_CLAIM_GRACE_SECS:
                stage_failed(data_dir, generation_id, name, "the stage's Kiro run never started")
            continue
        info = manager.get(spawn_id) if manager is not None else None
        if info is None:
            if elapsed > stage_timeout(name):
                stage_failed(data_dir, generation_id, name, "Kiro generation timed out or its result expired")
            continue
        if str(getattr(info, "app", "") or APP_NAME) != APP_NAME:
            fail_generation(data_dir, _read_record(data_dir, generation_id), "spawn ownership mismatch")
            break
        error = str(getattr(info, "error", "") or "")
        if error:
            stage_failed(data_dir, generation_id, name, f"Kiro generation failed: {error[:1000]}")
        elif not bool(getattr(info, "done", False)):
            if elapsed > stage_timeout(name):
                stage_failed(data_dir, generation_id, name, "Kiro generation timed out")
        else:
            try:
                text = _result_text(info)
            except GenerationError as exc:
                stage_failed(data_dir, generation_id, name, str(exc))
                continue
            finish_stage(data_dir, generation_id, name, text)
    return _read_record(data_dir, generation_id)


def _spawn_manager(request: Any) -> Any:
    """The gateway's ``SubagentManager`` (its spawns), reached through ``request.app``; None outside KiroCrew."""
    app = getattr(request, "app", None) if request is not None else None
    state = app.get("state") if app is not None else None
    return getattr(state, "subagents", None) if state is not None else None


def _settle(record: dict[str, Any], *, request: Any = None, data_dir: Path) -> dict[str, Any]:
    if record.get("status") not in LIVE_STATUSES:
        return record
    timeout = _record_timeout(record)
    now = time.time()
    if record.get("runner") == "kiro-cli":
        # Driven by a local kiro-cli process that reports its own result; fail it only once abandoned.
        last = _epoch(str(record.get("updatedAt") or record.get("createdAt") or ""))
        if now - last > timeout + KIRO_CLI_STALE_GRACE_SECS:
            fail_generation(data_dir, record, "Kiro CLI generation was abandoned: no result arrived in time")
        return record
    if record.get("staged"):
        return _settle_staged(record, request=request, data_dir=data_dir)
    elapsed = now - _epoch(str(record.get("createdAt") or ""))
    manager = _spawn_manager(request)
    info = manager.get(str(record.get("spawnId") or "")) if manager is not None else None
    if info is None:
        if elapsed > timeout:
            fail_generation(data_dir, record, "Kiro generation timed out or its result expired")
        return record
    if str(getattr(info, "app", "") or APP_NAME) != APP_NAME:
        return fail_generation(data_dir, record, "spawn ownership mismatch")
    error = str(getattr(info, "error", "") or "")
    if error:
        return fail_generation(data_dir, record, f"Kiro generation failed: {error[:1000]}")
    if not bool(getattr(info, "done", False)):
        if elapsed > timeout:
            fail_generation(data_dir, record, "Kiro generation timed out")
        return record
    try:
        text = _result_text(info)
    except GenerationError as exc:
        return fail_generation(data_dir, record, str(exc))
    return complete_generation(record, text, data_dir=data_dir)


def _require_applicable(record: dict[str, Any]) -> None:
    if record.get("status") == "superseded":
        newer = record.get("supersededBy") or "a newer generation"
        raise GenerationError(f"a newer draft exists for this project ({newer}); apply the newest draft or generate again")
    if record.get("status") != "ready" or not isinstance(record.get("result"), dict):
        raise GenerationError("generation is not ready to apply")


def _snapshot_dir(pdir: Path, generation_id: str) -> Path:
    return pdir / "generation" / "snapshots" / generation_id


def _prune_snapshots(pdir: Path, keep: int = SNAPSHOT_KEEP) -> list[str]:
    """Keep the newest ``keep`` snapshots (recorded time, then write time, then id); returns the removed ids.

    ``at`` has second resolution, so two snapshots of the same second are ordered by when they were written:
    the generation id is a content hash and would order them arbitrarily, dropping the newest.
    """
    root = pdir / "generation" / "snapshots"
    rows = []
    for path in root.iterdir() if root.is_dir() else ():
        try:
            at = str(json.loads((path / "snapshot.json").read_text(encoding="utf-8")).get("at") or "")
        except (OSError, json.JSONDecodeError, AttributeError):
            at = ""
        try:
            written = (path / "snapshot.json").stat().st_mtime_ns
        except OSError:
            written = 0
        rows.append((at, written, path.name, path))
    rows.sort(reverse=True)
    removed = []
    for _at, _written, name, path in rows[keep:]:
        shutil.rmtree(path, ignore_errors=True)
        removed.append(name)
    return removed


def _remove_empty_dirs(pdir: Path, rels: list[str]) -> None:
    """Remove directories left empty by deletions (up to, never including, the project root)."""
    root = pdir.resolve()
    for rel in sorted(rels, key=lambda r: -r.count("/")):
        parent = (pdir / rel).parent.resolve()
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


#: App-owned files of a project, never written through a pack path.
APP_MANAGED_FILES = frozenset({"project.json", "scenario.yaml"})


def pack_path(pdir: Path, rel: str) -> Path:
    """The resolved path of a pack-side file (prompts, documents, skills, guides) of the project.

    The ONE confinement rule for every write of pack content (``server.Service._pack_file`` for the
    editor, generation apply, its snapshot and revert): the path must stay inside the project, and
    app-managed state (:data:`APP_MANAGED_DIRS`, :data:`APP_MANAGED_FILES`) is refused however the path
    spells it. Letter case never matters (on macOS APFS 'Run/' IS 'run/') and the check runs on the
    resolved path too, so a symlink inside the project (agent -> run) cannot reach it either.
    """
    root = pdir.resolve()
    target = (pdir / rel).resolve()
    if root not in target.parents:
        raise GenerationError(f"path must stay inside the project: {rel}")
    managed_dirs = {name.casefold() for name in APP_MANAGED_DIRS}
    for parts in (Path(rel).parts, target.relative_to(root).parts):
        if not parts or parts[0].casefold() in managed_dirs or parts[-1].casefold() in APP_MANAGED_FILES:
            raise GenerationError(f"that path is managed by the app: {rel}")
    return target


def _apply_path(pdir: Path, rel: str) -> Path:
    """A path generation apply snapshots, writes or reverts: the project's own scenario.yaml (never
    through a link), else a pack file (:func:`pack_path`)."""
    if rel == "scenario.yaml":
        target = (pdir / rel).resolve()
        if target != pdir.resolve() / "scenario.yaml":
            raise GenerationError("scenario.yaml must be the project's own file, not a link")
        return target
    return pack_path(pdir, rel)


def _write_snapshot(pdir: Path, generation_id: str, rels: list[str], *, scenario_sha: str, digest: str) -> Path:
    """Copy the current bytes (or record absence) of every path apply will touch."""
    sources = [(rel, _apply_path(pdir, rel)) for rel in rels]  # every path is confined before anything is written
    root = _snapshot_dir(pdir, generation_id)
    if root.exists():
        shutil.rmtree(root)
    entries = []
    for rel, source in sources:
        existed = source.is_file()
        entry: dict[str, Any] = {"rel": rel, "existed": existed, "sha256Before": None}
        if existed:
            data = source.read_bytes()
            entry["sha256Before"] = hashlib.sha256(data).hexdigest()
            target = root / "files" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        entries.append(entry)
    root.mkdir(parents=True, exist_ok=True)
    _atomic_json(root / "snapshot.json", {"schema": "workshop-customizer/snapshot/1", "generationId": generation_id,
                                          "at": _utc_now(), "scenarioShaBefore": scenario_sha, "packDigestBefore": digest,
                                          "entries": entries, "scenarioShaAfter": None, "packDigestAfter": None})
    return root


def _last_generation(record: dict[str, Any], *, at: str | None = None) -> dict[str, Any]:
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    return {
        "id": record["id"],
        "mode": str(record.get("mode") or "draft"),
        "round": int(record.get("round") or 1),
        "at": at or record.get("appliedAt"),
        "summary": result.get("summary", ""),
        "files": len(result.get("files") or {}),
        "deleted": len(record.get("deleted") or []),
    }


def _apply_record(record: dict[str, Any], *, data_dir: Path, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply a ready generation: write changed files, delete orphans, write scenario.yaml (YAML), all
    behind a snapshot that ``revert_generation`` restores; rolls every change back on failure."""
    body = body if body is not None else {}
    if body.get("acknowledged") is not True:
        raise GenerationError("acknowledged must be true: confirm you reviewed the draft, its file changes and deletions")
    caller = record
    _require_applicable(caller)
    project_id = str(caller.get("projectId") or "")
    pdir = _project_dir(data_dir, project_id)
    with project_lock(pdir), _LOCK:  # lock order: the project flock, then the in-process lock
        # The on-disk record is the authority: an in-memory copy may predate a newer generation
        # that superseded it.  (It is still untrusted content; _verify_bundle checks it below.)
        record = _read_record(data_dir, str(caller.get("id")))
        _require_applicable(record)
        if str(record.get("projectId")) != project_id:
            raise GenerationError("generation record belongs to another project")
        scenario_path = pdir / "scenario.yaml"
        meta_path = pdir / "project.json"
        current_text = scenario_path.read_text(encoding="utf-8")
        if _sha256_text(current_text) != record.get("sourceScenarioSha256"):
            raise GenerationError("project changed after generation started (a review recorded, the pack kind switched or a "
                                  "file saved); generate again before applying")
        source_digest = record.get("sourcePackDigest")
        if source_digest and pack_digest(pdir) != source_digest:
            raise GenerationError("project files changed after generation started; generate again before applying")
        job = _running_job(pdir)
        if job:
            raise GenerationError(f"background job {job} is running for this project; wait for it to finish, then apply")
        meta = _read_meta(pdir)
        result = record["result"]
        # The record is on disk and must not be trusted blindly: verify it at the write boundary.
        files = _verify_bundle(record, result, pdir=pdir, current=_parse_scenario_text(current_text), meta=meta)
        reset = ((result.get("diff") or {}).get("provenance") or {}).get("reset", []) if isinstance(result.get("diff"), dict) else []
        confirmed_reset = [row for row in reset if isinstance(row, dict) and row.get("was") == "customer_confirmed"]
        if confirmed_reset and body.get("acknowledgeConfirmationResets") is not True:
            raise GenerationConflict(
                "this draft resets customer confirmations ("
                + ", ".join(f"{r.get('type')} {r.get('id')}" for r in confirmed_reset[:10])
                + "); apply with acknowledgeConfirmationResets=true to accept that")
        # The acknowledgement (the SA saw deleteOrphans) is what deletes them.
        delete = sorted(result.get("deleteOrphans") or []) if isinstance(result.get("deleteOrphans"), list) else []
        write = {rel: text for rel, text in files.items() if _disk_text(pdir, rel) != text}
        scenario_text = _dump_scenario(result["scenario"])
        snapshot = _write_snapshot(pdir, record["id"], sorted({*write, *delete, "scenario.yaml"}),
                                   scenario_sha=_sha256_text(current_text), digest=str(source_digest or pack_digest(pdir)))
        old: dict[Path, bytes | None] = {}

        def remember(path: Path) -> None:
            if path not in old:
                old[path] = path.read_bytes() if path.is_file() else None

        try:
            for rel, content in write.items():
                target = _apply_path(pdir, rel)
                remember(target)
                _atomic_text(target, content)
            for rel in delete:
                target = _apply_path(pdir, rel)
                remember(target)
                target.unlink(missing_ok=True)
            _remove_empty_dirs(pdir, delete)
            remember(scenario_path)
            _atomic_text(scenario_path, scenario_text)
            remember(meta_path)
            now = _utc_now()
            meta.setdefault("createdFromTemplate", meta.get("template"))
            record_after = {**record, "deleted": delete}
            meta.update(
                status="truth-review",
                template="kiro-generated",
                lastValidation=None,
                lastBuild=None,
                lastCalibration=None,
                generatedFiles=sorted(files),
                lastGeneration=_last_generation(record_after, at=now),
                updatedAt=now,
            )
            apply_seq = bump_edit_log(meta, "kiroApplies")["kiroApplies"]
            _atomic_json(meta_path, meta)
            manifest = json.loads((snapshot / "snapshot.json").read_text(encoding="utf-8"))
            manifest.update(scenarioShaAfter=_sha256_text(scenario_text), packDigestAfter=pack_digest(pdir))
            _atomic_json(snapshot / "snapshot.json", manifest)
        except Exception:
            for path, previous in reversed(list(old.items())):
                try:
                    if previous is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        tmp = path.with_name(path.name + ".rollback")
                        tmp.write_bytes(previous)
                        os.replace(tmp, path)
                except OSError:
                    pass
            shutil.rmtree(snapshot, ignore_errors=True)
            raise
        record.update(status="applied", appliedAt=now, applySeq=apply_seq, deleted=delete, orphansKept=[],
                      snapshot=snapshot.relative_to(pdir).as_posix(), acknowledgement="explicit")
        _write_record(data_dir, record)
        _prune_snapshots(pdir)
    if caller is not record:
        caller.clear()
        caller.update(record)
    return {
        "applied": True,
        "generationId": record["id"],
        "projectId": project_id,
        "files": len(files),
        "written": sorted(write),
        "deleted": delete,
        "orphansKept": [],
        "untracked": list(result.get("untracked") or []),
        "confirmationsReset": reset,
        "snapshot": record["snapshot"],
        "status": "truth-review",
    }


def apply_generation(record: dict[str, Any] | str, data_dir: Path | str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply a ready generation (record or id) to its project; raises GenerationError on refusal.

    ``body`` must carry ``acknowledged: true``; a draft that resets customer confirmations also needs
    ``acknowledgeConfirmationResets: true``.
    """
    data_dir = Path(data_dir)
    if isinstance(record, str):
        record = _read_record(data_dir, record)
    if body is not None and not isinstance(body, dict):
        raise GenerationError("apply body must be a JSON object")
    return _apply_record(record, data_dir=data_dir, body=body)


def revert_generation(record: dict[str, Any] | str, data_dir: Path | str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Restore the exact pre-apply bytes of an applied generation from its snapshot.

    Refused unless the project is still exactly as that apply left it (scenario sha and pack digest
    equal the snapshot's post-apply values), so a revert never discards a later edit.
    """
    data_dir = Path(data_dir)
    body = body if body is not None else {}
    if not isinstance(body, dict) or body.get("acknowledged") is not True:
        raise GenerationError("acknowledged must be true: confirm the project returns to its state before this apply")
    generation_id = record if isinstance(record, str) else str(record.get("id"))
    stored = _read_record(data_dir, generation_id)
    pdir = _project_dir(data_dir, str(stored.get("projectId") or ""))
    with project_lock(pdir), _LOCK:  # lock order: the project flock, then the in-process lock
        stored = _read_record(data_dir, generation_id)
        if stored.get("status") != "applied":
            raise GenerationError(f"generation {generation_id} is {stored.get('status')}; only an applied generation can be reverted")
        root = _snapshot_dir(pdir, generation_id)
        try:
            manifest = json.loads((root / "snapshot.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GenerationError("the snapshot of this apply is no longer kept; revert refused") from exc
        scenario_sha = _sha256_text((pdir / "scenario.yaml").read_text(encoding="utf-8"))
        if scenario_sha != manifest.get("scenarioShaAfter") or pack_digest(pdir) != manifest.get("packDigestAfter"):
            raise GenerationError("project changed after apply; revert refused")
        job = _running_job(pdir)
        if job:
            raise GenerationError(f"background job {job} is running for this project; wait for it to finish, then revert")
        entries = [e for e in manifest.get("entries") or [] if isinstance(e, dict)]
        payloads: dict[str, bytes | None] = {}
        for entry in entries:
            rel = str(entry.get("rel") or "")
            _apply_path(pdir, rel)
            if entry.get("existed"):
                data = (root / "files" / rel).read_bytes()
                if hashlib.sha256(data).hexdigest() != entry.get("sha256Before"):
                    raise GenerationError(f"snapshot file {rel} is damaged; revert refused")
                payloads[rel] = data
            else:
                payloads[rel] = None
        meta_path = pdir / "project.json"
        old: dict[Path, bytes | None] = {meta_path: meta_path.read_bytes()}
        restored, removed = [], []
        try:
            for rel, data in payloads.items():
                target = _apply_path(pdir, rel)
                old.setdefault(target, target.read_bytes() if target.is_file() else None)
                if data is None:
                    target.unlink(missing_ok=True)
                    removed.append(rel)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_name(target.name + ".tmp")
                    tmp.write_bytes(data)
                    os.replace(tmp, target)
                    restored.append(rel)
            _remove_empty_dirs(pdir, removed)
            meta = _read_meta(pdir)
            previous = _latest_applied(data_dir, str(stored["projectId"]), exclude=generation_id, meta=meta)
            meta.update(status="truth-review", lastValidation=None, lastBuild=None,
                        lastGeneration=_last_generation(previous) if previous else None, updatedAt=_utc_now())
            bump_edit_log(meta, "kiroReverts")
            _atomic_json(meta_path, meta)
        except Exception:
            for path, previous_bytes in old.items():
                try:
                    if previous_bytes is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(previous_bytes)
                except OSError:
                    pass
            raise
        stored.update(status="reverted", revertedAt=_utc_now())
        _write_record(data_dir, stored)
    if not isinstance(record, str):
        record.clear()
        record.update(stored)
    return {"reverted": True, "generationId": generation_id, "projectId": stored["projectId"],
            "restored": sorted(restored), "removed": sorted(removed), "status": "truth-review"}


async def _off_loop(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run blocking project work (``project_lock`` retries ``flock`` for up to LOCK_TIMEOUT_SECS) in a
    worker thread, so the KiroCrew gateway's event loop never stalls while another writer holds
    ``generation/.lock``."""
    import asyncio
    import functools

    return await asyncio.get_running_loop().run_in_executor(None, functools.partial(func, *args, **kwargs))


def start_refused(exc: Exception) -> tuple[int, dict[str, Any]]:
    """The status and body for a start ``prepare_generation`` refused (or whose body did not parse): 409 with the live
    generation, 413, else 400."""
    if isinstance(exc, GenerationConflict):
        payload: dict[str, Any] = {"error": str(exc)}
        if exc.record is not None:
            payload["generation"] = _public_record(exc.record)
        return 409, payload
    if isinstance(exc, GenerationTooLarge):
        return 413, {"error": str(exc)}
    return 400, {"error": str(exc)}


def _with_stale(public: dict[str, Any], record: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    """``stale``: a ready result whose project changed after the generation started (a review recorded, the pack
    kind switched, a file saved), which apply refuses; the page says so before the SA tries. ``revertable``: an
    applied result the project still matches exactly (revert's own check), so the page offers undo only then."""
    if public.get("status") == "applied":
        try:
            pdir = _project_dir(data_dir, str(record.get("projectId") or ""))
            manifest = json.loads((_snapshot_dir(pdir, str(record.get("id"))) / "snapshot.json").read_text(encoding="utf-8"))
            public["revertable"] = (_sha256_text((pdir / "scenario.yaml").read_text(encoding="utf-8")) == manifest.get("scenarioShaAfter")
                                    and pack_digest(pdir) == manifest.get("packDigestAfter"))
        except (GenerationError, OSError, ValueError):
            public["revertable"] = False
        return public
    if public.get("status") != "ready":
        return public
    try:
        pdir = _project_dir(data_dir, str(record.get("projectId") or ""))
        text = (pdir / "scenario.yaml").read_text(encoding="utf-8")
        digest = record.get("sourcePackDigest")
        public["stale"] = _sha256_text(text) != record.get("sourceScenarioSha256") or bool(digest and pack_digest(pdir) != digest)
    except (GenerationError, OSError):
        pass
    return public


def generation_route(method: str, parts: list[str], body: dict[str, Any], data_dir: Path, *,
                     start: Callable[[dict[str, Any]], tuple[int, Any]] | None = None,
                     request: Any = None) -> tuple[int, Any]:
    """One generation route, synchronously, as ``(status, payload)``: ``parts`` is the path after the App's prefix
    (``["generations", ...]``). The KiroCrew routes below, the standalone console's ``KiroRunner`` and the UI harness
    all serve through it; only the start differs (``start(body)``: KiroCrew's spawn, the local kiro-cli, a simulated
    answer). Latest and poll settle (``request`` lets ``_settle`` read the gateway's spawns), apply settles then
    applies, revert restores; a refusal is 404 on a GET, 409 on a POST."""
    try:
        if parts == ["generations"] and method == "POST" and start is not None:
            return start(body)
        if len(parts) == 3 and parts[:2] == ["generations", "latest"] and method == "GET":
            _project_dir(data_dir, parts[2])
            candidates = _project_records(data_dir, parts[2])
            if not candidates:
                return 200, {"generation": None}
            record = max(candidates, key=lambda item: str(item.get("createdAt") or ""))
            return 200, {"generation": _with_stale(_public_record(_settle(record, request=request, data_dir=data_dir)), record, data_dir)}
        if len(parts) == 2 and parts[0] == "generations" and method == "GET":
            record = _read_record(data_dir, parts[1])
            return 200, _with_stale(_public_record(_settle(record, request=request, data_dir=data_dir)), record, data_dir)
        if len(parts) == 3 and parts[0] == "generations" and parts[2] == "apply" and method == "POST":
            record = _settle(_read_record(data_dir, parts[1]), request=request, data_dir=data_dir)
            return 200, apply_generation(record, data_dir, body)
        if len(parts) == 3 and parts[0] == "generations" and parts[2] == "revert" and method == "POST":
            return 200, revert_generation(parts[1], data_dir, body)
    except GenerationError as exc:
        return (404 if method == "GET" else 409), {"error": str(exc)}
    return 404, {"error": "no such generation route"}


class _Gateway:
    """What ``_settle`` needs of a request (its ``app``, for the gateway's spawns), kept by a background watcher."""

    def __init__(self, app: Any):
        self.app = app


def register_routes(ctx: Any):
    """KiroCrew App hook contract: return the model-backed route table (its sync work is :func:`generation_route`)."""
    import asyncio

    from aiohttp import web
    from kiro_crew.apps.route_registry import AppRoute

    watches: dict[str, Any] = {}  # generation id -> its one watch task (also keeps it from being garbage-collected)

    async def run_ends(gateway: Any, spawn_ids: list[str], deadline: float, seen: set[str]) -> None:
        """Return when one of the spawns' runs ends, or by ``deadline``. The gateway keeps each running spawn's asyncio
        task in ``SubagentManager._tasks`` (private, KiroCrew 0.8): waiting on it wakes the moment Kiro finishes. A spawn
        with none to wait on (still queued, between a run and its continuation, or a KiroCrew without that map) is
        checked again after WATCH_INTERVAL_SECS instead."""
        tasks = getattr(_spawn_manager(gateway), "_tasks", None)
        loop = asyncio.get_running_loop()
        known = {s: run for s, run in ((s, tasks.get(s)) for s in spawn_ids) if isinstance(run, asyncio.Future)
                 and run.get_loop() is loop} if isinstance(tasks, dict) else {}
        ended = {s for s, run in known.items() if run.done()} - seen
        seen.update(ended)
        if ended:
            return  # a run ended before this wait began: settle it now (once; then it is checked on the interval)
        runs = {run for run in known.values() if not run.done()}
        left = max(0.0, deadline - time.time())
        if not runs:
            await asyncio.sleep(min(WATCH_INTERVAL_SECS, left))
            return
        # asyncio.wait, unlike awaiting a run, never cancels it when the watch stops.
        await asyncio.wait(runs, timeout=left if len(runs) == len(spawn_ids) else min(WATCH_INTERVAL_SECS, left),
                           return_when=asyncio.FIRST_COMPLETED)

    async def start_stages(data_dir: Path, generation_id: str, app_ctx: Any) -> None:
        """Spawn every stage of a staged draft that can run now (each claimed first, so only one runner starts it)."""
        record = await _off_loop(_read_record, data_dir, generation_id)
        if record.get("status") != "running":
            return
        for name in runnable_stages(record):
            if await _off_loop(claim_stage, data_dir, generation_id, name) is None:
                continue
            try:
                task = await _off_loop(stage_task, data_dir, await _off_loop(_read_record, data_dir, generation_id), name)
            except GenerationError as exc:
                current = await _off_loop(_read_record, data_dir, generation_id)
                await _off_loop(fail_generation, data_dir, current, f"draft stage {name}: {exc}")
                return
            try:
                spawn_id = await app_ctx.spawn.run(task=task, agent=AGENT_NAME, silent=True)
            except Exception as exc:  # noqa: BLE001 - a declined spawn is a failed attempt of the stage
                await _off_loop(stage_failed, data_dir, generation_id, name, f"KiroCrew declined the stage: {str(exc)[:500]}")
                continue
            await _off_loop(stage_spawned, data_dir, generation_id, name, spawn_id)

    async def watch(data_dir: Path, generation_id: str, gateway: Any, app_ctx: Any) -> None:
        """Settle a spawned generation as soon as Kiro finishes, not when the page next polls: KiroCrew prunes a
        finished spawn's result.txt an hour after it ends, so a page left closed that long found nothing to read.
        A staged draft's watch also spawns each stage once the stages it needs are done. ``_settle`` fails a run
        once its timeout passes, which ends the watch."""
        seen: set[str] = set()  # spawns whose ended run was already settled once
        try:
            while True:
                record = await _off_loop(_read_record, data_dir, generation_id)
                if record.get("status") not in LIVE_STATUSES:
                    return
                if record.get("staged"):
                    await start_stages(data_dir, generation_id, app_ctx)
                    record = await _off_loop(_read_record, data_dir, generation_id)
                    running = [s for s in record.get("stages") or [] if s.get("status") == "running" and s.get("spawnId")]
                    spawn_ids = [str(s["spawnId"]) for s in running]
                    deadline = min((_epoch(str(s.get("startedAt") or "")) + stage_timeout(s["name"]) + 1 for s in running),
                                   default=time.time() + WATCH_INTERVAL_SECS)
                else:
                    spawn_ids = [str(record.get("spawnId") or "")]
                    deadline = _epoch(str(record.get("createdAt") or "")) + _record_timeout(record) + 1
                await run_ends(gateway, spawn_ids, deadline, seen)
                record = await _off_loop(_read_record, data_dir, generation_id)
                await _off_loop(_settle, record, request=gateway, data_dir=data_dir)
        except Exception:  # noqa: BLE001 - the page's poll still settles; the gateway must not see this
            app_ctx.logger.exception("watching Kiro generation %s failed", generation_id)

    def ensure_watch(data_dir: Path, generation_id: str, request: Any, app_ctx: Any) -> None:
        """Start the generation's watch unless one is running (a poll restarts it after a gateway restart)."""
        current = watches.get(generation_id)
        if current is not None and not current.done():
            return
        # ``_settle`` reads the spawns from ``request.app``; keep only that past this request.
        task = asyncio.ensure_future(watch(data_dir, generation_id, _Gateway(request.app), app_ctx))
        watches[generation_id] = task
        task.add_done_callback(lambda done: watches.pop(generation_id, None) if watches.get(generation_id) is done else None)

    def keep_driving(reply: tuple[int, Any], request: Any, app_ctx: Any) -> tuple[int, Any]:
        """A poll of a live staged draft makes sure its watch runs: it is what starts the next stages."""
        body = reply[1].get("generation") if isinstance(reply[1], dict) and "generation" in reply[1] else reply[1]
        if (isinstance(body, dict) and body.get("staged") and body.get("status") == "running" and body.get("id")
                and app_ctx.spawn is not None):
            ensure_watch(Path(app_ctx.data_dir), str(body["id"]), request, app_ctx)
        return reply

    async def start(request: Any, app_ctx: Any):
        data_dir = Path(app_ctx.data_dir)
        try:
            if app_ctx.spawn is None:
                raise GenerationError("Kiro generation is unavailable: this App was not granted spawn permission")
            if request.content_length and request.content_length > MAX_REQUEST_BYTES:
                raise GenerationError("request is too large")
            body = await request.json()
            record, task = await _off_loop(
                prepare_generation, data_dir, body, runner="kirocrew-spawn",
                settle=lambda prior: _settle(prior, request=request, data_dir=data_dir),
            )
        except (GenerationError, json.JSONDecodeError) as exc:
            status, payload = start_refused(exc)
            return web.json_response(payload, status=status)
        try:
            spawn_id = await app_ctx.spawn.run(task=task, agent=AGENT_NAME, silent=True)
        except Exception as exc:
            fail_generation(data_dir, record, f"KiroCrew declined generation: {str(exc)[:1000]}")
            return web.json_response(_public_record(record), status=503)
        mark_running(data_dir, record, spawn_id=spawn_id)
        ensure_watch(data_dir, str(record["id"]), request, app_ctx)
        return web.json_response(_public_record(record), status=202)

    def answer(reply: tuple[int, Any]) -> Any:
        return web.json_response(reply[1], status=reply[0])

    async def latest(request: Any, app_ctx: Any):
        parts = ["generations", "latest", str(request.match_info["project_id"])]
        return answer(keep_driving(generation_route("GET", parts, {}, Path(app_ctx.data_dir), request=request), request, app_ctx))

    async def poll(request: Any, app_ctx: Any):
        parts = ["generations", str(request.match_info["generation_id"])]
        return answer(keep_driving(generation_route("GET", parts, {}, Path(app_ctx.data_dir), request=request), request, app_ctx))

    async def _body(request: Any) -> dict[str, Any]:
        if request.content_length and request.content_length > MAX_REQUEST_BYTES:
            raise GenerationError("request is too large")
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = None
        if body is None:
            return {}
        if not isinstance(body, dict):
            raise GenerationError("request body must be a JSON object")
        return body

    async def apply(request: Any, app_ctx: Any):
        try:
            body = await _body(request)
            parts = ["generations", str(request.match_info["generation_id"]), "apply"]
            return answer(await _off_loop(generation_route, "POST", parts, body, Path(app_ctx.data_dir), request=request))
        except GenerationError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except Exception as exc:  # fail closed; no traceback or model text crosses the API
            app_ctx.logger.exception("failed to apply Kiro generation")
            return web.json_response({"error": f"could not apply generated draft: {type(exc).__name__}"}, status=500)

    async def revert(request: Any, app_ctx: Any):
        try:
            body = await _body(request)
            parts = ["generations", str(request.match_info["generation_id"]), "revert"]
            return answer(await _off_loop(generation_route, "POST", parts, body, Path(app_ctx.data_dir), request=request))
        except GenerationError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except Exception as exc:  # fail closed
            app_ctx.logger.exception("failed to revert Kiro generation")
            return web.json_response({"error": f"could not revert the generation: {type(exc).__name__}"}, status=500)

    return [
        AppRoute("POST", "/generations", start),
        AppRoute("GET", "/generations/latest/{project_id}", latest),
        AppRoute("GET", "/generations/{generation_id}", poll),
        AppRoute("POST", "/generations/{generation_id}/apply", apply),
        AppRoute("POST", "/generations/{generation_id}/revert", revert),
    ]
