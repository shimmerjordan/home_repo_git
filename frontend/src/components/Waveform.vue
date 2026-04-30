<script setup>
const props = defineProps({
  levels: { type: Array, default: () => [] },
  active: { type: Boolean, default: false },
  height: { type: Number, default: 64 },
})
</script>

<template>
  <div class="flex items-end justify-center gap-[2px] w-full" :style="{ height: height + 'px' }">
    <div v-if="!levels.length" class="text-xs opacity-60 self-center">未启动音频采集</div>
    <template v-else>
      <div v-for="(v, i) in levels" :key="i"
           class="flex-1 rounded-sm transition-[height] duration-75 ease-out"
           :class="active ? 'bg-emerald-300' : 'bg-slate-300'"
           :style="{ height: Math.max(4, v * height) + 'px', opacity: active ? 0.6 + v * 0.4 : 0.4 }"></div>
    </template>
  </div>
</template>
