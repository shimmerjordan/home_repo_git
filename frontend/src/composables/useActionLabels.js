import { ref, watch } from 'vue'

// 快捷操作按钮是显示文字标签, 还是只显示图标。
//
// 为什么需要它: iPad 没有鼠标悬停, title 提示永远看不到, 5 个图标里还有两个是
// 破坏性操作(用完了 / 删除) —— 靠猜图标很危险。给用户一个开关自己决定要不要常驻文字,
// 比"看一眼就消失的提示"有用。
//
// 模块级单例, 不是每个组件实例各自持有: 一屏可能有十几条记录, 逐个切换是荒谬的。
const KEY = 'storage.quickActions.showLabels'

function load() {
  try {
    return localStorage.getItem(KEY) === '1'
  } catch {
    return false
  }
}

const showLabels = ref(load())

watch(showLabels, (v) => {
  try { localStorage.setItem(KEY, v ? '1' : '0') } catch {}
})

// 与 VoicePanel 里几个偏好项一致: 跟随其他标签页的改动。
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (ev) => {
    if (ev.key === KEY) showLabels.value = load()
  })
}

export function useActionLabels() {
  return {
    showLabels,
    toggleLabels: () => { showLabels.value = !showLabels.value },
  }
}
