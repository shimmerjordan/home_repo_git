<script setup>
import { ref, watch, onMounted, onBeforeUnmount, defineAsyncComponent, h } from 'vue'
import VoicePanel from './components/VoicePanel.vue'
import ItemList from './components/ItemList.vue'
import LocationManager from './components/LocationManager.vue'
import SettingsPanel from './components/SettingsPanel.vue'
import TransactionFeed from './components/TransactionFeed.vue'
import LogsPanel from './components/LogsPanel.vue'
import BackupPanel from './components/BackupPanel.vue'
import AuditPanel from './components/AuditPanel.vue'
import { api } from './api'

// The 3D builder drags in three.js + PlanEditor (~600 KB) that no other tab needs. Load it
// only when the 3D tab is first opened; <keep-alive> then preserves its scene across tab switches.
const BuildingPanel = defineAsyncComponent({
  loader: () => import('./components/BuildingPanel.vue'),
  loadingComponent: () => h('div', { class: 'card p-10 text-center text-sm text-slate-400' }, '正在载入 3D 模块…'),
  delay: 150,
})

// Tab state lives in the URL hash (#tab=items) so refreshing the page or sharing
// a link keeps the user on the same view. Hash routing is enough — no full router
// needed and it works behind nginx without any rewrite rules.
const VALID_TABS = ['voice', 'items', 'locations', 'building', 'log', 'audit', 'logs', 'settings']
function _tabFromHash() {
  if (typeof window === 'undefined') return 'voice'
  const m = window.location.hash.match(/tab=([\w-]+)/)
  const t = m && m[1]
  return VALID_TABS.includes(t) ? t : 'voice'
}
const tab = ref(_tabFromHash())
function _writeHash(t) {
  if (typeof window === 'undefined') return
  const next = `#tab=${t}`
  if (window.location.hash !== next) {
    // Use replaceState so we don't pollute the history stack with every tab click;
    // refresh still lands on the same tab.
    history.replaceState(null, '', next)
  }
}
watch(tab, _writeHash, { immediate: true })
if (typeof window !== 'undefined') {
  window.addEventListener('hashchange', () => { tab.value = _tabFromHash() })
}
const refreshKey = ref(0)
const settings = ref(null)

async function loadSettings() {
  try { settings.value = await api.getSettings() } catch (e) { console.warn(e) }
}
onMounted(loadSettings)

function bumpRefresh() { refreshKey.value += 1 }

// Fullscreen toggle — modern API + iOS webkit fallback. iPad Safari < 16.4 has no
// real fullscreen, so on those devices we toggle a CSS class to hide the page chrome.
const isFullscreen = ref(false)
// Two-tap exit guard: once in fullscreen, the first tap on the toggle arms a
// 2-second window during which a second tap actually exits. Single mis-taps
// (very common on iPad where the button sits near the page edge) get absorbed.
const exitArmed = ref(false)
let exitArmTimer = null
function _fsElement() {
  return document.fullscreenElement || document.webkitFullscreenElement || null
}
function syncFs() {
  isFullscreen.value = !!_fsElement() || document.documentElement.classList.contains('faux-fullscreen')
  if (!isFullscreen.value) {
    exitArmed.value = false
    if (exitArmTimer) { clearTimeout(exitArmTimer); exitArmTimer = null }
  }
}
async function _doExit() {
  const el = document.documentElement
  try {
    const ex = document.exitFullscreen || document.webkitExitFullscreen
    if (ex && _fsElement()) await ex.call(document)
  } catch {}
  el.classList.remove('faux-fullscreen')
  syncFs()
}
async function _doEnter() {
  const el = document.documentElement
  try {
    const rq = el.requestFullscreen || el.webkitRequestFullscreen
    if (rq) { await rq.call(el); return syncFs() }
    el.classList.add('faux-fullscreen')
  } catch {
    el.classList.add('faux-fullscreen')
  }
  syncFs()
}
async function toggleFullscreen() {
  if (!isFullscreen.value) { await _doEnter(); return }
  // In fullscreen: require two-tap to exit.
  if (!exitArmed.value) {
    exitArmed.value = true
    if (exitArmTimer) clearTimeout(exitArmTimer)
    exitArmTimer = setTimeout(() => { exitArmed.value = false; exitArmTimer = null }, 2000)
    return
  }
  if (exitArmTimer) { clearTimeout(exitArmTimer); exitArmTimer = null }
  await _doExit()
}
onMounted(() => {
  document.addEventListener('fullscreenchange', syncFs)
  document.addEventListener('webkitfullscreenchange', syncFs)
})
onBeforeUnmount(() => {
  document.removeEventListener('fullscreenchange', syncFs)
  document.removeEventListener('webkitfullscreenchange', syncFs)
})

const tabs = [
  { id: 'voice', label: '语音', icon: '🎙' },
  { id: 'items', label: '物品', icon: '📦' },
  { id: 'locations', label: '位置', icon: '🗂' },
  { id: 'building', label: '3D', icon: '🏗' },
  { id: 'log', label: '记录', icon: '📜' },
  { id: 'audit', label: '变更', icon: '🕓' },
  { id: 'logs', label: '诊断', icon: '🔧' },
  { id: 'backup', label: '备份', icon: '☁' },
  { id: 'settings', label: '设置', icon: '⚙' },
]

// --- Header ambient point-cloud: a quiet constellation behind the title bar.
// Contained to the header, low-opacity, pointer-reactive — a design accent, not page noise.
// Skips animation for reduced-motion users and pauses when the tab is hidden.
const bgCanvas = ref(null)
let _pfCleanup = null
function initParticleField() {
  const canvas = bgCanvas.value
  if (!canvas) return
  const host = canvas.parentElement
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const dpr = Math.min(2, window.devicePixelRatio || 1)
  let W = 0, H = 0, parts = [], raf = 0, running = true
  const pointer = { x: -999, y: -999, on: false }

  function resize() {
    W = host.clientWidth; H = host.clientHeight
    if (!W || !H) return
    canvas.width = W * dpr; canvas.height = H * dpr
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px'
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    const target = Math.max(12, Math.min(46, Math.round(W / 34)))
    if (parts.length !== target) {
      parts = Array.from({ length: target }, () => ({
        x: Math.random() * W, y: Math.random() * H,
        vx: (Math.random() * 2 - 1) * 0.14, vy: (Math.random() * 2 - 1) * 0.14,
        r: 0.8 + Math.random() * 1.4, a: 0.25 + Math.random() * 0.4,
      }))
    }
    if (reduce) draw()
  }
  function step() {
    for (const p of parts) {
      p.x += p.vx; p.y += p.vy
      if (p.x < 0 || p.x > W) p.vx *= -1
      if (p.y < 0 || p.y > H) p.vy *= -1
      if (pointer.on) {
        const dx = p.x - pointer.x, dy = p.y - pointer.y, d = Math.hypot(dx, dy)
        if (d < 90 && d > 1) { const f = (1 - d / 90) * 0.4; p.x += (dx / d) * f; p.y += (dy / d) * f }
      }
    }
    draw()
    if (running) raf = requestAnimationFrame(step)
  }
  function draw() {
    ctx.clearRect(0, 0, W, H)
    for (let i = 0; i < parts.length; i++) {
      const a = parts[i]
      for (let j = i + 1; j < parts.length; j++) {
        const b = parts[j], d = Math.hypot(a.x - b.x, a.y - b.y)
        if (d < 108) {
          ctx.strokeStyle = `rgba(129,140,248,${(1 - d / 108) * 0.12})`
          ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke()
        }
      }
      if (pointer.on) {
        const d = Math.hypot(a.x - pointer.x, a.y - pointer.y)
        if (d < 132) {
          ctx.strokeStyle = `rgba(165,180,252,${(1 - d / 132) * 0.22})`
          ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(pointer.x, pointer.y); ctx.stroke()
        }
      }
    }
    for (const p of parts) {
      ctx.fillStyle = `rgba(165,180,252,${p.a})`
      ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fill()
    }
  }
  const onMove = (e) => { const r = host.getBoundingClientRect(); pointer.x = e.clientX - r.left; pointer.y = e.clientY - r.top; pointer.on = true }
  const onLeave = () => { pointer.on = false; pointer.x = pointer.y = -999 }
  const onVis = () => {
    if (document.hidden) { running = false; cancelAnimationFrame(raf) }
    else if (!reduce && !running) { running = true; raf = requestAnimationFrame(step) }
  }
  const ro = new ResizeObserver(resize); ro.observe(host)
  host.addEventListener('pointermove', onMove)
  host.addEventListener('pointerleave', onLeave)
  document.addEventListener('visibilitychange', onVis)
  resize()
  if (!reduce) raf = requestAnimationFrame(step)
  _pfCleanup = () => {
    running = false; cancelAnimationFrame(raf); ro.disconnect()
    host.removeEventListener('pointermove', onMove)
    host.removeEventListener('pointerleave', onLeave)
    document.removeEventListener('visibilitychange', onVis)
  }
}
onMounted(initParticleField)
onBeforeUnmount(() => { _pfCleanup?.() })
</script>

<template>
  <div class="min-h-full flex flex-col bg-slate-100">
    <header
      class="relative overflow-hidden bg-slate-900 text-white sticky top-0 z-10 border-b border-white/10 shadow-lg shadow-slate-900/20
             px-3 sm:px-4 pb-2 sm:pb-3"
      style="padding-top: calc(0.5rem + env(safe-area-inset-top));">
      <canvas ref="bgCanvas" class="pointer-events-none absolute inset-0 z-0" aria-hidden="true"></canvas>
      <div class="relative z-[1] flex items-center justify-between gap-2 flex-wrap max-w-7xl mx-auto w-full">
        <div class="flex items-center gap-2.5 shrink-0">
          <span class="grid place-items-center w-8 h-8 rounded-lg bg-indigo-600 text-base leading-none shadow-sm" aria-hidden="true">🏠</span>
          <span class="flex flex-col leading-tight">
            <span class="font-semibold tracking-tight">语音仓储管家</span>
            <span class="text-[10px] text-slate-400 hidden sm:block">本地 NAS · 私有存储</span>
          </span>
          <button @click="toggleFullscreen"
            :class="['ml-1 px-2 py-1 rounded-md text-xs transition-colors',
                     exitArmed ? 'bg-amber-500 text-slate-900 font-medium' : 'bg-white/10 hover:bg-white/20 text-slate-200']"
            :title="isFullscreen ? (exitArmed ? '再点一次退出全屏' : '退出全屏需要点两下') : '全屏显示'">
            {{ isFullscreen ? (exitArmed ? '再点退出' : '⤡') : '⤢' }}
          </button>
        </div>
        <!-- Horizontal scroll on iPad portrait so tabs always fit; labels collapse to icons on narrow widths. -->
        <nav class="flex gap-1 text-sm overflow-x-auto -mx-1 px-1 py-0.5 no-scrollbar" aria-label="主导航">
          <button v-for="t in tabs" :key="t.id"
            :class="['inline-flex items-center px-2.5 py-1.5 rounded-lg shrink-0 transition-colors duration-150',
                     tab===t.id
                       ? 'bg-indigo-600 text-white font-medium shadow-sm'
                       : 'text-slate-300 hover:bg-white/10 hover:text-white']"
            :aria-current="tab===t.id ? 'page' : undefined"
            @click="tab=t.id">
            <span aria-hidden="true">{{ t.icon }}</span>
            <span class="hidden sm:inline ml-1.5">{{ t.label }}</span>
          </button>
        </nav>
      </div>
    </header>

    <main class="flex-1 p-2 sm:p-4 max-w-7xl w-full mx-auto">
      <VoicePanel v-show="tab==='voice'" :settings="settings" :refresh-key="refreshKey" @changed="bumpRefresh" />
      <ItemList v-show="tab==='items'" :refresh-key="refreshKey" @changed="bumpRefresh" />
      <LocationManager v-show="tab==='locations'" :refresh-key="refreshKey" @changed="bumpRefresh" />
      <keep-alive>
        <BuildingPanel v-if="tab==='building'" :refresh-key="refreshKey" @changed="bumpRefresh" />
      </keep-alive>
      <TransactionFeed v-show="tab==='log'" :refresh-key="refreshKey" />
      <AuditPanel v-show="tab==='audit'" :refresh-key="refreshKey" />
      <LogsPanel v-show="tab==='logs'" />
      <BackupPanel v-show="tab==='backup'" />
      <SettingsPanel v-show="tab==='settings'" @saved="loadSettings" />
    </main>

    <footer class="text-center text-xs text-slate-400 py-3">
      运行于本地 · 数据仅存储于你的 NAS
    </footer>
  </div>
</template>
