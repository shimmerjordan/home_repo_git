import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .database import Base, engine
from .routers import audit as audit_router
from .routers import backup as backup_router
from .routers import diag as diag_router
from .routers import dingtalk as dingtalk_router
from .routers import items as items_router
from .routers import locations as locations_router
from .routers import settings as settings_router
from .routers import voice as voice_router
from .services.logbuffer import app_log, install as install_logbuffer

# Init logging first so subsequent module-level logs are captured.
install_logbuffer()
logging.basicConfig(level=logging.INFO)

# Silence verbose 3rd-party loggers BEFORE any module imports them. Otherwise the
# logbuffer fills with thousands of WS/HTTP DEBUG lines per minute, which slows
# every /api/logs request and starves the API event loop.
for _name in ("websockets", "websockets.client", "websockets.protocol",
              "urllib3", "urllib3.connectionpool",
              "lark_oapi", "lark_oapi.ws", "lark_oapi.ws.client",
              "httpx", "httpcore"):
    try:
        logging.getLogger(_name).setLevel(logging.WARNING)
    except Exception:
        pass

from . import models  # noqa: F401  (ensure tables registered)
from . import migrations

Base.metadata.create_all(bind=engine)

# 补齐表结构 + 执行向后兼容迁移 (与备份恢复共用 migrations.run_all, 见 migrations.py)。
migrations.run_all(engine)

app = FastAPI(title="Voice Storage", version="0.2.0")

origins_raw = os.getenv("CORS_ORIGINS", "*").strip()
origins = ["*"] if origins_raw == "*" else [o.strip() for o in origins_raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(items_router.router)
app.include_router(items_router.recent_router)
app.include_router(locations_router.router)
app.include_router(voice_router.router)
app.include_router(settings_router.router)
app.include_router(diag_router.router)
app.include_router(audit_router.router)
app.include_router(dingtalk_router.router)
app.include_router(backup_router.router)


@app.middleware("http")
async def access_log(request: Request, call_next):
    started = time.time()
    try:
        response = await call_next(request)
    except Exception as exc:
        app_log.exception("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
        raise
    elapsed = (time.time() - started) * 1000
    if not request.url.path.startswith("/api/logs"):
        app_log.info("%s %s %s %.0fms",
                     request.method, request.url.path, response.status_code, elapsed)
    return response


@app.get("/api/health")
def health():
    return {"ok": True}


@app.on_event("startup")
async def _startup():
    app_log.info("Voice Storage backend started")
    # Telegram long-polling worker — only does anything when settings.telegram.enabled
    # is true. Starts the asyncio task either way so reload() works the moment
    # the user flips the toggle.
    try:
        from .services import telegram as _tg
        _tg.start()
    except Exception as exc:
        app_log.warning("telegram poller failed to start: %s", exc)
    try:
        from .services import feishu as _fs
        _fs.start()
    except Exception as exc:
        app_log.warning("feishu supervisor failed to start: %s", exc)
    # WebDAV 备份调度器 — 仅当 settings.webdav.enabled 且 schedule != manual 时才真正备份;
    # 无论如何都起 asyncio 任务, 这样用户在 UI 改配置后 reload() 立刻生效。
    try:
        from .services import backup as _bk
        _bk.start()
    except Exception as exc:
        app_log.warning("backup scheduler failed to start: %s", exc)


@app.on_event("shutdown")
async def _shutdown():
    try:
        from .services import telegram as _tg
        _tg.stop()
    except Exception:
        pass
    try:
        from .services import feishu as _fs
        _fs.stop()
    except Exception:
        pass
    try:
        from .services import backup as _bk
        _bk.stop()
    except Exception:
        pass
