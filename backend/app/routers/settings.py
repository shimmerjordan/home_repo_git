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
    store.update(body)
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
