# 性能 / 功耗 + 死代码清理 (子项目 A)

日期: 2026-09-03
状态: 已确认, 待实施

这是 repo_git 持续优化的第一个子项目。后续: D 文档/CI 瘦身, B AI 准确性, C UX 收敛 + 功能增补。

## 背景

调研发现的功耗与性能底噪 (file:line 见各节):

- 两个 Three.js 场景 (首页预览 + 3D 页 keep-alive) 的 RAF 永远在跑, 无按需渲染、无可见性暂停
- 8 个面板 `v-show` 开机全 mount; `LogsPanel` 3s / `VoicePanel` 30s 轮询与当前 tab 无关
- 启动时 `listItems(limit=1000)` 拉 3 遍、`listLocations` 拉 4 遍
- nginx 无 gzip、无静态资源缓存头, Scene3D chunk 580KB 裸传
- 后端每条 op 各查一次全表 (`_resolve_location` / `_find_exact_item` / `_same_name_items`); `pending-returns` N+1 且装全表 transactions; `transactions.item_id/location_id` 无索引
- Telegram 10s / 飞书 15s / 备份 60s 三个后台循环在功能关闭时仍周期唤醒
- 一批零引用导出、未用 import、重复 helper

## 已确认的取舍

1. 首页 3D 预览**保留**, 改按需渲染 (不改成点击加载, 不移除)。
2. 每个子项目完成后**重建并重启本机 `storage-app`** (`./start.sh --whisper`), 按三项检查验证 (CA 指纹、数据条数、产物含新串)。

## 设计

### A1. Scene3D 按需渲染 (`frontend/src/components/Scene3D.vue`)

`loop()` 改为"活跃窗口"模型。一个 `requestRender(reason)` 入口把场景置为活跃; 活跃期开 RAF, 最后一次活跃事件 1.5s 后停帧。触发活跃的来源:

- OrbitControls `start` / `change` / `end` 事件
- 相机 tween (`tweenCamera`) 与高亮脉冲 (`pulseTween`) 进行中 —— 每帧自行续期
- `rebuild()` / `updateItemVisibility()` 完成后一帧
- 容器尺寸变化 (resize)

尘粒 (motes) 只在活跃期更新, 空闲静止。

两层额外保护, 任一命中即停帧且不接受 `requestRender`:

- `document.visibilitychange` → hidden
- `IntersectionObserver` 判定容器不在视口 (覆盖 `v-show` 隐藏、滚出屏幕)

回到可见时补渲染一帧。

`lowQuality` 切换不再 dispose/重建 renderer, 改为原地调: `shadowMap.enabled`、`setPixelRatio`、灯光强度、吸顶灯可见性、尘粒数量 (重建 motes 即可)。`antialias` 是构造期参数, 改不了 —— 接受这一点 (省电模式的 antialias 与初始化时一致)。

三个 `deep: true` watcher 改为指纹字符串: `items` 用 `id:quantity:location_id:updated_at` 拼接, `locations` 用 `id:parent_id:kind:layout 关键字段` 拼接, `showItemsInRoomIds` 用 `join(',')`。指纹变了才重建。

`onBeforeUnmount` 与旧的 lowQuality watcher 中重复的 dispose 序列抽成 `disposeAll()`。

### A2. 面板挂载与轮询 (`frontend/src/App.vue` + 各面板)

- 8 个 `v-show` 面板改为 `v-if` + `<keep-alive>`, 与 BuildingPanel 一致。首次进入才 mount, 切走保留状态。
- `VALID_TABS` 补 `'backup'`。
- `LogsPanel` / `VoicePanel` 的轮询: `onDeactivated` 与 `visibilitychange→hidden` 时 `clearInterval`; `onActivated` / 回到可见时先立即刷新一次再重建 interval。VoicePanel 是首页, 唤醒词监听等语音逻辑**不受影响**, 只管两个 `load*` 轮询。
- 新增 `frontend/src/composables/useInventoryStore.js`: 模块级 `items` / `locations` ref + `loadItems()` / `loadLocations()` 带 in-flight 去重 (同一时刻多个调用共享一个 Promise) + `invalidate()`。`ItemList` / `LocationManager` / `VoicePanel` / `BuildingPanel` 改为从 store 取数; `refreshKey` 变化由 App 层调一次 `invalidate()+reload`, 面板不再各自 refetch。面板内的筛选/分页状态保持本地不变。

### A3. nginx 传输 (`deploy/app-routes.conf`)

```
gzip on; gzip_vary on; gzip_min_length 1024;
gzip_types application/javascript text/css application/json image/svg+xml application/manifest+json;
location /assets/ { expires 1y; add_header Cache-Control "public, immutable"; }
location = /index.html { add_header Cache-Control "no-cache"; }
```

Vite 产物文件名带 hash, `immutable` 安全。`/api/` 不压 (响应小, 省 CPU)。

### A4. 后端查询与索引

- `backend/app/migrations.py` 新增 `ensure_indexes(engine)`: `CREATE INDEX IF NOT EXISTS ix_transactions_item_id / ix_transactions_location_id`; `run_all` 调用它。`models.py` 对应字段加 `index=True` 让新库直接带索引。
- `routers/items.py` `pending-returns`: **保留运行余额算法** (按时间顺序累加、`max(0, …)` 钳位 —— 补货式 put_in 不算归还, 这是语义的一部分, 不能换成 SUM 聚合), 只改数据获取: 先查有过 `take_out` 的 item_id 集合, 只装载这些物品的 take_out/put_in/consume 列 (不装 ORM 对象), 再用两条 `IN` 查询预取 Item 与 Location; 不再逐个 `.get()`。
- `llm/intent.py`: `plan_operations` / `apply_operations` / `_execute_batch` 入口一次性构建 `ctx = {locations: [...], items_by_name: {...}, items_by_alias: {...}}`, `_resolve_location` / `_find_exact_item` / `_same_name_items` 接收 ctx 而不各自 `.all()`。**匹配语义不变** (子串双向、先命中先赢等问题留给 B)。
- 全部 `db.query(M).get(id)` → `db.get(M, id)`。

### A5. 后台循环按需唤醒

- `services/telegram.py`: 关闭态 `await _reload_event.wait()` (无 timeout); 开启态维持 25s 长轮询节奏。
- `services/backup.py`: 关闭态或 `schedule == manual` 时 `await _wake.wait()` 无 timeout; 开启态维持 60s。
- `services/feishu.py`: supervisor 的 `asyncio.sleep(POLL_INTERVAL_S)` → `wait_for(_reload_event.wait(), POLL_INTERVAL_S)`; 关闭态无 timeout。`reload()` 置事件。现有 680 行 supervisor 测试必须全绿; 若测试依赖 `sleep` 被 patch, 同步调整 patch 目标。
- 三处 `reload()` 已由 `routers/settings.py` / `routers/backup.py` 调用, 无需改路由。

### A6. 死代码与重复

删除:
- `frontend/src/api.js` `itemTransactions`
- `frontend/src/composables/sceneLayout.js` 的 `KIND_DEFAULTS` / `defaultsFor` / `snapAngleDeg` / `polygonSignedArea` (确认零引用后)
- `frontend/src/composables/useVoice.js` 的 `classifyYesNo` / `classifyAnswer` 导出 (若内部仍用则只去掉 export)
- 后端未用 import: `intent.py` `serialize_transaction`, `summary.py` `Iterable`, `routers/voice.py` `datetime/Any/models/location_path`, `routers/dingtalk.py` `location_path`
- `_execute_batch(parsed=...)` 无用形参; `eval/run_eval._match(items_by_id)` 无用形参
- `schemas.py` `ConfigPatch.webdav` 死字段

合并:
- `UNDOABLE_ACTIONS` 只留 `services/inventory.py` 一份, `routers/revise.py` 引用它
- `api.js` 三段 `URLSearchParams` 过滤逻辑抽 `qs(params)`
- `frontend/package.json` / `package-lock.json` name `storage-frontend` → `storage-app`

不碰: `_apply_stock_op` 三处手抄 (B 统一两条执行路径时处理); `migrate_to_home` 幂等空转; `websockets==16.0`。

## 测试

后端 (unittest, 在 `backend/tests` 下运行, 或 docker bind mount):
- 现有 69 个测试 + `eval.run_eval --replay` 全绿
- 新增 `test_migrations.py::test_ensure_indexes_idempotent` —— 跑两遍 `run_all`, `PRAGMA index_list('transactions')` 含两个新索引
- 新增 `test_api_plan_apply.py::test_pending_returns_query_count` —— 用 SQLAlchemy `before_cursor_execute` 事件计数, 造 5 个 pending item 时查询次数 ≤ 2
- 新增 `test_background_idle.py` —— telegram / backup 关闭态下 `reload()` 前不唤醒 (用 `asyncio.wait_for` 短超时断言任务仍挂起), `reload()` 后一个循环内醒来

前端 (无测试基建, 手工验证):
- `npm run build` 通过
- 重建镜像后浏览器 Performance: 3D 页无交互 3s 后 CPU 帧活动归零; 切到其他 tab 后 Network 无 `/api/logs` 请求
- `curl -sI --compressed .../assets/Scene3D-*.js` 含 `Content-Encoding: gzip` 与 `Cache-Control: public, immutable`

部署验证 (记忆 `repo-git-docker-rebuild`):
- CA 指纹不变、items/locations/transactions/audit_log 条数不变、线上 JS 含新加字符串

## 明确不做

- 任何按钮 / 布局 / 文案改动 (C)
- prompt / 匹配逻辑 / 两条执行路径统一 (B)
- 文档与 CI (D), 除本 spec 外不新增文档
