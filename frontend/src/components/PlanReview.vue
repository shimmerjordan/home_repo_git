<script setup>
import { ref, computed, watch } from 'vue'
import { api } from '../api'

// 落库前的人工确认。
//
// 存在的理由: AI 的物品匹配会错。说"存入洗发水"而库里只有"洗手液"时, 老流程直接给
// 洗手液加了库存, 用户看到的只是一句"好的已存入" —— 错误要等到下次盘点才发现。
// 现在后端只出"方案"(stage=plan), 每条操作带上全部候选 + 一个"新建"选项, 由这里
// 逐条确认。**模糊命中一律不预选已有物品**, 默认新建, 想合并就在下拉里挑。
//
// 控件选原生 <select> 而不是自绘下拉: 主力设备是 iPad, 原生 select 有系统级的
// 滚轮选择器, 命中率和可访问性都比任何自绘方案好, 候选多了也自带滚动。
const props = defineProps({
  operations: { type: Array, default: () => [] },
  locations: { type: Array, default: () => [] },
  planId: { type: String, default: '' },
  text: { type: String, default: '' },
})
const emit = defineEmits(['applied', 'cancelled'])

const OPT_NEW = 'new'
const OPT_SKIP = 'skip'

const VERB = {
  take_out: '取出', put_in: '存入', consume: '用完',
  create_item: '新增', delete_item: '删除', find: '查到',
}
// 动作徽标沿用「近期取放记录」/「流水」页那套配色, 让同一个动作在全站长一个样。
// 每种都是"同色系深字配浅底", 在白底和带色底(amber-50 / red-50)上都清晰。
const VERB_CLASS = {
  take_out: 'bg-amber-100 text-amber-800',
  put_in: 'bg-emerald-100 text-emerald-700',
  consume: 'bg-rose-100 text-rose-700',
  create_item: 'bg-indigo-100 text-indigo-700',
  delete_item: 'bg-red-100 text-red-700',
  find: 'bg-blue-100 text-blue-700',
}

const busy = ref(false)
const err = ref('')
const drafts = ref([])

// 只有 pending 的条目要人确认; find 已经当场查完了, 作为信息行展示。
const pending = computed(() => drafts.value.filter((d) => d.pending))
const infoRows = computed(() => drafts.value.filter((d) => !d.pending))
const activeCount = computed(() => pending.value.filter((d) => d.choice !== OPT_SKIP).length)

function buildDrafts(ops) {
  return (ops || []).map((op, idx) => ({
    idx,
    pending: !!op.pending,
    intent: op.intent,
    itemName: op.item_name || '',
    speech: op.speech || '',
    reason: op.reason || '',
    matchedBy: op.matched_by || '',
    allowNew: !!op.allow_new,
    forceNew: !!op.force_new,
    options: op.options || [],
    choice: op.selected || OPT_SKIP,
    newName: op.new_name_default || op.item_name || '',
    quantity: op.quantity || 1,
    locationId: op.location_id ?? '',
    locationPath: op.location_path || '',
  }))
}

watch(() => props.operations, (ops) => {
  drafts.value = buildDrafts(ops)
  err.value = ''
}, { immediate: true, deep: false })

// 需要留意的条目: 名字只是相近 / 同名多处 / 库里没有。这些最容易出错, 给视觉提示。
function needsAttention(d) {
  return d.choice !== OPT_SKIP && ['fuzzy', 'ambiguous', 'none'].includes(d.matchedBy)
}

function rowClass(d) {
  if (d.choice === OPT_SKIP) return 'border-slate-200 bg-slate-50 opacity-70'
  if (d.intent === 'delete_item') return 'border-red-300 bg-red-50'
  if (needsAttention(d)) return 'border-amber-300 bg-amber-50'
  return 'border-slate-200 bg-white'
}

function toggleSkip(d) {
  if (d.choice === OPT_SKIP) {
    const first = d.options[0]
    d.choice = first ? first.key : (d.allowNew ? OPT_NEW : OPT_SKIP)
  } else {
    d.choice = OPT_SKIP
  }
}

const canApply = computed(() => {
  if (!activeCount.value) return false
  return pending.value.every(
    (d) => d.choice !== OPT_NEW || !!d.newName.trim()
  )
})

// 删除是不可逆的 (物品档案连历史流水一起消失), 单独再问一次。
const deletions = computed(() =>
  pending.value.filter((d) => d.intent === 'delete_item' && d.choice !== OPT_SKIP)
)

async function apply() {
  if (busy.value || !canApply.value) return
  if (deletions.value.length) {
    const names = deletions.value.map((d) => d.itemName || '?').join('、')
    if (!confirm(`永久删除「${names}」?历史流水一并消失, 不可恢复。`)) return
  }
  busy.value = true
  err.value = ''
  try {
    const decisions = pending.value.map((d) => ({
      intent: d.intent,
      option_key: d.choice,
      new_item_name: d.choice === OPT_NEW ? d.newName.trim() : (d.itemName || null),
      location_id: d.locationId === '' ? null : Number(d.locationId),
      location_name: null,
      quantity: Math.max(1, Number(d.quantity) || 1),
    }))
    const r = await api.voiceApply({ text: props.text, plan_id: props.planId, decisions })
    emit('applied', r)
  } catch (e) {
    err.value = String(e.message || e).replace(/^\d+\s+\w+:\s*/, '')
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div v-if="drafts.length" class="space-y-2">
    <div class="flex items-baseline justify-between gap-2">
      <div class="label">待确认</div>
      <span v-if="pending.length" class="tag bg-amber-100 text-amber-900">
        执行 {{ activeCount }}/{{ pending.length }}
      </span>
    </div>

    <!-- 只读的查询结果: 不用确认, 但要看得见 -->
    <div v-for="d in infoRows" :key="'i' + d.idx"
         class="rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs text-slate-700">
      <span :class="['tag mr-1', VERB_CLASS[d.intent] || 'bg-slate-200 text-slate-800']">
        {{ VERB[d.intent] || d.intent }}
      </span>{{ d.speech }}
    </div>

    <!-- 待确认的每一条 -->
    <div v-for="d in pending" :key="d.idx"
         :class="['rounded-lg border p-2.5 space-y-2 text-xs', rowClass(d)]">
      <!-- 标题行: 动作 + 物品 + 数量 -->
      <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span :class="['tag', VERB_CLASS[d.intent] || 'bg-slate-200 text-slate-800']">
          {{ VERB[d.intent] || d.intent }}
        </span>
        <span class="min-w-0 flex-1 truncate font-medium text-slate-800">{{ d.itemName || '未命名' }}</span>
        <label v-if="d.intent !== 'delete_item'" class="flex items-center gap-1 text-slate-500">
          <span>×</span>
          <input v-model.number="d.quantity" type="number" min="1" inputmode="numeric"
                 class="input w-16 px-2 py-1 text-center text-xs" :disabled="d.choice === OPT_SKIP" />
        </label>
        <button class="btn btn-ghost btn-touch text-xs" @click="toggleSkip(d)"
                :title="d.choice === OPT_SKIP ? '重新加入执行' : '这条不要执行'">
          {{ d.choice === OPT_SKIP ? '↺ 恢复' : '⊘ 跳过' }}
        </button>
      </div>

      <template v-if="d.choice !== OPT_SKIP">
        <!-- 候选选择区。选项多时原生 select 自带滚动 -->
        <label class="block space-y-1">
          <span class="text-slate-500">记到</span>
          <select v-model="d.choice" class="input py-1.5 text-xs">
            <option v-for="o in d.options" :key="o.key" :value="o.key">
              {{ o.kind === 'fuzzy' ? '~ ' : '' }}{{ o.label }}{{ o.sublabel ? ' · ' + o.sublabel : '' }}
            </option>
            <option :value="OPT_SKIP">跳过</option>
          </select>
        </label>

        <!-- "都不是, 我自己填" —— 选中新建时才可编辑 -->
        <label v-if="d.allowNew" class="block space-y-1">
          <span class="text-slate-500">新名称</span>
          <input v-model="d.newName" class="input py-1.5 text-xs"
                 :disabled="d.choice !== OPT_NEW"
                 :placeholder="d.choice === OPT_NEW ? '物品名' : '选「新建」后可改'" />
        </label>

        <label v-if="d.intent !== 'delete_item' && d.intent !== 'consume'" class="block space-y-1">
          <span class="text-slate-500">位置</span>
          <select v-model="d.locationId" class="input py-1.5 text-xs">
            <option value="">不改位置</option>
            <option v-for="l in locations" :key="l.id" :value="l.id">{{ l.full_path }}</option>
          </select>
        </label>

        <p v-if="d.reason" :class="['leading-relaxed', needsAttention(d) ? 'text-amber-800' : 'text-slate-500']">
          {{ needsAttention(d) ? '⚠ ' : '' }}{{ d.reason }}
        </p>
      </template>
      <p v-else class="text-slate-500">已跳过</p>
    </div>

    <div v-if="err" class="rounded-lg bg-red-50 p-2 text-xs text-red-700">{{ err }}</div>

    <div v-if="pending.length" class="flex flex-wrap justify-end gap-2 pt-1">
      <button class="btn btn-secondary btn-touch text-xs" :disabled="busy" @click="emit('cancelled')">
        取消
      </button>
      <button class="btn btn-accent btn-touch text-xs" :disabled="busy || !canApply" @click="apply">
        {{ busy ? '执行中…' : `✓ 执行 ${activeCount} 条` }}
      </button>
    </div>
  </div>
</template>
