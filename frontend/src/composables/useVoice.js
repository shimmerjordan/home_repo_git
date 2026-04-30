// Voice primitives. The state machine lives in VoicePanel; this file just exposes:
//   - startWakeListening(onWake)   continuous SR that only fires when a wake word is heard
//   - stopWakeListening()
//   - captureUtterance({timeout})  one-shot SR (or MediaRecorder→Whisper) returning text
//   - listenYesNo({timeout})       one-shot SR with yes/no/unknown classification
//   - speak(text)                  TTS that returns a Promise resolving on end
//   - cancelSpeak()
//
// Wake words are passed as a Vue ref so settings changes take effect without reload.

import { ref, onBeforeUnmount, unref } from 'vue'

const SR =
  typeof window !== 'undefined' &&
  (window.SpeechRecognition || window.webkitSpeechRecognition)

const YES_WORDS = ['确定', '确认', '对', '是的', '是', '好的', '好', '嗯', '行', '可以', 'yes', 'ok', 'okay']
const NO_WORDS  = ['取消', '不对', '不是', '算了', '不要', '不行', '别', 'no', 'cancel']

export function classifyYesNo(text) {
  if (!text) return 'unknown'
  const t = text.toLowerCase().replace(/[，。！？,.\s]/g, '')
  for (const w of YES_WORDS) if (t.includes(w.toLowerCase())) return 'yes'
  for (const w of NO_WORDS)  if (t.includes(w.toLowerCase())) return 'no'
  return 'unknown'
}

// Classify into yes / no / wake / unknown — used during confirm dialogs so the user
// can also start a new command by uttering the wake word again.
export function classifyAnswer(text, wakeWords = []) {
  if (!text) return 'unknown'
  const t = text.toLowerCase().replace(/[，。！？,.\s]/g, '')
  for (const w of (wakeWords || [])) {
    if (w && t.includes(w.toLowerCase())) return 'wake'
  }
  for (const w of YES_WORDS) if (t.includes(w.toLowerCase())) return 'yes'
  for (const w of NO_WORDS)  if (t.includes(w.toLowerCase())) return 'no'
  return 'unknown'
}

export function useVoice({ wakeWordsRef, useWhisperRef, lang = 'zh-CN' } = {}) {
  const supported = !!SR
  const wakeListening = ref(false)
  const wakeHeard = ref('')
  const error = ref('')

  let wakeRec = null
  let wakeRestartTimer = null
  let stopRequested = false
  let wakeCallback = null

  function getWakeWords() {
    const v = unref(wakeWordsRef)
    if (Array.isArray(v)) return v.filter(Boolean)
    return []
  }

  function detectWake(text) {
    const wakes = getWakeWords()
    if (!wakes.length) return null
    const lowered = text.toLowerCase()
    for (const w of wakes) {
      const idx = lowered.indexOf(w.toLowerCase())
      if (idx !== -1) {
        return { tail: text.slice(idx + w.length).trim(), wake: w }
      }
    }
    return null
  }

  function startWakeListening(onWake) {
    error.value = ''
    if (!supported) {
      error.value = '当前浏览器不支持 Web Speech API'
      return false
    }
    if (wakeListening.value) return true
    wakeCallback = onWake
    stopRequested = false
    _spawnWakeRec()
    wakeListening.value = true
    return true
  }

  function _spawnWakeRec() {
    if (stopRequested) return
    const r = new SR()
    r.lang = lang
    r.continuous = true
    r.interimResults = true
    let buf = ''
    r.onresult = (ev) => {
      // Only check final pieces — drop interim to keep memory tiny.
      let chunk = ''
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        if (ev.results[i].isFinal) chunk += ev.results[i][0].transcript
      }
      if (!chunk) return
      buf = (buf + chunk).slice(-80)
      wakeHeard.value = buf
      const m = detectWake(buf)
      if (m) {
        const tail = m.tail
        buf = ''
        wakeHeard.value = ''
        // Stop the wake recognizer; let the caller take over with a single-shot.
        stopRequested = true
        try { r.stop() } catch {}
        wakeRec = null
        wakeListening.value = false
        if (wakeCallback) wakeCallback({ wake: m.wake, tail })
      }
    }
    r.onerror = (ev) => {
      // Transient errors -> restart. 'not-allowed' / 'service-not-allowed' surface to user.
      if (['not-allowed', 'service-not-allowed'].includes(ev.error)) {
        error.value = '麦克风未授权或被阻止 (' + ev.error + ')'
        stopWakeListening()
      }
    }
    r.onend = () => {
      if (stopRequested) return
      // Restart after a short delay to keep listening continuously.
      if (wakeRestartTimer) return
      wakeRestartTimer = setTimeout(() => {
        wakeRestartTimer = null
        if (!stopRequested) _spawnWakeRec()
      }, 300)
    }
    try {
      r.start()
      wakeRec = r
    } catch (e) {
      error.value = String(e.message || e)
    }
  }

  function stopWakeListening() {
    stopRequested = true
    wakeListening.value = false
    wakeCallback = null
    if (wakeRestartTimer) { clearTimeout(wakeRestartTimer); wakeRestartTimer = null }
    if (wakeRec) { try { wakeRec.stop() } catch {}; wakeRec = null }
    wakeHeard.value = ''
  }

  // ---- Single-shot capture ----

  function captureUtterance({ timeout = 8000, lang: overrideLang } = {}) {
    if (unref(useWhisperRef)) return _captureWithMediaRecorder({ timeout })
    return _captureWithSR({ timeout, lang: overrideLang || lang })
  }

  function _captureWithSR({ timeout, lang }) {
    return new Promise((resolve) => {
      if (!SR) { resolve({ text: '', error: 'SR 不支持' }); return }
      const r = new SR()
      r.lang = lang
      r.continuous = false
      r.interimResults = false
      r.maxAlternatives = 1
      let done = false
      const finish = (text, err) => {
        if (done) return
        done = true
        try { r.stop() } catch {}
        resolve({ text: text || '', error: err || '' })
      }
      r.onresult = (ev) => finish(ev.results[0]?.[0]?.transcript || '')
      r.onerror = (ev) => finish('', ev.error)
      r.onend = () => { if (!done) finish('') }
      try { r.start() } catch (e) { finish('', String(e.message || e)) }
      setTimeout(() => finish(''), timeout)
    })
  }

  async function _captureWithMediaRecorder({ timeout }) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const mr = new MediaRecorder(stream)
      const chunks = []
      mr.ondataavailable = (e) => chunks.push(e.data)
      const stopped = new Promise((resolve) => { mr.onstop = resolve })
      mr.start()
      setTimeout(() => mr.state === 'recording' && mr.stop(), timeout)
      await stopped
      stream.getTracks().forEach((t) => t.stop())
      return { blob: new Blob(chunks, { type: mr.mimeType || 'audio/webm' }), text: '' }
    } catch (e) {
      return { text: '', error: String(e.message || e) }
    }
  }

  async function listenYesNo({ timeout = 6000 } = {}) {
    const { text, error: err } = await captureUtterance({ timeout })
    return { result: classifyYesNo(text), text, error: err }
  }

  async function listenAnswer({ timeout = 6000 } = {}) {
    const { text, error: err } = await captureUtterance({ timeout })
    return { result: classifyAnswer(text, getWakeWords()), text, error: err }
  }

  // ---- TTS ----

  // Mutable TTS settings; setTtsConfig() is called from the panel whenever settings change.
  const ttsConfig = { voice: '', lang, rate: 1.05, pitch: 1.0 }

  function setTtsConfig(cfg = {}) {
    if (cfg.voice !== undefined) ttsConfig.voice = cfg.voice
    if (cfg.lang) ttsConfig.lang = cfg.lang
    if (cfg.rate !== undefined && Number.isFinite(+cfg.rate)) ttsConfig.rate = +cfg.rate
    if (cfg.pitch !== undefined && Number.isFinite(+cfg.pitch)) ttsConfig.pitch = +cfg.pitch
  }

  function _findVoice(name) {
    if (!name || !window.speechSynthesis) return null
    const voices = window.speechSynthesis.getVoices() || []
    return voices.find((v) => v.name === name) || null
  }

  function speak(text, overrides = {}) {
    if (!text || typeof window === 'undefined' || !window.speechSynthesis) {
      return Promise.resolve()
    }
    return new Promise((resolve) => {
      try {
        window.speechSynthesis.cancel()
        const u = new SpeechSynthesisUtterance(text)
        u.lang = overrides.lang || ttsConfig.lang || lang
        u.rate = overrides.rate ?? ttsConfig.rate ?? 1.05
        u.pitch = overrides.pitch ?? ttsConfig.pitch ?? 1.0
        const voiceName = overrides.voice ?? ttsConfig.voice
        const v = _findVoice(voiceName)
        if (v) u.voice = v
        u.onend = () => resolve()
        u.onerror = () => resolve()
        window.speechSynthesis.speak(u)
        // Safety timeout in case the engine never fires onend.
        setTimeout(resolve, Math.max(2500, text.length * 240))
      } catch {
        resolve()
      }
    })
  }

  function cancelSpeak() {
    try { window.speechSynthesis?.cancel() } catch {}
  }

  onBeforeUnmount(() => { stopWakeListening(); cancelSpeak() })

  return {
    supported,
    wakeListening,
    wakeHeard,
    error,
    startWakeListening,
    stopWakeListening,
    captureUtterance,
    listenYesNo,
    listenAnswer,
    classifyYesNo,
    classifyAnswer,
    speak,
    setTtsConfig,
    cancelSpeak,
  }
}
