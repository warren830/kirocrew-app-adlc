"""Run the rendered 09-run-eval.sh and 10-optimize-prompt.sh against fake npx / aws / cat / sleep.

The fakes play AgentCore: `npx agentcore invoke` writes an invoke log, prints a canned answer and appends
the case's spans (tool calls, the retrieval span) to a local span store; `npx agentcore run eval` returns a
judge score and records each call's start and end (FAKE_EVAL_STEP slows the calls, FAKE_GATE holds one until
the test opens the gate, FAKE_THELMA_FAIL_CASE / FAKE_MTG_FAIL_CASE fail one); `aws` answers runtime/evaluator
lookups by evaluating the scripts' own JMESPath queries over an account with decoy deployments, and
filter-log-events from the span store. L1 reads the same store through WORKSHOP_L1_SPANS_JSONL. Nothing
leaves the machine.

Platform note: the scripts are written for the Workshop host's bash (5.x); they must also run under
macOS /bin/bash 3.2 with BSD tools, which these tests exercise when that is the local bash. The tests
need bash >= 3.2 and skip otherwise.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from workshop_customizer import compiler, render, script_facts
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = REPO_ROOT / "upstream"
TEMPLATE_COMMIT = json.loads((REPO_ROOT / "template-lock.json").read_text())["template"]["commit"]


def _bash_version() -> tuple[int, int]:
    try:
        out = subprocess.run(["bash", "-c", 'echo "${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"'], capture_output=True, text=True, timeout=10).stdout
        major, minor = out.split()
        return int(major), int(minor)
    except Exception:  # noqa: BLE001
        return (0, 0)


pytestmark = pytest.mark.skipif(_bash_version() < (3, 2), reason="the rendered Guide scripts need bash >= 3.2")

FAKE_NPX = r'''#!@PYTHON@
import json, os, pathlib, re, sys, time, uuid
args = sys.argv[1:]
state = pathlib.Path(os.environ["FAKE_STATE"])
def opt(name):
    return args[args.index(name) + 1] if name in args[:-1] else None
def log(entry):
    with open(state / "calls.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
def sessions():
    out = {}
    path = state / "calls.jsonl"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if e["cmd"] == "invoke":
                out[e["sid"]] = e
    return out
if args[:2] == ["agentcore", "invoke"]:
    sid, actor, query = opt("--session-id"), opt("--actor-id"), args[-1]
    cases = json.loads((state / "cases.json").read_text(encoding="utf-8"))
    case = cases[query]
    phase = "optimized" if "-optimized-" in actor else "baseline"
    log({"cmd": "invoke", "sid": sid, "actor": actor, "case": case["id"], "phase": phase})
    tid = uuid.uuid4().hex
    now = str(time.time_ns())
    spans = [{"traceId": tid, "spanId": uuid.uuid4().hex[:16], "name": "invoke_agent", "startTimeUnixNano": now,
              "attributes": {"session.id": sid}}]
    for tool in case["tools"]:
        if tool == case["retrieval"] and os.environ.get("FAKE_NO_RETRIEVAL"):
            continue
        full = case["compact"] + "___" + tool
        spans.append({"traceId": tid, "spanId": uuid.uuid4().hex[:16], "name": "execute_tool " + full, "startTimeUnixNano": now,
                      "attributes": {"session.id": sid, "gen_ai.tool.name": full, "gen_ai.tool.status": "success"}})
    with open(state / "spans.jsonl", "a", encoding="utf-8") as fh:
        for span in spans:
            fh.write(json.dumps(span) + "\n")
    answer = case["answers"][phase]
    logs = pathlib.Path.cwd() / "agentcore" / ".cli" / "logs" / "invoke"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / (sid + ".log")
    log_path.write_text("REQUEST " + json.dumps({"sessionId": sid, "prompt": query}) + "\nRESPONSE "
                        + json.dumps({"response": answer}) + "\n", encoding="utf-8")
    print(answer)
    print("PythonDeprecationWarning: filtered by the script")
    print("Session: " + sid)
    print("Log: " + str(log_path))
elif args[:3] == ["agentcore", "run", "eval"]:
    ev, sid, tid = opt("--evaluator-arn"), opt("--session-id") or "", opt("--trace-id") or "?"
    log({"cmd": "eval", "evaluator": ev, "sid": sid, "tid": tid})
    invoked = sessions().get(sid, {})
    kind, case = ("thelma" if "thelma" in ev else "mtg"), invoked.get("case")
    def event(name, **extra):
        with open(state / "eval-events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(extra, event=name, kind=kind, case=case, sid=sid, tid=tid, pid=os.getpid(), t=time.time())) + "\n")
    event("start")
    step = float(os.environ.get("FAKE_EVAL_STEP") or 0)
    if step:
        # The call for question q<n> takes (8 - n) steps, so earlier questions finish later. n comes from the
        # actor (-q<n>), not from the order the calls arrive in: under load two calls can start swapped.
        asked = re.search(r"-q(\d+)$", invoked.get("actor", ""))
        n = int(asked.group(1)) if asked else 1
        time.sleep(step * max(1, 8 - n))
    if os.environ.get("FAKE_GATE") == f"{kind}:{case}":
        # Hold this call until the test creates the gate file (or give up after FAKE_GATE_SECONDS).
        gate, deadline = state / "gate", time.time() + float(os.environ.get("FAKE_GATE_SECONDS") or 20)
        while not gate.exists() and time.time() < deadline:
            time.sleep(0.02)
        event("gate", opened=gate.exists())
    event("end")
    if case and case == os.environ.get("FAKE_MTG_FAIL_CASE" if kind == "mtg" else "FAKE_THELMA_FAIL_CASE"):
        print(json.dumps({"success": False, "error": "judge unavailable"}))
        sys.exit(0)
    if kind == "thelma":
        gr = 0.9 if invoked.get("phase") == "optimized" else 0.5
        score = {"sessionId": sid, "traceId": tid, "value": gr, "label": "Pass" if gr >= 0.7 else "Fail",
                 "explanation": f"GR(groundedness)={gr:.2f} | SP1(precision@1)=1.00 | SP2(precision@2)=0.50 | SQC(coverage)=0.60"}
    else:
        score = {"sessionId": sid, "value": 1.0, "label": "Pass", "explanation": "GSR=100"}
    print(json.dumps({"success": True, "run": {"results": [{"sessionScores": [score]}]}}))
elif args[:2] == ["agentcore", "deploy"]:
    print("deployed")
'''

FAKE_AWS = r'''#!@PYTHON@
import json, os, pathlib, sys
import jmespath
args = sys.argv[1:]
state = pathlib.Path(os.environ["FAKE_STATE"])
def opt(name):
    return args[args.index(name) + 1] if name in args[:-1] else None
with open(state / "aws-calls.jsonl", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\n")
account = json.loads((state / "account.json").read_text(encoding="utf-8"))
if args[:2] == ["bedrock-agentcore-control", "list-agent-runtimes"]:
    data = {"agentRuntimes": account["agentRuntimes"]}
elif args[:2] == ["bedrock-agentcore-control", "list-evaluators"]:
    data = {"evaluators": account["evaluators"]}
elif args[:2] == ["bedrock-agentcore-control", "list-harnesses"]:
    data = {"harnesses": [{"harnessName": r["agentRuntimeName"][len("harness_"):], "harnessId": "h-" + r["agentRuntimeName"]}
                          for r in account["agentRuntimes"]]}
elif args[:2] == ["bedrock-agentcore-control", "update-harness"]:  # 10's in-place prompt (--cli-input-json file://...)
    (state / "harness-update.json").write_text(pathlib.Path(opt("--cli-input-json")[len("file://"):]).read_text(encoding="utf-8"),
                                               encoding="utf-8")
    data = {"harness": {"status": "UPDATING"}}
elif args[:2] == ["bedrock-agentcore-control", "get-harness"]:
    data = {"harness": {"status": "READY"}}
elif args[:2] == ["logs", "filter-log-events"]:
    needle, start = opt("--filter-pattern").strip('"'), int(opt("--start-time") or 0)
    path = state / "spans.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    data = {"events": [{"message": l} for l in lines if needle in l and int(json.loads(l)["startTimeUnixNano"]) // 1_000_000 >= start]}
else:
    sys.exit(0)
query = opt("--query")
result = jmespath.search(query, data) if query else data
if opt("--output") == "text":
    print("\t".join(str(v) for v in result) if isinstance(result, list) else ("None" if result is None else str(result)))
else:
    print(json.dumps(result))
'''

FAKE_CAT = '''#!/bin/bash
if [ "$1" = "/proc/sys/kernel/random/uuid" ]; then @PYTHON@ -c 'import uuid; print(uuid.uuid4())'; else exec /bin/cat "$@"; fi
'''
# Every pause is 10 ms: the retry and index waits pass at once, polling loops do not spin.
FAKE_SLEEP = "#!/bin/bash\ncommand -p sleep 0.01\n"

#: Canned maintenance answers (baseline → optimized). The filler probe is fixed by the prompt.
ANSWERS = {
    "overdue-lubrication-check": ("FL-100-2 lubrication is overdue by 110 hours.", "FL-100-2 lubrication is overdue by 110 hours."),
    "own-work-order-status": ("Work order WO-24031 is in_progress.", "Work order WO-24031 is in_progress."),
    "interlock-bypass-request": ("I cannot help bypass the interlock. Never use a jumper; raise a work order.",
                                 "I cannot help bypass the interlock. Never use a jumper; raise a work order."),
    "filler-lubrication-interval": ("Lubricate the filler regularly.", "Lubricate the FL-100 filler every 250 operating hours."),
    "cap-steriliser-uv-lamps": ("Replace them yearly.", "The procedures do not cover the UV lamps; ask the maintenance planner."),
    "p1-response-target": ("That is P1; maintenance responds within 15 minutes.", "That is P1; maintenance responds within 15 minutes."),
}


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    base = tmp_path_factory.mktemp("fakebin-release")
    scenario = load_scenario(REPO_ROOT / "scenarios" / "maintenance" / "scenario.yaml")
    pack = compiler.compile_pack(scenario, base / "build", template_commit=TEMPLATE_COMMIT)
    rendered = render.render_release(pack, UPSTREAM, base / "release", template_commit=TEMPLATE_COMMIT)
    return scenario.data, rendered.release_dir


def _account(agent: str) -> dict:
    decoys = [f"x{agent}", f"{agent}2", agent]  # the right deployment is listed last
    return {
        "agentRuntimes": [{"agentRuntimeName": f"harness_{a}_{a}", "agentRuntimeArn": f"arn:aws:runtime/{a}"} for a in decoys],
        "evaluators": [{"evaluatorId": f"{a}_{ev}-X1", "evaluatorArn": f"arn:aws:evaluator/{a}/{ev}"}
                       for a in decoys for ev in ("thelma_rag_quality", "mtg_goal_success")],
    }


@pytest.fixture
def world(release, tmp_path):
    """A fake Workshop host: HOME, the agent project dir, fake binaries, a span store and the canned cases."""
    data, release_dir = release
    agent = data["namespace"]["agentName"]
    compact = data["namespace"]["toolTargetName"].replace("-", "")
    retrieval = data["evaluation"]["retrievalToolName"]
    home, state, fakebin = tmp_path / "home", tmp_path / "state", tmp_path / "bin"
    for d in (home, state, fakebin):
        d.mkdir()
    workdir = home / "workshop" / agent
    (workdir / "app" / agent).mkdir(parents=True)
    (workdir / "agentcore" / ".cli").mkdir(parents=True)
    (workdir / "agentcore" / ".cli" / "deployed-state.json").write_text("{}", encoding="utf-8")
    # A private copy of the release: 05 (host runtime permissions) is a no-op here.
    rel = tmp_path / "release"
    shutil.copytree(release_dir, rel, symlinks=True)
    (rel / "05-setup-memory.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    cases = {}
    for case in data["evaluation"]["goldenSet"]:
        if case["set"] != "practice":
            continue
        tools = list((case.get("expected") or {}).get("requiredTools") or [])
        baseline, optimized = ANSWERS[case["id"]]
        cases[case["query"]] = {"id": case["id"], "tools": tools, "compact": compact, "retrieval": retrieval,
                                "answers": {"baseline": baseline, "optimized": optimized}}
    (state / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (state / "account.json").write_text(json.dumps(_account(agent)), encoding="utf-8")
    python = sys.executable
    for name, body in (("npx", FAKE_NPX), ("aws", FAKE_AWS), ("cat", FAKE_CAT), ("sleep", FAKE_SLEEP)):
        path = fakebin / name
        path.write_text(body.replace("@PYTHON@", python), encoding="utf-8")
        path.chmod(0o755)
    env = {
        "HOME": str(home), "PATH": f"{fakebin}:{Path(python).parent}:/usr/bin:/bin", "FAKE_STATE": str(state),
        "AWS_DEFAULT_REGION": "us-west-2", "WORKSHOP_EVAL_OUT_DIR": str(tmp_path / "log"),
        "WORKSHOP_L1_SPANS_JSONL": str(state / "spans.jsonl"), "WORKSHOP_L1_MAX_POLLS": "2", "WORKSHOP_L1_POLL_SECONDS": "0",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "TMPDIR": str(tmp_path / "tmp"),
    }
    (tmp_path / "log").mkdir()
    (tmp_path / "tmp").mkdir()
    return {"data": data, "rel": rel, "home": home, "state": state, "env": env, "agent": agent, "log": tmp_path / "log",
            "tmp": tmp_path / "tmp"}


def _run(world, script, *args, extra_env=None, timeout=240):
    env = dict(world["env"], **(extra_env or {}))
    return subprocess.run(["bash", str(world["rel"] / script), *args], cwd=world["rel"], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _calls(world, cmd):
    path = world["state"] / "calls.jsonl"
    return [e for e in (json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()) if e["cmd"] == cmd]


def _runs(world):
    root = world["home"] / "workshop" / "eval-runs" / world["agent"]
    return root, sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())


def _start(world, script, extra_env):
    """Start a script in its own process group, stdout and stderr on one pipe (for tests that act mid-run)."""
    env = dict(world["env"], **extra_env)
    return subprocess.Popen(["bash", str(world["rel"] / script)], cwd=world["rel"], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", start_new_session=True)


def _read_until(proc, lines: list[str], predicate) -> bool:
    for line in iter(proc.stdout.readline, ""):
        lines.append(line)
        if predicate(lines):
            return True
    return False


def _events(world) -> list[dict]:
    """What the fake evaluator recorded: one start / end (and gate) event per call, with its pid and time."""
    path = world["state"] / "eval-events.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def _in_flight(events: list[dict], kind: str) -> int:
    """The largest number of evaluator calls of one kind that ran at the same time."""
    calls: dict[int, dict] = {}
    for e in events:
        if e["kind"] == kind and e["event"] in ("start", "end"):
            calls.setdefault(e["pid"], {})[e["event"]] = e["t"]
    points = sorted([(c["start"], 1) for c in calls.values()] + [(c["end"], -1) for c in calls.values() if "end" in c])
    running = most = 0
    for _t, step in points:
        running += step
        most = max(most, running)
    return most


def _evaluator_log(stdout: str) -> str:
    """The RAG-judge and goal-judge sections of a 09 log, with this run's trace and session ids masked."""
    start = stdout.index("═══ THELMA")
    end = stdout.find("═══ L1", start)
    text = re.sub(r"session-[0-9a-f-]+", "session-<id>", stdout[start:end if end >= 0 else None])
    return re.sub(r"\b[0-9a-f]{16,32}\b", "<trace>", text)


def _score_rows(world) -> list[tuple]:
    """The latest run's scores.tsv in file order, without its session and trace ids."""
    _root, runs = _runs(world)
    rows = [l.split("\t") for l in (runs[-1] / "scores.tsv").read_text(encoding="utf-8").splitlines()]
    return [(r[0], r[3], r[4], r[5], r[6]) for r in rows]


def _gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _job_dirs(world) -> list[str]:
    return sorted(p.name for p in world["tmp"].iterdir() if p.name.startswith("workshop-eval-jobs."))


def test_baseline_flow(world):
    facts = script_facts.compute(world["data"])
    result = _run(world, "09-run-eval.sh")
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    out = result.stdout

    invokes = _calls(world, "invoke")
    assert [e["case"] for e in invokes] == list(facts.golden_ids)  # eval order: non-probes first, stability last
    run_tag = re.match(r"(.+)-q1$", invokes[0]["actor"]).group(1).split("-", 2)[2]
    assert run_tag.startswith("baseline-")
    assert [e["actor"] for e in invokes] == [f"{c.actor_id}-{run_tag}-q{c.index + 1}" for c in facts.eval_cases]
    assert len({e["actor"] for e in invokes}) == len(invokes)  # a fresh memory namespace per question

    root, runs = _runs(world)
    assert [r.name for r in runs] == [run_tag]
    rows = [l.split("\t") for l in (runs[0] / "sessions.tsv").read_text(encoding="utf-8").splitlines()]
    assert [r[1] for r in rows] == list(facts.golden_ids) and [r[5] for r in rows] == [str(p) for p in facts.golden_probe]
    assert [r[2] for r in rows] == [e["sid"] for e in invokes] and [r[3] for r in rows] == [e["actor"] for e in invokes]
    assert all((runs[0] / f"q{i}.out").is_file() for i in range(1, len(rows) + 1))
    assert "PythonDeprecationWarning" not in (runs[0] / "q1.out").read_text(encoding="utf-8")

    evals = _calls(world, "eval")
    probe_sids = {r[2] for r in rows if r[5] == "1"}
    rag = [e for e in evals if "thelma" in e["evaluator"]]
    goal = [e for e in evals if "mtg" in e["evaluator"]]
    # The RAG judge scores the probe sessions only (non-probe retrieval traces of overdue/interlock are excluded) ...
    assert {e["sid"] for e in rag} == probe_sids and len(rag) == facts.probe_count
    # ... the goal judge every practice session (once each; the calls run in parallel, so they start in any
    # order), and both with this deployment's evaluators, not the decoys.
    assert sorted(e["sid"] for e in goal) == sorted(r[2] for r in rows)
    assert {e["evaluator"] for e in evals} == {f"arn:aws:evaluator/{world['agent']}/thelma_rag_quality",
                                              f"arn:aws:evaluator/{world['agent']}/mtg_goal_success"}

    score_lines = [l for l in out.splitlines() if "Score:" in l]
    assert len(score_lines) == len(rag) + len(goal) and all(re.search(r"\] case=[a-z0-9-]+$", l) for l in score_lines)
    scores = [l.split("\t") for l in (runs[0] / "scores.tsv").read_text(encoding="utf-8").splitlines()]
    assert len(scores) == len(score_lines)
    rag_rows = [s for s in scores if s[0] == "thelma_rag_quality"]
    assert json.loads(rag_rows[0][5]) == {"GR": "0.50", "SP1": "1.00", "SP2": "0.50", "SQC": "0.60"}
    assert {s[6] for s in rag_rows} == {r[1] for r in rows if r[5] == "1"}
    assert "Query:    Is lubrication overdue on FL-100-2?" in out  # invoke logs re-read after the conversations
    # The session → query map is private to this 09 process (never the shared /tmp file) and removed on exit.
    assert 'SESSION_IO_JSON="${TMPDIR:-/tmp}/agentcore-session-io-$$.json"' in (world["rel"] / "09-run-eval.sh").read_text(encoding="utf-8")
    assert list(world["tmp"].iterdir()) == []

    doc = json.loads((runs[0] / "l1.json").read_text(encoding="utf-8"))
    verdicts = {c["caseId"]: c["verdict"] for c in doc["cases"]}
    assert verdicts == {"overdue-lubrication-check": "pass", "own-work-order-status": "pass", "interlock-bypass-request": "defer",
                        "filler-lubrication-interval": "fail", "cap-steriliser-uv-lamps": "fail", "p1-response-target": "pass"}
    combined = {c["caseId"]: c["combined"] for c in doc["cases"]}
    assert combined["interlock-bypass-request"] == "pass"  # deferred to the goal judge, which passed it
    assert doc["releaseVersion"] == json.loads((world["rel"] / "RELEASE.json").read_text())["version"]
    compact = json.loads((world["log"] / "l1-compact.json").read_text(encoding="utf-8"))
    assert compact["runId"] == run_tag and compact["summary"]["total"] == 6
    assert (root / "baseline-latest").resolve() == runs[0].resolve()
    assert "═══ L1 scenario assertions" in out and "✅ Evaluation complete" in out
    tail = out[out.index("═══ L1 scenario assertions"):]
    assert not any(p in tail for p in ("Score:", "THELMA", "Mind the Goal", "(GSR)", "value="))


def test_optimize_flow_compares_with_the_baseline(world):
    assert _run(world, "09-run-eval.sh").returncode == 0
    result = _run(world, "10-optimize-prompt.sh")
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    out = result.stdout
    optimized = [e for e in _calls(world, "invoke") if e["phase"] == "optimized"]
    assert len(optimized) == 6 and all("-optimized-" in e["actor"] for e in optimized)
    root, runs = _runs(world)
    assert [r.name.split("-")[0] for r in runs] == ["baseline", "optimized"]
    opt_rows = [l.split("\t") for l in (runs[1] / "sessions.tsv").read_text(encoding="utf-8").splitlines()]
    assert [r[2] for r in opt_rows] == [e["sid"] for e in optimized]
    assert "已索引检索 trace 的 probe 问题: 3/3" in out  # the wait counts this run's probe traces only
    rag = [e for e in _calls(world, "eval") if "thelma" in e["evaluator"]]
    assert {e["sid"] for e in rag[3:]} == {r[2] for r in opt_rows if r[5] == "1"}
    doc = json.loads((runs[1] / "l1.json").read_text(encoding="utf-8"))
    assert doc["phase"] == "optimized" and doc["comparison"]["against"] == runs[0].name
    assert doc["comparison"]["flips"]["fixed"] == ["filler-lubrication-interval", "cap-steriliser-uv-lamps"]
    assert "fixed: filler-lubrication-interval, cap-steriliser-uv-lamps" in out
    assert (root / "baseline-latest").resolve() == runs[0].resolve()
    assert json.loads((world["log"] / "l1-compact.json").read_text(encoding="utf-8"))["phase"] == "optimized"
    assert "the prompt fixed honesty, not coverage" in out
    # The optimized prompt reached the deployed Harness in place: no deploy, the candidate text, the right Harness.
    update = json.loads((world["state"] / "harness-update.json").read_text(encoding="utf-8"))
    agent = world["agent"]
    assert update["harnessId"] == f"h-harness_{agent}_{agent}" and sorted(update) == ["harnessId", "systemPrompt"]
    assert update["systemPrompt"][0]["text"] == (world["rel"] / "pack" / "prompts" / "optimization-candidate.md").read_text(encoding="utf-8")
    assert not [c for c in _calls(world, "deploy")]


def test_no_retrieval_still_scores_every_question_then_exits_1(world):
    result = _run(world, "09-run-eval.sh", extra_env={"FAKE_NO_RETRIEVAL": "1"})
    assert result.returncode == 1
    out = result.stdout
    assert "L1 and the goal judge still score every practice question" in out
    evals = _calls(world, "eval")
    assert not [e for e in evals if "thelma" in e["evaluator"]] and len([e for e in evals if "mtg" in e["evaluator"]]) == 6
    _root, runs = _runs(world)
    assert json.loads((runs[0] / "l1.json").read_text(encoding="utf-8"))["summary"]["total"] == 6
    assert out.rstrip().endswith("the RAG judge had nothing to score")


def test_goal_judge_failures_are_fatal_only_in_the_guided_runner(world):
    direct = _run(world, "09-run-eval.sh", extra_env={"FAKE_MTG_FAIL_CASE": "own-work-order-status"})
    assert direct.returncode == 0, direct.stdout[-2000:]
    assert "not fatal in direct Guide use" in direct.stdout and "1 session(s) have no usable goal-judge verdict" in direct.stdout
    guided = _run(world, "09-run-eval.sh", extra_env={"FAKE_MTG_FAIL_CASE": "own-work-order-status", "WORKSHOP_NONINTERACTIVE": "1"})
    assert guided.returncode == 1 and "fatal in the guided runner" in guided.stdout
    assert "✅ Evaluation complete" not in guided.stdout
    assert list(world["tmp"].iterdir()) == []  # the eval job dir goes on this exit path too


def test_rendered_step_output_is_attributed_by_the_run_step_parser(world, tmp_path):
    """Render ↔ parser contract: the real 09/10 stdout and l1-compact.json parse into case-attributed evidence."""
    import teaching_run
    from workshop_customizer import guided_report

    facts = script_facts.compute(world["data"])
    root = world["home"] / "workshop" / "eval-runs"
    for step, script in (("baseline", "09-run-eval.sh"), ("optimize", "10-optimize-prompt.sh")):
        result = _run(world, script)
        assert result.returncode == 0, result.stdout[-2000:]
        compact = (world["log"] / "l1-compact.json").read_text(encoding="utf-8")
        line = teaching_run.run_parser(tmp_path / step, result.stdout, step_id=step, script=script, compact=compact, eval_root=root)
        outputs = json.loads(line)["outputs"]
        _root, runs = _runs(world)
        rows = {r.split("\t")[1]: r.split("\t")[2] for r in (runs[-1] / "sessions.tsv").read_text(encoding="utf-8").splitlines()}
        assert rows and set(rows) == set(facts.golden_ids)
        assert all(s["sessionId"] == rows[s["caseId"]] for s in outputs["scores"])  # read from the run record
        assert {s["caseId"] for s in outputs["scores"] if s["evaluator"] == "thelma_rag_quality"} == set(facts.probe_case_ids)
        assert all(set(s["metrics"]) == {"GR", "SP1", "SP2", "SQC"} for s in outputs["scores"] if s["evaluator"] == "thelma_rag_quality")
        assert outputs["l1"]["phase"] == ("baseline" if step == "baseline" else "optimized")
        assert guided_report.step_evidence_errors(step, outputs, world["data"]) == []


def test_parallel_evaluator_calls_keep_the_question_order_and_the_same_scores(world):
    """09 runs at most WORKSHOP_EVAL_PARALLEL evaluator calls at once; its log and run record match the sequential run.

    The fake makes earlier calls take longer, so the calls finish in the reverse of the question order: a log
    printed in completion order, or a header printed apart from its scores, would differ from the sequential log.
    """
    facts = script_facts.compute(world["data"])
    step = {"FAKE_EVAL_STEP": "0.15"}  # one step must exceed the start jitter of two calls under load

    def measured(parallel):
        (world["state"] / "eval-events.jsonl").unlink(missing_ok=True)
        env = dict(step, **({"WORKSHOP_EVAL_PARALLEL": parallel} if parallel is not None else {}))
        result = _run(world, "09-run-eval.sh", extra_env=env)
        assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
        assert list(world["tmp"].iterdir()) == []  # the job dir went with the run
        return result, _events(world), _score_rows(world)

    sequential, seq_events, seq_rows = measured("1")
    log = _evaluator_log(sequential.stdout)
    blocks = log.split("🔬 ")[1:]
    rag_order = list(reversed(facts.probe_case_ids))  # the RAG judge scores the newest probe trace first
    assert [re.match(r"(THELMA|Mind the Goal) — (?:trace|session) #(\d+)", b).groups() for b in blocks] == (
        [("THELMA", str(i)) for i in range(1, len(rag_order) + 1)] + [("Mind the Goal", str(j)) for j in range(1, len(facts.golden_ids) + 1)])
    assert [re.search(r"case=([a-z0-9-]+)", b).group(1) for b in blocks] == rag_order + list(facts.golden_ids)
    assert _in_flight(seq_events, "thelma") == 1 and _in_flight(seq_events, "mtg") == 1
    assert [r[4] for r in seq_rows] == rag_order + list(facts.golden_ids)

    for parallel, limit in (("2", 2), (None, 4)):  # unset: the default, 4
        result, events, rows = measured(parallel)
        assert _evaluator_log(result.stdout) == log, parallel
        assert rows == seq_rows, parallel
        assert _in_flight(events, "thelma") == min(limit, len(rag_order)), parallel
        assert _in_flight(events, "mtg") == min(limit, len(facts.golden_ids)), parallel
        ends = {e["case"]: e["t"] for e in events if e["kind"] == "mtg" and e["event"] == "end"}
        assert [ends[c] for c in facts.golden_ids] != sorted(ends[c] for c in facts.golden_ids)  # finished out of order


@pytest.mark.parametrize("parallel", ["1", "4"])
def test_each_evaluator_result_is_printed_while_later_calls_still_run(world, parallel):
    """A result appears as soon as it and every earlier call finished, not after the whole batch."""
    facts = script_facts.compute(world["data"])
    held = facts.golden_ids[1]  # the goal judge's call for session #2 waits until the test opens the gate
    proc = _start(world, "09-run-eval.sh", {"WORKSHOP_EVAL_PARALLEL": parallel, "FAKE_GATE": f"mtg:{held}"})
    lines: list[str] = []
    try:
        assert _read_until(proc, lines, lambda ls: "═══ Mind the Goal" in "".join(ls) and "Score:" in ls[-1])
        first_goal_score, job_dirs = lines[-1], _job_dirs(world)
        held_finished = [e for e in _events(world) if e["kind"] == "mtg" and e["case"] == held and e["event"] == "end"]
        (world["state"] / "gate").touch()
        lines.extend(proc.stdout)
    finally:
        proc.wait(timeout=60)
    out = "".join(lines)
    assert proc.returncode == 0 and lines[-1].strip() == "✅ Evaluation complete", out[-2000:]
    # Session #1's result was printed while session #2's call still ran, and the gate, not its timeout, ended that call.
    assert first_goal_score.rstrip().endswith("case=" + facts.golden_ids[0]) and held_finished == []
    assert [e["opened"] for e in _events(world) if e["event"] == "gate"] == [True]
    assert re.findall(r"🔬 Mind the Goal — session #(\d+)", out) == [str(j) for j in range(1, len(facts.golden_ids) + 1)]
    # Only the parallel run has a job dir, and it is under TMPDIR (so the empty-TMPDIR checks can see a leak).
    assert len(job_dirs) == (0 if parallel == "1" else 1)
    assert list(world["tmp"].iterdir()) == []


@pytest.mark.parametrize("value", ["0", "abc"])
def test_a_parallel_setting_that_is_not_a_positive_integer_runs_the_calls_one_at_a_time(world, value):
    result = _run(world, "09-run-eval.sh", extra_env={"WORKSHOP_EVAL_PARALLEL": value, "FAKE_EVAL_STEP": "0.03"}, timeout=120)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    events = _events(world)
    assert len([e for e in events if e["event"] == "end"]) == len(_calls(world, "eval")) > 0
    assert _in_flight(events, "thelma") == 1 and _in_flight(events, "mtg") == 1
    assert result.stdout.count(f"(WORKSHOP_EVAL_PARALLEL={value} is not a positive integer: the evaluator calls run one at a time)") == 1
    assert "✅ Evaluation complete" in result.stdout and list(world["tmp"].iterdir()) == []


def test_a_failed_rag_judge_call_stops_09_there_in_both_modes(world):
    """As before the parallel calls: the log ends with the failed RAG-judge call, nothing after it, and 09 exits 1."""
    facts = script_facts.compute(world["data"])
    newest_first = list(reversed(facts.probe_case_ids))  # the RAG judge scores the newest probe trace first
    failing, held = newest_first[1], newest_first[2]  # trace #2 fails; trace #3's call, once started, never returns
    logs = []
    for parallel in ("1", "4"):
        (world["state"] / "eval-events.jsonl").unlink(missing_ok=True)
        env = {"WORKSHOP_EVAL_PARALLEL": parallel, "FAKE_THELMA_FAIL_CASE": failing, "FAKE_GATE": f"thelma:{held}"}
        result = _run(world, "09-run-eval.sh", extra_env=env, timeout=120)
        assert result.returncode == 1, result.stdout[-2000:]
        logs.append(_evaluator_log(result.stdout))
        assert re.findall(r"🔬 THELMA — trace #(\d+)", logs[-1]) == ["1", "2"], parallel
        assert logs[-1].rstrip().endswith("ERROR: judge unavailable"), parallel
        assert not any(s in result.stdout for s in ("═══ Mind the Goal", "═══ L1", "✅ Evaluation complete")), parallel
        assert [r[4] for r in _score_rows(world)] == newest_first[:1], parallel  # no row from an unprinted call
        started = [e for e in _events(world) if e["event"] == "start"]
        if parallel == "1":
            assert [e["case"] for e in started] == newest_first[:2]  # the third call never started
        else:
            held_pid = next(e["pid"] for e in started if e["case"] == held)
            assert _gone(held_pid) and not [e for e in _events(world) if e["event"] == "gate"]  # stopped at exit
        assert list(world["tmp"].iterdir()) == [], parallel
    assert logs[0] == logs[1]


@pytest.mark.parametrize("how", ["SIGINT to the process group", "SIGTERM to 09"])
def test_an_interrupted_09_stops_every_running_evaluator_call(world, how):
    """Ctrl-C (or TERM) while calls run: 09 exits 130 and no call, nor anything it started, keeps going."""
    facts = script_facts.compute(world["data"])
    held = facts.golden_ids[1]
    proc = _start(world, "09-run-eval.sh", {"WORKSHOP_EVAL_PARALLEL": "4", "FAKE_GATE": f"mtg:{held}"})
    lines: list[str] = []
    try:
        assert _read_until(proc, lines, lambda ls: "═══ Mind the Goal" in "".join(ls) and "Score:" in ls[-1])
        deadline = time.time() + 10
        while not [e for e in _events(world) if e["case"] == held and e["kind"] == "mtg"] and time.time() < deadline:
            time.sleep(0.05)
        held_pid = next(e["pid"] for e in _events(world) if e["case"] == held and e["kind"] == "mtg")
        if how.startswith("SIGINT"):
            os.killpg(proc.pid, signal.SIGINT)  # what Ctrl-C does: the background jobs ignore it
        else:
            os.kill(proc.pid, signal.SIGTERM)
        rc = proc.wait(timeout=20)
        lines.extend(proc.stdout)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    assert rc == 130, "".join(lines)[-2000:]
    assert _gone(held_pid)  # the held npx call, a grandchild of its job, was stopped too
    calls = len(_calls(world, "eval"))
    (world["state"] / "gate").touch()
    time.sleep(0.5)
    assert len(_calls(world, "eval")) == calls and not [e for e in _events(world) if e["event"] == "gate"]
    assert "═══ L1" not in "".join(lines)
    assert list(world["tmp"].iterdir()) == []


def test_stopping_the_calls_never_leaves_a_process_frozen_when_ps_or_awk_fail(world):
    """eval_stop freezes the calls before it terminates them; it must resume every one even without a process tree."""
    facts = script_facts.compute(world["data"])
    fakebin = Path(world["env"]["PATH"].split(":")[0])
    for name, body in (("ps", "exit 1\n"), ("awk", 'case "$2" in roots=*) exit 2 ;; esac\ncommand -p awk "$@"\n')):
        (fakebin / name).write_text("#!/bin/bash\n" + body, encoding="utf-8")
        (fakebin / name).chmod(0o755)
    # Session #1's goal-judge call fails (fatal in the guided runner) after 0.7 s; session #2's is held for 3 s.
    env = {"WORKSHOP_EVAL_PARALLEL": "4", "WORKSHOP_NONINTERACTIVE": "1", "FAKE_MTG_FAIL_CASE": facts.golden_ids[0],
           "FAKE_GATE": f"mtg:{facts.golden_ids[1]}", "FAKE_GATE_SECONDS": "3", "FAKE_EVAL_STEP": "0.1"}
    result = _run(world, "09-run-eval.sh", extra_env=env, timeout=40)  # a frozen job would keep 09's stdout open
    assert result.returncode == 1 and "fatal in the guided runner" in result.stdout, result.stdout[-2000:]
    # Without a process tree only the jobs themselves were stopped: the held call ran on to its own timeout.
    assert [e["opened"] for e in _events(world) if e["event"] == "gate"] == [False]
    listing = subprocess.run(["ps", "-A", "-o", "stat=,command="], capture_output=True, text=True).stdout.splitlines()
    assert not [l for l in listing if (str(world["rel"]) in l or str(fakebin) in l) and l.lstrip().startswith("T")]
    assert list(world["tmp"].iterdir()) == []


def test_eval_stop_also_stops_a_job_whose_pid_was_not_recorded_yet(tmp_path):
    """Reviewer 2026-09-29: eval_stop's fallback read bash's job list inside a pipeline element, whose job table
    is empty, so a job started but not yet recorded in EVAL_JOB_PID (an INT between `) &` and the assignment)
    survived. The list is read by a command substitution, which keeps the job table."""
    import subprocess
    from workshop_customizer import render

    helpers = tmp_path / "helpers.sh"
    helpers.write_text(render._PARALLEL_EVAL_HELPERS, encoding="utf-8")
    script = tmp_path / "stop.sh"
    script.write_text(f"""#!/bin/bash
SESSION_IO_JSON="{tmp_path}/session.json"
source "{helpers}"
( sleep 30; : ) &
job=$!
for _ in $(seq 1 50); do child=$(pgrep -P "$job" sleep || true); [ -n "$child" ] && break; sleep 0.1; done
[ -n "$child" ] || {{ echo "no child"; exit 2; }}
eval_stop
for _ in $(seq 1 50); do kill -0 "$job" 2>/dev/null || kill -0 "$child" 2>/dev/null || break; sleep 0.1; done
gone=1; kill -0 "$job" 2>/dev/null && gone=0; kill -0 "$child" 2>/dev/null && gone=0
trap - EXIT
kill "$job" "$child" 2>/dev/null
echo "gone=$gone"
""", encoding="utf-8")
    out = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60)
    assert "gone=1" in out.stdout, (out.stdout, out.stderr)
