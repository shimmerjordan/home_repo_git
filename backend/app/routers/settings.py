from fastapi import APIRouter, HTTPException

from ..config import AppConfig, store
from ..llm.client import LLMClient, LLMError
from ..schemas import ConfigPatch
from ..services import secrets

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _redact(cfg: AppConfig) -> dict:
    """脱敏后的完整配置 (密钥打码 + `<field>_set` 标记)。逻辑见 services/secrets.py。"""
    return secrets.redact(cfg.model_dump())


@router.get("")
def get_settings():
    return _redact(store.get())


@router.patch("")
def update_settings(patch: ConfigPatch):
    body: dict = {}
    if patch.llm is not None:
        body["llm"] = {k: v for k, v in patch.llm.model_dump(exclude_unset=True).items()}
    if patch.voice is not None:
        body["voice"] = {k: v for k, v in patch.voice.model_dump(exclude_unset=True).items()}
    if patch.dingtalk is not None:
        body["dingtalk"] = {k: v for k, v in patch.dingtalk.model_dump(exclude_unset=True).items()}
    if patch.telegram is not None:
        body["telegram"] = {k: v for k, v in patch.telegram.model_dump(exclude_unset=True).items()}
    if patch.feishu is not None:
        body["feishu"] = {k: v for k, v in patch.feishu.model_dump(exclude_unset=True).items()}
    store.update(body)
    # The Telegram poller subscribes to config changes — restart it so the new
    # token / enabled flag take effect immediately (no service restart needed).
    try:
        from ..services import telegram as _tg
        _tg.reload()
    except Exception:
        pass
    try:
        from ..services import feishu as _fs
        _fs.reload()
    except Exception:
        pass
    return _redact(store.get())


@router.post("/test-llm")
async def test_llm():
    cfg = store.get()
    client = LLMClient(cfg.llm)
    try:
        result = await client.chat(
            [
                {"role": "system", "content": "Reply with the single word: pong."},
                {"role": "user", "content": "ping"},
            ]
        )
    except LLMError as exc:
        raise HTTPException(502, str(exc))
    return {"ok": True, "content": result["content"][:200]}
