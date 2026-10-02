"""Regression checks for public Guide entry points, not only SSM orchestration."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from workshop_customizer import script_facts, teaching
from workshop_customizer.compiler import compile_pack
from workshop_customizer.render import render_release
from workshop_customizer.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    root = tmp_path_factory.mktemp("guide-release")
    commit = json.loads((ROOT / "template-lock.json").read_text())["template"]["commit"]
    pack = compile_pack(load_scenario(ROOT / "scenarios/it-helpdesk/scenario.yaml"),
                        root / "build", template_commit=commit)
    return render_release(pack, ROOT / "upstream", root / "release", template_commit=commit).release_dir


def test_guide_layout_forwards_exact_arguments_and_exit_status(release):
    script = release / "11-cost-latency.sh"
    original = script.read_text()
    try:
        script.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\nexit 17\n')
        result = subprocess.run(["bash", "./11-cost-latency.sh", "1234", "space here"],
                                cwd=release / "static/scripts", text=True, capture_output=True)
        assert result.returncode == 17
        assert result.stdout.splitlines() == ["1234", "space here"]
    finally:
        script.write_text(original)


def test_all_rendered_guide_commands_parse(release):
    for script in release.rglob("*.sh"):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_nova_target_prefix_and_retrieval_filters_agree(release):
    for script in ("09-run-eval.sh", "10-optimize-prompt.sh", "11-cost-latency.sh", "13-judge-stability.sh"):
        text = (release / script).read_text()
        assert "ittools___retrieve_it_policy" in text
        assert "it-tools___retrieve_it_policy" not in text
    assert 'legacy_name.replace("-", "")' in (release / "gateway/create_gateway.py").read_text()
    # 09 waits for / scores the script-facts probe count, and 10 re-scores the same number. The IT pack
    # declares |P| = 4 teaching probes; the count rendered is whatever the active semantics derives
    # (legacy: every practice case requiring the retrieval tool, i.e. 4 probes + the phishing case).
    data = load_scenario(ROOT / "scenarios/it-helpdesk/scenario.yaml").data
    n = script_facts.compute(data).probe_count
    assert len(teaching.probe_case_ids(data)) == 4
    assert f'N={n}' in (release / "09-run-eval.sh").read_text()
    assert f'"$SCRIPT_DIR/09-run-eval.sh" --eval-only {n}' in (release / "10-optimize-prompt.sh").read_text()


def test_explicit_trace_resolves_one_session_across_pages():
    spec = importlib.util.spec_from_file_location("trace_session", ROOT / "engine/workshop_customizer/templates/trace_session.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    trace = "a" * 32

    class Logs:
        def get_paginator(self, operation):
            assert operation == "filter_log_events"
            return self

        def paginate(self, **kwargs):
            assert kwargs["filterPattern"] == f'"{trace}"'
            yield {"events": [{"message": json.dumps({"traceId": "b" * 32, "attributes": {"session.id": "other"}})}]}
            yield {"events": [{"message": json.dumps({"traceId": trace, "attributes": {"session.id": "right"}})}]}

    assert module.session_for_trace(Logs(), trace, start_ms=0) == "right"
    with pytest.raises(ValueError):
        module.session_for_trace(Logs(), "not-a-trace", start_ms=0)


def test_tokens_count_leaf_calls_not_export_duplicates_or_wrapper_totals():
    spec = importlib.util.spec_from_file_location("trace_metrics", ROOT / "engine/workshop_customizer/templates/trace_metrics.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def span(sid, parent, incoming, outgoing):
        return {"spanId": sid, "parentSpanId": parent, "attributes": {
            "gen_ai.usage.input_tokens": incoming, "gen_ai.usage.output_tokens": outgoing,
        }}

    first = span("model1", "chat1", 100, 10)
    second = span("model2", "chat2", 120, 20)
    spans = [
        first, first, second,
        span("chat1", "cycle1", 100, 10), span("chat2", "cycle2", 120, 20),
        span("cycle1", "agent", 0, 0), span("cycle2", "agent", 0, 0),
        span("agent", None, 220, 30),
    ]
    assert module.count_tokens(spans) == (220, 30)
    assert module.count_tokens([span("agent", None, 220, 30)]) == (220, 30)


def test_request_success_without_usable_indexed_evidence_is_not_ready():
    spec = importlib.util.spec_from_file_location("evaluation_ready", ROOT / "engine/workshop_customizer/templates/evaluation_ready.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = {"success": True, "run": {"results": [{"sessionScores": [
        {"label": "Skipped", "value": None},
    ]}]}}
    assert not module.has_usable_score(result)
    result["run"]["results"][0]["sessionScores"] = [{"label": "Fail", "value": 0}]
    assert module.has_usable_score(result)  # A low quality score is still valid evidence.
    result["run"]["results"][0]["sessionScores"] = [{"label": "Error", "value": 0}]
    assert not module.has_usable_score(result)
    assert not module.has_usable_score({"success": False})


def test_cleanup_retry_does_not_query_or_delete_an_unidentified_kb(release, capsys):
    import ast
    from types import SimpleNamespace

    tree = ast.parse((release / "knowledge-base/create_kb.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "delete_kb")
    namespace = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<cleanup>", "exec"), namespace)

    class Client:
        def list_knowledge_bases(self, **kwargs):
            return {"knowledgeBaseSummaries": []}

        def get_knowledge_base(self, **kwargs):
            raise AssertionError("must not call GetKnowledgeBase with a missing ID")

    namespace["delete_kb"](SimpleNamespace(bedrock_agent_client=Client()), "it-knowledge-base")
    assert "already deleted" in capsys.readouterr().out
