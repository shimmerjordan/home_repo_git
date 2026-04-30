<script setup>
// Shared editor for the "level structure" trio:
//   - 本身收纳层数 (own levels)              — only for containers (not rooms)
//   - 在父第几层 (level inside parent)        — only when parent has levels >= 2
//   - 同层第几个 (slot within layer)          — only when level is set
//   - 起始高度 mm (mount height in mm)       — only when level is "free"
//
// All fields update `modelValue` (object: { levels, level, slot, mount_y_mm }).
// Used by both LocationManager modal and BuildingPanel properties panel
// so the logic and UI stay consistent.

import { computed } from 'vue'
import { catalogFor, effectiveGeometry, levelUsage } from '../composables/sceneLayout'

const props = defineProps({
  modelValue: { type: Object, required: true },     // { levels, level, slot, mount_y_mm }
  selectedLoc: { type: Object, default: null },     // the location being edited (for layer-usage warning)
  parentLoc: { type: Object, default: null },       // its parent
  allLocations: { type: Array, default: () => [] }, // for layer-usage computation
  // Optional override for the width used in usage preview (when user is editing w live).
  liveWidth: { type: Number, default: null },
  compact: { type: Boolean, default: false },        // smaller spacing for modals
})
const emit = defineEmits(['update:modelValue'])

function set(field, val) {
  emit('update:modelValue', { ...props.modelValue, [field]: val })
}

const cat = computed(() => props.selectedLoc ? catalogFor(props.selectedLoc.kind) : null)
const showOwnLevels = computed(() => !!cat.value?.container && cat.value?.kind !== 'room')

const parentGeo = computed(() => props.parentLoc ? effectiveGeometry(props.parentLoc) : null)
const parentHasLevels = computed(() => (parentGeo.value?.levels || 0) >= 2)
const showLevel = computed(() => parentHasLevels.value)
const showSlot = computed(() => parentHasLevels.value && (props.modelValue.level || 0) >= 1)
const showMountY = computed(() => !parentHasLevels.value || (props.modelValue.level || 0) === 0)

// Live layer usage preview for the currently picked level.
const layerInfo = computed(() => {
  if (!props.parentLoc || !parentHasLevels.value) return null
  const lv = +props.modelValue.level || 0
  if (!lv) return null
  const usage = levelUsage(props.allLocations, props.parentLoc.id, lv)
  let preview = 0
  for (const k of usage.kids) {
    if (k.id === props.selectedLoc?.id) {
      preview += (props.liveWidth != null ? +props.liveWidth : effectiveGeometry(k).w)
    } else {
      preview += effectiveGeometry(k).w
    }
  }
  return { preview, max: parentGeo.value.w, count: usage.count, overflow: preview > parentGeo.value.w + 0.001 }
})
</script>

<template>
  <div :class="['grid gap-2 text-sm', compact ? 'grid-cols-2' : 'grid-cols-2 md:grid-cols-4']">
    <div v-if="showOwnLevels">
      <label class="label">本身收纳层数</label>
      <input :value="modelValue.levels" type="number" step="1" min="0" max="20" class="input"
             @input="set('levels', +$event.target.value || 0)" />
      <div class="text-[11px] text-slate-500 mt-0.5">0=单层。≥2 时 3D 显示隔板。</div>
    </div>

    <div v-if="showLevel">
      <label class="label">在父第几层</label>
      <select :value="modelValue.level" class="input"
              @change="set('level', +$event.target.value || 0)">
        <option :value="0">— 自由 —</option>
        <option v-for="i in parentGeo.levels" :key="i" :value="i">第 {{ i }} 层 (1=底)</option>
      </select>
    </div>

    <div v-if="showSlot">
      <label class="label">同层第几个 (slot)</label>
      <input :value="modelValue.slot" type="number" step="1" min="0" class="input"
             @input="set('slot', +$event.target.value || 0)" />
      <div class="text-[11px] text-slate-500 mt-0.5">≥1 时按 slot 从左到右排</div>
    </div>

    <div v-if="showMountY">
      <label class="label">起始高度 (mm)</label>
      <input :value="modelValue.mount_y_mm" type="number" step="10" class="input"
             @input="set('mount_y_mm', +$event.target.value || 0)" />
      <div class="text-[11px] text-slate-500 mt-0.5">挂壁柜设非零, 例如 1200</div>
    </div>
  </div>

  <div v-if="layerInfo" class="mt-1 text-xs"
       :class="layerInfo.overflow ? 'text-red-600' : 'text-slate-500'">
    本层占用: {{ layerInfo.preview.toFixed(2) }}m / {{ layerInfo.max.toFixed(2) }}m ({{ layerInfo.count }} 个)
    <span v-if="layerInfo.overflow">⚠ 超出 {{ (layerInfo.preview - layerInfo.max).toFixed(2) }}m</span>
  </div>
</template>
