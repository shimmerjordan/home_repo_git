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
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Any

from ..config import store
from ..database import SessionLocal
from ..llm.client import LLMError
from ..llm.intent import execute_intent, parse_intent
from ..services.logbuffer import app_log

log = logging.getLogger("storage.feishu")

# Module state ---------------------------------------------------------------
_supervisor_task: asyncio.Task | None = None
_ws_thread: threading.Thread | None = None
_ws_client: Any = None
_api_client: Any = None
_main_loop: asyncio.AbstractEventLoop | None = None
_running_app_id: str = ""   # the app_id the current WS connection was opened with
_running_app_secret: str = ""


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


def _format_reply(result: dict[str, Any]) -> str:
    """Same Markdown shape as Telegram. Feishu's text msg_type renders plain text
    but accepts \\n line breaks; rich tables would need the 'post' or 'interactive'
    msg_types — left as a TODO if you want fancier cards."""
    from . import telegram as _tg
    return _tg._format_reply(result)


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

async def _run_intent(text: str, cfg) -> str:
    db = SessionLocal()
    try:
        try:
            out = await parse_intent(text, db, cfg)
        except LLMError as exc:
            return f"AI 出错了: {exc}"
        parsed = out["parsed"]
        # Silent execution — same policy as DingTalk/Telegram bots.
        if parsed.get("intent") in ("take_out", "put_in", "create_item"):
            parsed["confidence"] = max(parsed.get("confidence", 0.0), 1.0)
        result = execute_intent(db, text, parsed, cfg)
        return _format_reply(result)
    finally:
        db.close()


# ---- Event handler (runs on lark's thread) ---------------------------------

def _handle_message_event(data) -> None:
    """Called from the lark-oapi WebSocket thread with a P2ImMessageReceiveV1."""
    if _main_loop is None:
        return
    try:
        event = data.event
        message = event.message
        chat_id = message.chat_id
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

        sender_id = ""
        try:
            sender_id = event.sender.sender_id.open_id or ""
        except Exception:
            pass

        cfg = store.get()
        fs = cfg.feishu
        if fs.allowed_chat_ids and chat_id not in fs.allowed_chat_ids:
            log.info("feishu: chat %s not in allowlist", chat_id)
            return
        if fs.allowed_open_ids and sender_id and sender_id not in fs.allowed_open_ids:
            log.info("feishu: sender %s not in allowlist", sender_id)
            return

        app_log.info("feishu from=%s chat=%s text=%r", sender_id, chat_id, text[:120])

        # Hop to the FastAPI event loop to use async parse_intent.
        fut = asyncio.run_coroutine_threadsafe(_run_intent(text, cfg), _main_loop)
        try:
            reply = fut.result(timeout=90)
        except Exception as exc:
            reply = f"AI 出错了: {exc}"
        _send_text(chat_id, "chat_id", reply)
    except Exception as exc:
        log.exception("feishu handle: %s", exc)


# ---- WS thread lifecycle ---------------------------------------------------

def _run_ws_client(app_id: str, app_secret: str) -> None:
    """Body of the WS thread — blocks on lark's client.start()."""
    global _ws_client, _api_client
    import lark_oapi as lark
    _api_client = (lark.Client.builder()
                   .app_id(app_id).app_secret(app_secret)
                   .log_level(lark.LogLevel.WARNING)
                   .build())
    event_handler = (lark.EventDispatcherHandler.builder("", "")
                     .register_p2_im_message_receive_v1(_handle_message_event)
                     .build())
    _ws_client = lark.ws.Client(app_id, app_secret,
                                event_handler=event_handler,
                                log_level=lark.LogLevel.WARNING)
    app_log.info("feishu WS connecting (app=%s)", app_id)
    try:
        _ws_client.start()
    except Exception as exc:
        log.exception("feishu WS crashed: %s", exc)
    finally:
        app_log.info("feishu WS stopped")


def _stop_ws() -> None:
    """Best-effort stop. lark-oapi's WebSocket client doesn't expose a clean
    .stop(); we set the internal flag if present, otherwise we leave the thread
    alone — it's a daemon so process exit will reap it."""
    global _ws_client, _ws_thread, _api_client
    if _ws_client is not None:
        for attr in ("_stop", "stopped", "_should_stop"):
            try: setattr(_ws_client, attr, True)
            except Exception: pass
        try:
            close = getattr(_ws_client, "stop", None) or getattr(_ws_client, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
    _ws_client = None
    _api_client = None
    _ws_thread = None


async def _supervisor() -> None:
    """Polls config every few seconds. Starts the WS thread when feishu.enabled
    becomes true and the credentials change; stops when disabled."""
    global _ws_thread, _running_app_id, _running_app_secret
    while True:
        try:
            cfg = store.get()
            fs = cfg.feishu
            running = _ws_thread is not None and _ws_thread.is_alive()
            want = bool(fs.enabled and fs.app_id and fs.app_secret)
            creds_changed = (fs.app_id != _running_app_id or fs.app_secret != _running_app_secret)

            if want and (not running or creds_changed):
                if running and creds_changed:
                    _stop_ws()
                    await asyncio.sleep(1)
                if not _try_import():
                    app_log.warning("feishu: lark-oapi 未安装, 跳过 (pip install lark-oapi)")
                    await asyncio.sleep(30)
                    continue
                _running_app_id = fs.app_id
                _running_app_secret = fs.app_secret
                _ws_thread = threading.Thread(
                    target=_run_ws_client, args=(fs.app_id, fs.app_secret),
                    daemon=True, name="feishu-ws",
                )
                _ws_thread.start()
            elif not want and running:
                _stop_ws()
                _running_app_id = ""
                _running_app_secret = ""
        except Exception as exc:
            log.exception("feishu supervisor: %s", exc)
        await asyncio.sleep(5)


# ---- Public API ------------------------------------------------------------

def start() -> None:
    """Called once at FastAPI startup."""
    global _supervisor_task, _main_loop
    _main_loop = asyncio.get_event_loop()
    if _supervisor_task is None or _supervisor_task.done():
        _supervisor_task = asyncio.create_task(_supervisor(), name="feishu-supervisor")
        app_log.info("feishu supervisor started")


def reload() -> None:
    """Settings router calls this on every PATCH. The supervisor polls every 5s
    so it picks up changes automatically; this is a no-op kept for symmetry
    with telegram.reload()."""
    return None


def stop() -> None:
    global _supervisor_task
    if _supervisor_task and not _supervisor_task.done():
        _supervisor_task.cancel()
    _supervisor_task = None
    _stop_ws()
