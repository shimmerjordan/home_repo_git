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


if __name__ == "__main__":
    unittest.main()
