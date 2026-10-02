"""CLI ``guide``: render a Workshop Guide without a build (repair loops, golden regeneration)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

from workshop_customizer import cli, compiler, guide
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_COMMIT = json.loads((REPO_ROOT / "template-lock.json").read_text())["template"]["commit"]
IT = REPO_ROOT / "scenarios" / "it-helpdesk" / "scenario.yaml"


def _copy(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "it"
    shutil.copytree(IT.parent, root)
    return root / "scenario.yaml", yaml.safe_load(IT.read_text(encoding="utf-8"))


def _save(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_cli_guide_prints_the_compiled_guides(tmp_path, capsys):
    compiled = compiler.compile_pack(load_scenario(IT), tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    assert cli.main(["guide", str(IT), "--audience", "student", "--out", "-"]) == 0
    captured = capsys.readouterr()
    assert captured.out == (compiled.pack_dir / "labs" / "student-guide.md").read_text(encoding="utf-8")
    assert captured.err == ""
    out = tmp_path / "instructor.md"
    assert cli.main(["guide", str(IT), "--audience", "instructor", "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == (compiled.instructor_dir / "instructor-guide.md").read_text(encoding="utf-8")


def test_cli_guide_draft_renders_an_unreviewed_scenario(tmp_path, capsys):
    path, data = _copy(tmp_path)
    data["labs"]["guide"]["provenance"] = "ai_draft"
    _save(path, data)
    assert cli.main(["guide", str(path)]) == 1  # the gate refuses an unreviewed narrative outside --draft
    assert "guide-narrative" in capsys.readouterr().out
    assert cli.main(["guide", str(path), "--draft"]) == 0
    assert guide.DRAFT_MARKER in capsys.readouterr().out


def test_cli_guide_exits_1_on_a_narrative_holdout_leak(tmp_path, capsys):
    path, data = _copy(tmp_path)
    data["labs"]["guide"]["memoryLesson"] = "Also try the warranty-lookup question."
    _save(path, data)
    assert cli.main(["guide", str(path), "--audience", "student"]) == 1
    assert "guide.holdout_leak" in capsys.readouterr().err


def test_cli_guide_embeds_a_rehearsal_record(tmp_path, capsys):
    record = tmp_path / "rehearsal.json"
    record.write_text(json.dumps({"verdict": "not_ready", "phenomena": [{"id": "gap", "kind": "retrieval_gap", "verdict": "not_reproduced",
                                                                         "evidence": "RG_RETRIEVAL_OK_BASELINE"}]}), encoding="utf-8")
    assert cli.main(["guide", str(IT), "--audience", "instructor", "--rehearsal", str(record)]) == 0
    out = capsys.readouterr().out
    assert "Verdict: **not_ready**" in out and "RG_RETRIEVAL_OK_BASELINE" in out
