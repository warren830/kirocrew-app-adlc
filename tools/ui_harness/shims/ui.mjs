// Minimal stand-in for @kirocrew/app-sdk/ui: the seven components the app imports, rendered as plain elements.
import React from 'react'
const h = React.createElement

export function Card({ className = '', children }) { return h('section', { className: 'k-card ' + className }, children) }
export function CardTitle({ children }) { return h('h3', { className: 'k-card-title' }, children) }
export function Btn({ children, onClick, disabled, variant = 'primary', type = 'button' }) {
  return h('button', { type, onClick, disabled, className: 'k-btn k-btn-' + variant }, children)
}
export function Input({ value, onChange, placeholder, disabled, type = 'text' }) {
  return h('input', { className: 'k-input', value, onChange, placeholder, disabled, type, 'aria-label': placeholder })
}
export function Badge({ children, tone = 'neutral' }) { return h('span', { className: 'k-badge k-badge-' + tone }, children) }
export function StatCard({ label, value, accent }) {
  return h('div', { className: 'k-stat' + (accent ? ' k-stat-accent' : '') }, h('div', { className: 'k-stat-label' }, label), h('div', { className: 'k-stat-value' }, String(value)))
}
export function PageHeader({ title, subtitle }) { return h('header', { className: 'k-page-header' }, h('h1', null, title), subtitle ? h('p', null, subtitle) : null) }
