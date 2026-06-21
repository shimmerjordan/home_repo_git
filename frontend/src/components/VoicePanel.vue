<script setup>
import { ref, computed, watch, toRef, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { api } from '../api'
import { useVoice } from '../composables/useVoice'
import { useAudioMeter } from '../composables/useAudioMeter'
import Waveform from './Waveform.vue'
import Scene3D from './Scene3D.vue'
import { isLowEndDevice } from '../composables/sceneLayout'

// Persisted 3D quality preference shared with BuildingPanel via localStorage.
const LQ_KEY = 'storage.scene3d.lowQuality'
function loadLQ() {
  try {
    const raw = localStorage.getItem(LQ_KEY)
    if (raw === '0' || raw === '1') return raw === '1'
  } catch {}
  return isLowEndDevice()
}
const lowQuality = ref(loadLQ())
watch(() => lowQuality.value, (v) => {
  try { localStorage.setItem(LQ_KEY, v ? '1' : '0') } catch {}
})

// Shared with BuildingPanel via localStorage: list of rooms whose item cubes are
// shown by default. Read once on mount; users toggle from BuildingPanel.
const SHOWN_ITEMS_KEY = 'storage.scene3d.shownItemRooms'
function loadShownRooms() {
  try {
    const raw = localStorage.getItem(SHOWN_ITEMS_KEY)
    if (raw) {
      const arr = JSON.parse(raw)
      if (Array.isArray(arr)) return arr.map(Number).filter(Number.isFinite)
    }
  } catch {}
  return []
}
const shownItemRoomIds = ref(loadShownRooms())

// Same persistence channel as BuildingPanel — keep both viewers focused on the same
// home so what you see on the home tab matches what the 3D editor tab is showing.
const HOME_KEY = 'storage.activeHomeId'
function loadActiveHome() {
  try {
    const raw = localStorage.getItem(HOME_KEY)
    if (raw && raw !== 'null') return +raw || null
  } catch {}
  return null
}
const activeHomeId = ref(loadActiveHome())

// Pick up changes other tabs make (BuildingPanel writes shownItemRoomIds + active home).
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (ev) => {
    if (ev.key === SHOWN_ITEMS_KEY) shownItemRoomIds.value = loadShownRooms()
    if (ev.key === HOME_KEY) activeHomeId.value = loadActiveHome()
  })
}

const props = defineProps({ settings: Object, refreshKey: Number })
const emit = defineEmits(['changed'])

// Reactive refs into settings so changes propagate live.
const wakeWordsRef = computed(() => props.settings?.voice?.wake_words || [])
const useWhisperRef = computed(() => !!props.settings?.voice?.whisper_enabled)
const confirmBeforeLLM = computed(() => props.settings?.voice?.confirm_before_llm !== false)
const confidenceThreshold = computed(() => props.settings?.voice?.confidence_threshold ?? 0.5)

const voice = useVoice({ wakeWordsRef, useWhisperRef })
const meter = useAudioMeter({ bars: 36 })

watch(() => props.settings?.voice, (v) => {
  if (!v) return
  voice.setTtsConfig({
    voice: v.tts_voice || '',
    lang: v.tts_lang || 'zh-CN',
    rate: v.tts_rate ?? 1.05,
    pitch: v.tts_pitch ?? 1.0,
    enabled: v.tts_enabled !== false,
  })
}, { immediate: true, deep: true })

// ---- State machine ----
//   idle | command | confirm-text | processing | confirm-action | speaking
const phase = ref('idle')
const wakeOn = ref(false)         // user-toggled "持续监听唤醒词"
const transcript = ref('')        // recognized command text awaiting confirmation
const result = ref(null)          // last LLM IntentResult
const errorMsg = ref('')
const heardAnswer = ref('')       // what was heard during yes/no
const history = ref([])
const recentTx = ref([])
const isHttps = computed(() => typeof window !== 'undefined'
  && (window.location.protocol === 'https:' || window.location.hostname === 'localhost'))

// Pending promise resolver for confirm dialogs (yes/no by voice OR button).
const pendingAnswer = ref(null) // function(boolean)
const confirmPrompt = ref('')
const confirmDetail = ref('')

async function loadRecent() { try { recentTx.value = await api.recentTx(15) } catch {} }

// 3D scene data + highlight target — driven by search/voice result.
const sceneLocations = ref([])
const sceneItems = ref([])
const sceneHighlightItem = ref(null)
const sceneHighlightIds = ref([])    // multiple targets when same-name items exist in different places
const sceneHighlightLoc = ref(null)
const sceneSection = ref(null)       // DOM ref for auto-scroll
// "闭麦" — global mute. Blocks wake listening and push-to-talk until toggled off.
// Useful when AI is speaking nearby and you don't want stray "确定/取消" pickups.
const micMuted = ref(false)
// Increments on every result; used to trigger scroll-into-view.
const resultSeq = ref(0)

async function loadScene() {
  // Storage events don't fire in the same tab — re-read the active home from
  // localStorage on every refresh so changes made in BuildingPanel show up here.
  activeHomeId.value = loadActiveHome()
  try {
    const [locs, its] = await Promise.all([api.listLocations(), api.listItems({ limit: 1000 })])
    sceneLocations.value = locs
    sceneItems.value = its
  } catch {}
}

// "借出未归位" items — populated from /api/transactions/pending-returns.
const pendingReturns = ref([])
async function loadPending() {
  try { pendingReturns.value = await api.pendingReturns() } catch {}
}

// "待补充" items — quantity==0 records that are HIDDEN from search/voice. The
// user either re-stocks them (and quantity goes up via put_in) or decides
// they're permanently gone and deletes the row.
const depletedItems = ref([])
async function loadDepleted() {
  try { depletedItems.value = await api.depletedItems() } catch {}
}

async function deleteDepleted(it) {
  if (!confirm(`确认从数据库永久删除「${it.name}」?这条物品记录会消失,审计日志会保留。`)) return
  try {
    await api.deleteItem(it.id)
    await loadDepleted()
    emit('changed')
  } catch (e) { console.warn('delete depleted failed', e) }
}

async function restockDepleted(it) {
  const qtyStr = prompt(`补充「${it.name}」多少件?`, '1')
  const qty = parseInt(qtyStr || '0', 10)
  if (!qty || qty <= 0) return
  try {
    await api.recordTx(it.id, {
      item_id: it.id,
      action: 'put_in',
      quantity: qty,
      location_id: it.location_id || null,
      note: '手动补充',
    })
    await loadDepleted()
    emit('changed')
  } catch (e) { console.warn('restock failed', e) }
}

// "已归位" — record a put_in transaction that nets against the open take_out,
// then refresh the pending list.
async function markReturned(p) {
  try {
    await api.recordTx(p.item_id, {
      item_id: p.item_id,
      action: 'put_in',
      quantity: p.pending_quantity,
      location_id: p.return_location_id || null,
      note: '手动标记已归位',
    })
    await loadPending()
    emit('changed')
  } catch (e) { console.warn('mark returned failed', e) }
}

// "用完了" — record consume against the pending take_out so it doesn't
// linger on the reminder list. Inventory was already decremented when taken
// out, so we just clear the obligation without changing quantity.
async function markConsumed(p) {
  try {
    await api.recordTx(p.item_id, {
      item_id: p.item_id,
      action: 'consume',
      quantity: p.pending_quantity,
      note: '手动标记用完',
    })
    await loadPending()
    emit('changed')
  } catch (e) { console.warn('mark consumed failed', e) }
}

function timeAgo(iso) {
  if (!iso) return ''
  const ms = Date.now() - new Date(iso).getTime()
  const min = Math.floor(ms / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr} 小时前`
  const d = Math.floor(hr / 24)
  return `${d} 天前`
}

let _pollTimer = null
onMounted(() => {
  loadRecent(); loadScene(); loadPending(); loadDepleted()
  // Poll every 30 s so Feishu/bot-created transactions appear without a manual refresh.
  _pollTimer = setInterval(() => { loadRecent(); loadPending() }, 30_000)
})
onBeforeUnmount(() => { if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null } })
watch(() => props.refreshKey, () => { loadRecent(); loadScene(); loadPending(); loadDepleted() })

// When the user explicitly picks a candidate via "选这个", we must NOT let the
// auto-multi-highlight logic re-light all same-name items — the watcher would
// otherwise fight the explicit single highlight. Pickers set this flag once.
let suppressNextAutoHighlight = false

function scrollToScene() {
  // Bring the 3D preview into view after a successful search/intent. Uses smooth scroll
  // so the user sees the highlight appear contextually.
  nextTick(() => {
    const el = sceneSection.value
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  })
}

watch(() => result.value, (v) => {
  if (!v) return
  resultSeq.value += 1
  // Scroll into view whenever a new result lands AND there's something to look at.
  if ((v.candidates?.length || 0) > 0 || v.executed) scrollToScene()
  if (suppressNextAutoHighlight) {
    suppressNextAutoHighlight = false
    if (v.executed) loadScene()
    return
  }
  // Assist intent: highlight ALL recommended items in 3D so the user can see them at a glance.
  if (v.intent === 'assist' && v.recommendations?.length) {
    sceneHighlightItem.value = null
    sceneHighlightIds.value = []
    setTimeout(() => {
      sceneHighlightIds.value = v.recommendations.map((r) => r.item_id)
    }, 50)
    if (v.executed) loadScene()
    return
  }
  // Default: multi-highlight every candidate sharing the top candidate's name
  // (this is the "X 在多个地方" scenario).
  const cands = v.candidates || []
  if (cands.length) {
    const top = cands[0]
    const sameName = cands.filter((c) => c.item_name === top.item_name)
    sceneHighlightIds.value = []
    sceneHighlightItem.value = null
    setTimeout(() => {
      if (sameName.length >= 2) {
        sceneHighlightIds.value = sameName.map((c) => c.item_id)
      } else {
        sceneHighlightItem.value = top.item_id
      }
    }, 50)
  }
  if (v.executed) loadScene()
})
onBeforeUnmount(() => { stopAll() })

function stopAll() {
  voice.stopWakeListening()
  voice.cancelSpeak()
  meter.stop()
  clearAutoYes()
  if (pendingAnswer.value) { pendingAnswer.value(false); pendingAnswer.value = null }
}

// ---- Voice meter is independent of SR; runs whenever we want a UI signal ----
async function ensureMeter() {
  if (!meter.active.value) await meter.start()
}

// Abort signal — set when user taps mic during an in-flight command/process so we can
// short-circuit the rest of the state machine.
const abortRequested = ref(false)

// ---- Wake mode toggle ----
async function toggleWake() {
  if (wakeOn.value) {
    wakeOn.value = false
    voice.stopWakeListening()
    if (phase.value === 'idle') meter.stop()
  } else {
    if (micMuted.value) return
    wakeOn.value = true
    await ensureMeter()
    armWake()
  }
}

// "闭麦" — hard-off. Stops everything in-flight and prevents any new SR until toggled.
function toggleMute() {
  micMuted.value = !micMuted.value
  if (micMuted.value) {
    if (wakeOn.value) { wakeOn.value = false; voice.stopWakeListening() }
    abortCurrentCapture()
    voice.cancelSpeak()
    meter.stop()
  }
}

// Allow user to abort while in 'command' (stops SR) / 'speaking' (cancels TTS) /
// 'processing' (best-effort; will resolve naturally but TTS won't play).
function abortCurrentCapture() {
  if (phase.value === 'command') {
    // useVoice doesn't expose an external abort, but stopping all SR effectively
    // forces _captureWithSR's r.onend → finish('') path on the next tick.
    voice.stopWakeListening()
    try { voice.cancelSpeak() } catch {}
    // Force phase reset; the in-flight capture promise will resolve to empty and
    // captureCommand's "没听清" branch will run — speak that nothing to the user.
    abortRequested.value = true
  } else if (phase.value === 'speaking') {
    voice.cancelSpeak()
  } else if (phase.value === 'processing') {
    abortRequested.value = true
  }
}

function armWake() {
  // Start (or restart) the wake recognizer when in idle.
  if (!wakeOn.value || phase.value !== 'idle') return
  voice.startWakeListening(async ({ tail }) => {
    if (phase.value !== 'idle') return
    // brief affirmation, then go straight to command capture.
    await voice.speak('请说')
    await captureCommand(tail)
  })
}

watch(phase, (p) => { if (p === 'idle') armWake() })

// ---- Push-to-talk ----
async function pushToTalk() {
  if (micMuted.value) return
  // Re-tap during command/speak/process => abort.
  if (phase.value !== 'idle') {
    abortCurrentCapture()
    return
  }
  voice.stopWakeListening() // make sure no SR is running
  await ensureMeter()
  await captureCommand('')
}

async function captureCommand(prefilledTail) {
  errorMsg.value = ''
  abortRequested.value = false
  phase.value = 'command'
  transcript.value = ''
  if (abortRequested.value || micMuted.value) { phase.value = 'idle'; return }
  // If the wake-detector handed us a tail like "充电宝在哪", skip a second SR roundtrip.
  if (prefilledTail && prefilledTail.length >= 2) {
    transcript.value = prefilledTail
  } else {
    const { text, blob, error: err } = await voice.captureUtterance({ timeout: 8000 })
    if (abortRequested.value) { phase.value = 'idle'; return }
    if (err) {
      errorMsg.value = '识别失败: ' + err
      phase.value = 'idle'
      return
    }
    let said = text
    if (!said && blob) {
      try {
        const r = await api.transcribe(blob)
        said = r.text
      } catch (e) {
        errorMsg.value = '转写失败: ' + (e.message || e)
        phase.value = 'idle'
        return
      }
    }
    if (!said) {
      await voice.speak('没听清,请再说一次')
      phase.value = 'idle'
      return
    }
    transcript.value = said
  }

  // Pre-LLM confirm to save tokens (skippable in settings).
  if (confirmBeforeLLM.value) {
    const ans = await askConfirm({
      prompt: `你说的是,${transcript.value},确认发送吗?`,
      detail: '说"确定"或"取消",叫唤醒词重新开始,或点下方按钮',
    })
    if (ans === 'wake') {
      await voice.speak('好,重新听你说')
      phase.value = 'idle'
      // Immediately start a fresh capture as if push-to-talk.
      pushToTalk()
      return
    }
    if (ans !== 'yes') {
      await voice.speak('已取消')
      phase.value = 'idle'
      return
    }
  }

  await runIntent()
}

async function runIntent() {
  phase.value = 'processing'
  abortRequested.value = false
  try {
    const intent = await api.voiceIntent(transcript.value)
    if (abortRequested.value) { phase.value = 'idle'; return }
    result.value = intent
    history.value.unshift({
      time: new Date(),
      text: transcript.value,
      intent: intent.intent,
      speech: intent.speech,
      executed: intent.executed,
      confidence: intent.confidence,
    })
    history.value = history.value.slice(0, 12)

    // Low confidence: ask the user to confirm the proposed action by voice.
    if (intent.confidence < confidenceThreshold.value && intent.pending_action) {
      const ans = await askConfirm({
        prompt: intent.speech || `不太确定,执行${labelOf(intent.intent)}吗?`,
        detail: `置信度 ${(intent.confidence * 100).toFixed(0)}%,说"确定"或"取消",叫唤醒词重新开始`,
      })
      if (ans === 'wake') {
        await voice.speak('好,重新听你说')
        phase.value = 'idle'
        pushToTalk()
        return
      }
      if (ans !== 'yes') {
        await voice.speak('已取消')
        phase.value = 'idle'
        return
      }
      // Confirmed -> replay with confirmed pending_action.
      const r2 = await api.voiceIntent(transcript.value, {
        confirmed: true,
        pending_action: intent.pending_action,
      })
      result.value = r2
      if (r2.speech) await voice.speak(r2.speech)
      if (r2.executed) emit('changed')
    } else {
      if (intent.speech && !micMuted.value && !abortRequested.value) await voice.speak(intent.speech)
      if (intent.executed) emit('changed')
    }
  } catch (e) {
    errorMsg.value = String(e.message || e)
    await voice.speak('出错了')
  } finally {
    phase.value = 'idle'
    loadRecent()
  }
}

// Generic verbal-or-button confirm.
// Returns: 'yes' | 'no' | 'wake' (user said wake word again -> abandon and start fresh)
// Auto-yes: in the pre-LLM "your sentence sounds right?" confirm, falling silent for
// 30s defaults to YES — this is a confirmation of what we just transcribed, so the
// safe default is to proceed. The low-confidence "execute action?" confirm does NOT
// auto-yes (mutating data on silence would be dangerous).
async function askConfirm({ prompt, detail }) {
  const isTextConfirm = phase.value !== 'processing'
  phase.value = isTextConfirm ? 'confirm-text' : 'confirm-action'
  confirmPrompt.value = prompt
  confirmDetail.value = detail || ''
  heardAnswer.value = ''
  autoYesArmedAt.value = isTextConfirm ? Date.now() : 0
  await voice.speak(prompt)

  return new Promise((resolve) => {
    pendingAnswer.value = resolve
    listenLoop(resolve)
    if (isTextConfirm) armAutoYes(resolve)
  })
}

const AUTO_YES_MS = 30000
const autoYesArmedAt = ref(0)         // 0 = disabled; epoch ms = countdown start
const autoYesCountdown = ref(30)      // displayed in the confirm card
let autoYesTimer = null
let autoYesTick = null
function armAutoYes(myResolver) {
  if (autoYesTimer) clearTimeout(autoYesTimer)
  if (autoYesTick) clearInterval(autoYesTick)
  autoYesCountdown.value = Math.ceil(AUTO_YES_MS / 1000)
  autoYesTick = setInterval(() => {
    const elapsed = Date.now() - autoYesArmedAt.value
    autoYesCountdown.value = Math.max(0, Math.ceil((AUTO_YES_MS - elapsed) / 1000))
  }, 500)
  autoYesTimer = setTimeout(() => {
    if (autoYesTimer) { clearTimeout(autoYesTimer); autoYesTimer = null }
    if (autoYesTick) { clearInterval(autoYesTick); autoYesTick = null }
    autoYesArmedAt.value = 0
    if (pendingAnswer.value === myResolver) {
      pendingAnswer.value = null
      myResolver('yes')
    }
  }, AUTO_YES_MS)
}
function clearAutoYes() {
  if (autoYesTimer) { clearTimeout(autoYesTimer); autoYesTimer = null }
  if (autoYesTick) { clearInterval(autoYesTick); autoYesTick = null }
  autoYesArmedAt.value = 0
}

async function listenLoop(myResolver) {
  // Loop listening for yes / no / wake until a definitive answer or button click.
  while (pendingAnswer.value === myResolver) {
    const { result: r, text } = await voice.listenAnswer({ timeout: 3500 })
    if (pendingAnswer.value !== myResolver) return // already resolved by button
    heardAnswer.value = text
    if (r === 'yes')  { clearAutoYes(); pendingAnswer.value = null; myResolver('yes');  return }
    if (r === 'no')   { clearAutoYes(); pendingAnswer.value = null; myResolver('no');   return }
    if (r === 'wake') { clearAutoYes(); pendingAnswer.value = null; myResolver('wake'); return }
    if (text) await voice.speak('没听清,请说确定、取消,或再叫我一次')
  }
}

function answerYes()  { clearAutoYes(); if (pendingAnswer.value) { const r = pendingAnswer.value; pendingAnswer.value = null; r('yes')  } }
function answerNo()   { clearAutoYes(); if (pendingAnswer.value) { const r = pendingAnswer.value; pendingAnswer.value = null; r('no')   } }
function answerWake() { clearAutoYes(); if (pendingAnswer.value) { const r = pendingAnswer.value; pendingAnswer.value = null; r('wake') } }

async function submitText() {
  if (!transcript.value.trim() || phase.value !== 'idle') return
  await runIntent()
}

async function pickCandidate(c) {
  if (!result.value) return
  phase.value = 'processing'
  try {
    const pa = {
      intent: result.value?.raw?.intent || 'find',
      item_id: c.item_id,
      location_id: result.value?.raw?.location_id || null,
      quantity: result.value?.raw?.quantity || 1,
    }
    // The user explicitly picked ONE candidate — bypass the watcher's
    // multi-highlight branch and only light up the chosen item.
    suppressNextAutoHighlight = true
    const r = await api.voiceIntent(transcript.value, { confirmed: true, pending_action: pa })
    result.value = r
    sceneHighlightIds.value = []
    sceneHighlightItem.value = null
    setTimeout(() => { sceneHighlightItem.value = c.item_id }, 50)
    if (r.speech) await voice.speak(r.speech)
    if (r.executed) emit('changed')
    loadRecent()
  } finally { phase.value = 'idle' }
}

// ---- Display helpers ----
const phaseLabel = computed(() => ({
  idle: wakeOn.value ? '等待唤醒词' : '空闲',
  command: '听取指令中…',
  'confirm-text': '请确认你说的内容',
  processing: 'AI 思考中…',
  'confirm-action': '请确认要执行的操作',
  speaking: '播报中…',
}[phase.value]))

const phaseColor = computed(() => ({
  idle: wakeOn.value ? 'bg-blue-400' : 'bg-slate-400',
  command: 'bg-emerald-500 animate-pulse',
  'confirm-text': 'bg-amber-400',
  processing: 'bg-purple-400 animate-pulse',
  'confirm-action': 'bg-amber-400',
  speaking: 'bg-cyan-400',
}[phase.value]))

const intentLabel = { find: '查找', take_out: '借出', put_in: '归位', consume: '用完', list: '列表', create_item: '新增', assist: '推荐', unknown: '未知' }

function candidateNameOf(id) {
  const c = (result.value?.candidates || []).find((x) => x.item_id === id)
  if (c) return c.item_name
  const it = sceneItems.value.find((x) => x.id === id)
  return it?.name || '#' + id
}
function candidateLocOf(id) {
  const c = (result.value?.candidates || []).find((x) => x.item_id === id)
  if (c) return c.location_path
  const it = sceneItems.value.find((x) => x.id === id)
  return it?.location_path
}
function labelOf(i) { return intentLabel[i] || i || '操作' }
function fmt(d) { return new Date(d).toLocaleTimeString('zh-CN', { hour12: false }) }
function fmtFull(d) { return new Date(d).toLocaleString('zh-CN', { hour12: false }) }
const txLabel = { take_out: '借出', put_in: '归位', consume: '用完', adjust: '盘点' }
const txClass = {
  take_out: 'bg-amber-100 text-amber-800',
  put_in:   'bg-emerald-100 text-emerald-700',
  consume:  'bg-rose-100 text-rose-700',
  adjust:   'bg-slate-100 text-slate-700',
}

const inConfirm = computed(() => phase.value === 'confirm-text' || phase.value === 'confirm-action')
</script>

<template>
  <div class="space-y-4">
    <!-- Confirm overlay (in-flow card so it doesn't cover everything) -->
    <div v-if="inConfirm" class="card border-2 border-amber-400 p-5 space-y-3 bg-amber-50">
      <div class="flex items-start gap-3">
        <div class="text-3xl">{{ phase === 'confirm-text' ? '🤔' : '⚠' }}</div>
        <div class="flex-1">
          <div class="text-xs text-amber-700 uppercase tracking-wide font-semibold">
            {{ phase === 'confirm-text' ? '发送前确认' : '低置信度确认' }}
          </div>
          <div class="text-lg font-semibold text-slate-800 mt-1">{{ confirmPrompt }}</div>
          <div v-if="phase === 'confirm-text'" class="mt-2 p-2 bg-white rounded border border-slate-200 text-slate-700">
            “{{ transcript }}”
          </div>
          <div class="text-xs text-slate-500 mt-2">{{ confirmDetail }}</div>
          <div v-if="heardAnswer" class="text-xs text-slate-400 mt-1">听到: {{ heardAnswer }}</div>
          <div v-if="phase === 'confirm-text' && autoYesArmedAt > 0" class="text-xs text-emerald-700 mt-1">
            ⏱ {{ autoYesCountdown }} 秒后自动确认 (说"取消"或点 ✗ 可中止)
          </div>
        </div>
      </div>
      <div class="flex gap-3 justify-end flex-wrap">
        <button class="btn btn-secondary text-base px-5" @click="answerWake">↻ 重说</button>
        <button class="btn btn-secondary text-base px-5" @click="answerNo">✗ 取消</button>
        <button class="btn btn-primary text-base px-5" @click="answerYes">✓ 确认</button>
      </div>
    </div>

    <!-- Row 1: console (2/3) + latest result (1/3) -->
    <div class="grid gap-4 lg:grid-cols-3">
    <div class="rounded-2xl bg-gradient-to-br from-slate-900 via-slate-800 to-slate-700 text-white p-3 sm:p-4 shadow-lg lg:col-span-2">
      <div class="flex items-center justify-between gap-2 flex-wrap">
        <div class="space-y-0.5 min-w-0">
          <div class="flex items-center gap-2">
            <span :class="['inline-block w-2 h-2 rounded-full', phaseColor]"></span>
            <div class="text-base font-semibold">{{ phaseLabel }}</div>
          </div>
          <div class="text-xs opacity-60 flex flex-wrap gap-1">
            <span>唤醒词:</span>
            <span v-for="w in wakeWordsRef" :key="w" class="px-1.5 py-0.5 bg-white/10 rounded-full">{{ w }}</span>
            <span v-if="!wakeWordsRef.length" class="opacity-50">(未配置)</span>
          </div>
        </div>
        <div class="flex gap-1.5 flex-wrap justify-end">
          <button
            :class="['px-2.5 py-1.5 rounded-lg text-xs transition flex items-center gap-1.5',
                     micMuted ? 'bg-rose-500 hover:bg-rose-400' : 'bg-white/10 hover:bg-white/20']"
            @click="toggleMute"
            :title="micMuted ? '点击恢复麦克风' : '闭麦防止误识别'">
            {{ micMuted ? '🚫 闭麦中' : '🎙 在线' }}
          </button>
          <button
            :class="['px-2.5 py-1.5 rounded-lg text-xs transition flex items-center gap-1.5',
                     wakeOn ? 'bg-blue-500 hover:bg-blue-400' : 'bg-white/10 hover:bg-white/20',
                     micMuted ? 'opacity-40 cursor-not-allowed' : '']"
            :disabled="micMuted || (phase !== 'idle' && phase !== 'speaking')"
            @click="toggleWake">
            <span :class="['w-1.5 h-1.5 rounded-full', wakeOn ? 'bg-white animate-pulse' : 'bg-white/40']"></span>
            {{ wakeOn ? '听唤醒词中' : '唤醒监听' }}
          </button>
        </div>
      </div>

      <!-- Compact push-to-talk; tap again to abort while busy. -->
      <div class="flex items-center gap-3 mt-3">
        <button
          :disabled="micMuted && phase === 'idle'"
          :class="['relative w-20 h-20 rounded-full flex items-center justify-center text-3xl shadow-lg transition shrink-0',
                   micMuted
                     ? 'bg-slate-700 cursor-not-allowed opacity-50'
                     : (phase === 'idle'
                       ? 'bg-emerald-500 hover:bg-emerald-400 ring-4 ring-emerald-300/30 active:scale-95'
                       : 'bg-rose-500 hover:bg-rose-400 active:scale-95')]"
          @click="pushToTalk">
          <span v-if="phase === 'command'" class="absolute inset-0 rounded-full animate-ping bg-emerald-400 opacity-30"></span>
          {{ phase === 'idle' ? '🎤' : (phase === 'speaking' ? '🔇' : '⏹') }}
        </button>
        <div class="flex-1 min-w-0">
          <div class="text-sm font-medium">
            {{ micMuted ? '已闭麦' : (phase === 'idle' ? '点击说话' : '点击中断') }}
          </div>
          <div v-if="voice.wakeHeard.value" class="text-xs opacity-60 truncate">监听: {{ voice.wakeHeard.value }}</div>
          <div class="mt-1 bg-black/20 rounded-md px-2 py-1">
            <Waveform :levels="meter.levels.value" :active="meter.active.value" :height="22" />
            <div class="flex items-center justify-between mt-0.5 text-[10px] opacity-60">
              <span>{{ meter.active.value ? '🎙' : '—' }}</span>
              <span v-if="meter.error.value" class="text-amber-300 truncate">⚠ {{ meter.error.value }}</span>
              <span v-else class="font-mono">{{ (meter.rms.value * 100).toFixed(0) }}%</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Manual text input (always available) -->
      <div class="mt-4 flex gap-2">
        <input v-model="transcript" :disabled="phase !== 'idle'"
               class="flex-1 px-3 py-2 rounded-lg bg-white/10 placeholder-white/50 disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-white/30"
               placeholder="或在此输入文字: 把充电宝放进卧室抽屉" @keyup.enter="submitText" />
        <button class="px-4 py-2 rounded-lg bg-white text-slate-900 font-medium hover:bg-slate-100 disabled:opacity-50"
                :disabled="phase !== 'idle' || !transcript.trim()" @click="submitText">发送</button>
      </div>

      <div v-if="!isHttps" class="mt-3 text-xs bg-amber-400/20 text-amber-200 rounded p-2">
        ⚠ HTTP 访问无法用麦克风,请改用 https://&lt;ip&gt;:8443
      </div>
      <div v-if="errorMsg" class="mt-2 text-xs bg-red-500/30 rounded p-2">{{ errorMsg }}</div>
      <div v-if="voice.error.value" class="mt-2 text-xs bg-red-500/30 rounded p-2">{{ voice.error.value }}</div>
    </div>

    <!-- Row 1 right: session history (was: latest result — swapped to keep latest result
         next to the 3D preview for at-a-glance correlation). -->
      <div class="card p-4 space-y-2 lg:col-span-1">
        <div class="font-semibold">本次会话</div>
        <div v-if="!history.length" class="text-sm text-slate-400 py-12 text-center">暂无对话</div>
        <ul v-else class="space-y-2 max-h-[480px] overflow-auto">
          <li v-for="(h, i) in history" :key="i" class="text-xs border-l-2 pl-2"
              :class="h.executed ? 'border-emerald-400' : (h.intent === 'unknown' ? 'border-slate-300' : 'border-amber-400')">
            <div class="text-slate-400">{{ fmt(h.time) }} · {{ intentLabel[h.intent] || h.intent }} · {{ (h.confidence*100).toFixed(0) }}%</div>
            <div class="text-slate-700">{{ h.text }}</div>
            <div class="text-slate-500">↳ {{ h.speech }}</div>
          </li>
        </ul>
      </div>
    </div>

    <!-- Row 2: 3D preview (2/3) + latest result (1/3) -->
    <div class="grid gap-4 lg:grid-cols-3">
      <div ref="sceneSection" class="card p-4 space-y-2 lg:col-span-2 scroll-mt-20">
        <div class="flex items-center justify-between">
          <div class="font-semibold">家中位置预览</div>
          <button class="text-xs text-slate-400 hover:text-slate-700" @click="loadScene">↻ 刷新</button>
        </div>
        <div v-if="!sceneLocations.length" class="text-sm text-slate-400 py-12 text-center">
          还没有 3D 布局,去 "🏗 3D" 标签先搭一下房间。
        </div>
        <Scene3D v-else :locations="sceneLocations" :items="sceneItems"
                 :highlight-item-id="sceneHighlightItem"
                 :highlight-item-ids="sceneHighlightIds"
                 :highlight-location-id="sceneHighlightLoc"
                 :low-quality="lowQuality"
                 :show-items-in-room-ids="shownItemRoomIds"
                 :active-home-id="activeHomeId"
                 :height="'clamp(420px, 63vh, 780px)'"
                 @update:low-quality="lowQuality = $event" />
        <div class="text-xs text-slate-400">语音找到物品时这里会自动推进镜头并高亮目标(其余区域半透淡出)。</div>
      </div>

      <div class="card p-4 space-y-3 lg:col-span-1">
        <div class="flex items-center justify-between">
          <div class="font-semibold">最新识别结果</div>
          <button v-if="result" class="text-xs text-slate-400 hover:text-slate-700" @click="result = null">清除</button>
        </div>
        <div v-if="!result" class="text-sm text-slate-400 py-6 text-center">
          点击大麦克风开始,或开启上方"唤醒监听"。
        </div>
        <div v-else class="space-y-3 text-sm">
          <div class="text-slate-500 text-xs">你说: <span class="text-slate-800">{{ transcript }}</span></div>
          <div class="flex flex-wrap gap-2 items-center">
            <span class="tag">{{ intentLabel[result.intent] || result.intent }}</span>
            <span class="text-xs text-slate-500">置信度</span>
            <div class="flex-1 min-w-[80px] h-2 bg-slate-100 rounded-full overflow-hidden">
              <div :class="['h-full', result.confidence >= confidenceThreshold ? 'bg-emerald-500' : 'bg-amber-400']"
                   :style="{ width: (result.confidence * 100) + '%' }"></div>
            </div>
            <span class="text-xs font-mono">{{ (result.confidence * 100).toFixed(0) }}%</span>
            <span v-if="result.executed" class="tag bg-emerald-100 text-emerald-700">已执行</span>
          </div>
          <div class="text-base text-slate-800 bg-slate-50 rounded-lg p-3">💬 {{ result.speech }}</div>

          <!-- Needs-based recommendations: shows purpose alongside each item, with a
               "看 3D" affordance to scroll the highlight into view. -->
          <div v-if="result.recommendations?.length">
            <div class="label mb-1">推荐用品</div>
            <table class="w-full text-xs">
              <thead class="text-slate-400">
                <tr><th class="text-left py-1">物品</th><th class="text-left py-1">用途</th><th class="text-left py-1">位置</th></tr>
              </thead>
              <tbody>
                <tr v-for="rec in result.recommendations" :key="rec.item_id"
                    class="border-t border-slate-100 hover:bg-slate-50 cursor-pointer"
                    @click="pickCandidate({ item_id: rec.item_id, item_name: candidateNameOf(rec.item_id) })">
                  <td class="py-1 font-medium">{{ candidateNameOf(rec.item_id) }}</td>
                  <td class="py-1 text-slate-600">{{ rec.purpose }}</td>
                  <td class="py-1 text-slate-500 truncate">{{ candidateLocOf(rec.item_id) || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div v-if="result.candidates?.length">
            <div class="label mb-1">候选物品</div>
            <ul class="space-y-1.5 max-h-48 overflow-auto">
              <li v-for="c in result.candidates" :key="c.item_id"
                  class="flex items-center justify-between bg-white border border-slate-200 rounded-lg p-2">
                <span class="min-w-0 truncate">
                  <span class="font-medium">{{ c.item_name }}</span>
                  <span class="text-slate-500 ml-2 text-xs">{{ c.location_path || '未指定位置' }}</span>
                </span>
                <button class="btn btn-secondary text-xs flex-shrink-0" :disabled="phase !== 'idle'" @click="pickCandidate(c)">选这个</button>
              </li>
            </ul>
          </div>
        </div>
      </div>
    </div>

    <!-- Depleted items (待补充) — items whose quantity dropped to 0. They're
         HIDDEN from search/voice/3D so stale records don't pollute results;
         this is the only place to act on them: either restock (revives the
         item via put_in) or delete (audit-logged permanent removal). -->
    <div v-if="depletedItems.length" class="card p-4 border-rose-300 border-2 bg-rose-50">
      <div class="flex items-center justify-between mb-2">
        <div class="font-semibold flex items-center gap-2">
          <span>📭 待补充</span>
          <span class="tag bg-rose-200 text-rose-900">{{ depletedItems.length }}</span>
        </div>
        <button class="text-xs text-slate-500 hover:text-slate-800" @click="loadDepleted">↻</button>
      </div>
      <div class="text-xs text-slate-600 mb-2">
        这些物品库存已归零, 搜索和 3D 视图都不会再显示。
        如果还会买就点 <b>补货</b>;以后不要了就点 <b>永久删除</b>(操作会写入审计日志)。
      </div>
      <ul class="divide-y divide-rose-200">
        <li v-for="it in depletedItems" :key="it.id" class="py-2 flex items-center gap-2 text-sm flex-wrap">
          <span class="font-medium">{{ it.name }}</span>
          <span v-if="it.aliases" class="text-xs text-slate-400">({{ it.aliases }})</span>
          <span class="text-xs text-slate-500 truncate flex-1 min-w-0">
            {{ it.location_path || '未指定位置' }}
          </span>
          <button class="btn btn-secondary text-xs" @click="restockDepleted(it)" title="补充库存,回到正常列表">⇪ 补货</button>
          <button class="btn btn-danger text-xs" @click="deleteDepleted(it)" title="从数据库永久删除">🗑 永久删除</button>
        </li>
      </ul>
    </div>

    <!-- Pending returns (借出未归位) — surfaced prominently above the recent
         transactions because it's an actionable reminder, not a passive log. -->
    <div v-if="pendingReturns.length" class="card p-4 border-amber-300 border-2 bg-amber-50">
      <div class="flex items-center justify-between mb-2">
        <div class="font-semibold flex items-center gap-2">
          <span>🔔 待归位</span>
          <span class="tag bg-amber-200 text-amber-900">{{ pendingReturns.length }}</span>
        </div>
        <button class="text-xs text-slate-500 hover:text-slate-800" @click="loadPending">↻</button>
      </div>
      <div class="text-xs text-slate-600 mb-2">
        以下物品已借出但未记录归位。如果其实已经用完/扔了, 点"已用完";如果放回去了, 点"已归位"。
      </div>
      <ul class="divide-y divide-amber-200">
        <li v-for="p in pendingReturns" :key="p.item_id" class="py-2 flex items-center gap-2 text-sm flex-wrap">
          <span class="font-medium">{{ p.item_name }}</span>
          <span class="font-mono text-slate-600">×{{ p.pending_quantity }}</span>
          <span class="text-xs text-slate-500 truncate flex-1 min-w-0">
            {{ p.return_location_path || '原位置' }} · {{ timeAgo(p.last_take_at) }}
          </span>
          <button class="btn btn-secondary text-xs" @click="markReturned(p)" title="已经放回原位置">✓ 已归位</button>
          <button class="btn btn-secondary text-xs" @click="markConsumed(p)" title="已经用完了/扔了/送人了, 不会再归位">⊗ 已用完</button>
        </li>
      </ul>
    </div>

    <!-- Recent transactions -->
    <div class="card p-4">
      <div class="flex items-center justify-between mb-2">
        <div class="font-semibold">近期取放记录</div>
        <button class="text-xs text-slate-400 hover:text-slate-700" @click="loadRecent">↻ 刷新</button>
      </div>
      <div v-if="!recentTx.length" class="text-sm text-slate-400 py-4 text-center">暂无</div>
      <ul v-else class="divide-y divide-slate-100">
        <li v-for="t in recentTx" :key="t.id" class="py-2 flex items-center gap-3 text-sm">
          <span :class="['tag', txClass[t.action] || 'bg-slate-100 text-slate-700']">
            {{ txLabel[t.action] || t.action }}
          </span>
          <span class="font-medium">{{ t.item_name }}</span>
          <span class="font-mono text-slate-500">×{{ t.quantity }}</span>
          <span class="text-slate-500 truncate flex-1">{{ t.location_path || '' }}</span>
          <span class="text-xs text-slate-400">{{ fmtFull(t.created_at) }}</span>
        </li>
      </ul>
    </div>
  </div>
</template>
