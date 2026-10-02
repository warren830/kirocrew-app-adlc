"""engine console.claude_sdk: a Claude Agent SDK agent from a spec, no code — the spec checked (every refusal in Chinese),
the build context generated (a linux/arm64 Dockerfile with Node 22, the pinned Claude Code CLI and a non-root user; a
main.py that compiles; the library's skills as .claude/skills/<name>/SKILL.md; a deterministic zip the deploy module
accepts) and — with claude_agent_sdk, bedrock_agentcore, starlette and boto3 stubbed — answering the runtime contract
(text/plain with the turn's tools, JSON, a conversation that resumes from the session storage, the workspace action,
errors); deployed through the console's deploy pipeline (fake AWS, botocore-checked) as a dockerfile with the scan
gate, session storage, lifecycle and a Retrieve grant; and the deploy page."""
from __future__ import annotations

import asyncio
import base64
import hashlib
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
import uuid
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer.console import claude_sdk as cs  # noqa: E402
from workshop_customizer.console import deploy as dp  # noqa: E402
from workshop_customizer.console import skills_lab as sl  # noqa: E402

_spec = importlib.util.spec_from_file_location("console_deploy_tests_for_claude_sdk", REPO / "tests" / "test_console_deploy.py")
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)  # type: ignore[union-attr]  # its FakeAWS (botocore-checked) and the console server module

ACCOUNT, REGION = T.ACCOUNT, T.REGION
KB = "KBEXAMPLE1"
SKILL_TEXT = (REPO / "app" / "console" / "samples" / "skill-lab" / "loyalty-reply-style" / "SKILL.md").read_text(encoding="utf-8")
SKILLS = {"loyalty-reply-style": {"name": "loyalty-reply-style", "version": "v0001", "text": SKILL_TEXT, "description": "积分客服回复规范"}}
KBS = {KB: {"id": KB, "name": "adlc-console-demo", "type": "MANAGED", "description": "拾光家居积分规则（演示）"}}


def spec(**extra) -> dict:
    return cs.check_spec({**cs.SAMPLE_SPEC, "name": "points_agent", "knowledgeBases": [KB], **extra})


# -- the spec ------------------------------------------------------------------------------------------------------------

def test_a_spec_is_checked_field_by_field_with_reasons_in_chinese():
    good = spec(tools=["Glob", "Read", "Write", "Read"], skills=["loyalty-reply-style", {"name": "loyalty-reply-style"}, "other-skill@v0002"])
    assert good["tools"] == ["Read", "Write", "Glob"]  # Claude Code's order, deduplicated
    assert good["skills"] == [{"name": "loyalty-reply-style", "version": None}, {"name": "other-skill", "version": "v0002"}]
    assert (good["model"], good["fastModel"], good["maxTurns"], good["topK"], good["effort"], good["maxBudgetUsd"]) == (
        cs.DEFAULT_MODEL, cs.DEFAULT_FAST_MODEL, 12, 5, None, None)
    assert spec(fastModel="")["fastModel"] == "" and spec(effort="HIGH", maxBudgetUsd="0.5")["effort"] == "high"
    for body, message in (({"name": "1bad"}, "名称"), ({"name": "harness_x"}, "harness_"), ({"systemPrompt": "  "}, "系统 Prompt 是空的"),
                          ({"systemPrompt": "x" * 20_001}, "最多 20000"), ({"model": "GPT 5!"}, "模型"), ({"tools": ["WebSearch"]}, "没有 WebSearch"),
                          ({"tools": ["Task"]}, "不能用"), ({"tools": "Read"}, "一个列表"), ({"skills": ["Bad_Skill"]}, "技能"),
                          ({"skills": [f"s{i}" for i in range(11)]}, "最多 10"), ({"knowledgeBases": ["short"]}, "知识库"),
                          ({"knowledgeBases": [f"KB{i:08d}" for i in range(6)]}, "最多 5"), ({"maxTurns": 0}, "最多轮数"),
                          ({"maxTurns": 2.5}, "最多轮数"), ({"topK": 21}, "段落数"), ({"effort": "huge"}, "推理强度"), ({"maxBudgetUsd": 1000}, "费用上限"),
                          ({"skills": "loyalty-reply-style"}, "各是一个列表"), ({"knowledgeBases": KB}, "各是一个列表")):
        with pytest.raises(cs.ClaudeSdkError, match=message):
            spec(**body)


# -- the build context ---------------------------------------------------------------------------------------------------

def test_the_build_context_is_an_arm64_image_with_node_the_pinned_cli_and_a_non_root_user():
    files = cs.generate(spec(), skills=SKILLS, kbs=KBS, region=REGION, generated_at="2026-10-02T00:00:00Z")
    assert sorted(files) == [".claude/skills/loyalty-reply-style/SKILL.md", "Dockerfile", "agent_spec.json", "main.py", "requirements.txt"]
    docker = files["Dockerfile"]
    assert "FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim-trixie" in docker  # ECR Public: no Docker Hub limit
    # Node from nodejs.org, checked: NodeSource's package pulled in python3.11, which with Debian 12's perl tripped the CRITICAL gate (live)
    assert f"ARG NODE_SHA256={cs.NODE_SHA256}" in docker and 'sha256sum -c -' in docker and "nodesource" not in docker
    assert f"ARG CLAUDE_CODE_VERSION={cs.CLAUDE_CODE}" in docker
    assert 'npm install --global --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"' in docker and "apt-get upgrade -y" in docker
    assert "useradd --uid 1001" in docker and "USER agent" in docker and docker.index("USER agent") < docker.index("CMD")
    assert "CLAUDE_CODE_USE_BEDROCK=1" in docker and "EXPOSE 8080" in docker and 'CMD ["python", "main.py"]' in docker
    assert ">= (1, 43, 90)" in docker and "_bundled' / 'claude').unlink" in docker  # boto3 checked at build time; one copy of the CLI
    assert 'python -c "import docx, matplotlib, openpyxl, PIL, pptx, pypdf"' in docker  # the readers of a turn's files and matplotlib, checked at build time
    assert files["requirements.txt"].splitlines()[1:] == [cs.SDK, cs.AGENTCORE, "boto3>=1.43.90,<2", "botocore>=1.43.90,<2", "openpyxl==3.1.5",
                                                          "pypdf==6.19.0", "python-docx==1.2.0", "python-pptx==1.0.2", "pillow==12.3.0", "matplotlib==3.11.2"]
    # charts: a font with Chinese glyphs, matplotlib's config and font cache in the image, built as the agent's user
    assert "ca-certificates curl xz-utils fonts-wqy-microhei" in docker and "MPLBACKEND=Agg" in docker and "MPLCONFIGDIR=/app/.matplotlib" in docker
    assert "font.sans-serif: WenQuanYi Micro Hei, DejaVu Sans" in docker and docker.index("USER agent") < docker.index('"$MPLCONFIGDIR/matplotlibrc"')
    assert "assert 'WenQuanYi Micro Hei' in {f.name for f in fm.fontManager.ttflist}" in docker
    assert files[".claude/skills/loyalty-reply-style/SKILL.md"] == SKILL_TEXT  # as the library keeps it
    record = json.loads(files["agent_spec.json"])
    assert record["format"] == cs.FORMAT and record["spec"]["name"] == "points_agent" and record["skills"] == {"loyalty-reply-style": {"version": "v0001"}}
    assert record["knowledgeBases"][KB]["type"] == "MANAGED" and record["versions"] == {"sdk": cs.SDK, "cli": cs.CLAUDE_CODE, "base": cs.BASE_IMAGE}
    compile(files["main.py"], "main.py", "exec")
    assert 'MODEL = "us.anthropic.claude-sonnet-5-5"' in files["main.py"] and "## 知识库" in files["main.py"] and "kb_id KBEXAMPLE1" in files["main.py"]
    data = cs.bundle(files)
    assert data == cs.bundle(dict(files)) and zipfile.ZipFile(io.BytesIO(data)).namelist() == sorted(files)  # the same spec, the same zip
    facts = dp.inspect_archive(data, source="dockerfile")
    assert facts["dockerfile"] and facts["files"] == 5 and not facts["rebuilt"]
    tricky = cs.generate(spec(systemPrompt='答 __MODEL__ 和 __REGION__ 时别替换；"""三引号"""\\n', skills=[], knowledgeBases=[]), region=REGION)
    namespace: dict = {}
    exec(compile(tricky["main.py"].split("\nimport asyncio", 1)[0] + "\n", "head.py", "exec"), namespace)  # just the docstring
    assert "__MODEL__ 和 __REGION__" in tricky["main.py"] and 'MODEL = "us.anthropic.claude-sonnet-5-5"' in tricky["main.py"]
    with pytest.raises(cs.ClaudeSdkError, match="SKILL.md"):
        cs.generate(spec(), skills={}, kbs=KBS)


# -- the runtime contract, on stubs ------------------------------------------------------------------------------------------

class Script:
    """What the fake Claude Code does: which tools a turn calls, whether it fails, the files it writes, and what it saw."""

    def __init__(self):
        self.options: list = []
        self.calls = ["kb", "skill"]
        self.skill = "loyalty-reply-style"
        self.skill_args = ""
        self.fail: str | None = None
        self.result_subtype = "success"
        self.retrieved: list[dict] = []
        self.produce: dict[str, bytes] = {}  # files the turn writes into its working directory
        self.seen_skills: list[dict[str, str]] = []  # per turn: the SKILL.md texts in its config dir
        self.s3: dict[str, bytes] = {}  # the console bucket's objects, by key
        self.s3_error: str | None = None
        self.s3_calls: list[tuple] = []
        self.prompts: list[str] = []  # what Claude Code was asked, per turn
        self.found: list[dict[str, bytes]] = []  # per turn: the files in its working directory when it began


class FakeS3:
    def __init__(self, script: Script):
        self.script = script

    def list_objects_v2(self, Bucket, Prefix, **kw):
        self.script.s3_calls.append(("list", Bucket, Prefix))
        if self.script.s3_error:
            raise RuntimeError(self.script.s3_error)
        return {"Contents": [{"Key": k, "Size": len(v)} for k, v in sorted(self.script.s3.items()) if k.startswith(Prefix)], "IsTruncated": False}

    def get_object(self, Bucket, Key):
        self.script.s3_calls.append(("get", Bucket, Key))
        if self.script.s3_error:
            raise RuntimeError(self.script.s3_error)
        if Key not in self.script.s3:
            raise RuntimeError(f"An error occurred (NoSuchKey) when calling the GetObject operation: {Key}")
        return {"Body": io.BytesIO(self.script.s3[Key])}


def file_readers() -> dict[str, types.ModuleType]:
    """openpyxl, python-docx and pypdf as far as the template uses them, reading a JSON description of the document
    (the fake agent writes its "xlsx" as JSON): the real libraries are in the image, not in the test environment."""
    def described(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    class Cell:
        def __init__(self, value, bold=False):
            self.value, self.font = value, types.SimpleNamespace(b=bold)

    class Sheet:
        def __init__(self, spec, data_only):
            self.title, self.freeze_panes = spec["title"], spec.get("freeze")
            self.rows = [[(spec.get("cached") or {}).get(f"{r}:{c}") if data_only and isinstance(v, str) and v.startswith("=") else v
                          for c, v in enumerate(row, 1)] for r, row in enumerate(spec["rows"], 1)]
            self.bold_header = spec.get("boldHeader", False)
            self.max_row, self.max_column = max(len(self.rows), 1), max([len(r) for r in self.rows] + [1])
            self.merged_cells = types.SimpleNamespace(ranges=list(spec.get("merged") or []))

        def cell(self, r, c):
            row = self.rows[r - 1] if r <= len(self.rows) else []
            return Cell(row[c - 1] if c <= len(row) else None, bold=self.bold_header and r == 1)

    class Book:
        def __init__(self, spec, data_only):
            self.worksheets = [Sheet(s, data_only) for s in spec["sheets"]]
            self.sheetnames = [s.title for s in self.worksheets]

        def __getitem__(self, title):
            return next(s for s in self.worksheets if s.title == title)

    openpyxl = types.ModuleType("openpyxl")
    openpyxl.load_workbook = lambda path, data_only=False: Book(described(path), data_only)

    class Paragraph:
        def __init__(self, text, style):
            self.text, self.style = text, types.SimpleNamespace(name=style)

    class Table:
        def __init__(self, rows):
            self.rows = [types.SimpleNamespace(cells=[types.SimpleNamespace(text=t) for t in row]) for row in rows]

    class Document:
        def __init__(self, path):
            self.blocks = [Table(b["table"]) if "table" in b else Paragraph(b["text"], b.get("style", "Normal")) for b in described(path)["blocks"]]

        def iter_inner_content(self):
            return iter(self.blocks)

    docx = types.ModuleType("docx")
    docx.Document = Document

    class PdfReader:
        def __init__(self, path):
            self.pages = [types.SimpleNamespace(extract_text=lambda t=t: t) for t in described(path)["pages"]]
            self.is_encrypted = False

    pypdf = types.ModuleType("pypdf")
    pypdf.PdfReader = PdfReader
    return {"openpyxl": openpyxl, "docx": docx, "pypdf": pypdf, **fake_pptx(), **fake_pillow()}


SIGNATURES = {"PNG": b"\x89PNG\r\n\x1a\n", "JPEG": b"\xff\xd8\xff\xe0"}


def picture(width: int, height: int, fmt: str = "PNG", **extra) -> bytes:
    """An image as the fake Pillow reads it: the format's signature, then its size (and EXIF orientation) as JSON."""
    return SIGNATURES[fmt] + json.dumps({"w": width, "h": height, **extra}, separators=(",", ":")).encode()


def fake_pillow() -> dict[str, types.ModuleType]:
    """Pillow as far as the template uses it: an image is its format and size; saved as PNG it takes a byte a pixel,
    as JPEG a tenth of that (so a big image falls back to JPEG, as a photo would)."""
    class Picture:
        def __init__(self, fmt, size, mode="RGB", orientation=1):
            self.format, self.size, self.mode, self.info, self.n_frames, self.orientation = fmt, tuple(size), mode, {}, 1, orientation

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def load(self):
            return None

        def getexif(self):
            return {0x0112: self.orientation}

        def copy(self):
            return Picture(self.format, self.size, self.mode)

        def thumbnail(self, box):
            scale = min(1.0, box[0] / self.size[0], box[1] / self.size[1])
            self.size = (max(1, round(self.size[0] * scale)), max(1, round(self.size[1] * scale)))

        def convert(self, mode):
            return Picture(self.format, self.size, mode)

        def getchannel(self, name):
            return self

        def paste(self, other, mask=None):
            return None

        def resize(self, size):
            return Picture(self.format, size, self.mode)

        def save(self, buffer, format, **kw):
            body = picture(*self.size, fmt=format)
            buffer.write(body + b" " * max(0, (self.size[0] * self.size[1] // (1 if format == "PNG" else 10)) - len(body)))

    def open_(stream):
        data = stream.read()
        for fmt, signature in SIGNATURES.items():
            if data.startswith(signature):
                try:
                    meta = json.loads(data[len(signature):].split(b" ")[0])
                except ValueError as exc:
                    raise OSError("cannot identify image file") from exc
                return Picture(fmt, (meta["w"], meta["h"]), orientation=meta.get("o", 1))
        raise OSError("cannot identify image file")

    image = types.ModuleType("PIL.Image")
    image.open, image.new, image.MAX_IMAGE_PIXELS = open_, (lambda mode, size, color=None: Picture("PNG", size, mode)), None
    ops = types.ModuleType("PIL.ImageOps")
    ops.exif_transpose = lambda im: Picture(im.format, (im.size[1], im.size[0]), im.mode)  # orientation 6: a quarter turn
    pil = types.ModuleType("PIL")
    pil.Image, pil.ImageOps = image, ops
    return {"PIL": pil, "PIL.Image": image, "PIL.ImageOps": ops}


def fake_pptx() -> dict[str, types.ModuleType]:
    """python-pptx as far as the template uses it, reading a JSON description of the deck (see described)."""
    kinds = types.SimpleNamespace(PICTURE=13, GROUP=6, AUTO_SHAPE=1)

    def shape(spec, sid):
        out = types.SimpleNamespace(shape_id=sid, name=spec.get("name", f"Shape {sid}"), has_text_frame="text" in spec, has_table="table" in spec,
                                    has_chart="chart" in spec, shape_type=kinds.PICTURE if "picture" in spec else kinds.GROUP if "group" in spec else kinds.AUTO_SHAPE)
        if "text" in spec:
            out.text_frame = types.SimpleNamespace(paragraphs=[types.SimpleNamespace(text=p[0], level=p[1]) for p in spec["text"]],
                                                   text="\n".join(p[0] for p in spec["text"]))
        if "table" in spec:
            out.table = types.SimpleNamespace(rows=[types.SimpleNamespace(cells=[types.SimpleNamespace(text=c) for c in row]) for row in spec["table"]])
        if "chart" in spec:
            c = spec["chart"]
            out.chart = types.SimpleNamespace(chart_type=f"{c['type']} (57)", has_title=bool(c.get("title")),
                                              chart_title=types.SimpleNamespace(has_text_frame=True, text_frame=types.SimpleNamespace(text=c.get("title", ""))),
                                              plots=[types.SimpleNamespace(categories=c["categories"], series=[types.SimpleNamespace(name=n, values=v) for n, v in c["series"]])])
        if "picture" in spec:
            blob = base64.b64decode(spec["picture"])
            meta = json.loads(blob[8:].split(b" ")[0])
            out.image = types.SimpleNamespace(blob=blob, content_type="image/png", size=(meta["w"], meta["h"]))
        if "group" in spec:
            out.shapes = [shape(s, sid * 10 + i) for i, s in enumerate(spec["group"], 1)]
        return out

    class Shapes(list):
        title = None

    def slide(spec):
        shapes = Shapes()
        if spec.get("title"):
            shapes.title = shape({"text": [[spec["title"], 0]], "name": "Title 1"}, 1)
            shapes.append(shapes.title)
        shapes.extend(shape(s, i) for i, s in enumerate(spec.get("shapes") or [], 2))
        notes = types.SimpleNamespace(notes_text_frame=types.SimpleNamespace(text=spec.get("notes", "")))
        return types.SimpleNamespace(shapes=shapes, has_notes_slide="notes" in spec, notes_slide=notes)

    class Presentation:
        def __init__(self, path):
            self.slides = [slide(s) for s in json.loads(Path(path).read_text(encoding="utf-8"))["slides"]]

    pptx = types.ModuleType("pptx")
    pptx.Presentation = Presentation
    enum = types.ModuleType("pptx.enum")
    shapes_mod = types.ModuleType("pptx.enum.shapes")
    shapes_mod.MSO_SHAPE_TYPE = kinds
    return {"pptx": pptx, "pptx.enum": enum, "pptx.enum.shapes": shapes_mod}


def stub_modules(script: Script) -> dict[str, types.ModuleType]:
    mods = {name: types.ModuleType(name) for name in ("claude_agent_sdk", "bedrock_agentcore", "bedrock_agentcore.runtime", "starlette",
                                                      "starlette.responses", "boto3")}

    class Block:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class TextBlock(Block):
        pass

    class ToolUseBlock(Block):
        pass

    class ToolResultBlock(Block):
        pass

    class AssistantMessage(Block):
        pass

    class UserMessage(Block):
        pass

    class ResultMessage(Block):
        pass

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def tool(name, description, schema):
        def wrap(fn):
            return types.SimpleNamespace(name=name, description=description, input_schema=schema, handler=fn)
        return wrap

    def create_sdk_mcp_server(name, tools=None, version="1.0.0"):
        return {"type": "sdk", "name": name, "tools": list(tools or [])}

    async def query(prompt, options):
        kw = options.kw
        script.options.append(kw)
        script.prompts.append(prompt)
        cwd = Path(kw["cwd"])
        script.found.append({p.relative_to(cwd).as_posix(): p.read_bytes() for p in sorted(cwd.rglob("*"))
                             if p.is_file() and not p.relative_to(cwd).as_posix().startswith((".", "calls/"))})
        config = Path(kw["env"]["CLAUDE_CONFIG_DIR"])
        script.seen_skills.append({p.parent.name: p.read_text(encoding="utf-8") for p in sorted((config / "skills").glob("*/SKILL.md"))})
        sid = kw.get("resume") or kw.get("session_id")
        transcript = config / "projects" / "-workspace" / f"{sid}.jsonl"
        if kw.get("resume") and not transcript.exists():
            raise RuntimeError(f"Claude Code returned an error result: No conversation found with session ID: {sid}")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        with transcript.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"prompt": prompt}, ensure_ascii=False) + "\n")
        turns = len(transcript.read_text(encoding="utf-8").splitlines())
        if "kb" in script.calls:
            yield AssistantMessage(content=[ToolUseBlock(id="t1", name="mcp__kb__retrieve", input={"query": prompt})], parent_tool_use_id=None)
            retrieve = kw["mcp_servers"]["kb"]["tools"][0]
            out = await retrieve.handler({"query": prompt})
            yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=out["content"], is_error=out.get("is_error", False))])
        if "skill" in script.calls:
            ask = {"skill": script.skill, **({"args": script.skill_args} if script.skill_args else {})}
            yield AssistantMessage(content=[ToolUseBlock(id="t2", name="Skill", input=ask)], parent_tool_use_id=None)
            yield UserMessage(content=[ToolResultBlock(tool_use_id="t2", content=f"Launching skill: {script.skill}", is_error=False)])
        for rel, data in script.produce.items():  # the turn's files, in its working directory
            (Path(kw["cwd"]) / rel).parent.mkdir(parents=True, exist_ok=True)
            (Path(kw["cwd"]) / rel).write_bytes(data)
        if "bash" in script.calls:
            yield AssistantMessage(content=[ToolUseBlock(id="t3", name="Bash", input={"command": "rm -rf /"})], parent_tool_use_id=None)
            yield UserMessage(content=[ToolResultBlock(tool_use_id="t3", content="Permission denied", is_error=True)])
        if script.fail:
            raise RuntimeError(script.fail)
        answer = f"第 {turns} 轮：{prompt}"
        yield AssistantMessage(content=[TextBlock(text=answer)], parent_tool_use_id=None)
        yield ResultMessage(subtype=script.result_subtype, is_error=script.result_subtype != "success", num_turns=3, session_id=sid, total_cost_usd=0.0123,
                            usage={"input_tokens": 100, "output_tokens": 20}, result=answer if script.result_subtype == "success" else None,
                            stop_reason="end_turn", errors=[])

    class BedrockAgentCoreApp:
        def __init__(self):
            self.handlers = {}

        def entrypoint(self, fn):
            self.handlers["main"] = fn
            return fn

        def run(self, **kw):  # pragma: no cover - never in tests
            raise AssertionError("app.run() in a test")

    class Response:
        def __init__(self, content=None, status_code=200, media_type=None):
            self.body, self.status_code, self.media_type = content, status_code, media_type

    class Retrieve:
        def retrieve(self, **kw):
            script.retrieved.append(kw)
            return {"retrievalResults": [{"content": {"text": "推荐奖励在首单经过 7天无理由退货期 后发放 500 积分"}, "score": 0.71,
                                          "location": {"s3Location": {"uri": "s3://b/kb/KBEXAMPLE1/referral-policy.md"}, "type": "S3"}}]}

    for name, value in (("claude_agent_sdk", {"AssistantMessage": AssistantMessage, "ClaudeAgentOptions": ClaudeAgentOptions, "ResultMessage": ResultMessage,
                                              "TextBlock": TextBlock, "ToolResultBlock": ToolResultBlock, "ToolUseBlock": ToolUseBlock, "UserMessage": UserMessage,
                                              "create_sdk_mcp_server": create_sdk_mcp_server, "query": query, "tool": tool}),
                        ("bedrock_agentcore.runtime", {"BedrockAgentCoreApp": BedrockAgentCoreApp}), ("starlette.responses", {"Response": Response}),
                        ("boto3", {"client": lambda name, region_name=None: FakeS3(script) if name == "s3" else Retrieve()})):
        mods[name].__dict__.update(value)
    return {**mods, **file_readers()}


@pytest.fixture()
def agent(monkeypatch, tmp_path):
    """The generated main.py on the stubs, in a folder laid out as the image is: ``(namespace, script, workspace)``."""
    script = Script()
    for name, module in stub_modules(script).items():
        monkeypatch.setitem(sys.modules, name, module)
    workspace = tmp_path / "mnt" / "workspace"
    monkeypatch.setenv("AGENT_WORKSPACE", str(workspace))
    monkeypatch.setenv("AWS_REGION", REGION)

    def load(the_spec=None):
        files = cs.generate(the_spec or spec(), skills=SKILLS, kbs=KBS, region=REGION)
        app = tmp_path / "app"
        for rel, text in files.items():
            (app / rel).parent.mkdir(parents=True, exist_ok=True)
            (app / rel).write_text(text, encoding="utf-8")
        namespace = {"__name__": "claude_generated", "__file__": str(app / "main.py")}
        exec(compile(files["main.py"], str(app / "main.py"), "exec"), namespace)
        return namespace
    return load, script, workspace


def ask(ns: dict, payload: dict, session: str = "console-" + "a" * 40):
    return asyncio.run(ns["invoke"](payload, types.SimpleNamespace(session_id=session)))


def test_the_agent_answers_with_its_kb_tool_and_skill_and_offers_nothing_else(agent):
    load, script, workspace = agent
    ns = load()
    reply = ask(ns, {"prompt": "推荐积分什么时候到？", "actorId": "ann"})
    assert reply.status_code == 200 and reply.media_type == "text/plain; charset=utf-8"
    assert reply.body == "第 1 轮：推荐积分什么时候到？\n\n〔工具〕retrieve，Skill(loyalty-reply-style)"
    [opts] = script.options
    assert opts["tools"] == ["Read", "Write", "Glob", "Skill"] and opts["allowed_tools"] == ["Read", "Write", "Glob", "mcp__kb__retrieve"]
    assert opts["skills"] == ["loyalty-reply-style"] and opts["permission_mode"] == "dontAsk" and opts["strict_mcp_config"] and opts["verbatim_prompts"]
    assert opts["model"] == cs.DEFAULT_MODEL and opts["max_turns"] == 12 and "## 知识库" in opts["system_prompt"]
    assert opts["cwd"] == str(workspace) and opts["session_id"] == str(uuid.uuid5(uuid.UUID("6f1c0a52-3d0e-4b8f-9a77-0c5d2b1e9a41"), "console-" + "a" * 40))
    env = opts["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(workspace / ".claude-agent") and env["CLAUDE_CODE_USE_BEDROCK"] == "1" and env["AWS_REGION"] == REGION
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == cs.DEFAULT_FAST_MODEL and env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert (workspace / ".claude-agent" / "skills" / "loyalty-reply-style" / "SKILL.md").read_text(encoding="utf-8") == SKILL_TEXT  # copied in
    [call] = script.retrieved
    assert call == {"knowledgeBaseId": KB, "retrievalQuery": {"text": "推荐积分什么时候到？"},
                    "retrievalConfiguration": {"managedSearchConfiguration": {"numberOfResults": 5}}}
    assert ns["_retrieve_text"]("x", "").startswith("【adlc-console-demo】\n[1] 出处 referral-policy.md，相关度 0.71")
    bare = load(spec(tools=[], skills=[], knowledgeBases=[]))  # nothing but the model
    script.calls = []
    assert ask(bare, {"prompt": "你好"}, session="console-" + "b" * 40).body.endswith("〔这一轮没有调用工具〕")
    opts = script.options[-1]
    assert opts["tools"] == [] and opts["allowed_tools"] == [] and opts["skills"] == [] and opts["mcp_servers"] == {}


def test_a_conversation_resumes_from_the_workspace_even_in_a_new_process(agent):
    load, script, workspace = agent
    ns = load()
    first = ask(ns, {"prompt": "记下会员号 M1001"})
    second = ask(ns, {"prompt": "我的会员号？", "format": "json"})
    assert first.body.startswith("第 1 轮") and second.media_type == "application/json"
    out = json.loads(second.body)
    assert out["result"] == "第 2 轮：我的会员号？" and out["conversation"]["resumed"] and out["skills"] == ["loyalty-reply-style"]
    assert [t["tool"] for t in out["tools"]] == ["mcp__kb__retrieve", "Skill"] and out["turns"] == 3 and out["costUsd"] == 0.0123
    assert out["workspace"] == {"path": str(workspace), "sessionStorage": True}
    assert script.options[1]["resume"] == script.options[0]["session_id"] and "session_id" not in script.options[1]
    again = load()  # a stopped session's new microVM: the same session storage, a new process
    third = json.loads(ask(again, {"prompt": "还记得吗", "format": "json"}).body)
    assert third["result"].startswith("第 3 轮") and third["conversation"] == out["conversation"]
    (workspace / "notes.md").write_text("会员号 M1001", encoding="utf-8")
    listing = json.loads(ask(again, {"action": "workspace"}).body)
    assert listing["files"] == [{"path": "notes.md", "bytes": len("会员号 M1001".encode()), "modified": listing["files"][0]["modified"]}]
    assert listing["sessionStorage"] and listing["conversation"] == {"id": out["conversation"]["id"], "transcript": True}
    for path in (workspace / ".claude-agent" / "projects").rglob("*.jsonl"):  # session storage reset (a new version): a new conversation
        path.unlink()
    fourth = json.loads(ask(again, {"prompt": "重新开始", "format": "json"}).body)
    assert fourth["result"].startswith("第 1 轮") and not fourth["conversation"]["resumed"]


def test_failures_answer_with_a_status_and_one_findable_log_line(agent, monkeypatch):
    import logging

    load, script, _workspace = agent
    ns = load()
    records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    logging.getLogger("claude_agent").addHandler(handler)
    try:
        assert ask(ns, {"prompt": "  "}).status_code == 400 and ask(ns, {"prompt": "x" * 20_001}).status_code == 400
        script.fail = "AccessDeniedException: bedrock:InvokeModelWithResponseStream"
        failed = ask(ns, {"prompt": "hi"})
    finally:
        logging.getLogger("claude_agent").removeHandler(handler)
    assert failed.status_code == 500 and "AccessDeniedException" in json.loads(failed.body)["error"]
    [line] = [r for r in records if r.startswith("turn failed")]  # AgentCore drops a 500's body: the log says why
    assert "AccessDeniedException" in line and '"session": "console-aaaaaaaa' in line
    script.fail, script.calls = None, ["bash"]
    denied = ask(ns, {"prompt": "清理一下"})
    assert denied.body.endswith("〔工具〕Bash（失败）")  # dontAsk refused it; the model was told so
    script.calls, script.result_subtype = [], "error_max_turns"
    partial = ask(ns, {"prompt": "很长的任务"})
    assert partial.status_code == 200 and "〔用完了最多 12 轮，回答可能不完整〕" in partial.body
    no_storage = load()
    no_storage["_SETUP"].clear()
    monkeypatch.delenv("AGENT_WORKSPACE")  # no session storage: the image's own workspace
    script.result_subtype = "success"
    out = json.loads(ask(no_storage, {"prompt": "hi", "format": "json"}, session="console-" + "c" * 40).body)
    assert out["workspace"]["sessionStorage"] is False and out["workspace"]["path"].endswith("/app/workspace")


POINTS = "points-report-xlsx"
POINTS_TEXT = (REPO / "app" / "console" / "samples" / "skill-lab" / POINTS / "SKILL.md").read_text(encoding="utf-8")


def described(**doc) -> bytes:
    """A document as the fake readers take it (see file_readers)."""
    return json.dumps(doc, ensure_ascii=False).encode("utf-8")


def test_a_call_with_its_own_skills_runs_apart_and_answers_with_the_files_it_made(agent):
    load, script, workspace = agent
    ns = load(spec(knowledgeBases=[], tools=["Read", "Write", "Bash"]))
    candidate = POINTS_TEXT.replace("只用一句话", "用一句话（候选版本）")
    uri = f"s3://bucket-a/skills/{POINTS}/v0002/"
    script.s3 = {f"skills/{POINTS}/v0002/SKILL.md": candidate.encode(), f"skills/{POINTS}/v0002/templates/header.txt": b"hdr",
                 f"skills/{POINTS}/v0002x/SKILL.md": b"another prefix: never read"}
    script.calls, script.skill, script.skill_args = ["skill"], POINTS, "数据：" + "M1001 本期 800 到期 0；" * 20  # an input _brief cuts short
    rows = [["会员号", "本期积分", "到期积分"], ["M1001", 800, 0], ["00031", 1500, 0], ["合计", "=SUM(B2:B3)", "=SUM(C2:C3)"]]
    xlsx = described(sheets=[{"title": "积分明细", "rows": rows, "freeze": "A2", "boldHeader": True, "cached": {"4:2": 2300}}])
    script.produce = {"points-report.xlsx": xlsx, "notes/summary.md": "# 积分\n合计 2300".encode()}
    body = {"prompt": "做积分报表", "format": "json", "skills": ["loyalty-reply-style", POINTS], "skillOverrides": [{"name": POINTS, "uri": uri}]}
    out = json.loads(ask(ns, body).body)
    opts, call_dir = script.options[-1], workspace / out["call"]["workdir"]
    assert out["call"]["workdir"].startswith("calls/") and opts["cwd"] == str(call_dir) and call_dir.is_dir()  # its own directory
    assert opts["setting_sources"] == ["user"] and opts["skills"] == ["loyalty-reply-style", POINTS] and opts["tools"] == ["Read", "Write", "Bash", "Skill"]
    config = Path(opts["env"]["CLAUDE_CONFIG_DIR"])
    assert config == workspace / ".claude-agent" / "calls" / out["call"]["id"]  # and its own config: its skills are exactly the call's
    assert script.seen_skills[-1] == {"loyalty-reply-style": SKILL_TEXT, POINTS: candidate}
    assert (config / "skills" / POINTS / "templates" / "header.txt").read_bytes() == b"hdr"  # the skill's folder, not just SKILL.md
    assert out["call"]["skills"] == [{"name": "loyalty-reply-style", "source": "image", "version": "v0001"}, {"name": POINTS, "source": uri, "files": 2}]
    assert out["skills"] == out["skillsLoaded"] == [POINTS] and isinstance(out["tools"][0]["input"], str)  # the name survives a long input
    assert not out["conversation"]["resumed"] and "session_id" in opts and out["format"] == cs.FORMAT == "adlc-claude-agent/3"
    assert out["call"]["inputs"] == [] and script.prompts[-1] == "做积分报表"  # no input files: the prompt as it was
    md, sheet = out["artifacts"]  # by path
    assert (md["path"], md["type"], md["status"], md["text"]) == ("notes/summary.md", "md", "created", "# 积分\n合计 2300")
    assert (sheet["path"], sheet["type"], sheet["bytes"], sheet["sheets"]) == ("points-report.xlsx", "xlsx", len(xlsx), 1)
    assert base64.b64decode(sheet["base64"]) == xlsx and sheet["sha256"] == hashlib.sha256(xlsx).hexdigest()
    lines = sheet["text"].splitlines()
    assert lines[0] == "[工作表 1：积分明细] 4 行 × 3 列 · 冻结窗格 A2 · 首行加粗" and lines[1] == "    | A | B | C"
    assert lines[2:] == ["  1 | 会员号 | 本期积分 | 到期积分", "  2 | M1001 | 800 | 0", '  3 | "00031" | 1500 | 0', "  4 | 合计 | =SUM(B2:B3) → 2300 | =SUM(C2:C3)"]  # text kept text, formulas as written
    assert sheet["table"]["sheets"][0] == {"name": "积分明细", "rows": [["会员号", "本期积分", "到期积分"], ["M1001", "800", "0"], ['"00031"', "1500", "0"],
                                                                       ["合计", "=SUM(B2:B3) → 2300", "=SUM(C2:C3)"]], "nrows": 4, "ncols": 3, "truncated": False}
    assert [c for c in script.s3_calls if c[0] == "get"] == [("get", "bucket-a", f"skills/{POINTS}/v0002/SKILL.md"),
                                                             ("get", "bucket-a", f"skills/{POINTS}/v0002/templates/header.txt")]
    # the arm without the skill: no Skill tool, no skill in its config, the files it made all the same
    script.calls, script.produce = [], {"report.csv": "会员编号,积分\nM1001,800\n".encode("utf-8-sig")}
    bare = json.loads(ask(ns, {"prompt": "做积分报表", "format": "json", "skills": []}, session="console-" + "d" * 40).body)
    opts = script.options[-1]
    assert opts["tools"] == ["Read", "Write", "Bash"] and opts["skills"] == [] and script.seen_skills[-1] == {} and bare["call"]["skills"] == []
    [report] = bare["artifacts"]
    assert report["type"] == "csv" and report["text"].startswith("会员编号,积分") and report["table"]["sheets"][0]["rows"] == [["会员编号", "积分"], ["M1001", "800"]]
    # a conversation's turn still runs in the workspace, resumes nothing it did not start, and does not count the calls' files
    script.produce = {"notes.md": b"M1001"}
    reply = ask(ns, {"prompt": "记一下"})
    assert script.options[-1]["cwd"] == str(workspace) and "setting_sources" not in script.options[-1]
    assert reply.body.endswith("〔这一轮没有调用工具〕\n〔文件〕notes.md（5 B）")
    script.produce = {}
    again = json.loads(ask(ns, {"prompt": "再看看", "format": "json"}).body)
    assert again["conversation"]["resumed"] and again["artifacts"] == [] and "call" not in again  # notes.md is unchanged this turn
    script.produce = {"notes.md": b"M1001 M1002"}
    assert [(a["path"], a["status"]) for a in json.loads(ask(ns, {"prompt": "改一下", "format": "json"}).body)["artifacts"]] == [("notes.md", "changed")]


def test_a_call_that_brings_skills_is_checked_before_it_runs(agent):
    load, script, workspace = agent
    ns = load(spec(knowledgeBases=[]))
    script.calls, script.skill = ["skill"], POINTS
    uri = f"s3://bucket-a/skills/{POINTS}/v0002/"
    script.s3 = {f"skills/{POINTS}/v0002/SKILL.md": POINTS_TEXT.encode(), "skills/other/v0001/SKILL.md": POINTS_TEXT.encode()}
    for extra, status, words in (({"skills": "loyalty-reply-style"}, 400, "a list of skill names"), ({"skills": ["nope"]}, 400, "not one of this agent's skills"),
                                 ({"skills": [], "skillOverrides": [{"name": POINTS, "uri": uri}]}, 400, "not one of this call's skills"),
                                 ({"skillOverrides": [{"name": POINTS, "uri": "https://example.com/x/"}]}, 400, "s3://bucket/prefix/"),
                                 ({"skillOverrides": [{"name": "other", "uri": "s3://bucket-a/skills/other/v0001/"}]}, 400, "names points-report-xlsx, not other"),
                                 ({"skillOverrides": [{"name": POINTS, "uri": f"s3://bucket-a/skills/{POINTS}/v0009/"}]}, 400, "no SKILL.md"),
                                 ({"model": "not a model!"}, 400, "model")):
        before = len(script.options)
        reply = ask(ns, {"prompt": "hi", "format": "json", **extra})
        assert (reply.status_code, len(script.options)) == (status, before) and words in json.loads(reply.body)["error"], extra  # Claude Code never ran
    script.s3_error = "AccessDenied: s3:ListBucket"  # a read grant made seconds ago: the caller may try again
    refused = ask(ns, {"prompt": "hi", "format": "json", "skillOverrides": [{"name": POINTS, "uri": uri}]})
    assert refused.status_code == 503 and "AccessDenied" in json.loads(refused.body)["error"]
    script.s3_error = None
    out = json.loads(ask(ns, {"prompt": "hi", "format": "json", "skillOverrides": [{"name": POINTS, "uri": uri}], "model": cs.DEFAULT_FAST_MODEL}).body)
    assert [s["name"] for s in out["call"]["skills"]] == ["loyalty-reply-style", POINTS]  # skills left out: the image's, plus the override
    assert script.options[-1]["model"] == out["model"] == cs.DEFAULT_FAST_MODEL


def test_the_files_a_turn_made_are_read_as_documents_or_said_unreadable(agent):
    load, script, workspace = agent
    ns = load(spec(knowledgeBases=[], skills=[]))
    script.calls = []
    script.produce = {"brief.docx": described(blocks=[{"text": "积分报告", "style": "Title"}, {"text": "本期概况", "style": "Heading 2"},
                                                      {"text": "会员 3 位", "style": "List Bullet"}, {"table": [["会员号", "积分"], ["M1001", "800"]]}]),
                      "brief.pdf": described(pages=["第一页 合计 2300", "第二页"]), "broken.xlsx": b"not a workbook",
                      "logo.png": b"\x89PNG\r\n\x1a\n\x00\x00", "data.bin": b"\x00\x01\x02", "big.txt": ("字" * 20000).encode()}
    out = json.loads(ask(ns, {"prompt": "出文档", "format": "json", "skills": []}).body)
    found = {a["path"]: a for a in out["artifacts"]}
    assert sorted(found) == ["big.txt", "brief.docx", "brief.pdf", "broken.xlsx", "data.bin", "logo.png"]
    assert found["brief.docx"]["text"].splitlines()[:3] == ["# 积分报告", "## 本期概况", "- 会员 3 位"]
    assert found["brief.docx"]["table"]["sheets"][0]["rows"] == [["会员号", "积分"], ["M1001", "800"]]
    assert found["brief.pdf"]["pages"] == 2 and found["brief.pdf"]["text"] == "[第 1 页]\n第一页 合计 2300\n\n[第 2 页]\n第二页"
    assert found["broken.xlsx"]["error"].startswith("JSONDecodeError") and found["broken.xlsx"]["text"] == ""  # said so, the answer goes on
    assert (found["logo.png"]["type"], found["logo.png"]["text"], found["data.bin"]["type"]) == ("image", "", "binary")
    assert found["big.txt"]["truncated"] and len(found["big.txt"]["text"]) == 6000 and "base64" not in found["big.txt"]  # 60 KB: over the inline cap
    assert all("base64" in found[p] for p in ("brief.docx", "brief.pdf", "logo.png"))


def asset(data: bytes) -> tuple[str, str]:
    """A task file's key in the console bucket and its SHA-256."""
    digest = hashlib.sha256(data).hexdigest()
    return f"skill-lab/assets/{digest}", digest


def test_a_call_with_input_files_finds_them_in_its_directory_and_they_are_not_its_files(agent, monkeypatch):
    load, script, workspace = agent
    ns = load(spec(knowledgeBases=[], skills=[], tools=["Read", "Write", "Bash"]))
    script.calls = []
    members = "会员号,本期积分\nM1,10\n".encode("utf-8")
    book = described(sheets=[{"title": "积分", "rows": [["会员号", "本期积分"], ["0042", 650]]}])
    (k1, s1), (k2, s2) = asset(members), asset(book)
    script.s3 = {k1: members, k2: book}
    files = [{"path": "members.csv", "uri": f"s3://bucket-a/{k1}", "sha256": s1, "bytes": len(members)},
             {"path": "data/book.xlsx", "uri": f"s3://bucket-a/{k2}", "sha256": s2, "bytes": len(book)}]
    script.produce = {"points-chart.png": picture(800, 450)}
    out = json.loads(ask(ns, {"prompt": "画积分图", "format": "json", "skills": [], "files": files}).body)
    assert script.found[-1] == {"data/book.xlsx": book, "members.csv": members}  # in its directory before Claude Code began
    assert script.prompts[-1] == f"画积分图\n\n〔输入文件〕这次任务的文件已经放在当前工作目录里：members.csv（{len(members)} B）、data/book.xlsx（{len(book)} B）。"
    assert out["call"]["inputs"] == [{"path": "members.csv", "bytes": len(members), "sha256": s1}, {"path": "data/book.xlsx", "bytes": len(book), "sha256": s2}]
    assert [a["path"] for a in out["artifacts"]] == ["points-chart.png"]  # the inputs it left alone are not its files
    assert (workspace / out["call"]["workdir"] / "data" / "book.xlsx").read_bytes() == book
    assert [c for c in script.s3_calls if c[0] == "get"] == [("get", "bucket-a", k1), ("get", "bucket-a", k2)]
    script.produce = {"members.csv": "会员号,本期积分\nM1,99\n".encode("utf-8")}  # a turn that rewrites an input: it is a file it changed
    again = json.loads(ask(ns, {"prompt": "改一下", "format": "json", "files": files[:1]}, session="console-" + "e" * 40).body)
    assert [(a["path"], a["status"]) for a in again["artifacts"]] == [("members.csv", "changed")]
    assert again["call"]["skills"] == [] and script.options[-1]["setting_sources"] == ["user"]  # files alone make a call of its own (the image's skills: none)
    bad = members.replace(b"10", b"11")
    for extra, status, words in (([{**files[0], "sha256": "0" * 64}], 400, "is not the file the call names"),
                                 ([{**files[0], "bytes": len(members) + 1}], 400, "is not the file the call names"),
                                 ([{**files[0], "path": "../members.csv"}], 400, "not a plain relative path"),
                                 ([{**files[0], "path": ".claude/skills/x/SKILL.md"}], 400, "no hidden names"),
                                 ([{**files[0], "path": "a/b/c/d/e.csv"}], 400, "at most 4 levels"),
                                 ([files[0], {**files[1], "path": "MEMBERS.csv"}], 400, "named twice"),
                                 ([{**files[0], "uri": "https://example.com/members.csv"}], 400, "s3://bucket/key"),
                                 ([{**files[0], "uri": "s3://bucket-a/skill-lab/assets/../skills/x"}], 400, "s3://bucket/key"),
                                 ([{**files[0], "uri": "s3://bucket-a/skill-lab/assets/missing"}], 400, "does not exist"),
                                 ([{**files[0], "bytes": True}], 400, "bytes is a size"), ("members.csv", 400, "a list of"),
                                 ([files[0]] * 33, 400, "at most 32")):
        before = len(script.options)
        reply = ask(ns, {"prompt": "hi", "format": "json", "files": extra})
        assert (reply.status_code, len(script.options)) == (status, before) and words in json.loads(reply.body)["error"], extra  # Claude Code never ran
    script.s3[k1] = bad  # the object is not what the call names: refused, not answered
    assert ask(ns, {"prompt": "hi", "format": "json", "files": files[:1]}).status_code == 400
    script.s3[k1], script.s3_error = members, "AccessDenied: s3:GetObject"  # a read grant made seconds ago: the caller may try again
    refused = ask(ns, {"prompt": "hi", "format": "json", "files": files[:1]})
    assert refused.status_code == 503 and "AccessDenied" in json.loads(refused.body)["error"]
    script.s3_error = None
    ns["MAX_INPUT_BYTES"] = len(members)  # in all, as the call declares them
    assert "in all" in json.loads(ask(ns, {"prompt": "hi", "format": "json", "files": files}).body)["error"]


def test_pictures_and_a_presentation_are_viewed_for_the_judge(agent, monkeypatch):
    load, script, workspace = agent
    ns = load(spec(knowledgeBases=[], skills=[]))
    script.calls = []
    chart = picture(800, 450)
    deck = described(slides=[{"title": "本期积分概况", "shapes": [{"text": [["共 3 位会员", 0], ["M2001 最高", 1]]}], "notes": "先说结论"},
                             {"title": "明细", "shapes": [{"table": [["会员号", "积分"], ["M1", "10"]]},
                                                         {"chart": {"type": "BAR_CLUSTERED", "title": "积分图", "categories": ["M1", "M2"], "series": [["本期积分", [10.0, 400.0]]]}},
                                                         {"group": [{"picture": base64.b64encode(chart).decode(), "name": "Picture 4"}]}]}])
    script.produce = {"chart.png": chart, "big.png": picture(4000, 3000), "photo.jpg": picture(3000, 2000, "JPEG"), "turned.jpg": picture(600, 800, "JPEG", o=6),
                      "broken.png": b"\x89PNG\r\n\x1a\nnot json", "deck.pptx": deck}
    out = json.loads(ask(ns, {"prompt": "出图", "format": "json", "skills": []}).body)
    found = {a["path"]: a for a in out["artifacts"]}
    small = found["chart.png"]["image"]
    assert (small["format"], small["width"], small["height"]) == ("png", 800, 450)
    assert base64.b64decode(small["preview"]["base64"]) == chart and small["thumb"]["width"] == 320 and small["thumb"]["format"] == "png"  # fits: itself
    big = found["big.png"]["image"]
    assert (big["width"], big["preview"]["width"], big["preview"]["height"], big["preview"]["format"]) == (4000, 1568, 1176, "jpeg")  # PNG over 1 MB: JPEG
    assert big["preview"]["bytes"] <= ns["PREVIEW_BYTES"] and big["thumb"]["width"] == 320
    photo = found["photo.jpg"]["image"]
    assert (photo["format"], photo["preview"]["format"], photo["preview"]["width"]) == ("jpeg", "jpeg", 1568)  # a photo is re-encoded as JPEG
    turned = found["turned.jpg"]["image"]
    assert (turned["width"], turned["preview"]["width"], turned["preview"]["height"]) == (600, 800, 600)  # EXIF orientation applied, not passed through
    assert found["broken.png"]["image"]["error"].startswith("OSError: cannot identify image file")
    pres = found["deck.pptx"]
    assert pres["slides"] == 2 and pres["text"].splitlines() == [
        "[幻灯片 1] 本期积分概况", "  共 3 位会员", "    M2001 最高", "  备注：先说结论", "[幻灯片 2] 明细", "  [表格 1]", "      | A | B", "    1 | 会员号 | 积分",
        "    2 | M1 | 10", "  [图表 BAR_CLUSTERED] 标题「积分图」；类别 M1、M2；系列「本期积分」10, 400", "  [图片 Picture 4 · image/png · 800×450 px]"]
    assert pres["table"]["sheets"][0]["rows"] == [["会员号", "积分"], ["M1", "10"]]
    [inside] = pres["images"]
    assert inside["name"] == "幻灯片 2 · Picture 4" and base64.b64decode(inside["preview"]["base64"]) == chart  # a picture in a slide, previewed
    ns["MAX_PICTURES"] = 2  # the answer's budget: the first files in order get previews, the rest say why not
    script.produce = {f"p{i}.png": picture(100, 100) for i in range(3)}
    capped = json.loads(ask(ns, {"prompt": "出图", "format": "json", "skills": []}, session="console-" + "f" * 40).body)["artifacts"]
    assert ["preview" in a["image"] for a in capped] == [True, True, False] and "at most 2 pictures" in capped[2]["image"]["note"]


# -- through the console ---------------------------------------------------------------------------------------------------

class ClaudeAWS(T.FakeAWS):
    """The deploy module's fake AWS plus the knowledge bases and the S3 reads the skill library does, and — for the
    Skill Lab — a runtime of the template (a call's skills from its payload, the files it made in its answer) and a judge."""

    SERVICES = {**T.FakeAWS.SERVICES, "bedrock-agent": "kb", "bedrock-runtime": "rt"}

    def __init__(self, **kw):
        super().__init__(**kw)
        self.kbs = {KB: {"knowledgeBaseId": KB, "name": "adlc-console-demo", "status": "ACTIVE", "description": "拾光家居积分规则（演示）",
                         "knowledgeBaseArn": f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/{KB}", "knowledgeBaseConfiguration": {"type": "MANAGED"},
                         "roleArn": f"arn:aws:iam::{ACCOUNT}:role/kb", "createdAt": "t", "updatedAt": "t"}}
        self.answer = {"result": "推荐积分在首单 7 天无理由退货期后发放。"}
        self.judged: list[str] = []

    def data_invoke_agent_runtime(self, **p):
        payload = json.loads(p["payload"])
        if "skills" not in payload:  # the smoke call
            return super().data_invoke_agent_runtime(**p)
        loaded = [o["name"] for o in payload.get("skillOverrides") or []]
        inputs = []
        for f in payload.get("files") or []:  # what the runtime reads before the prompt, checked as it checks it
            data = self.objects[f["uri"].split("/", 3)[3]][-1]["Body"]
            assert hashlib.sha256(data).hexdigest() == f["sha256"] and len(data) == f["bytes"]
            inputs.append({"path": f["path"], "bytes": len(data), "sha256": f["sha256"]})
        if payload.get("files"):
            image = {"format": "png", "width": 800, "height": 450, "preview": {"format": "png", "base64": base64.b64encode(picture(800, 450)).decode()},
                     "thumb": {"format": "png", "width": 320, "height": 180, "base64": base64.b64encode(picture(320, 180)).decode()}}
            made = [{"path": "points-chart.png" if loaded else "chart.png", "bytes": 9759, "type": "image", "status": "created", "text": "", "image": image}]
        elif loaded:
            made = [{"path": "points-report.xlsx", "bytes": 5096, "type": "xlsx", "status": "created", "text": "[工作表 1：积分明细]\n  1 | 会员号 | 本期积分 | 到期积分"}]
        else:
            made = [{"path": "会员积分报表.csv", "bytes": 60, "type": "csv", "status": "created", "text": "会员编号,本期积分\n"}]
        answer = {"result": "已生成。", "tools": [{"tool": "Skill", "input": {"skill": n}, "ok": True, "skill": n} for n in loaded], "skills": loaded,
                  "skillsLoaded": loaded, "artifacts": made, "call": {"id": "c", "workdir": "calls/c", "skills": [], "inputs": inputs}, "format": cs.FORMAT}
        return {"response": io.BytesIO(json.dumps(answer, ensure_ascii=False).encode()), "contentType": "application/json", "statusCode": 200,
                "runtimeSessionId": p["runtimeSessionId"]}

    def rt_converse(self, modelId, system, messages, inferenceConfig, **p):
        user = messages[0]["content"][0]["text"]
        self.judged.append(user)
        self.shown = getattr(self, "shown", []) + [[b["image"] for b in messages[0]["content"] if "image" in b]]
        ok = ("--- points-report.xlsx" in user and "会员号 | 本期积分 | 到期积分" in user) or ("--- points-chart.png" in user and bool(self.shown[-1]))
        return {"output": {"message": {"role": "assistant", "content": [{"text": json.dumps({"pass": ok, "score": 1.0 if ok else 0.3, "reason": "r"})}]}}}

    def iam_get_role_policy(self, RoleName, PolicyName):
        found = self._role(RoleName)["policies"].get(PolicyName)
        if found is None:
            raise T.err("NoSuchEntity", f"policy {PolicyName}")
        return {"RoleName": RoleName, "PolicyName": PolicyName, "PolicyDocument": found}

    def ctl_list_harnesses(self, **p):
        return {"harnesses": []}

    def client(self, service, region_name=None, **kw):
        return ClaudeClient(self, service)

    def kb_get_knowledge_base(self, knowledgeBaseId):
        if knowledgeBaseId not in self.kbs:
            raise T.err("ResourceNotFoundException", f"kb {knowledgeBaseId}")
        return {"knowledgeBase": self.kbs[knowledgeBaseId]}

    def s3_get_object(self, Bucket, Key, **p):
        if not self.objects.get(Key):
            raise T.err("NoSuchKey", "no such key")
        body = self.objects[Key][-1]["Body"]
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}

    def s3_list_object_versions(self, Bucket, Prefix="", **p):
        return super().s3_list_object_versions(Bucket=Bucket, Prefix=Prefix)

    def s3_delete_objects(self, Bucket, Delete, **p):
        return super().s3_delete_objects(Bucket=Bucket, Delete=Delete)


class ClaudeClient(T.FakeClient):
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
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture()
def server(tmp_path, monkeypatch):
    for owner, attrs in ((dp.Pipeline, ("BUILD_POLL", "RUNTIME_POLL", "ENDPOINT_POLL", "IAM_PAUSE", "SCAN_POLL")), (dp.Teardown, ("POLL",))):
        for attr in attrs:
            monkeypatch.setattr(owner, attr, 0.0)
    aws = ClaudeAWS()
    console = T.console_mod.Console(tmp_path / "data", session_factory=lambda **kw: T.FakeSession(aws), clients_factory=lambda cfg: None)
    srv = T.console_mod.create_server(console)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = Client(srv.server_address[1])
    assert client.call("POST", "/workspaces", {"id": "dev", "accountId": ACCOUNT, "region": REGION, "profile": "default"})[0] == 201
    assert client.call("POST", "/workspaces/dev/skill-lab/samples", {})[0] == 201  # loyalty-reply-style v0001 in the library
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


C = "/workspaces/dev/claude-sdk"
NAME = "adlc_probe_claude_ab12cd"


def body(**extra) -> dict:
    return {**cs.SAMPLE_SPEC, "name": NAME, "knowledgeBases": [KB], **extra}


def test_the_template_previews_bundles_and_deploys_through_the_pipeline_with_the_scan_gate_and_session_storage(server):
    client, aws, _console = server
    status, cat = client.call("GET", f"{C}/catalog")
    assert status == 200 and cat["defaultModel"] == cs.DEFAULT_MODEL and "WebSearch" not in [t["id"] for t in cat["tools"]]
    assert cat["defaults"]["mountPath"] == "/mnt/workspace" and cat["defaults"]["scanBlock"] == ["CRITICAL"] and cat["versions"]["cli"] == cs.CLAUDE_CODE
    status, shown = client.call("POST", f"{C}/preview", body())
    assert status == 200 and shown["skills"] == {"loyalty-reply-style": "v0001"} and shown["knowledgeBases"][KB]["name"] == "adlc-console-demo"
    assert shown["files"][".claude/skills/loyalty-reply-style/SKILL.md"] == SKILL_TEXT and shown["names"] == {
        "role": f"adlc-probe-claude-rt-{NAME}", "repository": "adlc-probe-claude/adlc-probe-claude-ab12cd", "buildProject": "adlc-probe-claude-build"}
    status, packed = client.call("POST", f"{C}/bundle", body())
    assert status == 200 and packed["filename"] == f"{NAME}-claude-agent.zip" and "Dockerfile" in packed["files"]
    assert zipfile.ZipFile(io.BytesIO(base64.b64decode(packed["archive"]))).read("Dockerfile").decode().startswith("# Generated by the ADLC console")
    assert client.call("POST", f"{C}/preview", body(skills=["no-such-skill"]))[0] == 400
    assert client.call("POST", f"{C}/preview", body(knowledgeBases=["ABCDEFGHIJ"]))[1]["error"].startswith("知识库 ABCDEFGHIJ 不在这个工作区")
    status, job = client.call("POST", f"{C}/deploy", body(sessionStorage=True, lifecycle={"idleRuntimeSessionTimeout": 600, "maxLifetime": 7200}))
    assert status == 202 and job["kind"] == "deploy" and job["claude"]["sessionStorage"] == "/mnt/workspace"
    assert job["params"]["source"] == "dockerfile" and job["params"]["scanBlock"] == ["CRITICAL"] and job["params"]["knowledgeBases"] == [KB]
    done = finished(client, job["id"])
    assert done["status"] == "succeeded", done.get("error")
    assert [s["name"] for s in done["progress"]["stages"]] == list(dp.IMAGE_STAGES) and all(s["status"] == "succeeded" for s in done["progress"]["stages"])
    assert done["progress"]["scan"]["status"] == "COMPLETE" and done["result"]["scan"]["counts"] == {}
    [repo] = aws.named("ecr", "create_repository")
    assert repo["repositoryName"] == "adlc-probe-claude/adlc-probe-claude-ab12cd" and repo["imageScanningConfiguration"] == {"scanOnPush": True}
    [start] = aws.named("codebuild", "start_build")
    assert start["projectName"] == "adlc-probe-claude-build"  # a probe never touches the console's (or another probe's) build project
    [create] = aws.named("bedrock-agentcore-control", "create_agent_runtime")
    assert set(create["agentRuntimeArtifact"]) == {"containerConfiguration"} and create["roleArn"].endswith(f":role/adlc-console/adlc-probe-claude-rt-{NAME}")
    assert create["filesystemConfigurations"] == [{"sessionStorage": {"mountPath": "/mnt/workspace"}}]
    assert create["lifecycleConfiguration"] == {"idleRuntimeSessionTimeout": 600, "maxLifetime": 7200}
    assert create["environmentVariables"] == {"AGENT_WORKSPACE": "/mnt/workspace"} and create["networkConfiguration"] == {"networkMode": "PUBLIC"}
    policy = json.loads(next(p for p in aws.named("iam", "put_role_policy") if p["RoleName"] == f"adlc-probe-claude-rt-{NAME}")["PolicyDocument"])
    assert [s["Resource"] for s in policy["Statement"] if s["Sid"] == "KnowledgeBases"] == [[f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/{KB}"]]
    [smoke] = aws.named("bedrock-agentcore", "invoke_agent_runtime")
    assert json.loads(smoke["payload"]) == {"prompt": cs.SAMPLE_SPEC["sample"]}  # the spec's sample question
    [put] = [p for p in aws.named("s3", "put_object") if p["Key"].endswith("/context.zip")]
    with zipfile.ZipFile(io.BytesIO(put["Body"])) as uploaded:
        assert "Read" in json.loads(uploaded.read("agent_spec.json"))["spec"]["tools"] and uploaded.read(".claude/skills/loyalty-reply-style/SKILL.md").decode() == SKILL_TEXT
    rid = done["result"]["runtimeId"]
    status, agents = client.call("GET", f"{C}/agents")
    [listed] = agents["agents"]
    assert listed["name"] == NAME and listed["runtimeId"] == rid and listed["deployments"][0]["jobStatus"] == "succeeded"
    assert listed["deployments"][0]["scan"]["status"] == "COMPLETE" and listed["spec"]["skills"] == [{"name": "loyalty-reply-style", "version": None}]
    # a new version with another prompt keeps the runtime's session storage, so the agent still knows its workspace
    status, again = client.call("POST", f"{C}/deploy", body(runtimeId=rid, systemPrompt="你是积分客服，回答更简短。", smoke=False))
    assert status == 202 and again["claude"]["mode"] == "update" and again["claude"]["sessionStorage"] == "/mnt/workspace"
    assert finished(client, again["id"])["status"] == "succeeded"
    [update] = aws.named("bedrock-agentcore-control", "update_agent_runtime")
    assert update["filesystemConfigurations"] == [{"sessionStorage": {"mountPath": "/mnt/workspace"}}] and update["environmentVariables"] == {
        "AGENT_WORKSPACE": "/mnt/workspace"}
    assert client.call("GET", f"{C}/agents/{NAME}")[1]["spec"]["systemPrompt"] == "你是积分客服，回答更简短。"
    assert client.call("GET", f"{C}/agents/nobody")[0] == 404


def test_a_blocked_image_stops_the_template_deploy_and_only_admins_deploy(server):
    client, aws, console = server
    aws.scan_counts = {"CRITICAL": 1, "HIGH": 2}
    aws.scan_findings = [{"name": "CVE-2026-57433", "severity": "CRITICAL", "attributes": [{"key": "package_name", "value": "perl"},
                                                                                           {"key": "package_version", "value": "5.36.0-7+deb12u3"}]}]
    status, job = client.call("POST", f"{C}/deploy", body())
    done = finished(client, job["id"])
    assert done["status"] == "failed" and "CVE-2026-57433 (perl 5.36.0-7+deb12u3, CRITICAL)" in done["error"]
    assert done["progress"]["scan"]["blocking"] == {"CRITICAL": 1} and not aws.named("bedrock-agentcore-control", "create_agent_runtime")
    assert not aws.repos  # the repository this job made is taken back with its image
    status, job = client.call("POST", f"{C}/deploy", body(scanBlock=[]))  # report only: deployed, the counts on the job
    done = finished(client, job["id"])
    assert done["status"] == "succeeded" and done["progress"]["scan"]["counts"] == {"CRITICAL": 1, "HIGH": 2} and done["progress"]["scan"]["blocking"] == {}
    assert client.call("POST", "/users", {"username": "admin", "password": "0123456789", "role": "admin"})[0] == 201
    assert client.call("POST", "/login", {"username": "admin", "password": "0123456789"})[0] == 200
    assert client.call("POST", "/users", {"username": "ann", "password": "0123456789", "role": "member", "workspaces": ["dev"]})[0] == 201
    client.cookie = None
    assert client.call("POST", "/login", {"username": "ann", "password": "0123456789"})[0] == 200
    assert client.call("POST", f"{C}/preview", body())[0] == 200  # a member may look at the code
    assert client.call("POST", f"{C}/deploy", body(name="member_agent"))[0] == 403


def test_a_skill_is_evaluated_on_a_runtime_deployed_from_the_template_through_the_console(server, monkeypatch):
    client, aws, _console = server
    monkeypatch.setattr(sl, "GRANT_WAIT", 0.0)
    assert client.call("GET", "/workspaces/dev/skill-lab/samples")[1]["samples"][2]["name"] == POINTS
    status, imported = client.call("POST", "/workspaces/dev/skill-lab/samples", {"sample": POINTS})
    assert status == 201 and imported["skill"]["version"] == "v0001" and imported["taskset"]["counts"] == {"train": 4, "val": 3, "test": 6}
    status, job = client.call("POST", f"{C}/deploy", body(skills=[POINTS], knowledgeBases=[], tools=["Read", "Write", "Bash"]))
    assert status == 202 and job["claude"]["template"] == cs.TEMPLATE
    done = finished(client, job["id"])
    assert done["status"] == "succeeded", done.get("error")
    rid = done["result"]["runtimeId"]
    status, listed = client.call("GET", "/workspaces/dev/skill-lab/targets")
    assert status == 200 and listed["targets"] == [{"kind": "runtime", "id": rid, "name": NAME, "status": "READY", "template": cs.TEMPLATE, "skills": [POINTS],
                                                    "model": cs.DEFAULT_MODEL, "note": None, "inputFiles": True}]
    status, started = client.call("POST", "/workspaces/dev/skill-lab/evaluations", {"runtimeId": rid, "skill": POINTS, "tasksetId": imported["taskset"]["id"],
                                                                                    "limit": 2})
    assert status == 202, started
    ev = finished(client, started["job"]["id"])
    assert ev["status"] == "succeeded", ev.get("error")
    assert ev["result"]["arms"]["with"]["passRate"] == 1.0 and ev["result"]["arms"]["without"]["passRate"] == 0.0
    assert ev["result"]["arms"]["with"]["triggerRate"] == 1.0
    calls = [json.loads(c["payload"]) for c in aws.named("bedrock-agentcore", "invoke_agent_runtime")][1:]  # after the smoke call; each request botocore-checked
    uri = f"s3://{T.BUCKET}/skills/{POINTS}/v0001/"
    assert sorted(json.dumps(c, sort_keys=True) for c in calls) == sorted(json.dumps(c, sort_keys=True) for c in [
        {"prompt": q, "format": "json", "skills": s_, "skillOverrides": o} for q in [t["question"] for t in
            json.loads((REPO / "app/console/samples/skill-lab" / POINTS / "taskset.json").read_text(encoding="utf-8"))["test"][:2]]
        for s_, o in (([POINTS], [{"name": POINTS, "uri": uri}]), ([], []))])
    policy = json.loads(aws.roles[f"adlc-probe-claude-rt-{NAME}"]["policies"][sl.SKILLS_POLICY])
    assert policy == sl.runtime_read_policy(T.BUCKET)  # the runtime's role reads the library and the task files, put through the deploy module's role
    assert len(aws.judged) == 4 and all("Files the agent created or changed" in u for u in aws.judged)
    for _ in range(100):
        if len(aws.named("bedrock-agentcore", "stop_runtime_session")) >= 5:
            break
        time.sleep(0.02)
    assert len(aws.named("bedrock-agentcore", "stop_runtime_session")) == 5  # the smoke's and the four rollouts'


def test_a_skill_whose_tasks_give_files_is_evaluated_through_the_console_and_judged_from_its_pictures(server, monkeypatch):
    client, aws, console = server
    monkeypatch.setattr(sl, "GRANT_WAIT", 0.0)
    S = "/workspaces/dev/skill-lab"
    status, up = client.call("POST", f"{S}/assets", {"files": [{"name": "members.csv", "contentBase64": base64.b64encode("会员号,本期积分\nM1,10\n".encode()).decode()}]})
    assert status == 201 and up["assets"][0]["created"] and aws.named("s3", "put_object")[-1]["Key"] == f"skill-lab/assets/{up['assets'][0]['sha256']}"
    assert client.call("POST", f"{S}/assets", {"files": [{"name": "x.exe", "contentBase64": "TVo="}]})[0] == 400
    gone = client.call("DELETE", f"{S}/assets/{up['assets'][0]['sha256']}")[1]  # forgotten; the object stays (content-addressed, shared)
    assert gone["deleted"] == up["assets"][0]["asset"] and gone["objects"] == 0
    status, imported = client.call("POST", f"{S}/samples", {"sample": "points-chart-png"})
    assert status == 201 and imported["taskset"]["files"] == 11 and len([k for k in aws.objects if k.startswith("skill-lab/assets/")]) == 13  # + the one forgotten above, kept
    status, listed = client.call("GET", f"{S}/assets")
    assert status == 200 and len(listed["assets"]) == 12 and all(a["usedBy"] for a in listed["assets"])
    assert client.call("DELETE", f"{S}/assets/{listed['assets'][0]['sha256']}")[0] == 400  # a task set names it
    status, job = client.call("POST", f"{C}/deploy", body(skills=["points-chart-png"], knowledgeBases=[], tools=["Read", "Write", "Bash"]))
    rid = finished(client, job["id"])["result"]["runtimeId"]
    status, started = client.call("POST", f"{S}/evaluations", {"runtimeId": rid, "skill": "points-chart-png", "tasksetId": imported["taskset"]["id"], "limit": 2})
    assert status == 202, started
    ev = finished(client, started["job"]["id"])
    assert ev["status"] == "succeeded", ev.get("error")
    assert ev["params"]["inputFiles"] == 2 and ev["result"]["arms"]["with"]["passRate"] == 1.0
    calls = [json.loads(c["payload"]) for c in aws.named("bedrock-agentcore", "invoke_agent_runtime") if "files" in json.loads(c["payload"])]
    assert len(calls) == 4 and [f["path"] for f in calls[0]["files"]] in (["members.csv"], ["members.csv", "supplement.csv"])
    assert all(f["uri"] == f"s3://{T.BUCKET}/skill-lab/assets/{f['sha256']}" for c in calls for f in c["files"])
    assert len(aws.shown) == 4 and all(len(s) == 1 and s[0]["format"] == "png" for s in aws.shown)  # every judgment saw the chart (botocore-checked blocks)
    row = ev["result"]["rows"][0]
    assert "preview" not in row["with"]["artifacts"][0]["image"] and row["with"]["artifacts"][0]["image"]["thumb"]["width"] == 320
    assert client.call("POST", f"{S}/evaluations", {"harnessId": "h-0123456789", "skill": "points-chart-png", "tasksetId": imported["taskset"]["id"]})[1]["error"].startswith(
        "4 of these tasks give the agent input files")  # a Harness: refused before it is even looked up


def test_only_the_task_file_upload_takes_a_body_over_25_mb():
    assert T.console_mod.body_limit("/api/console/workspaces/dev/skill-lab/assets") == sl.MAX_UPLOAD_BODY > 4 * sl.MAX_ASSET_BYTES // 3
    assert T.console_mod.body_limit("/api/console/workspaces/dev/skill-lab/assets/abc") == T.console_mod.MAX_BODY == 25 * 1024 * 1024
    assert T.console_mod.body_limit("/api/console/workspaces/dev/knowledge-bases/x/upload") == T.console_mod.MAX_BODY


# -- the page ---------------------------------------------------------------------------------------------------------------

def test_the_deploy_page_has_the_template_the_scan_and_the_runtime_options():
    page = (REPO / "app" / "console" / "static" / "pages" / "deploy.mjs").read_text(encoding="utf-8")
    assert "export default { id: 'deploy', label: '部署代码', group: '构建', Page: DeployPage }" in page
    assert "从模板生成：Claude Agent SDK" in page and "/claude-sdk/deploy" in page and "/claude-sdk/preview" in page
    assert "镜像扫描" in page and "会话存储" in page and "生命周期" in page and "VPC" in page and "scanBlock" in page
    node_bin = shutil.which("node") or str(Path.home() / ".nvm" / "versions" / "node" / "v25.2.1" / "bin" / "node")
    if not Path(node_bin).exists():
        pytest.skip("no node to syntax-check the page")
    checked = subprocess.run([node_bin, "--check", str(REPO / "app" / "console" / "static" / "pages" / "deploy.mjs")], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
