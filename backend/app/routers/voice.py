import logging
import time
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .. import models
from ..config import store
from ..database import get_db
from ..llm.client import LLMError
from ..llm.intent import apply_operations, execute_intent, parse_intent
from ..schemas import IntentApply, IntentResult, VoiceQuery
from ..services.inventory import location_path
from ..services.logbuffer import app_log

log = logging.getLogger("storage.voice")
router = APIRouter(prefix="/api/voice", tags=["voice"])


@router.post("/intent", response_model=IntentResult)
async def voice_intent(payload: VoiceQuery, db: Session = Depends(get_db)):
    cfg = store.get()
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(400, "empty text")

    # Confirmation flow: client echoes pending_action with confirmed=true.
    ctx = payload.context or {}
    if ctx.get("confirmed") and ctx.get("pending_action"):
        pa = ctx["pending_action"]
        parsed = {
            "intent": pa.get("intent", "unknown"),
            "item_id": pa.get("item_id"),
            "location_id": pa.get("location_id"),
            "quantity": int(pa.get("quantity") or 1),
            "confidence": 1.0,
            "speech": "",
            "candidates": [],
            "operations": pa.get("operations") or [],
        }
        return execute_intent(db, text, parsed, cfg)

    # plan_only: 只出"待确认方案", 一个字都不落库。网页端在
    # voice.confirm_before_apply 打开时传 true; 没传就沿用老的"直接执行"行为
    # (群机器人没有界面可点, 永远走老路)。客户端显式传 false 可以强制关掉。
    plan_only = ctx.get("plan_only")
    if plan_only is None:
        plan_only = False
    plan_only = bool(plan_only) and cfg.voice.confirm_before_apply

    log.info("intent.parse text=%r plan_only=%s", text, plan_only)
    try:
        out = await parse_intent(text, db, cfg)
    except LLMError as exc:
        app_log.error("LLM error for %r: %s", text, exc)
        raise HTTPException(502, f"LLM error: {exc}")

    parsed = out["parsed"]
    log.debug("intent.parsed %s", parsed)
    result = execute_intent(db, text, parsed, cfg, plan_only=plan_only)
    app_log.info("intent.done text=%r intent=%s stage=%s conf=%.2f executed=%s tx=%s ops=%d",
                 text, result.get("intent"), result.get("stage"),
                 result.get("confidence", 0), result.get("executed"),
                 result.get("transaction_id"), len(result.get("operations") or []))
    return result


@router.post("/apply", response_model=IntentResult)
def voice_apply(payload: IntentApply, db: Session = Depends(get_db)):
    """执行用户在方案界面上确认过的操作。单事务, 不再调 LLM。

    这里不重新解析语句 —— 用户已经逐条挑好目标了, 再问一次 AI 只会引入新的猜错机会。
    """
    decisions = [d.model_dump() for d in payload.decisions]
    if not decisions:
        raise HTTPException(400, "没有要执行的操作")
    base = {
        "intent": "batch", "confidence": 1.0, "speech": "",
        "needs_confirmation": False, "pending_action": None,
        "candidates": [], "recommendations": [], "executed": False,
        "transaction_id": None, "operations": [], "stage": "applied",
        "plan_id": payload.plan_id, "raw": {"decisions": decisions},
    }
    result = apply_operations(db, payload.text or "", decisions, base)
    app_log.info("intent.apply plan=%s ops=%d executed=%s",
                 payload.plan_id, len(decisions), result.get("executed"))
    return result


@router.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """Proxy audio to the configured Whisper service. The browser may also do STT itself
    via Web Speech API and skip this endpoint."""
    cfg = store.get()
    if not cfg.voice.whisper_enabled:
        raise HTTPException(400, "Whisper service is disabled in settings")
    contents = await audio.read()
    if not contents:
        raise HTTPException(400, "empty audio")
    url = cfg.voice.whisper_url.rstrip("/") + "/asr"
    log.info("whisper.transcribe bytes=%d → %s", len(contents), url)
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                url,
                params={"task": "transcribe", "language": "zh", "output": "json"},
                files={"audio_file": (audio.filename or "rec.wav", contents, audio.content_type or "audio/wav")},
            )
    except httpx.HTTPError as exc:
        log.error("whisper request failed: %s", exc)
        raise HTTPException(502, f"Whisper request failed: {exc}")
    elapsed_ms = (time.time() - started) * 1000
    if resp.status_code >= 400:
        log.error("whisper HTTP %d %.0fms: %s", resp.status_code, elapsed_ms, resp.text[:200])
        raise HTTPException(502, f"Whisper HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    text = (data.get("text", "") or "").strip()
    log.info("whisper ok %.0fms text=%r", elapsed_ms, text[:80])
    return {"text": text, "raw": data}
