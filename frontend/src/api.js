// In dev (vite) we go through the /api proxy. In prod (nginx) /api is reverse-proxied to backend.
const BASE = ''

async function request(path, opts = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText}: ${text}`)
  }
  if (res.status === 204) return null
  return res.json()
}

export const api = {
  health: () => request('/api/health'),

  // items
  listItems: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
    ).toString()
    return request(`/api/items${qs ? '?' + qs : ''}`)
  },
  createItem: (data) => request('/api/items', { method: 'POST', body: JSON.stringify(data) }),
  updateItem: (id, data) => request(`/api/items/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteItem: (id) => request(`/api/items/${id}`, { method: 'DELETE' }),
  itemTransactions: (id) => request(`/api/items/${id}/transactions`),
  exportItemsUrl: () => '/api/items/export.csv',
  importTemplateUrl: () => '/api/items/import-template.csv',
  importItems: async (file, mode = 'upsert') => {
    const fd = new FormData()
    fd.append('file', file)
    const res = await fetch(`/api/items/import?mode=${mode}`, { method: 'POST', body: fd })
    if (!res.ok) throw new Error(await res.text())
    return res.json()
  },
  recordTx: (id, data) =>
    request(`/api/items/${id}/transactions`, { method: 'POST', body: JSON.stringify(data) }),
  recentTx: (limit = 50) => request(`/api/transactions?limit=${limit}`),
  searchTx: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
    ).toString()
    return request(`/api/transactions${qs ? '?' + qs : ''}`)
  },

  // diagnostics
  getDiag: () => request('/api/diag'),
  getLogs: (sinceId = 0, level = '') =>
    request(`/api/logs?since_id=${sinceId}${level ? '&level=' + level : ''}`),
  postClientLog: (level, message, context = null) =>
    request('/api/logs/client', {
      method: 'POST',
      body: JSON.stringify({ level, message, context }),
    }).catch(() => null),

  // locations
  listLocations: () => request('/api/locations'),
  createLocation: (data) => request('/api/locations', { method: 'POST', body: JSON.stringify(data) }),
  updateLocation: (id, data) =>
    request(`/api/locations/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteLocation: (id) => request(`/api/locations/${id}`, { method: 'DELETE' }),

  // voice
  voiceIntent: (text, context = null) =>
    request('/api/voice/intent', {
      method: 'POST',
      body: JSON.stringify({ text, context }),
    }),
  transcribe: async (blob) => {
    const fd = new FormData()
    fd.append('audio', blob, 'rec.webm')
    const res = await fetch('/api/voice/transcribe', { method: 'POST', body: fd })
    if (!res.ok) throw new Error(await res.text())
    return res.json()
  },

  // settings
  getSettings: () => request('/api/settings'),
  updateSettings: (patch) =>
    request('/api/settings', { method: 'PATCH', body: JSON.stringify(patch) }),
  testLLM: () => request('/api/settings/test-llm', { method: 'POST' }),
}
