// The few icons the console and the workshop App use, as small inline SVGs (lucide's shapes, simplified).
const R = window.React
const paths = {
  ArrowLeft: 'M19 12H5M12 19l-7-7 7-7', RefreshCw: 'M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5', Wand2: 'M15 4V2M15 16v-2M8 9h2M20 9h2M17.8 11.8 19 13M15 9h0M17.8 6.2 19 5M3 21l9-9M12.2 6.2 11 5',
  Download: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3', Rocket: 'M4.5 16.5c-1.5 1.3-2 5-2 5s3.7-.5 5-2c.7-.8.7-2.1-.1-2.9a2.2 2.2 0 0 0-2.9-.1zM12 15l-3-3a22 22 0 0 1 2-3.9A12.9 12.9 0 0 1 22 2c0 2.7-.8 7.5-6 11a22.4 22.4 0 0 1-4 2z',
  Settings: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', Plus: 'M12 5v14M5 12h14', Trash2: 'M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2',
  Send: 'M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z', Bot: 'M12 8V4H8M4 12h16v8H4zM2 14h2M20 14h2M15 13v2M9 13v2',
}
const icon = (name) => function Icon({ size }) {
  return R.createElement('svg', { width: size || 14, height: size || 14, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2,
    strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true }, R.createElement('path', { d: paths[name] || 'M4 12h16' }))
}
export const { ArrowLeft, RefreshCw, Wand2, Download, Rocket, Settings, Plus, Trash2, Send, Bot } = Object.fromEntries(Object.keys(paths).map((n) => [n, icon(n)]))
export default new Proxy({}, { get: (_, p) => (typeof p === 'string' ? icon(p) : undefined) })
