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

export function effectiveGeometry(loc) {
  const d = defaultsFor(loc.kind)
  const g = loc.geometry || {}
  return {
    x: +g.x || 0,
    y: +g.y || 0,
    z: +g.z || 0,
    w: +g.w || d.w,
    h: +g.h || d.h,
    d: +g.d || d.d,
    rot: +g.rot || 0,
    color: g.color || d.color,
    levels: Number.isFinite(+g.levels) ? +g.levels : (d.levels || 0),
    level: +g.level || 0,
    slot: +g.slot || 0,        // 1-based ordering within (parent, level); 0 = free placement
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
