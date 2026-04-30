import os
import platform
import sys

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from pydantic import BaseModel

from .. import models
from ..config import store
from ..database import get_db
from ..services.logbuffer import app_log, get_buffer

router = APIRouter(prefix="/api", tags=["diag"])


class ClientLogEntry(BaseModel):
    level: str = "INFO"
    message: str
    context: dict | None = None


@router.post("/logs/client")
def post_client_log(entry: ClientLogEntry):
    lvl = entry.level.upper()
    msg = f"[client] {entry.message}"
    if entry.context:
        msg += f" :: {entry.context}"
    {"ERROR": app_log.error, "WARNING": app_log.warning, "INFO": app_log.info}.get(lvl, app_log.info)(msg)
    return {"ok": True}


@router.get("/logs")
def get_logs(
    since_id: int = Query(0, ge=0),
    level: str | None = Query(None),
    limit: int = Query(300, ge=1, le=2000),
):
    items = get_buffer().snapshot(since_id=since_id, level=level)
    return {"items": items[-limit:], "next_since_id": items[-1]["id"] if items else since_id}


@router.get("/diag")
def diagnostics(db: Session = Depends(get_db)):
    cfg = store.get()
    items = db.query(models.Item).count()
    locs = db.query(models.Location).count()
    txs = db.query(models.Transaction).count()
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "database_url": os.getenv("DATABASE_URL", ""),
        "counts": {"items": items, "locations": locs, "transactions": txs},
        "llm": {
            "base_url": cfg.llm.base_url,
            "model": cfg.llm.model,
            "api_key_set": bool(cfg.llm.api_key),
            "supports_tools": cfg.llm.supports_tools,
        },
        "voice": {
            "wake_words": cfg.voice.wake_words,
            "confidence_threshold": cfg.voice.confidence_threshold,
            "whisper_enabled": cfg.voice.whisper_enabled,
            "whisper_url": cfg.voice.whisper_url,
        },
    }
