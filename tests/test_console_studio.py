"""engine console.studio: a flow is validated with reasons tied to its nodes, a custom tool is checked without running
it, every template generates a main.py that compiles and — with strands, bedrock-agentcore, boto3 and httpx stubbed —
answers the runtime contract (text/plain with the turn's tool calls, JSON on request, sessions, Swarm and Graph
answers); projects keep versions; a deployment goes through the console's deploy pipeline (fake AWS, every request
checked against botocore) with a Retrieve grant on the flow's knowledge bases; a deployed runtime reopens in Studio."""
from __future__ import annotations

import ast
import base64
import copy
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer.console import deploy as dp  # noqa: E402
from workshop_customizer.console import studio as st  # noqa: E402

_spec = importlib.util.spec_from_file_location("console_deploy_tests_for_studio", REPO / "tests" / "test_console_deploy.py")
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)  # type: ignore[union-attr]  # its FakeAWS (botocore-checked) and the console server module

ACCOUNT, REGION = T.ACCOUNT, T.REGION
KB = "KBEXAMPLE1"


def template(tid: str) -> dict:
    flow = copy.deepcopy(next(t for t in st.TEMPLATES if t["id"] == tid)["flow"])
    for n in flow["nodes"]:
        if n["type"] == "kb":
            n["data"].update(kbId=KB, kbName="adlc-console-demo", kbType="MANAGED")
    return flow


def messages(flow: dict, level: str = "error") -> list[str]:
    return [i["message"] for i in st.validate(st.normalize(flow))["issues"] if i["level"] == level]


def node(flow: dict, nid: str) -> dict:
    return next(n for n in flow["nodes"] if n["id"] == nid)


# -- literals ------------------------------------------------------------------------------------------------------------

def test_a_literal_reads_back_exactly_whatever_the_text():
    tricky = ['plain', '', 'ends with a quote"', 'ends with a backslash\\', '"""三引号"""在中间\n第二行', '\\n is not a newline\nbut this is',
              'tab\there\nand "quotes" \'single\' and \\\\ backslashes\nlong ' * 3, '🙂 emoji\nand 中文 separator', 'x\x00y\x07z\r\nwindows']
    for text in tricky:
        literal = st.py_str(text)
        assert ast.literal_eval(literal) == st._clean(text), literal
        compile(f"value = {literal}\n", "t.py", "exec")
    assert st.py_str("a\n" + "b" * 50).startswith('"""')  # long multi-line text stays readable in the preview
    assert "\x00" not in st._clean("a\x00b") and st._clean("a\r\nb\rc") == "a\nb\nc"


# -- custom tools --------------------------------------------------------------------------------------------------------

GOOD_TOOL = '''import re
from typing import Optional


def word_count(text: str, minimum: Optional[int] = None, unit: str = "词") -> str:
    """数一段文字有多少个词。

    Args:
        text: 要数的文字
        minimum: 只数不少于这个长度的词
        unit: 单位
    """
    words = [w for w in re.split(r"\\s+", text) if w and len(w) >= (minimum or 0)]
    return f"{len(words)} {unit}"
'''


def test_a_custom_tool_is_read_without_running_it():
    info = st.check_tool(GOOD_TOOL)
    assert info["ok"], info["problems"]
    assert info["name"] == "word_count" and info["doc"].startswith("数一段文字")
    assert [(p["name"], p["type"], p.get("default")) for p in info["params"]] == [("text", "str", None), ("minimum", "Optional[int]", None),
                                                                                  ("unit", "str", "词")]
    assert info["imports"] == ["re", "typing"] and info["packages"] == []
    assert st.check_tool("import requests\n\ndef fetch(url: str) -> str:\n    \"\"\"Fetch.\"\"\"\n    return requests.get(url).text\n")["packages"] == [
        "requests>=2.31,<3"]
    assert st.check_tool("async def ping(host: str) -> str:\n    \"\"\"Ping.\"\"\"\n    return host\n")["ok"]


@pytest.mark.parametrize("code, expected", [
    ("", "还没有代码"),
    ("def f(x: str) -> str:\n    return x\n", "文档字符串"),
    ("def f(x) -> str:\n    \"\"\"d\"\"\"\n    return x\n", "类型注解"),
    ("import os\n\ndef f(x: str) -> str:\n    \"\"\"d\"\"\"\n    return os.getcwd()\n", "不能 import os"),
    ("def f(x: str) -> str:\n    \"\"\"d\"\"\"\n    import subprocess\n    return x\n", "不能 import subprocess"),
    ("x = 1\n\ndef f(x: str) -> str:\n    \"\"\"d\"\"\"\n    return x\n", "函数外只能有 import"),
    ("print('loaded')\n\ndef f(x: str) -> str:\n    \"\"\"d\"\"\"\n    return x\n", "函数外只能有 import"),
    ("def f(x: str) -> str:\n    \"\"\"d\"\"\"\n    return eval(x)\n", "不能调用 eval()"),
    ("def f(x: str) -> str:\n    \"\"\"d\"\"\"\n    return open(x).read()\n", "不能调用 open()"),
    ("def f(x: str) -> str:\n    \"\"\"d\"\"\"\n    return x.__class__.__name__\n", "不能访问 __class__"),
    ("def f(x: str) -> str:\n    \"\"\"d\"\"\"\n    return getattr(x, name)\n", "getattr"),
    ("def f(*args: str) -> str:\n    \"\"\"d\"\"\"\n    return ''\n", "*args"),
    ("@cache\ndef f(x: str) -> str:\n    \"\"\"d\"\"\"\n    return x\n", "装饰器"),
    ("def f(x: str) -> str:\n    \"\"\"d\"\"\"\n    return x\n\ndef g(y: str) -> str:\n    \"\"\"d\"\"\"\n    return y\n", "只能有一个函数"),
    ("def f(x: str = str(1)) -> str:\n    \"\"\"d\"\"\"\n    return x\n", "默认值只能是常量"),
    ("def f(x: __import__('os').system('id')) -> str:\n    \"\"\"d\"\"\"\n    return x\n", "不能是表达式"),
    ("def f(x: Optional[str]) -> str:\n    \"\"\"d\"\"\"\n    return x\n", "from typing import Optional"),
    ("def f(x: str) -> str:\n    \"\"\"d\"\"\"\n    yield x\n", "不能 yield"),
    ("from json import loads as time\n\ndef f(x: str) -> str:\n    \"\"\"d\"\"\"\n    return x\n", "和生成的代码冲突"),
    ("def print(x: str) -> str:\n    \"\"\"d\"\"\"\n    return x\n", "函数名 print"),
    ("def 查询(x: str) -> str:\n    \"\"\"d\"\"\"\n    return x\n", "英文字母开头"),
    ("def f(x: str) -> str:\n    \"\"\"d\"\"\"\n    global y\n    return x\n", "global"),
    ("def f(x: str) -> str\n    return x\n", "第 1 行语法错误"),
])
def test_a_custom_tool_is_refused_with_the_reason(code, expected):
    info = st.check_tool(code)
    assert not info["ok"] and any(expected in p for p in info["problems"]), info["problems"]


def test_a_custom_tool_may_import_what_the_generated_module_imports_under_its_own_name():
    assert st.check_tool("import re\nimport json\n\ndef f(x: str) -> str:\n    \"\"\"d\"\"\"\n    return json.dumps(re.findall('a', x))\n")["ok"]
    assert st.check_tool("from collections.abc import Iterable\nimport botocore.exceptions\n\ndef f(x: str) -> str:\n    \"\"\"d\"\"\"\n    return x\n")["ok"]


# -- validation ------------------------------------------------------------------------------------------------------------

def test_every_template_is_a_runnable_flow_once_its_knowledge_base_is_chosen():
    for t in st.TEMPLATES:
        assert st.validate(st.normalize(template(t["id"])))["ok"], (t["id"], messages(template(t["id"])))
    assert messages(next(t for t in st.TEMPLATES if t["id"] == "kb-support")["flow"]) == ["「积分规则知识库」要选一个知识库"]


def test_validation_explains_why_a_flow_cannot_run():
    flow = template("assistant")
    flow["nodes"] = [n for n in flow["nodes"] if n["type"] != "input"]
    assert "需要恰好一个「输入」节点：它代表用户发来的问题" in messages(flow)

    flow = template("assistant")
    flow["edges"] = [e for e in flow["edges"] if e["source"] != "in"]
    assert "「输入」还没连到 Agent：把输入连到接收用户问题的 Agent" in messages(flow)

    flow = template("assistant")  # the answer comes from another agent than the one asked
    flow["nodes"].append(st._n("a2", "agent", 0, 0, name="other"))
    flow["edges"] = [e for e in flow["edges"] if e["target"] != "out"] + [st._e("a2", "out", "out", "in")]
    assert any("不是同一个" in m for m in messages(flow))

    flow = template("graph")  # agent → agent is a dependency: Graph mode only
    flow["graphMode"] = False
    assert any("只在 Graph 模式下有意义" in m for m in messages(flow))

    flow = template("graph")
    flow["edges"].append(st._e("a3", "out", "a1", "in"))
    assert any(m.startswith("Graph 里有环：「调研」 → 「撰写」 → 「审校」 → 「调研」") for m in messages(flow))

    flow = template("graph")  # a second entry nobody feeds
    flow["nodes"].append(st._n("a4", "agent", 0, 0, label="旁路", name="side"))
    flow["edges"].append(st._e("a4", "out", "a3", "in"))
    assert "「旁路」没有上游，是 Graph 的入口：把「输入」连到它（入口会拿到用户的问题）" in messages(flow)

    flow = template("orchestrator")
    flow["edges"] = [e for e in flow["edges"] if e["sourcePort"] != "sub"]
    errors = messages(flow)
    assert any("编排 Agent「主管」还没有子 Agent" in m for m in errors)

    flow = template("swarm")
    flow["edges"] = [e for e in flow["edges"] if e["target"] not in ("a2", "a3")]
    assert any("至少要两个成员" in m for m in messages(flow))

    flow = template("orchestrator")  # one agent under two parents, and a member wired to the output
    flow["nodes"].append(st._n("o2", "orchestrator", 0, 0, label="副主管", name="deputy"))
    flow["edges"] += [st._e("o2", "sub", "a1", "parent"), st._e("a2", "out", "out", "in")]
    errors = messages(flow)
    assert any("一个 Agent 只能有一个上级" in m for m in errors) and any("不能直接连输出" in m for m in errors)

    flow = template("orchestrator")
    node(flow, "a2")["data"]["name"] = "researcher"
    assert any("名称 researcher 重复了" in m for m in messages(flow))

    flow = template("assistant")  # two tools the model would call by the same name
    flow["nodes"].append(st._n("t3", "tool", 0, 0, label="又一个计算器", tool="calculator"))
    flow["edges"].append(st._e("t3", "tool", "a1", "tools"))
    assert any("有两个工具都叫 calculator" in m for m in messages(flow))

    flow = template("kb-support")
    node(flow, "c1")["data"]["code"] = "def search_points_rules(q: str) -> str:\n    \"\"\"d\"\"\"\n    return q\n"
    assert any("search_points_rules" in m for m in messages(flow))

    flow = template("assistant")
    node(flow, "a1")["data"].update(model="", systemPrompt="")
    errors = messages(flow)
    assert "「助手」要选一个模型" in errors and any("系统 Prompt 是空的" in m for m in errors)

    flow = template("assistant")  # warnings: an unusual model, a tool connected to nothing, an agent off the flow
    node(flow, "a1")["data"]["model"] = "us.example.some-model-v1:0"
    flow["nodes"] += [st._n("t9", "tool", 0, 0, label="闲置", tool="current_time"), st._n("a9", "agent", 0, 0, label="孤立", name="lonely")]
    warnings = messages(flow, "warning")
    assert any("不在常用列表里" in m for m in warnings) and "内置工具「闲置」没有连到任何 Agent，不会被用到" in warnings
    assert any("「孤立」不在这个流程里" in m for m in warnings) and st.validate(st.normalize(flow))["ok"]

    issues = st.validate(st.normalize(template("swarm") | {"edges": template("swarm")["edges"] + [st._e("t1", "tool", "s1", "tools")]}))["issues"]
    assert any(i["edge"] and "连线的一端节点不存在" in i["message"] for i in issues)  # an edge to a node that is not there


def test_a_flow_that_is_not_a_flow_is_refused_and_node_data_is_bounded():
    for bad, why in (([], "an object"), ({"nodes": "x"}, "lists"), ({"nodes": [{"id": "a b", "type": "agent"}]}, "node id"),
                     ({"nodes": [{"id": "a", "type": "robot"}]}, "unknown type"), ({"nodes": [{"id": "a", "type": "input"}] * 2}, "two nodes")):
        with pytest.raises(st.StudioError, match=why):
            st.normalize(bad)
    flow = st.normalize({"nodes": [{"id": "a1", "type": "agent", "x": "12.6", "y": None, "data": {"maxTokens": 10**9, "temperature": "hot", "systemPrompt": "x" * 50_000,
                                                                                                "secret": "dropped"}}]})
    data = flow["nodes"][0]["data"]
    assert flow["nodes"][0]["x"] == 13 and data["maxTokens"] == 64_000 and data["temperature"] is None and len(data["systemPrompt"]) == st.MAX_PROMPT
    assert "secret" not in data and flow["graphMode"] is False


# -- code generation ---------------------------------------------------------------------------------------------------

def test_every_template_generates_a_module_that_compiles_with_pinned_requirements():
    for t in st.TEMPLATES:
        files = st.generate(template(t["id"]), project={"id": "sp-0123456789", "name": t["name"]}, version=3, generated_at="2026-10-02T00:00:00Z")
        assert sorted(files) == ["main.py", "requirements.txt", "studio_flow.json"]
        compile(files["main.py"], "main.py", "exec")
        assert "@app.entrypoint\ndef invoke(payload, context=None):" in files["main.py"] and 'if __name__ == "__main__":\n    app.run()' in files["main.py"]
        reqs = files["requirements.txt"].splitlines()
        assert st.STRANDS in reqs and st.AGENTCORE in reqs and "boto3>=1.43.90,<2" in reqs
        assert any(r.startswith(st.TOOLS_PACKAGE) for r in reqs) == (t["id"] == "assistant")  # only calculator / current_time need it
        record = st.read_bundle(st.bundle(files))
        assert record["format"] == st.FORMAT and record["version"] == 3 and record["flow"] == st.normalize(template(t["id"]))
    assert st.bundle(files) == st.bundle(dict(files))  # the same flow makes the same zip
    with pytest.raises(st.StudioError) as caught:
        st.generate(next(t for t in st.TEMPLATES if t["id"] == "kb-support")["flow"])
    assert "1 个问题" in str(caught.value) and caught.value.issues[0]["node"] == "k1"


def test_the_generated_module_wires_what_the_canvas_shows():
    main = st.generate(template("kb-support"))["main.py"]
    assert "@tool\ndef lookup_points(member_id: str) -> str:" in main  # the custom tool as written, @tool added
    assert '@tool(name="search_points_rules", description=' in main and '_retrieve("KBEXAMPLE1", query, 5, managed=True)' in main
    assert "tools=[_kb_k1, lookup_points]," in main and "hooks=[trace]" in main and "import boto3" in main
    main = st.generate(template("orchestrator"))["main.py"]
    assert 'agent_researcher.as_tool(name="researcher", description="查资料、收集事实（可以访问网页）")' in main
    assert main.index("agent_researcher = Agent(") < main.index("agent_lead = Agent(")  # children first
    assert "import httpx" in main and "HTTP_MAX_CHARS = 12000" in main and "from strands_tools" not in main
    main = st.generate(template("swarm"))["main.py"]
    assert "swarm_support_team = Swarm(\n        [agent_triage, agent_billing, agent_tech],\n        entry_point=agent_triage," in main
    main = st.generate(template("graph"))["main.py"]
    assert 'builder.add_edge("research", "draft")' in main and 'builder.set_entry_point("research")' in main and 'OUTPUTS = ["review"]' in main
    vector = template("kb-support")
    node(vector, "k1")["data"]["kbType"] = "VECTOR"
    assert "managed=False" in st.generate(vector)["main.py"]
    assert "managed=True" in st.generate(vector, kbs={KB: {"type": "MANAGED", "name": "adlc-console-demo"}})["main.py"]  # AWS decides at deploy


# -- the generated runtime, with its libraries stubbed ---------------------------------------------------------------

SAMPLE_ARGS = {"lookup_points": {"member_id": "m1001"}, "search_points_rules": {"query": "积分有效期"}, "calculator": {"expression": "1+1"},
               "current_time": {}, "http_request": {"method": "GET", "url": "https://example.com/page"}}


class _Result:
    def __init__(self, text):
        self.text = text

    def __str__(self):
        return self.text + "\n"


def stub_modules(retrieved: list, fail: dict) -> dict[str, types.ModuleType]:
    """strands, strands_tools, bedrock_agentcore, starlette, boto3 and httpx as small fakes that keep the shapes the
    generated code relies on."""
    mods = {name: types.ModuleType(name) for name in ("strands", "strands.hooks", "strands.models", "strands.multiagent", "strands_tools",
                                                      "bedrock_agentcore", "bedrock_agentcore.runtime", "starlette", "starlette.responses", "boto3", "httpx")}

    class AfterToolCallEvent:
        def __init__(self, agent, tool_use, result, exception=None):
            self.agent, self.tool_use, self.result, self.exception = agent, tool_use, result, exception

    class HookProvider:
        pass

    class HookRegistry:
        def __init__(self):
            self.callbacks = []

        def add_callback(self, event_type, fn):
            self.callbacks.append((event_type, fn))

    def tool(fn=None, *, name=None, description=None):
        def wrap(f):
            f.tool_name, f.tool_description = name or f.__name__, description or f.__doc__
            return f
        return wrap(fn) if fn is not None else wrap

    class Agent:
        def __init__(self, name=None, description=None, model=None, system_prompt=None, tools=None, callback_handler=None, hooks=None):
            self.name, self.description, self.model, self.system_prompt, self.tools = name, description, model, system_prompt, list(tools or [])
            self.registry, self.messages = HookRegistry(), []
            for hook in hooks or []:
                hook.register_hooks(self.registry)

        def __call__(self, prompt):
            self.messages.append(prompt)
            outputs = []
            for t in self.tools:  # as strands does: a tool that raises becomes an error result, the turn goes on
                name = getattr(t, "tool_name", None)
                try:
                    out, result, error = t(**SAMPLE_ARGS.get(name, {"input": f"from {self.name}"})), {"status": "success"}, None
                except Exception as exc:  # noqa: BLE001
                    out, result, error = f"Error: {type(exc).__name__} - {exc}", {"status": "error"}, exc
                outputs.append(f"{name}={str(out).strip()}")
                for _kind, fn in self.registry.callbacks:
                    fn(AfterToolCallEvent(self, {"name": name}, result, error))
            if fail.get(self.name):
                raise RuntimeError(fail[self.name])
            return _Result(f"{self.name}<{prompt}>[{'; '.join(outputs)}] turns={len(self.messages)}")

        def as_tool(self, name=None, description=None):
            def run(input: str = "") -> str:
                return str(self(input)).strip()
            run.tool_name, run.tool_description = name or self.name, description
            return run

    class BedrockModel:
        def __init__(self, **kw):
            self.kw = kw

    class Status:
        def __init__(self, value):
            self.value = value

    class NodeResult:
        def __init__(self, result):
            self.result = result

    class Named:
        def __init__(self, node_id):
            self.node_id = node_id

    class Swarm:
        def __init__(self, nodes, *, entry_point=None, **kw):
            self.nodes, self.entry, self.kw = nodes, entry_point, kw

        def __call__(self, task):
            order = [self.entry] + [n for n in self.nodes if n is not self.entry]
            results = {a.name: NodeResult(a(task)) for a in order}
            return types.SimpleNamespace(node_history=[Named(a.name) for a in order], results=results, status=Status("completed"))

    class GraphBuilder:
        def __init__(self):
            self.nodes, self.edges, self.entries, self.timeouts = {}, [], [], {}

        def add_node(self, executor, node_id):
            self.nodes[node_id] = executor

        def add_edge(self, a, b):
            self.edges.append((a, b))

        def set_entry_point(self, node_id):
            self.entries.append(node_id)

        def set_execution_timeout(self, t):
            self.timeouts["execution"] = t

        def set_node_timeout(self, t):
            self.timeouts["node"] = t

        def build(self):
            builder = self

            def run(task):
                done, results = [], {}
                while len(done) < len(builder.nodes):
                    ready = [n for n in builder.nodes if n not in done and all(a in done for a, b in builder.edges if b == n)]
                    for nid in ready:
                        upstream = [str(results[a].result).strip() for a, b in builder.edges if b == nid]
                        results[nid] = NodeResult(builder.nodes[nid](task if not upstream else " + ".join(upstream)))
                        done.append(nid)
                return types.SimpleNamespace(execution_order=[Named(n) for n in done], results=results, status=Status("completed"),
                                             completed_nodes=len(done), total_nodes=len(done))
            return run

    class BedrockAgentCoreApp:
        def __init__(self):
            self.handlers = {}

        def entrypoint(self, fn):
            self.handlers["main"] = fn
            return fn

        def run(self):  # pragma: no cover - never in tests
            raise AssertionError("app.run() in a test")

    class Response:
        def __init__(self, content=None, status_code=200, media_type=None):
            self.body, self.status_code, self.media_type = content, status_code, media_type

    class Retrieve:
        def retrieve(self, **kw):
            retrieved.append(kw)
            if fail.get("retrieve"):
                raise RuntimeError(fail["retrieve"])
            return {"retrievalResults": [{"content": {"text": "每笔积分自获得之日起 24个月 有效"}, "score": 0.64,
                                          "location": {"s3Location": {"uri": "s3://b/kb/KBEXAMPLE1/points-earning-policy.md"}, "type": "S3"}}]}

    class HTTPError(Exception):
        pass

    class Client:
        def __init__(self, **kw):
            self.kw = kw

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, method, url, headers=None, content=None):
            page = "<html><head><style>p{}</style><script>var x=1</script></head><body><h1>AWS &amp; regions</h1>" + "<p>区域</p>" * 20000 + "</body></html>"
            return types.SimpleNamespace(status_code=200, reason_phrase="OK", text=page, headers={"content-type": "text/html; charset=utf-8"})

    def calculator(expression: str = "") -> str:
        return "Result: 2"

    def current_time(timezone: str = "") -> str:
        return "2026-10-02T08:00:00+08:00"

    calculator.tool_name, current_time.tool_name = "calculator", "current_time"
    for name, value in (("strands", {"Agent": Agent, "tool": tool}), ("strands.hooks", {"AfterToolCallEvent": AfterToolCallEvent, "HookProvider": HookProvider,
                                                                                         "HookRegistry": HookRegistry}),
                        ("strands.models", {"BedrockModel": BedrockModel}), ("strands.multiagent", {"Swarm": Swarm, "GraphBuilder": GraphBuilder}),
                        ("strands_tools", {"calculator": calculator, "current_time": current_time}),
                        ("bedrock_agentcore.runtime", {"BedrockAgentCoreApp": BedrockAgentCoreApp}), ("starlette.responses", {"Response": Response}),
                        ("boto3", {"client": lambda name, region_name=None: Retrieve()}), ("httpx", {"Client": Client, "HTTPError": HTTPError})):
        mods[name].__dict__.update(value)
    return mods


@pytest.fixture()
def runtime(monkeypatch):
    """Load a generated main.py on the stubs: ``load(flow) -> (namespace, retrieve calls)``; ``fail[agent] = message``."""
    retrieved: list = []
    fail: dict = {}
    for name, module in stub_modules(retrieved, fail).items():
        monkeypatch.setitem(sys.modules, name, module)

    def load(flow):
        namespace = {"__name__": "studio_generated"}
        exec(compile(st.generate(flow, project={"id": "sp-0123456789", "name": "测试"}, version=2)["main.py"], "main.py", "exec"), namespace)
        return namespace
    return load, retrieved, fail


def ctx(sid: str):
    return types.SimpleNamespace(session_id=sid * 40)


def test_the_runtime_answers_text_with_the_turns_tool_calls_and_keeps_each_sessions_conversation(runtime):
    load, retrieved, _fail = runtime
    ns = load(template("kb-support"))
    reply = ns["invoke"]({"prompt": "M1001 的积分多久过期？", "actorId": "alice"}, ctx("a"))
    assert reply.status_code == 200 and reply.media_type == "text/plain; charset=utf-8"
    assert "LEDGER-7731" in reply.body and "24个月" in reply.body and "turns=1" in reply.body
    assert reply.body.endswith("〔工具〕points_support·search_points_rules，points_support·lookup_points")
    assert retrieved == [{"knowledgeBaseId": KB, "retrievalQuery": {"text": "积分有效期"}, "retrievalConfiguration": {"managedSearchConfiguration": {"numberOfResults": 5}}}]
    assert "turns=2" in ns["invoke"]({"prompt": "那兑换呢？"}, ctx("a")).body  # same session: the same Agent, its conversation kept
    assert "turns=1" in ns["invoke"]({"prompt": "你好"}, ctx("b")).body  # another session starts afresh
    as_json = ns["invoke"]({"prompt": "再问一次", "format": "json"}, ctx("a"))
    assert as_json["tools"] == [{"agent": "points_support", "tool": "search_points_rules", "ok": True}, {"agent": "points_support", "tool": "lookup_points", "ok": True}]
    assert as_json["flow"] == {"project": "sp-0123456789", "name": "测试", "version": 2, "mode": "agent"} and "〔工具〕" not in as_json["result"]
    assert "〔" not in ns["invoke"]({"prompt": "不要工具记录", "trace": False}, ctx("a")).body
    empty = ns["invoke"]({"prompt": "  "}, ctx("a"))
    assert empty.status_code == 400 and json.loads(empty.body) == {"error": "the payload needs a non-empty prompt"}
    no_trace = template("kb-support")
    node(no_trace, "out")["data"]["traceTools"] = False
    assert "〔" not in load(no_trace)["invoke"]({"prompt": "hi"}, ctx("c")).body


def test_the_runtime_reports_a_failed_turn_as_an_error_with_the_calls_made(runtime):
    load, _retrieved, fail = runtime
    ns = load(template("orchestrator"))
    fail["lead"] = "ThrottlingException: slow down"
    reply = ns["invoke"]({"prompt": "写点什么"}, ctx("a"))
    body = json.loads(reply.body)
    assert reply.status_code == 500 and body["error"] == "RuntimeError: ThrottlingException: slow down"
    assert [c["tool"] for c in body["tools"]] == ["http_request", "researcher", "writer"]  # what ran before the failure


def test_a_failed_tool_call_is_marked_failed_and_the_turn_goes_on(runtime):
    load, _retrieved, fail = runtime
    fail["retrieve"] = "AccessDeniedException: not authorized to perform bedrock:Retrieve"
    reply = load(template("kb-support"))["invoke"]({"prompt": "积分多久过期"}, ctx("a"))
    assert reply.status_code == 200 and "search_points_rules=Error: RuntimeError - 知识库 KBEXAMPLE1 检索失败：RuntimeError: AccessDeniedException" in reply.body
    assert reply.body.endswith("〔工具〕points_support·search_points_rules（失败），points_support·lookup_points")
    fail.clear()
    fail["writer"] = "the writer's model is not enabled"
    reply = load(template("orchestrator"))["invoke"]({"prompt": "写点什么"}, ctx("b"))
    assert reply.status_code == 200 and reply.body.endswith("〔工具〕researcher·http_request，lead·researcher，lead·writer（失败）")


def test_an_orchestrators_sub_agents_are_its_tools_and_their_own_tool_calls_are_traced(runtime):
    load, _retrieved, _fail = runtime
    reply = load(template("orchestrator"))["invoke"]({"prompt": "调研 us-west-2"}, ctx("a")).body
    assert reply.endswith("〔工具〕researcher·http_request，lead·researcher，lead·writer")
    page = reply.split("http_request=", 1)[1]
    assert page.startswith("HTTP 200 OK\nAWS & regions 区域 区域") and "var x" not in page and "<p>" not in page
    assert "（正文共 " in page and "这里只有前 12000 个）" in page  # a big page reaches the model cut to HTTP_MAX_CHARS


def test_a_swarm_answers_with_its_last_member_and_a_graph_with_the_outputs_upstream_nodes(runtime):
    load, _retrieved, _fail = runtime
    swarm = load(template("swarm"))
    reply = swarm["invoke"]({"prompt": "多扣费了"}, ctx("a")).body
    assert reply.startswith("tech<多扣费了>") and reply.endswith("〔这一轮没有调用工具〕")
    second = swarm["invoke"]({"prompt": "还有呢"}, ctx("a")).body
    assert "之前的对话：\n用户：多扣费了\n助手：tech<多扣费了>" in second and "用户现在说：还有呢" in second  # a swarm restarts: history in front
    graph = load(template("graph"))
    reply = graph["invoke"]({"prompt": "介绍 AgentCore"}, ctx("a")).body
    assert reply.startswith("review<draft<research<介绍 AgentCore>") and "turns=1" in reply  # research → draft → review, review is the answer
    flow = template("graph")  # two nodes connected to the output: both, in the order they ran
    flow["edges"].append(st._e("a2", "out", "out", "in"))
    both = load(flow)["invoke"]({"prompt": "x"}, ctx("a")).body
    assert both.startswith("【撰写】\ndraft<") and "\n\n【审校】\nreview<" in both


def test_the_assistant_template_uses_the_built_in_tools_with_its_timezone(runtime, monkeypatch):
    load, _retrieved, _fail = runtime
    monkeypatch.setenv("DEFAULT_TIMEZONE", "x")
    monkeypatch.delenv("DEFAULT_TIMEZONE")  # absent now, and absent again after the test
    ns = load(template("assistant"))
    reply = ns["invoke"]({"prompt": "几点了"}, ctx("a")).body
    assert "calculator=Result: 2" in reply and "current_time=2026-10-02T08:00:00+08:00" in reply
    assert reply.endswith("〔工具〕assistant·calculator，assistant·current_time")
    import os
    assert os.environ["DEFAULT_TIMEZONE"] == "Asia/Shanghai"


# -- deploy.py: the knowledge-base grant ------------------------------------------------------------------------------

def test_a_deployment_may_name_knowledge_bases_its_role_retrieves_from():
    archive = T.b64(T.zip_of(T.sample()))
    req = dp.deploy_request({"source": "zip", "name": "kb_agent", "archive": archive, "knowledgeBases": [KB, KB, "ABCDEFGHIJ"]})
    assert req["knowledgeBases"] == ["ABCDEFGHIJ", KB] and dp.public_params(req)["knowledgeBases"] == ["ABCDEFGHIJ", KB]
    assert dp.deploy_request({"source": "zip", "name": "kb_agent", "archive": archive})["knowledgeBases"] == []
    assert "knowledgeBases" not in dp.public_params(dp.deploy_request({"source": "zip", "name": "kb_agent", "archive": archive}))
    for bad in ("KBEXAMPLE1", ["short"], [KB] * 0 + [f"KB{i:08d}" for i in range(11)], [None]):
        with pytest.raises(dp.DeployError, match="knowledgeBases"):
            dp.deploy_request({"source": "zip", "name": "kb_agent", "archive": archive, "knowledgeBases": bad})
    policy = dp.runtime_role_policy(account=ACCOUNT, region=REGION, knowledge_bases=[KB])
    [grant] = [s for s in policy["Statement"] if s["Sid"] == "KnowledgeBases"]
    assert grant == {"Sid": "KnowledgeBases", "Effect": "Allow", "Action": "bedrock:Retrieve", "Resource": [f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/{KB}"]}
    assert "KnowledgeBases" not in [s["Sid"] for s in dp.runtime_role_policy(account=ACCOUNT, region=REGION)["Statement"]]


# -- through the console: projects, preview, bundle, deploy, reopen --------------------------------------------------

class StudioAWS(T.FakeAWS):
    """The deploy module's fake AWS plus the knowledge bases and S3 reads Studio does."""

    SERVICES = {**T.FakeAWS.SERVICES, "bedrock-agent": "kb"}

    def __init__(self, **kw):
        super().__init__(**kw)
        self.kbs = {KB: {"knowledgeBaseId": KB, "name": "adlc-console-demo", "status": "ACTIVE", "description": "拾光家居积分规则（演示）",
                         "knowledgeBaseArn": f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/{KB}", "knowledgeBaseConfiguration": {"type": "MANAGED"},
                         "roleArn": f"arn:aws:iam::{ACCOUNT}:role/kb", "createdAt": "t", "updatedAt": "t"}}
        self.answer = {"result": "您好，我是积分客服。"}

    def client(self, service, region_name=None, **kw):
        return StudioClient(self, service)

    def kb_get_knowledge_base(self, knowledgeBaseId):
        if knowledgeBaseId not in self.kbs:
            raise T.err("ResourceNotFoundException", f"kb {knowledgeBaseId}")
        return {"knowledgeBase": self.kbs[knowledgeBaseId]}

    def kb_list_knowledge_bases(self, **p):
        return {"knowledgeBaseSummaries": [{k: v[k] for k in ("knowledgeBaseId", "name", "status", "description")} | {"updatedAt": "t"}
                                           for v in self.kbs.values()]}

    def logs_filter_log_events(self, logGroupName, **p):
        if logGroupName not in self.log_groups:
            raise T.err("ResourceNotFoundException", "no group")
        stream = "2026/10/02/[runtime-logs-{}]5bc68ffa"
        return {"events": [
            {"logStreamName": stream.format("console-b" * 4), "timestamp": 1790874900000, "message": 'INFO studio: turn {"tools": ["search_points_rules"]}\n'},
            {"logStreamName": stream.format("console-a" * 4), "timestamp": 1790874800000, "message": 'ERROR studio: turn failed {"error": "AccessDeniedException"}'},
            {"logStreamName": stream.format("console-a" * 4), "timestamp": 1790874700000, "message": 'INFO studio: turn {"tools": []}'}]}

    def s3_get_object(self, Bucket, Key, **p):
        if not self.objects.get(Key):
            raise T.err("NoSuchKey", "no such key")
        body = self.objects[Key][-1]["Body"]
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}


class StudioClient(T.FakeClient):
    def __getattr__(self, op: str):
        handler = getattr(self.aws, f"{type(self.aws).SERVICES[self.service]}_{op}", None)
        if handler is None:
            raise AttributeError(f"{self.service}.{op}")

        def call(**params):
            T.check_shape(self.service, op, params)
            self.aws.calls.append((self.service, op, params))
            return handler(**params)
        return call


class Client:
    def __init__(self, port):
        self.base, self.cookie = f"http://127.0.0.1:{port}/api/console", None

    def call(self, method, path, body=None):
        headers = {"Content-Type": "application/json", **({"Cookie": self.cookie} if self.cookie else {})}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.headers.get("Set-Cookie"):
                    self.cookie = resp.headers["Set-Cookie"].split(";")[0]
                raw = resp.read()
                return resp.status, (json.loads(raw or b"null") if "json" in (resp.headers.get("Content-Type") or "") else raw)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture()
def server(tmp_path, monkeypatch):
    for owner, attrs in ((dp.Pipeline, ("BUILD_POLL", "RUNTIME_POLL", "ENDPOINT_POLL", "IAM_PAUSE")), (dp.Teardown, ("POLL",))):
        for attr in attrs:
            monkeypatch.setattr(owner, attr, 0.0)
    aws = StudioAWS()
    console = T.console_mod.Console(tmp_path / "data", session_factory=lambda **kw: T.FakeSession(aws), clients_factory=lambda cfg: None)
    srv = T.console_mod.create_server(console)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = Client(srv.server_address[1])
    assert client.call("POST", "/workspaces", {"id": "dev", "accountId": ACCOUNT, "region": REGION, "profile": "default"})[0] == 201
    yield client, aws, console
    srv.shutdown()
    srv.server_close()


def finished(client: Client, job_id: str) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status, job = client.call("GET", f"/jobs/{job_id}")
        if status == 200 and job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still running")


S = "/workspaces/dev/studio"


def test_projects_keep_versions_and_the_canvas_previews_bundles_and_downloads(server):
    client, _aws, _console = server
    status, cat = client.call("GET", f"{S}/catalog")
    assert status == 200 and [t["id"] for t in cat["templates"]] == [t["id"] for t in st.TEMPLATES] and cat["defaultModel"] == st.DEFAULT_MODEL
    assert {t["type"] for t in cat["types"]} == set(st.NODE_TYPES) and any(r["graphOnly"] for r in cat["rules"])
    status, created = client.call("POST", f"{S}/projects", {"name": "积分客服", "template": "kb-support"})
    assert status == 201 and created["version"] == 1 and created["flowVersion"] == 1 and created["flow"] == st.normalize(st.TEMPLATES[1]["flow"])
    pid = created["id"]
    assert client.call("POST", f"{S}/projects", {"name": "x", "template": "nope"})[0] == 400
    flow = template("kb-support")
    status, saved = client.call("PUT", f"{S}/projects/{pid}", {"flow": flow, "note": "选了知识库"})
    assert status == 200 and saved["saved"] and saved["version"] == 2 and [v["version"] for v in saved["versionsList"]] == [2, 1]
    status, again = client.call("PUT", f"{S}/projects/{pid}", {"flow": flow, "name": "积分客服 v2"})
    assert not again["saved"] and again["version"] == 2 and again["name"] == "积分客服 v2"  # unchanged flow: no new version
    status, old = client.call("GET", f"{S}/projects/{pid}?version=1")
    assert old["flowVersion"] == 1 and not st.validate(st.normalize(old["flow"]))["ok"]
    assert client.call("GET", f"{S}/projects/{pid}?version=9")[0] == 404 and client.call("GET", f"{S}/projects/sp-0000000000")[0] == 404
    assert [p["id"] for p in client.call("GET", f"{S}/projects")[1]["projects"]] == [pid]
    status, preview = client.call("POST", f"{S}/preview", {"flow": flow, "project": {"id": pid, "name": "积分客服", "version": 2}})
    assert status == 200 and preview["ok"] and "def lookup_points" in preview["files"]["main.py"] and preview["tools"]["c1"]["name"] == "lookup_points"
    status, broken = client.call("POST", f"{S}/preview", {"flow": template("graph") | {"graphMode": False}})
    assert status == 200 and not broken["ok"] and broken["files"] is None and any(i["edge"] for i in broken["issues"] if i["level"] == "error")
    status, packed = client.call("POST", f"{S}/bundle", {"flow": flow, "project": {"name": "Points Support", "version": 2}})
    assert status == 200 and packed["filename"] == "points_support-v2.zip" and packed["files"] == ["main.py", "requirements.txt", "studio_flow.json"]
    assert st.read_bundle(base64.b64decode(packed["archive"]))["flow"] == st.normalize(flow)
    status, kbs = client.call("GET", f"{S}/knowledge-bases")
    assert status == 200 and kbs["knowledgeBases"][0] | {} == {"id": KB, "name": "adlc-console-demo", "status": "ACTIVE", "description": "拾光家居积分规则（演示）",
                                                                "type": "MANAGED"}
    status, gone = client.call("DELETE", f"{S}/projects/{pid}")
    assert status == 200 and gone["deleted"] == pid and client.call("GET", f"{S}/projects")[1]["projects"] == []


def test_deploying_a_project_runs_the_deploy_pipeline_with_a_retrieve_grant_and_reopens_from_the_runtime(server):
    client, aws, console = server
    pid = client.call("POST", f"{S}/projects", {"name": "积分客服", "flow": template("kb-support")})[1]["id"]
    status, job = client.call("POST", f"{S}/projects/{pid}/deploy", {"name": "adlc_probe_studio_ab12cd"})
    assert status == 202 and job["kind"] == "deploy" and job["params"]["knowledgeBases"] == [KB] and job["studio"]["flowVersion"] == 1
    done = finished(client, job["id"])
    assert done["status"] == "succeeded", done.get("error")
    rid = done["result"]["runtimeId"]
    [put] = [p for p in aws.named("s3", "put_object") if p["Key"].endswith("/source.zip")]
    with zipfile.ZipFile(io.BytesIO(put["Body"])) as uploaded:
        assert uploaded.namelist() == ["main.py", "requirements.txt", "studio_flow.json"] and "def lookup_points" in uploaded.read("main.py").decode()
    [start] = aws.named("codebuild", "start_build")
    assert start["projectName"] == "adlc-probe-studio-build"  # a probe never touches the console's (or another probe's) build project
    [create] = aws.named("bedrock-agentcore-control", "create_agent_runtime")
    assert create["agentRuntimeName"] == "adlc_probe_studio_ab12cd" and create["roleArn"].endswith(":role/adlc-console/adlc-probe-studio-rt-adlc_probe_studio_ab12cd")
    assert create["agentRuntimeArtifact"]["codeConfiguration"]["runtime"] == "PYTHON_3_13"
    policy = json.loads(next(p for p in aws.named("iam", "put_role_policy") if p["RoleName"] == "adlc-probe-studio-rt-adlc_probe_studio_ab12cd")["PolicyDocument"])
    assert [s["Resource"] for s in policy["Statement"] if s["Sid"] == "KnowledgeBases"] == [[f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/{KB}"]]
    [smoke] = aws.named("bedrock-agentcore", "invoke_agent_runtime")
    assert json.loads(smoke["payload"]) == {"prompt": "我是会员 M1001，我现在有多少积分？这些积分多久会过期？"}  # the Input node's sample
    [d] = st.studio_runtimes(console, "dev")
    assert (d["runtimeId"], d["jobStatus"], d["runtimeVersion"], d["flowVersion"], d["name"], d["projectId"]) == (
        rid, "succeeded", "1", 1, "adlc_probe_studio_ab12cd", pid)
    assert client.call("POST", f"{S}/open-runtime", {"runtimeId": rid})[1] == {"projectId": pid, "version": 1, "imported": False}

    # a new version of the same runtime, from a changed flow
    flow = template("kb-support")
    node(flow, "a1")["data"]["model"] = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert client.call("PUT", f"{S}/projects/{pid}", {"flow": flow})[1]["version"] == 2
    status, update = client.call("POST", f"{S}/projects/{pid}/deploy", {"runtimeId": rid, "smoke": False})
    assert status == 202 and update["studio"]["mode"] == "update" and update["studio"]["flowVersion"] == 2
    assert finished(client, update["id"])["result"]["version"] == "2"
    assert "claude-haiku" in zipfile.ZipFile(io.BytesIO([p for p in aws.named("s3", "put_object") if p["Key"].endswith("/source.zip")][-1]["Body"])).read(
        "main.py").decode()

    # another console (no project here) reopens the runtime from its deployment's source.zip
    console.store.write(st.COLLECTION, {})
    status, opened = client.call("POST", f"{S}/open-runtime", {"runtimeId": rid})
    assert status == 200 and opened["imported"] and opened["version"] == 1
    status, imported = client.call("GET", f"{S}/projects/{opened['projectId']}")
    assert imported["flow"] == st.normalize(flow) and imported["importedFrom"]["runtimeId"] == rid and "从 adlc_probe_studio_ab12cd 导入" in imported["name"]
    [link] = imported["deploymentsList"]  # the imported project knows its runtime: a new version can be published from it
    assert (link["runtimeId"], link["jobStatus"], link["runtimeVersion"], link["mode"]) == (rid, "succeeded", "2", "imported")
    assert client.call("POST", f"{S}/open-runtime", {"runtimeId": rid})[1] == {"projectId": opened["projectId"], "version": 1, "imported": False}
    status, again = client.call("POST", f"{S}/projects/{opened['projectId']}/deploy", {"runtimeId": rid, "smoke": False})
    assert status == 202 and finished(client, again["id"])["result"]["version"] == "3"
    theirs = aws.seed_runtime("someone_elses", console=False)["agentRuntimeId"]
    assert client.call("POST", f"{S}/open-runtime", {"runtimeId": theirs})[0] == 400


def test_a_deployment_is_refused_before_anything_is_made_when_the_flow_or_its_kb_cannot_run(server):
    client, aws, _console = server
    pid = client.call("POST", f"{S}/projects", {"name": "坏的", "template": "kb-support"})[1]["id"]
    status, refused = client.call("POST", f"{S}/projects/{pid}/deploy", {"name": "studio_agent"})
    assert status == 400 and refused["issues"][0]["node"] == "k1"  # no KB chosen: the flow's own issue, tied to its node
    flow = template("kb-support")
    node(flow, "k1")["data"]["kbId"] = "MISSING123"
    client.call("PUT", f"{S}/projects/{pid}", {"flow": flow})
    status, refused = client.call("POST", f"{S}/projects/{pid}/deploy", {"name": "studio_agent"})
    assert status == 400 and "MISSING123 不在这个工作区" in refused["error"]
    aws.kbs[KB]["status"] = "CREATING"
    client.call("PUT", f"{S}/projects/{pid}", {"flow": template("kb-support")})
    assert "等它 ACTIVE" in client.call("POST", f"{S}/projects/{pid}/deploy", {"name": "studio_agent"})[1]["error"]
    assert client.call("POST", f"{S}/projects/{pid}/deploy", {"name": "1bad"})[0] == 400
    assert not aws.named("bedrock-agentcore-control", "create_agent_runtime") and not aws.named("s3", "put_object")
    aws.kbs[KB]["status"] = "ACTIVE"
    # a member may build and save, not deploy or delete a project
    assert client.call("POST", "/users", {"username": "admin", "password": "0123456789", "role": "admin"})[0] == 201
    assert client.call("POST", "/login", {"username": "admin", "password": "0123456789"})[0] == 200
    assert client.call("POST", "/users", {"username": "ann", "password": "0123456789", "role": "member", "workspaces": ["dev"]})[0] == 201
    client.cookie = None
    assert client.call("POST", "/login", {"username": "ann", "password": "0123456789"})[0] == 200
    assert client.call("PUT", f"{S}/projects/{pid}", {"flow": template("assistant")})[0] == 200
    assert client.call("POST", f"{S}/projects/{pid}/deploy", {"name": "member_agent"})[0] == 403
    assert client.call("DELETE", f"{S}/projects/{pid}")[0] == 403


def test_a_runtimes_turns_and_errors_are_read_from_its_log_group_per_session(server):
    client, aws, _console = server
    rid = aws.seed_runtime("studio_agent")["agentRuntimeId"]
    status, none = client.call("GET", f"{S}/runtimes/{rid}/logs")
    assert status == 200 and none == {"logGroup": f"/aws/bedrock-agentcore/runtimes/{rid}-DEFAULT", "events": [], "missing": True}
    aws.log_groups.add(f"/aws/bedrock-agentcore/runtimes/{rid}-DEFAULT")
    status, found = client.call("GET", f"{S}/runtimes/{rid}/logs?session={'console-a' * 4}&minutes=30")
    assert status == 200 and [e["message"] for e in found["events"]] == ['INFO studio: turn {"tools": []}', 'ERROR studio: turn failed {"error": "AccessDeniedException"}']
    assert found["events"][0] == {"at": "2026-10-01T17:11:40Z", "session": "console-a" * 4, "message": 'INFO studio: turn {"tools": []}'} and found["minutes"] == 30
    [call] = aws.named("logs", "filter_log_events")[-1:]
    assert call["filterPattern"] == st.LOG_PATTERN and call["logGroupName"] == f"/aws/bedrock-agentcore/runtimes/{rid}-DEFAULT"
    assert len(client.call("GET", f"{S}/runtimes/{rid}/logs")[1]["events"]) == 3
    assert client.call("GET", f"{S}/runtimes/not-a-runtime/logs")[0] == 400


def test_a_probe_runtime_gets_its_modules_probe_names_and_any_other_the_consoles():
    names = st.names_for("adlc_probe_studio_1a2b3c")
    assert names.runtime_role("adlc_probe_studio_1a2b3c") == "adlc-probe-studio-rt-adlc_probe_studio_1a2b3c" and names.build_project == "adlc-probe-studio-build"
    assert st.names_for("adlc_probe_x").build_project == "adlc-probe-build" and st.names_for("order_agent") is dp.NAMES
    assert len(st.names_for("adlc_probe_studio_" + "x" * 30).runtime_role("adlc_probe_studio_" + "x" * 30)) <= 64


# -- the page -----------------------------------------------------------------------------------------------------------

def test_the_page_is_a_module_the_console_loads():
    page = (REPO / "app" / "console" / "static" / "pages" / "studio.mjs").read_text(encoding="utf-8")
    assert "export default { id: 'studio', label: 'Studio', group: '构建', Page" in page
    assert "from '../ui.mjs'" in page and "import React from 'react'" in page
    node_bin = shutil.which("node") or str(Path.home() / ".nvm" / "versions" / "node" / "v25.2.1" / "bin" / "node")
    if not Path(node_bin).exists():
        pytest.skip("no node to syntax-check the page")
    checked = subprocess.run([node_bin, "--check", str(REPO / "app" / "console" / "static" / "pages" / "studio.mjs")], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


# -- the review's findings, each pinned ------------------------------------------------------------------------------------

def _courier(tool_name: str) -> dict:
    code = f'def {tool_name}(tracking_number: str) -> str:\n    """Track a parcel by its number.\n\n    Args:\n        tracking_number: the number\n    """\n    return tracking_number\n'
    return {"graphMode": False, "nodes": [
        {"id": "in", "type": "input", "x": 0, "y": 0, "data": {"sample": "where is my parcel 123?"}},
        {"id": "a1", "type": "agent", "x": 1, "y": 0, "data": {"label": "Courier", "name": "courier", "systemPrompt": "You track parcels."}},
        {"id": "c1", "type": "custom-tool", "x": 1, "y": 1, "data": {"label": "parcel tool", "code": code}},
        {"id": "out", "type": "output", "x": 2, "y": 0, "data": {}}],
        "edges": [{"source": "in", "sourcePort": "out", "target": "a1", "targetPort": "in"},
                  {"source": "c1", "sourcePort": "tool", "target": "a1", "targetPort": "tools"},
                  {"source": "a1", "sourcePort": "out", "target": "out", "targetPort": "in"}]}


def test_a_custom_tool_may_not_take_a_name_the_generated_build_binds():
    """review L2: build() binds trace, builder and agent_<name> / swarm_<name>: a tool of such a name would be shadowed
    there (the Agent given the hook or the agent instead of the tool), so validation refuses it."""
    for name in ("trace", "builder"):
        assert any(f"函数名 {name} 和生成的代码" in m for m in messages(_courier(name))), name
    assert any("agent_courier 和生成代码里「Courier」的变量同名" in m for m in messages(_courier("agent_courier")))
    swarm = template("swarm")
    member = next(n for n in swarm["nodes"] if n["type"] == "agent")
    swarm_node = next(n for n in swarm["nodes"] if n["type"] == "swarm")
    tool = _courier(f"swarm_{swarm_node['data']['name']}")["nodes"][2]
    swarm["nodes"].append(tool)
    swarm["edges"].append({"source": tool["id"], "sourcePort": "tool", "target": member["id"], "targetPort": "tools"})
    assert any(f"swarm_{swarm_node['data']['name']} 和生成代码里" in m for m in messages(swarm))
    assert messages(_courier("agent_other")) == []  # no node of that name: nothing to shadow
    main = st.generate(_courier("track"))["main.py"]
    compile(main, "main.py", "exec")
    assert "        tools=[track]," in main and "    trace = _Trace()" in main
