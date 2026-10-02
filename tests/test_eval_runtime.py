"""The single-owner evaluation runtime render emits for 06/09/10/11/12/13/99 (SPEC D3, D6c-e).

Static checks on the rendered reference releases: every patch is applied exactly once, the counts
all equal |P|, lookups are scoped to this deployment (evaluated with the real JMESPath engine over an
account holding two namespaces plus prefix decoys), the knowledge base starts fresh, and the 06/10
wording comes from labs.teaching. The rendered flow itself runs in test_eval_flow_fakebin.py.
"""

from __future__ import annotations

import ast
import collections
import json
import re
import shutil
import subprocess
from pathlib import Path

import jmespath
import pytest
import yaml

from workshop_customizer import compiler, render, script_facts, teaching, validator
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = REPO_ROOT / "upstream"
TEMPLATE_COMMIT = json.loads((REPO_ROOT / "template-lock.json").read_text())["template"]["commit"]
PACKS = ("hr-default", "it-helpdesk", "maintenance")

#: Every (file, patch) of the evaluation runtime; each must be applied exactly once per release.
EVAL_RUNTIME_PATCHES = {
    ("06-test-conversation.sh", n) for n in (
        "first-conversation-from-pack", "first-conversation-actor", "first-conversation-topic", "memory-notice-from-pack",
        "phase3-title-from-pack")
} | {
    ("09-run-eval.sh", n) for n in (
        "golden-order-comment", "golden-queries", "golden-labels", "golden-case-arrays", "eval-run-context", "recent-n",
        "session-io-per-process", "fresh-baseline-traces", "golden-count-message", "per-case-actor-session-log", "expected-retrieval-count",
        "refresh-session-io-after-conversations", "evaluate-all-retrieval-cases", "guide-trace-only-evaluation",
        "wait-for-usable-evaluation-evidence", "reject-empty-evaluation", "reject-evaluation-error", "count-usable-scores",
        "reject-invalid-score", "reject-all-skipped", "case-tagged-scores-env", "case-tagged-scores-load",
        "case-tagged-scores-print", "probe-sessions-only-scope", "probe-sessions-only-filter", "one-trace-per-probe-session",
        "no-retrieval-still-scores-l1", "mtg-every-practice-session", "mtg-fatal-only-noninteractive", "l1-after-judges",
        "runtime-scoped-lookup", "evaluator-scoped-lookup")
} | {
    ("10-optimize-prompt.sh", n) for n in (
        "golden-queries", "golden-labels", "golden-case-arrays", "eval-run-context", "candidate-prompt-from-pack",
        "candidate-prompt-message", "golden-count-message", "fresh-optimized-traces", "per-case-actor-session-log",
        "wait-for-current-batch", "probe-trace-count", "probe-trace-wait-message", "wait-for-all-retrieval-cases",
        "matching-optimized-sample-count", "optimize-closing-from-teaching", "optimize-prompt-in-place", "optimize-prompt-in-place-header",
        "propagate-deploy-failure")
} | {
    ("11-cost-latency.sh", "recent-n-cost"),
    ("12-compare-models.sh", "comparison-eval-phase"), ("12-compare-models.sh", "comparison-plan-message"),
    ("12-compare-models.sh", "comparison-rerun-message"), ("12-compare-models.sh", "comparison-reading-reference"),
    ("13-judge-stability.sh", "runtime-scoped-lookup"), ("13-judge-stability.sh", "evaluator-scoped-lookup"),
    ("13-judge-stability.sh", "guide-explicit-trace-stability"),
    ("99-cleanup.sh", "cleanup-scoped-harness-lookup"), ("99-cleanup.sh", "cleanup-scoped-runtime-lookup"),
    ("99-cleanup.sh", "cleanup-scoped-kb-fallback"), ("99-cleanup.sh", "cleanup-eval-runs"),
    ("01-create-kb.sh", "docs-from-pack"),
    ("knowledge-base/create_kb.py", "kb-prune-helper"), ("knowledge-base/create_kb.py", "kb-prune-stale-documents"),
} | {
    (render.MTG_PROMPTS_REL, n) for n in (
        "mtg-judge-scenario-policy", "mtg-dialog-source", "mtg-rcof-e2-policy", "mtg-rcof-e6-policy", "mtg-user-label")
}

_RELEASES: dict[str, tuple] = {}


@pytest.fixture(scope="module")
def released(tmp_path_factory):
    def get(pack_id: str):
        if pack_id not in _RELEASES:
            base = tmp_path_factory.mktemp(f"rt-{pack_id}")
            scenario = load_scenario(REPO_ROOT / "scenarios" / pack_id / "scenario.yaml")
            pack = compiler.compile_pack(scenario, base / "build", template_commit=TEMPLATE_COMMIT)
            _RELEASES[pack_id] = (scenario.data, render.render_release(pack, UPSTREAM, base / "release", template_commit=TEMPLATE_COMMIT))
        return _RELEASES[pack_id]

    yield get
    _RELEASES.clear()


def _text(release, rel: str) -> str:
    return (release.release_dir / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Exactly one application of every patch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pack_id", PACKS)
def test_every_patch_is_applied_exactly_once(pack_id, released):
    _data, release = released(pack_id)
    counts = collections.Counter((p.file, p.name) for p in release.patches)
    assert [key for key, n in counts.items() if n != 1] == []
    assert EVAL_RUNTIME_PATCHES <= set(counts), sorted(EVAL_RUNTIME_PATCHES - set(counts))
    matches = {(p.file, p.name): p.matches for p in release.patches}
    assert matches[("99-cleanup.sh", "cleanup-scoped-harness-lookup")] == 2  # both harness queries of 99
    assert all(m == 1 for key, m in matches.items() if key in EVAL_RUNTIME_PATCHES and key[1] != "cleanup-scoped-harness-lookup")
    manifest = json.loads((release.release_dir / render.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert [(p["file"], p["name"]) for p in manifest["patches"]] == [(p.file, p.name) for p in release.patches]


@pytest.mark.parametrize("pack_id", PACKS)
def test_inserted_runtime_appears_once(pack_id, released):
    data, release = released(pack_id)
    s09, s10 = _text(release, "09-run-eval.sh"), _text(release, "10-optimize-prompt.sh")
    for text in (s09, s10):
        assert text.count(script_facts.RUNTIME_ACTOR_EXPR) == 1
        assert text.count("${GOLDEN_PROBE[$i]}") == 1  # the sessions.tsv probe column
        assert not re.search(r"^ACTOR_ID=", text, re.M) and "WORKSHOP_ACTOR_SUFFIX" not in text
    assert s09.count("l1_eval.py") == 1 and s09.count('EVAL_LABEL="$label"') == 1
    assert s09.count("f' case={_case}'") == 1 and s09.count("if SCOPED and sid not in PROBES: continue") == 1
    assert re.findall(r"^\s*build_session_io_map$", s09, re.M) == ["build_session_io_map", "    build_session_io_map"]
    # SINCE_EPOCH_MS = run start, never reset: exactly one assignment per script.
    assert len(re.findall(r"SINCE_EPOCH_MS=\$\(\( \$\(date \+%s\) \* 1000 \)\)", s09)) == 1
    assert len(re.findall(r"SINCE_EPOCH_MS=\$\(\( \$\(date \+%s\) \* 1000 \)\)", s10)) == 1
    assert _text(release, "12-compare-models.sh").count("WORKSHOP_EVAL_PHASE=comparison WORKSHOP_EVAL_RUN_DIR= ") == 1
    prompts = _text(release, render.MTG_PROMPTS_REL)
    assert prompts.count("USER_ROLE_LABEL") == 2 and prompts.count("EVALUATION_SYSTEM_PROMPT = ") == 1
    kb = _text(release, "knowledge-base/create_kb.py")
    assert kb.count("prune_stale_documents(") == 2
    assert _text(release, "01-create-kb.sh").count('find "$DOCS_DIR" -mindepth 1 -delete') == 1
    for script in sorted(release.release_dir.glob("*.sh")):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (script.name, result.stderr)


@pytest.mark.parametrize("pack_id", PACKS)
def test_counts_equal_the_probe_count(pack_id, released):
    data, release = released(pack_id)
    n = len(teaching.probe_case_ids(data))
    facts = script_facts.compute(data)
    assert facts.probe_count == facts.recent_n == n >= 3
    s09, s10, s11 = (_text(release, f) for f in ("09-run-eval.sh", "10-optimize-prompt.sh", "11-cost-latency.sh"))
    assert f'local want="{n}"' in s09 and re.search(rf"^    N={n}$", s09, re.M) and re.search(rf"^RECENT_N={n}\s", s09, re.M)
    assert f'--eval-only {n}\n' in s10 and f'-ge {n} ]' in s10 and re.search(rf"^RECENT_N={n}\s", s11, re.M)
    assert "or len(practice)" not in (REPO_ROOT / "engine" / "workshop_customizer" / "render.py").read_text(encoding="utf-8")
    ids = script_facts.bash_arrays(s09, "GOLDEN_IDS")[0]
    assert tuple(ids) == teaching.eval_order_ids(data) and ids[-1] == teaching.stability_case_id(data)
    probe = script_facts.bash_arrays(s09, "GOLDEN_PROBE")[0]
    assert probe == ["0"] * teaching.probe_start_index(data) + ["1"] * n
    manifest = json.loads((release.release_dir / render.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["evaluationFeatures"] == sorted(script_facts.FEATURES)
    assert (release.release_dir / "l1_eval.py").read_bytes() == render.L1_SOURCE.read_bytes()


# ---------------------------------------------------------------------------
# Lookups scoped to this deployment (two namespaces in one account)
# ---------------------------------------------------------------------------


def _account(*agents: str) -> dict:
    """An account holding several deployments; decoys first so an unscoped `head -1` would pick them."""
    runtimes, evaluators, harnesses, kbs = [], [], [], []
    for agent in agents:
        runtimes.append({"agentRuntimeName": f"harness_{agent}_{agent}", "agentRuntimeArn": f"arn:runtime/{agent}",
                         "agentRuntimeId": f"rt-{agent}"})
        for ev in ("thelma_rag_quality", "mtg_goal_success"):
            evaluators.append({"evaluatorId": f"{agent}_{ev}-Ab12Cd34", "evaluatorArn": f"arn:evaluator/{agent}/{ev}"})
        harnesses.append({"harnessName": f"{agent}_{agent}", "harnessId": f"h-{agent}"})
    for name in ("hr-knowledge-base", "it-knowledge-base", "maint-knowledge-base", "workshop-kb"):
        kbs.append({"name": name, "knowledgeBaseId": f"kb-{name}"})
    return {"agentRuntimes": runtimes, "evaluators": evaluators, "harnesses": harnesses, "knowledgeBaseSummaries": kbs}


def _queries(text: str) -> list[str]:
    return re.findall(r'--query "([^"]+)"', text)


@pytest.mark.parametrize("pack_id", ("it-helpdesk", "maintenance", "hr-default"))
def test_lookups_select_only_this_deployment(pack_id, released):
    data, release = released(pack_id)
    agent = data["namespace"]["agentName"]
    other = "itassistant" if agent != "itassistant" else "maintassistant"
    # Decoys: another pack, a name that contains ours, and one that extends ours.
    account = _account(f"x{agent}", f"{agent}2", other, agent)
    selections = {}
    for rel in ("09-run-eval.sh", "13-judge-stability.sh", "99-cleanup.sh"):
        for query in _queries(_text(release, rel)):
            if "agentRuntimes" in query or "evaluators" in query or "harnesses" in query or "knowledgeBaseSummaries" in query:
                for ev in ("thelma_rag_quality", "mtg_goal_success"):
                    q = query.replace("$1", ev)
                    selections[(rel, q)] = jmespath.search(q, account)
    assert selections, "no lookups found"
    for (rel, query), found in selections.items():
        flat = found if isinstance(found, list) else [found]
        assert flat and all(v is not None for v in flat), (rel, query, found)
        for value in flat:
            assert agent in str(value) or data["namespace"]["knowledgeBaseName"] in str(value), (rel, query, value)
            assert f"x{agent}" not in str(value) and f"{agent}2" not in str(value) and other not in str(value), (rel, query, value)
    s09 = _text(release, "09-run-eval.sh")
    assert f"starts_with(evaluatorId,'{agent}_$1')" in s09
    assert "contains(evaluatorId" not in s09 and "contains(agentRuntimeName" not in s09
    s13 = _text(release, "13-judge-stability.sh")
    assert "contains(evaluatorId" not in s13 and "contains(agentRuntimeName" not in s13
    s99 = _text(release, "99-cleanup.sh")
    assert f"starts_with(harnessName,'{agent}_')" in s99 and "contains(name,'hr')" not in s99


def test_upstream_lookups_would_have_picked_a_decoy():
    """The same account under the pinned upstream queries: the account-wide lookups are ambiguous."""
    account = _account("xitassistant", "itassistant")
    upstream = (UPSTREAM / "09-run-eval.sh").read_text(encoding="utf-8").replace("hrassistant", "itassistant")
    runtime_query = next(q for q in _queries(upstream) if "agentRuntimes" in q)
    assert jmespath.search(runtime_query, account)[0] == "arn:runtime/xitassistant"
    evaluator_query = next(q for q in _queries(upstream) if "evaluators" in q).replace("$1", "thelma_rag_quality")
    assert jmespath.search(evaluator_query, account)[0] == "arn:evaluator/xitassistant/thelma_rag_quality"


# ---------------------------------------------------------------------------
# Knowledge-base freshness (SPEC D6c)
# ---------------------------------------------------------------------------


def _prune_function(release):
    tree = ast.parse(_text(release, "knowledge-base/create_kb.py"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "prune_stale_documents")
    namespace = {"os": __import__("os")}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<prune>", "exec"), namespace)
    return namespace["prune_stale_documents"]


class FakeS3:
    def __init__(self, keys):
        self.keys = set(keys)
        self.deleted = []

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):
        keys = sorted(k for k in self.keys if k.startswith(Prefix))
        yield {"Contents": [{"Key": k} for k in keys[:2]]}
        yield {"Contents": [{"Key": k} for k in keys[2:]]}
        yield {}

    def delete_objects(self, Bucket, Delete):
        assert Delete["Quiet"] is True and len(Delete["Objects"]) <= 1000
        for obj in Delete["Objects"]:
            self.keys.discard(obj["Key"])
            self.deleted.append(obj["Key"])


def test_kb_prune_deletes_only_stale_keys_under_the_prefix(released, tmp_path, capsys):
    data, release = released("it-helpdesk")
    prune = _prune_function(release)
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in ("vpn_access.md", "hardware_lifecycle.md"):
        (docs / name).write_text("x", encoding="utf-8")
    s3 = FakeS3({"it/vpn_access.md", "it/hardware_lifecycle.md", "it/old_policy.md", "it/leave_policy.md",
                 "hr/leave_policy.md", "skills/x/SKILL.md", "itx/other.md"})
    stale = prune(s3, "bucket", data["namespace"]["kbPrefix"], str(docs))
    assert sorted(stale) == ["it/leave_policy.md", "it/old_policy.md"]
    assert s3.keys == {"it/vpn_access.md", "it/hardware_lifecycle.md", "hr/leave_policy.md", "skills/x/SKILL.md", "itx/other.md"}
    assert "Removed 2 stale object(s)" in capsys.readouterr().out
    for bad in ("", "it", "skills/", "/", "customizer-releases/", "multimodal/", "Customizer-Releases/"):
        with pytest.raises(RuntimeError, match="refusing to prune"):
            prune(FakeS3(set()), "bucket", bad, str(docs))


def test_kb_prune_never_touches_the_release_bundles(released, tmp_path):
    """The workshop bucket also holds the SA's release bundles under customizer-releases/: a pack whose
    kbPrefix were that prefix would have 01 delete every project's release.zip (review finding)."""
    from workshop_customizer.validator import RESERVED_BUCKET_PREFIXES

    _data, release = released("it-helpdesk")
    prune = _prune_function(release)
    bundles = {"customizer-releases/aws-it-helpdesk/it-helpdesk-0123456789ab/release.zip",
               "customizer-releases/aws-it-helpdesk/it-helpdesk-0123456789ab/RELEASE.json",
               "customizer-releases/infrastructure/0123456789abcdef-addons.json"}
    s3 = FakeS3(bundles | {"skills/x/SKILL.md", "multimodal/page-1.png"})
    for prefix in RESERVED_BUCKET_PREFIXES:
        with pytest.raises(RuntimeError, match="refusing to prune"):
            prune(s3, "bucket", prefix, str(tmp_path))
    assert s3.deleted == [] and bundles <= s3.keys


def test_kb_prune_runs_before_the_upload(released):
    _data, release = released("maintenance")
    kb = _text(release, "knowledge-base/create_kb.py")
    assert kb.index("prune_stale_documents(kb.s3_client") < kb.index("kb.upload_directory(") < kb.index("kb.synchronize_data(")
    assert 'INCLUSION_PREFIX = "maint/"' in kb


@pytest.mark.skipif(subprocess.run(["bash", "--version"], capture_output=True).returncode != 0, reason="no bash")
def test_docs_dir_is_emptied_before_the_pack_documents_are_copied(released, tmp_path):
    _data, release = released("it-helpdesk")
    s01 = _text(release, "01-create-kb.sh")
    block = s01[s01.index('mkdir -p "$DOCS_DIR"'):s01.index('"$DOCS_DIR/"\n') + len('"$DOCS_DIR/"\n')]
    docs = tmp_path / "knowledge-base"
    (docs / "nested").mkdir(parents=True)
    (docs / "generate_hr_docs_output.md").write_text("old HR doc", encoding="utf-8")
    (docs / "nested" / "stale.md").write_text("stale", encoding="utf-8")
    result = subprocess.run(["bash", "-ec", block], env={"PATH": "/usr/bin:/bin", "DOCS_DIR": str(docs), "SCRIPT_DIR": str(release.release_dir)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in docs.iterdir()) == sorted(p.name for p in (release.release_dir / "pack/knowledge-base/docs").glob("*.md"))


def _kb_data(files):
    return {"knowledge": {"documents": [{"id": f"d{i}", "file": f} for i, f in enumerate(files)]}}


def test_knowledge_documents_must_be_markdown_with_unique_names():
    assert validator.check_knowledge_documents(_kb_data(["knowledge-base/docs/a.md", "knowledge-base/b.md"])).findings == []
    report = validator.check_knowledge_documents(_kb_data(["docs/a.txt", "docs/B.md", "other/b.md", "x/c.MD"]))
    assert [(f.code, f.path) for f in report.findings] == [
        ("knowledge.document_not_markdown", "knowledge.documents.d0"),
        ("knowledge.duplicate_basename", "knowledge.documents.d2"),
        ("knowledge.document_not_markdown", "knowledge.documents.d3"),
    ]
    assert all(f.severity == "error" and f.scopes == ("knowledge",) for f in report.findings)
    assert "'d1' and 'd2' share the file name 'b.md'" in report.findings[1].message


@pytest.mark.parametrize("pack_id", PACKS)
def test_reference_packs_pass_the_knowledge_rule_through_the_composed_policy(pack_id):
    path = REPO_ROOT / "scenarios" / pack_id / "scenario.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    report = validator.validate_scenario_policy(data, root=path.parent)
    assert not [f for f in report.findings if f.code.startswith("knowledge.")]
    data["knowledge"]["documents"][0]["file"] = data["knowledge"]["documents"][0]["file"].replace(".md", ".txt")
    bad = validator.validate_scenario_policy(data, root=path.parent)
    assert "knowledge.document_not_markdown" in {f.code for f in bad.errors}


# ---------------------------------------------------------------------------
# 06 / 10 wording and pack.json
# ---------------------------------------------------------------------------


def _fc(**kw):
    return script_facts.FirstConversation(query="q", actor_id="a", source="teaching", **kw)


def test_first_conversation_lines():
    assert script_facts.first_conversation_topic(_fc(label="annual leave policy")) == "🗣️  Asking about annual leave policy..."
    assert script_facts.first_conversation_topic(_fc()) == "🗣️  Asking about your first question..."
    assert script_facts.first_conversation_topic(_fc(label="年假政策")) == "🗣️  正在询问：年假政策..."
    notice = script_facts.memory_notice
    assert notice(_fc(unknown_context=("tenure", "department", "specific leave balance"))) == (
        "Notice: The answer is GENERIC — the Agent doesn't know your tenure, department, or specific leave balance yet.")
    assert notice(_fc(unknown_context=("purchase date", "asset tag"))).endswith("know your purchase date or asset tag yet.")
    assert notice(_fc(unknown_context=("site",))).endswith("know your site yet.")
    assert notice(_fc(unknown_context=("工龄", "部门"))) == "提示：回答是通用的——Agent 还不知道你的工龄、部门。"
    assert "anything about you" in notice(_fc())


def test_the_pack_language_picks_the_06_wording_not_the_characters():
    """Reviewer regression: one curly apostrophe or accented letter used to switch an English pack to Chinese."""
    en = lambda **kw: _fc(language="en", **kw)  # noqa: E731
    assert script_facts.first_conversation_topic(en(label="Café loyalty points")) == "🗣️  Asking about Café loyalty points..."
    assert script_facts.memory_notice(en(unknown_context=("customer’s tier", "order history"))) == (
        "Notice: The answer is GENERIC — the Agent doesn't know your customer’s tier or order history yet.")
    assert script_facts.memory_notice(en(unknown_context=("storage temperature (°C)",))).startswith("Notice: ")
    zh = lambda **kw: _fc(language="zh-CN", **kw)  # noqa: E731
    # hr-default: a zh-CN pack whose label and items are ASCII keeps the upstream English lines ...
    hr = zh(label="annual leave policy", unknown_context=("tenure", "department", "specific leave balance"))
    assert script_facts.first_conversation_topic(hr) == "🗣️  Asking about annual leave policy..."
    assert script_facts.memory_notice(hr).startswith("Notice: The answer is GENERIC")
    # ... any Chinese in either one makes both lines Chinese (ASCII acronyms included).
    mixed = zh(label="会员积分", unknown_context=("SKU", "VIP"))
    assert script_facts.first_conversation_topic(mixed) == "🗣️  正在询问：会员积分..."
    assert script_facts.memory_notice(mixed) == "提示：回答是通用的——Agent 还不知道你的SKU、VIP。"
    assert script_facts.console_language(zh(label="VIP points", unknown_context=("会员等级",))) == "zh"
    assert script_facts.console_language(_fc(label="x")) == "legacy"


def test_an_english_pack_with_a_curly_apostrophe_renders_english_06_lines(tmp_path):
    root = tmp_path / "it-helpdesk"
    shutil.copytree(REPO_ROOT / "scenarios" / "it-helpdesk", root)
    path = root / "scenario.yaml"
    text = path.read_text(encoding="utf-8")
    old = "unknownContext: [\"laptop's purchase date\", asset tag]"
    assert old in text
    path.write_text(text.replace(old, "unknownContext: [\"laptop’s purchase date\", asset tag]"), encoding="utf-8")
    scenario = load_scenario(path)
    assert scenario.data["language"] == "en"
    pack = compiler.compile_pack(scenario, tmp_path / "build", template_commit=TEMPLATE_COMMIT)
    release = render.render_release(pack, UPSTREAM, tmp_path / "release", template_commit=TEMPLATE_COMMIT)
    s06 = _text(release, "06-test-conversation.sh")
    assert 'echo "Notice: The answer is GENERIC — the Agent doesn\'t know your laptop’s purchase date or asset tag yet."' in s06
    assert "提示" not in s06 and "正在询问" not in s06


def test_hr_first_conversation_reproduces_the_upstream_lines(released):
    _data, release = released("hr-default")
    s06 = _text(release, "06-test-conversation.sh")
    upstream = (UPSTREAM / "06-test-conversation.sh").read_text(encoding="utf-8")
    for line in ('echo "🗣️  Asking about annual leave policy..."', '--actor-id "employee-001"',
                 "\"I'd like to know about the annual leave policy. How many days am I entitled to and what's the application process?\""):
        assert line in upstream and line in s06
    assert "doesn't know your tenure, department, or specific leave balance yet." in s06  # upstream's sentence, one line


@pytest.mark.parametrize("pack_id,expected", [
    ("hr-default", ["检索质量好：绩效 performance review / 福利 benefits enrollment", "答案被埋没：病假", "仅供参考",
                    "知识库里没有答案：志愿者假期"]),
    ("it-helpdesk", ["Retrieval is good: P2 response target / Account lockout duration / Personal device on VPN",
                     "The knowledge base lacks the answer: VPN session limit", "the prompt fixed honesty, not coverage"]),
    ("maintenance", ["Retrieval is good: Filler lubrication interval / P1 response target", "Cap steriliser UV lamps"]),
])
def test_optimize_closing_reads_the_teaching_contrast_per_mechanism(pack_id, expected, released):
    data, release = released(pack_id)
    lines = render.optimize_closing_lines(data, script_facts.compute(data))
    s10 = _text(release, "10-optimize-prompt.sh")
    for line in lines:
        assert f"echo {render.bash_quote(line)}\n" in s10
    joined = "\n".join(lines)
    for fragment in expected:
        assert fragment in joined, fragment
    assert "content 072" not in s10.split("Phase 5 完成")[-1]
    # "stays Fail" only when a buried gap is the pack's sole kind of gap; next to an absent gap it is advisory.
    mechanisms = {p.get("mechanism") for p in teaching.phenomena(data, "retrieval_gap")}
    assert ("仍 Fail" in joined or "stays Fail" in joined) is (mechanisms == {"buried"})


@pytest.mark.parametrize("pack_id", PACKS)
def test_pack_json_carries_the_declared_l1_markers(pack_id, released):
    data, release = released(pack_id)
    pack_json = json.loads((release.release_dir / "pack" / "pack.json").read_text(encoding="utf-8"))
    assert pack_json["l1"] == data["evaluation"]["l1"]
    data = json.loads(json.dumps(data))
    del data["evaluation"]["l1"]
    assert "l1" not in compiler.render_pack_manifest(data, TEMPLATE_COMMIT, [])


def test_render_refuses_a_pack_without_probes(released, tmp_path):
    data, _release = released("maintenance")
    data = json.loads(json.dumps(data))
    data["labs"]["teaching"]["phenomena"] = [p for p in data["labs"]["teaching"]["phenomena"] if p["kind"] not in teaching.PROBE_KINDS]
    facts = script_facts.compute(data)
    assert facts.probe_count == 0
    with pytest.raises(render.RenderError, match=r"\|P\| = 0"):
        render._patch_scripts(tmp_path, data, facts)


def test_no_shared_env_dependency_for_runtime_actors():
    """RUN_TAG and the actors are computed in bash: the SSM RunStep document sets no actor variable."""
    document = (REPO_ROOT / "sync" / "ssm" / "WorkshopCustomizerRunStep.json").read_text(encoding="utf-8")
    assert "ACTOR" not in document and "RUN_TAG" not in document
