const store = require('../../utils/store.js')
const intent = require('../../utils/intent.js')
const asr = require('../../utils/asr.js')

Page({
  data: {
    text: '',
    recording: false,
    voiceReady: true,
    log: [], // {role:'user'|'bot', text, candidates?, pending?}
    busy: false,
  },
  onLoad() {
    this.rm = wx.getRecorderManager()
    this.rm.onStop((res) => {
      this.setData({ recording: false })
      if (res && res.tempFilePath) this._transcribe(res.tempFilePath)
    })
    this.rm.onError(() => {
      this.setData({ recording: false })
      wx.showToast({ title: '录音出错', icon: 'none' })
    })
  },
  onInput(e) { this.setData({ text: e.detail.value }) },

  startVoice() {
    this.setData({ recording: true, text: '录音中…' })
    this.rm.start({ duration: 60000, format: 'mp3', sampleRate: 16000, numberOfChannels: 1 })
  },
  stopVoice() {
    if (this.data.recording) this.rm.stop()
  },

  async _transcribe(filePath) {
    const s = store.getSettings()
    if (!s.llm || !s.llm.base_url) {
      wx.showToast({ title: '请先在设置配置 LLM', icon: 'none' })
      this.setData({ text: '' })
      return
    }
    this.setData({ text: '识别中…', busy: true })
    let text
    try {
      text = await asr.transcribe(s.llm, filePath)
    } catch (e) {
      this.setData({ busy: false, text: '' })
      wx.showToast({ title: '语音识别: ' + (e.message || ''), icon: 'none', duration: 3000 })
      return
    }
    if (text) {
      this.setData({ text })
      this.run(text)
    } else {
      this.setData({ busy: false, text: '' })
    }
  },

  send() {
    const t = (this.data.text || '').trim()
    if (t) this.run(t)
  },

  async run(text) {
    const s = store.getSettings()
    if (!s.llm || !s.llm.base_url) {
      this._push('bot', '请先在「设置」配置 LLM (base_url / api_key / model)')
      return
    }
    this._push('user', text)
    this.setData({ busy: true, text: '' })
    try {
      const { parsed } = await intent.parseIntent(text, s.llm)
      const threshold = (s.voice && s.voice.confidence_threshold) || 0.5
      const r = intent.executeIntent(text, parsed, threshold)
      this._push('bot', r.speech, r)
    } catch (e) {
      this._push('bot', '出错了: ' + (e.message || e))
    } finally {
      this.setData({ busy: false })
    }
  },

  confirm(e) {
    const idx = e.currentTarget.dataset.idx
    const entry = this.data.log[idx]
    if (!entry || !entry.pending) return
    const r = intent.confirmAction(entry.pending)
    const log = this.data.log.slice()
    log[idx] = Object.assign({}, entry, { pending: null })
    this.setData({ log })
    this._push('bot', r.speech, r)
  },
  cancel(e) {
    const idx = e.currentTarget.dataset.idx
    const log = this.data.log.slice()
    log[idx] = Object.assign({}, log[idx], { pending: null })
    this.setData({ log })
    this._push('bot', '已取消')
  },

  goItem(e) {
    wx.navigateTo({ url: `/pages/item-edit/item-edit?id=${e.currentTarget.dataset.id}` })
  },
  goSettings() { wx.navigateTo({ url: '/pages/settings/settings' }) },

  _push(role, text, extra) {
    const entry = Object.assign({ role, text }, extra ? {
      candidates: extra.candidates || [],
      pending: extra.needs_confirmation ? extra.pending : null,
    } : {})
    this.setData({ log: this.data.log.concat(entry) })
  },
})
