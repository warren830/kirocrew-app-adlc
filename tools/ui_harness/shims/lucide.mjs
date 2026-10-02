// Icon stand-ins: every named icon the host re-exports becomes a small inline glyph.
import React from 'react'
const icon = (glyph) => ({ size = 14 }) => React.createElement('span', { className: 'k-icon', style: { fontSize: size + 'px' }, 'aria-hidden': true }, glyph)
export const RefreshCw = icon('↻'), Plus = icon('+'), Shield = icon('⛨'), AlertTriangle = icon('⚠'), Package = icon('▣'), Download = icon('⤓'),
  Rocket = icon('➚'), ArrowLeft = icon('←'), ArrowRight = icon('→'), Check = icon('✓'), Menu = icon('≡'), Code = icon('‹›'), Settings = icon('⚙'),
  Clock = icon('◷'), Bot = icon('🤖'), Zap = icon('⚡'), Sparkles = icon('✦'), Wand2 = icon('✦'), X = icon('×'), Search = icon('⌕'), Star = icon('★'),
  Home = icon('⌂'), Users = icon('👥'), Trash2 = icon('🗑'), Loader2 = icon('◌'), ExternalLink = icon('↗'), ChevronRight = icon('›')
