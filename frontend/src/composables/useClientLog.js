// Reactive in-memory log for the frontend. Captures errors and our own events,
// and forwards each entry to /api/logs/client so backend logs page sees them too.
import { reactive } from 'vue'
import { api } from '../api'

const state = reactive({ entries: [] })
let counter = 0

function push(level, message, context = null) {
  counter += 1
  const entry = { id: counter, time: new Date().toISOString(), level, message, context }
  state.entries.unshift(entry)
  if (state.entries.length > 500) state.entries.length = 500
  if (level === 'ERROR' || level === 'WARNING') {
    api.postClientLog(level, message, context)
  }
}

let installed = false
export function installClientLog() {
  if (installed || typeof window === 'undefined') return
  installed = true
  window.addEventListener('error', (ev) => {
    push('ERROR', ev.message || 'window error', {
      filename: ev.filename, lineno: ev.lineno, colno: ev.colno,
    })
  })
  window.addEventListener('unhandledrejection', (ev) => {
    const reason = ev.reason
    push('ERROR', 'unhandled promise rejection: ' + (reason?.message || String(reason)))
  })
  // Capture console.error too.
  const orig = console.error.bind(console)
  console.error = (...args) => {
    try { push('ERROR', args.map((a) => (a?.stack || String(a))).join(' ')) } catch {}
    orig(...args)
  }
  push('INFO', 'Frontend log capture installed')
}

export function clientLog() { return state }
export function logEvent(level, message, context = null) { push(level.toUpperCase(), message, context) }
