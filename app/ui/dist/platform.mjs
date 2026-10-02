// ADLC 控制台 — the Workshop Customizer App's page in KiroCrew (hand-written ESM, no build step).
// KiroCrew gives an App one sidebar entry and one route (/apps/workshop-customizer), so this page holds both views:
//   控制台 — the ADLC console, framed from the App's own backend (/apps/workshop-customizer/api/console/), which is
//            behind the gateway's signed proxy and KiroCrew's sign-in;
//   工作坊 — the Workshop Customizer page itself (./index.mjs), rendered here by KiroCrew with the host SDK, not a
//            second copy inside the frame. The console's 工作坊 item asks for it with a message.
// Resolved through the host import map: react (and, through index.mjs, @kirocrew/app-sdk and lucide-react).

import React from 'react'
import WorkshopCustomizerApp from './index.mjs'

const { useState, useEffect, useRef, createElement: h } = React

export const CONSOLE_URL = '/apps/workshop-customizer/api/console/'
const VIEW_KEY = 'workshop-customizer:view'
const VIEWS = [['console', 'ADLC 控制台'], ['workshop', '工作坊 · Workshop Customizer']]

// A Workshop deep link (#wc=… or ?wc=…) opens 工作坊; else the view this tab had last; else the console.
function initialView() {
  try {
    if (/(?:^|[#?&])wc=/.test(window.location.hash) || /(?:^|[?&])wc=/.test(window.location.search)) return 'workshop'
    const saved = window.sessionStorage.getItem(VIEW_KEY)
    if (VIEWS.some(([v]) => v === saved)) return saved
  } catch (_) { /* no storage: the console */ }
  return 'console'
}

export default function AdlcPlatformPage() {
  const [view, setView] = useState(initialView)
  const [framed, setFramed] = useState(view === 'console')  // the frame loads the first time it is shown, then stays
  const frame = useRef(null)
  useEffect(() => {
    if (view === 'console') setFramed(true)
    try { window.sessionStorage.setItem(VIEW_KEY, view) } catch (_) { /* ignore */ }
  }, [view])
  useEffect(() => {
    const onMessage = (e) => {  // only the console's own frame, on this origin, may switch the view
      if (e.origin !== window.location.origin || !frame.current || e.source !== frame.current.contentWindow) return
      const data = e.data || {}
      if (data.type === 'adlc-console:open' && VIEWS.some(([v]) => v === data.view)) setView(data.view)
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
  return h('div', { className: 'adlc-page', style: { display: 'flex', flexDirection: 'column', flex: '1 1 auto', minHeight: 0, height: '100%' } },
    h('style', null, CSS),
    h('nav', { className: 'adlc-tabs', 'aria-label': 'ADLC' }, VIEWS.map(([v, label]) => h('button', {
      key: v, type: 'button', className: v === view ? 'adlc-tab on' : 'adlc-tab', 'aria-pressed': v === view, onClick: () => setView(v) }, label))),
    framed ? h('iframe', { ref: frame, src: CONSOLE_URL, title: 'ADLC 控制台', className: 'adlc-frame', style: { display: view === 'console' ? 'block' : 'none' } }) : null,
    view === 'workshop' ? h('div', { className: 'adlc-workshop' }, h(WorkshopCustomizerApp)) : null)
}

const CSS = `
.adlc-tabs{display:flex;gap:4px;padding:6px 12px 0;border-bottom:1px solid var(--border,#dce3de);flex:0 0 auto}
.adlc-tab{border:0;background:transparent;padding:6px 12px;cursor:pointer;color:var(--muted,#5d6a63);border-bottom:2px solid transparent;font:inherit;font-size:13px}
.adlc-tab:hover{color:var(--text,#1c2521)}
.adlc-tab.on{color:var(--text-strong,#0e1813);border-bottom-color:var(--accent,#1f6b57);font-weight:600}
.adlc-frame{flex:1 1 auto;min-height:0;width:100%;border:0;background:#f5f7fa}
.adlc-workshop{display:flex;flex-direction:column;flex:1 1 auto;min-height:0}
`
