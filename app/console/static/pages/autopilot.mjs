// 自动驾驶: a customer brief in, a class-ready release out — the platform's autonomous loop (Kiro writes the pack, the
// gates and Kiro's repairs, the build, the local prediction, direct rehearsals on AgentCore until every round holds) as
// one console job. The 工作坊 calls a direct rehearsal 快速彩排: so does this page, with the 工作坊's words for its verdicts.
import React from 'react'
import { call, useCtx, listOf, Card, Button, Field, Input, TextArea, Select, Chip, Note, Empty, ErrorLine, Table, useAction, useJob, JOB_TONE,
  JOB_LABEL, NeedWorkspace, loaded, loadFailed, consoleText, jobErrorText, JobError, At, firstChoice, logLines, secs, reasonText, blockingText } from '../ui.mjs'

const { useState, useEffect, createElement: h, Fragment } = React
const DIRECT_VERDICT = { ready: ['ok', '快速彩排通过'], not_ready: ['bad', '快速彩排没通过'], insufficient_evidence: ['warn', '证据不足'] }
const PREDICTION = { likely_ready: ['ok', '预计能上课'], likely_not_ready: ['warn', '预计还不能上课'], unknown: ['', '预测不了：要上 AWS 彩排才知道'] }
const PHENOMENON = { reproduced: ['ok', '复现了'], not_reproduced: ['bad', '没复现'], insufficient_evidence: ['warn', '证据不足'] }
const ROUND_MODE = { draft: '起草', repair: '修复', regenerate: '改写' }
const ROUND_STATUS = { ready: '待应用', applied: '已应用', failed: '失败', 'needs-input': '需要补充信息', 'invalid-output': '输出不合格', superseded: '已被新草稿取代' }
// The pack types autopilot can make, in the 工作坊's names (客户 Workshop is made on the 工作坊 page itself).
const PACK_KINDS = [['customer', '客户严格确认（真实客户）'], ['reference', '内部参考包（虚构场景）']]
const PACK_HINT = {
  customer: '所有阻断项都要客户确认（确认人 + 依据）才能构建：自动驾驶写完内容、修完校验就停在构建前（不上 AWS），到工作坊页确认后再构建和彩排。要用「客户 Workshop」类型，在工作坊页新建项目。',
  reference: '虚构或内部内容：自动驾驶按 SA 批量确认为合成数据，一路跑到「构建之后」选的那一步；只用于培训和回归，不交付客户。',
}
const pick = (map, key) => map[key] || ['', key || '—']
// What a finished run came to (the server's outcome, autopilot.outcome): a run that stopped is no "done".
const OUTCOME = {
  ready: ['ok', '快速彩排通过'], built: ['ok', '已构建（没做快速彩排）'], 'awaits-customer': ['info', '停在构建前：等客户确认'],
  'needs-input': ['warn', '要补充信息'], 'not-converged': ['warn', '没收敛'], stopped: ['warn', '中途停了'], 'pre-findings': ['warn', '本地预测还有问题'],
  'not-ready': ['bad', '快速彩排没通过'],
}
const GOOD = ['ready', 'built', 'awaits-customer']  // the run got where it was meant to: no "stopped at", no second run
// A job's outcome; one from before outcomes were recorded has it in the records list's row only.
const outcomeOf = (job, row) => (job && job.status === 'succeeded' ? (job.progress || {}).outcome || ((row || {}).progress || {}).outcome || null : null)
// Where a run is, from autopilot.phase ("生成与修复 02-repair", "本地预测 01", "快速彩排 4 进行中"): which round or which time.
function phaseText(phase) {
  const round = /^生成与修复 (\d+)-(\w+)$/.exec(phase)
  if (round) return `第 ${Number(round[1])} 轮（${ROUND_MODE[round[2]] || round[2]}）`
  const step = /^(本地预测|快速彩排) (\d+)(?: 进行中)?$/.exec(phase)
  return step ? `第 ${Number(step[2])} 次${step[1]}` : phase
}
function RunStatus({ job, row }) {
  const [tone, label] = OUTCOME[outcomeOf(job, row)] || [JOB_TONE[job.status], JOB_LABEL[job.status] || job.status]
  return h(Chip, { tone }, label)
}

function Result({ res, outcome }) {
  if (!res) return null
  const last = listOf(res.directRehearsals).slice(-1)[0]
  // a customer pack stops before the build by design: the customer confirms its blocking facts first
  const designed = outcome === 'awaits-customer'
  return h(Fragment, null,
    res.readyForClassDirect ? h(Note, { tone: 'ok' }, h('b', null, '快速彩排每一轮都通过：可以去工作坊页跑 Guided Run（上课前的最后一次检查）。'))
      : designed ? h(Note, { tone: 'ok' }, h('b', { title: res.error }, '按设计停在构建前：内容写好了，Kiro 能修的校验问题都修了；剩下的阻断项要客户确认（确认人 + 依据）。到工作坊页打开这个项目确认，再在那里构建和彩排。'))
      : outcome === 'built' ? h(Note, { tone: 'ok' }, h('b', null, '已构建发布包（没做快速彩排）：要上 AWS 彩排，到工作坊页打开这个项目。'))
      : h(Note, { tone: 'warn' }, h('b', { title: last && last.reasonCode ? `原因代码：${last.reasonCode}` : res.error || undefined }, (res.error && (consoleText(res.error) || res.error))
        || (last ? `快速彩排还没有每轮都通过（最后一次：${pick(DIRECT_VERDICT, last.verdict)[1]}${last.robust === false ? '，有的轮次没复现' : ''}）。` : '没有跑到快速彩排。'))),
    h('div', { className: 'cs-mut' }, `用时 ${secs(res.seconds)} · 校验 ${res.validationOk ? '通过' : designed ? '还差客户确认' : '未通过'} · 构建 ${(res.build || {}).version ? `第 ${res.build.version} 版` : '没有构建'}`),
    h(Table, { head: ['轮', '方式', '结果', 'Kiro 用时', '改动文件'], rows: listOf(res.rounds).map((r) => ({ key: r.n, cells: [r.n, ROUND_MODE[r.mode] || r.mode, ROUND_STATUS[r.status] || r.status,
      secs(r.kiroSeconds), r.filesChanged ?? '—'] })), empty: '没有生成轮次。' }),
    listOf(res.preRehearsals).length ? h(Card, { title: '本地预测（不部署：用 Bedrock 模型把每道练习题跑一遍，几分钟）' }, res.preRehearsals.map((p, i) => h('div', { key: i, className: 'cs-note' },
      h(Chip, { tone: pick(PREDICTION, p.prediction)[0] }, pick(PREDICTION, p.prediction)[1]), ` ${secs(p.seconds)} `, listOf(p.findings).map((f, j) => h('div', { key: j, className: 'cs-mut' }, f.slice(0, 300)))))) : null,
    listOf(res.directRehearsals).length ? h(Card, { title: '快速彩排（直连 AgentCore：部署到这个包自己的直连资源，用 Workshop 的同一套裁判判定）' }, h(Table, {
      head: ['次', '判定', '教学现象（绿色 = 复现了）', '稳定性', '要改的（整改项）', '用时'], rows: res.directRehearsals.map((d, i) => ({ key: i, cells: [i + 1,
        h(Chip, { tone: pick(DIRECT_VERDICT, d.verdict)[0], title: reasonText(d.reasonCode) ? `原因：${reasonText(d.reasonCode)}` : undefined }, pick(DIRECT_VERDICT, d.verdict)[1]),
        h('span', { className: 'cs-row' }, Object.entries(d.phenomena || {}).map(([k, v]) => h(Chip, { key: k, tone: pick(PHENOMENON, v)[0], title: pick(PHENOMENON, v)[1] }, k))),
        d.robust === undefined ? '—' : d.robust ? '每轮都通过' : `不稳定：${listOf(d.flaky).join('，')} 有的轮次没复现`, blockingText(listOf(d.blocking).join(', ')) || '—', secs(d.seconds)] })) })) : null)
}

function AutopilotPage() {
  const { wid, workspace } = useCtx()
  const [samples, setSamples] = useState(null)
  const [samplesError, setSamplesError] = useState(null)
  const [form, setForm] = useState({ projectId: '', displayName: '', customer: '', packKind: 'customer', brief: '', preRehearse: true, direct: true, directRepeat: '3' })
  const [jobs, setJobs] = useState(null)
  const [jobsError, setJobsError] = useState(null)
  const [open, setOpen] = useState(null)
  const job = useJob(open)
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const [projects, setProjects] = useState([])  // every project id in the workshop data: a run with one of them is refused
  const loadJobs = () => call('GET', `/workspaces/${wid}/autopilot`).then((r) => { setJobs(listOf(r.jobs)); setProjects(listOf(r.projects)); setJobsError(null) }).catch(setJobsError)
  useEffect(() => { call('GET', '/autopilot/briefs').then((r) => { setSamples(listOf(r.briefs)); setSamplesError(null) }).catch(setSamplesError) }, [])
  useEffect(() => { if (wid) loadJobs() }, [wid])
  useEffect(() => { if (job && job.status !== 'running') loadJobs() }, [job && job.status])
  if (!workspace) return h(NeedWorkspace, { title: '自动驾驶' })
  const sample = (name) => name && run('sample', async () => {
    const r = await call('GET', `/autopilot/briefs/${name}`)
    setForm((f) => ({ ...f, brief: r.brief, projectId: f.projectId || `${name}-auto`, displayName: f.displayName || name, packKind: 'reference' }))
  }, `已载入样例「${name}」：样例是虚构场景，项目类型换成了「内部参考包」`)
  // a strict customer pack stops before the build (its blocking facts wait for the customer): no prediction, no AWS
  const strict = form.packKind === 'customer'
  const offline = strict || (!form.direct && !form.preRehearse)  // no AWS at all: the workspace's credentials are not needed
  const start = () => run('start', () => call('POST', `/workspaces/${wid}/autopilot`, { ...form, ...(strict ? { direct: false, preRehearse: false } : {}),
    directRepeat: Number(form.directRepeat) }), strict ? '已开始：写到构建前就停' : offline ? '已开始：写完、构建就停（不用 AWS）' : form.direct ? '已开始：大约一小时' : '已开始：写完、构建、本地预测')
    .then((r) => { if (r) { setOpen(r.job.id); loadJobs() } })
  const short = 200 - form.brief.trim().length
  const used = projects.includes(form.projectId.trim())
  const missing = !form.projectId.trim() ? '先填项目 ID' : used ? `工作坊里已经有项目 ${form.projectId.trim()}：换一个项目 ID` : short > 0 ? `客户简报还差 ${short} 字` : null
  const row = listOf(jobs).find((j) => j.id === open) || null
  const outcome = outcomeOf(job, row)
  return h(Fragment, null, h('h1', null, '自动驾驶'),
    h(Card, { title: strict ? '一个简报进去：Kiro 写出整个场景包，停在构建前等客户确认' : offline ? '一个简报进去：Kiro 写出整个场景包，构建好就停（不上 AWS）'
      : !form.direct ? '一个简报进去：Kiro 写出整个场景包，构建、本地预测后就停' : '一个简报进去，一个可以上课的场景包出来', extra: h('span', { className: 'cs-row' }, missing ? h('span', { className: 'cs-mut' }, missing) : null,
      h(Button, { kind: 'pri', onClick: start, busy: busy === 'start', disabled: Boolean(missing) }, strict ? '开始（写到构建前）' : '开始')) },
      h(Note, null, strict
        ? '本机的 kiro-cli 写出整个场景包（知识文档、模拟工具、提示词、技能、golden 集（带标准答案的练习题）、教学设计），校验不过就让 Kiro 修，然后停在构建前：'
          + '阻断项要客户在工作坊页确认（确认人 + 依据），确认后在那里构建、彩排。这一步不上 AWS。'
        : offline ? '本机的 kiro-cli 写出整个场景包（知识文档、模拟工具、提示词、技能、golden 集（带标准答案的练习题）、教学设计），校验不过就让 Kiro 修；构建发布包就停（不上 AWS）。'
          + '要彩排，到工作坊页打开这个项目做快速彩排。'
        : !form.direct ? '本机的 kiro-cli 写出整个场景包（知识文档、模拟工具、提示词、技能、golden 集（带标准答案的练习题）、教学设计），校验不过就让 Kiro 修；构建发布包；'
          + '本地预测这堂课就停（不部署到 AgentCore）。要彩排，到工作坊页打开这个项目做快速彩排。'
        : '本机的 kiro-cli 写出整个场景包（知识文档、模拟工具、提示词、技能、golden 集（带标准答案的练习题）、教学设计），校验不过就让 Kiro 修；构建发布包；本地预测这堂课；'
          + '然后做快速彩排（直连 AgentCore 部署，按 Workshop 自己的裁判判定），按彩排的整改意见让 Kiro 修，直到每一轮都通过。内容里没有人工步骤；上课前在工作坊页跑一次 Guided Run 做最后确认。'),
      h('div', { className: 'cs-grid' },
        h(Field, { label: '项目 ID', hint: '小写字母、数字和连字符，字母开头；工作坊页用这个 ID 列出它' }, h(Input, { value: form.projectId, onChange: set('projectId'), mono: true, placeholder: '例：acme-claims' })),
        h(Field, { label: '显示名称' }, h(Input, { value: form.displayName, onChange: set('displayName') })),
        h(Field, { label: '客户' }, h(Input, { value: form.customer, onChange: set('customer') })),
        h(Field, { label: '项目类型（工作坊页三种类型里的两种）', hint: PACK_HINT[form.packKind] }, h(Select, { value: form.packKind, onChange: set('packKind'), options: PACK_KINDS })),
        h(Field, { label: '样例简报（虚构场景）' }, h(Select, { value: '', onChange: sample, options: [['', firstChoice(samples, samplesError, '没有样例简报', '载入一个样例…')], ...listOf(samples).map((s) => [s, s])] })),
        h(Field, { label: '构建之后：本地预测和快速彩排', hint: strict ? '「客户严格确认」的项目停在构建前，不做本地预测和快速彩排；换成「内部参考包」才会一路跑到彩排。' : null },
          strict ? h(Select, { value: 'strict', disabled: true, options: [['strict', '不做（停在构建前）']] })
            : h(Select, { value: form.direct ? form.directRepeat : form.preRehearse ? 'predict' : 'none',
              onChange: (v) => setForm((f) => ({ ...f, direct: v === '3' || v === '1', preRehearse: v !== 'none', directRepeat: v === '3' || v === '1' ? v : f.directRepeat })),
              options: [['3', '本地预测 + 快速彩排，每次 3 轮、每轮都通过才算（推荐）'], ['1', '本地预测 + 快速彩排，每次 1 轮'],
                ['predict', '只做本地预测，不部署到 AgentCore（本地预测用这个工作区的 AWS 凭证调 Bedrock 模型）'], ['none', '都不做：只生成和构建（完全不用 AWS）']] }))),
      h(Field, { label: '客户简报（谁在用、问什么、背后的系统和规则；200 字以上）' }, h(TextArea, { value: form.brief, onChange: set('brief'), rows: 12 })),
      h(ErrorLine, { error })),
    h(Card, { title: '自动驾驶记录', extra: h(Button, { onClick: loadJobs }, '刷新') }, loaded(jobs, jobsError, (list) => h(Table, { head: ['项目', '状态', '开始', ''], rows: list.map((j) => ({ key: j.id, onClick: () => setOpen(j.id),
      cells: [h('b', null, j.params.projectId), h(RunStatus, { job: j }), h(At, { iso: j.createdAt }), j.id === open ? '在看' : '查看'] })),
      empty: '还没有跑过自动驾驶。' }))),
    job ? h(Card, { title: job.status === 'unreadable' && row ? row.label : job.label, extra: h('span', { className: 'cs-row' }, h(RunStatus, { job, row }),
      job.progress && job.progress.phase && !GOOD.includes(outcome) ? h('span', { className: 'cs-mut' }, `${job.status === 'running' ? '当前' : '停在'}：${phaseText(job.progress.phase)}`) : null) },
      job.result ? h(Result, { res: job.result, outcome }) : null,
      h('pre', { className: 'cs-pre', style: { maxHeight: 360, overflow: 'auto' } }, logLines(job.log, 40) || (job.status === 'unreadable' ? '（读不到这个任务的日志）' : '…')),
      // a run that ended without its report (failed, cut off): its project may be half written; one line says what to do
      ['failed', 'interrupted', 'unreadable'].includes(job.status) && row && row.projectExists && !(job.status !== 'interrupted' && /工作坊页/.test(jobErrorText(job.error)))
        ? h(Fragment, null, job.status === 'interrupted' ? null : h(JobError, { error: job.error }),
          h(Note, { tone: job.status === 'interrupted' ? 'warn' : undefined }, `${job.status === 'interrupted' ? '控制台重启时这次自动驾驶还在跑，被中断了。' : ''}`
            + `项目 ${row.params.projectId} 已经写进工作坊：到工作坊页打开它接着做，或换一个新的项目 ID 重新开始。`))
        : h(JobError, { error: job.error })) : null)
}

export default { id: 'autopilot', label: '自动驾驶', group: '工作坊', Page: AutopilotPage }
