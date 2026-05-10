<script setup>
import { ref, onMounted } from 'vue'
import VoicePanel from './components/VoicePanel.vue'
import ItemList from './components/ItemList.vue'
import LocationManager from './components/LocationManager.vue'
import SettingsPanel from './components/SettingsPanel.vue'
import TransactionFeed from './components/TransactionFeed.vue'
import LogsPanel from './components/LogsPanel.vue'
import BuildingPanel from './components/BuildingPanel.vue'
import { api } from './api'

const tab = ref('voice')
const refreshKey = ref(0)
const settings = ref(null)

async function loadSettings() {
  try { settings.value = await api.getSettings() } catch (e) { console.warn(e) }
}
onMounted(loadSettings)

function bumpRefresh() { refreshKey.value += 1 }

const tabs = [
  { id: 'voice', label: '语音', icon: '🎙' },
  { id: 'items', label: '物品', icon: '📦' },
  { id: 'locations', label: '位置', icon: '🗂' },
  { id: 'building', label: '3D', icon: '🏗' },
  { id: 'log', label: '记录', icon: '📜' },
  { id: 'logs', label: '诊断', icon: '🔧' },
  { id: 'settings', label: '设置', icon: '⚙' },
]
</script>

<template>
  <div class="min-h-full flex flex-col">
    <header class="bg-slate-900 text-white px-3 py-2 sm:px-4 sm:py-3 sticky top-0 z-10">
      <div class="flex items-center justify-between gap-2 flex-wrap">
        <div class="font-semibold flex items-center gap-2 shrink-0">🏠 语音仓储管家</div>
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
      <LogsPanel v-show="tab==='logs'" />
      <SettingsPanel v-show="tab==='settings'" @saved="loadSettings" />
    </main>

    <footer class="text-center text-xs text-slate-400 py-3">
      运行于本地 · 数据仅存储于你的 NAS
    </footer>
  </div>
</template>
