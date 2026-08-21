# 演进路线 / 改动历史

```
v0.1  最小可用
   ├─ docker compose 起 FastAPI + Vue + SQLite
   ├─ OpenAI 兼容 LLM client (tool / JSON 双模式)
   ├─ 关键词预筛 + 摘要 → LLM → 置信度
   └─ 浏览器 SpeechRecognition + speechSynthesis

v0.2  iOS 适配 + 体验改进
   ├─ 自签 HTTPS:解决 Safari 麦克风限制
   ├─ 物品页:位置树侧栏 + 备注内联 + CSV 导入导出
   ├─ 语音页:hero 渐变 + 圆形按钮 + 候选项卡
   └─ 流水页:全字段筛选 + 统计 + 导出

v0.3  统一 + 文件夹化 + 可观测
   ├─ 单端口 8443:nginx 反代 /api + /docs,后端 / Whisper 不再对外
   ├─ start.sh:一键启动/停止/日志/--whisper profile
   ├─ Finder 风格位置浏览器:面包屑、卡片网格、hover 操作、循环检测
   ├─ 流水筛选页 (q / action / location / 时间区间 / 导出)
   ├─ 实时频谱波形 (AudioContext + Analyser)
   └─ 诊断 & 日志页:浏览器能力自检 + 5 个测试按钮 + 前后端合并日志

v0.4  完整语音状态机
   ├─ useVoice 原语化:startWakeListening / captureUtterance / listenYesNo / speak
   ├─ 唤醒监听轻量化:只取 final 片段, buffer ≤80 字, 命中即停, 切换单次 SR
   ├─ 大麦克风按钮 (主操作) + 唤醒监听小按钮 (副开关)
   ├─ 状态机:idle → command → confirm-text → processing → confirm-action → speaking
   ├─ LLM 前确认:省 token,语音/按钮双通道,可在设置关闭
   └─ 低置信度确认 (默认阈值 0.5):语音/按钮双通道,听不清自动重问

v0.5  3D 立体可视化
   ├─ Three.js 渲染房间 / 容器 / 物品,支持多层货架、旋转、家具组合 mesh
   ├─ 2D SVG 平面图编辑器:拖拽家具、贴边吸附、多边形房间、锁定模式
   ├─ 物品 cube 挂在容器 mesh 上(组合旋转、缩放、移动)
   ├─ 语音找到物品自动推进相机 + 多目标高亮 + 占位淡出
   └─ Git-blame 风格审计日志 (字段级 diff)

v0.6  多家 + 钉钉 + Telegram + 飞书
   ├─ "家" 作为最顶层概念 (我家 / 老家 / 父母家)
   ├─ 数据自动迁移:旧数据 0 损失、ID 不变,自动建 "我家" 并 reparent
   ├─ 钉钉 inbound webhook (加签 + 白名单 + Markdown 表格回复)
   ├─ Telegram 长轮询 (NAS 主动连出,无需公网 IP)
   ├─ 飞书 Lark Stream Mode (WebSocket 长连接,国内可用,无需公网)
   ├─ Hash 路由 (#tab=xxx),刷新页面不丢 tab
   ├─ 漂亮 3D 家具:床/沙发/椅/桌/冰箱/马桶/电视/盆栽... 都不再是单个立方体
   ├─ 30s 沉默自动确认识别文本
   ├─ iPad RMS 时域算法 + AudioContext 手势同步 resume
   └─ 全屏切换:双击退出防误触

v0.7  WebDAV 数据备份
   ├─ 全量备份到任意 WebDAV (坚果云/Nextcloud/群晖...):设置 + 物品 + 位置 + 流水 + 审计 + 日志
   ├─ 选择性备份 (按组件勾选) + 裸 SQLite 快照为恢复主道 (加表/加字段零维护)
   ├─ GFS 分层保留:日/周/月各保留最近 N 个,自动清理过期备份
   ├─ 定时自动备份 (hourly/daily/weekly) + 手动「立即备份」,asyncio 调度,改配置即时生效
   ├─ AES-256 (PBKDF2 + GCM) 口令加密,可选;cryptography/webdav4 缺库优雅降级
   ├─ 恢复:从远程备份点或上传包还原,恢复前自动本地快照,与启动共用迁移
   ├─ 重构:迁移函数抽到 migrations.py、密钥脱敏抽到 services/secrets.py (启动/恢复/两路由共用)
   └─ 新增独立「☁ 备份」标签页 (BackupPanel.vue)

v0.8  可信任的 AI + 单容器 (当前)
   ├─ 落库前逐条确认: AI 只出方案 (stage=plan), 每个单品一个候选下拉 +
   │  「新建, 名字自己填」+ 位置 + 跳过; 确认后 /api/voice/apply 单事务执行
   ├─ **模糊命中不再自动加库存**: 说"存入洗发水"默认新建, 不再悄悄记到"洗手液"头上
   ├─ 「新增 X 到 Y」= 全新物品: force_new 贯穿 prompt / tool schema / 后端兜底,
   │  不做同名合并 (想合并在下拉里挑已有物品)
   ├─ 回撤按钮进「近期取放记录」与「流水」页; undoable/undo_note 服务端算,
   │  撤不了的按钮置灰并把原因写进 title
   ├─ AI 准确度: max_tokens 512→4096 + 截断检测与自动重试 (512 时 4 物品语句
   │  stop_reason=max_tokens, operations 整批丢空 —— 多物品不准的头号原因);
   │  按物品分段检索 (多物品不再互相挤候选名额); 库存为 0 的档案对补货可见;
   │  单条/批量统一走一条路; 未知 op 不再静默丢弃; thinking 关闭时钉死 tool_choice
   ├─ 准确度评测台 backend/eval/: 32 条标注语句 + 易混夹具, 分类出准确率,
   │  cassette 录制/回放可离线复现 (CI 靠它做零成本回归); backend/tests/ 69 条测试
   ├─ 飞书长连接卡死修复: 收回重连主权 (auto_reconnect=False + 自建 loop),
   │  判死看连接而不是看线程, 连接数超限走 5~30 分钟长退避, 日志按模板限流,
   │  /api/diag 暴露飞书健康状态
   ├─ 前后端合并成单容器 storage-app (nginx + uvicorn by supervisord):
   │  端口 8080/8443 不变, ./data 布局不变, 本地 CA 不用重装
   └─ GitHub 发布流水线: 打 v* tag → 跑测试当闸门 → 构建 amd64+arm64 推 GHCR →
      建 Release (正文来自 .github/release-notes.md), 并附上版本钉死的 compose.yml。
      发布页以 compose 为中心: 下一个文件 + 写一行 .env 就能起。
      Dockerfile 改用 npm ci 保证同一 tag 重复构建产物一致; 前端固定在 BUILDPLATFORM
      原生编译, 不进 QEMU
```
