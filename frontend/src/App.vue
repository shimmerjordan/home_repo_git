<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import VoicePanel from './components/VoicePanel.vue'
import ItemList from './components/ItemList.vue'
import LocationManager from './components/LocationManager.vue'
import SettingsPanel from './components/SettingsPanel.vue'
import TransactionFeed from './components/TransactionFeed.vue'
import LogsPanel from './components/LogsPanel.vue'
import BuildingPanel from './components/BuildingPanel.vue'
import AuditPanel from './components/AuditPanel.vue'
import { api } from './api'

const tab = ref('voice')
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
function _fsElement() {
  return document.fullscreenElement || document.webkitFullscreenElement || null
}
function syncFs() { isFullscreen.value = !!_fsElement() || document.documentElement.classList.contains('faux-fullscreen') }
async function toggleFullscreen() {
  const el = document.documentElement
  const inFs = !!_fsElement()
  try {
    if (inFs) {
      const ex = document.exitFullscreen || document.webkitExitFullscreen
      if (ex) await ex.call(document)
    } else {
      const rq = el.requestFullscreen || el.webkitRequestFullscreen
      if (rq) { await rq.call(el); return syncFs() }
      // Fallback for iPad Safari without fullscreen API.
      el.classList.toggle('faux-fullscreen')
    }
  } catch {
    el.classList.toggle('faux-fullscreen')
  }
  syncFs()
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
  { id: 'settings', label: '设置', icon: '⚙' },
]
</script>

<template>
  <div class="min-h-full flex flex-col">
    <header class="bg-slate-900 text-white px-3 py-2 sm:px-4 sm:py-3 sticky top-0 z-10">
      <div class="flex items-center justify-between gap-2 flex-wrap">
        <div class="font-semibold flex items-center gap-2 shrink-0">
          🏠 语音仓储管家
          <button @click="toggleFullscreen"
            class="ml-1 px-2 py-1 rounded text-xs bg-white/10 hover:bg-white/20"
            :title="isFullscreen ? '退出全屏' : '全屏显示'">
            {{ isFullscreen ? '⤡' : '⤢' }}
          </button>
        </div>
        <!-- Horizontal scroll on iPad portrait so tabs always fit; labels collapse to icons on narrow widths. -->
        <nav class="flex gap-1 text-sm overflow-x-auto -mx-1 px-1 no-scrollbar">
          <button v-for="t in tabs" :key="t.id"
            :class="['px-2.5 py-1 rounded shrink-0', tab===t.id ? 'bg-white text-slate-900' : 'hover:bg-slate-800']"
            @click="tab=t.id">
            <span>{{ t.icon }}</span>
            <span class="hidden sm:inline ml-1">{{ t.label }}</span>
          </button>
        </nav>
      </div>
    </header>

    <main class="flex-1 p-2 sm:p-4 max-w-7xl w-full mx-auto">
      <VoicePanel v-show="tab==='voice'" :settings="settings" :refresh-key="refreshKey" @changed="bumpRefresh" />
      <ItemList v-show="tab==='items'" :refresh-key="refreshKey" @changed="bumpRefresh" />
      <LocationManager v-show="tab==='locations'" :refresh-key="refreshKey" @changed="bumpRefresh" />
      <BuildingPanel v-show="tab==='building'" :refresh-key="refreshKey" @changed="bumpRefresh" />
      <TransactionFeed v-show="tab==='log'" :refresh-key="refreshKey" />
      <AuditPanel v-show="tab==='audit'" :refresh-key="refreshKey" />
      <LogsPanel v-show="tab==='logs'" />
      <SettingsPanel v-show="tab==='settings'" @saved="loadSettings" />
    </main>

    <footer class="text-center text-xs text-slate-400 py-3">
      运行于本地 · 数据仅存储于你的 NAS
    </footer>
  </div>
</template>
