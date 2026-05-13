<script setup>
// Sims-like 2D top-down editor — uses POINTER events with setPointerCapture so
// it works correctly on iPad / touch devices.
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { api } from '../api'
import {
  FURNITURE_CATALOG, catalogFor, effectiveGeometry, buildWorldMap, defaultChildY,
  polygonBBox, pointInPolygon, snapPolygonStep,
  cleanPolygon, polygonIsSelfIntersecting,
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
// Polygon-room building state. While the user is clicking vertices, we store
// world-coord points here. Closing (clicking near the first vertex or pressing
// Enter) commits a new room with a `polygon` geometry.
const polyPoints = ref([])

// Snap the next polygon vertex to axis-aligned / parallel-to-prev-edge directions.
// Reference edges = ALL committed edges of the current poly + the most recent edge
// (so right-angle turns and alignment with the first wall come for free).
function polyRefEdges() {
  const refs = []
  for (let i = 1; i < polyPoints.value.length; i++) {
    const [a, b] = [polyPoints.value[i - 1], polyPoints.value[i]]
    refs.push([b[0] - a[0], b[1] - a[1]])
  }
  return refs
}
const polyCursorSnap = computed(() => {
  if (tool.value !== 'room-poly' || !polyPoints.value.length) return null
  const last = polyPoints.value[polyPoints.value.length - 1]
  return snapPolygonStep(last, [mouse.value.x, mouse.value.z], polyRefEdges(), 7, GRID_STEP)
})

// Vertex-edit drag state for an EXISTING selected polygon room.
const vertexDrag = ref(null)        // { locId, idx, snappedPt, hint }

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
      polygon: info.geo.polygon || null,
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

// Hit-test all shapes at a point. Returns the topmost (smallest area) match.
// `kindFilter` can be 'room', 'container', 'any'. Polygon rooms use point-in-polygon.
function findShapeAt(x, z, kindFilter = 'any') {
  const candidates = []
  for (const r of renderables.value) {
    if (kindFilter === 'room' && !r.isRoom) continue
    if (kindFilter === 'container') {
      const c = catalogFor(r.kind)
      if (!c?.container) continue
    }
    // Translate + un-rotate into the shape's local frame.
    let lx = x - r.x, lz = z - r.z
    if (r.rot) {
      const a = -r.rot * Math.PI / 180
      const cs = Math.cos(a), sn = Math.sin(a)
      ;[lx, lz] = [lx * cs - lz * sn, lx * sn + lz * cs]
    }
    let hit = false
    if (r.polygon) {
      hit = pointInPolygon(lx, lz, r.polygon)
    } else {
      hit = Math.abs(lx) <= r.w / 2 && Math.abs(lz) <= r.d / 2
    }
    if (hit) candidates.push({ r, area: r.w * r.d })
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

// Create a polygon room from a list of world-space vertices. Stores the polygon
// relative to the bounding-box center so the room's anchor is the center.
async function createPolygonRoom(worldPoints) {
  if (!worldPoints || worldPoints.length < 3) return
  // Drop accidental near-duplicates and collinear vertices the snap may have produced.
  const cleaned = cleanPolygon(worldPoints, 0.05, 0.015)
  if (cleaned.length < 3) {
    alert('多边形顶点太少, 请至少 3 个不同的点')
    return
  }
  if (polygonIsSelfIntersecting(cleaned)) {
    if (!confirm('检测到边交叉(自相交), 渲染可能不正确。\n确认仍然创建吗?')) return
  }
  const cat = catalogFor('room')
  const bb = polygonBBox(cleaned)
  if (!bb || bb.w < 0.5 || bb.d < 0.5) return
  const poly = cleaned.map(([x, z]) => [
    Math.round((x - bb.cx) * 1000) / 1000,
    Math.round((z - bb.cz) * 1000) / 1000,
  ])
  const geometry = {
    x: bb.cx, y: 0, z: bb.cz,
    w: bb.w, h: cat.h, d: bb.d,
    rot: 0, color: cat.color, levels: 0,
    polygon: poly,
  }
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

  // Auto-fit dimensions to the parent's available slot — keeps a nested 收纳箱
  // from poking out of the cabinet/box it's inside. Honours the smaller of the
  // catalog default and the available space (so we never blow it UP, only shrink).
  let fitW = cat.w, fitH = cat.h, fitD = cat.d
  if (parentGeo) {
    const margin = 0.04                                                       // 4 cm clearance per side
    const slotH = parentGeo.levels >= 2
      ? Math.max(0.04, parentGeo.h / parentGeo.levels - 0.04)                 // one layer's height
      : Math.max(0.05, parentGeo.h - margin)
    fitW = Math.min(cat.w, Math.max(0.05, parentGeo.w - margin))
    fitD = Math.min(cat.d, Math.max(0.05, parentGeo.d - margin))
    fitH = Math.min(cat.h, slotH)
  }

  const geometry = {
    x: localX, y, z: localZ,
    w: fitW, h: fitH, d: fitD, rot: 0, color: cat.color,
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

  // IMPORTANT: spread the raw stored geometry first so custom fields (e.g. `polygon`
  // for non-rectangular rooms) are preserved across move/rotate operations. Without
  // the spread, dragging an L-shaped room would re-save it as a plain rectangle.
  const geo = {
    ...(loc.geometry || {}),
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

// User clicked an edge of a polygon room → prompt for a new length (mm) and
// reflow the polygon. The chosen edge is rescaled along its own direction and
// every vertex AFTER it is shifted by the same delta, so the rest of the room
// keeps its shape — only the picked wall expands/contracts.
function editEdgeLength(r, idx) {
  const poly = r.polygon
  if (!poly) return
  const n = poly.length
  const a = poly[idx]
  const b = poly[(idx + 1) % n]
  const dx = b[0] - a[0], dz = b[1] - a[1]
  const oldLen = Math.hypot(dx, dz)
  if (oldLen < 1e-6) return
  const ans = prompt(`第 ${idx + 1} 条边长度 (mm):`, String(Math.round(oldLen * 1000)))
  if (ans == null) return
  const newLen = parseFloat(ans) / 1000
  if (!Number.isFinite(newLen) || newLen <= 0.05) {
    alert('长度需 ≥ 50 mm')
    return
  }
  const ux = dx / oldLen, uz = dz / oldLen
  const newB = [
    Math.round((a[0] + ux * newLen) * 1000) / 1000,
    Math.round((a[1] + uz * newLen) * 1000) / 1000,
  ]
  // Shift indices (idx+1) … (idx+n-1) mod n  by the delta, so everything after
  // the moved endpoint slides as a rigid block (preserves the rest of the shape).
  // The original vertex `a` (index = idx) is NOT shifted (it is the pivot).
  const dxShift = newB[0] - b[0]
  const dzShift = newB[1] - b[1]
  const shift = new Set()
  for (let k = 1; k < n; k++) shift.add((idx + k) % n)
  const newPoly = poly.map((p, i) =>
    shift.has(i)
      ? [Math.round((p[0] + dxShift) * 1000) / 1000,
         Math.round((p[1] + dzShift) * 1000) / 1000]
      : [p[0], p[1]]
  )
  const loc = props.locations.find((l) => l.id === r.id)
  if (loc) persistPolygonEdit(loc, newPoly)
}

// Persist a polygon vertex change. The polygon is stored relative to the room's
// current bbox-center anchor; after editing we re-center so the anchor still sits
// at the centroid (otherwise dragging would visually shift the whole room).
async function persistPolygonEdit(loc, newLocalPoly) {
  // Clean degenerate vertices first (snap + edge-length-edit can produce them).
  const cleaned = cleanPolygon(newLocalPoly, 0.05, 0.015)
  if (cleaned.length < 3) return
  if (polygonIsSelfIntersecting(cleaned)) {
    if (!confirm('当前修改导致边交叉。继续保存吗?')) return
  }
  const bb = polygonBBox(cleaned)
  if (!bb) return
  const recentered = cleaned.map(([x, z]) => [
    Math.round((x - bb.cx) * 1000) / 1000,
    Math.round((z - bb.cz) * 1000) / 1000,
  ])
  const oldGeo = loc.geometry || {}
  const newGeo = {
    ...oldGeo,
    x: (+oldGeo.x || 0) + bb.cx,
    z: (+oldGeo.z || 0) + bb.cz,
    w: bb.w, d: bb.d,
    polygon: recentered,
  }
  await props.applyEdit({
    kind: 'update', id: loc.id,
    patch: { geometry: newGeo }, before: props.snapshot(loc),
  })
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

  // Polygon-edge click → prompt to edit that edge's length.
  if (role === 'poly-edge' && locId) {
    const idx = +handleEl.dataset.eIdx
    const r = renderables.value.find((x) => x.id === locId)
    if (r) editEdgeLength(r, idx)
    return
  }

  // Polygon-vertex drag (only available when the polygon room is currently selected).
  if (role === 'poly-vertex' && locId) {
    const idx = +handleEl.dataset.vIdx
    const r = renderables.value.find((x) => x.id === locId)
    if (r && r.polygon) {
      drag.value = {
        type: 'poly-vertex', locId, idx,
        // Local copy of polygon we mutate during drag.
        local: r.polygon.map((pt) => [pt[0], pt[1]]),
        roomCx: r.x, roomCz: r.z,
      }
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
  } else if (tool.value === 'room-poly') {
    // Use the auto-aligned cursor preview when available; otherwise just snap to
    // the 5 cm grid. This locks new edges to horizontal / vertical / parallel /
    // perpendicular relative to the existing walls.
    let nx = Math.round(p.x / GRID_STEP) * GRID_STEP
    let nz = Math.round(p.z / GRID_STEP) * GRID_STEP
    if (polyCursorSnap.value) {
      nx = polyCursorSnap.value.p[0]
      nz = polyCursorSnap.value.p[1]
    }
    if (polyPoints.value.length >= 3) {
      const [fx, fz] = polyPoints.value[0]
      const closeTol = vb.value.w * 0.02
      if (Math.hypot(nx - fx, nz - fz) <= closeTol) {
        finishPolygon()
        return
      }
    }
    polyPoints.value = [...polyPoints.value, [nx, nz]]
  } else if (catalogFor(tool.value)) {
    placeFurniture(tool.value, p.x, p.z)
    tool.value = 'select'
  }
}

function finishPolygon() {
  const pts = polyPoints.value
  polyPoints.value = []
  if (pts.length >= 3) createPolygonRoom(pts)
  tool.value = 'select'
}

function cancelPolygon() {
  polyPoints.value = []
  tool.value = 'select'
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
  } else if (drag.value.type === 'poly-vertex') {
    // Cursor in polygon-local coords (room is at roomCx/roomCz, no rotation).
    const localCursor = [p.x - drag.value.roomCx, p.z - drag.value.roomCz]
    const i = drag.value.idx
    const local = drag.value.local
    // Reference edges = the two neighboring edges (i-1→i and i→i+1) plus
    // axis-alignment via the default refs in snapPolygonStep.
    const prev = local[(i - 1 + local.length) % local.length]
    const next = local[(i + 1) % local.length]
    const refs = []
    // Edge from prev to (current vertex) — direction tells us "the wall continues like…"
    refs.push([next[0] - prev[0], next[1] - prev[1]])
    // Snap relative to PREV neighbour (so the i-1 → i edge aligns).
    const snapA = snapPolygonStep(prev, localCursor, refs, 7, GRID_STEP)
    // ALSO try snapping relative to NEXT neighbour (so the i → i+1 edge aligns).
    const snapB = snapPolygonStep(next, localCursor, refs, 7, GRID_STEP)
    // Pick whichever finished closer to the raw cursor (or whichever locked an angle).
    let chosen = snapA
    if (snapB.snappedAngle != null && snapA.snappedAngle == null) chosen = snapB
    else if (snapA.snappedAngle != null && snapB.snappedAngle != null) {
      const da = Math.hypot(snapA.p[0] - localCursor[0], snapA.p[1] - localCursor[1])
      const db = Math.hypot(snapB.p[0] - localCursor[0], snapB.p[1] - localCursor[1])
      chosen = db < da ? snapB : snapA
    }
    local[i] = [chosen.p[0], chosen.p[1]]
    drag.value.snappedAngle = chosen.snappedAngle
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
  } else if (d.type === 'poly-vertex') {
    const loc = props.locations.find((l) => l.id === d.locId)
    if (loc) await persistPolygonEdit(loc, d.local)
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
  if (ev.key === 'Escape') {
    if (polyPoints.value.length) { cancelPolygon(); return }
    emit('select', null); tool.value = 'select'; drag.value = null
  } else if (ev.key === 'Enter' && tool.value === 'room-poly' && polyPoints.value.length >= 3) {
    finishPolygon()
  } else if (ev.key === 'Backspace' && tool.value === 'room-poly' && polyPoints.value.length) {
    polyPoints.value = polyPoints.value.slice(0, -1)
  } else if ((ev.key === 'Delete' || ev.key === 'Backspace') && props.selectedId) {
    deleteSelected()
  } else if (ev.key === 'r' || ev.key === 'R') {
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

// While poly-vertex dragging, give callers the live polygon points.
function liveRenderPolygon(r) {
  if (drag.value?.type === 'poly-vertex' && drag.value.locId === r.id) {
    return drag.value.local
  }
  return r.polygon
}

// Distance between two 2D points in metres (used for edge-length labels).
function edgeLen(a, b) {
  return Math.hypot(b[0] - a[0], b[1] - a[1])
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
    <div class="card p-1.5 flex items-center gap-1 overflow-x-auto no-scrollbar" style="touch-action: pan-x">
      <button :class="['btn text-xs flex-shrink-0', tool==='select' ? 'btn-primary' : 'btn-secondary']"
              title="选择 / 移动 / 平移画布"
              @click="tool='select'">↖<span class="hidden sm:inline ml-1">选择</span></button>
      <button :class="['btn text-xs flex-shrink-0', tool==='room-poly' ? 'btn-primary' : 'btn-secondary']"
              title="多边形房间: 点击逐个加顶点, 点回起点 / 按 Enter 闭合"
              @click="tool='room-poly'; polyPoints = []">🔷<span class="hidden md:inline ml-1">多边形</span></button>
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
        <template v-if="tool==='room'">📐 拖拽画矩形房间</template>
        <template v-else-if="tool==='room-poly'">
          🔷 点击逐个加顶点 ({{ polyPoints.length }} 点) · 点回起点 / 按 Enter 闭合 · Esc 取消 · Backspace 撤一点
        </template>
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
          <!-- Shape body. Polygon for non-rectangular rooms, rect otherwise.
               IMPORTANT: <polygon v-if> and <rect v-else> MUST be siblings with
               nothing in between, otherwise Vue pairs the v-else with the wrong
               v-if and the bbox rect ends up rendering on top of the polygon. -->
          <polygon v-if="r.polygon"
                   :points="liveRenderPolygon(r).map(p => p[0] + ',' + p[1]).join(' ')"
                   :fill="r.color"
                   :fill-opacity="0.15"
                   fill-rule="evenodd"
                   :stroke="r.color"
                   :stroke-width="0.08"
                   stroke-linejoin="miter"
                   data-role="shape" :data-loc-id="r.id" />
          <rect v-else :x="-r.w/2" :y="-r.d/2" :width="r.w" :height="r.d"
                :fill="r.color"
                :fill-opacity="r.isRoom ? 0.15 : 0.65"
                :stroke="r.color"
                :stroke-width="r.isRoom ? 0.08 : 0.04"
                data-role="shape" :data-loc-id="r.id" />

          <!-- Vertex handles + edge clickable overlays + length labels: only for the SELECTED polygon room. -->
          <template v-if="r.polygon && selectedId === r.id">
            <!-- Invisible thick line on each edge captures clicks for length editing. -->
            <line v-for="(pt, i) in liveRenderPolygon(r)" :key="'eh' + i"
                  :x1="pt[0]" :y1="pt[1]"
                  :x2="liveRenderPolygon(r)[(i + 1) % liveRenderPolygon(r).length][0]"
                  :y2="liveRenderPolygon(r)[(i + 1) % liveRenderPolygon(r).length][1]"
                  stroke="rgba(0,0,0,0)" stroke-width="0.30"
                  style="cursor: pointer"
                  data-role="poly-edge" :data-loc-id="r.id" :data-e-idx="i" />
            <g v-for="(pt, i) in liveRenderPolygon(r)" :key="'vh' + i">
              <circle :cx="pt[0]" :cy="pt[1]" r="0.15"
                      fill="#facc15" stroke="#1e293b" stroke-width="0.04"
                      style="cursor: grab"
                      data-role="poly-vertex"
                      :data-loc-id="r.id" :data-v-idx="i" />
            </g>
            <g v-for="(pt, i) in liveRenderPolygon(r)" :key="'el' + i">
              <text :x="(pt[0] + liveRenderPolygon(r)[(i + 1) % liveRenderPolygon(r).length][0]) / 2"
                    :y="(pt[1] + liveRenderPolygon(r)[(i + 1) % liveRenderPolygon(r).length][1]) / 2 - 0.08"
                    text-anchor="middle" font-size="0.18" fill="#1e293b"
                    style="paint-order: stroke; stroke: #fff; stroke-width: 0.06; cursor: pointer"
                    data-role="poly-edge" :data-loc-id="r.id" :data-e-idx="i">
                {{ (edgeLen(pt, liveRenderPolygon(r)[(i + 1) % liveRenderPolygon(r).length]) * 1000).toFixed(0) }} mm
              </text>
            </g>
            <!-- Snap-locked indicator: highlight the vertex pink while the angle is locked. -->
            <circle v-if="drag?.type === 'poly-vertex' && drag.locId === r.id && drag.snappedAngle != null"
                    :cx="liveRenderPolygon(r)[drag.idx][0]" :cy="liveRenderPolygon(r)[drag.idx][1]"
                    r="0.22" fill="none" stroke="#ec4899" stroke-width="0.04"
                    stroke-dasharray="0.08 0.06" pointer-events="none" />
          </template>
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
          <!-- Selection. Polygon shapes get an outline tracing their actual contour;
               rectangular shapes get the classic dashed bbox. The rotation handle
               always sits above the bbox top-edge. -->
          <template v-if="selectedId === r.id">
            <polygon v-if="r.polygon"
                     :points="liveRenderPolygon(r).map(p => p[0] + ',' + p[1]).join(' ')"
                     fill="none" stroke="#facc15" stroke-width="0.06"
                     stroke-dasharray="0.18 0.12" pointer-events="none" />
            <rect v-else :x="-r.w/2 - 0.06" :y="-r.d/2 - 0.06" :width="r.w + 0.12" :height="r.d + 0.12"
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

        <!-- In-progress polygon: committed edges, snapped preview line + alignment hint. -->
        <template v-if="tool === 'room-poly' && polyPoints.length">
          <polyline :points="polyPoints.map(p => p[0] + ',' + p[1]).join(' ')"
                    fill="none" stroke="#0ea5e9" stroke-width="0.05"
                    stroke-linecap="round" stroke-linejoin="round" pointer-events="none" />
          <!-- Preview line uses the SNAPPED cursor when alignment is active. -->
          <line :x1="polyPoints[polyPoints.length-1][0]" :y1="polyPoints[polyPoints.length-1][1]"
                :x2="polyCursorSnap ? polyCursorSnap.p[0] : mouse.x"
                :y2="polyCursorSnap ? polyCursorSnap.p[1] : mouse.z"
                :stroke="polyCursorSnap?.snappedAngle != null ? '#ec4899' : '#0ea5e9'"
                stroke-width="0.04" stroke-dasharray="0.12 0.08"
                pointer-events="none" />
          <!-- Snapped target dot (pink when angle locked) -->
          <circle v-if="polyCursorSnap && polyCursorSnap.snappedAngle != null"
                  :cx="polyCursorSnap.p[0]" :cy="polyCursorSnap.p[1]" r="0.08"
                  fill="#ec4899" pointer-events="none" />
          <circle v-for="(p, i) in polyPoints" :key="'v'+i"
                  :cx="p[0]" :cy="p[1]" r="0.10"
                  :fill="i === 0 ? '#22c55e' : '#0ea5e9'"
                  pointer-events="none" />
          <circle v-if="polyPoints.length >= 3"
                  :cx="polyPoints[0][0]" :cy="polyPoints[0][1]" r="0.18"
                  fill="none" stroke="#22c55e" stroke-width="0.04"
                  stroke-dasharray="0.08 0.06" pointer-events="none" />
        </template>

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
