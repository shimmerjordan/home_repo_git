"""密钥脱敏: 把 config 里的敏感字段 (API key / token / 密码 / 口令) 打码后再返回给前端,
并附带 `<field>_set` 布尔标记前端"是否已设置"。settings 与 backup 两个路由共用,
避免脱敏逻辑重复 (历史上内联在 routers/settings.py)。"""
from __future__ import annotations


def mask(value: str | None, head: int = 3, tail: int = 2) -> str:
    """把密钥打码成 'abc***yz' 形式; 太短则整体打码为 '***'; 空值返回 ''。"""
    if not value:
        return ""
    if len(value) > head + tail:
        return value[:head] + "***" + value[-tail:]
    return "***"


# 各敏感字段的打码形状 (section, field) -> (head, tail)。新增密钥字段在此登记即可。
SECRET_FIELDS: dict[tuple[str, str], tuple[int, int]] = {
    ("llm", "api_key"): (4, 2),
    ("dingtalk", "sign_secret"): (3, 2),
    ("dingtalk", "outgoing_sign_secret"): (3, 2),
    ("telegram", "bot_token"): (4, 3),
    ("feishu", "app_secret"): (3, 2),
    ("webdav", "password"): (3, 2),
    ("webdav", "passphrase"): (3, 2),
}


def redact(data: dict) -> dict:
    """对一个完整的 AppConfig dump 做脱敏 (原地修改并返回)。
    每个登记的密钥字段被打码, 并加上 `<field>_set` 布尔。"""
    for (section, field), (head, tail) in SECRET_FIELDS.items():
        sec = data.get(section)
        if not isinstance(sec, dict):
            continue
        raw = sec.get(field)
        if raw:
            sec[field] = mask(raw, head, tail)
            sec[field + "_set"] = True
        else:
            sec[field] = ""
            sec[field + "_set"] = False
    return data
