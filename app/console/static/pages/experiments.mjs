// A/B 与发布: configuration bundles (an agent's control and treatment configurations), A/B tests that AgentCore splits at
// a Gateway (a treatment Harness beside the agent, one online evaluation per arm), the canary ramp, live results, and a
// promotion that evidence opens: a robust verification of the treatment and an A/B result not worse than the control
// beyond the evaluator's noise band (an admin may override, with a reason that is recorded). 代码 Agent 金丝雀: the same for a
// code agent the console deployed (console.runtime_canary): the candidate version as a runtime of its own beside
// production, two runtime targets at a Gateway, promotion as production's next version, rollback before or after it.
import React from 'react'
import { call, useCtx, listOf, Card, Button, Field, Input, TextArea, Select, Chip, Note, Empty, ErrorLine, Table, useAction, useJob, Tabs, JOB_TONE,
  JOB_LABEL, NeedWorkspace, loadFailed, loaded, firstChoice, AwsStatus, At, when, JobError, statusText, logLines, quiet, secs, personText, consoleText, resourceText, jobErrorText, cancelled, stageText, BUILD_PHASE, PHASE_NAME, unscannedText } from '../ui.mjs'
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
// what a cleanup step did (experiments.cleanup_step, the canary's own notes)
const CLEANED = { deleted: '已删除', 'already gone': '本来就不在了' }
const CLEANED_RULES = [
  [/^(\d+) object version\(s\) deleted$/, (m) => `已删除（${m[1]} 个对象版本）`],
  [/^kept: production's version (\S+) runs it/, (m) => `保留：生产的版本 ${m[1]} 在用它，随生产一起留着`],
  [/^left: not this canary's endpoint$/, () => '没动：不是这个金丝雀建的端点'],
  [/^left: (\S+) is not this canary's candidate$/, (m) => `没动：${m[1]} 不是这个金丝雀的候选`],
  [/^left: (\S+) is not this experiment's treatment Harness/, (m) => `没动：${m[1]} 不是这个实验的候选副本`],
  [/^left: role (\S+) is not (?:the console's|this canary's)/, (m) => `没动：角色 ${m[1]} 不是这次建的`],
  [/^left: not the image this canary's build pushed \((\S+)\)/, (m) => `没动：不是这个金丝雀构建推送的镜像（${m[1]}）`],
  [/^left: an image the canary did not build \(given as the candidate\)/, () => '保留：不是金丝雀构建的镜像（候选是现成的镜像）'],
  [/^left: a version of production runs it \(or a version could not be read\)/, () => '保留：生产的某个版本在用它（或读不到版本，稳妥起见留着）'],
  [/^left: the role (\S+) exists and is not the console's/, (m) => `没动：角色 ${m[1]} 不是控制台建的（没有 adlc:console=1 标签）`],
  [/^left: the role (\S+) is tagged adlc:console=1 but is on the path \/,/, (m) => `没动：角色 ${m[1]} 是早期控制台建的（在路径 / 上）；有权限边界的工作区不接管它，账号管理员可以删掉它`],
  [/^left: the role (\S+) is on the path (\S+), not the console's/, (m) => `没动：角色 ${m[1]} 在路径 ${m[2].replace(/,$/, '')} 上，不是控制台的路径`],
  [/^left: \w+: (.*)$/s, (m) => `留下了：${consoleText(m[1]) || m[1]}`],
]
function cleanedText(r) {
  const said = String(r || '')
  if (CLEANED[said]) return CLEANED[said]
  for (const [re, to] of CLEANED_RULES) { const m = said.match(re); if (m) return to(m) }
  return said.replace(/^left: /, '留下了：').replace(/^left$/, '留下了')
}
function CleanupResults({ rec }) {
  return listOf(rec.cleanup).length ? h(Card, { title: '清理结果' }, h(Table, { head: ['资源', '结果'], rows: rec.cleanup.map((c, i) => ({ key: i,
    cells: [h('span', { title: c.resource }, resourceText(c.resource)), h('span', { title: c.result, style: c.failed ? { color: '#ab091e' } : undefined }, cleanedText(c.result))] })) })) : null
}
// what a promotion changed (experiments._apply's fields, runtime_canary.candidate_request's, a rollback's), and a gate
// condition by its short name
const CHANGED = { model: '模型', systemPrompt: '系统 Prompt', code: '代码', image: '镜像', 'image (Dockerfile)': '镜像（Dockerfile 构建）' }
const changedText = (c) => CHANGED[c] || String(c).replace(/^env:(.*)$/, '环境变量 $1').replace(/^rollback to version (\S+)$/, '回滚到版本 $1 的制品和变量')
const CONDITION = { verification: '验证', ab: 'A/B 结果', agent: '版本一致', metric: '指标' }
// an error from before a finished cleanup: what happened then, not something to act on now
const actionable = (rec) => rec.status === 'promoted' && /^rollback: /.test(rec.error || '')  // 撤销发布 is still there
const history = (error) => h('div', { className: 'cs-note', title: error }, `清理之前出过的错：${jobErrorText(error)}`)
// a record whose cleanup started: its A/B test, control endpoint and treatment copy are gone or going
const cleanupStarted = (rec) => Boolean(rec.cleanedAt) || ['cleaning', 'cleaned', 'cleanup_incomplete'].includes(rec.status)
// why a gate no longer leads to a promotion (null while one can still start, or is under way)
function closedText(rec, what) {
  const how = rec.status === 'cleaning' ? '正在清理' : rec.status === 'cleanup_incomplete' ? '清理没做完' : cleanupStarted(rec) ? '已清理'
    : rec.status === 'rolled_back' ? '已回滚' : null
  return how && `这个${what}${how}，不能再发布，门禁只作记录。要发布这个候选，另起一个${what}。`
}
// what else the A/B test compared (experiments._arms, runtime_canary._arms), as the gate names it
const ARM_NAME = [[/^production's control endpoint (\S+)$/, '生产上的对照端点 $1'], [/^the control endpoint (\S+)$/, '对照端点 $1'],
  [/^the treatment Harness (\S+)$/, '候选副本 Harness $1'], [/^the treatment endpoint (\S+)$/, '候选端点 $1'], [/^the candidate (\S+)$/, '候选 Runtime $1']]
const armName = (what) => { const s = String(what || ''); const hit = ARM_NAME.find(([re]) => re.test(s)); return hit ? s.replace(hit[0], hit[1]) : s }

// Harnesses, without the treatment Harnesses experiments make (<agent>_x<8 hex>); the picker's empty choice; the error.
function useAgents(wid) {
  const [list, , error] = useList(wid, `/workspaces/${wid}/agents`, 'agents')
  const agents = list === null ? null : listOf(list).filter((a) => a.kind === 'harness' && !/_x[0-9a-f]{8}$/.test(a.name || ''))
  return [listOf(agents), firstChoice(agents, error, '这个工作区还没有 Harness'), error]
}

function StatusChip({ status }) {
  const [label, tone] = STATUS[status] || [status, '']
  return h(Chip, { tone }, label)
}

// A job of an experiment or a canary. Interrupted (the console restarted) or failed with an outcome the record settled
// (``outcome``: a promotion or rollback that moved production all the same): what to do is the record's to say, above.
// (``outcome``: [label, tone, where the next step is said]).
function JobLine({ id, title, outcome }) {
  const job = useJob(id)
  if (!job) return null
  const settledBy = outcome && job.status === 'failed' ? outcome : null
  const [label, tone] = settledBy || [JOB_LABEL[job.status] || job.status, JOB_TONE[job.status]]
  const next = (outcome && outcome[2]) || '它做到了哪一步、接下来怎么做，以上面的说明为准。'
  return h(Card, { title: title || job.label, extra: h(Chip, { tone }, label) },
    h('pre', { className: 'cs-pre' }, logLines(listOf(job.log).map(logText), 10) || (job.status === 'unreadable' ? '（读不到这个任务的日志：它怎么结束的，以上面的说明为准）' : '…')),
    job.status === 'interrupted' ? h(Note, { tone: 'warn' }, `控制台重启时这个任务还没跑完：${next}`)
      : settledBy ? h(Note, null, next) : h(JobError, { error: job.error }))
}

// what an experiment's or a canary's job logs (experiments._create, _cleanup; runtime_canary._create, _promote,
// _undo_promotion), in the page's words; the deploy module's own lines stay as they are
const LOG = [
  [/^treatment Harness (\S+): (\S+)'s configuration with the treatment's prompt \/ model$/, (m) => `候选副本 Harness ${m[1]}：${m[2]} 的配置，换上候选的 Prompt / 模型`],
  [/^control endpoint (\S+) on (\S+): version (\S+) \(DEFAULT is untouched\)$/, (m) => `${m[2]} 上的对照端点 ${m[1]}：版本 ${m[3]}（DEFAULT 不动）`],
  [/^control endpoint (\S+) on (\S+): version (\S+) \(DEFAULT and its own endpoints stay as they are\)$/, (m) => `${m[2]} 上的对照端点 ${m[1]}：版本 ${m[3]}（DEFAULT 和它自己的端点都不动）`],
  [/^candidate runtime (\S+): the deploy module's stages, with (\S+)'s role and settings$/, (m) => `候选 Runtime ${m[1]}：按部署模块的阶段部署，用 ${m[2]} 的角色和设置`],
  [/^treatment endpoint (\S+) on (\S+): version (\S+) \(its DEFAULT keeps the smoke call and verifications\)$/, (m) => `${m[2]} 上的候选端点 ${m[1]}：版本 ${m[3]}（它的 DEFAULT 留给冒烟和验证）`],
  [/^role (\S+): the Gateway(?: \(InvokeAgentRuntime of both arms\))?, the A\/B test and the online evaluations$/, (m) => `角色 ${m[1]}：给 Gateway、A/B 测试和在线评估用`],
  [/^Gateway (\S+) \(AWS_IAM\)$/, (m) => `Gateway ${m[1]}（IAM 认证）`],
  [/^target (\S+): (?:InvokeHarness )?(\S+) \((.*)\)$/, (m) => `Gateway 目标 ${m[1]}：${m[2]}（${m[3]}）`],
  [/^online evaluation (\S+): (.*) on every session of (\S+) \((.*)\)$/, (m) => `在线评估 ${m[1]}：给 ${m[3]}（${m[4]}）的每个会话跑 ${m[2]}`],
  [/^A\/B test (\S+): ([\d.]+) % (control|production), ([\d.]+) % (?:treatment|candidate)$/, (m) => `A/B 测试 ${m[1]}：${m[3] === 'production' ? '生产' : '对照'} ${m[2]}%，候选 ${m[4]}%`],
  [/^running: send sessions to (\S+)$/, (m) => `运行中：会话发到 ${m[1]}`],
  [/^running: the console's chat and \/v1 go through (\S+)$/, (m) => `运行中：控制台的对话和 /v1 经过 ${m[1]}`],
  [/^(\d+) session\(s\) through (\S+)$/, (m) => `经 ${m[2]} 发 ${m[1]} 个会话`],
  [/^failed (\S+): (.*)$/s, (m) => `失败（${m[1]}）：${m[2]}`],
  [/^stopping the A\/B test \(its results are kept with the canary\)$/, () => '停下 A/B 测试（结果随金丝雀保留）'],
  [/^publishing the candidate's artifact and variables as (\S+)'s next version \(DEFAULT moves to it(?:, and the endpoint (\S+?))?\)$/,
    (m) => `把候选的制品和变量发布成 ${m[1]} 的下一个版本（DEFAULT ${m[2] ? `和端点 ${m[2]} ` : ''}移过去）`],
  [/^version (\S+) already runs version (\S+)'s artifact and variables: rolled back$/, (m) => `版本 ${m[1]} 已经在跑版本 ${m[2]} 的制品和变量：记成已撤销`],
  [/^endpoint (\S+) → version (\S+) \(the deploy module's point_endpoint\)$/, (m) => `端点 ${m[1]} → 版本 ${m[2]}`],
  [/^DEFAULT moves only with a new version: version (\S+)'s own artifact and variables are published again$/, (m) => `DEFAULT 只随新版本移动：把版本 ${m[1]} 的制品和变量再发布一次`],
  // the deploy module's progress (deploy.Pipeline), indented as it logs them
  [/^(\s+)runtime (\w+)$/, (m) => `${m[1]}Runtime：${statusText(m[2])}`],
  [/^(\s+)endpoint (\S+) (\w+), live version (\S+)$/, (m) => `${m[1]}端点 ${m[2]}：${statusText(m[3])}，${m[4] === '-' ? '还没有在用的版本' : `在用的版本 ${m[4]}`}`],
  [/^created the console bucket (\S+) \(versioned, private, tagged\)$/, (m) => `建了控制台的桶 ${m[1]}（开了版本控制、私有、带标签）`],
  [/^created the CodeBuild project (\S+) \((.*)\)$/, (m) => `建了 CodeBuild 项目 ${m[1]}（${m[2]}）`],
  [/^created the ECR repository (\S+) \(scan on push\)$/, (m) => `建了 ECR 仓库 ${m[1]}（推送时扫描）`],
  [/^turned on scan on push for (\S+)$/, (m) => `给 ${m[1]} 开了推送时扫描`],
  [/^could not turn on scan on push for (\S+): (.*)$/, (m) => `没能给 ${m[1]} 开推送时扫描（${m[2]}）`],
  [/^CodeBuild (\S+) started \((.*)\)$/, (m) => `CodeBuild ${m[1]} 开始（${m[2]}）`],
  [/^(\s+)build phase (\S+)$/, (m) => `${m[1]}构建阶段：${m[2] === 'COMPLETED' ? '完成' : BUILD_PHASE[m[2]] || m[2]}`],
  [/^(\s+)phases: (.*)$/, (m) => `${m[1]}各阶段用时：${m[2].split(', ').map((part) => part.replace(/^(\w+) (\d+)s$/, (all, name, n) => `${PHASE_NAME[name] || name} ${n} 秒`)).join('、')}`],
  [/^(\s+)the image scan gate did NOT complete \((.*)\): deploying unscanned$/s, (m) => `${m[1]}镜像扫描门槛没完成（${unscannedText(m[2])}）：不扫描就部署`],
  [/^(\s+)image scan findings: (.*)$/, (m) => `${m[1]}镜像扫描${m[2] === 'no findings' ? '：没有发现漏洞' : `发现：${m[2].split(', ').join('、')}`}`],
  [/^(\s+)image scan (\S+)(?:: (.*))?$/s, (m) => `${m[1]}镜像扫描：${SCAN_STATE[m[2]] || statusText(m[2])}${scanWhy(m[3])}`],
  [/^created the role (\S+) on (\S+)( \(with the workspace's permissions boundary\))?$/, (m) => `建了角色 ${m[1]}（路径 ${m[2]}${m[3] ? '，带工作区的权限边界' : ''}）`],
  [/^gave the role (\S+) the workspace's permissions boundary$/, (m) => `给角色 ${m[1]} 加上了工作区的权限边界`],
  [/^(\s+)IAM not visible yet, retry (\d+)\/(\d+) in (\d+) s: .*$/s, (m) => `${m[1]}IAM 还没生效：${m[4]} 秒后第 ${m[2]}/${m[3]} 次重试`],
  [/^(\s+)read failed on the network \((\w+)\), again in (\d+) s$/, (m) => `${m[1]}网络读取失败（${m[2]}）：${m[3]} 秒后再读`],
  [/^(\s+)warning: the workspace's roles carry the permissions boundary (\S+), which lets a runtime pull only from the console's repositories \((\S+)\): (\S+) is pulled only if/,
    (m) => `${m[1]}提醒：工作区的角色带着权限边界 ${m[2]}，Runtime 只能从控制台的仓库（${m[3]}）拉镜像：${m[4]} 要账号管理员把它加进权限边界才拉得到`],
  [/^(\s+)warning: (.*)$/s, (m) => `${m[1]}提醒：${m[2]}`],
  [/^(\s+)session not stopped: (.*)$/, (m) => `${m[1]}会话没停下（${m[2]}）`],
  [/^taking back what this job created failed: (.*)$/s, (m) => `撤回这个任务建的东西时失败了：${jobErrorText(m[1]).replace(/^没做成，服务端的原话：/, '')}`],
  [/^took back: (.*)$/, (m) => `撤回了：${m[1].split(', ').map((part) => part.replace(/^(\d+) source object\(s\)$/, '$1 个源码对象')
    .replace(/^repository (\S+)$/, 'ECR 仓库 $1').replace(/^role (\S+)$/, '角色 $1')).join('、')}`],
]
// the deploy module's stages (deploy.Pipeline: the candidate's deploy, the promotion's publish), as the deploy page names them
const STAGE = { validate: '校验', upload: '上传源码', build: '构建', scan: '镜像扫描', runtime: '创建 / 更新 Runtime', ready: '等待就绪', smoke: '冒烟调用',
  endpoint: '移动端点' }
const STAGE_DONE = { succeeded: '完成', skipped: '跳过', failed: '失败' }
const stageLog = [
  [/^\[(\w+)\] started$/, (m) => `「${STAGE[m[1]] || m[1]}」开始`],
  [/^\[(\w+)\] failed: (.*)$/s, (m) => `「${STAGE[m[1]] || m[1]}」失败：${jobErrorText(m[2]).replace(/^没做成，服务端的原话：/, '')}`],
  [/^\[(\w+)\] (\w+) in ([\d.]+) s: (.*)$/s, (m) => `「${STAGE[m[1]] || m[1]}」${STAGE_DONE[m[2]] || m[2]}（${secs(Number(m[3]))}）${m[4] ? `：${stageText(m[2], m[4]).replace(/^version (\S+)/, '版本 $1')}` : ''}`],
]
// ECR's scan states and the descriptions it gives with them (the ones that say nothing beyond the state are dropped)
const SCAN_STATE = { COMPLETE: '完成', ACTIVE: '已扫描（增强扫描：之后持续监控）', PENDING: '排队中', IN_PROGRESS: '进行中', FAILED: '失败' }
const scanWhy = (why) => (!why || /^The scan was completed successfully\.?$|^Continuous scan is selected for image\.?$/.test(why) ? ''
  : `（${why === 'the push scan is not registered yet' ? '推送时扫描还没登记上' : why}）`)
function logText(line) {
  const [, at, said] = String(line).match(/^(\d\d:\d\d:\d\d )?(.*)$/s)
  const hit = LOG.find(([re]) => re.test(said)) || stageLog.find(([re]) => re.test(said))
  if (hit) return (at || '') + hit[1](said.match(hit[0]))
  const step = said.match(/^(.+?): (deleted|already gone|kept: .*|left(?:: .*)?|\d+ object version\(s\) deleted)$/s)  // a cleanup step
  return step && resourceText(step[1]) !== step[1] ? `${at || ''}${resourceText(step[1])}：${cleanedText(step[2])}` : String(line)
}

// -- verifying a treatment (a console job of kind verify: the evaluation page lists it too) -----------------------------------

function VerifyTreatment({ body, title, path, note }) {
  const { wid } = useCtx()
  const [sets, , setsError] = useList(wid, `/workspaces/${wid}/contract-sets`, 'contractSets')
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
      h(Field, { label: '契约集' }, h(Select, { value: form.contractSet, onChange: set('contractSet'), options: [['', firstChoice(sets, setsError, '还没有契约集（在「评估 → 契约集」里建）')], ...listOf(sets).map((s) => [s.id, `${s.name}（${s.count}）`])] })),
      h(Field, { label: '轮数' }, h(Select, { value: form.repeat, onChange: set('repeat'), options: [['1', '1'], ['2', '2'], ['3', '3'], ['5', '5']] })),
      h(Field, { label: 'AgentCore 评估器（测噪声带）' }, h(Select, { value: form.panel ? 'y' : 'n', onChange: (v) => set('panel')(v === 'y'), options: [['y', '一起跑'], ['n', '不跑（噪声带取最小值）']] }))),
    h(Note, null, note || '在 Agent 上逐次调用时换用候选的 Prompt / 模型，不改动 Agent。每轮每条契约一个新会话；每轮都通过才算稳定。评估器面板量出每个评估器的噪声带，发布门禁用它。'),
    job ? h(Note, { tone: doc ? (doc.robust ? 'ok' : 'warn') : null }, doc ? `${doc.holding}/${listOf(doc.contracts).length} 条契约每轮都通过（${doc.repeat} 轮）：${doc.robust ? '稳定' : '不稳定'}`
      : `${JOB_LABEL[job.status] || job.status}：${logLines(job.log, 1) || '…'}`) : null,
    h(JobError, { error: job && job.error }), h(ErrorLine, { error: error || quiet(setsError) }))
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
  if (!bundle) return loaded(null, error)
  const agentArn = bundle.agent && bundle.agent.arn
  const addVersion = () => run('add', () => call('POST', ex(wid, `/bundles/${bid}/versions`), { ...form, componentArn: agentArn || undefined }), (r) => `已加版本 ${short(r.versionId)}`)
    .then((r) => { if (r) { load(); onChange() } })
  const remove = () => window.confirm(`删除配置包 ${bundle.name}？`) && run('del', () => call('DELETE', ex(wid, `/bundles/${bid}`)), '已删除').then((r) => r && onChange(true))
  return h(Fragment, null,
    h(Card, { title: `配置包 ${bundle.name}`, extra: h('span', { className: 'cs-row' }, bundle.agent ? h(Chip, null, bundle.agent.name) : null,
      bundle.console ? h(Button, { kind: 'danger', onClick: remove, busy: busy === 'del' }, '删除') : null) },
    h(Table, { head: ['版本', '时间', '说明', 'Prompt', '模型', ''], rows: listOf(bundle.versions).map((v) => {
      const conf = (agentArn && v.components[agentArn]) || Object.values(v.components || {})[0] || {}
      return { key: v.versionId, cells: [h('span', { className: 'cs-mono', title: v.versionId }, short(v.versionId)), h(At, { iso: v.createdAt }),
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
  const [agents, noAgent, agentsError] = useAgents(wid)
  const [bundles, load, listError] = useList(wid, ex(wid, '/bundles'), 'bundles')
  const [open, setOpen] = useState(null)
  const [form, setForm] = useState({ agentId: '', name: '', systemPrompt: '', model: '', commitMessage: '' })
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  useEffect(() => {
    if (!form.agentId) return
    call('GET', `/workspaces/${wid}/agents/harness/${form.agentId}`).then((a) => setForm((f) => ({ ...f, systemPrompt: a.systemPrompt || '', model: a.model || '' }))).catch(loadFailed('这个 Agent 现在的 Prompt 和模型'))
  }, [form.agentId])
  const create = () => run('create', () => call('POST', ex(wid, '/bundles'), { ...form, name: form.name || undefined }),
    (r) => (r.treatmentVersion ? `已建配置包：对照 ${short(r.controlVersion)}，候选 ${short(r.treatmentVersion)}` : '已建配置包（只有当前配置这一版）'))
    .then((r) => { if (r) { load(); setOpen(r.id) } })
  return h(Fragment, null,
    h(Card, { title: '配置包', extra: h(Button, { onClick: load }, '刷新') }, loaded(bundles, listError, (all) => h(Table, {
      head: ['名称', 'Agent', '创建', '来源'], rows: all.map((b) => ({ key: b.id, onClick: () => setOpen(b.id), cells: [h('b', null, b.name), b.agent ? b.agent.name : '—',
        h(At, { iso: b.createdAt }), b.console ? h(Chip, { tone: 'info' }, '控制台建的') : '别处建的'] })), empty: '还没有配置包：在下面从 Agent 的当前配置新建一个。' }))),
    open ? h(BundleDetail, { bid: open, onChange: (gone) => { load(); if (gone === true) setOpen(null) } }) : null,
    h(Card, { title: '从 Agent 的当前配置新建（第一版 = 对照；改了 Prompt 或模型就再存一版 = 候选）',
      extra: h(Button, { kind: 'pri', onClick: create, busy: busy === 'create', disabled: !form.agentId }, '新建') },
    h('div', { className: 'cs-grid' },
      h(Field, { label: 'Agent（Harness）' }, h(Select, { value: form.agentId, onChange: set('agentId'), options: [['', noAgent], ...agents.map((a) => [a.id, a.name])] })),
      h(Field, { label: '名称（可选）' }, h(Input, { value: form.name, onChange: set('name'), mono: true, placeholder: '字母开头，字母、数字或下划线' })),
      h(Field, { label: '候选的模型' }, h(Input, { value: form.model, onChange: set('model'), mono: true }))),
    h(Field, { label: '候选的系统 Prompt（已填入当前的，改动它）' }, h(TextArea, { value: form.systemPrompt, onChange: set('systemPrompt'), rows: 8 })),
    h(Field, { label: '候选版本说明（可选）' }, h(Input, { value: form.commitMessage, onChange: set('commitMessage') })),
    h(ErrorLine, { error: error || quiet(agentsError) })))
}

// -- the gate -------------------------------------------------------------------------------------------------------------

// the A/B test is over (AgentCore stopped it, or the record says stopped / cleaned / promoted): its results are final;
// recording: the cleanup is stopping it and has not recorded them yet
const ended = (rec) => Boolean((rec.live && rec.live.final) || awsStopped(rec) || !['running', 'paused'].includes(rec.status))
const recording = (rec) => rec.status === 'cleaning' && Boolean(rec.abTest) && !rec.finalResults && !listOf(rec.metrics).length

function said(c, rec, frozen) {
  if (c.id === 'verification') {
    const v = c.verification
    if (!v) return c.ok ? c.evidence : '还没有对这个候选配置的控制台验证'
    if (v.agentVersion && rec && v.agentVersion !== rec.agent.version) return `验证 ${v.id} 跑在版本 ${v.agentVersion} 上，实验比较的是版本 ${rec.agent.version}：再验证一次`
    return v.robust ? `验证 ${v.id}：${v.contracts} 条契约在 ${v.repeat} 轮里每轮都通过` : `验证 ${v.id}：${v.holding}/${v.contracts} 条契约每轮都通过（${v.repeat} 轮），不稳定`
  }
  if (c.id === 'ab') {
    if (!c.control) {
      if (frozen) return `发布时还没有 ${c.metric} 的 A/B 结果`
      if (!rec || !ended(rec)) return `还没有 ${c.metric} 的 A/B 结果（AgentCore 在会话结束后约 15 分钟发布）`
      return recording(rec) ? `${c.metric} 的结果正在记下（清理先停下 A/B 测试）` : `没有 ${c.metric} 的 A/B 结果：测试结束时还没有结果`
    }
    if (c.delta === undefined) return `样本太少：对照 ${c.control.n}，候选 ${c.treatment.n}（每组至少 ${MIN_SAMPLES}）`
    return `${c.metric}：对照 ${num(c.control.mean)}（n=${c.control.n}），候选 ${num(c.treatment.mean)}（n=${c.treatment.n}），Δ ${c.delta >= 0 ? '+' : ''}${num(c.delta)} ${c.worse ? '<' : '≥'} −${c.band}（噪声带${/measured/.test(c.bandSource) ? '，验证面板测得' : '，最小值：面板没测这个评估器'}）`
  }
  if (c.id === 'metric') return `实验声明按 ${c.declared} 决定；改按 ${c.metric} 决定算覆盖门禁（会记录）`
  // a canary compares production's versions (rec.copy is its candidate runtime), an experiment the agent's
  const [who, during, arms] = rec && rec.copy ? ['生产', '金丝雀', '生产上的对照端点和候选 Runtime '] : ['Agent ', '实验', '对照端点和候选副本']
  if (c.ok) return `${who}仍是${during}比较的版本 ${c.compared || (rec ? rec.agent.version : '')}，${arms}${!frozen && rec && cleanupStarted(rec) ? '随清理删了，不再检查' : '也没动'}`
  if (c.arm) {
    const a = c.arm
    const what = armName(a.what)
    if (a.now === null || a.now === undefined) return `${what} 不在了：A/B 比较的是版本 ${a.expected}`
    if (a.now === a.expected) return `${what} 正从 A/B 比较的版本 ${a.expected} 移到版本 ${a.target}`
    return `${what} 现在是版本 ${a.now}${a.target && a.target !== a.now ? `（正移到 ${a.target}）` : ''}，A/B 比较的是版本 ${a.expected}：证据说的是另一个版本`
  }
  return c.compared && c.now !== c.compared ? `${who}在${during}期间从版本 ${c.compared} 变成了 ${c.now ?? '（读不到）'}：A/B 比较的已不是它现在的配置` : c.evidence
}

// the gate card's title and verdict: decided at a promotion, a record only (closed), or what a promotion now needs
function gateHeading(shown, closed) {
  if (shown.decidedAt) return { title: `发布门禁（发布时 ${when(shown.decidedAt)} 的证据）`, verdict: shown.ok ? '当时通过' : '当时没通过' }
  if (closed) return { title: '发布门禁（只作记录）', verdict: shown.ok ? '条件都满足' : '条件没满足' }
  return { title: '发布门禁（证据决定，不是点一下）', verdict: shown.ok ? '可以发布' : '不能发布' }
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
  const stops = ['running', 'paused'].includes(rec.status) ? '发布会先停下 A/B 测试（结果保留，之后不能再继续）。' : ''
  const promote = () => window.confirm(override ? `在门禁没通过时发布：覆盖会连同原因记录下来。${stops}继续？` : `把候选配置发布到 ${rec.agent.name}（新版本，DEFAULT 立即使用）？${stops}`) &&
    run('promote', () => call('POST', ex(wid, `/ab-tests/${rec.id}/promote`), override ? { acknowledged: true, reason } : {}).catch((err) => {
      if (err.data && err.data.gate) setRefused(err.data)
      throw err
    }), (r) => `已发布：${r.agent} 版本 ${r.fromVersion} → ${r.toVersion}`).then((r) => r && onDone())
  const closed = !rec.promotion && closedText(rec, '实验')
  const { title, verdict } = gateHeading(shown, closed)
  return h(Card, { title, extra: h(Chip, { tone: shown.ok ? 'ok' : 'bad' }, verdict) },
    listOf(shown.conditions).map((c) => h('div', { key: c.id, className: 'cs-note' }, h(Chip, { tone: c.ok ? 'ok' : 'bad' }, c.ok ? '✓' : '✗'), ' ', h('b', null, GATE[c.id] || c.id), '：',
      h('span', { title: c.evidence }, said(c, rec, Boolean(shown.decidedAt))))),
    rec.promotion ? h(Note, { tone: 'ok' }, `已于 ${when(rec.promotion.at)} 由 ${rec.promotion.by} 发布：版本 ${rec.promotion.fromVersion} → ${rec.promotion.toVersion}${rec.promotion.override ? `（覆盖了门禁：${rec.promotion.reason}）` : ''}`)
      : closed ? h(Note, null, closed) : !admin ? h(Note, null, '发布需要管理员。')
        : h(Fragment, null,
          !shown.ok ? h('label', { className: 'cs-row' }, h('input', { type: 'checkbox', checked: override, onChange: (e) => setOverride(e.target.checked) }), '管理员覆盖：证据不足也发布（会记录覆盖、没通过的条件和原因）') : null,
          override && !shown.ok ? h(Field, { label: '原因（必填）' }, h(TextArea, { value: reason, onChange: setReason, rows: 2 })) : null,
          h(Button, { kind: 'pri', onClick: promote, busy: busy === 'promote', disabled: !shown.ok && !(override && reason.trim().length >= 5) }, '发布候选配置（100%）')),
    h(ErrorLine, { error }))
}

// -- one experiment --------------------------------------------------------------------------------------------------------

// An A/B test AgentCore stopped on its own (its maximum duration, or someone in its console) while the record still says
// running or paused: the page shows it stopped, and 停止 records its final results (AWS is not called again).
const awsStopped = (rec) => Boolean(rec.live && rec.live.executionStatus === 'STOPPED' && ['running', 'paused'].includes(rec.status))
const AWS_STOPPED = 'AgentCore 已经停了这个 A/B 测试（到了最长运行时间，或在 AgentCore 控制台被停掉）：结果保留，不能再暂停、改分流或发流量。'
  + '点「停止（记下最终结果）」把它记成已停止，再看结果、发布或清理。'
const runStatus = (rec) => (awsStopped(rec) ? 'stopped' : rec.status)
// AgentCore's own run state beside the record's, only when it says something the record's chip does not
// (a promotion or a rollback stops the test: STOPPED beside them would read as the promotion stopped)
const awsChip = (rec) => Boolean(rec.live && rec.live.executionStatus && rec.live.executionStatus !== 'STOPPED' && !awsStopped(rec)
  && statusText(rec.live.executionStatus) !== (STATUS[rec.status] || [])[0])

function Results({ rec }) {
  const rows = listOf(rec.metrics)
  // final once the test is over (AgentCore stopped it, or the record says stopped / cleaned / promoted): the record's results
  const final = ended(rec)
  const analysed = (rec.live && rec.live.analysisTimestamp) || (rec.finalResults && rec.finalResults.analysisTimestamp)
  const busy = recording(rec)  // the cleanup stops the test and records its results first
  const empty = !final ? '还没有结果：先经实验网关发流量，AgentCore 在会话结束后约 15 分钟发布（轨迹级评估器按每轮计数）。'
    : busy ? '清理会先停下 A/B 测试、记下最终结果，几秒后显示在这里。'
      : '这个 A/B 测试结束时还没有结果（没有经实验网关的会话，或 AgentCore 还没来得及分析）。'
  return h(Card, { title: `${final ? '最终结果' : '实时结果'}（${analysed ? `AgentCore 分析于 ${when(analysed)}` : busy ? '正在记下' : final ? '没有分析' : '还没有分析'}）` },
    h(Table, { head: ['评估器', '对照 均分（n）', '候选 均分（n）', 'Δ（越大越好）', '噪声带', 'p 值', '显著'], rows: rows.map((m) => {
      const delta = typeof m.treatment.mean === 'number' && typeof m.control.mean === 'number' ? m.polarity * (m.treatment.mean - m.control.mean) : null
      return { key: m.evaluator, cells: [h('b', null, m.evaluator, m.evaluator === rec.metric ? h(Chip, { tone: 'info' }, ' 门禁指标') : null), `${num(m.control.mean)}（${m.control.n}）`,
        `${num(m.treatment.mean)}（${m.treatment.n}）`, delta === null ? '—' : h(Chip, { tone: delta < -m.band ? 'bad' : delta > m.band ? 'ok' : '' }, `${delta >= 0 ? '+' : ''}${num(delta)}`),
        `±${m.band}`, typeof m.treatment.pValue === 'number' && m.treatment.pValue < 0.001 ? '<0.001' : num(m.treatment.pValue), m.treatment.significant ? '是' : '否'] }
    }), empty }))
}

function Ramp({ rec, reload, base }) {
  const { wid, me } = useCtx()
  const admin = Boolean(me && me.role === 'admin')
  const at = base || ex(wid, `/ab-tests/${rec.id}`)
  const { busy, error, run } = useAction()
  const live = rec.live || {}
  const t1 = (live.weights && live.weights.T1) || rec.weights.T1
  const stopped = awsStopped(rec)
  const active = ['running', 'paused'].includes(rec.status) && !stopped
  const go = (weight, acknowledged) => run(`w${weight}`, () => call('POST', `${at}/split`, { treatmentWeight: weight, ...(acknowledged ? { acknowledged: true } : {}) })
    .catch((err) => {
      if (!(err.data && err.data.condition)) throw err
      if (!window.confirm(`候选比${base ? '生产' : '对照'}差到噪声带之外，仍要把它的流量提到 ${weight}%？`)) throw cancelled()
      return call('POST', `${at}/split`, { treatmentWeight: weight, acknowledged: true })
    }), `候选流量 ${weight}%`).then((r) => r && reload())
  const state = (s, label) => run(s, () => call('POST', `${at}/state`, { executionStatus: s }), label).then((r) => r && reload())
  return h(Card, { title: '金丝雀（候选拿到的会话比例）', extra: stopped
    ? h(Button, { kind: 'danger', onClick: () => window.confirm('把它记成已停止，并记下最终结果？（AgentCore 那边已经停了）') && state('STOPPED', '已记成已停止'), busy: busy === 'STOPPED' }, '停止（记下最终结果）')
    : active ? h('span', { className: 'cs-row' },
      rec.status === 'running' ? h(Button, { onClick: () => state('PAUSED', '已暂停：所有会话走对照'), busy: busy === 'PAUSED' }, '暂停') : h(Button, { onClick: () => state('RUNNING', '已继续'), busy: busy === 'RUNNING' }, '继续'),
      h(Button, { kind: 'danger', onClick: () => window.confirm('停止 A/B 测试？结果会保留，但不能再继续。') && state('STOPPED', '已停止'), busy: busy === 'STOPPED' }, '停止')) : null },
  h('div', { className: 'cs-row' }, [...CANARY, 100].map((w, i) => h(Fragment, { key: w }, i ? h('span', { className: 'cs-mut' }, '→') : null,
    w === 100 ? h(Chip, { tone: rec.status === 'promoted' ? 'ok' : '' }, '100%：发布（见门禁）')
      : h(Button, { kind: w === t1 ? 'pri' : undefined, onClick: () => go(w), busy: busy === `w${w}`, disabled: !active || w === t1 || !admin }, `${w}%`)))),
  h(Note, null, `现在：对照 ${100 - t1}%，候选 ${t1}%（${live.executionStatus ? statusText(live.executionStatus) : (STATUS[rec.status] || [rec.status])[0]}）。改权重时控制台会自动暂停、改完再恢复（约十几秒，这期间所有会话都走${base ? '生产' : '对照'}）。候选更差（超出噪声带）时不能往上提，除非确认。`
    + `改权重需要管理员；门禁通过之前候选最多 ${MAX_UNPROVEN}%（再往上就是没有证据的发布）。`),
  stopped ? h(Note, { tone: 'warn' }, AWS_STOPPED) : null,
  h(ErrorLine, { error }))
}

function Traffic({ rec, base, hint }) {
  const { wid } = useCtx()
  const at = base || ex(wid, `/ab-tests/${rec.id}`)
  const [sets, , setsError] = useList(wid, `/workspaces/${wid}/contract-sets`, 'contractSets')
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
      h(Field, { label: '契约集的问题' }, h(Select, { value: form.contractSet, onChange: set('contractSet'), options: [['', sets === null && setsError ? '读不到契约集：手写问题' : '不用，手写'], ...listOf(sets).map((s) => [s.id, `${s.name}（${s.count}）`])] })),
      h(Field, { label: '每个问题几次' }, h(Select, { value: form.repeat, onChange: set('repeat'), options: [['1', '1'], ['2', '2'], ['3', '3'], ['5', '5']] }))),
    form.contractSet ? null : h(Field, { label: '问题（每行一个）' }, h(TextArea, { value: form.prompts, onChange: set('prompts'), rows: 4 })),
    h(Note, null, '每个问题一个新会话，由 AgentCore Gateway 按权重分给对照或候选（同一会话始终在同一组）。只有经过这个网关的会话才参与 A/B：'),
    rec.invokeUrl ? h('pre', { className: 'cs-pre' }, `POST ${rec.invokeUrl}\n# SigV4（服务名 bedrock-agentcore），${hint || '请求体与 InvokeHarness 相同：{"messages": [...], "actorId": "..."}'}\n# 头 X-Amzn-Bedrock-AgentCore-Runtime-Session-Id：会话 ID（33-100 字符）`) : null,
    job ? h(Note, { tone: result ? (result.failed ? 'warn' : 'ok') : null }, result ? `发出 ${result.sent} 个会话，失败 ${result.failed}` : `${JOB_LABEL[job.status] || job.status}…`) : null,
    result ? h(Table, { head: ['问题', '回答', '错误'], rows: listOf(result.samples).map((s) => ({ key: s.sessionId, cells: [excerpt(s.query, 50), excerpt(s.answer, 80), s.error ? excerpt(s.error, 80) : '—'] })) }) : null,
    h(JobError, { error: job && job.error }), h(ErrorLine, { error }))
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
  if (!rec) return loaded(null, error)
  const reload = () => { load(); onChange() }
  const cleanup = () => window.confirm('清理这个实验在 AWS 上建的一切（A/B 测试、网关、在线评估、候选 Harness、对照端点、角色）？结果和发布记录保留。') &&
    run('clean', () => call('DELETE', ex(wid, `/ab-tests/${eid}`)), '清理已开始').then((r) => r && setTimeout(reload, 500))
  const v = rec.verification
  return h(Fragment, null,
    h(Card, { title: rec.name, extra: h('span', { className: 'cs-row' }, h(StatusChip, { status: runStatus(rec) }), awsChip(rec) ? h(AwsStatus, { status: rec.live.executionStatus, tone: '' }) : null,
      ['creating', 'cleaning', 'cleaned'].includes(rec.status) || (rec.status === 'promoted' && rec.cleanedAt) ? null : h(Button, { kind: 'danger', onClick: cleanup, busy: busy === 'clean' }, '清理')) },
    h(Table, { head: ['项', '对照', '候选'], rows: [
      ['运行在', `${rec.agent.name} 版本 ${rec.agent.version}（端点 ${rec.controlEndpoint || '—'}；发布前 DEFAULT 不动）`, rec.treatmentHarness ? `${rec.treatmentHarness.name}（副本 Harness）` : '—'],
      ['配置包版本', h('span', { className: 'cs-mono' }, short(rec.bundle.control)), h('span', { className: 'cs-mono' }, short(rec.bundle.treatment))],
      ['模型', rec.control.model || '—', rec.treatment.model || '—'],
      ['Prompt', h('span', { title: rec.control.systemPrompt }, excerpt(rec.control.systemPrompt)), h('span', { title: rec.treatment.systemPrompt }, excerpt(rec.treatment.systemPrompt))],
      ['权重', `${rec.weights.C}%`, `${rec.weights.T1}%`],
    ].map((cells, i) => ({ key: i, cells })) }),
    h(Note, null, `评估器：${listOf(rec.evaluators).join('，')}；门禁指标 ${rec.metric}。${rec.live && rec.live.expiresAt ? `A/B 最长运行到 ${when(rec.live.expiresAt)}。` : ''}`),
    rec.error && rec.cleanedAt ? history(rec.error) : h(JobError, { error: rec.error }), h(ErrorLine, { error })),
    rec.status === 'creating' || rec.status === 'failed' ? h(JobLine, { id: rec.job, title: '创建实验' }) : null,
    h(CleanupResults, { rec }),
    rec.status === 'creating' || rec.status === 'failed' || !rec.abTest ? null : h(Fragment, null,
      ['running', 'paused'].includes(rec.status) ? h(Ramp, { rec, reload }) : null,
      h(Results, { rec }),
      h(Gate, { rec, gate: rec.gate, onDone: reload }),
      ['running', 'paused', 'stopped'].includes(rec.status) && (!v || !v.robust) ? h(VerifyTreatment, { title: '验证候选配置（门禁条件一）', body: { experiment: rec.id } }) : null,
      ['running', 'paused'].includes(rec.status) && !awsStopped(rec) ? h(Traffic, { rec }) : null))
}

// -- experiments -----------------------------------------------------------------------------------------------------------

function NewExperiment({ onCreated }) {
  const { wid } = useCtx()
  const [agents, noAgent, agentsError] = useAgents(wid)
  const [bundles, , bundlesError] = useList(wid, ex(wid, '/bundles'), 'bundles')
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
    }).catch((err) => { setVersions([]); loadFailed('这个配置包的版本')(err) })
  }, [form.bundleId])
  const evaluators = split(form.evaluators)
  const create = () => run('create', () => call('POST', ex(wid, '/ab-tests'), { agentId: form.agentId, bundleId: form.bundleId, controlVersion: form.controlVersion,
    treatmentVersion: form.treatmentVersion, treatmentWeight: Number(form.treatmentWeight), evaluators, metric: form.metric || evaluators[0], acknowledged: form.acknowledged }),
  '实验创建中（约一两分钟）').then((r) => r && onCreated(r.experiment.id))
  const versionOptions = versions.map((v) => [v.versionId, `${short(v.versionId)} · ${when(v.createdAt)} · ${excerpt(v.commitMessage, 40)}`])
  return h(Card, { title: '新建 A/B 实验', extra: h(Button, { kind: 'pri', onClick: create, busy: busy === 'create',
    disabled: !form.agentId || !form.bundleId || !form.acknowledged || form.controlVersion === form.treatmentVersion }, '创建') },
  h('div', { className: 'cs-grid' },
    h(Field, { label: 'Agent（Harness）' }, h(Select, { value: form.agentId, onChange: set('agentId'), options: [['', noAgent], ...agents.map((a) => [a.id, a.name])] })),
    h(Field, { label: '配置包' }, h(Select, { value: form.bundleId, onChange: set('bundleId'), options: [['', firstChoice(bundles && mine, bundlesError, form.agentId ? '这个 Agent 还没有配置包（在「配置包」里建）' : '还没有配置包（在「配置包」里建）')], ...mine.map((b) => [b.id, b.name])] })),
    h(Field, { label: '对照版本（须是 Agent 现在的配置）' }, h(Select, { value: form.controlVersion, onChange: set('controlVersion'), options: [['', form.bundleId ? '选择' : '先选配置包'], ...versionOptions] })),
    h(Field, { label: '候选版本' }, h(Select, { value: form.treatmentVersion, onChange: set('treatmentVersion'), options: [['', form.bundleId ? '选择' : '先选配置包'], ...versionOptions] })),
    h(Field, { label: '候选起始流量' }, h(Select, { value: form.treatmentWeight, onChange: set('treatmentWeight'), options: [['5', '5%（金丝雀）'], ['25', '25%'], ['50', '50%（经典 A/B）']] })),
    h(Field, { label: '评估器（逗号分隔，最多 5 个）' }, h(Input, { value: form.evaluators, onChange: set('evaluators'), mono: true })),
    h(Field, { label: '决定发布的指标' }, h(Select, { value: form.metric || evaluators[0] || '', onChange: set('metric'), options: evaluators.filter((e) => e !== 'Builtin.Refusal').map((e) => [e, e]) }))),
  h(Note, null, '会在 AWS 上建：候选 Harness（Agent 的副本，只换 Prompt / 模型，共用它的 Memory）、Agent 上一个固定在当前版本的对照端点、一个 IAM 角色、一个 AgentCore Gateway 和两个直通目标、每组一个在线评估（100% 抽样）、一个按目标分流的 A/B 测试。生产（DEFAULT）在发布前不变。'),
  h('label', { className: 'cs-row' }, h('input', { type: 'checkbox', checked: form.acknowledged, onChange: (e) => set('acknowledged')(e.target.checked) }),
    '我知道这些资源会一直存在（在线评估按评估的会话计费），直到我清理'),
  h(ErrorLine, { error: error || quiet(agentsError) || quiet(bundlesError) }))
}

function Experiments() {
  const { wid } = useCtx()
  const [list, load, listError] = useList(wid, ex(wid, '/ab-tests'), 'experiments')
  const [open, setOpen] = useState(null)
  return h(Fragment, null,
    h(Card, { title: 'A/B 实验', extra: h(Button, { onClick: load }, '刷新') }, loaded(list, listError, (all) => h(Table, {
      head: ['名称', 'Agent', '状态', '对照 / 候选', '门禁指标', '创建'], rows: all.map((e) => ({ key: e.id, onClick: () => setOpen(e.id), cells: [h('b', null, e.name),
        e.agent.name, h(Fragment, null, h(StatusChip, { status: e.status }), e.cleanedAt && e.status !== 'cleaned' ? h(Chip, null, ' 已清理') : null), `${e.weights.C}% / ${e.weights.T1}%`, e.metric, h('span', null, h(At, { iso: e.createdAt }), e.createdBy ? ` · ${personText(e.createdBy)}` : '')] })),
      empty: '还没有实验。先在「配置包」里从 Agent 的当前配置建一个对照和候选。' }))),
    open ? h(ExperimentDetail, { eid: open, onChange: load }) : null,
    h(NewExperiment, { onCreated: (id) => { load(); setOpen(id) } }))
}

function Releases() {
  const { wid } = useCtx()
  const [list, load, listError] = useList(wid, ex(wid, '/promotions'), 'promotions')
  return h(Card, { title: '发布记录', extra: h(Button, { onClick: load }, '刷新') }, loaded(list, listError, (all) => h(Table, {
    head: ['时间', 'Agent', '版本', '改了', '门禁', '操作人'], rows: all.map((p, i) => ({ key: `${p.experiment}-${i}`, cells: [h(At, { iso: p.at }),
      `${p.agent}${p.kind === 'runtime-canary' || p.kind === 'runtime-canary-rollback' ? '（金丝雀）' : ''}`, `${p.fromVersion} → ${p.toVersion}`, listOf(p.changed).map(changedText).join('，'),
      h(Fragment, null, p.kind === 'runtime-canary-rollback' ? h(Chip, { tone: 'warn', title: p.reason || '' }, `回滚${p.reason ? `：${excerpt(p.reason, 40)}` : ''}`)
        : p.override ? h(Chip, { tone: 'warn', title: p.reason }, `覆盖（没通过：${listOf(p.failed).map((id) => CONDITION[id] || id).join('、')}）：${excerpt(p.reason, 40)}`) : h(Chip, { tone: 'ok' }, `通过（${p.metric}）`),
      p.incomplete ? h(Chip, { tone: 'warn', title: jobErrorText(p.incomplete) }, ` 没走完（DEFAULT 已换到版本 ${p.toVersion}）`) : null), p.by] })),
    empty: '还没有发布。' })))
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

// a promotion that published the candidate but did not finish (runtime_canary._incomplete): the version its endpoint is
// still on when it did not follow (null when it did, or there is none, or production cannot be read)
function endpointLeft(rec) {
  const p = rec.promotion
  const now = p && p.endpoint && rec.production ? (rec.production.endpoints || {})[p.endpoint] : undefined
  return now !== undefined && now !== p.toVersion ? (now ?? '（读不到）') : null
}
function unfinished(rec) {
  const p = rec.promotion
  const left = endpointLeft(rec)
  return `发布没走完：DEFAULT 已到版本 ${p.toVersion}${left ? `，端点 ${p.endpoint} 还在版本 ${left}` : ''}（原因见上面的说明）。`
    + (left ? `点「撤销发布」回到版本 ${p.fromVersion}，或在「部署代码」把端点 ${p.endpoint} 移到版本 ${p.toVersion}。`
      : `确认生产在版本 ${p.toVersion} 上正常，或点「撤销发布」回到版本 ${p.fromVersion}。`)
}

function ProductionState({ rec }) {
  const state = rec.production
  if (!state) return null
  const own = Object.entries(state.endpoints || {}).filter(([name]) => name !== rec.controlEndpoint)
  const promoting = rec.status === 'promoting'  // the promotion job is moving production to the candidate's version
  const undone = rec.rollback && rec.rollback.of === 'the promotion' && rec.rollback.defaultVersion ? rec.rollback : null
  const moved = state.version !== rec.agent.version && !rec.promotion && !promoting
  const p = rec.promotion
  const left = p && p.incomplete && !undone ? endpointLeft(rec) : null
  const cut = Boolean(rec.error) && ['promoting', 'rolling_back'].includes(rec.status)  // its job ended without settling it
  // after a promotion someone deployed production again (not a rollback of this canary's)
  const since = p && !undone && rec.status === 'promoted' && state.version !== p.toVersion
  const sinceUndone = undone && rec.status === 'rolled_back' && state.version !== undone.defaultVersion  // deployed again after the undo
  // the endpoint the promotion moved is elsewhere now (a rollback that did not finish moved it back first)
  const epNow = p && p.endpoint && !p.incomplete && !undone && rec.status === 'promoted' ? (state.endpoints || {})[p.endpoint] : undefined
  const epMoved = epNow !== undefined && epNow !== p.toVersion
  const after = !p ? null
    : rec.status === 'rolling_back' ? (cut ? '撤销发布的任务没跑完：控制台每次打开这个金丝雀都会再查撤销做到了哪一步。'
      : `正在撤销发布：生产回到版本 ${p.fromVersion} 的制品和变量（作为一个新版本发布），进度见「撤销发布」任务。`)
      : p.incomplete && !undone ? `发布把 DEFAULT 从版本 ${p.fromVersion} 换到了 ${p.toVersion}，但没走完${left ? `：端点 ${p.endpoint} 还在版本 ${left}` : ''}。`
        : `${p.incomplete ? `发布把 DEFAULT 从版本 ${p.fromVersion} 换到了 ${p.toVersion}（没走完）` : `发布把它从版本 ${p.fromVersion} 换到了 ${p.toVersion}`}${undone ? `；撤销发布又把 DEFAULT 换到版本 ${undone.defaultVersion}（版本 ${undone.restoredFrom} 的制品和变量）${undone.endpoint ? `，端点 ${undone.endpoint} ${undone.endpointVersion !== undone.restoredFrom ? '还在' : p.incomplete ? '在' : '移回了'}版本 ${undone.endpointVersion ?? '（读不到）'}` : ''}${undone.incomplete ? '，但撤销没走完' : ''}` : ''}`
          + `${sinceUndone ? `；之后生产又换到了版本 ${state.version}（不是这个金丝雀换的）` : ''}`
          + `${since ? `；之后生产又换到了版本 ${state.version}（${rollbackCut(rec) ? '可能是那次没走完的撤销发布出来的' : '不是这个金丝雀换的'}）` : ''}`
          + `${epMoved ? `；端点 ${p.endpoint} 现在在版本 ${epNow}${rollbackCut(rec) && epNow === p.fromVersion ? '（那次没走完的撤销已经把它移回去了）' : ''}` : ''}。`
  return h(Note, { tone: moved || cut || since || sinceUndone || epMoved || (p && p.incomplete && !undone) || (undone && undone.incomplete) ? 'warn' : 'ok' }, `生产现在：DEFAULT → 版本 ${state.version}${own.filter(([n]) => n !== 'DEFAULT').map(([n, v]) => `，${n} → 版本 ${v}`).join('')}。`,
    after || (promoting ? (cut ? '发布任务没跑完：控制台每次打开这个金丝雀都会再查生产有没有换成候选。'
      : `正在发布：生产从版本 ${rec.agent.version} 换到一个新版本（候选的代码和变量），进度见「发布候选」任务。`)
      : moved ? `金丝雀比较的是版本 ${rec.agent.version}：生产在金丝雀之外被改了。` : '发布之前生产不动（候选是旁边的另一个 Runtime）。'))
}

// a promotion or rollback that did not finish but moved production (its record says promoted / rolled back): its cause is
// a warning beside what production runs now, not a failure
// a rollback of the promotion that failed or was cut off (not refused because production moved): the version production
// runs now may be the one it published; another 撤销发布 recognises it (runtime_canary._undo_promotion)
const rollbackCut = (rec) => rec.status === 'promoted' && /^rollback: /.test(rec.error || '') && !/ now, not the promoted /.test(rec.error || '')
const settled = (rec) => Boolean(rec.error) && ((rec.status === 'promoted' && rec.promotion && rec.promotion.incomplete)
  || (rec.status === 'rolled_back' && rec.rollback && rec.rollback.incomplete))

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
  const stops = ['running', 'paused'].includes(rec.status) ? '发布会先停下 A/B 测试（结果保留，之后不能再继续）。' : ''
  const promote = () => window.confirm(override ? `在门禁没通过时发布：覆盖会连同原因记录下来。${stops}继续？` : `把候选发布成 ${rec.agent.name} 的下一个版本（${target} 立即使用它）？${stops}`) &&
    run('promote', () => call('POST', rcPath(wid, `/${rec.id}/promote`), override ? { acknowledged: true, reason } : {}).catch((err) => {
      if (err.data && err.data.gate) setRefused(err.data)
      throw err
    }), '发布已开始').then((r) => r && onDone())
  const open = ['running', 'paused', 'stopped'].includes(rec.status)
  const p = rec.promotion
  const closed = !p && closedText(rec, '金丝雀')
  const { title, verdict } = gateHeading(shown, closed)
  return h(Card, { title, extra: h(Chip, { tone: shown.ok ? 'ok' : 'bad' }, verdict) },
    listOf(shown.conditions).map((c) => h('div', { key: c.id, className: 'cs-note' }, h(Chip, { tone: c.ok ? 'ok' : 'bad' }, c.ok ? '✓' : '✗'), ' ',
      h('b', null, CANARY_GATE[c.id] || c.id), '：',
      h('span', { title: c.evidence }, c.id === 'verification' && !c.verification ? (c.ok ? c.evidence : '还没有对候选 Runtime 的控制台验证') : said(c, rec, Boolean(shown.decidedAt))))),
    p ? h(Note, { tone: p.incomplete && (rec.status === 'promoted' || (rec.status === 'rolling_back' && rec.error)) ? 'warn' : 'ok' }, `已于 ${when(p.at)} 由 ${personText(p.by)} 发布：版本 ${p.fromVersion} → ${p.toVersion}`
      + `${p.endpoint && !p.incomplete ? `（端点 ${p.endpoint} 一起移过去）` : ''}${p.override ? `（覆盖了门禁：${p.reason}）` : ''}。`
      + `${!p.incomplete ? '' : rec.status === 'promoted' ? unfinished(rec) : rec.status === 'rolling_back' ? (rec.error ? '发布没走完；之后的撤销也没跑完：原因和控制台下一步做什么，见上面的说明。'
        : '发布没走完；现在正在撤销，进度见「撤销发布」任务。') : rec.rollback ? '发布没走完，后来撤销了：见下面「撤销发布」。'
          : rec.status === 'cleaning' ? '发布没走完；清理完金丝雀的资源后，这里会说下一步。' : ''}`)
      : closed ? h(Note, null, closed) : rec.status === 'promoting' && rec.error ? h(Note, { tone: 'warn' }, '按这些证据开始的发布任务没跑完：原因和控制台下一步做什么，见上面的说明。')
        : rec.status === 'promoting' ? h(Note, { tone: 'info' }, rec.decision && rec.decision.override
          ? `正在发布（${personText(rec.decision.by)} 覆盖了门禁：${rec.decision.reason}）：进度见「发布候选」任务。` : '正在按这些证据发布：进度见「发布候选」任务。')
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
    const who = `${when(rb.at)} ${personText(rb.by)}：`
    const why = rb.reason ? `原因：${rb.reason}` : ''
    if (rb.of === 'the promotion' && rb.incomplete) {  // the version before runs again, but the job did not finish
      const back = rb.endpoint && rb.endpointVersion !== rb.restoredFrom
      return h(Card, { title: '撤销发布', extra: h(Chip, { tone: 'warn' }, '已回滚，没走完') }, h(Note, { tone: 'warn' }, `${who}撤销发布没走完（原因见上面的说明），`
        + `但 DEFAULT 已换到版本 ${rb.defaultVersion}（版本 ${rb.restoredFrom} 的制品和变量）${rb.endpoint ? `，端点 ${rb.endpoint} 在版本 ${rb.endpointVersion ?? '（读不到）'}` : ''}：不用再撤销。`
        + `到「部署代码」确认版本 ${rb.defaultVersion} 正常${back ? `，并把端点 ${rb.endpoint} 移回版本 ${rb.restoredFrom}` : ''}。${why}`))
    }
    const others = Object.entries((rb.production || {}).endpoints || {}).filter(([n]) => n !== 'DEFAULT' && n !== rec.controlEndpoint).map(([n, v]) => `，${n} → 版本 ${v}`).join('')
    return h(Card, { title: rb.of === 'the promotion' ? '撤销发布' : '回滚', extra: h(Chip, { tone: 'warn' }, '已回滚') }, rb.of === 'the promotion'
      ? h(Note, null, `${who}${rb.endpoint ? `端点 ${rb.endpoint} ${rec.promotion && rec.promotion.incomplete ? '在' : '移回'}版本 ${rb.endpointVersion}；` : ''}DEFAULT 只随新版本移动，于是把版本 ${rb.restoredFrom} 的制品和变量再发布成版本 ${rb.defaultVersion}。${why}`)
      : h(Note, { tone: rb.moved ? 'warn' : 'ok' }, rb.moved
        ? `${who}回滚结束了金丝雀（结果保留），没有动生产；生产在金丝雀之外从版本 ${rec.agent.version} 换到了 ${rb.production.version}（DEFAULT → 版本 ${rb.production.version}${others}）。${why}`
        : `${who}回滚结束了金丝雀（结果保留），生产没有动过（DEFAULT → 版本 ${rb.production.version}${others}）。${why}`))
  }
  if (rec.status === 'promoted' && rec.production && rec.production.version !== rec.promotion.toVersion && !rollbackCut(rec)) {  // deployed again since
    return h(Card, { title: '撤销发布' }, h(Note, { tone: 'warn' }, `生产现在是版本 ${rec.production.version}，不是发布出来的版本 ${rec.promotion.toVersion}（之后又部署过）：`
      + '这里不能再撤销这次发布，要回到之前的版本，到「部署代码」回滚。'))
  }
  const before = ['running', 'paused', 'stopped'].includes(rec.status)
  if (!(before || rec.status === 'promoted') || !(me && me.role === 'admin')) return null
  const go = () => window.confirm(before ? `回滚金丝雀：${runStatus(rec) === 'stopped' ? '' : '停止分流，'}结果保留，生产保持原样；回滚后不能再发布这个候选。继续？` : `撤销发布：${rec.agent.endpoint ? `端点 ${rec.agent.endpoint} 移回版本 ${rec.promotion.fromVersion}，` : ''}DEFAULT 用版本 ${rec.promotion.fromVersion} 的制品再发布一个新版本。继续？`) &&
    run('rollback', () => call('POST', rcPath(wid, `/${rec.id}/rollback`), { reason }), before ? '已回滚：生产没有动过' : '回滚已开始').then((r) => r && onDone())
  return h(Card, { title: before ? `回滚（发布前：${runStatus(rec) === 'stopped' ? '' : '停止分流，'}生产不动；之后不能再发布这个候选）` : '撤销发布（回到发布前的版本）', extra: h(Button, { kind: 'danger', onClick: go, busy: busy === 'rollback' }, before ? '回滚' : '撤销发布') },
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
  if (!rec) return loaded(null, error)
  const reload = () => { load(); onChange() }
  const admin = me && me.role === 'admin'
  const cleanup = () => window.confirm('清理这个金丝雀在 AWS 上建的一切（A/B 测试、网关、在线评估、生产上的对照端点、候选 Runtime、角色、日志组；没发布的候选制品）？结果、发布和回滚记录保留。') &&
    run('clean', () => call('DELETE', rcPath(wid, `/${cid}`)), '清理已开始').then((r) => r && setTimeout(reload, 500))
  const copy = rec.copy || {}
  const changed = listOf(rec.candidate && rec.candidate.changed)
  const jobs = [[rec.job, '创建金丝雀（候选按部署模块的阶段部署）', rec.status === 'creating' || rec.status === 'failed'], [rec.promoteJob, '发布候选', rec.status === 'promoting' || (rec.promotion && rec.promotion.incomplete),
    rec.promotion && rec.promotion.incomplete ? ['没走完', 'warn', rec.status === 'rolling_back' ? (rec.error ? '之后的撤销也没跑完：原因和下一步见上面的说明。' : '这次发布正在撤销：进度见「撤销发布」任务。')
      : rec.rollback ? '这次发布后来撤销了：见下面「撤销发布」。' : rec.status === 'cleaning' ? '正在清理金丝雀的资源：清理完后，下一步见下面「发布门禁」里的说明。'
        : '生产现在的状态见上面，下一步见下面「发布门禁」里的说明。']
      : rec.status === 'promoting' && rec.error ? ['没跑完', 'warn', '原因和控制台下一步做什么，见上面的说明。'] : null],
    [rec.rollbackJob, '撤销发布', rec.status === 'rolling_back', rec.error ? ['没跑完', 'warn', '原因和控制台下一步做什么，见上面的说明。'] : null]]
  const active = ['running', 'paused'].includes(rec.status) && !awsStopped(rec)
  return h(Fragment, null,
    h(Card, { title: rec.name, extra: h('span', { className: 'cs-row' }, h(StatusChip, { status: runStatus(rec) }), awsChip(rec) ? h(AwsStatus, { status: rec.live.executionStatus, tone: '' }) : null,
      admin && !rec.cleanedAt && !['creating', 'promoting', 'rolling_back', 'cleaning'].includes(rec.status) ? h(Button, { kind: 'danger', onClick: cleanup, busy: busy === 'clean' }, '清理') : null) },
    h(Table, { head: ['项', '对照（生产）', '候选'], rows: [
      ['运行在', `${rec.agent.name} 版本 ${rec.agent.version}（固定在它的端点 ${rec.controlEndpoint || '—'}；发布前 DEFAULT${rec.agent.endpoint ? ` 和 ${rec.agent.endpoint}` : ''} 不动）`,
        copy.name ? `${copy.name} 版本 ${copy.version || '—'}（端点 ${rec.treatmentEndpoint || '—'}；它的 DEFAULT 只留给冒烟和验证）` : '—'],
      ['改了', '—', changed.length ? changed.map(changedText).join('，') : '—'],
      ['执行角色', '生产的', '同一个（不写它）'],
      ['权重', `${rec.weights.C}%`, `${rec.weights.T1}%`],
    ].map((cells, i) => ({ key: i, cells })) }),
    h(ProductionState, { rec }),
    h(Note, null, `评估器：${listOf(rec.evaluators).join('，')}；门禁指标 ${rec.metric}。${rec.live && rec.live.expiresAt ? `A/B 最长运行到 ${when(rec.live.expiresAt)}。` : ''}`),
    copy.smoke ? h(Note, { tone: 'ok' }, h('b', null, `候选的冒烟调用（${secs(copy.smoke.seconds)}）：`), excerpt(copy.smoke.answer, 160)) : null,
    settled(rec) ? h('div', { className: 'cs-note cs-tx-warn', title: rec.error }, jobErrorText(rec.error)) : rec.error && rec.cleanedAt && !actionable(rec) ? history(rec.error)
      : h(JobError, { error: rec.error }), h(ErrorLine, { error })),
    jobs.filter(([id, , show]) => id && show).map(([id, title, , outcome]) => h(JobLine, { key: id, id, title, outcome })),
    h(CleanupResults, { rec }),
    rec.status === 'creating' || rec.status === 'failed' || !rec.abTest ? null : h(Fragment, null,
      active || awsStopped(rec) ? h(Ramp, { rec, reload, base: rcPath(wid, `/${rec.id}`) }) : null,
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
  const [runtimes, , runtimesError] = useList(wid, `/workspaces/${wid}/deployments`, 'deployments')
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
    }).catch((err) => { setDetail(null); loadFailed('这个 Runtime 的部署信息')(err) })
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
    h(Field, { label: '生产 Agent（控制台部署的 Runtime）' }, h(Select, { value: form.runtimeId, onChange: set('runtimeId'), options: [['', firstChoice(runtimes && ready, runtimesError, '还没有就绪的、控制台部署的 Runtime（在「部署代码」部署）')], ...ready.map((d) => [d.runtimeId, `${d.name}（版本 ${d.version}${d.endpoint ? `，端点 ${d.endpoint}` : ''}）`])] })),
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
  h(ErrorLine, { error: error || quiet(runtimesError) }))
}

function RuntimeCanaries() {
  const { wid } = useCtx()
  const [list, load, listError] = useList(wid, rcPath(wid), 'canaries')
  const [open, setOpen] = useState(null)
  return h(Fragment, null,
    h(Card, { title: '代码 Agent 金丝雀', extra: h(Button, { onClick: load }, '刷新') }, loaded(list, listError, (all) => h(Table, {
      head: ['名称', '生产 Agent（比较的版本）', '状态', '生产 / 候选', '门禁指标', '创建'], rows: all.map((c) => ({ key: c.id, onClick: () => setOpen(c.id), cells: [h('b', null, c.name),
        `${c.agent.name} v${c.agent.version}`, h(Fragment, null, h(StatusChip, { status: c.status }), c.cleanedAt && c.status !== 'cleaned' ? h(Chip, null, ' 已清理') : null), `${c.weights.C}% / ${c.weights.T1}%`, c.metric,
        h('span', null, h(At, { iso: c.createdAt }), c.createdBy ? ` · ${personText(c.createdBy)}` : '')] })),
      empty: '还没有金丝雀。先在「部署代码」部署一个 HTTP Agent，再在下面拿候选版本和它比。' }))),
    open ? h(CanaryDetail, { cid: open, onChange: load }) : null,
    h(NewCanary, { onCreated: (id) => { load(); setOpen(id) } }))
}

function ExperimentsPage() {
  const { workspace } = useCtx()
  const [tab, setTab] = useState('ab')
  if (!workspace) return h(NeedWorkspace, { title: 'A/B 与发布' })
  const Body = { ab: Experiments, bundles: Bundles, canary: RuntimeCanaries, releases: Releases }[tab]
  return h(Fragment, null, h('h1', null, 'A/B 与发布'),
    h(Note, null, 'A/B 由 AgentCore Gateway 分流：对照是 Agent 本身（固定在当前版本的端点），候选是只换了 Prompt / 模型的副本 Harness；只有经过实验网关的会话参与。发布要证据：候选有稳定的验证，且在门禁指标上不比对照差到噪声带之外（噪声带 = 同一个评估器重复打分时的正常波动；差得比它还多才算真的更差）。代码 Agent 的金丝雀同理：候选版本先作为旁边的另一个 Runtime 跑，发布才成为生产的新版本。'),
    h(Tabs, { value: tab, onChange: setTab, options: [['ab', 'A/B 实验'], ['bundles', '配置包'], ['canary', '代码 Agent 金丝雀'], ['releases', '发布记录']] }),
    h(Body))
}

export default { id: 'experiments', label: 'A/B 与发布', group: '评估', Page: ExperimentsPage }
