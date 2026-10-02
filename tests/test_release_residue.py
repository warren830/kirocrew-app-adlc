"""Participant-visible HR wording from the pinned upstream Workshop must not reach a non-HR release.

The upstream scripts print (and register with AWS) HR phrasing that the older patches left alone,
so every scenario showed "Generating HR policy documents", an "HR policy KB" description and an
HR-flavoured memory notice. These anchors pin the replacements; ``expect=1`` in render fails the
build if the pinned upstream text ever moves.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from workshop_customizer import compiler, render, script_facts, validator
from workshop_customizer.scenario import load_scenario
from workshop_customizer.validator import PROSE_RESIDUE_WORDS, TEXT_SUFFIXES

REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = REPO_ROOT / "upstream"
TEMPLATE_COMMIT = json.loads((REPO_ROOT / "template-lock.json").read_text())["template"]["commit"]

# Upstream strings a participant sees on the console or in the AWS console, never in a non-HR release.
UPSTREAM_HR_PHRASES = (
    'Generating HR policy documents',
    'KB_DESCRIPTION = "HR policy KB for AgentCore workshop"',
    "doesn't know your tenure",
    "specific leave balance",
    "First Conversation with HR Agent",
    "Deploying HR Tools Lambda",
    "Resolving HR Tools Lambda ARN",
    "返回内置示例 HR 政策",
    'Description="Allows AgentCore Gateway to invoke the HR Tools Lambda"',
    "Asking about annual leave policy",
    # AWS console descriptions and the helper / header comments (they named the HR generator and questions).
    'description="HR assistant tools gateway"',
    'description="HR tools backed by Lambda"',
    "HR Tools Lambda",
    "生成 HR 政策",
    # The Mind the Goal judge prompt (SPEC D5): the HR framing and the "Employee:" dialog label.
    "employee experience chatbot",
    "dialog from an employee chatbot",
    "  Employee: {t['",
)
PATCHED_FILES = ("01-create-kb.sh", "02-create-gateway.sh", "06-test-conversation.sh", "09-run-eval.sh", "10-optimize-prompt.sh",
                 "knowledge-base/create_kb.py", "gateway/create_gateway.py", render.MTG_PROMPTS_REL)
#: Printed (echo) wording of the HR Workshop's own golden questions and content pages (SPEC D6e).
PRINTED_HR_WORDING = ("绩效", "福利", "病假", "content 063", "content 071", "content 072", "三个 golden", "3 个 golden", "3 个对话")


@pytest.fixture(scope="module", params=["it-helpdesk", "maintenance"])
def release(request, tmp_path_factory):
    base = tmp_path_factory.mktemp(f"residue-{request.param}")
    scenario = load_scenario(REPO_ROOT / "scenarios" / request.param / "scenario.yaml")
    pack = compiler.compile_pack(scenario, base / "build", template_commit=TEMPLATE_COMMIT)
    rendered = render.render_release(pack, UPSTREAM, base / "release", template_commit=TEMPLATE_COMMIT)
    return scenario.data, rendered.release_dir


def test_upstream_hr_phrases_are_replaced(release):
    data, root = release
    texts = {rel: (root / rel).read_text(encoding="utf-8") for rel in PATCHED_FILES}
    leaks = [(rel, phrase) for rel, text in texts.items() for phrase in UPSTREAM_HR_PHRASES if phrase in text]
    assert leaks == []
    assert "Copying the scenario's knowledge documents" in texts["01-create-kb.sh"]
    assert f"KB_DESCRIPTION = '{data['id']} knowledge base for AgentCore workshop'" in texts["knowledge-base/create_kb.py"]
    notice = script_facts.memory_notice(script_facts.compute(data).first_conversation)
    assert f'echo "{notice}"' in texts["06-test-conversation.sh"].replace("\\'", "'")
    assert notice.startswith("Notice: The answer is GENERIC — the Agent doesn't know your ")
    assert data["displayName"] in texts["06-test-conversation.sh"]
    assert "Deploying the scenario tools Lambda" in texts["02-create-gateway.sh"]
    assert "invoke the scenario tools Lambda" in texts["gateway/create_gateway.py"]
    assert f'description="{data["id"]} assistant tools gateway",' in texts["gateway/create_gateway.py"]
    for rel in ("09-run-eval.sh", "10-optimize-prompt.sh"):  # no HR topic word left in their comments either
        assert not [w for w in ("绩效", "福利", "病假", "content 071", "content 072") if w in texts[rel]], rel


def test_release_text_has_no_engine_residue_words(release):
    _data, root = release
    hits = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.suffix in TEXT_SUFFIXES:
            low = path.read_text(encoding="utf-8", errors="replace").lower()
            hits += [(path.relative_to(root).as_posix(), w) for w in PROSE_RESIDUE_WORDS if w.lower() in low]
    assert hits == []


def test_patched_shell_scripts_still_parse(release):
    _data, root = release
    for rel in ("01-create-kb.sh", "02-create-gateway.sh", "06-test-conversation.sh"):
        result = subprocess.run(["bash", "-n", str(root / rel)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (rel, result.stderr)


def test_release_scripts_print_no_hr_workshop_wording(release):
    """SPEC D6e: 09/10/12 print the pack's counts and cases, never 绩效/福利/病假 or 'content 06x/07x'."""
    data, root = release
    printed = [
        (path.name, line) for path in sorted(root.glob("*.sh"))
        for line in path.read_text(encoding="utf-8").splitlines() if line.lstrip().startswith(("echo", "printf", "print("))
    ]
    assert [(name, line) for name, line in printed if any(w in line for w in PRINTED_HR_WORDING)] == []
    assert validator.check_script_residue(data, root, sources_text=(REPO_ROOT / "scenarios" / data["id"] / "scenario.yaml").read_text()).findings == []
    s10 = (root / "10-optimize-prompt.sh").read_text(encoding="utf-8")
    assert "the prompt fixed honesty, not coverage" in s10  # IT and maintenance declare absent gaps


def test_script_residue_gate_blocks_a_printed_hr_line_and_allows_pack_wording(release, tmp_path):
    data, root = release
    copy = tmp_path / "release"
    for path in root.glob("*.sh"):
        (copy / path.name).parent.mkdir(parents=True, exist_ok=True)
        (copy / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    target = copy / "12-compare-models.sh"
    target.write_text(target.read_text(encoding="utf-8") + 'echo "（content 063 的表）并排比"\necho "Check your annual leave rules"\n# 绩效 in a comment is fine\n', encoding="utf-8")
    findings = validator.check_script_residue(data, copy, sources_text="")
    assert [(f.code, f.path) for f in findings.findings] == [("residue.script", "12-compare-models.sh")] * 2
    assert all(f.severity == "error" and f.scopes == () and f.line for f in findings.findings)
    # A word the pack's own sources use is pack content, not residue; 'content 06x' never is.
    allowed = validator.check_script_residue(data, copy, sources_text="our annual leave rules")
    assert [f.message for f in allowed.findings] == [findings.findings[0].message]
    # The HR reference pack is exempt.
    assert validator.check_script_residue({"id": "hr-default"}, copy).findings == []


def test_residue_gate_covers_aws_descriptions_in_release_helpers(release, tmp_path):
    """An AWS resource description a helper passes shows in the console: HR wording there is residue."""
    data, root = release
    copy = tmp_path / "release"
    (copy / "gateway").mkdir(parents=True)
    helper = (root / "gateway" / "create_gateway.py").read_text(encoding="utf-8")
    (copy / "gateway" / "create_gateway.py").write_text(helper, encoding="utf-8")
    assert validator.check_script_residue(data, copy).findings == []
    (copy / "gateway" / "create_gateway.py").write_text(
        helper.replace(f'description="{data["id"]} assistant tools gateway"', 'description="HR assistant tools gateway"'), encoding="utf-8")
    [finding] = validator.check_script_residue(data, copy).findings
    assert (finding.code, finding.path) == ("residue.script", "gateway/create_gateway.py") and finding.line == 170
