// 自动驾驶: a customer brief in, a class-ready release out — the platform's autonomous loop (Kiro writes the pack, the
// gates and Kiro's repairs, the build, the local prediction, direct rehearsals on AgentCore until every round holds) as
// one console job.
import React from 'react'
import { call, useCtx, listOf, Card, Button, Field, Input, TextArea, Select, Chip, Note, Empty, ErrorLine, Table, useAction, useJob, JOB_TONE,
  JOB_LABEL } from '../ui.mjs'

const { useState, useEffect, createElement: h, Fragment } = React
const VERDICT_TONE = { ready: 'ok', not_ready: 'bad', likely_ready: 'ok', likely_not_ready: 'warn' }
const PHENOMENON = { reproduced: 'ok', not_reproduced: 'bad' }

function Result({ res }) {
  if (!res) return null
  const last = listOf(res.directRehearsals).slice(-1)[0]
  return h(Fragment, null,
    res.readyForClassDirect ? h(Note, { tone: 'ok' }, h('b', null, '直连彩排每一轮都成立：可以去工作坊页跑 Guided Run（上课前的最后一次检查）。'))
      : h(Note, { tone: 'warn' }, h('b', null, res.error || (last ? `直连彩排还没有每轮都成立（${last.verdict}${last.reasonCode ? ` · ${last.reasonCode}` : ''}）。` : '没有跑到直连彩排。'))),
    h('div', { className: 'cs-mut' }, `用时 ${res.seconds ? Math.round(res.seconds / 60) : '—'} 分钟 · 校验 ${res.validationOk ? '通过' : '未通过'} · 构建 ${(res.build || {}).version || '—'}`),
    h(Table, { head: ['轮', '方式', '结果', 'Kiro 用时', '改动文件'], rows: listOf(res.rounds).map((r) => ({ key: r.n, cells: [r.n, r.mode, r.status,
      r.kiroSeconds ? `${Math.round(r.kiroSeconds)}s` : '—', r.filesChanged ?? '—'] })), empty: '没有生成轮次。' }),
    listOf(res.preRehearsals).length ? h(Card, { title: '本地预测（上 AWS 之前）' }, res.preRehearsals.map((p, i) => h('div', { key: i, className: 'cs-note' },
      h(Chip, { tone: VERDICT_TONE[p.prediction] }, p.prediction), ` ${p.seconds ? Math.round(p.seconds) : '—'}s `, listOf(p.findings).map((f, j) => h('div', { key: j, className: 'cs-mut' }, f.slice(0, 300)))))) : null,
    listOf(res.directRehearsals).length ? h(Card, { title: '直连彩排（AgentCore 上直接部署、按 Workshop 的评估器判定）' }, h(Table, {
      head: ['次', '判定', '现象', '稳定性', '用时'], rows: res.directRehearsals.map((d, i) => ({ key: i, cells: [i + 1, h(Chip, { tone: VERDICT_TONE[d.verdict] }, d.verdict),
        h('span', { className: 'cs-row' }, Object.entries(d.phenomena || {}).map(([k, v]) => h(Chip, { key: k, tone: PHENOMENON[v] }, k))),
        d.robust === undefined ? '—' : d.robust ? '每轮都成立' : `不稳定：${listOf(d.flaky).join('，')}`, d.seconds ? `${Math.round(d.seconds / 60)} 分钟` : '—'] })) })) : null)
}

function AutopilotPage() {
  const { wid, workspace } = useCtx()
  const [samples, setSamples] = useState([])
  const [form, setForm] = useState({ projectId: '', displayName: '', customer: '', packKind: 'customer', brief: '', preRehearse: true, direct: true, directRepeat: '3' })
  const [jobs, setJobs] = useState([])
  const [open, setOpen] = useState(null)
  const job = useJob(open)
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const loadJobs = () => call('GET', `/workspaces/${wid}/autopilot`).then((r) => setJobs(listOf(r.jobs))).catch(() => {})
  useEffect(() => { call('GET', '/autopilot/briefs').then((r) => setSamples(listOf(r.briefs))).catch(() => {}) }, [])
  useEffect(() => { if (wid) loadJobs() }, [wid])
  useEffect(() => { if (job && job.status !== 'running') loadJobs() }, [job && job.status])
  if (!workspace) return h(Empty, null, '先添加工作区。')
  const sample = (name) => name && run('sample', async () => {
    const r = await call('GET', `/autopilot/briefs/${name}`)
    setForm((f) => ({ ...f, brief: r.brief, projectId: f.projectId || `${name}-auto`, displayName: f.displayName || name, packKind: 'reference' }))
  })
  const start = () => run('start', () => call('POST', `/workspaces/${wid}/autopilot`, { ...form, directRepeat: Number(form.directRepeat) }), '已开始：大约一小时')
    .then((r) => { if (r) { setOpen(r.job.id); loadJobs() } })
  return h(Fragment, null, h('h1', null, '自动驾驶：从客户简报到可以上课'),
    h(Card, { title: '一个简报进去，一个可以上课的场景包出来', extra: h(Button, { kind: 'pri', onClick: start, busy: busy === 'start', disabled: !form.projectId || form.brief.length < 200 }, '开始') },
      h(Note, null, '本机的 kiro-cli 写出整个场景包（知识文档、模拟工具、提示词、技能、golden 集、教学设计），校验不过就让 Kiro 修；构建发布包；本地预测这堂课；'
        + '然后直连 AgentCore 部署并彩排（每轮按 Workshop 自己的评估器判定），按彩排的整改意见让 Kiro 修，直到每一轮都成立。内容里没有人工步骤；上课前在工作坊页跑一次 Guided Run 做最后确认。'),
      h('div', { className: 'cs-grid' },
        h(Field, { label: '项目 ID' }, h(Input, { value: form.projectId, onChange: set('projectId'), mono: true, placeholder: 'acme-claims' })),
        h(Field, { label: '显示名称' }, h(Input, { value: form.displayName, onChange: set('displayName') })),
        h(Field, { label: '客户' }, h(Input, { value: form.customer, onChange: set('customer') })),
        h(Field, { label: '包的类型', hint: '真实客户包的事实要 SA 在工作坊页确认来源后才能构建' }, h(Select, { value: form.packKind, onChange: set('packKind'),
          options: [['customer', '客户包（真实客户）'], ['reference', '参考包（虚构场景，自动按 SA 批量确认）']] })),
        h(Field, { label: '样例简报' }, h(Select, { value: '', onChange: sample, options: [['', '载入一个样例…'], ...samples.map((s) => [s, s])] })),
        h(Field, { label: '直连彩排' }, h(Select, { value: form.direct ? form.directRepeat : '0', onChange: (v) => setForm((f) => ({ ...f, direct: v !== '0', directRepeat: v === '0' ? f.directRepeat : v })),
          options: [['3', '每次 3 轮，每轮都成立才算（推荐）'], ['1', '每次 1 轮'], ['0', '不上 AWS：只生成、构建、本地预测']] }))),
      h(Field, { label: '客户简报（谁在用、问什么、背后的系统和规则；200 字以上）' }, h(TextArea, { value: form.brief, onChange: set('brief'), rows: 12 })),
      h(ErrorLine, { error })),
    h(Card, { title: '自动驾驶记录', extra: h(Button, { onClick: loadJobs }, '刷新') }, h(Table, { head: ['项目', '状态', '开始', ''], rows: jobs.map((j) => ({ key: j.id, onClick: () => setOpen(j.id),
      cells: [h('b', null, j.params.projectId), h(Chip, { tone: JOB_TONE[j.status] }, JOB_LABEL[j.status] || j.status), (j.createdAt || '').slice(0, 19), j.id === open ? '在看' : '查看'] })),
      empty: '还没有。' })),
    job ? h(Card, { title: job.label, extra: h('span', { className: 'cs-row' }, h(Chip, { tone: JOB_TONE[job.status] }, JOB_LABEL[job.status] || job.status),
      job.progress && job.progress.phase ? h('span', { className: 'cs-mut' }, `当前：${job.progress.phase}`) : null) },
      job.result ? h(Result, { res: job.result }) : null,
      h('pre', { className: 'cs-pre', style: { maxHeight: 360, overflow: 'auto' } }, listOf(job.log).slice(-40).join('\n') || '…'),
      job.error ? h('div', { className: 'cs-err' }, job.error) : null) : null)
}

export default { id: 'autopilot', label: '自动驾驶', group: '工作坊', Page: AutopilotPage }
