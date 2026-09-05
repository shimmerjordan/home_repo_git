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

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..config import store
from ..database import get_db
from ..services import botflow
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
    # 待确认方案的归属必须是稳定 id: senderNick 是可改、可重名的展示名,
    # 同群两个人取一样的昵称就等于共用一把确认钥匙; 用户在挂起期间改了
    # 群昵称, 自己的方案就取不回来了。取不到稳定 id 就交空串, botflow 的
    # 空身份守卫会拒绝高风险操作 —— 那正是我们要的 (白名单判定仍然用
    # 上面的 sender 变量, 不受影响)。
    identity = str(payload.get("senderStaffId") or payload.get("senderId") or "")
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

    conversation_id = str(payload.get("conversationId") or "")
    try:
        reply_text = await botflow.handle_bot_message(
            "dingtalk", conversation_id, identity, text, db, cfg)
    except Exception as exc:
        # 钉钉这条是 FastAPI 路由 —— 异常抛出去就是 500, 群里一个字都收不到,
        # 用户只会觉得机器人死了。
        # 回复用固定文案: exc 的原文可能带 SQL 语句、参数、连接串、文件路径,
        # 而这是发到真人群里的消息。细节只进日志。
        app_log.error("dingtalk: 处理失败 %s", exc)
        log.exception("dingtalk botflow: %s", exc)
        return {"msgtype": "text", "text": {"content": "出错了, 我这边记下了日志"}}
    app_log.info("dingtalk.done chat=%s len=%s", conversation_id, len(reply_text))
    # 钉钉的 markdown 是标准子集, 单个 \n 不构成换行 —— botfmt 的纯文本清单
    # (编号方案/候选列表/· 结果行) 全是连续单换行, 塞进 markdown 会挤成一坨。
    # text 消息按 \n 换行。代价只是丢掉 title (仅用于通知栏摘要, 已收敛成
    # 固定的"仓储管家", 本来就没有信息量)。
    return {"msgtype": "text", "text": {"content": reply_text}}


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
