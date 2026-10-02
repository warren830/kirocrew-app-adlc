"""tools/direct_fleet.py: which packs a fleet rehearses, each pack's row and the matrix (no AWS)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("direct_fleet", REPO / "tools" / "direct_fleet.py")
fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet)  # type: ignore[union-attr]


def _pack(data: Path, name: str, *, phenomena: int = 1, last: dict | None = None, version: str = "v1") -> Path:
    release = data / "projects" / name / "build" / "release"
    (release / "pack").mkdir(parents=True)
    (release / "RELEASE.json").write_text(json.dumps({"version": version}))
    teaching = {"phenomena": [{"id": f"p{i}"} for i in range(phenomena)]} if phenomena else {}
    (release / "pack" / "pack.json").write_text(json.dumps({"teaching": teaching}))
    (data / "projects" / name / "project.json").write_text(json.dumps({"lastRehearsal": last} if last else {}))
    return data / "projects" / name


def test_the_fleet_takes_built_packs_with_a_teaching_design(tmp_path):
    _pack(tmp_path, "campus")
    _pack(tmp_path, "reference", phenomena=0)
    (tmp_path / "projects" / "draft").mkdir()  # never built
    assert [p.name for p in fleet.fleet_packs(tmp_path, [])] == ["campus"]
    with pytest.raises(SystemExit, match="reference"):
        fleet.fleet_packs(tmp_path, ["campus", "reference"])


def test_a_pack_ready_for_class_that_direct_mode_finds_not_ready_is_flagged(tmp_path):
    pdir = _pack(tmp_path, "gas", last={"verdict": "ready", "readyForClass": True, "releaseVersion": "v1"})
    last = fleet.last_class_verdict(pdir, "v1")
    doc = {"verdict": "not_ready", "reasonCode": "CONTRASTS_NOT_REPRODUCED", "direct": {"invokeErrors": [{"caseId": "x"}]},
           "phenomena": [{"id": "gap", "kind": "retrieval_gap", "verdict": "not_reproduced"},
                         {"id": "pf1", "kind": "prompt_fixable", "verdict": "reproduced"},
                         {"id": "pf2", "kind": "prompt_fixable", "verdict": "insufficient_evidence"}]}
    r = fleet.row("gas", "v1", doc, None, 444.0, last)
    assert r["drift"] == "was ready for class on this release" and r["invokeErrors"] == 1
    assert fleet.kinds_summary(r["phenomena"]) == "gap 0/1 · PF 1/2"
    ok = fleet.row("campus", "v2", {"verdict": "ready", "phenomena": []}, None, 60.0, fleet.last_class_verdict(pdir, "v2"))
    failed = fleet.row("ops", "v3", None, "RuntimeError: quota", 5.0, None)
    flaky = fleet.row("loyalty", "v4", {"verdict": "ready", "phenomena": [], "direct": {"robust": False}}, None, 60.0, None)
    assert flaky["verdict"] == "not_robust"  # --repeat: ready in the last round only
    assert ok["drift"] is None and failed["verdict"] == "error" and failed["drift"] is None
    text = fleet.render_markdown({"generatedAt": "t", "packs": [r, ok, failed], "ready": 1, "parallel": 3, "seconds": 600})
    assert "| gas | **not_ready** `CONTRASTS_NOT_REPRODUCED` | gap 0/1 · PF 1/2 | gap ✗ pf1 ✓ pf2 ? | 7.4 | ready for class |" in text
    assert "| campus | **ready** | — | — | 1.0 | ready for class (v1) |" in text and "**error** — RuntimeError: quota" in text
    assert "## Check these" in text and "**gas**: was ready for class on this release, direct now not_ready" in text
