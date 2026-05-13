<script setup>
// Git-blame style change log. Every mutation on locations / items / transactions
// is recorded by the backend audit service; this panel surfaces it with filters
// and an expandable per-row diff so you can scrub back through "who changed what".
import { ref, computed, onMounted, watch } from 'vue'
import { api } from '../api'

const props = defineProps({ refreshKey: Number })

const rows = ref([])
const expanded = ref(new Set())
const filters = ref({
  q: '', entity_type: '', action: '',
  since: '', until: '', limit: 300,
})
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const params = { ...filters.value }
    if (params.since) params.since = new Date(params.since).toISOString()
    if (params.until) params.until = new Date(params.until).toISOString()
    rows.value = await api.listAudit(params)
  } finally { loading.value = false }
}

onMounted(load)
watch(() => props.refreshKey, load)
let debounce
watch(filters, () => {
  clearTimeout(debounce)
  debounce = setTimeout(load, 250)
}, { deep: true })

function clearFilters() {
  filters.value = { q: '', entity_type: '', action: '', since: '', until: '', limit: 300 }
}

function toggle(id) {
  const s = new Set(expanded.value)
  s.has(id) ? s.delete(id) : s.add(id)
  expanded.value = s
}

function fmt(d) { return new Date(d).toLocaleString('zh-CN', { hour12: false }) }

const ENTITY_LABEL = { location: '位置', item: '物品', transaction: '流水' }
const ACTION_LABEL = { create: '新建', update: '修改', delete: '删除', restore: '撤销', take_out: '取出', put_in: '存入', adjust: '盘点' }
const ACTION_CLASS = {
  create: 'bg-emerald-100 text-emerald-700',
  update: 'bg-blue-100 text-blue-700',
  delete: 'bg-rose-100 text-rose-700',
  take_out: 'bg-amber-100 text-amber-700',
  put_in: 'bg-emerald-100 text-emerald-700',
  adjust: 'bg-slate-100 text-slate-700',
  restore: 'bg-violet-100 text-violet-700',
}

// Pretty-print a single diff value (null, primitives, objects).
function fmtVal(v) {
  if (v == null) return '∅'
  if (typeof v === 'object') return JSON.stringify(v)
  if (typeof v === 'number') return Number.isFinite(v) ? (Math.abs(v - Math.round(v)) < 1e-6 ? v.toFixed(0) : v.toFixed(3)) : String(v)
  return String(v)
}

function changeRows(changes) {
  if (!changes) return []
  const out = []
  for (const [k, pair] of Object.entries(changes)) {
    if (k.startsWith('_')) continue
    if (Array.isArray(pair) && pair.length === 2) out.push({ key: k, before: pair[0], after: pair[1] })
  }
  // Show geometry-flattened keys grouped together at the end.
  out.sort((a, b) => {
    const ag = a.key.startsWith('geometry.') ? 1 : 0
    const bg = b.key.startsWith('geometry.') ? 1 : 0
    if (ag !== bg) return ag - bg
    return a.key.localeCompare(b.key)
  })
  return out
}

const summary = computed(() => {
  const byAction = {}
  for (const r of rows.value) byAction[r.action] = (byAction[r.action] || 0) + 1
  return byAction
})
</script>

<template>
  <div class="space-y-3">
    <div class="card p-3 grid grid-cols-2 md:grid-cols-6 gap-2">
      <input v-model="filters.q" class="input col-span-2" placeholder="🔎 名称或摘要" />
      <select v-model="filters.entity_type" class="input">
        <option value="">全部对象</option>
        <option value="location">位置</option>
        <option value="item">物品</option>
        <option value="transaction">流水</option>
      </select>
      <select v-model="filters.action" class="input">
        <option value="">全部动作</option>
        <option value="create">新建</option>
        <option value="update">修改</option>
        <option value="delete">删除</option>
      </select>
      <input v-model="filters.since" type="datetime-local" class="input" />
      <input v-model="filters.until" type="datetime-local" class="input" />
    </div>

    <div class="card p-3 flex flex-wrap items-center justify-between text-sm gap-2">
      <div class="flex gap-3 flex-wrap">
        <span>共 <b>{{ rows.length }}</b> 条</span>
        <span v-for="(n, k) in summary" :key="k">
          {{ ACTION_LABEL[k] || k }}: <b>{{ n }}</b>
        </span>
        <span v-if="loading" class="text-slate-400">加载中…</span>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-secondary text-xs" @click="clearFilters">清空筛选</button>
        <button class="btn btn-secondary text-xs" @click="load">↻ 刷新</button>
      </div>
    </div>

    <div class="card overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-slate-500">
          <tr>
            <th class="text-left p-2 w-40">时间</th>
            <th class="text-left p-2 w-20">对象</th>
            <th class="text-left p-2 w-20">动作</th>
            <th class="text-left p-2">摘要</th>
            <th class="text-right p-2 w-16">字段</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="r in rows" :key="r.id">
            <tr class="border-t hover:bg-slate-50 cursor-pointer" @click="toggle(r.id)">
              <td class="p-2 text-slate-500 whitespace-nowrap font-mono text-xs">{{ fmt(r.ts) }}</td>
              <td class="p-2"><span class="tag">{{ ENTITY_LABEL[r.entity_type] || r.entity_type }}</span></td>
              <td class="p-2"><span :class="['tag', ACTION_CLASS[r.action]]">{{ ACTION_LABEL[r.action] || r.action }}</span></td>
              <td class="p-2">
                <div class="text-slate-800">{{ r.summary }}</div>
                <div class="text-xs text-slate-400">{{ r.source }} · #{{ r.entity_id ?? '—' }}</div>
              </td>
              <td class="p-2 text-right text-slate-400 font-mono text-xs">
                {{ changeRows(r.changes).length }} {{ expanded.has(r.id) ? '▾' : '▸' }}
              </td>
            </tr>
            <tr v-if="expanded.has(r.id)" class="border-t bg-slate-50/60">
              <td colspan="5" class="p-3">
                <div v-if="r.changes?._created" class="text-xs text-emerald-700 mb-2">
                  新建,初始字段:
                </div>
                <div v-if="r.changes?._deleted" class="text-xs text-rose-700 mb-2">
                  删除,被清除的字段:
                </div>
                <table class="w-full text-xs">
                  <thead class="text-slate-400">
                    <tr>
                      <th class="text-left py-1 w-1/3">字段</th>
                      <th class="text-left py-1 w-1/3">变更前</th>
                      <th class="text-left py-1 w-1/3">变更后</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in changeRows(r.changes)" :key="row.key" class="border-t border-slate-200">
                      <td class="py-1 font-mono">{{ row.key }}</td>
                      <td class="py-1 font-mono text-rose-700 break-all">{{ fmtVal(row.before) }}</td>
                      <td class="py-1 font-mono text-emerald-700 break-all">{{ fmtVal(row.after) }}</td>
                    </tr>
                    <tr v-if="!changeRows(r.changes).length">
                      <td colspan="3" class="text-slate-400 py-1">无字段级差异</td>
                    </tr>
                  </tbody>
                </table>
              </td>
            </tr>
          </template>
          <tr v-if="!rows.length">
            <td colspan="5" class="p-8 text-center text-slate-400">暂无变更记录</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
