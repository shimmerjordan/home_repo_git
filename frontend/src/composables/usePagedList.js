// 本地分页 —— 给「整份数据已经在前端」的列表用 (待补充 / 待归位)。
//
// 服务端分页的列表 (近期取放记录) 不走这里: 它每 30s 轮询一次, 一次只该拉当前
// 页那几条, 所以由调用方直接带 offset 请求。这个 composable 只负责切片。
//
// 每页条数按 storageKey 分别记在 localStorage 里 —— 「待归位」通常只有几条,
// 「近期记录」想一次看 50 条, 让它们共用一个值只会互相打架。
import { ref, computed, watch, unref } from 'vue'

export const PAGE_SIZES = [10, 20, 50]

export function readPageSize(storageKey, fallback = 10) {
  try {
    const v = +localStorage.getItem(`storage.page.${storageKey}`)
    if (PAGE_SIZES.includes(v)) return v
  } catch {}
  return fallback
}

export function writePageSize(storageKey, v) {
  try { localStorage.setItem(`storage.page.${storageKey}`, String(v)) } catch {}
}

/**
 * @param source  ref/getter 指向完整数组
 * @param storageKey  每页条数的持久化键
 */
export function usePagedList(source, storageKey, fallbackSize = 10) {
  const pageSize = ref(readPageSize(storageKey, fallbackSize))
  const page = ref(1)
  watch(pageSize, (v) => { writePageSize(storageKey, v); page.value = 1 })

  const all = computed(() => {
    const v = typeof source === 'function' ? source() : unref(source)
    return Array.isArray(v) ? v : []
  })
  const total = computed(() => all.value.length)
  const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))

  // 删掉最后一页的最后一条后, page 会停在一个空页上 —— 用户看到的是「暂无」,
  // 还以为数据没了。数据一变就把越界的页码收回来。
  watch([total, pageSize], () => {
    if (page.value > pageCount.value) page.value = pageCount.value
  })

  const slice = computed(() => {
    const start = (page.value - 1) * pageSize.value
    return all.value.slice(start, start + pageSize.value)
  })

  return { page, pageSize, total, pageCount, slice }
}
