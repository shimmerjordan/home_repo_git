// 本地数据层 —— 用 JS 重写 NAS 版 backend 的 models + services/inventory + services/audit。
// 字段命名与后端 serialize_item / serialize_location / serialize_transaction / audit.serialize **完全对齐**,
// 以保证两端的备份包 (data/*.json) 可互相导入。
//
// 存储: util.storage (小程序 wx.storage / node 内存)。
// 表: locations / items / transactions / audit, 各存一个数组; 计数器存 vs_counters。

const { storage, fsStore, nowISO, uuid4 } = require('./util.js')

const K = {
  loc: 'vs_locations',
  item: 'vs_items',
  tx: 'vs_transactions',
  audit: 'vs_audit',
  counters: 'vs_counters',
  settings: 'vs_settings',
}

// 大表走 FileSystemManager (200MB), 计数器/设置走 storage (小且需独立持久)。
const BIG = { [K.loc]: 1, [K.item]: 1, [K.tx]: 1, [K.audit]: 1 }
function _backend(key) { return BIG[key] ? fsStore : storage }
function _all(key) { return _backend(key).get(key, []) }
function _save(key, arr) { _backend(key).set(key, arr) }

function _nextId(table) {
  const c = storage.get(K.counters, {})
  const id = (c[table] || 0) + 1
  c[table] = id
  storage.set(K.counters, c)
  return id
}

// ---------------------------------------------------------------- audit
function _audit(entity_type, entity_id, entity_name, action, changes, summary, source) {
  const rows = _all(K.audit)
  rows.push({
    id: _nextId('audit'),
    ts: nowISO(),
    entity_type,
    entity_id,
    entity_name: entity_name || '',
    action,
    changes: changes || {},
    summary: summary || '',
    source: source || 'mp',
  })
  _save(K.audit, rows)
}

function _diff(before, after, fields) {
  const changes = {}
  fields.forEach((f) => {
    if ((before[f] ?? null) !== (after[f] ?? null)) changes[f] = [before[f] ?? null, after[f] ?? null]
  })
  return changes
}

// ---------------------------------------------------------------- locations
function listLocations() { return _all(K.loc) }
function getLocation(id) { return _all(K.loc).find((l) => l.id === id) || null }

function locationPath(id) {
  const all = _all(K.loc)
  const byId = {}
  all.forEach((l) => (byId[l.id] = l))
  const parts = []
  const seen = {}
  let cur = byId[id]
  while (cur && !seen[cur.id]) {
    parts.unshift(cur.name)
    seen[cur.id] = 1
    cur = cur.parent_id != null ? byId[cur.parent_id] : null
  }
  return parts.join(' / ')
}

function createLocation(data) {
  const rows = _all(K.loc)
  const loc = {
    id: _nextId('locations'),
    uuid: uuid4(),
    name: data.name || '未命名',
    kind: data.kind || 'room',
    parent_id: data.parent_id ?? null,
    note: data.note || '',
    geometry: data.geometry || null,
    created_at: nowISO(),
  }
  rows.push(loc)
  _save(K.loc, rows)
  _audit('location', loc.id, loc.name, 'create', { _created: true }, `新建位置「${loc.name}」`)
  return loc
}

function updateLocation(id, patch) {
  const rows = _all(K.loc)
  const i = rows.findIndex((l) => l.id === id)
  if (i < 0) return null
  const before = { ...rows[i] }
  rows[i] = { ...rows[i], ...patch, id }
  _save(K.loc, rows)
  const changes = _diff(before, rows[i], ['name', 'kind', 'parent_id', 'note'])
  if (Object.keys(changes).length) _audit('location', id, rows[i].name, 'update', changes, `修改位置「${rows[i].name}」`)
  return rows[i]
}

function deleteLocation(id) {
  // 同后端: 有子位置或物品时拒绝删除, 避免孤儿。
  const items = _all(K.item).filter((it) => it.location_id === id)
  const children = _all(K.loc).filter((l) => l.parent_id === id)
  if (items.length || children.length) {
    throw new Error(`该位置下还有 ${items.length} 件物品 / ${children.length} 个子位置, 不能删除`)
  }
  const rows = _all(K.loc)
  const loc = rows.find((l) => l.id === id)
  _save(K.loc, rows.filter((l) => l.id !== id))
  if (loc) _audit('location', id, loc.name, 'delete', {}, `删除位置「${loc.name}」`)
}

// ---------------------------------------------------------------- items
function _serializeItem(it) {
  return { ...it, location_path: it.location_id != null ? locationPath(it.location_id) : null }
}

function listItems(params = {}) {
  let rows = _all(K.item)
  const { q, category, location_id } = params
  if (category) rows = rows.filter((it) => (it.category || '') === category)
  if (location_id != null && location_id !== '') rows = rows.filter((it) => it.location_id === Number(location_id))
  if (q) {
    const kw = String(q).toLowerCase().trim()
    rows = rows.filter((it) => {
      const hay = [it.name, it.aliases, it.category, it.tags, it.note].join(' ').toLowerCase()
      return hay.includes(kw)
    })
  }
  return rows.map(_serializeItem)
}

function getItem(id) {
  const it = _all(K.item).find((x) => x.id === id)
  return it ? _serializeItem(it) : null
}

function createItem(data) {
  const rows = _all(K.item)
  const it = {
    id: _nextId('items'),
    name: data.name || '未命名',
    aliases: data.aliases || '',
    category: data.category || '',
    tags: data.tags || '',
    quantity: data.quantity != null ? Number(data.quantity) : 1,
    price: data.price != null ? Number(data.price) : 0,
    note: data.note || '',
    location_id: data.location_id ?? null,
    pos_x: data.pos_x ?? null,
    pos_z: data.pos_z ?? null,
    created_at: nowISO(),
    updated_at: nowISO(),
  }
  rows.push(it)
  _save(K.item, rows)
  _audit('item', it.id, it.name, 'create', { _created: true }, `新建物品「${it.name}」`)
  return _serializeItem(it)
}

function updateItem(id, patch) {
  const rows = _all(K.item)
  const i = rows.findIndex((x) => x.id === id)
  if (i < 0) return null
  const before = { ...rows[i] }
  rows[i] = { ...rows[i], ...patch, id, updated_at: nowISO() }
  _save(K.item, rows)
  const changes = _diff(before, rows[i], ['name', 'aliases', 'category', 'tags', 'quantity', 'price', 'note', 'location_id'])
  if (Object.keys(changes).length) _audit('item', id, rows[i].name, 'update', changes, `修改物品「${rows[i].name}」`)
  return _serializeItem(rows[i])
}

function deleteItem(id) {
  const rows = _all(K.item)
  const it = rows.find((x) => x.id === id)
  _save(K.item, rows.filter((x) => x.id !== id))
  // 连带删除流水 (同后端 cascade)
  _save(K.tx, _all(K.tx).filter((t) => t.item_id !== id))
  if (it) _audit('item', id, it.name, 'delete', {}, `删除物品「${it.name}」`)
}

// ---------------------------------------------------------------- transactions
function recordTransaction(item_id, data) {
  const item = _all(K.item).find((x) => x.id === item_id)
  if (!item) throw new Error('物品不存在')
  const rows = _all(K.tx)
  const tx = {
    id: _nextId('transactions'),
    item_id,
    item_name: item.name,
    action: data.action, // take_out / put_in / adjust
    quantity: Number(data.quantity || 1),
    location_id: data.location_id ?? item.location_id ?? null,
    note: data.note || '',
    created_at: nowISO(),
  }
  rows.push(tx)
  _save(K.tx, rows)
  // 同步数量
  let q = item.quantity || 0
  if (tx.action === 'take_out') q -= tx.quantity
  else if (tx.action === 'put_in') q += tx.quantity
  else if (tx.action === 'adjust') q = tx.quantity
  updateItem(item_id, { quantity: Math.max(0, q) })
  return tx
}

function listTransactions(limit = 50) {
  return _all(K.tx).slice().reverse().slice(0, limit)
}
function listAudit(limit = 200) {
  return _all(K.audit).slice().reverse().slice(0, limit)
}

// ---------------------------------------------------------------- settings
function getSettings() { return storage.get(K.settings, {}) }
function setSettings(patch) {
  const cur = storage.get(K.settings, {})
  const next = { ...cur, ...patch }
  storage.set(K.settings, next)
  return next
}

// ---------------------------------------------------------------- seed
function ensureSeed() {
  if (_all(K.loc).length === 0) {
    createLocation({ name: '我家', kind: 'home', parent_id: null, note: '默认顶层, 可改名' })
  }
}

// ---------------------------------------------------------------- 备份数据载荷 (与 NAS 版 data/*.json 对齐)
function exportData() {
  return {
    locations: _all(K.loc).map((l) => ({ ...l, full_path: locationPath(l.id) })),
    items: _all(K.item).map(_serializeItem),
    transactions: _all(K.tx),
    audit: _all(K.audit),
    settings: getSettings(),
  }
}

// 从备份载荷导入。mode: 'replace' 清空后导入; 'merge' 仅按 id 覆盖/追加 (默认 replace, 简单可靠)。
function importData(data, mode = 'replace') {
  if (mode === 'replace') {
    _save(K.loc, (data.locations || []).map(_stripDerived))
    _save(K.item, (data.items || []).map(_stripDerived))
    _save(K.tx, data.transactions || [])
    _save(K.audit, data.audit || [])
    if (data.settings) storage.set(K.settings, data.settings)
    _rebuildCounters()
  } else {
    _mergeById(K.loc, (data.locations || []).map(_stripDerived))
    _mergeById(K.item, (data.items || []).map(_stripDerived))
    _mergeById(K.tx, data.transactions || [])
    _mergeById(K.audit, data.audit || [])
    _rebuildCounters()
  }
}

function _stripDerived(o) {
  const { location_path, full_path, ...rest } = o
  return rest
}
function _mergeById(key, incoming) {
  const cur = _all(key)
  const byId = {}
  cur.forEach((r) => (byId[r.id] = r))
  incoming.forEach((r) => (byId[r.id] = r))
  _save(key, Object.values(byId).sort((a, b) => (a.id || 0) - (b.id || 0)))
}
function _rebuildCounters() {
  const c = {}
  const max = (key) => _all(key).reduce((m, r) => Math.max(m, r.id || 0), 0)
  c.locations = max(K.loc)
  c.items = max(K.item)
  c.transactions = max(K.tx)
  c.audit = max(K.audit)
  storage.set(K.counters, c)
}

function _clearAll() {
  ;[K.loc, K.item, K.tx, K.audit, K.counters, K.settings].forEach((k) => _backend(k).remove(k))
}

module.exports = {
  K,
  listLocations, getLocation, locationPath, createLocation, updateLocation, deleteLocation,
  listItems, getItem, createItem, updateItem, deleteItem,
  recordTransaction, listTransactions, listAudit,
  getSettings, setSettings,
  ensureSeed, exportData, importData, _clearAll,
}
