"""WebDAV 数据备份与恢复。

设计要点 (见计划):
  - **裸 DB 为主**: 整个 SQLite 文件快照是恢复主道, 以后加表/加字段自动覆盖、零维护。
  - **JSON 辅助**: 物品/位置/流水/审计另导出可读 JSON, 供人工查看与跨端 (小程序) 导入。
  - **声明式组件注册表** COMPONENTS: 备份遍历它决定入包内容; 加组件 = 加一条。
  - **GFS 分层保留** apply_gfs: 纯函数, 按文件名时间戳分日/周/月桶。
  - **可选 AES-256 加密** (cryptography, 缺库时备份报错而非静默明文)。
  - **WebDAV 用 webdav4 库** (缺库时友好报错)。
  - 调度器 start()/reload()/stop() 照搬 services/telegram.py 的 asyncio 模式。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import CONFIG_PATH, WebDAVConfig, store
from ..database import engine
from .logbuffer import LOG_DIR, app_log

FORMAT_VERSION = 1
NAME_RE = re.compile(r"^backup-(\d{8})-(\d{6})(?:\.enc)?\.zip$")


# ----------------------------------------------------------------------------- paths
def _db_path() -> Path | None:
    """当前 SQLite 库文件路径; 非 sqlite 后端返回 None (裸 DB 备份不适用)。"""
    try:
        if engine.url.get_backend_name() != "sqlite":
            return None
        return Path(engine.url.database) if engine.url.database else None
    except Exception:
        return None


def _data_dir() -> Path:
    db = _db_path()
    if db:
        return db.parent
    return CONFIG_PATH.parent


# ----------------------------------------------------------------------------- 组件注册表
# 每个组件: collect(zf) 把文件写进 zip。restore 见 _restore_* (DB/settings/logs)。
def _collect_settings(zf: zipfile.ZipFile) -> list[str]:
    if CONFIG_PATH.exists():
        zf.writestr("config.json", CONFIG_PATH.read_bytes())
        return ["config.json"]
    return []


def _sqlite_snapshot() -> bytes | None:
    """用 sqlite3 .backup() 取一致性快照 (避免备份到写一半的库)。"""
    db = _db_path()
    if not db or not db.exists():
        return None
    tmp = Path(tempfile.mkstemp(suffix=".db")[1])
    try:
        src = sqlite3.connect(str(db))
        dst = sqlite3.connect(str(tmp))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def _export_json(zf: zipfile.ZipFile, arcname: str, rows: list[dict]) -> list[str]:
    zf.writestr(arcname, json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    return [arcname]


def _collect_inventory(zf: zipfile.ZipFile) -> list[str]:
    """裸 DB 快照 (权威, 含全部表) + 物品/位置可读 JSON。"""
    from .. import models
    from ..database import SessionLocal
    from . import inventory as inv

    written: list[str] = []
    snap = _sqlite_snapshot()
    if snap is not None:
        zf.writestr("db/storage.db", snap)
        written.append("db/storage.db")
    db = SessionLocal()
    try:
        items = [inv.serialize_item(i) for i in db.query(models.Item).order_by(models.Item.id).all()]
        locs = [inv.serialize_location(l) for l in db.query(models.Location).order_by(models.Location.id).all()]
        written += _export_json(zf, "data/items.json", items)
        written += _export_json(zf, "data/locations.json", locs)
    finally:
        db.close()
    return written


def _collect_transactions(zf: zipfile.ZipFile) -> list[str]:
    from .. import models
    from ..database import SessionLocal
    from . import inventory as inv

    db = SessionLocal()
    try:
        txs = [inv.serialize_transaction(t)
               for t in db.query(models.Transaction).order_by(models.Transaction.id).all()]
        return _export_json(zf, "data/transactions.json", txs)
    finally:
        db.close()


def _collect_audit(zf: zipfile.ZipFile) -> list[str]:
    from .. import models
    from ..database import SessionLocal
    from . import audit as aud

    db = SessionLocal()
    try:
        rows = [aud.serialize(r) for r in db.query(models.AuditLog).order_by(models.AuditLog.id).all()]
        return _export_json(zf, "data/audit.json", rows)
    finally:
        db.close()


def _collect_logs(zf: zipfile.ZipFile) -> list[str]:
    written: list[str] = []
    if LOG_DIR.exists():
        for p in sorted(LOG_DIR.glob("app.log*")):
            if p.is_file():
                arc = f"logs/{p.name}"
                zf.writestr(arc, p.read_bytes())
                written.append(arc)
    return written


# key -> (中文标签, collect 函数)。新增组件在此登记即可。
COMPONENTS: dict[str, tuple[str, Any]] = {
    "settings": ("app 设置", _collect_settings),
    "inventory": ("物品 + 位置 (整库)", _collect_inventory),
    "transactions": ("操作流水", _collect_transactions),
    "audit": ("审计日志", _collect_audit),
    "logs": ("系统日志", _collect_logs),
}


# ----------------------------------------------------------------------------- 打包 / 加密
def _build_zip(components: list[str]) -> tuple[bytes, dict]:
    buf = io.BytesIO()
    files: dict[str, dict] = {}
    selected = [c for c in components if c in COMPONENTS]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for key in selected:
            _label, collect = COMPONENTS[key]
            for arc in collect(zf):
                # sha256 记录进 manifest 供恢复时校验。
                files[arc] = {"sha256": hashlib.sha256(zf.read(arc)).hexdigest()}
        manifest = {
            "format_version": FORMAT_VERSION,
            "app": "voice-storage",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "components": selected,
            "files": files,
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buf.getvalue(), manifest


# 加密文件头: magic(5) + salt(16) + nonce(12) + ciphertext。
_ENC_MAGIC = b"VSBK1"
_PBKDF2_ITERS = 200_000


def _crypto():
    """返回 cryptography 的 AESGCM/PBKDF2 句柄; 缺库时返回 None。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        return AESGCM, PBKDF2HMAC, hashes
    except Exception:
        return None


def _derive_key(passphrase: str, salt: bytes):
    AESGCM, PBKDF2HMAC, hashes = _crypto()
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_PBKDF2_ITERS)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    parts = _crypto()
    if not parts:
        raise RuntimeError("加密需要 cryptography 库 (pip install cryptography), 或在备份设置里关闭加密")
    AESGCM = parts[0]
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(passphrase, salt)
    ct = AESGCM(key).encrypt(nonce, data, None)
    return _ENC_MAGIC + salt + nonce + ct


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    parts = _crypto()
    if not parts:
        raise RuntimeError("解密需要 cryptography 库 (pip install cryptography)")
    if blob[:5] != _ENC_MAGIC:
        raise ValueError("不是有效的加密备份包 (magic 不匹配)")
    AESGCM = parts[0]
    salt, nonce, ct = blob[5:21], blob[21:33], blob[33:]
    key = _derive_key(passphrase, salt)
    try:
        return AESGCM(key).decrypt(nonce, ct, None)
    except Exception:
        raise ValueError("解密失败: 口令错误或备份包损坏")


# ----------------------------------------------------------------------------- WebDAV 客户端
def _webdav(cfg: WebDAVConfig):
    try:
        from webdav4.client import Client
    except Exception:
        raise RuntimeError("WebDAV 功能需要 webdav4 库 (pip install webdav4)")
    if not cfg.url:
        raise ValueError("未配置 WebDAV 地址")
    return Client(base_url=cfg.url, auth=(cfg.username, cfg.password), timeout=30)


def _remote_path(cfg: WebDAVConfig, name: str = "") -> str:
    d = (cfg.remote_dir or "").strip("/")
    return f"{d}/{name}".strip("/") if d else name


def _ensure_remote_dir(client, cfg: WebDAVConfig) -> None:
    d = (cfg.remote_dir or "").strip("/")
    if not d:
        return
    # 逐级建目录, 已存在则忽略。
    parts = d.split("/")
    cur = ""
    for p in parts:
        cur = f"{cur}/{p}".strip("/")
        try:
            if not client.exists(cur):
                client.mkdir(cur)
        except Exception:
            pass


def test_connection(cfg: WebDAVConfig) -> dict:
    # 不探测根目录: 部分服务器 (如 bytemark/webdav) 禁止对 / 做 PROPFIND。
    # 直接确保/检查目标子目录, 兼容性更好。
    client = _webdav(cfg)
    try:
        _ensure_remote_dir(client, cfg)
        # 用 ls 而非 exists 做连通性检查: 部分服务器对集合路径会 301 重定向到带斜杠版,
        # webdav4 的 exists() 会因此抛错, 而 ls() 能正常列目录。
        client.ls(_remote_path(cfg) or "", detail=False)
        return {"ok": True, "message": "连接成功"}
    except Exception as exc:
        raise RuntimeError(f"WebDAV 连接失败: {exc}")


# ----------------------------------------------------------------------------- GFS 保留 (纯函数, 可单测)
def _parse_ts(name: str) -> datetime | None:
    m = NAME_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def apply_gfs(names: list[str], keep_daily: int, keep_weekly: int,
              keep_monthly: int) -> tuple[list[str], list[str]]:
    """Grandfather-Father-Son 保留: 每个日/周/月桶保留最近一个, 各保留最近 N 个桶。
    返回 (保留列表, 删除列表)。无法解析时间戳的文件名一律保留 (不误删)。"""
    parsed: list[tuple[datetime, str]] = []
    unknown: list[str] = []
    for n in names:
        dt = _parse_ts(n)
        if dt:
            parsed.append((dt, n))
        else:
            unknown.append(n)
    parsed.sort(key=lambda x: x[0], reverse=True)  # 新 -> 旧

    keep: set[str] = set(unknown)

    def _bucket(keyfn, limit: int) -> None:
        seen: dict[Any, str] = {}
        for dt, n in parsed:  # 已按新->旧, 每桶首个即最新
            k = keyfn(dt)
            if k not in seen:
                seen[k] = n
        for k in sorted(seen, reverse=True)[:max(0, limit)]:
            keep.add(seen[k])

    _bucket(lambda dt: dt.strftime("%Y%m%d"), keep_daily)
    _bucket(lambda dt: dt.isocalendar()[:2], keep_weekly)
    _bucket(lambda dt: (dt.year, dt.month), keep_monthly)

    all_names = {n for _, n in parsed} | set(unknown)
    delete = sorted(all_names - keep)
    return sorted(keep), delete


# ----------------------------------------------------------------------------- 备份 / 列表 / 下载 / 删除
def run_backup(cfg: WebDAVConfig | None = None, components: list[str] | None = None) -> dict:
    """打包 -> (加密) -> 上传 WebDAV -> GFS 清理。返回结果摘要。"""
    cfg = cfg or store.get().webdav
    comps = components if components is not None else cfg.components
    data, manifest = _build_zip(comps)

    encrypted = bool(cfg.encrypt and cfg.passphrase)
    if cfg.encrypt and not cfg.passphrase:
        app_log.warning("backup: encrypt=true 但未设置口令, 本次不加密")
    if encrypted:
        data = encrypt_bytes(data, cfg.passphrase)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"backup-{ts}{'.enc' if encrypted else ''}.zip"

    client = _webdav(cfg)
    _ensure_remote_dir(client, cfg)
    client.upload_fileobj(io.BytesIO(data), _remote_path(cfg, name), overwrite=True)
    app_log.info("backup uploaded: %s (%d bytes, components=%s, enc=%s)",
                 name, len(data), comps, encrypted)

    deleted = _apply_retention(client, cfg)
    return {
        "ok": True,
        "name": name,
        "size": len(data),
        "encrypted": encrypted,
        "components": manifest["components"],
        "deleted": deleted,
    }


def _apply_retention(client, cfg: WebDAVConfig) -> list[str]:
    try:
        names = [b["name"] for b in _list_raw(client, cfg)]
        _keep, delete = apply_gfs(names, cfg.keep_daily, cfg.keep_weekly, cfg.keep_monthly)
        for n in delete:
            try:
                client.remove(_remote_path(cfg, n))
            except Exception as exc:
                app_log.warning("backup: 删除过期备份 %s 失败: %s", n, exc)
        if delete:
            app_log.info("backup: GFS 清理了 %d 个过期备份", len(delete))
        return delete
    except Exception as exc:
        app_log.warning("backup: 保留策略执行失败: %s", exc)
        return []


def _list_raw(client, cfg: WebDAVConfig) -> list[dict]:
    out: list[dict] = []
    for entry in client.ls(_remote_path(cfg) or "/", detail=True):
        href = entry.get("name") or entry.get("href") or ""
        base = href.rstrip("/").split("/")[-1]
        if entry.get("type") == "directory":
            continue
        if not NAME_RE.match(base):
            continue
        out.append({
            "name": base,
            "size": entry.get("content_length") or 0,
            "modified": str(entry.get("modified") or ""),
        })
    return out


def list_backups(cfg: WebDAVConfig | None = None) -> list[dict]:
    cfg = cfg or store.get().webdav
    client = _webdav(cfg)
    rows = _list_raw(client, cfg)
    rows.sort(key=lambda r: r["name"], reverse=True)
    return rows


def download_backup(name: str, cfg: WebDAVConfig | None = None) -> bytes:
    cfg = cfg or store.get().webdav
    if not NAME_RE.match(name):
        raise ValueError("非法备份文件名")
    client = _webdav(cfg)
    buf = io.BytesIO()
    client.download_fileobj(_remote_path(cfg, name), buf)
    return buf.getvalue()


def delete_backup(name: str, cfg: WebDAVConfig | None = None) -> None:
    cfg = cfg or store.get().webdav
    if not NAME_RE.match(name):
        raise ValueError("非法备份文件名")
    client = _webdav(cfg)
    client.remove(_remote_path(cfg, name))
    app_log.info("backup deleted: %s", name)


# ----------------------------------------------------------------------------- 恢复
RESTORE_TARGETS = ("settings", "database", "logs")


def _pre_restore_snapshot() -> str:
    """恢复前把当前 DB + config 本地快照到 data/_pre_restore_<ts>/, 以便回滚。"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = _data_dir() / f"_pre_restore_{ts}"
    dest.mkdir(parents=True, exist_ok=True)
    db = _db_path()
    if db and db.exists():
        shutil.copy2(db, dest / "storage.db")
    if CONFIG_PATH.exists():
        shutil.copy2(CONFIG_PATH, dest / "config.json")
    app_log.info("backup: 恢复前快照已存到 %s", dest)
    return str(dest)


def restore(archive_bytes: bytes, passphrase: str = "",
            targets: list[str] | None = None) -> dict:
    """从备份包恢复。targets 选择恢复哪些: settings / database / logs。
    自动识别加密包并解密; 恢复前先本地快照。"""
    targets = [t for t in (targets or list(RESTORE_TARGETS)) if t in RESTORE_TARGETS]
    blob = archive_bytes
    if blob[:5] == _ENC_MAGIC:
        if not passphrase:
            raise ValueError("该备份已加密, 请提供解密口令")
        blob = decrypt_bytes(blob, passphrase)

    snapshot = _pre_restore_snapshot()
    restored: list[str] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json")) if "manifest.json" in names else {}

        if "settings" in targets and "config.json" in names:
            _restore_settings(zf.read("config.json"))
            restored.append("settings")

        if "database" in targets and "db/storage.db" in names:
            _restore_database(zf.read("db/storage.db"))
            restored.append("database")

        if "logs" in targets:
            log_files = [n for n in names if n.startswith("logs/")]
            if log_files:
                for n in log_files:
                    (LOG_DIR / Path(n).name).write_bytes(zf.read(n))
                restored.append("logs")

    app_log.info("backup: 恢复完成 targets=%s manifest=%s", restored, manifest.get("created_at"))
    return {"ok": True, "restored": restored, "snapshot": snapshot,
            "manifest": manifest}


def _restore_settings(raw: bytes) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_bytes(raw)
    store.reload()  # 让运行中的 ConfigStore 重新读盘


def _restore_database(snapshot: bytes) -> None:
    db = _db_path()
    if not db:
        raise RuntimeError("当前不是 SQLite 后端, 无法用裸 DB 快照恢复")
    engine.dispose()  # 断开连接池, 释放文件句柄
    tmp = db.with_suffix(db.suffix + ".restore")
    tmp.write_bytes(snapshot)
    os.replace(tmp, db)
    from .. import migrations
    migrations.run_all(engine)  # 与启动共用迁移, 旧库自动升级


# ----------------------------------------------------------------------------- 调度器 (照搬 telegram.py asyncio 模式)
_task: asyncio.Task | None = None
_wake: asyncio.Event | None = None
_last_run_at: datetime | None = None


def _due(cfg: WebDAVConfig, now: datetime) -> bool:
    global _last_run_at
    if cfg.schedule == "hourly":
        if _last_run_at and _last_run_at.strftime("%Y%m%d%H") == now.strftime("%Y%m%d%H"):
            return False
        return True
    if cfg.schedule == "daily":
        if now.hour != cfg.hour:
            return False
        return not (_last_run_at and _last_run_at.date() == now.date())
    if cfg.schedule == "weekly":
        if now.weekday() != 0 or now.hour != cfg.hour:
            return False
        return not (_last_run_at and _last_run_at.isocalendar()[:2] == now.isocalendar()[:2])
    return False


def _safe_run() -> None:
    global _last_run_at
    try:
        run_backup()
        _last_run_at = datetime.now()
    except Exception as exc:
        app_log.warning("backup: 定时备份失败: %s", exc)


async def _scheduler_loop() -> None:
    app_log.info("backup scheduler started")
    while True:
        try:
            cfg = store.get().webdav
            if cfg.enabled and cfg.schedule != "manual" and _due(cfg, datetime.now()):
                # webdav4 是同步阻塞 IO, 丢到线程池避免卡事件循环。
                await asyncio.get_event_loop().run_in_executor(None, _safe_run)
            try:
                await asyncio.wait_for(_wake.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            if _wake:
                _wake.clear()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            app_log.warning("backup scheduler error: %s", exc)
            await asyncio.sleep(60)


def start() -> None:
    global _task, _wake
    if _task and not _task.done():
        return
    _wake = asyncio.Event()
    _task = asyncio.ensure_future(_scheduler_loop())


def reload() -> None:
    """配置变更后唤醒调度循环即时生效 (与 telegram.reload 一致)。"""
    if _wake:
        _wake.set()


def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None
