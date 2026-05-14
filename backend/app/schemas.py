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
    action: str = Field(pattern="^(take_out|put_in|adjust)$")
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


class IntentResult(BaseModel):
    intent: str  # find / take_out / put_in / list / assist / unknown / clarify
    confidence: float
    speech: str  # what to read out loud to the user
    needs_confirmation: bool = False
    pending_action: Optional[dict[str, Any]] = None  # to replay if user confirms
    candidates: list[IntentCandidate] = []
    recommendations: list[IntentRecommendation] = []
    executed: bool = False
    transaction_id: Optional[int] = None
    raw: Optional[dict[str, Any]] = None


# --- Config ---

class LLMConfigPatch(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    timeout: Optional[int] = None
    supports_tools: Optional[bool] = None
    max_tokens: Optional[int] = None
    fast_mode: Optional[bool] = None


class VoiceConfigPatch(BaseModel):
    wake_words: Optional[list[str]] = None
    confidence_threshold: Optional[float] = None
    confirm_before_llm: Optional[bool] = None
    tts_enabled: Optional[bool] = None
    tts_voice: Optional[str] = None
    tts_lang: Optional[str] = None
    tts_rate: Optional[float] = None
    tts_pitch: Optional[float] = None
    whisper_url: Optional[str] = None
    whisper_enabled: Optional[bool] = None


class ConfigPatch(BaseModel):
    llm: Optional[LLMConfigPatch] = None
    voice: Optional[VoiceConfigPatch] = None


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
