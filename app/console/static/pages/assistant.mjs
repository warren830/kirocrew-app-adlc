// 助手: the architect assistant. A person describes the agent they need; the assistant asks, reads the workspace
// (knowledge bases, skills, Gateways, models) and proposes numbered revisions of an agent specification with the
// behaviour contracts that will verify it. Approving an exact revision creates the agent, saves its contract set and
// starts its verification; every step is shown here with links to the agent and the verification.
import React from 'react'
import { Plus, Send, RefreshCw, Trash2 } from 'lucide-react'
import { call, events, useCtx, listOf, cx, Card, Button, Field, Input, TextArea, Select, Chip, Note, Empty, ErrorLine, Table, useAction, Tabs, JOB_TONE,
  JOB_LABEL } from '../ui.mjs'

const { useState, useEffect, useRef, useCallback, createElement: h, Fragment } = React
const base = (wid) => `/workspaces/${wid}/assistant`
const MODELS = [['us.anthropic.claude-opus-5-5', '助手用 Claude Opus 5.5'], ['us.anthropic.claude-sonnet-5-5', '助手用 Claude Sonnet 5.5（更快）']]
const STATUS = { draft: ['草稿', 'ok'], invalid: ['无效', 'bad'], superseded: ['已被取代', ''], approved: ['已批准', 'info'] }
const APPROVAL = { running: ['进行中', 'info'], succeeded: ['完成', 'ok'], failed: ['失败', 'bad'], interrupted: ['被中断', 'warn'] }
const STEP = { done: ['完成', 'ok'], running: ['进行中', 'info'], failed: ['失败', 'bad'] }
const KIND = { answer: ['回答', 'info'], refusal: ['拒绝', 'warn'], escalation: ['转人工', 'warn'], tools: ['工具', ''], boundary: ['不能说', 'warn'] }
const CHECKS = [['mustMention', '必须提到'], ['mustMentionAnyOf', '每组至少提到一个'], ['mustNotMention', '不能提到'], ['requiredTools', '必须调用'],
  ['forbiddenTools', '不能调用'], ['shouldRefuse', '应当拒绝'], ['shouldEscalate', '应当转人工']]
const TOOL_LABEL = { list_knowledge_bases: '查看知识库列表', describe_knowledge_base: '查看知识库的文档', search_knowledge_base: '检索知识库',
  list_skills: '查看技能库', read_skill: '读技能', list_gateways: '查看 Gateway', describe_gateway: '查看 Gateway 的工具', list_agents: '查看现有 Agent',
  list_models: '查看可用模型', propose_agent: '提交提案' }
const EXAMPLE = '我需要一个回答会员积分问题的客服 Agent：积分怎么获得、多久过期、推荐奖励什么时候到账。只根据知识库回答，查不到就说不知道；不能承诺补发积分，也不能查别人的积分。'
const when = (s) => (s || '').replace('T', ' ').slice(0, 19)

const CSS = `
.cs-as-cols{display:grid;grid-template-columns:240px minmax(0,1fr);gap:14px;align-items:start}
.cs-as-list a{display:block;padding:7px 8px;border-radius:6px;cursor:pointer;margin:1px 0;color:inherit}
.cs-as-list a:hover{background:#f0f4f8}.cs-as-list a.on{background:#e6f6ff}
.cs-as-chat{max-height:58vh;overflow-y:auto;padding:2px 2px 8px}
.cs-as-tool{font-size:12.5px;color:#627d98;margin:3px 0 3px 4px}
.cs-as-md{white-space:normal}.cs-as-md p{margin:4px 0;white-space:pre-wrap}.cs-as-md ul,.cs-as-md ol{margin:4px 0;padding-left:22px}
.cs-as-md .cs-as-h{font-weight:700;margin:8px 0 2px}.cs-as-md code{background:#e4e7eb;padding:0 4px;border-radius:4px;font-size:12.5px}
.cs-as-md table{border-collapse:collapse;margin:6px 0}.cs-as-md td,.cs-as-md th{border:1px solid #d9e2ec;padding:3px 6px;font-size:13px;text-align:left}
.cs-as-diff div{white-space:pre-wrap}
`

function kindOf(exp) {
  if (exp.shouldRefuse === true) return 'refusal'
  if (exp.shouldEscalate === true) return 'escalation'
  const keys = Object.keys(exp).filter((k) => exp[k] !== false && exp[k] != null)
  if (keys.length && keys.every((k) => k === 'requiredTools' || k === 'forbiddenTools')) return 'tools'
  if (keys.length && keys.every((k) => k === 'mustNotMention' || k === 'forbiddenTools')) return 'boundary'
  return 'answer'
}

function checkText(key, value) {
  if (key === 'mustMentionAnyOf') return listOf(value).map((g) => listOf(g).join(' / ')).join('；')
  if (Array.isArray(value)) return value.join('、')
  return value === true ? '是' : value === false ? '否' : String(value)
}

// -- a small Markdown renderer (headings, lists, bold, code, tables, code blocks), React elements only ---------------------

function inline(text) {
  const out = []
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g
  let last = 0
  let m
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index))
    out.push(m[0].startsWith('**') ? h('b', { key: out.length }, m[0].slice(2, -2)) : h('code', { key: out.length }, m[0].slice(1, -1)))
    last = m.index + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

const LIST = /^\s*([-*•]|\d+[.)])\s+/
const BLOCK = /^(#{1,4}\s|\s*([-*•]|\d+[.)])\s|\s*\||```)/

function Markdown({ text }) {
  const lines = String(text || '').split('\n')
  const blocks = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (/^```/.test(line)) {
      const code = []
      for (i += 1; i < lines.length && !/^```/.test(lines[i]); i += 1) code.push(lines[i])
      i += 1
      blocks.push(h('pre', { key: blocks.length, className: 'cs-pre' }, code.join('\n')))
    } else if (/^\s*\|/.test(line)) {
      const rows = []
      for (; i < lines.length && /^\s*\|/.test(lines[i]); i += 1) if (!/^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i])) rows.push(lines[i].trim().replace(/^\||\|$/g, '').split('|'))
      blocks.push(h('table', { key: blocks.length }, h('tbody', null, rows.map((r, j) => h('tr', { key: j }, r.map((c, k) => h(j ? 'td' : 'th', { key: k }, inline(c.trim()))))))))
    } else if (/^#{1,4}\s/.test(line)) {
      blocks.push(h('div', { key: blocks.length, className: 'cs-as-h' }, inline(line.replace(/^#+\s*/, ''))))
      i += 1
    } else if (LIST.test(line)) {
      const items = []
      const ordered = /^\s*\d/.test(line)
      for (; i < lines.length && LIST.test(lines[i]); i += 1) items.push(lines[i].replace(LIST, ''))
      blocks.push(h(ordered ? 'ol' : 'ul', { key: blocks.length }, items.map((x, j) => h('li', { key: j }, inline(x)))))
    } else if (!line.trim()) {
      i += 1
    } else {
      const para = []
      for (; i < lines.length && lines[i].trim() && !BLOCK.test(lines[i]); i += 1) para.push(lines[i])
      blocks.push(h('p', { key: blocks.length }, inline(para.join('\n'))))
    }
  }
  return h('div', { className: 'cs-as-md' }, blocks)
}

// -- the transcript ------------------------------------------------------------------------------------------------------

function ToolLine({ m }) {
  const label = TOOL_LABEL[m.name] || m.name
  return h('div', { className: 'cs-as-tool' }, `· ${label}`, m.input && m.name !== 'propose_agent' ? h('span', { className: 'cs-mono' }, ` ${String(m.input).slice(0, 140)}`) : null,
    m.name === 'propose_agent' && m.input ? ` ${m.input}` : null,
    m.pending ? h('span', { className: 'cs-spin', style: { marginLeft: 6 } }) : m.summary ? ` → ${m.summary}` : null,
    listOf(m.errors).length ? h('div', { className: 'cs-err', style: { marginTop: 2 } }, m.errors.slice(0, 8).map((e) => `· ${e}`).join('\n')
      + (m.errors.length > 8 ? `\n…另 ${m.errors.length - 8} 条` : '')) : null)
}

function Row({ m, me, onRevision }) {
  if (m.role === 'user') return h('div', { className: 'cs-turn cs-user' }, h('div', { className: 'cs-bubble' }, m.text),
    m.author && me && m.author !== me.username ? h('div', { className: 'cs-mut' }, m.author) : null)
  if (m.role === 'assistant') return h('div', { className: 'cs-turn cs-agent' }, h('div', { className: 'cs-bubble' }, h(Markdown, { text: m.text })))
  if (m.role === 'tool') return h(ToolLine, { m })
  if (m.role === 'error') return h('div', { className: 'cs-err' }, `这一轮没有完成：${m.text}`)
  if (m.role === 'meta') return h('div', { className: 'cs-mut', style: { margin: '2px 0 10px 4px' } },
    `${m.seconds}s · 模型 ${m.modelCalls} 次 · 输入 ${(m.usage || {}).inputTokens || 0}（缓存 ${(m.usage || {}).cacheReadInputTokens || 0}）· 输出 ${(m.usage || {}).outputTokens || 0} tokens · `
    + `${m.awsCalls} 次 AWS 调用，全部只读`)
  if (m.role === 'note' && m.kind === 'proposal') return h('div', { className: 'cs-as-tool' }, h('a', { className: 'cs-click', onClick: () => onRevision(m.revision) },
    h(Chip, { tone: m.status === 'draft' ? 'ok' : 'bad' }, `第 ${m.revision} 版 · ${m.status === 'draft' ? '有效' : '无效'}`)), ' 看下面的提案')
  if (m.role === 'note') return h('div', { className: 'cs-as-tool' }, String(m.text || '').replace(/^\[控制台\]\s*/, ''))
  return null
}

function Transcript({ conv, live, me, onRevision }) {
  const bottom = useRef(null)
  const count = listOf(conv.messages).length + (live ? live.items.length + 1 : 0)
  useEffect(() => { if (bottom.current) bottom.current.scrollIntoView({ block: 'nearest' }) }, [count, live && live.thinking])
  const rows = listOf(conv.messages)
  return h('div', { className: 'cs-as-chat' },
    !rows.length && !live ? h(Empty, null, '说说你需要什么样的 Agent：给谁用、要回答什么、不能做什么、什么时候转人工。') : null,
    rows.map((m, i) => h(Row, { key: i, m, me, onRevision })),
    live ? h(Fragment, null, h(Row, { m: { role: 'user', text: live.user } }),
      live.items.map((m, i) => h(Row, { key: `l${i}`, m, me, onRevision })),
      live.error ? h('div', { className: 'cs-err' }, live.error) : null,
      live.thinking && !live.error ? h('div', { className: 'cs-mut' }, h('span', { className: 'cs-spin' }), ' 助手在想…') : null) : null,
    conv.inFlight && !live ? h(Note, null, h('span', { className: 'cs-spin' }), ` 助手正在回复第 ${conv.inFlight.turn} 轮（${conv.inFlight.by}，自动刷新）`) : null,
    h('div', { ref: bottom }))
}

// -- the proposal -------------------------------------------------------------------------------------------------------

function Spec({ p }) {
  const c = p.content || {}
  const b = p.bindings || {}
  const kbs = Object.fromEntries(listOf(b.knowledgeBases).map((k) => [k.id, k]))
  const pinned = Object.fromEntries(listOf(b.skills).map((s) => [s.name, s]))
  const gws = Object.fromEntries(listOf(b.gateways).map((g) => [g.id, g]))
  const skillName = (s) => (typeof s === 'string' ? s : (s || {}).name)
  return h(Fragment, null,
    h(Table, { head: ['项', '值'], rows: [
      ['名称', h('code', null, c.name)], ['模型', h('code', null, c.model)],
      ['知识库', listOf(c.knowledgeBases).map((k) => (kbs[k] ? `${k}（${kbs[k].name}）` : k)).join('、') || '无'],
      ['技能', listOf(c.skills).map((s) => `${skillName(s)} ${(pinned[skillName(s)] || {}).version || (s || {}).version || ''}`.trim()).join('、') || '无'],
      ['Gateway', listOf(c.gateways).map((g) => `${g.toolName} → ${(gws[g.gatewayId] || {}).name || g.gatewayId}`).join('、') || '无'],
      ['L1 看到的工具名', listOf(b.toolNames).length ? h('span', { className: 'cs-row' }, b.toolNames.map((t) => h(Chip, { key: t }, t))) : '—'],
      ['验证', `${c.verificationRounds || 2} 轮（每轮把每条契约问一遍）`],
      ['概述', c.summary || '—'],
      ['假设与待定', listOf(c.assumptions).length ? h('ul', { style: { margin: 0, paddingLeft: 18 } }, c.assumptions.map((a, i) => h('li', { key: i }, a))) : '—'],
    ].map((cells, i) => ({ key: i, cells })) }),
    h('div', { className: 'cs-mut', style: { marginTop: 8 } }, '系统 Prompt'),
    h('pre', { className: 'cs-pre', style: { whiteSpace: 'pre-wrap' } }, c.systemPrompt || ''))
}

function Contracts({ p }) {
  return h(Table, { head: ['契约', '类型', '问题', '检查', '来源'], rows: listOf((p.content || {}).contracts).map((x, i) => {
    const exp = (x && x.expected) || {}
    const [label, tone] = KIND[kindOf(exp)]
    return { key: `${x && x.id}-${i}`, cells: [h('span', null, h('b', null, x.id), x.label ? h('div', { className: 'cs-mut' }, x.label) : null), h(Chip, { tone }, label), x.query,
      h('div', null, CHECKS.filter(([k]) => k in exp).map(([k, name]) => h('div', { key: k }, h('span', { className: 'cs-mut' }, `${name}：`), checkText(k, exp[k])))),
      h('span', { className: 'cs-mut' }, x.source || '—')] }
  }), empty: '没有契约。' })
}

function Diff({ text }) {
  if (!text) return h(Empty, null, '这是第一版，没有可以对比的。')
  return h('pre', { className: 'cs-pre cs-as-diff' }, text.split('\n').map((line, i) => h('div', { key: i, style: line.startsWith('+') && !line.startsWith('+++')
    ? { background: '#1f4f2f' } : line.startsWith('-') && !line.startsWith('---') ? { background: '#5c2323' } : undefined }, line || ' ')))
}

function Steps({ steps }) {
  const result = (s) => {
    const r = s.result || {}
    if (s.status === 'failed') return h('span', { className: 'cs-err' }, s.error)
    if (s.key === 'harness') return h('span', { className: 'cs-mono' }, `${r.id || ''}${r.role ? ` · 角色 ${String(r.role).split('/').pop()}` : ''}`)
    if (s.key.startsWith('kb:')) return listOf(r.tools).join('、')
    if (s.key.startsWith('skill:')) return listOf(r.skills).join('、')
    if (s.key === 'contracts') return h('span', { className: 'cs-mono' }, r.id || '')
    if (s.key === 'verification') return h('span', { className: 'cs-mono' }, r.id || '')
    return r.status || ''
  }
  return h(Table, { head: ['步骤', '状态', '结果'], rows: listOf(steps).map((s) => ({ key: s.key, cells: [s.label,
    h(Chip, { tone: (STEP[s.status] || [])[1] }, (STEP[s.status] || [s.status])[0]), result(s)] })) })
}

function Verification({ info, go }) {
  if (!info) return null
  const r = info.result
  return h(Card, { title: '验证', extra: h('span', { className: 'cs-row' }, h(Chip, { tone: JOB_TONE[info.status] }, JOB_LABEL[info.status] || info.status),
    h(Button, { onClick: () => go('evaluation') }, '在评估页看')) },
  r ? h(Fragment, null,
    h(Note, { tone: r.robust ? 'ok' : 'warn' }, `${r.holding}/${listOf(r.contracts).length} 条契约在 ${r.repeat} 轮里每轮都通过：${r.robust ? '稳定' : '不稳定'}（${Math.round((r.seconds || 0) / 6) / 10} 分钟）`),
    h(Table, { head: ['契约', '通过轮次', '没过的检查', '提示'], rows: listOf(r.contracts).map((c) => ({ key: c.id, cells: [h('span', null, h('b', null, c.id),
      c.label && c.label !== c.id ? h('div', { className: 'cs-mut' }, c.label) : null), h(Chip, { tone: c.holds ? 'ok' : 'bad' }, `${c.passes}/${c.rounds}`),
      listOf(c.failed).join('、') || '—', c.note ? h('span', { className: 'cs-mut' }, c.note) : '—'] })) }),
    listOf(r.tools).length ? h('div', { className: 'cs-mut' }, `L1 在会话里看到的工具：${r.tools.join('、')}`) : null)
    : h(Fragment, null, h('div', { className: 'cs-mut' }, info.status === 'running' ? '每轮把每条契约在新会话里问一遍，等轨迹落盘后用 L1 判断（几分钟）。' : ''),
      listOf(info.log).length ? h('pre', { className: 'cs-pre' }, info.log.join('\n')) : null),
  info.error ? h('div', { className: 'cs-err' }, info.error) : null)
}

function Approval({ p, onResume, busy, go, setSelected }) {
  const a = p.approval
  const [label, tone] = APPROVAL[a.effectiveStatus] || [a.effectiveStatus, '']
  const open = (page) => { setSelected({ kind: 'harness', id: a.agent.id }); go(page) }
  return h(Fragment, null,
    h(Card, { title: `批准第 ${p.revision} 版`, extra: h('span', { className: 'cs-row' }, h(Chip, { tone }, label),
      a.agent ? h(Button, { onClick: () => open('agent') }, `打开 ${a.agent.name}`) : null,
      a.agent && a.effectiveStatus === 'succeeded' ? h(Button, { onClick: () => open('chat') }, '和它对话') : null,
      ['failed', 'interrupted'].includes(a.effectiveStatus) ? h(Button, { kind: 'pri', onClick: onResume, busy }, '从没完成的那步继续') : null) },
    h('div', { className: 'cs-mut' }, `${a.approvedBy} 于 ${when(a.approvedAt)} 批准${a.resumedBy ? ` · ${a.resumedBy} 继续执行` : ''}${a.finishedAt ? ` · ${when(a.finishedAt)} 结束` : ''}`),
    h(Steps, { steps: a.steps }),
    a.effectiveStatus === 'running' && a.jobInfo && listOf(a.jobInfo.log).length ? h('pre', { className: 'cs-pre' }, a.jobInfo.log.slice(-4).join('\n')) : null,
    a.error ? h('div', { className: 'cs-err' }, a.error) : null),
    h(Verification, { info: a.verificationInfo, go }))
}

const csv = (s) => String(s || '').split(/[\s,，、]+/).map((x) => x.trim()).filter(Boolean)

function EditForm({ p, latest, onSaved, onCancel }) {
  const { wid } = useCtx()
  const c = p.content && typeof p.content === 'object' ? p.content : {}
  const skillText = listOf(c.skills).map((s) => (typeof s === 'string' ? s : s.version ? `${s.name}@${s.version}` : s.name)).join(', ')
  const rest = {}
  for (const k of ['gateways', 'l1', 'assumptions']) if (c[k] !== undefined) rest[k] = c[k]
  const [form, setForm] = useState({ name: c.name || '', model: c.model || '', systemPrompt: c.systemPrompt || '', knowledgeBases: listOf(c.knowledgeBases).join(', '),
    skills: skillText, rounds: String(c.verificationRounds || 2), summary: c.summary || '', contracts: JSON.stringify(c.contracts || [], null, 1),
    rest: JSON.stringify(rest, null, 1) })
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const save = () => run('save', () => {
    const content = { ...JSON.parse(form.rest || '{}'), name: form.name.trim(), model: form.model.trim(), systemPrompt: form.systemPrompt,
      knowledgeBases: csv(form.knowledgeBases), skills: csv(form.skills).map((s) => (s.includes('@') ? { name: s.split('@')[0], version: s.split('@')[1] } : s)),
      verificationRounds: Number(form.rounds), contracts: JSON.parse(form.contracts || '[]') }
    if (form.summary.trim()) content.summary = form.summary
    return call('POST', `${base(wid)}/conversations/${p.conversation}/proposals`, { base: latest, content })
  }, (r) => `已存为第 ${r.revision} 版${r.status === 'draft' ? '' : '（无效，看错误）'}`).then((r) => { if (r) onSaved(r.revision) })
  return h(Card, { title: `编辑（基于第 ${p.revision} 版，存为新的一版）`, extra: h('span', { className: 'cs-row' }, h(Button, { onClick: onCancel }, '取消'),
    h(Button, { kind: 'pri', onClick: save, busy: busy === 'save' }, '存为新版本')) },
  h('div', { className: 'cs-grid' },
    h(Field, { label: '名称（Harness 名）' }, h(Input, { value: form.name, onChange: set('name'), mono: true })),
    h(Field, { label: '模型' }, h(Input, { value: form.model, onChange: set('model'), mono: true })),
    h(Field, { label: '知识库 id（逗号分隔）' }, h(Input, { value: form.knowledgeBases, onChange: set('knowledgeBases'), mono: true })),
    h(Field, { label: '技能（逗号分隔；名字@版本 固定版本）' }, h(Input, { value: form.skills, onChange: set('skills'), mono: true })),
    h(Field, { label: '验证轮数' }, h(Select, { value: form.rounds, onChange: set('rounds'), options: [['1', '1'], ['2', '2'], ['3', '3'], ['5', '5']] }))),
  h(Field, { label: '概述' }, h(TextArea, { value: form.summary, onChange: set('summary'), rows: 2 })),
  h(Field, { label: '系统 Prompt' }, h(TextArea, { value: form.systemPrompt, onChange: set('systemPrompt'), rows: 10 })),
  h(Field, { label: '行为契约（JSON：id、label、source、query、expected）', hint: 'expected：mustMention、mustMentionAnyOf、mustNotMention、requiredTools、forbiddenTools、shouldRefuse、shouldEscalate' },
    h(TextArea, { value: form.contracts, onChange: set('contracts'), rows: 14, mono: true })),
  h(Field, { label: '其他（JSON：gateways、l1、assumptions）' }, h(TextArea, { value: form.rest, onChange: set('rest'), rows: 4, mono: true })),
  h(Note, null, '保存时按助手的同一套规则检查（模型、知识库、技能、工具名、契约）；没通过也会存下来，助手下一轮会看到错误。'),
  h(ErrorLine, { error }))
}

function Proposal({ conv, revision, setRevision, onChanged, go, setSelected }) {
  const { wid } = useCtx()
  const [tab, setTab] = useState('spec')
  const [editing, setEditing] = useState(false)
  const { busy, run } = useAction()
  const proposals = listOf(conv.proposals)
  if (!proposals.length) return null
  const latest = proposals[proposals.length - 1]
  const p = proposals.find((x) => x.revision === revision) || latest
  const [statusLabel, statusTone] = STATUS[p.status] || [p.status, '']
  const c = p.content || {}
  const running = proposals.some((x) => x.approval && x.approval.effectiveStatus === 'running')
  const approvable = p.revision === latest.revision && p.status === 'draft' && !conv.inFlight && !running
  const approve = () => {
    const kbs = listOf((p.bindings || {}).knowledgeBases).map((k) => `${k.name}（${k.id}）`).join('、') || '无'
    const skills = listOf((p.bindings || {}).skills).map((s) => `${s.name} ${s.version}`).join('、') || '无'
    const text = `批准第 ${p.revision} 版（哈希 ${p.hash.slice(0, 12)}）？\n\n控制台会：\n· 创建 Harness ${c.name}（${c.model}）和它的执行角色 ${String(c.name).slice(0, 44)}-console-harness\n`
      + `· 挂上知识库：${kbs}\n· 用上技能：${skills}\n· 保存 ${listOf(c.contracts).length} 条行为契约，并开始 ${c.verificationRounds || 2} 轮验证\n\n批准前会重新核对这些资源；和审阅时不一样就不执行。`
    if (!window.confirm(text)) return
    run('approve', () => call('POST', `${base(wid)}/conversations/${conv.id}/approve`, { revision: p.revision, hash: p.hash }),
      (r) => (r.recorded ? '这一版已经批准过：显示记录的结果' : `已批准第 ${p.revision} 版，开始创建`)).then(onChanged)
  }
  const resume = () => run('resume', () => call('POST', `${base(wid)}/conversations/${conv.id}/approve`, { revision: p.revision, hash: p.hash, resume: true }),
    '从没完成的那步继续').then(onChanged)
  return h(Fragment, null,
    h(Card, { title: `提案 · 第 ${p.revision} 版`, extra: h('span', { className: 'cs-row' },
      h(Select, { value: String(p.revision), onChange: (v) => setRevision(Number(v)), options: proposals.slice().reverse().map((x) => [String(x.revision),
        `第 ${x.revision} 版 · ${(STATUS[x.status] || [x.status])[0]}${x.source === 'edit' ? ' · 手动编辑' : ''}`]) }),
      h(Chip, { tone: statusTone }, statusLabel),
      typeof p.content === 'object' && p.content ? h(Button, { onClick: () => setEditing(true) }, '编辑') : null,
      approvable ? h(Button, { kind: 'pri', onClick: approve, busy: busy === 'approve' }, `批准第 ${p.revision} 版`) : null) },
    h('div', { className: 'cs-mut' }, `${p.source === 'model' ? '助手提出' : `${p.createdBy} 手动编辑`} · ${when(p.createdAt)} · 哈希 `, h('span', { className: 'cs-mono' }, p.hash.slice(0, 16))),
    listOf(p.errors).length ? h('div', { className: 'cs-err' }, `没通过检查，不能批准：\n${p.errors.map((e) => `· ${e}`).join('\n')}`) : null,
    listOf(p.warnings).length ? h(Note, { tone: 'warn' }, h('div', null, '提醒：'), p.warnings.map((w, i) => h('div', { key: i }, `· ${w}`))) : null,
    p.status === 'draft' && p.revision === latest.revision && !p.approval ? h(Note, null, '批准前什么都不会创建。批准时控制台按这一版的哈希重新核对它引用的知识库、技能（含 S3 上的内容）和 Gateway，变了就拒绝。') : null,
    h(Tabs, { value: tab, onChange: setTab, options: [['spec', '规格'], ['contracts', `行为契约（${listOf(c.contracts).length}）`], ['diff', '与上一版对比'], ['text', '全文']] }),
    tab === 'spec' ? h(Spec, { p }) : tab === 'contracts' ? h(Contracts, { p }) : tab === 'diff' ? h(Diff, { text: p.diff }) : h('pre', { className: 'cs-pre', style: { whiteSpace: 'pre-wrap' } }, p.text)),
  editing ? h(EditForm, { p: { ...p, conversation: conv.id }, latest: latest.revision, onCancel: () => setEditing(false),
    onSaved: (r) => { setEditing(false); setRevision(r); onChanged() } }) : null,
  p.approval ? h(Approval, { p, onResume: resume, busy: busy === 'resume', go, setSelected }) : null)
}

// -- a conversation ------------------------------------------------------------------------------------------------------

function Conversation({ cid, onChanged, onDeleted, go, setSelected }) {
  const { wid, me } = useCtx()
  const [conv, setConv] = useState(null)
  const [live, setLive] = useState(null)
  const [message, setMessage] = useState('')
  const [revision, setRevision] = useState(null)
  const [error, setError] = useState(null)
  const { busy, run } = useAction()
  const load = useCallback(() => call('GET', `${base(wid)}/conversations/${cid}`).then((c) => { setConv(c); setError(null); return c }).catch(setError), [wid, cid])
  useEffect(() => { setConv(null); setRevision(null); load() }, [load])
  const watching = conv && !live && (conv.inFlight || listOf(conv.proposals).some((p) => p.approval && (p.approval.effectiveStatus === 'running'
    || (p.approval.verificationInfo && p.approval.verificationInfo.status === 'running'))))
  useEffect(() => {
    if (!watching) return undefined
    const timer = setTimeout(load, 3000)
    return () => clearTimeout(timer)
  }, [watching, conv])
  if (!conv) return h(Card, null, h(ErrorLine, { error }), h(Empty, null, '加载中…'))
  const send = async () => {
    const text = message.trim()
    if (!text || live) return
    setMessage(''); setError(null)
    setLive({ user: text, items: [], thinking: true })
    const patch = (fn) => setLive((l) => (l ? fn({ ...l, items: l.items.slice() }) : l))
    try {
      await events(`${base(wid)}/conversations/${cid}/turns`, { message: text }, (e) => {
        if (e.type === 'thinking') patch((l) => ({ ...l, thinking: true }))
        else if (e.type === 'text') patch((l) => ({ ...l, thinking: false, items: [...l.items, { role: 'assistant', text: e.text }] }))
        else if (e.type === 'tool') patch((l) => ({ ...l, thinking: false, items: [...l.items, { role: 'tool', name: e.name, input: e.input, pending: true }] }))
        else if (e.type === 'toolResult') {
          patch((l) => {
            const k = l.items.map((x) => x.role === 'tool' && x.pending && x.name === e.name).lastIndexOf(true)
            if (k >= 0) l.items[k] = { ...l.items[k], pending: false, ok: e.ok, summary: e.summary, errors: e.errors }
            return { ...l, thinking: true }
          })
        } else if (e.type === 'proposal') setRevision(e.revision)
        else if (e.type === 'error') patch((l) => ({ ...l, thinking: false, error: e.error }))
        else if (e.type === 'done') patch((l) => ({ ...l, thinking: false }))
      })
    } catch (err) {
      setError(err)
      setMessage(text)
    }
    await load()
    setLive(null)
    onChanged()
  }
  const share = () => run('share', () => call('POST', `${base(wid)}/conversations/${cid}/share`, { shared: !conv.shared }),
    conv.shared ? '已取消共享' : '已共享给这个工作区的成员').then(() => { load(); onChanged() })
  const remove = () => window.confirm(`删除这个对话？${listOf(conv.agents).length ? `它创建的 ${conv.agents.join('、')} 会保留。` : ''}`)
    && run('del', () => call('DELETE', `${base(wid)}/conversations/${cid}`), '已删除').then((r) => { if (r) onDeleted() })
  const keyDown = (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send() } }
  return h(Fragment, null,
    h(Card, { title: conv.title || '新对话', extra: h('span', { className: 'cs-row' },
      conv.shared ? h(Chip, { tone: 'info' }, `共享${conv.sharedBy ? `（${conv.sharedBy}）` : ''}`) : null,
      conv.mine ? null : h(Chip, null, `${conv.owner} 的对话`),
      h(Chip, null, (MODELS.find(([m]) => m === conv.model) || [null, conv.model])[1].replace('助手用 ', '')),
      me && me.role === 'admin' && (conv.mine || conv.shared) ? h(Button, { onClick: share, busy: busy === 'share' }, conv.shared ? '取消共享' : '共享给工作区') : null,
      h(Button, { icon: RefreshCw, onClick: load, title: '刷新' }),
      conv.mine ? h(Button, { kind: 'danger', icon: Trash2, onClick: remove, busy: busy === 'del', title: '删除对话' }) : null) },
    h(Transcript, { conv, live, me, onRevision: setRevision }),
    h('div', { className: 'cs-row', style: { alignItems: 'flex-end' } },
      h('textarea', { className: 'cs-input', rows: 3, value: message, placeholder: '回答助手的问题，或者说要怎么改（Ctrl+Enter 发送）', style: { flex: 1 },
        onChange: (e) => setMessage(e.target.value), onKeyDown: keyDown }),
      h(Button, { kind: 'pri', icon: Send, onClick: send, busy: !!live, disabled: !message.trim() || !!conv.inFlight }, '发送')),
    !listOf(conv.messages).length && !live ? h('div', { className: 'cs-row', style: { marginTop: 6 } }, h('span', { className: 'cs-mut' }, '例如：'),
      h('a', { className: 'cs-click cs-mut', onClick: () => setMessage(EXAMPLE) }, EXAMPLE.slice(0, 46) + '…')) : null,
    h(ErrorLine, { error })),
  h(Proposal, { conv, revision, setRevision, onChanged: () => { load(); onChanged() }, go, setSelected }))
}

// -- the page ------------------------------------------------------------------------------------------------------------

function AssistantPage({ go, setSelected }) {
  const { wid, workspace } = useCtx()
  const [list, setList] = useState(null)
  const [open, setOpen] = useState(null)
  const [model, setModel] = useState(MODELS[0][0])
  const [error, setError] = useState(null)
  const { busy, run } = useAction()
  const load = () => call('GET', `${base(wid)}/conversations`).then((r) => { setList(listOf(r.conversations)); setError(null) }).catch(setError)
  useEffect(() => { setList(null); setOpen(null); if (wid) load() }, [wid])
  if (!workspace) return h(Empty, null, '先在「设置 → 工作区」添加一个 AWS 账号和区域。')
  const create = () => run('new', () => call('POST', `${base(wid)}/conversations`, { model })).then((r) => { if (r) { setOpen(r.id); load() } })
  return h(Fragment, null, h('style', null, CSS),
    h('h1', null, '助手'),
    h(Note, null, '说说你需要什么样的 Agent。助手会问清楚，查看这个工作区的知识库、技能、Gateway 和模型，然后提出一份可审阅的规格和行为契约（必须说什么、不能说什么、该拒绝什么、该用哪些工具），按版本编号。'
      + '对话只读，不改动 AWS；只有你批准某一版，控制台才创建 Agent、挂知识库、用上技能、保存契约集，并马上开始验证。'),
    h('div', { className: 'cs-as-cols' },
      h('div', null,
        h(Card, { title: '新对话' }, h(Select, { value: model, onChange: setModel, options: MODELS }),
          h('div', { style: { marginTop: 8 } }, h(Button, { kind: 'pri', icon: Plus, onClick: create, busy: busy === 'new' }, '开始'))),
        h(Card, { title: '对话', extra: h(Button, { icon: RefreshCw, onClick: load, title: '刷新' }) }, h(ErrorLine, { error }),
          list === null ? h(Empty, null, '加载中…') : list.length ? h('div', { className: 'cs-as-list' }, list.map((x) => h('a', { key: x.id, className: cx(open === x.id && 'on'),
            onClick: () => setOpen(x.id) }, h('div', null, h('b', null, x.title || '新对话')),
            h('div', { className: 'cs-row', style: { gap: 4, marginTop: 2 } },
              x.revision ? h(Chip, { tone: (STATUS[x.proposalStatus] || [])[1] }, `第 ${x.revision} 版 · ${(STATUS[x.proposalStatus] || [x.proposalStatus])[0]}`) : null,
              x.shared ? h(Chip, { tone: 'info' }, '共享') : null, listOf(x.agents).map((a) => h(Chip, { key: a, tone: 'ok' }, a))),
            h('div', { className: 'cs-mut' }, `${x.mine ? '' : `${x.owner} · `}${when(x.updatedAt)}`)))) : h(Empty, null, '还没有对话。'))),
      open ? h('div', null, h(Conversation, { key: open, cid: open, onChanged: load, onDeleted: () => { setOpen(null); load() }, go, setSelected }))
        : h(Card, null, h(Empty, null, '选一个对话，或开始一个新对话。'))))
}

export default { id: 'assistant', label: '助手', group: '构建', Page: AssistantPage }
