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
| GET | `/api/transactions?q=&action=&location_id=&since=&until=&limit=` | 全局流水筛选 (每条带 `undoable` + `undo_note`) |
| GET / POST / PATCH / DELETE | `/api/locations[/{id}]` | 位置 CRUD (含 home / room / 容器 / 家具) |

## 语音 / AI

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/voice/intent` | `{text, context?}` → IntentResult(含 candidates / recommendations / operations) |
| POST | `/api/voice/apply` | `{text, plan_id, decisions[]}` → 执行用户确认过的方案, **单事务** |
| POST | `/api/voice/transcribe` | 音频上传, Whisper 转写(可选) |

### 方案 (plan) 与执行 (apply)

`/api/voice/intent` 传 `context: {plan_only: true}` → **只解析不落库**,返回
`stage="plan"` + `plan_id`,`operations[]` 每条带:

| 字段 | 含义 |
|---|---|
| `options[]` | 可落到的目标。`key` = `"new"` / `"skip"` / `"i:<item_id>"`;`kind` = `exact`/`fuzzy`/`new` |
| `selected` | 预选的 key。**模糊命中一律预选 `new`** |
| `allow_new` | 是否有"新建"选项(取出/用完为 false) |
| `force_new` | 用户说了"新增/新建/添加" → 不做同名合并 |
| `reason` | 这么预选的原因,给用户看 |
| `pending` | true = 待确认。`find` 只读当场查完,为 false |

`/api/voice/apply` 的 `decisions[]`:`{intent, option_key, new_item_name?, location_id?,
location_name?, quantity}` → 返回 `stage="applied"`。

服务端 `voice.confirm_before_apply` 是最终裁决权:关掉时即使传了 `plan_only` 也直接执行。
群机器人从不传这个字段。

## 操作复核 / 回撤

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/revise/undo` | `{transaction_id}` → 硬回滚一条流水 (库存恢复到操作前) |
| POST | `/api/revise/redirect` | `{transaction_id, target}` → 撤销后改到正确目标重做, 单事务 |

`undoable` / `undo_note` 由服务端算,判据与 `/api/revise/undo` 的护栏一致:必须是
`take_out/put_in/consume`、是**该物品的最后一条**流水、物品还在。前端只拿到最近 N 条,
自己判断不了第二条。新建产生的那条 `put_in` 被回撤时,物品档案一并删除。

## 配置 / 诊断 / 审计

| Method | Path | 说明 |
|---|---|---|
| GET / PATCH | `/api/settings` | 运行时配置 (API key / 加签秘钥等敏感字段脱敏返回) |
| POST | `/api/settings/test-llm` | 测试当前 LLM 配置 |
| GET | `/api/diag` | 完整诊断信息 (counts, LLM 配置, Python 版本, **飞书长连接健康状态**...) |
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
