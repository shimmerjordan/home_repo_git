<script setup>
import { ref } from 'vue'

defineOptions({ name: 'LocationTreeNode' })

const props = defineProps({
  node: Object,
  selectedId: { type: [Number, null], default: null },
  counts: { type: Object, default: () => ({}) },
  depth: { type: Number, default: 0 },
})
const emit = defineEmits(['select'])

const open = ref(true)
const KIND_ICON = { room: '🏠', shelf: '📚', drawer: '🗄️', box: '📦', other: '📍' }
</script>

<template>
  <li>
    <div class="flex items-stretch">
      <button v-if="node.children && node.children.length"
              class="px-1 hover:bg-slate-100 rounded text-slate-400"
              @click="open = !open">
        {{ open ? '▾' : '▸' }}
      </button>
      <span v-else class="px-1 w-5"></span>
      <button
        :class="['flex-1 text-left px-2 py-1.5 rounded flex items-center justify-between',
                 selectedId === node.id ? 'bg-slate-900 text-white' : 'hover:bg-slate-100']"
        :style="{ paddingLeft: 4 + depth * 14 + 'px' }"
        @click="emit('select', node.id)">
        <span>{{ KIND_ICON[node.kind] || '📍' }} {{ node.name }}</span>
        <span class="text-xs opacity-70">{{ counts[node.id] || '' }}</span>
      </button>
    </div>
    <ul v-if="open && node.children?.length">
      <LocationTreeNode v-for="c in node.children" :key="c.id"
        :node="c" :selected-id="selectedId" :counts="counts" :depth="depth + 1"
        @select="(id) => emit('select', id)" />
    </ul>
  </li>
</template>
