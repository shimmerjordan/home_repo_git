# 语音、LLM 与 iOS 注意事项

## 状态机

`idle → command → confirm-text → processing → confirm-action → speaking → idle`

1. **唤醒监听**(可选,小按钮开启)
   - 浏览器 Web Speech API 持续监听,**只检查唤醒词**(只取 final 片段、滚动 buffer ≤ 80 字、不积累 transcript、不调 AI),功耗极低
   - 命中 "小库 / 小仓 / 管家" 等任一唤醒词后立即停掉持续 SR(避免 iOS Safari 双 SR 冲突),TTS 播报 "请说",进入指令捕获
2. **指令捕获**(主操作:大圆形麦克风按钮)
   - 单次完整识别,8 秒超时
   - 也支持手动文字输入
   - 再点一次大按钮 / "闭麦" 按钮可随时中断
3. **发送前确认(省 token)** *默认开启,可在设置中关闭*
   - 弹出黄色确认卡片显示 "你说的是: XXX"
   - TTS 播报问句 + 同时启动 yes/no 单次识别(interim 早退,3.5s 超时)
   - 你可以**口头说** "确定/确认/对/是/好/行/yes/ok" 或 "取消/不对/算了/no",**也可以点按钮**
   - **30 秒沉默** 自动确认(只在 "发送前确认" 阶段;"低置信度执行确认" 不自动 yes,避免误操作)
4. **AI 解析意图** → 出方案(默认)或直接执行
5. **落库前逐条确认** *默认开启,设置页可关*
   - 会改数据的操作(取出/存入/用完/新增/删除)**一个字都不先写库**,后端只返回
     `stage="plan"` 的方案,由「最新识别结果」的确认卡片接手
   - 每条一张卡:动作徽标 + 数量 + 候选下拉(含「新建」)+ 位置 + 跳过
   - **名字相近时默认建新物品**。说「存入洗发水」而库里只有「洗手液」时预选"新建洗发水",
     洗手液只列在下面 —— 模糊匹配自动加库存是误操作的头号来源
   - 确认后 `POST /api/voice/apply` **单事务**执行,要么全成要么全不动
   - 查找/推荐是只读的不受影响;群机器人没界面可点,始终直接执行
6. **低置信度二次确认** *阈值默认 0.5, 仅在关掉上一条时生效*
   - LLM 返回 `confidence < threshold` 且是修改性操作(取出/存入)时,先不动数据,弹卡片 + 语音播报问 "是想 X 吗",同样支持口头/按钮二选一
   - 模糊匹配时给候选物品列表,可点 "选这个"
7. **需求型问答**: "我发烧了家里有什么药" 这种问句会走 `assist` 意图,返回带"用途"列的推荐表,并在 3D 视图里同时高亮所有相关物品
8. **批量操作**: 一句话多个物品时全部类型都支持混合(如 "把手表、铅笔、橡皮放进书桌1"、
   "我用完了洗手液, 拿了螺丝刀和卷尺"、"手表和铅笔在哪")。LLM 返回 `operations` 数组,
   逐条处理后汇总播报;位置支持按名称解析("书桌1" → 位置 id);部分失败时话术如实说明哪条没成

## LLM 接入(完全可配置)

- 两种接口格式,设置页可切换:
  - **OpenAI 兼容**(默认): 任何提供 `/v1/chat/completions` 的服务都行
  - **Anthropic (Claude)**: `/v1/messages`,支持 Claude 官方 API 和 **cc-trans 反代**(API Key 填 `cct-...` 客户端令牌,Base URL 填 cc-trans 地址如 `http://host.docker.internal:8787`)
- 内置预设:**OpenAI / 硅基流动 / DeepSeek / Ollama / 智谱 GLM / Claude 官方 / cc-trans**
- 运行时改 `base_url` + `api_format` + `api_key` + `model` + `temperature` + `timeout` + `max_tokens` +
  `thinking` + `effort`,**无需重启**(后两者仅 Anthropic 格式生效: `thinking` = 空/`adaptive`/`disabled`,
  `effort` = 空/`low`/`medium`/`high`/`xhigh`/`max`;留空 = 不传该字段,兼容不认 adaptive/effort 的老模型如 Haiku 4.5)
- 自动用工具调用(OpenAI `tool_calls` / Anthropic `tool_use`),模型不支持时降级为 JSON 模式
- "测试连接"按钮一键验证

### max_tokens 别调小

带思考的模型(Claude Opus/Sonnet)光 thinking 就吃掉大半输出预算,4 个物品的批量操作
实测要 490~550 output tokens。512 时会被截断,而截断的 `tool_use` input 是半截 JSON,
`operations` 整个变空 —— 用户说了四件事后端一件都没收到,却仍按"成功"往下走。
**这就是多物品操作不准的头号原因。**

| 语句 | max_tokens | stop_reason | operations |
|---|---|---|---|
| 把手表、铅笔、橡皮、订书机、计算器放进书桌1 | 512 | **max_tokens** | 5(侥幸完整) |
| 我用完了洗手液, 拿了螺丝刀和卷尺, 把两个充电器放回书桌1 | 512 | **max_tokens** | **0(全丢)** |
| 同上 | 2048 | tool_use | 4(全对) |

现在:默认 4096(上限 16384,设置页下限 2048);已存在的 `config.json` 里低于 2048
的值**载入时自动抬到 4096**(只改默认值对现有配置无效);`client.py` 检查
`stop_reason`/`finish_reason`,截断就用 4 倍预算自动重试一次。

### 加速 Tips

- **极速模式**: 设置页勾选,精简系统提示 + 减少给 AI 的候选物品数
- **轻量模型**: glm-4-flash / qwen2.5-7b-instruct / siliconflow 上的 Qwen 系列
- **关掉"发送前确认"** 省一次往返(但容易误识别就直接执行)
- **关掉"朗读 AI 结果"** 省 TTS 播放等待

## 准确度评测台 (`backend/eval/`)

32 条标注语句 + 一份带"易混对"的夹具库存(充电宝/充电器、洗手液/洗发水、螺丝刀/螺丝、
两处都有的电池、库存为 0 的抽纸),跑 `parse_intent` + `plan_operations`,逐条比对
意图 / 操作条数 / 每条落点 / 数量 / 位置,按分类出准确率。不落库(每个 case 一份内存 sqlite)。

`backend/tests` 和 `backend/eval` **不进镜像**(`Dockerfile` 只 `COPY backend/app`),必须把源码
bind mount 进去跑;镜像名不是 `docker-compose.yml` 里的 `container_name: storage-app`,而是
`build: .` 那个 service (`app`) 编译出来的 `repo_git-app`(`docker compose images` 可核实,
项目目录名不同则前缀会变):

```bash
docker run --rm -v "$PWD/backend:/src" -w /src \
  -e CONFIG_PATH=/tmp/eval-config.json repo_git-app python -m eval.run_eval

python -m eval.run_eval --replay --network none  # 离线回放录好的 cassette (CI 用这条, 顺手断网)
python -m eval.run_eval --compare             # max_tokens 512 vs 4096 对比
python -m eval.run_eval --only multi,mixed    # 只跑最难的两类
python -m eval.run_eval --case mixed-4        # 只跑一条, 调 prompt 时用
```

分类:`single / multi / mixed / confusable / force_new / restock / ambiguous / absent / readonly`。
第一次跑会把真实响应录进 `eval/cassettes/`,之后 `--replay` 离线复现同一份结果 ——
模型参数钉在 `eval/eval_config.json` 里,不跟着 `data/config.json` 飘,否则 cassette 全 miss。

单元测试(不联网、不要 key):

```bash
docker run --rm -v "$PWD/backend:/src" -w /src/tests repo_git-app python -m unittest discover -s .
```

## iOS / iPad 注意事项

- **语音必须 HTTPS**:`getUserMedia` 和 `SpeechRecognition` 要求 secure context。这是 8443 HTTPS 端口存在的全部理由;不用语音时走 8080 HTTP 即可,没有证书弹窗。
- **首次需用户手势**:Safari 第一次访问页面时麦克风权限要点击触发(点大麦克风按钮即可)
- **AudioContext 必须在手势同帧内 resume**:`useAudioMeter.js` 已经处理 — 同步构造 ctx + 立刻 resume,避免 await 后掉出手势栈
- **iPad RMS 显示问题**:频域分析器在 iPad 上被自动增益压成 0,我们用时域 `getByteTimeDomainData` 计算真实响度。touch/click/visibilitychange 也会重新 resume AudioContext。
- **Web Speech API 联网**:Apple 在云端识别中文,需要 iPad 能联外网。需要纯离线 → 用 Whisper:`./start.sh --whisper`,设置页勾"启用 Whisper"
- **iOS Safari 不擅长长 continuous SR**:本项目的设计已经规避了这个问题 —— 持续监听只为唤醒词、识别一旦命中就立即换成单次 SR
- **更专业的离线唤醒**(可选):接入 Picovoice Porcupine 即可,本仓库不默认集成(需个人 access key)
