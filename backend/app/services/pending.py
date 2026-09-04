"""群机器人的待确认方案 —— 进程内内存 + TTL, 不落库。

为什么不落 DB: 超过几分钟的待确认方案本来就该失效, 重启丢掉是可接受的;
落库要建表、要清理、要迁移, 为一个 5 分钟的临时状态不值。
(同样的取舍见 services/feishu.py 的 _seen_message_ids 去重表。)

键是 (渠道, 会话, 发送者) —— 同一个人在同一个群里同时只有一个待确认方案,
再说一句就把旧的顶掉。不同人、不同群互不干扰。
"""
from __future__ import annotations

import re
import time
from typing import Any

TTL_S = 300
MAX_ENTRIES = 200

_STORE: dict[tuple[str, str, str], dict[str, Any]] = {}

# 只认明确的确认/取消词。认不出就当作新的一句话去解析 —— 把"再拿个螺丝刀"
# 误判成确认, 比不认识它糟糕得多。
_YES = re.compile(r"^\s*(确认|确定|确认执行|执行|是的?|对|好的?|嗯|ok|yes|y)\s*[!!。.]*\s*$",
                  re.IGNORECASE)
_NO = re.compile(r"^\s*(取消|不要|不用|算了|不对|否|no|n)\s*[!!。.]*\s*$",
                 re.IGNORECASE)


def _key(channel: str, chat_id: str, sender_id: str) -> tuple[str, str, str]:
    return (str(channel), str(chat_id), str(sender_id))


def _sweep() -> None:
    now = time.time()
    for k in [k for k, v in _STORE.items() if now - v["created_at"] > TTL_S]:
        _STORE.pop(k, None)
    # 还超容就丢最老的
    while len(_STORE) > MAX_ENTRIES:
        oldest = min(_STORE, key=lambda k: _STORE[k]["created_at"])
        _STORE.pop(oldest, None)


def put(channel: str, chat_id: str, sender_id: str, plan: dict[str, Any]) -> None:
    _STORE[_key(channel, chat_id, sender_id)] = {"plan": plan, "created_at": time.time()}
    _sweep()


def peek(channel: str, chat_id: str, sender_id: str) -> dict[str, Any] | None:
    _sweep()
    entry = _STORE.get(_key(channel, chat_id, sender_id))
    return entry["plan"] if entry else None


def take(channel: str, chat_id: str, sender_id: str) -> dict[str, Any] | None:
    """取出并删除 —— 一个方案只能被确认一次。"""
    _sweep()
    entry = _STORE.pop(_key(channel, chat_id, sender_id), None)
    return entry["plan"] if entry else None


def drop(channel: str, chat_id: str, sender_id: str) -> None:
    _STORE.pop(_key(channel, chat_id, sender_id), None)


def classify_reply(text: str) -> str:
    """yes / no / other。other 表示"这不是确认, 当成新指令处理"。"""
    t = (text or "").strip()
    if _YES.match(t):
        return "yes"
    if _NO.match(t):
        return "no"
    return "other"
