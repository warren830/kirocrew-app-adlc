"""Deterministic pack compilation.

``compile_pack`` turns a validated :class:`Scenario` into two trees:

``<out>/pack/``        student-facing runtime inputs — namespace env, Gateway tool schema, mock
                       fixtures, knowledge documents, skills, prompts, the *practice* golden set and
                       the generated student Workshop Guide (``labs/student-guide.md``, the release's
                       ``README.md``).
``<out>/instructor/``  facilitator-only material — the *holdout* golden set, a provenance report,
                       the generated instructor guide and a frozen copy of the scenario. Distributed
                       separately; never synced to the student EC2.

Every output byte derives from the scenario or from files it references, so compiling the same
scenario twice yields identical files (verified by tests through SHA-256). ``checksums.json``
and ``derivation.json`` record, per output file, its hash and the scenario paths it came from.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import __version__, teaching
from .scenario import Scenario, item_class, provenance_policy, resolve_inside
from .validator import PackValidationError, ValidationReport, check_secret_residue, validate_output_tree, validate_scenario_policy

PACK_DIR = "pack"
INSTRUCTOR_DIR = "instructor"
LAMBDA_MODULE = "scenario_tools_handler"  # pack-independent module name used by rendered scripts


def _dump_json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class CompiledPack:
    scenario: Scenario
    out_dir: Path
    files: dict[str, str] = field(default_factory=dict)  # relative path -> sha256
    derivation: dict[str, list[str]] = field(default_factory=dict)  # relative path -> scenario paths
    report: ValidationReport = field(default_factory=ValidationReport)

    @property
    def pack_dir(self) -> Path:
        return self.out_dir / PACK_DIR

    @property
    def instructor_dir(self) -> Path:
        return self.out_dir / INSTRUCTOR_DIR


class _Writer:
    """Writes files under ``out_dir`` and records hash + derivation for each one."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.files: dict[str, str] = {}
        self.derivation: dict[str, list[str]] = {}

    def _target(self, rel: str) -> Path:
        target = resolve_inside(self.out_dir, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def text(self, rel: str, content: str, derived_from: list[str]) -> None:
        target = self._target(rel)
        target.write_text(content, encoding="utf-8")
        self._record(rel, target, derived_from)

    def copy(self, rel: str, source: Path, derived_from: list[str]) -> None:
        target = self._target(rel)
        shutil.copyfile(source, target)
        self._record(rel, target, derived_from)

    def _record(self, rel: str, target: Path, derived_from: list[str]) -> None:
        self.files[rel] = sha256_file(target)
        self.derivation[rel] = sorted(set(derived_from))


# ---------------------------------------------------------------------------
# Renderers for individual artifacts
# ---------------------------------------------------------------------------

#: Agent Skills metadata limit (the Harness's Strands skill loader reads name + description from the frontmatter).
SKILL_DESCRIPTION_MAX = 1024


def skill_markdown(name: str, text: str) -> str:
    """The SKILL.md the Harness can load: as written when it starts with ``---`` frontmatter, else with one built
    from the skill name and the first paragraph of its body. Live 2026-10-01 every Kiro-generated pack's skill
    lacked it and the runtime logged "failed to load skill: SKILL.md must start with --- frontmatter delimiter",
    then "no skills were loaded": the agent ran with no skills at all."""
    body = text.lstrip("\ufeff")
    if body.startswith("---"):
        return text
    paragraphs = [" ".join(line.strip() for line in block.splitlines()).strip()
                  for block in body.split("\n\n") if block.strip() and not block.lstrip().startswith("#")]
    title = next((line.lstrip("# ").strip() for line in body.splitlines() if line.startswith("#")), name)
    description = (paragraphs[0] if paragraphs else title).replace("\\", "/").replace('"', "'")[:SKILL_DESCRIPTION_MAX]
    return f'---\nname: {name}\ndescription: "{description}"\n---\n{body}'



def render_pack_env(data: dict[str, Any], template_commit: str) -> str:
    ns = data["namespace"]
    skills = " ".join(s["name"] for s in data.get("skills", []))
    lines = [
        "# Generated by workshop-customizer — do not edit; regenerate from scenario.yaml",
        f"WS_PACK_ID={data['id']}",
        f"WS_PACK_KIND={data['packKind']}",
        f"WS_AGENT_NAME={ns['agentName']}",
        f"WS_TOOL_TARGET={ns['toolTargetName']}",
        f"WS_GATEWAY_NAME={ns['gatewayName']}",
        f"WS_KB_NAME={ns['knowledgeBaseName']}",
        f"WS_KB_PREFIX={ns['kbPrefix']}",
        f"WS_SSM_PREFIX={ns['ssmParameterPrefix']}",
        f"WS_LAMBDA_NAME={ns['lambdaFunctionName']}",
        f"WS_LAMBDA_MODULE={LAMBDA_MODULE}",
        f"WS_RETRIEVAL_TOOL={data['evaluation']['retrievalToolName']}",
        f"WS_JUDGE_MODEL={data['evaluation'].get('judgeModel', 'us.amazon.nova-2-lite-v1:0')}",
        f'WS_SKILLS="{skills}"',
        f"WS_TEMPLATE_COMMIT={template_commit}",
    ]
    return "\n".join(lines) + "\n"


def render_tool_schema(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Gateway inline tool schema (same shape as upstream ``hr-tools-schema.json``)."""
    return [
        {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
        for t in data["tools"]
    ]


def render_fixtures(data: dict[str, Any]) -> dict[str, Any]:
    """Fixture bundle consumed by the generic Lambda handler."""
    ns = data["namespace"]
    tools: dict[str, Any] = {}
    for tool in data["tools"]:
        entry: dict[str, Any] = {"kind": tool["kind"], "description": tool["description"]}
        if tool["kind"] == "mock":
            entry["fixtures"] = tool["fixtures"]
        tools[tool["name"]] = entry
    return {
        "packId": data["id"],
        "retrievalToolName": data["evaluation"]["retrievalToolName"],
        "kbIdSsmParameter": f"{ns['ssmParameterPrefix']}/knowledge_base_id",
        "tools": tools,
    }


def split_golden_set(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cases = data["evaluation"]["goldenSet"]
    practice = [c for c in cases if c["set"] == "practice"]
    holdout = [c for c in cases if c["set"] == "holdout"]
    return practice, holdout


#: The practice-case fields that ship in pack/golden/practice.json: what L1 (l1_eval.py) and the release
#: scripts read, plus the provenance tag. Who confirmed a case and where (confirmedBy, confirmedAt,
#: confirmationRef) and its origin (material ids, SA notes) are instructor-only (D10) and never ship.
PRACTICE_CASE_FIELDS: tuple[str, ...] = ("id", "label", "query", "category", "set", "actorId", "roleId", "expected",
                                         "basis", "criticality", "provenance")


def student_practice_cases(practice: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The practice cases as they ship to participants (:data:`PRACTICE_CASE_FIELDS` only, schema order)."""
    return [{key: case[key] for key in PRACTICE_CASE_FIELDS if key in case} for case in practice]


def render_pack_manifest(data: dict[str, Any], template_commit: str, practice: list[dict[str, Any]]) -> dict[str, Any]:
    manifest = {
        "packId": data["id"],
        "displayName": data["displayName"],
        "packKind": data["packKind"],
        "language": data["language"],
        "namespace": data["namespace"],
        "lambdaModule": LAMBDA_MODULE,
        "retrievalToolName": data["evaluation"]["retrievalToolName"],
        "judgeModel": data["evaluation"].get("judgeModel", "us.amazon.nova-2-lite-v1:0"),
        "templateCommit": template_commit,
        "generatorVersion": __version__,
        "skills": [s["name"] for s in data.get("skills", [])],
        "documents": [{"id": d["id"], "title": d["title"], "file": Path(d["file"]).name} for d in data["knowledge"]["documents"]],
        "tools": [{"name": t["name"], "kind": t["kind"]} for t in data["tools"]],
        "practiceCases": [
            {"id": c["id"], "label": c["label"], "query": c["query"], "actorId": c["actorId"], "category": c["category"]}
            for c in practice
        ],
        "observations": data.get("labs", {}).get("observations", []),
    }
    l1 = data["evaluation"].get("l1")
    if l1:  # L1 marker rules read by the release's l1_eval.py (absent → built-in refusal markers only)
        manifest["l1"] = l1
    student_teaching = teaching.student_view(data)
    if student_teaching is not None:  # no defects, bait or absent terms: those are instructor-only
        manifest["teaching"] = student_teaching
    return manifest


PROVENANCE_BANNERS = {
    "reference": "Fictional reference pack — no statement is a customer fact.",
    "customer": "Customer pack — only customer-fact rows may be presented as the customer's policy.",
    "workshop": ("Workshop pack — only customer-fact rows may be presented as the customer's policy; every other "
                 "row is a synthetic teaching setting (合成教学设定)."),
}


def _origin_text(item: dict[str, Any]) -> str:
    origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
    kind = str(origin.get("kind") or "")
    materials = ", ".join(origin.get("materials") or [])
    return f"{kind} ({materials})" if kind and materials else kind


def render_provenance_report(scenario: Scenario) -> str:
    data = scenario.data
    policy = provenance_policy(data)
    lines = [
        f"# Provenance report — {data['displayName']} ({data['id']})",
        "",
        f"> {PROVENANCE_BANNERS.get(data['packKind'], PROVENANCE_BANNERS['customer'])}",
        "",
        f"- Pack kind: `{data['packKind']}`",
        f"- Facts: {len(data['facts'])}  Tools: {len(data['tools'])}  Documents: {len(data['knowledge']['documents'])}  Golden cases: {len(data['evaluation']['goldenSet'])}",
        "",
        "## Provenance policy",
        "",
        f"- Policy: `{policy['packKind']}` — {policy['rule']}",
        "- Classes: " + ", ".join(f"{name} {count}" for name, count in policy["classes"].items()),
        "- Customer anchors: " + "; ".join(
            f"{category}: {', '.join(ids) if ids else 'none'}" for category, ids in policy["anchors"].items()),
    ]
    if policy["anchorsMissing"]:
        lines.append("- Missing anchor categories (blocks a workshop build): " + ", ".join(policy["anchorsMissing"]))
    lines += [
        "",
        "## Facts",
        "",
        "| id | criticality | provenance | class | origin | confirmed by | reference |",
        "|---|---|---|---|---|---|---|",
    ]
    for fact in data["facts"]:
        lines.append(
            f"| {fact['id']} | {fact['criticality']} | {fact['provenance']} | {item_class(fact)} | {_origin_text(fact)} | {fact.get('confirmedBy', '')} | {fact.get('confirmationRef', fact.get('source', ''))} |"
        )
    lines += ["", "## Tools and documents", "", "| item | provenance | class | origin |", "|---|---|---|---|"]
    for tool in data["tools"]:
        lines.append(f"| tool {tool['name']} | {tool['provenance']} | {item_class(tool)} | {_origin_text(tool)} |")
    for doc in data["knowledge"]["documents"]:
        noise = " (noise)" if doc.get("noise") else ""
        lines.append(f"| document {doc['id']}{noise} | {doc['provenance']} | {item_class(doc)} | {_origin_text(doc)} |")
    lines += ["", "## Golden cases", "", "| id | set | category | provenance | class | origin | basis |", "|---|---|---|---|---|---|---|"]
    for case in data["evaluation"]["goldenSet"]:
        lines.append(
            f"| {case['id']} | {case['set']} | {case['category']} | {case['provenance']} | {item_class(case)} | {_origin_text(case)} | {', '.join(case['basis'])} |"
        )
    lines += ["", "## Waived gate findings", ""]
    if scenario.waived_items:
        for v in scenario.waived_items:
            exc = v.exception or {}
            lines.append(
                f"- {v.item_type} `{v.item_id}`: {v.reason} — waived by {', '.join(exc.get('approvedBy', []))} on {exc.get('approvedAt', '?')}: {exc.get('reason', '')}"
            )
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def compile_pack(scenario: Scenario, out_dir: Path, *, template_commit: str, strict_prose: bool = False) -> CompiledPack:
    """Compile ``scenario`` into ``out_dir`` (created; existing pack/instructor trees are replaced)."""
    data = scenario.data
    policy = validate_scenario_policy(data, root=scenario.root)
    if not policy.ok:
        raise PackValidationError(policy)

    out_dir = Path(out_dir)
    for sub in (PACK_DIR, INSTRUCTOR_DIR):
        target = out_dir / sub
        if target.exists():
            shutil.rmtree(target)
    out_dir.mkdir(parents=True, exist_ok=True)
    writer = _Writer(out_dir)

    practice, holdout = split_golden_set(data)

    # -- student-facing pack ------------------------------------------------
    writer.text(f"{PACK_DIR}/pack.env", render_pack_env(data, template_commit), ["namespace", "evaluation.retrievalToolName", "skills"])
    writer.text(
        f"{PACK_DIR}/pack.json",
        _dump_json(render_pack_manifest(data, template_commit, practice)),
        ["id", "namespace", "skills", "knowledge.documents", "tools", "evaluation.goldenSet[set=practice]", "labs.observations"]
        + (["evaluation.l1"] if data["evaluation"].get("l1") else [])
        + (["labs.teaching"] if teaching.declared(data) else []),
    )
    writer.text(f"{PACK_DIR}/tools/schema.json", _dump_json(render_tool_schema(data)), ["tools"])
    writer.text(f"{PACK_DIR}/tools/fixtures.json", _dump_json(render_fixtures(data)), ["tools", "namespace.ssmParameterPrefix"])
    writer.text(f"{PACK_DIR}/golden/practice.json", _dump_json(student_practice_cases(practice)), ["evaluation.goldenSet[set=practice]"])

    for doc in data["knowledge"]["documents"]:
        source = resolve_inside(scenario.root, doc["file"])
        writer.copy(f"{PACK_DIR}/knowledge-base/docs/{Path(doc['file']).name}", source, [f"knowledge.documents[{doc['id']}]"])

    for skill in data.get("skills", []):
        source = resolve_inside(scenario.root, skill["file"])
        writer.text(f"{PACK_DIR}/skills/{skill['name']}/SKILL.md", skill_markdown(skill["name"], source.read_text(encoding="utf-8")),
                    [f"skills[{skill['name']}]"])

    prompts = data["prompts"]
    writer.copy(f"{PACK_DIR}/prompts/baseline.md", resolve_inside(scenario.root, prompts["baselineFile"]), ["prompts.baselineFile"])
    writer.copy(
        f"{PACK_DIR}/prompts/optimization-candidate.md",
        resolve_inside(scenario.root, prompts["optimizationCandidateFile"]),
        ["prompts.optimizationCandidateFile"],
    )

    # -- Workshop Guide (SPEC D10): the student guide ships (release README.md), the instructor guide never.
    # labs.studentGuideFile / instructorGuideFile are embedded as supplements, not copied.
    from . import guide  # lazy: guide reaches render (optimize_closing_lines), which imports this module

    guides = guide.build_guides(scenario, template_commit=template_commit, generator_version=__version__, warnings=policy.warnings)
    writer.text(guide.STUDENT_GUIDE_PATH, guides.student, list(guides.student_sources))
    writer.text(guide.INSTRUCTOR_GUIDE_PATH, guides.instructor, list(guides.instructor_sources))

    # -- instructor bundle ----------------------------------------------------
    writer.text(f"{INSTRUCTOR_DIR}/golden/holdout.json", _dump_json(holdout), ["evaluation.goldenSet[set=holdout]"])
    writer.text(f"{INSTRUCTOR_DIR}/golden/all-cases.json", _dump_json(data["evaluation"]["goldenSet"]), ["evaluation.goldenSet"])
    writer.text(f"{INSTRUCTOR_DIR}/provenance-report.md", render_provenance_report(scenario),
                ["facts", "tools", "knowledge.documents", "evaluation.goldenSet", "governance.exceptions"])
    instructor_teaching = teaching.instructor_view(data)
    if instructor_teaching is not None:
        writer.text(f"{INSTRUCTOR_DIR}/teaching.json", _dump_json(instructor_teaching), ["labs.teaching", "evaluation.goldenSet"])
    writer.copy(f"{INSTRUCTOR_DIR}/scenario-snapshot{scenario.path.suffix}", scenario.path, ["<scenario file>"])

    # -- policy checks on the generated trees ----------------------------------
    report = validate_output_tree(data, student_root=out_dir / PACK_DIR, source_root=scenario.root)
    report.findings.extend(guides.findings)
    report.extend(guide.check_guides(data, guides, sources_text=guide.pack_sources_text(scenario), root=scenario.root))
    report.extend(check_secret_residue(out_dir / INSTRUCTOR_DIR))
    if strict_prose and report.warnings:
        report.findings = [replace(f, severity="error") if f.severity == "warning" else f for f in report.findings]
    if not report.ok:
        raise PackValidationError(report)

    # -- bookkeeping ------------------------------------------------------------
    checksums = {"algorithm": "sha256", "files": dict(sorted(writer.files.items()))}
    (out_dir / "checksums.json").write_text(_dump_json(checksums), encoding="utf-8")
    (out_dir / "derivation.json").write_text(_dump_json(dict(sorted(writer.derivation.items()))), encoding="utf-8")

    return CompiledPack(scenario=scenario, out_dir=out_dir, files=writer.files, derivation=writer.derivation, report=report)
