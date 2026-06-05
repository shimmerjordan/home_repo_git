# 数据备份 (WebDAV)

把全部数据异地备份到任意 WebDAV 网盘 (坚果云 / Nextcloud / 群晖 Drive / InfiniCLOUD 等),
支持**选择性备份**、**GFS 分层保留**、**定时自动备份**、**AES-256 加密**与**恢复**。
入口: 前端顶部「☁ 备份」标签页。

## 备份了什么

| 组件 | 内容 | 说明 |
|---|---|---|
| `settings` | `config.json` | app 设置 (含 LLM key / 机器人 token —— 建议开加密) |
| `inventory` | `db/storage.db` + `items.json` + `locations.json` | 物品 + 位置/房间。**裸 SQLite 快照是恢复主道** |
| `transactions` | `transactions.json` | 操作流水 (可读导出;数据本就在裸 DB 内) |
| `audit` | `audit.json` | 审计日志 (同上) |
| `logs` | `logs/app.log*` | 系统日志 |

> 设计取舍: 以**整库 SQLite 快照**为恢复主道 —— 以后加表/加字段自动覆盖、零维护;
> JSON 是可读导出 + 跨端 (如小程序) 导入用。详见 [`backend/app/services/backup.py`](../backend/app/services/backup.py)。

## 备份包格式

`backup-YYYYMMDD-HHMMSS.zip` (加密时为 `...enc.zip`),内部:

```
manifest.json   # 版本 / 时间 / 组件 / 各文件 sha256
config.json     # settings
db/storage.db   # SQLite 一致性快照 (sqlite3 .backup())
data/*.json     # 逻辑导出
logs/app.log*   # 日志
```

加密: PBKDF2(SHA256, 20万次) 派生密钥 + AES-256-GCM,文件头 `VSBK1 | salt(16) | nonce(12) | 密文`。
**口令丢失 = 无法恢复加密备份**,请妥善保管。

## GFS 分层保留

每个**日 / 周 / 月**桶各保留最近一个,日/周/月分别保留最近 `keep_daily` / `keep_weekly` / `keep_monthly` 个,
其余在每次备份后自动删除。无法解析时间戳的文件名一律保留 (不误删)。纯函数 `apply_gfs`,可单测。

## 调度

`schedule`: `manual` (仅手动) / `hourly` / `daily` / `weekly` (周一)。`daily`/`weekly` 在 `hour` 点触发。
需勾选「启用调度」且非 `manual` 才会自动跑。调度器与 Telegram/Feishu 同样是 asyncio 任务,改配置即时生效 (无需重启)。

## 恢复

从备份历史点「恢复」,或「从文件恢复」上传备份包。可选恢复内容:
- `settings` → 写回 `config.json` 并热重载
- `database` → `engine.dispose()` 后用快照覆盖库文件,再跑迁移 (与启动共用 `migrations.run_all`,旧库自动升级)
- `logs` → 写回日志目录

**恢复前会自动把当前 DB + config 本地快照到 `data/_pre_restore_<时间戳>/`** 以便回滚。
恢复会覆盖对应数据,务必确认。

## REST API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/backup/settings` | 配置 (密钥脱敏) + 组件目录 + 能力 |
| PATCH | `/api/backup/settings` | 改配置 (空密码/口令 = 保持不变) |
| POST | `/api/backup/test` | 测试 WebDAV 连接 |
| POST | `/api/backup/run` | 立即备份 (可传 `components` 覆盖) |
| GET | `/api/backup/list` | 远程备份列表 |
| GET | `/api/backup/download/{name}` | 下载备份包 |
| DELETE | `/api/backup/{name}` | 删除备份 |
| POST | `/api/backup/restore` | 从远程备份点恢复 |
| POST | `/api/backup/restore-upload` | 上传备份包恢复 |

## 依赖与降级

新增依赖 `webdav4` (WebDAV 客户端) 与 `cryptography` (加密),均**可选降级**:缺库时后端照常启动,
仅对应能力不可用 (备份页会提示)。旧 Docker 镜像不重建也能跑。

## 各网盘填法速查

| 网盘 | 服务器地址 | 账号 / 密码 |
|---|---|---|
| 坚果云 | `https://dav.jianguoyun.com/dav/` | 邮箱 / **应用密码**(账户安全里生成) |
| Nextcloud | `https://你的域名/remote.php/dav/files/用户名/` | 用户名 / 密码(建议应用专用密码) |
| 群晖 | `https://NAS:5006/` (开启 WebDAV Server 套件) | DSM 账号 / 密码 |
| InfiniCLOUD | `https://你的子域.teracloud.jp/dav/` | 账号 / 应用密码 |
