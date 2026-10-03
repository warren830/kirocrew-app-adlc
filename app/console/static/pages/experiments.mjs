// A/B 与发布: configuration bundles (an agent's control and treatment configurations), A/B tests that AgentCore splits at
// a Gateway (a treatment Harness beside the agent, one online evaluation per arm), the canary ramp, live results, and a
// promotion that evidence opens: a robust verification of the treatment and an A/B result not worse than the control
// beyond the evaluator's noise band (an admin may override, with a reason that is recorded). 代码 Agent 金丝雀: the same for a
// code agent the console deployed (console.runtime_canary): the candidate version as a runtime of its own beside
// production, two runtime targets at a Gateway, promotion as production's next version, rollback before or after it.
import React from 'react'
import { call, useCtx, listOf, Card, Button, Field, Input, TextArea, Select, Chip, Note, Empty, ErrorLine, Table, useAction, useJob, Tabs, JOB_TONE,
  JOB_LABEL } from '../ui.mjs'
import { useList } from './shared.mjs'

const { useState, useEffect, createElement: h, Fragment } = React
const ex = (wid, path) => `/workspaces/${wid}/experiments${path}`
const CANARY = [5, 25, 50]
const MIN_SAMPLES = 3
const STATUS = { creating: ['创建中', 'info'], running: ['运行中', 'ok'], paused: ['已暂停', 'warn'], stopped: ['已停止', ''], promoted: ['已发布', 'ok'],
  failed: ['失败', 'bad'], cleaning: ['清理中', 'warn'], cleaned: ['已清理', ''], promoting: ['发布中', 'info'], rolling_back: ['回滚中', 'warn'],
  rolled_back: ['已回滚', 'warn'], cleanup_incomplete: ['清理未完成', 'bad'] }
const LIVE = ['creating', 'running', 'paused', 'cleaning']
const GATE = { verification: '候选配置有一次稳定的控制台验证', ab: 'A/B：候选不比对照差到噪声带之外', agent: '实验期间比较的版本都没有被改动',
  metric: '按实验声明的指标决定' }
// the largest share a treatment gets before the gate holds (experiments.MAX_UNPROVEN): above it, it serves most sessions
const MAX_UNPROVEN = 50
const short = (s, n = 8) => (s ? String(s).slice(0, n) : '—')
const num = (v, d = 3) => (typeof v === 'number' ? v.toFixed(d) : '—')
const excerpt = (s, n = 90) => { const t = String(s || '').replace(/\s+/g, ' '); return t.length > n ? `${t.slice(0, n)}…` : t || '—' }
const split = (text) => text.split(/[\s,，]+/).filter(Boolean)

// Harnesses, without the treatment Harnesses experiments make (<agent>_x<8 hex>).
function useAgents(wid) {
  const [agents] = useList(wid, `/workspaces/${wid}/agents`, 'agents')
  return listOf(agents).filter((a) => a.kind === 'harness' && !/_x[0-9a-f]{8}$/.test(a.name || ''))
}

function StatusChip({ status }) {
  const [label, tone] = STATUS[status] || [status, '']
  return h(Chip, { tone }, label)
}

function JobLine({ id, title }) {
  const job = useJob(id)
  if (!job) return null
  return h(Card, { title: title || job.label, extra: h(Chip, { tone: JOB_TONE[job.status] }, JOB_LABEL[job.status] || job.status) },
    h('pre', { className: 'cs-pre' }, listOf(job.log).slice(-10).join('\n') || '…'), job.error ? h('div', { className: 'cs-err' }, job.error) : null)
}

// -- verifying a treatment (a console job of kind verify: the evaluation page lists it too) -----------------------------------

function VerifyTreatment({ body, title, path, note }) {
  const { wid } = useCtx()
  const [sets] = useList(wid, `/workspaces/${wid}/contract-sets`, 'contractSets')
  const [form, setForm] = useState({ contractSet: '', repeat: '3', panel: true })
  const [jobId, setJobId] = useState(null)
  const job = useJob(jobId)
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const start = () => run('verify', () => call('POST', path || ex(wid, '/verifications'), { ...body, contractSet: form.contractSet, repeat: Number(form.repeat), panel: form.panel }),
    '验证已开始').then((j) => j && setJobId(j.id))
  const doc = job && job.result
  return h(Card, { title: title || '验证候选配置', extra: h(Button, { kind: 'pri', onClick: start, busy: busy === 'verify', disabled: !form.contractSet }, '开始验证') },
    h('div', { className: 'cs-grid' },
      h(Field, { label: '契约集' }, h(Select, { value: form.contractSet, onChange: set('contractSet'), options: [['', '选择'], ...listOf(sets).map((s) => [s.id, `${s.name}（${s.count}）`])] })),
      h(Field, { label: '轮数' }, h(Select, { value: form.repeat, onChange: set('repeat'), options: [['1', '1'], ['2', '2'], ['3', '3'], ['5', '5']] })),
      h(Field, { label: 'AgentCore 评估器（测噪声带）' }, h(Select, { value: form.panel ? 'y' : 'n', onChange: (v) => set('panel')(v === 'y'), options: [['y', '一起跑'], ['n', '不跑（噪声带取最小值）']] }))),
    h(Note, null, note || '在 Agent 上逐次调用时换用候选的 Prompt / 模型，不改动 Agent。每轮每条契约一个新会话；每轮都通过才算稳定。评估器面板量出每个评估器的噪声带，发布门禁用它。'),
    job ? h(Note, { tone: doc ? (doc.robust ? 'ok' : 'warn') : null }, doc ? `${doc.holding}/${listOf(doc.contracts).length} 条契约每轮都通过（${doc.repeat} 轮）：${doc.robust ? '稳定' : '不稳定'}`
      : `${JOB_LABEL[job.status] || job.status}：${listOf(job.log).slice(-1)[0] || '…'}`) : null,
    job && job.error ? h('div', { className: 'cs-err' }, job.error) : null, h(ErrorLine, { error }))
}

// -- configuration bundles ---------------------------------------------------------------------------------------------------

function BundleDetail({ bid, onChange }) {
  const { wid } = useCtx()
  const [bundle, setBundle] = useState(null)
  const [form, setForm] = useState({ systemPrompt: '', model: '', commitMessage: '' })
  const [verifying, setVerifying] = useState(null)
  const { busy, error, run } = useAction()
  const load = () => call('GET', ex(wid, `/bundles/${bid}`)).then((b) => {
    setBundle(b)
    const latest = listOf(b.versions).find((v) => v.latest) || listOf(b.versions).slice(-1)[0]
    const conf = latest ? Object.values(latest.components || {})[0] || {} : {}
    setForm({ systemPrompt: conf.systemPrompt || '', model: conf.model || '', commitMessage: '' })
  }).catch((e) => run('load', () => Promise.reject(e)))
  useEffect(() => { setBundle(null); setVerifying(null); load() }, [bid])
  if (!bundle) return h(Fragment, null, h(ErrorLine, { error }), h(Empty, null, '加载中…'))
  const agentArn = bundle.agent && bundle.agent.arn
  const addVersion = () => run('add', () => call('POST', ex(wid, `/bundles/${bid}/versions`), { ...form, componentArn: agentArn || undefined }), (r) => `已加版本 ${short(r.versionId)}`)
    .then((r) => { if (r) { load(); onChange() } })
  const remove = () => window.confirm(`删除配置包 ${bundle.name}？`) && run('del', () => call('DELETE', ex(wid, `/bundles/${bid}`)), '已删除').then((r) => r && onChange(true))
  return h(Fragment, null,
    h(Card, { title: `配置包 ${bundle.name}`, extra: h('span', { className: 'cs-row' }, bundle.agent ? h(Chip, null, bundle.agent.name) : null,
      bundle.console ? h(Button, { kind: 'danger', onClick: remove, busy: busy === 'del' }, '删除') : null) },
    h(Table, { head: ['版本', '时间', '说明', 'Prompt', '模型', ''], rows: listOf(bundle.versions).map((v) => {
      const conf = (agentArn && v.components[agentArn]) || Object.values(v.components || {})[0] || {}
      return { key: v.versionId, cells: [h('span', { className: 'cs-mono', title: v.versionId }, short(v.versionId)), (v.createdAt || '').slice(0, 19),
        h('span', null, v.commitMessage || '—', v.versionId === bundle.controlVersion ? h(Chip, { tone: 'info' }, ' 对照') : null, v.latest ? h(Chip, null, ' 最新') : null),
        h('span', { title: conf.systemPrompt }, excerpt(conf.systemPrompt)), h('span', { className: 'cs-mono' }, conf.model || '—'),
        bundle.agent && v.versionId !== bundle.controlVersion ? h(Button, { onClick: () => setVerifying(v.versionId) }, '验证') : null] }
    }) }),
    bundle.truncated ? h(Note, null, '只显示最近 20 个版本。') : null,
    h(Note, null, '配置包是 AgentCore 的版本化配置（键是 Harness ARN，内容是 systemPrompt 与 modelId）。Harness 自己不读配置包：A/B 实验里候选配置由一个副本 Harness 运行。')),
    verifying ? h(VerifyTreatment, { title: `验证候选版本 ${short(verifying)}`, body: { agentId: bundle.agent.id, bundleId: bid, versionId: verifying } }) : null,
    // only the console's own bundles take a new version here: agent code that reads another team's bundle would run it
    bundle.console ? h(Card, { title: '在最新版本上加一个版本', extra: h(Button, { kind: 'pri', onClick: addVersion, busy: busy === 'add' }, '保存为新版本') },
      h(Field, { label: '模型' }, h(Input, { value: form.model, onChange: (v) => setForm((f) => ({ ...f, model: v })), mono: true })),
      h(Field, { label: '系统 Prompt' }, h(TextArea, { value: form.systemPrompt, onChange: (v) => setForm((f) => ({ ...f, systemPrompt: v })), rows: 8 })),
      h(Field, { label: '版本说明（可选）' }, h(Input, { value: form.commitMessage, onChange: (v) => setForm((f) => ({ ...f, commitMessage: v })) })))
      : h(Note, null, '这个配置包不是控制台建的：不在这里给它加版本（读它最新版本的 Agent 代码会直接用上新版本）。要比较新的配置，从 Agent 的当前配置新建一个配置包。'),
    h(ErrorLine, { error }))
}

function Bundles() {
  const { wid } = useCtx()
  const agents = useAgents(wid)
  const [bundles, load, listError] = useList(wid, ex(wid, '/bundles'), 'bundles')
  const [open, setOpen] = useState(null)
  const [form, setForm] = useState({ agentId: '', name: '', systemPrompt: '', model: '', commitMessage: '' })
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  useEffect(() => {
    if (!form.agentId) return
    call('GET', `/workspaces/${wid}/agents/harness/${form.agentId}`).then((a) => setForm((f) => ({ ...f, systemPrompt: a.systemPrompt || '', model: a.model || '' }))).catch(() => {})
  }, [form.agentId])
  const create = () => run('create', () => call('POST', ex(wid, '/bundles'), { ...form, name: form.name || undefined }),
    (r) => (r.treatmentVersion ? `已建配置包：对照 ${short(r.controlVersion)}，候选 ${short(r.treatmentVersion)}` : '已建配置包（只有当前配置这一版）'))
    .then((r) => { if (r) { load(); setOpen(r.id) } })
  return h(Fragment, null,
    h(Card, { title: '配置包', extra: h(Button, { onClick: load }, '刷新') }, h(ErrorLine, { error: listError }), bundles === null ? h(Empty, null, '加载中…') : h(Table, {
      head: ['名称', 'Agent', '创建', '来源'], rows: bundles.map((b) => ({ key: b.id, onClick: () => setOpen(b.id), cells: [h('b', null, b.name), b.agent ? b.agent.name : '—',
        (b.createdAt || '').slice(0, 19), b.console ? h(Chip, { tone: 'info' }, '控制台') : '其他'] })), empty: '还没有配置包。' })),
    open ? h(BundleDetail, { bid: open, onChange: (gone) => { load(); if (gone === true) setOpen(null) } }) : null,
    h(Card, { title: '从 Agent 的当前配置新建（第一版 = 对照；改了 Prompt 或模型就再存一版 = 候选）',
      extra: h(Button, { kind: 'pri', onClick: create, busy: busy === 'create', disabled: !form.agentId }, '新建') },
    h('div', { className: 'cs-grid' },
      h(Field, { label: 'Agent（Harness）' }, h(Select, { value: form.agentId, onChange: set('agentId'), options: [['', '选择'], ...agents.map((a) => [a.id, a.name])] })),
      h(Field, { label: '名称（可选）' }, h(Input, { value: form.name, onChange: set('name'), mono: true, placeholder: '字母开头，字母、数字或下划线' })),
      h(Field, { label: '候选的模型' }, h(Input, { value: form.model, onChange: set('model'), mono: true }))),
    h(Field, { label: '候选的系统 Prompt（已填入当前的，改动它）' }, h(TextArea, { value: form.systemPrompt, onChange: set('systemPrompt'), rows: 8 })),
    h(Field, { label: '候选版本说明（可选）' }, h(Input, { value: form.commitMessage, onChange: set('commitMessage') })),
    h(ErrorLine, { error })))
}

// -- the gate -------------------------------------------------------------------------------------------------------------

function said(c, rec) {
  if (c.id === 'verification') {
    const v = c.verification
    if (!v) return c.ok ? c.evidence : '还没有对这个候选配置的控制台验证'
    if (v.agentVersion && rec && v.agentVersion !== rec.agent.version) return `验证 ${v.id} 跑在版本 ${v.agentVersion} 上，实验比较的是版本 ${rec.agent.version}：再验证一次`
    return v.robust ? `验证 ${v.id}：${v.contracts} 条契约在 ${v.repeat} 轮里每轮都通过` : `验证 ${v.id}：${v.holding}/${v.contracts} 条契约每轮都通过（${v.repeat} 轮），不稳定`
  }
  if (c.id === 'ab') {
    if (!c.control) return `还没有 ${c.metric} 的 A/B 结果（AgentCore 在会话结束后约 15 分钟发布）`
    if (c.delta === undefined) return `样本太少：对照 ${c.control.n}，候选 ${c.treatment.n}（每组至少 ${MIN_SAMPLES}）`
    return `${c.metric}：对照 ${num(c.control.mean)}（n=${c.control.n}），候选 ${num(c.treatment.mean)}（n=${c.treatment.n}），Δ ${c.delta >= 0 ? '+' : ''}${num(c.delta)} ${c.worse ? '<' : '≥'} −${c.band}（噪声带${/measured/.test(c.bandSource) ? '，验证面板测得' : '，最小值：面板没测这个评估器'}）`
  }
  if (c.id === 'metric') return `实验声明按 ${c.declared} 决定；改按 ${c.metric} 决定算覆盖门禁（会记录）`
  if (c.ok) return `Agent 仍是实验比较的版本 ${c.compared || (rec ? rec.agent.version : '')}，对照端点和候选副本也没动`
  if (c.arm) {
    const a = c.arm
    if (a.now === null || a.now === undefined) return `${a.what} 不在了：A/B 比较的是版本 ${a.expected}`
    if (a.now === a.expected) return `${a.what} 正从 A/B 比较的版本 ${a.expected} 移到版本 ${a.target}`
    return `${a.what} 现在是版本 ${a.now}${a.target && a.target !== a.now ? `（正移到 ${a.target}）` : ''}，A/B 比较的是版本 ${a.expected}：证据说的是另一个版本`
  }
  return c.compared && c.now !== c.compared ? `Agent 在实验期间从版本 ${c.compared} 变成了 ${c.now ?? '（读不到）'}：A/B 比较的已不是它现在的配置` : c.evidence
}

function Gate({ rec, gate, onDone }) {
  const { wid, me } = useCtx()
  const [reason, setReason] = useState('')
  const [override, setOverride] = useState(false)
  const [refused, setRefused] = useState(null)
  const { busy, error, run } = useAction()
  const shown = (refused && refused.gate) || gate
  if (!shown) return null
  const admin = me && me.role === 'admin'
  const promote = () => window.confirm(override ? '在门禁没通过时发布：覆盖会连同原因记录下来。继续？' : `把候选配置发布到 ${rec.agent.name}（新版本，DEFAULT 立即使用）？`) &&
    run('promote', () => call('POST', ex(wid, `/ab-tests/${rec.id}/promote`), override ? { acknowledged: true, reason } : {}).catch((err) => {
      if (err.data && err.data.gate) setRefused(err.data)
      throw err
    }), (r) => `已发布：${r.agent} 版本 ${r.fromVersion} → ${r.toVersion}`).then((r) => r && onDone())
  const title = shown.decidedAt ? `发布门禁（发布时 ${String(shown.decidedAt).slice(0, 19)} 的证据）` : '发布门禁（证据决定，不是点一下）'
  const verdict = shown.decidedAt ? (shown.ok ? '当时通过' : '当时没通过') : (shown.ok ? '可以发布' : '不能发布')
  return h(Card, { title, extra: h(Chip, { tone: shown.ok ? 'ok' : 'bad' }, verdict) },
    listOf(shown.conditions).map((c) => h('div', { key: c.id, className: 'cs-note' }, h(Chip, { tone: c.ok ? 'ok' : 'bad' }, c.ok ? '✓' : '✗'), ' ', h('b', null, GATE[c.id] || c.id), '：',
      h('span', { title: c.evidence }, said(c, rec)))),
    rec.promotion ? h(Note, { tone: 'ok' }, `已于 ${(rec.promotion.at || '').slice(0, 19)} 由 ${rec.promotion.by} 发布：版本 ${rec.promotion.fromVersion} → ${rec.promotion.toVersion}${rec.promotion.override ? `（覆盖了门禁：${rec.promotion.reason}）` : ''}`)
      : !admin ? h(Note, null, '发布需要管理员。')
        : h(Fragment, null,
          !shown.ok ? h('label', { className: 'cs-row' }, h('input', { type: 'checkbox', checked: override, onChange: (e) => setOverride(e.target.checked) }), '管理员覆盖：证据不足也发布（会记录覆盖、没通过的条件和原因）') : null,
          override && !shown.ok ? h(Field, { label: '原因（必填）' }, h(TextArea, { value: reason, onChange: setReason, rows: 2 })) : null,
          h(Button, { kind: 'pri', onClick: promote, busy: busy === 'promote', disabled: !shown.ok && !(override && reason.trim().length >= 5) }, '发布候选配置（100%）')),
    h(ErrorLine, { error }))
}

// -- one experiment --------------------------------------------------------------------------------------------------------

function Results({ rec }) {
  const rows = listOf(rec.metrics)
  return h(Card, { title: `${rec.live && rec.live.final ? '最终结果' : '实时结果'}（${rec.live && rec.live.analysisTimestamp ? `AgentCore 分析于 ${String(rec.live.analysisTimestamp).slice(0, 19)}` : '还没有分析'}）` },
    h(Table, { head: ['评估器', '对照 均分（n）', '候选 均分（n）', 'Δ（越大越好）', '噪声带', 'p 值', '显著'], rows: rows.map((m) => {
      const delta = typeof m.treatment.mean === 'number' && typeof m.control.mean === 'number' ? m.polarity * (m.treatment.mean - m.control.mean) : null
      return { key: m.evaluator, cells: [h('b', null, m.evaluator, m.evaluator === rec.metric ? h(Chip, { tone: 'info' }, ' 门禁指标') : null), `${num(m.control.mean)}（${m.control.n}）`,
        `${num(m.treatment.mean)}（${m.treatment.n}）`, delta === null ? '—' : h(Chip, { tone: delta < -m.band ? 'bad' : delta > m.band ? 'ok' : '' }, `${delta >= 0 ? '+' : ''}${num(delta)}`),
        `±${m.band}`, typeof m.treatment.pValue === 'number' && m.treatment.pValue < 0.001 ? '<0.001' : num(m.treatment.pValue), m.treatment.significant ? '是' : '否'] }
    }), empty: '还没有结果：先经实验网关发流量，AgentCore 在会话结束后约 15 分钟发布（轨迹级评估器按每轮计数）。' }))
}

function Ramp({ rec, reload, base }) {
  const { wid, me } = useCtx()
  const admin = Boolean(me && me.role === 'admin')
  const at = base || ex(wid, `/ab-tests/${rec.id}`)
  const { busy, error, run } = useAction()
  const live = rec.live || {}
  const t1 = (live.weights && live.weights.T1) || rec.weights.T1
  const active = ['running', 'paused'].includes(rec.status)
  const go = (weight, acknowledged) => run(`w${weight}`, () => call('POST', `${at}/split`, { treatmentWeight: weight, ...(acknowledged ? { acknowledged: true } : {}) })
    .catch((err) => {
      if (err.data && err.data.condition && window.confirm(`候选比对照差到噪声带之外，仍要把它的流量提到 ${weight}%？`)) return call('POST', `${at}/split`, { treatmentWeight: weight, acknowledged: true })
      throw err
    }), `候选流量 ${weight}%`).then((r) => r && reload())
  const state = (s, label) => run(s, () => call('POST', `${at}/state`, { executionStatus: s }), label).then((r) => r && reload())
  return h(Card, { title: '金丝雀（候选拿到的会话比例）', extra: active ? h('span', { className: 'cs-row' },
    rec.status === 'running' ? h(Button, { onClick: () => state('PAUSED', '已暂停：所有会话走对照'), busy: busy === 'PAUSED' }, '暂停') : h(Button, { onClick: () => state('RUNNING', '已继续'), busy: busy === 'RUNNING' }, '继续'),
    h(Button, { kind: 'danger', onClick: () => window.confirm('停止 A/B 测试？结果会保留，但不能再继续。') && state('STOPPED', '已停止'), busy: busy === 'STOPPED' }, '停止')) : null },
  h('div', { className: 'cs-row' }, [...CANARY, 100].map((w, i) => h(Fragment, { key: w }, i ? h('span', { className: 'cs-mut' }, '→') : null,
    w === 100 ? h(Chip, { tone: rec.status === 'promoted' ? 'ok' : '' }, '100%：发布（见门禁）')
      : h(Button, { kind: w === t1 ? 'pri' : undefined, onClick: () => go(w), busy: busy === `w${w}`, disabled: !active || w === t1 || !admin }, `${w}%`)))),
  h(Note, null, `现在：对照 ${100 - t1}%，候选 ${t1}%（${live.executionStatus || rec.status}）。改权重要先暂停再恢复，约十几秒；暂停时所有会话都走对照。候选更差（超出噪声带）时不能往上提，除非确认。`
    + `改权重需要管理员；门禁通过之前候选最多 ${MAX_UNPROVEN}%（再往上就是没有证据的发布）。`),
  h(ErrorLine, { error }))
}

function Traffic({ rec, base, hint }) {
  const { wid } = useCtx()
  const at = base || ex(wid, `/ab-tests/${rec.id}`)
  const [sets] = useList(wid, `/workspaces/${wid}/contract-sets`, 'contractSets')
  const [form, setForm] = useState({ contractSet: '', prompts: '', repeat: '1' })
  const [jobId, setJobId] = useState(null)
  const job = useJob(jobId)
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const send = () => run('send', () => call('POST', `${at}/traffic`, { repeat: Number(form.repeat),
    ...(form.contractSet ? { contractSet: form.contractSet } : { prompts: form.prompts.split('\n').map((p) => p.trim()).filter(Boolean) }) }), '流量已开始').then((j) => j && setJobId(j.id))
  const result = job && job.result
  return h(Card, { title: '经实验网关发流量', extra: h(Button, { kind: 'pri', onClick: send, busy: busy === 'send', disabled: !['running', 'paused'].includes(rec.status) || (!form.contractSet && !form.prompts.trim()) }, '发送') },
    h('div', { className: 'cs-grid' },
      h(Field, { label: '契约集的问题' }, h(Select, { value: form.contractSet, onChange: set('contractSet'), options: [['', '不用，手写'], ...listOf(sets).map((s) => [s.id, `${s.name}（${s.count}）`])] })),
      h(Field, { label: '每个问题几次' }, h(Select, { value: form.repeat, onChange: set('repeat'), options: [['1', '1'], ['2', '2'], ['3', '3'], ['5', '5']] }))),
    form.contractSet ? null : h(Field, { label: '问题（每行一个）' }, h(TextArea, { value: form.prompts, onChange: set('prompts'), rows: 4 })),
    h(Note, null, '每个问题一个新会话，由 AgentCore Gateway 按权重分给对照或候选（同一会话始终在同一组）。只有经过这个网关的会话才参与 A/B：'),
    rec.invokeUrl ? h('pre', { className: 'cs-pre' }, `POST ${rec.invokeUrl}\n# SigV4（服务名 bedrock-agentcore），${hint || '请求体与 InvokeHarness 相同：{"messages": [...], "actorId": "..."}'}\n# 头 X-Amzn-Bedrock-AgentCore-Runtime-Session-Id：会话 ID（33-100 字符）`) : null,
    job ? h(Note, { tone: result ? (result.failed ? 'warn' : 'ok') : null }, result ? `发出 ${result.sent} 个会话，失败 ${result.failed}` : `${JOB_LABEL[job.status] || job.status}…`) : null,
    result ? h(Table, { head: ['问题', '回答', '错误'], rows: listOf(result.samples).map((s) => ({ key: s.sessionId, cells: [excerpt(s.query, 50), excerpt(s.answer, 80), s.error ? excerpt(s.error, 80) : '—'] })) }) : null,
    job && job.error ? h('div', { className: 'cs-err' }, job.error) : null, h(ErrorLine, { error }))
}

function ExperimentDetail({ eid, onChange }) {
  const { wid } = useCtx()
  const [rec, setRec] = useState(null)
  const { busy, error, run } = useAction()
  const load = () => call('GET', ex(wid, `/ab-tests/${eid}`)).then(setRec).catch((e) => run('load', () => Promise.reject(e)))
  useEffect(() => { setRec(null); load() }, [eid])
  useEffect(() => {
    if (!rec || !LIVE.includes(rec.status)) return undefined
    const timer = setInterval(load, rec.status === 'creating' || rec.status === 'cleaning' ? 4000 : 15000)
    return () => clearInterval(timer)
  }, [rec && rec.status])
  if (!rec) return h(Fragment, null, h(ErrorLine, { error }), h(Empty, null, '加载中…'))
  const reload = () => { load(); onChange() }
  const cleanup = () => window.confirm('清理这个实验在 AWS 上建的一切（A/B 测试、网关、在线评估、候选 Harness、对照端点、角色）？结果和发布记录保留。') &&
    run('clean', () => call('DELETE', ex(wid, `/ab-tests/${eid}`)), '清理已开始').then((r) => r && setTimeout(reload, 500))
  const v = rec.verification
  return h(Fragment, null,
    h(Card, { title: rec.name, extra: h('span', { className: 'cs-row' }, h(StatusChip, { status: rec.status }), rec.live && rec.live.executionStatus ? h(Chip, null, rec.live.executionStatus) : null,
      ['creating', 'cleaning', 'cleaned'].includes(rec.status) || (rec.status === 'promoted' && rec.cleanedAt) ? null : h(Button, { kind: 'danger', onClick: cleanup, busy: busy === 'clean' }, '清理')) },
    h(Table, { head: ['项', '对照', '候选'], rows: [
      ['运行在', `${rec.agent.name} 版本 ${rec.agent.version}（端点 ${rec.controlEndpoint || '—'}，DEFAULT 不动）`, rec.treatmentHarness ? `${rec.treatmentHarness.name}（副本 Harness）` : '—'],
      ['配置包版本', h('span', { className: 'cs-mono' }, short(rec.bundle.control)), h('span', { className: 'cs-mono' }, short(rec.bundle.treatment))],
      ['模型', rec.control.model || '—', rec.treatment.model || '—'],
      ['Prompt', h('span', { title: rec.control.systemPrompt }, excerpt(rec.control.systemPrompt)), h('span', { title: rec.treatment.systemPrompt }, excerpt(rec.treatment.systemPrompt))],
      ['权重', `${rec.weights.C}%`, `${rec.weights.T1}%`],
    ].map((cells, i) => ({ key: i, cells })) }),
    h(Note, null, `评估器：${listOf(rec.evaluators).join('，')}；门禁指标 ${rec.metric}。${rec.live && rec.live.expiresAt ? `A/B 最长运行到 ${String(rec.live.expiresAt).slice(0, 19)}。` : ''}`),
    rec.error ? h('div', { className: 'cs-err' }, rec.error) : null, h(ErrorLine, { error })),
    rec.status === 'creating' || rec.status === 'failed' ? h(JobLine, { id: rec.job, title: '创建实验' }) : null,
    listOf(rec.cleanup).length ? h(Card, { title: '清理结果' }, h(Table, { head: ['资源', '结果'], rows: rec.cleanup.map((c, i) => ({ key: i, cells: [c.resource, c.result] })) })) : null,
    rec.status === 'creating' || rec.status === 'failed' ? null : h(Fragment, null,
      h(Ramp, { rec, reload }),
      h(Results, { rec }),
      h(Gate, { rec, gate: rec.gate, onDone: reload }),
      !v || !v.robust ? h(VerifyTreatment, { title: '验证候选配置（门禁条件一）', body: { experiment: rec.id } }) : null,
      ['running', 'paused'].includes(rec.status) ? h(Traffic, { rec }) : null))
}

// -- experiments -----------------------------------------------------------------------------------------------------------

function NewExperiment({ onCreated }) {
  const { wid } = useCtx()
  const agents = useAgents(wid)
  const [bundles] = useList(wid, ex(wid, '/bundles'), 'bundles')
  const [versions, setVersions] = useState([])
  const [form, setForm] = useState({ agentId: '', bundleId: '', controlVersion: '', treatmentVersion: '', treatmentWeight: '5',
    evaluators: 'Builtin.Correctness, Builtin.Helpfulness', metric: '', acknowledged: false })
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const mine = listOf(bundles).filter((b) => !form.agentId || !b.agent || b.agent.id === form.agentId)
  useEffect(() => {
    if (!form.bundleId) { setVersions([]); return }
    call('GET', ex(wid, `/bundles/${form.bundleId}`)).then((b) => {
      const list = listOf(b.versions)
      setVersions(list)
      setForm((f) => ({ ...f, controlVersion: b.controlVersion || (list[0] || {}).versionId || '', treatmentVersion: (list.slice(-1)[0] || {}).versionId || '' }))
    }).catch(() => setVersions([]))
  }, [form.bundleId])
  const evaluators = split(form.evaluators)
  const create = () => run('create', () => call('POST', ex(wid, '/ab-tests'), { agentId: form.agentId, bundleId: form.bundleId, controlVersion: form.controlVersion,
    treatmentVersion: form.treatmentVersion, treatmentWeight: Number(form.treatmentWeight), evaluators, metric: form.metric || evaluators[0], acknowledged: form.acknowledged }),
  '实验创建中（约一两分钟）').then((r) => r && onCreated(r.experiment.id))
  const versionOptions = versions.map((v) => [v.versionId, `${short(v.versionId)} · ${(v.createdAt || '').slice(0, 16)} · ${excerpt(v.commitMessage, 40)}`])
  return h(Card, { title: '新建 A/B 实验', extra: h(Button, { kind: 'pri', onClick: create, busy: busy === 'create',
    disabled: !form.agentId || !form.bundleId || !form.acknowledged || form.controlVersion === form.treatmentVersion }, '创建') },
  h('div', { className: 'cs-grid' },
    h(Field, { label: 'Agent（Harness）' }, h(Select, { value: form.agentId, onChange: set('agentId'), options: [['', '选择'], ...agents.map((a) => [a.id, a.name])] })),
    h(Field, { label: '配置包' }, h(Select, { value: form.bundleId, onChange: set('bundleId'), options: [['', '选择'], ...mine.map((b) => [b.id, b.name])] })),
    h(Field, { label: '对照版本（须是 Agent 现在的配置）' }, h(Select, { value: form.controlVersion, onChange: set('controlVersion'), options: [['', '选择'], ...versionOptions] })),
    h(Field, { label: '候选版本' }, h(Select, { value: form.treatmentVersion, onChange: set('treatmentVersion'), options: [['', '选择'], ...versionOptions] })),
    h(Field, { label: '候选起始流量' }, h(Select, { value: form.treatmentWeight, onChange: set('treatmentWeight'), options: [['5', '5%（金丝雀）'], ['25', '25%'], ['50', '50%（经典 A/B）']] })),
    h(Field, { label: '评估器（逗号分隔，最多 5 个）' }, h(Input, { value: form.evaluators, onChange: set('evaluators'), mono: true })),
    h(Field, { label: '决定发布的指标' }, h(Select, { value: form.metric || evaluators[0] || '', onChange: set('metric'), options: evaluators.filter((e) => e !== 'Builtin.Refusal').map((e) => [e, e]) }))),
  h(Note, null, '会在 AWS 上建：候选 Harness（Agent 的副本，只换 Prompt / 模型，共用它的 Memory）、Agent 上一个固定在当前版本的对照端点、一个 IAM 角色、一个 AgentCore Gateway 和两个直通目标、每组一个在线评估（100% 抽样）、一个按目标分流的 A/B 测试。生产（DEFAULT）在发布前不变。'),
  h('label', { className: 'cs-row' }, h('input', { type: 'checkbox', checked: form.acknowledged, onChange: (e) => set('acknowledged')(e.target.checked) }),
    '我知道这些资源会一直存在（在线评估按评估的会话计费），直到我清理'),
  h(ErrorLine, { error }))
}

function Experiments() {
  const { wid } = useCtx()
  const [list, load, listError] = useList(wid, ex(wid, '/ab-tests'), 'experiments')
  const [open, setOpen] = useState(null)
  return h(Fragment, null,
    h(Card, { title: 'A/B 实验', extra: h(Button, { onClick: load }, '刷新') }, h(ErrorLine, { error: listError }), list === null ? h(Empty, null, '加载中…') : h(Table, {
      head: ['名称', 'Agent', '状态', '对照 / 候选', '门禁指标', '创建'], rows: list.map((e) => ({ key: e.id, onClick: () => setOpen(e.id), cells: [h('b', null, e.name),
        e.agent.name, h(StatusChip, { status: e.status }), `${e.weights.C}% / ${e.weights.T1}%`, e.metric, `${(e.createdAt || '').slice(0, 16)} ${e.createdBy || ''}`] })),
      empty: '还没有实验。先在「配置包」里从 Agent 的当前配置建一个对照和候选。' })),
    open ? h(ExperimentDetail, { eid: open, onChange: load }) : null,
    h(NewExperiment, { onCreated: (id) => { load(); setOpen(id) } }))
}

function Releases() {
  const { wid } = useCtx()
  const [list, load, listError] = useList(wid, ex(wid, '/promotions'), 'promotions')
  return h(Card, { title: '发布记录', extra: h(Button, { onClick: load }, '刷新') }, h(ErrorLine, { error: listError }), list === null ? h(Empty, null, '加载中…') : h(Table, {
    head: ['时间', 'Agent', '版本', '改了', '门禁', '操作人'], rows: list.map((p, i) => ({ key: `${p.experiment}-${i}`, cells: [(p.at || '').slice(0, 19),
      `${p.agent}${p.kind === 'runtime-canary' ? '（金丝雀）' : ''}`, `${p.fromVersion} → ${p.toVersion}`, listOf(p.changed).join('，'),
      h(Fragment, null, p.kind === 'runtime-canary-rollback' ? h(Chip, { tone: 'warn', title: p.reason || '' }, `回滚${p.reason ? `：${excerpt(p.reason, 40)}` : ''}`)
        : p.override ? h(Chip, { tone: 'warn', title: p.reason }, `覆盖（${listOf(p.failed).join('，')}）：${excerpt(p.reason, 40)}`) : h(Chip, { tone: 'ok' }, `通过（${p.metric}）`),
      p.incomplete ? h(Chip, { tone: 'bad', title: p.incomplete }, ' 没走完：生产已换版本') : null), p.by] })),
    empty: '还没有发布。' }))
}

// -- 代码 Agent 金丝雀 (console.runtime_canary) ---------------------------------------------------------------------------------

const rcPath = (wid, path = '') => ex(wid, `/runtime-canaries${path}`)
const CANARY_GATE = { verification: '候选 Runtime 有一次稳定的控制台验证', ab: 'A/B：候选不比生产差到噪声带之外', agent: '金丝雀期间比较的版本都没有被改动',
  metric: '按金丝雀声明的指标决定' }
const CANARY_LIVE = ['creating', 'running', 'paused', 'promoting', 'rolling_back', 'cleaning']
const MAX_ZIP = 18 * 1024 * 1024
const CANDIDATE_SOURCES = { code: [['zip', 'Python 代码 zip']], container: [['dockerfile', 'Dockerfile 构建目录 zip（构建到生产的镜像仓库）'], ['image', '生产镜像仓库里的镜像']] }

function readBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || '').split(',')[1] || '')
    reader.onerror = () => reject(reader.error || new Error('读取文件失败'))
    reader.readAsDataURL(file)
  })
}

function parseEnv(text) {
  const env = {}
  for (const raw of String(text || '').split('\n')) {
    const line = raw.trim()
    if (!line || line.startsWith('#')) continue
    const cut = line.indexOf('=')
    if (cut <= 0) throw new Error(`环境变量一行一个 KEY=VALUE：${line}`)
    env[line.slice(0, cut).trim()] = line.slice(cut + 1)
  }
  return env
}

function ProductionState({ rec }) {
  const state = rec.production
  if (!state) return null
  const own = Object.entries(state.endpoints || {}).filter(([name]) => name !== rec.controlEndpoint)
  const moved = state.version !== rec.agent.version && !rec.promotion
  return h(Note, { tone: moved ? 'warn' : 'ok' }, `生产现在：DEFAULT → 版本 ${state.version}${own.filter(([n]) => n !== 'DEFAULT').map(([n, v]) => `，${n} → 版本 ${v}`).join('')}。`,
    rec.promotion ? `发布把它从版本 ${rec.promotion.fromVersion} 换到了 ${rec.promotion.toVersion}。` : moved ? `金丝雀比较的是版本 ${rec.agent.version}：生产在金丝雀之外被改了。` : '发布之前生产不动（候选是旁边的另一个 Runtime）。')
}

function CanaryGate({ rec, gate, onDone }) {
  const { wid, me } = useCtx()
  const [reason, setReason] = useState('')
  const [override, setOverride] = useState(false)
  const [refused, setRefused] = useState(null)
  const { busy, error, run } = useAction()
  const shown = (refused && refused.gate) || gate
  if (!shown) return null
  const admin = me && me.role === 'admin'
  const target = rec.agent.endpoint ? `DEFAULT 和端点 ${rec.agent.endpoint}` : 'DEFAULT'
  const promote = () => window.confirm(override ? '在门禁没通过时发布：覆盖会连同原因记录下来。继续？' : `把候选发布成 ${rec.agent.name} 的下一个版本（${target} 立即使用它）？`) &&
    run('promote', () => call('POST', rcPath(wid, `/${rec.id}/promote`), override ? { acknowledged: true, reason } : {}).catch((err) => {
      if (err.data && err.data.gate) setRefused(err.data)
      throw err
    }), '发布已开始').then((r) => r && onDone())
  const title = shown.decidedAt ? `发布门禁（发布时 ${String(shown.decidedAt).slice(0, 19)} 的证据）` : '发布门禁（证据决定，不是点一下）'
  const verdict = shown.decidedAt ? (shown.ok ? '当时通过' : '当时没通过') : (shown.ok ? '可以发布' : '不能发布')
  const open = ['running', 'paused', 'stopped'].includes(rec.status)
  const p = rec.promotion
  return h(Card, { title, extra: h(Chip, { tone: shown.ok ? 'ok' : 'bad' }, verdict) },
    listOf(shown.conditions).map((c) => h('div', { key: c.id, className: 'cs-note' }, h(Chip, { tone: c.ok ? 'ok' : 'bad' }, c.ok ? '✓' : '✗'), ' ',
      h('b', null, CANARY_GATE[c.id] || c.id), '：',
      h('span', { title: c.evidence }, c.id === 'verification' && !c.verification ? (c.ok ? c.evidence : '还没有对候选 Runtime 的控制台验证') : said(c, rec)))),
    p ? h(Note, { tone: 'ok' }, `已于 ${(p.at || '').slice(0, 19)} 由 ${p.by} 发布：版本 ${p.fromVersion} → ${p.toVersion}${p.endpoint ? `（端点 ${p.endpoint} 一起移过去）` : ''}${p.override ? `（覆盖了门禁：${p.reason}）` : ''}${p.incomplete ? `；没走完：${p.incomplete}` : ''}`)
      : !open ? null : !admin ? h(Note, null, '发布需要管理员。')
        : h(Fragment, null,
          !shown.ok ? h('label', { className: 'cs-row' }, h('input', { type: 'checkbox', checked: override, onChange: (e) => setOverride(e.target.checked) }), '管理员覆盖：证据不足也发布（会记录覆盖、没通过的条件和原因）') : null,
          override && !shown.ok ? h(Field, { label: '原因（必填）' }, h(TextArea, { value: reason, onChange: setReason, rows: 2 })) : null,
          h(Button, { kind: 'pri', onClick: promote, busy: busy === 'promote', disabled: !shown.ok && !(override && reason.trim().length >= 5) }, `发布候选（${target} 换到新版本）`)),
    h(ErrorLine, { error }))
}

function Rollback({ rec, onDone }) {
  const { wid, me } = useCtx()
  const [reason, setReason] = useState('')
  const { busy, error, run } = useAction()
  const rb = rec.rollback
  if (rb) {
    return h(Card, { title: '回滚', extra: h(Chip, { tone: 'warn' }, '已回滚') }, rb.of === 'the promotion'
      ? h(Note, null, `${(rb.at || '').slice(0, 19)} ${rb.by}：${rb.endpoint ? `端点 ${rb.endpoint} 移回版本 ${rb.endpointVersion}；` : ''}DEFAULT 只随新版本移动，于是把版本 ${rb.restoredFrom} 的制品和变量再发布成版本 ${rb.defaultVersion}。${rb.reason ? `原因：${rb.reason}` : ''}`)
      : h(Note, { tone: rb.moved ? 'warn' : 'ok' }, `${(rb.at || '').slice(0, 19)} ${rb.by}：停止分流，生产没有动过（DEFAULT → 版本 ${rb.production.version}${Object.entries(rb.production.endpoints || {}).filter(([n]) => n !== 'DEFAULT' && n !== rec.controlEndpoint).map(([n, v]) => `，${n} → ${v}`).join('')}）。${rb.reason ? `原因：${rb.reason}` : ''}`))
  }
  const before = ['running', 'paused', 'stopped'].includes(rec.status)
  if (!(before || rec.status === 'promoted') || !(me && me.role === 'admin')) return null
  const go = () => window.confirm(before ? '回滚金丝雀：停止分流（结果保留），生产保持原样。继续？' : `撤销发布：${rec.agent.endpoint ? `端点 ${rec.agent.endpoint} 移回版本 ${rec.promotion.fromVersion}，` : ''}DEFAULT 用版本 ${rec.promotion.fromVersion} 的制品再发布一个新版本。继续？`) &&
    run('rollback', () => call('POST', rcPath(wid, `/${rec.id}/rollback`), { reason }), before ? '已回滚：生产没有动过' : '回滚已开始').then((r) => r && onDone())
  return h(Card, { title: before ? '回滚（发布前：停止分流，生产不动）' : '撤销发布（回到发布前的版本）', extra: h(Button, { kind: 'danger', onClick: go, busy: busy === 'rollback' }, before ? '回滚' : '撤销发布') },
    h(Field, { label: '原因（可选，会记录）' }, h(Input, { value: reason, onChange: setReason })), h(ErrorLine, { error }))
}

function ObservedSplit({ rec }) {
  const { wid } = useCtx()
  const [seen, setSeen] = useState(null)
  const { busy, error, run } = useAction()
  const load = () => run('seen', async () => setSeen(await call('GET', rcPath(wid, `/${rec.id}/observed`))))
  return h(Card, { title: '实际分流（aws/spans 里每组的会话数，晚几分钟）', extra: h(Button, { onClick: load, busy: busy === 'seen' }, seen ? '再数一次' : '数一数') },
    seen ? h(Table, { head: ['组', 'service.name', '会话', '占比', '权重'], rows: [['C', '生产（固定版本）'], ['T1', '候选']].map(([v, label]) => ({ key: v, cells: [label,
      h('span', { className: 'cs-mono' }, seen.arms[v].service), seen.arms[v].sessions, seen.share[v] === null ? '—' : `${Math.round(seen.share[v] * 100)}%`, `${seen.weights[v]}%`] })) })
      : h(Empty, null, '经网关的会话，按 Gateway 给它分的组计数（同一会话固定在一组）。'), h(ErrorLine, { error }))
}

function CanaryDetail({ cid, onChange }) {
  const { wid, me } = useCtx()
  const [rec, setRec] = useState(null)
  const { busy, error, run } = useAction()
  const load = () => call('GET', rcPath(wid, `/${cid}`)).then(setRec).catch((e) => run('load', () => Promise.reject(e)))
  useEffect(() => { setRec(null); load() }, [cid])
  useEffect(() => {
    if (!rec || !CANARY_LIVE.includes(rec.status)) return undefined
    const timer = setInterval(load, ['running', 'paused'].includes(rec.status) ? 15000 : 4000)
    return () => clearInterval(timer)
  }, [rec && rec.status])
  if (!rec) return h(Fragment, null, h(ErrorLine, { error }), h(Empty, null, '加载中…'))
  const reload = () => { load(); onChange() }
  const admin = me && me.role === 'admin'
  const cleanup = () => window.confirm('清理这个金丝雀在 AWS 上建的一切（A/B 测试、网关、在线评估、生产上的对照端点、候选 Runtime、角色、日志组；没发布的候选制品）？结果、发布和回滚记录保留。') &&
    run('clean', () => call('DELETE', rcPath(wid, `/${cid}`)), '清理已开始').then((r) => r && setTimeout(reload, 500))
  const copy = rec.copy || {}
  const changed = listOf(rec.candidate && rec.candidate.changed)
  const jobs = [[rec.job, '创建金丝雀（候选按部署模块的阶段部署）', rec.status === 'creating' || rec.status === 'failed'], [rec.promoteJob, '发布候选', rec.status === 'promoting' || (rec.promotion && rec.promotion.incomplete)],
    [rec.rollbackJob, '撤销发布', rec.status === 'rolling_back']]
  const active = ['running', 'paused'].includes(rec.status)
  return h(Fragment, null,
    h(Card, { title: rec.name, extra: h('span', { className: 'cs-row' }, h(StatusChip, { status: rec.status }), rec.live && rec.live.executionStatus ? h(Chip, null, rec.live.executionStatus) : null,
      admin && !rec.cleanedAt && !['creating', 'promoting', 'rolling_back', 'cleaning'].includes(rec.status) ? h(Button, { kind: 'danger', onClick: cleanup, busy: busy === 'clean' }, '清理') : null) },
    h(Table, { head: ['项', '对照（生产）', '候选'], rows: [
      ['运行在', `${rec.agent.name} 版本 ${rec.agent.version}（固定在它的端点 ${rec.controlEndpoint || '—'}；发布前 DEFAULT${rec.agent.endpoint ? ` 和 ${rec.agent.endpoint}` : ''} 不动）`,
        copy.name ? `${copy.name} 版本 ${copy.version || '—'}（端点 ${rec.treatmentEndpoint || '—'}；它的 DEFAULT 只留给冒烟和验证）` : '—'],
      ['改了', '—', changed.length ? changed.join('，') : '—'],
      ['执行角色', '生产的', '同一个（不写它）'],
      ['权重', `${rec.weights.C}%`, `${rec.weights.T1}%`],
    ].map((cells, i) => ({ key: i, cells })) }),
    h(ProductionState, { rec }),
    h(Note, null, `评估器：${listOf(rec.evaluators).join('，')}；门禁指标 ${rec.metric}。${rec.live && rec.live.expiresAt ? `A/B 最长运行到 ${String(rec.live.expiresAt).slice(0, 19)}。` : ''}`),
    copy.smoke ? h(Note, { tone: 'ok' }, h('b', null, `候选的冒烟调用（${copy.smoke.seconds} s）：`), excerpt(copy.smoke.answer, 160)) : null,
    rec.error ? h('div', { className: 'cs-err' }, rec.error) : null, h(ErrorLine, { error })),
    jobs.filter(([id, , show]) => id && show).map(([id, title]) => h(JobLine, { key: id, id, title })),
    listOf(rec.cleanup).length ? h(Card, { title: '清理结果' }, h(Table, { head: ['资源', '结果'], rows: rec.cleanup.map((c, i) => ({ key: i, cells: [c.resource, c.result] })) })) : null,
    rec.status === 'creating' || rec.status === 'failed' ? null : h(Fragment, null,
      active ? h(Ramp, { rec, reload, base: rcPath(wid, `/${rec.id}`) }) : null,
      h(Results, { rec }),
      active && copy.id ? h(ObservedSplit, { rec }) : null,
      h(CanaryGate, { rec, gate: rec.gate, onDone: reload }),
      h(Rollback, { rec, onDone: reload }),
      rec.verifying ? h(Note, { tone: 'info' }, '候选 Runtime 的验证正在进行（「评估 → 验证」里能看它的进度）；完成后门禁条件一会读到它。') : null,
      ['running', 'paused', 'stopped'].includes(rec.status) && !rec.verifying && !(rec.verification && rec.verification.robust) ? h(VerifyTreatment, { title: '验证候选 Runtime（门禁条件一）', body: {},
        path: rcPath(wid, `/${rec.id}/verifications`), note: '在候选 Runtime 的 DEFAULT 上跑契约集（不经网关，不算进 A/B），不改动生产。每轮每条契约一个新会话；每轮都通过才算稳定。评估器面板量出金丝雀评估器的噪声带，门禁用它。' }) : null,
      active ? h(Traffic, { rec, base: rcPath(wid, `/${rec.id}`), hint: '请求体是 Runtime 自己的载荷：{"prompt": "...", "actorId": "..."}；控制台的对话和 /v1 也经这里' }) : null))
}

function NewCanary({ onCreated }) {
  const { wid, me } = useCtx()
  const [runtimes] = useList(wid, `/workspaces/${wid}/deployments`, 'deployments')
  const [detail, setDetail] = useState(null)
  const [file, setFile] = useState(null)
  const [form, setForm] = useState({ runtimeId: '', source: 'zip', entryPoint: 'main.py', pythonRuntime: 'PYTHON_3_13', installRequirements: 'y', instrument: 'n', imageUri: '',
    env: '', treatmentWeight: '5', evaluators: 'Builtin.Correctness, Builtin.Helpfulness', metric: '', sessionTimeout: '5', acknowledged: false })
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const ready = listOf(runtimes).filter((d) => d.status === 'READY' && !/_c[0-9a-f]{8}$/.test(d.name || ''))
  useEffect(() => {
    if (!form.runtimeId) { setDetail(null); return }
    call('GET', `/workspaces/${wid}/deployments/${form.runtimeId}`).then((d) => {
      setDetail(d)
      const code = d.artifact && d.artifact.codeConfiguration
      setForm((f) => ({ ...f, source: code ? 'zip' : 'dockerfile', instrument: code && listOf(code.entryPoint)[0] === 'opentelemetry-instrument' ? 'y' : 'n',
        entryPoint: code ? listOf(code.entryPoint).slice(-1)[0] || 'main.py' : 'main.py', pythonRuntime: (code && code.runtime) || 'PYTHON_3_13' }))
    }).catch(() => setDetail(null))
  }, [form.runtimeId])
  const kind = detail && detail.artifact && detail.artifact.containerConfiguration ? 'container' : 'code'
  const evaluators = split(form.evaluators)
  const archive = form.source !== 'image'
  const create = () => run('create', async () => {
    const candidate = { source: form.source }
    const env = parseEnv(form.env)
    if (Object.keys(env).length) candidate.environment = env
    if (!archive) candidate.imageUri = form.imageUri.trim()
    else {
      if (!file) throw new Error('先选候选的 zip')
      if (file.size > MAX_ZIP) throw new Error('zip 最大 18 MB')
      candidate.archive = await readBase64(file)
      candidate.filename = file.name
      if (form.source === 'zip') Object.assign(candidate, { entryPoint: form.entryPoint.trim() || 'main.py', pythonRuntime: form.pythonRuntime,
        installRequirements: form.installRequirements === 'y', instrument: form.instrument === 'y' })
    }
    return call('POST', rcPath(wid), { runtimeId: form.runtimeId, candidate, treatmentWeight: Number(form.treatmentWeight), evaluators, metric: form.metric || evaluators[0],
      sessionTimeout: Number(form.sessionTimeout), acknowledged: form.acknowledged })
  }, '金丝雀创建中（候选要构建和部署，几分钟）').then((r) => r && onCreated(r.canary.id))
  if (!(me && me.role === 'admin')) return h(Card, { title: '新建代码 Agent 金丝雀' }, h(Note, null, '金丝雀会部署一个候选 Runtime：需要管理员。'))
  return h(Card, { title: '新建代码 Agent 金丝雀', extra: h(Button, { kind: 'pri', onClick: create, busy: busy === 'create',
    disabled: !form.runtimeId || !form.acknowledged || (archive ? !file : !form.imageUri.trim()) }, '创建') },
  h('div', { className: 'cs-grid' },
    h(Field, { label: '生产 Agent（控制台部署的 Runtime）' }, h(Select, { value: form.runtimeId, onChange: set('runtimeId'), options: [['', '选择'], ...ready.map((d) => [d.runtimeId, `${d.name}（版本 ${d.version}${d.endpoint ? `，端点 ${d.endpoint}` : ''}）`])] })),
    h(Field, { label: '候选来源' }, h(Select, { value: form.source, onChange: set('source'), options: CANDIDATE_SOURCES[kind] })),
    archive ? h(Field, { label: form.source === 'zip' ? '候选代码 zip' : '候选构建目录 zip', hint: file ? `${file.name} · ${(file.size / 1048576).toFixed(1)} MB` : '最大 18 MB' },
      h('input', { type: 'file', accept: '.zip,application/zip', className: 'cs-input', onChange: (e) => setFile((e.target.files && e.target.files[0]) || null) }))
      : h(Field, { label: '候选镜像 URI（生产镜像所在的仓库）' }, h(Input, { value: form.imageUri, onChange: set('imageUri'), mono: true })),
    form.source === 'zip' ? h(Field, { label: '入口文件' }, h(Input, { value: form.entryPoint, onChange: set('entryPoint'), mono: true })) : null,
    form.source === 'zip' ? h(Field, { label: 'requirements.txt' }, h(Select, { value: form.installRequirements, onChange: set('installRequirements'), options: [['y', '有就用 CodeBuild 安装（ARM64）'], ['n', '不安装']] })) : null,
    form.source === 'zip' ? h(Field, { label: '启动方式' }, h(Select, { value: form.instrument, onChange: set('instrument'), options: [['n', '直接运行'], ['y', 'opentelemetry-instrument 启动（在线评估要它的链路）']] })) : null,
    h(Field, { label: '候选起始流量' }, h(Select, { value: form.treatmentWeight, onChange: set('treatmentWeight'), options: [['5', '5%（金丝雀）'], ['25', '25%'], ['50', '50%（经典 A/B）']] })),
    h(Field, { label: '评估器（逗号分隔，最多 5 个）' }, h(Input, { value: form.evaluators, onChange: set('evaluators'), mono: true })),
    h(Field, { label: '决定发布的指标' }, h(Select, { value: form.metric || evaluators[0] || '', onChange: set('metric'), options: evaluators.filter((e) => e !== 'Builtin.Refusal').map((e) => [e, e]) })),
    h(Field, { label: '会话空闲多久算结束（分钟）' }, h(Input, { value: form.sessionTimeout, onChange: set('sessionTimeout') }))),
  h(Field, { label: '候选的环境变量（一行一个 KEY=VALUE，覆盖生产的同名变量，其余照搬生产的）' }, h(TextArea, { value: form.env, onChange: set('env'), rows: 2, mono: true, placeholder: 'FEATURE_FLAG=on' })),
  detail ? h(Note, null, `生产：${detail.name} 版本 ${detail.version}，${kind === 'code' ? '代码部署' : '容器镜像'}，环境变量 ${listOf(detail.environment).join('，') || '无'}。`) : null,
  h(Note, null, '会在 AWS 上建：候选 Runtime（生产的角色、网络、生命周期和变量，候选的代码；制品放在生产的源码前缀 / 镜像仓库里）、生产上一个固定在当前版本的对照端点、候选上的候选端点、一个 IAM 角色、一个 AgentCore Gateway 和两个 Runtime 目标、每组一个在线评估（100% 抽样）、一个按目标分流的 A/B 测试。生产的 DEFAULT 和它自己的端点在发布前不变。'),
  h('label', { className: 'cs-row' }, h('input', { type: 'checkbox', checked: form.acknowledged, onChange: (e) => set('acknowledged')(e.target.checked) }),
    '我知道这些资源会一直存在（在线评估按评估的会话计费），直到我清理'),
  h(ErrorLine, { error }))
}

function RuntimeCanaries() {
  const { wid } = useCtx()
  const [list, load, listError] = useList(wid, rcPath(wid), 'canaries')
  const [open, setOpen] = useState(null)
  return h(Fragment, null,
    h(Card, { title: '代码 Agent 金丝雀', extra: h(Button, { onClick: load }, '刷新') }, h(ErrorLine, { error: listError }), list === null ? h(Empty, null, '加载中…') : h(Table, {
      head: ['名称', '生产 Agent', '状态', '生产 / 候选', '门禁指标', '创建'], rows: list.map((c) => ({ key: c.id, onClick: () => setOpen(c.id), cells: [h('b', null, c.name),
        `${c.agent.name} v${c.agent.version}`, h(Fragment, null, h(StatusChip, { status: c.status }), c.cleanedAt ? h(Chip, null, ' 已清理') : null), `${c.weights.C}% / ${c.weights.T1}%`, c.metric,
        `${(c.createdAt || '').slice(0, 16)} ${c.createdBy || ''}`] })),
      empty: '还没有金丝雀。先在「部署代码」部署一个 HTTP Agent，再在下面拿候选版本和它比。' })),
    open ? h(CanaryDetail, { cid: open, onChange: load }) : null,
    h(NewCanary, { onCreated: (id) => { load(); setOpen(id) } }))
}

function ExperimentsPage() {
  const { workspace } = useCtx()
  const [tab, setTab] = useState('ab')
  if (!workspace) return h(Empty, null, '先添加工作区。')
  const Body = { ab: Experiments, bundles: Bundles, canary: RuntimeCanaries, releases: Releases }[tab]
  return h(Fragment, null, h('h1', null, 'A/B 与发布'),
    h(Note, null, 'A/B 由 AgentCore Gateway 分流：对照是 Agent 本身（固定在当前版本的端点），候选是只换了 Prompt / 模型的副本 Harness；只有经过实验网关的会话参与。发布要证据：候选有稳定的验证，且在门禁指标上不比对照差到噪声带之外。代码 Agent 的金丝雀同理：候选版本先作为旁边的另一个 Runtime 跑，发布才成为生产的新版本。'),
    h(Tabs, { value: tab, onChange: setTab, options: [['ab', 'A/B 实验'], ['bundles', '配置包'], ['canary', '代码 Agent 金丝雀'], ['releases', '发布记录']] }),
    h(Body))
}

export default { id: 'experiments', label: 'A/B 与发布', group: '评估', Page: ExperimentsPage }
