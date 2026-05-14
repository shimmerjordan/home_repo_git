# 飞书 (Lark) 机器人接入

> **类型**: Stream Mode — WebSocket 长连接 (NAS 主动出站 WSS)
> **需要公网?**: ❌
> **国内可用?**: ✅ **(推荐)**
> **额外依赖**: `lark-oapi` (已在 `backend/requirements.txt`)

## 为什么推荐?

- 飞书 2024 起官方支持 **Stream Mode**:应用通过长连接 WebSocket 订阅事件,完全 outbound
- 国内服务器,稳定 + 不需要翻墙
- 官方 Python SDK `lark-oapi` 处理鉴权、token 刷新、心跳,业务侧只写 handler
- 个人也可以创建 "自建应用",免企业认证

## 1. 创建飞书自建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app)
2. **创建企业自建应用** → 填名字 + 描述 + 上传图标 → 创建
3. 进入应用详情页,拿到 **App ID** (`cli_xxx...`) 和 **App Secret**(本页面或"凭证与基础信息"标签)

## 2. 开通权限和事件

在应用详情侧栏:

### 权限管理 → 添加权限
最少需要:
- `im:message` — 获取与发送单聊、群组消息
- `im:message.group_at_msg` — 接收群里 @ 机器人的消息
- `im:message.group_at_msg:readonly` — 同上,只读版本(如果上面那个加不了)
- `im:message.p2p_msg` — 接收单聊消息(可选)
- `im:chat:readonly` — 读取群信息(可选,排错时方便)

### 事件订阅 → 订阅事件
1. 切换 **订阅方式** 为 **"长连接 (推荐)"** —— 这一步是 stream mode 的开关,**默认是"事件回调",必须改**
2. 点 "+ 添加事件" 搜索并添加 `im.message.receive_v1` (接收消息 v1)
3. 也可以加 `im.chat.member.bot.added_v1` 等,业务侧目前只处理 receive

### 版本管理与发布 → 创建版本
1. 填可用范围(自建应用一般"全员可见")
2. 提交发布;企业有审核流程的需要管理员通过

## 3. 把机器人加到群

- 在飞书群里:`@小妹` 或 `加机器人` → 找你刚创建的应用名 → 添加
- 或者群设置 → 群机器人 → 添加 → 选自建应用

> 单聊也可以:直接搜应用名加好友。

## 4. 在本服务的 "设置" 页填配置

- 打开 `https://<NAS-IP>:8443/#tab=settings`
- 找到 **🪶 飞书机器人 (Stream Mode)** 卡片
- 勾选 "启用飞书长连接"
- 填 **App ID**,粘 **App Secret**
- 可选填白名单 (`chat_id` 是 `oc_xxx`,`open_id` 是 `ou_xxx`,一行一个)
- 保存

后端 `services/feishu.py` 的 **supervisor 任务每 5s 轮询配置**,检测到 enabled 翻转或凭证变化就重启 WebSocket 线程。**无需重启服务**。

## 5. 拿 chat_id / open_id (做白名单用)

把机器人加到群里随便发一句 → 看后端日志 (诊断页 → "服务端" 来源):

```
[INFO] feishu from=ou_abc123... chat=oc_def456... text='...'
```

`ou_xxx` 是发言者的 `open_id`,`oc_xxx` 是群的 `chat_id`。

或者去飞书开放平台的 **开发文档 → API 调试台** 调 `/im/v1/chats` 列群。

## 6. 在群里 @机器人 试试

```
@小库 充电宝在哪
@小库 我刚拿了卷尺
@小库 我发烧了家里有什么药
```

回复用 `text` 消息类型,带 Markdown 风格 `*` 高亮和 `_斜体_`。要更花哨可以改 [`services/feishu.py:_send_text`](../../backend/app/services/feishu.py) 用 `msg_type="post"` 或 `"interactive"` 发卡片。

## 工作原理

```
FastAPI startup
   └─ feishu.start()
      └─ asyncio.create_task(_supervisor)            # 每 5s 检查配置
         └─ enabled 且凭证有效?
              └─ threading.Thread → _run_ws_client   # lark 阻塞 API 必须用线程
                  └─ lark.ws.Client(app_id, app_secret).start()   # 阻塞,内部维持心跳
                     └─ event_handler.register_p2_im_message_receive_v1(_handle_message_event)
                         └─ 收到消息 → asyncio.run_coroutine_threadsafe(_run_intent, main_loop)
                             ├─ parse_intent (LLM)
                             ├─ execute_intent (DB)
                             └─ _send_text (lark.im.v1.message.create)
```

主线程是 FastAPI 的 asyncio 事件循环。lark 的 WebSocket 客户端 `client.start()` 是**阻塞**的所以放线程里;它收到消息后通过 `run_coroutine_threadsafe` 把工作甩回主循环用 async `parse_intent`,结果回流到线程后调同步的 SDK 发回复。

## 故障排查

| 现象 | 检查项 |
|---|---|
| `feishu: lark-oapi 未安装, 跳过` | `pip install lark-oapi` 或重新 `./start.sh --build`(已加进 requirements.txt) |
| 机器人不响应消息 | 看后端日志有没有 `feishu WS connecting` 行;没有就是 supervisor 还没识别到 enabled |
| `feishu WS crashed` | 多半是 App ID / Secret 不对,或者事件订阅没切到 "长连接" |
| 在群里 @ 没反应 | 权限里有没有 `im:message.group_at_msg`;事件里有没有 `im.message.receive_v1` |
| 收到消息但不回 | 看 SDK error 日志;权限里要有 `im:message` 才能 send |

## 参考

- [长连接接入文档](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/reference/event-subscription-guide/long-connection-mode)
- [lark-oapi Python SDK](https://github.com/larksuite/oapi-sdk-python)
- [接收消息事件 `im.message.receive_v1`](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)
- [发送消息 API `/im/v1/messages`](https://open.feishu.cn/document/server-docs/im-v1/message/create)
- [消息内容结构 (text / post / interactive)](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json)
