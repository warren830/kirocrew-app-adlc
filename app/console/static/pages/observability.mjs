// 观测: an agent's AgentCore metrics, its recent sessions, and one session's trace (spans, tokens, the conversation).
import React from 'react'
import { call, useCtx, listOf, Card, Button, Select, Chip, Empty, ErrorLine, Table, useAction, Note, Field, Input, TextArea } from '../ui.mjs'

const { useState, useEffect, createElement: h, Fragment } = React

const LABEL = { Invocations: '调用', Sessions: '会话', Latency: '平均延迟 ms', Errors: '错误', SystemErrors: '系统错误', UserErrors: '用户错误', Throttles: '限流' }

function Spark({ points }) {
  const values = listOf(points).map((p) => p.v)
  if (values.length < 2) return null
  const max = Math.max(...values, 1)
  const d = values.map((v, i) => `${i ? 'L' : 'M'}${(i / (values.length - 1)) * 160},${36 - (v / max) * 32}`).join(' ')
  return h('svg', { width: 160, height: 38, viewBox: '0 0 160 38' }, h('path', { d, fill: 'none', stroke: '#2680c2', strokeWidth: 1.5 }))
}

function Metrics({ data }) {
  const entries = Object.entries(data.series || {})
  if (!entries.length) return h(Empty, null, '这段时间没有指标（Agent 没被调用，或指标还没到）。')
  return h('div', { className: 'cs-grid' }, entries.map(([name, s]) => h(Card, { key: name, title: name.startsWith('eval:') ? `在线评估 ${name.slice(5)}` : (LABEL[name] || name) },
    h('div', { className: 'cs-big' }, s.total ?? '—'), h('div', { className: 'cs-mut' }, s.stat === 'Sum' ? '合计' : '平均'), h(Spark, { points: s.points }))))
}

function Trace({ trace }) {
  const width = Math.max(1, trace.durationMs)
  return h(Fragment, null,
    h('div', { className: 'cs-mut' }, `${trace.spans.length} 个 span · ${trace.durationMs} ms · 输入 ${trace.inputTokens} / 输出 ${trace.outputTokens} tokens`),
    h(Table, { head: ['Span', '时间线', '耗时', '模型 / 工具', 'Tokens'], rows: trace.spans.map((s) => ({ key: s.spanId, cells: [
      h('span', { className: 'cs-mono' }, s.name),
      h('div', { style: { position: 'relative', width: 220, height: 10, background: '#f0f4f8', borderRadius: 3 } },
        h('div', { style: { position: 'absolute', left: `${(s.offsetMs / width) * 100}%`, width: `${Math.max(1, (s.durationMs / width) * 100)}%`, height: 10,
          background: s.status === 'ERROR' ? '#e12d39' : '#2680c2', borderRadius: 3 } })),
      `${s.durationMs} ms`, s.tool || s.model || '—', s.inputTokens ? `${s.inputTokens} / ${s.outputTokens}` : '—'] })) }),
    listOf(trace.conversation).length ? h(Card, { title: '对话' }, trace.conversation.map((m, i) => h('div', { key: i, className: 'cs-note' },
      h(Chip, { tone: m.role === 'user' ? 'info' : 'ok' }, m.role === 'user' ? '用户' : 'Agent'), ' ', m.text.slice(0, 600)))) : null)
}

const PANEL = ['Builtin.Faithfulness', 'Builtin.Correctness', 'Builtin.Helpfulness', 'Builtin.Refusal', 'Builtin.GoalSuccessRate']

function ScoreSession({ base, sessionId, hours }) {
  const [picked, setPicked] = useState(PANEL)
  const [extra, setExtra] = useState('')
  const [assertions, setAssertions] = useState('')
  const [tools, setTools] = useState('')
  const [result, setResult] = useState(null)
  const { busy, error, run } = useAction()
  const evaluators = [...picked, ...extra.split(/[\s,，]+/).filter(Boolean)]
  const toggle = (e) => setPicked((list) => (list.includes(e) ? list.filter((x) => x !== e) : [...list, e]))
  const score = () => run('score', async () => setResult(await call('POST', `${base}/sessions/${sessionId}/evaluate?hours=${hours}`, {
    evaluators, assertions: assertions.split('\n').filter((a) => a.trim()), expectedTools: tools.split(/[\s,，]+/).filter(Boolean) })))
  return h(Card, { title: '现在给这个会话打分（AgentCore 评估器，像在线评估那样；不保存、不建资源）',
    extra: h(Button, { kind: 'pri', onClick: score, busy: busy === 'score', disabled: !evaluators.length || evaluators.length > 5 }, '打分') },
    h('div', { className: 'cs-row' }, PANEL.map((e) => h('label', { key: e, className: 'cs-row' }, h('input', { type: 'checkbox', checked: picked.includes(e), onChange: () => toggle(e) }),
      e.replace(/^Builtin\./, '')))),
    h('div', { className: 'cs-grid' },
      h(Field, { label: '其他评估器（ID，最多共 5 个）' }, h(Input, { value: extra, onChange: setExtra, mono: true, placeholder: 'Builtin.Coherence' })),
      h(Field, { label: '它应该调用的工具（可选，给 Trajectory 类评估器）' }, h(Input, { value: tools, onChange: setTools, mono: true }))),
    h(Field, { label: '参考断言（可选，一行一条：给 Correctness / GoalSuccessRate 等要参考答案的评估器）' },
      h(TextArea, { value: assertions, onChange: setAssertions, rows: 2, placeholder: '回答说明积分 24 个月有效' })),
    result ? h(Table, { head: ['评估器', '分数', '标签', '解释'], rows: result.scores.map((s) => ({ key: s.evaluator, cells: [s.evaluator.replace(/^Builtin\./, ''),
      typeof s.value === 'number' ? s.value.toFixed(2) : '—', h(Chip, { tone: s.error ? 'warn' : s.value >= 0.5 ? 'ok' : 'bad' }, s.label || '—'),
      h('span', { className: 'cs-mut' }, `${listOf(s.ignored).length ? `（没用上参考输入：${s.ignored.join('、')}）` : ''}${s.error || s.explanation || ''}`)] })) }) : null,
    result ? h('div', { className: 'cs-mut' }, `${result.records} 条记录${listOf(result.references).length ? '，带参考输入' : '，没有参考输入（和在线评估一样）'}`) : null,
    h(ErrorLine, { error }))
}

function ObservabilityPage({ selected }) {
  const { wid, workspace } = useCtx()
  const [agents, setAgents] = useState([])
  const [target, setTarget] = useState(selected ? `${selected.kind}:${selected.id}` : '')
  const [hours, setHours] = useState('24')
  const [metrics, setMetrics] = useState(null)
  const [sessions, setSessions] = useState(null)
  const [evaluations, setEvaluations] = useState([])
  const [trace, setTrace] = useState(null)
  const { busy, error, run } = useAction()
  useEffect(() => { if (wid) call('GET', `/workspaces/${wid}/agents`).then((r) => setAgents(listOf(r.agents))).catch(() => {}) }, [wid])
  const [kind, id] = target.split(':')
  const load = () => run('load', async () => {
    setTrace(null)
    const base = `/workspaces/${wid}/agents/${kind}/${id}`
    const [m, s] = await Promise.all([call('GET', `${base}/metrics?hours=${hours}`), call('GET', `${base}/sessions?hours=${hours}`)])
    setMetrics(m); setSessions(listOf(s.sessions)); setEvaluations(listOf(s.evaluations))
  })
  useEffect(() => { if (id) load() }, [target, hours])
  if (!workspace) return h(Empty, null, '先添加工作区。')
  const open = (sid) => run('trace', async () => setTrace(await call('GET', `/workspaces/${wid}/agents/${kind}/${id}/sessions/${sid}?hours=${hours}`)))
  return h(Fragment, null,
    h('h1', null, '观测'),
    h(Card, { title: '指标与会话', extra: h('span', { className: 'cs-row' },
      h(Select, { value: target, onChange: setTarget, options: [['', '选一个 Agent'], ...agents.map((a) => [`${a.kind}:${a.id}`, a.name])] }),
      h(Select, { value: hours, onChange: setHours, options: [['3', '3 小时'], ['24', '24 小时'], ['168', '7 天']] }),
      h(Button, { onClick: load, busy: busy === 'load', disabled: !id }, '刷新')) },
    h(ErrorLine, { error }),
    !id ? h(Empty, null, '选一个 Agent。') : metrics ? h(Metrics, { data: metrics }) : h(Empty, null, '加载中…')),
    sessions ? h(Card, { title: `最近的会话（${sessions.length}）` },
      h('div', { className: 'cs-mut' }, evaluations.length ? `在线评估：${evaluations.join('，')}（打分约在会话后 10 分钟出现；红色 = 没过）` : '这个 Agent 没有在线评估：会话没有打分（「评估」页可以据验证结果创建）。'),
      h(Table, { head: ['会话', '记录', '开始', '结束', '在线评估打分'], rows: sessions.map((s) => ({ key: s.sessionId,
        onClick: () => open(s.sessionId), cells: [h('span', { className: 'cs-mono' }, s.sessionId), s.records, (s.first || '').slice(0, 19), (s.last || '').slice(0, 19),
          Object.keys(s.scores || {}).length ? h('span', { className: 'cs-row' }, Object.entries(s.scores).map(([name, sc]) => h(Chip, { key: name, tone: sc.failed ? 'bad' : 'ok',
            title: `${name}: ${sc.label || ''}` }, `${name.replace(/^Builtin\./, '')} ${typeof sc.value === 'number' ? sc.value.toFixed(2) : sc.label || '—'}`))) : '—'] })),
        empty: '这段时间没有会话。' })) : null,
    trace ? h(Card, { title: `会话 ${trace.sessionId}` }, h(Trace, { trace })) : busy === 'trace' ? h(Note, null, '读取 trace…') : null,
    trace ? h(ScoreSession, { key: trace.sessionId, base: `/workspaces/${wid}/agents/${kind}/${id}`, sessionId: trace.sessionId, hours }) : null)
}

export default { id: 'observability', label: '观测', group: '治理', Page: ObservabilityPage }
