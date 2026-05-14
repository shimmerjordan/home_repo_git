<script setup>
// Finder-style location browser. Click a folder to enter it; ⚙ button opens
// a single modal with all editable attributes.
import { ref, computed, onMounted, watch } from 'vue'
import { api } from '../api'
import { effectiveGeometry } from '../composables/sceneLayout'
import LevelSlotFields from './LevelSlotFields.vue'

const props = defineProps({ refreshKey: Number })
const emit = defineEmits(['changed'])

const locations = ref([])
const items = ref([])
const cwdId = ref(null) // null = root
const editing = ref(null)              // currently editing location, drives the modal
const editForm = ref(null)             // editable copy

async function load() {
  const [locs, all] = await Promise.all([api.listLocations(), api.listItems({ limit: 1000 })])
  locations.value = locs
  items.value = all
}
onMounted(load)
watch(() => props.refreshKey, load)

const byId = computed(() => {
  const m = new Map()
  for (const l of locations.value) m.set(l.id, l)
  return m
})

const childrenOf = computed(() => {
  const m = new Map()
  for (const l of locations.value) {
    const k = l.parent_id || null
    if (!m.has(k)) m.set(k, [])
    m.get(k).push(l)
  }
  for (const arr of m.values()) arr.sort((a, b) => a.name.localeCompare(b.name, 'zh'))
  return m
})

const cwdChildren = computed(() => childrenOf.value.get(cwdId.value) || [])
const cwdItems = computed(() => items.value.filter((i) => (i.location_id || null) === cwdId.value))

const breadcrumbs = computed(() => {
  const trail = []
  let cur = cwdId.value
  const seen = new Set()
  while (cur && !seen.has(cur)) {
    seen.add(cur)
    const node = byId.value.get(cur)
    if (!node) break
    trail.unshift(node)
    cur = node.parent_id
  }
  return trail
})

function descendantCount(locId) {
  const stack = [locId]
  const set = new Set([locId])
  while (stack.length) {
    const cur = stack.pop()
    for (const c of childrenOf.value.get(cur) || []) {
      if (!set.has(c.id)) { set.add(c.id); stack.push(c.id) }
    }
  }
  let cnt = 0
  for (const it of items.value) if (it.location_id && set.has(it.location_id)) cnt++
  return cnt
}

const KIND_OPTIONS = [
  { value: 'home',     label: '🏡 家 (顶层)' },
  { value: 'room',     label: '🏠 房间' },
  { value: 'shelf',    label: '📚 书架' },
  { value: 'cabinet',  label: '🗄 柜子' },
  { value: 'wardrobe', label: '👔 衣柜' },
  { value: 'drawer',   label: '🗃 抽屉柜' },
  { value: 'box',      label: '📦 收纳箱' },
  { value: 'desk',     label: '🖥 书桌' },
  { value: 'table',    label: '🍽 桌子' },
  { value: 'bed',      label: '🛏 床' },
  { value: 'sofa',     label: '🛋 沙发' },
  { value: 'chair',    label: '🪑 椅子' },
  { value: 'plant',    label: '🪴 盆栽' },
  { value: 'other',    label: '📍 其他' },
]
const KIND_ICON = Object.fromEntries(KIND_OPTIONS.map((o) => [o.value, o.label.split(' ')[0]]))

async function createFolder() {
  const name = prompt('新文件夹名称')
  if (!name?.trim()) return
  const kind = cwdId.value === null ? 'room' : 'box'
  await api.createLocation({ name: name.trim(), kind, parent_id: cwdId.value })
  await load(); emit('changed')
}

// All possible parent targets for the editing node, excluding self/descendants.
const moveTargets = computed(() => {
  if (!editing.value) return []
  const forbidden = new Set([editing.value.id])
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

function openEdit(node) {
  editing.value = node
  const g = effectiveGeometry(node)
  editForm.value = {
    name: node.name || '',
    kind: node.kind || 'box',
    parent_id: node.parent_id || null,
    note: node.note || '',
    // mm units in the modal — easier on the eyes than 0.05 m and friends.
    w_mm: Math.round(g.w * 1000),
    h_mm: Math.round(g.h * 1000),
    d_mm: Math.round(g.d * 1000),
    rot: g.rot || 0,
    levelTrio: {
      levels: g.levels || 0,
      level: g.level || 0,
      slot: g.slot || 0,
      mount_y_mm: Math.round((+node.geometry?.y || 0) * 1000),
    },
  }
}

const editParent = computed(() => {
  if (!editForm.value?.parent_id) return null
  return locations.value.find((l) => l.id === editForm.value.parent_id) || null
})

function copyUuid() {
  if (!editing.value?.uuid) return
  try {
    navigator.clipboard.writeText(editing.value.uuid)
  } catch {
    // Fallback: select text in the input
  }
}

function closeEdit() {
  editing.value = null
  editForm.value = null
}

async function saveEdit() {
  if (!editing.value) return
  const patch = {}
  const f = editForm.value
  if (f.name && f.name !== editing.value.name) patch.name = f.name.trim()
  if (f.kind && f.kind !== editing.value.kind) patch.kind = f.kind
  if ((f.parent_id || null) !== (editing.value.parent_id || null)) patch.parent_id = f.parent_id || null
  if ((f.note || '') !== (editing.value.note || '')) patch.note = f.note

  // Geometry-related fields from the shared editor.
  const curGeo = editing.value.geometry || {}
  const wantLevels = +f.levelTrio.levels || 0
  const wantLevel = +f.levelTrio.level || 0
  const wantSlot = +f.levelTrio.slot || 0
  const wantY = ((+f.levelTrio.mount_y_mm) || 0) / 1000
  const wantW = Math.max(0.05, (+f.w_mm || 0) / 1000)
  const wantH = Math.max(0.05, (+f.h_mm || 0) / 1000)
  const wantD = Math.max(0.05, (+f.d_mm || 0) / 1000)
  const wantRot = +f.rot || 0
  const cur = effectiveGeometry(editing.value)
  const geomChanged =
    wantLevels !== (cur.levels || 0) ||
    wantLevel !== (cur.level || 0) ||
    wantSlot !== (cur.slot || 0) ||
    Math.abs(wantY - (+curGeo.y || 0)) > 1e-6 ||
    Math.abs(wantW - cur.w) > 1e-6 ||
    Math.abs(wantH - cur.h) > 1e-6 ||
    Math.abs(wantD - cur.d) > 1e-6 ||
    Math.abs(wantRot - cur.rot) > 1e-6
  if (geomChanged) {
    patch.geometry = {
      ...curGeo,
      x: cur.x, z: cur.z, color: cur.color,
      y: wantY,
      w: wantW, h: wantH, d: wantD, rot: wantRot,
      levels: wantLevels,
      level: wantLevel,
      slot: wantSlot,
    }
  }
  if (Object.keys(patch).length === 0) { closeEdit(); return }
  await api.updateLocation(editing.value.id, patch)
  await load(); emit('changed')
  closeEdit()
}

async function deleteEdit() {
  if (!editing.value) return
  const node = editing.value
  const direct = (childrenOf.value.get(node.id) || []).length
  const cnt = descendantCount(node.id)
  const msg = `确认删除 "${node.name}" ?\n` +
    (direct > 0 ? `它包含 ${direct} 个子文件夹,会被提升到上级\n` : '') +
    (cnt > 0 ? `内部 ${cnt} 件物品的位置将变为 "未指定"` : '')
  if (!confirm(msg)) return
  await api.deleteLocation(node.id)
  await load(); emit('changed')
  closeEdit()
}

function enter(node) { cwdId.value = node.id }
function up() {
  if (cwdId.value === null) return
  const node = byId.value.get(cwdId.value)
  cwdId.value = node?.parent_id ?? null
}
</script>

<template>
  <div class="space-y-3">
    <!-- Breadcrumb + actions -->
    <div class="card p-3 flex items-center gap-2 flex-wrap">
      <button class="btn btn-secondary text-xs" :disabled="cwdId === null" @click="up">⬆ 上级</button>
      <nav class="flex items-center text-sm flex-1 flex-wrap gap-1">
        <button class="hover:underline text-slate-700" @click="cwdId = null">📁 根目录</button>
        <template v-for="b in breadcrumbs" :key="b.id">
          <span class="text-slate-400">/</span>
          <button class="hover:underline" @click="cwdId = b.id">
            {{ KIND_ICON[b.kind] || '📁' }} {{ b.name }}
          </button>
        </template>
      </nav>
      <button class="btn btn-primary" @click="createFolder">+ 新建{{ cwdId === null ? '房间' : '子文件夹' }}</button>
    </div>

    <!-- Empty state -->
    <div v-if="!cwdChildren.length && !cwdItems.length" class="card p-8 text-center text-slate-400">
      <div class="text-3xl mb-2">📂</div>
      <div>这里还是空的</div>
      <div class="text-xs mt-1">点击右上"新建"添加文件夹,或在物品页指派物品到这里</div>
    </div>

    <!-- Subfolders grid -->
    <div v-if="cwdChildren.length" class="card p-3">
      <div class="label mb-2">子文件夹 ({{ cwdChildren.length }})</div>
      <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-2">
        <div v-for="c in cwdChildren" :key="c.id"
             class="relative border border-slate-200 rounded-lg p-3 hover:bg-slate-50 hover:border-slate-300 transition">
          <!-- Always-visible big edit button (top-right) -->
          <button class="absolute top-1.5 right-1.5 w-9 h-9 rounded-full bg-white border border-slate-200 hover:bg-slate-100 hover:border-slate-400 shadow-sm flex items-center justify-center text-base z-10"
                  title="编辑属性"
                  @click.stop="openEdit(c)">⚙</button>
          <!-- Whole card is the navigation target -->
          <button class="block w-full cursor-pointer" @click="enter(c)" @dblclick="enter(c)">
            <div class="text-3xl text-center pt-1">{{ KIND_ICON[c.kind] || '📁' }}</div>
            <div class="text-sm text-center font-medium truncate mt-1 px-1">{{ c.name }}</div>
            <div class="text-xs text-center text-slate-400 mt-0.5">
              {{ descendantCount(c.id) }} 件 · {{ (childrenOf.get(c.id) || []).length }} 子
            </div>
          </button>
        </div>
      </div>
    </div>

    <!-- Items in this folder -->
    <div v-if="cwdItems.length" class="card p-3">
      <div class="label mb-2">本文件夹直接存放的物品 ({{ cwdItems.length }})</div>
      <ul class="divide-y divide-slate-100">
        <li v-for="it in cwdItems" :key="it.id" class="py-2 flex items-center gap-3 text-sm">
          <span>📎</span>
          <span class="font-medium">{{ it.name }}</span>
          <span v-if="it.aliases" class="text-xs text-slate-400">({{ it.aliases }})</span>
          <span class="ml-auto font-mono text-slate-500">×{{ it.quantity }}</span>
        </li>
      </ul>
    </div>

    <!-- Edit modal -->
    <div v-if="editing && editForm" class="fixed inset-0 z-30 flex items-center justify-center p-4"
         @click.self="closeEdit">
      <div class="absolute inset-0 bg-black/40" @click="closeEdit"></div>
      <div class="relative card p-5 w-full max-w-lg space-y-3">
        <div class="flex items-center justify-between">
          <div class="font-semibold text-base">
            {{ KIND_ICON[editing.kind] || '📁' }} 编辑属性
            <span class="text-xs text-slate-500 ml-2 font-normal">{{ editing.full_path }}</span>
          </div>
          <button class="text-slate-400 hover:text-slate-700 text-lg leading-none" @click="closeEdit">✕</button>
        </div>
        <div class="space-y-3">
          <div>
            <label class="label">UUID (导入 CSV 时复制此值到 container_uuid 列)</label>
            <div class="flex gap-1">
              <input :value="editing.uuid" readonly class="input font-mono text-xs bg-slate-50" />
              <button class="btn btn-secondary text-xs" type="button" @click="copyUuid">📋 复制</button>
            </div>
          </div>
          <div class="grid grid-cols-2 gap-2">
            <div>
              <label class="label">名称</label>
              <input v-model="editForm.name" class="input" />
            </div>
            <div>
              <label class="label">类型</label>
              <select v-model="editForm.kind" class="input">
                <option v-for="o in KIND_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
              </select>
            </div>
          </div>
          <div>
            <label class="label">所在父级</label>
            <select v-model="editForm.parent_id" class="input">
              <option :value="null">📁 根目录</option>
              <option v-for="t in moveTargets" :key="t.id" :value="t.id">{{ t.full_path }}</option>
            </select>
            <div class="text-xs text-slate-500 mt-1">不能移到自己的子目录(已自动过滤)。</div>
          </div>
          <!-- Container dimensions (mm). Edit here directly so users don't have to
               flip to the 3D page just to resize a 收纳箱 / cabinet. -->
          <div class="grid grid-cols-4 gap-2">
            <div>
              <label class="label">宽 w (mm)</label>
              <input v-model.number="editForm.w_mm" type="number" step="10" min="50" class="input" />
            </div>
            <div>
              <label class="label">高 h (mm)</label>
              <input v-model.number="editForm.h_mm" type="number" step="10" min="50" class="input" />
            </div>
            <div>
              <label class="label">深 d (mm)</label>
              <input v-model.number="editForm.d_mm" type="number" step="10" min="50" class="input" />
            </div>
            <div>
              <label class="label">朝向°</label>
              <input v-model.number="editForm.rot" type="number" step="5" class="input" />
            </div>
          </div>

          <LevelSlotFields v-model="editForm.levelTrio"
                           :selected-loc="editing"
                           :parent-loc="editParent"
                           :all-locations="locations"
                           compact />

          <div>
            <label class="label">备注</label>
            <textarea v-model="editForm.note" class="input" rows="2" placeholder="可选"></textarea>
          </div>
        </div>
        <div class="flex justify-between items-center pt-2 border-t border-slate-100">
          <button class="btn btn-danger" @click="deleteEdit">🗑 删除</button>
          <div class="flex gap-2">
            <button class="btn btn-secondary" @click="closeEdit">取消</button>
            <button class="btn btn-primary" @click="saveEdit">保存</button>
          </div>
        </div>
        <div class="text-xs text-slate-400">
          想改 3D 尺寸/层数/位置请到 "🏗 3D" 页;这里只管文件夹结构。
        </div>
      </div>
    </div>
  </div>
</template>
