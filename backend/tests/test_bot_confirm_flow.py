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

    def test_format_plan_shows_which_record_was_preselected(self):
        """高风险方案的全部意义是让用户看清要改的是哪一条。只写用户说的原词
        ("充电"), 用户无从判断确认下去会动充电宝还是充电器。"""
        from app.services import botfmt
        plan = {"operations": [{
            "intent": "take_out", "item_name": "充电", "quantity": 1,
            "matched_by": "fuzzy", "item_id": 7, "selected": "i:7",
            "location_options": [], "reason": "只有名字相近的, 请复核",
            "candidates": [
                {"item_id": 7, "item_name": "充电器", "location_path": "我家/书房/书桌1"},
                {"item_id": 8, "item_name": "充电宝", "location_path": "我家/书房"},
            ]}]}
        out = botfmt.format_plan(plan, "有物品没认准, 是从几个候选里挑的")
        self.assertIn("充电器", out, "必须写明实际会改的是哪一条")
        self.assertIn("我家/书房/书桌1", out)

    def test_format_plan_marks_skip_as_not_executing(self):
        """selected=="skip" 渲染得和可执行条目一样, 用户会以为确认下去
        它也会被执行 —— 实际上什么都不会发生。"""
        from app.services import botfmt
        plan = {"operations": [{
            "intent": "take_out", "item_name": "不存在的东西", "quantity": 1,
            "matched_by": "none", "item_id": None, "selected": "skip",
            "location_options": [], "candidates": []}]}
        out = botfmt.format_plan(plan, "库里没有这个物品")
        self.assertIn("跳过", out)

    def test_location_ambiguity_reply_lists_every_operation(self):
        """位置歧义时整句都不执行, 所以回复必须把每一条都列出来 ——
        只列有歧义的那几条, 用户会以为其余的做成了, 那条操作就此永久丢失。"""
        from app.services import botfmt
        plan = {"operations": [
            {"intent": "take_out", "item_name": "螺丝刀", "quantity": 1,
             "location_options": [], "matched_by": "exact"},
            {"intent": "put_in", "item_name": "卷尺", "quantity": 1,
             "location_name": "书桌", "matched_by": "exact",
             "location_options": [{"location_id": 1, "name": "书桌1", "path": "我家/书房/书桌1"},
                                  {"location_id": 2, "name": "书桌10", "path": "我家/书房/书桌10"}]},
        ]}
        out = botfmt.format_location_ambiguity(plan)
        self.assertIn("螺丝刀", out, "没歧义的那条也必须出现, 否则用户以为它做成了")
        self.assertIn("卷尺", out)
        self.assertIn("都没执行", out)

    def test_location_ambiguity_candidates_are_one_per_line(self):
        """候选路径本身用 " / " 分隔层级, 如果多个候选也用 " / " 拼在一起,
        读起来像一条 6 级深的路径而不是两个候选 —— 这条恢复路径 (让用户
        照着候选重说) 实际上就断了。"""
        from app.services import botfmt
        plan = {"operations": [
            {"intent": "put_in", "item_name": "卷尺", "quantity": 1,
             "location_name": "书桌", "matched_by": "exact",
             "location_options": [{"location_id": 1, "name": "书桌1", "path": "我家/书房/书桌1"},
                                  {"location_id": 2, "name": "书桌10", "path": "我家/书房/书桌10"}]},
        ]}
        out = botfmt.format_location_ambiguity(plan)
        self.assertEqual(out.count("\n     ("), 2, "两个候选应各自独占一行")
        self.assertNotIn("书桌1 / 我家", out, "两条候选路径不能用 \" / \" 直接拼接")


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

    def test_llm_error_text_is_not_leaked_to_group(self):
        """LLMError 带上游网关响应体前 500 字节 —— 可能含 API key 前缀、
        内网 base_url、HTML 错误页。群里是真人在看。"""
        import asyncio
        from app.llm import intent as I
        from app.llm.client import LLMError
        from app.services import botflow
        from _fixtures import make_session, seed
        db = make_session(); seed(db)
        orig = I.parse_intent

        async def boom(text, db_, cfg):
            raise LLMError('LLM HTTP 401: {"error":{"message":"Incorrect API key '
                           'provided: sk-abcd1234xyz9"}}')
        I.parse_intent = boom
        try:
            reply = asyncio.run(botflow.handle_bot_message(
                "tg", "c1", "u1", "螺丝刀在哪", db, self._cfg()))
        finally:
            I.parse_intent = orig
        self.assertNotIn("sk-abcd", reply)
        self.assertNotIn("401", reply)

    def test_confirm_delete_does_not_relist_deleted_item(self):
        """delete 不产生 Transaction (executed=False), 而 base 里 5 分钟前算
        方案时的 candidates 快照原样带进 apply —— format_result 在没有
        recommendations 时会把 candidates 当"位置:"清单打出来, 打的是刚
        被删掉的那条, 看上去像没删成、容易导致用户重复操作。"""
        import asyncio
        from app.services import botflow, pending
        from _fixtures import make_session, seed, item_by
        db = make_session(); items = seed(db)[1]
        luosidao = item_by(items, "螺丝刀")
        pending.put("tg", "c1", "u1", {
            "stage": "plan",
            "operations": [{"intent": "delete_item", "item_name": "螺丝刀",
                            "quantity": 1, "selected": f"i:{luosidao.id}",
                            "item_id": luosidao.id, "location_id": None,
                            "location_name": None, "location_options": []}],
            "candidates": [{"item_id": luosidao.id, "item_name": "螺丝刀",
                            "location_path": "我家/书房"}],
        })
        reply = asyncio.run(botflow.handle_bot_message(
            "tg", "c1", "u1", "确认", db, self._cfg()))
        self.assertIn("已永久删除", reply)
        self.assertNotIn("位置:", reply, "delete 已成功, 不该再把被删物品列进候选清单")


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
        # 桩掉 bot 用户名: 不桩的话 _get_bot_username 会真的去打
        # api.telegram.org/getMe。CI 是离线跑的, 那次请求只会超时后被兜底吃掉,
        # 但每条这样的测试都要白等一次超时。空串 = 拿不到用户名 = 不剥前缀。
        tg._bot_username = ""
        try:
            asyncio.run(tg._handle_update({"message": {
                "text": "螺丝刀在哪",
                "chat": {"id": 12345},
                "from": {"id": 67890, "is_bot": False},
            }}, store.get()))
        finally:
            botflow.handle_bot_message = orig
            tg._send_message = orig_send
            tg._bot_username = None
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
        tg._bot_username = "my_bot"          # 跳过 getMe

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
            tg._bot_username = None
        self.assertEqual(seen["text"], "确认",
                         "@提及不剥掉的话, classify_reply 会判成 other、把方案丢掉")

    def test_telegram_strips_only_its_own_mention(self):
        """「@张三 确认」是说给别人听的, 不能当成对机器人的确认 ——
        那会立刻执行发言者挂起的删档。"""
        import asyncio
        from app.config import store
        from app.services import botflow, telegram as tg
        seen, fake = self._capture()
        orig, orig_send = botflow.handle_bot_message, tg._send_message
        tg._bot_username = "my_bot"          # 跳过 getMe

        async def no_send(*a, **k):
            return None
        botflow.handle_bot_message = fake
        tg._send_message = no_send
        try:
            asyncio.run(tg._handle_update({"message": {
                "text": "@张三 确认", "chat": {"id": 1},
                "from": {"id": 2, "is_bot": False}}}, store.get()))
        finally:
            botflow.handle_bot_message = orig
            tg._send_message = orig_send
            tg._bot_username = None
        self.assertEqual(seen["text"], "@张三 确认", "别人的 @ 不该被剥掉")

    def test_telegram_anonymous_sender_maps_to_empty_identity(self):
        """频道消息/匿名管理员没有 from 字段 → user_id 是 None。
        str(None) 得到 "None" 是个非空字符串, 会骗过空身份守卫, 让整个群
        共用一个 pending 槽位。必须落到 "" 才能被守卫挡住。"""
        import asyncio
        from app.config import store
        from app.services import botflow, telegram as tg
        seen, fake = self._capture()
        orig, orig_send = botflow.handle_bot_message, tg._send_message

        async def no_send(*a, **k):
            return None
        botflow.handle_bot_message = fake
        tg._send_message = no_send
        tg._bot_username = ""        # 别让测试去打真实的 getMe
        try:
            asyncio.run(tg._handle_update({"channel_post": {
                "text": "螺丝刀在哪", "chat": {"id": -100123},
            }}, store.get()))
        finally:
            botflow.handle_bot_message = orig
            tg._send_message = orig_send
            tg._bot_username = None
        self.assertEqual(seen["sender_id"], "", f'得到 {seen["sender_id"]!r}')

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
