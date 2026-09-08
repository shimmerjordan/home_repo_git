<script setup>
// 分页条 —— 上一页 / 页码 / 下一页 + 每页条数。
//
// 两种数据源都要伺候:
//   - 本地分页 (待补充/待归位): 知道总数, 传 total + page-count
//   - 服务端分页 (近期取放记录): 只靠多要 1 条前瞻知道有没有下一页,
//     所以 page-count 传 null, 由 has-next 决定「下一页」灰不灰
import { computed } from 'vue'
import { PAGE_SIZES } from '../composables/usePagedList'

const props = defineProps({
  page: { type: Number, required: true },
  pageSize: { type: Number, required: true },
  pageCount: { type: [Number, null], default: null },  // null = 总页数未知
  total: { type: [Number, null], default: null },
  hasNext: { type: Boolean, default: false },          // 仅在 pageCount 为 null 时生效
})
const emit = defineEmits(['update:page', 'update:pageSize'])

const canPrev = computed(() => props.page > 1)
const canNext = computed(() =>
  props.pageCount == null ? props.hasNext : props.page < props.pageCount)

// 什么时候显示这一条。翻得动就显示是显然的; 第三个条件是补一个坑: 每页调到 20
// 之后总共只有 15 条 —— 前后都翻不动, 整条控件跟着消失, 于是**再也调不回 10**。
// 只要用户主动调离过最小档, 就得让他有路回来。
const visible = computed(() =>
  canPrev.value || canNext.value || props.pageSize !== PAGE_SIZES[0])
</script>

<template>
  <!-- 只有一页且用的是默认条数时整条藏起来 —— 三五条东西不该配一排翻页控件 -->
  <div v-if="visible"
       class="flex items-center justify-between gap-2 pt-2 mt-1 border-t border-slate-200/70 text-xs">
    <div class="text-slate-500 tabular-nums">
      <template v-if="total != null">共 {{ total }} 条 · 第 {{ page }}/{{ pageCount }} 页</template>
      <template v-else>第 {{ page }} 页</template>
    </div>
    <div class="flex items-center gap-1.5">
      <label class="text-slate-500">
        每页
        <select :value="pageSize" class="ml-1 px-1.5 py-1 rounded border border-slate-300 bg-white"
                @change="emit('update:pageSize', +$event.target.value)">
          <option v-for="n in PAGE_SIZES" :key="n" :value="n">{{ n }}</option>
        </select>
      </label>
      <button class="btn btn-secondary text-xs px-2 py-1" :disabled="!canPrev"
              title="上一页" @click="emit('update:page', page - 1)">‹</button>
      <button class="btn btn-secondary text-xs px-2 py-1" :disabled="!canNext"
              title="下一页" @click="emit('update:page', page + 1)">›</button>
    </div>
  </div>
</template>
