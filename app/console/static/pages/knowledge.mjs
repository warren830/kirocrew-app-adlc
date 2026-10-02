// 知识库: Bedrock managed knowledge bases — create (upload or an S3 location), documents and their status, sync, a
// retrieval playground, and attaching a knowledge base to an agent (a Retrieve tool per KB plus one deep search).
import React from 'react'
import { call, useCtx, listOf, Card, Button, Field, Input, TextArea, Select, Chip, Note, Empty, ErrorLine, Table, useAction, useJob, JOB_TONE,
  JOB_LABEL } from '../ui.mjs'

const { useState, useEffect, createElement: h, Fragment } = React
const ws = (wid, path) => `/workspaces/${wid}${path}`
const TONE = { ACTIVE: 'ok', AVAILABLE: 'ok', INDEXED: 'ok', COMPLETE: 'ok', CREATING: 'info', IN_PROGRESS: 'info', STARTING: 'info', FAILED: 'bad' }

function readFiles(fileList) {
  return Promise.all(Array.from(fileList).map((f) => new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve({ name: f.name, contentBase64: String(reader.result).split(',')[1] || '' })
    reader.onerror = reject
    reader.readAsDataURL(f)
  })))
}

function CreateKb({ onCreated }) {
  const { wid } = useCtx()
  const [form, setForm] = useState({ name: '', description: '', mode: 'upload', bucket: '', prefix: '' })
  const [job, setJob] = useState(null)
  const live = useJob(job)
  const { busy, error, run } = useAction()
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  useEffect(() => { if (live && live.status !== 'running') onCreated() }, [live && live.status])
  const create = () => run('create', () => call('POST', ws(wid, '/knowledge-bases'), { name: form.name, description: form.description,
    source: form.mode === 'upload' ? { mode: 'upload' } : { mode: 's3', bucket: form.bucket, prefix: form.prefix } }), (r) => `已创建 ${r.id}，正在初始化（约 2 分钟）`)
    .then((r) => { if (r) { setJob(r.job); onCreated() } })
  return h(Card, { title: '新建托管知识库（向量库、嵌入和重排都由服务托管）', extra: h(Button, { kind: 'pri', onClick: create, busy: busy === 'create' }, '创建') },
    h('div', { className: 'cs-grid' },
      h(Field, { label: '名称' }, h(Input, { value: form.name, onChange: set('name'), mono: true, placeholder: 'hr-policies' })),
      h(Field, { label: '说明（Agent 会在工具描述里看到）' }, h(Input, { value: form.description, onChange: set('description') })),
      h(Field, { label: '数据来源' }, h(Select, { value: form.mode, onChange: set('mode'), options: [['upload', '上传文件（存到控制台的桶）'], ['s3', '已有的 S3 位置']] })),
      form.mode === 's3' ? h(Field, { label: 'Bucket' }, h(Input, { value: form.bucket, onChange: set('bucket'), mono: true })) : null,
      form.mode === 's3' ? h(Field, { label: '前缀' }, h(Input, { value: form.prefix, onChange: set('prefix'), mono: true })) : null),
    live ? h(Note, { tone: live.status === 'failed' ? 'warn' : '' }, h(Chip, { tone: JOB_TONE[live.status] }, JOB_LABEL[live.status] || live.status), ' ',
      live.progress && live.progress.status ? `知识库 ${live.progress.status}` : '', live.error ? ` ${live.error}` : '') : null,
    h(ErrorLine, { error }))
}

function KbDetail({ kbId, onChanged }) {
  const { wid } = useCtx()
  const [kb, setKb] = useState(null)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState(null)
  const [agents, setAgents] = useState([])
  const [harness, setHarness] = useState('')
  const [using, setUsing] = useState([])
  const { busy, error, run } = useAction()
  const loadUsing = () => call('GET', ws(wid, `/knowledge-bases/${kbId}/agents`)).then((r) => setUsing(listOf(r.agents))).catch(() => {})
  const load = () => run('load', async () => setKb(await call('GET', ws(wid, `/knowledge-bases/${kbId}`))))
  useEffect(() => { load(); loadUsing(); call('GET', ws(wid, '/agents')).then((r) => setAgents(listOf(r.agents).filter((a) => a.kind === 'harness'))).catch(() => {}) }, [kbId])
  if (!kb) return h(Fragment, null, h(ErrorLine, { error }), h(Empty, null, '加载中…'))
  const upload = (e) => { const files = e.target.files; if (files && files.length && (kb.console || window.confirm(`${kb.name} 不是这个控制台创建的：确定往它上传文件？`))) run('upload', async () => call('POST', ws(wid, `/knowledge-bases/${kbId}/files`), { files: await readFiles(files), acknowledged: kb.console ? undefined : true }),
    (r) => `已上传 ${r.uploaded.length} 个文件并开始同步`).then(load) }
  const ask = () => run('query', async () => setResults(listOf((await call('POST', ws(wid, `/knowledge-bases/${kbId}/query`), { query, results: 5 })).results)))
  const target = agents.find((a) => a.id === harness)
  const confirmFor = (name) => window.confirm(`${name} 不是这个控制台创建的：确定给它挂上这个知识库？（会改它的工具和执行角色的权限）`)
  const attach = () => run('attach', async () => {  // another team's agent: the backend asks, the person confirms, then it is sent again
    const url = ws(wid, `/knowledge-bases/${kbId}/attach`)
    try { return await call('POST', url, { harnessId: harness }) } catch (err) {
      if (!String(err.message || '').includes('acknowledged') || !confirmFor(target ? target.name : harness)) throw err
      return call('POST', url, { harnessId: harness, acknowledged: true })
    }
  },
    (r) => `${r.harness} 现在有 ${r.tools.join('、')}`).then(loadUsing)
  const detach = (hid) => run(`detach-${hid}`, () => call('POST', ws(wid, `/knowledge-bases/${kbId}/detach`), { harnessId: hid, acknowledged: true }),  // only agents it attached
    (r) => `已从 ${r.harness} 取下`).then(loadUsing)
  const remove = () => window.confirm(`删除知识库 ${kb.name}？${using.length ? `会先从 ${using.map((u) => u.name).join('、')} 上取下。` : ''}`) && run('del', () => call('DELETE', ws(wid, `/knowledge-bases/${kbId}`)), '已删除').then(onChanged)
  return h(Fragment, null,
    h(Card, { title: kb.name, extra: h('span', { className: 'cs-row' }, h(Chip, { tone: TONE[kb.status] }, kb.status), h(Chip, null, kb.type || '—'),
      h(Button, { onClick: load, busy: busy === 'load' }, '刷新'), kb.console ? h(Button, { kind: 'danger', onClick: remove }, '删除') : null) },
      kb.description ? h(Note, null, kb.description) : null,
      listOf(kb.failureReasons).length ? h('div', { className: 'cs-err' }, kb.failureReasons.join('\n')) : null,
      listOf(kb.sources).map((s) => h(Card, { key: s.id, title: `数据源 ${s.name}`, extra: h(Chip, { tone: TONE[s.status] }, s.status) },
        h('div', { className: 'cs-mut cs-mono' }, `s3://${s.bucket || '?'}/${s.prefix || ''}`),
        listOf(s.ingestion).length ? h(Note, null, `最近同步：${s.ingestion[0].status} · ${(s.ingestion[0].startedAt || '').slice(0, 19)}${s.ingestion[0].statistics ? ` · 已索引 ${s.ingestion[0].statistics.numberOfNewDocumentsIndexed ?? 0} 新 / ${s.ingestion[0].statistics.numberOfModifiedDocumentsIndexed ?? 0} 改 / 失败 ${s.ingestion[0].statistics.numberOfDocumentsFailed ?? 0}` : ''}`) : null,
        s.documents ? h(Table, { head: ['文档', '状态', '原因'], rows: s.documents.map((d) => ({ key: d.uri, cells: [h('span', { className: 'cs-mono' }, String(d.uri || '').split('/').pop()),
          h(Chip, { tone: TONE[d.status] }, d.status), d.reason || '—'] })), empty: '还没有文档。' }) : null)),
      h('div', { className: 'cs-row' }, h('label', { className: 'cs-btn' }, '上传文件', h('input', { type: 'file', multiple: true, style: { display: 'none' }, onChange: upload })),
        h(Button, { onClick: () => (kb.console || window.confirm(`${kb.name} 不是这个控制台创建的：确定重新同步？`)) && run('sync', () => call('POST', ws(wid, `/knowledge-bases/${kbId}/sync`), { acknowledged: kb.console ? undefined : true }), '已开始同步').then(load) }, '重新同步'))),
    h(Card, { title: '检索试验台：Agent 会看到什么', extra: h(Button, { kind: 'pri', onClick: ask, busy: busy === 'query', disabled: !query.trim() }, '检索') },
      h(TextArea, { value: query, onChange: setQuery, rows: 2, placeholder: '输入一个问题' }),
      results ? (results.length ? results.map((r, i) => h('div', { key: i, className: 'cs-note' }, h(Chip, null, r.score ? r.score.toFixed(3) : '—'), ' ',
        h('span', { className: 'cs-mut cs-mono' }, JSON.stringify(r.location || {}).slice(0, 120)), h('div', null, String(r.text || '').slice(0, 500)))) : h(Empty, null, '没有检索到内容。')) : null),
    h(Card, { title: '挂到 Agent 上', extra: h(Button, { kind: 'pri', onClick: attach, busy: busy === 'attach', disabled: !harness }, '挂上') },
      using.length ? h(Table, { head: ['已挂上的 Agent', ''], rows: using.map((u) => ({ key: u.harnessId, cells: [h('b', null, u.name),
        h(Button, { onClick: () => detach(u.harnessId), busy: busy === `detach-${u.harnessId}` }, '取下')] })) }) : null,
      h(Select, { value: harness, onChange: setHarness, options: [['', '选一个 Harness'], ...agents.map((a) => [a.id, a.name])] }),
      target ? h(Note, null, `${target.name} 会通过 Gateway 工具 adlckb 拿到两类工具：这个知识库的 Retrieve（快速检索），以及它所有知识库上的 AgenticRetrieveStream（服务端拆分子问题、检索并重排的深度检索）。它只能看到自己挂上的知识库。不是控制台建的 Agent 会先请你确认。`) : null),
    h(ErrorLine, { error }))
}

function KnowledgePage() {
  const { wid, workspace } = useCtx()
  const [list, setList] = useState(null)
  const [open, setOpen] = useState(null)
  const [error, setError] = useState(null)
  const load = () => call('GET', ws(wid, '/knowledge-bases')).then((r) => setList(listOf(r.knowledgeBases))).catch(setError)
  useEffect(() => { setList(null); setOpen(null); if (wid) load() }, [wid])
  if (!workspace) return h(Empty, null, '先添加工作区。')
  return h(Fragment, null, h('h1', null, '知识库'),
    h(Card, { title: '知识库', extra: h(Button, { onClick: load }, '刷新') }, h(ErrorLine, { error }), list === null ? h(Empty, null, '加载中…') : h(Table, {
      head: ['名称', '状态', '说明', '更新'], rows: list.map((k) => ({ key: k.id, onClick: () => setOpen(k.id),
        cells: [h('b', null, k.name), h(Chip, { tone: TONE[k.status] }, k.status), k.description || '—', (k.updatedAt || '').slice(0, 19)] })), empty: '没有知识库。' })),
    open ? h(KbDetail, { kbId: open, onChanged: () => { setOpen(null); load() } }) : h(CreateKb, { onCreated: load }))
}

export default { id: 'knowledge', label: '知识库', group: '构建', Page: KnowledgePage }
