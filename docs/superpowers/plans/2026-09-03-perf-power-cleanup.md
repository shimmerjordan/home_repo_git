# 性能 / 功耗 + 死代码清理 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 repo_git 的空闲功耗底噪降到接近零 (3D 按需渲染、轮询绑可见性、后台循环按需唤醒), 消掉几处 O(N) 全表查询与 N+1, 开 gzip/缓存, 删掉零引用代码 —— 全程不改任何用户可见行为。

**Architecture:** 前端: Scene3D 从"永远 RAF"改为"活跃窗口 + 可见性双保护"; 面板 `v-if + keep-alive` 懒挂载; 新增 `useInventoryStore` 共享物品/位置数据 (in-flight 去重), `usePausablePoll` 统一轮询暂停。后端: 迁移加索引; `pending-returns` 预取消 N+1 (保留运行余额语义); `intent.py` 三个全表查询走 per-session 缓存 (`db.info`, flush 即失效, 语义等价); 三个后台循环关闭态无 timeout 挂起。nginx 加 gzip + 静态缓存头。

**Tech Stack:** Vue 3.5 / Three.js 0.170 / Vite 6 / Tailwind 3; FastAPI 0.115 / SQLAlchemy 2.0 / SQLite; unittest (非 pytest); nginx + supervisord 单容器。

**Spec:** `docs/superpowers/specs/2026-09-03-perf-power-cleanup-design.md`

## Global Constraints

- **不 commit、不 push** —— 用户规则。每个任务末尾只做 `git status --short` / `git diff --stat` 核对改动范围; commit 由用户决定。
- **不改用户可见行为**: 匹配语义、`pending-returns` 结果、按钮/文案/布局全部不动。
- `backend/requirements.txt` 的 `websockets==16.0` 不动。
- 后端测试在 `backend/tests` 目录下用 **unittest** 跑, 宿主机无 fastapi, 用 docker bind mount:
  ```bash
  cd /home/xyz/Projects/priv/repo_git
  docker run --rm -v "$PWD/backend:/src" -w /src/tests \
    -e DATABASE_URL=sqlite:// -e CONFIG_PATH=/tmp/ci-config.json -e LOG_DIR=/tmp/ci-logs \
    repo_git-app python -m unittest discover -s . -v
  ```
  单个文件: 把 `discover -s . -v` 换成 `test_migrations -v`。
  eval 回放: `docker run --rm -v "$PWD/backend:/src" -w /src -e CONFIG_PATH=/tmp/eval-config.json --network none repo_git-app python -m eval.run_eval --replay`
- 前端无测试基建, 每个前端任务以 `cd frontend && npm run build` 通过为门槛 (本机 node v24 可用)。
- 现有 69 个后端测试 + eval 回放必须始终全绿。
- 文件路径均相对仓库根 `/home/xyz/Projects/priv/repo_git`。

---

## 文件结构

**新建**
- `backend/tests/test_migrations.py` — 索引迁移幂等测试
- `backend/tests/test_background_idle.py` — 三个后台循环关闭态零唤醒测试
- `frontend/src/composables/useInventoryStore.js` — 物品/位置共享数据 + in-flight 去重
- `frontend/src/composables/usePausablePoll.js` — 可见性感知的 setInterval

**修改 (后端)**
- `backend/app/models.py` — Transaction 两列 `index=True`
- `backend/app/migrations.py` — `ensure_indexes()`
- `backend/app/routers/items.py` — `pending_returns` 预取
- `backend/app/llm/intent.py` — 查询缓存、`db.get`、删未用 import/形参
- `backend/app/services/telegram.py` / `backup.py` / `feishu.py` — 关闭态挂起
- `backend/app/routers/voice.py` / `dingtalk.py` / `revise.py` / `services/summary.py` / `schemas.py` / `eval/run_eval.py` — 死代码
- `backend/tests/test_api_plan_apply.py` — 新增查询计数测试

**修改 (前端)**
- `frontend/src/App.vue` — `VALID_TABS`、`v-if + keep-alive`
- `frontend/src/components/Scene3D.vue` — 按需渲染、in-place lowQuality、指纹 watcher、`disposeAll`
- `frontend/src/components/LogsPanel.vue` / `VoicePanel.vue` — 用 `usePausablePoll`
- `frontend/src/components/ItemList.vue` / `LocationManager.vue` / `VoicePanel.vue` / `BuildingPanel.vue` — 用 `useInventoryStore`
- `frontend/src/api.js` — `qs()`、删 `itemTransactions`
- `frontend/src/composables/sceneLayout.js` / `useVoice.js` — 去掉零引用 export
- `frontend/package.json` / `package-lock.json` — name

**修改 (部署)**
- `deploy/app-routes.conf` — gzip + 缓存头

---

### Task 1: Transaction 索引迁移

**Files:**
- Modify: `backend/app/models.py:73,76`
- Modify: `backend/app/migrations.py:91-94`
- Create: `backend/tests/test_migrations.py`

**Interfaces:**
- Produces: `migrations.ensure_indexes(engine: Engine) -> None`, 被 `run_all` 调用。索引名 `ix_transactions_item_id`、`ix_transactions_location_id`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_migrations.py
"""migrations.run_all 必须幂等: 启动与备份恢复都会调它, 跑两遍不能报错、索引不能重复。"""
from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text

from _fixtures import make_session  # noqa: F401  (sys.path 注入)
from app import migrations
from app.database import Base


def _index_names(engine, table):
    with engine.connect() as conn:
        return {r[1] for r in conn.execute(text(f"PRAGMA index_list({table})"))}


class EnsureIndexesTest(unittest.TestCase):
    def test_indexes_created_and_idempotent(self):
        engine = create_engine("sqlite://", future=True)
        Base.metadata.create_all(bind=engine)
        # 模拟老库: 建表时没有这两个索引
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS ix_transactions_item_id"))
            conn.execute(text("DROP INDEX IF EXISTS ix_transactions_location_id"))
        self.assertNotIn("ix_transactions_item_id", _index_names(engine, "transactions"))

        migrations.run_all(engine)
        migrations.run_all(engine)   # 第二遍不能抛

        names = _index_names(engine, "transactions")
        self.assertIn("ix_transactions_item_id", names)
        self.assertIn("ix_transactions_location_id", names)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: docker 命令, 末尾 `test_migrations -v`
Expected: FAIL — `AssertionError: 'ix_transactions_item_id' not found in {...}`

- [ ] **Step 3: models.py 加 index=True**

`backend/app/models.py` 里 `Transaction`:
```python
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False, index=True)
    ...
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=True, index=True)
```
(SQLAlchemy 默认索引名就是 `ix_transactions_item_id` / `ix_transactions_location_id`, 与迁移一致。)

- [ ] **Step 4: migrations.py 加 ensure_indexes**

在 `run_all` 之前加:
```python
def ensure_indexes(engine: Engine) -> None:
    """老库补索引。models.py 上 index=True 只对新建表生效, 已有的 storage.db 得靠这里。
    transactions 按 item_id / location_id 过滤的接口 (流水筛选、pending-returns) 之前走全表。"""
    stmts = [
        "CREATE INDEX IF NOT EXISTS ix_transactions_item_id ON transactions (item_id)",
        "CREATE INDEX IF NOT EXISTS ix_transactions_location_id ON transactions (location_id)",
    ]
    with engine.begin() as conn:
        for s in stmts:
            conn.execute(_sql_text(s))
```
`run_all` 改为:
```python
def run_all(engine: Engine) -> None:
    """补齐表结构并执行所有向后兼容迁移。启动与恢复共用。"""
    ensure_columns(engine)
    ensure_indexes(engine)
    migrate_to_home(engine)
```

- [ ] **Step 5: 跑测试确认通过, 再跑全量**

Run: `test_migrations -v` → PASS; 再 `discover -s . -v` → 70 passed。

- [ ] **Step 6: 核对**

`git status --short` 应只有 `models.py`、`migrations.py`、`tests/test_migrations.py`。

---

### Task 2: pending-returns 去 N+1

**Files:**
- Modify: `backend/app/routers/items.py:451-499`
- Test: `backend/tests/test_api_plan_apply.py`

**Interfaces:**
- 响应 JSON 结构、排序、数值**完全不变**。只改查询次数。

- [ ] **Step 1: 写失败测试 (查询计数)**

在 `backend/tests/test_api_plan_apply.py` 的 `ApiPlanApplyTest` 类里追加 (文件已有 `cls.app`、`SessionLocal`、fixture 物品):

```python
    def test_pending_returns_constant_queries(self):
        """5 个物品各借出一次, 接口不能对每个物品再各查一次 Item/Location (N+1)。"""
        from sqlalchemy import event
        from app import models
        from app.database import SessionLocal, engine

        db = SessionLocal()
        items = db.query(models.Item).filter(models.Item.quantity > 0).limit(5).all()
        self.assertGreaterEqual(len(items), 5)
        for it in items:
            db.add(models.Transaction(item_id=it.id, action="take_out", quantity=1,
                                      location_id=it.location_id))
        db.commit()
        db.close()

        count = {"n": 0}
        def _count(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                count["n"] += 1
        event.listen(engine, "before_cursor_execute", _count)
        try:
            with httpx.Client(transport=httpx.ASGITransport(app=self.app), base_url="http://t") as c:
                r = c.get("/api/transactions/pending-returns")
        finally:
            event.remove(engine, "before_cursor_execute", _count)

        self.assertEqual(r.status_code, 200)
        names = {row["item_name"] for row in r.json()}
        for it in items:
            self.assertIn(it.name, names)
        # 1 次 transactions + 1 次 items + 1 次 locations, 留一点余量
        self.assertLessEqual(count["n"], 4, f"pending-returns 用了 {count['n']} 次 SELECT")
```

注意: 该测试类里其他 test 可能也造了流水, 断言用 `assertIn` 而不是 `assertEqual(len)`。若文件里 httpx 客户端的构造写法不同 (查看已有 test 怎么建 client), 照抄已有写法。

- [ ] **Step 2: 跑测试确认失败**

Run: `test_api_plan_apply -v`
Expected: FAIL — `pending-returns 用了 11 次 SELECT` (1 + 5 item + 5 location)。

- [ ] **Step 3: 重写 pending_returns 的查询部分**

保留运行余额算法 (`max(0, ...)` 钳位是语义的一部分, **不能**换成 SUM 聚合), 只改数据获取:

```python
@recent_router.get("/pending-returns")
def pending_returns(db: Session = Depends(get_db)):
    """List items currently checked out and awaiting return (借出未归位).

    Definition: per-item, running balance of take_out − put_in − consume, clamped
    at 0 (put_in of fresh stock is not a "return"), walked in chronological
    order. Only items with at least one take_out can be pending, so we restrict
    the walk to those; Item/Location rows are prefetched in two IN-queries.
    """
    taken_ids = [
        r[0] for r in db.query(models.Transaction.item_id)
        .filter(models.Transaction.action == "take_out").distinct().all()
    ]
    if not taken_ids:
        return []
    rows = (
        db.query(models.Transaction.item_id, models.Transaction.action,
                 models.Transaction.quantity, models.Transaction.created_at,
                 models.Transaction.location_id)
        .filter(models.Transaction.item_id.in_(taken_ids),
                models.Transaction.action.in_(("take_out", "put_in", "consume")))
        .order_by(models.Transaction.created_at.asc())
        .all()
    )
    pending: dict[int, dict] = {}
    for item_id, action, quantity, created_at, location_id in rows:
        slot = pending.setdefault(item_id, {"qty": 0, "last_take": None, "last_take_loc": None})
        if action == "take_out":
            slot["qty"] += quantity
            slot["last_take"] = created_at
            slot["last_take_loc"] = location_id
        else:  # put_in / consume
            slot["qty"] = max(0, slot["qty"] - quantity)
            if slot["qty"] == 0:
                slot["last_take"] = None
                slot["last_take_loc"] = None
        # adjust: excluded by the filter — it's a manual recount, not borrow/return

    live = {iid: s for iid, s in pending.items() if s["qty"] > 0}
    if not live:
        return []
    items_by_id = {
        it.id: it for it in db.query(models.Item).filter(models.Item.id.in_(list(live))).all()
    }
    loc_ids = {s["last_take_loc"] for s in live.values() if s["last_take_loc"]}
    locs_by_id = {
        l.id: l for l in db.query(models.Location).filter(models.Location.id.in_(list(loc_ids))).all()
    } if loc_ids else {}

    out = []
    for item_id, slot in live.items():
        item = items_by_id.get(item_id)
        if not item:
            continue
        ret_loc = locs_by_id.get(slot["last_take_loc"]) if slot["last_take_loc"] else None
        out.append({
            "item_id": item.id,
            "item_name": item.name,
            "pending_quantity": slot["qty"],
            "last_take_at": slot["last_take"].isoformat() if slot["last_take"] else None,
            "return_location_id": ret_loc.id if ret_loc else None,
            "return_location_path": location_path(ret_loc) if ret_loc else None,
        })
    out.sort(key=lambda r: r["last_take_at"] or "", reverse=True)
    return out
```

`location_path(ret_loc)` 会沿 `parent` 关系向上走, 每层一次 lazy load —— 这是原本就有的开销, 数量 = 位置深度 (3~4), 与物品数无关, 可接受。若测试里 count 超过 4 是因为这个, 把阈值放到 `4 + 深度`, 并在断言注释里说明。

- [ ] **Step 4: 跑测试确认通过, 再跑全量**

Run: `test_api_plan_apply -v` → PASS; `discover -s . -v` → 全绿。

- [ ] **Step 5: 核对**

`git diff --stat` 只有 `routers/items.py` 与 `tests/test_api_plan_apply.py`。

---

### Task 3: intent.py 查询缓存 + `db.get`

**Files:**
- Modify: `backend/app/llm/intent.py:310-331` (`_resolve_location`), `:435-443` (`_find_exact_item`), `:581-590` (`_same_name_items`), 所有 `db.query(models.X).get(id)`
- Test: 现有 `test_intent_plan.py` / `test_api_plan_apply.py` + 新增一个缓存失效测试

**Interfaces:**
- Produces: 模块内私有 `_all_locations(db) -> list[Location]`、`_all_items(db) -> list[Item]`, 结果缓存在 `db.info["_intent_cache"]`, 在 session `after_flush` / `after_rollback` 时清空。
- 匹配语义 (子串双向、先命中先赢、别名集合) **逐字保留**。

- [ ] **Step 1: 写失败测试 (缓存必须在 flush 后失效)**

在 `backend/tests/test_intent_plan.py` 末尾 (类外或新类) 加:

```python
class LookupCacheTest(unittest.TestCase):
    """_find_exact_item 走 per-session 缓存, 但新建物品 flush 之后必须立刻能被找到 ——
    apply 一批里第二条 "新建 X" 得合并到第一条刚建的 X 上, 这是现有行为。"""

    def test_cache_invalidated_after_flush(self):
        from app.llm import intent as I
        db = make_session()
        seed(db)
        self.assertIsNone(I._find_exact_item(db, "新物品Z"))
        db.add(models.Item(name="新物品Z", quantity=1))
        db.flush()
        found = I._find_exact_item(db, "新物品Z")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "新物品Z")

    def test_cache_hit_avoids_requery(self):
        from sqlalchemy import event
        from app.llm import intent as I
        db = make_session()
        seed(db)
        I._find_exact_item(db, "充电宝")          # 预热
        n = {"c": 0}
        def _count(conn, cursor, statement, parameters, context, executemany):
            if "FROM items" in statement:
                n["c"] += 1
        event.listen(db.get_bind(), "before_cursor_execute", _count)
        try:
            I._find_exact_item(db, "充电器")
            I._same_name_items(db, "电池")
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", _count)
        self.assertEqual(n["c"], 0)
```

确认文件顶部已 `from _fixtures import make_session, seed` 与 `from app import models` (已有的 import 里找, 没有就补)。

- [ ] **Step 2: 跑测试确认失败**

Run: `test_intent_plan -v`
Expected: `test_cache_hit_avoids_requery` FAIL (`2 != 0`); `test_cache_invalidated_after_flush` PASS (现在没缓存所以自然通过 —— 它是防回归的)。

- [ ] **Step 3: 实现缓存**

在 `intent.py` 的 `_resolve_location` 上方加:

```python
# ---- per-session lookup cache -------------------------------------------------
# plan / apply 一批 5 个物品会把 Location.all() / Item.all() 各查 5~10 遍。这里按 session
# 缓存整表, 任何 flush / rollback 立刻清空 —— 语义与"每次现查"完全一致 (session 是
# autoflush=False, 查询本来就只看得到已 flush 的数据)。
from sqlalchemy import event as _sa_event
from sqlalchemy.orm import Session as _SaSession

_CACHE_KEY = "_intent_cache"


def _cache(db: Session) -> dict:
    return db.info.setdefault(_CACHE_KEY, {})


def _clear_cache(session, *_args) -> None:
    session.info.pop(_CACHE_KEY, None)


_sa_event.listen(_SaSession, "after_flush", _clear_cache)
_sa_event.listen(_SaSession, "after_rollback", _clear_cache)
_sa_event.listen(_SaSession, "after_commit", _clear_cache)


def _all_locations(db: Session) -> list[models.Location]:
    c = _cache(db)
    if "locations" not in c:
        c["locations"] = db.query(models.Location).all()
    return c["locations"]


def _all_items(db: Session) -> list[models.Item]:
    c = _cache(db)
    if "items" not in c:
        c["items"] = db.query(models.Item).all()
    return c["items"]
```

然后三处替换:
- `_resolve_location`: `for loc in db.query(models.Location).all():` → `for loc in _all_locations(db):`
- `_find_exact_item`: `for candidate in db.query(models.Item).all():` → `for candidate in _all_items(db):`
- `_same_name_items`: `for it in db.query(models.Item).all():` → `for it in _all_items(db):`

- [ ] **Step 4: `db.query(M).get(id)` → `db.get(M, id)`**

`grep -n "\.query(models\.[A-Za-z]*)\.get(" backend/app/llm/intent.py backend/app/routers/items.py` 列出全部 (intent.py 约 8 处: 314/469/633/698/828/897/927/1273/1295; items.py 483/489 在 Task 2 已删)。逐个改成 `db.get(models.Location, ref["location_id"])` 这种形式。语义等价 (identity map 优先, 否则查库)。

- [ ] **Step 5: 跑测试**

Run: `test_intent_plan -v` → 两个新测试 PASS; `discover -s . -v` 全绿; eval `--replay` 准确率与改前一致 (先在改动前跑一次记下数字)。

- [ ] **Step 6: 核对**

`git diff --stat` 只有 `llm/intent.py`、`tests/test_intent_plan.py`。

---

### Task 4: 后台循环关闭态零唤醒

**Files:**
- Modify: `backend/app/services/telegram.py:139-153`
- Modify: `backend/app/services/backup.py:536-553`
- Modify: `backend/app/services/feishu.py:708-715, 758-763`
- Create: `backend/tests/test_background_idle.py`

**Interfaces:**
- 每个模块新增 `_loop_iterations: int` 计数器 (仅供测试观察, 每进一轮 +1)。
- `feishu` 新增模块级 `_reload_event: asyncio.Event | None`, `reload()` 在原有逻辑后 `set()`。
- `start()` / `reload()` / `stop()` 签名不变。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_background_idle.py
"""三个后台循环在功能关闭时必须挂起在 Event 上, 不能靠 timeout 周期空醒 ——
NAS 上即使什么都没开, 进程也不该每 10/15/60 秒醒一次。
默认配置 (临时 CONFIG_PATH) 里 telegram / feishu / webdav 全是关闭的。"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

os.environ.setdefault("CONFIG_PATH", os.path.join(tempfile.mkdtemp(), "config.json"))
os.environ.setdefault("DATABASE_URL", "sqlite://")

import sys                                                        # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _settle():
    """让被测任务跑到 await 处。"""
    for _ in range(5):
        await asyncio.sleep(0.01)


class _IdleLoopMixin:
    mod = None   # 子类填

    def test_disabled_loop_blocks_until_reload(self):
        async def scenario():
            m = self.mod
            m._loop_iterations = 0
            m.start()
            await _settle()
            first = m._loop_iterations
            self.assertEqual(first, 1, "启动后应进入第一轮并挂起")
            # 关闭态下等 0.3s, 迭代数不能涨 (若还有 timeout 空醒, 这里会被 flaky 地抓到;
            # 真正的保证是代码里没有 timeout 分支, 这条断言是烟雾测试)
            await asyncio.sleep(0.3)
            self.assertEqual(m._loop_iterations, first, "关闭态不应空醒")
            m.reload()
            await _settle()
            self.assertEqual(m._loop_iterations, first + 1, "reload() 必须立刻唤醒一轮")
            m.stop()
            await _settle()
        asyncio.run(scenario())


class TelegramIdleTest(_IdleLoopMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.services import telegram
        cls.mod = telegram


class BackupIdleTest(_IdleLoopMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.services import backup
        cls.mod = backup


class FeishuIdleTest(_IdleLoopMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.services import feishu
        cls.mod = feishu


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `test_background_idle -v`
Expected: 三个都 FAIL — `AttributeError: module has no attribute '_loop_iterations'`。

- [ ] **Step 3: telegram.py**

模块顶部 (其他 `_task` / `_reload_event` 全局旁) 加 `_loop_iterations = 0`。`_polling_loop` 改成:

```python
async def _polling_loop() -> None:
    """Main worker. Restartable via `reload()`."""
    global _last_offset, _loop_iterations
    while True:
        _loop_iterations += 1
        cfg = store.get()
        tg_cfg = cfg.telegram
        if not tg_cfg.enabled or not tg_cfg.bot_token:
            # 关闭态: 无 timeout 挂起, 只有 settings PATCH → reload() 才唤醒。
            # 以前是 wait_for(..., 10) 每 10s 空醒一次, 在 NAS 上是纯功耗底噪。
            await _reload_event.wait()
            _reload_event.clear()
            continue
        try:
            ... (原有 try 块不动)
```

- [ ] **Step 4: backup.py**

模块顶部加 `_loop_iterations = 0`。`_scheduler_loop`:

```python
async def _scheduler_loop() -> None:
    global _loop_iterations
    app_log.info("backup scheduler started")
    while True:
        _loop_iterations += 1
        try:
            cfg = store.get().webdav
            active = bool(cfg.enabled and cfg.schedule != "manual")
            if active and _due(cfg, datetime.now()):
                # webdav4 是同步阻塞 IO, 丢到线程池避免卡事件循环。
                await asyncio.get_event_loop().run_in_executor(None, _safe_run)
            if active:
                try:
                    await asyncio.wait_for(_wake.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
            else:
                # 关闭 / 手动模式: 无 timeout 挂起, 由 reload() 唤醒
                await _wake.wait()
            _wake.clear()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            app_log.warning("backup scheduler error: %s", exc)
            await asyncio.sleep(60)
```

- [ ] **Step 5: feishu.py**

全局区 (`_supervisor_task` 附近) 加:
```python
_reload_event: asyncio.Event | None = None
_loop_iterations = 0
```

新增判断函数 + 改 `_supervisor`:
```python
def _is_idle() -> bool:
    """关闭且 WS 已完全停掉 —— 这时轮询没有任何事可做。"""
    try:
        fs = store.get().feishu
        want = bool(fs.enabled and fs.app_id and fs.app_secret)
    except Exception:
        want = False
    return (not want) and _ws_thread is None and _ws_client is None


async def _supervisor() -> None:
    """只剩循环 + 等待, 真正的判断都在 _supervise_once 里。
    开启态每 POLL_INTERVAL_S 巡检一次; 关闭态无 timeout 挂起, 由 reload() 唤醒。"""
    global _loop_iterations
    while True:
        _loop_iterations += 1
        try:
            await _supervise_once()
        except Exception as exc:
            log.exception("feishu supervisor: %s", exc)
        try:
            if _is_idle():
                await _reload_event.wait()
            else:
                await asyncio.wait_for(_reload_event.wait(), timeout=POLL_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
        _reload_event.clear()
```

`start()` 里在 `_main_loop = ...` 之后加:
```python
    global _reload_event
    if _reload_event is None:
        _reload_event = asyncio.Event()
```
(把 `global` 声明合并到函数已有的那行 `global _supervisor_task, _main_loop`。)

`reload()` 末尾加:
```python
    if _reload_event is not None:
        try:
            _reload_event.set()
        except RuntimeError:
            pass
```

- [ ] **Step 6: 跑测试**

Run: `test_background_idle -v` → 3 PASS; `test_feishu_supervisor -v` → 全绿 (它直接调 `_supervise_once`, 不经过 `_supervisor`); `discover -s . -v` 全绿。

若 `FeishuIdleTest` 因为 `_supervise_once` 内部 `_in_thread` 用了 executor 而挂住, 检查 `_ws_thread`/`_ws_client` 在测试进程里是否为 None (应为 None, 未启动过)。

- [ ] **Step 7: 核对**

`git diff --stat`: `telegram.py`、`backup.py`、`feishu.py`、`tests/test_background_idle.py`。

---

### Task 5: 后端死代码

**Files:**
- Modify: `backend/app/llm/intent.py:21, 880-886`
- Modify: `backend/app/services/summary.py:6`
- Modify: `backend/app/routers/voice.py:3-4, 10, 16`
- Modify: `backend/app/routers/dingtalk.py:33`
- Modify: `backend/app/routers/revise.py:26`
- Modify: `backend/app/schemas.py:281`
- Modify: `backend/eval/run_eval.py:91` 附近 `_match`

- [ ] **Step 1: 逐项删除**

1. `intent.py:21`: `from ..services.inventory import location_path, search_items, serialize_transaction` → 去掉 `serialize_transaction` (先 `grep -n serialize_transaction backend/app/llm/intent.py` 确认只有 import 这一处)。
2. `intent.py` `_execute_batch` 签名去掉 `parsed` 形参; `grep -n "_execute_batch(" backend/app/llm/intent.py` 找到调用处同步删去实参。若调用处是关键字传参 `parsed=parsed`, 删那个关键字。
3. `summary.py:6`: 删 `from typing import Iterable` (grep 确认无使用)。
4. `routers/voice.py`: 删 `from datetime import datetime`、`from typing import Any`、`from .. import models`、`from ..services.inventory import location_path` —— 每删一个先 `grep -n "datetime\.\|\bAny\b\|models\.\|location_path" backend/app/routers/voice.py` 确认零使用。
5. `routers/dingtalk.py:33`: 删 `from ..services.inventory import location_path` (文件里 `location_path` 都是 dict key 字符串, 不是函数调用)。
6. `routers/revise.py:26`: 删 `UNDOABLE_ACTIONS = {...}` 那行, 改为 `from ..services.inventory import UNDOABLE_ACTIONS` (合并进已有的 `from ..services.inventory import ...` 行)。
7. `schemas.py` `ConfigPatch`: 删 `webdav: Optional[WebDAVConfigPatch] = None` 一行。**保留** `WebDAVConfigPatch` 类 (`routers/backup.py` 用)。`grep -rn "ConfigPatch\b" backend/app` 确认没人读 `.webdav`。
8. `eval/run_eval.py` `_match`: 去掉 `items_by_id` 形参, 调用处同步。

- [ ] **Step 2: 静态检查**

```bash
docker run --rm -v "$PWD/backend:/src" -w /src repo_git-app python -m pyflakes app eval 2>/dev/null \
  || docker run --rm -v "$PWD/backend:/src" -w /src repo_git-app python -c "import compileall,sys; sys.exit(not compileall.compile_dir('app', quiet=1))"
```
pyflakes 若镜像里没有, 退回 compileall 只保证语法。

- [ ] **Step 3: 跑全量测试 + eval 回放**

`discover -s . -v` 全绿; eval `--replay` 通过。

- [ ] **Step 4: 核对**

`git diff --stat` 应是 7 个后端文件 + `run_eval.py`, 全部是删行为主。

---

### Task 6: nginx gzip + 静态缓存

**Files:**
- Modify: `deploy/app-routes.conf`

- [ ] **Step 1: 改配置**

在 `client_max_body_size 25M;` 之后加:

```nginx
# 前端产物压缩: Scene3D chunk ~580KB / index ~230KB 以前裸传。Debian nginx 默认只压 text/html。
gzip on;
gzip_vary on;
gzip_min_length 1024;
gzip_comp_level 5;
gzip_types application/javascript text/css application/json image/svg+xml application/manifest+json;

# Vite 产物文件名带内容 hash, 可以放心让浏览器永久缓存; index.html 不缓存, 每次拿最新 hash。
location /assets/ {
    expires 1y;
    add_header Cache-Control "public, immutable";
}
location = /index.html {
    add_header Cache-Control "no-cache";
}
```

`/api/` 不加 gzip 例外 —— `gzip_types` 里有 `application/json`, 但 API 响应大多 <1KB 不会触发 `gzip_min_length`; 大列表 (`/api/items?limit=1000` 几十 KB) 被压反而是好事。

- [ ] **Step 2: 语法验证 (不重建镜像)**

```bash
docker run --rm -v "$PWD/deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  -v "$PWD/deploy/app-routes.conf:/etc/nginx/app-routes.conf:ro" \
  -v "$PWD/data/certs:/app/data/certs:ro" nginx:alpine nginx -t
```
Expected: `syntax is ok` / `test is successful`。(镜像拉不动就跳过, 留到 Task 11 重建时验证。)

- [ ] **Step 3: 核对**

`git diff deploy/app-routes.conf` 只有新增行。

---

### Task 7: useInventoryStore 共享数据

**Files:**
- Create: `frontend/src/composables/useInventoryStore.js`
- Modify: `frontend/src/components/ItemList.vue:21-31`
- Modify: `frontend/src/components/LocationManager.vue:18-22`
- Modify: `frontend/src/components/VoicePanel.vue:128-137`
- Modify: `frontend/src/components/BuildingPanel.vue:139-143`
- Modify: `frontend/src/App.vue` (`bumpRefresh`)

**Interfaces:**
- Produces:
  ```js
  useInventoryStore() → {
    items,            // Ref<Array>  全部物品, 含 quantity=0
    locations,        // Ref<Array>
    activeItems,      // ComputedRef<Array>  quantity > 0 (等价于服务端默认过滤)
    loadItems(force=false) → Promise<Array>,
    loadLocations(force=false) → Promise<Array>,
    loadAll(force=false) → Promise<[locations, items]>,
    invalidate(),     // 标脏, 下一次 load* 必重拉
  }
  ```

- [ ] **Step 1: 写 composable**

```js
// frontend/src/composables/useInventoryStore.js
// 物品 / 位置的共享数据源。以前物品页、位置页、语音页、3D 页各自 listItems(1000) +
// listLocations(), 开机四路并发拉同一份数据。这里: 模块级单例 + in-flight 去重
// (同时来的几个 load 共享一个 Promise) + 显式 invalidate() 标脏。
//
// 服务端 /api/items 默认过滤 quantity=0; 物品页要看全部所以传 include_depleted。
// 这里统一拉全量, 需要"库存>0"视图的用 activeItems, 与服务端默认过滤等价。
import { ref, computed } from 'vue'
import { api } from '../api'

const items = ref([])
const locations = ref([])
let itemsFresh = false
let locationsFresh = false
let itemsInflight = null
let locationsInflight = null

const activeItems = computed(() => items.value.filter((i) => (i.quantity || 0) > 0))

function loadItems(force = false) {
  if (itemsFresh && !force) return Promise.resolve(items.value)
  if (!itemsInflight) {
    itemsInflight = api.listItems({ limit: 1000, include_depleted: true })
      .then((rows) => { items.value = rows; itemsFresh = true; return rows })
      .finally(() => { itemsInflight = null })
  }
  return itemsInflight
}

function loadLocations(force = false) {
  if (locationsFresh && !force) return Promise.resolve(locations.value)
  if (!locationsInflight) {
    locationsInflight = api.listLocations()
      .then((rows) => { locations.value = rows; locationsFresh = true; return rows })
      .finally(() => { locationsInflight = null })
  }
  return locationsInflight
}

function loadAll(force = false) {
  return Promise.all([loadLocations(force), loadItems(force)])
}

function invalidate() {
  itemsFresh = false
  locationsFresh = false
}

export function useInventoryStore() {
  return { items, locations, activeItems, loadItems, loadLocations, loadAll, invalidate }
}
```

- [ ] **Step 2: App.vue — bumpRefresh 先标脏**

找到 `function bumpRefresh` (grep `bumpRefresh` in `App.vue`), 在函数体开头加 `store.invalidate()`; 顶部 `import { useInventoryStore } from './composables/useInventoryStore'` 与 `const store = useInventoryStore()`。这样任意面板 emit `changed` → 一次 invalidate → 各面板的 `refreshKey` watcher 调 `load*` 时共享同一个 in-flight 请求。

- [ ] **Step 3: ItemList.vue**

`load()` 改为: 有搜索词走服务端搜索 (语义是模糊搜索, 与全量列表不同, 保留); 无搜索词用 store:

```js
const store = useInventoryStore()
async function load() {
  // Items tab is the canonical management view — show depleted (quantity=0)
  // rows too so users can edit/restore them here. Search and voice paths
  // exclude depleted by default.
  if (q.value) {
    const [is_, locs] = await Promise.all([
      api.listItems({ q: q.value, limit: 1000, include_depleted: true }),
      store.loadLocations(),
    ])
    items.value = is_
    locations.value = locs
  } else {
    const [locs, all] = await store.loadAll()
    items.value = all
    locations.value = locs
  }
}
```
顶部加 `import { useInventoryStore } from '../composables/useInventoryStore'`。

- [ ] **Step 4: LocationManager.vue**

```js
const store = useInventoryStore()
async function load() {
  const [locs, all] = await store.loadAll()
  locations.value = locs
  items.value = store.activeItems.value     // 服务端默认过滤 quantity=0, 这里等价
}
```
注意 `store.activeItems.value` 要在 `loadAll` resolve 之后读 (上面顺序已保证)。

- [ ] **Step 5: VoicePanel.vue**

```js
const store = useInventoryStore()
async function loadScene() {
  // Storage events don't fire in the same tab — re-read the active home from
  // localStorage on every refresh so changes made in BuildingPanel show up here.
  activeHomeId.value = loadActiveHome()
  try {
    const [locs] = await store.loadAll()
    sceneLocations.value = locs
    sceneItems.value = store.activeItems.value
  } catch {}
}
```

- [ ] **Step 6: BuildingPanel.vue**

```js
const store = useInventoryStore()
async function load() {
  const [locs, its] = await store.loadAll()
  locations.value = locs.slice()            // 本地可编辑副本: applyEdit / history 直接改这个数组
  items.value = store.activeItems.value
}
```
`slice()` 是刻意的: BuildingPanel 的编辑历史会原地改 `locations.value`, 不能污染共享数组。

- [ ] **Step 7: 构建 + 手工验证**

`cd frontend && npm run build` 通过。`npm run dev` 起 vite (若后端不在 8000, `VITE_BACKEND=http://127.0.0.1:8080 npm run dev`; 线上容器 8080 有 `/api`), 打开 Network 面板刷新: `/api/items?limit=1000&include_depleted=true` 与 `/api/locations` 各**恰好 1 次** (Task 8 之前面板仍全 mount, 所以这是 store 去重生效的直接证据)。在物品页改一条数量 → 两个请求各再 1 次。

- [ ] **Step 8: 核对**

`git status --short`: 新文件 + 5 个修改。

---

### Task 8: 面板懒挂载 + 轮询暂停

**Files:**
- Create: `frontend/src/composables/usePausablePoll.js`
- Modify: `frontend/src/App.vue:24, 256-266`
- Modify: `frontend/src/components/LogsPanel.vue:19, 84-96`
- Modify: `frontend/src/components/VoicePanel.vue:263-269`

**Interfaces:**
- Produces: `usePausablePoll(fn, intervalMs) → { start(), stop() }`。在 `onMounted` 启动; `onDeactivated` / 页面 hidden 停; `onActivated` / 回到可见先执行一次 `fn` 再重启 (首次 activated 与 mounted 同帧时不重复执行)。

- [ ] **Step 1: 写 composable**

```js
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
```

- [ ] **Step 2: App.vue**

`VALID_TABS`:
```js
const VALID_TABS = ['voice', 'items', 'locations', 'building', 'log', 'audit', 'logs', 'backup', 'settings']
```

`<main>` 内改为 (BuildingPanel 保持原样, 其余 8 个统一进 keep-alive):
```vue
    <main class="flex-1 p-2 sm:p-4 max-w-7xl w-full mx-auto">
      <!-- v-if + keep-alive: 首次进入才挂载 (省掉开机 8 个面板并发拉数据),
           切走保留状态 (筛选条件、滚动位置、3D 相机)。 -->
      <keep-alive>
        <VoicePanel v-if="tab==='voice'" :settings="settings" :refresh-key="refreshKey" @changed="bumpRefresh" />
        <ItemList v-else-if="tab==='items'" :refresh-key="refreshKey" @changed="bumpRefresh" />
        <LocationManager v-else-if="tab==='locations'" :refresh-key="refreshKey" @changed="bumpRefresh" />
        <BuildingPanel v-else-if="tab==='building'" :refresh-key="refreshKey" @changed="bumpRefresh" />
        <TransactionFeed v-else-if="tab==='log'" :refresh-key="refreshKey" />
        <AuditPanel v-else-if="tab==='audit'" :refresh-key="refreshKey" />
        <LogsPanel v-else-if="tab==='logs'" />
        <BackupPanel v-else-if="tab==='backup'" />
        <SettingsPanel v-else-if="tab==='settings'" @saved="loadSettings" />
      </keep-alive>
    </main>
```
(单个 `<keep-alive>` 包 `v-if/v-else-if` 链是 Vue 官方推荐写法; 每个组件都有各自的缓存槽位。)

- [ ] **Step 3: LogsPanel.vue**

删 `let timer = null` (line 19) 与 `onMounted` 里的 `timer = setInterval(...)` 行、`onBeforeUnmount` 里的 `clearInterval(timer)`; 加:

```js
import { usePausablePoll } from '../composables/usePausablePoll'
// Pause auto-refresh while the user is selecting text in the log viewer —
// otherwise the 3s re-render clears the selection mid-copy.
usePausablePoll(() => { if (autoRefresh.value && !userSelecting.value) loadLogs() }, 3000)
```
`usePausablePoll(...)` 必须在 `<script setup>` 顶层同步调用 (它内部注册生命周期钩子), 放在 `onMounted(async () => {...})` 定义之前或之后都行, 但不能放进任何函数体。`onMounted` 里保留 `await loadDiag(); await loadLogs(true); document.addEventListener('selectionchange', ...)`。

- [ ] **Step 4: VoicePanel.vue**

删 `let _pollTimer = null`、`onMounted` 里的 `_pollTimer = setInterval(...)`、以及整行 `onBeforeUnmount(() => { if (_pollTimer) {...} })`; 加:

```js
import { usePausablePoll } from '../composables/usePausablePoll'
// Poll every 30 s so Feishu/bot-created transactions appear without a manual refresh.
usePausablePoll(() => { loadRecent(); loadPending() }, 30_000)
```
`onMounted(() => { loadRecent(); loadScene(); loadPending(); loadDepleted() })` 保留。

**不要动** `useVoice` / 唤醒词 / `autoYesTick` 相关的任何 timer —— 那是语音交互逻辑, 不是数据轮询。

- [ ] **Step 5: 构建 + 手工验证**

`npm run build` 通过。dev 模式: 刷新首页 → Network 只有语音页的请求, 没有 `/api/logs`、`/api/audit`、`/api/backup/*`; 切到诊断页 → 开始每 3s `/api/logs`; 切回语音页 → `/api/logs` 停; 切浏览器标签到别处 10s 再回来 → 立即一次 `/api/transactions` + `/api/transactions/pending-returns`, 然后恢复 30s。地址栏 `#tab=backup` 回车刷新 → 停在备份页。

- [ ] **Step 6: 核对**

`git status --short`: 新文件 + `App.vue`、`LogsPanel.vue`、`VoicePanel.vue`。

---

### Task 9: Scene3D 按需渲染

**Files:**
- Modify: `frontend/src/components/Scene3D.vue` — 状态区 (~64-80)、`init()` (~180-290)、`loop()` (550-558)、`tweenCamera` (562-579)、`pulseHighlight` (758-777)、`rebuild()` 末尾、`onMounted/onBeforeUnmount` (1027-1036)、`lowQuality` watcher (1039-1053)、三个 deep watcher (1055-1066)

**Interfaces:**
- 内部: `wake(ms = IDLE_AFTER_MS)`、`stopLoop()`、`applyQuality()`、`disposeAll()`。`defineExpose` 不变。

- [ ] **Step 1: 状态与 wake/loop**

把 `let raf = 0` 与 `let lastFrameT = 0` 附近改为:

```js
let raf = 0
let lastFrameT = 0
// ---- 按需渲染 ------------------------------------------------------------------
// 以前 loop() 常驻 RAF, 首页预览 + 3D 页两个场景各 60fps 空转。现在只在"活跃窗口"内
// 渲染: 交互 / 相机 tween / 高亮脉冲 / 数据重建 都会 wake(), 最后一次活跃 1.5s 后停帧。
// 三个门禁任一关闭就停帧且 wake() 无效: 页面隐藏、容器不在视口、组件被 keep-alive 换出。
const IDLE_AFTER_MS = 1500
let activeUntil = 0
let pageVisible = typeof document === 'undefined' ? true : !document.hidden
let inView = true
let activated = true
let io = null

function canRender() { return !!renderer && pageVisible && inView && activated }

function wake(ms = IDLE_AFTER_MS) {
  activeUntil = Math.max(activeUntil, performance.now() + ms)
  if (!raf && canRender()) raf = requestAnimationFrame(loop)
}

function stopLoop() {
  if (raf) cancelAnimationFrame(raf)
  raf = 0
  lastFrameT = 0
}
```

`loop()` 改为:
```js
function loop() {
  raf = 0
  if (!canRender()) { lastFrameT = 0; return }
  const now = performance.now()
  const dt = lastFrameT ? Math.min(0.05, (now - lastFrameT) / 1000) : 0.016
  lastFrameT = now
  // OrbitControls.update() 在阻尼未停时返回 true —— 松手后惯性滑行期间持续续期。
  if (controls.update()) activeUntil = Math.max(activeUntil, now + IDLE_AFTER_MS)
  if (moteState) updateMotes(dt, now / 1000)
  if (pulseTween) pulseTween()
  renderer.render(scene, camera)
  if (now < activeUntil) raf = requestAnimationFrame(loop)
  else lastFrameT = 0
}
```

- [ ] **Step 2: 活跃来源接线**

在 `init()` 里 `controls = new OrbitControls(...)` 之后:
```js
  controls.addEventListener('start', () => wake())
  controls.addEventListener('change', () => wake())
```
`transformControls` 存在时, 在 `dragging-changed` 监听旁加:
```js
    transformControls.addEventListener('change', () => wake())
```
`ResizeObserver` 回调里 `camera.updateProjectionMatrix()` 后加 `wake()`。

`tweenCamera`: 函数体第一行 (`const startCam = ...` 之前) 加 `wake(duration + 200)`。
`pulseHighlight`: `const DURATION = 3000` 之后加 `wake(DURATION + 200)`。
`rebuild()`: 函数末尾 (最后一个 `}` 之前) 加 `wake()`。
`updateItemVisibility()`、`updateRoomLights()`、`occludeForMultiHighlight()`、`applySelection()`: 各自函数末尾加 `wake()` (这些都改材质/可见性, 需要至少一帧)。

- [ ] **Step 3: 可见性门禁**

`init()` 末尾 (`resizeObserver.observe(...)` 后):
```js
  io = new IntersectionObserver(([entry]) => {
    inView = !!entry?.isIntersecting
    if (inView) wake(); else stopLoop()
  }, { threshold: 0 })
  io.observe(container.value)
```

模块顶层 (script setup 里, `onMounted` 附近):
```js
function onVisibility() {
  pageVisible = !document.hidden
  if (pageVisible) wake(); else stopLoop()
}
onMounted(() => {
  document.addEventListener('visibilitychange', onVisibility)
  init(); rebuild(); wake()
})
onActivated(() => { activated = true; wake() })
onDeactivated(() => { activated = false; stopLoop() })
```
import 行补 `onActivated, onDeactivated`。原 `onMounted(() => { init(); rebuild(); loop() })` 删掉。

- [ ] **Step 4: disposeAll + onBeforeUnmount**

```js
function disposeAll() {
  stopLoop()
  io?.disconnect(); io = null
  resizeObserver?.disconnect(); resizeObserver = null
  controls?.dispose()
  transformControls?.dispose?.()
  disposeExtras()
  if (renderer) { renderer.dispose(); renderer.domElement.remove() }
}
onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', onVisibility)
  disposeAll()
})
```

- [ ] **Step 5: lowQuality 原地切换**

`init()` 里把 `sun` 与 `floor` 提升为模块级 `let sun = null, floor = null` (与 `scene, camera, renderer` 同一行声明处)。`init()` 中 sun 的阴影参数**无条件**配置 (只有 `castShadow` 跟 lowQuality 走):
```js
  sun = new THREE.DirectionalLight(0xffeed5, props.lowQuality ? 1.0 : 0.9)
  sun.position.set(18, 28, 14)
  sun.castShadow = !props.lowQuality
  sun.shadow.mapSize.set(1024, 1024)
  sun.shadow.camera.near = 1
  sun.shadow.camera.far = 80
  sun.shadow.camera.left = -25
  sun.shadow.camera.right = 25
  sun.shadow.camera.top = 25
  sun.shadow.camera.bottom = -25
  sun.shadow.bias = -0.0005
  scene.add(sun)
```
`const floor = ...` → `floor = ...`。

删掉旧的 `watch(() => props.lowQuality, () => {...重建...})` 整段, 换成:
```js
// 省电/全光照切换: 原地调 renderer / 灯 / 尘粒, 再 rebuild() 让每个房间的吸顶灯与
// 网格阴影标志按新模式重建。以前是销毁并重建整个 WebGL 上下文, 切一次卡半秒。
// antialias 是 WebGLRenderer 构造期参数改不了 —— 沿用初始化时的值, 可接受。
function applyQuality() {
  if (!renderer) return
  const lq = props.lowQuality
  renderer.shadowMap.enabled = !lq
  renderer.setPixelRatio(Math.min(lq ? 1.5 : 2, window.devicePixelRatio || 1))
  sun.intensity = lq ? 1.0 : 0.9
  sun.castShadow = !lq
  floor.receiveShadow = !lq
  floor.material.needsUpdate = true
  if (motes) { scene.remove(motes); moteState.geo.dispose(); moteState.mat.dispose(); motes = null; moteState = null }
  if (!prefersReducedMotion) {
    moteState = buildMotes(lq ? 200 : 460)
    motes = moteState.points
    scene.add(motes)
  }
  rebuild()
}
watch(() => props.lowQuality, applyQuality)
```
`disposeExtras()` 里的 motes 清理已存在, `applyQuality` 自己清是因为不想顺带 dispose pmrem/bgTexture。

- [ ] **Step 6: 指纹 watcher**

删三个 `{ deep: true }` watcher, 换成:
```js
// 用指纹字符串代替 deep watch: 1000 条物品的递归深比较每次 props 变化都要跑, 而且
// 引用变了内容没变 (刷新拿回同样的数据) 也会触发整场重建。指纹只含 3D 用到的字段。
const itemsFp = computed(() => (props.items || [])
  .map((i) => `${i.id}:${i.name}:${i.quantity}:${i.location_id}:${i.pos_x ?? ''}:${i.pos_z ?? ''}`)
  .join('|'))
const locsFp = computed(() => (props.locations || [])
  .map((l) => `${l.id}:${l.parent_id}:${l.kind}:${l.name}:${JSON.stringify(l.geometry ?? null)}`)
  .join('|'))
watch([itemsFp, locsFp], rebuild)
watch(() => props.highlightItemId, (v) => {
  if (v) focusItems([v])
  updateRoomLights(); updateItemVisibility()
})
watch(() => (props.highlightItemIds || []).join(','), () => {
  const v = props.highlightItemIds
  if (Array.isArray(v) && v.length) focusItems(v)
  updateRoomLights(); updateItemVisibility()
})
watch(() => props.highlightLocationId, (v) => { if (v) focusLocation(v); updateRoomLights() })
watch(() => props.selectedLocationId, applySelection)
watch(() => (props.showItemsInRoomIds || []).join(','), updateItemVisibility)
```

- [ ] **Step 7: 构建 + 手工验证**

`npm run build` 通过 (注意 Scene3D chunk 仍是独立 chunk)。dev 模式打开 3D 页:
1. 拖动旋转 → 流畅; 松手惯性滑行结束 ~1.5s 后, Performance 面板录 3s 应看到**零** `requestAnimationFrame` 任务。
2. 点省电/光照切换 → 阴影/吸顶灯立即变, 无黑屏闪烁。
3. 语音页找一个物品 → 相机推进 + 脉冲 3s, 然后停帧。
4. 切到物品页再切回 3D → 画面保留, 一帧后静止。
5. 浏览器切到别的标签再回来 → 有一帧重绘。
6. 在 3D 页编辑器里拖动家具 → 3D 视图实时跟随 (指纹里有 geometry)。

- [ ] **Step 8: 核对**

`git diff --stat frontend/src/components/Scene3D.vue` — 只此一个文件。

---

### Task 10: 前端死代码

**Files:**
- Modify: `frontend/src/api.js:21-26, 31, 46-51, 54-59`
- Modify: `frontend/src/composables/sceneLayout.js:43, 52, 122, 200`
- Modify: `frontend/src/composables/useVoice.js:20, 30`
- Modify: `frontend/package.json:2`, `frontend/package-lock.json:2,8`

- [ ] **Step 1: api.js**

在 `request()` 之后加:
```js
// 过滤掉 undefined / null / '' 再拼 query string —— 三个列表接口共用。
function qs(params = {}) {
  const s = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
  ).toString()
  return s ? '?' + s : ''
}
```
三处改为:
```js
  listItems: (params = {}) => request(`/api/items${qs(params)}`),
  searchTx: (params = {}) => request(`/api/transactions${qs(params)}`),
  listAudit: (params = {}) => request(`/api/audit${qs(params)}`),
```
删 `itemTransactions: (id) => ...` 一行。

- [ ] **Step 2: sceneLayout.js**

对 `KIND_DEFAULTS`、`defaultsFor`、`snapAngleDeg`、`polygonSignedArea`: 先 `grep -rn "\bNAME\b" frontend/src` 确认只在本文件出现。若本文件内部有引用 → 只去掉 `export` 关键字; 若本文件内也无引用 → 整个删除。

- [ ] **Step 3: useVoice.js**

`export function classifyYesNo` → `function classifyYesNo`; `export function classifyAnswer` → `function classifyAnswer` (文件内部与 `return {...}` 里仍在用, 只去 export)。

- [ ] **Step 4: package name**

`frontend/package.json` `"name": "storage-frontend"` → `"storage-app"`; `package-lock.json` 里两处 (`grep -n storage-frontend`) 同步。

- [ ] **Step 5: 构建**

`npm run build` 通过; `grep -rn "itemTransactions\|storage-frontend" frontend/src frontend/package*.json` 零命中。

- [ ] **Step 6: 核对**

`git diff --stat`: 4 个文件 + lock。

---

### Task 11: 重建镜像、部署、验证

**Files:** 无代码改动。

- [ ] **Step 1: 记录改前基线**

```bash
cd /home/xyz/Projects/priv/repo_git
openssl x509 -in data/certs/ca.crt -noout -fingerprint -sha256
python3 -c "import sqlite3;c=sqlite3.connect('data/storage.db');print([c.execute('select count(*) from '+t).fetchone()[0] for t in ('items','locations','transactions','audit_log')])"
```
记下指纹与四个数字。

- [ ] **Step 2: 全量后端测试 + eval 回放** (用改动后的源码 bind mount 进**旧**镜像跑, 依赖没变所以可行)

两条 docker 命令全绿。

- [ ] **Step 3: 重建并重启**

```bash
./start.sh --whisper
docker ps --format '{{.Names}}\t{{.Status}}' | grep storage
```
等 `storage-app` 变 `healthy`。

- [ ] **Step 4: 三项检查 + 新增两项**

```bash
# 1. CA 指纹与 Step 1 一致
openssl x509 -in data/certs/ca.crt -noout -fingerprint -sha256
# 2. 数据条数与 Step 1 一致
python3 -c "import sqlite3;c=sqlite3.connect('data/storage.db');print([c.execute('select count(*) from '+t).fetchone()[0] for t in ('items','locations','transactions','audit_log')])"
# 3. 线上 JS 含新代码 (useInventoryStore 的请求参数)
JS=$(curl -sk https://127.0.0.1:8443/ | grep -oE 'assets/index-[A-Za-z0-9_-]+\.js' | head -1)
curl -sk "https://127.0.0.1:8443/$JS" | grep -c "include_depleted"
# 4. gzip + 缓存头
curl -skI -H 'Accept-Encoding: gzip' "https://127.0.0.1:8443/$JS" | grep -iE 'content-encoding|cache-control'
# 5. 索引已建
python3 -c "import sqlite3;c=sqlite3.connect('data/storage.db');print([r[1] for r in c.execute('PRAGMA index_list(transactions)')])"
# 6. 后端日志无异常
docker logs storage-app --since 2m 2>&1 | grep -iE 'error|traceback' | head
```
Expected: 指纹与条数不变; grep 计数 ≥1; 头里有 `Content-Encoding: gzip` 与 `Cache-Control: public, immutable`; 索引列表含两个 `ix_transactions_*`; 无 traceback。

- [ ] **Step 5: 浏览器抽查**

`https://<LAN_IP>:8443` 打开: 首页 3D 预览正常显示并在无交互时停帧; `#tab=backup` 刷新停在备份页; 诊断页日志每 3s 刷新, 切走停止; 语音说一句"找卷尺"走完整流程。

- [ ] **Step 6: 汇报**

向用户汇报: 测试数 (69 → 74+)、eval 准确率不变、6 项检查结果、`git status --short` 全部改动文件清单。**不 commit。**

---

## Self-Review 记录

**Spec 覆盖**: A1 → Task 9; A2 → Task 7 + 8; A3 → Task 6; A4 → Task 1 + 2 + 3; A5 → Task 4; A6 → Task 5 + 10; 测试/部署验证 → Task 11。spec 里 A4 "pending-returns 改聚合查询" 在写计划时发现会改变语义 (运行余额有 `max(0,…)` 钳位), 已改为"预取 + 只走有 take_out 的物品", spec 需同步这一句。

**类型一致性**: `useInventoryStore` 的 `loadAll()` 返回 `[locations, items]` 顺序在 Task 7 四处调用一致; `usePausablePoll(fn, ms)` 在 Task 8 两处一致; `wake()/stopLoop()/canRender()` 在 Task 9 各步一致; `_loop_iterations` 在 Task 4 三个模块与测试一致。
