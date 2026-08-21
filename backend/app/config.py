"""Runtime configuration: persisted to JSON so user can change LLM provider via UI."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/data/config.json"))


class LLMConfig(BaseModel):
    base_url: str = Field(default="https://api.openai.com/v1", description="OpenAI-compatible base URL")
    # "openai" = /chat/completions (OpenAI/DeepSeek/Ollama/硅基流动...);
    # "anthropic" = /v1/messages (Claude 官方 / cc-trans 反代等 Anthropic 兼容网关)。
    api_format: str = Field(default="openai", description='API 格式: "openai" 或 "anthropic"')
    api_key: str = Field(default="", description="API key (leave empty for Ollama etc.)")
    model: str = Field(default="gpt-4o-mini", description="Model name")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    # Anthropic (Claude) 专用。Claude 新家族 (Opus 4.7+/Sonnet 5/Fable 5) 已移除
    # temperature/top_p/top_k (传了直接 400), 关键参数只有 model + thinking(mode) + effort。
    # 留空 = 不传该字段, 由模型用自身默认 —— 兼容所有 Claude 版本 (老模型如 Haiku 4.5 不认 adaptive/effort)。
    thinking: str = Field(default="", description='Claude thinking mode: "" (不传) | "adaptive" | "disabled"')
    effort: str = Field(default="", description='Claude effort: "" (不传) | low | medium | high | xhigh | max')
    timeout: int = Field(default=60, ge=5, le=300)
    # Some providers (Ollama old versions, certain inference servers) don't support tool calling.
    # When false, we fall back to JSON-mode prompting.
    supports_tools: bool = Field(default=True)
    # 输出 token 上限。**不要再往 512 那种量级调**: 带思考的模型 (Claude Opus/Sonnet)
    # 光 thinking 块就吃掉大半预算, 一句 4 个物品的批量操作实测要 490~550 output tokens。
    # 512 时 stop_reason=max_tokens, tool_use 的 input JSON 断在一半, operations 整个丢空 ——
    # 这就是"多物品操作经常不符合预期"的头号原因。低于 MIN_SANE_MAX_TOKENS 的旧配置
    # 会在载入时自动抬到 DEFAULT_MAX_TOKENS (见 ConfigStore._migrate)。
    max_tokens: int = Field(default=4096, ge=64, le=16384)
    # Fast mode: shorter system prompt + smaller inventory summary. Set true when you've
    # picked a light model (e.g. glm-4-flash, qwen2.5-7b) and want minimum latency.
    fast_mode: bool = Field(default=False)


class VoiceConfig(BaseModel):
    wake_words: list[str] = Field(default_factory=lambda: ["小库", "小仓", "管家"])
    confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    confirm_before_llm: bool = Field(
        default=True,
        description="发送给 LLM 前先口头确认识别文本是否正确,节省 token",
    )
    # 落库前先让人过一遍。AI 的物品匹配会出错 —— 说"存入洗发水"而库里只有"洗手液"时,
    # 老流程直接给洗手液加库存, 用户看到的只是一句汇总话术。打开这个开关后, 所有会改数据的
    # 操作 (take_out/put_in/consume/create_item/delete_item) 都只生成"待确认方案",
    # 每个单品给出候选下拉 + 可自填的新物品名, 由用户点确认才真正执行。
    # 只读意图 (find/list/assist) 不受影响, 查东西还要点一下确认是折磨。
    # 群机器人 (飞书/TG/钉钉) 没有界面可点, 不走这条路, 行为不变。
    confirm_before_apply: bool = Field(
        default=True,
        description="改动数据前先在界面上逐条确认 (物品匹配错了可当场改)",
    )
    # TTS — names depend on the browser. Empty = browser default.
    tts_enabled: bool = Field(default=True, description="是否朗读 AI 结果")
    tts_voice: str = Field(default="")
    tts_lang: str = Field(default="zh-CN")
    tts_rate: float = Field(default=1.05, ge=0.5, le=2.0)
    tts_pitch: float = Field(default=1.0, ge=0.0, le=2.0)
    # Whisper service URL inside docker network, or external one.
    whisper_url: str = Field(default="http://whisper:9000")
    whisper_enabled: bool = Field(default=False)


class DingTalkConfig(BaseModel):
    enabled: bool = Field(default=False, description="是否启用钉钉机器人 Webhook")
    # 钉钉自定义机器人"安全设置 > 加签"提供的签名秘钥 (SEC 开头), 用于校验入站 webhook 来源。
    # 留空则跳过签名校验 (仅供内网测试; 公网部署必须填!)。
    sign_secret: str = Field(default="")
    # 可选: 仅允许这些用户 (DingTalk 的 senderStaffId 或 senderNick) 调用机器人。
    # 留空 = 允许所有人在群里 @机器人.
    allowed_users: list[str] = Field(default_factory=list)
    # 钉钉群机器人对外发消息要用的 access_token (webhook URL 中的 access_token 参数)。
    # 仅当你想让机器人"主动"推消息时填; 被动回复用同步响应即可。
    outgoing_webhook: str = Field(default="")
    outgoing_sign_secret: str = Field(default="")


class TelegramConfig(BaseModel):
    """Telegram bot via long-polling. NAS makes outbound HTTPS to api.telegram.org —
    no public IP, no port-forwarding, no internal-network tunnel required. The
    flip side: Telegram itself is not reachable from China without a VPN."""
    enabled: bool = Field(default=False)
    bot_token: str = Field(default="", description="从 @BotFather 获取的 bot token")
    # 白名单 (允许调用的 chat_id 和 user_id, 留空 = 允许所有)。chat_id 通常是负数 (群组)。
    allowed_chat_ids: list[str] = Field(default_factory=list)
    allowed_user_ids: list[str] = Field(default_factory=list)


class FeishuConfig(BaseModel):
    """Feishu / Lark bot via Stream Mode (WebSocket). NAS makes outbound WSS to
    open.feishu.cn — works inside any LAN without public IP / port-forwarding.
    Requires the lark-oapi Python SDK (in requirements.txt)."""
    enabled: bool = Field(default=False)
    app_id: str = Field(default="", description="飞书自建应用的 App ID (cli_xxx)")
    app_secret: str = Field(default="")
    # 白名单 (chat_id 或 sender open_id, 留空 = 允许所有)
    allowed_chat_ids: list[str] = Field(default_factory=list)
    allowed_open_ids: list[str] = Field(default_factory=list)


class WebDAVConfig(BaseModel):
    """异地备份到任意 WebDAV 网盘 (坚果云 / Nextcloud / 群晖 / InfiniCLOUD 等)。
    备份内容: SQLite 库 (物品/位置/流水/审计) + config.json (app 设置) + 系统日志。
    支持选择性组件、GFS 分层保留、定时自动备份、AES-256 口令加密、恢复。"""
    enabled: bool = Field(default=False, description="是否启用 WebDAV 备份调度")
    url: str = Field(default="", description="WebDAV 根地址, 如 https://dav.jianguoyun.com/dav/")
    username: str = Field(default="", description="WebDAV 账号 (坚果云用邮箱)")
    password: str = Field(default="", description="WebDAV 密码 / 应用授权密码")
    remote_dir: str = Field(default="voice-storage-backups", description="远程子目录")
    # 选择性备份: 勾选哪些组件入包。inventory = 物品+位置 (整库快照),
    # transactions / audit 为逻辑 JSON 导出, logs = 系统日志文件。
    components: list[str] = Field(
        default_factory=lambda: ["settings", "inventory", "transactions", "audit", "logs"]
    )
    encrypt: bool = Field(default=True, description="是否用口令对备份包做 AES-256 加密")
    passphrase: str = Field(default="", description="加密口令 (留空且 encrypt=true 时跳过加密)")
    # 调度: manual=只手动; hourly=每小时; daily/weekly 在 hour 点触发。
    schedule: str = Field(default="manual", description="manual / hourly / daily / weekly")
    hour: int = Field(default=3, ge=0, le=23, description="daily/weekly 触发的小时 (本地时区)")
    # GFS 分层保留: 自动清理过期备份, 各保留最近 N 个。
    keep_daily: int = Field(default=7, ge=0, le=365)
    keep_weekly: int = Field(default=4, ge=0, le=520)
    keep_monthly: int = Field(default=6, ge=0, le=120)


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    dingtalk: DingTalkConfig = Field(default_factory=DingTalkConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    webdav: WebDAVConfig = Field(default_factory=WebDAVConfig)


# 低于这个值的 max_tokens 一定会截断带思考模型的多物品输出, 载入时强制抬高。
MIN_SANE_MAX_TOKENS = 2048
DEFAULT_MAX_TOKENS = 4096


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._config = self._load()

    @staticmethod
    def _migrate(cfg: AppConfig) -> list[str]:
        """就地修正明显有害的历史配置值。返回人话描述的改动列表(空 = 没动)。

        存在的理由: 光改 Field 的 default 对**已经存在**的 data/config.json 毫无作用 ——
        线上那份 max_tokens=512 会一直生效, 等于没修。
        """
        notes: list[str] = []
        if cfg.llm.max_tokens < MIN_SANE_MAX_TOKENS:
            notes.append(
                f"llm.max_tokens {cfg.llm.max_tokens} → {DEFAULT_MAX_TOKENS} "
                f"(过小会截断多物品的 tool 调用, operations 会整个丢空)"
            )
            cfg.llm.max_tokens = DEFAULT_MAX_TOKENS
        return notes

    def _load(self) -> AppConfig:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                cfg = AppConfig(**data)
                notes = self._migrate(cfg)
                if notes:
                    for n in notes:
                        print(f"[config] 自动迁移: {n}", flush=True)
                    self._save(cfg)
                return cfg
            except Exception:
                pass
        cfg = AppConfig()
        self._save(cfg)
        return cfg

    def _save(self, cfg: AppConfig) -> bool:
        """写盘。**失败不抛** —— 这个方法在 import 期就会被调用 (_load 的迁移和
        首次建档), 让它抛就等于"data 目录只读时后端直接起不来"。写不进去时退回
        用内存里的配置继续跑, 只是改动不持久化。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
            return True
        except OSError as exc:
            print(f"[config] 无法写入 {self.path}: {exc} —— 本次改动不会持久化",
                  flush=True)
            return False

    def get(self) -> AppConfig:
        with self._lock:
            return self._config.model_copy(deep=True)

    def reload(self) -> AppConfig:
        """从磁盘重新加载配置 (备份恢复覆盖 config.json 后调用)。"""
        with self._lock:
            self._config = self._load()
            return self._config.model_copy(deep=True)

    def update(self, patch: dict[str, Any]) -> AppConfig:
        with self._lock:
            current = self._config.model_dump()
            # Deep-merge top-level sections.
            for key, value in patch.items():
                if isinstance(value, dict) and isinstance(current.get(key), dict):
                    current[key].update(value)
                else:
                    current[key] = value
            merged = AppConfig(**current)
            # UI 也拦一道: 把 max_tokens 调回 512 这种值会立刻让多物品操作重新变成静默丢失,
            # 所以这里同样抬高。设置页会写明这条下限。
            self._migrate(merged)
            self._config = merged
            self._save(self._config)
            return self._config.model_copy(deep=True)


store = ConfigStore(CONFIG_PATH)
