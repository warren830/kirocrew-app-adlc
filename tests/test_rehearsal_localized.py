"""The live rehearsals of the two acceptance scenarios, replayed through the localized engine.

docs/replace-generic/evidence/{live-loyalty-points,live-ops-support}-2026-09-28/rehearsal-r{1,2}-*.json were
written by the English-only engine from real Workshop runs. Replayed now (tests/rehearsal_replay.py):

* every verdict, code, status, severity, asset, number and id is identical, for the pack's language (zh-CN)
  and for the same evidence judged as an English pack: only the text changes;
* the English twins (``<field>En``) are exactly the recorded text, and an English pack's text is its twin;
* the Chinese text and the instructor-guide rehearsal section are pinned in tests/golden/rehearsal-text/
  (``WSC_UPDATE_GOLDENS=1`` rewrites them after review).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import rehearsal_replay as rr
from workshop_customizer import guide, rehearsal

GOLDEN = rr.REPO_ROOT / "tests" / "golden" / "rehearsal-text"
UPDATE = os.environ.get("WSC_UPDATE_GOLDENS") == "1"
CJK_RUN = re.compile(r"[㐀-鿿]+")
#: What each recorded rehearsal decided (the evidence READMEs): the first round not ready, the second ready for class.
EXPECTED = {
    ("live-loyalty-points-2026-09-28", "rehearsal-r1-not-ready.json"): ("not_ready", False),
    ("live-loyalty-points-2026-09-28", "rehearsal-r2-ready.json"): ("ready", True),
    ("live-ops-support-2026-09-28", "rehearsal-r1-not-ready.json"): ("not_ready", False),
    ("live-ops-support-2026-09-28", "rehearsal-r2-ready.json"): ("ready", True),
}


def _replay(directory: str, name: str, language: str | None = None) -> tuple[dict, dict, dict]:
    rec, args, kwargs = rr.replay_inputs(directory, name, language=language)
    return rec, rehearsal.build_rehearsal(args["state"], args["scenario"], **kwargs), args["scenario"]


def _unversioned(doc: dict) -> dict:
    return {**rr.verdict_projection(doc), "schema": None}


def test_the_recorded_evidence_keeps_its_schema_version():
    """Adding language and the English twins made rehearsal documents a new version: the engine writes
    workshop-customizer/rehearsal/2 (schemas/rehearsal.schema.json). Every recorded rehearsal under
    docs/replace-generic/evidence/ was written by the English-only engine and stays what it declares,
    workshop-customizer/rehearsal/1: valid against schemas/rehearsal-1.schema.json (the version-1 schema,
    unchanged), and not a version-2 document."""
    assert (rehearsal.SCHEMA, rehearsal.SCHEMA_V1) == ("workshop-customizer/rehearsal/2", "workshop-customizer/rehearsal/1")
    for version, path in rehearsal.SCHEMA_PATHS.items():
        assert json.loads(path.read_text(encoding="utf-8"))["properties"]["schema"] == {"const": version}
    records = sorted((rr.REPO_ROOT / "docs" / "replace-generic" / "evidence").glob("*/rehearsal*.json"))
    v1 = [p for p in records if json.loads(p.read_text(encoding="utf-8"))["schema"] == rehearsal.SCHEMA_V1]
    assert len(v1) == 6  # hr-default control and refresh, and two rounds of each acceptance scenario (09-28)
    for path in set(records) - set(v1):  # recorded after the localization: version 2, valid as such
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["schema"] == rehearsal.SCHEMA and doc["language"] and rehearsal.validate_document(doc) == [], path
    for path in v1:
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["schema"] == rehearsal.SCHEMA_V1 and "language" not in doc, path
        assert rehearsal.validate_document(doc) == [], path
        as_v2 = rehearsal.validate_document({**doc, "schema": rehearsal.SCHEMA})
        assert "<root>: 'language' is a required property" in as_v2 and "<root>: 'reasonEn' is a required property" in as_v2, path
    # A version-2 document is not a version-1 one, and an unknown version is refused.
    _, zh, _ = _replay(*rr.RECORDS[0])
    assert rehearsal.validate_document({**zh, "schema": rehearsal.SCHEMA_V1}) != []
    assert any("schema" in e for e in rehearsal.validate_document({**zh, "schema": "workshop-customizer/rehearsal/9"}))


def test_the_two_acceptance_scenarios_are_replayed():
    assert set(rr.RECORDS) == set(EXPECTED)


@pytest.mark.parametrize("directory,name", rr.RECORDS)
def test_replayed_verdicts_are_identical_in_both_languages(directory, name):
    recorded, zh, _ = _replay(directory, name)
    _, en, _ = _replay(directory, name, language="en")
    assert (zh["language"], en["language"]) == ("zh-CN", "en")
    assert rehearsal.validate_document(zh) == rehearsal.validate_document(en) == []
    # The replay is a version-2 document; the record stays version 1 (test_the_recorded_evidence_keeps_its_schema_version).
    assert (zh["schema"], en["schema"], recorded["schema"]) == (rehearsal.SCHEMA, rehearsal.SCHEMA, rehearsal.SCHEMA_V1)
    assert _unversioned(zh) == _unversioned(recorded) == _unversioned(en)
    assert (zh["verdict"], zh["readyForClass"]) == (recorded["verdict"], recorded["readyForClass"]) == EXPECTED[(directory, name)]
    assert [(h["id"], h["code"], h["severity"]) for h in zh["remediation"]] == [(h["id"], h["code"], h["severity"]) for h in recorded["remediation"]]


@pytest.mark.parametrize("directory,name", rr.RECORDS)
def test_the_english_twins_are_the_recorded_text(directory, name):
    recorded, zh, _ = _replay(directory, name)
    _, en, _ = _replay(directory, name, language="en")
    before = {where: text for where, text, _ in rr.text_fields(recorded)}
    twins = {where: twin for where, _, twin in rr.text_fields(zh)}
    assert before and {where: twins[where] for where in before} == before
    # blockerTexts is new; every other text field was already there.
    assert {w for w in twins if w not in before} <= {w for w in twins if ".blockerTexts[" in w}
    assert all(twin is not None for twin in twins.values())
    # An English pack writes English, and its twins repeat it.
    assert [(w, t) for w, t, _ in rr.text_fields(en)] == [(w, t) for w, t in twins.items()]
    assert all(t == twin for _, t, twin in rr.text_fields(en))


@pytest.mark.parametrize("directory,name", rr.RECORDS)
def test_a_chinese_pack_gets_chinese_text(directory, name):
    _, zh, _ = _replay(directory, name)
    for where, text, twin in rr.text_fields(zh):
        assert CJK_RUN.search(text), f"{where}: {text!r} is not Chinese"
        assert text != twin, where


def _snapshot(directory: str, name: str) -> dict:
    _, zh, _ = _replay(directory, name)
    return {
        "_comment": ("Chinese and English text of a live rehearsal replayed through engine/workshop_customizer/rehearsal.py "
                     "(tests/test_rehearsal_localized.py); regenerate with WSC_UPDATE_GOLDENS=1 after review."),
        "record": f"docs/replace-generic/evidence/{directory}/{name}",
        "verdict": zh["verdict"], "reasonCode": zh["reasonCode"], "readyForClass": zh["readyForClass"],
        "texts": [{"at": where, "zh-CN": text, "en": twin} for where, text, twin in rr.text_fields(zh)],
        "instructorGuide": {lang: guide.render_rehearsal_evidence(zh, lang, release_version=zh["releaseVersion"]).splitlines()
                            for lang in ("zh-CN", "en")},
    }


@pytest.mark.parametrize("directory,name", rr.RECORDS)
def test_localized_text_snapshot(directory, name):
    path = GOLDEN / f"{directory}.{Path(name).stem}.json"
    actual = json.dumps(_snapshot(directory, name), ensure_ascii=False, indent=1) + "\n"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
    assert path.is_file(), f"missing {path} (WSC_UPDATE_GOLDENS=1)"
    assert actual == path.read_text(encoding="utf-8"), f"{path.name} changed (WSC_UPDATE_GOLDENS=1 after review)"


@pytest.mark.parametrize("directory,name", rr.RECORDS)
def test_the_instructor_guide_section_reads_the_guide_language(directory, name):
    _, zh, scenario = _replay(directory, name)
    zh_section = guide.render_rehearsal_evidence(zh, "zh-CN", release_version=zh["releaseVersion"])
    en_section = guide.render_rehearsal_evidence(zh, "en", release_version=zh["releaseVersion"])
    for hint in zh["remediation"]:
        assert hint["action"][:40] in zh_section and hint["actionEn"][:40] not in zh_section
        assert hint["actionEn"][:40] in en_section
    assert zh["reason"] in zh_section and zh["reasonEn"] in en_section and zh["scopeWarning"] in zh_section
    # Each warning names what it is about: its phenomenon and case (or runs) lead the message as code spans.
    for warning in zh["warnings"]:
        assert warning["refs"] and warning["message"] in zh_section and warning["messageEn"] in en_section
        for ref in warning["refs"]:
            assert f"`{ref}`" in zh_section and f"`{ref}`" in en_section, (warning["code"], ref)
    # The English section's only Chinese is the pack's own text (an absentTerm, a teaching point).
    pack_text = json.dumps(scenario, ensure_ascii=False)
    assert all(run in pack_text for run in CJK_RUN.findall(en_section)), CJK_RUN.findall(en_section)
