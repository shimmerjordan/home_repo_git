// 通用工具 + 存储 shim。
// 关键: 在小程序里用 wx.*Sync; 在 node (单元测试) 里退化为内存 Map。
// 这样 store.js / backup.js 的纯逻辑可以脱离微信环境用 node 跑通验证。

const hasWx = typeof wx !== 'undefined' && wx.getStorageSync

const memStore = {} // node 测试用

const storage = {
  get(key, def) {
    if (hasWx) {
      const v = wx.getStorageSync(key)
      return v === '' || v === undefined || v === null ? def : v
    }
    return key in memStore ? memStore[key] : def
  },
  set(key, val) {
    if (hasWx) wx.setStorageSync(key, val)
    else memStore[key] = val
  },
  remove(key) {
    if (hasWx) wx.removeStorageSync(key)
    else delete memStore[key]
  },
}

// 文件存储 (大数据): wx.storage 单 key ≤1MB / 共 10MB; 大表 (物品/流水/审计) 改走
// FileSystemManager (共 200MB)。node 测试退化为内存。API 与 storage 一致 (get/set/remove)。
const hasFS = typeof wx !== 'undefined' && wx.getFileSystemManager && wx.env && wx.env.USER_DATA_PATH
const memFS = {}
const fsStore = {
  _path(name) { return `${wx.env.USER_DATA_PATH}/${name}.json` },
  get(name, def) {
    if (hasFS) {
      try {
        const s = wx.getFileSystemManager().readFileSync(this._path(name), 'utf8')
        return JSON.parse(s)
      } catch (e) {
        return def
      }
    }
    return name in memFS ? memFS[name] : def
  },
  set(name, val) {
    if (hasFS) wx.getFileSystemManager().writeFileSync(this._path(name), JSON.stringify(val), 'utf8')
    else memFS[name] = val
  },
  remove(name) {
    if (hasFS) { try { wx.getFileSystemManager().unlinkSync(this._path(name)) } catch (e) { /* ignore */ } }
    else delete memFS[name]
  },
}

function nowISO() {
  return new Date().toISOString()
}

// RFC4122 v4 (够用, 非密码学强随机)。
function uuid4() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

function fmtSize(n) {
  if (!n) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`
}

// base64 (小程序无 btoa)。入参为 Uint8Array。
const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
function base64Encode(bytes) {
  let out = ''
  let i
  for (i = 0; i + 2 < bytes.length; i += 3) {
    const n = (bytes[i] << 16) | (bytes[i + 1] << 8) | bytes[i + 2]
    out += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] + B64[(n >> 6) & 63] + B64[n & 63]
  }
  const rem = bytes.length - i
  if (rem === 1) {
    const n = bytes[i] << 16
    out += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] + '=='
  } else if (rem === 2) {
    const n = (bytes[i] << 16) | (bytes[i + 1] << 8)
    out += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] + B64[(n >> 6) & 63] + '='
  }
  return out
}

function utf8Bytes(str) {
  const out = []
  for (let i = 0; i < str.length; i++) {
    const c = str.charCodeAt(i)
    if (c < 0x80) out.push(c)
    else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f))
    else out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f))
  }
  return new Uint8Array(out)
}

function basicAuth(user, pass) {
  return 'Basic ' + base64Encode(utf8Bytes(`${user}:${pass}`))
}

module.exports = { storage, fsStore, nowISO, uuid4, fmtSize, hasWx, base64Encode, basicAuth }
