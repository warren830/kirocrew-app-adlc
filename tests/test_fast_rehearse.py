"""Tests for tools/fast_rehearse.py (no AWS: the host script is checked with bash -n, the judging on fixture runs)."""
from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import teaching_run as tr

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def fast():
    spec = importlib.util.spec_from_file_location("wc_fast_rehearse", REPO / "tools" / "fast_rehearse.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_the_host_script_runs_the_new_release_in_place_and_parses_like_the_guided_run(fast, tmp_path):
    script = fast.host_script(region="us-west-2", bucket="bucket-1", prefix="customizer-releases/p/fast/v2")
    path = tmp_path / "fast.sh"
    path.write_text(script, encoding="utf-8")
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode == 0
    assert "@" not in "".join(line for line in script.splitlines() if "REGION=" in line or "STEPS=" in line)
    assert "STEPS='conversation:06-test-conversation.sh baseline:09-run-eval.sh optimize:10-optimize-prompt.sh " \
           "judge-stability:13-judge-stability.sh'" in script
    assert "CONTENT='knowledge-base:01-create-kb.sh gateway:02-create-gateway.sh skills:03-configure-skills.sh'" in script
    # The synced release and its records stay as they are: nothing is written under ~/workshop/current or releases/.
    assert "ln -s" not in script and "releases/" not in script.replace("customizer-releases/", "")
    # 04 would rebuild the agent project: the prompt and skills reach the Harness through update-harness instead.
    assert "04-deploy.sh" in script and "run_step agent" not in script and "update-harness" in script
    # The parser is the RunStep document's own (the same text tests/teaching_run.py runs for the fixture runs).
    assert fast.parser_source() == tr.parser_source()


def test_the_fast_copy_of_05_asks_for_the_active_release(fast, tmp_path):
    """runtime_permissions.py refuses a release that is not ~/workshop/current; the fast copy never is."""
    script = fast.host_script(region="us-west-2", bucket="b", prefix="p")
    sed = next(line for line in script.splitlines() if line.startswith("sed -i "))
    target = tmp_path / "05-setup-memory.sh"
    target.write_text('python3 /opt/workshop-customizer/runtime_permissions.py --release-version "$RELEASE_VERSION"\n', encoding="utf-8")
    probe = sed.replace('"$REL/05-setup-memory.sh"', str(target))
    if shutil.which("gsed") is None and "darwin" in __import__("sys").platform:
        probe = probe.replace("sed -i ", "sed -i '' ", 1)
    subprocess.run(["bash", "-c", f"CURRENT_VERSION=pack-111111111111\n{probe}"], check=True)
    assert target.read_text() == 'python3 /opt/workshop-customizer/runtime_permissions.py --release-version "pack-111111111111"\n'


def test_the_fast_steps_replace_the_last_runs_and_are_judged_without_a_class_verdict(fast, tmp_path):
    previous = tr.run_state("hr-reproduced")
    previous["status"] = "passed"
    lines = {}
    for step_id in fast.FAST_STEPS:
        stdout, compact = tr._step_stdout("hr-reproduced", step_id)
        lines[step_id] = tr.run_parser(tmp_path / step_id, stdout, step_id=step_id, compact=compact)
    records = {sid: fast.step_record(sid, line) for sid, line in lines.items()}
    assert all(r["status"] == "passed" for r in records.values())
    assert records["baseline"]["outputs"]["scores"] == tr.load_steps("hr-reproduced")["baseline"]["outputs"]["scores"]
    state = fast.fast_state(previous, "hr-default-222222222222", records)
    assert state["releaseVersion"] == "hr-default-222222222222" and state["fast"]["from"] == previous["releaseVersion"]
    assert state["steps"]["agent"] == previous["steps"]["agent"] and previous["releaseVersion"] != state["releaseVersion"]

    from workshop_customizer import compiler
    from workshop_customizer.scenario import load_scenario

    project = tmp_path / "project"
    shutil.copytree(REPO / "scenarios" / "hr-default", project)
    commit = json.loads((REPO / "template-lock.json").read_text())["template"]["commit"]
    compiler.compile_pack(load_scenario(project / "scenario.yaml"), project / "build", template_commit=commit)
    doc = fast.judge(project, previous, "hr-default-222222222222", records)
    assert doc["verdict"] == "ready" and doc["readyForClass"] is False  # the regular loop decides the class
    assert doc["fast"]["steps"] == sorted(fast.FAST_STEPS) and "fast re-rehearsal" in fast.render_markdown(doc).lower()


def test_the_tool_refuses_without_a_complete_run_or_a_new_build(fast, tmp_path, capsys):
    project = tmp_path / "p"
    (project / "build" / "release").mkdir(parents=True)
    (project / "run").mkdir()
    (project / "build" / "release" / "RELEASE.json").write_text(json.dumps({"version": "p-111111111111"}))
    state = {"releaseVersion": "p-111111111111", "status": "passed", "steps": {}}
    (project / "run" / "state.json").write_text(json.dumps(state))
    argv = ["--project-dir", str(project), "--profile", "x", "--account", "1"]
    assert fast.main(argv) == 2 and "rebuild the repaired pack first" in capsys.readouterr().err
    (project / "run" / "state.json").write_text(json.dumps({**state, "status": "failed"}))
    assert fast.main(argv) == 2 and "not complete" in capsys.readouterr().err
