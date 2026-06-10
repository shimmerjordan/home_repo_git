// OpenAI 兼容的语音转文字接口 (Whisper)。用 wx.uploadFile 上传录音文件。
function transcribe(cfg, filePath) {
  return new Promise((resolve, reject) => {
    if (!cfg || !cfg.base_url) return reject(new Error('未配置 base_url'))
    const url = cfg.base_url.replace(/\/+$/, '') + '/audio/transcriptions'
    const header = {}
    if (cfg.api_key) header.Authorization = 'Bearer ' + cfg.api_key
    wx.uploadFile({
      url,
      filePath,
      name: 'file',
      header,
      formData: {
        model: cfg.asr_model || 'whisper-1',
        response_format: 'json',
        language: 'zh',
      },
      success: (res) => {
        try {
          const data = typeof res.data === 'string' ? JSON.parse(res.data) : res.data
          if (res.statusCode >= 200 && res.statusCode < 300 && data && data.text) {
            resolve(data.text.trim())
          } else {
            reject(new Error('ASR 返回: ' + JSON.stringify(data).slice(0, 120)))
          }
        } catch (e) {
          reject(new Error('ASR 响应解析失败'))
        }
      },
      fail: (e) => reject(new Error('录音上传失败: ' + (e.errMsg || e))),
    })
  })
}

module.exports = { transcribe }
