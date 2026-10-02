// Minimal stand-in for @kirocrew/app-sdk: same call surface the app uses (useAppApi().get/post/put/delete, useNotify()).
import { useMemo } from 'react'

async function request(method, path, body) {
  const res = await fetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
  const text = await res.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = text }
  if (!res.ok) {
    const err = new Error(`${method} ${path} → ${res.status}: ${data?.error || text}`)
    err.status = res.status
    err.data = data
    throw err
  }
  return data
}

export function useAppApi() {
  return useMemo(() => ({
    get: (p) => request('GET', p),
    post: (p, b) => request('POST', p, b ?? {}),
    put: (p, b) => request('PUT', p, b ?? {}),
    delete: (p) => request('DELETE', p),
  }), [])
}

function toast(kind, msg) {
  let list = document.getElementById('harness-toasts')
  if (!list) {
    list = document.createElement('ul')
    list.id = 'harness-toasts'
    document.body.appendChild(list)
  }
  const li = document.createElement('li')
  li.className = kind
  li.textContent = `[${kind}] ${msg}`
  list.appendChild(li)
}

export function useNotify() {
  return useMemo(() => ({
    error: (m) => toast('error', m),
    success: (m) => toast('success', m),
    info: (m) => toast('info', m),
  }), [])
}

export function useAppEvents() {}
export function useAppInfo() { return { name: 'workshop-customizer' } }
