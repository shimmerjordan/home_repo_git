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
from typing import Any

import httpx

from ..config import store
from ..database import SessionLocal
from ..llm.client import LLMError
from ..llm.intent import execute_intent, parse_intent
from ..services.logbuffer import app_log

log = logging.getLogger("storage.telegram")

_task: asyncio.Task | None = None
_reload_event: asyncio.Event | None = None
_last_offset: int = 0


def _api_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


async def _send_message(token: str, chat_id: int, text: str) -> None:
    if not token:
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(
                _api_url(token, "sendMessage"),
                json={"chat_id": chat_id, "text": text[:4000], "parse_mode": "Markdown"},
            )
    except Exception as exc:
        log.warning("telegram send failed: %s", exc)


def _format_reply(result: dict[str, Any]) -> str:
    """Plain-text + lightweight Markdown reply suitable for Telegram clients."""
    lines: list[str] = []
    if result.get("speech"):
        lines.append(result["speech"])
    cands = result.get("candidates") or []
    recs = result.get("recommendations") or []
    cm = {c["item_id"]: c for c in cands}
    if recs:
        lines.append("")
        lines.append("*推荐用品:*")
        for r in recs:
            c = cm.get(r["item_id"]) or {}
            name = c.get("item_name", f"#{r['item_id']}")
            purpose = r.get("purpose") or ""
            loc = c.get("location_path") or "未指定位置"
            lines.append(f"• *{name}* — {purpose}  _({loc})_")
    elif cands:
        lines.append("")
        lines.append("*位置:*")
        for c in cands[:10]:
            lines.append(f"• *{c['item_name']}* — _{c.get('location_path') or '未指定位置'}_")
    if result.get("executed"):
        lines.append("")
        lines.append("✅ 已记录")
    return "\n".join(lines).strip() or "（无内容）"


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
    if not text:
        await _send_message(tg_cfg.bot_token, chat_id,
                            "怎么帮你? 试试 `充电宝在哪` / `我刚拿了卷尺` / `我发烧了`")
        return

    app_log.info("telegram from=%s text=%r", user_id, text[:120])

    db = SessionLocal()
    try:
        try:
            out = await parse_intent(text, db, cfg)
        except LLMError as exc:
            await _send_message(tg_cfg.bot_token, chat_id, f"AI 出错了: {exc}")
            return
        parsed = out["parsed"]
        # Silent execution — DingTalk and Telegram share the policy: no UI to
        # confirm, so force-execute mutating intents that the LLM picked.
        if parsed.get("intent") in ("take_out", "put_in", "consume", "create_item"):
            parsed["confidence"] = max(parsed.get("confidence", 0.0), 1.0)
        result = execute_intent(db, text, parsed, cfg)
        await _send_message(tg_cfg.bot_token, chat_id, _format_reply(result))
    finally:
        db.close()


async def _polling_loop() -> None:
    """Main worker. Restartable via `reload()`."""
    global _last_offset
    while True:
        cfg = store.get()
        tg_cfg = cfg.telegram
        if not tg_cfg.enabled or not tg_cfg.bot_token:
            # Sleep with cancellation via reload event so save-to-enable wakes us up.
            try:
                await asyncio.wait_for(_reload_event.wait(), timeout=10)
                _reload_event.clear()
            except asyncio.TimeoutError:
                pass
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
    if _reload_event is None:
        _reload_event = asyncio.Event()
    if _task is None or _task.done():
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
