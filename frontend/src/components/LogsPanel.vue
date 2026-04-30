<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { api } from '../api'
import { clientLog, logEvent } from '../composables/useClientLog'
import { useAudioMeter } from '../composables/useAudioMeter'
import Waveform from './Waveform.vue'

const diag = ref(null)
const serverLogs = ref([])
const sinceId = ref(0)
const levelFilter = ref('')
const autoRefresh = ref(true)
const showSource = ref('all') // all / client / server
const search = ref('')

const front = clientLog()
const meter = useAudioMeter({ bars: 28 })

let timer = null

async function loadDiag() {
  try { diag.value = await api.getDiag() } catch (e) { logEvent('ERROR', 'getDiag failed: ' + e.message) }
}
async function loadLogs(reset = false) {
  if (reset) { sinceId.value = 0; serverLogs.value = [] }
  try {
    const r = await api.getLogs(sinceId.value, levelFilter.value)
    if (r.items.length) {
      serverLogs.value = [...r.items.reverse(), ...serverLogs.value].slice(0, 500)
      sinceId.value = r.next_since_id
    }
  } catch (e) { logEvent('ERROR', 'getLogs failed: ' + e.message) }
}

onMounted(async () => {
  await loadDiag()
  await loadLogs(true)
  timer = setInterval(() => { if (autoRefresh.value) loadLogs() }, 3000)
})
onBeforeUnmount(() => { clearInterval(timer); meter.stop() })
watch(levelFilter, () => loadLogs(true))

// Client-side capability checks
const caps = computed(() => {
  if (typeof window === 'undefined') return {}
  const proto = window.location.protocol
  const host = window.location.hostname
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition
  return {
    href: window.location.href,
    secure_context: window.isSecureContext,
    protocol: proto,
    is_localhost: host === 'localhost' || host === '127.0.0.1',
    has_mediaDevices: !!navigator.mediaDevices?.getUserMedia,
    has_speech_recognition: !!SR,
    has_speech_synthesis: !!window.speechSynthesis,
    has_audio_context: !!(window.AudioContext || window.webkitAudioContext),
    user_agent: navigator.userAgent,
  }
})

const micPerm = ref('unknown')
async function checkMicPermission() {
  try {
    if (!navigator.permissions?.query) { micPerm.value = 'permissions API 不可用'; return }
    const p = await navigator.permissions.query({ name: 'microphone' })
    micPerm.value = p.state
    p.onchange = () => micPerm.value = p.state
  } catch (e) { micPerm.value = '查询失败: ' + e.message }
}
onMounted(checkMicPermission)

async function testMic() {
  logEvent('INFO', '测试麦克风…')
  try {
    await meter.start()
    if (meter.error.value) {
      logEvent('ERROR', '麦克风测试失败: ' + meter.error.value)
    } else {
      logEvent('INFO', '麦克风访问成功,正在显示波形')
      setTimeout(() => { meter.stop(); logEvent('INFO', '麦克风测试结束') }, 5000)
    }
  } catch (e) {
    logEvent('ERROR', '麦克风异常: ' + e.message)
  }
  await checkMicPermission()
}

async function testSTT() {
  logEvent('INFO', '测试 Web Speech API…')
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition
  if (!SR) { logEvent('ERROR', 'SpeechRecognition 不可用'); return }
  const r = new SR()
  r.lang = 'zh-CN'; r.continuous = false; r.interimResults = false
  r.onresult = (ev) => logEvent('INFO', '识别结果: ' + (ev.results[0]?.[0]?.transcript || ''))
  r.onerror = (ev) => logEvent('ERROR', 'SR 错误: ' + ev.error)
  r.onend = () => logEvent('INFO', '识别结束')
  try { r.start() } catch (e) { logEvent('ERROR', '启动失败: ' + e.message) }
}

async function testTTS() {
  if (!window.speechSynthesis) { logEvent('ERROR', '不支持 speechSynthesis'); return }
  const u = new SpeechSynthesisUtterance('语音合成测试通过')
  u.lang = 'zh-CN'
  u.onend = () => logEvent('INFO', 'TTS 播放结束')
  speechSynthesis.cancel()
  speechSynthesis.speak(u)
  logEvent('INFO', 'TTS 已发起')
}

async function testBackend() {
  try {
    const r = await api.health()
    logEvent('INFO', '后端连通: ' + JSON.stringify(r))
    await loadDiag()
  } catch (e) { logEvent('ERROR', '后端不可达: ' + e.message) }
}

async function testLLM() {
  try {
    const r = await api.testLLM()
    logEvent('INFO', 'LLM 通: ' + r.content)
  } catch (e) { logEvent('ERROR', 'LLM 失败: ' + e.message) }
}

const merged = computed(() => {
  let arr = []
  if (showSource.value !== 'server') arr = arr.concat(front.entries.map((e) => ({ ...e, source: 'client' })))
  if (showSource.value !== 'client') arr = arr.concat(serverLogs.value.map((e) => ({ ...e, source: 'server' })))
  arr.sort((a, b) => new Date(b.time) - new Date(a.time))
  if (search.value) {
    const s = search.value.toLowerCase()
    arr = arr.filter((e) => (e.message || '').toLowerCase().includes(s))
  }
  return arr
})

function fmt(t) { return new Date(t).toLocaleTimeString('zh-CN', { hour12: false }) }
const lvlClass = {
  ERROR: 'bg-red-100 text-red-700',
  WARNING: 'bg-amber-100 text-amber-700',
  INFO: 'bg-slate-100 text-slate-700',
  DEBUG: 'bg-slate-50 text-slate-500',
}
</script>

<template>
  <div class="space-y-3">
    <!-- Top diagnostic cards -->
    <div class="grid gap-3 lg:grid-cols-3">
      <div class="card p-4 space-y-2">
        <div class="font-semibold">浏览器能力</div>
        <ul class="text-xs space-y-1 font-mono">
          <li>地址: <span class="break-all">{{ caps.href }}</span></li>
          <li>secure context: <b :class="caps.secure_context ? 'text-emerald-600' : 'text-red-600'">{{ caps.secure_context }}</b>
            <span v-if="!caps.secure_context" class="text-amber-600"> ← 需要 https 才能开麦克风</span>
          </li>
          <li>mediaDevices: <b :class="caps.has_mediaDevices ? 'text-emerald-600' : 'text-red-600'">{{ caps.has_mediaDevices }}</b></li>
          <li>SpeechRecognition: <b :class="caps.has_speech_recognition ? 'text-emerald-600' : 'text-red-600'">{{ caps.has_speech_recognition }}</b></li>
          <li>speechSynthesis: <b :class="caps.has_speech_synthesis ? 'text-emerald-600' : 'text-red-600'">{{ caps.has_speech_synthesis }}</b></li>
          <li>AudioContext: <b :class="caps.has_audio_context ? 'text-emerald-600' : 'text-red-600'">{{ caps.has_audio_context }}</b></li>
          <li>麦克风权限: <b>{{ micPerm }}</b></li>
        </ul>
        <div class="text-xs text-slate-400">UA: {{ caps.user_agent }}</div>
      </div>

      <div class="card p-4 space-y-2">
        <div class="font-semibold">后端状态</div>
        <div v-if="!diag" class="text-sm text-slate-400">加载中…</div>
        <ul v-else class="text-xs space-y-1 font-mono">
          <li>Python: {{ diag.python }}</li>
          <li>DB: {{ diag.database_url }}</li>
          <li>物品: <b>{{ diag.counts.items }}</b> · 位置: <b>{{ diag.counts.locations }}</b> · 流水: <b>{{ diag.counts.transactions }}</b></li>
          <li>LLM URL: {{ diag.llm.base_url }}</li>
          <li>LLM Model: {{ diag.llm.model }}</li>
          <li>API Key: <b :class="diag.llm.api_key_set ? 'text-emerald-600' : 'text-red-600'">{{ diag.llm.api_key_set ? '已设置' : '未设置' }}</b></li>
          <li>Whisper: {{ diag.voice.whisper_enabled ? '启用' : '未启用' }} ({{ diag.voice.whisper_url }})</li>
        </ul>
      </div>

      <div class="card p-4 space-y-2">
        <div class="font-semibold">自检测试</div>
        <div class="grid grid-cols-2 gap-2">
          <button class="btn btn-secondary text-xs" @click="testBackend">测试后端</button>
          <button class="btn btn-secondary text-xs" @click="testLLM">测试 LLM</button>
          <button class="btn btn-secondary text-xs" @click="testMic">测试麦克风</button>
          <button class="btn btn-secondary text-xs" @click="testSTT">测试语音识别</button>
          <button class="btn btn-secondary text-xs col-span-2" @click="testTTS">测试语音播报</button>
        </div>
        <Waveform :levels="meter.levels.value" :active="meter.active.value" :height="40" />
        <div v-if="meter.error.value" class="text-xs text-red-600">麦克风错误: {{ meter.error.value }}</div>
      </div>
    </div>

    <!-- Logs viewer -->
    <div class="card">
      <div class="px-3 py-2 border-b border-slate-200 flex flex-wrap gap-2 items-center">
        <div class="font-semibold mr-3">运行日志</div>
        <select v-model="showSource" class="input w-auto text-sm">
          <option value="all">全部来源</option>
          <option value="client">仅前端</option>
          <option value="server">仅后端</option>
        </select>
        <select v-model="levelFilter" class="input w-auto text-sm">
          <option value="">所有级别</option>
          <option value="DEBUG">DEBUG 及以上</option>
          <option value="INFO">INFO 及以上</option>
          <option value="WARNING">WARNING 及以上</option>
          <option value="ERROR">仅 ERROR</option>
        </select>
        <input v-model="search" class="input flex-1 min-w-[160px] text-sm" placeholder="搜索关键字" />
        <label class="text-xs flex items-center gap-1"><input type="checkbox" v-model="autoRefresh" /> 自动刷新</label>
        <button class="btn btn-secondary text-xs" @click="loadLogs(true); loadDiag()">↻</button>
      </div>
      <div class="max-h-[480px] overflow-auto font-mono text-xs">
        <div v-if="!merged.length" class="p-6 text-center text-slate-400">暂无日志</div>
        <div v-for="e in merged" :key="e.source + '-' + e.id"
             class="px-3 py-1.5 border-b border-slate-50 hover:bg-slate-50 flex gap-2 items-start">
          <span class="text-slate-400 whitespace-nowrap">{{ fmt(e.time) }}</span>
          <span :class="['px-1.5 rounded', lvlClass[e.level] || 'bg-slate-100']">{{ e.level }}</span>
          <span :class="['tag', e.source === 'client' ? 'bg-blue-100 text-blue-700' : 'bg-slate-200 text-slate-700']">
            {{ e.source }}
          </span>
          <span class="flex-1 break-all whitespace-pre-wrap">{{ e.message }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
