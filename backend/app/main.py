import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .database import Base, engine
from .routers import diag as diag_router
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
def _startup():
    app_log.info("Voice Storage backend started")
