const store = require('../../utils/store.js')
const llm = require('../../utils/llm.js')

const PRESETS = [
  { label: '硅基流动', base_url: 'https://api.siliconflow.cn/v1', model: 'Qwen/Qwen2.5-7B-Instruct', asr_model: 'FunAudioLLM/SenseVoiceSmall' },
  { label: 'DeepSeek', base_url: 'https://api.deepseek.com/v1', model: 'deepseek-chat', asr_model: '' },
  { label: '智谱 GLM', base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash', asr_model: '' },
  { label: 'OpenAI', base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini', asr_model: 'whisper-1' },
]

Page({
  data: {
    llm: { base_url: '', api_key: '', model: '', asr_model: '', temperature: 0.2, max_tokens: 512 },
    voice: { confidence_threshold: 0.5 },
    webdavPass: '',
    presets: PRESETS,
    testMsg: '',
    savedMsg: '',
  },
  onLoad() {
    const s = store.getSettings()
    this.setData({
      llm: Object.assign(this.data.llm, s.llm || {}),
      voice: Object.assign(this.data.voice, s.voice || {}),
      webdavPass: (s.webdav && s.webdav.passphrase) || '',
    })
  },
  onLlm(e) { this.setData({ [`llm.${e.currentTarget.dataset.field}`]: e.detail.value }) },
  onThreshold(e) { this.setData({ 'voice.confidence_threshold': Number(e.detail.value) }) },
  onWebdavPass(e) { this.setData({ webdavPass: e.detail.value }) },
  applyPreset(e) {
    const p = PRESETS[e.currentTarget.dataset.i]
    this.setData({ 'llm.base_url': p.base_url, 'llm.model': p.model, 'llm.asr_model': p.asr_model || '' })
  },
  save() {
    const llmCfg = {
      base_url: this.data.llm.base_url,
      api_key: this.data.llm.api_key,
      model: this.data.llm.model,
      asr_model: this.data.llm.asr_model || '',
      temperature: Number(this.data.llm.temperature) || 0.2,
      max_tokens: Number(this.data.llm.max_tokens) || 512,
    }
    store.setSettings({ llm: llmCfg, voice: { confidence_threshold: Number(this.data.voice.confidence_threshold) || 0.5 } })
    // 把备份口令合并进 webdav 设置 (备份页读取)
    const w = store.getSettings().webdav || {}
    store.setSettings({ webdav: Object.assign(w, { passphrase: this.data.webdavPass }) })
    this.setData({ savedMsg: '✅ 已保存' })
  },
  async test() {
    this.setData({ testMsg: '测试中…' })
    try {
      const content = await llm.chat(this.data.llm, [
        { role: 'system', content: '只回复一个词: pong' },
        { role: 'user', content: 'ping' },
      ])
      this.setData({ testMsg: '✅ 连通: ' + String(content).slice(0, 60) })
    } catch (e) {
      this.setData({ testMsg: '❌ ' + (e.message || e) })
    }
  },
})
