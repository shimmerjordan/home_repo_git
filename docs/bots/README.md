# 群机器人接入

允许在 IM 群里 **@机器人** 发指令(查找物品、记录取放、症状推荐等)。服务端**静默执行**,不需要语音确认。

## 通讯方向决定能否家用

| 模式 | 谁发起连接 | 适合 |
|---|---|---|
| **inbound webhook** | 平台 → NAS | 有公网 IP / 端口转发 / 反向隧道 |
| **outbound 长连接 / 长轮询** | NAS → 平台 | 家庭网络,无公网 IP |

如果你的 NAS 没有公网 IP,**只能选 outbound 方案**。下表汇总主流选择:

| 方案 | 国内可用 | 不需要公网 | 工作量 | 备注 |
|---|---|---|---|---|
| [钉钉自定义机器人](dingtalk.md) | ✅ | ❌ 需 inbound | 低 | 加签 + Markdown 回复 |
| [Telegram Bot](telegram.md) | ⚠ 需翻墙 | ✅ 长轮询 | 低 | 最简单,单 token |
| [飞书 Lark Stream Mode](feishu.md) | ✅ | ✅ WebSocket | 中 | **推荐:国内 + 不需公网** |
| QQ 官方 Q-Group Bot | ✅ | ✅ WebSocket | 中 | 需企业认证 / 个人版申请通过 |
| 企业微信应用 | ✅ | ❌ inbound 回调 | 中 | 同钉钉,需公网 |
| Slack Socket Mode | ⚠ | ✅ WebSocket | 中 | 国内用户少 |
| Wechaty/itchat (微信个人号) | ✅ | ✅ | 高 | **违反微信 ToS,封号风险高,不推荐** |
| Server酱 / Bark | ✅ | ✅ (只能推) | 极低 | 无法接收命令 |

## 已实现的方案

- **[钉钉](dingtalk.md)** — `inbound webhook` + 加签校验。需要公网 / 内网穿透。
- **[Telegram](telegram.md)** — `getUpdates` 长轮询。NAS 主动出站,无需公网。需要能访问 telegram.org。
- **[飞书](feishu.md)** — `lark-oapi` WebSocket Stream Mode。NAS 主动出站,**国内可用 + 无需公网**。

## 通用行为

不管哪个平台,后端都会:
1. 把 `@bot` / `/command` 前缀去掉,把剩余文本送到 [`llm/intent.py`](../../backend/app/llm/intent.py)
2. 跑 `parse_intent` → `execute_intent` pipeline,共享语音那条管线
3. **强制静默执行**:在 IM 上下文里没法语音确认,所以 `take_out / put_in / create_item` 的 confidence 拉满,直接执行
4. 把候选 / 推荐渲染成各平台支持的富文本(钉钉 Markdown、Telegram MarkdownV2、飞书 text/post)
5. 应用配置的白名单(staffId / chat_id / open_id),防止陌生人滥用
6. 写诊断日志,方便排查

## 加新平台

想接 Slack / QQ Bot / 企业微信?复制 [`backend/app/services/telegram.py`](../../backend/app/services/telegram.py)
或 [`feishu.py`](../../backend/app/services/feishu.py) 当模板,改:

- 协议(长轮询 vs WebSocket vs SDK 事件)
- 鉴权(token / app_id+secret / OAuth2)
- 消息解析(各家都有自己的 `text.content` / `message.body` 字段名)
- 回复 API(各家的 `sendMessage` 等价物)

业务逻辑可以直接复用 `_run_intent` / `_format_reply`。
