"""飞书 Stream Mode 长连接 supervisor 的行为测试。

不联网, 不 sleep: lark 的 ws.Client 用 FakeWsClient 顶掉 (整段替换
feishu._build_clients), 时间用可注入的 feishu._now 手动推。

FakeWsClient 的 start() 故意复刻 lark 的行为 —— 在传进来的 loop 上
run_until_complete 一个永不完成的协程 —— 这样 "_stop_ws 之后线程真的退出"
才是真的在测 call_soon_threadsafe(loop.stop) 那条路, 而不是测一个假动作。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace

# 让 `python -m unittest discover -s tests` 能 import 到 app 包。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("CONFIG_PATH", "/tmp/feishu-test-config.json")
os.environ.setdefault("LOG_DIR", "/tmp/feishu-test-logs")

from app.services import feishu  # noqa: E402


class FakeConn:
    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class FakeWsClient:
    """行为对齐 lark_oapi.ws.Client 的关键部分。"""

    def __init__(self, loop, fail_with: BaseException | None = None) -> None:
        self._loop = loop
        self._conn = None if fail_with else FakeConn()
        self._conn_id = "fake-conn-1"
        self._lock = None            # 只在需要测锁泄漏时才建真 asyncio.Lock
        self._fail_with = fail_with
        self.disconnect_calls = 0
        self.started = threading.Event()
        self.start_returned = threading.Event()

    def start(self) -> None:
        self.started.set()
        try:
            if self._fail_with is not None:
                raise self._fail_with
            # lark: loop.run_until_complete(_select()), 永不自己返回。
            self._loop.run_until_complete(self._select())
        finally:
            self.start_returned.set()

    async def _select(self) -> None:
        while True:
            await asyncio.sleep(3600)

    async def _disconnect(self) -> None:
        self.disconnect_calls += 1
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


class FeishuSupervisorTestCase(unittest.TestCase):
    """公共脚手架: 假配置 + 假时钟 + 每个用例结束把模块状态收干净。"""

    def setUp(self) -> None:
        self.clock = 1000.0
        self.built: list[FakeWsClient] = []
        self.fail_with: BaseException | None = None

        self._orig = {name: getattr(feishu, name) for name in
                      ("_now", "_build_clients", "store", "_try_import")}
        # 这些用例故意走崩溃 / 判死路径, 让 log.exception 的 traceback 静音,
        # 否则测试输出被刷成一坨, 真失败反而看不见。
        self._log = logging.getLogger("storage.feishu")
        self._log_level = self._log.level
        self._log.setLevel(logging.CRITICAL)

        feishu._now = lambda: self.clock
        feishu._try_import = lambda: True
        feishu.store = SimpleNamespace(get=lambda: SimpleNamespace(
            feishu=SimpleNamespace(enabled=True, app_id="cli_test",
                                   app_secret="secret", allowed_chat_ids=[],
                                   allowed_open_ids=[])))

        def build(app_id, app_secret, ws_loop):
            client = FakeWsClient(ws_loop, fail_with=self.fail_with)
            self.built.append(client)
            return object(), client

        feishu._build_clients = build
        self._reset_state()

    def tearDown(self) -> None:
        feishu._stop_ws()
        self._log.setLevel(self._log_level)
        for name, value in self._orig.items():
            setattr(feishu, name, value)
        self._reset_state()

    def _reset_state(self) -> None:
        feishu._ws_thread = None
        feishu._ws_client = None
        feishu._ws_loop = None
        feishu._api_client = None
        feishu._running_app_id = ""
        feishu._running_app_secret = ""
        feishu._last_thread_start = 0.0
        feishu._consecutive_failures = 0
        feishu._limit_hit = False
        feishu._unhealthy_since = None
        feishu._connected_since = None
        feishu._stop_requested = False

    # -- helpers ------------------------------------------------------------
    def _tick(self) -> None:
        """跑一轮 supervisor。"""
        asyncio.run(feishu._supervise_once())

    def _wait_started(self, client: FakeWsClient) -> None:
        self.assertTrue(client.started.wait(5), "WS 线程没起来")

    def _wait_built(self, n: int) -> FakeWsClient:
        """_restart_ws 在 thread.start() 之后就返回了, 建 client 发生在新线程里 ——
        断言个数之前必须等它真的建出来, 不然是竞态。"""
        deadline = time.time() + 5
        while time.time() < deadline:
            if len(self.built) >= n:
                return self.built[n - 1]
            time.sleep(0.005)
        self.fail(f"只建了 {len(self.built)} 个 client, 期望 {n}")

    def _ws_threads(self) -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t.name == "feishu-ws" and t.is_alive()]

    def _boot(self) -> FakeWsClient:
        """起一条健康连接, 返回它的 fake client。"""
        self._tick()
        client = self._wait_built(1)
        self._wait_started(client)
        self._tick()   # 让 supervisor 观察到 "已连接"
        self.assertTrue(feishu._connection_alive())
        return client


class TestDeadConnectionRestart(FeishuSupervisorTestCase):
    def test_dead_connection_restarts_after_grace(self):
        """连接假死 (_conn.closed=True) 超过 DEAD_AFTER_S 才重启, 不到就不动。"""
        first = self._boot()
        first_thread = feishu._ws_thread

        first._conn.closed = True      # 连接死了, 但 lark 的线程还活着
        self.assertFalse(feishu._connection_alive())

        self._tick()                   # 只是记下 _unhealthy_since
        self.assertIsNotNone(feishu._unhealthy_since)
        self.assertEqual(len(self.built), 1, "宽限期内不该重启")

        self.clock += feishu.DEAD_AFTER_S - 1
        self._tick()
        self.assertEqual(len(self.built), 1, "宽限期还没过就重启了")

        # 越过宽限期 + 越过退避窗口 (第一次启动已经把 _consecutive_failures 记成 1)
        self.clock += 2 + feishu._BACKOFF_NORMAL[0]
        self._tick()

        second = self._wait_built(2)
        self.assertGreaterEqual(first.disconnect_calls, 1, "重启前必须把旧连接关掉")
        self.assertFalse(first_thread.is_alive(), "旧线程必须真的退出")
        self._wait_started(second)
        self.assertEqual(len(self._ws_threads()), 1, "任何时刻只能有一条 feishu-ws")

    def test_dead_thread_restarts_without_grace(self):
        """线程整条没了是确定性的死, 不用等 DEAD_AFTER_S, 只受退避约束。"""
        self.fail_with = RuntimeError("boom")
        self._tick()
        client = self._wait_built(1)
        self.assertTrue(client.start_returned.wait(5))
        thread = feishu._ws_thread
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

        self.clock += feishu._BACKOFF_NORMAL[0] + 1
        self._tick()
        self._wait_built(2)


class TestHealthyConnectionStable(FeishuSupervisorTestCase):
    def test_healthy_never_restarts_and_stays_single(self):
        """连接健康时反复轮询也不重启, 且始终只有一条 feishu-ws 线程。"""
        client = self._boot()
        for _ in range(25):
            self.clock += feishu.POLL_INTERVAL_S
            self._tick()
            self.assertEqual(len(self._ws_threads()), 1)
        self.assertEqual(len(self.built), 1, "健康连接被无谓重启了")
        self.assertEqual(client.disconnect_calls, 0)
        # 撑过 HEALTHY_RESET_S 之后退避计数应该清零。
        self.assertEqual(feishu._consecutive_failures, 0)
        self.assertFalse(feishu._limit_hit)

        h = feishu.health()
        self.assertTrue(h["connected"])
        self.assertTrue(h["thread_alive"])
        self.assertEqual(h["conn_id"], "fake-conn-1")
        self.assertGreater(h["uptime_s"], 0)
        self.assertEqual(h["next_retry_in_s"], 0.0)

    def test_disabled_stops_thread(self):
        client = self._boot()
        thread = feishu._ws_thread
        feishu.store = SimpleNamespace(get=lambda: SimpleNamespace(
            feishu=SimpleNamespace(enabled=False, app_id="cli_test",
                                   app_secret="secret", allowed_chat_ids=[],
                                   allowed_open_ids=[])))
        self._tick()
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(client.disconnect_calls, 1)
        self.assertIsNone(feishu._ws_thread)
        self.assertEqual(self._ws_threads(), [])


class TestBackoff(FeishuSupervisorTestCase):
    def _fail_once(self, exc: BaseException) -> None:
        """让下一次连接立刻失败, 并等线程真的死透 (退避标志是在线程里翻的)。"""
        want = len(self.built) + 1
        self.fail_with = exc
        self._tick()
        client = self._wait_built(want)
        self.assertTrue(client.start_returned.wait(5))
        thread = feishu._ws_thread
        if thread is not None:
            thread.join(timeout=5)

    def test_conn_limit_uses_long_backoff(self):
        """连接数超限 → 300/600/1200/1800, 不能用 30s 一直焊在上限上。"""
        self._fail_once(RuntimeError(
            "received 3001 (registered) backbone registered too many conns "
            "[conn_id=abc123def456]"))
        self.assertTrue(feishu._limit_hit)
        self.assertEqual(feishu._consecutive_failures, 1)
        self.assertEqual(feishu._backoff_s(), 300.0)

        # 退避没到点不该重启。
        self.clock += 299
        self._tick()
        self.assertEqual(len(self.built), 1)

        self.clock += 2
        self._tick()
        self._wait_built(2)
        self.assertEqual(feishu._consecutive_failures, 2)
        self.assertEqual(feishu._backoff_s(), 600.0)

    def test_normal_failure_uses_short_backoff(self):
        self._fail_once(RuntimeError("connection reset by peer"))
        self.assertFalse(feishu._limit_hit)
        self.assertEqual(feishu._backoff_s(), 30.0)

        self.clock += 29
        self._tick()
        self.assertEqual(len(self.built), 1)

        self.clock += 2
        self._tick()
        self._wait_built(2)
        self.assertEqual(feishu._backoff_s(), 60.0)

    def test_backoff_caps(self):
        feishu._limit_hit = False
        feishu._consecutive_failures = 99
        self.assertEqual(feishu._backoff_s(), 300.0)
        feishu._limit_hit = True
        self.assertEqual(feishu._backoff_s(), 1800.0)


class WedgedWsClient(FakeWsClient):
    """start() 卡在同步阻塞里, 停不掉。

    复刻 lark `_get_conn_url()` 那个不带 timeout 的 requests.post: loop 在跑但被
    同步调用堵死, call_soon_threadsafe(loop.stop) 和 _disconnect 都排不上队,
    于是 _stop_ws 只能 join 超时。这是 "两条 feishu-ws 同时活着" 的唯一入口。
    """

    def __init__(self, loop, release: threading.Event) -> None:
        super().__init__(loop)
        self._conn = None          # 还卡在握手前 → _connection_alive() 是 False
        self._release = release

    def start(self) -> None:
        self.started.set()
        try:
            self._loop.run_until_complete(self._block())
        finally:
            self.start_returned.set()

    async def _block(self) -> None:
        self._release.wait(30)     # 同步阻塞, 整个 loop 都动不了


class TestSingleFlightUnderWedgedThread(FeishuSupervisorTestCase):
    """核心不变量: 任何时刻最多一条 feishu-ws。旧线程停不掉时宁可不连。"""

    def setUp(self) -> None:
        super().setUp()
        self.release = threading.Event()
        def build(app_id, app_secret, ws_loop):
            client = WedgedWsClient(ws_loop, self.release)
            self.built.append(client)
            return object(), client
        feishu._build_clients = build
        self._dt, self._jt = feishu.DISCONNECT_TIMEOUT_S, feishu.JOIN_TIMEOUT_S
        feishu.DISCONNECT_TIMEOUT_S = 0.2
        feishu.JOIN_TIMEOUT_S = 0.5

    def tearDown(self) -> None:
        self._release_and_join()
        feishu.DISCONNECT_TIMEOUT_S = self._dt
        feishu.JOIN_TIMEOUT_S = self._jt
        super().tearDown()

    def _release_and_join(self) -> None:
        """先放掉卡住的线程并等它把 loop 关掉, 再让基类 tearDown 去 _stop_ws ——
        顺序反了就会在 "线程正在退出" 的窗口里 run_coroutine_threadsafe, 白等满超时。"""
        self.release.set()
        thread = feishu._ws_thread
        if thread is not None:
            thread.join(timeout=5)

    def test_wedged_thread_never_yields_a_second_thread(self):
        self.assertTrue(feishu._restart_ws("cli", "sec"))
        first = self._wait_built(1)
        self._wait_started(first)

        self.assertFalse(feishu._restart_ws("cli", "sec"),
                         "旧线程还活着就起了新线程")
        self.assertEqual(len(self._ws_threads()), 1,
                         "出现了第二条 feishu-ws —— lark 的模块级 loop 会被改掉, "
                         "旧连接就永远关不掉了")
        self.assertEqual(len(self.built), 1)

    def test_failed_stop_keeps_refs_so_next_round_can_retry(self):
        feishu._restart_ws("cli", "sec")
        self._wait_started(self._wait_built(1))

        self.assertFalse(feishu._stop_ws(), "停不掉却报了成功")
        self.assertIsNotNone(feishu._ws_thread, "引用被清成 None = 又变回丢引用当停掉")
        self.assertIsNotNone(feishu._ws_client)
        self.assertIsNotNone(feishu._ws_loop)

        thread = feishu._ws_thread
        self._release_and_join()    # 线程走掉之后, 下一轮应该能停干净
        self.assertFalse(thread.is_alive())
        self.assertTrue(feishu._stop_ws())
        self.assertIsNone(feishu._ws_thread)

    def test_failed_restart_is_throttled_and_keeps_grace_clock(self):
        """停不掉时也要记一次尝试, 否则每 POLL_INTERVAL_S 就白占一条 executor 线程
        join 十几秒; 同时不能清 _unhealthy_since, 否则宽限期每轮从头数。"""
        self._tick()
        self._wait_started(self._wait_built(1))
        first_attempt = feishu._last_thread_start

        self.clock += 1
        self._tick()                # 连接不健康, 先记下 _unhealthy_since
        unhealthy_at = feishu._unhealthy_since
        self.assertIsNotNone(unhealthy_at)
        self.assertEqual(feishu._last_thread_start, first_attempt)

        self.clock += feishu.DEAD_AFTER_S + 1
        self._tick()                # 判死 → 进 _restart_ws, 停不掉 → 仍要记一次
        self.assertGreater(feishu._last_thread_start, first_attempt)
        self.assertEqual(feishu._unhealthy_since, unhealthy_at,
                         "起失败还清宽限期 → 下一轮又要白等 DEAD_AFTER_S")
        self.assertEqual(len(self._ws_threads()), 1)
        self.assertEqual(len(self.built), 1)

        second_attempt = feishu._last_thread_start
        self.clock += 1
        self._tick()                # 退避没到点 → 不再重试
        self.assertEqual(feishu._last_thread_start, second_attempt)


class TestShutdownBudget(FeishuSupervisorTestCase):
    """stop() 是 FastAPI shutdown 同步调的, 卡太久会被 docker SIGKILL。"""

    def setUp(self) -> None:
        super().setUp()
        self.release = threading.Event()
        self._sd, self._sj = feishu.SHUTDOWN_DISCONNECT_S, feishu.SHUTDOWN_JOIN_S
        def build(app_id, app_secret, ws_loop):
            client = WedgedWsClient(ws_loop, self.release)
            self.built.append(client)
            return object(), client
        feishu._build_clients = build

    def tearDown(self) -> None:
        self.release.set()
        thread = feishu._ws_thread
        if thread is not None:
            thread.join(timeout=5)
        feishu.SHUTDOWN_DISCONNECT_S = self._sd
        feishu.SHUTDOWN_JOIN_S = self._sj
        super().tearDown()

    def test_prod_budget_is_under_docker_grace(self):
        # 最坏情况 = disconnect + 重试 + join。compose 没配 stop_grace_period,
        # docker 默认只给 10s, 超了就是 SIGKILL。
        worst = feishu.SHUTDOWN_DISCONNECT_S * 2 + feishu.SHUTDOWN_JOIN_S
        self.assertLess(worst, 10.0, f"停机最坏 {worst}s, 超过 docker 的 10s 宽限期")

    def test_stop_uses_shutdown_budget_not_join_timeout(self):
        """线程卡住时 stop() 不能拿 JOIN_TIMEOUT_S(10s) 那套预算去等。"""
        feishu.SHUTDOWN_DISCONNECT_S = 0.2
        feishu.SHUTDOWN_JOIN_S = 0.2
        self.assertEqual(feishu.JOIN_TIMEOUT_S, 10, "前提: 常规预算是 10s")

        feishu._restart_ws("cli", "sec")
        self._wait_started(self._wait_built(1))

        t0 = time.monotonic()
        feishu.stop()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2.0, f"stop() 卡了 {elapsed:.1f}s, 用的还是常规预算")


class TestColdStart(FeishuSupervisorTestCase):
    def test_first_attempt_is_immediate(self):
        """重启容器后第一次连接不能被退避拖住 (_consecutive_failures 本来是 0)。"""
        self.assertEqual(feishu._consecutive_failures, 0)
        self.assertEqual(feishu._backoff_s(), 0.0)
        self.assertEqual(feishu._last_thread_start, 0.0)

        self._tick()                # 时钟一秒没动
        client = self._wait_built(1)
        self._wait_started(client)
        self._tick()
        self.assertTrue(feishu._connection_alive())
        self.assertEqual(feishu.health()["next_retry_in_s"], 0.0)


class TestRequestsTimeout(unittest.TestCase):
    """lark 的 _get_conn_url 用的 requests.post 必须带上超时。"""

    def test_wrapper_injects_timeout_and_delegates(self):
        calls = []

        class FakeRequests:
            Session = "sentinel"
            def post(self, url, **kw):
                calls.append((url, kw))
                return "resp"

        w = feishu._RequestsWithTimeout(FakeRequests(), 7)
        self.assertEqual(w.post("http://x", json={"a": 1}), "resp")
        self.assertEqual(calls[0][1]["timeout"], 7)
        w.post("http://x", timeout=1)
        self.assertEqual(calls[1][1]["timeout"], 1, "调用方显式给的超时不该被顶掉")
        self.assertEqual(w.Session, "sentinel", "其余属性要透传给真 requests")

    @unittest.skipUnless(feishu._try_import(), "lark-oapi 没装")
    def test_real_build_clients_installs_wrapper(self):
        """拿真 lark 跑一遍 (构造 Client 不联网), 别只测 fake。"""
        import lark_oapi.ws.client as lws
        orig_loop, orig_requests = lws.loop, lws.requests
        loop = asyncio.DefaultEventLoopPolicy().new_event_loop()
        # lark 的 ExpiringCache 在 __init__ 里就 create_task, 不把当前线程的 loop
        # 指过去, 那个 cron task 会挂到别人的 loop 上, 退出时刷 warning。
        asyncio.set_event_loop(loop)
        try:
            feishu._build_clients("cli_x", "sec_x", loop)
            self.assertIs(lws.loop, loop)
            self.assertIsInstance(lws.requests, feishu._RequestsWithTimeout)
            feishu._build_clients("cli_x", "sec_x", loop)   # 不能层层套娃
            self.assertIs(lws.requests._real, orig_requests)
        finally:
            lws.loop, lws.requests = orig_loop, orig_requests
            loop.set_exception_handler(lambda _l, _c: None)
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                # 跑一轮让 cancel 真的送达, 否则退出时刷 "never awaited"。
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            asyncio.set_event_loop(None)


class TestStopWs(FeishuSupervisorTestCase):
    def test_stop_ws_joins_thread_and_disconnects(self):
        """老实现只是丢引用; 现在必须真停: _disconnect 被调过 + 线程 join 成功。"""
        client = self._boot()
        thread = feishu._ws_thread

        self.assertTrue(feishu._stop_ws())

        self.assertGreaterEqual(client.disconnect_calls, 1, "没关连接, 飞书侧计数不会回落")
        self.assertTrue(client.start_returned.wait(5), "start() 没返回")
        self.assertFalse(thread.is_alive(), "线程没退出")
        self.assertIsNone(feishu._ws_thread)
        self.assertIsNone(feishu._ws_client)
        self.assertIsNone(feishu._ws_loop)
        self.assertEqual(self._ws_threads(), [])

    def test_stop_ws_recovers_from_leaked_lark_lock(self):
        """lark _connect() 泄漏的 asyncio 锁不能把 _stop_ws 永久卡住。"""
        client = self._boot()
        thread = feishu._ws_thread
        ws_loop = feishu._ws_loop

        # 在 WS 线程的 loop 上造出 lark 那个泄漏状态: 锁被 acquire 之后没人 release。
        lock = asyncio.run_coroutine_threadsafe(self._make_locked(), ws_loop).result(5)
        client._lock = lock
        asyncio.run_coroutine_threadsafe(self._acquire_and_leak(lock), ws_loop).result(5)
        self.assertTrue(lock.locked(), "锁没占上, 用例没测到泄漏路径")

        real_disconnect = client._disconnect

        async def disconnect_via_lock():
            async with lock:      # 第一次会卡死在这里 → 触发强行 release
                await real_disconnect()

        client._disconnect = disconnect_via_lock

        feishu.DISCONNECT_TIMEOUT_S = 0.3   # 别让用例真等 5s
        try:
            feishu._stop_ws()
        finally:
            feishu.DISCONNECT_TIMEOUT_S = 5

        self.assertGreaterEqual(client.disconnect_calls, 1,
                                "强行释放锁之后应该重试成功")
        self.assertFalse(thread.is_alive())

    @staticmethod
    async def _make_locked() -> asyncio.Lock:
        return asyncio.Lock()

    @staticmethod
    async def _acquire_and_leak(lock: asyncio.Lock) -> None:
        # 复刻 lark _connect(): acquire 之后直接 return, 永远不 release。
        await lock.acquire()


class TestLarkLogFilter(unittest.TestCase):
    def _record(self, msg: str, args=()) -> logging.LogRecord:
        return logging.LogRecord("Lark", logging.ERROR, __file__, 1, msg, args, None)

    def test_same_template_passes_once_and_counts_rest(self):
        f = feishu._LarkLogFilter(window_s=60)
        template = "receive message loop exit, err: %s"
        passed = sum(1 for _ in range(100) if f.filter(self._record(template, ("boom",))))
        self.assertEqual(passed, 1)
        self.assertEqual(f.suppressed_total, 99)

    def test_conn_id_and_numbers_normalized_into_one_bucket(self):
        """lark 的 _fmt_log 在打日志前就把 conn_id 拼进了 msg —— 不归一化等于不限流。"""
        f = feishu._LarkLogFilter(window_s=60)
        passed = 0
        for i in range(50):
            msg = (f"receive message loop exit, err: received 3001 (registered) "
                   f"backbone registered too many conns [conn_id=deadbeef{i:04d}]")
            if f.filter(self._record(msg)):
                passed += 1
        self.assertEqual(passed, 1)
        self.assertEqual(f.suppressed_total, 49)

    def test_summary_appended_after_window(self):
        clock = [500.0]
        orig_now = feishu._now
        feishu._now = lambda: clock[0]
        try:
            f = feishu._LarkLogFilter(window_s=60)
            self.assertTrue(f.filter(self._record("ping failed")))
            for _ in range(9):
                self.assertFalse(f.filter(self._record("ping failed")))
            clock[0] += 61
            rec = self._record("ping failed")
            self.assertTrue(f.filter(rec))
            self.assertIn("同类日志已抑制 9 条", rec.msg)
        finally:
            feishu._now = orig_now

    def test_conn_limit_text_detected(self):
        for text in ("received 3001 backbone registered too many conns",
                     "the number of connections exceeded the limit",
                     "conn limit exceed",
                     "handshake failed, code: 1000040350"):
            self.assertTrue(feishu._is_conn_limit_text(text), text)
        self.assertFalse(feishu._is_conn_limit_text("connection reset by peer"))
        self.assertFalse(feishu._is_conn_limit_text(None))

    def test_unrelated_rate_limit_is_not_conn_limit(self):
        """发消息接口限流走的是同一个 "Lark" logger —— 不能把它当连接数超限,
        否则长连接会被白按进 300~1800s 退避。"""
        for text in ("request exceeded the rate limit, retry later",
                     "tenant access token quota exceeded",
                     "im.v1.message.create failed: too many requests"):
            self.assertFalse(feishu._is_conn_limit_text(text), text)

    def test_filter_flips_limit_hit_even_when_suppressed(self):
        orig = feishu._limit_hit
        try:
            feishu._limit_hit = False
            f = feishu._LarkLogFilter(window_s=60)
            msg = "receive message loop exit, err: too many conns [conn_id=x1]"
            f.filter(self._record(msg))
            feishu._limit_hit = False           # 第一条已经翻过, 手动复位
            self.assertFalse(f.filter(self._record(msg)))   # 这条被抑制
            self.assertTrue(feishu._limit_hit, "被抑制的日志也要能翻出超限标志")
        finally:
            feishu._limit_hit = orig


class TestConnectionAlive(unittest.TestCase):
    """_connection_alive 得能兜住 websockets 新旧两套连接对象。"""

    def setUp(self) -> None:
        self._orig = feishu._ws_client

    def tearDown(self) -> None:
        feishu._ws_client = self._orig

    def test_no_client_or_no_conn(self):
        feishu._ws_client = None
        self.assertFalse(feishu._connection_alive())
        feishu._ws_client = SimpleNamespace(_conn=None)
        self.assertFalse(feishu._connection_alive())

    def test_legacy_closed_flag(self):
        feishu._ws_client = SimpleNamespace(_conn=SimpleNamespace(closed=False))
        self.assertTrue(feishu._connection_alive())
        feishu._ws_client = SimpleNamespace(_conn=SimpleNamespace(closed=True))
        self.assertFalse(feishu._connection_alive())

    def test_websockets14_state_and_close_code(self):
        # websockets>=14 的 ClientConnection 没有 .closed, 只有 .state / .close_code
        open_conn = SimpleNamespace(state=SimpleNamespace(name="OPEN"), close_code=None)
        feishu._ws_client = SimpleNamespace(_conn=open_conn)
        self.assertTrue(feishu._connection_alive())

        closed_conn = SimpleNamespace(state=SimpleNamespace(name="CLOSED"), close_code=3001)
        feishu._ws_client = SimpleNamespace(_conn=closed_conn)
        self.assertFalse(feishu._connection_alive())

    def test_unknown_shape_does_not_raise(self):
        feishu._ws_client = SimpleNamespace(_conn=object())
        self.assertTrue(feishu._connection_alive())


class TestHealthShape(unittest.TestCase):
    def test_keys_present_when_nothing_running(self):
        h = feishu.health()
        for key in ("enabled", "connected", "conn_id", "uptime_s", "last_event_at",
                    "consecutive_failures", "next_retry_in_s", "suppressed_logs",
                    "thread_alive"):
            self.assertIn(key, h)

    def test_survives_broken_store(self):
        orig = feishu.store
        feishu.store = SimpleNamespace(get=lambda: (_ for _ in ()).throw(RuntimeError("nope")))
        try:
            self.assertFalse(feishu.health()["enabled"])
        finally:
            feishu.store = orig


if __name__ == "__main__":
    unittest.main()
