// Centralized location-edit pipeline that records inverse operations for undo.
// All mutations to /api/locations should go through `applyEdit(action)` instead
// of calling `api.*` directly, so an undo entry is captured.
//
// Action shapes:
//   { kind: 'create', payload: <LocationCreate> }   → undo: delete by id
//   { kind: 'update', id, patch: <LocationUpdate>, before: <prev fields> }
//                                                   → undo: PATCH back to before
//   { kind: 'delete', id, payload: <LocationCreate> }
//                                                   → undo: recreate (NEW id; children orphans warned)
//   { kind: 'batch', label, updates: [{ id, patch, before }] }
//                                                   → undo: PATCH every id back, as ONE step
//
// batch 存在的理由: 「本层排开」一次要挪一层里的每一个子级。逐条 applyEdit 会压进
// N 条历史, 用户得连按 N 次撤销才回得去 —— 一个动作就该只对应一次撤销。
//
// Only single-level undo per action; redo is intentionally omitted (delete+recreate
// changes ids and we don't want to maintain id-remap).
import { ref } from 'vue'
import { api } from '../api'

const MAX_HISTORY = 60

export function useEditHistory() {
  const stack = ref([])
  const busy = ref(false)
  const lastError = ref('')

  const canUndo = () => stack.value.length > 0
  const undoLabel = () => stack.value.length ? labelOf(stack.value[stack.value.length - 1]) : ''

  function labelOf(a) {
    if (a.type === 'create') return `创建"${a.name || a.kind || ''}"`
    if (a.type === 'update') return `修改"${a.before?.name || ''}"`
    if (a.type === 'delete') return `删除"${a.payload?.name || ''}"`
    if (a.type === 'batch') return a.label || '批量修改'
    return '操作'
  }

  async function applyEdit(action) {
    busy.value = true
    lastError.value = ''
    try {
      if (action.kind === 'create') {
        const loc = await api.createLocation(action.payload)
        stack.value.push({ type: 'create', id: loc.id, kind: action.payload.kind, name: action.payload.name })
        cap()
        return loc
      }
      if (action.kind === 'update') {
        if (!action.before) {
          throw new Error('update action requires `before` snapshot')
        }
        await api.updateLocation(action.id, action.patch)
        stack.value.push({ type: 'update', id: action.id, before: action.before })
        cap()
        return null
      }
      if (action.kind === 'delete') {
        await api.deleteLocation(action.id)
        stack.value.push({ type: 'delete', payload: action.payload })
        cap()
        return null
      }
      if (action.kind === 'batch') {
        const updates = action.updates || []
        if (!updates.length) return null
        if (updates.some((u) => !u.before)) {
          throw new Error('batch action requires a `before` snapshot per update')
        }
        // 串行发 —— 后端是 SQLite 单写, 并发 PATCH 只会互相等锁。
        const done = []
        try {
          for (const u of updates) {
            await api.updateLocation(u.id, u.patch)
            done.push(u)
          }
        } catch (e) {
          // 中途失败: 已经成功的那几条仍然要能撤, 否则布局停在半改状态且撤不回去。
          if (done.length) {
            stack.value.push({ type: 'batch', label: action.label, updates: done })
            cap()
          }
          throw e
        }
        stack.value.push({ type: 'batch', label: action.label, updates: done })
        cap()
        return null
      }
      throw new Error('unknown action kind: ' + action.kind)
    } catch (e) {
      lastError.value = String(e.message || e)
      throw e
    } finally {
      busy.value = false
    }
  }

  function cap() {
    if (stack.value.length > MAX_HISTORY) stack.value = stack.value.slice(-MAX_HISTORY)
  }

  async function undo() {
    const a = stack.value.pop()
    if (!a) return
    busy.value = true
    try {
      if (a.type === 'create') {
        await api.deleteLocation(a.id).catch(() => {})
      } else if (a.type === 'update') {
        await api.updateLocation(a.id, a.before).catch(() => {})
      } else if (a.type === 'delete') {
        await api.createLocation(a.payload).catch(() => {})
      } else if (a.type === 'batch') {
        for (const u of a.updates || []) {
          await api.updateLocation(u.id, u.before).catch(() => {})
        }
      }
    } finally {
      busy.value = false
    }
  }

  function clear() {
    stack.value = []
  }

  // Convenience: snapshot the editable fields of a location so the caller can
  // attach `before` to an update action.
  function snapshot(loc) {
    if (!loc) return null
    return {
      name: loc.name,
      kind: loc.kind,
      parent_id: loc.parent_id,
      note: loc.note,
      geometry: loc.geometry || null,
    }
  }

  return { stack, busy, lastError, canUndo, undoLabel, applyEdit, undo, snapshot, clear }
}
