"""script_facts: the single seam between the scenario and the rendered 06/09/10/11 scripts.

The facts derive the labs.teaching runtime render emits (eval order, per-case personas, probe flags,
|P|, the 06 first conversation); verify_rendered proves the release scripts do what the facts say
(offline, deterministic).
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import pytest
import yaml

from workshop_customizer import compiler, render, script_facts, teaching
from workshop_customizer.scenario import load_scenario
from workshop_customizer.validator import PackValidationError, ValidationReport

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((REPO_ROOT / "template-lock.json").read_text(encoding="utf-8"))
TEMPLATE_COMMIT = LOCK["template"]["commit"]
UPSTREAM = REPO_ROOT / LOCK["template"].get("localPath", "upstream/")
REFERENCE_PACKS = ("hr-default", "it-helpdesk", "maintenance")


def _pack_data(pack_id: str) -> dict:
    return yaml.safe_load((REPO_ROOT / "scenarios" / pack_id / "scenario.yaml").read_text(encoding="utf-8"))


def _undeclared(data: dict) -> dict:
    """A copy without labs.teaching (the reference packs declare one since P1)."""
    data = json.loads(json.dumps(data))
    data["labs"].pop("teaching", None)
    return data


def _teaching_block() -> dict:
    return {
        "firstConversation": {
            "query": "What should I check first on my line today?", "actorId": "first-001",
            "unknownContext": ["assigned line", "shift"], "label": "your shift", "mustNotMention": ["FL-100-2"],
            "teachingPoint": "The agent does not know your line yet.",
        },
        "baselineDefects": [{"id": "no-grounding", "description": "No grounding rule.", "candidateFix": "Only answer from retrieved documents"}],
        "phenomena": [
            {"id": "pf-lube", "kind": "prompt_fixable", "caseIds": ["filler-lubrication-interval", "overdue-lubrication-check"],
             "defectIds": ["no-grounding"], "teachingPoint": "Grounding rules raise GR."},
            {"id": "gap-p1", "kind": "retrieval_gap", "caseIds": ["p1-response-target"], "mechanism": "absent",
             "absentTerms": ["escalation matrix"], "teachingPoint": "The KB lacks it."},
            {"id": "tool", "kind": "tool_use", "caseIds": ["own-work-order-status"], "teachingPoint": "Tools answer own data."},
            {"id": "refuse", "kind": "refusal", "caseIds": ["interlock-bypass-request"], "teachingPoint": "Refuse bypasses."},
        ],
    }


def _with_teaching(data: dict) -> dict:
    data = json.loads(json.dumps(data))
    data.setdefault("labs", {})["teaching"] = _teaching_block()
    return data


@pytest.fixture(scope="module")
def maintenance_pack(tmp_path_factory):
    scenario = load_scenario(REPO_ROOT / "scenarios" / "maintenance" / "scenario.yaml")
    return compiler.compile_pack(scenario, tmp_path_factory.mktemp("maint") / "out", template_commit=TEMPLATE_COMMIT)


@pytest.fixture(scope="module")
def maintenance_release(maintenance_pack, tmp_path_factory):
    return render.render_release(maintenance_pack, UPSTREAM, tmp_path_factory.mktemp("maint-rel") / "release", template_commit=TEMPLATE_COMMIT)


# ---------------------------------------------------------------------------
# Teaching facts (what render emits)
# ---------------------------------------------------------------------------


def test_teaching_facts_uses_eval_order_personas_probe_flags_and_probe_count():
    data = _with_teaching(_pack_data("maintenance"))
    facts = script_facts.compute(data)
    assert facts.golden_ids == teaching.eval_order_ids(data) == (
        "cap-steriliser-uv-lamps", "own-work-order-status", "interlock-bypass-request",
        "filler-lubrication-interval", "p1-response-target", "overdue-lubrication-check",
    )
    by_id = {c["id"]: c for c in data["evaluation"]["goldenSet"]}
    assert facts.golden_actors == tuple(by_id[cid]["actorId"] for cid in facts.golden_ids)
    assert facts.golden_probe == (0, 0, 0, 1, 1, 1)
    assert facts.probe_case_ids == teaching.probe_case_ids(data) == ("filler-lubrication-interval", "p1-response-target", "overdue-lubrication-check")
    assert facts.probe_count == facts.recent_n == 3
    assert facts.stability_case_id == teaching.stability_case_id(data) == "overdue-lubrication-check"
    assert facts.features == script_facts.FEATURES
    # The interlock case requires retrieval but is not a declared probe: asked early, not counted.
    interlock = next(c for c in facts.eval_cases if c.case_id == "interlock-bypass-request")
    assert interlock.requires_retrieval and not interlock.probe
    fc = facts.first_conversation
    assert (fc.source, fc.query, fc.actor_id, fc.label) == ("teaching", "What should I check first on my line today?", "first-001", "your shift")
    assert fc.unknown_context == ("assigned line", "shift") and fc.must_not_mention == ("FL-100-2",)
    assert fc.case_id is None and fc.teaching_point == "The agent does not know your line yet."


def test_teaching_facts_without_a_block_has_no_probes():
    facts = script_facts.compute(_undeclared(_pack_data("hr-default")))
    assert facts.probe_count == facts.recent_n == 0 and facts.stability_case_id is None
    assert facts.first_conversation.source == "practice"  # falls back so the facts stay total


# ---------------------------------------------------------------------------
# Names and invariants
# ---------------------------------------------------------------------------


def test_names_are_the_ones_the_rendered_scripts_use(maintenance_release):
    facts = script_facts.compute(maintenance_release.pack.scenario.data)
    rel = maintenance_release.release_dir
    assert facts.compact_target == facts.target_name.replace("-", "")
    assert f"--filter-pattern '\"{facts.retrieval_span}\"'" in (rel / "10-optimize-prompt.sh").read_text(encoding="utf-8")
    assert f"add memory --name {facts.memory_name} " in (rel / "04-deploy.sh").read_text(encoding="utf-8")
    assert facts.thelma_evaluator == f"{facts.agent_name}_thelma_rag_quality"
    assert facts.mtg_evaluator == f"{facts.agent_name}_mtg_goal_success"
    assert facts.eval_runs_root == "${WORKSHOP_ROOT:-$HOME/workshop}/eval-runs/" + facts.agent_name
    evaluation = maintenance_release.pack.scenario.data["evaluation"]
    assert facts.judge_model == evaluation.get("judgeModel", script_facts.DEFAULT_JUDGE_MODEL)
    pack_env = (rel / "pack" / "pack.env").read_text(encoding="utf-8")
    assert f"WS_JUDGE_MODEL={facts.judge_model}\n" in pack_env and f"WS_AGENT_NAME={facts.agent_name}\n" in pack_env


def test_invalid_facts_are_rejected():
    facts = script_facts.compute(_pack_data("maintenance"))
    with pytest.raises(ValueError):
        dataclasses.replace(facts, features=frozenset({"holdout-rehearsal"}))
    with pytest.raises(ValueError):
        dataclasses.replace(facts, probe_count=facts.probe_count + 1)
    with pytest.raises(ValueError):
        dataclasses.replace(facts, eval_cases=tuple(reversed(facts.eval_cases)))


# ---------------------------------------------------------------------------
# Bash array parsing
# ---------------------------------------------------------------------------

TRICKY = ['plain', 'has "quotes"', "dollar $HOME and `tick`", "back\\slash \\$", "multi\nline", "中文 问题？"]


def test_bash_arrays_round_trip_render_quoting():
    multi = "X=1\nGOLDEN_QUERIES=(\n" + "".join(f"  {render.bash_quote(v)}\n" for v in TRICKY) + ")\n"
    one_line = "GOLDEN_LABELS=(" + " ".join(render.bash_quote(v) for v in TRICKY) + ")\n"
    assert script_facts.bash_arrays(multi, "GOLDEN_QUERIES") == [TRICKY]
    assert script_facts.bash_arrays(one_line, "GOLDEN_LABELS") == [TRICKY]
    assert script_facts.bash_arrays("GOLDEN_PROBE=(0 1 1)\n", "GOLDEN_PROBE") == [["0", "1", "1"]]
    assert script_facts.bash_arrays("  # GOLDEN_IDS=(a)\nGOLDEN_IDS=(a)\nGOLDEN_IDS=(b c)\n", "GOLDEN_IDS") == [["a"], ["b", "c"]]
    assert script_facts.bash_arrays('GOLDEN_IDS=("open\n', "GOLDEN_IDS") == [None]
    assert script_facts.bash_arrays("nothing here", "GOLDEN_IDS") == []


# ---------------------------------------------------------------------------
# verify_rendered
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pack_id", REFERENCE_PACKS)
def test_reference_releases_agree_with_their_facts(pack_id, tmp_path: Path):
    scenario = load_scenario(REPO_ROOT / "scenarios" / pack_id / "scenario.yaml")
    pack = compiler.compile_pack(scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    release = render.render_release(pack, UPSTREAM, tmp_path / "release", template_commit=TEMPLATE_COMMIT)
    assert script_facts.verify_rendered(release.release_dir, script_facts.compute(scenario.data)) == []


TAMPER = [
    ("09-run-eval.sh", 'local want="3"', 'local want="2"', "09-run-eval.sh: want is 2, script facts say 3"),
    ("09-run-eval.sh", "    N=3\n", "    N=4\n", "09-run-eval.sh: N is 4"),
    ("09-run-eval.sh", '"Is lubrication overdue on FL-100-2?"', '"Is lubrication late on FL-100-2?"', "09-run-eval.sh: GOLDEN_QUERIES[0] is"),
    ("09-run-eval.sh", "RECENT_N=3 ", "RECENT_N=4 ", "09-run-eval.sh: RECENT_N is 4"),
    ("09-run-eval.sh", "GOLDEN_PROBE=(0 0 0 1 1 1)", "GOLDEN_PROBE=(0 0 1 1 1 1)", "09-run-eval.sh: GOLDEN_PROBE[2] is '1', script facts say '0'"),
    ("09-run-eval.sh", 'GOLDEN_ACTORS=("technician-001" "technician-001" "operator-001"', 'GOLDEN_ACTORS=("technician-001" "technician-001" "technician-001"',
     "09-run-eval.sh: GOLDEN_ACTORS[2] is 'technician-001', script facts say 'operator-001'"),
    ("09-run-eval.sh", 'GOLDEN_IDS=("overdue-lubrication-check"', 'GOLDEN_IDS=("overdue-lube-check"', "09-run-eval.sh: GOLDEN_IDS[0] is 'overdue-lube-check'"),
    ("09-run-eval.sh", 'local ACTOR="${GOLDEN_ACTORS[$i]}-${RUN_TAG}-q$((i+1))"', 'local ACTOR="${GOLDEN_ACTORS[$i]}"',
     "09-run-eval.sh: runtime actor ${GOLDEN_ACTORS[$i]}-${RUN_TAG}-q$((i+1)) appears 0 times"),
    ("09-run-eval.sh", 'eval-runs/maintassistant"', 'eval-runs/other"', "09-run-eval.sh: EVAL_ROOT is"),
    ("10-optimize-prompt.sh", "--eval-only 3\n", "--eval-only 2\n", "10-optimize-prompt.sh: --eval-only is 2"),
    ("10-optimize-prompt.sh", '-ge 3 ]', '-ge 2 ]', "10-optimize-prompt.sh: retrieval span wait is 2"),
    ("10-optimize-prompt.sh", 'ACTOR="${GOLDEN_ACTORS[$i]}-${RUN_TAG}-q$((i+1))"', 'ACTOR="${GOLDEN_ACTORS[$i]}-v2"', "10-optimize-prompt.sh: runtime actor"),
    ("10-optimize-prompt.sh", '"Own plant work order status" ', "", "10-optimize-prompt.sh: GOLDEN_LABELS has 5 entries"),
    ("11-cost-latency.sh", "RECENT_N=3 ", "RECENT_N=5 ", "11-cost-latency.sh: RECENT_N is 5"),
    ("13-judge-stability.sh", 'RUNS="${2:-3}"', 'RUNS="${2:-5}"', "13-judge-stability.sh: RUNS default is 5"),
    ("09-run-eval.sh", 'WORKSHOP_EVAL_ATTEMPTS:-10}', 'WORKSHOP_EVAL_ATTEMPTS:-5}', "09-run-eval.sh: evaluation retry budget"),
    ("06-test-conversation.sh", '--actor-id "technician-001"', '--actor-id "someone"', "06-test-conversation.sh: --actor-id is 'someone'"),
    ("06-test-conversation.sh", '"Which preventive maintenance tasks', '"Which corrective maintenance tasks', "06-test-conversation.sh: first-conversation query is"),
    ("06-test-conversation.sh", "Asking about PM tasks due this week...", "Asking about annual leave policy...", "06-test-conversation.sh: first-conversation topic line"),
    ("06-test-conversation.sh", "doesn't know your plant, line, or equipment you are responsible for yet.", "doesn't know you yet.",
     "06-test-conversation.sh: memory notice line"),
]


@pytest.mark.parametrize("rel,old,new,expected", TAMPER, ids=[t[3][:40] for t in TAMPER])
def test_verify_rendered_reports_each_disagreement(rel, old, new, expected, maintenance_release, tmp_path: Path):
    copy = tmp_path / "release"
    shutil.copytree(maintenance_release.release_dir, copy)
    path = copy / rel
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new), encoding="utf-8")
    problems = script_facts.verify_rendered(copy, script_facts.compute(maintenance_release.pack.scenario.data))
    assert len(problems) == 1 and problems[0].startswith(expected), problems


def test_verify_rendered_reports_duplicates_missing_files_and_practice_mismatch(maintenance_release, tmp_path: Path):
    copy = tmp_path / "release"
    shutil.copytree(maintenance_release.release_dir, copy)
    facts = script_facts.compute(maintenance_release.pack.scenario.data)
    s09 = copy / "09-run-eval.sh"
    s09.write_text(s09.read_text(encoding="utf-8") + '\nACTOR_ID="technician-001"\nGOLDEN_IDS=("x")\n', encoding="utf-8")
    (copy / "11-cost-latency.sh").unlink()
    practice = copy / "pack" / "golden" / "practice.json"
    practice.write_text(json.dumps(json.loads(practice.read_text(encoding="utf-8"))[:-1]), encoding="utf-8")
    problems = script_facts.verify_rendered(copy, facts)
    assert "09-run-eval.sh: a shared ACTOR_ID line remains" in problems
    assert "09-run-eval.sh: GOLDEN_IDS defined 2 times, expected once" in problems
    assert "11-cost-latency.sh: missing from the release" in problems
    assert "pack/golden/practice.json: practice case ids differ from the eval cases" in problems


def test_verify_rendered_checks_per_case_arrays_for_teaching_facts(tmp_path: Path):
    data = _with_teaching(_pack_data("maintenance"))
    facts = script_facts.compute(data)
    arrays = "".join(
        f"{name}=(" + " ".join(render.bash_quote(str(v)) for v in values) + ")\n"
        for name, values in (
            ("GOLDEN_QUERIES", facts.golden_queries), ("GOLDEN_LABELS", facts.golden_labels),
            ("GOLDEN_IDS", facts.golden_ids), ("GOLDEN_ACTORS", facts.golden_actors), ("GOLDEN_PROBE", facts.golden_probe),
        )
    )
    runtime = f'EVAL_ROOT="{facts.eval_runs_root}"\n  ACTOR="{script_facts.RUNTIME_ACTOR_EXPR}"\n'
    budget = (f'  local attempts="${{WORKSHOP_EVAL_ATTEMPTS:-{script_facts.EVAL_ATTEMPTS}}}" '
              f'pause="${{WORKSHOP_EVAL_RETRY_SECONDS:-{script_facts.EVAL_RETRY_SECONDS}}}"\n')
    s09 = f'RECENT_N={facts.recent_n}\n{arrays}{runtime}  local want="{facts.probe_count}"\n    N={facts.probe_count}\n{budget}'
    s10 = arrays.replace("GOLDEN_PROBE=(\"0\" \"0\"", "GOLDEN_PROBE=(\"1\" \"0\"") + runtime + f'"$SCRIPT_DIR/09-run-eval.sh" --eval-only {facts.probe_count}\n[ "${{CNT:-0}}" -ge {facts.probe_count} ]\n'
    fc = facts.first_conversation
    (tmp_path / "06-test-conversation.sh").write_text(
        f'echo {render.bash_quote(script_facts.first_conversation_topic(fc))}\n'
        f'  --actor-id "{fc.actor_id}" \\\n  --stream \\\n  {render.bash_quote(fc.query)} 2>&1)\n'
        f'echo {render.bash_quote(script_facts.memory_notice(fc))}\n', encoding="utf-8")
    (tmp_path / "09-run-eval.sh").write_text(s09, encoding="utf-8")
    (tmp_path / "10-optimize-prompt.sh").write_text(s10, encoding="utf-8")
    (tmp_path / "11-cost-latency.sh").write_text(f"RECENT_N={facts.recent_n}\n", encoding="utf-8")
    (tmp_path / "13-judge-stability.sh").write_text('RUNS="${2:-3}"\n', encoding="utf-8")
    (tmp_path / "pack" / "golden").mkdir(parents=True)
    (tmp_path / "pack" / "golden" / "practice.json").write_text(json.dumps([{"id": i} for i in facts.golden_ids]), encoding="utf-8")
    assert script_facts.verify_rendered(tmp_path, facts) == ["10-optimize-prompt.sh: GOLDEN_PROBE[0] is '1', script facts say '0'"]


# ---------------------------------------------------------------------------
# render consumes the facts
# ---------------------------------------------------------------------------


def test_render_writes_the_practice_patches_from_script_facts(maintenance_pack, tmp_path: Path, monkeypatch):
    real = script_facts.compute

    def fake(data):
        facts = real(data)
        cases = tuple(dataclasses.replace(c, query=f"Facts query {c.index}?", label=f"Facts label {c.index}", actor_id=f"persona-{c.index}")
                      for c in facts.eval_cases)
        first = dataclasses.replace(facts.first_conversation, query="Facts first question?", actor_id="first-actor", label="the facts topic")
        return dataclasses.replace(facts, eval_cases=cases, first_conversation=first)

    monkeypatch.setattr(script_facts, "compute", fake)
    release = render.render_release(maintenance_pack, UPSTREAM, tmp_path / "release", template_commit=TEMPLATE_COMMIT)
    s06 = (release.release_dir / "06-test-conversation.sh").read_text(encoding="utf-8")
    s09 = (release.release_dir / "09-run-eval.sh").read_text(encoding="utf-8")
    s10 = (release.release_dir / "10-optimize-prompt.sh").read_text(encoding="utf-8")
    assert '"Facts first question?" 2>&1)' in s06 and '--actor-id "first-actor"' in s06
    assert 'echo "🗣️  Asking about the facts topic..."' in s06
    assert '  "Facts query 0?"\n' in s09 and '"Facts label 4"' in s10
    actors = 'GOLDEN_ACTORS=(' + " ".join(f'"persona-{i}"' for i in range(6)) + ')'
    assert actors in s09 and actors in s10


def test_render_refuses_facts_without_probes(tmp_path: Path):
    facts = script_facts.compute(_pack_data("maintenance"))
    none = dataclasses.replace(facts, eval_cases=tuple(dataclasses.replace(c, probe=False) for c in facts.eval_cases), probe_count=0)
    with pytest.raises(render.RenderError, match="no retrieval probes"):
        render._patch_scripts(tmp_path, _pack_data("maintenance"), none)


def test_render_fails_when_the_token_pass_rewrites_a_practice_query(tmp_path: Path, monkeypatch):
    """A practice query naming an upstream identifier would be silently rewritten in 09/10."""
    root = tmp_path / "it-helpdesk"
    shutil.copytree(REPO_ROOT / "scenarios" / "it-helpdesk", root)
    data = yaml.safe_load((root / "scenario.yaml").read_text(encoding="utf-8"))
    practice = [c for c in data["evaluation"]["goldenSet"] if c["set"] == "practice"]
    practice[1]["query"] = "Why does the hrgateway reject my laptop?"
    (root / "scenario.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    scenario = load_scenario(root / "scenario.yaml")
    # The student guide shows the practice questions, so the guide residue gate refuses the pack first...
    with pytest.raises(PackValidationError, match=r"guide.residue: template identifier 'hrgateway'"):
        compiler.compile_pack(scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    # ...and without that gate the render-time script-facts check still catches the rewrite.
    from workshop_customizer import guide

    monkeypatch.setattr(guide, "check_guides", lambda *a, **k: ValidationReport())
    pack = compiler.compile_pack(scenario, tmp_path / "out", template_commit=TEMPLATE_COMMIT)
    index = script_facts.compute(scenario.data).golden_ids.index(practice[1]["id"])  # eval order, not practice order
    with pytest.raises(render.RenderError, match=rf"GOLDEN_QUERIES\[{index}\]"):
        render.render_release(pack, UPSTREAM, tmp_path / "release", template_commit=TEMPLATE_COMMIT)


def test_the_guides_print_the_retry_budget_09_uses(maintenance_release):
    """D3: the guide's retry-message table and 09 agree on the evaluation retry budget."""
    s09 = (maintenance_release.release_dir / "09-run-eval.sh").read_text(encoding="utf-8")
    assert 'retry in ${pause}s ${attempt}/${attempts}' in s09
    readme = (maintenance_release.release_dir / "README.md").read_text(encoding="utf-8")
    budget = f"retry in {script_facts.EVAL_RETRY_SECONDS}s n/{script_facts.EVAL_ATTEMPTS}"
    assert f"(spans not indexed yet, {budget})" in readme and f"(span evidence incomplete, {budget})" in readme
    assert "retry in 25s n/5" not in readme
