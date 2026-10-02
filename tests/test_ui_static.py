"""Static guards for the hand-written ESM UI (no JS test runner needed).

The UI is the v2 four-step flow (01 场景与课程 · 02 内容与评估 · 03 交付 · 04 彩排与上课, plus 高级). These
tests pin its structure (host import map only, single-props components, API client separated from the
views), that every backend call is a real route of app/backend/server.py or of the in-gateway Kiro
routes in app/backend/routes.py, and the safety copy the flow must keep.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI = REPO_ROOT / "app" / "ui" / "dist" / "index.mjs"
SERVER_PY = REPO_ROOT / "app" / "backend" / "server.py"
ROUTES_PY = REPO_ROOT / "app" / "backend" / "routes.py"
VENDOR = Path("/Applications/KiroCrew.app/Contents/Resources/backend-dist/kirocrew-backend-arm64/lib/python3.12/site-packages/kiro_crew/static/dist/vendor")
HOST_MODULES = ("react", "@kirocrew/app-sdk", "@kirocrew/app-sdk/ui", "lucide-react")
TEXT = UI.read_text(encoding="utf-8")


def _imports(module: str) -> set[str]:
    m = re.search(r"import \{([^}]*)\} from '" + re.escape(module) + "'", TEXT)
    return {n.strip() for n in m.group(1).split(",") if n.strip()} if m else set()


def _exports(shim: Path) -> set[str]:
    body = shim.read_text(encoding="utf-8")
    block = body[body.index("export const {") : body.index("} = m")]
    return {tok for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", block) if tok not in {"export", "const"}}


def _function_body(name: str) -> str:
    start = TEXT.index(f"function {name}(")
    return TEXT[start : TEXT.index("\n}\n", start)]


def _top_level_functions() -> list[tuple[str, int]]:
    lines = TEXT.split("\n")
    out = []
    for i, line in enumerate(lines):
        m = re.match(r"^(?:export default )?function ([A-Za-z0-9_]+)\(", line)
        if m:
            end = next(j for j in range(i + 1, len(lines)) if lines[j] == "}")
            out.append((m.group(1), end - i + 1))
    return out


# ---------------------------------------------------------------------------
# Module shape
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_ui_module_parses():
    subprocess.run(["node", "--check", str(UI)], check=True)


def test_only_host_import_map_modules():
    """The host resolves exactly four bare specifiers; anything else (a relative file, a URL, a
    dynamic import) would fail to load inside KiroCrew."""
    specifiers = re.findall(r"^import .* from '([^']+)'", TEXT, flags=re.M)
    assert specifiers and set(specifiers) <= set(HOST_MODULES), specifiers
    assert "import(" not in TEXT
    assert len(re.findall(r"^import ", TEXT, flags=re.M)) == len(specifiers)


def test_components_take_a_single_props_argument():
    """React calls components as Component(props, legacyContext): a rest parameter renders {} → React error #31."""
    used_as_components = set(re.findall(r"h\(([A-Z][A-Za-z0-9_]*),", TEXT)) | set(re.findall(r"h\(([A-Z][A-Za-z0-9_]*)\)", TEXT))
    checked = 0
    for name in used_as_components:
        m = re.search(r"function " + name + r"\(([^)]*)\)", TEXT)
        if not m:
            continue  # host-provided component or icon
        params = m.group(1)
        top_level = re.sub(r"\{[^}]*\}", "props", params)  # one destructured object == one parameter
        assert "..." not in top_level and top_level.count(",") == 0, f"{name}({params}) must take exactly one props argument"
        checked += 1
    assert checked >= 40


def test_no_unverified_host_components():
    for name in ("EmptyState", "SegmentedControl", "MarkdownRenderer"):
        assert name not in TEXT, f"{name}'s prop contract is unverified; use the self-contained helper instead"


@pytest.mark.skipif(not VENDOR.is_dir(), reason="KiroCrew host bundle not present")
def test_imports_are_provided_by_the_host():
    for module, shim in (("@kirocrew/app-sdk", "kirocrew-app-sdk.mjs"), ("@kirocrew/app-sdk/ui", "kirocrew-ui.mjs"), ("lucide-react", "lucide-react.mjs")):
        missing = _imports(module) - _exports(VENDOR / shim)
        assert not missing, f"{module}: {sorted(missing)} not exported by the host"


def test_state_and_api_client_are_separated_from_views():
    """Transport → makeApi → useWorkspace → views: views never call the backend themselves."""
    assert "const PROCESS_BASE = '/apps/workshop-customizer/api/apps/workshop-customizer'" in TEXT
    assert "const AGENT_BASE = '/api/apps/workshop-customizer'" in TEXT
    assert TEXT.count("fetch(") == 1 and "fetch(PROCESS_BASE + path" in TEXT
    assert "fn(AGENT_BASE + path" in TEXT
    api = _function_body("makeApi")
    for pattern in (r"\bcall\('", r"\bagentCall\('"):
        assert len(re.findall(pattern, TEXT)) == len(re.findall(pattern, api)), f"{pattern} used outside makeApi"
    assert "function useWorkspace(" in TEXT and "createContext(" in TEXT
    views = [name for name, _n in _top_level_functions() if name[0].isupper() and name not in ("WorkshopCustomizerApp",)]
    for name in views:
        assert "api." not in _function_body(name) or name in ("ProjectsPage", "NewProjectCard"), f"view {name} calls the API client directly"


def test_one_component_per_section_no_giant_render_function():
    components = [(name, n) for name, n in _top_level_functions() if name[0].isupper()]
    assert len(components) >= 60
    too_long = [(name, n) for name, n in components if n > 60]
    assert not too_long, f"split these components: {too_long}"


# ---------------------------------------------------------------------------
# Every backend call is a real route
# ---------------------------------------------------------------------------

SAMPLES = {"pid": "demo-proj", "id": "gen-0123456789abcdef", "mid": "mat-0123456789ab", "action": "status", "stepId": "baseline",
           "audience": "student", "filePath(rel)": "agent/baseline-prompt.md"}


def _concrete(template: str) -> list[str]:
    """A UI path template with ${...} filled with sample values (both arms of a ?: conditional)."""
    variants = [template]
    out = []
    while variants:
        current = variants.pop()
        m = re.search(r"\$\{([^}]*)\}", current)
        if not m:
            out.append(current)
            continue
        expr = m.group(1)
        cond = re.fullmatch(r"\s*\w+\s*\?\s*'([^']*)'\s*:\s*'([^']*)'\s*", expr)
        fills = [cond.group(1), cond.group(2)] if cond else [SAMPLES.get(expr.strip(), "x1")]
        variants += [current[: m.start()] + fill + current[m.end():] for fill in fills]
    return out


def _ui_calls(kind: str) -> list[tuple[str, str]]:
    api = _function_body("makeApi")
    calls = re.findall(r"\b" + kind + r"\('(GET|POST|PUT|DELETE)', (['`])(.+?)\2", api)
    return [(method, path) for method, _q, template in calls for path in _concrete(template)]


def _server():
    spec = importlib.util.spec_from_file_location("wc_app_server_ui_probe", SERVER_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _probe(server, method: str, path: str) -> None:
    service = mock.MagicMock()
    service.jobs.active.return_value = None
    service.SYNC_JOB_ACTIONS = ("status", "commit", "rollback")
    service.export_zip.return_value = (b"", "x.zip")
    service.guide_download.return_value = (b"", "x.md")
    route_path, _, query = path.partition("?")
    params = dict(pair.split("=", 1) for pair in query.split("&") if pair)
    server.route(service, method, route_path, params, {})  # raises HttpError(404, "no route …") when unknown


def test_every_process_call_is_a_server_route():
    server = _server()
    calls = _ui_calls("call")
    links = [("GET", p) for template in re.findall(r"\$\{PROCESS_BASE\}(/[^`]+)`", TEXT) for p in _concrete(template)]
    assert len(calls) >= 45 and len(links) >= 3
    with pytest.raises(server.HttpError):  # the probe does detect a route the backend does not have
        _probe(server, "GET", "/projects/demo-proj/no-such-route")
    for method, path in calls + links:
        try:
            _probe(server, method, path)
        except server.HttpError as exc:
            pytest.fail(f"{method} {path} is not a backend route: {exc}")


def test_every_agent_call_is_a_kiro_generation_route():
    table = re.findall(r'AppRoute\("(GET|POST)", "([^"]+)"', ROUTES_PY.read_text(encoding="utf-8"))
    patterns = [(m, re.compile("^" + re.sub(r"\{[a-z_]+\}", "[^/]+", p) + "$")) for m, p in table]
    calls = _ui_calls("agentCall")
    assert {path for _m, path in calls} >= {"/generations", "/generations/latest/demo-proj", "/generations/gen-0123456789abcdef/apply",
                                            "/generations/gen-0123456789abcdef/revert"}
    for method, path in calls:
        assert any(m == method and rx.match(path) for m, rx in patterns), f"{method} {path} is not a Kiro generation route"


def test_the_flow_uses_the_routes_of_every_capability():
    api = _function_body("makeApi")
    for needle in (
        "/pack-kind", "/materials`", "/materials/approval", "/materials/${mid}/text", "/items/confirm-batch", "/items/${id}/confirm",
        "/validate`", "/validation`", "/build`", "/release`", "/golden", "/guide?audience=", "/guide/preview?audience=", "/target`",
        "/sync/preflight", "/sync/apply", "/sync/${action}/job", "/sync/history", "/oneclick`", "/jobs/${id}", "/run`", "/run/next",
        "/run/steps/${stepId}/start", "/run/poll", "/run/reset", "/run/report", "/run/rehearsal", "/calibrate", "/scenario`",
        "/files/${filePath(rel)}", "/files/prune", "/direct`", "/direct/chat`", "/direct/chat/${id}", "/direct/cleanup`",
    ):
        assert needle in api, needle
    assert "export?bundle=instructor" in TEXT and "&download=1" in TEXT
    assert "call('POST', `/projects/${pid}/run/rehearsal`" in api and "call('GET', `/projects/${pid}/run/rehearsal`" in api


# ---------------------------------------------------------------------------
# The four-step flow and its contracts
# ---------------------------------------------------------------------------


def test_four_steps_without_the_parked_go_live_page():
    for label in ("{ id: '1', n: '01', label: '场景与课程' }", "{ id: '2', n: '02', label: '内容与评估' }",
                  "{ id: '3', n: '03', label: '交付' }", "{ id: '4', n: '04', label: '彩排与上课' }"):
        assert label in TEXT
    assert "上线差距" not in TEXT and "n: '05'" not in TEXT
    assert "离可以上课还差" in TEXT and "function readiness(" in TEXT
    for page in ("ScenarioStep", "ContentStep", "DeliveryStep", "RehearsalStep", "AdvancedPage", "ProjectsPage"):
        assert f"function {page}(" in TEXT


def test_no_fixed_demo_data_from_the_prototype():
    for token in ("Northstar", "FZ-0", "BATCH-9999", "QH-5591", "111122223333", "46 项", "22 项", "135 / 180"):
        assert token not in TEXT, token


def test_generation_loop_contract():
    assert "mode: 'draft'" in TEXT and "mode: 'regenerate'" in TEXT and "mode: 'repair'" in TEXT
    assert "includeWarnings" in TEXT and "body.scope = scope" in TEXT and "body.instructions" in TEXT
    assert "{ acknowledged: true, acknowledgeConfirmationResets: !!resets }" in TEXT
    assert "revertGeneration: (id) => agentCall('POST', `/generations/${id}/revert`, { acknowledged: true })" in TEXT
    assert "await api.validate(pid, true) // the draft is applied" in TEXT
    for status in ("queued", "running", "ready", "'needs-input'", "failed", "applied", "superseded", "reverted"):
        assert status in TEXT
    for field in ("g.diff", "listOf(g.changes)", "listOf(g.warnings)", "g.deleteOrphans", "g.ignoredChanges", "g.provenance"):
        assert field in TEXT, field
    assert "'Kiro 按校验结果修复'" in TEXT
    assert "startRemediation" in TEXT and "listOf(asset.scopes)" in TEXT


def test_validation_review_and_rehearsal_views():
    for needle in ("v.repair", "repair.saOnly", "repair.engine", "familyOf(", "teaching:", "l1:", "guide:"):
        assert needle in TEXT, needle
    for needle in ("anchors.required", "anchorCandidates", "confirmBatch", "confirmItem", "ORIGIN_LABEL", "CLASS_LABEL"):
        assert needle in TEXT, needle
    for needle in ("r.readyForClass", "r.readiness", "r.remediation", "r.phenomena", "caseTable", "teachingContrast", "reasonCode"):
        assert needle in TEXT, needle


def test_safety_copy_is_kept():
    assert "./99-cleanup.sh" in TEXT and "本应用不会替你执行删除命令" in TEXT
    assert "不是 Git 提交" in TEXT
    assert "startOneclick: (pid) => call('POST', `/projects/${pid}/oneclick`, { acknowledged: true })" in TEXT
    assert "confirmToken: preflight.confirmToken" in TEXT
    assert "保留题只给导师" in TEXT and "不要发给学员" in TEXT
    assert "不存任何凭证" in TEXT


# ---------------------------------------------------------------------------
# Review fixes: pure helpers evaluated with node, and the view contracts around them
# ---------------------------------------------------------------------------

NODE = shutil.which("node")


def _source_of(name: str) -> str:
    """A top-level `function name(…) {…}` or single-line `const name = …` of the UI module."""
    if f"\nfunction {name}(" in TEXT:
        return _function_body(name) + "\n}\n"
    m = re.search(r"^const " + re.escape(name) + r" = .*$", TEXT, flags=re.M)
    assert m, name
    return m.group(0) + "\n"


def _js(names: tuple[str, ...], expr: str, *, prelude: str = ""):
    import json

    script = prelude + "".join(_source_of(n) for n in names) + f"\nprocess.stdout.write(JSON.stringify({expr}))\n"
    out = subprocess.run([NODE, "--input-type=module", "-e", script], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_material_accept_list_dots_each_extension_once():
    """The backend lists `sorted(materials.SUPPORTED)`, already dotted; the picker must not add a second dot."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "engine"))
    from workshop_customizer import materials

    types = sorted(materials.SUPPORTED)
    assert types and all(t.startswith(".") and not t.startswith("..") for t in types)
    accept = _js(("listOf", "acceptList"), f"acceptList({types!r})").split(",")
    assert accept == types
    assert _js(("listOf", "acceptList"), "acceptList(['md', '.pdf', ' csv ', '', null])") == ".md,.pdf,.csv"
    assert _js(("extensionOf",), "[extensionOf('Policy.DOCX'), extensionOf('notes'), extensionOf('a.b/c')]") == [".docx", "", ""]
    picker = _function_body("MaterialPicker")
    assert "accept = acceptList(limits && limits.types)" in picker and "onDrop" in picker and "'选择文件'" in picker
    assert "className: 'wc-file', type: 'file'" in picker  # the native control is hidden behind the styled button
    assert "`.${t}`" not in TEXT.replace("(t.startsWith('.') ? t : `.${t}`)", "")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_generation_blocked_without_a_recorded_materials_approval():
    """routes._check_material_policy, mirrored so 生成草稿 / 修复 are disabled before the 409."""
    names = ("listOf", "materialsBlock")
    src = "{ generationUse: 'source', author: 'customer', extraction: { status: 'ok' } }"
    sa_bg = "{ generationUse: 'background', author: 'sa', extraction: { status: 'partial' } }"
    cases = {
        "none": "materialsBlock([], null, false)",
        "excluded": "materialsBlock([{ generationUse: 'exclude', extraction: { status: 'ok' } }], null, false)",
        "failedExtraction": "materialsBlock([{ generationUse: 'source', extraction: { status: 'failed' } }], null, false)",
        "noPolicy": f"materialsBlock([{src}], null, false)",
        "confidential": f"materialsBlock([{src}], {{ dataClassification: 'confidential', approvalRef: 'legal mail' }}, false)",
        "internal": f"materialsBlock([{src}], {{ dataClassification: 'internal', approvalRef: 'legal mail' }}, false)",
        "customerNoRef": f"materialsBlock([{src}], {{ dataClassification: 'synthetic' }}, false)",
        "saNoRef": f"materialsBlock([{sa_bg}], {{ dataClassification: 'synthetic' }}, false)",
        "repairSkipsBackground": f"materialsBlock([{sa_bg}], null, true)",
        "draftSendsBackground": f"materialsBlock([{sa_bg}], null, false)",
    }
    got = _js(names, "{" + ", ".join(f"{k}: {v}" for k, v in cases.items()) + "}")
    for key in ("none", "excluded", "failedExtraction", "internal", "saNoRef", "repairSkipsBackground"):
        assert got[key] is None, key
    for key in ("noPolicy", "confidential", "customerNoRef", "draftSendsBackground"):
        assert isinstance(got[key], str) and got[key], key
    assert "wsMaterialsBlock(ws, false)" in _function_body("DraftCard") and "disabled: live || !canDraft || !!blocked" in _function_body("DraftCard")
    assert "wsMaterialsBlock(ws, true)" in _function_body("RepairLauncher") and "wsMaterialsBlock(ws, true)" in _function_body("RemediationList")
    assert "add('approval', '1', 'bad', '先记录资料批准')" in _function_body("readiness")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_prompt_tab_marks_come_from_the_validation_not_a_text_match():
    """teaching.contains_term (NFKC, casefold, markdown marks, word boundaries) is the only matcher."""
    body = _function_body("PromptTab")
    assert "toLowerCase" not in body and ".includes(String(needle" not in body
    assert "defectChecks(ws.validationView)" in body
    assert "'teaching.defect_marker_missing'" in body and "'teaching.defect_fix_missing'" in body
    view = ("{ fresh: true, validation: { schema: [], policy: ["
            "{ code: 'teaching.defect_fix_missing', path: 'labs.teaching.baselineDefects.no-noise-filter' },"
            "{ code: 'teaching.defect_marker_kept', path: 'labs.teaching.baselineDefects.not-concise' },"
            "{ code: 'teaching.phenomenon_case_missing', path: 'labs.teaching.phenomena.x' }] } }")
    got = _js(("listOf", "defectChecks"), f"[defectChecks({view}), defectChecks({{ fresh: false, validation: {{ policy: [] }} }}),"
                                          "defectChecks({ fresh: true, validation: { schema: ['bad'], policy: [] } }), defectChecks(null)]")
    assert got[0] == {"no-noise-filter": ["teaching.defect_fix_missing"], "not-concise": ["teaching.defect_marker_kept"]}
    assert got[1:] == [None, None, None]  # stale, not loaded, or no policy run: no ✓ / ✕ at all


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_guide_viewer_drops_machine_markers_and_mapping_ignores_placeholders():
    md = "<!-- workshop-customizer:student-guide pack=x lang=en -->\\n<!-- draft-preview -->\\n\\n# Title\\n\\nText <!-- inline stays -->\\n"
    assert _js(("stripGuideMarkers",), f"stripGuideMarkers('{md}')") == "# Title\n\nText <!-- inline stays -->\n"
    assert "stripGuideMarkers(data.markdown)" in _function_body("GuidesCard")
    assert _js(("isPlaceholder",), "[isPlaceholder('TODO who asks the questions'), isPlaceholder(''), isPlaceholder('Plant operators')]") == [True, True, False]
    assert "isPlaceholder(sc.agent.audience)" in _function_body("MappingCard")


def test_generation_elapsed_is_measured_to_completed_at():
    body = _function_body("GenerationStatus")
    assert "Date.parse(g.completedAt)" in body and "g.updatedAt" not in body  # apply / revert bump updatedAt


def test_run_hints_and_run_all_acknowledgement():
    run = _function_body("GuidedRunCard")
    assert "const needAck = remaining > 1" in run and "(needAck && !ackAll)" in run and "canStart && needAck ?" in run
    assert "不会取消已经在 AWS 里运行的这一步" in run
    hint = _function_body("RunHintButton")
    assert "ws.go('3')" in hint and "scrollToRunCard" in hint and "ws.go('4')" not in _function_body("RemediationList")
    assert "id: RUN_CARD_ID" in run
    ready = _function_body("readiness")
    assert "Guided Run 绑定旧版本，需新建运行记录" in ready and "内容改过，需重新构建 Release" in ready


def test_case_table_says_missing_evidence_and_uses_chinese_verdicts():
    body = _function_body("CaseTableCard")
    assert "row.probe ?" in body and "'无证据'" in body and "L1_VERDICT" in body and "COMBINED" in body and "mtgLabel(" in body
    for code in ("pass", "fail", "undetermined", "defer", "unverified"):
        assert f"{code}: [" in TEXT


def test_lost_guidance_and_english_codes():
    one = _function_body("OneClickCard")
    assert "job.status === 'interrupted'" in one and "job.status === 'failed'" in one and "stageNote(s.note)" in one
    assert "listOf(g.truthLedger)" in _function_body("TruthLedger") and "h(TruthLedger, { g })" in _function_body("GenerationReview")
    assert "!i.confirmable" in _function_body("BatchReviewCard")
    assert "sync/cfn/customizer-addons.json" in _function_body("TargetForm")
    assert "s.startedAt" in _function_body("StepRow") and "useNow(running)" in _function_body("StepRow")
    assert "（合成数值）" not in TEXT
    for key in ("documents", "facts", "goldenCases", "holdout", "pending", "tools"):
        assert re.search(r"\b" + key + r": '", TEXT), key  # DETAIL_KEY maps the validate summary keys
    assert "CAL_VERDICT" in _function_body("CalibrationCard") and "SYNC_EVENT[e.event]" in _function_body("ManualSyncCard")
    assert "`应用结果：${syncStatus(r.status)[1]}`" in TEXT


def test_entry_page_shows_read_errors_and_class_mode_names_the_current_build():
    page = _function_body("ProjectsPage")
    assert "setProjects([]))" not in page and "errors.projects" in page and "templatesError: errors.templates" in page
    assert "项目列表读不到" in _function_body("ProjectListCard") and "模板列表读不到" in _function_body("NewProjectCard")
    klass = _function_body("ClassModeCard")
    assert "最近一次彩排通过" not in klass and "当前构建的 Release" in klass and "当前 Release 还没通过彩排" in klass


def test_prototype_structure_guides_tab_and_three_rehearsal_modes():
    assert "{ value: 'guides', label: '学员与导师材料' }" in _function_body("ContentStep") and "guides: GuidesCard" in TEXT
    assert "const REHEARSAL_MODES = [['rehearse', '彩排'], ['class', '上课'], ['after', '课后']]" in TEXT


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_repair_counts_the_rehearsal_findings_the_route_adds():
    """prepare_generation adds the current release's blocking rehearsal remediation (R1..Rn) to a repair or
    regenerate: GET validation lists them (rehearsalFindings), and the repair panel counts and lists them with
    an opt-out, as the scoped regenerate does; one remediation button sends one hint (review finding: the panel
    said 'Kiro 会修 2 条' while 9 findings were sent)."""
    view = "{ rehearsalFindings: [{ id: 'R1', scopes: ['prompts'] }, { id: 'R2', scopes: ['tools'] }, { id: 'R3', scopes: ['labs', 'golden'] }] }"
    got = _js(("listOf", "rehearsalFindingsFor"), f"[rehearsalFindingsFor({view}, []).map((f) => f.id), "
              f"rehearsalFindingsFor({view}, ['golden', 'prompts']).map((f) => f.id), rehearsalFindingsFor(null, []), rehearsalFindingsFor({{}}, ['labs'])]")
    assert got == [["R1", "R2", "R3"], ["R1", "R3"], [], []]
    scopes_js = "const SCOPES = " + repr(["agent", "facts", "knowledge", "tools", "skills", "prompts", "golden", "labs", "guides"]) + "\n"
    rehearsal = "[{ scopes: ['tools'] }, { scopes: ['prompts'] }]"
    got = _js(("listOf", "repairScopes"), "[" + ", ".join((
        f"repairScopes(['labs', 'golden'], null, [], {rehearsal})",  # an explicit scope wins
        f"repairScopes([], {{ repair: {{ suggestedScopes: ['golden', 'labs'] }} }}, [{{ scopes: ['agent'] }}], {rehearsal})",
        f"repairScopes([], {{ repair: {{}} }}, [{{ scopes: ['labs'] }}], {rehearsal})",
        "repairScopes([], null, [], [])")) + "]", prelude=scopes_js)
    assert got == [["golden", "labs"], ["tools", "prompts", "golden", "labs"], ["tools", "prompts", "labs"], []]
    import sys

    sys.path.insert(0, str(REPO_ROOT / "app" / "backend"))
    import routes

    assert list(routes.SCOPES) == ["agent", "facts", "knowledge", "tools", "skills", "prompts", "golden", "labs", "guides"]
    launcher = _function_body("RepairLauncher")
    assert "rehearsalFindingsFor(ws.validationView, scope)" in launcher and "const sent = [...selected, ...rehearsal]" in launcher
    assert "`Kiro 会修 ${sent.length} 条" in launcher and "来自彩排整改" in launcher and "连彩排整改一起修" in launcher
    assert "ws.startRepair({ includeWarnings, instructions, scope, includeRehearsal })" in launcher
    assert "if (includeRehearsal === false) body.includeRehearsal = false" in TEXT
    assert "scope: listOf(asset.scopes), includeRehearsal: false }, `remedy:${hint.id}`" in TEXT  # one hint, one item
    draft = _function_body("DraftCard")
    assert "rehearsalFindingsFor(ws.validationView, scope)" in draft and "ws.startRegenerate(scope, includeRehearsal)" in draft
    assert "...(includeRehearsal === false ? { includeRehearsal: false } : {})" in TEXT
    server = SERVER_PY.read_text(encoding="utf-8")
    assert '"rehearsalFindings": rehearsal_findings' in server
    # Recording a rehearsal writes run/rehearsal.json and a reset archives it: both refetch GET validation, so
    # the count never shows the previous rehearsal's findings (or none) until something else refreshes.
    record = TEXT[TEXT.index("recordRehearsal: () => act("):]
    assert "['project', 'validation'], (r) => `已记录彩排结论" in record[:400]
    reset = TEXT[TEXT.index("resetRun: (fromStep) => act("):]
    assert "['rehearsal', 'report', 'project', 'validation'], fromStep ?" in reset[:400]


# A tiny render harness: the component runs in node with stubbed hooks, and h() records the element tree.
RENDER_PRELUDE = """
let WS = null
let OVERRIDES = []
const useWs = () => WS
const useState = (v) => { const o = OVERRIDES.shift(); return [o === undefined ? v : o, () => {}] }
const h = (type, props, ...children) => ({ type: typeof type === 'string' ? type : type.name, props: props || {}, children })
function Seg() {} function Field() {} function TextInput() {} function Select() {} function Chip() {} function Button() {}
function Note() {} function ErrorLine() {}
const walk = (n, out = []) => { if (Array.isArray(n)) { n.forEach((c) => walk(c, out)) } else if (n && typeof n === 'object') { out.push(n); walk(n.children, out) } return out }
const textOf = (n) => walk(n).flatMap((x) => x.children).filter((c) => typeof c === 'string').join('')
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_guide_narrative_is_reviewed_without_an_origin():
    """server.confirm_item refuses any origin on the guide narrative (labs.guide has no origin field), so its
    single-item form offers none and never sends one, in every pack kind (review finding: in a workshop pack the
    form defaulted to sa_authored and always got HTTP 400). A workshop fact still needs its origin."""
    expr = """(() => {
  const calls = []
  const mk = (packKind) => ({ items: { packKind }, materials: { materials: [] }, busy: '', errors: {},
    confirmItem: (id, body) => { calls.push({ packKind, id, body }); return Promise.resolve(null) } })
  const render = (packKind, item, overrides = []) => {
    WS = mk(packKind); OVERRIDES = [...overrides]
    const nodes = walk(ConfirmItemForm({ item, onDone: () => {} }))
    const primary = nodes.find((n) => n.type === 'Button' && n.props.kind === 'primary')
    if (!primary.props.disabled) primary.props.onClick()
    return { disabled: !!primary.props.disabled, segs: nodes.filter((n) => n.type === 'Seg').map((n) => n.props.options.map((o) => o[0])),
             notes: nodes.filter((n) => n.type === 'Note').map(textOf) }
  }
  const narrative = { id: 'guide-narrative', kind: 'narrative', provenance: 'ai_draft' }
  const out = {}
  for (const kind of ['workshop', 'reference', 'customer']) out[kind] = render(kind, narrative)
  out.workshopFact = render('workshop', { id: 'f1', kind: 'fact', provenance: 'ai_draft' }, ['sa_synthetic'])
  out.workshopFactNoOrigin = render('workshop', { id: 'f2', kind: 'fact', provenance: 'ai_draft' }, ['sa_synthetic', '', '', '', ''])
  out.calls = calls
  return out
})()"""
    got = _js(("listOf", "ORIGIN_LABEL", "ConfirmItemForm"), expr, prelude=RENDER_PRELUDE)
    for kind in ("workshop", "reference", "customer"):
        assert got[kind]["disabled"] is False and got[kind]["segs"] == [["sa_synthetic"]], (kind, got[kind])
        assert "手册叙述是教学文字，不标来源。" in got[kind]["notes"]
    narrative_calls = [c for c in got["calls"] if c["id"] == "guide-narrative"]
    assert [c["body"] for c in narrative_calls] == [{"provenance": "sa_synthetic"}] * 3  # never an origin
    assert got["workshopFact"]["segs"][1] == ["customer_material", "sa_authored", "teaching_design"]  # no '不标' in a workshop pack
    assert [c["body"] for c in got["calls"] if c["id"] == "f1"] == [{"provenance": "sa_synthetic", "origin": {"kind": "sa_authored"}}]
    assert got["workshopFactNoOrigin"]["disabled"] is True  # a workshop synthetic setting still needs its origin


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_case_table_names_each_rows_phenomena_even_from_the_report():
    """guided_report.case_table rows have no phenomenonIds (rehearsal.py adds them), and the card prefers the
    report: the phenomena come from the rehearsal row of the same case, else labs.teaching (review finding)."""
    sc = "{ labs: { teaching: { phenomena: [{ id: 'pf-good', caseIds: ['p2', 'p3'] }, { id: 'gap', caseIds: ['p9'] }] } } }"
    rehearsal = "{ cases: [{ caseId: 'p2', phenomenonIds: ['pf-good-at-build'] }] }"
    got = _js(("listOf", "teachingOf", "phenomenaOf", "phenomenaOfCase", "casePhenomena"), "[" + ", ".join((
        f"casePhenomena({{ caseId: 'p2' }}, {rehearsal}, {sc})",  # the build snapshot's (rehearsal) wins over the live scenario
        f"casePhenomena({{ caseId: 'p3' }}, {rehearsal}, {sc})",
        f"casePhenomena({{ caseId: 'p3', phenomenonIds: ['own'] }}, {rehearsal}, {sc})",
        f"casePhenomena({{ caseId: 'zz' }}, null, {sc})",
        "casePhenomena({ caseId: 'p3' }, null, null)")) + "]")
    assert got == [["pf-good-at-build"], ["pf-good"], ["own"], [], []]
    body = _function_body("CaseTableCard")
    assert "casePhenomena(row, ws.rehearsal && ws.rehearsal.caseTable, ws.sc)" in body and "listOf(row.phenomenonIds).length" not in body


def test_a_new_release_is_re_synced_into_the_same_environment_after_scenario_only_cleanup():
    """SKILL.md steps 5-6 and both acceptance runs re-sync a changed release into the SAME SA-prepared
    environment after ./99-cleanup.sh --scenario-only; the UI used to say a new release needs a new
    environment and showed only the plain teardown (review finding)."""
    assert "换了 Release 要用新的 Workshop 环境" not in TEXT
    assert "const SCENARIO_ONLY_CLEANUP = 'cd /home/ssm-user/workshop/current && ./99-cleanup.sh --scenario-only'" in TEXT
    note = _function_body("NewReleaseNote")
    assert "h(Pre, null, SCENARIO_ONLY_CLEANUP)" in note and "保留 workshop-infra 和 Customizer 附加栈" in note
    assert "本应用不会替你运行" in note and "体检 → 一键覆盖 → 确认保留" in note and "从头跑完 15 步" in note
    assert "h(NewReleaseNote)" in _function_body("GuidedRunCard")
    remediation = _function_body("RemediationList")
    assert "newRelease ? h('div', { className: 'wc-mt1' }, h(NewReleaseNote)) : null" in remediation
    cleanup = _function_body("CleanupCard")
    assert "还会删掉 workshop-infra 和 Customizer 附加栈" in cleanup and "--scenario-only" in cleanup
    assert "h(Pre, null, 'cd /home/ssm-user/workshop/current && ./99-cleanup.sh')" in cleanup  # the teardown stays behind its ack


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_rehearsal_texts_show_the_pack_language_and_offer_the_english_twin():
    """rehearsal.py writes every text in the pack language with an English twin (<field>En). The UI shows the
    text, offers the twin on hover when it differs, names the machine readings in Chinese, and shows a warning's
    message (not the JSON of the warning object)."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "engine"))
    from workshop_customizer import rehearsal

    assert _js(("englishTwin",), "[englishTwin('中文', 'English'), englishTwin('same', 'same'), englishTwin('x', undefined) ?? null]") == \
        ["English", None, None]
    got = _js(("asText", "listOf", "warningText"), "[warningText({ code: 'PF_OPTIMIZED_NOT_PASS', message: '优化后的回答有提升' }), warningText('plain'), warningText(null)]")
    assert got == ["优化后的回答有提升", "plain", ""]
    # The refs say which phenomenon and case a warning is about (the message of PF_OPTIMIZED_NOT_PASS,
    # ABSENT_GAP_NOT_ADMITTED and MTG_REFUSAL_CONFLICT names neither); a ref the message names is not repeated.
    got = _js(("asText", "listOf", "warningText"), "[" + ", ".join((
        "warningText({ code: 'PF_OPTIMIZED_NOT_PASS', message: '优化后的回答有提升，但 THELMA 仍判为 Fail', "
        "refs: ['prompt-fix-p1-threshold', 'p1-full-condition-practice'] })",
        "warningText({ code: 'PHENOMENON_MIXED', message: 'gap 在 2 次完整运行中复现了 1 次；课前再跑一次', refs: ['gap'] })",
        "warningText({ code: 'VERDICT_MIXED', message: '几次完整运行对上课结论不一致', refs: ['current', 'history/r1', 7, ''] })",
    )) + "]")
    assert got == ["prompt-fix-p1-threshold · p1-full-condition-practice：优化后的回答有提升，但 THELMA 仍判为 Fail",
                   "gap 在 2 次完整运行中复现了 1 次；课前再跑一次", "current · history/r1：几次完整运行对上课结论不一致"]

    def keys(const: str) -> set[str]:  # a flat `const NAME = { key: value, 'two words': value }` (no nested braces)
        start = TEXT.index(f"const {const} = {{")
        body = TEXT[TEXT.index("{", start): TEXT.index("}", start) + 1]
        return set(re.findall(r"[{,]\s*'?([A-Za-z_ ]+?)'?:", body))

    assert keys("ADVISORY_READING") == set(rehearsal.NOISE_READINGS) | set(rehearsal.MEMORY_READINGS)
    assert keys("SCENARIO_SOURCE") == set(rehearsal.SCENARIO_SOURCES)
    assert keys("BLOCKER") == set(rehearsal.BLOCKERS)
    card = _function_body("RehearsalCard")
    for needle in ("warningText(w)", "ADVISORY_READING[a.reading]", "SCENARIO_SOURCE[r.inputs.scenario]", "r.readiness.blockerTexts",
                   "englishTwin(r.reason, r.reasonEn)", "englishTwin(a.note, a.noteEn)"):
        assert needle in card, needle
    assert "asText(w)}" not in card
    hints = _function_body("RemediationList")
    assert "englishTwin(hint.action, hint.actionEn)" in hints and "englishTwin(hint.because, hint.becauseEn)" in hints
    assert "englishTwin(f.message, f.messageEn)" in _function_body("FindingRow")
    # One remediation sent to Kiro carries the English original too (the repair contract is English).
    start = TEXT.index("startRemediation: (hint) => {")
    remedy = TEXT[start: TEXT.index("\n    },\n", start)]
    assert "englishTwin(hint.action, hint.actionEn)" in remedy and "English: ${en}" in remedy and "hint.becauseEn" in remedy


def test_direct_mode_is_a_fast_rehearsal_never_a_class_verdict():
    """04 彩排: the direct round (about 8 minutes, the pack's own direct resources) comes first; its verdict says it
    is not the class verdict and a ready one sends the SA on to the Guided Run; cleanup needs its own tick."""
    step = _function_body("RehearsalStep")
    assert step.index("h(DirectRunCard)") < step.index("h(GuidedRunCard)") and "h(DirectChatCard)" in step
    verdict = _function_body("DirectVerdict")
    assert "'不是上课结论'" in verdict and "Guided Run 拿上课结论" in verdict and "h(RemediationList" in verdict
    assert "DIRECT_VERDICT" in verdict and "map: VERDICT" not in verdict  # never '可以上课'
    cleanup = _function_body("DirectCleanup")
    assert "checked: ack" in cleanup and "!ack" in cleanup and "不碰 Workshop 的资源" in cleanup
    assert "readyForClass" not in _function_body("DirectRunCard")
