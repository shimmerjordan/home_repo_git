<script setup>
// Sims-like 2D top-down editor — uses POINTER events with setPointerCapture so
// it works correctly on iPad / touch devices.
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { api } from '../api'
import {
  FURNITURE_CATALOG, catalogFor, effectiveGeometry, buildWorldMap, defaultChildY,
} from '../composables/sceneLayout'

const SNAP_TOL = 0.12     // metres — snap distance for edge/center alignment
const GRID_STEP = 0.05    // 5 cm grid as fallback

const props = defineProps({
  locations: { type: Array, default: () => [] },
  items: { type: Array, default: () => [] },
  selectedId: { type: Number, default: null },
  // Centralised edit pipeline so undo can record every change.
  applyEdit: { type: Function, required: true },
  snapshot: { type: Function, required: true },
})
const emit = defineEmits(['changed', 'select'])

const tool = ref('select')
const drag = ref(null)
const mouse = ref({ x: 0, z: 0 })
const svgRef = ref(null)
const vb = ref({ x: -10, z: -10, w: 20, d: 20 })
const snapGuides = ref([])

const worldMap = computed(() => buildWorldMap(props.locations))

const renderables = computed(() => {
  const arr = []
  for (const [id, info] of worldMap.value) {
    const cat = catalogFor(info.loc.kind)
    arr.push({
      id, loc: info.loc, kind: info.loc.kind,
      icon: cat?.icon || '📍', label: info.loc.name,
      x: info.x, z: info.z, worldY: info.y,
      w: info.geo.w, d: info.geo.d, h: info.geo.h,
      rot: info.geo.rot || 0,
      color: info.geo.color,
      isRoom: !!cat?.isRoom,
      levels: info.geo.levels,
      level: info.geo.level,
      parentId: info.loc.parent_id,
    })
  }
  // Rooms first, then by world y so stacked items render on top of their hosts.
  arr.sort((a, b) => {
    if (a.isRoom !== b.isRoom) return a.isRoom ? -1 : 1
    if (a.worldY !== b.worldY) return a.worldY - b.worldY
    return a.id - b.id
  })
  return arr
})

const itemCountByLoc = computed(() => {
  const childrenOf = new Map()
  for (const l of props.locations) {
    const k = l.parent_id || 0
    if (!childrenOf.has(k)) childrenOf.set(k, [])
    childrenOf.get(k).push(l.id)
  }
  const direct = {}
  for (const it of props.items) if (it.location_id) direct[it.location_id] = (direct[it.location_id] || 0) + 1
  function sum(id) {
    let s = direct[id] || 0
    for (const c of childrenOf.get(id) || []) s += sum(c)
    return s
  }
  const out = {}
  for (const l of props.locations) out[l.id] = sum(l.id)
  return out
})

function svgPoint(ev) {
  if (!svgRef.value) return { x: 0, z: 0 }
  const pt = svgRef.value.createSVGPoint()
  pt.x = ev.clientX; pt.y = ev.clientY
  const m = svgRef.value.getScreenCTM()
  if (!m) return { x: 0, z: 0 }
  const p = pt.matrixTransform(m.inverse())
  return { x: p.x, z: p.y }
}

// Hit-test all rectangles at a point. Returns the topmost (smallest area) match.
// `kindFilter` can be 'room', 'container', 'any'.
function findShapeAt(x, z, kindFilter = 'any') {
  const candidates = []
  for (const r of renderables.value) {
    if (kindFilter === 'room' && !r.isRoom) continue
    if (kindFilter === 'container') {
      const c = catalogFor(r.kind)
      if (!c?.container) continue
    }
    let lx = x - r.x, lz = z - r.z
    if (r.rot) {
      const a = -r.rot * Math.PI / 180
      const cs = Math.cos(a), sn = Math.sin(a)
      ;[lx, lz] = [lx * cs - lz * sn, lx * sn + lz * cs]
    }
    if (Math.abs(lx) <= r.w / 2 && Math.abs(lz) <= r.d / 2) {
      candidates.push({ r, area: r.w * r.d })
    }
  }
  if (!candidates.length) return null
  candidates.sort((a, b) => a.area - b.area)
  return candidates[0].r
}

function autoName(kind, parentId) {
  const cat = catalogFor(kind)
  const base = cat?.label || '物件'
  const n = props.locations.filter((l) => l.kind === kind && (l.parent_id || null) === (parentId || null)).length
  return `${base} ${n + 1}`
}

// Snap a moved/created shape's center (x,z) to nearby grid lines and to
// other shapes' edges/centers. Returns {x, z, guides[]} where guides describe the
// alignment lines to render as feedback.
function computeSnap(movingId, x, z, w, d) {
  const guides = []
  let bestX = null, bestDX = SNAP_TOL
  let bestZ = null, bestDZ = SNAP_TOL

  const myL = x - w / 2, myR = x + w / 2, myCx = x
  const myT = z - d / 2, myB = z + d / 2, myCz = z

  for (const other of renderables.value) {
    if (other.id === movingId) continue
    if (other.rot !== 0) continue          // skip rotated; alignment math messy
    const oL = other.x - other.w / 2, oR = other.x + other.w / 2, oCx = other.x
    const oT = other.z - other.d / 2, oB = other.z + other.d / 2, oCz = other.z

    // X candidates: (my-edge or center, other-edge or center, resulting center x)
    const xs = [
      [myL, oL, x + (oL - myL),  oL,    'edge'],
      [myL, oR, x + (oR - myL),  oR,    'edge'],
      [myR, oR, x + (oR - myR),  oR,    'edge'],
      [myR, oL, x + (oL - myR),  oL,    'edge'],
      [myCx, oCx, x + (oCx - myCx), oCx, 'center'],
    ]
    for (const [mv, ov, newCx, axisX] of xs) {
      const d_ = Math.abs(mv - ov)
      if (d_ < bestDX) {
        bestDX = d_
        bestX = newCx
        guides.push({ axis: 'x', at: axisX, kind: 'snap-pending' })
      }
    }
    const zs = [
      [myT, oT, z + (oT - myT),  oT,    'edge'],
      [myT, oB, z + (oB - myT),  oB,    'edge'],
      [myB, oB, z + (oB - myB),  oB,    'edge'],
      [myB, oT, z + (oT - myB),  oT,    'edge'],
      [myCz, oCz, z + (oCz - myCz), oCz, 'center'],
    ]
    for (const [mv, ov, newCz, axisZ] of zs) {
      const d_ = Math.abs(mv - ov)
      if (d_ < bestDZ) {
        bestDZ = d_
        bestZ = newCz
        guides.push({ axis: 'z', at: axisZ, kind: 'snap-pending' })
      }
    }
  }

  // Apply best snap; otherwise round to grid.
  const finalX = bestX !== null ? bestX : Math.round(x / GRID_STEP) * GRID_STEP
  const finalZ = bestZ !== null ? bestZ : Math.round(z / GRID_STEP) * GRID_STEP
  // Active guides: only those matching the chosen alignment.
  const activeGuides = []
  if (bestX !== null) {
    const axisX = guides.filter((g) => g.axis === 'x').slice(-1)[0]?.at
    if (axisX != null) activeGuides.push({ axis: 'x', at: axisX })
  }
  if (bestZ !== null) {
    const axisZ = guides.filter((g) => g.axis === 'z').slice(-1)[0]?.at
    if (axisZ != null) activeGuides.push({ axis: 'z', at: axisZ })
  }
  return { x: finalX, z: finalZ, guides: activeGuides }
}

// ---- Edit ops (go through applyEdit so undo captures them) ----
async function createRoom(x, z, w, d) {
  const cat = catalogFor('room')
  const geometry = { x, y: 0, z, w, h: cat.h, d, rot: 0, color: cat.color, levels: 0 }
  const loc = await props.applyEdit({
    kind: 'create',
    payload: { name: autoName('room', null), kind: 'room', parent_id: null, geometry },
  })
  emit('changed')
  if (loc) emit('select', loc.id)
}

async function placeFurniture(kind, x, z) {
  const cat = catalogFor(kind)
  if (!cat) return

  // Snap to grid / siblings.
  const snapped = computeSnap(null, x, z, cat.w, cat.d)
  x = snapped.x; z = snapped.z

  // Pick parent: deepest container at click.
  const host = findShapeAt(x, z, 'container')
  const parentId = host?.id || null
  const parentLoc = parentId ? props.locations.find((l) => l.id === parentId) : null
  const parentGeo = parentLoc ? effectiveGeometry(parentLoc) : null
  const parentInfo = host ? worldMap.value.get(host.id) : null

  // Multi-level container → ask which level (skip prompt for non-box kinds; default L1).
  let level = 0
  if (parentGeo?.levels >= 2) {
    if (kind === 'box') {
      const ans = prompt(`把"${cat.label}"放在第几层?(1=底层 ~ ${parentGeo.levels}=顶层)`, '1')
      if (ans === null) return
      level = Math.max(1, Math.min(parentGeo.levels, parseInt(ans, 10) || 1))
    } else {
      level = 1
    }
  }

  const y = defaultChildY(parentLoc, level)
  const localX = parentInfo ? x - parentInfo.x : x
  const localZ = parentInfo ? z - parentInfo.z : z
  const geometry = {
    x: localX, y, z: localZ,
    w: cat.w, h: cat.h, d: cat.d, rot: 0, color: cat.color,
    levels: cat.levels || 0,
    level: level || 0,
    slot: 0,
  }
  const loc = await props.applyEdit({
    kind: 'create',
    payload: { name: autoName(kind, parentId), kind, parent_id: parentId, geometry },
  })
  emit('changed')
  if (loc) emit('select', loc.id)
}

async function persistMove(loc, newWorldX, newWorldZ, newRot) {
  // Reparent if non-room and dragged into a different container.
  let newParentId = loc.parent_id
  const cat = catalogFor(loc.kind)
  if (cat && !cat.isRoom) {
    // Don't allow self or descendants as new parent.
    const descendants = (() => {
      const set = new Set([loc.id])
      let added = true
      while (added) {
        added = false
        for (const l of props.locations) {
          if (l.parent_id && set.has(l.parent_id) && !set.has(l.id)) {
            set.add(l.id); added = true
          }
        }
      }
      return set
    })()
    const host = findShapeAt(newWorldX, newWorldZ, 'container')
    if (host && !descendants.has(host.id)) newParentId = host.id
    else if (!host) newParentId = null
  }

  const old = effectiveGeometry(loc)
  const newParentLoc = newParentId ? props.locations.find((l) => l.id === newParentId) : null
  const newParentInfo = newParentId ? worldMap.value.get(newParentId) : null
  const newParentGeo = newParentLoc ? effectiveGeometry(newParentLoc) : null

  // Recompute y based on new parent (only when reparenting to a different container).
  let newY = old.y
  let newLevel = old.level
  if (newParentId !== loc.parent_id) {
    if (newParentGeo?.levels >= 2) {
      // keep level if any (clamped); otherwise default to 1.
      newLevel = old.level || 1
      if (newLevel > newParentGeo.levels) newLevel = newParentGeo.levels
    } else {
      newLevel = 0
    }
    newY = defaultChildY(newParentLoc, newLevel)
  }

  const geo = {
    x: newParentInfo ? newWorldX - newParentInfo.x : newWorldX,
    y: newY,
    z: newParentInfo ? newWorldZ - newParentInfo.z : newWorldZ,
    w: old.w, h: old.h, d: old.d,
    rot: (newRot != null) ? newRot : old.rot,
    color: old.color,
    levels: old.levels,
    level: newLevel,
    slot: old.slot,
  }
  const patch = { geometry: geo }
  if (newParentId !== loc.parent_id) patch.parent_id = newParentId
  await props.applyEdit({ kind: 'update', id: loc.id, patch, before: props.snapshot(loc) })
  emit('changed')
}

async function deleteSelected() {
  const id = props.selectedId
  if (!id) return
  const loc = props.locations.find((l) => l.id === id)
  if (!loc) return
  if (!confirm(`删除 "${loc.name}"?`)) return
  await props.applyEdit({
    kind: 'delete',
    id,
    payload: {
      name: loc.name, kind: loc.kind, parent_id: loc.parent_id,
      note: loc.note || '', geometry: loc.geometry || null,
    },
  })
  emit('select', null)
  emit('changed')
}

// ---- Pointer event handling ----
// We use pointerdown on the SVG element, then setPointerCapture so that move/up
// events keep firing even if the cursor leaves the SVG. This works on mouse + touch.

function onPointerDown(ev) {
  if (ev.button != null && ev.button !== 0) return
  ev.preventDefault()
  const p = svgPoint(ev)
  mouse.value = p

  // Identify what was clicked via data-* attributes.
  const handleEl = ev.target.closest?.('[data-role]')
  const role = handleEl?.dataset.role
  const locId = handleEl ? +handleEl.dataset.locId : null

  if (role === 'rotate' && locId) {
    const r = renderables.value.find((x) => x.id === locId)
    if (r) {
      drag.value = { type: 'rotate', locId, cx: r.x, cz: r.z, rot: r.rot }
      svgRef.value.setPointerCapture?.(ev.pointerId)
    }
    return
  }

  if (role === 'shape' && locId && tool.value === 'select') {
    const r = renderables.value.find((x) => x.id === locId)
    if (r) {
      emit('select', locId)
      drag.value = {
        type: 'move', locId,
        offsetX: r.x - p.x, offsetZ: r.z - p.z,
        x: r.x, z: r.z,
      }
      svgRef.value.setPointerCapture?.(ev.pointerId)
    }
    return
  }

  // Background interaction.
  if (tool.value === 'select') {
    // Start a pan gesture; if user releases without moving > threshold, treat as deselect.
    drag.value = {
      type: 'pan',
      vbX: vb.value.x, vbZ: vb.value.z,
      startClientX: ev.clientX, startClientY: ev.clientY,
      moved: 0,
    }
    svgRef.value.setPointerCapture?.(ev.pointerId)
  } else if (tool.value === 'room') {
    drag.value = { type: 'create-room', startX: p.x, startZ: p.z, x: p.x, z: p.z }
    svgRef.value.setPointerCapture?.(ev.pointerId)
  } else if (catalogFor(tool.value)) {
    placeFurniture(tool.value, p.x, p.z)
    tool.value = 'select'
  }
}

function onPointerMove(ev) {
  const p = svgPoint(ev)
  mouse.value = p
  if (!drag.value) return
  if (drag.value.type === 'create-room') {
    drag.value.x = Math.round(p.x / GRID_STEP) * GRID_STEP
    drag.value.z = Math.round(p.z / GRID_STEP) * GRID_STEP
  } else if (drag.value.type === 'move') {
    const r = renderables.value.find((x) => x.id === drag.value.locId)
    const w = r?.w ?? 1, d = r?.d ?? 1
    const rawX = p.x + drag.value.offsetX
    const rawZ = p.z + drag.value.offsetZ
    const sn = computeSnap(drag.value.locId, rawX, rawZ, w, d)
    drag.value.x = sn.x; drag.value.z = sn.z
    snapGuides.value = sn.guides
  } else if (drag.value.type === 'rotate') {
    const a = Math.atan2(p.z - drag.value.cz, p.x - drag.value.cx) * 180 / Math.PI
    drag.value.rot = ((Math.round((a + 90) / 5) * 5) % 360 + 360) % 360
  } else if (drag.value.type === 'pan') {
    const dx = ev.clientX - drag.value.startClientX
    const dy = ev.clientY - drag.value.startClientY
    drag.value.moved = Math.max(drag.value.moved, Math.hypot(dx, dy))
    // Convert pixel delta to world delta via SVG screen-CTM.
    const m = svgRef.value.getScreenCTM()
    if (m && m.a) {
      vb.value = {
        x: drag.value.vbX - dx / m.a,
        z: drag.value.vbZ - dy / m.d,
        w: vb.value.w, d: vb.value.d,
      }
    }
  }
}

async function onPointerUp(ev) {
  const d = drag.value
  drag.value = null
  snapGuides.value = []
  try { svgRef.value?.releasePointerCapture?.(ev.pointerId) } catch {}
  if (!d) return
  if (d.type === 'pan') {
    if (d.moved < 4) emit('select', null)  // tap without drag = deselect
    return
  }
  if (d.type === 'create-room') {
    const w = Math.abs(d.x - d.startX)
    const dep = Math.abs(d.z - d.startZ)
    if (w > 0.6 && dep > 0.6) {
      await createRoom((d.x + d.startX) / 2, (d.z + d.startZ) / 2, w, dep)
    }
    tool.value = 'select'
  } else if (d.type === 'move') {
    const loc = props.locations.find((l) => l.id === d.locId)
    if (loc) await persistMove(loc, d.x, d.z, null)
  } else if (d.type === 'rotate') {
    const loc = props.locations.find((l) => l.id === d.locId)
    if (loc) {
      const w = worldMap.value.get(loc.id)
      await persistMove(loc, w.x, w.z, d.rot)
    }
  }
}

function onWheel(ev) {
  ev.preventDefault()
  const p = svgPoint(ev)
  const factor = ev.deltaY < 0 ? 0.85 : 1.18
  const newW = Math.max(2, Math.min(120, vb.value.w * factor))
  const newD = Math.max(2, Math.min(120, vb.value.d * factor))
  vb.value = {
    x: p.x - (p.x - vb.value.x) * (newW / vb.value.w),
    z: p.z - (p.z - vb.value.z) * (newD / vb.value.d),
    w: newW, d: newD,
  }
}

function fitAll() {
  const rooms = renderables.value.filter((r) => r.isRoom)
  if (!rooms.length) { vb.value = { x: -10, z: -10, w: 20, d: 20 }; return }
  let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity
  for (const r of rooms) {
    minX = Math.min(minX, r.x - r.w / 2); maxX = Math.max(maxX, r.x + r.w / 2)
    minZ = Math.min(minZ, r.z - r.d / 2); maxZ = Math.max(maxZ, r.z + r.d / 2)
  }
  const pad = 2
  vb.value = { x: minX - pad, z: minZ - pad, w: (maxX - minX) + pad * 2, d: (maxZ - minZ) + pad * 2 }
}

function onKey(ev) {
  const t = ev.target?.tagName
  if (t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT') return
  if (ev.key === 'Escape') { emit('select', null); tool.value = 'select'; drag.value = null }
  else if ((ev.key === 'Delete' || ev.key === 'Backspace') && props.selectedId) deleteSelected()
  else if (ev.key === 'r' || ev.key === 'R') {
    const r = renderables.value.find((x) => x.id === props.selectedId)
    if (r) {
      const loc = props.locations.find((l) => l.id === r.id)
      const w = worldMap.value.get(loc.id)
      persistMove(loc, w.x, w.z, (r.rot + 15) % 360)
    }
  }
}

onMounted(() => {
  window.addEventListener('keydown', onKey)
  watch(() => props.locations.length, (n, o) => { if (!o && n) fitAll() }, { immediate: true })
})
onBeforeUnmount(() => { window.removeEventListener('keydown', onKey) })

defineExpose({ fitAll })

// While dragging, render the moved/rotated shape at its drag position.
function liveTransform(r) {
  if (drag.value?.type === 'move' && drag.value.locId === r.id) {
    return `translate(${drag.value.x} ${drag.value.z}) rotate(${r.rot})`
  }
  if (drag.value?.type === 'rotate' && drag.value.locId === r.id) {
    return `translate(${r.x} ${r.z}) rotate(${drag.value.rot})`
  }
  return `translate(${r.x} ${r.z}) rotate(${r.rot})`
}

const ghost = computed(() => {
  if (drag.value?.type !== 'create-room') return null
  return {
    x: Math.min(drag.value.x, drag.value.startX),
    z: Math.min(drag.value.z, drag.value.startZ),
    w: Math.abs(drag.value.x - drag.value.startX),
    d: Math.abs(drag.value.z - drag.value.startZ),
  }
})
</script>

<template>
  <div class="flex flex-col gap-2">
    <!-- Toolbar: horizontal scroll, compact icons (labels hidden on narrow screens) -->
    <div class="card p-1.5 flex items-center gap-1 overflow-x-auto" style="touch-action: pan-x">
      <button :class="['btn text-xs flex-shrink-0', tool==='select' ? 'btn-primary' : 'btn-secondary']"
              title="选择 / 移动 / 平移画布"
              @click="tool='select'">↖<span class="hidden sm:inline ml-1">选择</span></button>
      <span class="w-px h-6 bg-slate-200 mx-0.5 flex-shrink-0"></span>
      <button v-for="c in FURNITURE_CATALOG" :key="c.kind"
              :class="['btn text-xs flex-shrink-0', tool===c.kind ? 'btn-primary' : 'btn-secondary']"
              :title="`${c.label} (${c.w}×${c.d}m${c.levels >= 2 ? `, ${c.levels}层` : ''})`"
              @click="tool = c.kind">
        <span class="text-base leading-none">{{ c.icon }}</span><span class="hidden md:inline ml-1">{{ c.label }}</span>
      </button>
      <span class="flex-1 min-w-2"></span>
      <button class="btn btn-secondary text-xs flex-shrink-0" @click="fitAll" title="重置视野">⤢</button>
      <button class="btn btn-danger text-xs flex-shrink-0" :disabled="!selectedId" @click="deleteSelected" title="删除选中">🗑</button>
    </div>

    <div class="text-xs text-slate-500 flex justify-between gap-2">
      <span class="truncate">
        <template v-if="tool==='room'">📐 拖拽画房间</template>
        <template v-else-if="tool!=='select'">🎯 点击放 <b>{{ catalogFor(tool)?.label }}</b></template>
        <template v-else>↖ 点物体拖移 / 空白拖动平移 / 滚轮缩放</template>
      </span>
      <span class="font-mono flex-shrink-0">{{ mouse.x.toFixed(1) }}, {{ mouse.z.toFixed(1) }} m</span>
    </div>

    <div class="card p-0 overflow-hidden bg-slate-50" style="touch-action: none">
      <svg ref="svgRef"
           :viewBox="`${vb.x} ${vb.z} ${vb.w} ${vb.d}`"
           preserveAspectRatio="xMidYMid meet"
           style="width:100%; height: clamp(360px, 60vh, 600px); user-select: none; display: block"
           :style="{ cursor: tool==='select' ? (drag?.type === 'pan' ? 'grabbing' : 'grab') : 'crosshair' }"
           @pointerdown="onPointerDown"
           @pointermove="onPointerMove"
           @pointerup="onPointerUp"
           @pointercancel="onPointerUp"
           @wheel="onWheel">
        <defs>
          <pattern id="grid1" width="1" height="1" patternUnits="userSpaceOnUse">
            <path d="M 1 0 L 0 0 0 1" fill="none" stroke="#e2e8f0" stroke-width="0.01"/>
          </pattern>
          <pattern id="grid5" width="5" height="5" patternUnits="userSpaceOnUse">
            <path d="M 5 0 L 0 0 0 5" fill="none" stroke="#cbd5e1" stroke-width="0.02"/>
          </pattern>
        </defs>
        <rect :x="vb.x" :y="vb.z" :width="vb.w" :height="vb.d" fill="url(#grid1)" pointer-events="none" />
        <rect :x="vb.x" :y="vb.z" :width="vb.w" :height="vb.d" fill="url(#grid5)" pointer-events="none" />
        <circle cx="0" cy="0" r="0.08" fill="#475569" pointer-events="none" />

        <g v-for="r in renderables" :key="r.id" :transform="liveTransform(r)">
          <!-- Shape body (clickable for select/move) -->
          <rect :x="-r.w/2" :y="-r.d/2" :width="r.w" :height="r.d"
                :fill="r.color"
                :fill-opacity="r.isRoom ? 0.15 : 0.65"
                :stroke="r.color"
                :stroke-width="r.isRoom ? 0.08 : 0.04"
                data-role="shape" :data-loc-id="r.id" />
          <!-- (layer dividers are only meaningful in 3D side view; the 2D label shows level count) -->
          <!-- Front-facing notch -->
          <line v-if="!r.isRoom" x1="0" y1="0" :x2="0" :y2="-r.d/2"
                stroke="#0f172a" stroke-width="0.04" stroke-linecap="round" pointer-events="none" />
          <!-- Label inside -->
          <text x="0" y="0" text-anchor="middle"
                :font-size="Math.min(r.w, r.d) * 0.18 + 0.12"
                fill="#0f172a" pointer-events="none" style="user-select: none">
            <tspan v-if="r.isRoom">{{ r.icon }} {{ r.label }}</tspan>
            <tspan v-else>{{ r.icon }}</tspan>
          </text>
          <!-- Label outside (for furniture) -->
          <text v-if="!r.isRoom" x="0" :y="r.d/2 + 0.18" text-anchor="middle"
                font-size="0.16" fill="#334155" pointer-events="none" style="user-select: none">
            {{ r.label }}<tspan v-if="r.levels >= 2"> · {{ r.levels }}层</tspan><tspan v-else-if="r.level"> · L{{ r.level }}</tspan>
            <tspan v-if="itemCountByLoc[r.id]" fill="#475569"> · {{ itemCountByLoc[r.id] }}件</tspan>
          </text>
          <!-- Selection -->
          <template v-if="selectedId === r.id">
            <rect :x="-r.w/2 - 0.06" :y="-r.d/2 - 0.06" :width="r.w + 0.12" :height="r.d + 0.12"
                  fill="none" stroke="#facc15" stroke-width="0.05" stroke-dasharray="0.18 0.12"
                  pointer-events="none" />
            <line x1="0" y1="0" :x2="0" :y2="-r.d/2 - 0.5" stroke="#facc15" stroke-width="0.04" pointer-events="none" />
            <circle cx="0" :cy="-r.d/2 - 0.5" r="0.18"
                    fill="#facc15" stroke="#1e293b" stroke-width="0.03"
                    style="cursor: grab"
                    data-role="rotate" :data-loc-id="r.id" />
          </template>
        </g>

        <rect v-if="ghost"
              :x="ghost.x" :y="ghost.z" :width="ghost.w" :height="ghost.d"
              fill="#94a3b8" fill-opacity="0.25"
              stroke="#475569" stroke-width="0.04" stroke-dasharray="0.2 0.1"
              pointer-events="none" />

        <!-- Snap guides while dragging -->
        <template v-for="(g, i) in snapGuides" :key="i">
          <line v-if="g.axis === 'x'" :x1="g.at" :x2="g.at" :y1="vb.z" :y2="vb.z + vb.d"
                stroke="#ec4899" stroke-width="0.03" stroke-dasharray="0.1 0.06" pointer-events="none" />
          <line v-else :x1="vb.x" :x2="vb.x + vb.w" :y1="g.at" :y2="g.at"
                stroke="#ec4899" stroke-width="0.03" stroke-dasharray="0.1 0.06" pointer-events="none" />
        </template>
      </svg>
    </div>
  </div>
</template>
