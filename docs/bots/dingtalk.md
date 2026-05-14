# 钉钉机器人接入

> **类型**: inbound webhook + 加签校验
> **需要公网?**: 是 (钉钉服务器 → NAS)
> **国内可用?**: ✅

## 1. 在钉钉创建自定义机器人

1. 群设置 → **群机器人** → **添加机器人** → **自定义** (通过 Webhook 接入)
2. 机器人名字随意,头像可选默认
3. **安全设置**:勾选 **"加签"**,复制 `SEC...` 开头的密钥 → 这是 `sign_secret`
   - 也可以选 IP 白名单,但**加签更稳**(NAS 公网 IP 可能变)
4. **接收地址 / Outgoing URL**:填本服务的公网入口 + `/api/dingtalk/webhook`
   - 例如 `https://your-nas.example.com:8443/api/dingtalk/webhook`
5. 完成 → 把机器人加到群

## 2. 在本服务的 "设置" 页填配置

- 打开 `https://<NAS-IP>:8443/#tab=settings`
- 找到 **🤖 钉钉机器人** 卡片
- 勾选 "启用钉钉 Webhook"
- 粘贴上一步的 `SEC...` 到 **加签秘钥**
- 可选填白名单(staffId / 昵称,一行一个)
- 保存

## 3. 让钉钉服务器能访问你的 NAS

钉钉的 Outgoing 是从钉钉的服务器主动调用 NAS 的 URL,所以需要**公网可达**。三种常见方案:

| 方案 | 复杂度 | 说明 |
|---|---|---|
| 路由器端口转发 | 低 | 把外网 8443 转到 NAS 的 8443;需要公网 IP(部分宽带是 NAT 后,不行) |
| frp / [cloudflared](https://github.com/cloudflare/cloudflared) 反向隧道 | 中 | 无需公网 IP,域名指向隧道入口 |
| [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) | 中 | 一行命令暴露;免费,有流量限制 |

无论哪种方案,确保 HTTPS 证书是钉钉信任的(自签证书会被拒)。

> **无公网?** 看 [Telegram](telegram.md) 或 [飞书](feishu.md),都是 NAS 主动连出的方案。

## 4. 在群里 @机器人 试试

```
@小库 充电宝在哪
@小库 我刚拿了卷尺
@小库 我发烧了家里有什么药
```

机器人会用 Markdown 回复一段总结 + 表格,自动找到物品、记录流水、给出建议。

## 5. 参考文档

- [自定义机器人接收消息](https://open.dingtalk.com/document/orgapp/receive-message)
- [自定义机器人安全设置 (加签算法)](https://open.dingtalk.com/document/orgapp/customize-robot-security-settings)
- [群消息消息类型说明 (Markdown / Text / ActionCard...)](https://open.dingtalk.com/document/orgapp/message-types-and-data-format)

## 安全提示

- **不要**在公网部署时把 `sign_secret` 留空 — 没有签名校验,任何人知道 URL 都能调你的机器人
- 白名单用 staffId(更稳)而不是昵称(可改)
- 后端会把入站 webhook 写到诊断日志,方便排查
