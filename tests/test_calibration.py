"""Tests for judge-noise calibration → evaluation.noiseBand."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from workshop_customizer import calibration, cli
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]

UPSTREAM_LOG = """=========================================
Optional Lab B: 评委稳定性检验
=========================================
  目标 trace: 0123456789abcdef  （连打 3 次 THELMA）
  第 1 次...
    value = 0.83
  第 2 次...
    value = 0.80
  第 3 次...
    value = （无结果，可能 spans 未索引，稍后重试）
  第 4 次...
    value = 0.86

  分数: [0.83, 0.8, 0.86]
  均值=0.830  标准差=0.024  极差=0.060
"""


def test_parse_scores_from_upstream_log_and_other_shapes():
    assert calibration.parse_scores(UPSTREAM_LOG) == [0.83, 0.80, 0.86]
    assert calibration.parse_scores("  分数: [0.5, 0.55]\n") == [0.5, 0.55]
    assert calibration.parse_scores("[0.9, 0.91]") == [0.9, 0.91]
    assert calibration.parse_scores("0.7\n0.72\n") == [0.7, 0.72]
    assert calibration.parse_scores("no numbers here") == []
    with pytest.raises(calibration.CalibrationError):
        calibration.parse_scores("[0.9,")


def test_summary_band_rule_and_verdicts():
    stable = calibration.summarise([0.83, 0.80, 0.86])
    assert stable.verdict == "stable" and stable.band == 0.06 and stable.runs == 3  # spread dominates 2*std
    tight = calibration.summarise([0.8, 0.8, 0.8])
    assert tight.band == calibration.BAND_FLOOR and tight.std == 0
    moderate = calibration.summarise([0.6, 0.7, 0.66])
    assert moderate.verdict == "moderate"
    unstable = calibration.summarise([0.3, 0.9, 0.5])
    assert unstable.verdict == "unstable" and unstable.band <= 1.0
    with pytest.raises(calibration.CalibrationError):
        calibration.summarise([0.5])
    with pytest.raises(calibration.CalibrationError):
        calibration.summarise([0.5, 1.5])


def test_is_real_change_fails_closed():
    band = {"thelma_rag_quality": 0.06}
    assert calibration.is_real_change(0.1, band)
    assert not calibration.is_real_change(0.05, band)
    assert not calibration.is_real_change(0.5, band, metric="unknown_metric")
    assert calibration.is_real_change(-0.07, 0.06)


def test_apply_writes_schema_valid_band_and_note(tmp_path):
    src = REPO_ROOT / "scenarios" / "it-helpdesk"
    dst = tmp_path / "it-helpdesk"
    shutil.copytree(src, dst)
    stability = calibration.calibrate(UPSTREAM_LOG)
    result = calibration.apply_to_scenario(dst / "scenario.yaml", stability)
    data = yaml.safe_load((dst / "scenario.yaml").read_text())
    assert data["evaluation"]["noiseBand"] == {"thelma_rag_quality": 0.06}
    assert any("noiseBand 0.060" in o for o in data["labs"]["observations"])
    assert result["noiseBand"] == 0.06 and result["verdict"] == "stable" and result["runs"] == 3
    # the edited scenario still passes schema, cross-refs and the provenance gate
    scenario = load_scenario(dst / "scenario.yaml")
    assert scenario.data["evaluation"]["noiseBand"]["thelma_rag_quality"] == 0.06
    # re-applying replaces the note instead of stacking duplicates
    calibration.apply_to_scenario(dst / "scenario.yaml", calibration.summarise([0.5, 0.6]))
    data = yaml.safe_load((dst / "scenario.yaml").read_text())
    assert sum(1 for o in data["labs"]["observations"] if "Judge noise calibration" in o) == 1
    assert data["evaluation"]["noiseBand"]["thelma_rag_quality"] == 0.1  # 2σ = spread = 0.1


def test_cli_calibrate_dry_run_and_apply(tmp_path, capsys):
    dst = tmp_path / "hr"
    shutil.copytree(REPO_ROOT / "scenarios" / "hr-default", dst)
    log = tmp_path / "stability.log"
    log.write_text(UPSTREAM_LOG)
    assert cli.main(["calibrate", str(dst / "scenario.yaml"), str(log), "--dry-run", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["applied"] is False and out["noiseBand"] == 0.06 and out["verdict"] == "stable"
    assert "noiseBand" not in (yaml.safe_load((dst / "scenario.yaml").read_text())["evaluation"])
    assert cli.main(["calibrate", str(dst / "scenario.yaml"), str(log), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is True
    assert yaml.safe_load((dst / "scenario.yaml").read_text())["evaluation"]["noiseBand"] == {"thelma_rag_quality": 0.06}
    bad = tmp_path / "bad.log"
    bad.write_text("nothing useful\n")
    assert cli.main(["calibrate", str(dst / "scenario.yaml"), str(bad), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"
