<script setup>
import { ref, onMounted, computed } from 'vue'
import { api } from '../api'

const emit = defineEmits(['saved'])

const cfg = ref(null)
const saving = ref(false)
const testResult = ref('')
const errorMsg = ref('')
const apiKeyInput = ref('')  // empty means leave unchanged

const presets = [
  { label: 'OpenAI', base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini', supports_tools: true },
  { label: '硅基流动 SiliconFlow', base_url: 'https://api.siliconflow.cn/v1', model: 'Qwen/Qwen2.5-7B-Instruct', supports_tools: true },
  { label: 'DeepSeek', base_url: 'https://api.deepseek.com/v1', model: 'deepseek-chat', supports_tools: true },
  { label: 'Ollama (本地)', base_url: 'http://host.docker.internal:11434/v1', model: 'qwen2.5:7b-instruct', supports_tools: false },
  { label: '智谱 GLM', base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash', supports_tools: true },
]

async function load() {
  cfg.value = await api.getSettings()
  apiKeyInput.value = ''
}

const voices = ref([])
function loadVoices() {
  if (typeof window === 'undefined' || !window.speechSynthesis) return
  voices.value = window.speechSynthesis.getVoices() || []
}

onMounted(() => {
  load()
  loadVoices()
  if (typeof window !== 'undefined' && window.speechSynthesis) {
    window.speechSynthesis.onvoiceschanged = loadVoices
  }
})

const filteredVoices = computed(() => {
  const all = voices.value
  // Prefer Chinese voices but show all so user can pick.
  const cn = all.filter((v) => /^zh|cmn|yue|wuu|nan/i.test(v.lang))
  return cn.length ? [...cn, ...all.filter((v) => !cn.includes(v))] : all
})

function previewTTS() {
  if (!cfg.value || !window.speechSynthesis) return
  window.speechSynthesis.cancel()
  const u = new SpeechSynthesisUtterance('你好,我是你的语音管家,这是当前选择的音色试听效果')
  u.lang = cfg.value.voice.tts_lang || 'zh-CN'
  u.rate = parseFloat(cfg.value.voice.tts_rate) || 1.05
  u.pitch = parseFloat(cfg.value.voice.tts_pitch) || 1.0
  const v = voices.value.find((vv) => vv.name === cfg.value.voice.tts_voice)
  if (v) u.voice = v
  window.speechSynthesis.speak(u)
}

function applyPreset(p) {
  cfg.value.llm.base_url = p.base_url
  cfg.value.llm.model = p.model
  cfg.value.llm.supports_tools = p.supports_tools
}

async function save() {
  saving.value = true
  errorMsg.value = ''
  try {
    const llm = {
      base_url: cfg.value.llm.base_url,
      model: cfg.value.llm.model,
      temperature: parseFloat(cfg.value.llm.temperature),
      timeout: parseInt(cfg.value.llm.timeout, 10),
      supports_tools: !!cfg.value.llm.supports_tools,
    }
    if (apiKeyInput.value) llm.api_key = apiKeyInput.value
    const voice = {
      wake_words: (cfg.value.voice.wake_words || []).map((s) => s.trim()).filter(Boolean),
      confidence_threshold: parseFloat(cfg.value.voice.confidence_threshold),
      confirm_before_llm: !!cfg.value.voice.confirm_before_llm,
      tts_voice: cfg.value.voice.tts_voice || '',
      tts_lang: cfg.value.voice.tts_lang || 'zh-CN',
      tts_rate: parseFloat(cfg.value.voice.tts_rate) || 1.05,
      tts_pitch: parseFloat(cfg.value.voice.tts_pitch) || 1.0,
      whisper_url: cfg.value.voice.whisper_url,
      whisper_enabled: !!cfg.value.voice.whisper_enabled,
    }
    await api.updateSettings({ llm, voice })
    await load()
    emit('saved')
  } catch (e) { errorMsg.value = String(e.message || e) }
  finally { saving.value = false }
}

async function testLLM() {
  testResult.value = '测试中…'
  try {
    const r = await api.testLLM()
    testResult.value = '✅ 连通: ' + r.content
  } catch (e) {
    testResult.value = '❌ 失败: ' + (e.message || e)
  }
}

const wakeWordsText = computed({
  get: () => (cfg.value?.voice?.wake_words || []).join(','),
  set: (v) => { if (cfg.value) cfg.value.voice.wake_words = v.split(/[,，]/).map(s => s.trim()).filter(Boolean) },
})
</script>

<template>
  <div v-if="cfg" class="grid gap-4 md:grid-cols-2">
    <div class="card p-4 space-y-3">
      <div class="font-semibold">大模型 (LLM) 配置</div>
      <div>
        <label class="label">预设</label>
        <div class="flex flex-wrap gap-1 mt-1">
          <button v-for="p in presets" :key="p.label"
            class="btn btn-secondary text-xs" @click="applyPreset(p)">{{ p.label }}</button>
        </div>
      </div>
      <div>
        <label class="label">Base URL (兼容 OpenAI 协议)</label>
        <input v-model="cfg.llm.base_url" class="input" />
      </div>
      <div>
        <label class="label">API Key {{ cfg.llm.api_key_set ? '(已设置, 留空保持)' : '' }}</label>
        <input v-model="apiKeyInput" class="input" type="password" :placeholder="cfg.llm.api_key_set ? '••••' : 'sk-...'" />
      </div>
      <div>
        <label class="label">Model</label>
        <input v-model="cfg.llm.model" class="input" />
      </div>
      <div class="grid grid-cols-3 gap-2">
        <div>
          <label class="label">Temperature</label>
          <input v-model="cfg.llm.temperature" type="number" step="0.1" class="input" />
        </div>
        <div>
          <label class="label">Timeout (秒)</label>
          <input v-model="cfg.llm.timeout" type="number" class="input" />
        </div>
        <div class="flex items-end">
          <label class="flex items-center gap-2 text-sm">
            <input type="checkbox" v-model="cfg.llm.supports_tools" /> 支持工具调用
          </label>
        </div>
      </div>
      <div class="text-xs text-slate-500">
        勾选后用 OpenAI 风格 tool_calls; 否则降级为 JSON 模式 (适配老 Ollama 等).
      </div>
      <div class="flex gap-2">
        <button class="btn btn-primary" :disabled="saving" @click="save">保存</button>
        <button class="btn btn-secondary" @click="testLLM">测试连接</button>
      </div>
      <div class="text-sm">{{ testResult }}</div>
      <div v-if="errorMsg" class="text-xs text-red-600">{{ errorMsg }}</div>
    </div>

    <div class="card p-4 space-y-3">
      <div class="font-semibold">语音配置</div>
      <div>
        <label class="label">唤醒词 (用逗号分隔)</label>
        <input v-model="wakeWordsText" class="input" />
      </div>
      <div>
        <label class="label">置信度阈值 (低于此值需要确认): {{ cfg.voice.confidence_threshold }}</label>
        <input v-model.number="cfg.voice.confidence_threshold" type="range" min="0" max="1" step="0.05" class="w-full" />
        <div class="text-xs text-slate-500 mt-1">默认 0.5。低于阈值时会语音播报让你"确定/取消"。</div>
      </div>
      <div class="border-t pt-3">
        <label class="flex items-center gap-2 text-sm">
          <input type="checkbox" v-model="cfg.voice.confirm_before_llm" />
          发送给 AI 前先口头确认识别文本
        </label>
        <div class="text-xs text-slate-500 mt-1">开启后:每条指令在调 AI API 前会播报"你说的是…确认吗",省 token。关闭后直接发送。</div>
      </div>
      <div class="border-t pt-3 space-y-2">
        <div class="font-medium text-sm">TTS 音色 (浏览器内置)</div>
        <div>
          <label class="label">音色</label>
          <select v-model="cfg.voice.tts_voice" class="input">
            <option value="">浏览器默认</option>
            <option v-for="v in filteredVoices" :key="v.name" :value="v.name">
              {{ v.name }} — {{ v.lang }}{{ v.localService ? '' : ' (云端)' }}
            </option>
          </select>
          <div class="text-xs text-slate-500 mt-1">
            iPad / iPhone 推荐选 "Tingting" / "Sin-Ji" / "Mei-Jia" 等中文音色;Mac 上有 "婷婷" / "美佳" 等。
            列表为空说明浏览器还在加载,稍候再打开。
          </div>
        </div>
        <div>
          <label class="label">语种</label>
          <select v-model="cfg.voice.tts_lang" class="input">
            <option value="zh-CN">普通话 (zh-CN)</option>
            <option value="zh-TW">繁体 (zh-TW)</option>
            <option value="zh-HK">粤语 (zh-HK)</option>
            <option value="en-US">English (en-US)</option>
          </select>
        </div>
        <div class="grid grid-cols-2 gap-2">
          <div>
            <label class="label">语速 {{ Number(cfg.voice.tts_rate).toFixed(2) }}</label>
            <input v-model.number="cfg.voice.tts_rate" type="range" min="0.5" max="2" step="0.05" class="w-full" />
          </div>
          <div>
            <label class="label">音调 {{ Number(cfg.voice.tts_pitch).toFixed(2) }}</label>
            <input v-model.number="cfg.voice.tts_pitch" type="range" min="0" max="2" step="0.05" class="w-full" />
          </div>
        </div>
        <button class="btn btn-secondary text-sm" @click="previewTTS">▶ 试听</button>
      </div>
      <div class="border-t pt-3">
        <label class="flex items-center gap-2 text-sm">
          <input type="checkbox" v-model="cfg.voice.whisper_enabled" /> 启用 Whisper STT
        </label>
        <div class="text-xs text-slate-500 mt-1">默认使用浏览器 Web Speech API (需 iPad 联网); 开启后将上传音频到 Whisper 服务做离线识别.</div>
      </div>
      <div>
        <label class="label">Whisper Service URL</label>
        <input v-model="cfg.voice.whisper_url" class="input" />
      </div>
      <button class="btn btn-primary" :disabled="saving" @click="save">保存</button>
    </div>
  </div>
  <div v-else class="text-slate-500">加载中…</div>
</template>
