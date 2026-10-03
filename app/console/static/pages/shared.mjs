// What the pages share (not a page itself): a workspace's API path, a list loaded from the API, a base64 file saved.
import React from 'react'
import { call, listOf } from '../ui.mjs'

const { useState, useEffect } = React

export const ws = (wid, path) => `/workspaces/${wid}${path}`

// The list under `key` of GET `url`, loaded once there is a workspace (and again when `url` or `deps` change): [list, load, error].
export function useList(wid, url, key, deps = []) {
  const [list, setList] = useState(null)
  const [error, setError] = useState(null)
  const load = () => call('GET', url).then((r) => { setList(listOf(r[key])); setError(null) }).catch(setError)
  useEffect(() => { if (wid) load() }, [wid, url, ...deps])
  return [list, load, error]
}

// useList for a page whose paths are under `base(wid, path)` (ws, or the page's own prefix).
export const listUnder = (base) => (wid, path, key, deps = []) => useList(wid, base(wid, path), key, deps)

export function download(filename, base64, type = 'application/zip') {
  const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0))
  const url = URL.createObjectURL(new Blob([bytes], { type }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 2000)
}
