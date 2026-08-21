"""端到端走一遍真实的 FastAPI 路由 —— 单元测试碰不到 response_model 的校验。

不联网: parse_intent 被替换成固定的假解析结果, 只测 "路由 + plan/apply + 序列化"
这一段。DB 用临时文件 sqlite, config 用临时 json。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

os.environ.setdefault("CONFIG_PATH", os.path.join(tempfile.mkdtemp(), "config.json"))
_DB = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"

import sys                                                        # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx                                                      # noqa: E402
from _fixtures import ITEMS, LOCATIONS                            # noqa: E402


class ApiPlanApplyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import models                                     # noqa: F401
        from app.main import app
        from app.database import SessionLocal
        from app.llm import intent as I

        cls.app = app
        cls.I = I
        # 造库存 (直接用真实 session, 建在临时 db 上)
        db = SessionLocal()
        by_path = {}
        for path in LOCATIONS:
            parts = path.split("/")
            parent = by_path.get("/".join(parts[:-1])) if len(parts) > 1 else None
            loc = models.Location(name=parts[-1], kind="room",
                                  parent_id=parent.id if parent else None)
            db.add(loc); db.flush(); by_path[path] = loc
        for name, aliases, cat, qty, path in ITEMS:
            db.add(models.Item(name=name, aliases=aliases, category=cat,
                               quantity=qty, location_id=by_path[path].id))
        db.commit()
        # commit 会 expire 所有实例, 关掉 session 后再读 .id 就 DetachedInstance ——
        # 趁 session 还开着把 id 取出来。
        cls.by_path = {k: v.id for k, v in by_path.items()}
        db.close()

    def _client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                 base_url="http://t")

    def _fake_parse(self, parsed):
        """替换掉 **路由模块里的** parse_intent。

        routers/voice.py 是 `from ..llm.intent import parse_intent` 按值导入的,
        只改 app.llm.intent.parse_intent 打不到它 —— 这个坑值得留个注释。
        """
        async def fake(text, db, cfg):
            return {"parsed": parsed, "summary": {"text": ""}}
        from app.routers import voice as vr
        vr.parse_intent = fake

    def _run(self, coro):
        import asyncio
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_health(self):
        async def go():
            async with self._client() as c:
                r = await c.get("/api/health")
                self.assertEqual(r.status_code, 200)
        self._run(go())

    def test_plan_then_apply_creates_new_item(self):
        """完整走一遍: plan → 用户选"新建" → apply → 物品真的建出来了。"""
        self._fake_parse({
            "intent": "put_in", "confidence": 0.7, "speech": "",
            "candidates": [], "recommendations": [], "quantity": 1,
            "operations": [{"intent": "put_in", "item_name": "洗发水",
                            "location_id": self.by_path["我家/卫生间/洗漱柜"],
                            "quantity": 1}],
        })

        async def go():
            async with self._client() as c:
                r = await c.post("/api/voice/intent", json={
                    "text": "把洗发水放进洗漱柜",
                    "context": {"plan_only": True}})
                self.assertEqual(r.status_code, 200, r.text)
                d = r.json()
                self.assertEqual(d["stage"], "plan")
                self.assertTrue(d["plan_id"])
                op = d["operations"][0]
                self.assertEqual(op["selected"], "new")
                self.assertTrue(op["allow_new"])
                self.assertTrue(op["options"], "options 必须序列化出来")
                self.assertEqual(op["new_name_default"], "洗发水")

                # 库里还没有洗发水
                items = (await c.get("/api/items?q=洗发水")).json()
                self.assertEqual([i for i in items if i["name"] == "洗发水"], [])

                r2 = await c.post("/api/voice/apply", json={
                    "text": "把洗发水放进洗漱柜", "plan_id": d["plan_id"],
                    "decisions": [{"intent": "put_in", "option_key": "new",
                                   "new_item_name": "洗发水",
                                   "location_id": op["location_id"], "quantity": 2}]})
                self.assertEqual(r2.status_code, 200, r2.text)
                d2 = r2.json()
                self.assertEqual(d2["stage"], "applied")
                self.assertTrue(d2["executed"])
                self.assertTrue(d2["operations"][0]["transaction_id"])

                items = (await c.get("/api/items?q=洗发水")).json()
                made = [i for i in items if i["name"] == "洗发水"]
                self.assertEqual(len(made), 1)
                self.assertEqual(made[0]["quantity"], 2)
                # 洗手液没被动
                soap = [i for i in (await c.get("/api/items?q=洗手液")).json()
                        if i["name"] == "洗手液"]
                self.assertEqual(soap[0]["quantity"], 1)
        self._run(go())

    def test_apply_empty_decisions_is_400(self):
        async def go():
            async with self._client() as c:
                r = await c.post("/api/voice/apply", json={"text": "x", "decisions": []})
                self.assertEqual(r.status_code, 400)
        self._run(go())

    def test_transactions_expose_undoable(self):
        """流水接口必须带 undoable/undo_note, 前端的回撤按钮全靠它。"""
        async def go():
            async with self._client() as c:
                # 自己造一条流水, 不依赖别的测试方法的执行顺序。
                # include_depleted=true: 前面的测试可能已经把卷尺取到 0,
                # 而 /api/items 默认隐藏库存为 0 的记录。
                items = (await c.get("/api/items?q=卷尺&include_depleted=true")).json()
                tape = next(i for i in items if i["name"] == "卷尺")
                r0 = await c.post(f"/api/items/{tape['id']}/transactions", json={
                    "item_id": tape["id"], "action": "take_out", "quantity": 1,
                    "location_id": tape["location_id"], "note": "测试"})
                self.assertEqual(r0.status_code, 200, r0.text)
                txs = (await c.get("/api/transactions?limit=50")).json()
                self.assertTrue(txs, "刚建的流水应该查得到")
                self.assertIn("undoable", txs[0])
                self.assertIn("undo_note", txs[0])
                undoable = [t for t in txs if t["undoable"]]
                self.assertTrue(undoable, "最新那条应该可以回撤")
                # 真的撤一次
                r = await c.post("/api/revise/undo",
                                 json={"transaction_id": undoable[0]["id"]})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertIn("message", r.json())
        self._run(go())

    def test_plan_only_ignored_when_setting_off(self):
        """confirm_before_apply 关掉时, 即使前端传 plan_only 也直接执行 —— 开关的
        最终裁决权在服务端。"""
        from app.config import store
        self._fake_parse({
            "intent": "take_out", "confidence": 0.95, "speech": "",
            "candidates": [], "recommendations": [], "quantity": 1,
            "operations": [{"intent": "take_out", "item_name": "卷尺", "quantity": 1}],
        })
        store.update({"voice": {"confirm_before_apply": False}})
        try:
            async def go():
                async with self._client() as c:
                    d = (await c.post("/api/voice/intent", json={
                        "text": "拿了卷尺", "context": {"plan_only": True}})).json()
                    self.assertNotEqual(d["stage"], "plan")
                    self.assertTrue(d["executed"])
            self._run(go())
        finally:
            store.update({"voice": {"confirm_before_apply": True}})

    def test_search_can_include_depleted_items(self):
        """按名字搜一个库存为 0 的物品, 传了 include_depleted 就必须搜得到。

        回归: search_items 内部默认过滤 quantity=0, 以前没把这个开关传进去,
        物品页搜"抽纸"(夹具里库存 0) 会搜不到, only_depleted+q 恒空。
        """
        async def go():
            async with self._client() as c:
                hidden = (await c.get("/api/items?q=抽纸")).json()
                self.assertEqual([i for i in hidden if i["name"] == "抽纸"], [],
                                 "默认应当隐藏库存为 0 的")
                shown = (await c.get("/api/items?q=抽纸&include_depleted=true")).json()
                self.assertTrue([i for i in shown if i["name"] == "抽纸"],
                                "传了 include_depleted 就该搜得到")
                only = (await c.get("/api/items?q=抽纸&only_depleted=true")).json()
                self.assertTrue([i for i in only if i["name"] == "抽纸"],
                                "only_depleted + q 不该恒空")
        self._run(go())

    def test_config_migration_raises_low_max_tokens(self):
        from app.config import store
        cfg = store.update({"llm": {"max_tokens": 512}})
        self.assertGreaterEqual(cfg.llm.max_tokens, 2048,
                                "改回 512 必须被自动抬高, 否则多物品又会静默丢失")


if __name__ == "__main__":
    unittest.main(verbosity=2)
