// frontend/src/composables/usePausablePoll.js
// 绑定可见性的 setInterval。以前诊断页 3s / 语音页 30s 的轮询从开机跑到关页,
// 不管用户在哪个 tab、浏览器是否切到后台。这里三种情况停表:
//   1. 组件被 <keep-alive> 换出 (onDeactivated)
//   2. 页面不可见 (document.hidden)
//   3. 组件卸载
// 回到前台时先立刻刷一次 (fn), 再恢复节奏, 这样切回来不会看到陈旧数据。
import { onMounted, onBeforeUnmount, onActivated, onDeactivated } from 'vue'

export function usePausablePoll(fn, intervalMs) {
  let timer = null
  let active = true          // 在 keep-alive 的"前台"槽位
  let mountedOnce = false

  const start = () => {
    if (timer || !active || (typeof document !== 'undefined' && document.hidden)) return
    timer = setInterval(fn, intervalMs)
  }
  const stop = () => { if (timer) { clearInterval(timer); timer = null } }
  const onVis = () => {
    if (document.hidden) stop()
    else { if (active) fn(); start() }
  }

  onMounted(() => {
    mountedOnce = true
    document.addEventListener('visibilitychange', onVis)
    start()
  })
  onActivated(() => {
    active = true
    // keep-alive 首次挂载会紧跟一次 activated; 那次 onMounted 刚跑过 fn, 不重复。
    if (mountedOnce) { mountedOnce = false; start(); return }
    fn()
    start()
  })
  onDeactivated(() => { active = false; stop() })
  onBeforeUnmount(() => {
    stop()
    document.removeEventListener('visibilitychange', onVis)
  })
  return { start, stop }
}
