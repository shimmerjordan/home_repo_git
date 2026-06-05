# 语音仓储管家 — 微信小程序

纯本地、无后端服务器的微信小程序版。物品/位置 CRUD、取出存入流水、**LLM 语义助手 + 语音**、
**WebDAV 备份恢复(AES-256 加密)**、2D 平面图。数据全部存在小程序本地,与 NAS 版**通过同一种备份包格式双向互通**。

> 对应[可行性调研报告](../docs/wechat-miniprogram.md)的规划。**部署/上架教程见 [deployment-miniprogram.md](../docs/deployment-miniprogram.md)**。

## 与主仓库完全隔离

`miniprogram/` 是独立第三端,**不影响**现有 `frontend/` (Vue) 与 `backend/` (FastAPI):
- 用[微信开发者工具](https://developers.weixin.qq.com/miniprogram/dev/devtools/download.html)单独打开本目录,独立编译/上传。
- 不进入 `docker-compose.yml` / `start.sh` / `vite` 任何流程。
- 零 npm 依赖(纯原生 + 自写 zip/inflate/AES/webdav/base64),无需「构建 npm」。

## 功能

| Tab | 功能 |
|---|---|
| 助手 | 文本/语音指令 → LLM 解析意图 → 查找 / 取出 / 存入 / 消耗 / 新建;低置信度先确认 |
| 物品 | 列表 + 搜索 + 增删改 + 取出/存入 |
| 位置 | 无限层级位置增删改(家 → 房间 → 容器 …) |
| 平面图 | Canvas 2D 房间平面图 + 物品统计(有 geometry 则按坐标摆放) |
| 备份 | WebDAV 配置 + 立即备份 + 历史 + 恢复 + 删除 |
| 设置 | LLM 配置(预设/测试)+ 置信度阈值 + 备份加密口令 |

## 目录

```
miniprogram/
  app.js / app.json / app.wxss / sitemap.json / project.config.json
  utils/
    util.js     # storage(10MB) + fsStore(FileSystemManager 200MB) + uuid + base64 + basicAuth
    store.js    # 本地数据层: 大表走 fsStore, 计数器/设置走 storage; 字段对齐后端 serialize_*
    zip.js      # 纯 JS ZIP: STORED 写 + STORED/DEFLATE(inflate) 读, 与 Python zipfile 双向互通
    crypto.js   # 纯 JS SHA256/HMAC/PBKDF2/AES-256-GCM, 与后端 VSBK1 加密字节级互通
    webdav.js   # wx.request 封装 PUT/GET/DELETE (无 PROPFIND/MKCOL, 见下)
    backup.js   # 构包 (manifest+config+data/*.json) / 加密 / 上传 / index.json 列表 / 恢复
    llm.js      # OpenAI 兼容 chat (wx.request) + 宽松 JSON 解析
    intent.js   # 语义意图解析 + 本地执行 (移植 backend/app/llm/intent.py)
  pages/
    assistant/ items/ item-edit/ locations/ plan/ backup/ settings/
```

## 跨端数据互通(已全部验证)

备份包是与 NAS 版一致的 ZIP:`manifest.json` + `config.json` + `data/*.json`(小程序无 SQLite,以 JSON 为权威,`manifest.source="miniprogram"`)。

| 方向 | 状态 | 说明 |
|---|---|---|
| 小程序 ↔ 小程序 | ✅ | 自身备份/恢复闭环(含加密) |
| 小程序备份 → NAS 版恢复 | ✅ | 后端 `restore` 从 `data/*.json` 重建库(JSON 回退) |
| NAS 版备份 → 小程序恢复 | ✅ | 小程序 `zip.js` 内置 DEFLATE inflate,可解后端压缩包 |
| 加密备份双向 | ✅ | AES-256-GCM + PBKDF2-SHA256(20万次),同口令字节级互通 |

## 已知限制(MVP)

- **WebDAV 受微信限制**:`wx.request` 不支持 `PROPFIND`/`MKCOL` → 列表用远程 `index.json`(自动维护);目标文件夹需**预先手动创建**。
- **语音**依赖「微信同声传译」插件(需在 mp 后台添加);未启用时语音按钮隐藏,文本指令始终可用。
- **LLM api_key** 存本地,定位个人自用;发布需把域名加入合法域名。
- **3D**:当前为 2D 平面图(Canvas 2D);完整 Three.js 3D 属后续阶段。
- PBKDF2 20 万次:已优化(缓存 HMAC ipad/opad 块状态,2× 加速;node 实测 ~0.4s),手机上约 1 秒级,备份操作不频繁,可接受。

## 测试(node)

核心纯逻辑用 storage/fs shim 可脱离微信在 node 跑,已验证:
- `zip.js` 与 Python `zipfile` **双向**互通(STORED 写、DEFLATE 读;含中文/emoji,CRC 正确);
- `crypto.js` 的 SHA256/HMAC/PBKDF2/AES-GCM 与 node crypto 及 **Python 后端 VSBK1 双向**字节级一致;
- `store.js` CRUD/搜索/路径/审计 + fsStore 路由;
- `intent.js` 意图解析 + 执行(find/take_out/put_in/consume/create + 低置信度确认);
- 小程序(加密)构包 → NAS 后端 `restore` → 数据 + 层级 + 流水 + 审计完整还原。
