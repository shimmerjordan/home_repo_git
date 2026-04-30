<script setup>
import { computed } from 'vue'
import TreeNode from './LocationTreeNode.vue'

const props = defineProps({
  locations: { type: Array, default: () => [] },
  selectedId: { type: [Number, null], default: null },
  itemCounts: { type: Object, default: () => ({}) }, // includes descendant counts
})
const emit = defineEmits(['select'])

const tree = computed(() => {
  const byId = new Map()
  for (const l of props.locations) byId.set(l.id, { ...l, children: [] })
  const roots = []
  for (const node of byId.values()) {
    if (node.parent_id && byId.has(node.parent_id)) {
      byId.get(node.parent_id).children.push(node)
    } else {
      roots.push(node)
    }
  }
  const sortRec = (nodes) => {
    nodes.sort((a, b) => a.name.localeCompare(b.name, 'zh'))
    nodes.forEach((n) => sortRec(n.children))
  }
  sortRec(roots)
  return roots
})

const total = computed(() => Object.values(props.itemCounts).reduce((a, b) => a + b, 0))
</script>

<template>
  <div class="text-sm">
    <button
      :class="['w-full text-left px-2 py-1.5 rounded flex items-center justify-between',
               selectedId === null ? 'bg-slate-900 text-white' : 'hover:bg-slate-100']"
      @click="emit('select', null)">
      <span>📦 全部物品</span>
      <span class="text-xs opacity-70">{{ total }}</span>
    </button>
    <button
      :class="['w-full text-left px-2 py-1.5 rounded flex items-center justify-between',
               selectedId === 0 ? 'bg-slate-900 text-white' : 'hover:bg-slate-100']"
      @click="emit('select', 0)">
      <span>❓ 未指定位置</span>
      <span class="text-xs opacity-70">{{ itemCounts[0] || 0 }}</span>
    </button>
    <ul class="mt-1">
      <TreeNode v-for="n in tree" :key="n.id" :node="n" :selected-id="selectedId"
                :counts="itemCounts" :depth="0" @select="(id) => emit('select', id)" />
    </ul>
  </div>
</template>
