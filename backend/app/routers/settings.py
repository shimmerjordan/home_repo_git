from fastapi import APIRouter, HTTPException

from ..config import AppConfig, store
from ..llm.client import LLMClient, LLMError
from ..schemas import ConfigPatch

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _redact(cfg: AppConfig) -> dict:
    data = cfg.model_dump()
    if data["llm"].get("api_key"):
        key = data["llm"]["api_key"]
        data["llm"]["api_key_set"] = True
        data["llm"]["api_key"] = (key[:4] + "***" + key[-2:]) if len(key) > 6 else "***"
    else:
        data["llm"]["api_key_set"] = False
        data["llm"]["api_key"] = ""
    # DingTalk secrets follow the same redaction pattern as the LLM key.
    dt = data.get("dingtalk") or {}
    for fld in ("sign_secret", "outgoing_sign_secret"):
        if dt.get(fld):
            v = dt[fld]
            dt[fld] = (v[:3] + "***" + v[-2:]) if len(v) > 6 else "***"
            dt[fld + "_set"] = True
        else:
            dt[fld + "_set"] = False
            dt[fld] = ""
    tg = data.get("telegram") or {}
    if tg.get("bot_token"):
        v = tg["bot_token"]
        tg["bot_token"] = (v[:4] + "***" + v[-3:]) if len(v) > 8 else "***"
        tg["bot_token_set"] = True
    else:
        tg["bot_token_set"] = False
        tg["bot_token"] = ""
    fs = data.get("feishu") or {}
    if fs.get("app_secret"):
        v = fs["app_secret"]
        fs["app_secret"] = (v[:3] + "***" + v[-2:]) if len(v) > 6 else "***"
        fs["app_secret_set"] = True
    else:
        fs["app_secret_set"] = False
        fs["app_secret"] = ""
    return data


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
