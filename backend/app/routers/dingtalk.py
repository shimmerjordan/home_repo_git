"""DingTalk (钉钉) bot webhook.

DingTalk's custom-bot model: user @s the bot in a group → DingTalk's server POSTs
a JSON event to a URL we configure. We verify the HMAC-SHA256 signature, hand the
text to the voice-intent pipeline in SILENT mode (no UI confirm prompts), and
return a DingTalk-shaped response — text or markdown table.

Docs:
- 自定义机器人接收消息 https://open.dingtalk.com/document/orgapp/receive-message
- 自定义机器人安全设置 加签算法 https://open.dingtalk.com/document/orgapp/customize-robot-security-settings

Public reachability: DingTalk's servers must be able to POST to /api/dingtalk/webhook.
Inside a home LAN that means port-forwarding 8443/tcp, or fronting with frp /
cloudflared / a reverse proxy with a real cert.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..config import store
from ..database import get_db
from ..llm.client import LLMError
from ..llm.intent import execute_intent, parse_intent
from ..services.inventory import location_path
from ..services.logbuffer import app_log

log = logging.getLogger("storage.dingtalk")
router = APIRouter(prefix="/api/dingtalk", tags=["dingtalk"])

# DingTalk allows 1h of clock skew but recommends 1h; we're stricter to limit replay.
MAX_TIMESTAMP_SKEW_MS = 60 * 60 * 1000  # 1 hour


def _verify_signature(timestamp: str, sign: str, secret: str) -> bool:
    """Implements DingTalk's incoming-webhook signature:
        string_to_sign = f"{timestamp}\n{secret}"
        sign = urlencode(base64(hmac_sha256(secret, string_to_sign)))
    """
    if not secret or not timestamp or not sign:
        return False
    try:
        ts_ms = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time() * 1000) - ts_ms) > MAX_TIMESTAMP_SKEW_MS:
        log.warning("dingtalk: timestamp out of range (%s)", timestamp)
        return False
    msg = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    expected = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
    return hmac.compare_digest(expected, sign)


def _format_markdown_response(result: dict[str, Any]) -> dict[str, Any]:
    """Render an IntentResult as a DingTalk markdown message. Markdown is the
    only DingTalk message type that renders tables nicely in mobile + desktop."""
    title = f"{result.get('intent', '结果')} · 置信度 {int((result.get('confidence', 0)) * 100)}%"
    lines: list[str] = []
    if result.get("speech"):
        lines.append(f"**{result['speech']}**\n")

    recs = result.get("recommendations") or []
    cands = result.get("candidates") or []
    by_id_cand = {c["item_id"]: c for c in cands}

    if recs:
        lines.append("| 物品 | 用途 | 位置 |")
        lines.append("| --- | --- | --- |")
        for r in recs:
            c = by_id_cand.get(r["item_id"]) or {}
            lines.append(f"| {c.get('item_name', '#' + str(r['item_id']))} | "
                         f"{r.get('purpose', '')} | {c.get('location_path') or '—'} |")
    elif cands:
        lines.append("| 物品 | 位置 |")
        lines.append("| --- | --- |")
        for c in cands[:20]:
            lines.append(f"| {c.get('item_name')} | {c.get('location_path') or '—'} |")

    if result.get("executed"):
        lines.append("\n✅ 已执行")

    md = "\n".join(lines) if lines else (result.get("speech") or "（无内容）")
    return {
        "msgtype": "markdown",
        "markdown": {"title": title[:80], "text": md},
    }


@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    """Receive an incoming DingTalk @bot event and synchronously return a reply.

    Query string from DingTalk: `?timestamp=...&sign=...` (only when 加签 is on).
    Body: JSON with `text.content`, plus `senderStaffId`, `senderNick`, etc.
    """
    cfg = store.get()
    dt_cfg = cfg.dingtalk
    if not dt_cfg.enabled:
        raise HTTPException(404, "DingTalk integration disabled")

    ts = request.query_params.get("timestamp")
    sign = request.query_params.get("sign")
    if dt_cfg.sign_secret:
        if not _verify_signature(ts or "", sign or "", dt_cfg.sign_secret):
            app_log.warning("dingtalk: signature rejected from %s", request.client.host if request.client else "?")
            raise HTTPException(401, "Invalid signature")
    else:
        app_log.warning("dingtalk: 签名秘钥未配置 — 跳过校验, 仅供内网测试")

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, f"Invalid JSON: {exc}")

    text = ((payload.get("text") or {}).get("content") or "").strip()
    if not text:
        return {"msgtype": "text", "text": {"content": "（没听见你说什么)"}}

    # SELF-ECHO GUARD: DingTalk's outgoing webhook normally only fires for human
    # @-mentions of the bot, but defensively reject any message whose sender is
    # the bot itself. `chatbotUserId` in the payload is the bot's user-id; if
    # `senderId` equals it, it's a self-message — drop.
    sender = payload.get("senderStaffId") or payload.get("senderNick") or ""
    sender_id = payload.get("senderId") or ""
    bot_user_id = payload.get("chatbotUserId") or ""
    if bot_user_id and sender_id and sender_id == bot_user_id:
        app_log.warning("dingtalk: self-message from chatbotUserId=%s — dropping", bot_user_id)
        return {"msgtype": "empty"}
    # DingTalk supports `isInAtList` to indicate the bot was mentioned. If a
    # webhook fires for a non-@ event, ignore it instead of replying to noise.
    if "isInAtList" in payload and not payload.get("isInAtList"):
        return {"msgtype": "empty"}

    if dt_cfg.allowed_users and sender and sender not in dt_cfg.allowed_users:
        app_log.warning("dingtalk: sender %r not allowed", sender)
        return {"msgtype": "text", "text": {"content": "你不在这个机器人的白名单里 ☹"}}

    app_log.info("dingtalk.webhook from=%s text=%r", sender, text[:120])

    try:
        out = await parse_intent(text, db, cfg)
    except LLMError as exc:
        app_log.error("dingtalk: LLM error: %s", exc)
        return {"msgtype": "text", "text": {"content": f"AI 出错了: {exc}"}}

    parsed = out["parsed"]
    # SILENT EXECUTION: force confidence high so execute_intent doesn't park
    # mutating actions behind "needs_confirmation". The user explicitly asked
    # for no-confirmation flow on DingTalk.
    if parsed.get("intent") in ("take_out", "put_in", "consume", "create_item") or parsed.get("operations"):
        parsed["confidence"] = max(parsed.get("confidence", 0.0), 1.0)
    result = execute_intent(db, text, parsed, cfg)
    app_log.info("dingtalk.done intent=%s exec=%s tx=%s",
                 result.get("intent"), result.get("executed"), result.get("transaction_id"))
    return _format_markdown_response(result)


@router.post("/test")
async def test_endpoint():
    """Simple liveness probe. Configure DingTalk's 'Outgoing URL' to point at
    /webhook; this endpoint is just for ops to verify the deploy."""
    cfg = store.get()
    return {
        "enabled": cfg.dingtalk.enabled,
        "sign_secret_set": bool(cfg.dingtalk.sign_secret),
        "allowed_users": cfg.dingtalk.allowed_users,
    }
