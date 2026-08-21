<script setup>
import { ref, onMounted, watch, computed } from 'vue'
import { api } from '../api'

const props = defineProps({ refreshKey: Number })

const txs = ref([])
const locations = ref([])
const filters = ref({ q: '', action: '', location_id: '', since: '', until: '', limit: 200 })
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const params = { ...filters.value }
    if (params.since) params.since = new Date(params.since).toISOString()
    if (params.until) params.until = new Date(params.until).toISOString()
    txs.value = await api.searchTx(params)
  } finally { loading.value = false }
}

async function loadLocations() {
  locations.value = await api.listLocations()
}

onMounted(() => { loadLocations(); load() })
watch(() => props.refreshKey, load)

let debounce
watch(filters, () => {
  clearTimeout(debounce)
  debounce = setTimeout(load, 200)
}, { deep: true })

function clearFilters() {
  filters.value = { q: '', action: '', location_id: '', since: '', until: '', limit: 200 }
}

function fmt(d) { return new Date(d).toLocaleString('zh-CN', { hour12: false }) }
const labels = { take_out: '借出', put_in: '归位', consume: '用完', adjust: '盘点' }
const palette = {
  take_out: 'bg-amber-100 text-amber-800',
  put_in: 'bg-emerald-100 text-emerald-700',
  consume: 'bg-rose-100 text-rose-700',
  adjust: 'bg-blue-100 text-blue-700',
}

const summary = computed(() => {
  const out = { take_out: 0, put_in: 0, consume: 0, adjust: 0 }
  for (const t of txs.value) out[t.action] = (out[t.action] || 0) + (t.quantity || 0)
  return out
})

// 回撤一条流水。undoable / undo_note 由服务端算 (见 services/inventory.annotate_undoable):
// 只有某物品的最后一条 take_out/put_in/consume 撤得回去, 中间被机器人动过就不安全。
const undoingId = ref(0)
const undoMsg = ref('')
async function undoTx(t) {
  if (!t.undoable || undoingId.value) return
  const verb = labels[t.action] || t.action
  if (!confirm(`回撤「${verb} ${t.item_name} ×${t.quantity}」?\n库存恢复到操作前。`
    + (t.action === 'put_in' ? '\n若这条是新建产生的, 物品档案一并删除。' : ''))) return
  undoingId.value = t.id
  undoMsg.value = ''
  try {
    const r = await api.undoTx(t.id)
    undoMsg.value = r?.message || '已回撤'
    await load()
  } catch (e) {
    undoMsg.value = String(e.message || e).replace(/^\d+\s+\w+:\s*/, '')
  } finally {
    undoingId.value = 0
  }
}

function exportCsv() {
  const cols = ['时间', '动作', '物品', '数量', '位置', '备注']
  const rows = txs.value.map((t) => [
    fmt(t.created_at), labels[t.action] || t.action, t.item_name,
    t.quantity, t.location_path || '', t.note || ''
  ])
  const csv = '﻿' + [cols, ...rows].map((r) =>
    r.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(',')).join('\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `transactions-${new Date().toISOString().slice(0,10)}.csv`
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="space-y-3">
    <div class="card p-3 grid grid-cols-2 md:grid-cols-6 gap-2">
      <input v-model="filters.q" class="input col-span-2" placeholder="🔎 物品名" />
      <select v-model="filters.action" class="input">
        <option value="">全部动作</option>
        <option value="take_out">借出</option>
        <option value="put_in">归位</option>
        <option value="consume">用完</option>
        <option value="adjust">盘点</option>
      </select>
      <select v-model="filters.location_id" class="input">
        <option value="">全部位置</option>
        <option v-for="l in locations" :key="l.id" :value="l.id">{{ l.full_path }}</option>
      </select>
      <input v-model="filters.since" type="datetime-local" class="input" />
      <input v-model="filters.until" type="datetime-local" class="input" />
    </div>

    <div class="card p-3 flex items-center justify-between text-sm">
      <div class="flex gap-3 flex-wrap">
        <span>共 <b>{{ txs.length }}</b> 条</span>
        <span>借出: <b class="text-amber-700">{{ summary.take_out }}</b></span>
        <span>归位: <b class="text-emerald-600">{{ summary.put_in }}</b></span>
        <span v-if="summary.consume">用完: <b class="text-rose-600">{{ summary.consume }}</b></span>
        <span v-if="summary.adjust">盘点: <b class="text-blue-600">{{ summary.adjust }}</b></span>
        <span v-if="loading" class="text-slate-400">加载中…</span>
        <span v-if="undoMsg" class="text-emerald-700">{{ undoMsg }}</span>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-secondary text-xs" @click="clearFilters">清空筛选</button>
        <button class="btn btn-secondary text-xs" @click="exportCsv" :disabled="!txs.length">⬇ 导出</button>
        <button class="btn btn-secondary text-xs" @click="load">↻ 刷新</button>
      </div>
    </div>

    <div class="card overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-slate-500">
          <tr>
            <th class="text-left p-2">时间</th>
            <th class="text-left p-2">动作</th>
            <th class="text-left p-2">物品</th>
            <th class="text-right p-2">数量</th>
            <th class="text-left p-2">位置</th>
            <th class="text-left p-2">备注</th>
            <th class="text-right p-2">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in txs" :key="t.id" class="border-t hover:bg-slate-50">
            <td class="p-2 text-slate-500 whitespace-nowrap">{{ fmt(t.created_at) }}</td>
            <td class="p-2"><span :class="['tag', palette[t.action]]">{{ labels[t.action] || t.action }}</span></td>
            <td class="p-2 font-medium">{{ t.item_name }}</td>
            <td class="p-2 text-right font-mono">{{ t.quantity }}</td>
            <td class="p-2 text-slate-600">{{ t.location_path || '—' }}</td>
            <td class="p-2 text-slate-500">{{ t.note }}</td>
            <td class="p-2 text-right">
              <button class="btn btn-secondary text-xs"
                      :disabled="!t.undoable || undoingId === t.id"
                      :title="t.undoable ? '撤销这条, 库存恢复到操作前' : (t.undo_note || '无法回撤')"
                      @click="undoTx(t)">
                {{ undoingId === t.id ? '…' : '↩ 回撤' }}
              </button>
            </td>
          </tr>
          <tr v-if="!txs.length"><td colspan="7" class="p-8 text-center text-slate-400">无匹配记录</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
