from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# --- Locations ---

class LocationBase(BaseModel):
    name: str
    kind: str = "room"
    parent_id: Optional[int] = None
    note: str = ""
    geometry: Optional[dict[str, Any]] = None


class LocationCreate(LocationBase):
    pass


class LocationUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    parent_id: Optional[int] = None
    note: Optional[str] = None
    geometry: Optional[dict[str, Any]] = None


class LocationOut(LocationBase):
    id: int
    uuid: str = ""
    full_path: str = ""
    created_at: datetime

    class Config:
        from_attributes = True


# --- Items ---

class ItemBase(BaseModel):
    name: str
    aliases: str = ""
    category: str = ""
    tags: str = ""
    quantity: int = 1
    price: float = 0.0
    note: str = ""
    location_id: Optional[int] = None
    pos_x: Optional[float] = None
    pos_z: Optional[float] = None


class ItemCreate(ItemBase):
    pass


class ItemUpdate(BaseModel):
    name: Optional[str] = None
    aliases: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[str] = None
    quantity: Optional[int] = None
    price: Optional[float] = None
    note: Optional[str] = None
    location_id: Optional[int] = None
    pos_x: Optional[float] = None
    pos_z: Optional[float] = None


class ItemOut(ItemBase):
    id: int
    location_path: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# --- Transactions ---

class TransactionCreate(BaseModel):
    item_id: int
    # consume = "用完了/扔了", semantically distinct from take_out (借出, needs return).
    action: str = Field(pattern="^(take_out|put_in|adjust|consume)$")
    quantity: int = 1
    location_id: Optional[int] = None
    note: str = ""


class TransactionOut(BaseModel):
    id: int
    item_id: int
    item_name: str = ""
    action: str
    quantity: int
    location_id: Optional[int]
    location_path: Optional[str] = None
    note: str
    created_at: datetime
    # 能否一键回撤。服务端算而不是前端猜: 前端只拿到最近 N 条, 判断不了
    # "这是不是该物品的最后一条流水" (/api/revise/undo 的硬护栏)。
    undoable: bool = False
    undo_note: str = ""

    class Config:
        from_attributes = True


# --- Voice / intent ---

class VoiceQuery(BaseModel):
    text: str
    context: Optional[dict[str, Any]] = None  # e.g. previous turn for confirmation


class IntentCandidate(BaseModel):
    item_id: int
    item_name: str
    location_path: Optional[str] = None
    score: float


class IntentRecommendation(BaseModel):
    item_id: int
    purpose: str = ""


class IntentOption(BaseModel):
    """待确认方案里, 某一条操作可以落到的一个目标。

    key 的取值: "new" (建新物品) / "skip" (跳过这条) / "i:<item_id>" (已有物品)。
    kind 只影响前端怎么标注 (exact 打勾, fuzzy 打"名字相近"角标)。
    """
    key: str
    kind: str = ""            # exact | fuzzy | new
    item_id: Optional[int] = None
    label: str = ""
    sublabel: str = ""
    score: Optional[float] = None


class IntentOperationResult(BaseModel):
    """Per-operation outcome. Filled for single-op utterances too, so the frontend
    has one uniform shape to render and revise."""
    intent: str  # find / take_out / put_in / consume / create_item / delete_item
    item_id: Optional[int] = None
    item_name: Optional[str] = None
    quantity: int = 1
    executed: bool = False
    transaction_id: Optional[int] = None
    speech: str = ""
    # 这条操作匹配到的备选 (命中项排第一)。让用户看清"我操作的是哪个"并能改判。
    candidates: list[IntentCandidate] = []
    matched_by: str = ""     # exact | fuzzy | created | none
    pending: bool = False    # True = 已识别但未执行, 等用户确认
    location_path: Optional[str] = None
    remaining: Optional[int] = None   # 操作后该物品的数量

    # ---- 以下仅在"待确认方案" (stage="plan") 里有值 ----
    # 这一条可以落到哪些目标上。前端渲染成下拉/滚动选择区, 含"新建"那一项。
    options: list[IntentOption] = []
    selected: str = ""              # 预选的 option key ("new" / "skip" / "i:<id>")
    allow_new: bool = False         # 是否允许建新物品 (取出/用完一个不存在的东西没意义)
    force_new: bool = False         # 用户明说了"新增" → 默认按全新物品处理
    new_name_default: str = ""      # 选中"新建"时输入框的默认值
    reason: str = ""                # 为什么这么预选 (给用户看的一句人话)
    location_id: Optional[int] = None
    location_name: Optional[str] = None


class IntentResult(BaseModel):
    intent: str  # find / take_out / put_in / list / assist / unknown / clarify / batch
    # direct = 已按老流程直接执行/查询完; plan = 待确认方案, 一个字都还没落库;
    # applied = 用户确认后执行完。
    stage: str = "direct"
    plan_id: Optional[str] = None
    confidence: float
    speech: str  # what to read out loud to the user
    needs_confirmation: bool = False
    pending_action: Optional[dict[str, Any]] = None  # to replay if user confirms
    candidates: list[IntentCandidate] = []
    recommendations: list[IntentRecommendation] = []
    executed: bool = False
    transaction_id: Optional[int] = None
    operations: list[IntentOperationResult] = []  # batch utterances: one entry per op
    raw: Optional[dict[str, Any]] = None


class IntentDecision(BaseModel):
    """用户在方案界面上对一条操作的最终决定。"""
    intent: str
    option_key: str = "skip"        # "new" / "skip" / "i:<item_id>"
    new_item_name: Optional[str] = None
    location_id: Optional[int] = None
    location_name: Optional[str] = None
    quantity: int = 1


class IntentApply(BaseModel):
    text: str = ""
    plan_id: Optional[str] = None
    decisions: list[IntentDecision] = []


# --- Config ---

class LLMConfigPatch(BaseModel):
    base_url: Optional[str] = None
    api_format: Optional[str] = None  # "openai" | "anthropic"
    api_key: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    thinking: Optional[str] = None  # Claude: "" | "adaptive" | "disabled"
    effort: Optional[str] = None  # Claude: "" | low | medium | high | xhigh | max
    timeout: Optional[int] = None
    supports_tools: Optional[bool] = None
    max_tokens: Optional[int] = None
    fast_mode: Optional[bool] = None


class VoiceConfigPatch(BaseModel):
    wake_words: Optional[list[str]] = None
    confidence_threshold: Optional[float] = None
    confirm_before_llm: Optional[bool] = None
    confirm_before_apply: Optional[bool] = None
    tts_enabled: Optional[bool] = None
    tts_voice: Optional[str] = None
    tts_lang: Optional[str] = None
    tts_rate: Optional[float] = None
    tts_pitch: Optional[float] = None
    whisper_url: Optional[str] = None
    whisper_enabled: Optional[bool] = None


class DingTalkConfigPatch(BaseModel):
    enabled: Optional[bool] = None
    sign_secret: Optional[str] = None
    allowed_users: Optional[list[str]] = None
    outgoing_webhook: Optional[str] = None
    outgoing_sign_secret: Optional[str] = None


class TelegramConfigPatch(BaseModel):
    enabled: Optional[bool] = None
    bot_token: Optional[str] = None
    allowed_chat_ids: Optional[list[str]] = None
    allowed_user_ids: Optional[list[str]] = None


class FeishuConfigPatch(BaseModel):
    enabled: Optional[bool] = None
    app_id: Optional[str] = None
    app_secret: Optional[str] = None
    allowed_chat_ids: Optional[list[str]] = None
    allowed_open_ids: Optional[list[str]] = None


class WebDAVConfigPatch(BaseModel):
    enabled: Optional[bool] = None
    url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    remote_dir: Optional[str] = None
    components: Optional[list[str]] = None
    encrypt: Optional[bool] = None
    passphrase: Optional[str] = None
    schedule: Optional[str] = None
    hour: Optional[int] = None
    keep_daily: Optional[int] = None
    keep_weekly: Optional[int] = None
    keep_monthly: Optional[int] = None


class ConfigPatch(BaseModel):
    llm: Optional[LLMConfigPatch] = None
    voice: Optional[VoiceConfigPatch] = None
    dingtalk: Optional[DingTalkConfigPatch] = None
    telegram: Optional[TelegramConfigPatch] = None
    feishu: Optional[FeishuConfigPatch] = None


# --- Audit log ---

class AuditLogOut(BaseModel):
    id: int
    ts: datetime
    entity_type: str
    entity_id: Optional[int] = None
    entity_name: str = ""
    action: str
    changes: dict[str, Any] = {}
    summary: str = ""
    source: str = ""

    class Config:
        from_attributes = True
