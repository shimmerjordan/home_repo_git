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

  async function start() {
    if (active.value) return
    error.value = ''
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (e) {
      error.value = String(e.message || e)
      return
    }
    ctx = new (window.AudioContext || window.webkitAudioContext)()
    if (ctx.state === 'suspended') { try { await ctx.resume() } catch {} }
    const src = ctx.createMediaStreamSource(stream)
    analyser = ctx.createAnalyser()
    analyser.fftSize = 512
    analyser.smoothingTimeConstant = 0.7
    src.connect(analyser)
    const buf = new Uint8Array(analyser.frequencyBinCount)
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
      rms.value = total / bars
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
  }

  function stop() {
    active.value = false
    if (raf) cancelAnimationFrame(raf)
    raf = 0
    if (analyser) { try { analyser.disconnect() } catch {}; analyser = null }
    if (ctx) { try { ctx.close() } catch {}; ctx = null }
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null }
    levels.value = new Array(bars).fill(0)
    rms.value = 0
  }

  onBeforeUnmount(stop)

  return { active, error, levels, rms, start, stop }
}
