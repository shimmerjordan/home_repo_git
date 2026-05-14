# REST API 速查

完整 OpenAPI 文档: `https://<host>:8443/docs` (FastAPI 自动生成,nginx 反代)

## 核心 CRUD

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET / POST / PATCH / DELETE | `/api/items[/{id}]` | 物品 CRUD |
| GET | `/api/items/export.csv` | CSV 导出 (含 home 前缀的完整 location_path) |
| GET | `/api/items/import-template.csv` | 导入模板 |
| POST | `/api/items/import?mode=upsert\|append\|replace` | CSV 导入,自动按层级建缺失位置 |
| POST/GET | `/api/items/{id}/transactions` | 单品流水 |
| GET | `/api/transactions?q=&action=&location_id=&since=&until=&limit=` | 全局流水筛选 |
| GET / POST / PATCH / DELETE | `/api/locations[/{id}]` | 位置 CRUD (含 home / room / 容器 / 家具) |

## 语音 / AI

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/voice/intent` | `{text, context?}` → IntentResult(含 candidates / recommendations) |
| POST | `/api/voice/transcribe` | 音频上传, Whisper 转写(可选) |

## 配置 / 诊断 / 审计

| Method | Path | 说明 |
|---|---|---|
| GET / PATCH | `/api/settings` | 运行时配置 (API key / 加签秘钥等敏感字段脱敏返回) |
| POST | `/api/settings/test-llm` | 测试当前 LLM 配置 |
| GET | `/api/diag` | 完整诊断信息 (counts, LLM 配置, Python 版本...) |
| GET | `/api/logs?since_id=&level=` | 增量拉取后端日志 |
| POST | `/api/logs/client` | 前端日志上报 |
| GET | `/api/audit?entity_type=&action=&q=&since=&until=&limit=` | 审计日志 (Git-blame 风格) |

## 机器人 Webhook

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/dingtalk/webhook?timestamp=&sign=` | 钉钉自定义机器人入站消息 |
| GET / POST | `/api/dingtalk/test` | 钉钉配置 liveness check |

Telegram 和飞书走出站长连接,无 HTTP 端点。

## 数据持久化

```
./data/
├── storage.db       # SQLite 数据库 (items / locations / transactions / audit_logs)
├── config.json      # LLM / 语音 / 机器人运行时配置 (含 API key,自行注意权限)
└── certs/
    ├── server.crt   # 自签证书 (持久化, 不会每次重建)
    └── server.key
```

备份只需要 tar 这一个目录。
