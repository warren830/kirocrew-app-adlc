// Registry: the AgentCore Registry. Registries; records of three kinds (an agent's A2A card filled from a Harness or
// Runtime, MCP tools from a Gateway or a server URL, a skill's SKILL.md in S3); submit → approve / reject / deprecate;
// semantic search over what is approved; and an agent's latest console verification attached as evidence.
import React from 'react'
import { call, useCtx, listOf, Card, Button, Field, Input, TextArea, Select, Chip, Note, Empty, ErrorLine, Table, useAction, Tabs } from '../ui.mjs'

const { useState, useEffect, createElement: h, Fragment } = React
const reg = (wid, rid, path = '') => `/workspaces/${wid}/registries${rid ? `/${rid}` : ''}${path}`
const time = (s) => (s || '').slice(0, 19).replace('T', ' ')

const STATUS = {
  DRAFT: ['草稿', ''], PENDING_APPROVAL: ['待审批', 'info'], APPROVED: ['已批准', 'ok'], REJECTED: ['已拒绝', 'bad'], DEPRECATED: ['已下线', 'warn'],
  CREATING: ['创建中', 'info'], UPDATING: ['更新中', 'info'], CREATE_FAILED: ['创建失败', 'bad'], UPDATE_FAILED: ['更新失败', 'bad'],
}
const REGISTRY_TONE = { READY: 'ok', CREATING: 'info', UPDATING: 'info', DELETING: 'warn', CREATE_FAILED: 'bad', UPDATE_FAILED: 'bad', DELETE_FAILED: 'bad' }
const KIND = { agent: 'Agent', mcp: 'MCP 工具', skill: '技能', custom: '自定义', gateway: 'Gateway' }
const statusLabel = (s) => (STATUS[s] || [s])[0]
function StatusChip({ status }) {
  const [label, tone] = STATUS[status] || [status, '']
  return h(Chip, { tone, title: status }, label)
}

// -- registries ------------------------------------------------------------------------------------------------------

function NewRegistry({ onCreated }) {
  const { wid } = useCtx()
  const [form, setForm] = useState({ name: '', description: '', approval: 'manual' })
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const create = () => run('create', () => call('POST', reg(wid), { name: form.name, description: form.description, autoApproval: form.approval === 'auto' }),
    (r) => `已创建 Registry ${r.name}（约一分钟后就绪）`).then((r) => r && onCreated(r))
  return h(Card, { title: '新建 Registry', extra: h(Button, { kind: 'pri', onClick: create, busy: busy === 'create', disabled: !form.name }, '创建') },
    h('div', { className: 'cs-grid' },
      h(Field, { label: '名称', hint: '字母或数字开头；字母、数字、_ - . /' }, h(Input, { value: form.name, onChange: set('name'), mono: true, placeholder: 'adlc-catalog' })),
      h(Field, { label: '说明' }, h(Input, { value: form.description, onChange: set('description') })),
      h(Field, { label: '审批' }, h(Select, { value: form.approval, onChange: set('approval'), options: [['manual', '人工审批：提交后由管理员批准'], ['auto', '自动批准：提交即上架']] }))),
    h(Note, null, '审批方式创建后不在这里改。Registry 带 adlc:console=1 标签，只有这样的 Registry 能在这里删除。'),
    h(ErrorLine, { error }))
}

// -- records -----------------------------------------------------------------------------------------------------------

function Records({ rid, status, version, open, onOpen, empty }) {
  const { wid } = useCtx()
  const [list, setList] = useState(null)
  const [listError, setListError] = useState(null)
  const [kind, setKind] = useState('')
  const [text, setText] = useState('')
  useEffect(() => {
    setList(null); setListError(null)
    call('GET', reg(wid, rid, `/records${status ? `?status=${status}` : ''}`)).then((r) => setList(listOf(r.records))).catch(setListError)
  }, [wid, rid, status, version])
  const needle = text.trim().toLowerCase()
  const rows = listOf(list).filter((r) => (!kind || r.type === kind)
    && (!needle || `${r.name} ${r.displayName || ''} ${r.description} ${r.id}`.toLowerCase().includes(needle)))
  return h(Fragment, null,
    h('div', { className: 'cs-row' },
      h(Select, { value: kind, onChange: setKind, options: [['', '全部类型'], ['agent', 'Agent'], ['mcp', 'MCP 工具'], ['skill', '技能']] }),
      h(Input, { value: text, onChange: setText, placeholder: '按名称、说明、ID 过滤' })),
    h(ErrorLine, { error: listError }),
    list === null ? h(Empty, null, '加载中…') : h(Table, {
      head: ['名称', '类型', '版本', '状态', '更新'],
      rows: rows.map((r) => ({ key: r.id, onClick: () => onOpen(r.id), cells: [
        h('span', null, h('b', null, r.displayName || r.name), r.id === open ? ' ◂' : null, r.description ? h('div', { className: 'cs-mut' }, r.description.slice(0, 90)) : null),
        KIND[r.type] || r.type, h('span', { className: 'cs-mono' }, r.version || '—'), h(StatusChip, { status: r.status }), time(r.updatedAt)] })),
      empty: empty || '这个 Registry 还没有记录。到「登记」添加 Agent、MCP 工具或技能。',
    }))
}

function Evidence({ evidence, latest }) {
  const newer = latest && (!evidence || latest.jobId !== evidence.jobId)
  const line = (e) => `验证 ${e.jobId}：${e.holding}/${e.contracts} 条契约每轮都通过（${e.repeat} 轮）· ${e.robust ? '稳定' : '不稳定'}`
  return h(Fragment, null,
    evidence ? h(Note, { tone: evidence.robust ? 'ok' : 'warn' },
      h('div', null, h('b', null, '审批证据　'), line(evidence)),
      listOf(evidence.notHolding).length ? h('div', null, `没有每轮都通过的契约：${evidence.notHolding.join('，')}`) : null,
      h('div', { className: 'cs-mut' }, `契约集 ${evidence.contractSet || '—'} · 模型 ${evidence.model || '—'} · ${evidence.panel ? '含 AgentCore 评估器' : '只有 L1'} · 验证于 ${time(evidence.verifiedAt)}`))
      : h(Note, { tone: 'warn' }, '没有附验证证据：审批人看不到这个 Agent 做到了什么。'),
    newer ? h(Note, null, `控制台里有更新的验证：${line(latest)}（「附验证证据并提交」会附上它）`) : null)
}

function CardView({ rec }) {
  const [raw, setRaw] = useState(false)
  const card = rec.card || {}
  const source = rec.source || {}
  return h(Fragment, null,
    h(Table, { head: ['名片', ''], rows: [
      ['名称', card.name], ['说明', card.description], ['地址', h('span', { className: 'cs-mono' }, card.url)],
      ['对应的 Agent', source.id ? `${source.kind === 'harness' ? 'Harness' : 'Runtime'} ${source.name || ''}（${source.id}）` : '名片里没有写'],
    ].map((cells, i) => ({ key: i, cells })) }),
    h(Table, { head: ['技能 ID', '技能', '说明', '标签'], rows: listOf(card.skills).map((s, i) => ({ key: `${s.id}-${i}`,
      cells: [h('span', { className: 'cs-mono' }, s.id), s.name, s.description, listOf(s.tags).join('，')] })), empty: '名片没有列技能。' }),
    h(Button, { onClick: () => setRaw(!raw) }, raw ? '收起名片 JSON' : '名片 JSON'),
    raw ? h('pre', { className: 'cs-pre' }, JSON.stringify(card, null, 1)) : null)
}

function EditRecord({ rid, rec, onSaved }) {
  const { wid } = useCtx()
  const [form, setForm] = useState({ description: rec.description || '', version: rec.version || '', card: rec.card ? JSON.stringify(rec.card, null, 1) : '',
    url: rec.url || '', uri: (rec.definition && rec.definition.path) || '', resync: false })
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const save = () => run('save', async () => {
    const body = {}
    if (form.description !== (rec.description || '')) body.description = form.description
    if (form.version && form.version !== rec.version) body.version = form.version
    if (rec.type === 'agent' && form.card !== JSON.stringify(rec.card, null, 1)) body.card = JSON.parse(form.card)
    if (rec.type === 'mcp' && form.url && form.url !== rec.url) body.url = form.url
    if (rec.type === 'skill' && form.resync) body.uri = form.uri
    return call('PUT', reg(wid, rid, `/records/${rec.id}`), body)
  }, '已保存：记录回到草稿，需要重新提交审批').then((r) => r && onSaved())
  return h(Card, { title: '修改记录', extra: h(Button, { kind: 'pri', onClick: save, busy: busy === 'save' }, '保存') },
    h('div', { className: 'cs-grid' },
      h(Field, { label: '说明' }, h(Input, { value: form.description, onChange: set('description') })),
      h(Field, { label: '版本', hint: '同名不同版本是另一条记录；这里改的是这条记录的版本号' }, h(Input, { value: form.version, onChange: set('version'), mono: true }))),
    rec.type === 'agent' ? h(Field, { label: 'A2A 名片（JSON）' }, h(TextArea, { value: form.card, onChange: set('card'), rows: 14, mono: true })) : null,
    rec.type === 'mcp' ? h(Field, { label: 'MCP 地址', hint: 'Gateway 的工具要换，就删掉这条记录、从 Gateway 重新登记' }, h(Input, { value: form.url, onChange: set('url'), mono: true })) : null,
    rec.type === 'skill' ? h(Fragment, null, h(Field, { label: 'SKILL.md 所在的 S3 位置' }, h(Input, { value: form.uri, onChange: set('uri'), mono: true })),
      h(Select, { value: form.resync ? 'y' : 'n', onChange: (v) => set('resync')(v === 'y'), options: [['n', '不重新读取 SKILL.md'], ['y', '从 S3 重新读取 SKILL.md']] })) : null,
    h(Note, null, '任何修改都会让记录回到草稿；已批准的版本在新版本获批之前仍然可以被发现。'),
    h(ErrorLine, { error }))
}

function RecordDetail({ rid, id, onChanged, onClosed }) {
  const { wid, me } = useCtx()
  const admin = Boolean(me && me.role === 'admin')
  const [rec, setRec] = useState(null)
  const [loadError, setLoadError] = useState(null)
  const [reason, setReason] = useState('')
  const [editing, setEditing] = useState(false)
  const { busy, error, run } = useAction()
  const load = () => call('GET', reg(wid, rid, `/records/${id}`)).then((r) => { setRec(r); setLoadError(null) }).catch(setLoadError)
  useEffect(() => { setRec(null); setEditing(false); setReason(''); load() }, [wid, rid, id])
  if (loadError) return h(Card, { title: '记录' }, h(ErrorLine, { error: loadError }))
  if (!rec) return h(Card, { title: '记录' }, h(Empty, null, '加载中…'))
  const act = (name, fn, done) => run(name, fn, done).then((r) => { if (r) { load(); onChanged() } return r })
  const path = (p) => reg(wid, rid, `/records/${id}${p}`)
  // another team's record: submitting (into a registry that may approve on submit) and deciding need a confirmation
  const theirs = (verb) => !rec.console && !window.confirm(`${rec.name} 不是控制台建的记录（别的团队的）。确定要${verb}它？`)
  const submit = () => { if (theirs('提交')) return; act('submit', () => call('POST', path('/submit'), rec.console ? {} : { acknowledged: true }), (r) => `已提交：${statusLabel(r.status)}`) }
  const publish = () => act('publish', () => call('POST', path('/publish'), {}), (r) => `已附上验证 ${r.evidence.jobId} 并提交：${statusLabel(r.status)}`)
  const decide = (status) => {
    if (status === 'DEPRECATED' && !window.confirm(`下线 ${rec.name}？下线不可恢复，记录不再能被发现。`)) return
    if (theirs(statusLabel(status))) return
    act(status, () => call('POST', path('/status'), { status, reason, ...(rec.console ? {} : { acknowledged: true }) }), (r) => `${statusLabel(r.status)}：${rec.name}`)
      .then((r) => r && setReason(''))
  }
  const remove = () => window.confirm(`删除记录 ${rec.name}（${rec.version || '无版本'}）？此操作不可恢复。`)
    && run('delete', () => call('DELETE', path('')), '已删除').then((r) => { if (r) { onChanged(); onClosed() } })
  const live = rec.status !== 'DEPRECATED'
  const canPublish = rec.type === 'agent' && rec.console && live && Boolean(rec.source && rec.source.id)
  return h(Fragment, null,
    h(Card, { title: `${rec.displayName || rec.name}`, extra: h('span', { className: 'cs-row' }, h(Chip, null, KIND[rec.type] || rec.type), h(StatusChip, { status: rec.status }),
      h(Button, { onClick: onClosed }, '关闭')) },
      rec.type === 'agent' ? h(Evidence, { evidence: rec.evidence, latest: rec.latestVerification }) : null,
      h(Table, { head: ['项', '值'], rows: [
        ['名称', h('span', { className: 'cs-mono' }, rec.name)], ['版本', rec.version || '—'], ['记录 ID', h('span', { className: 'cs-mono' }, rec.id)],
        ['说明', rec.description || '—'], ['状态说明', rec.statusReason || '—'], ['描述格式', rec.schemaVersion || '—'],
        ['创建', `${time(rec.createdAt)}${rec.createdBy ? `（账号 ${rec.createdBy}）` : ''}${rec.autoDetected ? ' · 自动发现' : ''}`], ['更新', time(rec.updatedAt)],
        ['由控制台创建', rec.console ? '是' : '否（提交和审批要确认，不能在这里修改或删除）'],
      ].map((cells, i) => ({ key: i, cells })) }),
      rec.type === 'agent' ? h(CardView, { rec }) : null,
      rec.type === 'mcp' ? h(Fragment, null,
        h(Table, { head: ['MCP 服务', ''], rows: [['名称', h('span', { className: 'cs-mono' }, (rec.server || {}).name)], ['地址', h('span', { className: 'cs-mono' }, rec.url || '—')],
          ['传输', listOf((rec.server || {}).remotes).map((x) => x.type).join('，') || '—']].map((cells, i) => ({ key: i, cells })) }),
        h(Table, { head: ['工具', '说明'], rows: listOf(rec.tools).map((t) => ({ key: t.name, cells: [h('span', { className: 'cs-mono' }, t.name), t.description] })),
          empty: '没有列出工具（按地址登记的 MCP 服务，工具由服务自己提供）。' })) : null,
      rec.type === 'skill' ? h(Fragment, null,
        h(Table, { head: ['技能', ''], rows: [['名称', (rec.definition || {}).name], ['S3 位置', h('span', { className: 'cs-mono' }, (rec.definition || {}).path)],
          ['版本', (rec.definition || {}).version], ['说明', (rec.definition || {}).description]].map((cells, i) => ({ key: i, cells })) }),
        rec.skillMd ? h('pre', { className: 'cs-pre' }, rec.skillMd.slice(0, 4000)) : null) : null,
      rec.type !== 'agent' && rec.type !== 'mcp' && rec.type !== 'skill' && rec.data !== undefined ? h('pre', { className: 'cs-pre' }, JSON.stringify(rec.data, null, 1)) : null,
      h('div', { className: 'cs-row' },
        rec.status === 'DRAFT' ? h(Button, { kind: canPublish ? '' : 'pri', onClick: submit, busy: busy === 'submit' }, '提交审批') : null,
        canPublish ? h(Button, { kind: 'pri', onClick: publish, busy: busy === 'publish', disabled: !rec.latestVerification,
          title: rec.latestVerification ? `附上验证 ${rec.latestVerification.jobId}` : '先在「评估 → 验证」验证这个 Agent' }, '附验证证据并提交') : null,
        rec.console && live ? h(Button, { onClick: () => setEditing(!editing) }, editing ? '取消修改' : '修改') : null,
        rec.console ? h(Button, { kind: 'danger', onClick: remove, busy: busy === 'delete' }, '删除') : null),
      admin && live && rec.status !== 'DRAFT' ? h(Card, { title: '审批（管理员）' },
        h('div', { className: 'cs-row' },
          h(Input, { value: reason, onChange: setReason, placeholder: '理由（写进记录的状态说明）' }),
          rec.status === 'PENDING_APPROVAL' || rec.status === 'REJECTED' ? h(Button, { kind: 'pri', onClick: () => decide('APPROVED'), busy: busy === 'APPROVED' }, '批准') : null,
          rec.status === 'PENDING_APPROVAL' ? h(Button, { kind: 'danger', onClick: () => decide('REJECTED'), busy: busy === 'REJECTED' }, '拒绝') : null,
          rec.status === 'APPROVED' ? h(Button, { kind: 'danger', onClick: () => decide('DEPRECATED'), busy: busy === 'DEPRECATED' }, '下线') : null),
        rec.status === 'REJECTED' ? h(Note, null, '被拒绝的记录要修改后才能重新提交；管理员也可以直接批准。') : null)
        : !admin && rec.status === 'PENDING_APPROVAL' ? h(Note, null, '等待管理员批准。') : null,
      h(ErrorLine, { error })),
    editing ? h(EditRecord, { rid, rec, onSaved: () => { setEditing(false); load(); onChanged() } }) : null)
}

// -- registering --------------------------------------------------------------------------------------------------------

function useCreate(rid, onCreated) {
  const { wid } = useCtx()
  const { busy, error, run } = useAction()
  const create = (body) => run('create', () => call('POST', reg(wid, rid, '/records'), body),
    (r) => `已登记 ${r.name}（草稿）${listOf(r.unlistedTargets).length ? `；目标 ${r.unlistedTargets.join('，')} 的工具没有内联定义，未列出` : ''}`).then((r) => r && onCreated(r))
  return { busy, error, run, create }
}

function AgentForm({ rid, onCreated }) {
  const { wid } = useCtx()
  const [agents, setAgents] = useState([])
  const [target, setTarget] = useState('')
  const [form, setForm] = useState({ name: '', version: '1.0.0', description: '', card: '' })
  const { busy, error, run, create } = useCreate(rid, onCreated)
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  useEffect(() => { if (wid) call('GET', `/workspaces/${wid}/agents`).then((r) => setAgents(listOf(r.agents))).catch(() => {}) }, [wid])
  const fill = () => {
    const [kind, ident] = target.split(':')
    run('fill', async () => {
      const r = await call('GET', reg(wid, '', `/agent-card?kind=${kind}&ident=${encodeURIComponent(ident)}`))
      setForm((f) => ({ ...f, name: r.card.name, description: r.card.description, card: JSON.stringify(r.card, null, 1) }))
    })
  }
  const register = () => {
    let card
    try { card = JSON.parse(form.card) } catch (err) { window.dispatchEvent(new CustomEvent('adlc-notify', { detail: { kind: 'error', message: `名片不是 JSON：${err.message}` } })); return }
    create({ type: 'agent', name: form.name, version: form.version, description: form.description, card })
  }
  return h(Card, { title: '登记 Agent（A2A 名片）', extra: h(Button, { kind: 'pri', onClick: register, busy: busy === 'create', disabled: !form.card || !form.name }, '登记') },
    h('div', { className: 'cs-row' },
      h(Select, { value: target, onChange: setTarget, options: [['', '选一个 Agent'], ...agents.map((a) => [`${a.kind}:${a.id}`, `${a.name}（${a.kind === 'harness' ? 'Harness' : 'Runtime'}）`])] }),
      h(Button, { onClick: fill, busy: busy === 'fill', disabled: !target }, '从 Agent 生成名片')),
    h(Note, null, '名片从 Agent 读出：Prompt 开头作说明，每个 Gateway 工具目标、每个 S3 技能各是一项技能，并写明它对应的 Harness / Runtime（之后据此附上它的验证证据）。登记前可以改。'),
    h('div', { className: 'cs-grid' },
      h(Field, { label: '记录名称' }, h(Input, { value: form.name, onChange: set('name'), mono: true })),
      h(Field, { label: '版本' }, h(Input, { value: form.version, onChange: set('version'), mono: true })),
      h(Field, { label: '说明' }, h(Input, { value: form.description, onChange: set('description') }))),
    h(Field, { label: 'A2A 名片（JSON：protocolVersion、name、description、url、version、capabilities、defaultInputModes、defaultOutputModes、skills）' },
      h(TextArea, { value: form.card, onChange: set('card'), rows: 14, mono: true, placeholder: '先选 Agent 生成，或粘贴一张 A2A 0.3 名片' })),
    h(ErrorLine, { error }))
}

function McpForm({ rid, onCreated }) {
  const { wid } = useCtx()
  const [gateways, setGateways] = useState(null)
  const [form, setForm] = useState({ mode: 'gateway', gatewayId: '', url: '', transport: 'streamable-http', name: '', description: '', version: '1.0.0' })
  const { busy, error, create } = useCreate(rid, onCreated)
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  useEffect(() => { if (wid) call('GET', reg(wid, '', '/gateways')).then((r) => setGateways(listOf(r.gateways))).catch(() => setGateways([])) }, [wid])
  const pick = (id) => {
    const g = listOf(gateways).find((x) => x.id === id)
    setForm((f) => ({ ...f, gatewayId: id, name: f.name || (g ? g.name : ''), description: f.description || (g ? g.description : '') }))
  }
  const register = () => create(form.mode === 'gateway'
    ? { type: 'mcp', gatewayId: form.gatewayId, name: form.name, description: form.description, version: form.version }
    : { type: 'mcp', url: form.url, transport: form.transport, name: form.name, description: form.description, version: form.version })
  const ready = form.name && (form.mode === 'gateway' ? form.gatewayId : form.url)
  return h(Card, { title: '登记 MCP 工具', extra: h(Button, { kind: 'pri', onClick: register, busy: busy === 'create', disabled: !ready }, '登记') },
    h('div', { className: 'cs-grid' },
      h(Field, { label: '来源' }, h(Select, { value: form.mode, onChange: set('mode'), options: [['gateway', '这个工作区的 AgentCore Gateway'], ['url', 'MCP 服务地址']] })),
      form.mode === 'gateway'
        ? h(Field, { label: 'Gateway', hint: gateways && !gateways.length ? '这个工作区没有 MCP Gateway' : '工具从各目标的内联定义读出（<目标>___<工具>）' },
          h(Select, { value: form.gatewayId, onChange: pick, options: [['', gateways === null ? '加载中…' : '选择'], ...listOf(gateways).map((g) => [g.id, `${g.name}（${g.authorizer || ''}）`])] }))
        : h(Field, { label: '地址' }, h(Input, { value: form.url, onChange: set('url'), mono: true, placeholder: 'https://example.com/mcp' })),
      form.mode === 'url' ? h(Field, { label: '传输' }, h(Select, { value: form.transport, onChange: set('transport'), options: [['streamable-http', 'Streamable HTTP'], ['sse', 'SSE']] })) : null,
      h(Field, { label: '记录名称' }, h(Input, { value: form.name, onChange: set('name'), mono: true })),
      h(Field, { label: '版本' }, h(Input, { value: form.version, onChange: set('version'), mono: true })),
      h(Field, { label: '说明', hint: 'MCP 的 server.json 只留前 100 个字符' }, h(Input, { value: form.description, onChange: set('description') }))),
    h(ErrorLine, { error }))
}

function SkillForm({ rid, onCreated }) {
  const [form, setForm] = useState({ uri: '', name: '', description: '', version: '1.0.0' })
  const { busy, error, create } = useCreate(rid, onCreated)
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const register = () => create({ type: 'skill', uri: form.uri, name: form.name, description: form.description, version: form.version })
  return h(Card, { title: '登记技能（S3 里的 SKILL.md）', extra: h(Button, { kind: 'pri', onClick: register, busy: busy === 'create', disabled: !form.uri }, '登记') },
    h(Field, { label: 'S3 位置', hint: 's3://bucket/prefix/（放 SKILL.md 的目录）或 s3://bucket/prefix/SKILL.md；直接模式的技能在 s3://adlc-direct-<账号>-<区域>/direct/<agent>/skills/<技能>/' },
      h(Input, { value: form.uri, onChange: set('uri'), mono: true, placeholder: 's3://bucket/skills/leave-calculator/' })),
    h('div', { className: 'cs-grid' },
      h(Field, { label: '记录名称（可选）', hint: '默认用 SKILL.md 的 name' }, h(Input, { value: form.name, onChange: set('name'), mono: true })),
      h(Field, { label: '版本' }, h(Input, { value: form.version, onChange: set('version'), mono: true })),
      h(Field, { label: '说明（可选）', hint: '默认用 SKILL.md 的 description' }, h(Input, { value: form.description, onChange: set('description') }))),
    h(Note, null, 'SKILL.md 要以 --- 开头的 frontmatter，name 只用小写字母、数字和单个连字符，并有 description：否则 Registry 不收。'),
    h(ErrorLine, { error }))
}

function NewRecord({ rid, onCreated }) {
  const [kind, setKind] = useState('agent')
  const Form = { agent: AgentForm, mcp: McpForm, skill: SkillForm }[kind]
  return h(Fragment, null, h(Tabs, { value: kind, onChange: setKind, options: [['agent', 'Agent'], ['mcp', 'MCP 工具'], ['skill', '技能']] }), h(Form, { rid, onCreated }))
}

// -- search -------------------------------------------------------------------------------------------------------------

function Search({ rid, onOpen }) {
  const { wid } = useCtx()
  const [q, setQ] = useState('')
  const [kind, setKind] = useState('')
  const [results, setResults] = useState(null)
  const { busy, error, run } = useAction()
  const go = () => run('search', async () => setResults(listOf((await call('GET', reg(wid, rid, `/search?q=${encodeURIComponent(q)}&type=${kind}`))).records)))
  return h(Card, { title: '搜索（别的 Agent 和用户能发现的内容）', extra: h('span', { className: 'cs-row' },
    h(Input, { value: q, onChange: setQ, placeholder: '比如：查假期余额' }),
    h(Select, { value: kind, onChange: setKind, options: [['', '全部类型'], ['agent', 'Agent'], ['mcp', 'MCP 工具'], ['skill', '技能']] }),
    h(Button, { kind: 'pri', onClick: go, busy: busy === 'search' }, '搜索')) },
  h(Note, null, '语义搜索，只返回已批准的记录，按相关度排序；刚批准的记录要几秒后才搜得到。'),
  results === null ? h(Empty, null, '输入要找的能力。留空搜索列出全部已批准的记录。') : h(Table, {
    head: ['名称', '类型', '版本', '说明', '证据'],
    rows: results.map((r) => ({ key: r.id, onClick: () => onOpen(r.id), cells: [h('b', null, r.displayName || r.name), KIND[r.type] || r.type,
      h('span', { className: 'cs-mono' }, r.version || '—'), (r.description || '').slice(0, 120),
      r.evidence ? h(Chip, { tone: r.evidence.robust ? 'ok' : 'warn' }, `${r.evidence.holding}/${r.evidence.contracts} ${r.evidence.robust ? '稳定' : '不稳定'}`) : '—'] })),
    empty: '没有找到已批准的记录。' }),
  h(ErrorLine, { error }))
}

// -- the page -------------------------------------------------------------------------------------------------------------

function RegistryPage() {
  const { wid, workspace, me } = useCtx()
  const admin = Boolean(me && me.role === 'admin')
  const [list, setList] = useState(null)
  const [listError, setListError] = useState(null)
  const [rid, setRid] = useState('')
  const [tab, setTab] = useState('records')
  const [open, setOpen] = useState(null)
  const [version, setVersion] = useState(0)
  const [creating, setCreating] = useState(false)
  const { busy, error, run } = useAction()
  const load = () => call('GET', reg(wid)).then((r) => {
    const found = listOf(r.registries)
    setList(found); setListError(null)
    setRid((current) => (found.some((x) => x.id === current) ? current : (found.find((x) => x.status === 'READY') || found[0] || {}).id || ''))
  }).catch(setListError)
  useEffect(() => { setList(null); setOpen(null); if (wid) load() }, [wid])
  if (!workspace) return h(Empty, null, '先添加工作区。')
  const pickRegistry = (v) => { setRid(v); setOpen(null) }
  const pickTab = (v) => { setTab(v); setOpen(null) }
  const sel = listOf(list).find((x) => x.id === rid)
  const changed = () => setVersion((v) => v + 1)
  const removeRegistry = () => window.confirm(`删除 Registry ${sel.name}？`) && run('delreg', async () => {
    try {
      return await call('DELETE', reg(wid, sel.id))
    } catch (err) {
      if (/delete them with it/.test(err.message) && window.confirm(`${sel.name} 里还有控制台登记的记录。连同这些记录一起删除？已批准的记录会从发现中消失。`)) {
        return call('DELETE', reg(wid, sel.id, '?withRecords=1'))
      }
      throw err
    }
  }, (r) => `已删除 Registry（连同 ${r.records} 条记录）`).then((r) => r && load())
  const created = (r) => { setTab('records'); changed(); setOpen(r.id) }
  const detail = open && sel ? h(RecordDetail, { rid: sel.id, id: open, onChanged: changed, onClosed: () => setOpen(null) }) : null
  return h(Fragment, null,
    h('h1', null, 'Registry'),
    h(Card, { title: 'Agent、MCP 工具和技能的目录', extra: h('span', { className: 'cs-row' },
      h(Select, { value: rid, onChange: pickRegistry, options: list && list.length ? list.map((x) => [x.id, `${x.name} · ${x.autoApproval ? '自动批准' : '人工审批'}`]) : [['', list === null ? '加载中…' : '没有 Registry']] }),
      h(Button, { onClick: () => { load(); changed() } }, '刷新'),
      admin ? h(Button, { onClick: () => setCreating(!creating) }, creating ? '收起' : '新建 Registry') : null) },
    h(ErrorLine, { error: listError }),
    sel ? h('div', { className: 'cs-row' },
      h(Chip, { tone: REGISTRY_TONE[sel.status] }, sel.status), h(Chip, null, sel.autoApproval ? '自动批准：提交即上架' : '人工审批：管理员批准后上架'),
      h(Chip, null, `发现授权 ${sel.authorizer || '—'}`), sel.console ? h(Chip, { tone: 'info' }, '控制台创建') : null,
      h('span', { className: 'cs-mut cs-mono' }, sel.id), sel.description ? h('span', { className: 'cs-mut' }, sel.description) : null,
      admin && sel.console ? h(Button, { kind: 'danger', onClick: removeRegistry, busy: busy === 'delreg' }, '删除 Registry') : null)
      : list && !list.length ? h(Empty, null, admin ? '这个账号和区域还没有 Registry：新建一个。' : '这个账号和区域还没有 Registry：请管理员新建。') : null,
    sel && sel.status !== 'READY' ? h(Note, { tone: 'warn' }, `Registry 是 ${sel.status}：就绪（READY，新建约一分钟）后才能登记记录。`) : null,
    h(ErrorLine, { error })),
    creating && admin ? h(NewRegistry, { onCreated: (r) => { setCreating(false); load().then(() => setRid(r.id)) } }) : null,
    sel ? h(Fragment, null,
      h(Tabs, { value: tab, onChange: pickTab, options: [['records', '目录'], ['review', '待审批'], ['new', '登记'], ['search', '搜索']] }),
      tab === 'records' ? h(Fragment, null, h(Card, { title: '记录' }, h(Records, { rid: sel.id, version, open, onOpen: setOpen })), detail) : null,
      tab === 'review' ? h(Fragment, null, h(Card, { title: '待审批' },
        h(Note, null, admin ? '打开一条记录，看它附的验证证据，再批准或拒绝。' : '只有管理员能批准或拒绝。'),
        h(Records, { rid: sel.id, status: 'PENDING_APPROVAL', version, open, onOpen: setOpen, empty: '没有待审批的记录。' })), detail) : null,
      tab === 'new' ? h(NewRecord, { rid: sel.id, onCreated: created }) : null,
      tab === 'search' ? h(Fragment, null, h(Search, { rid: sel.id, onOpen: setOpen }), detail) : null) : null)
}

export default { id: 'registry', label: 'Registry', group: '治理', Page: RegistryPage }
