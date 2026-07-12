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
4. **AI 解析意图** → 执行
5. **低置信度二次确认** *阈值默认 0.5*
   - LLM 返回 `confidence < threshold` 且是修改性操作(取出/存入)时,先不动数据,弹卡片 + 语音播报问 "是想 X 吗",同样支持口头/按钮二选一
   - 模糊匹配时给候选物品列表,可点 "选这个"
6. **需求型问答**: "我发烧了家里有什么药" 这种问句会走 `assist` 意图,返回带"用途"列的推荐表,并在 3D 视图里同时高亮所有相关物品
7. **批量操作**: 一句话包含多个物品/动作时(如 "我消耗了一瓶水和两片药"、"拿了卷尺,顺便放回螺丝刀"),LLM 返回 `operations` 数组,后端逐条执行并汇总播报;部分失败时话术如实说明哪条没找到。低置信度时整批待确认,确认后一次性执行

## LLM 接入(完全可配置)

- 两种接口格式,设置页可切换:
  - **OpenAI 兼容**(默认): 任何提供 `/v1/chat/completions` 的服务都行
  - **Anthropic (Claude)**: `/v1/messages`,支持 Claude 官方 API 和 **cc-trans 反代**(API Key 填 `cct-...` 客户端令牌,Base URL 填 cc-trans 地址如 `http://host.docker.internal:8787`)
- 内置预设:**OpenAI / 硅基流动 / DeepSeek / Ollama / 智谱 GLM / Claude 官方 / cc-trans**
- 运行时改 `base_url` + `api_format` + `api_key` + `model` + `temperature` + `timeout` + `max_tokens`,**无需重启**
- 自动用工具调用(OpenAI `tool_calls` / Anthropic `tool_use`),模型不支持时降级为 JSON 模式
- "测试连接"按钮一键验证

### 加速 Tips

- **极速模式**: 设置页勾选,精简系统提示 + 减少给 AI 的候选物品数
- **max_tokens 256~512** 一般中文够用
- **轻量模型**: glm-4-flash / qwen2.5-7b-instruct / siliconflow 上的 Qwen 系列
- **关掉"发送前确认"** 省一次往返(但容易误识别就直接执行)
- **关掉"朗读 AI 结果"** 省 TTS 播放等待

## iOS / iPad 注意事项

- **必须 HTTPS**:`getUserMedia` 和 `SpeechRecognition` 要求 secure context。这是 8443 端口存在的全部理由。
- **首次需用户手势**:Safari 第一次访问页面时麦克风权限要点击触发(点大麦克风按钮即可)
- **AudioContext 必须在手势同帧内 resume**:`useAudioMeter.js` 已经处理 — 同步构造 ctx + 立刻 resume,避免 await 后掉出手势栈
- **iPad RMS 显示问题**:频域分析器在 iPad 上被自动增益压成 0,我们用时域 `getByteTimeDomainData` 计算真实响度。touch/click/visibilitychange 也会重新 resume AudioContext。
- **Web Speech API 联网**:Apple 在云端识别中文,需要 iPad 能联外网。需要纯离线 → 用 Whisper:`./start.sh --whisper`,设置页勾"启用 Whisper"
- **iOS Safari 不擅长长 continuous SR**:本项目的设计已经规避了这个问题 —— 持续监听只为唤醒词、识别一旦命中就立即换成单次 SR
- **更专业的离线唤醒**(可选):接入 Picovoice Porcupine 即可,本仓库不默认集成(需个人 access key)
