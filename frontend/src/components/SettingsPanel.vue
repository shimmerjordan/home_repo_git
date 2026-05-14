<script setup>
import { ref, onMounted, computed } from 'vue'
import { api } from '../api'

const emit = defineEmits(['saved'])

const cfg = ref(null)
const saving = ref(false)
const testResult = ref('')
const errorMsg = ref('')
const apiKeyInput = ref('')  // empty means leave unchanged
const dtSignSecretInput = ref('')   // empty = leave unchanged
const dtAllowedInput = ref('')      // textarea, newline-separated
const tgTokenInput = ref('')        // empty = leave unchanged
const tgChatsInput = ref('')
const tgUsersInput = ref('')
const fsSecretInput = ref('')       // empty = leave unchanged
const fsChatsInput = ref('')
const fsUsersInput = ref('')
const origin = typeof window !== 'undefined' ? window.location.origin : ''

const presets = [
  { label: 'OpenAI', base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini', supports_tools: true },
  { label: '硅基流动 SiliconFlow', base_url: 'https://api.siliconflow.cn/v1', model: 'Qwen/Qwen2.5-7B-Instruct', supports_tools: true },
  { label: 'DeepSeek', base_url: 'https://api.deepseek.com/v1', model: 'deepseek-chat', supports_tools: true },
  { label: 'Ollama (本地)', base_url: 'http://host.docker.internal:11434/v1', model: 'qwen2.5:7b-instruct', supports_tools: false },
  { label: '智谱 GLM', base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash', supports_tools: true },
]

async function load() {
  cfg.value = await api.getSettings()
  if (cfg.value?.voice && cfg.value.voice.tts_enabled === undefined) cfg.value.voice.tts_enabled = true
  if (!cfg.value.dingtalk) cfg.value.dingtalk = { enabled: false, sign_secret: '', allowed_users: [] }
  if (!cfg.value.telegram) cfg.value.telegram = { enabled: false, bot_token: '', allowed_chat_ids: [], allowed_user_ids: [] }
  if (!cfg.value.feishu) cfg.value.feishu = { enabled: false, app_id: '', app_secret: '', allowed_chat_ids: [], allowed_open_ids: [] }
  apiKeyInput.value = ''
  dtSignSecretInput.value = ''
  dtAllowedInput.value = (cfg.value.dingtalk.allowed_users || []).join('\n')
  tgTokenInput.value = ''
  tgChatsInput.value = (cfg.value.telegram.allowed_chat_ids || []).join('\n')
  tgUsersInput.value = (cfg.value.telegram.allowed_user_ids || []).join('\n')
  fsSecretInput.value = ''
  fsChatsInput.value = (cfg.value.feishu.allowed_chat_ids || []).join('\n')
  fsUsersInput.value = (cfg.value.feishu.allowed_open_ids || []).join('\n')
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
      max_tokens: parseInt(cfg.value.llm.max_tokens, 10) || 512,
      fast_mode: !!cfg.value.llm.fast_mode,
    }
    if (apiKeyInput.value) llm.api_key = apiKeyInput.value
    const voice = {
      wake_words: (cfg.value.voice.wake_words || []).map((s) => s.trim()).filter(Boolean),
      confidence_threshold: parseFloat(cfg.value.voice.confidence_threshold),
      confirm_before_llm: !!cfg.value.voice.confirm_before_llm,
      tts_enabled: !!cfg.value.voice.tts_enabled,
      tts_voice: cfg.value.voice.tts_voice || '',
      tts_lang: cfg.value.voice.tts_lang || 'zh-CN',
      tts_rate: parseFloat(cfg.value.voice.tts_rate) || 1.05,
      tts_pitch: parseFloat(cfg.value.voice.tts_pitch) || 1.0,
      whisper_url: cfg.value.voice.whisper_url,
      whisper_enabled: !!cfg.value.voice.whisper_enabled,
    }
    const dingtalk = {
      enabled: !!cfg.value.dingtalk?.enabled,
      allowed_users: (dtAllowedInput.value || '')
        .split('\n').map((s) => s.trim()).filter(Boolean),
    }
    if (dtSignSecretInput.value) dingtalk.sign_secret = dtSignSecretInput.value
    const telegram = {
      enabled: !!cfg.value.telegram?.enabled,
      allowed_chat_ids: (tgChatsInput.value || '').split('\n').map((s) => s.trim()).filter(Boolean),
      allowed_user_ids: (tgUsersInput.value || '').split('\n').map((s) => s.trim()).filter(Boolean),
    }
    if (tgTokenInput.value) telegram.bot_token = tgTokenInput.value
    const feishu = {
      enabled: !!cfg.value.feishu?.enabled,
      app_id: cfg.value.feishu?.app_id || '',
      allowed_chat_ids: (fsChatsInput.value || '').split('\n').map((s) => s.trim()).filter(Boolean),
      allowed_open_ids: (fsUsersInput.value || '').split('\n').map((s) => s.trim()).filter(Boolean),
    }
    if (fsSecretInput.value) feishu.app_secret = fsSecretInput.value
    await api.updateSettings({ llm, voice, dingtalk, telegram, feishu })
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
      <div class="grid grid-cols-2 gap-2 border-t pt-3">
        <div>
          <label class="label">Max tokens (响应上限)</label>
          <input v-model.number="cfg.llm.max_tokens" type="number" min="64" max="8192" class="input" />
          <div class="text-xs text-slate-500 mt-1">越小越快, 中文 256~512 一般够用.</div>
        </div>
        <div class="flex items-end">
          <label class="flex items-center gap-2 text-sm">
            <input type="checkbox" v-model="cfg.llm.fast_mode" /> 极速模式 (精简上下文)
          </label>
        </div>
      </div>
      <div class="text-xs text-slate-500">
        极速模式 = 缩短系统提示 + 减少给 AI 的候选物品数. 配合轻量模型(如 glm-4-flash / qwen2.5-7b)效果最好.
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
        <div class="flex items-center justify-between">
          <div class="font-medium text-sm">TTS 音色 (浏览器内置)</div>
          <label class="flex items-center gap-1.5 text-xs">
            <input type="checkbox" v-model="cfg.voice.tts_enabled" /> 朗读 AI 结果
          </label>
        </div>
        <div class="text-xs text-slate-500">关闭后只显示文字, 不发声 — 也能进一步加快响应.</div>
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

    <!-- DingTalk bot integration (full-width card under the LLM+Voice cards) -->
    <div class="card p-4 space-y-3 md:col-span-2">
      <div class="font-semibold flex items-center gap-2">
        🤖 钉钉机器人
        <span class="text-xs text-slate-400 font-normal">通过 @机器人 在群里查询/操作仓储</span>
      </div>
      <label class="flex items-center gap-2 text-sm">
        <input type="checkbox" v-model="cfg.dingtalk.enabled" /> 启用钉钉 Webhook
      </label>
      <div>
        <label class="label">加签秘钥 (Sign Secret){{ cfg.dingtalk.sign_secret_set ? ' (已设置, 留空保持)' : '' }}</label>
        <input v-model="dtSignSecretInput" type="password" class="input"
               :placeholder="cfg.dingtalk.sign_secret_set ? '••••' : 'SEC...'" />
        <div class="text-xs text-slate-500 mt-1">
          在钉钉群 → 机器人管理 → 自定义机器人 → 安全设置选"加签", 复制 SEC 开头的秘钥粘到这里。
        </div>
      </div>
      <div>
        <label class="label">白名单 (一行一个 staffId 或 nick, 留空允许所有人)</label>
        <textarea v-model="dtAllowedInput" rows="2" class="input font-mono text-xs"
                  placeholder="zhangsan&#10;lisi"></textarea>
      </div>
      <div class="border-t pt-3">
        <div class="label">Webhook 地址</div>
        <div class="font-mono text-xs bg-slate-50 p-2 rounded select-all break-all">
          {{ origin }}/api/dingtalk/webhook
        </div>
        <div class="text-xs text-slate-500 mt-1">
          把这个地址粘到钉钉的"自定义机器人"配置里。<br>
          钉钉服务器需要能访问这个 URL — 家里部署要做端口转发或用 frp/cloudflared 暴露。详见 README。
        </div>
      </div>
      <button class="btn btn-primary" :disabled="saving" @click="save">保存</button>
    </div>

    <!-- Telegram bot (long-polling, no public IP required) -->
    <div class="card p-4 space-y-3 md:col-span-2">
      <div class="font-semibold flex items-center gap-2">
        ✈️ Telegram 机器人
        <span class="text-xs text-slate-400 font-normal">长轮询 · 无需公网 IP · 国内需翻墙</span>
      </div>
      <label class="flex items-center gap-2 text-sm">
        <input type="checkbox" v-model="cfg.telegram.enabled" /> 启用 Telegram 长轮询
      </label>
      <div>
        <label class="label">Bot Token{{ cfg.telegram.bot_token_set ? ' (已设置, 留空保持)' : '' }}</label>
        <input v-model="tgTokenInput" type="password" class="input"
               :placeholder="cfg.telegram.bot_token_set ? '••••' : '123456:ABC-DEF1234...'" />
        <div class="text-xs text-slate-500 mt-1">
          在 Telegram 找 <b>@BotFather</b> → /newbot → 拿到 token 粘到这里。NAS 主动连 api.telegram.org, 不需要任何端口转发。
        </div>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-2">
        <div>
          <label class="label">白名单 chat_id (一行一个, 留空允许所有)</label>
          <textarea v-model="tgChatsInput" rows="2" class="input font-mono text-xs"
                    placeholder="-1001234567890&#10;987654321"></textarea>
          <div class="text-xs text-slate-500 mt-1">群 id 是负数, 个人 id 是正数。把 bot 加到群后随便发一句, 看日志页能看到 chat_id。</div>
        </div>
        <div>
          <label class="label">白名单 user_id (一行一个, 留空允许所有)</label>
          <textarea v-model="tgUsersInput" rows="2" class="input font-mono text-xs"
                    placeholder="123456789"></textarea>
        </div>
      </div>
      <button class="btn btn-primary" :disabled="saving" @click="save">保存</button>
    </div>

    <!-- Feishu / Lark bot via Stream Mode (long-lived WebSocket, no public IP) -->
    <div class="card p-4 space-y-3 md:col-span-2">
      <div class="font-semibold flex items-center gap-2">
        🪶 飞书机器人 (Stream Mode)
        <span class="text-xs text-slate-400 font-normal">WebSocket 长连接 · 无需公网 IP · 国内可用</span>
      </div>
      <label class="flex items-center gap-2 text-sm">
        <input type="checkbox" v-model="cfg.feishu.enabled" /> 启用飞书长连接
      </label>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-2">
        <div>
          <label class="label">App ID</label>
          <input v-model="cfg.feishu.app_id" class="input" placeholder="cli_xxxxxxx" />
        </div>
        <div>
          <label class="label">App Secret{{ cfg.feishu.app_secret_set ? ' (已设置, 留空保持)' : '' }}</label>
          <input v-model="fsSecretInput" type="password" class="input"
                 :placeholder="cfg.feishu.app_secret_set ? '••••' : ''" />
        </div>
      </div>
      <div class="text-xs text-slate-500">
        在 <a href="https://open.feishu.cn/app" target="_blank" class="text-blue-600 underline">飞书开放平台</a>
        创建"自建应用",拿到 App ID + App Secret;开通 <b>"接收消息"</b> 权限,订阅
        <code>im.message.receive_v1</code> 事件,并把"事件订阅方式"改为
        <b>"长连接"</b>(Stream Mode)。详见 <code>docs/bots/feishu.md</code>。
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-2">
        <div>
          <label class="label">白名单 chat_id (一行一个, 留空允许所有)</label>
          <textarea v-model="fsChatsInput" rows="2" class="input font-mono text-xs"
                    placeholder="oc_xxxxxx"></textarea>
        </div>
        <div>
          <label class="label">白名单 open_id (一行一个, 留空允许所有)</label>
          <textarea v-model="fsUsersInput" rows="2" class="input font-mono text-xs"
                    placeholder="ou_xxxxxx"></textarea>
        </div>
      </div>
      <button class="btn btn-primary" :disabled="saving" @click="save">保存</button>
    </div>
  </div>
  <div v-else class="text-slate-500">加载中…</div>
</template>
