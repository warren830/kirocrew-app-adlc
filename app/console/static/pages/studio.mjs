// Studio: a visual builder for Strands agents. A canvas of nodes (输入, 输出, Agent, 编排 Agent, Swarm, 内置工具, 自定义工具,
// 知识库检索) wired port to port and checked as you build (every problem tied to its node), generated into Strands Python
// (main.py + requirements.txt), saved as versioned projects, deployed onto AgentCore Runtime through the console's code
// deployment and chatted with here. Hand-written SVG; the server (console/studio.py) validates and generates.
import React from 'react'
import { call, api, useCtx, listOf, Card, Button, Field, Input, TextArea, Select, Chip, Note, Empty, ErrorLine, Table, useAction, useJob, Tabs,
  JOB_TONE, JOB_LABEL, STATUS_TONE } from '../ui.mjs'
import { download } from './shared.mjs'

const { useState, useEffect, useRef, useMemo, useCallback, createElement: h, Fragment } = React
const ws = (wid, path) => `/workspaces/${wid}/studio${path}`
const LAST_KEY = (wid) => `adlc-studio:last:${wid}`
const DRAFT_KEY = (wid, pid) => `adlc-studio:draft:${wid}:${pid}`

// -- what nodes look like ---------------------------------------------------------------------------------------------

const W = 190
const H = 70
const STYLE = {
  input: { color: '#2f8132', hint: '用户发来的问题' },
  output: { color: '#4c63b6', hint: '最终回答' },
  agent: { color: '#2680c2', hint: '一个模型 + 系统 Prompt + 工具' },
  orchestrator: { color: '#7c5ec4', hint: '把子 Agent 当工具调用，汇总回答' },
  swarm: { color: '#0f8a83', hint: '成员之间互相交接，最合适的成员回答' },
  tool: { color: '#c47b0b', hint: '计算器、当前时间、HTTP 请求' },
  'custom-tool': { color: '#a3480f', hint: '一个带文档字符串的 Python 函数' },
  kb: { color: '#0b7da3', hint: '检索工作区的知识库' },
}
const PALETTE = [['输入输出', ['input', 'output']], ['Agent', ['agent', 'orchestrator', 'swarm']], ['工具', ['tool', 'custom-tool', 'kb']]]
const PORT_AT = { in: [0, H / 2, 'left'], out: [W, H / 2, 'right'], tools: [W * 0.3, H, 'bottom'], sub: [W * 0.7, H, 'bottom'], parent: [W / 2, 0, 'top'],
  tool: [W / 2, 0, 'top'] }
const PORT_LABEL = { in: '输入', out: '输出', tools: '工具', sub: '子 Agent', parent: '上级', tool: '工具' }
const EDGE_COLOR = { input: '#486581', output: '#486581', dependency: '#2680c2', tool: '#c47b0b', member: '#7c5ec4' }
const EXECUTABLE = ['agent', 'orchestrator', 'swarm']
const STAGE_LABEL = { validate: '校验', upload: '上传源码', build: '构建依赖（ARM64）', runtime: '创建 / 更新 Runtime', ready: '等待就绪', smoke: '冒烟调用',
  check: '检查', endpoints: '删除命名端点', repository: '删除 ECR 仓库', role: '删除执行角色', sources: '删除源码' }
const STAGE_TONE = { running: 'info', succeeded: 'ok', failed: 'bad' }
const STAGE_TEXT = { pending: '等待', running: '进行中', succeeded: '完成', skipped: '跳过', failed: '失败' }

function portAt(type, port) {
  if (type === 'swarm' && port === 'sub') return [W / 2, H, 'bottom']
  return PORT_AT[port]
}

// Text clipped to a width counted in half-width characters (CJK and full-width count two).
function clip(text, width) {
  const s = String(text || '')
  let used = 0
  for (let i = 0; i < s.length; i++) {
    used += /[ᄀ-￿]/.test(s[i]) ? 2 : 1
    if (used > width) return s.slice(0, i) + '…'
  }
  return s
}
const slug = (text) => String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')
const newId = (type) => `${type.replace(/[^a-z]/g, '').slice(0, 6)}${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)

function kbToolName(data) {
  if (data.toolName) return data.toolName
  const base = slug(data.kbName).slice(0, 50)
  return base && /^[a-z]/.test(base) ? `search_${base}` : `search_kb_${String(data.kbId || 'unset').toLowerCase()}`
}

function uniqueName(flow, base) {
  const taken = new Set(flow.nodes.map((n) => n.data && n.data.name).filter(Boolean))
  for (let i = 1; ; i++) if (!taken.has(`${base}_${i}`)) return `${base}_${i}`
}

function nodeLines(n, catalog, info) {
  const d = n.data || {}
  const model = (catalog.models.find((m) => m.id === d.model) || {}).label || d.model || '（选一个模型）'
  if (n.type === 'input') return ['用户的问题', d.sample ? `例：${d.sample}` : '可在右侧填一个示例问题']
  if (n.type === 'output') return ['回答', d.traceTools ? '附上这一轮的工具调用' : '只有回答']
  if (n.type === 'agent' || n.type === 'orchestrator') return [d.name || '（起一个英文名称）', model]
  if (n.type === 'swarm') return [d.name || '（起一个英文名称）', `最多交接 ${d.maxHandoffs} 次 · ${d.executionTimeout}s`]
  if (n.type === 'tool') {
    const t = catalog.builtinTools.find((b) => b.id === d.tool)
    return [t ? `${t.label}（${t.id}）` : '（选一个工具）', d.tool === 'current_time' ? `默认时区 ${d.timezone}` : d.tool === 'http_request' ? `最多 ${d.maxChars} 字` : 'strands-agents-tools']
  }
  if (n.type === 'custom-tool') {
    const name = (info && info.name) || ((/def\s+([A-Za-z_]\w*)/.exec(d.code || '') || [])[1])
    return [name ? `${name}()` : '（写一个函数）', (info && info.doc) || 'Python 函数']
  }
  if (n.type === 'kb') return [d.kbName || '（选一个知识库）', kbToolName(d)]
  return ['', '']
}

// The connection rules from the catalog: which port may connect to which (Graph mode adds Agent → Agent).
function ruleFor(catalog, graph, s, sp, t, tp) {
  return catalog.rules.find((r) => r.sources.includes(s) && r.sourcePort === sp && r.targets.includes(t) && r.targetPort === tp && (graph || !r.graphOnly)) || null
}

function connectProblem(catalog, flow, a, b) {
  // a, b: {node, port, dir}; returns [edge, null] or [null, why]
  if (a.dir === b.dir) return [null, '要从一个输出端口（实心一侧）连到一个输入端口']
  const [src, tgt] = a.dir === 'source' ? [a, b] : [b, a]
  if (src.node.id === tgt.node.id) return [null, '节点不能连到它自己']
  const label = (n) => `${(catalog.types.find((t) => t.type === n.type) || {}).label || n.type}「${n.data.label}」`
  if (!ruleFor(catalog, flow.graphMode, src.node.type, src.port, tgt.node.type, tgt.port)) {
    if (ruleFor(catalog, true, src.node.type, src.port, tgt.node.type, tgt.port)) return [null, 'Agent 之间的连线表示先后依赖：先打开右上角的 Graph 模式']
    return [null, `${label(src.node)} 的「${PORT_LABEL[src.port]}」不能连到 ${label(tgt.node)} 的「${PORT_LABEL[tgt.port]}」`]
  }
  if (flow.edges.some((e) => e.source === src.node.id && e.sourcePort === src.port && e.target === tgt.node.id && e.targetPort === tgt.port)) return [null, '这条连线已经有了']
  return [{ id: `e-${newId('e')}`, source: src.node.id, sourcePort: src.port, target: tgt.node.id, targetPort: tgt.port }, null]
}

function edgeKind(catalog, flow, e) {
  const s = flow.nodes.find((n) => n.id === e.source)
  const t = flow.nodes.find((n) => n.id === e.target)
  const rule = s && t && ruleFor(catalog, true, s.type, e.sourcePort, t.type, e.targetPort)
  return rule ? rule.kind : 'input'
}

function curve(a, b) {
  const pull = (side, p, k) => ({ left: [p[0] - k, p[1]], right: [p[0] + k, p[1]], top: [p[0], p[1] - k], bottom: [p[0], p[1] + k] }[side])
  const k = Math.max(40, Math.min(120, Math.hypot(b[0] - a[0], b[1] - a[1]) / 2))
  const c1 = pull(a[2], a, k)
  const c2 = pull(b[2], b, k)
  return `M${a[0]},${a[1]} C${c1[0]},${c1[1]} ${c2[0]},${c2[1]} ${b[0]},${b[1]}`
}

// -- the canvas ---------------------------------------------------------------------------------------------------------

function Canvas({ flow, catalog, edit, selection, setSelection, marks, tools, onNotice, onAdd, fitSignal, undo, redo }) {
  const svgRef = useRef(null)
  const wrapRef = useRef(null)
  const [view, setView] = useState({ x: 20, y: 20, k: 1 })
  const viewRef = useRef(view)
  viewRef.current = view
  const [wire, setWire] = useState(null)
  const byId = useMemo(() => Object.fromEntries(flow.nodes.map((n) => [n.id, n])), [flow.nodes])

  const toCanvas = (clientX, clientY) => {
    const rect = svgRef.current.getBoundingClientRect()
    const v = viewRef.current
    return { x: (clientX - rect.left - v.x) / v.k, y: (clientY - rect.top - v.y) / v.k }
  }
  const fit = useCallback(() => {
    const el = svgRef.current
    if (!el || !flow.nodes.length) return
    const xs = flow.nodes.map((n) => n.x)
    const ys = flow.nodes.map((n) => n.y)
    const minX = Math.min(...xs) - 50
    const minY = Math.min(...ys) - 40
    const bw = Math.max(...xs) + W + 50 - minX
    const bh = Math.max(...ys) + H + 50 - minY
    const rect = el.getBoundingClientRect()
    const k = Math.max(0.35, Math.min(1.15, rect.width / bw, rect.height / bh))
    setView({ k, x: (rect.width - bw * k) / 2 - minX * k, y: (rect.height - bh * k) / 2 - minY * k })
  }, [flow.nodes])
  useEffect(() => { fit() }, [fitSignal])

  useEffect(() => { // zoom around the pointer (a native listener: React's wheel handler is passive)
    const el = svgRef.current
    if (!el) return undefined
    const onWheel = (e) => {
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const px = e.clientX - rect.left
      const py = e.clientY - rect.top
      setView((v) => {
        const k = Math.max(0.35, Math.min(2, v.k * Math.exp(-e.deltaY * 0.0015)))
        return { k, x: px - ((px - v.x) * k) / v.k, y: py - ((py - v.y) * k) / v.k }
      })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  const track = (move, up) => {
    const onMove = (e) => move(e)
    const onUp = (e) => { window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp); if (up) up(e) }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }
  const downBackground = (e) => {
    if (e.button !== 0) return
    setSelection(null)
    const start = { x: e.clientX, y: e.clientY, vx: viewRef.current.x, vy: viewRef.current.y }
    track((m) => setView((v) => ({ ...v, x: start.vx + m.clientX - start.x, y: start.vy + m.clientY - start.y })))
  }
  const downNode = (e, n) => {
    if (e.button !== 0) return
    e.stopPropagation()
    setSelection({ kind: 'node', id: n.id })
    if (wrapRef.current) wrapRef.current.focus({ preventScroll: true })
    const start = { x: e.clientX, y: e.clientY, nx: n.x, ny: n.y, key: `move:${n.id}:${Date.now()}` }
    track((m) => {
      const k = viewRef.current.k
      const x = Math.round((start.nx + (m.clientX - start.x) / k) / 10) * 10
      const y = Math.round((start.ny + (m.clientY - start.y) / k) / 10) * 10
      edit((f) => {
        const cur = f.nodes.find((q) => q.id === n.id)
        if (!cur || (cur.x === x && cur.y === y)) return f
        return { ...f, nodes: f.nodes.map((q) => (q.id === n.id ? { ...q, x, y } : q)) }
      }, start.key)
    })
  }
  const downPort = (e, n, port, dir) => {
    if (e.button !== 0) return
    e.stopPropagation()
    const [px, py] = portAt(n.type, port)
    const from = { node: n, port, dir, x: n.x + px, y: n.y + py }
    setWire({ from, to: { x: from.x, y: from.y } })
    track((m) => setWire((w) => (w ? { ...w, to: toCanvas(m.clientX, m.clientY) } : w)), (up) => {
      setWire(null)
      const target = up.target && up.target.getAttribute && up.target.getAttribute('data-port')
      const nid = up.target && up.target.getAttribute && up.target.getAttribute('data-node')
      if (!target || !nid) return
      const other = byIdRef.current[nid]
      if (!other) return
      const dirOther = ((catalog.types.find((t) => t.type === other.type) || {}).ports || {})[target]
      const [edge, why] = connectProblem(catalog, flowRef.current, from, { node: other, port: target, dir: dirOther })
      if (why) onNotice(why)
      else { edit((f) => ({ ...f, edges: [...f.edges, edge] }), `edge:${edge.id}`); setSelection({ kind: 'edge', id: edge.id }) }
    })
  }
  const byIdRef = useRef(byId)
  byIdRef.current = byId
  const flowRef = useRef(flow)
  flowRef.current = flow

  const onKey = (e) => {
    const tag = (e.target && e.target.tagName) || ''
    if (/INPUT|TEXTAREA|SELECT/.test(tag)) return
    if ((e.key === 'Delete' || e.key === 'Backspace') && selection) {
      e.preventDefault()
      if (selection.kind === 'node') edit((f) => ({ ...f, nodes: f.nodes.filter((n) => n.id !== selection.id), edges: f.edges.filter((x) => x.source !== selection.id && x.target !== selection.id) }), `del:${selection.id}`)
      else edit((f) => ({ ...f, edges: f.edges.filter((x) => x.id !== selection.id) }), `del:${selection.id}`)
      setSelection(null)
    } else if (e.key === 'Escape') setSelection(null)
    else if ((e.metaKey || e.ctrlKey) && (e.key === 'z' || e.key === 'Z')) { e.preventDefault(); if (e.shiftKey) redo(); else undo() }
    else if ((e.metaKey || e.ctrlKey) && e.key === 'y') { e.preventDefault(); redo() }
  }
  const onDrop = (e) => {
    const type = e.dataTransfer.getData('text/x-studio-node')
    if (!type) return
    e.preventDefault()
    const p = toCanvas(e.clientX, e.clientY)
    onAdd(type, Math.round((p.x - W / 2) / 10) * 10, Math.round((p.y - H / 2) / 10) * 10)
  }
  const wiringOk = (n, port, dir) => wire && !connectProblem(catalog, flow, wire.from, { node: n, port, dir })[1]
  const ports = (n) => Object.entries(((catalog.types.find((t) => t.type === n.type) || {}).ports) || {})

  const edges = flow.edges.map((e) => {
    const s = byId[e.source]
    const t = byId[e.target]
    if (!s || !t || !portAt(s.type, e.sourcePort) || !portAt(t.type, e.targetPort)) return null
    const a = portAt(s.type, e.sourcePort)
    const b = portAt(t.type, e.targetPort)
    const d = curve([s.x + a[0], s.y + a[1], a[2]], [t.x + b[0], t.y + b[1], b[2]])
    const kind = edgeKind(catalog, flow, e)
    const bad = (marks.edges[e.id] || []).length > 0
    const on = selection && selection.kind === 'edge' && selection.id === e.id
    return h('g', { key: e.id },
      h('path', { d, fill: 'none', stroke: bad ? '#d64545' : EDGE_COLOR[kind] || '#486581', strokeWidth: on ? 3 : 1.6, strokeDasharray: bad ? '6 4' : kind === 'member' ? '5 3' : undefined,
        markerEnd: 'url(#st-arrow)', opacity: on ? 1 : 0.85 }),
      h('path', { d, fill: 'none', stroke: 'transparent', strokeWidth: 12, style: { cursor: 'pointer' },
        onMouseDown: (ev) => { ev.stopPropagation(); setSelection({ kind: 'edge', id: e.id }); if (wrapRef.current) wrapRef.current.focus({ preventScroll: true }) } },
      h('title', null, bad ? marks.edges[e.id].join('\n') : `${s.data.label} → ${t.data.label}`)))
  })

  const nodes = flow.nodes.map((n) => {
    const style = STYLE[n.type] || STYLE.agent
    const errs = (marks.nodes[n.id] || {}).errors || []
    const warns = (marks.nodes[n.id] || {}).warnings || []
    const on = selection && selection.kind === 'node' && selection.id === n.id
    const [line1, line2] = nodeLines(n, catalog, tools[n.id])
    const typeLabel = (catalog.types.find((t) => t.type === n.type) || {}).label || n.type
    return h('g', { key: n.id, transform: `translate(${n.x},${n.y})`, onMouseDown: (e) => downNode(e, n), style: { cursor: 'move' }, 'data-studio-node': n.id },
      h('rect', { width: W, height: H, rx: 8, fill: '#fff', stroke: errs.length ? '#d64545' : on ? '#2680c2' : '#bcccdc', strokeWidth: on || errs.length ? 2 : 1,
        filter: 'url(#st-shadow)' }),
      h('path', { d: `M0,22 V8 A8,8 0 0 1 8,0 H${W - 8} A8,8 0 0 1 ${W},8 V22 Z`, fill: style.color }),
      h('text', { x: 9, y: 15, fill: '#fff', fontSize: 11.5, fontWeight: 600 }, clip(n.data.label, 18)),
      h('text', { x: W - 8, y: 15, fill: 'rgba(255,255,255,.82)', fontSize: 9.5, textAnchor: 'end' }, typeLabel),
      h('text', { x: 10, y: 41, fontSize: 12, fill: '#243b53', fontFamily: n.type === 'agent' || n.type === 'orchestrator' || n.type === 'swarm' || n.type === 'custom-tool' ? 'ui-monospace,Menlo,monospace' : undefined },
        clip(line1, 27)),
      h('text', { x: 10, y: 59, fontSize: 10.5, fill: '#829ab1' }, clip(line2, 31)),
      errs.length || warns.length ? h('g', { transform: `translate(${W - 10},${H - 10})` },
        h('circle', { r: 7, fill: errs.length ? '#d64545' : '#e8a33d' }),
        h('text', { y: 3.5, textAnchor: 'middle', fontSize: 9.5, fill: '#fff', fontWeight: 700 }, String(errs.length || warns.length)),
        h('title', null, [...errs, ...warns].join('\n'))) : null,
      ports(n).map(([port, dir]) => {
        const [px, py, side] = portAt(n.type, port)
        const hot = wiringOk(n, port, dir)
        const label = n.type === 'swarm' && port === 'sub' ? '成员' : PORT_LABEL[port]
        const lx = side === 'left' ? px - 9 : side === 'right' ? px + 9 : px
        const ly = side === 'top' ? py - 8 : side === 'bottom' ? py + 16 : py - 8
        return h('g', { key: port },
          hot ? h('circle', { cx: px, cy: py, r: 10, fill: 'rgba(47,129,50,.18)', stroke: '#2f8132', strokeWidth: 1 }) : null,
          h('circle', { cx: px, cy: py, r: 5.5, fill: dir === 'source' ? style.color : '#fff', stroke: style.color, strokeWidth: 2, 'data-node': n.id, 'data-port': port,
            className: 'cs-st-port', onMouseDown: (e) => downPort(e, n, port, dir) }, h('title', null, `${label}（${dir === 'source' ? '从这里拖出连线' : '连线连到这里'}）`)),
          on || hot ? h('text', { x: lx, y: ly, fontSize: 9.5, fill: hot ? '#2f8132' : '#486581', textAnchor: side === 'left' ? 'end' : side === 'right' ? 'start' : 'middle',
            pointerEvents: 'none', fontWeight: 600 }, label) : null)
      }))
  })

  return h('div', { ref: wrapRef, tabIndex: 0, onKeyDown: onKey, onDragOver: (e) => e.preventDefault(), onDrop, className: 'cs-st-canvas',
    style: { position: 'relative', flex: 1, minWidth: 0, height: 580, border: '1px solid #d9e2ec', borderRadius: 10, background: '#fbfcfe', overflow: 'hidden', outline: 'none' } },
  h('svg', { ref: svgRef, width: '100%', height: '100%', onMouseDown: downBackground, style: { display: 'block', cursor: wire ? 'crosshair' : 'grab', userSelect: 'none' } },
    h('defs', null,
      h('pattern', { id: 'st-grid', width: 20 * view.k, height: 20 * view.k, patternUnits: 'userSpaceOnUse', x: view.x, y: view.y },
        h('circle', { cx: 1, cy: 1, r: 1, fill: '#d9e2ec' })),
      h('marker', { id: 'st-arrow', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' },
        h('path', { d: 'M0,0 L10,5 L0,10 z', fill: '#829ab1' })),
      h('filter', { id: 'st-shadow', x: '-10%', y: '-10%', width: '120%', height: '130%' }, h('feDropShadow', { dx: 0, dy: 1, stdDeviation: 1.5, floodOpacity: 0.12 }))),
    h('rect', { width: '100%', height: '100%', fill: 'url(#st-grid)' }),
    h('g', { transform: `translate(${view.x},${view.y}) scale(${view.k})` },
      edges,
      wire ? h('path', { d: curve([wire.from.x, wire.from.y, portAt(wire.from.node.type, wire.from.port)[2]], [wire.to.x, wire.to.y, 'left']), fill: 'none', stroke: '#2680c2',
        strokeWidth: 1.5, strokeDasharray: '4 3', pointerEvents: 'none' }) : null,
      nodes)),
  !flow.nodes.length ? h('div', { style: { position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#829ab1', pointerEvents: 'none' } },
    '从左边拖节点进来，或点一下添加') : null,
  h('div', { style: { position: 'absolute', right: 8, bottom: 8, display: 'flex', gap: 4 } },
    h(Button, { onClick: () => setView((v) => ({ ...v, k: Math.min(2, v.k * 1.2) })), title: '放大' }, '+'),
    h(Button, { onClick: () => setView((v) => ({ ...v, k: Math.max(0.35, v.k / 1.2) })), title: '缩小' }, '−'),
    h(Button, { onClick: fit, title: '把整个流程放进画布' }, '适应')),
  h('div', { style: { position: 'absolute', left: 10, bottom: 8, fontSize: 11, color: '#9fb3c8', pointerEvents: 'none' } },
    `${Math.round(view.k * 100)}% · 拖动空白处平移，滚轮缩放 · 从端口拖出连线 · Delete 删除选中`))
}

function Palette({ flow, onAdd }) {
  const has = (type) => flow.nodes.some((n) => n.type === type)
  return h('div', { style: { width: 118, flex: '0 0 118px', display: 'flex', flexDirection: 'column', gap: 6 } },
    PALETTE.map(([group, types]) => h('div', { key: group },
      h('div', { className: 'cs-mut', style: { margin: '2px 0 4px' } }, group),
      types.map((type) => {
        const single = (type === 'input' || type === 'output') && has(type)
        return h('div', { key: type, draggable: !single, title: single ? '已经有一个了（只能有一个）' : `${STYLE[type].hint}：拖到画布，或点一下添加`, 'data-palette': type,
          onDragStart: (e) => { e.dataTransfer.setData('text/x-studio-node', type); e.dataTransfer.effectAllowed = 'copy' },
          onClick: () => !single && onAdd(type),
          style: { border: '1px solid #d9e2ec', borderLeft: `4px solid ${STYLE[type].color}`, borderRadius: 6, padding: '5px 7px', marginBottom: 5, background: '#fff',
            cursor: single ? 'not-allowed' : 'grab', opacity: single ? 0.45 : 1, fontSize: 12.5 } },
        h('b', null, labelOf(type)), h('div', { style: { fontSize: 10.5, color: '#829ab1', lineHeight: 1.3 } }, STYLE[type].hint))
      }))))
}
const TYPE_NAMES = { input: '输入', output: '输出', agent: 'Agent', orchestrator: '编排 Agent', swarm: 'Swarm', tool: '内置工具', 'custom-tool': '自定义工具', kb: '知识库检索' }
const labelOf = (type) => TYPE_NAMES[type] || type

// -- the inspector ---------------------------------------------------------------------------------------------------

function NumberField({ label, value, onChange, hint, placeholder }) {
  const shown = value === null || value === undefined ? '' : String(value)
  const [text, setText] = useState(shown)
  useEffect(() => { if (Number(text) !== value && !(text.trim() === '' && (value === null || value === undefined))) setText(shown) }, [value])
  const change = (v) => {
    setText(v)
    if (v.trim() === '') onChange(null)
    else if (Number.isFinite(Number(v))) onChange(Number(v))
  }
  return h(Field, { label, hint }, h(Input, { value: text, placeholder, onChange: change }))
}

function ModelPicker({ catalog, value, onChange }) {
  const known = catalog.models.some((m) => m.id === value)
  const [custom, setCustom] = useState(!known)
  return h(Fragment, null,
    h(Field, { label: '模型' }, h(Select, { value: custom ? '__custom' : value, onChange: (v) => { if (v === '__custom') setCustom(true); else { setCustom(false); onChange(v) } },
      options: [...catalog.models.map((m) => [m.id, m.label]), ['__custom', '其他模型 ID…']] })),
    custom ? h(Field, { label: '模型 ID', hint: 'Bedrock 模型或推理配置文件 ID，如 us.anthropic.claude-sonnet-4-5-20250929-v1:0' },
      h(Input, { value, onChange, mono: true })) : null)
}

function Inspector({ flow, catalog, selection, edit, remove, duplicate, issues, tools, kbs, loadKbs }) {
  const node = selection && selection.kind === 'node' ? flow.nodes.find((n) => n.id === selection.id) : null
  const edge = selection && selection.kind === 'edge' ? flow.edges.find((e) => e.id === selection.id) : null
  const box = (title, ...children) => h('div', { style: { width: 300, flex: '0 0 300px', height: 580, overflowY: 'auto', border: '1px solid #d9e2ec', borderRadius: 10,
    background: '#fff', padding: '10px 12px', boxSizing: 'border-box' } }, h('div', { className: 'cs-card-h' }, h('b', null, title)), ...children)
  useEffect(() => { if (node && node.type === 'kb' && kbs === null) loadKbs() }, [node && node.id])
  if (edge) {
    const s = flow.nodes.find((n) => n.id === edge.source)
    const t = flow.nodes.find((n) => n.id === edge.target)
    const kind = edgeKind(catalog, flow, edge)
    const meaning = { input: '用户的问题交给它', output: '它的回答就是最终回答', dependency: 'Graph 依赖：上游先运行，结果传给下游', tool: '这个 Agent 可以调用这个工具',
      member: s && s.type === 'swarm' ? '它是这个 Swarm 的成员' : '它是这个编排 Agent 的子 Agent（作为工具被调用）' }[kind]
    return box('连线',
      h('div', null, `${s ? s.data.label : '?'}「${PORT_LABEL[edge.sourcePort] || edge.sourcePort}」 → ${t ? t.data.label : '?'}「${PORT_LABEL[edge.targetPort] || edge.targetPort}」`),
      h(Note, null, meaning),
      listOf(issues.filter((i) => i.edge === edge.id)).map((i, k) => h('div', { key: k, className: 'cs-err' }, i.message)),
      h(Button, { kind: 'danger', onClick: () => remove() }, '删除连线'))
  }
  if (!node) {
    const count = (types) => flow.nodes.filter((n) => types.includes(n.type)).length
    return box('流程',
      h(Note, null, flow.graphMode ? 'Graph 模式：没有上级的 Agent / Swarm 都是 Graph 的节点，Agent 之间的连线表示依赖（上游先运行，输出传给下游），连到「输出」的节点给出回答。'
        : '一个 Agent（或编排 Agent、Swarm）接收「输入」并连到「输出」。编排 Agent 把子 Agent 当工具；Swarm 的成员互相交接。'),
      h('div', { className: 'cs-mut' }, `${count(EXECUTABLE)} 个 Agent · ${count(['tool', 'custom-tool', 'kb'])} 个工具 · ${flow.edges.length} 条连线`),
      h(Note, null, '点一个节点或连线查看和修改它的属性。端口：左边是输入、右边是输出、下面接工具和子 Agent、上面接上级。'),
      h('div', { className: 'cs-mut' }, `生成的代码：${catalog.versions.strands} · ${catalog.versions.agentcore} · ${catalog.versions.python}`))
  }
  const d = node.data
  const set = (field) => (value) => edit((f) => ({ ...f, nodes: f.nodes.map((n) => (n.id === node.id ? { ...n, data: { ...n.data, [field]: value } } : n)) }), `prop:${node.id}:${field}`)
  const mine = issues.filter((i) => i.node === node.id)
  const members = flow.edges.filter((e) => e.source === node.id && e.sourcePort === 'sub').map((e) => flow.nodes.find((n) => n.id === e.target)).filter(Boolean)
  const info = tools[node.id]
  const body = []
  body.push(h(Field, { key: 'label', label: '显示名' }, h(Input, { value: d.label, onChange: set('label') })))
  if (node.type === 'input') {
    body.push(h(Field, { key: 's', label: '示例问题', hint: '部署后的冒烟调用和对话框默认用它' }, h(TextArea, { value: d.sample, onChange: set('sample'), rows: 3 })))
  } else if (node.type === 'output') {
    body.push(h(Field, { key: 't', label: '回答末尾附上工具调用', hint: '如「〔工具〕support·lookup_order」：调试时看 Agent 用了哪些工具；上线前可以关掉' },
      h(Select, { value: d.traceTools ? 'y' : 'n', onChange: (v) => set('traceTools')(v === 'y'), options: [['y', '附上'], ['n', '不附上']] })))
  } else if (node.type === 'agent' || node.type === 'orchestrator') {
    body.push(h(Field, { key: 'n', label: '名称（英文）', hint: '代码、子 Agent 工具名和 Graph 节点都用它' }, h(Input, { value: d.name, onChange: set('name'), mono: true })),
      h(Field, { key: 'd', label: '职责说明', hint: '作为子 Agent 或 Swarm 成员时，别人靠它决定什么时候找它' }, h(TextArea, { value: d.description, onChange: set('description'), rows: 2 })),
      h(ModelPicker, { key: 'm' + node.id, catalog, value: d.model, onChange: set('model') }),
      h(Field, { key: 'p', label: '系统 Prompt' }, h(TextArea, { value: d.systemPrompt, onChange: set('systemPrompt'), rows: 9 })),
      h('div', { key: 'nums', style: { display: 'flex', gap: 8 } },
        h('div', { style: { flex: 1 } }, h(NumberField, { label: '温度', value: d.temperature, onChange: set('temperature'), placeholder: '模型默认' })),
        h('div', { style: { flex: 1 } }, h(NumberField, { label: '最多输出 tokens', value: d.maxTokens, onChange: set('maxTokens') }))))
    if (node.type === 'orchestrator') body.push(h(Note, { key: 'o' }, `子 Agent：${members.map((m) => m.data.name || m.data.label).join('、') || '还没有（把「子 Agent」端口连到 Agent 的「上级」端口）'}`))
  } else if (node.type === 'swarm') {
    body.push(h(Field, { key: 'n', label: '名称（英文）' }, h(Input, { value: d.name, onChange: set('name'), mono: true })),
      h(Field, { key: 'e', label: '入口成员', hint: '用户的问题先交给它' }, h(Select, { value: d.entry || (members[0] ? members[0].id : ''), onChange: set('entry'),
        options: members.length ? members.map((m) => [m.id, `${m.data.label}（${m.data.name}）`]) : [['', '先连上成员']] })),
      h('div', { key: 'n1', style: { display: 'flex', gap: 8 } },
        h('div', { style: { flex: 1 } }, h(NumberField, { label: '最多交接次数', value: d.maxHandoffs, onChange: set('maxHandoffs') })),
        h('div', { style: { flex: 1 } }, h(NumberField, { label: '最多执行次数', value: d.maxIterations, onChange: set('maxIterations') }))),
      h('div', { key: 'n2', style: { display: 'flex', gap: 8 } },
        h('div', { style: { flex: 1 } }, h(NumberField, { label: '总超时（秒）', value: d.executionTimeout, onChange: set('executionTimeout') })),
        h('div', { style: { flex: 1 } }, h(NumberField, { label: '每个成员超时（秒）', value: d.nodeTimeout, onChange: set('nodeTimeout') }))),
      h(Note, { key: 'm' }, `成员：${members.map((m) => m.data.name || m.data.label).join('、') || '还没有（把「成员」端口连到 Agent 的「上级」端口）'}。成员会自动得到 handoff_to_agent 工具。`))
  } else if (node.type === 'tool') {
    const t = catalog.builtinTools.find((b) => b.id === d.tool)
    body.push(h(Field, { key: 't', label: '工具' }, h(Select, { value: d.tool, onChange: set('tool'), options: catalog.builtinTools.map((b) => [b.id, `${b.label}（${b.id}）`]) })),
      t ? h(Note, { key: 'd' }, t.description) : null)
    if (d.tool === 'current_time') body.push(h(Field, { key: 'z', label: '默认时区', hint: '模型没指定时区时用它' }, h(Input, { value: d.timezone, onChange: set('timezone'), mono: true })))
    if (d.tool === 'http_request') body.push(h(NumberField, { key: 'c', label: '返回给模型的最多字符数', value: d.maxChars, onChange: set('maxChars'), hint: '网页会先去掉标签再截断，避免撑爆上下文' }))
  } else if (node.type === 'custom-tool') {
    body.push(h(Field, { key: 'c', label: 'Python 代码', hint: `一个函数，带文档字符串和类型注解；可以 import：${catalog.safeModules.filter((m) => !m.includes('.')).join(' ')}` },
      h(TextArea, { value: d.code, onChange: set('code'), rows: 16, mono: true })),
    info && info.name ? h(Note, { key: 'i', tone: info.ok ? 'ok' : undefined }, `${info.name}(${listOf(info.params).map((p) => `${p.name}: ${p.type}`).join(', ')})${info.doc ? ` — ${info.doc}` : ''}`) : null,
    h(Button, { key: 'r', onClick: () => set('code')(catalog.customToolTemplate) }, '换成示例代码'))
  } else if (node.type === 'kb') {
    const options = listOf(kbs).map((k) => [k.id, `${k.name} · ${k.id} · ${k.type || '?'}${k.status !== 'ACTIVE' ? ` · ${k.status}` : ''}`])
    const pick = (id) => {
      const kb = listOf(kbs).find((k) => k.id === id)
      edit((f) => ({ ...f, nodes: f.nodes.map((n) => (n.id === node.id ? { ...n, data: { ...n.data, kbId: id, kbName: kb ? kb.name : '', kbType: kb && kb.type === 'VECTOR' ? 'VECTOR' : 'MANAGED',
        label: n.data.label === '知识库检索' && kb ? kb.name : n.data.label } } : n)) }), `prop:${node.id}:kb`)
    }
    body.push(h(Field, { key: 'k', label: '知识库', hint: kbs === null ? '加载中…' : '这个工作区的 Bedrock 知识库（托管或向量）' },
      h(Select, { value: d.kbId || '', onChange: pick, options: [['', d.kbId ? `${d.kbName || d.kbId}（不在列表里）` : '选一个知识库'], ...options] })),
    h(Button, { key: 'rf', onClick: loadKbs }, '刷新列表'),
    h(Field, { key: 'n', label: '工具名', hint: '模型按这个名字调用它（英文）' }, h(Input, { value: d.toolName, onChange: set('toolName'), mono: true, placeholder: kbToolName({ ...d, toolName: '' }) })),
    h(Field, { key: 'd', label: '何时使用', hint: '留空则用知识库的描述生成' }, h(TextArea, { value: d.description, onChange: set('description'), rows: 3 })),
    h(NumberField, { key: 't', label: '返回条数（1-20）', value: d.topK, onChange: set('topK') }),
    h(Note, { key: 'iam' }, '部署时 Runtime 的执行角色只得到这些知识库的 bedrock:Retrieve 权限；托管知识库用 managedSearchConfiguration 检索。'))
  }
  return box(`${labelOf(node.type)}「${d.label}」`, ...body,
    mine.map((i, k) => h('div', { key: `i${k}`, className: i.level === 'error' ? 'cs-err' : 'cs-note cs-tx-warn' }, i.message)),
    h('div', { className: 'cs-row', style: { marginTop: 10 } },
      node.type !== 'input' && node.type !== 'output' ? h(Button, { onClick: duplicate }, '复制节点') : null,
      h(Button, { kind: 'danger', onClick: () => remove() }, '删除节点')))
}

function Issues({ issues, onPick }) {
  if (!issues) return h('div', { className: 'cs-mut' }, '检查中…')
  if (!issues.length) return h(Note, { tone: 'ok' }, '没有问题：这个流程可以生成代码并部署。')
  return h('div', null, issues.map((i, k) => h('div', { key: k, onClick: () => onPick(i), className: i.level === 'error' ? 'cs-err' : 'cs-note cs-tx-warn',
    style: { cursor: i.node || i.edge ? 'pointer' : 'default', margin: '3px 0' } }, `${i.level === 'error' ? '错误' : '提醒'}：${i.message}`)))
}

// -- code ----------------------------------------------------------------------------------------------------------------

function CodeView({ preview, onDownload, busy }) {
  const [file, setFile] = useState('main.py')
  if (!preview) return h(Empty, null, '生成中…')
  if (!preview.ok) return h(Card, { title: '还不能生成代码' }, h(Issues, { issues: preview.issues.filter((i) => i.level === 'error'), onPick: () => {} }))
  const text = preview.files[file] || ''
  return h(Card, { title: '生成的代码', extra: h('span', { className: 'cs-row' },
    h(Tabs, { value: file, onChange: setFile, options: Object.keys(preview.files).map((f) => [f, f]) }),
    h(Button, { onClick: () => navigator.clipboard && navigator.clipboard.writeText(text) }, '复制'),
    h(Button, { kind: 'pri', onClick: onDownload, busy }, '下载 zip')) },
  h(Note, null, '这就是部署时上传的 zip：根目录的 main.py（BedrockAgentCoreApp 入口，{"prompt"} → 文本回答）、requirements.txt（CodeBuild 按 linux/arm64 安装）和 studio_flow.json（流程本身，部署后的 Runtime 可以在 Studio 重新打开）。'),
  h('pre', { className: 'cs-pre', style: { maxHeight: 560, overflow: 'auto', whiteSpace: 'pre', fontSize: 12 } }, text))
}

// -- deploy and chat -------------------------------------------------------------------------------------------------

function JobView({ job }) {
  const stages = listOf(job.progress && job.progress.stages)
  const smoke = (job.result || {}).smoke
  return h(Fragment, null,
    stages.length ? h(Table, { head: ['阶段', '状态', '耗时', '说明'], rows: stages.map((s) => ({ key: s.name, cells: [h('b', null, STAGE_LABEL[s.name] || s.name),
      s.status === 'running' ? h('span', { className: 'cs-row' }, h('span', { className: 'cs-spin' }), STAGE_TEXT.running) : h(Chip, { tone: STAGE_TONE[s.status] }, STAGE_TEXT[s.status] || s.status),
      s.seconds !== undefined && s.seconds !== null ? `${s.seconds} s` : '—', s.detail ? h('span', { className: s.status === 'failed' ? 'cs-err' : 'cs-mut' }, s.detail) : '—'] })) })
      : h(Empty, null, '准备中…'),
    smoke ? h(Note, { tone: 'ok' }, h('b', null, `冒烟调用（${smoke.seconds} s）：`), smoke.answer) : null,
    job.error ? h('div', { className: 'cs-err' }, job.error) : null,
    h('pre', { className: 'cs-pre', style: { maxHeight: 220, overflow: 'auto' } }, listOf(job.log).slice(-14).join('\n') || '…'))
}

function splitFooter(text) {
  const cut = text.lastIndexOf('\n\n〔')
  if (cut < 0) return [text, null]
  const tail = text.slice(cut + 2)
  if (!/^〔(工具|这一轮没有调用工具)/.test(tail)) return [text, null]
  return [text.slice(0, cut), tail.startsWith('〔工具〕') ? tail.slice(4).split('，') : []]
}

function RuntimeChat({ runtime, sample, go, setSelected }) {
  const { wid, me } = useCtx()
  const [turns, setTurns] = useState([])
  const [message, setMessage] = useState(sample || '')
  const [sessionId, setSessionId] = useState(null)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState(null)
  const [logs, setLogs] = useState(null)
  const bottom = useRef(null)
  useEffect(() => { setTurns([]); setSessionId(null); setMessage(sample || ''); setLogs(null) }, [runtime.runtimeId])
  const loadLogs = (all) => call('GET', ws(wid, `/runtimes/${runtime.runtimeId}/logs?minutes=120${sessionId && !all ? `&session=${encodeURIComponent(sessionId)}` : ''}`))
    .then((r) => setLogs({ ...r, scope: sessionId && !all ? '这个会话' : '全部会话' })).catch((err) => setLogs({ events: [], error: err.message }))
  const failed = turns.some((t) => t.error)
  useEffect(() => { if (bottom.current) bottom.current.scrollIntoView({ block: 'nearest' }) }, [turns])
  const send = async () => {
    const text = message.trim()
    if (!text) return
    setMessage(''); setSending(true); setError(null)
    setTurns((list) => [...list, { role: 'user', text }, { role: 'agent', text: '', live: true }])
    const patch = (fn) => setTurns((list) => { const copy = list.slice(); copy[copy.length - 1] = fn({ ...copy[copy.length - 1] }); return copy })
    try {
      await api.chat(wid, 'runtime', runtime.runtimeId, { message: text, sessionId, actorId: me && me.username }, (e) => {
        if (e.type === 'session') setSessionId(e.sessionId)
        else if (e.type === 'text') patch((t) => ({ ...t, text: t.text + e.text }))
        else if (e.type === 'error') patch((t) => ({ ...t, error: e.error }))
        else if (e.type === 'stop') patch((t) => ({ ...t, live: false, seconds: e.seconds }))
      })
    } catch (err) { setError(err) } finally { setSending(false); patch((t) => ({ ...t, live: false })) }
  }
  return h(Card, { title: `和 ${runtime.name} 对话`, extra: h('span', { className: 'cs-row' },
    h(Button, { onClick: () => { setTurns([]); setSessionId(null); setLogs(null) } }, '新会话'),
    h(Button, { kind: failed ? 'pri' : undefined, onClick: () => loadLogs(false), title: 'AgentCore 不把出错时的回答传回来：原因在 Runtime 的日志里' }, 'Runtime 日志'),
    h(Button, { onClick: () => { setSelected({ kind: 'runtime', id: runtime.runtimeId }); go('chat') } }, '在「对话」页打开')) },
  h('div', { className: 'cs-chat' }, turns.length ? turns.map((t, i) => {
    const [body, calls] = t.role === 'agent' ? splitFooter(t.text) : [t.text, null]
    return h('div', { key: i, className: `cs-turn ${t.role === 'user' ? 'cs-user' : 'cs-agent'}` },
      h('div', { className: 'cs-bubble' }, body || (t.live ? '…（第一轮要等 Runtime 冷启动）' : t.error ? '' : '（没有文字）')),
      calls ? h('div', { className: 'cs-row', style: { marginTop: 3 } }, calls.length ? calls.map((c, k) => h(Chip, { key: k, tone: c.includes('失败') ? 'bad' : 'info' }, c))
        : h('span', { className: 'cs-mut' }, '这一轮没有调用工具')) : null,
      t.seconds ? h('div', { className: 'cs-mut' }, `${t.seconds}s`) : null,
      t.error ? h('div', { className: 'cs-err' }, t.error) : null)
  }) : h(Empty, null, '问点什么：回答末尾会列出这一轮调用的工具（输出节点可关掉）。'), h('div', { ref: bottom })),
  h('div', { className: 'cs-row' }, h('div', { style: { flex: 1 } }, h(TextArea, { value: message, onChange: setMessage, rows: 2, placeholder: '输入问题' })),
    h(Button, { kind: 'pri', onClick: send, busy: sending }, '发送')),
  sessionId ? h('div', { className: 'cs-mut cs-mono' }, `会话 ${sessionId}`) : null,
  failed ? h(Note, { tone: 'warn' }, 'Runtime 返回了错误，但 AgentCore 不把出错的原因传回来：点「Runtime 日志」看这一轮在日志里写了什么。') : null,
  logs ? h('div', null,
    h('div', { className: 'cs-row', style: { justifyContent: 'space-between' } },
      h('span', { className: 'cs-mut' }, `${logs.logGroup || ''} · ${logs.scope || ''} · 最近 ${logs.minutes || 120} 分钟的工具调用、提醒和错误`),
      h('span', { className: 'cs-row' }, h(Button, { onClick: () => loadLogs(logs.scope === '全部会话') }, '刷新'), h(Button, { onClick: () => loadLogs(true) }, '全部会话'),
        h(Button, { onClick: () => setLogs(null) }, '收起'))),
    logs.error ? h('div', { className: 'cs-err' }, logs.error) : null,
    h('pre', { className: 'cs-pre', style: { maxHeight: 260, overflow: 'auto', whiteSpace: 'pre-wrap' } },
      listOf(logs.events).map((e) => `${e.at.slice(11, 19)} ${e.session ? e.session.slice(0, 14) : ''}  ${e.message}`).join('\n')
        || (logs.missing ? '这个 Runtime 还没有日志' : '还没有记录：CloudWatch 日志通常晚几十秒才查得到，过一会儿点「刷新」'))) : null,
  h(ErrorLine, { error }))
}

function DeployPanel({ project, dirty, valid, save, reload, sample, go, setSelected }) {
  const { wid, me } = useCtx()
  const admin = me && me.role === 'admin'
  const deployments = listOf(project.deploymentsList)
  const runtimes = []
  for (const d of deployments) if (d.runtimeId && d.jobStatus === 'succeeded' && !runtimes.some((r) => r.runtimeId === d.runtimeId)) runtimes.push(d)
  const [target, setTarget] = useState(runtimes[0] ? runtimes[0].runtimeId : '')
  const [name, setName] = useState(() => { const s = slug(project.name); return /^[a-z]/.test(s) ? `studio_${s}`.slice(0, 48) : `studio_${project.id.slice(3)}` })
  const [smoke, setSmoke] = useState('y')
  const [smokePrompt, setSmokePrompt] = useState('')
  const running = deployments.find((d) => d.jobStatus === 'running')
  const [jobId, setJobId] = useState(running ? running.jobId : null)
  const [chatWith, setChatWith] = useState(runtimes[0] ? runtimes[0].runtimeId : null)
  const [live, setLive] = useState({})
  const { busy, error, run } = useAction()
  const job = useJob(jobId)
  useEffect(() => {
    if (job && job.status !== 'running') {
      reload()
      if (job.status === 'succeeded' && job.result && job.result.runtimeId) { setChatWith(job.result.runtimeId); setTarget(job.result.runtimeId) }
    }
  }, [job && job.status])
  const loadLive = () => call('GET', `/workspaces/${wid}/deployments`).then((r) => setLive(Object.fromEntries(listOf(r.deployments).map((d) => [d.runtimeId, d])))).catch(() => {})
  useEffect(() => { loadLive() }, [project.id, deployments.length, job && job.status])
  const start = () => run('deploy', async () => {
    if (dirty && !(await save('部署前保存'))) throw new Error('没能保存画布，没有部署')
    const body = intoExisting ? { runtimeId: target } : { name: name.trim() }
    body.smoke = smoke === 'y'
    if (smokePrompt.trim()) body.smokePrompt = smokePrompt.trim()
    return call('POST', ws(wid, `/projects/${project.id}/deploy`), body)
  }, (j) => `已开始：${j.label}`).then((j) => { if (j) { setJobId(j.id); reload() } })
  const remove = (d) => window.confirm(`删除 Runtime ${d.name}？会删除它的端点、执行角色和源码（几分钟），流程项目保留。`) &&
    run(`del-${d.runtimeId}`, () => call('DELETE', `/workspaces/${wid}/deployments/${d.runtimeId}`), (j) => `已开始：${j.label}`).then((j) => { if (j) setJobId(j.id) })
  const known = Object.keys(live).length > 0
  const targets = runtimes.filter((r) => !known || live[r.runtimeId])
  const intoExisting = targets.some((r) => r.runtimeId === target)
  const chatRuntime = runtimes.find((r) => r.runtimeId === chatWith)
  const status = (rid) => (live[rid] ? live[rid].status : '—')
  return h(Fragment, null,
    h(Card, { title: '部署到 AgentCore Runtime', extra: admin ? h(Button, { kind: 'pri', onClick: start, busy: busy === 'deploy', disabled: !valid || (!intoExisting && !name.trim()) },
      dirty ? '保存并部署' : intoExisting ? '发布新版本' : '部署') : null },
    !admin ? h(Note, { tone: 'warn' }, '只有管理员可以部署。') : null,
    !valid ? h(Note, { tone: 'warn' }, '流程还有错误（见画布下方）：改好才能部署。') : null,
    h('div', { className: 'cs-grid' },
      h(Field, { label: '部署到' }, h(Select, { value: intoExisting ? target : '', onChange: setTarget,
        options: [['', '新的 Runtime'], ...targets.map((r) => [r.runtimeId, `${r.name} 的新版本（现在 v${(live[r.runtimeId] || {}).version || r.runtimeVersion || '?'}，流程 v${r.flowVersion}）`])] })),
      !intoExisting ? h(Field, { label: 'Runtime 名称', hint: '字母开头，字母、数字、下划线（最多 48）' }, h(Input, { value: name, onChange: setName, mono: true })) : null,
      h(Field, { label: '冒烟调用', hint: '就绪后调用一次，结束后停止会话' }, h(Select, { value: smoke, onChange: setSmoke, options: [['y', '调用一次'], ['n', '不调用']] })),
      smoke === 'y' ? h(Field, { label: '冒烟问题', hint: '留空用输入节点的示例问题' }, h(Input, { value: smokePrompt, onChange: setSmokePrompt, placeholder: sample || '你好' })) : null),
    h(Note, null, `部署的是保存过的版本（${dirty ? '画布有未保存的修改：会先保存成新版本' : `流程 v${project.version}`}）。代码 zip 直接部署到 Runtime（Python 3.13，ARM64），requirements.txt 由 CodeBuild 安装；`
      + '执行角色只有调用模型、日志、X-Ray，以及流程里知识库的检索权限。'),
    h(ErrorLine, { error })),
  jobId && job ? h(Card, { title: `${job.label}（${job.id}）`, extra: h(Chip, { tone: JOB_TONE[job.status] }, JOB_LABEL[job.status] || job.status) }, h(JobView, { job })) : null,
  h(Card, { title: `这个项目的部署（${deployments.length}）`, extra: h(Button, { onClick: () => { reload(); loadLive() } }, '刷新') },
    h(Table, { head: ['Runtime', '流程版本', 'Runtime 版本', '任务', '状态', '时间', ''], rows: deployments.map((d, i) => ({ key: d.jobId || i, cells: [
      h('b', null, d.name), `v${d.flowVersion}`, d.runtimeVersion || '—', h(Chip, { tone: JOB_TONE[d.jobStatus] }, JOB_LABEL[d.jobStatus] || d.jobStatus || '—'),
      d.runtimeId ? h(Chip, { tone: STATUS_TONE[status(d.runtimeId)] }, status(d.runtimeId)) : '—', (d.at || '').slice(0, 19).replace('T', ' '),
      h('span', { className: 'cs-row' },
        h(Button, { onClick: () => setJobId(d.jobId) }, '任务'),
        d.runtimeId && d.jobStatus === 'succeeded' ? h(Button, { onClick: () => setChatWith(d.runtimeId) }, '对话') : null,
        admin && d.runtimeId && d.jobStatus === 'succeeded' && live[d.runtimeId] ? h(Button, { kind: 'danger', onClick: () => remove(d), busy: busy === `del-${d.runtimeId}` }, '删除 Runtime') : null)] })),
    empty: '还没有部署过。' })),
  chatRuntime && live[chatWith] && live[chatWith].status === 'READY' ? h(RuntimeChat, { runtime: chatRuntime, sample, go, setSelected })
    : chatRuntime ? h(Note, null, `${chatRuntime.name} 现在是 ${status(chatWith)}：就绪后可以在这里对话。`) : null)
}

// -- versions and projects -------------------------------------------------------------------------------------------

function VersionsPanel({ project, baseVersion, openVersion, rename, removeProject, admin }) {
  const [name, setName] = useState(project.name)
  const [description, setDescription] = useState(project.description || '')
  const deployed = (v) => listOf(project.deploymentsList).filter((d) => d.flowVersion === v && d.jobStatus === 'succeeded').map((d) => d.name)
  return h(Fragment, null,
    h(Card, { title: `版本（${listOf(project.versionsList).length}）` }, h(Table, { head: ['版本', '保存时间', '保存人', '说明', '内容', '部署到', ''],
      rows: listOf(project.versionsList).map((v) => ({ key: v.version, cells: [h('b', null, `v${v.version}`), (v.savedAt || '').slice(0, 19).replace('T', ' '), v.savedBy || '—', v.note || '—',
        v.summary ? `${v.summary.agents} 个 Agent · ${v.summary.tools} 个工具${v.summary.graphMode ? ' · Graph' : ''}` : '—', [...new Set(deployed(v.version))].join('、') || '—',
        v.version === baseVersion ? h(Chip, { tone: 'ok' }, '画布上') : h(Button, { onClick: () => openVersion(v.version) }, '打开')] })) }),
    h(Note, null, '打开旧版本会把它放到画布上；保存后它成为最新的版本（旧版本都保留，最多 50 个）。')),
    h(Card, { title: '项目信息', extra: h(Button, { onClick: () => rename(name, description) }, '保存信息') },
      h(Field, { label: '名称' }, h(Input, { value: name, onChange: setName })),
      h(Field, { label: '描述' }, h(TextArea, { value: description, onChange: setDescription, rows: 2 })),
      project.importedFrom ? h(Note, null, `从 Runtime ${project.importedFrom.runtimeId}（版本 ${project.importedFrom.runtimeVersion}）导入`) : null,
      admin ? h(Button, { kind: 'danger', onClick: removeProject }, '删除项目') : null))
}

function Projects({ projects, catalog, onOpen, reload }) {
  const { wid, me } = useCtx()
  const admin = me && me.role === 'admin'
  const [name, setName] = useState('')
  const [runtimes, setRuntimes] = useState([])
  const [rid, setRid] = useState('')
  const { busy, error, run } = useAction()
  useEffect(() => { call('GET', `/workspaces/${wid}/deployments`).then((r) => setRuntimes(listOf(r.deployments).filter((d) => d.source === 'zip' && d.status !== 'MISSING'))).catch(() => {}) }, [wid])
  const create = (tid) => run('create', () => call('POST', ws(wid, '/projects'), { name: name.trim() || catalog.templates.find((t) => t.id === tid).name, template: tid }),
    (p) => `已创建 ${p.name}`).then((p) => { if (p) onOpen(p.id) })
  const importRuntime = () => run('import', () => call('POST', ws(wid, '/open-runtime'), { runtimeId: rid }),
    (r) => (r.imported ? '已从 Runtime 导入成新项目' : '打开了部署它的项目')).then((r) => { if (r) { reload(); onOpen(r.projectId, r.version) } })
  const remove = (p) => window.confirm(`删除项目 ${p.name} 和它的全部版本？（已部署的 Runtime 不受影响）`) && run('del', () => call('DELETE', ws(wid, `/projects/${p.id}`)), '已删除').then(reload)
  return h(Fragment, null,
    h(Card, { title: '新建项目' },
      h(Field, { label: '项目名称（可选，默认用模板名）' }, h(Input, { value: name, onChange: setName, placeholder: '订单客服' })),
      h('div', { className: 'cs-grid' }, catalog.templates.map((t) => h('div', { key: t.id, style: { border: '1px solid #d9e2ec', borderRadius: 8, padding: '10px 12px' } },
        h('b', null, t.name), h('div', { className: 'cs-mut', style: { margin: '4px 0 8px', minHeight: 36 } }, t.description),
        h(Button, { onClick: () => create(t.id), busy: busy === 'create' }, '用这个开始')))),
      h(ErrorLine, { error })),
    h(Card, { title: `项目（${listOf(projects).length}）` }, projects === null ? h(Empty, null, '加载中…') : h(Table, { head: ['名称', '版本', '内容', '部署', '更新', ''],
      rows: projects.map((p) => ({ key: p.id, cells: [h('b', null, p.name), `v${p.version}`, p.summary ? `${p.summary.agents} 个 Agent · ${p.summary.tools} 个工具${p.summary.graphMode ? ' · Graph' : ''}` : '—',
        p.deployments || '—', (p.updatedAt || '').slice(0, 19).replace('T', ' '),
        h('span', { className: 'cs-row' }, h(Button, { kind: 'pri', onClick: () => onOpen(p.id) }, '打开'), admin ? h(Button, { kind: 'danger', onClick: () => remove(p) }, '删除') : null)] })),
      empty: '还没有项目：从上面的模板开始。' })),
    h(Card, { title: '打开一个已部署的 Runtime', extra: h(Button, { onClick: importRuntime, busy: busy === 'import', disabled: !rid }, '在 Studio 打开') },
      h(Select, { value: rid, onChange: setRid, options: [['', runtimes.length ? '选一个控制台部署的 Runtime' : '没有控制台部署的代码 Runtime'], ...runtimes.map((r) => [r.runtimeId, `${r.name} · ${r.status} · v${r.version || '?'}`])] }),
      h(Note, null, 'Studio 部署的 zip 里带着流程（studio_flow.json）：在这个控制台找得到项目就打开它，找不到就从 Runtime 的源码导入成新项目。')))
}

// -- the page -----------------------------------------------------------------------------------------------------------

function StudioPage({ go, setSelected }) {
  const { wid, workspace, me, toast } = useCtx()
  const admin = me && me.role === 'admin'
  const [catalog, setCatalog] = useState(null)
  const [projects, setProjects] = useState(null)
  const [project, setProject] = useState(null)
  const [flow, setFlowState] = useState(null)
  const [saved, setSaved] = useState(null)
  const [baseVersion, setBaseVersion] = useState(null)
  const [selection, setSelection] = useState(null)
  const [tab, setTab] = useState('canvas')
  const [preview, setPreview] = useState(null)
  const [kbs, setKbs] = useState(null)
  const [note, setNote] = useState('')
  const [draft, setDraft] = useState(null)
  const [fitSignal, setFitSignal] = useState(0)
  const [failure, setFailure] = useState(null)
  const flowRef = useRef(null)
  flowRef.current = flow
  const history = useRef({ past: [], future: [], key: null, at: 0 })
  const ticket = useRef(0)
  const { busy, run } = useAction()

  const loadProjects = () => call('GET', ws(wid, '/projects')).then((r) => { setProjects(listOf(r.projects)); return listOf(r.projects) })
  const openProject = async (pid, version) => {
    if (flowRef.current && project && !same(flowRef.current, saved) && pid !== project.id && !window.confirm('画布有未保存的修改，确定离开？（草稿留在这个浏览器里）')) return
    try {
      const p = await call('GET', ws(wid, `/projects/${pid}${version ? `?version=${version}` : ''}`))
      setProject(p); setFlowState(p.flow); setSaved(p.flow); setBaseVersion(p.flowVersion); setSelection(null); setPreview(null); setTab('canvas')
      history.current = { past: [], future: [], key: null, at: 0 }
      window.localStorage.setItem(LAST_KEY(wid), pid)
      let stored = null
      try { stored = JSON.parse(window.localStorage.getItem(DRAFT_KEY(wid, pid)) || 'null') } catch (_) { stored = null }
      setDraft(stored && stored.flow && !same(stored.flow, p.flow) && stored.base === p.flowVersion ? stored : null)
      setFitSignal((n) => n + 1)
    } catch (err) {
      if (err.status === 404) window.localStorage.removeItem(LAST_KEY(wid))
      setFailure(err)
    }
  }
  const reloadProject = () => project && call('GET', ws(wid, `/projects/${project.id}?version=${baseVersion}`)).then((p) => setProject((cur) => ({ ...p, flow: cur ? cur.flow : p.flow }))).catch(() => {})

  useEffect(() => {
    setCatalog(null); setProjects(null); setProject(null); setFlowState(null); setSaved(null); setKbs(null); setFailure(null)
    if (!wid) return
    call('GET', ws(wid, '/catalog')).then(setCatalog).catch(setFailure)
    loadProjects().then((list) => {
      const last = window.localStorage.getItem(LAST_KEY(wid))
      if (last && list.some((p) => p.id === last)) openProject(last)
    }).catch(setFailure)
  }, [wid])

  // the canvas is checked and generated on the server as you edit
  useEffect(() => {
    if (!flow || !project) return undefined
    const mine = ++ticket.current
    const timer = setTimeout(() => {
      call('POST', ws(wid, '/preview'), { flow, project: { id: project.id, name: project.name, version: baseVersion } })
        .then((r) => { if (mine === ticket.current) setPreview(r) })
        .catch((err) => { if (mine === ticket.current) setPreview({ ok: false, issues: [{ level: 'error', message: err.message }], tools: {}, files: null }) })
    }, 400)
    if (!same(flow, saved)) window.localStorage.setItem(DRAFT_KEY(wid, project.id), JSON.stringify({ flow, base: baseVersion, at: new Date().toISOString() }))
    return () => clearTimeout(timer)
  }, [project && project.id, baseVersion, flow && JSON.stringify(flow)])

  const edit = useCallback((fn, key) => {
    const current = flowRef.current
    if (!current) return
    const next = fn(current)
    if (!next || next === current) return
    const hist = history.current
    const now = Date.now()
    if (!key || key !== hist.key || now - hist.at > 1500) { hist.past = [...hist.past.slice(-59), current]; hist.future = [] }
    hist.key = key; hist.at = now
    flowRef.current = next
    setFlowState(next)
  }, [])
  const undo = () => { const hist = history.current; if (!hist.past.length) return; hist.future.push(flowRef.current); const prev = hist.past.pop(); hist.key = null; flowRef.current = prev; setFlowState(prev) }
  const redo = () => { const hist = history.current; if (!hist.future.length) return; hist.past.push(flowRef.current); const next = hist.future.pop(); hist.key = null; flowRef.current = next; setFlowState(next) }

  const addNode = (type, x, y) => {
    const f = flowRef.current
    const data = JSON.parse(JSON.stringify(catalog.defaults[type]))
    const count = f.nodes.filter((n) => n.type === type).length
    if (EXECUTABLE.includes(type)) data.name = uniqueName(f, { agent: 'agent', orchestrator: 'lead', swarm: 'team' }[type])
    if (count) data.label = `${data.label} ${count + 1}`
    if (type === 'custom-tool') data.code = catalog.customToolTemplate
    let px = x
    let py = y
    if (px === undefined) {
      const xs = f.nodes.map((n) => n.x)
      px = xs.length ? Math.max(...xs) + 40 : 60
      py = f.nodes.length ? Math.min(...f.nodes.map((n) => n.y)) + 40 * (count % 5) : 120
    }
    const node = { id: newId(type), type, x: px, y: py, data }
    edit((cur) => ({ ...cur, nodes: [...cur.nodes, node] }), `add:${node.id}`)
    setSelection({ kind: 'node', id: node.id })
  }
  const removeSelected = () => {
    if (!selection) return
    if (selection.kind === 'node') edit((f) => ({ ...f, nodes: f.nodes.filter((n) => n.id !== selection.id), edges: f.edges.filter((e) => e.source !== selection.id && e.target !== selection.id) }), `del:${selection.id}`)
    else edit((f) => ({ ...f, edges: f.edges.filter((e) => e.id !== selection.id) }), `del:${selection.id}`)
    setSelection(null)
  }
  const duplicate = () => {
    const n = flowRef.current.nodes.find((q) => q.id === (selection && selection.id))
    if (!n) return
    const data = { ...n.data }
    if (EXECUTABLE.includes(n.type)) data.name = uniqueName(flowRef.current, (data.name || 'agent').replace(/_\d+$/, ''))
    const copy = { ...n, id: newId(n.type), x: n.x + 30, y: n.y + 30, data: { ...data, label: `${n.data.label} 副本` } }
    edit((f) => ({ ...f, nodes: [...f.nodes, copy] }), `add:${copy.id}`)
    setSelection({ kind: 'node', id: copy.id })
  }
  const save = (why) => run('save', async () => {
    const p = await call('PUT', ws(wid, `/projects/${project.id}`), { flow: flowRef.current, note: why || note })
    setProject(p); setSaved(p.flow); setBaseVersion(p.flowVersion); setFlowState(p.flow); flowRef.current = p.flow; setNote(''); setDraft(null)
    window.localStorage.removeItem(DRAFT_KEY(wid, project.id))
    loadProjects()
    return p
  }, (p) => (p.saved ? `已保存为 v${p.version}` : '没有变化'))
  const bundle = () => run('bundle', async () => {
    const out = await call('POST', ws(wid, '/bundle'), { flow: flowRef.current, project: { id: project.id, name: project.name, version: baseVersion } })
    download(out.filename, out.archive)
    return out
  })
  const pickIssue = (i) => { setTab('canvas'); if (i.node) setSelection({ kind: 'node', id: i.node }); else if (i.edge) setSelection({ kind: 'edge', id: i.edge }) }
  const loadKbs = () => call('GET', ws(wid, '/knowledge-bases')).then((r) => setKbs(listOf(r.knowledgeBases))).catch((err) => { setKbs([]); toast('error', err.message) })

  if (!workspace) return h(Empty, null, '先在「设置 → 工作区」添加一个 AWS 账号和区域。')
  if (!catalog) return h(Fragment, null, h('h1', null, 'Studio'), h(ErrorLine, { error: failure }), h(Empty, null, '加载中…'))
  const style = h('style', null, '.cs-st-port{transition:r .08s}.cs-st-port:hover{r:7.5px}.cs-st-canvas:focus{box-shadow:0 0 0 2px #bcdcf5}')
  if (!project || !flow) {
    return h(Fragment, null, style, h('h1', null, 'Studio'),
      h(Note, null, '在画布上搭一个 Strands Agent：模型、Prompt、工具、知识库、子 Agent、Swarm 或 Graph；Studio 生成 Python 代码，部署成 AgentCore Runtime，然后就在这里对话。'),
      h(ErrorLine, { error: failure }),
      h(Projects, { projects, catalog, onOpen: openProject, reload: loadProjects }))
  }
  const dirty = !same(flow, saved)
  const issues = preview ? listOf(preview.issues) : null
  const errors = listOf(issues).filter((i) => i.level === 'error')
  const warnings = listOf(issues).filter((i) => i.level === 'warning')
  const marks = { nodes: {}, edges: {} }
  for (const i of listOf(issues)) {
    if (i.node) { const m = marks.nodes[i.node] || (marks.nodes[i.node] = { errors: [], warnings: [] }); m[i.level === 'error' ? 'errors' : 'warnings'].push(i.message) }
    if (i.edge) (marks.edges[i.edge] || (marks.edges[i.edge] = [])).push(i.message)
  }
  const tools = (preview && preview.tools) || {}
  const sample = ((flow.nodes.find((n) => n.type === 'input') || {}).data || {}).sample || ''
  const valid = !!(preview && preview.ok)
  return h(Fragment, null, style,
    h('div', { className: 'cs-row', style: { justifyContent: 'space-between', marginBottom: 8 } },
      h('div', { className: 'cs-row' },
        h('h1', { style: { margin: 0 } }, project.name),
        h(Chip, { tone: dirty ? 'warn' : 'ok', title: dirty ? '画布和保存的版本不一样' : '已保存' }, `v${baseVersion}${baseVersion !== project.version ? `（最新 v${project.version}）` : ''}${dirty ? ' · 未保存' : ''}`),
        h(Chip, { tone: !preview ? undefined : errors.length ? 'bad' : warnings.length ? 'warn' : 'ok' },
          !preview ? '检查中' : errors.length ? `${errors.length} 个错误` : warnings.length ? `可以部署 · ${warnings.length} 个提醒` : '可以部署'),
        flow.graphMode ? h(Chip, { tone: 'info' }, 'Graph 模式') : null),
      h('div', { className: 'cs-row' },
        h(Button, { onClick: undo, disabled: !history.current.past.length, title: '撤销' }, '撤销'),
        h(Button, { onClick: redo, disabled: !history.current.future.length, title: '重做' }, '重做'),
        h(Button, { onClick: () => edit((f) => ({ ...f, graphMode: !f.graphMode }), 'graph'), title: '打开后，Agent 之间可以连线表示先后依赖（Strands Graph）' },
          flow.graphMode ? '关闭 Graph 模式' : '打开 Graph 模式'),
        h(Button, { onClick: bundle, busy: busy === 'bundle', disabled: !valid }, '下载 zip'),
        h(Button, { onClick: () => { if (!dirty || window.confirm('画布有未保存的修改，确定离开？（草稿留在这个浏览器里）')) { setProject(null); setFlowState(null); window.localStorage.removeItem(LAST_KEY(wid)); loadProjects() } } }, '全部项目'))),
    draft ? h(Note, { tone: 'warn' }, `这个浏览器里有一份 ${String(draft.at || '').slice(0, 19).replace('T', ' ')} 的未保存草稿。`, ' ',
      h('a', { className: 'cs-click', onClick: () => { edit(() => draft.flow, 'draft'); setDraft(null) } }, '恢复草稿'), ' · ',
      h('a', { className: 'cs-click', onClick: () => { window.localStorage.removeItem(DRAFT_KEY(wid, project.id)); setDraft(null) } }, '丢弃')) : null,
    h(Tabs, { value: tab, onChange: setTab, options: [['canvas', '画布'], ['code', '代码'], ['deploy', '部署与对话'], ['versions', `版本（${listOf(project.versionsList).length}）`]] }),
    tab === 'canvas' ? h(Fragment, null,
      h('div', { style: { display: 'flex', gap: 10, alignItems: 'flex-start' } },
        h(Palette, { flow, onAdd: (type) => addNode(type) }),
        h(Canvas, { flow, catalog, edit, selection, setSelection, marks, tools, onNotice: (m) => toast('error', m), onAdd: addNode, fitSignal, undo, redo }),
        h(Inspector, { flow, catalog, selection, edit, remove: removeSelected, duplicate, issues: listOf(issues), tools, kbs, loadKbs })),
      h('div', { className: 'cs-row', style: { marginTop: 10 } },
        h('div', { style: { flex: 1 } }, h(Input, { value: note, onChange: setNote, placeholder: '这次改了什么（可选，记在版本里）' })),
        h(Button, { kind: 'pri', onClick: () => save(), busy: busy === 'save', disabled: !dirty }, dirty ? '保存为新版本' : '已保存')),
      h(Card, { title: '检查结果' }, h(Issues, { issues, onPick: pickIssue }))) : null,
    tab === 'code' ? h(CodeView, { preview, onDownload: bundle, busy: busy === 'bundle' }) : null,
    tab === 'deploy' ? h(DeployPanel, { key: project.id, project, dirty, valid, save, reload: reloadProject, sample, go, setSelected }) : null,
    tab === 'versions' ? h(VersionsPanel, { key: `${project.id}:${project.version}`, project, baseVersion, admin,
      openVersion: (v) => { if (!dirty || window.confirm('画布有未保存的修改，确定打开别的版本？')) openProject(project.id, v) },
      rename: (name, description) => run('rename', () => call('PUT', ws(wid, `/projects/${project.id}`), { name, description }), '已保存').then((p) => { if (p) { setProject((cur) => ({ ...p, flow: cur.flow })); loadProjects() } }),
      removeProject: () => window.confirm(`删除项目 ${project.name} 和它的全部版本？（已部署的 Runtime 不受影响）`) &&
        run('delete', () => call('DELETE', ws(wid, `/projects/${project.id}`)), '已删除').then((r) => { if (r) { setProject(null); setFlowState(null); window.localStorage.removeItem(LAST_KEY(wid)); loadProjects() } }) }) : null)
}

export default { id: 'studio', label: 'Studio', group: '构建', Page: StudioPage }
