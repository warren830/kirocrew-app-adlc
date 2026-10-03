// 部署代码: an agent written in code onto AgentCore Runtime — a Python zip (direct code deploy; a requirements.txt is
// installed for ARM64 by CodeBuild), a Dockerfile build context (CodeBuild → ECR, then the image scan gate) or an
// existing ECR image (scan gate too) — as a staged console job, with the runtime options CreateAgentRuntime takes
// (session storage, lifecycle, VPC); 从模板生成：Claude Agent SDK writes a Claude Agent SDK container from a spec (no
// code) and deploys it the same way; the console's deployments with their versions and endpoints, a new version, delete.
import React from 'react'
import { call, useCtx, listOf, Card, Button, Field, Input, TextArea, Select, Chip, Note, Empty, ErrorLine, Table, useAction, useJob, Tabs, JOB_TONE,
  JOB_LABEL, STATUS_TONE } from '../ui.mjs'
import { ws, listUnder, download } from './shared.mjs'

const { useState, useEffect, createElement: h, Fragment } = React
const useList = listUnder(ws)
const MAX_BYTES = 18 * 1024 * 1024 // the console takes 25 MB JSON bodies; the zip travels base64-encoded in one

const SOURCES = [['zip', 'Python 代码 zip（直接部署）'], ['dockerfile', 'Dockerfile 构建目录 zip（CodeBuild 构建镜像）'], ['image', '已有的 ECR 镜像']]
const PROTOCOLS = [['HTTP', 'HTTP（默认）'], ['MCP', 'MCP'], ['A2A', 'A2A'], ['AGUI', 'AG-UI']]
const PYTHONS = ['PYTHON_3_13', 'PYTHON_3_12', 'PYTHON_3_11', 'PYTHON_3_10', 'PYTHON_3_14'].map((v) => [v, `Python ${v.slice(7).replace('_', '.')}`])
const SOURCE_LABEL = { zip: '代码 zip', dockerfile: 'Dockerfile', image: 'ECR 镜像' }
const LABELS = {
  deploy: { validate: '校验', upload: '上传源码', build: '构建', scan: '镜像扫描', runtime: '创建 / 更新 Runtime', ready: '等待就绪', smoke: '冒烟调用' },
  undeploy: { check: '检查', endpoints: '删除命名端点', runtime: '删除 Runtime', repository: '删除 ECR 仓库', role: '删除执行角色', sources: '删除源码' },
}
const STAGE_TONE = { running: 'info', succeeded: 'ok', failed: 'bad' }
const STAGE_TEXT = { pending: '等待', running: '进行中', succeeded: '完成', skipped: '跳过', failed: '失败' }
const SEVERITY_TONE = { CRITICAL: 'bad', HIGH: 'warn', MEDIUM: 'info' }
const SEVERITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFORMATIONAL', 'UNDEFINED']
const ranked = (counts) => Object.entries(counts || {}).sort(([a], [b]) => (SEVERITIES.indexOf(a) + 9) % 9 - (SEVERITIES.indexOf(b) + 9) % 9)
const SCAN_BLOCKS = [['CRITICAL', '有 CRITICAL 就拦下（默认）'], ['CRITICAL,HIGH', '有 CRITICAL 或 HIGH 就拦下'], ['', '只报告，不拦']]
const MOUNT = '/mnt/workspace'

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

const mb = (n) => `${(n / 1048576).toFixed(1)} MB`
const jobKind = (j) => (j.kind === 'undeploy' ? '删除' : (j.params || {}).mode === 'update' ? '新版本' : '部署')
const ids = (text) => String(text || '').split(/[\s,，]+/).map((s) => s.trim()).filter(Boolean)
const seconds = (value, unit) => (String(value || '').trim() === '' ? null : Math.round(Number(value) * unit))

// -- runtime options: session storage, lifecycle, network, the image scan gate --------------------------------------

const OPTIONS = { storage: 'keep', mount: MOUNT, idle: '', life: '', network: 'keep', subnets: '', groups: '', scan: 'CRITICAL' }
const createOptions = (overrides = {}) => ({ ...OPTIONS, storage: 'n', network: 'PUBLIC', ...overrides })

// The request fields the options make; on a new version, "keep" leaves the runtime's own.
function optionsBody(o, { container }) {
  const body = {}
  if (o.storage === 'y') body.sessionStorage = { mountPath: (o.mount || MOUNT).trim() }
  else if (o.storage === 'n') body.sessionStorage = false
  const idle = seconds(o.idle, 60)
  const life = seconds(o.life, 3600)
  if (idle !== null || life !== null) {
    if ((idle !== null && !Number.isFinite(idle)) || (life !== null && !Number.isFinite(life))) throw new Error('生命周期：空闲超时填分钟数，最长存活填小时数')
    body.lifecycle = { ...(idle !== null ? { idleRuntimeSessionTimeout: idle } : {}), ...(life !== null ? { maxLifetime: life } : {}) }
  }
  if (o.network === 'PUBLIC') body.network = 'PUBLIC'
  else if (o.network === 'VPC') {
    if (!ids(o.subnets).length || !ids(o.groups).length) throw new Error('VPC 网络：至少一个子网和一个安全组')
    body.network = { mode: 'VPC', subnets: ids(o.subnets), securityGroups: ids(o.groups) }
  }
  if (container) body.scanBlock = o.scan ? o.scan.split(',') : []
  return body
}

function RuntimeOptions({ value, onChange, mode, container }) {
  const set = (k) => (v) => onChange({ ...value, [k]: v })
  const keep = mode === 'update' ? [['keep', '保留当前']] : []
  return h(Fragment, null,
    h('div', { className: 'cs-grid' },
      h(Field, { label: '会话存储', hint: '每个会话一块持久目录：会话停止再恢复时文件还在（新版本或 14 天不用会清空）' },
        h(Select, { value: value.storage, onChange: set('storage'), options: [...keep, ['n', '不开'], ['y', '开']] })),
      value.storage === 'y' ? h(Field, { label: '挂载路径', hint: '/mnt 下一层目录' }, h(Input, { value: value.mount, onChange: set('mount'), mono: true, placeholder: MOUNT })) : null,
      h(Field, { label: '生命周期：空闲超时（分钟）', hint: mode === 'update' ? '留空沿用当前' : '留空用默认 15 分钟；1 到 480' },
        h(Input, { value: value.idle, onChange: set('idle'), placeholder: '15' })),
      h(Field, { label: '生命周期：最长存活（小时）', hint: mode === 'update' ? '留空沿用当前' : '留空用默认 8 小时；不能短于空闲超时' },
        h(Input, { value: value.life, onChange: set('life'), placeholder: '8' })),
      h(Field, { label: '网络' }, h(Select, { value: value.network, onChange: set('network'), options: [...keep, ['PUBLIC', '公网（默认）'], ['VPC', 'VPC（子网和安全组）']] })),
      container ? h(Field, { label: '镜像扫描', hint: 'ECR 推送时扫描；等扫描结果（最多 5 分钟），按这里拦下有漏洞的镜像' },
        h(Select, { value: value.scan, onChange: set('scan'), options: SCAN_BLOCKS })) : null),
    value.network === 'VPC' ? h('div', { className: 'cs-grid' },
      h(Field, { label: '子网（逗号或换行分隔）', hint: '同一个 VPC，在 AgentCore 支持的可用区（us-west-2：usw2-az1/az2/az3）' },
        h(TextArea, { value: value.subnets, onChange: set('subnets'), rows: 2, mono: true, placeholder: 'subnet-0123456789abcdef0' })),
      h(Field, { label: '安全组', hint: '和子网同一个 VPC' }, h(TextArea, { value: value.groups, onChange: set('groups'), rows: 2, mono: true, placeholder: 'sg-0123456789abcdef0' }))) : null,
    value.network === 'VPC' ? h(Note, { tone: 'warn' }, 'VPC 模式下 Runtime 只能经由这个 VPC 出网：要能访问 ECR、S3、Bedrock 和 CloudWatch Logs（NAT 网关或 VPC 端点）；公有子网也不会给它公网 IP。部署前会用 EC2 只读接口核对子网和安全组。') : null)
}

// -- the image scan, as a job keeps it ----------------------------------------------------------------------------

function ScanSummary({ scan }) {
  if (!scan) return null
  const counts = ranked(scan.counts)
  const gate = listOf(scan.block).length ? `拦截级别 ${listOf(scan.block).join(' / ')}` : '只报告，不拦'
  if (scan.status === 'UNSCANNED') return h(Note, { tone: 'warn' }, h('b', null, '镜像扫描没有完成，按未扫描部署：'), scan.reason || '—')
  if (scan.status !== 'COMPLETE') return h(Note, null, h('span', { className: 'cs-row' }, h('span', { className: 'cs-spin' }), `等待 ECR 扫描结果（${scan.repository || ''}）…`))
  const blocked = Object.keys(scan.blocking || {}).length > 0
  return h('div', { className: 'cs-note' },
    h('div', { className: 'cs-row' }, h('b', null, '镜像扫描'), blocked ? h(Chip, { tone: 'bad' }, '已拦下') : h(Chip, { tone: 'ok' }, '通过'),
      counts.length ? counts.map(([k, v]) => h(Chip, { key: k, tone: SEVERITY_TONE[k] }, `${k} ${v}`)) : h(Chip, { tone: 'ok' }, '没有发现漏洞'),
      h('span', { className: 'cs-mut' }, `${gate} · ${scan.seconds ?? '—'} s`)),
    listOf(scan.findings).length ? h('ul', { style: { margin: '6px 0 0', paddingLeft: 18 } }, listOf(scan.findings).map((f) => h('li', { key: f, className: 'cs-mono' }, f))) : null,
    blocked ? h('div', { className: 'cs-mut' }, '换一个打过补丁的基础镜像重新构建，或者在「镜像扫描」里放宽拦截级别再部署。') : null)
}

const scanBrief = (scan) => {
  if (!scan) return '—'
  if (scan.status === 'UNSCANNED') return h(Chip, { tone: 'warn' }, '未扫描')
  if (scan.status !== 'COMPLETE') return h(Chip, { tone: 'info' }, '扫描中')
  const counts = ranked(scan.counts)
  const blocked = Object.keys(scan.blocking || {}).length > 0
  return h(Chip, { tone: blocked ? 'bad' : counts.length ? 'warn' : 'ok' }, counts.length ? counts.map(([k, v]) => `${k[0]}${v}`).join(' ') : '无漏洞')
}

// -- a job's stages and log ----------------------------------------------------------------------------------------

function Stages({ job }) {
  const stages = listOf(job.progress && job.progress.stages)
  const labels = LABELS[job.kind] || {}
  if (!stages.length) return h(Empty, null, '准备中…')
  return h(Table, { head: ['阶段', '状态', '耗时', '说明'], rows: stages.map((s) => ({ key: s.name, cells: [
    h('b', null, labels[s.name] || s.name),
    s.status === 'running' ? h('span', { className: 'cs-row' }, h('span', { className: 'cs-spin' }), STAGE_TEXT.running) : h(Chip, { tone: STAGE_TONE[s.status] }, STAGE_TEXT[s.status] || s.status),
    s.seconds !== undefined && s.seconds !== null ? `${s.seconds} s` : '—',
    s.detail ? h('span', { className: s.status === 'failed' ? 'cs-err' : 'cs-mut' }, s.detail) : '—'] })) })
}

function JobPanel({ id, onDone, go, setSelected }) {
  const job = useJob(id)
  useEffect(() => { if (job && job.status !== 'running' && onDone) onDone(job) }, [job && job.status])
  if (!job) return h(Card, { title: '任务' }, h(Empty, null, '加载中…'))
  const result = job.result || {}
  const smoke = result.smoke
  const options = result.options || {}
  const chat = () => { setSelected({ kind: 'runtime', id: result.runtimeId }); go('chat') }
  return h(Card, { title: `${job.label}（${job.id}）`, extra: h('span', { className: 'cs-row' },
    job.status === 'succeeded' && job.kind === 'deploy' && result.runtimeId ? h(Button, { onClick: chat }, '和它对话') : null,
    h(Chip, { tone: JOB_TONE[job.status] }, JOB_LABEL[job.status] || job.status)) },
  h(Stages, { job }),
  h(ScanSummary, { scan: (job.progress || {}).scan }),
  smoke ? h(Note, { tone: 'ok' }, h('b', null, `冒烟调用（${smoke.qualifier}，${smoke.seconds} s）回复：`), smoke.answer) : null,
  result.runtimeId && job.kind === 'deploy' ? h('div', { className: 'cs-mut cs-mono' }, `${result.runtimeId} · 版本 ${result.version || '—'}${result.image ? ` · ${result.image}` : ''}`) : null,
  options.sessionStorage || options.lifecycle || options.network === 'VPC' ? h('div', { className: 'cs-mut' }, [
    options.sessionStorage ? `会话存储 ${options.sessionStorage}` : '', options.lifecycle ? `生命周期 ${lifecycleText(options.lifecycle)}` : '',
    options.network === 'VPC' ? '网络 VPC' : ''].filter(Boolean).join(' · ')) : null,
  job.error ? h('div', { className: 'cs-err' }, job.error) : null,
  h('pre', { className: 'cs-pre' }, listOf(job.log).slice(-40).join('\n') || '…'))
}

function lifecycleText(life) {
  if (!life) return '默认（空闲 15 分钟，最长 8 小时）'
  const idle = life.idleRuntimeSessionTimeout
  const max = life.maxLifetime
  return [idle ? `空闲 ${Math.round(idle / 60)} 分钟` : '', max ? `最长 ${(max / 3600).toFixed(max % 3600 ? 1 : 0)} 小时` : ''].filter(Boolean).join('，')
}

// -- the deployment form (a new runtime, or a new version of one) --------------------------------------------------

function DeployForm({ mode, target, onStarted }) {
  const { wid, me } = useCtx()
  const admin = me && me.role === 'admin'
  const record = (target && target.record) || {}
  const [form, setForm] = useState({ source: mode === 'update' ? (record.source || 'zip') : 'zip', name: '', entryPoint: 'main.py', pythonRuntime: 'PYTHON_3_13',
    installRequirements: 'y', instrument: 'n', imageUri: '', protocol: mode === 'update' ? 'keep' : 'HTTP', keepEnv: 'y', env: '', endpoint: record.endpoint || '', smoke: 'y',
    smokePrompt: '' })
  const [opts, setOpts] = useState(mode === 'update' ? OPTIONS : createOptions())
  const [file, setFile] = useState(null)
  const [picker, setPicker] = useState(0) // remounts the file input once a deployment started
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const archive = form.source !== 'image'
  const container = form.source !== 'zip'
  const submit = () => run('deploy', async () => {
    const body = { source: form.source, endpoint: form.endpoint.trim(), smoke: form.smoke === 'y', ...optionsBody(opts, { container }) }
    if (form.smokePrompt.trim()) body.smokePrompt = form.smokePrompt.trim()
    if (mode === 'create') body.name = form.name.trim()
    if (form.protocol !== 'keep') body.protocol = form.protocol
    if (mode === 'create' || form.keepEnv === 'n') body.environment = parseEnv(form.env)
    if (!archive) body.imageUri = form.imageUri.trim()
    else {
      if (!file) throw new Error('先选一个 zip 文件')
      if (file.size > MAX_BYTES) throw new Error(`zip 最大 18 MB（这个 ${mb(file.size)}）：依赖多就用 Dockerfile 构建，或把镜像推到 ECR 后选「已有的 ECR 镜像」`)
      body.archive = await readBase64(file)
      body.filename = file.name
      if (form.source === 'zip') Object.assign(body, { entryPoint: form.entryPoint.trim() || 'main.py', pythonRuntime: form.pythonRuntime,
        installRequirements: form.installRequirements === 'y', instrument: form.instrument === 'y' })
    }
    return call('POST', ws(wid, mode === 'create' ? '/deployments' : `/deployments/${target.runtimeId}/versions`), body)
  }, (j) => `已开始：${j.label}`).then((j) => { if (j) { setFile(null); setPicker((n) => n + 1); setForm((f) => ({ ...f, name: '' })); onStarted(j) } })
  const hint = { zip: 'zip 根目录放入口文件（默认 main.py，监听 8080：POST /invocations、GET /ping）；有 requirements.txt 时 CodeBuild 按 linux/arm64 装好依赖再部署，没有就原样直接部署。',
    dockerfile: 'zip 根目录放 Dockerfile（只有一层文件夹时会自动提出来）；CodeBuild 按 linux/arm64 构建，推到这个 Runtime 自己的 ECR 仓库（推送即扫描），扫描通过后按 digest 部署。上传里的 buildspec.yml 不会被使用。',
    image: '本工作区账号和区域里的私有 ECR 镜像（linux/arm64），用 :tag 或 @sha256: 指定；部署时固定到它当时的 digest，并读它的 ECR 扫描结果（仓库没开推送扫描就按未扫描部署）。' }[form.source]
  return h(Card, { title: mode === 'create' ? '部署一个用代码写的 Agent' : `发布 ${target.name} 的新版本`,
    extra: admin ? h(Button, { kind: 'pri', onClick: submit, busy: busy === 'deploy', disabled: (mode === 'create' && !form.name.trim()) || (archive ? !file : !form.imageUri.trim()) },
      mode === 'create' ? '部署' : '发布新版本') : null },
  !admin ? h(Note, { tone: 'warn' }, '只有管理员可以部署、发布新版本和删除。') : null,
  h('div', { className: 'cs-grid' },
    mode === 'create' ? h(Field, { label: 'Runtime 名称', hint: '字母开头，字母、数字或下划线（最多 48）' }, h(Input, { value: form.name, onChange: set('name'), mono: true, placeholder: 'order_agent' })) : null,
    h(Field, { label: '代码来源' }, h(Select, { value: form.source, onChange: set('source'), options: SOURCES })),
    archive ? h(Field, { label: form.source === 'zip' ? '代码 zip' : '构建目录 zip', hint: file ? `${file.name} · ${mb(file.size)}` : '最大 18 MB' },
      h('input', { key: picker, type: 'file', accept: '.zip,application/zip', className: 'cs-input', onChange: (e) => setFile((e.target.files && e.target.files[0]) || null) }))
      : h(Field, { label: 'ECR 镜像 URI' }, h(Input, { value: form.imageUri, onChange: set('imageUri'), mono: true, placeholder: '<账号>.dkr.ecr.<区域>.amazonaws.com/<仓库>:<tag>' })),
    form.source === 'zip' ? h(Field, { label: '入口文件' }, h(Input, { value: form.entryPoint, onChange: set('entryPoint'), mono: true })) : null,
    form.source === 'zip' ? h(Field, { label: 'Python 版本' }, h(Select, { value: form.pythonRuntime, onChange: set('pythonRuntime'), options: PYTHONS })) : null,
    form.source === 'zip' ? h(Field, { label: 'requirements.txt' }, h(Select, { value: form.installRequirements, onChange: set('installRequirements'),
      options: [['y', '有就用 CodeBuild 安装（ARM64）'], ['n', '不安装（依赖已经打进 zip）']] })) : null,
    form.source === 'zip' ? h(Field, { label: '启动方式', hint: '依赖里有 aws-opentelemetry-distro 时可以带插桩启动，链路进「观测」' },
      h(Select, { value: form.instrument, onChange: set('instrument'), options: [['n', `直接运行 ${form.entryPoint || 'main.py'}`], ['y', 'opentelemetry-instrument 启动']] })) : null,
    h(Field, { label: '协议' }, h(Select, { value: form.protocol, onChange: set('protocol'), options: mode === 'update' ? [['keep', '保留当前协议'], ...PROTOCOLS] : PROTOCOLS })),
    h(Field, { label: '命名端点（可选）', hint: mode === 'update' ? '新版本就绪后把它移到新版本；清空则只有 DEFAULT 跟随' : 'DEFAULT 总是跟随最新版本；命名端点固定在某个版本' },
      h(Input, { value: form.endpoint, onChange: set('endpoint'), mono: true, placeholder: 'live' })),
    h(Field, { label: '冒烟调用', hint: 'HTTP 协议才调用；结束后会停止这个会话' }, h(Select, { value: form.smoke, onChange: set('smoke'), options: [['y', '部署后调用一次'], ['n', '不调用']] }))),
  mode === 'update' ? h(Field, { label: '环境变量' }, h(Select, { value: form.keepEnv, onChange: set('keepEnv'), options: [['y', '保留当前的环境变量'], ['n', '换成下面这些']] })) : null,
  mode === 'create' || form.keepEnv === 'n' ? h(Field, { label: '环境变量（一行一个 KEY=VALUE）', hint: '值会存在 Runtime 的配置里，任务记录只留变量名' },
    h(TextArea, { value: form.env, onChange: set('env'), rows: 3, mono: true, placeholder: 'MODEL_ID=us.amazon.nova-2-lite-v1:0' })) : null,
  form.smoke === 'y' ? h(Field, { label: '冒烟调用的 prompt（可选）' }, h(Input, { value: form.smokePrompt, onChange: set('smokePrompt'), placeholder: '你好！这是 ADLC 控制台部署后的冒烟调用，请简短回复。' })) : null,
  h('b', { className: 'cs-mut' }, '运行时选项'),
  h(RuntimeOptions, { value: opts, onChange: setOpts, mode, container }),
  h(Note, null, hint),
  h(Note, null, '执行角色 adlc-console-rt-<名称> 由控制台创建（调用模型、日志、X-Ray、拉取它自己的镜像）；资源都带 adlc:console=1 标签，只有这样的 Runtime 能在这里更新和删除。'),
  h(ErrorLine, { error }))
}

// -- 从模板生成：Claude Agent SDK -------------------------------------------------------------------------------------

const CLAUDE_FORM = { name: '', description: '', systemPrompt: '', model: '', fastModel: '', maxTurns: '12', effort: '', tools: [], skills: [], knowledgeBases: [],
  topK: '5', sample: '', maxBudgetUsd: '' }

function toggle(list, value) { return list.includes(value) ? list.filter((v) => v !== value) : [...list, value] }

// A skill checkbox toggled: the picked names, each skill already in the form keeping its @version pin (an agent opened
// to publish again carries the versions it runs; a box ticked now takes the library's current version).
function keepPins(current, names) {
  return names.map((name) => (current || []).find((s) => String(s).split('@')[0] === name) || name)
}

// A Field for a group of checkboxes: a div, not a label (a label inside a label would toggle the wrong box).
function Group({ label, hint, children }) {
  return h('div', { className: 'cs-field' }, h('span', null, label), children, hint ? h('small', null, hint) : null)
}

function Checks({ items, picked, onChange, empty }) {
  if (items === null) return h('div', { className: 'cs-mut' }, '加载中…')
  if (!items.length) return h('div', { className: 'cs-mut' }, empty)
  return h('div', { className: 'cs-row' }, items.map(([v, label, title]) => h('label', { key: v, className: 'cs-row', title, style: { marginRight: 10 } },
    h('input', { type: 'checkbox', checked: picked.includes(v), onChange: () => onChange(toggle(picked, v)) }), label)))
}

function specBody(f) {
  return { name: f.name.trim(), description: f.description.trim(), systemPrompt: f.systemPrompt, model: f.model.trim(), fastModel: f.fastModel.trim(),
    maxTurns: f.maxTurns, effort: f.effort, tools: f.tools, skills: f.skills, knowledgeBases: f.knowledgeBases, topK: f.topK, sample: f.sample.trim(),
    maxBudgetUsd: f.maxBudgetUsd }
}

function CodePreview({ preview }) {
  const names = Object.keys((preview && preview.files) || {})
  const [file, setFile] = useState('main.py')
  if (!preview) return null
  const shown = names.includes(file) ? file : names[0]
  return h(Card, { title: `生成的构建目录（${names.length} 个文件，zip ${(preview.size / 1024).toFixed(1)} KB）`,
    extra: h('span', { className: 'cs-mut cs-mono' }, `${preview.names.repository} · ${preview.names.role}`) },
  h(Tabs, { value: shown, onChange: setFile, options: names.map((n) => [n, n]) }),
  h('pre', { className: 'cs-pre', style: { maxHeight: 420, overflow: 'auto', whiteSpace: 'pre-wrap' } }, preview.files[shown]))
}

function ClaudeAgents({ refresh, onEdit, go, setSelected }) {
  const { wid } = useCtx()
  const [list, load, error] = useList(wid, '/claude-sdk/agents', 'agents', [refresh])
  return h(Card, { title: '从这个模板部署的 Agent', extra: h(Button, { onClick: load }, '刷新') }, h(ErrorLine, { error }),
    list === null ? h(Empty, null, '加载中…') : h(Table, { head: ['名称', 'Runtime', '最近一次', '镜像扫描', '会话存储', ''],
      rows: list.map((a) => {
        const last = listOf(a.deployments)[0] || {}
        return { key: a.name, cells: [h('b', null, a.name), a.runtimeId ? h('span', { className: 'cs-mono' }, a.runtimeId) : '—',
          h(Chip, { tone: JOB_TONE[last.jobStatus] }, `${last.mode === 'update' ? '新版本' : '部署'} ${JOB_LABEL[last.jobStatus] || last.jobStatus || '—'}`),
          scanBrief(last.scan), last.sessionStorage || '—',
          h('span', { className: 'cs-row' }, h(Button, { onClick: () => onEdit(a) }, a.runtimeId ? '改了再发布' : '再部署'),
            a.runtimeId ? h(Button, { onClick: () => { setSelected({ kind: 'runtime', id: a.runtimeId }); go('chat') } }, '对话') : null)] }
      }), empty: '还没有从这个模板部署的 Agent。' }))
}

function ClaudeForm({ onStarted, refresh, go, setSelected }) {
  const { wid, me } = useCtx()
  const admin = me && me.role === 'admin'
  const [cat, setCat] = useState(null)
  const [skills, setSkills] = useState(null)
  const [kbs, setKbs] = useState(null)
  const [form, setForm] = useState(CLAUDE_FORM)
  const [opts, setOpts] = useState(createOptions({ storage: 'y' }))
  const [target, setTarget] = useState(null) // {name, runtimeId}: publishing a new version of it
  const [preview, setPreview] = useState(null)
  const [endpoint, setEndpoint] = useState('')
  const [smoke, setSmoke] = useState('y')
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  useEffect(() => {
    if (!wid) return
    call('GET', ws(wid, '/claude-sdk/catalog')).then((c) => { setCat(c); setForm((f) => ({ ...f, model: f.model || c.defaultModel, fastModel: f.fastModel || c.defaultFastModel,
      systemPrompt: f.systemPrompt || c.defaults.systemPrompt })) }).catch(() => {})
    call('GET', ws(wid, '/skill-lab/skills')).then((r) => setSkills(listOf(r.skills))).catch(() => setSkills([]))
    // one ListKnowledgeBases call; the deploy checks each chosen KB's type (MANAGED or VECTOR) itself
    call('GET', ws(wid, '/knowledge-bases')).then((r) => setKbs(listOf(r.knowledgeBases))).catch(() => setKbs([]))
  }, [wid])
  const sample = () => { const s = cat.sample; setTarget(null); setForm((f) => ({ ...f, ...s, name: f.name || `claude_points_${Math.random().toString(16).slice(2, 8)}`,
    maxTurns: String(s.maxTurns), tools: listOf(s.tools), skills: listOf(s.skills).filter((n) => listOf(skills).some((k) => k.name === n)),
    knowledgeBases: listOf(kbs).filter((k) => k.status === 'ACTIVE' && /积分|demo/i.test(`${k.name} ${k.description}`)).map((k) => k.id).slice(0, 1) })) }
  const edit = (agent) => {
    const s = agent.spec || {}
    setTarget(agent.runtimeId ? { name: agent.name, runtimeId: agent.runtimeId } : null)
    setForm({ ...CLAUDE_FORM, ...s, maxTurns: String(s.maxTurns || 12), topK: String(s.topK || 5), effort: s.effort || '', maxBudgetUsd: s.maxBudgetUsd ? String(s.maxBudgetUsd) : '',
      skills: listOf(s.skills).map((x) => (x.version ? `${x.name}@${x.version}` : x.name)), tools: listOf(s.tools), knowledgeBases: listOf(s.knowledgeBases) })
    setOpts(agent.runtimeId ? OPTIONS : createOptions({ storage: 'y' }))
    setPreview(null)
    window.scrollTo(0, 0)
  }
  const show = () => run('preview', async () => setPreview(await call('POST', ws(wid, '/claude-sdk/preview'), specBody(form))))
  const zip = () => run('zip', async () => { const out = await call('POST', ws(wid, '/claude-sdk/bundle'), specBody(form)); download(out.filename, out.archive) })
  const deployIt = () => run('deploy', () => call('POST', ws(wid, '/claude-sdk/deploy'), { ...specBody(form), ...optionsBody(opts, { container: true }),
    endpoint: endpoint.trim(), smoke: smoke === 'y', ...(target ? { runtimeId: target.runtimeId } : {}) }), (j) => `已开始：${j.label}`)
    .then((j) => { if (j) { onStarted(j) } })
  if (!cat) return h(Card, { title: '从模板生成：Claude Agent SDK' }, h(Empty, null, '加载中…'))
  const modelList = h('datalist', { id: 'claude-models' }, listOf(cat.models).map((m) => h('option', { key: m.id, value: m.id }, m.label)))
  const usable = kbs === null ? null : kbs.filter((k) => k.status === 'ACTIVE' && (!k.type || ['MANAGED', 'VECTOR'].includes(k.type)))
  return h(Fragment, null,
    h(Card, { title: target ? `改了再发布：${target.name}（${target.runtimeId}）` : '从模板生成：Claude Agent SDK', extra: h('span', { className: 'cs-row' },
      h(Button, { onClick: sample }, '填入示例'), target ? h(Button, { onClick: () => { setTarget(null); setOpts(createOptions({ storage: 'y' })) } }, '改为新部署') : null,
      h(Button, { onClick: show, busy: busy === 'preview', disabled: !form.name.trim() }, '预览代码'), h(Button, { onClick: zip, busy: busy === 'zip', disabled: !form.name.trim() }, '下载 zip'),
      admin ? h(Button, { kind: 'pri', onClick: deployIt, busy: busy === 'deploy', disabled: !form.name.trim() || !form.systemPrompt.trim() }, target ? '发布新版本' : '部署') : null) },
    h(Note, null, `不写代码：按下面的设定生成一个 Claude Agent SDK 容器（Dockerfile、main.py、技能和知识库工具），由 CodeBuild 构建成 linux/arm64 镜像，扫描通过后部署到 AgentCore Runtime。` +
      `镜像：${cat.versions.base}，Node ${cat.versions.node}，Claude Code ${cat.versions.cli}，${cat.versions.sdk}；Claude 经 Bedrock 调用（执行角色，不用 API Key）。`),
    !admin ? h(Note, { tone: 'warn' }, '只有管理员可以部署；成员可以预览和下载代码。') : null,
    modelList,
    h('div', { className: 'cs-grid' },
      h(Field, { label: 'Runtime 名称', hint: target ? '发布新版本时不能改名' : '字母开头，字母、数字或下划线（最多 48）' },
        target ? h(Input, { value: target.name, onChange: () => {}, mono: true }) : h(Input, { value: form.name, onChange: set('name'), mono: true, placeholder: 'points_claude_agent' })),
      h(Field, { label: '说明（可选）' }, h(Input, { value: form.description, onChange: set('description') })),
      h(Field, { label: '模型', hint: 'Bedrock 推理配置 ID，可以手填' }, h('input', { className: 'cs-input cs-mono', list: 'claude-models', value: form.model, onChange: (e) => set('model')(e.target.value) })),
      h(Field, { label: '后台模型', hint: 'Claude Code 的后台任务（标题、读网页的提取）；留空用主模型' },
        h('input', { className: 'cs-input cs-mono', list: 'claude-models', value: form.fastModel, onChange: (e) => set('fastModel')(e.target.value) })),
      h(Field, { label: '最多轮数（max turns）', hint: `${cat.limits.turns[0]} 到 ${cat.limits.turns[1]}：一次提问里模型最多调用几轮` }, h(Input, { value: form.maxTurns, onChange: set('maxTurns') })),
      h(Field, { label: '推理强度（effort）' }, h(Select, { value: form.effort, onChange: set('effort'), options: [['', '模型默认'], ...listOf(cat.efforts).map((e) => [e, e])] })),
      h(Field, { label: '每轮费用上限（美元，可选）', hint: '超过就停下这一轮（max_budget_usd）' }, h(Input, { value: form.maxBudgetUsd, onChange: set('maxBudgetUsd'), placeholder: '0.5' })),
      h(Field, { label: '示例问题', hint: '部署后的冒烟调用就问它' }, h(Input, { value: form.sample, onChange: set('sample'), placeholder: cat.defaults.sample }))),
    h(Field, { label: '系统 Prompt' }, h(TextArea, { value: form.systemPrompt, onChange: set('systemPrompt'), rows: 6 })),
    h(Group, { label: '内置工具', hint: '只给勾选的工具（其它工具模型看不到）；它们在 Runtime 的容器里运行，不需要确认' },
      h(Checks, { items: listOf(cat.tools).map((t) => [t.id, `${t.id}（${t.label}）`]), picked: form.tools, onChange: set('tools') })),
    h(Group, { label: '技能（来自「技能」页的技能库）', hint: '打包成 .claude/skills/<名称>/SKILL.md，模型通过 Skill 工具加载；用技能库的当前版本' },
      h(Checks, { items: skills === null ? null : skills.map((s) => [s.name, s.name, s.description]), picked: form.skills.map((s) => s.split('@')[0]),
        onChange: (names) => set('skills')(keepPins(form.skills, names)),
        empty: '技能库是空的：到「技能」页导入样例或保存一个技能。' })),
    h(Group, { label: '知识库', hint: `作为进程内 MCP 工具 ${cat.kbTool} 提供（Bedrock Retrieve）；执行角色只能检索勾选的知识库` },
      h(Checks, { items: usable === null ? null : usable.map((k) => [k.id, `${k.name}（${k.id}${k.type ? ` · ${k.type}` : ''}）`, k.description]), picked: form.knowledgeBases, onChange: set('knowledgeBases'),
        empty: '这个工作区没有可检索的知识库。' })),
    form.knowledgeBases.length ? h(Field, { label: '每次检索的段落数' }, h(Input, { value: form.topK, onChange: set('topK') })) : null,
    h('b', { className: 'cs-mut' }, target ? '运行时选项（新版本）' : '运行时选项'),
    h(RuntimeOptions, { value: opts, onChange: setOpts, mode: target ? 'update' : 'create', container: true }),
    h('div', { className: 'cs-grid' },
      h(Field, { label: '命名端点（可选）' }, h(Input, { value: endpoint, onChange: setEndpoint, mono: true, placeholder: 'live' })),
      h(Field, { label: '冒烟调用' }, h(Select, { value: smoke, onChange: setSmoke, options: [['y', '部署后用示例问题调用一次'], ['n', '不调用']] }))),
    h(Note, null, '会话存储开着时，Agent 的工作目录和 Claude Code 的对话记录都放在里面：会话停止再恢复（新的 microVM）也能接着聊、文件还在。'),
    h(ErrorLine, { error })),
    h(CodePreview, { preview }),
    h(ClaudeAgents, { refresh, onEdit: edit, go, setSelected }))
}

// -- the console's deployments --------------------------------------------------------------------------------------

function Detail({ runtimeId, stamp, onStarted, go, setSelected }) {
  const { wid, me } = useCtx()
  const admin = me && me.role === 'admin'
  const [detail, setDetail] = useState(null)
  const [updating, setUpdating] = useState(false)
  const [point, setPoint] = useState({ name: '', version: '' })
  const [keepRepo, setKeepRepo] = useState('n')
  const { busy, error, run } = useAction()
  const load = () => run('load', async () => setDetail(await call('GET', ws(wid, `/deployments/${runtimeId}`))))
  useEffect(() => { setUpdating(false); load() }, [runtimeId, stamp]) // stamp: its row in the list (status, version) changed
  if (!detail) return h(Card, { title: runtimeId }, h(ErrorLine, { error }), h(Empty, null, '加载中…'))
  const record = detail.record || {}
  const artifact = detail.artifact || {}
  const where = artifact.containerConfiguration ? artifact.containerConfiguration.containerUri
    : artifact.codeConfiguration ? `s3://${artifact.codeConfiguration.code.s3.bucket}/${artifact.codeConfiguration.code.s3.prefix} · ${artifact.codeConfiguration.runtime} · ${listOf(artifact.codeConfiguration.entryPoint).join(' ')}` : '—'
  const vpc = detail.vpc || null
  const lastScan = (listOf(record.history).slice(-1)[0] || {}).scan
  const pointTo = () => run('point', () => call('POST', ws(wid, `/deployments/${runtimeId}/endpoints`), { name: point.name.trim(), version: point.version }),
    (r) => `端点 ${r.name} ${r.action === 'created' ? '已创建' : '已移到'}版本 ${r.version}（生效约需半分钟）`).then(load)
  const dropEndpoint = (name) => window.confirm(`删除命名端点 ${name}？`) && run(`ep-${name}`, () => call('DELETE', ws(wid, `/deployments/${runtimeId}/endpoints/${name}`)), '端点删除中').then(load)
  const remove = () => window.confirm(`删除 ${detail.name}？会删除它的命名端点、Runtime、执行角色和源码${keepRepo === 'y' ? '（保留 ECR 仓库）' : '和它的 ECR 仓库'}，不可恢复。\n要几分钟：命名端点约 3 分钟，Runtime 约 5 分钟。`) &&
    run('delete', () => call('DELETE', ws(wid, `/deployments/${runtimeId}${keepRepo === 'y' ? '?keepRepo=1' : ''}`)), (j) => `已开始：${j.label}`).then((j) => { if (j) onStarted(j) })
  const chat = () => { setSelected({ kind: 'runtime', id: runtimeId }); go('chat') }
  const observe = () => { setSelected({ kind: 'runtime', id: runtimeId }); go('observability') }
  return h(Fragment, null,
    h(Card, { title: detail.name, extra: h('span', { className: 'cs-row' },
      detail.status !== 'MISSING' ? h(Button, { onClick: chat }, '对话') : null, detail.status !== 'MISSING' ? h(Button, { onClick: observe }, '观测') : null,
      h(Button, { onClick: load, busy: busy === 'load' }, '刷新'), h(Chip, { tone: STATUS_TONE[detail.status] || (detail.status === 'MISSING' ? 'warn' : '') }, detail.status)) },
    detail.status === 'MISSING' ? h(Note, { tone: 'warn' }, '这个 Runtime 已经不在了（在控制台外被删除）。可以删除控制台为它建的角色、ECR 仓库和源码。') : h(Table, { head: ['项', '值'], rows: [
      ['Runtime ID', h('code', null, detail.runtimeId)], ['当前版本', detail.version], ['制品', h('span', { className: 'cs-mono' }, where)],
      ['协议', detail.protocol || 'HTTP'], ['网络', vpc ? h('span', { className: 'cs-mono' }, `VPC · ${listOf(vpc.subnets).join(', ')} · ${listOf(vpc.securityGroups).join(', ')}`) : detail.network || '—'],
      ['会话存储', detail.sessionStorage ? h('span', { className: 'cs-mono' }, detail.sessionStorage) : '不开'], ['生命周期', lifecycleText(detail.lifecycle)],
      ['执行角色', h('span', { className: 'cs-mono' }, detail.roleArn || '—')],
      ['环境变量', listOf(detail.environment).join('，') || '—'], ['来源', SOURCE_LABEL[record.source] || record.source || '—'],
      ...(lastScan ? [['最近一次镜像扫描', scanBrief(lastScan)]] : []),
      ...(detail.failureReason ? [['失败原因', h('span', { className: 'cs-err' }, detail.failureReason)]] : []),
    ].map((cells, i) => ({ key: i, cells })) }),
    h(ErrorLine, { error })),
    detail.status !== 'MISSING' ? h(Card, { title: `版本（${listOf(detail.versions).length}）` }, h(Table, { head: ['版本', '状态', '说明', '更新'],
      rows: listOf(detail.versions).map((v) => ({ key: v.version, cells: [h('b', null, v.version), h(Chip, { tone: STATUS_TONE[v.status] }, v.status), v.description || '—',
        (v.updatedAt || '').slice(0, 19)] })) })) : null,
    detail.status !== 'MISSING' ? h(Card, { title: '端点' },
    h(Table, { head: ['端点', '服务中的版本', '目标版本', '状态', ''], rows: listOf(detail.endpoints).map((e) => ({ key: e.name, cells: [h('b', null, e.name), e.liveVersion || '—',
      e.targetVersion || '—', h(Chip, { tone: STATUS_TONE[e.status] }, e.status),
      admin && e.name !== 'DEFAULT' ? h(Button, { kind: 'danger', onClick: () => dropEndpoint(e.name), busy: busy === `ep-${e.name}` }, '删除') : null] })) }),
    admin ? h('div', { className: 'cs-row' },
      h('div', { style: { width: 200 } }, h(Input, { value: point.name, onChange: (v) => setPoint((p) => ({ ...p, name: v })), placeholder: '端点名，如 stable', mono: true })),
      h('div', { style: { width: 160 } }, h(Select, { value: point.version, onChange: (v) => setPoint((p) => ({ ...p, version: v })),
        options: [['', '选版本'], ...listOf(detail.versions).map((v) => [v.version, `版本 ${v.version}`])] })),
      h(Button, { onClick: pointTo, busy: busy === 'point', disabled: !point.name.trim() || !point.version }, '创建或指向')) : null,
    h(Note, null, 'DEFAULT 总是跟随最新版本；命名端点固定在一个版本上，回滚就是把它指回旧版本。')) : null,
    listOf(record.history).length ? h(Card, { title: '控制台的部署记录' }, h(Table, { head: ['版本', '来源', '制品', '镜像扫描', '任务', '时间'],
      rows: listOf(record.history).slice().reverse().map((x, i) => ({ key: i, cells: [x.version || '—', SOURCE_LABEL[x.source] || x.source, h('span', { className: 'cs-mono' }, x.artifact || '—'),
        x.scan ? scanBrief(x.scan) : '—', h('span', { className: 'cs-mono' }, x.jobId), (x.at || '').slice(0, 19)] })) })) : null,
    admin ? h(Card, { title: '更新与删除' },
      h('div', { className: 'cs-row' },
        detail.status !== 'MISSING' ? h(Button, { kind: 'pri', onClick: () => setUpdating((u) => !u) }, updating ? '收起' : '发布新版本') : null,
        h('div', { style: { width: 200 } }, h(Select, { value: keepRepo, onChange: setKeepRepo, options: [['n', '连同 ECR 仓库删除'], ['y', '保留 ECR 仓库']] })),
        h(Button, { kind: 'danger', onClick: remove, busy: busy === 'delete' }, '删除')),
      h(Note, null, '发布新版本 = UpdateAgentRuntime：同一个 ARN 上生成新的不可变版本，DEFAULT 跟到新版本；没填的协议、环境变量和运行时选项沿用当前（新版本会清空会话存储）。删除前会先删命名端点，等 Runtime 删完再删执行角色。')) : null,
    updating ? h(DeployForm, { mode: 'update', target: detail, onStarted: (j) => { setUpdating(false); onStarted(j) } }) : null)
}

function Deployments({ refresh, onStarted, go, setSelected }) {
  const { wid } = useCtx()
  const [list, load, error] = useList(wid, '/deployments', 'deployments', [refresh])
  const [open, setOpen] = useState(null)
  const row = open && listOf(list).find((d) => d.runtimeId === open)
  useEffect(() => { if (list && open && !row) setOpen(null) }, [list]) // deleted: close it
  return h(Fragment, null,
    h(Card, { title: '控制台部署的 Runtime', extra: h(Button, { onClick: load }, '刷新') }, h(ErrorLine, { error }), list === null ? h(Empty, null, '加载中…') : h(Table, {
      head: ['名称', '来源', '状态', '版本', '命名端点', '更新'],
      rows: list.map((d) => ({ key: d.runtimeId, onClick: () => setOpen(d.runtimeId), cells: [h('b', null, d.name), SOURCE_LABEL[d.source] || d.source || '—',
        h(Chip, { tone: STATUS_TONE[d.status] || (d.status === 'MISSING' ? 'warn' : '') }, d.status), d.version || '—', d.endpoint || '—', (d.updatedAt || '').slice(0, 19)] })),
      empty: '还没有从控制台部署的 Runtime。在「新部署」里部署一个。' })),
    row ? h(Detail, { key: open, runtimeId: open, stamp: `${row.status}:${row.version}:${row.updatedAt}`, onStarted, go, setSelected }) : null)
}

function Jobs({ refresh, openJob }) {
  const { wid } = useCtx()
  const [jobs, load] = useList(wid, '/deployments/jobs', 'jobs', [refresh])
  return h(Card, { title: '部署记录', extra: h(Button, { onClick: load }, '刷新') }, h(Table, { head: ['名称', '操作', '状态', '阶段', '镜像扫描', '开始', ''],
    rows: listOf(jobs).map((j) => ({ key: j.id, onClick: () => openJob(j.id), cells: [h('b', null, (j.params || {}).name || '—'), jobKind(j),
      h(Chip, { tone: JOB_TONE[j.status] }, JOB_LABEL[j.status] || j.status), (LABELS[j.kind] || {})[(j.progress || {}).stage] || '—', scanBrief((j.progress || {}).scan),
      (j.createdAt || '').slice(0, 19), h(Button, null, '查看')] })), empty: '还没有部署任务。' }))
}

function DeployPage({ go, setSelected }) {
  const { workspace } = useCtx()
  const [tab, setTab] = useState('new')
  const [jobId, setJobId] = useState(null)
  const [refresh, setRefresh] = useState(0)
  if (!workspace) return h(Empty, null, '先添加工作区。')
  const started = (j) => { setJobId(j.id); setRefresh((n) => n + 1) }
  const body = { new: () => h(DeployForm, { mode: 'create', onStarted: started }),
    claude: () => h(ClaudeForm, { onStarted: started, refresh, go, setSelected }),
    list: () => h(Deployments, { refresh, onStarted: started, go, setSelected }), jobs: () => h(Jobs, { refresh, openJob: setJobId }) }[tab]
  return h(Fragment, null, h('h1', null, '部署代码'),
    h(Tabs, { value: tab, onChange: setTab, options: [['new', '新部署'], ['claude', '从模板生成：Claude Agent SDK'], ['list', '已部署'], ['jobs', '部署记录']] }),
    jobId ? h(JobPanel, { id: jobId, onDone: () => setRefresh((n) => n + 1), go, setSelected }) : null,
    body())
}

export default { id: 'deploy', label: '部署代码', group: '构建', Page: DeployPage }
