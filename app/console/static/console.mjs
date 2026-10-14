// ADLC Console — the platform's console (hand-written ESM, React from the vendored UMD build, no build step). Inside
// KiroCrew the App's backend serves it under /apps/workshop-customizer/api/console/, framed by the App's page, and its
// 工作坊 item opens KiroCrew's own Workshop Customizer view; on its own, app/console/server.py serves it with the
// workshop App, the platform API (/api/console) and /v1.
import React from 'react'
import { Plus, Trash2, RefreshCw, Send } from 'lucide-react'
import { api, call, Ctx, useCtx, listOf, useConsoleState, cx, Card, Button, Field, Input, TextArea, Select, Chip, Note, Empty, ErrorLine, Table,
  STATUS_TONE, useAction, useJob, JOB_TONE, JOB_LABEL, IN_KIROCREW, openWorkshop, NeedWorkspace, loaded, AwsStatus, At, when, ROLE_LABEL,
  JobError, logLines, secs, firstChoice, quiet } from './ui.mjs'
import { MODULES } from './pages/index.mjs'

const { useState, useEffect, useRef, createElement: h, Fragment } = React

// ---------------------------------------------------------------------------
// Pages
// ---------------------------------------------------------------------------

function OverviewPage({ go }) {
  const { workspace, wid, me } = useCtx()
  const [counts, setCounts] = useState(null)
  const { busy, error, run } = useAction()
  const [identity, setIdentity] = useState(null)
  useEffect(() => {
    setCounts(null); setIdentity(null)
    if (!wid) return
    run('load', async () => {
      setIdentity(await api.verify(wid))
      const list = listOf((await api.agents(wid)).agents)
      setCounts({ harness: list.filter((a) => a.kind === 'harness').length, runtime: list.filter((a) => a.kind === 'runtime').length })
    })
  }, [wid])
  if (!workspace) return h(NeedWorkspace, { title: '概览' })
  return h(Fragment, null,
    h('h1', null, '概览'),
    h(Card, { title: `工作区 ${workspace.name}`, extra: h(Chip, null, `${workspace.accountId} · ${workspace.region}`) },
      busy ? h(Empty, null, '正在核对身份…') : identity ? h(Note, { tone: 'ok' }, `已核对：${identity.arn}`) : null,
      h(ErrorLine, { error })),
    h('div', { className: 'cs-grid' },
      h(Card, { title: 'Agent' }, h('div', { className: 'cs-big' }, counts ? counts.harness + counts.runtime : error ? '读不到' : '…'),
        h('div', { className: 'cs-mut' }, counts ? `Harness ${counts.harness} · Runtime ${counts.runtime}` : error ? (error.credential ? 'AWS 凭证用不了（见页面顶部）' : '原因见上面') : '正在读'),
        h(Button, { onClick: () => go('agents') }, '查看')),
      h(Card, { title: '新建 Agent' }, h('div', { className: 'cs-mut' }, '声明式 Harness：模型、Prompt、Gateway 工具、技能、Memory。'),
        h(Button, { kind: 'pri', icon: Plus, onClick: () => go('create') }, '新建')),
      h(Card, { title: '助手' }, h('div', { className: 'cs-mut' }, '说清楚要什么 Agent，助手出方案（带行为契约）；批准后创建并立刻验证。'),
        h(Button, { onClick: () => go('assistant') }, '开始')),
      h(Card, { title: '自动驾驶' }, h('div', { className: 'cs-mut' }, '一个客户简报 → 场景包：Kiro 生成、校验修复；内部参考包一路构建、本地预测、快速彩排到每轮都通过，客户严格确认的项目停在构建前等客户确认。'),
        h(Button, { onClick: () => go('autopilot') }, '开始')),
      h(Card, { title: '工作坊' }, h('div', { className: 'cs-mut' }, '把客户场景做成可以上课的 Workshop：Kiro 生成、彩排、上课。'),
        h(Button, { onClick: () => go('workshops') }, '打开'))),
    h(Note, null, IN_KIROCREW ? '在 KiroCrew 里打开：KiroCrew 的登录就是这里的认证（管理员，所有工作区）；状态在这个 App 的数据目录。'
      : me && me.open ? '开放模式：还没有用户，控制台只在本机可用；在「设置 → 用户与权限」添加第一个管理员后，所有访问都要登录。'
        : `当前用户：${me && me.username}（${me && (ROLE_LABEL[me.role] || me.role)}）`))
}

function AgentsPage({ go, setSelected }) {
  const { wid, workspace } = useCtx()
  const [list, setList] = useState(null)
  const { busy, error, run } = useAction()
  const load = () => run('load', async () => setList(listOf((await api.agents(wid)).agents)))
  useEffect(() => { setList(null); if (wid) load() }, [wid])
  if (!workspace) return h(NeedWorkspace, { title: 'Agent' })
  return h(Fragment, null,
    h('h1', null, 'Agent'),
    h(Card, { title: `${workspace.name} 里的 Agent`, extra: h('span', null, h(Button, { icon: RefreshCw, onClick: load, busy: busy === 'load' }, '刷新'), ' ',
      h(Button, { kind: 'pri', icon: Plus, onClick: () => go('create') }, '新建')) },
    loaded(list, error, (agents) => h(Table, {
      head: ['名称', '类型', '状态', '更新'],
      rows: agents.map((a) => ({ key: a.kind + a.id, onClick: () => { setSelected(a); go('agent') },
        cells: [h('b', null, a.name), a.kind === 'harness' ? 'Harness' : 'Runtime', h(AwsStatus, { status: a.status }), h(At, { iso: a.updatedAt })] })),
      empty: '这个工作区还没有 Agent。',
    }))))
}

function AgentDetailPage({ selected, go }) {
  const { wid, me } = useCtx()
  const [agent, setAgent] = useState(null)
  const [prompt, setPrompt] = useState('')
  const [model, setModel] = useState('')
  const { busy, error, run } = useAction()
  useEffect(() => {
    if (!selected) return
    run('load', async () => { const a = await api.agent(wid, selected.kind, selected.id); setAgent(a); setPrompt(a.systemPrompt || ''); setModel(a.model || '') })
  }, [selected && selected.id])
  if (!selected) return h(Fragment, null, h('h1', null, 'Agent 详情'), h(Empty, null, h('div', null, '还没有选 Agent：在「Agent」列表里点一行。'), h('div', { style: { marginTop: 8 } }, h(Button, { onClick: () => go('agents') }, '去 Agent 列表'))))
  if (!agent) return loaded(null, error)
  const save = () => {
    if (!agent.console && !window.confirm(`${agent.name} 不是这个控制台创建的：确定直接改它的 Prompt / 模型？（会立刻作用于它的线上流量；想先拿证据，用「A/B 与发布」）`)) return
    run('save', () => api.updateAgent(wid, agent.id, { systemPrompt: prompt !== agent.systemPrompt ? prompt : undefined, model: model !== agent.model ? model : undefined,
      acknowledged: agent.console ? undefined : true }), '已提交更新（Harness 原地更新，约半分钟）')
  }
  const remove = () => window.confirm(`删除 ${agent.name}？此操作不可恢复。`) && run('delete', () => api.deleteAgent(wid, agent.id), '已删除').then((r) => r && go('agents'))
  return h(Fragment, null,
    h('h1', null, agent.name),
    h(Card, { title: '概况', extra: h(AwsStatus, { status: agent.status }) },
      h(Table, { head: ['项', '值'], rows: [
        ['类型', agent.kind === 'harness' ? 'Harness' : 'Runtime'], ['ID', h('code', null, agent.id)], ['模型', agent.model || '—'],
        ['工具', listOf(agent.tools).map((t) => `${t.type}:${t.name}`).join('，') || '—'], ['技能', listOf(agent.skills).length],
        ['Memory', agent.memory ? h('code', null, agent.memory.arn) : '—'], ['Runtime', h('code', null, agent.runtimeId || '—')],
        ['由控制台创建', agent.console ? '是' : '否'],
      ].map((cells, i) => ({ key: i, cells })) })),
    agent.kind === 'harness' && me && me.role === 'admin' ? h(Card, { title: '修改 Prompt 与模型（直接生效；要先拿证据用「A/B 与发布」）', extra: h(Button, { kind: 'pri', onClick: save, busy: busy === 'save' }, '保存') },
      h(Field, { label: '模型' }, h(Input, { value: model, onChange: setModel, mono: true })),
      h(Field, { label: '系统 Prompt' }, h(TextArea, { value: prompt, onChange: setPrompt, rows: 12 }))) : null,
    h('div', { className: 'cs-row' }, h(Button, { onClick: () => go('chat') }, '和它对话'),
      agent.console ? h(Button, { kind: 'danger', icon: Trash2, onClick: remove, busy: busy === 'delete' }, '删除') : null),
    h(ErrorLine, { error }))
}

const MODELS = [['us.amazon.nova-2-lite-v1:0', 'Nova 2 Lite'], ['us.amazon.nova-pro-v1:0', 'Nova Pro'], ['us.anthropic.claude-haiku-4-5-20251001-v1:0', 'Claude Haiku 4.5'],
  ['us.anthropic.claude-sonnet-4-5-20250929-v1:0', 'Claude Sonnet 4.5']]

function CreateAgentPage({ go, setSelected }) {
  const { wid, workspace } = useCtx()
  const [form, setForm] = useState({ name: '', model: MODELS[0][0], systemPrompt: '你是一个乐于助人的助手。', gateways: '', skills: '', memoryArn: '' })
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  if (!workspace) return h(NeedWorkspace, { title: '新建 Agent' })
  const create = () => run('create', async () => {
    const gateways = form.gateways.split('\n').map((l) => l.trim()).filter(Boolean).map((l) => { const [arn, name] = l.split(/\s+/); return { arn, name } })
    const skills = form.skills.split('\n').map((l) => l.trim()).filter(Boolean)
    return api.createAgent(wid, { name: form.name, model: form.model, systemPrompt: form.systemPrompt, gateways, skills, memoryArn: form.memoryArn })
  }, (r) => `已创建 ${r.name}（创建中，约 1 分钟就绪）`).then((r) => { if (r) { setSelected({ kind: 'harness', id: r.id }); go('agent') } })
  return h(Fragment, null,
    h('h1', null, '新建 Agent'),
    h(Card, { title: '声明式 Harness（不写代码，不构建镜像）', extra: h(Button, { kind: 'pri', icon: Plus, onClick: create, busy: busy === 'create' }, '创建') },
      h(Field, { label: '名称', hint: '字母开头，字母、数字或下划线' }, h(Input, { value: form.name, onChange: set('name'), placeholder: 'hr_assistant', mono: true })),
      h(Field, { label: '模型' }, h(Select, { value: form.model, onChange: set('model'), options: MODELS })),
      h(Field, { label: '系统 Prompt' }, h(TextArea, { value: form.systemPrompt, onChange: set('systemPrompt'), rows: 8 })),
      h(Field, { label: 'Gateway 工具（每行：Gateway ARN 空格 工具名）', hint: '工具名只用字母和数字；Agent 会看到 <工具名>___<工具>' },
        h(TextArea, { value: form.gateways, onChange: set('gateways'), rows: 3, mono: true })),
      h(Field, { label: '技能（每行一个 s3://bucket/prefix/，目录里放 SKILL.md）' }, h(TextArea, { value: form.skills, onChange: set('skills'), rows: 2, mono: true })),
      h(Field, { label: 'Memory ARN（可选）' }, h(Input, { value: form.memoryArn, onChange: set('memoryArn'), mono: true })),
      h(Note, null, '执行角色由控制台创建（模型调用、这些 Gateway、Memory、技能所在的桶、日志）；Agent 带 adlc:console=1 标签，只有这样的 Agent 能在这里删除。'),
      h(ErrorLine, { error })))
}

function ChatPage({ selected }) {
  const { wid, workspace, me } = useCtx()
  const [agents, setAgents] = useState(null)
  const [target, setTarget] = useState(selected ? `${selected.kind}:${selected.id}` : '')
  const [turns, setTurns] = useState([])
  const [message, setMessage] = useState('')
  const [sessionId, setSessionId] = useState(null)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState(null)  // the last turn's
  const [agentsError, setAgentsError] = useState(null)
  const bottom = useRef(null)
  useEffect(() => { if (wid) api.agents(wid).then((r) => { setAgents(listOf(r.agents)); setAgentsError(null) }).catch(setAgentsError) }, [wid])
  useEffect(() => { setTurns([]); setSessionId(null) }, [target])
  useEffect(() => { if (bottom.current) bottom.current.scrollIntoView({ block: 'end' }) }, [turns])
  if (!workspace) return h(NeedWorkspace, { title: '对话' })
  const [kind, id] = target.split(':')
  const send = async () => {
    const text = message.trim()
    if (!text || !id) return
    setMessage(''); setSending(true); setError(null)
    setTurns((list) => [...list, { role: 'user', text }, { role: 'agent', text: '', tools: [], live: true }])
    const patch = (fn) => setTurns((list) => { if (!list.length) return list; const copy = list.slice(); copy[copy.length - 1] = fn({ ...copy[copy.length - 1] }); return copy })
    let heard = false  // anything of the reply arrived: keep it, with the error
    try {
      await api.chat(wid, kind, id, { message: text, sessionId, actorId: me && me.username }, (e) => {
        if (e.type !== 'session') heard = true
        if (e.type === 'session') setSessionId(e.sessionId)
        else if (e.type === 'text') patch((t) => ({ ...t, text: t.text + e.text }))
        else if (e.type === 'tool') patch((t) => ({ ...t, tools: [...t.tools, e.name] }))
        else if (e.type === 'experiment') patch((t) => ({ ...t, experiment: e }))
        else if (e.type === 'error') patch((t) => ({ ...t, error: e.error }))
        else if (e.type === 'stop') patch((t) => ({ ...t, live: false, usage: e }))
      })
      patch((t) => ({ ...t, live: false }))
    } catch (err) {
      if (heard) { patch((t) => ({ ...t, live: false })); setError(Object.assign(err, { lead: '回答没有完整收到' })) }
      else { setTurns((list) => list.slice(0, -2)); setMessage(text); setError(Object.assign(err, { lead: '没发出去（你的话放回输入框了）' })) }
    } finally { setSending(false) }
  }
  return h(Fragment, null,
    h('h1', null, '对话'),
    h(Card, { title: '和 Agent 对话（多轮：同一会话继续）', extra: h('span', { className: 'cs-row' },
      h(Select, { value: target, onChange: setTarget, options: [['', firstChoice(agents, agentsError, '这个工作区还没有 Agent', '选一个 Agent')],
        ...listOf(agents).map((a) => [`${a.kind}:${a.id}`, `${a.name}（${a.kind}）`])] }),
      h(Button, { onClick: () => { setTurns([]); setSessionId(null) } }, '新会话')) },
    h('div', { className: 'cs-chat' }, turns.length ? turns.map((t, i) => h(ChatTurn, { key: i, turn: t }))
      : h(Empty, null, id ? '问点什么。' : agents === null && agentsError ? `Agent 列表读不到，现在选不了 Agent${agentsError.credential ? '（AWS 凭证用不了，见页面顶部）' : '（原因见下面）'}。` : agents && !agents.length ? '这个工作区还没有 Agent：先在「新建 Agent」建一个。' : '先在右上角选一个 Agent。'), h('div', { ref: bottom })),
    h('div', { className: 'cs-row' }, h(TextArea, { value: message, onChange: setMessage, rows: 2, placeholder: '输入问题，Ctrl+Enter（⌘+Enter）发送', onSubmit: send }),
      h(Button, { kind: 'pri', icon: Send, onClick: send, busy: sending, disabled: !id }, '发送')),
    sessionId ? h('div', { className: 'cs-mut cs-mono' }, `会话 ${sessionId}`) : null,
    h(ErrorLine, { error: quiet(agentsError) }), h(ErrorLine, { error })))
}

function ChatTurn({ turn }) {
  return h('div', { className: cx('cs-turn', turn.role === 'user' ? 'cs-user' : 'cs-agent') },
    h('div', { className: 'cs-bubble' }, turn.text || (turn.live ? '…' : turn.error ? '' : '（没有文字）')),
    listOf(turn.tools).length ? h('div', { className: 'cs-mut' }, `工具：${turn.tools.join('，')}`) : null,
    turn.experiment ? h('div', { className: 'cs-mut' }, `经 A/B 实验「${turn.experiment.name}」分流（对照 ${turn.experiment.weights.C}% / 候选 ${turn.experiment.weights.T1}%，同一会话固定在一组）`) : null,
    turn.usage ? h('div', { className: 'cs-mut' }, `${secs(turn.usage.seconds)} · 输入 ${turn.usage.inputTokens || 0} · 输出 ${turn.usage.outputTokens || 0} tokens`) : null,
    h(JobError, { error: turn.error }))
}

function WorkshopsPage() {
  const [App, setApp] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => {
    if (IN_KIROCREW) { openWorkshop(); return }
    import(new URL('app/index.mjs', document.baseURI).href).then((m) => setApp(() => m.default)).catch(setError)
  }, [])
  if (IN_KIROCREW) {
    return h(Fragment, null, h('h1', null, '工作坊'), h(Card, { title: '工作坊在 KiroCrew 里有自己的页面' },
      h(Note, null, 'Workshop Customizer 是 KiroCrew 的 App：它的页面由 KiroCrew 直接打开，用 KiroCrew 的 Kiro 生成和这个 App 的后端，不在控制台里再放一份。'),
      h(Button, { kind: 'pri', onClick: openWorkshop }, '打开工作坊')))
  }
  if (error) return h(ErrorLine, { error })
  if (!App) return h(Empty, null, '加载工作坊…')
  return h('div', { className: 'cs-app' }, h(App))
}

// What the App's backend can do from KiroCrew's sandbox (GET /host): each check, what it found, and what to change
// when it is blocked. The kiro-cli check runs one real turn as a job.
function HostChecks({ host }) {
  return h(Table, { head: ['检查', '结果', '详情'], rows: listOf(host.checks).map((c) => ({ key: c.id, cells: [c.label,
    h(Chip, { tone: c.ok === true ? 'ok' : c.ok === false ? 'bad' : '' }, c.ok === true ? '可以' : c.ok === false ? (c.blocked ? '被沙箱挡住' : '不行') : '没检查'),
    h('div', null, h('div', { className: 'cs-mono', style: { whiteSpace: 'pre-wrap', wordBreak: 'break-all' } }, c.detail),
      c.fix ? h('div', { className: 'cs-tx-warn' }, `建议：${c.fix}`) : null)] })) })
}

function HostPage() {
  const [host, setHost] = useState(null)
  const [kiroJob, setKiroJob] = useState(null)
  const kiro = useJob(kiroJob)
  const { busy, error, run } = useAction()
  const load = (fresh) => run('load', async () => setHost(await call('GET', fresh ? '/host?fresh=1' : '/host')))
  useEffect(() => { if (IN_KIROCREW) load(false) }, [])
  if (!IN_KIROCREW) {
    return h(Fragment, null, h('h1', null, '运行环境'), h(Note, null, '这个控制台在自己的服务器上运行（app/console/server.py），不在 KiroCrew 的沙箱里：'
      + '它用这台机器的 AWS profile、网络和 kiro-cli。KiroCrew 里打开时，这一页列出沙箱里能做和不能做的事。'))
  }
  return h(Fragment, null, h('h1', null, '运行环境'),
    h(Card, { title: 'KiroCrew 里的 App 后端', extra: h(Button, { icon: RefreshCw, onClick: () => load(true), busy: busy === 'load' }, '重新检查') },
      host ? h(Fragment, null,
        h('div', { className: 'cs-mut' }, `沙箱：${host.sandbox.active ? `开（${host.sandbox.level || '?'} 档）` : '没有'} · 控制台数据：${host.consoleData}`
          + ` · Python：${host.python} · 引擎：${host.home} · ${when(host.checkedAt, true).slice(11)} 检查`),
        h(HostChecks, { host })) : h(Empty, null, busy === 'load' ? '检查中（几秒）…' : '还没检查。'),
      h(ErrorLine, { error })),
    h(Card, { title: '真跑一轮 kiro-cli', extra: h(Button, { onClick: () => run('kiro', async () => setKiroJob((await call('POST', '/host/kiro', {})).job.id)),
      busy: busy === 'kiro' || (kiro && kiro.status === 'running') }, '真跑一轮 kiro-cli') },
      h(Note, null, '从沙箱里的 App 后端启动 kiro-cli，用这个 App 的受限 agent 回一句话：自动驾驶就是这样跑 Kiro 的（大约半分钟，用一次 Kiro）。'),
      kiro ? h(Fragment, null, h(Chip, { tone: JOB_TONE[kiro.status] }, JOB_LABEL[kiro.status] || kiro.status),
        kiro.result ? h('div', { className: 'cs-mut' }, `退出码 ${kiro.result.exitCode} · ${kiro.result.seconds} 秒 · ${kiro.result.stdout.slice(-200)}`) : null,
        h(JobError, { error: kiro.error }),
        h('pre', { className: 'cs-pre' }, logLines(kiro.log) || '…')) : null))
}

// Inside KiroCrew: a line above every page when the sandbox stops something the console does.
function HostBanner({ go }) {
  const [blocked, setBlocked] = useState([])
  useEffect(() => {
    if (!IN_KIROCREW) return
    call('GET', '/host').then((info) => setBlocked(listOf(info.checks).filter((c) => c.ok === false))).catch(() => {})
  }, [])
  if (!blocked.length) return null
  return h('div', { className: 'cs-banner' }, h('b', null, '在 KiroCrew 的沙箱里，这些做不了：'), blocked.map((c) => c.label).join('；'), ' ',
    h('a', { className: 'cs-click', onClick: () => go('host') }, '看「运行环境」'))
}

// The credential advice as it reads on the 工作区 page itself: the form is below, 核对 is on the workspace's row.
const onWorkspacesPage = (message, id) => String(message)
  .replace(/在「设置 → 工作区」(把|给这个工作区)/, `点 ${id} 这一行，在下面的表单里$1`)
  .replace(/保存后点「核对」/, `保存后点 ${id} 这一行的「核对」`).replace(/再到「设置 → 工作区」点「核对」/, `再点 ${id} 这一行的「核对」`)

// Above every page, once: the current workspace's AWS credentials do not work, what to do, and the way to 核对.
const NO_AWS_PAGES = ['workshops', 'users', 'keys', 'host']  // the 工作坊 has its own AWS profile per project; these never call the workspace's AWS
function CredentialBanner({ go, page }) {
  const { credential, clearCredential, wid, workspace } = useCtx()
  if (!credential || NO_AWS_PAGES.includes(page)) return null
  const named = workspace && workspace.name && workspace.name !== wid ? `「${workspace.name}」（${wid}）` : ` ${wid} `
  const message = page === 'workspaces' ? onWorkspacesPage(credential.message, wid) : credential.message
  return h('div', { className: 'cs-banner cs-banner-bad' }, h('b', null, `工作区${named}的 AWS 凭证用不了：`), message, ' ',
    h('span', { className: 'cs-row', style: { display: 'inline-flex', marginLeft: 6 } }, page === 'workspaces' ? null : h(Button, { onClick: () => go('workspaces') }, '去「工作区」核对'),
      h('a', { className: 'cs-click', onClick: clearCredential, title: '先不提示（下一次失败会再出现）' }, '先关掉')))
}

function SpokeCard({ onUse }) {
  const [info, setInfo] = useState(null)
  const { busy, error, run } = useAction()
  const load = () => run('spoke', async () => setInfo(await api.spokeRole()))
  return h(Card, { title: '接入另一个 AWS 账号', extra: h(Button, { onClick: load, busy: busy === 'spoke' }, info ? '重新生成（会换一个 External ID）' : '生成部署命令') },
    h(Note, null, '在对方账号部署 spoke-role.yaml：它只信任这个控制台的身份，并要求 External ID；建角色、打标签、把角色交给 AgentCore、改信任和删角色都只限 IAM 路径 /adlc-console/ 下的角色'
      + '（控制台建的每个角色都在这个路径下，名字照旧：adlc-console-*、*-console-harness、*-online-eval），S3、构建和镜像权限限定在控制台自己的资源名。'
      + '它还建一个权限边界：控制台在那个账号建的每个角色都必须带上它，所以这些角色永远不能超出控制台需要的权限；别人建的角色即使名字相同，'
      + 'spoke 角色也不能给它打标签、改它或把它交给 AgentCore。部署后把输出的 RoleArn、PermissionsBoundaryArn 和同一个 External ID 填进上面「添加工作区」的表单；'
      + '用旧版模板部署过的账号，用同一条命令再部署一次。'),
    info ? h(Fragment, null,
      info.hubPrincipal ? h('div', { className: 'cs-mut' }, `控制台身份：${info.hubPrincipal}${info.exact ? '' : '（没有读到角色路径：带路径的角色要手动改成完整 ARN）'}`)
        : h(ErrorLine, { error: { message: '读不到这个控制台自己的 AWS 身份（运行控制台的这台机器的默认 AWS 凭证，不是哪个工作区的）：在这台机器的终端里登录（aws sso login 或 aws configure）后点「重新生成」；或者把下面命令里的 HubPrincipalArn 换成控制台运行用的角色或用户 ARN。',
          said: info.error || undefined } }),
      h('pre', { className: 'cs-pre', style: { whiteSpace: 'pre-wrap', wordBreak: 'break-all' } }, spokeSteps(info).join('\n')),
      h('div', { className: 'cs-row' },
        h('a', { className: 'cs-btn', href: `data:text/yaml;charset=utf-8,${encodeURIComponent(info.template)}`, download: 'spoke-role.yaml' }, '下载 spoke-role.yaml'),
        h(Button, { onClick: () => onUse(info.externalId) }, '把这个 External ID 填进表单'))) : null,
    h(ErrorLine, { error }))
}

// The steps after the spoke template, in the page's words, from the commands and the External ID the backend made.
function spokeSteps(info) {
  const command = String(info.command || '').replace('<the console role or user ARN>', '<控制台运行用的角色或用户 ARN>')
  return [`1. 在对方账号和区域部署：${command}`, `2. 读它的输出：${info.outputsCommand}`,
    `3. 在上面「添加工作区」填这个账号和区域：跨账号角色 ARN = 输出的 RoleArn，External ID = ${info.externalId}，权限边界 ARN = 输出的 ${info.boundaryOutput || 'PermissionsBoundaryArn'}`
    + '（控制台在那里建的每个角色都带上它；不填的话 spoke 角色什么角色都不让建）。',
    '4. 用旧版模板部署过的账号：用同一条命令再部署一次。控制台建的角色都在 IAM 路径 /adlc-console/ 下（spoke 角色只能建、打标签、交出或删除这个路径下的角色）；'
    + '以前建在 / 下的控制台角色会被拒绝：请账号管理员删掉它，控制台会在新路径下重建。']
}

// The spoke stack's boundary for a spoke role ARN: spoke-role.yaml names it <RoleName>-boundary in the same account.
function boundaryFor(roleArn) {
  const m = /^arn:aws:iam::(\d{12}):role\/(?:.*\/)?([\w+=,.@-]+)$/.exec(String(roleArn || '').trim())
  return m ? `arn:aws:iam::${m[1]}:policy/${m[2]}-boundary` : ''
}

const BLANK_WORKSPACE = { id: '', name: '', accountId: '', region: 'us-west-2', profile: 'default', roleArn: '', externalId: '', permissionsBoundaryArn: '' }
function WorkspacesPage() {
  const { workspaces, reload, wid, setWid, clearCredential } = useCtx()
  const [form, setForm] = useState(BLANK_WORKSPACE)
  const { busy, error, run } = useAction()
  const rows = useAction()  // 核对 and 移除: their errors show under the table, not under the form
  useEffect(() => { rows.clear() }, [wid])
  const [loadedFrom, setLoadedFrom] = useState(null)  // the workspace whose stored settings the form holds
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const stored = workspaces.find((w) => w.id === form.id.trim()) || null
  // saving replaces the whole record: an ID that is taken changes that workspace only once its settings are in the form
  const editing = Boolean(stored) && loadedFrom === stored.id
  const clash = Boolean(stored) && !editing
  // a row in the form, as stored (its External ID is never shown again: left blank, it is kept)
  const edit = (w) => { setForm({ ...BLANK_WORKSPACE, ...Object.fromEntries(Object.keys(BLANK_WORKSPACE).filter((k) => k !== 'externalId').map((k) => [k, w[k] || ''])) }); setLoadedFrom(w.id) }
  const clear = () => { setForm(BLANK_WORKSPACE); setLoadedFrom(null) }
  const current = workspaces.some((w) => w.id === wid)  // saving another workspace keeps this one; the first one becomes it
  const save = () => run('save', () => api.putWorkspace(form), (r) => `${r.id === wid ? `已保存工作区 ${r.id}` : current ? `已保存工作区 ${r.id}（现在用的还是 ${wid}）` : `已保存，并切换到工作区 ${r.id}`}`
    + `：点 ${r.id} 这一行的「核对」确认凭证能用`)
    .then((r) => { if (r) { rows.clear(); clearCredential(r.id); setLoadedFrom(r.id); reload(); if (!current) setWid(r.id) } })
  // a row's failure: whose, in this page's words (核对 is on the row, the form below it)
  const led = (lead, id, fn) => () => fn().catch((err) => { throw Object.assign(err, { lead, message: onWorkspacesPage(err.message, id) }) })
  return h(Fragment, null,
    h('h1', null, '工作区'),
    h(Card, { title: '工作区（一个 AWS 账号 + 区域）' }, h(Table, {
      head: ['ID', '名称', '账号', '区域', '凭证', ''],
      rows: workspaces.map((w) => ({ key: w.id, onClick: () => edit(w), cells: [h('b', null, w.id), w.name, w.accountId, w.region,
        w.roleArn ? `角色 ${w.roleArn.split('/').pop()}${w.permissionsBoundaryArn ? ' · 权限边界' : ' · 没有权限边界（spoke 角色不让建角色）'}` : `profile ${w.profile}${w.permissionsBoundaryArn ? ' · 权限边界' : ''}`,
        h('span', { className: 'cs-row', onClick: (e) => e.stopPropagation() }, w.id === wid ? h(Chip, { tone: 'ok' }, '当前') : h(Button, { onClick: () => setWid(w.id) }, '切换'),
          h(Button, { onClick: () => rows.run(`v-${w.id}`, led(`${w.id} 核对没通过`, w.id, () => api.verify(w.id)), (r) => `${w.id} 核对通过：${r.arn}`), busy: rows.busy === `v-${w.id}`, title: '用这个工作区的凭证调一次 sts:GetCallerIdentity，看登进的账号对不对' }, '核对'),
          h(Button, { onClick: () => edit(w), title: '把这个工作区填进下面的表单' }, '修改'),
          h(Button, { kind: 'danger', icon: Trash2, title: '从控制台移除（不动 AWS）', onClick: () => window.confirm(`移除工作区 ${w.id}？（只从控制台移除，不动 AWS）`) && rows.run('del', led(`${w.id} 没有移除`, w.id, () => api.deleteWorkspace(w.id)), () => {
            const next = workspaces.find((x) => x.id !== w.id)
            return w.id !== wid ? `已移除 ${w.id}` : next ? `已移除 ${w.id}，现在用的是工作区 ${next.id}` : `已移除 ${w.id}：现在没有工作区了`
          }).then(reload) }))] })),
      empty: '还没有工作区：在下面「添加工作区」填一个。' }),
      workspaces.length ? h('div', { className: 'cs-mut' }, '点一行（或「修改」）把它填进下面的表单。') : null,
      h(ErrorLine, { error: rows.error })),
    h(Card, { title: editing ? `修改工作区 ${stored.id}` : '添加工作区', extra: h('span', { className: 'cs-row' },
      editing ? h(Button, { onClick: clear }, '清空（改成添加）') : null,
      h(Button, { kind: 'pri', onClick: save, busy: busy === 'save', disabled: clash, title: clash ? `先把 ${stored.id} 的现有设置填进来` : undefined }, '保存')) },
      h('div', { className: 'cs-grid' },
        h(Field, { label: 'ID', hint: editing ? '改 ID 就是另存成一个新工作区（原来的留着）' : '字母开头，只用小写字母、数字和连字符，2–40 个字符；要修改已有的工作区，点上面它那一行' },
          h(Input, { value: form.id, onChange: set('id'), placeholder: '例：prod-us-west-2', mono: true })),
        h(Field, { label: '名称（可选，默认用 ID）' }, h(Input, { value: form.name, onChange: set('name') })),
        h(Field, { label: '账号（12 位数字）' }, h(Input, { value: form.accountId, onChange: set('accountId'), mono: true })),
        h(Field, { label: '区域' }, h(Input, { value: form.region, onChange: set('region'), mono: true })),
        h(Field, { label: '本机 AWS profile', hint: '这台机器 ~/.aws 里的 profile 名；用跨账号角色时，它是切换角色用的身份' }, h(Input, { value: form.profile, onChange: set('profile'), mono: true })),
        h(Field, { label: '跨账号角色 ARN（可选）', hint: '对方账号用 app/console/spoke-role.yaml 部署的角色' }, h(Input, { value: form.roleArn, onChange: set('roleArn'), mono: true })),
        h(Field, { label: 'External ID（可选）', hint: stored && stored.externalIdSet ? '已经设过（不再显示）：角色 ARN 不变时留空，就保留原来的' : null }, h(Input, { value: form.externalId, onChange: set('externalId'), mono: true })),
        h(Field, { label: '权限边界 ARN（spoke 工作区填）', hint: 'spoke-role.yaml 的输出 PermissionsBoundaryArn：控制台在这个账号建的每个角色都带上它（spoke 角色只让建带它的角色）；只用本机 profile 的工作区可以不填' },
          h('span', { className: 'cs-row' }, h(Input, { value: form.permissionsBoundaryArn, onChange: set('permissionsBoundaryArn'), mono: true, placeholder: boundaryFor(form.roleArn) }),
            boundaryFor(form.roleArn) && !form.permissionsBoundaryArn ? h(Button, { onClick: () => set('permissionsBoundaryArn')(boundaryFor(form.roleArn)) }, '按角色填入') : null))),
      clash ? h(Note, { tone: 'warn' }, h('span', null, `工作区 ${stored.id} 已经有了：保存会用这张表的值整个替换它（名称、角色、权限边界都会照表单清空或改掉）。要修改它，先把现有设置填进来。 `),
        h(Button, { onClick: () => edit(stored) }, `把 ${stored.id} 的现有设置填进来`)) : null,
      form.roleArn.trim() && !form.permissionsBoundaryArn.trim() ? h(Note, { tone: 'warn' }, '跨账号角色没有填权限边界：spoke-role.yaml 部署的角色只让控制台建带这个边界的角色，不填的话建 Harness、部署、知识库、实验都会被拒绝。') : null,
      h(ErrorLine, { error })),
    h(SpokeCard, { onUse: set('externalId') }))
}

const FIRST_ADMIN = 'adlc-console:first-admin'
function UsersPage() {
  const { workspaces, me, reload } = useCtx()
  const [users, setUsers] = useState(null)
  const [loadError, setLoadError] = useState(null)
  const [form, setForm] = useState({ username: '', password: '', role: 'member', workspaces: '' })
  const { busy, error, run } = useAction()
  const load = () => api.users().then((r) => { setUsers(listOf(r.users)); setLoadError(null) }).catch(setLoadError)
  useEffect(() => { load() }, [])
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const edit = (u) => setForm({ username: u.username, password: '', role: u.role, workspaces: listOf(u.workspaces).join(', ') })
  const listed = form.workspaces.split(/[\s,，]+/).filter(Boolean)
  const unknown = listed.filter((id) => !workspaces.some((w) => w.id === id))
  const firstAdmin = me && me.open && form.role === 'admin'
  const save = () => run('save', () => api.putUser({ username: form.username, role: form.role, workspaces: listed,
    ...(form.password ? { password: form.password } : {}) }), firstAdmin ? `已添加第一个管理员 ${form.username}：开放模式结束，用它登录` : '已保存用户')
    .then((r) => {  // the console now asks who is there: the sign-in page says why, with the name filled in
      if (r && firstAdmin) { window.sessionStorage.setItem(FIRST_ADMIN, form.username); reload() } else load()
    })
  return h(Fragment, null,
    h('h1', null, '用户与权限'),
    IN_KIROCREW ? h(Note, null, '在 KiroCrew 里，KiroCrew 的登录就是控制台的认证：这里的用户只给在自己服务器上运行的控制台（app/console/server.py）登录用。') : null,
    me && me.open ? h(Note, { tone: 'warn' }, '开放模式：还没有用户，控制台只在本机可用。添加第一个管理员后，所有访问都需要登录。') : null,
    h(Card, { title: '用户（点一行来修改）' }, loaded(users, loadError, (list) => h(Table, { head: ['用户', '角色', '可用工作区', ''], rows: list.map((u) => ({ key: u.username, onClick: () => edit(u), cells: [h('b', null, u.username), u.role === 'admin' ? '管理员' : '成员',
      u.role === 'admin' ? '全部' : listOf(u.workspaces).join('，') || '—', h(Button, { kind: 'danger', icon: Trash2, title: `删除用户 ${u.username}`,
        onClick: (e) => { e.stopPropagation(); if (window.confirm(`删除用户 ${u.username}？`)) run('del', () => api.deleteUser(u.username), '已删除').then(load) } })] })),
    empty: IN_KIROCREW ? '没有用户（在 KiroCrew 里用不到：KiroCrew 的登录就是认证）。' : '没有用户（开放模式）。' }))),
    h(Card, { title: '添加或修改', extra: h(Button, { kind: 'pri', onClick: save, busy: busy === 'save' }, '保存') },
      h('div', { className: 'cs-grid' },
        h(Field, { label: '用户名' }, h(Input, { value: form.username, onChange: set('username'), mono: true })),
        h(Field, { label: '密码（新用户至少 10 位；修改时留空就不变）' }, h(Input, { value: form.password, onChange: set('password'), type: 'password' })),
        h(Field, { label: '角色' }, h(Select, { value: form.role, onChange: set('role'), options: [['member', '成员'], ['admin', '管理员']] })),
        h(Field, { label: '可用工作区（成员填；管理员能用全部）', hint: workspaces.length ? `可填：${workspaces.map((w) => w.id).join('，')}（多个用逗号分隔）` : '还没有工作区' },
          h(Input, { value: form.workspaces, onChange: set('workspaces'), mono: true }))),
      unknown.length ? h(Note, { tone: 'warn' }, `没有这些工作区：${unknown.join('，')}（保存后它们不起作用）。`) : null,
      form.role === 'member' && form.username && !listed.length ? h(Note, { tone: 'warn' }, '这个成员没有可用的工作区：登录后什么都看不到。') : null,
      h(ErrorLine, { error })))
}

function KeysPage() {
  const { workspaces, wid, toast } = useCtx()
  const [keys, setKeys] = useState(null)
  const [loadError, setLoadError] = useState(null)
  const [form, setForm] = useState({ workspace: wid || (workspaces[0] || {}).id || '', agent: '', label: '' })
  const [issued, setIssued] = useState(null)
  useEffect(() => { if (!workspaces.some((w) => w.id === form.workspace) && workspaces.length) setForm((f) => ({ ...f, workspace: wid || workspaces[0].id })) }, [workspaces, wid])
  const { busy, error, run } = useAction()
  const load = () => api.keys().then((r) => { setKeys(listOf(r.keys)); setLoadError(null) }).catch(setLoadError)
  useEffect(() => { load() }, [])
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const issue = () => run('issue', () => api.issueKey(form)).then((r) => { if (r) { setIssued(r); load() } })
  // Inside KiroCrew, /v1 is on the App backend's own local port (the gateway admits only a signed-in browser and cuts
  // every request at 30 s); /host says where it listens, or why it does not.
  const [publicApi, setPublicApi] = useState(null)
  useEffect(() => { if (IN_KIROCREW) call('GET', '/host').then((info) => setPublicApi(info.publicApi || { listening: false })).catch(() => {}) }, [])
  const v1 = IN_KIROCREW ? ((publicApi && publicApi.url) || 'http://127.0.0.1:8772/v1') : `${window.location.origin}/v1`
  return h(Fragment, null,
    h('h1', null, 'API 密钥'),
    h(Card, { title: '对外接口 /v1' },
      IN_KIROCREW && publicApi ? (publicApi.listening
        ? h(Note, { tone: 'ok' }, `这个 App 的后端在本机 ${publicApi.url} 提供 /v1：不经过 KiroCrew 的网关，没有 30 秒的限制，流式返回照常；只答本机的程序，每次调用都要这里签发的密钥。`)
        : h(Note, { tone: 'warn' }, `/v1 的本机端口没有打开（${publicApi.error || '原因未知'}）：设置 ADLC_PUBLIC_API_PORT 换一个端口后，在 KiroCrew 里更新这个 App。`)) : null,
      h('pre', { className: 'cs-pre' },
        `curl -s ${v1}/chat -H "X-Api-Key: $KEY" -H "Content-Type: application/json" \\\n  -d '{"agent": "<Agent 名称>", "message": "你好"}'\n# 返回 {"text", "sessionId", "tools", "latencyMs", "usage"}；带上 sessionId 继续对话，加 "stream": true 流式返回`)),
    issued ? h(Note, { tone: 'ok' }, h('div', null, '密钥只显示这一次：现在复制，之后看不到了。'), h('span', { className: 'cs-row' }, h('code', { className: 'cs-mono' }, issued.key),
      h(Button, { onClick: () => navigator.clipboard.writeText(issued.key).then(() => toast('ok', '已复制密钥'), () => toast('error', '浏览器没让复制：手动选中复制')) }, '复制'))) : null,
    h(Card, { title: '密钥' }, loaded(keys, loadError, (list) => h(Table, { head: ['前缀', '工作区', 'Agent', '用途', '最近使用', '状态', ''], rows: list.map((k) => ({ key: k.id, cells: [h('code', null, k.prefix + '…'),
      h('span', { title: k.workspace }, (workspaces.find((w) => w.id === k.workspace) || {}).name || k.workspace),
      k.agent || '全部', k.label, k.lastUsedAt ? h(At, { iso: k.lastUsedAt }) : '还没用过', k.revoked ? h(Chip, { tone: 'bad' }, '已吊销') : h(Chip, { tone: 'ok' }, '有效'),
      k.revoked ? null : h(Button, { kind: 'danger', onClick: () => window.confirm(`吊销密钥 ${k.prefix}…（${k.label || '没写用途'}）？用它的程序会立刻 401，不能恢复。`)
        && run('revoke', () => api.revokeKey(k.id), '已吊销').then((r) => { if (r) { if (issued && issued.id === k.id) setIssued(null); load() } }) }, '吊销')] })), empty: '还没有密钥。' }))),
    h(Card, { title: '签发', extra: h(Button, { kind: 'pri', onClick: issue, busy: busy === 'issue', disabled: !workspaces.length }, '签发') },
      workspaces.length ? null : h(NeedWorkspace, { what: '签发密钥' }),
      h('div', { className: 'cs-grid' },
        h(Field, { label: '工作区' }, h(Select, { value: form.workspace, onChange: set('workspace'), options: workspaces.length ? workspaces.map((w) => [w.id, w.name]) : [['', '没有工作区']] })),
        h(Field, { label: '只允许调用的 Agent 名称（可选，留空 = 这个工作区的全部）', hint: '要和 Agent 的名称一字不差（/v1 按名称找它）；写错了，这个密钥会调不通' }, h(Input, { value: form.agent, onChange: set('agent'), mono: true })),
        h(Field, { label: '用途' }, h(Input, { value: form.label, onChange: set('label') }))),
      h(ErrorLine, { error })))
}

// Inside KiroCrew there is no console sign-in: a 401 is KiroCrew's own session that ran out.
function KiroCrewSignIn({ onDone }) {
  return h('div', { className: 'cs-login' }, h(Card, { title: 'KiroCrew 的登录过期了' },
    h(Note, null, '这个控制台用 KiroCrew 的登录：刷新 KiroCrew 的页面（或重新打开 KiroCrew），然后再试。'),
    h(Button, { kind: 'pri', onClick: onDone }, '再试一次')))
}

function LoginPage({ onDone }) {
  const [first] = useState(() => window.sessionStorage.getItem(FIRST_ADMIN))
  const [username, setUsername] = useState(first || '')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const login = () => api.login(username, password).then((r) => { window.sessionStorage.removeItem(FIRST_ADMIN); onDone(r) }).catch(setError)
  return h('div', { className: 'cs-login' }, h(Card, { title: '登录 ADLC 控制台' },
    first ? h(Note, { tone: 'ok' }, `开放模式已结束：用刚添加的管理员 ${first} 登录。`) : null,
    h(Field, { label: '用户名' }, h(Input, { value: username, onChange: setUsername, onEnter: login })),
    h(Field, { label: '密码' }, h(Input, { value: password, onChange: setPassword, type: 'password', onEnter: login })),
    h(Button, { kind: 'pri', onClick: login }, '登录'), h(ErrorLine, { error })))
}

// ---------------------------------------------------------------------------
// Shell
// ---------------------------------------------------------------------------

const NAV = [
  ['构建', [['overview', '概览'], ['agents', 'Agent'], ['create', '新建 Agent']]],
  ['运行', [['chat', '对话'], ['keys', 'API 密钥']]],
  ['工作坊', [['workshops', '工作坊']]],
  ['设置', [['workspaces', '工作区'], ['users', '用户与权限'], ['host', '运行环境']]],
]
const PAGES = { overview: OverviewPage, agents: AgentsPage, agent: AgentDetailPage, create: CreateAgentPage, chat: ChatPage, workshops: WorkshopsPage,
  workspaces: WorkspacesPage, users: UsersPage, keys: KeysPage, host: HostPage }

// The capability pages (pages/*.mjs): each one {id, label, group, Page}, placed in its group of the navigation.
for (const m of MODULES) {
  PAGES[m.id] = m.Page
  const section = NAV.find(([g]) => g === m.group) || (NAV.splice(NAV.length - 1, 0, [m.group, []]), NAV[NAV.length - 2])
  if (!section[1].some(([p]) => p === m.id)) section[1].push([m.id, m.label])
}

export default function Console() {
  const state = useConsoleState()
  const [page, setPage] = useState(() => (window.location.hash.slice(1) || 'overview'))
  const [selected, setSelected] = useState(null)
  const go = (p) => {
    if (p === 'workshops' && IN_KIROCREW) { openWorkshop(); return }  // KiroCrew's own Workshop Customizer view, not a copy in here
    window.location.hash = p; setPage(p)
  }
  useEffect(() => { const onHash = () => setPage(window.location.hash.slice(1) || 'overview'); window.addEventListener('hashchange', onHash); return () => window.removeEventListener('hashchange', onHash) }, [])
  if (state.me === undefined) {
    return h('div', { className: 'cs-root' }, h('style', null, CSS), state.failed ? h('div', { className: 'cs-login' }, h(Card, { title: '控制台连不上它的后端' },
      h(ErrorLine, { error: state.failed }), h(Button, { kind: 'pri', onClick: state.reload }, '再试一次'))) : h(Empty, null, '加载中…'))
  }
  if (state.me === null) return h('div', { className: 'cs-root' }, h('style', null, CSS), h(IN_KIROCREW ? KiroCrewSignIn : LoginPage, { onDone: state.reload }))
  const Page = PAGES[page] || OverviewPage
  return h(Ctx.Provider, { value: state }, h('div', { className: 'cs-root' }, h('style', null, CSS),
    h('aside', { className: 'cs-nav' },
      h('div', { className: 'cs-brand' }, 'ADLC 控制台'),
      h(Select, { value: state.wid, onChange: state.setWid, options: state.workspaces.length ? state.workspaces.map((w) => [w.id, `${w.name} · ${w.region}`]) : [['', '没有工作区']] }),
      NAV.map(([group, items]) => h('div', { key: group, className: 'cs-group' }, h('div', { className: 'cs-group-t' }, group),
        items.map(([id, label]) => h('a', { key: id, className: cx('cs-link', page === id && 'on'), onClick: () => go(id) }, label)))),
      h('div', { className: 'cs-me' }, IN_KIROCREW ? 'KiroCrew 登录 · 管理员' : state.me.open ? '本机（开放模式）' : `${state.me.username} · ${ROLE_LABEL[state.me.role] || state.me.role}`,
        state.me.open || IN_KIROCREW ? null : h('a', { className: 'cs-link', onClick: () => api.logout().then(state.reload) }, '退出'))),
    h('main', { className: cx('cs-main', page === 'workshops' && !IN_KIROCREW && 'cs-wide') }, h(CredentialBanner, { go, page }), h(HostBanner, { go }), h(Page, { go, selected, setSelected })),
    h('div', { className: 'cs-toasts' }, state.toasts.map((t) => h('div', { key: t.id, className: cx('cs-toast', t.kind === 'error' && 'cs-bad') }, t.message)))))
}

const CSS = `
.cs-root{display:flex;min-height:100vh;font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#1f2933;background:#f5f7fa}
.cs-nav{width:220px;flex:0 0 220px;background:#102a43;color:#d9e2ec;padding:14px 12px;display:flex;flex-direction:column;gap:10px}
.cs-brand{font-weight:700;font-size:16px;color:#fff;margin-bottom:4px}
.cs-group-t{font-size:11px;text-transform:uppercase;color:#829ab1;margin:8px 0 2px}
.cs-link{display:block;padding:5px 8px;border-radius:6px;cursor:pointer;color:#d9e2ec}
.cs-link:hover{background:#243b53}.cs-link.on{background:#334e68;color:#fff}
.cs-me{margin-top:auto;font-size:12px;color:#9fb3c8}
.cs-main{flex:1;padding:20px 28px;max-width:1100px}.cs-main.cs-wide{max-width:none;padding:0}
.cs-main h1{font-size:20px;margin:4px 0 14px}
.cs-card{background:#fff;border:1px solid #d9e2ec;border-radius:10px;padding:14px 16px;margin-bottom:14px}
.cs-card-h{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:8px}
.cs-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.cs-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.cs-btn{border:1px solid #bcccdc;background:#fff;border-radius:6px;padding:5px 11px;cursor:pointer;display:inline-flex;gap:5px;align-items:center;font:inherit}
.cs-btn:disabled{opacity:.55;cursor:default}.cs-pri{background:#2680c2;border-color:#2680c2;color:#fff}.cs-danger{color:#ba2525;border-color:#f29b9b}
.cs-field{display:flex;flex-direction:column;gap:4px;margin-bottom:10px}.cs-field>span{font-size:12px;color:#486581}.cs-field small{color:#829ab1}
.cs-input{border:1px solid #bcccdc;border-radius:6px;padding:6px 8px;font:inherit;width:100%;box-sizing:border-box;background:#fff}
.cs-mono{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}
.cs-chip{display:inline-block;padding:1px 8px;border-radius:10px;background:#e4e7eb;font-size:12px;white-space:nowrap}
.cs-ok{background:#e3f9e5;color:#207227}.cs-bad{background:#ffe3e3;color:#ab091e}.cs-warn{background:#fffbea;color:#8d2b0b}.cs-info{background:#e6f6ff;color:#0b69a3}
.cs-note{font-size:13px;color:#486581;margin:8px 0}.cs-tx-ok{color:#207227}.cs-tx-warn{color:#8d2b0b}
.cs-err{color:#ab091e;font-size:13px;margin-top:6px;white-space:pre-wrap}
.cs-empty{color:#829ab1;padding:14px 0}
.cs-tablew{overflow-x:auto}.cs-table{border-collapse:collapse;width:100%}.cs-table th,.cs-table td{text-align:left;padding:7px 8px;border-bottom:1px solid #e4e7eb;vertical-align:top}
.cs-table th{font-size:12px;color:#627d98;font-weight:600}.cs-click{cursor:pointer}.cs-click:hover{background:#f0f4f8}
.cs-big{font-size:28px;font-weight:700}.cs-mut{color:#829ab1;font-size:12.5px}
.cs-chat{max-height:56vh;overflow-y:auto;padding:4px 0 10px}.cs-turn{margin:8px 0;display:flex;flex-direction:column}.cs-user{align-items:flex-end}
.cs-bubble{white-space:pre-wrap;padding:8px 12px;border-radius:10px;max-width:80%}.cs-user .cs-bubble{background:#2680c2;color:#fff}.cs-agent .cs-bubble{background:#f0f4f8}
.cs-pre{background:#102a43;color:#d9e2ec;padding:10px 12px;border-radius:8px;overflow-x:auto;font-size:12.5px}
.cs-toasts{position:fixed;right:16px;bottom:16px;display:flex;flex-direction:column;gap:6px;z-index:10}
.cs-toast{background:#243b53;color:#fff;padding:8px 12px;border-radius:8px;max-width:420px;font-size:13px}
.cs-login{margin:12vh auto;width:360px}
.cs-spin{width:11px;height:11px;border:2px solid #bcccdc;border-top-color:#2680c2;border-radius:50%;display:inline-block;animation:cs-spin .8s linear infinite}
@keyframes cs-spin{to{transform:rotate(360deg)}}
.cs-app{min-height:100vh}
.cs-banner{background:#fffbea;border:1px solid #f0d58c;color:#8d2b0b;border-radius:8px;padding:8px 12px;margin-bottom:14px;font-size:13px}.cs-banner a{text-decoration:underline}
.cs-banner-bad{background:#fff1f1;border-color:#f5b5b5;color:#8a1020}
.cs-tabs{display:flex;gap:4px;margin-bottom:12px;border-bottom:1px solid #d9e2ec}.cs-tab{padding:6px 12px;cursor:pointer;color:#486581;border-bottom:2px solid transparent}.cs-tab.on{color:#102a43;border-bottom-color:#2680c2;font-weight:600}
`
