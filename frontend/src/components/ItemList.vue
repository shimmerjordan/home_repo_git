<script setup>
import { ref, watch, onMounted, computed } from 'vue'
import { api } from '../api'
import ItemEditor from './ItemEditor.vue'
import LocationTree from './LocationTree.vue'

const props = defineProps({ refreshKey: Number })
const emit = defineEmits(['changed'])

const items = ref([])
const locations = ref([])
const q = ref('')
const editing = ref(null)
const showNew = ref(false)
const selectedLocId = ref(null) // null=all, 0=unassigned, n=location id
const importMode = ref('upsert')
const importBusy = ref(false)
const importMsg = ref('')
const fileInput = ref(null)

async function load() {
  // Items tab is the canonical management view — show depleted (quantity=0)
  // rows too so users can edit/restore them here. Search and voice paths
  // exclude depleted by default.
  const [is_, locs] = await Promise.all([
    api.listItems({ q: q.value || undefined, limit: 1000, include_depleted: true }),
    api.listLocations(),
  ])
  items.value = is_
  locations.value = locs
}

onMounted(load)
watch(() => props.refreshKey, load)
let debounce
watch(q, () => {
  clearTimeout(debounce)
  debounce = setTimeout(load, 250)
})

// Build the set of descendant location ids for the selected node.
const descendantIds = computed(() => {
  if (selectedLocId.value === null || selectedLocId.value === 0) return null
  const childrenOf = new Map()
  for (const l of locations.value) {
    if (!childrenOf.has(l.parent_id)) childrenOf.set(l.parent_id, [])
    childrenOf.get(l.parent_id).push(l.id)
  }
  const out = new Set([selectedLocId.value])
  const stack = [selectedLocId.value]
  while (stack.length) {
    const cur = stack.pop()
    for (const c of childrenOf.get(cur) || []) {
      if (!out.has(c)) { out.add(c); stack.push(c) }
    }
  }
  return out
})

const filteredItems = computed(() => {
  if (selectedLocId.value === null) return items.value
  if (selectedLocId.value === 0) return items.value.filter((i) => !i.location_id)
  return items.value.filter((i) => descendantIds.value?.has(i.location_id))
})

// Counts per location id including descendants.
const itemCounts = computed(() => {
  const direct = {}
  for (const it of items.value) {
    const k = it.location_id || 0
    direct[k] = (direct[k] || 0) + 1
  }
  // Roll up to ancestors.
  const parentOf = new Map(locations.value.map((l) => [l.id, l.parent_id]))
  const out = { ...direct }
  for (const l of locations.value) {
    if (!direct[l.id]) continue
    let p = parentOf.get(l.id)
    while (p) {
      out[p] = (out[p] || 0) + direct[l.id]
      p = parentOf.get(p)
    }
  }
  return out
})

async function quickTx(item, action) {
  const qty = parseInt(prompt(`${action === 'take_out' ? '取出' : '存入'} ${item.name} 的数量`, '1'), 10)
  if (!qty || qty < 1) return
  await api.recordTx(item.id, { item_id: item.id, action, quantity: qty, location_id: item.location_id })
  await load()
  emit('changed')
}

async function remove(item) {
  if (!confirm(`确认删除 "${item.name}" ?`)) return
  await api.deleteItem(item.id)
  await load()
  emit('changed')
}

async function saveItem(payload, id) {
  if (id) await api.updateItem(id, payload)
  else await api.createItem(payload)
  editing.value = null
  showNew.value = false
  await load()
  emit('changed')
}

function downloadExport() {
  window.location.href = api.exportItemsUrl()
}
function downloadTemplate() {
  window.location.href = api.importTemplateUrl()
}

async function onFilePicked(ev) {
  const file = ev.target.files?.[0]
  if (!file) return
  importBusy.value = true
  importMsg.value = ''
  try {
    const r = await api.importItems(file, importMode.value)
    importMsg.value = `✅ 导入完成: 新增 ${r.created},更新 ${r.updated} (模式: ${r.mode})`
    await load()
    emit('changed')
  } catch (e) {
    importMsg.value = '❌ ' + (e.message || e)
  } finally {
    importBusy.value = false
    if (fileInput.value) fileInput.value.value = ''
  }
}
</script>

<template>
  <div class="grid grid-cols-1 lg:grid-cols-[260px_1fr] gap-4">
    <aside class="card p-3 lg:sticky lg:top-20 self-start max-h-[80vh] overflow-auto">
      <div class="label mb-2">位置 (树)</div>
      <LocationTree :locations="locations" :selected-id="selectedLocId"
                    :item-counts="itemCounts" @select="(id) => selectedLocId = id" />
    </aside>

    <section class="space-y-3">
      <div class="card p-3 flex flex-wrap gap-2 items-center">
        <input v-model="q" class="input flex-1 min-w-[200px]" placeholder="🔎 搜索物品(支持别名/分类/标签)" />
        <button class="btn btn-primary" @click="showNew = true">+ 新增</button>
        <button class="btn btn-secondary" @click="downloadExport">⬇ 导出 CSV</button>
        <button class="btn btn-secondary" @click="downloadTemplate">📄 导入模板</button>
        <!-- Import mode + file picker stay on the same line, never split by wrap. -->
        <div class="flex gap-2 items-center flex-shrink-0">
          <select v-model="importMode" class="input w-auto">
            <option value="upsert">合并 (按名称匹配更新)</option>
            <option value="append">追加</option>
            <option value="replace">替换全部</option>
          </select>
          <label class="btn btn-primary cursor-pointer flex-shrink-0">
            ⬆ 导入 CSV
            <input ref="fileInput" type="file" accept=".csv,text/csv" class="hidden"
                   :disabled="importBusy" @change="onFilePicked" />
          </label>
        </div>
      </div>
      <div v-if="importMsg" class="text-sm" :class="importMsg.startsWith('✅') ? 'text-emerald-700' : 'text-red-600'">
        {{ importMsg }}
      </div>

      <div class="card overflow-hidden">
        <div class="bg-slate-50 px-3 py-2 text-xs text-slate-500 flex justify-between">
          <span>共 {{ filteredItems.length }} 件</span>
          <span v-if="selectedLocId !== null">已筛选当前位置(含子节点)</span>
        </div>
        <table class="w-full text-sm">
          <thead class="bg-slate-50 text-slate-500">
            <tr>
              <th class="text-left p-2">名称 / 别名 / 备注</th>
              <th class="text-left p-2">分类 · 标签</th>
              <th class="text-left p-2">位置</th>
              <th class="text-right p-2">数量</th>
              <th class="text-right p-2">单价</th>
              <th class="text-right p-2">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="it in filteredItems" :key="it.id" class="border-t hover:bg-slate-50 align-top">
              <td class="p-2">
                <div class="font-medium">{{ it.name }}</div>
                <div v-if="it.aliases" class="text-xs text-slate-400">别名: {{ it.aliases }}</div>
                <div v-if="it.note" class="text-xs text-slate-600 mt-0.5 whitespace-pre-line">📝 {{ it.note }}</div>
              </td>
              <td class="p-2">
                <div v-if="it.category"><span class="tag">{{ it.category }}</span></div>
                <div v-if="it.tags" class="text-xs text-slate-400 mt-0.5">{{ it.tags }}</div>
              </td>
              <td class="p-2 text-slate-600">{{ it.location_path || '—' }}</td>
              <td class="p-2 text-right font-mono">{{ it.quantity }}</td>
              <td class="p-2 text-right font-mono">{{ it.price ? '¥' + it.price.toFixed(2) : '—' }}</td>
              <td class="p-2 text-right whitespace-nowrap space-x-1">
                <button class="btn btn-secondary text-xs" @click="quickTx(it, 'put_in')" title="存入">+</button>
                <button class="btn btn-secondary text-xs" @click="quickTx(it, 'take_out')" title="取出">−</button>
                <button class="btn btn-secondary text-xs" @click="editing = it">编辑</button>
                <button class="btn btn-danger text-xs" @click="remove(it)">删</button>
              </td>
            </tr>
            <tr v-if="!filteredItems.length"><td colspan="6" class="p-6 text-center text-slate-400">无物品</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <ItemEditor v-if="showNew" :locations="locations"
      :default-location-id="selectedLocId && selectedLocId > 0 ? selectedLocId : null"
      @cancel="showNew=false" @save="(p) => saveItem(p, null)" />
    <ItemEditor v-if="editing" :item="editing" :locations="locations"
      @cancel="editing=null" @save="(p) => saveItem(p, editing.id)" />
  </div>
</template>
