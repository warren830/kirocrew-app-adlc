"""The staged draft: one Kiro reply per part of the pack (routes STAGE_DEPS), offline.

A fake Kiro answers each stage from the maintenance reference pack, so the assembled draft must equal the pack a
one-reply draft of the same pack gives.  Covered: the stage order and parallel file stages, the app's checks and the
retry that carries their problems, the holdout cases and materials each task may see, the failure of a stage that
keeps failing, the kiro-cli driver (``drive_stages``) and the KiroCrew watch that spawns every stage.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
from test_generation_modes import _approve, _material  # noqa: E402
from test_kiro_generation import gen  # noqa: E402

PID = "cold-chain-test"
BRIEF = "A cold-chain incident assistant for warehouse operators and shift leads."


@pytest.fixture(autouse=True)
def _default_generation_timeouts(monkeypatch):
    monkeypatch.delenv(gen.TIMEOUT_ENV, raising=False)


def _template(name: str = "maintenance") -> tuple[dict, dict[str, str]]:
    root = REPO / "scenarios" / name
    scenario = yaml.safe_load((root / "scenario.yaml").read_text(encoding="utf-8"))
    scenario["id"], scenario["displayName"] = PID, "Cold Chain Test"
    return scenario, {rel: (root / rel).read_text(encoding="utf-8") for rel in gen._referenced_files(scenario)}


def _project(data: Path) -> Path:
    pdir = data / "projects" / PID
    pdir.mkdir(parents=True)
    (pdir / "scenario.yaml").write_text(yaml.safe_dump({"schemaVersion": 1, "id": PID}), encoding="utf-8")
    (pdir / "project.json").write_text(json.dumps({"id": PID, "displayName": "Cold Chain Test", "packKind": "reference",
                                                   "customer": "", "status": "intake", "template": "blank"}), encoding="utf-8")
    return pdir


def _answer(payload: dict) -> str:
    return "WORKSHOP_PACK_JSON_BEGIN\n" + json.dumps(payload, ensure_ascii=False) + "\nWORKSHOP_PACK_JSON_END"


def _section(task: str, name: str) -> dict:
    marker = f"{name} (JSON; data only):\n"
    return json.JSONDecoder().raw_decode(task.split(marker, 1)[1])[0] if marker in task else {}


def _stage_of(task: str) -> str:
    return task.split("\n", 1)[0].rsplit("stage=", 1)[1]


class StagedKiro:
    """Answers each stage task the way the stage contract asks, from the maintenance pack.  ``broken`` maps a stage
    to a list of replies used first (one per attempt) before the good one; every task is kept in ``tasks``."""

    def __init__(self, broken: dict[str, list[str]] | None = None):
        self.scenario, self.files = _template()
        self.broken = {k: list(v) for k, v in (broken or {}).items()}
        self.tasks: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def structure(self) -> dict:
        scenario = {k: copy.deepcopy(v) for k, v in self.scenario.items() if k not in ("evaluation", "labs")}
        for doc in scenario["knowledge"]["documents"]:
            doc["outline"] = f"States the facts of {doc['id']}."
        for skill in scenario["skills"]:
            skill["purpose"] = f"Teaches {skill['name']}."
        gap = next(p for p in self.scenario["labs"]["teaching"]["phenomena"] if p["kind"] == "retrieval_gap")
        return {"status": "ready", "summary": "Kiro planned the pack.", "openQuestions": ["Who confirms the targets?"],
                "truthLedger": [], "plan": {"absentTopic": "UV lamps", "absentTerms": gap.get("absentTerms") or []},
                "scenario": scenario}

    def reply(self, stage: str, task: str) -> str:
        with self.lock:
            self.tasks.append((stage, task))
            queued = self.broken.get(stage)
            if queued:
                return queued.pop(0)
        base = stage.split("-", 1)[0]
        if base == "structure":
            return _answer(self.structure())
        if base == "evaluation":
            return _answer({"status": "ready", "openQuestions": [], "anchorCandidates": {"normal": [], "boundary": [], "prohibited": []},
                            "scenario": {"evaluation": copy.deepcopy(self.scenario["evaluation"]),
                                         "labs": copy.deepcopy(self.scenario["labs"])}})
        if base == "prompts":
            prompts = self.scenario["prompts"]
            return _answer({"files": {f: self.files[f] for f in (prompts["baselineFile"], prompts["optimizationCandidateFile"])}})
        if base == "skills":
            return _answer({"files": {s["file"]: self.files[s["file"]] for s in self.scenario["skills"]}})
        return _answer({"files": {f: self.files[f] for f in _section(task, "STAGE_INPUT")["write"]}})


def _start(data: Path, body: dict | None = None) -> dict:
    record, _task = gen.prepare_generation(data, {"projectId": PID, "brief": BRIEF, **(body or {})}, runner="kiro-cli")
    return gen.mark_running(data, record)


def _one_reply_result(tmp_path: Path) -> dict:
    data = tmp_path / "one-reply"
    _project(data)
    scenario, files = _template()
    record, _ = gen.prepare_generation(data, {"projectId": PID, "brief": BRIEF, "staged": False}, runner="kiro-cli")
    gen.mark_running(data, record)
    done = gen.complete_generation(record, _answer({"status": "ready", "summary": "Kiro planned the pack.",
                                                    "openQuestions": ["Who confirms the targets?"], "truthLedger": [],
                                                    "anchorCandidates": {"normal": [], "boundary": [], "prohibited": []},
                                                    "scenario": scenario, "files": files}), data_dir=data)
    assert done["status"] == "ready", done.get("error")
    return done["result"]


def test_prepare_writes_a_staged_record_whose_task_is_the_structure(tmp_path):
    data = tmp_path / "data"
    _project(data)
    record, task = gen.prepare_generation(data, {"projectId": PID, "brief": BRIEF})
    assert record["staged"] is True and record["timeoutSecs"] == gen.STAGE_TIMEOUTS["structure"]
    assert [s["name"] for s in record["stages"]] == ["structure", "evaluation", "skills", "prompts"]
    assert {s["name"]: s["deps"] for s in record["stages"]} == {"structure": [], "evaluation": ["structure"],
                                                                "skills": ["structure"], "prompts": ["evaluation"]}
    assert task.startswith("WORKSHOP_CUSTOMIZER_TASK mode=draft round=1 project=cold-chain-test stage=structure\n"
                           "Create the structure of a Scenario Pack draft")
    assert record["stages"][0]["task"] == record["task"]
    assert set(record["contracts"]) == {"stage-structure", "provenance"}  # the structure includes no teaching rules
    assert "TEACHING SKELETON" not in task and '"plan"' in task and "STAGE_INPUT" not in task
    stored = json.loads((gen._stage_dir(data, record["id"]) / "input.json").read_text(encoding="utf-8"))
    assert stored["common"]["brief"] == BRIEF and stored["materials"] == []
    assert gen.stage_task(data, record, "structure") == task  # every runner composes the same structure task
    public = gen._public_record(record)
    assert public["staged"] is True and [s["name"] for s in public["stages"]] == ["structure", "evaluation", "skills", "prompts"]
    with pytest.raises(gen.GenerationError, match="staged must be a boolean"):
        gen.prepare_generation(data, {"projectId": PID, "brief": BRIEF, "staged": "yes"})


def test_a_staged_draft_runs_every_stage_and_assembles_the_one_reply_pack(tmp_path):
    data = tmp_path / "data"
    _project(data)
    kiro = StagedKiro()
    record = gen.drive_stages(data, _start(data), kiro.reply)
    assert record["status"] == "ready", record.get("error")
    assert {s["name"]: (s["status"], s["attempts"]) for s in record["stages"]} == {
        "structure": ("done", 1), "evaluation": ("done", 1), "skills": ("done", 1), "prompts": ("done", 1),
        "knowledge-1": ("done", 1), "knowledge-2": ("done", 1)}
    order = [stage for stage, _ in kiro.tasks]
    assert order[0] == "structure" and order.index("evaluation") < min(order.index(s) for s in ("prompts", "knowledge-1", "knowledge-2"))
    batches = [sorted(_section(t, "STAGE_INPUT")["write"]) for s, t in kiro.tasks if s.startswith("knowledge")]
    assert sorted(sum(batches, [])) == sorted(d["file"] for d in kiro.scenario["knowledge"]["documents"])
    assert all(len(b) <= gen.KNOWLEDGE_BATCH for b in batches)

    one_reply = _one_reply_result(tmp_path)
    result = record["result"]
    assert result["scenario"] == one_reply["scenario"]  # outlines, purposes and the plan never reach the pack
    assert result["files"] == one_reply["files"]
    assert result["openQuestions"] == ["Who confirms the targets?"] and result["summary"] == "Kiro planned the pack."
    assert record["completedAt"]
    applied = gen.apply_generation(record["id"], data, {"acknowledged": True})
    assert applied["applied"] is True


def test_the_file_stages_never_see_a_holdout_case_and_only_two_stages_see_the_materials(tmp_path):
    data = tmp_path / "data"
    pdir = _project(data)
    _material(pdir, "mat-0123456789ab", "Excursions above 8 °C for 30 minutes are reported to QA.", name="sop.md")
    _approve(pdir)
    kiro = StagedKiro()
    record = gen.drive_stages(data, _start(data), kiro.reply)
    assert record["status"] == "ready", record.get("error")
    holdout = [c["query"] for c in kiro.scenario["evaluation"]["goldenSet"] if c["set"] == "holdout"]
    for stage, task in kiro.tasks:
        if stage in ("structure", "evaluation"):
            continue
        assert not [q for q in holdout if q in task], stage
    with_materials = sorted({stage.split("-", 1)[0] for stage, task in kiro.tasks if "materials" in _section(task, "INPUT_DATA")})
    assert with_materials == ["knowledge", "structure"]
    prompts_input = _section(next(t for s, t in kiro.tasks if s == "prompts"), "STAGE_INPUT")
    teaching = kiro.scenario["labs"]["teaching"]
    assert prompts_input["baselineDefects"] == teaching["baselineDefects"]
    assert prompts_input["toolTargetName"] == gen.derive_namespace(PID)["toolTargetName"]
    knowledge_input = _section(next(t for s, t in kiro.tasks if s == "knowledge-1"), "STAGE_INPUT")
    gap = next(p for p in teaching["phenomena"] if p["kind"] == "retrieval_gap")
    assert knowledge_input["absentTerms"] == gap["absentTerms"]
    fixable = [c for p in teaching["phenomena"] if p["kind"] == "prompt_fixable" for c in p["caseIds"]]
    terms = {t for c in kiro.scenario["evaluation"]["goldenSet"] if c["id"] in fixable for t in c["expected"].get("mustMention", [])}
    required = {t for s, t_ in kiro.tasks if s.startswith("knowledge")
                for req in _section(t_, "STAGE_INPUT")["requirements"].values() for t in req["mustContain"]}
    assert terms and terms == required  # every probe answer term is asked of exactly the documents that host it


def test_a_stage_that_fails_its_checks_is_asked_again_with_the_problems_and_its_reply(tmp_path):
    data = tmp_path / "data"
    _project(data)
    scenario, files = _template()
    fix = scenario["labs"]["teaching"]["baselineDefects"][0]["candidateFix"]
    weak = {scenario["prompts"]["baselineFile"]: files[scenario["prompts"]["baselineFile"]],
            scenario["prompts"]["optimizationCandidateFile"]: files[scenario["prompts"]["optimizationCandidateFile"]].replace(fix, "")}
    kiro = StagedKiro({"prompts": [_answer({"files": weak})]})
    record = gen.drive_stages(data, _start(data), kiro.reply)
    assert record["status"] == "ready", record.get("error")
    prompts = gen._stage(record, "prompts")
    assert prompts["attempts"] == 2 and prompts["problems"] == []
    second = [t for s, t in kiro.tasks if s == "prompts"][1]
    retry = _section(second, "RETRY_INPUT")
    assert any(p.startswith("P1: candidateFix") for p in retry["problems"]) and retry["previousOutput"] == {"files": weak}
    assert gen.RETRY_NOTE in second


def test_a_reply_that_still_fails_its_checks_is_kept_and_its_problems_become_warnings(tmp_path):
    data = tmp_path / "data"
    _project(data)
    scenario, files = _template()
    fix = scenario["labs"]["teaching"]["baselineDefects"][0]["candidateFix"]
    weak = _answer({"files": {scenario["prompts"]["baselineFile"]: files[scenario["prompts"]["baselineFile"]],
                              scenario["prompts"]["optimizationCandidateFile"]:
                                  files[scenario["prompts"]["optimizationCandidateFile"]].replace(fix, "")}})
    record = gen.drive_stages(data, _start(data), StagedKiro({"prompts": [weak, weak, weak]}).reply)
    assert record["status"] == "ready", record.get("error")
    # its own retry, then once more for the precheck error in the file it wrote; then the draft takes it as it is
    assert gen._stage(record, "prompts")["attempts"] == 3
    assert [h["kind"] for h in gen._stage(record, "prompts")["history"]] == ["output", "precheck"]
    assert any(w.startswith("draft stage prompts: P1: candidateFix") for w in record["result"]["warnings"])
    assert record["result"]["precheck"]["errorCount"] >= 1


def test_a_precheck_error_in_a_file_asks_the_stage_that_wrote_it_once_more(tmp_path):
    data = tmp_path / "data"
    _project(data)
    scenario, files = _template()
    fix = scenario["labs"]["teaching"]["baselineDefects"][0]["candidateFix"]
    weak = _answer({"files": {scenario["prompts"]["baselineFile"]: files[scenario["prompts"]["baselineFile"]],
                              scenario["prompts"]["optimizationCandidateFile"]:
                                  files[scenario["prompts"]["optimizationCandidateFile"]].replace(fix, "")}})
    kiro = StagedKiro({"prompts": [weak, weak]})
    record = gen.drive_stages(data, _start(data), kiro.reply)
    assert record["status"] == "ready", record.get("error")
    prompts = gen._stage(record, "prompts")
    assert prompts["attempts"] == 3 and [h["kind"] for h in prompts["history"]] == ["output", "precheck"]
    assert prompts["history"][1]["problems"][0].startswith("precheck ")
    last = [t for stage, t in kiro.tasks if stage == "prompts"][-1]
    assert "precheck " in last  # the third task carries the precheck error
    assert record["precheckRedo"] == ["prompts"]
    assert record["result"]["precheck"]["errorCount"] == 0
    assert sum(1 for stage, _t in kiro.tasks if stage == "evaluation") == 1  # nothing upstream runs again


def test_a_stage_that_keeps_failing_fails_the_draft_with_its_name(tmp_path):
    data = tmp_path / "data"
    _project(data)
    cut = "WORKSHOP_PACK_JSON_BEGIN\n{\"files\": {\"knowledge-base/docs/x.md\": \"# Half a docu"
    record = gen.drive_stages(data, _start(data), StagedKiro({"knowledge-1": [cut, cut]}).reply)
    assert record["status"] == "failed"
    assert record["error"].startswith("draft stage knowledge-1 failed: Kiro's reply was cut off")
    assert gen._stage(record, "knowledge-1")["status"] == "failed"
    assert gen._stage(record, "knowledge-2")["status"] in ("done", "running", "pending")


def test_a_failed_run_is_run_again_on_its_own_budget_and_keeps_the_quality_retry(tmp_path):
    """Live 2026-10-12 five of six first runs broke off ("dispatch failure"): a run that fails is run again, and that
    never spends the retry a reply that fails its checks gets."""
    data = tmp_path / "data"
    _project(data)
    scenario, files = _template()
    fix = scenario["labs"]["teaching"]["baselineDefects"][0]["candidateFix"]
    weak = _answer({"files": {scenario["prompts"]["baselineFile"]: files[scenario["prompts"]["baselineFile"]],
                              scenario["prompts"]["optimizationCandidateFile"]:
                                  files[scenario["prompts"]["optimizationCandidateFile"]].replace(fix, "")}})
    kiro, calls = StagedKiro({"prompts": [weak]}), []

    def run(stage: str, task: str) -> str:
        calls.append(stage)
        if stage == "prompts" and calls.count("prompts") <= gen.STAGE_RUN_RETRIES:
            raise RuntimeError("Kiro CLI exited 1: Encountered an error in the response stream: dispatch failure")
        return kiro.reply(stage, task)

    record = gen.drive_stages(data, _start(data), run)
    assert record["status"] == "ready", record.get("error")
    prompts = gen._stage(record, "prompts")
    assert (prompts["runFailures"], prompts["outputRetries"], prompts["attempts"]) == (gen.STAGE_RUN_RETRIES, 1, gen.STAGE_RUN_RETRIES + 2)
    assert prompts["problems"] == []  # the quality retry still happened, after every broken run
    assert "RETRY_INPUT" in [t for s, t in kiro.tasks if s == "prompts"][-1]


def test_a_run_that_keeps_failing_fails_the_draft(tmp_path):
    data = tmp_path / "data"
    _project(data)
    kiro = StagedKiro()

    def run(stage: str, task: str) -> str:
        if stage == "evaluation":
            raise TimeoutError("Kiro CLI timed out after 1200s")
        return kiro.reply(stage, task)

    record = gen.drive_stages(data, _start(data), run)
    assert record["status"] == "failed" and record["error"].startswith("draft stage evaluation failed: TimeoutError")
    assert gen._stage(record, "evaluation")["runFailures"] == gen.STAGE_RUN_RETRIES + 1


def test_a_whole_pack_or_a_question_answers_the_draft_at_once(tmp_path):
    data = tmp_path / "data"
    _project(data)
    scenario, files = _template()
    whole = {"status": "ready", "summary": "all at once", "openQuestions": [], "truthLedger": [], "scenario": scenario, "files": files}
    record = gen.drive_stages(data, _start(data), lambda stage, task: _answer(whole))
    assert record["status"] == "ready" and [s["status"] for s in record["stages"]] == ["skipped"] * 4
    data2 = tmp_path / "data2"
    _project(data2)
    asked = gen.drive_stages(data2, _start(data2), lambda stage, task: _answer({"status": "needs_input", "openQuestions": ["Which plant?"]}))
    assert asked["status"] == "needs-input" and asked["result"]["openQuestions"] == ["Which plant?"]


def test_a_cut_off_reply_is_named_as_such():
    with pytest.raises(gen.GenerationError, match="cut off after .* before WORKSHOP_PACK_JSON_END"):
        gen._extract_json('WORKSHOP_PACK_JSON_BEGIN\n{"files": {"a.md": "# unfinished')
    # complete JSON without the end marker still parses
    assert gen._extract_json('WORKSHOP_PACK_JSON_BEGIN\n{"files": {}}\n') == {"files": {}}


# ---------------------------------------------------------------------------
# KiroCrew: the watch spawns every stage
# ---------------------------------------------------------------------------


@pytest.fixture
def handlers(monkeypatch):
    web = types.SimpleNamespace(json_response=lambda payload, status=200: SimpleNamespace(payload=payload, status=status))
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.web = web
    registry = types.ModuleType("kiro_crew.apps.route_registry")
    registry.AppRoute = lambda method, path, handler: (method, path, handler)
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)
    monkeypatch.setitem(sys.modules, "kiro_crew", types.ModuleType("kiro_crew"))
    monkeypatch.setitem(sys.modules, "kiro_crew.apps", types.ModuleType("kiro_crew.apps"))
    monkeypatch.setitem(sys.modules, "kiro_crew.apps.route_registry", registry)
    return {(method, path): handler for method, path, handler in gen.register_routes(None)}


class Gateway:
    """KiroCrew's side: each spawn gets an id and a run task that a test ends with ``finish``."""

    def __init__(self, kiro: StagedKiro):
        self.kiro, self.infos, self.calls = kiro, {}, []
        self.manager = SimpleNamespace(get=lambda spawn_id: self.infos.get(spawn_id), _tasks={})

    async def run(self, *, task: str, agent: str, silent: bool):
        spawn_id = f"spawn-{len(self.calls) + 1}"
        self.calls.append((spawn_id, _stage_of(task), task))
        self.manager._tasks[spawn_id] = asyncio.get_running_loop().create_future()
        return spawn_id

    def finish(self, spawn_id: str) -> None:
        _sid, stage, task = next(c for c in self.calls if c[0] == spawn_id)
        self.infos[spawn_id] = SimpleNamespace(done=True, result=self.kiro.reply(stage, task), result_truncated=False,
                                               error="", app="workshop-customizer")
        self.manager._tasks[spawn_id].set_result(None)


def _request(body=None, *, gateway: Gateway, match=None):
    async def read_json():
        return body
    return SimpleNamespace(json=read_json, match_info=match or {}, content_length=None,
                           app={"state": SimpleNamespace(subagents=gateway.manager)})


def test_the_watch_spawns_each_stage_when_the_stages_it_needs_are_done(tmp_path, handlers, monkeypatch):
    data = tmp_path / "data"
    _project(data)
    monkeypatch.setattr(gen, "WATCH_INTERVAL_SECS", 3600)  # only the run tasks wake the watch
    gateway = Gateway(StagedKiro())
    ctx = SimpleNamespace(data_dir=str(data), spawn=gateway, logger=SimpleNamespace(exception=lambda *a, **k: None))

    async def until(check, what: str):
        for _ in range(400):
            if check():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(what)

    def stages() -> list[str]:
        return [stage for _sid, stage, _t in gateway.calls]

    async def scenario():
        started = await handlers[("POST", "/generations")](_request({"projectId": PID, "brief": BRIEF}, gateway=gateway), ctx)
        gid = started.payload["id"]
        assert started.status == 202 and started.payload["staged"] is True and stages() == ["structure"]
        gateway.finish("spawn-1")
        await until(lambda: sorted(stages()) == ["evaluation", "skills", "structure"], "evaluation and skills start together")
        for sid, stage, _t in list(gateway.calls):
            if stage in ("evaluation", "skills"):
                gateway.finish(sid)
        await until(lambda: {"prompts", "knowledge-1", "knowledge-2"} <= set(stages()), "the file stages start together")
        record = gen._read_record(data, gid)
        assert {s["name"] for s in record["stages"] if s["status"] == "running"} == {"prompts", "knowledge-1", "knowledge-2"}
        for sid, stage, _t in list(gateway.calls):
            if stage in ("prompts", "knowledge-1", "knowledge-2"):
                gateway.finish(sid)
        await until(lambda: gen._read_record(data, gid)["status"] != "running", "the draft completes")
        return gid

    gid = asyncio.run(scenario())
    record = gen._read_record(data, gid)
    assert record["status"] == "ready", record.get("error")
    assert len(gateway.calls) == 6 and len({sid for sid, _s, _t in gateway.calls}) == 6
    assert {s["name"]: s["spawnId"] for s in record["stages"]} == {stage: sid for sid, stage, _t in gateway.calls}


def test_a_poll_restarts_the_watch_of_a_staged_draft(tmp_path, handlers, monkeypatch):
    """After a gateway restart no watch runs: the page's next poll starts one, which spawns the next stages."""
    data = tmp_path / "data"
    _project(data)
    monkeypatch.setattr(gen, "WATCH_INTERVAL_SECS", 3600)
    kiro = StagedKiro()
    record = _start(data)
    gen.claim_stage(data, record["id"], "structure")
    record = gen.finish_stage(data, record["id"], "structure", kiro.reply("structure", gen.stage_task(data, record, "structure")))
    assert gen.runnable_stages(record) == ["evaluation", "skills"]
    gateway = Gateway(kiro)
    ctx = SimpleNamespace(data_dir=str(data), spawn=gateway, logger=SimpleNamespace(exception=lambda *a, **k: None))

    async def scenario():
        polled = await handlers[("GET", "/generations/{generation_id}")](_request(match={"generation_id": record["id"]}, gateway=gateway), ctx)
        assert polled.payload["status"] == "running"
        for _ in range(400):
            if len(gateway.calls) == 2:
                break
            await asyncio.sleep(0.01)
        return sorted(stage for _sid, stage, _t in gateway.calls)

    assert asyncio.run(scenario()) == ["evaluation", "skills"]


# ---------------------------------------------------------------------------
# The engine's teaching rules in the stage checks
# ---------------------------------------------------------------------------


def _stage_outputs() -> tuple[dict, dict]:
    kiro = StagedKiro()
    first = json.loads(kiro.reply("structure", "")[len("WORKSHOP_PACK_JSON_BEGIN\n"):-len("\nWORKSHOP_PACK_JSON_END")])
    second = json.loads(kiro.reply("evaluation", "")[len("WORKSHOP_PACK_JSON_BEGIN\n"):-len("\nWORKSHOP_PACK_JSON_END")])
    return first, second


def test_the_reference_stages_pass_their_checks():
    first, second = _stage_outputs()
    assert gen.check_stage("structure", first, stage={}, outputs={}) == []
    assert gen.check_stage("evaluation", second, stage={}, outputs={"structure": first}) == []


def test_the_structure_check_refuses_a_role_argument_on_a_mock_tool():
    first, _second = _stage_outputs()
    mock = next(t for t in first["scenario"]["tools"] if t["kind"] == "mock")
    mock["inputSchema"]["properties"]["callerRole"] = {"type": "string", "description": "the asker's role"}
    problems = gen.check_stage("structure", first, stage={}, outputs={})
    assert any(p.startswith("teaching.gated_tool_role_argument: mock tool " + repr(mock["name"])) for p in problems)


def test_the_evaluation_check_runs_the_engines_teaching_rules():
    first, second = _stage_outputs()
    mock = next(t for t in first["scenario"]["tools"] if t["kind"] == "mock")
    value = second["scenario"]["labs"]["teaching"]["firstConversation"]["mustNotMention"][0]
    mock["description"] += f" (for example {value})"
    problems = gen.check_stage("evaluation", second, stage={}, outputs={"structure": first})
    assert any(p.startswith("teaching.first_term_in_tool_spec:") for p in problems)


def test_the_evaluation_check_finds_a_memory_marker_in_the_facts_of_a_document():
    """Live 2026-10-15: mustNotMention 'B' was a cabin class the rules document states; the knowledge stage writes the
    documents later, so the facts written into them are read instead (a fact's source names its file)."""
    first, second = _stage_outputs()
    doc = first["scenario"]["knowledge"]["documents"][0]
    first["scenario"]["facts"].append({"id": "fact-cabins", "statement": "Economy has the cabins Y, B and S.",
                                       "source": f"{doc['file']} (cabins)"})
    second["scenario"]["labs"]["teaching"]["firstConversation"]["mustNotMention"] = ["B"]
    problems = gen.check_stage("evaluation", second, stage={}, outputs={"structure": first})
    assert any(p.startswith("teaching.first_term_in_kb:") and doc["file"] in p for p in problems)


def test_a_short_document_is_asked_again():
    first, second = _stage_outputs()
    path = first["scenario"]["knowledge"]["documents"][0]["file"]
    _scenario, files = _template()
    problems = gen.check_stage("knowledge-1", {"files": {path: files[path][:200]}}, stage={"write": [path]},
                               outputs={"structure": first, "evaluation": second})
    assert any(p.startswith(f"K1: {path} is ") for p in problems)
    assert not any(p.startswith("K1:") for p in gen.check_stage("knowledge-1", {"files": {path: files[path]}}, stage={"write": [path]},
                                                                  outputs={"structure": first, "evaluation": second}))


def test_the_stage_checks_run_the_scenario_schema_on_their_own_sections():
    """Live 2026-10-12: an absentTerm of one character ('猫') passed every stage check and failed the schema after Apply."""
    first, second = _stage_outputs()
    gap = next(p for p in second["scenario"]["labs"]["teaching"]["phenomena"] if p["kind"] == "retrieval_gap")
    gap["absentTerms"] = ["猫"]
    problems = gen.check_stage("evaluation", second, stage={}, outputs={"structure": first})
    assert any(p.startswith("schema: labs/teaching/phenomena/") and "'猫' is too short" in p for p in problems)
    gap["absentTerms"] = ["宠物猫", "猫"]  # a short term beside a usable one is dropped (with a warning), not asked again
    assert not any("'猫'" in p for p in gen.check_stage("evaluation", second, stage={}, outputs={"structure": first}))
    first["scenario"]["facts"][0]["criticality"] = "urgent"
    assert "schema: facts/0/criticality: 'urgent' is not one of ['blocking', 'advisory']" in gen.check_stage("structure", first, stage={}, outputs={})
    # the structure reply has no golden set yet: the schema errors of the sections it does not write are not its problems
    assert not any(p.startswith("schema: evaluation") or p.startswith("schema: labs") or p.startswith("schema: <root>")
                   for p in gen.check_stage("structure", first, stage={}, outputs={}))


def test_the_structure_check_refuses_a_one_character_absent_term():
    first, _second = _stage_outputs()
    first["plan"]["absentTerms"] = ["猫"]
    problems = gen.check_stage("structure", first, stage={}, outputs={})
    assert any(p.startswith("plan.absentTerms") and "'猫'" in p for p in problems)
    first["plan"]["absentTerms"] = ["宠物", "猫"]
    assert not any(p.startswith("plan.absentTerms") for p in gen.check_stage("structure", first, stage={}, outputs={}))
    plan = gen._stage_input("evaluation", {}, {"structure": first}, {})["plan"]
    assert plan["absentTerms"] == ["宠物"]  # the next stages never see the one-character term


def test_a_one_character_gap_term_beside_a_usable_one_is_dropped_with_a_warning(tmp_path):
    result = _one_reply_result(tmp_path)
    scenario, files = _template()
    gap = next(p for p in scenario["labs"]["teaching"]["phenomena"] if p["kind"] == "retrieval_gap")
    gap["absentTerms"] = [*gap["absentTerms"], "猫"]
    out = gen._normalise_bundle({"status": "ready", "scenario": scenario, "files": files}, project_id=PID, pack_kind="reference")
    kept = next(p for p in out["scenario"]["labs"]["teaching"]["phenomena"] if p["kind"] == "retrieval_gap")
    assert "猫" not in kept["absentTerms"] and kept["absentTerms"]
    assert any("dropped the one-character term '猫'" in w for w in out["warnings"])
    assert result["precheck"]["ran"]


def test_a_ready_draft_carries_what_validation_will_report_after_apply(tmp_path):
    result = _one_reply_result(tmp_path)
    assert result["precheck"]["ran"] and result["precheck"]["errorCount"] == 0
    assert result["precheck"]["review"] > 0  # every Kiro item waits for the SA's review
    broken = copy.deepcopy(result)
    gap = next(p for p in broken["scenario"]["labs"]["teaching"]["phenomena"] if p["kind"] == "retrieval_gap")
    gap["absentTerms"].append("猫")
    precheck = gen._precheck(broken, tmp_path)
    assert precheck["errorCount"] == 1 and "'猫' is too short" in precheck["errors"][0]


def _prompts_reply(scenario: dict, files: dict, *, drop: str) -> str:
    """The prompts stage's reply with one teaching sentence taken out of its file (``fix`` from the candidate,
    ``marker`` from the baseline)."""
    prompts = scenario["prompts"]
    baseline, candidate = files[prompts["baselineFile"]], files[prompts["optimizationCandidateFile"]]
    defect = next(d for d in scenario["labs"]["teaching"]["baselineDefects"] if d.get("baselineMarker"))
    if drop == "fix":
        candidate = candidate.replace(defect["candidateFix"], "")
    else:
        baseline = baseline.replace(defect["baselineMarker"], "")
    return _answer({"files": {prompts["baselineFile"]: baseline, prompts["optimizationCandidateFile"]: candidate}})


CUT_OFF = "WORKSHOP_PACK_JSON_BEGIN\n{\"files\": {\"knowledge-base/docs/x.md\": \"# Part"


def test_an_unusable_rewrite_keeps_the_usable_first_reply(tmp_path):
    data = tmp_path / "data"
    _project(data)
    scenario, files = _template()
    first = StagedKiro().structure()["scenario"]["knowledge"]["documents"][0]["file"]
    kiro = StagedKiro()
    batch = None

    def reply(stage: str, task: str) -> str:
        nonlocal batch
        if stage.startswith("knowledge") and first in _section(task, "STAGE_INPUT")["write"]:
            batch = stage
            done = sum(1 for s, _t in kiro.tasks if s == stage)
            kiro.tasks.append((stage, task))
            write = _section(task, "STAGE_INPUT")["write"]
            if done == 0:  # a short document: K1 asks again
                return _answer({"files": {f: (files[f][:200] if f == first else files[f]) for f in write}})
            if done == 1:
                return CUT_OFF  # the rewrite is cut off: the first reply stands, the draft goes on
            return _answer({"files": {f: files[f] for f in write}})
        return kiro.reply(stage, task)

    record = gen.drive_stages(data, _start(data), reply)
    assert record["status"] == "ready", record.get("error")
    stage = gen._stage(record, batch)
    # attempt 1 asked again (K1), attempt 2 cut off (the first reply kept, no failure), attempt 3 the precheck rerun
    assert [h["kind"] for h in stage["history"]] == ["output", "precheck"] and stage["attempts"] == 3
    assert any(p.startswith(f"K1: {first} is ") for p in stage["history"][0]["problems"])


def test_a_precheck_rerun_that_fails_falls_back_to_the_reply_it_reran(tmp_path):
    data = tmp_path / "data"
    _project(data)
    scenario, files = _template()
    weak = _prompts_reply(scenario, files, drop="fix")
    record = gen.drive_stages(data, _start(data), StagedKiro({"prompts": [weak, weak, CUT_OFF]}).reply)
    assert record["status"] == "ready", record.get("error")
    prompts = gen._stage(record, "prompts")
    assert prompts["attempts"] == 3 and prompts["kept"] == "before-precheck"
    assert record["result"]["precheck"]["errorCount"] >= 1  # the SA sees it before Apply


def test_a_precheck_error_without_a_file_in_it_reruns_the_stage_its_code_belongs_to(tmp_path):
    data = tmp_path / "data"
    _project(data)
    scenario, files = _template()
    weak = _prompts_reply(scenario, files, drop="marker")  # "baselineMarker not found in the baseline prompt"
    record = gen.drive_stages(data, _start(data), StagedKiro({"prompts": [weak, weak]}).reply)
    assert record["status"] == "ready", record.get("error")
    assert record["precheckRedo"] == ["prompts"] and gen._stage(record, "prompts")["attempts"] == 3
    assert record["result"]["precheck"]["errorCount"] == 0


def test_the_kept_reply_is_the_one_with_the_less_serious_problems():
    warning = sorted(gen.EVALUATION_TEACHING_CODES)[0]  # a live-learned teaching warning
    assert gen._problem_rank(["schema: facts/0/criticality: 'x' is not one of"]) > gen._problem_rank(["S2: a", "S2: b"])
    assert gen._problem_rank(["teaching.missing_kind: no refusal phenomenon"]) > gen._problem_rank(["S2: a", "S1: b"])  # blocks the build
    assert gen._problem_rank(["S2: a"]) > gen._problem_rank([f"{warning}: a", f"{warning}: b"])
    # a rewrite that drops a file it had to write blocks the assembly (live: the draft failed after every stage was done)
    assert gen._problem_rank(["files is missing: knowledge-base/docs/sla.md"]) > gen._problem_rank(["K3: knowledge-base/docs/a.md lacks the literal probe answer terms ['4']"])
    # an unknown roleId the app replaces with the first role ranks with the warnings
    assert gen._problem_rank(["S3: case c1: roleId 'x' is not a role id"]) < gen._problem_rank(["S2: case c1: basis ['d'] must be fact ids"])


def test_a_case_whose_role_or_basis_the_app_fills_in_is_warned():
    scenario, files = _template()
    case = scenario["evaluation"]["goldenSet"][0]
    case["roleId"], case["basis"] = "contractor", []
    out = gen._normalise_bundle({"status": "ready", "scenario": scenario, "files": files}, project_id=PID, pack_kind="reference")
    assert any(w.startswith(f"evaluation.goldenSet[{case['id']}].roleId: unknown role 'contractor'") for w in out["warnings"])
    assert any(w.startswith(f"evaluation.goldenSet[{case['id']}].basis: none given") for w in out["warnings"])


def test_a_ready_draft_whose_project_changed_is_flagged_stale(tmp_path):
    data = tmp_path / "data"
    pdir = _project(data)
    record = gen.drive_stages(data, _start(data), StagedKiro().reply)
    assert record["status"] == "ready"
    status, payload = gen.generation_route("GET", ["generations", "latest", PID], {}, data)
    assert status == 200 and payload["generation"]["stale"] is False
    (pdir / "scenario.yaml").write_text((pdir / "scenario.yaml").read_text(encoding="utf-8") + "# a review recorded\n", encoding="utf-8")
    status, payload = gen.generation_route("GET", ["generations", record["id"]], {}, data)
    assert status == 200 and payload["stale"] is True  # apply would refuse it; the page says so first


def test_an_applied_draft_offers_undo_only_while_the_project_is_as_the_apply_left_it(tmp_path):
    data = tmp_path / "data"
    pdir = _project(data)
    record = gen.drive_stages(data, _start(data), StagedKiro().reply)
    gen.apply_generation(record["id"], data, {"acknowledged": True})
    status, payload = gen.generation_route("GET", ["generations", "latest", PID], {}, data)
    assert status == 200 and payload["generation"]["status"] == "applied" and payload["generation"]["revertable"] is True
    (pdir / "scenario.yaml").write_text((pdir / "scenario.yaml").read_text(encoding="utf-8") + "# a review recorded\n", encoding="utf-8")
    status, payload = gen.generation_route("GET", ["generations", record["id"]], {}, data)
    assert status == 200 and payload["revertable"] is False  # revert would refuse it; the page says so instead of a button


def test_a_rewrite_whose_runs_keep_failing_keeps_the_usable_first_reply(tmp_path):
    data = tmp_path / "data"
    _project(data)
    scenario, files = _template()
    weak = _prompts_reply(scenario, files, drop="fix")
    kiro = StagedKiro({"prompts": [weak]})
    calls = []

    def run(stage: str, task: str) -> str:
        if stage == "prompts":
            calls.append(task)
            if len(calls) > 1 and len(calls) <= 1 + gen.STAGE_RUN_RETRIES + 1:
                raise RuntimeError("Kiro response stream: dispatch failure")  # every run of the rewrite breaks off
        return kiro.reply(stage, task)

    record = gen.drive_stages(data, _start(data), run)
    assert record["status"] == "ready", record.get("error")
    prompts = gen._stage(record, "prompts")
    assert prompts["status"] == "done" and any(h["kind"] == "run" for h in prompts["history"])
    assert record["result"]["files"]  # the draft was assembled from the first reply (and its precheck rerun)


def test_a_precheck_rerun_that_makes_the_precheck_worse_is_undone(tmp_path):
    data = tmp_path / "data"
    _project(data)
    scenario, files = _template()
    weak = _prompts_reply(scenario, files, drop="fix")
    worse = _prompts_reply(scenario, files, drop="marker")  # passes no better: a second teaching error
    prompts = scenario["prompts"]
    both = json.loads(worse[len("WORKSHOP_PACK_JSON_BEGIN\n"):-len("\nWORKSHOP_PACK_JSON_END")])
    both["files"][prompts["optimizationCandidateFile"]] = json.loads(weak[len("WORKSHOP_PACK_JSON_BEGIN\n"):-len("\nWORKSHOP_PACK_JSON_END")])["files"][prompts["optimizationCandidateFile"]]
    original_check = gen.check_stage

    def lenient(name, payload, **kw):  # the stage check lets the rerun through, so only the precheck can judge it
        return [] if gen._stage_base(name) == "prompts" else original_check(name, payload, **kw)
    gen.check_stage = lenient
    try:
        record = gen.drive_stages(data, _start(data), StagedKiro({"prompts": [weak, _answer(both)]}).reply)
    finally:
        gen.check_stage = original_check
    assert record["status"] == "ready", record.get("error")
    assert record.get("precheckUndone") is True and gen._stage(record, "prompts")["kept"] == "before-precheck"
    assert gen._stage(record, "prompts")["keptAttempt"] == 1  # the reply the rerun was asked to redo
    assert record["result"]["precheck"]["errorCount"] == record["precheckBefore"]
