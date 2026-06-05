"""WebDAV 备份的 REST 接口。路由风格照搬 routers/settings.py。"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from ..config import store
from ..schemas import WebDAVConfigPatch
from ..services import backup, secrets

router = APIRouter(prefix="/api/backup", tags=["backup"])


def _redacted_webdav() -> dict:
    """脱敏后的 webdav 配置 + 能力/目录元信息, 供前端渲染。"""
    data = secrets.redact(store.get().model_dump())
    wd = data.get("webdav", {})
    parts = backup._crypto()
    try:
        import webdav4  # noqa: F401
        has_webdav = True
    except Exception:
        has_webdav = False
    return {
        "webdav": wd,
        "components": [{"key": k, "label": v[0]} for k, v in backup.COMPONENTS.items()],
        "restore_targets": list(backup.RESTORE_TARGETS),
        "capabilities": {"encryption": parts is not None, "webdav": has_webdav},
    }


@router.get("/settings")
def get_backup_settings():
    return _redacted_webdav()


@router.patch("/settings")
def update_backup_settings(patch: WebDAVConfigPatch):
    body = {k: v for k, v in patch.model_dump(exclude_unset=True).items()}
    # 空字符串的 password / passphrase 表示"保持不变" (与 LLM api_key 一致的约定)。
    for secret in ("password", "passphrase"):
        if body.get(secret) == "":
            body.pop(secret)
    store.update({"webdav": body})
    try:
        backup.reload()
    except Exception:
        pass
    return _redacted_webdav()


@router.post("/test")
def test_backup():
    try:
        return backup.test_connection(store.get().webdav)
    except Exception as exc:
        raise HTTPException(502, str(exc))


class RunBody(BaseModel):
    components: list[str] | None = None


@router.post("/run")
def run_backup(body: RunBody | None = None):
    try:
        return backup.run_backup(components=(body.components if body else None))
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/list")
def list_backups():
    try:
        return backup.list_backups()
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/download/{name}")
def download_backup(name: str):
    try:
        data = backup.download_backup(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.delete("/{name}")
def delete_backup(name: str):
    try:
        backup.delete_backup(name)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


class RestoreBody(BaseModel):
    name: str
    targets: list[str] | None = None
    passphrase: str = ""


@router.post("/restore")
def restore_from_remote(body: RestoreBody):
    """从 WebDAV 上已有的备份点恢复。"""
    try:
        data = backup.download_backup(body.name)
        return backup.restore(data, passphrase=body.passphrase, targets=body.targets)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/restore-upload")
async def restore_from_upload(
    file: UploadFile = File(...),
    passphrase: str = Form(""),
    targets: str = Form(""),
):
    """从用户上传的备份包恢复 (无需 WebDAV)。targets 为逗号分隔字符串。"""
    raw = await file.read()
    tgt = [t.strip() for t in targets.split(",") if t.strip()] or None
    try:
        return backup.restore(raw, passphrase=passphrase, targets=tgt)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))
