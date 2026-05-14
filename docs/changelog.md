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

v0.6  多家 + 钉钉 + Telegram + 飞书 (当前)
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
```
