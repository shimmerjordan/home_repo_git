import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .database import Base, engine
from .routers import audit as audit_router
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

from . import models  # noqa: F401  (ensure tables registered)
from sqlalchemy import text as _sql_text

Base.metadata.create_all(bind=engine)


def _ensure_columns():
    """SQLite-friendly mini-migration: ALTER TABLE if expected columns are missing.
    Also backfills UUIDs for any locations that lack one."""
    import uuid as _uuid
    expected = {
        "locations": [
            ("geometry", "TEXT DEFAULT ''"),
            ("uuid", "VARCHAR(36)"),
        ],
        "items": [
            ("pos_x", "REAL"),
            ("pos_z", "REAL"),
        ],
    }
    with engine.begin() as conn:
        for table, cols in expected.items():
            existing = {r[1] for r in conn.execute(_sql_text(f"PRAGMA table_info({table})"))}
            for name, typ in cols:
                if name not in existing:
                    conn.execute(_sql_text(f"ALTER TABLE {table} ADD COLUMN {name} {typ}"))
        # Backfill UUIDs for any rows that don't have one yet.
        rows = conn.execute(_sql_text("SELECT id FROM locations WHERE uuid IS NULL OR uuid = ''")).fetchall()
        for (lid,) in rows:
            conn.execute(_sql_text("UPDATE locations SET uuid = :u WHERE id = :i"),
                         {"u": str(_uuid.uuid4()), "i": lid})


_ensure_columns()


def _migrate_to_home():
    """One-shot data migration: legacy DBs predate the "家" (home) concept. If we
    detect ANY top-level locations and there is no home yet, synthesise "我家"
    and re-parent every orphan root location under it.

    Invariants:
      - IDs are NEVER changed (FKs keep working).
      - Geometry / item rows are NEVER touched.
      - Idempotent: if a 'home' kind already exists, do nothing.
    """
    import uuid as _uuid
    import json as _json
    from datetime import datetime as _dt
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(_sql_text("PRAGMA table_info(locations)"))}
        if "kind" not in cols or "parent_id" not in cols:
            return
        if conn.execute(_sql_text("SELECT 1 FROM locations WHERE kind = 'home' LIMIT 1")).fetchone():
            return
        orphans = conn.execute(_sql_text(
            "SELECT id, name FROM locations WHERE parent_id IS NULL"
        )).fetchall()
        if not orphans:
            return
        geo = _json.dumps({"x": 0, "y": 0, "z": 0, "w": 0, "h": 0, "d": 0, "color": "#0ea5e9"})
        res = conn.execute(_sql_text(
            "INSERT INTO locations (name, kind, parent_id, note, geometry, uuid, created_at) "
            "VALUES (:n, 'home', NULL, :note, :g, :u, :ts)"
        ), {"n": "我家", "note": "自动迁移生成,可在 3D 页改名",
            "g": geo, "u": str(_uuid.uuid4()), "ts": _dt.now().isoformat()})
        home_id = res.lastrowid
        for oid, _name in orphans:
            conn.execute(_sql_text(
                "UPDATE locations SET parent_id = :h WHERE id = :i AND parent_id IS NULL"
            ), {"h": home_id, "i": oid})
        try:
            ids_str = ",".join(str(o[0]) for o in orphans)
            conn.execute(_sql_text(
                "INSERT INTO audit_logs (ts, entity_type, entity_id, entity_name, action, "
                "changes, summary, source) VALUES (:ts, 'location', :eid, :en, 'create', "
                ":ch, :sm, 'auto-migrate')"
            ), {
                "ts": _dt.now().isoformat(),
                "eid": home_id,
                "en": "我家",
                "ch": _json.dumps({"_created": True, "_migrated_children": ids_str}),
                "sm": f"自动创建顶层「我家」, 把 {len(orphans)} 个根位置放进来",
            })
        except Exception:
            pass


_migrate_to_home()

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
