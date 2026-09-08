"""/api/transactions 的分页 —— 「近期取放记录」翻页就靠这个 offset。

行为断言 (下面几条) 覆盖的是 offset 本身: 逐页取完等于一次取完, 一条不重、一条不
漏; 越界返回空而不是报错; 多要 1 条能判断有没有下一页。

排序的并列问题另说。一次 apply 会在同一瞬间写好几条流水, created_at 完全相同, 而
SQL 对并列行的返回顺序不作保证 —— 翻到第 2 页可能又看见第 1 页那条, 另一条则永远
翻不到。代码里加了 id 作次序键兜住这点, 但**行为测试抓不到它**: 实测把 id.desc()
去掉, 下面的分页断言照样全绿, 因为 SQLite 对这条查询恰好按 rowid 返回。所以那一条
只能用结构断言钉住 (test_order_by_has_a_tiebreaker), 别误以为是行为验证。

注意断言都是**相对**当前流水条数写的, 不是相对本文件塞进去的那 25 条。`discover`
跑整套时 app.database 只会被最先 import 的那个模块初始化一次, 这个模块设的
DATABASE_URL 根本不生效, 于是库里躺着别的用例写下的流水 —— 写死 25 的话单跑绿、
整套红, 而 CI 跑的正是整套。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime

os.environ.setdefault("CONFIG_PATH", os.path.join(tempfile.mkdtemp(), "config.json"))
_DB = os.path.join(tempfile.mkdtemp(), "paging.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"

import sys                                                        # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx                                                      # noqa: E402

SEEDED = 25                                  # 本文件自己塞进去的条数
SAME_INSTANT = datetime(2026, 9, 8, 10, 0, 0)


class TxPagingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import models
        from app.main import app
        from app.database import SessionLocal

        cls.app = app
        db = SessionLocal()
        loc = models.Location(name="书房", kind="room")
        db.add(loc); db.flush()
        item = models.Item(name="螺丝", quantity=100, location_id=loc.id)
        db.add(item); db.flush()
        # 全部写成同一个时刻 —— 这正是一次批量 apply 的真实形态。
        for i in range(SEEDED):
            db.add(models.Transaction(
                item_id=item.id, location_id=loc.id, action="take_out",
                quantity=1, note=f"#{i}", created_at=SAME_INSTANT))
        db.commit()
        cls.total = db.query(models.Transaction).count()
        db.close()
        assert cls.total >= SEEDED

    def _client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                 base_url="http://t")

    async def _page(self, limit, offset):
        async with self._client() as c:
            r = await c.get(f"/api/transactions?limit={limit}&offset={offset}")
            self.assertEqual(r.status_code, 200, r.text)
            return r.json()

    def test_offset_defaults_to_zero(self):
        import asyncio
        first = asyncio.run(self._page(5, 0))
        async def _bare():
            async with self._client() as c:
                return (await c.get("/api/transactions?limit=5")).json()
        self.assertEqual([r["id"] for r in asyncio.run(_bare())],
                         [r["id"] for r in first])

    def test_pages_partition_the_feed_exactly(self):
        """逐页取完 == 一次取完 (同一 offset 契约, 不涉及并列排序)。"""
        import asyncio
        whole = [r["id"] for r in asyncio.run(self._page(self.total, 0))]
        self.assertEqual(len(whole), self.total)

        walked = []
        page_size = 7
        offset = 0
        while True:
            rows = asyncio.run(self._page(page_size, offset))
            if not rows:
                break
            walked.extend(r["id"] for r in rows)
            offset += page_size
            self.assertLess(offset, self.total + page_size * 2, "翻页没有终点")

        self.assertEqual(walked, whole)
        self.assertEqual(len(set(walked)), self.total, "有重复行")

    def test_lookahead_row_signals_next_page(self):
        """前端靠"多要 1 条"判断有没有下一页 —— 不额外查 total 的前提。"""
        import asyncio
        size = 10
        self.assertGreater(self.total, size, "流水太少, 这条断言没意义")
        # 第一页之后还有: 能拿到第 size+1 条
        self.assertEqual(len(asyncio.run(self._page(size + 1, 0))), size + 1)
        # 停在最后一页的页首: 剩下的不足 size+1 条 → 前端据此判定没有下一页
        last_offset = (self.total // size) * size
        if last_offset == self.total:            # 正好整除, 退一页才有剩余
            last_offset -= size
        last = asyncio.run(self._page(size + 1, last_offset))
        self.assertEqual(len(last), self.total - last_offset)
        self.assertLessEqual(len(last), size)

    def test_offset_past_the_end_is_empty_not_an_error(self):
        import asyncio
        self.assertEqual(asyncio.run(self._page(10, self.total + 50)), [])

    def test_order_by_has_a_tiebreaker(self):
        """并列的 created_at 必须再按 id 排。

        这是结构断言而不是行为断言, 理由见模块开头: SQLite 现在恰好按 rowid 返回,
        去掉 id.desc() 上面那些分页断言一条都不会红。换个执行计划 (比如哪天给
        created_at 建了索引) 就不再是这个顺序, 而那时翻页会开始重复/漏行。
        """
        import inspect
        from app.routers import items as R
        src = inspect.getsource(R.recent_transactions)
        head, _, tail = src.partition("order_by(")
        self.assertTrue(tail, "recent_transactions 里找不到 order_by")
        clause = tail.split(")\n")[0]
        self.assertIn("created_at.desc()", clause)
        self.assertIn("id.desc()", clause)

    def test_negative_offset_rejected(self):
        import asyncio
        async def _go():
            async with self._client() as c:
                return await c.get("/api/transactions?limit=5&offset=-1")
        self.assertEqual(asyncio.run(_go()).status_code, 422)


if __name__ == "__main__":
    unittest.main()
