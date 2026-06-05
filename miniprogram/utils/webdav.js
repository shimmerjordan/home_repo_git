// WebDAV 客户端 (基于 wx.request)。
//
// ⚠️ 重要约束: wx.request 只允许标准 HTTP 方法 (GET/POST/PUT/DELETE/...),
// **不支持 PROPFIND / MKCOL**。因此:
//   - 上传 = PUT, 下载 = GET, 删除 = DELETE (均可用)
//   - 「列目录」无法用 PROPFIND -> 改为在远程维护一个 index.json (见 backup.js)
//   - 「建目录」无法用 MKCOL -> 需用户在网盘里预先建好目标文件夹 (README 说明)
//
// 这是小程序端 WebDAV 的固有限制, 已在调研报告中列为风险点。

const { basicAuth } = require('./util.js')

function _join(base, path) {
  const b = (base || '').replace(/\/+$/, '')
  const p = (path || '').replace(/^\/+/, '')
  return `${b}/${p}`
}

// cfg: { url, username, password, remote_dir }
function _url(cfg, name) {
  const dir = (cfg.remote_dir || '').replace(/^\/+|\/+$/g, '')
  return _join(cfg.url, dir ? `${dir}/${name}` : name)
}

function _request(cfg, { method, name, data, responseType }) {
  return new Promise((resolve, reject) => {
    if (!cfg.url) return reject(new Error('未配置 WebDAV 地址'))
    wx.request({
      url: _url(cfg, name),
      method,
      data,
      responseType: responseType || 'text',
      header: {
        Authorization: basicAuth(cfg.username, cfg.password),
        'Content-Type': 'application/octet-stream',
      },
      success: (res) => {
        if (res.statusCode >= 200 && res.statusCode < 300) resolve(res)
        else if (res.statusCode === 404) resolve(null) // 不存在, 调用方自行处理
        else reject(new Error(`WebDAV ${method} ${name} 失败: HTTP ${res.statusCode}`))
      },
      fail: (err) => reject(new Error(`WebDAV 请求失败: ${err.errMsg || err}`)),
    })
  })
}

// 上传二进制 (ArrayBuffer)
function put(cfg, name, arrayBuffer) {
  return _request(cfg, { method: 'PUT', name, data: arrayBuffer })
}

// 下载为 ArrayBuffer (null 表示 404)
async function getBinary(cfg, name) {
  const res = await _request(cfg, { method: 'GET', name, responseType: 'arraybuffer' })
  return res ? res.data : null
}

// 下载为文本 (null 表示 404)。注意 wx.request 默认 dataType:'json' 会把 JSON 响应
// 自动解析成对象, 这里统一还原为字符串 (对象则 JSON.stringify)。
async function getText(cfg, name) {
  const res = await _request(cfg, { method: 'GET', name, responseType: 'text' })
  if (!res) return null
  const d = res.data
  if (typeof d === 'string') return d
  if (d == null) return ''
  try { return JSON.stringify(d) } catch (e) { return String(d) }
}

function del(cfg, name) {
  return _request(cfg, { method: 'DELETE', name })
}

module.exports = { put, getBinary, getText, del, _url }
