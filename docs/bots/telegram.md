# Telegram Bot 接入

> **类型**: 长轮询 (NAS 主动出站 HTTPS)
> **需要公网?**: ❌
> **国内可用?**: ⚠ 需翻墙 (Telegram 在国内被墙)

## 原理

后端启动一个常驻 asyncio 任务,长轮询 `api.telegram.org/bot<token>/getUpdates?timeout=25`。
NAS 主动出站 HTTPS,Telegram 服务器无需访问 NAS。

代码: [`backend/app/services/telegram.py`](../../backend/app/services/telegram.py)

## 1. 创建 Bot

1. Telegram 找 [@BotFather](https://t.me/BotFather)
2. `/newbot` → 起名 → 拿到 `123456:ABC-DEF1234...` 格式的 token

## 2. 拿 chat_id / user_id (可选,做白名单用)

- 把机器人加到群,随便发一句
- 打开服务的 **诊断** 页或后端日志,搜 `telegram from=` 行,旁边就是 chat_id 和 user_id
- 或临时访问 `https://api.telegram.org/bot<token>/getUpdates` 看 `chat.id` / `from.id`

## 3. 填配置

- 设置页 → **✈️ Telegram** → 勾选启用 + 粘 token + 可选白名单 → 保存
- 保存后**无需重启服务**,后端 `_reload_event` 会立刻让 poller 用新 token 工作

## 4. 在群里发指令

```
充电宝在哪
/find 卷尺
我刚拿了螺丝刀
我发烧了家里有什么药
```

机器人静默执行,直接返回结果(Markdown 格式)。

## 翻墙说明

Telegram 在国内无法直连。两种思路:

- **NAS 上跑代理客户端** (Clash / V2Ray),给 Python 进程设环境变量
  ```
  HTTPS_PROXY=http://127.0.0.1:7890
  HTTP_PROXY=http://127.0.0.1:7890
  ```
  在 `docker-compose.yml` 的 backend service 加 `environment:` 即可。
- **服务部署在境外 VPS**,通过 frp/wireguard 把数据库挂载到本地 NAS

国内不愿翻墙的用户:用 [飞书](feishu.md) 替代,体验类似,完全免穿透。

## 参考

- [Telegram Bot API](https://core.telegram.org/bots/api)
- [BotFather 文档](https://core.telegram.org/bots/features#botfather)
