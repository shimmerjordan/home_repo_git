# 识别结果条目快捷操作 — 设计

日期: 2026-07-31
状态: 待实施

## 背景

语音识别出结果后,「最新识别结果」卡片里的候选物品只有一个「选这个」按钮
([VoicePanel.vue:825](../../../frontend/src/components/VoicePanel.vue#L825)),推荐用品表格是整行点击等同
「选这个」。想对识别出来的物品做任何实际动作 —— 取出、补库存、换位置、删除 ——
都必须重新发起一轮语音输入,走完整的 SR → 确认 → LLM 意图链路。这在 iPad 上很累赘,
尤其是"刚找到东西,顺手取一个"这种高频场景。

「近期取放记录」列表同样只能看不能动。

## 目标

在识别结果与近期流水的物品条目上提供 5 个快捷操作,绕过语音与 LLM,直接调用 REST API。

## 非目标

- 不改语音状态机、不改 LLM 意图解析
- 不动「选这个」的语义(它走 LLM 重放,与快捷操作是两条路)
- 不动物品管理页 [ItemList.vue](../../../frontend/src/components/ItemList.vue) 已有的
  `+ / − / 编辑 / 删` 按钮(本次不重构它,避免扩大改动面)
- 不接入微信小程序(小程序正在被移除,见
  [2026-07-31-remove-miniprogram-design.md](2026-07-31-remove-miniprogram-design.md))

## 方案选择

抽出独立组件 `ItemQuickActions.vue`,三处接入点复用。

备选方案是在 VoicePanel 里内联三份模板,被否决:VoicePanel.vue 已 908 行,同时承担语音状态机、
3D 预览、待补充、待归位、近期流水五件事;再塞三份带展开态的操作面板会让文件失控。抽成组件后
每处接入只是一行标签,且后续若要挂到物品页也是一行。

## 组件设计

```
frontend/src/components/ItemQuickActions.vue   (新增, 约 180 行)

props:
  itemId     Number   必填
  locations  Array    位置列表, 复用调用方已有的 listLocations() 结果
  compact    Boolean  默认 false; true = 按钮换行到第二行(窄卡片用)

emits:
  done       任一操作成功后抛出, 调用方据此刷新

内部状态:
  open        '' | 'take' | 'put' | 'move'   当前展开的面板
  snapshot    Object | null                  GET /api/items/{id} 的结果
  loadErr     String                         快照加载失败(如 404)
  qty         Number                         步进器数值
  targetLocId Number | null                  换位置下拉选中值
  busy        Boolean                        请求进行中, 锁住全部按钮
  err         String                         操作失败信息
```

### 为什么展开时拉单条快照,而不是从 `sceneItems` 查表

VoicePanel 已有 `sceneItems`(`listItems({limit:1000})`),看似可以本地查表,但有三个硬伤:

1. `listItems` 默认排除 `quantity=0` 的物品([items.py:257](../../../backend/app/routers/items.py#L257)),
   归零物品查不到,「换位置」拿不到当前 `location_id`。
2. 候选物品 `c` 只有 `item_id / item_name / location_path`,没有 `location_id`,
   换位置无法回填默认选中项。
3. 取出步进器的上限、以及「取出」按钮在 `quantity=0` 时的禁用判断,都需要当前数量。
   从可能过期的 `sceneItems` 取会让上限失真。

`GET /api/items/{id}` 已存在([items.py:316](../../../backend/app/routers/items.py#L316)),
只需在 [api.js](../../../frontend/src/api.js) 补一行封装。请求只在点开面板时发生,
局域网 NAS 单条查询成本可忽略,不影响默认渲染。

`api.js` 新增:

```js
getItem: (id) => request(`/api/items/${id}`),
```

## 操作语义

| 按钮 | 展开 | 调用 | 语义 |
|---|---|---|---|
| **− 取出** | 数量步进器,默认 1,上限 = 快照 quantity | `recordTx(id, {item_id, action:'take_out', quantity, location_id: 快照.location_id})` | 数量减;会进入「待归位」提醒列表 |
| **⇪ 补库存** | 数量步进器,默认 1,无上限 | `recordTx(id, {item_id, action:'put_in', quantity, location_id: 快照.location_id})` | 数量加 |
| **⇄ 换位置** | 扁平下拉 `full_path`,默认选中快照 `location_id` | `updateItem(id, {location_id})` | 走审计日志,**不记流水** |
| **⊗ 用完了** | 无,`confirm()` 二次确认 | `recordTx(id, {item_id, action:'adjust', quantity: 0, note:'用完清零'})` | 清零 → 进「待补充」,可恢复 |
| **🗑 删除** | 无,`confirm()` 二次确认 | `deleteItem(id)` | 永久删除,审计日志保留 |

补充说明:

- **「用完了」为什么用 `adjust` 而不是 `consume`**:后端
  [items.py:369-374](../../../backend/app/routers/items.py#L369-L374) 的动作分支只有
  `take_out / put_in / adjust`,**没有 `consume` 分支** —— consume 只写一条流水,
  `item.quantity` 原封不动。它的用途是销掉「待归位」欠账(取出时数量已经扣过了,
  见 [VoicePanel.vue:190-192](../../../frontend/src/components/VoicePanel.vue#L190-L192) 注释)。
  唯一能直接设值的动作是 `adjust`(`item.quantity = qty`),因此清零走 `adjust` + `quantity:0`。
  流水标签会显示为「盘点」,靠 `note:'用完清零'` 区分于普通盘点。
  额外好处:`adjust` 到 0 是幂等的,不受快照与执行之间数量变化的影响。
- **换位置为什么不记流水**:后端 transaction 的 action 只有
  `take_out|put_in|adjust|consume`([schemas.py:86](../../../backend/app/schemas.py#L86)),
  没有 move。用 `put_in` 携带新 `location_id` 虽然能改位置
  ([items.py:372-373](../../../backend/app/routers/items.py#L372-L373)),但会**同时增加数量**,
  语义错误。因此走 `PATCH /api/items/{id}`,由 update_item 的审计逻辑记录字段变更。
- **取出/补库存为什么传当前 `location_id`**:与
  [ItemList.vue:90](../../../frontend/src/components/ItemList.vue#L90) 的 `quickTx` 保持一致,
  流水上带位置便于「待归位」计算归位目标。传当前值等于位置不变。
- **「用完了」与「删除」的区别**:前者记 consume 流水把数量清零,物品进入「待补充」列表
  (搜索/语音/3D 都隐藏,但可补货复活);后者从数据库永久抹除。两者都保留。

## 三处接入点

全部在 [VoicePanel.vue](../../../frontend/src/components/VoicePanel.vue):

| 接入点 | 位置 | compact | 注意 |
|---|---|---|---|
| 候选物品 `li` | 816-828 行 | `true` | 保留「选这个」按钮不动 |
| 推荐用品 `tr` | 798-814 行 | `true` | 新增一列;按钮必须 `@click.stop` |
| 近期取放记录 `li` | 895-905 行 | `false` | 按 `t.item_id` 挂载 |

**推荐用品的 `@click.stop` 是必须的**:`tr` 上有
`@click="pickCandidate(...)"`([VoicePanel.vue:807](../../../frontend/src/components/VoicePanel.vue#L807)),
不阻止冒泡的话点「取出」会同时触发一轮 LLM 意图重放。

`locations` 从 VoicePanel 已有的 `sceneLocations` 传入(`loadScene()` 已经在拉),不新增请求。

## 布局

结果卡片是 `lg:col-span-1`(约 1/3 宽,iPad 上约 270px 可用宽度)。5 个按钮加上名称与位置
挤不进一行,因此 `compact=true` 时条目排成两行:

```
┌─────────────────────────────────┐
│ 充电宝      卧室/床头柜/上层    │   ← 名称 + 位置(原有)
│ [−][⇪][⇄][⊗][🗑]      [选这个] │   ← 按钮组 + 原有按钮
└─────────────────────────────────┘
   展开后 ↓
│ 数量 [−] 2 [+]    [取消] [确认] │
```

按钮用图标 + `title` 属性,不带文字标签,保证 5 个能排下。近期取放记录卡片是全宽,
`compact=false` 时按钮与内容同行右对齐。

样式复用现有 `btn btn-secondary text-xs` / `btn btn-danger text-xs` 类,不引入新样式。

## 刷新联动

组件 `emit('done')` 后,VoicePanel 用一个统一 handler 处理:

```js
function onQuickActionDone() {
  loadRecent(); loadScene(); loadPending(); loadDepleted()
  emit('changed')
}
```

`emit('changed')` 向上传给 App.vue,让其他标签页的数据也失效(与现有
`restockDepleted` / `markReturned` 的做法一致)。

## 边界情况

| 情况 | 行为 |
|---|---|
| 物品已删除但流水还在 → `getItem` 404 | 面板显示「该物品已删除」,全部按钮禁用 |
| 快照 `quantity === 0` | 「取出」「用完了」禁用;「补库存」「换位置」「删除」可用 |
| 快照 `location_id` 为 null | 换位置下拉默认选「—」 |
| 请求进行中 | `busy=true`,全部按钮禁用,防重复提交 |
| 请求失败 | 面板内红字显示错误,**不吞异常**,面板保持展开让用户重试 |
| 取出数量超过库存 | 步进器上限锁死;后端 `max(0,…)` 兜底 |
| 快照与执行之间数量被改(如飞书机器人并发操作) | 取出/补库存是增量,天然安全;「用完了」用 `adjust:0` 幂等,同样安全。不做乐观锁 |
| 同时展开多个条目的面板 | 允许,互不干扰(各自独立组件实例) |
| 语音状态机 `phase !== 'idle'` | **不禁用**。快捷操作不依赖 LLM,与「选这个」不同 |

## 验证

项目当前**没有任何自动化测试基础设施**(无 pytest、无 vitest,
[package.json](../../../frontend/package.json) 只有 dev/build/preview)。本次不引入测试框架
——那属于独立决策,不该夹带在一个 UI 功能里。验证方式:

1. `cd frontend && npm run build` 通过
2. 手工清单(iPad Safari + 桌面浏览器各一遍):
   - 语音查找一件物品 → 候选条目上五个按钮都出现且不遮挡「选这个」
   - 取出 2 件 → 物品页数量减 2、「待归位」出现该条目、近期流水出现 take_out
   - 补库存 3 件 → 数量加 3
   - 换位置到另一个抽屉 → 物品页位置更新、3D 高亮位置改变、审计页出现字段变更记录
   - 用完了 → 物品从搜索消失、出现在「待补充」、补货能复活;
     近期流水出现一条「盘点」且备注为「用完清零」
   - 删除 → 物品消失、审计页保留记录
   - 点推荐用品行内的「取出」→ **不触发** LLM 重放(确认「本次会话」没有多出一条)
   - 断网状态下点任一按钮 → 面板内显示错误而非静默失败

## 改动文件

| 文件 | 改动 |
|---|---|
| `frontend/src/components/ItemQuickActions.vue` | 新增 |
| `frontend/src/components/VoicePanel.vue` | 3 处接入 + 1 个 handler |
| `frontend/src/api.js` | 新增 `getItem` |
