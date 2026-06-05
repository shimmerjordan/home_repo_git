"""轻量级 SQLite "迁移": 应用启动时 (main.py) 与 WebDAV 备份恢复后 (services/backup.py)
都调用 run_all(engine), 保证库结构补齐、向后兼容旧数据。

历史上这些函数内联在 main.py; 抽到这里是为了启动与恢复共用同一套逻辑、避免重复。
行为与原 main.py 完全一致。"""
from __future__ import annotations

import json as _json
import uuid as _uuid
from datetime import datetime as _dt

from sqlalchemy import text as _sql_text
from sqlalchemy.engine import Engine


def ensure_columns(engine: Engine) -> None:
    """SQLite-friendly mini-migration: ALTER TABLE if expected columns are missing.
    Also backfills UUIDs for any locations that lack one."""
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


def migrate_to_home(engine: Engine) -> None:
    """One-shot data migration: legacy DBs predate the "家" (home) concept. If we
    detect ANY top-level locations and there is no home yet, synthesise "我家"
    and re-parent every orphan root location under it.

    Invariants:
      - IDs are NEVER changed (FKs keep working).
      - Geometry / item rows are NEVER touched.
      - Idempotent: if a 'home' kind already exists, do nothing.
    """
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


def run_all(engine: Engine) -> None:
    """补齐表结构并执行所有向后兼容迁移。启动与恢复共用。"""
    ensure_columns(engine)
    migrate_to_home(engine)
