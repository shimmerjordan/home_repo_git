# Changelog

早期版本 (v0.1 ~ v0.6) 及完整历史见 [GitHub Releases](https://github.com/shimmerjordan/home_repo_git/releases)。

## Unreleased

性能与功耗优化, 外加一处刷新掉 tab 的 bug 修复。

- 3D 场景改按需渲染: 空闲彻底停帧, 三重门禁 (页面可见 / 容器在视口 / keep-alive 未换出)
- 面板从 `v-show` 全量挂载改 `v-if + keep-alive` 懒挂载; 诊断页 3s / 语音页 30s 轮询绑定可见性
- 新增 `useInventoryStore` 共享物品/位置数据 (in-flight 去重 + generation 防脏读), 开机重复请求从 items×3 + locations×4 降到各 1 次
- nginx 开 gzip + `/assets/` 永久缓存 (Scene3D chunk 580KB 此前裸传)
- 后端: `transactions` 加 `item_id`/`location_id` 索引; `pending-returns` 去 N+1; `intent.py` 的 Location/Item 全表查询按 session 缓存 (flush 即失效)
- telegram / 飞书 / 备份三个后台循环关闭态零唤醒 (挂 `asyncio.Event`, 由 `reload()` 唤醒), 开启态节奏不变
- 修复: `#tab=backup` 刷新会掉回语音页 (`VALID_TABS` 漏了 `backup`)
- 清理前后端零引用代码与重复定义
- 测试 69 → 76 条; AI 准确度评测回放 32/32 100% 不变

用户可见变化: `#tab=backup` 刷新后停在备份页; 数据无变化的刷新不再重置 3D 相机; 飞书开启态改设置立即生效, 不用再等最长 15s。

## v0.8 可信任的 AI + 单容器

- 落库前逐条确认: AI 只出方案 (stage=plan), 每个单品一个候选下拉 + 「新建, 名字自己填」+ 位置 + 跳过; 确认后 `/api/voice/apply` 单事务执行
- 模糊命中不再自动加库存: 说"存入洗发水"默认新建, 不再悄悄记到"洗手液"头上
- 「新增 X 到 Y」= 全新物品, 不做同名合并 (想合并在下拉里挑已有物品)
- 回撤按钮进「近期取放记录」与「流水」页; undoable 服务端算, 撤不了的按钮置灰并说明原因
- AI 准确度: max_tokens 512→4096 + 截断检测自动重试 (多物品语句不准的头号原因); 按物品分段检索; 准确度评测台 `backend/eval/` (32 条标注语句, cassette 录制/回放可离线复现)
- 飞书长连接卡死修复: 收回重连主权, 判死看连接而不是看线程, 连接数超限走长退避
- 前后端合并成单容器 `storage-app` (nginx + uvicorn by supervisord), 端口/数据布局不变
- GitHub 发布流水线: 打 v* tag → 测试闸门 → 构建 amd64+arm64 推 GHCR → 建 Release, 附版本钉死的 compose.yml

## v0.7 WebDAV 数据备份

- 全量备份到任意 WebDAV (坚果云/Nextcloud/群晖...): 设置 + 物品 + 位置 + 流水 + 审计 + 日志
- 选择性备份 (按组件勾选) + 裸 SQLite 快照为恢复主道
- GFS 分层保留: 日/周/月各保留最近 N 个, 自动清理过期备份
- 定时自动备份 (hourly/daily/weekly) + 手动「立即备份」, 改配置即时生效
- AES-256 (PBKDF2 + GCM) 口令加密, 可选
- 恢复: 从远程备份点或上传包还原, 恢复前自动本地快照
- 新增独立「☁ 备份」标签页

---

完整版本历史 (含 v0.1 ~ v0.6) 见 [GitHub Releases](https://github.com/shimmerjordan/home_repo_git/releases) 与 git 历史。
