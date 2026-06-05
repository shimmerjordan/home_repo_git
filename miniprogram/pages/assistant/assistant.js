const store = require('../../utils/store.js')
const intent = require('../../utils/intent.js')

// 微信同声传译插件 (语音识别 + 合成)。插件需在 mp 后台「插件」里添加后才可用。
let SI = null
try { SI = requirePlugin('WechatSI') } catch (e) { SI = null }

Page({
  data: {
    text: '',
    recording: false,
    voiceReady: false,
    log: [], // {role:'user'|'bot', text, candidates?, pending?}
    busy: false,
  },
  onLoad() {
    this.setData({ voiceReady: !!SI })
    if (SI) {
      this.rm = SI.getRecordRecognitionManager()
      this.rm.onRecognize = (res) => { if (res.result) this.setData({ text: res.result }) }
      this.rm.onStop = (res) => {
        this.setData({ recording: false })
        const t = (res && res.result) || this.data.text
        if (t) { this.setData({ text: t }); this.run(t) }
      }
      this.rm.onError = () => { this.setData({ recording: false }) }
    }
  },
  onInput(e) { this.setData({ text: e.detail.value }) },

  startVoice() {
    if (!SI || !this.rm) { wx.showToast({ title: '未启用同声传译插件', icon: 'none' }); return }
    this.setData({ recording: true, text: '' })
    this.rm.start({ lang: 'zh_CN' })
  },
  stopVoice() {
    if (this.rm) this.rm.stop()
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
      this._speak(r.speech)
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
    // 标记原条目已处理
    const log = this.data.log.slice()
    log[idx] = Object.assign({}, entry, { pending: null })
    this.setData({ log })
    this._push('bot', r.speech, r)
    this._speak(r.speech)
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
  _speak(text) {
    if (!SI || !text) return
    try {
      SI.textToSpeech({
        lang: 'zh_CN', tts: true, content: text,
        success: (res) => {
          // 复用单个 InnerAudioContext, 避免重复创建泄漏 (微信对并发上下文有上限)。
          if (!this._audio) {
            this._audio = wx.createInnerAudioContext()
            this._audio.onError(() => {})
          }
          this._audio.src = res.filename
          this._audio.play()
        },
      })
    } catch (e) { /* TTS 失败不影响功能 */ }
  },
  onUnload() {
    if (this._audio) { try { this._audio.destroy() } catch (e) {} this._audio = null }
  },
})
