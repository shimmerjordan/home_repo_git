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
    api_key: str = Field(default="", description="API key (leave empty for Ollama etc.)")
    model: str = Field(default="gpt-4o-mini", description="Model name")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    timeout: int = Field(default=60, ge=5, le=300)
    # Some providers (Ollama old versions, certain inference servers) don't support tool calling.
    # When false, we fall back to JSON-mode prompting.
    supports_tools: bool = Field(default=True)


class VoiceConfig(BaseModel):
    wake_words: list[str] = Field(default_factory=lambda: ["小库", "小仓", "管家"])
    confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    confirm_before_llm: bool = Field(
        default=True,
        description="发送给 LLM 前先口头确认识别文本是否正确,节省 token",
    )
    # TTS — names depend on the browser. Empty = browser default.
    tts_voice: str = Field(default="")
    tts_lang: str = Field(default="zh-CN")
    tts_rate: float = Field(default=1.05, ge=0.5, le=2.0)
    tts_pitch: float = Field(default=1.0, ge=0.0, le=2.0)
    # Whisper service URL inside docker network, or external one.
    whisper_url: str = Field(default="http://whisper:9000")
    whisper_enabled: bool = Field(default=False)


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._config = self._load()

    def _load(self) -> AppConfig:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return AppConfig(**data)
            except Exception:
                pass
        cfg = AppConfig()
        self._save(cfg)
        return cfg

    def _save(self, cfg: AppConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")

    def get(self) -> AppConfig:
        with self._lock:
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
            self._config = AppConfig(**current)
            self._save(self._config)
            return self._config.model_copy(deep=True)


store = ConfigStore(CONFIG_PATH)
