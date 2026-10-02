"""Tests for the engine CLI."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

from workshop_customizer import cli

REPO_ROOT = Path(__file__).resolve().parents[1]
HR_SCENARIO = REPO_ROOT / "scenarios" / "hr-default" / "scenario.yaml"


def run(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_validate_hr_default(capsys):
    code, payload = run(capsys, "validate", str(HR_SCENARIO), "--json")
    assert code == 0
    assert payload["status"] == "ok"
    assert payload["scenario"]["goldenCases"] == 16
    assert payload["scenario"]["probes"] == 4
    # Only the pinned advisory teaching warnings of the reference pack (see tests/test_teaching_policy.py).
    assert payload["policyFindings"] and all(f.startswith("WARNING teaching.") for f in payload["policyFindings"])


def test_validate_reports_blocked_gate(tmp_path: Path, capsys):
    root = tmp_path / "customer"
    shutil.copytree(HR_SCENARIO.parent, root)
    data = yaml.safe_load((root / "scenario.yaml").read_text(encoding="utf-8"))
    data["id"] = "customer-pack"
    data["packKind"] = "customer"
    (root / "scenario.yaml").write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")

    code, payload = run(capsys, "validate", str(root / "scenario.yaml"), "--json")
    assert code == 1 and payload["status"] == "error"
    assert "provenance gate blocked" in payload["error"]

    code, payload = run(capsys, "validate", str(root / "scenario.yaml"), "--json", "--allow-gate-findings")
    assert code == 1 and payload["status"] == "blocked"
    assert any("customer_confirmed" in v for v in payload["gateViolations"])


def test_build_and_verify_release(tmp_path: Path, capsys):
    out = tmp_path / "build"
    code, payload = run(capsys, "build-release", str(HR_SCENARIO), "--out", str(out), "--json")
    assert code == 0 and payload["status"] == "ok"
    assert payload["version"].startswith("hr-default-")
    release_dir = Path(payload["releaseDir"])
    assert (release_dir / "RELEASE.json").is_file()
    assert (release_dir / "pack" / "pack.env").is_file()
    assert (out / "instructor" / "golden" / "holdout.json").is_file()

    code, verified = run(capsys, "verify-release", str(release_dir), "--json")
    assert code == 0 and verified["version"] == payload["version"]

    (release_dir / "pack" / "pack.env").write_text("tampered\n")
    code, failed = run(capsys, "verify-release", str(release_dir), "--json")
    assert code == 1 and failed["status"] == "error" and "hash mismatch: pack/pack.env" in failed["error"]


def test_compile_only(tmp_path: Path, capsys):
    out = tmp_path / "build"
    code, payload = run(capsys, "compile", str(HR_SCENARIO), "--out", str(out), "--json")
    assert code == 0 and payload["files"] > 20
    assert (out / "checksums.json").is_file() and not (out / "release").exists()


def test_missing_scenario_is_an_error(capsys):
    code, payload = run(capsys, "validate", "/nonexistent/scenario.yaml", "--json")
    assert code == 1 and payload["status"] == "error"
