"""位置名子串误命中: 说"书桌1"而库里只有"书桌10"时, 老实现会静默落到书桌10。
现在的规则是: 精确名优先; 无精确名且多个子串候选时不猜, 把候选交给用户选。"""
from __future__ import annotations

import unittest

from _fixtures import item_by, make_session, seed
from app import models
from app.config import AppConfig
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


class ApplySkipLocationAmbiguityTest(unittest.TestCase):
    def test_skip_decision_wins_over_ambiguous_location(self):
        """用户明确选了跳过, 就该回"已跳过", 不该回"位置不明确"。"""
        db = make_session()
        by_path, _ = seed(db)
        db.add(models.Location(name="书桌10", kind="box",
                               parent_id=by_path["我家/书房"].id))
        db.flush()
        base = {"operations": [], "speech": ""}
        r = I.apply_operations(db, "把卷尺放进书桌", [
            {"intent": "put_in", "option_key": "skip", "location_name": "书桌",
             "quantity": 1}], base)
        op = r["operations"][0]
        self.assertIn("跳过", op["speech"])
        self.assertNotIn("不明确", op["speech"])


class DirectPathLocationAmbiguityTest(unittest.TestCase):
    """单条直连路径 (plan_only=False, 群机器人/关掉二次确认的语音走的那条) 以前遇到
    位置歧义会用 `_` 把候选丢掉, location_id 变成 None, 高置信度时照样 commit ——
    这个组合 (plan_only=False + 位置歧义 + 高置信度) 以前完全没测试覆盖。"""

    def test_high_confidence_putin_with_ambiguous_location_does_not_execute(self):
        db = make_session()
        by_path, items = seed(db)
        db.add(models.Location(name="书桌10", kind="box",
                               parent_id=by_path["我家/书房"].id))
        db.flush()
        juanchi = item_by(items, "卷尺")
        before_qty = juanchi.quantity
        parsed = {"intent": "put_in", "confidence": 1.0, "speech": "",
                  "item_id": juanchi.id, "location_name": "书桌", "quantity": 1}
        r = I.execute_intent(db, "把卷尺放进书桌", parsed, AppConfig())
        db.refresh(juanchi)
        self.assertEqual(juanchi.quantity, before_qty, "不该悄悄丢掉位置继续执行")
        self.assertFalse(r["executed"])
        self.assertTrue(r["needs_confirmation"])
        self.assertIn("不明确", r["speech"])
        self.assertIn("书桌", r["speech"])


if __name__ == "__main__":
    unittest.main()
