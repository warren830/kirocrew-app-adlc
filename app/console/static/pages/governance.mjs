// 策略: Cedar policies enforced at an AgentCore Gateway — policy engines and their policies (a Cedar editor, validation by
// the service, a policy from one sentence), attaching an engine to a Gateway, the Gateway's rules, rate limits and
// resource policy, and the decision log (every Allow / Deny with the policy that decided it).
import React from 'react'
import { call, useCtx, listOf, Card, Button, Field, Input, TextArea, Select, Chip, Note, Empty, ErrorLine, Table, useAction, Tabs, STATUS_TONE } from '../ui.mjs'

const { useState, useEffect, useRef, createElement: h, Fragment } = React
const gov = (wid, path) => `/workspaces/${wid}/governance${path}`
const tone = (s) => (String(s || '').endsWith('FAILED') ? 'bad' : STATUS_TONE[s] || '')
const shortId = (id) => String(id || '').replace(/-[a-z0-9_]{10}$/, '')
const mono = (text, title) => h('span', { className: 'cs-mono', title }, text)
const nowrap = (...children) => h('span', { style: { whiteSpace: 'nowrap' } }, ...children)
const narrow = (width, child) => h('span', { style: { display: 'inline-block', width } }, child)
const when = (at) => nowrap(String(at || '').slice(0, 19).replace('T', ' '))
const MODE = { LOG_ONLY: '仅记录', ENFORCE: '拦截', ACTIVE: '生效' }

// A Gateway the console did not create is changed only after the user agrees (the server asks for acknowledged: true).
function consent(g, what) {
  if (!g) return null
  if (g.console) return {}
  return window.confirm(`Gateway「${g.name}」不是这个控制台创建的，${what}会影响所有使用它的 Agent。确认要改吗？`) ? { acknowledged: true } : null
}

function cedarTemplate(kind, gatewayArn) {
  const gw = gatewayArn || 'arn:aws:bedrock-agentcore:<region>:<account>:gateway/<gateway-id>'
  if (kind === 'forbid') {
    return `// 退款超过 100 必须拒绝（工具名是 <target>___<tool>，参数在 context.input 下）\nforbid(\n  principal,\n  action == AgentCore::Action::"<target>___issue_refund",\n  resource == AgentCore::Gateway::"${gw}"\n) when { context.input.amount > 100 };`
  }
  return `// 允许某个 IAM 角色调用查询工具\npermit(\n  principal == AgentCore::IamEntity::"arn:aws:sts::<account>:assumed-role/<role-name>",\n  action == AgentCore::Action::"<target>___get_balance",\n  resource == AgentCore::Gateway::"${gw}"\n);`
}

// -- policies ---------------------------------------------------------------------------------------------------------

function Generate({ engine, gateways, onUse, onCreated }) {
  const { wid } = useCtx()
  const [gatewayId, setGatewayId] = useState('')
  const [text, setText] = useState('')
  const [gen, setGen] = useState(null)
  const { busy, error, run } = useAction()
  useEffect(() => {
    if (!gen || gen.status !== 'GENERATING') return undefined
    const timer = setTimeout(() => call('GET', gov(wid, `/engines/${engine.id}/generations/${gen.id}`)).then(setGen).catch(() => setGen({ ...gen })), 3000)
    return () => clearTimeout(timer)
  }, [gen])
  const start = () => run('gen', async () => setGen(await call('POST', gov(wid, `/engines/${engine.id}/generations`), { gatewayId, text })))
  const create = (a) => {
    const findings = listOf(a.findings).map((f) => f.type)
    if (findings.length && !window.confirm(`这条有校验发现（${findings.join('，')}），仍要建为策略？会以「忽略发现」创建，先仅记录。`)) return
    const name = window.prompt('策略名称（字母开头，字母、数字或下划线）', `${(gen.name || 'generated').slice(0, 30)}_${a.id.slice(-4)}`)
    if (name) run(`mk-${a.id}`, () => call('POST', gov(wid, `/engines/${engine.id}/policies`), { name, generationId: gen.id, assetId: a.id, enforcementMode: 'LOG_ONLY',
      ignoreFindings: listOf(a.findings).length > 0 }), (r) => `已建策略 ${r.name}：${r.status}（仅记录）`).then((r) => r && onCreated())
  }
  return h(Card, { title: '用一句话生成 Cedar', extra: h(Button, { kind: 'pri', onClick: start, busy: busy === 'gen' || (gen && gen.status === 'GENERATING'),
    disabled: !gatewayId || !text.trim() || !engine.console }, '生成') },
  h('div', { className: 'cs-grid' },
    h(Field, { label: '按哪个 Gateway 的工具生成（只读取它的工具）' }, h(Select, { value: gatewayId, onChange: setGatewayId,
      options: [['', '选择 Gateway'], ...gateways.map((g) => [g.id, g.name])] }))),
  h(Field, { label: '一句话（谁可以或不可以做什么）', hint: '例如：任何人都可以查余额；退款金额超过 100 美元一律不允许。' },
    h(TextArea, { value: text, onChange: setText, rows: 3 })),
  gen ? h(Note, { tone: gen.status === 'GENERATED' ? 'ok' : gen.status === 'GENERATING' ? '' : 'warn' },
    gen.status === 'GENERATING' ? '生成中（约 15 秒）…' : `${gen.status}${listOf(gen.statusReasons).length ? '：' + gen.statusReasons.join('；') : ''}`) : null,
  gen && listOf(gen.assets).map((a) => h('div', { key: a.id, className: 'cs-card' },
    h('div', { className: 'cs-card-h' }, h('span', null, '「', a.fragment, '」'),
      h('span', { className: 'cs-row' }, listOf(a.findings).map((f, i) => h(Chip, { key: i, tone: f.type === 'INVALID' || f.type === 'NOT_TRANSLATABLE' ? 'bad' : 'warn', title: f.description }, f.type)),
        a.usable ? h(Button, { onClick: () => onUse(a.statement) }, '放进编辑器') : null,
        a.usable ? h(Button, { kind: 'pri', onClick: () => create(a), busy: busy === `mk-${a.id}` }, '建为策略（仅记录）') : null)),
    a.statement ? h('pre', { className: 'cs-pre' }, a.statement) : h(Note, { tone: 'warn' }, listOf(a.findings).map((f) => f.description).join('；') || '这句话没法用这个 Gateway 的工具表达。'))),
  h(ErrorLine, { error }))
}

function Editor({ engine, gateways, policy, statement, setStatement, onSaved, onClear }) {
  const { wid } = useCtx()
  const [form, setForm] = useState({ name: '', description: '', mode: 'LOG_ONLY', ignore: 'n', gateway: '' })
  const [result, setResult] = useState(null)
  const saved = useRef('')
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  useEffect(() => {
    if (!policy || policy.id !== saved.current) setResult(null)  // a policy just saved keeps its verdict on screen
    setForm((f) => ({ ...f, name: policy ? policy.name : '', description: policy ? policy.description || '' : '', mode: policy ? policy.enforcementMode || 'LOG_ONLY' : 'LOG_ONLY' }))
  }, [policy && policy.id])
  const body = () => ({ statement, enforcementMode: form.mode, ignoreFindings: form.ignore === 'y', description: form.description || undefined })
  const validate = () => run('check', async () => setResult({ kind: 'check', ...(await call('POST', gov(wid, `/engines/${engine.id}/validate`), body())) }))
  const save = () => run('save', async () => {
    const out = policy ? await call('PUT', gov(wid, `/engines/${engine.id}/policies/${policy.id}`), body())
      : await call('POST', gov(wid, `/engines/${engine.id}/policies`), { ...body(), name: form.name })
    setResult({ kind: 'save', ...out })
    saved.current = out.id || ''
    onSaved(out)
    return out
  }, (r) => `${r.name}：${r.status}`)
  const gwArn = (gateways.find((g) => g.id === form.gateway) || {}).arn
  const reasons = result ? listOf(result.reasons || result.statusReasons) : []
  return h(Card, { title: policy ? `编辑 ${policy.name}` : '新建 Cedar 策略', extra: h('span', { className: 'cs-row' },
    h(Button, { onClick: validate, busy: busy === 'check', disabled: !engine.console || !statement.trim(), title: '服务端按 Gateway 的工具 schema 校验，不保留任何策略' }, '校验'),
    h(Button, { kind: 'pri', onClick: save, busy: busy === 'save', disabled: !engine.console || !statement.trim() || (!policy && !form.name) }, policy ? '保存修改' : '新建'),
    h(Button, { onClick: () => { setResult(null); onClear() } }, '清空')) },
  h('div', { className: 'cs-grid' },
    policy ? h(Field, { label: '名称' }, mono(policy.name, policy.id)) : h(Field, { label: '名称', hint: '字母开头，字母、数字或下划线' },
      h(Input, { value: form.name, onChange: set('name'), mono: true, placeholder: 'refund_cap' })),
    h(Field, { label: '模式' }, h(Select, { value: form.mode, onChange: set('mode'), options: [['LOG_ONLY', '仅记录（不影响决定）'], ['ACTIVE', '生效（参与 Allow / Deny）']] })),
    h(Field, { label: '校验发现（如过于宽泛的 permit）' }, h(Select, { value: form.ignore, onChange: set('ignore'),
      options: [['n', '有发现就不通过'], ['y', '忽略发现（宽泛的 permit 需要，每次修改都要选）']] })),
    h(Field, { label: '说明' }, h(Input, { value: form.description, onChange: set('description') }))),
  h('div', { className: 'cs-row' }, h('span', { className: 'cs-mut' }, '模板：'),
    narrow(280, h(Select, { value: form.gateway, onChange: set('gateway'), options: [['', '套用到哪个 Gateway'], ...gateways.map((g) => [g.id, g.name])] })),
    h(Button, { onClick: () => setStatement(cedarTemplate('permit', gwArn)) }, 'permit'), h(Button, { onClick: () => setStatement(cedarTemplate('forbid', gwArn)) }, 'forbid')),
  h(Field, { label: 'Cedar', hint: 'forbid 优先于 permit；没有 permit 命中的调用一律拒绝。工具是 AgentCore::Action::"<target>___<tool>"，参数在 context.input 下。' },
    h(TextArea, { value: statement, onChange: setStatement, rows: 12, mono: true, placeholder: cedarTemplate('forbid') })),
  result ? h(Note, { tone: (result.kind === 'check' ? result.valid : result.status === 'ACTIVE') ? 'ok' : 'warn' },
    result.kind === 'check' ? (result.valid ? '校验通过：服务端接受这条策略。' : `校验不通过（${result.stage === 'parse' ? '语法' : 'schema / 发现'}）`)
      : `${result.status}${result.kept ? '：修改没生效，原来的定义仍在执行' : ''}`) : null,
  reasons.length ? h('pre', { className: 'cs-pre' }, reasons.join('\n\n')) : null,
  result && result.leftover ? h(Note, { tone: 'warn' }, `校验用的临时策略 ${result.leftover.name} 没删掉，请在列表里删除。`) : null,
  h(ErrorLine, { error }))
}

function PoliciesTab({ data, reload }) {
  const { wid } = useCtx()
  const [engineId, setEngineId] = useState('')
  const [policies, setPolicies] = useState(null)
  const [editing, setEditing] = useState(null)
  const [statement, setStatement] = useState('')
  const [form, setForm] = useState({ name: '', description: '' })
  const { busy, error, run } = useAction()
  const engines = listOf(data.engines)
  const engine = engines.find((e) => e.id === engineId) || null
  const load = () => engineId && call('GET', gov(wid, `/engines/${engineId}/policies`)).then((r) => setPolicies(listOf(r.policies))).catch(() => setPolicies([]))
  useEffect(() => { setPolicies(null); setEditing(null); setStatement(''); load() }, [engineId])
  const create = () => run('engine', () => call('POST', gov(wid, '/engines'), form), (r) => `已建策略引擎 ${r.name}`).then((r) => { if (r) { setForm({ name: '', description: '' }); reload() } })
  const removeEngine = (e) => {
    const count = e.policies || 0
    if (!window.confirm(`删除策略引擎 ${e.name}${count ? `和它的 ${count} 条策略` : ''}？不可恢复。`)) return
    run(`del-${e.id}`, () => call('DELETE', gov(wid, `/engines/${e.id}`), { deletePolicies: count > 0 }), '已删除').then((r) => { if (r) { if (e.id === engineId) setEngineId(''); reload() } })
  }
  const removePolicy = (p) => window.confirm(`删除策略 ${p.name}？`) && run(`delp-${p.id}`, () => call('DELETE', gov(wid, `/engines/${engineId}/policies/${p.id}`)), '已删除')
    .then((r) => { if (r) { if (editing && editing.id === p.id) { setEditing(null); setStatement('') } load(); reload() } })
  return h(Fragment, null,
    h(Card, { title: '策略引擎（Cedar 策略的容器，挂到 Gateway 上生效）' }, h(Table, {
      head: ['名称', '状态', '来源', '策略', '挂在', ''],
      rows: engines.map((e) => ({ key: e.id, onClick: () => setEngineId(e.id), cells: [h('b', null, e.name), h(Chip, { tone: tone(e.status) }, e.status),
        e.console ? h(Chip, { tone: 'info' }, '控制台') : h(Chip, null, '其他（只读）'), e.policies ?? '—',
        listOf(e.gateways).map((g) => `${g.name}（${MODE[g.mode] || g.mode}）`).join('，') || '—',
        e.console ? nowrap(h(Button, { kind: 'danger', onClick: (ev) => { ev.stopPropagation(); removeEngine(e) }, busy: busy === `del-${e.id}`, disabled: listOf(e.gateways).length > 0,
          title: listOf(e.gateways).length ? '先在「Gateway」里卸下' : '' }, '删除')) : null] })),
      empty: '还没有策略引擎。' }),
    h('div', { className: 'cs-grid' }, h(Input, { value: form.name, onChange: (v) => setForm((f) => ({ ...f, name: v })), placeholder: '新引擎名称，如 refund_rules', mono: true }),
      h(Input, { value: form.description, onChange: (v) => setForm((f) => ({ ...f, description: v })), placeholder: '说明（可选）' }),
      h('div', null, h(Button, { kind: 'pri', onClick: create, busy: busy === 'engine', disabled: !form.name }, '新建引擎'))),
    h(ErrorLine, { error })),
    engine ? h(Fragment, null,
      engine.console ? null : h(Note, { tone: 'warn' }, `「${engine.name}」不是这个控制台创建的：只能查看，不能改它的策略。`),
      h(Card, { title: `${engine.name} 的策略`, extra: h(Button, { onClick: () => { setEditing(null); setStatement('') } }, '写新策略') },
        policies === null ? h(Empty, null, '加载中…') : h(Table, {
          head: ['名称', '状态', '模式', 'Cedar', '更新', ''],
          rows: policies.map((p) => ({ key: p.id, onClick: () => { setEditing(p); setStatement(p.statement) }, cells: [h('b', { title: p.id }, p.name),
            nowrap(h(Chip, { tone: tone(p.status), title: listOf(p.statusReasons).join('\n') }, p.status)),
            nowrap(h(Chip, { tone: p.enforcementMode === 'ACTIVE' ? 'ok' : 'warn' }, MODE[p.enforcementMode] || '—')),
            mono(p.statement.replace(/\s+/g, ' ').slice(0, 90)), when(p.updatedAt),
            engine.console ? nowrap(h(Button, { kind: 'danger', onClick: (ev) => { ev.stopPropagation(); removePolicy(p) }, busy: busy === `delp-${p.id}` }, '删除')) : null] })),
          empty: '这个引擎还没有策略：挂到 ENFORCE 的 Gateway 上会拒绝所有调用。' })),
      h(Editor, { engine, gateways: listOf(data.gateways), policy: editing, statement, setStatement, onClear: () => { setEditing(null); setStatement('') },
        onSaved: (p) => { if (p && p.id) setEditing(p); load(); reload() } }),
      h(Generate, { engine, gateways: listOf(data.gateways), onUse: (s) => { setEditing(null); setStatement(s) }, onCreated: () => { load(); reload() } })) : h(Empty, null, '选一个策略引擎看它的策略。'))
}

// -- Gateways -----------------------------------------------------------------------------------------------------------

const RULE_TEMPLATES = {
  bundle: { conditions: [{ matchPrincipals: { anyOf: [{ iamPrincipal: { arn: 'arn:aws:iam::<account>:role/<role>', operator: 'StringEquals' } }] } }],
    actions: [{ configurationBundle: { staticOverride: { bundleArn: 'arn:aws:bedrock-agentcore:<region>:<account>:configuration-bundle/<bundle-id>', bundleVersion: '<version-uuid>' } } }] },
  route: { conditions: [{ matchPaths: { anyOf: ['/<target>/*'] } }], actions: [{ routeToTarget: { staticRoute: { targetName: '<http-target>' } } }] },
  split: { conditions: [], actions: [{ routeToTarget: { weightedRoute: { trafficSplit: [{ name: 'stable', weight: 90, targetName: '<http-target>' },
    { name: 'canary', weight: 10, targetName: '<http-target-2>' }] } } }] },
}
const describeRule = (r) => [
  listOf(r.conditions).map((c) => c.matchPrincipals ? `调用者 ${listOf(c.matchPrincipals.anyOf).map((p) => (p.iamPrincipal || {}).arn).join(' / ')}`
    : c.matchPaths ? `路径 ${listOf(c.matchPaths.anyOf).join(' / ')}` : JSON.stringify(c)).join(' 且 ') || '所有请求',
  listOf(r.actions).map((a) => {
    const route = a.routeToTarget || {}
    const bundle = a.configurationBundle || {}
    if (route.staticRoute) return `→ 目标 ${route.staticRoute.targetName}`
    if (route.weightedRoute) return `→ ${listOf(route.weightedRoute.trafficSplit).map((s) => `${s.targetName} ${s.weight}%`).join(' / ')}`
    if (bundle.staticOverride) return `配置包 ${shortId(String(bundle.staticOverride.bundleArn).split('/').pop())}`
    if (bundle.weightedOverride) return `配置包分流 ${listOf(bundle.weightedOverride.trafficSplit).map((s) => `${s.name} ${s.weight}%`).join(' / ')}`
    return JSON.stringify(a)
  }).join('，')]

function Rules({ gateway }) {
  const { wid } = useCtx()
  const [list, setList] = useState(null)
  const [form, setForm] = useState({ priority: '10', description: '', template: 'bundle', json: JSON.stringify(RULE_TEMPLATES.bundle, null, 1) })
  const { busy, error, run } = useAction()
  const load = () => call('GET', gov(wid, `/gateways/${gateway.id}/rules`)).then((r) => setList(listOf(r.rules))).catch(() => setList([]))
  useEffect(() => { setList(null); load() }, [gateway.id])
  const create = () => {
    const ack = consent(gateway, '加规则')
    if (ack) run('create', async () => call('POST', gov(wid, `/gateways/${gateway.id}/rules`), { ...JSON.parse(form.json), priority: Number(form.priority), description: form.description, ...ack }), '已加规则').then((r) => r && load())
  }
  const remove = (r) => { const ack = consent(gateway, '删规则'); if (ack && window.confirm(`删除优先级 ${r.priority} 的规则？`)) run(`del-${r.id}`, () => call('DELETE', gov(wid, `/gateways/${gateway.id}/rules/${r.id}`), ack), '已删除').then((x) => x && load()) }
  return h(Card, { title: 'Gateway 规则（按调用者或路径改路由、换配置包）' },
    list === null ? h(Empty, null, '加载中…') : h(Table, { head: ['优先级', '条件', '动作', '状态', ''], rows: list.map((r) => {
      const [matches, effect] = describeRule(r)
      return { key: r.id, cells: [r.priority, h('span', { title: r.description || '' }, matches), effect, h(Chip, { tone: tone(r.status) }, r.status),
        r.managedBy ? h(Chip, { title: '由别的功能管理，不在这里删' }, r.managedBy) : nowrap(h(Button, { kind: 'danger', onClick: () => remove(r), busy: busy === `del-${r.id}` }, '删除'))] }
    }), empty: '没有规则。' }),
    h('div', { className: 'cs-grid' },
      h(Field, { label: '优先级（1-1000000，小的先匹配）' }, h(Input, { value: form.priority, onChange: (v) => setForm((f) => ({ ...f, priority: v })) })),
      h(Field, { label: '模板' }, h(Select, { value: form.template, onChange: (v) => setForm((f) => ({ ...f, template: v, json: JSON.stringify(RULE_TEMPLATES[v], null, 1) })),
        options: [['bundle', '按调用者换配置包'], ['route', '按路径路由到 HTTP 目标'], ['split', '按比例分流两个 HTTP 目标']] })),
      h(Field, { label: '说明' }, h(Input, { value: form.description, onChange: (v) => setForm((f) => ({ ...f, description: v })) }))),
    h(Field, { label: '条件与动作（API 原样：conditions 最多 2 个，actions 1-2 个）' },
      h(TextArea, { value: form.json, onChange: (v) => setForm((f) => ({ ...f, json: v })), rows: 9, mono: true })),
    h(Note, null, 'routeToTarget 只能指向 HTTP 协议的目标；配置包规则要求 Gateway 角色有 bedrock-agentcore:GetConfigurationBundleVersion，否则命中的调用者 tools/list 会失败。'),
    h('div', { className: 'cs-row' }, h(Button, { kind: 'pri', onClick: create, busy: busy === 'create' }, '加规则')), h(ErrorLine, { error }))
}

const KEYS = [['toolName', '工具 toolName'], ['targetName', '目标 targetName'], ['$.context.iam.principal', 'IAM 调用者'], ['$.context.iam.sourceIdentity', 'IAM sourceIdentity'],
  ['qualifiedModelId', '模型 qualifiedModelId']]
const METRICS = { requests: ['请求数', ['minute', 'second']], tokens: ['token 数', ['minute']], connections: ['并发连接', ['second']] }
const PERIOD = { second: '每秒', minute: '每分钟' }

function RateLimits({ gateway }) {
  const { wid } = useCtx()
  const [list, setList] = useState(null)
  const [form, setForm] = useState({ id: '', key: 'toolName', value: '*', metric: 'requests', rate: '60', period: 'minute', description: '' })
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v, ...(k === 'metric' ? { period: METRICS[v][1][0] } : {}) }))
  const load = () => call('GET', gov(wid, `/gateways/${gateway.id}/rate-limits`)).then((r) => setList(listOf(r.rateLimits))).catch(() => setList([]))
  useEffect(() => { setList(null); load() }, [gateway.id])
  const create = () => {
    const ack = consent(gateway, '加限流')
    if (ack) run('create', () => call('POST', gov(wid, `/gateways/${gateway.id}/rate-limits`), { rateLimitId: form.id || undefined, description: form.description, dimensionKeys: [form.key],
      entries: [{ dimensions: { [form.key]: form.value || '*' }, [form.metric]: [{ rate: Number(form.rate), period: form.period }] }], ...ack }), '已加限流').then((r) => r && load())
  }
  const remove = (r) => { const ack = consent(gateway, '删限流'); if (ack && window.confirm(`删除限流 ${r.id}？`)) run(`del-${r.id}`, () => call('DELETE', gov(wid, `/gateways/${gateway.id}/rate-limits/${r.id}`), ack), '已删除').then((x) => x && load()) }
  const limits = (e) => Object.keys(METRICS).filter((m) => e[m]).map((m) => `${METRICS[m][0]} ${e[m][0].rate} ${PERIOD[e[m][0].period] || e[m][0].period}`).join('，')
  return h(Card, { title: '限流（超限的调用得到 HTTP 429 / JSON-RPC -32003）' },
    list === null ? h(Empty, null, '加载中…') : h(Table, { head: ['ID', '维度', '限额', '状态', ''], rows: list.map((r) => ({ key: r.id, cells: [mono(r.id, r.description || ''),
      r.dimensionKeys.join(' + '), listOf(r.entries).map((e, i) => h('div', { key: i }, `${Object.entries(e.dimensions).map(([k, v]) => `${k}=${v}`).join('，')}：${limits(e)}`)),
      h(Chip, { tone: tone(r.status) }, r.status), nowrap(h(Button, { kind: 'danger', onClick: () => remove(r), busy: busy === `del-${r.id}` }, '删除'))] })), empty: '没有限流。' }),
    h('div', { className: 'cs-grid' },
      h(Field, { label: '按什么分别计数' }, h(Select, { value: form.key, onChange: set('key'), options: KEYS })),
      h(Field, { label: '取值（* 表示每个值各自计数）' }, h(Input, { value: form.value, onChange: set('value'), mono: true })),
      h(Field, { label: '限什么' }, h(Select, { value: form.metric, onChange: set('metric'), options: Object.entries(METRICS).map(([k, [label]]) => [k, label]) })),
      h(Field, { label: '上限' }, h(Input, { value: form.rate, onChange: set('rate') })),
      h(Field, { label: '时间窗' }, h(Select, { value: form.period, onChange: set('period'), options: METRICS[form.metric][1].map((p) => [p, PERIOD[p]]) })),
      h(Field, { label: 'ID（可选）' }, h(Input, { value: form.id, onChange: set('id'), mono: true, placeholder: 'refunds-per-minute' }))),
    h(Field, { label: '说明' }, h(Input, { value: form.description, onChange: set('description') })),
    h(Note, null, 'token 只能按分钟限，并发连接只能按秒限；同一组维度只能有一条限流。'),
    h('div', { className: 'cs-row' }, h(Button, { kind: 'pri', onClick: create, busy: busy === 'create', disabled: !Number(form.rate) && form.rate !== '0' }, '加限流')), h(ErrorLine, { error }))
}

const RESOURCE_TEMPLATE = (g) => JSON.stringify({ Version: '2012-10-17', Statement: [{ Sid: 'AllowInvoke', Effect: 'Allow',
  Principal: { AWS: `arn:aws:iam::${String(g.arn).split(':')[4]}:root` }, Action: 'bedrock-agentcore:InvokeGateway', Resource: g.arn }] }, null, 1)

function ResourcePolicy({ gateway }) {
  const { wid } = useCtx()
  const [current, setCurrent] = useState(undefined)
  const [text, setText] = useState('')
  const { busy, error, run } = useAction()
  const load = () => call('GET', gov(wid, `/gateways/${gateway.id}/resource-policy`)).then((r) => { setCurrent(r.policy); setText(JSON.stringify(r.policy || JSON.parse(RESOURCE_TEMPLATE(gateway)), null, 1)) })
    .catch(() => setCurrent(null))
  useEffect(() => { setCurrent(undefined); load() }, [gateway.id])
  const save = () => { const ack = consent(gateway, '改资源策略'); if (ack) run('save', async () => call('PUT', gov(wid, `/gateways/${gateway.id}/resource-policy`), { policy: JSON.parse(text), ...ack }), '已保存').then((r) => r && load()) }
  const remove = () => { const ack = consent(gateway, '删资源策略'); if (ack && window.confirm('删除这个 Gateway 的资源策略？')) run('del', () => call('DELETE', gov(wid, `/gateways/${gateway.id}/resource-policy`), ack), '已删除').then((r) => r && load()) }
  return h(Card, { title: '资源策略（谁可以调用这个 Gateway，IAM JSON）', extra: h('span', { className: 'cs-row' },
    h(Button, { kind: 'pri', onClick: save, busy: busy === 'save' }, '保存'), current ? h(Button, { kind: 'danger', onClick: remove, busy: busy === 'del' }, '删除') : null) },
  current === undefined ? h(Empty, null, '加载中…') : current ? null : h(Note, null, '还没有资源策略：下面是模板（允许本账号调用）。'),
  h(TextArea, { value: text, onChange: setText, rows: 10, mono: true }), h(ErrorLine, { error }))
}

function Attach({ gateway, engines, reload }) {
  const { wid } = useCtx()
  const [engineId, setEngineId] = useState(gateway.engine ? gateway.engine.id : '')
  const [mode, setMode] = useState(gateway.engine ? gateway.engine.mode : 'LOG_ONLY')
  const [result, setResult] = useState(null)
  const { busy, error, run } = useAction()
  useEffect(() => { setEngineId(gateway.engine ? gateway.engine.id : ''); setMode(gateway.engine ? gateway.engine.mode : 'LOG_ONLY'); setResult(null) }, [gateway.id])
  const attach = (grantRole) => {
    const what = mode === 'ENFORCE' ? '挂成 ENFORCE（被拒绝的工具调用会直接失败，没有 permit 命中的调用一律拒绝）' : '挂成 LOG_ONLY（只记录决定，不拦截）'
    const ack = consent(gateway, '挂策略引擎')
    if (!ack || !window.confirm(`把策略引擎挂到「${gateway.name}」，${what}？`)) return
    run('attach', async () => { const out = await call('PUT', gov(wid, `/gateways/${gateway.id}/engine`), { engineId, mode, grantRole: grantRole || undefined, ...ack }); setResult(out); return out },
      (r) => `已挂上：${MODE[r.mode] || r.mode}${r.roleGranted ? '（已给 Gateway 角色授权）' : ''}`).then((r) => r && reload())
  }
  const detach = () => {
    const ack = consent(gateway, '卸下策略引擎')
    if (ack && window.confirm(`从「${gateway.name}」卸下策略引擎？之后它的工具调用不再经过任何策略。`)) run('detach', () => call('DELETE', gov(wid, `/gateways/${gateway.id}/engine`), ack), '已卸下').then((r) => r && reload())
  }
  const role = error && error.data && error.data.roleStatement ? error.data : null
  return h(Card, { title: '策略引擎', extra: gateway.engine ? h(Chip, { tone: gateway.engine.mode === 'ENFORCE' ? 'ok' : 'warn' }, `${shortId(gateway.engine.id)} · ${MODE[gateway.engine.mode] || gateway.engine.mode}`) : h(Chip, null, '未挂') },
    gateway.console ? null : h(Note, { tone: 'warn' }, '这个 Gateway 不是控制台创建的：任何修改都会先请你确认。'),
    h('div', { className: 'cs-grid' },
      h(Field, { label: '引擎' }, h(Select, { value: engineId, onChange: setEngineId, options: [['', '选择'], ...engines.filter((e) => e.status === 'ACTIVE').map((e) => [e.id, `${e.name}（${e.policies ?? 0} 条）`])] })),
      h(Field, { label: '模式' }, h(Select, { value: mode, onChange: setMode, options: [['LOG_ONLY', 'LOG_ONLY：只记录决定'], ['ENFORCE', 'ENFORCE：拦截被拒绝的调用']] }))),
    h('div', { className: 'cs-row' }, h(Button, { kind: 'pri', onClick: () => attach(false), busy: busy === 'attach', disabled: !engineId }, gateway.engine ? '换 / 改模式' : '挂上'),
      gateway.engine ? h(Button, { kind: 'danger', onClick: detach, busy: busy === 'detach' }, '卸下') : null),
    result && result.warning ? h(Note, { tone: 'warn' }, `注意：${result.warning === 'no ACTIVE permit policy in this engine: under ENFORCE every tool call is denied' ? '这个引擎没有生效的 permit 策略：ENFORCE 下所有工具调用都会被拒绝。' : result.warning}`) : null,
    role ? h(Fragment, null, h(Note, { tone: 'warn' }, `Gateway 的角色 ${role.roleArn} 还不能使用这个引擎，需要加上：`), h('pre', { className: 'cs-pre' }, JSON.stringify(role.roleStatement, null, 1)),
      role.canGrant ? h(Button, { kind: 'pri', onClick: () => attach(true), busy: busy === 'attach' }, '给角色授权并挂上（角色是控制台创建的）')
        : h(Note, null, '这个角色不是控制台创建的：请在 IAM 里给它加上这段权限，再挂。')) : h(ErrorLine, { error }))
}

function GatewaysTab({ data, reload }) {
  const [gid, setGid] = useState('')
  const gateways = listOf(data.gateways)
  const gateway = gateways.find((g) => g.id === gid) || null
  return h(Fragment, null,
    h(Card, { title: 'Gateway' }, h(Table, { head: ['名称', '认证', '来源', '策略引擎', '状态'], rows: gateways.map((g) => ({ key: g.id, onClick: () => setGid(g.id), cells: [
      h('b', { title: g.id }, g.name), g.authorizerType, g.console ? h(Chip, { tone: 'info' }, '控制台') : h(Chip, null, '其他'),
      g.engine ? h(Chip, { tone: g.engine.mode === 'ENFORCE' ? 'ok' : 'warn' }, `${shortId(g.engine.id)} · ${MODE[g.engine.mode] || g.engine.mode}`) : '—', h(Chip, { tone: tone(g.status) }, g.status)] })),
    empty: '这个工作区没有 Gateway。' })),
    gateway ? h(Fragment, null, h('h1', null, gateway.name), h(Attach, { gateway, engines: listOf(data.engines), reload }), h(Rules, { gateway }), h(RateLimits, { gateway }),
      h(ResourcePolicy, { gateway })) : h(Empty, null, '选一个 Gateway：挂策略引擎、规则、限流、资源策略。'))
}

// -- the decision log ------------------------------------------------------------------------------------------------------

function DecisionsTab({ data }) {
  const { wid } = useCtx()
  const gateways = listOf(data.gateways)
  const [gid, setGid] = useState(() => ((gateways.find((g) => g.engine) || {}).id || ''))
  const [hours, setHours] = useState('24')
  const [log, setLog] = useState(null)
  const { busy, error, run } = useAction()
  const gateway = gateways.find((g) => g.id === gid) || null
  const load = () => gid && run('load', async () => setLog(await call('GET', gov(wid, `/gateways/${gid}/decisions?hours=${hours}`))))
  useEffect(() => { setLog(null); load() }, [gid, hours])
  const toggle = (on) => {
    const ack = consent(gateway, on ? '开启决策日志' : '关闭决策日志')
    if (!ack || (on && !window.confirm('把这个 Gateway 的应用日志投递到 CloudWatch Logs（保留 30 天，按量计费）？之后每次策略决定一行，带调用者。'))) return
    run('toggle', () => call(on ? 'PUT' : 'DELETE', gov(wid, `/gateways/${gid}/decision-log`), ack), on ? '已开启：几分钟后开始有记录' : '已关闭投递（日志组保留）').then((r) => r && load())
  }
  const s = log ? log.summary : null
  const source = log ? { logs: `来自应用日志 ${log.logGroup}`, spans: '来自 aws/spans（Gateway 开了追踪，没有调用者）' }[log.source] : ''
  return h(Fragment, null,
    h(Card, { title: '决策日志', extra: h('span', { className: 'cs-row' },
      narrow(300, h(Select, { value: gid, onChange: setGid, options: [['', '选择 Gateway'], ...gateways.map((g) => [g.id, `${g.name}${g.engine ? `（${MODE[g.engine.mode] || g.engine.mode}）` : ''}`])] })),
      narrow(110, h(Select, { value: hours, onChange: setHours, options: [['1', '1 小时'], ['3', '3 小时'], ['24', '24 小时'], ['168', '7 天']] })),
      h(Button, { onClick: load, busy: busy === 'load', disabled: !gid }, '刷新')) },
    !gid ? h(Empty, null, '选一个 Gateway。') : !log ? h(Empty, null, busy ? '读取中…' : '—') : h(Fragment, null,
      log.source ? h(Note, null, source, log.source === 'logs' ? ' · ' : '', log.source === 'logs' ? h('a', { className: 'cs-click', onClick: () => toggle(false) }, '关闭投递') : null)
        : h(Note, { tone: 'warn' }, '这个 Gateway 没有把应用日志或追踪投递出去：下面只有 CloudWatch 指标里的计数。', ' ', h(Button, { onClick: () => toggle(true), busy: busy === 'toggle' }, '开启决策日志')),
      log.error ? h('div', { className: 'cs-err' }, log.error) : null,
      h('div', { className: 'cs-grid' },
        [['允许', s.allow, 'ok'], ['拒绝', s.deny, 'bad'], ['默认拒绝（没有策略命中）', s.defaultDeny, 'warn'], ['仅记录策略命中', s.logOnlyMatches, ''], ['生效后会翻转的决定', s.flips, 'warn']]
          .map(([label, n, t]) => h(Card, { key: label, title: label }, h('div', { className: `cs-big ${t ? `cs-tx-${t === 'bad' ? 'warn' : t}` : ''}` }, n)))),
      listOf(s.byPolicy).length ? h(Card, { title: '按决定性策略' }, h(Table, { head: ['策略', '允许', '拒绝', '仅记录命中', '会翻转'], rows: s.byPolicy.map((p) => ({ key: p.policy,
        cells: [mono(shortId(p.policy), p.policy), p.allow, p.deny, p.logOnlyMatches, p.flips] })) })) : null,
      h(Card, { title: `每次决定（${log.total} 条${log.total > listOf(log.rows).length ? `，显示最近 ${log.rows.length} 条` : ''}）` }, h(Table, {
        head: ['时间', '决定', '工具', '决定性策略', '原因', '调用者', '参数'],
        rows: listOf(log.rows).map((r, i) => ({ key: `${r.requestId || i}-${i}`, cells: [nowrap(String(r.at || '').slice(5, 19).replace('T', ' ')),
          nowrap(r.decision === 'ALLOW' ? h(Chip, { tone: 'ok' }, '允许') : r.blocked ? h(Chip, { tone: 'bad' }, '拒绝') : h(Chip, { tone: 'warn', title: 'LOG_ONLY：调用照常执行' }, '拒绝·未拦截')),
          mono(r.tool || '—'), listOf(r.policies).length ? listOf(r.policies).map((p) => h('div', { key: p }, mono(shortId(p), p))) : '—',
          h('span', { className: 'cs-mut' }, r.reason || (listOf(r.logOnly).length ? `仅记录命中：${r.logOnly.map(shortId).join('，')}` : '—')),
          r.principal ? mono(String(r.principal).split(':').pop(), r.principal) : '—', r.arguments ? mono(r.arguments.slice(0, 80), r.arguments) : '—'] })),
        empty: log.source ? '这段时间没有决定。' : '开启决策日志后，每次工具调用的决定都会列在这里。' }))),
    h(ErrorLine, { error })))
}

function GovernancePage() {
  const { wid, workspace } = useCtx()
  const [tab, setTab] = useState('policies')
  const [data, setData] = useState(null)
  const { error, run } = useAction()
  const reload = () => run('load', async () => setData(await call('GET', gov(wid, ''))))
  useEffect(() => { setData(null); if (wid) reload() }, [wid])
  if (!workspace) return h(Empty, null, '先添加工作区。')
  const Body = { policies: PoliciesTab, gateways: GatewaysTab, decisions: DecisionsTab }[tab]
  return h(Fragment, null, h('h1', null, '策略'),
    h(Tabs, { value: tab, onChange: setTab, options: [['policies', '策略引擎与 Cedar'], ['gateways', 'Gateway：挂载、规则、限流'], ['decisions', '决策日志']] }),
    h(ErrorLine, { error }), data ? h(Body, { data, reload }) : h(Empty, null, '加载中…'))
}

export default { id: 'governance', label: '策略', group: '治理', Page: GovernancePage }
