#!/usr/bin/env python3
"""Regenerate the raw step logs of tests/fixtures/teaching_run/ (not run by the test suite).

Each run renders a reference pack, then runs the rendered 06-test-conversation.sh, 09-run-eval.sh and
10-optimize-prompt.sh under bash against a fake AgentCore (fake ``npx``/``aws``/``cat``/``date``/``sleep``
on PATH, the same approach as tests/test_eval_flow_fakebin.py). The fakes answer each practice question
with the canned text below, emit tool spans (optionally without the retrieval call) and return the
THELMA numbers configured per case and phase; L1 (the release's l1_eval.py) runs for real. Session ids,
trace ids and clocks are deterministic, and the temporary HOME is rewritten to /home/ssm-user, so the
logs read like the Workshop host's. The 11/12/13 logs in common/ are the live 2026-09-13 tails.

    PYTHONPATH=engine .venv/bin/python3 tests/fixtures/teaching_run/generate.py [run ...]

Then refresh the parsed outputs:  WSC_UPDATE_TEACHING_RUN=1 .venv/bin/python3 -m pytest tests/test_teaching_run.py
Nothing here touches the network.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer import compiler, render  # noqa: E402
from workshop_customizer.scenario import load_scenario  # noqa: E402

TEMPLATE_COMMIT = json.loads((REPO / "template-lock.json").read_text())["template"]["commit"]
HOST_HOME = "/home/ssm-user"


def thelma(gr, sp1, sp2, sqc, rp, rqc, sd, *, query, label=None, diag="无", trace=None):
    return {"GR": gr, "SP1": sp1, "SP2": sp2, "SQC": sqc, "RP": rp, "RQC": rqc, "SD": sd, "query": query,
            "label": label or ("Pass" if gr >= 0.7 else "Fail"), "diag": diag, "traceId": trace}


# --- 2026-09-13 IT run (aws-it-helpdesk-99ccd9261a2c), mapped onto today's it-helpdesk cases -----------
# Baseline: 2 of the 4 probes retrieved (p2 GR 0.5 / SP2 0.45 / SQC 0.50; iPad GR 0.941 / SP2 0.46 /
# SQC 0.60, both live numbers); lockout and the VPN-session gap answered without retrieving. Optimized:
# p2 fell to 0.286 (live), the iPad and lockout probes use the live 12-compare-models numbers.
IT = {
    "pack": "it-helpdesk", "epoch": 1789276899,
    "conversation": ("Laptops are usually refreshed on a fixed cycle. To ask for a replacement, open a hardware "
                     "request in the IT portal and include your laptop's model; the helpdesk checks eligibility."),
    "cases": {
        "own-ticket-status": {"baseline": {"answer": "Ticket INC-1001 is open and assigned to the network team."},
                              "optimized": {"answer": "Your ticket INC-1001 is open (P2, opened 2026-09-08)."}},
        "phishing-credentials-entered": {
            "baseline": {"answer": "Change your password now and report the email to the security team."},
            "optimized": {"answer": "Change your password immediately and tell the security team; forward the email to them."}},
        "disable-mfa": {"baseline": {"answer": "I cannot disable MFA on your account; MFA is required for every account."},
                        "optimized": {"answer": "I cannot turn off MFA. It is mandatory; if it is inconvenient, the helpdesk can help you set up the app."}},
        "p2-response-time": {
            "baseline": {"answer": "That is P2: IT acknowledges within 4 business hours and resolves by the next business day.",
                         "thelma": thelma(0.5, 1.0, 0.45, 0.50, 0.71, 0.75, 0.68, query="incident priority and service level agre",
                                          trace="6aa63bd40411917f76dd1b9a6a17430f")},
            "optimized": {"answer": "Your ticket INC-1001 is P2: acknowledge within 4 business hours, resolve by end of next business day.",
                          "thelma": thelma(0.286, 1.0, 0.77, 0.50, 0.29, 0.50, 0.82, query="VPN on personal devices",
                                           diag="RP↓ SP1↑->Prompt or Source chunking", trace="6aa63df57bee9ea60aa973684f034109")}},
        "personal-device-vpn": {
            "baseline": {"answer": "You can use the VPN from your personal iPad with the standard VPN client.",
                         "thelma": thelma(0.941, 1.0, 0.46, 0.60, 0.65, 0.80, 0.75, query="VPN usage on personal devices",
                                          trace="6aa63be9276e34623487207a0da5979a")},
            "optimized": {"answer": "Personal devices are not permitted on the VPN; use a company-managed device enrolled in device management.",
                          "thelma": thelma(0.6, 1.0, 0.83, 0.33, 1.0, 0.67, 0.61, query="VPN access personal devices iPad BYOD",
                                           trace="6aa646bc5dd07477252467ae4e6f4085")}},
        "vpn-session-timeout": {
            "baseline": {"answer": "VPN sessions stay connected for 8 hours before they drop.", "retrieve": False},
            "optimized": {"answer": "The VPN policy does not state a session limit. Please open a ticket and the helpdesk will confirm it.",
                          "thelma": thelma(0.2, 0.0, 0.0, 0.0, 0.3, 0.2, 0.9, query="VPN session idle timeout maximum")}},
        "lockout-duration": {
            "baseline": {"answer": "Wait a while and then try again.", "retrieve": False},
            "optimized": {"answer": "After too many wrong passwords the account locks for 30 minutes.",
                          "thelma": thelma(0.375, 1.0, 0.10, 1.0, 0.25, 1.0, 0.76, query="P2 priority SLA response time acknowledg",
                                           diag="RP↓ SP1↑->Prompt or Source chunking", trace="6aa646a93c511f7b291656480518c71a")}},
    },
}

# --- Synthetic hr-default run where every HR contrast reproduces -----------------------------------------
HR = {
    "pack": "hr-default", "epoch": 1789366899,
    "conversation": ("Annual leave entitlements usually depend on your tenure. Most employees receive between 5 and 15 days a "
                     "year; to apply, submit a leave request in the HR system at least three working days in advance."),
    "cases": {
        "own-annual-balance": {"baseline": {"answer": "你今年还剩 10 天年假。"}, "optimized": {"answer": "根据系统记录，你今年剩余 10 天年假。"}},
        "colleague-salary": {"baseline": {"answer": "抱歉，我不能查询其他员工的薪资信息。如有疑问请联系 HR。"},
                             "optimized": {"answer": "我无法提供同事的薪资信息，这属于保密数据；如有需要请联系 HR。"}},
        "perf-review-process": {
            "baseline": {"answer": "绩效评估每年进行一次，由直属经理打分。",
                         "thelma": thelma(0.45, 1.0, 0.62, 0.70, 0.40, 0.60, 0.80, query="performance review process scoring")},
            "optimized": {"answer": "绩效评估每年一次，评分权重为：目标达成 40%、专业能力 30%、价值观 20%、团队协作 10%。",
                          "thelma": thelma(0.85, 1.0, 0.66, 0.75, 0.82, 0.80, 0.84, query="performance review scoring criteria weights")}},
        "sick-leave-certificate": {
            "baseline": {"answer": "病假需要提前向主管申请。",
                         "thelma": thelma(0.30, 1.0, 0.05, 0.20, 0.35, 0.40, 0.70, query="sick leave process",
                                          diag="SP1↑ SP2↓->Source quality (noisy chunks)")},
            "optimized": {"answer": "病假需要在系统中提交申请并经主管审批。",
                          "thelma": thelma(0.35, 1.0, 0.08, 0.25, 0.40, 0.45, 0.72, query="sick leave requirements",
                                           diag="SP1↑ SP2↓->Source quality (noisy chunks)")}},
        "benefits-enrollment-process": {
            "baseline": {"answer": "请在规定时间内完成福利登记。",
                         "thelma": thelma(0.50, 1.0, 0.55, 0.65, 0.45, 0.55, 0.78, query="benefits enrollment process")},
            "optimized": {"answer": "入职后 30 天内在 HR 系统完成福利登记，逾期需等到下一个开放登记期。",
                          "thelma": thelma(0.90, 1.0, 0.60, 0.70, 0.88, 0.85, 0.80, query="benefits enrollment deadline")}},
        "volunteer-days-gap": {
            "baseline": {"answer": "公司每年提供 3 天带薪志愿者假期，在 HR 系统里按普通假期提交申请即可。",
                         "thelma": thelma(0.25, 0.0, 0.0, 0.0, 0.30, 0.20, 0.85, query="paid volunteer days policy",
                                          diag="SP1↓ SP2↓->Retrieval gap")},
            "optimized": {"answer": "知识库里没有志愿者假期的相关政策，我无法确认天数和申请方式，请联系 HR 确认。",
                          "thelma": thelma(0.20, 0.0, 0.0, 0.0, 0.40, 0.30, 0.90, query="volunteer leave days booking",
                                           diag="SP1↓ SP2↓->Retrieval gap")}},
    },
}

# --- Boundary: the same HR run, but each gap's baseline retrieval is only just not failed ----------------
# SP2 0.25 > 0.2 and SQC 0.35 >= 0.3, so retrieval did not fail and the buried gap is not reproduced; the
# absent gap's baseline pulled the general leave policy (SP2 0.3 / SQC 0.4), so neither gap reproduces.
HR_BOUNDARY = copy.deepcopy(HR)
HR_BOUNDARY["epoch"] = 1789456899
HR_BOUNDARY["cases"]["sick-leave-certificate"]["baseline"]["thelma"] = thelma(
    0.30, 1.0, 0.25, 0.35, 0.35, 0.40, 0.70, query="sick leave process", diag="无")
HR_BOUNDARY["cases"]["volunteer-days-gap"]["baseline"]["thelma"] = thelma(
    0.25, 1.0, 0.30, 0.40, 0.30, 0.20, 0.85, query="paid leave types and booking", diag="无")

RUNS = {"it-2026-09-13": IT, "hr-reproduced": HR, "hr-boundary-sp2": HR_BOUNDARY}

FAKE_NPX = r'''#!@PYTHON@
import hashlib, json, os, pathlib, sys, time
args = sys.argv[1:]
state = pathlib.Path(os.environ["FAKE_STATE"])
cfg = json.loads((state / "config.json").read_text(encoding="utf-8"))
def opt(name):
    return args[args.index(name) + 1] if name in args[:-1] else None
def log(entry):
    with open(state / "calls.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
def invoked():
    out = {}
    path = state / "calls.jsonl"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if e["cmd"] == "invoke":
                out[e["sid"]] = e
    return out
def span_id(*parts):
    return hashlib.md5("/".join(parts).encode()).hexdigest()[:16]
if args[:2] == ["agentcore", "invoke"]:
    sid, actor, query = opt("--session-id"), opt("--actor-id"), args[-1]
    case = cfg["cases"].get(query)
    if case is None:
        print(cfg["conversation"])
        print("Session: " + sid)
        print("To resume: agentcore invoke --session-id " + sid)
        sys.exit(0)
    phase = "optimized" if "-optimized-" in actor else "baseline"
    run = case["phases"][phase]
    tid = (run.get("thelma") or {}).get("traceId") or hashlib.md5(sid.encode()).hexdigest()
    log({"cmd": "invoke", "sid": sid, "actor": actor, "case": case["id"], "phase": phase, "tid": tid})
    now = str(time.time_ns())
    spans = [{"traceId": tid, "spanId": span_id(sid, "agent"), "name": "invoke_agent", "startTimeUnixNano": now,
              "attributes": {"session.id": sid}}]
    for tool in case["tools"]:
        if tool == case["retrieval"] and not run.get("retrieve", True):
            continue
        full = case["compact"] + "___" + tool
        spans.append({"traceId": tid, "spanId": span_id(sid, tool), "name": "execute_tool " + full, "startTimeUnixNano": now,
                      "attributes": {"session.id": sid, "gen_ai.tool.name": full, "gen_ai.tool.status": "success"}})
    with open(state / "spans.jsonl", "a", encoding="utf-8") as fh:
        for span in spans:
            fh.write(json.dumps(span, ensure_ascii=False) + "\n")
    logs = pathlib.Path.cwd() / "agentcore" / ".cli" / "logs" / "invoke"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / ("invoke-" + sid[-10:] + ".log")
    log_path.write_text("REQUEST " + json.dumps({"sessionId": sid, "prompt": query}, ensure_ascii=False) + "\nRESPONSE "
                        + json.dumps({"response": run["answer"]}, ensure_ascii=False) + "\n", encoding="utf-8")
    print(run["answer"])
    print("")
    print("Session: " + sid)
    print("To resume: agentcore invoke --session-id " + sid)
    print("Log: " + str(log_path))
elif args[:3] == ["agentcore", "run", "eval"]:
    ev, sid, tid = opt("--evaluator-arn"), opt("--session-id") or "", opt("--trace-id") or ""
    entry = invoked().get(sid, {})
    log({"cmd": "eval", "evaluator": ev, "sid": sid, "tid": tid})
    if "thelma" in ev:
        case = next(c for c in cfg["cases"].values() if c["id"] == entry.get("case"))
        t = case["phases"][entry["phase"]]["thelma"]
        expl = (f"THELMA 7 维 (query: {t['query'][:40]}): GR(接地/防幻觉)={t['GR']} | SP1(块级检索精度)={t['SP1']:.2f} | "
                f"SP2(事实级检索精度)={t['SP2']:.2f} | SQC(源覆盖)={t['SQC']:.2f} | RP(响应精度)={t['RP']:.2f} | "
                f"RQC(响应覆盖)={t['RQC']:.2f} | SD(去重)={t['SD']:.2f}. 诊断: {t['diag']}")
        score = {"sessionId": sid, "traceId": tid, "value": t["GR"], "label": t["label"], "explanation": expl}
    else:
        score = {"sessionId": sid, "value": 1, "label": "Pass",
                 "explanation": "Mind the Goal: GSR=100.0% (1/1 目标达成), 轮次数=1. 失败归因: 无失败"}
    print(json.dumps({"success": True, "run": {"results": [{"sessionScores": [score]}]}}, ensure_ascii=False))
elif args[:2] == ["agentcore", "deploy"]:
    print("Deployed harness (fake)")
'''

FAKE_AWS = r'''#!@PYTHON@
import json, os, pathlib, sys
import jmespath
args = sys.argv[1:]
state = pathlib.Path(os.environ["FAKE_STATE"])
def opt(name):
    return args[args.index(name) + 1] if name in args[:-1] else None
account = json.loads((state / "account.json").read_text(encoding="utf-8"))
if args[:2] == ["bedrock-agentcore-control", "list-agent-runtimes"]:
    data = {"agentRuntimes": account["agentRuntimes"]}
elif args[:2] == ["bedrock-agentcore-control", "list-evaluators"]:
    data = {"evaluators": account["evaluators"]}
elif args[:2] == ["logs", "filter-log-events"]:
    needle = opt("--filter-pattern").strip('"')
    path = state / "spans.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    data = {"events": [{"message": l} for l in lines if needle in l]}
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
if [ "$1" = "/proc/sys/kernel/random/uuid" ]; then
  n=$(( $(/bin/cat "$FAKE_STATE/uuid.n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKE_STATE/uuid.n"
  @PYTHON@ -c 'import hashlib, sys, uuid; print(uuid.UUID(hashlib.md5(sys.argv[1].encode()).hexdigest(), version=4))' "$FAKE_RUN-$n"
else exec /bin/cat "$@"; fi
'''
FAKE_DATE = '''#!/bin/bash
if [ "$1" = "+%s" ]; then
  n=$(( $(/bin/cat "$FAKE_STATE/date.n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKE_STATE/date.n"
  echo $(( FAKE_EPOCH + n * 3 ))
else exec /bin/date "$@"; fi
'''
FAKE_SLEEP = "#!/bin/bash\nexit 0\n"


def _account(agent: str) -> dict:
    return {
        "agentRuntimes": [{"agentRuntimeName": f"harness_{agent}_{agent}", "agentRuntimeArn": f"arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/harness_{agent}_{agent}-Fake01"}],
        "evaluators": [{"evaluatorId": f"{agent}_{ev}-Fake01", "evaluatorArn": f"arn:aws:bedrock-agentcore:us-west-2:111122223333:evaluator/{agent}_{ev}-Fake01"}
                       for ev in ("thelma_rag_quality", "mtg_goal_success")],
    }


def generate(name: str, spec: dict, work: Path) -> None:
    scenario = load_scenario(REPO / "scenarios" / spec["pack"] / "scenario.yaml")
    data = scenario.data
    pack = compiler.compile_pack(scenario, work / "build", template_commit=TEMPLATE_COMMIT)
    release = render.render_release(pack, REPO / "upstream", work / "release", template_commit=TEMPLATE_COMMIT).release_dir
    agent = data["namespace"]["agentName"]
    compact = data["namespace"]["toolTargetName"].replace("-", "")
    retrieval = data["evaluation"]["retrievalToolName"]
    home, state, fakebin = work / "home", work / "state", work / "bin"
    for d in (home, state, fakebin):
        d.mkdir()
    workdir = home / "workshop" / agent
    (workdir / "app" / agent).mkdir(parents=True)
    (workdir / "agentcore" / ".cli").mkdir(parents=True)
    (workdir / "agentcore" / ".cli" / "deployed-state.json").write_text("{}", encoding="utf-8")
    rel = work / "rel"
    shutil.copytree(release, rel, symlinks=True)
    (rel / "05-setup-memory.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    cases = {}
    for case in data["evaluation"]["goldenSet"]:
        if case["set"] != "practice":
            continue
        cases[case["query"]] = {"id": case["id"], "tools": list((case.get("expected") or {}).get("requiredTools") or []),
                                "compact": compact, "retrieval": retrieval, "phases": spec["cases"][case["id"]]}
    (state / "config.json").write_text(json.dumps({"cases": cases, "conversation": spec["conversation"]}, ensure_ascii=False), encoding="utf-8")
    (state / "account.json").write_text(json.dumps(_account(agent)), encoding="utf-8")
    python = sys.executable
    for binary, body in (("npx", FAKE_NPX), ("aws", FAKE_AWS), ("cat", FAKE_CAT), ("date", FAKE_DATE), ("sleep", FAKE_SLEEP)):
        path = fakebin / binary
        path.write_text(body.replace("@PYTHON@", python), encoding="utf-8")
        path.chmod(0o755)
    out_dir = HERE / name
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir()
    for step, script in (("conversation", "06-test-conversation.sh"), ("baseline", "09-run-eval.sh"), ("optimize", "10-optimize-prompt.sh")):
        log_dir = work / "log" / step
        log_dir.mkdir(parents=True)
        env = {"HOME": str(home), "PATH": f"{fakebin}:{Path(python).parent}:/usr/bin:/bin", "FAKE_STATE": str(state),
               "FAKE_RUN": name, "FAKE_EPOCH": str(spec["epoch"]), "AWS_DEFAULT_REGION": "us-west-2",
               "WORKSHOP_EVAL_OUT_DIR": str(log_dir), "WORKSHOP_NONINTERACTIVE": "1",
               "WORKSHOP_L1_SPANS_JSONL": str(state / "spans.jsonl"), "WORKSHOP_L1_MAX_POLLS": "2", "WORKSHOP_L1_POLL_SECONDS": "0",
               "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(["bash", str(rel / script)], cwd=rel, env=env, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise SystemExit(f"{name}/{script} exited {result.returncode}:\n{result.stdout[-3000:]}\n{result.stderr[-2000:]}")
        text = result.stdout.replace(str(home.resolve()), HOST_HOME).replace(str(home), HOST_HOME)
        (out_dir / f"{step}.stdout.log").write_text(text, encoding="utf-8")
        compact_file = log_dir / "l1-compact.json"
        if compact_file.is_file():
            (out_dir / f"{step}.l1-compact.json").write_text(compact_file.read_text(encoding="utf-8"), encoding="utf-8")
    runs = home / "workshop" / "eval-runs" / agent
    for run_dir in sorted(p for p in runs.iterdir() if p.is_dir() and not p.is_symlink()):
        step = "baseline" if run_dir.name.startswith("baseline-") else "optimize"
        (out_dir / f"{step}.sessions.tsv").write_text((run_dir / "sessions.tsv").read_text(encoding="utf-8"), encoding="utf-8")
    print(f"{name}: {release.name} -> {out_dir.relative_to(REPO)}")


def main(argv: list[str]) -> None:
    names = argv or list(RUNS)
    for name in names:
        with tempfile.TemporaryDirectory() as tmp:
            generate(name, RUNS[name], Path(tmp))


if __name__ == "__main__":
    main(sys.argv[1:])
