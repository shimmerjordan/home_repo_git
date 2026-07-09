<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount, shallowRef } from 'vue'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { TransformControls } from 'three/examples/jsm/controls/TransformControls.js'
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js'
import { buildWorldMap, catalogFor, defaultChildY, levelY,
         pointInPolygon, describeLocationChain } from '../composables/sceneLayout'
import { buildPrettyFurniture, hasPrettyMesh } from '../composables/furnitureMesh'

const props = defineProps({
  locations: { type: Array, default: () => [] },
  items: { type: Array, default: () => [] },
  highlightItemId: { type: Number, default: null },
  highlightItemIds: { type: Array, default: () => [] },   // multi-target (overrides single)
  highlightLocationId: { type: Number, default: null },
  // Accept either a number of pixels or any CSS string (e.g. "clamp(360px, 50vh, 700px)").
  height: { type: [Number, String], default: 480 },
  selectable: { type: Boolean, default: false },
  editable: { type: Boolean, default: false },
  selectedLocationId: { type: Number, default: null },
  // When true, skip shadows and per-room point lights so iPad / touch devices stay
  // smooth. Auto-detected upstream; user can flip the switch via the toolbar.
  lowQuality: { type: Boolean, default: false },
  // List of room IDs whose item cubes should be rendered as visible markers. Items
  // in other rooms (or directly in rooms without a container) stay hidden. A
  // currently-highlighted item is ALWAYS visible regardless of this list, so a
  // voice search still gets a pulsing target.
  showItemsInRoomIds: { type: Array, default: () => [] },
  // Home filter. When set, only locations whose ancestor chain includes this id
  // are rendered (rooms without any home ancestor are shown when this is null).
  activeHomeId: { type: Number, default: null },
})
const emit = defineEmits(['select-location', 'select-item', 'transform-end', 'update:low-quality'])

// Resolved CSS height for the renderer container.
const containerHeight = computed(() => typeof props.height === 'number' ? props.height + 'px' : String(props.height))

// Breadcrumb of the currently highlighted item(s). Rendered as a floating
// overlay so the user can see where the pulsing cube actually lives.
const highlightInfo = computed(() => {
  const ids = (props.highlightItemIds && props.highlightItemIds.length)
    ? props.highlightItemIds
    : (props.highlightItemId ? [props.highlightItemId] : [])
  if (!ids.length) return null
  const items = props.items || []
  const out = []
  for (const id of ids) {
    const item = items.find((i) => i.id === id)
    if (!item) continue
    out.push({
      id,
      name: item.name,
      path: describeLocationChain(item.location_id, props.locations || []),
    })
  }
  return out.length ? out : null
})

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

// Atmosphere extras (disposed alongside the renderer).
let motes = null, moteState = null, pmrem = null, bgTexture = null
let lastFrameT = 0
const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia
  && window.matchMedia('(prefers-reduced-motion: reduce)').matches

// Vertical depth gradient for the sky/backdrop — reads richer than a flat fill.
function makeGradientBackground() {
  const c = document.createElement('canvas')
  c.width = 4; c.height = 256
  const ctx = c.getContext('2d')
  const g = ctx.createLinearGradient(0, 0, 0, 256)
  g.addColorStop(0, '#22324f')       // sky
  g.addColorStop(0.55, '#141f36')
  g.addColorStop(1, '#0b1220')       // floor haze
  ctx.fillStyle = g; ctx.fillRect(0, 0, 4, 256)
  const t = new THREE.CanvasTexture(c)
  t.colorSpace = THREE.SRGBColorSpace
  return t
}

// Dust motes: per-point size / alpha / warm-cool colour, soft round sprite,
// perspective attenuation. Small + slow + varied = atmosphere, not confetti.
const MOTE_VERT = `
  attribute float aSize; attribute float aAlpha; attribute vec3 aColor;
  varying float vAlpha; varying vec3 vColor;
  uniform float uPixelRatio; uniform float uSize;
  void main(){
    vAlpha = aAlpha; vColor = aColor;
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_PointSize = aSize * uSize * uPixelRatio * (1.0 / max(0.1, -mv.z));
    gl_Position = projectionMatrix * mv;
  }`
const MOTE_FRAG = `
  varying float vAlpha; varying vec3 vColor;
  void main(){
    float d = length(gl_PointCoord - vec2(0.5));
    float a = smoothstep(0.5, 0.05, d);
    if (a <= 0.0) discard;
    gl_FragColor = vec4(vColor, a * vAlpha);
  }`

function buildMotes(count) {
  const pos = new Float32Array(count * 3)
  const col = new Float32Array(count * 3)
  const size = new Float32Array(count)
  const alpha = new Float32Array(count)
  const vy = new Float32Array(count)
  const phase = new Float32Array(count)
  const warm = [1.0, 0.88, 0.62], cool = [0.70, 0.80, 1.0]
  const R = 24
  for (let i = 0; i < count; i++) {
    pos[i*3]   = (Math.random()*2 - 1) * R
    pos[i*3+1] = Math.random() * 15
    pos[i*3+2] = (Math.random()*2 - 1) * R
    const mix = Math.random()
    for (let k = 0; k < 3; k++) col[i*3+k] = warm[k]*(1-mix) + cool[k]*mix
    size[i]  = 0.5 + Math.random()*2.2
    alpha[i] = 0.12 + Math.random()*0.5
    vy[i]    = 0.06 + Math.random()*0.28
    phase[i] = Math.random()*Math.PI*2
  }
  const geo = new THREE.BufferGeometry()
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3))
  geo.setAttribute('aColor', new THREE.BufferAttribute(col, 3))
  geo.setAttribute('aSize', new THREE.BufferAttribute(size, 1))
  geo.setAttribute('aAlpha', new THREE.BufferAttribute(alpha, 1))
  const mat = new THREE.ShaderMaterial({
    vertexShader: MOTE_VERT, fragmentShader: MOTE_FRAG,
    uniforms: {
      uPixelRatio: { value: Math.min(2, window.devicePixelRatio || 1) },
      uSize: { value: 26 },
    },
    transparent: true, depthWrite: false, blending: THREE.NormalBlending,
  })
  const points = new THREE.Points(geo, mat)
  points.frustumCulled = false
  points.renderOrder = 3
  return { points, pos, vy, phase, count, geo, mat }
}

function updateMotes(dt, t) {
  if (!moteState) return
  const { pos, vy, phase, count, geo } = moteState
  for (let i = 0; i < count; i++) {
    let y = pos[i*3+1] + vy[i] * dt
    if (y > 15.5) { y = -0.5; pos[i*3] = (Math.random()*2-1)*24; pos[i*3+2] = (Math.random()*2-1)*24 }
    pos[i*3+1] = y
    // sin*dt integrates to a bounded oscillation, so x drifts within a small band.
    pos[i*3] += Math.sin(t*0.25 + phase[i]) * 0.06 * dt
  }
  geo.attributes.position.needsUpdate = true
}

function disposeExtras() {
  if (motes) { scene?.remove(motes); moteState?.geo.dispose(); moteState?.mat.dispose() }
  motes = null; moteState = null
  if (pmrem) { pmrem.dispose(); pmrem = null }
  if (bgTexture) { bgTexture.dispose(); bgTexture = null }
  lastFrameT = 0
}

function init() {
  scene = new THREE.Scene()
  // Vertical gradient backdrop + depth fog toward the floor haze colour for a warmer look.
  bgTexture = makeGradientBackground()
  scene.background = bgTexture
  scene.fog = new THREE.Fog(0x0b1220, 34, 92)

  const w = container.value.clientWidth || 600
  const h = container.value.clientHeight || (typeof props.height === 'number' ? props.height : 480)
  camera = new THREE.PerspectiveCamera(45, w / h, 0.05, 500)
  camera.position.set(8, 9, 10)

  renderer = new THREE.WebGLRenderer({ antialias: !props.lowQuality, powerPreference: 'high-performance' })
  renderer.setSize(w, h)
  // Cap pixel ratio: full-res retina/4K is a big fill-rate cost for little gain. 2x is the
  // sweet spot on high quality; 1.5x keeps iPad Safari smooth in low-quality mode.
  renderer.setPixelRatio(Math.min(props.lowQuality ? 1.5 : 2, window.devicePixelRatio || 1))
  renderer.shadowMap.enabled = !props.lowQuality
  renderer.shadowMap.type = THREE.PCFSoftShadowMap
  renderer.outputColorSpace = THREE.SRGBColorSpace
  renderer.toneMapping = THREE.ACESFilmicToneMapping
  renderer.toneMappingExposure = 1.05
  container.value.appendChild(renderer.domElement)

  // Image-based lighting from a neutral room: gives MeshStandardMaterials real
  // specular / reflection response — the single biggest jump in material quality,
  // at a one-time prefilter cost and zero per-frame overhead.
  pmrem = new THREE.PMREMGenerator(renderer)
  const envRT = pmrem.fromScene(new RoomEnvironment(), 0.04)
  scene.environment = envRT.texture
  scene.environmentIntensity = 0.55

  controls = new OrbitControls(camera, renderer.domElement)
  controls.enableDamping = true
  controls.target.set(0, 1, 0)

  // Ambient + sky/ground hemisphere for soft fill.
  scene.add(new THREE.AmbientLight(0xffffff, 0.30))
  const hemi = new THREE.HemisphereLight(0xbfd4ff, 0x6b5a48, 0.45)
  hemi.position.set(0, 30, 0)
  scene.add(hemi)
  // Sun: directional light. Shadows only in high-quality mode (very expensive on iPad).
  const sun = new THREE.DirectionalLight(0xffeed5, props.lowQuality ? 1.0 : 0.9)
  sun.position.set(18, 28, 14)
  if (!props.lowQuality) {
    sun.castShadow = true
    sun.shadow.mapSize.set(1024, 1024)
    sun.shadow.camera.near = 1
    sun.shadow.camera.far = 80
    sun.shadow.camera.left = -25
    sun.shadow.camera.right = 25
    sun.shadow.camera.top = 25
    sun.shadow.camera.bottom = -25
    sun.shadow.bias = -0.0005
  }
  scene.add(sun)

  // Floor plane (catches shadows, gives a "ground" feel).
  const floorMat = new THREE.MeshStandardMaterial({
    color: 0x2c3a52, roughness: 0.95, metalness: 0,
  })
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(80, 80), floorMat)
  floor.rotation.x = -Math.PI / 2
  floor.position.y = -0.02
  floor.receiveShadow = !props.lowQuality
  scene.add(floor)

  const grid = new THREE.GridHelper(40, 40, 0x334155, 0x1e293b)
  grid.position.y = -0.01
  scene.add(grid)

  // Floating dust motes for atmospheric depth. Fewer in low-quality mode; none for
  // users who asked the OS to reduce motion.
  if (!prefersReducedMotion) {
    moteState = buildMotes(props.lowQuality ? 200 : 460)
    motes = moteState.points
    scene.add(motes)
  }

  if (props.editable) {
    transformControls = new TransformControls(camera, renderer.domElement)
    transformControls.size = 0.7
    transformControls.setSpace('world')
    // Three.js r170+ split TransformControls: the controller itself is no longer
    // an Object3D, so adding it to the scene fails with "not an instance of
    // THREE.Object3D". Use .getHelper() to get the visual gizmo Object3D.
    const tcHelper = typeof transformControls.getHelper === 'function'
      ? transformControls.getHelper()
      : transformControls
    scene.add(tcHelper)
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
    const hh = container.value.clientHeight     // pull from actual layout, supports CSS clamp()
    if (ww > 0 && hh > 0) {
      renderer.setSize(ww, hh)
      camera.aspect = ww / hh
      camera.updateProjectionMatrix()
    }
  })
  resizeObserver.observe(container.value)
}

// Map each location to its first 'home' ancestor (or null if none).
function computeHomeAncestors(locs) {
  const byId = new Map(locs.map((l) => [l.id, l]))
  const out = new Map()
  for (const l of locs) {
    let cur = l.id
    const seen = new Set()
    let home = null
    while (cur && !seen.has(cur)) {
      seen.add(cur)
      const n = byId.get(cur)
      if (!n) break
      if (n.kind === 'home') { home = n.id; break }
      cur = n.parent_id
    }
    out.set(l.id, home)
  }
  return out
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
  // Item cubes are now CHILDREN of their container mesh (so they inherit transforms).
  // Removing the container mesh already disposes them via the traverse above —
  // here we only need to clear the lookup map and dispose materials defensively.
  for (const v of itemMeshes.value.values()) {
    v.mesh.parent?.remove(v.mesh)
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

  // Pre-compute ancestor home for each location once per rebuild.
  const homeOf = computeHomeAncestors(props.locations || [])

  for (const [id, info] of world) {
    const { geo } = info
    // Home nodes are abstract groupings — no 3D mesh, no item cubes, no light.
    // Children (rooms) still see this home's geometry.x/z as a world offset via
    // buildWorldMap, so siblings stay spatially separated.
    if (info.loc.kind === 'home') continue
    if (props.activeHomeId != null && homeOf.get(id) !== props.activeHomeId) continue
    const isRoom = info.loc.kind === 'room'
    const cy = info.y + geo.h / 2
    // Polygon rooms use an extruded shape; everything else uses a centred BoxGeometry.
    const isPoly = isRoom && Array.isArray(geo.polygon) && geo.polygon.length >= 3
    let bodyGeo
    let meshY = cy
    if (isPoly) {
      // Three.js Shape lives on XY. We build it in world XZ coords and rotate so
      // depth becomes height. Negate Z to keep CCW winding and avoid mirroring.
      const pts = geo.polygon.map(([x, z]) => new THREE.Vector2(x, -z))
      const shape = new THREE.Shape(pts)
      bodyGeo = new THREE.ExtrudeGeometry(shape, { depth: geo.h, bevelEnabled: false })
      bodyGeo.rotateX(-Math.PI / 2)
      // ExtrudeGeometry now spans Y from 0 (floor) to geo.h (ceiling), so place at info.y.
      meshY = info.y
    } else {
      bodyGeo = new THREE.BoxGeometry(geo.w, geo.h, geo.d)
    }
    // For "pretty" solid furniture (bed/sofa/chair/tv/toilet/plant/...) we keep
    // an INVISIBLE box as the root (so hit-testing, transform-controls, occlusion
    // fading and item-cube parenting still work uniformly) and attach a detailed
    // group of sub-meshes on top.
    const prettyKind = !isRoom && hasPrettyMesh(info.loc.kind)
    const mat = new THREE.MeshStandardMaterial({
      color: new THREE.Color(geo.color),
      transparent: true,
      opacity: prettyKind ? 0 : (isRoom ? 0.06 : 0.18),
      depthWrite: false,
      roughness: 0.7,
      metalness: 0.05,
      side: THREE.DoubleSide,
    })
    const mesh = new THREE.Mesh(bodyGeo, mat)
    mesh.position.set(info.x, meshY, info.z)
    // Use the COMPOSED rotation (own rot + every ancestor's rot) so a box inside
    // a 90°-rotated cabinet visibly turns with it.
    mesh.rotation.y = (info.rotDeg || 0) * Math.PI / 180
    mesh.userData = { type: 'location', id }
    mesh.renderOrder = 1

    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(bodyGeo),
      new THREE.LineBasicMaterial({ color: new THREE.Color(geo.color) }),
    )
    // Hide edge outlines for pretty furniture — the detailed sub-meshes carry their
    // own visual structure and the box outline looks like a stray frame.
    if (prettyKind) edges.visible = false
    mesh.add(edges)

    if (prettyKind) {
      const pretty = buildPrettyFurniture(info.loc.kind, geo)
      if (pretty) {
        // Add each top-level sub-mesh directly so the existing occlusion code
        // (which iterates mesh.children with material) can fade them in/out.
        for (const child of [...pretty.children]) {
          pretty.remove(child)
          mesh.add(child)
        }
        // For deeper children (groups within groups) ensure shadows are set up.
        if (!props.lowQuality) {
          mesh.traverse((c) => {
            if (c.isMesh && c !== mesh) { c.castShadow = true; c.receiveShadow = true }
          })
        }
      }
    }

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

    // Shadows only when lighting is on (skip rooms — they're translucent shells).
    if (!props.lowQuality) {
      if (!isRoom) {
        mesh.castShadow = true
        mesh.receiveShadow = true
      } else {
        mesh.receiveShadow = true
      }
    }

    scene.add(mesh)
    const slot = { mesh, world: { x: info.x, y: cy, z: info.z }, geo, locInfo: info,
                   roomLight: null, roomLamp: null }

    // Per-room ceiling light: only in high-quality mode (point lights + shadow
    // updates are the single biggest perf hit on iPad Safari).
    if (isRoom && !props.lowQuality) {
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
    // Rule: only render item cubes when the parent is an ACTUAL storage container
    // (cabinet/shelf/drawer/box/fridge/desk/etc). Items placed directly in a room
    // would otherwise clutter the floor with anonymous pink cubes — they're still
    // searchable via voice + the highlight breadcrumb shows the chain.
    const cat = catalogFor(info.locInfo.loc.kind)
    if (!cat?.container || cat?.isRoom) continue

    const w = info.geo.w, h = info.geo.h, d = info.geo.d
    // Cube size = 25% of the container's smallest horizontal dim, capped at 20 cm.
    // We don't try to grid-pack any more — every cube lives at the container's
    // physical centre. Multiple items in the same container will visually overlap
    // into a single glowing marker (intentional: the breadcrumb tells you how many).
    const baseSize = Math.max(0.05, Math.min(0.2, Math.min(w, d) * 0.25))

    // Cubes are CHILDREN of the container mesh, positioned at LOCAL (0, 0, 0) —
    // i.e. the container's true physical centre. They inherit its world transform
    // (rotation, level offset, nested parenting) automatically.
    function spawn(it, size) {
      const cube = new THREE.Mesh(
        new THREE.BoxGeometry(size, size, size),
        new THREE.MeshStandardMaterial({
          color: new THREE.Color('#fb7185'), roughness: 0.6, transparent: true, opacity: 0.95,
        }),
      )
      cube.position.set(0, 0, 0)              // container centre, exact
      cube.userData = { type: 'item', id: it.id, name: it.name, locationId: locId }
      cube.visible = false                    // default hidden; updateItemVisibility flips it
      info.mesh.add(cube)
      itemMeshes.value.set(it.id, { mesh: cube, baseColor: cube.material.color.clone() })
    }
    items.forEach((it) => spawn(it, baseSize))
  }

  updateItemVisibility()

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
  const now = performance.now()
  const dt = lastFrameT ? Math.min(0.05, (now - lastFrameT) / 1000) : 0.016
  lastFrameT = now
  controls.update()
  if (moteState) updateMotes(dt, now / 1000)
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
  // On-path containers: light them up + reveal all their item cubes so the user
  // sees what else lives next to the target during the zoom-in.
  // The IMMEDIATE parent of a target item gets the strongest glow.
  const immediateParents = new Set()
  for (const tid of targetSet) {
    const t = (props.items || []).find((i) => i.id === tid)
    if (t?.location_id) immediateParents.add(t.location_id)
  }
  for (const id of onPath) {
    const v = locMeshes.value.get(id)
    if (!v) continue
    const m = v.mesh
    if (m.material && m.material.emissive) {
      const eSnap = m.material.emissive.getHex()
      const iSnap = m.material.emissiveIntensity ?? 1
      const strong = immediateParents.has(id)
      m.material.emissive.setHex(strong ? 0xffd166 : 0x88b4ff)
      m.material.emissiveIntensity = strong ? 0.55 : 0.25
      restoreFns.push(() => {
        m.material.emissive.setHex(eSnap)
        m.material.emissiveIntensity = iSnap
      })
    }
    // Reveal sibling item cubes inside this container temporarily.
    m.children.forEach((c) => {
      if (c.userData?.type === 'item' && !c.visible) {
        c.visible = true
        restoreFns.push(() => { c.visible = false })
      }
    })
  }
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
  const ids = (itemIds || []).filter((id) => !!id)
  if (!ids.length) return

  // Items that have a rendered cube (parent kind is a non-room container).
  const withCubes = ids.filter((id) => itemMeshes.value.has(id))

  // FALLBACK: no cubes exist (e.g. the item is placed directly in a room and we
  // intentionally skipped rendering it). Camera-zoom to the item's location and
  // let the highlight breadcrumb overlay tell the user where it is.
  if (!withCubes.length) {
    const item = (props.items || []).find((i) => i.id === ids[0])
    if (item?.location_id) await focusLocation(item.location_id)
    occludeForMultiHighlight(ids)
    return
  }

  if (withCubes.length === 1) {
    // Walk the parent chain for a "drill-in" feel on the single-target case.
    const id = withCubes[0]
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

  // Cube WORLD positions: cubes are children of their container mesh now, so
  // mesh.position is LOCAL. Use getWorldPosition() to fit the camera.
  const positions = withCubes.map((id) => {
    const m = itemMeshes.value.get(id).mesh
    m.updateMatrixWorld(true)
    return m.getWorldPosition(new THREE.Vector3())
  })
  const box = new THREE.Box3().setFromPoints(positions)
  const center = new THREE.Vector3(); box.getCenter(center)
  const size = new THREE.Vector3(); box.getSize(size)
  const span = Math.max(size.x, size.y, size.z, 0.5)
  const dist = Math.max(2.0, span * 1.8 + 1.5)
  await tweenCamera(
    { x: center.x + dist * 0.55, y: center.y + dist * 0.75, z: center.z + dist * 0.85 },
    { x: center.x, y: center.y, z: center.z }, 800)

  occludeForMultiHighlight(withCubes)
  for (const id of withCubes) pulseHighlight(itemMeshes.value.get(id).mesh)
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

// Show / hide item cubes based on the parent's room being in
// `props.showItemsInRoomIds`. A highlighted item is always visible regardless.
function updateItemVisibility() {
  const shownRooms = new Set(props.showItemsInRoomIds || [])
  const highlighted = new Set([
    ...((props.highlightItemIds && props.highlightItemIds.length) ? props.highlightItemIds : []),
    ...(props.highlightItemId ? [props.highlightItemId] : []),
  ])
  const itemsById = new Map((props.items || []).map((it) => [it.id, it]))
  for (const [iid, slot] of itemMeshes.value) {
    if (highlighted.has(iid)) { slot.mesh.visible = true; continue }
    const it = itemsById.get(iid)
    if (!it) { slot.mesh.visible = false; continue }
    const room = ancestorRoomOf(it.location_id)
    slot.mesh.visible = room != null && shownRooms.has(room)
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

  // Convert mesh world transform back to the parent's LOCAL frame, undoing the
  // parent's COMPOSED rotation. We saved that into locMeshes' info via buildWorldMap.
  const parentRotDeg = newParentInfo?.locInfo?.rotDeg || 0
  const inv = -parentRotDeg * Math.PI / 180
  const cs2 = Math.cos(inv), sn2 = Math.sin(inv)
  const dxw = ax - parentAx, dzw = az - parentAz
  const lxFinal = newParentInfo ? dxw * cs2 - dzw * sn2 : dxw
  const lzFinal = newParentInfo ? dxw * sn2 + dzw * cs2 : dzw
  const worldRotDeg = ((mesh.rotation.y * 180 / Math.PI) % 360 + 360) % 360
  const ownRotDeg = ((worldRotDeg - parentRotDeg) % 360 + 360) % 360

  const newGeo = {
    ...(loc.geometry || {}),
    x: lxFinal,
    // When parent has levels, y is overridden by buildWorldMap from `level`. Still record raw y as fallback.
    y: ay - parentAy,
    z: lzFinal,
    rot: ownRotDeg,
    w: newW, h: newH, d: newD,
    color: info.geo.color,
    levels: info.geo.levels,
    level: newLevel,
  }
  // Polygon rooms: scale the polygon points proportionally so the actual shape
  // resizes (otherwise the bbox-derived w/d would disagree with the underlying poly).
  const oldPoly = loc.geometry?.polygon
  if (Array.isArray(oldPoly) && oldPoly.length >= 3) {
    const sx = mesh.scale.x, sz = mesh.scale.z
    if (Math.abs(sx - 1) > 1e-3 || Math.abs(sz - 1) > 1e-3) {
      newGeo.polygon = oldPoly.map(([x, z]) => [
        Math.round(x * sx * 1000) / 1000,
        Math.round(z * sz * 1000) / 1000,
      ])
    }
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
  const hits = raycaster.intersectObjects(scene.children, true)
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
  const hits = raycaster.intersectObjects(scene.children, true)
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
  disposeExtras()
  if (renderer) { renderer.dispose(); renderer.domElement.remove() }
})

// Rebuilding the renderer is the cleanest way to honour a runtime lowQuality flip
// (shadow map state, perRoom lights and pixel ratio are all set up in init()).
watch(() => props.lowQuality, () => {
  cancelAnimationFrame(raf)
  resizeObserver?.disconnect()
  controls?.dispose()
  transformControls?.dispose?.()
  disposeExtras()
  if (renderer) { renderer.dispose(); renderer.domElement.remove() }
  // Reset module-locals so re-init starts cleanly.
  scene = camera = renderer = controls = transformControls = null
  locMeshes.value = new Map()
  itemMeshes.value = new Map()
  init()
  rebuild()
  loop()
})

watch(() => [props.locations, props.items], rebuild, { deep: true })
watch(() => props.highlightItemId, (v) => {
  if (v) focusItems([v])
  updateRoomLights(); updateItemVisibility()
})
watch(() => props.highlightItemIds, (v) => {
  if (Array.isArray(v) && v.length) focusItems(v)
  updateRoomLights(); updateItemVisibility()
}, { deep: true })
watch(() => props.highlightLocationId, (v) => { if (v) focusLocation(v); updateRoomLights() })
watch(() => props.selectedLocationId, applySelection)
watch(() => props.showItemsInRoomIds, updateItemVisibility, { deep: true })

defineExpose({ focusItem, focusItems, focusLocation, fitAll, setTransformMode })
</script>

<template>
  <div class="relative">
    <div ref="container" :style="{ height: containerHeight }"
         class="rounded-lg overflow-hidden border border-slate-200 bg-slate-900"></div>

    <!-- Highlight breadcrumb. Sits at the TOP-LEFT of the viewport so it never
         overlaps the pulsing cube (which is centred during the focus animation),
         and stays mostly transparent so the scene shows through. -->
    <div v-if="highlightInfo"
         class="pointer-events-none absolute top-2 left-2 right-12 max-w-md
                bg-slate-900/85 backdrop-blur-sm text-white text-xs rounded-lg
                px-3 py-2 shadow-lg border border-white/10 space-y-1">
      <div class="text-[10px] opacity-60 tracking-wide uppercase">
        高亮中 ({{ highlightInfo.length }} 处)
      </div>
      <ul class="space-y-1">
        <li v-for="h in highlightInfo" :key="h.id" class="leading-snug">
          <div class="font-medium text-amber-200">📎 {{ h.name }}</div>
          <div class="opacity-80 break-all">{{ h.path || '未指定位置' }}</div>
        </li>
      </ul>
    </div>

    <div v-if="tooltip.visible"
         class="pointer-events-none absolute bg-slate-900/95 text-white text-xs rounded px-2 py-1 shadow-lg"
         :style="{ left: tooltip.x + 'px', top: tooltip.y + 'px' }">
      {{ tooltip.text }}
    </div>
    <div class="absolute top-2 right-2 flex flex-col gap-1 items-end">
      <button class="px-2 py-1 rounded bg-white/90 hover:bg-white text-xs shadow"
              @click="fitAll(true)" title="重置视角">⤢</button>
      <button class="px-2 py-1 rounded bg-white/90 hover:bg-white text-xs shadow"
              :title="lowQuality ? '当前: 省电模式 (无阴影, 无吸顶灯). 点击切换到全光照' : '当前: 全光照 (耗电较高). 点击切换到省电模式'"
              @click="$emit('update:low-quality', !lowQuality)">
        {{ lowQuality ? '🌙 省电' : '☀ 光照' }}
      </button>
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
