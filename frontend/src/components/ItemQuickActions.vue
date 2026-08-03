<script setup>
import { ref, computed } from 'vue'
import { api } from '../api'
import { useActionLabels } from '../composables/useActionLabels'

const { showLabels, toggleLabels } = useActionLabels()

// 识别结果 / 流水条目上的快捷操作。绕过语音与 LLM, 直接打 REST,
// 省掉"想取一件东西还得重新说一遍"的整轮 SR → 确认 → 意图解析。
const props = defineProps({
  itemId: { type: Number, required: true },
  locations: { type: Array, default: () => [] },
  // 窄卡片(1/3 宽)里按钮排不进内容行, compact 时自己占一行。
  compact: { type: Boolean, default: false },
})
const emit = defineEmits(['done'])

const open = ref('')          // '' | 'take' | 'put' | 'move'
const snapshot = ref(null)    // GET /api/items/{id} 结果
const loadErr = ref('')
const qty = ref(1)
const targetLocId = ref(null)
const busy = ref(false)
const err = ref('')

const quantity = computed(() => snapshot.value?.quantity ?? 0)

// 按需拉快照, 不在 mounted 拉: 一屏十几个条目会打出十几个请求。
// 必须拉而不是复用 VoicePanel 的 sceneItems —— 后者排除了 quantity=0 的物品,
// 且候选项数据里只有 location_path 没有 location_id, 换位置回填不了默认值。
async function ensureSnapshot() {
  if (snapshot.value || loadErr.value) return
  try {
    snapshot.value = await api.getItem(props.itemId)
    targetLocId.value = snapshot.value.location_id ?? null
  } catch (e) {
    const msg = String(e.message || e)
    loadErr.value = msg.includes('404') ? '该物品已删除' : '读取物品失败: ' + msg
  }
}

async function toggle(panel) {
  err.value = ''
  if (open.value === panel) { open.value = ''; return }
  await ensureSnapshot()
  if (loadErr.value) { open.value = ''; return }
  qty.value = 1
  targetLocId.value = snapshot.value?.location_id ?? null
  open.value = panel
}

function bump(d) {
  // 取出不能超过库存; 补库存无上限。
  const max = open.value === 'take' ? Math.max(1, quantity.value) : 9999
  qty.value = Math.min(max, Math.max(1, qty.value + d))
}

async function run(fn) {
  if (busy.value) return
  busy.value = true
  err.value = ''
  try {
    await fn()
    open.value = ''
    snapshot.value = null   // 强制下次重新取, 避免用过期数量
    emit('done')
  } catch (e) {
    err.value = String(e.message || e)   // 不吞异常, 面板保持展开让用户重试
  } finally {
    busy.value = false
  }
}

function confirmTake() {
  return run(() => api.recordTx(props.itemId, {
    item_id: props.itemId, action: 'take_out', quantity: qty.value,
    location_id: snapshot.value?.location_id ?? null,
  }))
}

function confirmPut() {
  return run(() => api.recordTx(props.itemId, {
    item_id: props.itemId, action: 'put_in', quantity: qty.value,
    location_id: snapshot.value?.location_id ?? null,
  }))
}

// 换位置不记流水: transaction 没有 move 动作, 而 put_in 带 location_id 虽能改位置
// 却会同时把数量加上去, 语义不对。走 PATCH, 由 update_item 写审计日志。
function confirmMove() {
  return run(() => api.updateItem(props.itemId, { location_id: targetLocId.value || null }))
}

// 清零只能用 adjust: 后端动作分支只有 take_out / put_in / adjust,
// consume 只写流水不改数量(它是用来销掉"待归位"欠账的, 取出时已经扣过数了)。
async function markUsedUp() {
  err.value = ''
  await ensureSnapshot()
  if (loadErr.value || !snapshot.value) return
  if (quantity.value <= 0) { err.value = '数量已经是 0'; return }
  if (!confirm(`确认「${snapshot.value.name}」用完了?数量清零后进入待补充列表,以后还能补货复活。`)) return
  run(() => api.recordTx(props.itemId, {
    item_id: props.itemId, action: 'adjust', quantity: 0, note: '用完清零',
  }))
}

async function removeItem() {
  err.value = ''
  await ensureSnapshot()
  if (loadErr.value || !snapshot.value) return
  if (!confirm(`确认永久删除「${snapshot.value.name}」?物品记录会消失,审计日志保留。`)) return
  run(() => api.deleteItem(props.itemId))
}

// 数据驱动而不是写五遍 <button>: 图标和文字标签绑在一起, 不会改了一个忘了另一个。
const ACTIONS = [
  { key: 'take', icon: '−', label: '取出', run: () => toggle('take') },
  { key: 'put', icon: '⇪', label: '补库存', run: () => toggle('put') },
  { key: 'move', icon: '⇄', label: '换位置', run: () => toggle('move') },
  { key: 'used', icon: '⊗', label: '用完了', run: markUsedUp },
  { key: 'del', icon: '🗑', label: '删除', danger: true, run: removeItem },
]
</script>

<template>
  <div :class="compact ? 'mt-1' : ''">
    <!-- 全部 @click.stop: 推荐用品那一处的父行有 @click, 不拦会触发一轮 LLM 意图重放 -->
    <div class="flex items-center gap-1 flex-wrap">
      <button v-for="a in ACTIONS" :key="a.key"
              :class="['btn btn-touch text-xs', a.danger ? 'btn-danger' : 'btn-secondary']"
              :disabled="busy" :aria-label="a.label" :title="a.label"
              @click.stop="a.run()">
        <span aria-hidden="true">{{ a.icon }}</span>
        <span v-if="showLabels">{{ a.label }}</span>
      </button>
      <!-- iPad 上 title 永远不显示, 所以给一个能点的开关让文字常驻。
           偏好存 localStorage 并跨所有条目共享 —— 一屏十几条不可能逐个切。 -->
      <!-- 按下态用现有按钮词汇表达(ghost → secondary), 而不是给图标附加勾号 -->
      <button :class="['btn btn-touch text-xs', showLabels ? 'btn-secondary' : 'btn-ghost']"
              :aria-label="showLabels ? '隐藏按钮文字说明' : '显示按钮文字说明'"
              :aria-pressed="showLabels"
              :title="showLabels ? '隐藏按钮文字说明' : '显示按钮文字说明'"
              @click.stop="toggleLabels">
        <span aria-hidden="true">ⓘ</span>
      </button>
    </div>

    <div v-if="loadErr" class="mt-1 text-xs text-rose-600">{{ loadErr }}</div>
    <div v-else-if="err" class="mt-1 text-xs text-rose-600">{{ err }}</div>

    <div v-if="open === 'take' || open === 'put'"
         class="mt-1 flex items-center gap-2 bg-slate-50 rounded-lg p-2 flex-wrap" @click.stop>
      <span class="text-xs text-slate-500">{{ open === 'take' ? '取出' : '补库存' }}</span>
      <button class="btn btn-secondary btn-touch text-xs" :disabled="busy"
              aria-label="减少数量" @click="bump(-1)">−</button>
      <span class="font-mono w-8 text-center" aria-live="polite">{{ qty }}</span>
      <button class="btn btn-secondary btn-touch text-xs" :disabled="busy"
              aria-label="增加数量" @click="bump(1)">+</button>
      <span v-if="open === 'take'" class="text-xs"
            :class="quantity > 0 ? 'text-slate-400' : 'text-rose-600'">库存 {{ quantity }}</span>
      <span class="flex-1"></span>
      <button class="btn btn-secondary btn-touch text-xs" :disabled="busy" @click="open = ''">取消</button>
      <button class="btn btn-primary btn-touch text-xs"
              :disabled="busy || (open === 'take' && quantity <= 0)"
              @click="open === 'take' ? confirmTake() : confirmPut()">确认</button>
    </div>

    <div v-if="open === 'move'"
         class="mt-1 flex items-center gap-2 bg-slate-50 rounded-lg p-2 flex-wrap" @click.stop>
      <span class="text-xs text-slate-500">移到</span>
      <select v-model="targetLocId" class="input text-xs flex-1 min-w-[140px]" :disabled="busy">
        <option :value="null">—</option>
        <option v-for="l in locations" :key="l.id" :value="l.id">{{ l.full_path }}</option>
      </select>
      <button class="btn btn-secondary text-xs" :disabled="busy" @click="open = ''">取消</button>
      <button class="btn btn-primary text-xs" :disabled="busy" @click="confirmMove">确认</button>
    </div>
  </div>
</template>
