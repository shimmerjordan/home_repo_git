# 微信小程序可行性调研报告 + 上架计划

> 调研对象: 把「语音仓储管家」做成**纯本地、无后端服务器**的微信小程序 (个人开发者)。
> 硬约束: 不影响现有「Vue 前端 + FastAPI 后端」前后端分离的编译与使用。
> 结论先行: **可行,但不是"打包"而是"重写数据/业务层"**;3D 与语音有现成方案,整体属**中等工作量**。

---

## 0. 结论速览

| 模块 | 可行性 | 说明 |
|---|---|---|
| 数据层 (物品/位置/流水/审计) | ⚠️ **需重写** | 小程序无 SQLite、无 Python;用 JS + 本地存储重建数据层 |
| 本地存储容量 | ⚠️ **受限** | `wx.setStorage` 共 **10MB**(单 key 1MB);文件系统共 **200MB** |
| 3D 可视化 | 🟡 中 | 支持 WebGL,Three.js 需用**移植版**(无 `window/document`) |
| 语音 | 🟢 易 | 用官方**「微信同声传译」插件**(流式 ASR + TTS),免自建后端 |
| LLM 意图解析 | 🟢 易 | `wx.request` 直连 OpenAI 兼容 API(需配域名白名单) |
| WebDAV 备份 | 🟢 易 | `wx.request` 直连 WebDAV,**复用本项目的备份包格式**跨端互通 |
| 群机器人 (钉钉/TG/飞书) | ❌ 不适用 | 依赖常驻后端;小程序端无法承载,属"小程序确实做不到"的部分 |
| 个人开发者上架 | 🟢 可 | 认证 **¥30/年**,审核 7-10 工作日;无微信支付/直播(本项目用不到) |

**一句话**: 仓储 CRUD + 备份 + LLM + 语音都能在纯本地小程序里跑;群机器人因为本质是服务端常驻进程,留在 NAS 版即可。小程序作为**第三端**与现有前后端**并存**,互不影响。

---

## 1. 现状架构 vs 小程序约束 (逐项对照)

| 现状 (NAS 版) | 小程序约束 | 迁移方案 |
|---|---|---|
| FastAPI (Python) 常驻后端 | **不能跑 Python/常驻服务** | 业务逻辑用 JS 重写,跑在小程序前端 |
| SQLite `storage.db` | **无原生 SQLite** | 见 §2 本地数据层 |
| `config.json` 设置 | `wx.setStorage` | 设置存 storage(< 1MB,够用) |
| REST `/api/*` | 无自有 HTTP server | 调用本地 JS 模块,不走网络 |
| Web Speech API 语音 | **无此 API** | 微信同声传译插件 / `RecorderManager` |
| Three.js (浏览器) | WebGL ✅ 但无 DOM | Three.js 移植版 + `<canvas type="webgl">` |
| nginx HTTPS 自签证书 | **必须备案合法域名 + 可信证书** | 仅 LLM/WebDAV 外连需合法域名;本地数据无需联网 |
| LLM 外连 (httpx) | `wx.request` + **域名白名单** | 把 LLM/WebDAV 域名加入「合法域名」 |

---

## 2. 纯本地无后端架构方案

整体思路: **把 FastAPI 后端的"模型 + 业务"用 JS 在小程序端原样重建**,数据落在小程序本地存储。
现有后端 (`backend/app/models.py` / `services/inventory.py` / `services/audit.py`) 就是规格说明书,逐一翻译即可。

### 2.1 本地数据层

小程序存储能力:
- `wx.setStorageSync(key, val)`: 同步键值,**单 key ≤ 1MB,总计 ≤ 10MB**。
- `FileSystemManager` (`wx.getFileSystemManager()`): 本地文件,**总计 ≤ 200MB**;适合大数据与导入导出。
- **无 SQLite**(官方社区多年未提供)。

推荐方案 (按数据量分层):
- **小型部署 (几百件物品)**: 全部用 storage —— `locations` / `items` / `transactions` / `audit` 各存一个 JSON key,
  内存里维护索引(按名称/分类/标签搜索)。简单、够快。
- **大型部署 / 留足余量**: 主数据写 `FileSystemManager` 的 JSON 文件(走 200MB 配额),storage 只放索引与设置。
- 可选: 引入社区纯 JS 的「本地数据库」(基于 storage 模拟,如 lovefield / 自研 KV),但多数场景 JSON + 内存索引足矣。

**数据兼容(关键)**: 小程序的数据 schema 与本项目的逻辑 JSON 导出**对齐**(字段同 `serialize_item / serialize_location / serialize_transaction / audit.serialize`)。
于是 NAS 版导出的备份包 (`data/*.json`) 可直接被小程序导入,小程序的导出也能被 NAS 版导入 —— **跨端互通**。

### 2.2 WebDAV 备份 (复用本项目格式)

小程序端用 `wx.request` 直接发 WebDAV 的 `PUT` / `PROPFIND` / `GET` / `DELETE`(WebDAV 就是带几个自定义 method 的 HTTP):
- **直接复用本次实现的备份包格式**(`manifest.json` + `data/*.json` + `config.json`),
  zip 用纯 JS 库 (如 `jszip` 的小程序移植),加密用 `crypto-js` (AES) 对齐后端的 AES-GCM 协议(或简化为 AES-CBC,在两端统一)。
- 这样 **NAS 版与小程序版的备份可互相恢复**,一份数据两端可用。
- WebDAV 域名需加入小程序「合法域名」(见 §4);坚果云/Nextcloud 等均为可信 HTTPS 域名,满足要求。

### 2.3 3D 可视化

- 小程序 2.7.0+ 支持 WebGL;`<canvas type="webgl" id="c">`。
- Three.js 在小程序里需**移植版**(社区有 `threejs-miniprogram` 等),因为小程序无 `window` / `document` / DOM。
- 现有 [`Scene3D.vue`](../frontend/src/components/Scene3D.vue) 与 [`furnitureMesh.js`](../frontend/src/composables/furnitureMesh.js) 的几何/布局算法是**纯计算**,可几乎原样移植;
  只需替换"取 canvas、建 renderer、绑定触摸事件"这层 DOM 适配。
- 工作量中等;若想快速 MVP 可先不做 3D,用 2D 列表/平面图替代。

### 2.4 语音

- 小程序**没有** Web Speech API。改用官方**「微信同声传译」插件**:在公众平台「插件」里添加,提供
  **流式语音识别 (ASR) + 文本翻译 + 语音合成 (TTS)**,**无需自建后端**,个人小程序也能用。
- 录音用 `wx.getRecorderManager()`,把音频喂给插件做识别;TTS 用插件合成后 `wx.createInnerAudioContext()` 播放。
- 现有语音**状态机**逻辑 ([`useVoice.js`](../frontend/src/composables/useVoice.js)) 可移植,只换底层 ASR/TTS 实现。

### 2.5 LLM 意图解析

- `wx.request` 直连任意 OpenAI 兼容 `/v1/chat/completions`(与现有 [`llm/client.py`](../backend/app/llm/client.py) 同协议)。
- 把意图解析的 prompt 构造 ([`llm/intent.py`](../backend/app/llm/intent.py)) 翻译成 JS。
- **必须**把 LLM 服务域名加入小程序「合法域名」白名单。注意 API key 会落在客户端 —— 个人自用可接受,公开分发需用代理(但代理=后端,与"纯本地"冲突,故定位为**自用工具**)。

---

## 3. 不影响现有前后端分离 (工程隔离方案)

小程序作为**独立第三端**,与现有 `frontend/` + `backend/` **并存**,互不干扰:

```
repo_git/
  backend/          # 现有 FastAPI —— 原样保留,继续 docker compose
  frontend/         # 现有 Vue —— 原样保留,继续 vite build / nginx
  miniprogram/      # 【新增】微信小程序,独立 project.config.json + 独立构建/上传
  shared/           # 【可选】两端共享的 schema / 常量 (JSON Schema 或 .d.ts),避免逻辑漂移
```

- `miniprogram/` 用微信开发者工具独立打开、独立上传发布,**不进入** `docker-compose.yml` / `start.sh` / `vite` 流程。
- `.gitignore` 已忽略 `node_modules/` / `dist/`;小程序的 `node_modules` 同样不影响主仓库构建。
- 共享层只放**纯数据契约**(字段定义),不放运行时代码,从而 NAS 版与小程序版各自编译、各自演进,只靠备份包格式对齐保证数据互通。
- 结论: **现有"前后端分离"的开发、编译、部署、使用方式完全不变。**

---

## 4. 最低成本上架教程 (个人开发者)

### 费用清单

| 项 | 费用 |
|---|---|
| 注册小程序账号 | **¥0** |
| 个人主体认证 | **¥30 / 年**(一年一审;不认证则搜不到、不能用分享) |
| 服务器 | **¥0**(纯本地,无需服务器) |
| 域名 | **¥0**(若 LLM/WebDAV 用第三方已备案的可信域名);自建服务才需买域名+备案 |
| **合计** | **约 ¥30 / 年** |

### 步骤

1. **注册**: [mp.weixin.qq.com](https://mp.weixin.qq.com) → 立即注册 → 选「小程序」→ 用未注册过公众平台的邮箱。
2. **个人认证 (¥30/年)**: 后台「设置 → 基本设置」按引导做个人身份认证(身份证 + 微信扫脸)。完成后才可被搜索/分享。
3. **填基本信息**: 名称、头像、简介、服务类目(选「工具」类即可,个人可选类目有限)。
4. **拿 AppID**: 「开发 → 开发管理 → 开发者ID」记下 AppID。
5. **装工具**: 下载[微信开发者工具](https://developers.weixin.qq.com/miniprogram/dev/devtools/download.html),用 AppID 新建/导入 `miniprogram/` 项目。
6. **配合法域名**: 「开发 → 开发设置 → 服务器域名」把 LLM API 域名、WebDAV 域名加入 **request 合法域名**(必须 HTTPS、可信证书;**自签证书不可用**)。开发期可在工具里勾「不校验合法域名」临时绕过。
7. **加同声传译插件**(若做语音): 「设置 → 第三方设置 → 插件」搜索「微信同声传译」添加,在 `app.json` 声明。
8. **本地存储兜底**: 代码里对 10MB/200MB 配额做监控与导出提示,避免超限。
9. **上传**: 开发者工具点「上传」→ 填版本号与备注 → 进入「版本管理」。
10. **提审 + 发布**: 后台「管理 → 版本管理 → 提交审核」→ 审核约 **7-10 个工作日** → 通过后点「发布」。

### 个人号注意

- **无微信支付、无直播、无部分高级开放能力** —— 本项目都用不到,影响极小。
- 一年一审: 到期前完成年审,否则搜索/分享失效。
- 纯本地工具类、无内容分发,审核风险低;但**类目与简介**要如实(写"个人/家庭物品收纳管理工具")。

---

## 5. 工作量与风险

| 阶段 | 内容 | 工作量 | 风险 |
|---|---|---|---|
| **MVP** | 本地数据层(物品/位置 CRUD + 搜索)+ WebDAV 备份/恢复 | 中 | 低 |
| 阶段二 | LLM 意图解析 + 同声传译语音 | 中 | 中(域名白名单、插件配额) |
| 阶段三 | Three.js 移植 3D | 中-高 | 中(移植版兼容性、触摸交互) |
| 不做 | 群机器人(钉钉/TG/飞书) | — | 本质需后端,留 NAS 版 |

主要风险点:
- **存储上限**: 10MB storage + 200MB 文件,海量物品需分页/分片;给出导出提示。
- **API key 落客户端**: 定位为个人自用工具;公开分发需后端代理(与纯本地冲突)。
- **3D 移植**: Three.js 小程序移植版滞后于官方版本,部分特性需降级。

---

## 6. 建议的落地计划

1. 先复用本次的**备份包格式**作为两端数据契约(已落地,见 [`backup.md`](backup.md))。
2. 建 `miniprogram/` 脚手架,实现**本地数据层 + 物品/位置 CRUD**,用「导入 NAS 备份包」验证数据兼容。
3. 接 WebDAV 备份/恢复(直接对齐本项目格式),实现真正的跨端一份数据。
4. 加 LLM + 同声传译语音。
5. (可选) Three.js 移植 3D。
6. 个人认证 → 上传 → 提审 → 发布。

> 如需,我可以按此计划接着实现 **MVP(本地数据层 + 备份)** 的小程序代码 —— 它与现有前后端完全隔离,不影响现有编译与使用。

---

## 参考来源

- 小程序 WebGL / Three.js: [微信开放社区 - 小程序支持 webgl/three.js](https://developers.weixin.qq.com/community/develop/doc/5c3b5992a9bd226c968a4840975d119b)
- 同声传译插件: [微信同声传译 官方文档](https://developers.weixin.qq.com/miniprogram/dev/platform-capabilities/extended/translator.html)、[Tencent/Face2FaceTranslator](https://github.com/Tencent/Face2FaceTranslator)
- 存储与文件系统: [小程序存储详解](https://developers.weixin.qq.com/community/develop/doc/0002685282cc3830d7122b0ba6b409)、[文件系统](https://developers.weixin.qq.com/minigame/dev/guide/base-ability/file-system.html)
- 个人认证与费用: [个人小程序认证流程](https://developers.weixin.qq.com/community/develop/article/doc/0006c437da87b885b7900de966c013)、[认证费 个人 30 元](https://zhuanlan.zhihu.com/p/716290833)
