"""The rehearsal text table (engine/workshop_customizer/rehearsal_strings.py).

One table, two languages, no prose in rehearsal.py: every human-readable string rehearsal.py produces is a
template here, in zh-CN and en, with the same keys and placeholders, and the Chinese keeps the technical
tokens (GR / SP2 / SQC / THELMA / L1 / Mind the Goal, paths, ids, field names) untranslated.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from workshop_customizer import rehearsal
from workshop_customizer import rehearsal_strings as rs

REPO_ROOT = Path(__file__).resolve().parents[1]
REHEARSAL_PY = REPO_ROOT / "engine" / "workshop_customizer" / "rehearsal.py"
CJK = re.compile(r"[㐀-鿿]")


def test_both_languages_carry_the_same_keys_and_placeholders():
    assert rs.LANGUAGES == ("en", "zh-CN") and set(rs.STRINGS) == set(rs.LANGUAGES)
    assert set(rs.STRINGS["en"]) == set(rs.STRINGS["zh-CN"])
    for key, en in rs.STRINGS["en"].items():
        assert rs.placeholders(en) == rs.placeholders(rs.STRINGS["zh-CN"][key]), key


@pytest.mark.parametrize("language,expected", [
    ("zh-CN", "zh-CN"), ("zh", "zh-CN"), ("zh-TW", "zh-CN"), ("zh_Hans", "zh-CN"), ("ZH-cn", "zh-CN"),
    ("en", "en"), ("en-US", "en"), (None, "en"), ("", "en"), ("fr", "en"), (7, "en"),
])
def test_zh_packs_get_chinese_and_every_other_pack_english(language, expected):
    assert rs.lang_key(language) == expected


def _literal_keys(tree: ast.AST) -> set[str]:
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Msg" and node.args:
            if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                keys.add(node.args[0].value)
            elif isinstance(node.args[0], ast.IfExp):
                keys |= {n.value for n in (node.args[0].body, node.args[0].orelse) if isinstance(n, ast.Constant)}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "text":
            keys |= {a.value for a in node.args[1:2] if isinstance(a, ast.Constant) and isinstance(a.value, str)}
    return keys


def test_every_template_is_used_and_every_used_key_exists():
    """No dead template and no key that only fails at run time (Msg raises on an unknown key)."""
    tree = ast.parse(REHEARSAL_PY.read_text(encoding="utf-8"))
    used = _literal_keys(tree) | {"sep.list", "sep.clause"}  # Join's separators
    # The keys rehearsal.py builds from a code: Msg(f"run.{run}"), Msg(f"case.{code}") for the unusable L1 cells,
    # Msg(f"verdict.{reason_code}") and Msg(f"blocker.{code}").
    used |= {"run.baseline", "run.optimized"} | {f"case.{c}" for c in ("L1_MISSING", "L1_ERROR", "L1_UNRESOLVED")}
    used |= {f"verdict.{c}" for c in ("CONTRASTS_REPRODUCED", "PHENOMENON_NOT_REPRODUCED", "PHENOMENON_INSUFFICIENT")}
    used |= {f"blocker.{b}" for b in rehearsal.BLOCKERS}
    assert used - set(rs.STRINGS["en"]) == set(), "rehearsal.py names a template that does not exist"
    assert set(rs.STRINGS["en"]) - used == set(), "a template is never used"


def _outside_raise_and_docstrings(tree: ast.AST) -> list[str]:
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise):
            skip |= {id(n) for n in ast.walk(node)}
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            skip.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip]


def test_rehearsal_py_writes_no_prose_of_its_own():
    """Every sentence lives in rehearsal_strings.py; what is left in rehearsal.py is machine vocabulary."""
    vocabulary = set(rehearsal.HONESTY_READINGS) | set(rehearsal.SCENARIO_SOURCES) | {"current run"}
    words = re.compile(r"[A-Za-z]{2,}")
    prose = [s for s in _outside_raise_and_docstrings(ast.parse(REHEARSAL_PY.read_text(encoding="utf-8")))
             if " " in s.strip() and len(words.findall(s)) >= 3 and s not in vocabulary]
    assert prose == []
    assert rehearsal.SCOPE_WARNING == rs.text("en", "scope_warning") and rehearsal.READINESS_NOTE == rs.text("en", "contrast.readiness")


#: Templates whose text is the same in both languages (technical vocabulary only).
SAME_IN_BOTH = {"parity.l1"}


def test_the_chinese_is_chinese_with_full_width_sentence_punctuation():
    for key, zh in rs.STRINGS["zh-CN"].items():
        if key in SAME_IN_BOTH:
            assert zh == rs.STRINGS["en"][key]
            continue
        if not re.search(r"[A-Za-z㐀-鿿]", re.sub(r"\{\w+\}", "", zh)):
            continue  # separators and joins: punctuation around placeholders
        assert CJK.search(zh), f"{key}: no Chinese in {zh!r}"
        assert zh != rs.STRINGS["en"][key], key
        # Sentence punctuation next to Chinese is full-width; ASCII ':' / ';' stays only between technical
        # tokens (design: control, mechanism: absent, noise: true).
        assert not re.search(r"[㐀-鿿][;:]|[;:][㐀-鿿]|; ", zh), f"{key}: ASCII punctuation in {zh!r}"
        assert "<=" not in zh and ">=" not in zh and "->" not in zh, f"{key}: use ≤ / ≥ / → in Chinese"


def test_the_chinese_says_what_the_english_says():
    """Reviewed wordings: one description is not split into a list, a blocker says what is confirmed, an
    object belongs to one clause, and an admission names what is admitted."""
    zh = rs.STRINGS["zh-CN"]
    # "noise: true documents carrying the baitTerms" is one kind of document, not two.
    assert "（标了 noise: true 且带 baitTerms 的文档）" in zh["fix.RG_RETRIEVAL_OK.buried"]
    assert "noise: true、" not in zh["fix.RG_RETRIEVAL_OK.buried"]
    # What the manifest confirms: that this build is the run's release (the blocker and its because agree).
    assert zh["blocker.RELEASE_NOT_VERIFIED"] == "没有 Release 清单能确认这次构建就是本次运行的 Release"
    assert zh["why.RELEASE_NOT_VERIFIED"].startswith("没有 Release 清单能确认这次构建就是 Release ")
    assert zh["case.L1_CONTRAST_REPRODUCED"] == "基线没通过关键判据，优化轮通过了"
    assert "优化后的 Agent 承认知识库里没有答案并转交处理" in zh["case.RG_REPRODUCED.absent_admitted"]


#: Every Chinese surface an SA or instructor reads the judge noise band (evaluation.noiseBand) on.
NOISE_BAND_SURFACES = (
    "engine/workshop_customizer/rehearsal_strings.py",
    "engine/workshop_customizer/guide_strings.py",
    "engine/workshop_customizer/templates/guide/student.zh-CN.md",
    "engine/workshop_customizer/templates/guide/instructor.zh-CN.md",
    "app/ui/dist/index.mjs",
)


def test_the_judge_noise_band_has_one_chinese_name():
    """裁判噪声带 in the rehearsal texts, the guide and the UI: never 裁判波动, 评审噪声带 or a bare 噪声带, so the
    value the card shows is the value the reasons compare against (max(裁判噪声带, 0.05))."""
    zh = rs.STRINGS["zh-CN"]
    assert "max(裁判噪声带, {min})" in zh["case.PF_NO_GAIN"] and zh["fix.JUDGE_TOO_NOISY"].startswith("裁判噪声带太宽")
    for rel in NOISE_BAND_SURFACES:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "裁判噪声带" in text, rel
        assert not re.search(r"(?<!裁判)噪声带|裁判波动|超出波动|评审噪声", text), (rel, re.findall(r".{12}(?<!裁判)噪声带.{4}|裁判波动|超出波动|评审噪声", text))


#: A technical token: a path, a dotted / bracketed field path, a snake_case or camelCase identifier, a CLI flag.
TOKEN = re.compile(r"--[a-z][\w-]*|[A-Za-z_][\w.*\[\]/=-]*[\w*\]]")
NAMED = {"GR", "SP1", "SP2", "SQC", "THELMA", "L1", "RunStep", "Fail", "FAQ", "Memory", "Release", "HR"}


def _technical(text: str) -> set[str]:
    tokens = set()
    for token in TOKEN.findall(text):
        if token == "n/a":  # the English word for a missing value, not a token
            continue
        if token in NAMED or token.startswith("--") or re.search(r"[._/\[\]*]|\d|[a-z][A-Z]", token):
            tokens.add(token)
    if "Mind the Goal" in text:
        tokens.add("Mind the Goal")
    return tokens


def test_the_chinese_keeps_every_technical_token():
    for key, en in rs.STRINGS["en"].items():
        zh = rs.STRINGS["zh-CN"][key]
        missing = {t for t in _technical(en) if t not in zh}
        assert not missing, f"{key}: {sorted(missing)} dropped from {zh!r}"


def test_rendering_nests_localized_values_and_joins_with_the_language_separator():
    msg = rs.Msg("case.RG_RETRIEVAL_OK", run=rs.Msg("run.baseline"), sp2="0.56", sqc=rs.Msg("value.none"), sp2max="0.2", sqcmax="0.3")
    assert msg.render("en") == "retrieval found the answer in the baseline run (SP2 0.56, SQC n/a; a gap needs SP2 <= 0.2 or SQC < 0.3)"
    assert msg.render("zh-CN") == "基线轮检索找到了答案（SP2 0.56，SQC 缺失；构成缺口要求 SP2 ≤ 0.2 或 SQC < 0.3）"
    terms = rs.Join(["接入所需时长", "接入完成天数"])
    assert rs.render(terms, "en") == "接入所需时长, 接入完成天数" and rs.render(terms, "zh-CN") == "接入所需时长、接入完成天数"
    given = rs.Given("中文", "English")
    assert (given.render("zh-CN"), given.render("en")) == ("中文", "English")
    assert rs.render(None, "zh-CN") == "" and rs.render(0.5, "en") == "0.5"
    with pytest.raises(KeyError):
        rs.Msg("no.such.key")


def test_localize_writes_the_pack_language_and_an_english_twin():
    doc = {"reason": rs.Msg("verdict.RUN_INCOMPLETE"), "code": "RUN_INCOMPLETE", "items": [{"note": rs.Msg("advisory.noise_note"), "n": 1}]}
    zh = rs.localize(doc, "zh-CN")
    assert zh == {"reason": "这个 Release 还没有一次完整运行", "reasonEn": "no complete run of this release yet", "code": "RUN_INCOMPLETE",
                  "items": [{"note": rs.text("zh-CN", "advisory.noise_note"), "noteEn": rs.text("en", "advisory.noise_note"), "n": 1}]}
    en = rs.localize(doc, "en")
    assert en["reason"] == en["reasonEn"] == "no complete run of this release yet"
    assert isinstance(doc["reason"], rs.Msg)  # the input is not modified
