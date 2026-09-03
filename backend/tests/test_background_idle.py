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
