<script setup>
import { ref, computed, watch, toRef, onMounted, onBeforeUnmount } from 'vue'
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

async function loadScene() {
  try {
    const [locs, its] = await Promise.all([api.listLocations(), api.listItems({ limit: 1000 })])
    sceneLocations.value = locs
    sceneItems.value = its
  } catch {}
}

onMounted(() => { loadRecent(); loadScene() })
watch(() => props.refreshKey, () => { loadRecent(); loadScene() })

watch(() => result.value, (v) => {
  // When the LLM returns candidates, highlight every one with the same name as the
  // top candidate so multi-location items ("X 在多个地方") all light up at once.
  if (!v) return
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
  if (pendingAnswer.value) { pendingAnswer.value(false); pendingAnswer.value = null }
}

// ---- Voice meter is independent of SR; runs whenever we want a UI signal ----
async function ensureMeter() {
  if (!meter.active.value) await meter.start()
}

// ---- Wake mode toggle ----
async function toggleWake() {
  if (wakeOn.value) {
    wakeOn.value = false
    voice.stopWakeListening()
    if (phase.value === 'idle') meter.stop()
  } else {
    wakeOn.value = true
    await ensureMeter()
    armWake()
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
  if (phase.value !== 'idle') return
  voice.stopWakeListening() // make sure no SR is running
  await ensureMeter()
  await captureCommand('')
}

async function captureCommand(prefilledTail) {
  errorMsg.value = ''
  phase.value = 'command'
  transcript.value = ''
  // If the wake-detector handed us a tail like "充电宝在哪", skip a second SR roundtrip.
  if (prefilledTail && prefilledTail.length >= 2) {
    transcript.value = prefilledTail
  } else {
    const { text, blob, error: err } = await voice.captureUtterance({ timeout: 8000 })
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
  try {
    const intent = await api.voiceIntent(transcript.value)
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
      if (intent.speech) await voice.speak(intent.speech)
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
async function askConfirm({ prompt, detail }) {
  phase.value = (phase.value === 'processing') ? 'confirm-action' : 'confirm-text'
  confirmPrompt.value = prompt
  confirmDetail.value = detail || ''
  heardAnswer.value = ''
  await voice.speak(prompt)

  return new Promise((resolve) => {
    pendingAnswer.value = resolve
    listenLoop(resolve)
  })
}

async function listenLoop(myResolver) {
  // Loop listening for yes / no / wake until a definitive answer or button click.
  while (pendingAnswer.value === myResolver) {
    const { result: r, text } = await voice.listenAnswer({ timeout: 6000 })
    if (pendingAnswer.value !== myResolver) return // already resolved by button
    heardAnswer.value = text
    if (r === 'yes')  { pendingAnswer.value = null; myResolver('yes');  return }
    if (r === 'no')   { pendingAnswer.value = null; myResolver('no');   return }
    if (r === 'wake') { pendingAnswer.value = null; myResolver('wake'); return }
    if (text) await voice.speak('没听清,请说确定、取消,或再叫我一次')
  }
}

function answerYes()  { if (pendingAnswer.value) { const r = pendingAnswer.value; pendingAnswer.value = null; r('yes')  } }
function answerNo()   { if (pendingAnswer.value) { const r = pendingAnswer.value; pendingAnswer.value = null; r('no')   } }
function answerWake() { if (pendingAnswer.value) { const r = pendingAnswer.value; pendingAnswer.value = null; r('wake') } }

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
    const r = await api.voiceIntent(transcript.value, { confirmed: true, pending_action: pa })
    result.value = r
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

const intentLabel = { find: '查找', take_out: '取出', put_in: '存入', list: '列表', create_item: '新增', unknown: '未知' }
function labelOf(i) { return intentLabel[i] || i || '操作' }
function fmt(d) { return new Date(d).toLocaleTimeString('zh-CN', { hour12: false }) }
function fmtFull(d) { return new Date(d).toLocaleString('zh-CN', { hour12: false }) }
const txLabel = { take_out: '取出', put_in: '存入', adjust: '盘点' }

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
    <div class="rounded-2xl bg-gradient-to-br from-slate-900 via-slate-800 to-slate-700 text-white p-6 shadow-lg lg:col-span-2">
      <div class="flex items-center justify-between">
        <div class="space-y-1">
          <div class="text-xs opacity-70">语音控制台</div>
          <div class="flex items-center gap-2">
            <span :class="['inline-block w-2.5 h-2.5 rounded-full', phaseColor]"></span>
            <div class="text-xl font-semibold">{{ phaseLabel }}</div>
          </div>
          <div class="text-xs opacity-70 flex flex-wrap gap-1 mt-2">
            <span>唤醒词:</span>
            <span v-for="w in wakeWordsRef" :key="w" class="px-2 py-0.5 bg-white/10 rounded-full">{{ w }}</span>
            <span v-if="!wakeWordsRef.length" class="opacity-50">(未配置)</span>
          </div>
          <div v-if="voice.wakeHeard.value" class="text-xs opacity-50 mt-1">监听中: {{ voice.wakeHeard.value }}</div>
        </div>
        <!-- Smaller wake-listen toggle (top-right) -->
        <button
          :class="['px-3 py-2 rounded-lg text-sm transition flex items-center gap-2',
                   wakeOn ? 'bg-blue-500 hover:bg-blue-400' : 'bg-white/10 hover:bg-white/20']"
          :disabled="phase !== 'idle' && phase !== 'speaking'"
          @click="toggleWake">
          <span :class="['w-2 h-2 rounded-full', wakeOn ? 'bg-white animate-pulse' : 'bg-white/40']"></span>
          {{ wakeOn ? '正在监听唤醒词' : '开启唤醒监听' }}
        </button>
      </div>

      <!-- Big push-to-talk button (primary action) -->
      <div class="flex flex-col items-center gap-3 mt-6">
        <button
          :disabled="phase !== 'idle'"
          :class="['relative w-44 h-44 rounded-full flex items-center justify-center text-6xl shadow-2xl transition',
                   phase === 'idle'
                     ? 'bg-emerald-500 hover:bg-emerald-400 ring-8 ring-emerald-300/30 active:scale-95'
                     : 'bg-slate-600 cursor-not-allowed opacity-60']"
          @click="pushToTalk">
          <span v-if="phase === 'command'" class="absolute inset-0 rounded-full animate-ping bg-emerald-400 opacity-30"></span>
          🎤
        </button>
        <div class="text-sm opacity-90 text-center">
          <div class="font-medium">{{ phase === 'idle' ? '点击说话' : '请稍候' }}</div>
          <div class="text-xs opacity-60 mt-0.5">单次说话 · 一句话指令</div>
        </div>
      </div>

      <!-- Waveform -->
      <div class="mt-5 bg-black/20 rounded-xl p-3">
        <Waveform :levels="meter.levels.value" :active="meter.active.value" :height="48" />
        <div class="flex items-center justify-between mt-1 text-xs opacity-70">
          <span>{{ meter.active.value ? '🎙 音频采集中' : '点击麦克风启动' }}</span>
          <span v-if="meter.error.value" class="text-amber-300">⚠ {{ meter.error.value }}</span>
          <span v-else class="font-mono">RMS {{ (meter.rms.value * 100).toFixed(0) }}%</span>
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

    <!-- Latest result (1/3 of row 1) -->
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

    <!-- Row 2: 3D preview (2/3) + session history (1/3) -->
    <div class="grid gap-4 lg:grid-cols-3">
      <div class="card p-4 space-y-2 lg:col-span-2">
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
                 :height="'clamp(280px, 42vh, 520px)'"
                 @update:low-quality="lowQuality = $event" />
        <div class="text-xs text-slate-400">语音找到物品时这里会自动推进镜头并高亮目标(其余区域半透淡出)。</div>
      </div>

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

    <!-- Recent transactions -->
    <div class="card p-4">
      <div class="flex items-center justify-between mb-2">
        <div class="font-semibold">近期取放记录</div>
        <button class="text-xs text-slate-400 hover:text-slate-700" @click="loadRecent">↻ 刷新</button>
      </div>
      <div v-if="!recentTx.length" class="text-sm text-slate-400 py-4 text-center">暂无</div>
      <ul v-else class="divide-y divide-slate-100">
        <li v-for="t in recentTx" :key="t.id" class="py-2 flex items-center gap-3 text-sm">
          <span :class="['tag', t.action === 'take_out' ? 'bg-rose-100 text-rose-700' : 'bg-emerald-100 text-emerald-700']">
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
