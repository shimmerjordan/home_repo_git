<script setup>
import { ref, computed } from 'vue'
import { api } from '../api'

// 逐条展示这次语音做了什么, 并允许单条改判。
//
// 存在的理由: 后端对 put_in/take_out/consume 会做模糊匹配, 说"存入充电宝"而库里只有
// "充电器"时会直接给充电器加库存。以前这个过程完全不可见, 用户只看到一句汇总 speech。
// 现在每条都写清命中了谁、是精确还是猜的, 猜错了可以一键撤销并改到正确目标。
const props = defineProps({
  operations: { type: Array, default: () => [] },
})
const emit = defineEmits(['changed'])

const openIdx = ref(-1)        // 展开改判面板的行号
const choice = ref(null)       // 选中的候选 item_id; 'NEW' = 建新物品
const newName = ref('')
const busy = ref(false)
const err = ref('')
const done = ref({})           // idx -> 完成提示, 让用户看到结果而不是行突然消失

const VERB = {
  find: '找到', take_out: '已取出', put_in: '已存入',
  consume: '已用完', create_item: '已新增', delete_item: '待删除',
}

// 只有产生了流水的操作才谈得上撤销 —— find 是只读的, delete_item 还没执行。
function canRevise(op) {
  return !!op.transaction_id && ['take_out', 'put_in', 'consume', 'create_item'].includes(op.intent)
}

function rowClass(op) {
  if (op.pending) return 'border-amber-300 bg-amber-50'
  if (op.matched_by === 'fuzzy') return 'border-amber-200 bg-amber-50/40'
  if (!op.executed) return 'border-slate-200 bg-slate-50'
  return 'border-slate-200 bg-white'
}

function iconOf(op) {
  if (op.pending) return '🗑'
  if (!op.executed) return '—'
  return op.matched_by === 'fuzzy' ? '⚠' : '✓'
}

function toggle(idx) {
  err.value = ''
  if (openIdx.value === idx) { openIdx.value = -1; return }
  const op = props.operations[idx]
  openIdx.value = idx
  choice.value = op.item_id ?? null
  newName.value = op.item_name || ''
}

// put_in / create_item 才有"其实是个新物品"的说法; 取出/用完一个不存在的东西没有意义。
const canCreateNew = computed(() => {
  const op = props.operations[openIdx.value]
  return op && ['put_in', 'create_item'].includes(op.intent)
})

const dirty = computed(() => {
  const op = props.operations[openIdx.value]
  if (!op) return false
  if (choice.value === 'NEW') return !!newName.value.trim()
  return choice.value !== op.item_id
})

async function run(idx, fn) {
  if (busy.value) return
  busy.value = true
  err.value = ''
  try {
    const r = await fn()
    done.value = { ...done.value, [idx]: r?.message || '完成' }
    openIdx.value = -1
    emit('changed')
  } catch (e) {
    err.value = String(e.message || e).replace(/^\d+\s+\w+:\s*/, '')
  } finally {
    busy.value = false
  }
}

function undo(idx) {
  const op = props.operations[idx]
  run(idx, () => api.undoTx(op.transaction_id))
}

function applyRedirect(idx) {
  const op = props.operations[idx]
  const target = choice.value === 'NEW'
    ? { new_item_name: newName.value.trim() }
    : { item_id: choice.value }
  run(idx, () => api.redirectTx(op.transaction_id, target))
}

function confirmDelete(idx) {
  const op = props.operations[idx]
  if (!confirm(`永久删除「${op.item_name}」?历史流水一并消失, 不可恢复。`)) return
  run(idx, async () => {
    await api.deleteItem(op.item_id)
    return { message: `已永久删除「${op.item_name}」` }
  })
}

function dismiss(idx) {
  done.value = { ...done.value, [idx]: '已取消' }
}
</script>

<template>
  <div v-if="operations.length" class="space-y-1.5">
    <div class="label">本次操作</div>

    <div v-for="(op, idx) in operations" :key="idx"
         :class="['rounded-lg border p-2 text-xs', rowClass(op)]">
      <div class="flex items-start gap-2">
        <span class="shrink-0">{{ iconOf(op) }}</span>
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-baseline gap-x-1.5">
            <span class="text-slate-500">{{ VERB[op.intent] || op.intent }}</span>
            <span class="font-medium text-slate-800">{{ op.item_name || '?' }}</span>
            <span v-if="op.intent !== 'delete_item' && op.intent !== 'find'"
                  class="font-mono text-slate-500">×{{ op.quantity }}</span>
            <span v-if="op.matched_by === 'fuzzy'"
                  class="rounded-full bg-amber-200 px-1.5 text-[10px] text-amber-900">猜的</span>
            <span v-else-if="op.matched_by === 'created'"
                  class="rounded-full bg-emerald-100 px-1.5 text-[10px] text-emerald-700">新建</span>
          </div>
          <div class="text-slate-500">
            {{ op.location_path || '未指定位置' }}
            <span v-if="op.remaining !== null && op.remaining !== undefined" class="font-mono">
              · 共 {{ op.remaining }}
            </span>
          </div>
          <div v-if="done[idx]" class="mt-0.5 text-emerald-700">{{ done[idx] }}</div>
          <div v-else-if="!op.executed && !op.pending" class="mt-0.5 text-slate-500">{{ op.speech }}</div>
        </div>

        <div v-if="!done[idx]" class="flex shrink-0 gap-1">
          <template v-if="op.pending">
            <button class="btn btn-danger btn-touch text-xs" :disabled="busy" @click="confirmDelete(idx)">确认删除</button>
            <button class="btn btn-secondary btn-touch text-xs" :disabled="busy" @click="dismiss(idx)">取消</button>
          </template>
          <button v-else-if="canRevise(op)" class="btn btn-secondary btn-touch text-xs"
                  :disabled="busy" @click="toggle(idx)">改判</button>
        </div>
      </div>

      <!-- 改判面板: 默认收起, 批量五条时不至于把卡片撑爆 -->
      <div v-if="openIdx === idx" class="mt-2 space-y-1.5 rounded-md bg-white/70 p-2">
        <div class="text-slate-500">改到</div>
        <label v-for="c in op.candidates" :key="c.item_id"
               class="flex min-h-[44px] items-center gap-2 rounded px-1 py-1 hover:bg-slate-50">
          <input type="radio" class="h-4 w-4 shrink-0" :value="c.item_id" v-model="choice" />
          <span class="font-medium">{{ c.item_name }}</span>
          <span class="truncate text-slate-500">{{ c.location_path || '未指定位置' }}</span>
          <span v-if="c.item_id === op.item_id" class="text-slate-400">← 当前</span>
        </label>

        <label v-if="canCreateNew" class="flex min-h-[44px] items-center gap-2 px-1">
          <input type="radio" class="h-4 w-4 shrink-0" value="NEW" v-model="choice" />
          <span>新物品:</span>
          <input v-model="newName" class="input flex-1 py-0.5 text-xs"
                 :disabled="choice !== 'NEW'" placeholder="新物品名称" />
        </label>

        <div class="flex flex-wrap justify-end gap-1 pt-1">
          <button class="btn btn-secondary btn-touch text-xs" :disabled="busy" @click="openIdx = -1">取消</button>
          <button class="btn btn-secondary btn-touch text-xs" :disabled="busy" @click="undo(idx)">仅撤销</button>
          <button class="btn btn-primary btn-touch text-xs" :disabled="busy || !dirty"
                  @click="applyRedirect(idx)">撤销并改</button>
        </div>
      </div>

      <div v-if="err && openIdx === idx" class="mt-1 text-rose-600">{{ err }}</div>
    </div>

    <div v-if="err && openIdx === -1" class="text-xs text-rose-600">{{ err }}</div>
  </div>
</template>
