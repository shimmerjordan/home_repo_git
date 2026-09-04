# AI 准确性与群机器人确认 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补上评测台的覆盖空洞 (批量删除、确认后的 apply 阶段), 修掉位置子串误命中、两条执行路径不一致、多物品句 force_new 归属、量词与否定句四类准确性问题, 并给三个群机器人加"高风险操作先出方案等确认"。

**Architecture:** 先扩评测台再改代码 —— 每项修复都有能复现问题的 case (改前红、改完绿)。后端逻辑类修复 (位置歧义、路径统一) 靠 unittest + 现有 cassette 回放验证; prompt 类修复 (force_new、量词否定) 会让全部 cassette 失效, 集中放在一起改并一次性重录。群机器人的确认能力抽成 `services/botflow.py` 一个公共 handler, 三端只负责收发消息与身份提取, 顺带消掉三处复制粘贴的 confidence 强抬。

**Tech Stack:** FastAPI 0.115 / SQLAlchemy 2.0 / SQLite; unittest (非 pytest); 自建 eval 台 (cassette 录制回放); LLM 走用户自建 cc-trans 代理 (`claude-opus-4-8`)。

**Spec:** `docs/superpowers/specs/2026-09-04-ai-accuracy-design.md`

## Global Constraints

- **不 push。** 每个任务一个 commit, 分支 `perf-power-cleanup`, commit message 不带 Co-Authored 之类尾注, 用中文简述。
- **不要重建镜像 / 不要重启容器** —— 只有最后一个任务做部署验证。
- `backend/requirements.txt` 的 `websockets==16.0` 不动。
- 宿主机没有 fastapi。后端测试用 docker bind mount 跑 (unittest, 不是 pytest):
  ```bash
  cd /home/xyz/Projects/priv/repo_git
  docker run --rm -v "$PWD/backend:/src" -w /src/tests \
    -e DATABASE_URL=sqlite:// -e CONFIG_PATH=/tmp/ci-config.json -e LOG_DIR=/tmp/ci-logs \
    repo_git-app python -m unittest discover -s . -v
  ```
  单文件把 `discover -s . -v` 换成 `test_xxx -v`。
- eval 离线回放 (不联网):
  ```bash
  cp data/config.json /tmp/evalcfg/config.json   # 必须可写: 载入时会迁移并回写
  docker run --rm -v "$PWD/backend:/src" -v /tmp/evalcfg:/cfg -w /src --network none \
    -e CONFIG_PATH=/cfg/config.json repo_git-app python -m eval.run_eval --replay
  ```
  录制新 cassette 时**去掉 `--network none` 和 `--replay`**, 会真的调 LLM (走用户自建 cc-trans, 有成本)。
- **改前基线**: `.superpowers/sdd/2026-09-03-perf-power-cleanup/eval-baseline-before-B.txt` —— 32 条 case, 全部分类 100%。原有 32 条**不得回退**。
- 现有后端测试 76 个必须始终全绿。

## 关于 cassette 失效 (影响任务顺序, 必读)

`eval/run_eval.py:40-47` 的 `_key()` 把 `messages` 整个哈希进 cassette 文件名, 而 `messages` 含 `SYSTEM_PROMPT`。**所以任何一个字的 prompt 改动都会让 32 个 cassette 全部失效**, `--replay` 会报 "cassette 缺失"。

因此任务顺序是: 先做**不碰 prompt** 的后端修复 (T1-T5, 全程可 `--replay` 验证), 再把两个需要改 prompt 的修复 (T6-T7) 放在一起, 然后 T8 一次性重录全部 cassette 并重新测量。不要在 T1-T5 期间碰 `SYSTEM_PROMPT` / `INTENT_SCHEMA_HINT` / `TOOLS`。

---

## 文件结构

**新建**
- `backend/app/services/pending.py` — 群机器人待确认方案的内存存储 + 确认词识别
- `backend/app/services/risk.py` — 方案风险判定
- `backend/app/services/botflow.py` — 三端共用的消息处理流程
- `backend/app/services/botfmt.py` — 方案渲染成群消息纯文本
- `backend/tests/test_location_ambiguity.py`
- `backend/tests/test_bot_confirm_flow.py`

**修改**
- `backend/app/llm/intent.py` — 位置解析、路径统一、force_new、量词否定、prompt
- `backend/app/services/inventory.py` — 库存增减共享 helper
- `backend/app/routers/revise.py`、`backend/app/routers/items.py` — 改用共享 helper
- `backend/app/routers/dingtalk.py`、`backend/app/services/telegram.py`、`backend/app/services/feishu.py` — 接入 botflow
- `backend/app/schemas.py` — operation 增 `location_options`
- `backend/eval/cases.py`、`backend/eval/run_eval.py` — 新 case 与 `after` 断言
- `backend/tests/test_intent_plan.py` — 扩充

---

### Task 1: eval 台支持 apply 阶段与落库断言

**Files:**
- Modify: `backend/eval/run_eval.py:143-170` (`run_once`)、`:117-141` (`score_case`)
- Modify: `backend/eval/cases.py` (文件头 docstring 补新字段说明)

**Interfaces:**
- Produces: case 新增两个可选字段 —
  - `decisions: list[dict]` —— 有它就在 plan 之后再跑一次 apply。每条 `{intent, option_key, new_item_name?, location_name?, quantity?}`, `option_key ∈ {"new","skip","i:<fixture物品名>"}`。**注意**: 写 case 时用物品名而不是 id (fixture 每次新建库, id 不稳定), 由 `run_once` 在跑之前翻译成 `i:<真实id>`。
  - `after: list[dict]` —— apply 之后对库的断言, 每条 `{item, loc?, qty?, exists?}`。`qty` 断言数量, `exists=False` 断言物品已被删除。
- Produces: `score_case` 返回值新增 `after_ok: bool | None` (无 `after` 字段时为 None)。

- [ ] **Step 1: 写一个会失败的 case 验证机制不存在**

在 `backend/eval/cases.py` 的 `CASES` 末尾加:

```python
    # ---- 确认后落库 (端到端 plan → decisions → apply) ----
    dict(id="apply-takeout", cat="apply", text="拿两个电池",
         ops=[dict(intent="take_out", item="电池", qty=2)],
         decisions=[dict(intent="take_out", option_key="i:电池@书桌1", quantity=2)],
         after=[dict(item="电池", loc="书桌1", qty=6)]),
```

fixture 里"电池"在书桌1 有 8 个、洗漱柜有 4 个 (见 `backend/tests/_fixtures.py` 的 `ITEMS`), 所以取走 2 个之后书桌1 应剩 6。`option_key` 里的 `电池@书桌1` 是"物品名@位置名"写法, 用来在同名多处时定位。

- [ ] **Step 2: 跑一次确认它没被执行 (机制不存在)**

Run: eval 回放命令 (见 Global Constraints)
Expected: 这条 case 会因为 `apply` 分类不存在于报告、或 `decisions`/`after` 被忽略而**不做任何落库校验** —— 也就是说它会"假绿"。用 `--verbose` 观察确认它只跑了 plan。这一步是为了确认现状, 不是为了看红。

- [ ] **Step 3: 实现 decisions 翻译与 apply 执行**

在 `run_eval.py` 的 `run_once` 里, `result = I.execute_intent(...)` 之后加:

```python
        after_ok = None
        if case.get("decisions"):
            # option_key 里的 "i:物品名@位置名" 要翻译成真实 id —— fixture 每个 case
            # 重新建库, id 不稳定, 所以 case 里只能写名字。
            decisions = [_resolve_decision(db, d) for d in case["decisions"]]
            base = dict(result)
            result = I.apply_operations(db, case["text"], decisions, base)
            db.commit()
        if case.get("after"):
            after_ok = _check_after(db, case["after"])
```

并新增两个 helper (放在 `score_case` 之前):

```python
def _find_fixture_item(db, spec: str):
    """"物品名" 或 "物品名@位置名" → Item 对象。同名多处时必须带位置。"""
    from app import models
    name, _, loc_name = spec.partition("@")
    rows = db.query(models.Item).filter(models.Item.name == name).all()
    if loc_name:
        rows = [r for r in rows if r.location and r.location.name == loc_name]
    if len(rows) != 1:
        raise AssertionError(f"case 里的 {spec!r} 在 fixture 里命中 {len(rows)} 个, 必须唯一")
    return rows[0]


def _resolve_decision(db, d: dict) -> dict:
    out = dict(d)
    key = out.get("option_key") or ""
    if key.startswith("i:"):
        out["option_key"] = f"i:{_find_fixture_item(db, key[2:]).id}"
    return out


def _check_after(db, expects: list[dict]) -> bool:
    """apply 之后核对库里的真实状态。"""
    from app import models
    for exp in expects:
        name, _, loc_name = exp["item"].partition("@")
        q = db.query(models.Item).filter(models.Item.name == name)
        rows = [r for r in q.all()
                if not loc_name or (r.location and r.location.name == loc_name)]
        if "loc" in exp:
            rows = [r for r in rows if r.location and r.location.name == exp["loc"]]
        if exp.get("exists") is False:
            if rows:
                return False
            continue
        if len(rows) != 1:
            return False
        if "qty" in exp and rows[0].quantity != exp["qty"]:
            return False
    return True
```

`score_case(case, result)` 改签名为 `score_case(case, result, after_ok=None)`, 返回值里加 `after_ok=after_ok`; `run_once` 里调用处同步传参。

- [ ] **Step 4: 报告加一列**

`run_eval.py` 的总览表新增"落库对"列 —— 统计口径: 该分类里有 `after` 字段的 case 中 `after_ok` 为真的比例; 没有任何 `after` 的分类显示 `-`。找到打印总览的那段 (搜 `===== 总览 =====`), 按现有列的写法加一列, 保持对齐。

- [ ] **Step 5: 录 cassette 并验证**

新 case 的文本 "拿两个电池" 需要一次真实 LLM 调用录 cassette。**去掉 `--network none` 和 `--replay`** 跑一次 (会真的花钱), 然后再用 `--replay` 跑一遍确认可离线复现。

Expected: `apply` 分类出现在报告里, 落库对 = 100%; 原有 32 条全部保持 100%。

- [ ] **Step 6: Commit**

```bash
git add backend/eval/run_eval.py backend/eval/cases.py backend/eval/cassettes/
git commit -m "评测台支持 apply 阶段与落库断言"
```

---

### Task 2: 补批量删除与 apply 的 case

**Files:**
- Modify: `backend/eval/cases.py`

**Interfaces:**
- Consumes: Task 1 的 `decisions` / `after` 字段与 `i:物品名@位置名` 写法。

- [ ] **Step 1: 加 case**

在 `CASES` 里加 (delete 三条 + apply 三条):

```python
    # ---- 删除物品档案 ----
    dict(id="del-single", cat="delete", text="把螺丝刀这条记录删掉",
         ops=[dict(intent="delete_item", item="螺丝刀")]),
    dict(id="del-multi", cat="delete", text="把卷尺和螺丝刀的记录都删了",
         ops=[dict(intent="delete_item", item="卷尺"),
              dict(intent="delete_item", item="螺丝刀")]),
    dict(id="del-absent", cat="delete", text="把跑步机的记录删掉",
         ops=[dict(intent="delete_item", skip=True)]),

    # ---- 确认后落库 ----
    dict(id="apply-consume", cat="apply", text="用完了一瓶洗手液",
         ops=[dict(intent="consume", item="洗手液", qty=1)],
         decisions=[dict(intent="consume", option_key="i:洗手液", quantity=1)],
         after=[dict(item="洗手液", qty=0)]),
    dict(id="apply-new", cat="apply", text="新增一个订书机放到书桌1",
         ops=[dict(intent="create_item", new="订书机", qty=1, to="书桌1")],
         decisions=[dict(intent="create_item", option_key="new",
                         new_item_name="订书机", location_name="书桌1", quantity=1)],
         after=[dict(item="订书机", loc="书桌1", qty=1)]),
    dict(id="apply-delete", cat="delete", text="把卷尺这条记录删掉",
         ops=[dict(intent="delete_item", item="卷尺")],
         decisions=[dict(intent="delete_item", option_key="i:卷尺")],
         after=[dict(item="卷尺", exists=False)]),
```

- [ ] **Step 2: 录 cassette**

去掉 `--network none --replay` 跑一次录制这 6 条的响应。

- [ ] **Step 3: 用 --replay 跑, 观察结果**

Expected: 可能有 case 不过 —— 这正是要发现的。**如果 `del-*` 全绿说明删除意图本来就准, 那就如实记录**, 本任务的价值是补上覆盖而非制造失败。把每条的实际结果写进报告。

- [ ] **Step 4: 若有不过的, 分析原因但先不修**

不过的 case 记进报告, 说明是 prompt 问题还是后端问题。prompt 问题留到 T6/T7 一起处理 (改 prompt 会让 cassette 全失效)。后端问题若属于 T3/T4 范围, 在那两个任务里修。

- [ ] **Step 5: Commit**

```bash
git add backend/eval/cases.py backend/eval/cassettes/
git commit -m "补批量删除与确认落库的评测 case"
```

---

### Task 3: 位置名歧义不猜

**Files:**
- Modify: `backend/app/llm/intent.py:347-368` (`_resolve_location`) 与 5 处调用点 (`:734, 837, 982, 1003, 1169`)
- Modify: `backend/app/schemas.py` (`IntentOperationResult` 加字段)
- Create: `backend/tests/test_location_ambiguity.py`

**Interfaces:**
- Produces: `_resolve_location(db, ref) -> tuple[int | None, list[models.Location]]` —— 返回 (命中id, 歧义候选列表)。**所有 5 个调用点都要改**, 旧签名返回裸 int。
- Produces: operation 新字段 `location_options: list[dict]`, 每条 `{location_id, name, path}`, 无歧义时为 `[]`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_location_ambiguity.py
"""位置名子串误命中: 说"书桌1"而库里只有"书桌10"时, 老实现会静默落到书桌10。
现在的规则是: 精确名优先; 无精确名且多个子串候选时不猜, 把候选交给用户选。"""
from __future__ import annotations

import unittest

from _fixtures import make_session, seed
from app import models
from app.llm import intent as I


class ResolveLocationTest(unittest.TestCase):
    def setUp(self):
        self.db = make_session()
        self.by_path, _ = seed(self.db)

    def _add_loc(self, name, parent_path):
        loc = models.Location(name=name, kind="box",
                              parent_id=self.by_path[parent_path].id)
        self.db.add(loc); self.db.flush()
        return loc

    def test_exact_name_wins_over_substring(self):
        """库里同时有"书桌1"和"书桌10"时, 说"书桌1"必须命中书桌1。"""
        self._add_loc("书桌10", "我家/书房")
        loc_id, ambiguous = I._resolve_location(self.db, {"location_name": "书桌1"})
        self.assertEqual(loc_id, self.by_path["我家/书房/书桌1"].id)
        self.assertEqual(ambiguous, [])

    def test_ambiguous_substring_returns_candidates(self):
        """说"书桌"而库里有"书桌1"和"书桌10" —— 两个都像, 不许猜。"""
        self._add_loc("书桌10", "我家/书房")
        loc_id, ambiguous = I._resolve_location(self.db, {"location_name": "书桌"})
        self.assertIsNone(loc_id)
        names = sorted(l.name for l in ambiguous)
        self.assertEqual(names, ["书桌1", "书桌10"])

    def test_single_substring_still_resolves(self):
        """只有一个候选时照常命中 —— 不能因为怕歧义就什么都不解析。"""
        loc_id, ambiguous = I._resolve_location(self.db, {"location_name": "洗漱"})
        self.assertEqual(loc_id, self.by_path["我家/卫生间/洗漱柜"].id)
        self.assertEqual(ambiguous, [])

    def test_no_match_returns_none(self):
        loc_id, ambiguous = I._resolve_location(self.db, {"location_name": "阁楼"})
        self.assertIsNone(loc_id)
        self.assertEqual(ambiguous, [])

    def test_explicit_id_wins(self):
        target = self.by_path["我家/书房/工具箱"]
        loc_id, ambiguous = I._resolve_location(
            self.db, {"location_id": target.id, "location_name": "书桌"})
        self.assertEqual(loc_id, target.id)
        self.assertEqual(ambiguous, [])


class PlanLocationAmbiguityTest(unittest.TestCase):
    def test_plan_surfaces_location_options(self):
        """方案里该出现 location_options 让用户选, 而不是替他选一个。"""
        db = make_session()
        by_path, _ = seed(db)
        db.add(models.Location(name="书桌10", kind="box",
                               parent_id=by_path["我家/书房"].id))
        db.flush()
        op = {"intent": "put_in", "item_name": "卷尺", "quantity": 1,
              "location_name": "书桌", "item_id": None, "location_id": None,
              "force_new": False}
        r = I._plan_one(db, op)
        self.assertIsNone(r["location_id"])
        self.assertEqual(len(r["location_options"]), 2)
        self.assertIn("书桌", r["reason"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: docker 命令末尾 `test_location_ambiguity -v`
Expected: FAIL —— `TypeError: cannot unpack non-sequence int` 或类似 (旧签名返回裸 int)。

- [ ] **Step 3: 改 `_resolve_location`**

```python
def _resolve_location(
    db: Session, ref: dict[str, Any]
) -> tuple[int | None, list[models.Location]]:
    """把 location_id / location_name 解析成真实 Location id。

    返回 (命中id, 歧义候选)。规则: 精确名 (大小写不敏感) 直接胜出; 没有精确名时
    收集**全部**子串候选 —— 恰好一个就用它, 多个就一个都不选, 把候选交回给调用方
    让用户挑。老实现取第一个子串命中, "书桌1"会静默落到"书桌10"上。
    """
    if ref.get("location_id"):
        loc = db.get(models.Location, ref["location_id"])
        if loc:
            return loc.id, []
    name = (ref.get("location_name") or "").strip()
    if not name:
        return None, []
    name_lower = name.lower()
    candidates: list[models.Location] = []
    for loc in _all_locations(db):
        ln = (loc.name or "").lower()
        if ln == name_lower:
            return loc.id, []
        if (name_lower in ln or ln in name_lower
                or name_lower in location_path(loc).lower()):
            candidates.append(loc)
    if len(candidates) == 1:
        return candidates[0].id, []
    if candidates:
        # 名字长度最接近的排前面, 同长按全路径排 —— 只影响展示顺序, 不影响"不猜"。
        candidates.sort(key=lambda l: (abs(len(l.name or "") - len(name)),
                                       location_path(l)))
        return None, candidates
    return None, []
```

- [ ] **Step 4: 改 5 个调用点**

- `:734` (`_plan_one`): `loc_id, loc_ambig = _resolve_location(db, op)`; 返回值里加 `"location_options": [{"location_id": l.id, "name": l.name, "path": location_path(l)} for l in loc_ambig]`; 当 `loc_ambig` 非空时 `reason` 追加 `f"位置「{op.get('location_name')}」有 {len(loc_ambig)} 个候选, 请选一个"`。
- `:837` (`apply_operations`): `loc_id, loc_ambig = _resolve_location(db, d)`; 若 `loc_ambig` 非空 → 该条不落库, `r["speech"] = f"位置「{d.get('location_name')}」不明确, 没执行"`, `r["pending"] = True`, `continue`。
- `:982`、`:1003` (`_execute_batch`): 同样解包; 歧义时该条 `pending=True` + `matched_by="none"`, 不落库。
- `:1169` (`execute_intent`): `parsed["location_id"], _ = _resolve_location(db, parsed)`。

其余返回 `_plan_one` 结构的地方 (`_do_find` 等) 也要补 `"location_options": []`, 否则 schema 校验会缺字段 —— 搜 `"new_name_default"` 找到所有构造点。

- [ ] **Step 5: schema 加字段**

`backend/app/schemas.py` 的 `IntentOperationResult` 加:
```python
    location_options: list[dict[str, Any]] = []
```

- [ ] **Step 6: 跑测试**

Run: `test_location_ambiguity -v` → PASS; `discover -s . -v` → 全绿 (原 76 + 新增 6); eval `--replay` 与基线一致。

- [ ] **Step 7: Commit**

```bash
git add backend/app/llm/intent.py backend/app/schemas.py backend/tests/test_location_ambiguity.py
git commit -m "位置名歧义时不再猜: 精确优先, 多候选交给用户选"
```

---

### Task 4: 两条执行路径统一 + 库存增减收敛

**Files:**
- Modify: `backend/app/services/inventory.py` (新增共享 helper)
- Modify: `backend/app/llm/intent.py:522-543` (`_apply_stock_op`)、`:1006-1021` (`_execute_batch` 的 put_in 分支)
- Modify: `backend/app/routers/revise.py:145-151`、`backend/app/routers/items.py:371-379`
- Test: `backend/tests/test_intent_plan.py`

**Interfaces:**
- Produces: `services/inventory.py::apply_quantity_delta(item, action, qty, loc_id) -> None` —— **只改物品的数量与位置**, 不建 Transaction (三处的 tx 构造方式各不相同, 强行统一会出错)。`action ∈ {"take_out","consume","put_in","adjust"}`; `adjust` 是**绝对赋值**不是增减。

**注意**: 这三处**不是逐字重复**。`items.py:371` 多一个 `adjust` 分支且用 `models.Transaction(**payload.model_dump())` 建 tx; `revise.py:145` 是 redirect 流程的一部分。所以只抽"数量怎么变"这一段, 不要连 tx 构造一起抽。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_intent_plan.py` 末尾加:

```python
class UnifiedPathTest(unittest.TestCase):
    """非 plan 路径 (群机器人走的那条) 以前 put_in 找不到物品就静默新建,
    而 plan 路径会问用户。两条路径必须一致: 除非明确 force_new, 否则不许自动建档。"""

    def test_batch_putin_unknown_item_does_not_autocreate(self):
        db = make_session()
        seed(db)
        before = db.query(models.Item).count()
        parsed = {"intent": "put_in", "confidence": 1.0, "speech": "",
                  "operations": [{"intent": "put_in", "item_name": "跑步机",
                                  "quantity": 1, "location_name": "书房",
                                  "item_id": None, "location_id": None,
                                  "force_new": False}]}
        from app.config import store
        r = I.execute_intent(db, "把跑步机放进书房", parsed, store.get())
        self.assertEqual(db.query(models.Item).count(), before,
                         "不该静默新建物品档案")
        op = r["operations"][0]
        self.assertTrue(op["pending"])
        self.assertFalse(op["executed"])

    def test_batch_putin_with_force_new_still_creates(self):
        """明确说了是新东西, 照建不误 —— 收紧的是"没说"的情况。"""
        db = make_session()
        seed(db)
        parsed = {"intent": "put_in", "confidence": 1.0, "speech": "",
                  "operations": [{"intent": "put_in", "item_name": "跑步机",
                                  "quantity": 1, "location_name": "书房",
                                  "item_id": None, "location_id": None,
                                  "force_new": True}]}
        from app.config import store
        I.execute_intent(db, "新增跑步机到书房", parsed, store.get())
        self.assertEqual(
            db.query(models.Item).filter(models.Item.name == "跑步机").count(), 1)


class QuantityDeltaTest(unittest.TestCase):
    def test_adjust_is_absolute_not_delta(self):
        from app.services.inventory import apply_quantity_delta
        db = make_session()
        _, items = seed(db)
        it = next(i for i in items if i.name == "螺丝" )
        apply_quantity_delta(it, "adjust", 5, None)
        self.assertEqual(it.quantity, 5)

    def test_take_out_clamps_at_zero(self):
        from app.services.inventory import apply_quantity_delta
        db = make_session()
        _, items = seed(db)
        it = next(i for i in items if i.name == "洗手液")
        apply_quantity_delta(it, "take_out", 99, None)
        self.assertEqual(it.quantity, 0)
```

确认文件顶部已 import `models`、`store` 相关内容, 缺的补上。

- [ ] **Step 2: 跑测试确认失败**

Run: `test_intent_plan -v`
Expected: `test_batch_putin_unknown_item_does_not_autocreate` FAIL (物品数 +1); `QuantityDeltaTest` FAIL (`ImportError: cannot import name 'apply_quantity_delta'`)。

- [ ] **Step 3: 加共享 helper**

`backend/app/services/inventory.py` 里加 (放在 `UNDOABLE_ACTIONS` 附近):

```python
def apply_quantity_delta(item, action: str, qty: int, loc_id: int | None) -> None:
    """按动作改物品的数量与位置。**只动 item, 不建 Transaction** ——
    三个调用方 (语音执行 / 改判 / 手动记一笔) 的 tx 构造方式各不相同, 硬凑一起反而容易错。

    adjust 是绝对赋值 (手动盘点"现在还剩 N 个"), 其余是增减。
    """
    if action in ("take_out", "consume"):
        item.quantity = max(0, (item.quantity or 0) - qty)
    elif action == "put_in":
        item.quantity = (item.quantity or 0) + qty
        if loc_id:
            item.location_id = loc_id
    elif action == "adjust":
        item.quantity = qty
    item.updated_at = datetime.now()
```

确认 `inventory.py` 顶部已 `from datetime import datetime`, 没有就加。

- [ ] **Step 4: 三处改用它**

- `intent.py:_apply_stock_op`: 把数量那几行换成 `apply_quantity_delta(item, intent, qty, loc_id)`, 保留原有的 Transaction 构造与 `db.add/flush`。
- `revise.py:145-148`: 换成 `apply_quantity_delta(target_item, action, qty, loc_id)`, 保留后面的 tx 构造。
- `items.py:371-378`: 换成 `apply_quantity_delta(item, payload.action, qty, payload.location_id)`, 保留 `models.Transaction(**payload.model_dump())`。

- [ ] **Step 5: 改 `_execute_batch` 的 put_in 分支**

`intent.py:1006-1021` 那段"找不到就 `_create_or_merge_item`"改成:

```python
                if intent == "put_in" and name:
                    if op.get("force_new"):
                        item, tx, _merged = _create_or_merge_item(
                            db, name, qty, loc_id, note="语音存入(新建)")
                        r["matched_by"] = "created"
                        # ... 保留原有的 executed/speech 赋值
                    else:
                        # 不许静默建档 —— 与 plan 路径一致: 没明说是新东西就先问。
                        r["pending"] = True
                        r["matched_by"] = "none"
                        r["speech"] = f"库里没有{name}, 要新建吗"
                        results.append(r)
                        continue
```

照着该函数现有的变量名与 results 组装方式改, 不要改动其它分支。

- [ ] **Step 6: 跑测试**

Run: `test_intent_plan -v` → PASS; `discover -s . -v` → 全绿; eval `--replay` —— **注意**: `absent-take` / `absent-consume` 这两条 case 期望的就是"库里没有 → skip", 行为应保持; 若有 case 因本改动变红, 说明它原本依赖自动建档, 在报告里说明并判断是 case 该改还是代码该改。

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/inventory.py backend/app/llm/intent.py \
        backend/app/routers/revise.py backend/app/routers/items.py \
        backend/tests/test_intent_plan.py
git commit -m "两条执行路径统一: put_in 找不到不再静默建档; 库存增减收敛成一份"
```

---

### Task 5: 待确认存储与风险判定

**Files:**
- Create: `backend/app/services/pending.py`
- Create: `backend/app/services/risk.py`
- Create: `backend/tests/test_bot_confirm_flow.py` (本任务只写前两个类)

**Interfaces:**
- Produces: `pending.py`
  - `put(channel: str, chat_id: str, sender_id: str, plan: dict) -> None`
  - `take(channel, chat_id, sender_id) -> dict | None` —— 取出并**删除** (确认只能用一次); 过期返回 None
  - `peek(channel, chat_id, sender_id) -> dict | None` —— 不删
  - `drop(channel, chat_id, sender_id) -> None`
  - `classify_reply(text: str) -> str` —— 返回 `"yes"` / `"no"` / `"other"`
  - 常量 `TTL_S = 300`、`MAX_ENTRIES = 200`
- Produces: `risk.py::plan_risk(operations: list[dict]) -> tuple[bool, str]` —— (是否高风险, 中文原因)

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_bot_confirm_flow.py
"""群机器人的"高风险先确认"流程。这里只测存储与风险判定, 三端接线在另一个测试里。"""
from __future__ import annotations

import time
import unittest

from _fixtures import make_session  # noqa: F401  (sys.path 注入)
from app.services import pending, risk


class PendingStoreTest(unittest.TestCase):
    def setUp(self):
        pending.drop("tg", "c1", "u1")

    def test_put_then_take_returns_plan_once(self):
        pending.put("tg", "c1", "u1", {"stage": "plan", "operations": []})
        got = pending.take("tg", "c1", "u1")
        self.assertEqual(got["stage"], "plan")
        self.assertIsNone(pending.take("tg", "c1", "u1"), "确认只能用一次")

    def test_keys_are_isolated_per_sender_and_chat(self):
        pending.put("tg", "c1", "u1", {"a": 1})
        self.assertIsNone(pending.peek("tg", "c1", "u2"))
        self.assertIsNone(pending.peek("tg", "c2", "u1"))
        self.assertIsNone(pending.peek("feishu", "c1", "u1"))

    def test_expires_after_ttl(self):
        pending.put("tg", "c1", "u1", {"a": 1})
        # 直接把创建时间往前拨, 不真的 sleep 300 秒
        pending._STORE[("tg", "c1", "u1")]["created_at"] = time.time() - pending.TTL_S - 1
        self.assertIsNone(pending.take("tg", "c1", "u1"))

    def test_new_plan_replaces_old(self):
        pending.put("tg", "c1", "u1", {"n": 1})
        pending.put("tg", "c1", "u1", {"n": 2})
        self.assertEqual(pending.take("tg", "c1", "u1")["n"], 2)

    def test_capacity_cap(self):
        for i in range(pending.MAX_ENTRIES + 20):
            pending.put("tg", f"c{i}", "u", {"i": i})
        self.assertLessEqual(len(pending._STORE), pending.MAX_ENTRIES)


class ClassifyReplyTest(unittest.TestCase):
    def test_yes_words(self):
        for t in ["确认", "确定", "是的", "对", "执行", "好"]:
            self.assertEqual(pending.classify_reply(t), "yes", t)

    def test_no_words(self):
        for t in ["取消", "不要", "算了", "不对"]:
            self.assertEqual(pending.classify_reply(t), "no", t)

    def test_other_is_not_confirmation(self):
        """"再拿个螺丝刀"是一句新指令, 绝不能当成确认。"""
        for t in ["再拿个螺丝刀", "螺丝刀在哪", "确认一下电池还有几个"]:
            self.assertEqual(pending.classify_reply(t), "other", t)


class PlanRiskTest(unittest.TestCase):
    def _op(self, **kw):
        base = dict(intent="put_in", matched_by="exact", pending=True,
                    location_options=[])
        base.update(kw)
        return base

    def test_delete_is_high_risk(self):
        high, why = risk.plan_risk([self._op(intent="delete_item")])
        self.assertTrue(high)
        self.assertIn("删除", why)

    def test_fuzzy_match_is_high_risk(self):
        high, why = risk.plan_risk([self._op(matched_by="fuzzy")])
        self.assertTrue(high)

    def test_three_or_more_mutations_is_high_risk(self):
        high, _ = risk.plan_risk([self._op(), self._op(), self._op()])
        self.assertTrue(high)

    def test_location_ambiguity_is_high_risk(self):
        high, _ = risk.plan_risk([self._op(
            location_options=[{"location_id": 1}, {"location_id": 2}])])
        self.assertTrue(high)

    def test_two_exact_mutations_are_low_risk(self):
        high, why = risk.plan_risk([self._op(), self._op()])
        self.assertFalse(high)
        self.assertEqual(why, "")

    def test_find_only_is_low_risk(self):
        high, _ = risk.plan_risk([self._op(intent="find"), self._op(intent="find"),
                                  self._op(intent="find"), self._op(intent="find")])
        self.assertFalse(high)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `test_bot_confirm_flow -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.services.pending'`。

- [ ] **Step 3: 写 `pending.py`**

```python
"""群机器人的待确认方案 —— 进程内内存 + TTL, 不落库。

为什么不落 DB: 超过几分钟的待确认方案本来就该失效, 重启丢掉是可接受的;
落库要建表、要清理、要迁移, 为一个 5 分钟的临时状态不值。
(同样的取舍见 services/feishu.py 的 _seen_message_ids 去重表。)

键是 (渠道, 会话, 发送者) —— 同一个人在同一个群里同时只有一个待确认方案,
再说一句就把旧的顶掉。不同人、不同群互不干扰。
"""
from __future__ import annotations

import re
import time
from typing import Any

TTL_S = 300
MAX_ENTRIES = 200

_STORE: dict[tuple[str, str, str], dict[str, Any]] = {}

# 只认明确的确认/取消词。认不出就当作新的一句话去解析 —— 把"再拿个螺丝刀"
# 误判成确认, 比不认识它糟糕得多。
_YES = re.compile(r"^\s*(确认|确定|确认执行|执行|是的?|对|好的?|嗯|ok|OK|yes|y)\s*[!!。.]*\s*$")
_NO = re.compile(r"^\s*(取消|不要|不用|算了|不对|否|no|n)\s*[!!。.]*\s*$")


def _key(channel: str, chat_id: str, sender_id: str) -> tuple[str, str, str]:
    return (str(channel), str(chat_id), str(sender_id))


def _sweep() -> None:
    now = time.time()
    for k in [k for k, v in _STORE.items() if now - v["created_at"] > TTL_S]:
        _STORE.pop(k, None)
    # 还超容就丢最老的
    while len(_STORE) > MAX_ENTRIES:
        oldest = min(_STORE, key=lambda k: _STORE[k]["created_at"])
        _STORE.pop(oldest, None)


def put(channel: str, chat_id: str, sender_id: str, plan: dict[str, Any]) -> None:
    _STORE[_key(channel, chat_id, sender_id)] = {"plan": plan, "created_at": time.time()}
    _sweep()


def peek(channel: str, chat_id: str, sender_id: str) -> dict[str, Any] | None:
    _sweep()
    entry = _STORE.get(_key(channel, chat_id, sender_id))
    return entry["plan"] if entry else None


def take(channel: str, chat_id: str, sender_id: str) -> dict[str, Any] | None:
    """取出并删除 —— 一个方案只能被确认一次。"""
    _sweep()
    entry = _STORE.pop(_key(channel, chat_id, sender_id), None)
    return entry["plan"] if entry else None


def drop(channel: str, chat_id: str, sender_id: str) -> None:
    _STORE.pop(_key(channel, chat_id, sender_id), None)


def classify_reply(text: str) -> str:
    """yes / no / other。other 表示"这不是确认, 当成新指令处理"。"""
    t = (text or "").strip()
    if _YES.match(t):
        return "yes"
    if _NO.match(t):
        return "no"
    return "other"
```

`_sweep` 在 `put` 之后调用, 所以 `test_capacity_cap` 的断言是 `<= MAX_ENTRIES`。

- [ ] **Step 4: 写 `risk.py`**

```python
"""方案风险判定 —— 决定群机器人是秒执行还是先问一句。

低风险 (秒回): 精确命中的存取、明确的新建。日常绝大多数。
高风险 (先出方案): 删档、模糊命中已有物品、一次改三条以上、位置没定。
这四类正是误操作真正会造成损失的地方。
"""
from __future__ import annotations

from typing import Any

_MUTATIONS = {"take_out", "put_in", "consume", "create_item", "delete_item"}
MULTI_THRESHOLD = 3


def plan_risk(operations: list[dict[str, Any]]) -> tuple[bool, str]:
    """返回 (是否高风险, 中文原因)。低风险时原因是空串。"""
    ops = operations or []
    mutations = [o for o in ops if o.get("intent") in _MUTATIONS]

    if any(o.get("intent") == "delete_item" for o in mutations):
        return True, "包含删除物品档案"
    if any(o.get("matched_by") == "fuzzy" for o in mutations):
        return True, "有物品是模糊匹配到已有档案的"
    if any(o.get("location_options") for o in mutations):
        return True, "有操作的目标位置不明确"
    if len(mutations) >= MULTI_THRESHOLD:
        return True, f"一次要改 {len(mutations)} 条记录"
    return False, ""
```

- [ ] **Step 5: 跑测试**

Run: `test_bot_confirm_flow -v` → PASS; `discover -s . -v` → 全绿。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/pending.py backend/app/services/risk.py \
        backend/tests/test_bot_confirm_flow.py
git commit -m "群机器人待确认方案的内存存储与风险判定"
```

---

### Task 6: 群消息渲染与公共处理流程

**Files:**
- Create: `backend/app/services/botfmt.py`
- Create: `backend/app/services/botflow.py`
- Modify: `backend/tests/test_bot_confirm_flow.py` (加 `BotFlowTest`)

**Interfaces:**
- Consumes: Task 5 的 `pending.put/take/classify_reply`、`risk.plan_risk`
- Produces: `botfmt.py::format_plan(plan: dict, reason: str) -> str` —— 纯文本, 不依赖 Markdown (飞书发的是 `msg_type="text"`, 星号会原样显示)
- Produces: `botflow.py::handle_bot_message(channel, chat_id, sender_id, text, db, cfg) -> str` —— 返回要发回群里的文本。内部完成: 查待确认 → 确认词就 apply → 否则 parse → 判风险 → 高风险存 pending 并回方案 / 低风险直接执行。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_bot_confirm_flow.py` 末尾加:

```python
class BotFlowTest(unittest.TestCase):
    """三端共用的处理流程。parse_intent 用假的, 不联网。"""

    def setUp(self):
        from app.services import pending
        pending.drop("tg", "c1", "u1")

    def _cfg(self):
        from app.config import store
        return store.get()

    def _fake_parse(self, ops):
        async def _p(text, db, cfg):
            return {"parsed": {"intent": ops[0]["intent"], "confidence": 0.9,
                               "speech": "", "operations": ops}}
        return _p

    def test_low_risk_executes_immediately(self):
        import asyncio
        from app.llm import intent as I
        from app.services import botflow, pending
        from _fixtures import make_session, seed
        db = make_session(); seed(db)
        orig = I.parse_intent
        I.parse_intent = self._fake_parse([
            {"intent": "take_out", "item_name": "螺丝刀", "quantity": 1,
             "item_id": None, "location_id": None, "location_name": None,
             "force_new": False}])
        try:
            reply = asyncio.run(botflow.handle_bot_message(
                "tg", "c1", "u1", "拿螺丝刀", db, self._cfg()))
        finally:
            I.parse_intent = orig
        self.assertIsNone(pending.peek("tg", "c1", "u1"), "低风险不该留待确认")
        self.assertNotIn("回复", reply)

    def test_high_risk_stores_plan_and_asks(self):
        import asyncio
        from app.llm import intent as I
        from app.services import botflow, pending
        from _fixtures import make_session, seed
        db = make_session(); seed(db)
        orig = I.parse_intent
        I.parse_intent = self._fake_parse([
            {"intent": "delete_item", "item_name": "螺丝刀", "quantity": 1,
             "item_id": None, "location_id": None, "location_name": None,
             "force_new": False}])
        try:
            reply = asyncio.run(botflow.handle_bot_message(
                "tg", "c1", "u1", "把螺丝刀删了", db, self._cfg()))
        finally:
            I.parse_intent = orig
        self.assertIsNotNone(pending.peek("tg", "c1", "u1"))
        self.assertIn("确认", reply)
        self.assertIn("删除", reply)

    def test_confirm_applies_and_clears(self):
        import asyncio
        from app.services import botflow, pending
        from _fixtures import make_session, seed
        db = make_session(); seed(db)
        pending.put("tg", "c1", "u1", {
            "stage": "plan",
            "operations": [{"intent": "take_out", "item_name": "螺丝刀",
                            "quantity": 1, "selected": "i:%d" % 1,
                            "options": [], "location_id": None,
                            "location_name": None, "location_options": []}]})
        reply = asyncio.run(botflow.handle_bot_message(
            "tg", "c1", "u1", "确认", db, self._cfg()))
        self.assertIsNone(pending.peek("tg", "c1", "u1"))
        self.assertTrue(reply)

    def test_cancel_clears_without_applying(self):
        import asyncio
        from app.services import botflow, pending
        from _fixtures import make_session, seed
        db = make_session(); seed(db)
        before = db.query(__import__("app.models", fromlist=["x"]).Item).count()
        pending.put("tg", "c1", "u1", {"stage": "plan", "operations": []})
        reply = asyncio.run(botflow.handle_bot_message(
            "tg", "c1", "u1", "取消", db, self._cfg()))
        self.assertIsNone(pending.peek("tg", "c1", "u1"))
        self.assertIn("取消", reply)

    def test_unrelated_text_is_new_command_not_confirmation(self):
        """待确认期间说了别的, 应当作新指令重新解析, 旧方案作废。"""
        import asyncio
        from app.llm import intent as I
        from app.services import botflow, pending
        from _fixtures import make_session, seed
        db = make_session(); seed(db)
        pending.put("tg", "c1", "u1", {"stage": "plan", "operations": []})
        orig = I.parse_intent
        I.parse_intent = self._fake_parse([
            {"intent": "find", "item_name": "卷尺", "quantity": 1,
             "item_id": None, "location_id": None, "location_name": None,
             "force_new": False}])
        try:
            asyncio.run(botflow.handle_bot_message(
                "tg", "c1", "u1", "卷尺在哪", db, self._cfg()))
        finally:
            I.parse_intent = orig
        self.assertIsNone(pending.peek("tg", "c1", "u1"), "旧方案该被作废")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `test_bot_confirm_flow -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.services.botflow'`。

- [ ] **Step 3: 写 `botfmt.py`**

```python
"""把待确认方案渲染成群消息文本。

必须是**纯文本**: 飞书走 msg_type="text", Markdown 星号会原样显示出来;
钉钉虽然支持 markdown, 但三端共用一份渲染比各写一份更不容易走样。
所以只用序号、空格和箭头, 不用任何标记语法。
"""
from __future__ import annotations

from typing import Any

_VERB = {
    "take_out": "取出", "put_in": "存入", "consume": "用掉",
    "create_item": "新建", "delete_item": "删除档案", "find": "查找",
}


def format_plan(plan: dict[str, Any], reason: str) -> str:
    ops = plan.get("operations") or []
    lines = [f"这次操作需要确认 ({reason}):"]
    for i, op in enumerate(ops, 1):
        verb = _VERB.get(op.get("intent"), op.get("intent") or "操作")
        name = op.get("item_name") or "?"
        qty = op.get("quantity") or 1
        seg = f"{i}. {verb} {name} ×{qty}"
        loc = op.get("location_path") or op.get("location_name")
        if op.get("location_options"):
            names = "/".join(o.get("name", "?") for o in op["location_options"][:3])
            seg += f" → 位置不明确 ({names})"
        elif loc:
            seg += f" → {loc}"
        if op.get("matched_by") == "fuzzy":
            seg += " (模糊匹配)"
        lines.append(seg)
    lines.append("回复 确认 执行, 回复 取消 放弃。5 分钟内有效。")
    return "\n".join(lines)
```

- [ ] **Step 4: 写 `botflow.py`**

```python
"""三个群机器人 (钉钉 / Telegram / 飞书) 共用的一次消息处理。

各端 handler 只负责: 收消息、取出 (chat_id, sender_id, text)、把返回的文本发回去。
解析、风险判定、待确认、执行, 全在这里 —— 以前这段逻辑在三个文件里各抄了一份
(包括那句把 confidence 抬到 1.0 的)。
"""
from __future__ import annotations

from typing import Any

from ..config import AppConfig
from ..llm import intent as I
from ..llm.client import LLMError
from . import botfmt, pending, risk
from .logbuffer import app_log


def _decisions_from_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """把方案里每条的 selected 变成 apply 要的 decision。"""
    out = []
    for op in plan.get("operations") or []:
        if op.get("intent") == "find":
            continue
        out.append({
            "intent": op.get("intent"),
            "option_key": op.get("selected") or "skip",
            "new_item_name": op.get("new_name_default") or op.get("item_name"),
            "location_id": op.get("location_id"),
            "location_name": op.get("location_name"),
            "quantity": op.get("quantity") or 1,
        })
    return out


async def handle_bot_message(
    channel: str, chat_id: str, sender_id: str, text: str,
    db, cfg: AppConfig,
) -> str:
    """返回要发回群里的文本。"""
    reply_kind = pending.classify_reply(text)
    if reply_kind in ("yes", "no"):
        plan = pending.take(channel, chat_id, sender_id)
        if plan is None:
            return "没有待确认的操作 (可能已经超过 5 分钟失效了)。"
        if reply_kind == "no":
            return "已取消, 什么都没改。"
        base = dict(plan)
        result = I.apply_operations(db, plan.get("_text") or "", 
                                    _decisions_from_plan(plan), base)
        db.commit()
        return result.get("speech") or "已执行。"

    # 说了别的 —— 旧方案作废, 当新指令处理。
    pending.drop(channel, chat_id, sender_id)
    try:
        out = await I.parse_intent(text, db, cfg)
    except LLMError as exc:
        app_log.warning("%s: 解析失败 %s", channel, exc)
        return f"解析失败: {exc}"
    parsed = out["parsed"]

    plan = I.execute_intent(db, text, parsed, cfg, plan_only=True)
    high, why = risk.plan_risk(plan.get("operations") or [])
    if high:
        plan["_text"] = text
        pending.put(channel, chat_id, sender_id, plan)
        return botfmt.format_plan(plan, why)

    result = I.execute_intent(db, text, parsed, cfg)
    db.commit()
    return result.get("speech") or "好的。"
```

**注意**: `execute_intent` 跑两遍 (一次 plan_only 判风险, 低风险再跑一次真执行) 是刻意的 —— plan_only 不写库, 代价只是一次内存计算, 换来"判风险"和"执行"用同一套匹配逻辑。不要为了省这一次调用把风险判定塞进 execute_intent 里。

- [ ] **Step 5: 跑测试**

Run: `test_bot_confirm_flow -v` → PASS; `discover -s . -v` → 全绿。
若 `test_confirm_applies_and_clears` 因为 `selected` 里的假 item_id 报错, 把它改成先从 db 查真实 id 再拼 `option_key` —— 测试要反映真实数据。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/botfmt.py backend/app/services/botflow.py \
        backend/tests/test_bot_confirm_flow.py
git commit -m "群机器人公共处理流程与方案渲染"
```

---

### Task 7: 三端接入 botflow

**Files:**
- Modify: `backend/app/routers/dingtalk.py:98-165`
- Modify: `backend/app/services/telegram.py:80-137`
- Modify: `backend/app/services/feishu.py:269-285, 288-372`

**Interfaces:**
- Consumes: `botflow.handle_bot_message(channel, chat_id, sender_id, text, db, cfg) -> str`

身份字段 (调研已确认, 其中两处**需要新接线**):

| 渠道 | channel | chat_id | sender_id | 现状 |
|---|---|---|---|---|
| 钉钉 | `"dingtalk"` | `payload["conversationId"]` | `payload["senderStaffId"]` | `conversationId` **当前完全没被读取**, 要新增提取 |
| Telegram | `"telegram"` | `message["chat"]["id"]` | `message["from"]["id"]` | 都已提取 |
| 飞书 | `"feishu"` | `message.chat_id` | `event.sender.sender_id.open_id` | `sender_id` 已提取但**没往下传**, `_handle_async` 要加参数 |

- [ ] **Step 1: 钉钉接入**

`routers/dingtalk.py` 的 `webhook()` 里, 把"强抬 confidence + `execute_intent` + `_format_markdown_response`"那段换成:

```python
    conversation_id = str(payload.get("conversationId") or "")
    reply_text = await botflow.handle_bot_message(
        "dingtalk", conversation_id, str(sender or ""), text, db, cfg)
    return {"msgtype": "markdown",
            "markdown": {"title": "仓储管家", "text": reply_text}}
```

保留签名校验、自回声过滤、白名单等前置逻辑不动。删掉现在那句 `parsed["confidence"] = max(...)`。`_format_markdown_response` 若不再被引用就一并删掉。

- [ ] **Step 2: Telegram 接入**

`services/telegram.py` 的 `_handle_update` 里, 把 `parse_intent` → 强抬 confidence → `execute_intent` → `_format_reply` 那段换成:

```python
        db = SessionLocal()
        try:
            reply = await botflow.handle_bot_message(
                "telegram", str(chat_id), str(user_id), text, db, cfg)
        finally:
            db.close()
        await _send_message(tg_cfg.bot_token, chat_id, reply)
```

保留 `is_bot` 过滤、白名单、剥 `/command` 等前置逻辑。`_format_reply` **要保留** —— 飞书还在用它 (`feishu.py:153`), 而且 botflow 返回的是纯文本、不再经过它。

- [ ] **Step 3: 飞书接入**

`services/feishu.py`:
- `_handle_message_event` 里已经取到了 `sender_id`(`event.sender.sender_id.open_id`), 把它传进 `_handle_async`: `_handle_async(text, chat_id, sender_id, cfg)`。
- `_handle_async` 签名加 `sender_id`, 内部把 `_run_intent` 换成 `botflow.handle_bot_message("feishu", chat_id, sender_id, text, db, cfg)`, db 用 `SessionLocal()` 并 `try/finally: db.close()`。
- 删掉 `_run_intent` 里那句强抬 confidence; `_run_intent` 若不再被引用就删掉整个函数。

- [ ] **Step 4: 确认三处强抬 confidence 都没了**

```bash
grep -n 'confidence.*1\.0' backend/app/routers/dingtalk.py backend/app/services/telegram.py backend/app/services/feishu.py
```
Expected: 零命中。

- [ ] **Step 5: 跑测试**

Run: `discover -s . -v` → 全绿 (特别注意 `test_feishu_supervisor` 的 680 行必须全绿 —— 它测的是 WS supervisor, 不该被本次改动影响; 若因为 import 变化报错, 修 import 不要改测试逻辑)。

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/dingtalk.py backend/app/services/telegram.py \
        backend/app/services/feishu.py
git commit -m "三个群机器人接入公共流程: 高风险操作先出方案等确认"
```

---

### Task 8: force_new 归属 + 量词与否定 (改 prompt, 会让 cassette 全失效)

**Files:**
- Modify: `backend/app/llm/intent.py:29-94` (`SYSTEM_PROMPT`)、`:114-193` (`TOOLS`)、`:264-307` (`_normalize_operations`)、`:204-206` (正则常量)
- Modify: `backend/eval/cases.py`
- Test: `backend/tests/test_intent_plan.py`

**Interfaces:**
- Produces: `_QUANTIFIER_MAP: dict[str, int]`、`NEGATION_HINTS: re.Pattern`

**从这个任务开始 prompt 会变, 32 个 cassette 全部失效。** T9 会一次性重录, 所以本任务里 `--replay` 会失败是**预期的**, 不要为了让它绿而回退 prompt 改动。本任务的验证靠 unittest。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_intent_plan.py` 末尾加:

```python
class ForceNewAttributionTest(unittest.TestCase):
    """句子明确是"新增"时, 多物品句里每一条都该 force_new ——
    以前的兜底只在单条操作时生效, "新增 A 和 B 到 C" 里 B 会去匹配已有档案。"""

    def _norm(self, utterance, ops):
        return I._normalize_operations({"operations": ops}, utterance)

    def test_sentence_force_new_applies_to_all_ops(self):
        ops = [
            {"intent": "put_in", "item_name": "手表", "quantity": 1,
             "location_name": "书桌1", "item_id": None, "location_id": None},
            {"intent": "put_in", "item_name": "铅笔", "quantity": 1,
             "location_name": "书桌1", "item_id": None, "location_id": None},
        ]
        out = self._norm("新增手表和铅笔到书桌1", ops)
        self.assertTrue(all(o["force_new"] for o in out), out)

    def test_llm_own_judgement_is_not_overridden(self):
        """只要 LLM 自己给任何一条设了 force_new, 就说明它判断过了, 不覆盖。"""
        ops = [
            {"intent": "put_in", "item_name": "手表", "quantity": 1,
             "force_new": True, "item_id": None, "location_id": None,
             "location_name": None},
            {"intent": "put_in", "item_name": "铅笔", "quantity": 1,
             "force_new": False, "item_id": None, "location_id": None,
             "location_name": None},
        ]
        out = self._norm("新增手表和铅笔", ops)
        self.assertTrue(out[0]["force_new"])
        self.assertFalse(out[1]["force_new"])

    def test_restock_wording_never_force_new(self):
        ops = [{"intent": "put_in", "item_name": "电池", "quantity": 2,
                "item_id": None, "location_id": None, "location_name": None}]
        out = self._norm("又买了两个电池", ops)
        self.assertFalse(out[0]["force_new"])


class QuantifierTest(unittest.TestCase):
    def test_dozen(self):
        self.assertEqual(I._quantity_from_text("拿一打铅笔", "铅笔", None), 12)

    def test_pair(self):
        self.assertEqual(I._quantity_from_text("拿两双袜子", "袜子", None), 2)

    def test_explicit_quantity_wins(self):
        """LLM 已经给了数量就用它, 量词只在没给时兜底。"""
        self.assertEqual(I._quantity_from_text("拿一打铅笔", "铅笔", 3), 3)

    def test_half_stays_one(self):
        """数量是整数列, "半瓶"记 1 —— 这是刻意的取舍, 不做分数。"""
        self.assertEqual(I._quantity_from_text("用了半瓶洗手液", "洗手液", None), 1)


class NegationTest(unittest.TestCase):
    def test_negated_item_is_dropped(self):
        ops = [
            {"intent": "take_out", "item_name": "螺丝刀", "quantity": 1,
             "item_id": None, "location_id": None, "location_name": None},
            {"intent": "take_out", "item_name": "卷尺", "quantity": 1,
             "item_id": None, "location_id": None, "location_name": None},
        ]
        parsed = {"operations": ops}
        out = I._normalize_operations(parsed, "拿卷尺, 别拿螺丝刀")
        names = [o["item_name"] for o in out]
        self.assertIn("卷尺", names)
        self.assertNotIn("螺丝刀", names)
        self.assertTrue(parsed.get("_dropped_ops"))

    def test_no_negation_keeps_everything(self):
        ops = [{"intent": "take_out", "item_name": "螺丝刀", "quantity": 1,
                "item_id": None, "location_id": None, "location_name": None}]
        out = I._normalize_operations({"operations": ops}, "拿螺丝刀")
        self.assertEqual(len(out), 1)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `test_intent_plan -v`
Expected: FAIL —— `AttributeError: module has no attribute '_quantity_from_text'`, 以及 force_new/negation 三条断言不过。

- [ ] **Step 3: 后端实现**

`intent.py` 常量区 (`RESTOCK_HINTS` 附近) 加:

```python
# 量词 → 个数。只收有确定倍数的; "些/若干/几"这类模糊词不进表 (维持 1)。
_QUANTIFIER_MAP = {"打": 12, "双": 2, "对": 2, "副": 2}
_QUANTIFIER_RE = re.compile(
    r"([一二两三四五六七八九十\d]+)\s*(" + "|".join(_QUANTIFIER_MAP) + r")")
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 否定: 只处理"整句里明确不要某个东西"这种情形。条件句 ("如果没有就…")
# 和指代 ("把它放回去") 不在范围内 —— 那需要多轮上下文, 是另一件事。
NEGATION_HINTS = re.compile(r"别|不要|不用|除了|甭")


def _cn_int(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    if len(s) == 1:
        return _CN_NUM.get(s)
    return None


def _quantity_from_text(utterance: str, item_name: str, given: int | None) -> int:
    """LLM 没给数量时, 从量词里兜一个。给了就用它的。"""
    if given:
        return given
    for m in _QUANTIFIER_RE.finditer(utterance or ""):
        n = _cn_int(m.group(1))
        if n:
            return n * _QUANTIFIER_MAP[m.group(2)]
    return 1


def _is_negated(utterance: str, item_name: str) -> bool:
    """物品名前面 6 个字以内出现否定词 —— "拿卷尺, 别拿螺丝刀" 里只有螺丝刀被否定。"""
    if not item_name or not NEGATION_HINTS.search(utterance or ""):
        return False
    idx = utterance.find(item_name)
    if idx < 0:
        return False
    return bool(NEGATION_HINTS.search(utterance[max(0, idx - 6):idx]))
```

`_normalize_operations` 里:
- 数量: `"quantity": max(1, _coerce_int(raw.get("quantity")) or 0) or _quantity_from_text(utterance, raw.get("item_name") or "", _coerce_int(raw.get("quantity")))` —— 照现有写法改, 保证"LLM 给了就用 LLM 的"。
- 否定: 组装完 op 之后, `if _is_negated(utterance, op["item_name"]): _dropped_ops.append(op); continue`。
- force_new 兜底: 把现有的 `and len(parsed.get("operations") or []) == 1` 条件去掉, 换成"本轮所有 op 都没设 force_new"这个前提。具体做法: 先跑完一遍收集 ops, 再在末尾统一判断:

```python
    # 句子明确是新增措辞, 而 LLM 一条都没标 force_new —— 说明它没做这个判断,
    # 那就整句按新增处理。只要它标了任何一条, 就尊重它的判断, 一个字都不改。
    if sentence_force_new and ops and not any(o.get("force_new") for o in ops):
        for o in ops:
            if o["intent"] == "put_in":
                o["intent"] = "create_item"
                o["force_new"] = True
```

- [ ] **Step 4: 改 prompt**

`SYSTEM_PROMPT` 的 force_new 段落补一条多物品示例, 明确"每条操作各自判断 force_new"; 新增一条否定规则 ("用户明确说不要/别拿的物品, 不要出现在 operations 里") 和一条量词规则 ("一打=12, 一双/一对=2; '半瓶'按 1 记, 数量只支持整数")。
`TOOLS` 里 `force_new` 的 description 补 "每条操作独立判断, 不要因为句子里出现一次'新增'就给所有条目都设 true"。

- [ ] **Step 5: 跑 unittest**

Run: `test_intent_plan -v` → PASS; `discover -s . -v` → 全绿。
eval `--replay` **会报 cassette 缺失, 这是预期的** —— T9 重录。

- [ ] **Step 6: 加 eval case**

`eval/cases.py` 加:

```python
    # ---- 多物品句里的 force_new 归属 ----
    dict(id="fnmulti-two", cat="forcenew_multi", text="新增手表和铅笔到书桌1",
         ops=[dict(intent="create_item", new="手表", to="书桌1"),
              dict(intent="create_item", new="铅笔", to="书桌1")]),
    dict(id="fnmulti-mixed", cat="forcenew_multi",
         text="新增一个订书机到书桌1, 再把卷尺也放进去",
         ops=[dict(intent="create_item", new="订书机", to="书桌1"),
              dict(intent="put_in", item="卷尺", to="书桌1")]),

    # ---- 量词 ----
    dict(id="qty-dozen", cat="quantifier", text="拿一打电池",
         ops=[dict(intent="take_out", item="电池", qty=12)]),
    dict(id="qty-pair", cat="quantifier", text="用了两双手套",
         ops=[dict(intent="consume", skip=True)]),
    dict(id="qty-half", cat="quantifier", text="用了半瓶洗手液",
         ops=[dict(intent="consume", item="洗手液", qty=1)]),

    # ---- 否定 ----
    dict(id="neg-single", cat="negation", text="拿卷尺, 别拿螺丝刀",
         ops=[dict(intent="take_out", item="卷尺", qty=1)]),
    dict(id="neg-except", cat="negation", text="把书桌1 上的东西都拿走, 除了电池",
         ops=[dict(intent="take_out", item="充电宝", qty=1),
              dict(intent="take_out", item="充电器", qty=1)]),

    # ---- 位置歧义 ----
    dict(id="locambig-exact", cat="locambig", text="把卷尺放进书桌1",
         ops=[dict(intent="put_in", item="卷尺", to="书桌1")]),
```

`neg-except` 是最难的一条, 允许它不过 —— 它的价值是量出"复杂否定还差多远", 在报告里如实记录。

- [ ] **Step 7: Commit**

```bash
git add backend/app/llm/intent.py backend/eval/cases.py backend/tests/test_intent_plan.py
git commit -m "多物品句 force_new 归属、量词与否定句处理"
```

---

### Task 9: 重录 cassette 并测量

**Files:**
- Modify: `backend/eval/cassettes/` (全部重录)

**这个任务会真的调 LLM**, 走用户自建 cc-trans (`claude-opus-4-8`), 有成本。

- [ ] **Step 1: 备份旧 cassette**

```bash
cd /home/xyz/Projects/priv/repo_git
cp -r backend/eval/cassettes /tmp/cassettes-before-B
ls /tmp/cassettes-before-B | wc -l    # 应该是 32 + T1/T2 新录的
```

- [ ] **Step 2: 删掉旧 cassette 并重录**

prompt 改了之后旧 cassette 的 key 全部对不上, 留着只是垃圾:

```bash
rm -f backend/eval/cassettes/*.json
mkdir -p /tmp/evalcfg && cp data/config.json /tmp/evalcfg/config.json
docker run --rm -v "$PWD/backend:/src" -v /tmp/evalcfg:/cfg -w /src \
  -e CONFIG_PATH=/cfg/config.json repo_git-app python -m eval.run_eval --verbose
```

(不带 `--replay`、不带 `--network none`, 会真的调 LLM 并录下来。)

- [ ] **Step 3: 用 --replay 复现**

```bash
docker run --rm -v "$PWD/backend:/src" -v /tmp/evalcfg:/cfg -w /src --network none \
  -e CONFIG_PATH=/cfg/config.json repo_git-app python -m eval.run_eval --replay --verbose \
  | tee .superpowers/sdd/2026-09-03-perf-power-cleanup/eval-after-B.txt
```

- [ ] **Step 4: 与基线逐分类对比**

基线: `.superpowers/sdd/2026-09-03-perf-power-cleanup/eval-baseline-before-B.txt` (32 条, 全分类 100%)。

写一份对比进报告, 逐分类给出改前/改后。要求:
- **原有 9 个分类 (single/multi/mixed/confusable/force_new/restock/ambiguous/absent/readonly) 一条都不许回退。** 有回退就是 prompt 改动引入的副作用, 必须查清 —— 是新规则和旧规则打架, 还是模型这次发挥不同。
- 新分类 (delete/apply/forcenew_multi/quantifier/negation/locambig) 给出实际数字。`neg-except` 允许不过。

- [ ] **Step 5: 若有回退, 修 prompt 再重录**

只改 prompt 措辞, 不要改 case 去迁就结果。改完重复 Step 2-4。

- [ ] **Step 6: Commit**

```bash
git add backend/eval/cassettes/
git commit -m "prompt 改动后重录评测 cassette"
```

---

### Task 10: 全量回归与部署验证

**Files:** 无代码改动。

- [ ] **Step 1: 记录基线**

```bash
cd /home/xyz/Projects/priv/repo_git
openssl x509 -in data/certs/ca.crt -noout -fingerprint -sha256
python3 -c "import sqlite3;c=sqlite3.connect('data/storage.db');print([c.execute('select count(*) from '+t).fetchone()[0] for t in ('items','locations','transactions','audit_log')])"
```
预期 CA 指纹 `03:74:A6:F3:...:94`, 条数 `[1, 14, 3, 39]`。

- [ ] **Step 2: 重建前闸门**

全量后端测试 + eval `--replay` 全绿。不绿就停下报告, 不要重建。

- [ ] **Step 3: 重建**

```bash
./start.sh --whisper
```
等 `storage-app` healthy (`curl -sk https://127.0.0.1:8443/api/health`)。

- [ ] **Step 4: 六项检查**

```bash
openssl x509 -in data/certs/ca.crt -noout -fingerprint -sha256           # 1. 与 Step 1 一致
python3 -c "import sqlite3;c=sqlite3.connect('data/storage.db');print([c.execute('select count(*) from '+t).fetchone()[0] for t in ('items','locations','transactions','audit_log')])"  # 2. 一致
curl -sk https://127.0.0.1:8443/api/health                                # 3. {"ok":true}
curl -sk "https://127.0.0.1:8443/api/transactions/pending-returns" -o /dev/null -w "%{http_code}\n"  # 4. 200
docker logs storage-app --since 3m 2>&1 | grep -icE 'error|traceback'     # 5. 0
docker exec storage-app python -c "from app.services import botflow, pending, risk; print('bot modules ok')"  # 6.
```

- [ ] **Step 5: 群机器人冒烟 (只在配置了凭证时做)**

检查 `data/config.json` 里 telegram/feishu/dingtalk 是否 enabled。**都没启用就跳过并在报告里说明** —— 不要为了测试去启用它们或填凭证。若飞书已启用, 在群里发一句"把卷尺和螺丝刀的记录都删了"应当收到方案而非直接执行; 回"取消"应当收到"已取消"。

- [ ] **Step 6: 汇报**

报告写: 测试数变化、eval 逐分类改前改后对比、六项检查结果、`git log --oneline` 本子项目的 commit 列表。**不 push。**

---

## Self-Review 记录

**Spec 覆盖**: B0 → T1+T2; B1 → T3; B2 → T4; B3+B4 → T8; B5 → T5+T6+T7; 测试与部署 → T9+T10。

**写计划时发现并已修正的两处 spec 偏差**:
1. spec 说 `_apply_stock_op` 在三处"手抄三份"应收敛成一份 —— 实际读代码发现三者**不等价** (`items.py` 多一个 `adjust` 绝对赋值分支且用 `payload.model_dump()` 建 tx)。T4 改为只抽"数量怎么变"这一段成 `apply_quantity_delta`, 不动 tx 构造。
2. spec 没提 cassette 失效问题。实测 `run_eval._key()` 把 `SYSTEM_PROMPT` 哈希进文件名, 所以任何 prompt 改动都让 32 个 cassette 全失效。计划据此重排任务顺序 (prompt 改动集中在 T8, T9 统一重录), 并在 Global Constraints 里写明。

**类型一致性**: `_resolve_location` 返回 `tuple[int|None, list[Location]]` 在 T3 的 5 个调用点一致; `plan_risk(operations) -> tuple[bool,str]` 在 T5 定义、T6 使用一致; `handle_bot_message(channel, chat_id, sender_id, text, db, cfg) -> str` 在 T6 定义、T7 三处调用一致; `apply_quantity_delta(item, action, qty, loc_id)` 在 T4 定义与三处调用一致; `format_plan(plan, reason)` 在 T6 内部一致。
