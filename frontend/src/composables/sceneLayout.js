// Geometry helpers shared by Scene3D, PlanEditor, and BuildingPanel.
// Each location has a geometry { x, y, z, w, h, d, rot, color, levels?, level? } in meters.
// - x, y, z are RELATIVE to the parent's anchor (bottom-center of the parent box).
// - For containers with `levels > 1` (shelves, cabinets), the children specify `level`
//   and we compute their y from the level instead of their stored y.

// `container=true` → can host children (items or nested storage)
// `levels`          → default shelf count (0 = single compartment)
// `placement`       → where children sit by default:
//                     'inside' — children sit on the parent's interior bottom (y=0)
//                     'top'    — children stack on top of the parent (y=parent.h)
export const FURNITURE_CATALOG = [
  { kind: 'room',     label: '房间',   icon: '🏠', w: 4.0,  h: 2.7,  d: 4.0,  color: '#94a3b8', container: true,  isRoom: true, levels: 0, placement: 'inside' },
  { kind: 'shelf',    label: '书架',   icon: '📚', w: 1.0,  h: 1.8,  d: 0.30, color: '#a78bfa', container: true,  levels: 4, placement: 'inside' },
  { kind: 'cabinet',  label: '柜子',   icon: '🗄', w: 1.2,  h: 2.0,  d: 0.50, color: '#7c3aed', container: true,  levels: 3, placement: 'inside' },
  { kind: 'wardrobe', label: '衣柜',   icon: '👔', w: 1.5,  h: 2.2,  d: 0.60, color: '#6366f1', container: true,  levels: 2, placement: 'inside' },
  { kind: 'drawer',   label: '抽屉柜', icon: '🗃', w: 1.0,  h: 0.8,  d: 0.45, color: '#f59e0b', container: true,  levels: 3, placement: 'inside' },
  { kind: 'box',      label: '收纳箱', icon: '📦', w: 0.5,  h: 0.4,  d: 0.40, color: '#10b981', container: true,  levels: 0, placement: 'inside' },
  { kind: 'desk',     label: '书桌',   icon: '🖥', w: 1.2,  h: 0.75, d: 0.60, color: '#92400e', container: true,  levels: 0, placement: 'top' },
  { kind: 'table',    label: '桌子',   icon: '🍽', w: 1.4,  h: 0.75, d: 0.9,  color: '#a16207', container: true,  levels: 0, placement: 'top' },
  { kind: 'bed',      label: '床',     icon: '🛏', w: 1.5,  h: 0.5,  d: 2.0,  color: '#fb923c', container: false, levels: 0, placement: 'top' },
  { kind: 'sofa',     label: '沙发',   icon: '🛋', w: 2.0,  h: 0.85, d: 0.9,  color: '#ef4444', container: false, levels: 0, placement: 'top' },
  { kind: 'chair',    label: '椅子',   icon: '🪑', w: 0.5,  h: 0.9,  d: 0.5,  color: '#dc2626', container: false, levels: 0, placement: 'top' },
  { kind: 'plant',    label: '盆栽',   icon: '🪴', w: 0.4,  h: 0.8,  d: 0.4,  color: '#16a34a', container: false, levels: 0, placement: 'top' },
  // ---- Appliances / fixtures ----
  { kind: 'fridge',   label: '冰箱',   icon: '🧊', w: 0.7,  h: 1.85, d: 0.7,  color: '#e2e8f0', container: true,  levels: 4, placement: 'inside' },
  { kind: 'washer',   label: '洗衣机', icon: '🧺', w: 0.6,  h: 0.85, d: 0.6,  color: '#cbd5e1', container: true,  levels: 0, placement: 'inside' },
  { kind: 'stove',    label: '燃气灶', icon: '🔥', w: 0.7,  h: 0.10, d: 0.55, color: '#1e293b', container: false, levels: 0, placement: 'top' },
  { kind: 'sink',     label: '洗手台', icon: '🚰', w: 0.6,  h: 0.85, d: 0.5,  color: '#dbeafe', container: false, levels: 0, placement: 'inside' },
  { kind: 'toilet',   label: '坐便器', icon: '🚽', w: 0.4,  h: 0.75, d: 0.7,  color: '#f8fafc', container: false, levels: 0, placement: 'inside' },
  { kind: 'bathtub',  label: '浴缸',   icon: '🛁', w: 1.7,  h: 0.55, d: 0.75, color: '#e0f2fe', container: false, levels: 0, placement: 'inside' },
  { kind: 'shower',   label: '淋浴',   icon: '🚿', w: 0.9,  h: 2.0,  d: 0.9,  color: '#bae6fd', container: false, levels: 0, placement: 'inside' },
  { kind: 'tv',       label: '电视',   icon: '📺', w: 1.3,  h: 0.80, d: 0.10, color: '#0f172a', container: false, levels: 0, placement: 'top' },
  { kind: 'ac',       label: '空调',   icon: '❄️', w: 0.85, h: 0.30, d: 0.20, color: '#f1f5f9', container: false, levels: 0, placement: 'inside' },
  { kind: 'microwave',label: '微波炉', icon: '🍱', w: 0.5,  h: 0.30, d: 0.40, color: '#475569', container: false, levels: 0, placement: 'top' },
]

export const KIND_DEFAULTS = Object.fromEntries(
  FURNITURE_CATALOG.map((c) => [c.kind, { w: c.w, h: c.h, d: c.d, color: c.color, levels: c.levels || 0 }])
)
KIND_DEFAULTS.other = { w: 0.6, h: 0.6, d: 0.6, color: '#64748b', levels: 0 }

export function catalogFor(kind) {
  return FURNITURE_CATALOG.find((c) => c.kind === kind) || null
}

export function defaultsFor(kind) {
  return KIND_DEFAULTS[kind] || KIND_DEFAULTS.other
}

// Best-effort detection of mobile / touch / low-power devices so we can default
// to a "low quality" 3D mode (no shadows, no per-room lights). Users can flip the
// switch in the 3D viewer's toolbar if they want pretty visuals.
export function isLowEndDevice() {
  if (typeof window === 'undefined') return false
  const ua = (navigator.userAgent || '') + ''
  if (/iPad|iPhone|iPod|Android/i.test(ua)) return true
  // iPadOS 13+ reports as MacIntel + multi-touch.
  if (navigator.platform === 'MacIntel' && (navigator.maxTouchPoints || 0) > 1) return true
  // Generic coarse-pointer touch device.
  try {
    if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) return true
  } catch {}
  return false
}

// Polygon helpers — used for non-rectangular rooms. Polygon points are [x, z]
// in metres, RELATIVE to the room's geometry.x / geometry.z anchor.
export function polygonBBox(poly) {
  if (!poly || poly.length === 0) return null
  let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity
  for (const p of poly) {
    const x = +p[0], z = +p[1]
    if (x < minX) minX = x
    if (x > maxX) maxX = x
    if (z < minZ) minZ = z
    if (z > maxZ) maxZ = z
  }
  return { minX, maxX, minZ, maxZ, w: maxX - minX, d: maxZ - minZ,
           cx: (minX + maxX) / 2, cz: (minZ + maxZ) / 2 }
}

// Snap a 2D direction to the nearest reference angle (in degrees) within tolerance.
// `refs` defaults to horizontal/vertical (axis-aligned). Returns the snapped angle in
// degrees, or null if none of the references is within tolerance.
export function snapAngleDeg(angleDeg, refs = [0, 90, 180, -90], tolDeg = 7) {
  let best = null, bestDiff = tolDeg + 1
  for (const r of refs) {
    const diff = Math.abs(((angleDeg - r + 540) % 360) - 180)
    if (diff < bestDiff) { bestDiff = diff; best = r }
  }
  return bestDiff <= tolDeg ? best : null
}

// Given a starting point and a raw cursor, snap the direction of the segment
// to horizontal/vertical or to angles parallel/perpendicular to any reference
// edge. Length is rounded to the grid. Returns `{ p, snappedAngle, refUsed }`
// where `p` is the snapped end point and `snappedAngle` is null when no rule fired.
export function snapPolygonStep(prevPt, cursor, refEdges = [], tolDeg = 7, gridStep = 0.05) {
  if (!prevPt) return { p: cursor, snappedAngle: null, refUsed: null }
  const dx = cursor[0] - prevPt[0]
  const dz = cursor[1] - prevPt[1]
  const len = Math.hypot(dx, dz)
  if (len < 0.04) return { p: cursor, snappedAngle: null, refUsed: null }
  const ang = Math.atan2(dz, dx) * 180 / Math.PI

  // Build the candidate angle set: axis-aligned + parallel/perpendicular to refs.
  const refs = [0, 90, 180, -90]
  for (const e of refEdges) {
    const a = Math.atan2(e[1], e[0]) * 180 / Math.PI
    refs.push(a, a + 90, a - 90, a + 180)
  }
  const snapped = snapAngleDeg(ang, refs, tolDeg)

  const finalAng = snapped == null ? ang : snapped
  // Round length to grid for clean numbers (e.g. 3.05 m).
  const snapLen = Math.max(gridStep, Math.round(len / gridStep) * gridStep)
  const r = finalAng * Math.PI / 180
  return {
    p: [prevPt[0] + Math.cos(r) * snapLen, prevPt[1] + Math.sin(r) * snapLen],
    snappedAngle: snapped,
    refUsed: snapped == null ? null : (Math.abs(snapped) === 90 || snapped === 0 || Math.abs(snapped) === 180 ? 'axis' : 'parallel'),
  }
}

// Drop vertices that are within `minDistM` of the previous one (typo / double-tap)
// and vertices that are collinear with their neighbours (within `collinearTolM`).
export function cleanPolygon(pts, minDistM = 0.05, collinearTolM = 0.01) {
  if (!pts || pts.length < 3) return pts || []
  // Pass 1: drop near-duplicate consecutive points.
  const passOne = []
  for (const p of pts) {
    const prev = passOne[passOne.length - 1]
    if (!prev || Math.hypot(p[0] - prev[0], p[1] - prev[1]) >= minDistM) {
      passOne.push([p[0], p[1]])
    }
  }
  // Wrap-around dedupe between last and first.
  while (passOne.length >= 2) {
    const a = passOne[0], b = passOne[passOne.length - 1]
    if (Math.hypot(a[0] - b[0], a[1] - b[1]) < minDistM) passOne.pop()
    else break
  }
  if (passOne.length < 3) return passOne
  // Pass 2: drop vertices that lie on the line between their neighbours.
  const passTwo = []
  const n = passOne.length
  for (let i = 0; i < n; i++) {
    const prev = passOne[(i - 1 + n) % n]
    const cur = passOne[i]
    const next = passOne[(i + 1) % n]
    const dx = next[0] - prev[0], dz = next[1] - prev[1]
    const len = Math.hypot(dx, dz)
    let perp = 0
    if (len > 1e-9) {
      perp = Math.abs(dx * (prev[1] - cur[1]) - (prev[0] - cur[0]) * dz) / len
    }
    if (perp >= collinearTolM) passTwo.push(cur)
  }
  return passTwo.length >= 3 ? passTwo : passOne
}

// Signed area; positive = CCW (in screen-y-down space, which is our (x, z) plane).
export function polygonSignedArea(poly) {
  if (!poly || poly.length < 3) return 0
  let s = 0
  for (let i = 0, n = poly.length; i < n; i++) {
    const [ax, az] = poly[i]
    const [bx, bz] = poly[(i + 1) % n]
    s += ax * bz - bx * az
  }
  return s / 2
}

// Returns true if any non-adjacent edges of `poly` cross. Used as a sanity check
// after the user finishes drawing — if the polygon is self-intersecting, we warn.
export function polygonIsSelfIntersecting(poly) {
  if (!poly || poly.length < 4) return false
  const n = poly.length
  function segsCross(a1, a2, b1, b2) {
    const d = (a2[0] - a1[0]) * (b2[1] - b1[1]) - (a2[1] - a1[1]) * (b2[0] - b1[0])
    if (Math.abs(d) < 1e-9) return false
    const t = ((b1[0] - a1[0]) * (b2[1] - b1[1]) - (b1[1] - a1[1]) * (b2[0] - b1[0])) / d
    const u = ((b1[0] - a1[0]) * (a2[1] - a1[1]) - (b1[1] - a1[1]) * (a2[0] - a1[0])) / d
    return t > 1e-6 && t < 1 - 1e-6 && u > 1e-6 && u < 1 - 1e-6
  }
  for (let i = 0; i < n; i++) {
    const a1 = poly[i], a2 = poly[(i + 1) % n]
    for (let j = i + 2; j < n; j++) {
      // Skip the pair that share a vertex (i's last edge wraps to first).
      if (i === 0 && j === n - 1) continue
      const b1 = poly[j], b2 = poly[(j + 1) % n]
      if (segsCross(a1, a2, b1, b2)) return true
    }
  }
  return false
}

// Standard ray-casting point-in-polygon test. (px, pz) and poly are in the
// SAME coordinate frame (caller is responsible for un-rotating if needed).
export function pointInPolygon(px, pz, poly) {
  if (!poly || poly.length < 3) return false
  let inside = false
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const xi = +poly[i][0], zi = +poly[i][1]
    const xj = +poly[j][0], zj = +poly[j][1]
    const intersect = ((zi > pz) !== (zj > pz))
      && (px < (xj - xi) * (pz - zi) / ((zj - zi) || 1e-9) + xi)
    if (intersect) inside = !inside
  }
  return inside
}

export function effectiveGeometry(loc) {
  const def = defaultsFor(loc.kind)
  const g = loc.geometry || {}
  const poly = Array.isArray(g.polygon) && g.polygon.length >= 3 ? g.polygon : null
  let w = +g.w || def.w
  let d = +g.d || def.d
  if (poly) {
    const bb = polygonBBox(poly)
    if (bb) { w = bb.w || w; d = bb.d || d }
  }
  return {
    x: +g.x || 0,
    y: +g.y || 0,
    z: +g.z || 0,
    w,
    h: +g.h || def.h,
    d,
    rot: +g.rot || 0,
    color: g.color || def.color,
    levels: Number.isFinite(+g.levels) ? +g.levels : (def.levels || 0),
    level: +g.level || 0,
    slot: +g.slot || 0,
    polygon: poly,
    _set: !!loc.geometry,
  }
}

// Total width of children sharing the same parent + level (including those without slot).
export function levelUsage(locations, parentId, level) {
  const kids = locations.filter((l) => l.parent_id === parentId && (+l.geometry?.level || 0) === level)
  let used = 0
  for (const k of kids) used += effectiveGeometry(k).w
  return { used, count: kids.length, kids }
}

// y of a child's anchor (= bottom of its box) given its parent's height/levels and its own level.
// level is 1-based; level 1 = bottom shelf surface.
export function levelY(parentH, levels, level) {
  if (!levels || levels < 2 || !level) return 0
  const layerH = parentH / levels
  return (level - 1) * layerH
}

// Default y for a NEW child being placed inside `parentLoc`. Caller passes optional `level`
// when the parent is multi-level. Returns the y to write into the new geometry.
export function defaultChildY(parentLoc, level = 0) {
  if (!parentLoc) return 0
  const cat = catalogFor(parentLoc.kind)
  const pg = effectiveGeometry(parentLoc)
  if (pg.levels >= 2 && level) return levelY(pg.h, pg.levels, level)
  if (cat?.placement === 'top') return pg.h          // stack on top of desk/table
  return 0                                            // inside (room floor or container bottom)
}

// Walk parent chain → world coords. `info.y` is the child's anchor (bottom of its box),
// so Scene3D should add geo.h / 2 to render the mesh center.
export function buildWorldMap(locations) {
  const byId = new Map(locations.map((l) => [l.id, l]))
  const result = new Map()

  function compute(id) {
    if (result.has(id)) return result.get(id)
    const loc = byId.get(id)
    if (!loc) return null
    const g = effectiveGeometry(loc)
    const parent = loc.parent_id ? compute(loc.parent_id) : null
    const base = parent ? parent : { x: 0, y: 0, z: 0, geo: null }

    let xOff = g.x
    let yOff = g.y

    if (parent?.geo) {
      // Multi-level parent → override y from level
      if (parent.geo.levels >= 2 && g.level) {
        yOff = levelY(parent.geo.h, parent.geo.levels, g.level)

        // If this child has a slot, pack siblings on same layer left → right by slot.
        if (g.slot) {
          const sibs = locations
            .filter((l) => l.parent_id === loc.parent_id
              && (+l.geometry?.level || 0) === g.level
              && (+l.geometry?.slot || 0) > 0)
            .sort((a, b) => (+a.geometry.slot) - (+b.geometry.slot))
          let cursor = -parent.geo.w / 2
          for (const s of sibs) {
            const sg = effectiveGeometry(s)
            if (s.id === loc.id) {
              xOff = cursor + sg.w / 2
              break
            }
            cursor += sg.w
          }
        }
      }
    }

    const out = {
      x: base.x + xOff,
      y: base.y + yOff,
      z: base.z + g.z,
      geo: g,
      loc,
    }
    result.set(id, out)
    return out
  }
  for (const l of locations) compute(l.id)
  return result
}

// Auto-arrange unset geometries (only used when user clicks the "auto-arrange" button).
export function autoArrange(locations) {
  const childrenOf = new Map()
  for (const l of locations) {
    const k = l.parent_id || 0
    if (!childrenOf.has(k)) childrenOf.set(k, [])
    childrenOf.get(k).push(l)
  }
  const updates = []
  function placeChildren(parentId, parentGeo) {
    const kids = childrenOf.get(parentId || 0) || []
    kids.sort((a, b) => a.id - b.id)
    const cols = Math.ceil(Math.sqrt(kids.length)) || 1
    const cellW = parentGeo ? parentGeo.w / cols : 6
    const cellD = parentGeo ? parentGeo.d / cols : 6
    kids.forEach((k, i) => {
      const g = effectiveGeometry(k)
      const col = i % cols
      const row = Math.floor(i / cols)
      const startX = parentGeo ? -parentGeo.w / 2 + cellW / 2 : -((cols - 1) * cellW) / 2
      const startZ = parentGeo ? -parentGeo.d / 2 + cellD / 2 : -((cols - 1) * cellD) / 2
      const newGeo = {
        x: startX + col * cellW, y: 0, z: startZ + row * cellD,
        w: g.w, h: g.h, d: g.d, rot: 0, color: g.color,
        levels: g.levels, level: g.level,
      }
      if (!k.geometry || !k.geometry.w) updates.push({ id: k.id, geometry: newGeo })
      placeChildren(k.id, newGeo)
    })
  }
  placeChildren(0, null)
  return updates
}
