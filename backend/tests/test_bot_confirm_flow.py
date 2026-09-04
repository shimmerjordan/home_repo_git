"""群机器人的"高风险先确认"流程。这里只测存储与风险判定, 三端接线在另一个测试里。"""
from __future__ import annotations

import time
import unittest

from _fixtures import make_session  # noqa: F401  (sys.path 注入)
from app.services import pending, risk


class PendingStoreTest(unittest.TestCase):
    def setUp(self):
        # 清空而不是 drop 单个键: test_capacity_cap 会塞 220 条进这个模块级字典,
        # 只 drop 一个键会把残留带进后面的用例, 变成执行顺序依赖。
        pending._STORE.clear()

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

    def test_case_variants(self):
        for t in ["Yes", "YES", "Ok", "OK", "y", "Y"]:
            self.assertEqual(pending.classify_reply(t), "yes", t)
        for t in ["No", "NO", "n", "N"]:
            self.assertEqual(pending.classify_reply(t), "no", t)


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

    def test_same_name_ambiguity_is_high_risk(self):
        """库里多处同名时 _plan_one 会预选第一条 —— 和模糊命中一样危险。"""
        high, why = risk.plan_risk([self._op(matched_by="ambiguous")])
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
        from app import models
        from app.services import botflow, pending
        from _fixtures import make_session, seed, item_by
        db = make_session(); items = seed(db)[1]
        # 用真实 id 拼 option_key —— 硬编码 1 会随种子数据变化而失效。
        luosidao = item_by(items, "螺丝刀")
        before_qty = luosidao.quantity
        pending.put("tg", "c1", "u1", {
            "stage": "plan",
            "operations": [{"intent": "take_out", "item_name": "螺丝刀",
                            "quantity": 1, "selected": f"i:{luosidao.id}",
                            "options": [], "location_id": None,
                            "location_name": None, "location_options": []}]})
        reply = asyncio.run(botflow.handle_bot_message(
            "tg", "c1", "u1", "确认", db, self._cfg()))
        self.assertIsNone(pending.peek("tg", "c1", "u1"))
        self.assertTrue(reply)
        # 确认之后必须真的落库, 不能只是把 pending 清掉。
        db.refresh(luosidao)
        self.assertEqual(before_qty - luosidao.quantity, 1)

    def test_cancel_clears_without_applying(self):
        import asyncio
        from app import models
        from app.services import botflow, pending
        from _fixtures import make_session, seed, item_by
        db = make_session(); items = seed(db)[1]
        luosidao = item_by(items, "螺丝刀")
        before_qty = luosidao.quantity
        before_items = db.query(models.Item).count()
        pending.put("tg", "c1", "u1", {
            "stage": "plan",
            "operations": [{"intent": "take_out", "item_name": "螺丝刀",
                            "quantity": 1, "selected": f"i:{luosidao.id}",
                            "options": [], "location_id": None,
                            "location_name": None, "location_options": []}]})
        reply = asyncio.run(botflow.handle_bot_message(
            "tg", "c1", "u1", "取消", db, self._cfg()))
        self.assertIsNone(pending.peek("tg", "c1", "u1"))
        self.assertIn("取消", reply)
        # 取消的全部意义就在这两条断言: 数据一个字节都不能动。
        db.refresh(luosidao)
        self.assertEqual(luosidao.quantity, before_qty)
        self.assertEqual(db.query(models.Item).count(), before_items)

    def test_location_ambiguity_asks_instead_of_pending(self):
        """位置歧义不走确认流程 —— 确认是二选一, 解不了三选一的位置。
        存成待确认的话, 用户回"确认"之后 apply 会拿同一个名字重新解析、
        得到同样的歧义, 回一句"没执行" —— 确认了却什么也没发生。"""
        import asyncio
        from app import models
        from app.llm import intent as I
        from app.services import botflow, pending
        from _fixtures import make_session, seed
        db = make_session(); by_path = seed(db)[0]
        # 造出"书桌1"/"书桌10"两个子串候选
        db.add(models.Location(name="书桌10", kind="box",
                               parent_id=by_path["我家/书房"].id))
        db.flush()
        orig = I.parse_intent
        I.parse_intent = self._fake_parse([
            {"intent": "put_in", "item_name": "卷尺", "quantity": 1,
             "item_id": None, "location_id": None, "location_name": "书桌",
             "force_new": False}])
        try:
            reply = asyncio.run(botflow.handle_bot_message(
                "tg", "c1", "u1", "把卷尺放进书桌", db, self._cfg()))
        finally:
            I.parse_intent = orig
        self.assertIsNone(pending.peek("tg", "c1", "u1"),
                          "位置歧义不该存成待确认 —— 确认解不了它")
        self.assertIn("书桌1", reply)
        self.assertIn("书桌10", reply)
        self.assertNotIn("回复 确认", reply)

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


if __name__ == "__main__":
    unittest.main()
