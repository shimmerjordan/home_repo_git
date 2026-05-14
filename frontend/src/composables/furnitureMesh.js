// Pretty 3D furniture meshes — replaces the default "single box" look for
// non-container kinds (bed, sofa, chair, tv, toilet, plant, etc.) with a grouped
// composition that's instantly recognisable from a few meters away.
//
// Each builder returns a THREE.Group sized so that:
//   - its local origin is at the box-centre (matching the default BoxGeometry frame)
//   - the bounding extent is approximately w × h × d
//   - userData on the root is left blank; the caller (Scene3D) attaches type:location
//
// Container kinds (shelf, cabinet, wardrobe, drawer, box, desk, fridge, washer,
// table) stay as transparent boxes so nested items remain visible. We only style
// "solid" furniture here.
import * as THREE from 'three'

const _matCache = new Map()
function mat(key, opts) {
  if (_matCache.has(key)) return _matCache.get(key)
  const m = new THREE.MeshStandardMaterial(opts)
  _matCache.set(key, m)
  return m
}

function box(w, h, d, color, opts = {}) {
  const m = new THREE.Mesh(
    new THREE.BoxGeometry(w, h, d),
    new THREE.MeshStandardMaterial({
      color: new THREE.Color(color),
      roughness: opts.roughness ?? 0.7,
      metalness: opts.metalness ?? 0.05,
    }),
  )
  if (opts.castShadow !== false) m.castShadow = true
  if (opts.receiveShadow !== false) m.receiveShadow = true
  return m
}

function cyl(radTop, radBot, h, color, segs = 16, opts = {}) {
  const m = new THREE.Mesh(
    new THREE.CylinderGeometry(radTop, radBot, h, segs),
    new THREE.MeshStandardMaterial({
      color: new THREE.Color(color),
      roughness: opts.roughness ?? 0.65,
      metalness: opts.metalness ?? 0.05,
    }),
  )
  m.castShadow = true
  m.receiveShadow = true
  return m
}

function sphere(rad, color, opts = {}) {
  const m = new THREE.Mesh(
    new THREE.SphereGeometry(rad, 18, 12),
    new THREE.MeshStandardMaterial({
      color: new THREE.Color(color),
      roughness: opts.roughness ?? 0.55,
      metalness: opts.metalness ?? 0.05,
    }),
  )
  m.castShadow = true
  m.receiveShadow = true
  return m
}

// --- Per-kind builders. Coordinates are local to the group; y=0 is the centre,
// so the floor of the group is at y=-h/2. Most furniture sits on the floor of its
// container, which is at y=-h/2 in local space. ---

function buildBed(geo) {
  const g = new THREE.Group()
  const { w, h, d, color } = geo
  const frameH = Math.min(0.18, h * 0.35)
  const mattressH = h - frameH - 0.04
  // Frame.
  const frame = box(w, frameH, d, '#7c4a1f')
  frame.position.y = -h / 2 + frameH / 2
  g.add(frame)
  // Mattress.
  const mat = box(w - 0.05, mattressH, d - 0.05, color)
  mat.position.y = -h / 2 + frameH + mattressH / 2
  g.add(mat)
  // Pillow (at -d/2 = headboard side).
  const pillow = box(w * 0.7, 0.06, d * 0.18, '#fafafa')
  pillow.position.set(0, mat.position.y + mattressH / 2 + 0.03, -d / 2 + d * 0.15)
  g.add(pillow)
  // Headboard.
  const head = box(w, h * 0.7, 0.05, '#5b3617')
  head.position.set(0, -h / 2 + (h * 0.7) / 2 + frameH * 0.5, -d / 2 - 0.025)
  g.add(head)
  return g
}

function buildSofa(geo) {
  const g = new THREE.Group()
  const { w, h, d, color } = geo
  const seatH = h * 0.45
  // Base.
  const base = box(w, seatH, d, color)
  base.position.y = -h / 2 + seatH / 2
  g.add(base)
  // Backrest.
  const backH = h - seatH
  const back = box(w, backH, d * 0.25, color)
  back.position.set(0, -h / 2 + seatH + backH / 2, -d / 2 + (d * 0.25) / 2)
  g.add(back)
  // Armrests.
  const armW = w * 0.08
  for (const sign of [-1, 1]) {
    const arm = box(armW, h * 0.7, d, color)
    arm.position.set(sign * (w / 2 - armW / 2), -h / 2 + (h * 0.7) / 2, 0)
    g.add(arm)
  }
  // Cushion seams (decorative thin slabs).
  const cushionN = Math.max(2, Math.round(w / 0.7))
  const cushionW = (w - armW * 2 - 0.04) / cushionN
  const cushionD = d * 0.7
  for (let i = 0; i < cushionN; i++) {
    const cu = box(cushionW - 0.02, 0.06, cushionD, '#ffffff')
    cu.material.color = new THREE.Color(color).lerp(new THREE.Color('#ffffff'), 0.25)
    cu.position.set(-w / 2 + armW + cushionW * (i + 0.5), -h / 2 + seatH + 0.03, d * 0.05)
    g.add(cu)
  }
  return g
}

function buildChair(geo) {
  const g = new THREE.Group()
  const { w, h, d, color } = geo
  const seatY = -h / 2 + h * 0.5
  const seat = box(w * 0.95, 0.04, d * 0.95, color)
  seat.position.y = seatY
  g.add(seat)
  // Backrest.
  const back = box(w * 0.95, h * 0.5, 0.04, color)
  back.position.set(0, seatY + (h * 0.5) / 2, -d / 2 + 0.02)
  g.add(back)
  // 4 legs.
  const legH = h * 0.5 - 0.02
  const off = 0.04
  for (const sx of [-1, 1]) for (const sz of [-1, 1]) {
    const leg = box(0.04, legH, 0.04, '#2b2b2b')
    leg.position.set(sx * (w / 2 - off), seatY - legH / 2 - 0.02, sz * (d / 2 - off))
    g.add(leg)
  }
  return g
}

function buildTable(geo, isDesk = false) {
  const g = new THREE.Group()
  const { w, h, d, color } = geo
  const topThick = 0.04
  const top = box(w, topThick, d, color)
  top.position.y = h / 2 - topThick / 2
  g.add(top)
  // 4 legs.
  const legH = h - topThick
  const off = 0.04
  for (const sx of [-1, 1]) for (const sz of [-1, 1]) {
    const leg = box(0.05, legH, 0.05, '#3b2410')
    leg.position.set(sx * (w / 2 - off - 0.025), -h / 2 + legH / 2, sz * (d / 2 - off - 0.025))
    g.add(leg)
  }
  if (isDesk) {
    // Desk: add a small back panel + maybe a single side drawer.
    const drawerW = Math.min(0.36, w * 0.35)
    const drawerH = h * 0.3
    const drawer = box(drawerW, drawerH, d * 0.55, color)
    drawer.position.set(w / 2 - drawerW / 2 - 0.03, h / 2 - topThick - drawerH / 2 - 0.02, 0)
    g.add(drawer)
    const handle = box(drawerW * 0.45, 0.02, 0.02, '#6b7280')
    handle.position.set(drawer.position.x, drawer.position.y, d * 0.55 / 2 + 0.012)
    g.add(handle)
  }
  return g
}

function buildFridge(geo) {
  const g = new THREE.Group()
  const { w, h, d, color } = geo
  // Main body — slightly translucent so contents show through when "show items
  // in room" is toggled. Looks like a frosted-glass-door fridge.
  const body = box(w, h, d, color)
  body.material.transparent = true
  body.material.opacity = 0.45
  body.material.depthWrite = false
  body.position.y = 0
  g.add(body)
  // Slight darker outline (front face thin slab) to imply doors.
  const splitY = h * 0.25 - h / 2  // ~25% from top
  const splitOff = h * 0.25
  const seam = box(w + 0.005, 0.01, 0.005, '#94a3b8')
  seam.position.set(0, h / 2 - splitOff, d / 2 + 0.003)
  g.add(seam)
  // Two vertical chrome handles (top freezer door + bottom door).
  for (const part of [
    { yc: h / 2 - splitOff / 2, hh: splitOff * 0.7 },
    { yc: -splitOff / 2, hh: (h - splitOff) * 0.7 },
  ]) {
    const handle = box(0.03, part.hh, 0.03, '#cbd5e1', { metalness: 0.7, roughness: 0.25 })
    handle.position.set(-w / 2 + 0.06, part.yc, d / 2 + 0.015)
    g.add(handle)
  }
  return g
}

function buildToilet(geo) {
  const g = new THREE.Group()
  const { w, h, d } = geo
  // Tank at the back.
  const tankH = h * 0.55
  const tankD = d * 0.28
  const tank = box(w * 0.85, tankH, tankD, '#ffffff')
  tank.position.set(0, -h / 2 + tankH / 2, -d / 2 + tankD / 2)
  g.add(tank)
  // Bowl (cylinder at the front).
  const bowl = cyl(w * 0.42, w * 0.38, h * 0.45, '#ffffff', 20)
  bowl.position.set(0, -h / 2 + (h * 0.45) / 2, d * 0.05)
  g.add(bowl)
  // Seat ring (flat torus-ish: just a thin ring cylinder).
  const seat = cyl(w * 0.45, w * 0.45, 0.03, '#f3f4f6', 20)
  seat.position.set(0, -h / 2 + h * 0.45 + 0.015, d * 0.05)
  g.add(seat)
  return g
}

function buildSink(geo) {
  const g = new THREE.Group()
  const { w, h, d, color } = geo
  // Cabinet under.
  const cabH = h * 0.7
  const cab = box(w, cabH, d, '#cbd5e1')
  cab.position.y = -h / 2 + cabH / 2
  g.add(cab)
  // Basin counter.
  const counter = box(w + 0.02, 0.04, d + 0.02, color)
  counter.position.y = -h / 2 + cabH + 0.02
  g.add(counter)
  // Basin inset (darker square).
  const basin = box(w * 0.6, 0.02, d * 0.6, '#94a3b8')
  basin.position.y = counter.position.y + 0.025
  g.add(basin)
  // Faucet.
  const faucet = cyl(0.015, 0.015, 0.18, '#94a3b8', 10, { metalness: 0.7, roughness: 0.2 })
  faucet.position.set(0, counter.position.y + 0.11, -d / 2 + 0.06)
  g.add(faucet)
  return g
}

function buildBathtub(geo) {
  const g = new THREE.Group()
  const { w, h, d } = geo
  // Outer tub.
  const outer = box(w, h, d, '#ffffff')
  g.add(outer)
  // Inner darker recess to suggest interior.
  const inner = box(w * 0.9, 0.04, d * 0.85, '#e0f2fe')
  inner.position.y = h / 2 - 0.04
  g.add(inner)
  return g
}

function buildTV(geo) {
  const g = new THREE.Group()
  const { w, h, d } = geo
  // Screen face (black panel).
  const screen = box(w, h * 0.85, Math.max(0.04, d), '#0f172a')
  screen.position.y = h * 0.075
  g.add(screen)
  // Inner glow plane (lighter front face).
  const glow = box(w - 0.06, h * 0.78, 0.005, '#1f2937')
  glow.position.set(0, h * 0.075, d / 2 + 0.005)
  g.add(glow)
  // Stand.
  const standW = w * 0.3
  const stand = box(standW, h * 0.1, d * 1.6, '#1e293b')
  stand.position.y = -h / 2 + (h * 0.05)
  g.add(stand)
  return g
}

function buildPlant(geo) {
  const g = new THREE.Group()
  const { w, h, d } = geo
  const potH = h * 0.35
  const pot = cyl(w * 0.42, w * 0.32, potH, '#92400e', 16)
  pot.position.y = -h / 2 + potH / 2
  g.add(pot)
  // Foliage cluster: a few overlapping spheres.
  const leafR = Math.min(w, d) * 0.4
  const centers = [
    [0, -h / 2 + potH + leafR * 0.7, 0],
    [w * 0.18, -h / 2 + potH + leafR * 1.1, w * 0.08],
    [-w * 0.18, -h / 2 + potH + leafR * 1.0, -w * 0.08],
    [0, -h / 2 + potH + leafR * 1.6, 0],
  ]
  for (const [x, y, z] of centers) {
    const s = sphere(leafR * 0.85, '#16a34a', { roughness: 0.85 })
    s.position.set(x, y, z)
    g.add(s)
  }
  return g
}

function buildStove(geo) {
  const g = new THREE.Group()
  const { w, h, d, color } = geo
  const body = box(w, h, d, color)
  g.add(body)
  // 4 burners on top (dark rings).
  for (const sx of [-1, 1]) for (const sz of [-1, 1]) {
    const burner = cyl(w * 0.13, w * 0.13, 0.02, '#1f2937', 18)
    burner.position.set(sx * (w * 0.22), h / 2 + 0.01, sz * (d * 0.22))
    g.add(burner)
  }
  return g
}

function buildMicrowave(geo) {
  const g = new THREE.Group()
  const { w, h, d, color } = geo
  const body = box(w, h, d, color)
  g.add(body)
  // Door window (lighter rectangle on front).
  const win = box(w * 0.6, h * 0.65, 0.005, '#475569')
  win.position.set(-w * 0.1, 0, d / 2 + 0.003)
  g.add(win)
  // Control panel.
  const panel = box(w * 0.25, h * 0.85, 0.005, '#64748b')
  panel.position.set(w * 0.34, 0, d / 2 + 0.003)
  g.add(panel)
  return g
}

function buildAC(geo) {
  const g = new THREE.Group()
  const { w, h, d, color } = geo
  const body = box(w, h, d, color)
  g.add(body)
  // Vent slats.
  const slatN = 5
  for (let i = 0; i < slatN; i++) {
    const slat = box(w * 0.85, 0.01, 0.005, '#94a3b8')
    slat.position.set(0, -h * 0.2 + i * (h * 0.08), d / 2 + 0.004)
    g.add(slat)
  }
  return g
}

const BUILDERS = {
  bed: buildBed,
  sofa: buildSofa,
  chair: buildChair,
  table: buildTable,
  desk: (geo) => buildTable(geo, true),
  toilet: buildToilet,
  sink: buildSink,
  bathtub: buildBathtub,
  tv: buildTV,
  plant: buildPlant,
  stove: buildStove,
  microwave: buildMicrowave,
  ac: buildAC,
  // Fridge: we DO style it, but with a translucent body so the user can still see
  // items inside when "show items in room" is toggled. See buildFridge above.
  fridge: buildFridge,
}

// Returns a Group, or null if this kind should fall back to the default transparent box.
// `geo` must have { w, h, d, color, levels }. The caller handles rotation/position.
export function buildPrettyFurniture(kind, geo) {
  const builder = BUILDERS[kind]
  if (!builder) return null
  return builder(geo)
}

export function hasPrettyMesh(kind) {
  return !!BUILDERS[kind]
}
