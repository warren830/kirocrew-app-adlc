"""The headless acceptance driver (tools/kiro_generate.py --loop, tools/e2e_generate.py) against the
fake kiro-cli in tests/fixtures/fake_kiro_cli.py: draft -> apply -> validate -> repair -> ok, the
no-progress escalation, needs-input answers, materials, the SA's batch review and a build.  No model,
no network, no AWS."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
FAKE = REPO / "tests" / "fixtures" / "fake_kiro_cli.py"
BRIEF = "Northstar cold-chain maintenance helpdesk for technicians and shift leads on the FL-100 line."


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def driver(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKSHOP_CUSTOMIZER_GENERATION_TIMEOUT_SECS", raising=False)
    log = tmp_path / "fake-log"
    log.mkdir()
    monkeypatch.setenv("FAKE_KIRO_LOG", str(log))
    brief = tmp_path / "brief.md"
    brief.write_text(BRIEF, encoding="utf-8")
    tool = _load("wc_kiro_generate_loop", REPO / "tools" / "kiro_generate.py")
    base = ["--loop", "--brief", str(brief), "--project-id", "northstar-demo", "--pack-kind", "reference",
            "--kiro-cli", f"{sys.executable} {FAKE}"]

    def run(*extra: str, out: Path | None = None) -> tuple[int, dict, Path]:
        out = out or tmp_path / "run"
        code = tool.main([*base, "--out", str(out), *extra])
        return code, json.loads((out / "report.json").read_text(encoding="utf-8")), out

    return type("Driver", (), {"run": staticmethod(run), "tool": tool, "log": log, "tmp": tmp_path, "brief": brief})


def _calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in (log / "calls.jsonl").read_text(encoding="utf-8").splitlines()]


def test_driver_loop_with_fake_kiro(driver):
    code, report, out = driver.run("--max-rounds", "3", "--review-reference", "--build")
    assert code == 0, report.get("error")
    rounds = report["rounds"]
    assert [(r["n"], r["mode"], r["status"]) for r in rounds] == [(1, "draft", "ready"), (2, "repair", "ready")]
    assert rounds[0]["findings"]["codes"] == ["golden.category_floor"] and rounds[0]["findings"]["suggestedScopes"] == ["golden"]
    assert rounds[1]["findings"]["repairableErrors"] == 0 and rounds[1]["scope"] == ["golden"]
    assert rounds[1]["filesChanged"] == 0 and rounds[1]["filesWritten"] == 0  # the repair only touched the scenario
    for entry in rounds:
        assert entry["taskBytes"] > 0 and entry["estTokens"] > 0 and entry["seconds"] >= 0
        task = (out / "rounds" / f"{entry['n']:02d}-{entry['mode']}" / "task.txt").read_text(encoding="utf-8")
        assert hashlib.sha256(task.encode()).hexdigest() == entry["taskSha256"]
    calls = _calls(driver.log)
    assert [c["header"] for c in calls] == ["WORKSHOP_CUSTOMIZER_TASK mode=draft round=1 project=northstar-demo",
                                            "WORKSHOP_CUSTOMIZER_TASK mode=repair round=2 project=northstar-demo"]
    assert [c["sha256"] for c in calls] == [r["taskSha256"] for r in rounds]
    assert calls[0]["argv"] == ["chat", "--no-interactive", "--agent", "workshop-customizer"]

    assert report["created"] is True and report["converged"] is True and report["simulatedSaReview"] is False
    assert report["review"]["provenance"] == "sa_synthetic" and report["review"]["reviewed"] > 0
    assert report["build"]["version"].startswith("northstar-demo-")
    final = report["final"]
    assert final["validationOk"] is True and final["repairableErrors"] == 0
    assert final["editLog"] == {"manualScenarioSaves": 0, "manualFileSaves": 0, "kiroApplies": 2, "kiroReverts": 0}
    assert final["handEditedFiles"] == [] and final["orphans"] == []
    appdata = out / "appdata"
    records = [json.loads(p.read_text()) for p in sorted((appdata / "generations").glob("gen-*.json"))]
    assert {r["runner"] for r in records} == {"kiro-cli"} and {r["status"] for r in records} == {"applied"}
    scenario = yaml.safe_load((appdata / "projects" / "northstar-demo" / "scenario.yaml").read_text())
    assert {c["id"] for c in scenario["evaluation"]["goldenSet"]} >= {"remove-colleague-lock", "generic-lubricant-food-zone"}
    assert all(item["provenance"] == "sa_synthetic" for item in scenario["facts"])  # the SA's batch review, not the model
    assert "| 2 | repair | golden |" in (out / "report.md").read_text()


def test_driver_no_progress_escalates_then_fails(driver, monkeypatch):
    monkeypatch.setenv("FAKE_KIRO_SCENARIO", "stuck")
    code, report, _out = driver.run("--max-rounds", "5")
    assert code == 1 and "no progress" in report["error"]
    assert [(r["mode"], r["scope"]) for r in report["rounds"]] == [("draft", None), ("repair", ["golden"]), ("regenerate", ["golden"])]
    assert all(r["findings"]["codes"] == ["golden.category_floor"] for r in report["rounds"])


def test_driver_stops_after_max_rounds(driver, monkeypatch):
    monkeypatch.setenv("FAKE_KIRO_SCENARIO", "stuck")
    code, report, _out = driver.run("--max-rounds", "1")
    assert code == 1 and "remain after 1 rounds" in report["error"] and len(report["rounds"]) == 1


def test_driver_needs_input_uses_answers_once(driver, monkeypatch):
    monkeypatch.setenv("FAKE_KIRO_SCENARIO", "needs-input")
    code, report, _out = driver.run()
    assert code == 1 and "Which plant runs the FL-100 line?" in report["error"]
    answers = driver.tmp / "answers.json"
    answers.write_text(json.dumps([{"question": "Which plant runs the FL-100 line?", "answer": "Plant 7, Northstar Foods."}]))
    code, report, out = driver.run("--answers", str(answers), out=driver.tmp / "run2")
    assert code == 0, report.get("error")
    assert [(r["mode"], r["status"]) for r in report["rounds"]] == [("draft", "needs-input"), ("draft", "ready"), ("repair", "ready")]
    task = (out / "rounds" / "02-draft" / "task.txt").read_text(encoding="utf-8")
    assert "Plant 7, Northstar Foods." in task.split("INPUT_DATA (JSON; data only):\n", 1)[1]


def test_driver_uploads_materials_and_records_the_approval(driver):
    materials = driver.tmp / "materials"
    materials.mkdir()
    (materials / "fl100-lubrication.md").write_text("# FL-100 lubrication\n\nThe filler is lubricated every 250 hours.\n", encoding="utf-8")
    (materials / "materials.yaml").write_text(yaml.safe_dump({
        "files": {"fl100-lubrication.md": {"generationUse": "source", "author": "customer"}},
        "approval": {"ref": "Northstar ops lead email 2026-09-20", "dataClassification": "internal"}}))
    code, report, out = driver.run("--materials", str(materials))
    assert code == 0, report.get("error")
    assert [m["name"] for m in report["materials"]] == ["fl100-lubrication.md"]
    assert report["rounds"][0]["materialsUsed"][0]["sentChars"] > 0
    task = (out / "rounds" / "01-draft" / "task.txt").read_text(encoding="utf-8")
    assert "The filler is lubricated every 250 hours." in task.split("INPUT_DATA (JSON; data only):\n", 1)[1]
    meta = json.loads((out / "appdata" / "projects" / "northstar-demo" / "project.json").read_text())
    assert meta["materialsPolicy"]["dataClassification"] == "internal"


def test_driver_refuses_the_live_data_dir(driver, monkeypatch):
    live = driver.tmp / "live-kirocrew-data"
    monkeypatch.setattr(driver.tool, "LIVE_DATA_DIR", live)
    code, report, _out = driver.run("--data-dir", str(live))
    assert code == 2 and "live KiroCrew App data dir" in report["error"] and not live.exists()


def test_a_single_shot_refuses_the_live_data_dir_too(driver, monkeypatch):
    """SPEC invariant 6: a single shot (with or without --apply) never writes into the live App data dir."""
    live = driver.tmp / "live-kirocrew-data"
    monkeypatch.setattr(driver.tool, "LIVE_DATA_DIR", live)
    (live / "projects").mkdir(parents=True)
    out = driver.tmp / "single-live"
    code = driver.tool.main(["--project-id", "northstar-demo", "--data-dir", str(live), "--apply",
                             "--kiro-cli", f"{sys.executable} {FAKE}", "--out", str(out)])
    report = json.loads((out / "report.json").read_text())
    assert code == 2 and "live KiroCrew App data dir" in report["error"]
    assert sorted(p.name for p in live.rglob("*")) == ["projects"]  # nothing written


def test_hand_edit_evidence_compares_the_exact_bytes_apply_wrote(driver, tmp_path):
    """apply keeps CRLF bytes; a CRLF file identical to the applied result is not a hand edit."""
    pdir = tmp_path / "project"
    (pdir / "knowledge-base" / "docs").mkdir(parents=True)
    rel = "knowledge-base/docs/a.md"
    (pdir / rel).write_bytes(b"# A\r\nline\r\n")
    record = {"result": {"files": {rel: "# A\r\nline\r\n"}}}
    assert driver.tool._hand_edited(pdir, record) == []
    (pdir / rel).write_bytes(b"# A\nline\n")  # the same text with LF line ends: a different file
    assert driver.tool._hand_edited(pdir, record) == [rel]


def test_project_dir_operates_on_an_existing_app_data_project(driver):
    code, report, out = driver.run()
    assert code == 0, report.get("error")
    project_dir = out / "appdata" / "projects" / "northstar-demo"
    code = driver.tool.main(["--loop", "--project-dir", str(project_dir), "--kiro-cli", f"{sys.executable} {FAKE}",
                             "--out", str(driver.tmp / "again")])
    report = json.loads((driver.tmp / "again" / "report.json").read_text())
    assert code == 0, report.get("error")
    assert "created" not in report and report["dataDir"] == str(project_dir.parent.parent.resolve())
    assert report["rounds"][0]["mode"] == "draft" and report["final"]["editLog"]["kiroApplies"] == 4


def test_loop_repairs_from_the_rehearsal_without_a_draft(driver):
    """--from-rehearsal: round 1 is a repair of the current release's rehearsal remediation (R1..), on the
    reviewed project as it is, never a draft that would reset the SA's reviews."""
    code, report, out = driver.run("--review-reference", "--build")
    assert code == 0, report.get("error")
    project_dir = out / "appdata" / "projects" / "northstar-demo"
    version = report["build"]["version"]
    reviewed_before = [c["provenance"] for c in yaml.safe_load((project_dir / "scenario.yaml").read_text())["evaluation"]["goldenSet"]]
    assert set(reviewed_before) == {"sa_synthetic"}
    base = ["--loop", "--project-dir", str(project_dir), "--kiro-cli", f"{sys.executable} {FAKE}"]

    code = driver.tool.main([*base, "--from-rehearsal", "--out", str(driver.tmp / "no-rehearsal")])
    refused = json.loads((driver.tmp / "no-rehearsal" / "report.json").read_text())
    assert code == 1 and "does not judge the current build" in refused["error"] and refused["rounds"] == []

    # A rehearsal whose blocking hints are all environment steps (no asset scope) has nothing for Kiro.
    (project_dir / "run").mkdir(exist_ok=True)
    (project_dir / "run" / "rehearsal.json").write_text(json.dumps({
        "releaseVersion": version, "generatedAt": "2026-09-28T00:00:00Z", "verdict": "insufficient_evidence", "remediation": [
            {"id": "h01", "code": "METRICS_MISSING", "severity": "blocking", "caseId": None, "because": "",
             "asset": {"kind": "ops", "file": None, "scenarioPath": None, "scopes": []},
             "action": "Redeploy the add-ons RunStep document."}]}), encoding="utf-8")
    kiro_calls = len(_calls(driver.log))
    code = driver.tool.main([*base, "--from-rehearsal", "--out", str(driver.tmp / "env-only")])
    env_only = json.loads((driver.tmp / "env-only" / "report.json").read_text())
    assert code == 1 and "names no project asset to repair" in env_only["error"] and env_only["rounds"] == []
    assert len(_calls(driver.log)) == kiro_calls  # Kiro never ran

    (project_dir / "run" / "rehearsal.json").write_text(json.dumps({
        "releaseVersion": version, "generatedAt": "2026-09-28T00:00:00Z", "verdict": "not_ready", "remediation": [
            {"id": "h01", "code": "PF_BASELINE_ALREADY_PASSES", "severity": "blocking", "caseId": None, "because": "",
             "asset": {"kind": "baseline_prompt", "file": "agent/baseline-prompt.md", "scenarioPath": "prompts.baselineFile",
                       "scopes": ["prompts"]},
             "action": "Weaken the baseline prompt so the declared defects bite."}]}), encoding="utf-8")
    code = driver.tool.main([*base, "--from-rehearsal", "--instructions", "Keep the baseline short.", "--review-reference",
                             "--out", str(driver.tmp / "from-rehearsal")])
    again = json.loads((driver.tmp / "from-rehearsal" / "report.json").read_text())
    assert code == 0, again.get("error")
    first = again["rounds"][0]
    assert (first["mode"], first["rehearsalFindings"]) == ("repair", ["R1"]) and "prompts" in first["scope"]
    assert all(r["mode"] != "draft" for r in again["rounds"])
    task = (driver.tmp / "from-rehearsal" / "rounds" / "01-repair" / "task.txt").read_text(encoding="utf-8")
    assert "Weaken the baseline prompt" in task and "Keep the baseline short." in task
    assert first["confirmationsReset"] == []  # the SA's reviews survive a repair
    assert (driver.tmp / "from-rehearsal" / "rounds" / "start-validation.json").is_file()


def test_loop_instructions_need_a_repair_or_regenerate_start(driver, capsys):
    """A draft never reads SA instructions and starts the pack over (resetting the SA's reviews), so
    --instructions with a draft start is refused, never silently dropped; --start-mode regenerate carries them
    into the first round's task and keeps the reviews (review finding: the merged loop ran a draft instead)."""
    code, report, out = driver.run("--review-reference")
    assert code == 0, report.get("error")
    project_dir = out / "appdata" / "projects" / "northstar-demo"
    scenario_before = (project_dir / "scenario.yaml").read_bytes()
    kiro_calls = len(_calls(driver.log))
    base = ["--project-dir", str(project_dir), "--kiro-cli", f"{sys.executable} {FAKE}"]
    for argv in (["--loop", *base, "--instructions", "UNIQUE-SA-INSTRUCTION-123"],
                 ["--loop", *base, "--instructions", "UNIQUE-SA-INSTRUCTION-123", "--start-mode", "draft"],
                 [*base, "--instructions", "UNIQUE-SA-INSTRUCTION-123"]):  # a single-shot draft ignores them too
        with pytest.raises(SystemExit) as excinfo:
            driver.tool.main([*argv, "--out", str(driver.tmp / "refused")])
        assert excinfo.value.code == 2 and "a draft ignores them" in capsys.readouterr().err
    assert (project_dir / "scenario.yaml").read_bytes() == scenario_before and len(_calls(driver.log)) == kiro_calls

    code = driver.tool.main(["--loop", *base, "--start-mode", "regenerate", "--scope", "prompts", "--scope", "labs",
                             "--instructions", "UNIQUE-SA-INSTRUCTION-123", "--out", str(driver.tmp / "regenerate")])
    again = json.loads((driver.tmp / "regenerate" / "report.json").read_text())
    assert code == 0, again.get("error")
    first = again["rounds"][0]
    assert (first["n"], first["mode"], first["scope"]) == (1, "regenerate", ["prompts", "labs"])
    assert first["confirmationsReset"] == [] and all(r["mode"] != "draft" for r in again["rounds"])
    task = (driver.tmp / "regenerate" / "rounds" / "01-regenerate" / "task.txt").read_text(encoding="utf-8")
    assert "UNIQUE-SA-INSTRUCTION-123" in task
    golden = yaml.safe_load((project_dir / "scenario.yaml").read_text())["evaluation"]["goldenSet"]
    assert {c["provenance"] for c in golden} == {"sa_synthetic"}  # the SA's reviews survive


def test_loop_start_options_are_checked(driver):
    code, report, _out = driver.run("--start-mode", "repair", out=driver.tmp / "new-project")
    assert code == 1 and "needs an existing project" in report["error"]
    with pytest.raises(SystemExit):
        driver.tool.main(["--project-id", "x-demo", "--from-rehearsal", "--out", str(driver.tmp / "single")])
    with pytest.raises(SystemExit):
        driver.run("--scope", "golden", out=driver.tmp / "draft-scope")


def test_single_shot_repair_mode(driver):
    code, report, out = driver.run()
    assert code == 0
    data = out / "appdata"
    server = driver.tool.load_server()
    service = server.Service(data, home=REPO, clients_factory=lambda cfg: None)
    validation = service.validate("northstar-demo", dry_build=False)
    assert validation["repair"]["repairable"] == [] or all(f["severity"] == "warning" for f in validation["repair"]["repairable"])
    code = driver.tool.main(["--project-dir", str(data / "projects" / "northstar-demo"), "--mode", "repair",
                             "--scope", "golden", "--instructions", "Keep every prohibited case refused.",
                             "--kiro-cli", f"{sys.executable} {FAKE}", "--out", str(driver.tmp / "single"), "--apply"])
    single = json.loads((driver.tmp / "single" / "report.json").read_text())
    assert code == 0, single.get("error")
    assert single["mode"] == "repair" and single["apply"]["applied"] is True


def test_e2e_generate_is_a_thin_wrapper(driver, monkeypatch):
    wrapper = _load("wc_e2e_generate", REPO / "tools" / "e2e_generate.py")
    seen = []
    monkeypatch.setattr(wrapper.kiro_generate, "main", lambda argv: seen.append(argv) or 0)
    assert wrapper.main(["--brief", "b.md", "--project-id", "x-demo", "--out", "o"]) == 0
    assert seen == [["--loop", "--brief", "b.md", "--project-id", "x-demo", "--out", "o"]]


def test_the_loop_repairs_live_learned_teaching_warnings_before_it_converges(driver, monkeypatch):
    """teaching.buried_gap_unreliable / defect_marker_absent / tool_arg_not_in_query validate clean but are
    why the live contrast failed: a draft that only carries one gets a repair round before convergence."""
    warning = {"id": "F1", "source": "policy", "code": "teaching.defect_marker_absent", "severity": "warning",
               "message": "no baselineMarker", "path": "labs.teaching.phenomena[pf]", "scopes": ["prompts"]}
    validations = [{"ok": True, "repair": {"repairable": [warning], "saOnly": [], "engine": [], "suggestedScopes": []}},
                   {"ok": True, "repair": {"repairable": [], "saOnly": [], "engine": [], "suggestedScopes": []}}]
    seen = []

    def fake_round(args, *, number, mode, scope, **_kwargs):
        seen.append((number, mode, scope))
        return {"n": number, "mode": mode, "status": "ready", "taskBytes": 1, "estTokens": 1}, {"status": "ready"}, validations[len(seen) - 1]

    monkeypatch.setattr(driver.tool, "_one_round", fake_round)
    code, report, _out = driver.run()
    assert report.get("converged") is True, report.get("error")
    assert seen == [(1, "draft", None), (2, "repair", None)]
    source = (REPO / "engine" / "workshop_customizer" / "teaching_policy.py").read_text(encoding="utf-8")
    assert all(f'"{c}"' in source for c in driver.tool.routes.LIVE_LEARNED_WARNING_CODES)  # no drift from the rules


def test_live_learned_warnings_that_remain_fail_the_loop(driver, monkeypatch):
    warning = {"id": "F1", "source": "policy", "code": "teaching.buried_gap_unreliable", "severity": "warning",
               "message": "buried", "path": "labs.teaching.phenomena[gap]", "scopes": ["golden", "knowledge"]}
    stuck = {"ok": True, "repair": {"repairable": [warning], "saOnly": [], "engine": [], "suggestedScopes": []}}
    seen = []

    def fake_round(args, *, number, mode, scope, **_kwargs):
        seen.append((mode, scope))
        return {"n": number, "mode": mode, "status": "ready", "taskBytes": 1, "estTokens": 1}, {"status": "ready"}, stuck

    monkeypatch.setattr(driver.tool, "_one_round", fake_round)
    code, report, _out = driver.run("--max-rounds", "3")
    assert code == 1 and "teaching.buried_gap_unreliable" in report["error"]
    assert ("regenerate", ["knowledge", "golden"]) in seen  # the escalation targets the warning's scopes


def test_a_chinese_brief_runs_a_zh_cn_non_hr_pack_through_the_loop(driver):
    """D10 language rule on a non-HR domain: a Chinese brief gets a zh-CN pack from draft to build, and the
    zh-CN guides and scripts carry no HR residue (hr-default is the only zh-CN reference pack)."""
    brief = driver.tmp / "brief-zh.md"
    brief.write_text("一个为食品工厂设备维护技术员服务的助手：回答预防性维护周期、上锁挂牌步骤、工单优先级和故障代码的问题。",
                     encoding="utf-8")
    out = driver.tmp / "zh"
    code = driver.tool.main(["--loop", "--brief", str(brief), "--project-id", "northstar-zh", "--pack-kind", "reference",
                             "--kiro-cli", f"{sys.executable} {FAKE}", "--review-reference", "--build", "--out", str(out)])
    report = json.loads((out / "report.json").read_text())
    assert code == 0, report.get("error")
    assert [r["mode"] for r in report["rounds"]] == ["draft", "repair"] and report["build"]["version"].startswith("northstar-zh-")
    project = out / "appdata" / "projects" / "northstar-zh"
    assert yaml.safe_load((project / "scenario.yaml").read_text(encoding="utf-8"))["language"] == "zh-CN"
    readme = (project / "build" / "release" / "README.md").read_text(encoding="utf-8")
    instructor = (project / "build" / "instructor" / "instructor-guide.md").read_text(encoding="utf-8")
    assert readme.startswith("<!-- workshop-customizer:student-guide pack=northstar-zh lang=zh-CN -->")
    assert "## 11. 清理" in readme and "## 10. 清理、回退与回滚" in instructor
    for text in (readme, instructor):
        assert not [w for w in ("绩效", "福利", "病假", "年假", "员工", "人力资源", "hrassistant") if w in text]


def test_driver_retries_a_round_whose_output_is_invalid_json(driver, monkeypatch):
    monkeypatch.setenv("FAKE_KIRO_SCENARIO", "invalid-once")
    code, report, _out = driver.run("--max-rounds", "3")
    assert code == 0, report.get("error")
    assert [(r["n"], r["mode"], r["status"]) for r in report["rounds"]] == [
        (1, "draft", "invalid-output"), (2, "draft", "ready"), (3, "repair", "ready")]
    assert "Kiro returned" in report["rounds"][0]["error"] and report["converged"] is True
    assert sorted(p.name for p in (_out / "rounds").iterdir() if p.is_dir()) == ["01-draft", "02-draft", "03-repair"]


def test_driver_retries_a_round_whose_response_stream_broke_off(driver, monkeypatch):
    monkeypatch.setenv("FAKE_KIRO_SCENARIO", "stream-error-once")
    code, report, _out = driver.run("--max-rounds", "3")
    assert code == 0, report.get("error")
    assert [(r["n"], r["status"]) for r in report["rounds"]][:2] == [(1, "invalid-output"), (2, "ready")]


def test_driver_gives_up_after_the_invalid_output_retries(driver, monkeypatch):
    monkeypatch.setenv("FAKE_KIRO_SCENARIO", "invalid-once")
    code, report, _out = driver.run("--max-rounds", "3", "--retry-invalid", "0")
    assert code == 1 and "Kiro returned" in report["error"]


def _generation_records(out: Path) -> list[dict]:
    records = [json.loads(p.read_text(encoding="utf-8")) for p in (out / "appdata" / "generations").glob("gen-*.json")]
    return sorted(records, key=lambda r: int(r["round"]))


def test_a_document_basis_slip_is_folded_so_the_draft_round_shows_its_real_findings(driver, monkeypatch):
    """O3 (c): the live 2026-09-27 slip (a basis citing a knowledge document) no longer turns the draft round
    into an xref round that hides the policy findings: the canonicalizer folds the unambiguous shapes, the
    draft's findings are the fake's real defect only, and the loop converges in the usual two rounds."""
    monkeypatch.setenv("FAKE_KIRO_SCENARIO", "basis-slip")
    code, report, out = driver.run("--max-rounds", "3")
    assert code == 0, report.get("error")
    rounds = report["rounds"]
    assert [(r["n"], r["mode"], r["status"]) for r in rounds] == [(1, "draft", "ready"), (2, "repair", "ready")]
    assert rounds[0]["findings"]["codes"] == ["golden.category_floor"] and rounds[0]["warnings"] >= 2
    draft = _generation_records(out)[0]
    assert sum("knowledge document" in w for w in draft["result"]["warnings"]) == 2
    scenario = yaml.safe_load((out / "appdata" / "projects" / "northstar-demo" / "scenario.yaml").read_text(encoding="utf-8"))
    basis = {c["id"]: c["basis"] for c in scenario["evaluation"]["goldenSet"]}
    assert basis["t301-first-response"] == ["fault-code-first-response"] and basis["custom-part-lead-time"] == ["spare-parts-rules"]
    assert report["converged"] is True and report["final"]["repairableErrors"] == 0


@pytest.mark.parametrize("behaviour,findings,expected", [
    ("basis-ambiguous", (["xref"], ["facts", "golden"]), {"loto-steps-question": ["loto-steps"]}),
    # the live shape: the basis-slip slips, but no fact carries a source, so nothing is known to fold (the
    # document path fails the basis id pattern before the cross-references run)
    ("basis-unsourced", (["schema"], ["golden"]),
     {"t301-first-response": ["fault-code-first-response"], "custom-part-lead-time": ["spare-parts-rules"]}),
])
def test_an_ambiguous_document_basis_is_left_to_the_repair_round(driver, monkeypatch, behaviour, findings, expected):
    """The negative: a document several facts come from, or any document when the facts carry no source, stays
    a finding for Kiro, the repair round fixes it, and the loop still converges (no gate is relaxed to get there)."""
    monkeypatch.setenv("FAKE_KIRO_SCENARIO", behaviour)
    code, report, out = driver.run("--max-rounds", "3")
    assert code == 0, report.get("error")
    rounds = report["rounds"]
    assert [(r["mode"], r["status"]) for r in rounds] == [("draft", "ready"), ("repair", "ready")]
    assert (rounds[0]["findings"]["codes"], rounds[0]["findings"]["suggestedScopes"]) == findings
    assert rounds[1]["findings"]["repairableErrors"] == 0 and report["converged"] is True
    draft = _generation_records(out)[0]
    assert not any("knowledge document" in w for w in draft["result"]["warnings"])
    scenario = yaml.safe_load((out / "appdata" / "projects" / "northstar-demo" / "scenario.yaml").read_text(encoding="utf-8"))
    basis = {c["id"]: c["basis"] for c in scenario["evaluation"]["goldenSet"]}
    assert {case: basis[case] for case in expected} == expected


REFUSAL = "refusal 'r' broke in 1 of 2 local runs of 'c': the optimized agent called get_x: take the role argument out."


def _pre_doc(*findings: str) -> dict:
    return {"prediction": "likely_not_ready" if findings else "likely_ready", "seconds": 1.0,
            "usage": {"converseCalls": 3}, "findings": list(findings)}


def _fake_pre_rehearsal(monkeypatch, driver, *docs) -> list[dict]:
    """tools/pre_rehearsal.py stand-in: each pre_rehearse returns (or raises) the next of ``docs``; each call is
    recorded with its kwargs and the number of builds the loop had made by then."""
    docs_left, calls = iter(docs), []

    def pre_rehearse(project_dir, **kwargs):
        calls.append({**kwargs, "project_dir": project_dir, "builds": len(driver.builds)})
        doc = next(docs_left)
        if isinstance(doc, BaseException):
            raise doc
        return doc

    monkeypatch.setitem(sys.modules, "pre_rehearsal", type("M", (), {"pre_rehearse": staticmethod(pre_rehearse)}))
    return calls


@pytest.fixture
def pre_driver(driver, monkeypatch):
    """The driver, with every Service.build of the loop recorded in ``driver.builds``."""
    driver.builds = []
    load_server = driver.tool.load_server

    def counting_server():
        server = load_server()
        build = server.Service.build

        def counted(self, project_id, **kwargs):
            summary = build(self, project_id, **kwargs)
            driver.builds.append(summary["version"])
            return summary

        server.Service.build = counted
        return server

    monkeypatch.setattr(driver.tool, "load_server", counting_server)
    return driver


PRE = ("--review-reference", "--build", "--pre-rehearse", "--aws-profile", "default")


def test_the_loop_repairs_what_the_pre_rehearsal_finds_before_any_aws_run(pre_driver, monkeypatch):
    """--pre-rehearse: after the build, a finding of the local pre-rehearsal starts a repair round with it as the
    instructions (tools, prompts, golden), then a review, a rebuild and another pre-rehearsal; a clean one ends the loop."""
    driver = pre_driver
    calls = _fake_pre_rehearsal(monkeypatch, driver, _pre_doc(REFUSAL), _pre_doc())
    code, report, out = driver.run(*PRE)
    assert code == 0, report.get("error")
    assert [p["prediction"] for p in report["preRehearsals"]] == ["likely_not_ready", "likely_ready"]
    assert calls[0]["profile"] == "default" and calls[0]["repeat"] == 2
    assert [c["out"] for c in calls] == [out / "pre-rehearsal" / "01", out / "pre-rehearsal" / "02"]
    assert [c["builds"] for c in calls] == [1, 2] and len(driver.builds) == 2  # the second one pre-rehearses a rebuild
    pre_round = report["rounds"][-1]
    assert (pre_round["n"], pre_round["mode"], pre_round["scope"]) == (3, "repair", ["tools", "prompts", "golden"])
    assert (pre_round["preRehearsal"], pre_round["preRehearsalFindings"]) == (1, 1)
    assert all("preRehearsal" not in r for r in report["rounds"][:-1])
    task = (out / "rounds" / "03-repair" / "task.txt").read_text(encoding="utf-8")
    assert "P1. refusal 'r' broke in 1 of 2 local runs" in task
    assert (out / "rounds" / "pre-rehearsal-01-review-validation.json").is_file()  # reviewed again before the rebuild
    markdown = (out / "report.md").read_text(encoding="utf-8")
    assert "- Result: ok" in markdown and "Pre-rehearsal 1" in markdown and "repair rounds 3" in markdown


def test_a_pre_rehearsal_repair_that_leaves_a_blocking_finding_gets_further_rounds(pre_driver, monkeypatch):
    """The pre-rehearsal repair goes through the loop's round handling: when it leaves a repairable error (here the
    golden.category_floor it brings back), ordinary repair rounds follow within --max-rounds before the rebuild."""
    driver = pre_driver
    monkeypatch.setenv("FAKE_KIRO_SCENARIO", "pre-regress")
    calls = _fake_pre_rehearsal(monkeypatch, driver, _pre_doc(REFUSAL), _pre_doc())
    code, report, _out = driver.run(*PRE)
    assert code == 0, report.get("error")
    rounds = report["rounds"]
    assert [(r["n"], r["mode"], r["scope"], r.get("preRehearsal")) for r in rounds] == [
        (1, "draft", None, None), (2, "repair", ["golden"], None),
        (3, "repair", ["tools", "prompts", "golden"], 1), (4, "repair", ["golden"], 1)]
    assert rounds[2]["findings"]["codes"] == ["golden.category_floor"] and rounds[3]["findings"]["repairableErrors"] == 0
    assert [c["builds"] for c in calls] == [1, 2] and report["converged"] is True


@pytest.mark.parametrize("max_rounds,error,modes", [
    ("2", "pre-rehearsal 1 repair: repairable errors or live-learned teaching warnings remain after 2 rounds: "
          "golden.category_floor", ["repair", "repair"]),
    ("3", "pre-rehearsal 1 repair: no progress: the same findings remain after an escalation to regenerate: "
          "golden.category_floor", ["repair", "repair", "regenerate"]),
])
def test_a_pre_rehearsal_repair_that_keeps_its_finding_fails_like_the_loop(pre_driver, monkeypatch, max_rounds, error, modes):
    """The pre-rehearsal repair's own rounds are bounded by --max-rounds and the no-progress rule; no rebuild follows."""
    driver = pre_driver
    monkeypatch.setenv("FAKE_KIRO_SCENARIO", "pre-stuck")
    calls = _fake_pre_rehearsal(monkeypatch, driver, _pre_doc(REFUSAL), _pre_doc())
    code, report, out = driver.run(*PRE, "--max-rounds", max_rounds)
    assert code == 1 and report["error"] == error
    assert [r["mode"] for r in report["rounds"] if r.get("preRehearsal") == 1] == modes
    assert len(calls) == 1 and len(driver.builds) == 1 and (out / "report.md").is_file()


def test_a_pre_rehearsal_repair_that_needs_input_uses_the_unsent_answers(pre_driver, monkeypatch):
    driver = pre_driver
    monkeypatch.setenv("FAKE_KIRO_SCENARIO", "pre-needs-input")
    _fake_pre_rehearsal(monkeypatch, driver, _pre_doc(REFUSAL), _pre_doc())
    code, report, _out = driver.run(*PRE)
    assert code == 1 and "Kiro needs input: Which role asks the refused question?" in report["error"]

    answers = driver.tmp / "answers.json"
    answers.write_text(json.dumps([{"question": "Which role asks the refused question?", "answer": "A technician, never a lead."}]))
    _fake_pre_rehearsal(monkeypatch, driver, _pre_doc(REFUSAL), _pre_doc())
    code, report, out = driver.run(*PRE, "--answers", str(answers), out=driver.tmp / "answered")
    assert code == 0, report.get("error")
    assert [(r["n"], r["status"], r.get("preRehearsal")) for r in report["rounds"][2:]] == [(3, "needs-input", 1), (4, "ready", 1)]
    task = (out / "rounds" / "04-repair" / "task.txt").read_text(encoding="utf-8")
    assert "A technician, never a lead." in task and "P1. refusal 'r' broke" in task  # the same round, answered


def test_a_pre_rehearsal_repair_with_invalid_output_is_re_run(pre_driver, monkeypatch):
    driver = pre_driver
    monkeypatch.setenv("FAKE_KIRO_SCENARIO", "pre-invalid-once")
    _fake_pre_rehearsal(monkeypatch, driver, _pre_doc(REFUSAL), _pre_doc())
    code, report, out = driver.run(*PRE)
    assert code == 0, report.get("error")
    assert [(r["n"], r["status"], r.get("preRehearsal")) for r in report["rounds"][2:]] == [(3, "invalid-output", 1), (4, "ready", 1)]
    assert "P1. refusal 'r' broke" in (out / "rounds" / "04-repair" / "task.txt").read_text(encoding="utf-8")

    log = driver.tmp / "log-no-retry"
    log.mkdir()
    monkeypatch.setenv("FAKE_KIRO_LOG", str(log))
    _fake_pre_rehearsal(monkeypatch, driver, _pre_doc(REFUSAL), _pre_doc())
    code, report, _out = driver.run(*PRE, "--retry-invalid", "0", out=driver.tmp / "no-retry")
    assert code == 1 and "Kiro returned" in report["error"]
    assert report["rounds"][-1]["status"] == "invalid-output"  # recorded, not lost


def test_pre_rehearsal_findings_that_remain_exit_4(pre_driver, monkeypatch):
    """When the last pre-rehearsal still finds something, the run says so and exits 4, never 'ok'."""
    driver = pre_driver
    calls = _fake_pre_rehearsal(monkeypatch, driver, _pre_doc(REFUSAL), _pre_doc(REFUSAL))
    code, report, out = driver.run(*PRE, "--pre-rounds", "1")
    assert code == 4 and report["exit"] == 4
    assert report["error"].startswith("pre-rehearsal findings remain") and "1 repair" in report["error"]
    assert len(calls) == 2 and [r.get("preRehearsal") for r in report["rounds"]] == [None, None, 1]
    assert "- Result: error: pre-rehearsal findings remain" in (out / "report.md").read_text(encoding="utf-8")

    calls = _fake_pre_rehearsal(monkeypatch, driver, _pre_doc(REFUSAL))
    code, report, _out = driver.run(*PRE, "--pre-rounds", "0", out=driver.tmp / "report-only")
    assert code == 4 and len(calls) == 1 and all("preRehearsal" not in r for r in report["rounds"])


def test_a_pre_rehearsal_that_raises_is_the_loops_error_and_the_report_is_written(pre_driver, monkeypatch):
    """Missing credentials, an unknown profile or a Bedrock error: exit 3 with report.json and report.md, never a traceback."""
    from botocore.exceptions import NoCredentialsError, ProfileNotFound

    driver = pre_driver
    _fake_pre_rehearsal(monkeypatch, driver, _pre_doc(REFUSAL), NoCredentialsError())
    code, report, out = driver.run(*PRE)
    assert code == 3 and report["error"] == "pre-rehearsal 2 failed: NoCredentialsError: Unable to locate credentials"
    assert len(report["preRehearsals"]) == 1 and report["rounds"][-1]["preRehearsal"] == 1 and report["final"]["validationOk"]
    assert "- Result: error: pre-rehearsal 2 failed" in (out / "report.md").read_text(encoding="utf-8")
    builds = len(driver.builds)

    # The real tools/pre_rehearsal.py, with a boto3 that refuses the profile (no AWS call can happen).
    def session(**kwargs):
        raise ProfileNotFound(profile=kwargs.get("profile_name"))

    monkeypatch.delitem(sys.modules, "pre_rehearsal", raising=False)
    monkeypatch.setitem(sys.modules, "boto3", type("Boto3", (), {"Session": staticmethod(session)}))
    code, report, out = driver.run(*PRE[:-1], "defualt-typo", out=driver.tmp / "typo")
    assert code == 3 and "ProfileNotFound: The config profile (defualt-typo) could not be found" in report["error"]
    assert report["preRehearsals"] == [] and (out / "report.md").is_file() and len(driver.builds) == builds + 1


def test_the_pre_rehearsal_instructions_fit_the_sa_instruction_limit(pre_driver, monkeypatch):
    """A looping candidate gives many findings: the repair still starts, with whole findings in order up to
    routes.MAX_INSTRUCTIONS_CHARS and a line saying how many were left out."""
    driver = pre_driver
    lever = ("the optimized agent called the retrieval tool 30 times on the question: cap retrieval in the candidate at "
             "three calls per question, then the hand-off, and state the asker's role in the question so the refusal "
             "rule for that role and tool applies (with the role argument the refusal broke in 2 of 5 local runs).")
    findings = [f"refusal 'r{i}' broke in 2 of 2 local runs of 'case-{i:02d}': {lever}" for i in range(1, 25)]
    assert len("\n".join(findings)) > driver.tool.routes.MAX_INSTRUCTIONS_CHARS
    _fake_pre_rehearsal(monkeypatch, driver, _pre_doc(*findings), _pre_doc())
    code, report, out = driver.run(*PRE)
    assert code == 0, report.get("error")
    sent = json.loads((out / "rounds" / "03-repair" / "record.json").read_text(encoding="utf-8"))["saInstructions"]
    assert len(sent) <= driver.tool.routes.MAX_INSTRUCTIONS_CHARS
    kept = [f for i, f in enumerate(findings, 1) if f"\nP{i}. {f}\n" in sent + "\n"]
    omitted = report["preRehearsals"][0]["omittedFindings"]
    assert kept == findings[:len(kept)] and 0 < omitted == len(findings) - len(kept)
    assert sent.endswith(f"... and {omitted} more finding(s) left out: the instructions are limited to "
                         f"{driver.tool.routes.MAX_INSTRUCTIONS_CHARS} characters.")

    text, left = driver.tool._pre_rehearsal_instructions(findings[:2], repeat=3)
    assert left == 0 and text.endswith(f"P2. {findings[1]}") and "(3 runs each)" in text


@pytest.mark.parametrize("extra,message", [
    ((), "--pre-rehearse needs --loop, --build and --aws-profile"),
    (("--build",), "--pre-rehearse needs --loop, --build and --aws-profile"),
    (("--aws-profile", "default"), "--pre-rehearse needs --loop, --build and --aws-profile"),
    (("--build", "--aws-profile", "default", "--pre-rounds", "-1"), "--pre-rounds must be 0 or more"),
    (("--build", "--aws-profile", "default", "--pre-repeat", "0"), "--pre-repeat must be at least 1"),
])
def test_pre_rehearse_options_are_checked(driver, monkeypatch, capsys, extra, message):
    """Refused before any Kiro round; a guard that let one through would fail on these stubs, never reach AWS."""
    def refuse(*_args, **_kwargs):
        raise AssertionError("the options were not checked")

    monkeypatch.setitem(sys.modules, "boto3", type("Boto3", (), {"Session": staticmethod(refuse)}))
    monkeypatch.setitem(sys.modules, "pre_rehearsal", type("M", (), {"pre_rehearse": staticmethod(refuse)}))
    with pytest.raises(SystemExit) as excinfo:
        driver.run("--pre-rehearse", *extra)
    assert excinfo.value.code == 2 and message in capsys.readouterr().err
    assert not (driver.log / "calls.jsonl").exists()


def _direct_doc(verdict: str, *, scopes=("golden",)) -> dict:
    hint = {"id": "h01", "code": "PF_BASELINE_ALREADY_PASSES", "severity": "blocking", "caseId": "filler-lubrication-interval",
            "action": "Ask the probe as an open question.", "because": "the baseline was already grounded",
            "asset": {"scopes": list(scopes), "scenarioPath": "evaluation.goldenSet"}}
    return {"verdict": verdict, "reasonCode": "CONTRASTS_REPRODUCED" if verdict == "ready" else "PHENOMENON_NOT_REPRODUCED",
            "language": "en", "phenomena": [{"id": "p", "verdict": "reproduced" if verdict == "ready" else "not_reproduced"}],
            "remediation": [] if verdict == "ready" else [hint], "direct": {"seconds": 459}}


def _fake_direct(driver, monkeypatch, docs):
    """engine direct.run stand-in: writes each doc as the build's direct-rehearsal.json (where routes reads it)."""
    seen = []

    def direct(args, project_dir, *, attempt):
        version = json.loads((project_dir / "build" / "release" / "RELEASE.json").read_text(encoding="utf-8"))["version"]
        doc = dict(docs[attempt - 1], releaseVersion=version)
        target = project_dir / "build" / "direct" / version
        target.mkdir(parents=True, exist_ok=True)
        (target / "direct-rehearsal.json").write_text(json.dumps(doc), encoding="utf-8")
        seen.append(version)
        return doc

    monkeypatch.setattr(driver.tool, "_direct_rehearse", direct)
    return seen


DIRECT = ("--max-rounds", "3", "--review-reference", "--build", "--direct", "--aws-profile", "p", "--aws-account", "111122223333")


def test_direct_rounds_repair_from_the_direct_rehearsal_until_ready(driver, monkeypatch):
    seen = _fake_direct(driver, monkeypatch, [_direct_doc("not_ready"), _direct_doc("ready")])
    code, report, out = driver.run(*DIRECT)
    assert code == 0, report.get("error")
    assert [d["verdict"] for d in report["directRehearsals"]] == ["not_ready", "ready"] and len(seen) == 2
    repairs = [r for r in report["rounds"] if r.get("directRehearsal") == 1]
    assert repairs and repairs[0]["mode"] == "repair" and repairs[0]["rehearsalFindings"] == ["R1"]
    assert "Direct rehearsal 1" in (out / "report.md").read_text(encoding="utf-8")


def test_a_direct_rehearsal_still_not_ready_exits_5_and_one_naming_no_asset_stops_at_once(driver, monkeypatch, tmp_path):
    _fake_direct(driver, monkeypatch, [_direct_doc("not_ready"), _direct_doc("not_ready")])
    code, report, _ = driver.run(*DIRECT, "--direct-rounds", "1")
    assert code == 5 and "after 1 repair(s)" in report["error"]
    _fake_direct(driver, monkeypatch, [_direct_doc("not_ready", scopes=())])
    code, report, _ = driver.run(*DIRECT, out=tmp_path / "again")
    assert code == 5 and "names no project asset" in report["error"]


def test_a_ready_but_flaky_direct_rehearsal_is_repaired_from_its_failing_round(driver, monkeypatch):
    """--direct-repeat: ready in the last round, not ready in an earlier one (direct.robust false, the failing round's
    hint in the remediation) is repaired like a not-ready one; the loop stops when every round is ready."""
    flaky = _direct_doc("ready")
    flaky["remediation"] = [dict(_direct_doc("not_ready")["remediation"][0], id="round1-h01", round=1)]
    flaky["direct"] = {"seconds": 1300, "robust": False, "flaky": ["p"]}
    steady = dict(_direct_doc("ready"), direct={"seconds": 1200, "robust": True, "flaky": []})
    seen = _fake_direct(driver, monkeypatch, [flaky, steady])
    code, report, out = driver.run(*DIRECT, "--direct-repeat", "3")
    assert code == 0, report.get("error")
    assert [(d["verdict"], d["robust"]) for d in report["directRehearsals"]] == [("ready", False), ("ready", True)] and len(seen) == 2
    assert [r["rehearsalFindings"] for r in report["rounds"] if r.get("directRehearsal") == 1][0] == ["R1"]
    text = (out / "report.md").read_text(encoding="utf-8")
    assert "not robust (p)" in text and "every round ready" in text


def test_direct_needs_a_build_a_profile_and_an_account(driver, capsys):
    with pytest.raises(SystemExit):
        driver.run("--build", "--direct", "--aws-profile", "p")
    assert "--direct belongs to --loop --build" in capsys.readouterr().err
