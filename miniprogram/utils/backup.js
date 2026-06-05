// 备份模块: 把本地数据打成与 NAS 版一致的 ZIP 包 (manifest + config.json + data/*.json),
// 上传到 WebDAV。因小程序无 SQLite, 包里**没有** db/storage.db —— 数据以 data/*.json 为权威。
// NAS 版后端的 restore 已加 JSON 回退, 可直接恢复本包 (mini -> NAS 跨端)。
//
// 列目录靠远程的 index.json (见 webdav.js 注释: wx.request 不支持 PROPFIND)。

const store = require('./store.js')
const zip = require('./zip.js')
const webdav = require('./webdav.js')
const crypto = require('./crypto.js')
const { nowISO } = require('./util.js')

const FORMAT_VERSION = 1
const INDEX = 'index.json'
const ALL_COMPONENTS = ['settings', 'inventory', 'transactions', 'audit']

function _stamp() {
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
}

// 构建备份包 (Uint8Array) + manifest + 文件名。components 默认全选。
function buildPackage(components) {
  const comps = (components && components.length ? components : ALL_COMPONENTS).filter((c) =>
    ALL_COMPONENTS.includes(c)
  )
  const data = store.exportData()
  const files = {}
  if (comps.includes('settings')) files['config.json'] = JSON.stringify(data.settings || {}, null, 2)
  if (comps.includes('inventory')) {
    files['data/items.json'] = JSON.stringify(data.items, null, 2)
    files['data/locations.json'] = JSON.stringify(data.locations, null, 2)
  }
  if (comps.includes('transactions')) files['data/transactions.json'] = JSON.stringify(data.transactions, null, 2)
  if (comps.includes('audit')) files['data/audit.json'] = JSON.stringify(data.audit, null, 2)

  const manifest = {
    format_version: FORMAT_VERSION,
    app: 'voice-storage',
    source: 'miniprogram',
    created_at: nowISO(),
    components: comps,
    files: Object.keys(files),
  }
  files['manifest.json'] = JSON.stringify(manifest, null, 2)

  const bytes = zip.zipStore(files)
  const name = `backup-${_stamp()}.zip`
  return { bytes, manifest, name }
}

function _toArrayBuffer(u8) {
  return u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength)
}

// 上传 + 维护 index.json。cfg.passphrase 非空时用 AES-256-GCM 加密 (与 NAS 版互通)。
async function runBackup(cfg, components) {
  const { bytes, manifest, name: baseName } = buildPackage(components)
  let payload = bytes
  let name = baseName
  const encrypted = !!(cfg && cfg.passphrase)
  if (encrypted) {
    payload = crypto.encryptBackup(bytes, cfg.passphrase)
    name = baseName.replace(/\.zip$/, '.enc.zip') // 匹配 NAS 版命名约定
  }
  await webdav.put(cfg, name, _toArrayBuffer(payload))
  const list = await listBackups(cfg)
  list.unshift({ name, size: payload.length, created_at: manifest.created_at, source: 'miniprogram', encrypted })
  await _writeIndex(cfg, list)
  return { name, size: payload.length, components: manifest.components, encrypted }
}

async function listBackups(cfg) {
  const txt = await webdav.getText(cfg, INDEX)
  if (!txt) return []
  try {
    const j = JSON.parse(txt)
    return Array.isArray(j.backups) ? j.backups : []
  } catch (e) {
    return []
  }
}

function _writeIndex(cfg, backups) {
  const u8 = zip.utf8Encode(JSON.stringify({ backups }, null, 2))
  return webdav.put(cfg, INDEX, _toArrayBuffer(u8))
}

async function deleteBackup(cfg, name) {
  await webdav.del(cfg, name)
  const list = (await listBackups(cfg)).filter((b) => b.name !== name)
  await _writeIndex(cfg, list)
}

// 从 ZIP 字节恢复到本地 store。targets: 'data' (物品/位置/流水/审计) / 'settings'。
// 自动识别加密包 (VSBK1) 并用 passphrase 解密。
function restoreFromBytes(bytes, targets, mode, passphrase) {
  let u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes)
  if (crypto.isEncrypted(u8)) {
    if (!passphrase) throw new Error('该备份已加密, 请填写解密口令')
    u8 = crypto.decryptBackup(u8, passphrase)
  }
  // 只解 JSON 相关条目; 跳过 NAS 包里的 db/storage.db (小程序用不到, 避免无谓解压)。
  const entries = zip.unzip(u8, { filter: (n) => n === 'manifest.json' || n === 'config.json' || n.indexOf('data/') === 0 })
  const readJSON = (n) => (entries[n] ? JSON.parse(zip.utf8Decode(entries[n])) : undefined)

  const tgt = targets && targets.length ? targets : ['data', 'settings']
  const bundle = {}
  if (tgt.includes('data')) {
    bundle.locations = readJSON('data/locations.json') || []
    bundle.items = readJSON('data/items.json') || []
    bundle.transactions = readJSON('data/transactions.json') || []
    bundle.audit = readJSON('data/audit.json') || []
  }
  if (tgt.includes('settings')) {
    const cfg = readJSON('config.json')
    if (cfg) bundle.settings = cfg
  }
  store.importData(bundle, mode || 'replace')
  return { restored: tgt, manifest: readJSON('manifest.json') || {} }
}

async function restoreFromRemote(cfg, name, targets, mode) {
  const ab = await webdav.getBinary(cfg, name)
  if (!ab) throw new Error('远程备份不存在')
  return restoreFromBytes(new Uint8Array(ab), targets, mode, cfg && cfg.passphrase)
}

module.exports = {
  ALL_COMPONENTS,
  buildPackage,
  runBackup,
  listBackups,
  deleteBackup,
  restoreFromBytes,
  restoreFromRemote,
}
