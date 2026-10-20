// Workshop Customizer — KiroCrew app UI (hand-written ESM, no build step).
// The v2 four-step flow: 01 场景与课程 · 02 内容与评估 · 03 交付 · 04 彩排与上课, plus 高级 (raw files and
// manual operations). Every number and list on screen comes from the backend; the UI holds no demo data.
//
// Resolved through the host import map: react, @kirocrew/app-sdk, @kirocrew/app-sdk/ui, lucide-react.
// Layers, top to bottom: transport (fetch for the app process, the host api for the in-gateway Kiro
// routes) → makeApi (every backend route the page uses, and nothing else) → useWorkspace (resources,
// polling, actions, readiness) → views (one component per page section, reading the workspace from
// context). Styles are scoped under .wc-root and follow the host theme variables when present.

import React from 'react'
import { useAppApi, useNotify } from '@kirocrew/app-sdk'
// Only icons the host re-exports by name (see vendor/lucide-react.mjs).
import { ArrowLeft, RefreshCw, Wand2, Download, Rocket, Settings, Plus, Trash2 } from 'lucide-react'

const { useState, useEffect, useCallback, useMemo, useRef, useContext, createContext, createElement: h, Fragment } = React

// The deterministic engine stays in its isolated app process. Kiro generation is an in-gateway
// AppRoute so it can use the permissioned ctx.spawn seam (backed by the logged-in Kiro CLI).
const PROCESS_BASE = '/apps/workshop-customizer/api/apps/workshop-customizer'
const AGENT_BASE = '/api/apps/workshop-customizer'

// ---------------------------------------------------------------------------
// Labels (UI copy is Chinese; codes and ids stay exactly as the backend sends them)
// ---------------------------------------------------------------------------

const PACK_KINDS = ['reference', 'workshop', 'customer']
const PACK_KIND = {
  reference: { tone: 'info', label: '内部参考包', help: '虚构或内部内容：SA 可以把内容确认为合成数据；只用于培训和回归，不交付客户。' },
  workshop: { tone: 'acc', label: '客户 Workshop', help: '真实客户场景上课：实验数值可以是合成教学设定（要标来源），但正常、边界、禁止三类题各要至少一道客户确认的锚点题。' },
  customer: { tone: 'warn', label: '客户严格确认', help: '最严格：所有阻断项都要客户确认（确认人 + 凭证）；用于交付和上线。' },
}
const PROJECT_STATUS = {
  intake: ['', '新建'], 'truth-review': ['warn', '待校验通过'], validated: ['info', '已校验'], built: ['ok', '已构建'],
  'synced-pending-commit': ['warn', '待确认保留'], synced: ['ok', '已同步到 Workshop'], 'rolled-back': ['bad', '已回滚'],
  'rollback-incomplete': ['bad', '回滚不完整'],
}
const TEMPLATE_LABEL = {
  blank: '空白 · 由 Kiro 起草（推荐）', 'hr-default': 'HR 默认 Workshop（上游原版）', 'it-helpdesk': 'IT Helpdesk 参考包（虚构）',
  maintenance: '设备维修参考包（虚构）', 'kiro-generated': 'Kiro 生成',
}
const SCOPE_LABEL = {
  agent: '智能体', facts: '业务规则', knowledge: '知识文档', tools: '工具', skills: 'Skill', prompts: 'Prompt',
  golden: '题目与判据', labs: '教学设计', guides: '学员与导师手册',
}
const SCOPES = Object.keys(SCOPE_LABEL)
const CLASS_LABEL = {
  'customer-fact': ['ok', '客户事实'], 'derived-setting': ['info', '合成教学设定（源自客户资料）'], 'teaching-device': ['acc', '教学设计'],
  'synthetic-setting': ['', '合成教学设定'], draft: ['warn', '草稿（未审核）'],
}
const PROVENANCE_LABEL = { customer_confirmed: ['ok', '客户已确认'], sa_synthetic: ['', 'SA 合成'], ai_draft: ['warn', 'Kiro 草稿'], pending: ['warn', '待定'] }
const ORIGIN_LABEL = { customer_material: '客户资料', sa_authored: 'SA 编写', teaching_design: '教学设计' }
const ITEM_KIND_LABEL = { fact: '业务规则', tool: '工具', document: '文档', golden: '题目', narrative: '手册叙述', guide: '手册叙述' }
const CATEGORY_LABEL = { normal: '正常', boundary: '边界', prohibited: '禁止' }
const CATEGORIES = ['normal', 'boundary', 'prohibited']
const SET_LABEL = { practice: '练习', holdout: '留出' }
const GEN_STATUS = {
  queued: ['info', '排队中'], running: ['info', '生成中'], ready: ['warn', '待应用'], 'needs-input': ['warn', '需要补充信息'],
  failed: ['bad', '失败'], applied: ['ok', '已应用'], superseded: ['', '已被新草稿取代'], reverted: ['', '已撤销'],
}
const GEN_MODE = { draft: '起草', repair: '修复', regenerate: '改写' }
// A staged draft (routes STAGE_DEPS): one Kiro reply per part of the pack, in three phases.
const STAGE_INFO = {
  structure: ['结构', '角色、业务规则、知识文档规划、工具'], evaluation: ['题目与教学设计', '题目、教学现象、弱基线'],
  skills: ['Skill', '每个 Skill 的 SKILL.md'], prompts: ['Prompt', '弱基线和优化候选两份 Prompt'], guides: ['学员与导师手册', '学员和导师手册的补充内容'],
}
const STAGE_PHASES = [
  ['1', '先定结构', ['structure']],
  ['2', '写题目和教学设计（Skill 同时写）', ['evaluation', 'skills']],
  ['3', '按前两段并行写文件', ['knowledge', 'prompts', 'guides']],
]
const STAGE_STATUS = { pending: ['', '等待'], running: ['info', '进行中'], done: ['ok', '完成'], failed: ['bad', '失败'], skipped: ['', '没用到'] }
const stageBase = (name) => String(name || '').split('-')[0]
function stageInfo(s) {
  if (stageBase(s.name) !== 'knowledge') return STAGE_INFO[s.name] || [s.name, '']
  const files = listOf(s.write).map((f) => String(f).split('/').pop())
  return [`知识文档 · 第 ${s.name.slice(10)} 批`, files.length ? `${files.length} 篇：${files.join('、')}` : '一批知识文档']
}
// Why a stage's reply was not kept, or what it still lacks: the backend's messages (English, for Kiro) in one line each.
const STAGE_REASONS = [
  [/cut off|output limit/i, '回复被截断（输出上限或连接中断）'], [/response stream|dispatch failure|KiroExit|exited \d/i, 'Kiro 连接中断'],
  [/timed out|Timeout/i, 'Kiro 超时'], [/declined/i, 'KiroCrew 没能启动这一步'], [/never started/i, '这一步没能启动'],
  [/no JSON|invalid JSON|could not be checked|has no scenario|has no files/i, '回复格式不对'],
  [/^P1/, 'Prompt 没逐字写进教学设计要求的句子'], [/^P2/, 'Prompt 没写检索工具名'], [/^K1/, '有文档太短'],
  [/^K3.*lacks/, '文档缺少练习题要用的答案词'], [/^K3.*absentTerms/, '文档写进了本该缺失的内容'],
  [/^S1/, '题目数量或留出题比例不够'], [/^S[23]/, '题目引用了不存在的规则、角色或工具'], [/^T\d/, '教学设计和题目对不上'],
  [/gated_tool_role_argument/, '工具带了“角色”参数（Agent 会先调工具再拒绝）'], [/first_term_in_tool_spec/, '记忆探针的值写进了工具说明'],
  [/first_term_in_kb/, '记忆探针的值知识库里也有（不靠记忆也会说出来）'],
  [/tool_arg_not_in_query/, '练习题没写出工具必需的参数'], [/role_not_in_query/, '拒绝题没写出提问者的角色'],
  [/too_many_practice/, '练习题太多（上课要逐题跑）'], [/defect_marker_absent/, '弱基线缺少让它答偏的那句话'],
  [/plan\.absentTerms/, '“本该缺失”的关键词不合格（每个至少 2 个字，单字会误伤别的词）'],
  [/^schema:/, '有字段不符合 scenario 格式（如词太短、缺必填项）'],
  [/retrieval_case_not_probe/, '练习题要调检索工具，但不是检索探针（THELMA 不评，只做 L1）'], [/defect_fix_missing/, '优化候选 Prompt 没逐字写进 candidateFix'],
  [/defect_marker_missing/, '弱基线 Prompt 没逐字写进 baselineMarker'], [/defect_marker_kept/, '优化候选 Prompt 还留着弱基线那句话'],
  [/defect_already_fixed/, '弱基线 Prompt 已经写了 candidateFix（基线不会答偏）'], [/bait_missing/, '干扰文档没写进诱饵词'],
  [/fixable_answer_not_in_kb/, '练习题的答案词不在任何知识文档里'], [/absent_term_in_kb/, '知识文档写进了本该缺失的词'],
  [/gap_question_in_kb/, '知识文档写到了缺口问题本身'], [/missing_kind/, '缺一类教学现象'], [/refusal_case/, '拒绝题的类别或判据不对'],
  [/tool_use_case/, '工具题没有要求调用 mock 工具'], [/escalation_case/, '升级题没设 shouldEscalate'], [/noise_doc_flag/, '干扰现象列的文档没标 noise'],
  [/material_unknown/, '引用的资料已不在项目里（删了或没上传）：到「02 → 审核」重新确认这一项，来源改成现有资料或别的类型'],
  [/references unknown|cites unknown/, '引用了不存在的规则、文档、工具、题目或角色'], [/^teaching\./, '教学设计检查没通过'], [/facts has|documents plans|noise:true|kind=retrieval|mock tool|ids repeat|skill /, '结构不完整（规则、文档规划或工具）'],
]
const stageReason = (text) => (STAGE_REASONS.find(([re]) => re.test(String(text || ''))) || [null, '另一项检查没通过（见原始信息）'])[1]
const stageReasons = (texts) => [...new Set(listOf(texts).map(stageReason))]
// One message per check: an attempt's own problem list (a message may itself hold "; "), else its error.
const attemptProblems = (x) => (listOf(x.problems).length ? x.problems : [x.error])
// What the page shows next to the project's own content: a Kiro draft waiting for review, and where the content came from.
const precheckErrors = (g) => (g && g.precheck && g.precheck.ran ? g.precheck.errorCount || 0 : 0)
// Ids the SA reads as names: a question by its label, a role by its name, a document by its title (the id stays beside it).
const caseName = (sc, id) => { const c = goldenOf(sc).find((x) => x.id === id); return c && c.label ? `${c.label}（${id}）` : id }
const roleName = (sc, id) => { const r = listOf(sc && sc.agent && sc.agent.roles).find((x) => x.id === id); return r && r.name ? r.name : id }
const actorName = (sc, actorId) => {
  const c = goldenOf(sc).find((x) => x.actorId === actorId && x.roleId)
  const role = c ? roleName(sc, c.roleId) : null
  return role && role !== c.roleId ? `${role}（${actorId}）` : actorId
}
const docName = (sc, id) => { const d = listOf(sc && sc.knowledge && sc.knowledge.documents).find((x) => x.id === id); return d && d.title ? `${d.title}（${id}）` : id }
// What applying the waiting (or running) draft does: a scoped rewrite replaces only its scopes.
const replaceText = (g) => (g && g.mode === 'regenerate' && listOf(g.scope).length ? `应用后只替换它改写的部分（${listOf(g.scope).map((x) => SCOPE_LABEL[x] || x).join('、')}）` : '应用后整体换掉')
// The 01 card offers 在当前内容上改写 only on a project with content, no waiting draft, no template content and nothing running.
const canRewrite = (ws) => goldenOf(ws.sc).length > 0 && !pendingDraft(ws) && !templateOrigin(ws) && !isLive(ws.generation)
const pendingDraft = (ws) => { const g = ws.generation; return g && g.status === 'ready' && !g.stale && g.mode !== 'repair' && g.scenarioPreview ? g : null }
const draftRunning = (ws) => { const g = ws.generation; return !!g && isLive(g) && g.mode !== 'repair' }
// Any Kiro result that waits to be applied (a draft or a repair, unless it is void) or still runs: recording a
// review or saving now changes the project, and that result could no longer be applied.
const holdingResult = (ws) => { const g = ws.generation; return !!g && (isLive(g) || (g.status === 'ready' && !g.stale)) }
const holdText = (ws, doing = '记录审核或保存修改', then = '再审核') => {
  const g = ws.generation || {}
  const what = g.mode === 'repair' ? 'Kiro 修复结果' : 'Kiro 草稿'
  const where = `${isLive(g) ? '等它完成后，' : ''}${g.mode === 'repair' ? '先在「02 → 教学与评估」下方应用它' : '先到 01 应用它'}`
  return `${what}${isLive(g) ? '正在生成' : '待应用'}：现在${doing}会改动项目，它就不能再应用。${where}，${then}。`
}
function templateOrigin(ws) {
  const p = ws.project || {}
  const tpl = p.createdFromTemplate || p.template
  return tpl && tpl !== 'blank' && !((p.editLog || {}).kiroApplies > 0) ? TEMPLATE_LABEL[tpl] || tpl : null
}
const PHENOMENON_KIND = {
  prompt_fixable: 'Prompt 可修复', retrieval_gap: '检索缺口', noise_grounding: '干扰问答', tool_use: '工具调用', refusal: '拒绝',
  escalation: '升级转人',
}
const MECHANISM = { buried: '答案埋在文档里', absent: '文档里没有答案' }
const JUDGE_OF_KIND = {
  prompt_fixable: 'THELMA GR 基线 → 优化', retrieval_gap: 'THELMA SP2 / SQC（两轮都检索失败）', noise_grounding: 'THELMA SP1 / SP2（参考）',
  tool_use: 'L1 工具检查 + Mind the Goal', refusal: 'L1 拒绝检查 + Mind the Goal', escalation: 'L1 升级检查 + Mind the Goal',
}
const CONTRAST = {
  reproduced: ['ok', '已复现'], not_reproduced: ['bad', '没复现'], insufficient_evidence: ['warn', '证据不足'], advisory: ['', '参考'],
  missing: ['bad', '缺这一类'], undeclared: ['bad', '没有教学设计'],
}
const VERDICT = { ready: ['ok', '可以上课'], not_ready: ['bad', '还不能上课'], insufficient_evidence: ['warn', '证据不足'] }
const BLOCKER = {
  VERDICT_NOT_READY: '有教学现象没复现', VERDICT_INSUFFICIENT_EVIDENCE: '证据不足以判定', REPORT_INCOMPLETE: '运行报告不完整',
  GUIDES_MISSING: '学员或导师手册没构建', SCENARIO_NOT_BUILD_SNAPSHOT: '没按构建快照判定', RELEASE_NOT_VERIFIED: 'Release 未核实',
}
const GROUP_LABEL = { prompt_fixable: 'Prompt 可修复', retrieval_gap: '检索缺口', l1: '工具 / 拒绝 / 升级' }
// Readings of the advisory items (engine rehearsal.py NOISE_READINGS / MEMORY_READINGS) and the scenario the
// rehearsal judged (SCENARIO_SOURCES): machine values, shown in Chinese.
const ADVISORY_READING = {
  observed: '观察到干扰', not_observed: '没观察到干扰', no_evidence: '没有证据', generic: '没认出用户（符合预期）',
  personalized: '已认出用户', unknown: '无法判断',
}
const SCENARIO_SOURCE = { 'build snapshot': '构建快照', 'scenario file': '单独的 scenario 文件' }
const STEP_LABEL = {
  setup: '环境准备', infra: '部署基础设施', 'knowledge-base': '创建知识库', gateway: '创建 Gateway 与工具', skills: '配置 Skills',
  agent: '部署 Agent', memory: '配置 Memory', conversation: '第一次对话', 'eval-env': '准备评估环境', evaluators: '创建评估器',
  baseline: '基线评估', optimize: '优化 Prompt 并复评', 'cost-latency': '成本与延迟', models: '模型对比', 'judge-stability': 'Judge 稳定性',
}
const RUN_STATUS = { not_started: ['', '未开始'], running: ['info', '运行中'], passed: ['ok', '通过'], failed: ['bad', '失败'], blocked: ['', '等前一步'] }
const JOB_STATUS = {
  queued: ['info', '排队'], pending: ['', '等待'], running: ['info', '进行中'], succeeded: ['ok', '完成'], failed: ['bad', '失败'],
  skipped: ['', '跳过'], 'not-run': ['', '未运行'], interrupted: ['warn', '被中断'],
}
const ONECLICK_STAGE = { validate: '校验', build: '构建', preflight: '预检', apply: '上传并应用', verify: '核对', confirm: '确认保留', guided: '绑定 Guided Run' }
// The one-click job's stage notes (app/backend/server.py _oneclick), shown in Chinese.
const STAGE_NOTE = {
  'reused the release already built from these exact sources': '复用了按这些源文件已构建的 Release',
  'this release was already the active workshop tree': '这个 Release 已经是远端的当前版本',
  'left pending-commit on request: the watchdog rolls the Workshop back unless you confirm': '按要求停在“待确认保留”：不确认的话，远端到时会自动回滚',
  'the Guided Run needs a confirmed release': 'Guided Run 要等 Release 确认保留',
}
// Keys of the stage details (validate summary, build, preflight plan, apply / verify / confirm / guided).
const DETAIL_KEY = {
  documents: '文档', facts: '业务规则', tools: '工具', goldenCases: '题目', holdout: '留出题', pending: '待审核', version: '版本', files: '文件',
  contentHash: '内容哈希', reused: '复用已有构建', accountId: '账号', region: '区域', instanceId: '实例', documentName: '同步文档',
  runDocumentName: '运行文档', checks: '检查项', status: '状态', commandId: '命令', ssmStatus: 'SSM 状态', active: '远端当前版本',
  currentLink: 'current 指向', hostStatus: '远端状态', releases: '远端版本', releaseVersion: 'Release', steps: '步骤', passed: '已通过',
  nextStep: '下一步', reset: '新建了运行记录', expected: '应为', failed: '没通过的检查', gate: '审核门禁', schema: '结构', policy: '策略错误',
}
const SYNC_ACTIONS = [['status', '查看状态'], ['commit', '确认保留'], ['rollback', '回滚']]
const SYNC_EVENT = { preflight: '体检', apply: '上传并应用', status: '查看状态', commit: '确认保留', rollback: '回滚' }
const JOB_KIND = { oneclick: '一键覆盖', 'sync-status': '查看状态', 'sync-commit': '确认保留', 'sync-rollback': '回滚', direct: '快速彩排', 'direct-cleanup': '清理直连资源' }
// Direct mode (app/backend/server.py start_direct): one round's stages; its verdict is never a class verdict.
const DIRECT_STAGE = { provision: '部署资源', ask: '提问', settle: '等 trace 完整', judge: '裁判打分', rehearse: '彩排判定' }
const DIRECT_VERDICT = { ready: ['ok', '快速彩排通过'], not_ready: ['bad', '快速彩排没通过'], insufficient_evidence: ['warn', '证据不足'] }
const DIRECT_TIMING = { provision: '部署', ask: '提问', settle: '等 trace', judge: '打分' }
const CHAT_PROMPT = [['candidate', '候选 Prompt'], ['baseline', '基线 Prompt']]
const DIRECT_REPEAT = [[1, '1 轮'], [3, '3 轮（看稳定性）']]
// direct.panel readings: an AgentCore evaluator sees / misses / contradicts a designed contrast; a control agrees or not.
const PANEL_MARK = { sees: ['ok', '看出'], misses: ['', '没看出'], contradicts: ['bad', '反向'], agrees: ['ok', '认可'], disagrees: ['bad', '不认可'], 'no score': ['', '无分'] }
// Sync results: the EC2 applier's statuses (sync/host/apply_release.py) and the backend's own.
const SYNC_STATUS = {
  'pending-commit': ['warn', '已应用，待确认保留'], 'no-op': ['ok', '已是当前版本，没有变化'], committed: ['ok', '已确认保留'],
  'rolled-back': ['warn', '已回滚'], 'rolled-back-files-only': ['bad', '只回滚了文件（场景的 AWS 资源还在）'], 'smoke-failed': ['bad', '冒烟检查失败'],
  'not-confirmed': ['bad', '计划和体检时不一致，没有应用'], refused: ['bad', '被拒绝'], error: ['bad', '出错'], idle: ['', '空闲'],
  waiting: ['info', '等待中'], configured: ['', '已配置'], 'no-active-release': ['warn', '远端还没有当前版本'], success: ['ok', '成功'], failed: ['bad', '失败'],
}
const CAL_VERDICT = { stable: ['ok', '稳定'], moderate: ['warn', '中等'], unstable: ['bad', '不稳定'] }
// Per-case verdicts (engine l1.py / guided_report.case_table): L1, the combined verdict, Mind the Goal.
const L1_VERDICT = { pass: ['ok', '通过'], fail: ['bad', '不通过'], defer: ['warn', '交给裁判'], unverified: ['warn', '未核实'], error: ['bad', '出错'], 'n/a': ['', '不适用'] }
const COMBINED = { pass: ['ok', '通过'], fail: ['bad', '不通过'], undetermined: ['warn', '无法判定'] }
const MTG_LABEL = { pass: '通过', fail: '不通过' }
const MATERIAL_USE = { source: '作为事实来源', background: '背景参考', exclude: '不发给 Kiro' }
const EXTRACTION = { ok: ['ok', '已提取'], partial: ['warn', '部分提取'], failed: ['bad', '提取失败'] }
const CLASSIFICATION = { synthetic: '合成数据', internal: '内部资料', confidential: '机密（不能发给 Kiro）' }
const FAMILY = {
  teaching: ['acc', '教学'], l1: ['acc', 'L1'], guide: ['info', '手册'], provenance: ['warn', '来源'], residue: ['warn', '残留'],
  namespace: ['', '命名'], tools: ['', '工具'], materials: ['warn', '资料'], golden: ['', '题目'], gate: ['warn', '审核'],
  schema: ['bad', '结构'], xref: ['bad', '引用'], render: ['bad', '渲染'], project: ['', '项目'], rehearsal: ['warn', '彩排整改'],
}
const DRY_SKIP = { 'not requested': '这次是构建前的校验，不单独试构建', 'policy errors': '有策略错误，先修', 'the scenario did not load': 'scenario 读不出来' }
const STEPS = [
  { id: '1', n: '01', label: '场景与课程' },
  { id: '2', n: '02', label: '内容与评估' },
  { id: '3', n: '03', label: '交付' },
  { id: '4', n: '04', label: '彩排与上课' },
]

// ---------------------------------------------------------------------------
// Small pure helpers
// ---------------------------------------------------------------------------

const cx = (...names) => names.filter(Boolean).join(' ')
const pick = (map, key, fallback) => map[key] || [fallback === undefined ? '' : fallback, key || '—']
const isLive = (g) => !!g && (g.status === 'queued' || g.status === 'running')
const jobActive = (job) => !!job && (job.status === 'queued' || job.status === 'running')
const asText = (v) => (v === null || v === undefined ? '' : typeof v === 'string' ? v : JSON.stringify(v))
// The engine writes rehearsal texts in the pack language with an English twin (<field>En): show the twin on
// hover when it differs, so the SA can compare with the English repair contract.
const englishTwin = (text, en) => (typeof en === 'string' && en && en !== text ? en : undefined)
const listOf = (v) => (Array.isArray(v) ? v : [])
// A rehearsal warning is { code, message, messageEn, refs }; older records may hold a plain string. The refs
// (the phenomenon and case, or the runs) lead the message, except those it already names.
function warningText(w) {
  if (!w || typeof w !== 'object' || typeof w.message !== 'string') return asText(w)
  const refs = listOf(w.refs).filter((ref) => typeof ref === 'string' && ref && !w.message.includes(ref))
  return refs.length ? `${refs.join(' · ')}：${w.message}` : w.message
}
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : '—')
const fmtNum = (v, digits) => (typeof v === 'number' && Number.isFinite(v) ? String(Math.round(v * 10 ** (digits ?? 2)) / 10 ** (digits ?? 2)) : '—')
const fmtBytes = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n || 0} B`)
const shortHash = (s) => (s ? String(s).slice(0, 12) : '—')
function fmtTokens(n) {
  const v = Number(n) || 0
  return v >= 10000 ? `${(v / 10000).toFixed(1)} 万` : String(v)
}

function fmtSeconds(seconds) {
  const v = Math.max(0, Math.round(Number(seconds) || 0))
  return v >= 60 ? `${Math.floor(v / 60)} 分 ${String(v % 60).padStart(2, '0')} 秒` : `${v} 秒`
}
function detailValue(key, v) {
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (key === 'nextStep') return STEP_LABEL[v] || v || '—'
  if (typeof v === 'string' && /status$/i.test(key)) return (SYNC_STATUS[v] || SYNC_STATUS[v.toLowerCase()] || [])[1] || v
  if (Array.isArray(v)) return v.map((x) => (x && typeof x === 'object' ? x.name || x.code || x.id || JSON.stringify(x) : String(x))).join('、') || '—'
  if (v !== null && typeof v === 'object') return JSON.stringify(v)
  return v === null || v === undefined ? '—' : String(v)
}
function detailText(detail) {
  if (detail === null || detail === undefined) return ''
  if (typeof detail !== 'object') return String(detail)
  // The applier's raw answer and the timestamp are noise here; the job JSON keeps them.
  const text = Object.entries(detail).filter(([k, v]) => k !== 'applier' && k !== 'at' && k !== 'plan' && v !== undefined)
    .map(([k, v]) => `${DETAIL_KEY[k] || k}：${detailValue(k, v)}`).join(' · ')
  return text.length > 300 ? `${text.slice(0, 300)}…` : text
}
const stageNote = (note) => STAGE_NOTE[note] || note
// A sync record's outcome: its own status (apply, rollback), else the EC2 applier's.
const syncResult = (r) => (r && (r.status || (r.applier && typeof r.applier === 'object' && r.applier.status))) || ''
const syncStatus = (s) => (s ? (SYNC_STATUS[s] || SYNC_STATUS[String(s).toLowerCase()] || ['', s]) : ['', '—'])
// `accept` for the material picker: the backend lists extensions with their dot ('.md', '.pdf').
function acceptList(types) {
  return listOf(types).map((t) => String(t || '').trim()).filter(Boolean).map((t) => (t.startsWith('.') ? t : `.${t}`)).join(',')
}
const extensionOf = (name) => { const m = /\.[^./\\]+$/.exec(String(name || '')); return m ? m[0].toLowerCase() : '' }
// Guides start with machine markers (<!-- workshop-customizer:student-guide … -->); the viewer drops them.
function stripGuideMarkers(markdown) {
  return String(markdown || '').split('\n').filter((line) => !/^\s*<!--.*-->\s*$/.test(line)).join('\n').replace(/^\s*\n+/, '')
}
// A blank project's scenario carries template placeholders ("TODO who asks the questions").
const isPlaceholder = (v) => !v || /^\s*TODO\b/.test(String(v))
function verdictLabel(map, v) {
  if (v === null || v === undefined || v === '') return ['', '—']
  return map[v] || map[String(v).toLowerCase()] || ['', String(v)]
}
const mtgLabel = (v) => (v ? MTG_LABEL[String(v).toLowerCase()] || String(v) : '—')
const familyOf = (code) => FAMILY[String(code || '').split('.')[0]] || ['', String(code || '其他').split('.')[0]]
const filePath = (rel) => String(rel).split('/').map(encodeURIComponent).join('/')

// Scenario views (the live scenario.yaml, parsed by the backend).
const teachingOf = (sc) => (sc && sc.labs && sc.labs.teaching) || null
const goldenOf = (sc) => listOf(sc && sc.evaluation && sc.evaluation.goldenSet)
const phenomenaOf = (sc) => listOf(teachingOf(sc) && teachingOf(sc).phenomena)
const phenomenaOfCase = (sc, cid) => phenomenaOf(sc).filter((p) => listOf(p.caseIds).includes(cid)).map((p) => p.id)
function expectationChips(expected) {
  const e = expected || {}
  const out = []
  for (const t of listOf(e.requiredTools)) out.push(['acc', `必须调用 ${t}`])
  for (const t of listOf(e.forbiddenTools)) out.push(['warn', `不得调用 ${t}`])
  for (const t of listOf(e.mustMention)) out.push(['acc', `要提到 “${t}”`])
  for (const g of listOf(e.mustMentionAnyOf)) out.push(['acc', listOf(g).length > 3 ? `至少提到其一：${listOf(g).slice(0, 3).join(' / ')} …共 ${listOf(g).length} 种说法` : `至少提到其一：${listOf(g).join(' / ')}`])
  for (const t of listOf(e.mustNotMention)) out.push(['warn', `不得出现 “${t}”`])
  if (e.shouldRefuse) out.push(['warn', '应拒绝'])
  if (e.shouldEscalate) out.push(['info', '应升级转人'])
  return out
}
// The findings the repair route sends (routes._selected_findings): errors, teaching.* and residue.prose
// always; every other warning only with includeWarnings.
const repairSelected = (f, includeWarnings) => f.severity === 'error' || includeWarnings || String(f.code).startsWith('teaching.') || f.code === 'residue.prose'
// The rehearsal remediation a repair or regenerate also sends, as findings R1..Rn: GET validation lists the
// current release's (routes._rehearsal_findings); an explicit scope keeps only those sharing one of its scopes.
const rehearsalFindingsFor = (view, scope) => listOf(view && view.rehearsalFindings).filter((f) => !listOf(scope).length || listOf(f.scopes).some((s) => listOf(scope).includes(s)))
// The scopes a repair may rewrite (routes.prepare_generation): the explicit scope, else the validation's
// suggested scopes (or the selected findings' own) plus every sent rehearsal finding's scopes.
function repairScopes(scope, v, selected, rehearsal) {
  if (listOf(scope).length) return SCOPES.filter((s) => scope.includes(s))
  const suggested = listOf(v && v.repair && v.repair.suggestedScopes)
  const scopes = new Set([...(suggested.length ? suggested : listOf(selected).flatMap((f) => listOf(f.scopes))), ...listOf(rehearsal).flatMap((f) => listOf(f.scopes))])
  return SCOPES.filter((s) => scopes.has(s))
}
// Why a generation would be refused for its materials (routes._check_material_policy), or null. Every
// usable material that is not excluded is sent (repair: source materials only), and that needs a
// recorded materials approval classed synthetic or internal; customer materials also need who approved.
function materialsBlock(materials, policy, sourceOnly) {
  const usable = listOf(materials).filter((m) => {
    const use = m.generationUse || 'source'
    return use !== 'exclude' && (use === 'source' || !sourceOnly) && !!m.extraction && ['ok', 'partial'].includes(m.extraction.status)
  })
  if (!usable.length) return null
  if (policy && policy.dataClassification === 'confidential') return '资料批准记为“机密”：机密资料不能发给 Kiro。上传脱敏版本，或把资料改成“不发给 Kiro”。'
  if (!policy || !['synthetic', 'internal'].includes(policy.dataClassification)) return '先记录资料批准：有资料会发给 Kiro，要写明谁批准的、数据级别（01 → 客户资料）。'
  if (usable.some((m) => m.author !== 'sa') && !String(policy.approvalRef || '').trim()) return '先记录资料批准人：客户提供的资料要写明谁批准发给 Kiro（01 → 客户资料）。'
  return null
}
const wsMaterialsBlock = (ws, sourceOnly) => materialsBlock(ws.materials && ws.materials.materials, (ws.project && ws.project.materialsPolicy) || (ws.materials && ws.materials.policy), sourceOnly)

// ---------------------------------------------------------------------------
// Transport + API client: every backend route the page calls, and nothing else
// ---------------------------------------------------------------------------

function apiError(err, status, data) {
  if (err && err.isApiError) return err
  const e = new Error((data && data.error) || err?.data?.error || err?.message || String(err))
  e.isApiError = true
  e.status = status ?? err?.status ?? null
  e.data = data ?? err?.data ?? null
  return e
}

function useTransport() {
  const hostApi = useAppApi()
  const call = useCallback(async (method, path, body) => {
    let response
    try {
      response = await fetch(PROCESS_BASE + path, {
        method,
        credentials: 'same-origin',
        headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
    } catch (err) { throw apiError(err) }
    const type = response.headers.get('content-type') || ''
    const payload = type.includes('application/json') ? await response.json() : await response.text()
    if (!response.ok) throw apiError(new Error(`API ${response.status}`), response.status, typeof payload === 'object' ? payload : { error: String(payload).slice(0, 300) })
    return payload
  }, [])
  const agentCall = useCallback(async (method, path, body) => {
    const fn = hostApi[method.toLowerCase()]
    try { return await (body === undefined ? fn(AGENT_BASE + path) : fn(AGENT_BASE + path, body)) }
    catch (err) { throw apiError(err) }
  }, [hostApi])
  return { call, agentCall }
}

function makeApi(call, agentCall) {
  return {
    health: () => call('GET', '/health'),
    templates: () => call('GET', '/templates'),
    listProjects: () => call('GET', '/projects'),
    createProject: (body) => call('POST', '/projects', body),
    project: (pid) => call('GET', `/projects/${pid}`),
    scenario: (pid) => call('GET', `/projects/${pid}/scenario`),
    saveScenario: (pid, yaml) => call('PUT', `/projects/${pid}/scenario`, { yaml }),
    readFile: (pid, rel) => call('GET', `/projects/${pid}/files/${filePath(rel)}`),
    saveFile: (pid, rel, content) => call('PUT', `/projects/${pid}/files/${filePath(rel)}`, { content }),
    pruneFiles: (pid, body) => call('POST', `/projects/${pid}/files/prune`, body),
    switchPackKind: (pid, body) => call('POST', `/projects/${pid}/pack-kind`, body),
    items: (pid) => call('GET', `/projects/${pid}/items`),
    confirmItem: (pid, id, body) => call('POST', `/projects/${pid}/items/${id}/confirm`, body),
    confirmBatch: (pid, body) => call('POST', `/projects/${pid}/items/confirm-batch`, body),
    materials: (pid) => call('GET', `/projects/${pid}/materials`),
    uploadMaterial: (pid, body) => call('POST', `/projects/${pid}/materials`, body),
    updateMaterial: (pid, mid, body) => call('PUT', `/projects/${pid}/materials/${mid}`, body),
    deleteMaterial: (pid, mid) => call('DELETE', `/projects/${pid}/materials/${mid}`),
    materialText: (pid, mid) => call('GET', `/projects/${pid}/materials/${mid}/text`),
    recordMaterialApproval: (pid, body) => call('POST', `/projects/${pid}/materials/approval`, body),
    validate: (pid, dryBuild) => call('POST', `/projects/${pid}/validate`, { dryBuild }),
    validation: (pid) => call('GET', `/projects/${pid}/validation`),
    calibrate: (pid, body) => call('POST', `/projects/${pid}/calibrate`, body),
    build: (pid) => call('POST', `/projects/${pid}/build`, {}),
    release: (pid) => call('GET', `/projects/${pid}/release`),
    golden: (pid, instructor) => call('GET', `/projects/${pid}/golden${instructor ? '?instructor=1' : ''}`),
    guide: (pid, audience) => call('GET', `/projects/${pid}/guide?audience=${audience}`),
    guidePreview: (pid, audience) => call('GET', `/projects/${pid}/guide/preview?audience=${audience}`),
    putTarget: (pid, body) => call('PUT', `/projects/${pid}/target`, body),
    preflight: (pid) => call('POST', `/projects/${pid}/sync/preflight`, {}),
    syncApply: (pid, body) => call('POST', `/projects/${pid}/sync/apply`, body),
    startSyncJob: (pid, action) => call('POST', `/projects/${pid}/sync/${action}/job`, {}),
    latestSyncJob: (pid, action) => call('GET', `/projects/${pid}/sync/${action}/job`),
    syncHistory: (pid) => call('GET', `/projects/${pid}/sync/history`),
    startOneclick: (pid) => call('POST', `/projects/${pid}/oneclick`, { acknowledged: true }),
    latestOneclick: (pid) => call('GET', `/projects/${pid}/oneclick`),
    job: (pid, id) => call('GET', `/projects/${pid}/jobs/${id}`),
    run: (pid) => call('GET', `/projects/${pid}/run`),
    runNext: (pid) => call('POST', `/projects/${pid}/run/next`, {}),
    runStep: (pid, stepId) => call('POST', `/projects/${pid}/run/steps/${stepId}/start`, {}),
    runPoll: (pid) => call('POST', `/projects/${pid}/run/poll`, {}),
    runReset: (pid, fromStep) => call('POST', `/projects/${pid}/run/reset`, fromStep ? { fromStep } : {}),
    report: (pid) => call('GET', `/projects/${pid}/run/report`),
    direct: (pid) => call('GET', `/projects/${pid}/direct`),
    startDirect: (pid, body) => call('POST', `/projects/${pid}/direct`, body),
    startDirectChat: (pid, body) => call('POST', `/projects/${pid}/direct/chat`, body),
    directChat: (pid, id) => call('GET', `/projects/${pid}/direct/chat/${id}`),
    startDirectCleanup: (pid) => call('POST', `/projects/${pid}/direct/cleanup`, { acknowledged: true }),
    rehearsal: (pid) => call('GET', `/projects/${pid}/run/rehearsal`),
    recordRehearsal: (pid) => call('POST', `/projects/${pid}/run/rehearsal`, {}),
    // Kiro generation: in-gateway AppRoutes (app/backend/routes.py register_routes).
    startGeneration: (body) => agentCall('POST', '/generations', body),
    latestGeneration: (pid) => agentCall('GET', `/generations/latest/${pid}`),
    generation: (id) => agentCall('GET', `/generations/${id}`),
    applyGeneration: (id, body) => agentCall('POST', `/generations/${id}/apply`, body),
    revertGeneration: (id) => agentCall('POST', `/generations/${id}/revert`, { acknowledged: true }),
  }
}

// Plain download links (the browser saves the response).
const LINKS = {
  release: (pid) => `${PROCESS_BASE}/projects/${pid}/export`,
  instructorBundle: (pid) => `${PROCESS_BASE}/projects/${pid}/export?bundle=instructor`,
  guideDownload: (pid, audience) => `${PROCESS_BASE}/projects/${pid}/guide?audience=${audience}&download=1`,
}

const ApiContext = createContext(null)
const WsContext = createContext(null)
const useApi = () => useContext(ApiContext)
const useWs = () => useContext(WsContext)

function useApiValue() {
  const { call, agentCall } = useTransport()
  const hostNotify = useNotify?.()
  const api = useMemo(() => makeApi(call, agentCall), [call, agentCall])
  const notify = useMemo(() => ({
    error: (m) => (hostNotify?.error ? hostNotify.error(m) : console.error(m)),
    success: (m) => (hostNotify?.success ? hostNotify.success(m) : console.log(m)),
  }), [hostNotify])
  return useMemo(() => ({ api, notify }), [api, notify])
}

function useNow(active) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!active) return undefined
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [active])
  return now
}

// ---------------------------------------------------------------------------
// Workspace store: resources, polling and actions for one project
// ---------------------------------------------------------------------------

const RESOURCES = {
  project: (api, pid) => api.project(pid),
  scenario: (api, pid) => api.scenario(pid),
  items: (api, pid) => api.items(pid),
  materials: (api, pid) => api.materials(pid),
  validation: (api, pid) => api.validation(pid),
  generation: (api, pid) => api.latestGeneration(pid).then((r) => (r && r.generation) || null),
  release: (api, pid) => api.release(pid),
  run: (api, pid) => api.run(pid),
  oneclick: (api, pid) => api.latestOneclick(pid).then((r) => (r && r.job) || null),
  syncJob: (api, pid) => Promise.all(SYNC_ACTIONS.map(([a]) => api.latestSyncJob(pid, a).then((r) => (r && r.job) || null).catch(() => null)))
    .then((jobs) => jobs.filter(Boolean).sort((a, b) => String(a.createdAt).localeCompare(String(b.createdAt))).pop() || null),
  history: (api, pid) => api.syncHistory(pid).then((r) => listOf(r && r.history)),
  // Read after run: GET run creates the run state that rehearsal reads.
  rehearsal: (api, pid) => api.rehearsal(pid),
  report: (api, pid) => api.report(pid),
  direct: (api, pid) => api.direct(pid),
}
const LATE = ['rehearsal', 'report']
const ALL = Object.keys(RESOURCES)
const POLL_MS = 2000
const POLL_MAX_FAILURES = 5

function usePoll(active, key, tick) {
  // While `active`, runs `tick` every POLL_MS; `tick` returns true to keep polling (it refreshes what
  // changed itself when the watched thing settles). Backs off on errors and gives up after
  // POLL_MAX_FAILURES in a row (returns true then: the page says it lost contact).
  const [round, setRound] = useState(0)
  const [failures, setFailures] = useState(0)
  const tickRef = useRef(tick)
  tickRef.current = tick
  useEffect(() => { setFailures(0) }, [key])
  useEffect(() => {
    if (!active || failures >= POLL_MAX_FAILURES) return undefined
    let live = true
    const timer = setTimeout(async () => {
      try {
        const again = await tickRef.current()
        if (!live) return
        setFailures(0)
        if (again) setRound((n) => n + 1)
      } catch (_) { if (live) setFailures((n) => n + 1) }
    }, failures ? POLL_MS * 2 : POLL_MS)
    return () => { live = false; clearTimeout(timer) }
  }, [active, key, failures, round])
  return failures >= POLL_MAX_FAILURES
}

function useWorkspace(pid, go) {
  const { api, notify } = useApi()
  const [res, setRes] = useState({})
  const [busy, setBusy] = useState('')
  const [errors, setErrors] = useState({})
  const [brief, setBrief] = useState(null)
  const [preflight, setPreflight] = useState(null)
  const [runAll, setRunAll] = useState(false)

  const put = useCallback((name, patch) => setRes((prev) => ({ ...prev, [name]: { ...(prev[name] || {}), ...patch } })), [])
  const load = useCallback(async (name) => {
    try {
      const data = await RESOURCES[name](api, pid)
      put(name, { data, error: null, loaded: true })
      return data
    } catch (err) {
      put(name, { data: null, error: apiError(err), loaded: true })
      return null
    }
  }, [api, pid, put])
  const refresh = useCallback(async (...names) => {
    const list = names.length ? names : ALL
    await Promise.all(list.filter((n) => !LATE.includes(n)).map(load))
    await Promise.all(list.filter((n) => LATE.includes(n)).map(load))
  }, [load])
  useEffect(() => { setRes({}); setBrief(null); setPreflight(null); setErrors({}); setRunAll(false); refresh() }, [pid])

  const d = (name) => (res[name] ? res[name].data : undefined)
  const project = d('project')
  useEffect(() => { if (brief === null && project) setBrief(project.brief || '') }, [project])

  // One user action at a time; its error is shown next to the button (and as a host notification).
  const act = useCallback(async (name, fn, after, success) => {
    setBusy(name)
    setErrors((prev) => ({ ...prev, [name]: null }))
    try {
      const out = await fn()
      if (success) notify.success(typeof success === 'function' ? success(out) : success)
      return out
    } catch (err) {
      const e = apiError(err)
      setErrors((prev) => ({ ...prev, [name]: e }))
      notify.error(backendText(e.message) || e.message)
      return undefined
    } finally {
      setBusy('')
      if (after === 'all') await refresh()
      else if (after && after.length) await refresh(...after)
    }
  }, [notify, refresh])

  const generation = d('generation')
  const startGeneration = (body, name) => act(name || 'generate', async () => {
    try {
      const record = await api.startGeneration({ projectId: pid, ...body })
      put('generation', { data: record, error: null })
      return record
    } catch (err) {
      const e = apiError(err)
      if (e.data && e.data.generation) put('generation', { data: e.data.generation, error: null })
      throw e
    }
  }, ['project'], (r) => `Kiro 开始${GEN_MODE[r.mode] || '生成'}：${r.staged ? '分步起草大约 10–20 分钟' : '要几分钟'}，可以离开页面`)
  const ensureFreshValidation = async () => {
    const current = await api.validation(pid)
    if (!current || !current.fresh) await api.validate(pid, true)
  }
  const startRepair = ({ includeWarnings, instructions, scope, includeRehearsal }, name) => act(name || 'repair', async () => {
    await ensureFreshValidation()
    const body = { projectId: pid, mode: 'repair', includeWarnings: !!includeWarnings }
    if (instructions && instructions.trim()) body.instructions = instructions.trim()
    if (scope && scope.length) body.scope = scope
    if (includeRehearsal === false) body.includeRehearsal = false // the route adds the rehearsal's R findings unless told not to
    try {
      const record = await api.startGeneration(body)
      put('generation', { data: record, error: null })
      return record
    } catch (err) {
      const e = apiError(err)
      if (e.data && e.data.generation) put('generation', { data: e.data.generation, error: null })
      throw e
    }
  }, ['validation', 'project'], () => 'Kiro 开始修复：要几分钟，可以离开页面')

  const actions = {
    refresh,
    switchPackKind: (packKind, reason) => act('packKind', () => api.switchPackKind(pid, { packKind, reason, acknowledged: true }), 'all', '项目类型已切换'),
    uploadMaterial: (body) => act('upload', () => api.uploadMaterial(pid, { ...body, acknowledged: true }), ['materials'], (m) => `已上传 ${m.name}`),
    updateMaterial: (mid, body) => act(`material:${mid}`, () => api.updateMaterial(pid, mid, body), ['materials']),
    deleteMaterial: (mid) => act(`material:${mid}`, async () => {
      const deleted = await api.deleteMaterial(pid, mid)
      await api.validate(pid, true).catch(() => null) // a reviewed item may cite it: the verdict must say so now
      return deleted
    }, ['materials', 'validation', 'project'],
      (r) => (listOf(r.referencedBy).length ? `已删除；仍有 ${r.referencedBy.length} 项内容引用它，请重新审核` : '已删除')),
    materialText: (mid) => api.materialText(pid, mid),
    recordApproval: (body) => act('approval', () => api.recordMaterialApproval(pid, body), ['project', 'materials'], '已记录资料批准'),
    startDraft: (answers) => startGeneration({ mode: 'draft', brief: brief || '', ...(answers && answers.length ? { answers } : {}) }),
    startRegenerate: (scope, includeRehearsal) => startGeneration({ mode: 'regenerate', brief: brief || '', ...(scope && scope.length ? { scope } : {}),
      ...(includeRehearsal === false ? { includeRehearsal: false } : {}) }, 'regenerate'),
    startRepair,
    startRemediation: (hint) => {
      const asset = hint.asset || {}
      const where = [asset.file, asset.scenarioPath].filter(Boolean).join(' · ')
      // The English original goes along: the repair contract is English, the SA reads the pack language.
      const en = englishTwin(hint.action, hint.actionEn)
      const text = `彩排整改 ${hint.id} (${hint.code}): ${hint.action}\n原因: ${hint.because}${where ? `\n位置: ${where}` : ''}`
        + (en ? `\nEnglish: ${en}${hint.becauseEn ? ` Because: ${hint.becauseEn}` : ''}` : '')
      // One hint, one rehearsal item: this hint goes as the instructions, the rehearsal's other hints stay out.
      return startRepair({ instructions: text.slice(0, 4000), scope: listOf(asset.scopes), includeRehearsal: false }, `remedy:${hint.id}`)
        .then((r) => { if (r) go('2', 'eval'); return r })
    },
    applyGeneration: (g, resets) => act('apply', async () => {
      const applied = await api.applyGeneration(g.id, { acknowledged: true, acknowledgeConfirmationResets: !!resets })
      await api.validate(pid, true) // the draft is applied: show its findings right away
      return applied
    }, 'all', (r) => `已应用：写入 ${listOf(r.written).length} 个文件，删除 ${listOf(r.deleted).length} 个`),
    revertGeneration: (g) => act('revert', () => api.revertGeneration(g.id), 'all', '已撤销这次应用'),
    validate: () => act('validate', () => api.validate(pid, true), ['validation', 'project'], (v) => (v.ok ? '校验通过' : '校验完成：有要改的地方')),
    confirmItem: (id, body) => act(`confirm:${id}`, async () => {
      const recorded = await api.confirmItem(pid, id, body)
      await api.validate(pid, true).catch(() => null) // the review changed the project: show the new verdict right away
      return recorded
    }, ['items', 'scenario', 'validation', 'project', 'generation'], `已记录 ${id}`),
    confirmBatch: (ids, origin) => act('batch', async () => {
      const recorded = await api.confirmBatch(pid, { ids, acknowledged: true, ...(origin ? { origin } : {}) })
      await api.validate(pid, true).catch(() => null)
      return recorded
    }, ['items', 'scenario', 'validation', 'project', 'generation'], (r) => `已把 ${r.confirmed} 项记为 SA 合成，并重新校验`),
    build: () => act('build', () => api.build(pid), ['project', 'release', 'validation', 'run', 'rehearsal', 'report'], (b) => `已构建 ${b.version}`),
    putTarget: (form) => act('target', () => api.putTarget(pid, form), ['project'], '已保存 Workshop 环境'),
    runPreflight: () => act('preflight', async () => {
      setPreflight(null)
      try { const r = await api.preflight(pid); setPreflight(r); return r } catch (err) { setPreflight({ ok: false, unreachable: true }); throw err } // e.g. a profile not logged in
    }, ['history'],
      (r) => (r.ok ? '体检通过' : '体检未通过')),
    syncApply: () => act('syncApply', async () => {
      const r = await api.syncApply(pid, { confirmToken: preflight.confirmToken })
      setPreflight(null)
      return r
    }, ['project', 'history'], (r) => `应用结果：${syncStatus(r.status)[1]}`),
    startSyncJob: (action) => act(`sync:${action}`, async () => { const job = await api.startSyncJob(pid, action); put('syncJob', { data: job }); return job }, []),
    startOneclick: () => act('oneclick', async () => {
      try {
        const job = await api.startOneclick(pid)
        put('oneclick', { data: job })
        return job
      } catch (err) {
        const e = apiError(err)
        if (e.data && e.data.jobId) { put('oneclick', { data: (await api.job(pid, e.data.jobId)).job }); return null }
        throw e
      }
    }, []),
    runNext: () => act('run', async () => { const s = await api.runNext(pid); put('run', { data: s, error: null }); return s }, []),
    runStep: (stepId) => act('run', async () => { const s = await api.runStep(pid, stepId); put('run', { data: s, error: null }); return s }, []),
    resetRun: (fromStep) => act('reset', async () => { setRunAll(false); const s = await api.runReset(pid, fromStep); put('run', { data: s, error: null }); return s },
      // A reset archives run/rehearsal.json: the repair panel's rehearsal findings (GET validation) go with it.
      ['rehearsal', 'report', 'project', 'validation'], fromStep ? `已从「${STEP_LABEL[fromStep] || fromStep}」重置` : '已新建运行记录'),
    setRunAll,
    startDirect: (compareModels, options) => act('direct', async () => {
      const job = await api.startDirect(pid, { compareModels, ...(options || {}) })
      put('direct', { data: { ...(d('direct') || {}), job } })
      return job
    }, [], '快速彩排已开始：约 8 分钟'),
    startDirectCleanup: () => act('directCleanup', async () => {
      const cleanupJob = await api.startDirectCleanup(pid)
      put('direct', { data: { ...(d('direct') || {}), cleanupJob } })
      return cleanupJob
    }, []),
    askDirect: (body) => api.startDirectChat(pid, body),
    directChat: (id) => api.directChat(pid, id),
    recordRehearsal: () => act('rehearsal', async () => { const r = await api.recordRehearsal(pid); put('rehearsal', { data: r, error: null }); return r },
      // GET validation lists the new rehearsal's blocking remediation that a repair adds (R1..Rn).
      ['project', 'validation'], (r) => `已记录彩排结论：${pick(VERDICT, r.verdict)[1]}`),
    saveScenario: (yaml) => act('scenario', () => api.saveScenario(pid, yaml), 'all', '已保存 scenario.yaml'),
    readFile: (rel) => api.readFile(pid, rel),
    saveFile: (rel, content) => act(`file:${rel}`, () => api.saveFile(pid, rel, content), ['project', 'scenario', 'validation', 'release', 'generation'], `已保存 ${rel}`),
    pruneFiles: (dryRun) => act('prune', () => api.pruneFiles(pid, dryRun ? { dryRun: true } : { dryRun: false, acknowledged: true }), dryRun ? [] : ['scenario', 'validation', 'project', 'generation']),
    calibrate: (log, dryRun) => act('calibrate', () => api.calibrate(pid, { log, dryRun }), dryRun ? [] : ['project', 'scenario', 'validation', 'release', 'generation']),
    golden: (instructor) => api.golden(pid, instructor),
    guide: (audience, preview) => (preview ? api.guidePreview(pid, audience) : api.guide(pid, audience)),
  }

  // Polling: the live Kiro generation, background jobs, and the running Guided Run step.
  const genLost = usePoll(isLive(generation), generation && `${generation.id}:${generation.status}`, async () => {
    const next = await api.generation(generation.id)
    put('generation', { data: next, error: null })
    if (!isLive(next)) {
      if (next.status === 'ready') {
        const pre = precheckErrors(next)
        if (pre) notify.error(`Kiro 草稿已生成，但应用前预检有 ${pre} 个错误：先看草稿下方的预检`)
        else notify.success(next.mode === 'repair' ? 'Kiro 修复结果已生成：看过后应用' : 'Kiro 草稿已生成：预检没有错误，看过后应用')
      }
      if (next.status === 'failed') notify.error(next.error || 'Kiro 生成失败')
      refresh('project')
    }
    return isLive(next)
  })
  const oneclick = d('oneclick')
  const oneclickLost = usePoll(jobActive(oneclick), oneclick && oneclick.id, async () => {
    const next = (await api.job(pid, oneclick.id)).job
    put('oneclick', { data: next })
    if (!jobActive(next)) {
      if (next.status === 'succeeded') notify.success('一键覆盖完成')
      refresh()
    }
    return jobActive(next)
  })
  const syncJob = d('syncJob')
  const syncLost = usePoll(jobActive(syncJob), syncJob && syncJob.id, async () => {
    const next = (await api.job(pid, syncJob.id)).job
    put('syncJob', { data: next })
    if (!jobActive(next)) refresh('project', 'history', 'run')
    return jobActive(next)
  })
  const direct = d('direct')
  const directJob = direct && (jobActive(direct.cleanupJob) ? direct.cleanupJob : direct.job)
  const directLost = usePoll(jobActive(directJob), directJob && directJob.id, async () => {
    const next = (await api.job(pid, directJob.id)).job
    put('direct', { data: { ...direct, [next.kind === 'direct' ? 'job' : 'cleanupJob']: next } })
    if (!jobActive(next)) {
      if (next.status === 'succeeded') notify.success(next.kind === 'direct' ? `快速彩排完成：${pick(DIRECT_VERDICT, next.result && next.result.verdict)[1]}` : '直连资源已清理')
      refresh('direct', 'validation')
    }
    return jobActive(next)
  })
  const run = d('run')
  const runningId = run ? listOf(run.stepOrder).find((id) => run.steps[id] && run.steps[id].status === 'running') : null
  const runLost = usePoll(!!runningId, runningId && `${runningId}:${run.steps[runningId].commandId}`, async () => {
    const next = await api.runPoll(pid)
    put('run', { data: next, error: null })
    if (!next.currentStepId) refresh('rehearsal', 'report', 'project')
    return !!next.currentStepId
  })
  // Run all: after a step passes, start the next one until the run passes or fails.
  useEffect(() => {
    if (!runAll || !run || run.currentStepId || busy) return undefined
    if (run.status === 'failed' || run.status === 'passed') { setRunAll(false); return undefined }
    const timer = setTimeout(() => actions.runNext().then((s) => { if (!s) setRunAll(false) }), 600)
    return () => clearTimeout(timer)
  }, [runAll, run && run.updatedAt, run && run.currentStepId, run && run.status, busy])

  const ws = {
    pid, res, busy, errors, brief: brief || '', setBrief, preflight, runAll, go,
    project, scenarioView: d('scenario'), sc: d('scenario') ? d('scenario').scenario : null, items: d('items'),
    materials: d('materials'), validationView: d('validation'), generation, release: d('release'), run, runningId,
    oneclick, syncJob, history: d('history') || [], rehearsal: d('rehearsal'), report: d('report'), direct,
    lost: { generation: genLost, oneclick: oneclickLost, sync: syncLost, run: runLost, direct: directLost },
    ...actions,
  }
  ws.ready = readiness(ws)
  return ws
}

// ---------------------------------------------------------------------------
// Readiness: "离可以上课还差 N 件事", derived from the backend state only
// ---------------------------------------------------------------------------

// Errors in the content itself (schema, references, policy, the dry build, the project's files); the provenance
// gate is counted apart: it holds every Kiro draft until the SA reviews it, which is the review step, not an error.
const needsCustomer = (i) => i.criticality === 'blocking' && i.provenance !== 'customer_confirmed'
function contentErrorCount(v) {
  return listOf(v.schema).length + listOf(v.render).length
    + listOf(v.policy).filter((f) => f.severity === 'error').length + listOf(v.output).filter((f) => f.severity === 'error').length
    + listOf(v.workspace).filter((f) => f.severity === 'error').length
}
const UNREVIEWED_GATE = /^provenance '(ai_draft|pending)' cannot enter a formal pack/ // engine scenario.provenance_gate
const isGateFinding = (f) => f.source === 'gate' || String(f.code || '').startsWith('gate.')
const isUnreviewed = (f) => isGateFinding(f) && UNREVIEWED_GATE.test(String(f.messageEn || f.message || ''))
// Gate items that are neither "not reviewed yet", the customer's confirmations nor the missing anchors (each has its own readiness item).
const otherGateCount = (v) => listOf(v.gate).filter((g) => !UNREVIEWED_GATE.test(String(g.reason || '')) && !CUSTOMER_GATE.test(String(g.reason || '')) && g.item_type !== 'golden set').length
const GATE_TYPE = { fact: '业务规则', tool: '工具', knowledge_document: '知识文档', golden_case: '题目', golden_set: '题目与判据', guide_narrative: '手册叙述' }
// The gate's reasons (engine, English), as the SA reads them.
const GATE_TEXT = [
  [/^provenance 'ai_draft'/, () => 'Kiro 草稿，还没审核；审核后才能进正式包'],
  [/^provenance 'pending'/, () => '待定，还没审核；审核后才能进正式包'],
  [/anchor golden case per category.*missing: (.*)$/, (m) => `Workshop 包要求正常、边界、禁止三类各有一道客户确认的锚点题（它依据的规则都已客户确认）；还缺：${m[1].split(', ').map((c) => CATEGORY_LABEL[c] || c).join('、')}`],
  [/simulated confirmation/, () => '“客户已确认”带的是模拟的确认信息；只能记录真实的客户确认'],
  [/must carry origin/, () => 'SA 合成的阻断项要标来源（客户资料 / SA 编写 / 教学设计），或请客户确认'],
  [/requires provenance 'customer_confirmed'/, () => '客户严格确认的包：这一项要客户确认'],
]
// A schema error ("labs/teaching/phenomena/1/absentTerms/2: '猫' is too short") by the item's id and the field's name.
const SCHEMA_LISTS = [['labs/teaching/phenomena', '教学现象'], ['labs/teaching/baselineDefects', '弱基线缺陷'], ['evaluation/goldenSet', '题目'],
  ['knowledge/documents', '知识文档'], ['agent/roles', '角色'], ['facts', '业务规则'], ['tools', '工具'], ['skills', 'Skill']]
const SCHEMA_FIELD = {
  absentTerms: '本该缺失的词', baitTerms: '诱饵词', mustMention: '必须提到', mustMentionAnyOf: '至少提到其一', mustNotMention: '不得出现',
  query: '问题', label: '标题', statement: '规则', teachingPoint: '教学要点', caseIds: '题目', criticality: '重要性', category: '类别',
  set: '练习/留出', basis: '依据', expected: '判据', description: '说明', name: '名称', id: 'id', title: '标题', outline: '提纲',
}
const SCHEMA_SAID = [
  [/^(.+) is too short$/, '$1 太短'], [/^(.+) is too long$/, '$1 太长'], [/^(.+) is not one of \[(.*)\]$/, '$1 不是允许的值（可选：$2）'],
  [/^'(.+)' is a required property$/, '缺少必填字段 $1'], [/^(.+) does not match .+$/, '$1 格式不对'], [/^(.+) is not of type (.+)$/, '$1 类型不对（应为 $2）'],
  [/^Additional properties are not allowed \((.+) (?:was|were) unexpected\)$/, '多了不认识的字段：$1'], [/^(.+) should be non-empty$/, '$1 不能为空'],
]
function schemaText(text, sc) {
  const m = String(text || '').match(/^([^:\s]+): (.*)$/)
  if (!m) return text
  const parts = m[1].split('/')
  let where = m[1]
  for (const [prefix, label] of SCHEMA_LISTS) {
    const pre = prefix.split('/')
    if (!pre.every((x, i) => parts[i] === x) || !/^\d+$/.test(parts[pre.length] || '')) continue
    const list = pre.reduce((o, k) => (o && typeof o === 'object' ? o[k] : null), sc)
    const item = Array.isArray(list) ? list[Number(parts[pre.length])] : null
    const id = item && (item.id || item.name)
    const field = parts.slice(pre.length + 1).filter((x) => !/^\d+$/.test(x)).map((x) => SCHEMA_FIELD[x] || x).join(' · ')
    where = `${label}${id ? ` ${id}` : ` 第 ${Number(parts[pre.length]) + 1} 项`}${field ? ` 的「${field}」` : ''}`
    break
  }
  const said = SCHEMA_SAID.reduce((t, [re, out]) => (re.test(t) ? t.replace(re, out) : t), m[2])
  return `${where}：${said}`
}

// What stands between a clean draft and a build: the SA's review of Kiro's items, and for a workshop pack the
// customer-confirmed anchor questions. The precheck, the validation card, the next step and 03 all say it alike.
function reviewWork(review, anchors, packKind, need) {
  const missing = listOf(anchors).map((c) => CATEGORY_LABEL[c] || c)
  if (packKind === 'customer') { // strict: every blocking item needs the customer's own confirmation
    const steps = [review ? `请客户逐项确认 Kiro 起草的${need ? ` ${need} 项` : ''}阻断项（确认人 + 凭证；SA 合成不能替代），其余由你在「02 → 审核」确认`
      : need ? `请客户逐项确认 ${need} 项阻断项（确认人 + 凭证；SA 合成不能替代），在「02 → 审核」记录` : null].filter(Boolean)
    return { verdict: '等客户确认', steps, text: steps.length ? `${steps[0]}，之后才能构建` : '' }
  }
  const steps = [review ? `在「02 → 审核」确认 ${review} 项 Kiro 草稿` : null, missing.length ? `请客户确认${missing.join('、')}各一道锚点题（它依据的规则都要客户确认）` : null].filter(Boolean)
  return { verdict: review && missing.length ? '等审核和客户确认' : missing.length ? '等客户确认' : '等你审核', steps,
    text: steps.length > 1 ? `${steps.map((t, i) => `${'①②'[i]} ${t}`).join('；')}。两件都做完才能构建` : steps.length ? `${steps[0]}，之后才能构建` : '' }
}
const ANCHOR_GATE = /anchor golden case per category/
const CUSTOMER_GATE = /requires provenance 'customer_confirmed'/ // a 客户严格确认 pack's blocking item the customer has not confirmed

function gateText(message) {
  for (const [re, text] of GATE_TEXT) { const m = String(message || '').match(re); if (m) return text(m) }
  return message
}

function readiness(ws) {
  const out = []
  const add = (key, step, tone, text, tab) => out.push({ key, step, tone, text, tab })
  const p = ws.project
  if (!p) return out
  const g = ws.generation
  if (isLive(g)) add('gen', g.mode === 'repair' ? '2' : '1', 'info', g.staged ? `Kiro 正在分步起草（已完成 ${listOf(g.stages).filter((s) => s.status === 'done').length} 步）…` : `Kiro 正在${GEN_MODE[g.mode] || '生成'}…`, 'eval')
  else if (g && g.status === 'ready' && g.stale) add('gen', g.mode === 'repair' ? '2' : '1', 'bad', `${g.mode === 'repair' ? 'Kiro 修复结果' : 'Kiro 草稿'}已作废（生成后项目改过）：重新${g.mode === 'repair' ? '修复' : '起草'}，或继续用当前内容`, 'eval')
  else if (g && g.status === 'ready') {
    const pre = precheckErrors(g)
    add('gen', g.mode === 'repair' ? '2' : '1', pre ? 'bad' : 'warn', `${g.mode === 'repair' ? 'Kiro 修复结果' : 'Kiro 草稿'}待应用${pre ? `：预检有 ${pre} 个错误` : '（应用后再审核）'}`, 'eval')
  }
  else if (g && g.status === 'needs-input') add('gen', '1', 'warn', 'Kiro 需要补充信息')
  if (wsMaterialsBlock(ws, false)) add('approval', '1', 'bad', '先记录资料批准')
  // While a draft runs or waits for review, the item above says so: no second, contradicting "no content" item.
  if (ws.sc && !goldenOf(ws.sc).length && !draftRunning(ws) && !pendingDraft(ws) && !(g && g.status === 'needs-input')) add('content', '1', 'bad', '还没有内容：先生成草稿')
  const vv = ws.validationView
  const v = vv && vv.validation
  if (!v) { // a first draft that waits on an empty project is validated when it is applied
    if (!(holdingResult(ws) && !goldenOf(ws.sc).length)) add('validate', '2', 'warn', '还没校验', 'eval')
  } else if (!vv.fresh) add('validate', '2', 'warn', '内容改过，需重新校验', 'eval')
  else if (contentErrorCount(v)) add('validate', '2', 'bad', `校验有 ${contentErrorCount(v)} 个错误要改`, 'eval')
  else if (!v.ok && otherGateCount(v)) add('validate', '2', 'bad', `${otherGateCount(v)} 项来源标注要 SA 处理`, 'eval')
  const items = ws.items
  const holding = holdingResult(ws) // a review now changes the project: the waiting result could not be applied
  if (items && !holding) {
    const list = listOf(items.items)
    const drafts = list.filter((i) => i.provenance === 'ai_draft' || i.provenance === 'pending').length
    if (drafts) add('review', '2', 'warn', `${drafts} 项待审核`, 'review')
    if (items.packKind === 'customer') {
      const need = list.filter(needsCustomer).length
      if (need) add('confirm', '2', 'warn', `${need} 项待客户确认`, 'review')
    }
    const missing = listOf(items.anchors && items.anchors.missing)
    if (items.anchors && items.anchors.required && missing.length && goldenOf(ws.sc).length) add('anchors', '2', 'warn', `缺客户锚点：${missing.map((c) => CATEGORY_LABEL[c] || c).join('、')}`, 'review')
  }
  // GET release answers 409 when the sources changed after the last build (server._current_build).
  const staleBuild = !!(p.lastBuild && ws.res.release && ws.res.release.error)
  if (!p.lastBuild) add('build', '3', 'warn', '还没构建 Release')
  else if (staleBuild) add('build', '3', 'warn', '内容改过，需重新构建 Release')
  if (!p.target) add('target', '3', 'warn', 'Workshop 环境未配置')
  if (jobActive(ws.oneclick)) add('deliver', '3', 'info', '一键覆盖进行中…')
  else if (p.status !== 'synced') {
    add('deliver', '3', p.status === 'rollback-incomplete' ? 'bad' : 'warn',
      p.status === 'synced-pending-commit' ? '已应用，待确认保留' : p.status === 'rollback-incomplete' ? '回滚不完整' : '还没一键覆盖')
  }
  const run = ws.run
  if (run) {
    const order = listOf(run.stepOrder)
    const passed = order.filter((id) => run.steps[id].status === 'passed').length
    const failed = order.find((id) => run.steps[id].status === 'failed')
    if (failed) add('run', '4', 'bad', `Guided Run「${STEP_LABEL[failed] || failed}」失败`)
    else if (passed < order.length) add('run', '4', ws.runningId ? 'info' : 'warn', `Guided Run 通过 ${passed}/${order.length} 步`)
  } else if (!staleBuild) { // listed before the build too: building a release then moves the count
    // GET run answers 409 when the run record is bound to an earlier release: a new record is needed.
    const runError = p.lastBuild && ws.res.run && ws.res.run.error
    add('run', '4', runError ? 'bad' : 'warn', runError ? (runError.status === 409 ? 'Guided Run 绑定旧版本，需新建运行记录' : 'Guided Run 读不到') : 'Guided Run 未开始')
  }
  const dr = ws.direct && ws.direct.rehearsal
  if (jobActive(ws.direct && ws.direct.job)) add('direct', '4', 'info', '快速彩排进行中…')
  else if (dr && dr.verdict !== 'ready') add('direct', '4', 'bad', `快速彩排：${pick(DIRECT_VERDICT, dr.verdict)[1]}`)
  const lr = p.lastRehearsal
  if (!lr) add('rehearsal', '4', '', '还没记录彩排结论')
  else if (p.lastBuild && lr.releaseVersion !== p.lastBuild.version) add('rehearsal', '4', 'warn', '彩排结论针对旧版本')
  else if (!lr.readyForClass) add('rehearsal', '4', 'bad', `彩排：${pick(VERDICT, lr.verdict)[1]}`)
  return out
}

// ---------------------------------------------------------------------------
// Styles (scoped; host theme variables first, the v2 prototype palette as fallback)
// ---------------------------------------------------------------------------

const CSS = `
.wc-root{position:relative;container:wc-root/inline-size;--wc-warn-tx:light-dark(color-mix(in srgb,var(--wc-warn) 49%,var(--wc-strong)),var(--wc-warn));--wc-ok-tx:light-dark(color-mix(in srgb,var(--wc-ok) 63%,var(--wc-strong)),var(--wc-ok));--wc-acc-tx:light-dark(color-mix(in srgb,var(--wc-acc) 59%,var(--wc-strong)),var(--wc-acc));--wc-bad-tx:color-mix(in srgb,var(--wc-bad) 75%,var(--wc-strong));--wc-info-tx:light-dark(color-mix(in srgb,var(--wc-info) 59%,var(--wc-strong)),color-mix(in srgb,var(--wc-info) 75%,var(--wc-strong)));--wc-bg:var(--bg,#f4f6f4);--wc-panel:var(--card,#fff);--wc-text:var(--text,#1c2521);--wc-strong:var(--text-strong,#0e1813);--wc-muted:color-mix(in srgb,var(--muted,#5d6a63) 85%,var(--wc-strong));--wc-border:var(--border,#dce3de);--wc-border2:var(--border-strong,#c3cfc8);--wc-acc:var(--accent,#1f6b57);--wc-acc-h:var(--accent-hover,#175a48);--wc-acc-sub:var(--accent-subtle,#e6f1eb);--wc-acc-fg:var(--accent-fg,#fff);--wc-ok:var(--ok,#1e7b4f);--wc-ok-sub:var(--ok-subtle,#e4f4ea);--wc-warn:var(--warn,#9a5a06);--wc-warn-sub:var(--warn-subtle,#fcf1dc);--wc-bad:var(--danger,#b42318);--wc-bad-sub:var(--danger-subtle,#fdebea);--wc-info:var(--info,#1f5f99);--wc-info-sub:color-mix(in srgb,var(--wc-info) 13%,transparent);color:var(--wc-text);font:13px/1.55 var(--font-body,-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif);padding:12px 16px 32px}
.wc-root *{box-sizing:border-box}
.wc-root button,.wc-root input,.wc-root select,.wc-root textarea{font:inherit;color:inherit}
.wc-app{max-width:1320px;margin:0 auto;background:var(--wc-panel);border:1px solid var(--wc-border);border-radius:14px;overflow:hidden}
.wc-top{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;padding:11px 18px;border-bottom:1px solid var(--wc-border)}
.wc-logo{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:7px;background:var(--wc-acc);color:var(--wc-acc-fg);font-weight:700}
.wc-bar{display:flex;flex-wrap:wrap;align-items:center;gap:6px;padding:8px 18px;background:var(--wc-warn-sub);color:var(--wc-text);border-bottom:1px solid var(--wc-border)}
.wc-bar.done{background:var(--wc-ok-sub)}
.wc-body{display:flex;min-height:640px}
.wc-nav{width:188px;flex-shrink:0;padding:14px 10px;border-right:1px solid var(--wc-border);background:var(--wc-bg)}
.wc-nv{display:block;width:100%;text-align:left;border:0;border-radius:9px;padding:9px 11px;margin-bottom:4px;background:transparent;cursor:pointer;color:var(--wc-text)}
.wc-nv:hover{background:var(--wc-acc-sub)}
.wc-nv.on{background:var(--wc-acc-sub);color:var(--wc-acc-tx);font-weight:600}
.wc-nv small{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;font-size:11px;font-weight:400;line-height:1.4;margin-top:1px;word-break:break-word}
.wc-nav-meta{padding:0 11px;font-size:11px;color:var(--wc-muted);line-height:1.6}
.wc-nav-meta div{white-space:nowrap}
.wc-main{flex:1;min-width:0;padding:18px 22px 22px}
.wc-eyebrow{font-size:10.5px;letter-spacing:.1em;color:var(--wc-acc-tx);font-weight:600}
.wc-h1{font-size:19px;font-weight:650;color:var(--wc-strong);margin:4px 0 2px}
.wc-mut{color:var(--wc-muted)}.wc-strong{color:var(--wc-strong)}.wc-b{font-weight:600}
.wc-xs{font-size:11px}.wc-sm{font-size:12px}
.wc-stages{border:1px solid var(--wc-border);border-radius:9px;padding:10px;display:flex;flex-direction:column;gap:8px}
.wc-stage-phase{display:flex;flex-direction:column;gap:5px}
.wc-stage{display:flex;gap:8px;align-items:flex-start}
.wc-stage>.wc-chip{flex:none;min-width:44px;justify-content:center}
.wc-stage-body{min-width:0;flex:1}
.wc-card{background:var(--wc-panel);border:1px solid var(--wc-border);border-radius:11px;padding:14px;min-width:0}
.wc-card-h{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px}
.wc-g2{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:12px;align-items:start}
.wc-g2e{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:start}
.wc-g3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.wc-col{display:flex;flex-direction:column;gap:12px;min-width:0}
.wc-rf{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px}
.wc-fx{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.wc-top-al{align-items:flex-start}
.wc-mt{margin-top:12px}.wc-mt1{margin-top:6px}.wc-mt2{margin-top:18px}
.wc-ln{border-top:1px solid var(--wc-border)}
.wc-it{padding:7px 0}
.wc-chip{display:inline-flex;align-items:center;gap:4px;border-radius:999px;padding:1px 8px;font-size:11px;line-height:18px;border:1px solid var(--wc-border);color:var(--wc-muted);background:var(--wc-bg);white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis}
button.wc-chip{cursor:pointer}
.wc-ok{background:var(--wc-ok-sub);color:var(--wc-ok-tx);border-color:transparent}
.wc-warn{background:var(--wc-warn-sub);color:var(--wc-warn-tx);border-color:transparent}
.wc-bad{background:var(--wc-bad-sub);color:var(--wc-bad-tx);border-color:transparent}
.wc-acc{background:var(--wc-acc-sub);color:var(--wc-acc-tx);border-color:transparent}
.wc-info{background:var(--wc-info-sub);color:var(--wc-info-tx);border-color:transparent}
.wc-tx-ok{color:var(--wc-ok-tx)}.wc-tx-warn{color:var(--wc-warn-tx)}.wc-tx-bad{color:var(--wc-bad-tx)}.wc-tx-info{color:var(--wc-info-tx)}.wc-tx-acc{color:var(--wc-acc-tx)}
.wc-btn{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--wc-border2);background:var(--wc-panel);border-radius:8px;padding:5px 12px;font-size:12px;cursor:pointer;color:var(--wc-text);white-space:nowrap}
.wc-btn:hover{background:var(--wc-bg)}
.wc-btn.pri{background:var(--wc-acc);border-color:var(--wc-acc);color:var(--wc-acc-fg);font-weight:600}
.wc-btn.pri:hover{background:var(--wc-acc-h)}
.wc-btn.danger{border-color:var(--wc-bad);color:var(--wc-bad)}
.wc-btn.ghost{border-color:transparent;background:transparent;padding:3px 8px}
.wc-btn.sm{padding:2px 8px;font-size:11.5px}
.wc-btn[disabled]{opacity:.45;cursor:not-allowed}
.wc-tab{border:1px solid var(--wc-border);border-radius:8px;padding:4px 10px;font-size:12px;background:var(--wc-panel);cursor:pointer;color:var(--wc-text)}
.wc-tab.on{border-color:var(--wc-acc);color:var(--wc-acc-tx);background:var(--wc-acc-sub)}
.wc-tab .wc-cnt{margin-left:4px;color:var(--wc-muted);font-size:11px}
.wc-tab.on .wc-cnt{color:var(--wc-text)}
.wc-seg{display:inline-flex;border:1px solid var(--wc-border2);border-radius:8px;overflow:hidden;flex-wrap:wrap}
.wc-col>.wc-seg,.wc-lbl>.wc-seg{align-self:flex-start;width:max-content;max-width:100%}
.wc-seg button{border:0;padding:3px 10px;font-size:11.5px;background:var(--wc-panel);color:var(--wc-muted);cursor:pointer}
.wc-seg button+button{border-left:1px solid var(--wc-border)}
.wc-seg button.on{background:var(--wc-acc-sub);color:var(--wc-acc-tx);font-weight:600}
.wc-seg button[disabled]{cursor:not-allowed;opacity:.6}
.wc-field{width:100%;background:var(--wc-bg);border:1px solid var(--wc-border);border-radius:8px;padding:6px 9px;font-size:12.5px;color:var(--wc-strong)}
.wc-root .wc-field{color:var(--wc-strong)}
.wc-field::placeholder{color:var(--wc-muted);opacity:1}
textarea.wc-field{resize:vertical;line-height:1.5}
.wc-field.mono{font-family:ui-monospace,Menlo,monospace;font-size:11.5px}
.wc-lbl{display:block}.wc-lbl>.wc-xs{display:block;margin-bottom:3px;color:var(--wc-muted)}
.wc-check{display:flex;align-items:flex-start;gap:6px;font-size:12px;cursor:pointer}
.wc-check input{margin-top:3px}
.wc-drop{border:1px dashed var(--wc-border2);border-radius:9px;padding:12px;text-align:center;background:var(--wc-bg)}
.wc-drop.over{border-color:var(--wc-acc);background:var(--wc-acc-sub)}
.wc-file{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.wc-table{width:100%;border-collapse:collapse;font-size:12px}
.wc-table th{font-weight:400;color:var(--wc-muted);text-align:left;padding:4px 8px 4px 0;white-space:nowrap}
.wc-table td{border-top:1px solid var(--wc-border);padding:6px 8px 6px 0;vertical-align:top}
.wc-table td.nw{white-space:nowrap}
.wc-map{display:grid;grid-template-columns:auto auto 1fr;gap:3px 8px;font-size:12px}
.wc-foot{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;margin-top:16px}
.wc-note{font-size:11px;color:var(--wc-muted);margin-top:6px}
.wc-note.wc-tx-ok{color:var(--wc-ok-tx)}.wc-note.wc-tx-warn{color:var(--wc-warn-tx)}.wc-note.wc-tx-bad{color:var(--wc-bad-tx);font-size:12px}.wc-note.wc-tx-info{color:var(--wc-info-tx)}
.wc-mono{font-family:ui-monospace,Menlo,monospace}
.wc-kpiw{container-type:inline-size}
.wc-kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:8px}
.wc-kpi[data-n="4"]{grid-template-columns:repeat(4,minmax(0,1fr))}
@container (max-width:460px){.wc-kpi[data-n="4"]{grid-template-columns:repeat(2,minmax(0,1fr))}}
.wc-tile{border:1px solid var(--wc-border);border-radius:9px;padding:7px 10px;background:var(--wc-bg);min-width:0}
.wc-num{font-size:20px;font-weight:700;line-height:1.25;color:var(--wc-strong);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wc-num.sm{font-size:12.5px;font-weight:600;line-height:1.45;white-space:normal;overflow-wrap:anywhere;padding:3px 0}
.wc-pre{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;line-height:1.5;background:var(--wc-bg);border:1px solid var(--wc-border);border-radius:8px;padding:10px;max-height:420px;overflow:auto;white-space:pre-wrap;word-break:break-word;margin:6px 0 0;color:var(--wc-text)}
.wc-pre.tall{max-height:640px}
.wc-err{font-size:12px;color:var(--wc-bad-tx);background:var(--wc-bad-sub);border-radius:8px;padding:6px 9px;margin-top:6px;white-space:pre-wrap;word-break:break-word}
.wc-ok-box{font-size:12px;color:var(--wc-ok-tx);background:var(--wc-ok-sub);border-radius:8px;padding:6px 9px;margin-top:6px}
.wc-empty{padding:18px 8px;text-align:center;color:var(--wc-muted);font-size:12px}
.wc-empty .wc-b{color:var(--wc-strong)}
.wc-det>summary{cursor:pointer;font-size:12px;color:var(--wc-strong);padding:3px 0}
.wc-det[open]>summary{margin-bottom:4px}
.wc-sbar{display:flex;height:8px;border-radius:99px;overflow:hidden;background:var(--wc-border)}
.wc-sbar span{display:block;height:100%}
.wc-steps{display:flex;flex-wrap:wrap;gap:6px}
.wc-list-row{display:flex;gap:8px;align-items:flex-start;padding:7px 0;border-top:1px solid var(--wc-border)}
.wc-list-row:first-child{border-top:0}
.wc-grow{flex:1;min-width:0}
.wc-link{border:0;background:transparent;color:var(--wc-acc);cursor:pointer;padding:0;font-size:11px}
.wc-steprow .wc-hover{visibility:hidden}.wc-steprow:hover .wc-hover{visibility:visible}
.wc-steprow-h{display:flex;align-items:flex-start}
.wc-steprow-h>.wc-grow{display:flex;align-items:baseline;gap:0 6px;flex-wrap:wrap}
.wc-steprow-h>.wc-fx{flex-shrink:0;flex-wrap:nowrap;margin-left:8px}
.wc-banner{border-radius:8px;padding:8px 10px;margin-top:8px;font-size:12px}
.wc-clamp{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.wc-break{word-break:break-word}
.wc-spin{display:inline-block;width:10px;height:10px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:wc-spin .8s linear infinite}
@keyframes wc-spin{to{transform:rotate(360deg)}}
@container wc-root (max-width:948px){.wc-g2,.wc-g2e,.wc-g3{grid-template-columns:minmax(0,1fr)}.wc-nav{width:140px}.wc-nv small{-webkit-line-clamp:3}}
`

// ---------------------------------------------------------------------------
// Primitive views
// ---------------------------------------------------------------------------

function Chip({ tone, children, title, onClick }) {
  const className = cx('wc-chip', tone && `wc-${tone}`)
  return onClick ? h('button', { type: 'button', className, title, onClick }, children) : h('span', { className, title }, children)
}

function ToneChip({ map, value }) {
  const [tone, label] = pick(map, value)
  return h(Chip, { tone }, label)
}

function Button({ kind, small, disabled, onClick, children, title, busy, icon }) {
  return h('button', {
    type: 'button', title, disabled: disabled || busy, onClick,
    className: cx('wc-btn', kind === 'primary' && 'pri', kind === 'danger' && 'danger', kind === 'ghost' && 'ghost', small && 'sm'),
  }, busy ? h('span', { className: 'wc-spin', 'aria-hidden': true }) : icon ? h(icon, { size: 13 }) : null, children)
}

function LinkButton({ href, children, disabled, primary }) {
  if (disabled) return h(Button, { disabled: true, icon: Download }, children)
  return h('a', { className: cx('wc-btn', primary && 'pri'), href, download: true, style: { textDecoration: 'none' } }, h(Download, { size: 13 }), children)
}

function Card({ title, extra, children, className, sub, id }) {
  return h('section', { className: cx('wc-card', className), id },
    title || extra ? h('div', { className: 'wc-card-h' }, h('span', { className: 'wc-b wc-strong' }, title), extra ? h('span', { className: 'wc-fx' }, extra) : null) : null,
    sub ? h('div', { className: 'wc-xs wc-mut', style: { marginTop: -4, marginBottom: 6 } }, sub) : null,
    children)
}

function Field({ label, children, hint }) {
  return h('label', { className: 'wc-lbl' }, h('span', { className: 'wc-xs' }, label), children, hint ? h('span', { className: 'wc-note' }, hint) : null)
}

function TextInput({ value, onChange, placeholder, disabled, mono, type }) {
  return h('input', { className: cx('wc-field', mono && 'mono'), type: type || 'text', value: value ?? '', placeholder, disabled, onChange: (e) => onChange(e.target.value), 'aria-label': placeholder })
}

function TextArea({ value, onChange, rows, placeholder, mono, disabled, label }) {
  return h('textarea', { className: cx('wc-field', mono && 'mono'), rows: rows || 3, value: value ?? '', placeholder, disabled, spellCheck: !mono, 'aria-label': label || placeholder, onChange: (e) => onChange(e.target.value) })
}

function Select({ value, onChange, options, disabled, label }) {
  return h('select', { className: 'wc-field', value, disabled, 'aria-label': label, onChange: (e) => onChange(e.target.value) },
    options.map(([v, text]) => h('option', { key: v, value: v }, text)))
}

function Seg({ value, options, onChange, disabled }) {
  return h('span', { className: 'wc-seg', role: 'group' },
    options.map(([v, text]) => h('button', { key: v, type: 'button', className: v === value ? 'on' : '', disabled, 'aria-pressed': v === value, onClick: () => onChange(v) }, text)))
}

function Check({ checked, onChange, children, disabled }) {
  return h('label', { className: 'wc-check' }, h('input', { type: 'checkbox', checked: !!checked, disabled, onChange: (e) => onChange(e.target.checked) }), h('span', null, children))
}

function Note({ children, tone }) {
  return h('div', { className: cx('wc-note', tone && `wc-tx-${tone}`) }, children)
}

function Pre({ children, tall }) {
  return h('pre', { className: cx('wc-pre', tall && 'tall') }, children)
}

function Empty({ title, children }) {
  return h('div', { className: 'wc-empty' }, title ? h('div', { className: 'wc-b' }, title) : null, children ? h('div', { className: 'wc-mt1' }, children) : null)
}

// Backend refusals the SA meets in English, as the SA reads them (the original stays the tooltip).
const BACKEND_ERRORS = [
  [/validation must pass before a build/, '校验没通过，不能构建：「02 → 教学与评估」列着还差什么（要修的错误、待审核项、客户确认或客户锚点）。'],
  [/build or rebuild the current scenario before using its release/, '先按当前内容构建 Release（03 交付）。'],
  [/changed after generation started/, '生成后项目又改过（例如记录了审核、切换了项目类型或改了文件），这份结果不能再应用：请重新起草（修复结果就重新修复）。'],
  [/AWS profile '([^']*)' is not usable from this app/, (m) => `找不到 AWS profile「${m[1]}」，或它的登录已过期：先在终端运行 aws sso login（或重新拿临时角色），再体检。`],
  [/the scenario does not load; fix it before previewing the guide/, 'scenario 现在载入不了，预览不了手册：先到「02 → 教学与评估」修好错误。'],
  [/project changed after apply; revert refused/, '应用后项目又改过（例如记录了审核或改了文件），这次应用不能再撤销；要换内容就重新起草。'],
  [/snapshot of this apply is no longer kept/, '这次应用的快照已经不在了，不能撤销；要换内容就重新起草。'],
  [/(?:background )?job (\S+) is (?:still )?running for this project; wait for it to finish(?:, then (\w+))?/, (m) => `这个项目有后台任务在运行：等它结束再${{ generate: '起草', apply: '应用', revert: '撤销' }[m[2]] || '操作'}。`],
  [/run preflight first; apply needs a fresh confirm token/, '先做一次只读体检拿到确认令牌（15 分钟内有效），再应用。'],
  [/run (sync )?preflight first/, '先做一次只读体检（确认 Workshop 实例和文档），再执行这一步。'],
]
function backendText(text) {
  const said = String(text || '')
  const zh = BACKEND_ERRORS.find(([re]) => re.test(said))
  return zh ? (typeof zh[1] === 'function' ? zh[1](said.match(zh[0])) : zh[1]) : null
}
function ErrorLine({ error }) {
  if (!error) return null
  const text = error.message || String(error)
  const zh = backendText(text)
  return h('div', { className: 'wc-err', role: 'alert', title: zh ? text : undefined }, zh || text)
}

// A row of Stat tiles: four tiles go 4 across, or 2 × 2 when the card is narrow (never 3 + 1).
function Kpi({ children, className }) {
  const tiles = React.Children.toArray(children)
  return h('div', { className: cx('wc-kpiw', className) }, h('div', { className: 'wc-kpi', 'data-n': tiles.length }, tiles))
}

function Stat({ label, value, tone, small }) {
  return h('div', { className: 'wc-tile' }, h('div', { className: cx('wc-num', small && 'sm', tone && `wc-tx-${tone}`) }, value), h('div', { className: 'wc-xs wc-mut' }, label))
}

function Details({ summary, children, open }) {
  return h('details', { className: 'wc-det', open }, h('summary', null, summary), children)
}

function Tabs({ value, onChange, options }) {
  return h('div', { className: 'wc-fx', role: 'tablist' }, options.map((o) => h('button', {
    key: o.value, type: 'button', role: 'tab', 'aria-selected': o.value === value, className: cx('wc-tab', o.value === value && 'on'), onClick: () => onChange(o.value),
  }, o.label, o.count !== undefined && o.count !== null ? h('span', { className: 'wc-cnt' }, o.count) : null)))
}

function Table({ head, rows, empty }) {
  if (!rows.length) return h(Empty, null, empty || '没有内容')
  return h('div', { style: { overflowX: 'auto' } }, h('table', { className: 'wc-table' },
    h('thead', null, h('tr', null, head.map((c, i) => h('th', { key: i }, c)))),
    h('tbody', null, rows.map((r, i) => h('tr', { key: r.key || i }, (r.cells || r).map((c, j) => h('td', { key: j }, c)))))))
}

function ChipList({ chips }) {
  return h('span', { className: 'wc-fx' }, chips.map(([tone, text], i) => h(Chip, { key: i, tone }, text)))
}

function StepHeader({ n, title, h1, sub }) {
  return h(Fragment, null,
    h('div', { className: 'wc-eyebrow' }, `${n} · ${title}`),
    h('div', { className: 'wc-h1' }, h1),
    sub ? h('div', { className: 'wc-mut' }, sub) : null)
}

function StepFoot({ back, next, note }) {
  const ws = useWs()
  return h('div', { className: 'wc-foot' },
    back ? h(Button, { onClick: () => ws.go(back[0], back[2]) }, back[1]) : h('span', { className: 'wc-mut wc-sm' }, note || ''),
    next ? h(Button, { kind: 'primary', onClick: () => ws.go(next[0], next[2]) }, next[1]) : null)
}

function ResourceError({ name, hint }) {
  const ws = useWs()
  const r = ws.res[name]
  if (!r || !r.error) return null
  return h('div', { className: 'wc-note', title: r.error.message }, hint ? `${hint}：` : '', backendText(r.error.message) || r.error.message)
}

// ---------------------------------------------------------------------------
// Project list (entry page)
// ---------------------------------------------------------------------------

function HealthLine({ health }) {
  if (!health) return h(Note, null, '正在连接后端…')
  if (health.status === 'unreachable') {
    return h('div', { className: 'wc-err' }, '后端连不上：网关没有把请求转给应用后端（没启动，或健康检查失败）。在 KiroCrew → Apps 里关掉再打开这个应用，或重启 KiroCrew，然后刷新页面。')
  }
  if (!health.engine) return h('div', { className: 'wc-err' }, `找不到引擎（${health.engineError || '没配置 checkout'}）：设置 WORKSHOP_CUSTOMIZER_HOME 或 data/config.json {"homeDir": "<checkout>"}，再重启应用后端。`)
  return h('div', { className: 'wc-fx wc-sm wc-mut' }, h(Chip, { tone: 'ok' }, '后端正常'), h(Chip, { tone: 'ok' }, '引擎已加载'), `${health.projects} 个项目`)
}

function ProjectListCard({ projects, error, onOpen }) {
  const rows = listOf(projects).map((p) => ({
    key: p.id,
    cells: [
      h('div', null, h('div', { className: 'wc-b wc-strong' }, p.displayName || p.id), h('div', { className: 'wc-xs wc-mut wc-mono' }, p.id), p.customer ? h('div', { className: 'wc-xs wc-mut' }, p.customer) : null),
      h(Chip, { tone: (PACK_KIND[p.packKind] || {}).tone }, (PACK_KIND[p.packKind] || {}).label || p.packKind),
      h(ToneChip, { map: PROJECT_STATUS, value: p.status }),
      p.lastBuild ? h('span', { className: 'wc-mono wc-xs' }, p.lastBuild.version) : h('span', { className: 'wc-mut' }, '未构建'),
      p.lastRehearsal ? h(Chip, { tone: p.lastRehearsal.readyForClass ? 'ok' : pick(VERDICT, p.lastRehearsal.verdict)[0] }, p.lastRehearsal.readyForClass ? '可以上课' : pick(VERDICT, p.lastRehearsal.verdict)[1]) : h('span', { className: 'wc-mut' }, '未彩排'),
      h(Button, { small: true, onClick: () => onOpen(p.id) }, '打开 →'),
    ],
  }))
  return h(Card, { title: '项目', extra: error ? null : h('span', { className: 'wc-xs wc-mut' }, `${listOf(projects).length} 个`) },
    error ? h(ErrorLine, { error: new Error(`项目列表读不到：${error.message}。点右上角“刷新”再试；不是“还没有项目”。`) })
      : projects === null ? h(Empty, null, '加载中…') : h(Table, { head: ['名称', '类型', '状态', 'Release', '彩排', ''], rows, empty: '还没有项目。右边从模板或空白新建一个。' }))
}

function NewProjectCard({ templates, templatesError, onCreated }) {
  const { api, notify } = useApi()
  const [form, setForm] = useState({ id: '', displayName: '', customer: '', template: 'blank', packKind: 'workshop' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const set = (k) => (v) => setForm({ ...form, [k]: v })
  const valid = /^[a-z][a-z0-9-]{2,40}$/.test(form.id)
  const create = async () => {
    setBusy(true); setError(null)
    try { const p = await api.createProject(form); notify.success(`已创建 ${p.id}`); onCreated(p.id) }
    catch (err) { setError(apiError(err)) }
    finally { setBusy(false) }
  }
  const options = (templates.length ? templates : ['blank']).map((t) => [t, TEMPLATE_LABEL[t] || t])
  return h(Card, { title: '新建项目', sub: 'SA 拥有项目；客户确认事实，SA 在客户给不了真实值的地方补合成数据。' },
    h('div', { className: 'wc-col' },
      h('div', { className: 'wc-g2e' },
        h(Field, { label: '项目 ID（小写字母、数字、连字符）' }, h(TextInput, { value: form.id, onChange: set('id'), placeholder: '例：acme-cold-chain', mono: true })),
        h(Field, { label: '项目名称（会印在学员手册标题上，建好后不能改）' }, h(TextInput, { value: form.displayName, onChange: set('displayName'), placeholder: '例：ACME 冷链告警助手' }))),
      h('div', { className: 'wc-g2e' },
        h(Field, { label: '客户（不要写任何密钥）' }, h(TextInput, { value: form.customer, onChange: set('customer'), placeholder: '例：ACME' })),
        h(Field, { label: '从哪个模板开始', hint: templatesError ? `模板列表读不到（${templatesError.message}），现在只能从空白新建；刷新后再试。` : null },
          h(Select, { value: form.template, onChange: set('template'), options, label: '模板' }))),
      h(Note, null, form.template === 'blank' ? '空白：项目先是空的，Kiro 按你写的场景描述起草全部内容。'
        : `「${TEMPLATE_LABEL[form.template] || form.template}」：项目先装着这套现成内容当起点；之后用 Kiro 起草并应用会整体替换它。要按自己的场景从零写，选空白。`),
      h('div', null,
        h('span', { className: 'wc-xs wc-mut' }, '项目类型'),
        h('div', { className: 'wc-mt1' }, h(Seg, { value: form.packKind, onChange: set('packKind'), options: PACK_KINDS.map((k) => [k, PACK_KIND[k].label]) })),
        h(Note, null, PACK_KIND[form.packKind].help)),
      h('div', { className: 'wc-rf' },
        h('span', { className: 'wc-xs wc-mut' }, form.id && !valid ? 'ID 要以字母开头，3–41 个字符' : ''),
        h(Button, { kind: 'primary', icon: Plus, disabled: !valid, busy, onClick: create }, '创建')),
      h(ErrorLine, { error })))
}

function ProjectsPage({ onOpen }) {
  const { api } = useApi()
  const [health, setHealth] = useState(null)
  const [projects, setProjects] = useState(null)
  const [templates, setTemplates] = useState([])
  const [errors, setErrors] = useState({})
  const load = useCallback(() => {
    setErrors({})
    api.health().then(setHealth).catch(() => setHealth({ status: 'unreachable' }))
    // A failed read is shown as an error, never as "no projects" or "blank only".
    api.listProjects().then((r) => setProjects(listOf(r.projects))).catch((err) => { setProjects(null); setErrors((e) => ({ ...e, projects: apiError(err) })) })
    api.templates().then((r) => setTemplates(listOf(r.templates))).catch((err) => { setTemplates([]); setErrors((e) => ({ ...e, templates: apiError(err) })) })
  }, [api])
  useEffect(() => { load() }, [load])
  return h('div', { className: 'wc-app' },
    h('div', { className: 'wc-top' },
      h('div', { className: 'wc-fx' }, h('span', { className: 'wc-logo' }, 'W'), h('span', { className: 'wc-b wc-strong' }, 'Workshop Customizer'), h('span', { className: 'wc-mut' }, '把通用 HR Workshop 换成客户自己的场景')),
      h(Button, { kind: 'ghost', icon: RefreshCw, onClick: load }, '刷新')),
    h('div', { className: 'wc-main' },
      h('div', { className: 'wc-eyebrow' }, '项目'),
      h('div', { className: 'wc-h1' }, '选一个项目，或新建一个'),
      h('div', { className: 'wc-mut' }, '四步：场景与课程 → 内容与评估 → 交付 → 彩排与上课。'),
      h('div', { className: 'wc-mt' }, h(HealthLine, { health })),
      h('div', { className: 'wc-g2 wc-mt' }, h(ProjectListCard, { projects, error: errors.projects, onOpen }), h(NewProjectCard, { templates, templatesError: errors.templates, onCreated: onOpen }))))
}

// ---------------------------------------------------------------------------
// Workspace shell: top bar, readiness bar, step rail
// ---------------------------------------------------------------------------

function TopBar({ onBack }) {
  const ws = useWs()
  const p = ws.project
  const kind = PACK_KIND[p.packKind] || {}
  return h('div', { className: 'wc-top' },
    h('div', { className: 'wc-fx' },
      h(Button, { kind: 'ghost', icon: ArrowLeft, onClick: onBack, title: '返回项目列表' }, '项目'),
      h('span', { className: 'wc-logo' }, 'W'), h('span', { className: 'wc-b wc-strong' }, 'Workshop Customizer'), h('span', { className: 'wc-mut' }, '/'),
      h('span', { className: 'wc-b' }, p.displayName || p.id),
      h(Chip, { tone: kind.tone }, kind.label || p.packKind),
      h(Chip, null, p.lastBuild ? `Release ${p.lastBuild.version}` : '未构建'),
      h(ToneChip, { map: PROJECT_STATUS, value: p.status })),
    h('div', { className: 'wc-fx' },
      h(Button, { kind: 'ghost', icon: RefreshCw, onClick: () => ws.refresh() }, '刷新')))
}

function ReadinessBar() {
  const ws = useWs()
  const items = ws.ready
  if (!items.length) {
    const lr = ws.project.lastRehearsal
    return h('div', { className: 'wc-bar done' }, h('span', { className: 'wc-b' }, '可以上课：'), h(Chip, { tone: 'ok' }, `彩排通过 · Release ${lr ? lr.releaseVersion : ''}`))
  }
  return h('div', { className: 'wc-bar' },
    h('span', { className: 'wc-b' }, `离可以上课还差 ${items.length} 件事：`),
    items.map((it) => h(Chip, { key: it.key, tone: it.tone, onClick: () => ws.go(it.step, it.tab) }, `${it.text} →`)))
}

function StepRail({ step }) {
  const ws = useWs()
  const status = (id) => {
    const mine = ws.ready.filter((r) => r.step === id)
    if (!mine.length && id === '1' && templateOrigin(ws)) return h('small', { className: 'wc-tx-info' }, '用的是模板内容（还没按客户起草）')
    if (!mine.length && id === '1' && ((ws.project || {}).editLog || {}).kiroApplies > 0) return h('small', { className: 'wc-tx-info' }, '已应用 Kiro 草稿')
    if (!mine.length && id === '2' && holdingResult(ws) && (!goldenOf(ws.sc).length || listOf(ws.items && ws.items.items).some((i) => i.provenance === 'ai_draft' || i.provenance === 'pending'))) {
      return h('small', { className: 'wc-tx-warn' }, `等 Kiro ${(ws.generation || {}).mode === 'repair' ? '修复结果' : '草稿'}处理完再审核`)
    }
    if (!mine.length) return h('small', { className: 'wc-tx-ok' }, '已就绪')
    const first = mine.find((r) => r.tone === 'bad') || mine[0]
    return h('small', { className: first.tone ? `wc-tx-${first.tone}` : 'wc-mut', title: mine.map((r) => r.text).join(' · ') }, first.text + (mine.length > 1 ? `（另 ${mine.length - 1} 件事）` : ''))
  }
  return h('nav', { className: 'wc-nav' },
    STEPS.map((s) => h('button', { key: s.id, type: 'button', className: cx('wc-nv', step === s.id && 'on'), onClick: () => ws.go(s.id) }, `${s.n} ${s.label}`, status(s.id))),
    h('div', { className: 'wc-ln wc-mt2', style: { paddingTop: 10 } },
      h('button', { type: 'button', className: cx('wc-nv', step === 'adv' && 'on'), onClick: () => ws.go('adv') },
        h('span', { className: 'wc-fx' }, h(Settings, { size: 13 }), '高级'), h('small', { className: 'wc-mut' }, 'YAML · 文件 · 同步'))),
    h('div', { className: 'wc-nav-meta wc-mt1' }, h('div', null, '常用的三个按钮'), h('div', null, '01 生成草稿'), h('div', null, '03 一键覆盖'), h('div', null, '04 一键彩排')))
}

// ---------------------------------------------------------------------------
// 01 场景与课程
// ---------------------------------------------------------------------------

function ProjectFacts() {
  const ws = useWs()
  const p = ws.project
  const row = (label, value) => h('div', null, h('div', { className: 'wc-xs wc-mut' }, label), h('div', { className: 'wc-break' }, value || '—'))
  return h('div', { className: 'wc-g2e' },
    row('项目名称', p.displayName), row('项目 ID', h('span', { className: 'wc-mono' }, p.id)),
    row('客户', p.customer), row('模板', TEMPLATE_LABEL[p.createdFromTemplate || p.template] || p.createdFromTemplate || p.template))
}

function PackKindControl() {
  const ws = useWs()
  const current = ws.project.packKind
  const [target, setTarget] = useState(null)
  const [reason, setReason] = useState('')
  const [ack, setAck] = useState(false)
  const shown = target || current
  const history = listOf(ws.project.packKindHistory).slice(-3).reverse()
  const submit = () => ws.switchPackKind(target, reason.trim()).then((r) => { if (r) { setTarget(null); setReason(''); setAck(false) } })
  const locked = holdingResult(ws) // a switch changes the project: the waiting result could not be applied
  return h('div', null,
    h('span', { className: 'wc-xs wc-mut' }, '项目类型'),
    h('div', { className: 'wc-mt1' }, h(Seg, { value: shown, disabled: locked, onChange: (v) => { setTarget(v === current ? null : v); setAck(false) }, options: PACK_KINDS.map((k) => [k, PACK_KIND[k].label]) })),
    locked ? h(Note, { tone: 'warn' }, `${(ws.generation || {}).mode === 'repair' ? 'Kiro 修复结果' : 'Kiro 草稿'}${isLive(ws.generation) ? '生成中：等它完成并应用' : '待应用：先应用它'}，再切换类型。`) : h(Note, null, (PACK_KIND[shown] || {}).help),
    target ? h('div', { className: 'wc-col wc-mt1', style: { gap: 6 } },
      h(Field, { label: `切换到「${PACK_KIND[target].label}」的原因（10–500 字，记入历史）` }, h(TextArea, { value: reason, onChange: setReason, rows: 2 })),
      h(Check, { checked: ack, onChange: setAck }, '我知道：切换类型会改变 Release 要过的来源门禁，已有构建会失效。'),
      h('div', { className: 'wc-fx' },
        h(Button, { kind: 'primary', disabled: !ack || reason.trim().length < 10 || reason.trim().length > 500, busy: ws.busy === 'packKind', onClick: submit }, '确认切换'),
        h(Button, { onClick: () => setTarget(null) }, '取消')),
      h(ErrorLine, { error: ws.errors.packKind })) : null,
    history.length ? h('div', { className: 'wc-xs wc-mut wc-mt1' }, '切换记录：', history.map((e, i) => h('div', { key: i }, `${fmtTime(e.at)} · ${(PACK_KIND[e.from] || {}).label || e.from || '—'} → ${(PACK_KIND[e.to] || {}).label || e.to}${e.reason ? ` · ${e.reason}` : ''}`))) : null)
}

function BriefEditor() {
  const ws = useWs()
  const chars = ws.brief.trim().length
  return h(Field, { label: '客户场景描述（谁用 Agent、要完成什么、不能做什么）', hint: `${chars} 字 · 点「${draftLabel(ws)}」${canRewrite(ws) ? '或「在当前内容上改写」' : ''}时保存并发给 Kiro；上次提交的描述会自动带出。` },
    h(TextArea, {
      value: ws.brief, onChange: ws.setBrief, rows: 6, label: '客户场景描述',
      placeholder: '例：仓库的操作员和值班主管用助手查设备告警、判断异常等级、建维修工单；紧急告警必须建最高优先级工单。不能关闭安全联锁，数据缺失时不能编造。',
    }))
}

// noise:true is an on-topic document that ends with an off-topic FAQ (the distractor), not a distractor document.
function noiseNote(docs) {
  const n = docs.filter((d) => d.noise).length
  return !n ? '' : n === docs.length ? '（每份文末都带干扰问答）' : `（${n} 份文末带干扰问答）`
}

function OriginTag({ draft, origin }) {
  // Whose content a card shows: the Kiro draft waiting for review, or the template the project started from.
  if (draft) return h(Chip, { tone: 'warn' }, 'Kiro 草稿预览 · 还没应用')
  if (origin) return h(Chip, { title: '起草并应用后整体替换' }, `当前内容来自模板「${origin}」`)
  return null
}

function MappingCard() {
  const ws = useWs()
  const draft = pendingDraft(ws)
  const sc = draft ? draft.scenarioPreview : ws.sc
  if (!sc) return null
  const docs = listOf(sc.knowledge && sc.knowledge.documents)
  const tools = listOf(sc.tools)
  const golden = goldenOf(sc)
  const practice = golden.filter((c) => c.set === 'practice').length
  const rows = [
    ['用户', sc.agent && !isPlaceholder(sc.agent.audience) ? sc.agent.audience : '还没有'],
    ['知识文档', docs.length ? `${docs.length} 份${noiseNote(docs)}` : '还没有'],
    ['工具', tools.length ? tools.map((t) => t.name).join(' · ') : '还没有'],
    ['题目', golden.length ? `${golden.length} 道（练习 ${practice} · 留出 ${golden.length - practice}）` : '还没有'],
    ['教学现象', `${phenomenaOf(sc).length} 个`],
  ]
  return h('div', { className: 'wc-ln', style: { paddingTop: 10 } },
    h('div', { className: 'wc-rf' }, h('div', { className: 'wc-b wc-sm' }, '这一场的内容（对照官方 HR Workshop 换成了什么）'), h(OriginTag, { draft, origin: draft ? null : templateOrigin(ws) })),
    draftRunning(ws) ? h(Note, null, goldenOf(sc).length ? 'Kiro 正在起草；下面还是项目当前的内容，草稿完成后这里先预览它，应用后才换掉。' : 'Kiro 正在起草；草稿完成后这里先预览它的内容，应用后成为项目内容。') : null,
    h('div', { className: 'wc-map wc-mt1' }, rows.map(([k, v]) => h(Fragment, { key: k }, h('span', { className: 'wc-mut' }, k), h('span', null, '→'), h('span', { className: 'wc-break' }, v || '—')))))
}

function ScenarioCard() {
  return h(Card, { title: '客户场景' }, h('div', { className: 'wc-col' }, h(ProjectFacts), h(PackKindControl), h(BriefEditor), h(MappingCard)))
}

function readFileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '')
    reader.onerror = () => reject(reader.error || new Error('读取文件失败'))
    reader.readAsDataURL(file)
  })
}

function MaterialPicker({ limits, file, onFile, inputRef }) {
  // The native file control is hidden (it stays light and English in a dark host); a styled button
  // opens it, and the dashed box is a real drop target.
  const [over, setOver] = useState(false)
  const accept = acceptList(limits && limits.types)
  const drop = (e) => {
    e.preventDefault(); setOver(false)
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]
    if (f) onFile(f)
  }
  return h('div', {
    className: cx('wc-drop', over && 'over'), onDragOver: (e) => { e.preventDefault(); setOver(true) }, onDragLeave: () => setOver(false), onDrop: drop,
  },
  h('input', { ref: inputRef, className: 'wc-file', type: 'file', accept, tabIndex: -1, 'aria-label': '选择客户资料', onChange: (e) => onFile(e.target.files && e.target.files[0] ? e.target.files[0] : null) }),
  h('div', { className: 'wc-fx', style: { justifyContent: 'center' } },
    h(Button, { small: true, onClick: () => inputRef.current && inputRef.current.click() }, '选择文件'),
    h('span', { className: 'wc-sm wc-mut wc-break' }, file ? `已选：${file.name}（${fmtBytes(file.size)}）` : '或把文件拖到这里')),
  h('div', { className: 'wc-xs wc-mut wc-mt1' }, limits ? `支持 ${acceptList(limits.types).split(',').join(' / ')}；单个 ≤ ${fmtBytes(limits.maxBytes)}，最多 ${limits.maxFiles} 份。Kiro 读提取出的文字，每条生成内容标出处。` : ''))
}

function MaterialUpload({ limits }) {
  const ws = useWs()
  const [file, setFile] = useState(null)
  const [author, setAuthor] = useState('customer')
  const [use, setUse] = useState('source')
  const [notes, setNotes] = useState('')
  const [ack, setAck] = useState(false)
  const [localError, setLocalError] = useState(null)
  const input = useRef(null)
  const choose = (f) => {
    setLocalError(null)
    const allowed = acceptList(limits && limits.types).split(',').filter(Boolean)
    if (f && allowed.length && !allowed.includes(extensionOf(f.name))) { setFile(null); setLocalError(new Error(`不支持 ${extensionOf(f.name) || '没有扩展名的文件'}；支持 ${allowed.join(' / ')}`)); return }
    setFile(f)
  }
  const upload = async () => {
    setLocalError(null)
    if (limits && file.size > limits.maxBytes) { setLocalError(new Error(`文件超过 ${fmtBytes(limits.maxBytes)}`)); return }
    let contentBase64
    try { contentBase64 = await readFileBase64(file) } catch (err) { setLocalError(err); return }
    const r = await ws.uploadMaterial({ name: file.name, contentBase64, author, generationUse: use, notes })
    if (r) { setFile(null); setNotes(''); setAck(false); if (input.current) input.current.value = '' }
  }
  return h('div', { className: 'wc-col', style: { gap: 6 } },
    h(MaterialPicker, { limits, file, onFile: choose, inputRef: input }),
    file ? h(Fragment, null,
      h('div', { className: 'wc-g2e' },
        h(Field, { label: '谁提供的' }, h(Seg, { value: author, onChange: setAuthor, options: [['customer', '客户'], ['sa', 'SA']] })),
        h(Field, { label: '怎么用' }, h(Select, { value: use, onChange: setUse, options: Object.entries(MATERIAL_USE), label: '用途' }))),
      h(Field, { label: '备注（可选）' }, h(TextInput, { value: notes, onChange: setNotes })),
      h(Check, { checked: ack, onChange: setAck }, '我可以存放这份资料，并在生成时把它的文字发给 Kiro 使用的模型。'),
      h('div', null, h(Button, { kind: 'primary', disabled: !ack, busy: ws.busy === 'upload', onClick: upload }, `上传 ${file.name}`))) : null,
    h(ErrorLine, { error: localError || ws.errors.upload }))
}

function MaterialRow({ m, budget }) {
  const ws = useWs()
  const [text, setText] = useState(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const ex = m.extraction || {}
  const sent = budget && budget.perMaterial ? budget.perMaterial[m.id] : null
  const busy = ws.busy === `material:${m.id}`
  const toggleText = async () => {
    if (text !== null) { setText(null); return }
    try { const r = await ws.materialText(m.id); setText(r.text + (r.chars > r.text.length ? `\n…（共 ${r.chars} 字，只显示前 ${r.text.length} 字）` : '')) }
    catch (err) { setText(`读取失败：${apiError(err).message}`) }
  }
  return h('div', { className: 'wc-list-row' },
    h('div', { className: 'wc-grow' },
      h('div', { className: 'wc-fx' }, h('span', { className: 'wc-b wc-break' }, m.name), h(Chip, { tone: pick(EXTRACTION, ex.status)[0] }, pick(EXTRACTION, ex.status)[1]),
        h(Chip, null, m.author === 'sa' ? 'SA 提供' : '客户提供'), h('span', { className: 'wc-xs wc-mut' }, `${fmtBytes(m.bytes)} · ${ex.chars ?? 0} 字`)),
      h('div', { className: 'wc-xs wc-mut wc-mt1' },
        sent ? `起草时发送 ${sent.sentChars} 字${sent.truncated ? '（已截断）' : ''}` : m.generationUse === 'exclude' ? '不发送' : '不在发送预算内',
        listOf(ex.warnings).length ? ` · ${listOf(ex.warnings).join('；')}` : ''),
      text !== null ? h(Pre, null, text) : null),
    h('div', { className: 'wc-fx', style: { flexShrink: 0 } },
      h(Select, { value: m.generationUse, disabled: busy, label: `${m.name} 用途`, onChange: (v) => ws.updateMaterial(m.id, { generationUse: v }), options: Object.entries(MATERIAL_USE) }),
      h(Button, { small: true, onClick: toggleText }, text === null ? '看文字' : '收起'),
      confirmDelete
        ? h(Button, { small: true, kind: 'danger', busy, onClick: () => ws.deleteMaterial(m.id) }, '确认删除')
        : h(Button, { small: true, kind: 'ghost', icon: Trash2, title: '删除', onClick: () => setConfirmDelete(true) })))
}

function MaterialApproval({ policy }) {
  const ws = useWs()
  const [ref, setRef] = useState(policy ? policy.approvalRef : '')
  const [cls, setCls] = useState(policy ? policy.dataClassification : 'internal')
  return h('div', { className: 'wc-ln', style: { paddingTop: 10 } },
    h('div', { className: 'wc-rf' }, h('span', { className: 'wc-b wc-sm' }, '发给 Kiro 的批准'),
      policy ? h(Chip, { tone: policy.dataClassification === 'confidential' ? 'bad' : 'ok' }, `${CLASSIFICATION[policy.dataClassification] || policy.dataClassification} · ${fmtTime(policy.recordedAt)}`) : h(Chip, { tone: 'warn' }, '未记录')),
    h('div', { className: 'wc-g2e wc-mt1' },
      h(Field, { label: '谁批准的、在哪里（邮件、会议纪要）' }, h(TextInput, { value: ref, onChange: setRef, placeholder: '例：客户法务 2026-09-20 邮件' })),
      h(Field, { label: '数据级别' }, h(Select, { value: cls, onChange: setCls, options: Object.entries(CLASSIFICATION), label: '数据级别' }))),
    h('div', { className: 'wc-rf wc-mt1' },
      h(Note, null, '有资料会发给 Kiro 时必须先记录；机密资料不会被发送。'),
      h(Button, { disabled: ref.trim().length < 3, busy: ws.busy === 'approval', onClick: () => ws.recordApproval({ approvalRef: ref.trim(), dataClassification: cls }) }, '记录批准')),
    h(ErrorLine, { error: ws.errors.approval }))
}

function MaterialsCard() {
  const ws = useWs()
  const m = ws.materials
  const list = listOf(m && m.materials)
  const budget = m && m.budget
  return h(Card, {
    title: '客户资料',
    extra: budget ? h('span', { className: 'wc-xs wc-mut' }, `起草发送 ${budget.sentChars} / ${budget.totalChars} 字 · 约 ${budget.estTokens} tokens`) : null,
  },
  h(ResourceError, { name: 'materials', hint: '资料列表读不到' }),
  h(MaterialUpload, { limits: m && m.limits }),
  list.length ? h('div', { className: 'wc-mt1' }, list.map((x) => h(MaterialRow, { key: x.id, m: x, budget }))) : h(Note, null, '还没有资料。没有资料时，Kiro 只按场景描述起草，并把缺的事实列为待确认问题。'),
  m ? h(MaterialApproval, { policy: ws.project.materialsPolicy || m.policy }) : null)
}

function GenerationStatus({ g }) {
  const live = isLive(g)
  const now = useNow(live)
  const started = g.createdAt ? Date.parse(g.createdAt) : 0
  // Kiro's own run time: to completedAt (routes._update_live), never to updatedAt, which apply,
  // revert and supersede bump. An older record without completedAt shows when it started instead.
  const ended = live ? now : g.completedAt ? Date.parse(g.completedAt) : null
  const time = started && ended ? { label: live ? 'Kiro 已用时' : 'Kiro 用时', value: fmtSeconds((ended - started) / 1000) }
    : { label: '开始于', value: fmtTime(g.createdAt), small: true }
  return h(Kpi, null,
    g.status === 'ready' && g.stale ? h(Stat, { label: GEN_MODE[g.mode] || g.mode, tone: 'bad', value: '已作废' })
      : h(Stat, { label: `${GEN_MODE[g.mode] || g.mode}${g.round > 1 ? ` · 第 ${g.round} 份` : ''}`, tone: pick(GEN_STATUS, g.status)[0], value: pick(GEN_STATUS, g.status)[1], small: pick(GEN_STATUS, g.status)[1].length > 4 }),
    h(Stat, time),
    g.staged ? h(Stat, { small: true, label: `已发给 Kiro · ${listOf(g.stages).reduce((n, s) => n + (s.attempts || 0), 0)} 次`, value: `约 ${fmtTokens(listOf(g.stages).reduce((n, s) => n + (s.estTokens || 0) * Math.max(1, s.attempts || 0), 0))} tokens` })
      : h(Stat, { small: true, label: '已发给 Kiro', value: g.task ? `约 ${fmtTokens(g.task.estTokens)} tokens` : '—' }),
    h(Stat, { label: '发送的资料', value: `${listOf(g.materialsUsed).length} 份` }))
}

function GenerationStages({ g }) {
  // A staged draft's stages in the backend's order and phases: what each writes, how long it took, why a reply was redone.
  const now = useNow(isLive(g))
  const stages = listOf(g.stages)
  if (!g.staged || !stages.length) return null
  if (stages.every((s) => s.name === 'structure' || s.status === 'skipped') && stages.some((s) => s.status === 'skipped')) {
    return h(Note, null, g.status === 'needs-input' ? 'Kiro 先要你回答几个问题，还没开始分步起草。' : 'Kiro 在第一步就给出了整套内容，没有再分步。')
  }
  const of = (base) => stages.filter((s) => stageBase(s.name) === base)
  const batchesKnown = of('knowledge').length > 0
  const total = stages.length + (batchesKnown ? 0 : 1)
  const done = stages.filter((s) => s.status === 'done').length
  const running = stages.filter((s) => s.status === 'running').map((s) => stageInfo(s)[0])
  return h('div', { className: 'wc-stages wc-mt1' },
    h('div', { className: 'wc-rf' },
      h('span', { className: 'wc-b wc-sm' }, batchesKnown ? `分步起草：完成 ${done} / ${total} 步` : `分步起草：已完成 ${done} 步（知识文档分几批，第 2 段写完才知道）`),
      running.length ? h('span', { className: 'wc-xs wc-mut' }, `正在写：${running.join('、')}`) : null),
    STAGE_PHASES.map(([n, title, bases]) => {
      // The knowledge batches keep their place (first) before they are known, so the order never jumps.
      const rows = bases.flatMap((b) => (b === 'knowledge' && !batchesKnown ? [null] : of(b)))
      if (!rows.length) return null
      return h('div', { key: n, className: 'wc-stage-phase' },
        h('div', { className: 'wc-xs wc-mut' }, `第 ${n} 段 · ${title}`),
        rows.map((s) => (s ? h(StageRow, { key: s.name, s, now, applied: g.status === 'applied', running: isLive(g), failed: g.status === 'failed' })
          : h(StageRow, { key: 'knowledge', s: { name: 'knowledge', status: 'pending' }, now, info: ['知识文档', '第 2 段完成后按教学设计分批写，每批最多 4 篇'] }))))
    }))
}

// What became of each earlier attempt of a stage.
const ATTEMPT_KIND = { output: ['回复没通过检查', '已让 Kiro 重写'], precheck: ['应用前预检发现问题', '已让 Kiro 重写这一步'], run: ['运行失败', '已重跑'] }

function StageRow({ s, now, info, applied, running, failed }) {
  const left = listOf(s.problems)
  const [tone, word] = s.status === 'done' && left.length ? ['warn', '仍有问题'] : pick(STAGE_STATUS, s.status)
  const [name, what] = info || stageInfo(s)
  const started = s.startedAt ? Date.parse(s.startedAt) : 0
  const ended = s.status === 'running' ? now : s.completedAt ? Date.parse(s.completedAt) : 0
  const history = listOf(s.history)
  const raw = [...history.map((x, i) => `第 ${i + 1} 次（${(ATTEMPT_KIND[x.kind] || ATTEMPT_KIND.run)[0]}）：${x.error}`), ...left.map((x) => `已收下，仍未满足：${x}`)]
  return h('div', { className: 'wc-stage' },
    h(Chip, { tone }, word),
    h('div', { className: 'wc-stage-body' },
      h('div', { className: 'wc-sm' }, h('span', { className: 'wc-b' }, name), what ? h('span', { className: 'wc-mut' }, ` · ${what}`) : null,
        started && ended ? h('span', { className: 'wc-xs wc-mut' }, ` · ${s.status === 'running' ? '已用 ' : '用时 '}${fmtSeconds((ended - started) / 1000)}`) : null),
      history.map((x, i) => h('div', { key: i, className: 'wc-xs wc-tx-warn' },
        `${x.kind === 'precheck' ? '' : `第 ${x.attempt || i + 1} 次`}${(ATTEMPT_KIND[x.kind] || ATTEMPT_KIND.run)[0]}（${stageReasons(attemptProblems(x)).join('；')}）${s.status === 'failed' && i === history.length - 1 ? '' : `，${(ATTEMPT_KIND[x.kind] || ATTEMPT_KIND.run)[1]}`}`)),
      history.length && s.status === 'running' ? h('div', { className: 'wc-xs wc-mut' }, `正在第 ${s.attempts} 次`) : null,
      s.kept === 'first' ? h('div', { className: 'wc-xs wc-tx-warn' }, '重写的那版更差（或没能完成），收下的是第 1 版。') : null,
      s.kept === 'before-precheck' ? h('div', { className: 'wc-xs wc-tx-warn' }, `预检后重写的那版没成功（或更差），收下的是之前的第 ${s.keptAttempt || 1} 次回复。`) : null,
      left.length ? h('div', { className: 'wc-xs wc-tx-warn' }, `${history.length ? `收下的第 ${s.keptAttempt || (s.kept === 'first' ? 1 : s.attempts)} 次回复` : '这一版'}仍有 ${left.length} 处没完全满足（${stageReasons(left).join('；')}）${failed ? '。' : `；会不会成为错误，看${applied ? '「02 → 教学与评估」的校验' : running ? '全部步骤完成后出现的「应用前预检」' : '下方「应用前预检」'}。`}`) : null,
      s.status === 'failed' ? h('div', { className: 'wc-xs wc-tx-bad' }, `这一步失败：${stageReason(s.error)}。整份草稿停在这里，可以重新起草。`) : null,
      raw.length ? h(Details, { summary: '看原始信息' }, h(Pre, null, raw.join('\n\n'))) : null))
}

function NeedsInputForm({ g }) {
  const ws = useWs()
  const questions = listOf(g.openQuestions)
  const [answers, setAnswers] = useState({})
  const filled = questions.map((q, i) => ({ question: q, answer: (answers[i] || '').trim() })).filter((a) => a.answer)
  return h('div', { className: 'wc-col wc-mt', style: { gap: 6 } },
    h('div', { className: 'wc-b wc-sm' }, 'Kiro 要先弄清这些，才能起草（不会编造业务事实）'),
    questions.map((q, i) => h(Field, { key: i, label: `${i + 1}. ${q}` }, h(TextInput, { value: answers[i] || '', onChange: (v) => setAnswers({ ...answers, [i]: v }) }))),
    h('div', null, h(Button, { kind: 'primary', icon: Wand2, disabled: !filled.length || !!wsMaterialsBlock(ws, false), busy: ws.busy === 'generate', onClick: () => ws.startDraft(filled) }, `带着 ${filled.length} 条回答重新起草`)))
}

// The ignored change (routes: outside the round's scopes) that covers a change Kiro describes: then it did not happen.
function ignoredBy(target, ignored) {
  return listOf(ignored).find((r) => r && r.target && target && (target === r.target
    || (!String(r.target).includes('/') && (String(target).startsWith(`${r.target}.`) || String(target).startsWith(`${r.target}[`)))))
}

function ChangesTable({ g }) {
  const changes = listOf(g.changes)
  if (!changes.length) return null
  const dropped = changes.filter((c) => ignoredBy(c.target, g.ignoredChanges)).length
  return h(Details, { summary: `Kiro 说明的改动（${changes.length}${dropped ? `，其中 ${dropped} 处超出范围、没有生效` : ''}）`, open: true },
    h(Table, { head: ['针对', '位置', '做了什么'], rows: changes.map((c) => [h('span', { className: 'wc-mono wc-xs' }, c.finding || '—'), h('span', { className: 'wc-mono wc-xs wc-break' }, c.target || '—'),
      ignoredBy(c.target, g.ignoredChanges) ? h('span', null, h(Chip, { tone: 'warn' }, '没有生效：超出这次的修复范围'), ' ', h('span', { className: 'wc-mut' }, c.action || '—')) : c.action || '—']) }))
}

function DiffSummary({ g }) {
  const diff = g.diff || {}
  const items = diff.items || {}
  const files = diff.files || {}
  const rows = [
    ['新增内容', listOf(items.added).map((i) => `${ITEM_KIND_LABEL[i.type] || i.type} ${i.id}`)],
    ['删除内容', listOf(items.removed).map((i) => `${ITEM_KIND_LABEL[i.type] || i.type} ${i.id}`)],
    ['修改内容', listOf(items.modified).map((i) => `${ITEM_KIND_LABEL[i.type] || i.type} ${i.id}（${listOf(i.fields).join(', ')}）`)],
    ['改动的段落', listOf(diff.sections)],
    ['新增文件', listOf(files.added)], ['修改文件', listOf(files.modified)], ['删除文件', listOf(files.deleted)],
  ]
  return h('div', { className: 'wc-mt' },
    h(Kpi, null,
      h(Stat, { label: '内容：新增 · 修改 · 删除', value: `${listOf(items.added).length} · ${listOf(items.modified).length} · ${listOf(items.removed).length}` }),
      h(Stat, { label: '文件：新增 · 修改 · 删除', value: `${listOf(files.added).length} · ${listOf(files.modified).length} · ${listOf(files.deleted).length}`, tone: listOf(files.deleted).length ? 'warn' : '' }),
      h(Stat, { label: '沿用已审核', value: g.provenance ? g.provenance.preserved : '—' }),
      h(Stat, { label: '审核被重置', value: g.provenance ? listOf(g.provenance.reset).filter((r) => !r.removed).length : '—', tone: g.provenance && listOf(g.provenance.reset).some((r) => !r.removed) ? 'warn' : '' })),
    h(Details, { summary: '逐项看改动' },
      rows.filter(([, list]) => list.length).map(([label, list]) => h('div', { key: label, className: 'wc-it wc-ln' }, h('div', { className: 'wc-xs wc-mut' }, `${label}（${list.length}）`), h('div', { className: 'wc-sm wc-mono wc-break' }, list.join(' · ')))),
      files.unchanged ? h(Note, null, `${files.unchanged} 个文件没变。`) : null))
}

// Generation warnings the app fixed by itself, in one collapsed line per kind; a staged draft's own leftovers are on its stages.
const WARNING_GROUPS = [
  [/^draft stage /, null],
  [/app-assigned namespace|app-derived names/, '自动把模型写的资源名换成了本项目的名字'],
  [/knowledge document .*replaced by fact|dropped knowledge document/, '自动把题目依据里的文档名换成了对应的业务规则'],
  [/remapped to retrieval|now requires the string property|fixtures dropped/, '自动修正了检索工具的定义'],
  [/dropped the one-character term/, '自动去掉了太短的缺失词或诱饵词（单个字会误伤别的词）'],
  [/\.roleId: unknown role/, '自动把不存在的提问角色换成了第一个角色（请核对这些题是谁在问）'],
  [/\.basis: none given/, '自动给没写依据的题补了第一条业务规则（请核对依据）'],
]

function GenerationWarnings({ g }) {
  const grouped = new Map()
  const warnings = []
  for (const w of listOf(g.warnings)) {
    const group = WARNING_GROUPS.find(([re]) => re.test(String(w)))
    if (!group) warnings.push(w)
    else if (group[1]) grouped.set(group[1], [...(grouped.get(group[1]) || []), w])
  }
  const resets = listOf(g.provenance && g.provenance.reset)
  const kept = resets.filter((r) => !r.removed) // a kept item back to a Kiro draft; a removed one leaves with its review
  const gone = resets.filter((r) => r.removed)
  const done = g.status === 'applied'
  const ignored = listOf(g.ignoredChanges)
  const orphans = listOf(g.deleteOrphans)
  return h(Fragment, null,
    warnings.length ? h('div', { className: 'wc-mt1' }, h('div', { className: 'wc-xs wc-mut' }, `要看一下的提醒（${warnings.length}）`), warnings.map((w, i) => h('div', { key: i, className: 'wc-sm wc-tx-warn' }, `! ${w}`))) : null,
    [...grouped.entries()].map(([label, list]) => h(Details, { key: label, summary: `${label}：${list.length} 处（已自动处理，不用改）` }, h(Pre, null, list.join('\n')))),
    resets.length ? h(Details, { summary: h('span', { className: kept.length ? 'wc-tx-warn' : '' }, [
      kept.length ? `${kept.length} 项审核${done ? '已' : '会被'}重置为 Kiro 草稿` : null,
      gone.length ? `${gone.length} 项已审核的内容${done ? '已随删除去掉' : '会随删除去掉'}` : null].filter(Boolean).join('；')) },
      resets.map((r, i) => h('div', { key: i, className: 'wc-sm wc-mono' }, `${ITEM_KIND_LABEL[r.type] || r.type} ${r.id}：${pick(PROVENANCE_LABEL, r.was)[1]}${r.removed ? `，${done ? '已' : '会被'}删除` : ' → Kiro 草稿'}`))) : null,
    orphans.length ? h(Details, { summary: h('span', { className: done ? '' : 'wc-tx-warn' }, done
      ? `应用时删除了 ${orphans.length} 个没被引用的文件${g.revertable === false ? '' : '（撤销可恢复）'}`
      : `应用时删除 ${orphans.length} 个没被引用的文件（先做快照，可撤销）`) }, h('div', { className: 'wc-sm wc-mono wc-break' }, orphans.join(' · '))) : null,
    ignored.length ? h(Details, { summary: `${ignored.length} 处改动超出范围，已忽略` }, ignored.map((r, i) => h('div', { key: i, className: 'wc-sm' }, h('span', { className: 'wc-mono' }, r.target), ` — ${r.reason}`))) : null,
    listOf(g.untracked).length ? h(Note, null, `项目里还有 ${g.untracked.length} 个未跟踪文件，不会被动：${g.untracked.join(' · ')}`) : null)
}

function TruthLedger({ g }) {
  // Kiro's own account of every business fact it wrote: provenance, origin and who must confirm it.
  const ledger = listOf(g.truthLedger).filter((e) => e && typeof e === 'object')
  if (!ledger.length) return null
  const drafts = ledger.filter((e) => e.provenance !== 'customer_confirmed' && e.provenance !== 'sa_synthetic').length
  return h(Details, { summary: `Kiro 的来源账本（${ledger.length} 条，${drafts} 条待确认）` },
    h(Table, {
      head: ['规则', '内容', '来源状态', '出处', '要问谁'],
      rows: ledger.slice(0, 100).map((e, i) => ({
        key: `${asText(e.id)}:${i}`,
        cells: [h('span', { className: 'wc-mono wc-xs' }, asText(e.id) || '—'),
          h('div', null, h('div', { className: 'wc-sm wc-break' }, asText(e.statement) || '—'), e.criticality ? h('span', { className: 'wc-xs wc-mut' }, e.criticality === 'blocking' ? '阻断' : '参考') : null),
          e.provenance ? h(ToneChip, { map: PROVENANCE_LABEL, value: asText(e.provenance) }) : '—',
          h('span', { className: 'wc-xs' }, e.origin && typeof e.origin === 'object' ? ORIGIN_LABEL[e.origin.kind] || asText(e.origin.kind) || '—' : asText(e.origin) || '—'),
          h('span', { className: 'wc-xs wc-break' }, asText(e.openQuestion) || '—')],
      })),
    }),
    h(Note, null, '账本是 Kiro 的说明，不是确认；应用后每项仍要在 02 → 审核里逐项确认。'))
}

function GenerationPreview({ g }) {
  const files = listOf(g.filePreviews)
  return h(Details, { summary: `看原文：场景结构${files.length ? ` + ${files.length} 个改动文件` : ''}` },
    g.scenarioPreview ? h(Details, { summary: 'scenario（JSON）' }, h(Pre, null, JSON.stringify(g.scenarioPreview, null, 2))) : null,
    files.map((f) => h(Details, { key: f.path, summary: h('span', { className: 'wc-mono' }, f.path + (f.truncated ? '（只显示开头）' : '')) }, h(Pre, null, f.preview))))
}

const fileCounts = (g) => {
  const f = (g.diff && g.diff.files) || {}
  const n = (k) => listOf(f[k]).length
  return g.diff ? `：文件新增 ${n('added')}、修改 ${n('modified')}、删除 ${n('deleted')}` : ''
}

function ApplyBar({ g }) {
  const ws = useWs()
  const [ack, setAck] = useState(false)
  const [ackResets, setAckResets] = useState(false)
  const [ackErrors, setAckErrors] = useState(false)
  useEffect(() => { setAck(false); setAckResets(false); setAckErrors(false) }, [g.id])
  const confirmedResets = listOf(g.provenance && g.provenance.reset).filter((r) => r.was === 'customer_confirmed')
  const errors = precheckErrors(g)
  const after = reviewWork(g.precheck && g.precheck.review, g.precheck && g.precheck.anchorsMissing, (g.scenarioPreview || {}).packKind)
  const applyError = ws.errors.apply && /changed after generation started/.test(ws.errors.apply.message || '')
    ? new Error('起草开始后项目又改过（例如记录了审核、切换了项目类型或改了文件），这份草稿不能再应用：请重新起草。') : ws.errors.apply
  return h('div', { className: 'wc-ln wc-mt', style: { paddingTop: 10 } },
    g.stale ? null : h(Check, { checked: ack, onChange: setAck }, g.mode === 'repair'
      ? `我看过这份修复结果。应用 = 只写入它改的部分${fileCounts(g)}（先做快照，可撤销），之前的校验和构建失效；Kiro 改过的项要重新审核。`
      : g.mode === 'regenerate' && listOf(g.scope).length
        ? `我看过这份改写结果。应用 = 只替换它改写的部分（${listOf(g.scope).map((x) => SCOPE_LABEL[x] || x).join('、')}）${fileCounts(g)}（先做快照，可撤销），之前的校验和构建失效${after.text ? `；应用后还要：${after.text}` : ''}。`
        : `我看过这份${g.mode === 'regenerate' ? '改写结果' : '草稿'}。应用 = 用它替换项目内容${fileCounts(g)}（先做快照，可撤销），之前的校验和构建失效${after.text ? `；应用后还要：${after.text}` : ''}。`),
    errors ? h(Check, { checked: ackErrors, onChange: setAckErrors }, h('span', { className: 'wc-tx-bad' }, `我知道应用后校验会报 ${errors} 个错误，之后再修。`)) : null,
    confirmedResets.length ? h(Check, { checked: ackResets, onChange: setAckResets }, h('span', { className: 'wc-tx-bad' }, `接受重置 ${confirmedResets.length} 项客户确认（${confirmedResets.map((r) => r.id).slice(0, 6).join('、')}${confirmedResets.length > 6 ? '…' : ''}），之后要重新请客户确认。`)) : null,
    h('div', { className: 'wc-fx wc-mt1' }, h(Button, { kind: 'primary', disabled: g.stale || !ack || (confirmedResets.length && !ackResets) || (errors && !ackErrors), busy: ws.busy === 'apply', onClick: () => ws.applyGeneration(g, confirmedResets.length > 0) }, '应用并校验')),
    h(ErrorLine, { error: applyError }))
}

function RevertBar({ g }) {
  const ws = useWs()
  const [ack, setAck] = useState(false)
  if (g.revertable === false) { // the backend's own check: revert would refuse, so no button that only fails
    return h('div', { className: 'wc-ln wc-mt', style: { paddingTop: 10 } },
      h('div', { className: 'wc-sm' }, `已在 ${fmtTime(g.appliedAt)} 应用。应用后项目又改过（例如记录了审核或改了文件），这次应用不能再撤销；要换内容就重新起草。`))
  }
  return h('div', { className: 'wc-ln wc-mt', style: { paddingTop: 10 } },
    h('div', { className: 'wc-sm' }, `已在 ${fmtTime(g.appliedAt)} 应用。`),
    h(Check, { checked: ack, onChange: setAck }, `撤销：项目回到这次应用之前的状态（文件逐字节恢复）；这份${g.mode === 'repair' ? '修复结果' : '草稿'}不会回到「待应用」，要用只能重新${g.mode === 'repair' ? '修复' : '起草'}。应用后又改过就不能撤销。`),
    h('div', { className: 'wc-mt1' }, h(Button, { disabled: !ack, busy: ws.busy === 'revert', onClick: () => ws.revertGeneration(g) }, '撤销这次应用')),
    h(ErrorLine, { error: ws.errors.revert }))
}

// What validation will say once this result is applied (routes._precheck: schema, cross-references, the engine policy).
function Precheck({ g }) {
  const ws = useWs()
  const p = g.precheck
  if (!p) return null
  if (!p.ran) return h(Note, { tone: 'warn' }, `应用前的预检没跑成（${p.error === 'engine not found' ? '找不到校验引擎' : p.error || '原因不明'}）；应用后的校验会给出结果。`)
  if (g.stale) return h('div', { className: 'wc-err wc-mt1' }, h('div', { className: 'wc-b' }, `这份${g.mode === 'repair' ? '修复结果' : '草稿'}已作废：生成后项目又改过（例如记录了审核、切换了项目类型或改了文件）`),
    h('div', { className: 'wc-sm' }, g.mode === 'repair' ? '它不能再应用。要修就重新点“Kiro 按校验结果修复”。' : `它不能再应用。要换一份就点上面的“${draftLabel(ws)}”；不需要就不用管它，继续用项目当前内容。`))
  const work = reviewWork(p.review, p.anchorsMissing, (g.scenarioPreview || {}).packKind)
  const hints = listOf(p.warnings)
  const hintList = hints.length ? h(Details, { summary: `${hints.length} 条提示（不挡构建）` }, h('ul', { className: 'wc-sm', style: { margin: '4px 0', paddingLeft: 18 } },
    hints.slice(0, 12).map((f, i) => h('li', { key: i, title: f.message }, (STAGE_REASONS.find(([re]) => re.test(`${f.code}: ${f.message}`)) || [null, f.message])[1])))) : null
  if (!p.errorCount) {
    return h('div', { className: 'wc-ok-box wc-mt1' },
      h('div', { className: 'wc-b wc-sm' }, `应用前预检：没有需要修的错误${p.warningCount ? `（${p.warningCount} 条提示，不挡构建）` : ''}`),
      work.text ? h('div', { className: 'wc-sm' }, `应用后校验的结论会是「${work.verdict}」：${work.text}。`) : null, hintList)
  }
  const sc = g.scenarioPreview
  const findings = listOf(p.findings).length ? listOf(p.findings) : listOf(p.errors).map((e) => ({ code: String(e).split(':')[0], message: String(e).replace(/^[^:]+: /, ''), source: String(e).startsWith('schema') ? 'schema' : '' }))
  return h('div', { className: 'wc-err wc-mt1' },
    h('div', { className: 'wc-b' }, `应用前预检：${p.partial ? '至少 ' : ''}${p.errorCount} 个错误${p.partial ? '（结构错误修好前，其余检查跑不了）' : ''}`),
    h('ul', { className: 'wc-sm', style: { margin: '4px 0', paddingLeft: 18 } }, findings.slice(0, 8).map((f, i) => h('li', { key: i, title: f.message },
      f.source === 'schema' ? schemaText(f.message, sc) : isGateFinding(f) ? gateText(f.message) : (STAGE_REASONS.find(([re]) => re.test(`${f.code}: ${f.message}`)) || [null, `${f.code}：${f.message}`])[1]))),
    h('div', { className: 'wc-sm' }, '可以点上面的“重新起草”；也可以先应用，再到「02 内容与评估」点“Kiro 按校验结果修复”。'))
}

// What Kiro drafted, counted by the app (Kiro's own summary describes only the stage that wrote it).
function DraftSummary({ g }) {
  const sc = g.scenarioPreview
  if (!sc || g.mode === 'repair') return g.summary ? h('div', { className: 'wc-sm' }, g.summary) : null
  const docs = listOf(sc.knowledge && sc.knowledge.documents)
  const golden = goldenOf(sc)
  const practice = golden.filter((c) => c.set === 'practice').length
  const parts = [`${listOf(sc.agent && sc.agent.roles).length} 个角色`, `${listOf(sc.facts).length} 条业务规则`, `${docs.length} 份知识文档${noiseNote(docs)}`,
    `${listOf(sc.tools).length} 个工具`, `${golden.length} 道题（练习 ${practice} · 留出 ${golden.length - practice}）`, `${phenomenaOf(sc).length} 个教学现象`]
  return h('div', { className: 'wc-mt1' },
    h('div', { className: 'wc-sm' }, h('span', { className: 'wc-b' }, g.mode !== 'regenerate' ? 'Kiro 起草了：'
      : listOf(g.scope).length ? `Kiro 改写了：${listOf(g.scope).map((x) => SCOPE_LABEL[x] || x).join('、')}。应用后整份内容：` : 'Kiro 改写了全部内容：'), parts.join(' · ')),
    g.summary ? h(Details, { summary: listOf(g.stages).length ? 'Kiro 的说明（第 1 段 · 结构）' : 'Kiro 的说明' }, h('div', { className: 'wc-sm' }, g.summary)) : null)
}

function OpenQuestions({ g }) {
  const questions = [...new Set(listOf(g.openQuestions).map((q) => String(q).trim()).filter(Boolean))]
  if (!questions.length) return null
  const list = (qs, start) => h('ol', { className: 'wc-sm', start, style: { margin: '4px 0 0', paddingLeft: 18 } }, qs.map((q, i) => h('li', { key: i }, q)))
  return h('div', { className: 'wc-mt1' },
    h('div', { className: 'wc-xs wc-mut' }, `还要客户确认的问题（${questions.length} 个）`), list(questions.slice(0, 3), 1),
    questions.length > 3 ? h(Details, { summary: `展开其余 ${questions.length - 3} 个` }, list(questions.slice(3), 4)) : null)
}

// After the apply: the one thing left before a build is the SA's review of Kiro's items.
function AppliedNext({ g }) {
  const ws = useWs()
  const drafts = listOf(ws.items && ws.items.items).filter((i) => i.provenance === 'ai_draft' || i.provenance === 'pending').length
  const anchors = ws.items && ws.items.anchors && ws.items.anchors.required ? listOf(ws.items.anchors.missing) : []
  if ((!drafts && !anchors.length) || g.mode === 'repair') return null
  const work = reviewWork(drafts, anchors, ws.items && ws.items.packKind)
  return h('div', { className: 'wc-rf wc-mt' },
    h('span', { className: 'wc-sm wc-b' }, `下一步：${work.text}。`),
    h(Button, { kind: 'primary', small: true, onClick: () => ws.go('2', 'review') }, drafts ? `去审核 ${drafts} 项 →` : '去看锚点 →'))
}

function GenerationReview({ g }) {
  return h('div', { className: 'wc-mt' },
    g.status === 'ready' ? h(Precheck, { g }) : null,
    g.status === 'applied' ? h(AppliedNext, { g }) : null,
    g.status === 'needs-input' ? (g.summary ? h('div', { className: 'wc-sm' }, g.summary) : null) : h(DraftSummary, { g }),
    h(GenerationWarnings, { g }),
    g.status === 'needs-input' ? h(NeedsInputForm, { g }) : h(OpenQuestions, { g }),
    g.status !== 'needs-input' && g.diff ? h(DiffSummary, { g }) : null,
    h(ChangesTable, { g }),
    h(TruthLedger, { g }),
    g.status !== 'needs-input' ? h(GenerationPreview, { g }) : null,
    g.status === 'ready' ? h(ApplyBar, { g }) : null,
    g.status === 'applied' ? h(RevertBar, { g }) : null,
    g.status === 'superseded' ? h(Note, { tone: 'warn' }, `已有更新的一轮（${g.supersededBy}），这份不能再应用。`) : null,
    g.status === 'reverted' ? h(Note, null, `已在 ${fmtTime(g.revertedAt)} 撤销。`) : null)
}

// Why a generation failed, as the SA reads it (the English original is the tooltip).
function failText(error) {
  const m = String(error || '').match(/^draft stage (\S+) failed: ([\s\S]*)$/)
  if (m) return `分步起草停在「${stageInfo({ name: m[1] })[0]}」这一步：${stageReason(m[2])}。可以重新起草。`
  return backendText(error) || String(error)
}

function GenerationDetail({ modes }) {
  const ws = useWs()
  const g = ws.generation
  if (!g) return h(ResourceError, { name: 'generation', hint: 'Kiro 生成记录读不到' })
  if (!modes.includes(g.mode)) {
    if (!isLive(g) && g.status !== 'ready') return null
    const step = g.mode === 'repair' ? '2' : '1'
    return h('div', { className: 'wc-rf wc-mt' }, h('span', { className: 'wc-sm' }, `最近一轮是「${GEN_MODE[g.mode]}」：${g.status === 'ready' && g.stale ? '已作废' : pick(GEN_STATUS, g.status)[1]}`), h(Button, { small: true, onClick: () => ws.go(step, 'eval') }, '去看 →'))
  }
  return h('div', { className: 'wc-mt' },
    h(GenerationStatus, { g }),
    h(GenerationStages, { g }),
    g.error ? h('div', { className: 'wc-err', title: g.error }, failText(g.error)) : null,
    ws.lost.generation ? h(Note, { tone: 'warn' }, '和后端失联：生成还在后台进行，点顶部“刷新”继续跟进。') : null,
    isLive(g) ? h(Note, null, g.staged ? `Kiro 正在分步起草，每一步最长 ${Math.round((g.timeoutSecs || 0) / 60)} 分钟；可以离开页面，回来会继续显示进度。` : `Kiro 正在${GEN_MODE[g.mode]}（最长 ${Math.round((g.timeoutSecs || 0) / 60)} 分钟），可以离开页面，回来会继续显示进度。`) : h(GenerationReview, { g }))
}

function ScopePicker({ value, onChange }) {
  const toggle = (s) => onChange(value.includes(s) ? value.filter((x) => x !== s) : [...value, s])
  return h('span', { className: 'wc-fx' }, SCOPES.map((s) => h(Chip, { key: s, tone: value.includes(s) ? 'acc' : '', onClick: () => toggle(s) }, SCOPE_LABEL[s])))
}

// The 01 card's draft button, as it reads now (other texts that send the SA to it name it the same way).
function draftLabel(ws) {
  const g = ws.generation
  if (g && g.status === 'needs-input') return '不回答，直接重新起草'
  if (pendingDraft(ws)) return '重新起草（丢弃这份未应用的草稿）'
  if (templateOrigin(ws)) return '用 Kiro 起草（替换模板内容）'
  return goldenOf(ws.sc).length > 0 ? '从场景描述重新起草（整份替换）' : '生成草稿'
}

function DraftCard() {
  const ws = useWs()
  const [scope, setScope] = useState([])
  const [includeRehearsal, setIncludeRehearsal] = useState(true)
  const rehearsal = rehearsalFindingsFor(ws.validationView, scope) // a regenerate sends these too, unless unticked
  const g = ws.generation
  const live = isLive(g)
  const usable = listOf(ws.materials && ws.materials.materials).some((m) => m.generationUse === 'source' && m.extraction && ['ok', 'partial'].includes(m.extraction.status))
  const canDraft = ws.brief.trim().length >= 20 || usable
  const blocked = wsMaterialsBlock(ws, false) // the backend refuses a draft or regenerate without it (409)
  const hasPack = goldenOf(ws.sc).length > 0
  const waiting = pendingDraft(ws)
  const origin = templateOrigin(ws)
  const asking = !!g && g.status === 'needs-input' // the questions' form below holds the main action
  const label = draftLabel(ws)
  const pre = g && g.status === 'ready' ? precheckErrors(g) : 0
  const chip = !g || g.mode === 'repair' ? null : g.status === 'ready' && g.stale ? h(Chip, { tone: 'bad' }, '已作废：生成后项目改过') : pre ? h(Chip, { tone: 'bad' }, `待应用 · 预检 ${pre} 个错误`) : h(ToneChip, { map: GEN_STATUS, value: g.status })
  return h(Card, { title: 'Kiro 起草', extra: chip },
    h('div', { className: 'wc-sm' }, '按场景描述和资料起草整套内容：知识文档、工具、Prompt、题目与判据和教学设计。每项都先标成 Kiro 草稿，等你审核。'),
    h(Note, null, '描述和资料会发给你的 Kiro CLI 所用的模型；不要粘贴凭证、个人信息或未获批准的客户机密。这一步不调用 AWS。'),
    h('div', { className: 'wc-fx wc-mt' },
      h(Button, { kind: !asking && !waiting && (!hasPack || origin) ? 'primary' : undefined, icon: Wand2, disabled: live || !canDraft || !!blocked, busy: ws.busy === 'generate', onClick: () => ws.startDraft() }, label),
      !canDraft ? h('span', { className: 'wc-xs wc-mut' }, `描述再写 ${Math.max(0, 20 - ws.brief.trim().length)} 字，或上传一份作为事实来源的资料`) : null),
    blocked ? h(Note, { tone: 'bad' }, blocked) : null,
    g && g.mode === 'repair' && holdingResult(ws) ? h(Note, { tone: 'warn' }, `02 有一份${isLive(g) ? '正在生成' : '待应用'}的 Kiro 修复结果：在这里重新起草或改写会丢弃它。`) : null,
    canRewrite(ws) ? h('div', { className: 'wc-mt' },
      h('div', { className: 'wc-xs wc-mut' }, '在项目当前内容上只改写一部分（不选就改写全部），其余保持原样：'),
      h('div', { className: 'wc-mt1' }, h(ScopePicker, { value: scope, onChange: setScope })),
      rehearsal.length ? h('div', { className: 'wc-mt1' }, h(Check, { checked: includeRehearsal, onChange: setIncludeRehearsal },
        `连彩排整改一起发给 Kiro（${rehearsal.length} 条：${rehearsal.map((f) => f.id).join('、')}）`)) : null,
      h('div', { className: 'wc-mt1' }, h(Button, { disabled: live || !!blocked, busy: ws.busy === 'regenerate', onClick: () => ws.startRegenerate(scope, includeRehearsal) }, scope.length ? `在当前内容上改写：${scope.map((s) => SCOPE_LABEL[s]).join('、')}` : '在当前内容上改写全部'))) : null,
    h(ErrorLine, { error: ws.errors.generate || ws.errors.regenerate }),
    h(GenerationDetail, { modes: ['draft', 'regenerate'] }))
}

function PhenomenaCard() {
  const ws = useWs()
  const draft = pendingDraft(ws)
  const t = teachingOf(draft ? draft.scenarioPreview : ws.sc)
  if (!t) {
    return h(Card, { title: '这场要让学员看到的现象' }, draftRunning(ws)
      ? h(Empty, { title: 'Kiro 正在写教学设计' }, '分步起草的第 2 段写现象、弱基线和练习题；草稿完成后在这里预览。')
      : h(Empty, { title: '还没有教学设计（labs.teaching）' }, '生成草稿后由 Kiro 起草；它决定弱基线、干扰文档和练习题。'))
  }
  const fc = t.firstConversation || {}
  const origin = draft ? null : templateOrigin(ws)
  return h(Card, { title: '这场要让学员看到的现象', extra: h('span', { className: 'wc-fx' }, h(OriginTag, { draft, origin }), h('span', { className: 'wc-xs wc-mut' }, `${listOf(t.phenomena).length} 个现象 · ${listOf(t.baselineDefects).length} 处弱基线`)) },
    draftRunning(ws) ? h(Note, { tone: 'warn' }, origin ? `Kiro 正在起草新内容；下面仍是模板「${origin}」的现象，草稿完成后换成草稿的。` : 'Kiro 正在起草新内容；下面是项目当前的现象，草稿完成后换成草稿的。') : null,
    listOf(t.phenomena).map((p, i) => h('div', { key: p.id, className: cx('wc-it', i && 'wc-ln') },
      h('div', { className: 'wc-rf wc-top-al' }, h('span', { className: 'wc-b wc-mono wc-sm' }, p.id),
        h('span', { className: 'wc-fx' }, h(Chip, { tone: 'acc' }, PHENOMENON_KIND[p.kind] || p.kind), p.mechanism ? h(Chip, null, MECHANISM[p.mechanism] || p.mechanism) : null, p.design === 'contrast' ? h(Chip, { tone: 'info' }, '基线要失败') : null)),
      h('div', { className: 'wc-sm wc-mt1' }, p.teachingPoint),
      h('div', { className: 'wc-xs wc-mut' }, listOf(p.caseIds).length ? `练习题：${p.caseIds.map((id) => caseName(draft ? draft.scenarioPreview : ws.sc, id)).join('、')}`
        : listOf(p.documentIds).length ? `文档：${p.documentIds.map((id) => docName(draft ? draft.scenarioPreview : ws.sc, id)).join('、')}` : ''))),
    h('div', { className: 'wc-ln wc-it' },
      h('div', { className: 'wc-xs wc-mut' }, `第一次对话（Guided Run「第一次对话」那一步）· 提问人 ${fc.actorId ? actorName(draft ? draft.scenarioPreview : ws.sc, fc.actorId) : '—'}`),
      h('div', { className: 'wc-sm' }, fc.query || '—'),
      fc.teachingPoint ? h('div', { className: 'wc-xs wc-mut' }, fc.teachingPoint) : null))
}

function ScenarioStep() {
  return h(Fragment, null,
    h(StepHeader, { n: '01', title: '场景与课程', h1: '说清客户要什么、这场要教什么', sub: '只填必要信息，Kiro 据此起草全部内容；之后随时回来改，改完重新校验和构建。' }),
    h('div', { className: 'wc-g2 wc-mt' },
      h('div', { className: 'wc-col' }, h(ScenarioCard), h(MaterialsCard)),
      h('div', { className: 'wc-col' }, h(DraftCard), h(PhenomenaCard))),
    h(StepFoot, { note: '生成在后台进行，可以离开页面。', next: ['2', '去内容与评估 →', 'eval'] }))
}

// ---------------------------------------------------------------------------
// 02 内容与评估
// ---------------------------------------------------------------------------

function FindingRow({ f, sc }) {
  const ws = useWs()
  const [tone, family] = ANCHOR_GATE.test(String(f.messageEn || f.message || '')) || CUSTOMER_GATE.test(String(f.messageEn || f.message || '')) ? ['warn', '客户确认'] : familyOf(f.source === 'gate' ? 'gate' : f.code)
  // The gate's reason and a schema error in Chinese; the English original is the tooltip.
  const gate = isGateFinding(f) ? gateText(f.message) : f.source === 'schema' || f.code === 'schema' ? schemaText(f.message, sc || ws.sc) : null
  const zh = gate ? null : (STAGE_REASONS.find(([re]) => re.test(`${f.code}: ${f.message}`)) || [])[1]
  return h('div', { className: 'wc-list-row' },
    isGateFinding(f) ? h(Chip, { tone: 'warn', title: '不是内容错误：等审核或客户确认' }, '待') : h(Chip, { tone: f.severity === 'error' ? 'bad' : 'warn' }, f.severity === 'error' ? '✕' : '!'),
    h('div', { className: 'wc-grow' },
      h('div', { className: 'wc-fx' }, h(Chip, { tone }, family), h('span', { className: 'wc-mono wc-xs wc-b' }, f.code), f.id && !isGateFinding(f) ? h('span', { className: 'wc-xs wc-mut' }, f.id) : null,
        listOf(f.scopes).slice(1).map((s) => h(Chip, { key: s }, SCOPE_LABEL[s] || s))),
      h('div', { className: 'wc-sm wc-break wc-mt1', title: gate && gate !== f.message ? f.message : englishTwin(f.message, f.messageEn) }, gate || zh || f.message),
      zh ? h('div', { className: 'wc-xs wc-mut wc-break' }, f.message) : null,
      f.path ? h('div', { className: 'wc-xs wc-mut wc-mono wc-break' }, f.path) : null))
}

// The items the provenance gate holds until the SA reviews them, in one line per kind (the review tab lists each).
function ReviewPending({ findings }) {
  const ws = useWs()
  if (!findings.length) return null
  const kinds = {}
  for (const f of findings) { const k = GATE_TYPE[String(f.code || '').replace(/^gate\./, '')] || f.code; kinds[k] = (kinds[k] || 0) + 1 }
  return h('div', { className: 'wc-ln wc-it' },
    h('div', { className: 'wc-rf' }, h('span', { className: 'wc-b wc-tx-warn' }, `待你审核的 Kiro 草稿：${findings.length} 项`),
      h(Button, { small: true, onClick: () => ws.go('2', 'review') }, '去审核 →')),
    h('div', { className: 'wc-sm' }, Object.entries(kinds).map(([k, n]) => `${k} ${n}`).join(' · ')),
    h(Note, null, ws.items && ws.items.packKind === 'customer' ? '这不是内容错误：阻断项要客户逐项确认（确认人 + 凭证；SA 合成不能替代），其余由你审核，之后才能构建正式包。'
      : '这不是内容错误：Kiro 起草的每一项都要你过目（逐项确认，或批量记为 SA 合成），审核完才能构建正式包。'))
}

function FindingGroup({ title, findings, tone, note, action, count }) {
  const [all, setAll] = useState(false)
  if (!findings.length) return null
  const shown = all ? findings : findings.slice(0, 5)
  const errors = findings.filter((f) => f.severity === 'error').length
  return h('div', { className: 'wc-ln wc-it' },
    h('div', { className: 'wc-rf' }, h('span', { className: cx('wc-b', tone && `wc-tx-${tone}`) }, title),
      h('span', { className: 'wc-xs wc-mut' }, count || (errors ? `${errors} 个错误${findings.length - errors ? ` · ${findings.length - errors} 条提示` : ''}` : `${findings.length} 条提示（不挡构建）`))),
    note || action ? h('div', { className: 'wc-rf' }, note ? h(Note, null, note) : h('span'), action || null) : null,
    shown.map((f, i) => h(FindingRow, { key: f.id || i, f })),
    findings.length > 5 ? h(Button, { small: true, kind: 'ghost', onClick: () => setAll(!all) }, all ? '收起' : `展开全部 ${findings.length} 条`) : null)
}

function RepairLauncher({ v, fresh }) {
  const ws = useWs()
  const [includeWarnings, setIncludeWarnings] = useState(false)
  const [includeRehearsal, setIncludeRehearsal] = useState(true)
  const [instructions, setInstructions] = useState('')
  const [scope, setScope] = useState([])
  const [more, setMore] = useState(false)
  const repairable = listOf(v && v.repair && v.repair.repairable)
  // With a chosen scope the route leaves out the findings that scope cannot fix, as it does the rehearsal's.
  const selected = repairable.filter((f) => repairSelected(f, includeWarnings)
    && (!scope.length || !listOf(f.scopes).length || listOf(f.scopes).some((s) => scope.includes(s))))
  const nErrors = selected.filter((f) => f.severity === 'error').length
  const nHints = selected.filter((f) => f.severity !== 'error' && repairSelected(f, false)).length
  const nOther = selected.length - nErrors - nHints
  const otherWarnings = repairable.filter((f) => !repairSelected(f, false)).length
  const unreviewedOnly = !repairable.length && listOf(v && v.repair && v.repair.saOnly).some(isUnreviewed)
  // What the route really sends: the selected validation findings plus the current rehearsal's R findings.
  const rehearsalAll = rehearsalFindingsFor(ws.validationView, scope)
  const rehearsal = includeRehearsal ? rehearsalAll : []
  const sent = [...selected, ...rehearsal]
  const scopes = repairScopes(scope, v, selected, rehearsal)
  const live = isLive(ws.generation)
  const blocked = wsMaterialsBlock(ws, true) // a repair sends the source materials
  const waiting = !live && holdingResult(ws) && (ws.generation || {}).mode !== 'repair' // a repair would void the draft that waits in 01
  const can = !live && !waiting && !blocked && (sent.length > 0 || (instructions.trim() && scope.length > 0))
  return h('div', { className: 'wc-ln wc-mt', style: { paddingTop: 10 } },
    h('div', { className: 'wc-rf' },
      h('span', { className: 'wc-sm' }, sent.length
        ? `Kiro 会修 ${sent.length} 条：${[nErrors ? `${nErrors} 个错误` : null, nHints ? `${nHints} 条教学提示（教学类提示总是一起修）` : null, nOther ? `${nOther} 条一般提示` : null, rehearsal.length ? `${rehearsal.length} 条来自彩排整改` : null].filter(Boolean).join('、')}（范围：${scopes.map((s) => SCOPE_LABEL[s] || s).join('、')}）`
        : unreviewedOnly ? '没有 Kiro 能修的错误；剩下的审核和客户确认，修复不处理' : '没有 Kiro 能修的校验结果'),
      h(Button, { kind: nErrors || rehearsal.length ? 'primary' : undefined, icon: Wand2, disabled: !can, busy: ws.busy === 'repair', onClick: () => ws.startRepair({ includeWarnings, instructions, scope, includeRehearsal }) }, nErrors || rehearsal.length || !sent.length ? 'Kiro 按校验结果修复' : '（可选）让 Kiro 处理这些提示')),
    !nErrors && sent.length && !rehearsal.length ? h(Note, null, '提示不挡构建。要修就先修再审核：Kiro 改过的项会回到 Kiro 草稿，要重新审核。') : null,
    blocked ? h(Note, { tone: 'bad' }, blocked) : null,
    waiting ? h(Note, { tone: 'warn' }, '01 有一份待应用的 Kiro 草稿：现在开始修复会让它作废。先应用它，再修复。') : null,
    rehearsal.length ? h('div', { className: 'wc-mt1' }, rehearsal.map((f) => h(FindingRow, { key: f.id, f }))) : null,
    h('div', { className: 'wc-fx wc-mt1' },
      otherWarnings ? h(Check, { checked: includeWarnings, onChange: setIncludeWarnings }, `其余 ${otherWarnings} 条一般提示也一起修`) : null,
      rehearsalAll.length ? h(Check, { checked: includeRehearsal, onChange: setIncludeRehearsal }, `连彩排整改一起修（${rehearsalAll.length} 条）`) : null,
      h(Button, { small: true, kind: 'ghost', onClick: () => setMore(!more) }, more ? '收起说明' : '补充说明 / 限定范围')),
    more ? h('div', { className: 'wc-col wc-mt1', style: { gap: 6 } },
      h(TextArea, { value: instructions, onChange: setInstructions, rows: 3, placeholder: '给 Kiro 的额外说明（可选，≤ 4000 字）', label: '修复说明' }),
      h('div', { className: 'wc-xs wc-mut' }, '限定范围（不选 = 按校验结果自动定）：'), h(ScopePicker, { value: scope, onChange: setScope })) : null,
    !fresh && v ? h(Note, null, '内容在校验后改过；点修复时会先重新校验。') : null,
    h(ErrorLine, { error: ws.errors.repair }))
}

function ValidationCard() {
  const ws = useWs()
  const view = ws.validationView
  const v = view && view.validation
  const fresh = !!(view && view.fresh)
  const repair = (v && v.repair) || {}
  const repairable = listOf(repair.repairable)
  const byScope = {}
  for (const f of repairable) { const s = listOf(f.scopes)[0] || 'other'; (byScope[s] = byScope[s] || []).push(f) }
  const errors = v ? contentErrorCount(v) : 0
  const warnings = v ? [...listOf(v.policy), ...listOf(v.output), ...listOf(v.workspace)].filter((f) => f.severity === 'warning').length : 0
  const unreviewed = listOf(repair.saOnly).filter(isUnreviewed)
  const anchorGate = listOf(repair.saOnly).filter((f) => !isUnreviewed(f) && ANCHOR_GATE.test(String(f.messageEn || f.message || '')))
  const customerGate = listOf(repair.saOnly).filter((f) => !isUnreviewed(f) && CUSTOMER_GATE.test(String(f.messageEn || f.message || '')))
  const decide = listOf(repair.saOnly).filter((f) => !isUnreviewed(f) && !anchorGate.includes(f) && !customerGate.includes(f))
  const need = listOf(ws.items && ws.items.items).filter(needsCustomer).length
  const work = reviewWork(unreviewed.length, anchorGate.length ? listOf((v && v.anchors && v.anchors.anchorsMissing) || ['normal', 'boundary', 'prohibited']) : [], ws.items && ws.items.packKind, need)
  const holding = holdingResult(ws)
  const errorsByScope = {}
  const hintsByScope = {}
  for (const f of repairable) { const s = listOf(f.scopes)[0] || 'other'; const into = f.severity === 'error' ? errorsByScope : hintsByScope; (into[s] = into[s] || []).push(f) }
  return h(Card, {
    title: '评估体检',
    extra: [
      h(Chip, { key: 'f', tone: !v ? 'warn' : fresh ? 'ok' : 'warn' }, !v ? '还没校验' : fresh ? `按当前内容 · ${fmtTime(v.at)}` : '内容已改，需重新校验'),
      h(Button, { key: 'b', busy: ws.busy === 'validate', onClick: ws.validate }, '校验（含试构建）'),
    ],
  },
  h(ErrorLine, { error: ws.errors.validate }),
  !v ? h(Empty, { title: '还没校验' }, '校验会检查结构、引用、来源门禁、教学设计、L1 判据、手册，并试构建一次（不写 Release）。') : h(Fragment, null,
    h(Kpi, null,
      h(Stat, { label: '结论', value: v.ok ? '通过' : errors ? '有错误' : decide.length ? '待 SA 处理' : work.verdict, tone: v.ok ? 'ok' : errors ? 'bad' : 'warn', small: true }),
      h(Stat, { label: '要修的错误', value: errors, tone: errors ? 'bad' : 'ok' }),
      h(Stat, { label: '提示', value: warnings, tone: warnings ? 'warn' : '' }),
      h(Stat, { label: '待你审核', value: unreviewed.length, tone: unreviewed.length ? 'warn' : '' }),
      h(Stat, { label: '试构建', value: v.dryBuild && v.dryBuild.ran ? `${fmtNum(v.dryBuild.seconds, 1)} 秒` : '跳过' })),
    v.dryBuild && v.dryBuild.skipped ? h(Note, null, `试构建跳过：${DRY_SKIP[v.dryBuild.skipped] || v.dryBuild.skipped}`) : null,
    !fresh && !holding ? h('div', { className: 'wc-err' }, '内容在这次校验之后改过（例如记录了审核或改了文件）：下面是上次的结果，点右上角“校验（含试构建）”更新。') : null,
    holding ? h(Note, { tone: 'warn' }, (ws.generation || {}).mode === 'repair' ? `下面是项目当前内容的校验；${isLive(ws.generation) ? '正在生成' : '待应用'}的 Kiro 修复结果只改它列出的部分（见本卡下方）。先处理它，再审核。`
      : `下面是项目当前内容的校验，不是 01 里${isLive(ws.generation) ? '正在生成' : '待应用'}的 Kiro 草稿；${replaceText(ws.generation)}。先处理它，再审核。`)
      : !errors && !v.ok && work.text && fresh ? h(Note, { tone: 'warn' }, `内容没有要修的错误。还要：${work.text}。`) : null,
    [...SCOPES, 'other'].filter((s) => errorsByScope[s]).map((s) => h(FindingGroup, { key: `e-${s}`, title: s === 'other' ? '其他' : SCOPE_LABEL[s], tone: 'bad', findings: errorsByScope[s] })),
    h(FindingGroup, { title: '需要客户确认', tone: 'warn', findings: [...customerGate, ...anchorGate], count: `${customerGate.length + anchorGate.length} 项（不是内容错误）`,
      note: customerGate.length ? '客户严格确认的包：阻断项要客户确认（确认人 + 凭证）；Kiro 不改。' : 'Workshop 包要请客户确认锚点题；Kiro 不改。',
      action: h(Button, { small: true, onClick: () => ws.go('2', 'review') }, customerGate.length ? '去审核 →' : '去看锚点 →') }),
    h(ReviewPending, { findings: unreviewed }),
    h(FindingGroup, { title: '需要 SA 决定', tone: 'warn', findings: decide, note: '项目类型或资料问题：Kiro 不改。', action: h(Button, { small: true, onClick: () => ws.go('2', 'review') }, '去审核 →') }),
    [...SCOPES, 'other'].filter((s) => hintsByScope[s]).map((s) => h(FindingGroup, { key: `h-${s}`, title: `${s === 'other' ? '其他' : SCOPE_LABEL[s]} · 提示`, findings: hintsByScope[s] })),
    h(FindingGroup, { title: '模板或应用问题', tone: 'bad', findings: listOf(repair.engine), note: 'Kiro 修不了；多半要更新模板或应用。' }),
    !repairable.length && !decide.length && !anchorGate.length && !customerGate.length && !listOf(repair.engine).length && !unreviewed.length ? h(Note, { tone: 'ok' }, '没有发现问题。') : null),
  h(RepairLauncher, { v, fresh }),
  h(GenerationDetail, { modes: ['repair'] }))
}

function PhenomenaMatrix() {
  const ws = useWs()
  const sc = ws.sc
  const phenomena = phenomenaOf(sc)
  const cases = Object.fromEntries(goldenOf(sc).map((c) => [c.id, c]))
  const findings = listOf(ws.validationView && ws.validationView.validation && ws.validationView.validation.repair && ws.validationView.validation.repair.repairable)
  const v = ws.validationView && ws.validationView.validation
  const index = Object.fromEntries(phenomena.map((p, i) => [p.id, i]))
  const hits = (p) => findings.filter((f) => String(f.path || '').includes(p.id) || String(f.message || '').includes(`'${p.id}'`)
    || String(f.path || f.message || '').includes(`labs/teaching/phenomena/${index[p.id]}/`) || listOf(p.caseIds).some((c) => String(f.path || '').endsWith(`.${c}`)))
  const unchecked = v && listOf(v.schema).length > 0 // the scenario did not load: the teaching rules never ran
  const rows = phenomena.map((p) => {
    const found = hits(p)
    return {
      key: p.id,
      cells: [
        h('div', null, h('div', { className: 'wc-b wc-mono wc-xs' }, p.id), h('div', { className: 'wc-xs wc-mut' }, p.teachingPoint)),
        h('div', null, h(Chip, { tone: 'acc' }, PHENOMENON_KIND[p.kind] || p.kind), p.mechanism ? h('div', { className: 'wc-xs wc-mut wc-mt1' }, MECHANISM[p.mechanism] || p.mechanism) : null),
        h('div', null, listOf(p.caseIds).map((cid) => {
          const c = cases[cid]
          return h('div', { key: cid, style: { marginBottom: 4 } },
            h('span', { className: 'wc-xs' }, caseName(ws.sc, cid)), c ? h(Chip, { tone: c.set === 'holdout' ? 'warn' : '' }, SET_LABEL[c.set] || c.set) : h(Chip, { tone: 'bad' }, '不存在'),
            c ? h('div', null, h(ChipList, { chips: expectationChips(c.expected) })) : null)
        }), listOf(p.documentIds).length ? h('div', { className: 'wc-xs wc-mut' }, `文档：${p.documentIds.map((id) => docName(ws.sc, id)).join('、')}`) : null),
        h('span', { className: 'wc-xs' }, JUDGE_OF_KIND[p.kind] || '—'),
        found.length ? h(Chip, { tone: found.some((f) => f.severity === 'error') ? 'bad' : 'warn', title: found.map((f) => f.message).join('\n') }, `${found.length} 条要看`)
          : unchecked ? h(Chip, { tone: 'warn', title: '结构错误修好后才会检查教学设计' }, '未检查（结构错误）')
            : h(Chip, { tone: v ? 'ok' : '' }, v ? '✓' : '未校验'),
      ],
    }
  })
  return h(Card, { title: '教学现象 × 题目 × 判据', sub: '每个现象要能被练习题暴露，并由对应的判据判定；留出题只在导师侧使用。' },
    phenomena.length ? h(Table, { head: ['现象', '类型', '练习题与 L1 判据', '判据', '状态'], rows })
      : pendingDraft(ws) ? h(Empty, { title: 'Kiro 草稿待应用' }, '去 01 看草稿并应用；应用后这里显示现象、练习题和判据。')
        : draftRunning(ws) ? h(Empty, { title: 'Kiro 正在起草' }, '草稿完成并应用后，这里显示现象、练习题和判据。')
          : h(Empty, { title: '还没有教学设计' }, '去 01 生成草稿。'),
    h('div', { className: 'wc-note wc-ln', style: { paddingTop: 8 } }, 'L1 = 代码检查（调了哪些工具、有没有提到关键内容、有没有拒绝或升级）；Mind the Goal 看整段对话目标是否达成；THELMA 只评走检索的题（GR = 回答有没有依据、不编造 · SP1 = 检索到的文段准不准 · SP2 = 事实级检索精度 · SQC = 资料覆盖）。没有 trace、关联不到题、裁判报错，都记为证据不足，不算通过。'))
}

function TeachingEvalTab() {
  return h('div', { className: 'wc-col' }, h(ValidationCard), h(PhenomenaMatrix))
}

function ClassSummaryCard() {
  const ws = useWs()
  const it = ws.items
  const list = listOf(it && it.items)
  const byProv = {}
  for (const i of list) byProv[i.provenance] = (byProv[i.provenance] || 0) + 1
  const kind = PACK_KIND[it && it.packKind] || {}
  return h(Card, { title: '内容类别', extra: h(Chip, { tone: kind.tone }, kind.label || (it && it.packKind)) },
    h(Kpi, null, Object.entries(CLASS_LABEL).map(([k, [tone, label]]) => h(Stat, { key: k, label: k === 'draft' ? `${label}（不含手册叙述）` : label, value: (it && it.classes && it.classes[k]) || 0, tone: (it && it.classes && it.classes[k]) ? tone : '' }))),
    h('div', { className: 'wc-fx wc-mt1 wc-xs' }, Object.entries(byProv).map(([k, n]) => h(Chip, { key: k, tone: pick(PROVENANCE_LABEL, k)[0] }, `${pick(PROVENANCE_LABEL, k)[1]} ${n}`))),
    h(Note, null, kind.help))
}

function AnchorsCard() {
  const ws = useWs()
  const it = ws.items
  if (!it || !it.anchors || !it.anchors.required) return null
  const candidates = it.anchorCandidates || {}
  const cases = Object.fromEntries(goldenOf(ws.sc).map((c) => [c.id, c]))
  const facts = Object.fromEntries(listOf(ws.sc && ws.sc.facts).map((f) => [f.id, f]))
  return h(Card, { title: '客户锚点题', sub: '正常、边界、禁止三类各要至少一道客户确认的题，它依据的业务规则也要客户确认。' },
    h('div', { className: 'wc-g3' }, CATEGORIES.map((cat) => {
      const have = listOf(it.anchors[cat])
      // A confirmed question is an anchor only once every rule it rests on is confirmed too: say what is left.
      const waiting = goldenOf(ws.sc).filter((c) => c.category === cat && c.provenance === 'customer_confirmed' && !have.includes(c.id))
      const suggested = listOf(candidates[cat]).filter((id) => !have.includes(id) && !waiting.some((c) => c.id === id))
      return h('div', { key: cat, className: 'wc-tile' },
        h('div', { className: 'wc-rf' }, h('span', { className: 'wc-b' }, CATEGORY_LABEL[cat]), have.length ? h(Chip, { tone: 'ok' }, `${have.length} 道`) : h(Chip, { tone: 'bad' }, waiting.length ? '差规则' : '缺')),
        have.map((id) => h('div', { key: id, className: 'wc-xs' }, `✓ ${caseName(ws.sc, id)}`)),
        waiting.map((c) => {
          const left = listOf(c.basis).filter((fid) => !(facts[fid] && facts[fid].provenance === 'customer_confirmed'))
          return h('div', { key: c.id, className: 'wc-xs wc-mt1' }, h('span', null, caseName(ws.sc, c.id)),
            listOf(c.basis).length ? ' 题已确认 · 还差规则 ' : ' 题已确认，但没写依据的规则，算不了锚点', left.map((fid) => h(Chip, { key: fid, tone: 'warn' }, fid)))
        }),
        suggested.length ? h('div', { className: 'wc-xs wc-mut wc-mt1' }, 'Kiro 建议先确认：', suggested.map((id) => h('div', { key: id },
          h('span', null, caseName(ws.sc, id)), ' ', listOf(cases[id] && cases[id].basis).map((fid) => h(Chip, { key: fid, tone: facts[fid] && facts[fid].provenance === 'customer_confirmed' ? 'ok' : 'warn' }, `规则 ${fid}`))))) : null)
    })),
    h(Note, null, '在下面的列表里找到这道题和它依据的规则，逐项“记录客户确认”。'))
}

function BatchReviewCard() {
  const ws = useWs()
  const it = ws.items
  const kind = it && it.packKind
  const drafts = listOf(it && it.items).filter((i) => i.provenance === 'ai_draft' || i.provenance === 'pending')
  const pending = drafts.filter((i) => i.confirmable)
  const [selected, setSelected] = useState([])
  const [ack, setAck] = useState(false)
  const [originKind, setOriginKind] = useState('sa_authored')
  const chosen = selected.filter((id) => pending.some((i) => i.id === id))
  useEffect(() => { setAck(false) }, [chosen.join('|')])
  if (!it || kind === 'customer' || !drafts.length) return null
  // Drafts whose ids the review actions cannot address (the backend marks them not confirmable).
  const stuck = drafts.filter((i) => !i.confirmable)
  const stuckNote = stuck.length ? h('div', { className: 'wc-err' }, `${stuck.length} 项 Kiro 草稿的 id 无法被审核操作定位（${stuck.slice(0, 6).map((i) => i.id || '（没有 id）').join('、')}${stuck.length > 6 ? '…' : ''}），这里不能审核它们；先到“高级 → scenario.yaml”改正它们的 id。`) : null
  if (!pending.length) return h(Card, { title: '批量审核' }, stuckNote)
  const toggle = (id) => setSelected(chosen.includes(id) ? chosen.filter((x) => x !== id) : [...chosen, id])
  const submit = () => ws.confirmBatch(chosen, kind === 'workshop' ? { kind: originKind } : null).then((r) => { if (r) { setSelected([]); setAck(false) } })
  const holding = holdingResult(ws)
  return h(Card, { title: `批量审核：${pending.length} 项 Kiro 草稿`, sub: kind === 'workshop' ? '把选中的项记为 SA 合成教学设定，并标来源；客户事实请逐项记录客户确认。' : '参考包：SA 对照内容后，把选中的项记为 SA 合成数据（不是客户确认）。' },
    stuckNote,
    h('div', { className: 'wc-fx' },
      h(Button, { small: true, onClick: () => setSelected(pending.map((i) => i.id)) }, `全选 ${pending.length}`),
      h(Button, { small: true, disabled: !chosen.length, onClick: () => setSelected([]) }, '清空'),
      h('span', { className: 'wc-xs wc-mut' }, `已选 ${chosen.length}`)),
    h('div', { className: 'wc-mt1', style: { maxHeight: 280, overflow: 'auto', border: '1px solid var(--wc-border)', borderRadius: 8, padding: '0 8px' } },
      pending.map((i) => h('div', { key: `${i.kind}:${i.id}`, className: 'wc-list-row' },
        h(Check, { checked: chosen.includes(i.id), onChange: () => toggle(i.id) },
          h('span', { className: 'wc-fx' }, h(Chip, null, ITEM_KIND_LABEL[i.kind] || i.kind), h('span', { className: 'wc-mono wc-xs' }, i.id)),
          i.label ? h('span', { className: 'wc-xs wc-mut', style: { display: 'block' } }, i.label) : null)))),
    kind === 'workshop' ? h('div', { className: 'wc-mt1' }, h(Field, { label: '来源（没标来源的项用它）' }, h(Seg, { value: originKind, onChange: setOriginKind, options: [['sa_authored', ORIGIN_LABEL.sa_authored], ['teaching_design', ORIGIN_LABEL.teaching_design]] }))) : null,
    h('div', { className: 'wc-mt1' }, h(Check, { checked: ack, disabled: !chosen.length, onChange: setAck }, `我逐项看过这 ${chosen.length} 项，确认它们是我（SA）提供的安全合成数据。这不是客户确认。`)),
    h('div', { className: 'wc-mt1' }, h(Button, { kind: 'primary', disabled: !ack || !chosen.length || holding, busy: ws.busy === 'batch', onClick: submit }, `记为 SA 合成（${chosen.length}）`)),
    h(ErrorLine, { error: ws.errors.batch }))
}

function ConfirmItemForm({ item, onDone, locked }) {
  const ws = useWs()
  const kind = ws.items && ws.items.packKind
  const narrative = item.kind === 'narrative'
  const [prov, setProv] = useState(narrative || kind === 'reference' ? 'sa_synthetic' : 'customer_confirmed')
  const [by, setBy] = useState('')
  const [ref, setRef] = useState('')
  const [materialId, setMaterialId] = useState('')
  // The guide narrative (labs.guide) is teaching prose with no origin field: the server refuses any origin
  // on it in every pack kind, so its form offers none and never sends one.
  const [originKind, setOriginKind] = useState(narrative ? '' : (item.origin && item.origin.kind) || (kind === 'workshop' ? 'sa_authored' : ''))
  const [originMaterials, setOriginMaterials] = useState(listOf(item.origin && item.origin.materials))
  const materials = listOf(ws.materials && ws.materials.materials)
  const withOrigin = prov === 'sa_synthetic' && !narrative
  const body = () => {
    const b = { provenance: prov }
    if (prov === 'customer_confirmed') { b.confirmedBy = by.trim(); b.confirmationRef = ref.trim(); if (materialId) b.materialId = materialId }
    else if (withOrigin && originKind) b.origin = { kind: originKind, ...(originMaterials.length ? { materials: originMaterials } : {}) }
    return b
  }
  const valid = prov === 'customer_confirmed' ? by.trim() && ref.trim()
    : !withOrigin || ((originKind !== 'customer_material' || originMaterials.length > 0) && (kind !== 'workshop' || originKind))
  const busy = ws.busy === `confirm:${item.id}`
  return h('div', { className: 'wc-col wc-mt1', style: { gap: 6, padding: 10, background: 'var(--wc-bg)', borderRadius: 8 } },
    h(Seg, { value: prov, onChange: setProv, options: narrative ? [['sa_synthetic', 'SA 合成']] : [['customer_confirmed', '客户确认'], ['sa_synthetic', 'SA 合成']] }),
    prov === 'customer_confirmed' ? h('div', { className: 'wc-g2e' },
      h(Field, { label: '确认人（姓名、角色）' }, h(TextInput, { value: by, onChange: setBy })),
      h(Field, { label: '凭证（会议纪要、邮件、工单号）' }, h(TextInput, { value: ref, onChange: setRef }))) : null,
    prov === 'customer_confirmed' && materials.length ? h(Field, { label: '依据的客户资料（可选）' }, h(Select, { value: materialId, onChange: setMaterialId, label: '资料', options: [['', '—'], ...materials.map((m) => [m.id, m.name])] })) : null,
    withOrigin ? h(Field, { label: kind === 'workshop' ? '来源（客户 Workshop 必填）' : '来源（可选）' },
      h(Seg, { value: originKind, onChange: setOriginKind, options: [...(kind === 'workshop' ? [] : [['', '不标']]), ...Object.entries(ORIGIN_LABEL)] })) : null,
    narrative ? h(Note, null, '手册叙述是教学文字，不标来源。') : null,
    withOrigin && originKind === 'customer_material' ? h('div', { className: 'wc-fx' }, materials.length ? materials.map((m) => h(Chip, {
      key: m.id, tone: originMaterials.includes(m.id) ? 'acc' : '', onClick: () => setOriginMaterials(originMaterials.includes(m.id) ? originMaterials.filter((x) => x !== m.id) : [...originMaterials, m.id]),
    }, m.name)) : h(Note, { tone: 'warn' }, '先在 01 上传客户资料')) : null,
    h('div', { className: 'wc-fx' },
      h(Button, { kind: 'primary', small: true, disabled: !valid || locked, busy, onClick: () => ws.confirmItem(item.id, body()).then((r) => { if (r) onDone() }) }, prov === 'customer_confirmed' ? '记录客户确认' : '记为 SA 合成'),
      h(Button, { small: true, onClick: onDone }, '取消')),
    h(ErrorLine, { error: ws.errors[`confirm:${item.id}`] }))
}

function ItemsTable() {
  const ws = useWs()
  const list = listOf(ws.items && ws.items.items)
  const [kindFilter, setKindFilter] = useState('all')
  const reviewed = (i) => i.provenance === 'customer_confirmed' || i.provenance === 'sa_synthetic'
  const cases = Object.fromEntries(goldenOf(ws.sc).map((c) => [c.id, c]))
  const facts = Object.fromEntries(listOf(ws.sc && ws.sc.facts).map((f) => [f.id, f]))
  const [chosenState, setState] = useState(null)
  const state = chosenState || (list.some((i) => !reviewed(i)) ? 'pending' : 'all')
  const [open, setOpen] = useState(null)
  const shown = list.filter((i) => (kindFilter === 'all' || i.kind === kindFilter) && (state === 'all' || (state === 'pending' ? !reviewed(i) : reviewed(i))))
  const kinds = [...new Set(list.map((i) => i.kind))]
  return h(Card, { title: '逐项审核', extra: h('span', { className: 'wc-xs wc-mut' }, `${shown.length} / ${list.length} 项`) },
    h('div', { className: 'wc-rf' },
      h(Seg, { value: kindFilter, onChange: setKindFilter, options: [['all', '全部'], ...kinds.map((k) => [k, `${ITEM_KIND_LABEL[k] || k} ${list.filter((i) => i.kind === k).length}`])] }),
      h(Seg, { value: state, onChange: setState, options: [['pending', '待审核'], ['done', '已审核'], ['all', '全部']] })),
    shown.length ? h('div', { className: 'wc-mt1' }, shown.map((i) => h('div', { key: `${i.kind}:${i.id}`, className: 'wc-list-row', style: { flexDirection: 'column', alignItems: 'stretch' } },
      h('div', { className: 'wc-rf wc-top-al' },
        h('div', { className: 'wc-grow' },
          h('div', { className: 'wc-fx' }, h(Chip, null, ITEM_KIND_LABEL[i.kind] || i.kind), h('span', { className: 'wc-mono wc-xs wc-b' }, i.id),
            i.category ? h(Chip, null, CATEGORY_LABEL[i.category] || i.category) : null, i.set ? h(Chip, { tone: i.set === 'holdout' ? 'warn' : '' }, SET_LABEL[i.set] || i.set) : null,
            i.noise ? h(Chip, { tone: 'acc' }, '文末有干扰问答') : null, i.criticality === 'blocking' ? h(Chip, null, '阻断项') : null,
            i.kind === 'golden' ? listOf(cases[i.id] && cases[i.id].basis).map((fid) => h(Chip, { key: fid, tone: facts[fid] && facts[fid].provenance === 'customer_confirmed' ? 'ok' : '', title: facts[fid] ? pick(PROVENANCE_LABEL, facts[fid].provenance)[1] : '规则不存在' }, `依据：规则 ${fid}`)) : null),
          i.label ? h('div', { className: 'wc-sm wc-mt1 wc-break' }, i.label) : null),
        h('div', { className: 'wc-fx', style: { flexShrink: 0 } },
          h(Chip, { tone: pick(CLASS_LABEL, i.class)[0], title: `来源状态：${pick(PROVENANCE_LABEL, i.provenance)[1]}` }, pick(CLASS_LABEL, i.class)[1]),
          i.class === 'draft' && i.provenance === 'customer_confirmed' ? h(Chip, { tone: 'bad' }, '确认像是模拟的') : null,
          i.origin ? h(Chip, null, `${i.provenance === 'ai_draft' || i.provenance === 'pending' ? 'Kiro 标的来源' : '来源'}：${ORIGIN_LABEL[i.origin.kind] || i.origin.kind}`) : null,
          i.confirmable && i.provenance !== 'customer_confirmed' ? h(Button, { small: true, onClick: () => setOpen(open === i.id ? null : i.id) }, open === i.id ? '收起' : '确认…') : null)),
      open === i.id ? h(ConfirmItemForm, { item: i, onDone: () => setOpen(null), locked: holdingResult(ws) }) : null))) : h(Empty, null, state === 'pending' ? '没有待审核的内容。' : '没有内容。'))
}

function ReviewTab() {
  const ws = useWs()
  return h('div', { className: 'wc-col' }, h(ResourceError, { name: 'items', hint: '内容列表读不到' }),
    holdingResult(ws) ? h('div', { className: 'wc-err' }, holdText(ws)) : null, h(ClassSummaryCard), h(AnchorsCard), h(BatchReviewCard), h(ItemsTable), ws.items && ws.items.packKind === 'customer' ? h(Note, null, '客户严格确认：阻断项只能逐项记录客户确认；SA 合成不能替代客户确认。') : null)
}

function FactsTab() {
  const ws = useWs()
  const facts = listOf(ws.sc && ws.sc.facts)
  const golden = goldenOf(ws.sc)
  const classes = Object.fromEntries(listOf(ws.items && ws.items.items).map((i) => [`${i.kind}:${i.id}`, i.class]))
  return h(Card, { title: '业务规则', extra: h('span', { className: 'wc-xs wc-mut' }, `${facts.length} 条`) },
    h(Table, {
      head: ['规则', '重要性', '类别', '测它的题'],
      rows: facts.map((f) => ({
        key: f.id,
        cells: [h('div', null, h('div', { className: 'wc-break' }, f.statement), h('div', { className: 'wc-xs wc-mut wc-mono' }, f.id), f.source ? h('div', { className: 'wc-xs wc-mut' }, `出处：${f.source}`) : null),
          h(Chip, { tone: (f.criticality || 'blocking') === 'blocking' ? 'warn' : '' }, (f.criticality || 'blocking') === 'blocking' ? '阻断' : '参考'),
          h(ToneChip, { map: CLASS_LABEL, value: classes[`fact:${f.id}`] }),
          h('span', { className: 'wc-xs wc-break', title: golden.filter((c) => listOf(c.basis).includes(f.id)).map((c) => c.id).join(' · ') }, golden.filter((c) => listOf(c.basis).includes(f.id)).map((c) => c.label || c.id).join(' · ') || '—')],
      })),
      empty: '还没有业务规则。',
    }))
}

function FileView({ rel, label }) {
  const ws = useWs()
  const [state, setState] = useState(null)
  const toggle = async () => {
    if (state) { setState(null); return }
    try { setState({ text: (await ws.readFile(rel)).content }) } catch (err) { setState({ error: apiError(err) }) }
  }
  return h(Fragment, null,
    h(Button, { small: true, kind: 'ghost', onClick: toggle }, state ? '收起' : label || '看内容'),
    state ? (state.error ? h(ErrorLine, { error: state.error }) : h(Pre, null, state.text)) : null)
}

function KnowledgeTab() {
  const ws = useWs()
  const kb = (ws.sc && ws.sc.knowledge) || {}
  const docs = listOf(kb.documents)
  const classes = Object.fromEntries(listOf(ws.items && ws.items.items).map((i) => [`${i.kind}:${i.id}`, i.class]))
  return h(Card, { title: '知识文档', extra: h('span', { className: 'wc-xs wc-mut' }, `${docs.length} 份 · 检索工具 ${(ws.sc && ws.sc.evaluation && ws.sc.evaluation.retrievalToolName) || '—'}`) },
    docs.length ? docs.map((d, i) => h('div', { key: d.id, className: cx('wc-it', i && 'wc-ln') },
      h('div', { className: 'wc-rf wc-top-al' },
        h('div', { className: 'wc-grow' }, h('div', { className: 'wc-b' }, d.title || d.id), h('div', { className: 'wc-xs wc-mut wc-mono' }, d.file)),
        h('span', { className: 'wc-fx' }, d.noise ? h(Chip, { tone: 'acc' }, '文末有干扰问答') : null, h(ToneChip, { map: CLASS_LABEL, value: classes[`document:${d.id}`] }),
          d.origin ? h(Chip, null, `${d.provenance === 'ai_draft' || d.provenance === 'pending' ? 'Kiro 标的来源' : '来源'}：${ORIGIN_LABEL[d.origin.kind] || d.origin.kind}`) : null)),
      h(FileView, { rel: d.file }))) : h(Empty, null, '还没有知识文档。'),
    kb.noisePlan ? h(Note, null, `干扰设计：${kb.noisePlan.enabled ? '开启' : '关闭'}。${kb.noisePlan.rationale || ''}`) : null)
}

function ToolsTab() {
  const ws = useWs()
  const tools = listOf(ws.sc && ws.sc.tools)
  const skills = listOf(ws.sc && ws.sc.skills)
  const classes = Object.fromEntries(listOf(ws.items && ws.items.items).map((i) => [`${i.kind}:${i.id}`, i.class]))
  return h('div', { className: 'wc-col' },
    h(Card, { title: '工具', extra: h('span', { className: 'wc-xs wc-mut' }, `${tools.length} 个`) },
      tools.length ? tools.map((t, i) => {
        const props = Object.keys((t.inputSchema && t.inputSchema.properties) || {})
        const required = listOf(t.inputSchema && t.inputSchema.required)
        const fixtures = t.fixtures || {}
        return h('div', { key: t.name, className: cx('wc-it', i && 'wc-ln') },
          h('div', { className: 'wc-rf' }, h('span', { className: 'wc-b wc-mono' }, t.name),
            h('span', { className: 'wc-fx' }, h(Chip, { tone: t.kind === 'retrieval' ? 'info' : '' }, { retrieval: '检索工具', mock: '模拟工具' }[t.kind] || t.kind || '工具'), h(ToneChip, { map: CLASS_LABEL, value: classes[`tool:${t.name}`] }))),
          h('div', { className: 'wc-sm wc-mut wc-mt1' }, t.description),
          h('div', { className: 'wc-xs wc-mt1' }, '输入：', props.length ? props.map((p) => h(Chip, { key: p, tone: required.includes(p) ? 'acc' : '' }, p + (required.includes(p) ? ' *' : ''))) : '—',
            // The fixtures' provenance is the tool's own class (the chip above), not assumed synthetic.
            listOf(fixtures.cases).length || listOf(fixtures.errors).length ? h('span', { className: 'wc-mut' }, ` · 模拟返回 ${listOf(fixtures.cases).length} 种${fixtures.default ? ' + 默认返回' : ''}${listOf(fixtures.errors).length ? ` · 报错 ${listOf(fixtures.errors).length} 种` : ''}`) : null))
      }) : h(Empty, null, '还没有工具。')),
    h(Card, { title: 'Skills', extra: h('span', { className: 'wc-xs wc-mut' }, `${skills.length} 个`) },
      skills.length ? skills.map((s, i) => h('div', { key: s.name, className: cx('wc-it', i && 'wc-ln') },
        h('div', { className: 'wc-rf' }, h('span', { className: 'wc-b wc-mono' }, s.name), h('span', { className: 'wc-xs wc-mut wc-mono' }, s.file)),
        s.description ? h('div', { className: 'wc-sm wc-mut' }, s.description) : null, h(FileView, { rel: s.file }))) : h(Empty, null, '没有 Skills。')))
}

// The engine checks every baseline defect with teaching.contains_term (NFKC, casefold, markdown marks
// dropped, whitespace collapsed, word boundaries): teaching_policy reports teaching.defect_* findings.
// Read them from the fresh validation, by defect id; null when there is no fresh policy run.
const DEFECT_EXTRA = { 'teaching.defect_already_fixed': '基线里已经有这条修正，基线没有这个弱点', 'teaching.defect_marker_kept': '候选里还留着基线这句' }
function defectChecks(view) {
  const v = view && view.validation
  if (!v || !view.fresh || listOf(v.schema).length) return null
  const out = {}
  for (const f of listOf(v.policy)) {
    const m = /^labs\.teaching\.baselineDefects\.(.+)$/.exec(String(f.path || ''))
    if (m && String(f.code || '').startsWith('teaching.defect_')) (out[m[1]] = out[m[1]] || []).push(f.code)
  }
  return out
}

function PromptTab() {
  const ws = useWs()
  const prompts = (ws.sc && ws.sc.prompts) || {}
  const t = teachingOf(ws.sc)
  const [texts, setTexts] = useState({})
  useEffect(() => {
    let live = true
    for (const [key, rel] of [['baseline', prompts.baselineFile], ['candidate', prompts.optimizationCandidateFile]]) {
      if (!rel) continue
      ws.readFile(rel).then((r) => { if (live) setTexts((prev) => ({ ...prev, [key]: r.content })) }).catch((err) => { if (live) setTexts((prev) => ({ ...prev, [key]: `读取失败：${apiError(err).message}` })) })
    }
    return () => { live = false }
  }, [prompts.baselineFile, prompts.optimizationCandidateFile, ws.scenarioView && ws.scenarioView.sha256])
  // ✓ / ✕ are the fresh validation's teaching.defect_* findings, never a client-side text match.
  const checks = defectChecks(ws.validationView)
  const mark = (did, missingCode) => (checks === null ? h('span', { className: 'wc-mut', title: '校验后显示' }, '—')
    : listOf(checks[did]).includes(missingCode) ? h('span', { className: 'wc-tx-bad' }, '✕') : h('span', { className: 'wc-tx-ok' }, '✓'))
  const extra = (did) => listOf(checks && checks[did]).map((code) => DEFECT_EXTRA[code]).filter(Boolean)
  const defects = listOf(t && t.baselineDefects)
  return h('div', { className: 'wc-col' },
    h(Card, { title: '弱基线与优化候选', sub: `基线里故意写弱的每一处，都要能被某道练习题暴露；Guided Run「${STEP_LABEL.optimize}」那一步换成候选再评。✓ / ✕ 取自${checks === null ? '校验（内容改过，重新校验后显示）' : '最新校验，和构建门禁用同一套匹配规则'}。` },
      h(Table, {
        head: ['基线的弱点（教学设计）', '基线里这句', '候选改成', '候选含修正', '现象'],
        rows: defects.map((d) => ({
          key: d.id,
          cells: [h('div', null, h('div', null, d.description), h('div', { className: 'wc-xs wc-mut wc-mono' }, d.id),
            extra(d.id).map((text) => h('div', { key: text, className: 'wc-xs wc-tx-bad' }, text))),
            d.baselineMarker ? h('div', null, h('div', { className: 'wc-xs wc-mut' }, `“${d.baselineMarker}”`), mark(d.id, 'teaching.defect_marker_missing')) : h('span', { className: 'wc-mut' }, '没有对应句子（基线缺这条规则）'),
            d.candidateFix || h('span', { className: 'wc-mut' }, '未写'), d.candidateFix ? mark(d.id, 'teaching.defect_fix_missing') : h('span', { className: 'wc-mut' }, '—'),
            h('span', { className: 'wc-xs wc-mono' }, phenomenaOf(ws.sc).filter((p) => listOf(p.defectIds).includes(d.id)).map((p) => p.id).join(' · ') || '—')],
        })),
        empty: '还没有声明基线弱点（labs.teaching.baselineDefects）。',
      })),
    h('div', { className: 'wc-g2e' },
      h(Card, { title: '基线 Prompt', extra: h('span', { className: 'wc-xs wc-mut wc-mono' }, prompts.baselineFile || '—') }, h(Pre, { tall: true }, texts.baseline ?? '…')),
      h(Card, { title: '优化候选', extra: h('span', { className: 'wc-xs wc-mut wc-mono' }, prompts.optimizationCandidateFile || '—') }, h(Pre, { tall: true }, texts.candidate ?? '…'))))
}

function GoldenTab() {
  const ws = useWs()
  const [instructor, setInstructor] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => {
    let live = true
    ws.golden(instructor).then((r) => { if (live) { setData(r); setError(null) } }).catch((err) => { if (live) setError(apiError(err)) })
    return () => { live = false }
  }, [instructor, ws.scenarioView && ws.scenarioView.sha256])
  const cases = data ? [...listOf(data.practice), ...(instructor ? listOf(data.holdout) : [])] : []
  const classes = Object.fromEntries(listOf(ws.items && ws.items.items).map((i) => [`${i.kind}:${i.id}`, i.class]))
  return h(Card, {
    title: '题目与判据',
    extra: data ? [
      h(Chip, { key: 'p' }, `练习 ${listOf(data.practice).length} · 留出 ${data.holdoutCount}`),
      ...Object.entries(data.categories || {}).map(([k, n]) => h(Chip, { key: k }, `${CATEGORY_LABEL[k] || k} ${n}`)),
      h(Button, { key: 'b', small: true, onClick: () => setInstructor(!instructor) }, instructor ? '隐藏留出题' : '显示留出题（导师）'),
    ] : null,
  },
  h(ErrorLine, { error }),
  instructor && data && data.warning ? h(Note, { tone: 'bad' }, '留出题只给导师：不要贴到学员材料、幻灯片或聊天里。') : null,
  h(Table, {
    head: ['问题', '角色', '集合', '期望与判据', '现象', '类别'],
    rows: cases.map((c) => ({
      key: c.id,
      cells: [h('div', null, h('div', { className: 'wc-break' }, c.query), h('div', { className: 'wc-xs wc-mut' }, `${c.label} · `, h('span', { className: 'wc-mono' }, c.id))),
        h('span', { className: 'wc-xs wc-mut', title: c.roleId || c.actorId }, roleName(ws.sc, c.roleId) || c.actorId),
        h('div', null, h(Chip, { tone: c.set === 'holdout' ? 'warn' : '' }, SET_LABEL[c.set] || c.set), h('div', { className: 'wc-xs wc-mut wc-mt1' }, CATEGORY_LABEL[c.category] || c.category)),
        h(ChipList, { chips: expectationChips(c.expected) }),
        h('span', { className: 'wc-xs wc-mono' }, phenomenaOfCase(ws.sc, c.id).join(' · ') || '—'),
        h(ToneChip, { map: CLASS_LABEL, value: classes[`golden:${c.id}`] })],
    })),
    empty: data ? '还没有题目。' : '加载中…',
  }),
  h(Note, null, '留出题的期望只在导师侧使用，不下发到学员环境，也从不拿来调 Prompt。'))
}

function ContentStep({ tab, onTab }) {
  const ws = useWs()
  const sc = ws.sc
  const list = listOf(ws.items && ws.items.items)
  const pending = list.filter((i) => i.provenance === 'ai_draft' || i.provenance === 'pending').length
  const options = [
    { value: 'eval', label: '教学与评估' },
    { value: 'review', label: '审核', count: pending ? `${pending} 待审` : null },
    { value: 'facts', label: '业务规则', count: listOf(sc && sc.facts).length },
    { value: 'kb', label: '知识文档', count: listOf(sc && sc.knowledge && sc.knowledge.documents).length },
    { value: 'tools', label: '工具与 Skills', count: listOf(sc && sc.tools).length },
    { value: 'prompt', label: 'Prompt' },
    { value: 'golden', label: '题目与判据', count: goldenOf(sc).length },
    { value: 'guides', label: '学员与导师手册' },
  ]
  const current = options.some((o) => o.value === tab) ? tab : 'eval'
  const body = { eval: TeachingEvalTab, review: ReviewTab, facts: FactsTab, kb: KnowledgeTab, tools: ToolsTab, prompt: PromptTab, golden: GoldenTab, guides: GuidesCard }[current]
  return h(Fragment, null,
    h(StepHeader, { n: '02', title: '内容与评估', h1: '先看评估设计对不对，再按类别审核内容', sub: '每项内容都标：来源、确认状态和类别（客户事实 / 合成教学设定 / 教学设计 / 草稿）。' }),
    ws.scenarioView && ws.scenarioView.error ? h('div', { className: 'wc-err wc-mt' }, ws.scenarioView.error) : null,
    // While a draft waits or runs in 01, these tabs still show the project as it is (教学与评估 says so in its own card).
    (pendingDraft(ws) || draftRunning(ws)) && current !== 'eval'
      ? h(Note, { tone: 'warn' }, `下面是项目当前的内容，不是 01 里${draftRunning(ws) ? '正在生成' : '待应用'}的 Kiro 草稿；${replaceText(ws.generation)}。`) : null,
    h('div', { className: 'wc-mt' }, h(Tabs, { value: current, onChange: onTab, options })),
    h('div', { className: 'wc-mt' }, h(body)),
    h(StepFoot, { back: ['1', '← 回到场景与课程'], next: ['3', '去交付 →'] }))
}

// ---------------------------------------------------------------------------
// 03 交付
// ---------------------------------------------------------------------------

function TargetForm() {
  const ws = useWs()
  const t = ws.project.target || {}
  const initial = () => ({ profile: t.profile || '', region: t.region || 'us-west-2', expectedAccountId: t.expected_account_id || '', workshopStack: t.workshop_stack || 'workshop-infra', addonsStack: t.addons_stack || 'workshop-customizer-addons', releaseProject: t.releaseProject || ws.pid })
  const [form, setForm] = useState(initial)
  const [edit, setEdit] = useState(!ws.project.target)
  const set = (k) => (v) => setForm({ ...form, [k]: v })
  if (!edit) {
    return h('div', { className: 'wc-rf' },
      h('span', { className: 'wc-sm' }, h('span', { className: 'wc-mono' }, t.profile), ` · ${t.expected_account_id} · ${t.region} · ${t.workshop_stack} / ${t.addons_stack}`),
      h(Button, { small: true, onClick: () => { setForm(initial()); setEdit(true) } }, '修改'))
  }
  return h('div', { className: 'wc-col', style: { gap: 6 } },
    h('div', { className: 'wc-g2e' },
      h(Field, { label: 'AWS CLI profile（已登录的 SSO / 临时角色）' }, h(TextInput, { value: form.profile, onChange: set('profile'), mono: true })),
      h(Field, { label: '区域' }, h(TextInput, { value: form.region, onChange: set('region'), mono: true }))),
    h('div', { className: 'wc-g2e' },
      h(Field, { label: '应当是这个账号（12 位）' }, h(TextInput, { value: form.expectedAccountId, onChange: set('expectedAccountId'), mono: true })),
      h(Field, { label: 'Release 目录名（S3）' }, h(TextInput, { value: form.releaseProject, onChange: set('releaseProject'), mono: true }))),
    h('div', { className: 'wc-g2e' },
      h(Field, { label: '基础环境 stack' }, h(TextInput, { value: form.workshopStack, onChange: set('workshopStack'), mono: true })),
      h(Field, { label: '定制组件 stack' }, h(TextInput, { value: form.addonsStack, onChange: set('addonsStack'), mono: true }))),
    h('div', { className: 'wc-fx' },
      h(Button, { kind: 'primary', busy: ws.busy === 'target', onClick: () => ws.putTarget(form).then((r) => { if (r) setEdit(false) }) }, '保存'),
      ws.project.target ? h(Button, { onClick: () => setEdit(false) }, '取消') : null),
    h(ErrorLine, { error: ws.errors.target }),
    h(Note, null, '不存任何凭证：只记 profile 名，调用时才解析凭证。'),
    h(Note, null, '前提：Workshop 账号里已部署定制组件 stack（sync/cfn/customizer-addons.json，由运维部署），并把它的 sync 托管策略加到你的临时角色上。'))
}

function PreflightResult({ pf }) {
  if (!pf || pf.unreachable) return null // the error line above says why it could not run
  const plan = pf.plan || {}
  return h('div', { className: 'wc-mt1' },
    listOf(pf.checks).map((c, i) => h('div', { key: c.name || i, className: cx('wc-fx wc-it', i && 'wc-ln'), style: { flexWrap: 'nowrap', alignItems: 'flex-start' } },
      h('span', { className: c.status === 'ok' ? 'wc-tx-ok' : c.status === 'skip' ? 'wc-mut' : 'wc-tx-bad', style: { width: 14, flexShrink: 0 } }, c.status === 'ok' ? '✓' : c.status === 'skip' ? '–' : '✕'),
      h('span', { className: 'wc-grow' }, h('span', { className: 'wc-mono wc-xs' }, c.name), h('div', { className: 'wc-sm wc-mut wc-break' }, c.detail)))),
    h(Note, null, `账号 ${plan.accountId || '—'} · 区域 ${plan.region || '—'} · 实例 ${plan.instanceId || '—'} · 文档 ${plan.documentName || '—'} · ${fmtTime(pf.at)}`))
}

function TargetCard() {
  const ws = useWs()
  const pf = ws.preflight
  return h(Card, { title: 'Workshop 环境', extra: pf ? h(Chip, { tone: pf.ok ? 'ok' : 'bad' }, pf.ok ? '体检通过' : '体检未通过') : h(Chip, null, '未体检') },
    h(TargetForm),
    h('div', { className: 'wc-fx wc-mt' },
      h(Button, { disabled: !ws.project.target || !ws.project.lastBuild, busy: ws.busy === 'preflight', onClick: ws.runPreflight, title: ws.project.lastBuild ? '' : '先构建 Release' }, '只读体检'),
      !ws.project.lastBuild ? h(Button, { small: true, kind: 'ghost', onClick: scrollToBuildCard }, '体检对照当前 Release：先构建 ↓') : null),
    h(ErrorLine, { error: ws.errors.preflight }),
    h(PreflightResult, { pf }),
    h(Note, null, '体检只读：身份与账号、凭证有效期、两个 stack、模板版本、实例在线、场景资源还没创建。什么都不上传。'))
}

function OneClickCard() {
  const ws = useWs()
  const p = ws.project
  const job = ws.oneclick
  const active = jobActive(job)
  const [ack, setAck] = useState(false)
  useEffect(() => { if (!active) setAck(false) }, [active])
  const lv = p.lastValidation
  const drafts = listOf(ws.items && ws.items.items).filter((i) => i.provenance === 'ai_draft' || i.provenance === 'pending').length
  const strict = ws.items && ws.items.packKind === 'customer' ? listOf(ws.items.items).filter(needsCustomer).length : 0
  const vNow = ws.validationView && ws.validationView.validation
  const held = lv && vNow && vNow.at === lv.at ? `还差${gateLeft(vNow, ws.items)}`
    : lv ? pendingText(Math.max(0, (lv.gate || 0) - (listOf(lv.anchorsMissing).length ? 1 : 0)), listOf(lv.anchorsMissing).length) : ''
  const gates = [
    ['校验', !lv ? ['warn', '还没校验 · 任务里会校验'] : lv.ok ? ['ok', '通过']
      : (lv.schema || 0) + (lv.policyErrors || 0) + (lv.outputErrors || 0) + (lv.renderErrors || 0) + (lv.workspaceErrors || 0)
        ? ['bad', `有 ${(lv.schema || 0) + (lv.policyErrors || 0) + (lv.outputErrors || 0) + (lv.renderErrors || 0) + (lv.workspaceErrors || 0)} 个内容错误 · 构建会拦`]
        : ['warn', `内容没有错误；${held} · 构建会拦`]],
    ['审核', holdingResult(ws) ? ['warn', `Kiro ${(ws.generation || {}).mode === 'repair' ? '修复结果' : '草稿'}${isLive(ws.generation) ? '正在生成' : '待应用'}：先处理它，再审核`]
      : drafts ? ['warn', `${drafts} 项待审核${strict ? '，阻断项要客户确认' : ''} · 构建会拦`]
        : strict ? ['warn', `${strict} 项阻断项待客户确认 · 构建会拦`] : goldenOf(ws.sc).length ? ['ok', '没有待审核'] : ['', '还没有内容']],
    ['构建', p.lastBuild ? ['ok', p.lastBuild.version] : ['', '任务里构建']],
    ['目标环境', !p.target ? ['bad', '未配置'] : ws.preflight ? (ws.preflight.ok ? ['ok', '体检通过'] : ['bad', '体检未通过']) : ['', `${p.target.expected_account_id} · ${p.target.region}`]],
  ]
  const stages = job ? listOf(job.stages) : Object.keys(ONECLICK_STAGE).map((id) => ({ id, status: 'pending' }))
  return h(Card, { title: '一键覆盖', extra: job ? h(ToneChip, { map: JOB_STATUS, value: job.status }) : null },
    gates.map(([label, [tone, text]], i) => h('div', { key: label, className: cx('wc-rf wc-it', i && 'wc-ln') }, h('span', null, label), h(Chip, { tone }, text))),
    p.target ? h('div', { className: 'wc-mt1' }, h(Check, { checked: ack, disabled: active, onChange: setAck }, `确认这是正确的 Workshop 账号（${p.target.expected_account_id} · ${p.target.region}），场景的 AWS 资源还没创建。`)) : null,
    h('div', { className: 'wc-mt1' }, h(Button, { kind: 'primary', icon: Rocket, disabled: !p.target || !ack || active, busy: ws.busy === 'oneclick' || active, onClick: ws.startOneclick }, active ? '一键覆盖进行中…' : '一键覆盖到这套环境')),
    h(ErrorLine, { error: ws.errors.oneclick }),
    h('div', { className: 'wc-steps wc-mt1' }, stages.map((s, i) => h(Chip, { key: s.id, tone: pick(JOB_STATUS, s.status)[0], title: s.error || detailText(s.detail) || s.note || '' }, `${i + 1} ${ONECLICK_STAGE[s.id] || s.id}${s.status !== 'pending' ? ` · ${pick(JOB_STATUS, s.status)[1]}` : ''}`))),
    job ? h('div', { className: 'wc-mt1' }, stages.filter((s) => s.error || s.detail || s.note).map((s) => h('div', { key: s.id, className: 'wc-xs wc-break' },
      h('span', { className: 'wc-b' }, `${ONECLICK_STAGE[s.id] || s.id}：`), h('span', { className: s.error ? 'wc-tx-bad' : 'wc-mut', title: s.error ? asText(s.error) : undefined }, s.error ? backendText(asText(s.error)) || asText(s.error) : s.note ? stageNote(s.note) : detailText(s.detail))))) : null,
    job && job.error && !stages.some((s) => asText(s.error) === asText(job.error)) ? h('div', { className: 'wc-err', title: asText(job.error) }, backendText(asText(job.error)) || asText(job.error)) : null,
    job && job.status === 'succeeded' && job.result ? h(Note, { tone: 'ok' }, `Release ${asText(job.result.version)} 已应用${job.result.live ? '并确认保留' : ''}；去 04 从「${STEP_LABEL[job.result.nextStep] || job.result.nextStep || '—'}」开始彩排。`) : null,
    job && job.status === 'failed' ? h(Note, { tone: 'warn' }, '停在第一个失败的阶段，后面的阶段都没运行。修好原因后再点一键覆盖：内容没变时会复用已有构建，远端已是这个版本时应用会跳过。') : null,
    job && job.status === 'interrupted' ? h(Note, { tone: 'warn' }, '后端在任务运行中重启过，远端做到哪一步不知道。先到“高级 → 分步同步与回滚”做只读体检、再点“查看状态”，确认远端版本后再重新开始。') : null,
    ws.lost.oneclick ? h(Note, { tone: 'warn' }, '和后端失联：任务还在服务端运行，点顶部“刷新”继续跟进。') : null,
    h(Note, null, '一个后台任务：校验 → 构建 → 预检 → 上传并应用 → 核对 → 确认保留 → 绑定 Guided Run。失败会停在那一步；预检不过就什么都不上传。“确认保留”是结束远端恢复等待期，不是 Git 提交。需要分步执行或回滚，去“高级”。'))
}

const BUILD_CARD_ID = 'wc-build-card'
const scrollToBuildCard = () => {
  const el = document.getElementById(BUILD_CARD_ID)
  if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
function BuildCard() {
  const ws = useWs()
  const b = ws.project.lastBuild
  const rel = ws.release
  const relError = ws.res.release && ws.res.release.error
  const refused = ws.errors.build && ws.errors.build.data && ws.errors.build.data.validation
  const lv = ws.project.lastValidation // a newer validation (the review finished since) or none (reset) retires the refusal
  const outdated = !!(refused && (!lv || (lv.at && refused.at && lv.at > refused.at)))
  const buildError = outdated ? null : ws.errors.build
  const validation = outdated ? null : refused
  const vv = ws.validationView // the current validation already says whether a build would pass
  const held = vv && vv.fresh && vv.validation && !vv.validation.ok
    ? (contentErrorCount(vv.validation) ? `校验有 ${contentErrorCount(vv.validation)} 个内容错误` : `还差${gateLeft(vv.validation, ws.items)}`) : ''
  return h(Card, { id: BUILD_CARD_ID, title: '构建 Release', extra: b ? h(Chip, { tone: rel ? 'ok' : 'warn' }, rel ? '与当前内容一致' : '需要重新构建') : h(Chip, null, '未构建') },
    b ? h(Kpi, null,
      h(Stat, { label: '版本', small: true, value: h('span', { className: 'wc-mono' }, b.version) }),
      h(Stat, { label: '文件', value: b.files }),
      h(Stat, { label: '内容哈希', small: true, value: h('span', { className: 'wc-mono' }, shortHash(b.contentHash)) }),
      h(Stat, { label: '构建时间', small: true, value: fmtTime(b.at) })) : h(Empty, null, '校验通过后构建：编译 Scenario Pack，渲染 15 步 Release，并生成学员和导师手册。'),
    rel && rel.build ? h(Note, null, `模板 ${shortHash(rel.build.templateCommit)} · ${rel.fileCount} 个文件 · 补丁 ${rel.build.patches ?? '—'} 处`) : null,
    b && relError ? h(Note, { tone: 'warn' }, relError.message) : null,
    h('div', { className: 'wc-fx wc-mt' }, h(Button, { kind: b || held ? 'default' : 'primary', busy: ws.busy === 'build', onClick: ws.build }, b ? '重新构建' : '构建 Release'),
      held && !validation ? h('span', { className: 'wc-xs wc-tx-warn' }, `现在构建会被拦：${held}`) : null),
    h(ErrorLine, { error: buildError }),
    validation ? h('div', { className: 'wc-rf wc-mt1' }, h('span', { className: 'wc-sm' }, contentErrorCount(validation) ? `校验有 ${contentErrorCount(validation)} 个内容错误。` : `内容没有错误；还差${gateLeft(validation, ws.items)}。`), h(Button, { small: true, onClick: () => ws.go('2', 'eval') }, '去看校验结果 →')) : null)
}

// The 03 校验 row: the items left to review and the missing anchors, leaving out whichever is none.
const pendingText = (review, anchors) => review ? `${review} 项待审核${anchors ? '，另缺客户锚点' : ''}` : anchors ? '缺客户锚点' : '还有待处理项（见「02」校验结果）'
// What the gate still holds, by who acts: the SA's review, the customer's own confirmation (every blocking item
// of a 客户严格确认 pack), the customer anchors of a workshop pack.
function gateLeft(v, items) {
  const gate = listOf(v.gate)
  const need = new Set(items && items.packKind === 'customer' ? listOf(items.items).filter(needsCustomer).map((i) => i.id) : [])
  const customer = (g) => CUSTOMER_GATE.test(String(g.reason || '')) || need.has(g.item_id)
  return [gate.some((g) => g.item_type !== 'golden set' && !customer(g)) ? '审核' : null, gate.some(customer) ? '客户确认' : null,
    gate.some((g) => g.item_type === 'golden set') ? '客户锚点' : null].filter(Boolean).join('和') || '审核'
}

function ExportCard() {
  const ws = useWs()
  const built = !!ws.release
  const rows = [
    [['运行包', 'Release zip'], '可运行的 15 步文件树、运行清单、学员手册 README.md', '留出题、导师材料、私有来源', h(LinkButton, { href: LINKS.release(ws.pid), disabled: !built }, '导出')],
    [['学员手册', 'README.md'], 'Release 根目录那份 README.md', '导师答案、留出题', h(LinkButton, { href: LINKS.guideDownload(ws.pid, 'student'), disabled: !built }, '下载')],
    [['导师包', '只给导师'], '导师手册、留出题与期望、来源报告、彩排证据（有就带）', '任何 Release 文件', h(LinkButton, { href: LINKS.instructorBundle(ws.pid), disabled: !built }, '导出')],
  ]
  return h(Card, { title: '导出', sub: '每次导出都按当前构建重新核对文件哈希；源文件改过就要先重新构建。' },
    h(Table, { head: ['给谁', '包含', '不包含', ''], rows: rows.map(([[a, sub], b, c, d]) => [h('div', { style: { whiteSpace: 'nowrap' } }, h('div', { className: 'wc-b' }, a), h('div', { className: 'wc-xs wc-mut' }, sub)), b, h('span', { className: 'wc-mut' }, c), d]) }),
    h(Note, { tone: 'bad' }, '导师包含留出题和期望答案，不要发给学员。'))
}

function GuidesCard() {
  const ws = useWs()
  const [audience, setAudience] = useState('student')
  const builtVersion = ws.project.lastBuild ? ws.project.lastBuild.version : ''
  const [preview, setPreview] = useState(!builtVersion)
  useEffect(() => { setPreview(!builtVersion) }, [builtVersion])
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    let live = true
    setLoading(true); setError(null)
    ws.guide(audience, preview).then((r) => { if (live) setData(r) }).catch((err) => { if (live) { setData(null); setError(apiError(err)) } }).finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [audience, preview, ws.project.lastBuild && ws.project.lastBuild.version, ws.scenarioView && ws.scenarioView.sha256])
  return h(Card, {
    title: '学员与导师手册',
    extra: [
      h(Seg, { key: 'a', value: audience, onChange: setAudience, options: [['student', '学员手册'], ['instructor', '导师手册']] }),
      h(Seg, { key: 'p', value: preview ? 'draft' : 'built', onChange: (v) => setPreview(v === 'draft'), options: [['built', '已构建'], ['draft', '草稿预览']] }),
    ],
  },
  loading ? h(Note, null, '加载中…') : null,
  error && !goldenOf(ws.sc).length ? h(Empty, null, '还没有内容：先在「01」生成并应用草稿，再看手册。') : h(ErrorLine, { error }),
  data ? h(Fragment, null,
    h('div', { className: 'wc-fx' },
      data.draft ? h(Chip, { tone: 'warn' }, pendingDraft(ws) || draftRunning(ws) ? '草稿预览：按项目当前内容渲染（不是 01 的 Kiro 草稿），未经构建' : '草稿预览：按当前 scenario 渲染，未经构建') : h(Chip, { tone: 'ok' }, `已构建 · ${data.version} · ${data.fileName}`),
      data.rehearsalEvidence ? h(Chip, { tone: 'info' }, '含彩排证据') : null,
      !data.draft ? h(LinkButton, { href: LINKS.guideDownload(ws.pid, audience) }, '下载') : null),
    data.warning ? h(Note, { tone: 'bad' }, '导师手册含留出题和期望答案，不要发给学员。') : null,
    listOf(data.findings).length ? h('div', { className: 'wc-mt1' }, data.findings.map((f, i) => h(FindingRow, { key: i, f }))) : null,
    h(Pre, { tall: true }, stripGuideMarkers(data.markdown))) : null)
}

function DeliveryStep() {
  return h(Fragment, null,
    h(StepHeader, { n: '03', title: '交付', h1: '配置环境 → 构建 → 体检 → 一键覆盖', sub: '体检对照当前 Release，所以先构建；一键覆盖本身也会校验和构建。构建后可以先看手册和导出包。' }),
    h('div', { className: 'wc-g2e wc-mt' }, h(TargetCard), h(OneClickCard)),
    h('div', { className: 'wc-g2e wc-mt' }, h(BuildCard), h(ExportCard)),
    h('div', { className: 'wc-mt' }, h(GuidesCard)),
    h(StepFoot, { back: ['2', '← 回到内容与评估', 'eval'], next: ['4', '去彩排 →'] }))
}

// ---------------------------------------------------------------------------
// 04 彩排与上课
// ---------------------------------------------------------------------------

const RUN_CARD_ID = 'wc-guided-run'
const scrollToRunCard = () => {
  const el = document.getElementById(RUN_CARD_ID)
  if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function StepRow({ id, index, run }) {
  const ws = useWs()
  const s = run.steps[id]
  const [confirmReset, setConfirmReset] = useState(false)
  const [showOutput, setShowOutput] = useState(s.status === 'failed')
  const order = listOf(run.stepOrder)
  const earlierPassed = order.slice(0, index).every((x) => run.steps[x].status === 'passed')
  const canReset = !ws.runningId && s.status !== 'not_started' && earlierPassed && index > 0
  const outputs = s.outputs && Object.keys(s.outputs).length ? s.outputs : null
  const running = s.status === 'running'
  const now = useNow(running)
  const started = s.startedAt ? Date.parse(s.startedAt) : 0
  const meta = [s.commandId, s.ssmStatus, s.documentVersion ? `文档 v${s.documentVersion}` : '', s.durationSeconds != null ? fmtSeconds(s.durationSeconds) : ''].filter(Boolean).join(' · ')
  const times = s.startedAt ? `开始 ${fmtTime(s.startedAt)}${running && started ? ` · 已运行 ${fmtSeconds((now - started) / 1000)}` : ''}${s.finishedAt ? ` · 结束 ${fmtTime(s.finishedAt)}` : ''}` : ''
  return h('div', { className: cx('wc-it wc-steprow', index && 'wc-ln') },
    h('div', { className: 'wc-steprow-h' },
      h('span', { className: 'wc-mut', style: { width: 22, flexShrink: 0 } }, index + 1),
      h('span', { className: 'wc-grow' }, h('span', null, STEP_LABEL[id] || s.label || id), h('span', { className: 'wc-xs wc-mut wc-mono' }, s.script)),
      h('span', { className: 'wc-fx' },
        canReset && !confirmReset ? h('span', { className: 'wc-hover' }, h(Button, { small: true, kind: 'ghost', onClick: () => setConfirmReset(true), title: '归档当前记录，从这一步重跑' }, '从这步重跑')) : null,
        confirmReset ? h(Button, { small: true, kind: 'danger', busy: ws.busy === 'reset', onClick: () => ws.resetRun(id).then(() => setConfirmReset(false)) }, '确认从这步重跑') : null,
        confirmReset ? h(Button, { small: true, onClick: () => setConfirmReset(false) }, '取消') : null,
        s.status === 'failed' ? h(Button, { small: true, busy: ws.busy === 'run', disabled: ws.project.status !== 'synced', onClick: () => ws.runStep(id) }, '重试') : null,
        h(ToneChip, { map: RUN_STATUS, value: s.status }))),
    meta || s.summary || outputs ? h('div', { className: 'wc-fx wc-xs wc-mut', style: { paddingLeft: 22 } },
      h('span', { className: 'wc-mono' }, meta),
      s.summary || outputs ? h('button', { type: 'button', className: 'wc-link', onClick: () => setShowOutput(!showOutput) }, showOutput ? '收起输出' : '输出摘要') : null) : null,
    running && times ? h('div', { className: 'wc-xs wc-tx-info', style: { paddingLeft: 22 } }, times) : null,
    s.error ? h('div', { className: 'wc-err', style: { marginLeft: 22 } }, s.error) : null,
    showOutput && (s.summary || outputs) ? h('div', { style: { paddingLeft: 22 } },
      times && !running ? h('div', { className: 'wc-xs wc-mut wc-mt1' }, times) : null,
      s.summary ? h('div', { className: 'wc-xs wc-mt1', style: { whiteSpace: 'pre-wrap' } }, s.summary) : null,
      outputs ? h(Pre, null, JSON.stringify(outputs, null, 2)) : null) : null)
}

function GuidedRunCard() {
  const ws = useWs()
  const p = ws.project
  const run = ws.run
  const [ackAll, setAckAll] = useState(false)
  const runError = ws.res.run && ws.res.run.error
  const relError = ws.res.release && ws.res.release.error
  if (!p.lastBuild) return h(Card, { id: RUN_CARD_ID, title: 'Guided Run · 15 步' }, h(Empty, { title: '先构建 Release' }, '构建并一键覆盖后，才能在 Workshop 环境里逐步运行。'))
  if (!run) {
    // Sources changed after the build: a new run record would be refused too, so send the SA to rebuild.
    return h(Card, { id: RUN_CARD_ID, title: 'Guided Run · 15 步' },
      runError ? h('div', { className: 'wc-err' }, runError.message) : h(Note, null, '加载中…'),
      runError && relError ? h('div', { className: 'wc-rf wc-mt1' }, h(Note, { tone: 'warn' }, '内容在构建后改过：先重新构建并一键覆盖，再新建运行记录。'), h(Button, { small: true, onClick: () => ws.go('3') }, '去交付 →')) : null,
      runError && !relError ? h('div', { className: 'wc-mt1' }, h(Button, { busy: ws.busy === 'reset', onClick: () => ws.resetRun() }, '新建运行记录（绑定当前 Release）')) : null)
  }
  const order = listOf(run.stepOrder)
  const passed = order.filter((id) => run.steps[id].status === 'passed').length
  const failed = order.find((id) => run.steps[id].status === 'failed')
  const remaining = order.length - passed
  const synced = p.status === 'synced'
  const canStart = synced && !ws.runningId && run.status !== 'passed'
  const needAck = remaining > 1 // one step left: "run all" is just that step, no extra acknowledgement
  return h(Card, { id: RUN_CARD_ID, title: `Guided Run · ${order.length} 步`, extra: h(Chip, { tone: run.status === 'passed' ? 'ok' : failed ? 'bad' : 'info' }, `${passed} / ${order.length} 通过`) },
    !synced ? h('div', { className: 'wc-rf' }, h(Note, { tone: 'warn' }, `一键覆盖并确认保留后才能运行（当前：${pick(PROJECT_STATUS, p.status)[1]}）。`), h(Button, { small: true, onClick: () => ws.go('3') }, '去交付 →')) : null,
    h('div', { className: 'wc-fx wc-mt1' },
      h(Button, { disabled: !canStart, busy: ws.busy === 'run' && !ws.runAll, onClick: () => ws.runNext() }, failed ? `重试「${STEP_LABEL[failed]}」` : '只跑下一步'),
      remaining ? h(Button, { kind: 'primary', icon: Rocket, disabled: !canStart || (needAck && !ackAll) || ws.runAll, onClick: () => ws.setRunAll(true) }, `一键彩排（剩余 ${remaining} 步）`)
        : h(Chip, { tone: 'ok' }, `${order.length} 步已全部通过，去右侧记录彩排结论`),
      ws.runAll ? h(Button, { onClick: () => ws.setRunAll(false) }, '当前步完成后停止') : null,
      ws.runningId ? h(Chip, { tone: 'info' }, h('span', { className: 'wc-spin' }), ` 正在跑「${STEP_LABEL[ws.runningId]}」`) : null),
    canStart && needAck ? h('div', { className: 'wc-mt1' }, h(Check, { checked: ackAll, onChange: setAckAll }, `我知道剩余 ${remaining} 步要跑几十分钟，会创建计费的 AWS 资源；第一步失败就停。清理不会自动运行。`)) : null,
    ws.runAll ? h(Note, null, '一键彩排进行中。“当前步完成后停止”只是不再启动下一步，不会取消已经在 AWS 里运行的这一步。') : null,
    h(ErrorLine, { error: ws.errors.run }),
    ws.lost.run ? h(Note, { tone: 'warn' }, '进度检查失败多次：步骤还在远端运行，点顶部“刷新”继续跟进。') : null,
    h('div', { className: 'wc-mt1' }, order.map((id, i) => h(StepRow, { key: id, id, index: i, run }))),
    h('div', { className: 'wc-rf wc-ln wc-mt1', style: { paddingTop: 8 } },
      h(Note, null, '新建运行记录会归档当前记录（彩排历史保留），AWS 资源不动。'),
      h(Button, { small: true, disabled: !!ws.runningId, busy: ws.busy === 'reset', onClick: () => ws.resetRun() }, '新建运行记录')),
    h(NewReleaseNote),
    h(ErrorLine, { error: ws.errors.reset }))
}

function PhenomenonVerdicts({ phenomena }) {
  return h(Table, {
    head: ['现象', '类型', '结论', '原因', '复现'],
    rows: phenomena.map((p) => {
      const off = listOf(p.cases).filter((c) => c.status !== 'reproduced')
      return {
        key: p.id,
        cells: [h('div', { style: { minWidth: 150 } }, h('div', { className: 'wc-mono wc-xs wc-b' }, p.id), h('div', { className: 'wc-xs wc-mut wc-clamp', title: p.teachingPoint }, p.teachingPoint)),
          h('span', { className: 'wc-xs', style: { whiteSpace: 'nowrap' } }, PHENOMENON_KIND[p.kind] || p.kind, p.mechanism ? h('div', { className: 'wc-mut' }, MECHANISM[p.mechanism] || p.mechanism) : null),
          h(ToneChip, { map: CONTRAST, value: p.verdict || p.status }),
          h('div', { className: 'wc-xs' }, h('span', { className: 'wc-mono' }, p.reasonCode || ''), (off.length ? off : listOf(p.cases)).slice(0, 3).map((c) => h('div', { key: c.caseId, className: 'wc-mut wc-break', title: englishTwin(c.reason, c.reasonEn) }, `${c.caseId}：${c.reason}`))),
          p.replication ? h('span', { className: 'wc-xs' }, `${p.replication.reproducedIn} / ${p.replication.completeRuns}`) : '—'],
      }
    }),
    empty: '没有教学现象。',
  })
}

function RunHintButton() {
  // "Run all 15 Guided steps": the Guided Run card is on this page (scroll to it); before the release
  // is applied and confirmed, the next action is the one-click delivery in 03.
  const ws = useWs()
  const target = ws.run ? listOf(ws.run.stepOrder).find((sid) => ws.run.steps[sid].status !== 'passed') : null
  if (ws.project.status !== 'synced') return h(Button, { small: true, onClick: () => ws.go('3') }, '先去交付 →')
  return h(Button, { small: true, onClick: scrollToRunCard, title: target ? `下一步：${STEP_LABEL[target] || target}` : '' }, '去运行 ↑')
}

// A changed release is re-synced into the SAME SA-prepared environment (SKILL.md steps 5-6): the SA first
// removes this scenario's resources by hand with --scenario-only; plain 99-cleanup.sh is the after-class teardown.
const SCENARIO_ONLY_CLEANUP = 'cd /home/ssm-user/workshop/current && ./99-cleanup.sh --scenario-only'
function NewReleaseNote() {
  return h(Fragment, null,
    h(Note, null, '换了 Release（Kiro 修过并重新构建）就在同一个 Workshop 环境里重新同步：先以 workshop 用户在 Workshop EC2 上手动运行下面这条'
      + '（只删本场景的智能体、Gateway、知识库、工具 Lambda、SSM 参数和评测记录，保留 workshop-infra 和 Customizer 附加栈；本应用不会替你运行），'
      + '再到 03 交付 体检 → 一键覆盖 → 确认保留，然后新建运行记录、从头跑完 15 步。'),
    h(Pre, null, SCENARIO_ONLY_CLEANUP))
}

function RemediationList({ hints }) {
  const ws = useWs()
  const [all, setAll] = useState(false)
  const live = isLive(ws.generation)
  const blocked = wsMaterialsBlock(ws, true) // remediation starts a repair round
  const waiting = !live && holdingResult(ws) && (ws.generation || {}).mode !== 'repair' // a repair would void the draft that waits in 01 (as in 02)
  if (!hints.length) return null
  const blocking = hints.filter((x) => x.severity === 'blocking').length
  const newRelease = hints.some((x) => x.severity === 'blocking' && listOf(x.asset && x.asset.scopes).length) // a project file changes
  return h('div', { className: 'wc-mt' },
    h('div', { className: 'wc-rf' }, h('span', { className: 'wc-b wc-sm' }, `要调整的 ${hints.length} 项`), h('span', { className: 'wc-xs wc-mut' }, `阻断 ${blocking} · 建议 ${hints.length - blocking}`)),
    waiting && hints.some((x) => listOf(x.asset && x.asset.scopes).length) ? h(Note, { tone: 'warn' }, '01 有一份待应用的 Kiro 草稿：让 Kiro 修会让它作废。先应用它，再修。') : null,
    (all ? hints : hints.slice(0, 4)).map((hint) => {
      const asset = hint.asset || {}
      const scopes = listOf(asset.scopes)
      return h('div', { key: hint.id, className: 'wc-list-row' },
        h(Chip, { tone: hint.severity === 'blocking' ? 'bad' : 'warn' }, hint.severity === 'blocking' ? '阻断' : '建议'),
        hint.round ? h(Chip, { title: '快速彩排的这一轮没复现（最后一轮通过）' }, `第 ${hint.round} 轮`) : null,
        h('div', { className: 'wc-grow' },
          h('div', { className: 'wc-sm wc-break', title: englishTwin(hint.action, hint.actionEn) }, hint.action),
          h('div', { className: 'wc-xs wc-mut wc-break', title: englishTwin(hint.because, hint.becauseEn) }, hint.because),
          h('div', { className: 'wc-fx wc-mt1' }, h('span', { className: 'wc-xs wc-mono wc-mut wc-break' }, [asset.file, asset.scenarioPath].filter(Boolean).join(' · ') || asset.kind),
            scopes.map((s) => h(Chip, { key: s }, SCOPE_LABEL[s] || s)), h('span', { className: 'wc-xs wc-mut wc-mono' }, hint.code))),
        scopes.length ? h(Button, { small: true, icon: Wand2, disabled: live || waiting || !!blocked, title: waiting ? '01 有一份待应用的 Kiro 草稿：先应用它，再让 Kiro 修' : blocked || '只把这一条整改发给 Kiro（连同当前校验里 Kiro 能修的结果），彩排的其他整改不发', busy: ws.busy === `remedy:${hint.id}`, onClick: () => ws.startRemediation(hint) }, '让 Kiro 修')
          : hint.code === 'RUN_INCOMPLETE' ? h(RunHintButton) : null)
    }),
    hints.length > 4 ? h(Button, { small: true, kind: 'ghost', onClick: () => setAll(!all) }, all ? '收起' : `展开全部 ${hints.length} 项`) : null,
    newRelease ? h('div', { className: 'wc-mt1' }, h(NewReleaseNote)) : null)
}

function RehearsalCard() {
  const ws = useWs()
  const r = ws.rehearsal
  const error = ws.res.rehearsal && ws.res.rehearsal.error
  const lr = ws.project.lastRehearsal
  const stale = lr && ws.project.lastBuild && lr.releaseVersion !== ws.project.lastBuild.version
  return h(Card, {
    title: '彩排结论',
    extra: [
      lr ? h(Chip, { key: 'l', tone: stale ? 'warn' : '' }, stale ? `记录针对旧版本 ${lr.releaseVersion}` : `已记录 · ${fmtTime(lr.at)}`) : h(Chip, { key: 'l' }, '未记录'),
      h(Button, { key: 'b', disabled: !r, busy: ws.busy === 'rehearsal', onClick: ws.recordRehearsal }, '记录彩排结论'),
    ],
  },
  !r ? h(Empty, { title: '还没有彩排结论' }, error ? backendText(error.message) || error.message : '加载中…') : h(Fragment, null,
    h('div', { className: 'wc-rf' },
      h('span', { className: 'wc-fx' }, h(ToneChip, { map: VERDICT, value: r.verdict }), r.readyForClass ? h(Chip, { tone: 'ok' }, '可以替换 HR 通用版上课') : h(Chip, { tone: 'bad' }, '还不能上课')),
      h('span', { className: 'wc-xs wc-mut' }, `Release ${r.releaseVersion} · 完整运行 ${r.inputs ? r.inputs.completeRuns : 0} 次 · 判定依据 ${r.inputs ? SCENARIO_SOURCE[r.inputs.scenario] || r.inputs.scenario : ''}`)),
    h('div', { className: 'wc-sm wc-mt1', title: englishTwin(r.reason, r.reasonEn) }, h('span', { className: 'wc-mono wc-xs' }, r.reasonCode), ' ', r.reason),
    listOf(r.readiness && r.readiness.blockers).length ? h('div', { className: 'wc-fx wc-mt1' }, r.readiness.blockers.map((b) => {
      const said = listOf(r.readiness.blockerTexts).find((t) => t && t.code === b)
      return h(Chip, { key: b, tone: 'bad', title: said ? said.text : b }, BLOCKER[b] || (said && said.text) || b)
    })) : null,
    h('div', { className: 'wc-fx wc-mt1' }, Object.entries(r.groups || {}).map(([g, s]) => h(Chip, { key: g, tone: pick(CONTRAST, s)[0] }, `${GROUP_LABEL[g] || g}：${pick(CONTRAST, s)[1]}`))),
    h(Kpi, { className: 'wc-mt' },
      h(Stat, { label: '要判定的现象', value: r.summary ? r.summary.phenomena : listOf(r.phenomena).length }),
      h(Stat, { label: '已复现', value: r.summary ? r.summary.reproduced : '—', tone: 'ok' }),
      h(Stat, { label: '没复现', value: r.summary ? r.summary.notReproduced : '—', tone: r.summary && r.summary.notReproduced ? 'bad' : '' }),
      h(Stat, { label: '证据不足', value: r.summary ? r.summary.insufficientEvidence : '—', tone: r.summary && r.summary.insufficientEvidence ? 'warn' : '' })),
    phenomenaOf(ws.sc).length > (r.summary ? r.summary.phenomena : listOf(r.phenomena).length)
      ? h('div', { className: 'wc-xs wc-mut wc-mt1' }, `共 ${phenomenaOf(ws.sc).length} 个教学现象：${r.summary ? r.summary.phenomena : listOf(r.phenomena).length} 个按彩排判定，其余只作参考（不影响能否上课，见下面「参考」）。`) : null,
    listOf(r.advisory).length ? h('div', { className: 'wc-mt1' }, r.advisory.map((a, i) => h('div', { key: a.id || i, className: 'wc-xs wc-mut', title: englishTwin(a.note, a.noteEn) },
      `参考 · ${a.id || a.kind}：${ADVISORY_READING[a.reading] || a.reading || a.status}${a.note ? `（${a.note}）` : ''}`))) : null,
    h(RemediationList, { hints: listOf(r.remediation) }),
    listOf(r.warnings).length ? h('div', { className: 'wc-mt1' }, r.warnings.map((w, i) => h('div', { key: i, className: 'wc-xs wc-tx-warn', title: w && w.code }, `! ${warningText(w)}`))) : null,
    r.inputs && r.inputs.noiseBand ? h(Note, null, `裁判噪声带 ${fmtNum(r.inputs.noiseBand.value, 3)}（${r.inputs.noiseBand.source || '未标定'}）；分差落在这个范围内视为无明显差异。`) : null,
    h(Note, null, r.scopeWarning)),
  h(ErrorLine, { error: ws.errors.rehearsal }))
}

function PhenomenaCheckCard() {
  const ws = useWs()
  const r = ws.rehearsal
  if (!r || !listOf(r.phenomena).length) return null
  return h(Card, { title: '教学现象核对', sub: `按最近一次完整运行判定（${r.inputs && r.inputs.decisiveRun ? r.inputs.decisiveRun : '还没有完整运行'}）；复现 = 复现次数 / 完整运行次数。` },
    h(PhenomenonVerdicts, { phenomena: listOf(r.phenomena) }))
}

// The phenomena a case-table row belongs to. guided_report rows carry no phenomenonIds (only rehearsal.py
// adds them), so a report row takes them from the rehearsal's row of the same case, else from labs.teaching.
function casePhenomena(row, rehearsalTable, sc) {
  const fromRehearsal = listOf(rehearsalTable && rehearsalTable.cases).find((r) => r.caseId === row.caseId)
  return [listOf(row.phenomenonIds), listOf(fromRehearsal && fromRehearsal.phenomenonIds), phenomenaOfCase(sc, row.caseId)].find((ids) => ids.length) || []
}

function CaseTableCard() {
  const ws = useWs()
  const table = (ws.report && ws.report.caseTable) || (ws.rehearsal && ws.rehearsal.caseTable)
  const phenomena = (row) => casePhenomena(row, ws.rehearsal && ws.rehearsal.caseTable, ws.sc)
  const cases = listOf(table && table.cases)
  if (!cases.length) return null
  const one = (map, v) => (v ? h(ToneChip, { map, value: v }) : h('span', { className: 'wc-mut' }, '—'))
  const pair = (map, b, o) => h('span', { className: 'wc-fx', style: { flexWrap: 'nowrap' } }, one(map, b), '→', one(map, o))
  const cell = (row, key) => {
    const b = row.baseline || {}
    const o = row.optimized || {}
    if (key === 'l1') return pair(L1_VERDICT, b.l1 && b.l1.verdict, o.l1 && o.l1.verdict)
    // THELMA judges the retrieval probes only: a probe without a score is missing evidence, not "n/a".
    if (key === 'thelma') {
      if (b.thelma || o.thelma) return `${fmtNum(b.thelma && b.thelma.value)} → ${fmtNum(o.thelma && o.thelma.value)}`
      return row.probe ? h(Chip, { tone: 'warn', title: '检索探针应有 THELMA 分数；没有 trace 或关联不到题，记为证据不足' }, '无证据') : h('span', { className: 'wc-mut' }, '不适用')
    }
    if (key === 'mtg') return `${mtgLabel(b.mtg && b.mtg.label)} → ${mtgLabel(o.mtg && o.mtg.label)}`
    return pair(COMBINED, b.combined, o.combined)
  }
  return h(Card, { title: '逐题对照', sub: `练习题，基线 → 优化（来源：${ws.report ? '本轮报告' : `彩排 ${table.source || ''}`}）` },
    h(Table, {
      head: ['题目', 'L1', 'THELMA GR', 'Mind the Goal', '结论'],
      rows: cases.map((row) => ({
        key: row.caseId,
        cells: [h('div', null, h('span', { className: 'wc-break' }, row.label || row.caseId), ' ', row.probe ? h(Chip, { tone: 'info' }, '检索探针') : null, h('div', { className: 'wc-xs wc-mut wc-mono' }, row.caseId, phenomena(row).length ? ` · ${phenomena(row).join(', ')}` : '')),
          cell(row, 'l1'), cell(row, 'thelma'), cell(row, 'mtg'), h('span', { className: 'wc-b' }, cell(row, 'combined'))],
      })),
    }))
}

// The report's missing evidence (guided_report, English, one line per script) by the step that delivers it.
const EVIDENCE_STEP = { '09': 'baseline', '10': 'optimize', '11': 'cost-latency', '12': 'models', '13': 'judge-stability', baseline: 'baseline', optimized: 'optimize' }
function evidenceText(m) {
  const said = String(m || '')
  const step = EVIDENCE_STEP[(said.match(/\b(\d\d)-[\w-]+\.sh/) || said.match(/\((baseline|optimized)[;)]/) || [])[1]]
  if (!step) return said
  const what = /^L1/.test(said) ? ' L1 断言' : /^Mind the Goal/.test(said) ? ' Mind the Goal 判定' : /latency/.test(said) ? '指标'
    : /stability|noise band/.test(said) ? '数值和裁判噪声带' : /model identity/.test(said) ? '模型名和对比分数' : '评估分数'
  const cases = (said.match(/missing: ([^)]*)\)/) || [])[1]
  return `「${STEP_LABEL[step] || step}」的${what}${cases ? `（缺：${cases}）` : ''}`
}

function ReportCard() {
  const ws = useWs()
  const r = ws.report
  const reh = ws.rehearsal
  if (!r) {
    const missing = listOf(reh && reh.inputs && reh.inputs.missingEvidence)
    const run = ws.run
    const untouched = !run || listOf(run.stepOrder).every((id) => !run.steps[id] || run.steps[id].status === 'not_started')
    return h(Card, { title: '本轮报告' }, h(Empty, { title: '15 步全部通过后生成报告' },
      untouched && (missing.length || run) ? '还一步都没跑：先在「03 交付」一键覆盖并确认保留，再在上面的 Guided Run 里逐步运行。'
        : missing.length ? h('span', { title: missing.join('\n') }, `还缺：${missing.map(evidenceText).join('；')}`) : (ws.res.report && ws.res.report.error ? '' : '加载中…')))
  }
  const delta = (r.quality && r.quality.delta) || {}
  const contrast = r.teachingContrast || {}
  const cl = (r.operations && r.operations.costLatency) || {}
  const js = r.judgeStability || {}
  return h(Card, { title: '本轮报告', extra: h(Chip, { tone: r.status === 'complete' ? 'ok' : 'warn' }, r.status === 'complete' ? '完整' : '不完整') },
    h(Kpi, null,
      h(Stat, { label: '步骤通过', value: `${r.completion.passed} / ${r.completion.total}` }),
      h(Stat, { label: '平均延迟', value: cl.averageLatencySeconds ? `${fmtNum(cl.averageLatencySeconds, 1)} 秒` : '—' }),
      h(Stat, { label: '总成本', value: cl.totalCostUsd != null ? `$${fmtNum(cl.totalCostUsd, 4)}` : '—' }),
      h(Stat, { label: js.verdict ? `裁判噪声带 · ${verdictLabel(CAL_VERDICT, js.verdict)[1]}` : '裁判噪声带', value: fmtNum(js.noiseBand, 3), tone: js.verdict === 'stable' ? 'ok' : js.verdict ? 'warn' : '' })),
    listOf(r.completion.missingEvidence).length ? h(Note, { tone: 'warn' }, `缺证据：${r.completion.missingEvidence.join('；')}`) : null,
    h(Table, {
      head: ['评估器', '基线', '优化', '差', '超出裁判噪声带'],
      rows: Object.entries(delta).map(([name, d]) => [h('span', { className: 'wc-mono wc-xs' }, name), fmtNum(d.baselineMean, 3), fmtNum(d.optimizedMean, 3),
        h('span', { className: d.delta > 0 ? 'wc-tx-ok' : d.delta < 0 ? 'wc-tx-bad' : '' }, fmtNum(d.delta, 3)), d.clearsNoiseBand ? '是' : d.sampleCountsMatch ? '否' : '样本数不同']),
      empty: '没有可比较的评估分数。',
    }),
    listOf(r.quality && r.quality.regressions).length ? h(Note, { tone: 'bad' }, `退步：${r.quality.regressions.map((x) => x.evaluator).join('、')}`) : null,
    h('div', { className: 'wc-b wc-sm wc-mt' }, h('span', null, '本轮教学对比 '), h(ToneChip, { map: CONTRAST, value: contrast.currentRun })),
    h(PhenomenonVerdicts, { phenomena: listOf(contrast.phenomena) }),
    h(Note, null, contrast.readiness || r.scopeWarning))
}

function CleanupCard() {
  const [ack, setAck] = useState(false)
  return h(Card, { title: '课后清理（单独、不可撤销）' },
    h('div', { className: 'wc-sm wc-tx-bad' }, '清理不在一键彩排里；本应用不会替你执行删除命令，也不会删除 AWS 资源。'),
    h('div', { className: 'wc-mt1' }, h(Check, { checked: ack, onChange: setAck }, '我知道清理会永久删除 Workshop 的 AWS 资源；会先在 Workshop EC2 上核对账号和区域。')),
    h(Note, null, '这是课后拆除：不加参数的 99-cleanup.sh 还会删掉 workshop-infra 和 Customizer 附加栈。彩排中换 Release 不用它，用 ./99-cleanup.sh --scenario-only（见 Guided Run）。'),
    ack ? h(Fragment, null, h(Note, null, '单独确认之后，以 workshop 用户在 Workshop EC2 上手动运行：'), h(Pre, null, 'cd /home/ssm-user/workshop/current && ./99-cleanup.sh')) : null,
    h(Note, null, 'Workshop 校验和运行报告不是上线审批。'))
}

function ClassModeCard() {
  const ws = useWs()
  const p = ws.project
  const lr = p.lastRehearsal
  const built = !!ws.release
  // The downloads are always the current build; say plainly when that build has not passed rehearsal.
  const rehearsed = !!(lr && lr.readyForClass && p.lastBuild && lr.releaseVersion === p.lastBuild.version)
  const stale = !!(lr && p.lastBuild && lr.releaseVersion !== p.lastBuild.version)
  const verdict = lr ? (lr.readyForClass ? '可以上课' : lr.verdict === 'ready' ? '还不能上课' : pick(VERDICT, lr.verdict)[1]) : '未记录'
  const why = !p.lastBuild ? '还没构建 Release' : !lr ? '还没记录彩排结论' : stale ? `彩排结论针对旧版本 ${lr.releaseVersion}` : `彩排结论：${verdict}`
  return h(Card, { title: '上课', sub: '下面的手册和导师包都是当前构建的 Release；学员按 README.md 自己跑，导师看导师手册。' },
    h(Kpi, null,
      h(Stat, { label: 'Release', small: true, value: h('span', { className: 'wc-mono' }, p.lastBuild ? p.lastBuild.version : '未构建') }),
      h(Stat, { label: '彩排', small: true, value: stale ? '针对旧版本' : verdict, tone: rehearsed ? 'ok' : 'warn' }),
      h(Stat, { label: 'Workshop 状态', small: true, value: pick(PROJECT_STATUS, p.status)[1] })),
    !rehearsed ? h('div', { className: 'wc-banner wc-bad wc-rf' },
      h('span', null, h('span', { className: 'wc-b' }, '当前 Release 还没通过彩排'), `（${why}）：在“彩排”里跑完 15 步、把结论做到“可以上课”，再用它上课。`),
      h(Button, { small: true, onClick: () => ws.go('4', 'rehearse') }, '去彩排')) : null,
    h('div', { className: 'wc-fx wc-mt' },
      h(LinkButton, { href: LINKS.guideDownload(ws.pid, 'student'), disabled: !built }, '学员手册 README.md'),
      h(LinkButton, { href: LINKS.guideDownload(ws.pid, 'instructor'), disabled: !built }, '导师手册'),
      h(LinkButton, { href: LINKS.instructorBundle(ws.pid), disabled: !built }, '导师包')),
    h(Note, { tone: 'bad' }, '导师手册和导师包含留出题与期望答案，不要投屏或发给学员。'))
}

function ResultsCard() {
  // After class: this run's results and the evidence bundle for the instructor and the customer debrief.
  const ws = useWs()
  const r = ws.report
  const lr = ws.project.lastRehearsal
  return h(Card, { title: '成果', sub: '本轮报告和彩排证据，课后复盘用。逐题对照和各现象的结论在“彩排”里。' },
    r ? h(Kpi, null,
      h(Stat, { label: '步骤通过', value: `${r.completion.passed} / ${r.completion.total}` }),
      h(Stat, { label: '本轮报告', value: r.status === 'complete' ? '完整' : '不完整', tone: r.status === 'complete' ? 'ok' : 'warn' }),
      h(Stat, { label: '本轮教学对比', small: true, value: pick(CONTRAST, (r.teachingContrast || {}).currentRun)[1], tone: pick(CONTRAST, (r.teachingContrast || {}).currentRun)[0] }))
      : h(Empty, { title: '还没有本轮报告' }, '15 步全部通过后生成。'),
    h('div', { className: 'wc-fx wc-mt' },
      h(LinkButton, { href: LINKS.instructorBundle(ws.pid), disabled: !ws.release }, lr ? '导师包（含彩排证据）' : '导师包'),
      h(Button, { small: true, onClick: () => ws.go('4', 'rehearse') }, '看逐题对照')),
    h(Note, { tone: 'bad' }, '导师包含留出题与期望答案，只给导师；给客户的复盘请从中摘取。'))
}

function DirectRunCard() {
  const ws = useWs()
  const p = ws.project
  const direct = ws.direct || {}
  const job = direct.job
  const active = jobActive(job) || jobActive(direct.cleanupJob)
  const [models, setModels] = useState('')
  const [repeat, setRepeat] = useState(1)
  const [panel, setPanel] = useState(false)
  const compare = models.split(/[\s,，]+/).map((m) => m.trim()).filter(Boolean).slice(0, 3)
  const stages = job ? listOf(job.stages) : Object.keys(DIRECT_STAGE).map((id) => ({ id, status: 'pending' }))
  return h(Card, {
    title: '快速彩排 · 直连 AgentCore', extra: job ? h(ToneChip, { map: JOB_STATUS, value: job.status }) : null,
    sub: '不占 Workshop 实例：约 8 分钟把当前构建部署到这个包自己的直连资源（名字都带 direct），用 Workshop 的同一套裁判和彩排规则判定。用来快速迭代；能不能上课仍以 Guided Run 为准。',
  },
  h(Field, { label: '同时对比的模型（可选，最多 3 个）', hint: '每个模型用候选 Prompt 再问一遍全部题目，对比质量、成本和延迟' },
    h(TextInput, { value: models, onChange: setModels, placeholder: '例：us.amazon.nova-pro-v1:0', mono: true, disabled: active })),
  h('div', { className: 'wc-rf wc-mt1' }, h(Seg, { value: repeat, onChange: setRepeat, options: DIRECT_REPEAT, disabled: active }),
    h(Check, { checked: panel, disabled: active, onChange: setPanel }, '同时用 AgentCore 内置评估器打分（约多 2 分钟）')),
  h('div', { className: 'wc-mt1' }, h(Button, { kind: 'primary', icon: Rocket, disabled: !p.lastBuild || !p.target || active, busy: ws.busy === 'direct' || jobActive(job), onClick: () => ws.startDirect(compare, { repeat, panel }) },
    jobActive(job) ? '快速彩排进行中…' : repeat > 1 ? `快速彩排当前构建 ${repeat} 轮` : '快速彩排当前构建')),
  !p.lastBuild ? h(Note, { tone: 'warn' }, '先在 03 构建 Release：快速彩排跑的是当前构建。') : null,
  !p.target ? h(Note, { tone: 'warn' }, '先在 03 配置 Workshop 环境：快速彩排用同一个 AWS 账号和区域。') : null,
  h(ErrorLine, { error: ws.errors.direct }),
  h('div', { className: 'wc-steps wc-mt1' }, stages.map((s, i) => h(Chip, { key: s.id, tone: pick(JOB_STATUS, s.status)[0], title: asText(s.error) },
    `${i + 1} ${DIRECT_STAGE[s.id] || s.id}${s.status !== 'pending' ? ` · ${pick(JOB_STATUS, s.status)[1]}` : ''}`))),
  job && job.error ? h('div', { className: 'wc-err' }, asText(job.error)) : null,
  job && job.status === 'interrupted' ? h(Note, { tone: 'warn' }, '后端在快速彩排中重启过；直连资源可以直接重跑，会原地更新。') : null,
  ws.lost.direct ? h(Note, { tone: 'warn' }, '和后端失联：任务还在服务端运行，点顶部“刷新”继续跟进。') : null,
  direct.rehearsal ? h(DirectVerdict, { r: direct.rehearsal }) : p.lastBuild ? h('div', { className: 'wc-mt' }, h(Empty, null, '当前构建还没快速彩排过。')) : null,
  h(DirectCleanup))
}

// A quick rehearsal's time, said so the parts add up: the deploy once, then each stage over every round. Records before
// 0.4.6 keep only the last round's stages in direct.timings, so the rounds' own are added here.
function directTimeText(d) {
  const rounds = listOf(d.rounds).filter((r) => r && r.timings)
  const spent = { ...(d.timings || {}) }
  const stages = Object.keys(DIRECT_TIMING).filter((k) => k !== 'provision')
  if (rounds.length > 1) {
    for (const k of stages) {
      const seen = rounds.filter((r) => typeof r.timings[k] === 'number')
      if (seen.length) spent[k] = seen.reduce((n, r) => n + r.timings[k], 0)
    }
  }
  const part = (k) => (typeof spent[k] === 'number' ? `${DIRECT_TIMING[k]} ${fmtSeconds(spent[k])}` : null)
  const each = stages.map(part).filter(Boolean).join(' · ')
  const parts = [part('provision'), each && rounds.length > 1 ? `${rounds.length} 轮合计：${each}` : each].filter(Boolean)
  return `用时 ${d.seconds ? fmtSeconds(d.seconds) : '—'}${parts.length ? `（${parts.join(rounds.length > 1 ? '；' : ' · ')}）` : ''}`
}

function DirectVerdict({ r }) {
  const d = r.direct || {}
  return h(Fragment, null,
    h('div', { className: 'wc-rf wc-mt' },
      h('span', { className: 'wc-fx' }, h(ToneChip, { map: DIRECT_VERDICT, value: r.verdict }), h(Chip, null, '不是上课结论')),
      h('span', { className: 'wc-xs wc-mut' }, `${fmtTime(r.generatedAt)} · ${directTimeText(d)}`)),
    h('div', { className: 'wc-sm wc-mt1', title: englishTwin(r.reason, r.reasonEn) }, h('span', { className: 'wc-mono wc-xs' }, r.reasonCode), ' ', r.reason),
    h('div', { className: 'wc-mt1' }, h(PhenomenonVerdicts, { phenomena: listOf(r.phenomena) })),
    listOf(d.sweep).length ? h(DirectSweep, { sweep: d.sweep }) : null,
    d.panel ? h(DirectPanel, { panel: d.panel, phenomena: listOf(r.phenomena) }) : null,
    listOf(d.invokeErrors).length ? h(Note, { tone: 'warn' }, `${d.invokeErrors.length} 次调用出错：${d.invokeErrors.slice(0, 3).map((e) => `${e.caseId} ${e.error}`).join('；')}`) : null,
    h(RemediationList, { hints: listOf(r.remediation) }),
    d.robust === false ? h(Note, { tone: 'warn' }, `${listOf(d.rounds).length} 轮里 ${d.readyRounds ?? '?'} 轮通过：${listOf(d.flaky).join('、') || '有现象'}时有时无。`
      + (r.verdict === 'ready' ? '整改里标着「第 N 轮」的是那几轮没复现的原因，修好再跑 3 轮。' : '')) : null,
    listOf(d.skippedSkills).length ? h(Note, null, `没加载的 Skill（SKILL.md 没有 frontmatter，Workshop 上课时也没加载）：${d.skippedSkills.join('、')}。`) : null,
    r.verdict === 'ready' && d.robust !== false ? h(Note, { tone: 'ok' }, '快速彩排通过：去 03 一键覆盖，再跑一次 Guided Run 拿上课结论。') : null)
}

function DirectSweep({ sweep }) {
  return h('div', { className: 'wc-mt' }, h('div', { className: 'wc-b wc-sm' }, '模型对比（候选 Prompt）'), h(Table, {
    head: ['模型', '平均 GR', '候选 GR（原模型）', '平均延迟 s', '平均成本 $', '出错'],
    rows: sweep.map((m) => ({ key: m.model, cells: [h('span', { className: 'wc-mono wc-xs' }, m.model), fmtNum(m.meanGR, 3), fmtNum(m.candidateMeanGR, 3),
      fmtNum(m.averageLatencySeconds, 1), m.priced === false ? '未定价' : fmtNum(m.averageCostUsd, 4), m.invokeErrors || 0] })),
  }))
}

function DirectPanel({ panel, phenomena }) {
  if (panel.error) return h(Note, { tone: 'warn' }, `AgentCore 评估器这次没跑成（不影响彩排结论）：${panel.error}`)
  const evaluators = listOf(panel.evaluators)
  const cell = (pid, e) => listOf(panel.matrix).find((c) => c.phenomenonId === pid && c.evaluator === e)
  const short = (e) => e.replace('Builtin.', '').replace('ThirdParty.', '')
  const picks = listOf(panel.recommendation).filter((r) => r.recommended)
  const against = listOf(panel.recommendation).filter((r) => listOf(r.contradicts).length)
  const how = panel.references === false ? '按线上评估的方式打分：不给期望答案' : '给了期望答案（数据集方式）：选线上评估器请用不带期望答案的一次'
  return h('div', { className: 'wc-mt' }, h('div', { className: 'wc-b wc-sm' }, 'AgentCore 内置评估器（同一批会话，基线 → 优化）'),
    h('div', { className: 'wc-xs wc-mut' }, how), h(Table, {
    head: ['现象', ...evaluators.map(short)],
    rows: phenomena.map((p) => ({ key: p.id, cells: [h('span', { className: 'wc-mono wc-xs' }, p.id), ...evaluators.map((e) => {
      const c = cell(p.id, e)
      if (!c) return '—'
      const [tone, label] = pick(PANEL_MARK, c.reading)
      return h('span', { className: 'wc-xs', style: { whiteSpace: 'nowrap' }, title: `裁判噪声带 ${fmtNum(c.band, 3)}` }, `${fmtNum(c.baseline)} → ${fmtNum(c.optimized)} `, h(Chip, { tone }, label))
    })] })),
  }),
  picks.length ? h(Note, { tone: 'ok' }, `这个 Agent 上线做在线评估，建议用：${picks.map((r) => `${short(r.evaluator)}（看出 ${r.sees.join('、')}）`).join('；')}。`) : null,
  against.length ? h(Note, { tone: 'warn' }, `会奖励失败的评估器：${against.map((r) => `${short(r.evaluator)}（${r.contradicts.join('、')}）`).join('；')}。`) : null,
  listOf(panel.unseen).length ? h(Note, null, `建议的评估器看不出：${panel.unseen.join('、')}（这几项留给 Workshop 自己的裁判）。`) : null)
}

function DirectCleanup() {
  const ws = useWs()
  const direct = ws.direct || {}
  const [ack, setAck] = useState(false)
  const job = direct.cleanupJob
  if (!direct.resources && !jobActive(job)) return null
  const failed = job && job.status === 'failed' ? listOf(job.stages).map((s) => detailText(s.detail)).filter(Boolean).join('；') : ''
  return h(Details, { summary: '清理直连资源' },
    h(Note, null, '只删这个包的直连资源（知识库、工具 Lambda 与 Gateway、Memory、角色和 Harness，名字都带 direct），不碰 Workshop 的资源。下次快速彩排会重新创建，约多 3 分钟。'),
    h(Check, { checked: ack, disabled: jobActive(job), onChange: setAck }, '确认删除这个包的直连资源'),
    h('div', { className: 'wc-mt1' }, h(Button, { kind: 'danger', icon: Trash2, disabled: !ack || jobActive(job) || jobActive(direct.job), busy: ws.busy === 'directCleanup' || jobActive(job), onClick: ws.startDirectCleanup }, '清理')),
    h(ErrorLine, { error: ws.errors.directCleanup }),
    job ? h('div', { className: 'wc-xs wc-mt1' }, h(ToneChip, { map: JOB_STATUS, value: job.status }), failed ? h('span', { className: 'wc-tx-bad' }, ` ${failed}`) : null) : null)
}

function DirectChatCard() {
  const ws = useWs()
  const ready = !!(ws.direct && ws.direct.resources && ws.direct.resources.harness)
  const [question, setQuestion] = useState('')
  const [prompt, setPrompt] = useState('candidate')
  const [model, setModel] = useState('')
  const [chats, setChats] = useState([])
  const [error, setError] = useState(null)
  const running = chats.find((c) => c.status === 'running')
  useEffect(() => {
    if (!running) return undefined
    const timer = setTimeout(async () => {
      try {
        const next = await ws.directChat(running.id)
        setChats((list) => list.map((c) => (c.id === next.id ? next : c)))
      } catch (err) { setError(apiError(err)); setChats((list) => [...list]) } // keep polling: the answer is still coming
    }, 1500)
    return () => clearTimeout(timer)
  }, [running && running.id, chats])
  const ask = async () => {
    setError(null)
    try {
      const chat = await ws.askDirect({ question: question.trim(), prompt, ...(model.trim() ? { model: model.trim() } : {}) })
      setChats((list) => [chat, ...list].slice(0, 10))
      setQuestion('')
    } catch (err) { setError(apiError(err)) }
  }
  return h(Card, { title: '和直连 Agent 对话', sub: '用快速彩排部署的 Harness 问任何问题：每问一次是一个新会话，Prompt 和模型按这次的选择，不改 Harness。' },
    !ready ? h(Empty, null, '先跑一次快速彩排：它会部署这个包的直连 Harness。') : h(Fragment, null,
      h(TextArea, { value: question, onChange: setQuestion, rows: 3, placeholder: '学员会怎么问？', label: '问题' }),
      h('div', { className: 'wc-rf wc-mt1' }, h(Seg, { value: prompt, onChange: setPrompt, options: CHAT_PROMPT }),
        h(TextInput, { value: model, onChange: setModel, placeholder: '模型（默认用 Release 的）', mono: true })),
      h('div', { className: 'wc-mt1' }, h(Button, { kind: 'primary', disabled: !question.trim() || !!running, busy: !!running, onClick: ask }, '提问')),
      h(ErrorLine, { error }),
      chats.map((c) => h(DirectChatTurn, { key: c.id, chat: c }))))
}

function DirectChatTurn({ chat }) {
  return h('div', { className: 'wc-list-row wc-mt1' },
    h(Chip, { tone: chat.status === 'done' ? 'ok' : chat.status === 'failed' ? 'bad' : 'info' }, chat.prompt === 'baseline' ? '基线' : '候选'),
    h('div', { className: 'wc-grow' },
      h('div', { className: 'wc-sm wc-b wc-break' }, chat.question),
      chat.status === 'running' ? h('div', { className: 'wc-xs wc-mut' }, '回答中…') : h('div', { className: 'wc-sm wc-break', style: { whiteSpace: 'pre-wrap' } }, chat.answer || '（没有回答）'),
      h('div', { className: 'wc-xs wc-mut wc-mono wc-break' }, [listOf(chat.tools).length ? `工具：${chat.tools.join(', ')}` : '没调用工具', chat.model, chat.seconds ? `${chat.seconds}s` : '']
        .filter(Boolean).join(' · ')),
      chat.error ? h('div', { className: 'wc-xs wc-tx-bad wc-break' }, chat.error) : null))
}

const REHEARSAL_MODES = [['rehearse', '彩排'], ['class', '上课'], ['after', '课后']]
const REHEARSAL_MODE_NOTE = {
  rehearse: '先快速彩排（约 8 分钟，直连 AgentCore）反复改到通过，再跑 Guided Run 拿上课结论；两者都只对当前 Release 有效。',
  class: '上课用彩排通过的版本；学员手册和导师包都按当前构建导出。',
  after: '课后导出成果、清理资源；清理要单独确认。',
}

function RehearsalStep({ tab, onTab }) {
  const mode = REHEARSAL_MODES.some(([m]) => m === tab) ? tab : 'rehearse'
  const body = mode === 'rehearse' ? h(Fragment, null,
    h('div', { className: 'wc-g2 wc-mt' }, h(DirectRunCard), h(DirectChatCard)),
    h('div', { className: 'wc-g2 wc-mt' }, h(GuidedRunCard), h(RehearsalCard)),
    h('div', { className: 'wc-col wc-mt' }, h(PhenomenaCheckCard), h(CaseTableCard), h(ReportCard)))
    : mode === 'class' ? h('div', { className: 'wc-col wc-mt' }, h(ClassModeCard), h(GuidesCard))
      : h('div', { className: 'wc-g2e wc-mt' }, h(ResultsCard), h(CleanupCard))
  return h(Fragment, null,
    h(StepHeader, { n: '04', title: '彩排与上课', h1: '上课前自己完整跑一遍，确认设计的现象真的会出现' }),
    h('div', { className: 'wc-rf wc-mt' },
      h(Seg, { value: mode, onChange: (v) => onTab(v), options: REHEARSAL_MODES }),
      h('span', { className: 'wc-mut wc-sm' }, REHEARSAL_MODE_NOTE[mode])),
    body,
    h(StepFoot, { back: ['3', '← 回到交付'] }))
}

// ---------------------------------------------------------------------------
// 高级: raw scenario, files, calibration, step-by-step sync, project record
// ---------------------------------------------------------------------------

function ScenarioYamlCard() {
  const ws = useWs()
  const [yaml, setYaml] = useState(ws.project.scenarioYaml || '')
  useEffect(() => { setYaml(ws.project.scenarioYaml || '') }, [ws.project.scenarioYaml])
  const dirty = yaml !== (ws.project.scenarioYaml || '')
  return h(Card, { title: 'scenario.yaml', extra: h('span', { className: 'wc-xs wc-mut' }, `${yaml.length} 字符${dirty ? ' · 未保存' : ''}`) },
    h(TextArea, { value: yaml, onChange: setYaml, rows: 22, mono: true, label: 'scenario.yaml' }),
    h('div', { className: 'wc-fx wc-mt1' },
      h(Button, { kind: 'primary', disabled: !dirty, busy: ws.busy === 'scenario', onClick: () => ws.saveScenario(yaml) }, '保存手工修改'),
      h(Button, { disabled: !dirty, onClick: () => setYaml(ws.project.scenarioYaml || '') }, '放弃修改')),
    h(ErrorLine, { error: ws.errors.scenario }),
    h(Note, null, '手工保存会让校验和构建失效，并计入手工编辑记录；项目类型请在 01 切换。'))
}

function FileEditorCard() {
  const ws = useWs()
  const view = ws.scenarioView || {}
  const files = listOf(view.files)
  const [rel, setRel] = useState('')
  const [content, setContent] = useState('')
  const [loaded, setLoaded] = useState(null)
  const [error, setError] = useState(null)
  const [orphans, setOrphans] = useState(null)
  const [ackPrune, setAckPrune] = useState(false)
  const open = async (path) => {
    setRel(path); setError(null)
    try { const r = await ws.readFile(path); setContent(r.content); setLoaded(r.content) } catch (err) { const e = apiError(err); setError(e.status === 404 ? null : e); setLoaded(null); setContent('') } // 404: a new file (the button says 新建文件)
  }
  const listOrphans = async () => { const r = await ws.pruneFiles(true); if (r) setOrphans(r) }
  const prune = async () => { const r = await ws.pruneFiles(false); if (r) { setOrphans(r); setAckPrune(false) } }
  return h(Card, { title: '项目文件', extra: h('span', { className: 'wc-xs wc-mut' }, `${files.length} 个 · 引用 ${listOf(view.referenced).length} · 孤儿 ${listOf(view.orphans).length}`) },
    h('div', { className: 'wc-g2e' },
      h(Field, { label: '打开文件' }, h(Select, { value: files.includes(rel) ? rel : '', onChange: (v) => v && open(v), label: '文件', options: [['', '选一个…'], ...files.map((f) => [f, `${f}${listOf(view.orphans).includes(f) ? '（孤儿）' : ''}`])] })),
      h(Field, { label: '或新建 / 输入路径' }, h('div', { className: 'wc-fx', style: { flexWrap: 'nowrap' } }, h(TextInput, { value: rel, onChange: setRel, mono: true, placeholder: 'agent/baseline-prompt.md' }), h(Button, { small: true, disabled: !rel, onClick: () => open(rel) }, '打开')))),
    h(ErrorLine, { error }),
    rel ? h(Fragment, null,
      h('div', { className: 'wc-mt1' }, h(TextArea, { value: content, onChange: setContent, rows: 16, mono: true, label: rel })),
      h('div', { className: 'wc-fx wc-mt1' },
        h(Button, { kind: 'primary', disabled: content === loaded, busy: ws.busy === `file:${rel}`, onClick: () => ws.saveFile(rel, content).then((r) => { if (r) setLoaded(content) }) }, loaded === null ? '新建文件' : '保存文件'),
        h('span', { className: 'wc-xs wc-mut wc-mono' }, rel)),
      h(ErrorLine, { error: ws.errors[`file:${rel}`] })) : null,
    h('div', { className: 'wc-ln wc-mt', style: { paddingTop: 10 } },
      h('div', { className: 'wc-rf' }, h('span', { className: 'wc-b wc-sm' }, '没被 scenario 引用的文件'), h(Button, { small: true, busy: ws.busy === 'prune', onClick: listOrphans }, '列出')),
      orphans ? h(Fragment, null,
        h('div', { className: 'wc-sm wc-mono wc-break wc-mt1' }, listOf(orphans.orphans).join(' · ') || '没有孤儿文件'),
        listOf(orphans.untracked).length ? h(Note, null, `未跟踪（不会动）：${orphans.untracked.join(' · ')}`) : null,
        orphans.trash ? h(Note, { tone: 'ok' }, `已移到 ${orphans.trash}（可找回）`) : null,
        !orphans.trash && listOf(orphans.orphans).length ? h(Fragment, null,
          h(Check, { checked: ackPrune, onChange: setAckPrune }, `把这 ${orphans.orphans.length} 个文件移出项目（放进 generation/pruned，保留最近五批）。`),
          h('div', { className: 'wc-mt1' }, h(Button, { kind: 'danger', disabled: !ackPrune, busy: ws.busy === 'prune', onClick: prune }, '移出孤儿文件'))) : null) : null,
      h(ErrorLine, { error: ws.errors.prune })))
}

function CalibrationCard() {
  const ws = useWs()
  const [log, setLog] = useState('')
  const [result, setResult] = useState(null)
  const lc = ws.project.lastCalibration
  const run = async (dryRun) => { const r = await ws.calibrate(log, dryRun); if (r) setResult(r) }
  return h(Card, { title: '裁判噪声带标定（evaluation.noiseBand）', extra: lc ? h(Chip, { tone: verdictLabel(CAL_VERDICT, lc.verdict)[0] }, `上次 ${fmtNum(lc.noiseBand, 3)} · ${verdictLabel(CAL_VERDICT, lc.verdict)[1]}`) : h(Chip, null, '未标定') },
    h(Note, null, '在 Workshop 账号跑 13-judge-stability.sh，把输出（或每行一个分数）贴进来。裁判噪声带 = max(2σ, 极差, 0.02)；应用后当前构建失效。'),
    h('div', { className: 'wc-mt1' }, h(TextArea, { value: log, onChange: setLog, rows: 5, mono: true, placeholder: '例：\n    value = 0.83\n    value = 0.80\n    value = 0.86', label: '13-judge-stability.sh 输出' })),
    h('div', { className: 'wc-fx wc-mt1' },
      h(Button, { disabled: !log.trim(), busy: ws.busy === 'calibrate', onClick: () => run(true) }, '试算'),
      h(Button, { kind: 'primary', disabled: !result || result.applied || !log.trim(), onClick: () => run(false) }, '写入 scenario')),
    result ? h('div', { className: 'wc-fx wc-mt1' }, h(ToneChip, { map: CAL_VERDICT, value: result.verdict }),
      h('span', { className: 'wc-xs' }, `${result.runs} 次 · 均值 ${result.mean} · σ ${result.std} · 极差 ${result.spread} → noiseBand ${result.noiseBand}`), result.applied ? h(Chip, { tone: 'info' }, '已写入') : null) : null,
    h(ErrorLine, { error: ws.errors.calibrate }))
}

function ManualSyncCard() {
  const ws = useWs()
  const pf = ws.preflight
  const [ack, setAck] = useState(false)
  const job = ws.syncJob
  const active = jobActive(job) || jobActive(ws.oneclick)
  const plan = pf && pf.plan
  const [ackRollback, setAckRollback] = useState(false)
  // A release on the instance: an apply in the record, or a status check (e.g. after a job the restart cut off).
  const remote = ws.history.some((e) => (e.event === 'apply' && e.status !== 'refused') || e.event === 'status')
  const needs = !ws.project.target ? '先在「03 交付」配置 Workshop 环境' : !ws.project.lastBuild ? '先构建 Release（03 交付）' : '' // why 体检 is greyed out
  return h(Card, { title: '分步同步与回滚', sub: '一键覆盖的每个阶段都可以在这里单独执行；状态、确认保留和回滚在后台运行。' },
    h('div', { className: 'wc-b wc-sm' }, '1 · 只读体检'),
    h('div', { className: 'wc-fx wc-mt1' }, h(Button, { disabled: !ws.project.target || !ws.project.lastBuild || active, busy: ws.busy === 'preflight', onClick: ws.runPreflight, title: needs }, '体检'), pf ? h(Chip, { tone: pf.ok ? 'ok' : 'bad' }, pf.ok ? '通过' : '未通过') : null,
      needs ? h(Button, { small: true, kind: 'ghost', onClick: () => ws.go('3') }, `${needs} →`) : null),
    h(ErrorLine, { error: ws.errors.preflight }),
    h('div', { className: 'wc-b wc-sm wc-mt' }, '2 · 上传并应用（一次性令牌，15 分钟内有效）'),
    plan && pf.ok ? h(Fragment, null,
      h('div', { className: 'wc-sm wc-mt1 wc-break' }, '把 ', h('span', { className: 'wc-mono' }, plan.version), ' 上传到 ', h('span', { className: 'wc-mono' }, plan.sourceUri), '，在账号 ', h('span', { className: 'wc-mono' }, plan.accountId), `（${plan.region}）的实例 `, h('span', { className: 'wc-mono' }, plan.instanceId), ' 上运行 ', h('span', { className: 'wc-mono' }, plan.documentName), '，身份 ', h('span', { className: 'wc-mono' }, plan.identityArn), '。'),
      h('div', { className: 'wc-mt1' }, h(Check, { checked: ack, onChange: setAck }, '确认这是正确的 Workshop 账号，场景的 AWS 资源还没创建。')),
      h('div', { className: 'wc-mt1' }, h(Button, { kind: 'primary', disabled: !ack || active, busy: ws.busy === 'syncApply', onClick: () => ws.syncApply().then(() => setAck(false)) }, '应用 Release'))) : h(Note, null, pf && pf.unreachable ? '体检没跑成：原因见上面（多半是 profile 没登录），修好再体检。' : pf && !pf.ok ? '体检未通过：修好红色项再体检。' : '先体检通过，才能应用。'),
    h(ErrorLine, { error: ws.errors.syncApply }),
    h('div', { className: 'wc-b wc-sm wc-mt' }, '3 · 状态 / 确认保留 / 回滚'),
    h(Note, null, '应用后远端先用新版本并开恢复计时；冒烟通过再“确认保留”（不是 Git 提交）。“回滚”恢复上一个版本的文件，已创建的 AWS 资源不会回滚。'),
    h('div', { className: 'wc-fx wc-mt1' }, SYNC_ACTIONS.map(([action, label]) => h(Button, {
      key: action, kind: action === 'rollback' ? 'danger' : 'default', busy: ws.busy === `sync:${action}`,
      disabled: !ws.project.target || active || (action !== 'status' && !remote) || (action === 'rollback' && !ackRollback),
      title: action !== 'status' && !remote ? '还没有上传应用过：先应用 Release（远端状态不明就先“查看状态”）' : '',
      onClick: () => ws.startSyncJob(action).then(() => { if (action === 'rollback') setAckRollback(false) }),
    }, label))),
    remote ? h(Check, { checked: ackRollback, onChange: setAckRollback }, '要回滚：远端恢复上一个版本的文件；已创建的 AWS 资源不会回滚。')
      : h(Note, null, '还没有上传应用过：确认保留和回滚要等应用之后；远端状态不明就先“查看状态”。'),
    job ? h('div', { className: 'wc-fx wc-mt1' }, h(ToneChip, { map: JOB_STATUS, value: job.status }), h('span', { className: 'wc-sm' }, JOB_KIND[job.kind] || job.kind),
      job.result && syncResult(job.result) ? h(Chip, { tone: syncStatus(syncResult(job.result))[0] }, syncStatus(syncResult(job.result))[1]) : null, h('span', { className: 'wc-xs wc-mono wc-mut' }, job.id)) : null,
    job && !jobActive(job) && (job.result || job.error) ? h(Pre, null, JSON.stringify(job.result || { status: job.status, error: job.error }, null, 2)) : null,
    ws.lost.sync ? h(Note, { tone: 'warn' }, '和后端失联：任务还在服务端运行，点顶部“刷新”继续跟进。') : null,
    SYNC_ACTIONS.map(([a]) => h(ErrorLine, { key: a, error: ws.errors[`sync:${a}`] })),
    h('div', { className: 'wc-b wc-sm wc-mt' }, `同步记录（${ws.history.length}）`),
    ws.history.length ? h('div', { style: { maxHeight: 260, overflow: 'auto' } }, h(Table, {
      head: ['时间', '事件', '结果', '版本', '命令'],
      rows: ws.history.slice().reverse().map((e, i) => ({ key: i, cells: [h('span', { className: 'wc-xs' }, fmtTime(e.at)), h(Chip, null, SYNC_EVENT[e.event] || e.event),
        e.ok !== undefined && !syncResult(e) ? h(Chip, { tone: e.ok ? 'ok' : 'bad' }, e.ok ? '通过' : '未通过') : syncResult(e) ? h(Chip, { tone: syncStatus(syncResult(e))[0], title: e.reason || '' }, syncStatus(syncResult(e))[1]) : '',
        h('span', { className: 'wc-mono wc-xs' }, e.version || ''), h('span', { className: 'wc-mono wc-xs' }, e.commandId || '')] })),
    })) : h(Note, null, '还没有同步事件。'))
}

function ProjectRecordCard() {
  const ws = useWs()
  const p = ws.project
  const log = p.editLog || {}
  return h(Card, { title: '项目记录' },
    h(Kpi, null,
      h(Stat, { label: '手工保存 scenario', value: log.manualScenarioSaves || 0 }), h(Stat, { label: '手工保存文件', value: log.manualFileSaves || 0 }),
      h(Stat, { label: 'Kiro 应用', value: log.kiroApplies || 0 }), h(Stat, { label: 'Kiro 撤销', value: log.kiroReverts || 0 })),
    p.lastGeneration ? h(Note, null, `最近应用：${GEN_MODE[p.lastGeneration.mode] || p.lastGeneration.mode} 第 ${p.lastGeneration.round} 轮 · ${fmtTime(p.lastGeneration.at)} · ${p.lastGeneration.id}`) : null,
    h(Details, { summary: 'project.json（只读）' }, h(Pre, null, JSON.stringify({ ...p, scenarioYaml: undefined }, null, 2))))
}

function AdvancedPage() {
  const ws = useWs()
  return h(Fragment, null,
    h(StepHeader, { n: '高级', title: '原始文件与手动操作', h1: '直接改文件、标定裁判、分步同步', sub: '常规流程用不到这里；这里的手工改动都会计入项目记录。' }),
    holdingResult(ws) ? h('div', { className: 'wc-err wc-mt' }, holdText(ws, '保存这里的 YAML、文件或裁判噪声带', '再来改')) : null,
    h('div', { className: 'wc-g2e wc-mt' }, h('div', { className: 'wc-col' }, h(ScenarioYamlCard), h(CalibrationCard)), h('div', { className: 'wc-col' }, h(FileEditorCard), h(ManualSyncCard), h(ProjectRecordCard))))
}

// ---------------------------------------------------------------------------
// Workspace + root
// ---------------------------------------------------------------------------

function Workspace({ route, setRoute }) {
  const go = useCallback((step, tab) => setRoute((prev) => ({ ...prev, step, tab: tab || (prev.step === step ? prev.tab : '') })), [setRoute])
  const ws = useWorkspace(route.pid, go)
  const onBack = () => setRoute({ pid: '', step: '1', tab: '' })
  useEffect(() => {
    const root = document.querySelector('.wc-root')
    if (root) root.scrollTop = 0
    if (window.scrollTo) window.scrollTo(0, 0)
  }, [route.step])
  if (!ws.project) {
    const error = ws.res.project && ws.res.project.error
    return h('div', { className: 'wc-app' }, h('div', { className: 'wc-main' },
      error ? h(Fragment, null, h('div', { className: 'wc-err' }, `项目 ${route.pid} 打不开：${error.message}`), h('div', { className: 'wc-mt' }, h(Button, { icon: ArrowLeft, onClick: onBack }, '返回项目列表')))
        : h(Note, null, '正在加载项目…')))
  }
  const onTab = (tab) => setRoute((prev) => ({ ...prev, tab }))
  const page = route.step === '2' ? h(ContentStep, { tab: route.tab, onTab })
    : route.step === '3' ? h(DeliveryStep)
      : route.step === '4' ? h(RehearsalStep, { tab: route.tab, onTab })
        : route.step === 'adv' ? h(AdvancedPage)
          : h(ScenarioStep)
  return h(WsContext.Provider, { value: ws },
    h('div', { className: 'wc-app' },
      h(TopBar, { onBack }),
      h(ReadinessBar),
      h('div', { className: 'wc-body' },
        h(StepRail, { step: route.step }),
        h('main', { className: 'wc-main' }, page))))
}

const ROUTE_KEY = 'workshop-customizer:route'

function initialRoute() {
  // Deep link (#wc=<project>/<step>/<tab> or ?wc=…) wins; else the last position of this tab session.
  try {
    const fromUrl = (window.location.hash.match(/wc=([^&]+)/) || window.location.search.match(/wc=([^&]+)/) || [])[1]
    if (fromUrl) {
      const [pid, step, tab] = decodeURIComponent(fromUrl).split('/')
      return { pid: pid || '', step: step || '1', tab: tab || '' }
    }
    const saved = JSON.parse(window.sessionStorage.getItem(ROUTE_KEY) || 'null')
    if (saved && typeof saved.pid === 'string') return { pid: saved.pid, step: saved.step || '1', tab: saved.tab || '' }
  } catch (_) { /* no storage: start at the project list */ }
  return { pid: '', step: '1', tab: '' }
}

function AppShell() {
  const [route, setRoute] = useState(initialRoute)
  useEffect(() => { try { window.sessionStorage.setItem(ROUTE_KEY, JSON.stringify(route)) } catch (_) { /* ignore */ } }, [route])
  return route.pid
    ? h(Workspace, { key: route.pid, route, setRoute })
    : h(ProjectsPage, { onOpen: (pid) => setRoute({ pid, step: '1', tab: '' }) })
}

export default function WorkshopCustomizerApp() {
  const value = useApiValue()
  return h(ApiContext.Provider, { value },
    h('div', { className: 'wc-root overflow-y-auto flex-1 min-h-0', style: { overflowY: 'auto', flex: '1 1 auto', minHeight: 0 } },
      h('style', null, CSS),
      h(AppShell)))
}
