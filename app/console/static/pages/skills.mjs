// Skill Lab: a library of Agent Skills (SKILL.md versions in the console bucket), task sets with rubrics (a task may
// give the agent input files, uploaded once by content), evaluation on the real agent with and without the skill
// (lift, wins and losses, how often the model loaded it), and training: a reflection loop that rewrites SKILL.md from
// failures, gated on val, measured on test, then published. The agent is a Harness or a runtime deployed from the
// Claude Agent SDK template; the latter gets a task's files in its working directory and its answers bring the files
// the turn made (a spreadsheet's cells, a document's or a deck's text, an image), which the judge reads — and, for a
// picture, sees — and the results show (pictures as thumbnails).
import React from 'react'
import { call, useCtx, listOf, Card, Button, Field, Input, TextArea, Select, Chip, Note, Empty, ErrorLine, Table, useAction, useJob, Tabs, JOB_TONE,
  JOB_LABEL } from '../ui.mjs'
import { listUnder } from './shared.mjs'

const { useState, useEffect, createElement: h, Fragment } = React
const ws = (wid, path) => `/workspaces/${wid}/skill-lab${path}`
const useList = listUnder(ws)
// Another team's agent: the backend refuses without acknowledged, the person confirms, and only then is it sent again.
async function confirmed(send, what) {
  try { return await send(undefined) } catch (err) {
    if (!String(err.message || '').includes('acknowledged') || !window.confirm(`${what} 不是这个控制台创建的：确定继续？（会改它的技能或执行角色的权限）`)) throw err
    return send(true)
  }
}
const JUDGE = 'us.anthropic.claude-sonnet-5-5'
const OPTIMIZER = 'us.anthropic.claude-opus-5-5'
const ARM = { with: '有技能', without: '没技能', seed: '初始版本', best: '最佳版本' }
const VERDICT_TONE = { helps: 'ok', hurts: 'warn', not_loaded: 'warn' }
const KIND = { harness: 'Harness', runtime: 'Claude Agent SDK' }
const pct = (v) => (v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`)
const num = (v) => (v === null || v === undefined ? '—' : Number(v).toFixed(2))
const size = (n) => (n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`)
const TEMPLATE = '---\nname: my-skill\ndescription: 这个技能管什么；Agent 在用户问到什么时应该加载它（写得越贴近用户的原话越容易被加载）。\n---\n# 标题\n\n1. 第一条规则\n2. 第二条规则\n'
const MIME = { xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation', pdf: 'application/pdf', csv: 'text/csv', md: 'text/markdown',
  txt: 'text/plain', json: 'application/json' }
// A task's input files: Launchpad's types and limits (the server checks them again).
const ACCEPT = '.xlsx,.pdf,.png,.jpg,.jpeg,.webp,.md,.txt,.csv'
const MAX_FILE = 25 * 1024 * 1024
const IMAGE_MIME = { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp' }
const readFile = (f) => new Promise((resolve, reject) => {
  const reader = new FileReader()
  reader.onload = () => resolve({ name: f.name, contentBase64: String(reader.result).split(',')[1] || '' })
  reader.onerror = reject
  reader.readAsDataURL(f)
})
const fileChip = (f) => h(Chip, { key: f.path, title: `${f.asset || ''}` }, `${f.path} · ${size(f.bytes || 0)}`)

// The pictures of a file a turn made, as the page shows them: an image's thumbnail (or, from an older template, its
// own small bytes) and the thumbnails of the pictures inside a document.
function pictures(a) {
  const out = []
  const src = (p) => (p && p.base64 ? `data:${IMAGE_MIME[p.format] || 'image/png'};base64,${p.base64}` : null)
  if (a.type === 'image') {
    const ext = String(a.path || '').split('.').pop().toLowerCase()
    const own = (a.image && src(a.image.thumb)) || (a.base64 && IMAGE_MIME[ext] ? `data:${IMAGE_MIME[ext]};base64,${a.base64}` : null)
    if (own) out.push({ key: a.path, src: own, title: `${a.path}${a.image && a.image.width ? ` · ${a.image.width}×${a.image.height}` : ''}` })
  }
  listOf(a.images).forEach((p, i) => { const s = src(p.thumb); if (s) out.push({ key: `${a.path}#${i}`, src: s, title: `${a.path} → ${p.name}${p.width ? ` · ${p.width}×${p.height}` : ''}` }) })
  return out
}

function Thumbs({ list, height }) {
  if (!list.length) return null
  return h('div', { className: 'cs-row', style: { flexWrap: 'wrap', gap: 8, margin: '6px 0' } }, list.map((p) => h('figure', { key: p.key, style: { margin: 0 } },
    h('img', { src: p.src, alt: p.title, title: p.title, style: { maxHeight: height, maxWidth: height * 2, border: '1px solid #d9e2ec', borderRadius: 4, background: '#fff', display: 'block' } }),
    height > 60 ? h('figcaption', { className: 'cs-mut', style: { fontSize: 11 } }, p.title) : null)))
}

// What a skill can be evaluated on: the workspace's Harnesses and its Claude Agent SDK runtimes (deployed from the template).
function useTargets(wid) {
  const [targets, setTargets] = useState([])
  useEffect(() => { if (wid) call('GET', ws(wid, '/targets')).then((r) => setTargets(listOf(r.targets))).catch(() => {}) }, [wid])
  return targets
}

const targetKey = (t) => `${t.kind}:${t.id}`
const targetOptions = (targets) => targets.map((t) => [targetKey(t), `${t.name} · ${KIND[t.kind]}${t.note ? '（暂不可用）' : ''}`])
const targetBody = (key) => {
  const [kind, id] = String(key || '').split(/:(.*)/s)
  return kind === 'runtime' ? { runtimeId: id } : kind === 'harness' ? { harnessId: id } : {}
}
const jobTarget = (params) => (params.runtimeId ? `runtime:${params.runtimeId}` : params.harnessId ? `harness:${params.harnessId}` : '')

// -- results ------------------------------------------------------------------------------------------------------------

function Outcome({ x }) {
  if (!x) return '—'
  const files = x.artifacts ? listOf(x.artifacts) : null
  const first = files ? files.flatMap(pictures)[0] : null
  return h('span', { className: 'cs-row' }, h(Chip, { tone: x.invalid ? 'warn' : x.pass ? 'ok' : 'bad' }, x.invalid ? '无效' : x.pass ? '通过' : '未过'), num(x.score),
    x.skillLoaded ? h(Chip, { tone: 'info', title: '这次回答加载了这个技能（Harness 的 skills 工具 / Claude Agent SDK 的 Skill 工具）' }, '加载了') : null,
    files ? h(Chip, { tone: files.length ? undefined : 'warn', title: files.map((a) => a.path).join('\n') || '这一轮没有生成或改动文件' },
      files.length ? `文件 ${files.length}` : '没有文件') : null,
    first ? h('img', { src: first.src, title: first.title, alt: first.title, style: { height: 36, maxWidth: 72, objectFit: 'contain', border: '1px solid #d9e2ec', borderRadius: 3, background: '#fff' } }) : null)
}

// A spreadsheet-like preview: row numbers and column letters, the cells as the judge read them (formulas as written).
const letters = (n) => { let s = ''; for (let i = n; i > 0; i = Math.floor((i - 1) / 26)) s = String.fromCharCode(65 + ((i - 1) % 26)) + s; return s }

function Sheet({ sheet }) {
  const rows = listOf(sheet.rows)
  const width = Math.max(0, ...rows.map((r) => listOf(r).length))
  return h(Fragment, null,
    h('div', { className: 'cs-mut' }, `${sheet.name} · ${sheet.nrows} 行 × ${sheet.ncols} 列${sheet.truncated ? `（只显示前 ${rows.length} 行、${width} 列）` : ''}`),
    h(Table, { head: ['', ...Array.from({ length: width }, (_, i) => letters(i + 1))], empty: '（空表）',
      rows: rows.map((r, i) => ({ key: i, cells: [h('span', { className: 'cs-mut' }, i + 1), ...Array.from({ length: width }, (_, j) => h('span', {
        className: String(listOf(r)[j] || '').startsWith('=') ? 'cs-mono' : undefined }, listOf(r)[j] || ''))] })) }))
}

function Artifact({ a }) {
  const [full, setFull] = useState(false)
  const [sheet, setSheet] = useState(0)
  const sheets = listOf(a.table && a.table.sheets)
  const name = String(a.path || '').split('/').pop()
  const text = String(a.text || '')
  const pics = pictures(a)
  const unseen = [a.image, ...listOf(a.images)].filter((p) => p && (p.error || p.note) && !p.thumb)
  return h('div', { style: { borderLeft: '3px solid #d9e2ec', paddingLeft: 10, margin: '8px 0' } },
    h('div', { className: 'cs-row' }, h('b', { className: 'cs-mono' }, a.path), h(Chip, null, a.type), h('span', { className: 'cs-mut' }, size(a.bytes || 0)),
      h(Chip, { tone: a.status === 'changed' ? 'warn' : 'ok' }, a.status === 'changed' ? '改动' : '新建'),
      a.input ? h(Chip, { tone: 'warn', title: '这是题目给的输入文件，这一轮改动了它' }, '输入文件') : null,
      a.image && a.image.width ? h('span', { className: 'cs-mut' }, `${a.image.width}×${a.image.height}`) : null,
      a.sheets ? h('span', { className: 'cs-mut' }, `${a.sheets} 个工作表`) : null, a.pages ? h('span', { className: 'cs-mut' }, `${a.pages} 页`) : null,
      a.slides ? h('span', { className: 'cs-mut' }, `${a.slides} 张幻灯片`) : null,
      listOf(a.images).length ? h('span', { className: 'cs-mut' }, `${a.imagesTotal || listOf(a.images).length} 张图片`) : null,
      a.base64 ? h('a', { href: `data:${MIME[a.type] || IMAGE_MIME[String(a.path).split('.').pop().toLowerCase()] || 'application/octet-stream'};base64,${a.base64}`, download: name }, '下载') : null),
    a.error ? h('div', { className: 'cs-err' }, `读不了这个文件：${a.error}`) : null,
    h(Thumbs, { list: pics, height: 180 }),
    unseen.length ? h('div', { className: 'cs-mut' }, `没有预览：${unseen.map((p) => `${p.name || a.path}（${p.error || p.note}）`).join('；')}`) : null,
    sheets.length ? h(Fragment, null,
      sheets.length > 1 ? h(Tabs, { value: String(sheet), onChange: (v) => setSheet(Number(v)), options: sheets.map((s, i) => [String(i), s.name]) }) : null,
      h(Sheet, { sheet: sheets[Math.min(sheet, sheets.length - 1)] })) : null,
    text ? h(Fragment, null, h('div', { className: 'cs-row' }, h('span', { className: 'cs-mut' }, sheets.length ? '裁判读到的内容' : '内容（节选）'),
      text.length > 1500 ? h(Button, { onClick: () => setFull(!full) }, full ? '收起' : '全部') : null,
      a.truncated ? h('span', { className: 'cs-mut' }, '（运行时已截断）') : null),
    h('pre', { className: 'cs-pre' }, full ? text : text.slice(0, 1500) + (text.length > 1500 ? '\n…' : ''))) : (a.error || sheets.length || pics.length ? null : h(Note, null, '这种文件没有文本视图。')))
}

function Artifacts({ list, total }) {
  const items = listOf(list)
  return h('div', null, h('div', { className: 'cs-mut' }, items.length ? `这一轮生成或改动的文件（${total && total > items.length ? `共 ${total} 个，列出 ${items.length} 个` : `${items.length} 个`}）`
    : '这一轮没有生成或改动文件'), items.map((a) => h(Artifact, { key: a.path, a })))
}

function Arms({ arms, order }) {
  return h(Table, { head: ['', '通过率', '平均得分', '加载了技能', '题数'], rows: order.filter((k) => arms[k]).map((k) => ({ key: k,
    cells: [h('b', null, ARM[k]), pct(arms[k].passRate), num(arms[k].softMean), k === 'without' ? '—' : pct(arms[k].triggerRate),
      `${arms[k].n}${arms[k].invalid ? `（另 ${arms[k].invalid} 题裁判无效）` : ''}${arms[k].errors ? `，${arms[k].errors} 题出错` : ''}`] })) })
}

function Rows({ rows, order }) {
  const [open, setOpen] = useState(null)
  const row = listOf(rows).find((r) => r.id === open)
  return h(Fragment, null,
    h(Table, { head: ['题', ...order.map((k) => ARM[k]), ''], rows: listOf(rows).map((r) => ({ key: r.id, onClick: () => setOpen(open === r.id ? null : r.id),
      cells: [h('span', null, h('b', null, r.id), listOf(r.files).length ? h('span', { className: 'cs-mut', title: listOf(r.files).map((f) => f.path).join('\n') },
        ` · ${listOf(r.files).length} 个输入文件`) : null, h('div', { className: 'cs-mut' }, String(r.question).slice(0, 48))), ...order.map((k) => h(Outcome, { key: k, x: r[k] })),
        open === r.id ? '收起' : '看回答'] })) }),
    row ? h(Card, { title: `${row.id}：${row.question}` },
      listOf(row.files).length ? h('div', { className: 'cs-row' }, h('span', { className: 'cs-mut' }, '题目给的输入文件（答题前放进它的工作目录）'), listOf(row.files).map(fileChip)) : null,
      h('div', { className: 'cs-mut' }, '评分标准'), h('pre', { className: 'cs-pre' }, row.rubric),
      order.filter((k) => row[k]).map((k) => h('div', { key: k }, h('div', { className: 'cs-row' }, h('b', null, ARM[k]), h(Outcome, { x: row[k] }),
        row[k].seconds ? h('span', { className: 'cs-mut' }, `${row[k].seconds}s · 工具 ${listOf(row[k].tools).join('，') || '无'}`
          + `${typeof row[k].costUsd === 'number' ? ` · $${row[k].costUsd.toFixed(3)}` : ''}`) : null),
      h(Note, null, row[k].reason),
      listOf(row[k].judgedImages).length ? h('div', { className: 'cs-mut' }, `裁判看了图片：${row[k].judgedImages.join('；')}`) : null,
      h('pre', { className: 'cs-pre' }, row[k].text || row[k].error || '（没有回答）'),
      row[k].artifacts ? h(Artifacts, { list: row[k].artifacts, total: row[k].artifactsTotal }) : null))) : null)
}

function Verdict({ v }) {
  return v ? h(Note, { tone: VERDICT_TONE[v.key] }, h('b', null, v.text)) : null
}

function Curve({ history }) {
  const pts = listOf(history).filter((x) => typeof x.valScore === 'number')
  if (!pts.length) return null
  const W = 560
  const H = 150
  const pad = 26
  const x = (i) => pad + (pts.length === 1 ? 0 : (i * (W - 2 * pad)) / (pts.length - 1))
  const y = (v) => H - pad - v * (H - 2 * pad)
  let held = pts[0].valScore
  const line = pts.map((p, i) => { if (p.accepted) held = p.valScore; return `${x(i)},${y(held)}` }).join(' ')
  return h('svg', { width: W, height: H, style: { display: 'block', margin: '8px 0' } },
    [0, 0.5, 1].map((v) => h(Fragment, { key: v }, h('line', { x1: pad, y1: y(v), x2: W - pad, y2: y(v), stroke: '#e4e7eb' }),
      h('text', { x: 2, y: y(v) + 4, fontSize: 10, fill: '#829ab1' }, String(v)))),
    h('polyline', { points: line, fill: 'none', stroke: '#2680c2', strokeWidth: 2 }),
    pts.map((p, i) => h('g', { key: i }, h('circle', { cx: x(i), cy: y(p.valScore), r: 5, fill: p.kind === 'seed' ? '#486581' : p.accepted ? '#207227' : '#ab091e' },
      h('title', null, `第 ${p.step} 步：val ${p.valScore}${p.kind === 'seed' ? '（初始）' : p.accepted ? '，接受' : '，拒绝'}`)),
      h('text', { x: x(i) - 3, y: H - 8, fontSize: 10, fill: '#829ab1' }, String(p.step)))))
}

function Diff({ text }) {
  if (!text) return h(Empty, null, '最佳版本就是初始版本：没有改动。')
  return h('pre', { className: 'cs-pre' }, text.split('\n').map((line, i) => h('div', { key: i, style: line.startsWith('+') && !line.startsWith('+++')
    ? { background: '#e3f9e5', color: '#0e5814' } : line.startsWith('-') && !line.startsWith('---') ? { background: '#ffe3e3', color: '#8a041a' } : undefined }, line || ' ')))
}

// -- library ------------------------------------------------------------------------------------------------------------

function SkillDetail({ name, harnesses, onChanged }) { // applying a version to an agent: a Harness (a runtime gets it by publishing, a rebuild)
  const { wid } = useCtx()
  const [doc, setDoc] = useState(null)
  const [version, setVersion] = useState('')
  const [text, setText] = useState('')
  const [note, setNote] = useState('')
  const [target, setTarget] = useState('')
  const { busy, error, run } = useAction()
  const load = (v) => run('load', async () => {
    const d = await call('GET', ws(wid, `/skills/${name}${v ? `?version=${v}` : ''}`))
    setDoc(d); setVersion(d.version); setText(d.text)
  })
  useEffect(() => { load('') }, [name])
  if (!doc) return h(Fragment, null, h(ErrorLine, { error }), h(Empty, null, '加载中…'))
  const save = () => run('save', () => call('POST', ws(wid, '/skills'), { name, text, note }), (r) => (r.created ? `已保存为 ${r.version}（当前版本）` : `和 ${r.version} 一样，没有新建`))
    .then((r) => { if (r) { setNote(''); load(r.version); onChanged() } })
  const current = () => run('cur', () => call('POST', ws(wid, `/skills/${name}/current`), { version }), `${version} 已设为当前版本`).then(() => { load(version); onChanged() })
  const apply = () => run('apply', () => confirmed((ack) => call('POST', ws(wid, `/skills/${name}/apply`), { harnessId: target, version, acknowledged: ack }), '这个 Agent'),
    (r) => `${r.harness} 现在用 ${name} ${r.version}`).then(() => { load(version); onChanged() })
  const remove = (hid) => run(`rm-${hid}`, () => confirmed((ack) => call('POST', ws(wid, `/skills/${name}/remove`), { harnessId: hid, acknowledged: ack }), '这个 Agent'), '已取下').then(() => { load(version); onChanged() })
  const del = () => window.confirm(`删除技能 ${name} 和它的全部版本？`) && run('del', () => call('DELETE', ws(wid, `/skills/${name}`)), '已删除').then(onChanged)
  return h(Fragment, null,
    h(Card, { title: `${name} · ${doc.version}${doc.version === doc.current ? '（当前）' : ''}`, extra: h('span', { className: 'cs-row' },
      h(Select, { value: version, onChange: (v) => load(v), options: listOf(doc.versions).map((v) => [v.version, `${v.version} · ${v.source}${v.version === doc.current ? ' · 当前' : ''}`]) }),
      version !== doc.current ? h(Button, { onClick: current, busy: busy === 'cur' }, '设为当前') : null,
      listOf(doc.agents).length ? null : h(Button, { kind: 'danger', onClick: del }, '删除')) },
      h(Note, null, doc.description), h('div', { className: 'cs-mut cs-mono' }, doc.uri),
      h(TextArea, { value: text, onChange: setText, rows: 16, mono: true }),
      h('div', { className: 'cs-row' }, h(Input, { value: note, onChange: setNote, placeholder: '这次改了什么（可选）' }),
        h(Button, { kind: 'pri', onClick: save, busy: busy === 'save', disabled: text === doc.text }, '另存为新版本')),
      listOf(doc.versions).filter((v) => v.version === version && v.note).map((v) => h(Note, { key: v.version }, `${v.version}：${v.note}`))),
    h(Card, { title: '用在 Agent 上', extra: h(Button, { kind: 'pri', onClick: apply, busy: busy === 'apply', disabled: !target }, `用 ${version}`) },
      listOf(doc.agents).length ? h(Table, { head: ['Agent', '版本', ''], rows: doc.agents.map((a) => ({ key: a.harnessId,
        cells: [h('b', null, a.name), a.version, h(Button, { onClick: () => remove(a.harnessId), busy: busy === `rm-${a.harnessId}` }, '取下')] })) }) : null,
      h(Select, { value: target, onChange: setTarget, options: [['', '选一个 Harness'], ...harnesses.map((a) => [a.id, a.name])] }),
      h(Note, null, '会替换这个 Agent 上该技能的其他版本，并让它的执行角色能读技能库。不是控制台建的 Agent 也会被更新（已确认）。')),
    h(ErrorLine, { error }))
}

function Library() {
  const { wid } = useCtx()
  const harnesses = useTargets(wid).filter((t) => t.kind === 'harness')
  const [list, load, listError] = useList(wid, '/skills', 'skills')
  const [samples] = useList(wid, '/samples', 'samples')
  const [open, setOpen] = useState(null)
  const [form, setForm] = useState({ name: '', text: TEMPLATE, uri: '', project: '', sample: '' })
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const create = () => run('create', () => call('POST', ws(wid, '/skills'), { name: form.name, text: form.text }), (r) => `已创建 ${r.name} ${r.version}`)
    .then((r) => { if (r) { load(); setOpen(r.name) } })
  const chosen = form.sample || (listOf(samples)[0] || {}).name || ''
  const sample = () => run('sample', () => call('POST', ws(wid, '/samples'), { sample: chosen }),
    (r) => `已导入样例技能 ${r.skill.name} 和任务集「${r.taskset.name}」${r.taskset.files ? `（${r.taskset.files} 道题带输入文件）` : ''}`).then(load)
  const fromUri = () => run('uri', () => call('POST', ws(wid, '/skills/import'), { uri: form.uri }), (r) => `已导入 ${r.name} ${r.version}`).then(load)
  const fromPack = () => run('pack', () => call('POST', ws(wid, '/skills/import'), { project: form.project }),
    (r) => `导入 ${listOf(r.imported).filter((x) => !x.error).length} 个技能${listOf(r.imported).some((x) => x.error) ? `，跳过 ${r.imported.filter((x) => x.error).map((x) => x.name).join('、')}` : ''}`).then(load)
  return h(Fragment, null,
    h(Card, { title: '技能库', extra: h('span', { className: 'cs-row' },
      listOf(samples).length > 1 ? h(Select, { value: chosen, onChange: set('sample'), options: listOf(samples).map((s) => [s.name, `样例 ${s.name}`]) }) : null,
      h(Button, { onClick: sample, busy: busy === 'sample', disabled: !chosen }, '导入样例'), h(Button, { onClick: load }, '刷新')) },
      listOf(samples).filter((s) => s.name === chosen && s.about).map((s) => h(Note, { key: s.name }, `样例 ${s.name}：${s.about}${s.files ? `（${s.files} 道题带输入文件，导入时上传）` : ''}`)),
      h(ErrorLine, { error: listError }), list === null ? h(Empty, null, '加载中…') : h(Table, { head: ['技能', '当前版本', '版本数', '用它的 Agent', '更新'],
        rows: list.map((s) => ({ key: s.name, onClick: () => setOpen(s.name), cells: [h('span', null, h('b', null, s.name), h('div', { className: 'cs-mut' }, String(s.description || '').slice(0, 60))),
          s.current, s.versions, listOf(s.agents).map((a) => `${a.name}（${a.version}）`).join('，') || '—', (s.updatedAt || '').slice(0, 19)] })),
        empty: '还没有技能：新建一个，或导入样例、S3 上已有的技能、工作坊项目里的技能。' })),
    open ? h(SkillDetail, { name: open, harnesses, onChanged: load }) : h(Fragment, null,
      h(Card, { title: '新建技能', extra: h(Button, { kind: 'pri', onClick: create, busy: busy === 'create', disabled: !form.name }, '创建') },
        h(Field, { label: '名称（小写字母、数字、连字符；要和 frontmatter 的 name 一致）' }, h(Input, { value: form.name, onChange: set('name'), mono: true, placeholder: 'reply-style' })),
        h(TextArea, { value: form.text, onChange: set('text'), rows: 10, mono: true })),
      h(Card, { title: '导入' }, h('div', { className: 'cs-grid' },
        h(Field, { label: 'S3 上的技能（含 SKILL.md 的前缀）' }, h('div', { className: 'cs-row' }, h(Input, { value: form.uri, onChange: set('uri'), mono: true, placeholder: 's3://bucket/skills/name/' }),
          h(Button, { onClick: fromUri, busy: busy === 'uri', disabled: !form.uri }, '导入'))),
        h(Field, { label: '工作坊项目的发布包' }, h('div', { className: 'cs-row' }, h(Input, { value: form.project, onChange: set('project'), mono: true, placeholder: 'loyalty-points' }),
          h(Button, { onClick: fromPack, busy: busy === 'pack', disabled: !form.project }, '导入')))))),
    h(ErrorLine, { error }))
}

// -- task sets ----------------------------------------------------------------------------------------------------------

const SET_TEMPLATE = JSON.stringify({ train: [{ id: 'a', question: '用户会问的话', rubric: '通过的回答必须做到的事，一行一条' }],
  val: [{ id: 'b', question: '…', rubric: '…' }], test: [{ id: 'c', question: '…', rubric: '…' }] }, null, 1)

// The tasks a task set's JSON holds (a list is one group), with the ids the server gives the ones that have none.
function parseSet(json) {
  try {
    const parsed = JSON.parse(json)
    const groups = Array.isArray(parsed) ? [['tasks', parsed]] : parsed && Array.isArray(parsed.tasks) ? [['tasks', parsed.tasks]]
      : ['train', 'val', 'test'].map((k) => [k, (parsed || {})[k]])
    return { tasks: groups.flatMap(([k, list]) => listOf(list).map((t, i) => ({ split: k, index: i, id: String((t && t.id) || `${k}-${i + 1}`),
      question: String((t && t.question) || ''), files: listOf(t && t.files) }))) }
  } catch (err) { return { tasks: [], error: err.message } }
}

// The files uploaded for tasks: each kept once by its content, deleted only when no task set names it.
function Assets({ list, onChanged }) {
  const { wid } = useCtx()
  const { busy, error, run } = useAction()
  const del = (a) => window.confirm(`删除 ${listOf(a.names).join('、')}？`) && run(`del-${a.sha256}`, () => call('DELETE', ws(wid, `/assets/${a.sha256}`)), '已删除').then(onChanged)
  return h(Card, { title: '输入文件（题目带给 Agent 的文件，同样的内容只存一份）', extra: h(Button, { onClick: onChanged }, '刷新') },
    h(Table, { head: ['文件', '类型', '大小', '用在', ''], rows: listOf(list).map((a) => ({ key: a.sha256, cells: [
      h('span', null, h('b', null, listOf(a.names).join('、')), h('div', { className: 'cs-mut cs-mono' }, `sha256:${String(a.sha256).slice(0, 12)}…`)),
      a.type, size(a.bytes || 0), listOf(a.usedBy).map((u) => `${u.name}（${u.tasks} 题）`).join('，') || '—',
      listOf(a.usedBy).length ? null : h(Button, { kind: 'danger', onClick: () => del(a), busy: busy === `del-${a.sha256}` }, '删除')] })),
    empty: '还没有上传过文件：在下面「新建任务集」的题目行上添加，或导入带文件的样例（points-chart-png）。' }),
    h(Note, null, 'xlsx、pdf、png、jpg、webp、md、txt、csv，每个最多 25 MiB，每题最多 32 个、共 100 MiB（和 Launchpad 一样）。评估时 Claude Agent SDK Runtime 在答题前把文件放进这次调用的工作目录，'
      + '没改动的输入文件不算它生成的文件；Harness 的沙箱放不进文件，带文件的题不能在 Harness 上评估。'),
    h(ErrorLine, { error }))
}

function TaskSets() {
  const { wid } = useCtx()
  const [sets, load, listError] = useList(wid, '/tasksets', 'tasksets')
  const [skills] = useList(wid, '/skills', 'skills')
  const [assets, loadAssets] = useList(wid, '/assets', 'assets')
  const [open, setOpen] = useState(null)
  const [detail, setDetail] = useState(null)
  const [form, setForm] = useState({ name: '', description: '', json: SET_TEMPLATE })
  const [attached, setAttached] = useState({})
  const [gen, setGen] = useState({ skill: '', count: '8', guidance: '' })
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const parsed = parseSet(form.json)
  useEffect(() => { if (open) run('open', async () => setDetail(await call('GET', ws(wid, `/tasksets/${open}`)))) }, [open])
  // files picked on a task's row: uploaded one request each (base64 in JSON, at most 25 MiB a file), then named on the task
  const attach = (taskId) => (e) => {
    const picked = Array.from((e.target && e.target.files) || [])
    if (e.target) e.target.value = ''
    if (!picked.length) return
    run(`up-${taskId}`, async () => {
      const big = picked.find((f) => f.size > MAX_FILE)
      if (big) throw new Error(`${big.name} 是 ${size(big.size)}：每个文件最多 25 MiB`)
      const got = []
      for (const f of picked) got.push(...listOf((await call('POST', ws(wid, '/assets'), { files: [await readFile(f)] })).assets))
      setAttached((m) => ({ ...m, [taskId]: [...listOf(m[taskId]).filter((d) => !got.some((g) => g.path.toLowerCase() === d.path.toLowerCase())), ...got] }))
      loadAssets()
      return got
    }, (got) => `已上传 ${got.map((g) => `${g.name}${g.created ? '' : '（同样的内容已经有了，没有重复存）'}`).join('、')}`)
  }
  const detach = (taskId, path) => setAttached((m) => ({ ...m, [taskId]: listOf(m[taskId]).filter((d) => d.path !== path) }))
  const save = () => run('save', () => {
    const raw = JSON.parse(form.json)
    const withFiles = (k) => (t, i) => {
      const extra = listOf(attached[String((t && t.id) || `${k}-${i + 1}`)]).map((d) => ({ path: d.path, asset: d.asset }))
      return extra.length ? { ...t, files: [...listOf(t.files), ...extra] } : t
    }
    const body = Array.isArray(raw) ? { tasks: raw.map(withFiles('tasks')), mode: 'single' } : Array.isArray(raw.tasks) ? { ...raw, tasks: raw.tasks.map(withFiles('tasks')), mode: 'single' }
      : { ...raw, ...Object.fromEntries(['train', 'val', 'test'].filter((k) => Array.isArray(raw[k])).map((k) => [k, raw[k].map(withFiles(k))])), mode: 'split' }
    return call('POST', ws(wid, '/tasksets'), { ...body, name: form.name, description: form.description })
  }, (r) => `已保存「${r.name}」`).then((r) => { if (r) { setAttached({}); load(); loadAssets(); setOpen(r.id) } })
  const generate = () => run('gen', async () => {
    const r = await call('POST', ws(wid, '/tasksets/generate'), { skill: gen.skill, count: Number(gen.count), guidance: gen.guidance })
    setForm((f) => ({ ...f, name: f.name || `${gen.skill} · 生成`, json: JSON.stringify(r.tasks, null, 1) }))
    return r
  }, (r) => `${r.model} 出了 ${r.tasks.length} 道题：检查、修改后保存（一个列表会按 4:3:3 自动分成 train / val / test）`)
  const remove = (tid) => window.confirm('删除这个任务集？') && run('del', () => call('DELETE', ws(wid, `/tasksets/${tid}`)), '已删除').then(() => { setOpen(null); setDetail(null); load(); loadAssets() })
  const parts = detail ? (detail.mode === 'single' ? [['tasks', '全部（训练时按 4:3:3 分）']] : [['train', 'train（用来改技能）'], ['val', 'val（决定接不接受改动）'], ['test', 'test（最后对比，不参与训练）']]) : []
  return h(Fragment, null,
    h(Card, { title: '任务集', extra: h(Button, { onClick: load }, '刷新') }, h(ErrorLine, { error: listError }), sets === null ? h(Empty, null, '加载中…') : h(Table, {
      head: ['名称', '题数', '输入文件', '更新'], rows: sets.map((t) => ({ key: t.id, onClick: () => setOpen(t.id), cells: [h('span', null, h('b', null, t.name), h('div', { className: 'cs-mut' }, t.description || '')),
        Object.entries(t.counts).map(([k, v]) => `${k} ${v}`).join(' · '), t.files ? `${t.files} 题带文件` : '—', (t.updatedAt || '').slice(0, 19)] })), empty: '还没有任务集。' })),
    detail ? h(Card, { title: detail.name, extra: h('span', { className: 'cs-row' }, h(Button, { onClick: () => { setOpen(null); setDetail(null) } }, '关闭'),
      h(Button, { kind: 'danger', onClick: () => remove(detail.id) }, '删除')) },
      parts.map(([k, label]) => h(Fragment, { key: k }, h('div', { className: 'cs-mut' }, `${label} · ${listOf(detail[k]).length} 题`),
        h(Table, { head: ['id', '问题', '输入文件', '评分标准'], rows: listOf(detail[k]).map((t) => ({ key: t.id, cells: [h('span', { className: 'cs-mono' }, t.id), t.question,
          listOf(t.files).length ? h('div', { className: 'cs-row', style: { flexWrap: 'wrap' } }, listOf(t.files).map(fileChip)) : '—',
          h('div', { style: { whiteSpace: 'pre-wrap' }, className: 'cs-mut' }, t.rubric)] })) })))) : null,
    h(Assets, { list: assets, onChanged: loadAssets }),
    h(Card, { title: '让模型按技能出题' },
      h('div', { className: 'cs-grid' },
        h(Field, { label: '技能' }, h(Select, { value: gen.skill, onChange: (v) => setGen((g) => ({ ...g, skill: v })), options: [['', '选择'], ...listOf(skills).map((s) => [s.name, s.name])] })),
        h(Field, { label: '题数' }, h(Input, { value: gen.count, onChange: (v) => setGen((g) => ({ ...g, count: v })) }))),
      h(Field, { label: '补充说明（例如事实依据、要覆盖的边界情况）' }, h(TextArea, { value: gen.guidance, onChange: (v) => setGen((g) => ({ ...g, guidance: v })), rows: 3 })),
      h(Button, { onClick: generate, busy: busy === 'gen', disabled: !gen.skill }, '出题（约 30 秒，不保存）')),
    h(Card, { title: '新建任务集', extra: h(Button, { kind: 'pri', onClick: save, busy: busy === 'save', disabled: !form.name || !!parsed.error }, '保存') },
      h('div', { className: 'cs-grid' }, h(Field, { label: '名称' }, h(Input, { value: form.name, onChange: set('name') })),
        h(Field, { label: '说明' }, h(Input, { value: form.description, onChange: set('description') }))),
      h(Field, { label: '题目 JSON：{train, val, test} 三组，或一个列表（题目里也可以直接写 "files": [{"path", "asset": "sha256:…"}]）' },
        h(TextArea, { value: form.json, onChange: set('json'), rows: 12, mono: true })),
      parsed.error ? h(Note, { tone: 'warn' }, `JSON 还不能解析：${parsed.error}`) : h(Fragment, null,
        h('div', { className: 'cs-mut' }, '题目和它们的输入文件：在一行上添加的文件会先上传，保存时写进这道题的 files（放在它工作目录的同名路径）'),
        h(Table, { head: ['组', 'id', '问题', '输入文件', ''], empty: '还没有题目。', rows: parsed.tasks.map((t) => {
          const mine = listOf(attached[t.id])
          return { key: `${t.split}:${t.index}`, cells: [t.split, h('span', { className: 'cs-mono' }, t.id), String(t.question).slice(0, 60),
            h('div', { className: 'cs-row', style: { flexWrap: 'wrap' } }, t.files.filter((f) => f && f.path).map((f) => h(Chip, { key: `j-${f.path}` }, `${f.path}（JSON）`)),
              mine.map((d) => h('span', { key: d.path, className: 'cs-row' }, fileChip(d), h(Button, { onClick: () => detach(t.id, d.path), title: '不带这个文件' }, '×')))),
            h('label', { className: 'cs-btn', title: 'xlsx、pdf、png、jpg、webp、md、txt、csv，每个最多 25 MiB' }, busy === `up-${t.id}` ? '上传中…' : '添加文件',
              h('input', { type: 'file', multiple: true, accept: ACCEPT, style: { display: 'none' }, onChange: attach(t.id) }))] }
        }) }))),
    h(ErrorLine, { error }))
}

// -- evaluation ---------------------------------------------------------------------------------------------------------

function JobList({ jobs, kind, open, onOpen }) {
  const mine = listOf(jobs).filter((j) => j.kind === kind)
  return h(Table, { head: ['', '状态', '开始', ''], rows: mine.map((j) => ({ key: j.id, onClick: () => onOpen(j.id),
    cells: [h('b', null, j.label), h(Chip, { tone: JOB_TONE[j.status] }, JOB_LABEL[j.status] || j.status), (j.createdAt || '').slice(0, 19), j.id === open ? '在看' : '查看'] })),
    empty: '还没有。' })
}

function TargetNote({ targets, value, taskset }) {
  const t = targets.find((x) => targetKey(x) === value)
  if (!t) return null
  if (t.note) return h(Note, { tone: 'warn' }, `${t.name}：${t.note}`)
  const files = taskset && taskset.files ? taskset.files : 0
  if (t.kind !== 'runtime') {
    return files ? h(Note, { tone: 'warn' }, `「${taskset.name}」有 ${files} 道题带输入文件，Harness 的沙箱里放不进文件：这些题只能在 Claude Agent SDK Runtime 上评估`
      + '（训练会用到全部的题；评估时选一组不带文件的题也可以）。') : null
  }
  return h(Fragment, null, files && !t.inputFiles ? h(Note, { tone: 'warn' }, `「${taskset.name}」有 ${files} 道题带输入文件，而 ${t.name} 是旧模板（${t.template}）部署的，`
    + '调用带不了文件：先在「部署代码」给它部署一个新版本。') : null,
  h(Note, null, `Claude Agent SDK Runtime ${t.name}（${t.model || '模型未知'}；镜像里的技能：${listOf(t.skills).join('、') || '无'}）。`
    + '每道题在一个新会话（它自己的 microVM）里答：带技能的一臂按次从 S3 读这个技能版本，不带的一臂一个技能都不给（镜像里别的技能两臂都有）。'
    + '每次回答在工作区里有自己的目录（题目给的输入文件先放进去），回答里带回这一轮生成或改动的文件（表格的单元格、文档和幻灯片的文字、图片），'
    + '裁判同时看回答和文件——图片（以及文档里的图片）直接给裁判看。'))
}

function SkillForm({ form, set, targets, skills, sets, versions, extra }) {
  return h('div', { className: 'cs-grid' },
    h(Field, { label: 'Agent（Harness 或 Claude Agent SDK Runtime）' }, h(Select, { value: form.target, onChange: set('target'),
      options: [['', '选择'], ...targetOptions(targets)] })),
    h(Field, { label: '技能' }, h(Select, { value: form.skill, onChange: set('skill'), options: [['', '选择'], ...listOf(skills).map((s) => [s.name, s.name])] })),
    h(Field, { label: '版本' }, h(Select, { value: form.version, onChange: set('version'), options: [['', '当前版本'], ...versions.map((v) => [v.version, `${v.version} · ${v.source}`])] })),
    h(Field, { label: '任务集' }, h(Select, { value: form.tasksetId, onChange: set('tasksetId'), options: [['', '选择'], ...listOf(sets).map((t) => [t.id, t.name])] })),
    h(Field, { label: '换模型试（空 = 用 Agent 自己的模型）', hint: '只影响这次评估，不改 Agent' }, h(Input, { value: form.model, onChange: set('model'), mono: true, placeholder: 'us.anthropic.claude-haiku-4-5-20251001-v1:0' })),
    h(Field, { label: '裁判模型' }, h(Input, { value: form.judgeModel, onChange: set('judgeModel'), mono: true })),
    h(Field, { label: '并发' }, h(Select, { value: form.workers, onChange: set('workers'), options: [['2', '2'], ['4', '4'], ['8', '8']] })),
    h(Field, { label: '每题答几遍（取平均）', hint: 'Agent 和裁判每次调用都有波动；题少时多答几遍更可信' }, h(Select, { value: form.repeats, onChange: set('repeats'),
      options: [['1', '1 遍'], ['2', '2 遍'], ['3', '3 遍']] })),
    extra)
}

function useVersions(wid, skill) {
  const [versions, setVersions] = useState([])
  useEffect(() => { setVersions([]); if (skill) call('GET', ws(wid, `/skills/${skill}`)).then((d) => setVersions(listOf(d.versions))).catch(() => {}) }, [wid, skill])
  return versions
}

function Evaluate() {
  const { wid } = useCtx()
  const targets = useTargets(wid)
  const [skills] = useList(wid, '/skills', 'skills')
  const [sets] = useList(wid, '/tasksets', 'tasksets')
  const [jobs, loadJobs] = useList(wid, '/jobs', 'jobs')
  const [form, setForm] = useState({ target: '', skill: '', version: '', tasksetId: '', split: '', model: '', judgeModel: JUDGE, workers: '4', repeats: '1' })
  const [open, setOpen] = useState(null)
  const job = useJob(open)
  const versions = useVersions(wid, form.skill)
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  useEffect(() => { if (job && job.status !== 'running') loadJobs() }, [job && job.status])
  const { target, ...rest } = form
  const start = () => run('start', () => confirmed((ack) => call('POST', ws(wid, '/evaluations'), { ...rest, ...targetBody(target), workers: Number(form.workers),
    repeats: Number(form.repeats), acknowledged: ack }), '这个 Agent'), '评估已开始')
    .then((r) => { if (r) { setOpen(r.job.id); loadJobs() } })
  const res = job && job.result
  return h(Fragment, null,
    h(Card, { title: '这个技能到底有没有用：同一个 Agent，有技能和没技能各答一遍', extra: h(Button, { kind: 'pri', onClick: start, busy: busy === 'start',
      disabled: !form.target || !form.skill || !form.tasksetId }, '开始评估') },
      h(SkillForm, { form, set, targets, skills, sets, versions, extra: h(Field, { label: '用哪组题' }, h(Select, { value: form.split, onChange: set('split'),
        options: [['', '默认（test；一个列表就用全部）'], ['test', 'test'], ['val', 'val'], ['train', 'train'], ['all', '全部']] })) }),
      h(TargetNote, { targets, value: form.target, taskset: listOf(sets).find((x) => x.id === form.tasksetId) }),
      h(Note, null, '每道题两个新会话、两个独立的 actor（记忆不会串）：一个带技能，一个不带，其余完全相同。裁判按评分标准给两份回答打分。Agent 的执行角色会获得读技能库的权限。'),
      h(ErrorLine, { error })),
    h(Card, { title: '评估记录', extra: h(Button, { onClick: loadJobs }, '刷新') }, h(JobList, { jobs, kind: 'skill-eval', open, onOpen: setOpen })),
    job ? h(Card, { title: job.label, extra: h('span', { className: 'cs-row' }, h(Chip, { tone: JOB_TONE[job.status] }, JOB_LABEL[job.status] || job.status),
      job.progress && job.progress.total ? h('span', { className: 'cs-mut' }, `${job.progress.done}/${job.progress.total}`) : null) },
      h('div', { className: 'cs-mut' }, `${job.params.runtime ? `Claude Agent SDK Runtime ${job.params.runtime}` : `Harness ${job.params.harness}`} · `
        + `模型 ${job.params.model}${job.params.modelOverride ? '（这次换的）' : ''} · 裁判 ${job.params.judgeModel} · ${job.params.taskset} / ${job.params.split} · ${job.params.tasks} 题`
        + `${job.params.repeats > 1 ? ` × ${job.params.repeats} 遍` : ''}${job.params.inputFiles ? ` · ${job.params.inputFiles} 题带输入文件` : ''}`),
      res ? h(Fragment, null, h(Verdict, { v: res.verdict }), h(Arms, { arms: res.arms, order: ['with', 'without'] }),
        res.comparison ? h(Note, null, `提升：通过率 ${pct(res.comparison.lift.passRate)}，得分 ${num(res.comparison.lift.softMean)} · 通过上 赢 ${res.comparison.wins.length} / 输 ${res.comparison.losses.length} / 平 ${res.comparison.ties}`
          + (res.comparison.p !== null ? `（p=${res.comparison.p}）` : '') + ` · 得分上 好 ${listOf(res.comparison.softWins).length} / 差 ${listOf(res.comparison.softLosses).length}`) : null,
        h(Rows, { rows: res.rows, order: ['with', 'without'].filter((k) => res.arms[k]) })) : h('pre', { className: 'cs-pre' }, listOf(job.log).slice(-12).join('\n') || '…'),
      job.error ? h('div', { className: 'cs-err' }, job.error) : null) : null)
}

// -- training -----------------------------------------------------------------------------------------------------------

function Publish({ job, targets }) {
  const { wid } = useCtx()
  const [target, setTarget] = useState(jobTarget(job.params))
  const [force, setForce] = useState(false)
  const { busy, error, run } = useAction()
  const res = job.result
  const chosen = targets.find((t) => targetKey(t) === target)
  const publish = () => run('pub', () => confirmed((ack) => call('POST', ws(wid, `/jobs/${job.id}/publish`), { ...targetBody(target), acknowledged: ack, force }), '这个 Agent'),
    (r) => `已保存为 ${r.saved.name} ${r.saved.version}（当前版本）${r.applied ? `，${r.applied.harness} 已改用它` : ''}`
      + `${r.redeploy ? `，${r.redeploy.runtime} 的新版本开始部署（任务 ${r.redeploy.jobId}，在「部署代码」看进度）` : ''}`)
  if (!res.best || !res.best.step) return h(Note, null, '训练没有找到比初始版本更好的改法：没有可发布的新版本。')
  return h(Card, { title: '发布最佳版本', extra: h(Button, { kind: 'pri', onClick: publish, busy: busy === 'pub', disabled: !res.improved && !force }, '发布') },
    res.improved ? h(Note, { tone: 'ok' }, '最佳版本在留出的题上也比初始版本好。') : h(Note, { tone: 'warn' }, '最佳版本在 val 上更好，但在留出的题上没有超过初始版本：可能是过拟合 val。确实要发布就勾选「仍然发布」。'),
    h('div', { className: 'cs-row' }, h(Select, { value: target, onChange: setTarget, options: [['', '只存进技能库'], ...targets.filter((t) => !t.note).map((t) => [targetKey(t),
      t.kind === 'runtime' ? `并给 ${t.name} 部署新版本（重新构建镜像）` : `并让 ${t.name} 改用它`])] }),
      res.improved ? null : h('label', { className: 'cs-row' }, h('input', { type: 'checkbox', checked: force, onChange: (e) => setForce(e.target.checked) }), '仍然发布')),
    chosen && chosen.kind === 'runtime' ? h(Note, null, 'Claude Agent SDK Runtime 的技能在镜像里：发布会用新版本重新生成并构建镜像、过镜像扫描、发一个新的 Runtime 版本（需要管理员）。'
      + 'AgentCore 会给新版本一个空的会话存储。') : null,
    h(ErrorLine, { error }))
}

function TrainResult({ job, targets }) {
  const res = job.result
  const history = listOf(res ? res.history : (job.progress || {}).history)
  const [step, setStep] = useState(null)
  const chosen = res && step !== null ? res.history.find((x) => x.step === step) : null
  return h(Fragment, null,
    h(Curve, { history }),
    h(Table, { head: ['步', '类型', 'val 得分', '结果', '改了什么'], rows: history.map((x) => ({ key: x.step, onClick: res && x.text ? () => setStep(step === x.step ? null : x.step) : undefined,
      cells: [x.step, x.kind === 'seed' ? '初始' : x.kind === 'skip' ? '跳过' : `候选（预算 ${x.budget}）`,
        typeof x.currentScore === 'number' ? h('span', { title: '候选 vs 同时重答的当前版本' }, `${num(x.valScore)} vs ${num(x.currentScore)}（${x.delta >= 0 ? '+' : ''}${num(x.delta)}）`)
          : typeof x.valScore === 'number' ? num(x.valScore) : '—',
        x.kind === 'seed' ? '—' : x.kind === 'skip' ? h('span', { className: 'cs-mut' }, x.note) : h('span', { className: 'cs-row' },
          h(Chip, { tone: x.accepted ? 'ok' : 'bad' }, x.accepted ? '接受' : '拒绝'), x.best ? h(Chip, { tone: 'info' }, '最佳') : null),
        x.kind === 'candidate' ? h('div', null, listOf(x.edits).map((e, i) => h('div', { key: i, className: 'cs-mut' }, `· ${e}`)),
          x.failures ? h('div', { className: 'cs-mut' }, `据 ${x.failures.length} 道没过的题：${x.failures.join('，')}`) : null) : ''] })) }),
    chosen ? h(Card, { title: `第 ${chosen.step} 步的候选` }, h(Note, null, chosen.rationale), h('pre', { className: 'cs-pre' }, chosen.text)) : null,
    res ? h(Fragment, null,
      h(Card, { title: `留出的题（${res.final.split}，${listOf(res.final.rows).length} 题）：初始 vs 最佳 vs 没技能` },
        h(Arms, { arms: res.final, order: ['best', 'seed', 'without'] }),
        h(Note, null, `最佳 vs 初始：通过 赢 ${res.final.bestVsSeed.wins.length} / 输 ${res.final.bestVsSeed.losses.length}，得分 ${num(res.final.bestVsSeed.lift.softMean)}`
          + ` · 最佳 vs 没技能：通过率 ${pct(res.final.bestVsNone.lift.passRate)}，得分 ${num(res.final.bestVsNone.lift.softMean)}`),
        h(Rows, { rows: res.final.rows, order: ['best', 'seed', 'without'] })),
      h(Card, { title: '初始 → 最佳' }, h(Diff, { text: res.diff })),
      job.status === 'succeeded' ? h(Publish, { job, targets }) : null) : null)
}

function Train() {
  const { wid } = useCtx()
  const targets = useTargets(wid)
  const [skills] = useList(wid, '/skills', 'skills')
  const [sets] = useList(wid, '/tasksets', 'tasksets')
  const [jobs, loadJobs] = useList(wid, '/jobs', 'jobs')
  const [form, setForm] = useState({ target: '', skill: '', version: '', tasksetId: '', model: '', judgeModel: JUDGE, workers: '4', repeats: '2',
    optimizerModel: OPTIMIZER, epochs: '1', batchSize: '4', editBudget: '4', gateMetric: 'soft', gateMargin: '0.05' })
  const [open, setOpen] = useState(null)
  const job = useJob(open)
  const versions = useVersions(wid, form.skill)
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  useEffect(() => { if (job && job.status !== 'running') loadJobs() }, [job && job.status])
  const { target, ...rest } = form
  const start = () => run('start', () => confirmed((ack) => call('POST', ws(wid, '/trainings'), { ...rest, ...targetBody(target), workers: Number(form.workers), repeats: Number(form.repeats),
    epochs: Number(form.epochs), batchSize: Number(form.batchSize), editBudget: Number(form.editBudget), gateMargin: Number(form.gateMargin), acknowledged: ack }), '这个 Agent'),
  '训练已开始').then((r) => { if (r) { setOpen(r.job.id); loadJobs() } })
  const p = (job && job.progress) || {}
  return h(Fragment, null,
    h(Card, { title: '训练：从没过的题里改技能，只留在 val 上更好的改动', extra: h(Button, { kind: 'pri', onClick: start, busy: busy === 'start',
      disabled: !form.target || !form.skill || !form.tasksetId }, '开始训练') },
      h(SkillForm, { form, set, targets, skills, sets, versions, extra: h(Fragment, null,
        h(Field, { label: '轮数' }, h(Select, { value: form.epochs, onChange: set('epochs'), options: [['1', '1'], ['2', '2'], ['3', '3']] })),
        h(Field, { label: '每批题数' }, h(Select, { value: form.batchSize, onChange: set('batchSize'), options: [['2', '2'], ['3', '3'], ['4', '4'], ['6', '6'], ['8', '8']] })),
        h(Field, { label: '每步最多改几处（逐步减到 2）' }, h(Select, { value: form.editBudget, onChange: set('editBudget'), options: [['2', '2'], ['4', '4'], ['6', '6'], ['8', '8']] })),
        h(Field, { label: '接受改动看' }, h(Select, { value: form.gateMetric, onChange: set('gateMetric'), options: [['soft', '平均得分'], ['hard', '通过率'], ['mixed', '两者平均']] })),
        h(Field, { label: '至少要高出当前版本', hint: '候选和当前版本同时重答 val，差距够大才接受' }, h(Input, { value: form.gateMargin, onChange: set('gateMargin') })),
        h(Field, { label: '改写技能的模型' }, h(Input, { value: form.optimizerModel, onChange: set('optimizerModel'), mono: true }))) }),
      h(TargetNote, { targets, value: form.target, taskset: listOf(sets).find((x) => x.id === form.tasksetId) }),
      h(Note, null, '先在 val 上测初始版本；每一步用当前技能答一批 train 题，改写模型只看没过的题来改 SKILL.md（不许把评分标准里的事实抄进技能）。'
        + '候选和当前版本同时重答 val，候选高出至少设定的差距、且变好的题不少于变差的题才接受。最后在 test 上比较初始、最佳和没技能。'),
      h(ErrorLine, { error })),
    h(Card, { title: '训练记录', extra: h(Button, { onClick: loadJobs }, '刷新') }, h(JobList, { jobs, kind: 'skill-train', open, onOpen: setOpen })),
    job ? h(Card, { title: job.label, extra: h('span', { className: 'cs-row' }, h(Chip, { tone: JOB_TONE[job.status] }, JOB_LABEL[job.status] || job.status),
      p.steps ? h('span', { className: 'cs-mut' }, `第 ${p.step}/${p.steps} 步 · 当前 ${num(p.current)} · 最佳 ${num(p.best)}`) : null) },
      h('div', { className: 'cs-mut' }, `${job.params.runtime ? `Claude Agent SDK Runtime ${job.params.runtime}` : `Harness ${job.params.harness}`} · 模型 ${job.params.model} · 改写 ${job.params.optimizerModel} · 裁判 ${job.params.judgeModel} · train ${job.params.counts.train} / val ${job.params.counts.val} / test ${job.params.counts.test}`),
      h(TrainResult, { job, targets }),
      job.status === 'running' ? h('pre', { className: 'cs-pre' }, listOf(job.log).slice(-6).join('\n')) : null,
      job.error ? h('div', { className: 'cs-err' }, job.error) : null) : null)
}

function SkillLabPage() {
  const { workspace } = useCtx()
  const [tab, setTab] = useState('library')
  if (!workspace) return h(Empty, null, '先添加工作区。')
  return h(Fragment, null, h('h1', null, 'Skill Lab'),
    h(Tabs, { value: tab, onChange: setTab, options: [['library', '技能库'], ['sets', '任务集'], ['eval', '评估'], ['train', '训练']] }),
    tab === 'library' ? h(Library) : tab === 'sets' ? h(TaskSets) : tab === 'eval' ? h(Evaluate) : h(Train))
}

export default { id: 'skills', label: 'Skill Lab', group: '构建', Page: SkillLabPage }
