"""Every reference pack in scenarios/ validates, compiles and renders a residue-clean release."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from workshop_customizer import compiler, render, script_facts, validator
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = REPO_ROOT / "upstream"
TEMPLATE_COMMIT = json.loads((REPO_ROOT / "template-lock.json").read_text())["template"]["commit"]
PACKS = sorted(p.name for p in (REPO_ROOT / "scenarios").iterdir() if (p / "scenario.yaml").is_file())


def test_three_reference_packs_exist():
    assert PACKS == ["hr-default", "it-helpdesk", "maintenance"]


@pytest.mark.parametrize("pack_id", PACKS)
def test_pack_builds_a_verified_release(pack_id, tmp_path):
    scenario = load_scenario(REPO_ROOT / "scenarios" / pack_id / "scenario.yaml")
    policy = validator.validate_scenario_policy(scenario.data, root=scenario.root)
    assert policy.ok, policy.describe()
    pack = compiler.compile_pack(scenario, tmp_path / "build", template_commit=TEMPLATE_COMMIT)
    release = render.render_release(pack, UPSTREAM, tmp_path / "release", template_commit=TEMPLATE_COMMIT)
    manifest = render.verify_release(release.release_dir)
    assert manifest["packId"] == pack_id and release.version.startswith(f"{pack_id}-")
    assert len(manifest["files"]) >= 70
    # holdout never ships; instructor bundle stays outside the release
    assert not (release.release_dir / "instructor").exists()
    # Derive the expected version from the verified release content, without requiring ignored
    # workstation build artifacts to exist in a fresh checkout.
    assert release.version == f"{pack_id}-{manifest['contentHash'][:12]}"
    # The harness iteration limit rehearsal names on an empty reply is upstream's, and no pack changes it.
    limit = [str(script_facts.UPSTREAM_MAX_ITERATIONS)]
    for deploy in (UPSTREAM / "04-deploy.sh", release.release_dir / "04-deploy.sh"):
        assert re.findall(r"--max-iterations (\d+)", deploy.read_text(encoding="utf-8")) == limit, deploy
