<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { api } from '../api'
import PlanEditor from './PlanEditor.vue'
import Scene3D from './Scene3D.vue'
import LocationTreeNode from './LocationTreeNode.vue'
import LevelSlotFields from './LevelSlotFields.vue'
import { catalogFor, autoArrange, effectiveGeometry } from '../composables/sceneLayout'
import { useEditHistory } from '../composables/useEditHistory'

const props = defineProps({ refreshKey: Number })
const emit = defineEmits(['changed'])

const locations = ref([])
const items = ref([])
const selectedId = ref(null)
const search = ref('')
const matches = ref([])
const highlightItemId = ref(null)
const highlightLocationId = ref(null)

// View state. iPad-friendly: tree is a slide-out drawer, view-mode toggles between
// 2D / 3D / Split. Default: split on lg+, 3D on smaller.
const isWide = ref(typeof window !== 'undefined' && window.innerWidth >= 1024)
const viewMode = ref(isWide.value ? 'split' : '3d')   // '2d' | '3d' | 'split'
const treeOpen = ref(false)
const propsOpen = ref(true)

if (typeof window !== 'undefined') {
  window.addEventListener('resize', () => {
    isWide.value = window.innerWidth >= 1024
  })
}

const history = useEditHistory()

async function load() {
  const [locs, its] = await Promise.all([api.listLocations(), api.listItems({ limit: 1000 })])
  locations.value = locs
  items.value = its
}
onMounted(load)
watch(() => props.refreshKey, load)

async function applyEdit(action) {
  const r = await history.applyEdit(action)
  await load()
  emit('changed')
  return r
}
async function doUndo() {
  await history.undo()
  await load()
  emit('changed')
}

const selected = computed(() => locations.value.find((l) => l.id === selectedId.value) || null)
const selectedCat = computed(() => selected.value ? catalogFor(selected.value.kind) : null)
const parentLoc = computed(() => {
  if (!selected.value?.parent_id) return null
  return locations.value.find((l) => l.id === selected.value.parent_id)
})
const parentGeo = computed(() => parentLoc.value ? effectiveGeometry(parentLoc.value) : null)

const form = ref({
  name: '', kind: '', w: 0, h: 0, d: 0, rot: 0, color: '',
  levelTrio: { levels: 0, level: 0, slot: 0, mount_y_mm: 0 },
  parent_id: null,
})

watch(selected, (loc) => {
  if (!loc) return
  const g = effectiveGeometry(loc)
  form.value = {
    name: loc.name, kind: loc.kind,
    w: g.w, h: g.h, d: g.d, rot: g.rot, color: g.color,
    levelTrio: {
      levels: g.levels || 0,
      level: g.level || 0,
      slot: g.slot || 0,
      mount_y_mm: Math.round((g.y || 0) * 1000),
    },
    parent_id: loc.parent_id || null,
  }
}, { immediate: true })

// All locations except self + descendants (valid parents).
const validParents = computed(() => {
  if (!selected.value) return locations.value
  const forbidden = new Set([selected.value.id])
  let added = true
  while (added) {
    added = false
    for (const l of locations.value) {
      if (l.parent_id != null && forbidden.has(l.parent_id) && !forbidden.has(l.id)) {
        forbidden.add(l.id); added = true
      }
    }
  }
  return locations.value.filter((l) => !forbidden.has(l.id))
})

// Layer-usage warning is now rendered by the shared LevelSlotFields component.

async function applyProps() {
  if (!selected.value) return
  const cur = effectiveGeometry(selected.value)
  const trio = form.value.levelTrio
  const g = {
    ...(selected.value.geometry || {}),
    x: cur.x, z: cur.z,
    y: ((+trio.mount_y_mm) || 0) / 1000,         // mm → m
    w: +form.value.w, h: +form.value.h, d: +form.value.d,
    rot: +form.value.rot, color: form.value.color,
    levels: +trio.levels || 0,
    level: +trio.level || 0,
    slot: +trio.slot || 0,
  }
  const patch = { geometry: g }
  if (form.value.name && form.value.name !== selected.value.name) patch.name = form.value.name
  if ((form.value.parent_id || null) !== (selected.value.parent_id || null)) {
    patch.parent_id = form.value.parent_id || null
  }
  await applyEdit({ kind: 'update', id: selected.value.id, patch, before: history.snapshot(selected.value) })
}

function copySelectedUuid() {
  if (!selected.value?.uuid) return
  try { navigator.clipboard.writeText(selected.value.uuid) } catch {}
}

async function autoArrangeAll() {
  if (!confirm('对所有未布局的节点应用默认网格布局?')) return
  const updates = autoArrange(locations.value)
  for (const u of updates) {
    const loc = locations.value.find((l) => l.id === u.id)
    await applyEdit({ kind: 'update', id: u.id, patch: { geometry: u.geometry }, before: history.snapshot(loc) })
  }
}

async function on3DTransformEnd({ id, geometry, parent_id }) {
  const loc = locations.value.find((l) => l.id === id)
  if (!loc) return
  const patch = { geometry }
  if (parent_id !== undefined) patch.parent_id = parent_id
  await applyEdit({ kind: 'update', id, patch, before: history.snapshot(loc) })
}

// (slot reordering is now done by editing the slot number directly in LevelSlotFields)

function runSearch() {
  if (!search.value.trim()) { matches.value = []; return }
  const q = search.value.trim().toLowerCase()
  const itemMatch = items.value.filter((i) => {
    const hay = (i.name + ' ' + (i.aliases || '') + ' ' + (i.category || '')).toLowerCase()
    return hay.includes(q)
  })
  const locMatch = locations.value.filter((l) =>
    l.name.toLowerCase().includes(q) || (l.full_path || '').toLowerCase().includes(q)
  )
  matches.value = [
    ...itemMatch.slice(0, 12).map((i) => ({ kind: 'item', id: i.id, name: i.name, sub: i.location_path || '未指定' })),
    ...locMatch.slice(0, 8).map((l) => ({ kind: 'loc', id: l.id, name: l.name, sub: l.full_path })),
  ]
}
watch(search, runSearch)

function pickMatch(m) {
  if (m.kind === 'item') {
    highlightItemId.value = null
    setTimeout(() => (highlightItemId.value = m.id), 30)
  } else {
    highlightLocationId.value = null
    setTimeout(() => (highlightLocationId.value = m.id), 30)
    selectedId.value = m.id
  }
}

// (level field visibility is owned by LevelSlotFields)

const itemCountByLoc = computed(() => {
  const direct = {}
  for (const it of items.value) if (it.location_id) direct[it.location_id] = (direct[it.location_id] || 0) + 1
  const childrenOf = new Map()
  for (const l of locations.value) {
    const k = l.parent_id || 0
    if (!childrenOf.has(k)) childrenOf.set(k, [])
    childrenOf.get(k).push(l.id)
  }
  function sum(id) { let s = direct[id] || 0; for (const c of childrenOf.get(id) || []) s += sum(c); return s }
  const out = {}
  for (const l of locations.value) out[l.id] = sum(l.id)
  return out
})

const locationTree = computed(() => {
  const byId = new Map()
  for (const l of locations.value) byId.set(l.id, { ...l, children: [] })
  const roots = []
  for (const node of byId.values()) {
    if (node.parent_id && byId.has(node.parent_id)) byId.get(node.parent_id).children.push(node)
    else roots.push(node)
  }
  const sortRec = (nodes) => {
    nodes.sort((a, b) => {
      const sa = +a.geometry?.slot || 0, sb = +b.geometry?.slot || 0
      if (sa && sb) return sa - sb
      return a.name.localeCompare(b.name, 'zh')
    })
    nodes.forEach((n) => sortRec(n.children))
  }
  sortRec(roots)
  return roots
})

function selectFromTree(id) {
  selectedId.value = id
  if (!isWide.value) treeOpen.value = false
}
</script>

<template>
  <div class="space-y-2">
    <!-- Top bar -->
    <div class="card p-2 flex flex-wrap gap-2 items-center">
      <input v-model="search" class="input flex-1 min-w-[180px]" placeholder="🔎 搜索物品或位置" />
      <div class="flex gap-1">
        <button class="btn btn-secondary text-xs" :disabled="!history.canUndo()"
                :title="history.canUndo() ? '撤销: ' + history.undoLabel() : '无可撤销操作'"
                @click="doUndo">↶ <span class="hidden sm:inline">撤销</span></button>
        <button class="btn btn-secondary text-xs" @click="autoArrangeAll" title="给未布局节点自动排版">
          ⚙ <span class="hidden sm:inline">自动布局</span></button>
        <button class="btn btn-secondary text-xs" :class="{'!bg-slate-900 !text-white': treeOpen}"
                @click="treeOpen = !treeOpen" title="结构树">
          🗂 <span class="hidden sm:inline">树</span></button>
      </div>
      <!-- View mode toggle -->
      <div class="ml-auto flex bg-slate-100 rounded-lg p-0.5 text-xs">
        <button :class="['px-3 py-1 rounded-md', viewMode === '2d' && 'bg-white shadow-sm font-medium']"
                @click="viewMode = '2d'">2D</button>
        <button :class="['px-3 py-1 rounded-md', viewMode === '3d' && 'bg-white shadow-sm font-medium']"
                @click="viewMode = '3d'">3D</button>
        <button :class="['px-3 py-1 rounded-md', viewMode === 'split' && 'bg-white shadow-sm font-medium']"
                @click="viewMode = 'split'" title="分屏">⟷</button>
      </div>
    </div>

    <!-- Search results dropdown -->
    <div v-if="matches.length" class="card p-1 max-h-40 overflow-auto">
      <ul class="text-sm divide-y divide-slate-100">
        <li v-for="m in matches" :key="m.kind + m.id"
            class="px-3 py-1.5 flex justify-between items-center hover:bg-slate-50 cursor-pointer"
            @click="pickMatch(m)">
          <span class="truncate min-w-0">
            <span class="mr-2">{{ m.kind === 'item' ? '📎' : '🗂' }}</span>
            <span class="font-medium">{{ m.name }}</span>
            <span class="text-xs text-slate-500 ml-2">{{ m.sub }}</span>
          </span>
          <span class="text-xs text-slate-400 flex-shrink-0">推进 →</span>
        </li>
      </ul>
    </div>

    <!-- Main editor area. 'split' uses 2 cols on lg+; otherwise stacks. -->
    <div :class="[
        'grid gap-3',
        viewMode === 'split' ? 'lg:grid-cols-2 grid-cols-1' : 'grid-cols-1'
      ]">
      <PlanEditor v-show="viewMode === '2d' || viewMode === 'split'"
                  :locations="locations" :items="items" :selected-id="selectedId"
                  :apply-edit="applyEdit" :snapshot="history.snapshot"
                  @select="selectedId = $event" @changed="load" />
      <div v-show="viewMode === '3d' || viewMode === 'split'" class="card p-2">
        <div class="flex items-center justify-between mb-2 px-1 text-xs">
          <span class="font-medium">3D 预览 / 编辑</span>
          <span class="text-slate-400 hidden md:inline">点物体选中,gizmo 拖动</span>
        </div>
        <Scene3D :locations="locations" :items="items"
                 :highlight-item-id="highlightItemId"
                 :highlight-location-id="highlightLocationId"
                 :selected-location-id="selectedId"
                 :selectable="true" :editable="true"
                 :height="viewMode === '3d' ? 600 : 540"
                 @select-location="selectedId = $event"
                 @select-item="(id) => { highlightItemId = null; setTimeout(() => highlightItemId = id, 30) }"
                 @transform-end="on3DTransformEnd" />
      </div>
    </div>

    <!-- Properties panel: collapsible card -->
    <div v-if="selected" class="card p-3">
      <div class="flex items-center justify-between mb-2">
        <div class="font-semibold flex items-center gap-2 min-w-0">
          <span class="flex-shrink-0">{{ selectedCat?.icon || '📍' }} {{ selected.name }}</span>
          <span class="text-xs text-slate-500 font-normal truncate">{{ selected.full_path }}</span>
        </div>
        <button class="text-xs text-slate-400 hover:text-slate-700 flex-shrink-0" @click="propsOpen = !propsOpen">
          {{ propsOpen ? '收起' : '展开' }}
        </button>
      </div>
      <div v-if="propsOpen">
        <!-- UUID readonly + parent picker -->
        <div class="grid grid-cols-1 md:grid-cols-7 gap-2 text-sm mb-2">
          <div class="md:col-span-3">
            <label class="label">UUID (导入用,只读)</label>
            <div class="flex gap-1">
              <input :value="selected.uuid" readonly class="input font-mono text-xs bg-slate-50" />
              <button class="btn btn-secondary text-xs" @click="copySelectedUuid">📋</button>
            </div>
          </div>
          <div class="md:col-span-4">
            <label class="label">所在父级</label>
            <select v-model="form.parent_id" class="input">
              <option :value="null">📁 根目录</option>
              <option v-for="t in validParents" :key="t.id" :value="t.id">{{ t.full_path }}</option>
            </select>
          </div>
        </div>

        <div class="grid grid-cols-2 md:grid-cols-7 gap-2 text-sm">
          <div class="md:col-span-2"><label class="label">名称</label><input v-model="form.name" class="input" /></div>
          <div><label class="label">宽 w</label><input v-model.number="form.w" type="number" step="0.05" min="0.05" class="input" /></div>
          <div><label class="label">高 h</label><input v-model.number="form.h" type="number" step="0.05" min="0.05" class="input" /></div>
          <div><label class="label">深 d</label><input v-model.number="form.d" type="number" step="0.05" min="0.05" class="input" /></div>
          <div><label class="label">朝向°</label><input v-model.number="form.rot" type="number" step="5" class="input" /></div>
          <div><label class="label">颜色</label><input v-model="form.color" type="color" class="input h-9 p-1" /></div>
        </div>
        <div class="mt-2">
          <LevelSlotFields v-model="form.levelTrio"
                           :selected-loc="selected"
                           :parent-loc="parentLoc"
                           :all-locations="locations"
                           :live-width="form.w" />
        </div>
        <div class="mt-3 flex gap-2 items-center">
          <button class="btn btn-primary" @click="applyProps">保存</button>
          <span class="text-xs text-slate-500 truncate">2D 拖空白平移,3D 拖物体三维移动,Delete 删除。</span>
        </div>
      </div>
    </div>

    <!-- Tree drawer (overlay; toggled by 🗂 button) -->
    <div v-if="treeOpen" class="fixed inset-0 z-30 flex">
      <div class="absolute inset-0 bg-black/30" @click="treeOpen = false"></div>
      <aside class="relative ml-auto w-80 max-w-[90vw] h-full bg-white shadow-2xl flex flex-col">
        <div class="px-4 py-3 border-b flex items-center justify-between">
          <div class="font-semibold">🗂 结构树</div>
          <button class="btn btn-secondary text-xs" @click="treeOpen = false">✕ 关闭</button>
        </div>
        <div class="flex-1 overflow-auto p-2 text-sm">
          <div v-if="!locationTree.length" class="text-slate-400 p-2 text-center">先添加房间</div>
          <ul>
            <LocationTreeNode v-for="n in locationTree" :key="n.id"
                              :node="n" :selected-id="selectedId"
                              :counts="itemCountByLoc" :depth="0"
                              @select="selectFromTree" />
          </ul>
        </div>
      </aside>
    </div>
  </div>
</template>
