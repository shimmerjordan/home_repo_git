// OpenAI 兼容 LLM 客户端 (wx.request 直连)。与 NAS 版 backend/app/llm/client.py 同协议。
// ⚠️ api_key 会留在客户端 —— 定位为个人自用工具。发布需把 base_url 域名加入小程序合法域名。

function chat(cfg, messages) {
  return new Promise((resolve, reject) => {
    if (!cfg || !cfg.base_url) return reject(new Error('未配置 LLM base_url'))
    const url = cfg.base_url.replace(/\/+$/, '') + '/chat/completions'
    const header = { 'Content-Type': 'application/json' }
    if (cfg.api_key) header.Authorization = 'Bearer ' + cfg.api_key
    wx.request({
      url,
      method: 'POST',
      header,
      data: {
        model: cfg.model || 'gpt-4o-mini',
        messages,
        temperature: cfg.temperature != null ? cfg.temperature : 0.2,
        max_tokens: cfg.max_tokens || 512,
      },
      success: (res) => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          const c = res.data && res.data.choices && res.data.choices[0] && res.data.choices[0].message
          resolve((c && c.content) || '')
        } else {
          reject(new Error(`LLM HTTP ${res.statusCode}: ${JSON.stringify(res.data).slice(0, 200)}`))
        }
      },
      fail: (e) => reject(new Error('LLM 请求失败: ' + (e.errMsg || e))),
    })
  })
}

// 从模型回复里宽松抽取 JSON (容忍 ```json 围栏 / 前后多余文字)。
function parseJSON(text) {
  if (!text) throw new Error('LLM 返回为空')
  let s = String(text).trim()
  s = s.replace(/^```(?:json)?/i, '').replace(/```$/, '').trim()
  const start = s.indexOf('{')
  const end = s.lastIndexOf('}')
  if (start >= 0 && end > start) s = s.slice(start, end + 1)
  return JSON.parse(s)
}

module.exports = { chat, parseJSON }
