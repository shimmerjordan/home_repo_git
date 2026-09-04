"""Feishu (Lark) bot — Stream Mode (WebSocket, outbound only).

Architecture mirror of services/telegram.py but using Feishu's long-connection
protocol via the official `lark-oapi` SDK. NAS opens an outbound WSS to
open.feishu.cn — no public IP / no port-forwarding / no reverse tunnel required.

The lark-oapi WebSocket client (`lark.ws.Client.start()`) BLOCKS its calling
thread, so we run it in a daemon thread and bridge its sync event callback back
to the FastAPI event loop via `asyncio.run_coroutine_threadsafe` (so we can use
the existing async `parse_intent`).

Optional dependency: if `lark-oapi` is not installed we log a warning and the
Feishu feature stays disabled — old container images don't break.

重连主权在我们手里, 不在 lark 手里 (lark-oapi 1.4.15 的坑, 已核对源码):
  * `Client.start()` 结尾是 `loop.run_until_complete(_select())`, 而 `_select()`
    是 `while True: await asyncio.sleep(3600)` —— 线程永不退出。所以 "线程还活着"
    不能当成 "连接还活着", 判死必须看 `client._conn`。
  * `_try_connect()` 遇到 `ClientException` (连接数超限就属于这类) 会往上抛, 穿过
    `_reconnect()` / `_receive_message_loop()` 的 except 变成没人接的 task 异常,
    内部重连链就此断掉, 线程却仍 alive —— 这就是 "跑久了卡死"。
  * `Client._connect()` 在 `await self._lock.acquire()` 之后遇到
    `if self._conn is not None: return` 直接返回, 不 release; 之后
    `_disconnect()` / `_write_message()` 会永久阻塞在 acquire 上。
  * lark 的 `loop` 是 import 期绑定的模块级全局, 所有 Client 实例共用。第二条 WS
    线程里 `start()` 调 `loop.run_until_complete()` 时该 loop 正被上一条线程跑着,
    直接 `RuntimeError: This event loop is already running`。
所以: `auto_reconnect=False` 关掉 lark 的内部重连, 线程里自建 loop 并覆写
`lark_oapi.ws.client.loop`, 由本模块的 supervisor 负责判死 / 退避 / 重启。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from datetime import datetime
from typing import Any

from ..config import store
from ..database import SessionLocal
from ..services import botflow
from ..services.logbuffer import app_log

log = logging.getLogger("storage.feishu")

# Silence noisy 3rd-party DEBUG loggers — the lark WS handshake + websockets
# frame logs flood the app-log ring buffer and slow every API request when the
# logs panel is open. We keep WARNING+ from those libraries.
# 注意 lark-oapi 的 logger 名是 "Lark" (大写 L, 见 lark_oapi/core/log.py),
# logger 名大小写敏感, 所以下面那些 "lark_oapi*" / "lark*" 其实一个都没命中过。
for _name in ("websockets", "websockets.client", "websockets.protocol",
              "urllib3", "urllib3.connectionpool",
              "lark_oapi", "lark_oapi.ws", "lark_oapi.ws.client",
              "lark", "lark.ws", "Lark"):
    try:
        logging.getLogger(_name).setLevel(logging.WARNING)
    except Exception:
        pass

# 调参常量 (测试里会改小 / 替换掉) ------------------------------------------
POLL_INTERVAL_S = 15      # supervisor 轮询间隔
DEAD_AFTER_S = 90         # 连接连续不健康超过这么久才判死, 避免抖一下就重连
HEALTHY_RESET_S = 300     # 连续健康这么久就认为上一轮失败已经过去, 清零退避
JOIN_TIMEOUT_S = 10       # 停线程时等它退出的上限
DISCONNECT_TIMEOUT_S = 5  # 等 client._disconnect() 的上限
LOG_SUPPRESS_WINDOW_S = 60  # 同一条日志模板的放行窗口
# 进程退出时的停机预算, 必须明显小于 docker 的 SIGTERM 宽限期 (compose 没配
# stop_grace_period, 默认 10s)。用 JOIN_TIMEOUT_S 那套会阻塞 17s → 直接吃 SIGKILL,
# 连接反而关不掉, 而且排在后面的 backup.stop() 也跑不到。
SHUTDOWN_DISCONNECT_S = 2
SHUTDOWN_JOIN_S = 2
# lark `_get_conn_url()` 里的 requests.post 不带 timeout, 见 _RequestsWithTimeout。
CONN_URL_TIMEOUT_S = 15

# 普通失败的退避序列。
_BACKOFF_NORMAL = (30, 60, 120, 300)
# 连接数超限专属长退避: 飞书侧的连接计数要等服务端超时才回落, 30s 就重试等于
# 一直把自己焊在上限上, 3001 会刷到天荒地老。
_BACKOFF_CONN_LIMIT = (300, 600, 1200, 1800)


def _now() -> float:
    """单调时钟。抽成函数是为了让测试能整个替换掉, 不用真的 sleep。"""
    return time.monotonic()


# Module state ---------------------------------------------------------------
_supervisor_task: asyncio.Task | None = None
_ws_thread: threading.Thread | None = None
_ws_client: Any = None
_ws_loop: asyncio.AbstractEventLoop | None = None   # WS 线程自建的 loop, 停止时要用
_api_client: Any = None
_main_loop: asyncio.AbstractEventLoop | None = None
_running_app_id: str = ""   # the app_id the current WS connection was opened with
_running_app_secret: str = ""
_reload_event: asyncio.Event | None = None
_loop_iterations = 0
_last_thread_start: float = 0.0   # monotonic seconds; gates the reconnect backoff
_consecutive_failures: int = 0    # bumps on每次启动, reset on long-lived connection
_limit_hit: bool = False          # 上一次失败是 "连接数超限" → 走长退避
_unhealthy_since: float | None = None   # 连接首次被观察到不健康的时刻
_connected_since: float | None = None   # 连接首次被观察到健康的时刻 (算 uptime)
_last_event_at: float = 0.0       # epoch 秒; 仅诊断展示, 不参与判死
_stop_requested: bool = False     # 我们主动停的 → 线程里的 RuntimeError 不算崩溃

# "停旧 + 起新" 必须整段互斥: 任何时刻最多一条 feishu-ws 线程。
_lifecycle_lock = threading.Lock()

# Message-id dedup. Feishu (or our own reconnect) can occasionally redeliver the
# same event; without this guard one user @mention can spawn multiple replies →
# user sees "infinite loop" replies. LRU-ish: keep last 200 ids.
from collections import OrderedDict
_seen_message_ids: OrderedDict[str, float] = OrderedDict()
_DEDUP_MAX = 200
_DEDUP_TTL_S = 600  # 10 minutes

def _already_seen(message_id: str) -> bool:
    """Return True if we've processed this Feishu message id within the TTL."""
    if not message_id:
        return False
    now = _now()
    # Drop expired entries (cheap because OrderedDict is roughly insertion order).
    while _seen_message_ids:
        oldest_id, ts = next(iter(_seen_message_ids.items()))
        if now - ts > _DEDUP_TTL_S:
            _seen_message_ids.pop(oldest_id, None)
        else:
            break
    if message_id in _seen_message_ids:
        return True
    _seen_message_ids[message_id] = now
    if len(_seen_message_ids) > _DEDUP_MAX:
        _seen_message_ids.popitem(last=False)
    return False


def _try_import():
    """Lazy import so an old image without lark-oapi can still start up."""
    try:
        import lark_oapi as lark  # noqa: F401
        from lark_oapi.api.im.v1 import (  # noqa: F401
            CreateMessageRequest, CreateMessageRequestBody, P2ImMessageReceiveV1,
        )
        return True
    except ImportError:
        return False


# ---- 日志止血 --------------------------------------------------------------

# conn_id / 长数字串 / 长 hex 串每次重连都变, 不抹掉就等于没限流。
_VOLATILE_RE = re.compile(r"\[conn_id=[^\]]*\]|\b[0-9a-f]{8,}\b|\d{3,}")

# "连接数超限" 的文本特征。异常和日志都要认: websockets>=14 抛的是 InvalidStatus
# 而 lark 只 except InvalidStatusCode, 于是超限根本走不到它的 ClientException 分支,
# 只剩 3001 关闭时 _receive_message_loop 打的那行日志能认出来。
# 必须带上 conn 的上下文: 光认 "exceed" 会把发消息的接口限流 (同一个 "Lark" logger)
# 也当成连接数超限, 把长连接白白按进 30 分钟退避。
_CONN_LIMIT_RE = re.compile(
    r"too many conns"                       # 3001 关闭原因里的原话
    r"|conn(?:ection)?s?[^.,;]{0,40}exceed"  # "the number of connections exceeded ..."
    r"|exceed[^.,;]{0,40}conn"               # 反过来的语序
    r"|1000040350",                          # lark 的 EXCEED_CONN_LIMIT
    re.I)


def _is_conn_limit_text(text: Any) -> bool:
    return bool(_CONN_LIMIT_RE.search(str(text or "")))


class _LarkLogFilter(logging.Filter):
    """按日志模板限流 lark 的刷屏, 顺手识别 "连接数超限"。

    飞书侧连接数顶上限时 lark 会以每秒好几条的速度打
    "receive message loop exit, err: received 3001 ... too many conns"。
    logbuffer 只有 5000 条环, 几分钟就被打穿, 之后每个 /api/logs 请求都要序列化
    一大坨没用的文本 —— 这是拖慢整个后端的实际原因, 所以限流是必须的。

    key 取 record.msg (模板) 而不是 record.getMessage(): 前者不用做参数格式化,
    在刷屏路径上更便宜。lark 的 _fmt_log 会先把 conn_id 拼进 msg, 所以还要用
    _VOLATILE_RE 把易变部分抹掉才归得成同一类。
    """

    def __init__(self, window_s: float | None = None) -> None:
        super().__init__()
        self.window_s = LOG_SUPPRESS_WINDOW_S if window_s is None else window_s
        self._lock = threading.Lock()
        self._last_pass: dict[str, float] = {}
        self._pending: dict[str, int] = {}
        self.suppressed_total = 0

    @staticmethod
    def _key(record: logging.LogRecord) -> str:
        return _VOLATILE_RE.sub("#", str(record.msg))[:200]

    def filter(self, record: logging.LogRecord) -> bool:
        global _limit_hit
        key = self._key(record)
        if not _limit_hit and _is_conn_limit_text(record.msg):
            # 在限流判定之前认: 被抑制掉的那些也得能翻出长退避的标志位。
            _limit_hit = True
        now = _now()
        with self._lock:
            if len(self._last_pass) > 500:
                # 归一化之后模板数应该只有十几个; 真的涨到这个量说明有没抹干净的
                # 易变字段, 清空好过在长跑进程里慢慢漏。
                self._last_pass.clear()
                self._pending.clear()
            last = self._last_pass.get(key)
            if last is not None and now - last < self.window_s:
                self._pending[key] = self._pending.get(key, 0) + 1
                self.suppressed_total += 1
                return False
            self._last_pass[key] = now
            dropped = self._pending.pop(key, 0)
        if dropped:
            # 窗口末尾补一条汇总, 免得看日志的人以为世界很安静。
            record.msg = f"{record.msg} (同类日志已抑制 {dropped} 条)"
        return True


_lark_log_filter = _LarkLogFilter()
for _name in ("Lark", "lark", "lark_oapi"):
    try:
        _lg = logging.getLogger(_name)
        if _lark_log_filter not in _lg.filters:
            _lg.addFilter(_lark_log_filter)
    except Exception:
        pass


# ---- Send replies ----------------------------------------------------------

def _send_text(receive_id: str, receive_id_type: str, text: str) -> None:
    if _api_client is None:
        return
    try:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        req = (CreateMessageRequest.builder()
               .receive_id_type(receive_id_type)
               .request_body(CreateMessageRequestBody.builder()
                             .receive_id(receive_id)
                             .msg_type("text")
                             .content(json.dumps({"text": text[:4000]}))
                             .build())
               .build())
        resp = _api_client.im.v1.message.create(req)
        if not getattr(resp, "success", lambda: True)():
            log.warning("feishu send not ok: code=%s msg=%s",
                        getattr(resp, "code", "?"), getattr(resp, "msg", "?"))
    except Exception as exc:
        log.warning("feishu send failed: %s", exc)


# ---- Async pipeline (runs on the FastAPI loop) -----------------------------

# ---- Event handler (runs on lark's thread) ---------------------------------

def _handle_message_event(data) -> None:
    """Called from the lark-oapi WebSocket thread with a P2ImMessageReceiveV1.

    Must return FAST. lark runs its WebSocket ping/pong on the same thread, and
    any blocking work here will eventually trigger a ping_timeout (~30s) and
    force a reconnect — which hits feishu's per-app rate limit if it happens
    several times in a row. So: extract + filter here, fire-and-forget the
    LLM + reply work onto the main FastAPI event loop, return immediately.
    """
    global _last_event_at
    # 只做诊断展示。群里长时间没人说话是完全正常的, 拿它判死会误杀。
    _last_event_at = time.time()
    if _main_loop is None:
        return
    try:
        event = data.event
        message = event.message
        chat_id = message.chat_id
        message_id = getattr(message, "message_id", "") or ""

        # 1. SELF-ECHO GUARD: skip messages where the sender is an app/bot. Our
        #    own outgoing replies normally don't trigger receive_v1, but lark
        #    has occasionally fired duplicate events during reconnect — a stray
        #    bot-authored message must NEVER trigger another reply or we get
        #    the infinite-reply loop the user reported.
        sender_type = ""
        sender_id = ""
        try:
            sender_type = getattr(event.sender, "sender_type", "") or ""
            sender_id = event.sender.sender_id.open_id or ""
        except Exception:
            pass
        if sender_type and sender_type.lower() in ("app", "bot"):
            log.info("feishu: skipping message from app/bot sender (%s)", sender_id)
            return

        # 2. DEDUP: same message_id within TTL → already replied, drop.
        if _already_seen(message_id):
            log.info("feishu: duplicate message_id %s, dropping", message_id)
            return

        content_raw = message.content or "{}"
        try:
            content = json.loads(content_raw)
        except Exception:
            content = {}
        text = (content.get("text") or "").strip()
        # In group chats Feishu inserts @-mention placeholders like "@_user_1" —
        # drop them so the LLM sees plain natural language.
        text = re.sub(r"@_user_\d+", "", text).strip()
        if not text:
            return

        cfg = store.get()
        fs = cfg.feishu
        if fs.allowed_chat_ids and chat_id not in fs.allowed_chat_ids:
            log.info("feishu: chat %s not in allowlist", chat_id)
            return
        if fs.allowed_open_ids and sender_id and sender_id not in fs.allowed_open_ids:
            log.info("feishu: sender %s not in allowlist", sender_id)
            return

        app_log.info("feishu from=%s chat=%s text=%r", sender_id, chat_id, text[:120])

        # FIRE-AND-FORGET: schedule the LLM + reply work on the main loop and
        # return so the WS thread can keep the heartbeat going.
        asyncio.run_coroutine_threadsafe(_handle_async(text, chat_id, sender_id, cfg), _main_loop)
    except Exception as exc:
        log.exception("feishu handle: %s", exc)


async def _handle_async(text: str, chat_id: str, sender_id: str, cfg) -> None:
    """Runs on the FastAPI main loop. Does the LLM call, then sends the reply
    via the lark SDK in an executor thread (the SDK is sync)."""
    try:
        db = SessionLocal()
        try:
            reply = await botflow.handle_bot_message(
                "feishu", chat_id, sender_id, text, db, cfg)
        finally:
            db.close()
    except Exception as exc:
        log.exception("feishu intent: %s", exc)
        reply = f"AI 出错了: {exc}"
    try:
        # _send_text is sync (lark SDK) — run in executor so we don't block the loop.
        await asyncio.get_event_loop().run_in_executor(
            None, _send_text, chat_id, "chat_id", reply,
        )
    except Exception as exc:
        log.warning("feishu send_text failed: %s", exc)


# ---- WS thread lifecycle ---------------------------------------------------

class _RequestsWithTimeout:
    """给 lark 的 `requests.post` 强塞一个超时。

    lark `_get_conn_url()` 是 `requests.post(...)` 不带 timeout 的同步调用, 而它跑在
    WS 线程的 loop 里。网络半死时 (正好是要重连的时候) 这一下能挂住几十分钟: loop
    被同步调用堵死 → call_soon_threadsafe(loop.stop) 和 _disconnect 都排不上 →
    _stop_ws 只能 join 超时放弃 → 那条线程带着一个我们关不掉的连接一直躺着。
    这是 "线程停不掉" 的唯一已知成因, 从源头掐掉比事后补救便宜。
    """

    def __init__(self, real: Any, timeout: float) -> None:
        self._real = real
        self._timeout = timeout

    def post(self, *args, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return self._real.post(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _build_clients(app_id: str, app_secret: str, ws_loop: asyncio.AbstractEventLoop):
    """建 lark 的 HTTP client 和 WS client, 返回 (api_client, ws_client)。

    单独抽成模块级函数是为了让测试能整段替换, 不用真连 open.feishu.cn。
    """
    import lark_oapi as lark
    import lark_oapi.ws.client as _lws

    # 必须在 new Client 之前覆写: lark 的 loop 是 import 期绑定的模块全局, 所有
    # Client 实例共用它。不覆写的话第二条 WS 线程里 start() 会撞上
    # "This event loop is already running" (第一条线程正在跑同一个 loop)。
    _lws.loop = ws_loop
    if not isinstance(getattr(_lws, "requests", None), _RequestsWithTimeout):
        _lws.requests = _RequestsWithTimeout(_lws.requests, CONN_URL_TIMEOUT_S)

    api_client = (lark.Client.builder()
                  .app_id(app_id).app_secret(app_secret)
                  .log_level(lark.LogLevel.WARNING)
                  .build())
    event_handler = (lark.EventDispatcherHandler.builder("", "")
                     .register_p2_im_message_receive_v1(_handle_message_event)
                     .build())
    # auto_reconnect=False: lark 的内部重连遇到 ClientException 会把异常抛进
    # 没人接的 task, 重连链直接断掉而线程还活着 —— 重连交给我们的 supervisor。
    ws_client = lark.ws.Client(app_id, app_secret,
                               event_handler=event_handler,
                               log_level=lark.LogLevel.WARNING,
                               auto_reconnect=False)
    return api_client, ws_client


def _run_ws_client(app_id: str, app_secret: str) -> None:
    """Body of the WS thread — blocks on lark's client.start().

    CRITICAL: uvicorn installs **uvloop** as the global event-loop policy. uvloop
    has thread-affinity quirks: even a freshly-`asyncio.new_event_loop()`-created
    uvloop instance reports "this event loop is already running" when lark's
    `loop.run_until_complete()` is called from a worker thread. Workaround: build
    a vanilla **stdlib** SelectorEventLoop via the default policy, bypassing
    whatever global policy uvicorn set, and pin it to this thread.
    """
    global _ws_client, _api_client, _ws_loop, _limit_hit
    ws_loop = asyncio.DefaultEventLoopPolicy().new_event_loop()
    asyncio.set_event_loop(ws_loop)
    _ws_loop = ws_loop
    started_at = _now()
    try:
        _api_client, _ws_client = _build_clients(app_id, app_secret, ws_loop)
        app_log.info("feishu WS connecting (app=%s)", app_id)
        _ws_client.start()   # 阻塞: 内部是 loop.run_until_complete(_select())
    except Exception as exc:
        if _stop_requested:
            # _stop_ws() 里的 loop.stop() 会让 run_until_complete 抛
            # "Event loop stopped before Future completed" —— 这是预期的收尾, 不是崩溃。
            log.info("feishu WS 线程按请求退出: %s", exc)
        elif _is_conn_limit_text(exc):
            _limit_hit = True
            app_log.warning("feishu WS 连接数超限, 转入长退避: %s", exc)
        else:
            log.exception("feishu WS crashed: %s", exc)
    finally:
        app_log.info("feishu WS stopped (uptime=%.0fs)", _now() - started_at)
        try:
            # 收尾期间必然会有 "Task was destroyed but it is pending" 和
            # "Task exception was never retrieved" (auto_reconnect=False 之后
            # _receive_message_loop 的 raise 就是没人接的)。它们都走
            # loop.call_exception_handler, 默认实现按 ERROR 带 traceback 打进
            # logbuffer —— 收尾阶段的这些没有信息量, 只会挤掉真正有用的日志。
            # 注意 cancel() 单独用是没用的: loop 已经停了, 取消永远轮不到执行。
            ws_loop.set_exception_handler(lambda _loop, _ctx: None)
        except Exception:
            pass
        try:
            for task in asyncio.all_tasks(ws_loop):
                task.cancel()
        except Exception:
            pass
        try:
            ws_loop.close()
        except Exception:
            pass


def _force_release_lock(client: Any, ws_loop: asyncio.AbstractEventLoop) -> None:
    """绕过 lark `_connect()` 的锁泄漏: 它 acquire 之后走 `if self._conn is not
    None: return` 就再也不 release, 于是 `_disconnect()` 永久卡在 acquire 上。
    只在 _disconnect 已经超时之后才调 —— 正常持锁的瞬间不能乱抢。"""
    lock = getattr(client, "_lock", None)
    if lock is None or not getattr(lock, "locked", lambda: False)():
        return
    def _release() -> None:
        try:
            lock.release()
        except Exception:
            pass
    try:
        # asyncio.Lock 不是线程安全的, release 必须回到它自己的 loop 上做。
        ws_loop.call_soon_threadsafe(_release)
        log.warning("feishu: 强行释放 lark 泄漏的 asyncio 锁")
    except Exception:
        pass


def _disconnect_blocking(client: Any, ws_loop: asyncio.AbstractEventLoop,
                         timeout: float) -> bool:
    """在 WS 线程的 loop 上跑 client._disconnect() 并等它完成。"""
    try:
        if ws_loop.is_closed():
            return False
        fut = asyncio.run_coroutine_threadsafe(client._disconnect(), ws_loop)
    except Exception as exc:
        log.info("feishu: 无法调度 _disconnect (%s)", exc)
        return False
    try:
        fut.result(timeout)
        return True
    except Exception:
        return False


def _stop_ws(disconnect_timeout: float | None = None,
             join_timeout: float | None = None) -> bool:
    """真正把 WS 停掉。返回 True 表示确认停干净了。调用方要么持 _lifecycle_lock,
    要么是 stop()。

    老实现只是 setattr 几个 lark 根本没有的属性 (_stop / stopped / _should_stop)
    再找不存在的 stop()/close(), 然后把 _ws_thread 置 None —— 等于什么都没停, 只是
    丢掉了对旧线程的引用。supervisor 从此认为 "没在跑" 就再起一条, 于是一堆 Client
    各自重连, 飞书侧连接数顶爆 → 握手完立刻 3001 → 再重连。必须真停。

    join 超时时**保留**所有引用并返回 False: 那条线程还攥着一个我们关不掉的连接,
    把引用清成 None 等于又变回 "丢引用当停掉", 下一轮既停不了它也看不见它。
    """
    global _ws_client, _api_client, _ws_thread, _ws_loop
    global _connected_since, _unhealthy_since, _stop_requested
    client, thread, ws_loop = _ws_client, _ws_thread, _ws_loop
    dt = DISCONNECT_TIMEOUT_S if disconnect_timeout is None else disconnect_timeout
    jt = JOIN_TIMEOUT_S if join_timeout is None else join_timeout
    _stop_requested = True

    # 1. 先把连接干净关掉, 让飞书侧的连接计数尽快回落。直接 loop.stop() 会把 TCP
    #    连接扔给 GC, 计数得等服务端超时才减 —— 那期间重连一样会吃 3001。
    if client is not None and ws_loop is not None:
        if not _disconnect_blocking(client, ws_loop, dt):
            _force_release_lock(client, ws_loop)
            _disconnect_blocking(client, ws_loop, min(2.0, dt))

    # 2. 停 loop → run_until_complete(_select()) 返回 → start() 返回 → 线程自己退。
    if ws_loop is not None:
        try:
            if not ws_loop.is_closed():
                ws_loop.call_soon_threadsafe(ws_loop.stop)
        except Exception:
            pass

    # 3. 等线程真的走掉再清引用 —— 顺序反了就又变成 "丢引用当停掉"。
    if thread is not None and thread.is_alive():
        thread.join(timeout=jt)
        if thread.is_alive():
            log.warning("feishu: WS 线程 %.0fs 内没退出, 引用保留, 下一轮再停", jt)
            return False

    _ws_client = None
    _api_client = None
    _ws_thread = None
    _ws_loop = None
    _connected_since = None
    _unhealthy_since = None
    return True


def _stop_ws_locked() -> bool:
    """给 supervisor 用的带锁版本 —— 停的过程中不能有人在起新线程。"""
    with _lifecycle_lock:
        return _stop_ws()


def _restart_ws(app_id: str, app_secret: str) -> bool:
    """停旧 + 起新, 整段串在 _lifecycle_lock 里。返回是否真起了新线程。

    单飞是这个模块的核心不变量: 任何时刻最多一条名为 feishu-ws 的线程。
    """
    global _ws_thread, _last_thread_start, _stop_requested
    with _lifecycle_lock:
        # 无论起没起都算一次尝试: 退避得能把 "停不掉旧线程" 的重试也节流住,
        # 否则每 POLL_INTERVAL_S 就白占一条 executor 线程 join 十几秒。
        _last_thread_start = _now()
        if not _stop_ws():
            # 旧线程还活着。这时候起第二条是自杀: _build_clients 会把
            # lark_oapi.ws.client.loop 这个模块全局改成新 loop, 而 lark 的
            # start()/_connect() 是执行时才读那个全局的 —— 旧线程一醒过来就会拿
            # 新 loop 去 run_until_complete / create_task, 结果旧连接永远关不掉,
            # 飞书侧连接数只涨不落 → 3001 雪崩。宁可这一轮不连。
            app_log.warning("feishu: 旧 WS 线程还没停掉, 本轮不起新线程 (避免双连接)")
            return False
        _stop_requested = False
        thread = threading.Thread(target=_run_ws_client, args=(app_id, app_secret),
                                 daemon=True, name="feishu-ws")
        _ws_thread = thread
        thread.start()
        return True


def _connection_alive() -> bool:
    """存活判据是 "连接活着", 不是 "线程活着" —— lark 的线程永不退出。

    用 getattr 层层兜底: lark 的内部结构和 websockets 的连接对象都可能变
    (websockets>=14 的 ClientConnection 已经没有 .closed, 只有 .state/.close_code)。
    """
    client = _ws_client
    if client is None:
        return False
    conn = getattr(client, "_conn", None)
    if conn is None:
        return False
    closed = getattr(conn, "closed", None)
    if closed is not None:
        return not closed
    if getattr(conn, "close_code", None) is not None:
        return False
    state_name = getattr(getattr(conn, "state", None), "name", "")
    return state_name in ("", "OPEN", "CONNECTING")


def _backoff_s() -> float:
    """当前该等多久再重连。连接数超限走 300/600/1200/1800, 其余 30/60/120/300。"""
    if _consecutive_failures <= 0:
        return 0.0
    seq = _BACKOFF_CONN_LIMIT if _limit_hit else _BACKOFF_NORMAL
    return float(seq[min(_consecutive_failures - 1, len(seq) - 1)])


async def _in_thread(fn, *args):
    """把可能阻塞 10s 的 停/起 线程动作挪出事件循环, 别卡住所有 API。

    整段 停+起 在同一个 worker 里跑完 (见 _restart_ws), 所以不存在 "持着
    threading.Lock 去 await" 那种被 cancel 之后锁再也放不掉的死锁。
    """
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


async def _supervise_once() -> None:
    """supervisor 的单轮逻辑。抽出来是为了能直接调、不用等 sleep。"""
    global _running_app_id, _running_app_secret, _last_thread_start
    global _consecutive_failures, _limit_hit, _unhealthy_since, _connected_since

    cfg = store.get()
    fs = cfg.feishu
    want = bool(fs.enabled and fs.app_id and fs.app_secret)
    creds_changed = (fs.app_id != _running_app_id or fs.app_secret != _running_app_secret)
    thread = _ws_thread
    thread_alive = thread is not None and thread.is_alive()
    alive = _connection_alive()

    if not want:
        if thread is not None or _ws_client is not None:
            if await _in_thread(_stop_ws_locked):
                app_log.info("feishu: 已关闭, WS 线程已停止")
            # 停失败时不报 "已停止", 引用还留着, 下一轮会再试一次。
        _running_app_id = ""
        _running_app_secret = ""
        _consecutive_failures = 0
        _limit_hit = False
        return

    if alive and thread_alive and not creds_changed:
        _unhealthy_since = None
        if _connected_since is None:
            _connected_since = _now()
            app_log.info("feishu WS 已连接 (conn_id=%s)",
                         getattr(_ws_client, "_conn_id", "") or "?")
        # 撑住 HEALTHY_RESET_S 就认为上一轮失败翻篇了, 清零退避和超限标志。
        if (_consecutive_failures or _limit_hit) and _now() - _connected_since > HEALTHY_RESET_S:
            _consecutive_failures = 0
            _limit_hit = False
        return

    if thread_alive and not creds_changed:
        # 线程活着但连接不健康。auto_reconnect=False 之后 _receive_message_loop
        # 的异常没人接, 内部重连链已断, 这条线程不会自己好起来 —— 但先给
        # DEAD_AFTER_S 的宽限, 免得网络抖一下就重连吃飞书的连接数限制。
        if _unhealthy_since is None:
            _unhealthy_since = _now()
            return
        if _now() - _unhealthy_since < DEAD_AFTER_S:
            return
        app_log.warning("feishu: 连接已死 %.0fs (线程仍在), 重启 WS",
                        _now() - _unhealthy_since)
    # 线程已经没了的情况不用等宽限期: 那是确定性的死, 直接进退避判断。

    backoff = _backoff_s()
    elapsed = _now() - _last_thread_start
    if _last_thread_start and elapsed < backoff:
        return  # 静默等待, 不刷日志
    if not _try_import():
        app_log.warning("feishu: lark-oapi 未安装, pip install lark-oapi")
        return

    _running_app_id = fs.app_id
    _running_app_secret = fs.app_secret
    _consecutive_failures += 1
    if _consecutive_failures > 1:
        app_log.warning("feishu: 第 %d 次尝试连接 (上一次失败, 下次退避 %.0fs)",
                        _consecutive_failures, _backoff_s())
    if await _in_thread(_restart_ws, fs.app_id, fs.app_secret):
        # 只有真起了新线程才清 _unhealthy_since; 起失败 (旧线程没停掉) 时留着,
        # 免得下一轮又从头数 DEAD_AFTER_S 的宽限期。
        _unhealthy_since = None


def _is_idle() -> bool:
    """关闭且 WS 已完全停掉 —— 这时轮询没有任何事可做。"""
    try:
        fs = store.get().feishu
        want = bool(fs.enabled and fs.app_id and fs.app_secret)
    except Exception:
        want = False
    return (not want) and _ws_thread is None and _ws_client is None


async def _supervisor() -> None:
    """只剩循环 + 等待, 真正的判断都在 _supervise_once 里。
    开启态每 POLL_INTERVAL_S 巡检一次; 关闭态无 timeout 挂起, 由 reload() 唤醒。"""
    global _loop_iterations
    while True:
        _loop_iterations += 1
        try:
            await _supervise_once()
        except Exception as exc:
            log.exception("feishu supervisor: %s", exc)
        try:
            if _is_idle():
                await _reload_event.wait()
            else:
                await asyncio.wait_for(_reload_event.wait(), timeout=POLL_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
        _reload_event.clear()


# ---- Public API ------------------------------------------------------------

def health() -> dict:
    """给 /api/diag 用的健康快照。任何字段都不能因为 lark 内部结构变化而抛异常。"""
    try:
        fs = store.get().feishu
        enabled = bool(fs.enabled and fs.app_id and fs.app_secret)
    except Exception:
        enabled = False
    alive = _connection_alive()
    thread = _ws_thread
    uptime = 0.0
    if alive and _connected_since is not None:
        uptime = round(_now() - _connected_since, 1)
    next_retry = 0.0
    if enabled and not alive and _last_thread_start:
        next_retry = round(max(0.0, _backoff_s() - (_now() - _last_thread_start)), 1)
    return {
        "enabled": enabled,
        "connected": alive,
        "conn_id": getattr(_ws_client, "_conn_id", "") or "",
        "uptime_s": uptime,
        "last_event_at": (datetime.fromtimestamp(_last_event_at).isoformat(timespec="seconds")
                          if _last_event_at else None),
        "consecutive_failures": _consecutive_failures,
        "next_retry_in_s": next_retry,
        "suppressed_logs": _lark_log_filter.suppressed_total,
        "thread_alive": bool(thread is not None and thread.is_alive()),
    }


def start() -> None:
    """Called once at FastAPI startup."""
    global _supervisor_task, _main_loop, _reload_event
    _main_loop = asyncio.get_event_loop()
    if _supervisor_task is None or _supervisor_task.done():
        # 每次真正起新任务都建新 Event —— 若沿用旧对象, 它可能绑在上一个
        # (已关闭的) event loop 上, 下次 wait() 会抛 "attached to a different loop"
        # (测试里每个用例都是独立的 asyncio.run(), 生产里 start() 通常只调一次)。
        _reload_event = asyncio.Event()
        _supervisor_task = asyncio.create_task(_supervisor(), name="feishu-supervisor")
        app_log.info("feishu supervisor started")


def reload() -> None:
    """Settings router calls this on every PATCH. Reset the failure counter so
    a deliberate config change isn't blocked by the previous backoff window."""
    global _consecutive_failures, _last_thread_start, _limit_hit
    _consecutive_failures = 0
    _last_thread_start = 0.0
    _limit_hit = False
    if _reload_event is not None:
        try:
            _reload_event.set()
        except RuntimeError:
            pass


def stop() -> None:
    """FastAPI shutdown 里**同步**调的, 所以预算必须小: 它一卡就是卡整个事件循环,
    docker 那 10s 宽限期一到就是 SIGKILL, 连接反而关不成, 后面的 backup.stop()
    也跑不到。健康情况下 disconnect 是毫秒级, 2+2s 足够。"""
    global _supervisor_task
    if _supervisor_task and not _supervisor_task.done():
        _supervisor_task.cancel()
    _supervisor_task = None
    with _lifecycle_lock:
        _stop_ws(disconnect_timeout=SHUTDOWN_DISCONNECT_S,
                 join_timeout=SHUTDOWN_JOIN_S)
