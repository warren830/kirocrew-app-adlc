"""Golden render: the reference packs compile and render byte-for-byte as pinned.

Phase P0 (schema additions, validation seam, routes/script-facts refactors) did not change what a
pack without labs.teaching compiles or renders to; P1 re-pinned the packs once when they gained
labs.teaching and its content. Two pins guard release output:

* ``PINNED``: the release version (content hash of every release file) and the sha256 of the
  compiled pack's checksum table (student pack and instructor bundle);
* ``tests/fixtures/golden-render/<pack>.json``: the sha256 of every release file, of RELEASE.json
  and the applied patch list, captured before the P0d script-facts refactor. A mismatch names the
  files that changed.

Update a pin only in a commit that intentionally changes release output, and say so in that commit
message. ``WSC_UPDATE_GOLDENS=1`` rewrites the fixture files from the current render.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from workshop_customizer import compiler, render
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((REPO_ROOT / "template-lock.json").read_text(encoding="utf-8"))
TEMPLATE_COMMIT = LOCK["template"]["commit"]
UPSTREAM = REPO_ROOT / LOCK["template"].get("localPath", "upstream/")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "golden-render"
UPDATE = os.environ.get("WSC_UPDATE_GOLDENS") == "1"

# P1: the reference packs gained labs.teaching, evaluation.judge/l1 and teaching content (IT and
# maintenance probes, bait FAQ entries, maintenance prompt hints). P1b: the teaching render runtime
# (eval-order arrays, per-case RUN_TAG actors, run record, probe-scoped judging, L1 via l1_eval.py, the
# scenario-policy Mind the Goal prompt, KB freshness, scoped lookups, printed-residue patches) and
# pack.json evaluation.l1. P1 review fixes: l1_eval.py (negation cues scoped to their clause); the IT
# and maintenance labs.observations no longer say which prohibited topics are in the holdout; 09 keeps
# its session → query map in a per-process file (session-io-per-process); pack.json and RELEASE.json
# carry the labs.teaching derived order (student-safe teaching object; instructor/teaching.json).
# P4 (SPEC D12): RELEASE.json records provenancePolicy and the instructor provenance report gains the
# policy, class and origin columns; release versions (the hashed file set) are unchanged.
# kb-nonfilterable-metadata: create_kb.py also marks AMAZON_BEDROCK_METADATA non-filterable (S3 Vectors' 2 KB
# filterable-metadata cap failed every zh-CN chunk); the pack digests are unchanged.
# explicit-harness-image: 04 names the VPC Harness's private AgentCore image, so the CDK role can pull it at create.
# kb-ingestion-terminal-states / kb-ingestion-must-complete: create_kb.py's sync raises unless every document
# ingested (a FAILED ingestion no longer passes 01); the pack digests are unchanged.
# python-3.12: every script and shebang names python3.12 and 00-setup checks it; the student guide's
# Python prerequisite says 3.12, so the pack digests change too.
PINNED = {
    "hr-default": ("hr-default-2bacc4118cdf", "ad5a9df334583f4a1eff4f75c097be217e69afab7f7d91350f8a0580eec6dca1"),
    "it-helpdesk": ("it-helpdesk-6b077907be8b", "f2f3f2a3fec5c2faa120336c6d77abe87c7381fd7554c655c43c98a38fece965"),
    "maintenance": ("maintenance-7ec79083af4f", "802fa4df059f69269dd8dc221cd4c31af06fe70284843cfd6cbbe96df50e30e0"),
}

_RENDERED: dict[str, tuple] = {}


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """Compile + render a reference pack once per module; returns (pack, release)."""

    def get(pack_id: str):
        if pack_id not in _RENDERED:
            base = tmp_path_factory.mktemp(pack_id)
            scenario = load_scenario(REPO_ROOT / "scenarios" / pack_id / "scenario.yaml")
            pack = compiler.compile_pack(scenario, base / "out", template_commit=TEMPLATE_COMMIT)
            release = render.render_release(pack, UPSTREAM, base / "out" / "release", template_commit=TEMPLATE_COMMIT)
            _RENDERED[pack_id] = (pack, release)
        return _RENDERED[pack_id]

    yield get
    _RENDERED.clear()


def _fingerprint(release) -> dict:
    manifest = (release.release_dir / render.MANIFEST_NAME).read_bytes()
    return {
        "version": release.version,
        "manifestSha256": hashlib.sha256(manifest).hexdigest(),
        "patches": [[p.file, p.name, p.matches] for p in release.patches],
        "files": dict(sorted(release.files.items())),
    }


@pytest.mark.parametrize("pack_id", sorted(PINNED))
def test_reference_pack_release_is_unchanged(pack_id, rendered):
    pack, release = rendered(pack_id)
    pack_digest = hashlib.sha256(json.dumps(pack.files, sort_keys=True).encode("utf-8")).hexdigest()
    assert (release.version, pack_digest) == PINNED[pack_id]


@pytest.mark.parametrize("pack_id", sorted(PINNED))
def test_reference_pack_release_files_match_golden_fixture(pack_id, rendered):
    _pack, release = rendered(pack_id)
    actual = _fingerprint(release)
    path = FIXTURES / f"{pack_id}.json"
    if UPDATE:
        current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        current.update(actual, packId=pack_id)
        path.write_text(json.dumps(current, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    golden = json.loads(path.read_text(encoding="utf-8"))
    expected_files, actual_files = golden["files"], actual["files"]
    changed = sorted(k for k in set(expected_files) & set(actual_files) if expected_files[k] != actual_files[k])
    added = sorted(set(actual_files) - set(expected_files))
    removed = sorted(set(expected_files) - set(actual_files))
    assert (changed, added, removed) == ([], [], []), f"{pack_id}: release files changed={changed} added={added} removed={removed}"
    assert actual["patches"] == golden["patches"], f"{pack_id}: applied patch list changed"
    assert actual["manifestSha256"] == golden["manifestSha256"], f"{pack_id}: RELEASE.json changed"
    assert actual["version"] == golden["version"] == PINNED[pack_id][0]


_BARE_PYTHON = re.compile(r"(?<![\w./-])(python3|pip3)(?![\w.])")


@pytest.mark.parametrize("pack_id", sorted(PINNED))
def test_every_release_script_runs_python_3_12(pack_id, rendered):
    # Live 2026-11-17: on the Workshop EC2 a student shell's python3 is 3.9 without boto3; only the
    # Guided Run's PATH (/opt/workshop-customizer/bin) made 01's `python3 create_kb.py` work.
    _pack, release = rendered(pack_id)
    bare = []
    for path in sorted(release.release_dir.rglob("*")):
        rel = path.relative_to(release.release_dir).as_posix()
        if not path.is_file() or rel.startswith("pack/") or path.suffix not in (".sh", ".py"):
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".sh":
            bare += [f"{rel}:{text.count(chr(10), 0, m.start()) + 1}" for m in _BARE_PYTHON.finditer(text)]
        elif text.startswith("#!") and "python3.12" not in text.splitlines()[0]:
            bare.append(f"{rel}:1")
        compile(text, rel, "exec") if path.suffix == ".py" else None  # parses under the supported interpreter
    assert bare == [], f"{pack_id}: scripts that do not name python3.12: {bare}"


def _run_setup(release_dir: Path, tmp_path: Path, *, python312: bool):
    stubs = tmp_path / "bin"
    stubs.mkdir(parents=True)
    for name, body in (("npx", "echo 1.0.0-preview.30"), ("node", "echo v20.0.0"), ("aws", "echo 111122223333"),
                       ("python3", "echo Python 3.9.25")):
        (stubs / name).write_text(f"#!/bin/sh\n{body}\n")
    if python312:
        (stubs / "python3.12").write_text('#!/bin/sh\n[ "$1" = --version ] && echo Python 3.12.14; exit 0\n')
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    (stubs / "mkdir").symlink_to(shutil.which("mkdir"))
    return subprocess.run(["/bin/bash", str(release_dir / "00-setup.sh")], capture_output=True, text=True,
                          env={"PATH": str(stubs), "HOME": str(tmp_path)})


def test_00_setup_requires_python_3_12(rendered, tmp_path):
    _pack, release = rendered("hr-default")
    missing = _run_setup(release.release_dir, tmp_path / "a", python312=False)
    assert missing.returncode == 1 and "Python 3.12 not found" in missing.stdout
    ok = _run_setup(release.release_dir, tmp_path / "b", python312=True)
    assert ok.returncode == 0 and "Environment ready" in ok.stdout, ok.stdout + ok.stderr
