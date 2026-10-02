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
PINNED = {
    "hr-default": ("hr-default-85c5a2d70b64", "7f29def6c178235da1498a873e50b2b5fa51433de9c4dddfaeabe546c709b535"),
    "it-helpdesk": ("it-helpdesk-c6181abdde98", "07f73954af5d96b3f936ef5bc36a1e820062a5508d9ef8c0564f80c0cdd1422a"),
    "maintenance": ("maintenance-c8acba8ebcac", "210ef2c600a8a3d1ae8ae8f2a53e0b59b58bee7923fce5c2469b06d7a245fa4b"),
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
