// Opens a microphone stream and exposes per-frame frequency bars + RMS level.
// Used purely for visualization (the SpeechRecognition API doesn't expose audio).
import { ref, onBeforeUnmount } from 'vue'

export function useAudioMeter({ bars = 32 } = {}) {
  const active = ref(false)
  const error = ref('')
  const levels = ref(new Array(bars).fill(0))   // 0..1 frequency bins
  const rms = ref(0)                            // 0..1 overall loudness

  let stream = null
  let ctx = null
  let analyser = null
  let raf = 0
  let resumeListener = null

  async function start() {
    if (active.value) return
    error.value = ''
    const Ctor = window.AudioContext || window.webkitAudioContext
    if (!Ctor) { error.value = '浏览器不支持 AudioContext'; return }
    // iOS Safari requires AudioContext.resume() to be called SYNCHRONOUSLY inside a
    // user-gesture handler. Awaiting getUserMedia first kicks us out of the gesture
    // frame, so we must resume() right after construction — before any await.
    ctx = new Ctor()
    try { ctx.resume() } catch {}
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
      })
    } catch (e) {
      // Fall back to plain `audio: true` — older iOS rejects detailed constraints.
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      } catch (e2) {
        error.value = String(e2.message || e2)
        try { ctx.close() } catch {}
        ctx = null
        return
      }
    }
    // Defensive second resume — some iOS versions need both pre- and post-await.
    if (ctx.state === 'suspended') { try { await ctx.resume() } catch {} }

    // iOS occasionally suspends the AudioContext when SpeechRecognition or
    // SpeechSynthesis grab the audio session. Hook a global touch listener that
    // resumes the context the next time the user touches the page — that's a
    // legitimate gesture frame and brings the meter back to life.
    resumeListener = () => {
      if (ctx && ctx.state === 'suspended') { try { ctx.resume() } catch {} }
    }
    window.addEventListener('touchstart', resumeListener, { passive: true })
    window.addEventListener('click', resumeListener, { passive: true })
    document.addEventListener('visibilitychange', resumeListener)

    const src = ctx.createMediaStreamSource(stream)
    analyser = ctx.createAnalyser()
    analyser.fftSize = 512
    analyser.smoothingTimeConstant = 0.7
    src.connect(analyser)
    const buf = new Uint8Array(analyser.frequencyBinCount)
    const timeBuf = new Uint8Array(analyser.fftSize)
    active.value = true

    const tick = () => {
      if (!active.value || !analyser) return
      analyser.getByteFrequencyData(buf)
      const out = new Array(bars).fill(0)
      const step = Math.floor(buf.length / bars)
      let total = 0
      for (let i = 0; i < bars; i++) {
        let sum = 0
        for (let j = 0; j < step; j++) sum += buf[i * step + j]
        const v = sum / (step * 255)
        out[i] = v
        total += v
      }
      levels.value = out
      // Compute RMS from the time-domain waveform instead of the frequency bins —
      // iPad's frequency analyser is heavily smoothed/auto-gained and reads near 0
      // for normal speech, while the time-domain bytes track real loudness reliably.
      analyser.getByteTimeDomainData(timeBuf)
      let acc = 0
      for (let i = 0; i < timeBuf.length; i++) {
        const c = (timeBuf[i] - 128) / 128
        acc += c * c
      }
      const rmsRaw = Math.sqrt(acc / timeBuf.length)
      // Map to 0..1 with a soft floor so quiet rooms don't show full bar.
      rms.value = Math.min(1, rmsRaw * 3)
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
  }

  function stop() {
    active.value = false
    if (raf) cancelAnimationFrame(raf)
    raf = 0
    if (resumeListener) {
      window.removeEventListener('touchstart', resumeListener)
      window.removeEventListener('click', resumeListener)
      document.removeEventListener('visibilitychange', resumeListener)
      resumeListener = null
    }
    if (analyser) { try { analyser.disconnect() } catch {}; analyser = null }
    if (ctx) { try { ctx.close() } catch {}; ctx = null }
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null }
    levels.value = new Array(bars).fill(0)
    rms.value = 0
  }

  onBeforeUnmount(stop)

  return { active, error, levels, rms, start, stop }
}
