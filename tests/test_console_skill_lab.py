"""engine console.skills_lab: SKILL.md checks, task sets, the with/without comparison, the training loop (no AWS) — on a
Harness and on a Claude Agent SDK runtime, whose answers come with the files the turn made and are judged from them; task
input files (uploaded once by content, checked like Launchpad's task assets, given to a runtime's call, refused for a
Harness) and a judge that sees the pictures."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer.console import claude_sdk  # noqa: E402
from workshop_customizer.console import skills_lab as sl  # noqa: E402
from workshop_customizer.console.auth import AuthError  # noqa: E402
from workshop_customizer.console.jobs import Jobs  # noqa: E402
from workshop_customizer.console.store import Store  # noqa: E402

SEED = "---\nname: reply-style\ndescription: How to reply to a member. Use for every member question.\n---\n# Reply style\n\n1. Answer first.\n"
SIGNED = SEED + "2. End with the signature line.\n"
# a Claude Agent SDK runtime deployed from the template, and a skill whose output is a file
POINTS = "points-report-xlsx"
POINTS_SEED = "---\nname: points-report-xlsx\ndescription: 积分报表 Excel。要积分报表时使用。\n---\n# 积分报表\n\n1. 文件名 points-report.xlsx。\n"
POINTS_FULL = POINTS_SEED + "2. 表头：会员号、本期积分、到期积分。\n"
NAME = "adlc_probe_artifacts_ab12cd"
RID = f"{NAME}-Zx9Yw8Vu7T"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/{RID}"
ROLE = f"adlc-probe-artifacts-rt-{NAME}"
BUCKET = "adlc-console-111122223333-us-west-2"
# a skill that draws a chart from the member file each task gives (the agent's input), judged from the picture
CHART = "points-chart-png"
CHART_SEED = "---\nname: points-chart-png\ndescription: 积分柱状图。要画积分图时使用。\n---\n# 积分图\n\n1. 文件名 points-chart.png。\n"
CHART_FULL = CHART_SEED + "2. 横向柱状图，最高的橙色。\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24  # an image as far as the console looks (its signature)
MEMBERS = "会员号,本期积分\nM1,10\nM2,400\n".encode("utf-8")
EXTRA = "会员号,本期积分\nM1,5\n".encode("utf-8")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_skill_md_needs_frontmatter_whose_name_matches_and_a_description():
    assert sl.check_skill("reply-style", SEED)["description"].startswith("How to reply")
    folded = "---\nname: reply-style\ndescription: >-\n  Folded over\n  two lines.\n---\nBody\n"
    assert sl.check_skill("reply-style", folded)["description"] == "Folded over two lines."
    for name, text, match in (("reply-style", "# no frontmatter\n", "frontmatter"), ("other", SEED, "must be other"),
                              ("reply-style", "---\nname: reply-style\n---\nBody\n", "description"), ("Bad_Name", SEED, "lowercase"),
                              ("reply-style", "---\nname: reply-style\ndescription: d\n---\n\n", "no instructions")):
        with pytest.raises(sl.SkillLabError, match=match):
            sl.check_skill(name, text)


def test_task_sets_validate_and_a_single_list_splits_4_3_3_the_same_way_every_time(tmp_path):
    store = Store(tmp_path)
    tasks = [{"id": f"t{i}", "question": f"q{i}", "rubric": "r"} for i in range(10)]
    ts = sl.put_taskset(store, "dev", {"name": "one list", "tasks": tasks})
    parts = {k: [t["id"] for t in sl.split(ts, k)] for k in sl.SPLITS}
    assert [len(parts[k]) for k in sl.SPLITS] == [4, 3, 3] and sorted(sum(parts.values(), [])) == sorted(t["id"] for t in tasks)
    assert parts == {k: [t["id"] for t in sl.split(sl.taskset(store, "dev", ts["id"]), k)] for k in sl.SPLITS}
    assert sl.tasksets(store, "dev")[0]["counts"] == {"tasks": 10} and sl.tasksets(store, "prod") == []
    with pytest.raises(sl.SkillLabError, match="unique"):
        sl.check_taskset({"name": "x", "train": tasks[:2], "val": tasks[1:3]})
    with pytest.raises(sl.SkillLabError, match="rubric"):
        sl.check_taskset({"name": "x", "tasks": [{"id": "a", "question": "q"}]})
    with pytest.raises(sl.SkillLabError, match="val"):
        sl.check_taskset({"name": "x", "mode": "split", "train": tasks[:2]})


def test_the_sign_test_and_the_verdicts():
    assert sl.sign_test(5, 0) == 0.0625 and sl.sign_test(3, 1) == 0.625 and sl.sign_test(0, 0) is None

    def res(passes, score, loaded=True):
        return {"pass": passes, "score": score, "skillLoaded": loaded}

    helps = sl.compare({f"t{i}": res(True, 1.0) for i in range(6)}, {f"t{i}": res(False, 0.4, False) for i in range(6)})
    assert helps["wins"] == [f"t{i}" for i in range(6)] and helps["p"] == 0.0312 and sl.verdict(helps, "m")["key"] == "helps"
    partial = sl.compare({f"t{i}": res(False, 0.75) for i in range(3)}, {f"t{i}": res(False, 0.4, False) for i in range(3)})
    assert partial["ties"] == 3 and len(partial["softWins"]) == 3 and sl.verdict(partial, "m")["key"] == "partial"  # live 2026-10-01: +0.30, no pass changed
    not_loaded = sl.compare({"a": res(False, 0.4, False)}, {"a": res(False, 0.4, False)})
    assert sl.verdict(not_loaded, "us.amazon.nova-2-lite-v1:0")["key"] == "not_loaded"
    hurts = sl.compare({"a": res(False, 0.2), "b": res(False, 0.3)}, {"a": res(True, 1.0, False), "b": res(True, 1.0, False)})
    assert sl.verdict(hurts, "m")["key"] == "hurts"
    invalid = sl.compare({"a": {**res(True, 1.0), "invalid": True}}, {"a": res(False, 0.0, False)})
    assert invalid["with"]["n"] == 0 and invalid["with"]["invalid"] == 1 and invalid["wins"] == []


def test_the_edit_budget_decays_from_its_start_to_two():
    budgets = [sl.edit_budget(step, 5, 8) for step in range(1, 6)]
    assert budgets[0] == 8 and budgets[-1] == 2 and budgets == sorted(budgets, reverse=True)
    assert sl.edit_budget(1, 1, 4) == 4


class Runtime:
    """bedrock-runtime: the judge passes an answer that ends with the signature — or, for an agent that works on files,
    one whose points-report.xlsx has the right header; the optimizer adds the signature rule."""

    def __init__(self, optimizer_reply: str | None = None):
        self.optimizer_reply = optimizer_reply
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.contents: list[list[dict]] = []

    def converse(self, modelId, system, messages, inferenceConfig):
        user = messages[0]["content"][0]["text"]
        self.prompts.append(user)
        self.systems.append(system[0]["text"])
        self.contents.append(messages[0]["content"])
        images = [b["image"] for b in messages[0]["content"] if "image" in b]
        if "You improve an Agent Skill" in system[0]["text"]:
            text = self.optimizer_reply if self.optimizer_reply is not None else (
                f"<rationale>The answers lacked the signature.</rationale>\n<edits>\n- added the signature rule\n</edits>\n<skill>\n{SIGNED}</skill>")
        elif "Files the agent created or changed" in user and ".png" in user:  # a chart: judged from the picture it is shown
            files = user.split("Files the agent created or changed", 1)[1]
            ok = "--- points-chart.png" in files and "attached below as Image 1" in files and len(images) == 1 and images[0]["source"]["bytes"].startswith(PNG[:8])
            text = json.dumps({"pass": ok, "score": 1.0 if ok else 0.3 if images else 0.1, "reason": "the chart" if ok else "not the chart"})
        elif "Files the agent created or changed" in user:
            files = user.split("Files the agent created or changed", 1)[1]
            ok = "points-report.xlsx" in files and "会员号 | 本期积分 | 到期积分" in files
            score = 1.0 if ok else 0.5 if "points-report.xlsx" in files else 0.2
            text = json.dumps({"pass": ok, "score": score, "reason": "the file" if ok else "the file is not the report"})
        else:
            answer = user.split("Answer:\n", 1)[1]
            signed = answer.rstrip().endswith("— reply bot")
            text = "```json\n" + json.dumps({"pass": signed, "score": 1.0 if signed else 0.5, "reason": "signed" if signed else "no signature"}) + "\n```"
        return {"output": {"message": {"content": [{"text": text}]}}}


class S3:
    def __init__(self):  # the agent's own other skill is already there
        self.objects: dict[tuple[str, str], bytes] = {("other", "skills/house-rules/SKILL.md"): b"---\nname: house-rules\ndescription: d\n---\nBe polite.\n"}
        self.puts: list[str] = []
        self.deleted: list[tuple[str, str]] = []

    def put_object(self, Bucket, Key, Body, ContentType=None, IfNoneMatch=None, **kw):
        if IfNoneMatch == "*" and (Bucket, Key) in self.objects:
            raise RuntimeError("An error occurred (PreconditionFailed) when calling the PutObject operation (412)")
        self.objects[(Bucket, Key)] = Body
        self.puts.append(Key)

    def get_paginator(self, name):
        assert name == "list_object_versions"
        return type("P", (), {"paginate": lambda _s, **kw: [self.list_object_versions(**kw)]})()

    def get_object(self, Bucket, Key):
        return {"Body": type("B", (), {"read": lambda _s, b=self.objects[(Bucket, Key)]: b})()}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise RuntimeError("An error occurred (404) when calling the HeadObject operation: Not Found")
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def list_object_versions(self, Bucket, Prefix, **kw):  # the bucket is versioned: two versions of each object
        found = [k for b, k in self.objects if b == Bucket and k.startswith(Prefix)]
        return {"Versions": [{"Key": k, "VersionId": v} for k in found for v in ("v1", "v2")], "DeleteMarkers": [], "IsTruncated": False}

    def delete_objects(self, Bucket, Delete, **kw):
        for o in Delete["Objects"]:
            self.objects.pop((Bucket, o["Key"]), None)
            self.deleted.append((o["Key"], o.get("VersionId")))
        return {}

    def head_bucket(self, Bucket, **kw):
        return {}


class Data:
    """bedrock-agentcore: an agent that signs its answer only when a skill it loads says so (read from the fake S3)."""

    def __init__(self, s3: S3, fail_first: int = 0):
        self.s3, self.fail_first, self.calls = s3, fail_first, []

    def invoke_harness(self, harnessArn, runtimeSessionId, actorId, messages, skills=None, model=None, **kw):
        self.calls.append({"actor": actorId, "skills": skills, "session": runtimeSessionId})
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("RuntimeClientError: Received error (502) from runtime")
        texts = []
        for s in skills or []:
            bucket, key = s["s3"]["uri"][5:].split("/", 1)
            texts.append(self.s3.objects[(bucket, key + "SKILL.md")].decode())
        events = []
        if texts:
            events += [{"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"name": "skills"}}}},
                       {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": '{"skill_name": "reply-st'}}}},
                       {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": 'yle"}'}}}},
                       {"contentBlockStop": {"contentBlockIndex": 0}}]
        answer = "500 points." + ("\n— reply bot" if any("signature" in t for t in texts) else "")
        events += [{"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"text": answer}}}, {"messageStop": {"stopReason": "end_turn"}},
                   {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5}}}]
        return {"stream": events}


class RuntimeData(Data):
    """bedrock-agentcore with a Claude Agent SDK runtime of the template: a call's skills are what its payload names
    (an override read from the fake S3); the agent writes points-report.xlsx with the header its skill states, and a
    report.csv when it has no skill. ``legacy``: an image from before calls could bring their own skills."""

    def __init__(self, s3: S3):
        super().__init__(s3)
        self.payloads: list[dict] = []
        self.stopped: list[str] = []
        self.legacy = False
        self.no_inputs = False  # an image from before a call could bring its input files
        self.lock = threading.Lock()

    def invoke_agent_runtime(self, agentRuntimeArn, runtimeSessionId, payload, **kw):
        body = json.loads(payload)
        with self.lock:
            self.payloads.append({**body, "session": runtimeSessionId, "arn": agentRuntimeArn})
        texts = []
        for o in body.get("skillOverrides") or []:
            bucket, key = o["uri"][5:].split("/", 1)
            texts.append(self.s3.objects[(bucket, key + "SKILL.md")].decode())
        if body.get("files") is not None:  # a chart drawn from the task's files: a good one with the skill, and the arm without it rewrites an input
            return self._chart(body, texts)
        tools = [{"tool": "Skill", "input": {"skill": POINTS}, "ok": True, "skill": POINTS}] if texts else []
        if texts:
            header = "会员号 | 本期积分 | 到期积分" if any("表头" in t for t in texts) else "会员 | 积分"
            made = [{"path": "points-report.xlsx", "bytes": 5096, "type": "xlsx", "status": "created", "text": f"[工作表 1：积分明细] 2 行 × 3 列\n  1 | {header}",
                     "table": {"sheets": [{"name": "积分明细", "rows": [header.split(" | ")], "nrows": 2, "ncols": 3, "truncated": False}]}, "sheets": 1,
                     "base64": "UEsDBA==", "sha256": "ab", "secret": "never kept"}]
        else:
            made = [{"path": "report.csv", "bytes": 20, "type": "csv", "status": "created", "text": "会员,积分\nM1,800\n"}]
        answer = {"result": "已生成报表。", "tools": tools, "skills": [t["skill"] for t in tools], "skillsLoaded": [t["skill"] for t in tools], "artifacts": made,
                  "seconds": 9.5, "turns": 4, "costUsd": 0.021, "format": "adlc-claude-agent/2"}
        if not self.legacy:
            answer["call"] = {"id": "c1", "workdir": "calls/c1", "skills": []}
        return {"response": io.BytesIO(json.dumps(answer, ensure_ascii=False).encode()), "statusCode": 200, "contentType": "application/json"}

    def _chart(self, body: dict, texts: list[str]) -> dict:
        inputs = []
        for f in body["files"]:
            bucket, key = f["uri"][5:].split("/", 1)
            data = self.s3.objects[(bucket, key)]
            assert sha(data) == f["sha256"] and len(data) == f["bytes"]  # what the runtime checks before the prompt
            inputs.append({"path": f["path"], "bytes": len(data), "sha256": f["sha256"]})
        picture = {"format": "png", "width": 800, "height": 450, "preview": {"format": "png", "width": 800, "height": 450, "bytes": len(PNG), "base64": b64(PNG)},
                   "thumb": {"format": "png", "width": 320, "height": 180, "bytes": 30, "base64": b64(PNG[:30])}}
        if texts and any("横向" in t for t in texts):
            made = [{"path": "points-chart.png", "bytes": 9759, "type": "image", "status": "created", "text": "", "image": picture}]
        else:
            made = [{"path": "chart.png", "bytes": 9000, "type": "image", "status": "created", "text": "", "image": picture},
                    {"path": "members.csv", "bytes": 12, "type": "csv", "status": "changed", "text": "会员号,积分\n"}]
        tools = [{"tool": "Skill", "input": {"skill": CHART}, "ok": True, "skill": CHART}] if texts else []
        answer = {"result": "已生成图。", "tools": tools, "skills": [t["skill"] for t in tools], "skillsLoaded": [t["skill"] for t in tools], "artifacts": made,
                  "seconds": 7.5, "turns": 4, "costUsd": 0.02, "format": "adlc-claude-agent/3",
                  "call": {"id": "c2", "workdir": "calls/c2", "skills": [], **({} if self.no_inputs else {"inputs": inputs})}}
        return {"response": io.BytesIO(json.dumps(answer, ensure_ascii=False).encode()), "statusCode": 200, "contentType": "application/json"}

    def stop_runtime_session(self, agentRuntimeArn, runtimeSessionId, **kw):
        with self.lock:
            self.stopped.append(runtimeSessionId)
        return {"statusCode": 200}


class Ctl:
    def __init__(self):
        self.harness = {"harnessId": "h-1", "harnessName": "member_bot", "arn": "arn:h-1", "status": "READY", "executionRoleArn": "arn:aws:iam::1:role/bot-role",
                        "model": {"bedrockModelConfig": {"modelId": "us.anthropic.claude-haiku-4-5-20251001-v1:0"}},
                        "skills": [{"s3": {"uri": "s3://other/skills/house-rules/"}}]}
        self.updates: list[dict] = []

    def get_harness(self, harnessId):
        self.reads = getattr(self, "reads", 0) + 1
        return {"harness": json.loads(json.dumps(self.harness))}

    def list_tags_for_resource(self, resourceArn):
        return {"tags": {"adlc:console": "1"}}

    def update_harness(self, harnessId, **changes):
        self.updates.append(changes)
        self.harness.update(changes)
        return {"harness": {"status": "UPDATING"}}

    def get_agent_runtime(self, agentRuntimeId):
        if agentRuntimeId != RID:
            raise RuntimeError(f"ResourceNotFoundException: {agentRuntimeId}")
        return {"agentRuntimeId": RID, "agentRuntimeName": NAME, "agentRuntimeArn": RUNTIME_ARN, "status": "READY", "agentRuntimeVersion": "1",
                "roleArn": f"arn:aws:iam::111122223333:role/{ROLE}"}

    def list_harnesses(self, **kw):
        return {"harnesses": [{"harnessId": "h-1", "harnessName": "member_bot", "arn": "arn:h-1", "status": "READY"}]}

    def list_agent_runtimes(self, **kw):
        return {"agentRuntimes": [{"agentRuntimeId": RID, "agentRuntimeName": NAME, "agentRuntimeArn": RUNTIME_ARN, "status": "READY"}]}


class Iam:
    def __init__(self):
        self.policies: dict = {}

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.policies[(RoleName, PolicyName)] = json.loads(PolicyDocument)

    def delete_role_policy(self, RoleName, PolicyName):
        self.policies.pop((RoleName, PolicyName))

    def get_role_policy(self, RoleName, PolicyName):
        if (RoleName, PolicyName) not in self.policies:
            raise RuntimeError(f"NoSuchEntity: {RoleName}/{PolicyName}")
        return {"PolicyDocument": self.policies[(RoleName, PolicyName)]}


class Console:
    def __init__(self, root: Path, **runtime):
        self.store, self.jobs, self.workshop_data = Store(root / "store"), Jobs(root / "jobs"), root / "workshop"
        self.s3, self.ctl, self.iam, self.rt = S3(), Ctl(), Iam(), Runtime(**runtime)
        self.data = RuntimeData(self.s3)
        clients = {"s3": self.s3, "bedrock-agentcore-control": self.ctl, "iam": self.iam, "bedrock-runtime": self.rt}
        session = type("S", (), {"client": lambda _s, name, region_name=None, **kw: self.data if name == "bedrock-agentcore" else clients[name]})()
        ws = {"id": "dev", "accountId": "111122223333", "region": "us-west-2"}
        self.workspaces = type("W", (), {"get": lambda _s, wid: ws, "verify": lambda _s, wid: {}, "session": lambda _s, wid: session})()


def wait(console: Console, job_id: str) -> dict:
    for _ in range(300):
        job = console.jobs.get(job_id)
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("job still running")


def tasks(prefix: str, n: int) -> list[dict]:
    return [{"id": f"{prefix}{i}", "question": f"How many points, {i}?", "rubric": "Says 500 points.\nEnds with the signature."} for i in range(n)]


def test_an_evaluation_answers_every_task_with_and_without_the_skill(tmp_path):
    console = Console(tmp_path)
    saved = sl.save_version(console, "dev", "reply-style", SIGNED, source="edit")
    assert saved == {"name": "reply-style", "version": "v0001", "created": True, "uri": "s3://adlc-console-111122223333-us-west-2/skills/reply-style/v0001/"}
    assert sl.save_version(console, "dev", "reply-style", SIGNED, source="edit")["created"] is False  # same text, same version
    ts = sl.put_taskset(console.store, "dev", {"name": "set", "train": tasks("a", 2), "val": tasks("b", 2), "test": tasks("c", 6)})
    job = wait(console, sl.start_evaluation(console, "dev", {"harnessId": "h-1", "skill": "reply-style", "tasksetId": ts["id"]})["job"]["id"])
    assert job["status"] == "succeeded", job.get("error")
    result = job["result"]
    assert result["arms"]["with"] == {"n": 6, "invalid": 0, "passRate": 1.0, "softMean": 1.0, "triggerRate": 1.0, "errors": 0}
    assert result["arms"]["without"]["passRate"] == 0.0 and result["comparison"]["p"] == 0.0312 and result["verdict"]["key"] == "helps"
    # the arms differ only in the skill: the agent's own other skills stay in both; every answer has its own actor and session
    with_skills = [c["skills"] for c in console.data.calls if len(c["skills"]) == 2]
    without_skills = [c["skills"] for c in console.data.calls if len(c["skills"]) == 1]
    assert len(with_skills) == len(without_skills) == 6 and without_skills[0] == [{"s3": {"uri": "s3://other/skills/house-rules/"}}]
    assert len({c["actor"] for c in console.data.calls}) == 12 and len({c["session"] for c in console.data.calls}) == 12
    assert ("bot-role", sl.SKILLS_POLICY) in console.iam.policies and console.ctl.updates == []  # the agent itself is not changed


def test_a_platform_failure_is_retried_and_one_that_persists_is_not_counted(tmp_path):
    console = Console(tmp_path)
    sl.save_version(console, "dev", "reply-style", SIGNED, source="edit")
    task = tasks("t", 1)[0]
    harness = {"kind": "harness", "arn": "arn:h-1", "name": "member_bot"}
    console.data.fail_first = 2
    ok = sl.rollout(None, "us-west-2", console.data, harness, task, [{"s3": {"uri": "s3://adlc-console-111122223333-us-west-2/skills/reply-style/v0001/"}}],
                    "reply-style", None)
    assert ok["attempts"] == 3 and ok["skillLoaded"] and ok["text"].endswith("— reply bot")
    console.data.fail_first = 3
    lost = sl._grade(console.rt, sl.JUDGE_MODEL, task, sl.rollout(None, "us-west-2", console.data, harness, task, [], "reply-style", None))
    assert lost["invalid"] and lost["infra"] and "not graded" in lost["reason"]


def test_training_keeps_a_rewrite_only_when_val_improves_and_publishes_the_best(tmp_path):
    console = Console(tmp_path)
    sl.save_version(console, "dev", "reply-style", SEED, source="edit")
    ts = sl.put_taskset(console.store, "dev", {"name": "set", "train": tasks("a", 4), "val": tasks("b", 3), "test": tasks("c", 4)})
    job = wait(console, sl.start_training(console, "dev", {"harnessId": "h-1", "skill": "reply-style", "tasksetId": ts["id"], "batchSize": 4})["job"]["id"])
    assert job["status"] == "succeeded", job.get("error")
    result = job["result"]
    seed, step = result["history"]
    assert (seed["kind"], seed["valScore"]) == ("seed", 0.5) and (step["kind"], step["valScore"], step["accepted"], step["best"]) == ("candidate", 1.0, True, True)
    assert step["edits"] == ["added the signature rule"] and step["uri"].endswith(f"/skills/reply-style/candidates/{job['id']}/s001/")
    assert result["final"]["best"]["passRate"] == 1.0 and result["final"]["seed"]["passRate"] == 0.0 and result["improved"] is True
    assert "+2. End with the signature line." in result["diff"]
    published = sl.publish(console, "dev", job["id"], {"harnessId": "h-1"})
    assert published["saved"]["version"] == "v0002" and sl.library(console, "dev")["reply-style"]["current"] == "v0002"
    assert console.ctl.harness["skills"] == [{"s3": {"uri": "s3://other/skills/house-rules/"}},
                                             {"s3": {"uri": "s3://adlc-console-111122223333-us-west-2/skills/reply-style/v0002/"}}]
    with pytest.raises(sl.SkillLabError, match="take it off"):
        sl.delete_skill(console, "dev", "reply-style")


def test_an_optimizer_reply_that_breaks_the_skill_is_skipped(tmp_path):
    console = Console(tmp_path, optimizer_reply="<skill>\n---\nname: renamed\ndescription: d\n---\nBody\n</skill>")
    sl.save_version(console, "dev", "reply-style", SEED, source="edit")
    ts = sl.put_taskset(console.store, "dev", {"name": "set", "train": tasks("a", 2), "val": tasks("b", 2)})
    job = wait(console, sl.start_training(console, "dev", {"harnessId": "h-1", "skill": "reply-style", "tasksetId": ts["id"]})["job"]["id"])
    step = job["result"]["history"][1]
    assert step["kind"] == "skip" and "name must be reply-style" in step["note"] and job["result"]["best"]["step"] == 0
    assert job["result"]["final"]["split"] == "val"  # no test split: the seed and no skill are compared on val
    with pytest.raises(sl.SkillLabError, match="kept the seed"):
        sl.publish(console, "dev", job["id"], {})


def test_repeated_answers_merge_into_one_result_per_task():
    runs = [{"pass": True, "score": 1.0, "skillLoaded": True, "text": "a"}, {"pass": False, "score": 0.5, "skillLoaded": False, "text": "b"},
            {"pass": True, "score": 0.9, "skillLoaded": True, "text": "c"}]
    merged = sl.merge_runs(runs)
    assert (merged["pass"], merged["passShare"], merged["score"], merged["loadedShare"], merged["text"]) == (True, 0.667, 0.8, 0.667, "a")
    assert sl.merge_runs(runs[:2])["pass"] is False  # a tie is not a majority
    assert sl.summarize({"t": merged}) == {"n": 1, "invalid": 0, "passRate": 0.667, "softMean": 0.8, "triggerRate": 0.667, "errors": 0}
    lost = sl.merge_runs([{"pass": False, "score": 0.0, "invalid": True}, {"pass": True, "score": 1.0}])
    assert lost["pass"] is True and lost["passShare"] == 1.0  # an ungraded answer does not count either way


def test_a_candidate_must_beat_the_current_skill_re_measured_alongside_it_by_the_margin(tmp_path):
    console = Console(tmp_path)
    sl.save_version(console, "dev", "reply-style", SEED, source="edit")
    ts = sl.put_taskset(console.store, "dev", {"name": "set", "train": tasks("a", 2), "val": tasks("b", 2), "test": tasks("c", 2)})
    body = {"harnessId": "h-1", "skill": "reply-style", "tasksetId": ts["id"], "repeats": 2}
    job = wait(console, sl.start_training(console, "dev", {**body, "gateMargin": 0.6})["job"]["id"])  # +0.5 is not enough
    step = job["result"]["history"][1]
    assert (step["valScore"], step["currentScore"], step["delta"], step["accepted"]) == (1.0, 0.5, 0.5, False) and job["result"]["best"]["step"] == 0
    assert job["params"]["repeats"] == 2 and len([c for c in console.data.calls if c["skills"] and len(c["skills"]) == 2]) > 0
    job = wait(console, sl.start_training(console, "dev", body)["job"]["id"])
    assert job["result"]["history"][1]["accepted"] is True and job["result"]["improved"] is True


def test_taking_the_last_library_skill_off_an_agent_removes_its_read_grant(tmp_path):
    console = Console(tmp_path)
    sl.save_version(console, "dev", "reply-style", SIGNED, source="edit")
    sl.apply_to_agent(console, "dev", "reply-style", {"harnessId": "h-1"})
    assert ("bot-role", sl.SKILLS_POLICY) in console.iam.policies
    sl.remove_from_agent(console, "dev", "reply-style", {"harnessId": "h-1"})
    assert console.ctl.harness["skills"] == [{"s3": {"uri": "s3://other/skills/house-rules/"}}]  # the agent's own skill stays
    assert ("bot-role", sl.SKILLS_POLICY) not in console.iam.policies and sl.library(console, "dev")["reply-style"]["agents"] == {}


# -- a Claude Agent SDK runtime: answers that are files ------------------------------------------------------------------

def seed_claude_agent(console: Console, *, template: int | None = 2, pinned: str | None = None, ok: bool = True, skill: str = POINTS) -> None:
    """claude_sdk's record of an agent deployed from the template to RID (its deploy job finished), as deploy_agent leaves it."""
    job = console.jobs.start("deploy", "dev", {"name": NAME}, lambda ctx: ({"runtimeId": RID, "version": "1"} if ok else (_ for _ in ()).throw(RuntimeError("x"))))
    wait(console, job["id"])
    spec = claude_sdk.check_spec({"name": NAME, "systemPrompt": "你是积分运营助手。", "tools": ["Read", "Write", "Bash"],
                                  "skills": [f"{skill}@{pinned}" if pinned else skill], "sample": "你好"})
    entry = {"jobId": job["id"], "mode": "create", "runtimeId": None, "at": "t", "skills": {skill: "v0001"}, "model": spec["model"]}
    if template:
        entry["template"] = template
    rec = {"name": NAME, "workspace": "dev", "spec": spec, "createdAt": "t", "updatedAt": "t", "deployments": [entry]}
    console.store.update(claude_sdk.COLLECTION, {}, lambda all_: {**all_, f"dev:{NAME}": rec})


def stopped(console: Console, n: int) -> list[str]:
    for _ in range(200):  # the rollouts stop their sessions in the background
        if len(console.data.stopped) >= n:
            break
        time.sleep(0.01)
    return console.data.stopped


def test_an_evaluation_on_a_claude_sdk_runtime_is_judged_from_the_files_its_answers_made(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "GRANT_WAIT", 0.0)
    console = Console(tmp_path)
    seed_claude_agent(console)
    saved = sl.save_version(console, "dev", POINTS, POINTS_FULL, source="edit")
    ts = sl.put_taskset(console.store, "dev", {"name": "files", "train": tasks("a", 2), "val": tasks("b", 2), "test": tasks("c", 5)})
    job = wait(console, sl.start_evaluation(console, "dev", {"runtimeId": RID, "skill": POINTS, "tasksetId": ts["id"]})["job"]["id"])
    assert job["status"] == "succeeded", job.get("error")
    result, params = job["result"], job["params"]
    assert (params["targetKind"], params["runtimeId"], params["runtime"], params["model"]) == ("runtime", RID, NAME, "us.anthropic.claude-sonnet-5-5")
    assert result["arms"]["with"] == {"n": 5, "invalid": 0, "passRate": 1.0, "softMean": 1.0, "triggerRate": 1.0, "errors": 0}
    assert result["arms"]["without"]["passRate"] == 0.0 and result["comparison"]["wins"] == [f"c{i}" for i in range(5)] and result["verdict"]["key"] == "helps"
    with_calls = [p for p in console.data.payloads if p["skillOverrides"]]
    without_calls = [p for p in console.data.payloads if not p["skillOverrides"]]
    assert len(with_calls) == len(without_calls) == 5 and all(p["arn"] == RUNTIME_ARN and p["format"] == "json" for p in console.data.payloads)
    assert with_calls[0]["skills"] == [POINTS] and with_calls[0]["skillOverrides"] == [{"name": POINTS, "uri": saved["uri"]}]
    assert without_calls[0]["skills"] == []  # the agent's only skill: the arm without it has none at all
    assert len({p["session"] for p in console.data.payloads}) == 10 and len(set(stopped(console, 10))) == 10  # a session each, each stopped
    row = result["rows"][0]
    assert [a["path"] for a in row["with"]["artifacts"]] == ["points-report.xlsx"] and [a["path"] for a in row["without"]["artifacts"]] == ["report.csv"]
    assert row["with"]["artifacts"][0]["base64"] == "UEsDBA==" and "secret" not in row["with"]["artifacts"][0]  # only what a result keeps
    assert (row["with"]["costUsd"], row["with"]["workdir"]) == (0.021, "calls/c1")
    judged = [i for i, p in enumerate(console.rt.prompts) if "Files the agent created or changed" in p]
    assert len(judged) == 10 and all(sl.JUDGE_FILES in console.rt.systems[i] for i in judged)  # the judge read the files and was told how
    assert "--- points-report.xlsx · xlsx · 5096 bytes · created ---\n[工作表 1：积分明细]" in console.rt.prompts[judged[0]] or any(
        "--- points-report.xlsx · xlsx · 5096 bytes · created ---" in console.rt.prompts[i] for i in judged)
    assert console.iam.policies[(ROLE, sl.SKILLS_POLICY)] == sl.runtime_read_policy(BUCKET)  # its role reads the library (and the task files)
    assert sl.runtime_read_policy(BUCKET)["Statement"][0]["Resource"] == [f"arn:aws:s3:::{BUCKET}/skills/*", f"arn:aws:s3:::{BUCKET}/skill-lab/assets/*"]
    assert "files" not in console.data.payloads[0]  # these tasks bring no files: the call is what it was
    assert console.ctl.updates == [] and console.data.calls == []  # no Harness was touched or invoked


def test_training_on_a_runtime_sends_each_candidate_as_an_override_and_publishing_rebuilds_it(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "GRANT_WAIT", 0.0)
    console = Console(tmp_path, optimizer_reply=f"<rationale>the header</rationale>\n<edits>\n- the header row\n</edits>\n<skill>\n{POINTS_FULL}</skill>")
    seed_claude_agent(console)
    sl.save_version(console, "dev", POINTS, POINTS_SEED, source="edit")
    ts = sl.put_taskset(console.store, "dev", {"name": "files", "train": tasks("a", 4), "val": tasks("b", 2), "test": tasks("c", 3)})
    job = wait(console, sl.start_training(console, "dev", {"runtimeId": RID, "skill": POINTS, "tasksetId": ts["id"], "batchSize": 4})["job"]["id"])
    assert job["status"] == "succeeded", job.get("error")
    seed, step = job["result"]["history"]
    assert (seed["valScore"], step["valScore"], step["accepted"]) == (0.5, 1.0, True) and job["result"]["improved"] is True
    assert step["uri"].endswith(f"/skills/{POINTS}/candidates/{job['id']}/s001/")
    assert any(p["skillOverrides"] == [{"name": POINTS, "uri": step["uri"]}] for p in console.data.payloads)  # the candidate, read for the call
    optimizer = next(p for p in console.rt.prompts if "## Current SKILL.md" in p)
    assert "Files it produced: points-report.xlsx" in optimizer and "1 | 会员 | 积分" in optimizer  # the failures' files, as the judge read them
    final = job["result"]["final"]
    assert (final["best"]["passRate"], final["seed"]["passRate"], final["without"]["passRate"]) == (1.0, 0.0, 0.0)
    deployed: list = []
    monkeypatch.setattr(claude_sdk, "deploy_agent", lambda c, w, body, caller: deployed.append((body, caller)) or {"id": "deploy-0123456789"})
    member = {"username": "ann", "role": "member", "workspaces": ["dev"]}
    with pytest.raises(AuthError, match="admin"):
        sl.publish(console, "dev", job["id"], {"runtimeId": RID}, member)
    assert sl.library(console, "dev")[POINTS]["current"] == "v0001" and not deployed  # a member half-publishes nothing
    with pytest.raises(sl.SkillLabError, match="not a runtime deployed from the Claude Agent SDK template"):
        sl.publish(console, "dev", job["id"], {"runtimeId": "other_agent-AbCdEf1234"}, None)
    admin = {"username": "admin", "role": "admin", "workspaces": ["*"]}
    out = sl.publish(console, "dev", job["id"], {"runtimeId": RID}, admin)
    assert out["saved"]["version"] == "v0002" and out["redeploy"] == {"jobId": "deploy-0123456789", "runtimeId": RID, "runtime": NAME, "skill": POINTS,
                                                                      "version": "v0002", "pinned": False}
    [(body, caller)] = deployed
    assert body["runtimeId"] == RID and body["name"] == NAME and body["skills"] == [{"name": POINTS, "version": None}] and caller == admin
    assert body["tools"] == ["Read", "Write", "Bash"] and body["systemPrompt"] == "你是积分运营助手。"  # the deployed spec, as it was


def test_a_pinned_skill_moves_to_the_published_version_and_a_new_one_joins_the_spec(tmp_path, monkeypatch):
    console = Console(tmp_path)
    seed_claude_agent(console, pinned="v0001")
    deployed: list = []
    monkeypatch.setattr(claude_sdk, "deploy_agent", lambda c, w, body, caller: deployed.append(body) or {"id": "deploy-0123456789"})
    assert sl.redeploy_runtime(console, "dev", POINTS, "v0003", RID, None)["pinned"] is True
    assert sl.redeploy_runtime(console, "dev", "reply-style", "v0002", RID, None)["pinned"] is False
    assert deployed[0]["skills"] == [{"name": POINTS, "version": "v0003"}]
    assert deployed[1]["skills"] == [{"name": POINTS, "version": "v0001"}, {"name": "reply-style", "version": None}]


def test_a_runtime_that_cannot_take_a_calls_skills_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "GRANT_WAIT", 0.0)
    console = Console(tmp_path)
    sl.save_version(console, "dev", POINTS, POINTS_FULL, source="edit")
    ts = sl.put_taskset(console.store, "dev", {"name": "files", "tasks": tasks("t", 2)})
    body = {"runtimeId": RID, "skill": POINTS, "tasksetId": ts["id"]}
    with pytest.raises(sl.SkillLabError, match="not a runtime deployed from the Claude Agent SDK template"):
        sl.start_evaluation(console, "dev", body)  # no claude_sdk record of it: not the template's
    seed_claude_agent(console, template=None)
    with pytest.raises(sl.SkillLabError, match="before a call could bring its own skills"):
        sl.start_evaluation(console, "dev", body)
    seed_claude_agent(console)
    console.data.legacy = True  # the record says template 2 but the image answers without its call: the job stops at once
    job = wait(console, sl.start_evaluation(console, "dev", body)["job"]["id"])
    assert job["status"] == "failed" and "ignored the call's skills" in job["error"]


def test_the_targets_are_the_harnesses_and_the_claude_sdk_runtimes(tmp_path):
    console = Console(tmp_path)
    seed_claude_agent(console)
    assert sl.targets(console, "dev") == [{"kind": "harness", "id": "h-1", "name": "member_bot", "status": "READY"},
                                          {"kind": "runtime", "id": RID, "name": NAME, "status": "READY", "template": 2, "skills": [POINTS],
                                           "model": "us.anthropic.claude-sonnet-5-5", "note": None, "inputFiles": False}]
    seed_claude_agent(console, template=3)
    assert sl.targets(console, "dev")[1]["inputFiles"] is True  # its calls take a task's input files
    seed_claude_agent(console, template=None)
    assert "旧模板" in sl.targets(console, "dev")[1]["note"]


def test_the_samples_include_a_skill_whose_output_is_a_file(tmp_path):
    console = Console(tmp_path)
    assert [s["name"] for s in sl.samples()] == ["loyalty-reply-style", CHART, POINTS]
    got = sl.import_sample(console, "dev", POINTS)
    assert got["skill"]["name"] == POINTS and got["taskset"]["counts"] == {"train": 4, "val": 3, "test": 6}
    ts = sl.taskset(console.store, "dev", got["taskset"]["id"])
    assert all("points-report.xlsx" in t["rubric"] and "「积分明细」" in t["rubric"] and "会员号、本期积分、到期积分" in t["rubric"]
               for k in sl.SPLITS for t in ts[k])  # every rubric checks the file itself
    assert sl.import_sample(console, "dev")["skill"]["name"] == "loyalty-reply-style"  # the default, as before
    with pytest.raises(sl.SkillLabError, match="no sample nope"):
        sl.import_sample(console, "dev", "nope")


def test_the_files_view_is_what_the_judge_reads():
    view = sl.files_view([{"path": "a.xlsx", "type": "xlsx", "bytes": 10, "status": "created", "text": "x" * 30},
                          {"path": "b.md", "type": "md", "bytes": 3, "status": "changed", "text": "yyy", "truncated": True},
                          {"path": "c.png", "type": "image", "bytes": 9, "text": ""}, {"path": "d.pdf", "type": "pdf", "bytes": 1, "error": "PdfReadError: EOF"}], limit=20)
    assert view.splitlines()[:3] == ["Files the agent created or changed in its working directory (4):", "--- a.xlsx · xlsx · 10 bytes · created · truncated ---",
                                     "x" * 20]
    assert "--- b.md · md · 3 bytes · changed · truncated ---\n" in view and "(no text view for this type of file)" not in view.split("c.png")[0]
    assert sl.files_view([]) == "Files the agent created or changed in its working directory: none."


# -- task input files ------------------------------------------------------------------------------------------------------

def xlsx(*members: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue()


WORKBOOK = (("[Content_Types].xml", b"<Types/>"), ("xl/workbook.xml", b"<workbook/>"), ("xl/worksheets/sheet1.xml", b"<worksheet/>"))


def test_task_files_are_kept_once_by_content_and_checked_like_launchpads_task_assets(tmp_path, monkeypatch):
    console = Console(tmp_path)
    up = sl.upload_assets(console, "dev", [{"name": "members.csv", "contentBase64": b64(MEMBERS)}, {"name": "chart.png", "contentBase64": b64(PNG)},
                                          {"name": "book.xlsx", "contentBase64": b64(xlsx(*WORKBOOK))}])["assets"]
    assert [(a["name"], a["type"], a["bytes"], a["created"]) for a in up] == [("members.csv", "csv", len(MEMBERS), True), ("chart.png", "png", len(PNG), True),
                                                                             ("book.xlsx", "xlsx", len(xlsx(*WORKBOOK)), True)]
    assert up[0]["asset"] == f"sha256:{sha(MEMBERS)}" and console.s3.objects[(BUCKET, f"skill-lab/assets/{sha(MEMBERS)}")] == MEMBERS
    again = sl.upload_assets(console, "dev", [{"name": "copy.csv", "contentBase64": b64(MEMBERS)}])["assets"][0]
    assert again["created"] is False and console.s3.puts.count(f"skill-lab/assets/{sha(MEMBERS)}") == 1  # the same content is kept once
    listed = {a["sha256"]: a for a in sl.list_assets(console, "dev")}
    assert listed[sha(MEMBERS)]["names"] == ["members.csv", "copy.csv"] and listed[sha(MEMBERS)]["mediaType"] == "text/csv" and listed[sha(MEMBERS)]["usedBy"] == []
    pdf, jpg, webp = b"%PDF-1.7\n%%EOF\n", b"\xff\xd8\xff\xe0" + b"\x00" * 8, b"RIFF\x10\x00\x00\x00WEBPVP8 "
    assert [sl.sniff_asset(n, d) for n, d in (("a.pdf", pdf), ("a.JPEG", jpg), ("a.webp", webp), ("a.md", "# 标题".encode()), ("a.txt", b"x"))] == [
        "pdf", "jpg", "webp", "md", "txt"]
    bomb = xlsx(*WORKBOOK, ("xl/media/big.bin", b"\x00" * 300_000))
    for name, data, words in (("a.exe", b"MZ", "a task file is csv, jpeg, jpg, md, pdf, png, txt, webp, xlsx"), ("a.csv", PNG, "its content is binary"),
                              ("a.csv", b"a,b\x00", "NUL"), ("a.txt", b"\xff\xfe\x00", "NUL"), ("a.md", b"\xc3\x28", "not UTF-8"),
                              ("a.png", b"GIF89a", "does not match its extension"), ("a.pdf", PNG, "does not match its extension"),
                              ("a.xlsx", xlsx(("[Content_Types].xml", b"<Types/>")), "the workbook's members are missing"),
                              ("a.xlsx", xlsx(*WORKBOOK, ("xl/vbaProject.bin", b"x")), "macros"), ("a.xlsx", bomb, "zip bomb"),
                              ("a.xlsx", b"PK\x03\x04broken", "not a usable xlsx")):
        with pytest.raises(sl.SkillLabError, match=words):
            sl.sniff_asset(name, data)
    for files, words in (([], "at least one"), ([{"name": "a/b.csv", "contentBase64": b64(b"x")}], "a plain file name"),
                         ([{"name": ".env.txt", "contentBase64": b64(b"x")}], "a plain file name"), ([{"name": "a.csv", "contentBase64": "!!"}], "not base64"),
                         ([{"name": "a.csv", "contentBase64": ""}], "empty"),
                         ([{"name": "A.csv", "contentBase64": b64(b"x")}, {"name": "a.CSV", "contentBase64": b64(b"y")}], "the same name")):
        with pytest.raises(sl.SkillLabError, match=words):
            sl.upload_assets(console, "dev", files)
    monkeypatch.setattr(sl, "MAX_ASSET_BYTES", 10)
    with pytest.raises(sl.SkillLabError, match="at most 0 MiB a file"):
        sl.upload_assets(console, "dev", [{"name": "big.csv", "contentBase64": b64(b"x" * 11)}])
    monkeypatch.setattr(sl, "MAX_UPLOAD_FILES", 1)
    with pytest.raises(sl.SkillLabError, match="at most 1 an upload"):
        sl.upload_assets(console, "dev", [{"name": "a.csv", "contentBase64": b64(b"x")}, {"name": "b.csv", "contentBase64": b64(b"y")}])
    assert sl.MAX_UPLOAD_BODY > 4 * sl.MAX_ASSET_BYTES // 3  # a 25 MiB file fits its route's body, base64-encoded


def test_a_task_names_its_files_and_the_set_checks_them(tmp_path, monkeypatch):
    console = Console(tmp_path)
    csv_, png_ = sl.upload_assets(console, "dev", [{"name": "members.csv", "contentBase64": b64(MEMBERS)}, {"name": "logo.png", "contentBase64": b64(PNG)}])["assets"]
    task = {"id": "t1", "question": "画图", "rubric": "画了图",
            "files": [{"asset": csv_["asset"]}, {"path": "data/logo.png", "asset": png_["sha256"]}]}
    launchpads = {"id": "t2", "question": "q", "rubric": "r", "files": {"in/members.csv": {"asset": csv_["asset"], "name": "members.csv", "media_type": "text/csv"}}}
    ts = sl.put_taskset(console.store, "dev", {"name": "with files", "train": [task, launchpads], "val": tasks("b", 1)})
    assert ts["train"][0]["files"] == [{"path": "members.csv", "asset": csv_["asset"], "bytes": len(MEMBERS), "type": "csv"},  # the path: its upload's name
                                       {"path": "data/logo.png", "asset": png_["asset"], "bytes": len(PNG), "type": "png"}]
    assert ts["train"][1]["files"] == [{"path": "in/members.csv", "asset": csv_["asset"], "bytes": len(MEMBERS), "type": "csv"}]  # Launchpad's {path: descriptor}
    assert "files" not in ts["val"][0] and sl.tasksets(console.store, "dev")[0]["files"] == 2
    used = {a["sha256"]: a["usedBy"] for a in sl.list_assets(console, "dev")}
    assert used == {csv_["sha256"]: [{"id": ts["id"], "name": "with files", "tasks": 2}], png_["sha256"]: [{"id": ts["id"], "name": "with files", "tasks": 1}]}
    for files, words in (([{"asset": "sha256:" + "0" * 64}], "not a file uploaded to this workspace"), ([{"path": "x.csv"}], "needs its asset"),
                         ([{"path": "../members.csv", "asset": csv_["asset"]}], "not a plain relative path"),
                         ([{"path": ".claude/members.csv", "asset": csv_["asset"]}], "no hidden names"),
                         ([{"path": "a\\b.csv", "asset": csv_["asset"]}], "not a plain relative path"),
                         ([{"path": "a/b/c/d/e.csv", "asset": csv_["asset"]}], "at most 4 levels"),
                         ([{"path": "members.txt", "asset": csv_["asset"]}], "the file's own extension"),
                         ([{"asset": csv_["asset"]}, {"path": "MEMBERS.csv", "asset": csv_["asset"]}], "twice"),
                         ({"notes.txt": "written inline"}, "upload it as a file"), ("members.csv", "a list of")):
        with pytest.raises(sl.SkillLabError, match=words):
            sl.put_taskset(console.store, "dev", {"name": "bad", "tasks": [{**task, "files": files}]})
    monkeypatch.setattr(sl, "MAX_TASK_FILES", 1)
    with pytest.raises(sl.SkillLabError, match="at most 1 a task"):
        sl.put_taskset(console.store, "dev", {"name": "bad", "tasks": [task]})
    monkeypatch.setattr(sl, "MAX_TASK_FILES", 32)
    monkeypatch.setattr(sl, "MAX_TASK_BYTES", len(MEMBERS))
    with pytest.raises(sl.SkillLabError, match="MiB a task"):
        sl.put_taskset(console.store, "dev", {"name": "bad", "tasks": [task]})
    # a file a task set names is not forgotten; once no set names it, it leaves the catalog but its object stays: the
    # object is stored by its content, so another console on the account and region may hold the same file (review)
    with pytest.raises(sl.SkillLabError, match="is a file of the task set with files"):
        sl.delete_asset(console, "dev", csv_["asset"])
    sl.delete_taskset(console.store, "dev", ts["id"])
    assert sl.delete_asset(console, "dev", csv_["asset"])["objects"] == 0
    assert (BUCKET, f"skill-lab/assets/{csv_['sha256']}") in console.s3.objects and console.s3.deleted == []
    assert [a["sha256"] for a in sl.list_assets(console, "dev")] == [png_["sha256"]]
    with pytest.raises(sl.SkillLabError, match="no task file"):
        sl.delete_asset(console, "dev", csv_["sha256"])


def chart_set(console: Console, *, test_files: bool = True) -> dict:
    up = sl.upload_assets(console, "dev", [{"name": "members.csv", "contentBase64": b64(MEMBERS)},
                                           {"name": "extra.csv", "contentBase64": b64(EXTRA)}])["assets"]
    def task(tid: str, files: list) -> dict:
        return {"id": tid, "question": "工作目录里的 members.csv 画成积分柱状图", "rubric": "生成 points-chart.png\n横向柱状图", **({"files": files} if files else {})}
    both = [{"path": "members.csv", "asset": up[0]["asset"]}, {"path": "supplement.csv", "asset": up[1]["asset"]}]
    return sl.put_taskset(console.store, "dev", {"name": "charts", "train": [task("a0", both[:1]), task("a1", both[:1])], "val": [task("b0", [])],
                                                 "test": [task("c0", both[:1] if test_files else []), task("c1", both if test_files else [])]})


def test_a_harness_is_refused_tasks_that_bring_files_before_anything_of_it_is_touched(tmp_path):
    console = Console(tmp_path)
    sl.save_version(console, "dev", CHART, CHART_FULL, source="edit")
    ts = chart_set(console)
    body = {"harnessId": "h-1", "skill": CHART, "tasksetId": ts["id"]}
    with pytest.raises(sl.SkillLabError, match=r"2 of these tasks give the agent input files \(c0, c1\), and a Harness answers in a sandbox"):
        sl.start_evaluation(console, "dev", body)
    with pytest.raises(sl.SkillLabError, match=r"input files \(a0, a1, c0, c1\)"):
        sl.start_training(console, "dev", body)  # a training answers every split
    assert console.iam.policies == {} and getattr(console.ctl, "reads", 0) == 0 and console.data.calls == []  # not even read
    job = wait(console, sl.start_evaluation(console, "dev", {**body, "split": "val"})["job"]["id"])  # a split without files is fine
    assert job["status"] == "succeeded" and job["params"]["inputFiles"] == 0


def test_a_runtime_call_brings_the_tasks_files_and_the_judge_sees_the_inputs_and_the_pictures(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "GRANT_WAIT", 0.0)
    console = Console(tmp_path)
    seed_claude_agent(console, template=3, skill=CHART)
    sl.save_version(console, "dev", CHART, CHART_FULL, source="edit")
    ts = chart_set(console)
    job = wait(console, sl.start_evaluation(console, "dev", {"runtimeId": RID, "skill": CHART, "tasksetId": ts["id"]})["job"]["id"])
    assert job["status"] == "succeeded", job.get("error")
    result = job["result"]
    assert job["params"]["inputFiles"] == 2 and result["arms"]["with"]["passRate"] == 1.0 and result["arms"]["without"]["passRate"] == 0.0
    members, extra = sha(MEMBERS), sha(EXTRA)
    one = next(p for p in console.data.payloads if len(p["files"]) == 2)
    assert one["files"] == [{"path": "members.csv", "uri": f"s3://{BUCKET}/skill-lab/assets/{members}", "sha256": members, "bytes": len(MEMBERS)},
                            {"path": "supplement.csv", "uri": f"s3://{BUCKET}/skill-lab/assets/{extra}", "sha256": extra, "bytes": len(EXTRA)}]
    assert len(console.data.payloads) == 4 and all(p["files"] for p in console.data.payloads)  # both arms get the task's files
    row = next(r for r in result["rows"] if r["id"] == "c1")
    assert [f["path"] for f in row["files"]] == ["members.csv", "supplement.csv"]
    with_, without = row["with"], row["without"]
    assert [a["path"] for a in with_["artifacts"]] == ["points-chart.png"] and with_["inputs"][1] == {"path": "supplement.csv", "bytes": len(EXTRA), "sha256": extra}
    picture = with_["artifacts"][0]["image"]
    assert "preview" not in picture and picture["thumb"]["width"] == 320 and picture["width"] == 800  # the judge's preview is not kept; the thumbnail is
    assert with_["judgedImages"] == ["points-chart.png, 800×450 px"] and with_["pass"]
    changed = next(a for a in without["artifacts"] if a["path"] == "members.csv")
    assert changed["input"] is True and changed["status"] == "changed"  # the arm without the skill rewrote an input: marked so
    judged = [(i, p) for i, p in enumerate(console.rt.prompts) if "Input files the task gave the agent (2;" in p]
    assert len(judged) == 2 and all(sl.JUDGE_INPUTS in console.rt.systems[i] and sl.JUDGE_IMAGES in console.rt.systems[i] for i, _ in judged)
    i, prompt = next((i, p) for i, p in judged if "--- points-chart.png" in p)
    assert f"- members.csv · csv · {len(MEMBERS)} bytes\n- supplement.csv · csv · {len(EXTRA)} bytes" in prompt and "(an image, 800×450 px: attached below as Image 1)" in prompt
    blocks = console.rt.contents[i]
    assert [next(iter(b)) for b in blocks] == ["text", "text", "image"] and blocks[1]["text"] == "Image 1: points-chart.png, 800×450 px"
    assert blocks[2]["image"] == {"format": "png", "source": {"bytes": PNG}}
    assert "--- members.csv · csv · 12 bytes · changed (an input file) ---" in next(p for _, p in judged if "--- chart.png" in p)
    assert console.iam.policies[(ROLE, sl.SKILLS_POLICY)] == sl.runtime_read_policy(BUCKET)


def test_a_runtime_whose_calls_cannot_bring_files_is_refused_them(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "GRANT_WAIT", 0.0)
    console = Console(tmp_path)
    sl.save_version(console, "dev", CHART, CHART_FULL, source="edit")
    seed_claude_agent(console, template=2, skill=CHART)
    ts = chart_set(console)
    body = {"runtimeId": RID, "skill": CHART, "tasksetId": ts["id"]}
    with pytest.raises(sl.SkillLabError, match=r"before a call could bring its input files, and 2 of these tasks give some \(c0, c1\)"):
        sl.start_evaluation(console, "dev", body)
    assert console.iam.policies == {}
    job = wait(console, sl.start_evaluation(console, "dev", {**body, "split": "val"})["job"]["id"])
    assert job["status"] == "succeeded", job.get("error")  # template 2 still evaluates tasks without files
    seed_claude_agent(console, template=3, skill=CHART)
    console.data.no_inputs = True  # the record says template 3, the image answers without its call's inputs: the job stops at once
    job = wait(console, sl.start_evaluation(console, "dev", body)["job"]["id"])
    assert job["status"] == "failed" and "ignored the call's input files" in job["error"]
    console.data.no_inputs = False
    console.s3.objects.pop((BUCKET, f"skill-lab/assets/{sha(MEMBERS)}"))
    with pytest.raises(sl.SkillLabError, match="members.csv are no longer in the console bucket"):
        sl.start_evaluation(console, "dev", body)


class Refusing:
    """A judge model that takes no temperature (Claude Sonnet 5.5): it refuses the first request."""

    def __init__(self):
        self.requests: list[dict] = []

    def converse(self, **request):
        self.requests.append(json.loads(json.dumps(request, default=lambda b: b.decode("latin-1"))))
        if "temperature" in request["inferenceConfig"]:
            raise RuntimeError("ValidationException: temperature is not supported for this model")
        self.last = request
        return {"output": {"message": {"content": [{"text": '{"pass": true, "score": 1, "reason": "ok"}'}]}}}


def test_the_judge_is_shown_the_pictures_within_its_caps_and_told_why_the_rest_are_not(monkeypatch):
    small = b64(PNG)
    arts = [{"path": f"c{i}.png", "type": "image", "bytes": 32, "status": "created", "image": {"width": 80, "height": 45, "preview": {"format": "png", "base64": small}}}
            for i in range(4)]
    arts.append({"path": "deck.pptx", "type": "pptx", "bytes": 99, "status": "created", "text": "[幻灯片 1] 本期积分", "slides": 1, "imagesTotal": 5,
                 "images": [{"name": "幻灯片 1 · Picture 2", "width": 10, "height": 10, "preview": {"base64": small}},
                            {"name": "幻灯片 1 · Picture 3", "error": "UnidentifiedImageError: cannot identify image file"},
                            {"name": "幻灯片 1 · Picture 4", "preview": {"base64": b64(b"\x89PNG\r\n\x1a\n" + b"1" * 200)}},
                            {"name": "幻灯片 1 · Picture 5", "preview": {"base64": small}}]})
    arts.append({"path": "old.png", "type": "image", "bytes": 32, "status": "created", "base64": small})  # an older template's answer: the image's own bytes
    arts.append({"path": "late.png", "type": "image", "bytes": 32, "status": "created", "image": {"preview": {"base64": small}}})
    arts.append({"path": "members.csv", "type": "csv", "bytes": 5, "status": "changed", "text": "x", "input": True})
    monkeypatch.setattr(sl, "JUDGE_IMAGE_BYTES", 100)
    shown, left = sl.judge_images(arts)
    assert [s["label"] for s in shown] == ["c0.png, 80×45 px", "c1.png, 80×45 px", "c2.png, 80×45 px", "c3.png, 80×45 px", "deck.pptx → 幻灯片 1 · Picture 2, 10×10 px",
                                           "deck.pptx → 幻灯片 1 · Picture 5"]
    assert left == {("deck.pptx", "幻灯片 1 · Picture 3"): "UnidentifiedImageError: cannot identify image file",
                    ("deck.pptx", "幻灯片 1 · Picture 4"): "its preview is 0.0 MB (at most 0.00 MB an image)",
                    ("old.png", None): "a judgment shows at most 6 pictures", ("late.png", None): "a judgment shows at most 6 pictures"}
    view = sl.files_view(arts, images=shown, left_out=left)
    assert "--- c0.png · image · 32 bytes · created ---\n(an image, 80×45 px: attached below as Image 1)" in view
    assert "Pictures in this file (of 5):\n- 幻灯片 1 · Picture 2, 10×10 px: attached below as Image 5\n- 幻灯片 1 · Picture 3: not shown: UnidentifiedImageError" in view
    assert "(an image: not shown: a judgment shows at most 6 pictures)" in view and "--- members.csv · csv · 5 bytes · changed (an input file) ---" in view
    rt = Refusing()
    task = {"question": "画图", "rubric": "图", "files": [{"path": "members.csv", "type": "csv", "bytes": 5, "asset": "sha256:" + "a" * 64}]}
    assert sl.judge(rt, "us.anthropic.claude-sonnet-5-5", task, "已生成。", arts) == {"pass": True, "score": 1.0, "reason": "ok", "judgedImages": [s["label"] for s in shown]}
    first, second = rt.requests
    assert "temperature" in first["inferenceConfig"] and "temperature" not in second["inferenceConfig"]  # retried without it, the pictures still there
    content = rt.last["messages"][0]["content"]
    assert [next(iter(b)) for b in content] == ["text"] + ["text", "image"] * 6 and content[1] == {"text": "Image 1: c0.png, 80×45 px"}
    assert content[2] == {"image": {"format": "png", "source": {"bytes": PNG}}}
    assert "Input files the task gave the agent (1; in its directory before it began, not its work):\n- members.csv · csv · 5 bytes" in content[0]["text"]
    assert rt.last["system"][0]["text"] == sl.JUDGE_SYSTEM + sl.JUDGE_FILES + sl.JUDGE_INPUTS + sl.JUDGE_IMAGES
    rt = Refusing()
    sl.judge(rt, "m", {"question": "q", "rubric": "r"}, "a", [{"path": "a.md", "type": "md", "bytes": 1, "status": "created", "text": "x"}])
    assert len(rt.last["messages"][0]["content"]) == 1 and rt.last["system"][0]["text"] == sl.JUDGE_SYSTEM + sl.JUDGE_FILES  # text only, as before


def test_the_chart_sample_uploads_the_files_its_tasks_give(tmp_path):
    console = Console(tmp_path)
    got = sl.import_sample(console, "dev", CHART)
    assert got["skill"]["name"] == CHART and got["taskset"]["counts"] == {"train": 4, "val": 3, "test": 4} and got["taskset"]["files"] == 11
    ts = sl.taskset(console.store, "dev", got["taskset"]["id"])
    assert [f["path"] for f in ts["test"][1]["files"]] == ["members.csv", "supplement.csv"] and ts["test"][2]["files"][0]["type"] == "xlsx"
    assert all("points-chart.png" in t["rubric"] and "横向柱状图" in t["rubric"] and "没有改动输入文件" in t["rubric"] for k in sl.SPLITS for t in ts[k])
    keys = {k for _b, k in console.s3.objects if k.startswith("skill-lab/assets/")}
    assert len(keys) == 12 and len(console.s3.puts) - console.s3.puts.count(f"skills/{CHART}/v0001/SKILL.md") == 12  # 13 references, 12 contents
    again = sl.import_sample(console, "dev", CHART)
    assert again["taskset"]["id"] == got["taskset"]["id"] and len([k for k in console.s3.puts if k.startswith("skill-lab/")]) == 12  # nothing stored twice


def test_the_page_attaches_files_to_tasks_and_shows_pictures():
    page = (REPO / "app" / "console" / "static" / "pages" / "skills.mjs").read_text(encoding="utf-8")
    assert "export default { id: 'skills', label: 'Skill Lab', group: '构建', Page: SkillLabPage }" in page
    assert "call('POST', ws(wid, '/assets'), { files: [await readFile(f)] })" in page and "ws(wid, `/assets/${a.sha256}`)" in page
    assert "'.xlsx,.pdf,.png,.jpg,.jpeg,.webp,.md,.txt,.csv'" in page and "25 * 1024 * 1024" in page
    assert "a.image && src(a.image.thumb)" in page and "judgedImages" in page and "Harness 的沙箱里放不进文件" in page
    node = Path.home() / ".nvm" / "versions" / "node" / "v25.2.1" / "bin" / "node"
    if not node.exists():
        pytest.skip("no node to syntax-check the page")
    checked = subprocess.run([str(node), "--check", str(REPO / "app" / "console" / "static" / "pages" / "skills.mjs")], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def test_two_workspaces_on_one_account_never_overwrite_or_delete_each_others_skill_versions(tmp_path):
    first = Console(tmp_path / "a")
    second = Console(tmp_path / "b")
    second.s3 = first.s3  # the same account and region: one bucket, two console libraries
    clients = {"s3": first.s3, "bedrock-agentcore-control": second.ctl, "iam": second.iam, "bedrock-runtime": second.rt}
    session = type("S", (), {"client": lambda _s, name, region_name=None, **kw: second.data if name == "bedrock-agentcore" else clients[name]})()
    second.workspaces = type("W", (), {"get": lambda _s, wid: {"id": "dev", "accountId": "111122223333", "region": "us-west-2"},
                                       "verify": lambda _s, wid: {}, "session": lambda _s, wid: session})()
    a = sl.save_version(first, "dev", "reply-style", SEED, source="edit")
    b = sl.save_version(second, "dev", "reply-style", SIGNED, source="edit")
    assert (a["version"], b["version"]) == ("v0001", "v0002")  # the taken number is skipped, not written over
    assert sl.get_skill(first, "dev", "reply-style")["text"] == SEED and sl.get_skill(second, "dev", "reply-style")["text"] == SIGNED
    sl.delete_skill(second, "dev", "reply-style")
    assert sl.get_skill(first, "dev", "reply-style")["text"] == SEED  # the other library's version survives
    assert all(not key.startswith("skills/reply-style/v0001/") for key, _v in first.s3.deleted)


def test_after_a_number_clash_each_save_keeps_its_own_version(tmp_path):
    first, second = Console(tmp_path / "a"), Console(tmp_path / "b")
    second.s3 = first.s3
    clients = {"s3": first.s3, "bedrock-agentcore-control": second.ctl, "iam": second.iam, "bedrock-runtime": second.rt}
    session = type("S", (), {"client": lambda _s, name, region_name=None, **kw: second.data if name == "bedrock-agentcore" else clients[name]})()
    second.workspaces = type("W", (), {"get": lambda _s, wid: {"id": "dev", "accountId": "111122223333", "region": "us-west-2"},
                                       "verify": lambda _s, wid: {}, "session": lambda _s, wid: session})()
    sl.save_version(first, "dev", "reply-style", SEED, source="edit")  # v0001 is the first library's
    texts = [SIGNED, SIGNED + "3. Be brief.\n", SIGNED + "4. Be kind.\n"]
    made = [sl.save_version(second, "dev", "reply-style", t, source="edit")["version"] for t in texts]
    assert made == ["v0002", "v0003", "v0004"]  # review: the third save had renumbered all three records to v0004
    assert [sl.get_skill(second, "dev", "reply-style", v)["text"] for v in made] == texts


def test_publishing_onto_another_teams_harness_asks_before_the_current_version_moves(tmp_path):
    console = Console(tmp_path)
    console.ctl.list_tags_for_resource = lambda resourceArn: {"tags": {}}  # not the console's Harness
    sl.save_version(console, "dev", "reply-style", SEED, source="edit")
    job = {"id": "skill-train-0123456789", "kind": "skill-train", "workspace": "dev", "status": "succeeded", "params": {"skill": "reply-style"},
           "result": {"best": {"step": 1, "text": SIGNED, "valScore": 1.0}, "seed": {"valScore": 0.5}, "improved": True}}
    console.jobs.get = lambda jid: job
    with pytest.raises(sl.SkillLabError, match="acknowledged"):
        sl.publish(console, "dev", job["id"], {"harnessId": "h-1"})
    assert sl.library(console, "dev")["reply-style"]["current"] == "v0001" and len(sl.library(console, "dev")["reply-style"]["versions"]) == 1
