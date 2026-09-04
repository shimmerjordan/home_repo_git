"""Telegram bot — long-polling worker.

Why long-polling? It is the canonical way to run a Telegram bot WITHOUT a public
IP / port-forwarding / reverse tunnel: the NAS makes an outbound HTTPS GET to
`api.telegram.org/bot<token>/getUpdates?timeout=25` which blocks server-side for
up to 25s waiting for new messages. As soon as a message arrives Telegram returns
it; we ack by sending the next offset.

The same architectural pattern (background asyncio task in the FastAPI process
making outbound HTTPS / WebSocket calls) is what Lark Stream Mode, QQ Bot
WebSocket, and Slack Socket Mode all do — only the protocol differs.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from ..config import store
from ..database import SessionLocal
from ..services import botflow
from ..services.logbuffer import app_log

log = logging.getLogger("storage.telegram")

_task: asyncio.Task | None = None
_reload_event: asyncio.Event | None = None
_last_offset: int = 0
_loop_iterations = 0


def _api_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


async def _send_message(token: str, chat_id: int, text: str) -> None:
    if not token:
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(
                _api_url(token, "sendMessage"),
                json={"chat_id": chat_id, "text": text[:4000]},
            )
    except Exception as exc:
        log.warning("telegram send failed: %s", exc)


async def _handle_update(update: dict[str, Any], cfg) -> None:
    message = update.get("message") or update.get("channel_post") or update.get("edited_message")
    if not message:
        return
    text = (message.get("text") or "").strip()
    if not text:
        return
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    from_user = message.get("from") or {}
    user_id = from_user.get("id")

    # SELF-ECHO GUARD: Telegram normally doesn't deliver the bot's own messages
    # back via getUpdates, but `from.is_bot` is a cheap defensive check —
    # ignore any bot-authored message (including our own) so we never reply to
    # a reply. The getUpdates offset is already a dedup mechanism so we don't
    # need a separate message_id cache.
    if from_user.get("is_bot"):
        log.info("telegram: skipping bot-authored message from %s", user_id)
        return

    tg_cfg = cfg.telegram
    allowed_chats = [str(x) for x in (tg_cfg.allowed_chat_ids or [])]
    allowed_users = [str(x) for x in (tg_cfg.allowed_user_ids or [])]
    if allowed_chats and str(chat_id) not in allowed_chats:
        log.info("telegram: chat %s not in whitelist", chat_id)
        return
    if allowed_users and str(user_id) not in allowed_users:
        log.info("telegram: user %s not in whitelist", user_id)
        return

    # Strip leading bot command like "/find" or "/find@my_bot".
    if text.startswith("/"):
        space = text.find(" ")
        text = text[space + 1:].strip() if space != -1 else ""
    # 群聊 privacy mode 默认开启, 用户唤起机器人的常规做法就是 "@mybot 确认"。
    # 不剥掉这个前缀, classify_reply 的整句锚定会判成 "other", botflow 会把
    # 刚存的待确认方案丢掉 —— 用户确认了一次反而要从头再来。
    text = re.sub(r"^@\w+\s*", "", text).strip()
    if not text:
        await _send_message(tg_cfg.bot_token, chat_id,
                            "怎么帮你? 试试 充电宝在哪 / 我刚拿了卷尺 / 我发烧了")
        return

    app_log.info("telegram from=%s text=%r", user_id, text[:120])

    db = SessionLocal()
    try:
        reply = await botflow.handle_bot_message(
            "telegram", str(chat_id), str(user_id), text, db, cfg)
    finally:
        db.close()
    await _send_message(tg_cfg.bot_token, chat_id, reply)


async def _polling_loop() -> None:
    """Main worker. Restartable via `reload()`."""
    global _last_offset, _loop_iterations
    while True:
        _loop_iterations += 1
        cfg = store.get()
        tg_cfg = cfg.telegram
        if not tg_cfg.enabled or not tg_cfg.bot_token:
            # 关闭态: 无 timeout 挂起, 只有 settings PATCH → reload() 才唤醒。
            # 以前是 wait_for(..., 10) 每 10s 空醒一次, 在 NAS 上是纯功耗底噪。
            await _reload_event.wait()
            _reload_event.clear()
            continue
        try:
            async with httpx.AsyncClient(timeout=35) as client:
                resp = await client.get(
                    _api_url(tg_cfg.bot_token, "getUpdates"),
                    params={"offset": _last_offset, "timeout": 25,
                            "allowed_updates": '["message","channel_post"]'},
                )
            if resp.status_code == 401:
                log.error("telegram: 401 unauthorized — token is wrong, pausing 60s")
                await asyncio.sleep(60)
                continue
            data = resp.json()
            if not data.get("ok"):
                log.warning("telegram getUpdates not ok: %s", data)
                await asyncio.sleep(10)
                continue
            for upd in data.get("result", []):
                _last_offset = max(_last_offset, int(upd["update_id"]) + 1)
                try:
                    await _handle_update(upd, cfg)
                except Exception as exc:
                    log.exception("telegram handle update: %s", exc)
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as exc:
            log.warning("telegram http: %s", exc)
            await asyncio.sleep(5)
        except Exception as exc:
            log.exception("telegram loop: %s", exc)
            await asyncio.sleep(5)


def start() -> None:
    """Called once at FastAPI startup. Safe to call again — re-uses existing task."""
    global _task, _reload_event
    if _task is None or _task.done():
        # 每次真正起新任务都建新 Event —— 若沿用旧对象, 它可能绑在上一个
        # (已关闭的) event loop 上, 下次 wait() 会抛 "attached to a different loop"
        # (测试里每个用例都是独立的 asyncio.run(), 生产里 start() 通常只调一次)。
        _reload_event = asyncio.Event()
        _task = asyncio.create_task(_polling_loop(), name="telegram-poller")
        app_log.info("telegram poller started")


def reload() -> None:
    """Bump the loop so a fresh config (new token / enabled flip) takes effect."""
    if _reload_event is not None:
        try:
            _reload_event.set()
        except RuntimeError:
            pass  # Event loop not yet running


def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
