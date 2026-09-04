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


class FormatPlanTest(unittest.TestCase):
    def test_format_plan_keeps_find_answers(self):
        """一句话里混了查询和高风险操作时, 查询答案不能被丢掉 ——
        find 是只读的, 它算出的答案该直接给出来, 而不是混在待确认清单里。"""
        from app.services import botfmt
        plan = {"operations": [
            {"intent": "find", "item_name": "苹果", "quantity": 1,
             "speech": "苹果在我家/书房(×3)", "location_options": []},
            {"intent": "delete_item", "item_name": "螺丝刀", "quantity": 1,
             "speech": "", "location_options": [], "matched_by": "exact"},
        ]}
        out = botfmt.format_plan(plan, "包含删除物品档案")
        self.assertIn("苹果在我家/书房(×3)", out)
        # 待确认清单里只应有那条 delete, 且编号从 1 开始
        self.assertIn("1. 删除档案 螺丝刀 ×1", out)
        self.assertNotIn("查找 苹果", out)


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

    def _fake_parse_conf(self, ops, confidence):
        async def _p(text, db, cfg):
            return {"parsed": {"intent": ops[0]["intent"], "confidence": confidence,
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

    def test_low_confidence_becomes_pending_not_dead_end(self):
        """置信度低于阈值时 execute_intent 自己会拦下且不写库, 只回"我不太确定"。
        botflow 必须把它转成待确认 —— 否则用户回"确认"时 pending 是空的,
        只能得到"没有待确认的操作", 问了等于没问且再也触发不了。"""
        import asyncio
        from app.llm import intent as I
        from app.services import botflow, pending
        from _fixtures import make_session, seed, item_by
        db = make_session(); items = seed(db)[1]
        luosidao = item_by(items, "螺丝刀")
        before_qty = luosidao.quantity
        orig = I.parse_intent
        # 精确命中 (plan_risk 判低风险) + 低置信度 (execute_intent 的门槛会拦)
        I.parse_intent = self._fake_parse_conf([
            {"intent": "take_out", "item_name": "螺丝刀", "quantity": 1,
             "item_id": None, "location_id": None, "location_name": None,
             "force_new": False}], 0.2)
        try:
            reply = asyncio.run(botflow.handle_bot_message(
                "tg", "c1", "u1", "拿个螺丝刀", db, self._cfg()))
        finally:
            I.parse_intent = orig
        # 没写库
        db.refresh(luosidao)
        self.assertEqual(luosidao.quantity, before_qty)
        # 但存了待确认, 且回复是可确认的方案
        self.assertIsNotNone(pending.peek("tg", "c1", "u1"),
                             "低置信度必须转成待确认, 不能变成死胡同")
        self.assertIn("回复 确认", reply)
        # 接着确认一次, 必须真的执行
        reply2 = asyncio.run(botflow.handle_bot_message(
            "tg", "c1", "u1", "确认", db, self._cfg()))
        self.assertIsNone(pending.peek("tg", "c1", "u1"))
        db.refresh(luosidao)
        self.assertEqual(before_qty - luosidao.quantity, 1,
                         "确认之后必须真的扣库存")

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

    def test_find_reply_lists_candidates_not_just_speech(self):
        """只回 speech 会丢候选/推荐清单 —— assist 问药时整张清单都没了。
        这条钉住 botflow 走的是 botfmt.format_result 而不是 result["speech"]。"""
        from app.services import botfmt
        result = {
            "speech": "找到 2 个", "executed": False,
            "candidates": [
                {"item_id": 1, "item_name": "布洛芬", "location_path": "我家/客厅/药箱"},
                {"item_id": 2, "item_name": "退热贴", "location_path": "我家/卧室"},
            ],
            "recommendations": [],
        }
        out = botfmt.format_result(result)
        self.assertIn("布洛芬", out)
        self.assertIn("退热贴", out)
        self.assertIn("我家/客厅/药箱", out)
        # 纯文本契约: 不许出现 Markdown 标记
        self.assertNotIn("*", out)
        self.assertNotIn("_", out)

    def test_assist_reply_keeps_recommendations(self):
        from app.services import botfmt
        result = {
            "speech": "给你几个", "executed": False,
            "candidates": [{"item_id": 1, "item_name": "布洛芬",
                            "location_path": "我家/客厅/药箱"}],
            "recommendations": [{"item_id": 1, "purpose": "退烧"}],
        }
        out = botfmt.format_result(result)
        self.assertIn("布洛芬", out)
        self.assertIn("退烧", out)

    def test_reply_goes_through_format_result_not_bare_speech(self):
        """把 botflow 那两处改回 result["speech"] 时这条必须红 ——
        上一轮的两条测试直接调 format_result, 改回去照样绿, 等于没钉住接线。"""
        import asyncio
        from app.llm import intent as I
        from app.services import botflow
        from _fixtures import make_session, seed
        db = make_session(); seed(db)
        orig = I.parse_intent
        # "充电" 在夹具里能模糊命中充电宝和充电器两条
        I.parse_intent = self._fake_parse([
            {"intent": "find", "item_name": "充电", "quantity": 1,
             "item_id": None, "location_id": None, "location_name": None,
             "force_new": False}])
        try:
            reply = asyncio.run(botflow.handle_bot_message(
                "tg", "c1", "u1", "充电的东西在哪", db, self._cfg()))
        finally:
            I.parse_intent = orig
        self.assertIn("充电宝", reply)
        self.assertIn("充电器", reply)

    def test_high_risk_without_sender_id_refuses(self):
        """认不出说话人时, 待确认方案的 key 会退化成全群共享, 同群任何人
        回"确认"都能执行别人挂起的删档。宁可不办, 也不能记到别人头上。"""
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
                "tg", "c1", "", "把螺丝刀删了", db, self._cfg()))
        finally:
            I.parse_intent = orig
        self.assertIsNone(pending.peek("tg", "c1", ""), "空身份不许存待确认")
        self.assertIn("认不出", reply)

    def test_low_confidence_without_sender_id_refuses(self):
        """低置信度那条分支也会存 pending —— 所以它同样需要认得出说话人。
        上一轮只在高风险分支加了守卫, 这条是补上的另一半。"""
        import asyncio
        from app.llm import intent as I
        from app.services import botflow, pending
        from _fixtures import make_session, seed, item_by
        db = make_session(); items = seed(db)[1]
        luosidao = item_by(items, "螺丝刀")
        before_qty = luosidao.quantity
        orig = I.parse_intent
        I.parse_intent = self._fake_parse_conf([
            {"intent": "take_out", "item_name": "螺丝刀", "quantity": 1,
             "item_id": None, "location_id": None, "location_name": None,
             "force_new": False}], 0.2)
        try:
            reply = asyncio.run(botflow.handle_bot_message(
                "tg", "c1", "", "拿个螺丝刀", db, self._cfg()))
        finally:
            I.parse_intent = orig
        self.assertIsNone(pending.peek("tg", "c1", ""), "空身份不许存待确认")
        self.assertIn("认不出", reply)
        db.refresh(luosidao)
        self.assertEqual(luosidao.quantity, before_qty, "拒绝时不能写库")

    def test_low_risk_still_works_without_sender_id(self):
        """低风险直接执行不存 pending, 不需要身份 —— 守卫不该挡它。"""
        import asyncio
        from app.llm import intent as I
        from app.services import botflow, pending
        from _fixtures import make_session, seed, item_by
        db = make_session(); items = seed(db)[1]
        luosidao = item_by(items, "螺丝刀")
        before_qty = luosidao.quantity
        orig = I.parse_intent
        I.parse_intent = self._fake_parse([
            {"intent": "take_out", "item_name": "螺丝刀", "quantity": 1,
             "item_id": None, "location_id": None, "location_name": None,
             "force_new": False}])
        try:
            reply = asyncio.run(botflow.handle_bot_message(
                "tg", "c1", "", "拿个螺丝刀", db, self._cfg()))
        finally:
            I.parse_intent = orig
        self.assertIsNone(pending.peek("tg", "c1", ""))
        self.assertNotIn("认不出", reply)
        db.refresh(luosidao)
        self.assertEqual(before_qty - luosidao.quantity, 1, "低风险该照常执行")


class ChannelWiringTest(unittest.TestCase):
    """三端传给公共流程的 (channel, chat_id, sender_id, text) 四元组。
    传错顺序或传错字段, 群机器人会把待确认方案记到别人头上 —— 而这种错
    在单元测试之外几乎不可能被发现。"""

    def _capture(self):
        seen = {}

        async def fake(channel, chat_id, sender_id, text, db, cfg):
            seen.update(channel=channel, chat_id=chat_id,
                        sender_id=sender_id, text=text)
            return "ok"
        return seen, fake

    def test_telegram_passes_chat_and_user_ids(self):
        import asyncio
        from app.config import store
        from app.services import botflow, telegram as tg
        seen, fake = self._capture()
        orig = botflow.handle_bot_message
        orig_send = tg._send_message

        async def no_send(*a, **k):
            return None
        botflow.handle_bot_message = fake
        tg._send_message = no_send
        try:
            asyncio.run(tg._handle_update({"message": {
                "text": "螺丝刀在哪",
                "chat": {"id": 12345},
                "from": {"id": 67890, "is_bot": False},
            }}, store.get()))
        finally:
            botflow.handle_bot_message = orig
            tg._send_message = orig_send
        self.assertEqual(seen["channel"], "telegram")
        self.assertEqual(seen["chat_id"], "12345")
        self.assertEqual(seen["sender_id"], "67890")
        self.assertEqual(seen["text"], "螺丝刀在哪")

    def test_telegram_strips_at_mention(self):
        import asyncio
        from app.config import store
        from app.services import botflow, telegram as tg
        seen, fake = self._capture()
        orig = botflow.handle_bot_message
        orig_send = tg._send_message

        async def no_send(*a, **k):
            return None
        botflow.handle_bot_message = fake
        tg._send_message = no_send
        try:
            asyncio.run(tg._handle_update({"message": {
                "text": "@my_bot 确认",
                "chat": {"id": 1}, "from": {"id": 2, "is_bot": False},
            }}, store.get()))
        finally:
            botflow.handle_bot_message = orig
            tg._send_message = orig_send
        self.assertEqual(seen["text"], "确认",
                         "@提及不剥掉的话, classify_reply 会判成 other、把方案丢掉")

    def test_feishu_passes_sender_id_through(self):
        """飞书的 sender_id 以前提取了却没往下传, T7 才接上 —— 钉住它。"""
        import asyncio
        from app.config import store
        from app.services import botflow, feishu
        seen, fake = self._capture()
        orig = botflow.handle_bot_message
        orig_send = feishu._send_text
        botflow.handle_bot_message = fake
        feishu._send_text = lambda *a, **k: None
        try:
            asyncio.run(feishu._handle_async(
                "螺丝刀在哪", "oc_chat_1", "ou_sender_1", store.get()))
        finally:
            botflow.handle_bot_message = orig
            feishu._send_text = orig_send
        self.assertEqual(seen["channel"], "feishu")
        self.assertEqual(seen["chat_id"], "oc_chat_1")
        self.assertEqual(seen["sender_id"], "ou_sender_1")


if __name__ == "__main__":
    unittest.main()
