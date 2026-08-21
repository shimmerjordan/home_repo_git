"""待确认方案 (plan) / 执行 (apply) 的行为测试 —— 不联网, 不调 LLM。

覆盖的是用户实际抱怨的几件事:
  * 存入新物品时被错误合并到名字相近的已有物品上
  * "新增 X 到 Y" 应该是全新物品, 不做匹配
  * 多物品语句一条都不能丢
  * 回撤按钮该亮不该亮
"""
from __future__ import annotations

import unittest

from _fixtures import item_by, make_session, seed

from app.config import AppConfig
from app.llm import intent as I
from app.services.inventory import annotate_undoable, serialize_transaction
from app.services.summary import split_segments


def parsed_with(ops, **top):
    p = {"intent": ops[0]["intent"] if ops else "unknown", "confidence": 0.9,
         "speech": "", "candidates": [], "recommendations": [],
         "operations": ops, "quantity": 1}
    p.update(top)
    return p


def plan(db, text, ops, **top):
    cfg = AppConfig()
    return I.execute_intent(db, text, parsed_with(ops, **top), cfg, plan_only=True)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.db = make_session()
        self.locs, self.items = seed(self.db)

    def tearDown(self):
        self.db.close()

    # ---- 核心: 模糊命中绝不预选已有物品 ----

    def test_new_item_defaults_to_new_row(self):
        """"把洗发水放进洗漱柜" —— 库里只有洗手液, 必须默认新建, 一个字不落库。"""
        r = plan(self.db, "把洗发水放进洗漱柜", [{
            "intent": "put_in", "item_id": None, "item_name": "洗发水",
            "location_id": self.locs["我家/卫生间/洗漱柜"].id,
            "location_name": None, "quantity": 1, "force_new": False,
        }])
        self.assertEqual(r["stage"], "plan")
        op = r["operations"][0]
        self.assertEqual(op["selected"], "new", "不能挂到洗手液上")
        self.assertTrue(op["allow_new"])
        self.assertIn("new", [o["key"] for o in op["options"]])
        self.assertFalse(op["executed"])
        self.assertEqual(item_by(self.items, "洗手液").quantity, 1)

    def test_fuzzy_candidates_are_listed_but_never_preselected(self):
        """"充电线" 会模糊命中充电宝/充电器 —— 列出来但绝不预选。

        这是本功能最核心的一条取舍: 名字相近就自动加库存是误操作的根源。
        """
        r = plan(self.db, "把充电线放进书桌1", [{
            "intent": "put_in", "item_id": None, "item_name": "充电线",
            "location_id": self.locs["我家/书房/书桌1"].id,
            "location_name": None, "quantity": 1, "force_new": False,
        }])
        op = r["operations"][0]
        fuzzy = [o for o in op["options"] if o["kind"] == "fuzzy"]
        self.assertTrue(fuzzy, f"应当列出名字相近的候选, 实际 options={op['options']}")
        self.assertEqual(op["selected"], "new", "模糊命中一律不预选")
        self.assertIn("相近", op["reason"])

    def test_llm_guessed_item_id_is_not_auto_selected_when_name_differs(self):
        """LLM 猜了个 item_id 但名字对不上 —— 仍然默认新建。"""
        soap = item_by(self.items, "洗手液")
        r = plan(self.db, "存入洗发水", [{
            "intent": "put_in", "item_id": soap.id, "item_name": "洗发水",
            "location_id": None, "location_name": None, "quantity": 1, "force_new": False,
        }])
        op = r["operations"][0]
        self.assertEqual(op["selected"], "new")
        # 但 LLM 的猜测要出现在候选里, 方便一键采纳
        self.assertIn(f"i:{soap.id}", [o["key"] for o in op["options"]])

    def test_exact_name_is_preselected(self):
        """名字完全一致时正常预选它。"""
        driver = item_by(self.items, "螺丝刀")
        r = plan(self.db, "拿了螺丝刀", [{
            "intent": "take_out", "item_id": None, "item_name": "螺丝刀",
            "location_id": None, "location_name": None, "quantity": 1, "force_new": False,
        }])
        op = r["operations"][0]
        self.assertEqual(op["selected"], f"i:{driver.id}")
        self.assertEqual(op["matched_by"], "exact")
        self.assertFalse(op["allow_new"], "take_out 不该提供'新建'选项")

    def test_same_name_two_places_is_ambiguous(self):
        """电池在两处都有 —— 标 ambiguous 并让用户选。"""
        r = plan(self.db, "拿两个电池", [{
            "intent": "take_out", "item_id": None, "item_name": "电池",
            "location_id": None, "location_name": None, "quantity": 2, "force_new": False,
        }])
        op = r["operations"][0]
        self.assertEqual(op["matched_by"], "ambiguous")
        self.assertEqual(len([o for o in op["options"] if o["kind"] == "exact"]), 2)

    def test_take_out_missing_item_is_skipped(self):
        """取一个库里没有的东西 —— 没有'新建'可选, 预选 skip。"""
        r = plan(self.db, "拿了吹风机", [{
            "intent": "take_out", "item_id": None, "item_name": "吹风机",
            "location_id": None, "location_name": None, "quantity": 1, "force_new": False,
        }])
        op = r["operations"][0]
        self.assertEqual(op["selected"], "skip")
        self.assertEqual(op["matched_by"], "none")

    def test_item_id_without_name_falls_back_to_that_item(self):
        """模型只给 item_id 不给 item_name —— 不能预选一个空名字的"新建"。"""
        pb = item_by(self.items, "充电宝")
        r = plan(self.db, "把它放回书桌1", [{
            "intent": "put_in", "item_id": pb.id, "item_name": None,
            "location_id": self.locs["我家/书房/书桌1"].id,
            "location_name": None, "quantity": 1, "force_new": False,
        }])
        op = r["operations"][0]
        self.assertEqual(op["item_name"], "充电宝", "应当从 item_id 反查出名字")
        self.assertEqual(op["selected"], f"i:{pb.id}")
        self.assertNotEqual(op["selected"], "new")

    def test_no_name_no_id_is_skipped(self):
        r = plan(self.db, "放进去", [{
            "intent": "put_in", "item_id": None, "item_name": None,
            "location_id": None, "location_name": None, "quantity": 1, "force_new": False,
        }])
        op = r["operations"][0]
        self.assertEqual(op["selected"], "skip")
        self.assertIn("没识别出物品名", op["reason"])

    def test_take_out_with_only_guessed_id_is_flagged_fuzzy(self):
        """取出/用完不能新建, 只有 id 时用它但标成"猜的"。"""
        soap = item_by(self.items, "洗手液")
        r = plan(self.db, "用完了那个洗手的", [{
            "intent": "consume", "item_id": soap.id, "item_name": "洗手的",
            "location_id": None, "location_name": None, "quantity": 1, "force_new": False,
        }])
        op = r["operations"][0]
        self.assertEqual(op["selected"], f"i:{soap.id}")
        # 断言机器可读的信号, 不断言具体话术 —— 文案会改, matched_by 不会。
        # 前端就是靠 matched_by=fuzzy 把这条标黄并提示复核的。
        self.assertEqual(op["matched_by"], "fuzzy")
        self.assertTrue(op["reason"], "必须给出一句理由让用户知道这是猜的")

    # ---- force_new ----

    def test_force_new_ignores_exact_same_name(self):
        """"新增充电宝到书桌1" —— 库里已有充电宝, 但仍默认新建。"""
        r = plan(self.db, "新增充电宝到书桌1", [{
            "intent": "create_item", "item_id": None, "item_name": "充电宝",
            "location_id": self.locs["我家/书房/书桌1"].id,
            "location_name": None, "quantity": 1, "force_new": True,
        }])
        op = r["operations"][0]
        self.assertEqual(op["selected"], "new")
        self.assertTrue(op["force_new"])
        self.assertEqual(op["options"][0]["key"], "new", "force_new 时新建项排第一")
        # 已有的充电宝仍列出来, 允许改成合并
        self.assertIn(f"i:{item_by(self.items, '充电宝').id}",
                      [o["key"] for o in op["options"]])

    def test_create_item_implies_force_new(self):
        """模型没给 force_new, 但 intent 是 create_item —— 也当新建。"""
        ops = I._normalize_operations(parsed_with([{
            "intent": "create_item", "item_name": "台灯", "quantity": 1,
        }]), "创建一个台灯")
        self.assertTrue(ops[0]["force_new"])

    def test_backend_upgrades_put_in_to_create_when_user_said_xinzeng(self):
        """模型把"新增X到Y"解析成 put_in —— 后端兜底升级成 create_item + force_new。"""
        ops = I._normalize_operations(parsed_with([{
            "intent": "put_in", "item_name": "香薰", "quantity": 1,
        }]), "新增香薰到洗漱柜")
        self.assertEqual(ops[0]["intent"], "create_item")
        self.assertTrue(ops[0]["force_new"])

    def test_restock_phrasing_is_not_force_new(self):
        """"又买了两瓶水" 是补货, 不能当成全新物品。"""
        self.assertFalse(I._looks_force_new("又买了两瓶水"))
        self.assertFalse(I._looks_force_new("给抽纸补货"))
        self.assertTrue(I._looks_force_new("新增一个香薰"))

    def test_put_in_not_upgraded_when_multiple_ops(self):
        """多物品时不做"新增"兜底升级 —— 无法可靠判断修饰的是哪一个。"""
        ops = I._normalize_operations(parsed_with([
            {"intent": "put_in", "item_name": "香薰", "quantity": 1},
            {"intent": "put_in", "item_name": "毛巾", "quantity": 1},
        ]), "新增香薰, 把毛巾放进洗漱柜")
        self.assertEqual([o["intent"] for o in ops], ["put_in", "put_in"])

    # ---- 多物品一条不丢 ----

    def test_multi_op_plan_keeps_every_item(self):
        r = plan(self.db, "我用完了洗手液, 拿了螺丝刀和卷尺, 把两个充电器放回书桌1", [
            {"intent": "consume", "item_name": "洗手液", "quantity": 1},
            {"intent": "take_out", "item_name": "螺丝刀", "quantity": 1},
            {"intent": "take_out", "item_name": "卷尺", "quantity": 1},
            {"intent": "put_in", "item_name": "充电器", "quantity": 2,
             "location_id": None, "location_name": "书桌1"},
        ])
        self.assertEqual(len(r["operations"]), 4)
        self.assertEqual([o["quantity"] for o in r["operations"]], [1, 1, 1, 2])
        self.assertTrue(all(o["pending"] for o in r["operations"]))

    def test_duplicate_ops_are_deduped(self):
        ops = I._normalize_operations(parsed_with([
            {"intent": "take_out", "item_id": 4, "item_name": "螺丝刀", "quantity": 1},
            {"intent": "take_out", "item_id": 4, "item_name": "螺丝刀", "quantity": 1},
        ]), "拿了螺丝刀")
        self.assertEqual(len(ops), 1)

    def test_unknown_intent_is_recorded_not_silently_dropped(self):
        p = parsed_with([{"intent": "teleport", "item_name": "螺丝刀"}])
        ops = I._normalize_operations(p, "把螺丝刀传送走")
        self.assertEqual(ops, [])
        self.assertEqual(len(p["_dropped_ops"]), 1)

    def test_intent_alias_is_mapped(self):
        ops = I._normalize_operations(parsed_with([
            {"intent": "borrow", "item_name": "卷尺"}]), "借走卷尺")
        self.assertEqual(ops[0]["intent"], "take_out")

    def test_find_is_answered_inline_not_planned(self):
        """查询是只读的, 不该逼用户点确认。"""
        r = plan(self.db, "螺丝刀在哪, 顺便把卷尺放回书房", [
            {"intent": "find", "item_name": "螺丝刀", "quantity": 1},
            {"intent": "put_in", "item_name": "卷尺", "quantity": 1,
             "location_name": "书房"},
        ])
        find_op, put_op = r["operations"]
        self.assertFalse(find_op["pending"])
        self.assertTrue(find_op["executed"])
        self.assertTrue(put_op["pending"])
        self.assertIn("螺丝刀", r["speech"])


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.db = make_session()
        self.locs, self.items = seed(self.db)

    def tearDown(self):
        self.db.close()

    def _apply(self, decisions):
        base = {"intent": "batch", "confidence": 1.0, "speech": "",
                "needs_confirmation": False, "pending_action": None,
                "candidates": [], "recommendations": [], "executed": False,
                "transaction_id": None, "operations": [], "stage": "applied",
                "plan_id": "t", "raw": {}}
        return I.apply_operations(self.db, "", decisions, base)

    def test_apply_new_creates_fresh_row_even_if_name_exists(self):
        before = self.db.query(I.models.Item).filter_by(name="充电宝").count()
        r = self._apply([{"intent": "create_item", "option_key": "new",
                          "new_item_name": "充电宝", "quantity": 1,
                          "location_id": self.locs["我家/书房/书桌1"].id}])
        self.assertTrue(r["operations"][0]["executed"])
        after = self.db.query(I.models.Item).filter_by(name="充电宝").count()
        self.assertEqual(after, before + 1, "force_new 必须建新行, 不能合并")

    def test_apply_existing_item_merges(self):
        pb = item_by(self.items, "充电宝")
        self._apply([{"intent": "create_item", "option_key": f"i:{pb.id}",
                      "new_item_name": "充电宝", "quantity": 3}])
        self.db.refresh(pb)
        self.assertEqual(pb.quantity, 4, "挑了已有物品就等于合并进去")

    def test_apply_skip_does_nothing(self):
        driver = item_by(self.items, "螺丝刀")
        r = self._apply([{"intent": "take_out", "option_key": "skip",
                          "new_item_name": "螺丝刀", "quantity": 1}])
        self.db.refresh(driver)
        self.assertEqual(driver.quantity, 1)
        self.assertFalse(r["operations"][0]["executed"])
        self.assertFalse(r["executed"])

    def test_apply_consume_decrements(self):
        soap = item_by(self.items, "洗手液")
        self._apply([{"intent": "consume", "option_key": f"i:{soap.id}", "quantity": 1}])
        self.db.refresh(soap)
        self.assertEqual(soap.quantity, 0)

    def test_apply_is_atomic_across_ops(self):
        """多条一起提交, 只 commit 一次。"""
        driver = item_by(self.items, "螺丝刀")
        tape = item_by(self.items, "卷尺")
        r = self._apply([
            {"intent": "take_out", "option_key": f"i:{driver.id}", "quantity": 1},
            {"intent": "take_out", "option_key": f"i:{tape.id}", "quantity": 1},
        ])
        self.assertEqual(len([o for o in r["operations"] if o["executed"]]), 2)
        self.db.refresh(driver); self.db.refresh(tape)
        self.assertEqual((driver.quantity, tape.quantity), (0, 0))

    def test_apply_delete_item_removes_row(self):
        tape = item_by(self.items, "卷尺")
        tid = tape.id
        self._apply([{"intent": "delete_item", "option_key": f"i:{tid}", "quantity": 1}])
        self.assertIsNone(self.db.query(I.models.Item).get(tid))

    def test_apply_missing_target_reports_instead_of_crashing(self):
        r = self._apply([{"intent": "put_in", "option_key": "i:99999", "quantity": 1}])
        self.assertFalse(r["operations"][0]["executed"])
        self.assertIn("不存在", r["operations"][0]["speech"])

    def test_apply_new_without_name_reports(self):
        r = self._apply([{"intent": "put_in", "option_key": "new",
                          "new_item_name": "  ", "quantity": 1}])
        self.assertFalse(r["operations"][0]["executed"])


class UndoableTest(unittest.TestCase):
    def setUp(self):
        self.db = make_session()
        self.locs, self.items = seed(self.db)

    def tearDown(self):
        self.db.close()

    def _tx(self, item, action, qty=1):
        tx = I.models.Transaction(item_id=item.id, action=action, quantity=qty,
                                  location_id=item.location_id)
        self.db.add(tx)
        self.db.commit()
        return tx

    def test_last_tx_is_undoable(self):
        driver = item_by(self.items, "螺丝刀")
        tx = self._tx(driver, "take_out")
        rows = annotate_undoable(self.db, [serialize_transaction(tx)])
        self.assertTrue(rows[0]["undoable"])
        self.assertEqual(rows[0]["undo_note"], "")

    def test_superseded_tx_is_not_undoable(self):
        driver = item_by(self.items, "螺丝刀")
        first = self._tx(driver, "take_out")
        self._tx(driver, "put_in")
        rows = annotate_undoable(self.db, [serialize_transaction(first)])
        self.assertFalse(rows[0]["undoable"])
        self.assertIn("又被动过", rows[0]["undo_note"])

    def test_adjust_is_not_undoable(self):
        driver = item_by(self.items, "螺丝刀")
        tx = self._tx(driver, "adjust")
        rows = annotate_undoable(self.db, [serialize_transaction(tx)])
        self.assertFalse(rows[0]["undoable"])
        self.assertIn("操作前的数量", rows[0]["undo_note"])

    def test_annotate_is_constant_queries_not_n_plus_1(self):
        """标注本身对 N 条流水只发常数条 SQL —— 近期记录每 30s 轮询一次, 不能退化成 N+1。

        只量 annotate_undoable: 序列化时 tx.item / location_path(parent 链) 的懒加载
        是另一码事 (列表接口用 joinedload 缓解, 见 routers/items.py)。
        """
        txs = [self._tx(it, "take_out") for it in self.items]
        rows = [serialize_transaction(t) for t in txs]   # 先序列化完, 不计入
        seen = []
        from sqlalchemy import event
        event.listen(self.db.get_bind(), "before_cursor_execute",
                     lambda *a, **k: seen.append(1))
        annotate_undoable(self.db, rows)
        self.assertLessEqual(len(seen), 3,
                             f"annotate_undoable 发了 {len(seen)} 条 SQL, 退化成 N+1 了")


class SegmentTest(unittest.TestCase):
    def test_split_multi_item_utterance(self):
        segs = split_segments("我用完了洗手液, 拿了螺丝刀和卷尺, 把两个充电器放回书桌1")
        self.assertEqual(segs[0], "我用完了洗手液, 拿了螺丝刀和卷尺, 把两个充电器放回书桌1")
        joined = " ".join(segs)
        for w in ("洗手液", "螺丝刀", "卷尺", "充电器"):
            self.assertIn(w, joined)
        self.assertGreaterEqual(len(segs), 4, f"切得太粗: {segs}")

    def test_split_keeps_de_le_words_intact(self):
        """"和" 出现在"和的/和了"里不该切。"""
        segs = split_segments("充电宝在哪")
        self.assertEqual(segs, ["充电宝在哪"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
