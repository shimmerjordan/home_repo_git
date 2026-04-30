<script setup>
import { ref, watch, onMounted, onBeforeUnmount, shallowRef } from 'vue'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { TransformControls } from 'three/examples/jsm/controls/TransformControls.js'
import { buildWorldMap, catalogFor, defaultChildY, levelY } from '../composables/sceneLayout'

const props = defineProps({
  locations: { type: Array, default: () => [] },
  items: { type: Array, default: () => [] },
  highlightItemId: { type: Number, default: null },
  highlightItemIds: { type: Array, default: () => [] },   // multi-target (overrides single)
  highlightLocationId: { type: Number, default: null },
  height: { type: Number, default: 480 },
  selectable: { type: Boolean, default: false },
  editable: { type: Boolean, default: false },
  selectedLocationId: { type: Number, default: null },
})
const emit = defineEmits(['select-location', 'select-item', 'transform-end'])

const container = ref(null)
const tooltip = ref({ visible: false, x: 0, y: 0, text: '' })
const transformMode = ref('translate')

let scene, camera, renderer, controls, transformControls
let raf = 0
let resizeObserver = null

const locMeshes = shallowRef(new Map())
const itemMeshes = shallowRef(new Map())
const raycaster = new THREE.Raycaster()
const pointer = new THREE.Vector2()
let pulseTween = null

function init() {
  scene = new THREE.Scene()
  // Soft sky gradient via fog + a colored background gives a warmer look than flat dark.
  scene.background = new THREE.Color(0x14213d)
  scene.fog = new THREE.Fog(0x14213d, 30, 80)

  const w = container.value.clientWidth || 600
  const h = props.height
  camera = new THREE.PerspectiveCamera(45, w / h, 0.05, 500)
  camera.position.set(8, 9, 10)

  renderer = new THREE.WebGLRenderer({ antialias: true })
  renderer.setSize(w, h)
  renderer.setPixelRatio(window.devicePixelRatio)
  renderer.shadowMap.enabled = true
  renderer.shadowMap.type = THREE.PCFSoftShadowMap
  // Modern color/tone management for richer materials.
  renderer.outputColorSpace = THREE.SRGBColorSpace
  renderer.toneMapping = THREE.ACESFilmicToneMapping
  renderer.toneMappingExposure = 1.05
  container.value.appendChild(renderer.domElement)

  controls = new OrbitControls(camera, renderer.domElement)
  controls.enableDamping = true
  controls.target.set(0, 1, 0)

  // Ambient + sky/ground hemisphere for soft fill.
  scene.add(new THREE.AmbientLight(0xffffff, 0.30))
  const hemi = new THREE.HemisphereLight(0xbfd4ff, 0x6b5a48, 0.45)
  hemi.position.set(0, 30, 0)
  scene.add(hemi)
  // Sun: directional light with shadows.
  const sun = new THREE.DirectionalLight(0xffeed5, 0.9)
  sun.position.set(18, 28, 14)
  sun.castShadow = true
  sun.shadow.mapSize.set(1024, 1024)
  sun.shadow.camera.near = 1
  sun.shadow.camera.far = 80
  sun.shadow.camera.left = -25
  sun.shadow.camera.right = 25
  sun.shadow.camera.top = 25
  sun.shadow.camera.bottom = -25
  sun.shadow.bias = -0.0005
  scene.add(sun)

  // Floor plane (catches shadows, gives a "ground" feel).
  const floorMat = new THREE.MeshStandardMaterial({
    color: 0x2c3a52, roughness: 0.95, metalness: 0,
  })
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(80, 80), floorMat)
  floor.rotation.x = -Math.PI / 2
  floor.position.y = -0.02
  floor.receiveShadow = true
  scene.add(floor)

  const grid = new THREE.GridHelper(40, 40, 0x334155, 0x1e293b)
  grid.position.y = -0.01
  scene.add(grid)

  if (props.editable) {
    transformControls = new TransformControls(camera, renderer.domElement)
    transformControls.size = 0.7
    transformControls.setSpace('world')
    scene.add(transformControls)
    transformControls.addEventListener('dragging-changed', (ev) => {
      controls.enabled = !ev.value
      if (!ev.value) commitTransform()
    })
  }

  // Pointer events on the canvas (for tooltip + click selection).
  renderer.domElement.addEventListener('pointerdown', onPointerDown)
  renderer.domElement.addEventListener('pointermove', onPointerMove)
  renderer.domElement.addEventListener('pointerleave', () => (tooltip.value.visible = false))

  resizeObserver = new ResizeObserver(() => {
    if (!container.value) return
    const ww = container.value.clientWidth
    const hh = props.height
    if (ww > 0 && hh > 0) {
      renderer.setSize(ww, hh)
      camera.aspect = ww / hh
      camera.updateProjectionMatrix()
    }
  })
  resizeObserver.observe(container.value)
}

function clearObjects() {
  for (const v of locMeshes.value.values()) {
    scene.remove(v.mesh)
    v.mesh.traverse((c) => { c.geometry?.dispose?.(); c.material?.dispose?.() })
    if (v.roomLight) scene.remove(v.roomLight)
    if (v.roomLamp) {
      scene.remove(v.roomLamp)
      v.roomLamp.geometry?.dispose()
      v.roomLamp.material?.dispose?.()
    }
  }
  for (const v of itemMeshes.value.values()) {
    scene.remove(v.mesh)
    v.mesh.geometry?.dispose()
    v.mesh.material?.dispose?.()
  }
  locMeshes.value = new Map()
  itemMeshes.value = new Map()
}

function rebuild() {
  if (!scene) return
  // Detach transform before rebuild to avoid stale references.
  if (transformControls) transformControls.detach()
  clearObjects()
  const world = buildWorldMap(props.locations || [])

  for (const [id, info] of world) {
    const { geo } = info
    const isRoom = info.loc.kind === 'room'
    const cy = info.y + geo.h / 2
    const boxGeo = new THREE.BoxGeometry(geo.w, geo.h, geo.d)
    // Very transparent shell so internal structure (shelves, contents) is visible.
    const mat = new THREE.MeshStandardMaterial({
      color: new THREE.Color(geo.color),
      transparent: true,
      opacity: isRoom ? 0.06 : 0.18,
      depthWrite: false,
      roughness: 0.7,
      metalness: 0.05,
      side: THREE.DoubleSide,
    })
    const mesh = new THREE.Mesh(boxGeo, mat)
    mesh.position.set(info.x, cy, info.z)
    mesh.rotation.y = (geo.rot || 0) * Math.PI / 180
    mesh.userData = { type: 'location', id }
    mesh.renderOrder = 1  // shell renders after opaque interior bits

    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(boxGeo),
      new THREE.LineBasicMaterial({ color: new THREE.Color(geo.color) }),
    )
    mesh.add(edges)

    // Multi-level shelf dividers as 2 cm thick opaque slabs (look like real shelves).
    if (geo.levels >= 2) {
      const shelfColor = new THREE.Color(geo.color).multiplyScalar(0.6)
      const shelfMat = new THREE.MeshStandardMaterial({
        color: shelfColor, roughness: 0.55, metalness: 0.05,
      })
      const layerH = geo.h / geo.levels
      // Render dividers between layers (i = 1..levels-1) and a back panel for visibility.
      for (let i = 1; i < geo.levels; i++) {
        const slab = new THREE.Mesh(
          new THREE.BoxGeometry(Math.max(0.04, geo.w - 0.04), 0.02, Math.max(0.04, geo.d - 0.04)),
          shelfMat,
        )
        slab.position.y = -geo.h / 2 + i * layerH
        slab.renderOrder = 0
        mesh.add(slab)
      }
      // Subtle back panel so the cabinet has a visible "back wall".
      const backMat = new THREE.MeshStandardMaterial({
        color: new THREE.Color(geo.color).multiplyScalar(0.85),
        transparent: true, opacity: 0.55, side: THREE.DoubleSide,
      })
      const back = new THREE.Mesh(new THREE.PlaneGeometry(geo.w - 0.02, geo.h - 0.02), backMat)
      back.position.set(0, 0, -geo.d / 2 + 0.005)
      back.renderOrder = 0
      mesh.add(back)
    }

    // Casts/receives shadows for solid-ish furniture (skip rooms — they're translucent shells).
    if (!isRoom) {
      mesh.castShadow = true
      mesh.receiveShadow = true
    } else {
      mesh.receiveShadow = true
    }

    scene.add(mesh)
    const slot = { mesh, world: { x: info.x, y: cy, z: info.z }, geo, locInfo: info,
                   roomLight: null, roomLamp: null }

    // Per-room ceiling light. Default off; auto-on for the active/highlighted room.
    if (isRoom) {
      const lampY = cy + geo.h / 2 - 0.08
      const light = new THREE.PointLight(0xffe8b0, 0.0,
        Math.max(geo.w, geo.d) * 1.6, 1.8)
      light.position.set(info.x, lampY, info.z)
      light.castShadow = true
      light.shadow.mapSize.set(512, 512)
      light.shadow.bias = -0.0008
      scene.add(light)

      const lampMat = new THREE.MeshBasicMaterial({ color: 0x6a6a6a })
      const lamp = new THREE.Mesh(new THREE.SphereGeometry(0.07, 12, 8), lampMat)
      lamp.position.copy(light.position)
      scene.add(lamp)

      slot.roomLight = light
      slot.roomLamp = lamp
    }
    locMeshes.value.set(id, slot)
  }

  // Render any nested locations (boxes, drawers, etc.) AFTER their parents so
  // they sit visually on top in transparent containers.
  for (const v of locMeshes.value.values()) v.mesh.renderOrder = 2 - (v.locInfo.loc.parent_id ? 0 : 1)

  // Items as small cubes inside their parent.
  const itemsByLoc = new Map()
  for (const it of props.items || []) {
    if (!it.location_id) continue
    if (!itemsByLoc.has(it.location_id)) itemsByLoc.set(it.location_id, [])
    itemsByLoc.get(it.location_id).push(it)
  }
  for (const [locId, items] of itemsByLoc) {
    const info = locMeshes.value.get(locId)
    if (!info) continue
    // Items without explicit pos go in a grid; items with pos use their pos directly.
    const auto = items.filter((it) => it.pos_x == null && it.pos_z == null)
    const manual = items.filter((it) => !(it.pos_x == null && it.pos_z == null))

    const cols = Math.ceil(Math.sqrt(Math.max(auto.length, 1)))
    const cellW = (info.geo.w - 0.1) / Math.max(cols, 1)
    const cellD = (info.geo.d - 0.1) / Math.max(cols, 1)
    const baseSize = Math.max(0.06, Math.min(cellW, cellD) * 0.55)

    function spawn(it, x, z, size) {
      const cubeGeo = new THREE.BoxGeometry(size, size, size)
      const cubeMat = new THREE.MeshStandardMaterial({
        color: new THREE.Color('#fb7185'),
        emissive: new THREE.Color('#000'), roughness: 0.6,
      })
      const cube = new THREE.Mesh(cubeGeo, cubeMat)
      cube.position.set(x, info.world.y - info.geo.h / 2 + size / 2 + 0.02, z)
      cube.userData = { type: 'item', id: it.id, name: it.name, locationId: locId }
      scene.add(cube)
      itemMeshes.value.set(it.id, { mesh: cube, baseColor: cubeMat.color.clone() })
    }

    auto.forEach((it, i) => {
      const col = i % cols, row = Math.floor(i / cols)
      spawn(it,
        info.world.x - info.geo.w / 2 + 0.05 + col * cellW + cellW / 2,
        info.world.z - info.geo.d / 2 + 0.05 + row * cellD + cellD / 2,
        baseSize)
    })
    manual.forEach((it) => {
      const size = Math.max(0.05, Math.min(0.2, info.geo.w * 0.2))
      spawn(it,
        info.world.x + (+it.pos_x || 0),
        info.world.z + (+it.pos_z || 0),
        size)
    })
  }

  fitAll(false)
  applySelection()
}

function fitAll(animate = true) {
  const positions = []
  for (const v of locMeshes.value.values()) positions.push(new THREE.Vector3(v.world.x, v.world.y, v.world.z))
  if (!positions.length) return
  const box = new THREE.Box3().setFromPoints(positions)
  const center = new THREE.Vector3(); box.getCenter(center)
  const size = new THREE.Vector3(); box.getSize(size)
  const maxDim = Math.max(size.x, size.z, 4)
  const dist = maxDim * 1.6 + 4
  const target = { x: center.x, y: 1, z: center.z }
  const camPos = { x: center.x + dist * 0.7, y: dist * 0.8, z: center.z + dist * 0.7 }
  if (animate) tweenCamera(camPos, target, 900)
  else { camera.position.set(camPos.x, camPos.y, camPos.z); controls.target.set(target.x, target.y, target.z) }
}

function loop() {
  raf = requestAnimationFrame(loop)
  controls.update()
  if (pulseTween) pulseTween()
  renderer.render(scene, camera)
}

let cameraAnim = null
function tweenCamera(camPos, target, duration = 800) {
  const startCam = camera.position.clone()
  const startTgt = controls.target.clone()
  const endCam = new THREE.Vector3(camPos.x, camPos.y, camPos.z)
  const endTgt = new THREE.Vector3(target.x, target.y, target.z)
  const t0 = performance.now()
  return new Promise((resolve) => {
    cameraAnim = () => {
      const t = Math.min(1, (performance.now() - t0) / duration)
      const e = 0.5 - 0.5 * Math.cos(Math.PI * t)
      camera.position.lerpVectors(startCam, endCam, e)
      controls.target.lerpVectors(startTgt, endTgt, e)
      if (t >= 1) { cameraAnim = null; resolve() }
    }
    const orig = pulseTween
    pulseTween = () => { orig?.(); if (cameraAnim) cameraAnim() }
  })
}

// During a highlight, fade everything not on the path to the target. Stored
// originals are restored when the timer expires (or another highlight starts).
let occlusionRestore = null
function occludeForHighlight(targetItemId) {
  occludeForMultiHighlight([targetItemId])
}

function occludeForMultiHighlight(targetItemIds) {
  if (occlusionRestore) { occlusionRestore(); occlusionRestore = null }

  // Compute which locations are on the path (any target's ancestors).
  const onPath = new Set()
  const targetSet = new Set(targetItemIds || [])
  const byId = new Map((props.locations || []).map((l) => [l.id, l]))
  for (const tid of targetSet) {
    const target = (props.items || []).find((i) => i.id === tid)
    if (!target?.location_id) continue
    let cur = target.location_id
    while (cur) { onPath.add(cur); cur = byId.get(cur)?.parent_id }
  }

  const restoreFns = []
  for (const [id, v] of locMeshes.value) {
    if (onPath.has(id)) continue
    const m = v.mesh
    // Save originals.
    const matSnap = { opacity: m.material.opacity, transparent: m.material.transparent, depthWrite: m.material.depthWrite, visible: m.visible }
    const childSnap = []
    m.children.forEach((c) => {
      if (c.material) childSnap.push({ obj: c, opacity: c.material.opacity, transparent: c.material.transparent, visible: c.visible })
    })
    // Apply ghost effect.
    m.material.opacity = 0.04
    m.material.transparent = true
    m.material.depthWrite = false
    m.children.forEach((c) => {
      if (c.material) {
        c.material.opacity = 0.04
        c.material.transparent = true
      }
    })
    restoreFns.push(() => {
      m.material.opacity = matSnap.opacity
      m.material.transparent = matSnap.transparent
      m.material.depthWrite = matSnap.depthWrite
      m.visible = matSnap.visible
      childSnap.forEach((s) => {
        s.obj.material.opacity = s.opacity
        s.obj.material.transparent = s.transparent
        s.obj.visible = s.visible
      })
    })
  }
  for (const [iid, im] of itemMeshes.value) {
    if (targetSet.has(iid)) continue
    const m = im.mesh
    const snap = { opacity: m.material.opacity, transparent: m.material.transparent }
    m.material.opacity = 0.06
    m.material.transparent = true
    restoreFns.push(() => {
      m.material.opacity = snap.opacity
      m.material.transparent = snap.transparent
    })
  }

  occlusionRestore = () => { restoreFns.forEach((fn) => { try { fn() } catch {} }) }
  // Auto-restore after the pulse completes (~5s window).
  setTimeout(() => {
    if (occlusionRestore) { occlusionRestore(); occlusionRestore = null }
  }, 5000)
}

async function focusItem(itemId) {
  await focusItems([itemId])
}

// Highlight one or more item meshes at once. Camera fits all of them; every
// matching cube pulses; non-target locations and items are ghosted out.
async function focusItems(itemIds) {
  const ids = (itemIds || []).filter((id) => id && itemMeshes.value.has(id))
  if (!ids.length) return

  if (ids.length === 1) {
    // Walk the single-target chain so we get a satisfying "drill-in" animation.
    const id = ids[0]
    const item = (props.items || []).find((i) => i.id === id)
    if (item?.location_id) {
      const byId = new Map((props.locations || []).map((l) => [l.id, l]))
      const chain = []
      let cur = item.location_id
      while (cur) { chain.unshift(cur); cur = byId.get(cur)?.parent_id }
      for (let i = 0; i < chain.length; i++) {
        const info = locMeshes.value.get(chain[i])
        if (!info) continue
        const dim = Math.max(info.geo.w, info.geo.d, info.geo.h)
        const dist = Math.max(1.2, dim * (1.6 - i * 0.25))
        await tweenCamera(
          { x: info.world.x + dist * 0.6, y: info.world.y + dist * 0.8, z: info.world.z + dist * 0.9 },
          { x: info.world.x, y: info.world.y, z: info.world.z }, 900)
      }
    }
  }

  // Final: fit camera to the bounding box of all targets.
  const positions = ids.map((id) => itemMeshes.value.get(id).mesh.position.clone())
  const box = new THREE.Box3().setFromPoints(positions)
  const center = new THREE.Vector3(); box.getCenter(center)
  const size = new THREE.Vector3(); box.getSize(size)
  const span = Math.max(size.x, size.y, size.z, 0.5)
  const dist = Math.max(2.0, span * 1.8 + 1.5)
  await tweenCamera(
    { x: center.x + dist * 0.55, y: center.y + dist * 0.75, z: center.z + dist * 0.85 },
    { x: center.x, y: center.y, z: center.z }, 800)

  occludeForMultiHighlight(ids)
  for (const id of ids) {
    pulseHighlight(itemMeshes.value.get(id).mesh)
  }
}

async function focusLocation(locId) {
  const info = locMeshes.value.get(locId)
  if (!info) return
  const dim = Math.max(info.geo.w, info.geo.d, info.geo.h, 1.2)
  await tweenCamera(
    { x: info.world.x + dim * 1.0, y: info.world.y + dim * 1.2, z: info.world.z + dim * 1.3 },
    { x: info.world.x, y: info.world.y, z: info.world.z }, 700)
}

function pulseHighlight(mesh) {
  const orig = mesh.material.color.getHex()
  const startTime = performance.now()
  const DURATION = 3000
  const orig2 = pulseTween
  pulseTween = () => {
    orig2?.()
    const t = (performance.now() - startTime) / DURATION
    if (t >= 1) {
      mesh.material.color.setHex(orig)
      mesh.material.emissive.setHex(0)
      mesh.scale.setScalar(1)
      pulseTween = orig2
      return
    }
    const phase = (Math.sin(t * Math.PI * 6) + 1) / 2
    mesh.material.color.setRGB(1, 0.85 - phase * 0.5, 0.2)
    mesh.material.emissive.setRGB(phase * 0.4, phase * 0.3, 0)
    mesh.scale.setScalar(1 + phase * 0.3)
  }
}

// Find the ancestor room of a given location id (or item id for items).
function ancestorRoomOf(locId) {
  if (!locId) return null
  const byId = new Map((props.locations || []).map((l) => [l.id, l]))
  let cur = locId
  const seen = new Set()
  while (cur && !seen.has(cur)) {
    seen.add(cur)
    const l = byId.get(cur)
    if (!l) return null
    if (l.kind === 'room') return l.id
    cur = l.parent_id
  }
  return null
}

function activeRoomIds() {
  const set = new Set()
  if (props.selectedLocationId) {
    const r = ancestorRoomOf(props.selectedLocationId)
    if (r) set.add(r)
  }
  if (props.highlightLocationId) {
    const r = ancestorRoomOf(props.highlightLocationId)
    if (r) set.add(r)
  }
  // Single + multi item targets — light up every room that contains any of them.
  const itemTargets = new Set(props.highlightItemIds || [])
  if (props.highlightItemId) itemTargets.add(props.highlightItemId)
  for (const iid of itemTargets) {
    const item = (props.items || []).find((i) => i.id === iid)
    if (item?.location_id) {
      const r = ancestorRoomOf(item.location_id)
      if (r) set.add(r)
    }
  }
  return set
}

function updateRoomLights() {
  const active = activeRoomIds()
  for (const [id, v] of locMeshes.value) {
    if (!v.roomLight) continue
    const on = active.has(id)
    v.roomLight.intensity = on ? 1.6 : 0.0
    if (v.roomLamp) v.roomLamp.material.color.setHex(on ? 0xffe8b0 : 0x4a4a4a)
  }
}

function applySelection() {
  for (const [id, v] of locMeshes.value) {
    const isSel = id === props.selectedLocationId
    v.mesh.children.forEach((c) => {
      if (c.material && c.geometry?.type === 'EdgesGeometry') {
        c.material.color.set(isSel ? 0xfacc15 : new THREE.Color(v.geo.color))
      }
    })
  }
  // Auto-attach TransformControls when editable + a location is selected.
  if (props.editable && transformControls) {
    if (props.selectedLocationId) {
      const v = locMeshes.value.get(props.selectedLocationId)
      if (v) transformControls.attach(v.mesh)
      else transformControls.detach()
    } else {
      transformControls.detach()
    }
  }
  updateRoomLights()
}

function commitTransform() {
  if (!transformControls?.object) return
  const mesh = transformControls.object
  const ud = mesh.userData
  if (ud?.type !== 'location') return
  const loc = (props.locations || []).find((l) => l.id === ud.id)
  if (!loc) return
  const info = locMeshes.value.get(ud.id)
  if (!info) return

  // New world center / anchor (anchor = bottom of box).
  const newW = info.geo.w * mesh.scale.x
  const newH = info.geo.h * mesh.scale.y
  const newD = info.geo.d * mesh.scale.z
  const cx = mesh.position.x, cy = mesh.position.y, cz = mesh.position.z
  const ax = cx, ay = cy - newH / 2, az = cz

  // Find new parent: smallest container whose volume contains the new anchor (skip self/descendants).
  const cat = catalogFor(loc.kind)
  let newParentId = loc.parent_id
  if (cat && !cat.isRoom) {
    const descendants = (() => {
      const set = new Set([loc.id])
      let added = true
      while (added) {
        added = false
        for (const l of (props.locations || [])) {
          if (l.parent_id && set.has(l.parent_id) && !set.has(l.id)) {
            set.add(l.id); added = true
          }
        }
      }
      return set
    })()
    let bestId = null, bestVol = Infinity
    for (const [pid, pinfo] of locMeshes.value) {
      if (descendants.has(pid)) continue
      const pcat = catalogFor(pinfo.locInfo.loc.kind)
      if (!pcat?.container) continue
      const pg = pinfo.geo
      const pAx = pinfo.world.x, pAy = pinfo.world.y - pg.h / 2, pAz = pinfo.world.z
      const inside =
        ax >= pAx - pg.w / 2 - 0.01 && ax <= pAx + pg.w / 2 + 0.01 &&
        ay >= pAy - 0.02 && ay <= pAy + pg.h + 0.02 &&
        az >= pAz - pg.d / 2 - 0.01 && az <= pAz + pg.d / 2 + 0.01
      if (inside) {
        const v = pg.w * pg.h * pg.d
        if (v < bestVol) { bestVol = v; bestId = pid }
      }
    }
    newParentId = bestId
  }

  // Compute new geometry relative to new parent.
  const newParentInfo = newParentId ? locMeshes.value.get(newParentId) : null
  const newParentLoc = newParentId ? (props.locations || []).find((l) => l.id === newParentId) : null
  const newParentGeo = newParentInfo?.geo || null
  const parentAx = newParentInfo?.world.x || 0
  const parentAy = newParentInfo ? newParentInfo.world.y - newParentInfo.geo.h / 2 : 0
  const parentAz = newParentInfo?.world.z || 0

  // Auto-snap level if dragged into a multi-level container at a y matching a layer.
  let newLevel = info.geo.level
  if (newParentGeo?.levels >= 2) {
    const localAy = ay - parentAy
    const layerH = newParentGeo.h / newParentGeo.levels
    const guess = Math.round(localAy / layerH) + 1
    newLevel = Math.max(1, Math.min(newParentGeo.levels, guess))
  } else {
    newLevel = 0
  }

  const newGeo = {
    ...(loc.geometry || {}),
    x: ax - parentAx,
    // When parent has levels, y is overridden by buildWorldMap from `level`. Still record raw y as fallback.
    y: ay - parentAy,
    z: az - parentAz,
    rot: ((mesh.rotation.y * 180 / Math.PI) % 360 + 360) % 360,
    w: newW, h: newH, d: newD,
    color: info.geo.color,
    levels: info.geo.levels,
    level: newLevel,
  }

  emit('transform-end', {
    id: ud.id,
    geometry: newGeo,
    parent_id: (newParentId !== loc.parent_id) ? newParentId : undefined,
  })
}

function setTransformMode(m) {
  transformMode.value = m
  if (transformControls) transformControls.setMode(m)
}

function onPointerMove(ev) {
  if (!container.value) return
  const rect = renderer.domElement.getBoundingClientRect()
  pointer.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1
  pointer.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1
  raycaster.setFromCamera(pointer, camera)
  const hits = raycaster.intersectObjects(scene.children, false)
  const hit = hits.find((h) => h.object.userData?.type)
  if (hit) {
    const ud = hit.object.userData
    tooltip.value = {
      visible: true,
      x: ev.clientX - rect.left + 12,
      y: ev.clientY - rect.top + 12,
      text: ud.type === 'item' ? `📎 ${ud.name}` : `🗂 ${nameOfLoc(ud.id)}`,
    }
  } else tooltip.value.visible = false
}

function onPointerDown(ev) {
  if (!props.selectable) return
  if (transformControls?.dragging) return
  const rect = renderer.domElement.getBoundingClientRect()
  pointer.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1
  pointer.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1
  raycaster.setFromCamera(pointer, camera)
  const hits = raycaster.intersectObjects(scene.children, false)
  const hit = hits.find((h) => h.object.userData?.type)
  if (!hit) return
  const ud = hit.object.userData
  if (ud.type === 'item') emit('select-item', ud.id)
  else if (ud.type === 'location') emit('select-location', ud.id)
}

function nameOfLoc(id) {
  const l = (props.locations || []).find((x) => x.id === id)
  return l ? l.name : '?'
}

onMounted(() => { init(); rebuild(); loop() })
onBeforeUnmount(() => {
  cancelAnimationFrame(raf)
  resizeObserver?.disconnect()
  controls?.dispose()
  transformControls?.dispose?.()
  if (renderer) { renderer.dispose(); renderer.domElement.remove() }
})

watch(() => [props.locations, props.items], rebuild, { deep: true })
watch(() => props.highlightItemId, (v) => { if (v) focusItems([v]); updateRoomLights() })
watch(() => props.highlightItemIds, (v) => {
  if (Array.isArray(v) && v.length) focusItems(v)
  updateRoomLights()
}, { deep: true })
watch(() => props.highlightLocationId, (v) => { if (v) focusLocation(v); updateRoomLights() })
watch(() => props.selectedLocationId, applySelection)

defineExpose({ focusItem, focusItems, focusLocation, fitAll, setTransformMode })
</script>

<template>
  <div class="relative">
    <div ref="container" :style="{ height: height + 'px' }"
         class="rounded-lg overflow-hidden border border-slate-200 bg-slate-900"></div>
    <div v-if="tooltip.visible"
         class="pointer-events-none absolute bg-slate-900/95 text-white text-xs rounded px-2 py-1 shadow-lg"
         :style="{ left: tooltip.x + 'px', top: tooltip.y + 'px' }">
      {{ tooltip.text }}
    </div>
    <div class="absolute top-2 right-2 flex flex-col gap-1 items-end">
      <button class="px-2 py-1 rounded bg-white/90 hover:bg-white text-xs shadow"
              @click="fitAll(true)" title="重置视角">⤢</button>
      <div v-if="editable" class="bg-white/90 rounded shadow flex text-xs overflow-hidden">
        <button :class="['px-2 py-1', transformMode==='translate' && 'bg-slate-900 text-white']"
                @click="setTransformMode('translate')" title="移动 (T)">移</button>
        <button :class="['px-2 py-1', transformMode==='rotate' && 'bg-slate-900 text-white']"
                @click="setTransformMode('rotate')" title="旋转 (R)">转</button>
        <button :class="['px-2 py-1', transformMode==='scale' && 'bg-slate-900 text-white']"
                @click="setTransformMode('scale')" title="缩放 (S)">缩</button>
      </div>
    </div>
  </div>
</template>
