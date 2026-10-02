"""Tests for release rendering, release verification and the generic tools handler."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from workshop_customizer import compiler, render
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = REPO_ROOT / "upstream"
HR_SCENARIO = REPO_ROOT / "scenarios" / "hr-default" / "scenario.yaml"
TEMPLATE_COMMIT = json.loads((REPO_ROOT / "template-lock.json").read_text())["template"]["commit"]

IT_NAMESPACE = {
    "agentName": "itassistant",
    "toolTargetName": "it-tools",
    "gatewayName": "itgateway",
    "knowledgeBaseName": "it-knowledge-base",
    "kbPrefix": "it/",
    "ssmParameterPrefix": "/app/it",
    "lambdaFunctionName": "it-tools-handler",
}


@pytest.fixture(scope="module")
def hr_scenario():
    return load_scenario(HR_SCENARIO)


@pytest.fixture(scope="module")
def hr_release(tmp_path_factory, hr_scenario):
    base = tmp_path_factory.mktemp("hr")
    pack = compiler.compile_pack(hr_scenario, base / "build", template_commit=TEMPLATE_COMMIT)
    return render.render_release(pack, UPSTREAM, base / "release", template_commit=TEMPLATE_COMMIT)


def it_scenario(tmp_path: Path, hr_scenario):
    """hr-default content re-namespaced as a non-HR pack (namespace-rendering test double)."""
    root = tmp_path / "it-scenario"
    shutil.copytree(HR_SCENARIO.parent, root)
    data = json.loads(json.dumps(hr_scenario.data))
    data["id"] = "it-helpdesk"
    data["displayName"] = "IT Helpdesk (namespace test)"
    data["namespace"] = dict(IT_NAMESPACE)
    for tool in data["tools"]:
        if tool["name"] == "retrieve_hr_policy":
            tool["name"] = "retrieve_it_policy"
    data["evaluation"]["retrievalToolName"] = "retrieve_it_policy"
    for case in data["evaluation"]["goldenSet"]:
        for key in ("requiredTools", "forbiddenTools"):
            case["expected"][key] = [
                "retrieve_it_policy" if t == "retrieve_hr_policy" else t for t in case["expected"].get(key, [])
            ]
    (root / "scenario.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    (root / "scenario.yaml").unlink()
    # A real non-HR pack authors its own prompts and skills; the double rewrites the tool
    # references so the residue gate measures the renderer, not the borrowed HR prose.
    for path in list((root / "agent").glob("*.md")) + list((root / "skills").rglob("SKILL.md")):
        text = path.read_text(encoding="utf-8").replace("retrieve_hr_policy", "retrieve_it_policy").replace("hr-tools", "it-tools")
        path.write_text(text, encoding="utf-8")
    return load_scenario(root / "scenario.json")


def test_hr_release_layout_and_manifest(hr_release):
    rel = hr_release.release_dir
    for expected in [
        "RELEASE.json", "00-config.sh", "01-create-kb.sh", "02-create-gateway.sh", "03-configure-skills.sh",
        "04-deploy.sh", "09-run-eval.sh", "10-optimize-prompt.sh", "99-cleanup.sh", "LICENSE",
        "cfn/workshop-infra.yaml", "gateway/create_gateway.py", "gateway/hr-tools-schema.json",
        "knowledge-base/create_kb.py", "lambda/scenario_tools_handler.py", "lambda/fixtures.json",
        "pack/pack.env", "pack/golden/practice.json", "evaluators/thelma_eval/span_adapter.py", "l1_eval.py",
    ]:
        assert (rel / expected).is_file(), expected
    for removed in ["knowledge-base/generate_hr_docs.py", "knowledge-base/domain_faqs.py", "lambda/hr_tools_handler.py",
                    "README.zh-CN.md", "assets", "docs", "CONTRIBUTING.md", "instructor"]:
        assert not (rel / removed).exists(), removed
    # SPEC D10: the release root README.md is the scenario's generated student guide, not the upstream README.
    readme = (rel / "README.md").read_bytes()
    assert readme == (rel / "pack" / "labs" / "student-guide.md").read_bytes()
    assert readme.startswith(b"<!-- workshop-customizer:student-guide pack=hr-default lang=zh-CN -->\n")
    assert readme != (UPSTREAM / "README.md").read_bytes() and readme != (UPSTREAM / "README.zh-CN.md").read_bytes()
    manifest = json.loads((rel / "RELEASE.json").read_text())
    assert manifest["packId"] == "hr-default"
    assert manifest["version"] == hr_release.version and manifest["version"].startswith("hr-default-")
    assert manifest["templateCommit"] == TEMPLATE_COMMIT
    assert set(manifest["files"]) == {p.relative_to(rel).as_posix() for p in rel.rglob("*") if p.is_file() and p.name != "RELEASE.json"}
    assert {p["name"] for p in manifest["patches"]} >= {
        "docs-from-pack", "lambda-zip-fixtures", "baseline-prompt-from-pack", "skills-list-from-pack",
        "golden-queries", "golden-labels", "golden-case-arrays", "eval-run-context", "per-case-actor-session-log",
        "candidate-prompt-from-pack", "skill-dirs", "userdata-skill-dirs", "userdata-skill-heredocs", "generated",
        "first-conversation-actor", "first-conversation-topic", "memory-notice-from-pack", "l1-after-judges",
        "mtg-judge-scenario-policy", "kb-prune-stale-documents", "runtime-scoped-lookup", "evaluator-scoped-lookup",
    }
    assert "actor-id" not in {p["name"] for p in manifest["patches"]}  # no shared ACTOR_ID any more (SPEC D3)
    assert manifest["evaluationFeatures"] == ["l1-scenario-assertions/1", "mtg-scenario-policy/1", "per-case-actors/1"]
    assert (rel / "l1_eval.py").read_bytes() == render.L1_SOURCE.read_bytes()


def test_hr_release_patches_are_faithful(hr_release):
    rel = hr_release.release_dir
    s04 = (rel / "04-deploy.sh").read_text()
    assert "--memory longAndShortTerm" not in s04
    assert "add memory --name hrassistantmemory --strategies SEMANTIC,USER_PREFERENCE" in s04
    assert '.memory = {"mode":"existing","name":"hrassistantmemory","actorId":"{actorId}"}' in s04
    assert "SkillsFilesAccessPointArn" in s04 and ".s3AccessPoints =" in s04
    assert ".environment.agentCoreRuntimeEnvironment" not in s04
    assert 'cp "$SCRIPT_DIR/pack/prompts/baseline.md" "$WORKDIR/system-prompt.txt"' in s04
    assert "<< 'PROMPT'" not in s04
    assert "jq '.skills = [\"/mnt/skills/skills/deep-policy-analysis/SKILL.md\", \"/mnt/skills/skills/leave-calculator/SKILL.md\"]'" in s04
    s09 = (rel / "09-run-eval.sh").read_text()
    assert "ACTOR_ID=" not in s09 and '--actor-id "$ACTOR"' in s09
    assert 'local ACTOR="${GOLDEN_ACTORS[$i]}-${RUN_TAG}-q$((i+1))"' in s09
    assert 'GOLDEN_ACTORS=("employee-001" "employee-001" "employee-001" "employee-001" "employee-001" "employee-001")' in s09
    assert 'GOLDEN_IDS=("own-annual-balance" "colleague-salary" "perf-review-process" "sick-leave-certificate" "volunteer-days-gap" "benefits-enrollment-process")' in s09
    assert "GOLDEN_PROBE=(0 0 1 1 1 1)" in s09
    assert "Can you explain the performance review process and the scoring criteria used?" in s09
    assert "How many annual leave days do I have left this year?" in s09
    assert s09.count("GOLDEN_QUERIES=(") == 1
    assert 'EVAL_ROOT="${WORKSHOP_ROOT:-$HOME/workshop}/eval-runs/hrassistant"' in s09
    s10 = (rel / "10-optimize-prompt.sh").read_text()
    assert "ACTOR_ID=" not in s10 and 'ACTOR="${GOLDEN_ACTORS[$i]}-${RUN_TAG}-q$((i+1))"' in s10
    assert 'export WORKSHOP_EVAL_RUN_DIR="$EVAL_ROOT/$RUN_TAG"' in s10
    s06 = (rel / "06-test-conversation.sh").read_text()
    assert '--actor-id "employee-001"' in s06 and 'echo "🗣️  Asking about annual leave policy..."' in s06
    assert "I'd like to know about the annual leave policy." in s06
    assert "the Agent doesn't know your tenure, department, or specific leave balance yet." in s06
    prompts = (rel / render.MTG_PROMPTS_REL).read_text()
    assert "USER_ROLE_LABEL = 'Employee'" in prompts and "employee experience chatbot" not in prompts
    assert 'cp "$SCRIPT_DIR/pack/prompts/optimization-candidate.md" app/hrassistant/system-prompt.md' in s10
    s02 = (rel / "02-create-gateway.sh").read_text()
    assert "zip -j /tmp/hr-tools-lambda.zip scenario_tools_handler.py fixtures.json" in s02
    assert "--handler scenario_tools_handler.lambda_handler" in s02
    s01 = (rel / "01-create-kb.sh").read_text()
    assert 'find "$DOCS_DIR" -mindepth 1 -delete\ncp "$SCRIPT_DIR/pack/knowledge-base/docs/"*.md "$DOCS_DIR/"' in s01
    assert "generate_hr_docs" not in s01.split("\n", 12)[-1]  # only the header comment may mention it
    cfn = (rel / "cfn" / "workshop-infra.yaml").read_text()
    assert "SKILLEOF" not in cfn
    assert "Handler: scenario_tools_handler.lambda_handler" in cfn
    s03 = (rel / "03-configure-skills.sh").read_text()
    assert 'SKILLS="deep-policy-analysis leave-calculator"' in s03
    assert json.loads((rel / "gateway" / "hr-tools-schema.json").read_text()) == json.loads(
        (UPSTREAM / "gateway" / "hr-tools-schema.json").read_text()
    )


def test_hr_release_only_renames_the_lambda_module(hr_release):
    tokens = {t for counts in hr_release.token_counts.values() for t in counts}
    assert tokens == {"hr_tools_handler", "hr-tools___"}


def test_release_is_deterministic(tmp_path: Path, hr_scenario, hr_release):
    pack = compiler.compile_pack(hr_scenario, tmp_path / "build", template_commit=TEMPLATE_COMMIT)
    again = render.render_release(pack, UPSTREAM, tmp_path / "release", template_commit=TEMPLATE_COMMIT)
    assert again.version == hr_release.version
    assert again.files == hr_release.files


def test_verify_release_detects_tampering(tmp_path: Path, hr_release):
    copy = tmp_path / "copy"
    shutil.copytree(hr_release.release_dir, copy)
    assert render.verify_release(copy)["version"] == hr_release.version
    (copy / "04-deploy.sh").write_text((copy / "04-deploy.sh").read_text() + "\ncurl evil\n")
    with pytest.raises(render.RenderError, match="hash mismatch: 04-deploy.sh"):
        render.verify_release(copy)
    (copy / "extra.sh").write_text("echo hi\n")
    with pytest.raises(render.RenderError, match="unexpected file: extra.sh"):
        render.verify_release(copy)


def test_non_hr_namespace_is_rendered_without_residue(tmp_path: Path, hr_scenario):
    scenario = it_scenario(tmp_path, hr_scenario)
    pack = compiler.compile_pack(scenario, tmp_path / "build", template_commit=TEMPLATE_COMMIT)
    release = render.render_release(pack, UPSTREAM, tmp_path / "release", template_commit=TEMPLATE_COMMIT)
    rel = release.release_dir
    assert (rel / "gateway" / "it-tools-schema.json").is_file()
    s04 = (rel / "04-deploy.sh").read_text()
    assert "--name itassistant" in s04 and '"@it-tools/*"' in s04 and "hrassistant" not in s04
    s09 = (rel / "09-run-eval.sh").read_text()
    assert "ittools___retrieve_it_policy" in s09
    kb = (rel / "knowledge-base" / "create_kb.py").read_text()
    assert 'KB_NAME = "it-knowledge-base"' in kb and 'INCLUSION_PREFIX = "it/"' in kb
    assert 'SSM_KB_ID_PARAM = "/app/it/knowledge_base_id"' in kb
    adapter = (rel / "evaluators" / "thelma_eval" / "span_adapter.py").read_text()
    assert "retrieve_it_policy" in adapter and "retrieve_hr_policy" not in adapter
    forbidden = re.compile(r"hrassistant|hr-tools|hrgateway|hr-knowledge-base|/app/hr|retrieve_hr_policy|hr_tools_handler")
    for path in rel.rglob("*"):
        relpath = path.relative_to(rel).as_posix()
        if path.is_file() and path.suffix in {".sh", ".py", ".yaml", ".json", ".md"} and not relpath.startswith("pack/") and relpath != "RELEASE.json":
            assert not forbidden.search(path.read_text()), path
    assert release.version.startswith("it-helpdesk-")


def test_template_drift_fails_closed(tmp_path: Path, hr_scenario):
    drifted = tmp_path / "upstream"
    shutil.copytree(UPSTREAM, drifted, ignore=shutil.ignore_patterns(".git"))
    path = drifted / "04-deploy.sh"
    path.write_text(path.read_text().replace("<< 'PROMPT'", "<< 'SYSPROMPT'"))
    pack = compiler.compile_pack(hr_scenario, tmp_path / "build", template_commit=TEMPLATE_COMMIT)
    with pytest.raises(render.RenderError, match="baseline-prompt-from-pack"):
        render.render_release(pack, drifted, tmp_path / "release", template_commit=TEMPLATE_COMMIT)


# ---------------------------------------------------------------------------
# Generic Lambda handler
# ---------------------------------------------------------------------------


def load_handler(tmp_path: Path, fixtures_path: Path):
    target = tmp_path / "lambda"
    target.mkdir()
    shutil.copyfile(render.TEMPLATES_DIR / "scenario_tools_handler.py", target / "scenario_tools_handler.py")
    shutil.copyfile(fixtures_path, target / "fixtures.json")
    spec = importlib.util.spec_from_file_location(f"handler_{tmp_path.name}", target / "scenario_tools_handler.py")
    module = importlib.util.module_from_spec(spec)
    os.environ.pop("TOOL_FIXTURES_PATH", None)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class _Ctx:
    class client_context:  # noqa: N801 - mirrors the Lambda context shape
        custom = {"bedrockAgentCoreToolName": "hr-tools___check_leave_balance"}


def test_generic_handler_serves_compiled_fixtures(tmp_path: Path, hr_release):
    module = load_handler(tmp_path, hr_release.release_dir / "lambda" / "fixtures.json")

    resp = module.lambda_handler({"employee_id": "employee-001"}, _Ctx())
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert body["employee_id"] == "employee-001" and body["balances"]["annual"]["remaining"] == 10

    resp = module.lambda_handler({"employee_id": "employee-001", "leave_type": "sick", "name": "check_leave_balance"}, None)
    assert json.loads(resp["body"]) == {"employee_id": "employee-001", "leave_type": "sick", "entitled": 10, "used": 2, "remaining": 8}

    resp = module.lambda_handler({"employee_id": "employee-001", "leave_type": "maternity", "name": "check_leave_balance"}, None)
    assert json.loads(resp["body"]) == {"error": "Unknown leave type: maternity"}

    resp = module.lambda_handler(
        {"name": "submit_leave_request", "employee_id": "employee-001", "leave_type": "annual", "start_date": "2026-10-12", "end_date": "2026-10-14"},
        None,
    )
    body = json.loads(resp["body"])
    assert body["status"] == "submitted" and body["start_date"] == "2026-10-12"
    assert re.fullmatch(r"LV-\d{4}-[0-9A-F]{4}", body["confirmation_id"])

    resp = module.lambda_handler({"name": "no_such_tool"}, None)
    assert resp["statusCode"] == 400 and "Unknown tool" in resp["body"]
    sys.modules.pop(module.__name__, None)


def test_every_deploy_first_removes_the_cli_evaluator_zips(hr_release, tmp_path):
    """Live 2026-09-29: the AgentCore CLI left 59 <evaluator>-<uuid>.zip files (34 MB each) in the Workshop
    instance's 1.9 GB tmpfs /tmp, and 12's restore deploy failed with ENOSPC during CDK synth."""
    import subprocess
    import time

    rel = hr_release.release_dir
    for script, count in render._DEPLOY_SCRIPTS.items():
        lines = (rel / script).read_text(encoding="utf-8").splitlines()
        deploys = [i for i, line in enumerate(lines) if re.match(r"\s*npx agentcore deploy ", line)]
        assert len(deploys) == count, script
        assert all("render: prune-cli-zips" in lines[i - 1] for i in deploys), script
    tmp = tmp_path / "tmpfs"
    tmp.mkdir()
    old = time.time() - 3600
    for name in ("thelma_rag_quality-02ff91fa-49bc-4946-b47f-3515c86c2aab.zip", "mtg_goal_success-2347d2fe-5f79-4839-a040-965bc138c03d.zip",
                 "fresh-11111111-2222-3333-4444-555555555555.zip", "hr-tools-lambda.zip", "notes-02ff91fa.zip"):
        (tmp / name).write_bytes(b"x")
        if not name.startswith("fresh"):
            os.utime(tmp / name, (old, old))
    subprocess.run(["bash", "-c", render._PRUNE_CLI_ZIPS], env={**os.environ, "TMPDIR": str(tmp)}, check=True)
    assert sorted(p.name for p in tmp.iterdir()) == [
        "fresh-11111111-2222-3333-4444-555555555555.zip", "hr-tools-lambda.zip", "notes-02ff91fa.zip"]


FAKE_HARNESS_AWS = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_HARNESS_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
state.setdefault("calls", []).append(args)

def opt(name):
    return args[args.index(name) + 1] if name in args else None

def save():
    json.dump(state, open(state_path, "w"))

if args[:2] == ["bedrock-agentcore-control", "list-harnesses"]:
    print(state["harnessId"] if state.get("exists", True) else "None")
elif args[:2] == ["bedrock-agentcore-control", "get-harness"]:
    query = opt("--query")
    if query == "harness.status":
        print("READY")
    elif query == "harness.model":
        print(json.dumps(state["model"]))
    else:
        print(state["model"]["bedrockModelConfig"]["modelId"])
elif args[:2] == ["bedrock-agentcore-control", "update-harness"]:
    if state.get("failUpdates", 0) > 0:
        state["failUpdates"] -= 1
        save()
        sys.exit(254)
    if opt("--cli-input-json"):
        update = json.load(open(opt("--cli-input-json")[len("file://"):]))
        state["prompt"] = update["systemPrompt"][0]["text"]
        state["updateKeys"] = sorted(update)
    else:
        state["model"] = json.loads(opt("--model"))
    print("UPDATING")
save()
'''


def _run_compare_switch(release_dir, tmp_path, *, exists=True, fail_updates=0, then="true"):
    """12's in-place switch (helpers + Step 1) under a fake aws, followed by ``then``; returns (rc, state)."""
    import subprocess

    script = (release_dir / "12-compare-models.sh").read_text(encoding="utf-8")
    start = script.index("# Workshop Customizer: the comparison switches")
    block = script[start:script.index("# Step 2: ")]
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    (bindir / "aws").write_text(FAKE_HARNESS_AWS, encoding="utf-8")
    (bindir / "aws").chmod(0o755)
    (bindir / "sleep").write_text("#!/bin/sh\n", encoding="utf-8")
    (bindir / "sleep").chmod(0o755)
    state = tmp_path / "state.json"
    original = {"bedrockModelConfig": {"modelId": "us.amazon.nova-2-lite-v1:0", "apiFormat": "converse_stream", "maxTokens": 8192}}
    state.write_text(json.dumps({"harnessId": "hrassistant_hrassistant-abc", "exists": exists, "model": original,
                                 "failUpdates": fail_updates}), encoding="utf-8")
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_HARNESS_STATE": str(state)}
    prelude = 'set -e\nset -o pipefail\nREGION=us-west-2\nBASELINE_MODEL=us.amazon.nova-2-lite-v1:0\nCOMPARE_MODEL=us.amazon.nova-pro-v1:0\n'
    done = subprocess.run(["bash", "-c", prelude + block + then], env={**env, "LC_ALL": "en_US.UTF-8"}, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    return done.returncode, json.loads(state.read_text()), done.stdout + done.stderr, original


def test_the_model_comparison_switches_the_harness_in_place_and_back(hr_release, tmp_path):
    """12 no longer redeploys (two CDK deploys and two memory re-setups took 616-786 s live): update-harness swaps
    the model only (measured READY in 23-26 s) and the EXIT trap swaps it back, keeping the rest of the config."""
    text = (hr_release.release_dir / "12-compare-models.sh").read_text(encoding="utf-8")
    assert "agentcore deploy --yes" not in text and "05-setup-memory.sh" not in text
    assert 'HARNESS_NAME="hrassistant_hrassistant"' in text
    rc, state, out, original = _run_compare_switch(hr_release.release_dir, tmp_path / "ok",
                                                  then='[ "$(aws bedrock-agentcore-control get-harness --query x)" = '
                                                       '"$COMPARE_MODEL" ] || exit 9\n')
    assert rc == 0, out
    updates = [json.loads(c[c.index("--model") + 1]) for c in state["calls"] if c[1] == "update-harness"]
    assert [u["bedrockModelConfig"]["modelId"] for u in updates] == ["us.amazon.nova-pro-v1:0", "us.amazon.nova-2-lite-v1:0"]
    assert updates[0]["bedrockModelConfig"]["maxTokens"] == 8192 and updates[1] == original  # only the model id changes
    assert state["model"] == original and "已还原为基线模型" in out


def test_a_failed_comparison_switch_still_restores_and_a_missing_harness_changes_nothing(hr_release, tmp_path):
    rc, state, out, original = _run_compare_switch(hr_release.release_dir, tmp_path / "missing", exists=False)
    assert rc == 1 and "找不到 Harness" in out and not [c for c in state["calls"] if c[1] == "update-harness"]
    # The switch fails: 12 stops, and the trap still puts the original model back.
    rc, state, out, original = _run_compare_switch(hr_release.release_dir, tmp_path / "fail", fail_updates=1)
    assert rc != 0 and state["model"] == original
    assert [c[1] for c in state["calls"]].count("update-harness") == 2


def test_the_judges_wait_until_the_newest_probe_trace_is_complete(hr_release, tmp_path):
    """Live 2026-10-01: 09 scored a baseline answer GR 1.0 seconds after it was given and 0.31 when re-scored later;
    13 scored one trace [0.0, 0.611, 0.611]. 09 waits until its newest probe is old enough, before any judge."""
    import subprocess
    import time

    script = (hr_release.release_dir / "09-run-eval.sh").read_text(encoding="utf-8")
    helper = script[script.index("settle_probe_traces() {"):script.index("recent_retrieve_traces() {")]
    assert script.index("  settle_probe_traces\n") < script.index('echo "═══ THELMA (RAG quality)')
    sessions = tmp_path / "sessions.tsv"
    now_ms = int(time.time() * 1000)
    rows = [("1", "c1", "s1", "a1", str(now_ms - 600_000), "0"), ("2", "p1", "s2", "a2", str(now_ms - 1_000), "1"),
            ("3", "p2", "s3", "a3", str(now_ms - 300_000), "1")]
    sessions.write_text("".join("\t".join(r) + "\n" for r in rows), encoding="utf-8")

    def settle(seconds: int, file: str) -> tuple[float, str]:
        started = time.monotonic()
        done = subprocess.run(["bash", "-c", helper + "settle_probe_traces"], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", "GOLDEN_SESSIONS_FILE": file, "WORKSHOP_EVAL_SETTLE_SECONDS": str(seconds)})
        assert done.returncode == 0, done.stderr
        return time.monotonic() - started, done.stdout

    waited, out = settle(3, str(sessions))  # the newest probe (not the newer non-probe row) was asked 1 s ago
    assert 1.0 <= waited < 3.5 and "等待" in out
    waited, out = settle(3, str(tmp_path / "missing.tsv"))  # no sessions file (a trace given by id): no wait
    assert waited < 1.0 and out == ""

    s13 = (hr_release.release_dir / "13-judge-stability.sh").read_text(encoding="utf-8")
    assert "print(f'{best[0]}|{best[1]}|{bts}')" in s13 and 'SID="${REST%%|*}"; TS_NS="${REST#*|}"' in s13
    # The row parse, then (past the "fi" that closes 13's own if/else) the target line and the settle block.
    first, closing, rest = s13[s13.index('  TID="${ROW%%|*}"; REST='):s13.index("# 跑一次 THELMA")].split("\n", 2)
    assert closing == "fi"
    probe = f'ROW="t1|s1|{time.time_ns()}"\n{first}\n{rest}\necho "$TID $SID"'
    started = time.monotonic()
    done = subprocess.run(["bash", "-c", probe], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "WORKSHOP_EVAL_SETTLE_SECONDS": "2", "RUNS": "3"})
    assert done.returncode == 0, done.stderr
    assert 1.0 <= time.monotonic() - started < 3.0 and done.stdout.strip().endswith("t1 s1")


def test_step_10_updates_the_deployed_harness_prompt_in_place(hr_release, tmp_path):
    """10's Step 2: the prompt Step 1 wrote goes to the deployed Harness with update-harness (about 30 s), with no
    CDK deploy and no memory re-setup; only the prompt is sent, so model, tools, skills and memory stay."""
    import subprocess

    script = (hr_release.release_dir / "10-optimize-prompt.sh").read_text(encoding="utf-8")
    assert "npx agentcore deploy" not in script and '"$SCRIPT_DIR/05-setup-memory.sh"' not in script
    block = script[script.index("# Step 2: 在已部署的 Harness 上就地更新 System Prompt"):script.index("# Step 3: ")]
    work = tmp_path / "work"
    (work / "app" / "hrassistant").mkdir(parents=True)
    (work / "app" / "hrassistant" / "system-prompt.md").write_text("只依据文档回答。\n第二行", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "aws").write_text(FAKE_HARNESS_AWS, encoding="utf-8")
    (bindir / "aws").chmod(0o755)
    (bindir / "sleep").write_text("#!/bin/sh\n", encoding="utf-8")
    (bindir / "sleep").chmod(0o755)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"harnessId": "h-1", "model": {"bedrockModelConfig": {"modelId": "m"}}}), encoding="utf-8")
    import os
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_HARNESS_STATE": str(state), "LC_ALL": "en_US.UTF-8"}
    done = subprocess.run(["bash", "-c", "set -e\nREGION=us-west-2\n" + block], cwd=work, capture_output=True, encoding="utf-8",
                          errors="replace", env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    after = json.loads(state.read_text(encoding="utf-8"))
    assert after["prompt"] == "只依据文档回答。\n第二行" and after["updateKeys"] == ["harnessId", "systemPrompt"]
    assert after["model"] == {"bedrockModelConfig": {"modelId": "m"}} and "已换上优化后的 System Prompt" in done.stdout
    state.write_text(json.dumps({"harnessId": "h-1", "exists": False, "model": {}}), encoding="utf-8")
    missing = subprocess.run(["bash", "-c", "set -e\nREGION=us-west-2\n" + block], cwd=work, capture_output=True, encoding="utf-8",
                             errors="replace", env=env)
    assert missing.returncode == 1 and "找不到 Harness" in missing.stdout
