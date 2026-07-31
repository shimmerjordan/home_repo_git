# 识别结果快捷操作 + 移除小程序 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在识别结果与近期流水的物品条目上加 5 个快捷操作按钮,并从仓库彻底移除微信小程序。

**Architecture:** 抽出独立组件 `ItemQuickActions.vue`,三处接入点复用;展开面板时按需拉单条物品快照。小程序移除是纯删除 + 三处引用清理,外加删掉一段专为它写的备份恢复回退逻辑。

**Tech Stack:** Vue 3 `<script setup>` + Tailwind(自定义 `btn`/`input`/`card` 工具类)、FastAPI + SQLAlchemy。

## Global Constraints

- 项目**无任何自动化测试基础设施**(无 pytest、无 vitest)。本计划不引入测试框架 —— 那是独立决策。
  验证靠 `npm run build`、后端导入检查与手工清单。
- 前端样式只用 [style.css](../../../frontend/src/style.css) 已有的
  `btn / btn-primary / btn-secondary / btn-danger / input / card / tag / label`,不新增样式。
- 注释与 UI 文案一律简体中文,与现有代码保持一致。
- commit message **不带任何 Co-Authored-By 之类的尾注**。
- 两个 spec 依据:
  [快捷操作](../specs/2026-07-31-item-quick-actions-design.md)、
  [移除小程序](../specs/2026-07-31-remove-miniprogram-design.md)。

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `frontend/src/api.js` | REST 封装 | 加 `getItem` |
| `frontend/src/components/ItemQuickActions.vue` | 单个物品的 5 个快捷操作 + 内联展开面板,自带快照/忙碌/错误状态 | 新建 |
| `frontend/src/components/VoicePanel.vue` | 三处接入 + 一个刷新 handler | 修改 |
| `miniprogram/`、`docs/*miniprogram*.md` | 小程序 | 删除 |
| `README.md`、`docs/backup.md`、`backend/app/services/backup.py` | 小程序引用与专用回退逻辑 | 修改 |

---

### Task 1: api.js 增加 getItem

**Files:**
- Modify: `frontend/src/api.js:29`

**Interfaces:**
- Produces: `api.getItem(id) -> Promise<Item>`,Item 含
  `{id, name, aliases, category, tags, quantity, price, note, location_id, pos_x, pos_z, location_path, created_at, updated_at}`
  (见 [inventory.py:26-42](../../../backend/app/services/inventory.py#L26-L42))

- [ ] **Step 1: 在 `deleteItem` 上方插入一行**

```js
  getItem: (id) => request(`/api/items/${id}`),
```

- [ ] **Step 2: 确认后端端点存在**

Run: `grep -n 'items/{item_id}"' backend/app/routers/items.py`
Expected: 命中 `@router.get("/items/{item_id}")`

---

### Task 2: 新建 ItemQuickActions.vue

**Files:**
- Create: `frontend/src/components/ItemQuickActions.vue`

**Interfaces:**
- Consumes: `api.getItem` (Task 1)、`api.recordTx`、`api.updateItem`、`api.deleteItem`
- Produces: 组件 props `{ itemId: Number, locations: Array, compact: Boolean }`,emit `done`

**关键实现约束:**

1. **快照按需加载**。不在 `onMounted` 拉 —— 一屏可能有 15 个条目,会打出 15 个请求。
   只在首次展开面板 / 点「用完了」/ 点「删除」时拉一次。
2. **「用完了」必须用 `action:'adjust', quantity:0`**,不能用 `consume`。后端
   [items.py:369-374](../../../backend/app/routers/items.py#L369-L374) 的分支只有
   `take_out / put_in / adjust`,`consume` 不改数量。
3. **数量为 0 的禁用发生在快照加载之后**(面板内),不是按钮上 —— 否则要预拉请求。
4. 所有按钮 `@click.stop`,因为推荐用品那一处的父行有 `@click` 会触发 LLM 重放。
5. 操作成功后把 `snapshot` 置 null,下次展开重新拉,避免用过期数量。

- [ ] **Step 1: 写入完整组件**

```vue
<script setup>
import { ref, computed } from 'vue'
import { api } from '../api'

// 识别结果 / 流水条目上的快捷操作。绕过语音与 LLM, 直接打 REST。
const props = defineProps({
  itemId: { type: Number, required: true },
  locations: { type: Array, default: () => [] },
  // 窄卡片(1/3 宽)里按钮排不进内容行, compact 时自己占一行。
  compact: { type: Boolean, default: false },
})
const emit = defineEmits(['done'])

const open = ref('')          // '' | 'take' | 'put' | 'move'
const snapshot = ref(null)    // GET /api/items/{id} 结果
const loadErr = ref('')
const qty = ref(1)
const targetLocId = ref(null)
const busy = ref(false)
const err = ref('')

const quantity = computed(() => snapshot.value?.quantity ?? 0)

// 按需拉快照, 不在 mounted 拉: 一屏十几个条目会打出十几个请求。
// 必须拉而不是复用 VoicePanel 的 sceneItems, 因为后者排除了 quantity=0 的物品,
// 且候选项数据里没有 location_id。
async function ensureSnapshot() {
  if (snapshot.value || loadErr.value) return
  try {
    snapshot.value = await api.getItem(props.itemId)
    targetLocId.value = snapshot.value.location_id ?? null
  } catch (e) {
    const msg = String(e.message || e)
    loadErr.value = msg.includes('404') ? '该物品已删除' : '读取物品失败: ' + msg
  }
}

async function toggle(panel) {
  err.value = ''
  if (open.value === panel) { open.value = ''; return }
  await ensureSnapshot()
  if (loadErr.value) { open.value = ''; return }
  qty.value = 1
  targetLocId.value = snapshot.value?.location_id ?? null
  open.value = panel
}

function bump(d) {
  // 取出不能超过库存; 补库存无上限。
  const max = open.value === 'take' ? Math.max(1, quantity.value) : 9999
  qty.value = Math.min(max, Math.max(1, qty.value + d))
}

async function run(fn) {
  if (busy.value) return
  busy.value = true
  err.value = ''
  try {
    await fn()
    open.value = ''
    snapshot.value = null   // 强制下次重新取, 避免用过期数量
    emit('done')
  } catch (e) {
    err.value = String(e.message || e)   // 不吞异常, 面板保持展开让用户重试
  } finally {
    busy.value = false
  }
}

function confirmTake() {
  return run(() => api.recordTx(props.itemId, {
    item_id: props.itemId, action: 'take_out', quantity: qty.value,
    location_id: snapshot.value?.location_id ?? null,
  }))
}

function confirmPut() {
  return run(() => api.recordTx(props.itemId, {
    item_id: props.itemId, action: 'put_in', quantity: qty.value,
    location_id: snapshot.value?.location_id ?? null,
  }))
}

// 换位置不记流水: transaction 没有 move 动作, 而 put_in 带 location_id 会同时加数量。
function confirmMove() {
  return run(() => api.updateItem(props.itemId, { location_id: targetLocId.value || null }))
}

// 清零只能用 adjust: 后端没有 consume 分支, consume 只写流水不改数量
// (它是用来销掉"待归位"欠账的)。
async function markUsedUp() {
  err.value = ''
  await ensureSnapshot()
  if (loadErr.value || !snapshot.value) return
  if (quantity.value <= 0) { err.value = '数量已经是 0'; return }
  if (!confirm(`确认「${snapshot.value.name}」用完了?数量清零后进入待补充列表,以后还能补货复活。`)) return
  run(() => api.recordTx(props.itemId, {
    item_id: props.itemId, action: 'adjust', quantity: 0, note: '用完清零',
  }))
}

async function removeItem() {
  err.value = ''
  await ensureSnapshot()
  if (loadErr.value || !snapshot.value) return
  if (!confirm(`确认永久删除「${snapshot.value.name}」?物品记录会消失,审计日志保留。`)) return
  run(() => api.deleteItem(props.itemId))
}
</script>

<template>
  <div :class="compact ? 'mt-1' : ''">
    <!-- 全部 @click.stop: 推荐用品那一处的父行有 @click 会触发 LLM 意图重放 -->
    <div class="flex items-center gap-1 flex-wrap">
      <button class="btn btn-secondary text-xs" :disabled="busy" title="取出"
              @click.stop="toggle('take')">−</button>
      <button class="btn btn-secondary text-xs" :disabled="busy" title="补库存"
              @click.stop="toggle('put')">⇪</button>
      <button class="btn btn-secondary text-xs" :disabled="busy" title="换位置"
              @click.stop="toggle('move')">⇄</button>
      <button class="btn btn-secondary text-xs" :disabled="busy" title="用完了 (清零, 可补货复活)"
              @click.stop="markUsedUp">⊗</button>
      <button class="btn btn-danger text-xs" :disabled="busy" title="永久删除"
              @click.stop="removeItem">🗑</button>
    </div>

    <div v-if="loadErr" class="mt-1 text-xs text-rose-600">{{ loadErr }}</div>
    <div v-else-if="err" class="mt-1 text-xs text-rose-600">{{ err }}</div>

    <div v-if="open === 'take' || open === 'put'"
         class="mt-1 flex items-center gap-2 bg-slate-50 rounded-lg p-2 flex-wrap" @click.stop>
      <span class="text-xs text-slate-500">{{ open === 'take' ? '取出' : '补库存' }}</span>
      <button class="btn btn-secondary text-xs" :disabled="busy" @click="bump(-1)">−</button>
      <span class="font-mono w-8 text-center">{{ qty }}</span>
      <button class="btn btn-secondary text-xs" :disabled="busy" @click="bump(1)">+</button>
      <span v-if="open === 'take'" class="text-xs"
            :class="quantity > 0 ? 'text-slate-400' : 'text-rose-600'">库存 {{ quantity }}</span>
      <span class="flex-1"></span>
      <button class="btn btn-secondary text-xs" :disabled="busy" @click="open = ''">取消</button>
      <button class="btn btn-primary text-xs"
              :disabled="busy || (open === 'take' && quantity <= 0)"
              @click="open === 'take' ? confirmTake() : confirmPut()">确认</button>
    </div>

    <div v-if="open === 'move'"
         class="mt-1 flex items-center gap-2 bg-slate-50 rounded-lg p-2 flex-wrap" @click.stop>
      <span class="text-xs text-slate-500">移到</span>
      <select v-model="targetLocId" class="input text-xs flex-1 min-w-[140px]" :disabled="busy">
        <option :value="null">—</option>
        <option v-for="l in locations" :key="l.id" :value="l.id">{{ l.full_path }}</option>
      </select>
      <button class="btn btn-secondary text-xs" :disabled="busy" @click="open = ''">取消</button>
      <button class="btn btn-primary text-xs" :disabled="busy" @click="confirmMove">确认</button>
    </div>
  </div>
</template>
```

- [ ] **Step 2: 构建检查**

Run: `cd frontend && npm run build`
Expected: 构建成功,无 Vue 编译错误

---

### Task 3: VoicePanel 三处接入

**Files:**
- Modify: `frontend/src/components/VoicePanel.vue`(import、handler、三处模板)

**Interfaces:**
- Consumes: `ItemQuickActions`(Task 2)、已有的 `sceneLocations`

- [ ] **Step 1: 加 import**

在 `import Waveform from './Waveform.vue'` 下方:

```js
import ItemQuickActions from './ItemQuickActions.vue'
```

- [ ] **Step 2: 加统一刷新 handler**

放在 `timeAgo` 函数上方:

```js
// 快捷操作成功后统一刷新: 流水、3D 场景、待归位、待补充四个列表都可能受影响。
function onQuickActionDone() {
  loadRecent(); loadScene(); loadPending(); loadDepleted()
  emit('changed')
}
```

- [ ] **Step 3: 候选物品条目改成两行**

把 `<li v-for="c in result.candidates" ...>` 整块替换为:

```html
              <li v-for="c in result.candidates" :key="c.item_id"
                  class="bg-white border border-slate-200 rounded-lg p-2">
                <div class="flex items-center justify-between gap-2">
                  <span class="min-w-0 truncate">
                    <span class="font-medium">{{ c.item_name }}</span>
                    <span class="text-slate-500 ml-2 text-xs">{{ c.location_path || '未指定位置' }}</span>
                  </span>
                  <button class="btn btn-secondary text-xs flex-shrink-0" :disabled="phase !== 'idle'"
                          @click="pickCandidate(c)">选这个</button>
                </div>
                <ItemQuickActions :item-id="c.item_id" :locations="sceneLocations" compact
                                  @done="onQuickActionDone" />
              </li>
```

- [ ] **Step 4: 推荐用品加第二行**

表头不动(仍 3 列)。把 `<tbody>` 里的 `<tr v-for="rec ...">` 换成 `<template v-for>` 包两行:

```html
                <template v-for="rec in result.recommendations" :key="rec.item_id">
                  <tr class="border-t border-slate-100 hover:bg-slate-50 cursor-pointer"
                      @click="pickCandidate({ item_id: rec.item_id, item_name: candidateNameOf(rec.item_id) })">
                    <td class="py-1 font-medium">{{ candidateNameOf(rec.item_id) }}</td>
                    <td class="py-1 text-slate-600">{{ rec.purpose }}</td>
                    <td class="py-1 text-slate-500 truncate">{{ candidateLocOf(rec.item_id) || '—' }}</td>
                  </tr>
                  <tr>
                    <td colspan="3" class="pb-1">
                      <ItemQuickActions :item-id="rec.item_id" :locations="sceneLocations" compact
                                        @done="onQuickActionDone" />
                    </td>
                  </tr>
                </template>
```

用 `colspan="3"` 独占一行而不是加第 4 列 —— 卡片只有 1/3 宽,4 列会把按钮挤成一列。

- [ ] **Step 5: 近期取放记录加按钮**

在 `<span class="text-xs text-slate-400">{{ fmtFull(t.created_at) }}</span>` 之后、`</li>` 之前插入:

```html
          <ItemQuickActions v-if="t.item_id" :item-id="t.item_id" :locations="sceneLocations"
                            @done="onQuickActionDone" />
```

该卡片是全宽,不用 compact。

- [ ] **Step 6: 构建检查**

Run: `cd frontend && npm run build`
Expected: 构建成功

- [ ] **Step 7: 提交**

```bash
git add frontend/src/api.js frontend/src/components/ItemQuickActions.vue frontend/src/components/VoicePanel.vue docs/superpowers/
git commit -m "识别结果与流水条目加取出/补库存/换位置/用完/删除快捷操作"
```

---

### Task 4: 删除小程序代码与文档

**Files:**
- Delete: `miniprogram/`、`docs/wechat-miniprogram.md`、`docs/deployment-miniprogram.md`
- Modify: `README.md:34-36`

- [ ] **Step 1: 删除目录与文档**

```bash
git rm -r --quiet miniprogram docs/wechat-miniprogram.md docs/deployment-miniprogram.md
```

- [ ] **Step 2: 删掉 README 文档导航表的三行**

删除 [README.md:34-36](../../../README.md#L34-L36) 这三行(`wechat-miniprogram`、
`deployment-miniprogram`、`miniprogram/`),其余行不动。

- [ ] **Step 3: 验证 README 里不再有小程序**

Run: `grep -n -iE "miniprogram|小程序" README.md`
Expected: 无输出

---

### Task 5: 清理 backup 的小程序专用回退逻辑

**Files:**
- Modify: `backend/app/services/backup.py`(docstring 第 5 行、restore 分支、删函数)
- Modify: `docs/backup.md:18`

**背景:** `_restore_database_from_json` 是专为"小程序备份没有裸 SQLite"写的。裸库只在勾选
`inventory` 组件时入包([backup.py:93-96](../../../backend/app/services/backup.py#L93-L96)),
所以只勾流水/审计的 NAS 包也没有裸库 —— 删掉回退后这类包恢复 database 会走空,
因此**换成显式报错**而不是静默跳过。`ValueError` 会被
[routers/backup.py:119-120](../../../backend/app/routers/backup.py#L119-L120) 转成 HTTP 400 + 原消息。

- [ ] **Step 1: 改模块 docstring 第 5 行**

```
  - **JSON 辅助**: 物品/位置/流水/审计另导出可读 JSON, 供人工查看备份内容。
```

- [ ] **Step 2: restore 里的 elif 分支换成 else 报错**

把:

```python
            elif any(n.startswith("data/") for n in names):
                # 回退: 无裸库时从 data/*.json 重建 (如小程序产出的备份, 实现跨端恢复)。
                _restore_database_from_json(zf, names)
                restored.append("database")
```

换成:

```python
            else:
                raise ValueError(
                    "备份包内没有裸 SQLite 快照 (db/storage.db), 无法恢复数据库。"
                    "该包可能只勾选了流水或审计组件。"
                )
```

- [ ] **Step 3: 删掉整个 `_restore_database_from_json` 函数**

从 `def _restore_database_from_json(zf, names)` 起,到下一个顶层 `def` 之前全部删除。

- [ ] **Step 4: 改 docs/backup.md 第 18 行**

```
> JSON 是可读导出, 便于人工查看备份内容。详见 [`backend/app/services/backup.py`](../backend/app/services/backup.py)。
```

- [ ] **Step 5: 后端能正常导入**

Run: `cd backend && python -c "from app.services import backup; print('ok')"`
Expected: 打印 `ok`,无 NameError(确认没有残留调用)

- [ ] **Step 6: 全仓验证小程序已清干净**

Run:
```bash
grep -rIn --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=dist -iE "miniprogram|小程序" .
```
Expected: 只剩 `docs/superpowers/` 下的 spec 与本计划;
`docs/bots/README.md` 的「企业微信」「微信个人号」是群机器人对比表,**不应被删**,
但它不含「小程序」字样所以不会命中。

- [ ] **Step 7: 提交并推送**

```bash
git add -A
git commit -m "移除微信小程序: 删除代码/文档/导航, 备份恢复去掉 JSON 重建回退"
git push origin main
```

---

## 手工验证清单(部署后)

1. 语音查一件物品 → 候选条目出现 5 个按钮,不遮挡「选这个」
2. 取出 2 件 → 数量减 2、「待归位」出现该条、流水出现 take_out
3. 补库存 3 件 → 数量加 3
4. 换位置 → 物品页位置更新、3D 高亮位置改变、审计页有字段变更记录
5. 用完了 → 从搜索消失、进「待补充」、补货能复活;流水出现「盘点」且备注「用完清零」
6. 删除 → 物品消失、审计保留
7. 点推荐用品行内按钮 → 「本次会话」**不增加**新条目(验证 `@click.stop` 生效)
8. 断网点任一按钮 → 面板内显示错误而非静默失败
9. 备份面板:只勾「操作流水」备份后恢复 database → 看到明确中文错误提示
