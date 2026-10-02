// @kirocrew/app-sdk for the workshop App outside KiroCrew: its "agent" routes (Kiro generation) are the console
// server's own, and its notifications are the console's toasts.
const call = async (method, url, body) => {
  const response = await fetch(url, { method, credentials: 'same-origin', headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) })
  const text = await response.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch (_) { data = { error: text.slice(0, 300) } }
  if (!response.ok) throw Object.assign(new Error((data && data.error) || `HTTP ${response.status}`), { status: response.status, data })
  return data
}
const api = { get: (u) => call('GET', u), post: (u, b) => call('POST', u, b ?? {}), put: (u, b) => call('PUT', u, b ?? {}), delete: (u) => call('DELETE', u) }
const notify = {
  error: (message) => window.dispatchEvent(new CustomEvent('adlc-notify', { detail: { kind: 'error', message } })),
  success: (message) => window.dispatchEvent(new CustomEvent('adlc-notify', { detail: { kind: 'ok', message } })),
}
export const useAppApi = () => api
export const useNotify = () => notify
