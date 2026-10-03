// The console's shared layer: transport, state context and UI primitives (pages import these; they never fetch).
import React from 'react'

const { useState, useEffect, useCallback, useContext, createContext, createElement: h } = React

// ---------------------------------------------------------------------------
// Transport: every platform call goes through call() / events(); pages never fetch themselves
// ---------------------------------------------------------------------------

// Where the console is, as the page that serves it writes it (index.html): on its own server the API is /api/console;
// inside KiroCrew it is /apps/workshop-customizer/api/console, the App's backend behind the gateway's signed proxy.
const meta = (name, fallback) => (document.querySelector(`meta[name="${name}"]`) || {}).content || fallback
const API = meta('adlc-api', '/api/console')
const HOST = meta('adlc-host', 'standalone')
export const IN_KIROCREW = HOST === 'kirocrew'

// Inside KiroCrew the 工作坊 item opens KiroCrew's own Workshop Customizer view: this frame's parent, the App's page.
export function openWorkshop() {
  window.parent.postMessage({ type: 'adlc-console:open', view: 'workshop' }, window.location.origin)
}

async function read(response) {
  const text = await response.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch (_) { data = { error: text.slice(0, 300) } }
  return data
}

// An error a page shows. Inside KiroCrew the gateway answers some of them itself: say what they mean there.
function failure(status, data) {
  let message = (data && data.error) || `HTTP ${status}`
  if (IN_KIROCREW && status === 401) message = `KiroCrew 的登录过期了：刷新 KiroCrew 的页面再试（${message}）`
  else if (IN_KIROCREW && status === 502 && /backend unreachable|no reachable backend/.test(message)) message = `App 的后端没有响应（KiroCrew 也许正在重启它）：等几秒再试（${message}）`
  else if (IN_KIROCREW && status === 504) message = `KiroCrew 的网关等了 30 秒没等到回复（它代理每个请求最长 30 秒）：操作也许还在服务器上继续，过一会儿刷新看结果（${message}）`
  return Object.assign(new Error(message), { status, data })
}

// One poll of a ticket (inside KiroCrew, work that outlives one proxied request: a late answer, a turn's events). The
// server holds each poll at most 1.5 s; a poll that fails (the backend restarting) is tried again a few times.
async function poll(ticket, after) {
  for (let attempt = 0; ; attempt++) {
    try {
      const response = await fetch(`${API}/tickets/${ticket}?after=${after}`, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      const data = await read(response)
      if (response.ok) return data
      if (response.status < 500 || attempt >= 4) throw failure(response.status, data)
    } catch (err) {
      if (err.status || attempt >= 4) throw err
    }
    await new Promise((done) => setTimeout(done, 1000 * (attempt + 1)))
  }
}

export async function call(method, path, body) {
  const response = await fetch(API + path, {
    method, credentials: 'same-origin',
    headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  let status = response.status
  let data = await read(response)
  const ticket = status === 202 && response.headers.get('X-Adlc-Ticket')
  if (ticket) {  // still running on the server past the proxy's limit: its answer comes from the ticket
    for (;;) {
      const got = await poll(ticket, 0)
      if (got.done) { status = got.status; data = got.payload; break }
    }
  }
  if (status >= 400) throw failure(status, data)
  return data
}

// The events of a POST that answers with a stream (the chat, the assistant's turn): calls onEvent for each {type, ...}.
// On its own server they come as server-sent events; inside KiroCrew, whose proxy cuts every request at 30 s, the
// backend runs the turn on its own and the page polls its events, each poll a short GET.
export async function events(path, body, onEvent) {
  if (IN_KIROCREW) return polledEvents(path, body, onEvent)
  const response = await fetch(API + path, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  if (!response.ok) {
    let data = null
    try { data = await response.json() } catch (_) { /* not JSON */ }
    throw Object.assign(new Error((data && data.error) || `HTTP ${response.status}`), { status: response.status, data })
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let cut
    while ((cut = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, cut)
      buffer = buffer.slice(cut + 2)
      const line = block.split('\n').find((l) => l.startsWith('data: '))
      if (line) onEvent(JSON.parse(line.slice(6)))
    }
  }
}

async function polledEvents(path, body, onEvent) {
  const response = await fetch(API + path, { method: 'POST', credentials: 'same-origin',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-Adlc-Events': 'poll' }, body: JSON.stringify(body) })
  const data = await read(response)
  if (!response.ok) throw failure(response.status, data)
  const ticket = response.headers.get('X-Adlc-Ticket') || (data && data.ticket)
  if (!ticket) throw failure(502, { error: 'the turn did not start: no ticket came back' })
  let after = 0
  for (;;) {
    const got = await poll(ticket, after)
    for (const event of got.events || []) onEvent(event)
    after = got.next
    if (got.done) {
      if (got.status >= 400) throw failure(got.status, got.payload)
      return
    }
  }
}

export const api = {
  me: () => call('GET', '/me'),
  login: (username, password) => call('POST', '/login', { username, password }),
  logout: () => call('POST', '/logout', {}),
  workspaces: () => call('GET', '/workspaces'),
  putWorkspace: (body) => call('POST', '/workspaces', body),
  deleteWorkspace: (wid) => call('DELETE', `/workspaces/${wid}`),
  verify: (wid) => call('POST', `/workspaces/${wid}/verify`, {}),
  spokeRole: () => call('GET', '/spoke-role'),
  users: () => call('GET', '/users'),
  putUser: (body) => call('POST', '/users', body),
  deleteUser: (name) => call('DELETE', `/users/${name}`),
  keys: () => call('GET', '/keys'),
  issueKey: (body) => call('POST', '/keys', body),
  revokeKey: (kid) => call('DELETE', `/keys/${kid}`),
  agents: (wid) => call('GET', `/workspaces/${wid}/agents`),
  agent: (wid, kind, id) => call('GET', `/workspaces/${wid}/agents/${kind}/${id}`),
  createAgent: (wid, body) => call('POST', `/workspaces/${wid}/agents`, body),
  updateAgent: (wid, id, body) => call('PUT', `/workspaces/${wid}/agents/harness/${id}`, body),
  deleteAgent: (wid, id) => call('DELETE', `/workspaces/${wid}/agents/harness/${id}`),
  chat: (wid, kind, id, body, onEvent) => events(`/workspaces/${wid}/agents/${kind}/${id}/chat`, body, onEvent),
}

// ---------------------------------------------------------------------------
// State: the caller, the workspaces, the current one; toasts
// ---------------------------------------------------------------------------

export const Ctx = createContext(null)
export const useCtx = () => useContext(Ctx)
const WS_KEY = 'adlc-console:workspace'
export const listOf = (v) => (Array.isArray(v) ? v : [])

export function useConsoleState() {
  const [me, setMe] = useState(undefined)
  const [workspaces, setWorkspaces] = useState([])
  const [wid, setWidState] = useState(() => window.localStorage.getItem(WS_KEY) || '')
  const [toasts, setToasts] = useState([])
  const toast = useCallback((kind, message) => {
    const id = Math.random().toString(36).slice(2)
    setToasts((list) => [...list, { id, kind, message: String(message) }].slice(-4))
    setTimeout(() => setToasts((list) => list.filter((t) => t.id !== id)), 5000)
  }, [])
  useEffect(() => {
    const onNotify = (e) => toast(e.detail.kind, e.detail.message)
    window.addEventListener('adlc-notify', onNotify)
    return () => window.removeEventListener('adlc-notify', onNotify)
  }, [toast])
  const reload = useCallback(async () => {
    try {
      const who = await api.me()
      setMe(who)
      const list = listOf((await api.workspaces()).workspaces)
      setWorkspaces(list)
      if (!list.some((w) => w.id === window.localStorage.getItem(WS_KEY)) && list[0]) setWid(list[0].id)
    } catch (err) {
      if (err.status === 401) setMe(null)
      else toast('error', err.message)
    }
  }, [toast])
  const setWid = (id) => { window.localStorage.setItem(WS_KEY, id); setWidState(id) }
  useEffect(() => { reload() }, [reload])
  return { me, workspaces, wid, setWid, reload, toast, toasts, workspace: workspaces.find((w) => w.id === wid) || null }
}

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------

export const cx = (...names) => names.filter(Boolean).join(' ')
export function Card({ title, extra, children }) {
  return h('section', { className: 'cs-card' }, title || extra ? h('div', { className: 'cs-card-h' }, h('b', null, title), extra || null) : null, children)
}
export function Button({ onClick, children, kind, disabled, busy, icon, title }) {
  return h('button', { type: 'button', className: cx('cs-btn', kind && `cs-${kind}`), onClick, disabled: disabled || busy, title },
    busy ? h('span', { className: 'cs-spin' }) : icon ? h(icon, { size: 13 }) : null, children)
}
export function Field({ label, hint, children }) {
  return h('label', { className: 'cs-field' }, h('span', null, label), children, hint ? h('small', null, hint) : null)
}
export function Input({ value, onChange, placeholder, type, mono }) {
  return h('input', { className: cx('cs-input', mono && 'cs-mono'), type: type || 'text', value: value ?? '', placeholder, onChange: (e) => onChange(e.target.value) })
}
export function TextArea({ value, onChange, rows, placeholder, mono }) {
  return h('textarea', { className: cx('cs-input', mono && 'cs-mono'), rows: rows || 4, value: value ?? '', placeholder, onChange: (e) => onChange(e.target.value) })
}
export function Select({ value, onChange, options }) {
  return h('select', { className: 'cs-input', value, onChange: (e) => onChange(e.target.value) }, options.map(([v, t]) => h('option', { key: v, value: v }, t)))
}
export function Chip({ tone, children, title }) { return h('span', { className: cx('cs-chip', tone && `cs-${tone}`), title }, children) }
export function Note({ tone, children }) { return h('div', { className: cx('cs-note', tone && `cs-tx-${tone}`) }, children) }
export function Empty({ children }) { return h('div', { className: 'cs-empty' }, children) }
export function ErrorLine({ error }) { return error ? h('div', { className: 'cs-err' }, error.message || String(error)) : null }
export function Table({ head, rows, empty }) {
  if (!rows.length) return h(Empty, null, empty || '没有内容')
  return h('div', { className: 'cs-tablew' }, h('table', { className: 'cs-table' },
    h('thead', null, h('tr', null, head.map((c, i) => h('th', { key: i }, c)))),
    h('tbody', null, rows.map((r, i) => h('tr', { key: r.key || i, onClick: r.onClick, className: r.onClick ? 'cs-click' : '' }, r.cells.map((c, j) => h('td', { key: j }, c)))))))
}
export const STATUS_TONE = { READY: 'ok', ACTIVE: 'ok', CREATING: 'info', UPDATING: 'info', FAILED: 'bad', DELETING: 'warn' }

// One async action at a time per component, with its error.
export function useAction() {
  const { toast } = useCtx()
  const [busy, setBusy] = useState('')
  const [error, setError] = useState(null)
  const run = useCallback(async (name, fn, success) => {
    setBusy(name); setError(null)
    try {
      const out = await fn()
      if (success) toast('ok', typeof success === 'function' ? success(out) : success)
      return out
    } catch (err) { setError(err); toast('error', err.message); return undefined } finally { setBusy('') }
  }, [toast])
  return { busy, error, run }
}


// Polls a console job (GET /jobs/{id}) every two seconds while it runs; returns the latest job.
export function useJob(id) {
  const [job, setJob] = useState(null)
  useEffect(() => {
    if (!id) { setJob(null); return undefined }
    let live = true
    let timer = null
    const tick = async () => {
      try {
        const next = await call('GET', `/jobs/${id}`)
        if (!live) return
        setJob(next)
        if (next.status === 'running') timer = setTimeout(tick, 2000)
      } catch (_) { if (live) timer = setTimeout(tick, 4000) }
    }
    tick()
    return () => { live = false; clearTimeout(timer) }
  }, [id])
  return job
}

export function Tabs({ value, onChange, options }) {
  return h('div', { className: 'cs-tabs' }, options.map(([v, label]) => h('a', { key: v, className: cx('cs-tab', v === value && 'on'), onClick: () => onChange(v) }, label)))
}

export const JOB_TONE = { running: 'info', succeeded: 'ok', failed: 'bad', interrupted: 'warn' }
export const JOB_LABEL = { running: '进行中', succeeded: '完成', failed: '失败', interrupted: '被中断' }
